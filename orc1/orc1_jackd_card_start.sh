#!/bin/bash
# Per-card JACK server for the per-host-jackd model (2026-09-22).
#
# Started ON DEMAND by orc1-jackd@<card>.service when a module is routed to a
# non-main card. This is a NAMED jack server (jackd -n <card>), SEPARATE from
# the sole default server (orc1-jackd.service, the main card) -- the two coexist,
# one ALSA device each, NO bridging. A module host attaches to it with
# JACK_SERVER=<card> (jack servername). The module's engine adopts whatever rate
# this server reports (orc1_synth SAMPLE_RATE for the tonal engines; the sampler
# keeps its buffer at 48k and scales its read-step -- see orc1_sampler out_rate).
#
# Rate is READ FROM THE DEVICE: prefer 48000 when the card lists it (so a 48k
# module renders 1:1, no conversion), else the card's native rate (the Mixtrack
# Pro 3 is 44100-only). Deliberately does NOT touch the default server or any
# sibling per-card server: it opens exactly one hw:<card>.
set -u
CARD="${1:?usage: orc1_jackd_card_start.sh <CARD>}"

# STAND-IN (2026-10-23). When the MAIN (nameless) jackd is on this card -- it
# fell back here because the L-12 went away, see orc1_jackd_start.sh -- the card
# is taken. Don't fight for it: stay up as a stand-in so the modules bound to this
# unit still start, find no "<card>" server and fall back to the main one, which
# drives these same jacks. When the main server leaves the card, exit so
# Restart=on-failure brings the real per-card server back. Same idea as the
# pisound-out bridge idling (pisound_out_start.sh).
main_on_card() {
    local p a pre
    for p in $(pgrep -x jackd); do
        a="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
        pre=" ${a%%-dalsa*} "
        case "$pre" in *" -n "*) continue ;; esac      # a named (per-card) server
        case " $a " in *" -dhw:$CARD "*) return 0 ;; esac
    done
    return 1
}
if main_on_card; then
    echo "[orc1-jackd@$CARD] the main jackd is on hw:$CARD -- standing in until it leaves"
    while main_on_card; do sleep 2; done
    echo "[orc1-jackd@$CARD] main jackd left hw:$CARD -- restarting as the real server"
    exit 1
fi

# Card index, anchored on the closing bracket so "M3" never matches "M3Pro".
IDX="$(awk -v c="$CARD" '$0 ~ "^ *[0-9]+ \\["c" *\\]" {print $1; exit}' \
        /proc/asound/cards 2>/dev/null)"

# --- rate: prefer 48000 if supported, else the device's own (reuses the exact
# detection from orc1_bridge_start.sh; a discrete list or a continuous range) ---
RATE=48000
STREAM="/proc/asound/card${IDX}/stream0"
if [ -n "${IDX:-}" ] && [ -f "$STREAM" ]; then
  FOUND_R="$(awk '
      /^Playback:/ {inSect=1; next}
      /^[A-Z][a-z]+:/ {inSect=0}
      inSect && /Rates:/ {
        r=$0; sub(/.*Rates:/,"",r);
        isrange=(r ~ /-/); nf=0; delete nums;
        while (match(r,/[0-9]+/)) { nums[nf++]=substr(r,RSTART,RLENGTH)+0; r=substr(r,RSTART+RLENGTH); }
        for(i=0;i<nf;i++){ v=nums[i]; if(v==48000)has48=1; if(v>maxv)maxv=v; }
        if(isrange && nf>=2){ lo=nums[0]; hi=nums[nf-1]; if(lo<=48000 && 48000<=hi)has48=1; if(hi>maxv)maxv=hi; }
      }
      END{ if(has48)print 48000; else if(maxv>0)print maxv; }' "$STREAM")"
  [ -n "$FOUND_R" ] && RATE="$FOUND_R"
fi

# Buffering mirrors the main launcher's known-good values.
PERIOD=1024
NPERIODS=2
[ "$CARD" = "M3" ]      && NPERIODS=3      # USB full-speed controller
[ "$CARD" = "pisound" ] && NPERIODS=2

echo "[orc1-jackd@$CARD] jackd -n $CARD on hw:$CARD (rate $RATE, period $PERIOD, nperiods $NPERIODS)"
# JACK port cap (2026-09-23). jackd reserves shared memory for --port-max ports
# up front: the default 2048 = a 107 MB /dev/shm segment; 256 = 47 MB (both
# MEASURED on this Pi). The rig uses ~31 ports (+~40 with every bridge up).
PORT_MAX="${JACK_PORT_MAX:-256}"
exec /usr/bin/jackd -n "$CARD" -R --port-max "$PORT_MAX" -dalsa -dhw:"$CARD" -r "$RATE" -p "$PERIOD" -n "$NPERIODS"
