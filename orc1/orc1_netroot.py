#!/usr/bin/env python3
"""orc1_netroot.py -- the root-only half of the network page (orc1_net.py calls it via sudo).

  scan     {rescan}                       -> {"nets":[{ssid,signal,sec,inuse}], "cached":bool}
  known    {}                             -> {"nets":[{name,ssid,priority,trusted,kind}]}
  join     {ssid, psk, low, hidden}       -> starts a detached join job, returns at once
  joinrun  <ssid>   (internal, systemd-run)
  forget   {name}
  ap_psk   {psk}
  trust    {name, on}

Passwords arrive on STDIN as JSON (never argv, so `ps` never shows them) and are written
only into the NetworkManager keyfile (root, mode 600), exactly where nmcli would put them.
A join runs as its own transient systemd unit with a Pi-side rollback: if the new network
does not come up with an address within ~35 s it is forgotten again and the Pi goes back
to the hotspot (if it was one) or to the best known network, so a typo'd password cannot
strand the Pi. The outcome lands in /run/orc1_net_result.json and ~/orc1_netmode.log.
Stdlib only.
"""
import configparser
import json
import os
import re
import subprocess
import sys
import time

AP = "pb-hotspot"
IF = "wlan0"
KEYDIR = "/etc/NetworkManager/system-connections"
NETMODE = "/home/pibox/orc/orc1/orc1_netmode.sh"
RESULT = "/run/orc1_net_result.json"
SCAN = "/run/orc1_net_scan.json"
JOB = "/run/orc1_net_job.json"          # root-only: holds nothing secret, just the target
CFG = "/home/pibox/.orc1_net.json"
LOG = "/home/pibox/orc1_netmode.log"


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s netroot: %s\n" % (time.strftime("%T"), msg))
    except Exception:
        pass


def nm(*a, timeout=60):
    p = subprocess.run(["nmcli"] + list(a), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def split_terse(line):
    """nmcli -t escapes ':' and '\\' inside fields."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


def ap_active():
    rc, o = nm("-t", "-f", "NAME", "con", "show", "--active")
    return AP in o.splitlines()


def keyfile(name):
    p = os.path.join(KEYDIR, name + ".nmconnection")
    return p if os.path.isfile(p) else None


def load_cfg():
    try:
        with open(CFG) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def save_cfg(c):
    tmp = CFG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG)
    os.chown(CFG, 1000, 1000)


def valid_ssid(s):
    return (isinstance(s, str) and 1 <= len(s.encode("utf-8", "ignore")) <= 32
            and not any(ord(c) < 32 or ord(c) == 127 for c in s) and s != AP)


def valid_psk(p):
    return (isinstance(p, str) and ((8 <= len(p) <= 63 and all(32 <= ord(c) < 127 for c in p))
                                    or re.fullmatch(r"[0-9a-fA-F]{64}", p) is not None))


def set_psk(name, psk):
    """Write the secret straight into the keyfile (root-only), then have NM reload it."""
    path = keyfile(name)
    if not path:
        raise RuntimeError("profile file not found for %r" % name)
    cp = configparser.RawConfigParser(delimiters=("=",), interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(path)
    if not cp.has_section("wifi-security"):
        cp.add_section("wifi-security")
    cp.set("wifi-security", "psk", psk)
    with open(path, "w") as f:
        cp.write(f, space_around_delimiters=False)
    os.chmod(path, 0o600)
    nm("con", "reload")


def cmd_scan(a):
    if a.get("rescan") and not ap_active():
        nm("dev", "wifi", "rescan", "ifname", IF)
        time.sleep(5)
    if ap_active():
        # AP mode cannot scan without dropping the clients: serve the list taken before it came up
        try:
            with open(SCAN) as f:
                return {"nets": json.load(f), "cached": True}
        except Exception:
            return {"nets": [], "cached": True}
    rc, o = nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", IF)
    best = {}
    for line in o.splitlines():
        f = split_terse(line)
        if len(f) < 4 or not f[1]:
            continue
        try:
            sig = int(f[2])
        except ValueError:
            sig = 0
        row = {"ssid": f[1], "signal": sig, "sec": f[3] or "open", "inuse": f[0] == "*"}
        was = f[1] in best and best[f[1]]["inuse"]
        if f[1] not in best or sig > best[f[1]]["signal"]:
            best[f[1]] = row
        if was or row["inuse"]:
            best[f[1]]["inuse"] = True
    nets = sorted(best.values(), key=lambda r: -r["signal"])
    try:
        with open(SCAN, "w") as fh:
            json.dump(nets, fh)
        os.chmod(SCAN, 0o644)
    except Exception:
        pass
    return {"nets": nets, "cached": False}


def cmd_known(a):
    rc, o = nm("-t", "-f", "NAME,TYPE", "con", "show")
    trust = load_cfg().get("trusted_connections", [])
    nets = []
    for line in o.splitlines():
        f = split_terse(line)
        if len(f) < 2 or f[1] != "802-11-wireless":
            continue
        rc, v = nm("-t", "-g", "802-11-wireless.ssid,connection.autoconnect-priority,802-11-wireless.mode",
                   "con", "show", f[0])
        vs = v.splitlines() + ["", "0", ""]
        try:
            pr = int(vs[1])
        except ValueError:
            pr = 0
        nets.append({"name": f[0], "ssid": vs[0], "priority": pr, "trusted": f[0] in trust,
                     "kind": "hotspot" if vs[2] == "ap" else ("low" if pr < 0 else "normal")})
    return {"nets": nets}


def cmd_join(a):
    ssid, psk = a.get("ssid", ""), a.get("psk", "")
    if not valid_ssid(ssid):
        return {"ok": False, "error": "bad network name (1-32 characters)"}
    if psk and not valid_psk(psk):
        return {"ok": False, "error": "password must be 8-63 characters"}
    # enterprise / unsupported security is visible in the last scan
    sec = ""
    try:
        with open(SCAN) as f:
            for r in json.load(f):
                if r["ssid"] == ssid:
                    sec = r["sec"]
    except Exception:
        pass
    if "802.1X" in sec:
        return {"ok": False, "error": "this network uses 802.1X (enterprise) login, which this page cannot join"}
    if sec and sec != "open" and not psk and not keyfile(ssid):
        return {"ok": False, "error": "this network needs a password"}
    prio = -50 if a.get("low") else 0
    backup = None
    old = keyfile(ssid)
    if old:
        with open(old) as f:
            backup = f.read()
        nm("con", "delete", ssid)
    args = ["con", "add", "type", "wifi", "ifname", IF, "con-name", ssid, "ssid", ssid,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", str(prio)]
    if a.get("hidden"):
        args += ["802-11-wireless.hidden", "yes"]
    if psk:
        args += ["wifi-sec.key-mgmt", "sae" if ("WPA3" in sec and "WPA2" not in sec) else "wpa-psk"]
    rc, o = nm(*args)
    if rc != 0:
        if backup is not None:
            _restore(ssid, backup)
        return {"ok": False, "error": "could not create the profile: " + o[:200]}
    if psk:
        try:
            set_psk(ssid, psk)
        except Exception as e:
            nm("con", "delete", ssid)
            if backup is not None:
                _restore(ssid, backup)
            return {"ok": False, "error": "could not store the password: %s" % e}
    with open(JOB, "w") as f:
        json.dump({"ssid": ssid, "was_ap": ap_active(), "backup": backup}, f)
    os.chmod(JOB, 0o600)
    try:
        os.remove(RESULT)
    except OSError:
        pass
    subprocess.run(["systemd-run", "--no-block", "--collect", "--unit=orc1-wifi-join-%d" % int(time.time()),
                    "/usr/bin/python3", os.path.abspath(__file__), "joinrun", ssid],
                   capture_output=True, timeout=10)
    log("join started: %s (was_ap=%s)" % (ssid, ap_active()))
    return {"ok": True, "started": True, "was_ap": ap_active()}


def _restore(name, content):
    p = os.path.join(KEYDIR, name + ".nmconnection")
    with open(p, "w") as f:
        f.write(content)
    os.chmod(p, 0o600)
    nm("con", "reload")


def _result(**kw):
    kw["ts"] = time.time()
    with open(RESULT, "w") as f:
        json.dump(kw, f)
    os.chmod(RESULT, 0o644)


def _up_ok():
    rc, o = nm("-t", "-g", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show", IF)
    ls = o.splitlines() + ["", "", ""]
    return ls[0].startswith("100"), ls[1], ls[2].split("/")[0]


def cmd_joinrun(ssid):
    with open(JOB) as f:
        job = json.load(f)
    nm("con", "down", AP)
    time.sleep(3)
    nm("dev", "wifi", "rescan", "ifname", IF)
    time.sleep(6)
    ok, ip = False, ""
    for _ in range(2):
        rc, o = nm("-w", "30", "con", "up", ssid, timeout=60)
        end = time.time() + 20
        while time.time() < end:
            up, conn, ip = _up_ok()
            if up and conn == ssid and ip:
                ok = True
                break
            time.sleep(2)
        if ok:
            break
        nm("dev", "wifi", "rescan", "ifname", IF)
        time.sleep(6)
    if ok:
        log("join OK: %s %s" % (ssid, ip))
        _result(ok=True, ssid=ssid, ip=ip, message="joined %s" % ssid)
        return
    # rollback: forget what failed, restore what was replaced, go back to where we were
    log("join FAILED: %s -- rolling back" % ssid)
    nm("con", "delete", ssid)
    if job.get("backup"):
        _restore(ssid, job["backup"])
    _result(ok=False, ssid=ssid, ip="", message="could not join %s (wrong password, out of range, or no DHCP); went back to %s"
            % (ssid, "the hotspot" if job.get("was_ap") else "the previous network"))
    subprocess.run([NETMODE, "ap" if job.get("was_ap") else "client"], timeout=180)


def cmd_forget(a):
    name = a.get("name", "")
    if name == AP:
        return {"ok": False, "error": "the hotspot profile cannot be forgotten here"}
    if not keyfile(name):
        return {"ok": False, "error": "no such saved network"}
    rc, o = nm("-t", "-f", "NAME,TYPE", "con", "show")
    if not any(split_terse(l)[:2] == [name, "802-11-wireless"] for l in o.splitlines()):
        return {"ok": False, "error": "not a WiFi profile"}
    rc, o = nm("con", "delete", name)
    c = load_cfg()
    if name in c.get("trusted_connections", []):
        c["trusted_connections"].remove(name)
        save_cfg(c)
    log("forgot %s" % name)
    return {"ok": rc == 0, "error": "" if rc == 0 else o[:200]}


def cmd_ap_psk(a):
    p = a.get("psk", "")
    if not (isinstance(p, str) and 8 <= len(p) <= 63 and all(32 <= ord(c) < 127 for c in p)):
        return {"ok": False, "error": "hotspot password must be 8-63 plain characters"}
    set_psk(AP, p)
    log("hotspot password changed")
    return {"ok": True, "active": ap_active()}


def cmd_trust(a):
    name, on = a.get("name", ""), bool(a.get("on"))
    if name == AP or not keyfile(name):
        return {"ok": False, "error": "no such saved network"}
    c = load_cfg()
    t = c.get("trusted_connections", [])
    if on and name not in t:
        t.append(name)
    if not on and name in t:
        t.remove(name)
    c["trusted_connections"] = t
    save_cfg(c)
    subprocess.run(["/etc/NetworkManager/dispatcher.d/90-orc1-samba-trust", IF, "refresh"],
                   capture_output=True, timeout=20)
    return {"ok": True}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "joinrun":
        return cmd_joinrun(sys.argv[2])
    try:
        a = json.loads(sys.stdin.read() or "{}")
    except Exception:
        a = {}
    fn = {"scan": cmd_scan, "known": cmd_known, "join": cmd_join, "forget": cmd_forget,
          "ap_psk": cmd_ap_psk, "trust": cmd_trust}.get(cmd)
    if not fn:
        print(json.dumps({"ok": False, "error": "unknown command"}))
        return 2
    try:
        print(json.dumps(fn(a)))
    except Exception as e:
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
