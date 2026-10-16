#!/bin/bash
# ORC-1 HUD kiosk X session (started by orc1-hud.service via xinit).
# Full-screen play-state screen on the HDMI monitor: no window manager, no
# browser, no JACK. The renderer (orc_hud.py) only READS the brain's tmpfs
# play-state file. Control it with hud.sh.
#
# HUD_RES (default 1280x720) sets the output resolution; "native" keeps the
# monitor's own mode.

HERE=$(dirname "$(readlink -f "$0")")
PIDF=/tmp/orc1_hud.pid

xset s off -dpms 2>/dev/null
xsetroot -solid black 2>/dev/null
# No window manager = X's default "X" cursor shows on the bare root window.
blank=/tmp/orc1_blank_cursor.xbm
printf '#define b_width 1\n#define b_height 1\nstatic unsigned char b_bits[] = { 0x00 };\n' > "$blank"
xsetroot -cursor "$blank" "$blank" 2>/dev/null

res=${HUD_RES:-1280x720}
if [ "$res" != native ]; then
    out=$(xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')
    [ -n "$out" ] && xrandr --output "$out" --mode "$res" 2>/dev/null
fi

while true; do
    # SCHED_IDLE: below every ordinary process, far below the realtime audio
    # threads (SKIPBACK COMES FIRST).
    chrt --idle 0 python3 "$HERE/orc_hud.py" &
    pid=$!
    echo "$pid" > "$PIDF"
    wait "$pid"
    sleep 1          # a renderer that dies at once must not spin this loop
done
