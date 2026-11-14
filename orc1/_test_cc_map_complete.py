#!/usr/bin/env python3
"""Guard: the /cc page must list EVERY MIDI control the code reacts to.

Added 2026-10-19 after owner found /cc missing BeatBuddy "and a good bit of
other stuff": the generator only knew the handlers it had been taught, so each
new surface (module bus levels, the Zoom pedal, visuals, the FCB, the KIKPAD)
silently went unlisted. This test is the other half: it scans the CODE for
anything that dispatches on a CC / Program Change / pitch bend and fails unless
the generator lists it.

 1. every source file that dispatches on MIDI is registered in
    cc_surfaces.COVERAGE (add a new host/controller there, with its section);
 2. every section id COVERAGE names exists on the generated page;
 3. every named CC constant a handler compares against is in HANDLED_NAMES;
 4. every literal CC number a registered file compares against is on the page;
 5. every bus-level CC the code defines is listed; every command-socket action,
    every visuals knob, every Fire button note, every FCB switch is listed.

Pure stdlib (+ the pure ORC modules cc_surfaces imports); runs on the PC or the Pi.
    python3 _test_cc_map_complete.py
"""
import ast
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cc_surfaces as X
import gen_cc_map as G

EXPECTED = 146                 # +1 2026-11-09: orc1_dawsurf_bind's COVERAGE entry (its ids exist)
_PREV_PREV_PREV_EXPECTED = 145  # +3 2026-11-06: orc1_dawsurf_bridge registered + its COVERAGE ids + the daw section
_PREV_EXPECTED = 142                 # +5 2026-11-04: orc1_knobfocus + orc1_mcu registered (2 files x 2) + the knobs section
_PREV_PREV_EXPECTED = 137                 # +2 2026-10-29: the mxvbox module's COVERAGE entry + its section
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


# --- the page, built exactly as the generator builds it ----------------------
live = G.live_tables()
live["constructed"] = G.constructed_classes(G._engine_sources())
handlers = G.scan_handlers(G.SOURCES["synth"])
page = G.build(live, handlers, G.scan_consts(G.SOURCES["mixtrack"]),
               G.scan_web_senders(HERE), fire=G.fire_inputs(G.SOURCES["fire"]))
page_ids = set(re.findall(r'<details class="cc-sec" id="([\w]+)"', page))
cells = re.findall(r"<td class='cc'>([^<]*)</td>", page)
text = re.sub(r"<[^>]+>", " ", page)


def numbers_of(cell):
    out = set()
    for m in re.finditer(r"(\d+)\s*-\s*(\d+)", cell):
        out |= set(range(int(m.group(1)), int(m.group(2)) + 1))
    out |= {int(n) for n in re.findall(r"\d+", cell)}
    return out


listed = set()
for c in cells:
    listed |= numbers_of(c)
# the drum / chip-wave tables render numbers inside tags, not .cc cells
listed |= {int(n) for n in re.findall(r"<b>(\d+)</b>", page)}

# numbers listed per section (a literal must be in the sections its file is registered for)
by_section = {}
for m in re.finditer(r'<details class="cc-sec" id="(\w+)">(.*?)</details>', page, re.S):
    nums = set()
    for c in re.findall(r"<td class='cc'>([^<]*)</td>", m.group(2)):
        nums |= numbers_of(c)
    nums |= {int(n) for n in re.findall(r"<b>(\d+)</b>", m.group(2))}
    by_section[m.group(1)] = nums

# --- 1. every MIDI-dispatching file is registered ----------------------------
DISPATCH = re.compile(r"\.control\b|\bcc\s*==|\bcc\s+in\b|['\"]program_change['\"]|['\"]pitchwheel['\"]|"
                      r"['\"]control_change['\"]\s*(?:and|:|\))|msg\.type\s*==\s*['\"]control_change")
SKIP = re.compile(r"^(_test|test_|gen_|cc_surfaces|bsp_readback|fxprobe|pgmlib)")
cands = []
for p in sorted(glob.glob(os.path.join(HERE, "orc1*.py")) + [os.path.join(HERE, "daw", "orc_visuals.py")]):
    rel = os.path.relpath(p, HERE).replace(os.sep, "/")
    if SKIP.match(os.path.basename(rel)):
        continue
    if DISPATCH.search(src(rel)):
        cands.append(rel)
for rel in cands:
    check(f"registered: {rel}", rel in X.COVERAGE,
          "reacts to MIDI but is not in cc_surfaces.COVERAGE -- list it on /cc")

# --- 2. every section id COVERAGE names exists -------------------------------
gen_ids = {"channels", "global", "ctrl", "handlers", "drums", "sampler", "mixtrack", "fire", "web"}
known = {s["id"] for s in X.all_sections()} | gen_ids
for rel, (ids, why) in sorted(X.COVERAGE.items()):
    check(f"COVERAGE ids exist: {rel}", all(i in known for i in ids) and bool(why),
          [i for i in ids if i not in known])
for sid in sorted({s["id"] for s in X.all_sections()}):
    check(f"section on page: {sid}", sid in page_ids)

# --- 3 + 4. what the registered files compare a CC against -------------------
def cc_compares(rel):
    """(names, literals) a CC value is compared against in rel."""
    names, lits = set(), set()
    try:
        tree = ast.parse(src(rel))
    except SyntaxError:
        return names, lits

    def is_cc(n):
        return (isinstance(n, ast.Attribute) and n.attr == "control") or \
               (isinstance(n, ast.Name) and n.id in ("cc", "control"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and is_cc(node.left):
            for op, comp in zip(node.ops, node.comparators):
                items = comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set)) else [comp]
                for it in items:
                    if isinstance(it, ast.Constant) and isinstance(it.value, int):
                        lits.add(it.value)
                    elif isinstance(it, ast.Name):
                        names.add(it.id)
                    elif isinstance(it, ast.Attribute):
                        names.add(it.attr)
    return names, lits


# files whose CC compares are the generic synth-handler scan / controller tables the
# generator already enumerates by its own AST (and gen_cc_map's tests cover): the
# name + literal checks below are for the files cc_surfaces owns
OWNED = ["orc1_drum_host.py", "orc1_poly_host.py", "orc1_chip_host.py", "orc1_mono_host.py",
         "orc1_sampler_host.py", "orc1_bbkit_host.py", "orc1_fxmix.py", "orc1_zoomfx.py",
         "daw/orc_visuals.py", "orc1_kikpad.py"]
for rel in OWNED:
    names, lits = cc_compares(rel)
    missing_names = sorted(n for n in names if n not in X.HANDLED_NAMES)
    check(f"CC names registered: {rel}", not missing_names,
          f"{missing_names} compared against a CC but not in cc_surfaces.HANDLED_NAMES")
    mine = set()
    for sid in X.COVERAGE[rel][0]:
        mine |= by_section.get(sid, set())
    miss = sorted(n for n in lits if n not in mine)
    check(f"CC literals on page: {rel}", not miss,
          f"{miss} not listed in its sections {X.COVERAGE[rel][0]}")
    wrong = sorted((n, X.HANDLED_NAMES[n]) for n in names
                   if n in X.HANDLED_NAMES and X.HANDLED_NAMES[n] not in page_ids | gen_ids)
    check(f"HANDLED_NAMES point at real sections: {rel}", not wrong, wrong)

# --- 5. specific completeness ------------------------------------------------
C = X._cfg()
lv = {k: cc for cc, _l, k in X.bus_levels(C)}
levels_defined = set()
for fn in X._LEVEL_FILES:
    levels_defined |= set(k for _n, k, _d in X._LEVEL_PAT.findall(src(fn)))
raw_defs = set()
for fn in X._LEVEL_FILES:
    raw_defs |= set(re.findall(r"^(_(?:VOL|FXMIX)_\w+?_CC)\s*=", src(fn), re.M))
check("every _VOL_*/_FXMIX_* CC definition is parsed (no unlisted bus level)",
      len(raw_defs) == len({n for fn in X._LEVEL_FILES for n, _k, _d in X._LEVEL_PAT.findall(src(fn))}),
      sorted(raw_defs))
for k in ("MASTER_CC", "POLY_CC", "CHIP_CC", "DRUM_CC", "MONO_CC", "BBKIT_VOL_CC",
          "FXMIX_MIX_CC", "FXMIX_LEVEL_CC"):
    check(f"bus level listed: {k}", k in lv, sorted(lv))
check("bus levels are distinct CCs", len(set(lv.values())) == len(lv), lv)

acts = X.cmd_actions()
raw_actions = set(re.findall(r'\baction\s*==\s*"([A-Z0-9_]+)"', src("orc1.py")))
check("every command-socket action is listed", raw_actions == set(acts), raw_actions ^ set(acts))
check("BeatBuddy actions all have a label", all(acts[a] for a in acts if a.startswith("BB_")),
      [a for a in acts if a.startswith("BB_") and not acts[a]])
fam = " ".join(r[2] for s in X.actions_section() for b in s["blocks"] for r in b["rows"] if len(r) > 2)
check("all actions appear in the family table", all(a in fam for a in acts))
check("BeatBuddy section names the actions", all(a in text for a in acts if a.startswith("BB_")))

vis = G.__dict__  # noqa  (keep the generator imported)
knobs = X._dict_with_comments(src("daw/orc_visuals.py"), "KNOBS")
check("visuals: knob table was read", len(knobs) > 30, len(knobs))
check("visuals: every knob CC listed", set(knobs) <= listed, sorted(set(knobs) - listed))
check("visuals: scene CC listed", X._int_const(src("daw/orc_visuals.py"), "SCENE_CC") in listed)

import orc1_zoomfx as Z
check("zoomfx: every slot base and its knobs listed",
      all(b in listed and b + 9 in listed or f"{b + 1}-{b + 9}" in " ".join(cells) for b in Z.SLOT_BASES))
check("zoomfx: tuner and store CCs listed", {Z.CC_TUNER, Z.CC_TUNER2, Z.CC_STORE} <= listed)
check("zoomfx: program change range read from the module",
      any("Patch" in r[1] for s in X.zoomfx_section() for b in s["blocks"] for r in b["rows"]))

import orc1_pedal as P
for cc in list(P.FCB_EF_PROFILE["switch_cc"]) + list(P.FCB_EF_PROFILE["exp_cc"]) + [P.FCB_EF_PROFILE["pr_bank_cc"]]:
    check(f"FCB CC {cc} listed", cc in listed)
check("FCB SP notes listed", set(P.FCB_EF_PROFILE["switch_note"]) <= listed)

check("control-channel notes listed (base/ext/trigger/mod/func)",
      set(C.CTRL_BASE_NOTES) | set(C.CTRL_EXT_NOTES) | set(C.CTRL_MOD_NOTES) | {C.FUNC_KEY_NOTE}
      | set(C.CTRL_TRIGGER_NOTES) <= listed)
check("control-channel program changes listed", set(C.PC_PRESET_MAP) <= listed)
check("bind handshake CCs and commit note listed",
      {C.BIND_ARM_CC, C.BIND_CLEAR_CC, C.BIND_COMMIT_NOTE} <= listed)
check("every module id listed", all(m in text for m in C.MODULE_KEYS))
check("scene recall CC listed in its own section (and free of the ch-13 maps)",
      C.SCENE_RECALL_CC in by_section.get("scenes", set())
      and all(C.SCENE_RECALL_CC not in d for d in (C.CTRL_CC_MAP, C.ARP_CC_MAP, C.FUNC_CC_MAP)))

fire_src = src("orc1_fire.py")
btn = re.search(r"^BUTTON_NOTE\s*=\s*\{(.*?)\}", fire_src, re.S | re.M).group(1)
fnotes = {int(n) for _a, n in re.findall(r'"(\w+)"\s*:\s*(\d+)', btn)}
check("Fire: every button note listed", fnotes <= listed, sorted(fnotes - listed))
check("Fire: every button name listed",
      all(n in text for n, _ in re.findall(r'"(\w+)"\s*:\s*(\d+)', btn)))

# message-type census must mention the hosts that handle program change / pitch bend
cs = {r[0]: r for s in X.census_section() for b in s["blocks"] for r in b["rows"]}
check("census: chip host handles PC and pitch bend", cs.get("Chip host", [0] * 9)[3] == "yes"
      and cs["Chip host"][4] == "yes")
check("census: BBKit host handles PC", cs.get("BBKit host", [0] * 9)[3] == "yes")

# --- the page is a fragment, with no stale standalone chrome ----------------
check("page is a fragment (no <html>/<style>)", "<html" not in page and "<style" not in page)
check("page keeps no topic-bubble nav", "<nav" not in page)
check("every section has content", all(re.search(rf'id="{s}"><summary>.*?</details>', page, re.S)
                                       for s in page_ids))

print(f"\n{passed} passed, {failed} failed")
if passed + failed != EXPECTED:
    print(f"EXPECTED {EXPECTED} checks, ran {passed + failed}: a check was added or removed")
    failed += 1
sys.exit(1 if failed else 0)
