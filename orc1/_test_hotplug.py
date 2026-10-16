#!/usr/bin/env python3
"""_test_hotplug.py -- controller USB REPLUG paths (Fire + Mixtrack).

2026-09-29. The three replug fixes of 2026-09-28 were proven on hardware only:
  1. the brain notices a QUICK replug (same MIDI port name) by the USB address
     changing in /proc/asound/cardN/usbbus, and reopens the controller;
  2. FireHandler.rewake() opens fresh LED + OLED outputs and repaints pads +
     screen now AND again at +0.6/+1.8/+4.0 s (the Fire ignores paint while
     its firmware boots);
  3. fire_oled.reset_output() drops the OLED port cached by NAME (a replugged
     Fire reuses the name, so the cache kept sending into the dead port).

Layer 1 extracts _usb_card_idents / _hotplug_scan_once / _hotplug_controller
from orc1.py by SOURCE (orc1.py can't be imported: it runs as __main__ and
opens hardware) and runs them against a fake /proc/asound + fake mido port
list. Layers 2-3 import orc1_fire / fire_oled and drive the real functions
with stub ports. Touches no hardware, no real file, no JACK. Safe while the rig
runs.

DJ MODE (2026-09-29): while /run/orc1-dj-active exists Mixxx owns the
Mixtrack -- _mixtrack_sync_release closes the ORC's LED output and the input
thread drops every message; removing the marker reopens + wakes it. Tested
with a temp marker path against the extracted functions.

Mutation-prove (each turns checks red):
  - _hotplug_scan_once: `replugged = set()`      -> the quick-replug checks
  - _hotplug_controller: drop the fire.rewake()  -> rewake-on-replug checks
  - rewake(): drop fire_oled.reset_output()      -> the OLED reset check
  - rewake(): late-repaint delays `()`           -> the late-repaint checks
  - reset_output(): drop _OUT_CACHE.clear()      -> the reopen check
  - handler thread: drop `and not _mixtrack_released` -> the ignore check
  - _hotplug_controller: drop the `elif _mixtrack_released` -> replug-in-DJ
  - skipback_rings: sort by id only -> the RAM-first check (Mixtrack FX1-3
    must offer the rings a sampler grabs cleanly from before CARD rings)
  - _hotplug_scan_once / open_ports: drop the `if _is_patchbay(name)` branch
    -> the PATCHBAY checks (the CME U6MIDI Pro would be opened as a usb-engine
    input; with pisound DIN OUT -> CME In 1 cabled, _pass loops MIDI forever)

PATCHBAY (2026-09-30): ports matching cfg PATCHBAY_PORT_PREFIXES are never
opened as engine inputs, at startup or on hot-plug, and are logged once.

REVIEW FIXES (2026-10-01), each mutation-proven:
  - a Mixtrack met mid DJ set (brain start, or first plug-in) is built with
    led_output=False and never paints over Mixxx -> the "mid DJ set" checks
    (mutation: drop the sync before MixtrackHandler / pass led_output=True)
  - wake() clears the FX-LED change cache -> FX1 relights after reclaim
  - ONE scan at a time (_hotplug_scan + _hotplug_scan_lock): MIDI_RESCAN beside
    the 3 s loop opened a controller twice (mutation: drop the acquire)
  - a same-name replug CLOSES the old port, and the old handler's cleanup
    leaves the NEW port's name alone (_untrack_input compares the port)
"""
import ast
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
EXPECTED = 82
from orc1_kikpad import is_surface_port as _real_is_surface_port   # stdlib-only module
from orc1_config import is_fire_port as _real_is_fire_port         # the real name rule
_res = []


def check(name, ok, detail=""):
    _res.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


# ── layer 1: the brain's scan, extracted from orc1.py ───────────────────────

BRAIN_FUNCS = ("_usb_card_idents", "_hotplug_scan_once", "_hotplug_controller",
               "_mixtrack_sync_release", "make_mixtrack_handler_thread",
               "_is_patchbay", "open_ports", "_cme_scan", "_kikpad_open",
               "_track_input", "_untrack_input", "_drop_input", "_hotplug_scan")
# the real per-port handler threads (layer 1b only: the scan tests stub them)
HANDLER_FUNCS = ("make_usb_handler", "make_pedal_handler_thread")

FIRE_CARD, MT_CARD = "FL STUDIO FIRE", "Mixtrack Pro 3"
FIRE_PORT = "FL STUDIO FIRE:FL STUDIO FIRE MIDI 1 24:0"
MT_PORT = "Mixtrack Pro 3:Mixtrack Pro 3 MIDI 1 28:0"
CME_PORTS = [f"U6MIDI Pro:U6MIDI Pro Port {i + 1} 48:{i}" for i in range(3)]
KEYS_PORT = "Keystation 49:Keystation 49 MIDI 1 32:0"


class FakeProc:
    """A fake /proc/asound: cards listed in order, USB ones carry an address."""

    def __init__(self):
        self.cards = []            # [(num, id, long name, usbbus or None)]

    def set(self, *cards):
        self.cards = list(cards)

    def open(self, path, *a, **k):
        import io
        if path == "/proc/asound/cards":
            lines = []
            for num, cid, name, _bus in self.cards:
                lines.append(f"{num:2d} [{cid:<15}]: USB-Audio - {name}\n")
                lines.append(f"                      {name} at usb-xhci, high speed\n")
            return io.StringIO("".join(lines))
        for num, _cid, _name, bus in self.cards:
            if path == f"/proc/asound/card{num}/usbbus" and bus:
                return io.StringIO(bus + "\n")
        raise FileNotFoundError(path)


class FakePort:
    """An opened mido input: iterates its messages, records close()."""

    def __init__(self, name, msgs=()):
        self.name = name
        self.msgs = list(msgs)
        self.closed = False

    def __iter__(self):
        return iter(self.msgs)

    def close(self):
        self.closed = True


class BlockingPort(FakePort):
    """A port whose stream stays open until close() -- like a real device, so
    a reader thread doesn't see an instant 'disconnect'."""

    def __init__(self, name):
        super().__init__(name)
        self._gone = threading.Event()

    def __iter__(self):
        self._gone.wait(5)
        return iter(())

    def close(self):
        self.closed = True
        self._gone.set()


class FakeSurface:
    """Stands in for orc1_kikpad.KikpadSurface (the real one is covered by
    _test_kikpad_surface.py); records the brain's lifecycle calls."""
    made = []

    def __init__(self, engine):
        self.attached, self.detached, self.handled = [], [], []
        FakeSurface.made.append(self)

    def attach(self, name):
        self.attached.append(name)
        return True                          # the real attach returns whether the port opened

    def detach(self, name=None):
        self.detached.append(name)

    def handle(self, msg):
        self.handled.append(msg)

    def send_sysex(self, data):
        pass

    sysex_hook = None


class FakeMapLink:
    """Stands in for orc1_kikpad_map.KikpadMapLink (covered by _test_kikpad_map.py):
    records what the brain tells it about the pad coming and going."""
    made = []

    def __init__(self, send):
        self.send, self.events = send, []
        FakeMapLink.made.append(self)

    def on_sysex(self, data):
        pass

    def connected(self, on):
        self.events.append(bool(on))


class FakeMido:
    def __init__(self):
        self.inputs = []
        self.opened = []
        self.ports = []            # every FakePort handed out, in order
        self.open_delay = 0.0      # > 0: widen the open race (scan-lock test)
        self.blocking = set()      # names whose ports stay open (reader threads)

    def get_input_names(self):
        return list(self.inputs)

    def open_input(self, name):
        if self.open_delay:
            time.sleep(self.open_delay)
        self.opened.append(name)
        # a port = an (empty) message stream
        port = BlockingPort(name) if name in self.blocking else FakePort(name)
        self.ports.append(port)
        return port

    def get_output_names(self):
        return []


class LedPort:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Router:
    def __init__(self):
        self.gone, self.came = [], []

    def device_disconnected(self, n):
        self.gone.append(n)

    def device_connected(self, n):
        self.came.append(n)


class Ctl:
    """Stands in for the live FireHandler / MixtrackHandler objects."""

    def __init__(self):
        self.rewakes = 0
        self.wakes = 0
        self.outs = 0
        self.handled = 0
        self._out = None
        self._out_lock = threading.Lock()

    def handle_msg(self, msg):
        self.handled += 1

    def rewake(self):
        self.rewakes += 1

    def _open_output(self, prefix):
        self.outs += 1
        self._out = LedPort()

    def wake(self):
        self.wakes += 1


def brain_ns(real_handlers=False):
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = BRAIN_FUNCS + (HANDLER_FUNCS if real_handlers else ())
    segs = {n.name: ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in funcs}
    missing = [f for f in funcs if f not in segs]
    if missing:
        raise SystemExit(f"orc1.py lost {missing} -- update this suite")
    proc, md, router = FakeProc(), FakeMido(), Router()
    fire, mt = Ctl(), Ctl()
    built = []
    # the code passes sys.modules[__name__] to the handler constructors
    sys.modules.setdefault("orc1_extract", type(sys)("orc1_extract"))

    def mixtrack_handler(_eng, led_output=True):
        # mirrors MixtrackHandler.__init__: LED output + wake only if asked
        c = Ctl()
        c.led_output = led_output
        if led_output:
            c._open_output("mixtrack")
            c.wake()
        built.append(c)
        return c
    ns = {
        "__name__": "orc1_extract", "sys": sys, "threading": threading,
        "open": proc.open, "mido": md, "router": router,
        "cfg": type("cfg", (), {"is_fire_port": staticmethod(_real_is_fire_port),
                                "MIXTRACK_PORT_PREFIX": "mixtrack",
                                "PORTS": {"pisound": "pisound"}}),
        "_usb_idents": {}, "_opened_input_names": set(), "os": os,
        "_opened_input_ports": {}, "_hotplug_scan_lock": threading.Lock(),
        "built": built, "_route_message": lambda *a: None, "pedal": None,
        "_DJ_MARK": os.path.join(tempfile.mkdtemp(prefix="hp_dj_"), "orc1-dj-active"),
        "_mixtrack_released": False,
        "_hotplug_lock": threading.Lock(),
        "_FIRE_AVAILABLE": True, "_MIXTRACK_AVAILABLE": True,
        "fire": fire, "mixtrack": mt,
        "FireHandler": lambda *_: Ctl(), "MixtrackHandler": mixtrack_handler,
        "make_fire_handler_thread": lambda port: (lambda: None),
        "make_mixtrack_handler_thread": lambda port: (lambda: None),
        "make_usb_handler": lambda port: (lambda: None),
        "print_status": lambda *a, **k: None,
        "_orc1_pedal": type("p", (), {"recognised": staticmethod(lambda *a: False)}),
        "_PEDAL_PREFIXES": (),
        "_PATCHBAY_PREFIXES": ("u6midi",), "_patchbay_noted": set(), "cme": None, "_cme_present": set(),
        "_orc1_kikpad": type("k", (), {
            "is_surface_port": staticmethod(_real_is_surface_port),
            "KikpadSurface": FakeSurface}),
        "kikpad": None,
        "_orc1_kikpad_map": type("km", (), {"KikpadMapLink": FakeMapLink}),
        "kikpad_map": None,
        "_pedal_ports": [], "port_outs": {}, "find_name": lambda *a: None,
        "state": {"din_routing": "engine"},
        "log": [],
    }
    ns["print"] = lambda *a, **k: ns["log"].append(" ".join(str(x) for x in a))
    exec(compile("\n\n".join(segs[f] for f in funcs), "orc1.py", "exec"), ns)
    return ns, proc, md, router, fire, mt


def test_kikpad_surface():
    # The KIKPAD's port :1 is its host surface (bank 2): claimed by NAME,
    # never an engine input; port :0 (the music banks) stays a usb engine.
    ns, proc, md, router, fire, mt = brain_ns()
    usb = []
    ns["make_usb_handler"] = lambda port: (usb.append(port.name) or (lambda: None))
    scan = ns["_hotplug_scan_once"]
    p0, p1 = "KIKPAD:KIKPAD IN 32:0", "KIKPAD:KIKPAD IN 32:1"
    md.blocking = {p1}
    proc.set((4, "KIKPAD", "KIKPAD", "001/020"))
    md.inputs = [p0, p1]
    FakeSurface.made.clear()
    FakeMapLink.made.clear()
    scan()
    k = ns["kikpad"]
    check("KIKPAD :1 goes to the surface (attached for painting), not the engine",
          k is not None and k.attached == [p1] and p1 not in usb, f"usb={usb}")
    km = ns["kikpad_map"]
    check("... the map editor is built once, sends through the surface, hears its SysEx, and is told the pad is there",
          km is not None and km.send == k.send_sysex and k.sysex_hook == km.on_sysex and km.events[:1] == [True])
    check("... and KIKPAD :0 stays a usb engine input", usb == [p0])
    check("... both tracked as open", {p0, p1} <= ns["_opened_input_names"])
    md.inputs = [p0]
    scan()
    check("surface port unplugged: paint output detached", p1 in k.detached)
    check("... and the map editor is told the pad is gone", km.events[-1] is False)
    md.inputs = [p0, p1]
    scan()
    check("plugged back: the SAME surface re-attached (fresh output), one object",
          k.attached == [p1, p1] and len(FakeSurface.made) == 1)
    check("... the SAME map editor, told again (it re-reads the pad's maps on attach)",
          len(FakeMapLink.made) == 1 and km.events.count(True) == 2)
    for port in md.ports:
        port.close()                         # let the reader threads finish


def test_dj_first_sight():
    # The brain met the Mixtrack only while DJ mode was already on: it never
    # synced the marker yet (_mixtrack_released False), and no handler exists.
    ns, proc, md, router, fire, _ = brain_ns()
    ns["mixtrack"] = None
    open(ns["_DJ_MARK"], "w").close()
    proc.set((2, "Pro3", MT_CARD, "001/008"))
    md.inputs = [MT_PORT]
    ns["_hotplug_scan_once"]()
    m = ns["mixtrack"]
    check("Mixtrack first seen mid DJ set: built WITHOUT an LED output, never woken",
          m is not None and m.led_output is False and m.outs == 0 and m.wakes == 0
          and m._out is None, f"led_output={getattr(m, 'led_output', '?')} wakes="
          f"{getattr(m, 'wakes', '?')}")
    check("... and the marker is applied at once (released before the 3 s loop)",
          ns["_mixtrack_released"] is True)
    os.remove(ns["_DJ_MARK"])
    ns["_mixtrack_sync_release"]()
    check("... reclaim then opens the LEDs + wakes it once",
          m._out is not None and m.wakes == 1 and m.outs == 1)
    # and with no DJ mode it is built the ordinary way
    ns2, proc2, md2, *_ = brain_ns()
    ns2["mixtrack"] = None
    md2.inputs = [MT_PORT]
    ns2["_hotplug_scan_once"]()
    m2 = ns2["mixtrack"]
    check("no DJ mode: a first-seen Mixtrack opens its LEDs + wakes as before",
          m2.led_output is True and m2.wakes == 1)
    os.rmdir(os.path.dirname(ns["_DJ_MARK"]))


def test_dj_brain_start():
    # main() runs at brain start; it can't be exec'd (opens hardware), so check
    # its order: the marker is read BEFORE the handler, which gets led_output.
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    main = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = sorted((c.lineno, c) for c in ast.walk(main) if isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Name)
                   and c.func.id in ("MixtrackHandler", "_mixtrack_sync_release"))
    order = [c.func.id for _, c in calls]
    ctor = [c for _, c in calls if c.func.id == "MixtrackHandler"]
    kw = {k.arg: ast.unparse(k.value) for k in ctor[0].keywords} if ctor else {}
    check("brain start: _mixtrack_sync_release() runs BEFORE MixtrackHandler(...)",
          order[:2] == ["_mixtrack_sync_release", "MixtrackHandler"], str(order))
    check("... and the handler is built with led_output=not _mixtrack_released",
          kw.get("led_output") == "not _mixtrack_released", str(kw))


def _real_mixtrack(eng):
    """The REAL MixtrackHandler with its hardware + poller threads stubbed
    (same SAFETY as _test_mixtrack: no real MIDI port, no endless threads)."""
    import orc1_mixtrack as MT

    class Out:
        def __init__(self):
            self.sent = []

        def send(self, m):
            self.sent.append(m)

        def close(self):
            pass

    class H(MT.MixtrackHandler):
        def _open_output(self, prefix):      # never the real controller
            self.opens = getattr(self, "opens", 0) + 1
            self._out = Out()

        def _fx_led_loop(self):
            return

        def _led_tick_loop(self):
            return
    return H, MT


def test_mixtrack_led_output():
    import orc1_config
    eng = type("E", (), {"cfg": orc1_config,
                         "skipback_rings": staticmethod(lambda: [{"id": 2, "name": "SMP"}]),
                         "sampler_deck_source": staticmethod(lambda d: 2)})()
    H, MT = _real_mixtrack(eng)
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        quiet = H(eng, led_output=False)
    check("real MixtrackHandler(led_output=False): no LED port opened, nothing sent",
          getattr(quiet, "opens", 0) == 0 and quiet._out is None)
    with contextlib.redirect_stdout(io.StringIO()):
        h = H(eng)
        h._render_ring_leds()                       # the poller lights FX1
        fx1 = [m for m in h._out.sent if m.note == MT.NOTE_FX1 and m.velocity]
        h._out.sent.clear()
        h.wake()                                    # reclaim: sweep, then ALL off
        fx1_last = [m.velocity for m in h._out.sent
                    if m.note == MT.NOTE_FX1 and m.channel == MT.CH_DECK1 - 1][-1:]
        h._out.sent.clear()
        h._render_ring_leds()                       # the poller's next tick
    relit = [m for m in h._out.sent if m.note == MT.NOTE_FX1 and m.velocity
             and m.channel == MT.CH_DECK1 - 1]
    check("default build opens the LED port once (as before)", h.opens == 1 and fx1)
    check("wake() leaves FX1 dark (its last FX1 message is an off)", fx1_last == [0],
          str(fx1_last))
    check("after wake() blanks the LEDs, the ring poller RELIGHTS FX1 (cache cleared)",
          len(relit) == 1, f"FX1 note-ons after wake: {len(relit)}")


def test_scan_lock():
    ns, proc, md, router, fire, mt = brain_ns()
    md.inputs = [FIRE_PORT]
    lock = ns["_hotplug_scan_lock"]
    lock.acquire()
    skipped = ns["_hotplug_scan"](False)
    lock.release()
    check("MIDI_RESCAN while a scan runs: skipped (returns False, opens nothing)",
          skipped is False and md.opened == [])
    ns, proc, md, router, fire, mt = brain_ns()     # fresh: independent of the above
    md.inputs = [FIRE_PORT]
    md.open_delay = 0.3                 # a slow open = the real race window
    ts = [threading.Thread(target=ns["_hotplug_scan"]),
          threading.Thread(target=ns["_hotplug_scan"], args=(False,)),
          threading.Thread(target=ns["_hotplug_scan"])]
    for t in ts:
        t.start()
        time.sleep(0.05)
    for t in ts:
        t.join(5)
    check("three overlapping scans open a new Fire exactly ONCE",
          md.opened == [FIRE_PORT] and fire.rewakes == 1,
          f"opened={md.opened} rewakes={fire.rewakes}")
    # nothing may bypass the wrapper: every use of _hotplug_scan_once in
    # orc1.py sits inside _hotplug_scan
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    outside = []
    for fn in ast.parse(src).body:
        if isinstance(fn, ast.FunctionDef) and fn.name == "_hotplug_scan":
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id == "_hotplug_scan_once":
                outside.append(getattr(fn, "name", "?"))
    check("orc1.py reaches _hotplug_scan_once ONLY through the locked _hotplug_scan",
          outside == [], str(outside))
    check("MIDI_RESCAN uses the non-blocking wrapper",
          "target=_hotplug_scan, args=(False,)" in src)


def test_replug_ports():
    ns, proc, md, router, fire, mt = brain_ns()
    scan = ns["_hotplug_scan_once"]
    proc.set((3, "KS49", "Keystation 49", "001/010"))
    md.inputs = [KEYS_PORT]
    scan()
    old = md.ports[-1]
    check("an opened input's PORT is remembered under its name",
          ns["_opened_input_ports"].get(KEYS_PORT) is old)
    proc.set((3, "KS49", "Keystation 49", "001/011"))
    scan()
    new = md.ports[-1]
    check("same-name replug CLOSES the old port (seq client freed)",
          old.closed and new is not old and not new.closed)
    check("... and the new port is the one tracked, opened once",
          ns["_opened_input_ports"].get(KEYS_PORT) is new
          and md.opened.count(KEYS_PORT) == 2)
    md.inputs = []
    scan()
    check("unplugged: the port is closed and forgotten too",
          new.closed and KEYS_PORT not in ns["_opened_input_ports"]
          and KEYS_PORT not in ns["_opened_input_names"])

    # the OLD handler thread's cleanup after a replug (real handler code)
    for kind in ("make_usb_handler", "make_pedal_handler_thread"):
        ns2, _, md2, router2, *_ = brain_ns(real_handlers=True)
        a = FakePort(KEYS_PORT)
        ns2["_track_input"](KEYS_PORT, a)
        ns2["_drop_input"](KEYS_PORT)               # the replug branch
        b = FakePort(KEYS_PORT)
        ns2["_track_input"](KEYS_PORT, b)           # reopened, same name
        ns2[kind](a)()                              # old thread exits now
        check(f"{kind}: the OLD port's cleanup leaves the NEW port tracked",
              KEYS_PORT in ns2["_opened_input_names"]
              and ns2["_opened_input_ports"].get(KEYS_PORT) is b
              and router2.gone == [], f"router told={router2.gone}")
        ns2[kind](b)()                              # the new one ends (unplug)
        check(f"{kind}: its OWN port's cleanup still forgets it",
              KEYS_PORT not in ns2["_opened_input_names"]
              and KEYS_PORT not in ns2["_opened_input_ports"])


def test_dj_handover():
    ns, proc, md, router, fire, mt = brain_ns()
    mark = ns["_DJ_MARK"]
    sync = ns["_mixtrack_sync_release"]
    mt._open_output("mixtrack")
    led = mt._out
    check("no marker: nothing changes (ORC keeps the Mixtrack)",
          sync() is False and mt._out is led and not led.closed)
    open(mark, "w").close()
    sync()
    check("marker made: the ORC's LED output is closed and dropped (Mixxx paints)",
          ns["_mixtrack_released"] is True and mt._out is None and led.closed)
    ns["make_mixtrack_handler_thread"](["a", "b", "c"])()
    check("while released, every Mixtrack message is ignored by the ORC",
          mt.handled == 0, f"handled={mt.handled}")
    proc.set((2, "Pro3", MT_CARD, "001/008"))
    md.inputs = [MT_PORT]
    ns["_hotplug_scan_once"]()
    proc.set((2, "Pro3", MT_CARD, "001/009"))
    ns["_hotplug_scan_once"]()
    check("a replug mid DJ set does not re-wake the ORC's LEDs",
          mt.wakes == 0 and mt._out is None, f"wakes={mt.wakes}")
    sync()
    check("a second check with the marker still there is a no-op",
          mt._out is None and mt.outs == 1)
    os.remove(mark)
    sync()
    check("marker removed: LED output reopened + the Mixtrack woken",
          ns["_mixtrack_released"] is False and mt._out is not None and mt.wakes == 1)
    ns["make_mixtrack_handler_thread"](["a", "b"])()
    check("... and its messages drive the ORC again", mt.handled == 2)
    os.rmdir(os.path.dirname(mark))


def test_ring_order():
    import json
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "skipback_rings")
    d = tempfile.mkdtemp(prefix="hp_rings_")
    path = os.path.join(d, "tracks.json")
    fake_os = type("o", (), {"path": type("p", (), {
        "expanduser": staticmethod(lambda _p: path)})})
    ns = {"os": fake_os, "json": json}
    exec(compile(ast.get_source_segment(src, node), "orc1.py", "exec"), ns)
    tracks = [{"id": 0, "name": "MAIN", "store": "card"},
              {"id": 1, "name": "T1", "store": "card"},
              {"id": 2, "name": "SMP MAIN", "store": "ram"},
              {"id": 3, "name": "SMP IN3"},                     # no store = RAM
              {"id": 4, "name": "OFF", "armed": False}]
    with open(path, "w") as f:
        json.dump({"tracks": tracks}, f)
    got = ns["skipback_rings"]()
    check("Mixtrack FX order: RAM rings first (by id), then CARD, disarmed left out",
          [r["id"] for r in got] == [2, 3, 0, 1], str([r["id"] for r in got]))
    os.remove(path)
    check("unreadable tracks file -> [] (never raises into a controller)",
          ns["skipback_rings"]() == [])
    os.rmdir(d)


def test_brain():
    ns, proc, md, router, fire, mt = brain_ns()
    scan = ns["_hotplug_scan_once"]
    PI = (0, "pisound", "pisound", None)           # not USB: no usbbus file

    # boot: the Fire is already there but not yet opened by the scanner
    proc.set(PI, (1, "FIRE", FIRE_CARD, "001/005"))
    md.inputs = [FIRE_PORT]
    scan()
    check("a new Fire port is opened and the live handler re-woken",
          md.opened == [FIRE_PORT] and fire.rewakes == 1
          and FIRE_PORT in ns["_opened_input_names"], f"{md.opened} rewakes={fire.rewakes}")
    check("the USB address of each USB card is remembered; pisound (no usbbus) is not",
          ns["_usb_idents"] == {FIRE_CARD: "001/005"}, str(ns["_usb_idents"]))

    scan()
    check("steady state: a second scan reopens nothing, disconnects nothing",
          md.opened == [FIRE_PORT] and fire.rewakes == 1 and not router.gone)

    # QUICK replug: the port name never disappears, only the address changes
    proc.set(PI, (1, "FIRE", FIRE_CARD, "001/006"))
    scan()
    check("QUICK replug (same name, new USB address) is detected: router told it left",
          router.gone == [FIRE_PORT], str(router.gone))
    check("... and the Fire is reopened + re-woken in the same pass",
          md.opened == [FIRE_PORT, FIRE_PORT] and fire.rewakes == 2,
          f"{md.opened} rewakes={fire.rewakes}")
    check("... and stays tracked as open", FIRE_PORT in ns["_opened_input_names"])

    # SLOW replug: gone for a scan, then back
    proc.set(PI)
    md.inputs = []
    scan()
    check("SLOW replug, unplugged: port dropped + router told, nothing reopened",
          router.gone == [FIRE_PORT, FIRE_PORT] and FIRE_PORT not in ns["_opened_input_names"]
          and fire.rewakes == 2)
    check("... a card that VANISHED is not mistaken for a replug",
          ns["_usb_idents"] == {} and len(router.gone) == 2)
    proc.set(PI, (1, "FIRE", FIRE_CARD, "001/007"))
    md.inputs = [FIRE_PORT]
    scan()
    check("SLOW replug, plugged back: reopened + re-woken once (no double from the address)",
          md.opened.count(FIRE_PORT) == 3 and fire.rewakes == 3 and len(router.gone) == 2,
          f"opened={md.opened.count(FIRE_PORT)} rewakes={fire.rewakes} gone={len(router.gone)}")

    # the Mixtrack takes its own path, and ANOTHER card changing leaves the Fire alone
    proc.set(PI, (1, "FIRE", FIRE_CARD, "001/007"), (2, "Pro3", MT_CARD, "001/008"))
    md.inputs = [FIRE_PORT, MT_PORT]
    scan()
    check("Mixtrack arrives: its output reopened + LED wake, the Fire untouched",
          mt.outs == 1 and mt.wakes == 1 and fire.rewakes == 3, f"mt outs={mt.outs} wakes={mt.wakes}")
    proc.set(PI, (1, "FIRE", FIRE_CARD, "001/007"), (2, "Pro3", MT_CARD, "001/009"))
    scan()
    check("Mixtrack QUICK replug: re-woken; the Fire (address unchanged) is not",
          mt.wakes == 2 and fire.rewakes == 3 and router.gone[-1] == MT_PORT,
          f"mt wakes={mt.wakes} fire={fire.rewakes} gone={router.gone}")

    # a card that appears for the first time is not a "replug"
    ns2, proc2, md2, router2, fire2, _ = brain_ns()
    proc2.set((1, "FIRE", FIRE_CARD, "001/005"))
    ns2["_hotplug_scan_once"]()
    check("first sighting of a USB card never counts as a replug",
          router2.gone == [] and md2.opened == [])


def _noted(ns):
    return [l for l in ns["log"] if "patchbay" in l and "not opened" in l]


def test_patchbay():
    ns, proc, md, router, fire, mt = brain_ns()
    scan = ns["_hotplug_scan_once"]
    md.inputs = CME_PORTS + [KEYS_PORT]
    scan()
    check("PATCHBAY hot-plug: none of the CME's 3 ports is opened as an engine input",
          not any(p in md.opened for p in CME_PORTS)
          and not any(p in ns["_opened_input_names"] for p in CME_PORTS), str(md.opened))
    check("... while an ordinary USB keyboard arriving alongside IS opened",
          md.opened == [KEYS_PORT] and router.came == [KEYS_PORT], str(md.opened))
    check("... each CME port logged once as patchbay", len(_noted(ns)) == 3, str(_noted(ns)))
    scan()
    scan()
    check("steady state: later scans still never open it, and don't re-log it",
          md.opened == [KEYS_PORT] and len(_noted(ns)) == 3)
    md.inputs = [KEYS_PORT]
    scan()
    md.inputs = CME_PORTS + [KEYS_PORT]
    scan()
    check("unplug + replug: still not opened, logged again (one line per port)",
          md.opened == [KEYS_PORT] and len(_noted(ns)) == 6, str(md.opened))

    # the CME link re-reads the box when its config port comes and goes
    class FakeCme:
        def __init__(self):
            self.jobs = []
        def matches(self, n):
            return "u6midi" in n.lower()
        def submit(self, label, fn):
            self.jobs.append(label)
        def refresh(self):
            pass
        def close(self):
            pass
    ns4, proc4, md4, *_ = brain_ns()
    ns4["cme"] = fc = FakeCme()
    proc4.set((4, "Pro", "U6MIDI Pro", "001/020"))
    md4.inputs = CME_PORTS + [KEYS_PORT]
    ns4["_hotplug_scan_once"]()
    ns4["_hotplug_scan_once"]()
    check("CME plugged: ONE re-read queued (Port 1 only, not per scan, not per port)",
          fc.jobs == ["connect"], str(fc.jobs))
    proc4.set((4, "Pro", "U6MIDI Pro", "001/021"))
    ns4["_hotplug_scan_once"]()
    check("CME same-name replug (new USB address): one re-read queued, still not opened",
          fc.jobs == ["connect", "replug"]
          and not any(p in md4.opened for p in CME_PORTS), str(fc.jobs))
    md4.inputs = [KEYS_PORT]
    ns4["_hotplug_scan_once"]()
    check("CME unplugged: the link is told (publishes connected:false)",
          fc.jobs == ["connect", "replug", "unplugged"], str(fc.jobs))
    # the link's lifecycle no longer depends on the patchbay prefix
    ns5, _, md5, *_ = brain_ns()
    ns5["cme"] = fc5 = FakeCme()
    ns5["_PATCHBAY_PREFIXES"] = ()
    md5.inputs = list(CME_PORTS)
    ns5["_hotplug_scan_once"]()
    check("prefix removed from cfg: the CME link still sees the plug (connect queued)",
          fc5.jobs == ["connect"], str(fc5.jobs))

    # startup: open_ports() at brain boot
    ns2, _, md2, *_ = brain_ns()
    md2.inputs = CME_PORTS + [KEYS_PORT, "Midi Through:Midi Through Port-0 14:0"]
    din, usb_ins, mtp, fp = ns2["open_ports"]()
    check("PATCHBAY at startup: the CME's ports are not opened at all",
          not any(p in md2.opened for p in CME_PORTS)
          and not any(p in ns2["_opened_input_names"] for p in CME_PORTS), str(md2.opened))
    check("... the keyboard still becomes the one usb-engine input",
          md2.opened == [KEYS_PORT] and len(usb_ins) == 1, str(md2.opened))
    check("... and the boot log says why", len(_noted(ns2)) == 3, str(_noted(ns2)))

    # the choice is cfg's: empty the prefixes and the CME opens like any device
    ns3, _, md3, *_ = brain_ns()
    ns3["_PATCHBAY_PREFIXES"] = ()
    md3.inputs = list(CME_PORTS)
    ns3["_hotplug_scan_once"]()
    check("prefix removed from cfg -> the CME opens as an ordinary input again",
          sorted(md3.opened) == sorted(CME_PORTS), str(md3.opened))


def test_patchbay_config_path():
    # test via the path the code loads: orc1.py derives the tuple from cfg
    import orc1_config
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    check("cfg PATCHBAY_PORT_PREFIXES names the CME, and orc1.py reads that key",
          "u6midi" in [p.lower() for p in orc1_config.PATCHBAY_PORT_PREFIXES]
          and 'getattr(cfg, "PATCHBAY_PORT_PREFIXES"' in src)


# ── layer 2: FireHandler.rewake (real method, stub self) ────────────────────

class StubOled:
    def __init__(self):
        self.resets = 0
        self.forgets = 0

    def reset_output(self):
        self.resets += 1

    def forget_sent(self):
        self.forgets += 1


class FastStop:
    """Records the late-repaint delays instead of sleeping through them."""

    def __init__(self, stop_after=None):
        self.delays = []
        self.stop_after = stop_after

    def wait(self, d):
        self.delays.append(d)
        return self.stop_after is not None and len(self.delays) > self.stop_after


class OldPort:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_rewake():
    import orc1_fire
    real_oled = orc1_fire.fire_oled

    def fake_self(stop):
        s = type("F", (), {})()
        s.fcfg = {"PORT_PREFIX": "FIRE"}
        s._out = OldPort()
        s.old = s._out
        s.opens, s.renders, s.oleds = [], 0, 0
        s._stop = stop

        def _open_output():
            s.opens.append("fire")
            s._out = object()

        def _render():
            s.renders += 1

        def _oled():
            s.oleds += 1
        s._open_output, s._render, s._oled = _open_output, _render, _oled
        return s

    def run(stop):
        oled = StubOled()
        orc1_fire.fire_oled = oled
        s = fake_self(stop)
        try:
            orc1_fire.FireHandler.rewake(s)
            for t in threading.enumerate():
                if t.name == "fire-rewake":
                    t.join(5)
        finally:
            orc1_fire.fire_oled = real_oled
        return s, oled

    s, oled = run(FastStop())
    check("rewake closes the dead LED port and opens a fresh one",
          s.old.closed and s.opens == ["fire"] and not isinstance(s._out, OldPort))
    check("rewake drops the OLED's cached port (fire_oled.reset_output)", oled.resets == 1,
          f"resets={oled.resets}")
    check("rewake repaints pads + screen now and 3 more times (4 each)",
          s.renders == 4 and s.oleds == 4, f"renders={s.renders} oleds={s.oleds}")
    check("late repaints land at +0.6 / +1.8 / +4.0 s (booting firmware ignores earlier)",
          [round(sum(s._stop.delays[:i + 1]), 1) for i in range(len(s._stop.delays))] == [0.6, 1.8, 4.0],
          str(s._stop.delays))
    check("each late repaint forgets the OLED dedupe (else the frame is skipped)",
          oled.forgets == 3, f"forgets={oled.forgets}")

    s2, _ = run(FastStop(stop_after=0))
    check("a handler shutting down stops the late repaints",
          s2.renders == 1 and s2.oleds == 1, f"renders={s2.renders}")


# ── layer 3: fire_oled's per-name port cache ────────────────────────────────

def test_fire_names():
    # 2026-10-13: the Fire is claimed by NAME through ONE rule
    # (orc1_config.is_fire_port). Akai's firmware and our firmware's HOST mode
    # get the Fire handler; our sequencer firmware and anything else that just
    # happens to say "fire" stay plain usb engine inputs.
    host = "ORC FW host:ORC FW host MIDI 1 24:0"
    seq = "ORC FW seq:ORC FW seq MIDI 1 24:0"
    hello = "ORC FIRE hello:ORC FIRE hello MIDI 1 24:0"
    check("name rule: Akai's firmware and our host mode are a Fire",
          _real_is_fire_port(FIRE_PORT) and _real_is_fire_port(host))
    check("name rule: our sequencer firmware and a stray 'FIRE' name are NOT",
          not _real_is_fire_port(seq) and not _real_is_fire_port(hello)
          and not _real_is_fire_port(""))

    ns, proc, md, router, fire, mt = brain_ns()
    usb = []
    ns["make_usb_handler"] = lambda port: (usb.append(port.name) or (lambda: None))
    md.inputs = [host, seq, hello]
    ns["_hotplug_scan_once"]()
    check("hot-plug: host mode goes to the Fire handler (re-woken), not the engine",
          fire.rewakes == 1 and host not in usb, f"rewakes={fire.rewakes} usb={usb}")
    check("hot-plug: the sequencer firmware + a stray FIRE name are usb engine inputs",
          sorted(usb) == sorted([seq, hello]), f"usb={usb}")
    for port in md.ports:
        port.close()

    ns, proc, md, router, fire, mt = brain_ns()
    md.inputs = [seq, host]
    din, usb_ins, mtp, fp = ns["open_ports"]()
    check("boot (open_ports): host mode is the Fire port, the sequencer an engine input",
          fp is not None and fp.name == host and [p.name for p in usb_ins] == [seq],
          f"fire={getattr(fp, 'name', None)} usb={[p.name for p in usb_ins]}")
    for port in md.ports:
        port.close()

    import orc1_fire
    import orc1_config

    class FakeMido:
        def __init__(self, names):
            self.names, self.opened = names, []

        def get_output_names(self):
            return list(self.names)

        def open_output(self, n):
            self.opened.append(n)
            return n
    real = orc1_fire.mido
    try:
        fm = FakeMido([seq, hello, host])
        orc1_fire.mido = fm
        s = type("F", (), {})()
        s.cfg, s._out = orc1_config, None
        orc1_fire.FireHandler._open_output(s)
        check("Fire handler LED/pad output: opens host mode, skips seq + stray names",
              fm.opened == [host] and s._out == host, f"opened={fm.opened}")
    finally:
        orc1_fire.mido = real

    import fire_oled
    real = fire_oled.mido.get_output_names
    try:
        fire_oled.mido.get_output_names = lambda: [seq, hello, host]
        a = fire_oled.find_fire_output()
        fire_oled.mido.get_output_names = lambda: [seq, hello]
        b = fire_oled.find_fire_output()
        check("OLED output: host mode found; with only seq + stray names, none",
              a == host and b is None, f"{a!r} {b!r}")
    finally:
        fire_oled.mido.get_output_names = real


def test_oled_cache():
    import fire_oled

    class Port:
        def __init__(self):
            self.sent, self.closed = 0, False

        def send(self, m):
            self.sent += 1

        def close(self):
            self.closed = True

    class Img:
        def __init__(self, lit):
            self.lit = lit

        def load(self):
            lit = self.lit
            return type("PX", (), {"__getitem__": lambda _s, xy: 1 if xy == lit else 0})()

    opened = []

    def open_output(name):
        p = Port()
        opened.append(p)
        return p
    real = fire_oled.mido.open_output
    saved = (dict(fire_oled._OUT_CACHE), dict(fire_oled._LAST), dict(fire_oled._LAST_T))
    fire_oled._OUT_CACHE.clear(); fire_oled._LAST.clear(); fire_oled._LAST_T.clear()
    fire_oled.mido.open_output = open_output
    try:
        name = "FL STUDIO FIRE:test 0:0"
        fire_oled.send_image(Img((0, 0)), port=name)
        fire_oled.send_image(Img((1, 0)), port=name)
        check("same name: the cached OLED port is reused (one open, two frames)",
              len(opened) == 1 and opened[0].sent == 2)
        fire_oled.reset_output()
        fire_oled.send_image(Img((1, 0)), port=name)
        check("after reset_output: the old port is closed and a NEW one opened for the same name",
              opened[0].closed and len(opened) == 2, f"opens={len(opened)}")
        check("... and the frame is really sent (dedupe memory cleared too)",
              opened[1].sent == 1, f"sent={opened[1].sent}")
        fire_oled.send_image(Img((1, 0)), port=name)
        fire_oled.forget_sent()
        fire_oled.send_image(Img((1, 0)), port=name)
        check("forget_sent: an identical frame goes out again, on the SAME port",
              len(opened) == 2 and opened[1].sent == 2, f"opens={len(opened)} sent={opened[1].sent}")
    finally:
        fire_oled.mido.open_output = real
        fire_oled._OUT_CACHE.clear(); fire_oled._LAST.clear(); fire_oled._LAST_T.clear()
        fire_oled._OUT_CACHE.update(saved[0]); fire_oled._LAST.update(saved[1])
        fire_oled._LAST_T.update(saved[2])


def main():
    print("controller replug suite")
    t0 = time.monotonic()
    test_brain()
    test_dj_first_sight()
    test_dj_brain_start()
    test_mixtrack_led_output()
    test_scan_lock()
    test_replug_ports()
    test_patchbay()
    test_patchbay_config_path()
    test_kikpad_surface()
    test_ring_order()
    test_dj_handover()
    test_rewake()
    test_fire_names()
    test_oled_cache()
    passed = sum(_res)
    print(f"\n{passed} passed, {len(_res) - passed} failed  ({time.monotonic() - t0:.1f}s)")
    if len(_res) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_res)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if passed == len(_res) else 1


if __name__ == "__main__":
    sys.exit(main())
