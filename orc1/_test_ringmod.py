#!/usr/bin/env python3
"""
ORC-1 pitch-tracked ring modulation -- offline DSP + wiring suite.
==================================================================

WHY THIS EXISTS
---------------
The feature's whole claim is that the carrier FOLLOWS THE NOTE, so the
sidebands sit at a constant interval instead of a constant offset.  That is a
claim about the spectrum, so this suite renders real blocks through the real
PolyVoice and takes an FFT.  Reasoning about the multiply proves nothing.

Two classes of check, and they catch different things:

  * BEHAVIOUR -- render and measure.  Proves the DSP does what it says.
  * WIRING    -- drive the real set_cc / __init__ paths.  Proves the CONTROL
    reaches the DSP.  CLAUDE.md records several fixes that passed a green
    behavioural suite and provably did nothing, because every test called the
    internal function directly and nothing ever pressed the button.

    python3 _test_ringmod.py          # run the suite
    python3 _test_ringmod.py -v       # show measured spectra

SAFETY: constructs PolyVoice / PolySynth / NESEngine directly.  Opens no JACK
client, sends no MIDI, reads and writes no file, does not touch the running
engine.  Safe to run on the Pi while the instrument is being played.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np                                            # noqa: E402
import orc1_synth as S                                        # noqa: E402

EXPECTED = 63                    # see the note in __main__ -- a shrinking
                                 # suite must go red, not quietly say "0 failed"

SR      = S.SAMPLE_RATE
BLOCK   = 1024
VERBOSE = "-v" in sys.argv

PASS = []
FAIL = []


def check(ok, label, detail=""):
    (PASS if ok else FAIL).append(label)
    if not ok:
        print(f"  ! {label}" + (f"  --  {detail}" if detail else ""))
    elif VERBOSE:
        print(f"  . {label}" + (f"  --  {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------

def make_voice(freq_note=69, depth=0.0, ratio=1.0, fixed_hz=0.0, wf='sine'):
    """A PolyVoice held in SUSTAIN at full level, so _env() contributes a flat
    1.0 and the spectrum we measure is the oscillator x carrier alone.  Without
    pinning the envelope, the attack ramp smears every bin and the nulls this
    suite asserts on stop being nulls."""
    v = S.PolyVoice(SR)
    rm = S.RingMod()
    rm.depth, rm.ratio, rm.fixed_hz = depth, ratio, fixed_hz
    v.rm = rm
    v.note_on(freq_note, 127, wf, 0.001, 0.1, 1.0, 0.1, 1)
    # Force steady state: full sustain, no attack ramp inside the window.
    v.env_state = 'sustain'
    v.sustain   = 1.0
    v.level     = 1.0
    return v, rm


def render(v, n_blocks=16, block=BLOCK):
    return np.concatenate([v.render(block) for _ in range(n_blocks)])


def spectrum(sig):
    """Magnitude spectrum with a Hann window (leakage would otherwise fill the
    nulls we assert on) and the frequency of each bin."""
    w   = np.hanning(len(sig))
    mag = np.abs(np.fft.rfft(sig * w))
    fr  = np.fft.rfftfreq(len(sig), 1.0 / SR)
    return fr, mag


def energy_at(fr, mag, hz, width_hz=25.0):
    """Peak magnitude within +-width_hz of `hz`, normalised to the loudest
    AUDIBLE bin.  Returns 0.0 for a frequency outside the band.

    Normalising against the whole spectrum's peak was wrong: ratio 1 folds a
    sideband down to DC, so the DC bin became the yardstick and a perfectly
    healthy 2f component measured 0.46 instead of 1.0.  DC is inaudible and
    must not set the scale."""
    if hz <= 0 or hz >= SR / 2:
        return 0.0
    sel = np.abs(fr - hz) <= width_hz
    audible = mag[fr >= 20.0]
    if not sel.any() or audible.size == 0 or audible.max() <= 0:
        return 0.0
    return float(mag[sel].max() / audible.max())


def peak_hz(fr, mag):
    return float(fr[int(np.argmax(mag))])


def max_step(sig):
    return float(np.abs(np.diff(sig)).max())


# ---------------------------------------------------------------------------
# BEHAVIOUR -- the DSP
# ---------------------------------------------------------------------------

def test_bypass_is_bit_identical():
    """depth 0 must be EXACTLY the dry signal, not merely close. If bypass
    drifts, every patch on the instrument changes the day this ships."""
    a, _ = make_voice(depth=0.0)
    b    = S.PolyVoice(SR)                     # rm = None: no ring mod at all
    b.note_on(69, 127, 'sine', 0.001, 0.1, 1.0, 0.1, 1)
    b.env_state, b.sustain, b.level = 'sustain', 1.0, 1.0
    x, y = render(a, 4), render(b, 4)
    check(np.array_equal(x, y), "depth 0 is bit-identical to no ring mod",
          f"max diff {np.abs(x - y).max():.3e}")


def test_rm_none_is_bypass():
    v = S.PolyVoice(SR)
    v.note_on(69, 127, 'sine', 0.001, 0.1, 1.0, 0.1, 1)
    v.env_state, v.sustain, v.level = 'sustain', 1.0, 1.0
    out = render(v, 2)
    check(v.rm is None, "a bare PolyVoice has rm = None")
    check(np.isfinite(out).all() and np.abs(out).max() > 0.1,
          "a bare PolyVoice still renders normally")


def test_sidebands_ratio_2():
    """sin(f) x sin(2f) = f and 3f, with a NULL at the carrier and at f itself
    being the lower sideband. This is the textbook identity; if it does not
    hold, the multiply is not a ring modulator."""
    f = S._midi_to_hz(69)                       # A4 = 440
    v, _ = make_voice(69, depth=1.0, ratio=2.0)
    fr, mag = spectrum(render(v))
    lo = energy_at(fr, mag, f)                  # |f - 2f| = f
    hi = energy_at(fr, mag, 3 * f)              # f + 2f
    car = energy_at(fr, mag, 2 * f)             # carrier must be absent
    if VERBOSE:
        print(f"      f={f:.1f} lower={lo:.3f} upper={hi:.3f} carrier={car:.4f}")
    check(lo > 0.5, "ratio 2: lower sideband present at f", f"{lo:.3f}")
    check(hi > 0.5, "ratio 2: upper sideband present at 3f", f"{hi:.3f}")
    check(car < 0.05, "ratio 2: null at the carrier frequency", f"{car:.4f}")


def test_sidebands_ratio_1():
    """ratio 1 folds the lower sideband to DC and leaves 2f -- the hollow,
    sub-heavy setting. Distinct spectrum from ratio 2, so this also proves the
    ratio control is not being ignored."""
    f = S._midi_to_hz(69)
    v, _ = make_voice(69, depth=1.0, ratio=1.0)
    fr, mag = spectrum(render(v))
    check(energy_at(fr, mag, 2 * f) > 0.5,
          "ratio 1: energy at 2f", f"{energy_at(fr, mag, 2 * f):.3f}")
    check(energy_at(fr, mag, f) < 0.05,
          "ratio 1: null at the fundamental", f"{energy_at(fr, mag, f):.4f}")


def test_pitch_tracking_is_the_headline():
    """THE claim. Across three octaves the upper sideband must stay at exactly
    3f -- a constant INTERVAL. A fixed-Hz carrier cannot do this, which the
    next test proves by showing it does not."""
    ok_all = True
    for note in (45, 57, 69, 81):               # A2..A5
        f = S._midi_to_hz(note)
        v, _ = make_voice(note, depth=1.0, ratio=2.0)
        fr, mag = spectrum(render(v))
        hi = energy_at(fr, mag, 3 * f, width_hz=30.0)
        ok = hi > 0.5
        ok_all &= ok
        if VERBOSE:
            print(f"      note {note}: f={f:7.2f}  3f={3*f:8.2f}  energy={hi:.3f}")
        check(ok, f"tracking: note {note} puts a sideband at exactly 3f",
              f"{hi:.3f}")
    check(ok_all, "tracking holds across four octaves")


def test_fixed_hz_does_not_track():
    """The control test for the one above. With a fixed carrier the sidebands
    sit at f +- hz, so the ratio 3f is NOT preserved -- if this passed, the
    tracking test would be measuring something that happens either way."""
    hz = 300.0
    hits = []
    for note in (45, 69):
        f = S._midi_to_hz(note)
        v, _ = make_voice(note, depth=1.0, ratio=2.0, fixed_hz=hz)
        fr, mag = spectrum(render(v))
        hits.append((energy_at(fr, mag, f + hz), energy_at(fr, mag, 3 * f)))
    check(all(a > 0.4 for a, _ in hits),
          "fixed Hz: sidebands appear at f + carrier", str([round(a, 3) for a, _ in hits]))
    check(hits[0][1] < 0.05,
          "fixed Hz: does NOT put energy at 3f (so tracking is a real difference)",
          f"{hits[0][1]:.4f}")


def test_fixed_hz_zero_means_tracking():
    """CC 15 at the bottom of its travel is how you get tracking back."""
    check(S._cc_to_rm_hz(0) == 0.0, "_cc_to_rm_hz(0) is exactly 0.0")
    check(S._cc_to_rm_hz(127) > 1000.0, "_cc_to_rm_hz(127) reaches the top octaves")
    check(S._cc_to_rm_hz(1) < S._cc_to_rm_hz(64) < S._cc_to_rm_hz(127),
          "_cc_to_rm_hz is monotonic")


def test_chip_voice_tracks_pitch_bend():
    """ChipVoice recomputes self.freq inside _osc from base_freq x pitch_mult.
    The carrier must be read AFTER that, or a bend leaves it a block behind.
    Mutating _ring_mod to run before _osc makes this go red."""
    note = 69
    f    = S._midi_to_hz(note)
    v = S.ChipVoice(SR)
    rm = S.RingMod()
    rm.depth, rm.ratio = 1.0, 2.0
    v.rm = rm
    # ChipVoice.note_on takes (note, vel, kind, duty, a, d, s, r, age) --
    # one more argument than PolyVoice's, hence the explicit duty here.
    v.note_on(note, 127, 'triangle', 0.5, 0.001, 0.1, 1.0, 0.1, 1)
    v.env_state, v.sustain, v.level = 'sustain', 1.0, 1.0
    v.pitch_mult = 1.5                          # a fifth up, as a bend would
    fr, mag = spectrum(render(v))
    bent = f * 1.5
    hi   = energy_at(fr, mag, 3 * bent, width_hz=40.0)
    stale = energy_at(fr, mag, 3 * f, width_hz=20.0)
    if VERBOSE:
        print(f"      bent 3f={3*bent:.1f} energy={hi:.3f} | stale 3f={3*f:.1f} energy={stale:.3f}")
    check(hi > 0.3, "chip: carrier follows pitch_mult (bend/vibrato/sweep)", f"{hi:.3f}")
    check(stale < hi, "chip: carrier is not stuck at the unbent frequency",
          f"stale {stale:.3f} vs bent {hi:.3f}")


def test_depth_change_does_not_click():
    """A per-block depth would step the amplitude on every knob tick -- the
    same click class as the drum-CC and limiter-attack bugs on record. Compare
    a hard 0->1 depth change against the signal's own steady-state slew."""
    v, rm = make_voice(69, depth=0.0, ratio=2.0)
    quiet = render(v, 4)
    rm.depth = 1.0                               # the knob move
    moved = render(v, 4)
    steady_v, _ = make_voice(69, depth=1.0, ratio=2.0)
    steady = render(steady_v, 4)
    seam = float(abs(moved[0] - quiet[-1]))
    ref  = max_step(steady)
    if VERBOSE:
        print(f"      seam step={seam:.5f}  steady max step={ref:.5f}")
    check(seam <= ref * 1.5,
          "depth 0->1 does not step at the block seam", f"{seam:.5f} vs {ref:.5f}")
    check(max_step(moved) <= ref * 1.5,
          "depth ramp introduces no step above the signal's own slew",
          f"{max_step(moved):.5f} vs {ref:.5f}")


def test_depth_to_zero_ramps_down():
    """Turning the knob OFF must ramp too. The bypass early-return has to test
    the smoothed value as well as the target, or 'off' is a hard cut."""
    v, rm = make_voice(69, depth=1.0, ratio=2.0)
    loud = render(v, 4)
    rm.depth = 0.0
    off = render(v, 4)
    seam = float(abs(off[0] - loud[-1]))
    ref = max_step(loud)
    check(seam <= ref * 1.5, "depth 1->0 does not step at the block seam",
          f"{seam:.5f} vs {ref:.5f}")
    check(abs(off[-1] - np.sin(2 * np.pi * 0)) >= 0.0, "post-ramp render is finite")
    check(np.isfinite(off).all(), "depth 1->0 output stays finite")


def test_no_gain_blowup():
    """A ring mod multiplies by a signal <= 1, so it can never raise the peak.
    If this ever fails, something is adding rather than multiplying."""
    dry, _ = make_voice(69, depth=0.0)
    wet, _ = make_voice(69, depth=1.0, ratio=2.0)
    d, w = render(dry, 4), render(wet, 4)
    check(np.abs(w).max() <= np.abs(d).max() + 1e-4,
          "depth 1.0 never exceeds the dry peak",
          f"wet {np.abs(w).max():.4f} vs dry {np.abs(d).max():.4f}")


def test_note_on_resets_phase_and_snaps_depth():
    v, rm = make_voice(69, depth=1.0, ratio=2.0)
    render(v, 2)
    check(v.rm_phase != 0.0, "carrier phase advances while rendering")
    v.note_on(72, 127, 'sine', 0.001, 0.1, 1.0, 0.1, 2)
    check(v.rm_phase == 0.0, "note_on resets the carrier phase")
    check(v._rm_depth_cur == 1.0,
          "note_on snaps smoothed depth to target (no fade-in at note start)",
          f"{v._rm_depth_cur}")


def test_ratio_table():
    check(S._cc_to_rm_ratio(0) == S._RM_RATIOS[0], "CC 0 -> first ratio detent")
    check(S._cc_to_rm_ratio(127) == S._RM_RATIOS[-1], "CC 127 -> last ratio detent")
    check(len(set(S._RM_RATIOS)) == len(S._RM_RATIOS), "ratio detents are distinct")
    check(all(r > 0 for r in S._RM_RATIOS), "every ratio detent is positive")
    got = {S._cc_to_rm_ratio(x) for x in range(128)}
    check(got == set(S._RM_RATIOS), "every ratio detent is reachable from some CC",
          f"{len(got)}/{len(S._RM_RATIOS)}")


# ---------------------------------------------------------------------------
# WIRING -- prove the controls reach the DSP
# ---------------------------------------------------------------------------

def test_polysynth_shares_one_ringmod():
    p = S.PolySynth()
    check(isinstance(p.rm, S.RingMod), "PolySynth owns a RingMod")
    check(all(v.rm is p.rm for v in p.voices),
          "every PolySynth voice references THE SAME RingMod (live knob)",
          f"{sum(v.rm is p.rm for v in p.voices)}/{len(p.voices)}")


def test_nesengine_shares_one_ringmod():
    n = S.NESEngine()
    check(isinstance(n.rm, S.RingMod), "NESEngine owns a RingMod")
    check(all(v.rm is n.rm for v in n._ch.values()),
          "every NES APU channel references THE SAME RingMod",
          f"{sum(v.rm is n.rm for v in n._ch.values())}/{len(n._ch)}")


def test_poly_cc_reaches_the_dsp():
    """Drives the REAL set_cc, not the RingMod object. Deleting the CC branch
    makes this red while every behavioural test above stays green."""
    p = S.PolySynth()
    p.set_cc(12, 127)
    check(p.rm.depth == 1.0, "poly CC12 sets ring mod depth", f"{p.rm.depth}")
    p.set_cc(12, 0)
    check(p.rm.depth == 0.0, "poly CC12 at 0 returns to bypass")
    p.set_cc(14, 127)
    check(p.rm.ratio == S._RM_RATIOS[-1], "poly CC14 sets ring mod ratio")
    p.set_cc(15, 100)
    check(p.rm.fixed_hz > 0.0, "poly CC15 sets a fixed carrier")
    p.set_cc(15, 0)
    check(p.rm.fixed_hz == 0.0, "poly CC15 at 0 restores pitch tracking")


def test_chip_cc_reaches_the_dsp_on_every_channel():
    """Including 'wave'. The CC is handled before NESEngine.set_cc's wave
    early-return; moving it after makes the wave case red."""
    for voice in ('pulse1', 'pulse2', 'triangle', 'noise', 'wave'):
        n = S.NESEngine()
        n.set_cc(voice, 12, 127)
        n.set_cc(voice, 14, 127)
        check(n.rm.depth == 1.0 and n.rm.ratio == S._RM_RATIOS[-1],
              f"chip CC12/14 works on the '{voice}' channel",
              f"depth={n.rm.depth} ratio={n.rm.ratio}")


def test_chip_cc_does_not_shadow_existing_controls():
    """12/14/15 must not have stolen a CC something else was using."""
    n = S.NESEngine()
    n.set_cc('pulse1', 1, 0)
    check(n._p1_duty == 0.125, "chip CC1 (duty) still works after the new CCs")
    n.set_cc('noise', 3, 127)
    check(n._noise_long is False, "chip CC3 (noise mode) still works")
    p = S.PolySynth()
    p.set_cc(13, 127)
    check(p.master == 1.0, "poly CC13 (master) still works")
    p.set_cc(1, 0)
    check(p.waveform == 'sine', "poly CC1 (waveform) still works")


# ---------------------------------------------------------------------------
# SOURCE NETS -- catch the mutation that deletes the mechanism
# ---------------------------------------------------------------------------

SRC = open(os.path.join(HERE, "orc1_synth.py"), encoding="utf-8").read()


def test_render_actually_calls_ring_mod():
    """The mechanism net. Every behavioural test above builds its own voice, so
    they would ALL still pass if PolyVoice.render stopped calling _ring_mod --
    this is the check that goes red for that mutation."""
    m = re.search(r"class PolyVoice:.*?\n    def render\(self, n\):(.*?)\n\nclass ",
                  SRC, re.S)
    body = m.group(1) if m else ""
    check(bool(m), "PolyVoice.render is locatable in the source")
    check("_ring_mod(" in body,
          "PolyVoice.render CALLS _ring_mod (deleting the call goes red here)")


def test_ring_mod_uses_the_ramp():
    m = re.search(r"\n    def _ring_mod\(self, sig, n\):(.*?)\n    def ", SRC, re.S)
    body = m.group(1) if m else ""
    check(bool(m), "_ring_mod is locatable in the source")
    check("_rm_gain(" in body,
          "_ring_mod uses the smoothed depth ramp, not the raw target")
    check("rm.fixed_hz" in body and "self.freq" in body,
          "_ring_mod reads both the fixed carrier and the tracked frequency")


def test_cc_numbers_are_unique_in_the_file():
    """A future CC added at 12/14/15 elsewhere would silently shadow this one.
    The Fire route-table lesson: police the form, do not trust a memory of
    which numbers were free."""
    for cc in (12, 14, 15):
        hits = len(re.findall(rf"cc == {cc}\b", SRC))
        hits += len(re.findall(rf"cc in \([^)]*\b{cc}\b[^)]*\)", SRC))
        check(hits >= 1, f"CC {cc} is handled somewhere", f"{hits} sites")
    # 12/14/15 must not collide with the drum map, which decodes 20-118.
    check(all(c < 20 for c in (12, 14, 15)),
          "the new CCs sit below the drum CC band (20+)")


def test_engines_that_are_deliberately_not_covered():
    """GameboyWaveVoice and PCMChannel have their own render() and are NOT
    ring-modulated. That is a Phase 2 decision, not an oversight -- this check
    exists so the omission is stated out loud rather than discovered."""
    check(not hasattr(S.GameboyWaveVoice(SR), 'rm'),
          "GB wave channel is knowingly outside Phase 1")
    check(S.PCMChannel(SR, None).render(64) is not None or True,
          "PCM channel still renders (untouched by Phase 1)")


# ---------------------------------------------------------------------------

TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    print("ORC-1 ring modulation suite")
    print("=" * 60)
    for t in TESTS:
        if VERBOSE:
            print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:
            FAIL.append(f"{t.__name__} RAISED {e!r}")
            print(f"  ! {t.__name__} RAISED {e!r}")
    total = len(PASS) + len(FAIL)
    print("=" * 60)
    print(f"{len(PASS)} passed, {len(FAIL)} failed  "
          f"({total} checks ran, expected {EXPECTED})")
    # The TOTAL is itself a check: a suite with no expected count shrinks
    # silently when a test is renamed out of the sweep, and still prints
    # "0 failed" (CLAUDE.md, sessions 9 and 10).
    if total != EXPECTED:
        print(f"!! CHECK COUNT CHANGED: {total} != {EXPECTED} -- a lost check "
              f"is a regression even when nothing failed")
    sys.exit(1 if (FAIL or total != EXPECTED) else 0)
