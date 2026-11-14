#!/bin/sh
# wifikeep.sh -- MXV box (LibreELEC, service wifikeep.service): keep the box on the Wi-Fi the Pi is on.
# The box's RTL8723BS often misses the network in a scan, so connman's own autoconnect can
# give up (measured 2026-10-29: after a power cut it never rejoined). Every 10 s: if no
# saved Wi-Fi network is connected, scan and connect to the first saved one that is visible.
# Saved networks = /storage/.cache/connman/*.config files (written by boxwifi.sh).
#
# FOLLOWING THE PI'S HOTSPOT (2026-10-31): when the Pi is switched to its own hotspot
# (4 clicks on the pisound button -> network "pibox") the box on the home Wi-Fi can no longer
# see the Pi. The Pi's conductor touches /dev/shm/orc/pi_seen on every status call (every
# ~10 s, over whichever network works), so a stale pi_seen = "the Pi is not on my network".
# Then the box scans (at most every 30 s) and joins the hotspot if it is there; it only
# prefers the hotspot over a home network while the Pi is silent, so a stale scan entry
# after the Pi returns home cannot hold it on a network that is gone. When the Pi goes
# back to the home Wi-Fi the hotspot disappears, the link drops, and the loop below
# rejoins the home network. A connect that FAILS tries the next saved network (it used to
# stop at the first visible one, which a stale entry could make permanent).
HOT=${WIFIKEEP_HOTSPOT:-pibox}
CONF=${WIFIKEEP_CONF:-/storage/.cache/connman}
SEEN=${WIFIKEEP_SEEN:-/dev/shm/orc/pi_seen}
PI_SILENT=${WIFIKEEP_PI_SILENT:-25}      # seconds without the Pi's status call
SCAN_EVERY=${WIFIKEEP_SCAN_EVERY:-30}    # at most one hotspot scan per this many seconds
LOOP_S=${WIFIKEEP_LOOP_S:-10}

hex() { printf '%s' "$1" | od -An -tx1 | tr -d ' \n'; }
svc_of() { connmanctl services | grep -o "wifi_[0-9a-f]*_$(hex "$1")_managed_psk" | head -1; }
is_connected() { connmanctl services | grep -qE '^\*A[RO] .* wifi_'; }
on_hotspot() { connmanctl services | grep -E '^\*A[RO] ' | grep -q "_$(hex "$HOT")_managed"; }
# seconds since the Pi last called; no file yet (tmpfs: wiped at boot) = time since boot
pi_age() {
  # date -r, not stat -c: the box's busybox stat has no -c (measured 2026-10-31: it fell
  # through to "time since boot" every time, so the Pi always looked silent)
  m=$(date -r "$SEEN" +%s 2>/dev/null) || { cut -d. -f1 /proc/uptime; return; }
  echo $(( $(date +%s) - m ))
}
# saved network names, the hotspot first only while the Pi is silent, else last
names() {
  all=$(sed -n 's/^Name = //p' "$CONF"/*.config 2>/dev/null)
  others=$(printf '%s\n' "$all" | grep -vx "$HOT")
  hot=$(printf '%s\n' "$all" | grep -x "$HOT")
  if [ "$1" -gt "$PI_SILENT" ]; then printf '%s\n%s\n' "$hot" "$others"; else printf '%s\n%s\n' "$others" "$hot"; fi
}

last_scan=0
[ -n "$WIFIKEEP_SOURCE_ONLY" ] && return 0 2>/dev/null
while true; do
  age=$(pi_age)
  now=$(date +%s)
  if ! is_connected; then
    connmanctl scan wifi >/dev/null 2>&1
    names "$age" | while IFS= read -r name; do
      [ -n "$name" ] || continue
      svc=$(svc_of "$name")
      [ -n "$svc" ] || continue
      if connmanctl connect "$svc" >/dev/null 2>&1; then echo "wifikeep: connected to $name"; break; fi
    done
  elif [ "$age" -gt "$PI_SILENT" ] && ! on_hotspot && [ $((now - last_scan)) -ge "$SCAN_EVERY" ]; then
    last_scan=$now
    connmanctl scan wifi >/dev/null 2>&1
    sleep 6
    svc=$(svc_of "$HOT")
    if [ -n "$svc" ] && connmanctl connect "$svc" >/dev/null 2>&1; then
      echo "wifikeep: Pi silent for ${age}s, joined its hotspot $HOT"
    fi
  fi
  sleep "$LOOP_S"
done
