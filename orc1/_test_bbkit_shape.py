#!/usr/bin/env python3
"""
BBKit per-drum shaping (2026-11-04): level / tune / attack / decay / pan / filter
per drum, plus kit tune / kit cutoff, saved per kit.

    python3 _test_bbkit_shape.py [-v]

owner noticed the follow knobs couldn't shape the BeatBuddy kit: the host had
no parameters at all. This checks the host's side by RENDERING audio through the
real Voice and measuring it (pitch from the read positions, envelopes, pan,
filter attenuation), that a drum at its defaults plays EXACTLY as before (the
plain path, byte for byte), the CC handling (pick a drum, then shape it), the
copy-on-write the RT thread relies on, and the per-kit file (read-modify-write).

Pure: no JACK, no MIDI port. HOME / USERPROFILE point at a temp dir before the
host is imported, so its state files are temp files.
"""
import json
import os
import shutil
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="bbkit_shape_test_")
os.environ["HOME"] = _SANDBOX
os.environ["USERPROFILE"] = _SANDBOX
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                       # noqa: E402
import orc1_bbkit_host as H              # noqa: E402
import orc1_config as C                  # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 33
SR = H.SR
CC = C.BBKIT_SHAPE_CC


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def render(voice, frames, block=256):
    L, R = [], []
    for _ in range(0, frames, block):
        a = np.zeros(block, dtype=np.float32)
        b = np.zeros(block, dtype=np.float32)
        voice.mix(a, b, block)
        L.append(a)
        R.append(b)
    return np.concatenate(L)[:frames], np.concatenate(R)[:frames]


def stereo(x):
    return np.stack([x, x], axis=1).astype(np.float32)


check("the host writes its params under the sandbox HOME", H.PARAMS_FILE.startswith(_SANDBOX), H.PARAMS_FILE)
check("the host reads the shared CC table", H.SHAPE_CC == CC)

# ---- defaults = the old plain path, byte for byte ---------------------------------
rng = np.random.default_rng(1)
noise = stereo(rng.uniform(-0.5, 0.5, 6000))
sh0 = H.Shape({}, {})
check("defaults are 'plain'", sh0.plain and sh0.gain == 1.0 and sh0.rate == 1.0)
a = render(H.Voice(noise, 0.7, 36, 0, 0), 6400)
b = render(H.Voice(noise, 0.7, 36, 0, 0, sh0), 6400)
check("a default-shaped hit renders identically to an unshaped one",
      np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1]))
v = H.Voice(noise, 0.7, 36, 0, 0, H.Shape({"pan": 64, "level": 100, "decay": 127}, {"kit_cutoff": 127}))
check("explicit default values also take the plain path", v.shape is None)

# ---- level --------------------------------------------------------------------------
check("level 0 = gain 0", H.Shape({"level": 0}, {}).gain == 0.0)
check("level 127 is about +4 dB", abs(20 * np.log10(H.Shape({"level": 127}, {}).gain) - 4.15) < 0.1,
      H.Shape({"level": 127}, {}).gain)

# ---- tune: the read rate, measured from a ramp ---------------------------------------
ramp = stereo(np.arange(4000, dtype=np.float32) / 4000.0)
sh = H.Shape({"tune": 127}, {})
check("tune 127 = +12 semitones = rate 2", abs(sh.rate - 2.0) < 1e-9, sh.rate)
L, _ = render(H.Voice(ramp, 1.0, 36, 0, 0, sh), 2560)
n_out = int(np.count_nonzero(L))
check("an octave up plays the sample in half the time", abs(n_out - 2000) <= 2, n_out)
check("...reading every 2nd frame", np.allclose(L[1:100], np.arange(1, 100) * 2 / 4000.0, atol=1e-6))
sh = H.Shape({"tune": 64}, {"kit_tune": 0})
check("kit tune 0 = -12 semitones on every drum", abs(sh.rate - 2 ** (-12.19 / 12)) < 0.01, sh.rate)
L, _ = render(H.Voice(ramp, 1.0, 36, 0, 0, H.Shape({"tune": 22}, {})), 9000)
check("down a fifth-ish: linear interpolation stays monotonic, never past the end",
      np.all(np.diff(L[1:np.count_nonzero(L)]) > 0) and L.max() <= 1.0, L.max())

# ---- attack / decay -----------------------------------------------------------------
dc = stereo(np.ones(SR, dtype=np.float32))
L, _ = render(H.Voice(dc, 1.0, 36, 0, 0, H.Shape({"attack": 127}, {})), SR)
i10 = int(0.1 * SR)
check("attack 127 = 200 ms fade-in (half way at 100 ms)", L[0] == 0.0 and abs(L[i10] - 0.5) < 0.01
      and abs(L[int(0.21 * SR)] - 1.0) < 1e-6, (L[0], L[i10]))
sh = H.Shape({"decay": 0}, {})
L, _ = render(H.Voice(dc, 1.0, 36, 0, 0, sh), SR)
check("decay 0: down 1/e after ~15 ms", abs(L[int(0.015 * SR)] - np.exp(-1)) < 0.02, L[int(0.015 * SR)])
v = H.Voice(dc, 1.0, 36, 0, 0, sh)
render(v, int(0.25 * SR))
check("a decayed hit ends (frees its voice) long before the sample does", v.done)
L, _ = render(H.Voice(dc, 1.0, 36, 0, 0, H.Shape({"decay": 126}, {})), SR)
check("decay 126 is a long tail (~2.9 s)", L[SR - 1] > 0.7, L[SR - 1])

# ---- pan ------------------------------------------------------------------------------
L, R = render(H.Voice(noise, 1.0, 36, 0, 0, H.Shape({"pan": 0}, {})), 4096)
check("pan 0 = hard left", np.abs(R).max() == 0.0 and np.abs(L).max() > 0.4)
L, R = render(H.Voice(noise, 1.0, 36, 0, 0, H.Shape({"pan": 96}, {})), 4096)
check("pan 96: right at full, left about half", np.allclose(R[:4096], noise[:4096, 1])
      and abs(np.abs(L).sum() / np.abs(R).sum() - 31 / 63.0) < 0.01)

# ---- filter -----------------------------------------------------------------------------
t = np.arange(SR // 2) / SR
hiss = stereo(0.5 * np.sin(2 * np.pi * 8000 * t))
op, _ = render(H.Voice(hiss, 1.0, 36, 0, 0, H.Shape({}, {})), SR // 2)
lp, _ = render(H.Voice(hiss, 1.0, 36, 0, 0, H.Shape({"cutoff": 40}, {})), SR // 2)
att = 20 * np.log10(np.sqrt(np.mean(lp[2000:] ** 2)) / np.sqrt(np.mean(op[2000:] ** 2)))
check("cutoff 40 (~460 Hz) takes an 8 kHz tone down > 30 dB", att < -30, round(att, 1))
check("the lower of drum and kit cutoff wins", H.Shape({"cutoff": 100}, {"kit_cutoff": 40}).cutoff
      == H.Shape({"cutoff": 40}, {}).cutoff)

# ---- the engine's CC handling --------------------------------------------------------------
eng = H.KitEngine.__new__(H.KitEngine)
eng.params, eng.kit_params, eng._target, eng._params_dirty = {}, {}, None, False
eng.set_shape(CC["level"], 50)
check("a drum CC before any pick changes nothing", eng.params == {} and not eng._params_dirty)
eng.set_shape(CC["note"], 38)
before = eng.params
eng.set_shape(CC["level"], 50)
eng.set_shape(CC["tune"], 200)
check("pick 38, then level + tune land on 38 (values clamped)", eng.params == {38: {"level": 50, "tune": 127}},
      eng.params)
check("copy-on-write: the dict the RT thread held is untouched", before == {})
eng.set_shape(CC["kit_cutoff"], 30)
check("kit CCs go to the kit", eng.kit_params == {"kit_cutoff": 30} and eng._params_dirty)
check("an unrelated CC is not ours", not eng.set_shape(7, 10))
check("shape_for uses both", eng.shape_for(38).gain == 0.25 and eng.shape_for(36).cutoff is not None)

eng.voices = []
eng.instruments = {38: H.Instrument(38, "Snare", 0, 0, 100, [H.Layer(0, 127, [noise])]),
                   36: H.Instrument(36, "Kick", 0, 0, 100, [H.Layer(0, 127, [noise])])}
eng.set_shape(CC["level"], 0)
eng._trigger(38, 100, 0)
eng._trigger(36, 100, 0)
check("level 0 = that drum is skipped (no voice); others still play",
      [v.note for v in eng.voices] == [36], [v.note for v in eng.voices])

# ---- the per-kit file ----------------------------------------------------------------------
pf = os.path.join(_SANDBOX, "params.json")
with open(pf, "w") as f:
    json.dump({"kits": {"Jazz": {"notes": {"36": {"level": 90}}, "kit": {}}}, "other": 1}, f)
H.save_kit_params("Latin", {38: {"level": 50, "tune": 64}, 40: {"pan": 64}}, {"kit_tune": 70}, path=pf)
d = json.load(open(pf))
check("save keeps other kits and keys (read-modify-write)", d["kits"]["Jazz"]["notes"]["36"]["level"] == 90
      and d.get("other") == 1)
check("save drops values equal to the default", d["kits"]["Latin"] == {"notes": {"38": {"level": 50}},
      "kit": {"kit_tune": 70}}, d["kits"]["Latin"])
n, k = H.load_kit_params("Latin", path=pf)
check("load round-trips (note keys back to ints)", n == {38: {"level": 50}} and k == {"kit_tune": 70}, (n, k))
H.save_kit_params("Latin", {38: {"level": 100}}, {}, path=pf)
check("a kit back at all defaults leaves the file", "Latin" not in json.load(open(pf))["kits"])

# ---- the router + warm-up ---------------------------------------------------------------------
import types                              # noqa: E402
got = []
fake = types.SimpleNamespace(set_shape=lambda c, v: got.append((c, v)), voices=[],
                             note_on=lambda n, v: got.append(("note", n)), program=lambda p: None)
H.S._BINDINGS = {2: "bbkit"}
route = H._make_router(fake)
M = lambda t, **kw: types.SimpleNamespace(type=t, **kw)   # noqa: E731
route(M("control_change", channel=1, control=CC["note"], value=38))
route(M("control_change", channel=4, control=CC["level"], value=9))
route(M("note_on", channel=1, note=38, velocity=90))
check("the router hands kit-channel CCs to set_shape (other channels ignored)",
      got == [(CC["note"], 38), ("note", 38)], got)
H._warm_shaped()
check("the shaped warm-up runs", True)

shutil.rmtree(_SANDBOX, ignore_errors=True)
total = PASS + FAIL
print("%d passed, %d failed" % (PASS, FAIL))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
    sys.exit(1)
sys.exit(1 if FAIL else 0)
