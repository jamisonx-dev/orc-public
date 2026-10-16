#!/usr/bin/env python3
"""Generate the ORC-1 CC map: static/cc_map.html

    python3 gen_cc_map.py          # writes static/cc_map.html
    python3 gen_cc_map.py --check  # exit 1 if the page is out of date

WHY A GENERATOR AND NOT A HAND-WRITTEN PAGE
-------------------------------------------
A typed-out CC table is wrong the day after it is written, and this project
has been bitten repeatedly by documents that ASSERT hardware/software facts
instead of ENUMERATING them (CLAUDE.md's standing rule). So every number on
the generated page comes from one of exactly two places:

  1. The LIVE tables, by importing orc1_synth / orc1_config on the Pi. The
     drum map, the sampler vocabulary, the chip channel map and the bus
     volume CCs are all computed at import there, so this reads the same
     objects the audio thread does. (orc1_synth's import is side-effect
     free -- `import jack` lives inside run_jack -- verified 2026-08-04.)
  2. The SOURCE, by AST: every `cc == N` / `cc in (...)` branch inside each
     set_cc handler. Each is described by `describe()`: a curated label for
     the controls whose code does not read plainly, else a label derived from
     what the branch assigns (attack, filter cutoff, ...), else the first
     sentence of the author's comment.

Handlers on classes nothing constructs are left out of the page. The page
itself carries no source comments, code or provenance; `--check` compares a
rebuild against the page on disk to tell you it is stale.
"""

import ast, hashlib, html, io, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "static", "cc_map.html")
# Machine-readable sibling of cc_map.html, grouped by synth MODULE. Consumed by
# the Module Matrix web page's `params` panel so it shows real, live CC numbers
# per voice instead of a hand-typed (rotting) list. Written from the SAME data as
# the HTML page -- see build_json() -- so one regen keeps both in step.
OUT_JSON = os.path.join(HERE, "static", "cc_map.json")

SOURCES = {
    "synth":    os.path.join(HERE, "orc1_synth.py"),
    "config":   os.path.join(HERE, "orc1_config.py"),
    "fire":     os.path.join(HERE, "orc1_fire.py"),
    "mixtrack": os.path.join(HERE, "orc1_mixtrack.py"),
    "sampler":  os.path.join(HERE, "orc1_sampler.py"),
    # Added 2026-08-04 after an audit found the page missing the LARGEST CC
    # surface on the instrument: channel 13's control maps, dispatched by
    # orc1.py's handle_ctrl_cc from three dicts in orc1_config. They are dict
    # lookups, so even opening the file would not have shown them to the
    # `cc == N` scan -- they need the live tables (see ctrl_maps below).
    "brain":    os.path.join(HERE, "orc1.py"),
    "web":      os.path.join(HERE, "orc1_web.py"),
}


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


# ---------------------------------------------------------------------------
# AST: pull every literal CC branch out of a handler, with its comment
# ---------------------------------------------------------------------------

def _comment_above(lines, lineno):
    """Contiguous '#' comment block immediately above 1-based `lineno`."""
    out, i = [], lineno - 2
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            i -= 1
        elif not s:
            break
        else:
            break
    return " ".join(reversed(out)).strip()


def _const_int(node, consts=None):
    """An int literal, or (2026-09-24) a module-level int constant / simple
    +,-,* arithmetic on them (`_GB_HARM_CC0 + _GB_HARM_N`). None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if consts and isinstance(node, ast.Name):
        return consts.get(node.id)
    if consts and isinstance(node, ast.BinOp) \
            and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        a, b = _const_int(node.left, consts), _const_int(node.right, consts)
        if a is None or b is None:
            return None
        return a + b if isinstance(node.op, ast.Add) else \
            a - b if isinstance(node.op, ast.Sub) else a * b
    return None


def module_consts(tree):
    """{NAME: int} for module-level `NAME = <int expr>` assignments, in order."""
    out = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 \
                and isinstance(st.targets[0], ast.Name):
            v = _const_int(st.value, out)
            if v is not None:
                out[st.targets[0].id] = v
    return out


def _cc_literals(test, var="cc", consts=None):
    """CC numbers a branch test matches, plus any extra conditions on it.
    With `consts` (module-level int constants), `cc == NAME` and a range test
    `LO <= cc < HI` resolve too (the GB wave harmonic knobs, 2026-09-24);
    without it only literal numbers count, as before."""
    ccs, extra = [], []
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        if consts and len(node.ops) == 2 \
                and isinstance(node.comparators[0], ast.Name) \
                and node.comparators[0].id == var \
                and all(isinstance(o, (ast.Lt, ast.LtE)) for o in node.ops):
            lo = _const_int(node.left, consts)
            hi = _const_int(node.comparators[1], consts)
            if lo is not None and hi is not None:
                lo += isinstance(node.ops[0], ast.Lt)
                hi -= isinstance(node.ops[1], ast.Lt)
                if 0 <= lo <= hi <= 127:
                    ccs.extend(range(lo, hi + 1))
            continue
        if len(node.ops) != 1:
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == var):
            continue
        op, comp = node.ops[0], node.comparators[0]
        if isinstance(op, ast.Eq):
            v = _const_int(comp, consts)
            if v is not None:
                ccs.append(v)
        elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
            for e in comp.elts:
                v = _const_int(e, consts)
                if v is not None:
                    ccs.append(v)
    # any other named condition guarding this branch (e.g. voice == 'pulse1')
    for node in ast.walk(test):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = node.left
            if isinstance(left, ast.Name) and left.id != var:
                try:
                    extra.append(ast.unparse(node))
                except Exception:
                    pass
    return sorted(set(ccs)), sorted(set(extra))


_VOICE_RE      = re.compile(r"\bvoice\b")
_VOICE_ONLY_RE = re.compile(r"voice\s*==\s*'([a-z0-9_]+)'")


def scan_handlers(path):
    """[{owner, func, lineno, branches:[{ccs, extra, comment, code}]}]"""
    src   = read(path)
    lines = src.splitlines()
    tree  = ast.parse(src)
    found = []
    consts = module_consts(tree)

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            args = [a.arg for a in node.args.args]
            if "cc" not in args:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()
                return
            branches = []
            # Walk the statements in order (not ast.walk) so each branch carries
            # the VOICE guards around it (2026-09-24): an inner `if cc == 79:`
            # under `if voice == 'pulse1' and cc in (79, 80, 81):` is pulse1-only,
            # and everything after `if voice == 'wave': ...; return` is not for
            # the wave. Without this the matrix listed the pulse1 sweep CCs on
            # every chip voice.
            def walk(stmts, guards):
                guards = list(guards)
                for st in stmts:
                    if isinstance(st, ast.If):
                        ccs, extra = _cc_literals(st.test, consts=consts)
                        vg = [e for e in extra if _VOICE_RE.search(e)]
                        if ccs:
                            body0 = st.body[0]
                            comment = _comment_above(lines, body0.lineno) \
                                or _comment_above(lines, st.lineno)
                            try:
                                code = ast.unparse(body0)
                            except Exception:
                                code = ""
                            branches.append({
                                "ccs": ccs,
                                "extra": sorted(set(guards) | set(extra)),
                                "comment": comment, "code": code,
                                "line": st.lineno})
                        walk(st.body, guards + vg)
                        walk(st.orelse, guards)
                        # `if voice == 'x': ...; return` -> later siblings
                        # never see voice x.
                        m = _VOICE_ONLY_RE.fullmatch(ast.unparse(st.test).strip())
                        if (m and not ccs and not st.orelse
                                and isinstance(st.body[-1], ast.Return)):
                            guards.append(f"voice != '{m.group(1)}'")
                    else:
                        for field in ("body", "orelse", "finalbody", "handlers"):
                            sub = getattr(st, field, None)
                            if isinstance(sub, list) and sub and \
                                    isinstance(sub[0], (ast.stmt, ast.excepthandler)):
                                walk(sub, guards)
            walk(node.body, [])
            if branches:
                found.append({"owner": ".".join(self.stack) or "<module>",
                              "func": node.name, "line": node.lineno,
                              "branches": branches})
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    V().visit(tree)
    return found


# Plain-English names for what a branch assigns, matched against the branch's
# first statement. Checked before the author's comment so every voice words the
# same control the same way.
_CODE_LABELS = [
    (r"\.attack\s*=",                      "Attack time"),
    (r"\.decay\s*=",                       "Decay time"),
    (r"\.sustain\s*=",                     "Sustain level"),
    (r"\.release\s*=",                     "Release time"),
    (r"\.master\s*=",                      "Voice level"),
    (r"\brm\.depth\s*=",                   "Ring mod depth"),
    (r"\brm\.ratio\s*=",                   "Ring mod ratio"),
    (r"\brm\.fixed_hz\s*=",                "Ring mod fixed frequency"),
    (r"filt(?:\.set|_set)\(\s*mode=",           "Filter mode"),
    (r"filt(?:\.set|_set)\(\s*q=",              "Filter resonance"),
    (r"filt(?:\.set|_set)\(\s*cutoff=",         "Filter cutoff"),
    (r"\.octave\s*=",                      "Octave"),
    (r"\.glide_time\s*=",                  "Glide time"),
    (r"\['gain'\]\s*=",                   "Layer gain"),
    (r"\.waveform\s*=|\['wave'\]\s*=",    "Waveform"),
    (r"\.p1p2_spread\s*=",                 "P1/P2 stereo spread (on at 64+)"),
]

# Where neither the code nor the author's comment reads well as a one-line
# control name. Keyed by (class, cc).
_LABELS = {
    ("NESEngine", 1):   "Pulse duty cycle",
    ("NESEngine", 3):   "Noise mode: white (below 64) or metallic (64+)",
    ("NESEngine", 5):   "Glide time",
    ("NESEngine", 12):  "Ring mod depth",
    ("NESEngine", 13):  "Voice level",
    ("NESEngine", 14):  "Ring mod ratio",
    ("NESEngine", 15):  "Ring mod fixed frequency",
    ("NESEngine", 72):  "P1/P2 coupling mode",
    ("NESEngine", 73):  "P1/P2 detune (64 = none, about ±50 cents)",
    ("NESEngine", 77):  "Vibrato depth",
    ("NESEngine", 78):  "Vibrato rate",
    ("NESEngine", 79):  "Pulse 1 sweep time (0 = off)",
    ("NESEngine", 80):  "Pulse 1 sweep shift",
    ("NESEngine", 81):  "Pulse 1 sweep direction: up (below 64) or down (64+)",
    ("NESEngine", 86):  "Pan (64 = centre)",
    ("NESEngine", 87):  "Arpeggio interval A, in semitones (alone = trill)",
    ("NESEngine", 88):  "Arpeggio interval B, in semitones",
    ("NESEngine", 89):  "Arpeggio speed",
    ("NESEngine", 90):  "Pitch slide amount (64 = none)",
    ("NESEngine", 110): "Pitch slide time",
    ("NESEngine", 111): "Tremolo depth",
    ("NESEngine", 112): "Tremolo rate",
    ("NESEngine", 113): "Retrigger rate (0 = off)",
    ("NESEngine", 114): "Noise key split: notes at or above this play noise 2",
    ("NESEngine", 85):  "Pulse anti-alias, PolyBLEP (on at 64+)",
    ("GameboyWaveVoice", 1):  "Waveform preset (8 presets)",
    ("GameboyWaveVoice", 115): "Store: while held, the next Program Change saves the wave to that slot",
    ("GameboyWaveVoice", 7):  "Volume",
    ("GameboyWaveVoice", 20): "Custom wave: step to edit",
    ("GameboyWaveVoice", 21): "Custom wave: value for that step",
    ("GameboyWaveVoice", 22): "Volume mode: continuous (below 64) or 4-step (64+)",
    ("GameboyWaveVoice", 23): "Custom wave on (64+) or off",
    ("GameboyWaveVoice", 24): "Hard retrigger on every note (64+)",
    ("PolySynth", 1):   "Waveform: sine, saw, square or triangle",
    ("MonoSynth", 1):   "Waveform",
    ("MonoSynth", 5):   "Glide time",
    ("MonoSynth", 12):  "Ring mod depth",
}


for _n in range(8):
    _LABELS[("GameboyWaveVoice", 102 + _n)] = f"Wave harmonic {_n + 1} level"


def _tidy(text):
    """First sentence of a source comment, without dates, rulers or history."""
    t = re.sub(r"\(\s*\d{4}-\d\d-\d\d[^)]*\)", "", text)
    t = re.sub(r"\b\d{4}-\d\d-\d\d\b,?", "", t)
    t = re.sub(r"-{2,}", " ", t).strip(" -")
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    t = re.sub(r"\s+", " ", t).strip(" -,;:")
    return (t[:117].rstrip() + "...") if len(t) > 120 else t


def describe(owner, branch, cc=None):
    """One plain line saying what a CC branch (or one CC of it) does."""
    cls = owner.split(".")[-1]
    if cc is not None and (cls, cc) in _LABELS:
        return _LABELS[(cls, cc)]
    code = branch.get("code") or ""
    for pat, label in _CODE_LABELS:
        if re.search(pat, code):
            return label
    if branch.get("comment"):
        t = _tidy(branch["comment"])
        if t:
            return t
    return re.sub(r"\s+", " ", code)[:80]


def branch_rows(owner, branch):
    """[(cc, description)] for a branch, one entry per CC it answers."""
    return [(cc, describe(owner, branch, cc)) for cc in branch["ccs"]]


def scan_consts(path, pattern=r"^(CC_[A-Z0-9_]+|NOTE_[A-Z0-9_]+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*(?:#\s*(.*))?$"):
    """Module-level `CC_x = 0x11  # comment` constants, in source order."""
    out = []
    for i, line in enumerate(read(path).splitlines(), 1):
        m = re.match(pattern, line)
        if m:
            out.append({"name": m.group(1), "value": int(m.group(2), 0),
                        "comment": (m.group(3) or "").strip(), "line": i})
    return out


def schema_keys(path):
    """Config keys the /system page can actually edit -- i.e. the string
    literals in orc1_web.py's _CONFIG_SCHEMA.

    The page used to derive "editable on /system" from membership in
    cfg.SYNTH, which is a DIFFERENT set: it tagged CH_MONO (in no schema)
    editable and CH_SAMPLER (in the schema, under SAMPLER.) not. An
    assertion where an enumeration was available -- the exact bug class this
    generator exists to prevent.
    """
    keys = set()
    try:
        src = read(path)
    except OSError:
        return keys
    m = re.search(r"_CONFIG_SCHEMA\s*=\s*\[", src)
    if not m:
        return keys
    depth, i = 0, m.end() - 1
    while i < len(src):
        if src[i] == "[": depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0: break
        i += 1
    for lit in re.findall(r'"([A-Z_]+\.[A-Za-z_0-9]+)"', src[m.end():i]):
        keys.add(lit)
        keys.add(lit.split(".", 1)[1])      # bare name, for CH_* lookups
    return keys


def ctrl_maps(C):
    """Channel-13 (CH_CTRL) assignments, from the live config dicts that
    orc1.py's handle_ctrl_cc reads. Dict lookups, invisible to an AST scan
    for `cc == N` -- and the biggest CC surface on the instrument."""
    out = {}
    for name in ("FUNC_CC_MAP", "CTRL_CC_MAP", "ARP_CC_MAP"):
        d = getattr(C, name, None)
        if isinstance(d, dict):
            out[name] = {int(k): (v if isinstance(v, str) else repr(v))
                         for k, v in d.items() if isinstance(k, int)}
    return out


def fire_inputs(path):
    """The Fire's own CC INPUT -- its knobs and the SELECT encoder. They are
    a dict and a named constant compared inside `_cc(self, msg)`, so neither
    the int-literal scan nor the `cc`-named-parameter filter could see them;
    the page's Fire section showed 2 OLED-label rows and nothing else."""
    src = read(path)
    out = {"dials": {}, "select": None, "knob_defaults": []}
    m = re.search(r"^DIAL_CC\s*=\s*\{([^}]*)\}", src, re.M)
    if m:
        out["dials"] = {int(a): int(b)
                        for a, b in re.findall(r"(\d+)\s*:\s*(\d+)", m.group(1))}
    m = re.search(r"^SELECT_CC\s*=\s*(\d+)", src, re.M)
    if m:
        out["select"] = int(m.group(1))
    # Default knob assignments: ("name", ("cc_out", port, channel-or-MODULE, cc)).
    # Since 2026-09-24 a synth-bus target NAMES its module ("poly") and resolves
    # it from /matrix at send time; a digits-only pattern silently dropped those
    # (the page showed 2 of 22 knobs for two weeks). Keep both forms.
    m = re.search(r"_DEFAULT_KNOB_TARGETS\s*=\s*\[", src)
    if m:
        seg = src[m.start(): src.find("\n]", m.start())]
        pat = re.compile(r'\("([^"]+)",\s*\("cc_out",\s*"([^"]+)",\s*'
                         r'(\d+|"[A-Za-z_][\w]*"),\s*(\d+)\)\),?\s*(?:#\s*(.*))?$')
        for line in seg.splitlines():
            mm = pat.search(line.strip())
            if not mm:
                continue
            nm, port, ch, cc, desc = mm.groups()
            ch = int(ch) if ch.isdigit() else ch.strip('"')
            out["knob_defaults"].append(
                {"name": nm, "port": port, "ch": ch, "cc": int(cc),
                 "desc": (desc or "").strip()})
    return out


def _vol_targets(path):
    """The actual attribute each bus-volume CC writes, read out of
    _route_midi's source. Formatting these from the bus name produced
    `engine.drum.master`, which does not exist -- the real one is
    `engine.drum_master`. A generated page must not invent an identifier."""
    out = {}
    pat = re.compile(r"cc == _VOL_(\w+?)_CC:\s*(engine\.[\w.]+)\s*=")
    for line in read(path).splitlines():
        m = pat.search(line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def _engine_sources():
    """orc1_synth.py plus every module host. Since the audio-host split the
    engines are CONSTRUCTED in the hosts (`S.PolySynth()`, `S.NESEngine(...)`),
    not in orc1_synth.py -- its SynthEngine was removed 2026-09-24."""
    hosts = sorted(f for f in os.listdir(HERE)
                   if f.startswith("orc1_") and f.endswith("_host.py"))
    return [SOURCES["synth"]] + [os.path.join(HERE, f) for f in hosts]


def constructed_classes(paths):
    """Class names that are actually instantiated somewhere in `paths` (a path
    or a list), by bare name or as `S.<Name>(...)`. A set_cc handler on a
    class nobody constructs is dead code, and listing its CCs as if a player
    could use them is exactly the kind of confident wrong documentation this
    generator exists to avoid."""
    if isinstance(paths, str):
        paths = [paths]
    made = set()
    for path in paths:
        for node in ast.walk(ast.parse(read(path))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                made.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                made.add(node.func.attr)
    return made


def scan_web_senders(root):
    """Every CC the web UI can send: `cc: N` in static/js/*.js."""
    out = []
    jsdir = os.path.join(root, "static", "js")
    if not os.path.isdir(jsdir):
        return out
    pat = re.compile(r"cc:\s*(\d+)")
    for fn in sorted(os.listdir(jsdir)):
        if not fn.endswith(".js"):
            continue
        for i, line in enumerate(read(os.path.join(jsdir, fn)).splitlines(), 1):
            for m in pat.finditer(line):
                out.append({"page": fn, "cc": int(m.group(1)),
                            "line": i, "src": line.strip()[:160]})
    return out


# ---------------------------------------------------------------------------
# LIVE tables (import; falls back to "unavailable" off-Pi rather than lying)
# ---------------------------------------------------------------------------

def live_tables():
    sys.path.insert(0, HERE)
    t = {"ok": False, "error": None}
    try:
        import orc1_synth as S
        import orc1_config as C
    except Exception as e:                       # off-Pi (no numpy/mido/etc.)
        t["error"] = f"{type(e).__name__}: {e}"
        return t
    t["ok"] = True
    syn = getattr(C, "SYNTH", {}) or {}
    # Channels come from the SYNTH MODULE's resolved constants, not from
    # cfg.SYNTH: a key absent from the config dict still exists as a module
    # default (CH_SAMPLER is exactly that -- reading only the dict reported
    # it as "channel None"). cfg.SYNTH is then used to mark which ones are
    # editable on /system.
    t["channels"] = {k: v for k, v in vars(S).items()
                     if k.startswith("CH_") and isinstance(v, int)}
    t["cfg_keys"]   = {k for k in syn if k.startswith("CH_")}
    t["schema"]     = schema_keys(SOURCES["web"])
    t["ctrl_maps"]  = ctrl_maps(C)
    t["ctrl_ch"]    = getattr(C, "CH_CTRL", None)
    shared = {}
    for k, v in t["channels"].items():
        shared.setdefault(v, []).append(k)
    t["shared_channels"] = {v: ks for v, ks in shared.items() if len(ks) > 1}
    t["chip_ch_voice"] = dict(getattr(S, "_CHIP_CH_VOICE", {}))
    t["vol"] = {"channel": getattr(S, "_VOL_CH", None),
                "master": getattr(S, "_VOL_MASTER_CC", None),
                "poly":   getattr(S, "_VOL_POLY_CC", None),
                "chip":   getattr(S, "_VOL_CHIP_CC", None),
                "drum":   getattr(S, "_VOL_DRUM_CC", None)}
    t["vol_targets"] = _vol_targets(SOURCES["synth"])
    t["drum_cc"] = {int(k): list(v) for k, v in
                    getattr(S, "DRUM_CC_TO_VP", {}).items()}
    t["drum_notes"] = {int(k): v for k, v in
                       getattr(S, "DRUM_NOTE_MAP", {}).items()}
    smp = {}
    for name in dir(S):
        if not name.startswith("_SMP_"):
            continue
        val = getattr(S, name)
        if isinstance(val, dict) and val and all(isinstance(k, int) for k in val):
            smp[name] = {int(k): (list(v) if isinstance(v, (list, tuple)) else v)
                         for k, v in val.items()}
        elif isinstance(val, int):
            smp[name] = val
    t["sampler"] = smp
    t["sampler_ch"] = getattr(S, "CH_SAMPLER", None)
    t["panic_cc"] = [120, 123]
    t["constructed"] = constructed_classes(_engine_sources())
    return t


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

E = lambda s: html.escape(str(s), quote=True)

CSS = """
:root{--bg:#faf9f7;--fg:#1a1a1a;--mut:#6b6b6b;--line:#e2ded8;--card:#fff;
 --accent:#b4541f;--chip:#f0ece6;--warn:#8a5b00;--warnbg:#fff6e0;--code:#f5f2ed}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e6e3;--mut:#9a9691;
 --line:#2c2c2c;--card:#1c1c1c;--accent:#e8873f;--chip:#252525;--warn:#e0b060;
 --warnbg:#2a2415;--code:#111}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 90px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.09em;color:var(--accent);
 margin:38px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:13px;margin:20px 0 6px;color:var(--fg)}
.sub{color:var(--mut);font-size:13px;margin:0 0 18px}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0 10px;
 border-bottom:1px solid var(--line);margin-bottom:6px}
input[type=search]{width:100%;padding:11px 13px;font-size:14px;border-radius:9px;
 border:1px solid var(--line);background:var(--card);color:var(--fg)}
input[type=search]:focus{outline:2px solid var(--accent);outline-offset:-1px}
nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
nav a{font-size:11px;text-decoration:none;color:var(--mut);background:var(--chip);
 padding:4px 9px;border-radius:99px;border:1px solid var(--line)}
nav a:hover{color:var(--accent);border-color:var(--accent)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;
 background:var(--card);margin:8px 0 16px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
 color:var(--mut);font-weight:600;padding:9px 11px;border-bottom:1px solid var(--line);
 white-space:nowrap;background:var(--card);position:sticky;top:0}
td{padding:8px 11px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.cc{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:700;
 color:var(--accent);white-space:nowrap}
td.mono,code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
code{background:var(--code);padding:1px 5px;border-radius:4px}
.src{color:var(--mut);font-size:11px;white-space:nowrap}
.note{background:var(--warnbg);border:1px solid var(--warn);color:var(--warn);
 padding:10px 13px;border-radius:9px;font-size:12.5px;margin:10px 0}
.miss{color:var(--mut);font-style:italic}
.tag{display:inline-block;font-size:10px;padding:1px 7px;border-radius:99px;
 background:var(--chip);border:1px solid var(--line);color:var(--mut);
 margin-right:4px;white-space:nowrap}
.foot{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
 color:var(--mut);font-size:11.5px}
.foot code{font-size:11px}
.hidden{display:none}
.count{color:var(--mut);font-size:11px;font-weight:400;text-transform:none;
 letter-spacing:0;margin-left:8px}
"""

JS = """
const q = document.getElementById('q');
const rows = Array.from(document.querySelectorAll('tbody tr'));
const secs = Array.from(document.querySelectorAll('section'));
function apply(){
  const t = q.value.trim().toLowerCase();
  rows.forEach(r => r.classList.toggle('hidden', t && !r.textContent.toLowerCase().includes(t)));
  secs.forEach(s => {
    const vis = s.querySelectorAll('tbody tr:not(.hidden)').length;
    const has = s.querySelectorAll('tbody tr').length;
    s.classList.toggle('hidden', !!t && has > 0 && vis === 0);
  });
}
q.addEventListener('input', apply);
q.addEventListener('keydown', e => { if (e.key === 'Escape') { q.value=''; apply(); }});
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== q) { e.preventDefault(); q.focus(); }
});
"""


def table(headers, rows_html, section_id=None):
    if not rows_html:
        return '<p class="miss">nothing found</p>'
    # Headers are NOT re-escaped: they are authored here and may legitimately
    # contain an entity. Escaping them rendered `CC &rarr; parameter` as
    # literal text in the drums table -- the same double-escape trap already
    # worked around for the nav pills.
    h = "".join(f"<th>{x}</th>" for x in headers)
    return (f'<div class="scroll"><table><thead><tr>{h}</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody></table></div>')


HANDLER_TITLES = {
    "PolySynth":        "Poly synth",
    "MonoSynth":        "Mono synth",
    "NESEngine":        "Chip engine (every chip voice)",
    "GameboyWaveVoice": "Game Boy wave voice",
}

SAMPLER_LABELS = {
    "AUDITION":    "Audition (one-shot play-through)",
    "BENDDN":      "Pitch bend down",
    "BENDUP":      "Pitch bend up",
    "BR":          "Beat repeat (hold)",
    "BR_LEN":      "Beat repeat length (28-bit frame count, most significant CC first)",
    "CAP":         "Capture (velocity = seconds)",
    "CAP28":       "Frame-exact capture: length in frames, then window-end offset in ms (28-bit, most significant CC first)",
    "CAP_LSB":     "Capture length in deciseconds, low 7 bits (fires the capture)",
    "CAP_MSB":     "Capture length in deciseconds, high 7 bits",
    "CUE":         "Hot cue (velocity 1-4)",
    "DECK_SRC":    "Deck source (value = skipback track id)",
    "END":         "Region end",
    "EQ_HI":       "EQ high",
    "EQ_LOW":      "EQ low",
    "EQ_MID":      "EQ mid",
    "EXPORT":      "Export capture to samples",
    "FILTER":      "Filter sweep",
    "GAIN":        "Deck gain",
    "GATE":        "Gate (note on plays, note off stops)",
    "LOAD":        "Load file",
    "LOOP":        "Loop play / stop",
    "MASTER_GAIN": "Master gain",
    "NUDGE":       "Nudge capture window (10 ms per tick)",
    "ONSET":       "Onset snap (64+ on)",
    "PRESS_LAT":   "Press latency trim",
    "REVERSE":     "Reverse",
    "SCRATCH":     "Scratch mode",
    "SCRUB":       "Scrub (jog wheel)",
    "SEEK":        "Seek",
    "SPEED":       "Speed",
    "START":       "Region start",
    "STOP":        "Stop and rewind",
    "XF":          "Crossfade",
}

CHANNEL_ROLES = {"CH_POLY": "Poly synth", "CH_DRUM": "Drums",
                 "CH_MONO": "Mono synth", "CH_SAMPLER": "Sampler"}

PORT_NAMES = {"synth": "Synth bus", "pisound": "DIN out"}


def sampler_kinds(path):
    """{table name: 'Note' | 'CC'}, read from how the synth builds each table:
    a table built from a NOTE_ config key is a note table."""
    kinds = {}
    for line in read(path).splitlines():
        m = re.match(r'(_SMP_\w+)\s*=.*?"(NOTE|CC)_', line)
        if m:
            kinds[m.group(1)] = "Note" if m.group(2) == "NOTE" else "CC"
    return kinds


def _sampler_cell(val, deck):
    """The numbers one deck answers to, for one sampler table."""
    if not isinstance(val, dict):
        return ""
    plain = sorted(n for n, d in val.items() if d == deck)
    if plain:
        return " ".join(f"<b>{n}</b>" for n in plain)
    multi = {}
    for n, d in val.items():
        if isinstance(d, list) and d and d[0] == deck:
            multi.setdefault(d[1] if len(d) > 2 else "", []).append((d[-1], n))
    parts = []
    names = {"FRA": "length", "OFF": "offset"}
    for kind in sorted(multi, reverse=True):
        nums = " ".join(f"<b>{n}</b>" for _, n in sorted(multi[kind], reverse=True))
        parts.append(f"{names.get(kind, kind.lower()) + ': ' if kind else ''}{nums}")
    return " &middot; ".join(parts)


def _control_name(const, prefix):
    words = const[len(prefix):].lower().split("_")
    swap = {"hi": "high", "lo": "low", "dn": "down", "enc": "encoder",
            "fx": "FX", "eq": "EQ"}
    name = " ".join(swap.get(w, w) for w in words)
    name = re.sub(r"(\D)(\d)", r"\1 \2", name)
    return name[:1].upper() + name[1:]


def _voice_tags(extra):
    out = []
    for e in extra:
        m = re.fullmatch(r"voice == '(\w+)'", e)
        if m:
            out.append(f"{m.group(1)} only")
        m = re.fullmatch(r"voice != '(\w+)'", e)
        if m:
            out.append(f"not {m.group(1)}")
    return out


def build(live, handlers, mix_consts, web, fire=None):
    o = io.StringIO()
    w = o.write
    gen = time.strftime("%Y-%m-%d %H:%M:%S")

    secs = []   # (id, title)

    def sec(sid, title, count=None):
        secs.append((sid, title))
        c = f'<span class="count">{count}</span>' if count is not None else ""
        w(f'<section id="{sid}"><h2>{E(title)}{c}</h2>')

    w('<div class="wrap">')
    w('<h1>ORC-1 &mdash; CC map</h1>')
    w('<p class="sub">Every control-change assignment on the instrument. '
      'Press <code>/</code> to search.</p>')
    w('<div class="bar"><input type="search" id="q" '
      'placeholder="Search CC number, name, voice or page&hellip;">'
      '<nav id="nav"></nav></div>')

    if not live.get("ok"):
        w(f'<div class="note"><b>Live tables unavailable</b> &mdash; '
          f'{E(live.get("error"))}. The channel, drum and sampler sections are '
          f'missing; regenerate on the Pi for the full page.</div>')

    # -- channels ------------------------------------------------------------
    if live.get("ok"):
        chs   = live["channels"]
        chip  = live["chip_ch_voice"]
        share = live.get("shared_channels", {})
        rows  = []
        for name, num in sorted(chs.items(), key=lambda kv: (kv[1], kv[0])):
            bits = []
            if chip.get(num):
                bits.append(f"Chip voice <b>{E(chip[num])}</b>")
            elif name in CHANNEL_ROLES:
                bits.append(CHANNEL_ROLES[name])
            if num in share:
                others = [x for x in share[num] if x != name]
                bits.append('<span class="tag">shared with '
                            + E(", ".join(others)) + "</span>")
            rows.append(f"<tr><td class='cc'>CH {num}</td><td class='mono'>"
                        f"{E(name)}</td><td>{' '.join(bits)}</td></tr>")
        sec("channels", "MIDI channels", len(rows))
        w('<p class="sub">Default channels for the synth bus. The live '
          'assignment of each module is set on <b>/matrix</b>.</p>')
        w(table(["Channel", "Constant", "Role"], rows))
        w('</section>')

    # -- global --------------------------------------------------------------
    sec("global", "Global (any channel)", 2)
    w(table(["CC", "Name", "Effect"], [
        "<tr><td class='cc'>120</td><td>All Sound Off</td>"
        "<td>Silences every engine (the PANIC button)</td></tr>",
        "<tr><td class='cc'>123</td><td>All Notes Off</td>"
        "<td>Same as 120</td></tr>"]))
    w('</section>')

    # -- channel 13 control maps ---------------------------------------------
    cm = live.get("ctrl_maps") or {}
    if cm:
        titles = {"FUNC_CC_MAP": "Chord functions", "CTRL_CC_MAP": "Chord controls",
                  "ARP_CC_MAP": "Arpeggiator"}
        total_cm = sum(len(v) for v in cm.values())
        sec("ctrl", f"Brain control (channel {live.get('ctrl_ch', '?')})", total_cm)
        for name, d in cm.items():
            rows = [f"<tr><td class='cc'>{cc}</td><td class='mono'>{E(act)}</td></tr>"
                    for cc, act in sorted(d.items())]
            w(f'<h3>{E(titles.get(name, name))} <span class="count">{len(d)} CCs</span></h3>')
            w(table(["CC", "Action"], rows))
        w('</section>')

    # -- bus volumes ---------------------------------------------------------
    if live.get("ok") and live["vol"]["channel"]:
        v = live["vol"]
        rows = [f"<tr><td class='cc'>{v[k]}</td><td>{k.capitalize()} bus level</td>"
                f"<td>0&ndash;127</td></tr>"
                for k in ("master", "poly", "chip", "drum")]
        sec("bus", f"Bus levels (channel {v['channel']})", len(rows))
        w(table(["CC", "Control", "Range"], rows))
        w('</section>')

    # -- synth voice handlers ------------------------------------------------
    # A class nobody constructs is a retired engine; its CCs do nothing.
    made = live.get("constructed")
    live_h = [h for h in handlers
              if made is None or h["owner"] in made or h["owner"] == "<module>"]
    total = 0
    body = io.StringIO()
    for h in live_h:
        rows, seen = [], set()
        for b in h["branches"]:
            tags = _voice_tags(b["extra"])
            for cc, desc in branch_rows(h["owner"], b):
                key = (cc, desc, tuple(tags))
                if key in seen:
                    continue
                seen.add(key)
                rows.append((cc, desc, tags))
        rows.sort(key=lambda r: (r[0], r[1]))
        total += len(rows)
        cells = [f"<tr><td class='cc'>{cc}</td><td>{E(desc)}</td><td>"
                 + " ".join(f'<span class="tag">{E(t)}</span>' for t in tags)
                 + "</td></tr>" for cc, desc, tags in rows]
        title = HANDLER_TITLES.get(h["owner"].split(".")[-1], h["owner"])
        body.write(f'<h3>{E(title)} <span class="count">{len(rows)} CCs</span></h3>')
        body.write(table(["CC", "Control", ""], cells))
    sec("handlers", "Synth voices", total)
    w(body.getvalue())
    w('</section>')

    # -- drums ---------------------------------------------------------------
    if live.get("ok") and live["drum_cc"]:
        by_voice = {}
        for cc, (voice, param) in sorted(live["drum_cc"].items()):
            by_voice.setdefault(voice, []).append((cc, param))
        rows = []
        for voice, items in by_voice.items():
            cells = " ".join(f'<span class="tag"><b>{cc}</b> {E(p)}</span>'
                             for cc, p in items)
            rows.append(f"<tr><td class='mono'>{E(voice)}</td>"
                        f"<td>{cells}</td></tr>")
        sec("drums", f"Drums (channel {live['channels'].get('CH_DRUM', '?')})",
            len(live["drum_cc"]))
        w(table(["Voice", "CC and parameter"], rows))
        if live.get("drum_notes"):
            nrows = [f"<tr><td class='cc'>{n}</td><td class='mono'>{E(v)}</td></tr>"
                     for n, v in sorted(live["drum_notes"].items())]
            w("<h3>Trigger notes</h3>")
            w(table(["Note", "Voice"], nrows))
        w('</section>')

    # -- sampler -------------------------------------------------------------
    if live.get("ok") and live.get("sampler"):
        smp = live["sampler"]
        kinds = sampler_kinds(SOURCES["synth"])
        rows = []
        for name in sorted(smp, key=lambda n: SAMPLER_LABELS.get(n[5:], n).lower()):
            val = smp[name]
            key = name[5:]
            label = SAMPLER_LABELS.get(key, key.replace("_", " ").capitalize())
            kind = kinds.get(name, "CC")
            if isinstance(val, int):
                cells = f"<td colspan='2'><b>{val}</b> (both decks)</td>"
            else:
                cells = (f"<td>{_sampler_cell(val, 'A')}</td>"
                         f"<td>{_sampler_cell(val, 'B')}</td>")
            rows.append(f"<tr><td>{E(label)}</td><td>{kind}</td>{cells}</tr>")
        sec("sampler", f"Sampler (channel {live.get('sampler_ch', '?')})", len(rows))
        w(table(["Control", "Type", "Deck A", "Deck B"], rows))
        w('</section>')

    # -- mixtrack ------------------------------------------------------------
    if mix_consts:
        def mrows(items, prefix):
            out = []
            for c in items:
                note = re.sub(r"^\d+\s*(?:—|-)?\s*", "", c["comment"] or "")
                out.append(f"<tr><td class='cc'>{c['value']}</td><td class='mono'>"
                           f"0x{c['value']:02X}</td>"
                           f"<td>{E(_control_name(c['name'], prefix))}</td>"
                           f"<td>{E(note)}</td></tr>")
            return out
        ccs   = [c for c in mix_consts if c["name"].startswith("CC_")]
        notes = [c for c in mix_consts if c["name"].startswith("NOTE_")]
        sec("mixtrack", "Numark Mixtrack Pro 3", len(mix_consts))
        w('<p class="sub">What the controller sends. Deck A, deck B and the '
          'master section use separate MIDI channels.</p>')
        w("<h3>Control changes</h3>")
        w(table(["CC", "Hex", "Control", "Notes"], mrows(ccs, "CC_")))
        w("<h3>Notes (buttons and pads)</h3>")
        w(table(["Note", "Hex", "Control", "Notes"], mrows(notes, "NOTE_")))
        w('</section>')

    # -- fire ----------------------------------------------------------------
    fi = fire or {}
    if fi.get("dials") or fi.get("select") or fi.get("knob_defaults"):
        sec("fire", "Akai Fire", len(fi.get("dials", {}))
            + (1 if fi.get("select") else 0) + len(fi.get("knob_defaults", [])))
        if fi.get("dials") or fi.get("select"):
            rows = [f"<tr><td class='cc'>{cc}</td><td>Knob {idx + 1}</td></tr>"
                    for cc, idx in sorted(fi.get("dials", {}).items(),
                                          key=lambda kv: kv[1])]
            if fi.get("select"):
                rows.append(f"<tr><td class='cc'>{fi['select']}</td>"
                            f"<td>Select encoder</td></tr>")
            w("<h3>Fire to ORC</h3>")
            w(table(["CC", "Control"], rows))
        if fi.get("knob_defaults"):
            rows = [f"<tr><td class='mono'>{E(k['name'])}</td><td>{E(k['desc'])}</td>"
                    f"<td class='cc'>{k['cc']}</td><td>"
                    + (f"ch {k['ch']}" if isinstance(k['ch'], int)
                       else f"{E(k['ch'])} (channel from /matrix)")
                    + f"</td><td>{E(PORT_NAMES.get(k['port'], k['port']))}</td></tr>"
                    for k in fi["knob_defaults"]]
            w("<h3>Default knob targets</h3>")
            w('<p class="sub">What an un-remapped knob sends. Knobs are '
              'remappable with ALT+BROWSER.</p>')
            w(table(["Target", "Sends", "CC", "Channel", "Port"], rows))
        w('</section>')

    # -- web senders ---------------------------------------------------------
    if web:
        by_page = {}
        for e in web:
            by_page.setdefault(e["page"], []).append(e)
        rows = []
        for page in sorted(by_page):
            ccs = sorted({e["cc"] for e in by_page[page]})
            rows.append(
                f"<tr><td class='mono'>{E(page)}</td>"
                f"<td>{' '.join(f'<span class=tag>{c}</span>' for c in ccs)}</td></tr>")
        sec("web", "Web pages", len(by_page))
        w('<p class="sub">CCs each page script sends, where the number is '
          'written directly in the script.</p>')
        w(table(["Page script", "CCs sent"], rows))
        w('</section>')

    w(f'<div class="foot">Generated {E(gen)} by <code>gen_cc_map.py</code></div>')
    w('</div>')

    nav = "".join(f'<a href="#{sid}">{E(t)}</a>' for sid, t in secs)
    body = o.getvalue().replace('<nav id="nav"></nav>', f'<nav>{nav}</nav>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>ORC-1 CC map</title><style>{CSS}</style></head><body>'
            f'{body}<script>{JS}</script></body></html>')


# ---------------------------------------------------------------------------
# JSON: the same data as the page, grouped by synth module (for the web params
# panel). Only the CONSTRUCTED engines are mapped -- MonoSynth and ChipSynth are
# retired dead handlers (their set_cc still parses but nothing constructs them),
# so mapping voices to NESEngine/PolySynth/GameboyWaveVoice by hand here is a
# deliberate allowlist, not an assumption.
# ---------------------------------------------------------------------------

MODULE_HANDLERS = {                       # module key -> (class, method) in synth
    "poly":       ("PolySynth", "set_cc"),
    "chip_p1":    ("NESEngine", "set_cc"),
    "chip_p2":    ("NESEngine", "set_cc"),
    "chip_tri":   ("NESEngine", "set_cc"),
    "chip_noise": ("NESEngine", "set_cc"),
    "chip_noise2": ("NESEngine", "set_cc"),
    "chip_pcm":   ("NESEngine", "set_cc"),
    "gb_wave":    ("GameboyWaveVoice", "set_cc"),
}
# Which chip voice each chip module IS, for filtering NESEngine's per-voice
# branches (`voice == 'pulse1'` etc.).
CHIP_VOICE = {"chip_p1": "pulse1", "chip_p2": "pulse2", "chip_tri": "triangle",
              "chip_noise": "noise", "chip_pcm": "pcm",
              "chip_noise2": "noise2"}
# gb_wave CCs arrive through NESEngine.set_cc(voice='wave'), which handles the
# engine-wide / per-voice ones (vibrato, ring mod, glide/pan/arp/... mods) BEFORE
# handing the rest to GameboyWaveVoice.set_cc. List both (2026-09-24: the
# matrix drawer showed only the wave's own handler, so none of the shared mods).
EXTRA_HANDLERS = {"gb_wave": [("NESEngine", "set_cc", "wave")]}


def _branch_applies(extra, voice_name):
    """Does a NESEngine branch guarded by `extra` apply to this voice?

    Only a LITERAL voice constraint (`voice == 'x'` or `voice in ('a','b')`)
    restricts the branch. A non-literal guard we cannot resolve statically
    (`voice in self._vib_depth`) is treated as applying to all voices -- better
    to over-show a control than to silently hide a real one; the raw condition
    travels in the JSON so the panel can show it.
    """
    if voice_name is None:
        return True
    literal, names = False, set()
    for e in extra or []:
        # `voice != 'x'` = after an early return for voice x (see scan_handlers)
        if voice_name in re.findall(r"\bvoice\s*!=\s*'([a-z0-9_]+)'", e):
            return False
        if re.search(r"\bvoice\s*==", e) or re.search(r"\bvoice\s+in\s*[\(\[\{]", e):
            literal = True
            names |= set(re.findall(r"'([a-z0-9_]+)'", e))
    return (voice_name in names) if literal else True


def build_json(live, handlers):
    hidx = {(h["owner"].split(".")[-1], h["func"]): h for h in handlers}

    def rows_for(module_key, owner, func):
        srcs = [(owner, func, CHIP_VOICE.get(module_key))]   # voice None: poly / gb_wave
        srcs += EXTRA_HANDLERS.get(module_key, [])
        rows, seen = [], set()
        taken = set()        # a CC the module's own handler lists wins
        for o, f, voice in srcs:
            h = hidx.get((o, f))
            if not h:
                continue
            new = set()
            for b in h["branches"]:
                if not _branch_applies(b["extra"], voice):
                    continue
                if set(b["ccs"]) & taken:
                    continue
                new |= set(b["ccs"])
                _add_rows(o, b, rows, seen)
            taken |= new
        rows.sort(key=lambda r: r["cc"])
        return rows

    def _add_rows(owner, b, rows, seen):
        cond = "; ".join(b["extra"]) or None
        code = (b.get("code") or "").strip() or None
        for cc, desc in branch_rows(owner, b):
            key = (cc, desc, cond)          # collapse identical duplicate rows
            if key in seen:
                continue
            seen.add(key)
            rows.append({"cc": cc, "desc": desc, "cond": cond, "code": code})

    modules = {k: rows_for(k, *hf) for k, hf in MODULE_HANDLERS.items()}

    # drums come from the live DRUM_CC_TO_VP table: {cc: (voice, param)}
    drum = []
    for cc, vp in sorted((live.get("drum_cc") or {}).items()):
        vp = list(vp) if isinstance(vp, (list, tuple)) else [vp]
        v = vp[0] if vp else None
        p = vp[1] if len(vp) > 1 else ""
        drum.append({"cc": cc, "desc": f"{v} — {p}".strip(" —"), "cond": None})
    modules["drums"] = drum

    # sampler: deck transport is note-based (see /sampler), not CC -- nothing to
    # list here yet. Present as an explicit empty group rather than omitting it.
    modules["sampler"] = []

    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Generated by gen_cc_map.py from live synth tables + set_cc source. "
                "Do not hand-edit; run gen_cc_map.py to refresh.",
        "modules": modules,
    }


def _json_text(live, handlers):
    return json.dumps(build_json(live, handlers), indent=2, ensure_ascii=False)


def main():
    live     = live_tables()
    # Source-derived, so it works with or without the live import.
    live["constructed"]  = constructed_classes(_engine_sources())
    live.setdefault("vol_targets", _vol_targets(SOURCES["synth"]))
    handlers = scan_handlers(SOURCES["synth"])
    mix      = scan_consts(SOURCES["mixtrack"]) if os.path.exists(SOURCES["mixtrack"]) else []
    fire_in  = fire_inputs(SOURCES["fire"]) if os.path.exists(SOURCES["fire"]) else {}
    live.setdefault("schema", schema_keys(SOURCES["web"]))
    web      = scan_web_senders(HERE)
    page     = build(live, handlers, mix, web, fire=fire_in)

    if "--check" in sys.argv:
        # A degraded rebuild (no live tables) drops four whole sections --
        # every drum CC, the entire sampler vocabulary. Comparing a degraded
        # page against a degraded rebuild reported "up to date", which is the
        # goes-quiet-when-it-cannot-check failure mode by name.
        if not live.get("ok"):
            print("CANNOT CHECK: live tables unavailable here "
                  f"({live.get('error')}). Run --check on the Pi; a page built "
                  "without them is missing whole sections.")
            return 1
        if not os.path.exists(OUT):
            print("cc_map.html does not exist"); return 1
        cur = read(OUT)
        strip = lambda s: re.sub(r"Generated [0-9: -]+", "", s)
        if strip(cur) != strip(page):
            print("STALE: cc_map.html does not match the current sources")
            return 1
        # cc_map.json rides the same sources -- check it too, or a handler edit
        # could leave the params panel stale while the page reads "up to date".
        stripj = lambda s: re.sub(r'"generated":.*', "", s)
        cur_json = read(OUT_JSON) if os.path.exists(OUT_JSON) else ""
        if not cur_json or stripj(cur_json) != stripj(_json_text(live, handlers)):
            print("STALE: cc_map.json does not match the current sources")
            return 1
        print("cc_map.html + cc_map.json are up to date")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        f.write(_json_text(live, handlers))
    n = sum(len(h["branches"]) for h in handlers)
    jmods = build_json(live, handlers)["modules"]
    print(f"wrote {OUT}  ({len(page)} B, {n} synth CC branches, "
          f"{len(live.get('drum_cc', {}))} drum CCs, "
          f"{len(web)} web call sites, live tables: {live.get('ok')})")
    print(f"wrote {OUT_JSON}  (modules: "
          + ", ".join(f"{k}={len(v)}" for k, v in jmods.items()) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
