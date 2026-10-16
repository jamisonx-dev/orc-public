#!/usr/bin/env python3
"""
ORC-1 Mixtrack Handler  v2
Numark Mixtrack Pro 3 — dedicated control surface for the ORC-1 engine.

Completely isolated from the BSP/MPK control path. The Mixtrack sends on
MIDI channels 1-3; the engine uses 10/12/13/14/15. Zero overlap, and the
Pi never relays a Mixtrack message anywhere — it's all consumed here.

CHANNEL ARCHITECTURE (Mixtrack side):
  Ch 1  (0xB0)       —  Master section: crossfader, master gain, cue knobs
  Ch 2  (0xB1/0x91)  —  Deck A: sampler deck
  Ch 3  (0xB2/0x92)  —  Deck B: sampler deck

Both decks are SAMPLER decks. Chord mode, deck-B chord staging and the
crossfader chord morph were REMOVED 2026-08-01: they had never been used, and
the chord-mode jog was bound to relative_lead_voice / relative_pattern -- one
list-step per tick on a wheel that emits dense ticks, so spinning it ripped
through voicings and arp patterns. The crossfader now blends deck A against
deck B, which is what a crossfader on a DJ controller should do.

SHIFT is a per-action MODIFIER (x4 coarse scrub, x5 browse, SHIFT+touchstrip
-> FX param), NOT a map layer. It used to be an exclusive layer on deck B,
which silently disabled all 36 of that deck's controls.

──────────────────────────────────────────────────────────────────────────────
TO CHANGE ANY CONTROL ASSIGNMENT: edit the MIXTRACK_* dicts in orc1_config.py
and restart orc1.py. This file holds the action implementations and the
hardware-level (CC/note number) constants only — no assignments live here.
──────────────────────────────────────────────────────────────────────────────

Instantiation in orc1.py:
  from orc1_mixtrack import MixtrackHandler
  mixtrack = MixtrackHandler(engine_module)
  # then call mixtrack.handle_msg(msg) from the input thread
"""

import time
import threading

import mido


# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE CONSTANTS — fixed by the Mixtrack itself, do not change
# ══════════════════════════════════════════════════════════════════════════════

# MIDI channels (1-indexed, as seen on the wire)
CH_MASTER = 1   # 0xB0 / 0x90
CH_DECK1  = 2   # 0xB1 / 0x91
CH_DECK2  = 3   # 0xB2 / 0x92

# CC numbers (same on both deck channels)
CC_JOG         = 0x11   # 17  — jog wheel (relative, offset-binary)
CC_PITCH_HI    = 0x18   # 24  — pitch fader coarse
CC_PITCH_LO    = 0x38   # 56  — pitch fader fine
CC_EQ_HI       = 0x01   # 1
CC_EQ_MID      = 0x02   # 2
CC_EQ_LOW      = 0x03   # 3
CC_FILTER      = 0x04   # 4
CC_LINE_FADER  = 0x05   # 5
CC_BEAT_KNOB   = 0x13   # 19
CC_STRIP       = 0x1A   # 26  — touch strip
CC_STRIP_SHIFT = 0x1B   # 27  — shift+strip
CC_CROSSFADER  = 0x07   # 7   (master channel only)
CC_MASTER_GAIN = 0x0A   # 10  (master channel) -> master sampler level
CC_BROWSE      = 0x17   # 23  (master channel) browse encoder, relative
# Master-section buttons (master channel):
NOTE_LOAD1     = 0x10   # 16  LOAD-to-deck-A button
NOTE_LOAD2     = 0x11   # 17  LOAD-to-deck-B button
NOTE_ENC_PUSH  = 0x12   # 18  browse-encoder push

# Note numbers (same on both deck channels)
NOTE_PLAY       = 0x01   # 1
NOTE_SYNC       = 0x02   # 2
NOTE_CUE        = 0x03   # 3
NOTE_PITCH_DN   = 0x04   # 4
NOTE_PITCH_UP   = 0x05   # 5
NOTE_WHEEL_MODE = 0x06   # 6
NOTE_FX1        = 0x07   # 7
NOTE_FX2        = 0x08   # 8
NOTE_FX3        = 0x09   # 9
NOTE_TAP        = 0x0A   # 10
NOTE_SHIFT      = 0x0B   # 11
NOTE_JOG_TOUCH  = 0x0C   # 12  — capacitive sensor
NOTE_PAD_MODE   = 0x0D   # 13
NOTE_LOOP_IN    = 0x13   # 19
NOTE_LOOP_OUT   = 0x14   # 20
NOTE_LOOP_ON    = 0x15   # 21
NOTE_LOOP_HALVE = 0x16   # 22
NOTE_PAD1       = 0x17   # 23
NOTE_PAD2       = 0x18   # 24
NOTE_PAD3       = 0x19   # 25
NOTE_PAD4       = 0x1A   # 26
NOTE_HOTCUE1    = 0x1B   # 27
NOTE_HOTCUE2    = 0x1C   # 28
NOTE_HOTCUE3    = 0x1D   # 29
NOTE_HOTCUE4    = 0x1E   # 30
NOTE_INSTANT_FX = 0x1F   # 31
NOTE_SAMPLER1   = 0x20   # 32
NOTE_SAMPLER2   = 0x21   # 33
NOTE_SAMPLER3   = 0x22   # 34
NOTE_SAMPLER4   = 0x23   # 35

# Pad LED colors — the RGB pads read velocity as color. Non-pad buttons
# (PLAY/CUE/etc.) are single-color and ignore the velocity value.
# (LED_BLUE 32 and PITCH_CENTER_HI 64 went with chord mode 2026-08-01 —
# blue was the chord-mode pad colour, and the pitch-fader centre only meant
# anything to the chord transpose/BPM knobs.)
LED_OFF    = 0
LED_BLUE   = 32     # freed with chord mode; reused as the FORWARD-capture pad tint
LED_YELLOW = 96
LED_PURPLE = 127

# ── LED tick loop timing (tempo blink + forward-arm pulse) ───────────────────
_LED_TICK_S    = 0.04      # tick period of the LED thread (~25 Hz)
_TEMPO_FLASH_S = 0.07      # TAP LED on for this long at the START of each beat
_ARM_BLINK_HZ  = 6.0       # armed forward pad blink rate


def _tempo_led_on(now, phase0, bpm):
    """True when the TAP metronome LED should be lit at time `now`. The LED
    flashes for _TEMPO_FLASH_S at the start of every beat; `phase0` is the beat
    anchor (reset on each tap so the flash lands on the player's taps)."""
    if bpm <= 0:
        return False
    beat = 60.0 / bpm
    return ((now - phase0) % beat) < _TEMPO_FLASH_S


def _arm_blink_on(now):
    """Square-wave blink state for an armed forward-capture pad."""
    return int(now * _ARM_BLINK_HZ) % 2 == 0


# ══════════════════════════════════════════════════════════════════════════════
# SYMBOLIC-NAME → MIDI-NUMBER LOOKUPS
# These let the config dicts use human-readable names like "HOTCUE1" instead
# of bare numbers. The dispatch table at instantiation resolves them once.
# ══════════════════════════════════════════════════════════════════════════════

_NOTE_BY_NAME = {
    "PLAY":       NOTE_PLAY,       "SYNC":       NOTE_SYNC,
    "CUE":        NOTE_CUE,        "PITCH_DN":   NOTE_PITCH_DN,
    "PITCH_UP":   NOTE_PITCH_UP,   "WHEEL_MODE": NOTE_WHEEL_MODE,
    "FX1":        NOTE_FX1,        "FX2":        NOTE_FX2,
    "FX3":        NOTE_FX3,        "TAP":        NOTE_TAP,
    "SHIFT":      NOTE_SHIFT,      "JOG_TOUCH":  NOTE_JOG_TOUCH,
    "PAD_MODE":   NOTE_PAD_MODE,   "LOOP_IN":    NOTE_LOOP_IN,
    "LOOP_OUT":   NOTE_LOOP_OUT,   "LOOP_ON":    NOTE_LOOP_ON,
    "LOOP_HALVE": NOTE_LOOP_HALVE, "PAD1":       NOTE_PAD1,
    "PAD2":       NOTE_PAD2,       "PAD3":       NOTE_PAD3,
    "PAD4":       NOTE_PAD4,       "HOTCUE1":    NOTE_HOTCUE1,
    "HOTCUE2":    NOTE_HOTCUE2,    "HOTCUE3":    NOTE_HOTCUE3,
    "HOTCUE4":    NOTE_HOTCUE4,    "INSTANT_FX": NOTE_INSTANT_FX,
    "SAMPLER1":   NOTE_SAMPLER1,   "SAMPLER2":   NOTE_SAMPLER2,
    "SAMPLER3":   NOTE_SAMPLER3,   "SAMPLER4":   NOTE_SAMPLER4,
    "LOAD1":      NOTE_LOAD1,      "LOAD2":      NOTE_LOAD2,
    "ENC_PUSH":   NOTE_ENC_PUSH,
}

_CC_BY_NAME = {
    "JOG":          CC_JOG,         "PITCH_HI":    CC_PITCH_HI,
    "PITCH_LO":     CC_PITCH_LO,    "EQ_HI":       CC_EQ_HI,
    "EQ_MID":       CC_EQ_MID,      "EQ_LOW":      CC_EQ_LOW,
    "FILTER":       CC_FILTER,      "LINE_FADER":  CC_LINE_FADER,
    "BEAT_KNOB":    CC_BEAT_KNOB,   "STRIP":       CC_STRIP,
    "STRIP_SHIFT":  CC_STRIP_SHIFT, "CROSSFADER":  CC_CROSSFADER,
    "MASTER_GAIN":  CC_MASTER_GAIN, "BROWSE":      CC_BROWSE,
}


# ══════════════════════════════════════════════════════════════════════════════
# UNRESOLVED-ACTION REPORTING
# ══════════════════════════════════════════════════════════════════════════════
# Ported from orc1_fire.py (which got this 2026-07-29) after a review found the
# Mixtrack still without it.  Dispatch reaches every action by getattr() on a
# string from orc1_config.py, so a typo or a renamed method yields a DEAD
# CONTROL -- and the only report was behind `if self.cfg.DEBUG`, which is False
# on the live rig.  That is this project's oldest bug class: the pad does
# nothing, no exception is raised, and there is nothing to grep for.  The
# config header explicitly invites hand-editing, so this has to be loud
# whatever DEBUG says.
#
# Deduped, because CC dispatch fires per encoder click and an undeduped print
# would flood the journal from one knob sweep.
_UNRESOLVED_SEEN = set()


def _unresolved(prefix, name, kind):
    key = (prefix, name)
    if key in _UNRESOLVED_SEEN:
        return
    _UNRESOLVED_SEEN.add(key)
    print(f"  ! Mixtrack: {kind} mapped to '{name}' but no {prefix}{name}() "
          f"exists -- that control does nothing")


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER
# ══════════════════════════════════════════════════════════════════════════════

class MixtrackHandler:
    """
    All Mixtrack input is handled here. Nothing is ever forwarded — every
    message either drives engine state or is silently consumed.

    Construction:
      handler = MixtrackHandler(orc1_module)   # pass the imported engine
    """

    def __init__(self, eng, led_output=True):
        """led_output=False: build it without opening the LED port or waking
        the lights -- the brain passes that when it meets the Mixtrack mid DJ
        set (Mixxx owns the LEDs; reclaim opens + wakes it later)."""
        self.eng    = eng
        self.cfg    = eng.cfg            # cached for consistent access

        # ── Per-deck mod state ────────────────────────────────────────────
        # Mixtrack-local SHIFT — strictly local, does NOT touch engine
        # func_held. A per-action MODIFIER, never a layer (see _handle_deck).
        # This is the ONLY mod state left: _jog_touched, _pitch_hi and _xf
        # were removed with chord mode 2026-08-01. All three were still being
        # WRITTEN but nothing read them any more, which is the shape of state
        # a later reader trusts and acts on.
        self._shift       = {CH_DECK1: False, CH_DECK2: False}
        # Forward-capture: per-deck top-row direction (False = retro, the
        # default; True = forward N-bar). Toggled by _act_mixtrack_fwd_mode;
        # SHIFT inverts it for a single grab. _fwd_gesture latches the
        # retro/forward choice + pad at PRESS so releasing SHIFT mid-gesture
        # can't split a press from its release. See ORC1_Forward_Capture_Spec.
        self._fwd_mode    = {CH_DECK1: False, CH_DECK2: False}
        self._fwd_gesture = {}     # deck -> (use_forward, deck_letter, bars)
        # Settable-hotcue LED mirror: which of the 4 cue pads are ARMED, per
        # deck.  The synth deck is the real authority (it owns the positions);
        # this is only what colour to paint each pad.  It stays in sync because
        # every event that sets/clears a cue is brain-initiated -- a cue press,
        # a clear, or a grab/load (which wipes them, matching the deck).  A cue
        # set from a future WEB page would not update this (same hardware/web
        # split every page has); the deck's view JSON remains the web truth.
        self._cue_set = {CH_DECK1: [False, False, False, False],
                         CH_DECK2: [False, False, False, False]}

        self._master_ccs       = self._resolve_cc_map(
            getattr(self.cfg, "MIXTRACK_MASTER_CCS", {}))
        self._master_buttons   = self._resolve_button_map(
            getattr(self.cfg, "MIXTRACK_MASTER_BUTTONS", {}))

        # ── Deck control maps ────────────────────────────────────────────
        # Sampler only.  Mixtrack CHORD MODE WAS REMOVED 2026-08-01: owner
        # had never used it, and it was actively hostile -- the jog was wired
        # to relative_lead_voice / relative_pattern, one list-step per tick on
        # a wheel that emits dense ticks, so spinning it ripped through
        # voicings and arp patterns.  Deck B chord staging, the crossfader
        # chord morph and the whole per-deck mode switch went with it.
        self._d1s_buttons = self._resolve_button_map(
            getattr(self.cfg, "MIXTRACK_DECK1_SAMPLER_BUTTONS", {}))
        self._d1s_ccs     = self._resolve_cc_map(
            getattr(self.cfg, "MIXTRACK_DECK1_SAMPLER_CCS", {}))
        self._d2s_buttons = self._resolve_button_map(
            getattr(self.cfg, "MIXTRACK_DECK2_SAMPLER_BUTTONS", {}))
        self._d2s_ccs     = self._resolve_cc_map(
            getattr(self.cfg, "MIXTRACK_DECK2_SAMPLER_CCS", {}))
        self._maps = {
            CH_DECK1: (self._d1s_buttons, self._d1s_ccs),
            CH_DECK2: (self._d2s_buttons, self._d2s_ccs),
        }

        self._DECK_LETTER = {CH_DECK1: "A", CH_DECK2: "B"}
        print("  ✓ Mixtrack handler ready")
        print(f"    Deck A: {len(self._d1s_buttons)} buttons, "
              f"{len(self._d1s_ccs)} CCs")
        print(f"    Deck B: {len(self._d2s_buttons)} buttons, "
              f"{len(self._d2s_ccs)} CCs")

        # -- LED feedback output (mirrors the Fire's output pattern) --------
        # The Mixtrack lights a button/pad when it RECEIVES a note-on on the
        # same note+channel it sends. We open our own output to it and drive
        # the LEDs from here. Input handling never depends on this: if the
        # output can't open, the controller still works, just without lights.
        self._out = None
        self._out_lock = threading.Lock()
        if led_output:
            self._open_output(getattr(self.cfg, "MIXTRACK_PORT_PREFIX", "mixtrack"))
            self.wake()

        # ── FX button LEDs mirror the FX RACK running-state (2026-07-21) ──
        # A slow poller (not the MIDI thread — rack.status() reads json) keeps
        # the FX1-3 LEDs honest even when slots are toggled from the web /fx
        # page or the Fire. Toggles from THIS handler also repaint instantly
        # via rack.toggle's done-callback.
        self._fx_led_state = {}
        self._ring_saving  = {}            # (deck, note) -> True while a SHIFT+FXn save runs
        # Which rack slot (0-2) the SHIFT+touchstrip param sweep targets — set to
        # the last FX button pressed on either deck.
        self._rack_strip_slot = 0
        t = threading.Thread(target=self._fx_led_loop, daemon=True,
                             name="mt-fxleds")
        t.start()

        # ── Metronome LED (TAP button) + forward-capture arm pulse ──────────
        # A fast tick thread (25 Hz) flashes the TAP LED once per beat so the
        # live tempo is visible, and blinks a pad while a forward grab is armed.
        # Both decks share the tempo. Phase resets on each tap (_act_tap_tempo).
        self._tempo_phase = time.monotonic()
        self._tap_led_on  = False
        self._fwd_arm     = {}          # deck -> pad note currently blinking
        tl = threading.Thread(target=self._led_tick_loop, daemon=True,
                              name="mt-ledtick")
        tl.start()

    # ── Config resolution helpers ─────────────────────────────────────────

    @staticmethod
    def _well_formed(action, label):
        """An action must be a non-empty tuple/list whose head is a string.

        Dispatch does `name = action[0]` then `getattr(self, "_act_" + name)`
        OUTSIDE its own try/except, so a hand-edit typo like ("PLAY": (None,))
        raises a TypeError that escapes handle_msg into the controller's input
        thread -- which has no try/except at all.  The thread dies, the MIDI
        port stays open so the hotplug scanner never notices, and the whole
        controller goes dead until orc1 is restarted, with nothing in the log
        but a bare traceback.  Config is explicitly meant to be hand-edited
        (see the header of orc1_config.py), so validate it here instead.
        """
        if not isinstance(action, (tuple, list)) or not action:
            print(f"  ! Mixtrack config: {label} action must be a non-empty "
                  f"tuple, got {action!r} -- ignored")
            return False
        if not isinstance(action[0], str):
            print(f"  ! Mixtrack config: {label} action name must be a "
                  f"string, got {action[0]!r} -- ignored")
            return False
        return True

    def _resolve_button_map(self, name_map):
        """{symbolic_name: action} → {midi_note: action}. Skips bad entries."""
        out = {}
        for name, action in name_map.items():
            if not action:
                continue   # None / falsy = disabled
            num = _NOTE_BY_NAME.get(name)
            if num is None:
                print(f"  ! Mixtrack config: unknown button name '{name}'")
                continue
            if not self._well_formed(action, f"button '{name}'"):
                continue
            out[num] = action
        return out

    def _resolve_cc_map(self, name_map):
        out = {}
        for name, action in name_map.items():
            if not action:
                continue
            num = _CC_BY_NAME.get(name)
            if num is None:
                print(f"  ! Mixtrack config: unknown CC name '{name}'")
                continue
            if not self._well_formed(action, f"CC '{name}'"):
                continue
            out[num] = action
        return out

    # ── LED feedback output ───────────────────────────────────────────────

    def _open_output(self, prefix):
        prefix = (prefix or "mixtrack").lower()
        try:
            for n in mido.get_output_names():
                if prefix in n.lower():
                    self._out = mido.open_output(n)
                    print(f"  ✓ Mixtrack LED output: {n}")
                    return
        except Exception as e:
            print(f"  ! Mixtrack LED output unavailable: {e}")
        self._out = None

    def _led(self, note, deck, on=True, vel=127):
        """Light (on) or clear (off) one button/pad LED. deck = CH_DECK1/CH_DECK2."""
        if not self._out:
            return
        try:
            with self._out_lock:
                self._out.send(mido.Message(
                    "note_on", channel=deck - 1, note=note,
                    velocity=(vel if on else 0)))
        except Exception:
            pass

    def all_leds(self, on):
        """Set every known button/pad LED on both decks on or off."""
        for note in sorted(set(_NOTE_BY_NAME.values())):
            for deck in (CH_DECK1, CH_DECK2):
                self._led(note, deck, on)

    def all_off(self):
        self.all_leds(False)

    def wake(self):
        """Boot flourish so the controller visibly comes alive, then settle
        into the standard deck lighting."""
        if not self._out:
            return
        for note in sorted(set(_NOTE_BY_NAME.values())):   # quick sweep up
            for deck in (CH_DECK1, CH_DECK2):
                self._led(note, deck, True)
            time.sleep(0.008)
        time.sleep(0.2)
        self.all_leds(False)                               # clear
        # ...which blanked FX1-3 too: forget what the ring poller last sent,
        # or its change-only cache still says "lit" and they stay dark (seen
        # after a DJ-mode reclaim). Not yet built during __init__'s wake.
        cache = getattr(self, "_fx_led_state", None)
        if cache is not None:
            cache.clear()
        for deck in (CH_DECK1, CH_DECK2):
            self._refresh_mode_leds(deck)
        print("  ✓ Mixtrack awake (LEDs lit)")

    def _grab_pad_color(self, deck):
        """Top-row colour: BLUE = forward capture, PURPLE = retroactive."""
        return LED_BLUE if self._fwd_mode.get(deck, False) else LED_PURPLE

    def _refresh_mode_leds(self, deck):
        """Repaint one deck's LEDs.  Every deck is a sampler deck now, so
        there is one scheme: grab pads (blue/purple by mode), yellow hotcues,
        transport lit."""
        if not self._out:
            return
        # This unit's TOP ROW pads send different notes depending on the
        # hardware PAD MODE bank (verified by MIDI capture 2026-07-27): loop
        # (0x13-0x16), sampler (0x20-0x23), or pad (0x17-0x1a). We can't read
        # which bank is active, so light ALL of them -- the pad only lights on
        # the note it currently sends, the rest are harmless no-ops.
        # Top row = grab 1/2/4/8 bars. BLUE when this deck is in FORWARD mode,
        # PURPLE for the default retroactive grab -- the colour tells you which
        # direction a top-pad press will capture.
        grab_col = self._grab_pad_color(deck)
        for n in (NOTE_LOOP_IN, NOTE_LOOP_OUT, NOTE_LOOP_ON, NOTE_LOOP_HALVE,
                  NOTE_SAMPLER1, NOTE_SAMPLER2, NOTE_SAMPLER3, NOTE_SAMPLER4,
                  NOTE_PAD1, NOTE_PAD2, NOTE_PAD3, NOTE_PAD4):
            self._led(n, deck, True, grab_col)        # grab 1/2/4/8 bars
        self._paint_hotcue_leds(deck)                 # armed = yellow, empty = off
        self._led(NOTE_PLAY, deck, True)              # transport
        self._led(NOTE_CUE, deck, True)
        self._led(NOTE_SYNC, deck, True)              # reverse

    _HOTCUE_NOTES = (NOTE_HOTCUE1, NOTE_HOTCUE2, NOTE_HOTCUE3, NOTE_HOTCUE4)

    def _paint_hotcue_leds(self, deck):
        """Light the 4 hotcue pads from the LED mirror: ARMED = yellow (bright),
        EMPTY = off.  This is the only place hotcue pads are painted, so the
        set/unset state is always visible and never over-painted by the static
        yellow the mode refresh used to force."""
        if not self._out:
            return
        armed = self._cue_set.get(deck, (False,) * 4)
        for idx, n in enumerate(self._HOTCUE_NOTES):
            if armed[idx]:
                self._led(n, deck, True, LED_YELLOW)
            else:
                self._led(n, deck, False)             # empty pad = dark

    def _reset_hotcue_leds(self, deck):
        """A fresh grab / file load wipes the deck's cues (the synth does the
        same), so forget them here and repaint the pads dark.  No-op for a
        non-deck channel (master LOAD buttons route through the grab handler
        with deck=CH_MASTER, which owns no hotcue pads)."""
        if deck not in self._cue_set:
            return
        self._cue_set[deck] = [False, False, False, False]
        self._paint_hotcue_leds(deck)

    def _fx_rack(self):
        """The engine's 3-slot FX rack (RackManager) or None if unavailable."""
        return getattr(getattr(self.eng, "fx", None), "rack", None)

    def _render_fx_leds(self):
        """Light FX1-3 on each deck when the matching RACK slot (0/1/2) is
        running.  Both decks mirror the SAME 3 global slots -- the rack is one
        global series insert, not per-deck.  Safe off-thread."""
        if not self._out:
            return
        rack = self._fx_rack()
        if rack is None:
            return
        try:
            slots = rack.status().get("slots", [])
        except Exception:
            return
        fx_notes = (NOTE_FX1, NOTE_FX2, NOTE_FX3)
        for deck in (CH_DECK1, CH_DECK2):
            for i, note in enumerate(fx_notes):
                on = bool(i < len(slots) and slots[i].get("running"))
                key = (deck, note)
                if self._fx_led_state.get(key) != on:   # only send changes
                    self._fx_led_state[key] = on
                    self._led(note, deck, on)

    # ── RING buttons (2026-09-23): FX1/2/3 = this deck's skipback ring ────
    # The FX rack was parked (never worked right), so FX1-3 on each deck now
    # pick which skipback RING that sampler deck grabs from: FXn = the n-th
    # ARMED ring by id (FX1 = MAIN). LED lit = the deck's current ring; a
    # button with no ring behind it stays dark. SHIFT+FXn saves the last N s
    # of that ring to ~/recordings (LED blinks until the file is written).
    _RING_NOTES = (NOTE_FX1, NOTE_FX2, NOTE_FX3)

    def _render_ring_leds(self):
        """Paint FX1-3 on both decks from the LIVE state: the armed rings and
        each deck's grab source (read from the file the sampler host keeps, so
        a web pick shows here too). Only changes are sent. Safe off-thread."""
        if not self._out:
            return
        rings = self.eng.skipback_rings()
        for deck in (CH_DECK1, CH_DECK2):
            cur = self.eng.sampler_deck_source(self._DECK_LETTER[deck])
            for i, note in enumerate(self._RING_NOTES):
                if (deck, note) in self._ring_saving:
                    continue                          # the blink owns this LED
                on = i < len(rings) and rings[i]["id"] == cur
                key = (deck, note)
                if self._fx_led_state.get(key) != on:
                    self._fx_led_state[key] = on
                    self._led(note, deck, on)

    def _act_ring_button(self, msg, deck, which, slot):
        """FXn: point this deck's GRAB at the n-th armed ring. SHIFT+FXn: save
        the last N seconds of that ring to ~/recordings. A slot with no ring
        is a no-op (its LED is dark)."""
        if not self._is_press(msg):
            return
        try:
            idx = int(slot) - 1
        except (TypeError, ValueError):
            return
        rings = self.eng.skipback_rings()
        if not (0 <= idx < len(rings)):
            return
        tid = rings[idx]["id"]
        if self._shift.get(deck, False):
            self._ring_save(deck, msg.note, tid, rings[idx]["name"])
            return
        self.eng.sampler_set_source(which, tid)
        # The host persists the choice a few ms later; paint NOW from what we
        # just set so the press feels instant, then the poller keeps it true.
        for i, note in enumerate(self._RING_NOTES):
            on = i == idx
            if (deck, note) not in self._ring_saving:
                self._fx_led_state[(deck, note)] = on
                self._led(note, deck, on)
        print(f"  [MT] deck {which} grabs from ring t{tid} "
              f"'{rings[idx]['name']}' (FX{idx + 1})")

    def _ring_save(self, deck, note, tid, name):
        """SHIFT+FXn: save the ring's last N s; blink the button until done."""
        cfg = getattr(self.eng, "cfg", None)
        secs = int(getattr(cfg, "MIXTRACK_RING_SAVE_SECS", 60) or 60)
        key = (deck, note)
        if key in self._ring_saving:
            return                                    # already saving this one
        self._ring_saving[key] = True
        print(f"  [MT] saving last {secs}s of ring t{tid} '{name}'...")

        def _blink():
            on = True
            while key in self._ring_saving:
                self._led(note, deck, on)
                on = not on
                time.sleep(0.2)

        def _done(ok, fname):
            self._ring_saving.pop(key, None)
            self._fx_led_state.pop(key, None)         # force a repaint
            self._render_ring_leds()
        threading.Thread(target=_blink, daemon=True, name="mt-ringsave").start()
        self.eng.skipback_save(tid, secs, done=_done)

    def _fx_led_loop(self):
        time.sleep(3.0)                    # let boot lighting settle first
        while True:
            try:
                self._render_ring_leds()   # FX1-3 are RING buttons now
            except Exception:
                pass
            time.sleep(2.5)

    def _safe_bpm(self):
        """Live tempo for the metronome LED; 120 fallback if the engine can't
        answer (never let a bad bpm stall the tick loop)."""
        try:
            return float(self.eng.sampler_bpm())
        except Exception:
            return 120.0

    def _safe_beat_phase(self):
        """(since, beat_s) locked to the live clock grid, or None when no clock
        is running / the engine can't answer. Never raises into the tick loop.
        When present it drives the TAP LED phase-locked (jitter-proof); when
        None the loop free-runs off _safe_bpm()."""
        try:
            return self.eng.sampler_beat_phase()
        except Exception:
            return None

    def _led_tick_loop(self):
        """Drive the TAP metronome flash and any armed forward-capture pulse."""
        time.sleep(3.0)                    # after the boot flourish
        while True:
            try:
                self._tick_leds()
            except Exception:
                pass
            time.sleep(_LED_TICK_S)

    def _tick_leds(self):
        if not self._out:
            return
        now = time.monotonic()
        # TAP metronome: flash both decks' TAP LED at the start of each beat.
        # Prefer the live clock grid -- its beat boundary is anchored to a real
        # clock pulse, so it's immune to the per-pulse jitter that made the free
        # -running tick flutter wildly under external sync. Fall back to the
        # free-running tick (off the BPM estimate) only when no clock is running.
        bp = self._safe_beat_phase()
        if bp is not None:
            on = bp[0] < _TEMPO_FLASH_S
        else:
            on = _tempo_led_on(now, self._tempo_phase, self._safe_bpm())
        if on != self._tap_led_on:
            self._tap_led_on = on
            for deck in (CH_DECK1, CH_DECK2):
                self._led(NOTE_TAP, deck, on)
        # Forward-capture arm pulse: blink the pad while a grab is armed.
        for deck in (CH_DECK1, CH_DECK2):
            note = self._fwd_arm.get(deck)
            if note is not None:
                self._led(note, deck, _arm_blink_on(now), LED_BLUE)

    def _flash_pad(self, note, deck, ms=180):
        """Capture acknowledgement: blink the pressed pad bright, then repaint
        the deck's mode lighting. Fire-and-forget timer thread."""
        if not self._out:
            return
        self._led(note, deck, True, 127)
        t = threading.Timer(ms / 1000.0,
                            lambda: self._refresh_mode_leds(deck))
        t.daemon = True
        t.start()

    # ── Main entry point ──────────────────────────────────────────────────

    def handle_msg(self, msg):
        """Route a MIDI message. Called from the Mixtrack's input thread."""
        if not hasattr(msg, 'channel'):
            return   # sysex / clock — ignore

        ch = msg.channel + 1
        if ch == CH_MASTER:
            self._handle_master(msg)
        elif ch in (CH_DECK1, CH_DECK2):
            self._handle_deck(msg, ch)
        # Other channels: Mixtrack shouldn't send any. Drop silently.

    def _handle_master(self, msg):
        """Master section dispatch — crossfader / master gain (CCs) and the
        LOAD buttons (notes)."""
        if msg.type == 'control_change':
            action = self._master_ccs.get(msg.control)
            if action:
                self._dispatch_cc(action, msg, CH_MASTER)
        elif msg.type in ('note_on', 'note_off'):
            action = self._master_buttons.get(msg.note)
            if action:
                self._dispatch_button(action, msg, CH_MASTER)

    # Notes that are MODIFIERS rather than actions.  Dispatch no longer
    # special-cases them -- there is only one map per deck since chord mode was
    # removed, so there is no layer for a SHIFT release to get eaten by.  Kept
    # because it is the honest list of "these two are not ordinary controls",
    # and _test_mixtrack.py's reachability sweep skips them by it.
    _MODIFIER_NOTES = frozenset({NOTE_SHIFT, NOTE_JOG_TOUCH})

    def _handle_deck(self, msg, deck):
        """Dispatch a deck message against that deck's sampler maps.

        There is no mode dimension and no SHIFT map any more: SHIFT is a
        per-action MODIFIER that individual handlers read via self._shift
        (x4 coarse scrub, x5 browse, SHIFT+touchstrip -> FX param).  An
        exclusive SHIFT layer is what silently disabled all 36 of deck B's
        controls until 2026-08-01 -- see the note in orc1_config.py.
        """
        btns, ccs = self._maps[deck]
        if msg.type in ('note_on', 'note_off'):
            action = btns.get(msg.note)
            if action:
                self._dispatch_button(action, msg, deck)
            # No fallthrough — consumed.
        elif msg.type == 'control_change':
            action = ccs.get(msg.control)
            if action:
                self._dispatch_cc(action, msg, deck)

    # ── Action dispatch (string → method via getattr) ─────────────────────

    def _dispatch(self, prefix, action, msg, deck, kind):
        """Resolve and run one action.

        ⚠ THE NAME LOOKUP IS INSIDE THE try, DELIBERATELY.  `action[0]` and
        `"_act_" + name` used to sit outside it, so a malformed config entry
        raised a TypeError that escaped handle_msg into the controller's input
        thread -- which has no try/except of its own.  The thread died, the
        MIDI port stayed open so the hotplug scanner never noticed it was
        gone, and the whole controller was dead until orc1 was restarted, with
        nothing in the journal but a bare traceback.  _well_formed() rejects
        such entries at load time; this is the backstop for any other path.
        """
        try:
            name = action[0]
            method = getattr(self, prefix + name, None)
            if method:
                method(msg, deck, *action[1:])
                return
        except Exception as e:
            print(f"  ! Mixtrack action {action!r} failed: {e}")
            return
        _unresolved(prefix, name, kind)

    def _dispatch_button(self, action, msg, deck):
        self._dispatch("_act_", action, msg, deck, "button")

    def _dispatch_cc(self, action, msg, deck):
        self._dispatch("_cc_", action, msg, deck, "cc")

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _is_press(msg):
        """True for a note-on with velocity > 0."""
        return msg.type == 'note_on' and msg.velocity > 0

    # ══════════════════════════════════════════════════════════════════════
    # BUTTON ACTIONS  (_act_<name> — called as method(msg, deck, *args))
    # ══════════════════════════════════════════════════════════════════════

    def _act_consume(self, msg, deck, *args):
        """No-op button action: swallow the input without doing anything.
        Used by the (now inert) Deck-2 SHIFT layer so holding SHIFT keeps that
        layer exclusive — inputs are consumed, no base-map action leaks through."""
        return

    def _act_tap_tempo(self, msg, deck):
        if not self._is_press(msg):
            return
        self.eng.tap_tempo()
        # Anchor the metronome LED to this tap so the flash lands on the beat
        # the player is tapping (see _tempo_led_on).
        self._tempo_phase = time.monotonic()

    # ── Modifiers ─────────────────────────────────────────────────────────

    def _act_mixtrack_shift(self, msg, deck):
        """
        Toggle Mixtrack-local SHIFT for this deck.
        Strictly local — does NOT touch engine state["func_held"], so it
        can't bleed into BSP/MPK function-key behavior.

        Purely a modifier flag: individual handlers read it to widen their
        step (x4 coarse scrub, x5 browse) or to pick a different target
        (SHIFT+touchstrip -> FX param).  It is never a map layer.
        """
        self._shift[deck] = self._is_press(msg)

    def _act_jog_touch(self, msg, deck):
        """Capacitive platter touch -> vinyl SCRATCH on that deck.

        A hand on the platter brakes the record (pitch falls to a stop);
        letting go lets the motor spin it back up. The deck owns that
        physics -- see SampleDeck.set_scratch."""
        self.eng.sampler_scratch(self._DECK_LETTER[deck],
                                 self._is_press(msg))

    # ── Sampler forwarders (thin — ALL logic lives in the engine facade) ──

    def _act_sample_capture(self, msg, deck, which, seconds=None):
        if not self._is_press(msg):
            return
        self.eng.sampler_capture(which, int(seconds) if seconds else None)
        self._reset_hotcue_leds(deck)                  # new audio -> cues wiped
        self._flash_pad(msg.note, deck)                # capture ack
        if self.cfg.DEBUG:
            print(f"  [MT] sampler {which}: capture "
                  f"{seconds or 'default'}s")

    def _act_sample_capture_bars(self, msg, deck, which, bars):
        """Top-row grab, direction depends on the deck's mode:

        RETRO (default)   -- last N bars at the live BPM, loops immediately
                             (fires on PRESS; release ignored).
        FORWARD (fwd_mode  -- press marks the DOWNBEAT, release finalises. The
          XOR SHIFT)         engine reads the press duration to pick TAP (a
                             quick tap -> fixed N bars) vs HOLD (held -> record
                             to release, end snapped to a bar).

        The retro/forward choice is LATCHED at press (self._fwd_gesture) so
        releasing SHIFT between the press and its release can't hand the two
        halves to different code paths."""
        if self._is_press(msg):
            # .get(): the master LOAD buttons also route here with deck=CH_MASTER,
            # which is not in the per-deck dicts -- those grabs stay retroactive.
            use_forward = self._fwd_mode.get(deck, False) ^ self._shift.get(deck, False)
            self._fwd_gesture[deck] = (use_forward, which, float(bars))
            if not use_forward:
                self.eng.sampler_capture_bars(which, float(bars))
                self._reset_hotcue_leds(deck)          # new audio -> cues wiped
                self._flash_pad(msg.note, deck)        # capture ack
                print(f"  [MT] sampler {which}: loop-grab {bars} bars "
                      f"@ {self.eng.sampler_bpm():.1f} bpm")
            else:
                self.eng.sampler_capture_fwd_hold_start(which)
                self._fwd_arm[deck] = msg.note         # pulse this pad while held
                print(f"  [MT] sampler {which}: FWD arm (pad={bars})")
        else:
            g = self._fwd_gesture.pop(deck, None)
            if g and g[0]:                             # a forward press is open
                _use, which_p, bars_p = g
                self.eng.sampler_capture_fwd_hold_end(which_p, bars_p)
                # the new buffer lands N bars later and wipes the deck's cues;
                # dark the pads now, at the commit -- they belong to audio that
                # is on its way out.
                self._reset_hotcue_leds(deck)
                # stop the arm blink and flash a bright 'committed' ack
                armed = self._fwd_arm.pop(deck, None)
                if armed is not None:
                    self._flash_pad(armed, deck)

    def _act_mixtrack_fwd_mode(self, msg, deck):
        """Toggle this deck's top-row capture DIRECTION (retro <-> forward).
        The persistent half of the 'Both' toggle; SHIFT+pad still gives a
        one-off invert without flipping this. Cancels any forward grab armed on
        the deck so a mode flip can't strand a half-open hold. LED reflect is
        slice 4."""
        if not self._is_press(msg):
            return
        self._fwd_gesture.pop(deck, None)
        self._fwd_arm.pop(deck, None)             # drop any in-progress arm blink
        self.eng.sampler_capture_fwd_cancel(self._DECK_LETTER[deck])
        self._fwd_mode[deck] = not self._fwd_mode[deck]
        self._refresh_mode_leds(deck)             # repaint top row in the new tint
        print(f"  [MT] deck {self._DECK_LETTER[deck]} capture -> "
              f"{'FORWARD' if self._fwd_mode[deck] else 'RETRO'}")

    def _act_sample_loop_toggle(self, msg, deck, which):
        if not self._is_press(msg):
            return
        self.eng.sampler_loop_toggle(which)

    def _act_sample_reverse(self, msg, deck, which):
        """SYNC in sampler mode: run the record backwards (latching)."""
        if not self._is_press(msg):
            return
        self.eng.sampler_reverse(which)
        self._flash_pad(msg.note, deck)

    def _act_sample_export(self, msg, deck, which):
        """Save the deck's capture to the samples folder (unmapped by
        default — bind it to any spare button in the sampler maps)."""
        if not self._is_press(msg):
            return
        self.eng.sampler_export(which)
        self._flash_pad(msg.note, deck)

    def _act_sample_gate(self, msg, deck, which):
        self.eng.sampler_gate(which, self._is_press(msg))

    def _act_sample_stop(self, msg, deck, which):
        """Hard STOP + rewind (the big CUE button in sampler mode)."""
        if not self._is_press(msg):
            return
        self.eng.sampler_stop(which)

    def _act_sample_cue(self, msg, deck, which, index):
        """Settable hotcue.  Bare press = SET the cue at the live playhead if
        empty, else JUMP to it (the synth deck decides).  SHIFT+press = clear
        it.  SHIFT is a per-action modifier read here, NOT a map layer -- an
        exclusive SHIFT layer is what silently killed deck B in the past."""
        if not self._is_press(msg):
            return
        try:
            i = int(index)
        except (TypeError, ValueError):
            return
        if not (1 <= i <= 4):
            return
        if self._shift.get(deck, False):
            self.eng.sampler_cue_clear(which, i)
            self._cue_set[deck][i - 1] = False
        else:
            self.eng.sampler_cue(which, i)
            # After any bare press the pad is armed: it was either just set, or
            # it was already set and we jumped to it.  Either way -> lit.
            self._cue_set[deck][i - 1] = True
        self._paint_hotcue_leds(deck)

    # ── Top section (2026-07-07): FX slots, bend, file browse/load ────────

    def _act_fx_toggle_slot(self, msg, deck, slot):
        """FX1-3: toggle rack slot 0/1/2 on/off (eng.fx.rack).  Both decks reach
        the same 3 global slots.  Also marks that slot as the target for the
        SHIFT+touchstrip parameter sweep."""
        if not self._is_press(msg):
            return
        rack = self._fx_rack()
        if rack is None:
            return
        try:
            i = int(slot)
        except (TypeError, ValueError):
            return
        if not (0 <= i < 3):             # structural hard cap (N_SLOTS)
            return
        self._rack_strip_slot = i        # SHIFT+strip now sweeps this slot
        # done-callback repaints the FX LEDs the moment the slot is actually
        # up/down (a jalv (re)build takes 1-4s).
        try:
            rack.toggle(i, done=self._render_fx_leds)
        except Exception as e:
            print(f"  ! FX rack toggle failed: {e}")
            return
        print(f"  [MT] FX rack slot {i + 1}: toggle")

    def _cc_fx_rack_param(self, msg, deck):
        """SHIFT+touchstrip: sweep the FIRST parameter of the active rack slot
        (the last FX button pressed).  Strip 0-127 -> control min..max.  No-op
        if that slot is empty or the plugin has no parameters (e.g. a fixed
        console/dither effect)."""
        rack = self._fx_rack()
        if rack is None:
            return
        slot  = self._rack_strip_slot   # always set in __init__
        slots = rack.status().get("slots", [])
        if slot >= len(slots):
            return
        ctrls = slots[slot].get("controls", [])
        if not ctrls:
            return
        c     = ctrls[0]
        p_min = float(c.get("min", 0.0))
        p_max = float(c.get("max", 1.0))
        val   = p_min + (p_max - p_min) * (msg.value / 127.0)
        rack.param(slot, c["symbol"], val)

    def _act_sample_pitch_bend(self, msg, deck, which, updown):
        """PITCH +/-: momentary beatmatch nudge — engaged while held."""
        self.eng.sampler_pitch_bend(which, updown == "up",
                                    self._is_press(msg))

    def _act_sample_load_browsed(self, msg, deck, which):
        """WHEEL button: load the browsed file onto this deck (cued)."""
        if not self._is_press(msg):
            return
        self.eng.sampler_load_browsed(which)
        self._reset_hotcue_leds(deck)                  # new audio -> cues wiped
        self._flash_pad(msg.note, deck)

    # ══════════════════════════════════════════════════════════════════════
    # CC ACTIONS  (_cc_<name> — called as method(msg, deck, *args))
    # ══════════════════════════════════════════════════════════════════════

    def _rel(self, value):
        """Two's-complement relative decode for the Mixtrack's encoders:
        1..63 = +1..+63, 127..65 = -1..-63, 0/64 = 0. The Mixtrack emits small
        values (1-6) for gentle motion; the shared engine decoder defaults to
        'offset' mode (for a legacy Arturia BSP not in this rig), which misreads
        value 1 as -63 -- turning a light jog touch into a max-speed runaway.
        Decode locally so this controller is correct regardless of the global
        RELATIVE_CC_MODE (leaves the legacy core LEAD_VOICE path untouched)."""
        if value == 0 or value == 64:
            return 0
        return value if value < 64 else value - 128

    def _cc_consume(self, msg, deck):
        """Explicit no-op for controls you want to swallow silently."""
        return

    # ── Sampler CC forwarders (thin — engine facade does everything) ──────

    def _cc_sample_scrub(self, msg, deck, which):
        step = self._rel(msg.value)
        if self._shift.get(deck, False):
            step *= 4                     # coarse scrub with SHIFT held
        self.eng.sampler_scrub(which, step)

    def _cc_sample_speed(self, msg, deck, which):
        self.eng.sampler_speed(which, msg.value)

    def _cc_sample_gain(self, msg, deck, which):
        self.eng.sampler_gain(which, msg.value)

    def _cc_sample_eq(self, msg, deck, which, band):
        self.eng.sampler_eq(which, band, msg.value)

    def _cc_sample_filter(self, msg, deck, which):
        self.eng.sampler_filter(which, msg.value)

    def _cc_sample_crossfade(self, msg, deck):
        self.eng.sampler_crossfade(msg.value)

    def _cc_sample_browse(self, msg, deck, which):
        """Top encoder: scroll this deck's file-browser selection. SHIFT
        held = jump 5 files per tick for long lists."""
        step = self._rel(msg.value)
        if self._shift.get(deck, False):
            step *= 5
        if step:
            self.eng.sampler_browse(which, step)

    def _cc_sample_seek(self, msg, deck, which):
        """Touchstrip: needle-drop — absolute strip position -> jump to that
        fraction of the deck's buffer and play (declicked deck-side)."""
        self.eng.sampler_seek(which, msg.value)

    # ── Master ────────────────────────────────────────────────────────────

    def _cc_smart_crossfader(self, msg, deck):
        """Master crossfader: blend deck A against deck B."""
        self.eng.sampler_crossfade(msg.value)

    def _cc_sampler_master_gain(self, msg, deck):
        """Master GAIN knob -> overall sampler output level (both decks)."""
        self.eng.sampler_master_gain(msg.value)
