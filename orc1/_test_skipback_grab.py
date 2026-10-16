#!/usr/bin/env python3
"""_test_skipback_grab.py -- the skipback GRAB (skipback.sh) against synthetic rings.

2026-09-28: the grab was rewritten to STREAM the ring into the WAV on disk
instead of reading the whole span into RAM and parking a raw copy in /tmp
(tmpfs = RAM). A full 20-min stereo grab used to dip MemAvailable by 360 MB.

This suite runs the REAL script (bash) with its env overrides pointed at a
throwaway ring, and checks:
  * every byte of the WAV data equals the ring span it should come from
    (no wrap, wrap, full-length grab, missing pos file, mono track dir)
  * the WAV header (channels / rate / width / frame count)
  * the caller contract: rc 0 + a "Saved: <path>" line, secs clamped to the
    ring length, no half-written .part file left behind, mp3 still works
  * nothing lands in /tmp, and the copy's peak RSS stays far below the grab
    size (the regression this rewrite exists for)

STALLED WRITER (2026-09-29): a CARD ring's writer can stall for seconds, so
ring.pos stops moving. The grab now waits for it and cuts at the PRESS.
Checked with the wait enabled: a stale pos that resumes -> waits, then the
file ends at the press (not early, not at the late head); a fresh pos -> no
wait, byte-exact to pos; a pos that never moves -> grabbed as-is after the
wait. Every other check runs with SKIPBACK_FRESH_WAIT=0 (the old path).
Mutation-prove: drop the `pos = (pos - min(back, ...` trim -> the press check
fails; make the stale test always false -> the wait check fails.

Linux only (the script uses nice/ionice). Run on the Pi:  python3 _test_skipback_grab.py
Point SKIPBACK_SH at another copy to mutation-test an older script.
"""
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import wave

EXPECTED = 22

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("SKIPBACK_SH",
                        os.path.join(HERE, "skipback", "skipback.sh"))
RATE = 48000

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


class Rig:
    """A throwaway skipback root + tracks JSON + output dir."""

    def __init__(self):
        # ring root in /tmp (tmpfs on the Pi, like the real one); output on DISK
        self.root = tempfile.mkdtemp(prefix="sbgrab_ring_")
        self.out = tempfile.mkdtemp(prefix="sbgrab_out_",
                                    dir=os.path.expanduser("~"))
        self.tracks_json = os.path.join(self.root, "tracks.json")
        self.tracks = []

    def add_track(self, tid, secs, stereo, name="", pos=None, seed=1):
        ch = 2 if stereo else 1
        size = RATE * ch * 2 * secs
        d = self.root if tid == 0 else os.path.join(self.root, f"t{tid}")
        os.makedirs(d, exist_ok=True)
        data = random.Random(seed).randbytes(size)
        with open(os.path.join(d, "ring.raw"), "wb") as f:
            f.write(data)
        if pos is not None:
            with open(os.path.join(d, "ring.pos"), "w") as f:
                f.write(str(pos))
        self.tracks.append({"id": tid, "name": name, "stereo": stereo,
                            "length_secs": secs, "armed": True})
        with open(self.tracks_json, "w") as f:
            json.dump({"tracks": self.tracks}, f)
        return data

    def grab(self, secs, fmt="wav", tid=None, wait="0", fresh=True, press=None):
        if fresh:                       # a live writer flushes ring.pos ~24x/s
            for pf in glob.glob(os.path.join(self.root, "**", "ring.pos"),
                                recursive=True):
                os.utime(pf, None)
        env = dict(os.environ, SKIPBACK_DIR=self.root,
                   SKIPBACK_TRACKS_JSON=self.tracks_json,
                   SKIPBACK_OUTDIR=self.out, SKIPBACK_FRESH_WAIT=wait)
        if press is not None:
            env["SKIPBACK_PRESS_TS"] = repr(press)
        args = ["bash", SCRIPT, str(secs), fmt] + ([] if tid is None else [str(tid)])
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
        out = (r.stdout or "").strip()
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        return r.returncode, saved, r.stderr

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)


def expected_span(data, pos, need):
    buf = len(data)
    start = (pos - need) % buf
    if start < pos:
        return data[start:start + need]
    return data[start:] + data[:pos]


def read_wav(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getframerate(), w.getsampwidth(),
                w.getnframes(), w.readframes(w.getnframes()))


MARGIN = 2.0     # skipback.sh keeps this much clear of the live write head


def grab_matches(rig, data, pos, secs, ch, tid=None):
    rc, saved, err = rig.grab(secs, tid=tid)
    if rc != 0 or not saved or not os.path.exists(saved):
        return False, f"rc={rc} saved={saved!r} err={err.strip()[:200]}"
    need = secs * RATE * ch * 2
    nch, rate, width, nframes, frames = read_wav(saved)
    # review 2026-10-01: a grab reaching into the last MARGIN s before the
    # live head is trimmed to stay clear of it (it used to start AT the head)
    if need > len(data) - int(MARGIN * RATE) * ch * 2:
        got = nframes * 2 * ch
        if not (len(data) - int((MARGIN + 0.1) * RATE) * ch * 2 <= got
                <= len(data) - int(MARGIN * RATE) * ch * 2):
            return False, f"trimmed to {nframes / RATE:.2f} s"
        need = got
    want = expected_span(data, pos, need)
    if (nch, rate, width, nframes) != (ch, RATE, 2, need // (2 * ch)):
        return False, f"header {(nch, rate, width, nframes)}"
    if frames != want:
        return False, f"data mismatch ({len(frames)} vs {len(want)} bytes)"
    return True, ""


def test_data_and_contract():
    rig = Rig()
    try:
        fb = 2 * 2  # stereo frame bytes
        # track 0 (legacy top-level ring), 10 s stereo, head mid-ring
        pos = 6 * RATE * fb
        data = rig.add_track(0, 10, True, name="MAIN", pos=pos)

        ok, d = grab_matches(rig, data, pos, 4, 2, tid=0)
        check("no-wrap grab: every byte + header", ok, d)

        ok, d = grab_matches(rig, data, pos, 8, 2, tid=0)
        check("wrap-around grab: every byte + header", ok, d)

        ok, d = grab_matches(rig, data, pos, 10, 2, tid=0)
        check("full-length grab (start == pos)", ok, d)

        ok, d = grab_matches(rig, data, pos, 3, 2, tid=None)
        check("legacy call (no track arg) reads track 0", ok, d)

        rc, saved, _ = rig.grab(999, tid=0)
        n = read_wav(saved)[3] if rc == 0 and saved else 0
        clamped = (rc == 0 and saved.endswith(".wav") and "_10s_" in saved
                   and (10 - MARGIN - 0.1) * RATE <= n <= (10 - MARGIN) * RATE)
        check("seconds clamped to the ring length (name + frames)", clamped, saved)
        check("a full-ring grab keeps MARGIN s clear of the live head "
              "(never opens with the newest audio)",
              rc == 0 and read_wav(saved)[4] == expected_span(data, pos, n * fb),
              f"{n / RATE:.2f} s")

        check("saved file is in OUTDIR with the track-name prefix",
              os.path.dirname(saved) == rig.out
              and os.path.basename(saved).startswith("MAIN_"), saved)

        leftovers = [p for p in os.listdir(rig.out) if p.endswith(".part")]
        check("no .part file left behind", not leftovers, str(leftovers))

        rc, saved, err = rig.grab(2, fmt="mp3", tid=0)
        if shutil.which("lame"):
            ok = (rc == 0 and saved.endswith(".mp3") and os.path.getsize(saved) > 1000
                  and not [p for p in os.listdir(rig.out) if p.endswith(".part")])
            check("mp3 grab works and cleans up", ok, f"rc={rc} {err.strip()[:200]}")
        else:
            check("mp3 grab works and cleans up [SKIPPED: no lame]", True)

        # mono track with its own dir, head at 0 (whole ring is 'behind' the head)
        mono = rig.add_track(3, 5, False, name="T3", pos=0, seed=7)
        ok, d = grab_matches(rig, mono, 0, 5, 1, tid=3)
        check("mono track dir, pos 0, full grab", ok, d)
        ok, d = grab_matches(rig, mono, 0, 2, 1, tid=3)
        check("mono track dir, partial grab", ok, d)

        # missing pos file -> pos 0 (the old script's behaviour, kept)
        os.remove(os.path.join(rig.root, "t3", "ring.pos"))
        ok, d = grab_matches(rig, mono, 0, 2, 1, tid=3)
        check("missing ring.pos falls back to pos 0", ok, d)

        rc, saved, err = rig.grab(2, tid=9)
        check("unknown/unarmed track fails cleanly (rc != 0, no Saved)",
              rc != 0 and not saved, f"rc={rc}")

        # the LIVE ring's size + channels (heads.json) beat a config that is
        # ahead of it (a stereo/length edit the recorder hasn't applied yet)
        m6 = rig.add_track(6, 4, False, name="EDITED", pos=RATE * 2 * 3, seed=13)
        rig.tracks[-1].update(stereo=True, length_secs=9)       # config ahead
        with open(rig.tracks_json, "w") as f:
            json.dump({"tracks": rig.tracks}, f)
        with open(os.path.join(rig.root, "heads.json"), "w") as f:
            json.dump({"ts": time.time(), "tracks": {"6": {
                "channels": 1, "buffer_bytes": len(m6), "store": "ram"}}}, f)
        ok, d = grab_matches(rig, m6, RATE * 2 * 3, 1, 1, tid=6)
        check("ring size + channels come from the live ring (heads.json), "
              "not a config edit it hasn't applied", ok, d)

        # every grab ends at the press, stalled or not (single-track = GRAB ALL)
        pos7 = RATE * 2 * 3
        m7 = rig.add_track(7, 8, False, name="PRESS", pos=pos7, seed=14)
        rc, saved, err = rig.grab(1, tid=7, press=time.time() - 1.0)
        got = read_wav(saved)[4] if rc == 0 and saved else b""
        end = m7.find(got[-512:]) + 512 if len(got) >= 512 else -1
        want = pos7 - int((1.0 - 0.03) * RATE) * 2
        check("a fresh-head grab also ends at the press",
              end > 0 and abs(end - want) < int(0.15 * RATE) * 2,
              f"end={end} want~{want}")
    finally:
        rig.cleanup()


def test_stalled_writer():
    rig = Rig()
    try:
        fb = 4                                   # stereo
        bps = RATE * fb
        pos0 = 3 * bps
        data = rig.add_track(4, 20, True, name="CARD", pos=pos0, seed=11)
        posf = os.path.join(rig.root, "t4", "ring.pos")
        old = time.time() - 5
        os.utime(posf, (old, old))              # the writer has been stalled 5 s
        env = dict(os.environ, SKIPBACK_DIR=rig.root,
                   SKIPBACK_TRACKS_JSON=rig.tracks_json,
                   SKIPBACK_OUTDIR=rig.out, SKIPBACK_FRESH_WAIT="5")
        t0 = time.time()
        env["SKIPBACK_PRESS_TS"] = repr(t0)      # the caller stamps the press
        p = subprocess.Popen(["bash", SCRIPT, "2", "wav", "4"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.2)                          # ... then the writer catches up:
        pos1 = pos0 + 6 * bps                    # the backlog lands in one flush
        with open(posf + ".tmp", "w") as f:
            f.write(str(pos1))
        os.replace(posf + ".tmp", posf)
        t_flush = os.stat(posf).st_mtime
        out, err = p.communicate(timeout=30)
        elapsed = time.time() - t0
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        ok = p.returncode == 0 and os.path.exists(saved)
        end = -1
        if ok:
            frames = read_wav(saved)[4]
            end = data.find(frames[-512:]) + 512 if len(frames) >= 512 else -1
        # press ~= t0 (bash start); the file must end ~(t_flush - t0) before pos1
        want = pos1 - int((t_flush - t0) * RATE) * fb
        check("stalled writer: the grab WAITS for it, then succeeds",
              ok and elapsed >= 1.1 and "stalled" in err, f"rc={p.returncode} {elapsed:.2f}s {err.strip()[:160]}")
        check("... and the file ends at the PRESS (not early at the stale head, not late)",
              end > 0 and abs(end - want) < int(0.25 * bps) and pos0 < end < pos1,
              f"end={end} want~{want} pos0={pos0} pos1={pos1}")

        # a head that never moves (recorder down): grabbed as-is after the wait
        os.utime(posf, (old, old))
        t0 = time.time()
        rc, saved, err = rig.grab(2, tid=4, wait="1", fresh=False)
        got = read_wav(saved)[4] if rc == 0 and saved else b""
        check("head never moves: grabbed up to the last written audio after the wait",
              rc == 0 and time.time() - t0 >= 0.9 and "did not move" in err
              and got == expected_span(data, pos1, 2 * bps), f"rc={rc} {err.strip()[:160]}")
    finally:
        rig.cleanup()


def test_fresh_head_no_wait():
    rig = Rig()
    try:
        bps = RATE * 4
        pos = 7 * bps
        data = rig.add_track(5, 20, True, name="RAMLIKE", pos=pos, seed=12)
        t0 = time.time()
        rc, saved, err = rig.grab(3, tid=5, wait="5")
        got = read_wav(saved)[4] if rc == 0 and saved else b""
        return rc == 0 and time.time() - t0 < 3 and "stalled" not in err \
            and got == expected_span(data, pos, 3 * bps)
    finally:
        rig.cleanup()


def test_memory_and_tmp():
    """The regression this rewrite exists for: the grab must not hold the span
    in RAM, and must not write to /tmp (tmpfs)."""
    rig = Rig()
    try:
        secs = 220                                   # 42 MB stereo ring
        pos = 0                                      # whole ring in ONE span
        data = rig.add_track(0, secs, True, name="BIG", pos=pos)
        grab_bytes = secs * RATE * 4
        before = set(glob.glob("/tmp/skipback_*"))
        # run the grab in a fresh interpreter so RUSAGE_CHILDREN holds ONLY it
        probe = (
            "import os,resource,subprocess,sys\n"
            "env=dict(os.environ,SKIPBACK_DIR=sys.argv[1],"
            "SKIPBACK_TRACKS_JSON=sys.argv[2],SKIPBACK_OUTDIR=sys.argv[3])\n"
            "r=subprocess.run(['bash',sys.argv[4],sys.argv[5],'wav','0'],env=env,"
            "capture_output=True,text=True)\n"
            "print(r.returncode, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n")
        r = subprocess.run([sys.executable, "-c", probe, rig.root, rig.tracks_json,
                            rig.out, SCRIPT, str(secs)],
                           capture_output=True, text=True, timeout=180)
        try:
            rc, maxrss_kb = map(int, r.stdout.split())
        except ValueError:
            rc, maxrss_kb = -1, -1
        limit_kb = 20 * 1024
        print(f"    (grab peak RSS: {maxrss_kb // 1024} MB)")
        check(f"peak RSS of the grab < 20 MB for a {grab_bytes // 1048576} MB grab",
              rc == 0 and 0 < maxrss_kb < limit_kb,
              f"rc={rc} maxrss={maxrss_kb // 1024} MB")
        after = set(glob.glob("/tmp/skipback_*"))
        with open(SCRIPT) as f:
            src = f.read()
        check("nothing written to /tmp (runtime + source)",
              after == before and "/tmp/" not in src,
              f"new={sorted(after - before)}")
        saved = sorted(glob.glob(os.path.join(rig.out, "BIG_*.wav")))
        got = read_wav(saved[-1])[4] if saved else b""
        # a whole-ring ask keeps MARGIN s clear of the live head
        ok = (bool(saved) and len(got) >= grab_bytes - int((MARGIN + 0.1) * RATE) * 4
              and got == expected_span(data, pos, len(got)))
        check("big grab is byte-exact too", ok)
    finally:
        rig.cleanup()


def main_stall():
    test_stalled_writer()
    check("fresh head (every RAM ring): no wait, byte-exact to pos", test_fresh_head_no_wait())


def main():
    if not sys.platform.startswith("linux"):
        print("Linux only (nice/ionice/bash) -- run on the Pi.")
        return 1
    print(f"skipback grab suite -- script: {SCRIPT}")
    os.environ["SKIPBACK_FRESH_WAIT"] = "0"     # old path unless a check opts in
    test_data_and_contract()
    test_memory_and_tmp()
    main_stall()
    passed = sum(_results)
    failed = len(_results) - passed
    print(f"\n{passed} passed, {failed} failed")
    if len(_results) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_results)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
