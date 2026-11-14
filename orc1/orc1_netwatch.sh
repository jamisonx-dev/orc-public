#!/bin/bash
# orc1_netwatch.sh -- runtime half of the WiFi fallback chain (the boot half is orc1-netfallback).
# If the Pi has had NO network (no ethernet, no joined WiFi) for DOWN_AFTER seconds while it is NOT
# the hotspot, run `orc1_netmode.sh client`: try every saved WiFi (best priority first, a phone
# hotspot saved as low priority included), and if none is in range fall back to the hotspot.
# Never touches a Pi that is the hotspot, is mid-join (orc1_netroot.py), or is connected.
IF=wlan0
POLL=15
DOWN_AFTER=${ORC1_NETWATCH_DOWN:-120}
NETMODE=/home/pibox/orc/orc1/orc1_netmode.sh
JOB=/run/orc1_net_job.json
LOG=/home/pibox/orc1_netmode.log
st() { nmcli -t -g GENERAL.STATE dev show "$1" 2>/dev/null | cut -c1-3; }
conn() { nmcli -t -g GENERAL.CONNECTION dev show $IF 2>/dev/null; }
online() {
	[ "$(st eth0)" = 100 ] && return 0
	[ "$(st $IF)" = 100 ] && [ "$(conn)" != pb-hotspot ] && return 0
	return 1
}
down=0
while sleep $POLL; do
	if online || [ "$(conn)" = pb-hotspot ]; then down=0; continue; fi
	# a join job in the last 3 minutes owns the radio
	if [ -e $JOB ] && [ $(( $(date +%s) - $(stat -c %Y $JOB) )) -lt 180 ]; then down=0; continue; fi
	down=$((down + POLL))
	if [ $down -ge $DOWN_AFTER ]; then
		echo "$(date +%T) netwatch: no network for ${down}s -> netmode client" >> $LOG
		"$NETMODE" client
		down=0
	fi
done
