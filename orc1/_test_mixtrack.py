#!/usr/bin/env python3
"""
ORC-1 Mixtrack handler -- headless test harness + regression suite.
===================================================================

WHY THIS EXISTS
---------------
Same reason as _test_fire.py, one controller over.  orc1_mixtrack.py is a
1200-line dispatch surface whose every action is reached by getattr() on a
string from orc1_config.py, and whose dispatch wraps every action in a bare
`except Exception` that prints one line and continues.  A typo in a config
map, a renamed method, or a layer that shadows another produces a DEAD
CONTROL with no exception and nothing to grep for.  The Mixtrack is also
frequently unplugged, so bugs in it have historically shipped untested for
months.

MixtrackHandler CAN be constructed off-hardware: __init__ touches only
eng.cfg.  Stub the MIDI output, the LED wake flourish and the FX-LED poller
and the whole handler runs in-process against synthetic MIDI.

    python3 _test_mixtrack.py          # run the suite
    python3 _test_mixtrack.py -v       # also list every reachable mapping

Pure stdlib apart from mido/orc1_config, which the Pi already has.


SAFETY -- READ BEFORE EDITING
-----------------------------
This suite is designed to be safe to run WHILE THE INSTRUMENT IS PLAYING.
Four things make that true.  Do not remove any of them:

  1. `_open_output` is overridden to a no-op.  The REAL one opens any MIDI
     output whose name contains "mixtrack" -- on this rig that is the actual
     Numark Mixtrack Pro 3, and an unstubbed run would blast LED note-ons at
     a controller someone is playing.
  2. `wake()` is overridden to a no-op.  The real one sweeps every LED on
     both decks with a 8ms sleep each (~0.5s of MIDI spray) before settling.
  3. `_fx_led_loop` is overridden to return immediately, so the poller
     thread __init__ starts dies at once instead of running for the life of
     the process.
  4. The handler is given a FakeEng.  NOTHING here reaches the real engine,
     the JACK graph, the FX rack, or any file.  The Mixtrack handler does no
     disk I/O of its own, so unlike the Fire suite no HOME sandbox is needed
     -- but if that ever changes, add one (see _test_fire.py items 3/6/7 and
     the 2026-07-31 knobmap clobber in CLAUDE.md).

Nothing here restarts a service or touches the Pi's live state.
"""

import os
import sys
import types
import inspect
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orc1_config as cfg                                    # noqa: E402

try:
    import mido
except Exception:                                            # pragma: no cover
    mido = None

import orc1_mixtrack as MT                                   # noqa: E402


# ---------------------------------------------------------------------------
# Engine stubs
# ---------------------------------------------------------------------------

class FakeRack:
    """3-slot FX rack stub.  Real enough that _act_fx_toggle_slot and
    _cc_fx_rack_param run their whole body instead of early-returning on a
    None rack (which would make them look tested when they are not)."""

    def __init__(self):
        self.slots = [
            {"running": False, "controls": [
                {"symbol": "wet", "min": 0.0, "max": 1.0}]},
            {"running": False, "controls": [
                {"symbol": "gain", "min": -20.0, "max": 20.0}]},
            {"running": False, "controls": []},      # plugin with no params
        ]
        self.calls = []

    def status(self):
        return {"slots": self.slots}

    def toggle(self, i, done=None):
        self.calls.append(("toggle", i))
        self.slots[i]["running"] = not self.slots[i]["running"]
        if done:
            done()

    def param(self, slot, symbol, val):
        self.calls.append(("param", slot, symbol, round(float(val), 4)))


class FakeArp:
    """Arp stub.  Real values where the handler compares or does arithmetic
    on them -- a permissive __getattr__ no-op returns a function there and
    the action dies with a fake failure that looks like a product bug."""

    def __init__(self):
        self.enabled       = False
        self.pattern_name  = "UP"
        self.current_chord = None
        self.internal_bpm  = 120.0
        self.drift_bpm     = 120.0
        try:
            import orc1_arp
            self.clock_mode = orc1_arp.CLOCK_INTERNAL
        except Exception:
            self.clock_mode = "internal"
        self.calls = []

    def _estimated_bpm(self):
        return 120.0

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.calls.append(("set_enabled", bool(on)))

    def set_clock_mode(self, mode):
        self.clock_mode = mode
        self.calls.append(("set_clock_mode", mode))

    def set_pattern(self, name, patterns):
        self.pattern_name = name
        self.calls.append(("set_pattern", name))

    def set_internal_bpm(self, bpm):
        self.internal_bpm = float(bpm)
        self.calls.append(("set_internal_bpm", round(float(bpm), 2)))

    def set_drift_bpm(self, bpm):
        self.drift_bpm = float(bpm)
        self.calls.append(("set_drift_bpm", round(float(bpm), 2)))

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _stub(*a, **k):
            self.calls.append((name, a))
            return None
        return _stub


class FakeEng:
    """Stand-in for the orc1 __main__ module the handler is handed.

    Facades the handler only calls for side effects resolve to a recording
    no-op.  Anything it does ARITHMETIC or len() on gets a real value.
    """

    def __init__(self):
        self.cfg  = cfg
        self.arp  = FakeArp()
        self.fx   = types.SimpleNamespace(rack=FakeRack())
        self.calls = []                     # (name, args) every facade call
        self.ROOT_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G",
                           "Ab", "A", "Bb", "B"]
        self.state = {
            "root": 0, "chord_root": 0, "scale": "MAJ",
            "chord_base": "MAJ", "chord_exts": set(), "octave": 4,
            "oct_offset": 0, "lead_voice_pos": 0, "inversion": 0,
            "voicing": "CLOSE", "spread": 0, "hold": False,
            "key_lock": False, "mirror_input_ch": False, "root_mode": False,
            "arp_passthrough": False, "live_notes": set(),
            "trigger_note": None, "func_transpose": 0, "func_held": False,
            "vl_strength": 0.0, "vl_enabled": False, "vel_override": 0,
            "quality_mode": "DIAT", "base_override": None,
            "scale_mode": "off", "scale_snap": False,
        }

    # -- things the handler measures, not just pokes -----------------------
    def get_intervals(self, base=None, exts=None, **k):
        return [0, 4, 7]

    def build_chord_notes(self, *a, **k):
        return [60, 64, 67]

    def get_voice_positions(self, notes):
        return [0, 1, 2]

    def chord_root_now(self):
        return self.state["chord_root"]

    def sampler_bpm(self):
        return 120.0

    def mirror_active(self):
        return False

    # -- skipback ring buttons (FX1-3, 2026-09-23): len()/compare on these --
    rings = [{"id": 0, "name": "MAIN"}, {"id": 1, "name": "T1"}]
    deck_src = {"A": 0, "B": 1}

    def skipback_rings(self):
        return list(self.rings)

    def sampler_deck_source(self, deck):
        return self.deck_src.get(deck, 0)

    def skipback_save(self, track_id, seconds=60, done=None):
        self.calls.append(("skipback_save", (track_id, seconds)))
        if done:
            done(True, "fake.wav")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _stub(*a, **k):
            self.calls.append((name, a))
            return None
        return _stub


# ---------------------------------------------------------------------------
# Headless handler
# ---------------------------------------------------------------------------

class HeadlessMixtrack(MT.MixtrackHandler):
    """MixtrackHandler with every hardware/thread side effect removed.
    See the SAFETY block at the top of this file before changing any of it."""

    def _open_output(self, prefix):        # SAFETY 1 -- never touch real MIDI
        self._out = None

    def wake(self):                        # SAFETY 2 -- no LED spray/sleep
        pass

    def _fx_led_loop(self):                # SAFETY 3 -- poller exits at once
        return

    def _led_tick_loop(self):              # SAFETY 3b -- metronome/arm poller too
        return

    def _flash_pad(self, note, deck, ms=180):
        # Deterministic: the real one spawns a Timer thread per press and the
        # sweeps press thousands of pads.
        self.flashes.append((deck, note))


def build_mt(mode_a="sampler", mode_b="sampler"):
    """Construct a HeadlessMixtrack with both decks in the given modes.

    Returns (handler, eng).  Recording lists live on the handler:
      .fired    -- (deck, kind, action_name) every action that actually ran
      .flashes  -- (deck, note) every _flash_pad
      .errors   -- the handler's own swallowed '! Mixtrack action ... failed'
    """
    eng = FakeEng()
    mt = HeadlessMixtrack.__new__(HeadlessMixtrack)
    mt.flashes = []
    mt.fired   = []
    mt.errors  = []
    MT.MixtrackHandler.__init__(mt, eng)
    _instrument(mt)
    return mt, eng


def _instrument(mt):
    """Wrap every _act_*/_cc_* so we can see WHICH action actually ran for a
    given message.  This is what makes the reachability net possible: the
    config says what SHOULD fire; this records what DID."""
    for name in dir(type(mt)):
        if not (name.startswith("_act_") or name.startswith("_cc_")):
            continue
        fn = getattr(mt, name)
        if not callable(fn):
            continue

        def make(nm, f):
            def wrapper(msg, deck, *a, **k):
                kind = "btn" if nm.startswith("_act_") else "cc"
                mt.fired.append((deck, kind, nm[5:] if kind == "btn"
                                 else nm[4:]))
                return f(msg, deck, *a, **k)
            return wrapper
        setattr(mt, name, make(name, fn))


@contextlib.contextmanager
def capture_errors(mt):
    """Collect the handler's own swallowed-exception prints.

    _dispatch_button/_dispatch_cc catch EVERY exception an action raises and
    print one line.  On the live rig that line scrolls past in the journal and
    the control silently does nothing; here it must fail the test.
    """
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
    for line in buf.getvalue().splitlines():
        if "failed:" in line or "unknown" in line.lower():
            mt.errors.append(line.strip())


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def note_on(ch1, note, vel=127):
    return mido.Message("note_on", channel=ch1 - 1, note=note, velocity=vel)


def note_off(ch1, note):
    return mido.Message("note_on", channel=ch1 - 1, note=note, velocity=0)


def cc(ch1, control, value):
    return mido.Message("control_change", channel=ch1 - 1,
                        control=control, value=value)


DECKS = (MT.CH_DECK1, MT.CH_DECK2)
ALL_NOTES = sorted(set(MT._NOTE_BY_NAME.values()))
ALL_CCS   = sorted(set(MT._CC_BY_NAME.values()))
# Chord mode was removed 2026-08-01: both decks are sampler decks and
# there is no mode dimension left.  Kept as a tuple so the sweeps read the
# same and re-adding a mode later is a one-line change.
MODES     = ("sampler",)


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

RESULTS = []

# Self-check total.  main() collects tests by sorted(globals()), so a test
# renamed out of the `test_` prefix -- or lost to an edit -- silently vanishes
# and the suite still prints `0 failed` at a LOWER total (the session-9/10
# "a suite with no expected total shrinks silently" trap).  Keep this in step
# with the checks below AND the test-orc SKILL.md table; a mismatch fails the
# run deliberately.
EXPECTED = 62


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail))
    return bool(ok)


def _maps_for(mt, deck, mode="sampler"):
    """(buttons, buttons_shift, ccs, ccs_shift) for a deck.

    There are no SHIFT maps any more -- see test_no_shift_layer_exists -- so
    the shift slots are always None.  The shape is kept so the reachability
    sweep still expresses "which layer should this control resolve through".
    """
    btns, ccs = mt._maps[deck]
    return btns, None, ccs, None


# ---------------------------------------------------------------------------
# 1. STATIC -- every action name in every map resolves to a real method
# ---------------------------------------------------------------------------

def test_dispatch_names():
    """A config map naming a method that does not exist is a DEAD CONTROL:
    _dispatch_button/_dispatch_cc do `getattr(..., None)` and fall through
    to a DEBUG-only print, so on the live rig the button just does nothing."""
    mt, _ = build_mt()
    missing = []
    button_maps = [
        ("MASTER_BUTTONS", mt._master_buttons),
        ("DECK1_SAMPLER_BUTTONS", mt._d1s_buttons),
        ("DECK2_SAMPLER_BUTTONS", mt._d2s_buttons),
    ]
    cc_maps = [
        ("MASTER_CCS", mt._master_ccs),
        ("DECK1_SAMPLER_CCS", mt._d1s_ccs),
        ("DECK2_SAMPLER_CCS", mt._d2s_ccs),
    ]
    for label, m in button_maps:
        for num, action in m.items():
            if not hasattr(mt, "_act_" + action[0]):
                missing.append(f"{label}[{num}] -> _act_{action[0]}")
    for label, m in cc_maps:
        for num, action in m.items():
            if not hasattr(mt, "_cc_" + action[0]):
                missing.append(f"{label}[{num}] -> _cc_{action[0]}")
    check(not missing, "every mapped action resolves to a method",
          "; ".join(missing))


def test_documented_actions_exist():
    """Every action name in orc1_config.py's AVAILABLE ACTIONS block must
    resolve to a real handler.

    That block is the reference someone consults before editing a MIXTRACK_*
    map.  After chord mode was deleted it still advertised ~25 removed actions,
    and since cfg.DEBUG is False by default, wiring one in produces a control
    that does nothing with no error printed anywhere -- a documentation bug
    that manufactures the project's signature silent-dead-control failure.
    Enforcing the doc is cheaper than remembering to update it.
    """
    import re
    src = open(cfg.__file__, encoding="utf-8").read()
    start = src.find("# AVAILABLE BUTTON ACTIONS")
    end = src.find("DECK MAPS", start)
    if start < 0 or end < 0:
        check(False, "AVAILABLE ACTIONS block is findable",
              "header moved or was deleted -- this guard is now blind")
        return
    block = src[start:end]
    mt, _ = build_mt()
    documented = set(re.findall(r'#\s+\("([a-z_0-9]+)"', block))
    missing = sorted(n for n in documented
                     if not hasattr(mt, "_act_" + n)
                     and not hasattr(mt, "_cc_" + n))
    check(not missing and len(documented) > 5,
          f"all {len(documented)} documented actions exist",
          "no handler for: " + ", ".join(missing) if missing
          else f"only found {len(documented)} names -- regex may have broken")


def test_no_write_only_state():
    """No instance attribute may be WRITTEN but never READ.

    When chord mode was deleted, `_jog_touched`, `_pitch_hi` and `_xf` were
    left behind still being assigned on every touch / at construction, with
    every reader gone.  That is worse than plainly dead code: it looks
    maintained, so the next person reads it as live state and acts on it.  It
    is also exactly the shape of the deck-2 SHIFT maps, which were knowingly
    left as an "inert" layer in July and went on to disable a whole deck.

    Read-only-elsewhere attributes are fine; this only fires on names that
    NOTHING in the module ever loads.  If an attribute is genuinely read from
    another module, add it to EXTERNALLY_READ with the reader named.
    """
    import ast as _ast
    src = open(MT.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    stored, loaded = {}, set()
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Attribute)
                and isinstance(node.value, _ast.Name)
                and node.value.id == "self"):
            if isinstance(node.ctx, _ast.Store):
                stored.setdefault(node.attr, node.lineno)
            else:
                loaded.add(node.attr)
        # getattr(self, "name", default) is a read too, and an attribute-only
        # walk cannot see it -- missing this makes the check cry wolf, and a
        # check that cries wolf is one people switch off.
        if (isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], _ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], _ast.Constant)
                and isinstance(node.args[1].value, str)):
            loaded.add(node.args[1].value)
    # Attributes other modules legitimately reach into.
    EXTERNALLY_READ = {
        "_DECK_LETTER",   # orc1.py hotplug/status paths
    }
    dead = sorted((n, ln) for n, ln in stored.items()
                  if n not in loaded and n not in EXTERNALLY_READ)
    check(not dead, "no write-only instance state",
          " | ".join(f"self.{n} written at line {ln}, never read"
                     for n, ln in dead))


def test_unresolved_action_is_loud():
    """A control mapped to a non-existent action must SAY SO, whatever DEBUG is.

    Every action is reached by getattr() on a string from orc1_config.py, so a
    typo or a renamed method gives a dead control with no exception.  The only
    report used to sit behind `if self.cfg.DEBUG`, which is False on the live
    rig -- so the pad just did nothing, with nothing to grep for.  The Fire was
    fixed for exactly this on 2026-07-29; the Mixtrack was not, until a review
    caught it.  Must also DEDUPE: CC dispatch fires per encoder click.
    """
    import io as _io
    import contextlib as _c
    bad = []
    for kind, prefix, send in (
            ("button", "_act_",
             lambda m: m.handle_msg(note_on(MT.CH_DECK1, MT.NOTE_PLAY))),
            ("cc", "_cc_",
             lambda m: m.handle_msg(cc(MT.CH_DECK1, MT.CC_JOG, 3)))):
        MT._UNRESOLVED_SEEN.clear()
        mt, _ = build_mt()
        if prefix == "_act_":
            mt._d1s_buttons = dict(mt._d1s_buttons)
            mt._d1s_buttons[MT.NOTE_PLAY] = ("bogus_xyz",)
        else:
            mt._d1s_ccs = dict(mt._d1s_ccs)
            mt._d1s_ccs[MT.CC_JOG] = ("bogus_xyz",)
        mt._maps[MT.CH_DECK1] = (mt._d1s_buttons, mt._d1s_ccs)
        buf = _io.StringIO()
        with _c.redirect_stdout(buf):
            send(mt)
            send(mt)                       # twice: must print only once
        lines = [l for l in buf.getvalue().splitlines() if "bogus_xyz" in l]
        if len(lines) != 1:
            bad.append(f"{kind}: printed {len(lines)} line(s), want exactly 1 "
                       f"(dedupe) -- {lines}")
        elif "does nothing" not in lines[0]:
            bad.append(f"{kind}: unhelpful message {lines[0]!r}")
    MT._UNRESOLVED_SEEN.clear()
    check(not bad, "an unresolvable action is reported loudly, once",
          " | ".join(bad))


def test_no_dead_handlers():
    """_act_/_cc_ methods that no map ever names.  Not always a bug (some are
    deliberately spare) but each one is either dead code or a control someone
    forgot to wire -- worth seeing."""
    mt, _ = build_mt()
    # Track button and CC namespaces SEPARATELY.  Flattening both to bare
    # names lets a mapped _act_x mask an unmapped _cc_x of the same name --
    # which is exactly what happened: `consume` is mapped as a BUTTON action
    # (PAD_MODE), so `_cc_consume` was silently reported as wired.
    named_btn = set()
    for m in (mt._master_buttons, mt._d1s_buttons, mt._d2s_buttons):
        named_btn |= {a[0] for a in m.values()}
    named_cc = set()
    for m in (mt._master_ccs, mt._d1s_ccs, mt._d2s_ccs):
        named_cc |= {a[0] for a in m.values()}
    orphans = sorted(
        ["_act_" + n[5:] for n in dir(type(mt))
         if n.startswith("_act_") and n[5:] not in named_btn] +
        ["_cc_" + n[4:] for n in dir(type(mt))
         if n.startswith("_cc_") and n[4:] not in named_cc])
    check(True, f"unmapped handlers: {len(orphans)}", ", ".join(orphans))


# ---------------------------------------------------------------------------
# 2. REACHABILITY -- can each map entry actually FIRE in its own mode?
# ---------------------------------------------------------------------------

def test_reachability():
    """THE net.  For every (deck, mode, shift) layer, feed each mapped
    note/CC in and record which action actually ran.  A map entry whose
    action never fires is a control that is silently shadowed by another
    layer -- the bug class that keeps producing 'this button does nothing'.
    """
    unreachable = []
    for mode in MODES:
        for deck in DECKS:
            for shift in (False, True):
                mt, _ = build_mt(mode_a=mode, mode_b=mode)
                btns, btns_shift, ccs, ccs_shift = _maps_for(mt, deck, mode)
                # what the handler will consult for a NON-modifier note
                want_b = btns_shift if (shift and btns_shift) else btns
                want_c = ccs_shift if (shift and ccs_shift) else ccs
                for num, action in sorted(want_b.items()):
                    if num in MT.MixtrackHandler._MODIFIER_NOTES:
                        continue
                    # Re-pin the layer before EVERY message: some mapped
                    # actions (PAD_MODE -> toggle_deck_mode) legitimately
                    # change the mode, which would otherwise silently move
                    # the rest of this sweep onto the wrong map.
                    mt._shift[deck] = shift
                    mt.fired.clear()
                    with capture_errors(mt):
                        mt.handle_msg(note_on(deck, num))
                        mt.handle_msg(note_off(deck, num))
                    got = {f[2] for f in mt.fired}
                    if action[0] not in got:
                        unreachable.append(
                            f"{mode}/deck{deck-1}/shift={shift} note {num} "
                            f"maps '{action[0]}' but fired {got or '{}'}")
                for num, action in sorted(want_c.items()):
                    mt._shift[deck] = shift
                    mt.fired.clear()
                    with capture_errors(mt):
                        mt.handle_msg(cc(deck, num, 100))
                    got = {f[2] for f in mt.fired}
                    if action[0] not in got:
                        unreachable.append(
                            f"{mode}/deck{deck-1}/shift={shift} cc {num} "
                            f"maps '{action[0]}' but fired {got or '{}'}")
    check(not unreachable, "every mapped control is reachable in its layer",
          " | ".join(unreachable))


def test_shift_does_not_disable_sampler_controls():
    """In SAMPLER mode, holding SHIFT must not silently kill a sampler
    control.

    The deck-2 SHIFT maps were emptied to an all-`consume` "inert layer" when
    drum edit was removed (2026-07-21), but `_handle_deck` makes a non-empty
    SHIFT map EXCLUSIVE -- so every entry in it shadows the sampler map
    underneath.  Deck 1 has no SHIFT maps and is unaffected, so the two decks
    behave differently under SHIFT: the handler's own advertised SHIFT
    modifiers (x4 coarse scrub in _cc_sample_scrub, x5 browse in
    _cc_sample_browse) are dead on deck B, and so is SHIFT+touchstrip.
    """
    shadowed = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, _ = build_mt()
        btns, btns_shift, ccs, ccs_shift = _maps_for(mt, deck, "sampler")
        for num, action in sorted(btns.items()):
            if num in MT.MixtrackHandler._MODIFIER_NOTES:
                continue
            if action[0] == "consume":
                continue                       # already deliberately inert
            mt._shift[deck] = True
            mt.fired.clear()
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, num))
                mt.handle_msg(note_off(deck, num))
            got = {f[2] for f in mt.fired}
            if action[0] not in got:
                shadowed.append(f"deck{letter} note {num} "
                                f"'{action[0]}' -> {got or '{}'}")
        for num, action in sorted(ccs.items()):
            if action[0] == "consume":
                continue
            mt._shift[deck] = True
            mt.fired.clear()
            with capture_errors(mt):
                mt.handle_msg(cc(deck, num, 100))
            got = {f[2] for f in mt.fired}
            if action[0] not in got:
                shadowed.append(f"deck{letter} cc {num} "
                                f"'{action[0]}' -> {got or '{}'}")
    check(not shadowed,
          "SHIFT does not disable sampler controls on either deck",
          " | ".join(shadowed))


def test_sampler_ccs_reachable_under_shift():
    """Focused regression for the shadowing class above, aimed at the CCs the
    hardware ONLY emits while SHIFT is held (STRIP_SHIFT).  If the deck's
    SHIFT map claims that CC, the sampler map's entry can never run."""
    problems = []
    for deck in DECKS:
        mt, _ = build_mt()
        _, _, s_ccs, s_ccs_shift = _maps_for(mt, deck, "sampler")
        if MT.CC_STRIP_SHIFT not in s_ccs:
            continue
        want = s_ccs[MT.CC_STRIP_SHIFT][0]
        mt._shift[deck] = True            # hardware only sends it with SHIFT
        mt.fired.clear()
        with capture_errors(mt):
            mt.handle_msg(cc(deck, MT.CC_STRIP_SHIFT, 64))
        got = {f[2] for f in mt.fired}
        if want not in got:
            problems.append(f"deck{deck-1}: SHIFT+strip maps '{want}' "
                            f"but fired {got or '{}'}")
    check(not problems, "SHIFT+touchstrip reaches its sampler action",
          " | ".join(problems))


# ---------------------------------------------------------------------------
# 3. SWEEPS -- nothing raises, nothing is swallowed
# ---------------------------------------------------------------------------

def test_button_sweep():
    """Every button on every channel, both deck modes, both shift states,
    press and release.  Asserts no action raises (the dispatch swallows it)."""
    total = 0
    errs = []
    for mode in MODES:
        for shift in (False, True):
            mt, _ = build_mt(mode_a=mode, mode_b=mode)
            with capture_errors(mt):
                for deck in DECKS:
                    mt._shift[deck] = shift
                    for n in ALL_NOTES:
                        mt.handle_msg(note_on(deck, n))
                        mt.handle_msg(note_off(deck, n))
                        total += 2
                for n in ALL_NOTES:                    # master section
                    mt.handle_msg(note_on(MT.CH_MASTER, n))
                    mt.handle_msg(note_off(MT.CH_MASTER, n))
                    total += 2
            errs += mt.errors
    check(not errs, f"button sweep clean ({total} events)",
          " | ".join(sorted(set(errs))))


def test_cc_sweep():
    """Every CC on every channel across its full range, both modes, both
    shift states.  Boundary values included -- 0/64/127 are where the
    relative decoders and the crossfader zone logic change behaviour."""
    total = 0
    errs = []
    values = (0, 1, 5, 63, 64, 65, 100, 126, 127)
    for mode in MODES:
        for shift in (False, True):
            mt, _ = build_mt(mode_a=mode, mode_b=mode)
            with capture_errors(mt):
                for deck in DECKS:
                    mt._shift[deck] = shift
                    for c in ALL_CCS:
                        for v in values:
                            mt.handle_msg(cc(deck, c, v))
                            total += 1
                for c in ALL_CCS:
                    for v in values:
                        mt.handle_msg(cc(MT.CH_MASTER, c, v))
                        total += 1
            errs += mt.errors
    check(not errs, f"cc sweep clean ({total} events)",
          " | ".join(sorted(set(errs))))


def test_no_shift_layer_exists():
    """SHIFT must be a MODIFIER, never a map layer.

    _handle_deck treats a non-empty SHIFT map as EXCLUSIVE, so any SHIFT map
    silently shadows the deck's real controls underneath it -- that is exactly
    how deck B lost all 36 of its sampler controls between 2026-07-21 and
    2026-08-01.  If someone reintroduces one, this fails and points them at
    _handle_deck before it reaches hardware.
    """
    mt, _ = build_mt()
    bad = []
    for deck in DECKS:
        entry = mt._maps[deck]
        if len(entry) != 2:
            bad.append(f"deck{deck - 1}: _maps entry has {len(entry)} slots, "
                       f"expected exactly (buttons, ccs)")
    for attr in ("_d2_buttons_shift", "_d2_ccs_shift"):
        if getattr(mt, attr, None):
            bad.append(f"{attr} is populated")
    check(not bad, "no SHIFT map layer exists", " | ".join(bad))


def test_chord_config_maps_are_gone():
    """The six chord map dicts must not come back in orc1_config.py.  An
    orphaned map outliving its feature is how the deck-2 SHIFT layer survived
    the drum-edit removal and went on to disable a whole deck."""
    dead = [n for n in ("MIXTRACK_DECK1_BUTTONS", "MIXTRACK_DECK1_CCS",
                        "MIXTRACK_DECK2_BUTTONS", "MIXTRACK_DECK2_CCS",
                        "MIXTRACK_DECK2_BUTTONS_SHIFT",
                        "MIXTRACK_DECK2_CCS_SHIFT")
            if getattr(cfg, n, None)]
    check(not dead, "chord config maps are gone", ", ".join(dead))


# ---------------------------------------------------------------------------
# 4. BEHAVIOURAL regressions
# ---------------------------------------------------------------------------

def test_shift_release_not_eaten():
    """SHIFT and JOG_TOUCH must always resolve through the BASE map, or a
    SHIFT release lands in the SHIFT map (which has no SHIFT entry), is
    dropped, and the deck is stuck in SHIFT forever."""
    bad = []
    for mode in MODES:
        for deck in DECKS:
            mt, _ = build_mt(mode_a=mode, mode_b=mode)
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, MT.NOTE_SHIFT))
                held = mt._shift[deck]
                mt.handle_msg(note_off(deck, MT.NOTE_SHIFT))
                released = mt._shift[deck]
            if not held or released:
                bad.append(f"{mode}/deck{deck-1}: held={held} "
                           f"stuck={released}")
    check(not bad, "SHIFT press/release tracks correctly", " | ".join(bad))


def test_jog_relative_decode():
    """The Mixtrack emits small two's-complement deltas.  Reading value 1 as
    -63 turns a light jog touch into a max-speed runaway (the 2026-07-07
    bug).  Assert the local decoder, and that a jog CC reaches the sampler
    scrub facade with the right SIGN."""
    mt, eng = build_mt()
    vals = {0: 0, 1: 1, 5: 5, 63: 63, 64: 0, 65: -63, 127: -1}
    bad = [f"{k}->{mt._rel(k)} (want {v})"
           for k, v in vals.items() if mt._rel(k) != v]
    check(not bad, "relative CC decode is two's-complement", " | ".join(bad))

    eng.calls.clear()
    with capture_errors(mt):
        mt.handle_msg(cc(MT.CH_DECK1, MT.CC_JOG, 1))     # gentle forward
        mt.handle_msg(cc(MT.CH_DECK1, MT.CC_JOG, 127))   # gentle backward
    scrubs = [c for c in eng.calls if c[0] == "sampler_scrub"]
    ok = (len(scrubs) == 2 and scrubs[0][1][1] > 0 and scrubs[1][1][1] < 0)
    check(ok, "jog forward/backward reach sampler_scrub with correct sign",
          str(scrubs))


def test_jog_touch_engages_scratch():
    """Touching the platter must engage scratch in sampler mode -- and must
    NOT in chord mode (where the platter is the lead-voice encoder)."""
    bad = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, eng = build_mt()
        eng.calls.clear()
        with capture_errors(mt):
            mt.handle_msg(note_on(deck, MT.NOTE_JOG_TOUCH))
            mt.handle_msg(note_off(deck, MT.NOTE_JOG_TOUCH))
        got = [c for c in eng.calls if c[0] == "sampler_scratch"]
        if len(got) != 2 or got[0][1] != (letter, True) \
                or got[1][1] != (letter, False):
            bad.append(f"deck{letter}: {got}")
    check(not bad, "platter touch/release engages+releases scratch on both "
          "decks", " | ".join(bad))


def test_top_row_all_banks_grab():
    """The hardware PAD MODE button cycles the top row through three note
    banks and we cannot read which is active, so all three must grab.  If one
    bank is unmapped the pads 'work only sometimes'."""
    bad = []
    banks = {
        "loop":    (MT.NOTE_LOOP_IN, MT.NOTE_LOOP_OUT,
                    MT.NOTE_LOOP_ON, MT.NOTE_LOOP_HALVE),
        "sampler": (MT.NOTE_SAMPLER1, MT.NOTE_SAMPLER2,
                    MT.NOTE_SAMPLER3, MT.NOTE_SAMPLER4),
        "pad":     (MT.NOTE_PAD1, MT.NOTE_PAD2,
                    MT.NOTE_PAD3, MT.NOTE_PAD4),
    }
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        for bank, notes in banks.items():
            for want_bars, n in zip((1.0, 2.0, 4.0, 8.0), notes):
                mt, eng = build_mt()
                eng.calls.clear()
                with capture_errors(mt):
                    mt.handle_msg(note_on(deck, n))
                grabs = [c for c in eng.calls
                         if c[0] == "sampler_capture_bars"]
                if not grabs or grabs[0][1] != (letter, want_bars):
                    bad.append(f"deck{letter}/{bank} note {n}: {grabs}")
    check(not bad, "all 3 pad-mode banks grab the right bar count",
          " | ".join(bad))


def test_transport_buttons():
    """PLAY = loop toggle, CUE = hard stop, in sampler mode on both decks."""
    bad = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, eng = build_mt()
        eng.calls.clear()
        with capture_errors(mt):
            mt.handle_msg(note_on(deck, MT.NOTE_PLAY))
            mt.handle_msg(note_on(deck, MT.NOTE_CUE))
        names = [c[0] for c in eng.calls]
        if "sampler_loop_toggle" not in names:
            bad.append(f"deck{letter}: PLAY -> {names}")
        if "sampler_stop" not in names:
            bad.append(f"deck{letter}: CUE -> {names}")
    check(not bad, "PLAY toggles loop, CUE stops", " | ".join(bad))


def test_hotcues():
    """Bottom row = settable hotcues 1-4. A BARE press fires sampler_cue with
    the deck letter + 1-based index (the synth deck decides set-vs-jump)."""
    bad = []
    notes = (MT.NOTE_HOTCUE1, MT.NOTE_HOTCUE2,
             MT.NOTE_HOTCUE3, MT.NOTE_HOTCUE4)
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        for want, n in zip((1, 2, 3, 4), notes):
            mt, eng = build_mt()
            eng.calls.clear()
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, n))
            got = [c for c in eng.calls if c[0] == "sampler_cue"]
            if not got or got[0][1] != (letter, want):
                bad.append(f"deck{letter} hotcue{want}: {got}")
    check(not bad, "hotcues 1-4 fire sampler_cue", " | ".join(bad))


def test_hotcue_shift_clears():
    """SHIFT+hotcue must CLEAR the cue (sampler_cue_clear), NOT set/jump it.
    SHIFT is read as a per-action modifier inside _act_sample_cue -- there is
    no SHIFT map layer (that layer is what silently killed deck B once)."""
    bad = []
    notes = (MT.NOTE_HOTCUE1, MT.NOTE_HOTCUE2,
             MT.NOTE_HOTCUE3, MT.NOTE_HOTCUE4)
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        for want, n in zip((1, 2, 3, 4), notes):
            mt, eng = build_mt()
            eng.calls.clear()
            mt._shift[deck] = True
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, n))
            clears = [c for c in eng.calls if c[0] == "sampler_cue_clear"]
            sets   = [c for c in eng.calls if c[0] == "sampler_cue"]
            if not clears or clears[0][1] != (letter, want) or sets:
                bad.append(f"deck{letter} SHIFT+hotcue{want}: "
                           f"clear={clears} cue={sets}")
    check(not bad, "SHIFT+hotcue clears the cue and does not set/jump it",
          " | ".join(bad))


def test_hotcue_led_mirror_tracks_set_and_clear():
    """The LED mirror (_cue_set) is what colours the pads. A bare press arms
    the bit (the pad is now set, whether it was just set or jumped to); a
    SHIFT press clears it."""
    bad = []
    notes = (MT.NOTE_HOTCUE1, MT.NOTE_HOTCUE2,
             MT.NOTE_HOTCUE3, MT.NOTE_HOTCUE4)
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, _ = build_mt()
        for i, n in enumerate(notes):
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, n))          # bare -> armed
            if not mt._cue_set[deck][i]:
                bad.append(f"deck{letter} cue{i+1} not armed after set")
            mt._shift[deck] = True
            with capture_errors(mt):
                mt.handle_msg(note_on(deck, n))          # SHIFT -> cleared
            mt._shift[deck] = False
            if mt._cue_set[deck][i]:
                bad.append(f"deck{letter} cue{i+1} still armed after clear")
    check(not bad, "the LED mirror arms on set and clears on SHIFT+press",
          " | ".join(bad))


def test_hotcue_mirror_reset_by_grab_and_load():
    """A fresh grab / file load wipes the deck's cues on the synth side, so the
    LED mirror must reset too -- else dark/armed pads would lie about the new
    audio."""
    bad = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        # (a) top-row grab
        mt, _ = build_mt()
        mt._cue_set[deck] = [True, True, True, True]
        with capture_errors(mt):
            mt.handle_msg(note_on(deck, MT.NOTE_LOOP_ON))    # a grab pad
        if any(mt._cue_set[deck]):
            bad.append(f"deck{letter}: grab did not reset the mirror "
                       f"-> {mt._cue_set[deck]}")
        # (b) file load onto the deck (WHEEL button)
        mt, _ = build_mt()
        mt._cue_set[deck] = [True, True, True, True]
        with capture_errors(mt):
            mt.handle_msg(note_on(deck, MT.NOTE_WHEEL_MODE))
        if any(mt._cue_set[deck]):
            bad.append(f"deck{letter}: load did not reset the mirror "
                       f"-> {mt._cue_set[deck]}")
    check(not bad, "a grab or load resets the deck's hotcue LED mirror",
          " | ".join(bad))


def test_hotcue_leds_reflect_the_mirror():
    """Painting must light ARMED pads yellow and turn EMPTY pads OFF -- the
    whole point of settable cues is that you can SEE which are set. Capture the
    real note-ons _paint_hotcue_leds emits."""
    class FakeOut:
        def __init__(self):
            self.sent = []
        def send(self, m):
            self.sent.append((m.channel, m.note, m.velocity))

    bad = []
    notes = (MT.NOTE_HOTCUE1, MT.NOTE_HOTCUE2,
             MT.NOTE_HOTCUE3, MT.NOTE_HOTCUE4)
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, _ = build_mt()
        mt._out = FakeOut()
        mt._cue_set[deck] = [True, False, True, False]
        mt._paint_hotcue_leds(deck)
        by_note = {n: v for (ch, n, v) in mt._out.sent if ch == deck - 1}
        want = {notes[0]: MT.LED_YELLOW, notes[1]: 0,
                notes[2]: MT.LED_YELLOW, notes[3]: 0}
        for n, v in want.items():
            if by_note.get(n) != v:
                bad.append(f"deck{letter} note {n}: got {by_note.get(n)} "
                           f"want {v}")
    check(not bad, "armed hotcue pads light yellow, empty pads go dark",
          " | ".join(bad))


def test_pitch_bend_momentary():
    """PITCH +/- is a MOMENTARY beatmatch nudge: it must send on both press
    AND release, or the deck stays bent forever."""
    bad = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, eng = build_mt()
        eng.calls.clear()
        with capture_errors(mt):
            mt.handle_msg(note_on(deck, MT.NOTE_PITCH_UP))
            mt.handle_msg(note_off(deck, MT.NOTE_PITCH_UP))
        got = [c for c in eng.calls if c[0] == "sampler_pitch_bend"]
        if len(got) != 2 or got[0][1][2] is not True \
                or got[1][1][2] is not False:
            bad.append(f"deck{letter}: {got}")
    check(not bad, "pitch bend engages on press, releases on release",
          " | ".join(bad))


def test_crossfader_smart_routing():
    """The master crossfader blends the SAMPLER decks whenever either deck is
    in sampler mode, and only morphs chords when BOTH are chord."""
    bad = []
    for v in (0, 64, 127):
        mt, eng = build_mt()
        eng.calls.clear()
        with capture_errors(mt):
            mt.handle_msg(cc(MT.CH_MASTER, MT.CC_CROSSFADER, v))
        got = [c for c in eng.calls if c[0] == "sampler_crossfade"]
        if not got or got[0][1] != (v,):
            bad.append(f"xf {v}: {got}")
    check(not bad, "crossfader blends deck A against deck B", " | ".join(bad))


def test_fx_buttons_are_ring_buttons():
    """FX1-3 are per-deck skipback RING buttons (2026-09-23; the FX rack was
    parked). Driven through the REAL dispatch from a MIDI note."""
    # (1) FXn -> this deck grabs from the n-th ARMED ring; letters never crossed
    bad = []
    for deck in DECKS:
        L = "A" if deck == MT.CH_DECK1 else "B"
        for n, note in ((1, MT.NOTE_FX1), (2, MT.NOTE_FX2)):
            mt, eng = build_mt()
            eng.calls.clear()
            with capture_errors(mt):
                mt._shift[deck] = False
                mt.handle_msg(note_on(deck, note))
            got = [c for c in eng.calls if c[0] == "sampler_set_source"]
            want = (L, eng.rings[n - 1]["id"])
            if got != [("sampler_set_source", want)] or mt.errors:
                bad.append(f"deck{L} FX{n}: {got} {mt.errors}")
    check(not bad, "FX1/FX2 point THIS deck's grab at the 1st/2nd armed ring",
          " | ".join(bad))

    # (2) FXn = the n-th ARMED ring in id order, not ring id n
    mt, eng = build_mt()
    eng.rings = [{"id": 0, "name": "MAIN"}, {"id": 3, "name": "VOX"}]
    eng.calls.clear()
    with capture_errors(mt):
        mt.handle_msg(note_on(MT.CH_DECK1, MT.NOTE_FX2))
    got = [c for c in eng.calls if c[0] == "sampler_set_source"]
    check(got == [("sampler_set_source", ("A", 3))],
          "FX2 = the 2nd ARMED ring (id 3 here), not ring id 2", str(got))

    # (3) a button with no ring behind it does nothing
    mt, eng = build_mt()                               # only 2 rings armed
    eng.calls.clear()
    with capture_errors(mt):
        mt.handle_msg(note_on(MT.CH_DECK2, MT.NOTE_FX3))
    hits = [c for c in eng.calls if c[0] in ("sampler_set_source", "skipback_save")]
    check(not hits and not mt.errors, "FX3 with no 3rd ring is a clean no-op",
          f"{hits} {mt.errors}")

    # (4) SHIFT+FXn saves that ring to a file (configured length), no repoint
    mt, eng = build_mt()
    eng.calls.clear()
    with capture_errors(mt):
        mt._shift[MT.CH_DECK1] = True
        mt.handle_msg(note_on(MT.CH_DECK1, MT.NOTE_FX2))
    saves = [c for c in eng.calls if c[0] == "skipback_save"]
    srcs = [c for c in eng.calls if c[0] == "sampler_set_source"]
    want = ("skipback_save", (1, int(getattr(cfg, "MIXTRACK_RING_SAVE_SECS", 60))))
    check(saves == [want] and not srcs and not mt._ring_saving,
          "SHIFT+FX2 saves ring t1 to ~/recordings (and does not repoint)",
          f"saves={saves} srcs={srcs} still_saving={mt._ring_saving}")

    # (5) LEDs: lit = the deck's CURRENT ring (read from the live source)
    mt, eng = build_mt()
    mt._out = object()
    sent = {}
    mt._led = lambda note, deck, on=True, vel=127: sent.__setitem__((deck, note), bool(on))
    eng.deck_src = {"A": 1, "B": 0}
    mt._fx_led_state.clear()
    mt._render_ring_leds()
    A, B = MT.CH_DECK1, MT.CH_DECK2
    want = {(A, MT.NOTE_FX1): False, (A, MT.NOTE_FX2): True, (A, MT.NOTE_FX3): False,
            (B, MT.NOTE_FX1): True, (B, MT.NOTE_FX2): False, (B, MT.NOTE_FX3): False}
    check(sent == want, "ring LEDs light each deck's current ring (FX3 dark)",
          f"got {sent}")


def test_master_load_buttons():
    """Master LOAD1/LOAD2 grab a loop into deck A/B."""
    mt, eng = build_mt()
    eng.calls.clear()
    with capture_errors(mt):
        mt.handle_msg(note_on(MT.CH_MASTER, MT.NOTE_LOAD1))
        mt.handle_msg(note_on(MT.CH_MASTER, MT.NOTE_LOAD2))
    got = [c for c in eng.calls if c[0] == "sampler_capture_bars"]
    decks = [g[1][0] for g in got]
    check(decks == ["A", "B"], "master LOAD1/2 grab into deck A/B", str(got))


def test_mode_switch_silences_deck():
    """Switching a deck's mode must stop its sample gate, or a looping deck
    keeps sounding with no control surface attached to it."""
    mt, _ = build_mt()
    leftovers = [n for n in ("set_deck_mode", "_act_toggle_deck_mode",
                             "_on_crossfader", "set_deck_b_root",
                             "xf_in_deck_b_zone", "_retrigger_if_held",
                             "deck_b", "_deck_mode")
                 if hasattr(mt, n)]
    check(not leftovers, "chord-mode machinery is fully removed",
          ", ".join(leftovers))


def test_control_arguments_are_correct():
    """Reachability proves DISPATCH, not BEHAVIOUR.

    _instrument records a fired action before the handler body runs, so a
    control counts as "reachable" the instant the right method name is looked
    up -- regardless of what it then passes on.  Most of the Mixtrack's
    DSP-affecting surface (EQ bands, filter, speed, gain, browse, seek,
    reverse, export, load, tap tempo, master gain, FX param) had NO assertion
    on the facade call at all, so e.g. _cc_sample_eq swapping "hi" and "low",
    or reverse addressing the wrong deck, would have shipped green.
    """
    bad = []

    def drive(deck, msg, want_name, want_args=None, rack=False):
        mt, eng = build_mt()
        eng.fx.rack.calls.clear()
        eng.calls.clear()
        with capture_errors(mt):
            mt._shift[deck] = False
            mt.handle_msg(msg)
        got = eng.fx.rack.calls if rack else eng.calls
        hit = [c for c in got if c[0] == want_name]
        if not hit:
            bad.append(f"{want_name}: never called (got {got})")
        elif want_args is not None and hit[0][1] != want_args:
            bad.append(f"{want_name}: args {hit[0][1]} != {want_args}")

    for deck in DECKS:
        L = "A" if deck == MT.CH_DECK1 else "B"
        # the three EQ bands must not be transposed
        for cc_num, band in ((MT.CC_EQ_HI, "hi"), (MT.CC_EQ_MID, "mid"),
                             (MT.CC_EQ_LOW, "low")):
            drive(deck, cc(deck, cc_num, 100), "sampler_eq", (L, band, 100))
        drive(deck, cc(deck, MT.CC_FILTER, 20), "sampler_filter", (L, 20))
        drive(deck, cc(deck, MT.CC_PITCH_HI, 90), "sampler_speed", (L, 90))
        drive(deck, cc(deck, MT.CC_LINE_FADER, 77), "sampler_gain", (L, 77))
        drive(deck, cc(deck, MT.CC_STRIP, 33), "sampler_seek", (L, 33))
        drive(deck, cc(deck, MT.CC_BEAT_KNOB, 1), "sampler_browse", (L, 1))
        drive(deck, note_on(deck, MT.NOTE_SYNC), "sampler_reverse", (L,))
        drive(deck, note_on(deck, MT.NOTE_WHEEL_MODE),
              "sampler_load_browsed", (L,))
        drive(deck, note_on(deck, MT.NOTE_TAP), "tap_tempo", ())
    # master section
    drive(MT.CH_MASTER, cc(MT.CH_MASTER, MT.CC_MASTER_GAIN, 111),
          "sampler_master_gain", (111,))
    # SHIFT+touchstrip sweeps the active rack slot's first control
    mt, eng = build_mt()
    eng.fx.rack.calls.clear()
    with capture_errors(mt):
        mt._shift[MT.CH_DECK1] = True
        mt.handle_msg(cc(MT.CH_DECK1, MT.CC_STRIP_SHIFT, 127))
    params = [c for c in eng.fx.rack.calls if c[0] == "param"]
    if not params or params[0][1] != 0 or params[0][2] != "wet":
        bad.append(f"fx_rack_param: {eng.fx.rack.calls}")
    check(not bad, "controls pass the right arguments to the engine",
          " | ".join(bad))


def test_malformed_config_cannot_kill_the_input_thread():
    """A hand-edit typo in a MIXTRACK_* map must not take the controller down.

    Dispatch reads action[0] and builds "_act_"+name OUTSIDE its try/except,
    and the controller's input thread in orc1.py has no try/except at all, so
    a malformed entry kills the thread -- leaving the MIDI port open, so the
    hotplug scanner never notices and only an orc1 restart recovers it.
    """
    import io as _io
    import contextlib as _c
    bad = []
    for label, entry in (("non-tuple", "sample_stop"),
                         ("None head", (None,)),
                         ("int head", (1, "A")),
                         ("empty tuple", ())):
        mt, _ = build_mt()
        buf = _io.StringIO()
        try:
            with _c.redirect_stdout(buf):
                resolved = mt._resolve_button_map({"PLAY": entry})
                mt._d1s_buttons = dict(mt._d1s_buttons)
                mt._d1s_buttons[MT.NOTE_PLAY] = entry
                mt._maps[MT.CH_DECK1] = (mt._d1s_buttons, mt._d1s_ccs)
                mt.handle_msg(note_on(MT.CH_DECK1, MT.NOTE_PLAY))
        except Exception as e:
            bad.append(f"{label}: {type(e).__name__} escaped handle_msg ({e})")
            continue
        if resolved:
            bad.append(f"{label}: was accepted into the map as {resolved}")
    check(not bad, "a malformed config entry cannot kill the input thread",
          " | ".join(bad))


def test_deck_letters_never_crossed():
    """Every deck-1 control must address deck 'A' and every deck-2 control
    'B'.  A crossed letter is invisible until you play both decks at once."""
    bad = []
    for deck in DECKS:
        letter = "A" if deck == MT.CH_DECK1 else "B"
        mt, eng = build_mt()
        eng.calls.clear()
        with capture_errors(mt):
            for n in ALL_NOTES:
                mt.handle_msg(note_on(deck, n))
                mt.handle_msg(note_off(deck, n))
            for c in ALL_CCS:
                mt.handle_msg(cc(deck, c, 100))
        for name, args in eng.calls:
            if not name.startswith("sampler_"):
                continue
            if not args or not isinstance(args[0], str):
                continue
            if args[0] in ("A", "B") and args[0] != letter:
                bad.append(f"deck{letter}: {name}{args}")
    check(not bad, "deck letters never crossed", " | ".join(sorted(set(bad))))


# ---------------------------------------------------------------------------
# Forward capture (2026-08-12) -- tap/hold routing on the top row
# ---------------------------------------------------------------------------

def test_forward_capture_dispatch():
    """The top-row pads route to retro vs forward capture per the deck's mode
    (XOR SHIFT), latched at press. Verifies the HANDLER calls the right engine
    facade; the scheduler math itself is _test_fwd_capture.py + live."""
    A, B = MT.CH_DECK1, MT.CH_DECK2
    PAD = MT.NOTE_PAD1              # a top-row pad -> ('sample_capture_bars',...)
    MODE = MT.NOTE_PAD_MODE         # -> ('mixtrack_fwd_mode',)

    def names(eng):
        return [c[0] for c in eng.calls]

    # default = retro on both decks
    mt, eng = build_mt()
    check(mt._fwd_mode[A] is False and mt._fwd_mode[B] is False,
          "forward mode defaults to retro on both decks")

    # RETRO: press fires retroactive grab; release adds no facade call
    mt, eng = build_mt()
    with capture_errors(mt):
        mt.handle_msg(note_on(A, PAD))
    check("sampler_capture_bars" in names(eng)
          and "sampler_capture_fwd_hold_start" not in names(eng),
          "retro press -> sampler_capture_bars")
    n_before = len(eng.calls)
    with capture_errors(mt):
        mt.handle_msg(note_off(A, PAD))
    check(len(eng.calls) == n_before, "retro release -> no extra facade call")

    # FORWARD: press -> fwd_hold_start ; release -> fwd_hold_end(which, bars)
    mt, eng = build_mt()
    mt._fwd_mode[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, PAD))
    check("sampler_capture_fwd_hold_start" in names(eng)
          and "sampler_capture_bars" not in names(eng),
          "forward press -> fwd_hold_start (not retro)")
    with capture_errors(mt):
        mt.handle_msg(note_off(A, PAD))
    ends = [c for c in eng.calls if c[0] == "sampler_capture_fwd_hold_end"]
    check(len(ends) == 1 and ends[0][1][0] == "A"
          and float(ends[0][1][1]) == 1.0,
          "forward release -> fwd_hold_end('A', 1.0)")

    # SHIFT inverts retro -> forward for one grab
    mt, eng = build_mt()
    mt._shift[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, PAD))
    check("sampler_capture_fwd_hold_start" in names(eng),
          "SHIFT+pad in retro mode -> forward")

    # SHIFT inverts forward -> retro for one grab
    mt, eng = build_mt()
    mt._fwd_mode[A] = True
    mt._shift[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, PAD))
    check("sampler_capture_bars" in names(eng)
          and "sampler_capture_fwd_hold_start" not in names(eng),
          "SHIFT+pad in forward mode -> retro")

    # MODE button toggles fwd_mode and cancels any armed grab
    mt, eng = build_mt()
    mt.handle_msg(note_on(A, MODE))
    check(mt._fwd_mode[A] is True, "PAD MODE press -> forward")
    mt.handle_msg(note_on(A, MODE))
    check(mt._fwd_mode[A] is False, "PAD MODE press again -> retro")
    check("sampler_capture_fwd_cancel" in names(eng),
          "mode toggle cancels any armed forward grab")

    # LATCH: press with SHIFT (forward), release WITHOUT shift -> still hold_end
    mt, eng = build_mt()
    mt._shift[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, PAD))
    mt._shift[A] = False                    # SHIFT released mid-gesture
    with capture_errors(mt):
        mt.handle_msg(note_off(A, PAD))
    check(any(c[0] == "sampler_capture_fwd_hold_end" for c in eng.calls),
          "retro/forward latched at press (SHIFT released mid-gesture)")

    # per-deck independence
    mt, eng = build_mt()
    mt.handle_msg(note_on(A, MODE))
    check(mt._fwd_mode[A] is True and mt._fwd_mode[B] is False,
          "mode toggle is per-deck (A on, B off)")


def test_led_feedback():
    """Metronome LED phase math, forward-mode pad tint, arm-pulse state (slice 4)."""
    A = MT.CH_DECK1
    tled = MT._tempo_led_on
    # TAP metronome flashes for _TEMPO_FLASH_S at each beat start (120bpm=0.5s)
    beat = 60.0 / 120.0
    check(tled(0.0, 0.0, 120.0) is True, "tempo LED on at beat start")
    check(tled(MT._TEMPO_FLASH_S + 0.01, 0.0, 120.0) is False,
          "tempo LED off after the flash window")
    check(tled(beat, 0.0, 120.0) is True, "tempo LED on again at next beat")
    check(tled(0.2, 0.0, 0.0) is False, "tempo LED off when bpm<=0")

    # arm blink takes BOTH states across a fraction of a second
    states = {MT._arm_blink_on(t / 100.0) for t in range(0, 40)}
    check(states == {True, False}, "arm blink toggles (both states seen)")

    # top-row tint follows the deck's mode
    mt, _ = build_mt()
    check(mt._grab_pad_color(A) == MT.LED_PURPLE, "retro deck -> purple grab pads")
    mt._fwd_mode[A] = True
    check(mt._grab_pad_color(A) == MT.LED_BLUE, "forward deck -> blue grab pads")

    # forward press arms the pad; release disarms + flashes a committed ack
    mt, _ = build_mt()
    mt._fwd_mode[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, MT.NOTE_PAD1))
    check(mt._fwd_arm.get(A) == MT.NOTE_PAD1, "forward press arms the pad")
    mt.flashes.clear()
    with capture_errors(mt):
        mt.handle_msg(note_off(A, MT.NOTE_PAD1))
    check(A not in mt._fwd_arm, "forward release disarms the pad")
    check((A, MT.NOTE_PAD1) in mt.flashes, "forward release flashes committed ack")

    # a mode toggle clears an in-progress arm
    mt, _ = build_mt()
    mt._fwd_mode[A] = True
    with capture_errors(mt):
        mt.handle_msg(note_on(A, MT.NOTE_PAD1))
    mt.handle_msg(note_on(A, MT.NOTE_PAD_MODE))
    check(A not in mt._fwd_arm, "mode toggle clears an armed pad")


def test_metronome_led_phase_locks_to_live_clock():
    """The TAP metronome LED must phase-lock to the live clock grid when one is
    running, and only free-run when it isn't.

    Regression for the external-sync 'TAP lights flutter like a drum voice' bug:
    the old _tick_leds free-ran the flash off a per-pulse BPM ESTIMATE, so clock
    jitter smeared the beat into chaos. Now it asks eng.sampler_beat_phase() and
    flashes on `since` (which resets on a real clock pulse), falling back to the
    free-running tick only when no clock is present.
    """
    A, B = MT.CH_DECK1, MT.CH_DECK2
    mt, eng = build_mt()
    mt._out = object()                       # defeat the 'no output' early-return
    sent = []
    mt._led = lambda note, deck, on=True, vel=127: sent.append((note, deck, bool(on)))
    def taps():
        return {(d, on) for (n, d, on) in sent if n == MT.NOTE_TAP}

    # (1) live clock, at the beat boundary -> ON. _safe_bpm is pinned to 0 so the
    #     free-running fallback would be OFF: this check FAILS if the LED ever
    #     free-runs instead of locking to the clock (the mutation guard).
    eng.sampler_beat_phase = lambda: (0.0, 0.5)
    mt._safe_bpm = lambda: 0.0
    mt._tap_led_on = False
    sent.clear(); mt._tick_leds()
    check((A, True) in taps() and (B, True) in taps(),
          "phase-lock: TAP on at the beat boundary (both decks)")

    # (2) live clock, past the flash window -> OFF, even with a fast free-run bpm.
    eng.sampler_beat_phase = lambda: (MT._TEMPO_FLASH_S + 0.05, 0.5)
    mt._safe_bpm = lambda: 100000.0          # free-run would be ~always ON
    mt._tap_led_on = True
    sent.clear(); mt._tick_leds()
    check((A, False) in taps(),
          "phase-lock: TAP off outside the flash window (ignores bpm)")

    # (3) the flash decision is governed by `since`, not the beat length -- an
    #     absurd beat_s cannot flip it. This is what makes it jitter-proof.
    eng.sampler_beat_phase = lambda: (0.0, 9.9)
    mt._safe_bpm = lambda: 0.0
    mt._tap_led_on = False
    sent.clear(); mt._tick_leds()
    check((A, True) in taps(),
          "phase-lock: on-state independent of beat length (jitter-proof)")

    # (4) no live clock -> None -> the free-running fallback still drives the LED.
    eng.sampler_beat_phase = lambda: None
    mt._safe_bpm = lambda: 0.0               # free-run -> unconditionally OFF
    mt._tap_led_on = True
    sent.clear(); mt._tick_leds()
    check((A, False) in taps() and (B, False) in taps(),
          "fallback: free-running tick used when no clock is present")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    verbose = "-v" in sys.argv
    if mido is None:
        print("mido not available -- cannot run")
        return 1
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            RESULTS.append((False, f"{t.__name__} RAISED",
                            traceback.format_exc().strip().splitlines()[-1]))
    npass = sum(1 for ok, _, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    for ok, label, detail in RESULTS:
        if ok and not verbose:
            continue
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label}")
        if detail and (not ok or verbose):
            for chunk in detail.split(" | "):
                if chunk.strip():
                    print(f"         {chunk.strip()}")
    print(f"\n  {npass} passed, {nfail} failed  "
          f"({len(RESULTS)} checks, expected {EXPECTED})")
    drift = len(RESULTS) != EXPECTED
    if drift:
        print(f"  !! CHECK COUNT IS {len(RESULTS)}, EXPECTED {EXPECTED} -- a "
              f"check was added or silently lost. Update EXPECTED deliberately "
              f"or find it.")
    return 1 if (nfail or drift) else 0


if __name__ == "__main__":
    sys.exit(main())
