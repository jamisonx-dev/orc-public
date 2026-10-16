#!/usr/bin/env python3
"""skipback_grab_multi.py -- grab SEVERAL skipback tracks, sample-aligned.

    usage: skipback_grab_multi.py <seconds|0> [--tracks 0,3,5] [--root DIR]
                                  [--outdir DIR] [--joins 3:4,9:10]
                                  [--joins-file FILE]

<seconds> = how far back; 0 (or more than is available) = everything every
chosen track still holds in common. No --tracks = every recording track.

Output: ONE folder in ~/recordings holding one WAV per track, all starting at
the same JACK frame and all the same length -- drop them into a DAW at zero and
they line up to the sample. Plus manifest.json (tracks, files, end frame, any
dropped-period warnings). Prints "Saved: <folder>" and exits 0, like
skipback.sh, so callers parse both the same way.

Stereo JOINS: --joins L:R (or --joins-file, a JSON {"joins": [[L, R], ...]} --
the web /skipback page keeps ~/.orc1_skipback_joins.json) writes two MONO
tracks as ONE stereo WAV (L track left, R track right), same start and length
as everything else. A join only applies when both tracks are in the grab and
both are mono; otherwise they are written as separate files and the manifest
says why (manifest "joins_skipped"). A track can be in only one join. With no
--joins / --joins-file nothing is joined (the file is never read by default).

How the alignment works: the recorder stamps every JACK period with its frame
time, so each head in <root>/heads.json carries an exact frame. The newest
frame EVERY chosen track has reached is the common end; each track's cut ends
there (a track that drained further is cut that many frames earlier).

Memory: tracks are copied together, oldest audio first, CHUNK frames at a
time, and every output file is fdatasync'd + dropped from the page cache every
SYNC_EVERY bytes -- nothing holds a whole track in RAM, and nothing is written
to /tmp (tmpfs). The folder is built under a hidden .part name and renamed
when every file is complete.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
import time
import wave

RATE_DEFAULT = 48000
SAMPLE_BYTES = 2
HEADS_NAME = "heads.json"
HEADS_MAX_AGE = 3.0          # s -- older (after the wait) = recorder isn't writing
FRESH_S = 0.5                # heads older than this at the press = writer stalled
FRESH_WAIT_DEFAULT = 15.0    # s to wait for a stalled writer (SKIPBACK_FRESH_WAIT)
CHUNK_FRAMES = 1 << 20       # per track per step (2 MB mono, 4 MB stereo)
SYNC_EVERY = 32 << 20        # bytes written (all files) between syncs
FULL_RING_MARGIN = 2.0       # s kept clear of the write head on a full ring
# A head's "t" is stamped when its drain FINISHED, but the newest audio in it
# was captured about half a writer pass + one period earlier. Trimming to the
# press by (t - press) alone ended files ~20-60 ms early (review 2026-10-01).
HEAD_LAG_S = 0.03
CUSHION_S = {"ram": 11.0, "card": 44.0}   # recorder RB_BYTES / RB_BYTES_CARD


def fail(msg):
    print(f"skipback-multi: {msg}", file=sys.stderr)
    sys.exit(1)


def signed32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x >= (1 << 31) else x


def plan(heads, ids, seconds, rate, back=0):
    """Pure: decide each track's [start, start+nbytes) in its ring. Returns
    (frames, per-track list, end_frame). `back` = frames to end BEFORE the
    common head (the press, when the heads were published after a writer
    stall). Raises ValueError with a readable message when nothing can be
    grabbed."""
    tracks = heads.get("tracks", {})
    chosen = []
    for tid in ids:
        h = tracks.get(str(tid))
        if h is None or h.get("frame") is None:
            raise ValueError(f"track {tid} is not recording (no head)")
        chosen.append((tid, h))
    if not chosen:
        raise ValueError("no recording tracks to grab")

    ref = chosen[0][1]["frame"]
    offs = [signed32(h["frame"] - ref) for _, h in chosen]
    common = min(offs)
    out, limit = [], None
    for (tid, h), off in zip(chosen, offs):
        fb = h["channels"] * SAMPLE_BYTES
        buf = h["buffer_bytes"]
        lag = off - common + back               # frames past the (press) end
        end = (h["pos"] - lag * fb) % buf
        # Only audio recorded since the recorder armed this track carries
        # stamps that line up with the others (older, e.g. pre-reboot history
        # on a CARD ring, is kept for single-track grabs but not aligned).
        have_bytes = min(h["fill_bytes"], h.get("aligned_bytes", h["fill_bytes"]), buf)
        # never cut into the last FULL_RING_MARGIN before the LIVE write head.
        # main() lowers pos to the press and adds how far the live head is
        # past it (the shift, plus any backlog a still-stalled writer will
        # write when its card unsticks) as "margin_s" (review 2026-10-01:
        # the margin used to be measured from the shifted pos, so a long
        # wait ate it and the catch-up writes overwrote the oldest audio).
        margin = int(h.get("margin_s", FULL_RING_MARGIN) * rate)
        have = min(have_bytes // fb, buf // fb - margin) - lag
        limit = have if limit is None else min(limit, have)
        out.append({"id": tid, "h": h, "fb": fb, "buf": buf, "end": end})
    want = int(seconds * rate) if seconds > 0 else limit
    frames = max(0, min(want, limit))
    if frames <= 0:
        raise ValueError("nothing recorded in common yet")
    for t in out:
        t["start"] = (t["end"] - frames * t["fb"]) % t["buf"]
    return frames, out, (ref + common - back) & 0xFFFFFFFF


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(s)) or "track"


def parse_joins(text):
    """'3:4,9:10' -> [(3, 4), (9, 10)]. Raises ValueError."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        l, r = part.split(":")
        out.append((int(l), int(r)))
    return out


def load_joins_file(path):
    """{"joins": [[L, R], ...]} -> [(L, R)]. Missing file = no joins; a
    malformed file is an error (a grab silently ignoring your joins would
    hand you the wrong folder)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    items = raw.get("joins", []) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path}: no 'joins' list")
    out = []
    for j in items:
        if (not isinstance(j, (list, tuple)) or len(j) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in j)):
            raise ValueError(f"{path}: bad join {j!r} (want [L, R] track ids)")
        out.append((j[0], j[1]))
    return out


def outputs(tracks, joins):
    """Pure: group the planned tracks into output files. Returns
    (outs, skipped): outs = list of lists of planned tracks (1 = its own file,
    2 = [L, R] joined into one stereo file), in grab order; skipped = list of
    readable reasons for joins that could not apply."""
    by_id = {t["id"]: t for t in tracks}
    partner, skipped, used = {}, [], set()
    for l, r in joins:
        tag = f"t{l}+t{r}"
        if l == r:
            skipped.append(f"{tag}: a track can't join itself")
        elif l in used or r in used:
            skipped.append(f"{tag}: a track is already in another join")
        elif l not in by_id or r not in by_id:
            skipped.append(f"{tag}: not both in this grab")
        elif by_id[l]["h"]["channels"] != 1 or by_id[r]["h"]["channels"] != 1:
            skipped.append(f"{tag}: both tracks must be MONO")
        else:
            partner[l] = r
            used.update((l, r))
    outs = []
    for t in tracks:
        if t["id"] in partner:
            outs.append([t, by_id[partner[t["id"]]]])
        elif t["id"] not in used:
            outs.append([t])
    return outs, skipped


def interleave(lb, rb):
    """Two mono 16-bit little-endian byte strings -> one stereo LRLR..."""
    out = bytearray(len(lb) * 2)
    out[0::4] = lb[0::2]
    out[1::4] = lb[1::2]
    out[2::4] = rb[0::2]
    out[3::4] = rb[1::2]
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("seconds", type=float)
    ap.add_argument("--tracks", default="",
                    help="comma-separated track ids (default: all recording)")
    ap.add_argument("--root", default=os.environ.get(
        "SKIPBACK_DIR", os.path.expanduser("~/.skipbuf")))
    ap.add_argument("--outdir", default=os.environ.get(
        "SKIPBACK_OUTDIR", os.path.expanduser("~/recordings")))
    ap.add_argument("--joins", default="",
                    help="mono tracks to write as one stereo WAV, L:R,L:R")
    ap.add_argument("--joins-file", default="",
                    help='JSON {"joins": [[L, R], ...]}; added to --joins')
    a = ap.parse_args()
    try:
        joins = parse_joins(a.joins)
        if a.joins_file:
            joins += load_joins_file(os.path.expanduser(a.joins_file))
    except (ValueError, OSError) as e:
        fail(f"bad joins: {e}")

    # The press. On CARD rings the SD card can stall that writer for seconds
    # (measured 7.3 s); its heads then stop moving while RAM tracks' keep
    # going (two writers since 2026-09-29). Each head carries "t" = when it
    # was true. Wait until every chosen track has drained past the press,
    # then shift each track's cut back to the press by its own "t" -- whole
    # frames on pos AND frame together, so the sample alignment holds --
    # instead of refusing or ending early.
    press = float(os.environ.get("SKIPBACK_PRESS_TS") or time.time())
    max_wait = float(os.environ.get("SKIPBACK_FRESH_WAIT", FRESH_WAIT_DEFAULT))
    hp = os.path.join(a.root, HEADS_NAME)

    def load_heads():
        try:
            with open(hp) as f:
                return json.load(f)
        except Exception:
            return None

    heads = load_heads()
    if heads is None:
        fail(f"no heads snapshot in {a.root} -- is the recorder running?")
    if a.tracks.strip():
        try:
            ids = [int(x) for x in a.tracks.split(",") if x.strip()]
        except ValueError:
            fail(f"bad --tracks {a.tracks!r}")
    else:
        ids = sorted(int(k) for k in heads.get("tracks", {}))

    def head_t(h, tid):
        """When track tid's head was true (older snapshots: the file's ts)."""
        t = h.get("tracks", {}).get(str(tid), {}).get("t")
        return float(t if t is not None else h.get("ts", 0))

    def behind(h):
        return [tid for tid in ids if head_t(h, tid) < press]

    waited = False
    if any(head_t(heads, tid) < press - FRESH_S for tid in ids) and max_wait > 0:
        deadline = time.time() + max_wait
        while time.time() < deadline and behind(heads):
            time.sleep(0.05)
            h = load_heads()
            if h is not None:
                heads = h
        waited = True
    age = time.time() - float(heads.get("ts", 0))
    if age > HEADS_MAX_AGE:
        fail(f"heads snapshot is {age:.0f}s old -- the recorder isn't writing")
    rate = int(heads.get("rate", RATE_DEFAULT))
    stalled = behind(heads) if waited else []
    shifted = 0.0
    now = time.time()
    for tid in ids:
        h = heads.get("tracks", {}).get(str(tid))
        if not h or h.get("frame") is None:
            continue                            # plan() reports it
        ht = head_t(heads, tid)
        k = int((ht - HEAD_LAG_S - press) * rate)
        # how far the live head may be past this head when we read: the
        # backlog since "t" (a still-stalled writer catches it up later)
        # (a fresh head owes less than the margin already covers; a stale one
        # is capped at the writer's RT cushion: it can't owe more than that)
        owed = now - ht
        owed = (min(owed, CUSHION_S.get(h.get("store"), 44.0))
                if owed > FRESH_S else 0.0)
        h["margin_s"] = FULL_RING_MARGIN + owed + max(0, k) / float(rate)
        if k <= 0:
            continue                            # head is at / before the press
        fb = h["channels"] * SAMPLE_BYTES
        h["frame"] = (h["frame"] - k) & 0xFFFFFFFF
        h["pos"] = (h["pos"] - k * fb) % h["buffer_bytes"]
        h["fill_bytes"] = max(0, h["fill_bytes"] - k * fb)
        if "aligned_bytes" in h:
            h["aligned_bytes"] = max(0, h["aligned_bytes"] - k * fb)
        shifted = max(shifted, k / float(rate))
    if waited:
        print(f"skipback-multi: a writer was stalled; waited and cut at the "
              f"press" + (f" (still stalled: {stalled})" if stalled else ""),
              file=sys.stderr)
    # A chosen track with no head (armed but blocked: a CARD track with the
    # card partition unmounted, or refused for RAM) is left out with a note,
    # instead of failing the whole grab (review 2026-10-01: the /skipback
    # page sends every ARMED track, so one blocked track lost them all).
    live = heads.get("tracks", {})
    not_recording = [tid for tid in ids
                     if (live.get(str(tid)) or {}).get("frame") is None]
    if not_recording and len(not_recording) < len(ids):
        ids = [tid for tid in ids if tid not in not_recording]
        print(f"skipback-multi: not recording, left out: {not_recording}",
              file=sys.stderr)
    else:
        not_recording = []
    try:
        frames, tracks, end_frame = plan(heads, ids, a.seconds, rate)
    except ValueError as e:
        fail(str(e))

    outs, joins_skipped = outputs(tracks, joins)
    secs = frames / float(rate)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"multi_{len(tracks)}tr_{secs:.0f}s_{stamp}"
    os.makedirs(a.outdir, exist_ok=True)
    # two grabs in the same second (web + pedal) must not collide
    for n in range(1, 100):
        b = base if n == 1 else f"{base}_{n}"
        part = os.path.join(a.outdir, f".{b}.part")
        final = os.path.join(a.outdir, b)
        if os.path.exists(final):
            continue
        try:
            os.makedirs(part)
            break
        except FileExistsError:
            continue
    else:
        fail("no free folder name")

    files = []
    try:
        for grp in outs:
            if len(grp) == 1:
                h = grp[0]["h"]
                name = f"t{grp[0]['id']:02d}_{safe_name(h.get('name'))}.wav"
                nch = h["channels"]
            else:
                l, r = grp
                name = (f"t{l['id']:02d}+t{r['id']:02d}_"
                        f"{safe_name(l['h'].get('name'))}+"
                        f"{safe_name(r['h'].get('name'))}.wav")
                nch = 2
            f = open(os.path.join(part, name), "wb")
            w = wave.open(f, "wb")
            w.setnchannels(nch)
            w.setsampwidth(SAMPLE_BYTES)
            w.setframerate(rate)
            w.setnframes(frames)
            rings = [open(os.path.join(a.root, t["h"]["dir"], "ring.raw"), "rb")
                     for t in grp]
            files.append((grp, f, w, rings, name))

        def read(t, ring, done, n):
            off = (t["start"] + done * t["fb"]) % t["buf"]
            want = n * t["fb"]
            first = min(want, t["buf"] - off)
            ring.seek(off)
            b = ring.read(first)
            if first < want:
                ring.seek(0)
                b += ring.read(want - first)
            return b

        unsynced, done = 0, 0
        while done < frames:
            n = min(CHUNK_FRAMES, frames - done)
            for grp, f, w, rings, _ in files:
                parts = [read(t, ring, done, n) for t, ring in zip(grp, rings)]
                b = parts[0] if len(parts) == 1 else interleave(*parts)
                w.writeframesraw(b)
                unsynced += len(b)
            done += n
            if unsynced >= SYNC_EVERY:
                for _, f, _, _, _ in files:
                    f.flush()
                    os.fdatasync(f.fileno())
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                unsynced = 0
        nch_of = {}
        for grp, f, w, rings, name in files:
            nch_of[name] = w.getnchannels()
            w.close()
            f.flush()
            os.fdatasync(f.fileno())
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            f.close()
            for ring in rings:
                ring.close()

        gaps = {str(t["id"]): t["h"].get("gaps", 0) for t in tracks
                if t["h"].get("gaps", 0)}
        manifest = {
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "rate": rate, "frames": frames, "seconds": round(secs, 3),
            "end_jack_frame": end_frame,
            "aligned": True,
            "tracks": [{"id": t["id"], "name": t["h"].get("name"),
                        "channels": t["h"]["channels"], "file": name,
                        **({"joined": "LR"[k]} if len(grp) == 2 else {})}
                       for grp, _, _, _, name in files
                       for k, t in enumerate(grp)],
            "files": [{"file": name, "channels": nch_of[name],
                       "tracks": [t["id"] for t in grp]}
                      for grp, _, _, _, name in files],
        }
        if joins_skipped:
            manifest["joins_skipped"] = joins_skipped
        if not_recording:
            manifest["not_recording"] = not_recording
        if stalled:
            manifest["stalled"] = stalled
            manifest["stalled_note"] = (
                "these tracks' writers were still stalled after the wait, so "
                "every file ends at THEIR last written audio (a little before "
                "the press)")
        if gaps:
            # The recorder fills a dropped stretch with silence of its exact
            # length (2026-10-01), so the files still line up; a drop it could
            # not fill restarts that track's aligned span instead, and plan()
            # never reaches back past it.
            manifest["note"] = (
                "these tracks dropped periods since they were armed (an xrun "
                "or a stalled writer); each drop is silent in the file, and "
                "the tracks still line up")
            manifest["gaps"] = gaps
        with open(os.path.join(part, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)
        os.rename(part, final)
    except Exception as e:
        for _, f, _, rings, _ in files:
            for h in (f, *rings):
                try:
                    h.close()
                except Exception:
                    pass
        shutil.rmtree(part, ignore_errors=True)
        fail(f"grab failed: {e}")

    print(f"Saved: {final}")


if __name__ == "__main__":
    main()
