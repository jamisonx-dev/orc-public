#!/bin/sh
# _test_wifikeep.sh -- the box's wifikeep.sh ordering + "is the Pi silent" logic.
#   sh _test_wifikeep.sh        (runs anywhere with sh; no connman needed: it never calls it)
# Sandboxed: its own temp dir for the saved networks and pi_seen.
EXPECTED=8
T=$(mktemp -d)
mkdir -p "$T/conf"
printf '[service_wifi_homewifi]\nType = wifi\nName = HomeWifi\nPassphrase = x\n' > "$T/conf/homewifi.config"
printf '[service_wifi_pibox]\nType = wifi\nName = pibox\nPassphrase = y\n' > "$T/conf/pibox.config"
export WIFIKEEP_CONF="$T/conf" WIFIKEEP_SEEN="$T/seen" WIFIKEEP_SOURCE_ONLY=1
. "$(dirname "$0")/wifikeep.sh"

pass=0; fail=0
check() { if [ "$1" = "ok" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $2"; fi; }
first() { names "$1" | grep -v '^$' | head -1; }
last() { names "$1" | grep -v '^$' | tail -1; }

check "$([ "$(first 5)" = HomeWifi ] && echo ok)" "Pi seen 5 s ago: home network first"
check "$([ "$(last 5)" = pibox ] && echo ok)" "Pi seen 5 s ago: hotspot last"
check "$([ "$(first 60)" = pibox ] && echo ok)" "Pi silent 60 s: hotspot first"
check "$([ "$(last 60)" = HomeWifi ] && echo ok)" "Pi silent 60 s: home network last"
check "$([ "$(first 25)" = HomeWifi ] && echo ok)" "exactly PI_SILENT s is not yet silent"
check "$([ "$(names 60 | grep -c .)" = 2 ] && echo ok)" "both saved networks listed, no more"

touch "$T/seen"
a=$(pi_age)
check "$([ "$a" -le 2 ] && echo ok)" "fresh pi_seen -> age ~0 (got $a)"
rm -f "$T/seen"
a=$(pi_age)
up=$(cut -d. -f1 /proc/uptime)
check "$([ "$a" -ge "$((up - 2))" ] && echo ok)" "no pi_seen -> age = time since boot (got $a, uptime $up)"

echo "$pass passed, $fail failed"
rm -rf "$T"
[ "$((pass + fail))" = "$EXPECTED" ] || { echo "FAIL: expected $EXPECTED checks, ran $((pass + fail))"; exit 1; }
[ "$fail" = 0 ]
