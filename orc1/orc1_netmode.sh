#!/bin/bash
# orc1_netmode.sh -- switch the Pi between "join a known WiFi" and "be the hotspot".
#   ap       bring the pb-hotspot profile up (SSID/password in that NetworkManager profile)
#   client   drop the hotspot, rejoin the best known WiFi; if none is in range, stay hotspot
#   toggle   ap <-> client (the pisound button, HOLD_3S)
#   auto     boot fallback: if neither wlan0 nor eth0 gets connected in time, ap
#   status   print the mode
# Feedback on the pisound LED: ap = 3 flashes, client = 1 flash, failed / nothing to join = 6 quick.
# Runs as root (pisound-btn and systemd both are). wlan0 is the only radio, so AP mode and
# client mode are exclusive: the hotspot is at 172.24.1.1 (web: http://172.24.1.1:5000).

AP=pb-hotspot
IF=wlan0
WAIT=${ORC1_NETMODE_WAIT:-45}

LOG=/home/pibox/orc1_netmode.log
# own log file, synced per line: the journal is not persistent, and a hang must leave a trail
nlog() { logger -t orc1-netmode "$*"; echo "$*"; echo "$(date +%T) $*" >> $LOG; sync -d $LOG 2>/dev/null; }
run() { local o r; o=$("$@" 2>&1); r=$?; echo "$(date +%T) \$ $* -> $r $o" >> $LOG; sync -d $LOG 2>/dev/null; return $r; }

. /usr/local/pisound/scripts/common/common.sh 2>/dev/null
type flash_leds >/dev/null 2>&1 || flash_leds() { :; }
blink() { local n=$1 gap=$2 i; for ((i = 0; i < n; i++)); do flash_leds 20; sleep "$gap"; done; }

ap_active() { nmcli -t -f NAME con show --active | grep -qx "$AP"; }
connected() {
	# the hotspot itself does not count: `nmcli dev connect` happily re-activates it
	# (it used to carry the top autoconnect priority) and that is not "joined a network"
	[ "$(nmcli -t -g GENERAL.STATE dev show eth0 2>/dev/null | cut -c1-3)" = "100" ] && return 0
	[ "$(nmcli -t -g GENERAL.STATE dev show $IF 2>/dev/null | cut -c1-3)" = "100" ] &&
		[ "$(nmcli -t -g GENERAL.CONNECTION dev show $IF 2>/dev/null)" != "$AP" ] && return 0
	return 1
}

# Bluetooth tether (PANU): join a paired+trusted phone's tethering, if it is on and in range.
# Measured 2026-10-19: gives the Pi internet
# and does not disturb wlan0, so it is tried IN ADDITION to the hotspot, never instead of it.
try_bt() {
	local n a
	while IFS= read -r n; do
		[ -z "$n" ] && continue
		a=$(nmcli -t -g bluetooth.bdaddr con show "$n" 2>/dev/null)
		bluetoothctl info "$a" 2>/dev/null | grep -q "Trusted: yes" || continue
		run nmcli -w 25 con up "$n" && { nlog "bluetooth tether up: $n"; return 0; }
	done < <(nmcli -t -f NAME,TYPE con show | awk -F: '$2=="bluetooth" {print $1}')
	return 1
}

go_ap() {
	if ap_active; then nlog "hotspot already up"; return 0; fi
	nlog "starting hotspot ($AP)"
	if run nmcli con up "$AP"; then blink 3 0.4; nlog "hotspot up"; return 0; fi
	nlog "hotspot FAILED to start"; blink 6 0.15; return 1
}

go_client() {
	nlog "leaving hotspot, joining a known WiFi"
	run nmcli con down "$AP"
	# a just-dropped AP needs a moment before the radio will scan/join
	sleep 5
	local try t
	for try in 1 2 3; do
		# the scan is asynchronous: connecting straight after it uses stale results (measured:
		# a just-restored home SSID was "not there", so NM picked the hotspot instead)
		run nmcli dev wifi rescan ifname $IF
		sleep 7
		# bring up each known WiFi profile by name, best priority first. `dev connect` is not
		# enough: after a manual disconnect NM blocks that profile's autoconnect (so it is
		# skipped) and the hotspot wins. An explicit `con up` clears the block, and only
		# succeeds when that SSID is really in range.
		local name
		while IFS= read -r name; do
			[ -n "$name" ] && run nmcli con up "$name" && break
		done < <(nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY con show | awk -F: -v ap="$AP" '$2=="802-11-wireless" && $1!=ap {print $3 "\t" $1}' | sort -rn | cut -f2-)
		for t in 1 2 3 4 5 6; do
			if connected; then blink 1 0.4; nlog "joined: $(nmcli -t -g GENERAL.CONNECTION dev show $IF)"; return 0; fi
			ap_active && break
			sleep 2
		done
		# landed back on the hotspot (the only thing NM could see): drop it and rescan
		ap_active && { run nmcli con down "$AP"; sleep 5; }
	done
	try_bt
	nlog "no known WiFi in range, back to hotspot"
	blink 6 0.15
	go_ap
	return 1
}

if [ "$1" = status ]; then
	if ap_active; then echo "hotspot ($AP, 172.24.1.1)"; else echo "client: $(nmcli -t -g GENERAL.CONNECTION dev show $IF)"; fi
	exit 0
fi
exec 9>/run/orc1_netmode.lock
flock -n 9 || { nlog "busy, ignoring"; exit 0; }

case "$1" in
	ap) go_ap ;;
	client) go_client ;;
	toggle) if ap_active; then go_client; else go_ap; fi ;;
	auto)
		for ((s = 0; s < WAIT; s += 3)); do connected && { nlog "auto: already connected"; exit 0; }; sleep 3; done
		# one explicit try (a known SSID may just have been slow to scan) before giving up the radio
		nlog "auto: not connected after ${WAIT}s, one more try"
		run nmcli dev connect $IF; sleep 12
		connected && { nlog "auto: joined late"; exit 0; }
		try_bt; nlog "auto: no network, starting hotspot"; go_ap ;;
	status) if ap_active; then echo "hotspot ($AP, 172.24.1.1)"; else echo "client: $(nmcli -t -g GENERAL.CONNECTION dev show $IF)"; fi ;;
	*) echo "usage: $0 ap|client|toggle|auto|status" >&2; exit 2 ;;
esac
