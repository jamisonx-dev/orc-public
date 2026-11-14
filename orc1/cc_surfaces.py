#!/usr/bin/env python3
"""cc_surfaces -- every MIDI control the ORC listens to that is NOT a plain
`cc == N` branch in a synth handler. Feeds gen_cc_map.py (the /cc page).

Pure stdlib + the ORC's own pure modules (orc1_config, orc1_pedal, orc1_kikpad,
orc1_patterns): no numpy, no mido, no JACK, so it runs on the PC, in the guard
test and on the Pi alike. Everything here is READ OUT of the code (a config
dict, a module constant, an AST/regex scan of a source file); nothing is typed
in as a number that the code also holds.

Each builder returns a list of SECTIONS (plain dicts):
    {"id", "group", "title", "intro", "blocks": [{"title", "headers", "rows",
     "numcols", "pre"}]}
Cells are plain text; gen_cc_map.py escapes them.

COVERAGE (bottom of the file) is the registry the guard test
(_test_cc_map_complete.py) holds the code to: every source file that reacts to
a CC / Program Change / pitch bend must be named there with the page section
that lists it (or a stated reason it is a sender / user data).
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Page groups, in rail order.
G_BRAIN   = "Brain & buses"
G_VOICES  = "Voices"
G_RHYTHM  = "Drums & BeatBuddy"
G_SAMPLE  = "Sampler & visuals"
G_PEDALS  = "Pedals & pads"
G_CTRL    = "Controllers"
G_REF     = "Reference"
GROUPS = [G_BRAIN, G_VOICES, G_RHYTHM, G_SAMPLE, G_PEDALS, G_CTRL, G_REF]


def _read(rel):
    p = os.path.join(HERE, rel)
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _cfg():
    import orc1_config as C
    return C


def _sec(sid, group, title, intro="", blocks=None):
    return {"id": sid, "group": group, "title": title, "intro": intro,
            "blocks": blocks or []}


def _blk(title, headers, rows, numcols=(0,), pre=None):
    return {"title": title, "headers": headers, "rows": rows,
            "numcols": tuple(numcols), "pre": pre}


def count(sec):
    return sum(len(b["rows"]) for b in sec["blocks"])


def _ranges(nums):
    """[36,37,38,40] -> '36-38, 40'"""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Brain: bus levels, bind handshake, channel-13 notes and program changes
# ---------------------------------------------------------------------------

_LEVEL_PAT = re.compile(
    r'^(_(?:VOL|FXMIX)_\w+?_CC)\s*=\s*int\(\s*[\w.]+\.get\(\s*"(\w+)"\s*,\s*(\d+)\s*\)\s*\)',
    re.M)

_LEVEL_LABELS = {
    "MASTER_CC": "Master bus level",
    "POLY_CC": "Poly synth bus level",
    "CHIP_CC": "Chip bus level (all chip voices)",
    "DRUM_CC": "Drum bus level",
    "MONO_CC": "Mono synth bus level",
    "BBKIT_VOL_CC": "BBKit (sampled BeatBuddy kit) bus level",
    "FXMIX_MIX_CC": "FX mixer: wet/dry",
    "FXMIX_LEVEL_CC": "FX mixer: return level (unity at 64, up to 2x)",
}
_LEVEL_FILES = ("orc1_synth.py", "orc1_bbkit_host.py", "orc1_fxmix.py",
                "orc1_poly_host.py", "orc1_chip_host.py", "orc1_mono_host.py",
                "orc1_drum_host.py", "orc1_sampler_host.py")


def bus_levels(C):
    """[(cc, label, key)] -- every `_VOL_*_CC` / `_FXMIX_*_CC` the code defines,
    resolved through the config exactly as the code does."""
    cfgs = [getattr(C, "SYNTH_VOL", {}) or {}, getattr(C, "SYNTH", {}) or {}]
    out, seen = [], set()
    for fn in _LEVEL_FILES:
        for name, key, default in _LEVEL_PAT.findall(_read(fn)):
            if key in seen:
                continue
            seen.add(key)
            val = int(default)
            for d in cfgs:
                if key in d:
                    val = int(d[key])
                    break
            out.append((val, _LEVEL_LABELS.get(key, key.replace("_", " ").title()), key))
    return sorted(out)


def module_ids(C):
    return list(getattr(C, "MODULE_KEYS", []))


def brain_sections():
    C = _cfg()
    ch = C.CH_CTRL
    secs = []

    lv = bus_levels(C)
    secs.append(_sec(
        "bus", G_BRAIN, f"Bus levels (channel {ch})",
        "CCs on the control channel of the internal synth bus. The Fire faders and "
        "the web LEVELS sliders send exactly these. Value 0-127.",
        [_blk("", ["CC", "Control"], [[str(cc), label] for cc, label, _k in lv])]))

    mods = module_ids(C)
    arm, clr = C.BIND_ARM_CC, C.BIND_CLEAR_CC
    secs.append(_sec(
        "bind", G_BRAIN, f"Module bind handshake (channel {ch})",
        "How /matrix moves a module to a channel. The brain emits it on the internal bus "
        "only (a controller's channel 13 is never forwarded there), so a controller "
        "cannot spoof it. The value is the module's id (its position in the list below).",
        [_blk("Messages", ["Number", "Message", "Meaning"], [
            [str(arm), "CC", "ARM: the next note-on's channel becomes the target of the module id in the value"],
            [str(clr), "CC", "CLEAR: take the module id in the value out of the channel map"],
            [str(C.BIND_COMMIT_NOTE), "Note-on", "COMMIT: the note that completes an armed bind"]]),
         _blk("Module ids", ["Id", "Module"], [[str(i), m] for i, m in enumerate(mods)])]))

    def notes(d):
        by = {}
        for n, act in d.items():
            by.setdefault(act, []).append(n)
        return [[_ranges(v), str(k)] for k, v in sorted(by.items(), key=lambda kv: min(kv[1]))]

    blocks = [
        _blk("Chord base type", ["Note", "Sets"], [[str(n), a] for n, a in sorted(C.CTRL_BASE_NOTES.items())]),
        _blk("Extensions, chromatic (toggle)", ["Note", "Toggles"],
             [[str(n), a] for n, a in sorted(C.CTRL_EXT_NOTES.items())]),
        _blk("Extensions, scale degree (toggle)", ["Note", "Toggles"],
             [[str(n), a] for n, a in sorted(getattr(C, "CTRL_EXT_DEGREE_NOTES", {}).items())]),
        _blk("Chord triggers (ROOT mode)", ["Notes", "Plays"],
             [[_ranges(C.CTRL_TRIGGER_NOTES.keys()),
               "Chord on that note's degree (" + str(len(C.CTRL_TRIGGER_NOTES)) + " notes)"]]),
        _blk("Modifiers", ["Note", "Action"], [[str(n), a] for n, a in sorted(C.CTRL_MOD_NOTES.items())]),
        _blk("Function key", ["Note", "Does"],
             [[str(C.FUNC_KEY_NOTE), "Hold: CCs route through the function map (Brain control section) instead"]]),
    ]
    secs.append(_sec("ctrlnotes", G_BRAIN, f"Control notes (channel {ch})",
                     "Note-ons on the control channel: the chord engine's pads. "
                     "Read from orc1_config.", blocks))

    pcs = [[f"{C.SCALE_PC_OFFSET}+", "Scale select: PC n picks the n-th scale in the brain's scale order"],
           [f"{C.ARP_PC_OFFSET}+", "Arpeggiator pattern: PC picks the n-th pattern (orc1_patterns.PATTERN_ORDER)"]]
    pcs += [[str(n), "Chord-engine preset " + name] for n, name in sorted(C.PC_PRESET_MAP.items())]
    secs.append(_sec("ctrlpc", G_BRAIN, f"Program change (channel {ch})",
                     "Program changes on the control channel.",
                     [_blk("", ["PC", "Selects"], pcs)]))

    sc = getattr(C, "SCENE_RECALL_CC", None)
    if sc is not None:
        secs.append(_sec(
            "scenes", G_BRAIN, f"Scene recall (channel {ch})",
            "A CC on the control channel recalls a scene (orc1_scenes): the value picks the "
            "scene, and it lands with that scene's own timing (next bar unless set otherwise). "
            "A pad or FCB switch can do the same with the action <code>scene:N</code>.",
            [_blk("", ["CC", "Value", "Does"],
                  [[str(sc), "1-127", "Recall scene N = the value (0 is ignored: a button's release recalls nothing)"]])]))
    return secs


# ---------------------------------------------------------------------------
# Visuals, Zoom pedal
# ---------------------------------------------------------------------------

def _dict_with_comments(src, name):
    """{key: (value, comment)} for a module-level literal dict `name = {...}`
    whose values are strings (the comment is the text after # on the same line)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    lines = src.splitlines()
    for st in tree.body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                              for t in st.targets) \
                and isinstance(st.value, ast.Dict):
            out = {}
            for k, v in zip(st.value.keys, st.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, int) \
                        and isinstance(v, ast.Constant):
                    line = lines[k.lineno - 1]
                    m = re.search(r"#\s*(.*)$", line)
                    out[k.value] = (v.value, m.group(1).strip() if m else "")
            return out
    return {}


def _int_const(src, name):
    m = re.search(rf"^{name}\s*=\s*(\d+)", src, re.M)
    return int(m.group(1)) if m else None


def visuals_section():
    src = _read(os.path.join("daw", "orc_visuals.py"))
    knobs = _dict_with_comments(src, "KNOBS")
    scene = _int_const(src, "SCENE_CC")
    rows = []
    if scene is not None:
        rows.append([str(scene), "scene", "0 = cycle; otherwise picks a scene by value range"])
    rows += [[str(cc), v, c] for cc, (v, c) in sorted(knobs.items())]
    intro = ("The HDMI visuals renderer reads CCs on whatever channel the <b>visuals</b> module is "
             "bound to on /matrix (unbound = it ignores them). Any controller, a Fire knob naming "
             "\"visuals\", the /visuals page and the /system VISUALS card all send these.")
    return [_sec("visuals", G_SAMPLE, "Visuals", intro,
                 [_blk("", ["CC", "Knob", "Meaning"], rows)])]


def mxvbox_section():
    """The MXV box visuals module: its knob table lives in mxvbox/pi/mxvbox_knobs.py
    (pure data), read here so the page and the conductor can never disagree."""
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mxvbox", "pi", "mxvbox_knobs.py")
        spec = importlib.util.spec_from_file_location("mxvbox_knobs", p)
        M = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(M)
        knobs = M.KNOBS
    except Exception as e:                      # keep the page honest about a failed read
        return [_sec("mxvbox", G_VOICES, "MXV box visuals (mxvbox)",
                     f"Could not read mxvbox_knobs.py: {type(e).__name__}: {e}")]
    rows = [[str(cc), name, f"{what} (default {d})"] for cc, (name, d, what) in sorted(knobs.items())]
    return [_sec("mxvbox", G_VOICES, "MXV box visuals (mxvbox)",
                 "The old Android box on the Pi's eth0, drawing beat-locked visuals. CCs on the channel "
                 "the <b>mxvbox</b> module is bound to (/matrix); the conductor (service orc1-boxvis) "
                 "saves them in ~/.orc1_mxvbox.json. Kick / snare hits come from the drums and bbkit "
                 "channels by name. Notes on its channel: note N cues clip N for the next clip bar.",
                 [_blk("Control changes", ["CC", "Knob", "Meaning"], rows)])]


def daw_section():
    """The ORC as a DAW control surface (module "daw"): the CC -> Mackie strip table lives in
    orc1_config (DAWSURF_FADER_CC / DAWSURF_BANK_CC), read here so the page can't drift."""
    C = _cfg()
    faders = getattr(C, "DAWSURF_FADER_CC", {})
    banks = getattr(C, "DAWSURF_BANK_CC", {})
    rows = [[str(cc), "master fader" if s == "master" else f"strip {s + 1} fader",
             "0-127 = the DAW fader, both ways (the DAW's moves come back as this CC)"]
            for cc, s in sorted(faders.items())]
    rows += [[str(cc), "bank " + ("left" if d < 0 else "right"),
              "once each time the value rises through 64"] for cc, d in sorted(banks.items())]
    rows += [[str(cc), f"{v} view: shared / independent",
              "toggles on the rise through 64 (independent = its own window of tracks; needs the OSC dialect)"]
             for cc, v in sorted(getattr(C, "DAWSURF_VIEW_CC", {}).items())]
    notes = getattr(C, "DAWSURF_BANK_NOTES", {})
    nrows = [[str(n), f"bank {b + 1}", f"jump the bsp view to tracks {8 * b + 1}-{8 * b + 8}"]
             for n, b in sorted(notes.items())]
    return [_sec("daw", G_CTRL, "DAW control surface (daw)",
                 "CCs on the channel the <b>daw</b> module is bound to (/matrix). The bridge (service "
                 "orc1-dawsurf, orc1_dawsurf_bridge.py) plays them into the DAW as a Mackie Control "
                 "surface over the network-MIDI port named " + ", ".join(getattr(C, "DAWSURF_PORT_NAMES", []))
                 + " (Mackie) or to REAPER's OSC surface (pattern file orc1/daw/ORC.ReaperOSC), and sends the "
                 "DAW's fader moves back to the Fire as the same CCs. These are the SHIPPED bindings: any CC or "
                 "note can run any DAW action (fader, pan, mute, solo, rec, select, transport, bank, shift, "
                 "goto_bank, view_mode) through ~/.orc1_dawsurf_bindings.json (format in orc1_dawsurf_bind.py).",
                 [_blk("Control changes", ["CC", "Control", "Meaning"], rows),
                  _blk("Notes (bank pads)", ["Note", "Control", "Meaning"], nrows)])]


def zoomfx_section():
    try:
        import orc1_zoomfx as Z
        bases, tun, tun2, sto = Z.SLOT_BASES, Z.CC_TUNER, Z.CC_TUNER2, Z.CC_STORE
    except Exception as e:                      # keep the page honest about a failed read
        return [_sec("zoomfx", G_VOICES, "Zoom MultiStomp (zoomfx)",
                     f"Could not read orc1_zoomfx: {type(e).__name__}: {e}")]
    rows = []
    for i, b in enumerate(bases):
        rows.append([f"{b}", f"Slot {i + 1}", "On/off (value 64+ = on)"])
        rows.append([f"{b + 1}-{b + 9}", f"Slot {i + 1}", "Knobs 1-9, scaled to each effect's real range"])
    rows += [[str(tun), "Tuner", "64+ on (pedal CC 74)"],
             [str(tun2), "Tuner, other mode", "64+ on (pedal CC 75)"],
             [str(sto), "Store", "Fires once on the rise through 64: stores the current patch to its own slot"]]
    doc = Z.__doc__ or ""
    m = re.search(r"program change (\d+)-(\d+) = patch (\d+)-(\d+)", doc)
    pc = (f"{m.group(1)}-{m.group(2)}", f"Patch {m.group(3)}-{m.group(4)} on the pedal") if m \
        else ("?", "Patch select (see orc1_zoomfx.py)")
    return [_sec("zoomfx", G_VOICES, "Zoom MultiStomp (zoomfx)",
                 "A pedal driven by plain CCs on the channel the <b>zoomfx</b> module is bound to "
                 "(/matrix). No audio; orc1_zoomfx.py turns these into the pedal's SysEx. "
                 "Slots 4-6 need the 6SLOT-EDIT firmware patch.",
                 [_blk("Control changes", ["CC", "Target", "Meaning"], rows),
                  _blk("Program change", ["PC", "Selects"], [list(pc)])])]


# ---------------------------------------------------------------------------
# BeatBuddy, drums, command socket
# ---------------------------------------------------------------------------

_ACTION_PAT = re.compile(r'\baction\s*==\s*"([A-Z0-9_]+)"')

# Plain-words labels for actions whose code carries no comment. The page falls
# back to the code's own comment, then to nothing: a missing label is never wrong.
ACTION_LABELS = {
    "BB_SONG_SELECT": "Load a song file by name",
    "BB_KIT_SELECT": "Select a kit (program number)",
    "BB_OUTPUT": "Where the BeatBuddy plays: 909 drums, BBKit, or a MIDI port + channel",
    "BB_MIX": "Per-drum level (0 = mute); group \"reset\" restores all",
    "BB_STOP": "Stop the groove",
    "BB_NEXT": "One-shot transition to the next part (fill plays, then advances)",
    "BB_TEMPO_MODE": "mode \"song\" = loading a song sets the ORC tempo; \"clock\" = keep the current tempo",
    "BB_MATCH_BPM": "Snap the ORC clock to the loaded song's bpm (no-op under external clock)",
    "SCENE_CAPTURE": "Store the current value of each ticked setting in scene slot N",
    "SCENE_RECALL": "Recall scene N (timing: IMMEDIATE / NEXT_BEAT / NEXT_BAR / BARS_2 / BARS_4)",
    "SCENE_SET": "Edit scene N by hand: name, default timing, values",
    "SCENE_CLEAR": "Empty scene slot N",
    "SCENE_LIST": "Read back every scene + the pending recall (the reply carries the data)",
    "SETTINGS_READ": "Read live brain values of settings (tempo, harmony, arp, Fire pattern)",
    "FIRE_PATTERN": "Queue a pattern on our Fire firmware (v0.30+): it switches at its next bar",
}


def cmd_actions(src=None):
    """{name: comment} for every `action == "NAME"` in the brain's command
    handler. The comment is the trailing # on that line or on the next one."""
    src = src if src is not None else _read("orc1.py")
    lines = src.splitlines()
    out = {}
    for i, line in enumerate(lines):
        for name in _ACTION_PAT.findall(line):
            m = re.search(r"#\s*(.*)$", line)
            if not m and i + 1 < len(lines):
                m = re.search(r"#\s*(.*)$", lines[i + 1])
            out.setdefault(name, ACTION_LABELS.get(name) or (m.group(1).strip() if m else ""))
    return out


def beatbuddy_section():
    C = _cfg()
    acts = cmd_actions()
    bb = {k: v for k, v in acts.items() if k.startswith("BB_")}
    try:
        import orc1_pedal as P
        gest = sorted(P.DISCRETE)
    except Exception:
        gest = []
    rows_a = [[k, v or ""] for k, v in sorted(bb.items())]
    t909 = getattr(C, "BB_TO_909", {})
    by = {}
    for bbn, tgt in t909.items():
        by.setdefault(tgt, []).append(bbn)
    rows_m = [[str(t), _ranges(v)] for t, v in sorted(by.items())]
    host = _read("orc1_bbkit_host.py")
    has_pc = "program_change" in host
    blocks = [
        _blk("Command-socket actions", ["Action", "What it does"], rows_a, numcols=()),
        _blk("Pedal / pad gestures (discrete)", ["Gesture"], [[g] for g in gest], numcols=()),
        _blk("MIDI into the BBKit module (the sampled kit)", ["Message", "Meaning"], [
            ["Note-on", "Triggers that note's articulation from the kit (the kit's own note layout)"],
            ["Program change", "Select kit n" if has_pc else "(not handled)"]],
             numcols=()),
        _blk("BeatBuddy note -> 909 voice (when the BeatBuddy plays the ORC drums)",
             ["909 note", "BeatBuddy notes folded onto it"], rows_m),
    ]
    shape = getattr(C, "BBKIT_SHAPE_CC", {})
    if shape:
        sdef = getattr(C, "BBKIT_SHAPE_DEFAULT", {})
        notes = {"note": "Pick the drum (its note number) the per-drum CCs below edit",
                 "level": "Picked drum: level", "tune": "Picked drum: tune (+/- 12 semitones)",
                 "attack": "Picked drum: fade-in (0 - 200 ms)",
                 "decay": "Picked drum: decay (127 = natural length)",
                 "pan": "Picked drum: pan", "cutoff": "Picked drum: low-pass cutoff (127 = open)",
                 "reso": "Picked drum: resonance", "kit_tune": "Whole kit: tune",
                 "kit_cutoff": "Whole kit: low-pass cutoff"}
        blocks.insert(3, _blk("BBKit per-drum shaping (CCs on the bbkit channel; saved per kit)",
                              ["CC", "What it does", "Default"],
                              [[str(c), notes.get(k, k), "" if k == "note" else str(sdef.get(k, ""))]
                               for k, c in sorted(shape.items(), key=lambda kv: kv[1])]))
    return [_sec("beatbuddy", G_RHYTHM, "BeatBuddy",
                 "The BeatBuddy is an ORC module, not a MIDI device: <b>there is no CC for it</b>. "
                 "It is driven by command-socket actions, which the web page, the FCB1010 pedal "
                 "(<code>cmd:NAME</code>), pad maps and the KIKPAD can all fire. Its output goes "
                 "to the 909 drums, the BBKit module, or a MIDI port; the BBKit bus level is in "
                 "<a href=\"#bus\">Bus levels</a>.", blocks)]


def actions_section():
    acts = cmd_actions()
    groups = {}
    for name, c in acts.items():
        groups.setdefault(name.split("_")[0], []).append((name, c))
    rows = []
    for g in sorted(groups):
        names = sorted(groups[g])
        rows.append([g, str(len(names)), ", ".join(n for n, _ in names)])
    prefixes = sorted(set(re.findall(r'action\.startswith\("([A-Z0-9_]+)"\)', _read("orc1.py"))))
    pre_rows = [[p + "*", "Handled by prefix"] for p in prefixes]
    blocks = [_blk("Actions by family", ["Family", "Count", "Actions"], rows, numcols=())]
    if pre_rows:
        blocks.append(_blk("Prefix actions", ["Pattern", ""], pre_rows, numcols=()))
    return [_sec("actions", G_REF, "Command-socket actions",
                 f"{len(acts)} actions the brain accepts. Not MIDI: they arrive from the web pages, and "
                 "a pedal switch or pad can fire any of them as <code>cmd:NAME</code>.", blocks)]


# ---------------------------------------------------------------------------
# Pedal, pad maps, KIKPAD
# ---------------------------------------------------------------------------

def pedal_section():
    try:
        import orc1_pedal as P
    except Exception as e:
        return [_sec("pedal", G_PEDALS, "FCB1010 pedal", f"Could not read orc1_pedal: {e}")]
    pf = P.FCB_EF_PROFILE
    ch = pf["channel"] + 1
    ef = [[str(cc), f"Switch {sw}"] for cc, sw in sorted(pf["switch_cc"].items(), key=lambda kv: kv[1])]
    ef += [[str(cc), f"Expression {n}"] for cc, n in sorted(pf["exp_cc"].items())]
    sp = [[str(n), f"Switch {sw}"] for n, sw in sorted(pf["switch_note"].items(), key=lambda kv: kv[1])]
    mp = P.DEFAULT_FCB_MAP

    def pages(mode):
        rows = []
        m = mp.get(mode, {})
        if "all" in m:
            rows.append(["every page", ", ".join(f"{k}: {v}" for k, v in sorted(m["all"].items()))])
        for i, pg in enumerate(m.get("pages", [])):
            rows.append([f"page {i + 1}", ", ".join(f"{k}: {v}" for k, v in sorted(pg.items())) or "(empty)"])
        return rows
    act_rows = []
    for mode in ("ef", "notes"):
        for pg, what in pages(mode):
            act_rows.append([mode.upper() if mode == "ef" else "SP", pg, what])
    for chn, what in sorted(mp.get("pr", {}).items()):
        act_rows.append(["Pr", f"channel {chn}", what])
    blocks = [
        _blk(f"EF mode (MIDI channel {ch}; toggles, any value = one press)",
             ["CC", "Control"], ef),
        _blk("SP mode (note-on at press, note-off at release)", ["Note", "Control"], sp),
        _blk("Pr mode (dial-a-number)", ["Number", "Meaning"], [
            [str(pf["pr_bank_cc"]), f"Bank prefix CC, then a Program Change on the dialled channel; "
                                    f"the dialled channel picks the action (below)"]]),
        _blk("Default switch actions (editable on /pedal)", ["Mode", "Page", "Switch: action"],
             act_rows, numcols=()),
    ]
    return [_sec("pedal", G_PEDALS, "FCB1010 pedal",
                 "Recognised by USB name (CH345 adapter / \"fcb\"). Everything it sends is a "
                 "whitelist: anything else from that port is dropped. Actions: BeatBuddy gestures, "
                 "<code>chord:N</code>, <code>cmd:ACTION</code>, <code>rc505:NAME</code>, "
                 "<code>page:up/down</code>. The live assignment is in ~/.orc1_pedal.json.",
                 blocks)]


def padmap_section():
    try:
        import orc1_padmap as M
        import orc1_pedal as P
    except Exception as e:
        return [_sec("padmap", G_PEDALS, "Pad maps", f"Could not read orc1_padmap: {e}")]
    doc = M.__doc__ or ""
    m = re.search(r"A pad entry.*?\n\n", doc, re.S)
    pre = m.group(0).strip() if m else None
    acts = sorted(P.DISCRETE)
    blocks = [
        _blk("Entry types", ["do", "Effect"], [
            ["pass", "Unchanged (same as no entry)"], ["ignore", "Swallowed"],
            ["note", "Play a note on a NAMED module (resolved from /matrix at hit time)"],
            ["cc", "Send a CC to a named module (CC pads only)"],
            ["action", "Run an action (the pedal's vocabulary, press-only)"]], numcols=()),
        _blk("Action vocabulary", ["Action"],
             [[a] for a in acts] + [["chord:N / chord:stop"], ["cmd:ACTION (any command-socket action)"],
                                    ["rc505:NAME / rc505:NAME:hold"]], numcols=()),
    ]
    return [_sec("padmap", G_PEDALS, "Pad maps (any drum brain / pad device)",
                 "Not fixed numbers: each device's pads and CCs are mapped on <b>/pads</b> "
                 "(~/.orc1_padmap.json) and matched by the device's name. Message shapes: "
                 "<code>note:N</code> and <code>cc:N</code> keys.", blocks)]


def kikpad_section():
    try:
        import orc1_kikpad as K
    except Exception as e:
        return [_sec("kikpad", G_CTRL, "KIKPAD", f"Could not read orc1_kikpad: {e}")]
    doc = K.__doc__ or ""
    wire = re.search(r"Wire \(.*?\n\n", doc, re.S)
    layout = re.search(r"LAYOUT.*?\n\n", doc, re.S)
    enc = [[str(i), name, ("per deck" if pd else "global")]
           for i, (name, pd, _s, _st) in enumerate(K.ENCODERS)]
    blocks = [
        _blk("Bank 2 (host surface), port :1, channel " + str(K.CH + 1), ["Number", "Message", "Meaning"], [
            ["0-63", "Note", "Pad press / release"],
            [f"{K.BTN_BASE}-{K.BTN_BASE + 23}", "Note", "Buttons (MS1 = 64, SET = shift)"],
            ["0-7", "CC", "Encoders (65 = +1, 63 = -1)"],
            [str(K.SHOWN_CC), "CC", "1 = bank shown, 0 = left"]]),
        _blk("Encoders on the sampler page", ["CC", "Control", "Scope"], enc, numcols=(0,)),
    ]
    if wire:
        blocks.append(_blk("The pad's own wire description", ["Spec"], [], numcols=(), pre=wire.group(0).strip()))
    if layout:
        blocks.append(_blk("Layout", ["Spec"], [], numcols=(), pre=layout.group(0).strip()))
    return [_sec("kikpad", G_CTRL, "KIKPAD (Midiplus SmartPad)",
                 "Banks 0 and 1 and the encoders are maps stored ON the pad (edit on <b>/kikpad</b>). "
                 "Bank 2 is a surface the ORC owns: it reports presses on a second USB-MIDI port, claimed "
                 "by name: the sampler decks + skipback.", blocks)]


# ---------------------------------------------------------------------------
# Fire buttons, message census
# ---------------------------------------------------------------------------

def knobfocus_section():
    try:
        import orc1_knobfocus as KF
        import orc1_mcu as MC
    except Exception as e:
        return [_sec("knobs", G_CTRL, "Follow knobs", f"Could not read orc1_knobfocus: {e}")]
    d = KF.DEFAULTS
    ccs = d["ccs"]
    rows = []
    for i, c in enumerate(ccs):
        row = "top" if i < 8 else "bottom"
        rows.append([str(c), f"Knob {i + 1} ({row} row)",
                     " / ".join(f"{m}: {p[i][1]}" for m, p in sorted(KF.DEFAULT_PAGES.items())
                                if m in ("poly", "mono", "chip_p1") and p[i]) or "(empty on synths)"])
    drum = [[f"Knob {i + 1}", f"focused drum voice: {p.replace('_', ' ')}"]
            for i, p in enumerate(KF.DRUM_FOCUS_PARAMS)]
    drum += [[f"Knob {9 + i}", f"{v.replace('_', ' ')} level"] for i, v in enumerate(KF.DRUM_LEVEL_ROW)]
    mcu = [
        [f"{MC.VPOT_CC0}-{MC.VPOT_CC0 + 7}", "CC, ch 1", "V-pot 1-8 turn (relative, sign-magnitude) = knobs 1-8"],
        ["pitch bend", "ch 1-8", "Fader 1-8 = knobs 9-16 (absolute); ch 9 = master fader"],
        [str(MC.JOG_CC), "CC, ch 1", "Jog wheel (decoded, not used yet)"],
        [f"{MC.VPOT_RING_CC0}-{MC.VPOT_RING_CC0 + 7}", "CC out, ch 1", "V-pot LED rings (encode only)"],
        ["0-31", "Note, ch 1", "Rec / solo / mute / select x8 (decoded)"],
        ["91-95", "Note, ch 1", "Rewind, fast fwd, stop, play, record (decoded)"],
    ]
    blocks = [
        _blk(f"Default: device '{d['device']}', channel {d['channel']}, {d['encoding']} encoders",
             ["CC", "Knob", "What it moves (synth pages)"], rows),
        _blk("Drums page (the drum voice you last hit)", ["Knob", "Moves"], drum, numcols=()),
        _blk("Mackie Control (MCU) input, ports listed in mcu_ports", ["Number", "Message", "Meaning"],
             mcu, numcols=()),
    ]
    return [_sec("knobs", G_CTRL, "Follow knobs (BeatStep Pro encoders, any MCU surface)",
                 "Not fixed parameters: the 16 knobs move the parameters of the module <b>in focus</b> "
                 "= the voice being played (a module takes focus when it plays after being quiet; "
                 "never while a knob is being turned), or a locked one. Each knob moves the module's own "
                 "CC from its current value. Config: <code>~/.orc1_knobfocus.json</code>, socket actions "
                 "KNOBS_STATUS / KNOBS_SET. HUI is not decoded yet.", blocks)]


def fire_buttons_section():
    src = _read("orc1_fire.py")
    m = re.search(r"^BUTTON_NOTE\s*=\s*\{(.*?)\}", src, re.S | re.M)
    pairs = re.findall(r'"(\w+)"\s*:\s*(\d+)', m.group(1)) if m else []
    base = _int_const(src, "PAD_NOTE_BASE")
    mx = _int_const(src, "PAD_NOTE_MAX")
    rows = [[n, name] for name, n in sorted(pairs, key=lambda p: int(p[1]))]
    touch = re.search(r"^DIAL_TOUCH_NOTE\s*=\s*\{([^}]*)\}", src, re.M)
    if touch:
        rows.append([_ranges(int(x) for x in re.findall(r"\d+", touch.group(1))), "Knob touch (1-4)"])
    if base is not None and mx is not None:
        rows.append([f"{base}-{mx}", "Pads (row-major, 16 columns x 4 rows)"])
    return [_sec("firebuttons", G_CTRL, "Akai Fire: buttons and pads",
                 "The note numbers the Fire sends for its buttons and pads. What a button <i>does</i> "
                 "depends on the view and modifier layer; that is on /control and not enumerated here.",
                 [_blk("", ["Note", "Control"], rows)])]


_CENSUS_FILES = [
    ("orc1.py", "Brain (control channel, routing)"),
    ("orc1_drum_host.py", "Drum host"), ("orc1_poly_host.py", "Poly host"),
    ("orc1_chip_host.py", "Chip host"), ("orc1_mono_host.py", "Mono host"),
    ("orc1_sampler_host.py", "Sampler host"), ("orc1_bbkit_host.py", "BBKit host"),
    ("orc1_fxmix.py", "FX mixer"), ("orc1_zoomfx.py", "Zoom pedal"),
    (os.path.join("daw", "orc_visuals.py"), "Visuals renderer"),
    ("orc1_fire.py", "Fire handler"), ("orc1_mixtrack.py", "Mixtrack handler"),
    ("orc1_kikpad.py", "KIKPAD surface"), ("orc1_padmap.py", "Pad maps"),
    ("orc1_pedal.py", "FCB1010 pedal"),
]


def census_section():
    pats = (("control_change", "CC"), ("program_change", "PC"),
            ("pitchwheel", "Pitch bend"), ("polytouch", "Poly AT"), ("aftertouch", "Aftertouch"))
    rows = []
    for fn, label in _CENSUS_FILES:
        src = _read(fn)
        if not src:
            continue
        flags = ["yes" if re.search(rf"['\"]{p}['\"]", src) else "" for p, _ in pats]
        rows.append([label, fn.replace(os.sep, "/")] + flags)
    return [_sec("census", G_REF, "Which parts listen to which message types",
                 "Found by scanning each source file for the message type, so a part that starts "
                 "handling program change or pitch bend shows up here automatically.",
                 [_blk("", ["Part", "File"] + [n for _, n in pats], rows, numcols=())])]


def unlisted_section():
    return [_sec("unlisted", G_REF, "What this page does not enumerate",
                 "Stated plainly, so nothing here is mistaken for complete when it is not:",
                 [_blk("", ["Item", "Why / where to look"], [
                     ["Fire button functions per view and modifier", "Depends on view state; see /control and the Fire section of the manual"],
                     ["Routing rules and the input matrix", "User data (/routes, /matrix); rules are not fixed numbers"],
                     ["Pad maps and the pedal's live assignment", "User data (~/.orc1_padmap.json, ~/.orc1_pedal.json)"],
                     ["KIKPAD banks 0 and 1, encoders", "Stored on the pad; read back and edited on /kikpad"],
                     ["RC-505 assign map and Perform-VE CCs", "The ORC SENDS these (outputs); the map is rc505_config.json, the CC chart is in the Perform-VE manual"],
                     ["Sampler, drums, poly, mono, chip, Mixtrack, Fire knobs", "Listed on this page, from the live tables and handlers"]],
                       numcols=())])]


def all_sections():
    secs = []
    secs += brain_sections()
    secs += zoomfx_section()
    secs += mxvbox_section()
    secs += daw_section()
    secs += beatbuddy_section()
    secs += visuals_section()
    secs += pedal_section()
    secs += padmap_section()
    secs += kikpad_section()
    secs += knobfocus_section()
    secs += fire_buttons_section()
    secs += actions_section()
    secs += census_section()
    secs += unlisted_section()
    return secs


# ---------------------------------------------------------------------------
# COVERAGE: the guard test's contract. file -> (section ids that list it, why)
# A file whose code reacts to CC / PC / pitch bend and is NOT here fails the
# guard test, so adding a handler forces an entry (and so a look at the page).
# Section ids may name sections gen_cc_map.py builds itself (channels, ctrl,
# handlers, drums, sampler, mixtrack, fire, web).
# ---------------------------------------------------------------------------
COVERAGE = {
    "orc1.py":              (("bus", "bind", "ctrl", "ctrlnotes", "ctrlpc", "scenes", "actions", "beatbuddy"),
                             "control channel dispatch; command socket"),
    "orc1_synth.py":        (("handlers", "drums", "sampler", "bus", "channels"), "engine CC tables and handlers"),
    "orc1_drum_host.py":    (("drums", "bus", "bind", "global"), "drum host"),
    "orc1_poly_host.py":    (("handlers", "bus", "bind", "global"), "poly host"),
    "orc1_chip_host.py":    (("handlers", "bus", "bind", "global"), "chip host"),
    "orc1_mono_host.py":    (("handlers", "bus", "bind", "global"), "mono host"),
    "orc1_sampler_host.py": (("sampler", "bus", "bind", "global"), "sampler host"),
    "orc1_bbkit_host.py":   (("beatbuddy", "bus", "bind", "global"), "BBKit host"),
    "orc1_fxmix.py":        (("bus",), "FX mixer reads its two bus CCs"),
    "orc1_zoomfx.py":       (("zoomfx",), "Zoom pedal module"),
    "daw/orc_visuals.py":   (("visuals",), "visuals renderer"),
    "../mxvbox/pi/conductor.py": (("mxvbox",), "MXV box visuals conductor (knobs in mxvbox_knobs.py)"),
    "orc1_dawsurf_bridge.py": (("daw",), "DAW control surface bridge (CC map in orc1_config.DAWSURF_*)"),
    "orc1_dawsurf_bind.py": (("daw",), "DAW surface binding table: shipped defaults from orc1_config.DAWSURF_*, "
                                       "user bindings in ~/.orc1_dawsurf_bindings.json"),
    "orc1_fire.py":         (("fire", "firebuttons"), "Fire handler"),
    "orc1_mixtrack.py":     (("mixtrack",), "Mixtrack handler"),
    "orc1_kikpad.py":       (("kikpad",), "KIKPAD bank 2 surface"),
    "orc1_padmap.py":       (("padmap",), "pad maps (user data)"),
    "orc1_knobfocus.py":    (("knobs",), "follow knobs: claims a device's knob CCs, moves the focused module"),
    "orc1_mcu.py":          (("knobs",), "Mackie Control codec (V-pots, faders, buttons, LCD)"),
    "orc1_pedal.py":        (("pedal",), "FCB1010"),
    "orc1_routes.py":       (("unlisted",), "user routing rules, not fixed numbers"),
    "orc1_cme.py":          (("unlisted",), "CME patchbay: passes traffic, defines no controls"),
    "orc1_rc505.py":        (("unlisted",), "OUTPUT driver: the ORC sends to the RC-505"),
    "orc1_autosampler.py":  (("unlisted",), "OUTPUT: sends to an external sampler"),
    "orc1_kikpad_link.py":  (("kikpad",), "OUTPUT: edits the pad's maps"),
    "orc1_settings_registry.py": (("unlisted",), "the scenes' settings list: reads the code's own CC "
                                                 "tables, reacts to no MIDI itself"),
    "orc1_web.py":          (("web",), "web pages SEND CCs"),
}

# Names a CC comparison may use without a literal number, with where the page
# lists them. A new `cc == _FOO_CC` that is not here fails the guard test.
HANDLED_NAMES = {
    "_BIND_ARM_CC": "bind", "_BIND_CLEAR_CC": "bind",
    "_VOL_DRUM_CC": "bus", "_VOL_POLY_CC": "bus", "_VOL_CHIP_CC": "bus",
    "_VOL_MONO_CC": "bus", "_VOL_MASTER_CC": "bus", "_VOL_BBKIT_CC": "bus",
    "_ENGINE_MASTER_CC": "handlers",
    "SCENE_CC": "visuals", "KNOBS": "visuals", "SHOWN_CC": "kikpad",
    "_CHIP_PERSIST_SKIP": "handlers",     # persistence filter, not a control
    "CC_TUNER": "zoomfx", "CC_TUNER2": "zoomfx", "CC_STORE": "zoomfx",
    "mix_cc": "bus", "level_cc": "bus",
}
