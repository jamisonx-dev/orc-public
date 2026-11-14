#!/bin/bash
# Reap a stale DEFAULT (nameless) jackd before orc1-jackd starts a fresh one.
#
# Replaces the old `pkill -x jackd` (ExecStartPre of orc1-jackd.service), which
# killed EVERY jackd -- including the NAMED per-card servers of the per-host-jackd
# model (jackd -n <card>) -- without ever bringing them back. This kills only a
# jackd whose command line has NO `-n` argument, i.e. the default server, and
# leaves `jackd -n M3` (etc.) alone. Also clears the default server's stale shm,
# matching the old unit's second ExecStartPre (kept there as well).
for p in $(pgrep -x jackd); do
    args="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
    # A vanished/renamed proc reads empty -- NEVER guess "default" and kill; skip.
    [ -z "${args// /}" ] && continue
    # A NAMED server is `jackd -n <card> -dalsa ...`: the server-name `-n` sits in
    # the SERVER args, BEFORE `-dalsa`. The ALSA backend ALSO takes `-n <nperiods>`
    # (the default server is `jackd ... -dalsa -dhw:L12 ... -n 2`), so matching a
    # bare ` -n ` anywhere would spare the default too and reap NOTHING -- silently
    # disabling this safety net. Look only at the prefix before the backend.
    prefix=" ${args%%-dalsa*} "
    case "$prefix" in
        *" -n "*) : ;;                       # server-name -n -> NAMED, leave it
        *)        kill "$p" 2>/dev/null ;;   # no server name -> the default, reap
    esac
done

# Stale shm/sockets (2026-10-23). The unit used to run `rm -f /dev/shm/jack*` here,
# which also deleted every NAMED server's socket (jack_<card>_<uid>_0) and the
# registry all servers share: the per-card server kept running, its connected
# hosts kept playing, but nothing new could join it (measured 2026-10-23: BBKit
# and jack_lsp got "server socket: No such file" on a live `jackd -n pisound`).
# Wipe everything only when NO jackd is left; otherwise only the default
# server's own files.
for _ in 1 2 3 4 5 6 7 8 9 10; do          # let a reaped default jackd exit
    left=0
    for p in $(pgrep -x jackd); do
        a="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
        case " ${a%%-dalsa*} " in *" -n "*) : ;; *) left=1 ;; esac
    done
    [ $left = 0 ] && break
    sleep 0.5
done
if pgrep -x jackd >/dev/null; then
    rm -f /dev/shm/jack_default_* /dev/shm/jack_sem.*_default_* 2>/dev/null
else
    rm -f /dev/shm/jack* /dev/shm/jack_* 2>/dev/null
fi
exit 0
