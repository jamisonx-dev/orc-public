#!/bin/bash
# hud.sh -- ORC-1 HUD: the play-state screen on the HDMI monitor
# (orc1-hud.service, a kiosk on VT7 like VISUALS / DAW / DJ mode).
#
#   sudo hud.sh start     show it (refuses while DAW or DJ mode runs; starting
#                         VISUALS, DAW or DJ replaces it -- they share the monitor)
#   sudo hud.sh stop      blank the monitor again
#   hud.sh shot           save the frame it is showing -> /tmp/orc1_hud_shot.png
#   hud.sh status         one JSON line
#
# Cheap by design: plain 2D drawing, SCHED_IDLE, reads one tmpfs file the brain
# already publishes (orc1_playstate.py). Not a JACK client.
set -u
PIDF=/tmp/orc1_hud.pid
FEED=/dev/shm/orc1_playstate.json

case "${1:-}" in
start)
    for u in orc1-daw orc1-dj; do
        if systemctl is-active --quiet "$u"; then
            echo "$u is running -- the monitor is in use"
            exit 0
        fi
    done
    # stop VISUALS (or anything else on the monitor) and WAIT for its X session to
    # end: Conflicts= alone races the closing session (see kiosk_clear.sh)
    /bin/bash "$(dirname "$(readlink -f "$0")")/kiosk_clear.sh" orc1-hud
    systemctl start orc1-hud
    ;;
stop)
    systemctl stop orc1-hud
    ;;
shot)
    pid=$(cat "$PIDF" 2>/dev/null)
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -USR1 "$pid" 2>/dev/null; then
        sleep 0.4
        echo /tmp/orc1_hud_shot.png
    else
        echo "hud is not running" >&2
        exit 1
    fi
    ;;
status)
    active=false
    systemctl is-active --quiet orc1-hud && active=true
    age=null
    if [ -r "$FEED" ]; then
        age=$(( $(date +%s) - $(stat -c %Y "$FEED") ))
    fi
    printf '{"active":%s,"feed_age_s":%s}\n' "$active" "$age"
    ;;
*)
    echo "usage: hud.sh start | stop | shot | status" >&2
    exit 2
    ;;
esac
