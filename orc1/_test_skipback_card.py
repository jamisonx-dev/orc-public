#!/usr/bin/env python3
"""_test_skipback_card.py -- skipback CARD tracks (store:"card", 2026-09-28).

A CARD track keeps ring.raw on the card partition (/mnt/skipring on the rig),
ring.pos + a ring.raw SYMLINK in the tmpfs track dir, and a once-a-second
checkpoint (fdatasync the ring, THEN record the position on the card) so the
history survives a reboot ("persist", the default) or starts fresh on a new
boot ("persist": false).

Drives the REAL Recorder (_bring_up, _process, drain, _checkpoint_all,
_teardown_track, _apply_ram_guard) with fake JACK ports + real
jack.RingBuffers, a temp dir standing in for the card partition, and the REAL
skipback.sh / skipback_grab_multi.py as readers. Simulates: recorder restart
(same boot), clean reboot, power cut between checkpoints, fresh-on-boot, an
unmounted card, a full card, disarm.

TWO WRITERS (2026-09-29): a stalled card write must never hold up a RAM
track. MEASURED on the rig that day: with ONE writer thread, SD stalls under a
28 GB copy blocked the RAM tracks' drains, their 2.7 s RT buffers overflowed
(15 gaps each) and their ring.pos froze. test_card_stall_isolated() freezes a
card ring's write() and runs the real RAM + CARD writer threads in real time.
Mutation-prove: start ONE writer (_writer_loop(None)) -> the RAM-isolation
checks fail; run() starting one writer thread -> the run() source check fails.

Temp dirs only; touches no live ring, config, status or the real card
partition. Safe while the rig runs.
"""
import inspect
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
import jack  # noqa: E402,F401  (RingBuffer needs only libjack)
import skipback_recorder as SR  # noqa: E402

SB_SH = os.path.join(HERE, "skipback", "skipback.sh")
GRAB = os.path.join(HERE, "skipback", "skipback_grab_multi.py")
EXPECTED = 38
P = 1024
RATE = SR.RATE
_res = []


def check(name, ok, detail=""):
    _res.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


class FakePort:
    def __init__(self, name):
        self.buf = None

    def get_buffer(self):
        return memoryview(self.buf).cast("B")

    def unregister(self):
        pass


class FakeClient:
    def __init__(self):
        self.last_frame_time = 0
        self.blocksize = P
        self.samplerate = RATE

        class _In:
            def register(self, name):
                return FakePort(name)
        self.inports = _In()


STATE = {"mounted": True, "cap": 1 << 30, "boot": "boot-A"}
SR._disk_mounted = lambda path: STATE["mounted"]
SR._disk_capacity = lambda path: STATE["cap"]
SR._boot_id = lambda: STATE["boot"]
SR.log = lambda *a, **k: None


def cfg(tid, secs, stereo=True, store="card", persist=True, name=None):
    return {"id": tid, "name": name or f"T{tid}", "stereo": stereo,
            "length_secs": secs, "armed": True, "mode": "auto", "left": "",
            "right": "", "dir": f"t{tid}", "store": store, "persist": persist}


class Rig:
    """One recorder 'process'. tmpfs root and card root are separate dirs;
    a new Rig on the same card root with a WIPED tmpfs root = a reboot."""

    def __init__(self, card_root, tmp_root=None):
        self.card_root = card_root
        self.root = tmp_root or tempfile.mkdtemp(prefix="sbcard_tmpfs_")
        self.r = SR.Recorder(os.path.join(self.root, "cfg.json"), self.root,
                             os.path.join(self.root, "status.json"),
                             src_path=os.path.join(self.root, "nosrc.json"),
                             disk_root=card_root)
        self.r.client = FakeClient()
        self.ft = 1000 * P
        self.n = 0

    def arm(self, c):
        self.r._bring_up(c)
        self.r._publish_rt()
        return self.r.tracks.get(c["id"])

    def run(self, periods, drain_every=3):
        for i in range(periods):
            sig = (np.sin(2 * np.pi * 441 * (self.n + np.arange(P)) / RATE) * 0.5
                   + (self.n % 7) * 0.01).astype(np.float32)
            for t in self.r.tracks.values():
                for p in t.ports:
                    p.buf = sig
            self.r.client.last_frame_time = self.ft
            self.r._process(P)
            self.ft += P
            self.n += P
            if i % drain_every == 0:
                for t in self.r.tracks.values():
                    t.drain()
        for t in self.r.tracks.values():
            t.drain()

    def tracks_json(self, cfgs):
        path = os.path.join(self.root, "tracks.json")
        with open(path, "w") as f:
            json.dump({"tracks": [dict(c, source={"mode": "auto"}) for c in cfgs]}, f)
        return path

    def grab1(self, tid, secs, cfgs):
        env = dict(os.environ, SKIPBACK_DIR=self.root,
                   SKIPBACK_TRACKS_JSON=self.tracks_json(cfgs),
                   SKIPBACK_OUTDIR=os.path.join(self.card_root, "..", os.path.basename(self.card_root) + "_out"))
        r = subprocess.run(["bash", SB_SH, str(secs), "wav", str(tid)],
                           capture_output=True, text=True, env=env, timeout=60)
        out = r.stdout.strip()
        path = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        if r.returncode or not path:
            return None
        with wave.open(path) as w:
            data = w.readframes(w.getnframes())
        os.remove(path)
        return data


def ckpt(card, d="t0"):
    """This track's entry in the card's combined checkpoint file (or None)."""
    try:
        with open(os.path.join(card, SR.CKPT_NAME)) as f:
            return json.load(f)["tracks"].get(d)
    except Exception:
        return None


def ring_span(trk, secs):
    """The last `secs` of a track's ring straight from the card file."""
    need = int(secs * trk.bytes_per_sec)
    with open(trk.ring_path, "rb") as f:
        buf = f.read()
    start = (trk.pos - need) % trk.buffer_bytes
    return buf[start:start + need] if start < trk.pos else buf[start:] + buf[:trk.pos]


class BlockingFile:
    """Stands in for a card ring's file object: write() blocks while
    `gate` is clear -- an SD card stalling the writer thread."""

    def __init__(self, f, gate):
        self._f, self._gate = f, gate

    def write(self, b):
        self._gate.wait()
        return self._f.write(b)

    def __getattr__(self, name):
        return getattr(self._f, name)


def test_card_stall_isolated(base, one_writer=False, watchdog=False):
    import threading
    card = os.path.join(base, "card_stall" + ("_wd" if watchdog else ""))
    os.makedirs(card)
    STATE.update(mounted=True, boot="boot-S")
    R = Rig(card)
    tc = R.arm(cfg(0, 8, True, store="card"))
    tr = R.arm(cfg(1, 8, False, store="ram"))
    gate = threading.Event()                   # clear = the card is stalled
    gate.set()                                 # ... not yet
    tc.ring = BlockingFile(tc.ring, gate)
    R.r._stop.clear()
    loops = [None] if one_writer else [False, True]
    ths = [threading.Thread(target=R.r._writer_loop, args=(c,), daemon=True)
           for c in loops]
    for th in ths:
        th.start()

    def feed(seconds):
        n = int(seconds * RATE / P)
        t_next = time.monotonic()
        for _ in range(n):
            sig = np.full(P, 0.1, dtype=np.float32)
            for t in R.r.tracks.values():
                for p in t.ports:
                    p.buf = sig
            R.r.client.last_frame_time = R.ft
            R.r._process(P)
            R.ft += P
            t_next += P / RATE
            time.sleep(max(0.0, t_next - time.monotonic()))

    feed(0.5)                                  # both writing normally ...
    gate.clear()                               # ... then the card stalls
    feed(0.3)
    wd = {}
    if watchdog:
        # The 5 s watchdog's _reconcile -> _publish_rt during the stall: once
        # with nothing changed, once with a change (a track list rebuilt).
        # Review 2026-10-01: it used to hold the RAM lock while waiting for
        # the stalled CARD lock = the RAM drains froze with the card.
        def dog():
            t0 = time.monotonic()
            R.r._publish_rt()
            wd["unchanged_s"] = time.monotonic() - t0
            R.r._writer_tracks = list(R.r._writer_tracks)   # != the tuple
            R.r._publish_rt()
            wd["changed_done"] = True
        wth = threading.Thread(target=dog, daemon=True)
        wth.start()
        time.sleep(0.05)
    r0, c0 = tr.written, tc.written
    feed(1.5)
    r1, c1 = tr.written, tc.written
    hp = os.path.join(R.root, SR.HEADS_NAME)
    try:
        hs = json.load(open(hp))
        now = time.time()
        age_r = now - hs["tracks"]["1"]["t"]
        age_c = now - hs["tracks"]["0"]["t"]
    except Exception as e:
        print("    heads read failed:", repr(e))
        age_r = age_c = 99.0
    pos_age = time.time() - os.stat(os.path.join(R.root, "t1", "ring.pos")).st_mtime
    gate.set()                                 # the card comes back
    feed(0.5)
    if watchdog:
        wth.join(3)
    time.sleep(0.3)
    R.r._stop.set()
    for th in ths:
        th.join(3)
    got = {"ram_during": (r1 - r0) / tr.bytes_per_sec, "card_during": c1 - c0,
           "age_r": age_r, "age_c": age_c, "pos_age": pos_age,
           "gaps": (tr.gaps, tc.gaps), "card_after": tc.written - c1,
           "wd": wd}
    for t in list(R.r.tracks.values()):
        t.close()
    return got


def main():
    print("skipback CARD-track suite")
    base = tempfile.mkdtemp(prefix="sbcard_", dir=os.path.expanduser("~"))
    card = os.path.join(base, "card")
    os.makedirs(card)
    try:
        # ---- bring-up ----------------------------------------------------
        STATE.update(mounted=True, boot="boot-A")
        A = Rig(card)
        c0, c1 = cfg(0, 6, True), cfg(1, 5, False, store="ram")
        t0 = A.arm(c0)
        t1 = A.arm(c1)
        disk_file = os.path.join(card, "t0", "ring.raw")
        st = os.stat(disk_file)
        check("card ring file lives on the card, full size, preallocated",
              st.st_size == t0.buffer_bytes and st.st_blocks * 512 >= t0.buffer_bytes,
              f"size {st.st_size} blocks {st.st_blocks * 512} want {t0.buffer_bytes}")
        link = os.path.join(A.root, "t0", "ring.raw")
        check("tmpfs ring.raw is a symlink to the card file; ring.pos stays in tmpfs",
              os.path.islink(link) and os.path.realpath(link) == os.path.realpath(disk_file)
              and os.path.isfile(os.path.join(A.root, "t0", "ring.pos"))
              and not os.path.exists(os.path.join(card, "t0", "ring.pos")))
        check("RAM track unchanged: real file in tmpfs, nothing on the card",
              os.path.isfile(os.path.join(A.root, "t1", "ring.raw"))
              and not os.path.islink(os.path.join(A.root, "t1", "ring.raw"))
              and not os.path.exists(os.path.join(card, "t1")))
        check("a checkpoint entry exists from the moment it arms",
              ckpt(card) is not None and ckpt(card)["pos"] == 0)
        check("card tracks get a deep RT buffer (>= 40 s); RAM tracks >= 10 s (was 2.7)",
              t0.rbs[0].write_space / 4 / RATE >= 40
              and 10 <= t1.rbs[0].write_space / 4 / RATE < 40,
              f"card {t0.rbs[0].write_space / 4 / RATE:.1f}s ram {t1.rbs[0].write_space / 4 / RATE:.1f}s")

        A.run(200)                                   # ~4.3 s
        got = A.grab1(0, 3, [c0, c1])
        check("skipback.sh grabs a CARD track through the symlink, byte-exact",
              got is not None and got == ring_span(t0, 3))

        A.r._checkpoint_all()
        ck = ckpt(card)
        check("checkpoint records the head position + ring shape",
              ck["pos"] == t0.pos and ck["buffer_bytes"] == t0.buffer_bytes
              and ck["channels"] == 2 and ck["boot_id"] == "boot-A")

        # ---- recorder restart, same boot (tmpfs kept) -------------------
        pos_before = t0.pos
        A.run(20)
        exact = t0.pos
        A.r._teardown_track(t0, delete_files=False)
        B = Rig(card, tmp_root=A.root)
        t0b = B.arm(c0)
        check("restart (same boot): resumes from tmpfs ring.pos, exact",
              t0b.resumed_from == "tmpfs" and t0b.pos == exact)
        B.run(15)                                    # audio AFTER the last checkpoint
        B.r._teardown_track(t0b, delete_files=False)  # clean stop -> final ckpt

        # ---- clean reboot (tmpfs wiped), persist ------------------------
        before = ring_span(t0b, 3)
        pos_clean = t0b.pos
        shutil.rmtree(A.root)
        STATE["boot"] = "boot-B"
        C = Rig(card)
        t0c = C.arm(c0)
        check("reboot + persist: resumes from the card checkpoint at the exact head",
              t0c.resumed_from == "checkpoint" and t0c.pos == pos_clean,
              f"{t0c.resumed_from} {t0c.pos} vs {pos_clean}")
        after = C.grab1(0, 3, [c0])
        check("pre-reboot audio is still there, byte-exact", after == before)
        check("tmpfs symlink + ring.pos recreated after the reboot",
              os.path.islink(os.path.join(C.root, "t0", "ring.raw"))
              and open(os.path.join(C.root, "t0", "ring.pos")).read() == str(pos_clean))
        C.run(60)
        C.r._write_heads()
        h = json.load(open(os.path.join(C.root, SR.HEADS_NAME)))["tracks"]["0"]
        check("heads: aligned_bytes covers only audio since this start",
              h["aligned_bytes"] == t0c.aligned and 0 < h["aligned_bytes"] < h["fill_bytes"],
              str(h))
        r = subprocess.run([sys.executable, GRAB, "0", "--root", C.root,
                            "--outdir", os.path.join(base, "multi")],
                           capture_output=True, text=True, timeout=60)
        m = json.load(open(os.path.join(r.stdout.split("Saved:", 1)[1].strip(), "manifest.json"))) \
            if "Saved:" in r.stdout else {}
        check("GRAB ALL after a reboot reaches back only to the restart (aligned span)",
              0 < m.get("frames", 0) <= t0c.aligned // t0c.frame_bytes, str(m.get("frames")))

        # ---- power cut between checkpoints ------------------------------
        C.r._checkpoint_all()
        ck_pos = ckpt(card)["pos"]
        C.run(25)                                    # more audio, NO checkpoint
        check("(setup) head moved past the last checkpoint", t0c.pos != ck_pos)
        shutil.rmtree(C.root)                        # no clean stop at all
        STATE["boot"] = "boot-C"
        D = Rig(card)
        t0d = D.arm(c0)
        check("power cut: resumes at the last checkpoint (<= 1 s lost)",
              t0d.resumed_from == "checkpoint" and t0d.pos == ck_pos)
        # review 2026-10-01: the audio written after that checkpoint sits just
        # AFTER the resumed head (= a full-ring grab's first seconds); it is
        # cleared, since new recording overwrites exactly that spot first
        with open(t0d.ring_path, "rb") as f:
            rb = f.read()
        n = min(int(SR.RESUME_CLEAR_S * RATE) * t0d.frame_bytes, t0d.buffer_bytes // 4)
        n -= n % t0d.frame_bytes
        p0 = t0d.pos
        after_head = (rb[p0:p0 + n] if p0 + n <= len(rb)
                      else rb[p0:] + rb[:p0 + n - len(rb)])
        check("... and the post-checkpoint audio after the head is cleared to silence",
              len(after_head) == n and not any(after_head) and any(rb[:p0] + rb[p0 + n:]),
              f"n={n} nonzero={sum(1 for x in after_head if x)}")
        D.r._teardown_track(t0d, delete_files=False)

        # ---- fresh-on-boot ----------------------------------------------
        cf = cfg(0, 6, True, persist=False)
        shutil.rmtree(D.root)
        E = Rig(card)
        t0e = E.arm(cf)                              # same boot-C: keeps history
        check("persist:false, SAME boot (tmpfs lost): still resumes",
              t0e.resumed_from == "checkpoint")
        E.r._teardown_track(t0e, delete_files=False)
        shutil.rmtree(E.root)
        STATE["boot"] = "boot-D"
        F = Rig(card)
        t0f = F.arm(cf)
        with open(disk_file, "rb") as f:
            zero = not any(f.read())
        check("persist:false, NEW boot: starts fresh (pos 0, old audio gone)",
              t0f.resumed_from is None and t0f.pos == 0 and zero)

        # ---- guards -------------------------------------------------------
        F.r._cfg_tracks = [cfg(5, 7200, True), cfg(6, 600, True, store="ram")]
        F.r._blocked_logged = set()
        STATE.update(mounted=True, cap=2 << 30)
        F.r._apply_ram_guard()
        check("RAM budget ignores CARD rings (1.3 GB card + 110 MB RAM both arm)",
              F.r._blocked == set() and F.r._ram_used == SR.Recorder._ring_bytes(cfg(6, 600, True)),
              f"blocked={F.r._blocked} ram={F.r._ram_used}")
        STATE["cap"] = 1 << 30
        F.r._apply_ram_guard()
        check("CARD ring bigger than the card space: refused (RAM track still arms)",
              F.r._blocked == {5} and "card" in F.r._card_why.get(5, ""))
        STATE.update(mounted=False, cap=2 << 30)
        F.r._apply_ram_guard()
        check("card NOT mounted: CARD track refuses to arm, RAM track unaffected",
              F.r._blocked == {5} and "not mounted" in F.r._card_why.get(5, ""))
        STATE["mounted"] = True
        F.r._cfg_tracks = [cf]
        F.r._write_status()
        stj = json.load(open(F.r.status_path))
        check("status reports store / persist per track",
              stj["tracks"][0]["store"] == "card" and stj["tracks"][0]["persist"] is False
              and "card_used_mb" in stj)

        # ---- config + structure -------------------------------------------
        ok1, _ = SR._valid_track({"id": 3, "length_secs": 10, "store": "card"}, set())
        bad, why = SR._valid_track({"id": 3, "length_secs": 10, "store": "usb"}, set())
        ok2, _ = SR._valid_track({"id": 3, "length_secs": 10}, set())
        check("config: store/persist validated, defaults ram + persist",
              ok1["store"] == "card" and bad is None and ok2["store"] == "ram"
              and ok2["persist"] is True)
        check("switching RAM<->CARD is a structural change (ring rebuilt)",
              SR.Recorder._structural_change(cfg(0, 6, store="ram"), cfg(0, 6, store="card")))

        # ---- one combined file: entries don't clobber each other -----------
        t2 = F.arm(cfg(2, 5, False))
        F.run(10)
        F.r._checkpoint_tracks([t2])
        F.r._checkpoint_tracks([t0f])
        check("combined checkpoint: each track's entry survives the others' saves",
              ckpt(card, "t0") is not None and ckpt(card, "t2") is not None
              and ckpt(card, "t2")["pos"] == t2.pos)
        F.r._cfg_tracks = [cf, cfg(2, 5, False)]
        F.r._write_status()
        stj = json.load(open(F.r.status_path))
        check("status reports the slowest checkpoint + writer pass (ms)",
              isinstance(stj.get("ckpt_max_ms"), int) and isinstance(stj.get("writer_max_ms"), int)
              and stj["ckpt_max_ms"] > 0)
        F.r._teardown_track(t2, delete_files=True)

        # ---- disarm deletes everything -----------------------------------
        F.r._teardown_track(t0f, delete_files=True)
        check("disarm removes the card file, its checkpoint entry, symlink and dirs",
              not os.path.exists(os.path.join(card, "t0")) and ckpt(card) is None
              and ckpt(card, "t2") is None
              and not os.path.lexists(os.path.join(F.root, "t0", "ring.raw")))

        # ---- durability order (source) --------------------------------------
        src = inspect.getsource(SR.Recorder._checkpoint_tracks)
        check("checkpoint: fdatasync every ring BEFORE recording positions",
              0 <= src.find("os.fdatasync(fd)") < src.find("os.replace(tmp, path)"))
        locked, rest = src.split("with self._wlock:", 1)[1].split("synced = {}", 1)
        check("checkpoint: heads snapshotted under the writer lock, synced outside it",
              "os.dup(" in locked and "fdatasync" not in locked and "os.fdatasync(fd)" in rest)
        # ---- TWO WRITERS: a stalled card never holds up a RAM track ------
        g = test_card_stall_isolated(base)
        check("card write stalled 1.5 s: the RAM track kept draining in real time",
              g["ram_during"] > 1.2, str(g))
        check("... its ring.pos kept moving (sampler grabs stay on time)",
              g["pos_age"] < 0.3, f"ring.pos {g['pos_age']:.2f} s old")
        check("... heads.json shows it: RAM head fresh, CARD head stale",
              g["age_r"] < 0.3 and g["age_c"] > 1.0, str(g))
        check("the stalled card track wrote nothing meanwhile, then caught up",
              g["card_during"] == 0 and g["card_after"] > 0, str(g))
        check("no gaps on either track (the card's 43 s cushion held it)",
              g["gaps"] == (0, 0), str(g["gaps"]))
        g = test_card_stall_isolated(base, watchdog=True)
        check("watchdog during a card stall: RAM track still drained in real time",
              g["ram_during"] > 1.2, str(g))
        check("... the routine (nothing changed) publish took no writer lock",
              g["wd"].get("unchanged_s", 9) < 0.05, str(g["wd"]))
        check("... a real change waited the card out, then completed",
              g["wd"].get("changed_done") is True and g["gaps"] == (0, 0), str(g))
        run_src = inspect.getsource(SR.Recorder.run)
        check("the live recorder starts one writer per group (RAM + CARD)",
              "args=(False,)" in run_src and "args=(True,)" in run_src)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(_res)
    print(f"\n{passed} passed, {len(_res) - passed} failed")
    if len(_res) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_res)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if passed == len(_res) else 1


if __name__ == "__main__":
    sys.exit(main())
