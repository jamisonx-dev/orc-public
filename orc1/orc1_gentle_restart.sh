#!/bin/bash
# orc1_gentle_restart.sh -- restart the ORC brain WITHOUT glitching skipback.
#
# A plain `systemctl restart orc1` stops all six module hosts at the same
# instant (they are PartOf=orc1). MEASURED 2026-09-28: that pile-up of JACK
# clients leaving made skipback-rec miss a deadline (an xrun + ~40 ms gap),
# exactly like starting them together does (hence 70-stagger.conf). This
# stops the hosts one at a time, restarts the brain, then starts the hosts
# one at a time. Verified the same night: 0 new xruns / gaps.
#
# The one-at-a-time loops live in orc1_hosts.sh (shared with DAW/DJ mode and
# the web's "restart synth"). While DAW/DJ engines-off mode has the hosts
# stopped, they are NOT started here (orc1_hosts.sh start skips, exit 4): the
# app's ExecStopPost brings them back when it ends. (Before 2026-10-01 this
# started all six unconditionally, under a running Mixxx / REAPER.) PartOf only
# try-restarts, so restarting orc1 leaves stopped hosts stopped.
#
# usage: sudo orc1_gentle_restart.sh
set -u
HERE=$(dirname "$(readlink -f "$0")")
HOSTS_SH="$HERE/orc1_hosts.sh"
HOSTS="orc1-bbkit orc1-chip orc1-mono orc1-poly orc1-sampler orc1-drum"
/bin/bash "$HOSTS_SH" stop
systemctl restart orc1
/bin/bash "$HOSTS_SH" start
systemctl is-active orc1 $HOSTS | paste -sd" "
