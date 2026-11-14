"""Guard (2026-11-09): the synth bus carries ONLY synthesis; non-synth modules get their messages from the ORC on
their own port. owner: "anything can be a module. i just said don't put it on the synth. we have a brain for
information" ... "don't ever move anything that doesn't need an internal synthesis onto the synth again."

orc1.py cannot be imported (it runs as __main__ and opens hardware), so like the chord suites this pulls the pieces
out of its source with ast and runs them against stubs: _SynthBusSplit (the one choke point every synth-bus write
goes through), _rebuild_synth_chs (keeps the channel -> ORC module map), _synth_bus_guard_once (keeps rtpmidid off
the bus AND the ORC ports). Pure stdlib + orc1_config; runs on the PC or the Pi:  python3 _test_orc_ports.py
"""
import ast, os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_config as cfg                                    # noqa: E402

EXPECTED = 16
passed = failed = 0


def check(cond, label, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", label, extra)


SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def seg(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            return ast.get_source_segment(SRC, node)
    raise KeyError(name)


# ---- the config: which modules the ORC feeds itself, and the exact-name rule ----
check(set(cfg.ORC_MODULES) == {"visuals", "zoomfx", "mxvbox", "daw"} and set(cfg.ORC_MODULES) <= set(cfg.MODULE_KEYS),
      "ORC_MODULES = the four non-synth modules, all real module keys", cfg.ORC_MODULES)
check([cfg.orc_port_name(m) for m in ("visuals", "daw")] == ["ORC-visuals", "ORC-daw"], "ports are 'ORC-<module>'")
check(cfg.is_orc_port("RtMidiOut Client:ORC-daw 140:0", "daw")
      and not cfg.is_orc_port("orc1-dawsurf-fire:fire faders 142:0", "daw")
      and not cfg.is_orc_port("RtMidiOut Client:ORC-daw2 141:0", "daw")
      and not cfg.is_orc_port("RtMidiOut Client:ORC1-Synth-Bus 133:0", "daw")
      and not cfg.is_orc_port("orc-visuals-in:RtMidi input 151:0", "visuals")
      and not cfg.is_orc_port("RtMidiOut Client:ORC-visuals 140:0", "daw"),
      "is_orc_port: the WHOLE port name, case-sensitive; never a look-alike / the bridge's or renderer's own clients")
check(cfg.port_part("RtMidiOut Client:ORC-zoomfx 131:0") == "ORC-zoomfx" and cfg.port_part("") == "",
      "port_part strips the client and the numbers")

# ---- the splitter ----
ns = {}
exec(seg("_SynthBusSplit"), {"_ORC_MOD_BY_CH": {}}, ns)
Split = ns["_SynthBusSplit"]


class Out:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)

    def close(self):
        pass


def msg(ch=None, kind="control_change"):
    m = types.SimpleNamespace(type=kind)
    if ch is not None:
        m.channel = ch - 1                     # mido channels are 0-based
    return m


bus, vis, daw = Out(), Out(), Out()
by_ch = {16: "visuals", 4: "daw", 11: "mxvbox"}
sp = Split(bus, {"visuals": vis, "daw": daw}, by_ch)
sp.send(msg(16)); sp.send(msg(4)); sp.send(msg(10)); sp.send(msg(None, "sysex"))
check(len(vis.sent) == 1 and vis.sent[0].channel == 15, "a message on the visuals channel goes to ORC-visuals")
check(len(daw.sent) == 1, "a message on the daw channel goes to ORC-daw")
check(len(bus.sent) == 2 and {getattr(m, "channel", None) for m in bus.sent} == {9, None},
      "a synth channel (drums, 10) and a channel-less message go to the synth bus, as before")
sp.send(msg(11))
check(len(bus.sent) == 2, "an ORC module whose port is missing (mxvbox here): DROPPED, never onto the synth bus")
by_ch.clear()
sp.send(msg(16))
check(len(bus.sent) == 3 and len(vis.sent) == 1, "unbound again: that channel is ordinary (synth bus) once more")

# ---- _rebuild_synth_chs keeps the channel -> ORC module map, IN PLACE (the splitter holds the dict) ----
g = {"cfg": cfg, "_bindings": {"drums": 10, "visuals": 16, "daw": 4, "poly": 6}, "_SYNTH_CHS": {},
     "_ORC_MOD_BY_CH": {99: "stale"}}
held = g["_ORC_MOD_BY_CH"]
exec(seg("_rebuild_synth_chs"), g)
g["_rebuild_synth_chs"]()
check(g["_ORC_MOD_BY_CH"] is held and held == {16: "visuals", 4: "daw"},
      "_rebuild_synth_chs: ORC modules only, synth modules left out, same dict object", held)
check(set(g["_SYNTH_CHS"]) == {10, 16, 4, 6}, "the brain still FORWARDS every bound channel (then the splitter decides)")

# ---- every synth-bus write goes through the splitter ----
import re as _re
_assigns = _re.findall(r"^\s*_synth_bus = (.*?)\s*(?:#.*)?$", SRC, _re.M)
check(sorted(_assigns) == ["None", "None", "_SynthBusSplit(bus, orc_ports)"],
      "the only bus object the brain writes to is the splitter (else None: start-up / failure)", _assigns)
check('port_outs["synth"] = _synth_bus' in SRC, "routing rules naming 'synth' go through the splitter too")
check("_synth_bus.send(msg)" in seg("_scene_send_cc") and "_synth_bus.send(msg)" in seg("send_synth_midi"),
      "scene recalls and send_synth_midi write through _synth_bus (= the splitter)")
check("virtual=True" in seg("_open_synth_bus") and "cfg.orc_port_name(m)" in seg("_open_synth_bus"),
      "_open_synth_bus opens one virtual ORC port per ORC module")

# ---- the rtpmidid guard covers the bus AND the ORC ports ----
ACONNECT = """client 131: 'RtMidiOut Client' [type=user,pid=1]
    0 'ORC1-Synth-Bus  '
\tConnecting To: 132:0, 140:0
client 133: 'RtMidiOut Client' [type=user,pid=1]
    0 'ORC-visuals     '
\tConnecting To: 132:0, 141:0
client 134: 'RtMidiOut Client' [type=user,pid=1]
    0 'ORC-visuals2    '
\tConnecting To: 132:0
client 132: 'rtpmidid' [type=user,pid=2]
    0 'Network         '
client 140: 'orc1-poly' [type=user,pid=3]
    0 'in              '
client 141: 'orc-visuals-ctl' [type=user,pid=4]
    0 'in              '
"""
calls = []
fake_sp = types.SimpleNamespace(check_output=lambda *a, **k: ACONNECT,
                                run=lambda args, **k: calls.append(args))
gg = {"cfg": cfg, "print": lambda *a, **k: None}
exec(seg("_synth_bus_guard_once"), gg)
real_sp = sys.modules.get("subprocess")
sys.modules["subprocess"] = fake_sp
try:
    gg["_synth_bus_guard_once"]()
finally:
    sys.modules["subprocess"] = real_sp
pairs = sorted((a[2], a[3]) for a in calls)
check(pairs == [("131:0", "132:0"), ("133:0", "132:0")],
      "rtpmidid is cut from the synth bus AND from ORC-visuals; real readers and look-alike names left alone", pairs)

print(f"_test_orc_ports: {passed} passed, {failed} failed (expected {EXPECTED})")
if passed + failed != EXPECTED:
    print(f"COUNT DRIFT: ran {passed + failed}, EXPECTED {EXPECTED}")
    sys.exit(1)
sys.exit(1 if failed else 0)
