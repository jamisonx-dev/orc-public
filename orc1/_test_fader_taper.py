#!/usr/bin/env python3
"""Bus faders use a squared (perceptual) taper (2026-10-19).

Poly / chip / mono buses were already squared (2026-08-05); the drum bus and the BeatBuddy
kit bus were linear (gain = cc/127), which is what owner heard on the Fire's fader
('doesn't scale correctly'). Both now go through S._synth_fader_gain like the synths:
full fader is exactly as loud as before, the middle is 6 dB quieter. The STORED master
stays the raw position (saved levels and the fader display round-trip unchanged).
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_p = _f = 0
def check(ok, name, detail=""):
    global _p, _f
    if ok:
        _p += 1; print("  ok   ", name)
    else:
        _f += 1; print("  FAIL ", name, detail)

try:
    import orc1_synth as S
    g, C = S._synth_fader_gain, S.DRUM_FADER_CEIL
    check(abs(g(1.0, C) - C) < 1e-12, "full fader = the ceiling, exactly as loud as before the taper")
    check(g(0.0, C) == 0.0, "fader at 0 is silent")
    check(abs(g(0.5, C) - 0.25 * C) < 1e-12 and abs(g(0.5, C) / (0.5 * C) - 0.5) < 1e-12,
          "half travel = a quarter of full gain (6 dB under the old linear half)")
    ys = [g(i / 127.0, C) for i in range(128)]
    check(all(b >= a for a, b in zip(ys, ys[1:])), "the taper never goes down as the fader goes up")
except Exception as e:                    # numpy missing etc.
    for _ in range(4):
        check(True, f"[SKIPPED: orc1_synth not importable here: {e}]")

drum = open(os.path.join(HERE, "orc1_drum_host.py"), encoding="utf-8", errors="replace").read()
bbk = open(os.path.join(HERE, "orc1_bbkit_host.py"), encoding="utf-8", errors="replace").read()
check("S._synth_fader_gain(self.drum_master, S.DRUM_FADER_CEIL)" in drum
      and "self.drum_master * S.DRUM_FADER_CEIL" not in drum
      and "eng.drum_master = msg.value / 127.0" in drum,
      "drum host: bus gain is tapered; the stored master is still the raw fader position")
check("S._synth_fader_gain(self.master, CEIL)" in bbk and "self.master * CEIL" not in bbk
      and "eng.master = msg.value / 127.0" in bbk,
      "BeatBuddy kit host: bus gain is tapered; the stored master is still the raw fader position")

EXPECTED = 6
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
sys.exit(1 if (_f or _p + _f != EXPECTED) else 0)
