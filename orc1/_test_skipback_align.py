#!/usr/bin/env python3
"""_test_skipback_align.py -- sample-aligned multitrack skipback grabs.

2026-09-28. Each skipback track drains on its own, so at any instant two heads
can sit periods apart and ring.pos alone can't line tracks up. The recorder
now stamps every JACK period with its frame time; heads.json publishes each
head's exact frame; skipback_grab_multi.py cuts every track at the newest frame
they ALL reached.

This drives the REAL Recorder (_bring_up, _process, drain, _write_heads,
_publish_rt) with fake JACK ports -- real jack.RingBuffers, no server -- and
feeds every port the SAME signal, a function of the absolute frame. Drains are
deliberately staggered so the heads disagree, the JACK clock wraps 2^32 inside
the grab window, a period is dropped on one track, and a track is armed late.
Then it runs the REAL grab script and checks every sample.

Pure temp dirs; touches no live ring, config or status. Safe while the rig runs.
Mutation-prove: in skipback_grab_multi.plan() set lag = 0 -> the alignment
checks fail; drop the `self.gaps += 1` line -> the gap checks fail.

Stereo JOINS (2026-09-29): two mono tracks written as one stereo WAV. Every
port here carries the same signal, so an L/R swap is invisible in the rings --
the L/R order is proven on interleave()/outputs() directly. Mutation-prove:
swap lb/rb in interleave() -> the L/R check fails; drop the MONO test in
outputs() -> the stereo-join refusal check fails.

STALLED WRITER (2026-09-29): a CARD ring can stall the writer for seconds, so
heads.json stops updating. GRAB ALL now waits for a snapshot published after
the press and cuts AT the press (plan(back=...)). Mutation-prove: pass back=0
-> the press check fails. Every other check runs with SKIPBACK_FRESH_WAIT=0.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "skipback"))
import jack  # noqa: E402  (real module: RingBuffer needs only libjack, no server)
import skipback_recorder as SR  # noqa: E402

GRAB = os.environ.get("SKIPBACK_GRAB_MULTI",
                      os.path.join(HERE, "skipback", "skipback_grab_multi.py"))
EXPECTED = 30
P = 1024                       # period frames
RATE = SR.RATE
_res = []


def check(name, ok, detail=""):
    _res.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def enc(frames):
    """float32 signal that is a pure function of the absolute frame index."""
    return (((frames % 30011) - 15000) / 32768.0).astype(np.float32)


def as_i16(frames):
    """What the recorder writes for enc(frames) -- same ops as Track.drain."""
    return (np.clip(enc(frames), -1.0, 1.0) * 32767.0).astype("<i2")


class FakePort:
    def __init__(self, name):
        self.name = name
        self.buf = None

    def get_buffer(self):
        return memoryview(self.buf).cast("B")   # len() = bytes, like CFFI

    def unregister(self):
        pass


class FakeInports:
    def register(self, name):
        return FakePort(name)


class FakeClient:
    def __init__(self, ft):
        self.last_frame_time = ft
        self.inports = FakeInports()
        self.blocksize = P
        self.samplerate = RATE


class Rig:
    def __init__(self, ft0):
        self.root = tempfile.mkdtemp(prefix="sbalign_ring_")
        self.out = tempfile.mkdtemp(prefix="sbalign_out_", dir=os.path.expanduser("~"))
        self.r = SR.Recorder(os.path.join(self.root, "cfg.json"), self.root,
                             os.path.join(self.root, "status.json"),
                             src_path=os.path.join(self.root, "nosrc.json"))
        self.r.client = FakeClient(ft0)
        self.ft = ft0
        self.skip = set()

    def arm(self, tid, secs, stereo):
        self.r._bring_up({"id": tid, "name": f"T{tid}", "stereo": stereo,
                          "length_secs": secs, "armed": True, "mode": "auto",
                          "left": "", "right": "", "dir": f"t{tid}"})
        self.r._publish_rt()

    def cycle(self):
        """One JACK period: every port gets enc(abs frames); tracks in
        self.skip are left out of this callback (= a dropped period)."""
        abs_frames = (self.ft + np.arange(P, dtype=np.int64)) & 0xFFFFFFFF
        sig = enc(abs_frames)
        for trk in self.r.tracks.values():
            for p in trk.ports:
                p.buf = sig
        full = self.r._rt_groups
        if self.skip:
            keep = [g for g, t in zip(full, self.r._writer_tracks) if t.id not in self.skip]
            self.r._rt_groups = tuple(keep)
        self.r.client.last_frame_time = self.ft
        self.r._process(P)
        self.r._rt_groups = full
        self.ft = (self.ft + P) & 0xFFFFFFFF

    def cycle_split(self, tid, snap=False):
        """One period where the writer drains track `tid` BETWEEN the
        callback queueing its audio and queueing its stamp -- the race a
        live writer hits. Other tracks get the period normally."""
        abs_frames = (self.ft + np.arange(P, dtype=np.int64)) & 0xFFFFFFFF
        sig = enc(abs_frames)
        trk = self.r.tracks[tid]
        for t in self.r.tracks.values():
            for p in t.ports:
                p.buf = sig
        full = self.r._rt_groups
        self.r._rt_groups = tuple(g for g, t in zip(full, self.r._writer_tracks) if t.id != tid)
        self.r.client.last_frame_time = self.ft
        self.r._process(P)
        self.r._rt_groups = full
        for p, rb in zip(trk.ports, trk.rbs):
            rb.write(p.get_buffer())                   # audio queued ...
        trk.drain()                                    # ... writer runs now ...
        if snap:
            self.r._write_heads()                      # ... and publishes heads
        trk.stamp_rb.write(SR.STAMP.pack(self.ft, P))  # ... stamp lands after
        self.ft = (self.ft + P) & 0xFFFFFFFF

    def drain(self, *tids):
        for tid in tids:
            self.r.tracks[tid].drain()

    def heads(self):
        self.r._write_heads()
        with open(os.path.join(self.root, SR.HEADS_NAME)) as f:
            return json.load(f)

    def grab(self, secs, tracks="", refresh=True, extra=(), wait="0"):
        if refresh:
            now = time.time()               # a live writer: heads are fresh
            for trk in self.r.tracks.values():
                if trk.head is not None:
                    trk.head = trk.head[:5] + (now,)
            self.r._write_heads()           # a live writer does this ~24x/s
        args = [sys.executable, GRAB, str(secs), "--root", self.root, "--outdir", self.out]
        if tracks:
            args += ["--tracks", tracks]
        args += list(extra)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, SKIPBACK_FRESH_WAIT=wait))
        out = r.stdout.strip()
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        return r.returncode, saved, r.stderr

    def cleanup(self):
        for t in list(self.r.tracks.values()):
            t.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)


def load_dir(d):
    """{filename: int16 array (mono) or (L, R)}, manifest"""
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "*.wav"))):
        with wave.open(p) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            ch = w.getnchannels()
        out[os.path.basename(p)] = (a[0::2], a[1::2]) if ch == 2 else (a,)
    with open(os.path.join(d, "manifest.json")) as f:
        return out, json.load(f)


def aligned_to(wavs, end_frame, frames):
    want = as_i16((end_frame - frames + np.arange(frames, dtype=np.int64)) & 0xFFFFFFFF)
    for name, chans in wavs.items():
        for c in chans:
            if len(c) != frames or not np.array_equal(c, want):
                bad = int(np.argmax(c != want)) if len(c) == frames else -1
                return False, f"{name}: len {len(c)} vs {frames}, first diff at {bad}"
    return True, ""


def main():
    SR.log = lambda *a, **k: None
    print(f"skipback aligned-grab suite -- grab script: {GRAB}")
    # JACK's 32-bit frame clock wraps ~40 periods before the end of the run,
    # i.e. inside the grab window.
    rig = Rig(ft0=(2 ** 32 - 380 * P) & 0xFFFFFFFF)
    try:
        rig.arm(0, 5, True)
        rig.arm(1, 4, False)
        for i in range(420):
            if i == 60:
                rig.arm(2, 6, False)                    # armed late
            rig.cycle()
            # staggered drains: RT periods land between tracks' drains
            if i % 3 == 0:
                rig.drain(0)
            if i % 5 == 0:
                rig.drain(1)
            if i % 7 == 0 and 2 in rig.r.tracks:
                rig.drain(2)
        rig.drain(0); rig.cycle(); rig.drain(1); rig.cycle(); rig.cycle(); rig.drain(2)
        h = rig.heads()
        frames = {k: v["frame"] for k, v in h["tracks"].items()}
        check("heads disagree before the grab (the test is adversarial)",
              len(set(frames.values())) == 3, str(frames))
        pos_ok = all(open(os.path.join(rig.root, f"t{t}", "ring.pos")).read()
                     == str(rig.r.tracks[t].pos) for t in (0, 1, 2))
        check("per-track ring.pos files unchanged in meaning (= head pos)", pos_ok)

        end = min(frames.values(), key=lambda f: SR_signed(f - frames["0"]))
        rc, saved, err = rig.grab(1.5)
        check("grab 1.5 s from all tracks: rc 0 + Saved folder",
              rc == 0 and os.path.isdir(saved), f"rc={rc} {err.strip()[:300]}")
        wavs, man = load_dir(saved)
        check("one WAV per track + manifest", len(wavs) == 3 and len(man["tracks"]) == 3,
              str(list(wavs)))
        ok, d = aligned_to(wavs, end, int(1.5 * RATE))
        check("every sample of every track lines up (across the 2^32 wrap)", ok, d)
        check("manifest: end frame = newest frame all tracks reached, aligned",
              man["end_jack_frame"] == end and man["aligned"] is True and man["frames"] == int(1.5 * RATE),
              str({k: man[k] for k in ("end_jack_frame", "aligned", "frames")}))
        check("no .part left behind",
              not [p for p in os.listdir(rig.out) if p.endswith(".part")])

        time.sleep(1.1)                                 # new folder name
        rc, saved0, err = rig.grab(0)
        wavs0, man0 = load_dir(saved0) if rc == 0 else ({}, {})
        n0 = man0.get("frames", 0)
        # t1 is the limit: 4 s ring, full -> minus the 2 s safety margin
        ok, d = aligned_to(wavs0, end, n0) if n0 else (False, err)
        check("seconds=0 grabs everything held in common, still aligned",
              rc == 0 and RATE <= n0 <= 2 * RATE + P and ok, f"frames={n0} {d}")
        time.sleep(1.1)
        rc, saved99, _ = rig.grab(99)
        check("asking for more than exists clamps to the same span",
              rc == 0 and load_dir(saved99)[1]["frames"] == n0)

        time.sleep(1.1)
        rc, saved_s, _ = rig.grab(1, tracks="2,0")
        w_s, m_s = load_dir(saved_s)
        e_s = min(frames["0"], frames["2"], key=lambda f: SR_signed(f - frames["0"]))
        ok, d = aligned_to(w_s, e_s, RATE)
        check("--tracks subset: only those tracks, aligned to THEIR common end",
              rc == 0 and len(w_s) == 2 and ok, d)

        # ── stereo joins ──
        time.sleep(1.1)
        rc, saved_j, err = rig.grab(1.5, extra=["--joins", "1:2"])
        w_j, m_j = load_dir(saved_j) if rc == 0 else ({}, {})
        jn = [n for n in w_j if n.startswith("t01+t02_")]
        ok, d = aligned_to(w_j, end, int(1.5 * RATE)) if rc == 0 else (False, err)
        check("--joins 1:2: t1+t2 become ONE stereo WAV next to t0, all aligned",
              rc == 0 and len(w_j) == 2 and len(jn) == 1 and len(w_j[jn[0]]) == 2 and ok,
              f"{list(w_j)} {d}")
        check("manifest records the join (files list + L/R per track)",
              rc == 0 and {f["file"]: f["tracks"] for f in m_j.get("files", [])}.get(jn[0] if jn else "") == [1, 2]
              and [t.get("joined") for t in m_j["tracks"] if t["id"] in (1, 2)] == ["L", "R"]
              and "joins_skipped" not in m_j, str(m_j.get("files")))

        import skipback_grab_multi as GM
        lb = np.array([1, 2, 3], dtype="<i2").tobytes()
        rb = np.array([-1, -2, -3], dtype="<i2").tobytes()
        il = np.frombuffer(GM.interleave(lb, rb), dtype="<i2")
        fake = [{"id": i, "h": {"channels": c}} for i, c in ((0, 2), (1, 1), (2, 1), (3, 1))]
        outs, skipped = GM.outputs(fake, [(2, 1), (0, 3), (1, 3)])
        check("interleave puts the L track LEFT; a join's order is [L, R]",
              il.tolist() == [1, -1, 2, -2, 3, -3]
              and [[t["id"] for t in g] for g in outs] == [[0], [2, 1], [3]],
              f"{il.tolist()} {[[t['id'] for t in g] for g in outs]}")
        check("a join with a STEREO track or a reused track is refused, with reasons",
              len(skipped) == 2 and "MONO" in skipped[0] and "another join" in skipped[1],
              str(skipped))

        jf = os.path.join(rig.root, "joins.json")
        with open(jf, "w") as f:
            json.dump({"joins": [[0, 1], [1, 2]]}, f)
        time.sleep(1.1)
        rc, saved_f, err = rig.grab(1, extra=["--joins-file", jf])
        w_f, m_f = load_dir(saved_f) if rc == 0 else ({}, {})
        check("--joins-file: joins applied, the stereo-track join skipped + reported",
              rc == 0 and sorted(w_f) == sorted(n for n in w_f if n.startswith(("t00_", "t01+t02_")))
              and len(w_f) == 2 and any("MONO" in x for x in m_f.get("joins_skipped", [])),
              f"{list(w_f)} {m_f.get('joins_skipped')} {err.strip()[:200]}")
        with open(jf, "w") as f:
            json.dump({"joins": [[1, "2"]]}, f)
        rc, saved_b, err = rig.grab(1, extra=["--joins-file", jf])
        check("a malformed joins file refuses the grab (never silently unjoined)",
              rc != 0 and not saved_b and "bad joins" in err, err.strip())

        # a dropped period on track 1
        rig.skip = {1}
        rig.cycle()
        rig.skip = set()
        for _ in range(60):
            rig.cycle()
        rig.drain(0, 1, 2)
        h2 = rig.heads()
        check("a dropped period is counted as a gap (heads + status)",
              h2["tracks"]["1"]["gaps"] == 1 and h2["tracks"]["0"]["gaps"] == 0
              and rig.r.tracks[1].gaps == 1)
        rc, saved_g, _ = rig.grab(1)
        w_g, m_g = load_dir(saved_g)
        ok, d = aligned_to(w_g, h2["tracks"]["0"]["frame"], RATE)
        check("audio AFTER the gap still lines up", rc == 0 and ok, d)
        # review 2026-10-01: the drop is written as SILENCE of its exact length,
        # so the audio BEFORE it lines up too (it used to slide by a period)
        rc, saved_g2, _ = rig.grab(1.5)              # t1's ring is only 4 s
        w_g2, m_g2 = load_dir(saved_g2) if rc == 0 else ({}, {})
        n2 = int(1.5 * RATE)
        end2 = h2["tracks"]["0"]["frame"]
        want = as_i16((end2 - n2 + np.arange(n2, dtype=np.int64)) & 0xFFFFFFFF)
        gap_ok, gd = rc == 0 and len(w_g2) == 3, ""
        for name, chans in w_g2.items():
            c = chans[0]
            if len(c) != n2:
                gap_ok, gd = False, f"{name}: len {len(c)}"
                continue
            diff = np.nonzero(c != want)[0]
            if name.startswith("t01"):
                # one period's span differs, and it is silent (a few pattern
                # samples in it are 0 anyway, so "differs" can be < P)
                if not (P - 16 <= len(diff) <= P and diff[-1] - diff[0] < P
                        and not c[diff].any()):
                    gap_ok, gd = False, f"{name}: {len(diff)} differ"
            elif len(diff):
                gap_ok, gd = False, f"{name}: {len(diff)} differ"
        check("a grab ACROSS the gap: every track lines up, the drop is one "
              "silent period", gap_ok, gd)
        check("manifest notes the gap (still aligned + a note)",
              m_g["aligned"] is True and m_g.get("gaps") == {"1": 1} and "note" in m_g,
              str({k: m_g.get(k) for k in ("aligned", "gaps", "note")}))

        for i in range(40):
            if i % 4 == 0:
                rig.cycle_split(i % 3)
            else:
                rig.cycle()
        rig.drain(0, 1, 2)
        h3 = rig.heads()
        time.sleep(1.1)
        rc, saved_r, _ = rig.grab(0.5)
        ok, d = aligned_to(load_dir(saved_r)[0], h3["tracks"]["0"]["frame"], RATE // 2) if rc == 0 else (False, "rc")
        check("writer draining mid-callback (audio queued, stamp not yet): still aligned",
              ok and all(v["gaps"] == (1 if k == "1" else 0) for k, v in h3["tracks"].items()), d)

        # grab at the worst moment: snapshot taken while t0 has a period of
        # audio queued whose stamp hasn't landed yet
        rig.drain(0, 1, 2)
        rig.cycle()                                     # one stamped period queued
        rig.cycle_split(0, snap=True)
        hs = json.load(open(os.path.join(rig.root, SR.HEADS_NAME)))
        rc, saved_w, _ = rig.grab(0.5, refresh=False)
        ok, d = aligned_to(load_dir(saved_w)[0], hs["tracks"]["1"]["frame"], RATE // 2) if rc == 0 else (False, "rc")
        check("grab taken mid-callback (unstamped audio queued): aligned", ok, d)

        # ── stalled writer: heads.json stops, then catches up ──
        rig.drain(0, 1, 2)
        hp = os.path.join(rig.root, SR.HEADS_NAME)
        rig.r._write_heads()
        d = json.load(open(hp))
        d["ts"] = time.time() - 5                       # snapshot 5 s stale
        for v in d["tracks"].values():
            v["t"] = d["ts"]                            # ... every head too
        json.dump(d, open(hp, "w"))
        import threading
        t0 = time.time()
        res = {}

        def run_grab():
            res["r"] = rig.grab(1, refresh=False, wait="5")
        th = threading.Thread(target=run_grab)
        th.start()
        time.sleep(1.2)
        for _ in range(60):                             # the backlog lands ...
            rig.cycle()
        rig.drain(0, 1, 2)
        rig.r._write_heads()                            # ... and heads publish
        hs2 = json.load(open(hp))
        th.join(30)
        rc, saved_s, err = res.get("r", (1, "", "no result"))
        w_s, m_s = load_dir(saved_s) if rc == 0 else ({}, {})
        common = min((v["frame"] for v in hs2["tracks"].values()),
                     key=lambda f: SR_signed(f - hs2["tracks"]["0"]["frame"]))
        back = SR_signed(common - m_s.get("end_jack_frame", common)) if m_s else 0
        ref = min(hs2["tracks"].values(),
                  key=lambda v: SR_signed(v["frame"] - hs2["tracks"]["0"]["frame"]))
        want = int((ref["t"] - t0) * RATE)
        ok, dd = (aligned_to(w_s, m_s["end_jack_frame"], m_s["frames"])
                  if rc == 0 and m_s.get("frames", 0) > RATE // 2 else (False, err))
        check("stalled writer: GRAB ALL waits for fresh heads (no refusal), still aligned",
              rc == 0 and "stalled" in err and ok, f"rc={rc} {err.strip()[:160]} {dd}")
        check("... and cuts at the PRESS: the end sits (heads ts - press) before the heads",
              rc == 0 and abs(back - want) < int(0.25 * RATE),
              f"back={back} want~{want}")

        # the live writer loop (not just _write_heads) publishes the snapshot
        hp = os.path.join(rig.root, SR.HEADS_NAME)
        os.remove(hp)
        import threading
        rig.r._stop.clear()
        th = threading.Thread(target=rig.r._writer_loop, daemon=True)
        th.start()
        time.sleep(0.3)
        rig.r._stop.set()
        th.join(2)
        check("the writer loop publishes heads.json every pass", os.path.exists(hp))

        # a gap that can't be filled (frames jump absurdly) restarts the
        # aligned span instead of claiming alignment across it
        t1 = rig.r.tracks[1]
        t1.head_ft = (t1.head_ft - (1 << 30)) & 0xFFFFFFFF
        rig.cycle()
        rig.drain(0, 1, 2)
        h3 = rig.heads()
        check("an unfillable gap restarts that track's aligned span",
              h3["tracks"]["1"]["aligned_bytes"] <= 2 * P * 2
              and h3["tracks"]["0"]["aligned_bytes"] > RATE * 2,
              str({k: v["aligned_bytes"] for k, v in h3["tracks"].items()}))
        rc, saved_x, err = rig.grab(1, tracks="9")
        check("unknown track: fails cleanly, nothing saved", rc != 0 and not saved_x)
        rc, saved_x, err = rig.grab(1, tracks="0,9")
        m_x = load_dir(saved_x)[1] if rc == 0 else {}
        check("a chosen track that isn't recording is left out (noted), the rest saved",
              rc == 0 and m_x.get("not_recording") == [9]
              and [t["id"] for t in m_x["tracks"]] == [0], f"rc={rc} {err.strip()[:200]}")
        hp = os.path.join(rig.root, SR.HEADS_NAME)
        d = json.load(open(hp))
        d["ts"] = time.time() - 60
        json.dump(d, open(hp, "w"))
        rc, saved_x, err = rig.grab(1, refresh=False)
        check("stale heads (recorder not writing): refuses", rc != 0 and "old" in err, err.strip())
        with open(GRAB) as f:
            src = f.read()
            check("grab script never writes to /tmp", "\"/tmp" not in src and "'/tmp" not in src)
    finally:
        rig.cleanup()

    passed = sum(_res)
    print(f"\n{passed} passed, {len(_res) - passed} failed")
    if len(_res) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_res)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if passed == len(_res) else 1


def SR_signed(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x >= (1 << 31) else x


if __name__ == "__main__":
    sys.exit(main())
