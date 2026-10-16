#!/usr/bin/env python3
"""Regression suite for the /fx rack's I/O -- INTERNAL mode by MODULE (2026-09-28).

The audio-host split retired the single ORC1-Synth client on 2026-09-24, but the
/fx page's INTERNAL preset still wired the rack's send from ORC1-Synth:out_L/R
(a port that no longer exists), and the dry-cut that keeps you from hearing dry
+ wet was hardcoded to ORC1-Synth too, so it cut nothing. owner's choice: the
INTERNAL source is ONE module, by NAME; the send is its outs and the return is
wherever its module output sends it, resolved live (orc1_config.fx_io_resolve).

Also covered: switching the source while the rack runs used to leave the OLD
module cut and the NEW one playing dry (the rack's held token made the re-cut a
no-op, and release re-read fx_io, reconnecting the wrong ports).

Pure stdlib + the ORC modules (no jalv, no jackd: jack calls are stubbed).
Everything that touches a file runs in a temp HOME. Safe on the live rig.
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_SANDBOX = tempfile.mkdtemp(prefix="fxio_test_")
os.environ["HOME"] = _SANDBOX           # before any ~ is expanded below
import orc1_config as cfg
cfg.MODULE_OUT_FILE = os.path.join(_SANDBOX, ".orc1_module_outputs.json")

PASS = 0
FAIL = 0


def ok(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ! {name}")


def read(p):
    with open(os.path.join(HERE, p), encoding="utf-8") as f:
        return f.read()


def set_module_outs(d):
    with open(cfg.MODULE_OUT_FILE, "w") as f:
        json.dump(d, f)


PHYS = ["system:playback_1", "system:playback_2",
        "system:playback_3", "system:playback_4"]

# ── fx_io_resolve ────────────────────────────────────────────────────────────
set_module_outs({"poly": "out-3-4", "drum": "none"})
r = cfg.fx_io_resolve({"mode": "internal", "module": "poly"}, PHYS)
ok((r["send_l"], r["send_r"]) == ("ORC1-Poly:out_L", "ORC1-Poly:out_R"),
   "internal poly: send = the module's outs")
ok((r["return_l"], r["return_r"]) == ("system:playback_3", "system:playback_4"),
   "internal poly: return = its module output (out-3-4)")
ok(r["return_none"] is False, "internal poly: return_none False")

r = cfg.fx_io_resolve({"mode": "internal", "module": "drum"}, PHYS)
ok((r["return_l"], r["return_r"]) == ("", "") and r["return_none"] is True,
   "module output 'none' -> empty return, flagged")

r = cfg.fx_io_resolve({"mode": "internal", "module": "chip"}, PHYS)
ok((r["return_l"], r["return_r"]) == ("system:playback_1", "system:playback_2"),
   "module with no saved output -> main pair (same default as the host)")

called = []
def _phys():
    called.append(1)
    return PHYS
hw = dict(cfg.FX_IO_HARDWARE, module="poly")
ok(cfg.fx_io_resolve(hw, _phys) == hw and not called,
   "hardware mode passes through and never asks jackd")
cust = {"mode": "internal", "module": "", "send_l": "a:1", "send_r": "a:2",
        "return_l": "b:1", "return_r": "b:2"}
ok(cfg.fx_io_resolve(cust, _phys) == cust and not called,
   "internal with custom ports (no module) passes through")
ok(cfg.fx_io_resolve({"mode": "internal", "module": "ORC1-Synth"}, PHYS)
   .get("send_l") is None, "unknown module is not resolved to anything")

# Every module key the web lists must resolve to its own client.
for k in cfg.MODULE_OUT_KEYS:
    rr = cfg.fx_io_resolve({"mode": "internal", "module": k}, PHYS)
    ok(rr["send_l"] == cfg.MODULE_JACK_NAMES[k] + ":out_L", f"module {k} resolves")

# ── load_fx_io (reads ~/.orc1_fx_io.json at call time) ───────────────────────
fx_file = os.path.join(_SANDBOX, ".orc1_fx_io.json")
ok(cfg.load_fx_io(PHYS)["mode"] == "hardware", "no file -> hardware defaults")
with open(fx_file, "w") as f:
    json.dump({"mode": "internal", "module": "poly",
               "send_l": "ORC1-Synth:out_L", "send_r": "ORC1-Synth:out_R",
               "return_l": "x", "return_r": "y"}, f)
io = cfg.load_fx_io(PHYS)
ok(io["send_l"] == "ORC1-Poly:out_L" and io["return_l"] == "system:playback_3",
   "stored stale ports are overridden by the module's live ports")
os.remove(fx_file)

# ── fx_io_apply (the POST rules) ─────────────────────────────────────────────
A = cfg.fx_io_apply
cur_hw = dict(cfg.FX_IO_HARDWARE)
n, e = A(cur_hw, {"preset": "internal", "module": "chip"})
ok(e is None and n["mode"] == "internal" and n["module"] == "chip",
   "preset internal + module")
n, e = A(cur_hw, {"preset": "internal"})
ok(e and n is cur_hw, "preset internal with no module anywhere -> error, unchanged")
n, e = A(dict(cur_hw, module="mono"), {"preset": "internal"})
ok(e is None and n["module"] == "mono", "preset internal reuses the remembered module")
n, e = A(cur_hw, {"module": "drum"})
ok(e is None and n["mode"] == "internal" and n["module"] == "drum",
   "bare {module} switches to internal on it")
n, e = A(cur_hw, {"module": "ORC1-Synth"})
ok(e and n is cur_hw, "unknown module rejected")
cur_int = {"mode": "internal", "module": "poly", "send_l": "ORC1-Poly:out_L",
           "send_r": "ORC1-Poly:out_R", "return_l": "system:playback_3",
           "return_r": "system:playback_4"}
n, e = A(cur_int, {"preset": "hardware"})
ok(e is None and n["mode"] == "hardware" and n["send_l"] == "pisound-in:capture_1"
   and n["module"] == "poly", "preset hardware -> pisound ports, module remembered")
n, e = A(cur_int, {"mode": "hardware"})
ok(e is None and n["return_l"] == "pisound-out:playback_1",
   "bare mode change brings that mode's ports (no internal-cut on hw ports)")
n, e = A(cur_int, {"mode": "internal", "send_l": "c:1", "send_r": "c:2",
                   "return_l": "d:1", "return_r": "d:2"})
ok(e is None and n["module"] == "" and n["send_l"] == "c:1",
   "explicit ports -> custom (module dropped so nothing overrides them)")
ok(A(cur_int, {"preset": "bogus"})[1] and A(cur_int, {"mode": "bogus"})[1],
   "unknown preset / mode rejected")

# ── FXManager dry-cut: cuts send->return, restores EXACTLY what it cut ──────
import orc1_fx
calls = []
orc1_fx._jack_silent = lambda cmd, s, d: calls.append((cmd, s, d))
state = {"io": {"mode": "internal"},
         "ports": {"FX_SEND_L": "ORC1-Poly:out_L", "FX_SEND_R": "ORC1-Poly:out_R",
                   "FX_RETURN_L": "system:playback_3", "FX_RETURN_R": "system:playback_4"}}
orc1_fx._read_fx_io = lambda: dict(state["io"])
orc1_fx._fcm._get_fx_ports = lambda: dict(state["ports"])
import threading
fm = orc1_fx.FXManager.__new__(orc1_fx.FXManager)
fm._internal_count, fm._internal_lock = 0, threading.Lock()
fm._internal_cut, fm._internal_chains = [], set()
fm.dbg = lambda *a, **k: None

ok(fm.internal_acquire() is True, "acquire in internal mode takes a token")
ok(calls == [("jack_disconnect", "ORC1-Poly:out_L", "system:playback_3"),
             ("jack_disconnect", "ORC1-Poly:out_R", "system:playback_4")],
   "first acquire cuts the SOURCE's own dry wires (not ORC1-Synth)")
calls.clear()
state["ports"] = {"FX_SEND_L": "ORC1-Chip:out_L", "FX_SEND_R": "ORC1-Chip:out_R",
                  "FX_RETURN_L": "system:playback_1", "FX_RETURN_R": "system:playback_2"}
fm.internal_release()
ok(calls == [("jack_connect", "ORC1-Poly:out_L", "system:playback_3"),
             ("jack_connect", "ORC1-Poly:out_R", "system:playback_4")],
   "release restores the pairs it CUT even after fx_io moved to another module")
calls.clear()
fm.internal_release()
ok(calls == [] and fm._internal_count == 0, "release with no token is a no-op")
state["io"] = {"mode": "hardware"}
ok(fm.internal_acquire() is False and calls == [] and fm._internal_count == 0,
   "hardware mode: no cut, no token")

# ── Rack: rebuild re-cuts against the CURRENT source ─────────────────────────
import orc1_fx_rack
rk = orc1_fx_rack.RackManager.__new__(orc1_fx_rack.RackManager)
rk._lock, rk._runtime, rk._holds_internal = threading.Lock(), [], False
rk.dbg = lambda *a, **k: None
rk._fx = fm
state["io"] = {"mode": "internal"}
state["ports"] = {"FX_SEND_L": "ORC1-Poly:out_L", "FX_SEND_R": "ORC1-Poly:out_R",
                  "FX_RETURN_L": "system:playback_3", "FX_RETURN_R": "system:playback_4"}
rk._teardown_locked = lambda: None
rk._build_locked = lambda: rk._acquire_internal()     # an active rack
calls.clear()
rk._apply_worker(None)
ok(rk._holds_internal and ("jack_disconnect", "ORC1-Poly:out_L", "system:playback_3") in calls,
   "rack build cuts the poly's dry path")
calls.clear()
state["ports"] = {"FX_SEND_L": "ORC1-Chip:out_L", "FX_SEND_R": "ORC1-Chip:out_R",
                  "FX_RETURN_L": "system:playback_1", "FX_RETURN_R": "system:playback_2"}
rk._apply_worker(None)                                  # = SET_FX_IO -> rack.apply()
ok(("jack_connect", "ORC1-Poly:out_L", "system:playback_3") in calls,
   "source switch while running: OLD module's dry path is restored")
ok(("jack_disconnect", "ORC1-Chip:out_L", "system:playback_1") in calls,
   "source switch while running: NEW module's dry path is cut")
ok(fm._internal_count == 1, "exactly one token held after the switch")
state["io"] = {"mode": "hardware"}
calls.clear()
rk._apply_worker(None)
ok(not rk._holds_internal and fm._internal_count == 0 and
   ("jack_connect", "ORC1-Chip:out_L", "system:playback_1") in calls,
   "switch to hardware: dry restored, rack holds no phantom token")

# ── Source nets: nothing still names the retired client for fx ───────────────
for p in ("orc1_web.py", "orc1_fx.py", "static/js/fx.js"):
    src = read(p)
    ok('"ORC1-Synth:out' not in src and "'ORC1-Synth:out" not in src,
       f"{p} no longer wires ORC1-Synth:out (as a string literal)")
js = read("static/js/fx.js")
ok("loadChains" not in js, "fx.js: no call to the undefined loadChains()")
ok("setModule" in js and 'id="io-mod"' in read("templates/fx.html"),
   "/fx has the INTERNAL module picker")
ok("fx_io_apply" in read("orc1_web.py") and '"module"' in read("orc1.py"),
   "web POST uses fx_io_apply; brain SET_FX_IO persists module")

EXPECTED = 41
print(f"\n{PASS} passed, {FAIL} failed  (expected {EXPECTED})")
sys.exit(1 if (FAIL or PASS != EXPECTED) else 0)
