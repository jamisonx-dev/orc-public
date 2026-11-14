#!/bin/sh
# Runs ON the box: asks for the HomeWifi password, saves it as a connman network file, connects.
printf 'Type the HomeWifi Wi-Fi password, then Enter (nothing shows while you type): '
stty -echo; read -r pw; stty echo; echo
[ -n "$pw" ] || { echo "Empty, nothing saved."; exit 1; }
f=/storage/.cache/connman/homewifi.config
umask 077
printf '[service_wifi_homewifi]\nType = wifi\nName = HomeWifi\nPassphrase = %s\n' "$pw" > "$f"
pw=
echo "Saved. Connecting..."
connmanctl scan wifi >/dev/null 2>&1
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  sleep 2
  if connmanctl services | grep -q '^\*A[RO] *HomeWifi'; then echo "CONNECTED to HomeWifi."; exit 0; fi
done
echo "Not connected yet (wrong password?). Run this again to retry."
