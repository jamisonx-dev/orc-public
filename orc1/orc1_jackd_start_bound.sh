#!/bin/bash
# ExecStartPost (as root) for orc1-jackd and orc1-jackd@<card> (2026-10-23):
# start every ENABLED unit bound to this server that is down.
#
#   orc1_jackd_start_bound.sh orc1-jackd.service
#   orc1_jackd_start_bound.sh orc1-jackd@pisound.service
#
# WHY (measured 2026-10-23): skipback-record, the sampler and the module hosts
# are BindsTo= their jackd. When jackd stops they stop; systemd queues their
# start again, but if that jackd start FAILS the queued starts fail with result
# 'dependency' and are never retried -- jackd's own Restart= later brought it back
# on the L-12 and skipback stayed down until it was started by hand. Running this
# on every successful server start puts back whatever that start should have.
#
# For the MAIN server it also starts the units of every per-card server named in
# a module drop-in (50-jack-server.conf): orc1_jackd_start.sh may have stopped
# such a server to take its card, which stopped its modules too; starting them
# pulls the server back up as a stand-in (orc1_jackd_card_start.sh) and they
# fall back to this one.
#
# Never fails the unit (always exit 0): it is a helper, not a gate.
UNIT="${1:?usage: orc1_jackd_start_bound.sh <jackd unit>}"
log() { echo "[jackd-bound] $*"; }

main_on_card() {   # same test as orc1_jackd_card_start.sh
    local p a pre
    for p in $(pgrep -x jackd); do
        a="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
        pre=" ${a%%-dalsa*} "
        case "$pre" in *" -n "*) continue ;; esac
        case " $a " in *" -dhw:$1 "*) return 0 ;; esac
    done
    return 1
}

UNITS="$(systemctl show -p BoundBy --value "$UNIT")"
case "$UNIT" in
    orc1-jackd@*)
        CARD="${UNIT#orc1-jackd@}"; CARD="${CARD%.service}"
        if ! main_on_card "$CARD"; then
            # A real per-card server: wait until it answers, or a host started now
            # finds no server and falls back to the MAIN one (the wrong card).
            i=0
            until runuser -u pibox -- env JACK_NO_START_SERVER=1 jack_lsp -s "$CARD" >/dev/null 2>&1; do
                i=$((i + 1))
                [ $i -ge 60 ] && { log "$CARD server not answering after 30 s -- starting its units anyway"; break; }
                sleep 0.5
            done
        fi
        ;;
    *)
        for s in $(grep -hoE 'JACK_SERVER=\S+' /etc/systemd/system/orc1-*.service.d/50-jack-server.conf 2>/dev/null \
                   | cut -d= -f2 | sort -u); do
            UNITS="$UNITS $(systemctl show -p BoundBy --value "orc1-jackd@$s.service")"
        done
        ;;
esac

for u in $(echo $UNITS | tr ' ' '\n' | sort -u); do   # bbkit can be bound to both servers
    case "$u" in *.target) continue ;; esac
    [ "$(systemctl is-enabled "$u" 2>/dev/null)" = "enabled" ] || continue
    systemctl is-active --quiet "$u" && continue
    [ "$(systemctl is-active "$u" 2>/dev/null)" = "activating" ] && continue
    log "$UNIT up: starting $u"
    systemctl --no-block start "$u"
done
exit 0
