#!/usr/bin/env python3
"""_test_drum_fix.py — 2026-08-17 drum fixes regression suite.

Covers two fixes made together:
  1. The Cymbal voice ignores filter_freq/filter_res (no low-pass). The
     cymbal.wav has no energy above ~8 kHz and the SampleVoice SVF goes
     unstable at high cutoffs (a saved filter_freq=127 turned the crash into a
     sub-bass blob, peak ~5.0). Cymbal.set_param must drop those params and
     keep lp_filt permanently bypassed.
  2. The Fire drum editor now seeds _voice_vals from ~/.orc1_drum_params.json
     (the synth re-applies that file at boot). The stored CC is post-curve; the
     editor holds a pre-curve display value, so the seed inverts the curve
     (log<->exp, linear identity). This suite AST-extracts the REAL
     FireHandler._apply_curve and proves the invert->resend round-trip is exact.

Run:  python3 _test_drum_fix.py [-v]
"""
import os
import sys
import ast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXPECTED = 32
_PASS = 0
_FAIL = 0
_VERBOSE = "-v" in sys.argv

def check(cond, label):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        if _VERBOSE:
            print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


# ── 1. Cymbal: filter is a no-op, lp_filt stays bypassed ─────────────────────
def t_cymbal_filter_noop():
    import numpy as np
    import orc1_synth as S
    cy = S.Cymbal()
    check(cy.lp_filt._bypass is True, "cymbal lp_filt bypassed at construction")
    base_cutoff = cy._lp_cutoff
    # The exact saved values that broke it.
    cy.set_param("filter_freq", 127)
    cy.set_param("filter_res", 18)
    check(cy.lp_filt._bypass is True, "filter_freq/res do NOT un-bypass the cymbal")
    check(cy._lp_cutoff == base_cutoff, "filter_freq does not move the cymbal cutoff")
    # Non-filter params still work.
    cy.set_param("level", 100)
    cy.set_param("decay", 90)
    check(True, "level/decay set_param on cymbal does not raise")
    # Render with the broken saved filter applied: output must be sane (the bug
    # produced peak ~5.0). Trigger and render the tail.
    cy.trigger(120)
    peak = 0.0
    for _ in range(200):
        buf = np.asarray(cy.render(512), dtype=np.float32)
        if buf.size:
            peak = max(peak, float(np.abs(buf).max()))
        if not cy.active:
            break
    check(peak <= 1.5, f"cymbal render stays sane with saved filter (peak {peak:.3f} <= 1.5)")
    check(peak > 0.05, f"cymbal actually produced sound (peak {peak:.3f} > 0.05)")

    # A normal sample voice (not cymbal) STILL honours the filter -- the fix is
    # scoped to Cymbal only.
    sv = S.SampleVoice()
    sv.set_param("filter_freq", 40)
    check(sv.lp_filt._bypass is False, "plain SampleVoice filter_freq DOES engage (fix is cymbal-only)")


# ── 2. Fire curve round-trip (real _apply_curve via ast) ─────────────────────
def _extract_apply_curve():
    """Pull FireHandler._apply_curve out of orc1_fire.py by source text and
    exec it in isolation -- same technique as the chord suites, so we test the
    DEPLOYED function without importing the whole Fire handler."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "orc1_fire.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_curve":
            seg = ast.get_source_segment(src, node)
            # strip the @staticmethod decorator line if present and dedent
            import textwrap
            seg = textwrap.dedent(seg)
            ns = {}
            exec(seg, ns)
            return ns["_apply_curve"]
    raise RuntimeError("_apply_curve not found in orc1_fire.py")

def t_curve_roundtrip():
    apply_curve = _extract_apply_curve()
    check(apply_curve(0, "log") == 0, "curve(0) == 0")
    check(apply_curve(127, "log") == 127, "curve(127) == 127")
    check(apply_curve(64, "linear") == 64, "linear curve is identity")

    INV = {"log": "exp", "exp": "log", "linear": "linear"}
    # The property that matters: a value the Fire knob can actually PRODUCE must
    # survive save+reload. The user dials a display value d -> the synth is sent
    # c = curve(d, mode) -> the file stores c. On reload the seed recovers
    # d' = curve(c, INV[mode]); if the user then re-sends, the synth must land
    # back on the SAME c. So iterate over reachable display values d (not over
    # arbitrary CCs -- a log knob leaves gaps, e.g. no display reaches CC 1..17,
    # so a hand-edited/web-set CC in a gap is display-approximate by nature; the
    # boot seed never re-sends, so the synth keeps its exact saved value anyway).
    modes = ["log", "exp", "linear"]
    for mode in modes:
        inv = INV[mode]
        worst = 0
        for d in range(128):
            c      = apply_curve(d, mode)      # what the synth was sent + saved
            d_back = apply_curve(c, inv)       # what the reload seeds for display
            c2     = apply_curve(d_back, mode) # what a re-send would produce
            worst  = max(worst, abs(c2 - c))
        check(worst <= 1, f"{mode}: reachable value survives save/reload within 1 LSB (worst {worst})")

    # Spot-check the exact saved values that were reverting.
    # decay is 'log'; saved 111 must resend to 111 through the display value.
    disp = apply_curve(111, "exp")
    check(apply_curve(disp, "log") == 111, "decay 111 round-trips exactly")
    disp = apply_curve(28, "exp")
    check(apply_curve(disp, "log") == 28, "filter_freq 28 round-trips exactly")
    # linear params are shown as-is.
    check(apply_curve(58, "linear") == 58, "tune 58 shown as-is (linear)")


# ── 3. The seeding code actually exists in orc1_fire.py (wiring net) ──────────
def t_seeding_wired():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "orc1_fire.py"), encoding="utf-8").read()
    check("_load_drum_params" in src,
          "orc1_fire imports the synth's saved-param loader")
    check("_INV_CURVE" in src, "orc1_fire defines the inverse-curve map")
    # The overlay must write into _voice_vals (the display cache the OLED reads).
    check("_slot[_p] = self._apply_curve" in src,
          "seed overlays inverted values into the _voice_vals slot")
    # The cymbal filter must be suppressed at the editor level so the sculpt
    # screen shows "--" (the knob is inert now). Assert the guard exists inside
    # _voice_has_param, not just anywhere in the file.
    fn_src = src[src.index("def _voice_has_param"):]
    fn_src = fn_src[:fn_src.index("\n    def ", 1)]
    check('voice == "cymbal"' in fn_src and "filter_freq" in fn_src
          and "return False" in fn_src,
          "_voice_has_param suppresses the cymbal filter (shows '--')")


# -- 4. RIDE (2026-09-23): the 909's missing voice ------------------------------
def t_ride_voice():
    """The 909's own ride: notes 51/53/59 -> ride (were folded onto the crash),
    appended LAST in the CC map so no existing voice's CCs moved, plays
    samples/ride.wav, keeps the crash's no-low-pass rule, and is a real drum-host
    voice that the one-shot slots never swallow. Mutation: map 51 back to
    "cymbal" / insert ride before the slots / drop it from the host -> fails."""
    import orc1_config as cfg
    import orc1_synth as S
    m = S.DRUM_NOTE_MAP
    check(all(m.get(n) == "ride" for n in (51, 59))
          and m.get(49) == "cymbal" and m.get(57) == "cymbal",
          "notes 51/59 -> ride (53 = its own ride bell since the voices pass), 49/57 crash")
    L = cfg.DRUM_CC["LAYOUT"]
    check(L["ride"]["start"] == 103 and L["ride"]["params"] == ["level", "tune", "attack", "decay"]
          and L["cowbell"]["start"] == 73 and L["smpl_0"]["start"] == 79
          and L["smpl_3"]["start"] == 97
          and list(cfg.DRUM_CC["VOICE_PARAMS"]).index("ride")
              > list(cfg.DRUM_CC["VOICE_PARAMS"]).index("smpl_3"),
          "ride after every pre-existing voice (CCs 103-106); their CCs unchanged")
    r = S.Ride()
    check(r.name == "ride" and str(getattr(r, "_loaded_path", "")).endswith("ride.wav")
          and len(r._data) > 48000,
          "Ride plays samples/ride.wav (a real, multi-second sample)")
    r.set_param("filter_freq", 127); r.set_param("filter_res", 127)
    check(r.lp_filt._bypass is True, "ride ignores filter CCs (no unstable low-pass)")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "orc1_drum_host.py"), encoding="utf-8").read()
    check('"ride":       S.Ride()' in src and "not in S.KIT_SAMPLE_FILES" in src
          and "ride.wav" in S.KIT_SAMPLE_FILES,
          "drum host builds the ride voice and keeps ride.wav out of the one-shot slots")
    check("ride" in cfg.DRUM_CC["EDIT_VOICES"],
          "ride is editable (Fire drum editor + /drums)")


# -- 5. More kit voices (2026-09-23): ride bell, splash, hi-mid tom ------------------
def t_more_voices():
    """owner: have the voices rather than fold. Each new voice on its own GM
    note, appended LAST (nothing moved), sampled ones play their own file and are
    never one-shot slots, and the hi-mid tom is pitched BETWEEN mid and hi."""
    import orc1_config as cfg
    import orc1_synth as S
    m = S.DRUM_NOTE_MAP
    check(m.get(53) == "ride_bell" and m.get(55) == "splash" and m.get(48) == "himid_tom"
          and m.get(41) == "low_tom" and m.get(43) == "mid_tom" and m.get(50) == "hi_tom",
          "53 ride bell, 55 splash, 48 hi-mid tom; 41/43/50 toms unchanged")
    L = cfg.DRUM_CC["LAYOUT"]
    starts = {v: L[v]["start"] for v in ("cowbell", "smpl_3", "ride", "ride_bell",
                                         "splash", "himid_tom")}
    check(starts == {"cowbell": 73, "smpl_3": 97, "ride": 103, "ride_bell": 107,
                     "splash": 111, "himid_tom": 115}
          and max(cfg.DRUM_CC["CC_TO_VP"]) == 118,
          "new voices appended last (107/111/115), nothing moved, top CC 118 (< 120)")
    b, sp = S.RideBell(), S.Splash()
    check(b.name == "ride_bell" and sp.name == "splash"
          and str(b._loaded_path).endswith("ride_bell.wav")
          and str(sp._loaded_path).endswith("splash.wav")
          and len(b._data) > 48000 and len(sp._data) > 48000,
          "ride bell + splash play their own real samples")
    check({"ride_bell.wav", "splash.wav"} <= set(S.KIT_SAMPLE_FILES),
          "their samples are never taken as one-shot slots")
    D = S.Tom._DEFAULTS
    check(D["mid"][0] < D["himid"][0] < D["hi"][0] and D["mid"][1] < D["himid"][1] < D["hi"][1]
          and S.Tom("himid").name == "himid_tom",
          "hi-mid tom is pitched between the mid and hi toms")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "orc1_drum_host.py"), encoding="utf-8").read()
    check('S.RideBell()' in src and 'S.Splash()' in src and "S.Tom('himid')" in src
          and all(v in cfg.DRUM_CC["EDIT_VOICES"] for v in ("ride_bell", "splash", "himid_tom")),
          "drum host builds all three; all three are editable")


TESTS = [t_cymbal_filter_noop, t_curve_roundtrip, t_seeding_wired, t_ride_voice,
         t_more_voices]

if __name__ == "__main__":
    for fn in TESTS:
        if _VERBOSE:
            print(f"\n{fn.__name__}")
        fn()
    total = _PASS + _FAIL
    print(f"\n{_PASS} passed, {_FAIL} failed  (total {total})")
    if total != EXPECTED:
        print(f"WARNING: EXPECTED {EXPECTED}, ran {total} -- a net may have changed.")
        sys.exit(2)
    sys.exit(1 if _FAIL else 0)
