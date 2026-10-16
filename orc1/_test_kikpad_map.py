#!/usr/bin/env python3
"""Regression suite for orc1_kikpad_map (the editor of the KIKPAD's own pad + encoder maps).

Pure stdlib, no MIDI hardware. FakePad speaks the firmware's editor protocol
(smartpad-kikpad mod_kikpad_remap.h: same commands, same clamps); the firmware itself
is tested by that repo's host harness. Run:  python3 _test_kikpad_map.py
"""
import ast
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_kikpad_map as M
import orc1_kikpad as K

EXPECTED = 82
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


class FakePad:
    """The firmware's side of the wire: maps in RAM, replies as the firmware does."""

    def __init__(self, link=None):
        self.link = link
        self.alive = True
        self.proto = 1
        self.sent = []
        self.factory()
        self.dirty = 0

    def factory(self):
        self.pads = [[[1, 36 + i % 40, 16, i % 64] for i in range(64)] for _b in range(2)]
        self.enc = [[[7 + m, e] for e in range(8)] for m in range(8)]
        self.bankch = [13, 0]

    def __call__(self, data):                       # the link's send()
        self.sent.append(list(data))
        if not self.alive:
            return
        assert list(data[:3]) == [0x7D, 0x4B, 0x50]
        assert all(0 <= b <= 127 for b in data), "a data byte over 127 would break the SysEx"
        r = self.reply(data[3], list(data[4:]))
        if r is not None:
            self.link.on_sysex([0x7D, 0x4B, 0x50] + r)

    def reply(self, cmd, a):
        if cmd == 0x01:
            return [0x01, self.proto, 2, 8, 64]
        if cmd == 0x02 and len(a) == 2 and a[0] < 2 and a[1] < 64:
            return [0x02, a[0], a[1]] + self.pads[a[0]][a[1]]
        if cmd == 0x03 and len(a) == 6 and a[0] < 2 and a[1] < 64:
            t = 0 if a[2] > 3 else a[2]
            self.pads[a[0]][a[1]] = [t, a[3] & 127, min(a[4], 16), a[5] & 63]
            self.dirty = 1
            return [0x02, a[0], a[1]] + self.pads[a[0]][a[1]]
        if cmd == 0x04 and len(a) == 2:
            return [0x04, a[0], a[1]] + self.enc[a[0]][a[1]]
        if cmd == 0x05 and len(a) == 4:
            self.enc[a[0]][a[1]] = [a[2] & 127, a[3] & 15]
            self.dirty = 1
            return [0x04, a[0], a[1]] + self.enc[a[0]][a[1]]
        if cmd == 0x06 and len(a) == 1:
            return [0x06, a[0], self.bankch[a[0]]]
        if cmd == 0x07 and len(a) == 2:
            self.bankch[a[0]] = a[1] & 15
            self.dirty = 1
            return [0x06, a[0], self.bankch[a[0]]]
        if cmd == 0x08 and len(a) == 1:
            self.factory()
            return [0x08, a[0]]
        if cmd == 0x09:
            return [0x09, self.dirty]
        return None


tmp = tempfile.mkdtemp(prefix="kpmap_")
SF = os.path.join(tmp, "map.json")


def new_link(**kw):
    pad = FakePad()
    link = M.KikpadMapLink(pad, state_file=SF, timeout=0.05, start=False, log=lambda m: None, **kw)
    pad.link = link
    return pad, link


# ---- wire helpers -----------------------------------------------------------------------
check("body: ours", M.body([0x7D, 0x4B, 0x50, 0x02, 1]) == [0x02, 1])
check("body: somebody else's SysEx", M.body([0x7E, 0x00, 0x06, 0x02]) is None)
check("body: too short", M.body([0x7D, 0x4B, 0x50]) is None)
check("colour black", M.colour_rgb(0) == "#000000")
check("colour WHITE (7) = the 2/3 bits", M.colour_rgb(7) == "#aaaaaa")
check("colour 63 = full white", M.colour_rgb(63) == "#ffffff")
check("colour YELLOW (54) = full yellow", M.colour_rgb(54) == "#ffff00")
check("colour LT_GREEN (50)", M.colour_rgb(50) == "#55ff00")
check("colour ORANGE (52)", M.colour_rgb(52) == "#ff5500")
check("colour BLUE (1) = the B bit is bit 0", M.colour_rgb(1) == "#0000aa")

# ---- web_command: everything the endpoint can send --------------------------------------
a, p = M.web_command({"op": "pad", "bank": 1, "pad": 63, "type": "toggle", "number": 80, "channel": 16, "colour": 33})
check("pad: action", a == "KIKPAD_MAP_PAD")
check("pad: channel 16 (1-based) -> 15 on the wire, type name -> index",
      p == {"bank": 1, "pad": 63, "type": 3, "number": 80, "channel": 15, "colour": 33}, p)
a, p = M.web_command({"op": "pad", "bank": 0, "pad": 0, "type": "note", "number": 36, "channel": "bank", "colour": 0})
check("pad: 'bank' channel -> 16 (follow the bank)", p["channel"] == 16)
for bad, why in [({"op": "pad", "bank": 2, "pad": 0, "type": "note"}, "bank 2"),
                 ({"op": "pad", "bank": 0, "pad": 64, "type": "note"}, "pad 64"),
                 ({"op": "pad", "bank": 0, "pad": 0, "type": "chord"}, "unknown type"),
                 ({"op": "pad", "bank": 0, "pad": 0, "type": "cc", "channel": 0}, "channel 0"),
                 ({"op": "pad", "bank": 0, "pad": 0, "type": "cc", "channel": 17}, "channel 17"),
                 ({"op": "pad", "bank": 0, "pad": 0, "type": "cc", "number": 128}, "number 128"),
                 ({"op": "pad", "bank": 0, "pad": 0, "type": "cc", "colour": 64}, "colour 64"),
                 ({"op": "pad", "bank": "x", "pad": 0, "type": "cc"}, "bank not a number"),
                 ({"op": "encoder", "mode": 8, "encoder": 0, "cc": 1, "channel": 1}, "mode 8"),
                 ({"op": "encoder", "mode": 0, "encoder": 8, "cc": 1, "channel": 1}, "encoder 8"),
                 ({"op": "bank_channel", "bank": 0, "channel": 0}, "bank channel 0"),
                 ({"op": "reset", "what": "bank3"}, "reset bank 3 (not mappable)"),
                 ({"op": "explode"}, "unknown op"),
                 ({}, "no op"),
                 ([1, 2], "not an object")]:
    try:
        M.web_command(bad)
        check(f"refused: {why}", False)
    except ValueError:
        check(f"refused: {why}", True)
check("encoder", M.web_command({"op": "encoder", "mode": 4, "encoder": 2, "cc": 21, "channel": 14})
      == ("KIKPAD_MAP_ENC", {"mode": 4, "encoder": 2, "cc": 21, "channel": 13}))
check("bank channel", M.web_command({"op": "bank_channel", "bank": 1, "channel": 7})
      == ("KIKPAD_MAP_BANKCH", {"bank": 1, "channel": 6}))
check("reset codes", [M.web_command({"op": "reset", "what": w})[1]["what"] for w in ("bank0", "bank1", "encoders", "all")]
      == [0, 1, 2, 127])
check("read", M.web_command({"op": "read"}) == ("KIKPAD_MAP_READ", {}))
check("the page's job id rides along", M.web_command({"op": "read", "job": "w1-2"})[1] == {"job": "w1-2"})
check("an oversized job id is dropped", "job" not in M.web_command({"op": "read", "job": "x" * 41})[1])

# ---- the link against the fake pad --------------------------------------------------------
pad, link = new_link()
link.read_all()
mp = link.state["map"]
check("read_all: supported", link.state["supported"] is True)
check("read_all: every pad read", all(x is not None for b in mp["banks"] for x in b["pads"]))
check("read_all: every encoder read", all(x is not None for m in mp["encoders"] for x in m))
check("read_all: pad format", mp["banks"][0]["pads"][5] == {"type": "note", "number": 41, "channel": "bank",
                                                          "colour": 5, "rgb": M.colour_rgb(5)}, mp["banks"][0]["pads"][5])
check("read_all: bank channels 1-based (14 and 1)", mp["banks"][0]["channel"] == 14 and mp["banks"][1]["channel"] == 1)
check("read_all: encoder format, channel 1-based", mp["encoders"][3][2] == {"cc": 10, "channel": 3})
check("read_all: asks the save state", link.state["dirty"] is False)
check("read_all: 1 hello + 2 bank + 128 pad + 64 encoder + 1 status requests", len(pad.sent) == 196, len(pad.sent))

pad.sent.clear()
link.set_pad(1, 10, 2, 74, 9, 33)
check("set_pad: one request", len(pad.sent) == 1 and pad.sent[0] == [0x7D, 0x4B, 0x50, 0x03, 1, 10, 2, 74, 9, 33])
check("set_pad: marks the pad as saving (it saves ~2 s later; never 'saved' straight after an edit)", link.state["dirty"] is True)
check("set_pad: stores what the PAD replied", mp["banks"][1]["pads"][10]["type"] == "cc" and mp["banks"][1]["pads"][10]["channel"] == 10)
link.set_pad(0, 0, 9, 200, 40, 99)                # the pad clamps; we must show its answer, not our request
q = mp["banks"][0]["pads"][0]
check("set_pad: the pad's clamps win (type 9 -> off, 200 -> 72, ch 40 -> bank, colour 99 -> 35)",
      q["type"] == "off" and q["number"] == 72 and q["channel"] == "bank" and q["colour"] == 35, q)
link.set_encoder(7, 7, 33, 15)
check("set_encoder", mp["encoders"][7][7] == {"cc": 33, "channel": 16})
link.set_bank_channel(0, 3)
check("set_bank_channel", mp["banks"][0]["channel"] == 4)
check("poll_dirty after edits", link.poll_dirty() is True)
link.reset(127)
check("reset: everything read back (the factory values)", mp is not link.state["map"]
      and link.state["map"]["banks"][0]["channel"] == 14 and link.state["map"]["encoders"][7][7] == {"cc": 14, "channel": 8})

# no answer / old firmware
pad, link = new_link()
pad.alive = False
try:
    link.read_all()
    check("a silent pad: read_all raises", False)
except RuntimeError as e:
    check("a silent pad: read_all raises", "hello" in str(e))
check("a silent pad: marked unsupported", link.state["supported"] is False)
pad, link = new_link()
pad.proto = 0
check("hello refuses protocol 0", link.hello() is False)
pad, link = new_link()
try:
    pad.alive = False
    link.set_pad(0, 0, 1, 1, 1, 1)
    check("a set with no reply raises (never reported as done)", False)
except RuntimeError:
    check("a set with no reply raises (never reported as done)", True)

# stale / foreign replies never satisfy a request
pad, link = new_link()
link.on_sysex([0x7D, 0x4B, 0x50, 0x02, 0, 5, 1, 99, 16, 1])        # a late reply from an earlier request
pad.alive = False
check("a stale reply is drained, not taken as the answer", link._ask(0x02, [0, 5], [0x02, 0, 5]) is None)
pad.alive = True
real = pad.reply
pad.reply = lambda c, a: [0x02, a[0], (a[1] + 1) % 64, 1, 1, 1, 1] if c == 0x02 else real(c, a)
check("a reply for another pad does not count", link._ask(0x02, [0, 5], [0x02, 0, 5]) is None)
pad.reply = real
link.on_sysex([0x7E, 0x7F, 0x06, 0x02])                             # an identity reply: not ours
check("foreign SysEx is ignored", link._replies.empty())

# jobs, the state file, connect / disconnect
pad, link = new_link()
link.run_job("read", link.read_all, "w7")
st = json.load(open(SF))
check("state file written", st["supported"] is True and st["busy"] is False)
check("last job echoed with ok", st["last"] == {"op": "read", "ok": True, "job": "w7"})
pad.alive = False
link.run_job("pad 0/1", lambda: link.set_pad(0, 1, 1, 1, 1, 1), "w8")
st = json.load(open(SF))
check("a failed job: last.ok false + its error, busy cleared", st["last"]["ok"] is False and st["last"]["job"] == "w8"
      and "confirm" in st["last"]["error"] and st["busy"] is False and st["error"])
link.connected(False)
st = json.load(open(SF))
check("disconnect: connected false, map cleared", st["connected"] is False and st["map"]["banks"][0]["pads"][0] is None)
pad.alive = True
link.connected(True)
check("connect queues a full read", not link._jobs.empty() and link._jobs.get()[0] == "read")
check("the state names the encoder modes", link.state["modes"][0] == "VOLUME" and len(link.state["modes"]) == 8)

# ---- the surface hands SysEx to the editor, and sends it ------------------------------------
class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class Out:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


class Engine:
    pass


s = K.KikpadSurface(Engine(), home=tmp, n_sets=1, log=lambda m: None)
got = []
s.sysex_hook = got.append
s.handle(Msg("sysex", data=(0x7D, 0x4B, 0x50, 0x01, 1, 2, 8, 64)))
check("surface: SysEx goes to the editor hook", got == [[0x7D, 0x4B, 0x50, 0x01, 1, 2, 8, 64]])
try:
    s.send_sysex([1, 2])
    check("surface: send_sysex without the port raises", False)
except RuntimeError:
    check("surface: send_sysex without the port raises", True)
s.out, s._msg = Out(), Msg
s.send_sysex([0x7D, 0x4B, 0x50, 0x09])
check("surface: send_sysex sends a sysex message", len(s.out.sent) == 1 and s.out.sent[0].type == "sysex"
      and list(s.out.sent[0].data) == [0x7D, 0x4B, 0x50, 0x09])
s.sysex_hook = None
s.handle(Msg("sysex", data=(1, 2, 3)))
check("surface: no hook = SysEx ignored, no crash", True)

# ---- the brain and the web are wired to it ------------------------------------------------
brain = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
check("brain: the hook is wired", "kikpad.sysex_hook = kikpad_map.on_sysex" in brain)
check("brain: the link sends through the surface", "KikpadMapLink(kikpad.send_sysex)" in brain)
check("brain: attach tells the link (read on connect)", "kikpad_map.connected(bool(attached))" in brain)
check("brain: a lost pad tells the link", "kikpad_map.connected(False)" in brain)
for act in ("KIKPAD_MAP_READ", "KIKPAD_MAP_PAD", "KIKPAD_MAP_ENC", "KIKPAD_MAP_BANKCH", "KIKPAD_MAP_RESET"):
    check(f"brain handles {act}", f'action == "{act}"' in brain)
check("brain: every map command passes the page's job", brain.count(", job)") >= 5 and 'cmd.get("job")' in brain)
web = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
check("web: the POST validates through web_command", "_km.web_command(" in web)
check("web: the GET reads the link's state file", "_km.STATE_FILE" in web)
check("web: the page exists", os.path.exists(os.path.join(HERE, "templates", "kikpad.html"))
      and os.path.exists(os.path.join(HERE, "static", "js", "kikpad.js")))
check("nav: KIK entry", 'href="/kikpad"' in open(os.path.join(HERE, "templates", "base.html"), encoding="utf-8").read())
health = open(os.path.join(HERE, "orc1_health.py"), encoding="utf-8").read()
check("health: /kikpad in WEB_PAGES", '"/kikpad"' in health)

if passed + failed != EXPECTED:
    print(f"COUNT DRIFT: ran {passed + failed}, EXPECTED {EXPECTED}")
    failed += 1
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
