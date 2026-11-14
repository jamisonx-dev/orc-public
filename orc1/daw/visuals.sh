#!/bin/bash
# visuals.sh -- ORC-1 VISUALS mode: line-drawing visuals on the HDMI monitor
# (orc1-visuals.service, a kiosk on VT7 like DAW/DJ mode).
#
#   sudo visuals.sh start       show them (refuses while DAW or DJ mode runs;
#                               starting DAW/DJ mode replaces the visuals)
#   sudo visuals.sh stop        blank the monitor again
#   visuals.sh next             skip to the next scene / pattern now
#   visuals.sh set orc [secs]   OUR scenes, following the music (the default),
#                               cycling every secs (default 60)
#   visuals.sh set tunnel|mystify|tube|harmonograph|warp|hypercube|scope|ridgeline|rose|lorenz|chladni|knot|polytope|hopf|shape4d|logo
#                               hold one of our scenes
#   visuals.sh set classic [secs]
#                               XScreenSaver's line patterns, cycling
#   visuals.sh set <name>       hold one XScreenSaver pattern (e.g. helix)
#   visuals.sh fps 30|45|60     frame rate of our scenes (60 = ~17 ms less
#                               delay, roughly twice the CPU)
#   visuals.sh source <left port> [right port]
#                               what the visuals LISTEN to (any JACK output
#                               port; one port = mono), via orc1-vistap
#   visuals.sh source ring      listen to skipback's RAM ring instead (the
#                               pre-tap way)
#   visuals.sh shot             save the frame on the monitor -> /tmp/orc1_visuals_shot.png
#   visuals.sh list            every XScreenSaver pattern this Pi has
#   visuals.sh status           one JSON line
#
# Cheap by design: the renderer is no JACK client (it READS the MIDI bus, and
# hears audio through orc1-vistap, a tiny C listen-only client, see
# orc_jacktap.c); they draw on the GPU at 30 fps (~28% of one core, measured 2026-09-29); all
# niced, and the unit's memory cap is copied onto the kiosk session
# (cap-session).
set -u
CONF=/home/pibox/.orc1_visuals
PIDF=/tmp/orc1_visuals.pid
SCENES='^(tunnel|mystify|tube|harmonograph|warp|hypercube|scope|ridgeline|rose|lorenz|chladni|knot|polytope|hopf|shape4d|logo)$'

end_current() {
    local pid
    pid=$(cat "$PIDF" 2>/dev/null) || return 0
    [[ "$pid" =~ ^[0-9]+$ ]] && kill "$pid" 2>/dev/null
    return 0
}

write_conf() {             # line 3 (fps) is kept across a scene change
    local fps
    fps=$(sed -n 3p "$CONF" 2>/dev/null)
    printf '%s\n%s\n%s\n' "$1" "$2" "${3:-$fps}" > "$CONF"
    [ "$(id -u)" = 0 ] && chown pibox:pibox "$CONF"
    return 0
}

case "${1:-}" in
start)
    for u in orc1-daw orc1-dj; do
        if systemctl is-active --quiet "$u"; then
            echo "$u is running -- the monitor is in use"
            exit 0
        fi
    done
    # stop the HUD (or anything else on the monitor) and WAIT for its X session to
    # end: Conflicts= alone races the closing session (see kiosk_clear.sh)
    /bin/bash "$(dirname "$(readlink -f "$0")")/kiosk_clear.sh" orc1-visuals
    systemctl start orc1-visuals
    ;;
stop)
    systemctl stop orc1-visuals
    ;;
next)
    if [ "$(cat /tmp/orc1_visuals.now 2>/dev/null)" = orc ]; then
        # our renderer steps to its next scene itself (the trails carry over)
        pid=$(cat "$PIDF" 2>/dev/null)
        [[ "$pid" =~ ^[0-9]+$ ]] && kill -USR1 "$pid" 2>/dev/null
    else
        end_current
    fi
    ;;
set)
    name=${2:-}
    [ -n "$name" ] || { echo "usage: visuals.sh set orc|classic [secs] | <scene or pattern>" >&2; exit 2; }
    if [ "$name" = orc ] || [ "$name" = classic ] || [ "$name" = cycle ]; then
        secs=${3:-60}
        [[ "$secs" =~ ^[0-9]+$ ]] || { echo "seconds must be a number" >&2; exit 2; }
        [ "$name" = cycle ] && name=classic
        write_conf "$name" "$secs"
    elif [[ "$name" =~ $SCENES ]]; then
        write_conf "orc:$name" 0
    else
        [ -x "/usr/libexec/xscreensaver/$name" ] || { echo "no scene or pattern called '$name' (try: visuals.sh list)" >&2; exit 2; }
        write_conf "$name" 0
    fi
    # our renderer running and staying ours: it re-reads the choice in place
    # (no restart = no blank gap, trails carry over); anything else restarts
    if [ "$(cat /tmp/orc1_visuals.now 2>/dev/null)" = orc ] &&
       [[ "$(head -1 "$CONF")" =~ ^orc(:|$) ]]; then
        pid=$(cat "$PIDF" 2>/dev/null)
        [[ "$pid" =~ ^[0-9]+$ ]] && kill -USR2 "$pid" 2>/dev/null
    else
        end_current
    fi
    ;;
fps)
    fps=${2:-}
    [[ "$fps" =~ ^(30|45|60)$ ]] || { echo "usage: visuals.sh fps 30|45|60" >&2; exit 2; }
    m1=$(sed -n 1p "$CONF" 2>/dev/null); m2=$(sed -n 2p "$CONF" 2>/dev/null)
    write_conf "${m1:-orc}" "${m2:-60}" "$fps"
    if [ "$(cat /tmp/orc1_visuals.now 2>/dev/null)" = orc ]; then
        pid=$(cat "$PIDF" 2>/dev/null)
        [[ "$pid" =~ ^[0-9]+$ ]] && kill -USR2 "$pid" 2>/dev/null
    fi
    ;;
source)
    left=${2:-}
    [ -n "$left" ] || { echo "usage: visuals.sh source <left port> [right port] | ring" >&2; exit 2; }
    # orc1-vistap re-reads this within 0.5 s (no restart)
    src=/home/pibox/.orc1_vistap
    printf '%s\n%s\n' "$left" "${3:-}" > "$src.tmp" && mv "$src.tmp" "$src"
    [ "$(id -u)" = 0 ] && chown pibox:pibox "$src"
    exit 0
    ;;
shot)
    # our renderer saves its finished frame (an X grab of the GL window is stale)
    pid=$(cat "$PIDF" 2>/dev/null)
    [[ "$pid" =~ ^[0-9]+$ ]] || { echo "visuals not running" >&2; exit 1; }
    rm -f /tmp/orc1_visuals_shot.png
    kill -WINCH "$pid" 2>/dev/null
    for _ in $(seq 20); do
        [ -s /tmp/orc1_visuals_shot.png ] && { echo /tmp/orc1_visuals_shot.png; exit 0; }
        sleep 0.1
    done
    echo "no frame saved (is it our renderer, not classic?)" >&2; exit 1
    ;;
list)
    echo "our scenes: tunnel mystify tube harmonograph warp hypercube scope ridgeline rose lorenz chladni knot polytope hopf shape4d logo"
    echo -n "classic: "
    ls /usr/libexec/xscreensaver | grep -vE '^(xscreensaver|.*\.(pl|sh))' | tr '\n' ' '
    echo
    ;;
status)
    active=false
    systemctl is-active --quiet orc1-visuals && active=true
    now=null
    $active && [ -r /tmp/orc1_visuals.now ] && now="\"$(cat /tmp/orc1_visuals.now)\""
    mode=orc
    [ -r "$CONF" ] && mode=$(head -1 "$CONF")
    echo "{\"active\": $active, \"pattern\": $now, \"mode\": \"$mode\"}"
    ;;
*)
    echo "usage: visuals.sh start | stop | next | set orc|classic [secs] | set <scene or pattern> | fps N | source <port> [port] | list | status" >&2
    exit 2
    ;;
esac
