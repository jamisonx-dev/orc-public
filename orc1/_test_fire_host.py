#!/usr/bin/env python3
"""
ORC mode on our Fire firmware (orc1_orcmode.py) + its hooks in the brain, the Fire handler
and the screen sender (2026-11-11, fw v0.33).

    python3 -B _test_fire_host.py          (PC or Pi; stdlib only)

Covers:
  1. the frames: which SysEx is ours (21 / 22 / 23 from our Fire only; hello, bars, pages and
     other devices pass on), a press / release / turn -> exactly the note / CC message the Fire
     handler was written for (Akai's numbers), the knob-touch note, presses before the handler is up;
  2. on / off: the wake-up points the handler and the screen at the shared link and repaints; off
     lets go of everything still held (through the handler), blanks the hooks, stops the pad colours;
  3. the keep-alive (once a second, only while on), a Fire that goes away, request() refusals;
  4. HostOut: SysEx out through the link, nothing else, never raises;
  5. the wiring, from the source: orc1.py hands 21-23 to fire_host BEFORE fire_pages and FireBarLink,
     polls it, builds it on fire_bar_link (no port of its own); orc1_fire / fire_oled honour HOST_OUT;
  6. a CROSS-CHECK AGAINST THE FIRMWARE when its source is here (hostm.c / ctl.c): the button notes
     the C sends are the ORC's BUTTON_NOTE table, the command bytes match. Source: $FIRE_HOSTM_SRC,
     else ../../fire_firmware_hm/fw/fireseq, ../../fire_firmware/fw/fireseq, ~/fire_fw/scratch_hm/fireseq.
     Missing = SKIPPED lines, never fewer checks.

SAFETY: nothing here opens a MIDI port or imports the brain; the Fire is never touched.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import orc1_orcmode as H      # noqa: E402

EXPECTED = 45
passed = failed = skipped = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label} {extra}")


def skip(label, why):
    global passed, skipped
    passed += 1
    skipped += 1
    print(f"  [SKIPPED: {why}] {label}")


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)

    def key(self):
        return (self.type, getattr(self, "note", getattr(self, "control", None)),
                getattr(self, "velocity", getattr(self, "value", None)), getattr(self, "channel", None))


class FakeMido:
    Message = Msg


class FakeFire:
    def __init__(self):
        self.got, self.rewakes, self._out, self.boom = [], 0, "stale", False

    def handle_msg(self, msg):
        if self.boom:
            raise RuntimeError("handler bug")
        self.got.append(msg.key())

    def rewake(self):
        self.rewakes += 1


class Mod:
    HOST_OUT = None


class Rig:
    def __init__(self, version=9, connected=True, fire=None):
        self.sent, self.logs, self.now = [], [], 100.0
        self.version, self.connected, self.fire, self.made = version, connected, fire, 0
        self.fire_mod, self.oled_mod = Mod(), Mod()
        self.send_fails = False
        self.host = H.FireHost(FakeMido, send=self.send, link_version=lambda: (self.connected, self.version if self.connected else None),
                               is_ours=lambda n: "orc fw seq" in (n or "").lower(), get_fire=lambda: self.fire,
                               make_fire=self.make, fire_mod=self.fire_mod, oled_mod=self.oled_mod,
                               log=self.logs.append, clock=lambda: self.now, run_async=lambda fn: fn())

    def send(self, data):
        if self.send_fails:
            raise RuntimeError("no ORC FW seq Fire connected")
        self.sent.append(list(data))

    def make(self):
        self.made += 1
        self.fire = FakeFire()
        return self.fire

    def rx(self, *payload, source="ORC FW seq:ORC FW seq MIDI 1 36:0"):
        return self.host.handle_sysex(H.ORC_HDR + list(payload), source)


# ---- 1. the frames ----
r = Rig()
check("not ours: hello (00), bars (0F), a page reply (13), a surface pad (0C) are left for the other handlers",
      not r.rx(0x00, 8) and not r.rx(0x0F, 1, 127, 0) and not r.rx(0x13, 0, 0) and not r.rx(0x0C, 5, 100))
check("not ours: the same frames from another device, another maker's header, a frame too short",
      not r.rx(0x21, 1, source="BeatStep Pro") and not r.host.handle_sysex([0x47, 0x7F, 0x43, 0x21, 1], "ORC FW seq")
      and not r.host.handle_sysex(H.ORC_HDR + [0x21], "ORC FW seq") and not r.host.on)
check("a press before ORC mode is on is consumed and given to nobody", r.rx(0x22, 60, 100) and r.made == 0 and r.fire is None)

check("21 01 from the Fire switches ORC mode on", r.rx(0x21, 1) and r.host.on)
check("... the wake-up builds the handler once and repaints it, after telling the Fire which curve to use (24 20)",
      r.made == 1 and r.fire.rewakes == 1 and r.sent[0] == H.ORC_HDR + [0x24, 20])
check("... and points the pad colours and the screen at the shared link (one object for both)",
      r.fire_mod.HOST_OUT is r.host.out and r.oled_mod.HOST_OUT is r.host.out)
r.rx(0x21, 1)
check("a second 21 01 changes nothing (no second wake-up)", r.made == 1 and r.fire.rewakes == 1)

f = r.fire
r.rx(0x22, 54, 100); r.rx(0x22, 54, 0)
check("a pad: 22 <note> <vel> -> note_on with that note + velocity on channel 0, vel 0 -> note_off",
      f.got == [("note_on", 54, 100, 0), ("note_off", 54, 0, 0)], str(f.got))
f.got.clear()
r.rx(0x22, 48, 127); r.rx(0x22, 33, 127); r.rx(0x22, 33, 0); r.rx(0x22, 48, 0)
check("buttons arrive as Akai's notes in the order pressed (SHIFT down, BROWSER, up, up)",
      [g[:3] for g in f.got] == [("note_on", 48, 127), ("note_on", 33, 127), ("note_off", 33, 0), ("note_off", 48, 0)])
f.got.clear()
r.rx(0x23, 2, 1)
check("a knob turn: the stock Fire's touch note first, then the relative CC (knob 2 = note 18, CC 18)",
      f.got == [("note_on", 18, 127, 0), ("control_change", 18, 1, 0)], str(f.got))
f.got.clear(); r.now += 0.1
r.rx(0x23, 2, 127)
check("a turn right after: only the CC (still the same touch), down = 127", f.got == [("control_change", 18, 127, 0)])
f.got.clear(); r.now += 0.5
r.rx(0x23, 2, 3); r.rx(0x23, 0, 1)
check("after a pause the next turn is a new touch; each knob has its own",
      f.got == [("note_on", 18, 127, 0), ("control_change", 18, 3, 0), ("note_on", 16, 127, 0), ("control_change", 16, 1, 0)], str(f.got))
f.got.clear()
r.rx(0x23, 4, 65)
check("SELECT (k = 4) is CC 118, no touch note", f.got == [("control_change", 118, 65, 0)])
f.got.clear()
check("a knob number that does not exist (5-127) is consumed and dropped", r.rx(0x23, 9, 1) and f.got == [])
n0 = r.host.inputs
f.boom = True
ok = r.rx(0x22, 60, 100)
f.boom = False
check("a bug in the handler is logged, never raised into the MIDI input thread",
      ok and any("input" in s for s in r.logs) and r.host.inputs == n0)

# ---- 2. off ----
f.got.clear(); r.host._down.clear()
r.rx(0x22, 70, 100); r.rx(0x22, 71, 100); r.rx(0x22, 48, 127); r.rx(0x22, 71, 0)
f.got.clear()
check("21 00 switches ORC mode off", r.rx(0x21, 0) and not r.host.on)
check("... everything still held is let go through the handler (pad 70 and SHIFT; 71 was already up)",
      f.got == [("note_off", 48, 0, 0), ("note_off", 70, 0, 0)], str(f.got))
check("... the pad colours stop (handler output cleared, hook cleared) and the screen goes to a sink",
      f._out is None and r.fire_mod.HOST_OUT is None and isinstance(r.oled_mod.HOST_OUT, H.NullOut))
f.got.clear()
check("presses after off are consumed and dropped", r.rx(0x22, 60, 100) and f.got == [])
r.rx(0x21, 1)
check("on again: the SAME handler is repainted, not rebuilt; nothing is remembered as held",
      r.made == 1 and f.rewakes == 2 and r.fire_mod.HOST_OUT is r.host.out and not r.host._down)

# ---- 3. keep-alive, a Fire that goes away, request ----
r.sent.clear()
r.host.poll(); r.host.poll(); r.now += 0.5; r.host.poll(); r.now += 0.6; r.host.poll()
check("while on: 'still here' (21 02) at once, then once a second, however often poll runs",
      r.sent == [H.ORC_HDR + [0x21, 2]] * 2, str(r.sent))
r.send_fails = True; r.now += 2
try:
    r.host.poll(); ok = True
except Exception:
    ok = False
r.send_fails = False
check("a failed keep-alive send never raises", ok)
r.connected = False
r.host.poll()
check("the Fire unplugged: ORC mode is off here too, said once", not r.host.on and sum("gone" in s for s in r.logs) == 1)
r.connected = True; r.sent.clear(); r.now += 5; r.host.poll()
check("while off: no keep-alive", r.sent == [])

q = Rig(connected=False)
check("request: no Fire -> refused in words, nothing sent", q.host.request(True) == (False, "no Fire with our firmware connected (FLAME seq / ORC FW seq)") and q.sent == [])
q = Rig(version=None)
check("request: hello not answered yet -> refused", q.host.request(True)[0] is False and "hello" in q.host.request(True)[1] and q.sent == [])
q = Rig(version=8)
ok, why = q.host.request(True)
check("request: firmware version 8 (v0.33: ORC mode but only 22 brightness steps) -> refused, nothing sent", not ok and "v0.34" in why and q.sent == [])
q = Rig(version=9)
check("request(True) sends 21 01, request(False) 21 00; the state only changes when the Fire answers",
      q.host.request(True) == (True, "") and q.host.request(False) == (True, "")
      and q.sent == [H.ORC_HDR + [0x21, 1], H.ORC_HDR + [0x21, 0]] and not q.host.on)
n_sent = len(q.sent)
check("curve: 10-30 is kept HERE (nothing sent to the Fire for it), anything else refused, the status reports it",
      q.host.set_curve(22)[0] and q.host.out.curve == 22 and len(q.sent) == n_sent and not q.host.set_curve(9)[0]
      and not q.host.set_curve(31)[0] and not q.host.set_curve("x")[0] and q.host.out.curve == 22 and q.host.status()["curve"] == 22)
s = H.socket_cmd(q.host, {"action": "FIRE_HOST_STATUS"})
check("FIRE_HOST_STATUS says on / connected / version / capable",
      s["ok"] and s["on"] is False and s["connected"] and s["version"] == 9 and s["capable"] is True
      and H.socket_cmd(Rig(version=8).host, {"action": "FIRE_HOST_STATUS"})["capable"] is False)
s = H.socket_cmd(Rig(version=8).host, {"action": "FIRE_HOST", "on": True})
check("FIRE_HOST on an old firmware: ok false with the reason", s["ok"] is False and "v0.34" in s["error"])

# ---- 4. HostOut ----
got = []
o = H.HostOut(got.append)
oled = (0x47, 0x7F, 0x43, 0x0E, 0, 6, 0, 7, 0, 127, 0x55, 0x2A)
o.send(Msg("sysex", data=oled))
o.send(Msg("note_on", note=1, velocity=1, channel=0))
check("HostOut: a screen SysEx goes out byte for byte as a list, anything that is not SysEx is dropped",
      got == [list(oled)] and o.sent == 1 and o.failed == 0)

# ---- 4b. pad colours: the Fire squares (fw v0.34), the ORC only bends ----
frame = [0x47, 0x7F, 0x43, 0x65, 0, 8, 5, 3, 6, 10, 63, 0x7F, 0, 20]
check("the default curve (20) sends the handler's colours untouched: the Fire's own squared curve is the look",
      H.CURVE_DEFAULT == 20 and H.FW_CURVE == 20 and H.remap_pads(frame) == frame
      and all(H.pad_value(c) == c for c in range(128)))
check("curve 10 = brightness straight in line with the value: what is sent, squared by the Fire, comes back to c / 127",
      all(abs((H.pad_value(c, 10) / 127.0) ** 2 - c / 127.0) < 0.012 for c in range(128)))
check("every curve 10-30: 0 stays 0, 127 stays 127, brighter in is never dimmer out, always 0-127",
      all(H.pad_value(0, k) == 0 and H.pad_value(127, k) == 127 for k in range(10, 31))
      and all(0 <= H.pad_value(c, k) <= H.pad_value(c + 1, k) <= 127 for k in range(10, 31) for c in range(127)))
check("lower lifts the dim colours, higher pushes them down (dim 20: curve 10 > 20 > 30)",
      H.pad_value(20, 10) > H.pad_value(20, 15) > H.pad_value(20, 20) == 20 > H.pad_value(20, 25) > H.pad_value(20, 30) >= 1)
got.clear()
o.curve = 10
o.send(Msg("sysex", data=tuple(frame)))
check("a pad-colour SysEx leaves with each colour bent by the curve; pad numbers, header and length untouched",
      got == [frame[:6] + [5] + [H.pad_value(x, 10) for x in (3, 6, 10)] + [63] + [H.pad_value(x, 10) for x in (0x7F, 0, 20)]]
      and got[0][7:10] != [3, 6, 10], str(got))
check("... a pad frame cut short keeps its odd tail, and other makers' / other commands' frames are not touched",
      H.remap_pads([0x47, 0x7F, 0x43, 0x65, 0, 6, 1, 127, 0, 0, 9, 9], 10) == [0x47, 0x7F, 0x43, 0x65, 0, 6, 1, 127, 0, 0, 9, 9]
      and H.remap_pads([0x00, 0x20, 0x29, 0x65, 0, 4, 1, 3, 6, 10], 10) == [0x00, 0x20, 0x29, 0x65, 0, 4, 1, 3, 6, 10]
      and H.remap_pads(H.ORC_HDR + [0x21, 2], 10) == H.ORC_HDR + [0x21, 2])

# ---- 5. the wiring ----
B = src("orc1.py")
i_host, i_pages, i_bar = (B.find("fire_host.handle_sysex(msg.data, source_name)"), B.find("fire_pages.handle_sysex(msg.data, source_name)"),
                          B.find("fire_bar_link.handle_sysex(msg.data, source_name)"))
check("orc1.py: 21-23 reach fire_host BEFORE fire_pages and FireBarLink (which would swallow them)", 0 < i_host < i_pages < i_bar)
m = re.search(r"fire_host = _orc1_orcmode\.FireHost\((.*?)\n\n", B, re.S)
check("orc1.py: fire_host is built on fire_bar_link's send / version / name rule and the brain's own handler (no port of its own)",
      bool(m) and "send=fire_bar_link.host_send" in m.group(1) and "link_version=fire_bar_link.link_version" in m.group(1)
      and "is_ours=fire_bar_link.is_ours" in m.group(1) and "make_fire=_fire_host_make" in m.group(1)
      and "open_output" not in src("orc1_orcmode.py").split('"""', 2)[2])
check("orc1.py: the scenes loop polls fire_host every pass, and FIRE_HOST* go to socket_cmd",
      "            scenes.poll()\n            fire_host.poll()" in B
      and 'if action in ("FIRE_HOST", "FIRE_HOST_STATUS", "FIRE_HOST_CURVE"):\n        r = _orc1_orcmode.socket_cmd(fire_host, cmd)' in B)
FS, OS_ = src("orc1_fire.py"), src("fire_oled.py")
oo = FS[FS.find("    def _open_output(self):"):FS.find("    def _send_pads(self, cells):")]
si = OS_[OS_.find("def send_image("):OS_.find("# Text rendering")]
check("orc1_fire._open_output uses HOST_OUT before it looks for a port; fire_oled.send_image sends to HOST_OUT without opening one",
      0 < oo.find("if HOST_OUT is not None:\n            self._out = HOST_OUT\n            return") < oo.find("mido.get_output_names()")
      and 0 < si.find("if HOST_OUT is not None:\n        name, out = _HOST_NAME, HOST_OUT") < si.find("find_fire_output()")
      and "\nHOST_OUT = None\n" in FS and "\nHOST_OUT = None\n" in OS_)
ns = {}
exec(re.search(r"^DIAL_CC\s*=.*?$", FS, re.M).group(0) + "\n" + re.search(r"^SELECT_CC\s*=.*?$", FS, re.M).group(0)
     + "\n" + re.search(r"^DIAL_TOUCH_NOTE\s*=.*?$", FS, re.M).group(0), ns)
check("the knob / SELECT numbers here are the handler's own (DIAL_CC 16-19 -> knob 0-3, touch notes 16-19, SELECT 118)",
      ns["DIAL_CC"] == {H.DIAL_CC_BASE + k: k for k in range(4)} and ns["SELECT_CC"] == H.SELECT_CC
      and ns["DIAL_TOUCH_NOTE"] == {H.DIAL_CC_BASE + k for k in range(4)})

# ---- 6. against the firmware source ----
cands = [os.environ.get("FIRE_HOSTM_SRC"), os.path.join(HERE, "..", "..", "fire_firmware_hm", "fw", "fireseq"),
         os.path.join(HERE, "..", "..", "fire_firmware", "fw", "fireseq"), os.path.expanduser("~/fire_fw/scratch_hm/fireseq"),
         os.path.expanduser("~/fire_fw/fw/fireseq")]
fw = next((c for c in cands if c and os.path.isfile(os.path.join(c, "hostm.c"))), None)
L1 = "firmware: every button note hostm.c sends is the ORC's BUTTON_NOTE for that button, and pads start at PAD_NOTE_BASE"
L2 = "firmware: ctl.c parses 21 <0-2> and 24 <10-30>, main.c sends 22 / 23, HOST_VERSION is this module's"
if not fw:
    skip(L1, "no firmware source with hostm.c here")
    skip(L2, "no firmware source with hostm.c here")
else:
    def fsrc(n):
        with open(os.path.join(fw, n), encoding="utf-8") as fh:
            return fh.read().replace("\r\n", "\n")
    hc, cc, mc = fsrc("hostm.c"), fsrc("ctl.c"), fsrc("main.c")
    cnotes = {k: int(v) for k, v in re.findall(r"\[IN_(\w+) - 64\] = (\d+)", hc)}
    exec(re.search(r"^BUTTON_NOTE = \{.*?^\}", FS, re.M | re.S).group(0) + "\n" + re.search(r"^PAD_NOTE_BASE\s*=.*?$", FS, re.M).group(0), ns)
    names = {"STEP": "STEP", "NOTE": "NOTE", "DRUM": "DRUM", "PERFORM": "PERFORM", "SHIFT": "SHIFT", "ALT": "ALT", "PATSONG": "PAT_SONG",
             "PLAY": "PLAY", "STOP": "STOP", "REC": "REC", "PAT_UP": "PATTERN_UP", "PAT_DN": "PATTERN_DOWN", "BROWSER": "BROWSER",
             "GRID_L": "GRID_LEFT", "GRID_R": "GRID_RIGHT", "SELECT": "SELECT_PRESS", "KNOBMODE": "MODE",
             "ROW1": "MUTE1", "ROW2": "MUTE2", "ROW3": "MUTE3", "ROW4": "MUTE4"}
    check(L1, len(cnotes) == 21 and set(cnotes) == set(names) and all(ns["BUTTON_NOTE"][names[k]] == v for k, v in cnotes.items())
          and f"note = {ns['PAD_NOTE_BASE']} + pad_of_input(idx)" in hc, str(cnotes))
    check(L2, "case 0x21: if (n == 2 && p[1] <= 2) { h.cmd = HC_HOSTM;" in cc and "case 0x24: if (n == 2 && p[1] >= 10 && p[1] <= 30) { h.cmd = HC_GAMMA;" in cc
          and "hostm_send(0x22, m[0], m[1]);" in mc and "hostm_send(0x23, 4, hm_rel(d));" in mc
          and f"#define HOST_VERSION {H.HOST_VERSION} " in mc and (H.CMD_MODE, H.MSG_INPUT, H.MSG_TURN, H.CMD_CURVE) == (0x21, 0x22, 0x23, 0x24))

print(f"\n{passed} passed, {failed} failed" + (f"  ({skipped} SKIPPED)" if skipped else ""))
if passed + failed != EXPECTED:
    print(f"EXPECTED {EXPECTED} checks, ran {passed + failed}: a check was added or removed")
    failed += 1
sys.exit(1 if failed else 0)
