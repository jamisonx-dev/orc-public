#!/usr/bin/env python3
"""
skipback_recorder.py -- multitrack skipback: ONE JACK client records N rings.

Production recorder (Phase 1, task P1.1 -- built as the P0.2 prototype,
hardware-verified in the P0.5/P0.6 soak) for the multitrack skipback
(_planning/ORC1_Multitrack_Skipback_Spec.md). Replaces the
alsa_out -> ALSA Loopback -> arecord -> python chain (skipback_bridge.sh +
skipback_daemon.sh) with a single python-jack client ("skipback-rec") that
registers input ports per armed track and writes each track's audio straight
into a per-track ring file:

    <root>/t<id>/ring.raw   S16_LE interleaved raw @ 48 kHz  (contract C1 --
    <root>/t<id>/ring.pos   byte-identical to skipback_daemon.sh's format)

RingBufferReader (orc1_sampler.py) and skipback.sh read these unchanged --
only the path and channels/length params differ per track.

Thread model:
  JACK process callback -- RT (contract C7): does NOTHING but memcpy each
                           port's period buffer into that port's preallocated
                           jack.RingBuffer. No allocation, no numpy, no file
                           I/O, no locks, no logging, no exception escapes.
  writer thread         -- every ~42 ms drains the RingBuffers, converts
                           float32 -> S16_LE interleaved, appends into the
                           ring file with wraparound (exact daemon math),
                           then flushes ring.pos via tmp+os.replace. The pos
                           file's mtime is the readers' timestamp anchor, so
                           it is always written LAST. After each pass it
                           also publishes <root>/heads.json: every head's
                           pos + JACK frame (from the per-period STAMPs),
                           for sample-aligned multitrack grabs
                           (skipback_grab_multi.py).
  TWO writer threads (2026-09-29): one drains the RAM tracks, one the CARD
                           tracks, each under its own lock. MEASURED the day
                           it changed: with ONE writer for both, an SD-card
                           stall (7 s under heavy card I/O) blocked the RAM
                           tracks' drains too, their 2.7 s RT buffers
                           overflowed (15 gaps each), and their ring.pos froze
                           -- which also made sampler grabs land early. Now a
                           card stall can only delay card tracks (43 s
                           cushion). Each drain publishes its head as ONE
                           tuple (Track.head) so heads.json never needs the
                           other writer's lock.
  main/watchdog thread  -- every ~5 s: reload the (hand-editable) tracks
                           config on mtime change, validate it, reconcile
                           ports for armed/disarmed tracks, resolve + wire
                           sources idempotently, enforce the RAM guard (C4),
                           and publish the status JSON (C3) atomically.

Config (contract C2, hand-editable, recorder is read-only on it):
  {"tracks":[{"id":0,"name":"MAIN","source":{"mode":"auto"},
              "stereo":true,"length_secs":1200,"armed":true},
             {"id":1,"name":"SYNTH","source":{"mode":"manual",
              "left":"ORC1-Synth:out_L","right":"ORC1-Synth:out_R"},
              "stereo":true,"length_secs":900,"armed":true}]}
  Mono track: "stereo":false, source needs only "left" (one port, no summing).
  Optional per-track "dir": directory name under --root (default "t<id>") --
  lets the Phase-0 proto write proto_t0/proto_t1 beside the untouched live
  ring without touching it.

Phase-0 proto invocation (alongside the live pipeline):
  python3 skipback_recorder.py \
      --config ~/.orc1_skipback_tracks_proto.json \
      --root   ~/.skipbuf \
      --status ~/.orc1_skipback_status_proto.json

jackd is NEVER autostarted (single-owner discipline -- orc1-jackd.service
owns the server). Server down at START: retry every few seconds, and after
FRESH_PROCESS_AFTER_SECS exit so systemd starts a fresh process. Server lost
MID-RUN: keep the ring files and EXIT (code EXIT_RECONNECT) -- systemd
Restart=on-failure respawns a fresh process that resumes every ring. An
in-process reconnect is NOT attempted: measured 2026-09-23, it wedges forever on
libjack's stale shm mapping ("Cannot open shm segment") while systemd says
"active". The unit is also BindsTo=orc1-jackd (drop-in 50-jackd-bind.conf).
SKIPBACK COMES FIRST (owner, 2026-09-23): nothing in here may stop or refuse
the recorder to protect another process -- see _apply_ram_guard.
An existing ring whose size matches config is KEPT on start (write head
resumed from ring.pos, so history survives a daemon restart); missing or
size-mismatched rings are re-created and pos reset to 0.

Phase-1 production behavior:
  - first run with no tracks config: synthesized from the legacy
    ~/.orc1_skipback_src.json (migrate_legacy_config -- track 0 MAIN,
    1860s stereo, armed). Idempotent.
  - a MISSING or UNPARSEABLE tracks config never deletes rings: the
    last-known-good config stays in force (review item 13).
  - while ~/.orc1_skipback_src.json exists it overrides track 0's SOURCE
    (C5 shim -- the /skipback page's source picker keeps working through
    Phase 1 without touching orc1_web.py).
  - legacy symlinks <root>/ring.{raw,pos} -> t0/ recreated on every start.
"""

import argparse
import json
import os
import signal
import struct
import threading
import time

# Single-owner discipline, belt + braces: the systemd unit sets this too, but
# make sure no code path can ever autostart a jackd.
os.environ.setdefault("JACK_NO_START_SERVER", "1")

import numpy as np
import jack

# ── constants ────────────────────────────────────────────────────────────────

RATE            = 48000          # the whole rig is pinned at 48 kHz
SAMPLE_BYTES    = 2              # S16_LE in the ring files
CLIENT_NAME     = "skipback-rec"
RESUME_CLEAR_S = 15             # cleared after a checkpoint-resumed head (> worst sync stall)
MAX_GAP_FILL_S = 120            # a dropped stretch up to this long is filled with silence
WRITER_INTERVAL = 0.042          # ~42 ms drain cadence == daemon's ~43 ms chunk
WATCHDOG_SECS   = 5.0            # same cadence the old bridge watchdog used
FRESH_PROCESS_AFTER_SECS = 60.0  # no jackd this long -> exit for a fresh process
EXIT_RECONNECT  = 75             # nonzero: systemd Restart=on-failure respawns us
JACK_RETRY_SECS = 3.0            # reconnect poll while jackd is down
RAM_BUDGET      = 900 * 1024 * 1024   # C4: sum of armed ring bytes <= 900 MB
FREE_MIN        = 400 * 1024 * 1024   # C4: projected MemAvailable floor
DRAIN_FAIL_LIMIT = 25                 # consecutive drain failures (~1s at the
                                      # 42ms cadence) before a track reports
                                      # recording:false in status (review 18)
LEGACY_SRC_JSON = "~/.orc1_skipback_src.json"   # single-track era source cfg

# Per-channel RT ringbuffer for RAM tracks: 2^21 bytes of float32 mono @48k =
# ~10.9 s of audio. It was 2^19 (~2.7 s) until 2026-09-29: "RAM never stalls"
# turned out false -- after the RAM/CARD writer split, a RAM writer pass still
# took 2.6 s once under memory pressure (Mixxx + a library scan), 0.1 s from
# dropping audio. SKIPBACK COMES FIRST: ~2 MB per channel buys the margin.
RB_BYTES = 1 << 21
# CARD tracks get a far deeper per-channel RT buffer: ~43.7 s of float32 mono.
# MEASURED 2026-09-28 (65-min 14-track soak on the PRO Endurance): SD cards
# pause for SECONDS while they reorganise internally -- one stall lasted >2.7 s
# and dropped ~0.3 s on all 14 tracks; a checkpoint went 7.8 s stale. RAM never
# does this, so RAM tracks keep the small buffer. 14 card channels = ~112 MB,
# a fraction of the ~800 MB their rings no longer take from RAM.
RB_BYTES_CARD = 1 << 23

# Sample-aligned multitrack grabs (2026-09-28). Each track drains on its own,
# so at any instant two heads can sit a period (or more) apart and ring.pos
# alone can't line tracks up. Every period the RT callback pushes one STAMP
# (JACK frame time of the period's first frame, frame count) into a per-track
# ringbuffer, AFTER that period's audio. The writer drains whole stamped
# periods, so each head knows its exact JACK frame; once per pass it publishes
# every head as one snapshot, HEADS_NAME (<root>/heads.json), which
# skipback_grab_multi.py cuts all tracks against. Sized for >= the audio
# ringbuffer's periods at a 16-frame period.
STAMP = struct.Struct("<II")
STAMP_RB_BYTES = 1 << 18         # periods covering RB_BYTES at 16 frames
STAMP_RB_BYTES_CARD = 1 << 19     # periods covering RB_BYTES_CARD at 16 frames
HEADS_NAME = "heads.json"

# CARD tracks (2026-09-28): "store": "card" puts a track's ring.raw on a
# dedicated card partition instead of tmpfs RAM. The tmpfs track dir keeps
# ring.pos (rewritten ~24x/s -- must never churn the flash) plus a ring.raw
# SYMLINK to the card file, so every reader (skipback.sh, the sampler's
# RingBufferReader, grab_multi) keeps opening <root>/<dir>/ring.raw unchanged.
# A checkpoint thread fdatasyncs each card ring every CKPT_SECS and THEN
# records every position in ONE combined file on the card (CKPT_NAME), so a
# reboot / power cut resumes the history ("persist": true, the default),
# losing only the last second or so.
# "persist": false = start fresh on each new boot. A card track REFUSES to
# arm while DISK_ROOT isn't a mountpoint: writing there would silently fill
# the root filesystem. RAM tracks are unaffected either way.
DISK_ROOT     = "/mnt/skipring"
CKPT_SECS     = 1.0
CKPT_NAME     = "checkpoint.json"   # ONE file for every card track (not 14:
                                    # per-track files meant 14 create+fsync+
                                    # rename per second of journal churn)
DISK_RESERVE  = 64 * 1024 * 1024      # kept free on the card partition
STORES        = ("ram", "card")


def _disk_mounted(path):
    """Is the card partition actually mounted? (Module-level so tests can
    stand a temp dir in for it.)"""
    return os.path.ismount(path)


def _disk_capacity(path):
    """Usable bytes on the card partition for rings (whole filesystem minus
    a reserve -- the rings are the only thing meant to live there)."""
    st = os.statvfs(path)
    return max(0, st.f_blocks * st.f_frsize - DISK_RESERVE)


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        return ""

# Auto-source resolver chain, ported from skipback_bridge.sh resolve_src():
# Zoom L12 master when the L12 is the active card (its capture_13/14 exist),
# else pisound in, else the card's first stereo input (last entry = the
# unconditional fallback).
#
# ⚠ 2026-09-15: the last entry USED to be ("ORC1-Synth:out_L","ORC1-Synth:out_R")
# -- a "synth direct" tap. The audio-host split (2026-09-02) RETIRED that client,
# so the fallback named a port that no longer exists: on any card that is neither
# the L12 nor a pisound, auto-source resolved to a dead client and the recorder
# could never connect (records silence, retries forever). There is no single
# summed "synth" bus post-split to tap, so the honest card-agnostic last resort
# is the fitted card's first stereo INPUT (system:capture_1/2) -- whatever the
# user has patched to return there. (health flagged this as DEAD; it was never
# reached on this rig because the L12 is always present, but it was a real trap.)
AUTO_PAIRS = [
    ("system:capture_13", "system:capture_14"),
    ("pisound-in:capture_1", "pisound-in:capture_2"),
    ("system:capture_1", "system:capture_2"),
]


def log(msg):
    print(f"[skipback-rec] {msg}", flush=True)


# ── config loading (contract C2 -- the file is untrusted, hand-editable) ─────

def _valid_track(t, seen_ids):
    """Validate one raw track entry. Returns (cfg_dict, None) or (None, why)."""
    if not isinstance(t, dict):
        return None, "not an object"
    tid = t.get("id")
    if not isinstance(tid, int) or isinstance(tid, bool) or not 0 <= tid <= 63:
        return None, "bad id (int 0-63 required)"
    if tid in seen_ids:
        return None, f"duplicate id {tid}"
    stereo = t.get("stereo", True)
    if not isinstance(stereo, bool):
        return None, "stereo must be true/false"
    length = t.get("length_secs")
    if (not isinstance(length, (int, float)) or isinstance(length, bool)
            or not 5 <= length <= 7200):
        return None, "length_secs out of range (5-7200)"
    armed = t.get("armed", True)
    if not isinstance(armed, bool):
        return None, "armed must be true/false"
    name = t.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"T{tid}"
    src = t.get("source") or {}
    if not isinstance(src, dict):
        return None, "source must be an object"
    left = src.get("left") or ""
    right = src.get("right") or ""
    # Legacy {"left":..,"right":..} with no mode == manual (bridge back-compat)
    mode = src.get("mode") or ("manual" if left else "auto")
    if mode not in ("auto", "manual"):
        return None, f"bad source mode {mode!r}"
    if mode == "manual":
        if not isinstance(left, str) or not left:
            return None, "manual source needs 'left'"
        # DEVIATION from skipback_bridge.sh (documented, P0.4 review item 10):
        # the bridge silently fell back to AUTO when a manual config was
        # incomplete; here an incomplete stereo-manual track is REJECTED
        # (skipped + logged) instead -- a hand-edited tracks file should fail
        # loudly per-track, not silently record something else. (Migration
        # keeps the bridge semantics when translating src.json: incomplete
        # manual there becomes {"mode":"auto"}.)
        if stereo and (not isinstance(right, str) or not right):
            return None, "stereo manual source needs 'right'"
    # Optional dir override (Phase-0 proto uses proto_t0/proto_t1). Plain
    # directory name only -- never a path that could escape the root.
    d = t.get("dir") or f"t{tid}"
    if (not isinstance(d, str) or not d or "/" in d or "\\" in d
            or d in (".", "..") or d.startswith(".")):
        return None, f"bad dir {d!r} (plain directory name only)"
    store = t.get("store", "ram")
    if store not in STORES:
        return None, f"store must be one of {STORES}"
    persist = t.get("persist", True)
    if not isinstance(persist, bool):
        return None, "persist must be true/false"
    return {"id": tid, "name": name.strip(), "stereo": stereo,
            "length_secs": int(length), "armed": armed,
            "mode": mode, "left": left, "right": right, "dir": d,
            "store": store, "persist": persist}, None


def _load_tracks(path):
    """Read + validate the tracks JSON. Returns (tracks, ok).

    ok=False means the file was missing, unreadable, not JSON, or not the
    C2 shape ({"tracks": [...]}) -- i.e. we did NOT get a successfully
    parsed config. Callers must then KEEP the last-known-good config:
    tearing tracks down (and deleting their rings) on a failed read is the
    data-loss bug from P0.4 review item 13. ok=True with individual invalid
    tracks skipped-and-logged is still a successful parse -- the file is
    hand-editable (cross-cutting trap #8)."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception as e:
        log(f"config {path} unreadable ({e}) -- keeping last-known-good")
        return [], False
    items = raw.get("tracks") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        log("config has no 'tracks' list -- keeping last-known-good")
        return [], False
    out, seen = [], set()
    for i, t in enumerate(items):
        cfg, why = _valid_track(t, seen)
        if cfg is None:
            log(f"config: skipping tracks[{i}]: {why}")
            continue
        seen.add(cfg["id"])
        out.append(cfg)
    return out, True


def _parse_legacy_src(path):
    """Parse the single-track era ~/.orc1_skipback_src.json into a C2 source
    dict, with skipback_bridge.sh's exact semantics: explicit manual needs
    BOTH ports, legacy {"left","right"} (no mode) = manual, anything else
    (incl. missing/unreadable file) = {"mode":"auto"}. Returns None only when
    the file exists but cannot be read as JSON -- callers decide whether that
    matters (migration treats it as auto)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"mode": "auto"}
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    mode = d.get("mode")
    left = d.get("left") or ""
    right = d.get("right") or ""
    if not isinstance(left, str) or not isinstance(right, str):
        left, right = "", ""
    if (mode == "manual" or mode is None) and left and right:
        return {"mode": "manual", "left": left, "right": right}
    return {"mode": "auto"}


def migrate_legacy_config(config_path, src_path):
    """First-run migration (spec §Config): no tracks file -> synthesize it
    from today's single-source config. Track 0: MAIN, today's source (auto
    stays auto; legacy {left,right} -> manual), stereo, 1860s (legacy length
    for byte-identical behavior -- the 1200s new-install default arrives in
    Phase 2), armed, dir t0 (implicit). Written atomically. Idempotent: an
    existing tracks file (even an unreadable one -- never clobber) = no-op.
    Returns True when a file was written."""
    if os.path.exists(config_path):
        return False
    src = _parse_legacy_src(src_path) or {"mode": "auto"}
    cfg = {"tracks": [{"id": 0, "name": "MAIN", "source": src,
                       "stereo": True, "length_secs": 1860, "armed": True}]}
    tmp = config_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path)
    log(f"migrated legacy config: {src_path} -> {config_path} "
        f"(track 0 MAIN, source {src}, stereo, 1860s, armed)")
    return True


def _mem_available_bytes():
    """MemAvailable from /proc/meminfo, in bytes; None when unreadable
    (non-Linux dev box, weird kernel) -- caller falls back to budget-only."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _ring_resident_bytes(ring_path):
    """Bytes of a ring file already resident in RAM (tmpfs allocates pages
    only as they are written -- st_blocks tracks that). 0 if absent."""
    try:
        return os.stat(ring_path).st_blocks * 512
    except OSError:
        return 0


# ── one track: ring file + write head + RT queues ────────────────────────────

def _signed32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x >= (1 << 31) else x


class Track:
    """A single ring: its config, its ring/pos files, its JACK input ports and
    the per-port RT ringbuffers. Files are touched by the writer + watchdog
    threads only -- never by the process callback."""

    def __init__(self, cfg, root, disk_root=DISK_ROOT):
        self.cfg = cfg
        self.id = cfg["id"]
        self.channels = 2 if cfg["stereo"] else 1
        self.frame_bytes = self.channels * SAMPLE_BYTES
        self.bytes_per_sec = RATE * self.frame_bytes
        self.buffer_bytes = cfg["length_secs"] * self.bytes_per_sec
        self.dir = os.path.join(root, cfg["dir"])
        self.pos_path = os.path.join(self.dir, "ring.pos")
        self.card = cfg.get("store", "ram") == "card"
        self.persist = cfg.get("persist", True)
        if self.card:
            self.card_dir = os.path.join(disk_root, cfg["dir"])
            self.ring_path = os.path.join(self.card_dir, "ring.raw")
            self.link_path = os.path.join(self.dir, "ring.raw")
            self.ckpt_path = os.path.join(disk_root, CKPT_NAME)
        else:
            self.card_dir = self.link_path = self.ckpt_path = None
            self.ring_path = os.path.join(self.dir, "ring.raw")
        self.aligned = 0            # bytes written since THIS process armed it
        self.resumed_from = None    # "tmpfs" | "checkpoint" | None (fresh)
        self.ring = None            # open file handle once armed
        self.pos = 0                # current write offset (bytes)
        self.written = 0            # bytes written (fill estimate, capped)
        self.ports = []             # jack OwnPorts, registered by Recorder
        self.rbs = []               # one jack.RingBuffer per port
        self.stamp_rb = None        # jack.RingBuffer of STAMPs, one per period
        self.head_ft = None         # JACK frame time (mod 2^32) at the head
        # (pos, frame, written, aligned, gaps, wall time) set as ONE tuple at
        # the end of every drain, so heads.json reads a consistent head
        # without taking this track's writer lock (atomic under the GIL).
        self.head = None
        self.gaps = 0               # stamped periods that didn't follow on
        self.resolved = []          # last-resolved source port names
        self.connected = False
        # Drain-failure accounting (P0.4 review item 18): last exception
        # string + total count survive recovery (visible in status); the
        # consecutive-failure streak drives recording:false + log throttling.
        self.drain_err = None       # str(last exception), None = never failed
        self.drain_err_count = 0    # total failed drain passes
        self._drain_streak = 0      # CONSECUTIVE failures (reset on success)
        self._drain_dead_logged = False

    def port_names(self):
        if self.channels == 2:
            return [f"t{self.id}_L", f"t{self.id}_R"]
        return [f"t{self.id}_M"]

    def init_ring(self):
        """Create or adopt the ring file (contract C1). A ring whose size
        matches config is KEPT and the write head resumed from ring.pos --
        history survives a daemon restart. Anything else = fresh ring, pos 0
        (same as skipback_daemon.sh's size-check re-init)."""
        if self.card:
            return self._init_card_ring()
        os.makedirs(self.dir, exist_ok=True)
        keep = False
        try:
            keep = os.path.getsize(self.ring_path) == self.buffer_bytes
        except OSError:
            pass
        if keep:
            try:
                with open(self.pos_path) as f:
                    pos = int(f.read().strip())
            except Exception:
                pos = 0
            pos %= self.buffer_bytes
            pos -= pos % self.frame_bytes       # keep the head frame-aligned
            self.pos = pos
            self.written = pos                  # conservative fill estimate
            log(f"t{self.id}: kept existing ring "
                f"({self.cfg['length_secs']}s x {self.channels}ch), "
                f"resumed head at byte {pos}")
        else:
            # truncate() preallocates the full logical size; on tmpfs the
            # file is sparse, so RAM is only consumed as audio is written
            # (exactly like the daemon's python init).
            with open(self.ring_path, "wb") as f:
                f.truncate(self.buffer_bytes)
            self.pos = 0
            self.written = 0
            log(f"t{self.id}: new ring {self.buffer_bytes} bytes "
                f"({self.cfg['length_secs']}s x {self.channels}ch) "
                f"at {self.ring_path}")
        self.ring = open(self.ring_path, "r+b")
        self._flush_pos()

    def _clear_after_head(self):
        """After a CHECKPOINT resume (a reboot), the audio written after the
        last checkpoint (up to the 1 s interval + the sync time, measured up
        to ~10 s while the card stalls) sits right AFTER the resumed head, so
        a full-ring grab would open with it: the newest pre-reboot audio
        first, then the oldest (review 2026-10-01). New recording overwrites
        exactly that spot first anyway, so it is cleared to silence now."""
        n = min(int(RESUME_CLEAR_S * RATE) * self.frame_bytes,
                self.buffer_bytes // 4)
        n -= n % self.frame_bytes
        zero = bytes(min(n, 1 << 20))
        try:
            with open(self.ring_path, "r+b") as f:
                off, left = self.pos, n
                while left > 0:
                    k = min(left, len(zero), self.buffer_bytes - off)
                    f.seek(off)
                    f.write(zero[:k])
                    left -= k
                    off = (off + k) % self.buffer_bytes
                f.flush()
                os.fdatasync(f.fileno())
        except OSError as e:
            log(f"t{self.id}: clearing after the resumed head failed: {e}")

    def _read_ckpt(self):
        """This track's entry in the card's combined checkpoint file, if it
        matches the ring's shape."""
        try:
            with open(self.ckpt_path) as f:
                c = json.load(f).get("tracks", {}).get(self.cfg["dir"])
            if (isinstance(c, dict) and c.get("buffer_bytes") == self.buffer_bytes
                    and c.get("channels") == self.channels):
                return c
        except Exception:
            pass
        return None

    def _init_card_ring(self):
        """CARD track: ring.raw on the card partition, ring.pos + a ring.raw
        symlink in the tmpfs track dir. Head resume order: the tmpfs ring.pos
        (same boot -- a recorder restart, exact) > the card checkpoint (after a
        reboot, when "persist") > fresh. The file is preallocated at full
        size, so it can never run out of space mid-recording."""
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.card_dir, exist_ok=True)
        keep = False
        try:
            keep = os.path.getsize(self.ring_path) == self.buffer_bytes
        except OSError:
            pass
        pos = written = None
        if keep:
            try:
                with open(self.pos_path) as f:
                    pos = int(f.read().strip())
                self.resumed_from = "tmpfs"
            except Exception:
                c = self._read_ckpt()
                if c is not None and (self.persist or c.get("boot_id") == _boot_id()):
                    pos, written = int(c["pos"]), int(c.get("written", 0))
                    self.resumed_from = "checkpoint"
        if pos is None:
            try:
                os.remove(self.ring_path)
            except OSError:
                pass
            fd = os.open(self.ring_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                os.posix_fallocate(fd, 0, self.buffer_bytes)
            finally:
                os.close(fd)
            self.pos, self.written, self.resumed_from = 0, 0, None
            log(f"t{self.id}: new CARD ring {self.buffer_bytes} bytes "
                f"({self.cfg['length_secs']}s x {self.channels}ch) at "
                f"{self.ring_path}")
        else:
            pos %= self.buffer_bytes
            pos -= pos % self.frame_bytes
            self.pos = pos
            self.written = pos if written is None else min(written, self.buffer_bytes)
            if self.resumed_from == "checkpoint":
                self._clear_after_head()
            log(f"t{self.id}: kept CARD ring, resumed head at byte {pos} "
                f"(from {self.resumed_from})")
        # the symlink every reader opens (replace a stale link / old RAM file)
        try:
            if os.path.islink(self.link_path):
                if os.readlink(self.link_path) != self.ring_path:
                    os.remove(self.link_path)
            elif os.path.exists(self.link_path):
                os.remove(self.link_path)
            if not os.path.islink(self.link_path):
                os.symlink(self.ring_path, self.link_path)
        except OSError as e:
            log(f"t{self.id}: ring.raw symlink failed: {e}")
        self.ring = open(self.ring_path, "r+b")
        self._flush_pos()
        # (the Recorder records this track's first checkpoint right after
        #  bring-up -- the combined file is the Recorder's to write)

    def ckpt_entry(self, pos, written):
        return {"pos": pos, "written": min(written, self.buffer_bytes),
                "buffer_bytes": self.buffer_bytes, "channels": self.channels,
                "boot_id": _boot_id(), "ts": time.time()}

    def drain(self):
        """WRITER THREAD ONLY. Move whatever the RT callback queued into the
        ring file. Chunk mechanics are lifted from skipback_daemon.sh's inline
        python verbatim: two-branch wraparound write, pos = end % BUFFER_BYTES,
        and ring.pos flushed via tmp + os.replace after EVERY chunk (its mtime
        is the timestamp anchor readers use -- see orc1_sampler.head_info)."""
        if self.ring is None or not self.rbs:
            return
        fills, unaligned = [], False            # (at frame, silent frames)
        if self.stamp_rb is None:               # unstamped (no aligned grabs)
            n = min(rb.read_space for rb in self.rbs)
            n -= n % 4                          # whole float32 samples only
            if n == 0:
                return
            head_ft = self.head_ft
        else:
            # Drain exactly the STAMPED periods: their audio is already in the
            # port ringbuffers (the callback writes audio first, stamp last),
            # and any period queued after we read the stamps waits for the
            # next pass -- so the head always sits on a known JACK frame.
            #
            # A dropped stretch (an xrun, or the RT buffer overflowing behind
            # a stalled writer) is written as SILENCE of its exact length, so
            # ring position stays tied to JACK frame time and every track
            # still lines up across the drop (review 2026-10-01: before, the
            # older audio slid by the whole dropped time and GRAB ALL could
            # not tell). A gap that can't be filled (absurd size / frames
            # going backwards) instead restarts the aligned span.
            k = self.stamp_rb.read_space // STAMP.size
            if k == 0:
                return
            head_ft, frames = self.head_ft, 0
            for ft, nf in STAMP.iter_unpack(self.stamp_rb.read(k * STAMP.size)):
                if head_ft is not None and ft != head_ft:
                    self.gaps += 1              # dropped period(s) before this
                    g = _signed32(ft - head_ft)
                    if 0 < g <= MAX_GAP_FILL_S * RATE:
                        fills.append((frames, g))
                    else:
                        unaligned = True
                head_ft = (ft + nf) & 0xFFFFFFFF
                frames += nf
            n = frames * 4
        chans = [np.frombuffer(rb.read(n), dtype=np.float32)
                 for rb in self.rbs]
        if self.channels == 2:
            pcm = np.empty(chans[0].size * 2, dtype="<i2")
            pcm[0::2] = (np.clip(chans[0], -1.0, 1.0) * 32767.0).astype("<i2")
            pcm[1::2] = (np.clip(chans[1], -1.0, 1.0) * 32767.0).astype("<i2")
        else:
            pcm = (np.clip(chans[0], -1.0, 1.0) * 32767.0).astype("<i2")
        if fills:
            ch, parts, prev = self.channels, [], 0
            for at, g in fills:
                parts.append(pcm[prev * ch:at * ch])
                parts.append(np.zeros(g * ch, dtype="<i2"))
                prev = at
            parts.append(pcm[prev * ch:])
            pcm = np.concatenate(parts)
        if unaligned:
            self.aligned = 0                    # older audio no longer lines up
        data = pcm.tobytes()
        if len(data) > self.buffer_bytes:       # unreachable at sane lengths
            data = data[-self.buffer_bytes:]
        end = self.pos + len(data)
        if end <= self.buffer_bytes:
            self.ring.seek(self.pos)
            self.ring.write(data)
        else:
            first = self.buffer_bytes - self.pos
            self.ring.seek(self.pos)
            self.ring.write(data[:first])
            self.ring.seek(0)
            self.ring.write(data[first:])
        self.pos = end % self.buffer_bytes
        self.written += len(data)
        self.aligned += len(data)
        self.ring.flush()       # audio must land before the head marker moves
        self._flush_pos()       # LAST: pos content + mtime anchor (contract C1)
        self.head_ft = head_ft
        self.head = (self.pos, head_ft, self.written, self.aligned, self.gaps,
                     time.time())

    def _flush_pos(self):
        # Byte-identical semantics to the daemon: plain text offset, no
        # newline, written to <pos>.tmp then os.replace'd (atomic on tmpfs).
        tmp = self.pos_path + ".tmp"
        with open(tmp, "w") as pf:
            pf.write(str(self.pos))
        os.replace(tmp, self.pos_path)

    def close(self, delete_files=False):
        """Flush + close; optionally delete the ring files (disarm reclaims
        tmpfs RAM -- spec: disarmed tracks free their ring, config kept)."""
        if self.ring is not None:
            try:
                self.ring.flush()
                self.ring.close()
            except Exception:
                pass
            self.ring = None
        if delete_files:
            paths = [self.ring_path, self.pos_path, self.pos_path + ".tmp"]
            if self.card:
                paths += [self.link_path,
                          os.path.join(self.card_dir, "ring.ckpt")]  # pre-v2 file
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            for d in ([self.dir] + ([self.card_dir] if self.card else [])):
                try:
                    os.rmdir(d)
                except OSError:
                    pass                # dir not empty / never created: fine


# ── the recorder ─────────────────────────────────────────────────────────────

class Recorder:

    def __init__(self, config_path, root, status_path, src_path=None,
                 disk_root=DISK_ROOT):
        self.config_path = config_path
        self.root = root
        self.disk_root = disk_root
        self._disk_used = 0
        self._card_why = {}             # id -> why a CARD track isn't armed
        self._ckpt_err_logged = False
        self._ckpt_lock = threading.Lock()   # the combined checkpoint file
        self._ckpt_ms_max = 0.0              # slowest checkpoint (card sync)
        self._writer_ms_max = 0.0            # slowest writer pass (either)
        self._writer_ms = {False: 0.0, True: 0.0}   # slowest pass: RAM / CARD
        self.status_path = status_path
        self.src_path = (src_path if src_path is not None
                         else os.path.expanduser(LEGACY_SRC_JSON))
        self.client = None
        self.tracks = {}                # id -> Track; watchdog thread only
        self._cfg_tracks = []           # last-known-GOOD validated config
        self._cfg_ever_loaded = False   # a config has parsed OK at least once
        self._cfg_mtime = None
        self._cfg_missing_logged = False
        self._cfg_bad_logged = False
        self._src_mtime = 0             # legacy src.json follow (C5 shim)
        self._src_override = None
        self._meminfo_warned = False
        self._blocked = set()           # ids refused by the RAM guard
        self._blocked_logged = set()
        self._ram_used = 0
        # RT handoff: the callback reads this tuple by reference; the watchdog
        # swaps in a whole new tuple (atomic under the GIL). No locks in RT.
        self._rt_groups = ()            # ((ports, ringbuffers, stamp_rb), ...)
        self._heads_err_logged = False
        self._writer_tracks = ()        # tracks the writer threads drain
        # One lock per writer: False = RAM tracks, True = CARD tracks. A pass
        # holds only its own; _publish_rt waits each one out in turn, never
        # holding both (a card stall must not freeze the RAM drains).
        # _wlock stays the CARD lock: the checkpoint snapshot takes it.
        self._wlocks = {False: threading.Lock(), True: threading.Lock()}
        self._wlock = self._wlocks[True]
        self._heads_lock = threading.Lock()  # the heads.json file write
        self._stop = threading.Event()
        self._jack_down = threading.Event()
        self._rt_trouble = False        # unexpected exception in the callback
        self._rt_dropped = False        # RT ringbuffer overflowed (writer stall)
        self._xruns = 0

    def request_stop(self):
        self._stop.set()

    # ── RT callback (contract C7) ────────────────────────────────────────────

    def _process(self, frames):
        # C7: NOTHING here but a memcpy of each port's period into its
        # preallocated jack.RingBuffer. port.get_buffer() is a CFFI memoryview
        # of JACK's own buffer (no audio copy to make it); rb.write() memcpys
        # those bytes into the preallocated ring. On overflow we drop the
        # WHOLE period (write_space check) rather than let rb.write() store a
        # partial -- jack ringbuffers are byte-granular, so a partial write
        # can strand a torn float32 and permanently misalign the stream
        # (P0.4 review item 21a). Whole periods are frame-aligned, and L/R
        # ringbuffers have identical fill histories so they drop together
        # (no channel skew). No numpy, no file I/O, no locks, no logging,
        # and no exception can escape -- the bare except stores a flag
        # (plain attribute write, RT-safe) that the watchdog thread logs.
        #
        # Per TRACK (2026-09-28): all of a track's ports and its STAMP go in
        # together or not at all, and the stamp is written LAST, so a stamp
        # the writer can see always has its audio queued ahead of it.
        try:
            stamp = STAMP.pack(self.client.last_frame_time & 0xFFFFFFFF,
                               frames)
            for ports, rbs, srb in self._rt_groups:
                bufs = [p.get_buffer() for p in ports]
                ok = srb is None or srb.write_space >= STAMP.size
                for i in range(len(rbs)):
                    if rbs[i].write_space < len(bufs[i]):
                        ok = False
                if not ok:
                    self._rt_dropped = True
                    continue
                for i in range(len(rbs)):
                    rbs[i].write(bufs[i])
                if srb is not None:
                    srb.write(stamp)
        except Exception:
            self._rt_trouble = True

    def _on_xrun(self, delayed_usecs):
        # Not the process callback (JACK notification context) -- a counter
        # increment is fine. Surfaced in the status JSON for the P0.5 soak.
        self._xruns += 1

    def _on_shutdown(self, status, reason):
        self._jack_down.set()

    # ── writer thread ────────────────────────────────────────────────────────

    def _writer_loop(self, card=None):
        """One writer thread. card=False drains the RAM tracks, card=True the
        CARD tracks (run() starts one of each, so a stalled SD card never
        holds up a RAM drain). card=None drains every track under both locks
        (the single-writer behaviour; kept for callers that want it)."""
        while not self._stop.is_set():
            t0 = time.monotonic()
            if card is None:
                with self._wlocks[False], self._wlocks[True]:
                    for trk in self._writer_tracks:
                        self._drain_once(trk)
                    self._write_heads()
            else:
                with self._wlocks[card]:
                    for trk in self._writer_tracks:
                        if trk.card == card:
                            self._drain_once(trk)
                    self._write_heads()
            spent = time.monotonic() - t0
            if spent * 1000.0 > self._writer_ms_max:
                self._writer_ms_max = spent * 1000.0
            if card is not None and spent * 1000.0 > self._writer_ms[card]:
                self._writer_ms[card] = spent * 1000.0
            self._stop.wait(max(0.005, WRITER_INTERVAL - spent))

    def _write_heads(self):
        """WRITER THREAD(S), after a drain pass: every head as ONE snapshot
        (<root>/heads.json). Each track's pos + frame come from its own
        published head tuple (set atomically at the end of its drain), so a
        multitrack grab can cut all tracks at one JACK frame, and neither
        writer needs the other's lock. Per-track "t" = when that head was
        true: a CARD track stalled by the SD card shows an old "t" while the
        RAM tracks stay fresh, and GRAB ALL waits on it. Kept in the root
        (tmpfs) beside the rings; the per-track ring.pos files are untouched
        and remain the single-track readers' contract."""
        heads = {}
        for trk in self._writer_tracks:
            h = trk.head
            if trk.ring is None or h is None or h[1] is None:
                continue
            pos, frame, written, aligned, gaps, t = h
            heads[str(trk.id)] = {
                "pos": pos, "frame": frame, "t": t,
                "channels": trk.channels, "buffer_bytes": trk.buffer_bytes,
                "fill_bytes": min(written, trk.buffer_bytes),
                "dir": trk.cfg["dir"], "name": trk.cfg["name"],
                "gaps": gaps,
                # bytes recorded since THIS process armed the track: the span
                # whose stamps line up with every other track (older audio
                # -- before a restart/reboot -- is kept but not aligned)
                "aligned_bytes": min(aligned, trk.buffer_bytes),
                "store": "card" if trk.card else "ram"}
        if not heads:
            return
        path = os.path.join(self.root, HEADS_NAME)
        try:
            with self._heads_lock:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"ts": time.time(), "rate": RATE,
                               "tracks": heads}, f)
                os.replace(tmp, path)
            self._heads_err_logged = False
        except Exception as e:
            if not self._heads_err_logged:
                self._heads_err_logged = True
                log(f"heads snapshot write failed: {e}")

    def _ckpt_loop(self):
        while not self._stop.wait(CKPT_SECS):
            self._checkpoint_all()

    def _checkpoint_all(self):
        self._checkpoint_tracks([t for t in self._writer_tracks
                                 if t.card and t.ring is not None])

    def _checkpoint_tracks(self, tracks, drop=()):
        """CARD rings: snapshot each head under _wlock (so each pos matches
        bytes already handed to the OS), then -- OUTSIDE that lock, so a slow
        card never stalls the writer -- fdatasync each ring, and only THEN
        record every position in the card's ONE combined checkpoint file.
        `drop` = ring dirs whose entries go (disarmed/deleted tracks)."""
        t_start = time.monotonic()
        jobs = []
        with self._wlock:
            for trk in tracks:
                if trk.card and trk.ring is not None:
                    try:
                        jobs.append((trk, os.dup(trk.ring.fileno()),
                                     trk.pos, trk.written))
                    except OSError:
                        pass
        synced = {}
        for trk, fd, pos, written in jobs:
            try:
                os.fdatasync(fd)
                synced[trk.cfg["dir"]] = trk.ckpt_entry(pos, written)
            except Exception as e:
                if not self._ckpt_err_logged:
                    self._ckpt_err_logged = True
                    log(f"t{trk.id}: card sync failed: {e}")
            finally:
                os.close(fd)
        if not synced and not drop:
            return
        path = os.path.join(self.disk_root, CKPT_NAME)
        with self._ckpt_lock:
            try:
                try:
                    with open(path) as f:
                        entries = json.load(f).get("tracks", {})
                    if not isinstance(entries, dict):
                        entries = {}
                except Exception:
                    entries = {}
                entries.update(synced)
                for d in drop:
                    entries.pop(d, None)
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"tracks": entries}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                # and the rename itself, or a power cut can bring back an
                # older checkpoint (= a bigger overlap after the head)
                dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
                self._ckpt_err_logged = False
            except Exception as e:
                if not self._ckpt_err_logged:
                    self._ckpt_err_logged = True
                    log(f"card checkpoint write failed: {e}")
        ms = (time.monotonic() - t_start) * 1000.0
        if ms > self._ckpt_ms_max:
            self._ckpt_ms_max = ms

    def _drain_once(self, trk):
        """One drain pass with failure accounting (review item 18). Logs on
        STATE CHANGE only (first failure, streak crossing DRAIN_FAIL_LIMIT,
        recovery) -- never once per pass."""
        try:
            trk.drain()
        except Exception as e:
            trk.drain_err = str(e)
            trk.drain_err_count += 1
            trk._drain_streak += 1
            if trk._drain_streak == 1:
                log(f"t{trk.id}: writer error: {e}")
            elif (trk._drain_streak >= DRAIN_FAIL_LIMIT
                    and not trk._drain_dead_logged):
                trk._drain_dead_logged = True
                log(f"t{trk.id}: {trk._drain_streak} consecutive drain "
                    f"failures -- track marked NOT recording (last: {e})")
            return
        if trk._drain_streak:
            log(f"t{trk.id}: writer recovered after "
                f"{trk._drain_streak} failed pass(es)")
            trk._drain_streak = 0
            trk._drain_dead_logged = False

    # ── watchdog: config / reconcile / wire / status ─────────────────────────

    def _refresh_config(self):
        """Reload the tracks config on mtime change. Review item 13 (the
        data-loss bug): a MISSING or UNPARSEABLE config keeps the last-known-
        good config -- rings are torn down + deleted ONLY when a successfully
        parsed config explicitly omits or disarms a track. (First-ever start
        with no config never reaches here empty: run() synthesizes the file
        via migrate_legacy_config first.)"""
        try:
            mt = os.stat(self.config_path).st_mtime
        except OSError:
            if not self._cfg_missing_logged:
                log(f"config {self.config_path} missing -- keeping "
                    f"last-known-good ({len(self._cfg_tracks)} track(s))")
                self._cfg_missing_logged = True
            self._cfg_mtime = None       # any reappearing file = fresh load
            return
        self._cfg_missing_logged = False
        if mt == self._cfg_mtime:
            return
        tracks, ok = _load_tracks(self.config_path)
        self._cfg_mtime = mt             # don't re-parse an unchanged bad file
        if not ok:
            if not self._cfg_bad_logged:
                log(f"config unreadable/invalid -- keeping last-known-good "
                    f"({len(self._cfg_tracks)} track(s)) until it parses")
                self._cfg_bad_logged = True
            return
        self._cfg_bad_logged = False
        self._cfg_tracks = tracks
        self._cfg_ever_loaded = True
        log(f"config loaded: {len(self._cfg_tracks)} valid track(s)")

    def _refresh_src_override(self):
        """C5 shim: through Phase 1 the web's source picker still writes the
        legacy ~/.orc1_skipback_src.json (orc1_web.py is untouched until
        Phase 2). While that file exists it stays authoritative for TRACK 0's
        source (mode/left/right only -- never structure), so a web source
        change keeps taking effect within a watchdog pass, exactly like the
        old bridge. Phase 2's track editor must update (or delete) src.json
        when it edits track 0's source, then this shim can retire."""
        try:
            mt = os.stat(self.src_path).st_mtime
        except OSError:
            self._src_override = None
            self._src_mtime = 0
            return
        if mt == self._src_mtime:
            return
        self._src_mtime = mt
        d = _parse_legacy_src(self.src_path)
        if d is not None:
            self._src_override = d
            log(f"track 0 source follows legacy src.json: {d}")
        # unreadable src.json: keep the previous override (same
        # keep-last-known-good discipline as the tracks config)

    def _effective_cfg(self, trk):
        """Track config with the track-0 legacy-source override applied."""
        cfg = trk.cfg
        if trk.id == 0 and self._src_override is not None:
            ov = self._src_override
            cfg = dict(cfg, mode=ov["mode"],
                       left=ov.get("left", ""), right=ov.get("right", ""))
        return cfg

    @staticmethod
    def _ring_bytes(cfg):
        ch = 2 if cfg["stereo"] else 1
        return cfg["length_secs"] * RATE * ch * SAMPLE_BYTES

    def _apply_ram_guard(self):
        """Contract C4 (recorder side): refuse to arm tracks that would push
        (a) the armed-ring sum over the 900 MB budget, OR (b) projected free
        RAM below 400 MB (review item 21d) -- projected free = MemAvailable
        minus the armed ring bytes NOT yet resident in tmpfs (conservative:
        a ring is budgeted at full size up front, so growth to wrap can't
        surprise anyone; already-resident pages are excluded because
        MemAvailable has them subtracted already). Applied in config order;
        blocked tracks are flagged in status. If /proc/meminfo is unreadable,
        fall back to budget-only and log once. The web POST is the first line
        of defense -- this re-check exists because the file is hand-editable."""
        mem_avail = _mem_available_bytes()
        if mem_avail is None and not self._meminfo_warned:
            self._meminfo_warned = True
            log("WARNING: /proc/meminfo unreadable -- free-RAM projection "
                "disabled, RAM guard is budget-only")
        blocked, total, growth, low_ids = set(), 0, 0, set()
        disk_total, card_why = 0, {}
        if not hasattr(self, "_ramlow_logged"):
            self._ramlow_logged = set()
        for cfg in self._cfg_tracks:
            if not cfg["armed"]:
                continue
            b = self._ring_bytes(cfg)
            if cfg.get("store", "ram") == "card":
                why = None
                if not _disk_mounted(self.disk_root):
                    why = f"card storage {self.disk_root} is not mounted"
                else:
                    try:
                        cap = _disk_capacity(self.disk_root)
                    except OSError as e:
                        cap, why = 0, f"card storage unreadable ({e})"
                    if why is None and disk_total + b > cap:
                        why = (f"{b // 1048576} MB would exceed the card's "
                               f"{cap // 1048576} MB ring space")
                if why is not None:
                    blocked.add(cfg["id"])
                    card_why[cfg["id"]] = why
                    if cfg["id"] not in self._blocked_logged:
                        log(f"CARD guard: refusing to arm t{cfg['id']} "
                            f"'{cfg['name']}': {why}")
                        self._blocked_logged.add(cfg["id"])
                else:
                    disk_total += b
                    self._blocked_logged.discard(cfg["id"])
                continue
            ring_path = os.path.join(self.root, cfg["dir"], "ring.raw")
            g = max(0, b - _ring_resident_bytes(ring_path))
            # The free-RAM floor NEVER stops or refuses the recorder
            # (2026-09-23, owner: "the skipback is the most important thing
            # here. everything else can go jump in a lake"). It used to refuse
            # to arm -- and, re-checked every pass, TORE DOWN running rings on
            # any dip under 400 MB (a /patch bridge, one sampler grab: both
            # measured), reclaiming nothing (blocked rings keep their files)
            # while systemctl said "active". After a reboot the rings regrow
            # from empty, so a tight moment would have kept skipback OFF. Now a
            # low projection is only WARNED (log + status "ram_low"); memory
            # pressure is for OTHER processes to yield to (the recorder and
            # jackd run with OOMScoreAdjust=-1000). The 900 MB budget below is
            # the user's own configured ring sizing and still applies.
            if (mem_avail is not None and g > 0
                    and mem_avail - (growth + g) < FREE_MIN):
                low_ids.add(cfg["id"])
                if cfg["id"] not in self._ramlow_logged:
                    log(f"WARNING: free RAM is low (projected "
                        f"{(mem_avail - growth - g) // 1048576} MB after t"
                        f"{cfg['id']} grows) -- arming it ANYWAY: skipback "
                        f"comes first; other processes must yield")
                    self._ramlow_logged.add(cfg["id"])
            else:
                self._ramlow_logged.discard(cfg["id"])
            why = None
            if total + b > RAM_BUDGET:
                why = (f"{b // 1048576} MB would exceed the "
                       f"{RAM_BUDGET // 1048576} MB budget")
            if why is not None:
                blocked.add(cfg["id"])
                if cfg["id"] not in self._blocked_logged:
                    log(f"RAM guard: refusing to arm t{cfg['id']} "
                        f"'{cfg['name']}': {why}")
                    self._blocked_logged.add(cfg["id"])
            else:
                total += b
                growth += g
                self._blocked_logged.discard(cfg["id"])
        self._blocked = blocked
        self._ram_used = total
        self._disk_used = disk_total
        self._card_why = card_why
        self._ram_low = low_ids               # warned, NOT blocked (status)

    @staticmethod
    def _structural_change(old, new):
        # These require a ring/port rebuild; name/source edits do not.
        return (old["stereo"] != new["stereo"]
                or old["length_secs"] != new["length_secs"]
                or old["dir"] != new["dir"]
                or old.get("store", "ram") != new.get("store", "ram"))

    def _publish_rt(self):
        """Rebuild the tuples the RT callback + writer thread iterate.
        Tuple swap is atomic; a writer pass reads the tuple once, so taking
        each writer's lock AFTER the swap waits out any in-flight pass and a
        torn-down track is never drained after close.

        Review 2026-10-01: this used to hold the RAM lock WHILE waiting for
        the CARD lock, on every 5 s watchdog pass, so a stalled SD write
        froze the RAM drains too (the coupling the two-writer split removed).
        Now: nothing changed = no locks at all, and on a change each lock is
        taken alone, one after the other, never both at once."""
        groups, wtracks = [], []
        for trk in self.tracks.values():
            groups.append((tuple(trk.ports), tuple(trk.rbs), trk.stamp_rb))
            wtracks.append(trk)
        groups, wtracks = tuple(groups), tuple(wtracks)
        if groups == self._rt_groups and wtracks == self._writer_tracks:
            return
        self._rt_groups = groups
        self._writer_tracks = wtracks
        for card in (False, True):
            with self._wlocks[card]:
                pass

    def _bring_up(self, cfg):
        trk = Track(cfg, self.root, self.disk_root)
        try:
            trk.init_ring()
            for nm in trk.port_names():
                trk.ports.append(self.client.inports.register(nm))
            rbb = RB_BYTES_CARD if trk.card else RB_BYTES
            trk.rbs = [jack.RingBuffer(rbb) for _ in trk.ports]
            trk.stamp_rb = jack.RingBuffer(
                STAMP_RB_BYTES_CARD if trk.card else STAMP_RB_BYTES)
        except Exception as e:
            log(f"t{cfg['id']}: bring-up failed: {e}")
            for p in trk.ports:
                try:
                    p.unregister()
                except Exception:
                    pass
            trk.close(delete_files=False)
            return
        self.tracks[cfg["id"]] = trk
        self._legacy_symlinks(trk)
        if trk.card:
            self._checkpoint_tracks([trk])
        log(f"t{cfg['id']} '{cfg['name']}' armed: {trk.channels}ch, "
            f"{cfg['length_secs']}s ring, ports {trk.port_names()}")

    def _teardown_track(self, trk, delete_files):
        self.tracks.pop(trk.id, None)
        self._publish_rt()          # RT + writer stop seeing the track first
        # Grace for any in-flight process cycle: ~2 JACK periods, derived from
        # the live blocksize (review item 3 -- the old fixed 60 ms silently
        # assumed jackd's -p 1024 @48k). Clamped 20-250 ms; falls back to
        # 60 ms if the client is gone.
        try:
            grace = 2.0 * self.client.blocksize / float(self.client.samplerate)
        except Exception:
            grace = 0.06
        time.sleep(min(0.25, max(0.02, grace)))
        for p in trk.ports:
            try:
                p.unregister()
            except Exception:
                pass                # jackd may already be gone -- fine
        trk.ports, trk.rbs, trk.stamp_rb = [], [], None
        if trk.card:
            try:
                trk.ring.flush()
            except Exception:
                pass
            if delete_files:
                self._checkpoint_tracks([], drop=[trk.cfg["dir"]])
            else:                   # last word before a stop: data, then pos
                self._checkpoint_tracks([trk])
        trk.close(delete_files=delete_files)
        log(f"t{trk.id} torn down"
            + (" (ring files deleted, RAM reclaimed)" if delete_files else
               " (ring files kept)"))

    def _resolve(self, cfg, names):
        """Source resolution, ported from skipback_bridge.sh resolve_src().
        Returns the list of source port names ([L, R] stereo / [M] mono --
        a mono track records exactly ONE port, never a summed pair)."""
        if cfg["mode"] == "manual":
            pair = (cfg["left"], cfg["right"] or cfg["left"])
        else:
            for l, r in AUTO_PAIRS[:-1]:
                if l in names and r in names:
                    pair = (l, r)
                    break
            else:
                pair = AUTO_PAIRS[-1]           # synth direct, unconditional
        if cfg["stereo"]:
            return [pair[0], pair[1]]
        return [pair[0]]                        # mono: left of the pair only

    def _wire(self, trk, names):
        """Idempotent wiring (bridge wire() semantics): if a port is already
        connected to exactly its resolved source, touch nothing -- never
        flap. Sources that aren't up yet are retried next pass."""
        ok = True
        for port, src in zip(trk.ports, trk.resolved):
            try:
                conns = [p.name for p in self.client.get_all_connections(port)]
            except jack.JackError:
                ok = False
                continue
            if conns == [src]:
                continue                        # already correct -- no-op
            if src not in names:
                ok = False                      # source not registered yet
                continue
            for old in conns:
                if old != src:
                    try:
                        self.client.disconnect(old, port)
                    except jack.JackError:
                        pass
            if src not in conns:
                try:
                    self.client.connect(src, port)
                    log(f"t{trk.id}: source -> {src}")
                except jack.JackError as e:
                    log(f"t{trk.id}: connect {src} failed: {e}")
                    ok = False
        trk.connected = ok

    def _legacy_symlinks(self, trk):
        """Contract C5: <root>/ring.{raw,pos} -> t0/ so legacy readers (the
        sampler's RingBufferReader, skipback.sh) keep working byte-for-byte.
        (Re)created on every start -- tmpfs wipes symlinks on reboot. Guards:
        only when track 0 lives at the canonical t0 dir (a proto 'dir'
        override must never touch the live paths), and a REAL file at the
        legacy path means the old pipeline still owns it -- skip, log, never
        clobber."""
        if trk.id != 0 or trk.cfg["dir"] != "t0":
            return
        for nm in ("ring.raw", "ring.pos"):
            path = os.path.join(self.root, nm)
            target = os.path.join("t0", nm)     # relative: survives root moves
            try:
                if os.path.islink(path):
                    if os.readlink(path) != target:
                        os.remove(path)
                        os.symlink(target, path)
                        log(f"legacy symlink repointed: {path} -> {target}")
                elif os.path.exists(path):
                    log(f"NOT symlinking {path}: real file present "
                        f"(legacy pipeline still owns it)")
                else:
                    os.symlink(target, path)
                    log(f"legacy symlink created: {path} -> {target}")
            except OSError as e:
                log(f"symlink {path} failed: {e}")

    def _write_status(self):
        """Contract C3: atomic status JSON, one entry per configured track
        plus globals. Written every watchdog pass; web reads, never writes."""
        entries = []
        for cfg in self._cfg_tracks:
            trk = self.tracks.get(cfg["id"])
            e = {
                "id": cfg["id"],
                "name": cfg["name"],
                "armed": cfg["armed"],
                # recording = ring handle alive AND drains actually landing
                # (review item 18: a dead file handle / persistent drain
                # failure must not report as recording)
                "recording": bool(trk and trk.ring is not None
                                  and trk._drain_streak < DRAIN_FAIL_LIMIT),
                "resolved": list(trk.resolved) if trk else [],
                "connected": bool(trk and trk.connected),
                "fill_secs": (round(min(trk.written, trk.buffer_bytes)
                                    / trk.bytes_per_sec, 1) if trk else 0),
                "ring_bytes": trk.buffer_bytes if trk else 0,
                # periods dropped since this track was armed (0 = every
                # sample since arming lines up with every other track)
                "gaps": trk.gaps if trk else 0,
                "store": cfg.get("store", "ram"),
                "persist": cfg.get("persist", True),
            }
            if trk and trk.card:
                e["resumed_from"] = trk.resumed_from
            if trk and trk.drain_err is not None:
                e["drain_err"] = trk.drain_err
                e["drain_err_count"] = trk.drain_err_count
            if cfg["id"] in self._card_why:
                e["card_blocked"] = self._card_why[cfg["id"]]
            elif cfg["id"] in self._blocked:
                e["ram_blocked"] = True
            entries.append(e)
        status = {"ts": time.time(),
                  "ram_used_mb": round(self._ram_used / 1048576.0, 1),
                  "card_used_mb": round(self._disk_used / 1048576.0, 1),
                  "card_mounted": _disk_mounted(self.disk_root),
                  # slowest card checkpoint / writer pass since start (ms):
                  # the stall margin vs the ~43 s card RT buffer
                  "ckpt_max_ms": round(self._ckpt_ms_max),
                  "writer_max_ms": round(self._writer_ms_max),
                  # per writer: a CARD stall must never show up in RAM's
                  "writer_ram_max_ms": round(self._writer_ms[False]),
                  "writer_card_max_ms": round(self._writer_ms[True]),
                  "ram_low": sorted(getattr(self, "_ram_low", ())),
                  "xruns": self._xruns,
                  "tracks": entries}
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(status, f)
            os.replace(tmp, self.status_path)
        except Exception as e:
            log(f"status write failed: {e}")

    def _reconcile(self):
        """One watchdog pass: config -> RAM guard -> teardown -> bring-up ->
        resolve + wire -> publish RT tuples -> status."""
        self._refresh_config()
        self._refresh_src_override()
        self._apply_ram_guard()

        want = {c["id"]: c for c in self._cfg_tracks
                if c["armed"] and c["id"] not in self._blocked}

        # Teardown: removed / disarmed (delete files = reclaim RAM),
        # RAM-blocked (files KEPT -- see below), and structurally-changed
        # tracks (rebuilt fresh below).
        for tid in list(self.tracks):
            trk = self.tracks[tid]
            cfg = want.get(tid)
            if cfg is None:
                # `want` excludes RAM-blocked ids, so a track the RAM guard
                # REFUSED TO ARM lands in this same branch as "removed" and
                # "disarmed" -- and used to be torn down with
                # delete_files=True. Refusing to arm a track destroyed audio
                # that had already been recorded. Fixed 2026-07-29.
                #
                # Keyed on _blocked specifically, NOT by flipping the whole
                # branch: "removed" must still delete or the ring directory
                # leaks forever, and "disarmed" must still delete because
                # disarm-reclaims-RAM is the documented contract (Track.close).
                #
                # KNOWN TRADE (deliberate, and the safer side of it): a kept
                # ring is a FROZEN ring -- nothing advances ring.pos while the
                # track is blocked, so a reader that trusts pos can serve
                # minutes-old audio believing it is current. head_info()
                # clamps its extrapolation to 1 s on a stale mtime but still
                # returns the stale pos itself. Losing recorded audio is the
                # worse failure, so the ring is kept; gating the sampler on
                # ring.pos mtime is the follow-up (see the plan's P8/P9).
                # Note also that a blocked track's bytes never enter `total`,
                # so its retained RAM is invisible to the budget.
                _blocked = tid in self._blocked
                self._teardown_track(trk, delete_files=not _blocked)
                if _blocked:
                    log(f"t{tid} RAM-blocked: ring files KEPT (frozen, not "
                        f"deleted) -- recorded audio preserved")
            elif self._structural_change(trk.cfg, cfg):
                self._teardown_track(trk, delete_files=True)
            else:
                trk.cfg = cfg               # pick up name/source-only edits

        # Bring-up: newly armed tracks.
        for tid, cfg in want.items():
            if tid not in self.tracks:
                self._bring_up(cfg)

        # Resolve + wire every active track (auto mode follows the rig).
        try:
            names = set(p.name for p in self.client.get_ports())
        except jack.JackError:
            names = set()
        for trk in self.tracks.values():
            trk.resolved = self._resolve(self._effective_cfg(trk), names)
            self._wire(trk, names)

        self._publish_rt()

        # Surface RT flags set by the callback (it can't log for itself).
        if self._rt_trouble:
            self._rt_trouble = False
            log("WARNING: exception caught inside the process callback "
                "(port torn down mid-cycle?) -- audio continues")
        if self._rt_dropped:
            self._rt_dropped = False
            log("WARNING: RT ringbuffer overflow -- writer thread stalled "
                "behind its RT buffer (RAM ~10.9 s, CARD ~43.7 s), samples "
                "were dropped")

        self._write_status()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def run(self):
        # First-run migration (spec): no tracks config -> synthesize from the
        # legacy single-source config. Idempotent; NEVER falls through to a
        # zero-track (mass-delete) state on first start.
        try:
            migrate_legacy_config(self.config_path, self.src_path)
        except Exception as e:
            log(f"migration failed ({e}) -- continuing without a config")
        threading.Thread(target=self._writer_loop, args=(False,), daemon=True,
                         name="skipback-writer-ram").start()
        threading.Thread(target=self._writer_loop, args=(True,), daemon=True,
                         name="skipback-writer-card").start()
        threading.Thread(target=self._ckpt_loop, daemon=True,
                         name="skipback-ckpt").start()
        fails = 0
        while not self._stop.is_set():
            # Connect (never autostart -- orc1-jackd.service owns the server).
            try:
                self.client = jack.Client(CLIENT_NAME, no_start_server=True)
            except jack.JackError as e:
                fails += 1
                log(f"jackd not reachable ({e}) -- retrying in "
                    f"{JACK_RETRY_SECS:g}s")
                self._write_status()            # stale-free 'down' snapshot
                if fails * JACK_RETRY_SECS >= FRESH_PROCESS_AFTER_SECS:
                    # Belt and braces: an in-process retry can wedge on a stale
                    # libjack shm mapping forever (measured 2026-09-23). A fresh
                    # process never does, and systemd restarts us in 2 s.
                    log(f"still no jackd after {fails} tries -- exiting so "
                        f"systemd starts a FRESH process (rings are kept)")
                    raise SystemExit(EXIT_RECONNECT)
                self._stop.wait(JACK_RETRY_SECS)
                continue
            fails = 0
            self._jack_down.clear()
            self.client.set_process_callback(self._process)
            self.client.set_shutdown_callback(self._on_shutdown)
            self.client.set_xrun_callback(self._on_xrun)
            try:
                self.client.activate()
            except jack.JackError as e:
                log(f"activate failed ({e}) -- retrying")
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
                self._stop.wait(JACK_RETRY_SECS)
                continue
            log(f"connected to jackd as '{CLIENT_NAME}' "
                f"(sr={self.client.samplerate}, "
                f"period={self.client.blocksize})")
            self._cfg_mtime = None              # force a full fresh reconcile

            # Watchdog loop -- runs until stop or the server goes away.
            while not self._stop.is_set() and not self._jack_down.is_set():
                try:
                    self._reconcile()
                except jack.JackError as e:
                    log(f"jack error in watchdog ({e}) -- treating server "
                        f"as down")
                    self._jack_down.set()
                except Exception as e:
                    log(f"watchdog error: {e}")
                deadline = time.monotonic() + WATCHDOG_SECS
                while (time.monotonic() < deadline
                       and not self._stop.is_set()
                       and not self._jack_down.is_set()):
                    time.sleep(0.25)

            # Server lost or stopping: keep every ring file (history survives;
            # matching-size rings are adopted + resumed on reconnect).
            for tid in list(self.tracks):
                self._teardown_track(self.tracks[tid], delete_files=False)
            try:
                self.client.deactivate()
                self.client.close()
            except Exception:
                pass
            self.client = None
            if not self._stop.is_set():
                # Do NOT reconnect in-process. MEASURED 2026-09-23: after a jackd
                # restart this process retried every 3 s forever ("Cannot open
                # shm segment" -- libjack keeps the dead server's shm mapping)
                # while systemd reported it "active": recording silently OFF.
                # A fresh process connects first time, so exit and let
                # Restart=on-failure (2 s) bring one up. Rings are kept above,
                # and init_ring resumes each head -- no recorded audio is lost.
                log("jack connection lost -- rings kept; exiting so systemd "
                    "restarts a FRESH process (in-process reconnect wedges)")
                self._write_status()
                raise SystemExit(EXIT_RECONNECT)
        log("stopped")


# ── entry point ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ORC-1 multitrack skipback ring recorder (JACK client "
                    "'skipback-rec'). See _planning/"
                    "ORC1_Multitrack_Skipback_Spec.md.")
    ap.add_argument("--config", default="~/.orc1_skipback_tracks.json",
                    help="tracks config JSON (hand-editable; reloaded live "
                         "on mtime change)")
    ap.add_argument("--root", default="~/.skipbuf",
                    help="ring root dir (tmpfs); track dirs live under it")
    ap.add_argument("--status", default="~/.orc1_skipback_status.json",
                    help="status JSON written atomically each watchdog pass")
    ap.add_argument("--disk-root", default=DISK_ROOT,
                    help="card partition for store:card rings (must be a "
                         "mountpoint, or card tracks refuse to arm)")
    args = ap.parse_args()

    rec = Recorder(os.path.expanduser(args.config),
                   os.path.expanduser(args.root),
                   os.path.expanduser(args.status),
                   disk_root=os.path.expanduser(args.disk_root))

    def _sig(signum, frame):
        log(f"signal {signum} -- shutting down")
        rec.request_stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    rec.run()


if __name__ == "__main__":
    main()
