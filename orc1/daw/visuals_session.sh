#!/bin/bash
# ORC-1 VISUALS mode X session (started by orc1-visuals.service via xinit).
# Full-screen line-drawing visuals on the HDMI monitor: no window manager,
# no JACK, nothing in the audio graph. Control it with visuals.sh.
#
# ~/.orc1_visuals picks what plays (visuals.sh set writes it):
#   line 1: "orc" (the default) = our own GPU scenes (orc_visuals.py), which
#           follow the music on the MIDI bus, cycling; "orc:<scene>" holds one
#           (tunnel, mystify, tube, harmonograph, warp, hypercube, scope,
#           ridgeline, rose, lorenz, chladni, knot, polytope, hopf, shape4d, logo);
#           "classic" (or "cycle") = step through XScreenSaver's CYCLE below;
#           any other word = hold that one XScreenSaver module (e.g. helix)
#   line 2: seconds per scene / pattern while cycling (default 60)
# It is re-read every time a pattern ends, so a change lands on the next one
# (visuals.sh set/next also end the current one at once).
#
# VIS_RES (default 1280x720) lowers the output resolution: XScreenSaver's
# modules draw on the CPU, so their cost grows with the pixel count. VIS_RES=native keeps
# the monitor's own mode.

HERE=$(dirname "$(readlink -f "$0")")
HACKS=/usr/libexec/xscreensaver
CONF=$HOME/.orc1_visuals
PIDF=/tmp/orc1_visuals.pid
# One entry per pattern, with its options. MEASURED 2026-09-29 (Xorg CPU at
# 1280x720): stock qix and vermiculate keep Xorg at ~75% of a core, so qix
# runs as one polygon (3%) and vermiculate is left out; the rest cost 0-25%.
CYCLE=("qix -count 1 -delay 40000" kaleidescope helix xspirograph epicycle
       polytopes attraction halo fadeplot timetunnel euler2d moire2 wander)

xset s off -dpms 2>/dev/null
xsetroot -solid black 2>/dev/null
# No window manager = X's default "X" cursor shows on the bare root window
# whenever nothing covers it (owner saw it at every scene change). A 1x1
# empty bitmap as the root cursor hides it for good.
blank=/tmp/orc1_blank_cursor.xbm
printf '#define b_width 1\n#define b_height 1\nstatic unsigned char b_bits[] = { 0x00 };\n' > "$blank"
xsetroot -cursor "$blank" "$blank" 2>/dev/null

res=${VIS_RES:-1280x720}
if [ "$res" != native ]; then
    out=$(xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')
    [ -n "$out" ] && xrandr --output "$out" --mode "$res" 2>/dev/null
fi

i=0
while true; do
    mode=orc
    secs=60
    if [ -r "$CONF" ]; then
        { read -r m; read -r s; } < "$CONF"
        [ -n "${m:-}" ] && mode=$m
        [[ "${s:-}" =~ ^[0-9]+$ ]] && secs=$s
    fi
    if [ "$mode" = orc ] || [ "${mode#orc:}" != "$mode" ]; then
        scene=
        [ "$mode" != orc ] && scene=${mode#orc:}
        [ "$secs" -gt 0 ] || secs=60
        echo "visuals: orc ${scene:-cycle}"
        echo orc > /tmp/orc1_visuals.now
        # SCHED_IDLE: below every ordinary process, far below the realtime
        # audio threads. One skipback xrun (21:13:38 2026-09-29) landed while
        # an early build of this ran at ~90% of a core; now ~10-18%.
        # shellcheck disable=SC2086
        chrt --idle 0 python3 "$HERE/orc_visuals.py" $scene --secs "$secs" &
        pid=$!
        echo "$pid" > "$PIDF"
        wait "$pid"
        sleep 1
        continue
    fi
    if [ "$mode" = cycle ] || [ "$mode" = classic ]; then
        hack=${CYCLE[i % ${#CYCLE[@]}]}
        i=$((i + 1))
    else
        hack=$mode
        secs=0
    fi
    read -r -a argv <<< "$hack"
    [ -x "$HACKS/${argv[0]}" ] || { echo "visuals: no module '${argv[0]}', using qix"; argv=(qix -count 1); }
    echo "visuals: $hack"
    echo "${hack%% *}" > /tmp/orc1_visuals.now

    xsetroot -solid black 2>/dev/null
    # nice: the audio side is realtime-scheduled anyway, this keeps the
    # patterns behind the brain / web / other ordinary processes too
    nice -n 15 "$HACKS/${argv[0]}" "${argv[@]:1}" -root &
    pid=$!
    echo "$pid" > "$PIDF"
    timer=
    if [ "$secs" -gt 0 ]; then
        (sleep "$secs"; kill "$pid" 2>/dev/null) &
        timer=$!
    fi
    wait "$pid"
    [ -n "$timer" ] && kill "$timer" 2>/dev/null
    sleep 1        # a module that dies at once must not spin this loop
done
