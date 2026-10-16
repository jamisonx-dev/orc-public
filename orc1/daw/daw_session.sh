#!/bin/bash
# ORC-1 DAW mode X session (started by orc1-daw.service via xinit).
# Openbox as a minimal window manager, then REAPER in the foreground: when
# REAPER quits, this script ends, xinit tears X down, and the unit stops.
#
# DAW_RES (optional, e.g. 1280x720) lowers the output resolution to save GPU /
# CPU on the Pi; unset = the monitor's preferred mode.

export JACK_NO_START_SERVER=1

xset s off -dpms 2>/dev/null          # no screen blanking mid-edit

if [ -n "${DAW_RES:-}" ]; then
    out=$(xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')
    [ -n "$out" ] && xrandr --output "$out" --mode "$DAW_RES" 2>/dev/null
fi

openbox &
OB=$!

/opt/REAPER/reaper -nosplash
rc=$?

kill "$OB" 2>/dev/null
exit $rc
