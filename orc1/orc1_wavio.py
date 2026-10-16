"""WAV read + FFT resample WITHOUT importing scipy (2026-09-23).

The drum host's SampleVoice.load and the chip PCMChannel loader used
`scipy.io.wavfile.read` + `scipy.signal.resample`. Importing scipy.signal pulls
~530 modules / ~48 MB private into the process (measured on the Pi) -- the drum
host sat at ~98 MB private while the other hosts dropped to ~20 MB once
orc1_lfilter.py removed their scipy.signal import. These two replacements keep
that host scipy-free too:

  read_wav(path)  -> (rate, data) EXACTLY as scipy.io.wavfile.read returns it for
                     8-bit (uint8) and 16-bit (int16) PCM, mono (n,) or multi
                     (n, ch). Any other format (24/32-bit, float, extensible
                     headers the stdlib reader rejects) falls back to the real
                     scipy reader, so nothing that loaded before can stop loading.
  resample(x, num) -> scipy.signal.resample's real-input FFT method (rfft, keep
                     min(num, n)//2+1 bins, halve/double the unpaired bin, scale,
                     irfft), with numpy.fft. numpy >= 2 keeps float32 as single
                     precision like scipy.fft; results agree to float rounding
                     (_test_wavio.py checks it against scipy on the real samples).
"""
import wave

import numpy as np


def read_wav(path):
    try:
        with wave.open(path, "rb") as w:
            sw, ch = w.getsampwidth(), w.getnchannels()
            rate, n = w.getframerate(), w.getnframes()
            if w.getcomptype() != "NONE" or sw not in (1, 2):
                raise ValueError("not 8/16-bit PCM")
            raw = w.readframes(n)
    except Exception:
        import scipy.io.wavfile as _wf          # anything else: the real reader
        return _wf.read(path)
    dt = np.uint8 if sw == 1 else np.dtype("<i2")
    data = np.frombuffer(raw, dtype=dt)
    data = data[:len(data) - len(data) % ch]
    if ch > 1:
        data = data.reshape(-1, ch)
    return rate, data.copy()                    # writable, like scipy's


def resample(x, num):
    x = np.asarray(x)
    n_x = x.shape[-1]
    num = int(num)
    s_fac = n_x / num
    m = min(num, n_x)
    m2 = m // 2 + 1
    X = np.fft.rfft(x)[..., :m2]
    if m % 2 == 0 and num != n_x:              # the unpaired bin at m//2
        X[..., m // 2] *= 2 if num < n_x else 0.5
    return np.fft.irfft(X / s_fac, n=num)
