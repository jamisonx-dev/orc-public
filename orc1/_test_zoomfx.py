#!/usr/bin/env python3
"""Regression suite for orc1_zoomfx (the Zoom MultiStomp as the "zoomfx" module).

Pure stdlib + a fake mido: no pedal, no MIDI. The patch fixture is a REAL edit-buffer dump
from owner's MS-70CDR (patch 44 "Basic CN", 2026-10-13). Run:  python3 _test_zoomfx.py
"""
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_zoomfx as Z

EXPECTED = 70
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


DUMP = bytes.fromhex(
    "f05200612842410000620010010000000000000000050000000000010048020718012840060000000000000000"
    "040059000044001c000032000000000000000000000041020c0050281c00183000050000000000001000000100"
    "002c03487000180011400600000000000000411001002c0828003200404006000000000000000040580b420061"
    "73696320434e00202000f7")
PACKED = list(DUMP[5:-1])

# ---- 7-in-8 packing ------------------------------------------------------------------------
u = Z.unpack7(PACKED)
check("dump unpacks to 122 bytes", len(u) == Z.PATCH_LEN, len(u))
check("pack7 round-trips the real dump byte for byte", Z.pack7(u) == PACKED)
check("pack7 of 7 high bytes", Z.pack7(bytes([0x80] * 7)) == [0x7F] + [0] * 7)
check("unpack7 restores bit 7", Z.unpack7([0x40, 0x01]) == bytes([0x81]))

# ---- patch parse (values measured on the pedal) -------------------------------------------
p = Z.parse_patch(u)
check("name", p["name"] == "Basic CN", p["name"])
check("tempo 90", p["tempo"] == 90, p["tempo"])
ids = [f["id"] for f in p["fx"]]
check("six effect IDs", ids == [0x01000060, 0x014000C0, 0x0200002C, 0x08000120, 0x06000040, 0x060000A0], [hex(i) for i in ids])
check("on/off states", [f["on"] for f in p["fx"]] == [1, 0, 1, 1, 1, 1])
check("SmoothDly TIME (13-bit P0) = 322", p["fx"][3]["params"][0] == 322)
check("TheVibe P0-P5", p["fx"][4]["params"][:6] == [25, 60, 48, 24, 1, 100])
check("Fuzz Smile P0-P3", p["fx"][1]["params"][:4] == [60, 70, 81, 100])

# ---- the parameter table shipped with the module ------------------------------------------
params = Z.load_params()
check("table loads (>200 effects)", len(params) > 200, len(params))
check("SmoothDly TIME max 3014 (thammer agrees)", params[0x08000120][1][0] == ("TIME", 3014))
check("Bottom B Bass max 10 (the pedal clamped 30 -> 10)", params[0x0200002C][1][0] == ("Bass", 10))
check("imported B1on Fuzz Smile is in the table", params[0x014000C0][0] == "BaFzSmile")
check("missing file -> empty table, no crash", Z.load_params("/nonexistent/x.json") == {})

# ---- CC -> action ------------------------------------------------------------------------
A = lambda cc, v, patch=p: Z.cc_action(cc, v, patch, params)
check("CC20 >= 64 = slot 1 on", A(20, 127) == ("edit", 0, 0, 1))
check("CC20 < 64 = slot 1 off", A(20, 63) == ("edit", 0, 0, 0))
check("CC51 = slot 4 knob 1 (TIME) full = 3014", A(51, 127) == ("edit", 3, 2, 3014))
check("CC51 0 = 0", A(51, 0) == ("edit", 3, 2, 0))
check("CC51 64 = middle of 0-3014", A(51, 64) == ("edit", 3, 2, (64 * 3014 + 63) // 127))
check("CC41 = slot 3 knob 1 scaled to 0-10", A(41, 127) == ("edit", 2, 2, 10))
check("CC73 = slot 5 knob 3 (Bias 0-100)", A(73, 127) == ("edit", 4, 4, 100))
check("CC80 = slot 6 on/off", A(80, 0) == ("edit", 5, 0, 0))
check("CC89 on a 5-knob effect = no such knob", A(89, 64) is None)
check("CC 60-69 are never a slot", all(A(c, 64) is None for c in range(60, 70)))
check("CC7 (the /matrix LVL slider) does nothing", A(7, 100) is None)
check("CC19 / CC90-92 edges", A(19, 1) is None and A(90, 127) == ("tuner", 74, 127))
check("CC91 = the pedal's CC75", A(91, 0) == ("tuner", 75, 0))
check("CC92 high = store, low = nothing", A(92, 127) == ("store",) and A(92, 10) is None)
empty = json.loads(json.dumps(p)); empty["fx"][5]["id"] = 0
check("edit aimed at an EMPTY slot is dropped", A(85, 64, empty) is None and A(80, 127, empty) is None)
check("no patch read yet = drop", A(21, 64, None) is None)
unknown = json.loads(json.dumps(p)); unknown["fx"][0]["id"] = 0x7FFFFFF
check("effect without a table -> raw value", A(22, 99, unknown) == ("edit", 0, 3, 99))
check("4-slot pedal (MS-60B) ignores slots 5-6", Z.cc_action(71, 64, p, params, slots=4) is None)

# ---- messages -------------------------------------------------------------------------------
check("edit message 14-bit", Z.edit_msg(0x61, 3, 2, 3014) == [0xF0, 0x52, 0x00, 0x61, 0x31, 3, 2, 3014 & 0x7F, 3014 >> 7, 0xF7])
check("sx frame", Z.sx(0x61, 0x50) == [0xF0, 0x52, 0x00, 0x61, 0x50, 0xF7])


# ---- the link with a fake mido -----------------------------------------------------------
class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)

    @staticmethod
    def from_bytes(b):
        m = Msg("raw")
        m.raw = list(b)
        return m


class FakePort:
    def __init__(self, sink=None, cb=None):
        self.sink, self.cb = sink, cb

    def send(self, m):
        self.sink.append(m.raw if m.type == "raw" else ("sysex", list(m.data)))

    def close(self):
        pass


def fake_mido(names, sink, reply_identity=True):
    f = types.ModuleType("mido")
    f.get_output_names = lambda: list(names)
    f.get_input_names = lambda: list(names)
    f.Message = lambda t, **kw: Msg(t, **kw)
    f.Message.from_bytes = Msg.from_bytes
    holder = {}

    def oin(name, callback=None):
        holder["cb"] = callback
        return FakePort(cb=callback)

    def oout(name):
        port = FakePort(sink)
        real_send = port.send

        def send(m):
            real_send(m)
            if reply_identity and m.type == "sysex" and list(m.data) == [0x7E, 0x00, 0x06, 0x01]:
                holder["cb"](Msg("sysex", data=[0x7E, 0x00, 0x06, 0x02, 0x52, 0x61, 0, 0, 0, 0x32, 0x2E, 0x30, 0x30]))
        port.send = send
        return port
    f.open_input, f.open_output = oin, oout
    return f, holder


Z.SEND_GAP_S = 0
NAMES = ["Midi Through:Midi Through Port-0 14:0", "ZOOM MS Series:ZOOM MS Series MIDI 1 40:0"]
sink = []
fm, holder = fake_mido(NAMES, sink)
link = Z.ZoomLink(fm, params)
check("connects to the port found by NAME", link.connect() and link.port_name == NAMES[1])
check("identity -> MS-70CDR, 6 slots", link.dev == 0x61 and link.model == "MS-70CDR" and link.slots == 6)
check("on connect: editor mode on, ask program, ask patch",
      [0xF0, 0x52, 0x00, 0x61, 0x50, 0xF7] in sink and [0xF0, 0x52, 0x00, 0x61, 0x33, 0xF7] in sink
      and [0xF0, 0x52, 0x00, 0x61, 0x29, 0xF7] in sink)
holder["cb"](Msg("sysex", data=list(DUMP[1:-1])))
check("a 28 dump from the pedal becomes the live patch", link.patch and link.patch["name"] == "Basic CN")
holder["cb"](Msg("program_change", program=43))
check("the pedal's program change is tracked", link.program == 43)

sink.clear()
for v in range(0, 128, 8):                 # a fast sweep of TheVibe Depth (CC72)
    link.handle_cc(72, v)
link.handle_cc(72, 127)
link.flush()
edits = [m for m in sink if m[:5] == [0xF0, 0x52, 0x00, 0x61, 0x31]]
check("a sweep is coalesced to ONE edit (the newest)", len(edits) == 1 and edits[0][5:9] == [4, 3, 100, 0], edits)
check("the model follows our edit", link.patch["fx"][4]["params"][1] == 100)

sink.clear()
link.handle_cc(51, 127); link.handle_cc(20, 0); link.flush()
check("two different knobs = two edits", len([m for m in sink if m[4] == 0x31]) == 2)
check("slot-1 off reached the model", link.patch["fx"][0]["on"] == 0)

sink.clear()
link.handle_cc(92, 127); link.handle_cc(92, 127); link.flush()
check("STORE fires once per rise, to the current program",
      sink == [[0xF0, 0x52, 0x00, 0x61, 0x32, 0x01, 0, 0, 43, 0, 0, 0, 0, 0, 0xF7]], sink)
sink.clear(); link.handle_cc(92, 0); link.handle_cc(92, 100); link.flush()
check("STORE re-arms after going low", len(sink) == 1)

sink.clear(); link.handle_cc(90, 127); link.flush()
check("tuner CC90 -> the pedal's CC74", sink == [[0xB0, 74, 127]])

sink.clear(); link.handle_program(7); link.flush()
check("program change -> pedal PC, then a patch re-read is scheduled", sink == [[0xC0, 7]] and link._want_patch > 0)
link._want_patch = 1e-9; link.flush()
check("...and the patch is re-read", sink[-1] == [0xF0, 0x52, 0x00, 0x61, 0x29, 0xF7])
sink.clear(); link.handle_program(50); link.handle_program(-1); link.flush()
check("program out of 0-49 is ignored", sink == [])

holder["cb"](Msg("sysex", data=[0x52, 0x00, 0x61, 0x31, 3, 2, 0x10, 0x01]))
check("the pedal's own knob turn (31) updates the model", link.patch["fx"][3]["params"][0] == 0x10 | 0x01 << 7)
holder["cb"](Msg("sysex", data=[0x52, 0x00, 0x61, 0x31, 1, 0, 1, 0]))
check("the pedal's own on/off updates the model", link.patch["fx"][1]["on"] == 1)
holder["cb"](Msg("sysex", data=[0x52, 0x00, 0x58, 0x31, 1, 0, 0, 0]))
check("another pedal's id is ignored", link.patch["fx"][1]["on"] == 1)

st = link.state(1)
check("state: device/channel/patch", st["connected"] and st["device"] == "MS-70CDR" and st["channel"] == 1 and st["patch"] == "Basic CN")
check("state: named knobs with ranges", st["fx"][3]["name"] == "SmoothDly" and st["fx"][3]["params"][0]["max"] == 3014)
check("state is JSON-serialisable", bool(json.dumps(st)))

names_now = list(NAMES)
fm2, h2 = fake_mido(names_now, [])
l2 = Z.ZoomLink(fm2, params); l2.connect()
h2["cb"](Msg("sysex", data=list(DUMP[1:-1])))
check("second link read its patch (so the drop test is not vacuous)", l2.patch is not None)
names_now.pop()
fm2.get_output_names = lambda: list(names_now)
check("a replug/unplug is noticed", not l2.still_there())
l2.drop()
check("drop clears device + patch (a stale handle drops silently)", l2.dev is None and l2.patch is None and l2.out is None)
check("not connected -> CCs are ignored, no crash", l2.handle_cc(21, 64) is None and l2.pending == {})

fm3, _ = fake_mido(NAMES[:1], [])
check("no pedal plugged in -> connect() is False", Z.ZoomLink(fm3, params).connect() is False)

# ---- the binding --------------------------------------------------------------------------
tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"bindings": {"10": "drums", "1": "zoomfx"}}, tmp); tmp.close()
clock = [0.0]
b = Z.Binding(tmp.name, clock=lambda: clock[0])
check("bound channel 1 -> 0-based 0", b.channel() == 0)
json.dump({"bindings": {"10": "drums", "4": "zoomfx"}}, open(tmp.name, "w"))
check("not re-read inside the 2 s window", b.channel() == 0)
clock[0] = 2.5
check("a /matrix move applies live", b.channel() == 3)
json.dump({"bindings": {"10": "drums"}}, open(tmp.name, "w")); clock[0] = 5.0
check("unbound -> None", b.channel() is None)


class Rec:
    def __init__(self):
        self.ccs, self.pcs = [], []

    def handle_cc(self, c, v):
        self.ccs.append((c, v))

    def handle_program(self, p):
        self.pcs.append(p)


json.dump({"bindings": {"1": "zoomfx"}}, open(tmp.name, "w")); clock[0] = 9.0
r = Rec()
Z.on_bus(Msg("control_change", channel=0, control=21, value=5), b, r)
Z.on_bus(Msg("control_change", channel=1, control=21, value=5), b, r)
Z.on_bus(Msg("program_change", channel=0, program=3), b, r)
Z.on_bus(Msg("note_on", channel=0, note=60, velocity=99), b, r)
check("only the bound channel's CCs + PCs reach the pedal", r.ccs == [(21, 5)] and r.pcs == [3])
os.unlink(tmp.name)

# ---- the brain must leave the pedal alone -------------------------------------------------
import orc1_config
check("brain patchbay list names the pedal", "zoom ms" in [x.lower() for x in orc1_config.PATCHBAY_PORT_PREFIXES])
check("zoomfx is a MODULE_KEYS entry, appended last", orc1_config.MODULE_KEYS[-1] == "zoomfx")
mjs = open(os.path.join(HERE, "static", "js", "matrix.js"), encoding="utf-8").read()
check("/matrix lists zoomfx", "key: 'zoomfx'" in mjs)

print(f"{passed} passed, {failed} failed")
if passed + failed != EXPECTED:
    print(f"COUNT DRIFT: ran {passed + failed}, EXPECTED {EXPECTED}")
    sys.exit(1)
sys.exit(1 if failed else 0)
