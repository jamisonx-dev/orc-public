#!/usr/bin/env python3
"""PeakLimiter regression -- the per-host brickwall limiter reinstated after the
audio host split (2026-09-03). Renders real blocks and asserts: transparent below
CEIL, instant-attack cap at/under CEIL, stereo-LINKED gain (image preserved), and
~80ms release recovery. Pure stdlib + numpy; imports orc1_synth (side-effect free).

Run on the Pi:  python3 _test_limiter.py
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_synth as S

EXPECTED_TOTAL = 12
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ! {label}")

N   = 1024
SR  = S.SAMPLE_RATE
CEIL = 0.98

def block(v, n=N):
    return np.full(n, float(v), dtype=np.float32)

# -- 1. transparent below CEIL (bit-identical) --------------------------------
lim = S.PeakLimiter()
b = block(0.5)
out = lim.process([b])[0]
check(np.array_equal(out, b), "below CEIL: output is bit-identical (transparent)")
check(abs(lim._g - 1.0) < 1e-9, "below CEIL: gain state stays at unity")

# a signal exactly at CEIL is still transparent (peak <= CEIL)
lim2 = S.PeakLimiter()
bc = block(CEIL)
check(np.array_equal(lim2.process([bc])[0], bc), "at CEIL exactly: still transparent")

# -- 2. instant-attack cap ----------------------------------------------------
lim3 = S.PeakLimiter()
loud = block(1.5)
o = lim3.process([loud])[0]
check(float(np.max(np.abs(o))) <= CEIL + 1e-6, "loud block: peak capped at/under CEIL (no overshoot)")
check(float(np.max(np.abs(o))) > CEIL - 0.05, "loud block: capped near CEIL, not silenced")
# fast attack applies the whole block at target -> flat, no ramp within the block
check(np.allclose(o, o[0], atol=1e-6), "attack: whole block held at the target gain (instant)")

# -- 3. stereo-LINKED: gain comes from the louder channel, image preserved ----
lim4 = S.PeakLimiter()
L = block(1.5); R = block(0.3)
Lo, Ro = lim4.process([L, R])
g = CEIL / 1.5                       # gain the louder side forces
check(np.allclose(Lo, 1.5 * g, atol=1e-4), "stereo: louder channel scaled to CEIL")
check(np.allclose(Ro, 0.3 * g, atol=1e-4), "stereo: quieter channel scaled by the SAME (linked) gain")
# the L/R ratio (stereo image) is unchanged by limiting
check(abs((Lo[0]/Ro[0]) - (1.5/0.3)) < 1e-3, "stereo: image ratio preserved (not per-channel limited)")

# -- 4. release recovers toward unity after a loud block ----------------------
lim5 = S.PeakLimiter()
lim5.process([block(2.0)])           # slam gain down
g_after_attack = lim5._g
# feed quiet blocks; gain must climb back up (release)
for _ in range(50):                  # ~50 * 1024/48000 ≈ 1.07s
    lim5.process([block(0.2)])
check(lim5._g > g_after_attack, "release: gain recovers upward after the loud block")
check(lim5._g > 0.99, "release: gain returns to ~unity given enough quiet blocks")

# -- 5. empty input is safe ---------------------------------------------------
check(S.PeakLimiter().process([]) == [], "empty input -> no crash, returns []")

print(f"\n_test_limiter: {PASS} passed, {FAIL} failed  (expected {EXPECTED_TOTAL})")
if PASS != EXPECTED_TOTAL:
    print(f"  !! total {PASS} != expected {EXPECTED_TOTAL} -- a group may not have run")
sys.exit(1 if (FAIL or PASS != EXPECTED_TOTAL) else 0)
