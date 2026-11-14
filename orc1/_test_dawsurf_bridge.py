#!/usr/bin/env python3
"""
The "daw" module bridge (orc1_dawsurf_bridge): Fire fader CCs <-> a DAW as a Mackie Control
surface, the binding / port following, and the brain's skip of the DAW port. Since 2026-11-09 also
the BINDING table (orc1_dawsurf_bind: shipped defaults = the old behaviour, a user file from a
non-default CC), VIEWS shared / independent per surface with REAPER on OSC (the view CCs, list end,
bank pads, the BSP's own window, exact 0 dB, pages past the bank size) and the UDP OscLink (fake
socket: no real socket is ever opened).

    python3 _test_dawsurf_bridge.py [-v]        (PC: py _test_dawsurf_bridge.py)

Pure: fake ports and a fake clock, no MIDI port opened, no service touched. HOME and
USERPROFILE point at a temp dir BEFORE the bridge is imported (Windows expanduser reads
USERPROFILE), every file it writes is in that dir, and the real bindings / status files are
checked byte-identical at the end.
"""
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 271


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def md5(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


# ---- sandbox BEFORE importing anything that resolves "~" -----------------------------
# (the live status file in /dev/shm is NOT md5-compared: the running orc1-dawsurf rewrites it
# every second, so that net failed on the Pi whenever the service ran. Every write_status call
# the suite makes is recorded instead and must land in the sandbox.)
REAL = [os.path.expanduser("~/.orc1_bindings.json"), os.path.expanduser("~/.orc1_dawsurf_bindings.json")]
REAL_MD5 = {p: md5(p) for p in REAL}
SANDBOX = tempfile.mkdtemp(prefix="dawsurf_test_")
os.environ["HOME"] = SANDBOX
os.environ["USERPROFILE"] = SANDBOX
sys.path.insert(0, HERE)
import orc1_config as cfg                       # noqa: E402
cfg.DAWSURF_DIALECT = "mcu"     # the suite tests the Mackie path by default (OSC cases pass dialect="osc"); the Pi may run OSC
cfg.DAWSURF_REAPER_WEB = None
import orc1_mcu as M                            # noqa: E402
import orc1_dawsurf_bridge as B                 # noqa: E402

STATUS_WRITES = []
_real_write_status = B.write_status


def _recorded_write_status(st, path=B.STATUS):
    STATUS_WRITES.append(path)
    if os.path.abspath(path).startswith(os.path.abspath(SANDBOX)):
        _real_write_status(st, path)


B.write_status = _recorded_write_status

CH = 5                                          # the daw module's channel in these tests (1-based)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(ch=CH, clock=None):
    out = {"daw": [], "fire": []}
    br = B.Bridge(lambda m: out["daw"].append(m), lambda raw: out["fire"].append(list(raw)),
                  clock=clock or Clock())
    br.set_channel(ch)
    br.daw_connected(True)
    br.fire_connected(True)
    out["daw"].clear()
    out["fire"].clear()
    return br, out


def cc(ch, num, val):
    return [0xB0 | (ch - 1), num, val]


def pb(strip, v14):
    return B.to_bytes(M.fader(strip, v14))


def pitches(msgs):
    return [(m["channel"], m["pitch"] + 8192) for m in msgs if m["type"] == "pitchwheel"]


def notes(msgs):
    return [(m["note"], m["velocity"]) for m in msgs if m["type"] == "note_on"]


try:
    # ---- 1. raw <-> dict codec --------------------------------------------------------
    for d in (M.fader(3, 12345), M.fader(8, 0), M.button("mute", True, strip=2),
              {"type": "control_change", "channel": 0, "control": 16, "value": 65},
              {"type": "aftertouch", "channel": 0, "value": 0x2C}, M.surface_hello()):
        back = B.from_bytes(B.to_bytes(d))
        check("codec round trip: %s" % d["type"], back == {k: (tuple(v) if k == "data" else v)
                                                         for k, v in d.items()}, (d, back))
    check("codec: hello = F0 00 00 66 14 01 'XYZ'... F7",
          B.to_bytes(M.surface_hello())[:9] == [0xF0, 0, 0, 0x66, 0x14, 0x01, 0x58, 0x59, 0x5A]
          and B.to_bytes(M.surface_hello())[-1] == 0xF7)
    check("codec: pitch bend LSB first", B.to_bytes(M.fader(0, 16383)) == [0xE0, 0x7F, 0x7F]
          and B.to_bytes(M.fader(2, 0)) == [0xE2, 0, 0])

    # ---- 2. Fire -> DAW ------------------------------------------------------------------
    clk = Clock()
    br, out = make(clock=clk)
    br.on_bus(cc(CH, 20, 64))
    check("Fire CC 20 -> touch strip 1 first", notes(out["daw"])[:1] == [(0x68, 127)],
          out["daw"])
    check("Fire CC 20 -> pitch bend strip 1 = 64/127", pitches(out["daw"]) == [(0, round(64 / 127 * 16383))],
          out["daw"])
    check("Fire move is not bounced back to the Fire", out["fire"] == [], out["fire"])
    out["daw"].clear()
    br.on_bus(cc(CH, 20, 70))
    check("a second move: no second touch, one bend", notes(out["daw"]) == [] and
          pitches(out["daw"]) == [(0, round(70 / 127 * 16383))], out["daw"])
    out["daw"].clear()
    br.on_bus(cc(CH, 28, 127))
    check("CC 28 -> the MASTER fader (pitch bend ch 9) at full", (8, 16383) in pitches(out["daw"]), out["daw"])
    check("CC 28 -> master touch note 0x70", (0x70, 127) in notes(out["daw"]), out["daw"])
    out["daw"].clear()
    for m in (cc(CH + 1, 21, 50), cc(CH, 31, 50), [0x90 | (CH - 1), 60, 100], cc(CH, 20, 70)):
        br.on_bus(m)
    check("other channel / unmapped CC / a note / a repeat value: nothing to the DAW", out["daw"] == [],
          out["daw"])
    check("strips 1-8 = CC 20-27 (the cfg table)", [cfg.DAWSURF_FADER_CC[c] for c in range(20, 28)]
          == list(range(8)) and cfg.DAWSURF_FADER_CC[28] == "master")

    # touch release
    out["daw"].clear()
    clk.t += cfg.DAWSURF_TOUCH_RELEASE_S - 0.05
    br.tick()
    check("touch held until the release time", out["daw"] == [], out["daw"])
    clk.t += 0.1
    br.tick()
    check("touch let go after DAWSURF_TOUCH_RELEASE_S (strip 1 + master)",
          sorted(notes(out["daw"])) == [(0x68, 0), (0x70, 0)], out["daw"])
    check("model shows no strip touched afterwards",
          not br.surf.strip(0).touched and not br.surf.strip("master").touched)

    # ---- 3. DAW -> Fire ------------------------------------------------------------------
    br, out = make()
    br.on_daw(pb(2, 8192))
    check("REAPER strip 3 bend -> CC 22 to the Fire on the daw channel",
          out["fire"] == [cc(CH, 22, B.cc7(8192 / 16383))], out["fire"])
    check("REAPER bend is not sent back to REAPER", pitches(out["daw"]) == [], out["daw"])
    out["fire"].clear()
    br.on_daw(pb(2, 8192 + 20))
    check("a 14-bit wobble inside one CC step: no Fire send", out["fire"] == [], out["fire"])
    br.on_daw(pb(8, 0))
    check("REAPER master bend -> CC 28", out["fire"] == [cc(CH, 28, 0)], out["fire"])
    out["fire"].clear()
    out["daw"].clear()
    # the Fire echoes back what it was sent
    br.on_bus(cc(CH, 22, B.cc7(8192 / 16383)))
    check("Fire echo of a value we sent it: nothing to the DAW", out["daw"] == [], out["daw"])
    # REAPER echoes the Fire's own move
    br.on_bus(cc(CH, 23, 100))
    out["fire"].clear()
    br.on_daw(pb(3, round(100 / 127 * 16383)))
    check("REAPER echo of the Fire's move: nothing to the Fire", out["fire"] == [], out["fire"])
    br.on_daw(pb(3, 0))
    check("while the Fire fader is held, REAPER can't yank it", out["fire"] == [], out["fire"])

    # ---- 4. no endless bounce: both sides echo everything --------------------------------
    clk = Clock()
    q = []
    out = {"daw": [], "fire": []}

    def to_daw(m):
        out["daw"].append(m)
        if m["type"] == "pitchwheel":
            q.append(("daw", B.to_bytes(m)))           # a REAPER that echoes every fader

    def to_fire(raw):
        out["fire"].append(list(raw))
        q.append(("bus", list(raw)))                  # a Fire that echoes every CC onto the bus

    br = B.Bridge(to_daw, to_fire, clock=clk)
    br.set_channel(CH)
    br.daw_connected(True)
    br.fire_connected(True)
    q.append(("bus", cc(CH, 24, 90)))
    q.append(("daw", pb(5, 4000)))
    steps = 0
    while q and steps < 1000:
        src, m = q.pop(0)
        (br.on_bus if src == "bus" else br.on_daw)(m)
        steps += 1
    check("echoing Fire + echoing REAPER settle (no endless bounce)", not q and steps < 10, steps)
    check("... and each side got the other's move once",
          pitches(out["daw"]) == [(4, round(90 / 127 * 16383))] and
          out["fire"].count(cc(CH, 25, B.cc7(4000 / 16383))) == 1, (out["daw"], out["fire"]))

    # ---- 5. bank ---------------------------------------------------------------------
    clk = Clock()
    br, out = make(clock=clk)
    br.on_bus(cc(CH, 21, 10))                         # strip 2 touched
    out["daw"].clear()
    br.on_bus(cc(CH, 30, 127))
    check("CC 30 rising -> bank right press + release",
          (0x2F, 127) in notes(out["daw"]) and (0x2F, 0) in notes(out["daw"]), out["daw"])
    check("a bank lets the held touch go first", notes(out["daw"])[0] == (0x69, 0), out["daw"])
    out["daw"].clear()
    br.on_bus(cc(CH, 30, 100))
    check("held high: no second bank", out["daw"] == [], out["daw"])
    br.on_bus(cc(CH, 30, 0))
    br.on_bus(cc(CH, 30, 127))
    check("down then up again: a second bank", notes(out["daw"]).count((0x2F, 127)) == 1, out["daw"])
    out["daw"].clear()
    br.on_bus(cc(CH, 29, 64))
    check("CC 29 -> bank left", notes(out["daw"]) == [(0x2E, 127), (0x2E, 0)], out["daw"])
    out["fire"].clear()
    br.on_daw(pb(1, 16383))                           # the new bank's strip 2 arrives
    check("after a bank the new strip's fader reaches the Fire", out["fire"] == [cc(CH, 21, 127)],
          out["fire"])

    # ---- 5b. the DAW hello is re-sent until the DAW's state arrives (2026-11-06, measured live:
    #          a hello sent before the rtpMIDI session reconnected was lost; the page stayed blank)
    clk = Clock()
    br, out = make(clock=clk)
    br.on_bus(cc(CH, 20, 50))                         # a Fire move fills the model: must NOT count
    out["daw"].clear()
    br.daw_connected(True)
    def dhellos():
        return sum(1 for m in out["daw"] if m["type"] == "sysex")
    clk.t += 1.0; br.tick()
    check("DAW hello: one at connect, none 1 s later", dhellos() == 1, dhellos())
    clk.t += 2.5; br.tick()
    check("DAW hello: no state from the DAW after 3 s -> sent again (a Fire move doesn't count)",
          dhellos() == 2, dhellos())
    br.on_daw(pb(0, 9000))                            # REAPER's reset: the strips arrive
    for _ in range(5):
        clk.t += 40; br.tick()
    check("DAW hello: once a fader came FROM the DAW, no more hellos", dhellos() == 2, dhellos())
    br.daw_connected(False); br.daw_connected(True)
    clk.t += 3.5; br.tick()
    check("DAW hello: a reconnect forgets the old state and retries again", dhellos() == 4, dhellos())

    # ---- 6. DAW / Fire up and down, rebind -------------------------------------------
    br, out = make()
    br.daw_connected(False)
    br.on_bus(cc(CH, 20, 33))
    check("DAW down: a Fire move sends nothing (no error)", out["daw"] == [], out["daw"])
    check("DAW down: the model still keeps the move", B.cc7(br.surf.strip(0).fader) == 33)
    br.daw_connected(True)
    check("DAW up: the hello goes out", len(out["daw"]) == 1 and out["daw"][0]["type"] == "sysex"
          and tuple(out["daw"][0]["data"][:8]) == (0, 0, 0x66, 0x14, 1, 0x58, 0x59, 0x5A), out["daw"])
    br.on_daw(pb(4, 16383))
    out["fire"].clear()
    br.fire_connected(False)
    br.on_daw(pb(4, 0))
    check("Fire gone: nothing sent to it", out["fire"] == [], out["fire"])
    br.fire_connected(True)
    check("Fire back: its faders repainted from the model (incl. a move made while it was away)",
          cc(CH, 24, 0) in out["fire"] and cc(CH, 20, 33) in out["fire"], out["fire"])
    out["fire"].clear()
    br.set_channel(9)
    check("rebind: the faders are re-sent on the NEW channel",
          cc(9, 24, 0) in out["fire"] and all(m[0] == 0xB8 for m in out["fire"]), out["fire"])
    out["daw"].clear()
    br.on_bus(cc(CH, 20, 99))
    check("rebind: the old channel is ignored", out["daw"] == [], out["daw"])
    br.on_bus(cc(9, 20, 99))
    check("rebind: the new channel drives the DAW", pitches(out["daw"]) == [(0, round(99 / 127 * 16383))],
          out["daw"])
    out["daw"].clear()
    out["fire"].clear()
    br.set_channel(None)
    check("unbind lets a held touch go", (0x68, 0) in notes(out["daw"]), out["daw"])
    out["daw"].clear()
    br.on_bus(cc(9, 21, 5))
    br.on_daw(pb(5, 9000))
    check("unbound: nothing to the DAW from the bus, nothing to the Fire",
          out["daw"] == [] and out["fire"] == [], (out["daw"], out["fire"]))

    # ---- 7. labels + status ------------------------------------------------------------------
    br, out = make()
    br.on_daw(B.to_bytes(M.strip_labels(["Kick", "Bass"])))
    st = br.status()
    check("REAPER's LCD names land in the status", st["strips"][0]["name"] == "Kick"
          and st["strips"][1]["name"] == "Bass", st["strips"][:2])
    check("status is plain JSON", isinstance(json.loads(json.dumps(st)), dict))
    check("status names the channel + map", st["channel"] == CH and st["fader_cc"]["28"] == "master")
    p = os.path.join(SANDBOX, "status.json")
    B.write_status(st, p)
    with open(p) as f:
        check("write_status writes it atomically (no .tmp left)",
              json.load(f)["channel"] == CH and not os.path.exists(p + ".tmp"))

    # ---- 8. the binding --------------------------------------------------------------------
    bpath = os.path.join(SANDBOX, ".orc1_bindings.json")

    def write_b(d, bump):
        with open(bpath, "w") as f:
            f.write(d if isinstance(d, str) else json.dumps({"bindings": d}))
        os.utime(bpath, ns=(bump * 10 ** 9, bump * 10 ** 9))

    bd = B.Binding()
    check("Binding resolves ~ inside the sandbox", os.path.normpath(bd.path) == os.path.normpath(bpath), bd.path)
    check("no bindings file: unbound", bd.channel() is None)
    write_b({"10": "drums", "4": "daw"}, 1000)
    check("bound: channel read from the file", bd.channel() == 4)
    write_b({"10": "drums", "12": "daw"}, 1001)
    check("rebound: the file change is picked up", bd.channel() == 12)
    write_b("{not json", 1002)
    check("a half-written file keeps the last answer", bd.channel() == 12)
    write_b({"10": "drums"}, 1003)
    check("taken out of /matrix: unbound", bd.channel() is None)

    # ---- 9. the runtime: ports come and go -----------------------------------------------
    class FakePort:
        def __init__(self):
            self.avail = None
            self.name = None
            self.checks = self.closes = 0

        def check(self, extra_ok=True):
            self.checks += 1
            if self.name is not None and (self.avail != self.name or not extra_ok):
                self.close()
            if self.avail and self.name is None:
                self.name = self.avail
                return "new"
            return self.name is not None

        def close(self):
            if self.name is not None:
                self.closes += 1
            self.name = None

    P = {k: FakePort() for k in ("bus", "daw_in", "daw_out", "fire")}
    out = {"daw": [], "fire": []}
    br = B.Bridge(lambda m: out["daw"].append(m), lambda raw: out["fire"].append(list(raw)))
    logs = []
    rt = B.Runtime(br, P, B.Binding(bpath), "orc1-dawsurf", log=logs.append)
    P["bus"].avail = "RtMidiOut Client:ORC-daw 133:0"
    P["daw_in"].avail = P["daw_out"].avail = "rtpmidid:ORC DAW 136:2"
    P["fire"].avail = "ORC FW seq:ORC FW seq MIDI 1 40:0"

    def hellos():
        return sum(1 for m in out["daw"] if m["type"] == "sysex")

    st = rt.check()
    check("unbound: the bridge idles (no DAW / Fire port opened)",
          P["daw_out"].name is None and P["fire"].name is None and P["daw_in"].checks == 0)
    check("unbound: still follows the bus", P["bus"].name is not None)
    write_b({"6": "daw"}, 2000)
    st = rt.check()
    check("bound: DAW in + out and the Fire opened", P["daw_out"].name and P["daw_in"].name
          and P["fire"].name and st["daw"] and st["fire"], st)
    check("DAW port appearing -> hello sent", hellos() == 1, out["daw"])
    rt.check()
    rt.check()
    check("steady: no repeat hello, each state logged once",
          hellos() == 1 and sum("DAW port" in l for l in logs) == 1, logs)
    br.on_daw(pb(0, 16383))
    check("live: the DAW's fader reaches the Fire on channel 6", out["fire"][-1:] == [cc(6, 20, 127)],
          out["fire"])
    P["daw_in"].avail = P["daw_out"].avail = None
    st = rt.check()
    check("DAW port gone: marked down, logged once", not st["daw"] and not br.daw_up
          and sum("not there" in l and "DAW" in l for l in logs) == 1, logs)
    n = len(out["daw"])
    br.on_bus(cc(6, 21, 77))
    check("DAW down: Fire moves send nothing", len(out["daw"]) == n)
    P["daw_in"].avail = P["daw_out"].avail = "rtpmidid:ORC DAW 137:2"
    rt.check()
    check("DAW port back -> a second hello", hellos() == 2, out["daw"])
    P["daw_out"].avail = P["daw_in"].avail = "rtpmidid:ORC DAW 140:2"      # rtpmidid restarted
    rt.check()
    check("same name, new client:port (rtpmidid restart) -> hello again", hellos() == 3)
    out["fire"].clear()
    P["fire"].avail = "ORC FW seq:ORC FW seq MIDI 1 44:0"                  # Fire replugged
    rt.check()
    check("Fire replug -> its faders repainted", cc(6, 20, 127) in out["fire"], out["fire"])
    out["fire"].clear()
    write_b({"8": "daw"}, 2001)
    rt.check()
    check("rebind in the file -> the bridge follows (faders on channel 8)",
          br.ch == 8 and cc(8, 20, 127) in out["fire"], (br.ch, out["fire"]))
    write_b({"10": "drums"}, 2002)
    st = rt.check()
    check("unbound in the file -> DAW and Fire ports closed",
          P["daw_out"].name is None and P["fire"].name is None and not st["daw"], st)
    # the bus listener is reopened when the subscription check says it is deaf
    before = P["bus"].closes
    deaf = ('Client 133 : "RtMidiOut Client" [User Legacy]\n'
            '  Port   0 : "ORC-daw" (R-e-) [In]\n'
            'Client 150 : "orc1-dawsurf-bus" [User Legacy]\n'
            '  Port   0 : "bus in" (-We-) [Out]\n')
    rt.check(deaf)
    check("bus subscription lost -> the bus port is closed and reopened",
          P["bus"].closes == before + 1 and P["bus"].name is not None)

    # ---- 10. seq_listening -----------------------------------------------------------------
    ok_text = deaf + "    Connected From: 133:0\n"
    old_bus = ok_text + 'Client 140 : "RtMidiOut Client" [User Legacy]\n  Port   0 : "ORC-daw" (R-e-) [In]\n'
    check("seq_listening: connected from the bus", B.seq_listening(ok_text, "orc1-dawsurf-bus"))
    check("seq_listening: not connected", not B.seq_listening(deaf, "orc1-dawsurf-bus"))
    check("seq_listening: another client's subscription doesn't count",
          not B.seq_listening(ok_text, "orc-boxvis-in"))
    check("seq_listening: still fine with a second bus listed", B.seq_listening(old_bus, "orc1-dawsurf-bus"))
    # 2026-11-09: the bridge's module messages come from the ORC's own "ORC-daw" port, never the synth bus
    on_synth = ('Client 133 : "RtMidiOut Client" [User Legacy]\n  Port   0 : "ORC1-Synth-Bus" (R-e-) [In]\n'
                'Client 150 : "orc1-dawsurf-bus" [User Legacy]\n  Port   0 : "bus in" (-We-) [Out]\n'
                '    Connected From: 133:0\n')
    check("seq_listening: a subscription to the SYNTH BUS does not count (the DAW is not a synth)",
          not B.seq_listening(on_synth, "orc1-dawsurf-bus"))
    is_mine = lambda p: B.cfg.is_orc_port(p, B.MODULE)
    check("its port is EXACTLY 'ORC-daw': not the synth bus, not its own orc1-dawsurf-* clients, not a look-alike",
          is_mine("RtMidiOut Client:ORC-daw 140:0") and not is_mine("RtMidiOut Client:ORC1-Synth-Bus 133:0")
          and not is_mine("orc1-dawsurf-fire:fire faders 142:0") and not is_mine("RtMidiOut Client:ORC-daw2 141:0")
          and not is_mine("orc1-dawsurf-bus:bus in 150:0"))

    # ---- 11. port names ----------------------------------------------------------------------
    check("DAW port matched: rtpmidid session 'ORC DAW'", cfg.is_dawsurf_port("rtpmidid:ORC DAW 136:2"))
    check("DAW port: never the bridge's own ports",
          not cfg.is_dawsurf_port("orc1-dawsurf-daw-out:daw out 150:0")
          and not cfg.is_dawsurf_port("ORC DAW thing:orc1-dawsurf-fire 151:0"))
    check("DAW port: not the other rtpMIDI session", not cfg.is_dawsurf_port("rtpmidid:owner (2) 136:1"))
    check("Fire out: our firmware's port", B.is_fire_out("ORC FW seq:ORC FW seq MIDI 1 40:0"))
    check("Fire out: Akai's firmware / host mode too",
          B.is_fire_out("FL STUDIO FIRE:FL STUDIO FIRE MIDI 1 24:0") and B.is_fire_out("ORC FW host 1 30:0"))
    check("Fire out: not the bridge's own port / not the BSP",
          not B.is_fire_out("orc1-dawsurf-fire:fire faders 151:0")
          and not B.is_fire_out("Arturia BeatStep Pro:Arturia BeatStep Pro Arturia Be 32:0"))

    # ---- 12. the brain skips the DAW port + the bridge's clients (static, by AST) ---------------
    with open(os.path.join(HERE, "orc1.py"), encoding="utf-8") as f:
        osrc = f.read()
    tree = ast.parse(osrc)
    skips = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("_SKIP", "_skip_inputs", "_skip_sub"):
            seg = ast.get_source_segment(osrc, node.value)
            try:
                skips.setdefault(node.targets[0].id, []).append(eval(seg, {"cfg": cfg, "tuple": tuple, "getattr": getattr}))
            except Exception as e:
                skips.setdefault(node.targets[0].id, []).append(e)
    for nm in ("_SKIP", "_skip_inputs", "_skip_sub"):
        vals = skips.get(nm, [])
        names = ("rtpmidid:ORC DAW 136:2", "orc1-dawsurf-fire:fire faders 151:0",
                 "orc1-dawsurf-daw-out:daw out 150:0")
        check("brain %s skips the DAW port and the bridge's clients" % nm,
              len(vals) == 1 and isinstance(vals[0], tuple) and
              all(any(s in n.lower() for s in vals[0]) for n in names), vals)
    check("brain skips still keep the old entries", all(
        {"midi through", "rtmidi", "orc1-synth"} <= set(v[0]) for v in skips.values() if v and isinstance(v[0], tuple)))
    check("brain still opens the PC's normal rtpMIDI session",
          not any(s in "rtpmidid:studio (2) 136:1" for s in skips.get("_skip_inputs", [()])[0]))
    check("cfg.DAWSURF_BRAIN_SKIP is lower case", all(s == s.lower() for s in cfg.DAWSURF_BRAIN_SKIP)
          and "orc daw" in cfg.DAWSURF_BRAIN_SKIP and "orc1-dawsurf" in cfg.DAWSURF_BRAIN_SKIP)

    # ---- 13. the module + the unit file ---------------------------------------------------------
    K = cfg.MODULE_KEYS
    check("'daw' is the LAST module key (every earlier wire id unchanged)",
          K[-1] == "daw" and K.index("mxvbox") == 14 and K.index("drums") == 0 and len(set(K)) == len(K), K)
    with open(os.path.join(HERE, "..", "systemd", "orc1-dawsurf.service"), encoding="utf-8") as f:
        unit = f.read()
    check("unit: runs the bridge as pibox, niced, memory-capped",
          "User=pibox" in unit and "orc1/orc1_dawsurf_bridge.py" in unit and "Nice=10" in unit
          and "MemoryMax=" in unit)
    check("unit: NOT PartOf orc1 (a brain restart must not take it down)",
          not any(l.strip().startswith(("PartOf", "BindsTo")) for l in unit.splitlines()))
    import inspect
    psrc = inspect.getsource(B.Port.check)
    check("bridge: the callback is set again after every open_port",
          "set_callback(" in psrc and psrc.index("set_callback(") > psrc.index("open_port("))

    # ==== 14. the Fire's DAW page (fw v0.31f, hello version 6) ==================================
    # 2026-11-09 (owner): one list, the tracks then MASTER, one track per SELECT detent, 14-bit
    # levels so the knobs have full resolution. The Fire shows a 4-row window of REAPER's strips.
    HDR = [0x7D, 0x4F, 0x52, 0x43]

    def frames(raws):
        """ORC host frames among raw Fire sends -> [(cmd, [data...])]."""
        return [(r[5], list(r[6:-1])) for r in raws
                if r and r[0] == 0xF0 and r[-1] == 0xF7 and list(r[1:5]) == HDR]

    def ccs(raws):
        return [r for r in raws if r and (r[0] & 0xF0) == 0xB0]

    def make_page(version=6, daw=True, cc_with_page=False, ch=CH, names=None):
        clk = Clock()
        out = {"daw": [], "fire": []}
        logs = []
        br = B.Bridge(lambda m: out["daw"].append(m), lambda raw: out["fire"].append(list(raw)),
                      clock=clk, cc_with_page=cc_with_page, log=logs.append)
        br.set_channel(ch)
        if daw:
            br.daw_connected(True)
        br.fire_connected(True)
        if version is not None:
            br.on_fire(B.orc_frame(0, version))
        if names:
            br.on_daw(B.to_bytes(M.strip_labels(names)))
        return br, out, clk, logs

    def row_frame(row, rgb, lvl, name):
        return (0x1B, [row] + list(rgb) + [(lvl >> 7) & 0x7F, lvl & 0x7F] + [ord(c) for c in name])

    def lvl_frame(row, lvl):
        return (0x1C, [row, (lvl >> 7) & 0x7F, lvl & 0x7F])

    def chn(daw):
        return [n for n in notes(daw) if n[0] in (0x30, 0x31)]

    EIGHT = [str(i) for i in range(1, 9)]          # what REAPER sends for 8 unnamed tracks
    D2F, F2D = B.daw_to_fire, B.fire_to_daw         # the 0 dB knee (DAWSURF_UNITY_*)
    check("unity: a tap in pad column 12 = exactly REAPER's 0 dB (11730); 0 dB shows as column 12",
          F2D(B.fire_tap_fine(12)) == 11730 and D2F(11730) == B.fire_tap_fine(12)
          and F2D(0) == 0 and F2D(16383) == 16383 and D2F(0) == 0 and D2F(16383) == 16383)
    check("unity: monotonic both ways, round trip within 1",
          all(F2D(f) <= F2D(f + 1) for f in range(16383)) and all(D2F(d) <= D2F(d + 1) for d in range(16383))
          and max(abs(D2F(F2D(f)) - f) for f in range(16384)) <= 1)

    # hello
    br, out, clk, logs = make_page(version=None)
    check("page: a Fire appearing gets the hello F0 7D 4F 52 43 00 F7",
          [0xF0, 0x7D, 0x4F, 0x52, 0x43, 0x00, 0xF7] in out["fire"], out["fire"])
    out["fire"].clear()
    clk.t += 2.9
    br.tick()
    n_early = len(frames(out["fire"]))
    clk.t += 0.2
    br.tick()
    check("page: unanswered hello retried after 3 s, not before",
          n_early == 0 and frames(out["fire"]) == [(0, [])], out["fire"])
    for _ in range(10):
        clk.t += 3.1
        br.tick()
    nh = sum(1 for c, _ in frames(out["fire"]) if c == 0)
    check("page: after 5 fast tries the hello slows to every 30 s", nh == 4, nh)
    br.on_fire([0xF0, 0x7D, 0x4F, 0x52, 0x43, 0x00, 0xF7])
    check("page: a hello REQUEST (no version byte) is not taken as an answer", br.fire_version is None)

    # an older Fire: CC path only (3 = no page; 5 = the old 7-bit page this no longer speaks)
    for oldv in (3, 5):
        br, out, clk, logs = make_page(version=oldv)
        out["fire"].clear()
        br.tick()
        br.on_daw(pb(2, 8192))
        br.tick()
        check("hello %d: no page message at all, the DAW fader goes as CC 22" % oldv,
              frames(out["fire"]) == [] and out["fire"] == [cc(CH, 22, B.cc7(8192 / 16383))], out["fire"])
    check("hello 5: status says no page", br.status()["page"]["active"] is False
          and br.status()["page"]["fire_version"] == 5)
    br.on_fire(B.orc_frame(0x1D, 2, 100, 0))
    check("old Fire: a stray 1D moves nothing", pitches(out["daw"]) == [], out["daw"])

    # hello 6: DAW not there -> 0 rows; DAW up, nothing named yet -> just MASTER
    br, out, clk, logs = make_page(daw=False)
    out["fire"].clear()
    br.tick()
    check("hello 6, DAW not there: the Fire is told 0 rows once (a stale page goes)",
          frames(out["fire"]) == [(0x1A, [0])], out["fire"])
    out["fire"].clear()
    br.daw_connected(True)
    br.tick()
    F = frames(out["fire"])
    check("DAW up, no tracks named yet: 1A 1 + the MASTER row only",
          F == [(0x1A, [1]), row_frame(0, B.PAGE_MASTER_RGB, 0, "MASTER")], F)
    out["fire"].clear()
    clk.t += 1
    br.on_daw(B.to_bytes(M.strip_labels(EIGHT)))
    br.tick()
    F = frames(out["fire"])
    check("8 tracks: 1A 4 first, then tracks 1-4 (no MASTER while tracks fill the window)",
          F == [(0x1A, [4])] + [row_frame(r, B.PAGE_PALETTE[r], 0, EIGHT[r]) for r in range(4)], F)
    out["fire"].clear()
    br.tick()
    check("steady: nothing re-sent", out["fire"] == [], out["fire"])
    check("status: page active, 4 rows shown", br.status()["page"]["active"]
          and br.status()["page"]["rows_shown"] == 4 and len(br.status()["page"]["rows"]) == 4)
    check("1B wire bytes: R G B, level hi lo, name", B.orc_frame(0x1B, 0, 127, 60, 0, 99, 5, ord("K")) ==
          [0xF0, 0x7D, 0x4F, 0x52, 0x43, 0x1B, 0, 127, 60, 0, 99, 5, 0x4B, 0xF7])

    # levels are 14-bit; no CC while the page is up
    clk.t += 1
    br.on_daw(pb(2, 5000))
    check("page up: the DAW's fader is NOT also sent as a CC (DAWSURF_CC_WITH_PAGE off)",
          ccs(out["fire"]) == [], out["fire"])
    br.tick()
    check("a level change -> one 1C with the FULL 14-bit level (in the Fire's scale)",
          frames(out["fire"]) == [lvl_frame(2, D2F(5000))], out["fire"])
    out["fire"].clear()
    clk.t += 1
    br.on_daw(pb(2, 5001))
    br.tick()
    check("one 14-bit step is news (it was lost at 7 bits)", frames(out["fire"]) == [lvl_frame(2, D2F(5001))],
          out["fire"])
    out["fire"].clear()
    clk.t += 1
    br.on_daw(B.to_bytes(M.lcd("Kick".ljust(7), 0, 0)))      # strip 1's cell only
    br.tick()
    check("a name change -> 1B for that row only",
          frames(out["fire"]) == [row_frame(0, B.PAGE_PALETTE[0], 0, "Kick")], out["fire"])
    out["fire"].clear()
    clk.t += 1
    br.on_daw(pb(8, 16383))
    br.on_daw(pb(5, 16383))
    br.tick()
    check("strips outside the window / MASTER while not shown: nothing sent", frames(out["fire"]) == [],
          out["fire"])

    # cc_with_page: both
    brc, outc, clkc, _ = make_page(cc_with_page=True, names=EIGHT)
    brc.tick()
    outc["fire"].clear()
    clkc.t += 1
    brc.on_daw(pb(1, 16383))
    brc.tick()
    check("DAWSURF_CC_WITH_PAGE on: the CC AND the 1C go",
          ccs(outc["fire"]) == [cc(CH, 21, 127)] and frames(outc["fire"]) == [lvl_frame(1, 16383)],
          outc["fire"])

    # coalescing
    br, out, clk, logs = make_page(names=EIGHT)
    br.tick()
    out["fire"].clear()
    clk.t += 1
    for i in range(100):
        br.on_daw(pb(3, i * 160))                     # a REAPER fader moving: 100 bends at once
    br.tick()
    check("100 bends in one window -> ONE 1C with the latest value",
          frames(out["fire"]) == [lvl_frame(3, D2F(99 * 160))], out["fire"])
    out["fire"].clear()
    br.on_daw(pb(3, 16383))
    clk.t += 0.01
    br.tick()
    held = list(out["fire"])
    clk.t += 0.011
    br.tick()
    check("a row is not re-sent within PAGE_MIN_GAP_S; the latest value goes after it",
          held == [] and frames(out["fire"]) == [lvl_frame(3, 16383)], (held, out["fire"]))
    out["fire"].clear()
    clk.t += 1
    for k in range(50):                               # every strip moving + renamed, over and over
        for s in range(8):
            br.on_daw(pb(s, (k * 300 + s) % 16384))
        br.on_daw(B.to_bytes(M.strip_labels(["T%d_%d" % (k % 3, s) for s in range(8)])))
    br.tick()
    rows = [d[0] for c, d in frames(out["fire"])]
    check("a burst over every strip: at most one message per row per tick", sorted(rows) == list(range(4)), rows)

    # the Fire moves a row: 1D <row> <hi> <lo>
    br, out, clk, logs = make_page(names=EIGHT)
    br.tick()
    out["fire"].clear()
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1D, 3, 64, 1))
    check("1D row 4 (14-bit) -> MCU touch strip 4 then its pitch bend (through the 0 dB knee)",
          notes(out["daw"])[:1] == [(0x6B, 127)] and pitches(out["daw"]) == [(3, F2D(64 << 7 | 1))], out["daw"])
    br.tick()
    check("1D: the move is not bounced back to the Fire (no 1C, no CC)", out["fire"] == [], out["fire"])
    br.on_daw(pb(3, F2D(64 << 7 | 1)))
    br.on_daw(pb(3, 0))
    br.tick()
    check("1D: REAPER's echo / a late report while held never reaches the Fire", out["fire"] == [],
          out["fire"])
    clk.t += cfg.DAWSURF_TOUCH_RELEASE_S + 0.1
    br.tick()
    br.on_daw(pb(3, F2D(64 << 7 | 1)))
    br.tick()
    check("1D: touch released after the hold time; REAPER confirming the value sends nothing",
          (0x6B, 0) in notes(out["daw"]) and frames(out["fire"]) == [], out["fire"])
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1D, 3, 64, 1))
    check("1D equal to what the Fire shows: no news, nothing to the DAW", out["daw"] == [], out["daw"])
    br.on_fire(B.orc_frame(0x1D, 4, 10, 0))
    br.on_fire(B.orc_frame(0x1D, 1, 3))
    check("1D past the shown rows / too short (the old 7-bit form): ignored", out["daw"] == [], out["daw"])
    out["fire"].clear()
    clk.t += 1
    br.on_daw(pb(2, 16383))
    br.tick()
    sent = frames(out["fire"])
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1D, 2, 127, 127))
    check("a 1D that echoes our 1C is not sent on to the DAW",
          sent == [lvl_frame(2, 16383)] and out["daw"] == [], (sent, out["daw"]))

    # MASTER: after the last track, inside the window
    br, out, clk, logs = make_page(names=["Kick", "Bass"])
    br.tick()
    F = frames(out["fire"])
    check("2 tracks: rows = Kick, Bass, MASTER (1A 3)",
          (0x1A, [3]) in F and row_frame(2, B.PAGE_MASTER_RGB, 0, "MASTER") in F, F)
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1D, 2, 127, 127))
    check("1D on the MASTER row -> the master fader (pitch bend ch 9) + master touch",
          (8, 16383) in pitches(out["daw"]) and (0x70, 127) in notes(out["daw"]), out["daw"])
    out["fire"].clear()
    clk.t += cfg.DAWSURF_TOUCH_RELEASE_S + 1
    br.tick()
    br.on_daw(pb(8, 5000))
    br.tick()
    check("the master fader -> its row", frames(out["fire"]) == [lvl_frame(2, D2F(5000))], out["fire"])

    # 1E <dir> <count>: REAPER moves one track per detent; the list ends at the last track + MASTER
    br, out, clk, logs = make_page(names=EIGHT)
    br.tick()
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1E, 1, 1))
    check("1E 01 1 -> MCU channel right once (one track, not a bank of 8)",
          chn(out["daw"]) == [(0x31, 127), (0x31, 0)] and not [n for n in notes(out["daw"]) if n[0] in (0x2E, 0x2F)],
          out["daw"])
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1E, 0, 4))
    check("1E 00 4 (SHIFT + PATTERN) -> channel left 4 times", chn(out["daw"]) == [(0x30, 127), (0x30, 0)] * 4,
          out["daw"])
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1E, 1, 9))
    check("8 real strips: forward is allowed up to 5 (last 3 tracks + MASTER), more once REAPER re-sends",
          chn(out["daw"]).count((0x31, 127)) == 5, out["daw"])
    br, out, clk, logs = make_page(names=["Kick", "Bass", "Keys"])
    br.tick()
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1E, 1, 1))
    check("3 tracks + MASTER already fill the window: forward does nothing (the list's end)",
          chn(out["daw"]) == [], out["daw"])
    br, out, clk, logs = make_page(names=EIGHT)
    br.tick()
    br.on_fire(B.orc_frame(0x1E, 1, 1))
    out["fire"].clear()
    clk.t += 1
    br.on_daw(B.to_bytes(M.strip_labels([str(i) for i in range(2, 10)])))   # REAPER moved by one
    for s in range(8):
        br.on_daw(pb(s, 1000 * (s + 2)))
    br.tick()
    F = frames(out["fire"])
    check("after a shift every row shows the next track (2-5), one 1B per row",
          F == [row_frame(s, B.PAGE_PALETTE[s], D2F(1000 * (s + 2)), str(s + 2)) for s in range(4)], F)
    out["fire"].clear()
    clk.t += 1
    br.on_daw(B.to_bytes(M.strip_labels(["7", "8", "9", "", "", "", "", ""])))   # near the end of 9 tracks
    br.tick()
    names_sent = [bytes(d[6:]).decode() for c, d in frames(out["fire"]) if c == 0x1B]
    check("9 tracks at the end: rows 7, 8, 9, MASTER (no empty rows)",
          names_sent == ["7", "8", "9", "MASTER"], frames(out["fire"]))

    # DAW gone / back
    out["fire"].clear()
    br.daw_connected(False)
    br.tick()
    check("DAW gone -> 1A 0 (the page disappears), no rows", frames(out["fire"]) == [(0x1A, [0])],
          out["fire"])
    nm = br.n["page_move"]
    out["daw"].clear()
    br.on_fire(B.orc_frame(0x1D, 2, 10, 0))
    check("no rows shown: a late 1D is ignored", br.n["page_move"] == nm and out["daw"] == [], br.n)
    out["fire"].clear()
    br.daw_connected(True)
    br.tick()
    F = frames(out["fire"])
    check("DAW back -> the page again, 1A first then every row", F[:1] == [(0x1A, [4])] and
          [d[0] for c, d in F[1:] if c == 0x1B] == list(range(4)), F)

    # names
    check("row names: printable ASCII, max 8, trimmed",
          B.page_name("  Ab\x01cdefghijk ") == "Abcdefgh" and B.page_name(None) == "")
    check("fine14: 0 -> 0, 1 -> 16383, clamped", B.fine14(0) == 0 and B.fine14(1) == 16383 and B.fine14(2) == 16383
          and B.fine14(-1) == 0)

    # ==== 15. a hardware Mackie surface (the BeatStep Pro) on the SAME window + bank pads (2026-11-09) ====
    surf_out = []
    br = B.Bridge(lambda m: out_d.append(m), lambda raw: None, send_surface=surf_out.append, log=lambda t: None)
    out_d = []
    br.set_channel(CH)
    br.daw_connected(True)
    br.surface_connected(True)
    check("BSP up: its 8 x (rec, solo, mute, select) step lights are set from the model (all off)",
          len(surf_out) == 32 and all(m[2] == 0 for m in surf_out), surf_out[:4])
    surf_out.clear()
    br.on_daw(B.to_bytes({"type": "note_on", "channel": 0, "note": 9, "velocity": 127}))   # REAPER: solo 2 ON
    check("REAPER's solo on track 2 lights the BSP's step for it (note 9)", surf_out == [[0x90, 9, 127]], surf_out)
    out_d.clear()
    br.on_surface([0xB0, 16, 1])
    br.on_surface([0xB0, 18, 65])
    vp = [(m["control"], m["value"]) for m in out_d if m["type"] == "control_change"]
    check("BSP pan knobs: V-pot 1 +1, V-pot 3 -1 go on to the DAW", vp == [(16, 1), (18, 65)], out_d)
    out_d.clear()
    br.on_daw(pb(0, 8000))                              # REAPER says track 1 sits at 8000
    br.on_surface([0xE0, 0x00, 0x20])                   # the BSP's remembered position: 4096 (first reading)
    check("BSP fader, first reading: only calibrates, nothing moves", pitches(out_d) == [], out_d)
    br.on_surface([0xE0, 0x00, 0x21])                   # +128 (one click)
    check("BSP fader: the CHANGE moves the track from where it really is (8000 + 128)",
          pitches(out_d) == [(0, 8128)] and (0x68, 127) in notes(out_d), out_d)
    out_d.clear()
    br2s = B.Bridge(lambda m: out_s.append(m), lambda raw: None, log=lambda t: None)
    out_s = []
    br2s.set_channel(CH)
    br2s.daw_connected(True)
    br2s.on_daw(pb(1, 9000))                            # track 2 sits at 9000
    br2s.on_surface([0xE1, 0, 0])                       # the BSP's knob 10 is at its bottom (first reading)
    br2s.on_surface([0xE1, 0, 0])                       # a click down: it repeats 0
    br2s.on_surface([0xE1, 0, 0])
    check("BSP knob pinned at ITS bottom: each repeated 0 = one more click down (track 9000 -> 8744)",
          pitches(out_s)[-1:] == [(1, 9000 - 256)], out_s)
    out_s.clear()
    br2s.on_surface([0xE1, 0x7F, 0x7F])                 # jumps to its top
    br2s.on_surface([0xE1, 0x7F, 0x7F])                 # pinned at the top: one more click up
    check("...and at ITS top a repeat = one more click up", pitches(out_s)[-1:] == [(1, 8744 + 16383 + 128)] or
          pitches(out_s)[-1:] == [(1, 16383)], out_s)
    br2s.on_surface([0xE1, 0x00, 0x30])
    n = len(out_s)
    br2s.on_surface([0xE1, 0x00, 0x30])
    check("a repeat in the MIDDLE is not a click (nothing sent)", len(out_s) == n, out_s[n:])
    out_d.clear()
    br.on_surface([0x90, 16, 127])
    br.on_surface([0x80, 16, 0])
    check("BSP even step = mute track 1: press + release to the DAW", notes(out_d) == [(16, 127), (16, 0)], out_d)
    out_d.clear()
    br.surf.display["assignment"] = "01"
    check("window_first from REAPER's display '01' = 0; bank 1", br.window_first() == 0 and br.current_bank() == 0)
    br.on_bus([0x90 | (CH - 1), 46, 100])               # bank pad 3 (top row) -> tracks 17-24
    check("bank pad 3: two bank-right presses (tracks 1 -> 17)",
          [n for n in notes(out_d) if n[1]] == [(0x2F, 127)] * 2, out_d)
    out_d.clear()
    br.surf.display["assignment"] = "19"
    br.on_bus([0x90 | (CH - 1), 46, 100])
    check("window at track 19 (Fire nudged it): pad 3 = two channel-left presses back to 17",
          [n for n in notes(out_d) if n[1]] == [(0x30, 127)] * 2, out_d)
    out_d.clear()
    br.surf.display["assignment"] = "17"
    br.on_bus([0x90 | (CH - 1), 46, 100])
    br.on_bus([0x90 | (CH - 1), 46, 0])
    check("already on that bank / a pad release: nothing", out_d == [], out_d)
    br.on_bus([0x90 | (CH - 1), 36, 100])               # bottom-left pad = bank 9 -> track 65
    check("bottom-left pad = bank 9: from track 17, six bank-right presses",
          [n for n in notes(out_d) if n[1]].count((0x2F, 127)) == 6, out_d)
    lights = []
    brl = B.Bridge(lambda m: None, lambda raw: None, send_banklight=lights.append, log=lambda t: None)
    brl.set_channel(CH)
    brl.daw_connected(True)
    brl.banklight_connected(True)
    brl.surf.display["assignment"] = "01"
    brl.tick()
    check("bank light: bank 1 on screen -> pad item 0x78 held ON", lights == [[0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 2, 0, 0x10, 0x78, 0x7F, 0xF7]], lights)
    lights.clear()
    brl.tick()
    check("bank light: steady -> nothing re-sent", lights == [], lights)
    brl.surf.display["assignment"] = "73"
    brl.tick()
    check("bank light: window at track 73 = bank 10 -> old pad handed back (0x40), item 0x71 ON",
          [m[9:11] for m in lights] == [[0x78, 0x40], [0x71, 0x7F]], lights)
    lights.clear()
    brl.banklight_connected(True)
    brl.tick()
    check("bank light: the BSP reappearing -> lit again", [m[9:11] for m in lights] == [[0x71, 0x7F]], lights)
    lights.clear()
    brl.daw_connected(False)
    brl.tick()
    check("bank light: DAW gone -> the pad handed back", [m[9:11] for m in lights] == [[0x71, 0x40]], lights)
    check("bank light port: the BSP's MAIN port only", B.is_banklight_port("Arturia BeatStep Pro:Arturia BeatStep Pro Arturia Be 40:0")
          and not B.is_banklight_port("Arturia BeatStep Pro:Arturia BeatStep Pro BeatStepPr 40:1"))
    check("bank pads read on the daw channel only", (B.Bridge.on_bus(br, [0x90 | (CH % 16), 46, 100]) is None))
    check("brain skips the BSP's Mackie port (cfg)", "beatstep pro beatsteppr" in cfg.DAWSURF_BRAIN_SKIP
          and B.is_mcu_surface("Arturia BeatStep Pro:Arturia BeatStep Pro BeatStepPr 40:1")
          and not B.is_mcu_surface("Arturia BeatStep Pro:Arturia BeatStep Pro Arturia Be 40:0")
          and not B.is_mcu_surface("orc1-dawsurf-surf-out:x BeatStep Pro BeatStepPr"))
    # fw v0.36: the Fire's own DAW port (names measured on the Pi 2026-11-12) is skipped by the brain and is NOT
    # taken for the sequencer port by any seq-name matcher; the seq port still is.
    _seq, _daw = "ORC FW:ORC FW seq 32:0", "ORC FW:ORC FW DAW 32:1"
    _skip = lambda n: any(s in n.lower() for s in cfg.DAWSURF_BRAIN_SKIP)
    check("brain skips the Fire's DAW port, not its seq port (fw v0.36)",
          _skip(_daw) and _skip("FLAME:FLAME DAW 32:1") and not _skip(_seq) and not _skip("FLAME:FLAME seq 32:0"))
    check("the seq-port names match the Fire's seq port only (fw v0.36)",
          any(p.lower() in _seq.lower() for p in cfg.FIRE_SEQ_PORT_NAMES)
          and not any(p.lower() in _daw.lower() for p in cfg.FIRE_SEQ_PORT_NAMES)
          and cfg.is_clock_thru_port(_seq) and not cfg.is_clock_thru_port(_daw)
          and B.is_fire_out(_seq) and not B.is_fire_out(_daw) and not cfg.is_fire_port(_daw))

    # OSC: the web remote's track list decides the count (REAPER's OSC fakes "Track N" past the end)
    bro = B.Bridge(lambda m: None, lambda raw: None, log=lambda t: None, dialect="osc") if "dialect" in B.Bridge.__init__.__code__.co_varnames else None
    if bro is not None:
        bro.set_tracks({i: ("", None) for i in range(0, 20)})          # master + 19 tracks
        check("OSC + web remote: 19 tracks, slot 19 holds one, slot 20 doesn't (whatever OSC says)",
              bro.n_tracks() == 19 and bro._has_track(18) and not bro._has_track(19), (bro.n_tracks(),))
    else:
        check("OSC + web remote: bridge takes a dialect", False, "no dialect kwarg")
    # ---- colours: REAPER web remote ----
    text = ("NTRACK\t12\n"
            "TRACK\t0\tMASTER\t0\t1\t0\t-1500\t-1500\t1\t0\t0\t0\t1\t0\n"
            "TRACK\t1\tKick\t0\t1\t0\t-1500\t-1500\t1\t0\t0\t0\t1\t0\n"
            "TRACK\t2\tBassline\t0\t1\t0\t-1500\t-1500\t1\t0\t0\t0\t1\t%d\n" % 0x1FF8000 +
            "TRACK\tx\tbad\n"
            "TRACK\t3\t\t0\t1\t0\t-1500\t-1500\t1\t0\t0\t0\t1\t%d\r\n" % 0x1000000)
    tr = B.parse_reaper_tracks(text)
    check("web: TRACK lines parsed, a bad line skipped", sorted(tr) == [0, 1, 2, 3], tr)
    check("web: colour 0 = none (palette)", tr[1] == ("Kick", None), tr[1])
    check("web: 0x1FF8000 = custom flag + #FF8000 -> (127, 64, 0)", tr[2] == ("Bassline", (127, 64, 0)), tr[2])
    check("web: the flag alone = black (custom colour set to black)", tr[3] == ("", (0, 0, 0)), tr[3])
    check("web: bgr=True reads Windows BGR", B.parse_reaper_tracks(text, bgr=True)[2][1] == (0, 64, 127))
    check("MCU name match: 6 chars of the name; unnamed = the number",
          B.mcu_name_matches("Bassli", "Bassline", 2) and B.mcu_name_matches("09", "", 9)
          and B.mcu_name_matches("09", "Track 9", 9) and not B.mcu_name_matches("09", "Drums", 9)
          and not B.mcu_name_matches("", "", 1) and not B.mcu_name_matches("Bass", "Bassline", 2))

    def tracks(n, named=None, coloured=None):
        named, coloured = named or {}, coloured or {}
        return {i: (named.get(i, ""), coloured.get(i)) for i in range(0, n + 1)}

    br, out, clk, logs = make_page(names=EIGHT)
    br.tick()
    out["fire"].clear()
    clk.t += 1
    br.set_tracks(tracks(12, coloured={1: (10, 20, 30), 3: (1, 2, 3)}))
    br.tick()
    check("web colours: strip i <- track i+1 at bank 0 (unnamed tracks = their numbers), others keep the palette",
          frames(out["fire"]) == [row_frame(0, (10, 20, 30), 0, "1"), row_frame(2, (1, 2, 3), 0, "3")],
          out["fire"])
    out["fire"].clear()
    clk.t += 1
    br.on_daw(B.to_bytes(M.strip_labels(["Kick", "Bass"])))
    br.set_tracks(tracks(12, named={9: "Kick", 10: "Bassline"}, coloured={9: (5, 5, 5), 10: (6, 6, 6)}))
    br.tick()
    F = {d[0]: d for c, d in frames(out["fire"])}
    check("web colours: the strip names find the bank (Kick, Bass = tracks 9, 10 -> offset 8)",
          br.bank_offset == 8 and F.get(0, [None])[1:4] == [5, 5, 5] and F.get(1, [None])[1:4] == [6, 6, 6]
          and F.get(2, [None])[1:4] == list(B.PAGE_MASTER_RGB), (br.bank_offset, F))
    # unnamed tracks show their number
    br2, out2, clk2, _ = make_page()
    br2.on_daw(B.to_bytes(M.strip_labels(["  17  ", "  18  "])))
    br2.set_tracks(tracks(20, coloured={17: (7, 7, 7)}))
    check("web colours: unnamed tracks matched by their number ('  17  ' -> offset 16)",
          br2.bank_offset == 16 and br2.row_colour(0) == (7, 7, 7), br2.bank_offset)
    # no name matches: the bank counter
    br3, out3, clk3, _ = make_page()
    br3.set_tracks(tracks(10))
    br3.bank(+1)
    br3.bank(+1)
    check("bank counter: +8, and stops where REAPER stops (offset + 8 >= tracks)", br3.bank_offset == 8,
          br3.bank_offset)
    br3.bank(-1)
    br3.bank(-1)
    check("bank counter: never below 0", br3.bank_offset == 0)
    br3.surf.receive(M.strip_labels(["Zed"]))
    br3.set_tracks(None)
    check("web remote gone: back to the palette", br3.row_colour(0) == B.PAGE_PALETTE[0] and not br3.web_colour)
    check("master row keeps its own colour", br3.row_colour(B.MASTER) == B.PAGE_MASTER_RGB)

    # the poller
    calls = []
    answers = {"text": text, "fail": False}

    def fake_fetch(url):
        calls.append(url)
        if answers["fail"]:
            raise OSError("timed out")
        return answers["text"]

    br4, out4, clk4, _ = make_page(version=3)
    wl = []
    web = B.ReaperWeb(br4, url="http://192.168.1.50:8080/", fetch=fake_fetch, log=wl.append)
    check("web poller: idle (no request) while the Fire has no page", web.poll_once() is False and calls == [])
    br4.on_fire(B.orc_frame(0, 6))
    check("web poller: page up -> GET <url>/_/TRACK, colours in",
          web.poll_once() and calls == ["http://192.168.1.50:8080/_/TRACK"] and br4.web_colour.get(1) == (127, 64, 0),
          (calls, br4.web_colour))
    answers["fail"] = True
    web.poll_once()
    web.poll_once()
    kept = dict(br4.web_colour)
    web.poll_once()
    check("web poller: colours kept over 2 failures, dropped (palette) on the 3rd, logged once",
          kept and not br4.web_colour and sum("not answering" in l for l in wl) == 1, (kept, wl))
    check("status: plain JSON with the page block", isinstance(json.loads(json.dumps(br4.status()))["page"], dict))

    # ---- the runtime: Fire replug re-asks hello and rebuilds the page; unbind clears it ----
    P = {k: FakePort() for k in ("bus", "daw_in", "daw_out", "fire", "fire_in")}
    out = {"daw": [], "fire": []}
    clk = Clock()
    br = B.Bridge(lambda m: out["daw"].append(m), lambda raw: out["fire"].append(list(raw)), clock=clk,
                  log=lambda t: None)
    rt = B.Runtime(br, P, B.Binding(bpath), "orc1-dawsurf", log=lambda t: None)
    P["bus"].avail = "RtMidiOut Client:ORC-daw 133:0"
    P["daw_in"].avail = P["daw_out"].avail = "rtpmidid:ORC DAW 136:2"
    P["fire"].avail = P["fire_in"].avail = "ORC FW seq:ORC FW seq MIDI 1 40:0"
    write_b({"4": "daw"}, 3000)
    rt.check()
    check("runtime: the Fire's own output opened too (fire_in) and a hello sent",
          P["fire_in"].name is not None and (0, []) in frames(out["fire"]), out["fire"])
    br.on_fire(B.orc_frame(0, 6))
    br.tick()
    check("runtime: answered 6 -> the page goes up (MASTER only until REAPER names its tracks)",
          [f for f in frames(out["fire"]) if f[0] == 0x1A] == [(0x1A, [1])], frames(out["fire"]))
    out["fire"].clear()
    P["fire"].avail = P["fire_in"].avail = "ORC FW seq:ORC FW seq MIDI 1 44:0"      # replugged
    rt.check()
    check("Fire replug: version forgotten, hello asked again",
          br.fire_version is None and frames(out["fire"]) == [(0, [])], out["fire"])
    out["fire"].clear()
    br.on_fire(B.orc_frame(0, 6))
    br.tick()
    F = frames(out["fire"])
    check("Fire replug: the page rebuilt (1A first, then its rows)",
          F[:1] == [(0x1A, [1])] and len([1 for c, d in F if c == 0x1B]) == 1, F)
    out["fire"].clear()
    P["fire_in"].avail = "ORC FW seq:ORC FW seq MIDI 1 48:0"       # only the input side re-listed
    rt.check()
    check("a new Fire INPUT port alone also re-asks hello", (0, []) in frames(out["fire"]), out["fire"])
    br.on_fire(B.orc_frame(0, 6))
    br.tick()
    out["fire"].clear()
    write_b({"10": "drums"}, 3001)
    rt.check()
    check("unbound: 1A 0 sent BEFORE the Fire port closes",
          frames(out["fire"]) == [(0x1A, [0])] and P["fire"].name is None and P["fire_in"].name is None,
          out["fire"])

    # ---- the brain and the Fire's new SysEx --------------------------------------------------
    import orc1_scenes as SC
    import orc1_fire_pages as FP
    SEQ = "ORC FW seq:ORC FW seq MIDI 1 40:0"
    link = SC.FireBarLink(None, log=lambda *a: None)
    fp = FP.FirePages(lambda d: None, lambda: (True, 5), link.is_ours, log=lambda *a: None)
    page_msgs = [HDR + [0x1D, 3, 40], HDR + [0x1E, 1], HDR + [0x00, 5]]
    check("brain: fire_pages leaves 1D / 1E / hello to the next handler",
          not any(fp.handle_sysex(d, SEQ) for d in page_msgs))
    check("brain: FireBarLink swallows 1D / 1E / hello 5 from the seq Fire (never routed / to the DIN)",
          all(link.handle_sysex(d, SEQ) for d in page_msgs) and link.version == 5)
    check("brain: hello 5 still counts as a bars-capable Fire (>= 2) and pages-capable (>= 3)",
          link.version >= SC.BARS_VERSION and link.version >= FP.PAGES_VERSION)
    with open(os.path.join(HERE, "orc1.py"), encoding="utf-8") as f:
        osrc2 = f.read()
    i1 = osrc2.find("fire_pages.handle_sysex(msg.data, source_name)")
    i2 = osrc2.find("fire_bar_link.handle_sysex(msg.data, source_name)")
    i3 = osrc2.find("_sysex_is_device_chatter(msg.data)")
    check("brain: SysEx goes fire_pages -> FireBarLink -> device-chatter filter, in that order",
          0 < i1 < i2 < i3, (i1, i2, i3))

    # ==== 16. BINDINGS: everything mappable (orc1_dawsurf_bind, 2026-11-09) ====================
    import orc1_dawsurf_bind as BIND
    T = BIND.BindingTable(path="")
    f = lambda kind, num, ch=CH: T.find(kind, ch, num, CH)
    check("shipped: CC 20-27 = fader slots 0-7 of the cc view, with feedback; 28 = master",
          [(f("cc", c)["action"], f("cc", c)["slot"], f("cc", c)["view"], f("cc", c)["feedback"])
           for c in range(20, 29)] == [("fader", i, "cc", True) for i in range(8)] + [("fader", "master", "cc", True)])
    check("shipped: CC 29 / 30 = bank -1 / +1 of the cc view",
          (f("cc", 29)["action"], f("cc", 29)["dir"], f("cc", 30)["dir"]) == ("bank", -1, 1))
    check("shipped: notes 44-51 = banks 0-7, 36-43 = banks 8-15 on the bsp view",
          [f("note", n)["bank"] for n in range(44, 52)] == list(range(8))
          and [f("note", n)["bank"] for n in range(36, 44)] == list(range(8, 16))
          and all(f("note", n)["view"] == "bsp" for n in range(36, 52)))
    check("shipped: CC 31 / 32 / 33 toggle the fire / bsp / cc view",
          [(f("cc", c)["action"], f("cc", c)["surface"], f("cc", c)["mode"]) for c in (31, 32, 33)]
          == [("view_mode", s, "toggle") for s in ("fire", "bsp", "cc")])
    check("a daw-channel binding only answers on the daw channel", T.find("cc", CH + 1, 20, CH) is None
          and T.find("cc", CH, 20, None) is None)
    check("view CCs are free of the fader / bank CCs (cfg)",
          not set(cfg.DAWSURF_VIEW_CC) & (set(cfg.DAWSURF_FADER_CC) | set(cfg.DAWSURF_BANK_CC)))
    bad = [BIND.check_binding(x)[1] for x in (
        {"cc": 1, "note": 2, "action": "mute", "slot": 0}, {"cc": 200, "action": "mute", "slot": 0},
        {"cc": 1, "ch": 17, "action": "mute", "slot": 0}, {"cc": 1, "action": "explode"},
        {"cc": 1, "action": "mute"}, {"cc": 1, "action": "bank", "dir": 2},
        {"cc": 1, "action": "transport", "name": "eject"}, {"cc": 1, "action": "view_mode", "surface": "x"},
        {"cc": 1, "action": "fader", "slot": 0, "view": "kikpad"}, {"cc": 1, "action": "shift", "n": 0})]
    check("bad bindings are refused with a reason (10 kinds)", all(bad), bad)

    upath = os.path.join(SANDBOX, ".orc1_dawsurf_bindings.json")

    def write_u(d, bump):
        with open(upath, "w") as fh:
            fh.write(d if isinstance(d, str) else json.dumps(d))
        os.utime(upath, ns=(bump * 10 ** 9, bump * 10 ** 9))

    write_u({"bindings": [{"cc": 74, "action": "fader", "slot": 2},
                          {"cc": 20, "action": "none"},
                          {"note": 60, "ch": 3, "action": "transport", "name": "play"},
                          {"cc": 75, "action": "mute", "slot": 1},
                          {"cc": 76, "action": "fader", "slot": 0, "rel": True},
                          {"cc": 99, "action": "bogus"}]}, 3000)
    logs = []
    br = B.Bridge(lambda m: out["daw"].append(m), lambda raw: out["fire"].append(list(raw)), log=logs.append)
    out = {"daw": [], "fire": []}
    check("the user's file resolves ~ inside the sandbox", os.path.normpath(br.binds.path) == os.path.normpath(upath))
    check("a bad user binding is skipped and logged, the rest load",
          len(br.binds.errors) == 1 and any("skipped" in l and "bogus" in l for l in logs), (br.binds.errors, logs))
    br.set_channel(CH)
    br.daw_connected(True)
    out["daw"].clear()
    br.on_bus(cc(CH, 74, 100))
    check("user binding: CC 74 (not a shipped CC) moves strip 3's fader",
          pitches(out["daw"]) == [(2, round(100 / 127 * 16383))], out["daw"])
    out["daw"].clear()
    br.on_bus(cc(CH, 20, 90))
    check('user binding: "action": "none" switches the shipped CC 20 off', out["daw"] == [], out["daw"])
    br.on_bus(cc(CH, 21, 90))
    check("...the other shipped CCs still work", pitches(out["daw"]) == [(1, round(90 / 127 * 16383))], out["daw"])
    out["daw"].clear()
    br.on_bus([0x92, 60, 100])                          # channel 3, a literal-channel binding
    check("user binding on its own channel (3): note 60 = PLAY press + release",
          notes(out["daw"]) == [(M.BUTTON_NOTES["play"], 127), (M.BUTTON_NOTES["play"], 0)], out["daw"])
    out["daw"].clear()
    br.on_bus(cc(CH, 75, 127))
    br.on_bus(cc(CH, 75, 0))
    br.on_bus(cc(CH, 75, 127))
    check("a CC button: each rise through 64 = one press (mute strip 2), the fall is no press",
          [n for n in notes(out["daw"]) if n[1]] == [(17, 127)] * 2, out["daw"])
    out["daw"].clear()
    br.on_daw(pb(0, 8000))
    br.on_bus(cc(CH, 76, 66))
    check("a relative knob (64 + 2) nudges strip 1 up 2/128 of the range from where REAPER has it",
          pitches(out["daw"]) == [(0, round((8000 / 16383 + 2 / 128.0) * 16383))], out["daw"])
    write_u({"bindings": [{"cc": 74, "action": "fader", "slot": 5}], "replace_defaults": True}, 3001)
    check("the file is re-read when it changes", br.binds.refresh() and not br.binds.refresh())
    out["daw"].clear()
    br.on_bus(cc(CH, 21, 10))
    br.on_bus(cc(CH, 74, 10))
    check('"replace_defaults": the shipped CCs are gone, the user\'s remain',
          pitches(out["daw"]) == [(5, round(10 / 127 * 16383))], out["daw"])
    write_u("{half a file", 3002)
    check("a broken file keeps the last table", br.binds.refresh() is False and br.binds.find("cc", CH, 74, CH))
    os.remove(upath)
    br.binds.refresh()
    check("file gone: back to the shipped table", br.binds.find("cc", CH, 20, CH)["slot"] == 0
          and br.binds.find("cc", CH, 74, CH) is None)

    # ==== 17. VIEWS with REAPER on OSC: shared / independent per surface ==========================
    def osc_tracks(br, n, first=1, names=None):
        for t in range(first, n + 1):
            br.on_daw(("/track/%d/number/str" % t, (str(t),)))
            br.on_daw(("/track/%d/name" % t, ((names or {}).get(t, "Trk%d" % t),)))
            br.on_daw(("/track/%d/volume" % t, (t / 100.0,)))

    def osc_make(tracks=12, strips=16, version=6, modes=None):
        clk = Clock()
        o = {"daw": [], "fire": []}
        b2 = B.Bridge(lambda m: o["daw"].append(m), lambda raw: o["fire"].append(list(raw)), clock=clk,
                      dialect="osc", osc_tracks=strips, bindings=BIND.default_bindings(),
                      view_modes=modes, log=lambda t: None)
        b2.set_channel(CH)
        b2.daw_connected(True)
        b2.fire_connected(True)
        if version is not None:
            b2.on_fire(B.orc_frame(0, version))
        osc_tracks(b2, tracks)
        b2.tick()
        o["daw"].clear()
        o["fire"].clear()
        return b2, o, clk

    # pressing the ALREADY-LIT bank pad toggles the BSP view follow / roam; the pad blinks while roaming
    lb, lo, lclk = osc_make(tracks=30)
    lights = []
    lb.send_banklight = lights.append
    lb.banklight_connected(True)
    lb.tick()
    pad1 = [0x90 | (CH - 1), 44, 100]                    # top-left = bank 1 = the bank on screen
    lb.on_bus(pad1)
    check("lit pad pressed: the BSP view toggles to independent (roam), no jump",
          lb.effective_mode("bsp") == "independent" and lb.view_first("bsp") == 0, lb.view_mode)
    lights.clear()
    seen = set()
    for _ in range(6):
        lclk.t += 0.3
        lb.tick()
    seen = {m[10] for m in lights if m[9] == 0x78}
    check("roaming: the lit pad blinks (ON and OFF both sent)", seen == {0x7F, 0}, lights)
    lb.on_bus([0x90 | (CH - 1), 45, 100])                # pad 2 = bank 2: a JUMP, not a toggle
    check("another pad still jumps (the roaming view moves to tracks 9-16)",
          lb.view_first("bsp") == 8 and lb.effective_mode("bsp") == "independent")
    lb.on_bus([0x90 | (CH - 1), 45, 100])                # pad 2 again = now the lit one
    lights.clear()
    lb.tick()
    lclk.t += 0.6
    lb.tick()
    check("lit pad pressed again: back to follow (shared), the pad steady ON",
          lb.effective_mode("bsp") == "shared" and all(m[10] == 0x7F for m in lights if m[9] == 0x79), lights)

    def vols(msgs):
        return [(m[0], m[1][0]) for m in msgs if m[0].endswith("/volume")]

    ob, oo, oclk = osc_make()
    check("OSC: the hello = bank size, bank 1, refresh action 41743",
          ob.dialect == "osc" and ob.osc and ob.surf.adapter.hello() ==
          [(0, ("/device/track/count", (16,))), (0, ("/device/track/bank/select", (1,))), (0, ("/action", (41743,)))])
    check("OSC: REAPER's state counts once a volume arrives", ob.daw_state_seen() and ob.n_tracks() == 12)
    check("OSC: every view starts shared on track 1",
          all(ob.view_first(v) == 0 and ob.effective_mode(v) == "shared" for v in B.SURFACES))
    ob.on_bus(cc(CH, 20, 127))
    check("OSC: CC 20 -> /track/1/volume 1.0 (with the touch first)",
          oo["daw"][0] == ("/track/1/volume/touch", (1,)) and vols(oo["daw"]) == [("/track/1/volume", 1.0)],
          oo["daw"])
    check("OSC: the Fire page shows tracks 1-4 by name", [r[2] for r in
          [ob.row_state(i) for i in range(4)]] == ["Trk1", "Trk2", "Trk3", "Trk4"])
    oo["daw"].clear()
    ob.on_fire(B.orc_frame(0x1E, 1, 2))                  # the Fire scrolls 2 tracks forward
    check("OSC shared: a Fire scroll moves EVERY shared view (fire / bsp / cc all on track 3); to REAPER only the "
          "held touch let go", [ob.view_first(v) for v in B.SURFACES] == [2, 2, 2]
          and oo["daw"] == [("/track/1/volume/touch", (0,))], oo["daw"])
    ob.on_bus(cc(CH, 20, 0))
    check("OSC shared: CC 20 now moves track 3", vols(oo["daw"]) == [("/track/3/volume", 0.0)], oo["daw"])
    oo["fire"].clear()
    ob.cc_with_page = True                               # (with the page up the CCs only go out if asked)
    ob.on_daw(("/track/4/volume", (0.5,)))
    check("OSC: REAPER's track 4 move reaches the Fire as CC 21 (slot 2 of the cc view)",
          oo["fire"] == [cc(CH, 21, B.cc7(0.5))], oo["fire"])
    ob.on_bus(cc(CH, 31, 127))                           # CC 31: the fire view goes independent
    check("CC 31 rising: the fire view independent, its window kept (track 3)",
          ob.view_mode["fire"] == "independent" and ob.view_first("fire") == 2)
    ob.on_fire(B.orc_frame(0x1E, 1, 5))
    check("OSC independent: the Fire scrolls on its own (track 3 -> 8), the cc + bsp views stay on 3",
          ob.view_first("fire") == 7 and ob.view_first("cc") == 2 and ob.view_first("bsp") == 2,
          [ob.view_first(v) for v in B.SURFACES])
    check("...the page's rows: tracks 8-11", ob.page_targets() == [7, 8, 9, 10] and ob.row_state(0)[2] == "Trk8")
    ob.on_fire(B.orc_frame(0x1E, 1, 2))
    check("...two more detents: the last three tracks + MASTER (the list's end)", ob.view_first("fire") == 9
          and ob.page_targets() == [9, 10, 11, B.MASTER])
    ob.on_fire(B.orc_frame(0x1E, 1, 3))
    check("...further detents: stays at the list's end", ob.view_first("fire") == 9)
    ob.on_bus(cc(CH, 33, 127))                           # the cc view independent too
    ob.on_bus(cc(CH, 30, 127))                           # bank right on the cc view
    check("OSC: CC 30 banks only the independent cc view (3 -> 11); bsp stays shared on 3",
          ob.view_first("cc") == 10 and ob.view_first("bsp") == 2 and ob.view_first("fire") == 9)
    ob.on_bus(cc(CH, 30, 0))
    ob.on_bus(cc(CH, 30, 127))
    check("OSC: bank right refused once offset + 8 >= the tracks", ob.view_first("cc") == 10)
    ob.on_bus(cc(CH, 31, 0))
    ob.on_bus(cc(CH, 31, 127))                           # fire back to shared
    check("toggle back to shared: the fire view snaps onto the shared window (track 3)",
          ob.view_mode["fire"] == "shared" and ob.view_first("fire") == 2)
    ob.on_bus([0x90 | (CH - 1), 37, 100])                # bank pad = bank 10 (tracks 73-80): past the end
    check("OSC: a bank pad past the last track = the last bank that holds one (bsp + fire shared -> track 9)",
          ob.view_first("bsp") == 8 and ob.view_first("fire") == 8 and ob.current_bank() == 1,
          [ob.view_first(v) for v in B.SURFACES])
    st = ob.status()
    check("status: dialect, views (mode / effective / first), tracks, bindings",
          st["dialect"] == "osc" and st["views"]["cc"] == {"mode": "independent", "effective": "independent",
                                                            "first": 10}
          and st["osc"]["tracks"] == 12 and len(st["strips"]) == 12 and any(b["num"] == 31 for b in st["bindings"]))
    check("status stays plain JSON", isinstance(json.loads(json.dumps(st)), dict))

    # the BSP on its own window (OSC)
    sout = []
    ob.send_surface = sout.append
    ob.surface_connected(True)
    sout.clear()
    ob.on_bus(cc(CH, 32, 127))                           # bsp independent (on track 9)
    ob.on_daw(("/track/9/mute", (1.0,)))
    check("OSC: REAPER mutes track 9 -> the BSP's mute light for its slot 1 (note 16)", sout == [[0x90, 16, 127]], sout)
    oo["daw"].clear()
    ob.on_surface([0x90, 16, 127])
    ob.on_surface([0x80, 16, 0])
    check("OSC: the BSP's mute step on slot 1 = /track/9/mute/toggle (once: the release sends nothing)",
          oo["daw"] == [("/track/9/mute/toggle", ())], oo["daw"])
    sout.clear()
    ob.on_bus([0x90 | (CH - 1), 44, 100])                # bank pad 1: the bsp view back to track 1
    check("OSC: the BSP's view moving repaints its lights (track 9's mute goes dark on slot 1)",
          [0x90, 16, 0] in sout and ob.view_first("bsp") == 0, sout)
    oo["daw"].clear()
    ob.on_surface([0xB0, 17, 3])                         # V-pot 2 three detents right
    check("OSC: a V-pot turn = an absolute pan from REAPER's last (centre + 3 %)",
          oo["daw"] == [("/track/2/pan", (0.53,))] or
          (oo["daw"][0][0] == "/track/2/pan" and abs(oo["daw"][0][1][0] - 0.53) < 1e-9), oo["daw"])

    # 0 dB: learned from REAPER, set exactly in dB
    ob2, oo2, _ = osc_make(tracks=4)
    ob2.on_daw(("/track/1/volume", (0.7,)))
    ob2.on_daw(("/track/1/volume/db", (0.0,)))
    check("OSC: REAPER's 0 dB position learned from a (volume, volume/db = 0) pair",
          ob2.unity_daw() == round(0.7 * 16383), ob2.unity_daw())
    ob2.tick()
    oo2["daw"].clear()
    kf = B.fire_tap_fine(cfg.DAWSURF_UNITY_COLUMN)
    ob2.on_fire(B.orc_frame(0x1D, 1, (kf >> 7) & 0x7F, kf & 0x7F))
    check("OSC: the Fire's unity column = /track/2/volume/db 0.0 exactly (no position guess)",
          [m for m in oo2["daw"] if m[0].endswith("/db")] == [("/track/2/volume/db", (0.0,))]
          and not vols(oo2["daw"]), oo2["daw"])
    check("OSC: the knee follows the learned 0 dB: REAPER at 0.7 shows on the Fire at the unity column",
          B.daw_to_fire(B.fine14(0.7), ob2.unity_daw()) == kf)

    # pages past DAWSURF_OSC_TRACKS
    ob3, oo3, _ = osc_make(tracks=16, strips=16)
    check("OSC: a full page (16 of 16) = maybe more: 17 tracks assumed", ob3.n_tracks() == 17)
    ob3.on_bus(cc(CH, 33, 127))
    ob3.on_bus(cc(CH, 30, 127)); ob3.on_bus(cc(CH, 30, 0))
    ob3.on_bus(cc(CH, 30, 127))                          # cc view 0 -> 8 -> 16 = page 2
    check("OSC: a view stepping past track 16 switches REAPER to bank 2 + refresh, the model is cleared",
          ("/device/track/bank/select", (2,)) in oo3["daw"] and ("/action", (41743,)) in oo3["daw"]
          and ob3.surf.adapter.page == 1 and ob3.surf.strips[0].name == "", oo3["daw"][-3:])
    oo3["daw"].clear()
    ob3.on_daw(("/track/1/number/str", ("17",)))
    ob3.on_bus(cc(CH, 20, 64))
    check("OSC page 2: slot 1 of the cc view = REAPER's /track/1 of bank 2 (track 17)",
          vols(oo3["daw"]) == [("/track/1/volume", 64 / 127.0)] and ob3.surf.adapter.track_number(0) == 17,
          oo3["daw"])

    # Mackie: a view CC changes nothing on the wire
    br, out = make()
    logs = []
    br.log = logs.append
    br.on_bus(cc(CH, 32, 127))
    check("Mackie: a view CC is remembered but every view stays on REAPER's one window (nothing sent)",
          br.view_mode["bsp"] == "independent" and br.effective_mode("bsp") == "shared" and out["daw"] == []
          and any("Mackie" in l for l in logs), (out["daw"], logs))

    # ==== 18. OscLink: the UDP side with a fake socket (no real socket is opened) ================
    class FakeSock:
        def __init__(self):
            import queue
            self.q, self.sent, self.bound, self.closed = queue.Queue(), [], None, False

        def setsockopt(self, *a):
            pass

        def bind(self, a):
            self.bound = a

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            import queue
            if self.closed:
                raise OSError("closed")
            try:
                return self.q.get(timeout=0.05)
            except queue.Empty:
                raise OSError("timed out")

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def close(self):
            self.closed = True

    import orc1_dawsurface_osc as OSCM
    import time as _time
    fs = FakeSock()
    got = []
    link = B.OscLink("127.0.0.1", 8100, 9100, got.append, log=lambda t: None, sock_factory=lambda: fs)
    check("OscLink: nothing opened until the first check", fs.bound is None and link.name is None)
    check("OscLink: first check = new, bound on the listen port", link.check() == "new" and fs.bound == ("0.0.0.0", 9100)
          and link.check() is True)
    link.write(("/track/1/volume", (0.25,)))
    check("OscLink: a write = one datagram to REAPER's port, OSC-encoded",
          fs.sent == [(OSCM.osc_message("/track/1/volume", (0.25,)), ("127.0.0.1", 8100))], fs.sent)
    fs.q.put((OSCM.osc_bundle([OSCM.osc_message("/track/2/name", ("Bass",)),
                               OSCM.osc_message("/play", (1.0,))]), ("127.0.0.1", 8100)))
    fs.q.put((OSCM.osc_message("/track/3/name", ("evil",)), ("10.9.9.9", 8100)))
    fs.q.put((b"garbage", ("127.0.0.1", 8100)))
    for _ in range(100):
        if link.dropped >= 2:
            break
        _time.sleep(0.01)
    check("OscLink: a bundle from REAPER arrives as its messages, in order; another host / garbage dropped",
          got == [("/track/2/name", ("Bass",)), ("/play", (1.0,))] and link.dropped == 2, (got, link.dropped))
    link.close()
    check("OscLink: close = socket closed, link down", fs.closed and link.name is None and link.sock is None)
    check("OscLink: no host configured = stays down, opens nothing",
          B.OscLink(None, 8100, 9100, got.append, sock_factory=lambda: 1 / 0).check() is False)

    # ---- 2026-11-09 (owner: "daw mode stuff really only needs to be going on when i have a daw connected") ----
    # With OSC the DAW 'port' is an address that is always there, so the Fire got hello + page + fader CCs on every
    # plug-in (a screen hiccup). Now: no DAW = not one message to the Fire.
    brN, outN, clkN, _ = make_page(version=None, daw=False)
    brN.tick()
    clkN.t += 30.0
    brN.tick()
    check("no DAW: a plugged-in Fire hears NOTHING from the bridge (no hello, page or fader CC), even later",
          outN["fire"] == [], outN["fire"])
    brN.daw_connected(True)
    check("DAW arrives: the Fire gets its hello then", (0, []) in frames(outN["fire"]), frames(outN["fire"]))
    brN.on_fire(B.orc_frame(0, 6))
    brN.tick()
    check("DAW arrives: the Fire answers 6 -> its page goes up",
          any(f[0] == 0x1A and f[1] and f[1][0] > 0 for f in frames(outN["fire"])), frames(outN["fire"]))
    outN["fire"].clear()
    brN.daw_connected(False)
    brN.tick()
    check("DAW goes: the Fire's DAW page empties (1A 0)", (0x1A, [0]) in frames(outN["fire"]), frames(outN["fire"]))
    brP, outP = make()
    brP.on_daw(pb(0, 16383))                          # REAPER told us a fader...
    brP.daw_connected(False)                          # ...then went away
    outP["fire"].clear()
    brP.set_channel(CH + 1)                           # a rebind re-pushes every known fader
    check("DAW gone: a rebind pushes no fader CCs to the Fire", ccs(outP["fire"]) == [], outP["fire"])

    PA = {k: FakePort() for k in ("bus", "daw_in", "daw_out", "fire", "fire_in")}
    outA = {"daw": [], "fire": []}
    brA = B.Bridge(lambda m: outA["daw"].append(m), lambda raw: outA["fire"].append(list(raw)), clock=Clock(),
                   log=lambda t: None)
    alive, logsA = {"v": False}, []
    rtA = B.Runtime(brA, PA, B.Binding(bpath), "orc1-dawsurf", log=logsA.append, daw_alive=lambda: alive["v"])
    PA["bus"].avail = "RtMidiOut Client:ORC-daw 133:0"
    PA["daw_in"].avail = PA["daw_out"].avail = "osc 192.168.1.50:8100 (listening on 9100)"
    PA["fire"].avail = PA["fire_in"].avail = "ORC FW seq:ORC FW seq MIDI 1 40:0"
    write_b({"4": "daw"}, 4000)
    rtA.check()
    check("runtime: the OSC address is there but REAPER is silent -> DAW down, the Fire untouched, said so",
          not brA.daw_up and outA["fire"] == [] and any("REAPER is not answering" in l for l in logsA), (logsA, outA))
    alive["v"] = True
    rtA.check()
    check("runtime: REAPER answers -> DAW up, hello to REAPER and to the Fire",
          brA.daw_up and outA["daw"] and (0, []) in frames(outA["fire"]), (outA["daw"][:2], frames(outA["fire"])))
    alive["v"] = False
    rtA.check()
    check("runtime: REAPER quits -> DAW down again", not brA.daw_up)

    brW, _, _, _ = make_page(version=3)               # an old Fire: no page, so colours are never wanted
    callsW, ansW = [], {"fail": False}

    def fetchW(url):
        callsW.append(url)
        if ansW["fail"]:
            raise OSError("connection refused")
        return text

    webW = B.ReaperWeb(brW, url="http://192.168.1.50:8080/", fetch=fetchW, log=lambda t: None)
    check("web: not the liveness probe + no page = no request, liveness unknown",
          webW.poll_once() is False and callsW == [] and webW.alive is None)
    webW.probe = True
    webW.poll_once()
    check("web probe: asks REAPER even with no page; it answers = alive", len(callsW) == 1 and webW.alive is True)
    ansW["fail"] = True
    webW.poll_once()
    webW.poll_once()
    still = webW.alive
    webW.poll_once()
    check("web probe: 2 misses = still alive (a blip), the 3rd = REAPER not running", still is True
          and webW.alive is False, (still, webW.alive))
    # ---- 2026-11-12: a Fire reboot under the SAME port name is a new port (its USB address changed) ----
    class _FakeRt:
        def __init__(s, *a, **k):
            s.ports, s.opened, s.closed, s.cb = [], 0, 0, None

        def get_ports(s):
            return list(s.ports)

        def open_port(s, i, label):
            s.opened += 1

        def close_port(s):
            s.closed += 1

        def ignore_types(s, **k):
            pass

        def cancel_callback(s):
            s.cb = None

        def set_callback(s, cb):
            s.cb = cb

    _fake_rtmidi = type(sys)("rtmidi")
    _fake_rtmidi.MidiIn = _fake_rtmidi.MidiOut = _FakeRt
    _saved_rtmidi = sys.modules.get("rtmidi")
    sys.modules["rtmidi"] = _fake_rtmidi
    try:
        FPORT = "ORC FW seq:ORC FW seq MIDI 1 32:0"
        usb = {"ORC FW seq": "001/007"}
        idf = lambda p: B.port_usb_ident(p, idents=lambda: dict(usb))    # noqa: E731
        po = B.Port("t-fire", "fire", B.is_fire_out, ident=idf)
        pi = B.Port("t-fire-in", "fire in", B.is_fire_out, callback=lambda *a: None, sysex=True, ident=idf)
        po.rt.ports = pi.rt.ports = [FPORT]
        first = (po.check(), pi.check())
        again = (po.check(), pi.check())
        check("Fire port: 'new' once, then just open (same name, same USB address)",
              first == ("new", "new") and again == (True, True) and po.rt.opened == 1, (first, again))
        usb["ORC FW seq"] = "001/008"            # the Fire rebooted between two checks: same name, same 32:0
        rb = (po.check(), pi.check())
        check("Fire rebooted (same port name, NEW USB address): both ports reopened as 'new'",
              rb == ("new", "new") and po.rt.closed == 1 and po.rt.opened == 2 and pi.rt.opened == 2, rb)
        check("the input's callback is set again after the reopen", pi.rt.cb is not None)
        plain = B.Port("t-plain", "plain", lambda p: True)
        plain.rt.ports = ["X:x 20:0"]
        check("a port without ident (DAW / bus / surfaces) behaves as before",
              plain.check() == "new" and plain.check() is True and plain.ident is None)
        check("port_usb_ident: card name before the first ':'; unknown card or a failing reader -> None",
              B.port_usb_ident(FPORT, idents=lambda: {"ORC FW seq": "9/9"}) == "9/9"
              and B.port_usb_ident("Other:x 1:0", idents=lambda: {}) is None
              and B.port_usb_ident(FPORT, idents=lambda: 1 / 0) is None)
        _src = open(os.path.join(HERE, "orc1_dawsurf_bridge.py"), encoding="utf-8").read()
        check("main(): both Fire ports are given the USB-address check",
              'ports["fire"] = Port(C + "-fire", "fire faders", is_fire_out, ident=port_usb_ident)' in _src
              and "callback=on_fire, sysex=True,\n                            ident=port_usb_ident)" in _src)
    finally:
        if _saved_rtmidi is not None:
            sys.modules["rtmidi"] = _saved_rtmidi
        else:
            sys.modules.pop("rtmidi", None)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

# ---- the real files were never touched --------------------------------------------------------
os.environ.pop("HOME", None)
for p, h in REAL_MD5.items():
    check("real file untouched: %s" % p, md5(p) == h, (h, md5(p)))
check("every status write went to the sandbox (never the live /dev/shm file)",
      STATUS_WRITES and all(os.path.abspath(p).startswith(os.path.abspath(SANDBOX)) for p in STATUS_WRITES),
      STATUS_WRITES)

print("%d passed, %d failed" % (PASS, FAIL))
if PASS + FAIL != EXPECTED:
    print("EXPECTED %d checks, ran %d: a check was added or removed" % (EXPECTED, PASS + FAIL))
    FAIL += 1
sys.exit(1 if FAIL else 0)
