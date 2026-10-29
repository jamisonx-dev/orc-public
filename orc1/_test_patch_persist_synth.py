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

EXPECTED = 66                    # a shrinking suite must go red, not say "0 failed"
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
    # Per-voice CCs only here (1 = duty, 77 = vibrato depth); the engine-wide
    # CCs have their own tests below (they live under "_engine").
    eng._chip_param_pending = {"pulse1": {1: 50}, "pulse2": {77: 30}}
    eng._chip_params_dirty  = True
    S._flush_chip_params(eng)

    check(os.path.exists(S.CHIP_PARAMS_FILE), "chip: file written")
    check(eng._chip_param_pending == {},      "chip: pending cleared after flush")
    check(eng._chip_params_dirty is False,    "chip: dirty cleared after flush")

    loaded = S._load_chip_params()
    check(loaded == {"pulse1": {"1": 50}, "pulse2": {"77": 30}},
          "chip: round-trips nested by voice", str(loaded))

    # Merge: add a CC to an existing voice + a whole new voice.
    eng._chip_param_pending = {"pulse1": {77: 70}, "wave": {1: 12}}
    eng._chip_params_dirty  = True
    S._flush_chip_params(eng)
    loaded = S._load_chip_params()
    check(loaded == {"pulse1": {"1": 50, "77": 70},
                     "pulse2": {"77": 30},
                     "wave":   {"1": 12}},
          "chip: merge preserves other voices/CCs", str(loaded))


# ---------------------------------------------------------------------------
# 3b. ENGINE-WIDE chip CCs (2026-10-26): one "_engine" key, not per voice
# ---------------------------------------------------------------------------

def _snap(obj, path="", depth=0, out=None, seen=None):
    """Flatten an engine's scalar state into {path: value} (numpy arrays and
    callables skipped) so two engines can be compared field by field."""
    if out is None:
        out, seen = {}, set()
    if id(obj) in seen or depth > 5:
        return out
    if isinstance(obj, (bool, int, float, str, type(None))):
        out[path] = round(obj, 9) if isinstance(obj, float) else obj
        return out
    seen.add(id(obj))
    if isinstance(obj, dict):
        for k, v in obj.items():
            _snap(v, f"{path}[{k!r}]", depth + 1, out, seen)
    elif isinstance(obj, (list, tuple)):
        if len(obj) <= 64:
            for i, v in enumerate(obj):
                _snap(v, f"{path}[{i}]", depth + 1, out, seen)
    elif not callable(obj) and (hasattr(obj, "__dict__") or hasattr(obj, "__slots__")):
        items = dict(vars(obj)) if hasattr(obj, "__dict__") else {}
        for k in getattr(type(obj), "__slots__", ()):     # e.g. RingMod
            if hasattr(obj, k):
                items[k] = getattr(obj, k)
        for k, v in items.items():
            _snap(v, f"{path}.{k}", depth + 1, out, seen)
    return out


_CHIP_TEST_VOICES = tuple(S.NESEngine._VOICES) + ("pcm",)


def test_engine_set_matches_code():
    """The engine-wide set is DERIVED from NESEngine.set_cc, not trusted: a CC is
    engine-wide on voice v iff sending it on v gives exactly the same non-default
    engine state as sending it on some other voice."""
    base = _snap(S.NESEngine())
    wrong_engine, missed_engine = [], []
    for cc in range(128):
        if cc in S._CHIP_PERSIST_SKIP:
            continue
        changed = {v: False for v in _CHIP_TEST_VOICES}
        shared  = {v: True for v in _CHIP_TEST_VOICES}
        for val in (100, 30, 127):
            snaps = {}
            for v in _CHIP_TEST_VOICES:
                e = S.NESEngine()
                e.set_cc(v, cc, val)
                snaps[v] = _snap(e)
            for v in _CHIP_TEST_VOICES:
                changed[v] |= snaps[v] != base
                shared[v] &= any(snaps[v] == snaps[u] for u in _CHIP_TEST_VOICES
                                 if u != v)
        for v in _CHIP_TEST_VOICES:
            actual = changed[v] and shared[v]
            classed = S._chip_cc_is_engine(v, cc)
            if classed and not actual:
                wrong_engine.append((v, cc, 0))
            if actual and not classed:
                missed_engine.append((v, cc, 0))
    check(not wrong_engine,
          "engine set: every CC filed under _engine really is voice-independent",
          str(sorted(set((v, c) for v, c, _ in wrong_engine))[:20]))
    check(not missed_engine,
          "engine set: every voice-independent CC is filed under _engine",
          str(sorted(set((v, c) for v, c, _ in missed_engine))[:20]))
    check(S._chip_cc_is_engine("wave", 12) and not S._chip_cc_is_engine("wave", 9)
          and S._chip_cc_is_engine("noise", 9),
          "engine set: wave shares ring mod but keeps its own ADSR")


def test_chip_engine_flush(tmp):
    S.CHIP_PARAMS_FILE = os.path.join(tmp, "chip_engine.json")
    eng = _stub_engine()
    eng._chip_param_pending = {S._CHIP_ENGINE_KEY: {74: 20}, "pulse1": {1: 64}}
    S._flush_chip_params(eng)
    # A voice-keyed engine-wide CC (an older caller) is filed under _engine too.
    eng._chip_param_pending = {"pulse2": {74: 99}}
    S._flush_chip_params(eng)
    loaded = S._load_chip_params()
    check(loaded == {S._CHIP_ENGINE_KEY: {"74": 99}, "pulse1": {"1": 64}},
          "chip flush: engine-wide CC kept once under _engine, last write wins",
          str(loaded))


# The exact ~/.orc1_chip_params.json found on the Pi 2026-10-26.
PI_SHAPE = {
    "wave": {"16": 0, "23": 0, "1": 89, "7": 102, "8": 28, "9": 125, "78": 92, "121": 0},
    "pulse1": {"8": 0, "9": 63, "10": 80, "11": 57, "74": 53, "12": 75, "64": 0,
               "75": 0, "85": 0, "71": 31, "79": 102, "121": 0, "7": 102},
    "noise": {"3": 0, "9": 88, "8": 0, "71": 0, "74": 46, "121": 0, "7": 0},
    "triangle": {"64": 0, "74": 42, "121": 0, "7": 102},
    "pulse2": {"64": 0, "74": 0, "71": 127, "1": 0, "75": 78, "70": 111, "11": 37,
               "8": 0, "9": 0, "121": 0, "7": 0},
    "pcm": {"121": 0},
}
# What the live file migrates to: per engine-wide CC, the LAST voice in file
# order that holds it (wave, pulse1, noise, triangle, pulse2, pcm) = what the
# old boot replay ended on = what owner hears today.
PI_ENGINE = {"8": 0, "9": 0, "10": 80, "11": 37, "74": 0, "12": 75,
             "75": 78, "85": 0, "71": 127, "70": 111}


def _old_replay(data):
    """The chip host's boot replay as it was BEFORE 2026-10-26 (copied from the
    replaced code): walk the loaded file in its own order, set_cc every CC on
    the voice it was saved under. The yardstick a migration must reproduce."""
    e = S.NESEngine()
    for _voice, _ccs in data.items():
        if not isinstance(_ccs, dict):
            continue
        for _cc, _val in _ccs.items():
            try:
                _cc = int(_cc)
                if _cc in S._CHIP_PERSIST_SKIP:
                    continue
                e.set_cc(_voice, _cc, int(_val))
            except Exception:
                pass
    e._wave.state_dirty = False         # the host clears it after replay
    return e


def _new_replay(mig):
    """Replay a MIGRATED dict the way the chip host now does."""
    e = S.NESEngine()
    for v, ccs in mig.items():
        if not isinstance(ccs, dict):
            continue
        av = S._CHIP_ENGINE_APPLY_VOICE if v == S._CHIP_ENGINE_KEY else v
        for c, val in ccs.items():
            if int(c) not in S._CHIP_PERSIST_SKIP:
                e.set_cc(av, int(c), int(val))
    e._wave.state_dirty = False
    return e


def _diff(e1, e2):
    a, b = _snap(e1), _snap(e2)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def test_migration_pi_shape(tmp):
    import copy
    import json
    import random
    src = copy.deepcopy(PI_SHAPE)
    out, dropped = S._chip_params_migrate(src)
    check(src == PI_SHAPE, "migrate: input not mutated")
    check(out.get(S._CHIP_ENGINE_KEY) == PI_ENGINE,
          "migrate Pi shape: _engine = the last voice in file order (what plays today)",
          str(out.get(S._CHIP_ENGINE_KEY)))
    lost = [(v, c) for v, ccs in out.items() if v != S._CHIP_ENGINE_KEY
            for c in ccs if S._chip_cc_is_engine(v, int(c))]
    check(not lost, "migrate Pi shape: no engine-wide copy left under a voice", str(lost))
    untouched = all(out[v][c] == val for v, ccs in PI_SHAPE.items()
                    for c, val in ccs.items() if not S._chip_cc_is_engine(v, int(c)))
    check(untouched and out["wave"] == PI_SHAPE["wave"],
          "migrate Pi shape: every per-voice value kept (incl. wave's own ADSR)")
    check(sorted(dropped) == sorted([
        ("pulse1", 8, 0), ("pulse1", 9, 63), ("pulse1", 11, 57), ("pulse1", 74, 53),
        ("pulse1", 75, 0), ("pulse1", 71, 31), ("noise", 8, 0), ("noise", 9, 88),
        ("noise", 71, 0), ("noise", 74, 46), ("triangle", 74, 42)]),
          "migrate Pi shape: the overridden copies are reported", str(dropped))
    again, d2 = S._chip_params_migrate(out)
    check(again == out and not d2, "migrate: idempotent on a migrated file")
    # THE contract: the migrated file plays exactly what the old replay played,
    # for the live file, its reverse, and shuffled voice / CC orders.
    rng = random.Random(1026)
    cases = [("live file", PI_SHAPE), ("reversed", dict(reversed(list(PI_SHAPE.items()))))]
    for i in range(6):
        vs = list(PI_SHAPE.items())
        rng.shuffle(vs)
        sh = {}
        for v, ccs in vs:
            items = list(ccs.items())
            rng.shuffle(items)
            sh[v] = dict(items)
        cases.append((f"shuffle {i}", sh))
    bad = []
    for label, data in cases:
        d = _diff(_new_replay(S._chip_params_migrate(data)[0]), _old_replay(data))
        if d:
            bad.append((label, d[:6]))
    check(not bad, "migrate: engine == what the OLD boot replay produced (8 file orders)",
          str(bad))
    check(_diff(_new_replay(out), S.NESEngine()) != [],
          "migrate: the compared state is non-trivial (differs from a fresh engine)")
    # An existing _engine value always beats an old per-voice copy.
    c = S._chip_params_migrate({S._CHIP_ENGINE_KEY: {"74": 7},
                                "pulse1": {"74": 53}})[0]
    check(c == {S._CHIP_ENGINE_KEY: {"74": 7}, "pulse1": {}},
          "migrate: an existing _engine value wins", str(c))
    # A flush also rewrites an old file in the new shape.
    S.CHIP_PARAMS_FILE = os.path.join(tmp, "chip_pi.json")
    with open(S.CHIP_PARAMS_FILE, "w") as f:
        json.dump(PI_SHAPE, f)
    eng = _stub_engine()
    eng._chip_param_pending = {"pulse1": {77: 5}}
    S._flush_chip_params(eng)
    disk = S._load_chip_params()
    exp = copy.deepcopy(out)
    exp["pulse1"]["77"] = 5
    check(disk == exp, "migrate: a flush writes the migrated file", str(disk)[:300])


def _host_sandbox(tmp, name):
    S.CHIP_PARAMS_FILE   = os.path.join(tmp, name)
    S.BUS_LEVELS_FILE    = os.path.join(tmp, name + ".bus")
    S.GB_WAVE_STATE_FILE = os.path.join(tmp, name + ".wave")


def test_chip_host_restart(tmp):
    """End to end through the REAL chip host: a value set on voice B after voice A
    must be the one a restarted host plays."""
    import json
    import orc1_chip_host as H
    _host_sandbox(tmp, "chip_host.json")
    moves = [("pulse1", 74, 20), ("pulse2", 74, 99),      # filter: B after A
             ("noise", 75, 100), ("pulse1", 75, 30),      # octave: A after B
             ("triangle", 12, 90),                        # ring depth
             ("pulse2", 1, 0), ("pulse1", 1, 100),        # per-voice duty
             ("wave", 9, 125), ("noise", 9, 10)]          # wave ADSR vs engine
    h = H.ChipHost()
    for v, cc, val in moves:
        h.chip_cc(v, cc, val)
    h.render(64)
    S._flush_chip_params(h)
    disk = S._load_chip_params()
    check(disk.get(S._CHIP_ENGINE_KEY) == {"74": 99, "75": 30, "12": 90, "9": 10},
          "host: engine-wide CCs saved once, last value set", json.dumps(disk))
    check(disk.get("pulse2") == {"1": 0} and disk.get("pulse1") == {"1": 100}
          and disk.get("wave") == {"9": 125},
          "host: per-voice CCs saved under their voice", json.dumps(disk))
    h2 = H.ChipHost()                       # the 'restart'
    ref = S.NESEngine()
    for v, cc, val in moves:
        ref.set_cc(v, cc, val)
    d = _diff(h2.chip, ref)
    check(not d, "host: restarted engine == engine that saw the live moves", str(d[:10]))
    # An OLD-shape file: the real host boots to what the OLD replay played, and
    # rewrites the file in the _engine shape AT BOOT (no CC needed).
    for label, data in (("file order", PI_SHAPE),
                        ("reversed", dict(reversed(list(PI_SHAPE.items()))))):
        with open(S.CHIP_PARAMS_FILE, "w") as f:
            json.dump(data, f)
        hb = H.ChipHost()
        d = _diff(hb.chip, _old_replay(data))
        check(not d, f"host: old Pi file boots to the old replay's sound ({label})",
              str(d[:10]))
        disk = S._load_chip_params()
        check(disk == S._chip_params_migrate(data)[0]
              and isinstance(disk.get(S._CHIP_ENGINE_KEY), dict),
              f"host: boot rewrote the old file in the _engine shape ({label})",
              json.dumps(disk)[:300])
    # A migrated file is left alone at boot (no rewrite).
    m0 = os.stat(S.CHIP_PARAMS_FILE).st_mtime_ns
    with open(S.CHIP_PARAMS_FILE) as f:
        before = f.read()
    os.utime(S.CHIP_PARAMS_FILE, ns=(m0 - 10**9, m0 - 10**9))
    H.ChipHost()
    with open(S.CHIP_PARAMS_FILE) as f:
        after = f.read()
    check(after == before and os.stat(S.CHIP_PARAMS_FILE).st_mtime_ns == m0 - 10**9,
          "host: an already-migrated file is not rewritten at boot")


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
    check("self._chip_param_pending.setdefault(_pk, {})[cc] = val" in segc
          and "S._CHIP_ENGINE_KEY if S._chip_cc_is_engine(voice, cc) else voice" in segc,
          "wiring: chip_cc records pending (engine-wide CCs under _engine)")
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
    check("_chip_raw = S._load_chip_params()" in ch
          and "S._chip_params_migrate(_chip_raw)" in ch
          and "S._write_chip_params(_chip_saved)" in ch,
          "wiring: chip host boot loads + migrates + rewrites chip params")
    check("self.chip.set_cc(_apply_v, _cc, int(_val))" in ch,
          "wiring: chip host boot applies via set_cc")


# ---------------------------------------------------------------------------

def _md5(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def main():
    tmp = tempfile.mkdtemp(prefix="orc_patchpersist_")
    # Snapshot + restore the module globals so we never point at the real files.
    _saved = (S.POLY_PARAMS_FILE, S.CHIP_PARAMS_FILE, S.BUS_LEVELS_FILE,
              S.GB_WAVE_STATE_FILE)
    real = [os.path.expanduser(p) for p in
            ("~/.orc1_chip_params.json", "~/.orc1_poly_params.json",
             "~/.orc1_bus_levels.json", "~/.orc1_gb_wave.json")]
    before = [_md5(p) for p in real]
    try:
        test_skip_sets()
        test_poly_roundtrip(tmp)
        test_poly_empty_pending(tmp)
        test_chip_roundtrip(tmp)
        test_engine_set_matches_code()
        test_chip_engine_flush(tmp)
        test_migration_pi_shape(tmp)
        test_chip_host_restart(tmp)
        test_loader_robustness(tmp)
        test_poly_setcc_idempotent()
        test_source_wiring()
    finally:
        (S.POLY_PARAMS_FILE, S.CHIP_PARAMS_FILE, S.BUS_LEVELS_FILE,
         S.GB_WAVE_STATE_FILE) = _saved
    check([_md5(p) for p in real] == before,
          "safety: the real chip/poly/bus/wave files are byte-identical")

    total = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed  (of {total})")
    if total != EXPECTED:
        print(f"  ! SUITE SIZE CHANGED: expected {EXPECTED}, ran {total} "
              f"-- a vanished check is a silent regression")
        return 2
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
