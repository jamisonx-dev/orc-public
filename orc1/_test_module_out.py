#!/usr/bin/env python3
"""Regression suite for PER-MODULE OUTPUT ROUTING (2026-09-15).

The audio-host split left every module force-connecting its out_L/out_R to the
first physical playback pair on startup, so routing a module elsewhere left it
ALSO on the mains ("still comes through the original"). The fix: a persisted
per-module destination the hosts respect at startup, rewritten live + persisted
by the /api/module_outputs web endpoint. The model (resolver + saved-file) lives
in orc1_config so hosts (via orc1_synth) and the light web process share it.

Pure stdlib. Imports orc1_config (numpy-free) and reads the host / synth / web /
recorder SOURCE for the wiring nets. Safe to run anywhere.
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_config as cfg

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


# ── resolver ────────────────────────────────────────────────────────────────
phys4 = ["system:playback_1", "system:playback_2",
         "system:playback_3", "system:playback_4"]
phys2 = phys4[:2]
r = cfg.resolve_module_out
MAIN = ("system:playback_1", "system:playback_2")
ok(r("none", phys4) is None,                     "none -> None (no output)")
ok(r("out-1-2", phys4) == MAIN,                  "out-1-2 -> first pair")
ok(r("out-3-4", phys4) == ("system:playback_3", "system:playback_4"),
   "out-3-4 -> second pair")
ok(r("out-3-4", phys2) == MAIN,                  "out-3-4 on 2-port card -> main fallback")
ok(r(None, phys4) == MAIN,                       "None (unset) -> main")
ok(r("garbage", phys4) == MAIN,                  "garbage -> main")
ok(r("out-9-10", phys4) == MAIN,                 "out-of-range -> main")
ok(r("out-1", phys4) == MAIN,                    "malformed 2-part id -> main")
ok(r("out-a-b", phys4) == MAIN,                  "non-integer id -> main")
ok(r("out-1-2", []) is None,                     "no phys ports -> None")
ok(r("out-1-1", phys4) == ("system:playback_1", "system:playback_1"),
   "same-index pair honoured")
ok(r(None, ["system:playback_1"]) == ("system:playback_1", "system:playback_1"),
   "single-port card duplicates the one port")

# ── load_module_out (RMW-friendly file reader) ───────────────────────────────
tmp = tempfile.mkdtemp()
orig = cfg.MODULE_OUT_FILE
try:
    cfg.MODULE_OUT_FILE = os.path.join(tmp, "mo.json")
    ok(cfg.load_module_out("poly") is None,      "missing file -> None")
    with open(cfg.MODULE_OUT_FILE, "w") as f:
        f.write("{ not valid json")
    ok(cfg.load_module_out("poly") is None,      "corrupt file -> None")
    with open(cfg.MODULE_OUT_FILE, "w") as f:
        json.dump({"poly": "out-3-4", "chip": 42}, f)
    ok(cfg.load_module_out("poly") == "out-3-4", "valid string value")
    ok(cfg.load_module_out("chip") is None,      "non-string value -> None")
    ok(cfg.load_module_out("mono") is None,      "absent key -> None")
finally:
    cfg.MODULE_OUT_FILE = orig

# ── key tables consistent ────────────────────────────────────────────────────
ok(set(cfg.MODULE_JACK_NAMES) == set(cfg.MODULE_OUT_KEYS), "JACK names cover keys")
ok(set(cfg.MODULE_LABELS) == set(cfg.MODULE_OUT_KEYS),     "labels cover keys")
ok(len(cfg.MODULE_OUT_KEYS) == 6,                          "6 modules")
ok(all(cfg.MODULE_JACK_NAMES[k].startswith("ORC1-")
       for k in cfg.MODULE_OUT_KEYS),                      "jack names are ORC1-*")

# ── host wiring: each host calls autoconnect_module_out with its own key ──────
HOST_KEY = {
    "orc1_poly_host.py": "poly", "orc1_chip_host.py": "chip",
    "orc1_drum_host.py": "drum", "orc1_sampler_host.py": "sampler",
    "orc1_mono_host.py": "mono", "orc1_bbkit_host.py": "bbkit",
}
for fname, key in HOST_KEY.items():
    src = read(fname)
    has_call = "autoconnect_module_out(" in src
    ok(has_call, f"{fname} calls autoconnect_module_out")
    tail = src.split("autoconnect_module_out(", 1)[1][:140] if has_call else ""
    ok(f'"{key}"' in tail, f"{fname} passes key '{key}'")
    ok("JACK_AUTOCONNECT:" not in src, f"{fname} old blind auto-connect block gone")

# ── synth wrapper delegates + honours JACK_AUTOCONNECT ───────────────────────
s = read("orc1_synth.py")
ok("def autoconnect_module_out(" in s,      "synth defines autoconnect_module_out")
ok("if not JACK_AUTOCONNECT:" in s,         "synth honours JACK_AUTOCONNECT")
ok("resolve_module_out(" in s and "load_module_out(" in s,
   "synth delegates to cfg resolver")

# ── web endpoints + teardown-before-connect ──────────────────────────────────
w = read("orc1_web.py")
ok("/api/module_outputs" in w,              "web exposes /api/module_outputs")
ok("def api_module_outputs_set(" in w,      "web has the POST handler")
post = w.split("def api_module_outputs_set(", 1)[1].split("\n@app.route", 1)[0]
ok("jack_disconnect" in post and post.index("jack_disconnect") < post.index("jack_connect"),
   "POST tears down BEFORE it connects")
ok("_mo_write(" in post,                    "POST persists the choice")

# ── skipback dead fallback fixed ─────────────────────────────────────────────
sb = read("skipback/skipback_recorder.py")
auto_pairs = sb.split("AUTO_PAIRS = [", 1)[1].split("]", 1)[0]
ok("ORC1-Synth" not in auto_pairs,          "dead ORC1-Synth removed from AUTO_PAIRS")
ok('("system:capture_1", "system:capture_2")' in auto_pairs,
   "generic capture_1/2 fallback present in AUTO_PAIRS")

EXPECTED = 48
print(f"\n{PASS} passed, {FAIL} failed  (expected {EXPECTED})")
sys.exit(1 if (FAIL or PASS != EXPECTED) else 0)
