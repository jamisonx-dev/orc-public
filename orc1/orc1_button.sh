#!/bin/bash
# orc1_button.sh -- what the pisound button does for the actions pisound-btn hands us.
#   orc1_button.sh shutdown   any HOLD of 3 s or more: power the Pi off (cleanly)
#   orc1_button.sh netmode    4 quick clicks: hotspot <-> home WiFi (orc1_netmode.sh toggle)
# pisound-btn appends "<clicks> <hold ms> ..." after our own argument. Runs as root.
#
# 2026-10-31: HOLD_3S used to be the network toggle and only HOLD_5S shut down, and the
# daemon decides at RELEASE by hold time (3-5 s / 5-7 s / over 7 s = nothing), so a
# shutdown needed a release inside a 2-second window, and letting go early flipped the
# WiFi to the hotspot instead. Now every hold of 3 s or more is a shutdown.
# Every press is logged to ~/orc1_button.log (synced: the journal does not keep these).

LOG=/home/pibox/orc1_button.log
blog() { echo "$(date +'%F %T') $*" >> "$LOG"; sync -d "$LOG" 2>/dev/null; }

. /usr/local/pisound/scripts/common/common.sh 2>/dev/null
type flash_leds >/dev/null 2>&1 || flash_leds() { :; }

what=$1; shift
blog "button: $what (clicks=${1:-?} hold_ms=${2:-?})"

case "$what" in
shutdown)
	for _ in 1 2 3 4 5 6 7 8 9 10; do flash_leds 1; sleep 0.1; done
	blog "shutting down"
	[ -n "$ORC1_BUTTON_DRYRUN" ] && { blog "dry run: not powering off"; exit 0; }
	sync
	exec systemctl poweroff
	;;
netmode)
	[ -n "$ORC1_BUTTON_DRYRUN" ] && { blog "dry run: not toggling"; exit 0; }
	exec /home/pibox/orc/orc1/orc1_netmode.sh toggle
	;;
*)
	blog "unknown action '$what'"
	exit 1
	;;
esac
