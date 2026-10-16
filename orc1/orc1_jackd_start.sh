#!/bin/bash
# ORC-1 JACK server owner (2026-07-14). Standalone: this is the SOLE process
# that starts jackd. orc1-synth, skipback-bridge, pisound-in/out and orc-bt-jack
# all connect to the server this starts (they set JACK_NO_START_SERVER=1). jackd
# runs in the foreground so systemd tracks it directly, and it is NOT PartOf any
# consumer -- so a synth crash/restart can never take jackd (and therefore the
# skipback ring) down. Card-selection logic mirrors the old synth launcher.
DIR="$(cd "$(dirname "$0")" && pwd)"
CHOICE_FILE="$HOME/.orc1_audio.json"
WAIT=90
card_present() { aplay -l 2>/dev/null | grep -q "^card [0-9]*: $1 "; }
list_playback_cards() { aplay -l 2>/dev/null | sed -n 's/^card [0-9]*: \([^ ]*\) .*/\1/p' | sort -u; }
CHOSEN=""; PERIOD_OVERRIDE=""
if [ -f "$CHOICE_FILE" ]; then
    CHOSEN=$(python3 -c "import json;print(json.load(open('$CHOICE_FILE')).get('card',''))" 2>/dev/null)
    PERIOD_OVERRIDE=$(python3 -c "import json;print(json.load(open('$CHOICE_FILE')).get('period',''))" 2>/dev/null)
fi
TARGET=""; elapsed=0
while [ -z "$TARGET" ]; do
    for c in $CHOSEN L12 pisound; do
        if [ -n "$c" ] && card_present "$c"; then TARGET="$c"; break; fi
    done
    [ -n "$TARGET" ] && break
    if [ $elapsed -ge $WAIT ]; then
        TARGET=$(list_playback_cards | grep -v -E '^(Loopback|vc4hdmi.*)$' | grep -v '^Headphones$' | head -1)
        [ -z "$TARGET" ] && TARGET=$(list_playback_cards | grep -v '^Loopback$' | head -1)
        break
    fi
    echo "[orc1-jackd] Waiting for audio card (${CHOSEN:-L12/pisound})... ${elapsed}s"
    sleep 2; elapsed=$((elapsed + 2))
done
[ -z "$TARGET" ] && { echo "[orc1-jackd] No playback card; defaulting to L12"; TARGET="L12"; }
if [ "$TARGET" = "L12" ]; then
    echo "[orc1-jackd] Priming Zoom L-12 playback endpoint..."
    aplay -D hw:L12 -f S32_LE -r 48000 -c 2 /dev/zero & PRIME=$!
    sleep 3; kill $PRIME 2>/dev/null; wait $PRIME 2>/dev/null; sleep 1
fi
PERIOD=512; NPERIODS=2
[ "$TARGET" = "pisound" ] && PERIOD=1024
[ "$TARGET" = "M3" ] && PERIOD=1024 && NPERIODS=3
[ -n "$PERIOD_OVERRIDE" ] && PERIOD="$PERIOD_OVERRIDE"
EXTRA=""
# pisound used to run playback-only (-P, "historical", from the July config when
# a separate pisound-in bridge owned its capture). That bridge is disabled, and
# with -P the fallback card had no inputs at all, so skipback recorded SILENCE
# whenever the L-12 was off. Full duplex since 2026-09-24 (owner): skipback's
# auto source then records the pisound inputs (system:capture_1/2).
# JACK port cap (2026-09-23). jackd reserves shared memory for --port-max ports
# up front: the default 2048 = a 107 MB /dev/shm segment; 256 = 47 MB (both
# MEASURED on this Pi). The rig uses ~31 ports (+~40 with every bridge up).
PORT_MAX="${JACK_PORT_MAX:-256}"
echo "[orc1-jackd] Starting jackd on hw:$TARGET (period $PERIOD, periods $NPERIODS)"
echo "/usr/bin/jackd -R --port-max $PORT_MAX -dalsa -dhw:$TARGET$EXTRA -r 48000 -p $PERIOD -n $NPERIODS" > ~/.jackdrc
exec /usr/bin/jackd -R --port-max "$PORT_MAX" -dalsa -dhw:$TARGET$EXTRA -r 48000 -p $PERIOD -n $NPERIODS
