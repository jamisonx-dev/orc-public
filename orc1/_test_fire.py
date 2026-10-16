#!/usr/bin/env python3
"""
ORC-1 Fire handler -- headless test harness + regression suite.
================================================================

WHY THIS EXISTS
---------------
orc1_fire.py is the biggest module in the project (6400+ lines, one class,
230+ methods) and was the only risky component with no test path at all.
Every bug in it had to be found by playing the instrument, and because the
file swallows almost every exception (81 `except Exception`, 68 of them a
bare `pass`) the failure mode is "the pad does nothing" with no journal
trail.  Several such bugs have survived for days or weeks.

Unlike orc1.py, FireHandler CAN be imported and constructed off-hardware:
its __init__ takes only the engine module and touches no engine attributes.
Stubbing the MIDI output, the two render threads and the state writer is
enough to drive the whole handler with synthetic MIDI and assert on what it
emits -- including which OLED screen it painted, which is exactly the
"menu reverts to the wrong screen" bug class.

    python3 _test_fire.py            # run the suite
    python3 _test_fire.py -v         # also list every silent no-op found

Pure stdlib apart from mido/orc1_config, which the Pi already has.
Runs on the Pi and on a dev box.  Read-only with respect to the live rig.


SAFETY -- READ BEFORE EDITING
-----------------------------
This suite is designed to be safe to run WHILE THE INSTRUMENT IS PLAYING.
Five things make that true.  Do not remove any of them:

  1. `_open_output` is overridden to a no-op.  The real one scans for a port
     whose name contains "FIRE" and opens it -- on the live rig that is the
     actual Akai Fire, and a test run would blast LEDs and OLED frames at a
     controller someone is playing.
  2. `_save_fire_state` is overridden to a no-op.  The live orc1 service
     writes ~/orc1_state.json every 60s; a test must never race it.
  3. HOME is redirected to a throwaway sandbox dir for the duration of each
     handler build, so every `~/...` read/write the handler does is isolated
     and deterministic (an absent ~/.orc1_bus_levels.json cleanly falls back
     to the config BOOT values).
  4. The render thread and OLED keepalive loop are stubbed out, so no
     background thread survives a test.
  5. (Obsolete 2026-08-06) The Fire ROUTE grid was removed -- routing now lives
     on /patch and the synth's live-graph auto-remove coupling -- so there is no
     `_route_restore_all` and no ~/orc1_routing.json to stub or guard.

  6. Any test that deliberately calls a REAL disk writer -- bypassing the
     stubs above, e.g. `orc1_fire.FireHandler._save_fire_state(fire)` in
     test_universal_banks, or `_save_knobmap` in the knobmap test -- MUST be
     wrapped in `sandbox_home()` itself.  `build_fire()` tears its sandbox
     down BEFORE returning (its docstring says so), and these writers resolve
     `~` at call time, so an unwrapped call writes the live rig's state file
     for real.  A sibling suite already did exactly this and silently
     overwrote ~/orc1_routing.json on every run -- see CLAUDE.md 2026-07-30.

  7. `_save_knobmap` is overridden to REFUSE any write whose target is not
     inside the current sandboxed HOME, raising instead of writing. Item 6 is
     the rule; this is the enforcement, added after the rule alone failed to
     prevent the 2026-07-31 clobber of the live ~/fire_knobmap.json. A test
     that wants persistence must set `fire._knobmap_file` to a path inside a
     `sandbox_home()` block.

Nothing here restarts a service or touches the Pi's live state files.
"""

import ast
import os
import sys
import json
import types
import shutil
import tempfile
import subprocess
import traceback
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orc1_config as cfg                                    # noqa: E402

try:
    import mido
except Exception:                                            # pragma: no cover
    mido = None


# ---------------------------------------------------------------------------
# Sandboxed HOME
# ---------------------------------------------------------------------------

_SANDBOX_MARKER = "ORC1_FIRETEST_SANDBOX"


def in_sandbox():
    """The sandbox dir if sandbox_home() is active, else None.

    An explicit marker is necessary: checking "is the target under $HOME" is
    NOT enough to tell a sandbox from the real home directory, because outside
    a sandbox the target legitimately IS under $HOME. The first version of the
    _save_knobmap guard made exactly that mistake and did not fire.
    """
    return os.environ.get(_SANDBOX_MARKER)


@contextlib.contextmanager
def sandbox_home(state=None, bus_levels=None):
    """Redirect ~ to a temp dir seeded with the given state files.

    Every path the handler resolves with expanduser()/$HOME lands here, so a
    test can pin the exact on-disk state it wants and can never touch (or be
    perturbed by) the live rig's files.
    """
    d = tempfile.mkdtemp(prefix="orc1_firetest_")
    old = {k: os.environ.get(k)
           for k in ("HOME", "USERPROFILE", _SANDBOX_MARKER)}
    try:
        os.environ["HOME"] = d
        os.environ["USERPROFILE"] = d
        os.environ[_SANDBOX_MARKER] = d
        if state is not None:
            with open(os.path.join(d, "orc1_state.json"), "w") as f:
                json.dump(state, f)
        if bus_levels is not None:
            with open(os.path.join(d, ".orc1_bus_levels.json"), "w") as f:
                json.dump(bus_levels, f)
        yield d
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Engine stub
# ---------------------------------------------------------------------------

class FakeArp:
    """Arp stub. Real attributes where the handler reads them for display;
    permissive no-ops for the facade methods it calls to mutate state."""
    def __init__(self):
        self.enabled      = False
        self.pattern_name = "UP"
        self.bpm          = 120.0
        # Must be a REAL value: the handler compares it against the
        # CLOCK_* constants imported from orc1_arp, and reads the matching
        # *_bpm float. A __getattr__ stub returns a function here and the
        # handler dies on `self._bpm() + step`.
        try:
            import orc1_arp
            self.clock_mode = orc1_arp.CLOCK_INTERNAL
        except Exception:
            self.clock_mode = "internal"
        self.internal_bpm = 120.0
        self.drift_bpm    = 120.0
        self.pattern      = 0
        self.octaves      = 1
        self.gate         = 0.5
        self.calls        = []

    def _estimated_bpm(self):
        return 120.0

    def set_enabled(self, on):
        self.enabled = bool(on)

    def set_internal_bpm(self, bpm):
        self.internal_bpm = float(bpm)

    def set_drift_bpm(self, bpm):
        self.drift_bpm = float(bpm)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        def _stub(*a, **k):
            self.calls.append((name, a))
            return None
        return _stub


class FakeEng:
    """Stand-in for the orc1 __main__ module the handler is handed.

    Anything not explicitly modelled resolves to a recording no-op, so the
    handler can reach for engine facades that only exist at runtime without
    the test having to model the entire brain.
    """

    def __init__(self):
        self.cfg = cfg
        self.arp = FakeArp()
        self.seq = None                     # set by build_fire()
        self.sent = []                      # (port, msg) the handler emitted
        self.calls = []                     # (name, args) engine facade calls
        self.ROOT_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G",
                           "Ab", "A", "Bb", "B"]
        self.SCALE_NAMES = getattr(cfg, "SCALE_NAMES", ["MAJ", "MIN"])
        self.state = {
            "root": 0, "chord_root": 0, "chord_root_name": "C",
            "scale": "MAJ", "chord_base": "MAJ", "chord_exts": set(),
            "chord_degree": "I", "octave": 4, "hold": False,
            "key_lock": False, "scale_snap": False, "base_override": None,
            "voicing": "CLOSE", "bpm": 120.0,
        }

    def send_to(self, port, msg):
        self.sent.append((port, msg))

    def send_synth_midi(self, *a, **k):
        self.sent.append(("synth", a))

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

import orc1_fire                                             # noqa: E402

# Painters that belong to exactly one view in steady state.  Derived from
# _oled()'s own per-view branch (orc1_fire.py ~2160-2290), not guessed.
# If one of these paints while a different view is active and no overlay is
# open, the OLED is lying about where you are -- the "menu reverts" bug.
VIEW_EXCLUSIVE_PAINTERS = {
    "_oled_faders":            {"faders"},
    "_oled_mpc_assign":        {"faders"},
    "_oled_universal":         {"universal"},
    "_oled_universal_assign":  {"universal"},
    "_oled_route":             {"fx"},
    "_fx_browse_oled":         {"fx"},
    "_oled_sculpt":            {"seq"},
    "_oled_track":             {"seq", "mixer"},
    "_oled_track_edit":        {"seq", "mixer"},
    "_oled_step_note":         {"seq", "mixer"},
    "_oled_browse":            {"seq", "mixer"},
    "_oled_chord_param_menu":  {"chord"},
    "_oled_chord_rows":        {"chord"},
    # NB: the KEY overlay is deliberately available on all three keyboard
    # views -- _key_overlay_active() gates on
    #     self.view in ("chord", "poly", "chip")
    # so painting it from poly/chip is correct, not a leak.  (An earlier
    # revision of this table listed it as chord-only and produced 30-odd
    # false positives.)
    "_oled_key_overlay":       {"chord", "poly", "chip"},
}

# Painters that are only legal once their edit mode has been entered; calling
# them cold raises KeyError on an unseeded working dict.  Not reachable in
# normal operation (both entry points fully seed the dict), so the suite
# drives them through the real entry path instead of cold.
PAINTER_PRECONDITION = {
    "_oled_mpc_assign":       lambda f: f._mpc_enter_assign(0),
    "_oled_universal_assign": lambda f: f._universal_enter_assign((0, 0)),
}

DISPATCH_PREFIXES = ("_act_", "_dial_", "_sel_", "_menuact_")

N_ROWS, N_COLS = orc1_fire.N_ROWS, orc1_fire.N_COLS

# Pristine fire_oled.show_lines, captured before any test wraps it.
_REAL_SHOW_LINES = (orc1_fire.fire_oled.show_lines
                    if orc1_fire.fire_oled is not None else None)


class HeadlessFire(orc1_fire.FireHandler):
    """FireHandler with all hardware and disk side effects removed.

    Records, for assertion:
        .oled_paints  [str]              which _oled_* painter ran, in order
        .oled_lines   [list[tuple]]      the text handed to fire_oled
        .unresolved   [(prefix, name)]   dispatches that silently did nothing
    """

    # -- side-effect removal (see SAFETY at the top of this file) -----------
    def _open_output(self, prefix=None):
        self._out = types.SimpleNamespace(
            send=lambda m: self._eng_ref.sent.append(("fire_out", m)),
            close=lambda: None)

    def _render_thread(self):
        pass

    def _oled_keepalive_loop(self):
        pass

    def _save_fire_state(self):
        pass

    def _save_knobmap(self):
        # SAFETY ITEM 7 -- DO NOT REMOVE. The real _save_knobmap() resolves
        # FIRE["KNOBMAP_FILE"] ("~/fire_knobmap.json") with expanduser AT CALL
        # TIME, so any test that reaches it outside a sandboxed HOME writes the
        # player's real knob map -- replacing every remap they have made with
        # whatever synthetic defaults the test happened to build.
        #
        # That is not hypothetical: it happened on 2026-07-31, via
        # _kmap_cc_commit() in test_knobmap_keeps_arbitrary_cc_out, and it
        # destroyed live assignments. It is the same failure as the
        # _test_route_buses.py incident of 2026-07-30 (CLAUDE.md), one day
        # apart, because the earlier lesson was written down but not enforced
        # in code. This guard enforces it: writes are refused unless the target
        # is inside the current (sandboxed) HOME, and loudly, not silently.
        path = self._knobmap_file
        if not path:
            return
        box  = in_sandbox()
        full = os.path.realpath(os.path.expanduser(path))
        if not box or not full.startswith(os.path.realpath(box) + os.sep):
            raise AssertionError(
                "_test_fire.py tried to write a knobmap OUTSIDE the sandbox:\n"
                f"    target  = {full}\n    sandbox = {box}\n"
                "Wrap the call in sandbox_home() and set fire._knobmap_file to\n"
                "a path inside it. See SAFETY item 6/7 at the top of this file.")
        return orc1_fire.FireHandler._save_knobmap(self)

    def _save_chord_rows(self):
        # SAFETY ITEM 8 (2026-09-24) -- DO NOT REMOVE. Same trap as item 7:
        # the real _save_chord_rows() resolves ~/.orc1_chord_rows.json at CALL
        # time, and test_button_sweep presses the chord-bank PAGE buttons on a
        # handler whose sandbox is already gone -- so every Pi run of this
        # suite overwrote the player's real chord-row banks with the headless
        # handler's defaults. Found when a new start-up save made it visible.
        # Unlike the knobmap guard this is a quiet no-op (the sweep presses
        # those buttons legitimately); the whole-run net in main() proves the
        # real file is never touched.
        box = in_sandbox()
        full = os.path.realpath(os.path.expanduser(orc1_fire._CHORD_ROWS_FILE))
        if not box or not full.startswith(os.path.realpath(box) + os.sep):
            self.unsandboxed_row_saves = getattr(self, "unsandboxed_row_saves", 0) + 1
            return
        return orc1_fire.FireHandler._save_chord_rows(self)

    def __init__(self, eng):
        self._eng_ref   = eng
        self.oled_paints = []
        self.oled_lines  = []
        self.oled_overflow = []   # lines that will not render inside 128x64
        self.unresolved  = []
        super().__init__(eng)


def _install_recorders(fire):
    """Wrap the OLED painters and the four dynamic-dispatch sites.

    Done by wrapping the bound methods on the instance rather than patching
    the class, so nothing leaks between tests.
    """
    # 1. painter identity
    for name in dir(fire):
        if not (name.startswith("_oled_") or name == "_fx_browse_oled"):
            continue
        if name in ("_oled_keepalive_loop",):
            continue
        fn = getattr(fire, name)
        if not callable(fn):
            continue
        def make(nm, f):
            def wrapper(*a, **k):
                fire.oled_paints.append(nm)
                return f(*a, **k)
            return wrapper
        try:
            setattr(fire, name, make(name, fn))
        except Exception:
            pass

    # 2. OLED text content -- and whether it actually FITS the panel.
    #
    # This is the standing net for the bug class found on 2026-07-31: every
    # assign screen on the Fire was written longer than the 128px display can
    # render, so the channel and output of a pad assignment were invisible for
    # as long as the feature had existed.  Nothing raises, nothing logs, and
    # over SSH everything looks perfect -- only a player holding the device
    # can see it.  So every line painted anywhere in the sweeps is measured
    # here, against fire_oled's own budget (see the note there for why that is
    # pure arithmetic and needs no font stack).
    if orc1_fire.fire_oled is not None:
        # Always restore from the PRISTINE function captured at import, not
        # from whatever is installed right now: if an earlier test forgot to
        # restore, saving the wrapper here would chain them and attribute one
        # test's overflowing lines to the next.
        real_show = _REAL_SHOW_LINES
        def show_lines(lines, *a, **k):
            fire.oled_lines.append(list(lines))
            for item in lines:
                try:
                    text, x, y, scale = item
                except Exception:
                    fire.oled_overflow.append(f"malformed draw tuple: {item!r}")
                    continue
                # Trailing spaces draw nothing -- render() starts from a blank
                # image every time, so padding a line to a fixed width is a
                # no-op, not an overflow. Leading spaces DO matter (the BROWSER
                # menu indents its non-selected items), so only strip the end.
                text = text.rstrip()
                if not orc1_fire.fire_oled.fits(text, x, scale):
                    fire.oled_overflow.append(
                        f"{len(text)} chars > "
                        f"{orc1_fire.fire_oled.max_chars(scale, x)} "
                        f"(x={x} scale={scale}): {text!r} "
                        f"-> renders as "
                        f"{orc1_fire.fire_oled.fit(text, x, scale)!r}")
                elif not orc1_fire.fire_oled.fits_box(text, x, y, scale):
                    fire.oled_overflow.append(
                        f"clipped at the bottom (y={y} scale={scale}): {text!r}")
            return None            # never touch the real device
        orc1_fire.fire_oled.show_lines = show_lines
        fire._restore_oled = lambda: setattr(
            orc1_fire.fire_oled, "show_lines", real_show)
    else:
        fire._restore_oled = lambda: None

    # Silent-dispatch detection is done statically instead -- see
    # test_dispatch_names().  Hooking __getattribute__ to catch it at runtime
    # is possible but recursion-prone and would slow every sweep by ~100x for
    # no extra coverage: the action tables are static data, so resolving them
    # against the class up front finds exactly the same defects.
    return fire


def build_fire(state=None, bus_levels=None, view=None):
    """Construct a HeadlessFire inside a sandboxed HOME.

    Returns (fire, eng, sandbox_dir_contents).  The sandbox is torn down
    before returning, so this is for handlers that do their disk reads during
    __init__ (which is all of them).
    """
    import io
    eng = FakeEng()
    buf = io.StringIO()
    with sandbox_home(state=state, bus_levels=bus_levels):
        # Construction is chatty (banner, voice select) and, when something
        # goes wrong, prints a swallowed-exception warning instead of raising.
        # Capture it: quiet output, and the warnings become assertable.
        with contextlib.redirect_stdout(buf):
            try:
                import orc1_seq
                eng.seq = orc1_seq.Sequencer(eng)
            except Exception:
                eng.seq = None
            fire = HeadlessFire(eng)
            _install_recorders(fire)
    fire.boot_log = buf.getvalue()
    if view:
        fire.view = view
    return fire, eng


# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

_RESULTS = []
VERBOSE = "-v" in sys.argv


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    if not cond:
        print(f"  FAIL  {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"        {line}")
    elif VERBOSE:
        print(f"  ok    {name}")


def section(title):
    print(f"\n--- {title} ---")


@contextlib.contextmanager
def capture_handler_errors(sink):
    """Collect the handler's own swallowed-exception prints.

    _dispatch_button/_dispatch_dial/_dispatch_select catch every exception an
    action raises and print `  ! Fire action 'x': ...` -- on the live rig that
    goes to the journal and nobody sees it. Here it becomes a test failure.
    """
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    for line in buf.getvalue().splitlines():
        s = line.strip()
        if s.startswith("!") or "[warn]" in s:
            sink.append(s)


# ---------------------------------------------------------------------------
# 1. STATIC: every action name in every map resolves to a real method
# ---------------------------------------------------------------------------

def _class_methods():
    src = open(os.path.join(HERE, "orc1_fire.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    cls = [n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "FireHandler"][0]
    return {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}, src, tree


def _literal_maps(tree, pred):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
            if pred(nm):
                try:
                    out[nm] = ast.literal_eval(node.value)
                except Exception:
                    pass
    return out


def test_dispatch_names():
    """An action name with no matching method is a SILENT no-op.

    orc1_fire.py:3180  `m = getattr(self, "_act_" + action[0], None)`
                       `if m:`            <- no else, no log
    Same shape for _dial_ (3215), _sel_ (3357), _menuact_ (4350).
    A typo or a renamed method therefore produces a dead control with no
    diagnostic whatsoever.  This test is the compile-time check the language
    doesn't give us.
    """
    section("static: dynamic-dispatch name resolution")
    methods, src, tree = _class_methods()

    checks = []   # (label, mapping{key: action_tuple}, prefix)

    mod_maps = _literal_maps(tree, lambda n: "BUTTON" in n.upper())
    for nm, m in mod_maps.items():
        checks.append((f"orc1_fire.{nm}", m, "_act_"))
    if isinstance(cfg.FIRE.get("BUTTONS"), dict):
        checks.append(("FIRE['BUTTONS']", cfg.FIRE["BUTTONS"], "_act_"))

    for key in ("DIALS", "DIALS_SHIFT", "DIALS_ALT", "TRACK_EDIT_KNOBS"):
        v = cfg.FIRE.get(key)
        if isinstance(v, dict):
            checks.append((f"FIRE[{key!r}]", v, "_dial_"))
        elif isinstance(v, (list, tuple)):
            checks.append((f"FIRE[{key!r}]", dict(enumerate(v)), "_dial_"))
    for nm, m in _literal_maps(tree, lambda n: "DIAL" in n.upper()).items():
        checks.append((f"orc1_fire.{nm}", dict(enumerate(m))
                       if isinstance(m, (list, tuple)) else m, "_dial_"))

    for key in ("SELECT", "SELECT_ALT", "SELECT_SHIFT"):
        v = cfg.FIRE.get(key)
        if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
            checks.append((f"FIRE[{key!r}]", {key: v}, "_sel_"))

    total = 0
    for label, mapping, prefix in checks:
        if not isinstance(mapping, dict):
            continue
        bad = []
        for key, action in mapping.items():
            if not (isinstance(action, (list, tuple))
                    and action and isinstance(action[0], str)):
                continue
            total += 1
            if prefix + action[0] not in methods:
                bad.append(f"{key!r} -> {action[0]!r}  (no {prefix}{action[0]})")
        check(f"{label}: all names resolve to {prefix}*",
              not bad, "\n".join(bad))
    check("scanned at least 40 action references", total >= 40,
          f"only scanned {total}")


def test_no_dead_act_methods():
    """A dispatch handler named by nothing is dead code -- usually the residue
    of a map edit that forgot the handler, or vice versa.

    Handlers are reached by BARE name (`("view", "chord")` -> `_act_view`), and
    menu actions are keyed by menu-item name, so the check is: does the bare
    suffix appear as a string literal in orc1_fire.py or orc1_config.py?
    Counting occurrences of the full method name instead reports every
    correctly-wired handler as dead.
    """
    section("static: unreferenced action handlers")
    methods, src, tree = _class_methods()
    cfg_src = open(os.path.join(HERE, "orc1_config.py"), encoding="utf-8").read()
    haystack = src + cfg_src
    dead = []
    for m in sorted(methods):
        for pre in DISPATCH_PREFIXES:
            if not m.startswith(pre):
                continue
            bare = m[len(pre):]
            named = (f'"{bare}"' in haystack or f"'{bare}'" in haystack)
            called = (f"self.{m}" in src)
            if not named and not called:
                dead.append(m)
            break
    check("no dispatch handler is defined-but-never-named", not dead,
          "dead: " + ", ".join(dead) if dead else "")


def test_key_overlay_pin_cannot_trap_the_keyboard():
    """A pinned KEY overlay owns EVERY pad, so no note can sound while it is up.

    That is intended for a setup screen, but it shipped able to trap the player:
    _pad_press() consults _key_overlay_active() before any view handling, and
    the latch keeps that true with ALT released. The instrument simply went
    silent with nothing to explain it. These are the guards that make it
    escapable rather than a trap.
    """
    section("key overlay: a pin must never silently trap the keyboard")
    fire, _eng = build_fire(state={})
    fire.view = "poly"

    fire._key_overlay_latched = True
    fire._alt = False
    check("pinned + ALT released still shows the overlay (by design)",
          fire._key_overlay_active())

    # 1. Changing view must drop the pin, or it lurks and reclaims the grid
    #    the moment you return to a keyboard view.
    fire._set_view("seq")
    check("changing view clears the pin", not fire._key_overlay_latched)
    fire._set_view("poly")
    check("...and it does not come back when you return",
          not fire._key_overlay_active())

    # 2. BROWSER is the documented escape hatch.
    fire._key_overlay_latched = True
    fire._act_browser(True)
    check("BROWSER clears the pin", not fire._key_overlay_latched)

    # 2b. PAT_SONG (FX view) assigns self.view DIRECTLY instead of calling
    #     _set_view(), so it needs its own cleanup. Without it, a quick trip to
    #     the FX view and back silently reclaimed the whole grid -- the same
    #     dead-keyboard bug in a new shape. (PERFORM's old universal-view toggle
    #     was removed 2026-09-02 -- PERFORM now just opens chord via _set_view.)
    for label, act in (("PAT_SONG/fx", fire._act_fx_view),):
        fire.view = "chord"
        fire._shift = fire._alt = False
        fire._key_overlay_latched = True
        act(True); act(False)               # press + release = one toggle
        check(f"{label} clears the pin", not fire._key_overlay_latched,
              f"view={fire.view} latched={fire._key_overlay_latched}")

    # 3. SHIFT deactivates the overlay (it gates on `not shift`), so the grid
    #    MUST repaint on that edge or the screen lies about what a press does.
    #    Reset the view first: 2b's toggle left it on fx, where the overlay
    #    cannot be active at all and the edge would never fire.
    fire.view = "poly"
    fire._shift = False
    fire._key_overlay_latched = True
    fire._alt = False
    painted = []
    real_render = fire._render
    fire._render = lambda *a, **k: painted.append(1)
    try:
        fire._act_shift(True)
    finally:
        fire._render = real_render
    check("pressing SHIFT while pinned repaints the grid", painted,
          "no repaint -> grid shows the overlay while presses go to the keyboard")


def test_menu_and_overlay_block_pad_input():
    """A menu/overlay owning the OLED must also own the pads, or the grid
    silently edits live state underneath a screen that isn't showing it.

    _dispatch_dial() and _dispatch_select() (the encoders) already check
    _menu_open and every self._overlay value before falling through to
    per-view dispatch. _pad_press() checked neither -- so with the BROWSER
    menu, or any named overlay (kmap/kmap_cc/track_edit/chord_rows/
    fx_browse/preset_picker) visibly open, every pad press still reached
    the seq/mixer/etc. handler underneath and mutated real state (e.g.
    toggling a step) while the OLED showed something else entirely.
    """
    section("pad input: a menu/overlay must block pads, not just the OLED")
    fire, eng = build_fire(state={}, view="seq")
    if not fire.n_tracks:
        check("fixture has at least one seq track to probe", False,
              "n_tracks == 0, cannot exercise the guard")
        return

    fire.view_top = 0
    fire.page     = 0
    tidx, step = 0, 0
    trk = fire.tracks[tidx]
    before = bool(trk["steps"][step])
    note = orc1_fire.PAD_NOTE_BASE   # row 0, col 0 -> track 0, step 0

    for label, setup in (
        ("BROWSER menu open",       lambda: setattr(fire, "_menu_open", True)),
        ("browse overlay open",     lambda: setattr(fire, "_overlay", "browse")),
        ("kmap overlay open",       lambda: setattr(fire, "_overlay", "kmap")),
        ("kmap_cc overlay open",    lambda: setattr(fire, "_overlay", "kmap_cc")),
        ("track_edit overlay open", lambda: setattr(fire, "_overlay", "track_edit")),
        ("chord_rows overlay open", lambda: setattr(fire, "_overlay", "chord_rows")),
        ("fx_browse overlay open",  lambda: setattr(fire, "_overlay", "fx_browse")),
        ("preset_picker overlay open", lambda: setattr(fire, "_overlay", "preset_picker")),
    ):
        fire._menu_open = False
        fire._overlay   = None
        trk["steps"][step] = before
        setup()
        fire._pad_press(note, 127)
        after = bool(trk["steps"][step])
        check(f"{label}: pad press does not toggle the step underneath it",
              after == before,
              f"step was {before}, is now {after}")

    # And the guard must not be permanent -- once the overlay/menu clears,
    # the same pad must work normally again.
    fire._menu_open = False
    fire._overlay   = None
    trk["steps"][step] = before
    fire._pad_press(note, 127)
    check("pad works normally again once menu/overlay clears",
          bool(trk["steps"][step]) != before,
          "step did not toggle with nothing open -- guard is over-blocking")

    # _set_view() must clear every overlay (previously only track_edit was
    # cleared), and the BROWSER menu, or a leftover overlay/menu keeps
    # painting the wrong screen (or nothing, for the un-guarded ones like
    # preset_picker/kmap/kmap_cc/chord_rows/fx_browse) across a view switch.
    for ov in ("browse", "kmap", "kmap_cc", "track_edit", "chord_rows",
               "fx_browse", "preset_picker"):
        fire._overlay = ov
        fire._menu_open = True
        fire._set_view("mixer")
        check(f"_set_view clears overlay '{ov}'", fire._overlay is None)
        check(f"_set_view clears _menu_open (was '{ov}')", not fire._menu_open)
        fire._set_view("seq")

    # 2026-08-04 follow-up review: _act_fx_view assigns self.view DIRECTLY
    # instead of calling _set_view() (see the KEY-overlay pin test above for
    # why), so it needs the SAME overlay/menu cleanup by hand -- the first pass
    # only added it to _set_view() and the FX mode-jump pads, missing this.
    for label, act in (("PAT_SONG/fx", fire._act_fx_view),):
        fire.view = "chord"
        fire._shift = fire._alt = False
        fire._overlay = "browse"
        fire._menu_open = True
        act(True); act(False)
        check(f"{label} clears leftover _overlay",
              fire._overlay is None, f"view={fire.view} overlay={fire._overlay}")
        check(f"{label} clears leftover _menu_open",
              not fire._menu_open, f"view={fire.view}")
        fire._set_view("seq")   # reset for the next iteration / later checks

    # 2026-08-04 follow-up review: SHIFT+REC (_add_track) is reachable from
    # every view via _dispatch_button, but used to set _overlay="track_edit"
    # without checking self.view -- track_edit's OLED render is gated to
    # seq/mixer, so calling this from e.g. chord view left the screen looking
    # completely normal while the pad-blocking guard above killed every pad
    # anyway, with nothing on screen to explain why. _add_track() now jumps
    # to seq view itself so the overlay it sets is the one actually shown.
    fire.view = "chord"
    fire._shift = True
    fire._alt = False
    fire._overlay = None
    fire._act_seq_clear(True)
    check("SHIFT+REC from chord view lands somewhere track_edit is visible",
          fire.view in ("seq", "mixer"), f"view={fire.view}")
    check("...and the overlay it set is actually track_edit",
          fire._overlay == "track_edit", f"overlay={fire._overlay}")
    fire._shift = False
    fire._overlay = None
    fire._set_view("seq")

    # 2026-08-04 follow-up review: "chord_params" (set by SELECT-press in
    # chord view) was missing from _oled()'s overlay dispatch table entirely,
    # so any repaint after entry silently fell back to the ordinary chord
    # view render while pads stayed blocked -- a dead grid with a completely
    # normal-looking screen. Confirm _oled() now recognizes it.
    fire.view = "chord"
    calls = []
    real = fire._chord_params_oled
    fire._chord_params_oled = lambda *a, **k: calls.append(1)
    try:
        fire._overlay = "chord_params"
        fire._oled()
    finally:
        fire._chord_params_oled = real
    check("_oled() routes 'chord_params' to its own painter", calls,
          "fell through to the normal per-view render instead")
    fire._overlay = None


def test_scale_mode_cycle_order():
    """The cycle must not land on 'filter' first.

    'filter' SILENCES off-scale notes; every other mode corrects them. The old
    control was a boolean SCALE SNAP whose single press meant "snap on", so a
    cycle starting off -> filter turns long-standing muscle memory into a
    half-dead keyboard.
    """
    section("scale modes: cycle order is deliberate")
    modes = getattr(orc1_fire, "_KEY_MODE_COLORS", {})
    check("the Fire knows a colour for every engine mode",
          set(modes) == set(cfg_scale_modes()),
          f"fire={sorted(modes)} engine={sorted(cfg_scale_modes())}")
    order = cfg_scale_modes()
    check("'off' is first", order and order[0] == "off", f"{order}")
    check("the first press from off gives 'nearest', not 'filter'",
          len(order) > 1 and order[1] == "nearest", f"{order}")
    check("'filter' is last", order and order[-1] == "filter", f"{order}")


def cfg_scale_modes():
    """SCALE_MODES lifted from orc1.py without importing it."""
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SCALE_MODES":
                    return tuple(ast.literal_eval(node.value))
    return ()


# ---------------------------------------------------------------------------
# 2. LIFECYCLE: construction and state restore
# ---------------------------------------------------------------------------

def test_construction():
    section("lifecycle: construction")
    fire, eng = build_fire(state={})
    check("constructs with an empty state file", fire is not None)
    check("lands in a known view", fire.view in orc1_fire.VIEWS,
          f"view={fire.view!r}")
    # LED/OLED traffic to the (stubbed) Fire itself is expected at boot; what
    # must NOT happen is the handler emitting notes/CCs to the synth or the
    # DIN out just by being constructed.
    external = [(p, m) for p, m in eng.sent if p != "fire_out"]
    check("emits no external MIDI during construction", not external,
          f"{len(external)}: {external[:4]}")
    for attr in ("_mpc_faders", "_universal_pads", "_dialmap", "_cc_vals"):
        check(f"self.{attr} exists after __init__", hasattr(fire, attr))
    # A swallowed exception during construction prints "[warn] ..." and
    # silently abandons whatever it was doing.  Boot must be warning-free.
    warns = [l for l in fire.boot_log.splitlines()
             if "[warn]" in l or l.strip().startswith("!")]
    check("construction produces no swallowed-exception warnings",
          not warns, "\n".join(warns))


def test_state_restore_without_saved_faders():
    """REGRESSION -- the `_mpc_faders` warning in the journal.

    __init__ calls _load_fire_state() at line 595, but self._mpc_faders is
    not created until line 607.  Inside the loader, _sync_volume_fader_values()
    reads _mpc_faders -> AttributeError -> swallowed by the bare `except` ->
    THE REST OF THE LOADER NEVER RUNS.  Everything restored after that point
    (universal pads) is silently discarded.

    Only reproduces when the state file has no saved fader bank, which is why
    it was intermittent.
    """
    section("lifecycle: state restore with no saved fader bank")
    state = {
        "fire_mpc_faders": {},                       # <- the trigger
        "fire_universal_pads": {
            "0,0": {"type": "cc", "number": 7, "channel": 1, "port": "synth"}
        },
        "fire_chip_base_note": 48,
    }
    fire, eng = build_fire(state=state)
    check("universal pad assignment survives the load",
          fire._universal_pads.get((0, 0)) is not None,
          f"_universal_pads = {fire._universal_pads!r}")


def test_saved_state_is_not_clobbered_by_init():
    """REGRESSION -- __init__ overwrites what _load_fire_state restored.

    _load_fire_state() runs at line 595.  Line 607 then does
        self._mpc_faders   = self._default_volume_faders()
    and line 625
        self._universal_pads = {}
    both unconditionally.  So even on the success path a saved fader bank and
    saved UCS pad assignments are thrown away -- which is the opposite of what
    the comment at line 605 claims.
    """
    section("lifecycle: saved state survives the rest of __init__")
    state = {
        "fire_mpc_faders": {
            "0": {"type": "cc", "number": 100, "channel": 3,
                  "port": "pisound", "value": 42, "label": "MYFADER"}
        },
        "fire_universal_pads": {
            "1,2": {"type": "note", "number": 64, "channel": 5,
                    "port": "pisound", "value": 100, "latch": False}
        },
    }
    fire, eng = build_fire(state=state)
    f0 = fire._mpc_faders.get(0) or {}
    check("saved fader bank survives __init__",
          f0.get("number") == 100 and f0.get("label") == "MYFADER",
          f"fader 0 = {f0!r}")
    check("saved universal pad survives __init__",
          fire._universal_pads.get((1, 2)) is not None,
          f"_universal_pads = {fire._universal_pads!r}")


# ---------------------------------------------------------------------------
# 3. INPUT SWEEP: no gesture may raise, in any view, on any layer
# ---------------------------------------------------------------------------

def _modifier_notes(fire):
    shift = alt = None
    for note, action in fire._buttons.items():
        if not (isinstance(action, (list, tuple)) and action):
            continue
        if action[0] == "shift":
            shift = note
        elif action[0] == "alt":
            alt = note
    return shift, alt


def _msg(kind, **kw):
    return mido.Message(kind, **kw)


def test_unresolved_action_is_loud():
    """A control wired to a nonexistent action must SAY SO.

    This is the counterpart to test_dispatch_names: the static check keeps
    the shipped maps honest, this one guarantees that if a bad name ever does
    reach the hardware -- from a runtime-built map, a knobmap JSON, a preset --
    it announces itself instead of presenting as a dead control.
    """
    section("diagnostics: unresolved action names are reported")
    if mido is None:
        check("mido available", False, "mido not importable")
        return
    fire, eng = build_fire(state={})
    orc1_fire._UNRESOLVED_SEEN.clear()
    sink = []
    with capture_handler_errors(sink):
        fire._dispatch_button(("definitely_not_a_real_action",), True)
    check("unresolved _act_ name is logged",
          any("definitely_not_a_real_action" in s for s in sink),
          f"captured: {sink!r}")

    # ...and only once, however many times the control is hit.
    orc1_fire._UNRESOLVED_SEEN.clear()
    sink2 = []
    with capture_handler_errors(sink2):
        for _ in range(25):
            fire._dispatch_button(("another_missing_action",), True)
    hits = [s for s in sink2 if "another_missing_action" in s]
    check("repeated hits log once, not once per event", len(hits) == 1,
          f"{len(hits)} log lines from 25 presses")
    try:
        fire._restore_oled()
    except Exception:
        pass


def test_swallowed_hotpath_error_is_deduped():
    """A swallowed HOT-PATH error must leave ONE breadcrumb, not one per frame.

    The counterpart to test_unresolved_action_is_loud for the fallback sites
    instrumented 2026-08-21: _set_scale/_set_root/_set_chord_base/
    _chord_root_name/_effective_base each try an engine facade and fall through
    to a local fallback on failure -- the fallback keeps the control working,
    which is exactly how a broken engine setter stayed invisible behind a silent
    `except: pass`.  Some run on every OLED render; the chip-CC and cc_out knob
    pushes fire per encoder click.  So the trace has to be DEDUPED or a single
    knob sweep floods the journal.  This guards the mechanism against a revert
    to a bare `pass` (no output) or to an undeduped print (one line per call).
    """
    section("diagnostics: hot-path swallowed errors are deduped")
    import io
    orc1_fire._SWALLOW_SEEN.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(25):
            orc1_fire._swallowed("set_scale", ValueError("boom"))
        orc1_fire._swallowed("effective_base", KeyError("k"))
    lines = buf.getvalue().splitlines()
    hits = [l for l in lines if "set_scale" in l]
    check("a repeated hot-path swallow logs once, not once per call",
          len(hits) == 1, f"{len(hits)} lines from 25 calls")
    check("the line names the site + error and says repeats are suppressed",
          bool(hits) and "boom" in hits[0] and "suppress" in hits[0],
          repr(hits[0] if hits else None))
    check("a different site still surfaces its own first failure",
          any("effective_base" in l for l in lines),
          repr(lines))
    # Same site, a DIFFERENT exception type, is a distinct signal worth seeing.
    orc1_fire._SWALLOW_SEEN.clear()
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        orc1_fire._swallowed("cc_out send", ValueError("a"))
        orc1_fire._swallowed("cc_out send", ValueError("a"))   # deduped
        orc1_fire._swallowed("cc_out send", TypeError("b"))    # new type -> shown
    l2 = [l for l in buf2.getvalue().splitlines() if "cc_out send" in l]
    check("same site dedups per exception type, not per site", len(l2) == 2,
          f"{len(l2)} lines (want 2: ValueError once + TypeError once)")

    # Source-level net: the helper-behaviour checks above don't prove any SITE
    # actually calls it, so a revert of one shim back to `except: pass` would
    # slip through.  The 2026-08-21 pass wired 8 hot-path fallback sites; assert
    # they're still wired (>= so the count can grow, not silently shrink).
    src = open(os.path.join(HERE, "orc1_fire.py"), encoding="utf-8").read()
    n_sites = src.count('_swallowed("')          # call sites (the def has no quote)
    check("the hot-path fallback sites still call _swallowed", n_sites >= 8,
          f"only {n_sites} _swallowed(\"...\") call sites (expected >= 8)")


def test_button_sweep():
    """Every button, in every view, on every modifier layer.

    This is the test that would have caught the ext-pad press/release
    asymmetry and the ungated _oled_sculpt: it drives the real dispatch path
    and fails on any exception the handler would otherwise have printed to a
    journal nobody was reading.
    """
    section("sweep: every button x every view x every layer")
    if mido is None:
        check("mido available for the input sweep", False, "mido not importable")
        return

    failures = []
    painted_wrong = []
    swallowed = []
    overflow = []
    gestures = 0

    with capture_handler_errors(swallowed):
      for view in orc1_fire.VIEWS:
        for layer in ("base", "shift", "alt"):
            fire, eng = build_fire(state={})
            shift_n, alt_n = _modifier_notes(fire)
            mod = {"base": None, "shift": shift_n, "alt": alt_n}[layer]
            buttons = sorted(fire._buttons.keys())
            for note in buttons:
                action = fire._buttons[note]
                if isinstance(action, (list, tuple)) and action \
                        and action[0] in ("shift", "alt"):
                    continue          # the modifier itself
                # fresh view + no lingering overlay for each gesture
                fire.view = view
                fire._overlay = None
                fire._menu_open = False
                fire.oled_paints.clear()
                try:
                    if mod is not None:
                        fire.handle_msg(_msg("note_on", note=mod, velocity=127))
                    fire.handle_msg(_msg("note_on", note=note, velocity=127))
                    fire.handle_msg(_msg("note_off", note=note, velocity=0))
                    if mod is not None:
                        fire.handle_msg(_msg("note_off", note=mod, velocity=0))
                    gestures += 1
                except Exception:
                    failures.append(
                        f"view={view} layer={layer} btn={note} "
                        f"action={action!r}\n"
                        + traceback.format_exc(limit=6))
                    continue

                # menu coherence: did a painter for a DIFFERENT view run,
                # while we are still in `view` and no overlay is open?
                if fire.view == view and not fire._overlay and not fire._menu_open:
                    for p in fire.oled_paints:
                        owners = VIEW_EXCLUSIVE_PAINTERS.get(p)
                        if owners and view not in owners:
                            painted_wrong.append(
                                f"view={view} layer={layer} btn={note} "
                                f"action={action!r} painted {p} "
                                f"(belongs to {sorted(owners)})")
            overflow.extend(fire.oled_overflow)
            try:
                fire._restore_oled()
            except Exception:
                pass

    check(f"no button gesture raises ({gestures} gestures)",
          not failures, "\n".join(failures[:6]))
    check("no gesture paints another view's OLED screen",
          not painted_wrong, "\n".join(sorted(set(painted_wrong))[:12]))
    _oled_overflow_check(overflow, "button sweep")
    check("no button gesture triggers a swallowed handler error",
          not swallowed, "\n".join(sorted(set(swallowed))[:12]))


def test_encoder_sweep():
    """The four knobs and SELECT, both directions, every view and layer.

    SELECT-turn is where the 2026-07-22 'menu reverts to drum voices' bug
    lived (_sel_scroll_voice painted _oled_sculpt from any view).
    """
    section("sweep: encoders + SELECT x every view x every layer")
    if mido is None:
        check("mido available for the encoder sweep", False, "mido not importable")
        return

    dial_ccs = sorted(getattr(orc1_fire, "DIAL_CC", {}).keys())
    sel_cc = getattr(orc1_fire, "SELECT_CC", 118)
    failures = []
    painted_wrong = []
    swallowed = []
    overflow = []
    turns = 0

    with capture_handler_errors(swallowed):
      for view in orc1_fire.VIEWS:
        for layer in ("base", "shift", "alt"):
            fire, eng = build_fire(state={})
            shift_n, alt_n = _modifier_notes(fire)
            mod = {"base": None, "shift": shift_n, "alt": alt_n}[layer]
            for cc in list(dial_ccs) + [sel_cc]:
                for val in (1, 127):          # +1 and -1 relative
                    fire.view = view
                    fire._overlay = None
                    fire._menu_open = False
                    fire.oled_paints.clear()
                    try:
                        if mod is not None:
                            fire.handle_msg(_msg("note_on", note=mod, velocity=127))
                        fire.handle_msg(_msg("control_change",
                                             control=cc, value=val))
                        if mod is not None:
                            fire.handle_msg(_msg("note_off", note=mod, velocity=0))
                        turns += 1
                    except Exception:
                        failures.append(
                            f"view={view} layer={layer} cc={cc} val={val}\n"
                            + traceback.format_exc(limit=6))
                        continue
                    if fire.view == view and not fire._overlay \
                            and not fire._menu_open:
                        for p in fire.oled_paints:
                            owners = VIEW_EXCLUSIVE_PAINTERS.get(p)
                            if owners and view not in owners:
                                painted_wrong.append(
                                    f"view={view} layer={layer} cc={cc} "
                                    f"painted {p} (belongs to {sorted(owners)})")
            overflow.extend(fire.oled_overflow)
            try:
                fire._restore_oled()
            except Exception:
                pass

    check(f"no encoder turn raises ({turns} turns)",
          not failures, "\n".join(failures[:6]))
    check("no encoder turn paints another view's OLED screen",
          not painted_wrong, "\n".join(sorted(set(painted_wrong))[:12]))
    _oled_overflow_check(overflow, "encoder sweep")
    check("no encoder turn triggers a swallowed handler error",
          not swallowed, "\n".join(sorted(set(swallowed))[:12]))


def test_pad_sweep():
    """Every pad, pressed and released, in every view.

    Press/release asymmetry is a recurring bug here (the chord extension pads
    were inert on press and retriggered on release for months).  A pad that
    raises on release is invisible in normal play until it wedges something.
    """
    section("sweep: every pad press+release x every view")
    if mido is None:
        check("mido available for the pad sweep", False, "mido not importable")
        return

    base = getattr(orc1_fire, "PAD_NOTE_BASE", 54)
    top  = getattr(orc1_fire, "PAD_NOTE_MAX", 117)
    failures = []
    swallowed = []
    pads = 0
    with capture_handler_errors(swallowed):
      for view in orc1_fire.VIEWS:
        fire, eng = build_fire(state={})
        for note in range(base, top + 1):
            fire.view = view
            fire._overlay = None
            fire._menu_open = False
            try:
                fire.handle_msg(_msg("note_on", note=note, velocity=127))
                fire.handle_msg(_msg("note_off", note=note, velocity=0))
                pads += 1
            except Exception:
                failures.append(f"view={view} pad={note}\n"
                                + traceback.format_exc(limit=6))
        try:
            fire._restore_oled()
        except Exception:
            pass
    check(f"no pad press/release raises ({pads} pads)",
          not failures, "\n".join(failures[:6]))
    check("no pad gesture triggers a swallowed handler error",
          not swallowed, "\n".join(sorted(set(swallowed))[:12]))
    _oled_overflow_check(fire, "pad sweep")


# ---------------------------------------------------------------------------
# 4. OLED: every painter runs without raising
# ---------------------------------------------------------------------------

def test_oled_painters():
    """Call every zero-argument _oled_* painter directly.

    A painter that raises is invisible: _oled() is called from the render
    path, where the exception is swallowed and the screen simply keeps its
    previous content -- which reads to the player as "the menu froze".
    """
    section("oled: every painter survives a direct call")
    fire, eng = build_fire(state={})
    failures = []
    called = 0
    import inspect
    for name in sorted(dir(orc1_fire.FireHandler)):
        if not (name.startswith("_oled_") or name == "_fx_browse_oled"):
            continue
        if name == "_oled_keepalive_loop":
            continue
        fn = getattr(orc1_fire.FireHandler, name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            required = [p for p in list(sig.parameters.values())[1:]
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_ONLY,
                                       p.POSITIONAL_OR_KEYWORD)]
        except Exception:
            continue
        if required:
            continue          # needs arguments; covered by the sweeps instead
        try:
            pre = PAINTER_PRECONDITION.get(name)
            if pre:
                pre(fire)     # enter the edit mode this painter belongs to
            getattr(fire, name)()
            called += 1
        except Exception:
            failures.append(f"{name}\n" + traceback.format_exc(limit=5))
    try:
        fire._restore_oled()
    except Exception:
        pass
    check(f"no zero-arg OLED painter raises ({called} painters)",
          not failures, "\n".join(failures[:6]))
    _oled_overflow_check(fire, "direct painter calls")


def test_oled_lines_do_not_overlap():
    """No two lines of the same screen may collide vertically.

    fits_box() asks "does THIS line stay on the panel", which is a different
    question from "does it stay clear of the line above it" -- so the width
    net missed a screen whose four rows each fit individually and every one of
    which overlapped its neighbour. (The chord-row config overlay: fixing an
    off-the-bottom clip by tightening the pitch to 11px traded one invisible
    defect for another.)

    Modelling this arithmetically does not work -- the vertical extent of a
    line depends on which glyphs it contains -- so it is measured, by
    rendering a descender-heavy worst case at each screen's real coordinates.
    Needs Pillow, so it runs on the Pi and skips on a bare dev box.
    """
    section("oled: lines of one screen do not collide")
    if orc1_fire.fire_oled is None:
        check("fire_oled importable for the overlap check", False,
              "fire_oled not available (needs Pillow) -- run this on the Pi")
        return
    try:
        orc1_fire.fire_oled.render([("x", 2, 0, 1)])
    except Exception as exc:
        check("Pillow available for the overlap check", False, repr(exc))
        return

    # Collect the distinct GEOMETRIES every screen paints, from the sweeps.
    layouts = set()
    for fire in _sweep_all_views():
        for lines in fire.oled_lines:
            geo = tuple((len(t.rstrip()), int(x), int(y), int(s))
                        for t, x, y, s in lines)
            if len(geo) > 1:
                layouts.add(geo)
        try:
            fire._restore_oled()
        except Exception:
            pass

    worst = "Apgqy|Wj"        # ascender + descender heavy
    W, H  = orc1_fire.fire_oled.OLED_W, orc1_fire.fire_oled.OLED_H

    def box(n, x, y, scale):
        """Bounding box of an n-char worst-case string drawn at (x, y)."""
        if n <= 0:
            return None
        text = (worst * (n // len(worst) + 1))[:n]
        px = orc1_fire.fire_oled.render([(text, x, y, scale)]).load()
        rows = [r for r in range(H) if any(px[c, r] for c in range(W))]
        cols = [c for c in range(W) if any(px[c, r] for r in range(H))]
        if not rows:
            return None
        return (cols[0], rows[0], cols[-1], rows[-1])

    bad = []
    for geo in sorted(layouts):
        boxes = [(g, box(*g)) for g in geo]
        for i in range(len(boxes)):
            gi, bi = boxes[i]
            if bi is None:
                continue
            for j in range(i + 1, len(boxes)):
                gj, bj = boxes[j]
                if bj is None:
                    continue
                # Only a defect if they collide in BOTH axes. Several screens
                # deliberately put a small line beside a large one (the giant
                # track number with its right-hand column, MIXER with its bpm
                # readout) -- those share rows on purpose and are not stacked.
                if (bi[1] <= bj[3] and bj[1] <= bi[3]
                        and bi[0] <= bj[2] and bj[0] <= bi[2]):
                    bad.append(f"{gi} and {gj} overlap "
                               f"(rows {bi[1]}-{bi[3]} vs {bj[1]}-{bj[3]}, "
                               f"cols {bi[0]}-{bi[2]} vs {bj[0]}-{bj[2]})")
    check(f"no screen's lines overlap ({len(layouts)} layouts)",
          not bad, "\n".join(sorted(set(bad))[:8]))


def _sweep_all_views():
    """Yield a handler per view with its OLED painted, for geometry capture."""
    for view in orc1_fire.VIEWS:
        fire, eng = build_fire(state={}, view=view)
        try:
            fire._oled()
        except Exception:
            pass
        # Also the edit screens, which are where the tight layouts live.
        for name, pre in PAINTER_PRECONDITION.items():
            try:
                pre(fire)
                getattr(fire, name)()
            except Exception:
                pass
        for name in ("_oled_kmap", "_oled_kmap_cc", "_oled_chord_rows",
                     "_oled_universal", "_oled_faders", "_oled_preset_picker",
                     "_oled_route", "_oled_menu", "_oled_subscreen"):
            try:
                getattr(fire, name)()
            except Exception:
                pass
        yield fire


def test_oled_truncations_are_all_deliberate():
    """Every fit() that actually truncates must say WHY.

    fit() is an escape hatch that removes a line from the width net: fits()
    is trivially true for text already cut to the budget, so wrapping a line
    in fit() silently opts it out of the check. That is precisely how the CC
    OUT editor shipped rendering "universal  E0 cc" -- the CC number, the
    entire point of the screen, clipped off in the one view the feature
    exists for -- while 129 green checks said nothing. Reported from the
    field within minutes of deploy.

    So: truncating a genuinely variable-length thing (a plugin name, a track
    name, a port key) is fine and must be TAGGED. An untagged truncation
    means a fixed string or -- far worse -- a VALUE is being clipped, and the
    fix for that is to write the line shorter, not to hide it behind fit().
    """
    section("oled: every truncation is tagged as deliberate")
    if orc1_fire.fire_oled is None:
        check("fire_oled importable for the truncation check", False,
              "fire_oled not available -- run this on the Pi")
        return

    # Static half: no fit() call anywhere may omit why=.
    src  = open(os.path.join(HERE, "orc1_fire.py"), encoding="utf-8").read()
    bare = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "fit":
            if not any(k.arg == "why" for k in node.keywords):
                bare.append(f"line {node.lineno}")
    check("no fire_oled.fit() call omits why=", not bare, ", ".join(bare))

    # Dynamic half: drive every screen and inspect what actually got cut.
    orc1_fire.fire_oled.TRUNCATIONS.clear()
    for fire in _sweep_all_views():
        try:
            fire._restore_oled()
        except Exception:
            pass
    untagged = [(t, b) for why, t, b in orc1_fire.fire_oled.TRUNCATIONS
                if not why]
    check("no screen truncated an untagged string",
          not untagged,
          "\n".join(f"{t!r} -> {b} chars" for t, b in untagged[:8]))
    orc1_fire.fire_oled.TRUNCATIONS.clear()


def test_cc_editor_shows_and_changes_the_cc():
    """The CC number must be VISIBLE and MOVABLE from encoder 0.

    The field report was "could not see the cc at all or change anything
    besides channel and output" -- one symptom, because a value you cannot
    read looks like a knob that does nothing.
    """
    section("cc editor: the CC is readable and encoder 0 moves it")
    if orc1_fire.fire_oled is None:
        check("fire_oled importable for the CC editor check", False, "no PIL")
        return
    for view in orc1_fire.VIEWS:          # 'universal' is the longest name
        fire, eng = build_fire(view=view)
        fire._enter_kmap()
        fire._kmap_knob, fire._kmap_layer = 0, "base"
        fire._enter_kmap_cc()
        fire._kmap_cc = {"port": "pisound", "channel": 16, "cc": 127}
        fire.oled_lines.clear()
        eng.sent.clear()
        fire._oled_kmap_cc()
        painted = [t for lines in fire.oled_lines for (t, _x, _y, _s) in lines]
        shown   = [t for t in painted if "cc" in t]
        check(f"{view}: the CC value is fully visible",
              any("127" in t for t in shown),
              f"cc line(s) painted: {shown!r}")
        # Encoder 0 must reach the editor through the REAL dial dispatch.
        # Start mid-range: the value clamps at 0..127, so testing from 127
        # would measure the clamp instead of the encoder.
        fire._kmap_cc["cc"] = 64
        eng.sent.clear()
        fire._dispatch_dial(0, +1)
        fire._dispatch_dial(0, -3)
        check(f"{view}: encoder 0 changes the CC",
              fire._kmap_cc["cc"] == 62, f"64 -> {fire._kmap_cc['cc']}")
        # ...and must not have driven a real parameter on the way. Only MIDI
        # to a PORT counts -- 'fire_out' entries are pad-grid SysEx going back
        # to the controller's own LEDs, which every render emits.
        midi = [m for port, m in eng.sent if port != "fire_out"]
        check(f"{view}: encoder 0 sent no MIDI while editing",
              not midi, repr(midi[:3]))
        try:
            fire._restore_oled()
        except Exception:
            pass


def test_no_import_time_home_paths():
    """No module-level constant may bake in an absolute path under ~.

    `_CHORD_ROWS_FILE = os.path.expanduser("~/.orc1_chord_rows.json")` was
    resolved at IMPORT time, before any test could redirect HOME -- so it
    defeated even a correctly sandboxed test and would have written the live
    rig's file. That is the same class as the 2026-07-31 knobmap clobber, and
    the reason this check is static rather than another per-writer stub:
    guarding writers one at a time only ever covers the ones somebody thought
    of. Keep "~" paths as raw strings and expanduser() them at the point of
    use.
    """
    section("static: no ~ path resolved at import time")
    src  = open(os.path.join(HERE, "orc1_fire.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    bad  = []

    def scan(body, where):
        """Assignments evaluated at import: module level AND class bodies.

        Checking only tree.body missed `_BUS_LEVELS_FILE = expanduser(...)`
        inside `class FireHandler:` -- a class-body assignment runs when the
        class statement executes, which is import time just the same. One
        syntactic level deeper than the first version of this check looked.
        """
        for node in body:
            if isinstance(node, ast.ClassDef):
                scan(node.body, f"class {node.name}")
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", ""))
                if name in ("expanduser", "expandvars", "realpath", "abspath"):
                    tgt = getattr(node, "target", None) or node.targets[0]
                    lbl = getattr(tgt, "id", "<expr>")
                    bad.append(f"line {node.lineno} ({where}): "
                               f"{lbl} = ...{name}(...)")

    scan(tree.body, "module level")
    check("no import-time constant resolves a ~ path (module or class body)",
          not bad, "\n".join(bad))


def _oled_overflow_check(source, label):
    """Shared assertion: nothing painted during this test ran off the panel.

    `source` is either a handler (single-instance tests) or an already
    accumulated list (the sweeps, which build a fresh handler per view).
    """
    items = getattr(source, "oled_overflow", source)
    check(f"no OLED line overflows 128x64 ({label})",
          not items,
          "\n".join(sorted(set(items))[:10]))


# ---------------------------------------------------------------------------
# 5. OLED: the assign screens fit at their WORST case
# ---------------------------------------------------------------------------

def test_oled_assign_screens_fit_worst_case():
    """Paint every assign screen with the longest values it can ever hold.

    The sweeps exercise these screens at their DEFAULTS (note 60, value 100,
    channel 1, port pisound), which is exactly why the original overflow
    survived: the defaults are short enough that a casual look seems fine.
    The failure only shows at three-digit numbers, two-digit channels, a long
    port key and a latch flag -- i.e. once you have actually set something up.
    So pin the maxima explicitly.
    """
    section("oled: assign screens at maximum field widths")
    if orc1_fire.fire_oled is None:
        check("fire_oled importable for the OLED budget check", False,
              "fire_oled not available (needs Pillow) -- run this on the Pi")
        return
    fire, eng = build_fire(state={})
    # "pisound" and "synth" get 3-char labels (DIN/SYN); the real worst case
    # is any OTHER port key, which _port_label uppercases and truncates to
    # FOUR characters -- so the test must use one, or it validates a field one
    # character narrower than the code can actually produce.
    worst = {"type": "note", "number": 127, "value": 127,
             "channel": 16, "port": "sampler", "latch": True}
    # Guard the test's own premise, as a reported check rather than an assert:
    # if _port_label ever stops capping at 4 characters this test would quietly
    # stop being the worst case, and the width net would still pass because the
    # field it validates got narrower than the code can emit.
    check("the port label is capped at 4 chars (test premise)",
          len(orc1_fire._port_label(worst["port"])) == 4,
          repr(orc1_fire._port_label(worst["port"])))
    try:
        # Universal pad assign: every type, every layer, the last bank.
        fire.view = "universal"
        fire._universal_bank = fire._universal_nbanks - 1
        for typ in ("note", "cc", "pc"):
            for layer in ("base", "shift", "alt"):
                fire._universal_enter_assign((N_ROWS - 1, N_COLS - 1))
                fire._universal_assign_layer = layer
                fire._universal_edit = dict(worst, type=typ)
                fire._oled_universal_assign()
        # Universal play screen, assigned / unassigned / nothing touched.
        fire._universal_assign_mode = False
        fire._universal_pads[(N_ROWS - 1, N_COLS - 1)] = dict(worst)
        fire._last_universal_pad = (N_ROWS - 1, N_COLS - 1)
        fire._oled_universal()
        fire._last_universal_pad = (0, 0)          # exists but unassigned
        fire._oled_universal()
        fire._last_universal_pad = None
        fire._oled_universal()
        # Fader assign + play screens.
        fire.view = "faders"
        fire._mpc_enter_assign(63)
        fire._mpc_edit = dict(worst, type="cc")
        fire._oled_mpc_assign()
        fire._mpc_assign_mode = False
        fire._mpc_faders[63] = dict(worst, type="cc",
                                    label="A VERY LONG FADER NAME")
        fire._mpc_last_fader = 63
        fire._oled_faders()
        # Knob map + the CC OUT editor, with the longest label in the menu.
        for view in ("seq", "universal", "faders"):
            fire.view = view
            for layer in ("base", "shift", "alt"):
                fire._kmap_layer = layer
                for knob in range(4):
                    fire._kmap_knob = knob
                    for sel in range(len(fire._kmap_targets())):
                        fire._kmap_sel = sel
                        fire._oled_kmap()
                        fire._oled_kmap(confirm=True)
                    fire._kmap_cc = {"port": "pisound", "channel": 16, "cc": 127}
                    fire._oled_kmap_cc()
    finally:
        try:
            fire._restore_oled()
        except Exception:
            pass
    _oled_overflow_check(fire, "assign screens, maximum widths")
    check("assign screens actually painted",
          len(fire.oled_lines) > 20, f"only {len(fire.oled_lines)} paints")


# ---------------------------------------------------------------------------
# 6. Universal Control Surface: pad banks
# ---------------------------------------------------------------------------

def test_universal_page_knobs():
    """Per-page (per-bank) knob overrides on the universal surface (Fire reorg P2).

    Read-overlay model: an override wins on its page; absent / other-bank / null
    falls back to the baseline _dialmap["universal"]; validation matches the
    knobmap loader; _dispatch_dial actually consults the overlay; it round-trips
    through the state file (save AND load), and an invalid entry in the file is
    rejected on load.
    """
    section("universal: per-page knobs")
    fire, eng = build_fire(state={}, view="universal")
    CC74 = ("cc_out", "synth", 8, 74)

    # -- validation: the surface takes SEND actions only (+none) ---------------
    check("valid: a well-formed free CC send", fire._valid_universal_knob(CC74))
    check("valid: a named cc_out preset (synth cutoff)",
          fire._valid_universal_knob(("cc_out", "synth", 8, 74)))
    check("valid: none (park the knob)", fire._valid_universal_knob(("none",)))
    check("invalid: drum voice_param rejected on the surface",
          not fire._valid_universal_knob(("voice_param", "attack")))
    check("invalid: bpm is an internal target, not a send action",
          not fire._valid_universal_knob(("bpm",)))
    check("invalid: spread (chord engine) rejected on the surface",
          not fire._valid_universal_knob(("spread",)))
    check("invalid: unknown action rejected",
          not fire._valid_universal_knob(("bogus_xyz",)))
    check("invalid: empty rejected", not fire._valid_universal_knob(()))

    # -- setter stores valid, rejects bad --------------------------------------
    check("set accepts a valid action", fire.set_universal_knob(0, "base", 0, CC74))
    check("stored on the right (bank,layer,idx)",
          fire._universal_knobs.get(0, {}).get("base", {}).get(0) == CC74,
          repr(fire._universal_knobs))
    check("set rejects an invalid action",
          fire.set_universal_knob(0, "base", 1, ("voice_param", "a")) is False)
    check("set rejects a bad knob index",
          fire.set_universal_knob(0, "base", 9, ("bpm",)) is False)
    check("set rejects a bad layer",
          fire.set_universal_knob(0, "nope", 0, ("bpm",)) is False)

    # -- read-overlay: the effective action ------------------------------------
    check("override wins on its own page",
          fire._universal_knob_action("base", 0) == CC74)
    fire._universal_bank = 1
    check("another bank falls back (universal base is empty)",
          fire._universal_knob_action("base", 0) is None)
    # The universal baseline is now BLANK on every layer (send-only surface, the
    # bpm/track_vel/spread debris was removed 2026-09-02) -- an unassigned knob
    # falls back to nothing rather than to an internal ORC action.
    check("baseline is blank on the CC surface (no internal debris)",
          fire._universal_knob_action("shift", 0) is None
          and fire._universal_knob_action("alt", 1) is None)
    fire._universal_bank = 0

    # -- clear falls back ------------------------------------------------------
    check("clearing an override returns True",
          fire.set_universal_knob(0, "base", 0, None) is True)
    check("cleared knob falls back to the baseline",
          fire._universal_knob_action("base", 0) is None)

    # -- dispatch END-TO-END actually consults the overlay ---------------------
    # FakeEng records send_to as (port, Message) and send_synth_midi as
    # ("synth", (args...)); flatten both shapes to the actual messages.
    def _all_msgs(sent):
        out = []
        for _p, a in sent:
            if hasattr(a, "control"):
                out.append(a)
            elif isinstance(a, (tuple, list)):
                out.extend(m for m in a if hasattr(m, "control"))
        return out
    fire.set_universal_knob(0, "base", 0, CC74)
    fire.view = "universal"; fire._universal_bank = 0
    eng.sent.clear()
    fire._dispatch_dial(0, +5)
    check("universal knob dispatch fires the per-page CC override",
          any(m.control == 74 for m in _all_msgs(eng.sent)),
          f"sent={eng.sent}")
    fire._universal_bank = 1          # no override here, base baseline is empty
    eng.sent.clear()
    fire._dispatch_dial(0, +5)
    check("no override + empty baseline dispatches nothing",
          not eng.sent, f"sent={eng.sent}")
    fire._universal_bank = 0

    # -- persistence: REAL save + load round-trip ------------------------------
    # HeadlessFire._save_fire_state is a no-op by design (never race the live
    # service), so drive the real one exactly like the pad round-trip above.
    fire.set_universal_knob(2, "alt", 3, CC74)
    with sandbox_home() as home:
        orc1_fire.FireHandler._save_fire_state(fire)
        with open(os.path.join(home, "orc1_state.json")) as fh:
            saved = json.load(fh)
        fire2, _ = build_fire(state=saved, view="universal")
        with sandbox_home(state=saved):
            fire2._load_fire_state()
    check("the saved file carries the knobs key",
          saved.get("fire_universal_knobs", {}).get("2", {}).get("alt", {})
          .get("3") == list(CC74),
          repr(saved.get("fire_universal_knobs")))
    check("per-page knob survives a save/load round trip",
          fire2._universal_knobs.get(2, {}).get("alt", {}).get(3) == tuple(CC74),
          repr(fire2._universal_knobs))

    # -- LOAD path validates: a junk entry in the file is dropped --------------
    bad = {"fire_universal_knobs": {"0": {"base": {"0": ["voice_param", "a"]}}}}
    fbad, _ = build_fire(state=bad)
    with sandbox_home(state=bad):
        fbad._load_fire_state()
    check("invalid per-page knob in the file is rejected on load",
          fbad._universal_knobs.get(0, {}).get("base", {}).get(0) is None,
          repr(fbad._universal_knobs))


def test_universal_fader_element():
    """Universal fader ROWS (Fire reorg P3): a row becomes a horizontal CC fader
    (16 pads = one 0-127 level) overriding per-pad behaviour; a column tap sets
    the level + sends CC; the row renders as a bar; set/clear + persistence."""
    section("universal: fader rows")
    fire, eng = build_fire(state={}, view="universal")
    fire._universal_bank = 0

    def _msgs(sent, attr):
        out = []
        for _p, a in sent:
            if hasattr(a, attr):
                out.append(a)
            elif isinstance(a, (tuple, list)):
                out.extend(m for m in a if hasattr(m, attr))
        return out

    # -- set / validate --------------------------------------------------------
    check("set_universal_fader accepts a valid row",
          fire.set_universal_fader(0, 1, cc=74, channel=8, port="synth"))
    check("stored on (bank,row) with its cc",
          fire._universal_faders.get(0, {}).get(1, {}).get("cc") == 74)
    check("bad cc rejected", not fire.set_universal_fader(0, 2, cc=200))
    check("bad channel rejected",
          not fire.set_universal_fader(0, 2, cc=10, channel=99))
    check("bad row rejected", not fire.set_universal_fader(0, 9, cc=10))

    # -- input: a column tap sets the level and sends its CC -------------------
    fire.view = "universal"; fire._universal_bank = 0
    eng.sent.clear()
    fire._pad_press(orc1_fire.PAD_NOTE_BASE + 1 * N_COLS + (N_COLS - 1), 100)
    ccs = _msgs(eng.sent, "control")
    check("full-right tap sends CC74 = 127",
          any(m.control == 74 and m.value == 127 for m in ccs), f"sent={eng.sent}")
    check("fader value stored = 127", fire._universal_faders[0][1]["value"] == 127)
    eng.sent.clear()
    fire._pad_press(orc1_fire.PAD_NOTE_BASE + 1 * N_COLS + 0, 100)
    check("full-left tap sets the low step ((0+1)*127//16 = 7)",
          fire._universal_faders[0][1]["value"] == 7,
          repr(fire._universal_faders[0][1]))

    # -- a fader row OVERRIDES a per-pad assignment underneath it --------------
    fire._universal_pads[(1, 5)] = {"type": "note", "number": 60, "value": 100,
                                    "channel": 3, "port": "pisound"}
    eng.sent.clear()
    fire._pad_press(orc1_fire.PAD_NOTE_BASE + 1 * N_COLS + 5, 100)
    check("a fader row ignores the per-pad note underneath it",
          not any(m.type == "note_on" for m in _msgs(eng.sent, "type")),
          f"sent={eng.sent}")

    # -- render draws a bar proportional to the value -------------------------
    fire._universal_faders[0][1]["value"] = 64
    captured = []
    fire._send_pads = lambda cells: captured.extend(cells)
    fire._render_universal()
    row1 = {idx % N_COLS: (rr, gg, bb) for (idx, rr, gg, bb) in captured
            if idx // N_COLS == 1}
    lit = round(64 * N_COLS / 127)          # = 8
    check("fader bar: cols below the level differ from cols above",
          lit == 8 and row1.get(0) != row1.get(N_COLS - 1)
          and row1.get(0) == (0, 0x30, 0x30) and row1.get(15) == (0, 0x06, 0x06),
          f"lit={lit} c0={row1.get(0)} c15={row1.get(15)}")

    # -- clear returns the row to per-pad -------------------------------------
    check("clear_universal_fader removes it",
          fire.clear_universal_fader(0, 1)
          and fire._universal_faders.get(0, {}).get(1) is None)

    # -- persistence round-trip (real save/load) ------------------------------
    fire.set_universal_fader(2, 0, cc=20, channel=5, port="pisound")
    fire._universal_faders[2][0]["value"] = 100
    with sandbox_home() as home:
        orc1_fire.FireHandler._save_fire_state(fire)
        with open(os.path.join(home, "orc1_state.json")) as fh:
            saved = json.load(fh)
        fire2, _ = build_fire(state=saved, view="universal")
        with sandbox_home(state=saved):
            fire2._load_fire_state()
    check("fader survives save/load with its value",
          fire2._universal_faders.get(2, {}).get(0, {}).get("cc") == 20
          and fire2._universal_faders[2][0]["value"] == 100,
          repr(fire2._universal_faders))

    badf = {"fire_universal_faders": {"0": {"0": {"cc": 999, "channel": 1,
                                                  "port": "x"}}}}
    fb, _ = build_fire(state=badf)
    with sandbox_home(state=badf):
        fb._load_fire_state()
    check("a bad fader (cc out of range) is rejected on load",
          fb._universal_faders.get(0, {}).get(0) is None, repr(fb._universal_faders))


def test_universal_banks():
    """GRID <> pages banks; every bank persists; the old key migrates."""
    section("universal: pad banks")
    fire, eng = build_fire(state={}, view="universal")
    n = fire._universal_nbanks
    check("more than one pad bank exists", n > 1, f"nbanks={n}")

    # Paging clamps rather than wrapping.
    fire._universal_set_bank(-1)
    check("GRID left at bank 0 stays at bank 0", fire._universal_bank == 0,
          f"bank={fire._universal_bank}")
    for _ in range(n + 3):
        fire._universal_set_bank(+1)
    check("GRID right clamps at the last bank",
          fire._universal_bank == n - 1, f"bank={fire._universal_bank}")

    # The GRID buttons must actually reach the paging code. Driving
    # _universal_set_bank() directly proves the mechanism but NOT the wiring:
    # a mutation that deletes the _act_page branch entirely left every bank
    # assertion passing, because nothing exercised the button path.
    fire._universal_bank = 0
    fire.view = "universal"
    fire._act_page(True, +1)
    check("GRID right pages the bank from the real button action",
          fire._universal_bank == 1, f"bank={fire._universal_bank}")
    fire._act_page(True, -1)
    check("GRID left pages back",
          fire._universal_bank == 0, f"bank={fire._universal_bank}")
    # ...and must not page banks from another view.
    fire.view = "seq"
    fire._act_page(True, +1)
    check("GRID does not page banks outside the universal view",
          fire._universal_bank == 0, f"bank={fire._universal_bank}")
    fire.view = "universal"

    # A pad physically HELD across a bank change must get its note-off. Once
    # the bank changes, _pad_release looks the pad up in the NEW bank and finds
    # nothing -- so without an explicit release the note sustains forever on
    # whatever external gear it was sent to.
    fire._universal_bank = 0
    fire.view = "universal"
    fire._universal_pads[(1, 1)] = {"type": "note", "number": 64, "value": 100,
                                    "channel": 3, "port": "pisound",
                                    "latch": False}
    eng.sent.clear()
    fire._pad_press(orc1_fire.PAD_NOTE_BASE + 1 * N_COLS + 1, 100)
    held_after_press = dict(fire._universal_held)
    fire._universal_set_bank(+1)
    offs = [m for p, m in eng.sent
            if getattr(m, "type", "") == "note_off" and getattr(m, "note", 0) == 64]
    check("a held universal pad is note-off'd when the bank changes",
          bool(held_after_press) and len(offs) == 1,
          f"held={held_after_press} note_offs={len(offs)}")
    check("held-pad bookkeeping is cleared by the bank change",
          not fire._universal_held, repr(fire._universal_held))
    fire._universal_bank = 0
    fire._universal_pads.pop((1, 1), None)

    # Each bank holds its own pads, and _universal_pads follows the bank.
    fire._universal_bank = 0
    fire._universal_pads[(0, 0)] = {"type": "note", "number": 60, "value": 100,
                                    "channel": 1, "port": "pisound"}
    fire._universal_bank = 1
    check("a pad assigned in bank 0 is absent from bank 1",
          (0, 0) not in fire._universal_pads,
          f"bank1 keys={list(fire._universal_pads)}")
    fire._universal_pads[(0, 0)] = {"type": "cc", "number": 74, "value": 64,
                                    "channel": 2, "port": "synth"}
    fire._universal_bank = 0
    check("bank 0's pad survived writing bank 1",
          fire._universal_pads.get((0, 0), {}).get("number") == 60,
          repr(fire._universal_pads.get((0, 0))))

    # Rebinding the attribute (what orc1.py's UNIVERSAL_CLEAR_PAD does from
    # another module) must land in the CURRENT bank, not orphan the reference.
    fire._universal_bank = 1
    fire._universal_pads = {}
    check("rebinding _universal_pads clears only the current bank",
          not fire._universal_banks[1] and fire._universal_banks[0],
          f"b0={fire._universal_banks[0]} b1={fire._universal_banks[1]}")

    # Round-trip through the real save/load pair.
    fire._universal_banks[1] = {(2, 3): {"type": "cc", "number": 74,
                                         "value": 64, "channel": 5,
                                         "port": "synth"}}
    with sandbox_home() as home:
        orc1_fire.FireHandler._save_fire_state(fire)
        with open(os.path.join(home, "orc1_state.json")) as fh:
            saved = json.load(fh)
        fire2, _ = build_fire(state=saved, view="universal")
        with sandbox_home(state=saved):
            fire2._load_fire_state()
    check("bank 0 survives a save/load round trip",
          fire2._universal_banks[0].get((0, 0), {}).get("number") == 60,
          repr(fire2._universal_banks[0]))
    check("bank 1 survives a save/load round trip",
          fire2._universal_banks[1].get((2, 3), {}).get("channel") == 5,
          repr(fire2._universal_banks[1]))
    check("the legacy fire_universal_pads key still mirrors bank 0",
          saved.get("fire_universal_pads", {}).get("0,0", {}).get("number") == 60,
          repr(saved.get("fire_universal_pads")))

    # A pre-bank state file migrates into bank 0 instead of being dropped.
    legacy = {"fire_universal_pads": {"1,2": {"type": "note", "number": 42,
                                              "value": 99, "channel": 3,
                                              "port": "pisound"}}}
    fire3, _ = build_fire(state=legacy, view="universal")
    with sandbox_home(state=legacy):
        fire3._load_fire_state()
    check("a pre-bank saved grid migrates into bank 0",
          fire3._universal_banks[0].get((1, 2), {}).get("number") == 42,
          repr(fire3._universal_banks[0]))

    # Junk must not create phantom banks GRID can never reach.
    junk = {"fire_universal_banks": {"99": {"0,0": {"type": "note",
                                                    "number": 1}},
                                     "x": {}, "0": {"9,9": {}, "0,1": {
                                         "type": "cc", "number": 7}}}}
    fire4, _ = build_fire(state=junk, view="universal")
    with sandbox_home(state=junk):
        fire4._load_fire_state()
    check("an out-of-range bank index is ignored",
          set(fire4._universal_banks) == set(range(fire4._universal_nbanks)),
          repr(sorted(fire4._universal_banks)))
    check("an out-of-range row/col is ignored",
          (9, 9) not in fire4._universal_banks[0]
          and (0, 1) in fire4._universal_banks[0],
          repr(fire4._universal_banks[0]))
    for f in (fire, fire2, fire3, fire4):
        try:
            f._restore_oled()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 7. Knob mapping: arbitrary CC out
# ---------------------------------------------------------------------------

def test_knobs_name_modules():
    """Internal knob targets NAME a module and follow /matrix (2026-09-24).

    Before: every internal target was a literal channel written when the module
    lived there. Poly and chip P2 swapped channels on /matrix, so the POLY
    view's knobs (ch 8) drove chip P2, "p2vibd" still pointed at ch 10, and the
    Fire's cached channels only caught up after an orc1 restart.
    """
    section("knobs: internal targets name a module, resolved live")
    wf = orc1_fire._well_formed_send_action
    check("a send may name a module on the synth bus",
          wf(("cc_out", "synth", "poly", 74)) and wf(("pitch_out", "synth", "chip_p2")))
    check("a module name is refused off the synth bus / when unknown",
          not wf(("cc_out", "pisound", "poly", 74))
          and not wf(("cc_out", "synth", "nosuchmodule", 74)))

    fire, eng = build_fire(view="poly")
    eng._bindings = {"poly": 6, "chip_p2": 8, "chip_p1": 9, "gb_wave": 5}
    poly_sends = [a for L in ("base", "shift")
                  for a in fire._dialmap["poly"][L].values() if a[0] == "cc_out"]
    check("the POLY view's knobs name the poly module (not ch 8)",
          poly_sends and all(a[2] == "poly" for a in poly_sends), f"{poly_sends}")

    def ccs_sent():
        return [(m.channel + 1, m.control) for p, m in eng.sent
                if p == "synth" and getattr(m, "type", "") == "control_change"]
    eng.sent.clear()
    fire._dial_cc_out(0, 1, "synth", "poly", 74)
    check("a poly knob sends on poly's LIVE channel (6)", ccs_sent() == [(6, 74)], f"{ccs_sent()}")
    eng._bindings["poly"] = 3          # moved on /matrix: no restart, no re-save
    eng.sent.clear()
    fire._dial_cc_out(0, 1, "synth", "poly", 74)
    check("...and follows a /matrix move at once (3)", ccs_sent() == [(3, 74)], f"{ccs_sent()}")
    del eng._bindings["poly"]           # taken out of the fold
    eng.sent.clear()
    fire._dial_cc_out(0, 1, "synth", "poly", 74)
    check("an UNBOUND module's knob sends nothing (never a stale channel)",
          ccs_sent() == [], f"{ccs_sent()}")
    eng.sent.clear()
    fire._dial_cc_out(0, 1, "pisound", 10, 74)
    lit = [(m.channel + 1, m.control) for p, m in eng.sent if p == "pisound"]
    check("a plain channel number still means exactly that channel", lit == [(10, 74)], f"{lit}")

    named = {lbl: tuple(a) for lbl, a in cfg.FIRE.get("KNOB_TARGETS", [])}
    named.update({lbl: tuple(a) for lbl, a in orc1_fire._DEFAULT_KNOB_TARGETS})
    check("named targets name modules (p2vibd = chip_p2, not ch 10; synflt = poly)",
          named.get("p2vibd", (0, 0, None))[2] == "chip_p2"
          and named.get("synflt", (0, 0, None))[2] == "poly",
          f"p2vibd={named.get('p2vibd')} synflt={named.get('synflt')}")

    fire._select_chip_voice("pulse2")
    chip_sends = [a for L in ("base", "shift", "alt")
                  for a in fire._dialmap["chip"][L].values()
                  if a[0] in ("cc_out", "pitch_out")]
    check("chip view knobs for Pulse 2 name chip_p2",
          chip_sends and all(a[2] == "chip_p2" for a in chip_sends), f"{chip_sends}")
    check("the chip KEYBOARD plays P2's live channel (8)", fire._chip_ch_now == 8,
          f"{fire._chip_ch_now}")
    eng._bindings["chip_p2"] = 12
    check("...and follows a /matrix move without a restart", fire._chip_ch_now == 12)
    eng._bindings["poly"] = 6
    check("the poly keyboard follows poly's binding", fire._poly_ch_now == 6)

    # One-time migration of a saved literal-channel map.
    with sandbox_home() as home:
        f2, e2 = build_fire(view="poly")
        f2._knobmap_file = os.path.join(home, "fire_knobmap.json")
        with open(f2._knobmap_file, "w") as fh:
            json.dump({"poly": {"base": {"0": ["cc_out", "synth", 8, 74]}},
                       "seq": {"base": {"0": ["cc_out", "synth", 10, 77],
                                        "1": ["pitch_out", "synth", 5]}},
                       "universal": {"base": {"0": ["cc_out", "synth", 8, 20],
                                              "1": ["cc_out", "pisound", 8, 74]}}}, fh)
        f2._load_knobmap()
        with open(f2._knobmap_file) as fh:
            saved = json.load(fh)
    check("migration: a shipped poly-view knob (synth ch 8) -> poly",
          f2._dialmap["poly"]["base"][0] == ("cc_out", "synth", "poly", 74))
    check("migration: old named targets -> their module (ch10 cc77 -> chip_p2, ch5 bend -> gb_wave)",
          f2._dialmap["seq"]["base"][0] == ("cc_out", "synth", "chip_p2", 77)
          and f2._dialmap["seq"]["base"][1] == ("pitch_out", "synth", "gb_wave"),
          f"{f2._dialmap['seq']['base']}")
    check("migration: a hand-set synth CC and an outboard send keep their channel",
          f2._dialmap["universal"]["base"][0] == ("cc_out", "synth", 8, 20)
          and f2._dialmap["universal"]["base"][1] == ("cc_out", "pisound", 8, 74))
    check("migration is written back (the file now names the module)",
          saved.get("poly", {}).get("base", {}).get("0") == ["cc_out", "synth", "poly", 74])

    # On-device CC OUT editor: modules offered on the synth port, committed by name.
    with sandbox_home() as home:
        f3, _ = build_fire(view="universal")
        f3._knobmap_file = os.path.join(home, "km.json")
        opts = f3._kmap_ch_options("synth")
        check("CC OUT editor offers modules before ch 1 on the synth port only",
              "poly" in opts and opts.index("poly") < opts.index(1)
              and all(isinstance(o, int) for o in f3._kmap_ch_options("pisound")))
        f3._kmap_view, f3._kmap_layer, f3._kmap_knob = "seq", "base", 2
        f3._kmap_cc = {"port": "synth", "channel": "gb_wave", "cc": 102}
        f3._kmap_cc_commit()
        got = f3._dialmap["seq"]["base"].get(2)
    check("CC OUT editor commits a module-named send",
          tuple(got or ()) == ("cc_out", "synth", "gb_wave", 102), f"{got}")


def test_knobmap_keeps_arbitrary_cc_out():
    """An arbitrary ("cc_out", port, ch, cc) must survive a restart.

    It did not before 2026-07-31: _load_knobmap accepted only actions present
    in FIRE["KNOB_TARGETS"], so a CC assigned from the /control web page was
    written to ~/fire_knobmap.json and silently dropped on the next boot. The
    web route had been recorded as working since 2026-07-13 -- and it does
    work, right up until you reboot.
    """
    section("knobmap: arbitrary cc_out survives save/load")
    custom = ("cc_out", "pisound", 3, 20)
    with sandbox_home() as home:
        fire, eng = build_fire(view="universal")
        fire._knobmap_file = os.path.join(home, "fire_knobmap.json")
        fire._dialmap["universal"]["base"][0] = custom
        fire._save_knobmap()
        fire2, _ = build_fire(view="universal")
        fire2._knobmap_file = fire._knobmap_file
        fire2._load_knobmap()
        got = fire2._dialmap["universal"]["base"].get(0)
    check("arbitrary cc_out round-trips through the knobmap file",
          tuple(got or ()) == custom, f"got {got!r}")

    # Structural validation still rejects nonsense.
    bad = [("cc_out", "pisound", 0, 20),      # channel 0
           ("cc_out", "pisound", 3, 200),     # cc out of range
           ("cc_out", "", 3, 20),             # no port
           ("cc_out", "pisound", "3", 20),    # channel not an int
           ("cc_out", "pisound", True, 20),   # bool is not a channel
           ("cc_out", "pisound", 3),          # wrong arity
           ("rm", "-rf", 1, 2)]               # not a send action at all
    rejected = [a for a in bad if not orc1_fire._well_formed_send_action(a)]
    check("malformed send actions are rejected",
          len(rejected) == len(bad),
          f"accepted: {[a for a in bad if a not in rejected]}")

    # The CC OUT menu sentinel must never reach the dial map: there is no
    # _dial_cc_custom, so a knob holding it would silently do nothing.
    with sandbox_home() as home:
        f3, _ = build_fire(view="universal")
        f3._knobmap_file = os.path.join(home, "km.json")
        with open(f3._knobmap_file, "w") as fh:
            json.dump({"universal": {"base": {"0": list(
                orc1_fire._KMAP_CC_ACTION)}}}, fh)
        before = f3._dialmap["universal"]["base"].get(0)
        f3._load_knobmap()
        after = f3._dialmap["universal"]["base"].get(0)
    check("the CC OUT sentinel is not loadable as a dial action",
          tuple(after or ()) != orc1_fire._KMAP_CC_ACTION,
          f"before={before!r} after={after!r}")

    # And it must be reachable from the menu, first, on every view.
    #
    # ⚠ EVERYTHING BELOW MUST STAY INSIDE sandbox_home(). _kmap_cc_commit()
    # calls the REAL _save_knobmap() -- HeadlessFire does not stub it -- and
    # _knobmap_file resolves "~/fire_knobmap.json" at call time. Without the
    # sandbox AND the _knobmap_file override, running this suite overwrites
    # the player's real knob map with the synthetic defaults this test built.
    # It did exactly that on 2026-07-31 before the sandbox was added: the
    # identical mistake, in the identical shape, as the _test_route_buses.py
    # incident CLAUDE.md records for 2026-07-30. See SAFETY item 6.
    with sandbox_home() as home:
        f4, _ = build_fire(view="universal")
        f4._knobmap_file = os.path.join(home, "km2.json")
        check("CC OUT is the first knob-map target in every view",
              all(tuple(_view_first_target(f4, v)) == orc1_fire._KMAP_CC_ACTION
                  for v in orc1_fire.VIEWS),
              repr({v: _view_first_target(f4, v) for v in orc1_fire.VIEWS}))

        # Committing the editor writes a real, well-formed action to the view's
        # BASELINE dialmap and persists via _save_knobmap. Uses a non-universal
        # view: universal knob assigns are per-page now (they go to
        # _universal_knobs via _save_fire_state, covered by
        # test_universal_per_page_knob_assign), so the baseline+knobmap-file
        # behaviour this check exists for lives on the ordinary views.
        f4.view = "poly"
        f4._enter_kmap()
        f4._kmap_layer, f4._kmap_knob = "shift", 2
        f4._enter_kmap_cc()
        f4._kmap_cc = {"port": "pisound", "channel": 11, "cc": 33}
        f4._kmap_cc_commit()
        got = f4._dialmap["poly"]["shift"].get(2)
        check("the CC OUT editor commits a well-formed cc_out",
              tuple(got or ()) == ("cc_out", "pisound", 11, 33), f"got {got!r}")
        check("the CC OUT editor has a real dial handler",
              hasattr(f4, "_dial_" + (got or ("",))[0]), repr(got))
        check("the CC OUT editor wrote to the sandbox, not to ~",
              os.path.exists(f4._knobmap_file), f4._knobmap_file)

        # UNIVERSAL and FADERS must not offer drum-sculpt targets: __init__
        # strips them on load, so offering them promises what the loader undoes.
        for view in ("universal", "faders"):
            f4.view = view
            f4._enter_kmap()
            offered = [t for t in f4._kmap_targets()
                       if t[1] and t[1][0] == "voice_param"]
            check(f"{view} knob menu offers no voice_param target", not offered,
                  repr(offered[:4]))
        f4.view = "seq"
        f4._enter_kmap()
        check("seq knob menu still offers voice_param targets",
              any(t[1] and t[1][0] == "voice_param" for t in f4._kmap_targets()))

        # The editor must assign to the view it was OPENED on. The mode
        # buttons are not gated on an open overlay, so a stray press mid-edit
        # used to redirect the assignment to a different view's knob.
        f4.view = "chip"
        f4._enter_kmap()                         # real path: overlay opens on chip
        f4._kmap_layer, f4._kmap_knob = "base", 1
        f4._enter_kmap_cc()
        f4._kmap_cc = {"port": "pisound", "channel": 4, "cc": 12}
        f4._set_view("seq")                      # stray mode-button press
        f4._kmap_cc_commit()
        # The PLAIN knob-map assign path has the same exposure and predates
        # the CC editor: _kmap_assign used to read self.view directly.
        f4.view = "poly"
        f4._enter_kmap()
        f4._kmap_knob, f4._kmap_layer = 3, "base"
        f4._sync_kmap_sel()
        tgts = f4._kmap_targets()
        f4._kmap_sel = next(i for i, t in enumerate(tgts) if t[1] == ("bpm",))
        f4._set_view("seq")                      # stray mode-button press
        f4._kmap_assign()
        check("plain knob-map assign lands on the view it was opened on",
              tuple(f4._dialmap["poly"]["base"].get(3) or ()) == ("bpm",),
              f"poly={f4._dialmap['poly']['base'].get(3)!r} "
              f"seq={f4._dialmap['seq']['base'].get(3)!r}")
        f4.view = "universal"

        check("CC OUT commits to the view it was opened on",
              tuple(f4._dialmap["chip"]["base"].get(1) or ()) ==
              ("cc_out", "pisound", 4, 12),
              f"chip={f4._dialmap['chip']['base'].get(1)!r} "
              f"seq={f4._dialmap['seq']['base'].get(1)!r}")

    # A knob PRESET must be validated like a knobmap: ~/fire_presets.json is
    # the same threat model (user/hand-editable) and this loader had none.
    with sandbox_home() as home:
        f5, _ = build_fire(view="universal")
        f5._knobmap_file = os.path.join(home, "km3.json")
        with open(os.path.join(home, "fire_presets.json"), "w") as fh:
            json.dump({"knobs": {"0": {"dialmap": {"universal": {"base": {
                "0": list(orc1_fire._KMAP_CC_ACTION),      # the sentinel
                "1": ["rm", "-rf"],                        # junk
                "2": ["cc_out", "pisound", 5, 21],         # legitimate
            }}}}}}, fh)
        f5._preset_slot, f5._preset_action = 0, "load_knobs"
        f5._preset_confirm()
        b = f5._dialmap["universal"]["base"]
    check("a preset cannot install the CC OUT sentinel",
          tuple(b.get(0) or ()) != orc1_fire._KMAP_CC_ACTION, repr(b.get(0)))
    check("a preset cannot install an unknown action",
          tuple(b.get(1) or ()) != ("rm", "-rf"), repr(b.get(1)))
    check("a preset still installs a valid arbitrary cc_out",
          tuple(b.get(2) or ()) == ("cc_out", "pisound", 5, 21), repr(b.get(2)))

    # The per-view rule must hold in ALL THREE writers, not just the on-device
    # menu: a drum-sculpt target on UNIVERSAL/FADERS applies live, sculpts an
    # unrelated drum voice, drags the OLED to the drum EDIT screen (
    # _dial_voice_param has no view gate), and is then stripped at the next
    # restart while /control still shows it assigned.
    check("the shared rule rejects voice_param on universal",
          not orc1_fire._target_allowed_in_view(("voice_param", "attack"),
                                                "universal"))
    check("the shared rule rejects voice_param on faders",
          not orc1_fire._target_allowed_in_view(("voice_param", "attack"),
                                                "faders"))
    check("the shared rule allows voice_param on seq",
          orc1_fire._target_allowed_in_view(("voice_param", "attack"), "seq"))
    check("the shared rule allows a cc_out on universal",
          orc1_fire._target_allowed_in_view(("cc_out", "pisound", 1, 74),
                                            "universal"))

    # ...and the PRESET path honours it end to end.
    with sandbox_home() as home:
        f7, _ = build_fire(view="universal")
        f7._knobmap_file = os.path.join(home, "km5.json")
        with open(os.path.join(home, "fire_presets.json"), "w") as fh:
            json.dump({"knobs": {"0": {"dialmap": {
                "universal": {"base": {"0": ["voice_param", "attack"],
                                       "1": ["bpm"],
                                       "2": ["cc_out", "pisound", 1, 74]}},
                "seq":       {"base": {"0": ["voice_param", "attack"]}},
            }}}}, fh)
        f7._preset_slot, f7._preset_action = 0, "load_knobs"
        f7._preset_confirm()
        uni, seq = f7._dialmap["universal"]["base"], f7._dialmap["seq"]["base"]
    check("a preset cannot put voice_param on a universal knob",
          tuple(uni.get(0) or ()) != ("voice_param", "attack"), repr(uni.get(0)))
    check("a preset cannot put an internal target (bpm) on universal now",
          tuple(uni.get(1) or ()) != ("bpm",), repr(uni.get(1)))
    check("a SEND action in the same preset still loads on universal",
          tuple(uni.get(2) or ()) == ("cc_out", "pisound", 1, 74), repr(uni.get(2)))
    check("the same preset entry is still allowed on seq",
          tuple(seq.get(0) or ()) == ("voice_param", "attack"), repr(seq.get(0)))

    # A malformed preset must degrade, not abort the load partway. A bare int
    # used to reach tuple() and raise inside the loop, so every entry after it
    # -- and the save, and the confirmation flash -- were silently skipped.
    with sandbox_home() as home:
        f6, _ = build_fire(view="chord")
        f6._knobmap_file = os.path.join(home, "km4.json")
        with open(os.path.join(home, "fire_presets.json"), "w") as fh:
            json.dump({"knobs": {"0": {"dialmap": {
                "chord": {"base": {"0": 5,                      # not a list
                                   "1": ["bpm"]},               # after the bad one
                          "shift": None},                       # null layer
                "poly":  None,                                  # null view
            }}}}, fh)
        f6._preset_slot, f6._preset_action = 0, "load_knobs"
        raised = None
        try:
            f6._preset_confirm()
        except Exception as exc:                # noqa: BLE001 - that IS the bug
            raised = exc
        got = f6._dialmap["chord"]["base"].get(1)
    check("a malformed preset entry does not raise", raised is None, repr(raised))
    check("entries after a malformed one still load",
          tuple(got or ()) == ("bpm",), repr(got))
    for f in (fire, fire2, f3, f4):
        try:
            f._restore_oled()
        except Exception:
            pass


def _view_first_target(fire, view):
    """First knob-map target for `view`, driven through the real entry path.

    _kmap_targets() reads the PINNED view, not self.view, so a test that only
    assigned self.view would be asking a question the overlay never asks.
    """
    prev_view, prev_pin = fire.view, getattr(fire, "_kmap_view", None)
    fire.view = view
    fire._enter_kmap()
    try:
        t = fire._kmap_targets()
        return t[0][1] if t else ()
    finally:
        fire.view, fire._kmap_view = prev_view, prev_pin


# ---------------------------------------------------------------------------
# 8. Two screens that were showing the wrong thing
# ---------------------------------------------------------------------------

def test_remap_encoders_does_not_hijack_the_grid():
    """The encoder-remap screen must not paint the mode-jump row.

    It used to open the SHIFT+ALT subscreen, which put eight live mode-jump
    pads on the bottom row of an unrelated screen and made every other pad
    dead -- _pad_press hands the whole grid to _subscreen_pad_press whenever a
    subscreen is up.
    """
    section("encoder remap: no leftover mode-jump row")
    for view in ("universal", "faders", "seq"):
        fire, eng = build_fire(view=view)
        fire._menuact_remap_encoders("Remap Encoders")
        check(f"remap in {view} does not open the subscreen",
              not fire._subscreen and not fire._subscreen_latched,
              f"subscreen={fire._subscreen} latched={fire._subscreen_latched}")
        check(f"remap in {view} enters the kmap overlay",
              fire._overlay == "kmap", f"overlay={fire._overlay!r}")
        # BROWSER must still be the way out.
        fire._act_browser(True)
        check(f"BROWSER exits the remap screen in {view}",
              fire._overlay is None, f"overlay={fire._overlay!r}")
        try:
            fire._restore_oled()
        except Exception:
            pass


def test_overlays_survive_the_mode_jump_subscreen():
    """The SHIFT+ALT mode-jump subscreen must not paint over an open overlay.

    `_update_subscreen` is called from _act_shift/_act_alt with no overlay
    check at all, so holding both modifiers latches the subscreen whatever is
    open -- and choosing a knob-map LAYER is itself done by holding SHIFT or
    ALT while touching a knob, so brushing both together mid-edit is a real
    gesture, not a contrived one. Only "kmap" was excluded from the four
    subscreen special-cases; the CC OUT editor added in the same session was
    not, so it would be hidden behind a screen that says something else while
    still being the open overlay.
    """
    section("overlays: the mode-jump subscreen does not hijack them")
    for ov, painter, enter in (
            ("kmap",    "_oled_kmap",    lambda f: f._enter_kmap()),
            ("kmap_cc", "_oled_kmap_cc", lambda f: (f._enter_kmap(),
                                                    f._enter_kmap_cc()))):
        fire, eng = build_fire(view="universal")
        enter(fire)
        check(f"{ov}: overlay is open", fire._overlay == ov,
              f"overlay={fire._overlay!r}")
        fire._subscreen = fire._subscreen_latched = True
        fire.oled_paints.clear()
        fire._oled()
        check(f"{ov}: its own screen still paints under the subscreen",
              painter in fire.oled_paints and
              "_oled_subscreen" not in fire.oled_paints,
              repr(fire.oled_paints))
        # A mode-jump must close the overlay, not leave it open behind a new view.
        fire._subscreen_pad_press(N_ROWS - 1, 8)
        check(f"{ov}: a mode jump closes the overlay",
              fire._overlay is None, f"overlay={fire._overlay!r}")
        try:
            fire._restore_oled()
        except Exception:
            pass


def test_select_turn_arp_scope():
    """SELECT-turn drives the arp pattern only in the views you PLAY.

    In UNIVERSAL and FADERS -- assignable MIDI control surfaces with no
    relationship to the arp -- it used to change the arpeggiator pattern out
    from under you, with nothing on screen to say it had.
    """
    section("select encoder: arp fallback scope")
    for view, should_move in (("chord", True), ("poly", True), ("chip", True),
                              ("universal", False), ("faders", False),
                              ("fx", False)):
        fire, eng = build_fire(view=view)
        # FakeArp records facade calls rather than applying them, so assert on
        # the CALL, not on pattern_name -- which the stub never mutates.
        eng.arp.calls.clear()
        fire._sel_scroll_track(+1)
        moved = any(c[0] == "set_pattern" for c in eng.arp.calls)
        check(f"SELECT-turn in {view} "
              f"{'scrolls' if should_move else 'leaves'} the arp pattern",
              moved == should_move,
              f"arp calls: {[c[0] for c in eng.arp.calls]}")
        try:
            fire._restore_oled()
        except Exception:
            pass


def test_fire_output_shares_one_io_lock():
    """The OLED frame and the LED grid are big SysEx sent to the same Fire on
    separate ports/threads; if their sends can run concurrently their USB-MIDI
    packets interleave and the OLED draws a corrupted frame (the glitch seen
    turning a knob during playback). Both send sites must take fire_oled.IO_LOCK.
    Source-level net: cheap, and it fails if either site drops the shared lock."""
    import inspect
    fo = orc1_fire.fire_oled
    if fo is None:
        check("io-lock net (skipped: no fire_oled/mido)", True)
        return
    check("fire_oled exposes IO_LOCK", hasattr(fo, "IO_LOCK"),
          "the shared serialization lock is gone")
    pads_src = inspect.getsource(orc1_fire.FireHandler._send_pads)
    check("_send_pads serializes on fire_oled.IO_LOCK",
          "IO_LOCK" in pads_src,
          "the LED-grid send no longer shares the OLED lock -> frames can tear")
    send_src = inspect.getsource(fo.send_image)
    check("send_image serializes the device write on IO_LOCK",
          "IO_LOCK" in send_src,
          "the OLED send no longer holds the shared lock -> frames can tear")


def test_oled_pushes_are_coalesced():
    """A burst of rapid show_lines() calls must NOT render one frame per call:
    render()+image_to_buf() is ~7 ms of GIL-holding work each, and rendering
    per detent starves the MIDI-clock thread so an externally-synced sequence
    drags. The coalescer renders at most ~once per _MIN_INTERVAL and always
    paints the final frame. Guards against a regression that reverts show_lines
    to a synchronous per-call render (mutation: alias show_lines = show_lines_now
    -> the render count jumps back to one-per-call and the first assert fails)."""
    fo = orc1_fire.fire_oled
    if fo is None:
        check("oled coalescer (skipped: no fire_oled/mido)", True)
        return
    import time as _time
    rendered = []
    real_render, real_send = fo.render, fo.send_image
    # Start from a clean slate so a prior test's pushes can't skew the count.
    with fo._oled_lock:
        fo._oled_pending.clear()
        fo._oled_last_t.clear()
    fo.render     = lambda draws: (rendered.append(list(draws)) or "IMG")
    fo.send_image = lambda img, port=None: None
    try:
        N = 30
        for i in range(N):
            fo.show_lines([(f"v{i}", 2, 0, 1)])
        immediate = len(rendered)
        # No sleeps between the calls, so the ~25 fps worker cannot have kept up.
        check("rapid pushes are not rendered one-per-call",
              immediate < N,
              f"rendered {immediate} frames for {N} back-to-back pushes")
        # Let the worker drain its trailing frame.
        for _ in range(60):
            _time.sleep(0.01)
            with fo._oled_lock:
                drained = not fo._oled_pending
            if drained and rendered:
                break
        total = len(rendered)
        check("a burst still renders at least the final frame", total >= 1,
              f"rendered {total} frames total")
        check("the coalesced frame is the LAST one submitted",
              bool(rendered) and rendered[-1] == [("v29", 2, 0, 1)],
              f"last rendered: {rendered[-1] if rendered else None}")
        check("the whole burst renders far fewer frames than pushes",
              total <= 8, f"rendered {total} frames for {N} pushes")
    finally:
        # Drain, then restore, so no queued frame reaches real hardware.
        with fo._oled_lock:
            fo._oled_pending.clear()
        fo.render, fo.send_image = real_render, real_send


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def test_chord_row_banks():
    """GRID <> flips row/routing banks; per-row octave is ABSOLUTE; the freed
    OCT pads are dark + inert; PAT is the sole octave control."""
    section("chord: row/routing banks + absolute octave")
    fire, eng = build_fire(state={}, view="chord")
    fire._save_chord_rows = lambda: None   # never write the player's real file

    # banks exist and _chord_rows points into them
    n = len(fire._chord_banks)
    check("more than one row bank exists", n > 1, f"n={n}")
    check("bank count == _CHORD_N_BANKS",
          n == orc1_fire._CHORD_N_BANKS, f"{n} vs {orc1_fire._CHORD_N_BANKS}")
    check("active bank defaults to 0", fire._chord_bank == 0, f"bank={fire._chord_bank}")
    check("_chord_rows IS the active bank object",
          fire._chord_rows is fire._chord_banks[fire._chord_bank], "identity")

    # GRID <> flips the bank -- from the REAL button action, not just the helper
    fire.view = "chord"
    fire._act_page(True, +1)
    check("GRID right pages to bank 1", fire._chord_bank == 1, f"bank={fire._chord_bank}")
    check("_chord_rows follows the active bank",
          fire._chord_rows is fire._chord_banks[1], "identity after page")
    fire._act_page(True, -1)
    check("GRID left pages back to bank 0", fire._chord_bank == 0, f"bank={fire._chord_bank}")
    fire._act_page(True, -1)
    check("GRID left WRAPS to the last bank", fire._chord_bank == n - 1,
          f"bank={fire._chord_bank}")
    fire._chord_bank = 0
    fire._chord_rows = fire._chord_banks[0]

    # must not page banks from another view
    fire.view = "seq"
    fire._act_page(True, +1)
    check("GRID does not page row banks outside chord view",
          fire._chord_bank == 0, f"bank={fire._chord_bank}")
    fire.view = "chord"

    # an edit is isolated to its bank and survives a round trip
    fire._chord_banks[0][0]["channel"] = 5
    fire._chord_banks[1][0]["channel"] = 12
    fire._chord_bank = 0; fire._chord_rows = fire._chord_banks[0]
    fire._act_page(True, +1)                       # -> bank 1
    check("bank 1 has its own edit", fire._chord_rows[0]["channel"] == 12,
          fire._chord_rows[0])
    fire._act_page(True, -1)                       # -> bank 0
    check("bank 0 kept its own edit", fire._chord_rows[0]["channel"] == 5,
          fire._chord_rows[0])

    # The REAL default the handler boots with (config CHORD_ROW_MAP wins over
    # orc1_fire._DEFAULT_CHORD_ROWS via fcfg.get) must ALREADY be the absolute
    # stack -- an octave-0 config default silently collapses every row onto the
    # base octave, which asserting against _DEFAULT_CHORD_ROWS alone would miss.
    base = fire.perform_base_note
    fresh, _ = build_fire(state={}, view="chord")
    fb = fresh._chord_banks[fresh._chord_bank]
    check("boot default: R0 an octave above R1",
          int(fb[0].get("octave") or 0) == int(fb[1].get("octave") or 0) + 1, fb[0])
    check("boot default reproduces the C5/C4/C3/C2 stack",
          [fresh._row_pitch(r, 4) for r in range(4)]
          == [base + 36, base + 24, base + 12, base],
          [fresh._row_pitch(r, 4) for r in range(4)])

    # ABSOLUTE octave: default stack reproduces the old C5/C4/C3/C2 layout
    fire._chord_rows = [dict(r) for r in orc1_fire._DEFAULT_CHORD_ROWS]
    fire._chord_banks[0] = fire._chord_rows
    check("R0 col4 = base + 36 (top octave)",
          fire._row_pitch(0, 4) == base + 36, fire._row_pitch(0, 4))
    check("R3 col4 = base (bottom octave)",
          fire._row_pitch(3, 4) == base, fire._row_pitch(3, 4))
    # setting one row's octave moves ONLY that row (absolute, not a shared shift)
    fire._chord_rows[2]["octave"] = 4
    check("changing R2 octave moves only R2",
          fire._row_pitch(2, 4) == base + 48 and fire._row_pitch(3, 4) == base,
          (fire._row_pitch(2, 4), fire._row_pitch(3, 4)))

    # freed OCT pads: dark + inert
    r2 = orc1_fire._PERF_CTRL_LAYOUT[2]
    check("R2 col2 pad freed (kind none)", r2[2][0] == "none", r2[2])
    check("R2 col3 pad freed (kind none)", r2[3][0] == "none", r2[3])
    check("freed pad renders dark",
          orc1_fire._perf_ctrl_color("none", None, "MAJ", set(), False, False) == (0, 0, 0),
          "colour")
    pb = fire.perform_base_note
    fire._perform_ctrl_press(2, 2, 100)
    fire._perform_ctrl_press(2, 3, 100)
    check("pressing a freed pad does not shift the octave",
          fire.perform_base_note == pb, fire.perform_base_note)

    # PAT up/down is THE octave control (up = higher pitch)
    pb = fire.perform_base_note
    fire._shift = False
    fire._act_scroll_tracks(True, -1)
    check("PAT up raises perform_base_note one octave",
          fire.perform_base_note == pb + 12, fire.perform_base_note)
    fire._act_scroll_tracks(True, +1)
    check("PAT down restores it", fire.perform_base_note == pb, fire.perform_base_note)


def test_chord_row_bank_migration():
    """A pre-banks ~/.orc1_chord_rows.json (bare list of 4) migrates its RELATIVE
    octave to ABSOLUTE, seats as bank 0, and preserves the played pitch."""
    section("chord: pre-banks file migration")
    old_rows = [
        {"type": "direct", "port": "synth", "channel": 8,  "octave": 0},
        {"type": "direct", "port": "synth", "channel": 9,  "octave": 0},
        {"type": "direct", "port": "synth", "channel": 7,  "octave": 0},
        {"type": "chord",  "port": None,    "channel": None, "octave": 0},
    ]
    import io as _io
    with sandbox_home() as d:
        with open(os.path.join(d, ".orc1_chord_rows.json"), "w") as f:
            json.dump(old_rows, f)
        eng = FakeEng()
        try:
            import orc1_seq
            eng.seq = orc1_seq.Sequencer(eng)
        except Exception:
            eng.seq = None
        with contextlib.redirect_stdout(_io.StringIO()):
            fire = HeadlessFire(eng)
            _install_recorders(fire)
        b0 = fire._chord_banks[0]
        check("R0 octave migrated 0 -> +3", b0[0]["octave"] == 3, b0[0])
        check("R1 octave migrated 0 -> +2", b0[1]["octave"] == 2, b0[1])
        check("R2 octave migrated 0 -> +1", b0[2]["octave"] == 1, b0[2])
        check("R3 octave migrated 0 -> 0",  b0[3]["octave"] == 0, b0[3])
        base = fire.perform_base_note
        ok = all(fire._row_pitch(r, 4) == base + (3 - r) * 12 for r in range(4))
        check("played pitch preserved vs the old (3-row) formula", ok,
              [fire._row_pitch(r, 4) for r in range(4)])
        check("all bank slots present after migration",
              len(fire._chord_banks) == orc1_fire._CHORD_N_BANKS,
              len(fire._chord_banks))


def test_chord_rows_name_modules():
    """Direct chord rows on the synth NAME their module (2026-09-24).

    The shipped rows were literal synth ch 8 / 9 / 7. After the poly moved on
    /matrix, row 0 played chip P2, and row 2 (ch 7 = the old mono alias) hit
    the SAMPLER's channel. Saved rows holding exactly those defaults migrate;
    a row aimed at a channel by hand keeps its number."""
    section("chord rows: synth rows name a module, resolved live")
    import io as _io
    rows = [
        {"type": "direct", "port": "synth", "channel": 8,  "octave": 3},
        {"type": "direct", "port": "synth", "channel": 9,  "octave": 2},
        {"type": "direct", "port": "synth", "channel": 7,  "octave": 1},
        {"type": "direct", "port": "synth", "channel": 12, "octave": 0},
    ]
    with sandbox_home() as d:
        p = os.path.join(d, ".orc1_chord_rows.json")
        with open(p, "w") as f:
            json.dump({"v": 2, "active": 0, "banks": [rows]}, f)
        eng = FakeEng()
        import orc1_seq
        eng.seq = orc1_seq.Sequencer(eng)
        with contextlib.redirect_stdout(_io.StringIO()):
            fire = HeadlessFire(eng)
            _install_recorders(fire)
        b0 = fire._chord_banks[0]
        with open(p) as f:
            saved = json.load(f)
    check("shipped rows 0-2 migrate to poly / chip_p1 / mono",
          [r["channel"] for r in b0[:3]] == ["poly", "chip_p1", "mono"],
          [r["channel"] for r in b0[:4]])
    check("a hand-set synth row (ch 12) keeps its channel", b0[3]["channel"] == 12)
    check("the migration is written back to the file",
          saved["banks"][0][0]["channel"] == "poly", saved["banks"][0][0])

    eng._bindings = {"poly": 6, "chip_p1": 9, "mono": 3}
    fire._chord_row_top = 0
    got = []
    eng.note_on_out = lambda n, v, port, ch: got.append((port, ch))
    fire._perform_key(0, 4, 999, 100)
    check("row 0 plays the poly on its LIVE channel (6)", got == [("synth", 6)], got)
    eng._bindings["poly"] = 2
    got.clear(); fire._dir_held.clear()
    fire._perform_key(0, 4, 998, 100)
    check("...and follows a /matrix move (2)", got == [("synth", 2)], got)
    del eng._bindings["poly"]
    got.clear(); fire._dir_held.clear()
    fire._perform_key(0, 4, 997, 100)
    check("an UNBOUND module's row stays silent", got == [], got)

    vr = orc1_fire._valid_chord_row
    check("web row validator: module on synth ok; module elsewhere / bad channel refused",
          vr({"type": "direct", "port": "synth", "channel": "poly", "octave": 1})
          and not vr({"type": "direct", "port": "pisound", "channel": "poly", "octave": 0})
          and not vr({"type": "direct", "port": "synth", "channel": 0, "octave": 0})
          and vr({"type": "chord", "port": None, "channel": None, "octave": 0}))

    with sandbox_home():
        fire._chord_row_sel = 3              # the ch-12 synth row
        fire._chord_rows[3] = {"type": "direct", "port": "synth", "channel": 1, "octave": 0}
        fire._chord_rows_dial(3, -1)          # from ch 1 back into the modules
        check("row channel knob steps into the modules on the synth port",
              isinstance(fire._chord_rows[3]["channel"], str), fire._chord_rows[3])
        fire._chord_rows_dial(2, 1)           # port away from synth
        check("...and a module falls back to ch 1 off the synth port",
              fire._chord_rows[3]["channel"] == 1, fire._chord_rows[3])


def test_module_migration_runs_once():
    """The literal-channel -> module migration is ONE-time (2026-10-01).

    It had no version marker, so it re-ran on every boot: a synth ch 8 knob CC
    or a ch 8 / ch 7 chord row the player set ON PURPOSE afterwards turned back
    into "poly" / "mono" at the next restart. Now a marked file loads as-is and
    an unmarked one is migrated once and re-saved with the marker."""
    section("migration: knob map + chord rows migrate ONCE (version marker)")
    import io as _io
    lit = ["cc_out", "synth", 8, 74]
    with sandbox_home() as home:
        f, _ = build_fire(view="poly")
        f._knobmap_file = os.path.join(home, "km.json")
        with open(f._knobmap_file, "w") as fh:
            json.dump({"_version": orc1_fire._KNOBMAP_VERSION,
                       "poly": {"base": {"0": lit}}}, fh)
        before = os.stat(f._knobmap_file).st_mtime_ns
        f._load_knobmap()
        check("a MARKED knob map keeps a deliberate synth ch 8 CC as a channel",
              f._dialmap["poly"]["base"][0] == tuple(lit), f"{f._dialmap['poly']['base'][0]}")
        check("...and loading it does not rewrite the file",
              os.stat(f._knobmap_file).st_mtime_ns == before)

        # An unmarked file that already names modules (= the live rig's files,
        # migrated 2026-09-24): every entry kept, marker added.
        mods = {"poly": {"base": {"0": ["cc_out", "synth", "poly", 74],
                                  "1": ["cc_out", "synth", "poly", 71]}},
                "chip": {"alt": {"0": ["pitch_out", "synth", "chip_p1"]}}}
        f2, _ = build_fire(view="poly")
        f2._knobmap_file = os.path.join(home, "km2.json")
        with open(f2._knobmap_file, "w") as fh:
            json.dump(mods, fh)
        f2._load_knobmap()
        with open(f2._knobmap_file) as fh:
            saved = json.load(fh)
        check("an unmarked, already-migrated knob map gets the marker on load",
              saved.get("_version") == orc1_fire._KNOBMAP_VERSION, saved.get("_version"))
        check("...with every saved entry unchanged",
              all(saved[v][l][k] == a for v, ls in mods.items()
                  for l, ks in ls.items() for k, a in ks.items()))
        f3, _ = build_fire(view="poly")
        f3._knobmap_file = f2._knobmap_file
        f3._dialmap["poly"]["base"][0] = tuple(lit)     # player re-aims at ch 8
        f3._save_knobmap()
        f4, _ = build_fire(view="poly")
        f4._knobmap_file = f2._knobmap_file
        f4._load_knobmap()
        check("a ch 8 CC saved AFTER the migration survives the next boot",
              f4._dialmap["poly"]["base"][0] == tuple(lit), f"{f4._dialmap['poly']['base'][0]}")

        # Chord rows: v3 = migrated; a deliberate synth ch 8 / 7 row stays.
        p = os.path.join(home, ".orc1_chord_rows.json")
        rows = [{"type": "direct", "port": "synth", "channel": 8, "octave": 3},
                {"type": "direct", "port": "synth", "channel": 9, "octave": 2},
                {"type": "direct", "port": "synth", "channel": 7, "octave": 1}]
        with open(p, "w") as fh:
            json.dump({"v": orc1_fire._CHORD_ROWS_VERSION, "active": 0, "banks": [rows]}, fh)
        before = os.stat(p).st_mtime_ns
        eng = FakeEng()
        import orc1_seq
        eng.seq = orc1_seq.Sequencer(eng)
        with contextlib.redirect_stdout(_io.StringIO()):
            fr = HeadlessFire(eng)
        check("a MARKED (v3) chord-row file keeps synth ch 8 / 9 / 7 rows as channels",
              [r["channel"] for r in fr._chord_banks[0][:3]] == [8, 9, 7],
              [r["channel"] for r in fr._chord_banks[0][:3]])
        check("...and is not rewritten on load", os.stat(p).st_mtime_ns == before)
        with open(p, "w") as fh:
            json.dump({"v": 2, "active": 0, "banks": [rows]}, fh)
        with contextlib.redirect_stdout(_io.StringIO()):
            fr2 = HeadlessFire(eng)
        with open(p) as fh:
            sv = json.load(fh)
        check("an unmarked (v2) file still migrates once and is saved as v3",
              [r["channel"] for r in fr2._chord_banks[0][:3]] == ["poly", "chip_p1", "mono"]
              and sv.get("v") == orc1_fire._CHORD_ROWS_VERSION, sv.get("v"))


def test_knobmap_set_without_a_fire():
    """/control knob edits with the Fire UNPLUGGED are saved (2026-10-01).
    The brain's KNOBMAP_SET needed a Fire and silently dropped them."""
    section("knobmap: saved with no Fire attached")
    w = orc1_fire.knobmap_set_offline
    with sandbox_home() as home:
        p = os.path.join(home, "km.json")
        w(p, "poly", "shift", 2, ("cc_out", "synth", "poly", 9))
        with open(p) as fh:
            d = json.load(fh)
        check("a missing file is created with the entry + the marker",
              d.get("poly", {}).get("shift", {}).get("2") == ["cc_out", "synth", "poly", 9]
              and d.get("_version") == orc1_fire._KNOBMAP_VERSION, d)
        with open(p, "w") as fh:
            json.dump({"poly": {"base": {"0": ["cc_out", "synth", 8, 74]}}}, fh)
        w(p, "seq", "base", 1, ("cc_out", "pisound", 3, 20))
        with open(p) as fh:
            d = json.load(fh)
        check("an unmarked file is migrated before the marker is written",
              d["poly"]["base"]["0"] == ["cc_out", "synth", "poly", 74]
              and d["seq"]["base"]["1"] == ["cc_out", "pisound", 3, 20], d)
        w(p, "poly", "base", 0, ("cc_out", "synth", 8, 74))
        with open(p) as fh:
            d = json.load(fh)
        check("...and a later ch 8 edit on the marked file stays ch 8",
              d["poly"]["base"]["0"] == ["cc_out", "synth", 8, 74], d)
        bad = []
        for args in (("nosuchview", "base", 0), ("poly", "nolayer", 0), ("poly", "base", 7)):
            try:
                w(p, *args, ("none",))
                bad.append(args)
            except ValueError:
                pass
        check("a bad view / layer / knob is refused (raises, not written)", not bad, bad)
        f, _ = build_fire(view="poly")
        f._knobmap_file = p
        f._load_knobmap()
        check("the Fire loads the offline edits on plug-in (marked: ch 8 stays ch 8)",
              f._dialmap["poly"]["base"][0] == ("cc_out", "synth", 8, 74)
              and f._dialmap["seq"]["base"][1] == ("cc_out", "pisound", 3, 20),
              f"{f._dialmap['poly']['base'][0]} {f._dialmap['seq']['base'].get(1)}")


def test_unbound_keyboard_is_silent():
    """An UNBOUND module's Fire keyboard / voice CCs send NOTHING (2026-10-01).
    _live_ch fell back to the shipped cfg channel, so with the poly taken out
    of the fold the POLY keyboard played whatever sat on ch 8."""
    section("keyboards: unbound module = silent, never a stale channel")
    fire, eng = build_fire(view="poly")

    def synth_msgs():
        out = []
        for p, m in eng.sent:
            if p != "synth":
                continue
            m = m[0] if isinstance(m, tuple) and m else m
            if hasattr(m, "type"):
                out.append(m)
        return out
    base = getattr(orc1_fire, "PAD_NOTE_BASE", 54)
    eng._bindings = {"chip_p1": 9}                # poly + chip_p2 out of the fold
    check("an unbound module's channel property is None (no cfg fallback)",
          fire._ch_poly is None and fire._poly_ch_now is None and fire._ch_chip == 9)
    eng.sent.clear()
    fire.view = "poly"
    fire.handle_msg(_msg("note_on", note=base + 5, velocity=100))
    fire.handle_msg(_msg("note_off", note=base + 5, velocity=0))
    fire._select_poly_voice("pulse1")
    fire._poly_note(60, 100, True)
    check("POLY keyboard + voice CC1 send nothing while poly is unbound",
          synth_msgs() == [], [str(m) for m in synth_msgs()])
    fire.view = "chip"
    fire._select_chip_voice("pulse2")
    eng.sent.clear()
    fire.handle_msg(_msg("note_on", note=base + 5, velocity=100))
    fire.handle_msg(_msg("note_off", note=base + 5, velocity=0))
    check("CHIP keyboard on an unbound voice (P2) sends nothing",
          not [m for m in synth_msgs() if m.type in ("note_on", "note_off")],
          [str(m) for m in synth_msgs()])
    fire._select_chip_voice("pulse1")
    eng.sent.clear()
    fire.handle_msg(_msg("note_on", note=base + 5, velocity=100))
    fire.handle_msg(_msg("note_off", note=base + 5, velocity=0))
    notes = [(m.type, m.channel + 1) for m in synth_msgs() if m.type in ("note_on", "note_off")]
    check("...while a BOUND voice (P1 on 9) still plays + releases on its channel",
          notes == [("note_on", 9), ("note_off", 9)], notes)
    eng.sent.clear()
    fire._panic()
    pch = sorted({m.channel + 1 for m in synth_msgs()
                  if m.type == "control_change" and m.control == 123})
    check("PANIC hits only the BOUND modules' live channels", pch == [9], pch)

    # forget_synth_channel: the brain calls it after releasing a moved module.
    eng._bindings = {"poly": 6, "chip_p1": 9}
    fire.view = "poly"
    fire.handle_msg(_msg("note_on", note=base + 7, velocity=100))
    fire._dir_held[999] = ("synth", 6, 60)
    fire._dir_held[998] = ("pisound", 6, 60)
    fire.forget_synth_channel(6)
    eng.sent.clear()
    fire.handle_msg(_msg("note_off", note=base + 7, velocity=0))
    check("forget_synth_channel drops held synth notes on that channel only",
          not fire._poly_held and 999 not in fire._dir_held and 998 in fire._dir_held
          and not [m for m in synth_msgs() if m.type == "note_off"],
          f"{fire._poly_held} {fire._dir_held}")


def test_chord_tall_row_stack():
    """A bank is a tall stack (>4 rows); the Fire shows a 4-row window that
    SELECT-turn scrolls. Extra rows default OFF (a fresh bank sounds like the old
    4-row one). Physical rows map through the viewport to logical rows; SHIFT+
    SELECT keeps the arp-pattern scroll; a bank switch resets the window."""
    section("chord: tall row stack + SELECT viewport")
    fire, eng = build_fire(state={}, view="chord")
    fire._save_chord_rows = lambda: None   # never write the player's real file
    with sandbox_home():          # belt-and-suspenders isolation
        rows = fire._chord_rows
        check("a bank holds more than 4 rows",
              len(rows) == orc1_fire._CHORD_ROWS_PER_BANK, f"n={len(rows)}")
        check("rows past the first four default to off",
              all(r.get("type") == "off" for r in rows[4:]), rows[4:5])
        check("viewport starts at the top", fire._chord_row_top == 0)
        check("physical row 0 == logical row 0 at top",
              fire._chord_cfg(0) is rows[0], "identity")

        # a recognisable config on logical row 4, then scroll the window to it
        rows[4] = {"type": "direct", "port": "rtp", "channel": 12, "octave": -1}
        fire.view = "chord"; fire._shift = False; fire._alt = False
        fire._dispatch_select(+4)      # the REAL dispatch -> viewport scroll
        check("SELECT-turn scrolled the viewport", fire._chord_row_top == 4,
              f"top={fire._chord_row_top}")
        check("physical row 0 now shows logical row 4",
              fire._chord_cfg(0).get("channel") == 12
              and fire._chord_cfg(0).get("port") == "rtp", fire._chord_cfg(0))
        base = fire.perform_base_note
        check("row pitch follows the scrolled row's absolute octave",
              fire._row_pitch(0, 4) == base + (-1) * 12, fire._row_pitch(0, 4))

        # clamp at both ends (scroll, not wrap)
        fire._dispatch_select(+99)
        maxtop = orc1_fire._CHORD_ROWS_PER_BANK - orc1_fire.N_ROWS
        check("viewport clamps at the last full window",
              fire._chord_row_top == maxtop, f"top={fire._chord_row_top}")
        fire._dispatch_select(-99)
        check("viewport clamps at the top", fire._chord_row_top == 0,
              f"top={fire._chord_row_top}")

        # SHIFT+SELECT must NOT move the viewport (it drives the arp pattern)
        fire._chord_row_top = 2
        fire._shift = True
        fire._dispatch_select(+1)
        check("SHIFT+SELECT leaves the viewport alone",
              fire._chord_row_top == 2, f"top={fire._chord_row_top}")
        fire._shift = False

        # a fresh bank resets the window to the top
        fire._chord_row_top = 3
        fire._cycle_chord_bank(+1)
        check("switching banks resets the viewport to the top",
              fire._chord_row_top == 0, f"top={fire._chord_row_top}")

        # Configure-Rows selection scrolls the WHOLE stack, not just 4
        fire._chord_row_sel = 0
        for _ in range(orc1_fire._CHORD_ROWS_PER_BANK - 1):
            fire._chord_rows_dial(0, +1)
        check("Configure-Rows selection reaches the last row",
              fire._chord_row_sel == orc1_fire._CHORD_ROWS_PER_BANK - 1,
              f"sel={fire._chord_row_sel}")


def test_universal_per_page_knob_assign():
    """On-device knob assign in universal writes the CURRENT PAGE's per-page
    override, not the shared baseline -- so each page gets its own knobs from the
    device (per-page was web-only before). Pins the page at entry so a GRID flip
    mid-edit can't retarget, and non-universal views still write the baseline."""
    section("universal: per-page on-device knob assign")
    fire, eng = build_fire(state={}, view="universal")
    n = fire._universal_nbanks
    check("universal has multiple pages", n > 1, f"n={n}")

    with sandbox_home():   # the non-universal commit hits the real _save_knobmap
        # enter via the real gesture path; it pins the current page
        fire.view = "universal"
        fire._universal_bank = 0
        fire._enter_kmap()
        check("kmap pins the current universal page", fire._kmap_bank == 0,
              f"bank={fire._kmap_bank}")

        # commit a CC to page 0 knob 0 via the CC-editor commit path
        fire._kmap_knob, fire._kmap_layer = 0, "base"
        fire._kmap_cc = {"port": "pisound", "channel": 3, "cc": 20}
        fire._kmap_cc_commit()
        ov0 = fire._universal_knobs.get(0, {}).get("base", {}).get(0)
        check("page 0 knob got the per-page override",
              ov0 == ("cc_out", "pisound", 3, 20), ov0)
        check("the shared baseline dialmap was NOT written",
              fire._dialmap.get("universal", {}).get("base", {}).get(0)
              != ("cc_out", "pisound", 3, 20),
              fire._dialmap.get("universal", {}).get("base", {}).get(0))
        check("effective knob action reflects the page override",
              fire._universal_knob_action("base", 0) == ("cc_out", "pisound", 3, 20),
              fire._universal_knob_action("base", 0))

        # a DIFFERENT page gets its own knob; page 0 stays put
        fire._universal_bank = 1
        fire._enter_kmap()
        fire._kmap_knob, fire._kmap_layer = 0, "base"
        fire._kmap_cc = {"port": "pisound", "channel": 5, "cc": 21}
        fire._kmap_cc_commit()
        check("page 1 has its own knob",
              fire._universal_knobs.get(1, {}).get("base", {}).get(0)
              == ("cc_out", "pisound", 5, 21),
              fire._universal_knobs.get(1, {}).get("base", {}).get(0))
        check("page 0 knob is unchanged (isolated per page)",
              fire._universal_knobs.get(0, {}).get("base", {}).get(0)
              == ("cc_out", "pisound", 3, 20),
              fire._universal_knobs.get(0, {}).get("base", {}).get(0))

        # pinning: a page flip mid-edit must not retarget the commit
        fire._universal_bank = 0
        fire._enter_kmap()                 # pins page 0
        fire._universal_bank = 1           # user pages away before committing
        fire._kmap_knob, fire._kmap_layer = 1, "base"
        fire._kmap_cc = {"port": "pisound", "channel": 7, "cc": 22}
        fire._kmap_cc_commit()
        check("commit lands on the PINNED page, not the flipped-to one",
              fire._universal_knobs.get(0, {}).get("base", {}).get(1)
              == ("cc_out", "pisound", 7, 22)
              and fire._universal_knobs.get(1, {}).get("base", {}).get(1) is None,
              (fire._universal_knobs.get(0, {}).get("base", {}).get(1),
               fire._universal_knobs.get(1, {}).get("base", {}).get(1)))

        # regression: a NON-universal view still writes the baseline dialmap
        fire.view = "poly"
        fire._enter_kmap()
        fire._kmap_knob, fire._kmap_layer = 2, "base"
        fire._kmap_commit_action(("cc_out", "synth", 8, 74))
        check("non-universal assign still writes the baseline dialmap",
              fire._dialmap["poly"]["base"][2] == ("cc_out", "synth", 8, 74),
              fire._dialmap["poly"]["base"].get(2))


def test_pretty_lights():
    """Pretty lights: mode-table integrity, every renderer emits a full valid
    grid, FLAME lights its bottom row (the 2026-09-05 regression it was renamed
    from FIRE to fix), and the live master-speed knob rides + clamps."""
    import math as _math
    fire, eng = build_fire()

    NAMES  = orc1_fire.FireHandler._PL_MODE_NAMES
    NMODES = orc1_fire.FireHandler._PL_N_MODES
    SCALES = orc1_fire.FireHandler._PL_SPEED_SCALE
    check("pretty lights: name/count/scale tables are index-aligned (12)",
          NMODES == len(NAMES) == len(SCALES) == 12,
          f"names={len(NAMES)} n={NMODES} scales={len(SCALES)}")
    check("pretty lights: FIRE was renamed to FLAME in the name table",
          "FLAME" in NAMES and "FIRE" not in NAMES, NAMES)

    cap = []
    fire._send_pads = lambda cells: (cap.clear(), cap.extend(cells))
    times = [i * 0.37 for i in range(24)]

    # every renderer, sampled across time, emits a full well-formed grid
    order = ["plasma", "sparkle", "sweep", "flame", "beat", "sparse",
             "rain", "ripple", "comet", "scanner", "wave", "vu"]
    for nm, key in zip(NAMES, order):
        fn = getattr(fire, "_render_pl_" + key)
        ok_all = True
        for t in times:
            cap.clear(); fn(t, _math)
            if len(cap) != 64 or sorted(c[0] for c in cap) != list(range(64)):
                ok_all = False; break
            if any(not (0 <= ch <= 0x7F) for c in cap for ch in c[1:]):
                ok_all = False; break
        check(f"pretty lights: {nm} emits a full 64-pad grid in range", ok_all)

    # dispatch reaches every mode without crashing
    fire._pl_start, fire._pl_speed = 0.0, 1.0
    disp_ok = True
    for m in range(NMODES):
        fire._pl_mode = m; cap.clear()
        try:
            fire._render_pretty_lights()
        except Exception:
            disp_ok = False; break
        if len(cap) != 64:
            disp_ok = False; break
    check("pretty lights: dispatch renders all 12 modes", disp_ok)

    # FLAME regression, both endpoints (the grid is only 4 rows tall):
    #   - bottom row (pads 48-63) is a SOLID base: lit every frame. The original
    #     inverted heat_base made it always black.
    #   - top row (pads 0-15) gets intermittent flame licks: lit in some frames,
    #     dark in others. The first fix left it ALWAYS dark (heat_base 0 at row 0).
    bottom, top = set(range(48, 64)), set(range(0, 16))
    bottom_lit_always = True
    top_lit_frames = 0
    for t in times:
        cap.clear(); fire._render_pl_flame(t, _math)
        lit = {c[0] for c in cap if (c[1] + c[2] + c[3]) > 0}
        if not (bottom <= lit):
            bottom_lit_always = False
        if top & lit:
            top_lit_frames += 1
    check("FLAME lights its whole bottom row every frame (solid base)",
          bottom_lit_always)
    check("FLAME's top row flickers with licks (not blank, not solid)",
          top_lit_frames >= len(times) // 3, f"top lit in {top_lit_frames}/{len(times)}")

    # live master-speed knob: rides on knob 0, clamps, swallows the turn so it
    # never falls through to the normal view dispatch.
    fire._pretty_lights = True
    fire._pl_speed = 1.0
    fire._dispatch_dial(0, 3)
    up = fire._pl_speed
    fire._dispatch_dial(0, -3)
    check("master-speed knob 0 increases then decreases _pl_speed",
          up > 1.0 and fire._pl_speed < up, f"up={up} now={fire._pl_speed}")
    fire._pl_speed = 1.0
    for _ in range(300):
        fire._dispatch_dial(0, 20)
    hi = fire._pl_speed
    for _ in range(600):
        fire._dispatch_dial(0, -20)
    lo = fire._pl_speed
    check("master-speed clamps to [0.1, 4.0]",
          hi == 4.0 and lo == 0.1, f"hi={hi} lo={lo}")
    fire._pretty_lights = False


def main():
    print("ORC-1 Fire handler test suite")
    print(f"  orc1_fire.py : {os.path.join(HERE, 'orc1_fire.py')}")
    print(f"  mido         : {'yes' if mido else 'NO (sweeps will skip)'}")
    print(f"  fire_oled    : {'yes' if orc1_fire.fire_oled else 'no'}")

    # Whole-run net (2026-09-24): the handler's own disk files in the REAL home
    # must be byte-identical after the run. The chord-row banks were silently
    # overwritten by every Pi run of this suite until then (SAFETY item 8).
    import hashlib
    real_home = os.path.expanduser("~")
    guarded = [os.path.join(real_home, n)
               for n in ("fire_knobmap.json", ".orc1_chord_rows.json")]

    def _snap():
        out = {}
        for p in guarded:
            try:
                with open(p, "rb") as fh:
                    out[p] = hashlib.md5(fh.read()).hexdigest()
            except FileNotFoundError:
                out[p] = None
        return out
    before = _snap()

    for t in (test_dispatch_names,
              test_oled_truncations_are_all_deliberate,
              test_cc_editor_shows_and_changes_the_cc,
              test_no_import_time_home_paths,
              test_oled_lines_do_not_overlap,
              test_no_dead_act_methods,
              test_key_overlay_pin_cannot_trap_the_keyboard,
              test_menu_and_overlay_block_pad_input,
              test_scale_mode_cycle_order,
              test_construction,
              test_state_restore_without_saved_faders,
              test_saved_state_is_not_clobbered_by_init,
              test_oled_painters,
              test_oled_assign_screens_fit_worst_case,
              test_universal_banks,
              test_universal_page_knobs,
              test_universal_fader_element,
              test_chord_row_banks,
              test_chord_row_bank_migration,
              test_chord_rows_name_modules,
              test_module_migration_runs_once,
              test_knobmap_set_without_a_fire,
              test_unbound_keyboard_is_silent,
              test_chord_tall_row_stack,
              test_universal_per_page_knob_assign,
              test_knobmap_keeps_arbitrary_cc_out,
              test_knobs_name_modules,
              test_remap_encoders_does_not_hijack_the_grid,
              test_overlays_survive_the_mode_jump_subscreen,
              test_select_turn_arp_scope,
              test_fire_output_shares_one_io_lock,
              test_oled_pushes_are_coalesced,
              test_unresolved_action_is_loud,
              test_swallowed_hotpath_error_is_deduped,
              test_pretty_lights,
              test_button_sweep,
              test_encoder_sweep,
              test_pad_sweep):
        try:
            t()
        except Exception:
            check(f"{t.__name__} itself crashed", False,
                  traceback.format_exc(limit=8))

    after = _snap()
    changed = [p for p in guarded if before[p] != after[p]]
    check("the REAL knob map + chord-row banks were not touched by the run",
          not changed, f"modified: {changed}")

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    print(f"\n{'='*58}")
    print(f"  {passed} passed, {failed} failed, {len(_RESULTS)} checks")
    print(f"{'='*58}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
