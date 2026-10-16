#!/usr/bin/env python3
"""Regression suite for orc1_kikpad (the KIKPAD's bank 2 = sampler + skipback surface).

Pure stdlib, no MIDI hardware, no live files: the engine (the orc1 module) is a
recorder, the paint output a list, and HOME a temp folder. Also checks every
brain function the surface calls really exists in orc1.py with a compatible
signature, and that the brain claims port :1 before its generic USB path.
Run:  python3 _test_kikpad_surface.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_kikpad as K

EXPECTED = 118
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


class Msg:
    def __init__(self, type, channel=0, **kw):
        self.type = type
        self.channel = channel
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return f"Msg({self.type} {self.__dict__})"


class Out:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.closed = False

    def send(self, m):
        self.sent.append(m)

    def close(self):
        self.closed = True


class Engine:
    """Records every named call; skipback calls keep their done callback."""
    def __init__(self):
        self.calls = []
        self.save_all_result = True

    def __getattr__(self, fn):
        if not fn.startswith(("sampler_", "skipback_", "module_cc", "visuals_")):
            raise AttributeError(fn)

        def f(*a):
            self.calls.append((fn,) + a)
            if fn == "skipback_save_all":
                return self.save_all_result
            return None
        return f

    def names(self):
        return [c[0] for c in self.calls]


HOME = tempfile.mkdtemp(prefix="kp_surface_")
NOW = [1000.0]


VIS_AUDIO = os.path.join(HOME, "vis_audio.json")


def make(home=HOME):
    e = Engine()
    s = K.KikpadSurface(e, home=home, clock=lambda: NOW[0], wall=lambda: NOW[0],
                        log=lambda m: None, vis_audio=VIS_AUDIO, n_sets=5)
    outs = []

    def opener(name):
        o = Out(name)
        outs.append(o)
        return o
    s._start_poll = lambda: None              # no thread in the tests: drive refresh by hand
    s.attach("KIKPAD:KIKPAD IN 32:1", opener=opener, msg_factory=Msg)
    return s, e, outs


def press(s, idx):
    s.handle(Msg("note_on", note=idx, velocity=127))


def release(s, idx):
    s.handle(Msg("note_off", note=idx, velocity=0))


def tap(s, idx):
    press(s, idx)
    release(s, idx)


def P(row, c):
    return row * 8 + c


try:
    # ---- port recognition (by name + port number) ------------------------------
    check("port :1 is the surface", K.is_surface_port("KIKPAD:KIKPAD IN 32:1"))
    check("a moved client number still matches", K.is_surface_port("KIKPAD:KIKPAD IN 40:1"))
    check("port :0 (the music banks) is not", not K.is_surface_port("KIKPAD:KIKPAD IN 32:0"))
    check("another device's :1 is not", not K.is_surface_port("pisound:pisound MIDI 20:1"))
    check("empty name", not K.is_surface_port(""))

    # ---- every function the surface calls exists in the brain -------------------
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    defs = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    ksrc = open(os.path.join(HERE, "orc1_kikpad.py"), encoding="utf-8").read()
    called = set()
    for node in ast.walk(ast.parse(ksrc)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_call" \
                and node.args and isinstance(node.args[0], ast.Constant):
            called.add((node.args[0].value, len(node.args) - 1))
        if isinstance(node, ast.IfExp):          # "a" if shift else "b" passed to _call
            for v in (node.body, node.orelse):
                if isinstance(v, ast.Constant) and str(v.value).startswith("sampler_"):
                    called.add((v.value, None))
    missing, bad_arity = [], []
    for fn, n in sorted(called, key=str):
        d = defs.get(fn)
        if d is None:
            missing.append(fn)
            continue
        if n is None:
            continue
        a = d.args
        lo = len(a.args) - len(a.defaults)
        if not lo <= n <= len(a.args):
            bad_arity.append(f"{fn}/{n}")
    check("surface calls >= 20 named brain functions", len(called) >= 20, f"{len(called)}")
    check("every called function exists in orc1.py", not missing, str(missing))
    check("every call's argument count fits the brain's signature", not bad_arity, str(bad_arity))
    check("no channel/CC plumbing in the surface (named calls only)",
          "send_synth_midi" not in ksrc and "_smp_cc" not in ksrc and "_CH_SAMPLER" not in ksrc)

    # ---- brain wiring: port :1 is claimed before the generic USB paths -----------
    def body(name):
        return ast.get_source_segment(src, defs[name])
    op = body("open_ports")
    check("open_ports claims the surface before the usb-engine branch",
          0 < op.find("_orc1_kikpad.is_surface_port") < op.find("usb_ins.append"))
    hs = body("_hotplug_scan_once")
    check("hot-plug claims the surface before the generic usb handler",
          0 < hs.find("_orc1_kikpad.is_surface_port(name)") < hs.find("make_usb_handler"))
    check("a vanished surface port detaches the paint output",
          "kikpad.detach(name)" in hs)
    ko = body("_kikpad_open")
    check("the surface reader feeds handle(), never _route_message",
          "surface.handle(msg)" in ko and "_route_message" not in ko)

    # ---- sampler pads -------------------------------------------------------------
    s, e, outs = make()
    tap(s, P(0, 2))
    check("A row0 col2 = grab last 4 bars", e.calls[-1] == ("sampler_capture_bars", "A", 4))
    tap(s, P(3, 0))
    check("B row3 col0 = grab last 1 bar", e.calls[-1] == ("sampler_capture_bars", "B", 1))
    tap(s, P(0, 6))
    check("A col6 = grab NEXT 4 bars", e.calls[-1] == ("sampler_capture_fwd_fixed", "A", 4))
    e.calls.clear()
    press(s, P(3, 7))
    check("B HOLD press = forward hold start", e.calls == [("sampler_capture_fwd_hold_start", "B")])
    release(s, P(3, 7))
    check("B HOLD release = hold end", e.calls[-1] == ("sampler_capture_fwd_hold_end", "B"))
    for c, fn in enumerate(("sampler_loop_toggle", "sampler_stop", "sampler_reverse",
                            "sampler_audition")):
        tap(s, P(1, c))
        check(f"A transport col {c} = {fn}", e.calls[-1] == (fn, "A"))
    tap(s, P(4, 5))
    check("B cue 2 = set-or-jump", e.calls[-1] == ("sampler_cue", "B", 2))
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_SET, velocity=127))
    tap(s, P(4, 5))
    check("SET + cue = clear that cue", e.calls[-1] == ("sampler_cue_clear", "B", 2))
    s.handle(Msg("note_off", note=K.BTN_BASE + K.BT_SET, velocity=0))
    tap(s, P(4, 5))
    check("SET released: back to set-or-jump", e.calls[-1] == ("sampler_cue", "B", 2))
    e.calls.clear()
    press(s, P(2, 2))
    check("A stutter 1/4 beat on press", e.calls == [("sampler_beat_repeat_on", "A", 0.25)])
    press(s, P(2, 2))
    check("a repeated note-on while held does not fire twice", len(e.calls) == 1)
    release(s, P(2, 2))
    check("stutter off on release", e.calls[-1] == ("sampler_beat_repeat_off", "A"))
    tap(s, P(5, 6))
    check("B jump 1/2 = seek 64", e.calls[-1] == ("sampler_seek", "B", 64))
    tap(s, P(2, 4))
    check("A jump 0 = seek 0", e.calls[-1] == ("sampler_seek", "A", 0))
    e.calls.clear()
    release(s, P(0, 0))
    check("a release with no press does nothing", e.calls == [])

    # leaving bank 2 mid-gesture must not strand a stutter / a held capture / SET
    e.calls.clear()
    press(s, P(5, 0))
    press(s, P(0, 7))
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_SET, velocity=127))
    s.handle(Msg("control_change", control=K.SHOWN_CC, value=0))
    check("leaving releases the stutter", ("sampler_beat_repeat_off", "B") in e.calls)
    check("leaving ends the held capture", ("sampler_capture_fwd_hold_end", "A") in e.calls)
    check("leaving drops SET", not s.shift)
    check("nothing still held", not s.held)

    # ---- encoders -----------------------------------------------------------------
    s, e, outs = make()
    s.handle(Msg("control_change", control=0, value=65))
    check("encoder 1 right = deck A speed 65", e.calls[-1] == ("sampler_speed", "A", 65))
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS2, velocity=127))
    s.handle(Msg("control_change", control=0, value=63))
    check("MS2 picks deck B: speed 63", e.calls[-1] == ("sampler_speed", "B", 63))
    s.handle(Msg("control_change", control=4, value=65))
    check("encoder 5 = EQ mid on deck B", e.calls[-1] == ("sampler_eq", "B", "mid", 66))
    s.handle(Msg("control_change", control=6, value=63))
    check("encoder 7 = crossfade (global)", e.calls[-1] == ("sampler_crossfade", 62))
    s.handle(Msg("control_change", control=7, value=65))
    check("encoder 8 = sampler master", e.calls[-1] == ("sampler_master_gain", 87))
    for _ in range(80):
        s.handle(Msg("control_change", control=1, value=65))
    check("gain clamps at 127", s.enc["B"]["gain"] == 127)
    n = len(e.calls)
    s.handle(Msg("control_change", control=1, value=65))
    check("no send once clamped", len(e.calls) == n)
    s.handle(Msg("control_change", control=2, value=65))
    tap(s, P(3, 0))
    check("a grab puts the cached filter back to bypass (the sampler resets it)",
          s.enc["B"]["filter"] == 64)
    s.handle(Msg("control_change", control=2, value=65))
    check("next filter turn continues from bypass", e.calls[-1] == ("sampler_filter", "B", 66))
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS1, velocity=127))
    check("MS1 picks deck A back", s.enc_deck == "A")
    n = len(e.calls)
    s.handle(Msg("control_change", control=0, value=65, channel=3))
    check("other MIDI channels are ignored", len(e.calls) == n)

    # ---- skipback -----------------------------------------------------------------
    os.makedirs(os.path.join(HOME, ".skipbuf"), exist_ok=True)
    with open(os.path.join(HOME, ".orc1_skipback_tracks.json"), "w") as f:
        json.dump({"tracks": [
            {"id": 2, "store": "ram", "length_secs": 60, "armed": True},
            {"id": 0, "store": "card", "length_secs": 1200, "armed": True},
            {"id": 1, "store": "card", "length_secs": 900, "armed": True},
            {"id": 5, "store": "ram", "length_secs": 30, "armed": False}]}, f)
    with open(os.path.join(HOME, ".skipbuf", "heads.json"), "w") as f:
        json.dump({"tracks": {"0": {"t": NOW[0] - 8}, "1": {"t": NOW[0] - 60},
                              "2": {"t": NOW[0] - 1}}}, f)
    s, e, outs = make()
    check("armed tracks only, sorted by id", [t["id"] for t in s.tracks] == [0, 1, 2])
    pads, _ = s.frame()
    check("card track with a recent head = blue", pads[P(6, 0)] == K.BLUE)
    check("card track 60 s stale = red (not recording)", pads[P(6, 1)] == K.RED)
    check("RAM track live = green", pads[P(6, 2)] == K.GREEN)
    check("empty slot dark", pads[P(6, 3)] == K.BLACK)
    check("default length 1m selected", pads[P(7, 1)] == K.WHITE and pads[P(7, 0)] == K.DIM_GREY)
    tap(s, P(7, 3))
    check("length pad picks 5 min", s.sb_len == 300)
    tap(s, P(6, 0))
    call = e.calls[-1]
    check("track pad = skipback_save(track 0, 300 s)", call[:3] == ("skipback_save", 0, 300))
    n = len(e.calls)
    tap(s, P(6, 0))
    check("second press while saving is ignored", len(e.calls) == n)
    call[3](True, "x.wav")
    pads, _ = s.frame()
    check("save done = green flash", pads[P(6, 0)] == K.GREEN and not s.sb_busy)
    NOW[0] += 2.0
    pads, _ = s.frame()
    check("flash ends", pads[P(6, 0)] == K.BLUE or pads[P(6, 0)] == K.RED)
    NOW[0] -= 2.0
    tap(s, P(7, 5))
    tap(s, P(6, 1))
    check("ALL on one track = its ring length", e.calls[-1][:3] == ("skipback_save", 1, 900))
    e.calls[-1][3](False, "")
    pads, _ = s.frame()
    check("failed save = red flash", pads[P(6, 1)] == K.RED)
    tap(s, P(7, 7))
    call = e.calls[-1]
    check("GRAB ALL = skipback_save_all(0 = everything, every track)",
          call[:3] == ("skipback_save_all", 0, None))
    check("GRAB ALL busy while writing", s.sb_all_busy)
    call[3](True, "multi")
    check("GRAB ALL done clears busy", not s.sb_all_busy)
    e.save_all_result = False
    tap(s, P(7, 7))
    pads, _ = s.frame()
    check("GRAB ALL refused (one already writing) = red, not stuck busy",
          not s.sb_all_busy and pads[P(7, 7)] == K.RED)
    n = len(e.calls)
    tap(s, P(6, 5))
    check("pressing an empty track slot does nothing", len(e.calls) == n)

    # ---- painting -----------------------------------------------------------------
    with open(os.path.join(HOME, ".orc1_sampler_view_A.json"), "w") as f:
        json.dump({"length_s": 4.0, "cues": [0.1, None, 0.5, None]}, f)
    s, e, outs = make()
    out = outs[-1]
    pads, btns = s.frame()
    check("deck A loaded: LOOP lit green", pads[P(1, 0)] == K.GREEN)
    check("deck B empty: LOOP dim", pads[P(4, 0)] == K.DIM_GREY)
    check("cue 1 + 3 set = yellow, 2 empty = dim",
          pads[P(1, 4)] == K.YELLOW and pads[P(1, 6)] == K.YELLOW and pads[P(1, 5)] == K.DIM_GREY)
    check("MS1 lit for the encoder deck", btns[K.BT_MS1] and not btns[K.BT_MS2])
    notes = [m for m in out.sent if m.type in ("note_on", "note_off")]
    check("attach paints all 64 pads + 24 buttons", len(notes) == 88, f"{len(notes)}")
    check("paint goes out on MIDI channel 1", all(m.channel == 0 for m in out.sent))
    out.sent.clear()
    s.render()
    check("nothing changed = nothing sent", out.sent == [])
    press(s, P(0, 0))
    check("a held pad lights white (one message)",
          len(out.sent) == 1 and out.sent[0].note == P(0, 0) and out.sent[0].velocity == K.WHITE)
    release(s, P(0, 0))
    out.sent.clear()
    s.handle(Msg("control_change", control=K.SHOWN_CC, value=1))
    check("bank shown again = full repaint", len(out.sent) == 88, f"{len(out.sent)}")
    old = out
    s2_opens = len(outs)
    s.attach("KIKPAD:KIKPAD IN 32:1", opener=lambda n: (outs.append(Out(n)) or outs[-1]),
             msg_factory=Msg)
    check("re-attach closes the old handle (a stale one drops silently)", old.closed)
    check("re-attach opens a fresh one and repaints", len(outs) == s2_opens + 1
          and len(outs[-1].sent) == 88)
    s.detach("KIKPAD:KIKPAD IN 99:1")
    check("detach for another name leaves the output", s.out is outs[-1])
    s.detach("KIKPAD:KIKPAD IN 32:1")
    check("detach closes the output", s.out is None and outs[-1].closed)
    press(s, P(0, 0))
    check("presses still work with no paint output", e.calls[-1] == ("sampler_capture_bars", "A", 1))
    check("colour values fit the pad (6 bits)", all(0 <= c < 64 for c in s.frame()[0]))
    s.handle(Msg("sysex", data=(1, 2, 3)))
    check("unknown message types are ignored", True)

    # ==== VISUALS page ===========================================================
    # -- the tables must be the renderer's own (parsed from daw/orc_visuals.py) ----
    vsrc = open(os.path.join(HERE, "daw", "orc_visuals.py"), encoding="utf-8").read()
    vtree = ast.parse(vsrc)
    r_knobs = r_scenes = r_scene_cc = None
    for node in vtree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
            if nm == "KNOBS":
                r_knobs = ast.literal_eval(node.value)
            elif nm == "SCENE_CC":
                r_scene_cc = ast.literal_eval(node.value)
            elif nm == "SCENES":
                r_scenes = [k.value for k in node.value.keys]
            elif nm == "KNOB_DEFAULT":                # has a {cc: 0 for cc in range(..)} splat
                r_kdef = eval(compile(ast.Expression(node.value), "KNOB_DEFAULT", "eval"),
                              {"__builtins__": {}, "range": range})
    check("VIS_KNOBS = the renderer's KNOBS (name -> CC)",
          r_knobs is not None and K.VIS_KNOBS == {v: k for k, v in r_knobs.items()})
    fresh_knobs = make()[0].vis["knobs"]
    check("VIS_DEFAULT = the renderer's KNOB_DEFAULT (by name), and a fresh surface starts there "
          "(not 64: a first nudge must not jump an off MOVEMENT knob to half)",
          K.VIS_DEFAULT == {r_knobs[cc]: v for cc, v in r_kdef.items()}
          and fresh_knobs == {n: r_kdef.get(K.VIS_KNOBS[n], 64) for n in K.VIS_KNOBS})
    check("scene CC = the renderer's SCENE_CC", K.VIS_SCENE_CC == r_scene_cc)
    check("VIS_SCENES = the renderer's SCENES, in order", list(K.VIS_SCENES) == r_scenes)
    n = len(K.VIS_SCENES)
    check("scene_value(i) lands on scene i through the renderer's mapping",
          all((K.scene_value(i) - 1) * n // 127 == i for i in range(n))
          and all(1 <= K.scene_value(i) <= 127 for i in range(n)))
    check("bucket_mid(i, n) buckets back to i, any list size 1..128",
          all(K.bucket(K.bucket_mid(i, m), m) == i for m in range(1, 129) for i in range(m)))
    banked = [nm for b in K.VIS_BANKS for nm in b if nm is not None]
    check("encoder banks reach every knob once (bg_mode + mask_shape are pads)",
          sorted(banked + ["bg_mode", "mask_shape"]) == sorted(K.VIS_KNOBS)
          and len(banked) == len(set(banked)))

    # -- brain: module_cc resolves the module live, unbound = silent ----------------
    seg = ast.get_source_segment(src, defs["module_cc"])
    sent = []
    ns = {"_module_channel": lambda m: {"visuals": 15}.get(m),
          "send_synth_midi": lambda m: sent.append(m),
          "mido": type("m", (), {"Message": staticmethod(lambda t, **kw: (t, kw))})}
    exec(seg, ns)
    ok = ns["module_cc"]("visuals", 21, 200)
    check("module_cc sends on the module's bound channel (ch15 -> 14), value clamped",
          ok is True and sent == [("control_change", {"channel": 14, "control": 21, "value": 127})])
    sent.clear()
    check("module_cc to an unbound module sends nothing", ns["module_cc"]("nobody", 21, 5) is False
          and sent == [])

    # -- page switching -------------------------------------------------------------
    with open(os.path.join(HOME, ".orc1_visuals_state.json"), "w") as f:
        json.dump({"knobs": {"speed": 70, "mask_fit": 127, "mask_shape": 0, "bg_mode": 0,
                             "bg_set": 0, "logo": 10}, "scene": "tube", "cycling": False}, f)
    if os.path.exists(VIS_AUDIO):
        os.remove(VIS_AUDIO)
    s, e, outs = make()
    RIGHT, LEFT = K.BTN_BASE + K.BT_RIGHT, K.BTN_BASE + K.BT_LEFT
    press(s, P(2, 0))                                  # deck A stutter held ...
    s.handle(Msg("note_on", note=RIGHT, velocity=127))
    check("RIGHT = visuals page; a held sampler stutter is released on the way",
          s.page == "visuals" and e.calls[-1] == ("sampler_beat_repeat_off", "A"))
    pads, btns = s.frame()
    check("RIGHT lit, LEFT dark, MS1 = encoder bank 1", btns[K.BT_RIGHT] and not btns[K.BT_LEFT]
          and btns[K.BT_MS1])

    # -- renderer stopped: only START works ----------------------------------------
    e.calls.clear()
    tap(s, P(0, 3))
    s.handle(Msg("control_change", control=0, value=65))
    check("stopped: scene pads + encoders do nothing", e.calls == [])
    check("stopped: START pad orange, scenes dim", pads[K.VIS_POWER] == K.ORANGE
          and pads[P(0, 0)] == K.DIM_GREY)
    tap(s, K.VIS_POWER)
    check("START tap = visuals_power(True)", e.calls == [("visuals_power", True)])
    tap(s, K.VIS_POWER)
    check("a second tap while starting doesn't start twice", len(e.calls) == 1)
    NOW[0] += 0.0
    p1 = s.frame()[0][K.VIS_POWER]
    NOW[0] += 0.25
    p2 = s.frame()[0][K.VIS_POWER]
    check("START blinks while starting", {p1, p2} == {K.WHITE, K.BLACK})

    # -- renderer running ------------------------------------------------------------
    open(VIS_AUDIO, "w").close()
    os.utime(VIS_AUDIO, (NOW[0], NOW[0]))
    s.refresh()
    pads, btns = s.frame()
    check("running (fresh audio file): START pad green", s.vis["running"]
          and pads[K.VIS_POWER] == K.GREEN)
    check("state file read: playing scene (tube) white", pads[P(0, 2)] == K.WHITE
          and pads[P(0, 0)] != K.WHITE)
    check("FIT on from the state file = cyan", pads[P(3, 7)] == K.CYAN)
    e.calls.clear()
    s.handle(Msg("control_change", control=0, value=65))
    check("encoder 1 (bank MS1) = speed, from the saved 70", e.calls[-1] == ("module_cc", "visuals", 21, 72))
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS4, velocity=127))
    s.handle(Msg("control_change", control=6, value=65))
    check("MS4 bank, encoder 7 = logo, one value per click", e.calls[-1] == ("module_cc", "visuals", 52, 11))
    plog = []
    s._log = plog.append                    # the handler swallows errors: collect them
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS1 + 4, velocity=127))
    s.handle(Msg("control_change", control=0, value=65))
    sz = e.calls[-1]
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS1 + 5, velocity=127))
    s.handle(Msg("control_change", control=4, value=65))
    tr = e.calls[-1]
    n_mod = len(e.calls)
    s.handle(Msg("control_change", control=7, value=65))
    check("MS5 bank enc 1 = camera SIZE (CC55); MS6 enc 5 = trail react (CC78); "
          "MS6 enc 8 is empty (sends nothing, no error); MS6 lit",
          sz[:3] == ("module_cc", "visuals", 55) and tr[:3] == ("module_cc", "visuals", 78)
          and len(e.calls) == n_mod and s.frame()[1][K.BT_MS1 + 5] and not plog)
    s._log = lambda m: None
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_MS4, velocity=127))
    tap(s, P(1, 3))
    check("scene pad 11 (chladni) = CC20 at its value",
          e.calls[-1] == ("module_cc", "visuals", 20, K.scene_value(11)))
    check("... and it lights at once", s.frame()[0][P(1, 3)] == K.WHITE)
    tap(s, P(2, 0))
    check("CYCLE = CC20 0, lit green", e.calls[-1] == ("module_cc", "visuals", 20, 0)
          and s.frame()[0][P(2, 0)] == K.GREEN)
    tap(s, P(2, 3))
    check("peephole pad 3 = rounded (shape 2) via mask_shape",
          e.calls[-1] == ("module_cc", "visuals", 44, K.bucket_mid(2, 7))
          and s.frame()[0][P(2, 3)] == K.WHITE)
    tap(s, P(3, 7))
    check("FIT toggles off (127 -> 0)", e.calls[-1] == ("module_cc", "visuals", 49, 0))
    tap(s, P(3, 2))
    check("bend mode 3rd = squeeze", e.calls[-1] == ("module_cc", "visuals", 35, K.bucket_mid(2, 5)))
    tap(s, P(4, 4))
    check("pattern set 5 (of 5)", e.calls[-1] == ("module_cc", "visuals", 50, K.bucket_mid(4, 5)))
    n_calls = len(e.calls)
    tap(s, P(4, 5))
    check("no 6th set pad when there are 5 sets", len(e.calls) == n_calls)
    tap(s, K.VIS_POWER)
    check("STOP needs SET (a bare tap while running does nothing)", len(e.calls) == n_calls)
    s.handle(Msg("note_on", note=K.BTN_BASE + K.BT_SET, velocity=127))
    tap(s, K.VIS_POWER)
    s.handle(Msg("note_off", note=K.BTN_BASE + K.BT_SET, velocity=0))
    check("SET + START pad = visuals_power(False)", e.calls[-1] == ("visuals_power", False))

    # -- the lagging state file doesn't snap back what was just turned --------------
    with open(os.path.join(HOME, ".orc1_visuals_state.json"), "w") as f:
        json.dump({"knobs": {"speed": 70, "trails": 99, "logo": 10},
                   "scene": "tube", "cycling": False}, f)
    os.utime(os.path.join(HOME, ".orc1_visuals_state.json"), (NOW[0] + 1, NOW[0] + 1))
    os.utime(VIS_AUDIO, (NOW[0], NOW[0]))
    s.refresh()
    check("a knob just turned keeps our value (72, not the file's 70)",
          s.vis["knobs"]["speed"] == 72)
    check("an untouched knob follows the file (trails 99: the web moved it)",
          s.vis["knobs"]["trails"] == 99)
    check("a scene just picked isn't snapped back", s.vis["scene"] != "tube" or s.vis["cycling"])
    NOW[0] += K.VIS_TOUCH_S + 0.5
    os.utime(os.path.join(HOME, ".orc1_visuals_state.json"), (NOW[0], NOW[0]))
    os.utime(VIS_AUDIO, (NOW[0], NOW[0]))
    s.refresh()
    check("after the touch window the file wins again", s.vis["knobs"]["speed"] == 70
          and s.vis["scene"] == "tube")
    NOW[0] += K.VIS_RUN_S + 1
    s.refresh()
    check("audio file goes stale = renderer counted as stopped", not s.vis["running"])
    s.handle(Msg("note_on", note=LEFT, velocity=127))
    check("LEFT = back to the sampler page", s.page == "sampler" and s.frame()[1][K.BT_LEFT])
finally:
    shutil.rmtree(HOME, ignore_errors=True)

print(f"{passed} passed, {failed} failed")
if passed + failed != EXPECTED:
    print(f"COUNT DRIFT: ran {passed + failed}, EXPECTED {EXPECTED}")
    failed += 1
sys.exit(1 if failed else 0)
