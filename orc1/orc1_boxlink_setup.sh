#!/bin/bash
# orc1_boxlink_setup.sh -- make eth0 a private cable link to ONE device (the MXV S805
# video box, 2026-10-29). Run once with sudo; safe to run again.
#
#   Pi eth0 = 10.55.0.1/29, the box gets 10.55.0.2-6 from orc1-boxlink-dhcp (no gateway,
#   no DNS). IP forwarding stays off, so the box can reach the Pi and nothing else.
#   orc1_nettrust / Samba only trust wlan0's subnet, so the box is NOT trusted for the
#   power buttons, network pages or shares.
#
# To use eth0 on the home router instead:  nmcli con down orc-box-link
# (NetworkManager's automatic DHCP profile then takes eth0; the DHCP unit only ever
# answers on 10.55.0.0/29, so it stays quiet on a real LAN).
set -e
P=orc-box-link
if ! nmcli -t -f NAME con show | grep -qx "$P"; then
    nmcli con add type ethernet ifname eth0 con-name "$P" \
        ipv4.method manual ipv4.addresses 10.55.0.1/29 ipv4.never-default yes \
        ipv6.method link-local connection.autoconnect yes connection.autoconnect-priority 10
else
    nmcli con mod "$P" ipv4.addresses 10.55.0.1/29   # was a /30 for the first hour
fi
install -m 644 /home/pibox/orc/systemd/orc1-boxlink-dhcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now orc1-boxlink-dhcp
nmcli con up "$P" >/dev/null 2>&1 || true   # no carrier until the box is plugged in: fine
echo "ip_forward=$(cat /proc/sys/net/ipv4/ip_forward) (must be 0)"
