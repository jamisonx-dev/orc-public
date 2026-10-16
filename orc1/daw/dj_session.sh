#!/bin/bash
# ORC-1 DJ mode X session (started by orc1-dj.service via xinit).
# Openbox as a minimal window manager, then Mixxx fullscreen in the
# foreground: when Mixxx quits, this script ends, xinit tears X down, and the
# unit stops (its ExecStopPost hands the Mixtrack back to the ORC).
#
# DAW_RES (optional, e.g. 1280x720) lowers the output resolution to save GPU /
# CPU on the Pi; unset = the monitor's preferred mode.
# MIXXX_ARGS (optional) extra flags, e.g. --safe-mode if OpenGL waveforms
# misbehave on the Pi's GPU.

export JACK_NO_START_SERVER=1

xset s off -dpms 2>/dev/null          # no screen blanking mid-set

if [ -n "${DAW_RES:-}" ]; then
    out=$(xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')
    [ -n "$out" ] && xrandr --output "$out" --mode "$DAW_RES" 2>/dev/null
fi

openbox &
OB=$!

# shellcheck disable=SC2086
mixxx --full-screen ${MIXXX_ARGS:-}
rc=$?

kill "$OB" 2>/dev/null
exit $rc
