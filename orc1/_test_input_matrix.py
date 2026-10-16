#!/usr/bin/env python3
"""
Input matrix — stage:"pre" preemptive routing (option B, P1).

Drives the REAL RoutingEngine.process() with a stage arg, and source-nets the
_route_message wiring (the pre call + its ordering BEFORE channel dispatch, which
is what lets a rule claim a channel a module owns). Pure stdlib + mido; safe to
run while the rig is in use (touches no live graph; overrides the engine's rule
lists after construction so it never reads the live routes file).

Mutation-prove it: make process() ignore `stage` -> the stage-filter checks fail;
move the pre call AFTER the _SYNTH_CHS dispatch -> the ordering net fails.
"""
import ast, os, sys
import mido
from orc1_routes import RoutingEngine

HERE = os.path.dirname(os.path.abspath(__file__))

class _Cfg:
    MIDI_REMAP = []

PORT_OUTS = {"performve": None, "pisound": None, "synth": None}
R = RoutingEngine(_Cfg, PORT_OUTS)
R._static  = []          # isolate from the live ~/.orc1_routes.json
R._devices = {}

def non(ch, note=60, vel=100): return mido.Message('note_on',  channel=ch-1, note=note, velocity=vel)
def noff(ch, note=60):          return mido.Message('note_off', channel=ch-1, note=note, velocity=0)
def cc(ch, control=1, value=0): return mido.Message('control_change', channel=ch-1, control=control, value=value)

_p = _f = 0
def check(cond, label):
    global _p, _f
    if cond: _p += 1
    else: _f += 1; print(f"  FAIL: {label}")

PRE_FWD = {"stage": "pre", "in_ch": 5, "in_type": "note",
           "out_port": "performve", "out_ch": 1, "action": "forward"}

# 1-2 stage filter isolates pre from post
R._static = [PRE_FWD]
out_pre  = R.process(non(5), "usb", stage="pre")
out_post = R.process(non(5), "usb", stage="post")
check(out_pre and out_pre[0][0] == "performve", "pre rule fires at stage=pre")
check(out_post is None, "pre rule INVISIBLE at stage=post")

# 3-4 a post (default, no stage key) rule is the mirror image
R._static = [{"in_ch": 6, "in_type": "note", "out_port": "pisound", "out_ch": 2}]
check(R.process(non(6), "usb", stage="post") is not None, "post rule (no stage key) fires at stage=post")
check(R.process(non(6), "usb", stage="pre") is None, "post rule INVISIBLE at stage=pre")

# 5 forward remaps to out_ch (ch1 -> channel 0)
R._static = [PRE_FWD]
o = R.process(non(5, note=64), "usb", stage="pre")
check(o and o[0][1].channel == 0 and o[0][1].note == 64, "pre forward remaps to out_ch1, keeps note")

# 6 consume claims + suppresses
R._static = [{"stage": "pre", "in_ch": 5, "in_type": "note", "action": "consume"}]
check(R.process(non(5), "usb", stage="pre") == [], "pre consume returns [] (claimed, suppressed)")

# 7-8 in_port scopes the source (usb vs din) — the 'listen to a specific in' need
R._static = [{"stage": "pre", "in_port": "din", "in_ch": 5, "in_type": "note",
              "out_port": "performve", "out_ch": 1}]
check(R.process(non(5), "din", stage="pre") is not None, "in_port din matches din source")
check(R.process(non(5), "usb", stage="pre") is None,     "in_port din does NOT match usb source")

# 9 note-off pairs automatically (same rule matches the off by note number)
R._static = [PRE_FWD]
check(R.process(noff(5), "usb", stage="pre") is not None, "note_off is claimed by the same rule (pairing)")

# 10 wrong channel doesn't match
check(R.process(non(7), "usb", stage="pre") is None, "pre rule ignores a non-matching channel")

# 11-12 value range
R._static = [{"stage": "pre", "in_ch": 5, "in_type": "note", "match": [36, 51],
              "out_port": "performve", "out_ch": 1}]
check(R.process(non(5, note=40), "usb", stage="pre") is not None, "range rule matches an in-range note")
check(R.process(non(5, note=90), "usb", stage="pre") is None,     "range rule rejects an out-of-range note")

# 13 non-note left alone by a note rule
R._static = [PRE_FWD]
check(R.process(cc(5), "usb", stage="pre") is None, "a note-typed pre rule ignores a CC")

# ── placeholders are NOT rules (the starter file's _comment/_disabled entries) ─
# They used to match everything (all defaults = "any") and forward to pisound.
from orc1_routes import is_rule
PARKED = {"_disabled": {"in_ch": 1, "in_type": "note", "out_port": "pisound", "out_ch": 1}}
R._static = [{"_comment": "annotation only"}, PARKED, {}]
check(R.process(non(5), "usb", stage="post") is None and R.process(non(5), "din", stage="post") is None,
      "comment / _disabled / empty entries never match (no DIN spam)")
check(not is_rule({"_comment": "x"}) and not is_rule(PARKED) and not is_rule({}),
      "is_rule rejects comment, parked and empty entries")
R._static = [{"_comment": "my note", "in_ch": 5, "out_port": "pisound"}]
check(R.process(non(5), "usb", stage="post") is not None,
      "a REAL rule that carries a _comment annotation still matches")
R._static = [{"_disabled": True, "in_ch": 5, "out_port": "pisound"}]
check(R.process(non(5), "usb", stage="post") is None, "any _disabled key parks a rule")

# ── source nets on _route_message (orc1.py) ────────────────────────────────
SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
tree = ast.parse(SRC)
route = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_route_message":
        route = ast.get_source_segment(SRC, node).replace("\r\n", "\n"); break
check(route is not None, "_route_message found")
check('router.process(msg, source_label, stage="pre", source_name=source_name)' in route,
      "_route_message runs the pre stage (with the real source name)")
_seg = route.split("_pre is not None")[1].split("ch = msg.channel")[0] if "_pre is not None" in route else ""
check("send_to(_pk, _m)" in _seg and "return" in _seg,
      "_route_message forwards each pre-claim then returns (consumes) BEFORE ch dispatch")
# THE load-bearing net: the pre pass must run BEFORE the module-channel dispatch,
# or it could never claim a channel a module owns.
check('stage="pre"' in route and 'if ch in _SYNTH_CHS' in route
      and route.index('stage="pre"') < route.index('if ch in _SYNTH_CHS'),
      "pre pass runs BEFORE the _SYNTH_CHS module dispatch (preempts channel ownership)")
# process() gained the stage parameter
check('def process(self, msg, source_port=' in open(os.path.join(HERE, "orc1_routes.py"),
      encoding="utf-8").read() and "stage='post'" in open(os.path.join(HERE, "orc1_routes.py"),
      encoding="utf-8").read(), "RoutingEngine.process has a stage param defaulting to post")

# ── guard helpers: CH13 reservation + DIN anti-feedback (P2, behavioral) ──────
class _Cfg2:
    CH_CTRL = 13
def _fsrc(name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise SystemExit(f"{name} not found")
NSG = {"cfg": _Cfg2}
exec(_fsrc("_pre_stage_channel_ok"), NSG)
exec(_fsrc("_din_echo_blocked"), NSG)
_chok = NSG["_pre_stage_channel_ok"]; _dblock = NSG["_din_echo_blocked"]
check(_chok(13) is False, "guard: pre CANNOT claim CH_CTRL(13) — ORC control inbox reserved")
check(_chok(5) is True and _chok(12) is True and _chok(14) is True,
      "guard: pre CAN claim every other channel (5/12/14 stay free)")
check(_dblock("din", "pisound") is True, "guard: DIN->pisound echo blocked (anti-feedback)")
check(_dblock("din", "performve") is False and _dblock("usb", "pisound") is False,
      "guard: DIN->non-DIN out and USB->pisound NOT blocked")
check("_pre_stage_channel_ok(msg.channel + 1)" in route, "_route_message gates pre on the CH_CTRL guard")
check("_din_echo_blocked(source_label, _pk)" in route, "_route_message drops DIN-echo pre outputs")

# ── 2026-09-24: per-device sources, always-on profiles, post-stage DIN guard ──
# in_port "self" = only the profile's own device; any other text = a device name.
MPC_NAME, BSP_NAME = "MPC1000 MIDI 1", "Arturia BeatStep Pro:Arturia BeatStep Pro MIDI 1"
SELF_RULE = {"in_port": "self", "in_ch": 5, "out_port": "pisound"}
R._static  = []
R._devices = {"MPC1000": {"port_match": "MPC", "rules": [SELF_RULE], "active": True}}
check(R.process(non(5), "usb", source_name=MPC_NAME) is not None,
      "in_port self: the profile's OWN device matches")
check(R.process(non(5), "usb", source_name=BSP_NAME) is None,
      "in_port self: another USB device does NOT match (the shared-'usb'-label gap)")
R._devices = {}
R._static  = [SELF_RULE]
check(R.process(non(5), "usb", source_name=MPC_NAME) is None,
      "in_port self on a STATIC rule never matches (it owns no device)")
R._static  = [{"in_port": "beatstep", "in_ch": 5, "out_port": "pisound"}]
check(R.process(non(5), "usb", source_name=BSP_NAME) is not None
      and R.process(non(5), "usb", source_name=MPC_NAME) is None,
      "in_port <device name>: matches that device only (case-insensitive substring)")
R._static  = [{"in_port": "usb", "in_ch": 5, "out_port": "pisound"}]
check(R.process(non(5), "usb", source_name=BSP_NAME) is not None
      and R.process(non(5), "din", source_name="pisound") is None,
      "in_port usb still means the KIND, whatever the device is called")

# always-on profile: loaded active, never deactivated, survives save -> reload
import json, tempfile
_tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"version": 1, "devices": {
    "GLOBAL": {"port_match": "(always on)", "always": True,
               "rules": [{"stage": "pre", "in_port": "din", "in_ch": 5,
                          "out_port": "performve", "out_ch": 1}]},
    "MPC1000": {"port_match": "MPC", "rules": []}}}, _tmp); _tmp.close()
R._static = []; R._devices = {}; R._path = _tmp.name
R._load_file()
check("GLOBAL" in R.active_devices() and "MPC1000" not in R.active_devices(),
      "always-on profile is ACTIVE at load with nothing connected; a normal one is not")
check(R.process(non(5), "din", stage="pre", source_name="pisound") is not None,
      "always-on pre rule fires with no device connected (the P2.5 global-rule gap)")
R.device_disconnected("(always on)")
R.device_disconnected("MPC1000 MIDI 1")
check("GLOBAL" in R.active_devices(), "device_disconnected never switches an always-on profile off")
R.save(); R._devices = {}; R._load_file()
check(R._devices.get("GLOBAL", {}).get("always") is True
      and R._devices["GLOBAL"]["active"] is True
      and "always" not in json.load(open(_tmp.name))["devices"]["MPC1000"],
      "save() keeps always:true through a save/reload (and doesn't add it elsewhere)")
os.unlink(_tmp.name)

# source nets: the real port name reaches the engine; post outputs are DIN-guarded
check('_route_message(msg, "usb", port_name)' in SRC, "USB handler passes the real port name")
check('_route_message(msg, "din", din_name)' in SRC, "DIN loop passes the DIN port name")
_post = route.split("if ch in _SYNTH_CHS")[1] if "if ch in _SYNTH_CHS" in route else ""
check("_din_echo_blocked(source_label, port_key)" in _post,
      "post-stage rule outputs are DIN-echo guarded like pre and _pass()")

# ── 2026-10-14: device-chatter SysEx never reaches the DIN (the Fire's hello) ──
NSD = {}
exec(SRC[SRC.index("_DEVICE_SYSEX_PREFIXES = "):].split("\n\n")[0], NSD)
exec(_fsrc("_sysex_is_device_chatter"), NSD)
_chat = NSD["_sysex_is_device_chatter"]
FIRE_HELLO = mido.Message("sysex", data=[0x7D, 0x4F, 0x52, 0x43, 0x00, 0x01]).data
check(_chat(FIRE_HELLO), "the Fire firmware's hello reply (7D 'ORC') is device chatter")
check(_chat([0x00, 0x20, 0x6B, 0x7F, 0x42]), "Arturia config SysEx is still device chatter")
check(not _chat([0x7D, 0x01, 0x02]) and not _chat([0x41, 0x10, 0x00])
      and not _chat([0x7D, 0x4F, 0x52]) and not _chat([]),
      "other SysEx (another 7D user, Roland, a truncated prefix, empty) still passes")
_nc = route.split("if not hasattr(msg, 'channel'):")[1] if "if not hasattr(msg, 'channel'):" in route else ""
check("_sysex_is_device_chatter(msg.data)" in _nc
      and _nc.index("_sysex_is_device_chatter(msg.data)") < _nc.index("_pass(msg)"),
      "_route_message drops device-chatter SysEx BEFORE the _pass to the DIN")

EXPECTED = 44
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or (_p + _f) != EXPECTED:
    sys.exit(1)
