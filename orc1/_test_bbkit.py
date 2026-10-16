#!/usr/bin/env python3
"""Regression suite for orc1_bbkit_host (the sampled BeatBuddy kit engine).

Self-contained: builds a synthetic kit (stdlib `wave` WAVs + kit.json) in a temp
dir, so it needs neither scipy, jack, mido, nor the real 630MB kits. Exercises
the audible-critical behaviour: velocity-layer selection, round-robin rotation,
choke groups, polyphony stealing, sample-accurate note offset, master scaling,
and the MIDI router (bound-channel notes, bus-level CC, all-notes-off).

Run:  python3 _test_bbkit.py
"""
import os, sys, json, wave, tempfile, struct, shutil
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_bbkit_host as bb
import orc1_synth as S

EXPECTED = 34
_p = _f = 0
def check(name, cond):
    global _p, _f
    if cond: _p += 1
    else:
        _f += 1
        print(f"  ! FAIL: {name}")

def write_wav(path, amp=0.5, frames=1200, ch=1, sr=None):
    sr = sr or bb.SR
    data = np.full((frames, ch), amp, dtype=np.float32)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()
    w = wave.open(path, "wb")
    w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(pcm); w.close()

def build_kit(d):
    files = {"k_soft.wav":0.2, "k_a.wav":0.5, "k_b.wav":0.6,
             "hc.wav":0.3, "ho.wav":0.35, "st.wav":0.4}
    for fn, amp in files.items():
        write_wav(os.path.join(d, fn), amp=amp, ch=(2 if fn == "ho.wav" else 1))
    man = {"name": "TestKit", "instruments": {
        "36": {"name": "Kick", "choke": 0, "poly": 2, "volume": 100, "layers": [
            {"lo": 0, "hi": 63,  "rr": [{"wav": "k_soft.wav"}]},
            {"lo": 64, "hi": 127, "rr": [{"wav": "k_a.wav"}, {"wav": "k_b.wav"}]}]},
        "42": {"name": "HatClosed", "choke": 1, "poly": 1, "volume": 100,
               "layers": [{"lo": 0, "hi": 127, "rr": [{"wav": "hc.wav"}]}]},
        "46": {"name": "HatOpen", "choke": 1, "poly": 1, "volume": 80,
               "layers": [{"lo": 0, "hi": 127, "rr": [{"wav": "ho.wav"}]}]},
    }}
    with open(os.path.join(d, "kit.json"), "w") as f:
        json.dump(man, f)

class Msg:
    def __init__(self, type, channel=0, note=0, velocity=0, control=0, value=0, program=0):
        self.type=type; self.channel=channel; self.note=note; self.velocity=velocity
        self.control=control; self.value=value; self.program=program

def write_noise(path, frames, ch, bits=16, seed=1):
    """Real (non-constant) audio so any mis-slice / off-by-one shows up."""
    rng = np.random.default_rng(seed)
    x = np.clip(rng.standard_normal((frames, ch)) * 0.3, -1, 1)
    if bits == 16:
        pcm = (x * 32767).astype("<i2").tobytes()
    else:                                                  # 24-bit little-endian
        v = (x * 8388607).astype("<i4")
        pcm = b"".join(int(t).to_bytes(3, "little", signed=True) for t in v.ravel())
    w = wave.open(path, "wb")
    w.setnchannels(ch); w.setsampwidth(bits // 8); w.setframerate(bb.SR)
    w.writeframes(pcm); w.close()


def render_voice(buf, blocks=30, n=1024, fade_at=12):
    """Mix one Voice block by block (gain + a fade that starts mid-sample)."""
    v = bb.Voice(buf, 0.7, 36, 0, pre=37)
    L, R = [], []
    for i in range(blocks):
        oL = np.zeros(n, np.float32); oR = np.zeros(n, np.float32)
        if i == fade_at:
            v.fade_len = 30000; v.start_fade()
        v.mix(oL, oR, n)
        L.append(oL); R.append(oR)
    return np.concatenate(L), np.concatenate(R)


def streaming_checks(tmp):
    """STREAMED samples (2026-09-23): head in RAM, body mapped from the card."""
    frames = int(bb.SR * 0.5)                         # 500 ms > the 100 ms head
    st, mo, b24 = (os.path.join(tmp, f) for f in ("st.wav", "mo.wav", "b24.wav"))
    write_noise(st, frames, 2, seed=2); write_noise(mo, frames, 1, seed=3)
    write_noise(b24, 4000, 2, bits=24, seed=4)
    for name, path in (("stereo", st), ("mono", mo)):
        sb = bb.SampleBuf(path)
        ref = bb._load_wav_stereo(path)               # the OLD full float32 load
        a, b = render_voice(sb), render_voice(ref)
        check(f"streamed {name} renders BIT-IDENTICAL to the old full load "
              f"(across the head/card boundary + a fade)",
              sb.streamed and np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1]))
    # absolute, not relative: both renders above share Voice.mix, so a mixer bug
    # (e.g. right channel reading the left) would pass the identity check.
    sb = bb.SampleBuf(st)
    with wave.open(st) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2)
    v = bb.Voice(sb, 1.0, 36, 0, pre=0)
    oL = np.zeros(8000, np.float32); oR = np.zeros(8000, np.float32)
    v.mix(oL, oR, 8000)                                # crosses the 4800-frame head
    want = raw[:8000].astype(np.float32) / 32768.0
    check("stereo: left out = file's LEFT channel, right out = file's RIGHT",
          np.array_equal(oL, want[:, 0]) and np.array_equal(oR, want[:, 1]))
    check("only the HEAD is resident in RAM (16-bit), the body is file-mapped",
          sb.streamed and sb.full is None and len(sb.head) == bb._HEAD_FRAMES
          and sb.head.dtype == np.int16 and len(sb) == frames)
    s24 = bb.SampleBuf(b24)
    check("non-16-bit WAV falls back to the old full load (still plays)",
          (not s24.streamed) and np.array_equal(np.asarray(s24), bb._load_wav_stereo(b24)))
    # read-ahead happens on the MIDI thread (note_on), never in render()
    eng = fresh_engine(os.path.join(tmp, "kits")); eng._jack_ft = None
    calls = []
    orig = bb.SampleBuf.warm
    bb.SampleBuf.warm = lambda self: calls.append(self.path)
    try:
        eng.note_on(36, 100)
        check("note_on (MIDI thread) pre-warms every variant of the layer it will hit",
              sorted(os.path.basename(c) for c in calls) == ["k_a.wav", "k_b.wav"])
        def _boom(self):
            raise AssertionError("warm() called from the audio callback")
        bb.SampleBuf.warm = _boom
        ok = True
        try:
            eng.render(256); eng.render(256)
        except AssertionError:
            ok = False
        check("render() (the JACK callback) never issues a read-ahead", ok)
    finally:
        bb.SampleBuf.warm = orig


def fresh_engine(kdir):
    bb.KITS_DIR = kdir
    return bb.KitEngine()

def main():
    tmp = tempfile.mkdtemp(prefix="bbkit_")
    kdir = os.path.join(tmp, "kits", "TestKit")
    os.makedirs(kdir)
    build_kit(kdir)
    # keep the engine's state file out of the real home
    bb.STATE_FILE = os.path.join(tmp, "state.json")
    try:
        # ---- kit loading ----
        name, instr = bb.load_kit(kdir)
        check("kit name", name == "TestKit")
        check("3 instruments", set(instr) == {36, 42, 46})
        check("kick has 2 layers", len(instr[36].layers) == 2)
        check("kick hard layer has 2 rr", len(instr[36].layers[1].rr) == 2)
        check("stereo wav -> [N,2]", instr[46].layers[0].rr[0].shape[1] == 2)
        check("mono wav widened to 2ch", instr[36].layers[0].rr[0].shape[1] == 2)
        check("dynamics preserved (no per-sample normalise)",
              abs(float(np.abs(instr[36].layers[0].rr[0]).max()) - 0.2) < 0.02)

        # ---- layer selection ----
        check("vel 30 -> soft layer", instr[36].layer_for(30) is instr[36].layers[0])
        check("vel 100 -> hard layer", instr[36].layer_for(100) is instr[36].layers[1])

        # ---- round-robin ----
        L = instr[36].layers[1]
        b0, b1, b2 = L.pick(), L.pick(), L.pick()
        check("round-robin alternates", b0 is not b1)
        check("round-robin wraps", b2 is b0)

        # ---- triggering / voice selection ----
        eng = fresh_engine(os.path.dirname(kdir))
        eng._jack_ft = None
        check("engine loaded kit", set(eng.instruments) == {36, 42, 46})
        eng._trigger(36, 30, 0)
        check("soft hit picks soft buffer", eng.voices[-1].buf is eng.instruments[36].layers[0].rr[0])
        eng.voices.clear()
        eng._trigger(36, 120, 0)
        check("hard hit gain reflects velocity", abs(eng.voices[-1].gain - (120/127.0)) < 1e-6)

        # ---- render: idle vs sounding ----
        eng.voices.clear()
        L0, R0 = eng.render(256)
        check("idle renders silence", float(np.abs(L0).max()) == 0.0)
        eng.note_on(36, 110)
        L1, R1 = eng.render(256)
        check("note renders audio", float(np.abs(L1).max()) > 0.0)

        # ---- choke group ----
        eng.voices.clear()
        eng._trigger(46, 100, 0)              # open hat
        open_voice = eng.voices[-1]
        eng._trigger(42, 100, 0)              # closed hat chokes open (same group 1)
        check("choke fades group-mate", open_voice.fade_left >= 0)
        eng.voices.clear()
        eng._trigger(46, 100, 0); ov = eng.voices[-1]
        eng._trigger(36, 100, 0)              # kick is not in the hat choke group
        check("non-group hit does not choke", ov.fade_left < 0)

        # ---- polyphony steal ----
        eng.voices.clear()
        for _ in range(3):
            eng._trigger(36, 100, 0)          # kick poly = 2
        live = [v for v in eng.voices if v.fade_left < 0]
        check("poly limit steals oldest", len(live) == 2)

        # ---- sample-accurate offset ----
        v = bb.Voice(np.ones((500, 2), dtype=np.float32), 1.0, 36, 0, pre=10)
        oL = np.zeros(64, dtype=np.float32); oR = np.zeros(64, dtype=np.float32)
        v.mix(oL, oR, 64)
        check("pre-offset leaves lead silent", float(np.abs(oL[:10]).max()) == 0.0)
        check("pre-offset sounds after offset", float(oL[10]) > 0.0)

        # ---- voice completion ----
        v2 = bb.Voice(np.ones((100, 2), dtype=np.float32), 1.0, 36, 0, pre=0)
        oL = np.zeros(256, dtype=np.float32); oR = np.zeros(256, dtype=np.float32)
        v2.mix(oL, oR, 256)
        check("short sample completes", v2.done)

        # ---- master scaling ----
        eng.voices.clear(); eng.master = 0.0
        eng.note_on(36, 127); Lz, Rz = eng.render(256)
        check("master 0 = silence", float(np.abs(Lz).max()) == 0.0)

        # ---- router (seed bindings deterministically: bbkit on ch2, so the
        #       result doesn't depend on the live ~/.orc1_bindings.json) ----
        saved_bind = dict(S._BINDINGS)
        S._BINDINGS.clear(); S._BINDINGS[2] = "bbkit"
        try:
            eng2 = fresh_engine(os.path.dirname(kdir)); eng2._jack_ft = None
            route = bb._make_router(eng2)
            route(Msg("note_on", channel=1, note=36, velocity=100))   # ch2 = bound
            eng2.render(64)
            check("router: note on bound channel triggers", len(eng2.voices) >= 1)
            eng2.voices.clear()
            route(Msg("note_on", channel=5, note=36, velocity=100))   # ch6 = unbound
            eng2.render(64)
            check("router: note on unbound channel ignored", len(eng2.voices) == 0)
            route(Msg("control_change", channel=S._VOL_CH - 1, control=bb._VOL_BBKIT_CC, value=64))
            check("router: bus CC sets master", abs(eng2.master - 64/127.0) < 1e-6)
            eng2.voices.clear(); eng2._trigger(36, 100, 0)
            route(Msg("control_change", channel=1, control=123, value=0))
            check("router: all-notes-off fades voices", eng2.voices[-1].fade_left >= 0)
        finally:
            S._BINDINGS.clear(); S._BINDINGS.update(saved_bind)

        streaming_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_p} passed, {_f} failed  (expected {EXPECTED})")
    if _p != EXPECTED or _f:
        print("  !! COUNT/FAIL MISMATCH -- regression"); sys.exit(1)

if __name__ == "__main__":
    main()
