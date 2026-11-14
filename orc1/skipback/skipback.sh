#!/bin/bash
# skipback.sh — grab the last N seconds from a skipback ring into ~/recordings.
#
# Phase 2 (multitrack): an optional 3rd arg selects which track ring to grab.
# Track 0 uses the legacy top-level ~/.skipbuf/ring.{raw,pos} paths (the t0/
# symlink points here too, so nothing about a bare track-0 grab changed); other
# tracks read ~/.skipbuf/t<id>/. Ring length (secs) and channel count come from
# the tracks JSON per track — NOT the old hardcoded 1860s/2ch constants.
#
#   usage: skipback.sh <seconds_back> [wav|mp3] [track_id]
#
# WAV is the default (the ring's PCM streamed under a WAV header — no encoder).
# mp3 is kept as an option (2nd arg or $SKIPBACK_FORMAT). The copy and the
# encode run niced + idle-IO so a grab can never preempt the RT synth.

# The moment of the press: the grab ends HERE even if the recorder's writer is
# stalled on a CARD ring (see the head wait below). Exported so the root ->
# user re-exec keeps the original press time.
export SKIPBACK_PRESS_TS="${SKIPBACK_PRESS_TS:-$(date +%s.%N)}"

SECONDS_BACK=$1
FORMAT="${2:-${SKIPBACK_FORMAT:-wav}}"
TRACK_ID="${3:-0}"

case "$FORMAT" in wav|mp3) ;; *) FORMAT="wav" ;; esac
if ! [[ "$TRACK_ID" =~ ^[0-9]+$ ]]; then
    echo "skipback: bad track id '$TRACK_ID' (0-63)" >&2
    exit 1
fi

# The pisound button daemon runs this script as ROOT, where $HOME is /root and
# the per-user skipback ring (written by the pibox-owned skipback-record
# service) can't be found -- so a button press failed with "no ring ... at
# /root/.skipbuf/ring.raw" and did nothing. If we're root and our own ring is
# absent, re-exec as the user who OWNS this script (the ORC install user) with
# their HOME set, so the button works no matter which user the daemon runs as
# and the grabbed WAVs stay owned by that user. Portable: the user is derived
# from the file, never hardcoded. Manual/web runs (already the ORC user) skip
# this entirely.
if [ "$(id -u)" = "0" ] && [ -z "${SKIPBACK_DIR:-}" ] && [ ! -d "$HOME/.skipbuf" ]; then
    _self="$(readlink -f "$0")"
    _owner="$(stat -c %U "$_self" 2>/dev/null)"
    _ohome="$(getent passwd "$_owner" 2>/dev/null | cut -d: -f6)"
    if [ -n "$_owner" ] && [ "$_owner" != "root" ] && [ -d "$_ohome/.skipbuf" ] \
       && command -v runuser >/dev/null 2>&1; then
        exec runuser -u "$_owner" -- env HOME="$_ohome" "$_self" "$@"
    fi
fi

SKIPBUF="${SKIPBACK_DIR:-$HOME/.skipbuf}"
TRACKS_JSON="${SKIPBACK_TRACKS_JSON:-$HOME/.orc1_skipback_tracks.json}"

# Ring length + channel count + a filename-safe track name + the per-track ring
# DIRECTORY come from the tracks JSON (python3 — jq is not guaranteed on the Pi).
# Missing file / missing track falls back to the historical 1860s / 2ch /
# no-name / t<id>, so a bare track-0 grab still behaves exactly as before.
#
# One field PER LINE, not space-separated (changed 2026-07-29). The old form
# was `print secs, chans, name` read back with `awk '{print $N}'`, but `name`
# is frequently EMPTY — awk collapses whitespace runs, so appending a 4th
# field would have made $3 pick up the dir whenever the track was unnamed.
# Line-per-field is immune to that.
META=$(python3 - "$TRACKS_JSON" "$TRACK_ID" << 'PYEOF'
import json, re, sys
path, tid = sys.argv[1], int(sys.argv[2])
secs, chans, name, rdir = 1860, 2, "", ""
try:
    with open(path) as f:
        for t in json.load(f).get("tracks", []):
            if t.get("id") == tid:
                secs = int(t.get("length_secs", 1860))
                chans = 2 if t.get("stereo", True) else 1
                name = re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("name", "")))
                d = t.get("dir")
                # Plain directory name only — this is interpolated into a shell
                # path, so anything that could escape ~/.skipbuf is rejected and
                # falls back to the default rather than being sanitized into
                # some other track's directory.
                if (isinstance(d, str) and d and "/" not in d and "\\" not in d
                        and not d.startswith(".")):
                    rdir = d
                break
except Exception:
    pass
print(secs)
print(chans)
print(name)
print(rdir or ("t%d" % tid))
PYEOF
)
RING_SECS=$(printf '%s\n' "$META" | sed -n 1p)
CHANNELS=$(printf '%s\n' "$META"  | sed -n 2p)
TRACK_NAME=$(printf '%s\n' "$META" | sed -n 3p)
TRACK_DIR=$(printf '%s\n' "$META"  | sed -n 4p)
[[ "$RING_SECS" =~ ^[0-9]+$ ]] || RING_SECS=1860
[[ "$CHANNELS"  =~ ^[12]$   ]] || CHANNELS=2
[[ "$TRACK_DIR" =~ ^[A-Za-z0-9_-]+$ ]] || TRACK_DIR="t$TRACK_ID"

# Validate seconds: positive integer, clamped to THIS ring's length.
if ! [[ "$SECONDS_BACK" =~ ^[0-9]+$ ]] || [ "$SECONDS_BACK" -lt 1 ]; then
    echo "usage: $(basename "$0") <seconds_back 1-$RING_SECS> [wav|mp3] [track_id]" >&2
    exit 1
fi
[ "$SECONDS_BACK" -gt "$RING_SECS" ] && SECONDS_BACK=$RING_SECS

# Track 0 = legacy top-level ring. PINNED, not resolved: the recorder's
# _legacy_symlinks refuses to point the legacy paths at anything but t0 for
# track 0, and the sampler reads those legacy paths (SAMPLER.RING_PATH), so
# honouring a hand-set dir here would read a DIFFERENT file than the live
# capture path. Tracks 1+ honour their configured dir (2026-07-29) — this
# used to hardcode t<id>, so a track with a hand-set dir failed with a clean
# but wrong "no ring for track N (armed?)".
if [ "$TRACK_ID" = "0" ]; then
    RINGFILE="$SKIPBUF/ring.raw"
    POSFILE="$SKIPBUF/ring.pos"
else
    RINGFILE="$SKIPBUF/$TRACK_DIR/ring.raw"
    POSFILE="$SKIPBUF/$TRACK_DIR/ring.pos"
fi
if [ ! -f "$RINGFILE" ]; then
    echo "skipback: no ring for track $TRACK_ID at $RINGFILE (armed?)" >&2
    exit 1
fi

OUTDIR="${SKIPBACK_OUTDIR:-$HOME/recordings}"
mkdir -p "$OUTDIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
PREFIX="skipback"
[ -n "$TRACK_NAME" ] && PREFIX="$TRACK_NAME"
OUTBASE="${PREFIX}_${SECONDS_BACK}s_$TIMESTAMP"
OUTFILE="$OUTDIR/$OUTBASE.$FORMAT"
# The WAV is built under a hidden .part name in OUTDIR and renamed into place
# only when complete, so nothing ever sees a half-written grab.
PARTWAV="$OUTDIR/.$OUTBASE.wav.part"
# nice + idle IO class: keep the copy and any encode off the synth's back.
NICE="nice -n 15 ionice -c3"

RATE=48000
BYTES_PER_SEC=$((RATE * CHANNELS * 2))
BUFFER_BYTES=$((RATE * CHANNELS * 2 * RING_SECS))
BYTES_NEEDED=$((SECONDS_BACK * BYTES_PER_SEC))

# STREAM [pos-BYTES_NEEDED, pos) out of the wraparound ring straight into the
# WAV on disk, CHUNK bytes at a time (same two-branch wrap math as the
# recorder's writer -- pos is a byte offset flushed every chunk).
#
# Changed 2026-09-28. The old version read the WHOLE span into Python RAM and
# wrote a raw copy to /tmp -- which is tmpfs, i.e. RAM again -- before sox
# wrapped it: a full 20-min stereo grab dipped MemAvailable by 360 MB
# (measured), and a big enough allocation can wedge jackd (CLAUDE.md). Now the
# copy holds one chunk, and the output is fdatasync'd + dropped from the page
# cache every SYNC_EVERY bytes so unwritten pages can't pile up in RAM either.
# The ring (tmpfs) is only READ -- never fadvise'd: its pages are the audio.
#
# Stalled writer (2026-09-29): on a CARD ring the SD card can stall the
# recorder's writer for seconds (measured 7.3 s under heavy card I/O); the
# audio waits safely in its ~43 s RT cushion, but ring.pos stops moving, so
# cutting at pos would end the file EARLY by the stall. So: if pos was last
# flushed more than FRESH_S before the press, wait (up to
# SKIPBACK_FRESH_WAIT s, default 15) for the writer to flush past the press,
# then trim the end back to the press (pos's mtime = when pos was true). A
# fresh pos -- every RAM ring, and a card ring that isn't stalling -- takes
# the old path unchanged. A pos that never moves (recorder down) is grabbed
# as-is after the wait, with a note on stderr.
#
# Review 2026-10-01:
#  * The ring's REAL size + channel count come from the recorder's heads.json
#    when it lists this track (the config can be ~5 s ahead of the live ring
#    after a length/stereo edit, and the wrap maths with the wrong size is
#    garbage). The config values are the fallback.
#  * The span never reaches into the last MARGIN_S before the LIVE write head
#    (plus whatever a stalled writer still owes): a full-ring grab used to
#    start at the head itself, so its first seconds were the NEWEST audio.
#  * Every grab ends at the press (minus HEAD_LAG_S: pos is flushed when a
#    drain ends, its newest audio is a little older), stalled or not, the
#    same as GRAB ALL.
$NICE python3 - "$RINGFILE" "$POSFILE" "$PARTWAV" "$BYTES_NEEDED" "$BUFFER_BYTES" "$CHANNELS" "$RATE" "$SKIPBUF/heads.json" "$TRACK_ID" << 'PYEOF'
import json, os, sys, time, wave

ring_file, pos_file, out_file = sys.argv[1:4]
bytes_needed, buffer_bytes, channels, rate = map(int, sys.argv[4:8])
heads_file, track_id = sys.argv[8], sys.argv[9]
CHUNK = 4 << 20
SYNC_EVERY = 32 << 20
FRESH_S = 0.5
MARGIN_S = 2.0
HEAD_LAG_S = 0.03
# the most a stalled writer can still owe = its RT cushion (recorder
# RB_BYTES / RB_BYTES_CARD); a stopped recorder owes nothing but can't say so
CUSHION_S = {"ram": 11.0, "card": 44.0}
store = "card" if os.path.islink(ring_file) else "ram"
try:
    with open(heads_file) as f:
        _h = json.load(f)["tracks"][track_id]
    if int(_h["channels"]) in (1, 2) and int(_h["buffer_bytes"]) > 0:
        _frames = bytes_needed // (2 * channels)
        channels, buffer_bytes = int(_h["channels"]), int(_h["buffer_bytes"])
        bytes_needed = _frames * 2 * channels
        store = _h.get("store", store)
except Exception:
    pass                    # no snapshot / track not listed: config values
press = float(os.environ.get("SKIPBACK_PRESS_TS") or time.time())
max_wait = float(os.environ.get("SKIPBACK_FRESH_WAIT", "15"))


def read_head():
    """(pos, mtime) -- mtime None when there is no pos file."""
    try:
        st = os.stat(pos_file)
        with open(pos_file) as f:
            return int(f.read().strip()), st.st_mtime
    except Exception:
        return 0, None


pos, mt = read_head()
if mt is not None and mt < press - FRESH_S and max_wait > 0:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(0.05)
        pos, mt = read_head()
        if mt is not None and mt >= press:
            break
    if mt is not None and mt >= press:
        print(f"skipback: recorder was stalled; waited {mt - press:.1f} s and "
              f"cut at the press", file=sys.stderr)
    else:
        print(f"skipback: ring head did not move for {max_wait:.0f} s "
              f"(recorder stopped?) -- grabbing up to the last written audio",
              file=sys.stderr)

frame = 2 * channels
live_pos, live_mt = pos, mt
if mt is not None and mt - HEAD_LAG_S > press:          # end at the press
    back = int((mt - HEAD_LAG_S - press) * rate) * frame
    pos = (pos - min(back, buffer_bytes - frame)) % buffer_bytes
# keep clear of the live head: what it is past our cut + what a stalled
# writer still has to write + MARGIN_S
ahead = (live_pos - pos) % buffer_bytes
# (a fresh head -- flushed every ~43 ms -- owes less than MARGIN_S covers)
stale = time.time() - live_mt if live_mt else 0.0
owed = (int(min(stale, CUSHION_S.get(store, 44.0)) * rate) * frame
        if stale > FRESH_S else 0)
room = buffer_bytes - ahead - owed - int(MARGIN_S * rate) * frame
room -= room % frame
if bytes_needed > room:
    bytes_needed = max(0, room)
    print(f"skipback: trimmed to {bytes_needed // frame / rate:.1f} s to stay "
          f"clear of the live write head", file=sys.stderr)
if bytes_needed <= 0:
    sys.exit("skipback: nothing safe to grab yet")

start = (pos - bytes_needed) % buffer_bytes
if start < pos:
    spans = [(start, bytes_needed)]
else:
    spans = [(start, buffer_bytes - start), (0, pos)]

with open(out_file, "wb") as f:
    w = wave.open(f, "wb")
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.setnframes(sum(n for _, n in spans) // frame)
    unsynced = 0
    with open(ring_file, "rb") as ring:
        for off, n in spans:
            ring.seek(off)
            while n > 0:
                b = ring.read(min(CHUNK, n))
                if not b:
                    break
                w.writeframesraw(b)
                n -= len(b)
                unsynced += len(b)
                if unsynced >= SYNC_EVERY:
                    f.flush()
                    os.fdatasync(f.fileno())
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    unsynced = 0
    w.close()              # patches the header if a short read changed the count
    f.flush()
    os.fdatasync(f.fileno())
    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
PYEOF
[ $? -eq 0 ] || { echo "skipback: copy from ring failed" >&2; rm -f "$PARTWAV"; exit 1; }

if [ "$FORMAT" = "mp3" ]; then
    $NICE lame --cbr -b 192 "$PARTWAV" "$OUTFILE" || { echo "skipback: lame failed" >&2; rm -f "$PARTWAV" "$OUTFILE"; exit 1; }
    rm -f "$PARTWAV"
else
    mv -f "$PARTWAV" "$OUTFILE" || { echo "skipback: rename failed" >&2; rm -f "$PARTWAV"; exit 1; }
fi

echo "Saved: $OUTFILE"
