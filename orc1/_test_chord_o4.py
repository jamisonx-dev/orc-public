#!/usr/bin/env python3
"""Offline tests for the O4 chord CONTROL scheme (2026-07-30).

O3 (2026-07-29) fixed the harmony; O4 makes the controls say what they are
doing. Three things ship here and each has a way of being silently wrong:

  * quality_mode DIAT/FIXED -- an EXPLICIT source for the chord quality.
    Before this, a base pad was a momentary override cleared by the next key
    press and there was no way to ask for a persistent quality at all. The
    failure mode is that get_intervals() and effective_base() disagree about
    which path they took, so the OLED names a chord that isn't sounding.
  * scale_mode off/filter/up/down/nearest -- replaces the binary scale_snap.
    "filter" makes scale_snap_note() able to return None for the first time,
    which every caller has to handle or the keyboard goes silent / crashes.
  * base_override narrowed to mean ONLY "momentary override inside DIAT", so
    the four places that set it must agree.

Same technique as _test_chord_o3.py: the real orc1.py is parsed with `ast` and
the wanted tables/functions are exec'd in an isolated namespace against a stub
state. orc1.py cannot be imported (it runs as __main__ and opens hardware).

    python3 _test_chord_o4.py [-v]

Zero side effects: no MIDI, no JACK, no files written, nothing on the live rig.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, "orc1.py")
VERBOSE = "-v" in sys.argv

WANT_NAMES = [
    "CHORD_BASE", "CHORD_EXTS", "CHORD_EXT_DEGREES",
    "CHORD_EXT_DEGREE_FALLBACK", "SCALES", "SCALE_NAMES",
    # SCALE_ORDER is a comprehension over SCALE_GROUPS, so the group table has
    # to come with it or the exec of SCALE_ORDER raises NameError.
    "SCALE_GROUPS", "SCALE_ORDER",
    "CHORD_SHAPE_NAMES", "ROMAN_DEGREES", "ROOT_NAMES", "SCALE_MODES",
]
WANT_FUNCS = [
    "degree_intervals", "_split_ext_steps", "get_intervals", "chord_root_now",
    "current_degree", "shape_name", "effective_base", "scale_pitch_classes",
    "_snap_to_pcs", "scale_snap_note", "scale_mode_now", "set_scale_mode",
    "set_chord_base",
]


def load():
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    ns, found = {}, set()
    for node in tree.body:
        seg = name = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in WANT_NAMES:
                    seg, name = ast.get_source_segment(src, node), t.id
                    break
        elif isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            seg, name = ast.get_source_segment(src, node), node.name
        if seg:
            exec(seg, ns)
            found.add(name)
    missing = (set(WANT_NAMES) | set(WANT_FUNCS)) - found
    if missing:
        sys.exit("FATAL: could not extract from orc1.py: " + ", ".join(sorted(missing)))
    return ns


NS = load()

STATE = {
    "root": 0, "chord_root": 0, "scale": "MAJ", "chord_base": "MAJ",
    "chord_exts": set(), "base_override": False, "quality_mode": "DIAT",
    "scale_snap": False, "scale_mode": "off", "key_lock": False,
    "inversion": 0,
}
NS["state"] = STATE
# _retrigger_chord is called by set_chord_base; it plays audio on the real rig.
NS["_retrigger_chord"] = lambda *a, **k: None

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append(bool(cond))
    if cond:
        if VERBOSE:
            print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}")
        if detail:
            print(f"        {detail}")


def section(t):
    print(f"\n--- {t} ---")


def reset(**kw):
    STATE.update({
        "root": 0, "chord_root": 0, "scale": "MAJ", "chord_base": "MAJ",
        "chord_exts": set(), "base_override": False, "quality_mode": "DIAT",
        "scale_snap": False, "scale_mode": "off", "key_lock": False,
        "inversion": 0,
    })
    STATE.update(kw)


# ---------------------------------------------------------------------------
# 1. quality_mode
# ---------------------------------------------------------------------------

def test_quality_mode():
    section("quality: DIAT vs latched FIXED")
    gi, eb = NS["get_intervals"], NS["effective_base"]

    # In C major the ii degree (D) is minor. DIAT must produce a minor third.
    reset(root=0, scale="MAJ", chord_root=2, chord_base="MAJ")
    diat = gi("MAJ", set())
    check("DIAT on degree ii gives a minor third (not the MAJ pad)",
          3 in diat and 4 not in diat, f"{diat}")

    # FIXED must give the pad's own shape on the SAME degree.
    reset(root=0, scale="MAJ", chord_root=2, chord_base="MAJ",
          quality_mode="FIXED")
    fixed = gi("MAJ", set())
    check("FIXED on degree ii gives the MAJ pad's major third",
          4 in fixed and 3 not in fixed, f"{fixed}")

    # THE REGRESSION THAT MATTERS: the label must follow the same path as the
    # notes. If these disagree the OLED names a chord that is not sounding.
    check("FIXED: effective_base() agrees with get_intervals()",
          eb() == "MAJ", f"effective_base={eb()!r} ivs={fixed}")
    reset(root=0, scale="MAJ", chord_root=2, chord_base="MAJ")
    check("DIAT: effective_base() does NOT claim the pad's quality",
          eb() != "MAJ", f"effective_base={eb()!r}")

    # A momentary override inside DIAT behaves like FIXED for one chord.
    reset(root=0, scale="MAJ", chord_root=2, chord_base="MAJ",
          base_override=True)
    mom = gi("MAJ", set())
    check("DIAT + base_override gives the pad's shape (momentary override)",
          mom == fixed, f"{mom} vs {fixed}")

    # FIXED must be independent of base_override -- that is the whole point of
    # latching it. Before O4 there was no state that survived a key press.
    reset(root=0, scale="MAJ", chord_root=2, chord_base="MAJ",
          quality_mode="FIXED", base_override=False)
    check("FIXED holds with base_override False (survives a key press)",
          gi("MAJ", set()) == fixed, f"{gi('MAJ', set())}")


def test_base_override_is_diat_only():
    section("quality: base_override narrowed to DIAT-only")
    scb = NS["set_chord_base"]

    reset(quality_mode="DIAT")
    scb("MIN", retrigger=False)
    check("DIAT: a base pad sets the momentary override flag",
          STATE["base_override"] is True)

    reset(quality_mode="FIXED")
    scb("MIN", retrigger=False)
    check("FIXED: a base pad does NOT set base_override (no phantom '*')",
          STATE["base_override"] is False, f"{STATE['base_override']}")
    check("FIXED: the base pad still takes effect",
          STATE["chord_base"] == "MIN", f"{STATE['chord_base']}")

    # An unknown name must change nothing at all.
    reset(quality_mode="DIAT", chord_base="MAJ")
    scb("NOPE", retrigger=False)
    check("an unknown base name is ignored entirely",
          STATE["chord_base"] == "MAJ" and STATE["base_override"] is False)


# ---------------------------------------------------------------------------
# 2. scale_mode
# ---------------------------------------------------------------------------

def test_scale_modes():
    section("scale: five modes")
    ssn, smn, ssm = NS["scale_snap_note"], NS["scale_mode_now"], NS["set_scale_mode"]

    # C major: C#(61) is off-scale, C(60) is in it.
    reset(root=0, scale="MAJ")

    ssm("off")
    check("off: an off-scale note passes through untouched", ssn(61) == 61)

    ssm("filter")
    check("filter: an off-scale note returns None (silenced)", ssn(61) is None)
    check("filter: an IN-scale note still passes", ssn(60) == 60)

    ssm("up")
    check("up: C# rounds up to D", ssn(61) == 62, f"{ssn(61)}")

    ssm("down")
    check("down: C# rounds down to C", ssn(61) == 60, f"{ssn(61)}")

    ssm("nearest")
    check("nearest: C# resolves to a scale tone", ssn(61) % 12 in (0, 2),
          f"{ssn(61)}")
    check("nearest: an in-scale note is untouched", ssn(64) == 64)

    # Direction must hold at an awkward interval too: in C major, F#(66) is
    # equidistant-ish between F and G, so up/down must still differ.
    ssm("up")
    up = ssn(66)
    ssm("down")
    dn = ssn(66)
    check("up and down genuinely differ on F#", up != dn, f"up={up} down={dn}")
    check("up/down land on real scale tones",
          up % 12 in NS["scale_pitch_classes"]()
          and dn % 12 in NS["scale_pitch_classes"](),
          f"up={up} down={dn}")

    # CHROM has every pitch class, so nothing can ever be filtered -- a filter
    # that silenced notes there would mute the instrument on the boot scale.
    reset(root=0, scale="CHROM")
    ssm("filter")
    check("CHROM: filter never silences (every pitch class is in scale)",
          ssn(61) == 61, f"{ssn(61)}")

    # Legacy compatibility: state written before O4 has only scale_snap.
    reset(root=0, scale="MAJ")
    STATE.pop("scale_mode", None)
    STATE["scale_snap"] = True
    check("legacy scale_snap=True reads as 'nearest'", smn() == "nearest")
    STATE["scale_snap"] = False
    check("legacy scale_snap=False reads as 'off'", smn() == "off")

    # A garbage value must degrade, not raise -- this is consulted per note.
    reset(root=0, scale="MAJ", scale_mode="banana", scale_snap=False)
    check("an unrecognised scale_mode degrades to 'off'", smn() == "off")
    check("...and the note still passes through", ssn(61) == 61)


def test_scale_mode_mirror():
    section("scale: legacy mirror stays consistent")
    ssm, smn = NS["set_scale_mode"], NS["scale_mode_now"]
    reset()
    for m in NS["SCALE_MODES"]:
        ssm(m)
        check(f"{m}: scale_snap mirror matches the mode",
              STATE["scale_snap"] == (m != "off"),
              f"mode={m} snap={STATE['scale_snap']}")
        check(f"{m}: round-trips through scale_mode_now()", smn() == m)
    reset()
    check("set_scale_mode rejects an unknown mode", ssm("sideways") is False)
    check("...and leaves the current mode alone", smn() == "off")


# ---------------------------------------------------------------------------
# 3. O3 must not have regressed
# ---------------------------------------------------------------------------

def test_o3_still_holds():
    section("regression: O3 harmony unchanged in DIAT")
    gi = NS["get_intervals"]
    scale_pcs = NS["scale_pitch_classes"]

    bad = []
    for scale in NS["SCALE_ORDER"]:
        for key in range(12):
            for cr in range(12):
                reset(root=key, scale=scale, chord_root=cr)
                pcs = scale_pcs()
                if pcs is None:
                    continue          # CHROM is unconstrained by design
                if cr not in pcs:
                    continue          # off-key root: chord_root_now() handles it
                for iv in gi("MAJ", set()):
                    if (cr + iv) % 12 not in pcs:
                        bad.append((scale, key, cr, iv))
    check("every DIAT chord is still inside its scale", not bad,
          f"{len(bad)} out-of-scale, first: {bad[:3]}")

    # FIXED is explicitly allowed to leave the scale -- that is what it is for.
    reset(root=0, scale="MAJ", chord_root=2, quality_mode="FIXED")
    ivs = gi("MAJ", set())
    check("FIXED is allowed to leave the scale (that is its purpose)",
          any((2 + i) % 12 not in scale_pcs() for i in ivs), f"{ivs}")


def test_no_bare_scale_snap_writes():
    """scale_snap is a MIRROR. Writing it directly desyncs it from scale_mode.

    This shipped as a real bug: handle_ctrl_cc()'s SCALE_SNAP branch (CC 11 on
    CH_CTRL, reachable from any UCS pad or external controller) set the mirror
    on its own. Because scale_mode_now() consults scale_mode FIRST and "off" is
    a valid value, the mode never changed -- so the CC toggled the Fire's pad
    shading while the actual note-snapping ignored it completely.

    Static check, because the offending call sites live in functions that are
    not extractable without the whole engine.
    """
    section("static: scale_snap is only written by set_scale_mode()")
    src = open(SRC_PATH, encoding="utf-8").read()
    lines = src.splitlines()

    # Find the body of set_scale_mode so its own legitimate write is excluded.
    tree = ast.parse(src)
    ok_range = range(0)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "set_scale_mode":
            ok_range = range(node.lineno, (node.end_lineno or node.lineno) + 1)

    offenders = []
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if 'state["scale_snap"]' in line and "=" in line.split('state["scale_snap"]')[1][:3]:
            if n not in ok_range:
                offenders.append(f"line {n}: {stripped[:72]}")
    check("no bare state['scale_snap'] write outside set_scale_mode()",
          not offenders, "\n        ".join(offenders))

    # And the reason it matters, asserted directly.
    reset()
    STATE["scale_mode"] = "off"
    STATE["scale_snap"] = True          # what a bare mirror write looks like
    check("a bare mirror write does NOT change behaviour (proves the trap)",
          NS["scale_mode_now"]() == "off",
          "if this passes as 'nearest' the precedence changed; revisit the fix")


def main():
    print("=" * 58)
    print("  O4 chord control scheme")
    print("=" * 58)
    for t in (test_quality_mode, test_base_override_is_diat_only,
              test_scale_modes, test_scale_mode_mirror,
              test_no_bare_scale_snap_writes, test_o3_still_holds):
        try:
            t()
        except Exception:
            import traceback
            check(f"{t.__name__} itself crashed", False,
                  traceback.format_exc(limit=6))
    passed = sum(1 for r in _RESULTS if r)
    failed = len(_RESULTS) - passed
    print(f"\n{'=' * 58}")
    print(f"  {passed} passed, {failed} failed, {len(_RESULTS)} checks")
    print(f"{'=' * 58}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
