"""
orc1_pedal.py -- foot-pedal input for the BeatBuddy mind (orc1_beatbuddy.py).

Recognises a pedal by USB/MIDI NAME (the ORC north star) and turns its footswitch
messages into BeatBuddy pedal gestures. owner's FCB1010 has no USB -- it reaches
the Pi through a generic USB->DIN adapter that enumerates as **"CH345"** (QinHeng
CH345 cable). So the default recognised prefixes are "ch345" and "fcb".

⚠ FEEDBACK SAFETY. On this rig the CH345's DIN is cross-patched into the pisound DIN.
The brain's generic USB path would `_pass()` any USB input straight to the pisound
DIN out, which loops back through the adapter -> a runaway (the session-19 BeatStep
scenario, new device). Therefore the pedal is handled on its OWN input thread that
routes ONLY into this module and NEVER passes anything through to a DIN/synth out.
This class is a pure SINK: it reads messages and calls back into the mind. It emits
to no MIDI port. That is what closes the loop.

Two message models are supported per-switch via the config map:

  * "primary"  -- a momentary switch that sends note-on (down) / note-off (up).
    This module runs the little debounce/timing layer the BeatBuddy firmware kept
    in button.c (openbbm ships no button.c): down->PRESS, held-past-LONG_MS->
    LONG_PRESS, up->RELEASE, a 2nd down within MULTI_MS->MULTI_TAP. Long-press is
    resolved by poll(now_ms) (called from the mind's clock tick) so no timer thread
    is needed. These feed mind.button_callback() directly -- the faithful path.

  * a DISCRETE gesture name -- for press-only switches (the FCB default sends a
    Program Change per switch, no release). Each mapped message fires one gesture:
      tap | transition | double | start | stop | pause |
      song_next | song_prev | kit_next | kit_prev
    "transition" toggles hold()/release() across successive presses so a press-only
    pedal can still start a transition and then advance.

The FCB's real stream was captured 2026-09-23 -> FCB_EF_PROFILE (the brain always
uses a profile: only the profile's messages are accepted, everything else dropped).
Switch presses are logged; dropped shapes are logged once each. A crude runaway guard
(switches only -- a pedal sweep is legitimately ~50 msg/s) logs a feedback storm.
The legacy raw PC/note/CC map below applies only when no profile is given.
"""

import threading
import time


# Default recognition + map. Overridable from cfg.PEDAL_PORT_PREFIXES / cfg.PEDAL_MAP.
DEFAULT_PREFIXES = ("ch345", "fcb")

# map: {"program_change": {num: gesture}, "control_change": {num: gesture},
#       "note": {num: gesture-or-"primary"}}.  A gesture is one of the DISCRETE
# names below, or "primary" (momentary on/off switch -> the firmware pedal grammar).
DEFAULT_MAP = {
    "program_change": {
        0: "tap",           # FCB switch 1 -> drumfill / start
        1: "transition",    # switch 2     -> transition (toggles hold/release)
        2: "double",        # switch 3     -> outro / stop
        3: "song_next",     # switch 4
        4: "song_prev",     # switch 5
        5: "stop",          # switch 6
        6: "kit_next",      # switch 7
        7: "kit_prev",      # switch 8
    },
    "note": {
        # If a switch is configured to send note on/off, map it to "primary" for
        # the full momentary grammar (tap/hold/release/double from one switch):
        0: "primary",
    },
    "control_change": {},
}

DISCRETE = {"tap", "transition", "double", "start", "stop", "pause",
            "song_next", "song_prev", "kit_next", "kit_prev"}

# ── Device profile: the FRONT DOOR (quarantine) ─────────────────────────────────
# With a profile, handle() accepts ONLY the messages the profile names and turns them
# into abstract controls (switch N pressed / expression pedal X at value V). Anything
# else from the port -- other FCB modes, CH345 byte corruption -- is DROPPED (counted,
# logged once per shape). Nothing from the port is ever forwarded anywhere, so the
# FCB's CH13 traffic can never reach the ORC's CH_CTRL inbox.
#
# FCB1010 + EurekaPROM in EF mode, MEASURED 2026-09-23 (aseqdump on the CH345):
#   * everything on ch13 (0-based 12) -- the ORC's reserved CH_CTRL
#   * switches 1-9 -> CC21-29, switch 10 -> CC20 (pressed 1..10 in order); EF mode
#     TOGGLES, so successive presses alternate 127/0 -> ANY value = one press. There
#     is no release message, so no hold/long-press from EF mode.
#   * expression A -> CC30, B -> CC31, 0-127 in ~4-step increments, with occasional
#     out-of-sequence glitches (e.g. 109,118,111) -> ExpFilter below.
#   * the CH345 adapter corrupted a few bytes in 635 (a CC turned into Stop + a
#     note-on; a note-on into poly-aftertouch) -- the strict whitelist drops those.
#
# The FCB's own MODE button is the ORC's page selector (owner, 2026-09-23): each
# mode's stream has its own shape, so the front door knows which mode sent it.
#   * "ef"    -- the CCs above. Toggle switches: a press only, no release.
#   * "notes" -- the FCB's SP (Specials) mode: ch1 note-on at PRESS, note-off at
#     RELEASE (MEASURED: a 3 s hold = note-off 2.9 s later) -> real momentary
#     switches. Physical switches 1-2 drive the FCB's two RELAY jacks and send NO
#     MIDI (owner heard the relay click; EurekaPROM docs: SP = relays + notes).
#     Pressing 1..10 in order gave exactly 8 notes, so switches 3-10 = notes
#     24,26,28,29,31,33,35,36 [measured, 2026-09-23].
#   * "pr"    -- the FCB's Pr (Presets) mode is a DIAL-A-NUMBER mode: some switches
#     step the channel / bank / preset number on the FCB's display, others SEND it
#     as CC107=<bank> then PC <program> on the dialled channel [measured: ch1 bank0
#     PC0; ch3 bank5 PC32]. The ORC treats the dialled CHANNEL as the target
#     selector: map["pr"][ch] says what that channel's number does.
FCB_EF_PROFILE = {
    "name": "FCB1010/EurekaPROM",
    "channel": 12,
    "switch_cc": {21: 1, 22: 2, 23: 3, 24: 4, 25: 5, 26: 6, 27: 7, 28: 8, 29: 9,
                  20: 10},
    "exp_cc": {30: "A", 31: "B"},
    "note_channel": 0,
    "switch_note": {24: 3, 26: 4, 28: 5, 29: 6, 31: 7, 33: 8, 35: 9, 36: 10},
    "pr_bank_cc": 107,          # Pr mode's bank prefix (any channel)
}

# Modes whose switches send a RELEASE (so hold-to-sound and the BeatBuddy footswitch
# grammar work). EF and PC are press-only.
MOMENTARY_MODES = {"notes"}

# What each switch DOES, per FCB mode. Action names:
#   a BeatBuddy gesture (tap/double/stop/transition/song_next/...), "primary" (the
#   real BeatBuddy footswitch: tap / hold = transition / double-tap = stop -- needs a
#   momentary mode), "chord:N" (chord on scale degree N; "chord:stop" releases),
#   "cmd:ACTION" (any ORC command-socket action, e.g. cmd:SEQ_PLAY),
#   "rc505:NAME" (an RC-505 assign by orc1_rc505 action name; in SP the RC sees
#   the real press AND release, so its own hold gestures work), "rc505:NAME:hold"
#   (the ORC holds that RC button ~4 s itself, e.g. hold STOP = clear, any mode),
#   "page:up" / "page:down" (ORC-side pages within the current FCB mode).
# Per mode: "pages" is a list of {switch: action}; "all" applies on every page
# unless the page itself maps that switch. Switch "any" = the one-button PC mode.
_CHORDS_1_6 = {n: f"chord:{n}" for n in range(1, 7)}
DEFAULT_FCB_MAP = {
    "ef":    {"all": {9: "page:down", 10: "page:up"},
              "pages": [dict(_CHORDS_1_6), {}]},
    # SP mode: only 8 MIDI switches (3-10), so no paging -- 3-8 = chords I-VI
    # (sound while held), 9 = the BeatBuddy footswitch, 10 free.
    "notes": {"pages": [{3: "chord:1", 4: "chord:2", 5: "chord:3", 6: "chord:4",
                         7: "chord:5", 8: "chord:6", 9: "primary"}]},
    # Pr: dialled channel -> what its number does. ch1 = BeatBuddy tap (what the
    # old "every switch sends PC0" behaviour was, and owner liked it).
    "pr":    {1: "tap"},
}

# ── Config: shared by the brain (applies + persists) and the web (/pedal) ───────
# One validator so a bad value from the page can never reach the brain's actions.
import copy as _copy
import re as _re

MODES = {"ef": range(1, 11), "notes": range(3, 11)}     # switches that send MIDI
BB_GESTURES = ["tap", "double", "stop", "pause", "transition",
               "song_next", "song_prev", "kit_next", "kit_prev"]
CMD_BLOCKED = {"PEDAL_CONFIG_SET", "RESTART_ORC"}          # no foot-gun / recursion
_CMD_RE = _re.compile(r"^cmd:([A-Z][A-Z0-9_]*)(:[A-Za-z0-9_.\-]+)*$")
_FWD_RE = _re.compile(r"^fwd:([A-Za-z0-9_\-]+):(\d{1,2})$")
CTRL_CH = 13          # the ORC's reserved control channel -- never an output

# (value, label) for the page's dropdowns. "cmd:" values are any ORC command.
SWITCH_CHOICES = (
    [("", "— nothing —")]
    + [(f"chord:{n}", f"Chord {r}") for n, r in
       zip(range(1, 8), ["I", "II", "III", "IV", "V", "VI", "VII"])]
    + [("chord:stop", "Chord stop"),
       ("primary", "BeatBuddy footswitch (tap / hold / 2x)"),
       ("tap", "BeatBuddy tap (start / fill)"),
       ("double", "BeatBuddy outro / stop"), ("stop", "BeatBuddy stop now"),
       ("pause", "BeatBuddy pause"), ("transition", "BeatBuddy transition"),
       ("song_next", "BeatBuddy next song"), ("song_prev", "BeatBuddy prev song"),
       ("kit_next", "BeatBuddy next kit"), ("kit_prev", "BeatBuddy prev kit"),
       ("page:up", "Page up"), ("page:down", "Page down"),
       ("cmd:SEQ_PLAY", "Seq play"), ("cmd:SEQ_STOP", "Seq stop"),
       ("cmd:SEQ_TOGGLE_RECORD", "Seq record"),
       ("cmd:TAP_TEMPO", "Tap tempo"), ("cmd:TOGGLE_HOLD", "Chord hold on/off"),
       ("cmd:TOGGLE_ARP", "Arp on/off"), ("cmd:PANIC", "Panic (all notes off)")]
    # RC-505: names = the assigns in ~/rc505_tools/rc505_config.json (orc1_rc505)
    + [("rc505:tgt_tr_rec_play", "RC-505 rec / dub / play (selected track)"),
       ("rc505:tgt_tr_stop", "RC-505 stop (selected track; SP hold = RC's clear)"),
       ("rc505:tgt_tr_stop:hold", "RC-505 CLEAR selected track (ORC holds stop)"),
       ("rc505:target_track_dec", "RC-505 previous track"),
       ("rc505:target_track_inc", "RC-505 next track"),
       ("rc505:all_start_stop", "RC-505 all start / stop"),
       ("rc505:undo_redo", "RC-505 undo / redo")]
    + [(f"rc505:tr{n}_rec_play", f"RC-505 track {n} rec / dub / play") for n in range(1, 6)]
    + [(f"rc505:tr{n}_play_stop", f"RC-505 track {n} play / stop") for n in range(1, 6)]
    + [(f"rc505:tr{n}_play_stop:hold", f"RC-505 CLEAR track {n} (ORC holds stop)")
       for n in range(1, 6)])
PR_CHOICES = ([("", "— nothing —"), ("bb_song", "BeatBuddy song #N")]
              + [(g, "BeatBuddy " + g.replace("_", " ")) for g in BB_GESTURES])


def valid_switch_action(a):
    if a in ("", None):
        return True
    if a in DISCRETE or a in ("primary", "chord:stop", "page:up", "page:down"):
        return True
    if _re.fullmatch(r"chord:[1-7]", a) or _re.fullmatch(r"rc505:[a-z0-9_]+(:hold)?", a):
        return True
    m = _CMD_RE.match(a)
    return bool(m) and m.group(1) not in CMD_BLOCKED


def valid_pr_action(a, ports=None):
    if a in ("", None, "bb_song") or a in BB_GESTURES:
        return True
    m = _FWD_RE.match(a)
    if not m:
        return False
    ch = int(m.group(2))
    return (1 <= ch <= 16 and ch != CTRL_CH
            and (ports is None or m.group(1) in ports))


def refuse_protected(conf, prev, protect_error):
    """PROTECTED CHANNELS (2026-10-14): a pedal OUTPUT on a protected channel may not be newly CHOSEN.
    conf = a validated config about to be saved, prev = the one saved before (validated, or None),
    protect_error(ch) -> '' or why not. An entry that is new or changed and points at a protected channel
    goes back to what prev had (or away); one that was already saved is kept (protection never silently
    drops old config). Mutates conf; returns the errors."""
    errs = []
    prev = prev if isinstance(prev, dict) else default_config()
    pr, old_pr = conf["map"].get("pr", {}), (prev.get("map") or {}).get("pr", {})
    for k in list(pr):
        m = _FWD_RE.match(pr[k] or "")
        why = protect_error(int(m.group(2))) if m else ""
        if why and pr[k] != old_pr.get(k):
            errs.append(f"pr ch{k}: {why}")
            if old_pr.get(k):
                pr[k] = old_pr[k]
            else:
                pr.pop(k)
    for name in ("A", "B"):
        t, old = conf["exp"].get(name), (prev.get("exp") or {}).get(name)
        why = protect_error(t["ch"]) if t else ""
        if why and t != old:
            errs.append(f"exp {name}: {why}")
            conf["exp"][name] = old
    return errs


def default_config():
    return {"map": _copy.deepcopy(DEFAULT_FCB_MAP),
            "exp": {"A": None, "B": None},
            "chord_style": "latch", "stab_beats": 1.0, "chord_octave": 4}


def validate_config(conf, ports=None):
    """Return (clean_config, errors). Keys may arrive as JSON strings ("3") --
    they come back as ints. Anything invalid is dropped and reported, never
    passed through. `ports` = the allowed MIDI port keys (None = don't check)."""
    errs = []
    out = default_config()
    conf = conf if isinstance(conf, dict) else {}
    m_in = conf.get("map") if isinstance(conf.get("map"), dict) else {}
    for mode, sw_range in MODES.items():
        src = m_in.get(mode)
        if not isinstance(src, dict):
            continue
        def clean(d, where):
            res = {}
            for k, a in (d or {}).items():
                try:
                    n = int(k)
                except (TypeError, ValueError):
                    errs.append(f"{where}: bad switch {k!r}"); continue
                if n not in sw_range:
                    errs.append(f"{where}: switch {n} sends no MIDI"); continue
                if not valid_switch_action(a):
                    errs.append(f"{where} switch {n}: bad action {a!r}"); continue
                if a:
                    res[n] = a
            return res
        pages = src.get("pages")
        pages = pages if isinstance(pages, list) and pages else [{}]
        out["map"][mode] = {"all": clean(src.get("all"), f"{mode} all"),
                            "pages": [clean(p if isinstance(p, dict) else {},
                                            f"{mode} p{i + 1}")
                                      for i, p in enumerate(pages[:16])]}
    pr_in = m_in.get("pr")
    if isinstance(pr_in, dict):
        pr = {}
        for k, a in pr_in.items():
            try:
                ch = int(k)
            except (TypeError, ValueError):
                errs.append(f"pr: bad channel {k!r}"); continue
            if not 1 <= ch <= 16:
                errs.append(f"pr: channel {ch} out of range"); continue
            if not valid_pr_action(a, ports):
                errs.append(f"pr ch{ch}: bad action {a!r}"); continue
            if a:
                pr[ch] = a
        out["map"]["pr"] = pr
    e_in = conf.get("exp") if isinstance(conf.get("exp"), dict) else {}
    for name in ("A", "B"):
        t = e_in.get(name)
        if not t:
            continue
        try:
            port, ch, cc = str(t.get("port", "")), int(t.get("ch")), int(t.get("cc"))
        except (TypeError, ValueError, AttributeError):
            errs.append(f"exp {name}: bad target"); continue
        if not (1 <= ch <= 16) or ch == CTRL_CH or not 0 <= cc <= 127:
            errs.append(f"exp {name}: ch must be 1-16 (not {CTRL_CH}), cc 0-127"); continue
        if port != "synth" and ports is not None and port not in ports:
            errs.append(f"exp {name}: unknown port {port!r}"); continue
        out["exp"][name] = {"port": port, "ch": ch, "cc": cc}
    if conf.get("chord_style") in ("latch", "stab"):
        out["chord_style"] = conf["chord_style"]
    try:
        out["stab_beats"] = min(8.0, max(0.25, float(conf.get("stab_beats", 1))))
    except (TypeError, ValueError):
        errs.append("stab_beats: not a number")
    try:
        out["chord_octave"] = min(7, max(1, int(conf.get("chord_octave", 4))))
    except (TypeError, ValueError):
        errs.append("chord_octave: not a number")
    return out, errs


class ExpFilter:
    """De-glitch one expression pedal. Three layers:
      * WAKE gate: from rest, the pedal must move >= `wake` steps away from where it
        last settled on TWO consecutive readings before anything is emitted. Kills a
        dirty pot's flicker and lone spikes while nobody is touching it.
      * median-of-3 while moving: rejects a single out-of-sequence reading.
      * deadband: a median within `deadband` of the last emitted value is not sent.
    flush() (called from poll) emits the last raw reading once the pedal has been
    still for `settle_ms`, so the travel ends land exactly (0 and 127 matter).
    After `rest_ms` without a reading the pedal is considered at rest again."""

    def __init__(self, wake=6, deadband=3, settle_ms=40, rest_ms=1500):
        self.wake, self.deadband = wake, deadband
        self.settle_ms, self.rest_ms = settle_ms, rest_ms
        self.hist = []          # last raw readings (<= 3)
        self.base = None        # where it last settled (wake reference)
        self.out = None         # last emitted value
        self.moving = False
        self.last_t = None
        self.flushed = True

    def feed(self, v, now_ms):
        """Take one raw reading; return a value to emit, or None."""
        if self.last_t is None or now_ms - self.last_t >= self.rest_ms:
            if self.moving and self.out is not None:
                self.base = self.out
            self.moving = False
        self.last_t = now_ms
        self.hist = (self.hist + [v])[-3:]
        if self.base is None:            # first reading ever: learn the rest point
            self.base = v
            return None
        if not self.moving:
            if len(self.hist) < 2:
                return None
            a, b = self.hist[-2], self.hist[-1]
            if (abs(a - self.base) >= self.wake and abs(b - self.base) >= self.wake
                    and (a > self.base) == (b > self.base)):
                self.moving = True
                return self._emit(b)
            return None
        med = sorted(self.hist)[1] if len(self.hist) == 3 else v
        if self.out is None or abs(med - self.out) >= self.deadband:
            return self._emit(med)
        self.flushed = False
        return None

    def _emit(self, v):
        self.out = v
        self.flushed = (v == self.hist[-1])
        return v

    def flush(self, now_ms):
        """Pedal still for settle_ms while moving -> emit the last raw reading."""
        if (self.moving and not self.flushed and self.last_t is not None
                and now_ms - self.last_t >= self.settle_ms):
            self.flushed = True
            last = self.hist[-1]
            # only a real difference or a travel END -- never a hover flicker
            if last != self.out and (abs(last - self.out) >= self.deadband
                                     or last in (0, 127)):
                self.out = last
                return last
        return None

LONG_MS = 500      # hold threshold -> LONG_PRESS (transition)
MULTI_MS = 350     # a 2nd press within this -> MULTI_TAP (double / stop)


class PedalInput:
    """Feedback-safe pedal sink. Feeds a momentary 'primary' switch straight into
    mind.button_callback (with press/long/release/multi timing) and fires discrete
    gestures via callbacks. Emits to NO port."""

    def __init__(self, button_cb, action_cb, mapping=None, log_fn=None,
                 long_ms=LONG_MS, multi_ms=MULTI_MS, profile=None, exp_cb=None,
                 exp_filter_kw=None, switch_cb=None, pr_cb=None):
        # button_cb(event_name, now_ms): event in PRESS/RELEASE/LONG_PRESS/MULTI_TAP
        self.button_cb = button_cb
        # action_cb(gesture_name): discrete gesture (tap/transition/double/...)
        self.action_cb = action_cb
        # switch_cb(action, down, momentary): every OTHER action (chord:N, cmd:X, ...)
        # -- the brain owns what those mean.
        self.switch_cb = switch_cb or (lambda *_: None)
        # pr_cb(action, channel_1based, bank, program): a Pr-mode number send
        self.pr_cb = pr_cb or (lambda *_: None)
        self._pr_bank = {}      # channel(0-based) -> last Pr bank prefix
        # profile: the device front door (see FCB_EF_PROFILE). With a profile the map
        # is per FCB mode (see DEFAULT_FCB_MAP). Without one, the legacy raw
        # PC/note/CC map below applies.
        self.profile = profile
        # exp_cb(name, value): a de-glitched expression-pedal value ("A"/"B")
        self.exp_cb = exp_cb or (lambda *_: None)
        self.page = {}          # FCB mode -> current page index
        self._held_action = {}  # (mode, switch) -> the action its press ran
        self.mode = None        # the FCB mode that sent the last switch message
        if profile is not None:
            self.map = DEFAULT_FCB_MAP if mapping is None else mapping
            kw = exp_filter_kw or {}
            self.exp = {n: ExpFilter(**kw) for n in profile.get("exp_cc", {}).values()}
        else:
            self.map = mapping or DEFAULT_MAP
            self.exp = {}
        self.dropped = {}       # shape -> count of messages the front door refused
        self._lock = threading.Lock()   # handle() runs on the port thread, poll() elsewhere
        self.log = log_fn or (lambda *_: None)
        self.long_ms = long_ms
        self.multi_ms = multi_ms
        # primary-switch state
        self._down = False
        self._down_t = 0
        self._long_fired = False
        self._last_up_t = -10_000
        # runaway guard
        self._win_start = 0.0
        self._win_count = 0

    # -- event names fed to mind.button_callback --------------------------------
    PRESS = "PEDAL_PRESS"
    RELEASE = "PEDAL_RELEASE"
    LONG = "PEDAL_LONG_PRESS"
    MULTI = "PEDAL_MULTI_TAP"

    def _guard(self, now_ms):
        """Crude feedback-storm detector: >40 pedal msgs/sec is not a human foot."""
        if now_ms - self._win_start >= 1000:
            self._win_start = now_ms
            self._win_count = 0
        self._win_count += 1
        if self._win_count == 41:
            self.log("pedal", "⚠ >40 msg/s -- possible MIDI feedback loop on the pedal input")
        return self._win_count <= 60   # above 60/s, drop (protect the mind)

    def handle(self, msg, now_ms=None):
        """Route one incoming pedal MIDI message. ALWAYS consumes it (returns True):
        nothing from a pedal port is ever passed on."""
        if now_ms is None:
            now_ms = int(time.monotonic() * 1000)
        with self._lock:
            if self.profile is not None:
                self._handle_profiled(msg, now_ms)
                return True
            return self._handle_raw(msg, now_ms)

    def _drop(self, shape):
        n = self.dropped.get(shape, 0) + 1
        self.dropped[shape] = n
        if n == 1:   # once per shape: a wrong FCB mode / adapter corruption is visible
            self.log("pedal", f"dropped {shape} (not a {self.profile['name']} message;"
                              f" further ones counted silently)")

    def _classify(self, msg):
        """Front door: (kind, mode, key, down) for an accepted message, else None.
        kind is "exp" (key = pedal name, down = value) or "switch"."""
        prof = self.profile
        mtype = getattr(msg, "type", None)
        ch = getattr(msg, "channel", None)
        if mtype == "control_change" and ch == prof["channel"]:
            if msg.control in prof.get("exp_cc", {}):
                return ("exp", None, prof["exp_cc"][msg.control], msg.value)
            sw = prof.get("switch_cc", {}).get(msg.control)
            if sw is not None:
                return ("switch", "ef", sw, True)       # EF toggles: any value = press
        if mtype in ("note_on", "note_off") and ch == prof.get("note_channel"):
            sw = prof.get("switch_note", {}).get(msg.note)
            if sw is not None:
                down = mtype == "note_on" and getattr(msg, "velocity", 0) > 0
                return ("switch", "notes", sw, down)
        if (mtype == "control_change" and isinstance(ch, int)
                and msg.control == prof.get("pr_bank_cc")):
            return ("bank", "pr", ch, msg.value)
        if mtype == "program_change" and isinstance(ch, int):
            return ("pr", "pr", ch, msg.program)
        return None

    def _handle_profiled(self, msg, now_ms):
        c = self._classify(msg)
        if c is None:
            mtype = getattr(msg, "type", None)
            ch = getattr(msg, "channel", None)
            ctl = getattr(msg, "control", None)
            self._drop(f"{mtype} ch{ch + 1 if isinstance(ch, int) else '-'}"
                       + (f" cc{ctl}" if ctl is not None else ""))
            return
        kind, mode, key, down = c
        if kind == "bank":                  # Pr bank prefix: remember, send nothing
            self._pr_bank[key] = down
            return
        if kind == "pr":
            if not self._guard(now_ms):
                return
            ch1, program = key + 1, down
            bank = self._pr_bank.get(key, 0)
            a = (self.map.get("pr") or {}).get(ch1)
            self.log("pedal", f"pr ch{ch1} bank{bank} #{program} -> {a or 'unmapped'}")
            if a in DISCRETE:
                self._fire_discrete(a, now_ms)
            elif a:
                self.pr_cb(a, ch1, bank, program)
            return
        if kind == "exp":
            out = self.exp[key].feed(down, now_ms)
            if out is not None:
                self.exp_cb(key, out)
            return
        if not self._guard(now_ms):       # switches only: a pedal sweep is not a storm
            return
        self.mode = mode
        if mode in MOMENTARY_MODES:
            # a release goes to whatever its PRESS ran, even if the page changed in
            # between (else a held RC button / chord would never be let go)
            if down:
                g = self.action_for(mode, key)
                self._held_action[(mode, key)] = g
            else:
                g = self._held_action.pop((mode, key), None)
        else:
            g = self.action_for(mode, key)
        if down:
            self.log("pedal", f"{mode} p{self.page.get(mode, 0) + 1} switch {key}"
                              f" -> {g or 'unmapped'}")
        if g:
            self._run(g, mode, down, now_ms)

    def pages(self, mode):
        return (self.map.get(mode) or {}).get("pages") or [{}]

    def action_for(self, mode, key):
        """The action a switch has on the CURRENT page of that FCB mode."""
        m = self.map.get(mode) or {}
        pages = self.pages(mode)
        page = pages[self.page.get(mode, 0) % len(pages)]
        return page.get(key) or (m.get("all") or {}).get(key)

    def _run(self, g, mode, down, now_ms):
        momentary = mode in MOMENTARY_MODES
        if g in ("page:up", "page:down"):
            if down:
                n = len(self.pages(mode))
                step = 1 if g == "page:up" else -1
                self.page[mode] = (self.page.get(mode, 0) + step) % n
                self.log("pedal", f"{mode} page {self.page[mode] + 1}/{n}")
        elif g == "primary":
            if down:
                self._sw_down(now_ms)
                if not momentary:          # a press-only mode: a press IS a tap
                    self._sw_up(now_ms)
            elif momentary:
                self._sw_up(now_ms)
        elif g in DISCRETE:
            if down:
                self._fire_discrete(g, now_ms)
        else:
            self.switch_cb(g, down, momentary)

    def _handle_raw(self, msg, now_ms):
        mtype = getattr(msg, "type", None)
        if mtype not in ("note_on", "note_off", "program_change", "control_change"):
            # clock / transport / sysex etc. -- a pedal never sends these; ignore.
            return True
        if not self._guard(now_ms):
            return True

        if mtype == "program_change":
            g = self.map.get("program_change", {}).get(msg.program)
            self.log("pedal", f"PC {msg.program} -> {g or 'unmapped'}")
            if g:
                self._fire_discrete(g, now_ms)
            return True

        if mtype == "control_change":
            g = self.map.get("control_change", {}).get(msg.control)
            # a CC switch is 'down' when value>=64 (typical), 'up' below
            self.log("pedal", f"CC {msg.control}={msg.value} -> {g or 'unmapped'}")
            if g == "primary":
                (self._sw_down if msg.value >= 64 else self._sw_up)(now_ms)
            elif g:
                if msg.value >= 64:      # discrete fires on the down edge
                    self._fire_discrete(g, now_ms)
            return True

        # note_on / note_off
        num = msg.note
        g = self.map.get("note", {}).get(num)
        down = (mtype == "note_on" and getattr(msg, "velocity", 0) > 0)
        self.log("pedal", f"{'note_on' if down else 'note_off'} {num} -> {g or 'unmapped'}")
        if g == "primary":
            (self._sw_down if down else self._sw_up)(now_ms)
        elif g:
            if down:                     # discrete note switch fires on press
                self._fire_discrete(g, now_ms)
        return True

    # -- momentary 'primary' switch: the button.c timing layer ------------------
    def _sw_down(self, now_ms):
        self._down = True
        self._down_t = now_ms
        self._long_fired = False
        if now_ms - self._last_up_t <= self.multi_ms:
            self.button_cb(self.MULTI, now_ms)   # rapid 2nd tap -> firmware multi-tap
        else:
            self.button_cb(self.PRESS, now_ms)

    def _sw_up(self, now_ms):
        if not self._down:
            return
        self._down = False
        self._last_up_t = now_ms
        self.button_cb(self.RELEASE, now_ms)

    def poll(self, now_ms=None):
        """Fire LONG_PRESS once the primary switch has been held past long_ms.
        Also flushes the expression filters so a pedal's travel END lands.
        Called from the brain's pedal poll thread (and the mind's clock tick)."""
        if now_ms is None:
            now_ms = int(time.monotonic() * 1000)
        with self._lock:
            for name, f in self.exp.items():
                out = f.flush(now_ms)
                if out is not None:
                    self.exp_cb(name, out)
            self._poll_long(now_ms)

    def _poll_long(self, now_ms):
        if not self._down or self._long_fired:
            return
        if now_ms - self._down_t >= self.long_ms:
            self._long_fired = True
            self.button_cb(self.LONG, now_ms)

    # -- discrete gesture dispatch ----------------------------------------------
    def _fire_discrete(self, g, now_ms):
        if g == "transition":
            # toggle: 1st press starts the transition (hold), next press advances (release)
            self._trans_on = not getattr(self, "_trans_on", False)
            self.action_cb("hold" if self._trans_on else "release")
        elif g in DISCRETE:
            self.action_cb(g)
        else:
            self.log("pedal", f"unknown gesture {g!r}")


def recognised(name, prefixes=DEFAULT_PREFIXES):
    """True if a MIDI input port name looks like a recognised pedal."""
    nl = (name or "").lower()
    return any(p in nl for p in prefixes)
