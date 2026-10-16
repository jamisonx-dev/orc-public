#!/usr/bin/env python3
"""
ORC-1 poly / chip PATCH persistence -- round-trip + wiring suite.
=================================================================

WHY THIS EXISTS
---------------
Drum voice params have always survived a restart; poly and chip synth patches
did NOT (only their bus master was persisted).  That asymmetry is the
"persistence feels inconsistent" symptom owner reported.  This suite proves
the new poly/chip auto-persist path both round-trips to disk AND is actually
wired into the CC handlers -- the second half matters because CLAUDE.md records
several fixes that passed a green behavioural suite and provably did nothing,
because the control was never connected to the code under test.

Two classes of check:
  * BEHAVIOUR -- flush -> file -> load round-trips; set_cc reapply is idempotent.
  * WIRING    -- SOURCE checks that the poly_cc / chip_cc branches set the dirty
    flags, the persist thread + final flush drain both, __init__ seeds the
    pending dicts, and boot re-applies the saved CCs.  Skip-set contract:
    master (CC13) and chip coupling (72/73/76) must be EXCLUDED.

    python3 _test_patch_persist_synth.py       # run
    python3 _test_patch_persist_synth.py -v     # verbose

SAFETY: constructs PolySynth directly; all disk I/O is redirected to a temp
directory via the module-global FILE paths.  Opens no JACK client, sends no
MIDI, touches none of the real ~/.orc1_*_params.json files or the running
engine.  Safe to run on the Pi while the instrument is being played.
"""

import os
import sys
import types
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orc1_synth as S                                          # noqa: E402

EXPECTED = 43                    # a shrinking suite must go red, not say "0 failed"
VERBOSE  = "-v" in sys.argv

PASS = []
FAIL = []


def check(ok, label, detail=""):
    (PASS if ok else FAIL).append(label)
    if not ok:
        print(f"  ! {label}" + (f"  --  {detail}" if detail else ""))
    elif VERBOSE:
        print(f"  . {label}" + (f"  --  {detail}" if detail else ""))
    return ok


def _stub_engine():
    """Minimal object carrying only what the flush functions read/write."""
    return types.SimpleNamespace(
        _poly_param_pending={}, _poly_params_dirty=False,
        _chip_param_pending={}, _chip_params_dirty=False,
    )


# ---------------------------------------------------------------------------
# 1. Skip-set contract
# ---------------------------------------------------------------------------

def test_skip_sets():
    check(S._ENGINE_MASTER_CC in S._POLY_PERSIST_SKIP,
          "poly skip: master CC excluded")
    check(S._ENGINE_MASTER_CC in S._CHIP_PERSIST_SKIP,
          "chip skip: master CC excluded")
    for cc in (72, 73, 76):
        check(cc in S._CHIP_PERSIST_SKIP,
              f"chip skip: coupling CC{cc} excluded (ephemeral join)")
    # Coupling must NOT be excluded from poly -- poly has no such join, and
    # over-skipping would silently drop legitimate poly patch data.
    check(72 not in S._POLY_PERSIST_SKIP,
          "poly skip: CC72 NOT excluded (poly persists all non-master)")


# ---------------------------------------------------------------------------
# 2. Poly round-trip + merge
# ---------------------------------------------------------------------------

def test_poly_roundtrip(tmp):
    S.POLY_PARAMS_FILE = os.path.join(tmp, "poly.json")
    eng = _stub_engine()
    eng._poly_param_pending = {1: 100, 8: 40}
    eng._poly_params_dirty  = True
    S._flush_poly_params(eng)

    check(os.path.exists(S.POLY_PARAMS_FILE), "poly: file written")
    check(eng._poly_param_pending == {},      "poly: pending cleared after flush")
    check(eng._poly_params_dirty is False,    "poly: dirty cleared after flush")

    loaded = S._load_poly_params()
    check(loaded == {"1": 100, "8": 40},
          "poly: round-trips (string CC keys)", str(loaded))

    # Merge: a second flush must preserve the earlier CCs (read-modify-write).
    eng._poly_param_pending = {9: 20, 1: 64}   # 1 overwrites, 9 is new
    eng._poly_params_dirty  = True
    S._flush_poly_params(eng)
    loaded = S._load_poly_params()
    check(loaded == {"1": 64, "8": 40, "9": 20},
          "poly: merge preserves untouched + overwrites touched", str(loaded))


def test_poly_empty_pending(tmp):
    S.POLY_PARAMS_FILE = os.path.join(tmp, "poly_empty.json")
    eng = _stub_engine()
    eng._poly_params_dirty = True          # dirty but nothing pending
    S._flush_poly_params(eng)
    check(not os.path.exists(S.POLY_PARAMS_FILE),
          "poly: empty pending writes nothing")
    check(eng._poly_params_dirty is False,
          "poly: empty flush still clears dirty")


# ---------------------------------------------------------------------------
# 3. Chip round-trip + merge (nested by voice)
# ---------------------------------------------------------------------------

def test_chip_roundtrip(tmp):
    S.CHIP_PARAMS_FILE = os.path.join(tmp, "chip.json")
    eng = _stub_engine()
    eng._chip_param_pending = {"pulse1": {1: 50}, "pulse2": {8: 30}}
    eng._chip_params_dirty  = True
    S._flush_chip_params(eng)

    check(os.path.exists(S.CHIP_PARAMS_FILE), "chip: file written")
    check(eng._chip_param_pending == {},      "chip: pending cleared after flush")
    check(eng._chip_params_dirty is False,    "chip: dirty cleared after flush")

    loaded = S._load_chip_params()
    check(loaded == {"pulse1": {"1": 50}, "pulse2": {"8": 30}},
          "chip: round-trips nested by voice", str(loaded))

    # Merge: add a CC to an existing voice + a whole new voice.
    eng._chip_param_pending = {"pulse1": {8: 70}, "wave": {1: 12}}
    eng._chip_params_dirty  = True
    S._flush_chip_params(eng)
    loaded = S._load_chip_params()
    check(loaded == {"pulse1": {"1": 50, "8": 70},
                     "pulse2": {"8": 30},
                     "wave":   {"1": 12}},
          "chip: merge preserves other voices/CCs", str(loaded))


# ---------------------------------------------------------------------------
# 4. Loader robustness
# ---------------------------------------------------------------------------

def test_loader_robustness(tmp):
    S.POLY_PARAMS_FILE = os.path.join(tmp, "nope_missing.json")
    check(S._load_poly_params() == {}, "poly load: missing file -> {}")

    bad = os.path.join(tmp, "corrupt.json")
    with open(bad, "w") as f:
        f.write("{ this is not json ")
    S.POLY_PARAMS_FILE = bad
    check(S._load_poly_params() == {}, "poly load: corrupt file -> {}")

    S.CHIP_PARAMS_FILE = bad
    check(S._load_chip_params() == {}, "chip load: corrupt file -> {}")

    # A non-dict top level (e.g. a list) must be rejected, not trusted.
    listf = os.path.join(tmp, "list.json")
    with open(listf, "w") as f:
        f.write("[1, 2, 3]")
    S.CHIP_PARAMS_FILE = listf
    check(S._load_chip_params() == {}, "chip load: non-dict -> {}")


# ---------------------------------------------------------------------------
# 5. Idempotent re-apply (the whole reason replay-at-boot is safe)
# ---------------------------------------------------------------------------

def test_poly_setcc_idempotent():
    # CC1=100 -> triangle (100//32 == 3); CC8/9/11 -> ADSR times; CC10 sustain.
    saved = {1: 100, 8: 40, 9: 55, 10: 90, 11: 30, 74: 60, 71: 20, 75: 100}
    a = S.PolySynth()
    b = S.PolySynth()
    for cc, v in saved.items():
        a.set_cc(cc, v)
    # apply twice to b to prove re-applying the same CCs is a no-op the 2nd time
    for _ in range(2):
        for cc, v in saved.items():
            b.set_cc(cc, v)
    check(a.waveform == b.waveform == 'triangle',
          "poly set_cc: waveform reproduced + idempotent", f"{a.waveform}/{b.waveform}")
    check(abs(a.attack  - b.attack)  < 1e-9, "poly set_cc: attack reproduced")
    check(abs(a.decay   - b.decay)   < 1e-9, "poly set_cc: decay reproduced")
    check(abs(a.sustain - b.sustain) < 1e-9, "poly set_cc: sustain reproduced")
    check(abs(a.release - b.release) < 1e-9, "poly set_cc: release reproduced")
    # A fresh engine that never saw the CCs must differ (proves the CCs matter).
    fresh = S.PolySynth()
    check(fresh.waveform != 'triangle' or abs(fresh.attack - a.attack) > 1e-9,
          "poly set_cc: fresh engine differs from restored (CCs are load-bearing)")


# ---------------------------------------------------------------------------
# 6. WIRING -- source-level nets (mutation-proof the control is connected)
# ---------------------------------------------------------------------------

def test_source_wiring():
    # Repointed 2026-09-24: these nets used to grep orc1_synth.py's SynthEngine,
    # the retired single-process synth (removed that day). The LIVE persistence
    # wiring lives in the poly + chip HOSTS, which reuse the S._flush_* /
    # S._load_* helpers exercised above -- so that is what these now guard.
    def _src(name):
        return open(os.path.join(HERE, name), encoding="utf-8").read()
    ph = _src("orc1_poly_host.py")
    ch = _src("orc1_chip_host.py")

    # __init__ seeds the pending dicts (must exist before the MIDI thread runs).
    check("self._poly_param_pending = {}" in ph, "wiring: poly host __init__ seeds poly pending")
    check("self._chip_param_pending = {}" in ch, "wiring: chip host __init__ seeds chip pending")

    # poly_cc drain branch records non-master CCs.
    i = ph.find("kind == 'poly_cc':")
    seg = ph[i:i + 1000] if i >= 0 else ""
    check("self._poly_param_pending[cc] = val" in seg,
          "wiring: poly_cc records pending")
    check("self._poly_params_dirty = True" in seg,
          "wiring: poly_cc sets dirty")

    # chip_cc drain branch records non-coupling, non-master CCs.
    j = ch.find("elif kind == 'chip_cc':")
    segc = ch[j:j + 1200] if j >= 0 else ""
    check("cc not in S._CHIP_PERSIST_SKIP" in segc,
          "wiring: chip_cc guards on skip set")
    check("self._chip_param_pending.setdefault(voice, {})[cc] = val" in segc,
          "wiring: chip_cc records pending")
    check("self._chip_params_dirty = True" in segc,
          "wiring: chip_cc sets dirty")

    # each host's persist thread drains its own params.
    k = ph.find("def persist_thread")
    check(k >= 0 and "S._flush_poly_params(eng)" in ph[k:k + 700],
          "wiring: poly host persist thread drains poly")
    k = ch.find("def persist_thread")
    check(k >= 0 and "S._flush_chip_params(eng)" in ch[k:k + 700],
          "wiring: chip host persist thread drains chip")

    # final shutdown flush (in each host's main) drains too.
    m = ph.find("def main(")
    check(m >= 0 and "S._flush_poly_params(eng)" in ph[m:],
          "wiring: poly host final flush drains poly")
    m = ch.find("def main(")
    check(m >= 0 and "S._flush_chip_params(eng)" in ch[m:],
          "wiring: chip host final flush drains chip")

    # boot re-applies saved CCs through set_cc.
    check("S._load_poly_params().items()" in ph, "wiring: poly host boot loads poly params")
    check("self.poly.set_cc(_cc, int(_val))" in ph, "wiring: poly host boot applies via set_cc")
    check("S._load_chip_params().items()" in ch, "wiring: chip host boot loads chip params")
    check("self.chip.set_cc(_voice, _cc, int(_val))" in ch,
          "wiring: chip host boot applies via set_cc")


# ---------------------------------------------------------------------------

def main():
    tmp = tempfile.mkdtemp(prefix="orc_patchpersist_")
    # Snapshot + restore the module globals so we never point at the real files.
    _saved = (S.POLY_PARAMS_FILE, S.CHIP_PARAMS_FILE)
    try:
        test_skip_sets()
        test_poly_roundtrip(tmp)
        test_poly_empty_pending(tmp)
        test_chip_roundtrip(tmp)
        test_loader_robustness(tmp)
        test_poly_setcc_idempotent()
        test_source_wiring()
    finally:
        S.POLY_PARAMS_FILE, S.CHIP_PARAMS_FILE = _saved

    total = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed  (of {total})")
    if total != EXPECTED:
        print(f"  ! SUITE SIZE CHANGED: expected {EXPECTED}, ran {total} "
              f"-- a vanished check is a silent regression")
        return 2
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
