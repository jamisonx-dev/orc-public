#!/bin/bash
# TC-Helicon Perform-VE <-> JACK bridge (2026-07-30).
#
# The Perform-VE is a fully class-compliant UAC2 device: 8 in / 8 out, 24-bit,
# 48 kHz fixed. No driver or firmware is needed on Linux. It shows up as ALSA
# card "PerformVE".
#
# *** THE QUIRK THIS SCRIPT EXISTS FOR ***
# The Perform-VE only produces capture audio while a USB PLAYBACK stream is
# open. With playback closed:
#   - `arecord` fails outright with "read error: Input/output error"
#   - `alsa_in` starts, logs nothing, and emits PURE DIGITAL SILENCE
# The second failure mode is the dangerous one -- a capture bridge that looks
# perfectly healthy and records nothing. Measured 2026-07-30: 0 nonzero samples
# in 193,536 with playback closed; correct audio the instant playback opened.
# So performve-in MUST NOT start until performve-out is actually streaming.
# Unit ordering alone is not enough (a started process is not an open stream),
# hence the bounded readiness wait below.
#
# Usage: performve_bridge_start.sh in|out
set -u

DIR="${1:-}"
CARD="PerformVE"
RATE=48000
PERIOD=1024
NPERIODS=2
CHANNELS=8            # device is fixed at 8x8; do not lower without testing

if [ "$DIR" != "in" ] && [ "$DIR" != "out" ]; then
    echo "[performve] usage: $0 in|out" >&2
    exit 2
fi

# --- device present? idle rather than crash-loop if it is unplugged ----------
if ! grep -q "\[$CARD" /proc/asound/cards 2>/dev/null; then
    echo "[performve-$DIR] card '$CARD' not present - idling (replug and restart this unit)."
    exec sleep infinity
fi

# --- never start a JACK server; orc1-jackd owns it --------------------------
export JACK_NO_START_SERVER=1

if [ "$DIR" = "out" ]; then
    echo "[performve-out] bridging JACK -> $CARD playback (${CHANNELS}ch @ ${RATE})."
    exec /usr/bin/alsa_out -d "hw:$CARD" -j performve-out \
         -r "$RATE" -p "$PERIOD" -n "$NPERIODS" -c "$CHANNELS"
fi

# --- capture: wait for the playback bridge to really be streaming -----------
# Bounded readiness wait (project convention: poll for the real condition, never
# a blind sleep). We wait for performve-out's JACK ports to exist, which only
# happens once alsa_out has opened the device.
deadline=$(( SECONDS + 30 ))
until jack_lsp 2>/dev/null | grep -q '^performve-out:'; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "[performve-in] performve-out ports never appeared after 30s." >&2
        echo "[performve-in] Capture would return SILENCE without it - refusing to start." >&2
        exit 1
    fi
    sleep 0.5
done
# alsa_out registers ports slightly before its first period is clocked out;
# give the stream a moment to actually run before opening capture.
sleep 1

echo "[performve-in] performve-out is up; bridging $CARD capture -> JACK (${CHANNELS}ch @ ${RATE})."
exec /usr/bin/alsa_in -d "hw:$CARD" -j performve-in \
     -r "$RATE" -p "$PERIOD" -n "$NPERIODS" -c "$CHANNELS"
