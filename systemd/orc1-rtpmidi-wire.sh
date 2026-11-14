#!/bin/bash
# PROPOSED replacement for /usr/local/bin/orc1-rtpmidi-wire.sh (NOT applied, 2026-11-06).
# Wire ORC1-Synth-Bus output to the PC's ordinary rtpMIDI session on rtpmidid.
# Finds ports by name since client IDs change each restart.
#
# The live script takes rtpmidid's port ":1", i.e. whichever PC session rtpmidid discovered
# FIRST. Once a second session ("ORC DAW", the Mackie Control surface port used by
# orc1_dawsurf_bridge.py) exists, that can be ":1", and then the whole synth bus would be
# wired into REAPER's Mackie Control input (bus notes = MCU button presses: PLAY, RECORD ...).
# This version takes the first rtpmidid session port (not port 0, the announcements port)
# whose name does NOT contain "orc daw" (orc1_config.DAWSURF_PORT_NAMES, lower case).
SKIP="orc daw"
# 2026-11-13: also never "flame rtp" (orc1_config.FLAME_RTP_PORT_NAMES): that session is the Fire's DAW mode
# over the network (orc1_flame_rtp.py), and synth-bus notes there would be Mackie button presses on the DAW.
SKIP2="flame rtp"
for i in $(seq 1 10); do
    SRC=$(aconnect -l 2>/dev/null | awk '/^client/{match($0,/[0-9]+/); c=substr($0,RSTART,RLENGTH)} /ORC1-Synth-Bus/{print c":0"}' | head -1)
    DST=$(aconnect -l 2>/dev/null | awk -v skip="$SKIP" -v skip2="$SKIP2" '
        /^client/ { inrtp = ($0 ~ /rtpmidid/); match($0, /[0-9]+/); c = substr($0, RSTART, RLENGTH); next }
        inrtp && /^[ \t]+[0-9]+ / { n = $1; if (n != 0 && index(tolower($0), skip) == 0 && index(tolower($0), skip2) == 0) { print c ":" n; exit } }')
    if [ -n "$SRC" ] && [ -n "$DST" ]; then
        aconnect "$SRC" "$DST" 2>/dev/null
        if aconnect -l 2>/dev/null | grep "Connecting To:" | grep -qF "$DST"; then
            logger "orc1-rtpmidi: ORC1-Synth-Bus ($SRC) wired to rtpmidid ($DST)"
            exit 0
        fi
    fi
    sleep 1
done
logger "orc1-rtpmidi: wire timed out (rtpmidid session may not be active yet)"
exit 0
