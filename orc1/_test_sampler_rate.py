#!/usr/bin/env python3
"""Sampler output-rate conversion (per-host-jackd plan, step 3, 2026-09-21).

The sampler's ring/capture/nudge/region math is 48k-native (pinned to the ring
rate). When a deck's JACK output runs at a different rate (a per-card jackd at
44100, e.g. the Mixtrack), the deck keeps its buffer + all that math at self.sr
(48k) and scales ONLY its render read-step by sr/out_rate, so a 48k grab plays at
the correct PITCH and the loop lasts the correct TIME. The deck's own fractional
interpolating read is the resampler; out_rate == sr is a byte-identical no-op.

Pure stdlib + numpy; builds SampleDecks directly (no jack/mido/ring). Mutation:
force _rate_ratio back to 1.0 in render and the 44100 pitch + read-rate checks go
red (the 48k tone would sound at 1000*44100/48000 = ~919 Hz and _pos would advance
at 1.0/frame instead of 1.088)."""
import sys
import numpy as np

sys.path.insert(0, "/home/pibox/orc/orc1")
import orc1_sampler as smp

SR = 48000
BLOCK = 1024
EXPECTED = 14

_p = _f = 0


def check(cond, label):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label}")


def sine_buf(freq, n, sr=SR, amp=0.5):
    t = np.arange(n, dtype=np.float64) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_freq(buf, fs):
    b = np.asarray(buf, dtype=np.float64)
    b = b - b.mean()
    if np.max(np.abs(b)) < 1e-6:
        return float("nan")
    w = np.hanning(len(b))
    spec = np.abs(np.fft.rfft(b * w))
    freqs = np.fft.rfftfreq(len(b), 1.0 / fs)
    spec[freqs < 50] = 0.0
    i = int(np.argmax(spec))
    if 0 < i < len(spec) - 1:
        a, bb, c = spec[i - 1], spec[i], spec[i + 1]
        den = (a - 2 * bb + c)
        d = 0.5 * (a - c) / den if den else 0.0
    else:
        d = 0.0
    return (i + d) * fs / len(b)


def render_secs(deck, secs, out_rate):
    n = int(secs * out_rate)
    out, got = [], 0
    while got < n:
        b = deck.render(BLOCK)
        if b is None:
            b = np.zeros(BLOCK, dtype=np.float32)
        out.append(np.asarray(b, dtype=np.float64))
        got += len(b)
    return np.concatenate(out)[:n]


# --- 1. ratio field --------------------------------------------------------
d_def = smp.SampleDeck("T", sample_rate=SR)
check(abs(d_def._rate_ratio - 1.0) < 1e-12, "default out_rate -> ratio 1.0")
check(d_def._out_rate == SR, "default out_rate == sample_rate")

d48 = smp.SampleDeck("T", sample_rate=SR, out_rate=48000)
check(abs(d48._rate_ratio - 1.0) < 1e-12, "out_rate 48000 -> ratio 1.0")

d441 = smp.SampleDeck("T", sample_rate=SR, out_rate=44100)
check(abs(d441._rate_ratio - (48000.0 / 44100.0)) < 1e-9,
      "out_rate 44100 -> ratio 48000/44100")

# --- 2. byte-identical at 48k ---------------------------------------------
buf = sine_buf(1000.0, SR * 2)          # 2s of 48k tone
a = smp.SampleDeck("T", sample_rate=SR)             # default out_rate
a.load_buffer(buf.copy())
b = smp.SampleDeck("T", sample_rate=SR, out_rate=48000)
b.load_buffer(buf.copy())
ya = render_secs(a, 0.5, SR)
yb = render_secs(b, 0.5, SR)
check(np.array_equal(ya, yb), "out_rate default == explicit 48000 (byte-identical)")

# --- 3. PITCH preserved at 44100 ------------------------------------------
# Control: play the 48k tone on a 48k deck -> 1000 Hz measured at 48000.
c48 = smp.SampleDeck("T", sample_rate=SR, out_rate=48000)
c48.load_buffer(sine_buf(1000.0, SR * 2))
f48 = peak_freq(render_secs(c48, 1.0, 48000), 48000)
check(abs(f48 - 1000.0) < 5.0, f"control: 48k deck plays 1000 Hz (got {f48:.1f})")

# The fix: same 48k tone on a 44100 deck -> STILL 1000 Hz at the 44100 output.
c441 = smp.SampleDeck("T", sample_rate=SR, out_rate=44100)
c441.load_buffer(sine_buf(1000.0, SR * 2))
f441 = peak_freq(render_secs(c441, 1.0, 44100), 44100)
check(abs(f441 - 1000.0) < 6.0, f"44100 deck plays 1000 Hz, not ~919 (got {f441:.1f})")
check(not (900.0 < f441 < 935.0),
      f"44100 deck is NOT the unscaled ~919 Hz bug (got {f441:.1f})")

# --- 4. read-rate / loop LENGTH: _pos advances sr/out_rate per output frame -
# One steady block from rest: step==1.0, so _pos advances BLOCK*rr buffer-frames.
for out_rate, want_ratio in ((48000, 1.0), (44100, 48000.0 / 44100.0)):
    d = smp.SampleDeck("T", sample_rate=SR, out_rate=out_rate)
    d.load_buffer(sine_buf(1000.0, SR * 4))   # 4s buffer: no wrap in one block
    d.render(BLOCK)
    adv = d._pos                              # started at 0
    check(abs(adv - BLOCK * want_ratio) < 2.0,
          f"out_rate {out_rate}: _pos advanced {adv:.1f} ~ {BLOCK*want_ratio:.1f}")

# Loop LENGTH follows the same ratio: a 1.0s (48k) buffer looped on a 44100 deck
# lasts 1.0s of OUTPUT = 44100 output frames per loop. Measure via _pos wrap.
d = smp.SampleDeck("T", sample_rate=SR, out_rate=44100)
loop_frames = SR                              # 1.0 s at 48k = 48000 buffer frames
d.load_buffer(sine_buf(1000.0, loop_frames))
# frames of OUTPUT to traverse one loop = loop_frames / rr = loop_frames*out/sr
want_out_frames = loop_frames * 44100.0 / 48000.0     # = 44100
prev = d._pos
wrapped_at = None
total = 0
for _ in range(60):                           # up to ~60 blocks (> 1 loop)
    d.render(BLOCK)
    total += BLOCK
    if d._pos < prev:                         # wrapped past the loop end
        wrapped_at = total
        break
    prev = d._pos
check(wrapped_at is not None, "44100 loop wraps within the render window")
check(wrapped_at is not None and abs(wrapped_at - want_out_frames) < 2 * BLOCK,
      f"44100 loop length ~{want_out_frames:.0f} output frames (wrapped at {wrapped_at})")

# --- 5. manager threads out_rate to its decks -----------------------------
class _FakeRing:
    rate = 48000
    channels = 2
    buffer_bytes = 48000 * 2 * 2
    def refresh_geometry(self): return False
    def head_info(self): return (0, 0)
    def snapshot_frames(self, *a, **k): return np.zeros(1024, dtype=np.float32)

# Build under a throwaway HOME + persistence off so constructing the manager
# never reads/writes the live ~/.orc1_sampler_* state (it restores decks + writes
# view files in __init__ otherwise).
import tempfile as _tf, os as _os
_orig_home = _os.environ.get("HOME")
_os.environ["HOME"] = _tf.mkdtemp()
try:
    mgr = smp.SamplerManager(_FakeRing(), 48000, out_rate=44100,
                             cfg={"PERSIST_DECKS": False})
    check(mgr.out_rate == 44100, "manager stores out_rate")
    check(all(abs(dk._rate_ratio - 48000.0 / 44100.0) < 1e-9
              for dk in mgr.decks.values()), "manager passes out_rate to both decks")
finally:
    if _orig_home is not None:
        _os.environ["HOME"] = _orig_home

print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
sys.exit(1 if (_f or _p != EXPECTED) else 0)
