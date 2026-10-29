#!/usr/bin/env python3
"""First NOISE note after a host restart must not miss its audio deadline.

MEASURED 2026-10-19 (Pi): the first np.random call in a fresh process takes ~34 ms
(numpy seeds its global generator lazily; later calls are microseconds). Paid
inside the JACK callback by the first snare (any noise voice) after a host restart
= an xrun and an audible pop (found while verifying a BeatBuddy fix; the drum host
xrun'd on the first snare, every time). orc1_synth.preload_midi_backend() --
called by every host before JACK activates -- now seeds it first. Pure timing check in a fresh subprocess; skips if numpy/scipy
are missing.
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_p = _f = 0
def check(ok, name, detail=""):
    global _p, _f
    if ok:
        _p += 1; print("  ok   ", name)
    else:
        _f += 1; print("  FAIL ", name, detail)

LIMIT_MS = 12.0
code = r'''
import sys, time
sys.path.insert(0, %r)
try:
    import orc1_synth as S
except Exception as e:
    print("SKIP", e); sys.exit(0)
S.preload_midi_backend()
v = S.Snare()
v.trigger(100)
t = time.perf_counter(); v.render(1024); first = (time.perf_counter() - t) * 1000
print("FIRST_MS", first)
''' % HERE
worst = None
skipped = False
for _ in range(3):                      # a loaded box can hiccup once; the bug is EVERY time
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True).stdout
    if "SKIP" in out:
        skipped = True; break
    ms = float(out.split("FIRST_MS")[1])
    worst = ms if worst is None else min(worst, ms)
if skipped:
    check(True, "[SKIPPED: orc1_synth not importable here]")
else:
    check(worst < LIMIT_MS, f"first snare render after preload is fast (best of 3: {worst:.1f} ms < {LIMIT_MS} ms)",
          f"{worst:.1f} ms")

SRC = open(os.path.join(HERE, "orc1_synth.py"), encoding="utf-8", errors="replace").read()
pre = SRC[SRC.index("def preload_midi_backend("):SRC.index("def _warm_noise(")]
check("_warm_noise()" in pre, "preload_midi_backend seeds numpy's RNG")
hosts = [f for f in os.listdir(HERE) if re.fullmatch(r"orc1_\w*(host|fxmix)\.py", f)]
missing = [h for h in hosts
           if "preload_midi_backend()" not in open(os.path.join(HERE, h), encoding="utf-8", errors="replace").read()]
check(len(hosts) >= 6 and not missing, f"every audio host calls preload_midi_backend ({len(hosts)} hosts)", str(missing))

EXPECTED = 3
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
sys.exit(1 if (_f or _p + _f != EXPECTED) else 0)
