#!/usr/bin/env python3
"""orc1_health FX / module-output judgements (2026-10-01).

  * a module feeding the /fx RACK (INTERNAL mode, a slot on) reaches its saved
    output THROUGH the fx-rack-* clients -- health used to call that DEAD;
  * DAW/DJ "engines off" stops the module hosts on purpose, so the FX send
    ports being absent is IDLE, not DEAD (same rule as the services check);
  * INTERNAL on a module whose output is NONE resolves the return to "" --
    health used to say nothing; now it warns.

Pure stdlib, no JACK: the graph is a synthetic dict, run_* never called.
    python3 _test_health_fx.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_health as H
import orc1_config as cfg

EXPECTED = 18
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def levels(prefix):
    return [(r["level"], r["id"]) for r in H.RESULTS if (r["id"] or "").startswith(prefix)]


# -- _reach_through_fx --------------------------------------------------------
PB = ("system:playback_1", "system:playback_2")
ports = {"ORC1-Poly:out_L", "ORC1-Poly:out_R", "fx-rack-0:in_l", "fx-rack-0:in_r",
         "fx-rack-0:out_l", "fx-rack-0:out_r", "fx-rack-1:inL", "fx-rack-1:outL",
         "fx-rack-1:outR", *PB}
conns = {
    "ORC1-Poly:out_L": ["fx-rack-0:in_l"], "ORC1-Poly:out_R": ["fx-rack-0:in_r"],
    "fx-rack-0:in_l": ["ORC1-Poly:out_L"], "fx-rack-0:in_r": ["ORC1-Poly:out_R"],
    "fx-rack-0:out_l": ["fx-rack-1:inL"], "fx-rack-0:out_r": ["fx-rack-1:inL"],
    "fx-rack-1:inL": ["fx-rack-0:out_l", "fx-rack-0:out_r"],
    "fx-rack-1:outL": [PB[0]], "fx-rack-1:outR": [PB[1]],
    PB[0]: ["fx-rack-1:outL"], PB[1]: ["fx-rack-1:outR"],
}
rl = H._reach_through_fx({"fx-rack-0:in_l"}, "L", conns, ports)
rr = H._reach_through_fx({"fx-rack-0:in_r"}, "R", conns, ports)
check("a module -> 2-slot rack (one slot mono) reaches its saved pair",
      PB[0] in rl and PB[1] in rr, f"{sorted(rl)} | {sorted(rr)}")
c2 = dict(conns, **{"fx-rack-1:outL": [], "fx-rack-1:outR": [],
                    PB[0]: [], PB[1]: []})
check("...and a rack whose last slot is NOT wired to the return does not",
      PB[0] not in H._reach_through_fx({"fx-rack-0:in_l"}, "L", c2, ports))
ins = {"ORC1-FX-3:dry_L": ["ORC1-Poly:out_L"], "ORC1-FX-3:out_L": [PB[0]]}
check("the fx INSERT hop still works (dry_L -> out_L)",
      PB[0] in H._reach_through_fx({"ORC1-FX-3:dry_L"}, "L", ins, set(ins)))
check("a plain wire is just itself",
      H._reach_through_fx({PB[0]}, "L", {}, set()) == {PB[0]})

# -- sec_module_outputs is information only: unusual wiring is never DEAD/WARN ----
_orig = (cfg.MODULE_OUT_KEYS, cfg.load_module_out, cfg.resolve_module_out, H._phys_playback)
cfg.MODULE_OUT_KEYS = ["poly"]
cfg.load_module_out = lambda k: "out-1-2"
cfg.resolve_module_out = lambda dest, phys: PB if dest != "none" else None
H._phys_playback = lambda: list(PB)
H.RESULTS.clear()
H.sec_module_outputs(ports, conns)
check("sec_module_outputs: never raises dead/warn for any module wiring",
      not any(l in ("dead", "warn") for l, _ in levels("modout")), levels("modout"))
_odd = dict(conns)
_odd["ORC1-Poly:out_L"] = ["somewhere:else"]
H.RESULTS.clear()
H.sec_module_outputs(ports, _odd)
check("...even a module wired somewhere no setting says",
      not any(l in ("dead", "warn") for l, _ in levels("")), levels(""))

# -- sec_fx: engines off = IDLE, output NONE = WARN -------------------------------
d = tempfile.mkdtemp()
H.FX_IO_JSON = os.path.join(d, "io.json")
H.FX_RACK_JSON = os.path.join(d, "rack.json")
H.FX_RACK_STATUS_JSON = os.path.join(d, "rack_status.json")   # never the real one
with open(H.FX_IO_JSON, "w") as f:
    json.dump({"mode": "internal", "module": "poly"}, f)
mark = os.path.join(d, "engines-off")
H._DAW_OFF_MARK = mark
live = {PB[0], PB[1]}                       # module hosts stopped: no ORC1-Poly ports
H.RESULTS.clear()
H.sec_fx(live)
check("without the engines-off marker a missing FX send port is DEAD",
      ("dead", "fx.io.send_l") in levels("fx.io"), levels("fx.io"))
open(mark, "w").close()
H.RESULTS.clear()
H.sec_fx(live)
lv = levels("fx.io")
check("DAW/DJ engines-off: the stopped module's FX send ports are IDLE, not DEAD",
      ("idle", "fx.io.send_l") in lv and ("idle", "fx.io.send_r") in lv
      and not any(l == "dead" for l, _ in lv), lv)
os.remove(mark)
cfg.load_module_out = lambda k: "none"
H.RESULTS.clear()
H.sec_fx(ports)
check("...but with every rack slot OFF, an unheard return is not a warning",
      ("warn", "fx.io.return") not in levels("fx.io"), levels("fx.io"))
with open(H.FX_RACK_JSON, "w") as f:
    json.dump({"slots": [{"plugin": "x", "enabled": True}]}, f)
H.RESULTS.clear()
H.sec_fx(ports)
lv = levels("fx.io")
check("INTERNAL on a module whose output is NONE warns (return wired nowhere)",
      ("warn", "fx.io.return") in lv, lv)
cfg.load_module_out = lambda k: "out-1-2"
H.RESULTS.clear()
H.sec_fx(ports | {"ORC1-Poly:out_L"})
check("...and a normal INTERNAL setup raises no return warning",
      ("warn", "fx.io.return") not in levels("fx.io"), levels("fx.io"))

# -- sec_fx: no source module = WARN only while a rack slot is ON (2026-10-14) ----
with open(H.FX_IO_JSON, "w") as f:
    json.dump({"mode": "internal"}, f)
H.RESULTS.clear()
H.sec_fx(ports)
check("no source module + a rack slot ON warns",
      ("warn", "fx.io.module") in levels("fx.io"), levels("fx.io"))
with open(H.FX_RACK_JSON, "w") as f:
    json.dump({"slots": [{"plugin": "x", "enabled": False}]}, f)
H.RESULTS.clear()
H.sec_fx(ports)
check("no source module + every rack slot OFF does NOT warn (FX unused)",
      not any(l == "warn" for l, _ in levels("fx.io")), levels("fx.io"))

# -- rack slot saved ON but not started since boot = IDLE (2026-10-19) ----------
# Boot no longer restores the rack (owner: leave it off at boot, stop health
# calling it DEAD). Only a slot the manager STARTED and lost is DEAD.
with open(H.FX_RACK_JSON, "w") as f:
    json.dump({"slots": [{"plugin": "capacitor2", "enabled": True}]}, f)
with open(H.FX_RACK_STATUS_JSON, "w") as f:
    json.dump({"slots": [{"idx": 0, "running": False}]}, f)
no_rack = {p for p in ports if not p.startswith('fx-rack-')}   # slot 0 has NO ports
H.RESULTS.clear()
H.sec_fx(no_rack)
lv = levels("fx.slot")
check("a saved-ON slot not started since boot is IDLE, not DEAD",
      ("idle", "fx.slot.0") in lv and not any(l == "dead" for l, _ in lv), lv)
with open(H.FX_RACK_STATUS_JSON, "w") as f:
    json.dump({"slots": [{"idx": 0, "running": True}]}, f)
H.RESULTS.clear()
H.sec_fx(no_rack)
check("...but a slot the manager STARTED whose ports are gone is still DEAD",
      ("dead", "fx.slot.0") in levels("fx.slot"), levels("fx.slot"))
os.remove(H.FX_RACK_STATUS_JSON)
H.RESULTS.clear()
H.sec_fx(no_rack)
check("...and with no status file (no information) it is DEAD as before",
      ("dead", "fx.slot.0") in levels("fx.slot"), levels("fx.slot"))

# -- main_jackd_line: the MAIN jackd, never a card's own server (2026-10-19) ---
# After a reboot the pisound's own server (jackd -n pisound) started first and
# the health check read it as the main one ("jackd is on hw:pisound").
PS = "\n".join([
    "ARGS",
    "/usr/bin/jackd -n pisound -R --port-max 256 -dalsa -dhw:pisound -r 48000 -p 1024 -n 2",
    "/usr/bin/jackd -R --port-max 256 -dalsa -dhw:L12 -r 48000 -p 1024 -n 2",
])
check("main_jackd_line picks the UNNAMED jackd even when a named one is listed first",
      "-dhw:L12" in (H.main_jackd_line(PS) or ""), H.main_jackd_line(PS))
check("...and returns None when only a card's own server runs",
      H.main_jackd_line(PS.splitlines()[1]) is None,
      H.main_jackd_line(PS.splitlines()[1]))

for f in os.listdir(d):
    os.remove(os.path.join(d, f))
os.rmdir(d)

(cfg.MODULE_OUT_KEYS, cfg.load_module_out, cfg.resolve_module_out, H._phys_playback) = _orig

total = passed + failed
print(f"\n{passed} passed, {failed} failed  (expected {EXPECTED})")
if total != EXPECTED:
    print(f"COUNT DRIFT: ran {total}")
    sys.exit(1)
sys.exit(1 if failed else 0)
