#!/usr/bin/env python3
"""
Knobs that follow the played voice (orc1_knobfocus) + the MCU codec (orc1_mcu).

    python3 _test_knobfocus.py [-v]

owner 2026-11-04: map the BeatStep Pro's knobs "however works best cc-wise", the
knob set changes to the voice being played (attack, decay, release, tune, filter,
ring mod...), and make the ORC MCU-aware. Covers: knob encodings, every default
page slot points at a CC the module's own handler really reads, focus follows a
NEW voice but a running loop doesn't keep stealing it, turning freezes focus, lock,
values start from what the hosts saved, chip engine-wide CCs share a value, the
MCU decode/encode, and that the brain is wired to all of it.

Pure: no MIDI port, no service. HOME/USERPROFILE point at a temp dir BEFORE import
and every file the code could touch is a temp file.
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="knobfocus_test_")
os.environ["HOME"] = _SANDBOX
os.environ["USERPROFILE"] = _SANDBOX

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_knobfocus as K     # noqa: E402
import orc1_mcu as M           # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 99


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


class Msg(object):
    def __init__(self, type, **kw):
        self.type = type
        self.channel = kw.pop("channel", 0)
        self.__dict__.update(kw)


def cc(ch, control, value):
    return Msg("control_change", channel=ch - 1, control=control, value=value)


def note(ch, n, vel=100):
    return Msg("note_on", channel=ch - 1, note=n, velocity=vel)


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


# the drum layout exactly as the config builds it
sys.path.insert(0, HERE)
import orc1_config as C        # noqa: E402
LAYOUT = C.DRUM_CC["LAYOUT"]
DRUM_NOTES = K.drum_note_map(os.path.join(HERE, "orc1_synth.py"),
                             C.SYNTH.get("DRUM_NOTE_MAP", {}))
BIND = {"drums": 10, "poly": 6, "mono": 3, "chip_p1": 9, "chip_p2": 4, "bbkit": 2}
KITS = os.path.join(_SANDBOX, "kits")
for _k, _ins in (("Alpha", {"36": "Kick Drum", "38": "Snare", "42": "Hi-Hats Closed", "64": "Conga Open"}),
                 ("Beta", {"36": "Big Kick", "38": "Brush Snare"})):
    os.makedirs(os.path.join(KITS, _k))
    with open(os.path.join(KITS, _k, "kit.json"), "w") as _f:
        json.dump({"name": _k, "instruments": {n: {"name": nm} for n, nm in _ins.items()}}, _f)
BB_STATE = os.path.join(_SANDBOX, "bbkit.json")
with open(BB_STATE, "w") as _f:
    json.dump({"kit": "Alpha"}, _f)


class Clock(object):
    t = 1000.0

    def __call__(self):
        return self.t


def make(conf=None, files=None):
    clock = Clock()
    sent = []
    d = tempfile.mkdtemp(dir=_SANDBOX)
    conf_file = os.path.join(d, "knobfocus.json")
    if conf is not None:
        with open(conf_file, "w") as f:
            json.dump(conf, f)
    pf = {}
    for k, data in (files or {}).items():
        pf[k] = os.path.join(d, k + ".json")
        with open(pf[k], "w") as f:
            json.dump(data, f)
    kf = K.KnobFocus(
        send_cc=lambda ch, c, v: sent.append((ch, c, v)),
        module_ch=BIND.get,
        module_at=lambda ch: next((m for m, c in BIND.items() if c == ch), None),
        drum_voice_of=DRUM_NOTES.get, drum_layout=LAYOUT,
        conf_file=conf_file, param_files=pf, now=clock, kits_dir=KITS, bbkit_state=BB_STATE)
    return kf, sent, clock, conf_file


BSP = "Arturia BeatStep Pro:Arturia BeatStep Pro Arturia Be 36:0"

# ---- 1. encodings ------------------------------------------------------------
check("offset64: 65 = +1, 63 = -1", (K.decode_relative(65, "offset64"),
      K.decode_relative(63, "offset64")) == (1, -1))
check("twos: 1 = +1, 127 = -1", (K.decode_relative(1, "twos"),
      K.decode_relative(127, "twos")) == (1, -1))
check("signmag: 1 = +1, 65 = -1, 67 = -3", (K.decode_relative(1, "signmag"),
      K.decode_relative(65, "signmag"), K.decode_relative(67, "signmag")) == (1, -1, -3))

# ---- 2. every default page slot is a CC the module's handler reads ------------
def handled_ccs(cls):
    tree = ast.parse(src("orc1_synth.py"))
    out = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "set_cc":
                    for n in ast.walk(fn):
                        if isinstance(n, ast.Compare):
                            names = [x for x in [n.left] + n.comparators
                                     if isinstance(x, ast.Name) and x.id == "cc"]
                            if not names:
                                continue
                            for x in [n.left] + n.comparators:
                                items = x.elts if isinstance(x, (ast.Tuple, ast.Set, ast.List)) else [x]
                                for it in items:
                                    if isinstance(it, ast.Constant) and isinstance(it.value, int):
                                        out.add(it.value)
    return out


OWNERS = {"poly": ["PolySynth"], "mono": ["MonoSynth"],
          "chip_p1": ["NESEngine"], "chip_p2": ["NESEngine"], "chip_tri": ["NESEngine"],
          "chip_noise": ["NESEngine"], "chip_noise2": ["NESEngine"],
          "gb_wave": ["NESEngine", "GameboyWaveVoice"]}
check("every default page has an owner to check against", set(K.DEFAULT_PAGES) == set(OWNERS),
      set(K.DEFAULT_PAGES) ^ set(OWNERS))
for mod, owners in sorted(OWNERS.items()):
    real = set()
    for o in owners:
        real |= handled_ccs(o)
    page = K.DEFAULT_PAGES[mod]
    used = [s[0] for s in page if s]
    check("%s: 16 slots, every CC handled by %s" % (mod, "+".join(owners)),
          len(page) == 16 and real and set(used) <= real, sorted(set(used) - real))
check("pages hold no duplicate CC", all(
    len([s[0] for s in p if s]) == len({s[0] for s in p if s}) for p in K.DEFAULT_PAGES.values()))

kf, sent, clock, _ = make()
dp = kf.page("drums", "snare")
check("drum page top row = the focused voice's params",
      [s[1] for s in dp[:6]] == [LAYOUT["snare"]["start"] + i for i in range(6)], dp[:6])
check("drum page bottom row = 8 voice levels",
      [s[1] for s in dp[8:]] == [LAYOUT[v]["start"] for v in K.DRUM_LEVEL_ROW], dp[8:])
tp = kf.page("drums", "low_tom")
check("a tom (4 params) leaves filter slots empty", tp[4] is None and tp[5] is None and tp[3])
check("drum note map read from the synth source", DRUM_NOTES.get(36) == "bass_drum"
      and DRUM_NOTES.get(38) == "snare" and DRUM_NOTES.get(42) == "closed_hat", DRUM_NOTES)
check("DRUM_NOTE_MAP override applies", K.drum_note_map(
    os.path.join(HERE, "orc1_synth.py"), {"60": "snare"}).get(60) == "snare")

# ---- 3. knobs claimed by device + channel + CC only ----------------------------
kf, sent, clock, _ = make()
kf.observe(note(6, 60))
check("first note takes focus", kf.focus == "poly")
check("knob 1 (CC 20 ch 8) on the BSP is claimed", kf.handle(cc(8, 20, 65), BSP))
check("... and sends poly cutoff 64+1 on poly's channel", sent[-1:] == [(6, 74, 65)], sent)
n = len(sent)
check("same CC from another device is NOT claimed", not kf.handle(cc(8, 20, 65), "Akai MPK"))
check("same CC on another channel is NOT claimed", not kf.handle(cc(7, 20, 65), BSP))
check("a CC outside the 16 is NOT claimed", not kf.handle(cc(8, 40, 65), BSP))
check("nothing sent for unclaimed messages", len(sent) == n)
kf.handle(cc(8, 20, 60), BSP)
check("a -4 turn goes down from the current value", sent[-1] == (6, 74, 61), sent[-1])
for _ in range(80):
    kf.handle(cc(8, 20, 127), BSP)
check("clamped at 127", sent[-1] == (6, 74, 127))
check("no resend once pinned at 127", sent.count((6, 74, 127)) == 1)
kf.handle(cc(8, 32, 65), BSP)   # knob 13 = Level on poly
check("knob 13 = poly Level (CC 13)", sent[-1] == (6, 13, 65), sent[-1])
n = len(sent)
kf.handle(cc(8, 35, 65), BSP)   # knob 16 = empty on poly
check("an empty slot sends nothing (still claimed)", len(sent) == n)
kf.conf["enabled"] = False
check("disabled: nothing claimed", not kf.handle(cc(8, 20, 65), BSP))

# ---- 4. values start from what the hosts saved; fresh values win -----------------
kf, sent, clock, _ = make(files={"poly": {"74": 30, "8": 100},
                                 "chip": {"_engine": {"74": 10}, "pulse1": {"1": 90}},
                                 "drums": {"snare": {"tune": 88}}})
kf.observe(note(6, 60))
kf.handle(cc(8, 20, 65), BSP)
check("poly cutoff starts from the saved 30", sent[-1] == (6, 74, 31), sent[-1])
kf.observe(cc(6, 8, 5))          # the Fire / web moved attack just now
clock.t += 3
kf.observe(note(10, 36))         # drums take focus
clock.t += 3
kf.observe(note(6, 60))          # back to poly: the file says attack 100, but 5 is fresh
kf.handle(cc(8, 28, 65), BSP)    # knob 9 = Attack
check("a value seen seconds ago beats the saved file", sent[-1] == (6, 8, 6), sent[-1])
kf.command({"focus": "chip_p1"})
kf.handle(cc(8, 23, 65), BSP)    # knob 4 = Duty
check("chip voice value from its own saved slot", sent[-1] == (9, 1, 91), sent[-1])
kf.handle(cc(8, 20, 65), BSP)
check("chip cutoff from the engine-wide saved value", sent[-1] == (9, 74, 11), sent[-1])
kf.command({"focus": "chip_p2"})
kf.handle(cc(8, 20, 65), BSP)
check("chip_p2 cutoff continues the SHARED engine value", sent[-1] == (4, 74, 12), sent[-1])
kf.command({"focus": "drums", "voice": "snare"})
kf.handle(cc(8, 21, 65), BSP)    # knob 2 = snare tune
check("drum voice value from the saved drum file",
      sent[-1] == (10, LAYOUT["snare"]["start"] + 1, 89), sent[-1])
kf2, s2, _c, _ = make()
kf2.command({"focus": "poly"})
kf2.handle(cc(8, 20, 65), BSP)
check("a never-set parameter starts at 64", s2[-1] == (6, 74, 65), s2)

# ---- 5. focus follows a NEW voice, not a running loop ------------------------------
kf, sent, clock, _ = make()
kf.observe(note(6, 60))
check("poly in focus", kf.focus == "poly")
for _ in range(20):                       # a drum loop already running, hits every 0.25 s
    clock.t += 0.25
    kf.observe(note(10, 42))
check("the drums' FIRST hit after silence takes focus", kf.focus == "drums")
kf.command({"focus": "poly"})
for _ in range(20):
    clock.t += 0.25
    kf.observe(note(10, 42))
check("a loop that never goes quiet doesn't steal it back", kf.focus == "poly")
clock.t += 5
kf.observe(note(10, 36))
check("after drums were quiet > quiet_s, a hit takes focus", kf.focus == "drums"
      and kf.voice == "bass_drum", (kf.focus, kf.voice))
clock.t += 0.1
kf.observe(note(10, 38))
check("a quiet drum voice takes the drum page", kf.voice == "snare")
clock.t += 0.1
kf.observe(note(10, 36))
check("a voice heard 0.2 s ago doesn't flip it back", kf.voice == "snare")
clock.t += 5
kf.handle(cc(8, 20, 65), BSP)             # turning a knob...
clock.t += 0.5
kf.observe(note(6, 60))
check("...freezes focus while turning (hold_s)", kf.focus == "drums")
clock.t += 3
kf.observe(note(6, 60))
check("after the hold, poly takes it", kf.focus == "poly")
kf.observe(Msg("note_on", channel=0, note=60, velocity=100))   # ch 1: nothing bound
check("a note on an unbound channel changes nothing", kf.focus == "poly")
clock.t += 5
kf.observe(note(10, 36, vel=0))
check("note-on velocity 0 (a note-off) changes nothing", kf.focus == "poly")

kf, sent, clock, conf_file = make(conf={"ignore": ["drums"]})
kf.observe(note(10, 36))
check("an ignored module never takes focus", kf.focus is None)

# ---- 6. lock / commands / persistence ------------------------------------------------
kf, sent, clock, conf_file = make()
r = kf.command({"mode": "lock", "focus": "mono"})
check("lock on mono", r["focus"] == "mono" and r["mode"] == "lock")
clock.t += 10
kf.observe(note(6, 60))
check("lock: playing poly does not move focus", kf.focus == "mono")
saved = json.load(open(conf_file))
check("lock + module saved to the config file", saved.get("mode") == "lock"
      and saved.get("locked") == "mono", saved)
kf3, _s, _c, _f = make(conf=saved)
kf3.start()
check("a restart comes back locked on mono", kf3.focus == "mono")
check("unknown module refused", not kf.command({"focus": "sampler"})["ok"])
st = kf.status()
check("status lists 16 slots with names", len(st["slots"]) == 16
      and st["slots"][0]["name"] == "Cutoff", st["slots"][:2])
check("status offers drums + the synth modules", {"drums", "poly", "mono", "chip_p1"}
      <= set(st["modules"]), st["modules"])
r = kf.command({"conf": {"channel": 5, "ccs": list(range(102, 118))}})
check("conf edit: channel + CCs take", kf.handle(cc(5, 102, 65), BSP)
      and not kf.handle(cc(8, 20, 65), BSP))
kf.command({"conf": {"pages": {"mono": [[74, "Cut"]] + [None] * 15}}})
check("a user page overrides the default", kf.names()[0] == "Cut" and kf.names()[1] == "")

# ---- 7. absolute knobs with pickup ---------------------------------------------------
kf, sent, clock, _ = make(conf={"encoding": "abs"}, files={"poly": {"74": 40}})
kf.observe(note(6, 60))
kf.handle(cc(8, 20, 100), BSP)
check("abs: far from the value = no jump", sent == [])
kf.handle(cc(8, 20, 41), BSP)
kf.handle(cc(8, 20, 50), BSP)
check("abs: picks up near the value, then follows", sent == [(6, 74, 41), (6, 74, 50)], sent)

# ---- 8. MCU codec ----------------------------------------------------------------------
check("MCU V-pot 3 +2", M.decode(Msg("control_change", channel=0, control=18, value=2))
      == ("vpot", 2, 2))
check("MCU V-pot 1 -1 (0x41)", M.decode(Msg("control_change", channel=0, control=16, value=0x41))
      == ("vpot", 0, -1))
check("MCU fader 2 (pitch bend ch 2)", M.decode(Msg("pitchwheel", channel=1, pitch=8191))
      == ("fader", 1, 16383))
check("MCU master fader = ch 9", M.decode(Msg("pitchwheel", channel=8, pitch=-8192))
      == ("fader", 8, 0))
check("MCU play button", M.decode(Msg("note_on", channel=0, note=94, velocity=127))
      == ("button", "play", None, True))
check("MCU mute 4 release", M.decode(Msg("note_on", channel=0, note=19, velocity=0))
      == ("button", "mute", 3, False))
check("MCU jog -3", M.decode(Msg("control_change", channel=0, control=60, value=0x43))
      == ("jog", -3))
check("a CC on ch 2 is not MCU", M.decode(Msg("control_change", channel=1, control=16, value=1)) is None)
check("ring: value 127 = LED 11, wrap mode", M.vpot_ring(0, 127, M.RING_WRAP)
      == {"type": "control_change", "channel": 0, "control": 48, "value": 0x2B})
lc = M.strip_labels(["Cutoff", "Resonance"])
check("scribble strip: Mackie header + 56 chars, names clipped to 6",
      lc["data"][:6] == (0, 0, 0x66, 0x14, 0x12, 0) and len(lc["data"]) == 6 + 56
      and bytes(lc["data"][6:20]).decode() == "Cutoff Resona ", lc["data"][6:20])
check("fader encode round-trips", M.decode(Msg(**M.fader(3, 12000))) == ("fader", 3, 12000))
check("button LED encode", M.button_led("mute", True, strip=2)["note"] == 18
      and M.button_led("play", False)["velocity"] == 0)

kf, sent, clock, _ = make(conf={"mcu_ports": ["MCU Pro"]})
kf.command({"focus": "poly"})
check("MCU port: V-pot 1 turns knob slot 1", kf.handle(
    Msg("control_change", channel=0, control=16, value=3), "Mackie MCU Pro:0") and sent[-1] == (6, 74, 67))
kf.handle(Msg("pitchwheel", channel=0, pitch=8191), "Mackie MCU Pro:0")
check("MCU port: fader 1 sets slot 9 (Attack) absolutely", sent[-1] == (6, 8, 127), sent[-1])

# ---- 8b. BBKit page: one CC picks the drum, the rest shape it --------------------------
import orc1_config as CF                                       # noqa: E402
BC = CF.BBKIT_SHAPE_CC
check("BBKit shaping CCs are distinct and fit one channel",
      len(set(BC.values())) == len(BC) and all(0 <= v < 120 for v in BC.values()), BC)
check("every shaping param has a default", set(CF.BBKIT_SHAPE_DEFAULT) == set(BC) - {"note"})
kf, sent, clock, _ = make(files={"bbkit": {"kits": {"Alpha": {"notes": {"38": {"level": 80}},
                                                               "kit": {"kit_tune": 70}}}}})
kf.observe(note(2, 38))
check("a BBKit hit takes focus on that drum", kf.focus == "bbkit" and kf.bb_note == 38,
      (kf.focus, kf.bb_note))
nm = kf.names()
check("labels come from the kit's own names", nm[0] == "Snare level" and nm[7] == "Kit tune"
      and nm[9] == "Kick Drum level", nm)
check("a common drum the kit lacks leaves its level slot empty", nm[12] == "" and kf.page()[12] is None,
      nm)
del sent[:]
kf.handle(cc(8, 20, 65), BSP)
check("knob 1 = pick drum 38, then its level from the saved 80",
      sent == [(2, BC["note"], 38), (2, BC["level"], 81)], sent)
del sent[:]
kf.handle(cc(8, 21, 65), BSP)
check("knob 2 = tune, from its DEFAULT 64 (not a blind 64 guess)",
      sent == [(2, BC["note"], 38), (2, BC["tune"], 65)], sent)
del sent[:]
kf.handle(cc(8, 23, 63), BSP)
check("knob 4 = decay down from its default 127", sent[-1] == (2, BC["decay"], 126), sent)
del sent[:]
kf.handle(cc(8, 27, 65), BSP)
check("knob 8 = kit tune: no drum pick, from the saved 70", sent == [(2, BC["kit_tune"], 71)], sent)
del sent[:]
kf.handle(cc(8, 29, 65), BSP)
check("knob 10 = kick level (bottom row): picks 36 first", sent == [(2, BC["note"], 36),
      (2, BC["level"], 101)], sent)
for m in [cc(2, BC["note"], 64), cc(2, BC["level"], 50)]:   # the web / a pad shaped conga
    kf.observe(m)
clock.t += 5
kf.observe(note(2, 64))
del sent[:]
kf.handle(cc(8, 20, 65), BSP)
check("a value someone else sent (pick 64, level 50) is followed", sent[-1] == (2, BC["level"], 51),
      sent)
clock.t += 1.6                                # past the turn-hold
kf.observe(note(2, 38))
check("a BBKit drum heard 5 s ago (quiet) takes the page", kf.bb_note == 38)
clock.t += 0.1
kf.observe(note(2, 64))
check("one heard 0.1 s ago doesn't flip it back", kf.bb_note == 38)
kf.observe(note(2, 99))
check("a note the kit doesn't have is ignored", kf.bb_note == 38)
kf.observe(Msg("program_change", channel=1, program=1))
check("program change 1 = the 2nd kit (sorted, as the host scans)", kf.bb_kit() == "Beta"
      and kf.names()[0] == "Brush Snare level", kf.names()[:1])
del sent[:]
kf.handle(cc(8, 20, 65), BSP)
check("each kit keeps its own values (Beta snare from the default 100)",
      sent[-1] == (2, BC["level"], 101), sent)
r = kf.command({"focus": "bbkit", "note": 36})
check("KNOBS_SET can pick a BBKit drum", r["bb_note"] == 36 and r["slots"][0]["name"] == "Big Kick level"
      and r["slots"][0]["value"] == 100, r["slots"][0])
check("bbkit is offered as a page", "bbkit" in r["modules"], r["modules"])
b = src("orc1.py")
check("program changes reach the observer (kit tracking)",
      '("note_on", "control_change", "program_change")' in b)

# ---- 9. the brain is wired to it -------------------------------------------------------
b = src("orc1.py")
rm = b[b.index("def _route_message"):b.index("def make_usb_handler")]
check("_route_message claims knobs right after the pad maps",
      0 < rm.index("padmap.handle(") < rm.index("knobfocus.handle(msg, source_name)")
      < rm.index("if not hasattr(msg, 'channel')"))
st_ = b[b.index("def send_to(port_key, msg)"):b.index("def note_on_out")]
check("send_to(synth) feeds the observer", "_knobfocus_observe(msg)" in st_)
ssm = b[b.index("def send_synth_midi(msg)"):b.index("# -- Live sampler facade")]
check("send_synth_midi feeds the observer", "_knobfocus_observe(msg)" in ssm)
check("KNOBS_STATUS / KNOBS_SET socket actions", 'action == "KNOBS_STATUS"' in b
      and 'action == "KNOBS_SET"' in b)
check("the HUD feed carries the knob names", '"knobs":' in b and "knobfocus.names()" in b)
check("boot restores a lock", "knobfocus.start()" in b)

shutil.rmtree(_SANDBOX, ignore_errors=True)
total = PASS + FAIL
print("%d passed, %d failed" % (PASS, FAIL))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
    sys.exit(1)
sys.exit(1 if FAIL else 0)
