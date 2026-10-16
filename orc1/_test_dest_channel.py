#!/usr/bin/env python3
"""Chord-engine voice destinations NAME their module (2026-09-24).

The presets in orc1_config used the _CH_* constants frozen at import. After
chip P2 moved to ch 8 on /matrix, NES_CHORD's 3rd went to ch 6 = the POLY.
Now an internal ("synth") destination names the module ("chip_p2") and
orc1._dest_channel resolves it from the live bindings at play time; unbound =
silent. A plain channel number still means that channel.

orc1.py can't be imported (it is __main__ and opens hardware), so the
functions are AST-extracted and run against stub globals, like the chord
suites. Pure stdlib.
    python3 _test_dest_channel.py
"""
import ast, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_config as cfg

EXPECTED = 30
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
tree = ast.parse(src)
fns = {n.name: ast.get_source_segment(src, n) for n in tree.body
       if isinstance(n, ast.FunctionDef) and n.name in ("_dest_channel", "resolve")}
check("orc1.py defines _dest_channel and resolve", set(fns) == {"_dest_channel", "resolve"})

bindings = {"chip_p1": 9, "chip_p2": 8, "chip_tri": 11, "poly": 6}
g = {"_module_channel": lambda m: bindings.get(m),
     "state": {"voice_map": {}, "preset": "NES_CHORD",
               "chord_out": {"port": None, "channel": None}},
     "cfg": cfg, "mirror_active": lambda: False}
for code in fns.values():
    exec(code, g)
dc, resolve = g["_dest_channel"], g["resolve"]

check("a module on the synth bus -> its LIVE channel", dc("synth", "chip_p2") == 8)
bindings["chip_p2"] = 12
check("...follows a /matrix move", dc("synth", "chip_p2") == 12)
check("an unbound module -> None", dc("synth", "chip_noise2") is None)
check("a module name off the synth bus -> None (never guessed)", dc("pisound", "poly") is None)
check("a plain channel number is untouched", dc("pisound", 10) == 10 and dc("synth", 3) == 3)

bindings["chip_p2"] = 8
check("NES_CHORD 3rd plays chip P2's live channel (8, not the frozen 6)",
      resolve("CHORD_3RD") == ("synth", 8), f"{resolve('CHORD_3RD')}")
del bindings["chip_tri"]
check("a preset slot whose module is unbound is silent", resolve("CHORD_5TH") == (None, None),
      f"{resolve('CHORD_5TH')}")
bindings["chip_tri"] = 11
g["state"]["voice_map"] = {"CHORD_ROOT": {"port": "synth", "channel": "poly"}}
check("a voice-map override may name a module too", resolve("CHORD_ROOT") == ("synth", 6),
      f"{resolve('CHORD_ROOT')}")
g["state"]["voice_map"] = {}

synth_dests = [(n, slot, d["channel"]) for n, p in cfg.PRESETS.items()
               for slot, d in p.items()
               if isinstance(d, dict) and d.get("port") == "synth"]
check("the presets have synth destinations to check", len(synth_dests) >= 10, len(synth_dests))
check("every shipped synth destination names a module (no frozen channel numbers)",
      all(isinstance(ch, str) and ch in cfg.MODULE_KEYS for _, _, ch in synth_dests),
      [x for x in synth_dests if not isinstance(x[2], str)])
arp_src = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and n.name == "_arp_voice_resolve"]
check("the arp's ARP_DEST path resolves through _dest_channel",
      bool(arp_src) and ast.get_source_segment(src, arp_src[0]).count("_dest_channel(") >= 2)

# -- a voice-map entry with no channel is SILENT, not ch 1 (2026-10-01) -------
# /routes saved channel:null when its picker could not show an unbound module;
# resolve() then sent that slot to ch 1 = whatever module sat there.
g["state"]["voice_map"] = {"CHORD_ROOT": {"port": "synth", "channel": None},
                           "CHORD_3RD": {"port": "pisound"},
                           "CHORD_5TH": {"port": "pisound", "channel": 5}}
check("a voice-map entry with channel null is silent (not ch 1)",
      resolve("CHORD_ROOT") == (None, None), f"{resolve('CHORD_ROOT')}")
check("...and so is one with no channel key at all",
      resolve("CHORD_3RD") == (None, None), f"{resolve('CHORD_3RD')}")
check("a voice-map entry WITH a channel still plays it",
      resolve("CHORD_5TH") == ("pisound", 5), f"{resolve('CHORD_5TH')}")
g["state"]["voice_map"] = {}

# -- ARP_DEST set but unbound = SILENT, not a CHORD_ALL fallback (2026-10-01) --
# Behaviour, through the REAL ArpEngine._get_output_route + the real
# _arp_voice_resolve (nested in main(), so dedented and exec'd here).
import textwrap
import orc1_arp
_avr = textwrap.dedent(ast.get_source_segment(src, arp_src[0]))
g.update({"ARP_SILENT": orc1_arp.ARP_SILENT,
          "_ARP_SLOT_ORDER": ["CHORD_ROOT", "CHORD_3RD", "CHORD_5TH",
                              "CHORD_7TH", "CHORD_9TH"]})
exec(_avr, g)
_eng = orc1_arp.ArpEngine(lambda *a: None, lambda *a: None, lambda: None)
_eng.resolve_fn, _eng.voice_resolve_fn = resolve, g["_arp_voice_resolve"]
cfg.PRESETS["_T_ARP"] = {"CHORD_ALL": {"port": "synth", "channel": "poly"},
                         "ARP_DEST": {"port": "synth", "channel": "chip_p1"}}
cfg.PRESETS["_T_ALL"] = {"CHORD_ALL": {"port": "synth", "channel": "poly"}}
g["state"]["preset"] = "_T_ARP"
check("arp: a preset ARP_DEST plays its module's live channel",
      _eng._get_output_route(None, tone_idx=0) == ("synth", 9),
      f"{_eng._get_output_route(None, tone_idx=0)}")
del bindings["chip_p1"]
check("arp: ARP_DEST naming an UNBOUND module is silent (no CHORD_ALL leak)",
      _eng._get_output_route(None, tone_idx=0) == (None, None),
      f"{_eng._get_output_route(None, tone_idx=0)}")
bindings["chip_p1"] = 9
g["state"]["voice_map"] = {"ARP_DEST": None}
check("arp: a voice-map ARP_DEST of None (off) is silent",
      _eng._get_output_route(None, tone_idx=1) == (None, None),
      f"{_eng._get_output_route(None, tone_idx=1)}")
g["state"]["voice_map"] = {"ARP_DEST": {"port": "synth", "channel": "chip_noise2"}}
check("arp: a voice-map ARP_DEST naming an unbound module is silent",
      _eng._get_output_route(None, tone_idx=0) == (None, None),
      f"{_eng._get_output_route(None, tone_idx=0)}")
g["state"]["voice_map"] = {}
g["state"]["preset"] = "_T_ALL"
check("arp: NO ARP_DEST still falls back to CHORD_ALL (unchanged)",
      _eng._get_output_route(None, tone_idx=0) == ("synth", 6),
      f"{_eng._get_output_route(None, tone_idx=0)}")
del cfg.PRESETS["_T_ARP"], cfg.PRESETS["_T_ALL"]
g["state"]["preset"] = "NES_CHORD"

# -- /matrix lists EVERY module (2026-10-01: mono + bbkit had no row) ---------
import re
_mjs = open(os.path.join(HERE, "static", "js", "matrix.js"), encoding="utf-8").read()
_blk = _mjs.split("const MODULES = [", 1)[1].split("];", 1)[0]
_jskeys = re.findall(r"key:\s*'([a-z0-9_]+)'", _blk)
check("matrix.js MODULES has a row for every cfg MODULE_KEYS entry (and no extras)",
      sorted(_jskeys) == sorted(cfg.MODULE_KEYS),
      f"missing={sorted(set(cfg.MODULE_KEYS) - set(_jskeys))} "
      f"extra={sorted(set(_jskeys) - set(cfg.MODULE_KEYS))}")
check("the brain publishes module_keys in its state (pages list unbound modules)",
      '"module_keys":' in src and "list(_MM_KEYS)" in src)

# -- web: voice labels + one-shots follow the LIVE bindings (2026-10-01) -----
# /api/voice_thru labelled channels from the web's shipped cfg ("poly ch 8" =
# really chip P2 on the rig, mono/bbkit missing); /api/oneshot used CH_DRUM.
import json as _json, tempfile as _tf
_wsrc = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
_wt = ast.parse(_wsrc)
_lb = next(n for n in _wt.body if isinstance(n, ast.FunctionDef) and n.name == "_live_bindings")
_wg = {"os": os, "json": _json}
exec(ast.get_source_segment(_wsrc, _lb), _wg)
_live = _wg["_live_bindings"]
check("web _live_bindings: the brain state's table wins",
      _live({"bindings": {"poly": 6, "chip_p2": 8, "mono": 3}}) == {"poly": 6, "chip_p2": 8, "mono": 3})
_d = _tf.mkdtemp()
_bf = os.path.join(_d, "b.json")
with open(_bf, "w") as _f:
    _json.dump({"bindings": {"6": "poly", "2": "bbkit"}}, _f)
_old_bf = getattr(cfg, "BINDINGS_FILE", None)
cfg.BINDINGS_FILE = _bf
check("...no state -> the persisted bindings file (inverted to module -> ch)",
      _live(None) == {"poly": 6, "bbkit": 2}, _live(None))
cfg.BINDINGS_FILE = os.path.join(_d, "missing.json")
check("...neither -> {} (nothing bound), never the cfg CH_* seed", _live({}) == {})
cfg.BINDINGS_FILE = _old_bf
os.remove(_bf); os.rmdir(_d)
_vt = next(n for n in _wt.body if isinstance(n, ast.FunctionDef) and n.name == "api_voice_thru_get")
_vts = ast.get_source_segment(_wsrc, _vt)
check("/api/voice_thru lists the live bindings, not cfg SYNTH.CH_*",
      "_live_bindings(" in _vts and "CH_POLY" not in _vts and "CH_DRUM" not in _vts)
_os = next(n for n in _wt.body if isinstance(n, ast.FunctionDef) and n.name == "api_oneshot")
_oss = ast.get_source_segment(_wsrc, _os)
check("/api/oneshot uses the drums module's live channel, not CH_DRUM",
      '_live_bindings(' in _oss and '.get("drums")' in _oss and "CH_DRUM\", 10" not in _oss)

# -- brain cmd nets (orc1.py can't be imported; the writers are tested in
#    _test_fire via orc1_fire.knobmap_set_offline / _migrate_chord_banks) ----
_km = src.split('elif action == "KNOBMAP_SET"', 1)[1].split("\n    elif action", 1)[0]
check("KNOBMAP_SET works with the Fire unplugged and logs instead of `pass`",
      src.count('elif action == "KNOBMAP_SET":') == 1 and "knobmap_set_offline(" in _km
      and "! KNOBMAP_SET" in _km, _km[:80])
_cr = src.split('elif action == "CHORD_ROWS_SET"', 1)[1].split("\n    elif action", 1)[0]
check("CHORD_ROWS_SET (no Fire) migrates an unmarked file before writing the marker",
      "_migrate_chord_banks(" in _cr and "_CHORD_ROWS_VERSION" in _cr)
_rjs = open(os.path.join(HERE, "static", "js", "routes.js"), encoding="utf-8").read()
_co = _rjs.split("function _chOpts(", 1)[-1].split("\n}", 1)[0]
check("/routes voice-map picker: every module (unbound marked), synth port only",
      "module_keys" in _rjs and "'unbound'" in _co and "port === 'synth'" in _co)

total = passed + failed
print(f"\n{passed} passed, {failed} failed  (expected {EXPECTED})")
if total != EXPECTED:
    print(f"COUNT DRIFT: ran {total}")
    sys.exit(1)
sys.exit(1 if failed else 0)
