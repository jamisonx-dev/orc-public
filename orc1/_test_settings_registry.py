#!/usr/bin/env python3
"""Guard: orc1_settings_registry (the scenes settings list) is complete,
reads where the owners really write, and recalls through paths that exist.

    python3 -B _test_settings_registry.py

Pure stdlib for the structural / completeness / recall checks. The READ checks
drive the REAL writers (orc1_synth's flushers, the visuals saver, the brain's
BeatBuddy mix writer, the FX rack's save) into a sandbox HOME and read the
result back through the registry, so a read path that no longer matches its
writer goes red. Those need numpy (orc1_synth); without it they print
[SKIPPED: no orc1_synth] and still count, so the total never shrinks -- run on
the Pi for the real thing.

SAFETY: HOME is repointed to a temp dir AT IMPORT, before any ORC module is
imported (orc1_synth / orc1_fx_rack resolve their ~ paths at import). The real
~/.orc1_* files and ~/orc1_state.json are md5'd before and after and must be
byte-identical.
"""
import ast
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.expanduser("~")


# Every file the registry reads = every file a writer driven by this test could
# touch if the sandbox leaked. (Not all of ~/.orc1_*: the live recorder rewrites
# ~/.orc1_skipback_status.json every few seconds.)
WATCH = (".orc1_bus_levels.json", ".orc1_poly_params.json", ".orc1_chip_params.json",
         ".orc1_mono_params.json", ".orc1_drum_params.json", ".orc1_gb_wave.json",
         ".orc1_visuals_state.json", ".orc1_fx_rack.json", ".orc1_fx_inserts.json",
         ".orc1_bb_song.json", ".orc1_bbkit.json", ".orc1_bb_mix.json", ".orc1_bindings.json",
         "orc1_state.json")


def _real_files():
    out = {}
    for p in [os.path.join(REAL_HOME, n) for n in WATCH]:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[p] = hashlib.md5(f.read()).hexdigest()
    return out


_BEFORE = _real_files()
SANDBOX = tempfile.mkdtemp(prefix="orc1_regtest_")
os.environ["HOME"] = SANDBOX
os.environ["USERPROFILE"] = SANDBOX       # Windows expanduser
assert os.path.expanduser("~") == SANDBOX
sys.path.insert(0, HERE)

import orc1_settings_registry as R            # noqa: E402

EXPECTED = 60
passed = failed = skipped = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def skip(name, why):
    global passed, skipped
    passed += 1
    skipped += 1
    print(f"  ok   {name}  [SKIPPED: {why}]")


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def sb(rel):
    """a path inside the sandbox HOME"""
    return os.path.join(SANDBOX, rel)


def wipe_sandbox():
    for p in glob.glob(os.path.join(SANDBOX, "*")) + glob.glob(os.path.join(SANDBOX, ".*")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.unlink(p)


# --- 0. import hygiene (fresh interpreter) -----------------------------------
code = ("import os, sys, builtins; opened = []\n"
        "_o = builtins.open\n"
        "def spy(*a, **k):\n    opened.append(a[0]); return _o(*a, **k)\n"
        "builtins.open = spy\n"
        f"sys.path.insert(0, {HERE!r})\n"
        "import orc1_settings_registry as R\n"
        "bad = [m for m in ('orc1_config', 'gen_cc_map', 'cc_surfaces', 'orc1_synth') if m in sys.modules]\n"
        "print(len(opened), bad, [v for k, v in vars(R).items() if not k.startswith('__') and isinstance(v, str) and os.path.isabs(v) and 'orc1_' in v])\n")
r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                   env=dict(os.environ, HOME=SANDBOX, USERPROFILE=SANDBOX))
check("import has no side effects (no file opened, no ORC module, no ~ expanded)",
      r.returncode == 0 and r.stdout.strip() == "0 [] []", (r.stdout + r.stderr)[-300:])

# --- 1. structure ------------------------------------------------------------
reg = R.registry()
ids = [e["id"] for e in reg]
by = R.by_id()
check("registry builds, ids unique", len(ids) == len(set(ids)) and len(ids) > 300, len(ids))

SET_KINDS = {"cc", "bus_cc", "ctrl_cc", "ctrl_pc", "action"}
READ_KINDS = {"json", "json_agree", "state", "visuals_scene", "brain"}
TYPES = {"cc", "int", "float", "bool", "enum", "str", "dict"}
bad = [e["id"] for e in reg
       if e["type"] not in TYPES or e["set"]["kind"] not in SET_KINDS
       or (e["read"] is not None and e["read"]["kind"] not in READ_KINDS)
       or not e["thing"] or not e["label"]
       or (not e["sceneable"] and not e.get("why"))]
check("every entry well-formed (types, kinds, thing/label, a why when not sceneable)", not bad, bad[:8])

bad = [e["id"] for e in reg if "channel" in e["set"]
       or (e["set"]["kind"] == "cc" and not e["set"].get("module"))]
check("no literal channel anywhere: synth CCs name a MODULE", not bad, bad[:8])

NEVER = ("MODULE_BIND", "MODULE_UNBIND", "MODULE_JOIN", "SET_CHORD_OUT", "SET_MELODY_OUT",
         "SET_VOICE_THRU", "SET_VOICE_MAP", "SET_FX_IO", "SAMPLER_SET_SOURCE", "SKIPBACK_SAVE_ALL",
         "BB_OUTPUT", "RESTART_ORC")
bad = [e["id"] for e in reg if e["sceneable"] and (
       e["set"].get("action") in NEVER or re.match(r"(patch|matrix|skipback|net|module_out|protect)", e["id"]))]
check("no wiring / skipback / system setting is sceneable", not bad, bad)

if R.HERE not in sys.path:
    sys.path.insert(0, R.HERE)
import cc_surfaces as X                         # noqa: E402
import orc1_config as C                         # noqa: E402
acts = X.cmd_actions()
used = sorted({e["set"]["action"] for e in reg if e["set"]["kind"] == "action"})
check("every recall action is a real command-socket action in orc1.py",
      all(a in acts for a in used), [a for a in used if a not in acts])
# and each is a branch of _execute_cmd (not only of handle_ctrl_cc, which shares the name)
body = ast.get_source_segment(src("orc1.py"), next(
    n for n in ast.parse(src("orc1.py")).body if isinstance(n, ast.FunctionDef) and n.name == "_execute_cmd"))
check("every recall action is dispatched by _execute_cmd (the socket handler)",
      all(f'action == "{a}"' in body for a in used), [a for a in used if f'action == "{a}"' not in body])

maps = dict(C.CTRL_CC_MAP)
maps.update(C.ARP_CC_MAP)
bad = []
for e in reg:
    s = e["set"]
    if s["kind"] != "ctrl_cc":
        continue
    if s["enc"]["type"] == "map":
        for k, (cc, _v) in s["enc"]["map"].items():
            if not str(maps.get(cc, "")).endswith(k) and maps.get(cc) != "SPREAD":
                bad.append((e["id"], k, cc))
    elif maps.get(s["cc"]) != s["action"]:
        bad.append((e["id"], s["cc"], maps.get(s["cc"])))
check("every ctrl_cc lands on the named brain action in cfg's CH_CTRL maps", not bad, bad)

# the brain's handle_ctrl_cc / handle_arp_cc really branch on each of them
ctrl_src = src("orc1.py")
bad = [e["set"]["action"] for e in reg if e["set"]["kind"] == "ctrl_cc" and "action" in e["set"]
       and f'action == "{e["set"]["action"]}"' not in ctrl_src]
check("every ctrl_cc action is handled in orc1.py (handle_ctrl_cc / handle_arp_cc)", not bad, bad)

pe = by["arp.pattern"]["set"]
import orc1_patterns as P                       # noqa: E402
check("arp pattern PC = cfg.ARP_PC_OFFSET + orc1_patterns.PATTERN_ORDER",
      pe["base"] == C.ARP_PC_OFFSET and pe["enc"]["choices"] == list(P.PATTERN_ORDER))

# --- 2. completeness: every CC a host handles is in the registry -------------
import gen_cc_map as G                          # noqa: E402
handlers = {h["owner"]: h for h in G.scan_handlers(os.path.join(HERE, "orc1_synth.py"))
            if h["func"] == "set_cc"}
made = G.constructed_classes(G._engine_sources())
check("the voice engines the hosts construct are the ones mapped",
      {"PolySynth", "MonoSynth", "NESEngine", "GameboyWaveVoice"} <= made | {"GameboyWaveVoice"}
      and "ChipSynth" not in made, sorted(made & {"PolySynth", "MonoSynth", "NESEngine", "ChipSynth"}))


def covered(fam, cc, mods):
    if (fam, cc) in R.ALIASES:
        return True
    return any(f"{m}.cc{cc}" in by for m in mods) or f"{fam}.cc{cc}" in by


poly_cc = sorted({c for b in handlers["PolySynth"]["branches"] for c in b["ccs"]})
miss = [c for c in poly_cc if not covered("poly", c, ["poly"])]
check("poly: every PolySynth.set_cc CC is a setting", not miss, miss)

mono_cc = {c for b in handlers["MonoSynth"]["branches"] for c in b["ccs"]}
ms = ast.get_source_segment(src("orc1_synth.py"), next(
    n for n in ast.walk(ast.parse(src("orc1_synth.py")))
    if isinstance(n, ast.ClassDef) and n.name == "MonoSynth"))
m = re.search(r"for li, base in (\(\([^\n]*\)\)):", ms)
span = int(re.search(r"base <= cc <= base \+ (\d+)", ms).group(1))
for _li, base in ast.literal_eval(m.group(1)):
    mono_cc |= set(range(base, base + span + 1))
miss = [c for c in sorted(mono_cc) if not covered("mono", c, ["mono"])]
check("mono: every MonoSynth CC (incl. the layer loop) is a setting", not miss and len(mono_cc) >= 25,
      (miss, len(mono_cc)))

voice_of = dict(C.MODULE_VOICE)
# A branch "reaches" a voice when gen_cc_map's guard logic lets it through AND,
# if the branch's own source block names voices (`voice == 'x'`, or
# `voice in self._attr` with a literal dict), that voice is one of them.
# Re-derived here from raw source lines (indentation blocks), not via the
# registry's own AST classifier.
syn_lines = src("orc1_synth.py").splitlines()
dict_keys = {a: set(re.findall(r"'(\w+)'\s*:", body)) for a, body in
             re.findall(r"self\.(\w+)\s*=\s*\{([^}]*)\}", src("orc1_synth.py"))}


def block(line):
    i = line - 1
    ind = len(syn_lines[i]) - len(syn_lines[i].lstrip())
    out = [syn_lines[i]]
    for t in syn_lines[i + 1:]:
        if t.strip() and len(t) - len(t.lstrip()) <= ind:
            break
        out.append(t)
    return chr(10).join(out)


def reaches(b, v):
    if not G._branch_applies(b["extra"], v):
        return False
    txt = block(b["line"])
    named = set(re.findall(r"voice\s*==\s*'(\w+)'", txt))
    for a in re.findall(r"voice\s+in\s+self\.(\w+)", txt):
        named |= dict_keys.get(a, set())
    return not named or v in named


chip_miss, chip_extra = [], []
for mod, v in voice_of.items():
    for b in handlers["NESEngine"]["branches"]:
        if not reaches(b, v):
            continue
        for c in b["ccs"]:
            if not covered("chip", c, [mod]):
                chip_miss.append((mod, c))
for e in reg:
    mm = re.match(r"(chip_\w+|gb_wave)\.cc(\d+)$", e["id"])
    if not mm or mm.group(1) == "gb_wave":
        continue
    v, c = voice_of[mm.group(1)], int(mm.group(2))
    if not any(c in b["ccs"] and reaches(b, v) for b in handlers["NESEngine"]["branches"]):
        chip_extra.append(e["id"])
check("chip: every NESEngine CC each chip voice reaches is a setting", not chip_miss, chip_miss[:10])
check("chip: no per-voice setting for a CC that voice never reaches", not chip_extra, chip_extra)

gb_cc = sorted({c for b in handlers["GameboyWaveVoice"]["branches"] for c in b["ccs"]})
miss = [c for c in gb_cc if f"gb_wave.cc{c}" not in by]
check("gb wave: every GameboyWaveVoice CC is a setting", not miss, miss)

# engine-wide vs per-voice, spot-checked against the source text of the branch
nes = ast.get_source_segment(src("orc1_synth.py"), next(
    n for n in ast.walk(ast.parse(src("orc1_synth.py")))
    if isinstance(n, ast.ClassDef) and n.name == "NESEngine"))
blk8 = re.search(r"elif cc == 8:\n(.*?)\n\s+elif", nes, re.S).group(1)
blk1 = re.search(r"if cc == 1:\n(.*?)\n\s+elif cc == 3", nes, re.S).group(1)
check("chip ADSR is one ENGINE-WIDE setting; pulse duty is PER VOICE (matches the code)",
      "voice" not in blk8 and "chip.cc8" in by and "chip_p1.cc8" not in by
      and "voice" in blk1 and "chip_p1.cc1" in by and "chip_tri.cc1" not in by)

skipset = R._chip_persist_skip(src("orc1_synth.py"))
bad = [e["id"] for e in reg
       if re.match(r"(chip|chip_\w+|gb_wave)\.cc(\d+)$", e["id"])
       and int(e["id"].split("cc")[-1]) in skipset
       and e["read"] and e["read"].get("file") == R.CHIP_PARAMS]
check("no setting reads a chip CC the chip host never writes to the patch file (_CHIP_PERSIST_SKIP)",
      not bad and {72, 73, 76, 13} <= skipset, bad)

drum = C.DRUM_CC["CC_TO_VP"]
bad = [cc for cc, (v, p) in drum.items()
       if by.get(f"drums.{v}.{p}", {}).get("set", {}).get("cc") != cc
       or by[f"drums.{v}.{p}"]["set"]["module"] != "drums"]
check("drums: every cfg.DRUM_CC CC is the setting of its own voice/param", not bad and len(drum) > 60,
      bad[:8])

lv = X.bus_levels(C)
bad = [(k, cc) for cc, _l, k in lv
       if not any(e["set"]["kind"] == "bus_cc" and e["set"]["cc"] == cc for e in reg)]
check("levels: every bus-level CC the hosts define is a setting", not bad and len(lv) >= 8, bad)

vsrc = src(os.path.join("daw", "orc_visuals.py"))
knobs = X._dict_with_comments(vsrc, "KNOBS")
vcc = {e["set"]["cc"] for e in reg if e["set"]["kind"] == "cc" and e["set"]["module"] == "visuals"}
miss = sorted(set(knobs) - vcc)
check("visuals: every KNOBS CC and the SCENE CC are settings",
      not miss and X._int_const(vsrc, "SCENE_CC") in vcc and len(knobs) > 30, miss)

kinds = {e["read"]["kind"] for e in reg if e["read"]}
check("bus levels BB KIT is readable (bbkit host writes 'bbkit'), master is not sceneable",
      by["level.bbkit"]["read"]["path"] == ["bbkit"] and not by["level.master"]["sceneable"]
      and '_bus_level_write({"bbkit"' in src("orc1_bbkit_host.py")
      and "_VOL_MASTER_CC" not in "".join(src(f) for f in ("orc1_poly_host.py", "orc1_chip_host.py",
                                                            "orc1_drum_host.py", "orc1_mono_host.py",
                                                            "orc1_bbkit_host.py", "orc1_sampler_host.py")))

# state paths are keys write_state_json really writes
wsj = ast.get_source_segment(src("orc1.py"), next(
    n for n in ast.parse(src("orc1.py")).body if isinstance(n, ast.FunctionDef) and n.name == "write_state_json"))
bad = []
for e in reg:
    rd = e["read"]
    if rd and rd["kind"] == "state":
        for k in rd["path"]:
            if f'"{k}":' not in wsj:
                bad.append((e["id"], k))
check("every ~/orc1_state.json path is a key write_state_json writes", not bad, bad)

# --- 3. reads, driven through the REAL writers --------------------------------
try:
    import orc1_synth as S
    _syn_err = None
except Exception as ex:                          # off-Pi: no numpy
    S, _syn_err = None, f"{type(ex).__name__}: {ex}"


def rv(*ids):
    return R.read_values(list(ids))


if S is None:
    for n in ("orc1_synth paths point into the sandbox", "poly read via S._flush_poly_params","mono read via S._flush_mono_params",
              "chip per-voice read via S._flush_chip_params",
              "chip engine-wide: one _engine value, the last set wins",
              "chip engine-wide: unmigrated file -> agree -> value, disagree -> None",
              "drum read via S._flush_drum_params", "bus levels via S._bus_level_write",
              "gb wave harmonic + custom via GameboyWaveVoice.export_state"):
        skip(n, "no orc1_synth (" + _syn_err[:60] + ")")
else:
    check("orc1_synth paths point into the sandbox", S.POLY_PARAMS_FILE.startswith(SANDBOX))
    wipe_sandbox()
    S._flush_poly_params(types.SimpleNamespace(_poly_param_pending={74: 33, 8: 5}, _poly_params_dirty=True))
    check("poly read via S._flush_poly_params", rv("poly.cc74", "poly.cc8", "poly.cc71")
          == {"poly.cc74": 33, "poly.cc8": 5, "poly.cc71": None})
    S._flush_mono_params(types.SimpleNamespace(_mono_param_pending={22: 77, 1: 9}, _mono_params_dirty=True))
    check("mono read via S._flush_mono_params", rv("mono.cc22", "mono.cc1") == {"mono.cc22": 77, "mono.cc1": 9})
    S._flush_chip_params(types.SimpleNamespace(
        _chip_param_pending={"pulse1": {1: 40, 77: 12}, "wave": {7: 99}, "noise2": {3: 100}},
        _chip_params_dirty=True))
    check("chip per-voice read via S._flush_chip_params",
          rv("chip_p1.cc1", "chip_p1.cc77", "gb_wave.cc7", "chip_noise2.cc3", "chip_p2.cc1")
          == {"chip_p1.cc1": 40, "chip_p1.cc77": 12, "gb_wave.cc7": 99, "chip_noise2.cc3": 100,
              "chip_p2.cc1": None})
    S._flush_chip_params(types.SimpleNamespace(_chip_param_pending={"pulse1": {8: 10}}, _chip_params_dirty=True))
    a = rv("chip.cc8")["chip.cc8"]
    S._flush_chip_params(types.SimpleNamespace(_chip_param_pending={"noise": {8: 20}}, _chip_params_dirty=True))
    b = R.read_values(["chip.cc8"], detail=True)["chip.cc8"]
    check("chip engine-wide: one _engine value, the last set wins", a == 10 and b["value"] == 20,
          (a, b))
    # an old-format file (engine-wide CC per voice, never rewritten): the agree rule
    with open(os.path.expanduser(R.CHIP_PARAMS), "w") as f:
        json.dump({"pulse1": {"8": 10}, "noise": {"8": 20}, "pulse2": {"74": 5},
                   "triangle": {"74": 5}}, f)
    c = R.read_values(["chip.cc8", "chip.cc74"], detail=True)
    check("chip engine-wide: unmigrated file -> agree -> value, disagree -> None",
          c["chip.cc74"]["value"] == 5 and c["chip.cc8"]["value"] is None
          and "ambiguous" in c["chip.cc8"]["note"], c)
    S._flush_drum_params(types.SimpleNamespace(_drum_param_pending={"snare": {"decay": 55}},
                                               _drum_params_dirty=True))
    check("drum read via S._flush_drum_params",
          rv("drums.snare.decay", "drums.snare.tune") == {"drums.snare.decay": 55, "drums.snare.tune": None})
    S._bus_level_write({"poly": 90, "bbkit": 70, "drum": 44, "mono": 3})
    check("bus levels via S._bus_level_write",
          rv("level.poly", "level.bbkit", "level.drum", "level.mono", "level.chip")
          == {"level.poly": 90, "level.bbkit": 70, "level.drum": 44, "level.mono": 3, "level.chip": None})
    w = S.GameboyWaveVoice()
    h0 = G.module_consts(ast.parse(src("orc1_synth.py")))["_GB_HARM_CC0"]
    w.set_cc(h0 + 3, 99)
    w.set_cc(23, 127)
    with open(os.path.expanduser(S.GB_WAVE_STATE_FILE), "w") as f:
        json.dump(w.export_state(), f)
    check("gb wave harmonic + custom via GameboyWaveVoice.export_state",
          rv(f"gb_wave.cc{h0 + 3}", f"gb_wave.cc{h0}", "gb_wave.cc23")
          == {f"gb_wave.cc{h0 + 3}": 99, f"gb_wave.cc{h0}": 0, "gb_wave.cc23": 127})

# visuals: the renderer's own save_state(), exec'd from its source
wipe_sandbox()
vtree = ast.parse(vsrc)
fn = next(n for n in vtree.body if isinstance(n, ast.FunctionDef) and n.name == "save_state")
ns = {"json": json, "os": os, "STATE": sb(".orc1_visuals_state.json"),
      "KNOBS": {cc: v for cc, (v, _c) in knobs.items()}, "sets_count": lambda: 7, "BG_MODES": [0] * 10}
exec(compile(ast.Module([fn], []), "orc_visuals.save_state", "exec"), ns)
first = sorted(knobs)[0]
ns["save_state"]({first: 17}, "tube", False)
dflt = R.visuals_knob_defaults()
second = sorted(knobs)[1]
got = R.read_values([f"visuals.{knobs[first][0]}", f"visuals.{knobs[second][0]}", "visuals.scene"])
check("visuals read via the renderer's save_state (knob, unsaved knob -> KNOB_DEFAULT, scene)",
      got == {f"visuals.{knobs[first][0]}": 17,
              f"visuals.{knobs[second][0]}": int(dflt.get(second, 64)), "visuals.scene": "tube"}, got)
ns["save_state"]({first: 17}, "tube", True)
check("visuals scene reads 'cycle' while cycling", rv("visuals.scene") == {"visuals.scene": "cycle"})

# BeatBuddy mix: the brain's own _bb_mix_set, exec'd from orc1.py
btree = ast.parse(src("orc1.py"))
fns = [n for n in btree.body if isinstance(n, ast.FunctionDef) and n.name in ("_bb_mix_file", "_bb_mix_set")]
bns = {"json": json, "os": os, "_BB_MIX": {}}
exec(compile(ast.Module(fns, []), "orc1._bb_mix_set", "exec"), bns)
bns["_bb_mix_set"]("snare", 0.5)
bns["_bb_mix_set"]("kick", 1.0)
check("bb mix via orc1._bb_mix_set (absent group = 1.0)",
      rv("bb.mix.snare", "bb.mix.kick") == {"bb.mix.snare": 0.5, "bb.mix.kick": 1.0})

# song / kit: the writers' json.dump lines, then a read of that shape
ok_src = ('json.dump({"song": os.path.abspath(cand)}, f)' in src("orc1.py")
          and 'json.dump({"kit": os.path.basename(path)}, f)' in src("orc1_bbkit_host.py"))
with open(sb(".orc1_bb_song.json"), "w") as f:
    json.dump({"song": "/x/ABC.BBS"}, f)
with open(sb(".orc1_bbkit.json"), "w") as f:
    json.dump({"kit": "Voice"}, f)
check("bb song / kit read the keys their writers dump",
      ok_src and rv("bb.song", "bb.kit") == {"bb.song": "/x/ABC.BBS", "bb.kit": "Voice"})

# FX rack: RackManager.save (the brain's writer)
try:
    import orc1_fx_rack as FR
    rack_ok = FR.RACK_STATE_FILE.startswith(SANDBOX)
    FR.RackManager.save(types.SimpleNamespace(), {"slots": [
        {"plugin": None, "enabled": False, "params": {}},
        {"plugin": "matrixverb", "enabled": True, "params": {"drywet": 0.3}},
        {"plugin": None, "enabled": False, "params": {}}]})
    got = rv("fx.rack.1.enabled", "fx.rack.1.params", "fx.rack.0.params", "fx.rack.2.enabled")
    check("fx rack via RackManager.save (enabled, {plugin, params}, empty slot)",
          rack_ok and got == {"fx.rack.1.enabled": True,
                              "fx.rack.1.params": {"plugin": "matrixverb", "params": {"drywet": 0.3}},
                              "fx.rack.0.params": None, "fx.rack.2.enabled": False}, got)
except Exception as ex:
    check("fx rack via RackManager.save (enabled, {plugin, params}, empty slot)", False, ex)

# brain state
with open(sb("orc1_state.json"), "w") as f:
    json.dump({"arp": {"bpm": 123.4, "rate": "1/16", "enabled": True}, "root": 5, "scale": "DOR",
               "voicing": "OPEN", "arp_hold": True, "beatbuddy": {"tempo_mode": "clock"}}, f)
check("brain state reads (tempo, root, scale, voicing, arp, BB tempo mode)",
      rv("tempo.bpm", "harmony.root", "harmony.scale", "harmony.voicing", "arp.rate",
         "arp.enabled", "arp.hold", "bb.tempo_mode", "harmony.preset")
      == {"tempo.bpm": 123.4, "harmony.root": 5, "harmony.scale": "DOR", "harmony.voicing": "OPEN",
          "arp.rate": "1/16", "arp.enabled": True, "arp.hold": True, "bb.tempo_mode": "clock",
          "harmony.preset": None})

d = R.read_values(["sampler.A.gain", "fx.mix"], detail=True)
check("no-owner settings read None with a reason",
      d["sampler.A.gain"]["value"] is None and d["fx.mix"]["value"] is None
      and "no capture" in by["sampler.A.gain"]["note"])
try:
    R.read_values(["no.such.id"])
    check("unknown id raises KeyError", False)
except KeyError:
    check("unknown id raises KeyError", True)

# --- 4. recall ---------------------------------------------------------------
FILE_B = {"6": "poly", "10": "drums", "9": "chip_p2", "16": "visuals", "3": "mono", "2": "bbkit"}
STATE_B = {"poly": 6, "drums": 10, "chip_p2": 9, "visuals": 16, "mono": 3}
ctrl = int((getattr(C, "SYNTH_VOL", {}) or {}).get("CH", C.CH_CTRL))

msgs, sk = R.build_recall({"poly.cc74": 40}, FILE_B)
check("poly CC -> the channel /matrix binds poly to (file-shaped bindings)",
      msgs == [{"type": "cc", "channel": 6, "cc": 74, "value": 40, "id": "poly.cc74"}] and not sk, msgs)
msgs, _ = R.build_recall({"poly.cc74": 40}, STATE_B)
check("same with the brain's module->channel bindings", msgs[0]["channel"] == 6)
msgs, sk = R.build_recall({"poly.cc74": 40}, {"7": "sampler"})
check("unbound module is skipped, not sent", not msgs and "not bound" in sk[0][1], sk)
msgs, _ = R.build_recall({"chip.cc8": 11}, FILE_B)
check("chip engine-wide CC goes to the first BOUND chip voice (chip_p2 here)",
      msgs == [{"type": "cc", "channel": 9, "cc": 8, "value": 11, "id": "chip.cc8"}], msgs)

dcc = {cc: vp for cc, vp in C.DRUM_CC["CC_TO_VP"].items()}
inv = {f"drums.{v}.{p}": cc for cc, (v, p) in dcc.items()}
vals = {k: 50 for k in list(inv)[:5] + list(inv)[-3:]}
msgs, _ = R.build_recall(vals, FILE_B)
check("drum settings recall as cfg.DRUM_CC's own CC on the drums channel",
      sorted((m["channel"], m["cc"]) for m in msgs) == sorted((10, inv[k]) for k in vals), msgs[:3])
lvcc = {k: cc for cc, _l, k in lv}
msgs, _ = R.build_recall({"level.drum": 77, "level.bbkit": 60}, FILE_B)
check("bus levels recall on CH_CTRL with the hosts' own CCs",
      sorted((m["channel"], m["cc"], m["value"]) for m in msgs)
      == sorted([(ctrl, lvcc["DRUM_CC"], 77), (ctrl, lvcc["BBKIT_VOL_CC"], 60)]), msgs)
msgs, sk = R.build_recall({"level.master": 100}, FILE_B)
check("a non-sceneable setting is skipped with its reason", not msgs and "not sceneable" in sk[0][1])

msgs, _ = R.build_recall({"tempo.bpm": 98.5}, FILE_B)
m2, sk2 = R.build_recall({"tempo.bpm": 98.5}, FILE_B, {"clock_mode": "external"})
check("tempo -> SET_BPM; skipped while following an outside clock",
      msgs[0]["cmd"] == {"action": "SET_BPM", "value": 98.5} and not m2 and sk2)

# arp rate / gate / pattern / visuals scene round-trip through the CODE's own decode
sub = by["arp.rate"]["set"]["enc"]["choices"]
ok = all(sub[int((R.encode(by["arp.rate"]["set"]["enc"], s) / 127) * (len(sub) - 1))] == s for s in sub)
check("arp rate encodes to a CC handle_arp_cc decodes back to the same rate", ok and len(sub) == 11)
ok = all(abs((0.1 + R.encode(by["arp.gate"]["set"]["enc"], g) / 127.0 * 1.9) - g) <= 1.9 / 127
         for g in (0.1, 0.5, 1.0, 1.37, 2.0))
check("arp gate encodes within one CC step of handle_arp_cc's 0.1 + v/127*1.9", ok)
pats = list(P.PATTERN_ORDER)
msgs, _ = R.build_recall({"arp.pattern": pats[3]}, FILE_B)
check("arp pattern -> ctrl PC that handle_ctrl_pc maps back to that pattern",
      msgs[0]["type"] == "ctrl_pc" and pats[msgs[0]["program"] - C.ARP_PC_OFFSET] == pats[3])
names = R.visuals_scenes()
enc = by["visuals.scene"]["set"]["enc"]
ok = all(names[min(len(names) - 1, (R.encode(enc, s) - 1) * len(names) // 127)] == s for s in names)
check("visuals scene encodes to a CC the renderer maps back to that scene; cycle = 0",
      ok and R.encode(enc, "cycle") == 0 and len(names) > 5)
msgs, _ = R.build_recall({"harmony.voicing": "DROP2", "harmony.key_lock": True}, FILE_B)
check("voicing / key lock -> the brain's CH_CTRL latch CCs",
      sorted((maps[m["cc"]], m["value"]) for m in msgs) == [("KEY_LOCK", 127), ("VOICING_DROP2", 127)], msgs)
msgs, _ = R.build_recall({"fx.rack.1.params": {"plugin": "matrixverb", "params": {"a": 0.1, "b": 0.9}}}, FILE_B)
check("fx rack knobs -> one RACK_PARAM per symbol, guarded by the slot's plugin",
      [m["cmd"] for m in msgs] == [{"action": "RACK_PARAM", "idx": 1, "symbol": "a", "value": 0.1},
                                   {"action": "RACK_PARAM", "idx": 1, "symbol": "b", "value": 0.9}]
      and all(m["require"] == {"rack_plugin": [1, "matrixverb"]} for m in msgs), msgs)
msgs, _ = R.build_recall({"bb.kit": "Rock"}, FILE_B, {"bb_kits": ["Jazz", "Rock", "Voice"]})
check("bb kit -> BB_KIT_SELECT with the kit's index in the host's sorted kit list",
      msgs[0]["cmd"] == {"action": "BB_KIT_SELECT", "program": 1})
msgs, _ = R.build_recall({"tempo.bpm": 100.0, "bb.song": "/x/A.BBS", "poly.cc74": 3}, FILE_B)
check("recall order: BeatBuddy song (sets tempo) before tempo before voices",
      [m["id"] for m in msgs] == ["bb.song", "tempo.bpm", "poly.cc74"], [m["id"] for m in msgs])
msgs, _ = R.build_recall({"fire.pattern": 5}, FILE_B)
check("fire pattern -> FIRE_PATTERN action (host 0D, sent by the scenes engine); read live from the brain",
      msgs == [{"type": "action", "cmd": {"action": "FIRE_PATTERN", "pattern": 5}, "id": "fire.pattern"}]
      and by["fire.pattern"]["read"]["kind"] == "brain"
      and R.read_one(by["fire.pattern"])[0] is None, msgs)
kits = R.bbkit_kits(os.path.join(HERE, "kits"))
host_kits = sorted(glob.glob(os.path.join(HERE, "kits", "*")))
check("bbkit_kits() lists kit folders the way the BBKit host indexes them",
      kits == [os.path.basename(p) for p in host_kits if os.path.isfile(os.path.join(p, "kit.json"))])

# --- 5. counts per thing, and the real HOME untouched ------------------------
summ = R.summary()
print("\n  items by thing: " + ", ".join(f"{k} {v}" for k, v in sorted(summ.items())))
check("summary covers every thing the plan lists",
      all(any(t.startswith(k) for t in summ) for k in
          ("Tempo", "Harmony", "Arp", "Levels", "Poly", "Mono", "Chip", "GB wave", "Drums",
           "Visuals", "FX", "BeatBuddy", "Sampler")))
os.environ["HOME"] = os.environ["USERPROFILE"] = REAL_HOME
shutil.rmtree(SANDBOX, ignore_errors=True)
after = _real_files()
check("real ~/.orc1_* and ~/orc1_state.json byte-identical", after == _BEFORE,
      sorted(k for k in set(after) | set(_BEFORE) if after.get(k) != _BEFORE.get(k)))

print(f"\n{passed} passed, {failed} failed" + (f"  ({skipped} SKIPPED)" if skipped else ""))
if passed + failed != EXPECTED:
    print(f"EXPECTED {EXPECTED} checks, ran {passed + failed}: a check was added or removed")
    failed += 1
sys.exit(1 if failed else 0)
