#!/usr/bin/env python3
"""_test_visuals.py -- the VISUALS renderer's logic (daw/orc_visuals.py),
without a display: pygame + moderngl are stubbed, so it runs anywhere.

Covers what the music and the knobs do (the GPU side is checked by eye):
  * a kick / snare counts only on a channel bound to drums or bbkit
  * the colour follows the LOWEST held tonal note round the circle of fifths
  * the visuals module's own channel is knobs only: CC 21-27 move knobs,
    CC 20 asks for a scene, and a note there (the bind handshake's commit
    note) never touches the colour
  * CCs on any other channel don't move the knobs
  * knob positions round-trip through the state file; junk is ignored
  * the batched line builder = the old per-call build, segment for segment
  * every scene draws at 1280x720 without error
  * the JACK TAP (orc1-vistap's shared memory, faked in a temp file): newest
    frames exact across the ring wrap, stale/dead/unconnected = off, the tap
    wins over the ring and hands back to it, beats + first-frame latency
    through it, the stream's sample rate, the source file, and the C header
    layout = the Python reader's (compiled with gcc when there is one)
Bindings and the state file are pointed at a temp dir (never the live ones).
"""
import json
import math
import os
import sys
import tempfile
import time
import types

EXPECTED = 152
HERE = os.path.dirname(os.path.abspath(__file__))
for m in ("pygame", "moderngl"):
    sys.modules.setdefault(m, types.ModuleType(m))
sys.path.insert(0, HERE)
import numpy as np                                   # noqa: E402
import orc_visuals as V                              # noqa: E402

TMP = tempfile.mkdtemp(prefix="vis_test_")
# /tmp on the Pi is RAM (tmpfs). Each run's fake rings are ~22 MB; left behind,
# 45 runs filled /tmp to 100% (923 MB of RAM) on 2026-09-30. Always clean up.
import atexit                                        # noqa: E402
import shutil                                        # noqa: E402
atexit.register(shutil.rmtree, TMP, True)
V.BINDINGS = os.path.join(TMP, "bindings.json")
V.STATE = os.path.join(TMP, "state.json")
V.TAP_SHM = os.path.join(TMP, "no_tap_shm")          # never the live tap
V.TAP_SRC = os.path.join(TMP, "vistap_src")
with open(V.BINDINGS, "w") as f:                    # drums ch10, poly ch6, visuals ch15
    json.dump({"bindings": {"10": "drums", "6": "poly", "2": "bbkit",
                            "15": "visuals"}}, f)

passed = failed = 0


def ok(cond, name):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", name)


class Msg:
    def __init__(self, type, channel, **kw):
        self.type, self.channel = type, channel
        self.__dict__.update(kw)


def note(ch1, n, vel=100):
    return Msg("note_on", ch1 - 1, note=n, velocity=vel)


def off(ch1, n):
    return Msg("note_off", ch1 - 1, note=n, velocity=0)


def cc(ch1, c, v):
    return Msg("control_change", ch1 - 1, control=c, value=v)


# ---- kicks / snares --------------------------------------------------------
m = V.Music()
m.on_msg(note(10, 36, 127))
ok(time.monotonic() - m.kick_t < 0.5, "kick on the drums channel pulses")
ok(abs(m.kick_v - 1.0) < 0.01, "full-velocity kick = full pulse")
m = V.Music()
m.on_msg(note(2, 35, 64))
ok(m.kick_t > 0, "kick on the bbkit channel pulses")
m = V.Music()
m.on_msg(note(6, 36, 127))
ok(m.kick_t < 0, "note 36 on the poly channel is NOT a kick")
m.on_msg(note(10, 38))
ok(m.snare_t > 0, "snare on the drums channel flashes")
ok(not any(k[0] == 9 for k in m.held), "drum notes never set the colour")

# ---- colour ------------------------------------------------------------------
m = V.Music()
m.on_msg(note(6, 64))
m.on_msg(note(6, 57))
ok(m.hue == 90.0, "lowest held note A -> fifths step 3 -> hue 90")
m.on_msg(off(6, 57))
m.on_msg(note(6, 60))
ok(m.hue == 0.0 and sorted(m.held.values()) == [60, 64],
   "A released, C+E held -> C -> hue 0")
m.on_msg(note(13, 50))
ok(13 - 1 not in {k[0] for k in m.held}, "ch13 (control) notes ignored")

# ---- the visuals channel -----------------------------------------------------
m = V.Music()
hue0 = m.hue
m.on_msg(cc(15, 21, 100))
ok(m.knobs[21] == 100 and m.knobs_dirty, "CC21 on the visuals channel moves SPEED")
m.on_msg(cc(15, 27, 5))
ok(m.knobs[27] == 5, "CC27 on the visuals channel moves COLOUR")
m.on_msg(cc(15, 20, 127))
ok(m.scene_req == 127, "CC20 on the visuals channel asks for a scene")
m.on_msg(note(15, 1, 1))
ok(m.hue == hue0 and not m.held, "the bind handshake note on our channel is ignored")
m.on_msg(cc(6, 22, 9))
ok(m.knobs[22] == 64, "CC22 on ANOTHER channel does not move a knob")
m.on_msg(cc(15, 7, 99))
ok(7 not in m.knobs, "a CC outside 20-27 on our channel is ignored")

# ---- state file ----------------------------------------------------------------
V.save_state(m.knobs, "warp", False)
with open(V.STATE) as f:
    st = json.load(f)
ok(st["knobs"]["speed"] == 100 and st["scene"] == "warp" and st["cycling"] is False,
   "state file carries knobs by NAME + scene + cycling")
ok(V.load_knobs()[21] == 100 and V.load_knobs()[27] == 5,
   "knob positions come back after a restart")
with open(V.STATE, "w") as f:
    json.dump({"knobs": {"speed": "fast", "twist": 999, "trails": 10}}, f)
k = V.load_knobs()
ok(k[21] == 64 and k[22] == 64 and k[23] == 10, "junk knob values fall back to 64")

# ---- line builder --------------------------------------------------------------


def old_build(pts, col, width, closed):
    p = np.asarray(pts, dtype="f4")
    if closed:
        p = np.vstack([p, p[:1]])
    a, b = p[:-1], p[1:]
    d = b - a
    ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-4)[:, None]
    r = width * 3.0
    n = np.stack([-d[:, 1], d[:, 0]], 1) / ln * r
    t = d / ln * r * 0.5
    a0, b0 = a - t, b + t
    v = np.zeros((len(a), 6, 6), "f4")
    v[:, :, 0:2] = np.stack([a0 + n, a0 - n, b0 + n, b0 + n, a0 - n, b0 - n], 1)
    v[:, :, 2] = np.array([1, -1, 1, 1, -1, -1])
    v[:, :, 3:6] = col
    return v.reshape(-1, 6)


L, ref = V.Lines(), []
for c in ([(0, 0), (10, 5), (20, -3), (4, 4)], (1, .5, .2), 1.5, True), \
         ([(3, 3), (9, 9)], (.1, .2, .3), .7, False):
    L.add(*c)
    ref.append(old_build(*c))
got, want = L.array(), np.concatenate(ref)
ok(got.shape == want.shape and np.allclose(got, want, atol=1e-4),
   "batched line builder == per-call build")
ok(V.Lines().array() is None, "an empty frame builds nothing")

# ---- audio tap (a fake skipback ring in a temp dir) -------------------------------
SR = 48000


def fake_skipbuf(tracks):
    """tracks: {id: (dir, name, channels, store)} -> a temp ~/.skipbuf."""
    root = tempfile.mkdtemp(prefix="vis_sb_", dir=TMP)
    heads = {}
    for tid, (d, name, ch, store) in tracks.items():
        os.makedirs(os.path.join(root, d))
        size = SR * ch * 2 * 4                         # 4 s ring
        with open(os.path.join(root, d, "ring.raw"), "wb") as f:
            f.write(b"\0" * size)
        with open(os.path.join(root, d, "ring.pos"), "w") as f:
            f.write("0")
        heads[tid] = {"dir": d, "name": name, "channels": ch,
                      "buffer_bytes": size, "store": store}
    with open(os.path.join(root, "heads.json"), "w") as f:
        json.dump({"tracks": heads}, f)
    return root


def play(root, d, ch, signal_fn, secs, tap, fps=30):
    """Write signal_fn(t) into the ring frame by frame like the recorder,
    calling tap.update() at fps. Returns the times update() saw a beat."""
    size = SR * ch * 2 * 4
    pos, beats, lv = 0, [], []
    per = SR // fps
    for k in range(int(secs * fps)):
        t = np.arange(k * per, (k + 1) * per) / SR
        x = np.clip(signal_fn(t), -1, 1)
        pcm = (np.repeat(x[:, None], ch, 1) * 32767).astype("<i2").tobytes()
        with open(os.path.join(root, d, "ring.raw"), "r+b") as f:
            first = min(len(pcm), size - pos)
            f.seek(pos)
            f.write(pcm[:first])
            if first < len(pcm):
                f.seek(0)
                f.write(pcm[first:])
        pos = (pos + len(pcm)) % size
        with open(os.path.join(root, d, "ring.pos"), "w") as f:
            f.write(str(pos))
        b, _, l = tap.update(k / fps)
        if b > 0:
            beats.append(k / fps)
        if l is not None:
            lv.append(l)
    return beats, lv


def kicks(gain):
    def fn(t):
        ph = t % 0.5                                   # 120 BPM
        return gain * np.sin(2 * np.pi * 60 * t) * np.exp(-ph * 25)
    return fn


def fresh():
    # a NEW ring per scenario: a reused ring leaks the last scenario's audio
    # into the first read and jams the auto gain (it hid a mutation once)
    return fake_skipbuf({"0": ("t0", "MAIN", 2, "card"), "2": ("t2", "SMP MAIN", 2, "ram"),
                         "3": ("t3", "SMP IN3", 1, "ram")})


root = fresh()
root = fresh()
tap = V.AudioTap(root)
tap._pick(0.0)
ok(tap.dir and tap.dir.endswith("t2"), "audio tap picks the RAM stereo MAIN ring over card / mono")

beats, _ = play(root, "t2", 2, kicks(0.5), 8.0, tap)
iv = np.diff(beats)
ok(len(beats) >= 12 and np.all(np.abs(iv - 0.5) < 0.08),
   f"120 BPM kicks -> a beat every 0.5 s ({len(beats)} beats)")

root = fresh()
tap = V.AudioTap(root)
beats_q, lv_q = play(root, "t2", 2, kicks(0.004), 8.0, tap)   # ~ -60 dBFS rms,
ok(len(beats_q) >= 12, f"QUIET kicks (-60 dBFS rms, the real BT level) still beat ({len(beats_q)})")
ok(max(lv_q) > 0.8, "quiet music still reaches full level after the auto gain")

root = fresh()
tap = V.AudioTap(root)
beats_s, _ = play(root, "t2", 2, lambda t: 0.3 * np.sin(2 * np.pi * 80 * t), 6.0, tap)
ok(len(beats_s) == 0, "a steady bass tone is not a beat")

root = fresh()
tap = V.AudioTap(root)
beats_z, lv_z = play(root, "t2", 2, lambda t: 0 * t, 3.0, tap)
ok(len(beats_z) == 0 and max(lv_z[5:] or [0]) == 0.0, "silence = no beats, level 0")
root = fresh()
tap = V.AudioTap(root)
_, lv_n = play(root, "t2", 2, lambda t: 0.0002 * np.sin(2 * np.pi * 997 * t), 6.0, tap)
ok(max(lv_n[5:]) < 0.35, "the -74 dBFS noise between songs stays LOW (the gain floor)")

tap = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"))
ok(tap.update(1.0) == (0.0, 0.0, 0.0), "no skipback at all = quiet no-op, no crash")

# ---- the beat BAND (CC 32/33) + latency ----------------------------------------


def kicks_and_hats(t):
    """60 Hz kicks on the beat (0.0, 0.5, ...) + 7 kHz hats on the offbeat
    (0.25, 0.75, ...), 120 BPM."""
    kph, hph = t % 0.5, (t - 0.25) % 0.5
    return (0.4 * np.sin(2 * np.pi * 60 * t) * np.exp(-kph * 25)
            + 0.3 * np.sin(2 * np.pi * 7000 * t) * np.exp(-hph * 60))


def phase_of(beats):
    ph = [b % 0.5 for b in beats[4:]]
    return sum(ph) / len(ph) if ph else None


ok(abs(V.cc_to_hz(V.KNOB_DEFAULT[32]) - 30) < 2 and abs(V.cc_to_hz(V.KNOB_DEFAULT[33]) - 150) < 5
   and all(V.hz_to_cc(V.cc_to_hz(v)) == v for v in range(128)),
   "band knobs: default 30-150 Hz; CC <-> Hz round-trips at every step")
root = fresh()
tap = V.AudioTap(root)
b_kick, _ = play(root, "t2", 2, kicks_and_hats, 8.0, tap)
root = fresh()
tap = V.AudioTap(root)
tap.set_band(V.cc_to_hz(V.hz_to_cc(5000)), V.cc_to_hz(V.hz_to_cc(10000)))
b_hat, _ = play(root, "t2", 2, kicks_and_hats, 8.0, tap)
pk, ph = phase_of(b_kick), phase_of(b_hat)
ok(pk is not None and ph is not None and pk < 0.1 and 0.22 < ph < 0.35,
   f"the BAND picks what beats: 30-150 Hz -> the kicks (phase {pk}), 5-10 kHz -> the hats ({ph})")
root = fresh()
tap = V.AudioTap(root)
tap.set_band(58, 62)                          # narrower than one ~47 Hz bin
b_narrow, _ = play(root, "t2", 2, kicks_and_hats, 6.0, tap)
ok(len(b_narrow) >= 8, "a band narrower than one FFT bin still listens (nearest bin)")
root = fresh()
tap = V.AudioTap(root)
play(root, "t2", 2, lambda t: 0.2 * np.sin(2 * np.pi * 60 * t), 1.0, tap)
peak = max(range(len(tap.spec)), key=lambda i: -999 if tap.spec[i] is None else tap.spec[i])
ok(tap.spec[peak] is not None and 45 <= 20 * 800 ** ((peak + 0.5) / tap.SPEC_BANDS) <= 80,
   "the page's spectrum peaks at the tone (a steady 60 Hz)")
late = [b % 0.5 for b in b_kick[4:]]
ok(late and max(late) <= 1 / 30 + 0.002,
   f"a kick shows on the FIRST frame after it lands (worst {max(late) * 1000:.0f} ms, window {V.AudioTap.N / 48:.0f} ms)")

# ---- the JACK TAP (orc1-vistap shared memory, faked in a temp file) ------------------


class FakeShm:
    """What orc_jacktap.c writes, as a plain temp file the reader mmaps."""

    def __init__(self, cap=16384, rate=48000, period=1024):
        self.path = tempfile.mktemp(prefix="vis_shm_", dir=TMP)
        self.cap, self.rate, self.period = cap, rate, period
        self.wpos, self.alive, self.conn = 0, 1, 2
        self.magic, self.mono = b"ORCTAP1\0", time.monotonic()
        with open(self.path, "wb") as f:
            f.write(b"\0" * (V.TapShm.HDR.size + cap * 2 * 4))
        self.hdr()

    def hdr(self, **kw):
        self.__dict__.update(kw)
        with open(self.path, "r+b") as f:
            f.write(V.TapShm.HDR.pack(self.magic, self.rate, 2, self.cap, self.period,
                                      self.wpos, self.mono, self.conn, self.alive, 0, 1))

    def push(self, fr):
        """Append float32 frames [n, 2] like one JACK period."""
        fr = np.asarray(fr, "<f4").reshape(-1, 2)
        with open(self.path, "r+b") as f:
            k = self.wpos % self.cap
            first = min(len(fr), self.cap - k)
            f.seek(V.TapShm.HDR.size + k * 8)
            f.write(fr[:first].tobytes())
            if first < len(fr):
                f.seek(V.TapShm.HDR.size)
                f.write(fr[first:].tobytes())
        self.wpos += len(fr)
        self.hdr(mono=time.monotonic())


def play_tap(shm, signal_fn, secs, tap, fps=30, rate=48000):
    """Feed signal_fn(t) through the fake tap one period at a time, calling
    tap.update() at fps (1024-frame periods, like the rig)."""
    beats, per, sent = [], shm.period, 0
    for k in range(int(secs * fps)):
        want = int((k + 1) / fps * rate)
        while sent + per <= want:
            t = np.arange(sent, sent + per) / rate
            x = np.clip(signal_fn(t), -1, 1).astype("f4")
            shm.push(np.stack([x, x], 1))
            sent += per
        b, _, _ = tap.update(k / fps)
        if b > 0:
            beats.append(k / fps)
    return beats


ts = V.TapShm(os.path.join(TMP, "no_such_shm"))
ok(ts.read(0.0, 4096) == ("off", None), "tap: no shared memory = off (the ring takes over)")

sh = FakeShm(cap=8192)
ramp = np.arange(12000, dtype="f4")
sh.push(np.stack([ramp, -ramp], 1))            # wpos 12000 > cap: the span wraps
ts = V.TapShm(sh.path)
st, fr = ts.read(0.0, 4096)
ok(st == "new" and fr.shape == (4096, 2) and fr[0, 0] == 12000 - 4096 and fr[-1, 0] == 11999
   and np.all(np.diff(fr[:, 0]) == 1) and np.all(fr[:, 1] == -fr[:, 0]),
   "tap: the newest N frames come back exact, in order, across the ring's wrap")
ok(ts.read(0.1, 4096) == ("same", None), "tap: nothing new since the last read = 'same' (no re-use)")

offs = []
for kw in ({"alive": 0}, {"conn": 0}, {"mono": time.monotonic() - 1.0}, {"magic": b"NOTATAP\0"}):
    s2 = FakeShm()
    s2.push(np.zeros((5000, 2)))
    s2.hdr(**kw)
    offs.append(V.TapShm(s2.path).read(0.0, 4096)[0])
ok(offs == ["off"] * 4, f"tap: stopped / unconnected / stale / not ours = off ({offs})")
s3 = FakeShm()
s3.push(np.zeros((1000, 2)))
ok(V.TapShm(s3.path).read(0.0, 4096) == ("same", None), "tap: just started (< N frames) waits")

# the tap wins over the ring, and the ring takes over when the tap stops
root = fresh()
sh = FakeShm()
tap = V.AudioTap(root, shm=sh.path)
play(root, "t2", 2, lambda t: 0.3 * np.sin(2 * np.pi * 440 * t), 0.2, tap)  # ring has 440 Hz
sh.push(np.full((4096, 2), 0.25, "f4"))
tap.update(1.0)
ok(tap.source == "tap" and np.allclose(tap.frames, 0.25),
   "tap: with both running, the visuals hear the TAP, not the ring")
sh.hdr(mono=time.monotonic() - 2.0)            # the tap stalls / dies
play(root, "t2", 2, lambda t: 0.3 * np.sin(2 * np.pi * 440 * t), 0.2, tap)
back = tap.source
sh.push(np.full((2048, 2), 0.1, "f4"))         # ... and comes back
tap.update(2.0)
ok(back == "ring" and tap.source == "tap", "tap: stale -> the ring takes over; fresh again -> back to the tap")

sh = FakeShm()
tap = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"), shm=sh.path)
b_tap = play_tap(sh, kicks(0.5), 8.0, tap)
iv = np.diff(b_tap)
ok(len(b_tap) >= 12 and np.all(np.abs(iv - 0.5) < 0.08),
   f"tap: 120 BPM kicks -> a beat every 0.5 s with no skipback at all ({len(b_tap)} beats)")
late = [b % 0.5 for b in b_tap[4:]]
ok(late and max(late) <= 1 / 30 + 0.022,
   f"tap: a kick shows within a frame + one JACK period (worst {max(late) * 1000:.0f} ms)")

sh = FakeShm(rate=44100)
tap = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"), shm=sh.path)
play_tap(sh, lambda t: 0.2 * np.sin(2 * np.pi * 60 * t), 1.0, tap, rate=44100)
peak = max(range(len(tap.spec)), key=lambda i: -999 if tap.spec[i] is None else tap.spec[i])
lo_hz, hi_hz = tap.freq[tap.band_m][[0, -1]]
ok(tap.rate == 44100 and 45 <= 20 * 800 ** ((peak + 0.5) / tap.SPEC_BANDS) <= 80
   and lo_hz >= 30 and hi_hz <= 150,
   f"tap: the stream's own rate (44.1k) re-fits the bins; 60 Hz still peaks, band {lo_hz:.0f}-{hi_hz:.0f} Hz")

with open(V.TAP_SRC, "w") as f:
    f.write("ORC1-Drum:out_L\n\n")
mono = V.tap_ports()
with open(V.TAP_SRC, "w") as f:
    f.write("system:capture_13\nsystem:capture_14\n")
pair = V.tap_ports()
os.remove(V.TAP_SRC)
ok(mono == ["ORC1-Drum:out_L"] and pair == ["system:capture_13", "system:capture_14"]
   and V.tap_ports() == ["system:capture_1", "system:capture_2"],
   "tap source file: a blank right = mono; two lines = a pair; no file = capture_1 + 2")

csrc = open(os.path.join(HERE, "orc_jacktap.c"), encoding="utf-8").read()
websrc = open(os.path.join(HERE, "..", "orc1_web.py"), encoding="utf-8").read()
ok('"system:capture_1"' in csrc and '"system:capture_2"' in csrc
   and '["system:capture_1", "system:capture_2"]' in websrc,
   "the default source agrees: the C tap = the renderer's label = the web page")

V.AUDIO_PUB = os.path.join(TMP, "audio.json")
sh = FakeShm()
tap = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"), shm=sh.path)
play_tap(sh, kicks(0.3), 0.5, tap)
V.publish_audio(tap, 0.1, 0.5, 30)
pub = json.load(open(V.AUDIO_PUB))
ok(pub["source"] == "tap" and pub["tap_ports"] == ["system:capture_1", "system:capture_2"]
   and abs(pub["cadence_ms"] - 21.3) < 0.1 and pub["age_ms"] is not None,
   "the page is told it's the tap, which ports, the 21.3 ms period and the audio's age")


def c_layout():
    """The C struct's field offsets, compiled for real; None = no gcc here."""
    import re
    import shutil
    import subprocess
    if not shutil.which("gcc"):
        return None
    body = re.search(r"struct tap_hdr \{(.*?)\};", csrc, re.S).group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)        # comments out
    names = re.findall(r"(\w+)(?:\[\d+\])?;", body)
    prog = ("#include <stdint.h>\n#include <stddef.h>\n#include <stdio.h>\n"
            "struct tap_hdr {%s};\nint main(void){\n" % body
            + "".join('printf("%%zu ", offsetof(struct tap_hdr, %s));\n' % n for n in names)
            + 'printf("%zu\\n", sizeof(struct tap_hdr)); return 0; }\n')
    d = tempfile.mkdtemp(dir=TMP)
    with open(os.path.join(d, "l.c"), "w") as f:
        f.write(prog)
    subprocess.run(["gcc", "-o", os.path.join(d, "l"), os.path.join(d, "l.c")], check=True)
    out = subprocess.run([os.path.join(d, "l")], capture_output=True, text=True).stdout
    return names, [int(x) for x in out.split()]


lay = c_layout()
if lay is None:
    print("  [SKIPPED: no gcc] C header layout check")
    ok(True, "C header layout (skipped: no gcc)")
else:
    names, offs = lay
    import struct as _st
    fmt = V.TapShm.HDR.format.lstrip("<")
    py_offs = [0, 8, 12, 16, 20, 24, 32, 40, 44, 48, 52, 56]
    ok(names[:11] == ["magic", "rate", "channels", "cap", "period", "wpos", "mono", "conn",
                      "alive", "xruns", "pid"]
       and offs == py_offs + [64] and V.TapShm.HDR.size == 64
       and _st.calcsize("<8s4IQd") == 40,
       f"the C tap's header (compiled) = what the Python reader unpacks ({offs})")

# ---- frame pacing (vsync) -----------------------------------------------------------
ok(V.vsync_plan(60, 60.0) == 1 and V.vsync_plan(30, 60.0) == 2 and V.vsync_plan(60, 59.94) == 1
   and V.vsync_plan(45, 60.0) == 0 and V.vsync_plan(30, 50.0) == 0 and V.vsync_plan(60, 50.0) == 0,
   "vsync: 60 fps = every refresh, 30 = every 2nd; 45 on 60 Hz / anything uneven = no vsync")
r = 60.0
ok(all(abs(V.frame_dt(raw, r, 1) - 1 / 60) < 1e-9 for raw in (0.0152, 0.0167, 0.0181))
   and abs(V.frame_dt(0.034, r, 1) - 2 / 60) < 1e-9
   and abs(V.frame_dt(0.018, r, 2) - 2 / 60) < 1e-9
   and V.frame_dt(0.0173, r, 0) == 0.0173,
   "vsync: a few ms of jitter still moves exactly one refresh; a missed refresh counts two; off = raw")
src = open(os.path.join(HERE, "orc_visuals.py"), encoding="utf-8").read()
ok("vbo.orphan()                  # never wait on last frame's draw\n            vbo.write(data.tobytes())" in src,
   "the line buffer is orphaned before each write (writing the one the GPU still draws from stalled a frame 13.8 -> 31 ms)")
ok("dt = min(frame_dt(raw_dt, refresh, vsync), 0.1)" in src and src.count("vsync = pace(fps)") == 2
   and "if vsync:\n            clock.tick()" in src,
   "the frame loop USES the pacing: snapped dt, vsync re-planned on an fps change, no sleep when synced")

# ---- BACKGROUND + PEEPHOLE -----------------------------------------------------------
kn0 = {V.KNOBS[cc]: V.KNOB_DEFAULT.get(cc, 64) for cc in V.KNOBS}
ok(kn0["bg_level"] == 0 and kn0["bg_pattern"] == 0 and kn0["mask_shape"] == 0
   and V.bucket(kn0["mask_shape"], V.MASK_SHAPES) == 0 and V.bg_params(kn0, 720)["rest"] == 0.0
   and V.bg_params(kn0, 720)["lfo_hz"] == 0.0,
   "defaults keep the old look: the background rests at black, no LFO, no peephole")
# every list size a knob can carry (a pattern page is up to 128): the old
# check re-typed the formula and only tried <= 10 names, so it missed that
# (i*128+64)//n falls into range i-1 past 66 names (30 tiles unpickable)
_bm_bad = [(n, i) for n in range(1, 129) for i in range(n)
           if V.bucket(V.bucket_mid(i, n), [0] * n) != i]
ok(not _bm_bad and all(0 <= V.bucket(v, V.MASK_SHAPES) < len(V.MASK_SHAPES) for v in range(128)),
   f"every page button's value lands in its own range, any list size 1..128 {_bm_bad[:6]}; "
   "every knob value picks something")

bad = []
for name in V.BG_PATTERNS:
    t = V.make_pattern(name, 128)
    shifted = V.make_pattern(name, 128, shift=0.5)
    rolled = np.roll(np.roll(t, -64, 0), -64, 1)
    seam = float((np.abs(shifted - rolled) > 0.05).mean())
    if not (t.shape == (128, 128) and t.dtype == np.float32 and t.min() >= 0 and t.max() <= 1
            and t.max() > 0.5 and (t < 0.02).mean() > 0.1 and seam < 0.01):
        bad.append((name, round(seam, 3), round(float((t < 0.02).mean()), 2)))
ok(not bad, f"every pattern: a 0..1 tile with real black gaps, SEAMLESS (half shift = half roll) {bad}")

pr = V.Presence()
rest = [pr.step(1 / 60, 0.0, 0.0, 1.0, 0.0) for _ in range(300)]
up = [pr.step(1 / 60, 1.0, 0.0, 1.0, 0.0) for _ in range(6)]           # 0.1 s of a hit
down = [pr.step(1 / 60, 0.0, 0.0, 1.0, 0.0) for _ in range(180)]       # then 3 s of nothing
ok(max(rest) == 0.0 and up[-1] > 0.9 and down[-1] < 0.05 and down[40] > down[-1],
   f"background: black at rest, the music brings it in within 0.1 s ({up[-1]:.2f}), "
   f"it fades back to black ({down[-1]:.3f} after 3 s)")
pr = V.Presence()
never = [pr.step(1 / 60, 1.0, 0.0, 0.0, 0.0) for _ in range(120)]
pr = V.Presence()
lfo = [pr.step(1 / 60, 0.0, 0.1, 1.0, 0.5) for _ in range(240)]          # 0.5 Hz, 4 s
ok(max(never) == 0.0 and abs(min(lfo) - 0.1) < 0.02 and abs(max(lfo) - 0.5) < 0.02,
   "REACT 0 = the music never lifts it; the LFO breathes between LEVEL and LEVEL + 0.4")

mp = V.mask_params(kn0)
big = V.mask_params(dict(kn0, mask_size=127, mask_x=0, mask_y=127))
ok(mp["shape"] == 0 and abs(mp["size"] - 0.9) < 0.02 and mp["pos"] == (0.0, 0.0)
   and big["size"] > mp["size"] and big["pos"] == (-1.0, 63 / 64),
   "peephole knobs: off by default, SIZE middle ~ the screen height, X/Y middle = centred")

js = open(os.path.join(HERE, "..", "static", "js", "visuals.js"), encoding="utf-8").read()
# no JS runtime on the Pi: pin the page's bucketMid to the same lo/hi middle
# as V.bucket_mid (checked for every size above)
_jsbm = "".join(js.split("const bucketMid", 1)[-1].split("};", 1)[0].split())
ok("lo=Math.ceil(i*128/n),hi=Math.ceil((i+1)*128/n)-1" in _jsbm
   and "Math.floor((lo+hi)/2)" in _jsbm,
   "the page's bucketMid is the same in-range middle as bucket_mid (gallery tiles all pickable)")


def js_list(name):
    import re
    m = re.search(r"const %s = \[(.*?)\];" % name, js, re.S)
    return tuple(re.findall(r"'([^']*)'", m.group(1))) if m else None


import ast as _ast
_main = next(n for n in _ast.parse(open(os.path.join(HERE, "orc_visuals.py"), encoding="utf-8").read()).body
             if isinstance(n, _ast.FunctionDef) and n.name == "main")
_calls = [(fn.name if isinstance(fn, _ast.FunctionDef) else "main")
          for fn in [_main] + [d for d in _ast.walk(_main) if isinstance(d, _ast.FunctionDef)]
          for c in _ast.walk(fn) if isinstance(c, _ast.Call) and getattr(c.func, "id", "") == "make_pattern"
          and not any(c in _ast.walk(d) for d in _ast.walk(fn) if isinstance(d, _ast.FunctionDef) and d is not fn)]
ok(_calls == ["_build_patterns"],
   f"patterns are built once off the frame loop, never mid-frame (80 ms hitch) {_calls}")

ok(js_list("BG_PATTERNS") == V.BG_PATTERNS and js_list("BG_MODES") == V.BG_MODES
   and js_list("MASK_SHAPES") == V.MASK_SHAPES,
   "the page's pattern / mode / shape buttons are the renderer's lists, in order")


def gpu_check():
    """Compile the REAL show shader and render with it off-screen (EGL) in a
    separate python (this suite stubs moderngl). None = no GPU here."""
    import subprocess
    code = r'''
import os, sys
os.environ["SDL_AUDIODRIVER"] = "dummy"
sys.path.insert(0, %r)
try:
    import numpy as np, moderngl
    ctx = moderngl.create_standalone_context(backend="egl", require=310)
except Exception:
    sys.exit(77)
import orc_visuals as V
W, H = 160, 90
comp = V.Composer(ctx, W, H)                  # the renderer's own final stages
img = ctx.texture((W, H), 4, dtype="f2"); pat = ctx.texture((256, 256), 1, dtype="f1")
pat.repeat_x = pat.repeat_y = True
pat.write((V.make_pattern("checker") * 255).astype("u1").tobytes())
fbo = ctx.framebuffer([ctx.texture((W, H), 4)])
def r(lines, amt, shape, mode=0, ptex=None, tile=(80.0, 80.0), amp=8.0):
    # tile 80 px = 10 px checker squares: the background renders at HALF
    # resolution, so a far finer tile blurs its black gaps to grey (by design)
    img.write(np.full((H, W, 4), lines, "f2").tobytes())
    comp.background(ptex or pat, amt, mode, amp, 0.07, 2.0, tile, 0.05, 0.3, 1.0)
    comp.set_mask(shape, 0.6, 0.002, (0.0, 0.0))
    comp.show_frame(img, 0.0, 1.0, target=fbo)
    return np.frombuffer(fbo.read(components=3), "u1").reshape(H, W, 3).astype(int)
rest = r(0.0, 0.0, 0).max()
full = r(0.0, 1.0, 0)
modes = [r(0.0, 1.0, 0, m).max() for m in range(len(V.BG_MODES))]
shapes = [r(0.5, 0.0, s) for s in range(1, len(V.MASK_SHAPES))]
off = r(0.5, 0.0, 0)
# a NON-square tile: 2 px black|white shown 40 px wide x 10 px tall -> white
# runs of 20 px along a row (a square-only shader would give 5)
tile2 = ctx.texture((2, 1), 1, dtype="f1", alignment=1)
tile2.repeat_x = tile2.repeat_y = True
tile2.filter = (moderngl.NEAREST, moderngl.NEAREST)
tile2.write(bytes([0, 255]))
row = r(0.0, 1.0, 0, 0, ptex=tile2, tile=(40.0, 10.0), amp=0.0)[H // 2].max(axis=1) > 60
edges = np.flatnonzero(np.diff(row.astype(int)))
run = int(np.median(np.diff(edges))) if len(edges) > 2 else -1
print(rest, full.max(), round(float((full.max(axis=2) == 0).mean()), 3), min(modes),
      max(int(s[0, 0].max()) + int(s[-1, -1].max()) + int(s[0, -1].max()) for s in shapes),
      min(int(s[H // 2, W // 2].max()) for s in shapes), int(off[0, 0].max()), run)
'''
    r = subprocess.run([sys.executable, "-B", "-c", code % HERE], capture_output=True, text=True)
    if r.returncode == 77:
        return None
    try:
        return [float(x) for x in r.stdout.strip().splitlines()[-1].split()]
    except Exception:
        return ("error", (r.stderr or r.stdout).strip().splitlines()[-3:])


g = gpu_check()
if g is None:
    print("  [SKIPPED: no GPU/EGL] shader render check")
    ok(True, "shader render check (skipped: no GPU/EGL)")
else:
    ok(isinstance(g, list) and g[0] == 0 and g[1] > 50 and g[2] > 0.2 and g[3] > 50
       and g[4] == 0 and g[5] > 50 and g[6] > 50 and g[7] == 20,
       f"GPU: the real shader compiles; background black at rest, lifted it keeps black gaps, "
       f"every bend mode draws, every peephole shape blacks the corners but not the centre, "
       f"mask off leaves the corners lit, a non-square tile keeps its shape {g}")

# ---- FIT: the scene stays inside the peephole -----------------------------------------
Wf, Hf = 1280, 720
mpc = V.mask_params(dict(kn0, mask_shape=V.bucket(0, V.MASK_SHAPES) + 20, mask_x=96, mask_y=40))
ok(V.Fit().plan(300.0, V.mask_params(kn0), Wf, Hf, 1 / 60) is None
   and V.Fit().plan(300.0, V.mask_params(dict(kn0, mask_shape=20, mask_fit=0)), Wf, Hf, 1 / 60) is None,
   "fit does nothing with no peephole, or with FIT at 0 (the shape just clips)")

bad = []
for cls in (V.Hypercube, V.Tube, V.Tunnel, V.Mystify):
    for shape_v in (20, 40, 60, 78, 97, 116):                  # circle .. star
        mp_ = V.mask_params(dict(kn0, mask_shape=shape_v, mask_x=96, mask_y=40, mask_size=50))
        fitter = V.Fit()
        sc = cls(Wf, Hf)
        sc.ctx = None
        worst = 0.0
        for k in range(90):
            L = V.Lines()
            sc.draw(L, k * 3.0, 0.0, 0.0)
            fp = fitter.plan(L.radius((Wf / 2, Hf / 2)), mp_, Wf, Hf, 1 / 60)
            S = L._stack()
            P = S[:, 0:4].reshape(-1, 2)
            c0, kk, c1 = fp
            Q = (P - c0) * kk + c1
            r_in = (V.MASK_INRADIUS[mp_["shape"]] - mp_["soft"]) * mp_["size"] * Hf / 2
            if cls in (V.Tube, V.Tunnel):
                # these reach past the screen by design (rings fly at you): what
                # was ON SCREEN must land inside; the mask trims the rest
                dist = np.hypot(Wf, Hf) / 2 * kk
            else:
                dist = np.quantile(np.hypot(Q[:, 0] - c1[0], Q[:, 1] - c1[1]), 0.95)
            worst = max(worst, dist / r_in)
        if worst > 1.0:
            bad.append((cls.__name__, V.MASK_SHAPES[mp_["shape"]], round(worst, 3)))
ok(not bad, f"FIT: hypercube/mystify stay inside every shape's inscribed circle; tube/tunnel's "
            f"on-screen view does; moved to an off-centre peephole, frame after frame {bad}")

fpc = V.Fit().plan(300.0, V.mask_params(dict(kn0, mask_shape=20, mask_x=96, mask_y=40)), Wf, Hf, 1 / 60)
ok(np.allclose(fpc[2], (Wf / 2 * 1.5, Hf / 2 * (1 + 24 / 64))) and np.allclose(fpc[0], (Wf / 2, Hf / 2)),
   f"FIT moves the scene to the peephole's centre (x right = right, y up = up on screen) {fpc[2]}")

ft = V.Fit()
mp1 = V.mask_params(dict(kn0, mask_shape=20))
k_small = ft.plan(100.0, mp1, Wf, Hf, 1 / 60)[1]
k_grow = ft.plan(200.0, mp1, Wf, Hf, 1 / 60)[1]                # the shape reaches further
k_after = [ft.plan(100.0, mp1, Wf, Hf, 1 / 60)[1] for _ in range(30)][-1]   # 0.5 s later
k_late = [ft.plan(100.0, mp1, Wf, Hf, 1 / 60)[1] for _ in range(900)][-1]   # 15 s later
ok(abs(k_grow - k_small / 2) < 1e-6 and k_grow < k_after < k_small * 0.8 and k_late / k_small > 0.99,
   "FIT: a bigger reach shrinks it AT ONCE (never pokes out); it eases back over seconds (no pumping)")

L1 = V.Lines()
L1.add([(100.0, 100.0), (300.0, 100.0)], (1, 1, 1), 2.0)
v0 = L1.array()
v1 = L1.array((np.array([640.0, 360.0], "f4"), 0.3, np.array([900.0, 200.0], "f4")))
ok(abs(np.hypot(*(v0[0, :2] - v0[1, :2])) - np.hypot(*(v1[0, :2] - v1[1, :2]))) < 1e-3
   and abs(v1[:, 0].mean() - (640 + (200 - 640) * 0.3 + 260)) < 1.0,
   "FIT moves and shrinks the points but keeps the line THICKNESS (glow built after)")

src = open(os.path.join(HERE, "orc_visuals.py"), encoding="utf-8").read()
ok("uniform vec2 ctr" in V.FADE_FS and "texture(prev, q + ctr)" in V.FADE_FS
   and 'fade["ctr"].value' in src and "data = L.array(camera(fitp, mods, W, H))" in src,
   "the trails pivot on the peephole's centre, and the frame loop applies the fit")


def gpu_inradius():
    """Render every mask on the GPU; nearest black pixel = the real inscribed
    radius. None = no GPU here."""
    import subprocess
    code = r'''
import os, sys
os.environ["SDL_AUDIODRIVER"] = "dummy"
sys.path.insert(0, %r)
try:
    import numpy as np, moderngl
    ctx = moderngl.create_standalone_context(backend="egl", require=310)
except Exception:
    sys.exit(77)
import orc_visuals as V
W, H = 640, 360
comp = V.Composer(ctx, W, H)
img = ctx.texture((W, H), 4, dtype="f2"); img.write(np.full((H, W, 4), 0.5, "f2").tobytes())
fbo = ctx.framebuffer([ctx.texture((W, H), 4)])
yy, xx = np.mgrid[0:H, 0:W]
out = []
redraw = []
for s in range(1, len(V.MASK_SHAPES)):
    redraw.append(comp.set_mask(s, 0.8, 0.002, (0.0, 0.0)))
    redraw.append(not comp.set_mask(s, 0.8, 0.002, (0.0, 0.0)))   # same settings = no redraw
    comp.show_frame(img, 0.0, 1.0, target=fbo)
    a = np.frombuffer(fbo.read(components=1), "u1").reshape(H, W)
    d = np.hypot(xx + 0.5 - W / 2, yy + 0.5 - H / 2) / (H / 2 * 0.8)
    out.append(round(float(d[a == 0].min()), 4))
# trails pivot: a dot AT the peephole centre must stay put under zoom + spin
fade = ctx.program(vertex_shader=V.QUAD_VS, fragment_shader=V.FADE_FS)
fva = ctx.vertex_array(fade, [(ctx.buffer(np.array([-1,-1,1,-1,-1,1,1,1], "f4")), "2f", "p")])
prev = np.zeros((H, W, 4), "f2"); cx, cy = int(W * 0.75), int(H * 0.3)
prev[cy - 3:cy + 4, cx - 3:cx + 4] = 1.0
img.write(prev.tobytes())
for k, v in dict(prev=0, decay=1.0, zoom=1.5, spin=0.4, ctr=(0.75, 0.3)).items():
    fade[k].value = v
fbo.use(); img.use(0); fva.render(moderngl.TRIANGLE_STRIP)
a = np.frombuffer(fbo.read(components=1), "u1").reshape(H, W)
out.append(int(a[cy, cx]))
out.append(int(all(redraw)))
print(*out)
'''
    r = subprocess.run([sys.executable, "-B", "-c", code % HERE], capture_output=True, text=True)
    if r.returncode == 77:
        return None
    try:
        return [float(x) for x in r.stdout.strip().splitlines()[-1].split()]
    except Exception:
        return ("error", (r.stderr or r.stdout).strip().splitlines()[-3:])


gi = gpu_inradius()
if gi is None:
    print("  [SKIPPED: no GPU/EGL] mask inscribed-radius calibration + trails pivot")
    ok(True, "mask calibration (skipped: no GPU/EGL)")
    ok(True, "trails pivot (skipped: no GPU/EGL)")
    ok(True, "mask redraw on change (skipped: no GPU/EGL)")
else:
    ok(isinstance(gi, list) and len(gi) == len(V.MASK_SHAPES) + 1
       and all(0.97 * m <= t <= m for t, m in zip(V.MASK_INRADIUS[1:], gi[:-2])),
       f"GPU: each shape's inscribed radius in the table is <= the rendered one and within 3% "
       f"(table {V.MASK_INRADIUS[1:]}, measured {gi[:-2] if isinstance(gi, list) else gi})")
    ok(isinstance(gi, list) and gi[-2] > 200,
       f"GPU: the trails zoom + spin round the peephole's centre (a dot there stays lit: {gi[-2] if isinstance(gi, list) else gi})")
    ok(isinstance(gi, list) and gi[-1] == 1,
       "the mask is drawn only when its settings change (per pixel every frame cost 8 ms)")

# ---- pattern SETS: built-in + Hero Patterns + Pattern Monster --------------------------
import vis_patterns as VP                            # noqa: E402
sets = VP.pattern_sets(V.BG_PATTERNS)
sizes = [(n, len(names)) for n, _, names in sets]
ok(sets[0][0] == "built-in" and sets[0][2] == list(V.BG_PATTERNS) and V.BG_PATTERNS == VP.BUILTIN
   and ("hero", 87) in sizes and [z for n, z in sizes if n.startswith("monster")] == [128, 128, 74]
   and all(len(names) <= 128 for _, _, names in sets),
   f"sets: built-in 10, hero 87, monster in pages of <= 128 (every tile has its own knob value) {sizes}")
reach = all(sorted({V.bucket(v, names) for v in range(128)}) == list(range(len(names)))
            for _, _, names in sets if names)          # CUSTOM may be empty: nothing to reach
ok(reach and V.KNOB_DEFAULT[50] == 0, "every pattern in every set is reachable from the PATTERN knob; SET starts built-in")
bad = []
import xml.etree.ElementTree as _ET                  # noqa: E402
for folder in ("hero", "monster"):
    for slug in VP._slugs(folder):
        t = open(os.path.join(VP.PAT_DIR, folder, slug + ".svg"), encoding="utf-8").read()
        try:
            _ET.fromstring(t)                         # 16 titles had a bare "&"
        except _ET.ParseError:
            bad.append(slug + " (not XML)")
            continue
        w, h = VP.svg_size(os.path.join(VP.PAT_DIR, folder, slug + ".svg"))
        if not (w > 0 and h > 0 and f'viewBox="0 0 {w:g} {h:g}"' in t and 'fill="#000"/>' in t
                and "<title>" in t and 'fill="#000" d=' not in t and "<script" not in t.lower()):
            bad.append(slug)
cr = open(os.path.join(VP.PAT_DIR, "CREDITS.md"), encoding="utf-8").read()
ok(not bad and "CC BY 4.0" in cr and "Steve Schoger" in cr and "Permission is hereby granted" in cr
   and "Copyright (c) 2020 - 2023 pattern.monster" in cr,
   f"every file tile: valid XML, its own size + viewBox, black background, recoloured, no script; CREDITS carry both licences {bad[:5]}")
ok(VP.tile_scale(192, 192) == 1 and VP.tile_scale(4, 4) == 64 and VP.tile_scale(40, 1) == 6
   and VP.tile_scale(20, 20) == 13 and VP.tile_scale(360, 360) == 1,
   "tiles render at a WHOLE-number scale (one exact period per texture), longer side ~256 px")
websrc = open(os.path.join(HERE, "..", "orc1_web.py"), encoding="utf-8").read()
ok('folder not in ("hero", "monster", "custom")' in websrc and r'fullmatch(r"[a-z0-9-]{1,80}", slug)' in websrc
   and "if slug not in vp._slugs(folder)" in websrc,
   "the tile endpoint only serves the pattern folders' own tiles (hero, monster, your custom; no path tricks)")
ok("pat_cache[key] = ((g[::-1] * 255)" in src and "g = make_pattern(n, PAT_N)" in src
   and 'tile_want.put(key)' in src and "key not in tile_asked" in src,
   "file tiles load in the background thread, flipped right way up, asked for once")


def tile_check():
    """rsvg-convert + a real pygame load of a few tiles (on the Pi)."""
    import shutil
    import subprocess
    if not shutil.which("rsvg-convert"):
        return None
    code = r"""
import os, sys, tempfile
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["HOME"] = tempfile.mkdtemp()          # a throwaway ~/.cache
sys.path.insert(0, %r)
import vis_patterns as VP
out = []
for folder, slug in (("hero", "anchors-away"), ("monster", "waves-1"), ("hero", "4-point-stars")):
    g, (w, h) = VP.load_tile(folder, slug)
    k = VP.tile_scale(w, h)
    out.append(int(g.shape == (round(h * k), round(w * k)) and 0 <= g.min() and g.max() <= 1
                   and (g < 0.05).mean() > 0.2 and g.max() > 0.9))
# anchors-away: the anchor's ring (top) sits above its flukes; loaded top-down,
# the upper half of the anchor column holds the ring
import shutil; shutil.rmtree(os.environ["HOME"], True)
print(*out)
"""
    r = subprocess.run([sys.executable, "-B", "-c", code % HERE], capture_output=True, text=True)
    try:
        return [int(x) for x in r.stdout.strip().splitlines()[-1].split()]
    except Exception:
        return ("error", (r.stderr or r.stdout).strip().splitlines()[-3:])


tc = tile_check()
if tc is None:
    print("  [SKIPPED: no rsvg-convert] tile rasterise + load")
    ok(True, "tile rasterise + load (skipped: no rsvg-convert)")
else:
    ok(tc == [1, 1, 1], f"tiles rasterise at their exact size with real black gaps and load as grey {tc}")

# ---- frame cost: same answers, cheaper (2026-09-30: tube + star ran 46 fps) ----------
tq = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"), shm=os.path.join(TMP, "no_shm"))
rng = np.random.RandomState(5)
sig = (rng.randn(4096) * 0.1 + 0.3 * np.sin(2 * np.pi * 60 * np.arange(4096) / 48000)).astype("f4")
tq.env = 0.05
sl = np.abs(np.fft.rfft(sig * tq.win_l)) / (tq.N_LONG / 4)
old = [round(float(20 * np.log10(sl[ix].max() / 0.05 + 1e-9)), 1) for ix in tq.spec_idx]
new = np.round(20 * np.log10(np.maximum.reduceat(np.append(sl, 0.0), tq._spec_ri)[::2] / 0.05 + 1e-9), 1).tolist()
ok(old == new and len(new) == tq.SPEC_BANDS, "the one-call spectrum = the old per-band loop, band for band")
shq = FakeShm()
tq = V.AudioTap(os.path.join(TMP, "no_such_skipbuf"), shm=shq.path)
ids, keep, t = set(), [], 0.0
for k in range(120):                                   # 2 s at 60 fps, new audio every frame
    shq.push(np.stack([sig[:800]] * 2, 1))
    tq.update(t)
    keep.append(tq.spec)                            # alive = ids stay unique
    ids.add(id(tq.spec))
    t += 1 / 60
ok(40 <= len(ids) <= 52, f"the spectrum updates ~25x/s, not on every tap delivery ({len(ids)} in 2 s)")
Lr = V.Lines()
sc = V.Tube(1280, 720); sc.ctx = None
sc.draw(Lr, 50.0, 0.0, 0.0)
P = Lr._stack()[:, 0:4].reshape(-1, 2)
q95 = float(np.quantile(np.hypot(P[:, 0] - 640, P[:, 1] - 360), 0.95))
ok(abs(Lr.radius((640, 360)) - q95) < 1.0 and Lr._stack() is Lr._stack(),
   "FIT's reach = the 95th percentile (within 1 px), and the line list is built once per frame")

# ---- WEIGHT: the overloaded look as a choice -------------------------------------------
Lw = V.Lines()
Lw.add([(100.0, 100.0), (300.0, 100.0)], (1, 1, 1), 2.0)
wd = []
for v in (64, 127, 0):
    V.WEIGHT[0] = V.line_weight(v)
    va = Lw.array()
    wd.append(round(float(np.hypot(*(va[0, :2] - va[1, :2]))), 2))
V.WEIGHT[0] = 1.0
ok(V.KNOB_DEFAULT.get(53, 64) == 64 and abs(wd[1] - wd[0] * V.line_weight(127)) < 0.05
   and V.line_weight(127) > 2.4 and abs(wd[2] - wd[0] * 0.4) < 0.05,
   f"WEIGHT: the middle = each scene's own width, top ~x2.5 (the overloaded glow), 0 = x0.4 {wd}")
ok('WEIGHT[0] = line_weight(music.knobs[53])' in src, "the frame loop applies the WEIGHT knob")

# ---- the audio GATE: quiet is silence, not something to boost ------------------------
def noise_at(db):
    rs = np.random.RandomState(9)
    return lambda t: (10 ** (db / 20.0)) * 1.4 * rs.randn(len(t))


gl = {}
for db in (-72, -80):
    root = fresh()
    tq = V.AudioTap(root)
    b_, lv_ = play(root, "t2", 2, noise_at(db), 8.0, tq)
    gl[db] = (len(b_), round(max(lv_[5:] or [0]), 2))
root = fresh()
tq = V.AudioTap(root)
tq.gate = V.audio_gate(0)                            # gate off = the old behaviour
_, lv_off = play(root, "t2", 2, noise_at(-72), 8.0, tq)
root = fresh()
tq = V.AudioTap(root)
b_q, lv_q2 = play(root, "t2", 2, kicks(0.004), 8.0, tq)   # quiet MUSIC, -60 dBFS
ok(gl[-72] == (0, 0.0) and gl[-80] == (0, 0.0) and max(lv_off[5:]) > 0.3
   and len(b_q) >= 12 and max(lv_q2) > 0.8,
   f"GATE: hiss at -72 / -80 dBFS = silence (no beats, level 0: {gl}) though the auto gain alone "
   f"lifted it to {max(lv_off[5:]):.2f}; quiet music at -60 still beats + reaches full level")
_sh_q, _sh_m = FakeShm(), FakeShm()
_tq_q = V.AudioTap(os.path.join(TMP, "no_skipbuf_q"), shm=_sh_q.path)
_tq_m = V.AudioTap(os.path.join(TMP, "no_skipbuf_m"), shm=_sh_m.path)
play_tap(_sh_q, noise_at(-110), 2.0, _tq_q)          # an interface's own hiss, silent input
play_tap(_sh_m, lambda t: 0.0014 * np.sin(2 * np.pi * 220 * t), 2.0, _tq_m)   # quiet music, -60 dBFS rms
_lit_m = sum(1 for d in _tq_m.spec if d is not None and d > -72)
ok(all(d is None for d in _tq_q.spec) and _lit_m >= 3,
   f"GATE on the display too: -110 dBFS hiss (a silent L-12) draws no bars; quiet music draws "
   f"{_lit_m} (2026-10-12: the hiss drew bars up to a quarter high)")
ok(V.audio_gate(0) == 0.0 and abs(20 * np.log10(V.audio_gate(56)) + 68) < 0.5
   and V.KNOB_DEFAULT[54] == 56 and 'audio.gate = audio_gate(music.knobs[54])' in src,
   "GATE knob: 0 = off, default = -68 dBFS, and the frame loop applies it")

pr2 = V.Presence()
for _ in range(3):
    pr2.step(1 / 60, 1.0, 0.0, 1.0, 0.0)
ok(pr2.env > 0.6 and pr2.sway < 0.45 * pr2.env
   and "bp[\"amp\"] * (1.0 + 1.5 * presence.sway)" in src and "bg_clock % 3600.0" in src,
   f"a beat punches the brightness at once but swells the sway slowly (no yank): "
   f"after 50 ms env {pr2.env:.2f}, sway {pr2.sway:.2f}; the sway runs on the steady frame clock")

# ---- beat envelope + its knobs ------------------------------------------------------


def rise(attack, frames=3, dt=1 / 30):
    pu = V.AudioPulse(attack=attack, release=0.35)
    pu.hit(1.0)
    return [pu.step(dt) for _ in range(frames)]


ok(rise(V.audio_attack(0))[0] > 0.95, "ATTACK all the way down = the beat lands in the first frame")
fast, slow = rise(V.audio_attack(V.KNOB_DEFAULT[29])), rise(V.audio_attack(127))
ok(fast[0] > 0.5 and slow[0] < fast[0] * 0.6 and abs(V.audio_attack(V.KNOB_DEFAULT[29]) - 0.02) < 0.002,
   "ATTACK default is quick (~20 ms, as before: first frame > half), top is a slow swell")


def after(release, secs=0.5, dt=1 / 30):
    pu = V.AudioPulse(attack=0.0, release=release)
    pu.hit(1.0)
    for _ in range(int(secs / dt)):
        v = pu.step(dt)
    return v


ok(after(V.audio_release(0)) < 0.01 < after(V.audio_release(64)) < after(V.audio_release(127)),
   "RELEASE: longer knob = the beat lingers longer")
ok(all(V.audio_onset(a) > V.audio_onset(b) for a, b in ((0, 64), (64, 127)))
   and all(V.audio_amount(a) < V.audio_amount(b) for a, b in ((1, 32), (32, 64), (64, 127))),
   "SENS lowers the onset bar as it rises; AUDIO amount rises along its curve")
ok(V.audio_amount(1) < 0.01 and V.audio_amount(0) == 0.0, "AUDIO's low end is genuinely subtle")

# ---- scenes --------------------------------------------------------------------


def nseg(L):
    return len(L.seg) + sum(len(b) for b in L.blocks)


class FakeMusic:
    def __init__(self, notes=()):
        self.held = {(5, n): n for n in notes}


def fake_tap(frames=None, spec=None):
    t = types.SimpleNamespace(dir="t2" if frames is not None or spec is not None else None,
                              live=frames is not None or spec is not None,
                              env=0.1, frames=frames,
                              frames_t=time.monotonic(), spec=spec)
    return t


# ---- LOGOS / line drawings (vis_logos.py) --------------------------------------------
import vis_logos as VL                               # noqa: E402
USER_LOGOS = os.path.join(TMP, "user_logos")         # never the live ~/orc1_logos
VL.user_dir = lambda: USER_LOGOS
sq_abs = VL.parse_path("M0 0 H10 V10 Z")
sq_rel = VL.parse_path("m0 0 l10 0 l0 10 z")
cub = VL.parse_path("M0 0 C0 10 10 10 10 0")[0]
arc = VL.parse_path("M0 0 A5 5 0 0 1 10 0")[0]
arcc = VL.parse_path("M0 0a5 5 0 0110 0")[0]           # flags written with no spaces
ok(sq_abs == [[(0, 0), (10, 0), (10, 10), (0, 0)]] and sq_abs == sq_rel
   and cub[-1] == (10.0, 0.0) and abs(max(y for _, y in cub) - 7.5) < 0.2
   and abs(min(y for _, y in arc) + 5) < 0.05 and abs(arc[-1][0] - 10) < 1e-9
   and np.allclose(arc, arcc),
   "SVG paths: absolute = relative, curves pass where they should, arcs (even squashed flags)")
g = VL.parse_svg('<svg xmlns="http://www.w3.org/2000/svg"><g transform="translate(10,0) scale(2)">'
                 '<rect x="1" y="1" width="1" height="1"/></g>'
                 '<circle cx="0" cy="0" r="3" transform="rotate(90)"/><text>hi</text>'
                 '<defs><path d="M0 0 L99 99"/></defs><script>alert(1)</script></svg>')
ok(len(g) == 2 and g[0][0] == (12.0, 2.0) and g[0][2] == (14.0, 4.0)
   and abs(max(math.hypot(x, y) for x, y in g[1]) - 3) < 1e-6,
   "SVG shapes + nested transforms; text, <defs> and <script> are ignored")
xxe = ('<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
       '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 L1 1"/><title>&x;</title></svg>')
try:
    leaked = "root:" in repr(VL.parse_svg(xxe))
except Exception:
    leaked = False
ok(not leaked, "a hostile SVG can't read files off the Pi (external entities are never fetched)")
shipped = [(n, VL.load(pth)) for n, pth in VL.logo_list() if os.path.dirname(pth) == VL.SHIPPED]
ok(sorted(n for n, _ in shipped) == ["bolt", "orc-1", "spiral-eye"] and all(
       P and abs(max(math.hypot(x, y) for q in P for x, y in q) - 1) < 1e-9
       and sum(len(q) for q in P) <= VL.MAX_POINTS for _, P in shipped),
   "the three shipped drawings load, centred and scaled to radius 1")
big = [[(math.cos(t) * (1 + 0.001 * math.sin(40 * t)), math.sin(t)) for t in np.linspace(0, 6.28, 20000)]]
nb = VL.normalize(big, max_points=500)
bolt = dict(shipped)["bolt"]
ob = VL.outline(bolt)
bolt_corners = {(round(x, 6), round(y, 6)) for x, y in bolt[0]}
eye = VL.outline(dict(shipped)["orc-1"])                  # the O (an ellipse) is the longest line
curvy = VL.outline(dict(shipped)["spiral-eye"])            # its eye outline = two CURVES
cub_pts = VL.parse_path("M0 0 C0 10 10 10 10 0")[0]
ang = np.diff(np.unwrap(np.arctan2([y for _, y in eye], [x for x, _ in eye])))
ok(sum(len(q) for q in nb) <= 500 and len(ob) == 7
   and {(round(x, 6), round(y, 6)) for x, y in ob} == bolt_corners
   and 40 <= len(eye) <= 160 and np.abs(ang).max() < 0.2
   and len(VL.outline(nb, max_points=100)) <= 100
   and len(cub_pts) >= 20 and len(curvy) >= 40,
   f"outlines keep the drawing's own corners exactly (bolt: 7) and curves smooth (O: {len(eye)} pts, "
   f"max turn {np.abs(ang).max():.2f} rad); the point budget holds")
websrc = open(os.path.join(HERE, "..", "orc1_web.py"), encoding="utf-8").read()
ok("polys = vl.parse_svg(text)" in websrc and "if len(data) > vl.MAX_BYTES:" in websrc
   and "only your uploaded logos can be deleted" in websrc and "send_file(paths" not in websrc
   and '"polys": [[[round(x, 4)' in websrc,
   "uploads must parse + fit 2 MB; only your own logos delete; previews send parsed lines, never the SVG")

bank = V.LogoBank()
bank.refresh(0.0)
for n, P in shipped:
    bank.cache[n] = [np.asarray(q, "f4") for q in P]
    bank.sigs[n] = bank._sig(bank.paths[n])          # = what the loader records
V.LOGOS = bank
V.LOGOS_KNOB[0] = 0                                  # "chord" -> the logo scene uses the first
lg = V.Logo(1280, 720)
lg.ctx = None
Lf = V.Lines()
lg.draw(Lf, 500.0, 0.0, 0.5)                          # the first = bolt (7 strokes)
V.LOGOS_KNOB[0] = V.bucket_mid(1 + bank.names.index("orc-1"), len(bank.names) + 1)
Ll = V.Lines()
lg.draw(Ll, 500.0, 0.0, 0.5)
i_bolt = 1 + bank.names.index("bolt")
V.LOGOS_KNOB[0] = V.bucket_mid(i_bolt, len(bank.names) + 1)
s4 = V.Shape4D(1280, 720)
s4.ctx = None
L4 = V.Lines()
s4.draw(L4, 100.0, 0.0, 0.0)
V.LOGOS_KNOB[0] = 0
L4c = V.Lines()
s4.draw(L4c, 100.0, 0.0, 0.0)                         # same frame, chord shape
ok(nseg(Lf) >= 7 and nseg(Ll) > 50 and np.isfinite(Ll.array()).all() and nseg(L4) >= 4 * len(ob)
   and not (L4._stack().shape == L4c._stack().shape
            and np.allclose(L4._stack()[:, :4], L4c._stack()[:, :4]))
   and len(L4._stack()) == 4 * len(ob)                   # outlines only, no joins
   and V.bucket(V.bucket_mid(i_bolt, len(bank.names) + 1), ["chord"] + bank.names) == i_bolt,
   "the logo scene draws the drawing + its trace; shape4d pushes the picked logo's outline into 4D, drawing ONLY its four outlines (no joins)")

drawn = []
for name, cls in V.SCENES.items():
    for label, ctx in (("no ctx", None),
                       ("no audio", V.SceneCtx(fake_tap(), FakeMusic())),
                       ("audio + chord", V.SceneCtx(
                           fake_tap(np.random.RandomState(1).randn(4096, 2).astype("f4") * .05,
                                    list(np.linspace(-40, -10, 48))),
                           FakeMusic((48, 52, 55))))):
        try:
            sc = cls(1280, 720)
            sc.ctx = ctx
            L = V.Lines()
            for f in (0.0, 50.0, 5000.0):
                sc.draw(L, f, 200.0, 0.8)
            arr = L.array()
            drawn.append(nseg(L) > 0 and arr is not None and np.isfinite(arr).all())
        except Exception as e:
            print("  scene", name, label, "raised", repr(e))
            drawn.append(False)
ok(all(drawn) and len(V.SCENES) == 16,
   "all 16 scenes draw finite geometry: without context, without audio, and with audio + a chord")

# ---- SHAPE MACHINES (vis_shapes.py) --------------------------------------------------
import vis_shapes as VS                              # noqa: E402
KNOWN = {"5-cell": (5, 10, 4), "tesseract": (16, 32, 4), "16-cell": (8, 24, 6),
         "24-cell": (24, 96, 8), "600-cell": (120, 720, 12),
         "duoprism-5x5": (25, 50, 4), "duoprism-3x8": (24, 48, 4)}
got = {}
for name in VS.POLYTOPES:
    Vp, Ep = VS.polytope(name)
    deg = set(np.bincount(Ep.ravel(), minlength=len(Vp)).tolist())
    got[name] = (len(Vp), len({tuple(e) for e in Ep.tolist()}), deg.pop() if len(deg) == 1 else deg)
ok(got == {n: KNOWN[n] for n in VS.POLYTOPES} and all(
       abs(np.linalg.norm(VS.polytope(n)[0], axis=1).max() - 1) < 1e-9 for n in VS.POLYTOPES),
   f"polytopes: vertex / edge counts + every corner's degree match the known shapes, unit size {got}")
sq = VS.superformula(4, 100, 100, 100, k=4)
Vx, Ex = VS.extrude_4d(sq)
ok(len(Vx) == 16 and len(Ex) == 32 and set(np.bincount(Ex.ravel()).tolist()) == {4},
   "any shape into 4D: a square comes out as a tesseract (16 corners, 32 edges, 4 at each)")
Vo, Eo = VS.extrude_4d(VS.superformula(3, 1, 1, 1, k=24), depth=0.35)
outl = Eo[:4 * 24]
ok(all((b_ - a_) in (1, -23) for a_, b_ in outl.tolist()) and all(abs(b_ - a_) >= 24 for a_, b_ in Eo[96:].tolist())
   and np.allclose(np.abs(Vo[:, 2:]), 0.35),
   "extrude: the four outlines come first, then the joins between copies (drawn thin); depth sets the gap")
Rv = VS.rotate4(VS.polytope("24-cell")[0], (0.3, 0.1, 0.7, 0.2, 0.5, 0.9))
ok(np.allclose(np.linalg.norm(Rv, axis=1), 1.0), "a 4D rotation keeps every point's distance (it's rigid)")
sec3 = VS.superformula(3, 0.5, 4, 4, k=12)            # 3 lobes: a twist shows
seams = []
for pq in VS.TORUS_KNOTS:
    tb = VS.sweep(VS.torus_knot(*pq, n=120), sec3, 0.15)
    inner = max(np.linalg.norm(tb[i + 1] - tb[i], axis=1).max() for i in range(119))
    seams.append(round(float(np.linalg.norm(tb[0] - tb[-1], axis=1).max() / inner), 2))
ok(tb.shape == (120, 12, 3) and max(seams) <= 1.05,
   f"the swept tube closes on itself point for point, no twist jump at the seam, every knot {seams}")
r4 = np.hypot(*VS.chord_shape([60, 64, 67, 71], 0.0, k=240).T)
r3 = np.hypot(*VS.chord_shape([60, 64, 67], 0.0, k=240).T)
peaks = lambda r: int(((r > np.roll(r, 1)) & (r > np.roll(r, -1)) & (r > 0.9)).sum())   # noqa: E731
ok(abs(r3.max() - 1) < 1e-9 and peaks(r3) == 3 and peaks(r4) == 6,
   f"chord shapes: a triad = 3 lobes, a 7th chord = 6 (more notes, more lobes) ({peaks(r3)}, {peaks(r4)})")
fb = VS.hopf_fibers(per=6, n=32)
ok(len(fb) == 24 and all(np.isfinite(c).all() and c.shape == (32, 3) for c in fb),
   "Hopf: 4 latitudes x 6 = 24 linked circles, all finite")
web = open(os.path.join(HERE, "..", "orc1_web.py"), encoding="utf-8").read()
import re as _re                                     # noqa: E402
wl = _re.findall(r'"([a-z0-9]+)"', _re.search(r"_VIS_SCENES = \[(.*?)\]", web, _re.S).group(1))
sh = open(os.path.join(HERE, "visuals.sh"), encoding="utf-8").read()
shl = _re.search(r"SCENES='\^\((.*?)\)\$'", sh).group(1).split("|")
ok(wl == list(V.SCENES) and shl == list(V.SCENES),
   "the web page, visuals.sh and the renderer list the same scenes, in order")

# XY scope: identical L and R (mono) = a VERTICAL line (x = L - R = 0)
sc = V.Scope(1280, 720)
mono = np.sin(np.linspace(0, 40, 4096)).astype("f4")[:, None].repeat(2, 1) * .08
sc.ctx = V.SceneCtx(fake_tap(mono), FakeMusic())
L = V.Lines()
sc.draw(L, 0.0, 0.0, 0.0)
xs = np.concatenate([L.blocks[0][:, 0], L.blocks[0][:, 2]])
ys = np.concatenate([L.blocks[0][:, 1], L.blocks[0][:, 3]])
ok(np.ptp(xs) < 1.0 and np.ptp(ys) > 100, "XY scope: mono audio draws a vertical line")
side = np.stack([mono[:, 0], -mono[:, 0]], 1)
sc.ctx = V.SceneCtx(fake_tap(side), FakeMusic())
L = V.Lines()
sc.draw(L, 0.0, 0.0, 0.0)
ok(np.ptp(np.concatenate([L.blocks[0][:, 1], L.blocks[0][:, 3]])) < 1.0,
   "XY scope: out-of-phase L/R draws a horizontal line")

# Ridgeline hidden lines: a tall ridge in FRONT hides a lower one behind it
rl = V.Ridgeline(1280, 720)
rl.ctx = V.SceneCtx(fake_tap(), FakeMusic())
rl.ctx.audio.dir, rl.ctx.audio.live = "t2", True
rl.rows = [np.full(48, 0.95), np.full(48, 0.05)]   # [front, back]
rl._spec_id = id(rl.ctx.audio.spec)
L = V.Lines()
rl.draw(L, 0.0, 0.0, 0.0)
ys = np.concatenate([b[:, 1] for b in L.blocks] + [b[:, 3] for b in L.blocks])
front_y = 720 * .92 - 0.95 * 720 * .32
ok(len(L.blocks) >= 1 and np.all(np.abs(ys - front_y) < 40),
   "ridgeline: a low ridge behind a tall one is hidden (only the front one draws)")
rl.rows = [np.full(48, 0.05), np.full(48, 0.95)]   # a tall ridge BEHIND a low one
L = V.Lines()
rl.draw(L, 0.0, 0.0, 0.0)
ys = np.concatenate([b[:, 1] for b in L.blocks] + [b[:, 3] for b in L.blocks])
z1 = 1 / V.Ridgeline.ROWS
back_y = (720 * .92 - z1 * 720 * .6) - 0.95 * 720 * .32 * (1 - z1 * .5)
front_low = 720 * .92 - 0.05 * 720 * .32
ok(np.any(np.abs(ys - back_y) < 3) and np.any(np.abs(ys - front_low) < 3),
   "ridgeline: a taller ridge behind still shows above the front one")

# Chladni: marching squares puts the zero line where it belongs
u, v = np.meshgrid(np.linspace(0, 1, 41), np.linspace(0, 1, 21))
S = V.Chladni.contours(u - 0.5, 1.0, 1.0)              # zero line at u = 0.5 -> x = 20
ok(len(S) >= 20 and np.all(np.abs(S[:, [0, 2]] - 20) < 1e-3),
   "chladni: marching squares finds a straight zero line exactly")
r2 = (u - .5) ** 2 + ((v - .5) * 20 / 40) ** 2
S = V.Chladni.contours(np.sqrt(r2) - 0.3, 1.0, 1.0)
rad = np.hypot(S[:, 0] / 40 - .5, (S[:, 1] / 20 - .5) * .5)
ok(len(S) > 30 and np.all(np.abs(rad - 0.3) < 0.02), "chladni: a circle's contour sits on the circle")
ch = V.Chladni(1280, 720)
ch.ctx = V.SceneCtx(fake_tap(), FakeMusic((48, 52, 55)))
L1 = V.Lines(); ch.draw(L1, 100.0, 0.0, 0.0)
ch.ctx = V.SceneCtx(fake_tap(), FakeMusic((48, 51, 58)))
L2 = V.Lines(); ch.draw(L2, 100.0, 0.0, 0.0)
ok(not np.array_equal(L1.blocks[0], L2.blocks[0]), "chladni: a different chord = a different plate")

# Rose + the chord memory
ro = V.Rose(1280, 720)
ro.ctx = V.SceneCtx(fake_tap(), FakeMusic((48, 52, 55)))
La = V.Lines(); ro.draw(La, 0.0, 0.0, 0.0)
ro2 = V.Rose(1280, 720)
ro2.ctx = V.SceneCtx(fake_tap(), FakeMusic((48, 49, 54)))
Lb = V.Lines(); ro2.draw(Lb, 0.0, 0.0, 0.0)
ok(not np.allclose(np.array(La.seg)[:, :4], np.array(Lb.seg)[:, :4]),
   "rose: a different chord draws a different figure")
cx = V.SceneCtx(fake_tap(), FakeMusic((50, 53, 57)))
cx.notes()
cx.music.held.clear()
ok(cx.notes() == [50, 53, 57], "scenes remember the last chord after you let go")

# Lorenz stays bounded (it's chaotic, not explosive) at any beat level
lo = V.Lorenz(1280, 720)
lo.ctx = None
for k in range(600):
    lo.draw(V.Lines(), float(k), 0.0, (k % 15) / 15.0)
ok(all(map(np.isfinite, lo.q)) and abs(lo.q[2]) < 80, "lorenz stays on the attractor for 600 frames")

# ---- review fixes (2026-10-01) -------------------------------------------------------
# 1. a dense SVG can't expand into millions of points: the parser counts as it goes
dense = "M0 0 " + "q1 1 1 1 " * 10000                 # ~90 KB -> 240k points uncapped
t0 = time.monotonic()
caught = []
for label, svg in (("path", '<svg><path d="%s"/></svg>' % dense),
                   ("circles", "<svg>" + '<circle r="1"/>' * 5000 + "</svg>"),
                   ("z flood", '<svg><path d="M0 0%s"/></svg>' % ("z" * 150000))):
    try:
        VL.parse_svg(svg)
        caught.append(label + ": parsed")
    except VL.TooDetailed:
        caught.append("capped")
under = VL.parse_svg('<svg><path d="M0 0 %s"/></svg>' % ("q1 1 1 1 " * 1000))   # 24k: fine
ok(caught == ["capped"] * 3 and issubclass(VL.TooDetailed, ValueError)
   and sum(map(len, under)) == 24001 and time.monotonic() - t0 < 20,
   f"a dense path / a flood of circles / of z's is refused as too detailed (the upload 400s) {caught}")
up = websrc[websrc.index("def api_visuals_logos"):websrc.index("def api_visuals_logo(")]
ok("request.content_length is None" in up and "request.content_length > vl.MAX_BYTES + 64 * 1024" in up
   and up.index("request.content_length >") < up.index("request.files.get("),
   "the upload is judged by its Content-Length BEFORE the form is parsed (nothing big spooled into /tmp = RAM)")

# 3. shape4d works out a logo's outline ONCE, not every frame
calls, real_outline = [], VL.outline
VL.outline = lambda P, *a, **k: calls.append(1) or real_outline(P, *a, **k)
try:
    s4 = V.Shape4D(1280, 720)
    s4.ctx = None
    V.LOGOS = bank
    V.LOGOS_KNOB[0] = V.bucket_mid(i_bolt, len(bank.names) + 1)
    for fr in range(5):
        s4.draw(V.Lines(), float(fr), 0.0, 0.0)
    n_same = len(calls)
    bank.cache["bolt"] = [q.copy() for q in bank.cache["bolt"]]     # a reload = a new list
    s4.draw(V.Lines(), 6.0, 0.0, 0.0)
finally:
    VL.outline = real_outline
    V.LOGOS_KNOB[0] = 0
ok(n_same == 1 and len(calls) == 2, f"shape4d: one outline per logo (5 frames -> {n_same}), a reloaded logo -> a new one")


# 4. tap lap check: the C writer stores a period BEFORE publishing wpos, so a copy
#    whose start is more than cap - period behind the re-read wpos may be torn
class LapHdr:
    """TapShm's header struct, but the writer moves on DURING the copy."""

    def __init__(self, ahead):
        self.size, self.ahead, self.n = V.TapShm.HDR.size, ahead, 0

    def unpack_from(self, mm, off):
        v = list(V.TapShm.HDR.unpack_from(mm, off))
        self.n += 1
        if self.n == 2:                                   # the re-read after the copy
            v[5] += self.ahead
        return tuple(v)


laps = []
for ahead in (8192 - 1024 - 4096, 8192 - 1024 - 4096 + 1):
    s5 = FakeShm(cap=8192, period=1024)
    s5.push(np.zeros((12288, 2), "f4"))
    r5 = V.TapShm(s5.path)
    r5.read(0.0, 16)                                       # map it
    r5.wpos, r5.HDR = None, LapHdr(ahead)
    laps.append(r5.read(0.0, 4096)[0])
ok(laps == ["new", "same"],
   f"tap: a copy is kept while within cap - one period of the writer, dropped one frame past it {laps}")

# 5. LogoBank follows the files: re-upload, fix, delete; names are unique
os.makedirs(USER_LOGOS, exist_ok=True)


def put_logo(name, body):
    with open(os.path.join(USER_LOGOS, name + ".svg"), "w") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg">%s</svg>' % body)


def bank_get(b, name, t):
    """Pick `name` and wait for the loader thread (<= 5 s)."""
    knob = V.bucket_mid(1 + b.names.index(name), len(b.names) + 1)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        got = b.pick(knob, t)
        if got is not None or name in b.failed:
            return got
        time.sleep(0.01)
    return None


put_logo("mine", '<path d="M0 0 L1 0"/>')
put_logo("bolt", '<path d="M0 0 L1 1 L2 0"/>')            # same name as a shipped one
put_logo("broken", "<path d=")
b2 = V.LogoBank()
b2.refresh(0.0)
names = [n for n, _ in VL.logo_list()]
uniq = (names.count("bolt") == 1 and dict(VL.logo_list())["bolt"].startswith(USER_LOGOS)
        and b2.names.count("bolt") == 1
        and names.index("bolt") == sorted(n for n, _ in shipped).index("bolt"))
first = bank_get(b2, "mine", 0.0)
bad1 = bank_get(b2, "broken", 0.0)
time.sleep(0.02)
put_logo("mine", '<path d="M0 0 L1 0 L1 1 L0 1"/>')       # re-uploaded, same name
put_logo("broken", '<path d="M0 0 L5 5"/>')               # fixed
b2.refresh(10.0)
second = bank_get(b2, "mine", 10.0)
fixed = bank_get(b2, "broken", 10.0)
os.remove(os.path.join(USER_LOGOS, "mine.svg"))
b2.refresh(20.0)
gone = "mine" not in b2.names and "mine" not in b2.cache
ok(uniq and first is not None and len(first[0]) == 2 and bad1 is None
   and second is not None and len(second[0]) == 4 and fixed is not None and gone,
   "logos: a re-upload / a fixed file is re-read, a deleted one is dropped, and an upload named like "
   "a shipped logo replaces it (listed once, in its place)")
shutil.rmtree(USER_LOGOS, True)

# 6. two processes rasterising the same tile never share (or publish) a torn temp file
PT = os.path.join(TMP, "pat")
os.makedirs(os.path.join(PT, "hero"), exist_ok=True)
with open(os.path.join(PT, "hero", "t.svg"), "w") as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
real = (VP.PAT_DIR, VP.cache_dir, VP.subprocess.run)
outs, fail = [], [False]


def fake_rsvg(cmd, **kw):
    o = cmd[cmd.index("-o") + 1]
    outs.append(o)
    with open(o, "wb") as f:
        f.write(b"half")
    time.sleep(0.05)                                      # both threads inside at once
    if fail[0]:
        raise VP.subprocess.CalledProcessError(1, cmd)
    with open(o, "ab") as f:
        f.write(b" + the rest")


import threading                                     # noqa: E402
VP.PAT_DIR, VP.cache_dir, VP.subprocess.run = PT, (lambda: os.path.join(TMP, "patcache")), fake_rsvg
try:
    th = [threading.Thread(target=VP.tile_png, args=("hero", "t")) for _ in range(2)]
    [t.start() for t in th]
    [t.join() for t in th]
    png = os.path.join(TMP, "patcache", "hero", "t.png")
    whole = open(png, "rb").read() == b"half + the rest"
    os.remove(png)
    fail[0] = True
    try:
        VP.tile_png("hero", "t")
    except VP.subprocess.CalledProcessError:
        pass
    left = sorted(os.listdir(os.path.join(TMP, "patcache", "hero")))
finally:
    VP.PAT_DIR, VP.cache_dir, VP.subprocess.run = real
ok(len(set(outs[:2])) == 2 and all(str(os.getpid()) in o for o in outs) and whole and left == [],
   f"tiles: each rasteriser writes its own temp file, only a whole PNG is published, a failed one leaves nothing {left}")

# 7. the fps log in /tmp (RAM) stays small; a failing BUILD of the tap isn't retried forever
logp = os.path.join(TMP, "fps.log")
for k in range(500):
    V.append_bounded(logp, "line %d %s" % (k, "x" * 40), max_bytes=4096, keep=20)
tail = open(logp).read().splitlines()
unit = open(os.path.join(HERE, "..", "..", "systemd", "orc1-vistap.service"), encoding="utf-8").read()
ok(os.path.getsize(logp) <= 4096 + 60 and tail[-1].startswith("line 499 ") and len(tail) <= 80
   and "append_bounded(FPS_LOG" in src and 'open("/tmp/orc1_visuals_fps.log", "a")' not in src
   and "|| exit 78" in unit and "RestartPreventExitStatus=78" in unit and 'exec "$$b"' in unit
   and "Restart=on-failure" in unit,
   "the fps log is bounded; a failed tap build exits 78 = no retry loop (the tap dying with JACK still restarts)")

# 8. the LISTEN pickers offer AUDIO output ports only (never a MIDI port)
fn = websrc[websrc.index("def _jack_sources():"):websrc.index("def _skipback_current(")]
LSP = ("system:capture_1\n\tproperties: output,physical,terminal,\n\t32 bit float mono audio\n"
       "system:playback_1\n\tproperties: input,physical,terminal,\n\t32 bit float mono audio\n"
       "a2j:Fire (capture): Fire MIDI 1\n\tproperties: output,physical,terminal,\n\t8 bit raw midi\n"
       "ORC1-Drum:out_L\n\tproperties: output,\n\t32 bit float mono audio\n")
g8 = {"_jack": lambda a: LSP if "-t" in a else ""}
exec(fn, g8)
ok(g8["_jack_sources"]() == ["system:capture_1", "ORC1-Drum:out_L"],
   "JACK sources = audio OUTPUT ports only (a MIDI port is no source for the tap or a ring)")

# ---- 2026-10-12: beat envelope fix, camera + mods, LISTEN honesty, the new page ------


class _OldPulse:                       # the envelope as it was before 2026-10-12
    def __init__(self, attack, release):
        self.attack, self.release, self.tgt, self.v = attack, release, 0.0, 0.0

    def hit(self, s):
        self.tgt = max(self.tgt, s)

    def step(self, dt):
        if self.attack <= 1e-3:
            self.v = self.tgt
        else:
            self.v += (self.tgt - self.v) * (1.0 - math.exp(-dt / self.attack))
        self.tgt *= math.exp(-dt / max(self.release, 1e-3))
        return self.v


import random as _rnd                                # noqa: E402
_r = _rnd.Random(5)
_diff = 0
for _ in range(60):
    _rel, _dt = V.audio_release(_r.randrange(128)), _r.choice([1 / 60, 1 / 30, 1 / 45])
    _o, _n = _OldPulse(0.0, _rel), V.AudioPulse(attack=V.audio_attack(0), release=_rel)
    for _f in range(300):
        for _h in range(_r.choice([0, 0, 0, 1, 2])):
            _s = _r.random()
            _o.hit(_s)
            _n.hit(_s)
        _diff += _o.step(_dt) != _n.step(_dt)
ok(_diff == 0, f"ATTACK 0 (owner's setting) = the old envelope frame for frame ({_diff} frames differ)")


def _peak(a, r, dt=1 / 60):
    pu = V.AudioPulse(attack=V.audio_attack(a), release=V.audio_release(r))
    pu.hit(1.0)
    return max(pu.step(dt) for _ in range(int(1.0 / dt)))


_low = min(_peak(a, r) for a in range(0, 128, 7) for r in range(0, 128, 9))
ok(_low > 0.999 and _OldPulse(V.audio_attack(127), V.audio_release(0)) is not None,
   f"every ATTACK x RELEASE: a full hit reaches full height (lowest {_low:.3f}; the old envelope "
   "peaked at 29% at ATTACK 127 + short RELEASE)")
_att = [V.audio_attack(v) for v in range(128)]
ok(_att[0] == 0.0 and abs(_att[1] - 0.016) < 1e-9 and abs(_att[127] - 0.25) < 1e-9
   and all(b > a for a, b in zip(_att[1:], _att[2:])),
   "ATTACK: 0 = instant, 1 = one 60 fps frame, every notch longer, 127 = 250 ms")

_kd = {cc: V.KNOB_DEFAULT.get(cc, 64) for cc in V.KNOBS}
_rest = [V.mod_params(_kd, h, ph, 720) for h in (0.0, 0.7, 1.2) for ph in (0.0, 0.3, 0.77)]
_fit = (np.array([640.0, 360.0], "f4"), 0.5, np.array([700.0, 300.0], "f4"))
ok(all(m["size"] == 1.0 and m["angle"] == 0.0 and m["sway"] == (0.0, 0.0) and m["glow"] == 1.0
       and m["trail"] == 0.0 for m in _rest)
   and V.camera(None, _rest[4], 1280, 720) is None and V.camera(_fit, _rest[4], 1280, 720) is _fit,
   "camera + mods at their defaults: nothing moves, whatever the beat or LFO (the picture is unchanged)")


def _moved(cc, hit, ph):
    k = dict(_kd)
    k[cc] = 127
    return V.mod_params(k, hit, ph, 720), V.mod_params(_kd, hit, ph, 720)


_chk = []
for _i, _t in enumerate(V.MOD_TARGETS):
    _f = {"size": "size", "spin": "angle", "glow": "glow", "sway": "sway", "trail": "trail"}[_t]
    _a, _b = _moved(70 + 2 * _i, 1.0, 0.0)           # REACT on a beat, LFO at its zero crossing
    _c, _d = _moved(70 + 2 * _i, 0.0, 0.25)          # REACT with no beat
    _e, _g = _moved(71 + 2 * _i, 0.0, 0.25)          # LFO at its peak, no beat
    _chk.append(_a[_f] != _b[_f] and _c[_f] == _d[_f] and _e[_f] != _g[_f])
ok(all(_chk) and [V.KNOBS[70 + 2 * i] for i in range(5)] == [t + "_react" for t in V.MOD_TARGETS]
   and [V.KNOBS[71 + 2 * i] for i in range(5)] == [t + "_lfo" for t in V.MOD_TARGETS],
   f"each REACT moves its target only on a beat, each LFO only with its phase {_chk}")

_cam = V.camera(None, {"size": 2.0, "angle": math.pi / 2, "sway": (0.0, 0.0)}, 1280, 720)
_Lc = V.Lines()
_Lc.add([(740.0, 360.0), (840.0, 360.0)], (1, 1, 1), 2.0)
_v0, _v1 = _Lc.array(), _Lc.array(_cam)
_mid = _v1[:, :2].mean(axis=0)
_wid0 = np.hypot(*(_v0[0, :2] - _v0[1, :2]))
_wid1 = np.hypot(*(_v1[0, :2] - _v1[1, :2]))
ok(abs(_mid[0] - 640) < 1.0 and abs(_mid[1] - (360 + 300)) < 1.0 and abs(_wid0 - _wid1) < 1e-3,
   f"camera SIZE x2 + ANGLE 90 deg: a line 150 px right of centre lands 300 px below it "
   f"({_mid.round(1)}), same line thickness")
_sz = V.mod_params({**_kd, 55: 0}, 0, 0, 720)["size"], V.mod_params({**_kd, 55: 127}, 0, 0, 720)["size"]
_an = V.mod_params({**_kd, 56: 0}, 0, 0, 720)["angle"], V.mod_params({**_kd, 56: 127}, 0, 0, 720)["angle"]
ok(abs(_sz[0] - 1 / 3) < 1e-6 and abs(_sz[1] - 3 ** (63 / 64)) < 1e-6
   and abs(_an[0] + math.pi) < 1e-9 and _an[1] > 3.0,
   "SIZE = x1/3 .. x3, ANGLE = -180 .. +180 deg")


class _FakeTempo:
    def __init__(self, bpm, bars):
        self.bpm, self._bars = bpm, bars

    def bars(self):
        return self._bars


_lf = V.ModLFO()
for _ in range(60):
    _lf.step(1 / 60, 0.5, None, _FakeTempo(120, None))
_free = _lf.ph
_lf2 = V.ModLFO()
for _ in range(120):                                  # 2 s at 120 BPM, stopped: 1 bar = 2 s
    _lf2.step(1 / 60, 3.0, 1.0, _FakeTempo(120, None))
_lf3, _bar = V.ModLFO(), [2.30]
_lf3.ph = 0.8
for _ in range(90):                                   # running: glides onto the bar's phase
    _bar[0] += (1 / 60) * 120 / 240
    _lf3.step(1 / 60, 3.0, 1.0, _FakeTempo(120, _bar[0]))
_err = ((_bar[0] % 1.0) - _lf3.ph + 0.5) % 1.0 - 0.5
ok(abs(_free - 0.5) < 1e-6 and min(_lf2.ph, 1.0 - _lf2.ph) < 1e-6 and abs(_err) < 0.01,
   f"LFO: FREE runs at RATE; SYNC stopped = the ORC's tempo; SYNC running locks onto the bar "
   f"(error {_err:+.4f} cycles after 1.5 s)")

_ps = os.path.join(TMP, "playstate.json")
_tw = [1000.0]
with open(_ps, "w") as f:
    json.dump({"t": 1000.0, "transport": {"running": True, "bpm": 120.0, "step": 40,
                                          "age_ms": 50, "step_ms": 125.0}}, f)
_tp = V.Tempo(path=_ps, wall=lambda: _tw[0])
_tp.poll(0.0)
_b1 = _tp.bars()
_tw[0] = 1000.0 + 0.5                                 # long after: capped at a step + SLACK
_b2 = _tp.bars()
_tw[0] = 1000.0 + 3.0                                 # no rewrite for 3 s = the brain is gone
_b3 = _tp.bars()
_tw[0] = 1000.5
with open(_ps, "w") as f:
    json.dump({"t": 1001.0, "transport": {"running": False, "bpm": 90.0, "step": -1}}, f)
os.utime(_ps, None)
_tp._mt = None
_tp.poll(1.0)
ok(abs(_b1 - (40 + 0.4) / 16) < 1e-9 and abs(_b2 - (40 + 3.0) / 16) < 1e-9 and _b3 is None
   and _tp.bars() is None and _tp.bpm == 90.0,
   "Tempo: bars = (step + time since it began) / 16, never runs ahead > a step + 0.25 s; "
   "a feed not rewritten for > 2.5 s (brain gone) = no bar; stopped = no bar, BPM kept")


def _lfo_wobble(slack):
    """The synced LFO fed the way the rig feeds it: the brain rewrites the
    play-state file at each 16th step, 0-33 ms late (age_ms says how late),
    the renderer polls it every 0.1 s and draws at 60 fps. -> the LFO's
    per-frame speed, min / max as a share of the tempo's, over 3 s."""
    path = os.path.join(TMP, "ps_lfo.json")
    clock = [5000.0]
    tp = V.Tempo(path=path, wall=lambda: clock[0])
    tp.SLACK = slack
    lfo, rng, step_s = V.ModLFO(), _rnd.Random(2), 0.125      # 120 BPM, 1/4 bar = 0.5 s
    nxt, step, ver, speeds, prev = 0.0, 0, 0, [], None
    for k in range(int(4.0 * 60)):
        t = k / 60.0
        while nxt <= t:                                   # the brain publishes the new step
            lag = rng.uniform(0.0, 0.033)
            with open(path, "w") as f:
                json.dump({"t": 5000.0 + nxt + lag, "transport": {
                    "running": True, "bpm": 120.0, "step": step,
                    "age_ms": int(lag * 1000), "step_ms": 125.0}}, f)
            ver += 1
            os.utime(path, ns=(ver * 10 ** 9, ver * 10 ** 9))
            step, nxt = step + 1, nxt + step_s
        clock[0] = 5000.0 + t
        tp.poll(t)
        ph = lfo.step(1 / 60, 0.3, 0.25, tp)
        if prev is not None and t > 1.0:
            speeds.append(((ph - prev) % 1.0) / ((1 / 60) / 0.5))
        prev = ph
    return min(speeds), max(speeds)


_wb = _lfo_wobble(V.Tempo.SLACK)
_wb_old = _lfo_wobble(0.5 * 0.125)                      # the old cap: 1.5 steps = a step + half a step
ok(0.9 < _wb[0] and _wb[1] < 1.1,
   f"synced LFO at 120 BPM, fed like the rig (stepwise file, polled 10x/s, publish lag): speed stays "
   f"{_wb[0]:.2f}x-{_wb[1]:.2f}x of the tempo (the 1.5-step cap gave {_wb_old[0]:.2f}x-{_wb_old[1]:.2f}x)")
ok(V.LFO_SYNC_BARS[0] is None and len(V.LFO_SYNC) == len(V.LFO_SYNC_BARS)
   and all(V.lfo_rate(a) < V.lfo_rate(b) for a, b in ((0, 64), (64, 127)))
   and abs(V.lfo_rate(0) - 0.02) < 1e-9 and abs(V.lfo_rate(127) - 4.0) < 1e-9,
   "LFO SYNC list: FREE first, a bar length for each other; RATE 0.02 .. 4 Hz")

_t48 = V.AudioTap(os.path.join(TMP, "no_skipbuf"), shm=os.path.join(TMP, "no_shm"))
_t48.set_band(60.0, 90.0)                             # no 47 Hz bin inside
_bt1 = _t48.band_true
_t48.set_band(30.0, 150.0)
_bt2 = _t48.band_true
ok(sum(_t48.spec_runs) == V.AudioTap.SPEC_BANDS and max(_t48.spec_runs) > 1
   and abs(_bt1[0] - 46.875) < 0.1 and abs(_bt1[1] - 140.625) < 0.1
   and _bt2[0] == 20.0 and abs(_bt2[1] - 187.5) < 0.1,
   f"LISTEN honesty: bass bands sharing a bin are merged ({_t48.spec_runs[:8]}...); a 60-90 Hz band "
   f"really hears {_bt1[0]:.0f}-{_bt1[1]:.0f} Hz, the default 30-150 hears {_bt2[0]:.0f}-{_bt2[1]:.0f}")
V.publish_audio(_t48, 0.2, 0.1, 60, {"lfo": 0.25, "hit": 0.5})
_pub2 = json.load(open(V.AUDIO_PUB))
ok(_pub2["band_true"] == [20, 188] and sum(_pub2["spec_runs"]) == 48
   and _pub2["lfo"] == 0.25 and _pub2["hit"] == 0.5,
   "the page is sent the real band, the bar merges, and the LFO + beat for the MOVEMENT meters")
ok("if now - aud[\"pub_t\"] > 0.04:" in src and "mpulse.hit(music.kick_v)" in src
   and "hit = min(1.2, max(mpulse.step(dt), audio_k * a_p)" in src
   and '"synced": sync is not None and tempo.bars() is not None' in src
   and "(kn[\"trails\"] + mods[\"trail\"])" in src and "* mods[\"glow\"])" in src,
   "the frame loop: spectrum 25/s, MIDI kicks feed the mods' envelope (capped at 1.2), trails + glow "
   "mods applied, 'synced' only while the bar is really followed")

import re as _re                                     # noqa: E402
_jk = {m.group(1): int(m.group(2)) for m in _re.finditer(r"(\w+):\s*\[(\d+), '",
                                                         js.split("const KNOB = {", 1)[1].split("\n};", 1)[0])}
_jd = {m.group(1): int(m.group(2))
       for m in _re.finditer(r"(\w+): (\d+)", js.split("const DEFAULTS = {", 1)[1].split("};", 1)[0])}
_want_def = {V.KNOBS[cc]: v for cc, v in V.KNOB_DEFAULT.items()}
ok(_jk == {n: cc for cc, n in V.KNOBS.items()} and _jd == _want_def
   and js_list("LFO_SYNC") == V.LFO_SYNC and "const SCENE_CC = 20;" in js and V.SCENE_CC == 20,
   f"the page's KNOB table + DEFAULTS + LFO SYNC list = the renderer's "
   f"(missing {sorted(set(V.KNOBS.values()) - set(_jk))}, extra {sorted(set(_jk) - set(V.KNOBS.values()))})")
_tpl = open(os.path.join(HERE, "..", "templates", "visuals.html"), encoding="utf-8").read()
_ids = _re.findall(r"'([a-z-]+-knobs)'", js.split("const GROUP_EL", 1)[1].split("};", 1)[0])
ok(_ids and all(f'id="{i}"' in _tpl for i in _ids)
   and all(f'id="{i}"' in _tpl for i in ("mod-grid", "lfo-sync", "aud-ticks", "mv-hit", "mv-lfo",
                                         "logo-box", "midi-list", "bg-modes", "mask-shapes",
                                         "band-pick", "band-same", "aud-beat2"))
   and "/api/visuals/audio/stream" in js and "def api_visuals_audio_stream" in websrc
   and "es.readyState === EventSource.CLOSED" in js and "if (document.hidden) audStreamClose()" in js
   and "if (prev !== null && d.t !== prev) onAudio(d);" in js,
   "every knob group the script fills has its box on the page; the spectrum streams over SSE, "
   "closes while hidden, polls when the browser gives up, ignores a leftover first frame")

# ---- 2026-10-12: a SECOND beat band (the background's own) --------------------------


def _two_bands(on, onset2=2.0, secs=6.0, fps=30, sig=None):
    """kicks (60 Hz, on the beat) + hats (7 kHz, offbeat) through the fake tap,
    band 1 = 30-150 Hz, band 2 = 5-10 kHz. -> (band-1 beat times, band-2 times)."""
    sh = FakeShm()
    tap = V.AudioTap(os.path.join(TMP, "no_skipbuf_2b"), shm=sh.path)
    tap.set_band(30.0, 150.0)
    tap.set_band2(5000.0, 10000.0)
    tap.band2_on, tap.ONSET2 = on, onset2
    b1, b2, per, sent = [], [], sh.period, 0
    for k in range(int(secs * fps)):
        want = int((k + 1) / fps * 48000)
        while sent + per <= want:
            t = np.arange(sent, sent + per) / 48000
            x = np.clip((sig or kicks_and_hats)(t), -1, 1).astype("f4")
            sh.push(np.stack([x, x], 1))
            sent += per
        x1, x2, _ = tap.update(k / fps)
        if x1 > 0:
            b1.append(k / fps)
        if x2 > 0:
            b2.append(k / fps)
    return b1, b2


_k1, _h2 = _two_bands(True)
_k1_off, _h2_off = _two_bands(False)
_ph1, _ph2 = phase_of(_k1), phase_of(_h2)
ok(len(_k1) >= 8 and len(_h2) >= 8 and _ph1 is not None and _ph2 is not None
   and (_ph1 < 0.1 or _ph1 > 0.4) and 0.15 < _ph2 < 0.35
   and _k1_off == _k1 and _h2_off == [],
   f"two beat bands from ONE tap: band 1 (30-150 Hz) beats on the kicks (phase {_ph1:.2f} of 0.5 s), "
   f"band 2 (5-10 kHz) on the offbeat hats ({_ph2:.2f}); OWN BAND off = band 2 silent and band 1 unchanged")


def _soft_hats(t):
    """kicks + a STEADY 7 kHz tone that swells only 30% on the offbeat: a
    jump of ~1.7x in band 2, so SENS decides whether it counts."""
    kph, hph = t % 0.5, (t - 0.25) % 0.5
    return (0.4 * np.sin(2 * np.pi * 60 * t) * np.exp(-kph * 25)
            + 0.1 * np.sin(2 * np.pi * 7000 * t) * (1.0 + 0.35 * np.exp(-hph * 12)))


_k_s, _h2_strict = _two_bands(True, onset2=V.audio_onset(0), sig=_soft_hats)
_k_l, _h2_loose = _two_bands(True, onset2=V.audio_onset(127), sig=_soft_hats)
ok(len(_h2_strict) == 0 and len(_h2_loose) >= 8 and V.KNOBS[61] == "sens2" and _k_s == _k_l,
   f"SENS BG is band 2's own: strict {len(_h2_strict)} vs loose {len(_h2_loose)} hat beats, "
   "band 1's kicks unchanged by it")
_kb = {cc: V.KNOB_DEFAULT.get(cc, 64) for cc in V.KNOBS}
ok(not V.band2_on(_kb) and V.bg_pulse(_kb, 0.7, 0.2) == 0.7
   and V.bg_pulse({**_kb, 62: 64}, 0.7, 0.2) == 0.2 and V.bg_pulse({**_kb, 62: 63}, 0.7, 0.2) == 0.7
   and [V.KNOBS[c] for c in (59, 60, 61, 62)] == ["band2_lo", "band2_hi", "sens2", "band2_on"]
   and V.KNOB_DEFAULT[62] == 0 and (V.KNOB_DEFAULT[59], V.KNOB_DEFAULT[60]) == (V.KNOB_DEFAULT[32], V.KNOB_DEFAULT[33]),
   "OWN BAND (CC 62) starts off = the background follows band 1 as before; >= 64 = band 2's pulse")
ok("audio_k * bg_pulse(music.knobs, a_p, b_p)" in src and "a_beat, a_beat2, a_lvl = audio.update(now)" in src
   and "bpulse.attack, bpulse.release = apulse.attack, apulse.release" in src
   and "audio.band2_on = band2_on(music.knobs)" in src and '"beat2_age"' in src and '"band2_true"' in src,
   "the frame loop: the background's audio beat comes from bg_pulse, band 2 shares ATTACK / RELEASE, "
   "and the page gets band 2's real band + beat")

# visuals.sh shot (SIGWINCH): nothing heavy on the frame loop. MEASURED
# 2026-10-12 on the Pi: pygame.image.save in the writer thread held the GIL for
# the whole encode (a 0.66 s frozen frame), and the RGB screen.read cost ~125 ms
# (RGBA ~40). Now one RGBA read on the loop and a thread writes the PNG with
# zlib (which lets go of the GIL). Driven with a fake GL context: the main thread
# must keep running while the PNG is made, and the file must decode (by hand) to
# GL's bottom-up rows top-down; an opaque frame stays RGBA (no conversion copy),
# a frame with real alpha comes out RGB so it is never see-through.
import struct as _st                                 # noqa: E402
import zlib as _zl                                   # noqa: E402
_SW, _SH = 1920, 1080
_srng = np.random.default_rng(7)
_srgba = (_srng.random(_SW * _SH * 4) * 48).astype(np.uint8)
_srgba[3::4] = 255
_sraw = _srgba.tobytes()
_sev = []


class _ShotCtx:
    class screen:
        @staticmethod
        def read(components=3):
            _sev.append(components)
            return _sraw


def _png_decode(path):
    pb = open(path, "rb").read()
    pos, ch = 8, {}
    while pos < len(pb):
        (ln,) = _st.unpack(">I", pb[pos:pos + 4])
        kind, body = pb[pos + 4:pos + 8], pb[pos + 8:pos + 8 + ln]
        (crc,) = _st.unpack(">I", pb[pos + 8 + ln:pos + 12 + ln])
        assert crc == _zl.crc32(kind + body) & 0xFFFFFFFF, kind
        ch[kind] = body
        pos += 12 + ln
    w, h, depth, colour, _c, _f, _i = _st.unpack(">IIBBBBB", ch[b"IHDR"])
    n = {2: 3, 6: 4}[colour]
    rows = np.frombuffer(_zl.decompress(ch[b"IDAT"]), np.uint8).reshape(h, 1 + w * n)
    assert pb[:8] == b"\x89PNG\r\n\x1a\n" and depth == 8 and b"IEND" in ch
    assert not rows[:, 0].any()                      # filter 0 on every row
    return colour, rows[:, 1:].reshape(h, w, n)


V.SHOT_PNG = os.path.join(TMP, "shot.png")
_tick_worst, _t_last = 0.0, time.monotonic()
_t_end = _t_last + 15.0
V.save_shot(_ShotCtx, _SW, _SH)
_tick_worst = time.monotonic() - _t_last           # the call itself is frame-loop time
_t_last = time.monotonic()
while not os.path.exists(V.SHOT_PNG) and time.monotonic() < _t_end:
    time.sleep(0.002)
    _n = time.monotonic()
    _tick_worst, _t_last = max(_tick_worst, _n - _t_last), _n
_png_ok = _png2_ok = False
try:
    _col, _px = _png_decode(V.SHOT_PNG)
    _png_ok = _col == 6 and np.array_equal(_px, _srgba.reshape(_SH, _SW, 4)[::-1])
    _see = _srgba[:4 * 64 * 8].copy()                # 64x8, one pixel half see-through
    _see[3 + 4 * 70] = 128
    os.remove(V.SHOT_PNG)
    V.write_shot(_see.tobytes(), 64, 8)
    _col2, _px2 = _png_decode(V.SHOT_PNG)
    _png2_ok = _col2 == 2 and np.array_equal(_px2, _see.reshape(8, 64, 4)[::-1, :, :3])
except Exception as _e:
    print(f"  shot decode: {_e!r}")
ok(_png_ok and _png2_ok and _sev == [4] and _tick_worst < 0.1
   and "pygame.image.save(" not in src and "save_shot(ctx, W, H)" in src,
   f"visuals.sh shot: one RGBA read ({_sev}); a valid PNG, rows top-down, opaque = RGBA, real "
   f"alpha = RGB; the frame loop kept running while it encoded (worst main-thread gap "
   f"{_tick_worst * 1000:.0f} ms, must be < 100)")


# ---- 2026-10-12: your own pattern tiles (the CUSTOM set) -----------------------------
_home0 = os.environ.get("HOME")
os.environ["HOME"] = os.path.join(TMP, "home_custom")         # never the real ~/orc1_patterns
os.makedirs(os.environ["HOME"], exist_ok=True)
try:
    _cs = VP.pattern_sets(V.BG_PATTERNS)
    ok(_cs[-1] == ("custom", "custom", []) and len(_cs) == 6 and V.sets_count() == 6
       and [n for n, _, _ in _cs[:5]] == ["built-in", "hero", "monster 1", "monster 2", "monster 3"],
       f"CUSTOM is the LAST set and is there even when empty (6 sets; the others keep their places) "
       f"{[n for n, _, _ in _cs]}")

    # the SET knob keeps meaning the same set when the count grows 5 -> 6
    _same = all(V.bucket(V.remap_set_knob(v, 5, 6), [0] * 6) == V.bucket(v, [0] * 5) for v in range(128))
    with open(V.STATE, "w") as f:
        json.dump({"knobs": {"bg_set": 89, "bg_pattern": 110}}, f)       # owner's, pre-CUSTOM
    _old = V.load_knobs()
    with open(V.STATE, "w") as f:
        json.dump({"knobs": {"bg_set": 74, "bg_pattern": 110}, "sets_n": 6}, f)
    _new = V.load_knobs()
    ok(_same and V.bucket(_old[50], [0] * 6) == 3 and _old[34] == 110 and _new[50] == 74
       and V.remap_set_knob(89, 6, 6) == 89,
       f"SET knob survives the new set: every old position lands on the same set; a pre-CUSTOM state "
       f"(89 = monster 2) loads as {_old[50]} (still monster 2); a file that says sets_n 6 is left alone")

    _good = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
             '<rect width="40" height="40" fill="black"/><circle cx="20" cy="20" r="8" fill="white"/></svg>')
    _bad = {
        "doctype": '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                   '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">&x;</svg>',
        "script": '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><script>alert(1)</script></svg>',
        "image": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="4" '
                 'height="4"><image xlink:href="file:///etc/hostname" width="4" height="4"/></svg>',
        "ext use": '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><use href="/etc/x.svg#a"/></svg>',
        "url()": '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
                 '<rect width="4" height="4" fill="url(http://x/y.svg#p)"/></svg>',
        "@import": '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><style>@import "x.css";</style></svg>',
        "not svg": '<html><body/></html>',
        "broken": '<svg width="4"',
        "no size": '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>',
        "huge": '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">' + " " * (600 * 1024) + '</svg>',
    }
    _why = {}
    for _k, _t in _bad.items():
        try:
            VP.check_upload(_t)
            _why[_k] = None
        except ValueError as e:
            _why[_k] = str(e)
    _vb = VP.check_upload('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 20"><rect/></svg>')
    _px = VP.check_upload('<svg xmlns="http://www.w3.org/2000/svg" height="12px" width="24px"><rect/></svg>')
    _loc = VP.check_upload('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><defs><pattern id="p"/>'
                           '</defs><rect width="4" height="4" fill="url(#p)"/><use href="#p"/></svg>')
    ok(all(_why.values()) and VP.check_upload(_good) == (40.0, 40.0) and _vb == (30.0, 20.0)
       and _px == (24.0, 12.0) and _loc == (4.0, 4.0),
       f"upload checks refuse DOCTYPE/ENTITY, script, embedded images, links + url() to other files, "
       f"@import, non-SVG, broken XML, no size, > 512 KB {[k for k, v in _why.items() if not v]}; size comes "
       f"from width/height (any order, px ok) or the viewBox; links inside the file (#id) are fine")

    import shutil as _sh                                 # noqa: E402
    _rsvg = _sh.which("rsvg-convert")
    try:
        import PIL                                       # noqa: F401
        _pil = True
    except ImportError:
        _pil = False
    if _rsvg and _pil:
        _sig0 = VP.custom_sig()
        _n1, _w1 = VP.save_upload("My Dots.svg", _good)
        _blue = _good.replace('fill="white"', 'fill="#0000ff"')
        try:
            VP.save_upload("blue.svg", _blue)
            _blue_err = None
        except ValueError as e:
            _blue_err = str(e)
        _solid = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                  '<rect width="10" height="10" fill="white"/></svg>')
        _n2, _w2 = VP.save_upload("solid.svg", _solid)
        _names = VP.pattern_sets(V.BG_PATTERNS)[-1][2]
        _png = VP.tile_png("custom", "my-dots")
        # (this suite stubs pygame, so the tile is read the way the upload check reads it)
        _bright, _dark = VP.tile_stats(_png)
        _units = VP.svg_size(os.path.join(VP.user_dir(), "my-dots.svg"))
        _sig1 = VP.custom_sig()
        _gone = VP.delete_upload("solid") and not os.path.exists(os.path.join(VP.user_dir(), "solid.svg"))
        _left = os.listdir(VP.user_dir())
        ok(_n1 == "my-dots" and _w1 is None and _blue_err and "RED" in _blue_err
           and _n2 == "solid" and _w2 and "black gaps" in _w2
           and _names == ["my-dots", "solid"] and os.path.exists(_png)
           and _units == (40.0, 40.0) and _bright == 255 and 0.5 < _dark < 0.95
           and _sig1 != _sig0 and _gone and _left == ["my-dots.svg"]
           and not VP.delete_upload("no-such"),
           f"upload end to end (rsvg + PIL on the Pi): white-on-black stored as 'my-dots', blue-only refused "
           f"({(_blue_err or '')[:40]}...), solid white stored WITH a no-gaps warning, both in the CUSTOM "
           f"set, the tile loads as grey (white = 1, gaps = 0), the folder signature changes, delete works, "
           f"no temp files left {_left}")
    else:
        ok(True, "[SKIPPED: no rsvg-convert / PIL] CUSTOM upload end to end")
finally:
    if _home0 is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _home0

ok("sig = vis_patterns.custom_sig()" in src and "sets[:] = vis_patterns.pattern_sets(BG_PATTERNS)" in src
   and "if sets[si][2] else pat[\"key\"]" in src and "now_names[key[1]] != slug" in src
   and '"sets_n": sets_count()' in src,
   "renderer: CUSTOM uploads/deletes reload the sets live (cache for that set dropped), an empty set "
   "asks for nothing, the loader never caches a tile whose slot changed meanwhile, the state file "
   "records sets_n")
ok('folder not in ("hero", "monster", "custom")' in websrc and 'if folder != vp.CUSTOM:' in websrc
   and "vp.save_upload(up.filename, text)" in websrc and "request.content_length > vp.MAX_UPLOAD" in websrc
   and 'id="pat-custom"' in _tpl and "async function patUpload()" in js and "async function patDelete()" in js,
   "web: upload (size judged before the body is spooled) + delete for custom only + previews; "
   "the page's CUSTOM upload box")

total = passed + failed
print(f"{passed} passed, {failed} failed  (expected {EXPECTED})")
if failed or total != EXPECTED:
    if total != EXPECTED:
        print(f"  COUNT DRIFT: ran {total}, expected {EXPECTED}")
    sys.exit(1)
