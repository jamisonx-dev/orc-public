"""orc1_bsplights -- the ORC drives the BeatStep Pro's 16 step lights (service orc1-bsplights).

Needs the BSP to run our "stepled" firmware (beatstep-pro-handler/firmware/): on stock
firmware the BSP repaints its steps at once and these frames only flash.

    python3 orc1_bsplights.py          (run by systemd; Ctrl-C / SIGTERM hands every step back)

SHOWS (config ~/.orc1_bsplights.json {"show": ..., "track": ...}, set from /beatstep):
  off      every step handed back to the BSP (stock look)
  manual   this service sends nothing: the /beatstep STEP LIGHTS palette owns the lights
  track    one ORC seq track (by name): its steps in the track colour, the playhead white;
           muted = only the playhead; longer than 16 = the 16-step page under the playhead
  clock    bar position: beats 2-4 blue, beat 1 cyan, the current step white; dark when stopped
  modules  step N = the Nth bound /matrix module: flashes in its colour on each note it plays
  tracks   step N = ORC seq track N: white on a hit at the current step, its colour if it has
           notes, off if muted or empty

Reads the brain's play-state feed (/dev/shm/orc1_playstate.json) and keeps its OWN step clock
phase-locked to it (lock_grid / next_due, same as mxvbox/pi/conductor.py: stepping from each
30 Hz feed update skipped steps). Sends only the steps whose colour changed, one 12-byte SysEx
each; repaints everything when the BSP (re)appears. Never touches the brain, audio or skipback.
Status for the web: /dev/shm/orc1_bsplights_status.json. Import-safe: nothing runs until main().
"""
import json, os, signal, sys, time

FEED = "/dev/shm/orc1_playstate.json"
CONF = os.path.expanduser("~/.orc1_bsplights.json")
STATUS = "/dev/shm/orc1_bsplights_status.json"
PORT_MATCH = ("Arturia BeatStep Pro", "Arturia Be")     # the BSP's first (USB) port

SHOWS = ("off", "manual", "track", "clock", "modules", "tracks")
DEFAULT = {"show": "manual", "track": ""}
COL = {"off": 0x00, "red": 0x01, "green": 0x04, "blue": 0x10,
       "yellow": 0x05, "cyan": 0x14, "magenta": 0x11, "white": 0x15}
RELEASE = 0x40                    # stepled firmware: hand the step back to the BSP
PALETTE = ("red", "yellow", "green", "cyan", "magenta", "blue")   # per track / module, by index
STALE_S = 3.0                     # the brain rewrites the feed at least once a second
FLASH_S = 0.12                    # how long a module flash lasts
PORT_CHECK_S = 2.0
REPAINT_S = 10.0


def colour_of(i):
    return COL[PALETTE[i % len(PALETTE)]]


def frame_bytes(step, value):
    """F0 00 20 6B 7F 42 02 00 10 <30+step> <value> F7  (step 0..15)."""
    return [0xF0, 0x00, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, 0x30 + step, value & 0x7F, 0xF7]


# ------------------------------------------------------------------ step clock (from conductor.py)
def lock_grid(origin, step_s, o, s):
    """The step grid (origin = wall time of step 0) after one feed update that puts it at o.
    Start, a tempo change or a jump > 40 ms re-locks; small drift is followed 1/10 per update."""
    if origin is None or step_s != s or abs(o - origin) > 0.040:
        return o, s
    return origin + (o - origin) * 0.1, step_s


def next_due(origin, step_s, sent, now):
    """(the next step not yet shown, the wall time it is due). Never goes back (sent + 1);
    after a stall jumps to the current step instead of bursting the backlog."""
    k = max(sent + 1, int((now - origin) / step_s))
    return k, origin + k * step_s


def is_running(d, now):
    tr = (d or {}).get("transport", {})
    if not tr.get("running") or tr.get("step", -1) < 0:
        return False
    return now - float((d or {}).get("t") or 0) <= STALE_S


def is_fresh(d, now):
    return d is not None and now - float(d.get("t") or 0) <= STALE_S


# ------------------------------------------------------------------ the shows: pure functions
def show_track(d, k, cfg):
    """16 colours for one seq track; k = current global step or None when stopped."""
    out = [COL["off"]] * 16
    tracks = (d or {}).get("tracks", [])
    want = (cfg.get("track") or "").lower()
    idx = next((i for i, t in enumerate(tracks) if t.get("name", "").lower() == want), 0 if tracks else None)
    if idx is None:
        return out
    t = tracks[idx]
    s = t.get("steps", "") or ""
    n = len(s) or int(t.get("len") or 16)
    pos = (k % n) if k is not None else None
    page = (pos // 16) if pos is not None else 0
    if not t.get("mute"):
        c = colour_of(idx)
        for j in range(16):
            i = page * 16 + j
            if i < len(s) and s[i] == "1":
                out[j] = c
    if pos is not None:
        out[pos % 16] = COL["white"]
    return out


def show_clock(d, k, cfg):
    out = [COL["off"]] * 16
    if k is None:
        return out
    for b in (4, 8, 12):
        out[b] = COL["blue"]
    out[0] = COL["cyan"]
    out[k % 16] = COL["white"]
    return out


def show_tracks(d, k, cfg):
    out = [COL["off"]] * 16
    for i, t in enumerate((d or {}).get("tracks", [])[:16]):
        s = t.get("steps", "") or ""
        if t.get("mute") or "1" not in s:
            continue
        out[i] = colour_of(i)
        if k is not None and s[k % len(s)] == "1":
            out[i] = COL["white"]
    return out


class ModuleFlash:
    """Step N = the Nth bound module (feed binding order): its colour for FLASH_S after its hit
    counter rises. A counter that goes DOWN (brain restart) just re-baselines."""

    def __init__(self):
        self.last = {}
        self.until = {}

    def frame(self, d, now):
        out = [COL["off"]] * 16
        mods = (d or {}).get("mods", {}) or {}
        for i, mod in enumerate(list(((d or {}).get("bindings", {}) or {}).keys())[:16]):
            h = int((mods.get(mod) or {}).get("hits", 0))
            prev = self.last.get(mod)
            if prev is not None and h > prev:
                self.until[mod] = now + FLASH_S
            self.last[mod] = h
            if now < self.until.get(mod, 0):
                out[i] = colour_of(i)
        return out


# ------------------------------------------------------------------ the sender
class Lights:
    """Holds what the BSP shows; send(frame) writes only the changed steps through `out(bytes)`."""

    def __init__(self, out):
        self.out = out
        self.shown = [None] * 16          # None = unknown (after (re)connect) -> repaint
        self.sent = 0

    def forget(self):
        self.shown = [None] * 16

    def send(self, frame):
        for i, v in enumerate(frame):
            if self.shown[i] != v:
                self.out(frame_bytes(i, v))
                self.shown[i] = v
                self.sent += 1

    def release_all(self):
        self.send([RELEASE] * 16)


def load_conf(path=CONF):
    try:
        with open(path) as f:
            c = json.load(f)
        if not isinstance(c, dict) or c.get("show") not in SHOWS:
            return dict(DEFAULT)
        return {**DEFAULT, **c}
    except Exception:
        return dict(DEFAULT)


def save_conf(conf, path=CONF):
    """Atomic write (the web calls this through its endpoint)."""
    c = {**DEFAULT, **{k: v for k, v in conf.items() if k in DEFAULT}}
    if c["show"] not in SHOWS:
        raise ValueError("show must be one of %s" % (SHOWS,))
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, path)
    return c


class Engine:
    """One pass = one decision; main() just loops it. Pure enough to test with a fake clock."""

    def __init__(self, lights):
        self.lights = lights
        self.flash = ModuleFlash()
        self.origin = self.step_s = None
        self.k = -1
        self.handed_back = False

    def tick(self, d, conf, now):
        """Returns how long the loop may sleep."""
        show = conf.get("show", "manual")
        if show in ("off", "manual"):
            if not self.handed_back:
                self.lights.release_all()
                self.handed_back = True
            self.origin, self.k = None, -1
            return 0.05
        self.handed_back = False
        if not is_fresh(d, now):
            self.origin, self.k = None, -1
            self.lights.send([COL["off"]] * 16)
            return 0.05
        wait = 0.02
        k = None
        if is_running(d, now):
            tr = d["transport"]
            s = float(tr["step_ms"]) / 1000.0
            step_wall = float(d.get("t") or 0) - float(tr.get("age_ms") or 0) / 1000.0
            self.origin, self.step_s = lock_grid(self.origin, self.step_s, step_wall - tr["step"] * s, s)
            nk, due = next_due(self.origin, self.step_s, self.k, now)
            if now >= due:
                self.k = nk
            else:
                wait = min(wait, due - now)
            k = self.k if self.k >= 0 else None
        else:
            self.origin, self.k = None, -1
        if show == "track":
            frame = show_track(d, k, conf)
        elif show == "clock":
            frame = show_clock(d, k, conf)
        elif show == "tracks":
            frame = show_tracks(d, k, conf)
        else:
            frame = self.flash.frame(d, now)
            wait = min(wait, 0.01)
        self.lights.send(frame)
        return max(0.001, wait)


# ------------------------------------------------------------------ runtime
_fc = {"mt": None, "d": None}


def read_feed(path=FEED):
    try:
        mt = os.stat(path).st_mtime_ns
        if mt != _fc["mt"]:
            with open(path) as f:
                _fc["d"] = json.load(f)
            _fc["mt"] = mt
    except Exception:
        _fc["d"] = None
    return _fc["d"]


class BspPort:
    """ONE ALSA client for the whole run (named orc1-bsplights); get_ports() re-lists each check,
    so a BSP replug is seen without creating a new MIDI client every PORT_CHECK_S."""

    def __init__(self):
        import rtmidi
        self.client = rtmidi.MidiOut(name="orc1-bsplights")
        self.mo = None            # self.client once a port is open
        self.name = None

    def check(self):
        """Returns 'new' when the BSP just (re)appeared, else whether it is present."""
        ports = self.client.get_ports()
        idx = next((i for i, p in enumerate(ports) if all(m in p for m in PORT_MATCH)), None)
        if idx is not None and self.mo is not None and ports[idx] != self.name:
            self.close()          # same device name, new client:port number = it was replugged
        if idx is not None and self.mo is None:
            self.client.open_port(idx, "steplights")
            self.mo, self.name = self.client, ports[idx]
            return "new"
        if idx is None and self.mo is not None:
            self.close()
        return idx is not None

    def write(self, data):
        if self.mo is not None:
            self.mo.send_message(data)

    def close(self):
        try:
            if self.mo is not None:
                self.client.close_port()
        except Exception:
            pass
        self.mo, self.name = None, None


def write_status(st, path=STATUS):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError:
        pass


def main():
    port = BspPort()
    lights = Lights(lambda b: port.write(b))
    eng = Engine(lights)
    stop = {"now": False}

    def _bye(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    conf, conf_mt, next_check, next_status, next_repaint = load_conf(), None, 0.0, 0.0, 0.0
    print("bsplights: show=%s; waiting for the BSP" % conf["show"], flush=True)
    while not stop["now"]:
        now = time.time()
        if now >= next_check:
            next_check = now + PORT_CHECK_S
            try:
                mt = os.stat(CONF).st_mtime_ns
            except OSError:
                mt = None
            if mt != conf_mt:
                conf_mt, conf = mt, load_conf()
                print("bsplights: show=%s track=%r" % (conf["show"], conf["track"]), flush=True)
            if port.check() == "new":
                lights.forget()
                eng.handed_back = False
                print("bsplights: BSP found (%s), repainting" % port.name, flush=True)
        if now >= next_repaint:
            # backstop: the BSP can lose its lights without us seeing it (a restart faster than a
            # check). A full repaint is 16 x 12 bytes; driven shows only (off/manual stay quiet).
            next_repaint = now + REPAINT_S
            if conf["show"] not in ("off", "manual"):
                lights.forget()
        try:
            wait = eng.tick(read_feed(), conf, now) if port.mo is not None else 0.2
        except Exception as e:                         # a send to a vanished port
            print("bsplights: send failed (%s); waiting for the BSP" % e, flush=True)
            port.close(); lights.forget(); wait = 0.5
        if now >= next_status:
            next_status = now + 1.0
            write_status({"t": now, "show": conf["show"], "track": conf["track"],
                          "bsp": port.mo is not None, "sent": lights.sent})
        time.sleep(wait)
    if port.mo is not None:
        lights.forget()
        lights.release_all()
        time.sleep(0.05)
    write_status({"t": time.time(), "show": conf["show"], "track": conf["track"], "bsp": False,
                  "sent": lights.sent, "stopped": True})
    print("bsplights: stopped, steps handed back", flush=True)


if __name__ == "__main__":
    main()
