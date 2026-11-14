#!/bin/bash
# orc1_netmode.sh -- switch the Pi between "join a known WiFi" and "be the hotspot".
#   ap       bring the pb-hotspot profile up (SSID/password in that NetworkManager profile)
#   client   drop the hotspot, rejoin the best known WiFi; if none is in range, stay hotspot
#   toggle   ap <-> client (the pisound button, HOLD_3S)
#   auto     boot fallback: if neither wlan0 nor eth0 gets connected in time, ap -- and then WATCH
#   watch    (started by auto) while the hotspot is up BECAUSE of the fallback, look for a known
#            WiFi and rejoin it when it is back (2026-11-03: after a power cut the router came back
#            after the Pi, and the Pi sat on its own hotspot for 4 hours). A hotspot you picked
#            yourself (the button / ap / toggle) is left alone: they clear the fallback marker.
#   status   print the mode
# Feedback on the pisound LED: ap = 3 flashes, client = 1 flash, failed / nothing to join = 6 quick.
# Runs as root (pisound-btn and systemd both are). wlan0 is the only radio, so AP mode and
# client mode are exclusive: the hotspot is at 172.24.1.1 (web: http://172.24.1.1:5000).

AP=pb-hotspot
IF=wlan0
WAIT=${ORC1_NETMODE_WAIT:-45}
# the hotspot is up only because nothing was found at boot (auto); gone = you chose the mode
FALLBACK=${ORC1_NETMODE_FALLBACK:-/run/orc1_netmode.fallback}
# watch: look every FAST s for the first FAST_FOR s (the router is usually back by then), then
# every SLOW s. A scan briefly takes the one radio off the hotspot's channel, so not too often.
FAST=${ORC1_NETMODE_FAST:-60}
FAST_FOR=${ORC1_NETMODE_FAST_FOR:-900}
SLOW=${ORC1_NETMODE_SLOW:-300}

LOG=${ORC1_NETMODE_LOG:-/home/pibox/orc1_netmode.log}
LOCK=${ORC1_NETMODE_LOCK:-/run/orc1_netmode.lock}     # (both overridable for _test_netmode.py)
# own log file, synced per line: the journal is not persistent, and a hang must leave a trail
nlog() { logger -t orc1-netmode "$*"; echo "$*"; echo "$(date +%T) $*" >> $LOG; sync -d $LOG 2>/dev/null; }
run() { local o r; o=$("$@" 2>&1); r=$?; echo "$(date +%T) \$ $* -> $r $o" >> $LOG; sync -d $LOG 2>/dev/null; return $r; }

. "${ORC1_NETMODE_COMMON:-/usr/local/pisound/scripts/common/common.sh}" 2>/dev/null   # (the LED; tests: none)
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

# the SSIDs of the known WiFi profiles (not the hotspot)
known_ssids() {
	local n
	while IFS= read -r n; do
		[ -n "$n" ] && nmcli -t -g 802-11-wireless.ssid con show "$n" 2>/dev/null
	done < <(nmcli -t -f NAME,TYPE con show | awk -F: -v ap="$AP" '$2=="802-11-wireless" && $1!=ap {print $1}')
}

# a known WiFi in range right now? (scanned WITHOUT dropping the hotspot: measured 2026-11-03,
# wlan0 lists every network in range while it runs pb-hotspot). Prints the first one seen.
known_in_range() {
	local seen k
	seen=$(timeout 40 nmcli -t -f SSID dev wifi list --rescan yes ifname $IF 2>/dev/null | sort -u)
	while IFS= read -r k; do
		[ -n "$k" ] && grep -qxF "$k" <<< "$seen" && { echo "$k"; return 0; }
	done < <(known_ssids)
	return 1
}

watch_home() {
	local t0=$SECONDS k
	nlog "watch: the hotspot is a fallback: looking for a known WiFi"
	while [ -f "$FALLBACK" ]; do
		if [ $((SECONDS - t0)) -lt "$FAST_FOR" ]; then sleep "$FAST"; else sleep "$SLOW"; fi
		[ -f "$FALLBACK" ] || break
		if ! ap_active; then nlog "watch: the hotspot is down already, stopping"; rm -f "$FALLBACK"; break; fi
		k=$(known_in_range) || continue
		nlog "watch: $k is in range again, rejoining"
		"$0" rejoin && break                     # (the lock: a button press in progress wins)
	done
	nlog "watch: done"
}

if [ "$1" = watch ]; then watch_home; exit 0; fi
if [ "$1" = status ]; then
	if ap_active; then echo "hotspot ($AP, 172.24.1.1)"; else echo "client: $(nmcli -t -g GENERAL.CONNECTION dev show $IF)"; fi
	exit 0
fi
exec 9>"$LOCK"
flock -n 9 || { nlog "busy, ignoring"; [ "$1" = rejoin ] && exit 1; exit 0; }   # (rejoin: try again later)

case "$1" in
	ap) rm -f "$FALLBACK"; go_ap ;;
	client) rm -f "$FALLBACK"; go_client ;;
	toggle) rm -f "$FALLBACK"; if ap_active; then go_client; else go_ap; fi ;;
	rejoin)                                   # the watcher's: back to a known WiFi, fallback over
		go_client && { rm -f "$FALLBACK"; exit 0; }
		exit 1 ;;
	auto)
		for ((s = 0; s < WAIT; s += 3)); do connected && { nlog "auto: already connected"; exit 0; }; sleep 3; done
		# one explicit try (a known SSID may just have been slow to scan) before giving up the radio
		nlog "auto: not connected after ${WAIT}s, one more try"
		run nmcli dev connect $IF; sleep 12
		connected && { nlog "auto: joined late"; exit 0; }
		try_bt; nlog "auto: no network, starting hotspot"
		if go_ap; then
			touch "$FALLBACK"
			# the watcher runs on its own (this unit is a oneshot, boot waits for it)
			systemd-run --unit=orc1-netreturn --collect "$0" watch >/dev/null 2>&1 ||
				{ ( "$0" watch </dev/null >/dev/null 2>&1 & ) ; }
		fi ;;
	status) if ap_active; then echo "hotspot ($AP, 172.24.1.1)"; else echo "client: $(nmcli -t -g GENERAL.CONNECTION dev show $IF)"; fi ;;
	*) echo "usage: $0 ap|client|toggle|auto|status" >&2; exit 2 ;;
esac
