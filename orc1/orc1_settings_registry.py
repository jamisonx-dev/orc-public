#!/usr/bin/env python3
"""ORC-1 settings registry -- every setting a SCENE can capture and recall.

Step 1 of scenes (plan: _planning/ORC1_Scenes_and_Web_Reorg_Plan.md, Part 1).
One entry per setting:

    id         stable key, e.g. "poly.cc74", "drums.bass_drum.decay", "tempo.bpm"
    thing      grouping label for the UI, e.g. "Poly > Filter"
    label      plain-words name
    type       "cc" (int 0-127) | "int" | "float" | "bool" | "enum" | "str" | "dict"
    range      (lo, hi) for numbers, or None
    choices    list for enums, or None (some lists live in the running brain)
    default    the value the code boots with when nothing was saved, or None
    set        HOW recall applies it live (see SET KINDS below)
    read       WHERE capture finds the value in force (see READ KINDS), or None
    sceneable  False = never offered for a scene (with a "why")
    note       caveats that matter when capturing / recalling

SET KINDS (all live: nothing here restarts a host)
    {"kind": "cc", "module": "poly", "cc": 74}
        a CC on the internal synth bus, on the channel /matrix binds that MODULE
        to (resolved at recall time from the bindings; unbound = skipped).
        "module" may be a list = the first bound one (chip engine-wide CCs).
    {"kind": "bus_cc", "cc": 8}
        a CC on CH_CTRL (13) of the internal synth bus: the bus levels the
        module hosts listen to. The only literal channel in the registry.
    {"kind": "ctrl_cc", "cc": 31, "enc": {...}}
        the BRAIN's own control-channel handler (orc1.handle_ctrl_cc), i.e.
        what a controller's CH_CTRL knob does. The scenes engine (in the brain)
        calls it directly. "enc" turns the natural value into 0-127.
    {"kind": "ctrl_pc", "base": 60, "enc": {...}}
        the brain's control-channel program change (orc1.handle_ctrl_pc).
    {"kind": "action", "action": "SET_BPM", "payload": {"value": "$v"}}
        a command-socket action; "$v" is replaced by the value.

READ KINDS (paths are resolved at CALL time, never at import)
    {"kind": "json", "file": "~/.orc1_poly_params.json", "path": ["74"]}
    {"kind": "json_agree", "file": ..., "primary": [...], "paths": [[...], ...]}
        one engine-wide value: "primary" when the file has it (chip "_engine"),
        else the old per-key copies -- the value if they all agree, else None
        (ambiguous -- see "note").
    {"kind": "state", "path": ["arp", "bpm"]}
        ~/orc1_state.json, written by the brain's print_status() at the END of
        every command-socket action (orc1.py _execute_cmd), so it is fresh after
        a web/scene command. A CH_CTRL CC that does not call print_status (arp
        knobs, VL strength, SPREAD) shows only after the next print_status.
    {"kind": "visuals_scene"}  special: scene name, or "cycle"
    {"kind": "brain", "key": "fire.pattern"}
        held only in the running brain (no file): read_one gives None; the
        scenes engine asks the brain live (SETTINGS_READ). The scenes engine
        also reads every "state" entry live that way, so capture never sees
        the ~/orc1_state.json lag.
    "decode" (optional) converts what the file holds into the entry's value.
    "missing" (optional) = the code's own default when the key was never saved.

Everything per-CC is GENERATED from the code (gen_cc_map's AST scan of the
set_cc handlers, cfg.DRUM_CC, cc_surfaces.bus_levels, the visuals KNOBS table,
cfg's CH_CTRL maps) so it cannot drift; _test_settings_registry.py fails when
a host handles a CC the registry does not list.

Pure stdlib. Nothing runs at import: no file read, no `~` expanded, no ORC
module imported. registry() builds (and caches) on first call.
"""
import ast
import glob
import json
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

STATE_JSON = "~/orc1_state.json"
BUS_LEVELS = "~/.orc1_bus_levels.json"
POLY_PARAMS = "~/.orc1_poly_params.json"
CHIP_PARAMS = "~/.orc1_chip_params.json"
MONO_PARAMS = "~/.orc1_mono_params.json"
DRUM_PARAMS = "~/.orc1_drum_params.json"
GB_WAVE = "~/.orc1_gb_wave.json"
VISUALS_STATE = "~/.orc1_visuals_state.json"
FX_RACK = "~/.orc1_fx_rack.json"
FX_INSERTS = "~/.orc1_fx_inserts.json"
BB_SONG = "~/.orc1_bb_song.json"
BB_KIT = "~/.orc1_bbkit.json"
BB_MIX = "~/.orc1_bb_mix.json"

# Chip modules that are NOT the GB wave (the NESEngine engine-wide CCs reach
# these; after `if voice == 'wave': ... return` the wave never sees them).
CHIP_NONWAVE = ("chip_p1", "chip_p2", "chip_tri", "chip_noise", "chip_noise2", "chip_pcm")

# CCs a host HANDLES that the registry deliberately does not list as their own
# setting -- each with the reason. The completeness test accepts exactly these.
# {(family, cc): reason}; family = poly | mono | chip | gb_wave
ALIASES = {
    ("poly", 13): "voice level = the poly BUS level (level.poly, ch13 CC8): same engine.poly.master",
    ("mono", 13): "voice level = the mono BUS level (level.mono): same mono.master",
    ("chip", 13): "chip master = the chip BUS level (level.chip): NESEngine.master is engine-wide",
}


def _src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def _cfg():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import orc1_config as C
    return C


def _gen():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import gen_cc_map as G
    return G


def _module_assign(src, name):
    """ast node of a module-level `name = ...` in src."""
    for st in ast.parse(src).body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                              for t in st.targets):
            return st.value
    return None


def _literal(src, name):
    node = _module_assign(src, name)
    return ast.literal_eval(node) if node is not None else None


# ---------------------------------------------------------------------------
# Thing labels
# ---------------------------------------------------------------------------

_GROUP_WORDS = [
    (r"attack|decay|sustain|release|envelope", "Envelope"),
    (r"anti-alias|wave|duty|noise mode|harmonic|volume mode|retrigger on every", "Oscillator"),
    (r"filter|cutoff|resonance", "Filter"),
    (r"ring mod", "Ring mod"),
    (r"layer", "Layers"),
    (r"glide|portamento|octave|vibrato|tremolo|arpeggio|slide|retrigger|pan|split|sweep|detune|coupling|spread",
     "Performance"),
    (r"level|volume", "Level"),
]


def _group(label):
    low = label.lower()
    for pat, g in _GROUP_WORDS:
        if re.search(pat, low):
            return g
    return "Other"


def _e(id_, thing, label, typ, set_, read, rng=(0, 127), default=None, choices=None,
       sceneable=True, note="", why=None, order=50):
    return {"id": id_, "thing": thing, "label": label, "type": typ,
            "range": rng if typ in ("cc", "int", "float") else None,
            "choices": choices, "default": default, "set": set_, "read": read,
            "sceneable": bool(sceneable), "why": why, "note": note, "order": order}


# ---------------------------------------------------------------------------
# Synth voices: generated from the set_cc handlers (gen_cc_map's AST scan)
# ---------------------------------------------------------------------------

def _branches(owner):
    G = _gen()
    for h in G.scan_handlers(os.path.join(HERE, "orc1_synth.py")):
        if h["owner"] == owner and h["func"] == "set_cc":
            return h["branches"]
    return []


def _class_node(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    return None


def nes_branch_info():
    """{line: {"pervoice": bool, "voices": set|None}} for every CC branch of
    NESEngine.set_cc. A branch is PER-VOICE when its own test, its body, or an
    enclosing `if` mentions `voice` (or the per-voice mod `m`); otherwise its
    state is ENGINE-WIDE (one value shared by every chip voice it reaches).
    "voices" = the literal voice names the branch acts for, when it names them
    (`voice == 'pulse1'` in its test/body, or `voice in self._vib_depth` resolved
    through that dict's literal keys), else None (= every voice it reaches)."""
    src = _src("orc1_synth.py")
    tree = ast.parse(src)
    cls = _class_node(tree, "NESEngine")
    dict_keys = {}
    for n in ast.walk(cls):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == "self"
                and isinstance(n.value, ast.Dict)
                and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.value.keys)):
            dict_keys[n.targets[0].attr] = {k.value for k in n.value.keys}
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "set_cc")
    out = {}
    pv_re = re.compile(r"\bvoice\b|\bm\.\w|\bm is not None\b")

    def voices_in(text):
        names = set(re.findall(r"voice\s*==\s*'(\w+)'", text))
        for attr in re.findall(r"voice\s+in\s+self\.(\w+)", text):
            names |= dict_keys.get(attr, set())
        return names

    def walk(stmts, anc):
        for st in stmts:
            if isinstance(st, ast.If):
                t = ast.unparse(st.test)
                body = "\n".join(ast.unparse(b) for b in st.body)
                ccs = re.search(r"\bcc\b", t)
                if ccs:
                    text = " ".join(anc + [t, body])
                    nm = voices_in(" ".join(anc + [t, body]))
                    out[st.lineno] = {"pervoice": bool(pv_re.search(text)),
                                      "voices": nm or None}
                walk(st.body, anc + [t])
                walk(st.orelse, anc)
            else:
                for f in ("body", "orelse", "finalbody"):
                    sub = getattr(st, f, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        walk(sub, anc)
    walk(fn.body, [])
    return out


def _cfg_voice():
    return dict(_cfg().MODULE_VOICE)


def _poly_entries():
    G = _gen()
    out, seen = [], set()
    for b in _branches("PolySynth"):
        for cc in b["ccs"]:
            if cc in seen or ("poly", cc) in ALIASES:
                continue
            seen.add(cc)
            lab = G.describe("PolySynth", b, cc)
            out.append(_e(f"poly.cc{cc}", f"Poly > {_group(lab)}", lab, "cc",
                          {"kind": "cc", "module": "poly", "cc": cc},
                          {"kind": "json", "file": POLY_PARAMS, "path": [str(cc)]},
                          note="written by the poly host ~1.5 s after the change (persist_thread)"))
    return out


def mono_layer_ccs():
    """{cc: (layer, field)} for MonoSynth's layer CCs, read out of the loop
    `for li, base in ((1, 20), (2, 25)): if base <= cc <= base + 4: ... off == k:
    L['field'] = ...` (dict math the generic `cc == N` scan cannot see)."""
    tree = ast.parse(_src("orc1_synth.py"))
    cls = _class_node(tree, "MonoSynth")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "set_cc")
    out = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple):
            pairs = [ast.literal_eval(e) for e in n.iter.elts]
            span, fields = None, {}
            for m in ast.walk(n):
                if isinstance(m, ast.Compare) and len(m.ops) == 2 and isinstance(m.comparators[1], ast.BinOp):
                    span = ast.literal_eval(m.comparators[1].right)
                if (isinstance(m, ast.If) and isinstance(m.test, ast.Compare)
                        and isinstance(m.test.left, ast.Name) and m.test.left.id == "off"):
                    k = ast.literal_eval(m.test.comparators[0])
                    tgt = m.body[0].targets[0] if isinstance(m.body[0], ast.Assign) else None
                    if isinstance(tgt, ast.Subscript):
                        fields[k] = ast.literal_eval(tgt.slice)
            for li, base in pairs:
                for off in range((span or 0) + 1):
                    out[base + off] = (li, fields.get(off, f"param {off}"))
    return out


_LAYER_WORDS = {"on": "on/off (64+ = on)", "semi": "semitone offset (-24..+24)",
                "cents": "detune (64 = none, +/-50 cents)", "wave": "waveform", "gain": "gain"}


def _mono_entries():
    G = _gen()
    out, seen = [], set()
    note = "written by the mono host ~1.5 s after the change (persist_thread)"
    for b in _branches("MonoSynth"):
        for cc in b["ccs"]:
            if cc in seen or ("mono", cc) in ALIASES:
                continue
            seen.add(cc)
            lab = G.describe("MonoSynth", b, cc)
            if cc == 19:
                lab = "Layer 0 gain"
            out.append(_e(f"mono.cc{cc}", f"Mono > {_group(lab)}", lab, "cc",
                          {"kind": "cc", "module": "mono", "cc": cc},
                          {"kind": "json", "file": MONO_PARAMS, "path": [str(cc)]}, note=note))
    for cc, (li, field) in sorted(mono_layer_ccs().items()):
        if cc in seen:
            continue
        seen.add(cc)
        lab = f"Layer {li} {_LAYER_WORDS.get(field, field)}"
        out.append(_e(f"mono.cc{cc}", "Mono > Layers", lab, "cc",
                      {"kind": "cc", "module": "mono", "cc": cc},
                      {"kind": "json", "file": MONO_PARAMS, "path": [str(cc)]}, note=note))
    return out


def _chip_entries():
    """NESEngine + GameboyWaveVoice. Engine-wide CCs become ONE "chip.ccN"
    entry; per-voice CCs one "<module>.ccN" entry per chip module they act on."""
    G = _gen()
    C = _cfg()
    voice_of = _cfg_voice()                       # module -> voice

    info = nes_branch_info()
    # the persist skip set, read out of orc1_synth.py (not retyped)
    synth = _src("orc1_synth.py")
    skip = _chip_persist_skip(synth)
    out, done = [], set()
    note_file = "written by the chip host ~1.5 s after the change (persist_thread)"
    for b in _branches("NESEngine"):
        bi = info.get(b["line"], {"pervoice": True, "voices": None})
        for cc in b["ccs"]:
            lab = G.describe("NESEngine", b, cc)
            if not bi["pervoice"]:
                key = ("chip", cc)
                if key in done or key in ALIASES:
                    continue
                done.add(key)
                reach_wave = G._branch_applies(b["extra"], "wave")
                mods = list(CHIP_NONWAVE) + (["gb_wave"] if reach_wave else [])
                voices = [voice_of[m] for m in mods if m in voice_of]
                ent = _e(f"chip.cc{cc}", f"Chip (all voices) > {_group(lab)}", lab, "cc",
                         {"kind": "cc", "module": mods, "cc": cc},
                         {"kind": "json_agree", "file": CHIP_PARAMS,
                          "primary": ["_engine", str(cc)],
                          "paths": [[v, str(cc)] for v in voices]},
                         note="ENGINE-WIDE: one value for every chip voice, saved once under "
                              "\"_engine\" (2026-10-26). A file the chip host has not "
                              "rewritten since may still hold it per voice: then capture "
                              "returns it only when those agree. " + note_file)
                if cc in skip:
                    ent.update(sceneable=False, read=None,
                               why="P1/P2 join control (ephemeral, owned by /matrix MODULE_JOIN; "
                                   "deliberately not persisted, orc1_synth _CHIP_PERSIST_SKIP)")
                out.append(ent)
                continue
            for mod in CHIP_NONWAVE + ("gb_wave",):
                v = voice_of.get(mod)
                if v is None or not G._branch_applies(b["extra"], v):
                    continue
                if bi["voices"] is not None and v not in bi["voices"]:
                    continue
                key = (mod, cc)
                if key in done:
                    continue
                done.add(key)
                out.append(_e(f"{mod}.cc{cc}", f"{_title(mod)} > {_group(lab)}", lab, "cc",
                              {"kind": "cc", "module": mod, "cc": cc},
                              {"kind": "json", "file": CHIP_PARAMS, "path": [v, str(cc)]},
                              note=note_file))
    # GB wave's own handler (reached via NESEngine.set_cc(voice='wave') ->
    # self._wave.set_cc); a CC NESEngine already listed for the wave wins.
    harm0 = G.module_consts(ast.parse(synth)).get("_GB_HARM_CC0")
    for b in _branches("GameboyWaveVoice"):
        for cc in b["ccs"]:
            key = ("gb_wave", cc)
            if key in done:
                continue
            done.add(key)
            lab = G.describe("GameboyWaveVoice", b, cc)
            ent = _e(f"gb_wave.cc{cc}", f"GB wave > {_group(lab)}", lab, "cc",
                     {"kind": "cc", "module": "gb_wave", "cc": cc},
                     {"kind": "json", "file": CHIP_PARAMS, "path": ["wave", str(cc)]},
                     note=note_file)
            if harm0 is not None and harm0 <= cc < harm0 + 8:
                ent["read"] = {"kind": "json", "file": GB_WAVE, "path": ["harm", cc - harm0],
                               "missing": 0}
                ent["note"] = ("knob position kept in ~/.orc1_gb_wave.json 'harm' "
                               "(chip host flush_wave_state, ~1.5 s); code default 0")
            elif cc == 23:
                ent["read"] = {"kind": "json", "file": GB_WAVE, "path": ["use_custom"],
                               "decode": "bool127", "missing": 0}
                ent["note"] = "kept in ~/.orc1_gb_wave.json 'use_custom' (not the patch file)"
            elif cc in (20, 21):
                ent.update(sceneable=False, read=None,
                           why="momentary draw of one wave-RAM step (step / value pair); the "
                               "table itself lives in ~/.orc1_gb_wave.json, not a knob")
            elif cc in skip:
                ent.update(sceneable=False, read=None,
                           why="momentary STORE arm (held while a program change saves a slot)")
            out.append(ent)
    return out


def _chip_persist_skip(synth_src):
    """The chip host's persist-skip set, evaluated from orc1_synth.py's own
    `_CHIP_PERSIST_SKIP = ...` / `|= ...` lines."""
    G = _gen()
    tree = ast.parse(synth_src)
    consts = G.module_consts(tree)
    consts.setdefault("_ENGINE_MASTER_CC", 13)
    env = dict(consts, set=set, range=range)
    val = set()
    for st in tree.body:
        tgt = None
        if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name):
            tgt, op = st.targets[0].id, "="
        elif isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name):
            tgt, op = st.target.id, "|="
        if tgt != "_CHIP_PERSIST_SKIP":
            continue
        v = eval(compile(ast.Expression(st.value), "<skip>", "eval"), {"__builtins__": {}}, env)
        val = set(v) if op == "=" else (val | set(v))
    return val


def _title(mod):
    return {"chip_p1": "Chip P1", "chip_p2": "Chip P2", "chip_tri": "Chip triangle",
            "chip_noise": "Chip noise", "chip_noise2": "Chip noise 2", "chip_pcm": "Chip PCM",
            "gb_wave": "GB wave"}.get(mod, mod)


# ---------------------------------------------------------------------------
# Drums (cfg.DRUM_CC)
# ---------------------------------------------------------------------------

def _drum_entries():
    C = _cfg()
    out = []
    for cc, (voice, param) in sorted(C.DRUM_CC["CC_TO_VP"].items()):
        out.append(_e(f"drums.{voice}.{param}",
                      f"Drums > {voice.replace('_', ' ').title()}",
                      param.replace("_", " "), "cc",
                      {"kind": "cc", "module": "drums", "cc": int(cc)},
                      {"kind": "json", "file": DRUM_PARAMS, "path": [voice, param]},
                      note="written by the drum host ~1.5 s after the change; a voice never "
                           "touched keeps its code default (not a CC value: read gives None)"))
    return out


# ---------------------------------------------------------------------------
# Bus levels (CH_CTRL of the synth bus)
# ---------------------------------------------------------------------------

def _level_entries():
    C = _cfg()                       # also puts HERE on sys.path
    import cc_surfaces as X
    lv = {k: cc for cc, _l, k in X.bus_levels(C)}
    out = []
    spec = [("MASTER_CC", "master", "Master", "master"),
            ("POLY_CC", "poly", "Poly bus", "poly"),
            ("CHIP_CC", "chip", "Chip bus (all chip voices)", "chip"),
            ("DRUM_CC", "drum", "Drum bus", "drum"),
            ("MONO_CC", "mono", "Mono bus", "mono"),
            ("BBKIT_VOL_CC", "bbkit", "BB KIT bus", "bbkit")]
    for key, sid, lab, fkey in spec:
        if key not in lv:
            continue
        e = _e(f"level.{sid}", "Levels", lab, "cc", {"kind": "bus_cc", "cc": lv[key]},
               {"kind": "json", "file": BUS_LEVELS, "path": [fkey]},
               note="~/.orc1_bus_levels.json, written by the owning host ~1.5 s after a "
                    "change (flock'd merge, orc1_synth._bus_level_write); a bus never moved "
                    "since the file existed is absent (boot level, read gives None)", order=40)
        if sid == "master":
            e.update(sceneable=False,
                     why="no module host applies CH_CTRL CC%d live: each host bakes the saved "
                         "'master' in as a static gain at start (orc1_poly_host._static_master; "
                         "the router ignores MASTER_CC)" % lv[key])
        out.append(e)
    for key, sid, lab in (("FXMIX_MIX_CC", "mix", "FX mixer wet/dry"),
                          ("FXMIX_LEVEL_CC", "level", "FX mixer return level (64 = unity)")):
        if key in lv:
            out.append(_e(f"fx.{sid}", "Levels", lab, "cc", {"kind": "bus_cc", "cc": lv[key]},
                          None, order=40,
                          note="the legacy single FX mixer (orc1_fxmix.py, CCs %d/%d): it keeps "
                               "mix/level in memory only and publishes nothing, so capture cannot "
                               "read it (type the value); no mixer process listens unless one "
                               "was started" % (lv["FXMIX_MIX_CC"], lv["FXMIX_LEVEL_CC"])))
    return out


# ---------------------------------------------------------------------------
# Brain: tempo, harmony, arp (state = ~/orc1_state.json)
# ---------------------------------------------------------------------------

def _ctrl_cc_for(action):
    """CH_CTRL CC bound to a brain action name in cfg's maps (CTRL_CC_MAP,
    ARP_CC_MAP), so a config change follows. None if unbound."""
    C = _cfg()
    for d in (C.ARP_CC_MAP, C.CTRL_CC_MAP):
        for cc, act in d.items():
            if act == action:
                return int(cc)
    return None


def _brain_entries():
    C = _cfg()
    brain = _src("orc1.py")
    arp_src = _src("orc1_arp.py")
    subdiv = _literal(arp_src, "SUBDIVISION_LIST") or []
    scale_names = _literal(brain, "SCALE_NAMES") or []
    scale_modes = list(_literal(brain, "SCALE_MODES") or [])
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import orc1_patterns as P
    patterns = list(P.PATTERN_ORDER)
    st = lambda *p: {"kind": "state", "path": list(p)}
    lag = ("captured from ~/orc1_state.json (fresh after any command-socket action; "
           "after a CH_CTRL knob it lags until the next print_status)")
    out = [
        _e("tempo.bpm", "Tempo", "Tempo (BPM)", "float",
           {"kind": "action", "action": "SET_BPM", "payload": {"value": "$v"}, "skip_if": "clock_external"},
           st("arp", "bpm"), rng=(20.0, 300.0), order=20,
           note="skipped while following an outside clock; the state's bpm is the clock "
                "ESTIMATE rounded to 0.1 (orc1.write_state_json). " + lag),
        _e("tempo.clock_mode", "Tempo", "Clock source", "enum",
           {"kind": "action", "action": "SET_CLOCK_SRC", "payload": {"mode": "$v"}},
           st("arp", "clock_mode"), choices=["internal", "external", "drift"], order=15,
           note=lag),
        _e("harmony.root", "Harmony", "Key / root (0 = C)", "int",
           {"kind": "action", "action": "ROOT_SET", "payload": {"value": "$v"}},
           st("root"), rng=(0, 11), default=0, order=30, note=lag),
        _e("harmony.scale", "Harmony", "Scale", "enum",
           {"kind": "action", "action": "SCALE_SET", "payload": {"value": "$v"}},
           st("scale"), choices=scale_names, order=30,
           note="names = orc1.SCALES (the live list is in the state's 'scales'). " + lag),
        _e("harmony.scale_mode", "Harmony", "Scale mode (note snapping)", "enum",
           {"kind": "action", "action": "SET_SCALE_MODE", "payload": {"value": "$v"}},
           st("scale_mode"), choices=scale_modes, order=30, note=lag),
        _e("harmony.quality_mode", "Harmony", "Chord quality (DIAT / FIXED)", "enum",
           {"kind": "action", "action": "SET_QUALITY_MODE", "payload": {"value": "$v"}},
           st("quality_mode"), choices=["DIAT", "FIXED"], order=30, note=lag),
        _e("harmony.preset", "Harmony", "Chord preset", "enum",
           {"kind": "action", "action": "SET_PRESET", "payload": {"preset": "$v"}},
           st("preset"), choices=sorted(C.PRESETS), default=C.DEFAULT_PRESET, order=25,
           note="SET_PRESET releases held chord notes. " + lag),
    ]
    vmap = {k: [_ctrl_cc_for("VOICING_" + k), 127] for k in ("OPEN", "CLOSE", "DROP2")}
    sp = _ctrl_cc_for("SPREAD")
    if sp is not None:
        vmap["SPREAD"] = [sp, 100]
    out.append(_e("harmony.voicing", "Harmony", "Voicing", "enum",
                  {"kind": "ctrl_cc", "enc": {"type": "map", "map": vmap}},
                  st("voicing"), choices=sorted(vmap), order=30,
                  note="OPEN/CLOSE/DROP2 = their CH_CTRL latch CCs; SPREAD = the SPREAD knob at "
                       "100 (the knob's own position is not in the state, so its exact amount "
                       "is not captured). " + lag))
    for sid, act, lab, typ, enc, path in (
            ("harmony.vl_strength", "VL_STRENGTH", "Voice leading strength (0 = off)", "float",
             {"type": "linear", "lo": 0.0, "hi": 1.0}, ("vl_strength",)),
            ("harmony.key_lock", "KEY_LOCK", "Key lock", "bool", {"type": "bool"}, ("key_lock",)),
            ("arp.enabled", "ARP_TOGGLE", "Arp on", "bool", {"type": "bool"}, ("arp", "enabled")),
            ("arp.hold", "ARP_HOLD", "Arp hold (latch)", "bool", {"type": "bool"}, ("arp_hold",)),
            ("arp.rate", "ARP_RATE", "Arp rate", "enum",
             {"type": "index", "choices": subdiv}, ("arp", "rate")),
            ("arp.gate", "ARP_GATE", "Arp gate (x0.1..x2)", "float",
             {"type": "linear", "lo": 0.1, "hi": 2.0}, ("arp", "gate")),
            ("arp.swing", "ARP_SWING", "Arp swing (0..0.5)", "float",
             {"type": "linear", "lo": 0.0, "hi": 0.5}, ("arp", "swing")),
            ("arp.humanize", "ARP_HUMANIZE", "Arp humanize (0..1)", "float",
             {"type": "linear", "lo": 0.0, "hi": 1.0}, ("arp", "humanize"))):
        cc = _ctrl_cc_for(act)
        rng = (enc["lo"], enc["hi"]) if enc["type"] == "linear" else None
        ent = _e(sid, "Arp" if sid.startswith("arp") else "Harmony", lab, typ,
                 {"kind": "ctrl_cc", "cc": cc, "action": act, "enc": enc}, st(*path),
                 rng=rng, choices=enc.get("choices"), order=30,
                 note=("float rounded to 0.01 in the state, so recall may land one CC step off. "
                       if typ == "float" else "") + lag)
        if cc is None:
            ent.update(sceneable=False, why=f"no CH_CTRL CC is mapped to {act} in cfg")
        out.append(ent)
    out.append(_e("arp.pattern", "Arp", "Arp pattern", "enum",
                  {"kind": "ctrl_pc", "base": int(C.ARP_PC_OFFSET),
                   "enc": {"type": "index_exact", "choices": patterns}},
                  st("arp", "pattern"), choices=patterns, order=30,
                  note="program change ARP_PC_OFFSET + index in orc1_patterns.PATTERN_ORDER. " + lag))
    return out


# ---------------------------------------------------------------------------
# Visuals (daw/orc_visuals.py KNOBS + SCENE_CC)
# ---------------------------------------------------------------------------

def visuals_knob_defaults(src=None):
    """KNOB_DEFAULT evaluated from the renderer's source (it holds a dict
    comprehension, so literal_eval is not enough); a knob not in it = 64."""
    src = src if src is not None else _src(os.path.join("daw", "orc_visuals.py"))
    node = _module_assign(src, "KNOB_DEFAULT")
    if node is None:
        return {}
    return eval(compile(ast.Expression(node), "<KNOB_DEFAULT>", "eval"),
                {"__builtins__": {}}, {"range": range})


def visuals_scenes(src=None):
    src = src if src is not None else _src(os.path.join("daw", "orc_visuals.py"))
    node = _module_assign(src, "SCENES")
    return [k.value for k in node.keys] if isinstance(node, ast.Dict) else []


def _visuals_thing(name):
    if name.endswith("_pal"):
        return "Visuals > Palettes"
    for pre, t in (("bg_", "Background"), ("mask_", "Peephole"), ("lines_", "Lines"),
                   ("band", "Listen"), ("sens", "Listen")):
        if name.startswith(pre):
            return "Visuals > " + t
    return "Visuals > Play"


def _visuals_entries():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import cc_surfaces as X
    src = _src(os.path.join("daw", "orc_visuals.py"))
    knobs = X._dict_with_comments(src, "KNOBS")
    dflt = visuals_knob_defaults(src)
    note = ("~/.orc1_visuals_state.json 'knobs', written by the renderer at most every 2 s "
            "and ONLY while it runs (orc1-visuals kiosk); with the renderer stopped the CC "
            "is not heard and the file keeps the last value")
    out = []
    for cc, (name, comment) in sorted(knobs.items()):
        d = int(dflt.get(cc, 64))
        out.append(_e(f"visuals.{name}", _visuals_thing(name),
                      (comment or name).split(";")[0].strip() or name, "cc",
                      {"kind": "cc", "module": "visuals", "cc": int(cc)},
                      {"kind": "json", "file": VISUALS_STATE, "path": ["knobs", name], "missing": d},
                      default=d, note=note))
    scene_cc = X._int_const(src, "SCENE_CC")
    if scene_cc is not None:
        names = visuals_scenes(src)
        out.append(_e("visuals.scene", "Visuals > Play", "Scene (or cycle)", "enum",
                      {"kind": "cc", "module": "visuals", "cc": scene_cc,
                       "enc": {"type": "visuals_scene", "choices": names}},
                      {"kind": "visuals_scene"}, choices=["cycle"] + names, default="cycle",
                      note="CC %d: 0 = cycle, else names[(v-1)*n//127] (orc_visuals render "
                           "loop). " % scene_cc + note))
    return out


# ---------------------------------------------------------------------------
# FX rack, BeatBuddy, sampler
# ---------------------------------------------------------------------------

def _fx_entries():
    src = _src("orc1_fx_rack.py")
    n = _literal(src, "N_SLOTS") or 3
    out = []
    for i in range(int(n)):
        out.append(_e(f"fx.rack.{i}.enabled", "FX > Rack", f"Slot {i + 1} on", "bool",
                      {"kind": "action", "action": "RACK_ENABLE",
                       "payload": {"idx": i, "enabled": "$v"}},
                      {"kind": "json", "file": FX_RACK, "path": ["slots", i, "enabled"],
                       "missing": False},
                      default=False, order=60, sceneable=False,
                      why="RACK_ENABLE -> RackManager.set_enabled -> apply() tears down and "
                          "respawns EVERY rack jalv (even when the value is unchanged): killing "
                          "JACK clients risks skipback xruns, and the RACK_PARAM writes that "
                          "follow would race the rebuild. Switch slots on/off by hand",
                      note="~/.orc1_fx_rack.json, saved at once (RackManager.set_enabled). "
                           "Turning a slot on/off REBUILDS the rack's jalv chain (apply(), "
                           "~1-4 s): no host restart, but the FX return gaps"))
        out.append(_e(f"fx.rack.{i}.params", "FX > Rack", f"Slot {i + 1} knobs", "dict",
                      {"kind": "action", "action": "RACK_PARAM", "per_key": "symbol",
                       "payload": {"idx": i, "symbol": "$k", "value": "$v"},
                       "require": {"rack_plugin": i}},
                      {"kind": "json", "file": FX_RACK, "path": ["slots", i],
                       "decode": "rack_params"},
                      order=61,
                      note="value = {plugin, params{symbol: value}}; recall sends RACK_PARAM per "
                           "symbol (live FIFO write) and must be skipped when the slot now holds "
                           "a different plugin. Saved debounced ~1 s (RackManager._schedule_save); "
                           "a knob never moved is not in the file (plugin default)"))
        out.append(_e(f"fx.rack.{i}.plugin", "FX > Rack", f"Slot {i + 1} plugin", "str",
                      {"kind": "action", "action": "RACK_ASSIGN",
                       "payload": {"idx": i, "plugin": "$v"}},
                      {"kind": "json", "file": FX_RACK, "path": ["slots", i, "plugin"]},
                      sceneable=False, order=59,
                      why="assigning a plugin spawns a new jalv process and rebuilds the chain; "
                          "not a live knob"))
    return out


def fx_insert_entries(home_file=None):
    """Per-insert mix/level, from ~/.orc1_fx_inserts.json AT CALL TIME."""
    try:
        with open(os.path.expanduser(home_file or FX_INSERTS)) as f:
            ins = json.load(f).get("inserts", [])
    except Exception:
        ins = []
    out = []
    for i in ins:
        iid = i.get("id") if isinstance(i, dict) else None
        if not iid:
            continue
        for k, lab in (("mix", "wet/dry (0..1)"), ("level", "return level (0..1, 0.5 = unity)")):
            out.append(_e(f"fx.insert.{iid}.{k}", "FX > Inserts", f"Insert {iid} {lab}", "float",
                          {"kind": "action", "action": "FXMIX_SET", "payload": {"id": iid, k: "$v"}},
                          None, rng=(0.0, 1.0), order=62,
                          note="orc1_fxmix keeps mix/level in memory only: nothing to read back"))
    return out


def bbkit_kits(kits_dir=None):
    """Kit folder names in the order the BBKit host indexes them (sorted dirs
    holding kit.json under BBKIT_DIR or orc1/kits) = the program number."""
    d = kits_dir
    if d is None:
        d = (_cfg().SYNTH or {}).get("BBKIT_DIR") or os.path.join(HERE, "kits")
    return [os.path.basename(p) for p in sorted(glob.glob(os.path.join(d, "*")))
            if os.path.isfile(os.path.join(p, "kit.json"))]


def _bb_entries():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import orc1_beatbuddy as B
    out = [
        _e("bb.song", "BeatBuddy", "Song", "str",
           {"kind": "action", "action": "BB_SONG_SELECT", "payload": {"song": "$v"}},
           {"kind": "json", "file": BB_SONG, "path": ["song"]}, order=10,
           note="absolute path; loading a song SETS THE TEMPO to the song's bpm unless the "
                "tempo mode is 'clock' (so a scene recalls the song before tempo.bpm). "
                "~/.orc1_bb_song.json is written at once by orc1._bb_song_select"),
        _e("bb.kit", "BeatBuddy", "BB KIT drumset", "str",
           {"kind": "action", "action": "BB_KIT_SELECT", "payload": {"program": "$v"},
            "enc": {"type": "bbkit"}},
           {"kind": "json", "file": BB_KIT, "path": ["kit"]}, order=12,
           note="kit FOLDER name; recall turns it into the program number = its index in the "
                "host's sorted kit list (bbkit_kits()). Written by the BBKit host when a kit "
                "loads (orc1_bbkit_host._do_load)"),
        _e("bb.tempo_mode", "BeatBuddy", "Tempo mode (song / clock)", "enum",
           {"kind": "action", "action": "BB_TEMPO_MODE", "payload": {"mode": "$v"}},
           {"kind": "state", "path": ["beatbuddy", "tempo_mode"]}, choices=["song", "clock"],
           default="song", order=11,
           note="brain memory only (_bb_follow_clock, resets to 'song' on restart); read from "
                "~/orc1_state.json, present only while the BeatBuddy mind is loaded"),
    ]
    for g, lab in B.DRUM_GROUPS:
        out.append(_e(f"bb.mix.{g}", "BeatBuddy > Mix", f"{lab.title()} level (0 = mute)", "float",
                      {"kind": "action", "action": "BB_MIX", "payload": {"group": g, "gain": "$v"}},
                      {"kind": "json", "file": BB_MIX, "path": [g], "missing": 1.0},
                      rng=(0.0, float(B.MIX_MAX)), default=1.0,
                      note="~/.orc1_bb_mix.json, written at once by orc1._bb_mix_set; 1.0 is "
                           "dropped from the file, so absent = 1.0"))
    return out


def _sampler_entries():
    C = _cfg()
    S = C.SAMPLER
    out = []
    why_ro = ("the sampler host publishes no deck state: SampleDeck.status() is in-process "
              "only and ~/.orc1_sampler_view_<deck>.json holds peaks/start/end/cues")
    for deck in ("A", "B"):
        for key, act, lab in (("gain", "SAMPLER_GAIN", "gain (0..1.5x)"),
                              ("filter", "SAMPLER_FILTER", "filter (64 = off, <64 LP, >64 HP)"),
                              ("speed", "SAMPLER_SPEED", "speed (64 = 1x)")):
            out.append(_e(f"sampler.{deck}.{key}", f"Sampler > Deck {deck}", f"Deck {deck} {lab}",
                          "cc", {"kind": "action", "action": act,
                                 "payload": {"deck": deck, "value": "$v"}},
                          None, order=70, note="no capture: " + why_ro + " (type the value)"))
        for key, act, lab in (("loop", "SAMPLER_LOOP", "loop play"),
                              ("reverse", "SAMPLER_REVERSE", "reverse")):
            out.append(_e(f"sampler.{deck}.{key}", f"Sampler > Deck {deck}", f"Deck {deck} {lab}",
                          "bool", {"kind": "action", "action": act, "payload": {"deck": deck}},
                          None, sceneable=False, order=70,
                          why=f"{act} is a TOGGLE with no absolute form and no read-back, so a "
                              "recall cannot land on a chosen state"))
    out.append(_e("sampler.xfade", "Sampler", "Crossfade A/B", "cc",
                  {"kind": "action", "action": "SAMPLER_XFADE", "payload": {"value": "$v"}},
                  None, order=70, note="no capture: " + why_ro))
    out.append(_e("level.sampler", "Levels", "Sampler master (both decks)", "cc",
                  {"kind": "action", "action": "SAMPLER_MASTER_GAIN", "payload": {"value": "$v"}},
                  None, order=40, note="no capture: " + why_ro))
    return out


def _fire_entries():
    """Our Fire sequencer firmware's pattern (host 0D, v0.30+)."""
    return [_e("fire.pattern", "Fire sequencer", "Fire pattern (0-31; song mode: song slot)", "int",
               {"kind": "action", "action": "FIRE_PATTERN", "payload": {"pattern": "$v"}},
               {"kind": "brain", "key": "fire.pattern"}, rng=(0, 31), order=5,
               note="host 0D: the Fire switches at ITS next bar (at once when stopped); the scenes "
                    "engine sends it one bar early when the Fire is the bar source. Needs Fire "
                    "firmware v0.30 (hello version 2); older = skipped. Captured from the Fire's "
                    "last bar message (0F), so only while its bar messages run")]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Building the registry parses orc1.py / orc1_synth.py etc. with ast: ~3.4 s on
# the Pi, and it holds the GIL in ~300 ms stretches. The BRAIN must never pay
# that on one of its own threads (MIDI input, clock, command socket), so it:
#   1. calls set_build_allowed(False): registry() then raises NotReady instead
#      of building in-process;
#   2. runs `nice python3 -B orc1_settings_registry.py --build-cache` as a
#      SEPARATE PROCESS, which writes CACHE_FILE (JSON) keyed by the mtime +
#      size of every source the build reads (source_signature());
#   3. load_cache() reads that JSON (fast) when the signature still matches.
# Tests and the web keep the default (build in-process when no cache).
CACHE_FILE = "~/.cache/orc1/settings_registry.json"
_CACHE = None
_CACHE_SIG = None
_LOCK = threading.RLock()
_BUILD_ALLOWED = True


class NotReady(RuntimeError):
    """The settings list is not loaded yet (the brain builds it in a separate process)."""


def set_build_allowed(on):
    global _BUILD_ALLOWED
    _BUILD_ALLOWED = bool(on)


def _source_files():
    fs = sorted(glob.glob(os.path.join(HERE, "orc1*.py")))
    fs += [os.path.join(HERE, n) for n in ("gen_cc_map.py", "cc_surfaces.py",
                                           os.path.join("daw", "orc_visuals.py"))]
    return [f for f in fs if os.path.isfile(f)]


def source_signature():
    """[[relpath, mtime_ns, size]] of every source the build reads (cheap: stats)."""
    out = []
    for f in _source_files():
        st = os.stat(f)
        out.append([os.path.relpath(f, HERE).replace(os.sep, "/"), st.st_mtime_ns, st.st_size])
    return out


def _build():
    ents = []
    for fn in (_brain_entries, _level_entries, _poly_entries, _mono_entries,
               _chip_entries, _drum_entries, _visuals_entries, _fx_entries,
               _bb_entries, _sampler_entries, _fire_entries):
        ents.extend(fn())
    # JSON-normalise (tuples -> lists) so a built registry and a cached one are identical
    return json.loads(json.dumps(ents))


def build_cache(path=None):
    """Build the registry (in THIS process) and write the JSON cache atomically."""
    p = os.path.expanduser(path or CACHE_FILE)
    sig = source_signature()
    ents = _build()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 1, "sig": sig, "entries": ents}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return len(ents)


def load_cache(path=None):
    """Load the JSON cache into memory when its signature matches the sources
    now on disk. True = loaded (or already current). Never builds."""
    global _CACHE, _CACHE_SIG
    p = os.path.expanduser(path or CACHE_FILE)
    sig = source_signature()
    with _LOCK:
        if _CACHE is not None and _CACHE_SIG == sig:
            return True
        try:
            with open(p) as f:
                data = json.load(f)
        except Exception:
            return False
        if data.get("version") != 1 or data.get("sig") != sig or not data.get("entries"):
            return False
        _CACHE, _CACHE_SIG = data["entries"], sig
        return True


def cache_current(path=None):
    """True when the in-memory registry matches the sources on disk."""
    with _LOCK:
        return _CACHE is not None and _CACHE_SIG == source_signature()


def ready():
    with _LOCK:
        return _CACHE is not None


def registry(refresh=False):
    """[entry] -- the static registry plus the call-time FX inserts. Raises
    NotReady when nothing is loaded and in-process building is switched off."""
    global _CACHE, _CACHE_SIG
    with _LOCK:
        if _CACHE is None or refresh:
            if not _BUILD_ALLOWED:
                if refresh or not load_cache():
                    raise NotReady("settings list loading")
            else:
                _CACHE, _CACHE_SIG = _build(), source_signature()
        ents = list(_CACHE)
    return ents + fx_insert_entries()


def by_id(refresh=False):
    out = {}
    for e in registry(refresh):
        if e["id"] in out:
            raise ValueError(f"duplicate registry id {e['id']}")
        out[e["id"]] = e
    return out


# ---------------------------------------------------------------------------
# READ (capture)
# ---------------------------------------------------------------------------

def _load(path, cache):
    p = os.path.expanduser(path)
    if p not in cache:
        try:
            with open(p) as f:
                cache[p] = json.load(f)
        except Exception:
            cache[p] = None
    return cache[p]


_MISS = object()


def _dig(obj, path):
    for k in path:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        elif isinstance(obj, list) and isinstance(k, int) and 0 <= k < len(obj):
            obj = obj[k]
        else:
            return _MISS
    return obj


def _decode(v, how):
    if how == "bool127":
        return 127 if v else 0
    if how == "rack_params":
        if not isinstance(v, dict) or not v.get("plugin"):
            return None
        return {"plugin": v["plugin"], "params": dict(v.get("params") or {})}
    return v


def read_one(entry, cache=None):
    """(value, source, note) for one entry. value None = nothing readable."""
    cache = {} if cache is None else cache
    r = entry.get("read")
    if not r:
        return None, None, entry.get("why") or entry.get("note") or "no readable owner"
    k = r["kind"]
    if k == "json":
        data = _load(r["file"], cache)
        v = _dig(data, r["path"]) if data is not None else _MISS
        if v is _MISS:
            if "missing" in r:
                return r["missing"], "code default", "not saved yet"
            return None, None, f"{r['file']}: key not saved"
        return _decode(v, r.get("decode")), r["file"], ""
    if k == "json_agree":
        data = _load(r["file"], cache)
        if r.get("primary") and data is not None:
            v = _dig(data, r["primary"])
            if v is not _MISS:
                return v, r["file"], ""
        vals = []
        for p in r["paths"]:
            v = _dig(data, p) if data is not None else _MISS
            if v is not _MISS:
                vals.append(v)
        if not vals:
            return None, None, f"{r['file']}: never saved"
        if len(set(vals)) == 1:
            return vals[0], r["file"], ""
        return None, None, f"ambiguous: voices hold {sorted(set(vals))}"
    if k == "state":
        data = _load(STATE_JSON, cache)
        v = _dig(data, r["path"]) if data is not None else _MISS
        if v is _MISS:
            return None, None, "not in ~/orc1_state.json"
        return _decode(v, r.get("decode")), STATE_JSON, ""
    if k == "visuals_scene":
        data = _load(VISUALS_STATE, cache)
        if not isinstance(data, dict):
            return None, None, "no visuals state"
        if data.get("cycling"):
            return "cycle", VISUALS_STATE, ""
        return data.get("scene"), VISUALS_STATE, ""
    if k == "brain":
        return None, None, "held by the running brain: read live (SETTINGS_READ)"
    return None, None, f"unknown read kind {k}"


def read_values(ids, detail=False):
    """Current value of each id (for CAPTURE). {id: value | None}; with
    detail=True {id: {"value", "source", "note"}}. Unknown ids -> KeyError."""
    reg = by_id()
    cache, out = {}, {}
    for i in ids:
        v, src, note = read_one(reg[i], cache)
        out[i] = {"value": v, "source": src, "note": note} if detail else v
    return out


# ---------------------------------------------------------------------------
# RECALL (pure: returns what to send; the scenes engine sends it)
# ---------------------------------------------------------------------------

def channel_of(module, bindings):
    """1-based channel a MODULE is bound to, or None. Accepts the file shape
    {"10": "drums"} (~/.orc1_bindings.json "bindings") or the brain/state shape
    {"drums": 10}."""
    if not bindings:
        return None
    if module in bindings and isinstance(bindings[module], int):
        return bindings[module]
    for ch, m in bindings.items():
        if m == module:
            try:
                return int(ch)
            except (TypeError, ValueError):
                return None
    return None


def encode(enc, value, ctx=None):
    """natural value -> 0-127 (or a program) for a ctrl_cc / ctrl_pc / enc'd cc."""
    t = enc["type"]
    if t == "bool":
        return 127 if value else 0
    if t == "linear":
        lo, hi = enc["lo"], enc["hi"]
        return max(0, min(127, int(round((float(value) - lo) / (hi - lo) * 127))))
    if t == "index":
        # handle_arp_cc: idx = int((value / 127) * (N - 1)); first v that lands on idx
        ch = enc["choices"]
        i, n = ch.index(value), len(ch)
        for v in range(128):
            if int((v / 127) * (n - 1)) == i:
                return v
        raise ValueError(value)
    if t == "index_exact":
        return enc["choices"].index(value)
    if t == "visuals_scene":
        if value == "cycle":
            return 0
        names = enc["choices"]
        i, n = names.index(value), len(names)
        for v in range(1, 128):
            if min(n - 1, (v - 1) * n // 127) == i:
                return v
        raise ValueError(value)
    if t == "bbkit":
        kits = (ctx or {}).get("bb_kits")
        if kits is None:
            kits = bbkit_kits()
        return kits.index(value)
    if t == "map":
        return enc["map"][value]
    raise ValueError(f"unknown encoding {t}")


def _fill(payload, value, key=None):
    out = {}
    for k, v in payload.items():
        out[k] = value if v == "$v" else (key if v == "$k" else v)
    return out


def build_recall(values, bindings, ctx=None):
    """{id: value} -> (messages, skipped), in recall order.

    messages: dicts, one of
      {"type": "cc", "channel": 1-16, "cc": n, "value": v, "id": ...}   synth bus
      {"type": "ctrl_cc", "cc": n, "value": v, "id": ...}   brain handle_ctrl_cc
      {"type": "ctrl_pc", "program": p, "id": ...}           brain handle_ctrl_pc
      {"type": "action", "cmd": {"action": ..., ...}, "id": ..., "require": {...}?}
    skipped: [(id, reason)] -- unknown id, not sceneable, module unbound, value
    not encodable, outside clock for tempo.
    ctx (optional): {"clock_mode": "external"|..., "bb_kits": [...]}."""
    ctx = ctx or {}
    reg = by_id()
    C = _cfg()
    ctrl_ch = int((getattr(C, "SYNTH_VOL", {}) or {}).get("CH", C.CH_CTRL))
    msgs, skipped = [], []
    ordered = sorted(values.items(), key=lambda kv: (reg[kv[0]]["order"] if kv[0] in reg else 99, kv[0]))
    for sid, val in ordered:
        e = reg.get(sid)
        if e is None:
            skipped.append((sid, "unknown setting id"))
            continue
        if not e["sceneable"]:
            skipped.append((sid, "not sceneable: " + (e.get("why") or "")))
            continue
        if val is None:
            skipped.append((sid, "no value"))
            continue
        s = e["set"]
        k = s["kind"]
        try:
            if s.get("skip_if") == "clock_external" and ctx.get("clock_mode") == "external":
                skipped.append((sid, "following an outside clock"))
                continue
            if k == "cc":
                mods = s["module"] if isinstance(s["module"], list) else [s["module"]]
                ch = next((c for c in (channel_of(m, bindings) for m in mods) if c), None)
                if ch is None:
                    skipped.append((sid, f"module {'/'.join(mods)} is not bound on /matrix"))
                    continue
                v = encode(s["enc"], val, ctx) if "enc" in s else int(val)
                msgs.append({"type": "cc", "channel": ch, "cc": s["cc"],
                             "value": max(0, min(127, v)), "id": sid})
            elif k == "bus_cc":
                msgs.append({"type": "cc", "channel": ctrl_ch, "cc": s["cc"],
                             "value": max(0, min(127, int(val))), "id": sid})
            elif k == "ctrl_cc":
                if s["enc"]["type"] == "map":
                    cc, v = s["enc"]["map"][val]
                else:
                    cc, v = s["cc"], encode(s["enc"], val, ctx)
                msgs.append({"type": "ctrl_cc", "cc": cc, "value": v, "id": sid})
            elif k == "ctrl_pc":
                msgs.append({"type": "ctrl_pc", "program": s["base"] + encode(s["enc"], val, ctx),
                             "id": sid})
            elif k == "action":
                if s.get("per_key"):
                    params = (val or {}).get("params", {}) if isinstance(val, dict) else {}
                    req = {"rack_plugin": [s["require"]["rack_plugin"], val.get("plugin")]} \
                        if s.get("require") else None
                    for sym, pv in sorted(params.items()):
                        m = {"type": "action", "cmd": dict(_fill(s["payload"], pv, sym),
                                                           action=s["action"]), "id": sid}
                        if req:
                            m["require"] = req
                        msgs.append(m)
                else:
                    v = encode(s["enc"], val, ctx) if "enc" in s else val
                    msgs.append({"type": "action",
                                 "cmd": dict(_fill(s["payload"], v), action=s["action"]),
                                 "id": sid})
            else:
                skipped.append((sid, f"unknown set kind {k}"))
        except (ValueError, KeyError, TypeError) as ex:
            skipped.append((sid, f"cannot encode {val!r}: {ex}"))
    return msgs, skipped


def summary():
    """{thing-root: count} -- e.g. {"Poly": 13, ...} for a quick look."""
    out = {}
    for e in registry():
        root = e["thing"].split(" > ")[0]
        out[root] = out.get(root, 0) + 1
    return out


if __name__ == "__main__":
    if "--build-cache" in sys.argv[1:]:
        rest = [a for a in sys.argv[1:] if a != "--build-cache"]
        n = build_cache(rest[0] if rest else None)
        print(f"settings registry cache: {n} entries -> {os.path.expanduser(rest[0] if rest else CACHE_FILE)}")
        sys.exit(0)
    for k, v in sorted(summary().items()):
        print(f"{v:4d}  {k}")
    print(f"{len(registry()):4d}  total")
