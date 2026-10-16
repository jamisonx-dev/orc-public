"""Live play-state feed: what the instrument is doing RIGHT NOW, cheaply.

Why this exists: ~/orc1_state.json is a settings snapshot written by
print_status() after a button press, so it answers "what did I last set", not
"what is playing". A HUD / kiosk needs the second one.

Design (read this before changing it):
  * The brain only READS attributes it already keeps (seq._global_step, the
    arp's bpm, state["held_notes"] ...). Nothing in the MIDI or clock hot path
    is added to, except ActivityTracker.hit() on a synth-bus note-on, which is
    two dict stores.
  * A daemon thread wakes ~30x/s, builds a small dict, and writes it ATOMICALLY
    to tmpfs (/dev/shm) ONLY when something changed (plus a 1 s heartbeat).
    Idle cost is a handful of attribute reads; a tick that finds nothing new
    writes nothing.
  * Ages are never stored, because an age changes every call and would defeat
    change detection: the transport carries the step counter plus the age of
    that step AT PUBLISH TIME ("age_ms", ignored by the comparison), and the
    page extrapolates the playhead from its own clock. Module activity is a hit
    COUNTER; the page pulses when the counter goes up.
  * The web process and the pygame HUD only ever read the file. Nobody can
    block the brain by reading it slowly.
"""
import json
import os
import threading
import time

PATH = "/dev/shm/orc1_playstate.json"
SCHEMA = 1

# Keys inside the snapshot that change every call and must not count as "new".
_VOLATILE_TOP = ("t", "n")
_VOLATILE_TRANSPORT = ("age_ms",)

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    """60 -> 'C4' (middle C = C4)."""
    n = int(n)
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"


def held_names(held):
    """state['held_notes'] is a list of (note, port, channel); show each pitch
    once, low to high."""
    try:
        return [note_name(n) for n in sorted({int(h[0]) for h in held})]
    except Exception:
        return []


class ActivityTracker:
    """Per-MIDI-channel hit counter for the synth bus.

    hit() is called from send_to() for every synth-bus note-on, so it must be
    as cheap as it can be: one dict store of a fresh 2-list (no read-modify-
    write across threads; a lost increment only means a pulse is missed, never
    a crash)."""

    def __init__(self):
        self._hits = {}

    def hit(self, channel, velocity):
        prev = self._hits.get(channel)
        self._hits[channel] = [(prev[0] + 1) if prev else 1, int(velocity)]

    def by_module(self, bindings):
        """{module: {'hits': n, 'vel': v}} for bound modules that have played."""
        out = {}
        for mod, ch in dict(bindings).items():
            h = self._hits.get(int(ch))
            if h:
                out[mod] = {"hits": h[0], "vel": h[1]}
        return out


def _stable(snap):
    """The snapshot with its volatile fields removed, as a comparable string."""
    s = {k: v for k, v in snap.items() if k not in _VOLATILE_TOP}
    tr = s.get("transport")
    if isinstance(tr, dict):
        s["transport"] = {k: v for k, v in tr.items()
                          if k not in _VOLATILE_TRANSPORT}
    return json.dumps(s, sort_keys=True, separators=(",", ":"))


class PlayStatePublisher(threading.Thread):
    """Calls snapshot_fn() ~hz times a second, writes the file on change."""

    def __init__(self, snapshot_fn, path=PATH, hz=30.0, heartbeat=1.0,
                 wall=time.time, mono=time.monotonic):
        super().__init__(daemon=True, name="playstate")
        self._fn = snapshot_fn
        self.path = path
        self._period = 1.0 / float(hz)
        self._heartbeat = float(heartbeat)
        self._wall = wall
        self._mono = mono
        self._stop_evt = threading.Event()
        self._last_stable = None
        self._last_write = -1e9
        self._n = 0
        self.writes = 0
        self.errors = 0

    def tick(self):
        """One iteration. Returns True when the file was (re)written."""
        try:
            snap = self._fn()
        except Exception:
            self.errors += 1
            return False
        if not isinstance(snap, dict):
            self.errors += 1
            return False
        key = _stable(snap)
        now = self._mono()
        if key == self._last_stable and (now - self._last_write) < self._heartbeat:
            return False
        self._n += 1
        snap = dict(snap)
        snap["v"] = SCHEMA
        snap["t"] = self._wall()
        snap["n"] = self._n
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snap, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            self.errors += 1
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        self._last_stable = key
        self._last_write = now
        self.writes += 1
        return True

    def run(self):
        while not self._stop_evt.is_set():
            self.tick()
            self._stop_evt.wait(self._period)

    def stop(self):
        self._stop_evt.set()


def read(path=PATH, wall=time.time):
    """(state_dict, age_seconds) or (None, None). age = time since the brain
    last wrote it, so a dead brain shows up as a growing age, not stale data
    that looks live."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d, max(0.0, wall() - float(d.get("t", 0)))
    except Exception:
        return None, None


def sse_events(path=PATH, stop=lambda: False, poll=0.03, keepalive=15.0,
               sleep=time.sleep, stat=os.stat, mono=time.monotonic):
    """Generator of Server-Sent-Events strings: one 'data:' frame whenever the
    file changes, a comment line every `keepalive` s so proxies / the browser
    don't time the connection out. Reads the file, never the brain."""
    last = None
    last_sent = mono()
    while not stop():
        try:
            m = stat(path).st_mtime_ns
        except OSError:
            m = None
        if m is not None and m != last:
            last = m
            try:
                with open(path) as f:
                    body = f.read().strip()
                if body:
                    yield f"data: {body}\n\n"
                    last_sent = mono()
            except OSError:
                pass
        elif mono() - last_sent >= keepalive:
            yield ": keepalive\n\n"
            last_sent = mono()
        sleep(poll)
