#!/usr/bin/env python3
"""Regression suite for the Game Boy wave channel (orc1_synth.GameboyWaveVoice).

    python3 _test_gb_wave.py [path/to/orc1_synth.py]

EXPECTED = 91 checks. A LOWER total is a regression even when it says
"0 failed" -- a check that vanishes reads exactly like a check that passes
(CLAUDE.md, session 9).

SAFETY: touches no hardware, no JACK graph, no files in ~. It reads
orc1_synth.py as TEXT and execs only the wave-channel class and its
constants via ast.get_source_segment -- importing orc1_synth would open the
audio device. Safe to run while the instrument is being played.

All six 2026-08-04 de-click fixes are mutation-tested: reverting any one of
them (release stage, DC-free DAC, phase continuity, table crossfade, gain
ramp, sustain) turns this suite red.

A "pop" is an inter-sample step LARGER THAN THE WAVEFORM'S OWN EDGES -- a
square wave legitimately steps 2.0 every half cycle, so an absolute step
limit is meaningless here (that mistake produced 15 bogus failures on the
first run of this probe).  Every transition is therefore measured against a
steady-state baseline captured from the same voice in the same config.

Extracts the class from the deployed source by AST -- never imports
orc1_synth (which opens hardware).
"""
import ast, os, sys, numpy as np

EXPECTED = 91

_here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1_synth.py")
SRC = sys.argv[1] if len(sys.argv) > 1 else _here
src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

WANT_FN = {"_midi_to_hz", "_cc_to_time", "_gb_harmonic_table"}
WANT_CLS = {"GameboyWaveVoice"}
WANT_ASSIGN = {"_GB_WAVE_PRESETS", "_GB_VOL_GAINS", "_GB_GAIN_RAMP_S",
               "_GB_XFADE_S", "_GB_PANIC_FADE_S",
               # 2026-09-24 wave shaping + slots
               "_GB_HARM_CC0", "_GB_HARM_N", "_GB_STORE_CC", "_GB_N_SLOTS"}
ns = {"np": np}
picked = []
for node in tree.body:
    take = False
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FN: take = True
    elif isinstance(node, ast.ClassDef) and node.name in WANT_CLS: take = True
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_ASSIGN: take = True
    if take:
        picked.append(ast.get_source_segment(src, node))
exec("\n\n".join(picked), ns)
GBW  = ns["GameboyWaveVoice"]
PRE  = ns["_GB_WAVE_PRESETS"]
NAMES = ("triangle","sine","saw_up","saw_dn","square","pulse25","double_saw","warmth")

SR, N, NOTE = 48000, 128, 45      # A2-ish: several cycles per block
def run(blocks, events=()):
    v = GBW(SR); ev = dict(events); out = []
    for b in range(blocks):
        for fn in ev.get(b, []): fn(v)
        out.append(v.render(N))
    return np.concatenate(out), v

def mstep(sig):
    return float(np.abs(np.diff(sig)).max()) if len(sig) > 1 else 0.0

FAILS = []
RAN   = []
def check(label, val, limit, unit="step"):
    ok = val <= limit
    RAN.append(label)
    print(f"{'ok  ' if ok else 'FAIL'}  {label:<48} {unit} {val:.4f}  (limit {limit:.4f})")
    if not ok: FAILS.append(label)

def check_min(label, val, floor, unit="val"):
    """For assertions of the form 'this must be at LEAST x'. Without it every
    check has to be squeezed into `val <= limit`, which is how this suite
    ended up with `CC7 is continuous` comparing a distance that can never
    exceed 0.125 against a limit of 1.0 -- a check that could not fail."""
    ok = val >= floor
    RAN.append(label)
    print(f"{'ok  ' if ok else 'FAIL'}  {label:<48} {unit} {val:.4f}  (min {floor:.4f})")
    if not ok: FAILS.append(label)

# ⚠ THE CEILING THAT MAKES A max-step CHECK MEAN ANYTHING.
# Every preset spans the full 0-15 table range, so `_dac` output spans exactly
# 2.0, and env<=1 * gain<=1 means NO signal this voice can produce steps more
# than 2.0. For the five hard-edged presets the steady-state baseline IS 2.0,
# so `base*1.02 + 0.02` = 2.06 was above the ceiling: 10 checks could not fail,
# and reverting the release stage on square/pulse25/double_saw passed green.
# Worse, the metric inverts there -- an instant cutoff REMOVES the waveform,
# so it removes the big edges too and scores BETTER than the fix. Hence both
# the cap here and the envelope-shape checks further down, which do not use
# max-step at all.
STEP_CEILING = 1.95
def limit_for(base, env_peak=1.0):
    """Limit for a max-step check whose steady-state baseline is `base`.

    `env_peak` is how much louder the envelope can legitimately be during the
    window than it was when `base` was measured -- a retrigger re-enters
    ATTACK, which peaks at 1.0 while a note at the shipping sustain of 0.8
    was measured at 0.8, so its own edges are legitimately 1.25x bigger.
    Without that the default-sustain checks fail on the envelope, not a click.

    The cap can never sit BELOW the baseline: on a preset whose own edge is
    already the full 2.0 span, capping at 1.95 would fail the waveform for
    existing. Those presets are simply not judgeable by max-step -- which is
    the point of the release-tail and CC-1-identity checks that do not use it.
    """
    return min(base * env_peak * 1.02 + 0.02, max(STEP_CEILING, base * env_peak))

# Every test runs at sustain = 1.0 (CC 10 = 127). The envelope's attack peak
# is 1.0 while its sustain is lower, so at the default sustain the attack
# peak's edges are legitimately ~1/sustain LARGER than a steady-state
# baseline -- which reads as a false "onset click" (8 of them, first run).
FLAT = lambda v: v.set_cc(10, 127)

print("== per-preset: onset / release / DC / steady-state baseline ==")
for pi, pname in enumerate(NAMES):
    sig, v = run(400, {0: [FLAT, lambda v, p=pi: v.set_cc(1, p*16+8)],
                       2: [lambda v: v.note_on(NOTE, 100)],
                       200:[lambda v: v.note_off(NOTE)]})
    base = mstep(sig[100*N:150*N])              # the waveform's own worst edge
    lim  = limit_for(base)
    check(f"{pname}: note-on onset",     mstep(sig[2*N-2 : 6*N]),     lim)
    check(f"{pname}: note-off release",  mstep(sig[200*N-2:206*N]),   lim)
    check(f"{pname}: silent after release", float(np.abs(sig[-20*N:]).max()), 1e-4, "peak")
    check(f"{pname}: inactive after release", 0.0 if v._active is False else 1.0,
          0.0, "active?")
    # ⚠ ENVELOPE SHAPE, not max-step. On a hard-edged preset an instant cutoff
    # is INVISIBLE to max-step (it removes the waveform's own big edges along
    # with the sound), so the release stage is asserted here by its DURATION:
    # the tail must still be audible well after note-off. Reverting the
    # release stage on square/pulse25/double_saw passed the whole suite green
    # before this check existed.
    tail = float(np.abs(sig[203*N:206*N]).max())
    check_min(f"{pname}: release tail is a fade, not a cut", tail, 0.05, "peak")
    # The voice must play the preset it was ASKED for. Every limit above is
    # derived from the rendered signal itself, so if CC 1 selects the wrong
    # table the baseline moves with it and the suite proves only "whatever
    # it picked is smooth" -- `idx = value // 16` -> `// 8` passed green.
    check(f"{pname}: CC 1 selected this preset",
          0.0 if list(v._wave) == list(PRE[pi]) else 1.0, 0.0, "wrong?")
    # DC: the DAC conversion itself must be zero-mean over the whole table
    tbl = np.asarray(PRE[pi], dtype=np.float32)
    dac = GBW._dac(tbl, float(tbl.mean()), np.arange(32))
    check(f"{pname}: table DC after DAC", abs(float(dac.mean())), 1e-6, "mean")

print("\n== transitions ==")
# Baseline = max steady-state step BEFORE and AFTER the transition. A swap
# lands on a new waveform whose own edges may be far bigger than the old
# one's (triangle -> pulse25 goes from 0.13 to 2.0), so a before-only
# baseline reports the new waveform itself as a click.
def transition(label, setup, at, action, win=1400, flat=True):
    pre = ([FLAT] if flat else []) + setup
    sig, _ = run(400, {0: pre, 2: [lambda v: v.note_on(NOTE, 100)], at: action})
    base = max(mstep(sig[(at-40)*N:(at-5)*N]), mstep(sig[(at+60)*N:(at+120)*N]))
    # Off FLAT the note sits at the default sustain (0.8) but a retriggered
    # attack still peaks at 1.0 -- see limit_for's env_peak.
    peak = 1.0 if flat else 1.0 / 0.8
    check(label, mstep(sig[at*N-2 : at*N+win]), limit_for(base, peak))

transition("retrigger while sounding (pulse25)",
           [lambda v: v.set_cc(1, 88)], 200, [lambda v: v.note_on(NOTE, 100)])
transition("preset swap mid-note (tri -> pulse25)",
           [lambda v: v.set_cc(1, 8)], 200, [lambda v: v.set_cc(1, 88)])
transition("preset swap mid-note (square -> sine)",
           [lambda v: v.set_cc(1, 72)], 200, [lambda v: v.set_cc(1, 24)])
transition("preset swap mid-note (tri -> sine, both smooth)",
           [lambda v: v.set_cc(1, 8)], 200, [lambda v: v.set_cc(1, 24)])
transition("preset swap mid-note (sine -> warmth, both smooth)",
           [lambda v: v.set_cc(1, 24)], 200, [lambda v: v.set_cc(1, 120)])
# Volume + retrigger tests use SINE deliberately: a square/pulse table's own
# 2.0 edge is as big as any click those paths could produce, so the check
# would have no teeth there (verified -- the phase-reset mutation survives a
# pulse25 retrigger test and dies on this one).
transition("volume drop mid-note (continuous, sine)",
           [lambda v: v.set_cc(22, 0), lambda v: v.set_cc(1, 24)],
           200, [lambda v: v.set_cc(7, 20)])
transition("volume drop mid-note (authentic 4-step, sine)",
           [lambda v: v.set_cc(22, 127), lambda v: v.set_cc(1, 24)],
           200, [lambda v: v.set_cc(7, 20)])
# Retrigger must be measured on TRIANGLE, at several retrigger instants.
# Table index 0 is the phase-reset landing point: on sine that index is the
# table's MIDPOINT (~zero crossing), so a phase reset there is nearly
# silent and the check has no teeth -- the "always reset phase" mutation
# escaped a sine-based test. Triangle's index 0 is a DAC extreme, and
# sweeping the instant guarantees at least one reset lands mid-cycle.
worst_rt, base_rt = 0.0, 0.0
for at in (200, 201, 202, 203, 204):
    sig, _ = run(300, {0: [FLAT, lambda v: v.set_cc(1, 8)],
                       2: [lambda v: v.note_on(NOTE, 100)],
                       at: [lambda v: v.note_on(NOTE, 100)]})
    worst_rt = max(worst_rt, mstep(sig[at*N-2 : at*N+600]))
    base_rt  = max(base_rt,  mstep(sig[(at-40)*N:(at-5)*N]))
check("retrigger while sounding (triangle, 5 instants)",
      worst_rt, limit_for(base_rt))
# ⚠ THE SAME TWO GUARDS, AT THE SHIPPING DEFAULT SUSTAIN (0.8) RATHER THAN
# FLAT. Both are `_env_level` predicates, and every check above runs at
# sustain 1.0 -- the one value that makes them look armed no matter what
# they compare against. Widening the phase-continuity guard to `< 1.0` and
# the crossfade-arming guard to `> 0.999` (i.e. reverting both fixes for
# every sustain below full) passed the entire suite green before this ran.
transition("retrigger at DEFAULT sustain (triangle)",
           [lambda v: v.set_cc(1, 8)], 200, [lambda v: v.note_on(NOTE, 100)],
           flat=False)
transition("preset swap at DEFAULT sustain (tri -> sine)",
           [lambda v: v.set_cc(1, 8)], 200, [lambda v: v.set_cc(1, 24)],
           flat=False)
transition("custom-table activate mid-note",
           [lambda v: v.set_cc(1, 72)], 200, [lambda v: v.set_cc(23, 127)])
transition("custom Wave RAM edit mid-note",
           [lambda v: v.set_cc(1, 8), lambda v: v.set_cc(23, 127)],
           200, [lambda v: v.set_cc(20, 0), lambda v: v.set_cc(21, 64)])

# White-box: prove the crossfade MECHANISM arms on each swap path, so the
# black-box numbers above are not passing merely because the new waveform
# hides the step under its own edges.
for lbl, act in (("CC1 preset select", lambda v: v.set_cc(1, 88)),
                 ("CC23 custom activate", lambda v: v.set_cc(23, 127)),
                 ("CC21 live Wave RAM edit", None)):
    v = GBW(SR); v.set_cc(10, 127); v.note_on(NOTE, 100)
    for _ in range(200): v.render(N)
    if act is None:
        v.set_cc(23, 127)
        for _ in range(60): v.render(N)
        v._xf_left = 0; v._xf_prev = None
        v.set_cc(20, 0); v.set_cc(21, 64)
    else:
        act(v)
    armed = v._xf_left > 0 and v._xf_prev is not None
    check(f"crossfade armed by {lbl}", 0.0 if armed else 1.0, 0.0, "armed?")

# The cached table must track a live Wave RAM edit, or the drawing is silent.
v = GBW(SR); v.set_cc(10, 127); v.note_on(NOTE, 100); v.render(N)
v.set_cc(23, 127); v.render(N)
v.set_cc(20, 0); v.set_cc(21, 127)
check("CC21 edit reaches the cached table", abs(float(v._tbl[0]) - 15.0), 1e-6, "err")
check("CC21 edit updates the cached mean",
      abs(v._tbl_mean - float(np.asarray(v._custom, dtype=np.float32).mean())),
      1e-6, "err")

print("\n== 2026-08-04 review round 2: the fixes for what the reviewers found ==")

# R1. A CC 7 change made while SILENT must land immediately, not ramp into
# the next note under an already-open envelope. Measured +21 dB overshoot,
# and a 0.74 burst after asking for MUTE.
for cc7, lbl in ((8, "quiet"), (0, "mute")):
    v = GBW(SR); v.set_cc(10, 127); v.set_cc(1, 24)
    v.note_on(NOTE, 100); [v.render(N) for _ in range(60)]
    v.note_off(NOTE);     [v.render(N) for _ in range(200)]
    v.set_cc(7, cc7)                      # volume moved between phrases
    v.note_on(NOTE, 100)
    sig = np.concatenate([v.render(N) for _ in range(300)])
    first  = float(np.abs(sig[:int(0.0025*SR)]).max())
    steady = float(np.abs(sig[int(0.2*SR):int(0.5*SR)]).max())
    check(f"CC7 set while silent -> no overshoot on next note ({lbl})",
          first, max(steady, 1e-4) * 1.10 + 1e-4, "peak")

# R2. sustain = 0 must end the note, not park it in 'sustain' at level 0 with
# _active True forever (measured 5.5% of one core, permanently).
v = GBW(SR); v.set_cc(1, 24); v.set_cc(10, 0)
v.note_on(NOTE, 100); [v.render(N) for _ in range(400)]
check("sustain=0 decays to IDLE, not a silent active voice",
      0.0 if (v._active is False and v._env_state == 'idle') else 1.0,
      0.0, "stuck?")

# R3. Moving CC 10 under a held note must ramp, not step.
worst_s, base_s = 0.0, 0.0
for pad in range(4):
    v = GBW(SR); v.set_cc(1, 8); v.set_cc(10, 127)
    v.note_on(NOTE, 100)
    a = np.concatenate([v.render(N) for _ in range(60 + pad)])
    v.set_cc(10, 40)
    b = np.concatenate([v.render(N) for _ in range(30)])
    worst_s = max(worst_s, mstep(np.concatenate([a[-4:], b[:1200]])))
    base_s  = max(base_s,  mstep(a[-4000:]))
check("sustain moved under a held note ramps", worst_s, limit_for(base_s))

# R4. A swap DURING a fade must continue from the mix actually being heard,
# not discard it (~41% of the discontinuity leaked back; on a Wave RAM drag
# that is every gesture step).
# The interruption instant must be SWEPT: the leaked step is proportional to
# how far apart the two tables are AT THAT PHASE, so a single instant can
# land where they happen to agree and the check has no teeth (verified --
# disabling the blend escaped a single-instant version of this check).
# ⚠ The interruption must land INSIDE the fade. The fade is
# _GB_XFADE_S * SR = 240 samples, so sweeping in whole 128-sample blocks
# only ever interrupts once (at 128) -- every later gap lands after the fade
# has already finished, which is a plain sequential swap and proves nothing.
# Verified: a block-granular version of this check let the blend be disabled
# entirely and stayed green. Render in 16-sample chunks instead.
worst_x, base_x = 0.0, 0.0
XF_N = int(ns["_GB_XFADE_S"] * SR)
for off in range(16, XF_N, 16):
    v = GBW(SR); v.set_cc(10, 127); v.set_cc(1, 8)
    v.note_on(NOTE, 100)
    a = np.concatenate([v.render(N) for _ in range(60)])
    v.set_cc(1, 24)                                   # first swap: tri -> sine
    b = np.concatenate([v.render(16) for _ in range(off // 16)])
    v.set_cc(1, 120)                                  # interrupt it, mid-fade
    c = np.concatenate([v.render(N) for _ in range(30)])
    worst_x = max(worst_x, mstep(np.concatenate([b[-4:], c[:1200]])))
    # Baseline from BOTH ends: the window lands on warmth, whose own edges
    # differ from triangle's, so a before-only baseline reports the
    # destination waveform as a click (the trap transition() documents).
    base_x  = max(base_x, mstep(a[-4000:]), mstep(c[-2000:]))
check("swap interrupting a fade (14 instants)", worst_x, limit_for(base_x))

# CC 20 must latch the step CC 21 will write -- across the range, not just at
# 0. (`* 32 // 128` -> `// 127` is an EQUIVALENT mutation, identical for all
# 128 input values; `// 64` is not, and this is what catches it.)
v = GBW(SR)
worst_cwi = 0
for raw in (0, 4, 40, 64, 100, 127):
    v.set_cc(20, raw)
    worst_cwi = max(worst_cwi, abs(v._cwi - min(31, raw * 32 // 128)))
check("CC 20 latches the right Wave RAM step", float(worst_cwi), 0.0, "err")

# R5. Crossfade state must not survive the note that armed it.
v = GBW(SR); v.set_cc(1, 8); v.set_cc(10, 127); v.set_cc(11, 0)
v.note_on(NOTE, 100); [v.render(N) for _ in range(40)]
v.note_off(NOTE)
armed = False
for _ in range(400):
    if not armed and 1e-4 < v._env_level < 3e-3:
        v.set_cc(1, 72); armed = True
    v.render(N)
    if not v._active: break
check("no crossfade state left after release", float(v._xf_left), 0.0, "xf_left")

# R6. PANIC must be immediate even with a 3-second CC 11 release.
v = GBW(SR); v.set_cc(10, 127); v.set_cc(11, 127)   # release = 3.0s
v.note_on(NOTE, 100); [v.render(N) for _ in range(60)]
v.silence()
sig = np.concatenate([v.render(N) for _ in range(8)])   # ~21ms
check("silence() quiets the voice within ~20ms",
      float(np.abs(sig[int(0.015*SR):]).max()), 1e-3, "peak")
check("  ... and CC 11 is not clobbered by it", abs(v.release - 3.0), 0.01, "err")

# R7. Absolute level. Nothing else asserts it, so changing the _dac divisor
# from 7.5 to 8.0 -- the whole voice 6% quiet -- passed green.
v = GBW(SR); v.set_cc(10, 127); v.set_cc(1, 8)     # triangle, flat, full gain
v.note_on(NOTE, 100)
sig = np.concatenate([v.render(N) for _ in range(80)])
check("absolute output level (triangle, gain 1.0)",
      abs(float(np.abs(sig[-4000:]).max()) - 1.0), 0.02, "err")

# R8. pitch_mult must actually reach the phase increment: dropping it from
# render() kills bend and vibrato on this channel, silently.
def _cycles(v, blocks=40):
    s = np.concatenate([v.render(N) for _ in range(blocks)])
    s = s[-3000:]
    return int(np.count_nonzero(np.diff(np.signbit(s))))
v = GBW(SR); v.set_cc(10, 127); v.set_cc(1, 24); v.note_on(NOTE, 100)
c1 = _cycles(v)
v.pitch_mult = 2.0
c2 = _cycles(v)
check_min("pitch_mult reaches the phase increment", c2 / max(c1, 1), 1.8, "ratio")

print("\n== restrictions lifted ==")
sig, v = run(600, {2: [lambda v: v.note_on(NOTE, 100)]})
check("held note still sounding after 1.5s (sustain works)",
      0.30 - min(float(np.abs(sig[-20*N:]).max()), 0.30), 0.29, "peak-def")
sig, v = run(400, {2: [lambda v: v.set_cc(11, 110)],       # long release
                   4: [lambda v: v.note_on(NOTE, 100)],
                   200:[lambda v: v.note_off(NOTE)]})
check_min("release tail audible 0.16s after note-off",
          float(np.abs(sig[260*N:280*N]).max()), 0.05, "peak")
# ...and the SHIPPING DEFAULT release, with no CC 11 sent at all. Without
# this, shortening the default from 150ms to 0.5ms goes unnoticed, because
# the only other release-length check sets CC 11 = 110 first.
sig, v = run(400, {2: [lambda v: v.note_on(NOTE, 100)],
                   200:[lambda v: v.note_off(NOTE)]})
check_min("default release (no CC 11) is still a fade",
          float(np.abs(sig[201*N:203*N]).max()), 0.05, "peak")
v = GBW(SR); v.set_cc(22, 0); v.set_cc(7, 50)
v.note_on(NOTE, 100); [v.render(N) for _ in range(200)]
g = v._gain(N)
# The old form of this check measured the distance to the nearest ladder step
# against a limit of 1.0 -- but that distance can never exceed 0.125, so it
# passed for every value the code can produce. It needs a FLOOR, not a limit.
check_min("CC7 is continuous (far from every 4-step value)",
          min(abs(float(np.mean(g)) - s) for s in (0.0, 0.25, 0.5, 1.0)),
          0.05, "dist")
check("  ... and equals cc/127", abs(float(np.mean(g)) - 50/127.0), 1e-6, "err")
v = GBW(SR); v.set_cc(22, 127); v.set_cc(7, 50)
v.note_on(NOTE, 100); [v.render(N) for _ in range(200)]
check("CC22 restores the authentic 4-step ladder",
      abs(float(np.mean(v._gain(N))) - 0.25), 1e-6, "err")
v = GBW(SR); v.set_cc(24, 127); v.note_on(NOTE, 100)
[v.render(N) for _ in range(50)]
v.note_on(NOTE, 100)
check("CC24 restores authentic phase reset", abs(v._phase), 1e-9, "phase")

print()
for f in FAILS: print("   FAILED -", f)
print(f"{len(RAN) - len(FAILS)} passed, {len(FAILS)} failed  "
      f"({len(RAN)} checks ran, expected {EXPECTED})")
short = len(RAN) != EXPECTED
if short:
    print(f"!! CHECK COUNT IS {len(RAN)}, EXPECTED {EXPECTED} -- a check was "
          f"added or silently lost. Update EXPECTED deliberately or find it.")
sys.exit(1 if (FAILS or short) else 0)
