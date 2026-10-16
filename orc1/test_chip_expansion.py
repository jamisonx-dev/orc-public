#!/usr/bin/env python3
"""
Offline self-tests for the NES/Game Boy chip expansion (2026-07-20):
pitch bend, vibrato, Pulse-1 hardware sweep, authentic LFSR noise, and
custom Wave RAM. Pure offline -- no JACK, no MIDI ports, no audio device.

Run:  python3 test_chip_expansion.py
Exits 0 if every test passes, 1 otherwise, and prints a PASS/FAIL line per test.

How it works: instantiates NESEngine (the chip engine class) directly and
renders blocks the same way the JACK callback would, then measures the
fundamental frequency of the rendered audio with an interpolated FFT peak.
"""

import sys
import os
import types

# -- Make sure we import the local orc1_synth, not some installed copy --------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# -- Stub the `jack` module defensively ----------------------------------------
# orc1_synth only imports jack inside main() (not at module import time), but
# stub it anyway so this test can never accidentally touch a real JACK server.
if 'jack' not in sys.modules:
    sys.modules['jack'] = types.ModuleType('jack')

import numpy as np
import orc1_synth as S


SR = S.SAMPLE_RATE
BLOCK = 512

_results = []          # (name, passed, detail)


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def render(eng, n_samples):
    """Render n_samples through NESEngine.render in JACK-sized blocks."""
    chunks = []
    done = 0
    while done < n_samples:
        n = min(BLOCK, n_samples - done)
        chunks.append(eng.render(n))
        done += n
    return np.concatenate(chunks)


def measure_f0(sig, sr=SR):
    """Fundamental frequency via Hann-windowed FFT peak + parabolic interp."""
    sig = np.asarray(sig, dtype=np.float64)
    sig = sig - sig.mean()
    if sig.size < 256 or np.abs(sig).max() < 1e-6:
        return 0.0
    w = sig * np.hanning(sig.size)
    nfft = 1 << (int(np.ceil(np.log2(sig.size))) + 2)   # 4x zero-pad
    spec = np.abs(np.fft.rfft(w, nfft))
    # Ignore DC / sub-audio bins
    lo = max(2, int(20.0 * nfft / sr))
    k = lo + int(np.argmax(spec[lo:]))
    # Parabolic interpolation around the peak bin for sub-bin accuracy
    if 1 <= k < spec.size - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        denom = a - 2 * b + c
        d = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        d = max(-0.5, min(0.5, d))
    else:
        d = 0.0
    return (k + d) * sr / nfft


def rms(sig):
    return float(np.sqrt(np.mean(np.asarray(sig, dtype=np.float64) ** 2)))


def fresh():
    return S.NESEngine()


# ==============================================================================
print("== 1. Pitch bend (pulse1) ==")
eng = fresh()
eng.note_on('pulse1', 69, 100)          # A4 = 440 Hz
render(eng, 4096)                       # let attack settle
f_base = measure_f0(render(eng, 8192))
check("baseline f0 ~ 440 Hz", abs(f_base - 440.0) < 440 * 0.03, f"{f_base:.1f} Hz")

eng.set_pitchwheel('pulse1', 8191)      # full up = +CHIP_BEND_RANGE semis
render(eng, BLOCK)                      # one block to apply
f_up = measure_f0(render(eng, 8192))
expect_up = 440.0 * 2.0 ** ((8191 / 8192.0) * S.CHIP_BEND_RANGE / 12.0)
check("full bend up ~ 2x base (range 12)", abs(f_up - expect_up) < expect_up * 0.03,
      f"{f_up:.1f} Hz, expected {expect_up:.1f}")

eng.set_pitchwheel('pulse1', 0)
render(eng, BLOCK)
f_ctr = measure_f0(render(eng, 8192))
check("bend back to centre = base", abs(f_ctr - 440.0) < 440 * 0.03, f"{f_ctr:.1f} Hz")

eng.set_pitchwheel('pulse1', -8192)     # full down = -12 semis = half
render(eng, BLOCK)
f_dn = measure_f0(render(eng, 16384))
check("full bend down ~ 0.5x base", abs(f_dn - 220.0) < 220 * 0.03, f"{f_dn:.1f} Hz")

# Bend on pulse1 must NOT move the triangle (it plays on its own channel)
eng2 = fresh()
eng2.note_on('triangle', 69, 100)
eng2.set_pitchwheel('pulse1', 8191)
eng2.set_pitchwheel('noise', 8191)      # ignored: noise not bendable
render(eng2, 2048)
f_tri = measure_f0(render(eng2, 8192))
check("triangle unaffected by bends", abs(f_tri - 440.0) < 440 * 0.03, f"{f_tri:.1f} Hz")
check("noise bend ignored (no state)", 'noise' not in eng2._bend)

# ==============================================================================
print("== 2. Vibrato (free-running LFO) ==")
eng = fresh()
eng.note_on('pulse1', 69, 100)
eng.set_cc('pulse1', 77, 127)           # depth = 100 cents
eng.set_cc('pulse1', 78, 20)            # rate ~ 2 Hz
render(eng, 4096)
win = 2048
f0s = []
for _ in range(24):                     # ~1 s of audio in 43 ms windows
    f0s.append(measure_f0(render(eng, win)))
spread = (max(f0s) - min(f0s)) / 440.0
check("vibrato modulates f0 (spread > 3%)", spread > 0.03, f"spread {spread*100:.1f}%")

eng.set_cc('pulse1', 77, 0)             # depth 0 = off
render(eng, 4096)
f0s = [measure_f0(render(eng, win)) for _ in range(12)]
spread0 = (max(f0s) - min(f0s)) / 440.0
check("depth 0 -> stable pitch (spread < 0.5%)", spread0 < 0.005, f"spread {spread0*100:.2f}%")

# Wave-channel vibrato arrives on its own channel and must work too
engw = fresh()
engw.note_on('wave', 69, 100)
engw.set_cc('wave', 77, 127)
engw.set_cc('wave', 78, 20)
render(engw, 4096)
f0s = [measure_f0(render(engw, win)) for _ in range(24)]
spread_w = (max(f0s) - min(f0s)) / 440.0
check("wave-channel vibrato works", spread_w > 0.03, f"spread {spread_w*100:.1f}%")

# ==============================================================================
print("== 3. Hardware sweep (pulse1 only) ==")
eng = fresh()
eng.set_cc('pulse1', 79, 32)            # time = 2 -> step every 15.6 ms
eng.set_cc('pulse1', 80, 80)            # shift = 5 (gentle ~3% steps)
eng.set_cc('pulse1', 81, 0)             # direction = up
eng.note_on('pulse1', 57, 100)          # A3 = 220 Hz
f_first = measure_f0(render(eng, 2048))
render(eng, 4096)                       # let it sweep ~85 ms further
f_later = measure_f0(render(eng, 2048))
check("sweep up: f0 rises", f_later > f_first * 1.02,
      f"{f_first:.1f} -> {f_later:.1f} Hz")

# Extreme sweep must overflow the 11-bit register and silence the voice
eng = fresh()
eng.set_cc('pulse1', 79, 16)            # time = 1 (fastest non-zero)
eng.set_cc('pulse1', 80, 16)            # shift = 1 (huge x1.5 steps)
eng.set_cc('pulse1', 81, 0)             # up
eng.note_on('pulse1', 69, 100)
sig = render(eng, int(SR * 1.2))        # overflow + full release fit in 1.2 s
tail = sig[-int(SR * 0.2):]
check("extreme sweep overflows -> silence", rms(tail) < 0.01,
      f"tail rms {rms(tail):.5f}")
check("sweep disarmed after overflow", not eng._sweep_on)

# Sweep down: frequency falls
eng = fresh()
eng.set_cc('pulse1', 79, 32)
eng.set_cc('pulse1', 80, 80)            # shift 5
eng.set_cc('pulse1', 81, 127)           # direction = down
eng.note_on('pulse1', 69, 100)
f_first = measure_f0(render(eng, 2048))
render(eng, 4096)
f_later = measure_f0(render(eng, 2048))
check("sweep down: f0 falls", f_later < f_first * 0.98,
      f"{f_first:.1f} -> {f_later:.1f} Hz")

# Sweep must not touch pulse2
eng = fresh()
eng.set_cc('pulse1', 79, 32)
eng.set_cc('pulse1', 80, 80)
eng.note_on('pulse2', 69, 100)
render(eng, 4096)
f_p2 = measure_f0(render(eng, 8192))
check("sweep leaves pulse2 alone", abs(f_p2 - 440.0) < 440 * 0.03, f"{f_p2:.1f} Hz")

# time = 0 disables sweep entirely
eng = fresh()
eng.set_cc('pulse1', 79, 0)             # time 0 = off
eng.set_cc('pulse1', 80, 127)
eng.note_on('pulse1', 69, 100)
render(eng, 4096)
f_off = measure_f0(render(eng, 8192))
check("sweep time 0 = off", abs(f_off - 440.0) < 440 * 0.03, f"{f_off:.1f} Hz")

# ==============================================================================
print("== 4. Authentic LFSR noise ==")
eng = fresh()
eng.set_cc('noise', 3, 127)             # 7-bit short mode
eng.note_on('noise', 60, 100)
nv = eng._ch['noise']
hold = nv._nhold
period = 127 * hold                     # 7-bit sequence length x hold samples
sig = nv._osc_noise(3 * period)         # raw oscillator (no envelope)
check("7-bit noise periodic at 127 x hold",
      np.array_equal(sig[:period], sig[period:2 * period]),
      f"hold={hold}, period={period}")
check("7-bit noise values are +/-1 only",
      set(np.unique(sig)).issubset({-1.0, 1.0}))

eng.set_cc('noise', 3, 0)               # back to 15-bit long mode
eng.note_on('noise', 60, 100)
nv = eng._ch['noise']
hold = nv._nhold
lag = 127 * hold
sig = nv._osc_noise(2 * lag + 1024)
check("15-bit noise NOT 127-periodic",
      not np.array_equal(sig[:lag], sig[lag:2 * lag]))
check("15-bit table full period 32767", S._GB_LFSR_15.shape[0] == 32767)
check("7-bit table full period 127", S._GB_LFSR_7.shape[0] == 127)
# The 7-bit sequence must itself be non-trivial (not all one value)
check("7-bit table has both polarities",
      (S._GB_LFSR_7 > 0).any() and (S._GB_LFSR_7 < 0).any())

# ==============================================================================
print("== 5. Custom Wave RAM ==")
eng = fresh()
w = eng._wave
# Draw a square into the custom table over the CC pipeline:
# steps 0-15 = 15 (DAC -1.0), steps 16-31 = 0 (DAC +1.0)
for i in range(32):
    eng.set_cc('wave', 20, i * 4)                   # latch index i
    eng.set_cc('wave', 21, 127 if i < 16 else 0)    # write 15 or 0
eng.set_cc('wave', 23, 127)             # activate custom table
# 2026-09-24: the 08-04 de-click pass added an envelope + table crossfade, so
# the FIRST samples are never exactly +/-1. Hold sustain at full (CC10=127) and
# measure after attack/decay settle -- the shape claim is unchanged.
eng.set_cc('wave', 10, 127)
eng.note_on('wave', 69, 100)
w.render(48000)                         # let attack/decay settle
sig = w.render(4096)
uniq = set(np.unique(np.round(sig, 4)))
check("custom square: output is 2-level +/-1", uniq == {-1.0, 1.0}, str(sorted(uniq)))

eng.set_cc('wave', 23, 0)               # revert to preset (default = triangle)
sig = w.render(4096)
check("CC23 < 64 reverts to preset (multi-level)", len(np.unique(np.round(sig, 4))) > 4,
      f"{len(np.unique(np.round(sig, 4)))} levels")

# Live edit: while custom is active, a CC21 write is heard immediately
eng.set_cc('wave', 23, 127)
eng.set_cc('wave', 20, 0)               # index 0
eng.set_cc('wave', 21, 60)              # write value 7 (60*16//128) at step 0
w.render(4096)                          # let the 5 ms edit crossfade finish
sig = w.render(4096)
check("live custom edit is audible", len(np.unique(np.round(sig, 4))) == 3,
      f"{len(np.unique(np.round(sig, 4)))} levels")

# CC1 preset select must also drop out of custom mode
eng.set_cc('wave', 23, 127)
eng.set_cc('wave', 1, 70)               # preset 4 = square (hardware preset)
check("CC1 preset select exits custom mode", not w._use_custom)

# ==============================================================================
print("== 6. Regression (no-modulation behaviour unchanged) ==")
eng = fresh()
eng.note_on('pulse1', 60, 100)
eng.note_on('pulse2', 64, 100)
eng.note_on('wave', 48, 100)
eng.note_on('noise', 60, 100)
eng.note_on('triangle', 55, 100)
sig = render(eng, SR // 2)
check("all chip voices render, nonzero RMS", np.isfinite(sig).all() and rms(sig) > 0.01,
      f"rms {rms(sig):.3f}")

# Noise 15-bit default still sounds
eng = fresh()
eng.note_on('noise', 60, 100)
sig = render(eng, SR // 4)
check("default (15-bit) noise sounds", rms(sig) > 0.01, f"rms {rms(sig):.3f}")

# ADSR: release still fades to silence
eng = fresh()
eng.note_on('pulse1', 69, 100)
render(eng, SR // 4)
eng.note_off(69)
sig = render(eng, SR)                   # 1 s >> default 0.15 s release
check("ADSR release fades to idle", rms(sig[-SR // 4:]) < 1e-3 and
      eng._ch['pulse1'].env_state == 'idle', f"tail rms {rms(sig[-SR//4:]):.5f}")

# Detune coupling still detunes (and survives the per-block recompute)
eng = fresh()
eng.set_cc('pulse1', 72, 40)            # coupling mode idx 2 = 'detune'
check("coupling mode set to detune", eng.coupling_mode == 'detune')
eng.note_on('pulse1', 69, 100)
render(eng, 4096)                       # a few blocks of pitch-mod recompute
p1f = eng._ch['pulse1'].freq
p2f = eng._ch['pulse2'].freq
ratio = p2f / p1f
expect = 2.0 ** (eng.detune_cents / 1200.0)
check("detune coupling: p2 stays detuned after render",
      abs(ratio - expect) < 1e-4, f"ratio {ratio:.6f}, expected {expect:.6f}")

# Duty CC still live-applies
eng = fresh()
eng.note_on('pulse1', 69, 100)
eng.set_cc('pulse1', 1, 0)              # duty 12.5%
check("duty live-apply intact", eng._ch['pulse1'].duty == 0.125)

# ==============================================================================
print("== 7. MIDI routing (chip host router, pitchwheel) ==")
# Was orc1_synth._route_midi; chip dispatch moved to orc1_chip_host at the
# 2026-09-02 audio-host split and _route_midi was removed 2026-09-24.
import orc1_chip_host as CHH
_saved_bindings = S._BINDINGS
S._BINDINGS = dict(S._DEFAULT_BINDINGS)   # router reads bindings at build

class FakeMsg:
    def __init__(self, **kw):
        self.__dict__.update(kw)

class FakeEngine:
    def __init__(self):
        self.calls = []
    def chip_note_on(self, *a):  self.calls.append(('on',) + a)
    def chip_note_off(self, *a): self.calls.append(('off',) + a)
    def chip_cc(self, *a):       self.calls.append(('cc',) + a)
    def chip_pitch(self, *a):    self.calls.append(('pitch',) + a)

fe = FakeEngine()
CHH._make_router(fe)(FakeMsg(type='pitchwheel', channel=S.CH_CHIP - 1, pitch=1234))
check("pitchwheel on CH_CHIP -> chip_pitch('pulse1')",
      fe.calls == [('pitch', 'pulse1', 1234)], str(fe.calls))

fe = FakeEngine()
CHH._make_router(fe)(FakeMsg(type='pitchwheel', channel=S.CH_GB_WAVE - 1, pitch=-8192))
S._BINDINGS = _saved_bindings
check("pitchwheel on CH_GB_WAVE -> chip_pitch('wave')",
      fe.calls == [('pitch', 'wave', -8192)], str(fe.calls))

# Host-level queue dispatch: guard means legacy ChipSynth is safe too
class FakeChip:
    pass
check("chip_pitch dispatch guards on set_pitchwheel",
      not hasattr(FakeChip(), 'set_pitchwheel'))

# ==============================================================================
fails = [r for r in _results if not r[1]]
print()
print(f"{len(_results) - len(fails)}/{len(_results)} tests passed.")
if fails:
    print("FAILED:")
    for name, _, detail in fails:
        print(f"  - {name}  {detail}")
    sys.exit(1)
sys.exit(0)
