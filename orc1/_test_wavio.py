#!/usr/bin/env python3
"""orc1_wavio -- WAV read + FFT resample without scipy (2026-09-23). Run on the Pi.

Self-contained: builds its own test WAVs (8/16-bit, mono/stereo, 16k/22.05k/
32k/44.1k/48k) in a temp dir, because the real samples/ folder is now all
16-bit/48k (owner: drum samples must not be resampled at load).
  1. read_wav == scipy.io.wavfile.read EXACTLY on the synthetic set AND on
     every real WAV in samples/ (rate, dtype, shape, values);
  2. the full SampleVoice.load pipeline matches the old scipy pipeline --
     identical for 48k, within float rounding (< 1e-5 of full scale, below
     -100 dB) for the resampled ones;
  3. a non-48k file is LOUDLY flagged at load (the WARNING line), and loading it
     imports ZERO scipy modules.
Mutation: drop the unpaired-bin correction in resample -> check 2 fails; return
the wrong dtype from read_wav -> check 1 fails; drop the warning or re-add
`import scipy.signal` in SampleVoice.load -> check 3 fails.
"""
import glob
import os
import subprocess
import sys
import tempfile
import wave

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


import orc1_wavio as W                          # noqa: E402
import scipy.io.wavfile as SW                   # noqa: E402  (reference)
from scipy.signal import resample as SRS        # noqa: E402  (reference)

# --- synthetic test WAVs ------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="wavio_")
rng = np.random.default_rng(9)


def mk(name, rate, bits, ch, secs=0.2):
    n = int(rate * secs)
    t = np.arange(n) / rate
    sig = np.exp(-t * 18) * np.sin(2 * np.pi * 180 * t) + 0.05 * rng.standard_normal(n)
    sig = np.clip(sig * 0.8, -1, 1)
    x = np.stack([sig, np.roll(sig, 7)], axis=1) if ch == 2 else sig
    raw = (np.round(x * 127 + 128).astype(np.uint8) if bits == 8
           else np.round(x * 32767).astype("<i2"))
    p = os.path.join(TMP, name)
    with wave.open(p, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(bits // 8); w.setframerate(rate)
        w.writeframes(raw.tobytes())
    return p


synth = [mk("m16_16k.wav", 16000, 16, 1), mk("m16_22k.wav", 22050, 16, 1),
         mk("m8_32k.wav", 32000, 8, 1), mk("s16_44k.wav", 44100, 16, 2),
         mk("m16_48k.wav", 48000, 16, 1), mk("s16_48k.wav", 48000, 16, 2)]
real = sorted(glob.glob(os.path.join(HERE, "samples", "*.wav")))

# 1. reader exactness
bad = []
for f in synth + real:
    r1, d1 = W.read_wav(f)
    r2, d2 = SW.read(f)
    if not (r1 == r2 and d1.dtype == d2.dtype and d1.shape == d2.shape
            and np.array_equal(d1, d2)):
        bad.append(os.path.basename(f))
check(not bad and len(real) >= 1,
      f"read_wav == scipy.io.wavfile.read ({len(synth)} synthetic + {len(real)} real files)",
      f"differ: {bad[:5]}")


# 2. the whole load pipeline, old (scipy) vs new (SampleVoice as shipped)
def old_pipeline(path, target):
    sr, data = SW.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)
    if np.abs(data).max() > 1.0:
        data /= 32768.0
    if sr != target:
        data = SRS(data, int(len(data) * target / sr)).astype(np.float32)
    peak = np.abs(data).max()
    return data / peak * 0.9 if peak > 0 else data


import orc1_synth as S                          # noqa: E402

worst, worst_f, lens_bad = 0.0, "", []
for f in synth:
    v = S.SampleVoice()
    v.load(f)
    new, ref = v._data, old_pipeline(f, S.SAMPLE_RATE)
    if len(new) != len(ref):
        lens_bad.append(os.path.basename(f))
        continue
    d = float(np.max(np.abs(new.astype(np.float64) - ref.astype(np.float64))))
    if d > worst:
        worst, worst_f = d, os.path.basename(f)
check(not lens_bad and worst < 1e-5,
      "SampleVoice.load matches the old scipy pipeline (4 resampled + 2 native files)",
      f"worst diff {worst:.2e} ({worst_f}); length mismatch {lens_bad[:3]}")
print(f"  (worst difference vs the old scipy load: {worst:.2e} of full scale, {worst_f})")

# 3. a non-48k file: warned loudly, and no scipy imported
code = ("import sys; sys.path.insert(0, %r); import orc1_synth as S; "
        "v = S.SampleVoice(); v.load(%r); "
        "print('RESULT', len(v._data) > 1, sum(1 for k in sys.modules if k.startswith('scipy')))"
        % (HERE, synth[0]))
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
res = [l for l in r.stdout.splitlines() if l.startswith("RESULT")]
warned = "WARNING m16_16k.wav is 16000 Hz" in r.stdout
check(res == ["RESULT True 0"] and warned,
      "non-48k sample: loud WARNING at load + ZERO scipy modules imported",
      f"result={res} warned={warned} {r.stderr.strip()[-200:]}")

import shutil                                   # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
EXPECTED = 3
print(f"{PASS} passed, {FAIL} failed  (expected {EXPECTED})")
sys.exit(1 if (FAIL or PASS + FAIL != EXPECTED) else 0)
