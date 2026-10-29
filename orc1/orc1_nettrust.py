"""orc1_nettrust.py -- who may change the Pi's network settings or read its files.

The ORC web pages have no login and the Pi sits on whatever WiFi it can find, so
the pages that join networks, browse files or reboot the Pi only answer to a
TRUSTED source address:

  * the Pi itself (127.x / ::1)
  * the Pi's own hotspot subnet (172.24.1.0/24): joining it takes the WPA key
  * the local LAN, but ONLY while the Pi is joined to a network listed as a home
    network in ~/.orc1_net.json ("trusted_connections", default: none; mark your network as home on the :5003 page).
    On a stranger's WiFi the LAN is not trusted, so nobody there can reach these
    pages (use the hotspot).

Stdlib only, no side effects on import. Used by orc1_net.py (the :5003 service) and
orc1_system.py (the :5002 reboot/shutdown buttons).
"""
import ipaddress
import json
import os
import subprocess
import time

ALWAYS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "172.24.1.0/24")]   # Tailscale ranges removed 2026-10-25
CFG = "/home/pibox/.orc1_net.json"      # absolute: the samba dispatcher reads it as root
DEFAULT_HOME = []
IF = "wlan0"

_cache = {"t": 0.0, "v": None}


def load_cfg():
    try:
        with open(CFG) as f:
            c = json.load(f)
        if isinstance(c, dict):
            return c
    except Exception:
        pass
    return {}


def trusted_connections():
    c = load_cfg().get("trusted_connections")
    return list(c) if isinstance(c, list) else list(DEFAULT_HOME)


def active_wifi_connection():
    """Name of the NetworkManager profile wlan0 is on right now ('' if none). Cached 4 s."""
    now = time.time()
    if now - _cache["t"] < 4 and _cache["v"] is not None:
        return _cache["v"]
    try:
        v = subprocess.run(["nmcli", "-t", "-g", "GENERAL.CONNECTION", "dev", "show", IF],
                           capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        v = ""
    _cache["t"], _cache["v"] = now, v
    return v


def at_home():
    n = active_wifi_connection()
    return bool(n) and n in trusted_connections()


def _addr(a):
    ip = ipaddress.ip_address(a.split("%")[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip


def source_class(addr):
    """'local' | 'hotspot' | 'home' | None (not trusted)."""
    try:
        ip = _addr(addr)
    except Exception:
        return None
    for net in ALWAYS:
        if ip.version == net.version and ip in net:
            if net.network_address.is_loopback:
                return "local"
            return "hotspot"
    if (ip.is_private or ip.is_link_local) and at_home():
        return "home"
    return None


def trusted(addr):
    return source_class(addr) is not None
