#!/usr/bin/env python3
"""
WHOLE-PI POWER BUTTONS (2026-10-25): /system's REBOOT / SHUT DOWN
(orc1_web /api/power/<action>) and the :5002 dashboard's buttons
(orc-control/orc1_system.py /system/reboot|shutdown).

The dashboard's buttons used to need a "system token" typed into a browser
prompt (the published default, never told to anyone), so they failed with
"Invalid token". Both now answer to the network rule only (orc1_nettrust:
the Pi itself, its hotspot, the home network) and fail closed.

Pure: subprocess.Popen is replaced by a recorder and orc1_nettrust.trusted
by a stub, so NOTHING here can reboot or power off the Pi. The stub is
installed before either app is imported, and every check asserts on the
recorded command, never on a real one.

Mutation-prove: make api_power skip the trust check -> "untrusted refused";
put the token back into orc1_system._authorized -> "dashboard, no token".
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "orc-control"))

EXPECTED = 19
_p = _f = 0


def check(cond, label, extra=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label} {extra}")


# ── safety first: nothing may really run ─────────────────────────────────────
RAN = []


class FakePopen:
    def __init__(self, cmd, *a, **k):
        RAN.append(list(cmd))


subprocess.Popen = FakePopen

import orc1_nettrust as N           # noqa: E402

# ── the REAL trust rule (before it is stubbed): Tailscale removed 2026-10-25 ─
check(not N.trusted("100.76.34.48") and not N.trusted("fd7a:115c:a1e0::5"),
      "a Tailscale address is NOT trusted (Tailscale removed)")
check(N.source_class("127.0.0.1") == "local" and N.source_class("172.24.1.7") == "hotspot",
      "the Pi itself + its hotspot are still trusted")
check(not N.trusted("8.8.8.8"), "an outside address is not trusted")
# orc1_samba_trust runs main() at import (it is a dispatcher script and writes
# /etc/samba), so read its list from the source, never import it.
import ast                           # noqa: E402
_sm = ast.parse(open(os.path.join(HERE, "orc1_samba_trust.py"), encoding="utf-8").read())
SAMBA_ALWAYS = next(ast.literal_eval(n.value) for n in _sm.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "ALWAYS")
check(not any(a.startswith(("100.64", "fd7a")) for a in SAMBA_ALWAYS)
      and "172.24.1.0/24" in SAMBA_ALWAYS, "Samba's always-allowed list has no Tailscale range")
net_src = open(os.path.join(HERE, "orc1_net.py"), encoding="utf-8").read().lower()
check("tailscale" not in net_src, "the WiFi page (orc1_net.py) no longer calls or mentions tailscale")

TRUST = {"ok": True}
N.trusted = lambda addr: TRUST["ok"]

import orc1_web as W                # noqa: E402  (side-effect free, see _test_patch_persist)
import orc1_system as SY            # noqa: E402

cl = W.app.test_client()

# ── /api/power on the main web ───────────────────────────────────────────────
RAN.clear(); TRUST["ok"] = True
r = cl.post("/api/power/shutdown")
check(r.status_code == 200 and r.get_json().get("ok"), "shutdown from a trusted address: ok", r.data)
check(RAN == [["sudo", "-n", "systemctl", "poweroff"]], "shutdown runs systemctl poweroff", RAN)

RAN.clear()
r = cl.post("/api/power/reboot")
check(r.status_code == 200 and RAN == [["sudo", "-n", "systemctl", "reboot"]],
      "reboot runs systemctl reboot", RAN)

RAN.clear(); TRUST["ok"] = False
r = cl.post("/api/power/shutdown")
check(r.status_code == 403 and RAN == [], "untrusted address refused, nothing run", (r.status_code, RAN))

RAN.clear(); TRUST["ok"] = True
r = cl.post("/api/power/halt")
check(r.status_code == 400 and RAN == [], "unknown action refused, nothing run", (r.status_code, RAN))

r = cl.get("/api/power/shutdown")
check(r.status_code == 405 and RAN == [], "GET does nothing (POST only)", (r.status_code, RAN))

N_trusted = N.trusted
def _boom(addr):
    raise RuntimeError("nettrust broken")
N.trusted = _boom
RAN.clear()
r = cl.post("/api/power/shutdown")
check(r.status_code == 403 and RAN == [], "a broken trust check fails CLOSED", (r.status_code, RAN))
N.trusted = N_trusted

# ── the :5002 dashboard ──────────────────────────────────────────────────────
sc = SY.app.test_client()
RAN.clear(); TRUST["ok"] = True
r = sc.post("/system/shutdown")
check(r.status_code == 200 and RAN and RAN[-1][:2] == ["sudo", "shutdown"],
      "dashboard shutdown works with NO token from a trusted address", (r.status_code, RAN))
RAN.clear()
r = sc.post("/system/reboot")
check(r.status_code == 200 and RAN == [["sudo", "reboot"]], "dashboard reboot, no token", (r.status_code, RAN))
RAN.clear(); TRUST["ok"] = False
r = sc.post("/system/shutdown", headers={"Authorization": "Bearer " + SY.SYSTEM_TOKEN})
check(r.status_code == 401 and RAN == [], "dashboard: untrusted refused even WITH the token", (r.status_code, RAN))
TRUST["ok"] = True

# ── the pages ────────────────────────────────────────────────────────────────
page = sc.get("/").get_data(as_text=True)
check("prompt(" not in page and "orc1_token" not in page, "dashboard page no longer asks for a token")
html = open(os.path.join(HERE, "templates", "system.html"), encoding="utf-8").read()
js = open(os.path.join(HERE, "static", "js", "system.js"), encoding="utf-8").read()
check("powerAsk('shutdown')" in html and "powerAsk('reboot')" in html and 'id="power-go"' in html,
      "/system has REBOOT + SHUT DOWN with an on-page confirm")
pjs = "\n".join(l for l in js.split("SYSTEM CONTROLS: whole-Pi")[1].splitlines()
                if not l.lstrip().startswith("//"))
check("'/api/power/' + action" in js and "function powerGo" in js and "confirm(" not in pjs,
      "the page posts /api/power/<action> after its own confirm step (no browser confirm)")
check("GRAB ALL" in js.split("SYSTEM CONTROLS: whole-Pi")[1], "the confirm warns that skipback RAM audio is lost")

total = _p + _f
print(f"{_p} passed, {_f} failed")
if total != EXPECTED:
    print(f"COUNT DRIFT: ran {total} checks, EXPECTED = {EXPECTED}")
    sys.exit(1)
sys.exit(1 if _f else 0)
