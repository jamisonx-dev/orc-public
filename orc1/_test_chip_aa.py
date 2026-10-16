#!/usr/bin/env python3
"""
ORC-1 chip pulse anti-alias (PolyBLEP) -- offline DSP + wiring suite.
====================================================================

The claim: CC 85 opt-in band-limits the pulse oscillator (default OFF, so the
raw aliased square -- the 8-bit charm -- is untouched), and when ON it removes
the aliasing energy that a naive square folds back down (the cyclic "fizz" of a
detuned P1+P2 pair). That is a claim about the SPECTRUM, so this renders real
blocks through the real ChipVoice and takes an FFT -- reasoning about the maths
proves nothing (CLAUDE.md has several "green suite, did nothing" fixes).

Two classes of check:
  * BEHAVIOUR -- render + measure; proves the DSP does what it says.
  * WIRING    -- drive the real set_cc path; proves the CC reaches the flag and
                 that the default is genuinely unchanged.

    python3 _test_chip_aa.py         # run
    python3 _test_chip_aa.py -v      # show measured spectra

SAFETY: constructs ChipVoice / NESEngine directly. Opens no JACK client, sends
no MIDI, reads/writes no file. Safe to run while the instrument is played.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np                                            # noqa: E402
import orc1_synth as S                                        # noqa: E402

EXPECTED = 15                    # a shrinking suite must go red, not say "0 failed"

SR      = S.SAMPLE_RATE
VERBOSE = "-v" in sys.argv
PASS, FAIL = [], []


def check(ok, label, detail=""):
    (PASS if ok else FAIL).append(label)
    if not ok:
        print(f"  ! {label}" + (f"  --  {detail}" if detail else ""))
    elif VERBOSE:
        print(f"  . {label}" + (f"  --  {detail}" if detail else ""))
    return ok


def _osc_buffer(freq, duty, aa, n):
    """Render one continuous pulse buffer straight from the oscillator."""
    v = S.ChipVoice(SR)
    v.kind, v.duty, v.aa = 'pulse', duty, aa
    v.base_freq, v.pitch_mult, v.phase = float(freq), 1.0, 0.0
    return np.asarray(v._osc(n), dtype=np.float64)


def _alias_and_fundamental(buf, f0):
    """Split spectral energy into (alias, fundamental) buckets.

    A naive square at f0 has harmonics only at odd multiples of f0. Anything
    above Nyquist folds back onto NON-harmonic bins -- that folded content is
    the alias energy. `tol` is a half-window around each true harmonic.
    """
    w = np.hanning(len(buf))
    X = np.fft.rfft(buf * w)
    freqs = np.fft.rfftfreq(len(buf), 1.0 / SR)
    power = (np.abs(X) ** 2)
    tol = f0 / 4.0
    harmonics = np.arange(f0, SR / 2.0, 2 * f0)     # f0, 3f0, 5f0, ...
    near_harm = np.zeros_like(freqs, dtype=bool)
    for h in harmonics:
        near_harm |= np.abs(freqs - h) < tol
    audible = freqs > 200.0                          # ignore DC / sub-rumble
    alias = float(power[audible & ~near_harm].sum())
    fund  = float(power[np.abs(freqs - f0) < tol].sum())
    return alias, fund


# ---------------------------------------------------------------------------
# BEHAVIOUR
# ---------------------------------------------------------------------------

def test_default_is_raw():
    """A fresh ChipVoice has aa OFF, and with aa off the pulse osc emits ONLY
    the exact +-1 naive square -- i.e. the shipping sound is byte-for-byte
    unchanged until someone opts in."""
    v = S.ChipVoice(SR)
    check(v.aa is False, "ChipVoice defaults to aa=False", repr(v.aa))
    off = _osc_buffer(1500.0, 0.5, aa=False, n=8192)
    only_pm1 = bool(np.all((off == 1.0) | (off == -1.0)))
    check(only_pm1, "aa OFF: pulse is pure +-1 (raw square, unchanged)",
          f"distinct={np.unique(off).size}")


def test_aa_changes_only_edges():
    """aa ON deviates from +-1 (the band-limited samples straddling each edge),
    but leaves most samples untouched -- it corrects edges, not the whole wave.
    If PolyBLEP were a no-op this stays all +-1 and the test fails."""
    on = _osc_buffer(1500.0, 0.5, aa=True, n=8192)
    deviates = np.count_nonzero((on != 1.0) & (on != -1.0))
    check(deviates > 0, "aa ON: some samples are band-limited (not +-1)",
          f"deviating={deviates}")
    check(deviates < on.size // 3,
          "aa ON: only a minority of samples change (edges, not the body)",
          f"deviating={deviates}/{on.size}")
    check(float(np.max(np.abs(on))) < 1.6,
          "aa ON: stays bounded (no runaway overshoot)",
          f"peak={np.max(np.abs(on)):.3f}")


def test_aa_cuts_alias_energy():
    """The headline: at a high fundamental (many harmonics fold), aa ON has far
    less non-harmonic (alias) energy than aa OFF, while KEEPING the fundamental.
    The control -- fundamental preserved -- is what stops 'it just muted everything'
    from passing. f0 chosen so folded harmonics land BETWEEN real harmonics; a
    'nice' f0 like 3000 folds back onto harmonic bins and shows no measurable
    alias -- a property of the geometry, not the filter."""
    f0 = 2500.0
    off = _osc_buffer(f0, 0.5, aa=False, n=SR)     # 1 s -> 1 Hz bins
    on  = _osc_buffer(f0, 0.5, aa=True,  n=SR)
    a_off, f_off = _alias_and_fundamental(off, f0)
    a_on,  f_on  = _alias_and_fundamental(on,  f0)
    if VERBOSE:
        print(f"    alias off={a_off:.1f} on={a_on:.1f}  ratio={a_on/max(a_off,1e-9):.3f}")
        print(f"    fund  off={f_off:.1f} on={f_on:.1f}")
    check(a_on < 0.5 * a_off,
          "aa ON at least halves the alias (non-harmonic) energy",
          f"on/off={a_on/max(a_off,1e-9):.3f}")
    check(f_on > 0.5 * f_off,
          "aa ON preserves the fundamental (did not just mute the note)",
          f"on/off={f_on/max(f_off,1e-9):.3f}")


def test_detuned_pair_is_less_glitchy():
    """The real complaint: a DETUNED P1+P2 pair fizzes. Sum two pulses a few
    cents apart and confirm aa ON drops the alias energy of the SUM too."""
    f0 = 2600.0
    det = f0 * (2.0 ** (7.0 / 1200.0))             # +7 cents, the default detune
    off = _osc_buffer(f0, 0.5, False, SR) + _osc_buffer(det, 0.5, False, SR)
    on  = _osc_buffer(f0, 0.5, True,  SR) + _osc_buffer(det, 0.5, True,  SR)
    a_off, _ = _alias_and_fundamental(off, f0)
    a_on,  _ = _alias_and_fundamental(on,  f0)
    check(a_on < 0.6 * a_off,
          "aa ON cuts the alias energy of a detuned pulse pair",
          f"on/off={a_on/max(a_off,1e-9):.3f}")


def test_low_note_barely_changes():
    """At a low fundamental almost nothing aliases, so aa ON is nearly identical
    to OFF -- the fix targets the ranges that actually fizz, not everything."""
    f0 = 110.0
    off = _osc_buffer(f0, 0.5, False, SR)
    on  = _osc_buffer(f0, 0.5, True,  SR)
    a_off, _ = _alias_and_fundamental(off, f0)
    a_on,  _ = _alias_and_fundamental(on,  f0)
    check(a_on <= a_off * 1.05,
          "aa ON never ADDS alias energy at a low note",
          f"on/off={a_on/max(a_off,1e-9):.3f}")


# ---------------------------------------------------------------------------
# WIRING
# ---------------------------------------------------------------------------

def test_cc85_sets_both_pulses():
    """CC 85 on a pulse channel flips aa on BOTH pulse voices (so one toggle
    covers a coupled/detuned pair), and 0 turns it back off."""
    eng = S.NESEngine()
    check(eng._ch['pulse1'].aa is False and eng._ch['pulse2'].aa is False,
          "NESEngine boots with pulse aa off")
    eng.set_cc('pulse1', 85, 127)
    check(eng._ch['pulse1'].aa is True and eng._ch['pulse2'].aa is True,
          "CC85=127 on pulse1 turns aa on for BOTH pulses")
    eng.set_cc('pulse1', 85, 0)
    check(eng._ch['pulse1'].aa is False and eng._ch['pulse2'].aa is False,
          "CC85=0 turns aa back off")
    # engine-wide: arriving on pulse2 also covers pulse1
    eng.set_cc('pulse2', 85, 100)
    check(eng._ch['pulse1'].aa is True,
          "CC85 on pulse2 also enables pulse1 (engine-wide)")


def test_cc85_is_persisted_not_skipped():
    """The flag is a patch parameter -- it must NOT be in the skip set, so the
    host records+restores it like the other chip params (auto-remember-last)."""
    check(85 not in S._CHIP_PERSIST_SKIP,
          "CC 85 is persisted (not in _CHIP_PERSIST_SKIP)",
          str(sorted(S._CHIP_PERSIST_SKIP)))


def test_aa_ignores_non_pulse():
    """Triangle/noise don't read .aa, so enabling it must not disturb them.
    (Guards against a future refactor that routes .aa into the wrong branch.)"""
    v = S.ChipVoice(SR)
    v.kind, v.base_freq, v.pitch_mult, v.phase = 'triangle', 440.0, 1.0, 0.0
    v.aa = False
    base = np.asarray(v._osc(4096), dtype=np.float64)
    v.aa, v.phase = True, 0.0
    withaa = np.asarray(v._osc(4096), dtype=np.float64)
    check(np.array_equal(base, withaa),
          "aa has no effect on the triangle wave")


def main():
    print("ORC-1 chip pulse anti-alias (PolyBLEP) suite")
    print(f"  orc1_synth : {os.path.join(HERE, 'orc1_synth.py')}")
    for t in (test_default_is_raw, test_aa_changes_only_edges,
              test_aa_cuts_alias_energy, test_detuned_pair_is_less_glitchy,
              test_low_note_barely_changes, test_cc85_sets_both_pulses,
              test_cc85_is_persisted_not_skipped, test_aa_ignores_non_pulse):
        try:
            t()
        except Exception as e:
            check(False, f"{t.__name__} itself crashed", repr(e))
    total = len(PASS) + len(FAIL)
    print(f"\n{'='*58}")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed, {total} checks")
    if total != EXPECTED:
        print(f"  ! EXPECTED {EXPECTED} checks, ran {total} -- suite changed size")
        print(f"{'='*58}")
        return 1
    print(f"{'='*58}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
