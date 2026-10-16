#!/usr/bin/env python3
"""Regression test for gen_cc_map.py's per-module CC lists (the /matrix param
drawer reads static/cc_map.json, built from these).

Added 2026-09-24 after a review found the drawer hiding real controls and
showing dead ones:
  * gb_wave listed only GameboyWaveVoice.set_cc -- none of the NESEngine mods
    the wave gets first (glide/pan/arp/... CC 5/86-90/110-113, vibrato 77/78),
    and not the harmonic knobs (a RANGE test on named constants, 102-109) or
    the store CC (`cc == _GB_STORE_CC`, 115).
  * the pulse1-only sweep CCs (79/80) showed on every chip voice, because an
    inner `if cc == 79:` lost its outer `voice == 'pulse1'` guard.

Source-only (AST), no live import: runs on the PC or the Pi.
    python3 _test_cc_map_gen.py
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_cc_map as G

EXPECTED = 26
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


mods = G.build_json({}, G.scan_handlers(G.SOURCES["synth"]))["modules"]
ccs = {k: {r["cc"] for r in v} for k, v in mods.items()}

wave = ccs["gb_wave"]
check("gb_wave: harmonic knobs 102-109 (range on named constants)",
      set(range(102, 110)) <= wave, sorted(wave))
check("gb_wave: store CC 115 (cc == NAME)", 115 in wave, sorted(wave))
check("gb_wave: shared mods from NESEngine (5, 86-90, 110-113)",
      {5, 86, 87, 88, 89, 90, 110, 111, 112, 113} <= wave, sorted(wave))
check("gb_wave: vibrato 77/78 (handled before the wave early-return)",
      {77, 78} <= wave, sorted(wave))
check("gb_wave: own handler still listed (1, 7, 20-24)",
      {1, 7, 20, 21, 22, 23, 24} <= wave, sorted(wave))
check("gb_wave: nothing from AFTER the wave early-return (3 noise mode, 74 filter)",
      not ({3, 74, 75} & wave), sorted(wave))
check("gb_wave: CC 1 described by the WAVE handler, not NESEngine's duty",
      all("duty" not in (r["desc"] or "").lower()
          for r in mods["gb_wave"] if r["cc"] == 1))

check("chip_p1: sweep 79/80/81 listed", {79, 80, 81} <= ccs["chip_p1"])
for k in ("chip_p2", "chip_tri", "chip_noise", "chip_noise2"):
    check(f"{k}: pulse1-only sweep 79/80 NOT listed",
          not ({79, 80, 81} & ccs[k]), sorted(ccs[k]))
check("chip_noise: key split 114 listed; noise2 does not get it",
      114 in ccs["chip_noise"] and 114 not in ccs["chip_noise2"])
check("chip_noise2: the per-voice mods are listed",
      {5, 86, 87, 113} <= ccs["chip_noise2"])

# -- Descriptions (2026-10-07): the page and the drawer showed "(no comment in
# source)" / "(no description)" next to raw code. Every row now reads as a name.
_hs = G.scan_handlers(G.SOURCES["synth"])
_rows = [(h["owner"], cc, d) for h in _hs
         if h["owner"] not in ("ChipSynth", "_MonoSynthRetired")
         for b in h["branches"] for cc, d in G.branch_rows(h["owner"], b)]
check("describe: every live CC row has a plain description (no code, no dates)",
      _rows and all(d and "self." not in d and "_cc_to" not in d
                    and not re.search(r"20\d\d-\d\d", d) and "no desc" not in d.lower()
                    for _, _, d in _rows),
      [r for r in _rows if "self." in r[2] or "_cc_to" in r[2]][:3])
check("describe: the same control reads the same on every voice",
      {d for o, cc, d in _rows if cc == 11 and o != "GameboyWaveVoice"} == {"Release time"}
      and {d for o, cc, d in _rows if cc == 74} == {"Filter cutoff"})
check("describe: multi-CC branches list one row per CC",
      {r[1] for r in _rows if r[0] == "GameboyWaveVoice"} >= set(range(102, 110)))
_page = G.build({"constructed": G.constructed_classes(G._engine_sources())},
               _hs, [], [], fire=None)
check("page: no placeholder text, raw code or retired engines",
      "no comment in source" not in _page and "self." not in _page
      and "MonoSynthRetired" not in _page and "ChipSynth" not in _page)
_kinds = G.sampler_kinds(G.SOURCES["synth"])
check("sampler: note vs CC comes from the config key (audition is a note, capture length a CC)",
      _kinds.get("_SMP_AUDITION") == "Note" and _kinds.get("_SMP_CAP_MSB") == "CC"
      and _kinds.get("_SMP_BR") == "Note", _kinds)

# -- Fire default knobs (2026-10-07): a synth-bus target NAMES its module since
# 2026-09-24; the digits-only pattern dropped those (page showed 2 of 22 knobs).
import re
_here = os.path.dirname(os.path.abspath(__file__))
_fsrc = open(os.path.join(_here, "orc1_fire.py"), encoding="utf-8").read()
_i = re.search(r"_DEFAULT_KNOB_TARGETS\s*=\s*\[", _fsrc).start()
_seg = _fsrc[_i:_fsrc.find("\n]", _i)]
_n_src = len(re.findall(r'\(\s*"[^"]+",\s*\("cc_out"', _seg))
_kd = G.fire_inputs(os.path.join(_here, "orc1_fire.py"))["knob_defaults"]
check("fire: every default cc_out knob is listed (module-named ones too)",
      _n_src >= 20 and len(_kd) == _n_src, f"{len(_kd)} of {_n_src}")
check("fire: a module-named knob keeps its module name",
      any(k["name"] == "synvol" and k["ch"] == "poly" for k in _kd),
      [k for k in _kd if k["name"] == "synvol"])

# -- /cc rebuilds itself when stale (2026-10-07: it sat two weeks old while the
# page said "read out of the running code"). AST-extract the web helpers.
import ast, re, tempfile, types
_wsrc = open(os.path.join(_here, "orc1_web.py"), encoding="utf-8").read()
_tree = ast.parse(_wsrc)
_fns = {n.name: ast.get_source_segment(_wsrc, n) for n in _tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("_cc_map_stale", "_cc_map_refresh",
                                                         "cc_map_page")}
check("web: /cc calls the refresh before serving",
      "_cc_map_refresh()" in (_fns.get("cc_map_page") or ""))
_d = tempfile.mkdtemp()
os.makedirs(os.path.join(_d, "static", "js")); os.makedirs(os.path.join(_d, "templates"))
_page = os.path.join(_d, "static", "cc_map.html")
_py = os.path.join(_d, "orc1_x.py"); _tp = os.path.join(_d, "_test_x.py")
for p, t in ((_page, 100), (_py, 50), (_tp, 200)):
    open(p, "w").close(); os.utime(p, (t, t))
import threading as _th
_calls = []
_fake_sp = types.SimpleNamespace(run=lambda cmd, **kw: (_calls.append((cmd, kw.get("cwd"))),
                                                       os.utime(_page, (300, 300))))
_g = {"os": os, "threading": _th, "__file__": os.path.join(_d, "orc1_web.py"),
      "_CC_MAP_LOCK": _th.Lock(), "print": lambda *a, **k: None}
exec(_fns["_cc_map_stale"], _g); exec(_fns["_cc_map_refresh"], _g)
check("web: page newer than the code = fresh (a _test_ file doesn't count)",
      _g["_cc_map_stale"]() is False)
os.utime(_py, (200, 200))
check("web: a code file newer than the page = stale", _g["_cc_map_stale"]() is True)
_saved_sp = sys.modules.get("subprocess"); sys.modules["subprocess"] = _fake_sp
try:
    _g["_cc_map_refresh"]()
    _ok = True
except Exception as e:
    _ok = repr(e)
finally:
    sys.modules["subprocess"] = _saved_sp
check("web: a stale page runs gen_cc_map.py (niced) in the code dir, no NameError",
      _ok is True and len(_calls) == 1 and _calls[0][0][:3] == ["nice", "-n", "15"]
      and _calls[0][0][-1] == "gen_cc_map.py" and _calls[0][1] == _d, (_ok, _calls))
_g["_cc_map_refresh"]()
check("web: a fresh page is served without rebuilding", len(_calls) == 1)
for p in (_page, _py, _tp):
    os.remove(p)
os.rmdir(os.path.join(_d, "static", "js")); os.rmdir(os.path.join(_d, "static"))
os.rmdir(os.path.join(_d, "templates")); os.rmdir(_d)

total = passed + failed
print(f"\n{passed} passed, {failed} failed")
if total != EXPECTED:
    print(f"COUNT DRIFT: ran {total}, EXPECTED = {EXPECTED}")
    sys.exit(1)
sys.exit(1 if failed else 0)
