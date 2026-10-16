#!/usr/bin/env python3
"""orc1_lfilter -- scipy's C lfilter without importing scipy.signal (2026-09-23).

Proves (on the Pi, where scipy is installed):
  1. the lite path is ACTIVE (SOURCE == "direct"), not the fallback;
  2. its output AND carried filter state are BIT-IDENTICAL to the real
     scipy.signal.lfilter, using the ORC's own BiquadFilter coefficients
     (LP/HP/BP x a cutoff/Q sweep), float32 + float64 audio, state carried
     across 20 consecutive blocks exactly as the engines run it;
  3. importing orc1_synth + orc1_sampler loads ZERO scipy modules -- the memory
     win (~48 MB private per host). This net goes red if anyone re-adds a
     module-level `from scipy.signal import ...`.
Mutation: make orc1_lfilter.lfilter use a numpy-free reimplementation or drop
the zi pass-through -> check 2 fails; re-add the scipy import in orc1_synth ->
check 3 fails.
"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {label}" + (f"  -- {detail}" if detail else ""))


import orc1_lfilter as L                       # noqa: E402

check(L.SOURCE == "direct", "lite path active (not the scipy fallback)",
      f"SOURCE={L.SOURCE}")

# --- 2. bit-identical to the real scipy lfilter ---------------------------------
from scipy.signal import lfilter as ref       # noqa: E402  (the reference)
import orc1_synth as S                         # noqa: E402

rng = np.random.default_rng(23)
cases = mism = 0
worst = ""
for mode in ("LP", "HP", "BP"):
    for cutoff in (40.0, 180.0, 900.0, 4000.0, 12000.0):
        for q in (0.5, 0.707, 2.0, 8.0):
            f = S.BiquadFilter(48000, cutoff=cutoff, q=q, mode=mode)
            b, a = f._b, f._a
            for dt in (np.float32, np.float64):
                zi_l = np.zeros(2, dtype=np.float64)
                zi_r = np.zeros(2, dtype=np.float64)
                for _blk in range(20):
                    x = (rng.standard_normal(1024) * 0.3).astype(dt)
                    y_l, zi_l = L.lfilter(b, a, x, zi=zi_l)
                    y_r, zi_r = ref(b, a, x, zi=zi_r)
                    cases += 1
                    if not (y_l.dtype == y_r.dtype and np.array_equal(y_l, y_r)
                            and np.array_equal(zi_l, zi_r)):
                        mism += 1
                        worst = f"{mode} {cutoff}Hz q{q} {dt.__name__}"
check(cases == 3 * 5 * 4 * 2 * 20 and mism == 0,
      "bit-identical to scipy.signal.lfilter (output + carried state, 2400 blocks)",
      f"cases={cases} mismatches={mism} e.g. {worst}")

# the real engine path end to end: BiquadFilter.process through the lite lfilter
f1 = S.BiquadFilter(48000, cutoff=700.0, q=1.2, mode="BP")
x = (rng.standard_normal(4096) * 0.3).astype(np.float32)
y1 = np.concatenate([f1.process(x[i:i + 1024]) for i in range(0, 4096, 1024)])
y2, _ = ref(f1._b, f1._a, x, zi=np.zeros(2))
check(np.allclose(y1, y2.astype(y1.dtype), atol=0, rtol=0) or np.array_equal(y1, y2),
      "BiquadFilter.process through the lite path == one-shot scipy over the same audio",
      f"max diff {float(np.max(np.abs(y1 - y2))):.3g}")

# --- 3. the memory win: no scipy modules loaded by the engine imports ----------
code = ("import sys; sys.path.insert(0, %r); import orc1_synth, orc1_sampler; "
        "print(sum(1 for k in sys.modules if k.startswith('scipy')))" % HERE)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
n = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "?"
check(n == "0", "importing orc1_synth + orc1_sampler loads ZERO scipy modules",
      f"scipy modules loaded: {n} {r.stderr.strip()[-200:]}")

EXPECTED = 4
print(f"{PASS} passed, {FAIL} failed  (expected {EXPECTED})")
sys.exit(1 if (FAIL or PASS + FAIL != EXPECTED) else 0)
