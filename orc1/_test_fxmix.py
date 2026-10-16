#!/usr/bin/env python3
"""FxMixer regression (2026-09-03): the audio-rate wet/dry crossfade + return
level. Proves mix=0 passes dry, mix=1 passes wet, level scales, and -- the whole
point -- a mix/level JUMP crossfades SMOOTHLY (no output discontinuity) where a
hard switch would click. Pure stdlib + numpy; imports orc1_fxmix (jack/mido
deferred to run_jack/main, so import is safe offline).

Run on the Pi:  python3 _test_fxmix.py
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_synth as S
import orc1_fxmix as FX

EXPECTED_TOTAL = 13
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ! {label}")

SR = S.SAMPLE_RATE
N  = 1024

def tone(f, n, phase0=0.0, a=0.3):
    t = (phase0 + np.arange(n)) / SR
    return (a * np.sin(2 * np.pi * f * t)).astype(np.float32)

def settle(mx, dry, wet, blocks=40):
    """Run enough blocks for the slew to reach its target; return last out."""
    o = (None, None)
    for _ in range(blocks):
        o = mx.process(dry, dry.copy(), wet, wet.copy())
    return o

DRY = tone(220, N)
WET = tone(330, N, phase0=123)

# -- 1. mix endpoints ---------------------------------------------------------
mx = FX.FxMixer()
mx.set_mix(0.0); mx.set_level(1.0)
oL, _ = settle(mx, DRY, WET)
check(np.allclose(oL, DRY, atol=1e-4), "mix=0 -> output is the DRY signal")
mx.set_mix(1.0)
oL, _ = settle(mx, DRY, WET)
check(np.allclose(oL, WET, atol=1e-4), "mix=1 -> output is the WET signal")

# -- 2. level scales ----------------------------------------------------------
mx = FX.FxMixer(); mx.set_mix(1.0); mx.set_level(0.5)
oL, _ = settle(mx, DRY, WET)
check(np.allclose(oL, WET * 0.5, atol=1e-4), "level=0.5 halves the output")
mx.set_level(2.0)
oL, _ = settle(mx, DRY, WET)
check(np.allclose(oL, WET * 2.0, atol=1e-3), "level=2.0 doubles the output")
mx.set_level(5.0)  # clamp
check(mx.level_t == 2.0, "level target clamps at 2.0")
mx.set_mix(-1.0); check(mx.mix_t == 0.0, "mix target clamps at 0.0")

# -- 3. THE POINT: a mix JUMP crossfades smoothly (no click) ------------------
# PHASE-CONTINUOUS dry+wet over the whole window (sliced per block, so the only
# discontinuity possible is the mixer's). Jump mix 0->1 mid-window; a smooth slew
# keeps the output diff a small fraction of a hard switch's boundary click.
NB   = 8
Dseq = tone(220, N * NB)
Wseq = tone(330, N * NB, phase0=50)
def blk(seq, i): return seq[i*N:(i+1)*N].copy()
mx = FX.FxMixer(ramp_ms=25.0); mx.set_mix(0.0)
outs = []
for i in range(NB):
    if i == 3:
        mx.set_mix(1.0)                            # JUMP the target mid-window
    outs.append(mx.process(blk(Dseq, i), blk(Dseq, i), blk(Wseq, i), blk(Wseq, i))[0])
cross = np.concatenate(outs)
cross_slope = float(np.max(np.abs(np.diff(cross))))
# Hard switch on the SAME continuous signals: dry for 3 blocks then wet -> a big
# click at the block-3 boundary. The slewed crossfade must be a fraction of it.
hard = np.concatenate([blk(Dseq, i) for i in range(3)] + [blk(Wseq, i) for i in range(3, NB)])
hard_slope = float(np.max(np.abs(np.diff(hard))))
check(cross_slope < 0.35 * hard_slope,
      f"mix jump crossfades SMOOTHLY (cross={cross_slope:.4f} << hard-switch={hard_slope:.4f})")
# and it actually MOVED from dry toward wet during those blocks
check(mx.mix > 0.5, "mix slewed toward the target across the crossfade")
# the crossfade should take about ramp_ms, not one block (proves the slew exists)
mx2 = FX.FxMixer(ramp_ms=25.0); mx2.set_mix(0.0); settle(mx2, DRY, WET, 5)
mx2.set_mix(1.0); mx2.process(DRY, DRY.copy(), WET, WET.copy())   # one block
check(mx2.mix < 1.0, "one block does NOT reach the target (slew spans ~ramp_ms)")

# -- 4. level jump also smooth -----------------------------------------------
mx = FX.FxMixer(ramp_ms=25.0); mx.set_mix(1.0); mx.level = mx.level_t = 1.0
outs = []
for i in range(NB):
    if i == 3:
        mx.set_level(0.0)   # slam the return to silence mid-window
    outs.append(mx.process(blk(Dseq, i), blk(Dseq, i), blk(Wseq, i), blk(Wseq, i))[0])
cross = np.concatenate(outs)
check(float(np.max(np.abs(np.diff(cross)))) < 0.35 * hard_slope,
      "level jump to 0 fades SMOOTHLY (no click)")

# -- 5. router maps this instance's CCs ---------------------------------------
# _make_router now takes per-instance mix/level CCs (arbitrary instances each get
# their own addressing), so pass explicit CC numbers and confirm they route.
import mido
MIX_CC, LVL_CC = 3, 4
mx = FX.FxMixer(); route = FX._make_router(mx, MIX_CC, LVL_CC)
route(mido.Message("control_change", channel=S._VOL_CH - 1, control=MIX_CC, value=64))
check(abs(mx.mix_t - 64/127) < 1e-6, "router: this instance's mix CC sets the mix target")
route(mido.Message("control_change", channel=S._VOL_CH - 1, control=LVL_CC, value=64))
check(abs(mx.level_t - 1.0) < 1e-6, "router: this instance's level CC=64 -> unity level")
# a DIFFERENT instance's CC must NOT affect this one (independent addressing)
mx2 = FX.FxMixer(); route2 = FX._make_router(mx2, 5, 6)
route2(mido.Message("control_change", channel=S._VOL_CH - 1, control=MIX_CC, value=100))
check(mx2.mix_t == 0.0, "router: another instance ignores CCs that aren't its own")

print(f"\n_test_fxmix: {PASS} passed, {FAIL} failed  (expected {EXPECTED_TOTAL})")
if PASS != EXPECTED_TOTAL:
    print(f"  !! total {PASS} != expected {EXPECTED_TOTAL} -- a group may not have run")
sys.exit(1 if (FAIL or PASS != EXPECTED_TOTAL) else 0)
