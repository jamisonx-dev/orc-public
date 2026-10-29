#!/usr/bin/python3
"""NetworkManager dispatcher (installed as /etc/NetworkManager/dispatcher.d/90-orc1-samba-trust).

Keeps Samba's `hosts allow` in step with WHERE the Pi is: the guest shares ([home],
[recordings], [reaper]) are open to the Pi itself, and its own hotspot always,
and to the local LAN only while wlan0 is joined to a network listed as "home" in
~/.orc1_net.json. On a stranger's WiFi nobody on that LAN can open the shares.
Also runnable by hand: `orc1_samba_trust.py wlan0 refresh`. Stdlib only.
"""
import ipaddress
import json
import subprocess
import sys

CONF = "/etc/samba/orc1-hosts.conf"
CFG = "/home/pibox/.orc1_net.json"
IF = "wlan0"
ALWAYS = ["127.0.0.1", "::1", "172.24.1.0/24"]   # Tailscale ranges removed 2026-10-25


def out(*a):
    try:
        return subprocess.run(list(a), capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:
        return ""


def main():
    if len(sys.argv) > 2 and sys.argv[2] not in ("up", "down", "dhcp4-change", "connectivity-change", "refresh"):
        return
    try:
        trusted = json.load(open(CFG)).get("trusted_connections", [])
    except Exception:
        trusted = []
    conn = out("nmcli", "-t", "-g", "GENERAL.CONNECTION", "dev", "show", IF)
    hosts = list(ALWAYS)
    if conn and conn in trusted:
        a = out("ip", "-4", "-o", "addr", "show", IF).split()
        if len(a) > 3:
            hosts.append(str(ipaddress.ip_interface(a[3]).network))
    text = "hosts allow = %s\nhosts deny = ALL\n" % " ".join(hosts)
    try:
        old = open(CONF).read()
    except Exception:
        old = ""
    if text != old:
        open(CONF, "w").write(text)
        out("smbcontrol", "all", "reload-config")


main()
