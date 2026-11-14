#!/bin/bash
# orc1_hosts.sh -- stop / start / restart the six module hosts ONE AT A TIME.
#
#   sudo orc1_hosts.sh stop           stop them, 0.7 s apart
#   sudo orc1_hosts.sh start [force]  start them, 1 s apart (each unit's
#                                     70-stagger.conf adds its own 0.7 s)
#   sudo orc1_hosts.sh restart        stop, then start
#
# SKIPBACK COMES FIRST. MEASURED 2026-09-28: six JACK clients leaving (or
# joining) the graph at the same instant made skipback-rec miss a deadline (an
# xrun + ~40 ms gap). One `systemctl stop|restart` naming all six does exactly
# that -- 70-stagger.conf only spaces out STARTS. So every sequence that moves
# all six goes through here: orc1_gentle_restart.sh, daw/daw_mode.sh (engines
# off + restore) and the web's "restart synth" (/api/restart/synth).
#
# DAW/DJ engines-off: while /run/orc1-daw-engines-off exists, REAPER / Mixxx
# have the engines' CPU + RAM and the app's ExecStopPost (daw_mode.sh restore)
# brings them back. So `start` and `restart` do NOTHING then and exit 4 --
# only `start force` (that restore) starts them.
#
# One sequence at a time (flock on /run/orc1-hosts.lock): two overlapping
# loops would interleave stops and starts (e.g. a web restart running while
# DAW mode stops the engines). A second caller waits for the first.
#
# exit: 0 done, 1 a unit failed (the rest were still tried), 2 usage,
#       4 skipped (engines-off), 75 the lock stayed busy for 60 s
set -u
RUN=${ORC1_RUN_DIR:-/run}               # tests point this at a temp dir
MARK=$RUN/orc1-daw-engines-off          # made / removed by daw/daw_mode.sh
LOCK=$RUN/orc1-hosts.lock
STOP_ORDER="orc1-bbkit orc1-chip orc1-mono orc1-poly orc1-sampler orc1-drum"
START_ORDER="orc1-drum orc1-sampler orc1-poly orc1-chip orc1-mono orc1-bbkit"

case "${1:-}" in
stop|start|restart) ;;
*) echo "usage: orc1_hosts.sh stop | start [force] | restart" >&2; exit 2 ;;
esac

if [ "${ORC1_HOSTS_LOCKED:-}" != "$LOCK" ]; then
    exec env ORC1_HOSTS_LOCKED="$LOCK" flock -w 60 -E 75 "$LOCK" \
        /bin/bash "$(readlink -f "$0")" "$@"
fi

rc=0
stop_all() {
    for h in $STOP_ORDER; do
        systemctl stop "$h" || rc=1
        sleep 0.7
    done
}
start_all() {
    for h in $START_ORDER; do
        systemctl start "$h" || rc=1
        sleep 1
    done
}
engines_off() {
    [ -e "$MARK" ] || return 1
    echo "engines are OFF for DAW/DJ mode -- not starting them (they come back when it ends)"
    return 0
}

case "$1" in
stop)
    stop_all
    ;;
start)
    if [ "${2:-}" != force ] && engines_off; then exit 4; fi
    start_all
    ;;
restart)
    engines_off && exit 4
    stop_all
    start_all
    ;;
esac
exit $rc
