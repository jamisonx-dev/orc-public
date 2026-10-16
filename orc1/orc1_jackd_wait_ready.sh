#!/bin/sh
# ExecStartPost for orc1-jackd (2026-09-24): hold the unit in "activating"
# until jackd actually ANSWERS, so everything ordered After= it (the six module
# hosts, skipback-record, bt-route) starts once, against a live server.
#
# WHY: orc1-jackd is Type=simple, so systemd called it "started" the instant
# orc1_jackd_start.sh launched -- but on the L-12 the script primes the card
# for ~4 s before exec'ing jackd. Every host's first start hit "server_failed",
# exited, and was restarted 5 s later (measured: each module started twice per
# jackd restart, and the late second wave collided with an fx-insert restart).
#
# Never fails the unit: after 80 s it releases the dependents anyway (the old
# behaviour), so a card that never shows up can't wedge the boot.
export JACK_NO_START_SERVER=1
i=0
while [ $i -lt 160 ]; do
    jack_lsp >/dev/null 2>&1 && exit 0
    sleep 0.5
    i=$((i + 1))
done
echo "[orc1-jackd] jackd not answering after 80 s -- releasing dependents anyway"
exit 0
