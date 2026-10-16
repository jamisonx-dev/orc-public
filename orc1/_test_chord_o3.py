"""Offline tests for the O3 chord engine (2026-07-29).

Runs against the REAL orc1.py source -- top-level tables and functions are
extracted with `ast` and exec'd in an isolated namespace with a stub `state`.
orc1.py cannot be imported (it runs as __main__ and opens hardware), so this is
the closest thing to testing the deployed module without touching it.

    python3 _test_chord_o3.py

Zero side effects: no MIDI, no JACK, no files written, nothing on the live rig.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, "orc1.py")

WANT_NAMES = [
    "CHORD_BASE", "CHORD_BASE_NAMES", "CHORD_EXTS", "CHORD_EXT_NAMES",
    "CHORD_EXT_DEGREES", "CHORD_EXT_DEGREE_NAMES", "CHORD_EXT_DEGREE_FALLBACK",
    "SCALES", "SCALE_NAMES", "SCALE_GROUPS", "SCALE_ORDER",
    "CHORD_SHAPE_NAMES", "ROMAN_DEGREES", "ROOT_NAMES",
]
WANT_FUNCS = [
    "degree_intervals", "_split_ext_steps", "get_intervals", "chord_root_now",
    "current_degree", "shape_name", "_ext_sort_key", "chord_name_str",
    "effective_base", "build_chord_notes", "get_all_inversions",
    "scale_pitch_classes",
]


def load():
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    found = set()
    for node in tree.body:
        seg = None
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
G = NS  # functions resolve their globals here, so `state` must live in G

# ---- stub engine state -------------------------------------------------------
STATE = {
    "root": 0, "chord_root": 0, "scale": "MAJ", "chord_base": "MAJ",
    "chord_exts": set(), "base_override": False, "key_lock": False,
    "octave": 4, "oct_offset": 0, "inversion": 0, "voicing": "CLOSE",
    "spread": 0,
}
G["state"] = STATE

SCALES = NS["SCALES"]
CHORD_BASE = NS["CHORD_BASE"]
get_intervals = NS["get_intervals"]
degree_intervals = NS["degree_intervals"]
chord_name_str = NS["chord_name_str"]
effective_base = NS["effective_base"]
build_chord_notes = NS["build_chord_notes"]
scale_pitch_classes = NS["scale_pitch_classes"]
current_degree = NS["current_degree"]
chord_root_now = NS["chord_root_now"]
shape_name = NS["shape_name"]

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# The pre-O3 DIATONIC_BASE ladders for MAJ and MIN, hardcoded here ON PURPOSE as
# a frozen regression baseline: O3 must reproduce them exactly, because these are
# the two scales that are actually played most.  If this test ever fails, the
# harmony of the common case has changed and that is a real regression.
OLD_MAJ = ["MAJ", "MIN", "MIN", "MAJ", "MAJ", "MIN", "DIM"]
OLD_MIN = ["MIN", "DIM", "MAJ", "MIN", "MIN", "MAJ", "MAJ"]

_passed = _failed = 0


def check(cond, label, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {label}" + (f"  [{detail}]" if detail else ""))


def reset(**kw):
    STATE.update({
        "root": 0, "chord_root": 0, "scale": "MAJ", "chord_base": "MAJ",
        "chord_exts": set(), "base_override": False, "key_lock": False,
        "octave": 4, "oct_offset": 0, "inversion": 0, "voicing": "CLOSE",
        "spread": 0,
    })
    STATE.update(kw)


def triad_name(ivs):
    """Name a stack from its FULL interval set.

    Must not truncate to three: a four-note stack named by its triad would make
    a seventh-chord test pass while reporting the wrong chord (that mistake made
    this suite's first run print MAJ where G7 was correct).
    """
    key = tuple(i for i in ivs if i < 12)
    for nm, shape in CHORD_BASE.items():
        if key == tuple(sorted(shape)):
            return nm
    return NS["CHORD_SHAPE_NAMES"].get(key, str(key))


print("=" * 70)
print("1. REGRESSION GUARD -- MAJ/MIN ladders must match the pre-O3 table")
print("=" * 70)
for scale, old in (("MAJ", OLD_MAJ), ("MIN", OLD_MIN)):
    iv = SCALES[scale]
    got = []
    for d in range(len(iv)):
        reset(scale=scale, root=0, chord_root=iv[d] % 12)
        got.append(triad_name(get_intervals("MAJ", set())))
    check(got == old, f"{scale} ladder unchanged", f"got {got} want {old}")
    print(f"  {scale}: {' '.join(got)}")

print()
print("=" * 70)
print("2. IN-KEY BY CONSTRUCTION -- every scale x key x degree")
print("=" * 70)
bad, total = 0, 0
for scale, iv in SCALES.items():
    for key in range(12):
        allowed = {(key + i) % 12 for i in iv}
        for d in range(len(iv)):
            cr = (key + iv[d]) % 12
            reset(scale=scale, root=key, chord_root=cr)
            ivs = get_intervals("MAJ", set())
            total += 1
            if any((cr + i) % 12 not in allowed for i in ivs):
                bad += 1
check(bad == 0, "no chord contains an out-of-scale note", f"{bad} of {total}")
print(f"  {total} chords, {bad} with a note outside their scale")

print()
print("=" * 70)
print("3. THE ORIGINAL BUG -- quality must agree with SCALE SNAP in EVERY key")
print("=" * 70)
bad = 0
for scale in ("MAJ", "MIN", "DOR", "MIX", "HMIN", "LYD", "PHR"):
    for key in range(12):
        reset(scale=scale, root=key, chord_root=key)
        allowed = set(scale_pitch_classes() or range(12))
        ivs = get_intervals("MAJ", set())
        if any((key + i) % 12 not in allowed for i in ivs):
            bad += 1
check(bad == 0, "tonic chord always inside SCALE SNAP's set (was 36/60 broken)",
      f"{bad} failures")
print(f"  84 tonic chords across 7 scales x 12 keys, {bad} disagreeing")

print()
print("=" * 70)
print("4. SEVENTHS come from the scale")
print("=" * 70)
reset(scale="MAJ", root=0)
want = ["maj7", "m7", "m7", "maj7", "7", "m7", "m7b5"]
got = []
for d in range(7):
    reset(scale="MAJ", root=0, chord_root=SCALES["MAJ"][d] % 12,
          chord_exts={"7th"})
    got.append(triad_name(get_intervals("MAJ", {"7th"})))
check(got == want, "C major seventh ladder", f"got {got}")
print(f"  {' '.join(got)}")

print()
print("=" * 70)
print("5. FIXED mode (base pad pressed) is unchanged")
print("=" * 70)
for base, shape in CHORD_BASE.items():
    reset(base_override=True, chord_base=base, scale="MAJ", root=0, chord_root=0)
    check(get_intervals(base, set()) == sorted(shape),
          f"FIXED {base} -> {sorted(shape)}", str(get_intervals(base, set())))
# FIXED must win even on a scale that has a perfectly good ladder
reset(base_override=True, chord_base="DIM", scale="MAJ", root=0, chord_root=0)
check(get_intervals("DIM", set()) == [0, 3, 6],
      "FIXED overrides the scale's own I chord")

print()
print("=" * 70)
print("6. CHROMATIC escape hatch")
print("=" * 70)
# On the ii of C major the scale's 7th is minor (10).  M7 must force 11.
reset(scale="MAJ", root=0, chord_root=2, chord_exts={"M7"})
ivs = get_intervals("MAJ", {"M7"})
check(11 in ivs, "chromatic M7 forces a major seventh over ii", str(ivs))
reset(scale="MAJ", root=0, chord_root=2, chord_exts={"7th"})
ivs = get_intervals("MAJ", {"7th"})
check(10 in ivs and 11 not in ivs, "scale 7th over ii is minor", str(ivs))
# Both vocabularies at once must not explode and must include both sevenths.
reset(scale="MAJ", root=0, chord_root=2, chord_exts={"7th", "M7"})
ivs = get_intervals("MAJ", {"7th", "M7"})
check(10 in ivs and 11 in ivs, "mixed vocabularies coexist", str(ivs))
try:
    reset(scale="MAJ", root=0, chord_root=2, chord_exts={"7th", "M7", "11th", "6"})
    chord_name_str()
    NS["_ext_sort_key"]("11th")
    sorted({"7th", "M7"}, key=NS["_ext_sort_key"])
    check(True, "mixed ext set sorts and names without raising")
except Exception as e:
    check(False, "mixed ext set sorts and names without raising", repr(e))

print()
print("=" * 70)
print("7. Fallthrough: off-key root, unknown scale, tiny scale")
print("=" * 70)
# C# is not in C major -> no degree -> fixed shape on that root.
reset(scale="MAJ", root=0, chord_root=1, chord_base="MIN")
check(get_intervals("MIN", set()) == [0, 3, 7],
      "off-key root falls through to the fixed shape",
      str(get_intervals("MIN", set())))
check(current_degree() is None, "off-key root reports no degree")
check(degree_intervals(1, 0, "MAJ") is None, "degree_intervals rejects off-key root")
check(degree_intervals(0, 0, "NOPE") is None, "degree_intervals rejects unknown scale")
# A degree ext with no ladder must stay audible, not vanish.
reset(scale="MAJ", root=0, chord_root=1, chord_base="MAJ", chord_exts={"7th"})
check(10 in get_intervals("MAJ", {"7th"}),
      "degree ext survives the fixed fallthrough via FALLBACK",
      str(get_intervals("MAJ", {"7th"})))

print()
print("=" * 70)
print("8. KEY LOCK pulls an off-key root into the key")
print("=" * 70)
reset(scale="MAJ", root=0, chord_root=1, key_lock=True)
check(chord_root_now() in {0, 2}, "C# pulled to C or D in C major",
      str(chord_root_now()))
check(current_degree() is not None, "locked root now has a degree")
ivs = get_intervals("MAJ", set())
allowed = set(scale_pitch_classes())
cr = chord_root_now()
check(all((cr + i) % 12 in allowed for i in ivs),
      "locked off-key press still yields an in-key chord", str(ivs))
reset(scale="MAJ", root=0, chord_root=1, key_lock=False)
check(chord_root_now() == 1, "unlocked root is left alone")

print()
print("=" * 70)
print("9. Explicit chord_root (Mixtrack deck B) harmonises ITS OWN root")
print("=" * 70)
# Live chord sits on C (degree I).  Deck B asks for D.  D must come out minor,
# not inherit C's major.  This is the bug the chord_root parameter exists for.
reset(scale="MAJ", root=0, chord_root=0)
check(triad_name(get_intervals("MAJ", set())) == "MAJ", "live chord is I=MAJ")
check(triad_name(get_intervals("MAJ", set(), chord_root=2)) == "MIN",
      "explicit root 2 harmonises as ii=MIN",
      triad_name(get_intervals("MAJ", set(), chord_root=2)))
notes = build_chord_notes(2, "MAJ", set(), 4, 0, "CLOSE", 0, 0)
check(sorted(n % 12 for n in notes) == sorted([2, 5, 9]),
      "build_chord_notes(D) gives D,F,A", str(sorted(n % 12 for n in notes)))

print()
print("=" * 70)
print("10. Naming never lies")
print("=" * 70)
reset(scale="MAJ", root=0, chord_root=0)
check(effective_base() == "MAJ", "I of C major names as MAJ", effective_base())
reset(scale="HMIN", root=0, chord_root=3)
check(effective_base() == "AUG", "bIII of C harmonic minor names as AUG",
      effective_base())
# Every scale/degree: the name must either be absent or match the real intervals.
mismatch = 0
for scale, iv in SCALES.items():
    for d in range(len(iv)):
        reset(scale=scale, root=0, chord_root=iv[d] % 12)
        ivs = get_intervals("MAJ", set())
        nm = shape_name(ivs)
        if nm is not None:
            key = [k for k, v in NS["CHORD_SHAPE_NAMES"].items() if v == nm]
            if tuple(ivs) not in key:
                mismatch += 1
check(mismatch == 0, "no shape is given a name that contradicts its intervals",
      str(mismatch))
# An unnamed stack must degrade to a degree, never to a wrong triad name.
reset(scale="HIRA", root=0, chord_root=SCALES["HIRA"][3] % 12)
nm = chord_name_str()
check(nm not in CHORD_BASE or shape_name(get_intervals("MAJ", set())) is not None,
      "unnamed HIRA stack is not mislabelled as a basic triad", nm)

print()
print("=" * 70)
print("11. CHROM is not a ladder (regression: it is the BOOT DEFAULT scale)")
print("=" * 70)
# Section 2's in-scale check is VACUOUS for CHROM -- every pitch class is in the
# scale, so a whole-tone cluster passes it.  Assert the actual shape instead.
check(degree_intervals(0, 0, "CHROM") is None,
      "degree_intervals refuses CHROM", str(degree_intervals(0, 0, "CHROM")))
for base, shape in CHORD_BASE.items():
    reset(scale="CHROM", root=0, chord_root=0, chord_base=base)
    check(get_intervals(base, set()) == sorted(shape),
          f"CHROM falls through to the fixed {base} triad",
          str(get_intervals(base, set())))
# ...and it must stay persistent, i.e. survive base_override being cleared, which
# handle_note_input() does on every key press.
reset(scale="CHROM", root=0, chord_root=7, chord_base="MIN", base_override=False)
check(get_intervals("MIN", set()) == [0, 3, 7],
      "CHROM base pads are PERSISTENT (survive base_override clearing)",
      str(get_intervals("MIN", set())))

print()
print("=" * 70)
print("12. print_status()-style formatting survives a scale-degree ext")
print("=" * 70)
# The real print_status() raised a KeyError on CHORD_EXTS[e] for any degree ext,
# which killed write_state_json(), every /cmd reply, and DIN MIDI input.  It is
# not extractable here (it prints and writes files), so assert the exact
# expression it uses, over every combination of both vocabularies.
import itertools
ALL_EXTS = list(NS["CHORD_EXT_DEGREES"]) + list(NS["CHORD_EXTS"])
bad = 0
for r in range(len(ALL_EXTS) + 1):
    for combo in itertools.combinations(ALL_EXTS, r):
        try:
            sorted(set(combo), key=NS["_ext_sort_key"])
            reset(scale="MAJ", root=0, chord_root=2, chord_exts=set(combo))
            chord_name_str()
            effective_base()
        except Exception:
            bad += 1
check(bad == 0, "all 256 ext combinations sort/name without raising", str(bad))

print()
print("=" * 70)
print("13. effective_base() never reports a quality that is not sounding")
print("=" * 70)
lies = []
for scale, iv in SCALES.items():
    for d in range(len(iv)):
        reset(scale=scale, root=0, chord_root=iv[d] % 12, chord_base="MAJ")
        eb = effective_base()
        ivs = get_intervals("MAJ", set())
        if eb in CHORD_BASE and sorted(CHORD_BASE[eb]) != list(ivs):
            lies.append((scale, d + 1, eb, ivs))
check(not lies, "no scale/degree labels a stack with a triad it isn't",
      str(lies[:4]))
# The specific case the reviewer found: HIRA I is (0,3,8) -- an unnamed shape.
reset(scale="HIRA", root=0, chord_root=0, chord_base="MAJ")
check(effective_base() != "MAJ",
      "HIRA I does not fall back to the stale stored MAJ", effective_base())

print()
print("=" * 70)
print(f"RESULT: {_passed} passed, {_failed} failed")
print("=" * 70)
sys.exit(1 if _failed else 0)
