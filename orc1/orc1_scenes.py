#!/usr/bin/env python3
"""ORC-1 SCENES engine -- capture / recall / timing (step 2 of scenes).

Plan: _planning/ORC1_Scenes_and_Web_Reorg_Plan.md, Part 1. The settings list
(what a setting is, how to read it, how to set it live) is
orc1_settings_registry.py; this module stores scenes and decides WHEN a recall
lands. The brain (orc1.py) owns one SceneEngine and keeps its hooks thin; the
engine never imports the brain, so it runs against fakes in _test_scenes.py.

A SCENE is a numbered slot 1-128 holding a name, a default timing, and only the
settings ticked for it: {setting id: value}. Settings a scene does not hold are
left alone by a recall. A value of None = ticked but not known (capture had no
readable owner, e.g. sampler decks): recall skips it until a value is typed in.

STORE  ~/.orc1_scenes.json (path resolved when SceneStore is BUILT, never at
       import). The brain is the only writer. Every write is a read-modify-write
       under an flock on "<file>.lock" and lands by tmp + fsync + rename, so a
       reader never sees half a file. Shape:
         {"version": 1, "slots": {"<n>": {"name", "timing", "values": {...}}}}
       Empty slots are simply absent.

TIMING  IMMEDIATE | NEXT_BEAT | NEXT_BAR (default) | BARS_2 | BARS_4
  - A recall that is not IMMEDIATE waits as the ONE pending recall. A newer
    recall (any timing) replaces it; counting starts again from the new press.
  - NEXT_BEAT = the next beat; NEXT_BAR = the next bar start; BARS_2 / BARS_4 =
    the 2nd / 4th bar start from the press (counted, not aligned to a phrase).
  - WHERE THE BAR COMES FROM (owner 2026-10-26: "the Fire's bar"):
      * the Fire, when it runs our firmware with bar messages (hello version
        >= 2, host 0E 01 sent) and a 0F bar message arrived within ~1.25 bar
        lengths (a STOP from the Fire drops it at once): each 0F is a bar start;
      * otherwise the ORC: the brain feeds orc_beat(downbeat) from its clock
        (the sequencer's grid while it runs, the BeatBuddy's own beat-in-bar
        while it plays, else the arp's 24-ppqn pulse grid).
  - NOTHING PLAYING -> NEXT_* lands at once (decided 2026-10-26): no bar to
    wait for, and it matches the Fire, whose PATTERN buttons and host 0D switch
    at once while it is stopped. A recall left pending when the transport stops
    lands at the next poll() (the brain polls ~4 Hz). playing_from() is the rule.

THE FIRE PATTERN (setting "fire.pattern", recall = host 0D <n>): the Fire
applies 0D at ITS OWN next bar, so when the Fire is the bar source the 0D goes
out ONE BAR EARLY (at the press for NEXT_BAR, after bar N-1 for BARS_N) and the
Fire switches on the very bar the rest of the scene lands on. On the ORC clock
it goes out with the rest of the burst.
  - Song mode: the Fire reads 0D n as a song SLOT, so while the Fire is known to
    be in song mode (0F / 0D reply) fire.pattern is skipped ("Fire in song mode").
  - A replaced recall whose 0D already went out leaves that pattern queued on
    the Fire; if the new recall does not send its own 0D right now, the queue is
    cleared by re-sending the pattern playing ("n = the pattern already playing
    clears the queue", USER_GUIDE) -- only when the Fire is known to be in
    PATTERN mode (in song mode that would jump the song).
  - A Fire on firmware without 0D (hello version 1) or no Fire = skipped.

SENDING: one persistent worker thread (a queue) sends each burst; the clock
thread that notices the boundary only enqueues. The burst is PACED (CHUNK
messages, then PACE_S sleep): every reader of the internal synth bus has a
200-cell ALSA input pool (/proc/asound/seq/clients, measured 2026-10-26: each
"RtMidiIn Client" Input pool 200; the bus has 7 readers and EVERY event goes to
all of them), drained by rtmidi's native input thread. 32 events per chunk is
6x under that pool and a 2 ms gap lets every reader drain, so ~300 CCs land in
10 chunks, ~20 ms of gaps. A setting whose current value is already known and
equal is not sent (brain-held values read live; action settings read from
files written at once), so a recall sends changes. Senders are injected:
    send_synth_cc(channel 1-16, cc, value)   internal synth bus CC (raise/False = failed)
    call_ctrl_cc(cc, value)                  orc1.handle_ctrl_cc
    call_ctrl_pc(program)                    orc1.handle_ctrl_pc
    run_action(cmd dict)                     orc1._execute_cmd
    burst_guard()                            context manager around the burst
                                             (the brain silences per-message
                                             print_status; state is written once
                                             at the end via on_change)
and the Fire link (FireBarLink below, or a fake) for 0D.

Pure stdlib. Importing it opens nothing and expands no `~`.
"""
import contextlib
import json
import os
import queue
import threading
import time

try:
    import fcntl as _fcntl                # Linux (the Pi); absent on Windows
except ImportError:                       # pragma: no cover - PC test runs
    _fcntl = None

N_SLOTS = 128
TIMINGS = ("IMMEDIATE", "NEXT_BEAT", "NEXT_BAR", "BARS_2", "BARS_4")
DEFAULT_TIMING = "NEXT_BAR"
_BARS = {"NEXT_BAR": 1, "BARS_2": 2, "BARS_4": 4}
STORE_FILE = "~/.orc1_scenes.json"
FIRE_PATTERN_ACTION = "FIRE_PATTERN"      # the registry's fire.pattern set action
NOT_READY = "settings list loading"

CHUNK = 32                                # messages per paced chunk (see SENDING)
PACE_S = 0.002                            # gap between chunks

# Fire host protocol (fire_firmware fw/fireseq USER_GUIDE, v0.30)
ORC_HDR = [0x7D, 0x4F, 0x52, 0x43]        # F0 7D 'O' 'R' 'C' <cmd> ... F7
CMD_HELLO, CMD_QUEUE_PAT, CMD_BARS, MSG_BAR = 0x00, 0x0D, 0x0E, 0x0F
# 06 = the Fire's last crash record (fw host protocol). Reply length by record version, the table in
# ~/fire_fw/host_vu.py (decode_crash); bytes: 5 version, 6 valid, 7 cause, 8 stage, 9 count, 10 total,
# then 5-byte words from 11 (word 0 = PC at 11-15, word 6 = uptime ms at 41-45).
CMD_CRASH = 0x06
CRASH_REPLY_LENS = {1: 46, 2: 61, 3: 91}
NO_SONG = 127                             # 0F song slot byte when not in song mode
BARS_VERSION = 2                          # hello version that has 0D / 0E / 0F
FIRE_SEQ_PORTS = ("orc fw seq", "flame seq")   # our sequencer firmware's port: old name, and FLAME (2026-11-12)

# The Fire is the bar source while its 0F messages keep coming: the last one
# within FIRE_BAR_GRACE bar lengths (FIRE_BAR_FIRST_S before a 2nd bar has
# given a length; never under FIRE_BAR_MIN_S).
FIRE_BAR_GRACE = 1.25
FIRE_BAR_FIRST_S = 6.0
FIRE_BAR_MIN_S = 1.0


def valid_slot(slot):
    try:
        n = int(slot)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= N_SLOTS else None


def slot_for_cc_value(value):
    """ch 13 SCENE_RECALL_CC: value N = scene N (1-127); 0 = None (ignored), so
    a momentary button's release (value 0) recalls nothing."""
    v = max(0, min(127, int(value)))
    return v or None


def clock_running(last_pulse_wall, bpm, now, beats=2.0):
    """Clock pulses count as running only if one arrived within `beats` beats
    at the last known tempo."""
    if last_pulse_wall is None:
        return False
    beat_s = 60.0 / max(20.0, float(bpm or 120.0))
    return (now - last_pulse_wall) <= beats * beat_s


def playing_from(seq_running, bb_playing, arp_notes, ext_clock, pulses_running, last_transport):
    """Is anything playing that has a beat to land on?
    seq_running: the ORC sequencer (an outside START / STOP starts / stops it);
    bb_playing: the BeatBuddy mind; arp_notes: the arp is on and holding a chord;
    ext_clock / pulses_running: following an outside clock that is ticking;
    last_transport: the last 'start' / 'stop' / 'continue' seen from outside,
    or None. TRANSPORT WINS over pulses: an MPC / RC-505 that keeps sending clock
    while stopped counts as stopped once it has sent a STOP."""
    if seq_running or bb_playing or arp_notes:
        return True
    if ext_clock and pulses_running:
        return last_transport != "stop"
    return False


def _empty_slot():
    return {"name": "", "timing": DEFAULT_TIMING, "values": {}}


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SceneStore:
    def __init__(self, path=None):
        self.path = os.path.expanduser(path or STORE_FILE)
        self._tlock = threading.RLock()

    class _Flock:
        def __init__(self, store):
            self.store, self.f = store, None

        def __enter__(self):
            self.store._tlock.acquire()
            if _fcntl is not None:
                try:
                    self.f = open(self.store.path + ".lock", "a")
                    _fcntl.flock(self.f.fileno(), _fcntl.LOCK_EX)
                except OSError:
                    self.f = None
            return self

        def __exit__(self, *exc):
            if self.f is not None:
                try:
                    _fcntl.flock(self.f.fileno(), _fcntl.LOCK_UN)
                finally:
                    self.f.close()
            self.store._tlock.release()
            return False

    def locked(self):
        return SceneStore._Flock(self)

    def load(self):
        """{"version": 1, "slots": {"<n>": slot}} -- tolerant of a missing or
        damaged file (an empty store), and of junk slots (dropped)."""
        try:
            with open(self.path) as f:
                data = json.load(f)
        except Exception:
            data = {}
        slots = {}
        for k, s in (data.get("slots") or {}).items() if isinstance(data, dict) else ():
            n = valid_slot(k)
            if n is None or not isinstance(s, dict):
                continue
            t = s.get("timing")
            slots[str(n)] = {"name": str(s.get("name") or "")[:40],
                             "timing": t if t in TIMINGS else DEFAULT_TIMING,
                             "values": dict(s.get("values") or {})}
        return {"version": 1, "slots": slots}

    def _write(self, data):
        d = os.path.dirname(self.path) or "."
        tmp = os.path.join(d, ".%s.%d.%d.tmp" % (os.path.basename(self.path), os.getpid(),
                                                 threading.get_ident()))
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def update(self, slot, fn):
        """Locked read-modify-write of one slot. fn(slot dict) edits it in place
        and returns a result; a slot left with no values, no name and the default
        timing is dropped (= empty)."""
        n = valid_slot(slot)
        if n is None:
            raise ValueError(f"scene slot must be 1-{N_SLOTS}, got {slot!r}")
        with self.locked():
            data = self.load()
            s = data["slots"].get(str(n)) or _empty_slot()
            res = fn(s)
            if s["values"] or s["name"] or s["timing"] != DEFAULT_TIMING:
                data["slots"][str(n)] = s
            else:
                data["slots"].pop(str(n), None)
            self._write(data)
            return res

    def get(self, slot):
        n = valid_slot(slot)
        if n is None:
            return None
        return self.load()["slots"].get(str(n))

    def clear(self, slot):
        n = valid_slot(slot)
        if n is None:
            raise ValueError(f"scene slot must be 1-{N_SLOTS}, got {slot!r}")
        with self.locked():
            data = self.load()
            had = data["slots"].pop(str(n), None) is not None
            self._write(data)
            return had


# ---------------------------------------------------------------------------
# Worker: ONE persistent thread fed by a queue
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, name="scene-recall"):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name=name)
        self.thread.start()

    def _run(self):
        while True:
            fn = self.q.get()
            try:
                fn()
            except Exception:
                pass

    def __call__(self, fn):
        self.q.put(fn)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SceneEngine:
    def __init__(self, store, registry, send_synth_cc, call_ctrl_cc, call_ctrl_pc, run_action,
                 fire=None, bindings=lambda: {}, recall_ctx=lambda: {}, is_playing=lambda: False,
                 brain_read=None, clock=time.monotonic, wall=time.time, spawn=None,
                 log=print, on_change=None, require_ok=None, burst_guard=None,
                 sleep=time.sleep, chunk=CHUNK, pace_s=PACE_S):
        """registry: orc1_settings_registry (or a fake with by_id / read_one /
        build_recall). fire: a FireBarLink-like object (send_pattern(n) ->
        (ok, why), current_pattern(), in_song_mode(), bars_enabled(), stopped()),
        or None. brain_read(ids) -> {id: live value} for the settings the brain
        itself holds (SETTINGS_READ). spawn: None = one persistent Worker."""
        self.store, self.R = store, registry
        self.send_synth_cc, self.call_ctrl_cc = send_synth_cc, call_ctrl_cc
        self.call_ctrl_pc, self.run_action = call_ctrl_pc, run_action
        self.fire, self.bindings, self.recall_ctx = fire, bindings, recall_ctx
        self.is_playing_fn, self.brain_read = is_playing, brain_read
        self.clock, self.wall, self.log = clock, wall, log
        self.spawn = spawn if spawn is not None else Worker()
        self.on_change = on_change
        self.require_ok = require_ok or _rack_require_ok
        self.burst_guard = burst_guard or contextlib.nullcontext
        self.sleep, self.chunk, self.pace_s = sleep, max(1, int(chunk)), float(pace_s)
        self._notready = getattr(registry, "NotReady", None) or _NeverRaised
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self.pending = None               # the one waiting recall (dict) or None
        self.last = None                  # what the last burst did
        self._fire_bar_t = None           # monotonic time of the last Fire 0F
        self._fire_bar_len = None         # seconds between the last two
        self._fire_was_active = False
        self.fire_last_bar = None         # (pattern, song slot, bar) of the last 0F

    # -- edit -------------------------------------------------------------------
    def _current(self, ids, reg, only_cheap=False):
        """{id: current value} (None = not known). Brain-held settings (read kind
        "state" / "brain") come LIVE from brain_read. only_cheap: only those, and
        action settings read from a file (written at once): the module hosts'
        CC files lag ~1.5 s, so a CC is never judged unchanged from them."""
        live_ids = [i for i in ids if (reg[i].get("read") or {}).get("kind") in ("state", "brain")]
        live = {}
        if live_ids and self.brain_read:
            try:
                live = self.brain_read(live_ids) or {}
            except Exception as e:
                self.log(f"[scenes] live read failed ({e})")
        out, cache = {}, {}
        for i in ids:
            if i in live_ids and self.brain_read:
                out[i] = live.get(i)
                continue
            rd = reg[i].get("read") or {}
            st = reg[i].get("set") or {}
            if only_cheap and not (rd.get("kind") == "json" and st.get("kind") == "action"
                                   and not st.get("per_key")):
                continue
            try:
                out[i] = self.R.read_one(reg[i], cache)[0]
            except Exception:
                out[i] = None
        return out

    def capture(self, slot, ids, merge=False):
        """Read each ticked setting's value now and store it in `slot` (the slot
        then holds EXACTLY these ids, unless merge=True). -> {"ok", "values",
        "unread": {id: why}}. Brain-held settings come LIVE from brain_read, not
        from ~/orc1_state.json (which lags after a ch-13 knob)."""
        try:
            reg = self.R.by_id()
        except self._notready:
            return {"ok": False, "error": NOT_READY}
        ids = list(dict.fromkeys(ids or []))
        bad = [i for i in ids if i not in reg]
        if bad:
            return {"ok": False, "error": f"unknown setting id(s): {bad[:5]}"}
        nots = [i for i in ids if not reg[i]["sceneable"]]
        if nots:
            return {"ok": False, "error": f"not sceneable: {nots[:5]}"}
        live_ids = [i for i in ids if (reg[i].get("read") or {}).get("kind") in ("state", "brain")]
        live = {}
        if live_ids and self.brain_read:
            try:
                live = self.brain_read(live_ids) or {}
            except Exception as e:
                self.log(f"[scenes] live read failed ({e}); using the state file")
        vals, unread, cache = {}, {}, {}
        for i in ids:
            if i in live and live[i] is not None:
                vals[i] = live[i]
                continue
            v, _src, why = self.R.read_one(reg[i], cache)
            vals[i] = v
            if v is None:
                unread[i] = why or "no readable owner"

        def edit(s):
            if not merge:
                s["values"] = {}
            s["values"].update(vals)
        self.store.update(slot, edit)
        self._changed()
        return {"ok": True, "slot": valid_slot(slot), "values": vals, "unread": unread}

    def set(self, slot, name=None, timing=None, values=None, merge=False, remove=None):
        """Edit a slot by hand. values = the slot's settings (REPLACES them unless
        merge=True); a value may be None (= ticked, type it later). remove = ids
        to untick."""
        if timing is not None and timing not in TIMINGS:
            return {"ok": False, "error": f"timing must be one of {TIMINGS}"}
        if values is not None:
            if not isinstance(values, dict):
                return {"ok": False, "error": "values must be {setting id: value}"}
            try:
                reg = self.R.by_id()
            except self._notready:
                return {"ok": False, "error": NOT_READY}
            bad = [i for i in values if i not in reg or not reg[i]["sceneable"]]
            if bad:
                return {"ok": False, "error": f"unknown or not sceneable: {bad[:5]}"}

        def edit(s):
            if name is not None:
                s["name"] = str(name)[:40]
            if timing is not None:
                s["timing"] = timing
            if values is not None:
                if not merge:
                    s["values"] = {}
                s["values"].update(values)
            for i in remove or ():
                s["values"].pop(i, None)
            return dict(s)
        out = self.store.update(slot, edit)
        self._changed()
        return {"ok": True, "slot": valid_slot(slot), "scene": out}

    def clear(self, slot):
        had = self.store.clear(slot)
        with self._lock:
            if self.pending and self.pending["slot"] == valid_slot(slot):
                old, self.pending = self.pending, None
                if old.get("fire_sent"):
                    self._fire_clear_queue()
        self._changed()
        return {"ok": True, "slot": valid_slot(slot), "had": had}

    def list(self):
        return {"ok": True, "slots": self.store.load()["slots"], "status": self.status()}

    # -- timing -------------------------------------------------------------------
    def fire_bars_active(self):
        """True while the Fire is the bar source (bar messages on, and coming)."""
        if self.fire is None or self._fire_bar_t is None:
            return False
        try:
            if not self.fire.bars_enabled():
                return False
        except Exception:
            return False
        win = (FIRE_BAR_FIRST_S if not self._fire_bar_len
               else max(FIRE_BAR_MIN_S, FIRE_BAR_GRACE * self._fire_bar_len))
        return self.clock() - self._fire_bar_t <= win

    def fire_stop(self):
        """A STOP came from the Fire: it is no longer the bar source."""
        with self._lock:
            self._fire_bar_t = None
            self._fire_bar_len = None

    def playing(self):
        if self.fire_bars_active():
            return True
        try:
            return bool(self.is_playing_fn())
        except Exception:
            return False

    def bar_source(self):
        if self.fire_bars_active():
            return "fire"
        return "orc" if self.playing() else "stopped"

    def _fire_song(self):
        """True / False / None (unknown) -- is the Fire in song mode?"""
        try:
            return self.fire.in_song_mode() if self.fire is not None else None
        except Exception:
            return None

    def recall(self, slot, timing=None, source=""):
        """Recall scene `slot` with its own timing, or `timing` to override it.
        -> {"ok", "when": "now" | "pending", ...}."""
        n = valid_slot(slot)
        if n is None:
            return {"ok": False, "error": f"scene slot must be 1-{N_SLOTS}"}
        if timing not in (None, "") and timing not in TIMINGS:
            return {"ok": False, "error": f"timing must be one of {TIMINGS}"}
        s = self.store.get(n)
        if not s or not s["values"]:
            return {"ok": False, "error": f"scene {n} is empty"}
        t = timing or s.get("timing") or DEFAULT_TIMING
        try:
            msgs, skipped = self.R.build_recall(s["values"], self.bindings() or {},
                                                self.recall_ctx() or {})
        except self._notready:
            return {"ok": False, "error": NOT_READY}
        except Exception as e:
            return {"ok": False, "error": f"cannot build scene {n}: {e}"}
        fire_msgs = [m for m in msgs if m["type"] == "action"
                     and m["cmd"].get("action") == FIRE_PATTERN_ACTION]
        rest = [m for m in msgs if m not in fire_msgs]
        skipped = list(skipped)
        fire_pat = fire_msgs[-1]["cmd"].get("pattern") if fire_msgs else None
        if fire_pat is not None and self._fire_song() is True:
            skipped.append(("fire.pattern", "Fire in song mode"))
            fire_pat = None
        p = {"slot": n, "name": s.get("name", ""), "timing": t, "source": source,
             "values": dict(s["values"]), "msgs": rest, "fire": fire_pat,
             "fire_sent": False, "skipped": skipped, "at": self.wall(),
             "beats": 1 if t == "NEXT_BEAT" else 0, "bars": _BARS.get(t, 0)}
        with self._lock:
            old = self.pending
            self.pending = None
            playing = self.playing()
            if t == "IMMEDIATE" or not playing:
                self._dispatch_locked(p, why="immediate" if t == "IMMEDIATE" else "stopped")
                self._after_replace_locked(old, sends_now=p["fire"] is not None)
                return {"ok": True, "slot": n, "when": "now", "timing": t,
                        "bar_source": self.bar_source()}
            self.pending = p
            p["src"] = self.bar_source()
            self._maybe_fire_early_locked(p)
            self._after_replace_locked(old, sends_now=p["fire_sent"])
        self._changed()
        return {"ok": True, "slot": n, "when": "pending", "timing": t,
                "bar_source": p["src"]}

    def orc_beat(self, downbeat):
        """A beat of the ORC clock (downbeat = beat 1 of the bar). Called from
        the clock thread: cheap, never sends itself."""
        p = self.pending
        if p is None:
            return
        with self._lock:
            p = self.pending
            if p is None:
                return
            if p["beats"]:
                self._dispatch_locked(p, why="beat")
                return
            if downbeat and p["bars"] and not self.fire_bars_active():
                self._count_bar_locked(p)

    def fire_bar(self, pattern, song_slot, bar):
        """The Fire's 0F: a bar starts on the Fire."""
        now = self.clock()
        with self._lock:
            if self._fire_bar_t is not None:
                dt = now - self._fire_bar_t
                if 0.2 < dt < 30.0:
                    self._fire_bar_len = dt
            self._fire_bar_t = now
            self.fire_last_bar = (pattern, song_slot, bar)
            p = self.pending
            if p is None:
                return
            p["src"] = "fire"
            if p["beats"]:
                self._dispatch_locked(p, why="beat")
            elif p["bars"]:
                self._count_bar_locked(p)

    def poll(self):
        """Called ~4x a second: a recall left pending when everything stopped
        lands now; when the Fire stops being the bar source its pattern / song
        state is forgotten (it can change on the Fire while stopped)."""
        with self._lock:
            active = self.fire_bars_active()
            if self._fire_was_active and not active and self.fire is not None:
                try:
                    self.fire.stopped()
                except Exception:
                    pass
            self._fire_was_active = active
            p = self.pending
            if p is None:
                return
            if not self.playing():
                self._dispatch_locked(p, why="stopped")
                return
            p["src"] = self.bar_source()

    # -- internals ------------------------------------------------------------------
    def _count_bar_locked(self, p):
        p["bars"] -= 1
        if p["bars"] <= 0:
            self._dispatch_locked(p, why="bar")
        else:
            self._maybe_fire_early_locked(p)

    def _maybe_fire_early_locked(self, p):
        """Fire as bar source: queue the Fire's pattern ONE BAR before the scene
        lands, so the Fire's own switch happens on the same bar."""
        if (p["fire"] is not None and not p["fire_sent"] and p["bars"] == 1
                and self.fire_bars_active()):
            p["fire_sent"] = True
            pat = p["fire"]
            self.spawn(lambda: self._send_fire(p, pat))

    def _after_replace_locked(self, old, sends_now):
        """The replaced recall had already queued its pattern on the Fire, and the
        new one does not queue its own right now: clear the Fire's queue."""
        if old and old.get("fire_sent") and not sends_now:
            self._fire_clear_queue()

    def _fire_clear_queue(self):
        """Re-send the pattern playing = clear the Fire's queue. Pattern mode ONLY
        (known): in song mode 0D n jumps the song to slot n."""
        if self._fire_song() is not False:
            return
        try:
            cur = self.fire.current_pattern()
        except Exception:
            cur = None
        if cur is not None:
            self.spawn(lambda: self._send_fire(None, cur))

    def _dispatch_locked(self, p, why):
        """Land recall p now (from any thread): hand the burst to the worker."""
        if self.pending is p:
            self.pending = None
        p["why"] = why
        self.spawn(lambda: self._send(p))

    def _send_fire(self, p, pattern):
        try:
            if self.fire is None:
                ok, why = False, "no Fire with our firmware linked (FLAME seq / ORC FW seq)"
            else:
                ok, why = self.fire.send_pattern(int(pattern))
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        if p is not None and not ok:
            p.setdefault("skipped", []).append(("fire.pattern", why))
        return ok

    def _unchanged_ids(self, p):
        """Ids whose current value is known and already equal to the scene's."""
        vals = p.get("values") or {}
        try:
            reg = self.R.by_id()
            ids = [i for i in vals if i in reg and i != "fire.pattern"]
            cur = self._current(ids, reg, only_cheap=True)
            return {i for i, v in cur.items() if v is not None and _same(v, vals[i])}
        except Exception as e:
            self.log(f"[scenes] unchanged check failed ({e}); sending everything")
            return set()

    def _send(self, p):
        """The burst (worker thread). Never raises."""
        with self._send_lock:
            sent, errors = 0, []
            same = self._unchanged_ids(p)
            todo = [m for m in p["msgs"] if m.get("id") not in same]
            with self.burst_guard():
                if p["fire"] is not None and not p["fire_sent"]:
                    p["fire_sent"] = True
                    if self._send_fire(p, p["fire"]):
                        sent += 1
                for k, m in enumerate(todo):
                    if k and k % self.chunk == 0:
                        self.sleep(self.pace_s)
                    try:
                        t = m["type"]
                        if t == "cc":
                            if self.send_synth_cc(m["channel"], m["cc"], m["value"]) is False:
                                raise RuntimeError("synth bus send failed")
                        elif t == "ctrl_cc":
                            self.call_ctrl_cc(m["cc"], m["value"])
                        elif t == "ctrl_pc":
                            self.call_ctrl_pc(m["program"])
                        elif t == "action":
                            if m.get("require") and not self.require_ok(m["require"]):
                                p["skipped"].append((m["id"], "FX slot now holds another plugin"))
                                continue
                            self.run_action(dict(m["cmd"]))
                        else:
                            p["skipped"].append((m.get("id"), f"unknown message type {t}"))
                            continue
                        sent += 1
                    except Exception as e:
                        errors.append((m.get("id"), f"{type(e).__name__}: {e}"))
            self.last = {"slot": p["slot"], "name": p.get("name", ""), "timing": p["timing"],
                         "why": p.get("why"), "source": p.get("source", ""), "sent": sent,
                         "failed": len(errors), "unchanged": sorted(same),
                         "skipped": [list(x) for x in p["skipped"]],
                         "errors": [list(x) for x in errors], "at": self.wall()}
            self.log(f"[scenes] scene {p['slot']} recalled ({p.get('why')}): {sent} sent, "
                     f"{len(same)} unchanged, {len(p['skipped'])} skipped, {len(errors)} failed")
        self._changed()

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def status(self):
        p = self.pending
        return {"pending": ({"slot": p["slot"], "timing": p["timing"], "bars_left": p["bars"],
                             "beats_left": p["beats"], "bar_source": p.get("src"),
                             "fire_sent": p["fire_sent"], "source": p.get("source", ""),
                             "at": p["at"]} if p else None),
                "last": self.last,
                "bar_source": self.bar_source(),
                "fire": (self.fire.status() if self.fire is not None and hasattr(self.fire, "status")
                         else None)}


class _NeverRaised(Exception):
    pass


def _rack_require_ok(req, path="~/.orc1_fx_rack.json"):
    """A RACK_PARAM recall only lands when the slot still holds the plugin the
    scene was captured with ({"rack_plugin": [idx, plugin]})."""
    rp = req.get("rack_plugin") if isinstance(req, dict) else None
    if not rp:
        return True
    try:
        with open(os.path.expanduser(path)) as f:
            slots = json.load(f).get("slots") or []
        return slots[int(rp[0])].get("plugin") == rp[1]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The Fire link: hello / version, bar messages on, 0F in, 0D out
# ---------------------------------------------------------------------------

class FireBarLink:
    """Talks to our Fire SEQUENCER firmware ("ORC FW seq") for scenes.

    - When the port appears (or comes back with a new USB address = the Fire
      rebooted, which also switches bar messages off) it sends a hello and
      waits for the version.
    - Version >= 2 (v0.30+): sends 0E 01 (bar messages on); every 0F then
      reaches on_bar(pattern, song_slot, bar). send_pattern(n) sends 0D n.
    - Version 1 (v0.29d and older): sends NOTHING else. No 0E, and
      send_pattern refuses, so scenes use the ORC clock and skip fire.pattern.
    - Pattern / song mode: from 0F (song slot 127 = no song) and the 0D reply's
      song flag; forgotten (None = unknown) when the Fire stops or goes away.
    - Crash records (2026-11-12, folded in from orc1-firevu so it no longer
      needs its own connection): once per connection, after the hello reply,
      it asks 06; a VALID record not seen before reaches on_crash(data) once
      (the brain saves the full report with host_crash.py --save).
    Pure apart from the injected mido module (a fake in the tests). The brain
    calls poll() about once a second and handle_sysex() for every SysEx in."""
    HELLO_RETRY_S = 3.0
    HELLO_SLOW_S = 30.0
    HELLO_FAST_TRIES = 5

    def __init__(self, mido_mod, idents=lambda: {}, on_bar=None, log=print,
                 clock=time.monotonic, ports=FIRE_SEQ_PORTS, on_crash=None):
        self.mido, self.idents, self.on_bar, self.log, self.clock = mido_mod, idents, on_bar, log, clock
        self.on_crash = on_crash
        self._crash_asked = False         # 06 sent on this connection
        self._crash_seen = None           # the last record handed to on_crash (kept across reconnects)
        self.ports = tuple(p.lower() for p in ports)
        self.port = self.ident = self.out = None
        self.version = None
        self.bars_on = False
        self.pattern = None               # pattern now playing (0F / 0D reply), None = unknown
        self.song = None                  # True / False / None = unknown
        self._hello_t = None
        self._tries = 0
        self._lock = threading.RLock()

    def is_ours(self, name):
        nl = (name or "").lower()
        return any(p in nl for p in self.ports)

    def _send(self, data):
        self.out.send(self.mido.Message("sysex", data=list(data)))

    def _close(self):
        if self.out is not None:
            try:
                self.out.close()
            except Exception:
                pass
        self.out = None
        self.port = self.ident = None
        self.version, self.bars_on = None, False
        self.pattern = self.song = None
        self._hello_t, self._tries = None, 0
        self._crash_asked = False

    def stopped(self):
        """The Fire stopped: its pattern / song mode may change by hand now."""
        with self._lock:
            self.pattern = self.song = None

    def poll(self):
        with self._lock:
            try:
                outs = [n for n in self.mido.get_output_names() if self.is_ours(n)]
            except Exception:
                return
            if not outs:
                if self.out is not None:
                    self.log(f"[scenes] Fire {self.port} gone: bars from the ORC clock")
                self._close()
                return
            port = outs[0]
            try:
                ident = (self.idents() or {}).get(port.split(":", 1)[0].strip())
            except Exception:
                ident = None
            if self.out is None or port != self.port or ident != self.ident:
                self._close()
                try:
                    self.out = self.mido.open_output(port)
                except Exception as e:
                    self.log(f"[scenes] cannot open {port} ({e}); will retry")
                    self.out = None
                    return
                self.port, self.ident = port, ident
            now = self.clock()
            if self.version is None:
                wait = self.HELLO_RETRY_S if self._tries < self.HELLO_FAST_TRIES else self.HELLO_SLOW_S
                if self._hello_t is None or now - self._hello_t >= wait:
                    try:
                        self._send(ORC_HDR + [CMD_HELLO])
                        self._hello_t, self._tries = now, self._tries + 1
                    except Exception as e:
                        self.log(f"[scenes] hello to {port} failed ({e})")
                        self._close()
                return
            if not self._crash_asked:
                try:
                    self._send(ORC_HDR + [CMD_CRASH])
                    self._crash_asked = True
                except Exception as e:
                    self.log(f"[scenes] crash-record request to {port} failed ({e})")
                    self._close()
                    return
            if self.version >= BARS_VERSION and not self.bars_on:
                try:
                    self._send(ORC_HDR + [CMD_BARS, 1])
                    self.bars_on = True
                    self.log(f"[scenes] Fire {port}: firmware version {self.version}, bar messages on")
                except Exception as e:
                    self.log(f"[scenes] cannot switch bar messages on ({e})")
                    self._close()

    def handle_sysex(self, data, source_name=""):
        """True = an ORC-protocol frame from our seq Fire (consumed: hello reply,
        0F bar, 0D reply ...). False = not ours."""
        data = list(data or ())
        if data[:4] != ORC_HDR or len(data) < 5 or not self.is_ours(source_name):
            return False
        cmd = data[4]
        bar = crash = None
        with self._lock:
            if cmd == CMD_HELLO and len(data) >= 6:
                v = data[5]
                if v != self.version:
                    self.version = v
                    if v < BARS_VERSION:
                        self.log(f"[scenes] Fire firmware version {v}: no bar messages, "
                                 "scenes follow the ORC clock")
            elif cmd == MSG_BAR and len(data) >= 8:
                self.pattern = data[5]
                self.song = data[6] != NO_SONG
                if self.version is None:
                    self.version = BARS_VERSION       # only v2+ sends 0F
                bar = (data[5], data[6], data[7])
            elif cmd == CMD_QUEUE_PAT and len(data) >= 9:
                # reply: <ok> <current pattern> <queued> <song 0/1>
                self.pattern = data[6]
                self.song = bool(data[8])
            elif cmd == CMD_CRASH and len(data) >= 7 and CRASH_REPLY_LENS.get(data[5]) == len(data):
                # one crash = (valid .. total, PC, uptime); NOT the whole reply: v3 also carries the TIM4
                # misread counter, which changes without a new crash
                key = tuple(data[6:16] + data[41:46])
                if data[6] and key != self._crash_seen:
                    self._crash_seen = key
                    crash = data
        if bar is not None and self.on_bar:
            self.on_bar(*bar)
        if crash is not None and self.on_crash:
            try:
                self.on_crash(crash)
            except Exception as e:
                self.log(f"[scenes] could not hand on the Fire's crash record ({e})")
        return True

    def bars_enabled(self):
        return bool(self.out is not None and self.bars_on)

    def current_pattern(self):
        return self.pattern

    def in_song_mode(self):
        return self.song

    def send_pattern(self, n):
        """-> (ok, why). 0D n: the Fire queues pattern n (song mode: slot n)."""
        with self._lock:
            if self.out is None:
                return False, "no Fire with our firmware connected (FLAME seq / ORC FW seq)"
            if self.version is None:
                return False, "the Fire has not answered hello yet"
            if self.version < BARS_VERSION:
                return False, (f"Fire firmware version {self.version} has no queue-pattern "
                               "command (needs v0.30, hello version 2)")
            n = int(n)
            if not 0 <= n <= 31:
                return False, f"pattern must be 0-31, got {n}"
            try:
                self._send(ORC_HDR + [CMD_QUEUE_PAT, n])
                return True, ""
            except Exception as e:
                return False, f"send failed: {e}"

    def host_send(self, data):
        """Send one ORC-protocol SysEx (data = ORC_HDR + cmd + ..., no F0/F7) to the
        seq Fire, sharing this link's port and lock (orc1_fire_pages uses it for the
        UNIVERSAL pages). Raises when no Fire is connected or the send fails."""
        with self._lock:
            if self.out is None:
                raise RuntimeError("no Fire with our firmware connected (FLAME seq / ORC FW seq)")
            self._send(data)

    def link_version(self):
        """-> (connected, hello version or None)."""
        with self._lock:
            return self.out is not None, (self.version if self.out is not None else None)

    def status(self):
        return {"port": self.port, "version": self.version, "bars": self.bars_enabled(),
                "pattern": self.pattern, "song": self.song}
