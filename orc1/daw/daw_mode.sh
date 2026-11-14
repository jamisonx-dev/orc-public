#!/bin/bash
# daw_mode.sh -- enter / leave ORC-1 DAW mode (REAPER) or DJ mode (Mixxx) on
# the HDMI monitor. Both are X kiosk units on VT7, so only one runs at a time.
#
#   sudo daw_mode.sh start [dj] [engines-on]
#                         DAW (REAPER, orc1-daw) or, with `dj`, DJ mode
#                         (Mixxx + the Mixtrack, orc1-dj). By DEFAULT the six
#                         module hosts are stopped first (frees CPU + RAM);
#                         they come back by themselves when the app ends.
#                         engines-on keeps them running. MEASURED 2026-09-29:
#                         Mixxx + all six engines swapped the Pi and skipback
#                         itself missed deadlines (12 gaps) -- so off is the
#                         default (owner), for REAPER too.
#   sudo daw_mode.sh stop ask the running app to quit (daw_close.py): it asks
#                         about unsaved edits / a playing deck ON THE MONITOR;
#                         exit 3 = still open after 15 s
#   sudo daw_mode.sh stop force
#                         kill it (unsaved edits lost, and the abrupt JACK
#                         exit costs one xrun)
#   sudo daw_mode.sh restore
#                         bring the hosts back if engines-off stopped them
#                         (both units' ExecStopPost run this, so it fires
#                         however the app ends: quit, crash, STOP, X failing)
#   sudo daw_mode.sh mixtrack release|reclaim
#                         hand the Mixtrack to Mixxx / back to the ORC (the
#                         orc1-dj unit runs these around Mixxx)
#   sudo daw_mode.sh cap-session <unit>
#                         copy <unit>'s MemoryHigh/MemoryMax onto the logind
#                         session on tty7 (orc1-kiosk-cap@<kiosk>.service runs
#                         this; never a kiosk ExecStartPost, see that unit).
#                         PAMName=login makes pam_systemd MOVE the app into
#                         user-1000.slice/session-N.scope, so the unit's own
#                         memory caps never touch it (measured 2026-09-29:
#                         orc1-dj charged 1 MB while Mixxx sat at 786 MB).
#   daw_mode.sh status    one JSON line (no root needed). "busy": true while
#                         a start/stop is running or the app's unit is
#                         activating / deactivating (the host stop, the
#                         ExecStopPost restore): the
#                         /system page keeps every button disabled then.
#
# ONE start/stop at a time (2026-10-01): `start` and `stop` hold
# /run/orc1-daw.lock for their whole run, and refuse while the unit is
# activating/deactivating (is-active is false then, which used to let a second
# START slip into the ~4 s host stop / ~20 s restore
# windows). The unit HOOKS (mixtrack, cap-session, restore) never take that
# lock: `start` holds it across `systemctl start <unit>`, which waits for the
# unit's ExecStartPre/Post -- a hook taking it would deadlock. The host loops
# themselves are serialised separately by orc1_hosts.sh's own lock.
#
# SKIPBACK COMES FIRST: the hosts go down and come back ONE AT A TIME, same
# spacing as orc1_gentle_restart.sh. MEASURED 2026-09-28: stopping all six at
# once made skipback-rec miss a deadline. skipback-record, jackd and the brain
# are never touched here.
set -u
HERE=$(dirname "$(readlink -f "$0")")
RUN=${ORC1_RUN_DIR:-/run}               # tests point these at a temp dir
PROC=${ORC1_PROC:-/proc}
MARK=$RUN/orc1-daw-engines-off          # tmpfs: a reboot starts the hosts anyway
DJ_MARK=$RUN/orc1-dj-active             # the brain leaves the Mixtrack alone
LOCK=$RUN/orc1-daw.lock                 # start / stop only (see above)
LOCK_WAIT=${ORC1_DAW_LOCK_WAIT:-3}
# the one-at-a-time host loops (shared with orc1_gentle_restart.sh + the web)
HOSTS_SH="$HERE/../orc1_hosts.sh"

hdmi() {
    for s in /sys/class/drm/card*-HDMI-A-*/status; do
        [ "$(cat "$s" 2>/dev/null)" = connected ] && { echo true; return; }
    done
    echo false
}

unit_state() {
    systemctl show -p ActiveState --value "$1" 2>/dev/null
}

running_unit() {
    # any state but inactive/failed counts: activating / deactivating are the
    # windows a second START must not slip into
    for u in orc1-daw orc1-dj; do
        case "$(unit_state "$u")" in
        ""|inactive|failed) ;;
        *) echo "$u"; return ;;
        esac
    done
}

lock_held() {
    # a shared probe: fails (76) only while start/stop holds the lock
    [ -e "$LOCK" ] || return 1
    flock -n -s -E 76 "$LOCK" true 2>/dev/null
    [ $? -eq 76 ]
}

poke_brain() {
    # apply the Mixtrack marker now instead of on the brain's next 3 s check;
    # the brain being down is fine (it reads the marker when it starts)
    python3 - <<'EOF' 2>/dev/null || true
import json, socket
s = socket.socket(socket.AF_UNIX)
s.settimeout(1.0)
s.connect("/home/pibox/.orc1_cmd.sock")
s.sendall((json.dumps({"action": "MIXTRACK_SYNC"}) + "\n").encode())
s.shutdown(socket.SHUT_WR)
s.recv(64)
EOF
}

case "${1:-}" in
start|stop)
    if [ "${ORC1_DAW_LOCKED:-}" != "$LOCK" ]; then
        ORC1_DAW_LOCKED="$LOCK" flock -w "$LOCK_WAIT" -E 76 "$LOCK" \
            /bin/bash "$(readlink -f "$0")" "$@"
        rc=$?
        [ $rc -eq 76 ] && echo "busy: another DAW/DJ start or stop is running" >&2
        exit $rc
    fi
    ;;
esac

case "${1:-}" in
start)
    shift
    unit=orc1-daw
    off=true
    for a in "$@"; do
        case "$a" in
        dj) unit=orc1-dj ;;
        engines-off) off=true ;;          # the default; kept for old callers
        engines-on) off=false ;;
        *) echo "unknown start option: $a" >&2; exit 2 ;;
        esac
    done
    cur=$(running_unit)
    if [ -n "$cur" ]; then
        st=$(unit_state "$cur")
        if [ "$st" = active ]; then
            echo "$cur is already running -- stop it first"
            exit 0
        fi
        echo "busy: $cur is $st -- try again when it has finished" >&2
        exit 75
    fi
    if $off; then
        touch "$MARK"
        /bin/bash "$HOSTS_SH" stop
    fi
    # VISUALS / the HUD share this monitor: stop them and wait for their X session
    # to end first (Conflicts= alone races the closing session, kiosk_clear.sh)
    /bin/bash "$HERE/kiosk_clear.sh" "$unit"
    if ! systemctl start "$unit"; then
        echo "$unit did not start" >&2
        # its ExecStopPost restores the engines; this covers a start job that
        # never ran the unit at all (restore is a no-op once the marker's gone)
        [ -n "$(running_unit)" ] || /bin/bash "$HERE/daw_mode.sh" restore
        exit 1
    fi
    ;;
stop)
    # Ask the app to quit like its close box does (it closes JACK cleanly and
    # asks about unsaved edits on the monitor). NOT systemctl stop first:
    # SIGTERM killed REAPER mid-cycle = an xrun (measured 2026-09-29).
    # `stop force` skips the ask. Either way the unit's ExecStopPost runs.
    unit=$(running_unit)
    [ -n "$unit" ] || { echo "neither DAW nor DJ mode is running"; exit 0; }
    case "$(unit_state "$unit")" in
    deactivating) echo "$unit is already stopping"; exit 0 ;;
    activating) echo "busy: $unit is still starting -- try again in a moment" >&2
                exit 75 ;;
    esac
    cls=REAPER
    [ "$unit" = orc1-dj ] && cls=mixxx
    if [ "${2:-}" != force ] && runuser -u pibox -- env DISPLAY=:0 \
            XAUTHORITY=/home/pibox/.Xauthority python3 "$HERE/daw_close.py" "$cls"; then
        for _ in $(seq 30); do
            [ "$(unit_state "$unit")" = active ] || exit 0
            sleep 0.5
        done
        echo "still open: it is probably asking something on the monitor"
        exit 3
    fi
    systemctl stop "$unit"
    ;;
restore)
    [ -e "$MARK" ] || exit 0
    # the marker goes AFTER the hosts are back: a restore cut short leaves it,
    # so the next restore (and health's "OFF (DAW/DJ)") still knows
    /bin/bash "$HOSTS_SH" start force
    rm -f "$MARK"
    ;;
mixtrack)
    case "${2:-}" in
    release) touch "$DJ_MARK" ;;
    reclaim) rm -f "$DJ_MARK" ;;
    *) echo "usage: daw_mode.sh mixtrack release|reclaim" >&2; exit 2 ;;
    esac
    poke_brain
    ;;
cap-session)
    unit="${2:-}"
    [ -n "$unit" ] || { echo "usage: daw_mode.sh cap-session <unit>" >&2; exit 2; }
    high=$(systemctl show -p MemoryHigh --value "$unit")
    max=$(systemctl show -p MemoryMax --value "$unit")
    [ "$high" = infinity ] && [ "$max" = infinity ] && exit 0
    # NEVER fail (it used to be an ExecStartPost, where failing stopped the app).
    # The scope is found from the unit's OWN main process (pam_systemd moves it
    # into session-N.scope a moment after the fork): the first tty7 session in
    # loginctl can be the previous kiosk's, still closing.
    scope=
    for _ in $(seq 40); do
        pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null)
        if [ -n "$pid" ] && [ "$pid" != 0 ]; then
            scope=$(sed -n 's|^0::.*/\(session-[0-9]*\.scope\)$|\1|p' \
                    "$PROC/$pid/cgroup" 2>/dev/null)
            [ -n "$scope" ] && break
        fi
        sleep 0.25
    done
    if [ -z "$scope" ]; then
        # fallback: a tty7 session that is not closing
        for sid in $(loginctl list-sessions --no-legend 2>/dev/null |
                     awk '/(^| )tty7( |$)/ {print $1}'); do
            [ "$(loginctl show-session -p State --value "$sid" 2>/dev/null)" = closing ] &&
                continue
            scope=session-$sid.scope
            break
        done
    fi
    if [ -z "$scope" ]; then
        echo "cap-session: no session scope found -- $unit runs UNCAPPED" >&2
    elif systemctl set-property --runtime "$scope" \
            MemoryHigh="$high" MemoryMax="$max"; then
        echo "cap-session: $scope MemoryHigh=$high MemoryMax=$max"
    else
        echo "cap-session: set-property on $scope failed -- $unit runs UNCAPPED" >&2
    fi
    exit 0
    ;;
status)
    unit=$(running_unit)
    st=inactive
    [ -n "$unit" ] && st=$(unit_state "$unit")
    busy=false
    case "$st" in activating|deactivating|reloading) busy=true ;; esac
    lock_held && busy=true
    app=null
    [ "$unit" = orc1-daw ] && app='"reaper"'
    [ "$unit" = orc1-dj ] && app='"mixxx"'
    active=false
    [ -n "$unit" ] && active=true
    off=false
    [ -e "$MARK" ] && off=true
    dj=false
    [ -e "$DJ_MARK" ] && dj=true
    echo "{\"active\": $active, \"app\": $app, \"state\": \"$st\", \"busy\": $busy, \"engines_off\": $off, \"mixtrack_to_mixxx\": $dj, \"hdmi\": $(hdmi)}"
    ;;
*)
    echo "usage: daw_mode.sh start [dj] [engines-on] | stop [force] | restore | mixtrack release|reclaim | cap-session <unit> | status" >&2
    exit 2
    ;;
esac
