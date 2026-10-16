#!/usr/bin/env python3
"""Regression suite for orc1_kikpad_link (the ORC -> KIKPAD key sender).

Pure stdlib, no MIDI hardware: `mido` is replaced by a fake for the loop tests, and
the web call is stubbed. Run:  python3 _test_kikpad_link.py
"""
import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_kikpad_link as L

EXPECTED = 33
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


# ---- the scale list must be the ORC's own, in order ------------------------------------
src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
groups = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCALE_GROUPS" for t in node.targets):
        groups = ast.literal_eval(ast.get_source_segment(src, node.value))
orc_order = [s for _label, g in groups for s in g]
check("SCALE_ORDER equals orc1.SCALE_ORDER", L.SCALE_ORDER == orc_order)
check("25 scales", len(L.SCALE_ORDER) == 25)

# ---- scale_index -----------------------------------------------------------------------
check("MAJ is index 0", L.scale_index("MAJ") == 0)
check("last scale index", L.scale_index("DBLH") == 24)
check("case-insensitive", L.scale_index("min5") == L.SCALE_ORDER.index("MIN5"))
check("unknown scale -> None", L.scale_index("NOPE") is None)
check("None scale -> None", L.scale_index(None) is None)

# ---- key_from_state --------------------------------------------------------------------
check("normal state", L.key_from_state({"root": 7, "scale": "DOR"}) == (7, 1))
check("root wraps", L.key_from_state({"root": 12, "scale": "MAJ"}) == (0, 0))
check("missing root", L.key_from_state({"scale": "MAJ"}) is None)
check("garbage root", L.key_from_state({"root": "x", "scale": "MAJ"}) is None)
check("unknown scale -> unusable", L.key_from_state({"root": 0, "scale": "ZZZ"}) is None)

# ---- messages ---------------------------------------------------------------------------
m = L.messages_for(9, 5)
check("two messages", len(m) == 2)
check("no channel 16 any more (port :1 is a private wire; channel byte 0)", all(c == 0 for c, _cc, _v in m))
check("CC10 root / CC11 scale", m == [(0, 10, 9), (0, 11, 5)])

# ---- port finding -----------------------------------------------------------------------
names = ["Midi Through:Midi Through Port-0 14:0", "pisound:pisound MIDI 36:0", "KIKPAD:KIKPAD IN 40:0", "KIKPAD:KIKPAD IN 40:1"]
check("picks the KIKPAD's HOST port (:1), never its music port", L.find_pad_port(names) == "KIKPAD:KIKPAD IN 40:1")
check("none when absent", L.find_pad_port(names[:2]) is None)
check("case-insensitive prefix", L.find_pad_port(["kikpad:x 1:1"]) == "kikpad:x 1:1" and L.find_pad_port(["kikpad:x 1:0"]) is None)


# ---- the loop, with a fake mido ---------------------------------------------------------
class FakeOut:
    def __init__(self, sink):
        self.sink = sink

    def send(self, msg):
        self.sink.append((msg.channel, msg.control, msg.value))

    def close(self):
        pass


def run_once(port_names, state):
    sink = []
    fake = types.ModuleType("mido")
    fake.get_output_names = lambda: port_names
    fake.open_output = lambda name: FakeOut(sink)
    fake.Message = lambda kind, channel, control, value: types.SimpleNamespace(channel=channel, control=control, value=value)
    sys.modules["mido"] = fake
    real_fetch = L.fetch_state
    L.fetch_state = (lambda: state) if state is not None else (lambda: (_ for _ in ()).throw(OSError("down")))
    try:
        code = L.run(once=True)
    finally:
        L.fetch_state = real_fetch
        del sys.modules["mido"]
    return code, sink


code, sink = run_once(names, {"root": 2, "scale": "MIN", "scales": L.SCALE_ORDER})
check("--once sends the key and exits 0", code == 0 and sink == [(0, 10, 2), (0, 11, 5)], f"{code} {sink}")
code, sink = run_once(names[:2], {"root": 2, "scale": "MIN"})
check("--once without a pad exits 1, sends nothing", code == 1 and sink == [])
code, sink = run_once(names, {"root": 2, "scale": "ZZZ"})
check("--once with an unknown scale exits 1, sends nothing", code == 1 and sink == [])
code, sink = run_once(names, None)
check("--once with the web process down exits 1, sends nothing", code == 1 and sink == [])

# ---- the pad reboots: its ALSA port dies and comes back (same name!) ---------------------
check("port_gone: still listed", L.port_gone("KIKPAD:KIKPAD IN 40:0", names) is False)
check("port_gone: vanished", L.port_gone("KIKPAD:KIKPAD IN 40:0", names[:2]) is True)
check("port_gone: nothing open", L.port_gone(None, names) is True)
check("heartbeat boundary", L.heartbeat_due(10.0, 0.0) and not L.heartbeat_due(9.9, 0.0))


def run_loops(names_for_loop, state, clock_step, loops):
    """Drive the real loop for `loops` iterations with a fake mido and clock.
    Returns (messages sent, ports opened, handles closed)."""
    sink, ctr = [], {"open": 0, "closed": 0, "loop": 1}

    class Out:
        def send(self, msg):
            sink.append((msg.channel, msg.control, msg.value))

        def close(self):
            ctr["closed"] += 1

    fake = types.ModuleType("mido")
    fake.get_output_names = lambda: names_for_loop(ctr["loop"])

    def open_output(name):
        ctr["open"] += 1
        return Out()

    fake.open_output = open_output
    fake.Message = lambda kind, channel, control, value: types.SimpleNamespace(channel=channel, control=control, value=value)
    sys.modules["mido"] = fake
    t = {"now": 0.0}
    real_fetch = L.fetch_state
    L.fetch_state = lambda: state

    def clock():
        return t["now"]

    def sleep(_s):
        t["now"] += clock_step
        ctr["loop"] += 1

    try:
        L.run(max_loops=loops, clock=clock, sleep=sleep)
    finally:
        L.fetch_state = real_fetch
        del sys.modules["mido"]
    return sink, ctr["open"], ctr["closed"]


st = {"root": 2, "scale": "MIN", "scales": L.SCALE_ORDER}
sink, opens, closed = run_loops(lambda i: names, st, 11.0, 3)
check("heartbeat reopens the port every time (3 opens)", opens == 3, f"opens={opens}")
check("heartbeat resends the key each time (6 messages)", len(sink) == 6, f"sent={len(sink)}")
check("old handles are closed, not leaked", closed == 2, f"closed={closed}")

sink, opens, closed = run_loops(lambda i: names if i != 2 else names[:2], st, 3.0, 3)
check("pad vanishes then returns: handle dropped and reopened (2 opens)", opens == 2, f"opens={opens}")
check("key resent after the pad returns (4 messages)", len(sink) == 4, f"sent={len(sink)}")
check("dead handle closed", closed >= 1, f"closed={closed}")

sink, opens, closed = run_loops(lambda i: names, st, 1.0, 5)
check("no spam between heartbeats (1 open, 2 messages in 5 s)", opens == 1 and len(sink) == 2, f"opens={opens} sent={len(sink)}")

print(f"{passed} passed, {failed} failed")
if passed + failed != EXPECTED:
    print(f"COUNT DRIFT: ran {passed + failed}, EXPECTED {EXPECTED}")
    failed += 1
sys.exit(1 if failed else 0)
