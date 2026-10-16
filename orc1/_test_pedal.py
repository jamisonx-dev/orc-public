#!/usr/bin/env python3
"""Regression suite for orc1_pedal (foot-pedal input -> BeatBuddy mind).

Pure stdlib, self-contained: a tiny fake mido message + captured callbacks. Proves
name recognition, the momentary 'primary' switch timing layer (PRESS / held->
LONG_PRESS via poll / RELEASE / rapid-2nd-tap -> MULTI_TAP), discrete PC/CC/note
gestures incl. the transition toggle, the clock/transport ignore, and the feedback
runaway guard. It also drives the REAL BeatBuddyMind through the pedal to prove the
emitted events actually work the grammar end to end.

Run:  python3 _test_pedal.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_pedal as P
import orc1_beatbuddy as bb

EXPECTED = 117
_p = _f = 0


def check(name, cond):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ! FAIL: {name}")


class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


def make(mapping=None):
    ev = []      # button events (name, t)
    ac = []      # discrete actions
    pedal = P.PedalInput(button_cb=lambda e, t: ev.append((e, t)),
                         action_cb=lambda g: ac.append(g),
                         mapping=mapping, log_fn=lambda *a: None)
    return pedal, ev, ac


def test_recognised():
    check("recognise CH345", P.recognised("CH345:CH345 MIDI 1 28:0"))
    check("recognise FCB", P.recognised("FCB1010 MIDI 1"))
    check("reject pisound", not P.recognised("pisound MIDI PS-2C6MZ6V"))
    check("reject empty", not P.recognised(""))


def test_primary_tap():
    # a switch mapped 'primary' on note 0: quick down+up = PRESS then RELEASE
    m, ev, ac = make({"note": {0: "primary"}})
    m.handle(Msg("note_on", note=0, velocity=100), now_ms=1000)
    m.handle(Msg("note_off", note=0, velocity=0), now_ms=1100)
    check("primary quick tap -> PRESS then RELEASE",
          [e[0] for e in ev] == [P.PedalInput.PRESS, P.PedalInput.RELEASE])
    check("no discrete actions from primary", ac == [])


def test_primary_long_press_via_poll():
    m, ev, ac = make({"note": {0: "primary"}})
    m.handle(Msg("note_on", note=0, velocity=100), now_ms=2000)
    m.poll(now_ms=2200)   # 200ms < LONG_MS -> nothing yet
    check("poll before threshold: no LONG", P.PedalInput.LONG not in [e[0] for e in ev])
    m.poll(now_ms=2600)   # 600ms >= 500 -> LONG_PRESS fires once
    m.poll(now_ms=2900)   # still held -> must NOT re-fire
    longs = [e[0] for e in ev].count(P.PedalInput.LONG)
    check("held past threshold -> exactly one LONG_PRESS", longs == 1)
    m.handle(Msg("note_off", note=0, velocity=0), now_ms=3000)
    check("release after hold -> RELEASE last", ev[-1][0] == P.PedalInput.RELEASE)


def test_primary_double_tap():
    m, ev, ac = make({"note": {0: "primary"}})
    m.handle(Msg("note_on", note=0, velocity=100), now_ms=5000)
    m.handle(Msg("note_off", note=0, velocity=0), now_ms=5080)
    m.handle(Msg("note_on", note=0, velocity=100), now_ms=5200)   # within 350ms of up
    names = [e[0] for e in ev]
    check("2nd rapid press -> MULTI_TAP (not PRESS)",
          names == [P.PedalInput.PRESS, P.PedalInput.RELEASE, P.PedalInput.MULTI])
    # a slow 2nd press is a fresh PRESS, not MULTI
    m2, ev2, _ = make({"note": {0: "primary"}})
    m2.handle(Msg("note_on", note=0, velocity=100), now_ms=6000)
    m2.handle(Msg("note_off", note=0, velocity=0), now_ms=6080)
    m2.handle(Msg("note_on", note=0, velocity=100), now_ms=6600)  # 520ms later
    check("slow 2nd press -> PRESS again", ev2[-1][0] == P.PedalInput.PRESS)


def test_discrete_pc():
    m, ev, ac = make()   # default map
    m.handle(Msg("program_change", program=0), now_ms=100)   # -> tap
    m.handle(Msg("program_change", program=2), now_ms=200)   # -> double
    m.handle(Msg("program_change", program=5), now_ms=300)   # -> stop
    check("PC 0 -> tap", ac[0] == "tap")
    check("PC 2 -> double", ac[1] == "double")
    check("PC 5 -> stop", ac[2] == "stop")
    check("discrete never touches button_cb", ev == [])
    m.handle(Msg("program_change", program=99), now_ms=400)  # unmapped
    check("unmapped PC -> no action", len(ac) == 3)


def test_transition_toggle():
    m, ev, ac = make()   # PC 1 -> transition
    m.handle(Msg("program_change", program=1), now_ms=100)   # 1st -> hold
    m.handle(Msg("program_change", program=1), now_ms=200)   # 2nd -> release
    m.handle(Msg("program_change", program=1), now_ms=300)   # 3rd -> hold
    check("transition toggles hold/release/hold", ac == ["hold", "release", "hold"])


def test_cc_primary_and_ignore():
    m, ev, ac = make({"control_change": {64: "primary"}})
    m.handle(Msg("control_change", control=64, value=127), now_ms=100)  # down
    m.handle(Msg("control_change", control=64, value=0), now_ms=180)    # up
    check("CC>=64 down -> PRESS, <64 -> RELEASE",
          [e[0] for e in ev] == [P.PedalInput.PRESS, P.PedalInput.RELEASE])
    # clock / transport ignored, consumed
    check("clock ignored+consumed", m.handle(Msg("clock"), now_ms=200) is True)
    check("clock produced nothing", len(ev) == 2 and len(ac) == 0)


def test_feedback_guard_drops_storm():
    logs = []
    m = P.PedalInput(button_cb=lambda e, t: None,
                     action_cb=lambda g: logs.append(("act", g)),
                     mapping={"program_change": {0: "tap"}},
                     log_fn=lambda tag, s: logs.append((tag, s)))
    consumed_after_cap = None
    for i in range(80):                       # 80 msgs in the same 1s window
        consumed_after_cap = m.handle(Msg("program_change", program=0), now_ms=500)
    warned = any("feedback" in s for (_t, s) in logs if isinstance(s, str))
    acts = sum(1 for (t, _s) in logs if t == "act")
    check("storm logs a feedback warning", warned)
    check("storm is rate-capped (fewer than 80 acted)", acts < 80)


def test_end_to_end_through_mind():
    """Drive the REAL mind through the pedal: primary switch tap starts it,
    discrete 'double' stops it -- proving the pedal events work the grammar."""
    # minimal synthetic song via the beatbuddy suite's builder is overkill here;
    # build a 1-part song inline.
    s = bb.Song(); s.bpm = 120; s.nPart = 1; s.tracks = []
    def trk(evs):
        t = bb.Track(); t.event = evs; t.nTick = 1920; t.barLength = 1920
        t.timeSigNum = 4; t.timeSigDen = 4; bb._adjust_trig_length(t); return t
    s.tracks = [trk([(0, 36, 100), (960, 38, 100)])]           # part1 main
    p = bb.SongPart(); p.mainLoopIndex = 0; p.nDrumFill = 0; p.drumFillIndex = [-1]*8
    s.part = [p]; s.intro = bb.SongPart(); s.outro = bb.SongPart()

    notes = []
    m = bb.BeatBuddyMind(emit=lambda n, v, d, r, pid, pu: notes.append(n) if v > 0 else None)
    m.load_song(s)

    def action(g):
        {"tap": m.tap, "double": m.double_tap, "stop": m.stop,
         "hold": m.hold, "release": m.release, "pause": m.pause}.get(g, lambda: None)()
    pedal = P.PedalInput(button_cb=lambda e, t: m.button_callback(e, t),
                         action_cb=action,
                         mapping={"note": {0: "primary"}, "program_change": {2: "double"}},
                         log_fn=lambda *a: None)

    # primary tap from stopped -> starts (no intro -> straight to main)
    pedal.handle(Msg("note_on", note=0, velocity=100), now_ms=0)
    pedal.handle(Msg("note_off", note=0, velocity=0), now_ms=80)
    for i in range(20):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("pedal primary tap started the song", m.is_playing())
    check("mind emitted main notes via pedal start", 36 in notes)
    # discrete double -> stop
    pedal.handle(Msg("program_change", program=2), now_ms=1000)
    for i in range(200):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == bb.STOPPED:
            break
    check("pedal double stopped the song", m.PlayerStatus == bb.STOPPED)


# ── FCB front door (profile) -- shapes MEASURED off the real FCB, 2026-09-23 ─────
FCB = P.FCB_EF_PROFILE


def make_fcb(mapping=None, logs=None):
    ac, ex, ev, sw = [], [], [], []
    m = P.PedalInput(button_cb=lambda e, t: ev.append(e), action_cb=ac.append,
                     mapping=mapping, profile=FCB,
                     exp_cb=lambda n, v: ex.append((n, v)),
                     switch_cb=lambda a, d, mo: sw.append((a, d, mo)),
                     log_fn=(lambda t, s: logs.append(s)) if logs is not None
                     else (lambda *a: None))
    m.sw = sw
    return m, ac, ex, ev


def cc(ctl, val, ch=12):
    return Msg("control_change", channel=ch, control=ctl, value=val)


def non(n, ch=0):
    return Msg("note_on", channel=ch, note=n, velocity=85)


def noff(n, ch=0):
    return Msg("note_off", channel=ch, note=n, velocity=85)


def ef_only(page):
    return {"ef": {"pages": [page]}}


def test_fcb_switches():
    logs = []
    m, ac, ex, ev = make_fcb({}, logs=logs)            # nothing mapped anywhere
    for i, c in enumerate([21, 22, 23, 24, 25, 26, 27, 28, 29, 20]):
        m.handle(cc(c, 127), now_ms=i * 1000)
    got = [s for s in logs if "switch" in s]
    check("FCB CC21..29,20 -> EF switches 1..10 in order",
          got == [f"ef p1 switch {n} -> unmapped" for n in range(1, 11)])
    check("an empty map does nothing at all", ac == [] and ev == [] and m.sw == [])
    m2, ac2, _, _ = make_fcb(ef_only({1: "tap"}))
    m2.handle(cc(21, 127), now_ms=0)
    m2.handle(cc(21, 0), now_ms=2000)     # EF toggles: the 2nd press arrives as 0
    check("mapped switch fires on EVERY press (127 or 0)", ac2 == ["tap", "tap"])


def test_fcb_modes():
    m, ac, ex, ev = make_fcb()             # DEFAULT_FCB_MAP
    m.handle(cc(21, 127), now_ms=0)
    check("EF switch 1 -> chord:1, press only, not momentary",
          m.sw == [("chord:1", True, False)])
    m.sw.clear()
    m.handle(non(24), now_ms=1000)
    m.handle(noff(24), now_ms=3900)        # measured: release 2.9 s later
    check("note mode switch 1 -> chord:1 down THEN up, momentary",
          m.sw == [("chord:1", True, True), ("chord:1", False, True)])
    check("SP mode: note 24 = physical switch 3, 36 = switch 10 (1-2 are relays)",
          FCB["switch_note"][24] == 3 and FCB["switch_note"][36] == 10
          and 1 not in FCB["switch_note"].values())
    check("SP switch 8 = chord VI, 9 = BeatBuddy footswitch",
          m.action_for("notes", 8) == "chord:6" and m.action_for("notes", 9) == "primary")
    m.handle(Msg("program_change", channel=0, program=0), now_ms=5000)
    m.handle(cc(107, 0, ch=0), now_ms=5001)
    check("Pr ch1 send -> BeatBuddy tap (the default)", ac == ["tap"])
    check("the Pr bank prefix (CC107) is taken as a bank, not junk",
          m.dropped == {})


def test_fcb_pages():
    logs = []
    m, ac, ex, ev = make_fcb(logs=logs)
    m.handle(cc(20, 127), now_ms=0)        # switch 10 = page up
    check("switch 10 -> EF page 2", m.page.get("ef") == 1)
    m.handle(cc(21, 127), now_ms=1000)
    check("switch 1 on the empty page 2 does nothing", m.sw == [])
    m.handle(cc(20, 127), now_ms=2000)     # wraps back to page 1
    m.handle(cc(21, 127), now_ms=3000)
    check("page up wraps to page 1; switch 1 plays chord:1 again",
          m.page["ef"] == 0 and m.sw == [("chord:1", True, False)])
    m.handle(cc(29, 127), now_ms=4000)     # switch 9 = page down (wraps to last)
    check("switch 9 -> page down (wraps)", m.page["ef"] == 1)
    check("pages are per FCB mode", m.page.get("notes", 0) == 0)
    check("page change is logged", any("ef page 2/2" in s for s in logs))


def test_fcb_primary_footswitch():
    m, ac, ex, ev = make_fcb()             # notes switch 7 = primary
    m.handle(non(35), now_ms=0)
    m.poll(now_ms=700)                     # held past LONG_MS
    m.handle(noff(35), now_ms=800)
    check("note-mode footswitch: PRESS, hold -> LONG, RELEASE",
          ev == [P.PedalInput.PRESS, P.PedalInput.LONG, P.PedalInput.RELEASE])
    m2, _, _, ev2 = make_fcb(ef_only({1: "primary"}))
    m2.handle(cc(21, 127), now_ms=0)
    check("EF (press-only) primary = an instant tap (PRESS+RELEASE)",
          ev2 == [P.PedalInput.PRESS, P.PedalInput.RELEASE])


def test_fcb_front_door_drops():
    m, ac, ex, ev = make_fcb(ef_only({1: "tap", 10: "stop"}))
    junk = [cc(123, 0, ch=0),
            Msg("note_on", channel=0, note=25, velocity=85),      # not a switch note
            Msg("note_on", channel=12, note=24, velocity=85),     # switch note, wrong ch
            Msg("stop"),                                          # CH345 corruption
            Msg("polytouch", channel=4, note=85, value=0),        # CH345 corruption
            cc(21, 127, ch=0),                                    # right CC, wrong ch
            cc(50, 127)]                                          # right ch, unknown CC
    for i, j in enumerate(junk):
        check(f"junk {j.type} consumed", m.handle(j, now_ms=i * 1000) is True)
    check("no junk reaches an action/exp/button",
          ac == [] and ex == [] and ev == [] and m.sw == [])
    check("junk is counted by shape", sum(m.dropped.values()) == len(junk))


def sweep(m, vals, t0, step=20):
    for i, v in enumerate(vals):
        m.handle(cc(31, v), now_ms=t0 + i * step)
    return t0 + len(vals) * step


def test_fcb_exp_sweep_deglitch():
    m, ac, ex, ev = make_fcb()
    up = [0, 4, 8, 12, 16, 17, 20, 29, 33, 100, 104, 108, 109, 118, 111, 116, 120, 124, 127]
    t = sweep(m, [0], 0)                       # learn the rest point
    t = sweep(m, up, t + 2000)
    m.poll(now_ms=t + 100)                     # pedal still -> travel end flushes
    vals = [v for n, v in ex]
    check("exp sweep emits on CC31 as pedal B", ex and all(n == "B" for n, _ in ex))
    check("exp sweep ends exactly at 127", vals[-1] == 127)
    check("glitch 118 (between 109 and 111) never emitted", 118 not in vals)
    check("exp output never goes backwards on a rising sweep",
          all(b >= a for a, b in zip(vals, vals[1:])))
    ex.clear()
    t = sweep(m, [123, 119, 115, 111, 8, 4, 0], t + 200)
    m.poll(now_ms=t + 100)
    check("falling sweep ends exactly at 0", ex and ex[-1][1] == 0)
    # creep into the end in small steps: the last move is under the deadband but
    # the travel END must still land
    m2, _, ex2, _ = make_fcb()
    t = sweep(m2, [127], 0)
    t = sweep(m2, [100, 90, 80, 40, 20, 10, 5, 3, 2, 1, 0], t + 2000)
    m2.poll(now_ms=t + 100)
    check("small-step creep still lands exactly on 0", ex2 and ex2[-1][1] == 0)


def test_fcb_exp_rest_noise():
    m, ac, ex, ev = make_fcb()
    t = sweep(m, [64], 0)
    t = sweep(m, [65, 63, 64, 66, 62, 64, 67], t + 2000)     # dirty-pot flicker
    t = sweep(m, [64, 95, 64], t + 2000)                     # lone spike
    t = sweep(m, [65, 95, 64], t + 2000)                     # wobble then spike, same side
    m.poll(now_ms=t + 100)
    check("rest flicker + lone spike emit NOTHING", ex == [])
    t = sweep(m, [70, 76, 80], t + 2000)                     # a real press
    check("a real move from rest still wakes it", len(ex) > 0)
    m.poll(now_ms=t + 100)                                   # let the move land
    ex.clear()
    t = sweep(m, [80, 81, 80, 81, 80, 79, 80], t)            # hover mid-travel
    m.poll(now_ms=t + 100)
    check("hover flicker mid-travel emits nothing", ex == [])


def test_fcb_sweep_is_not_a_storm():
    logs = []
    m, ac, ex, ev = make_fcb(ef_only({1: "tap"}), logs=logs)
    for i in range(128):                                      # 128 readings in ~0.6s
        m.handle(cc(30, i), now_ms=5 * i)
    check("a fast pedal sweep does not trip the feedback guard",
          not any("feedback" in s for s in logs))
    m.handle(cc(21, 127), now_ms=700)
    check("switch still works right after a sweep", ac == ["tap"])


def test_brain_quarantine_static():
    """The CH13 guarantee is structural: the pedal port's thread only calls
    pedal.handle, never the router or any output. Checked on the real source."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py"),
               encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "make_pedal_handler_thread")
    calls = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            calls.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    bad = sorted(calls & {"_route_message", "_pass", "send_to", "note_on_out",
                          "send_synth_midi", "send", "note_on", "note_off"})
    check(f"pedal thread never routes/sends (calls {bad})", not bad)
    check("pedal thread feeds pedal.handle", "handle" in calls)
    check("brain ALWAYS gives the pedal a front-door profile",
          "or _orc1_pedal.FCB_EF_PROFILE" in src)


def _brain_ns():
    """Exec the brain's pedal action code (extracted by AST from the real orc1.py)
    against stubs -- orc1.py itself can't be imported."""
    import ast, types
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"_pedal_exp_value", "_pedal_degree_pitch", "_pedal_chord_off",
            "_pedal_chord_on", "_pedal_chord", "_pedal_switch_action", "_pedal_rc505"}
    parts = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            parts.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") in ("_pedal_chord_st", "_pedal_exp", "_pedal_exp_t")
                for t in n.targets):
            parts.append(ast.get_source_segment(src, n))
    log = []

    class Timer:
        def __init__(self, secs, fn):
            self.secs, self.fn, self.cancelled = secs, fn, False
            log.append(("timer", round(secs, 3)))
        def start(self): pass
        def cancel(self): self.cancelled = True

    class Msg:
        def __init__(self, type, **kw): self.type = type; self.__dict__.update(kw)

    cfg = types.SimpleNamespace(CH_CTRL=13, PEDAL_CHORD_OCTAVE=4, PEDAL_CHORD_VEL=100,
                                PEDAL_CHORD_STYLE="latch", PEDAL_CHORD_STAB_BEATS=1,
                                PEDAL_EXP_MAP={})
    arp = types.SimpleNamespace(_estimated_bpm=lambda: None, internal_bpm=120)
    ns = {"cfg": cfg, "arp": arp, "state": {"scale": "MAJ", "root": 0},
          "SCALES": {"MAJ": [0, 2, 4, 5, 7, 9, 11], "MIN5": [0, 3, 5, 7, 10]},
          "handle_note_input": lambda p, v: log.append(("on", p)),
          "handle_note_off_input": lambda p: log.append(("off", p)),
          "_execute_cmd": lambda c: log.append(("cmd", c["action"])),
          "send_synth_midi": lambda m: log.append(("synth", m.channel, m.control, m.value)),
          "send_to": lambda k, m: log.append(("port", k, m.channel, m.control, m.value)),
          "mido": types.SimpleNamespace(Message=Msg),
          "threading": types.SimpleNamespace(Timer=Timer),
          "time": __import__("time")}
    ns["rc505"] = types.SimpleNamespace(
        cc_map={"tgt_tr_stop": {}, "all_start_stop": {}},
        press_down=lambda n: log.append(("rc_down", n)),
        press_up=lambda n: log.append(("rc_up", n)),
        tap=lambda n: log.append(("rc_tap", n)),
        hold_for=lambda n: log.append(("rc_hold", n)))
    exec((chr(10) * 2).join(parts), ns)
    check("brain pedal code extracted (7 functions + state)",
          all(k in ns for k in want) and "_pedal_chord_st" in ns)
    return ns, log, cfg


def test_brain_chords():
    ns, log, cfg = _brain_ns()
    P_ = ns["_pedal_degree_pitch"]
    check("C major degrees I..VI = 48,50,52,53,55,57",
          [P_(n) for n in range(1, 7)] == [48, 50, 52, 53, 55, 57])
    ns["state"].update(scale="MIN5", root=9)          # A minor pentatonic
    check("pentatonic degree 6 wraps up an octave (A5 -> 69)", P_(6) == 12 * 4 + 9 + 12)
    ns["state"].update(scale="MAJ", root=0)
    act = ns["_pedal_switch_action"]
    act("chord:1", True, False)
    act("chord:2", True, False)                        # latch: next chord replaces
    act("chord:2", True, False)                        # same switch again = stop
    check("EF latch: I on, I off + II on, II off",
          log == [("on", 48), ("off", 48), ("on", 50), ("off", 50)])
    log.clear()
    act("chord:5", True, True); act("chord:5", False, True)
    check("note mode: sounds while held", log == [("on", 55), ("off", 55)])
    log.clear()
    act("chord:3", True, True); act("chord:1", False, True)   # stale release
    check("a release of a DIFFERENT switch doesn't kill the chord", log == [("on", 52)])
    act("chord:stop", True, False)
    check("chord:stop releases", log[-1] == ("off", 52))
    log.clear()
    cfg.PEDAL_CHORD_STYLE = "stab"
    act("chord:4", True, False)
    check("stab: plays and schedules a 1-beat release at 120 bpm",
          log == [("on", 53), ("timer", 0.5)])
    act("chord:4", True, False)
    check("stab: pressing again RE-triggers (not a latch stop)",
          log[-3:] == [("off", 53), ("on", 53), ("timer", 0.5)])


def test_brain_cmd_and_exp():
    ns, log, cfg = _brain_ns()
    ns["_pedal_switch_action"]("cmd:SEQ_PLAY", True, False)
    ns["_pedal_switch_action"]("cmd:SEQ_PLAY", False, True)
    check("cmd: runs the ORC command once, on the press", log == [("cmd", "SEQ_PLAY")])
    log.clear()
    ev = ns["_pedal_exp_value"]
    ev("A", 64)
    check("unmapped exp pedal sends nothing", log == [])
    cfg.PEDAL_EXP_MAP = {"A": {"port": "synth", "ch": 3, "cc": 74},
                         "B": {"port": "rc505", "ch": 1, "cc": 11}}
    ev("A", 90); ev("B", 10)
    check("exp A -> synth ch3 CC74, exp B -> rc505 port ch1 CC11",
          log == [("synth", 2, 74, 90), ("port", "rc505", 0, 11, 10)])
    log.clear()
    cfg.PEDAL_EXP_MAP = {"A": {"port": "synth", "ch": 13, "cc": 1}}
    ev("A", 50)
    check("exp pedal can NEVER send on CH_CTRL (13)", log == [])


def test_brain_rc505():
    ns, log, cfg = _brain_ns()
    act = ns["_pedal_switch_action"]
    act("rc505:tgt_tr_stop", True, True); act("rc505:tgt_tr_stop", False, True)
    check("SP: the RC sees the foot's real press and release",
          log == [("rc_down", "tgt_tr_stop"), ("rc_up", "tgt_tr_stop")])
    log.clear()
    act("rc505:all_start_stop", True, False)
    check("EF (press-only): a complete tap", log == [("rc_tap", "all_start_stop")])
    log.clear()
    act("rc505:tgt_tr_stop:hold", True, True); act("rc505:tgt_tr_stop:hold", False, True)
    act("rc505:tgt_tr_stop:hold", True, False)
    check(":hold = the ORC holds it, on the press only, in SP and EF",
          log == [("rc_hold", "tgt_tr_stop"), ("rc_hold", "tgt_tr_stop")])
    log.clear()
    act("rc505:tr9_rec_play", True, True); act("rc505:tr9_rec_play", False, True)
    check("a name with no RC assign sends nothing", log == [])
    ns["rc505"] = None
    act("rc505:tgt_tr_stop", True, True)
    check("no RC handler -> nothing, no crash", log == [])


def test_fcb_rc505_and_sp_pages():
    import orc1_rc505 as R
    check("rc505 actions validate", P.valid_switch_action("rc505:tgt_tr_stop")
          and P.valid_switch_action("rc505:tr3_play_stop:hold"))
    check("bad rc505 actions refused", not any(P.valid_switch_action(a) for a in (
        "rc505:", "rc505:Bad Name", "rc505:x:y", "rc505:x:hold:hold")))
    names = {v.split(":")[1] for v, _ in P.SWITCH_CHOICES if v.startswith("rc505:")}
    h = R.RC505Handler(send=lambda m: None, config_path=None)
    check(f"every RC choice on /pedal is in the RC's default map ({sorted(names - set(h.cc_map))})",
          names and names <= set(h.cc_map))
    check("every /pedal choice passes the validator",
          all(P.valid_switch_action(v) for v, _ in P.SWITCH_CHOICES))
    # SP with ORC-side paging: switch 10 = page up on every page
    conf, errs = P.validate_config({"map": {"notes": {
        "all": {"10": "page:up"},
        "pages": [{"4": "rc505:tgt_tr_stop", "9": "rc505:tgt_tr_stop:hold"},
                  {"4": "chord:1"}]}}})
    check("SP pages + page:up + rc505 actions survive validation",
          not errs and conf["map"]["notes"]["all"] == {10: "page:up"}
          and conf["map"]["notes"]["pages"][0] == {4: "rc505:tgt_tr_stop",
                                                   9: "rc505:tgt_tr_stop:hold"})
    m, ac, ex, ev = make_fcb(conf["map"])
    m.handle(non(26), now_ms=0)            # switch 4 down on page 1 = RC stop
    m.handle(non(36), now_ms=100)          # switch 10 = page up, while 4 is held
    m.handle(noff(36), now_ms=150)
    m.handle(noff(26), now_ms=2500)        # 4 released on page 2 (chord:1 there)
    check("SP page change mid-hold: the release goes to what the PRESS ran",
          m.sw == [("rc505:tgt_tr_stop", True, True), ("rc505:tgt_tr_stop", False, True)]
          and m.page["notes"] == 1)
    m.sw.clear()
    m.handle(non(26), now_ms=3000); m.handle(noff(26), now_ms=3200)
    check("SP page 2: switch 4 is now chord:1 (down + up)",
          m.sw == [("chord:1", True, True), ("chord:1", False, True)])
    m.sw.clear()
    m.handle(noff(28), now_ms=4000)
    check("a release with no press seen does nothing", m.sw == [])


def test_bb_song_survives_restart():
    """A restart used to leave the mind in NO_SONG_LOADED -> the FCB's BeatBuddy
    switch did nothing. The loaded song is now saved and reloaded at startup."""
    import ast, tempfile, types, json as _json
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    parts = [ast.get_source_segment(src, n) for n in tree.body
             if isinstance(n, ast.FunctionDef)
             and n.name in ("_bb_song_file", "_bb_song_restore", "_bb_song_select")]
    home = tempfile.mkdtemp()
    songs = os.path.join(home, "songs"); os.makedirs(songs)
    open(os.path.join(songs, "rock.BBS"), "w").close()
    loads = []

    class Mind:
        _songs_dir = songs
        CurrSong = None
        def load_bbs(self, p):
            loads.append(p); self.CurrSong = types.SimpleNamespace(bpm=100, nPart=1)
        load_ir = load_bbs

    import os as _os
    fake_os = types.SimpleNamespace(path=types.SimpleNamespace(
        expanduser=lambda p: p.replace("~", home), exists=_os.path.exists,
        isabs=_os.path.isabs, join=_os.path.join, basename=_os.path.basename,
        abspath=_os.path.abspath), replace=_os.replace)
    ns = {"os": fake_os, "json": _json, "beatbuddy": Mind(), "arp": None,
          "_bb_follow_clock": False}
    exec((chr(10) * 2).join(parts), ns)
    ns["_bb_song_select"]("rock")
    saved = os.path.join(home, ".orc1_bb_song.json")
    check("loading a song saves it", os.path.exists(saved))
    ns["beatbuddy"] = Mind()                  # "restart": a fresh, empty mind
    loads.clear()
    ns["_bb_song_restore"]()
    check("startup restore reloads the saved song",
          loads and loads[0].endswith("rock.BBS"))
    check("the brain calls _bb_song_restore() at BeatBuddy startup",
          "_bb_song_restore()\n        print(f\"  BeatBuddy mind ready" in src)


def test_fcb_pr_mode():
    got = []
    m = P.PedalInput(button_cb=lambda *a: None, action_cb=lambda g: got.append(("g", g)),
                     profile=FCB, log_fn=lambda *a: None,
                     mapping={"pr": {1: "tap", 3: "bb_song", 4: "fwd:mpc:10"}},
                     pr_cb=lambda a, ch, b, p: got.append((a, ch, b, p)))
    m.handle(cc(107, 5, ch=2), now_ms=0)                   # measured: ch3 bank5
    m.handle(Msg("program_change", channel=2, program=32), now_ms=10)
    check("Pr ch3 bank5 #32 -> bb_song with the dialled numbers",
          got == [("bb_song", 3, 5, 32)])
    got.clear()
    m.handle(Msg("program_change", channel=3, program=7), now_ms=2000)
    check("bank is per channel (ch4 never got a prefix -> bank 0)",
          got == [("fwd:mpc:10", 4, 0, 7)])
    got.clear()
    m.handle(Msg("program_change", channel=0, program=99), now_ms=4000)
    m.handle(Msg("program_change", channel=9, program=1), now_ms=6000)
    check("ch1 -> tap whatever the number; an unmapped channel does nothing",
          got == [("g", "tap")])


def test_config_validation():
    ports = {"mpc", "rc505", "pisound"}
    raw = {"map": {"ef": {"all": {"9": "page:down"},
                          "pages": [{"1": "chord:1", "2": "cmd:SEQ_PLAY",
                                     "3": "cmd:RESTART_ORC", "4": "rm -rf",
                                     "11": "tap"}]},
                   "notes": {"pages": [{"1": "tap", "9": "primary"}]},
                   "pr": {"2": "fwd:mpc:10", "5": "fwd:mpc:13", "6": "fwd:nope:1",
                          "17": "tap"}},
           "exp": {"A": {"port": "synth", "ch": "3", "cc": "74"},
                   "B": {"port": "mpc", "ch": 13, "cc": 1}},
           "chord_style": "stab", "stab_beats": 99, "chord_octave": "x"}
    c, errs = P.validate_config(raw, ports=ports)
    ef = c["map"]["ef"]
    check("JSON string keys come back as ints", ef["all"] == {9: "page:down"})
    check("valid switch actions kept", ef["pages"][0] == {1: "chord:1", 2: "cmd:SEQ_PLAY"})
    check("RESTART_ORC / garbage / switch 11 all refused + reported",
          sum("ef p1" in e or "switch 11" in e for e in errs) == 3)
    check("SP switch 1 refused (relay, sends no MIDI)",
          c["map"]["notes"]["pages"][0] == {9: "primary"})
    check("Pr: valid forward kept; ch13 / unknown port / ch17 refused",
          c["map"]["pr"] == {2: "fwd:mpc:10"})
    check("exp A kept (strings coerced); exp B on ch13 refused",
          c["exp"] == {"A": {"port": "synth", "ch": 3, "cc": 74}, "B": None})
    check("stab beats clamped, bad octave falls back",
          c["chord_style"] == "stab" and c["stab_beats"] == 8.0 and c["chord_octave"] == 4)
    d, e2 = P.validate_config({})
    check("empty config = the defaults, no errors",
          d == P.default_config() and e2 == [])
    import json as _j
    rt, e3 = P.validate_config(_j.loads(_j.dumps(c)), ports=ports)
    check("a saved config survives the JSON round trip unchanged", rt == c and e3 == [])


def _brain_conf_ns(tmp_home):
    import ast, types, json as _json
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py"),
               encoding="utf-8").read()
    want = {"_pedal_pr_action", "_pedal_conf_file", "_pedal_conf_apply",
            "_pedal_conf_load", "_pedal_conf_set", "_protect_err"}
    parts = [ast.get_source_segment(src, n) for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef) and n.name in want]
    log = []

    class Msg_:
        def __init__(self, type, **kw): self.type = type; self.__dict__.update(kw)

    import os as _os, threading as _th
    cfg = types.SimpleNamespace(CH_CTRL=13, PORTS={"mpc": "MPC", "rc505": "RC"})
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(expanduser=lambda p: p.replace("~", tmp_home)),
        listdir=lambda d: ["b.BBS", "a.BBS", "c.json", "notes.txt"], replace=_os.replace)
    pedal = types.SimpleNamespace(_lock=_th.Lock(), map=None, page={"ef": 1})
    ns = {"cfg": cfg, "os": fake_os, "json": _json, "_orc1_pedal": P, "pedal": pedal,
          "beatbuddy": types.SimpleNamespace(_songs_dir="/x"),
          "mido": types.SimpleNamespace(Message=Msg_),
          "send_to": lambda k, m: log.append((k, m.type, m.channel,
                                              getattr(m, "program", getattr(m, "value", None)))),
          "_bb_song_select": lambda n: log.append(("song", n)),
          "_pedal_chord_off": lambda: log.append(("chord_off",)),
          "print": lambda *a, **k: None}
    exec((chr(10) * 2).join(parts), ns)
    return ns, log, cfg, pedal


def test_brain_pr_and_config():
    import tempfile, json as _json
    home = tempfile.mkdtemp()
    ns, log, cfg, pedal = _brain_conf_ns(home)
    pr = ns["_pedal_pr_action"]
    pr("bb_song", 1, 0, 1)
    check("bb_song #1 = the 2nd song in sorted order", log == [("song", "b.BBS")])
    log.clear()
    pr("bb_song", 1, 1, 0)                         # bank 1 = song #128: only 3 exist
    check("bb_song counts the bank (bank1 #0 = song 128 -> none, nothing loaded)",
          log == [])
    log.clear()
    pr("fwd:mpc:10", 2, 3, 40)
    check("fwd: bank as CC0 then the program change, on the chosen channel",
          log == [("mpc", "control_change", 9, 3), ("mpc", "program_change", 9, 40)])
    log.clear()
    pr("fwd:mpc:13", 2, 0, 40)
    check("fwd can never send on CH_CTRL (13)", log == [])
    ns["_pedal_conf_set"]({"map": {"ef": {"pages": [{"1": "chord:3"}]}},
                           "exp": {"A": {"port": "synth", "ch": 2, "cc": 7}},
                           "chord_style": "stab"})
    check("config applied live: map swapped, pages reset, chord released",
          pedal.map["ef"]["pages"] == [{1: "chord:3"}] and pedal.page == {}
          and ("chord_off",) in log)
    check("config applied live: exp + chord style on cfg",
          cfg.PEDAL_EXP_MAP["A"] == {"port": "synth", "ch": 2, "cc": 7}
          and cfg.PEDAL_CHORD_STYLE == "stab")
    saved = _json.load(open(os.path.join(home, ".orc1_pedal.json")))
    check("config saved to ~/.orc1_pedal.json", saved["chord_style"] == "stab")
    back = ns["_pedal_conf_load"]()
    check("reload after restart gives the same config (int keys)",
          back["map"]["ef"]["pages"] == [{1: "chord:3"}])
    open(os.path.join(home, ".orc1_pedal.json"), "w").write("{corrupt")
    check("a corrupt file loads as None (= defaults), no crash",
          ns["_pedal_conf_load"]() is None)


def main():
    for t in (test_recognised, test_primary_tap, test_primary_long_press_via_poll,
              test_primary_double_tap, test_discrete_pc, test_transition_toggle,
              test_cc_primary_and_ignore, test_feedback_guard_drops_storm,
              test_end_to_end_through_mind,
              test_fcb_switches, test_fcb_modes, test_fcb_pages,
              test_fcb_primary_footswitch, test_fcb_front_door_drops, test_fcb_exp_sweep_deglitch,
              test_fcb_exp_rest_noise, test_fcb_sweep_is_not_a_storm,
              test_brain_quarantine_static, test_brain_chords,
              test_brain_cmd_and_exp, test_brain_rc505, test_fcb_rc505_and_sp_pages,
              test_bb_song_survives_restart,
              test_fcb_pr_mode, test_config_validation, test_brain_pr_and_config):
        t()
    print(f"\n{_p} passed, {_f} failed  (expected {EXPECTED})")
    if _p != EXPECTED or _f:
        print("  !! COUNT/FAIL MISMATCH -- regression")
        sys.exit(1)


if __name__ == "__main__":
    main()
