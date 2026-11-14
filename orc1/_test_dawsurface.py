#!/usr/bin/env python3
"""
The ORC AS a DAW control surface: the neutral model (orc1_dawsurface), the MCU
dialect (orc1_dawsurface_mcu) and the surface-side half of orc1_mcu.

    python3 _test_dawsurface.py [-v]        (PC: py _test_dawsurface.py)

Pure stdlib: no MIDI port, no file, no service. Messages are the plain dicts
orc1_mcu builds (decode_host() reads those as well as mido messages); one
check converts through mido when it is installed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_mcu as M                                   # noqa: E402
import orc1_dawsurface as D                            # noqa: E402
from orc1_dawsurface_mcu import McuAdapter             # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 117


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


class Obj(object):
    """A mido-like attribute message, for M.decode (which reads attributes)."""
    def __init__(self, d):
        self.__dict__.update(d)
        self.channel = d.get("channel", 0)


def cc(control, value, ch=0):
    return {"type": "control_change", "channel": ch, "control": control, "value": value}


def note(n, vel, ch=0):
    return {"type": "note_on", "channel": ch, "note": n, "velocity": vel}


def make(devices=2, **kw):
    sent = []
    s = D.DawSurface(McuAdapter(devices=devices, **kw), send=lambda p, m: sent.append((p, m)))
    evs = []
    s.subscribe(evs.append)
    return s, sent, evs


# ---- 1. orc1_mcu: what the surface sends ---------------------------------------------
print("1. surface-side encoders")
check("V-pot 3 +3 = CC 18 value 3", M.vpot_turn(2, 3) == [cc(18, 3)], M.vpot_turn(2, 3))
check("V-pot 1 -1 = 0x41", M.vpot_turn(0, -1) == [cc(16, 0x41)])
check("V-pot 0 detents = no message", M.vpot_turn(0, 0) == [])
check("+63 / -63 fit one message (0x3F / 0x7F)",
      M.vpot_turn(0, 63) == [cc(16, 0x3F)] and M.vpot_turn(0, -63) == [cc(16, 0x7F)])
check("+100 splits into 63 + 37", [m["value"] for m in M.vpot_turn(0, 100)] == [63, 37])
check("-64 splits into -63 -1 (never 0x40 = 'minus zero')",
      [m["value"] for m in M.vpot_turn(0, -64)] == [0x7F, 0x41])
bad = [d for d in range(-200, 201)
       if sum(M.decode(Obj(m))[2] for m in M.vpot_turn(5, d)) != d
       or any(m["value"] in (0, 0x40) for m in M.vpot_turn(5, d))]
check("every delta -200..200 round-trips through the reader", not bad, bad[:5])
check("V-pot strip 8 refused", raises(lambda: M.vpot_turn(8, 1), ValueError))
check("jog -3 = CC 60 0x43", M.jog_turn(-3) == [cc(60, 0x43)])
check("jog round-trips", M.decode(Obj(M.jog_turn(7)[0])) == ("jog", 7))
check("play press / release = note 94 vel 127 / 0",
      M.button("play", True) == note(94, 127) and M.button("play", False) == note(94, 0))
check("mute strip 5 = note 20", M.button("mute", True, strip=4)["note"] == 20)
check("strip button out of range refused", raises(lambda: M.button("mute", True, strip=8), ValueError))
check("fader touch 4 = note 107, master touch = note 112",
      M.fader_touch(3, True) == note(107, 127) and M.fader_touch(8, False) == note(112, 0))
bad = [n for n in M.BUTTONS.values() if M.decode(Obj(M.button(n, True))) != ("button", n, None, True)]
check("every named button round-trips through the reader", not bad, bad)
check("button names are unique and never shadow a strip button",
      len(M.BUTTON_NOTES) == len(M.BUTTONS)
      and not set(M.BUTTON_NOTES) & set(n for n, _b in M._STRIP_BUTTONS))
check("REAPER's bank / channel / flip / global / transport notes",
      [M.BUTTON_NOTES[n] for n in ("bank_left", "bank_right", "channel_left", "channel_right",
                                   "flip", "global_view", "rewind", "fast_fwd", "stop", "play",
                                   "record", "cycle")]
      == [0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x56])
check("fader clamps 14-bit both ends",
      M.fader(0, 20000)["pitch"] == 8191 and M.fader(0, -5)["pitch"] == -8192
      and M.fader(0, 8192)["pitch"] == 0)

# ---- 2. orc1_mcu: what a host sends ------------------------------------------------------
print("2. decode_host")
H = M.decode_host
check("pitch bend ch 9 = master fader", H({"type": "pitchwheel", "channel": 8, "pitch": 8191})
      == ("fader", 8, 16383))
check("pitch bend ch 10 is not MCU", H({"type": "pitchwheel", "channel": 9, "pitch": 0}) is None)
check("ring CC 50: pos 6, wrap, centre", H(cc(50, 0x40 | 0x20 | 6)) == ("ring", 2, 6, 2, True))
r = M.vpot_ring(7, 127, M.RING_SPREAD)
check("vpot_ring encode -> decode", H(r) == ("ring", 7, 11, 3, False), H(r))
check("solo LED velocity 1 = BLINK (REAPER's soloed track)",
      H(note(8 + 3, 1)) == ("led", "solo", 3, M.LED_BLINK))
check("LED 127 on, 0 off, note_off off",
      H(note(16, 127)) == ("led", "mute", 0, M.LED_ON) and H(note(16, 0))[3] == M.LED_OFF
      and H({"type": "note_off", "channel": 0, "note": 16, "velocity": 64})[3] == M.LED_OFF)
check("led() encodes all three states",
      [H(M.led("rec", s, strip=2))[3] for s in (M.LED_OFF, M.LED_BLINK, M.LED_ON)]
      == [M.LED_OFF, M.LED_BLINK, M.LED_ON])
check("unknown note = raw number", H(note(120, 127)) == ("led", 120, None, M.LED_ON))
check("meter strip 6 at 0 dB (0xC)", H(M.meter(5, 0xC)) == ("meter", 5, 12))
check("meter 0xE / 0xF = overload / clear",
      H(M.meter(1, 0xE))[2] == "overload" and H(M.meter(1, 0xF))[2] == "clear")
check("meter strip nibble 8+ is not a strip",
      H({"type": "aftertouch", "channel": 0, "value": 0x85}) is None)
check("Ardour's master meter (channel pressure ch 2)",
      H({"type": "aftertouch", "channel": 1, "value": 0x0A}) == ("master_meter", 0, 10))
segs = [(c, d) for c in "0123456789ABZ -" for d in (False, True)]
bad = [(c, d) for c, d in segs if H(M.segment(3, c, d))[3:] != (c, d)]
check("7-segment chars + dot round-trip", not bad, bad)
check("segment CC 0x4B = assignment digit 1 (the LEFT one)",
      H(M.segment(1, "7", cc0=M.ASSIGN_CC0)) == ("segment", "assignment", 1, "7", False))
check("timecode on channel 16 accepted (some DAWs)", H(cc(0x40, 0x31, ch=15))[:3]
      == ("segment", "timecode", 0))
check("LCD SysEx: row 2 col 7", H(M.lcd("Hello", row=1, col=7)) == ("lcd", 0x14, 63, "Hello"))
check("other manufacturers' SysEx ignored",
      H({"type": "sysex", "data": (0x00, 0x20, 0x6B, 0x14, 0x12, 0, 65)}) is None)
check("a CC on ch 2 is not a ring", H(cc(48, 5, ch=1)) is None)
check("device query decoded", H(M.device_query()) == ("sysex", 0x14, 0, ()))

# ---- 3. handshake -----------------------------------------------------------------------------
print("3. handshake")
check("challenge response, worked by hand for 01 02 03 04 -> 05 05 7B 2F",
      M.challenge_response((1, 2, 3, 4)) == (5, 5, 0x7B, 0x2F), M.challenge_response((1, 2, 3, 4)))
check("challenge 10 20 30 01 -> 39 12 71 50 (by hand; pins the table's '>> 4', not Ardour's)",
      M.challenge_response((0x10, 0x20, 0x30, 0x01)) == (57, 18, 113, 80),
      M.challenge_response((0x10, 0x20, 0x30, 0x01)))
check("all response bytes are 7-bit",
      all(0 <= b < 128 for c0 in (0, 63, 127) for c3 in (0, 64, 127)
          for b in M.challenge_response((c0, 127, 99, c3))))
a = M.surface_answer(H(M.device_query(0x15)), dev=0x15)
check("device query -> host connection query (serial + challenge)",
      a["data"] == (0, 0, 0x66, 0x15, 0x01) + tuple(b"XYZORC1") + M.DEFAULT_CHALLENGE, a)
good = M.host_reply("XYZORC1", M.challenge_response(M.DEFAULT_CHALLENGE))
wrong = M.host_reply("XYZORC1", (0, 0, 0, 0))
check("right response -> confirmation 0x03", M.surface_answer(H(good))["data"][4] == 0x03)
check("wrong response, lenient -> still confirmed", M.surface_answer(H(wrong))["data"][4] == 0x03)
check("wrong response, strict -> error 0x04", M.surface_answer(H(wrong), strict=True)["data"][4] == 0x04)
v = M.surface_answer(H(M.sysex(M.SX_VERSION_REQ, (0,))))
check("version request -> 0x14 + 5 ASCII", v["data"][4] == 0x14 and bytes(v["data"][5:]) == b"V1.00")
check("hello = REAPER's 9-byte reset message (F0 00 00 66 14 01 58 59 5A)",
      (0xF0,) + M.surface_hello()["data"][:8] == (0xF0, 0, 0, 0x66, 0x14, 0x01, 0x58, 0x59, 0x5A))
check("an LCD write needs no answer", M.surface_answer(H(M.lcd("x"))) is None)

# ---- 4. the model, ORC gestures -> wire --------------------------------------------------------
print("4. gestures")
s, sent, evs = make(devices=2)
check("2 devices = 16 strips + master", len(s.strips) == 16 and s.master is not None)
out = s.set_fader(3, 0.7)
check("strip 4 fader 0.7 -> pitch bend ch 4 on the main port",
      out == [(0, {"type": "pitchwheel", "channel": 3, "pitch": int(round(0.7 * 16383)) - 8192})], out)
check("...the model holds it and tells subscribers",
      s.strips[3].fader == 0.7 and ("strip", 3, "fader", 0.7) in evs)
check("...and it went out through send()", sent == out)
check("strip 12 = extender port 1, ch 4", s.set_fader(11, 0.5)[0][0] == 1
      and s.set_fader(11, 0.5)[0][1]["channel"] == 3)
check("fader clamps: 1.5 -> top, -1 -> bottom",
      s.set_fader(0, 1.5)[0][1]["pitch"] == 8191 and s.set_fader(0, -1)[0][1]["pitch"] == -8192
      and s.strips[0].fader == 0.0)
check("master fader -> port 0 ch 9", s.set_fader(D.MASTER, 1.0) == [(0, M.fader(8, 16383))])
check("touch master = note 112; touch strip 10 = extender note 105",
      s.touch(D.MASTER)[0] == (0, note(112, 127)) and s.touch(9)[0] == (1, note(105, 127)))
s.touch(9, False)
check("V-pot strip 11 -3 -> extender CC 18 0x43", s.turn(10, -3) == [(1, cc(18, 0x43))])
check("a zero turn sends nothing", s.turn(10, 0) == [] and s.jog(0) == [])
out = s.press("mute", 5)
check("mute strip 6 = press + release, note 21",
      out == [(0, note(21, 127)), (0, note(21, 0))], out)
check("...the model waits for the DAW (no optimistic toggle)", s.strips[5].mute is D.OFF)
check("loop = MCU CYCLE (note 86)", s.press("loop")[0][1]["note"] == 86)
check("bank right / left = 47 / 46, channel right / left = 49 / 48",
      [s.bank(1)[0][1]["note"], s.bank(-1)[0][1]["note"],
       s.channel(1)[0][1]["note"], s.channel(-1)[0][1]["note"]] == [47, 46, 49, 48])
check("bank keys go to the main port", all(p == 0 for p, _m in s.bank(1)))
check("jog +2 -> port 0 CC 60", s.jog(2) == [(0, cc(60, 2))])
check("select strip 16 = extender note 31", s.press("select", 15)[0] == (1, note(31, 127)))
check("strip 17 refused", raises(lambda: s.set_fader(16, 0.5), IndexError))
check("button on strip 17 refused", raises(lambda: s.press("mute", 16), IndexError))
n = len(sent)
s.set_fader(1, 0.2)
s.press("play")
check("send() got every message, in order",
      [m[1].get("note", m[1].get("pitch")) for m in sent[n:]] == [int(round(0.2 * 16383)) - 8192, 94, 94])

# ---- 5. the model, DAW -> state ---------------------------------------------------------------
print("5. receive")
s, sent, evs = make(devices=2)
e = s.receive(M.fader(2, 12000), port=1)
check("extender fader 3 -> strip 11", e == [("strip", 10, "fader", 12000 / 16383.0)], e)
check("the same value again = no event", s.receive(M.fader(2, 12000), port=1) == [])
check("master fader on an extender port ignored", s.receive(M.fader(8, 100), port=1) == []
      and s.master.fader is None)
check("master fader on the main port", s.receive(M.fader(8, 16383)) == [("strip", D.MASTER, "fader", 1.0)])
s.touch(4)
check("a touched fader doesn't chase the host", s.receive(M.fader(4, 3000)) == []
      and s.strips[4].fader is None)
s.touch(4, False)
check("...and follows again after release", s.receive(M.fader(4, 3000)) != [])
s.receive(M.led("mute", M.LED_ON, strip=0), port=1)
s.receive(M.led("solo", M.LED_BLINK, strip=0), port=1)
check("extender mute 1 -> strip 9 mute ON, solo BLINK (truthy)",
      s.strips[8].mute is D.ON and s.strips[8].solo == D.BLINK and bool(s.strips[8].solo))
e = s.receive(M.led("play", M.LED_ON))
check("play LED -> transport play", e == [("transport", "play", D.ON)] and s.transport["play"])
check("CYCLE LED -> transport loop", s.receive(M.led("cycle", M.LED_ON)) == [("transport", "loop", D.ON)])
s.receive(M.led("flip", M.LED_ON))
s.receive(note(120, 127))
check("other LEDs land in leds by name", s.leds.get("flip") and s.leds.get("note_120"))
s.receive(M.led("vpot_push", M.LED_ON, strip=2))
check("a strip LED with no model field is kept as name:strip", s.leds.get("vpot_push:2"))
s.receive(M.meter(7, 0xC), port=1)
check("extender meter 8 at 0 dB -> strip 16", abs(s.strips[15].meter - 12 / 13.0) < 1e-9)
s.receive(M.meter(7, 0xE), port=1)
check("overload set, level kept", s.strips[15].overload is True and abs(s.strips[15].meter - 12 / 13.0) < 1e-9)
s.receive(M.meter(7, 0xF), port=1)
check("overload cleared", s.strips[15].overload is False)
s.receive({"type": "aftertouch", "channel": 1, "value": 0x0D})
check("Ardour master meter -> master strip", s.master.meter == 1.0)
s.receive(cc(48 + 1, 5))
s.receive(cc(48 + 1, 0))
check("ring value 0 = ring off", s.strips[1].vpot is None)
s.receive(cc(48 + 1, 1))
check("ring pos 1 = 0.0, dot", s.strips[1].vpot == 0.0 and s.strips[1].vpot_mode == "dot")
s.receive(cc(48 + 1, 0x10 | 0x40 | 11))
check("ring pos 11 = 1.0, boost_cut, centre",
      s.strips[1].vpot == 1.0 and s.strips[1].vpot_mode == "boost_cut" and s.strips[1].vpot_centre)

# LCD
names = "".join(n.ljust(7) for n in ("Kick", "Snare", "Bass", "Keys", "Vox", "Gtr", "FX", "Bus"))
vals = "".join(v.ljust(7) for v in ("-3.0", "0.0", "-12", "-inf", "+1.5", "-6", "-20", "0.0"))
e = s.receive({"type": "sysex", "data": M.SYSEX_HDR + (0x14, 0x12, 0)
               + tuple(ord(c) for c in names + vals)})      # REAPER writes both rows at once
check("one 112-char LCD write = 8 names + 8 second lines",
      [x.name for x in s.strips[:8]] == ["Kick", "Snare", "Bass", "Keys", "Vox", "Gtr", "FX", "Bus"]
      and [x.name2 for x in s.strips[:8]] == ["-3.0", "0.0", "-12", "-inf", "+1.5", "-6", "-20", "0.0"])
check("...16 change events, none for the extender",
      len(e) == 16 and all(ev[1] < 8 for ev in e), len(e))
e = s.receive(M.lcd("Drums  ", row=0, col=14))
check("partial write at offset 14 changes only strip 3",
      e == [("strip", 2, "name", "Drums")], e)
s.receive(M.lcd("Horn", row=0, col=49))
s.receive({"type": "sysex", "data": M.SYSEX_HDR + (0x14, 0x12, 52) + tuple(b"XXXXwrap")})
check("a write that runs off row 1 wraps into row 2",
      (s.strips[7].name, s.strips[0].name2) == ("HorXXXX", "wrap"),
      (s.strips[7].name, s.strips[0].name2))
s.receive({"type": "sysex", "data": M.SYSEX_HDR + (0x14, 0x12, 110) + tuple(b"abcdef")})
check("a write past char 112 is clipped, no crash", s.adapter.lcd_row(0, 1)[-2:] == "ab")
s.receive(M.lcd("ExtOne", dev=0x15), port=1)
check("extender LCD names strips 9-16 only", s.strips[8].name == "ExtOne" and s.strips[0].name == "Kick")
check("lcd_row keeps the raw text", s.adapter.lcd_row(1, 0).startswith("ExtOne"))
s.receive({"type": "sysex", "data": M.SYSEX_HDR + (0x14, 0x12, 0) + (7, 65)})
check("control characters on the LCD become spaces", s.strips[0].name == "Ack", s.strips[0].name)

# 7-segment displays
for i, ch in enumerate(reversed("0010102003")):
    s.receive(M.segment(i, ch, dot=(i in (3, 5))))
check("timecode reads left to right, with dots",
      s.display.get("timecode") == "00101.02.003", s.display.get("timecode"))
s.receive(M.segment(1, "1", cc0=M.ASSIGN_CC0))
s.receive(M.segment(0, "2", cc0=M.ASSIGN_CC0))
check("assignment display = '12' (0x4B left, 0x4A right)", s.display.get("assignment") == "12")
check("7-segment code 0 = a blank digit (REAPER sends 0x00 for blanks), not '@'",
      H(cc(0x40, 0x00))[3] == " " and H(cc(0x41, 0x40))[3:] == (" ", True), H(cc(0x40, 0x00)))
s2 = D.DawSurface(McuAdapter())
for i, v in enumerate([0x30, 0x30, 0x30, 0x00, 0x31 | 0x40, 0x00, 0x31 | 0x40, 0x00, 0x00, 0x00]):
    s2.receive(cc(0x40 + i, v))              # REAPER beats display, 1.1.000, blanks = 0x00
check("REAPER's blank timecode digits read as spaces (was '@@1@1@@000')",
      s2.display.get("timecode") == "   1. 1. 000", s2.display.get("timecode"))

# handshake through the model
n = len(sent)
e = s.receive(M.device_query(0x15), port=1)
check("device query on port 2 -> answered on port 2 as an XT, no model events",
      e == [] and sent[n:] == [(1, M.surface_hello(0x15))], sent[n:])
check("hello() announces every device",
      [(p, m["data"][3]) for p, m in s.hello()] == [(0, 0x14), (1, 0x15)])
check("a message on a port past the last device is ignored", s.receive(M.fader(0, 5), port=2) == [])
check("non-MCU traffic is ignored", s.receive(cc(7, 100, ch=3)) == [])

# fader round trip: one surface's gesture is another's motor move
a, _sa, _ea = make(devices=1)
b, _sb, _eb = make(devices=1)
bad = []
for v in (0.0, 0.001, 0.25, 0.5, 0.7071, 0.999, 1.0):
    for _p, m in a.set_fader(2, v):
        b.receive(m)
    if abs(b.strips[2].fader - v) > 0.5 / 16383 + 1e-12:
        bad.append((v, b.strips[2].fader))
check("fader values survive the 14-bit trip (within half a step)", not bad, bad)

# subscription
s, sent, evs = make(devices=1)
f = s.subscribe(lambda ev: None)
s.unsubscribe(evs.append)
s.receive(M.fader(0, 1))
check("unsubscribe stops events", evs == [])
snap = s.snapshot()
check("snapshot has every strip, master, transport, leds, display",
      len(snap["strips"]) == 8 and snap["master"]["index"] == D.MASTER
      and set(snap["transport"]) == set(D.TRANSPORT) and snap["strips"][0]["fader"] == 1 / 16383.0)
check("extender ids: 0x15 by default, configurable",
      McuAdapter(devices=3).dev_id(2) == 0x15 and McuAdapter(devices=2, ext_dev=0x11).dev_id(1) == 0x11)
check("devices < 1 refused", raises(lambda: McuAdapter(devices=0), ValueError))

# ---- 6. the core is dialect-neutral ---------------------------------------------------------------
print("6. neutral core")
core = open(os.path.join(HERE, "orc1_dawsurface.py")).read()
code = "\n".join(core.split('"""')[0::2])                     # outside docstrings
check("the core imports no dialect and names no MCU wire detail",
      "orc1_mcu" not in code and "pitch" not in code and "sysex" not in code.lower()
      and "16383" not in code and "N_STRIPS" not in code, [w for w in ("orc1_mcu", "pitch", "16383")
                                                          if w in code])


class Fake(D.Adapter):
    n_strips = 13

    def decode(self, msg, port=0):
        return [msg], []


f = D.DawSurface(Fake())
check("any strip count, no master", len(f.strips) == 13 and f.master is None
      and raises(lambda: f.set_fader(D.MASTER, 1), IndexError))
f.receive(("colour", 12, (255, 0, 128)))
check("a dialect that knows colours sets them", f.strips[12].colour == (255, 0, 128))
f.receive(("fader", 3, 7.0))
check("the core clamps a dialect's out-of-range fader", f.strips[3].fader == 1.0)
check("default adapter gestures send nothing", f.set_fader(0, 0.5) == [] and f.press("play") == [])
f.receive(("pan", 4, 0.25))
check("a dialect's pan update lands on the strip (clamped)", f.strips[4].pan == 0.25
      and (f.receive(("pan", 4, 3.0)) and f.strips[4].pan == 1.0))
check("Mackie / the base: the host banks (surface_banking False)",
      D.Adapter.surface_banking is False and McuAdapter().surface_banking is False)
m, msent, _e = make(devices=1)
check("set_fader_db on a dialect without dB: the plain fader goes out instead",
      m.set_fader_db(0, 0.0, 0.5) == m.set_fader(0, 0.5) and m.strips[0].fader == 0.5)
check("set_pan on a dialect without absolute pan: nothing sent, the model untouched",
      m.set_pan(0, 0.2) == [] and m.strips[0].pan is None)
f.strips[2].name = "x"
f.clear()
check("clear(): every strip fresh, the same count", len(f.strips) == 13 and f.strips[2].name == "")

# ---- 7. mido at the edge -----------------------------------------------------------------------------
try:
    import mido
except Exception:
    mido = None
if mido is None:
    check("[SKIPPED: no mido] dicts convert through mido", True)
else:
    ok = True
    for d in (M.fader(3, 100), M.button("play", True), M.vpot_turn(1, -2)[0], M.meter(2, 5),
              M.surface_hello(), M.lcd("Hi")):
        m = M.to_mido(d, mido)
        ok = ok and M.decode_host(m) == M.decode_host(d)
    check("dicts convert through mido and decode the same", ok)

total = PASS + FAIL
print("%d passed, %d failed" % (PASS, FAIL))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
    sys.exit(1)
sys.exit(1 if FAIL else 0)
