#!/usr/bin/env python3
"""
Control SURFACES (2026-10-14): orc1_padmap's surface fields + colours + surface_frames, and
orc1_surface_link driven against a fake mido. Pure stdlib (+ the two modules); touches no
port, no file, no service. Safe to run while the rig is in use.

The frames are checked against the Fire firmware's own parsing rules (fire_firmware
fw/fireseq/ctl.c ctl_host_parse: 0A <first> <count 1-10> <r g b x count>, levels 0-7,
first + count <= 64; 0B 01 <ch 1-16> / 0B 00) so the two sides can't drift apart silently.

Mutation-prove: make a surface pass unmapped pads through -> the "silent" check fails;
drop the USB address from the link identity -> the reboot check fails; resend on every
poll -> the "no timer" check fails.
"""
import sys

import orc1_padmap as P
from orc1_surface_link import SurfaceLink

EXPECTED = 42
_p = _f = 0


def check(cond, label, extra=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label} {extra}")


MODS = ["drums", "poly", "sampler"]

# ── validation ──────────────────────────────────────────────────────────────
raw = {"devices": {"Fire": {"port_match": "ORC FW", "surface": True, "surface_ch": 15, "pads": {
    "note:0": {"do": "note", "module": "drums", "note": 36, "color": "red"},
    "note:1": {"do": "pass", "color": "blue"},
    "note:2": {"do": "pass"},
    "note:3": {"do": "note", "module": "drums", "note": 38, "color": "chartreuse"}}}}}
conf, errs = P.validate_config(raw, MODS)
d = conf["devices"]["Fire"]
check(d.get("surface") is True and d.get("surface_ch") == 15, "surface + its channel are kept", d)
check(d["pads"]["note:0"].get("color") == "red", "a pad keeps its colour")
check("note:1" in d["pads"] and d["pads"]["note:1"] == {"do": "pass", "color": "blue"},
      "a pass-through pad with only a colour is kept (it is drawn)")
check("note:2" not in d["pads"], "a pass-through pad with nothing else is still dropped")
check("note:3" not in d["pads"] and any("colour" in e for e in errs), "an unknown colour is refused + reported", errs)
c2, e2 = P.validate_config({"devices": {"X": {"port_match": "x", "surface": True, "surface_ch": 13}}}, MODS)
check(c2["devices"]["X"]["surface_ch"] == P.SURFACE_CH_DEFAULT and e2, "channel 13 (ORC control) is refused", e2)
c3, _ = P.validate_config({"devices": {"X": {"port_match": "x", "surface": "yes"}}}, MODS)
check("surface" not in c3["devices"]["X"], "surface must be exactly true (no truthy strings)")
c4, _ = P.validate_config({"devices": {"X": {"port_match": "x", "pads": {"note:5": {"do": "ignore", "color": "off"}}}}}, MODS)
check("color" not in c4["devices"]["X"]["pads"]["note:5"], "'off' means no colour")

# ── a surface's unmapped pads are silent; mapped ones work as before ─────────
sent = []


class M:
    def __init__(s, t, ch=14, note=0, vel=100):        # ch 14 = MIDI ch 15 = the profile's surface_ch
        s.type, s.channel, s.note, s.velocity = t, ch, note, vel


class SX:                                            # the Fire's surface pad hit (v0.21): F0 7D 'ORC' 0C <pad> <vel> F7
    def __init__(s, pad, vel=100):
        s.type, s.data = "sysex", P.SURF_PAD_HDR + [pad, vel]


FIRE_IN = "ORC FW seq:ORC FW seq MIDI 1 32:0"
pm = P.PadMap(emit=lambda *a: sent.append(a), module_ch=lambda m: {"drums": 10}.get(m), run_action=lambda a: None,
              log=lambda s: None)
pm.set_config(conf)
check(pm.handle(SX(40), FIRE_IN) is True and not sent, "a surface pad with no entry is consumed and sends nothing")
check(pm.handle(SX(0), FIRE_IN) is True and sent == [("note_on", 10, 36, 100)],
      "a mapped surface pad (0C SysEx) plays its note on the named module", sent)
sent.clear()
check(pm.handle(SX(0, 0), FIRE_IN) is True and sent == [("note_off", 10, 36, 0)],
      "its release (0C, velocity 0) ends that note", sent)
check(pm.handle(SX(1), FIRE_IN) is False, "an explicit 'pass' on a surface still passes through")
sent.clear()
check(pm.handle(M("note_on", ch=9, note=36), FIRE_IN) is False and not sent,
      "the Fire's SEQUENCER notes pass through untouched")
check(pm.handle(M("note_on", ch=14, note=0), FIRE_IN) is False and not sent,
      "... on ANY channel, even the old surface channel with a mapped pad's number: the surface takes no channel")


class C:
    def __init__(s, ch, cc=1, val=64):
        s.type, s.channel, s.control, s.value = "control_change", ch, cc, val


check(pm.handle(C(3), FIRE_IN) is False, "a knob CC passes through")
sent.clear()
check(pm.handle(type("O", (), {"type": "sysex", "data": P.ORC_HDR + [0x07, 0, 100] + [0] * 16})(), FIRE_IN) is False and not sent,
      "other Fire SysEx (a 07 status reply, long enough to look like one) is not a pad hit")
plain, _ = P.validate_config({"devices": {"S": {"port_match": "Simmons", "pads": {}}}}, MODS)
pm2 = P.PadMap(emit=lambda *a: None, module_ch=lambda m: 10, run_action=lambda a: None, log=lambda s: None)
pm2.set_config(plain)
check(pm2.handle(M("note_on", note=40), "Simmons SD350") is False, "a NON-surface device's unmapped pads still pass through")
check([n for n, _ in pm.surfaces()] == ["Fire"] and pm2.surfaces() == [], "surfaces() lists only surface profiles")

# ── the frames, checked against the firmware's parsing rules ─────────────────
fr = P.surface_frames(d)
check(fr[0] == P.ORC_HDR + [0x0B, 1, 15], "first: surface on, on the profile's channel", fr[0])
check(fr == [P.surface_on(15)] + P.surface_colours(d), "surface_frames = surface on + the colours")
cols = [f for f in fr if f[4] == 0x0A]
ok, covered = True, []
for f in cols:
    first, count, body = f[5], f[6], f[7:]
    if not (first < 64 and 1 <= count <= 10 and first + count <= 64 and len(body) == 3 * count
            and all(0 <= v <= 7 for v in body) and len(f) + 2 < 40):   # F0 + (7D 'ORC' + payload) + F7 < sizeof sx (40)
        ok = False
    covered += list(range(first, first + count))
check(ok, "every 0A message is one the firmware accepts (count 1-10, <= pad 63, levels 0-7, fits the 40-byte buffer)")
check(covered == list(range(64)), "the 0A messages paint all 64 pads exactly once")
rgb = [tuple(f[7 + 3 * i:10 + 3 * i]) for f in cols for i in range(f[6])]
check(rgb[0] == P.COLOR_RGB["red"] and rgb[1] == P.COLOR_RGB["blue"] and rgb[2] == (0, 0, 0) and rgb[63] == (0, 0, 0),
      "pads take their entry's colour; the rest are dark", rgb[:3])
check(all(0 <= v <= 7 for c in P.COLOR_RGB.values() for v in c), "every palette colour is a valid LED level")
check(P.surface_off() == P.ORC_HDR + [0x0B, 0], "surface off = 0B 00")
check(all(b < 0x80 for f in fr for b in f), "every byte is valid SysEx data (< 0x80)")

# ── the link, against a fake mido ────────────────────────────────────────────
FIRE = "ORC FW seq:ORC FW seq MIDI 1 32:0"


class FakeOut:
    def __init__(s, name, log):
        s.name, s.log, s.closed, s.fail = name, log, False, False

    def send(s, m):
        if s.fail:
            raise OSError("gone")
        s.log.append((s.name, list(m.data)))

    def close(s):
        s.closed = True


class FakeMido:
    def __init__(s):
        s.outs, s.sent, s.opened = [FIRE], [], []

    class Message:
        def __init__(s, t, data):
            s.type, s.data = t, data

    def get_output_names(s):
        return list(s.outs)

    def open_output(s, name):
        o = FakeOut(name, s.sent)
        s.opened.append(o)
        return o


fm, logs = FakeMido(), []
surf = [("Fire", d)]
ident = {"ORC FW seq": "001/112"}
link = SurfaceLink(fm, lambda: surf, lambda: dict(ident), log=logs.append)


def ARM(dv):
    """What a Fire that appears gets: armed (channel + colours) but NOT shown."""
    return [P.surface_on(dv.get("surface_ch", 16))] + P.surface_colours(dv) + [P.surface_off()]


link.poll()
check(len(fm.opened) == 1 and [x[1] for x in fm.sent] == ARM(d),
      "appears: opens the Fire's output once, ARMS the surface and leaves the Fire on its steps (ends with 0B 00)")
fm.sent.clear(); link.poll(); link.poll()
check(fm.sent == [] and len(fm.opened) == 1, "nothing changed: nothing is resent (no timer: a player who left surface mode stays out)")
d2 = dict(d, pads=dict(d["pads"], **{"note:5": {"do": "pass", "color": "green"}}))
surf[:] = [("Fire", d2)]; link.poll()
check(len(fm.opened) == 1 and [x[1] for x in fm.sent] == P.surface_colours(d2),
      "the map changes: only the colours, same handle (never pops the surface up)")
fm.sent.clear(); link.repaint("Fire"); link.poll()
check(len(fm.opened) == 1 and [x[1] for x in fm.sent] == P.surface_frames(d2),
      "repaint (SHOW ON FIRE): everything resent once, surface-on first, same handle")
fm.sent.clear(); link.poll()
check(fm.sent == [], "... and only once")
fm.sent.clear(); ident["ORC FW seq"] = "001/113"; link.poll()
check(len(fm.opened) == 2 and fm.opened[0].closed and [x[1] for x in fm.sent] == ARM(d2),
      "a reboot (same port name, new USB address): a NEW handle, re-armed, not shown")
fm.sent.clear(); fm.outs = []; link.poll()
check(fm.opened[1].closed and fm.sent == [], "the port vanishes: the handle is dropped, nothing sent")
fm.outs = [FIRE]; link.poll()
check(len(fm.opened) == 3 and [x[1] for x in fm.sent] == ARM(d2), "and it comes back: re-armed, not shown")
fm.opened[-1].fail = True; surf[:] = [("Fire", d)]; fm.sent.clear(); link.poll()
check(fm.opened[-1].closed, "a send that fails: the handle is dropped (retry next poll)")
link.poll()
check(len(fm.opened) == 4 and [x[1] for x in fm.sent] == ARM(d), "... and the retry reopens + re-arms")
fm.sent.clear(); d3 = dict(d, surface_ch=12); surf[:] = [("Fire", d3)]; link.poll()
check([x[1] for x in fm.sent] == ARM(d3), "the surface channel changes: re-armed on the new channel, still not shown")
surf[:] = [("Fire", d)]; link.poll()
fm.sent.clear(); surf[:] = []; link.poll()
check(fm.sent and fm.sent[-1][1] == P.surface_off() and fm.opened[-1].closed and link.links == {},
      "surface switched off on /pads: 'surface off' once, handle closed")
# a BRAIN restart: a new link, the same Fire (same USB address) already armed -> colours only, no 0B (whose
# "on" would switch off a light show running on the Fire)
import os, tempfile
tdir = tempfile.mkdtemp()
apath = os.path.join(tdir, "armed.json")
fa = FakeMido()
la = SurfaceLink(fa, lambda: [("Fire", d)], lambda: dict(ident), log=lambda s: None, armed_path=apath)
la.poll()
check([x[1] for x in fa.sent] == ARM(d), "first link ever: armed")
fb = FakeMido()
lb = SurfaceLink(fb, lambda: [("Fire", d)], lambda: dict(ident), log=lambda s: None, armed_path=apath)
lb.poll()
check([x[1] for x in fb.sent] == P.surface_colours(d), "brain restart, same Fire already armed: colours only, no 0B", fb.sent)
for f in os.listdir(tdir):
    os.remove(os.path.join(tdir, f))
os.rmdir(tdir)

fm2, logs2 = FakeMido(), []
fm2.outs = ["Arturia BeatStep Pro:BeatStep Pro MIDI 1 24:0"]
link2 = SurfaceLink(fm2, lambda: [("BSP", {"port_match": "BeatStep", "surface": True, "pads": {}})],
                    lambda: {}, log=logs2.append)
link2.poll(); link2.poll()
check(fm2.opened == [] and len([l for l in logs2 if "not our Fire firmware" in l]) == 1,
      "a profile matching a device that isn't our firmware: never opened, said once", logs2)


class BadMido(FakeMido):
    def get_output_names(s):
        raise RuntimeError("alsa down")


logs3 = []
link3 = SurfaceLink(BadMido(), lambda: surf, lambda: {}, log=logs3.append)
link3.poll(); link3.poll()
check(len(logs3) == 1, "port listing fails: no crash, said once", logs3)

print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or (_p + _f) != EXPECTED:
    sys.exit(1)
