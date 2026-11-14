#!/bin/bash
# Generic ALSA <-> JACK bridge for one card, one direction.
#
# Invoked by orc1-bridge@.service as:  orc1_bridge_start.sh <CARD>.<in|out>
# e.g.  orc1_bridge_start.sh M3.out
#
# WHY THIS EXISTS
# ---------------
# jackd owns exactly ONE sound card. Every other audio interface plugged into
# the Pi is invisible to JACK -- and therefore invisible to the patchbay, the
# skipback recorder, and everything else -- until a bridge process pulls it in.
# This box has ten ALSA cards and JACK could see one and a half of them.
#
# Channel count is READ FROM THE DEVICE (/proc/asound/cardN/stream0), never
# assumed. alsa_in/alsa_out default to 2 channels, so an unspecified 8-channel
# device would silently appear as a stereo pair and its other six channels
# would simply not exist, with no error anywhere.
#
# COST, deliberately not hidden: a bridge resamples against that device's own
# clock, which is real CPU (~12% for an 8-channel device on this Pi) and a
# second clock domain. That is why bridges are started on demand from /patch
# rather than all mounted at boot.
set -u

INST="${1:?usage: orc1_bridge_start.sh <CARD>.<in|out>}"
CARD="${INST%.*}"
DIR="${INST##*.}"

if [ "$DIR" != "in" ] && [ "$DIR" != "out" ]; then
  echo "bad direction '$DIR' (want in or out)" >&2
  exit 2
fi

# Resolve the card index so we can read its stream descriptor. Both patterns
# are anchored on the CLOSING bracket: without it, "M3" would also match a card
# named "M3Pro" and we would read the wrong device's channel count -- which
# surfaces as an 8-channel box silently appearing as a stereo pair, the exact
# failure this script's channel detection exists to prevent.
IDX="$(awk -v c="$CARD" '$0 ~ "^ *[0-9]+ \\["c" *\\]" {print $1; exit}' /proc/asound/cards 2>/dev/null)"
if [ -z "$IDX" ]; then
  IDX="$(grep -E "^ *[0-9]+ \[$CARD *\]" /proc/asound/cards | head -1 | sed -E 's/^ *([0-9]+).*/\1/')"
fi

CH=2
RATE=48000
STREAM="/proc/asound/card${IDX}/stream0"
if [ -n "${IDX:-}" ] && [ -f "$STREAM" ]; then
  # The file lists a "Playback:" and/or "Capture:" section, each with one or
  # more "Channels: N" lines (one per altsetting). Take the LARGEST in the
  # section we care about -- that is what the device can actually carry.
  SECT="Playback"
  [ "$DIR" = "in" ] && SECT="Capture"
  FOUND="$(awk -v s="$SECT" '
      $0 ~ "^"s":" {inSect=1; next}
      /^[A-Z][a-z]+:/ {inSect=0}
      inSect && /Channels:/ {gsub(/[^0-9]/,"",$0); if ($0+0>m) m=$0+0}
      END {if (m>0) print m}' "$STREAM")"
  [ -n "$FOUND" ] && CH="$FOUND"
  echo "[bridge] $CARD $DIR: device reports $CH channel(s)"

  # SAMPLE RATE is READ FROM THE DEVICE, never assumed. Unlike the old
  # alsa_in/alsa_out tools, zita REFUSES to open a card at a rate it does not
  # support and dies with "Can't open ALSA playback device" -- so a card that
  # only does 44100 (e.g. the Numark MixTrack Pro 3) must be opened at 44100,
  # and zita resamples between JACK (48000) and that rate. Prefer 48000 when the
  # device lists it (then zita only clock-syncs, no rate conversion); else take
  # the device's own rate. Handles a discrete list ("44100, 48000") and a
  # continuous range ("8000 - 96000").
  FOUND_R="$(awk -v s="$SECT" '
      $0 ~ "^"s":" {inSect=1; next}
      /^[A-Z][a-z]+:/ {inSect=0}
      inSect && /Rates:/ {
        r=$0; sub(/.*Rates:/,"",r);
        isrange=(r ~ /-/);
        nf=0; delete nums;
        while (match(r,/[0-9]+/)) { nums[nf++]=substr(r,RSTART,RLENGTH)+0; r=substr(r,RSTART+RLENGTH); }
        for(i=0;i<nf;i++){ v=nums[i]; if(v==48000)has48=1; if(v>maxv)maxv=v; }
        if(isrange && nf>=2){ lo=nums[0]; hi=nums[nf-1]; if(lo<=48000 && 48000<=hi)has48=1; if(hi>maxv)maxv=hi; }
      }
      END{ if(has48)print 48000; else if(maxv>0)print maxv; }' "$STREAM")"
  [ -n "$FOUND_R" ] && RATE="$FOUND_R"
  echo "[bridge] $CARD $DIR: using rate $RATE Hz"
else
  echo "[bridge] $CARD $DIR: no stream0 (not USB audio) -- using $CH channels"
fi

# JACK client name: lowercase card + direction. Stable and predictable, so the
# patchbay can map a card to its ports without a lookup table that could rot.
CLIENT="$(echo "$CARD" | tr '[:upper:]' '[:lower:]')-$DIR"

# RESAMPLER: zita-ajbridge, NOT the alsa_in/alsa_out example tools.
# A second sound card runs on its own free-running clock (this box has no word
# clock between cards), so anything crossing to the non-jackd card MUST be
# async-resampled. The alsa_out/alsa_in resampler drifts AUDIBLY -- garbled,
# glitchy sustained notes with ZERO jack xruns, because the artifact lives in
# the bridge, not the graph. zita's resampler holds the drift cleanly. Same
# JACK client name and playback_/capture_ port names, so a saved /patch
# reconnects untouched. (First hit on pisound-out 2026-08-09, then M3-out here.)
BIN="/usr/bin/zita-j2a"
[ "$DIR" = "in" ] && BIN="/usr/bin/zita-a2j"

echo "[bridge] exec $BIN -d hw:$CARD -j $CLIENT -r $RATE -c $CH"
exec "$BIN" -d "hw:$CARD" -j "$CLIENT" -r "$RATE" -p 1024 -n 4 -c "$CH"
