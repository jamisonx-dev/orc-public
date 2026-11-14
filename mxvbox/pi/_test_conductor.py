"""_test_conductor.py -- the mxvbox conductor's logic (pure python3, no rig needed).

    python3 _test_conductor.py

Sandboxed: HOME points at a temp dir BEFORE the conductor is imported (its save paths
resolve at import), nothing is sent to the box (send is replaced), and the real
~/.orc1_mxvbox*.json files are checked byte-identical at the end.
The step-clock simulation is the guard for the bug the first version had: it derived
"the next step" from each feed update and SKIPPED steps when the feed lagged.
"""
import hashlib, json, os, random, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = [os.path.expanduser("~/.orc1_mxvbox.json")]


def md5(p):
    try:
        return hashlib.md5(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


real_before = [md5(p) for p in REAL]
SANDBOX = tempfile.mkdtemp(prefix="mxvbox_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX   # USERPROFILE: what ~ means on Windows
sys.path.insert(0, HERE)
threads_before = threading.active_count()
import conductor as C                   # noqa: E402
import mxvbox_knobs as K                # noqa: E402

EXPECTED = 123
passed = failed = 0


def check(cond, label, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", label, extra)


SENT = []
REAL_SEND = C.send
C.send = lambda cmd: SENT.append(cmd)


def reset():
    C.knob.clear()
    C.knob.update(K.defaults())
    C.state.update(cue=None, clip=None, show=True, my_ch=None, drum_chs=set(), dirty=0.0, scene_bars=0,
                   rot=C.ROT_NONE, rot_step=None, looks_calc=0.0)
    C.state["box_clips"] = 5                  # the box reports five clips in its RAM
    del SENT[:]


# ---- import-safe, sandboxed -------------------------------------------------------
check(threading.active_count() == threads_before, "importing starts no threads (nothing runs until main())")
check(C.SAVE.startswith(SANDBOX), "the knob save path is in the sandbox", C.SAVE)

# ---- the defaults are the look signed off on 2026-10-29 ---------------------------
reset()
C.state["clip"] = 0
check(C.hit_style("kick") == "blur", "default kick = full-screen blur", C.hit_style("kick"))
check(C.hit_style("snare") == "warp", "default snare = warp (the flipped band's slot)", C.hit_style("snare"))
check(C.lines_cmd() == "t 0\nn 4 1 000000", "default lines = fine black scanlines (1 row in 4), the 2nd layer off", C.lines_cmd())
check(C.clips_of_4() == 3 and C.change_every() == 1, "default: 3 clip bars in 4, a change every bar")
check(abs(C.fade_s() - 0.45) < 0.005, "default fade 0.45 s", C.fade_s())
check(C.hit_cmd("blur") == "h blur 4 450", "default blur hit: level 4, faded by the box over FADE", C.hit_cmd("blur"))

# ---- the hit REACTIONS (KICK / SNARE STYLE in tenths) ---------------------------------
reset()
C.state["clip"] = 0
got = []
for v in (10, 12, 18, 115):                                # blur (default + old button), RGB, off
    C.knob[21] = v
    got.append(C.hit_style("kick"))
check(got == ["blur", "blur", "rgb", None],
      "blur, RGB split and off kept their values when the CPU reactions left (their slots were refilled)", got)
got = []
for i in range(len(K.REACTIONS)):
    C.knob[21] = int(i * 128 / len(K.REACTIONS) + 6)        # the middle of each tenth (what the BOX tab sends)
    got.append(K.REACTIONS[K.reaction(C.knob[21])])
check(got == list(K.REACTIONS) and K.reaction(0) == 0 and K.reaction(127) == len(K.REACTIONS) - 1,
      "KICK STYLE tenths, in order: blur rgb invert zoom warp shudder grey hue off kaleido", got)
check(not set(K.REACTIONS) & {"mirror", "flip", "colour"}, "the CPU-drawn reactions are gone")
reset()
C.state.update(my_ch=15, drum_chs={2})
C.knob[21] = 19                                             # RGB SPLIT
C.fire_hit("kick", 1.0)
check(SENT == ["h rgb 12 450"],
      "an RGB SPLIT kick: ONE message, the box holds and fades it (12 px at HIT SIZE 64, the FADE in ms)", SENT)
C.knob[28], C.knob[20] = 32, 127
C.fire_hit("kick", 2.0)
check(SENT[-1] == "h rgb 6 1500", "...HIT SIZE scales it, FADE sets how long", SENT[-1:])
for v, want in ((45, "h zoom 0.06 1500"), (70, "h shudder 1.5 1500"), (96, "h hue 45 1500"), (121, "h kaleido 6 1500"),
                ((6, "h blur 2 1500")), (31, "h invert 0.5 1500"), (57, "h warp 5 1500"), (82, "h grey 0.5 1500")):
    C.knob[21] = v
    C.fire_hit("kick", 3.0)
    check(SENT[-1] == want, "kick reaction at %d -> %s (kaleido is a snap: HIT SIZE does not scale it)" % (v, want), SENT[-1:])
reset()
looks = []
for v in (0, 30, 60, 90, 120):
    C.knob[23] = v
    looks.append(C.lines_cmd())
check(looks == ["t 0\nn 0", "t 0\nn 4 1 000000", "t 0\nn 2 1 000000", "t 0\nn 3 1 000000", "t 0\nn 6 2 000000"],
      "LINES fifths, black: the GPU's thin lines, and never an 'n 0' after them (it used to cancel them)", looks)
C.knob[23], C.knob[24] = 30, 100
check(C.lines_cmd() == "t 0\nn 2 1 blur", "LINES FILL 64+ = the blurred picture, as picture-row lines (the black layer off)")

# ---- which bars are clips; what each step sends -------------------------------------
reset()
C.knob[25] = 0
check(not any(C.clip_bar(b) for b in range(8)), "CLIPS 0 = patterns only")
C.knob[25] = 127
check(all(C.clip_bar(b) for b in range(8)), "CLIPS 127 = clips only")
reset()
cmds = [C.picture(s, []) for s in range(16)]
check(cmds[0].startswith("p next ") and all(c is None for c in cmds[1:]),
      "a clip bar: 'p' on its downbeat, nothing on the other 15 steps", cmds[:3])
check("n 4 1 000000" in cmds[0].split("\n"), "the lines look rides every bar's downbeat (survives a box restart)")
pat = [C.picture(3 * 16 + s, []) for s in range(16)]        # bar 3 = the pattern bar of 4
check(all(p for p in pat) and not pat[0].startswith("p ") and C.state["clip"] is None,
      "a pattern bar sends a picture every step, and no clip is current")
reset()
C.state["cue"] = 9
first = C.picture(0, [])
check(first.startswith("p cue 9 ") and C.state["cue"] is None,
      "a cue note asks the box for library clip N, once", first.split("\n")[0])
reset()
C.knob[29], C.knob[25] = 40, 127          # change every 2 bars, all clips
ps = [s for s in range(0, 64) if (C.picture(s, []) or "").startswith("p ")]
check(ps == [0, 32], "CHANGE EVERY 2: a new clip every other bar only", ps)

# ---- SCENES (CC 39-41): the pattern bars show a scene the box's GPU draws -----------
reset()
C.state["bps"] = 2.0
check([K.scene_of(v) for v in (0, 18, 19, 40, 60, 80, 100, 127)] == [None, None, "parallax", "floor", "tunnel", "rings", "spiral", "opart"],
      "SCENE sevenths: patterns, parallax, floor, tunnel, rings, spiral, op-art")
check(C.scene_cmd() is None, "default SCENE = no scene: the pattern bars show the patterns as before")
C.knob[39] = 60
sc = [C.picture(3 * 16 + s, []) for s in range(16)]         # bar 3 = the pattern bar of 4
check(sc[0].split("\n")[0] == "S tunnel 0.5000 0.504 0.000 0.000" and all(x is None for x in sc[1:]) and C.state["clip"] == "scene",
      "a pattern bar with SCENE on: 'S tunnel' on the downbeat (one cycle a bar at 120 bpm), nothing mid-bar", sc[:2])
C.knob[40], C.knob[41], C.state["bps"] = 0, 127, 1.0
check(C.scene_cmd() == "S tunnel 0.0000 1.000 0.000 0.000", "SCENE SPEED 0 = still; DETAIL 127 = 1", C.scene_cmd())
C.knob[39], C.knob[43] = 19, 60                             # parallax, SCENE CYCLE every 2 bars
got = [C.scene_cmd(b).split()[1] for b in range(0, 14, 2)]
check(got == ["parallax", "floor", "tunnel", "rings", "spiral", "opart", "parallax"],
      "SCENE CYCLE: the next scene every N bars, from the chosen one, round and round", got)
check(C.scene_cmd(1).split()[1] == "parallax" and C.scene_cmd(2).split()[1] == "floor", "...changing only every N bars")
C.knob[25], C.state["scene_bars"] = 64, 0                  # PICTURES: 2 clip bars, 2 scene bars of every 4
shown = [C.picture(b * 16, []).split("\n")[0] for b in range(48)]
names = [x.split()[1] for x in shown if x.startswith("S ")]
check(len(names) == 24 and set(names) == set(K.SCENE_NAMES[1:]) and names[:4] == ["parallax", "parallax", "floor", "floor"],
      "SCENE CYCLE through the song: every scene comes round (it counts scene bars, not all bars)", names[:8])
C.knob[25] = K.defaults()[25]
C.knob[43] = 0
check(C.scene_cmd(9).split()[1] == "parallax", "SCENE CYCLE off: the chosen scene stays")
C.knob[39] = 60
C.knob[42] = 127
check(C.scene_cmd().endswith(" 1.000 0.000") and K.KNOBS[42][1] == 0, "SCENE VIDEO 127 = all your clips (default 0 = drawn)", C.scene_cmd())
C.knob[44] = 127
check(C.scene_cmd().endswith(" 1.000 1.000") and K.KNOBS[44][1] == 0, "SCENE OVERLAY 127 = the clip flat over it all (default 0 = off)", C.scene_cmd())
C.knob[44] = 0
C.knob[39] = 0
check(not (C.picture(3 * 16, []) or "").startswith("S "), "SCENE back to 0: patterns again")
reset()

# ---- bar effects follow the knobs -----------------------------------------------------
reset()
fx1, fx2 = C.bar_fx(1), C.bar_fx(2)
check(fx1[0].startswith("w 10 ") and fx2[1] == "x next 0.50" and not any(x.startswith("r ") for x in fx1 + fx2),
      "default: ripple on some clip bars, weave (every other line) on others; no raster bars sent", (fx1, fx2))
C.knob[26], C.knob[27] = 0, 0
check(C.bar_fx(1)[0] == "w 0 0 0" and C.bar_fx(2)[1] == "x -1", "WOBBLE 0 / WEAVE 0 turn them off")
check("l 0" in C.bar_fx(1) and "l 0" in C.bar_fx(2), "every bar also clears the OLD picture-row lines (l 0), so a stale one cannot stack with the thin lines")
C.state["bps"] = 2.0
C.knob[26] = 64
check(C.bar_fx(1)[0] == "w 10 1.5 2.000", "the ripple is timed to the feed's tempo", C.bar_fx(1)[0])

# ---- hits: fade shape, the show switch, MIDI routing -------------------------------
reset()
check([C.num(v) for v in (4.0, 2.35, 0.5, 0)] == ["4", "2.35", "0.5", "0"], "amounts written short")
C.knob[28] = 40
check(C.hit_cmd("blur") == "h blur 2.5 450", "HIT SIZE scales the blur smoothly (no rounding to a whole level)", C.hit_cmd("blur"))
C.knob[28] = 127
check(C.hit_cmd("blur") == "h blur 4 450" and C.hit_cmd("invert") == "h invert 1 450" and C.hit_cmd("grey") == "h grey 1 450",
      "a big HIT SIZE can't go past all: blur 4, invert / grey 1", (C.hit_cmd("blur"), C.hit_cmd("invert")))
C.knob[28] = K.defaults()[28]
C.state["show"] = False
C.fire_hit("kick", 1.0)
check(SENT == [], "SHOW off: hits send nothing", SENT)
check([C.alpha_of(v) for v in (0, 64, 127, 200)] == [0, 129, 255, 255], "SHOW 0-127 -> brightness 0-255 (clamped)")
C.state.update(alpha=None, alpha_t=0.0)
C.knob[7] = 127
check(C.alpha_cmd(100.0) == "a 255 0.00", "the first send is instant (a box restart starts at full)")
check(C.alpha_cmd(101.0) is None, "SHOW unchanged: nothing is sent again")
C.knob[7] = 0
check(C.alpha_cmd(102.0) == "a 0 0.80", "SHOW to 0: a fade to black over 0.8 s")
check(C.alpha_cmd(113.0) == "a 0 0.00", "the 10 s refresh re-sends the level instantly (a restarted box), never mid-fade")
C.knob[7] = 127
C.state.update(alpha=None, alpha_t=0.0, show=True)
reset()
C.state.update(my_ch=15, drum_chs={2, 10}, clip=0)
C.on_midi(([0xB0 | 14, 20, 90], 0))                   # CC 20 on ch 15
check(C.knob[20] == 90 and C.state["dirty"] > 0, "a CC on the module's channel sets that knob + marks it to save")
C.on_midi(([0xB0 | 13, 21, 1], 0))                    # same CC on ch 14: not ours
check(C.knob[21] == K.defaults()[21], "a knob CC on another channel is ignored")
C.on_midi(([0x90 | 14, 3, 100], 0))
check(C.state["cue"] == 3, "a note on the module's channel cues a clip")
C.on_midi(([0x90 | 1, 36, 100], 0))                   # kick on ch 2 (bbkit)
check(SENT and SENT[-1].startswith("h blur 4 "), "a kick on a drum channel fires its hit at once (one message)", SENT[-1:])
n = len(SENT)
C.on_midi(([0x90 | 1, 36, 0], 0))                      # note-on vel 0 = note-off
C.on_midi(([0x90 | 4, 36, 100], 0))                    # kick on a non-drum channel
check(len(SENT) == n, "note-offs and drum notes on other channels do nothing")

# ---- persistence + the clip tool's file -------------------------------------------
reset()
C.knob[20] = 77
C.save_knobs()
C.knob[20] = 0
with open(C.SAVE) as f:
    j = json.load(f)
j["20"], j["99"], j["21"] = 77, 5, 999
with open(C.SAVE, "w") as f:
    json.dump(j, f)
C.load_knobs()
check(C.knob[20] == 77 and 99 not in C.knob and C.knob[21] == 127, "knobs reload; unknown CCs dropped, values clamped")
# ---- the box owns its clips: the conductor only signals --------------------------------
reset()
seen = [C.picture(b * 16, []).split("\n")[0] for b in range(3)]   # bars 0-2 = clip bars
check(seen == ["p next 0", "p next 37", "p next 74"], "each clip bar just says 'p next' (+ where to start)", seen)
reset()
C.state["box_clips"] = 0
first = C.picture(0, [])
rest = [C.picture(s, []) for s in range(1, 16)]
check(not first.startswith("p ") and C.state["clip"] is None and all(r for r in rest),
      "the box has no clip loaded (yet, or after a reboot): a clip bar becomes a pattern bar that still moves",
      (first.split("\n")[0], rest[:2]))

# ---- the bus check (follows brain restarts) ----------------------------------------
LISTING = '''Client 135 : "RtMidiOut Client" [User Legacy]
  Port   0 : "ORC1-Synth-Bus" (R-e-) [In]
    Connecting To: 147:0
Client 147 : "orc-boxvis-in" [User Legacy]
  Port   0 : "RtMidi input" (-We-) [Out]
    Connected From: %s
'''
check(C.seq_listening(LISTING % "135:0"), "connected from the current bus -> listening")
check(not C.seq_listening(LISTING % "131:0"), "still on a dead bus (131) after a restart -> reconnect")
# 2026-11-09: mxvbox / visuals knobs come from the ORC's own ports, never the synth bus (it is for synthesis only)
check(C.port_part("RtMidiOut Client:ORC-mxvbox 140:0") == "ORC-mxvbox" and C.ORC_PORT == "ORC-mxvbox"
      and C.ORC_VIS_PORT == "ORC-visuals" and C.port_part("orc-boxvis-ctl:RtMidi input 151:0") != C.ORC_PORT,
      "the ORC ports are matched by their EXACT name (not our own orc-boxvis-* clients, not a look-alike)")
ORC_LISTING = '''Client 140 : "RtMidiOut Client" [User Legacy]
  Port   0 : "ORC-mxvbox" (R-e-) [In]
Client 135 : "RtMidiOut Client" [User Legacy]
  Port   0 : "ORC1-Synth-Bus" (R-e-) [In]
Client 151 : "orc-boxvis-ctl" [User Legacy]
  Port   0 : "RtMidi input" (-We-) [Out]
    Connected From: %s
'''
mine = lambda p: p == C.ORC_PORT
check(C.seq_listening(ORC_LISTING % "140:0", C.CTL_CLIENT, mine)
      and not C.seq_listening(ORC_LISTING % "135:0", C.CTL_CLIENT, mine),
      "the knob listener counts as fed only by 'ORC-mxvbox' (a synth-bus feed does not count)")
_csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "conductor.py"), encoding="utf-8").read()
check("threading.Thread(target=bus_thread, daemon=True).start()" in _csrc
      and "((CTL_CLIENT, ORC_PORT), (VIS_CLIENT, ORC_VIS_PORT))" in _csrc,
      "three listeners: the synth bus (drum hits), ORC-mxvbox (our knobs), ORC-visuals (LISTEN knobs)")

# ---- the step clock: every step once, in order, on time, through a laggy feed ------
def simulate(lock=C.lock_grid, drop=0.15, seed=1):
    rnd = random.Random(seed)
    step_s, lead = 60 / 115.0 / 4, -0.015
    origin = None
    sent, sends, feed_t = -1, [], 0.0
    t = 0.0
    s_held = None
    while t < 16 * 16 * step_s:            # 16 bars; the true grid has step 0 at t = 0
        if t >= feed_t:                     # a feed update: max 30 Hz, some lost, and the
            feed_t = t + 1 / 30.0           # grid it implies is off by +-2 ms (age_ms is
            if rnd.random() > drop:         # whole ms, the write lands a moment later)
                origin, s_held = lock(origin, s_held, rnd.uniform(-0.002, 0.002), step_s)
        if origin is not None:
            k, due = C.next_due(origin, step_s, lead, sent, t)
            if t >= due:
                sends.append((k, t - (k * step_s - lead)))
                sent = k
        t += 0.002
    return sends


sends = simulate()
ks = [k for k, _ in sends]
check(ks == list(range(ks[0], ks[0] + len(ks))) and len(ks) > 250,
      "16 bars through a laggy, lossy feed: every step sent once, in order, none skipped", (ks[:5], len(ks)))
late = max(abs(e) for _, e in sends[1:])
check(late < 0.004, "...and each within 4 ms of its true time (2 ms tick + smoothed +-2 ms noise)", "%.4f" % late)
raw = simulate(lock=lambda origin, step_s, o, s: (o, s))      # NO smoothing: take each update raw
check(max(abs(e) for _, e in raw[1:]) > late, "smoothing the grid beats taking each feed update raw",
      ("%.4f" % max(abs(e) for _, e in raw[1:]), "%.4f" % late))

# ---- the POOL knob (CC 30) -> 'g N' to the box -----------------------------------
C.state.update(pool=None, pool_t=0.0)
C.knob[30] = 0
C.knob[60] = 2
check(C.pool_cmd(1009.0) == "g 0\ng2 2", "SCENE POOL (CC 60) rides along: g2 N = the scenes' own pool")
C.knob[60] = 0
check(C.pool_cmd(1000.0) == "g 0\ng2 0", "pool: told at start (0 = everything)")
check(C.pool_cmd(1001.0) is None, "...not again while nothing changed")
C.knob[30] = 3
check(C.pool_cmd(1002.0) == "g 3\ng2 0", "...at once when the knob moves")
check(C.pool_cmd(1007.5) == "g 3\ng2 0", "...and again every 5 s (UDP can drop, the box can restart)")
C.knob[30] = 0

# ---- the GPU looks (CC 31-38) -> K / R / C / X to the box --------------------------
reset()
C.state.update(looks=None, looks_t=0.0, bps=2.0)
check(C.looks_text() == "K 0 0.0000\nR 0.00\nC 0.0 1.000 0.000 0.0000\nX 0.00\nM 0\nF 0",
      "default looks: every one off / as filmed (the signed-off look is unchanged)", repr(C.looks_text()))
C.knob.update({31: 89, 32: 127, 33: 127, 34: 64, 35: 0, 36: 127, 37: 127, 38: 127})
check(C.looks_text() == "K 6 0.5000\nR 32.00\nC 180.0 0.000 1.000 0.5000\nX 2.00\nM 0\nF 0",
      "KALEIDO 6, SPIN + HUE CYCLE one turn a bar (0.5 turns/s at 120 bpm), RGB 32 px, HUE half way, grey, negative, 2 s crossfade",
      repr(C.looks_text()))
C.state["bps"] = 1.0
check(C.looks_text().split("\n")[0] == "K 6 0.2500", "...SPIN follows the tempo (half the tempo, half the turns/s)")
reset()
C.state.update(looks=None, looks_t=0.0, bps=2.0)
check(C.looks_cmd(2000.0) is not None and C.looks_cmd(2001.0) is None, "looks: told at start, not again while nothing changed")
C.knob[33] = 64
check(C.looks_cmd(2002.0) == C.looks_text() and "R 8.13" in C.looks_text(), "...at once when a knob moves (RGB on a curve: 64 = 8 px)")
check(C.looks_cmd(2007.5) is not None, "...and again every 5 s (a restarted box starts with every look off)")
C.knob[33] = 100
check(C.looks_cmd(2007.51) is None and C.looks_cmd(2007.57) is not None,
      "...built at most every LOOKS_DT (it was every 4 ms loop), so a knob lands within 50 ms")
C.knob[33] = 0
reset()
C.knob.update({50: 100, 45: 0})
C.state["rot"] = dict(C.ROT_NONE, looks=("mask",))
C.fire_hit("kick", 0.0)
check(any(c.startswith("h mask ") for c in SENT), "MASK HIT also throws open ROTATE's mask (it only followed your own)", SENT)
reset()
check(all(cc in K.KNOBS for cc in range(31, 39)) and K.KNOBS[35][1] == 64, "CC 31-38 are mxvbox knobs (SATURATION's default 64 = as filmed)")
reset()

# ---- MASK / PEEPHOLE (CC 45-50) -> M to the box, MASK HIT on the kick ----------------
reset()
C.state.update(looks=None, looks_t=0.0, bps=2.0)
C.knob.update({45: 20, 46: 127, 47: 70, 48: 127, 49: 64})
check(C.looks_text().split("\n")[-2] == "M 1 1.000 2 0.5000 0.504 2.0000",
      "MASK circle, full size, the negative through it, drifting a turn a bar, breathing once a beat", C.looks_text())
C.knob[45] = 127
check(C.looks_text().split("\n")[-2].startswith("M 7 "), "MASK 127 = the dark-parts key (eighths)")
C.knob[45] = 10
check(C.looks_text().split("\n")[-2] == "M 0", "MASK 0-15 = off")
C.knob.update({45: 20, 50: 127, 21: K.REACTIONS.index("off") * 13 + 5})
del SENT[:]
C.fire_hit("kick", 3.0)
check(SENT == ["h mask 0.6 450"], "MASK HIT: the kick opens the hole, even with the kick's own reaction off", SENT)
C.knob[45] = 0
del SENT[:]
C.fire_hit("kick", 4.0)
check(SENT == [], "...but not with the mask off", SENT)
check(all(cc in K.KNOBS for cc in range(45, 51)) and K.KNOBS[45][1] == 0, "CC 45-50 are mxvbox knobs, the mask off by default")
# ---- FRAME / PEEPHOLE (CC 51-57) -> F to the box -------------------------------------
C.knob.update({51: 30, 52: 127, 53: 32, 54: 100, 55: 127, 56: 100})
check(C.looks_text().split("\n")[-1] == "F 1 1.000 0.252 0.787 1.000 1 1",
      "FRAME circle, full size, left of centre and low, a wide soft edge, INVERTED (an area blocked out)", C.looks_text())
C.knob.update({51: 127, 52: 64, 56: 0, 57: 127})
check(C.looks_text().split("\n")[-1].startswith("F 5 1.000 ") and C.looks_text().endswith(" 0 4"),
      "FRAME 127 = your picture (SIZE 64 = the screen), lit inside, picture 4", C.looks_text())
C.knob[51] = 10
check(C.looks_text().split("\n")[-1] == "F 0", "FRAME 0-21 = off: the whole rectangle")
check(all(cc in K.KNOBS for cc in range(51, 58)) and K.KNOBS[51][1] == 0, "CC 51-57 are mxvbox knobs, the frame off by default")
C.knob.update({c_: K.KNOBS[c_][1] for c_ in range(45, 58)})
reset()

# ---- cable or Wi-Fi -------------------------------------------------------------
check(C.pick_link(True, "192.168.1.114") == ("cable", "10.55.0.6"), "cable up: the cable wins, even with Wi-Fi known")
check(C.pick_link(False, "192.168.1.114") == ("wifi", "192.168.1.114"), "cable down: the box's Wi-Fi address")
check(C.pick_link(False, None) == ("none", None), "neither: no address (nothing is sent)")
check(C.keep_wifi("192.168.1.114", None) == "192.168.1.114" and C.keep_wifi("192.168.1.114", "192.168.1.120") == "192.168.1.120"
      and C.keep_wifi(None, None) is None,
      "a failed mDNS lookup keeps the last good address (it dropped out for ~27 min on 2026-11-02)")
check(C.box_ssh("192.168.1.114")[-3:] == ["-o", "HostName=192.168.1.114", "mxvbox"] and C.box_ssh(None) == C.BOX_SSH
      and C.box_ssh("evil; rm -rf /") == C.BOX_SSH,
      "the status call reaches the box at the link's address (never by the flaky name; nothing odd passed through)")
carrier = os.path.join(SANDBOX, "carrier")
for val, want in (("1\n", True), ("0\n", False)):
    with open(carrier, "w") as f:
        f.write(val)
    check(C.cable_up(carrier) is want, "cable_up reads eth0's carrier: %r" % val.strip())
check(C.cable_up(os.path.join(SANDBOX, "nope")) is False, "cable_up: no carrier file = no cable")
sent = []
real_sock, C.sock = C.sock, type("S", (), {"sendto": lambda self, d, a: sent.append(a)})()
saved = dict(C.link)
C.link["addr"] = None
REAL_SEND("c 000000")
C.link["addr"] = "192.168.1.114"
REAL_SEND("c 000000")
C.sock = real_sock
C.link.update(saved)
check(sent == [("192.168.1.114", 9000)], "send: dropped while there is no link, else to the current address", sent)

# ---- LISTEN: the box reacts to what the visuals' LISTEN tab hears ---------------------
reset()
check(K.KNOBS[58][1] == 109 and C.listen_style(58) is None and C.listen_style(59) is None and C.listen_hits(1.0, 1.0, True) == [],
      "LISTEN STYLE / LISTEN 2 STYLE are off by default: no hits")
C.knob[58] = K.REACTIONS.index("rgb") * 13 + 5
C.knob[59] = K.REACTIONS.index("invert") * 13 + 5
h1 = C.listen_hits(1.0, 0.0, False)
h2 = C.listen_hits(0.0, 1.0, True)
check(len(h1) == 2 and h1[0].startswith("h rgb ") and h1[1].startswith("h invert ") and h2 == [C.hit_cmd("invert")],
      "a band-1 beat -> LISTEN STYLE (and LISTEN 2 too while the background has no own band); a band-2 beat -> LISTEN 2", (h1, h2))
C.state["vis_ch"] = 16
C.vis.clear()
C.on_midi(([0xB0 | 15, 32, 20], 0))                      # the visuals channel: LISTEN's band low edge
C.on_midi(([0xB0 | 14, 33, 99], 0))                      # another channel: not LISTEN's
C.on_midi(([0xB0 | 15, 40, 99], 0))                      # the visuals channel, but not a LISTEN knob
check(C.vis == {32: 20}, "LISTEN's band / SENS / GATE are followed live on the visuals module's channel", C.vis)


class FakeTap(object):
    source, band2_on = "tap", False

    def __init__(self):
        self.beats = [(0.0, 0.0, 0.1), (0.8, 0.0, 0.5), (0.0, 0.0, 0.2)]
        self.knobs = None

    def update(self, now):
        return self.beats.pop(0) if self.beats else (0.0, 0.0, 0.0)


taps = []


def make_fake():
    taps.append(FakeTap())
    return taps[-1]


def applied(tap, k):
    tap.knobs = dict(k)


naps = []


def fake_sleep(s):
    naps.append(s)
    if len(naps) >= 4:
        raise KeyboardInterrupt                         # (ends the loop)


del SENT[:]
C.vis.update({31: 64, 32: 8, 33: 38, 54: 56, 59: 8, 60: 38, 61: 64, 62: 0})
try:
    C.listen_thread((make_fake, applied), fake_sleep)
except KeyboardInterrupt:
    pass
check(SENT == [C.hit_cmd("rgb"), C.hit_cmd("invert")] and taps[0].knobs == C.vis and naps[0] == C.LISTEN_DT,
      "the listener: one LISTEN beat = one hit per style, the detector set from LISTEN's knobs, ~80 looks a second", (SENT, naps))
C.knob[58] = C.knob[59] = 109
del naps[:]
del SENT[:]
try:
    C.listen_thread((make_fake, applied), fake_sleep)
except KeyboardInterrupt:
    pass
check(SENT == [] and naps[0] == 0.25, "both LISTEN styles off: it doesn't even look (costs nothing)", (SENT, naps))


class BadOnceTap(FakeTap):
    def update(self, now):
        if not getattr(self, "failed", False):
            self.failed = True
            raise TypeError("a bad heads.json entry")      # (vis_listen's int(None), the review's case)
        return (0.8, 0.0, 0.5)


C.knob[58] = 10                                          # LISTEN STYLE = blur
del naps[:]
del SENT[:]
try:
    C.listen_thread((BadOnceTap, applied), fake_sleep)
except KeyboardInterrupt:
    pass
check(C.hit_cmd("blur") in SENT, "LISTEN carries on after an error (the thread used to die until a restart)", (SENT, naps))
C.knob[58] = C.knob[59] = 109
del SENT[:]

# ---- WHEN STOPPED + a stale feed (2026-11-03) ---------------------------------------
T0 = 1000.0
live = {"t": T0, "transport": {"running": True, "step": 5}}
check(C.is_running(live, T0 + 1) and not C.is_running(live, T0 + C.STALE_S + 0.5),
      "a fresh playing feed runs; one older than STALE_S (the brain gone) counts as stopped")
check(not C.is_running({"t": T0, "transport": {"running": False, "step": 5}}, T0) and not C.is_running(None, T0),
      "stopped / no feed = not running")
picks = []
for v in (0, 42, 43, 85, 86, 127):
    C.knob[70] = v
    picks.append(C.when_stopped())
check(picks == ["black", "black", "hold", "hold", "free", "free"], "WHEN STOPPED in thirds: black / hold / free", picks)
C.knob[70] = 0
C.state["vis_ch"] = None
reset()

# ---- ROTATE: the box picks which looks play, bar by bar ------------------------------
reset()
for b_ in range(8):
    C.rotate_tick(b_)
check(C.state["rot"]["looks"] == () and C.looks_text().split("\n")[0] == "K 0 0.0000", "ROTATE off by default: no picks")
C.knob[61] = 60                                          # every 2 bars
picks = []
for b_ in range(16):
    C.rotate_tick(b_)
    picks.append(C.state["rot"]["looks"])
changes = [i for i in range(1, 16) if picks[i] != picks[i - 1]]
check(changes == [2, 4, 6, 8, 10, 12, 14] and all(len(p_) == 1 and p_[0] in C.ROT_LOOKS for p_ in picks),
      "ROTATE every 2 bars, HOW MANY 1: a new single look on every other bar, never the same twice running", picks[::2])
C.knob.update({62: 127, 63: 0, 71: 127, 72: 127, 73: 0, 74: 0, 75: 0})   # (the allow CCs moved off 64-68)
seen = set()
for b_ in range(16, 40, 2):
    C.rotate_tick(b_)
    seen.update(C.state["rot"]["looks"])
check(seen == set(["rgb", "hue"]), "only the ALLOWED looks come up (HOW MANY 3, but only RGB + HUE allowed)", seen)
C.state["rot"] = {"looks": ("kaleido", "mask", "rgb"), "kick": "zoom", "snare": "hue",
                  "p": {"kaleido": 6, "rgb": 8.0, "hue": 120.0, "mask": (2, 0)}}
lt = C.looks_text().split("\n")
check(lt[0].startswith("K 6 ") and lt[1] == "R 8.00" and lt[4].startswith("M 2 0.450 0 "),
      "a pick shows up in the looks: kaleido 6, RGB 8 px, a diamond mask with another clip", lt)
C.knob.update({31: 60, 33: 127, 45: 30})
lt = C.looks_text().split("\n")
check(lt[0].startswith("K 4 ") and lt[1] == "R 32.00" and lt[4].startswith("M 1 "),
      "...but a look you set yourself always wins (your KALEIDO 4, RGB 127, MASK circle)", lt)
C.knob.update({31: 0, 33: 0, 45: 0})
check(C.hit_style("kick") == C.REACTION_DEFAULT_KICK if hasattr(C, "REACTION_DEFAULT_KICK") else C.hit_style("kick") != "zoom",
      "ROTATE HITS off: KICK / SNARE STYLE stay as set")
C.knob[76] = 127                                       # (ROTATE HITS; was CC 69 until 2026-11-03)
check(C.hit_style("kick") == "zoom" and C.hit_style("snare") == "hue", "ROTATE HITS on: the kick and snare reactions follow the pick")
C.knob[61] = 0
C.rotate_tick(41)
check(C.state["rot"]["looks"] == () and C.hit_style("kick") != "zoom", "ROTATE back to off: no picks, the styles as set again")
reset()

# ---- ROTATE's knobs moved off the sustain / pedal CCs (2026-11-03) ------------------
check(not any(64 <= cc <= 69 for cc in K.KNOBS), "no box knob sits on CC 64-69 (sustain / pedal CCs)")
old = os.path.join(SANDBOX, "old_knobs.json")
with open(old, "w") as f:
    json.dump({"61": 63, "64": 127, "66": 0, "69": 20}, f)      # his real saved values had INVERT = 0
reset()
C.load_knobs(old)
with open(old) as f:
    after = json.load(f)
check(C.knob[73] == 0 and C.knob[71] == 127 and C.knob[76] == 20 and after.get("73") == 0 and C.knob[61] == 63,
      "saved values on the old CCs move to the new ones once (INVERT not allowed stays not allowed), and are saved",
      (C.knob[71], C.knob[73], C.knob[76], after))
reset()

# ---- the real files were never touched -------------------------------------------
check([md5(p) for p in REAL] == real_before, "real ~/.orc1_mxvbox*.json untouched")

total = passed + failed
print("%d passed, %d failed  (expected %d)" % (passed, failed, EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed == 0 and total == EXPECTED else 1)
