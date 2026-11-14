#!/bin/bash
# orc1_mdns_guard.sh -- keep the Pi's mDNS name = its hostname, so http://pibox.local:5000 keeps working.
# avahi renames itself on a "host name conflict" (pibox-2.local) when a Wi-Fi rejoin makes it hear its
# own old announcement (seen 2026-11-03 11:27:52, right as a new IPv6 address came up). Nothing ever
# renamed it back, so pibox.local was gone until a reboot: owner, "an every day problem at this point".
# Run by orc1-mdnsname.timer once a minute, as root. Quiet when the name is right.
want=$(hostname)
have=$(busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server GetHostName 2>/dev/null |
       sed -n 's/^s "\(.*\)"$/\1/p')
[ -z "$have" ] && exit 0                     # (avahi not running: nothing to fix here)
if [ "$have" != "$want" ]; then
	logger -t orc1-mdns "avahi renamed itself $have.local: setting it back to $want.local"
	busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server SetHostName s "$want"
fi
exit 0
