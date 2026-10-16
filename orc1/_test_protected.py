#!/usr/bin/env python3
"""
PROTECTED CHANNELS (2026-10-14): one live setting (orc1_config.protected_config, file
~/.orc1_protected_channels.json, edited on /matrix) that every channel picker refuses.

Covers: the defaults (12, 13, 14) and the file, 13 locked on, the live set-like view the
brain / hosts use (follows the file without a restart), protect_error, the /api endpoints
(lock, "module bound there" refusal, names), and the save-time refusals: pedal (new vs
already-saved entries), routes POST, chord out, voice thru. Plus source checks that the
brain's save points call the guard.

Pure: every file is repointed into a temp dir, write_cmd is captured, nothing reaches the
brain. Safe while the rig is in use.

Mutation-prove: lock removed -> "13 stays on"; the already-saved exception removed from
refuse_protected -> "an entry already saved stays"; the routes guard removed -> the routes
check; the bound-module refusal removed -> its check.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_config as C            # noqa: E402
import orc1_pedal as P             # noqa: E402

EXPECTED = 27
_p = _f = 0


def check(cond, label, extra=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label} {extra}")


TMP = tempfile.mkdtemp(prefix="prot_")
C.PROTECTED_FILE = os.path.join(TMP, "protected.json")
C.BINDINGS_FILE = os.path.join(TMP, "bindings.json")


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)
    t = time.time() + write.n          # a distinct mtime each write (the cache keys on mtime_ns)
    write.n += 1
    os.utime(path, (t, t))


write.n = 1

# ── the setting ─────────────────────────────────────────────────────────────
check(C.protected_channels() == {12, 13, 14}, "no file: the defaults 12, 13, 14", C.protected_channels())
check(14 in C.PROTECTED and 5 not in C.PROTECTED and sorted(C.PROTECTED) == [12, 13, 14] and len(C.PROTECTED) == 3,
      "PROTECTED behaves like the old set")
write(C.PROTECTED_FILE, {"channels": {"12": {"on": False}, "13": {"on": False}, "14": {"on": True, "name": "MPC"},
                                      "5": {"on": True, "name": "my synth"}}})
check(C.protected_channels() == {5, 13, 14}, "the file wins (12 off, 5 on)", C.protected_channels())
check(13 in C.PROTECTED, "13 stays on even when the file says off (locked)")
check(5 in C.PROTECTED and 12 not in C.PROTECTED, "the live view follows the file with no restart")
check(C.protect_error(5) == "ch 5 is protected (my synth) -- switch that off on /matrix to use it",
      "protect_error names the channel's job", C.protect_error(5))
check(C.protect_error(12) == "" and C.protect_error("x") == "bad channel", "free channel = ''; junk = bad channel")
write(C.PROTECTED_FILE, "not json at all")
check(C.protected_channels() == {12, 13, 14}, "an unreadable file falls back to the defaults")

# ── pedal: a NEW pick of a protected channel is refused, an old one kept ─────
write(C.PROTECTED_FILE, {"channels": {"14": {"on": True, "name": "MPC"}}})
prev, _ = P.validate_config({"map": {"pr": {"3": "fwd:pisound:14"}}, "exp": {"A": {"port": "synth", "ch": 14, "cc": 7}}})
new, _ = P.validate_config({"map": {"pr": {"3": "fwd:pisound:14", "4": "fwd:pisound:14", "5": "fwd:pisound:2"}},
                            "exp": {"A": {"port": "synth", "ch": 14, "cc": 7}, "B": {"port": "synth", "ch": 14, "cc": 1}}})
errs = P.refuse_protected(new, prev, C.protect_error)
check(new["map"]["pr"].get(3) == "fwd:pisound:14" and new["exp"]["A"] == {"port": "synth", "ch": 14, "cc": 7},
      "an entry already saved stays (protecting never silently drops config)", new)
check(4 not in new["map"]["pr"] and new["exp"]["B"] is None, "a NEW pedal pick on ch 14 is refused", new)
check(new["map"]["pr"].get(5) == "fwd:pisound:2", "an unprotected pick passes")
check(len(errs) == 2 and all("protected" in e for e in errs), "each refusal is reported", errs)
new2, _ = P.validate_config({"map": {"pr": {"3": "fwd:pisound:14"}}})
new2["map"]["pr"][3] = "fwd:pisound:12"            # changed TO a now-unprotected channel
check(P.refuse_protected(new2, prev, C.protect_error) == [], "changing away from a protected channel is fine")

# ── the web endpoints ───────────────────────────────────────────────────────
import orc1_web as W               # noqa: E402  (side-effect free, see _test_patch_persist)
SENT = []
W.write_cmd = lambda a, p=None: SENT.append((a, p)) or True
W.ROUTES = os.path.join(TMP, "routes.json")
W._PEDAL_CONF = os.path.join(TMP, "pedal.json")
cl = W.app.test_client()
write(C.PROTECTED_FILE, {"channels": {"12": {"on": True, "name": "chord input"}, "14": {"on": True, "name": "MPC"}}})
write(C.BINDINGS_FILE, {"bindings": {"6": "poly", "10": "drums"}})

r = cl.post("/api/protected_channels", json={"channel": 13, "on": False})
check(r.status_code == 400 and 13 in C.protected_channels(), "the page can't switch 13 off")
r = cl.post("/api/protected_channels", json={"channel": 6, "on": True})
check(r.status_code == 400 and "poly" in r.get_json()["error"] and 6 not in C.protected_channels(),
      "protecting a channel a module is bound to is refused (move it first)", r.get_json())
r = cl.post("/api/protected_channels", json={"channel": 7, "on": True, "name": "  Behringer  "})
j = r.get_json()
check(r.status_code == 200 and 7 in C.protected_channels() and j["channels"]["7"]["name"] == "Behringer",
      "protect a free channel with a name", j)
r = cl.post("/api/protected_channels", json={"channel": 12, "on": False})
check(r.status_code == 200 and 12 not in C.protected_channels(), "12 (chord input) can be switched off")
g = cl.get("/api/protected_channels").get_json()["channels"]
check(len(g) == 16 and g["13"]["locked"] and not g["14"]["locked"] and g["14"]["on"] and not g["12"]["on"],
      "GET lists all 16 with on / name / locked", g)

SENT.clear()
r = cl.post("/api/chord_out", json={"port": "pisound", "channel": 14})
check(r.status_code == 400 and not SENT, "chord out on a protected channel: refused, nothing sent")
r = cl.post("/api/voice_thru", json={"src_ch": 1, "port": "pisound", "channel": 7})
check(r.status_code == 400 and not SENT, "voice thru to a protected channel: refused")
r = cl.post("/api/voice_thru", json={"src_ch": 1, "port": "pisound", "channel": 3})
check(r.status_code == 200 and SENT and SENT[-1][0] == "SET_VOICE_THRU", "voice thru to a free channel: sent")

old = {"version": 1, "devices": {"MPC": {"port_match": "MPC", "rules": [{"in_ch": 1, "out_port": "pisound", "out_ch": 14}]}}}
write(W.ROUTES, old)
keep = json.loads(json.dumps(old))
keep["devices"]["MPC"]["rules"].append({"in_ch": 2, "out_port": "pisound", "out_ch": 3})
r = cl.post("/api/routes", json=keep)
check(r.status_code == 200, "routes: saving the page with an OLD ch-14 rule still works", r.get_json())
bad = json.loads(json.dumps(keep))
bad["devices"]["MPC"]["rules"].append({"in_ch": 4, "out_port": "pisound", "out_ch": 14})
r = cl.post("/api/routes", json=bad)
check(r.status_code == 400 and "protected" in r.get_json()["error"], "routes: a NEW rule out to ch 14 is refused")

SENT.clear()
r = cl.post("/api/pedal", json={"config": {"map": {"pr": {"4": "fwd:pisound:14"}}}})
check(r.get_json()["config"]["map"]["pr"] == {} and any("protected" in e for e in r.get_json()["errors"]),
      "pedal POST: a new protected pick is dropped + reported before it reaches the brain", r.get_json())

# ── the brain's save points call the guard (orc1.py can't be imported) ───────
src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()


def block(start):
    return src.split(start, 1)[1].split("\n    elif action", 1)[0]


check("_protect_err(" in block('elif action == "KNOBMAP_SET":') and "_protect_err(" in block('elif action == "BB_OUTPUT":')
      and "_protect_err(" in block('elif action == "SET_VOICE_MAP":')
      and "_protect_err(" in block('elif action == "UNIVERSAL_SET_FADER":'),
      "KNOBMAP_SET, BB_OUTPUT, SET_VOICE_MAP, UNIVERSAL_SET_FADER all check the guard")
check("refuse_protected(" in src.split("def _pedal_conf_set", 1)[1].split("\ndef ", 1)[0],
      "the brain's pedal save runs refuse_protected")
check("cfg.PROTECTED" in src and "_MM_RESERVED" in src, "the brain's binding guard is the live view")

for f in os.listdir(TMP):
    os.remove(os.path.join(TMP, f))
os.rmdir(TMP)
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or (_p + _f) != EXPECTED:
    sys.exit(1)
