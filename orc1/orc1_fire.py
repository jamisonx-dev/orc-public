#!/usr/bin/env python3
"""
ORC-1 Fire Handler  v2
Akai Fire (FL STUDIO FIRE) -- control surface + step sequencer for ORC-1.

Mirrors the orc1_mixtrack.py pattern: hardware constants + action
implementations live here; what each control DOES is set by the FIRE_* dicts
in orc1_config.py. Construction/threading parallel the Mixtrack:

    from orc1_fire import FireHandler
    fire = FireHandler(engine_module)
    # input thread calls fire.handle_msg(msg)
    # orc1.py clock hook calls fire.clock_tick() per MIDI clock pulse

VERIFIED HARDWARE MAP (captured on-device with fire_mapper.py):
  Pads        note_on 54..117, 4x16 row-major.  pad(r,c)=54+r*16+c
  4 dials     relative CC 16/17/18/19 (1=+1 .. 127=-1); touch = note_on 16..19
  SELECT      turn = relative CC 118;  press = note_on 25
  Transport   PLAY 51  STOP 52  REC 53
  Modes       STEP 44  NOTE 45  DRUM 46  PERFORM 47
  Modifiers   SHIFT 48  ALT 49
  Misc        PAT/SONG 50   MODE(chan/mix/user) 26   BROWSER 33
  Nav         PATTERN+ 31  PATTERN- 32   GRID< 34  GRID> 35
  Row mutes   MUTE1..4 = 36..39  (one per pad row)

SEQUENCER
  M tracks, each with its OWN step length (1..MAX_LEN) -> true polyrhythms.
  A 4-row x 16-col viewport scrolls vertically (tracks) and pages
  horizontally (steps > 16). Every track advances one step per 1/16 clock and
  wraps at its own length. Drives ORC-1's internal synth by default
  (send_synth_midi), or any port/channel if a track sets an external dest.

CLOCK INDEPENDENCE
  Own internal step thread at the engine BPM (works with no external clock).
  When the arp clock mode is EXTERNAL it advances on incoming pulses instead
  (6 per 1/16 step). One coherent tempo either way.

VIEWS (MODE button cycles; STEP/PERFORM jump directly)
  seq        step grid (default)                      [was "channel"]
  mixer      dials = per-track velocity for the visible rows; pads show bars
  chord      chromatic chord surface into the engine   [was "perform"]
  chip       NES/GB chip voices
  poly       chromatic keyboard into the internal poly synth   [was "user2"]
  faders    MPC1000-style channel mixer -- per-row fader assign (BUILT).
             (2026-09-02) being retired into a "fader element" droppable onto
             the universal surface -- see _planning/ORC1_Fire_Reorg_Spec.md
  universal  universal control surface (BUILT) -- banked pad grid, per-pad
             note/CC/PC assign with SHIFT/ALT sub-layers, on-device + web
             authoring, persisted. Slated to become the boot default; see
             the reorg spec above.
  fx         FX chain view -- folded into the MODE cycle as an 8th mode
             (Session E); PAT_SONG remains a dedicated quick-jump into it,
             SHIFT/ALT+PAT_SONG unaffected (tap tempo / clock-out toggle).

Session E (2026-06-30) renamed the first three view strings for clarity and
added faders/universal/fx to the MODE cycle. See SESSION_E_FIRE_PLAN.md and
SESSION_E_BREAKDOWN_PREP.md for the full spec and as-built notes.
"""

import time
import threading
import os
import json
import colorsys   # smooth hue sweep for the ALT+knob2 track-colour dial

try:
    import mido
except Exception:
    mido = None

try:
    import fire_oled
except Exception:
    fire_oled = None


# ==============================================================================
# HARDWARE CONSTANTS -- verified on-device, do not change
# ==============================================================================

PAD_NOTE_BASE = 54
PAD_NOTE_MAX  = 117
N_ROWS, N_COLS = 4, 16

DIAL_CC         = {16: 0, 17: 1, 18: 2, 19: 3}
DIAL_TOUCH_NOTE = {16, 17, 18, 19}
SELECT_CC       = 118

BUTTON_NOTE = {
    "STEP": 44, "NOTE": 45, "DRUM": 46, "PERFORM": 47,
    "SHIFT": 48, "ALT": 49, "PAT_SONG": 50,
    "PLAY": 51, "STOP": 52, "REC": 53,
    "PATTERN_UP": 31, "PATTERN_DOWN": 32, "BROWSER": 33,
    "GRID_LEFT": 34, "GRID_RIGHT": 35,
    "SELECT_PRESS": 25, "MODE": 26,
    "MUTE1": 36, "MUTE2": 37, "MUTE3": 38, "MUTE4": 39,
}
MUTE_ROW_NOTE = {36: 0, 37: 1, 38: 2, 39: 3}

PULSES_PER_STEP = 6        # 24 PPQN -> 1/16 step
MAX_LEN = 64
MAX_TRACKS = 32            # ceiling for live-added tracks (viewport scrolls)

# Session E (2026-06-30): renamed channel->seq, perform->chord, user2->poly;
# added faders, universal, fx as full MODE-cycle modes (8 total). fx keeps
# its existing PAT_SONG quick-jump button in addition to being in the cycle.
VIEWS = ["seq", "mixer", "chord", "chip", "poly", "faders", "universal", "fx"]

# NES P1+P2 coupling modes, in cycle order. Fire and orc1_synth.py are
# separate processes with no shared Python object (see the coupling-mode
# bugfix writeup), so Fire keeps its OWN copy of this list to drive the
# local mode it THINKS is active, then sends the result out as CC72 for
# the synth to actually apply. Must stay in sync with NES_COUPLE_MODES in
# orc1_synth.py by hand -- there's no way to share this list across the
# process boundary without adding IPC just for a constant.
CHIP_COUPLE_MODES = ['none', 'unison', 'detune', 'fifth', 'third', 'octave', 'octave_dn']

# Game Boy CH3 wave-channel presets, in the same CC1 band order the synth
# decodes them (GameboyWaveVoice.set_cc: idx = min(7, value // 16)).  Used to
# name the current waveform on the OLED when the wave voice is selected, so a
# knob turn lands on a *named* preset instead of an anonymous sweep.
GB_WAVE_NAMES = ['TRI', 'SINE', 'SAWUP', 'SAWDN', 'SQ', 'PLS25', '2xSAW', 'WARM']
# Hardware 4-step volume labels, matching GameboyWaveVoice (min(3, v*4//128)).
GB_VOL_LABELS = ['MUTE', '25%', '50%', '100%']

# -- Colours (0..127 per channel) ----------------------------------------------
OFF      = (0, 0, 0)
PLAYHEAD = (0x40, 0x40, 0x40)
ACCENT_BOOST = 0x28
TRACK_COLOR = [
    (0x40, 0x00, 0x00), (0x00, 0x40, 0x00), (0x00, 0x00, 0x40),
    (0x40, 0x20, 0x00), (0x30, 0x00, 0x30), (0x00, 0x30, 0x30),
    (0x30, 0x30, 0x00), (0x18, 0x18, 0x18),
]


# Session E (2026-06-30) BROWSER context menu items, per mode: (label, key)
# where key resolves to a _menuact_<key> method on FireHandler. "Browse Track
# Params" on seq/mixer preserves the pre-Session-E plain-BROWSER overlay
# entry that the plan's own menu spec never re-homed anywhere (see the long
# comment above _act_browser's menu-item methods). faders/universal/fx items
# reference features from later steps (13/16/17/10/12/18) not yet built --
# their _menuact_ methods show an honest "not wired yet" OLED flash.
_MENU_ITEMS = {
    "seq": [
        ("Browse Params", "browse_params"),
        ("Save Pattern",        "save_pattern"),
        ("Clear Steps",     "clear_all_steps"),
        ("Add Track",           "add_track"),
        ("Remap Encoders",      "remap_encoders"),
    ],
    "mixer": [
        ("Browse Params", "browse_params"),
        ("Remap Encoders",      "remap_encoders"),
        ("Save State",          "save_state"),
    ],
    "chord": [
        ("Configure Rows",       "configure_rows"),
        ("Cycle Scale",          "cycle_scale"),
        ("Remap Encoders",       "remap_encoders"),
        ("Def. Voicing",  "set_default_voicing"),
        ("Save State",           "save_state"),
    ],
    "chip": [
        ("Remap Encoders",     "remap_encoders"),
        ("Coupling Mode",  "set_coupling_mode"),
        ("Detune Amount",  "set_detune_amount"),
        ("Save State",         "save_state"),
    ],
    "poly": [
        ("Remap Encoders",        "remap_encoders"),
        ("Def. Waveform",  "set_default_waveform"),
        ("Save State",            "save_state"),
    ],
    "faders": [
        ("Clear Fader",   "mpc_clear_fader"),
        ("Clear Faders",   "mpc_clear_all"),
        ("Save Faders",  "preset_save_faders"),
        ("Load Faders",  "preset_load_faders"),
        ("Remap Encoders",     "remap_encoders"),
        ("Save State",         "save_state"),
    ],
    "universal": [
        ("Toggle Latch",       "universal_toggle_latch"),
        ("Clear This Pad",            "universal_clear_pad"),
        ("Clear Bank",           "universal_clear_pads"),
        ("Save Pads",           "preset_save_ucs"),
        ("Load Pads",           "preset_load_ucs"),
        ("Save Knobs",          "preset_save_knobs"),
        ("Load Knobs",          "preset_load_knobs"),
        ("Remap Encoders",            "remap_encoders"),
        ("Save State",                "save_state"),
    ],
    "fx": [
        ("Remap Encoders", "remap_encoders"),
        ("Save State",     "save_state"),
    ],
}

# Session E (2026-06-30) subscreen mode-jump row (row 3, cols 8-15) -- all 8 modes.
# Chord type names in cycle order (mirrors CHORD_BASE_NAMES in orc1.py).
_CHORD_BASE_NAMES = ["MAJ", "MIN", "SUS", "DIM"]
_PRESETS_FILE     = "~/fire_presets.json"   # pad / fader / knob preset banks
# NOT expanduser()'d here.  A module-level absolute path is baked in at
# IMPORT time, before any test can redirect HOME -- so it defeats even a
# correctly sandboxed test and writes the live rig's file.  That is the same
# failure that destroyed ~/fire_knobmap.json on 2026-07-31; keep every
# "~"-relative path a raw string and expand it at the point of use.
# _test_fire.py::test_no_import_time_home_paths enforces this.
_CHORD_ROWS_FILE  = "~/.orc1_chord_rows.json"
# How many row/routing BANKS the chord surface cycles through with GRID <>.
# NOT a designed-in limit -- raise it freely; each unused bank is just a copy of
# the defaults and costs a few bytes of JSON. Every bank auto-persists the moment
# it is edited and reloads on boot, so this is "save/reload pages", not a fixed
# page count.
_CHORD_N_BANKS = 8
# Rows PER BANK. The Fire shows a 4-row (N_ROWS) window into this taller stack;
# SELECT-turn on the chord screen scrolls the window. Rows past the first four
# default to "off" (silent) until you configure them, so a fresh bank sounds
# exactly like the old 4-row one. Freely raisable, like the bank count.
_CHORD_ROWS_PER_BANK = 8

# Chord view row routing -------------------------------------------------------
_ROW_TYPES  = ["chord", "direct", "off"]
# Ports the Configure-Rows overlay can cycle through.  "synth" is the internal
# engine; add any keys you define in orc1_config.PORTS to route rows to external
# gear (they'll pick up the default tint below).
_ROW_PORTS  = ["synth", "pisound", "rtp"]
_ROW_PORT_SHORT = {
    "synth": "syn ", "pisound": "piso", "rtp": "rtp ",
}
# White-key pad colour per row type/port (R,G,B in Fire 0-127 range).
# Chord rows keep the existing warm-white; direct rows get a cool per-port tint.
# Ports without an explicit entry fall back to the default tint in _render.
_ROW_COL_CHORD  = (8, 6, 4)     # warm white
_ROW_COL_OFF    = (1, 1, 1)     # near-dark
_ROW_COL_DIRECT = {              # keyed by port name
    "synth":   (8,  3, 12),      # purple
    "pisound": (3,  9,  5),      # cool green
    "rtp":     (2,  4, 14),      # blue
}
# -- KEY overlay (2026-07-28) --------------------------------------------------
# Hold ALT in chord/poly/chip to momentarily turn the grid into a key/scale
# picker; release and the keyboard is back.  Costs no permanent pads, which is
# the whole point -- a dedicated root row would have eaten a keyboard octave.
#
#   rows 0-2, cols 4-15 : scales, one SCALE_GROUPS row each
#   row 3,    cols 4-15 : the 12 chromatic roots (col 4 = C ... col 15 = B,
#                         the same column-to-pitch-class mapping the chord
#                         keyboard already uses, so the pads sit where you
#                         expect them)
#   row 3,    cols 0-1  : KEY LOCK / SCALE SNAP toggles
_KEY_ROOT_ROW    = 3               # grid row holding the 12 root pads
_KEY_ROOT_COL0   = 4               # col of root C
_KEY_WHITE_PCS   = {0, 2, 4, 5, 7, 9, 11}
_KEY_ROOT_WHITE  = (10,  8,  6)    # unselected white key
_KEY_ROOT_BLACK  = (2,  1,  1)     # unselected black key
_KEY_ROOT_SEL    = (0x50, 0x30, 0x00)   # the active root -- warm gold
# One hue per scale group so the three rows read as distinct families.
_KEY_GROUP_COLORS = [
    ((3, 6, 10), (0x10, 0x40, 0x7F)),   # MODES  -- blue
    ((3, 10, 6), (0x10, 0x7F, 0x40)),   # PENTA  -- green
    ((9, 4, 10), (0x60, 0x18, 0x7F)),   # COLOUR -- violet
]
_KEY_TOGGLE_OFF  = (6, 4, 1)
_KEY_LOCK_ON     = (0x7F, 0x50, 0x00)   # amber
_KEY_SNAP_ON     = (0x00, 0x7F, 0x30)   # green
# O4 (2026-07-30). The control zone was cols 0-1; cols 2-3 were dark and are
# now QUALITY and PIN. Col 1 stopped being a boolean and cycles the five scale
# modes, so it needs a colour per mode rather than an on/off pair.
_KEY_MODE_COLORS = {
    "off":     _KEY_TOGGLE_OFF,
    "filter":  (0x7F, 0x00, 0x18),      # red -- notes are being SILENCED
    "up":      (0x00, 0x60, 0x7F),      # cyan
    "down":    (0x00, 0x28, 0x7F),      # blue
    "nearest": _KEY_SNAP_ON,            # green, the pre-O4 look
}
_KEY_QUAL_DIAT   = (6, 4, 1)            # DIAT is the default -- reads as "off"
_KEY_QUAL_FIXED  = (0x7F, 0x30, 0x50)   # magenta, matches the chromatic-ext hue
_KEY_PIN_ON      = (0x7F, 0x7F, 0x7F)   # white = overlay is pinned open
_KEY_PIN_OFF     = (10, 10, 10)
_KEY_COL_MODE    = 1                    # scale-mode cycle
_KEY_COL_QUAL    = 2                    # DIAT / FIXED
_KEY_COL_PIN     = 3                    # latch the overlay open

_DEFAULT_CHORD_ROWS = [
    # Per-row "octave" is now ABSOLUTE (2026-09-03): it fully sets that row's
    # octave offset from perform_base_note, with no hidden (3-row)*12 auto-stack.
    # These defaults ([+3,+2,+1,0]) reproduce exactly the old C5/C4/C3/C2 stack
    # (perform_base_note default 36 = C2), so the sound is unchanged out of the box.
    # A "synth" row NAMES its module (live /matrix binding, 2026-09-24).
    # Row 0 top (highest octave): direct → internal poly synth
    {"type": "direct", "port": "synth", "channel": "poly",    "octave": 3},
    # Row 1: direct → internal chip pulse 1
    {"type": "direct", "port": "synth", "channel": "chip_p1", "octave": 2},
    # Row 2: direct → internal bass tone (the mono synth)
    {"type": "direct", "port": "synth", "channel": "mono",    "octave": 1},
    # Row 3 bottom (lowest octave): chord engine (original behaviour)
    {"type": "chord",  "port": None,    "channel": None, "octave": 0},
]


def _atomic_write_json(path, data):
    """Write JSON via temp file + rename, so no reader can see a partial file.

    Both of this handler's writes to ~/orc1_state.json used a plain
    `open(path,"w")`, which TRUNCATES immediately and then streams. That leaves
    a window in which the file is empty or half-written, and there are readers
    on both sides of it:

      * orc1.py's write_state_json() now reads this file to carry forward the
        seven fire_* keys it does not own. A torn read there is swallowed, and
        that cycle's write lands WITHOUT those keys -- a narrow reappearance of
        the exact clobber that fix was for.
      * orc1_web.py's read_state() reads it from a DIFFERENT PROCESS on every
        /state poll, so a torn read blanks the entire web UI for that tick.

    A lock would not help the second case at all, since it crosses a process
    boundary; atomicity fixes both. os.replace is atomic on POSIX, and the temp
    file is created in the same directory so the rename never crosses a
    filesystem. Matches what write_state_json() has always done.
    """
    tmp = path + ".fire.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)          # never leave a stray temp behind
        except Exception:
            pass
        raise


_SUBSCREEN_JUMP = [
    ("seq",       (0x40, 0x00, 0x00)),   # col  8: red
    ("mixer",     (0x00, 0x40, 0x00)),   # col  9: green
    ("chord",     (0x40, 0x20, 0x00)),   # col 10: gold
    ("chip",      (0x00, 0x30, 0x30)),   # col 11: cyan
    ("poly",      (0x00, 0x00, 0x40)),   # col 12: blue
    ("faders",   (0x28, 0x00, 0x38)),   # col 13: purple (FADERS)
    ("universal", (0x18, 0x18, 0x18)),   # col 14: dim grey
    ("fx",        (0x40, 0x18, 0x00)),   # col 15: amber-orange
]


def pad_index(row, col):
    return row * N_COLS + col


def note_to_rc(note):
    i = note - PAD_NOTE_BASE
    return i // N_COLS, i % N_COLS


# --------------------------------------------------------------------------- #
# Assign-screen text helpers (shared by the UNIVERSAL pads and the FADERS bank) #
# --------------------------------------------------------------------------- #
# Both surfaces are the same idea -- an assignable control that sends MIDI
# somewhere -- so they share the strings that describe one.  Every line here
# is written to the real panel budget (fire_oled.max_chars: 16 small, 14 big
# at x=2); _test_fire.py fails if any painted line exceeds it.

def _port_label(port):
    """Short OLED label for a MIDI port key.

    'pisound' is the DIN out, 'synth' the internal bus.  Anything else -- an
    rtp session, a hot-plugged USB device opened by MIDI_SCAN -- gets an
    uppercased 4-char stub, because the port list is now enumerated live
    (see _assign_ports) instead of being a hardcoded pair.  The 4-char cap is
    load-bearing: it is what keeps the "ch16 PISO LATCH" line inside 16.
    """
    p = str(port or "")
    if p == "pisound":
        return "DIN"
    if p == "synth":
        return "SYN"
    return p.upper()[:4] or "?"


def _assign_detail_line(e):
    """'what it sends' line: type, number and value.

    PC carries no value (program change is a single number), so it says so
    rather than printing a meaningless one.  Worst case 'NOTE 127 val 127'
    is exactly 16 characters.
    """
    typ = str(e.get("type", "note")).upper()
    num = int(e.get("number", e.get("cc", 0)))
    if typ == "PC":
        return f"PC {num}"
    return f"{typ} {num} val {int(e.get('value', 0))}"


def _assign_dest_line(e, latch=None, hint=False):
    """'where it goes' line: channel, port, and latch flag when set.

    Worst case 'ch16 PISO LATCH' is 15 characters.  This is the half that was
    invisible before 2026-07-31 -- the old single-line detail string ran off
    the panel right after the value, so channel and output could never be
    read back on a NOTE or CC assignment.
    """
    line = f"ch{int(e.get('channel', 1))} {_port_label(e.get('port'))}"
    if latch is None:
        latch = e.get("latch")
    if latch:
        line += " LATCH"
    elif hint:
        # The 4-line rewrite spent its last line on the encoder legend and so
        # dropped the only on-screen mention that SELECT-turn cycles the type.
        # An unlatched destination line has the room, so it carries it.
        line += " SEL=ty"
    return line


def _assign_knob_legend(e):
    """Encoder legend for an assign screen; PC has no value knob."""
    if str(e.get("type", "note")).upper() == "PC":
        return "E0n E2c E3p"
    return "E0n E1v E2c E3p"


# Sentinel knob target: selecting it in the KNOB MAP menu opens the CC OUT
# editor rather than assigning anything. It is deliberately NOT a real dial
# action -- there is no _dial_cc_custom -- so _load_knobmap filters it out of
# the accepted set; see the note there.
_KMAP_CC_ACTION = ("cc_custom",)
_KMAP_CC_TARGET = ("CC OUT", _KMAP_CC_ACTION)


# Only these views have an actual drum-voice selection (self._sel_voice) for
# _dial_voice_param to sculpt. Every other view -- including chord/poly/chip,
# which each have their OWN attack/decay/etc via cc_out on their own channel
# -- has no drum voice, so offering "attack"/"decay"/... there just means the
# menu quietly assigns a knob to whatever drum voice happens to be selected
# (always sent on CH_DRUM by _send_drum_param, regardless of the view you're
# editing). That is the 2026-08-04 "chip attack knob moves the kick click"
# bug: chip had no gate at all, so voice_param was offered and assigned there
# same as universal/faders always correctly blocked it.
_VOICE_PARAM_VIEWS = ("mixer", "seq")

# The universal/faders surfaces are EXTERNAL CONTROL surfaces: their whole job is
# to send a signal OUT. Every internal ORC parameter is reachable by just sending
# the right CC to the right channel (that's the module-matrix/synth-bus design),
# so the named internal targets (voice_param, bpm, spread, track_*, oct_offset)
# are redundant clutter there. On these surfaces the assign menu offers ONLY send
# actions -- plus "none" to park a knob. (2026-09-02, Fire reorg.)
_SEND_ACTION_TYPES      = ("cc_out", "pitch_out")
_EXTERNAL_SURFACE_VIEWS = ("universal", "faders")

# -- Knob sends that NAME A MODULE (2026-09-24) ---------------------------------
# A send to the internal synth bus may name a MODULE instead of a channel number:
# ("cc_out", "synth", "poly", 74) / ("pitch_out", "synth", "chip_p2"). The
# channel is looked up at SEND time from the brain's live /matrix bindings, so
# moving a module on /matrix moves every knob aimed at it -- nothing has to be
# re-saved. Before this every internal target was a literal channel written
# when the module lived there: after poly and chip P2 swapped channels the
# POLY view's knobs (ch 8) drove chip P2, and "p2vibd" still pointed at ch 10.
# A plain 1-16 channel still means exactly that channel (external gear, or a
# deliberate "whatever sits on ch N").
try:
    import orc1_config as _cfg_mod
    _MODULE_KEYS   = frozenset(getattr(_cfg_mod, "MODULE_KEYS", ()))
    _MODULE_CH_KEY = dict(getattr(_cfg_mod, "MODULE_CH_KEY", {}))
except Exception:
    _MODULE_KEYS, _MODULE_CH_KEY = frozenset(), {}

# Short OLED names (the panel is ~16 characters wide).
_MODULE_SHORT = {"poly": "POLY", "chip_p1": "P1", "chip_p2": "P2",
                 "chip_tri": "TRI", "chip_noise": "NS", "chip_noise2": "NS2",
                 "gb_wave": "WAVE", "chip_pcm": "PCM", "drums": "DRUM",
                 "sampler": "SMP", "mono": "MONO", "bbkit": "BBKIT"}

# The channel each module shipped on when the literal-channel defaults were
# written. Used ONLY to migrate saved knob maps whose entries are exactly those
# shipped defaults (see _migrate_legacy_send); never to route anything live.
_LEGACY_SYNTH_SENDS = {
    # (kind, ch, cc-or-None) -> module
    **{("cc_out", 8, cc): "poly" for cc in (1, 8, 9, 11, 13, 70, 71, 74, 75)},
    **{("cc_out", 9, cc): "chip_p1"
       for cc in (1, 8, 9, 11, 13, 70, 71, 74, 75, 77, 78, 79, 80, 81)},
    ("pitch_out", 9, None): "chip_p1",
    ("cc_out", 10, 77): "chip_p2", ("cc_out", 10, 78): "chip_p2",
    ("pitch_out", 10, None): "chip_p2",
    ("cc_out", 5, 77): "gb_wave", ("cc_out", 5, 78): "gb_wave",
    ("pitch_out", 5, None): "gb_wave",
}


def _send_ch_ok(port, ch):
    """A send action's channel field: 1-16, or a module key on the synth bus."""
    if isinstance(ch, str):
        return port == "synth" and ch in _MODULE_KEYS
    return isinstance(ch, int) and not isinstance(ch, bool) and 1 <= ch <= 16


def _valid_chord_row(r):
    """One chord-row config from outside (web): {type, port, channel, octave}.
    A direct row's channel is 1-16, or a module key on the synth port."""
    if not isinstance(r, dict) or r.get("type") not in ("chord", "direct", "off"):
        return False
    oc = r.get("octave", 0)
    if not isinstance(oc, int) or isinstance(oc, bool) or not -4 <= oc <= 4:
        return False
    if r["type"] == "direct":
        port = r.get("port")
        return isinstance(port, str) and bool(port) and _send_ch_ok(port, r.get("channel"))
    return True


def _migrate_legacy_send(view, act):
    """Saved literal-channel synth send -> the module it was written for, or
    `act` unchanged. Only exact shipped defaults convert (a poly-view knob on
    synth ch 8 was the poly by construction; the named targets are listed in
    _LEGACY_SYNTH_SENDS) -- a CC the player aimed at a channel by hand stays a
    channel."""
    if (not isinstance(act, tuple) or len(act) < 3 or act[0] not in _SEND_ACTION_TYPES
            or act[1] != "synth" or isinstance(act[2], str)):
        return act
    ch = act[2]
    cc = act[3] if act[0] == "cc_out" and len(act) == 4 else None
    if view == "poly" and ch == 8:
        mod = "poly"
    else:
        mod = _LEGACY_SYNTH_SENDS.get((act[0], ch, cc))
    if mod is None:
        return act
    return (act[0], "synth", mod) + tuple(act[3:])


# Format markers that say "the literal-channel -> module migration has run".
# Without them the "one-time" migration ran on EVERY boot, so a synth ch 8 CC
# (or chord row on ch 8 / 7) the player set ON PURPOSE after 2026-09-24 was
# silently turned back into "poly" / "mono" at the next restart (review,
# 2026-10-01). A file carrying the marker is loaded as-is; one without it is
# migrated once and re-saved with it.
_KNOBMAP_VERSION    = 2     # top-level "_version" key of ~/fire_knobmap.json
_CHORD_ROWS_VERSION = 3     # "v" of ~/.orc1_chord_rows.json (v2 = banked,
                            # v3 = banked + module-migrated; same shape)
_LEGACY_CHORD_ROWS = {0: (8, "poly"), 1: (9, "chip_p1"), 2: (7, "mono")}


def _migrate_chord_banks(banks):
    """Shipped literal-channel synth chord rows -> the module they were written
    for (2026-09-24): rows 0-2 of the old default stack were synth ch 8 / 9 / 7
    = poly / chip pulse 1 / the old ch-7 mono. Only a row that still holds
    exactly that default converts. Mutates `banks` in place; returns the count.
    Call ONLY on a file older than _CHORD_ROWS_VERSION."""
    n = 0
    for b in banks:
        if not isinstance(b, list):
            continue
        for i, r in enumerate(b):
            if (i in _LEGACY_CHORD_ROWS and isinstance(r, dict)
                    and r.get("type") == "direct" and r.get("port") == "synth"
                    and r.get("channel") == _LEGACY_CHORD_ROWS[i][0]):
                r["channel"] = _LEGACY_CHORD_ROWS[i][1]
                n += 1
    return n


def knobmap_set_offline(path, view, layer, knob, action):
    """Write ONE knob assignment into the saved knob map with no Fire attached
    (the brain's KNOBMAP_SET when the Fire is unplugged; the Fire loads the
    file when it is plugged in). The caller has validated `action`.

    An UNMARKED file is migrated first (same rule as _load_knobmap), because
    writing the marker without migrating would freeze its shipped literal-
    channel defaults as channels for good. Raises on a bad view/layer/knob or
    an unreadable-but-present file (never silently overwrites one)."""
    if not path:
        raise ValueError("no KNOBMAP_FILE configured")
    if view not in VIEWS or layer not in ("base", "shift", "alt") or not 0 <= int(knob) <= 3:
        raise ValueError(f"bad knob address {view}/{layer}/{knob}")
    full = os.path.expanduser(path)
    try:
        with open(full) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{full}: not a knob-map object")
    if int(data.get("_version", 0) or 0) < _KNOBMAP_VERSION:
        for v, layers in data.items():
            if not isinstance(layers, dict):
                continue
            for knobs in layers.values():
                if not isinstance(knobs, dict):
                    continue
                for k, act in list(knobs.items()):
                    if isinstance(act, (list, tuple)):
                        new = _migrate_legacy_send(v, tuple(act))
                        if new != tuple(act):
                            knobs[k] = list(new)
    data.setdefault(view, {}).setdefault(layer, {})[str(int(knob))] = list(action)
    data["_version"] = _KNOBMAP_VERSION
    _atomic_write_json(full, data)


def _target_allowed_in_view(action, view):
    """False for a knob target that does not belong in `view`.

    ONE rule, shared by all writers -- the on-device menu, the web KNOBMAP_SET /
    UNIVERSAL_SET_KNOB, and the knob-preset loader -- so no path can assign a
    target another path would undo (the "the menu promises what the loader
    undoes" defect this filter exists to prevent).

    Two exclusions:
      - voice_param sculpts a drum voice, so it belongs only to the drum-facing
        views (MIXER/SEQ). (2026-08-04)
      - the external control surfaces (universal/faders) take ONLY send actions
        (cc_out/pitch_out) or "none"; internal ORC targets are meaningless there
        because you drive an internal param by sending its CC yourself. (2026-09-02)
    """
    if not action:
        return False
    if action[0] == "voice_param" and view not in _VOICE_PARAM_VIEWS:
        return False
    if view in _EXTERNAL_SURFACE_VIEWS:
        # Allow the send actions, "none", and the CC-OUT MENU sentinel (which
        # opens the CC editor -- the primary way to send a CC here; it is still
        # unsavable as an assignment, see _knob_action_accepted).
        return (action[0] in _SEND_ACTION_TYPES
                or action == _KMAP_CC_ACTION
                or action[0] == "none")
    return True


def _well_formed_send_action(act):
    """True for a structurally valid ('cc_out'|'pitch_out', port, ch[, cc]).

    These are the only knob targets whose parameters are free-form rather
    than chosen from FIRE["KNOB_TARGETS"], so they cannot be validated by
    membership in that list -- which is exactly how they used to be lost:
    _load_knobmap accepted ONLY actions present in KNOB_TARGETS, so an
    arbitrary CC assigned from the /control web page was written to
    ~/fire_knobmap.json and then silently dropped on the next restart. The
    web route had been recorded as working since 2026-07-13 because the
    assignment does work -- right up until you reboot.

    Mirrors the validation orc1.py's KNOBMAP_SET already applies on the way
    in; keep the two in step.
    """
    if not isinstance(act, (list, tuple)):
        return False
    if len(act) == 4 and act[0] == "cc_out":
        _, port, ch, cc = act
        return (isinstance(port, str) and bool(port) and _send_ch_ok(port, ch)
                and isinstance(cc, int) and not isinstance(cc, bool) and 0 <= cc <= 127)
    if len(act) == 3 and act[0] == "pitch_out":
        _, port, ch = act
        return isinstance(port, str) and bool(port) and _send_ch_ok(port, ch)
    return False


def decode_relative(v):
    return v if v < 64 else v - 128


def _scale(color, num, den):
    return tuple(min(0x7F, max(0, c * num // den)) for c in color)


def _vel_tint(color, vel):
    """Tint a track colour by a per-step velocity so the grid shows dynamics:
    loud (accent-tier) steps brighten like the old accent LED; quieter steps
    dim proportionally toward black. vel is a MIDI velocity 1-127."""
    vel = max(1, min(127, int(vel)))
    if vel >= 110:                       # accent band -> the old bright look
        return tuple(min(0x7F, x + ACCENT_BOOST) for x in color)
    return _scale(color, vel, 127)       # ghost/normal -> proportional dim


_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def _note_name(n):
    """MIDI note number -> name string, e.g. 60 -> 'C4'."""
    n = int(n)
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"

# Tone index -> short display name for chord-aware sequencer tracks.
# Matches the ArpEngine tone index convention: 0=root 1=3rd 2=5th 3=7th 4=9th -1=bass
_CHORD_TONES  = {-1: "BASS", 0: "ROOT", 1: "3rd", 2: "5th", 3: "7th", 4: "9th"}
_CHORD_TONE_ORDER = [-1, 0, 1, 2, 3, 4]   # scroll order for the UI


# ==============================================================================
# HANDLER
# ==============================================================================

# Action names that resolved to no method, so we complain about each exactly
# once instead of on every knob click.
_UNRESOLVED_SEEN = set()


def _unresolved(prefix, name):
    """A control is wired to an action name that has no handler.

    Every dynamic dispatch in this file is `getattr(self, PREFIX + name, None)`
    followed by a bare `if m:`. Without this, a typo in a config map or a
    renamed method produces a control that silently does nothing -- no
    exception, no log, nothing to grep for. That failure mode has cost this
    project multiple multi-day bugs (a dead route pad went unnoticed for ten
    days), so it is now loud on first occurrence.

    Deduplicated because dial dispatch runs per encoder click; an undeduped
    print would flood the journal from one knob sweep.

    _test_fire.py::test_dispatch_names catches the same defect statically,
    before it ever reaches hardware.
    """
    key = prefix + name
    if key in _UNRESOLVED_SEEN:
        return
    _UNRESOLVED_SEEN.add(key)
    print(f"  ! Fire: control mapped to '{name}' but no {key}() exists "
          f"-- that control does nothing", flush=True)


_SWALLOW_SEEN = set()


def _swallowed(where, exc):
    """Report a swallowed error from a HOT-PATH best-effort / fallback site,
    ONCE per (site, exception-type).

    Two shapes use this instead of a plain `print`:
      * engine-facade fallback shims -- `_set_scale`/`_set_root`/
        `_set_chord_base`/`_chord_root_name`/`_effective_base` all try the
        engine setter/getter and fall through to a local state fallback on
        failure. The fallback keeps the control WORKING, which is exactly why a
        broken engine method stayed invisible: the pad still does something.
        Some of these (`_chord_root_name`, `_effective_base`) run on every OLED
        render.
      * MIDI pushes on knob paths -- the chip couple/detune CCs and the
        `cc_out` knob send fire once per encoder click.

    An undeduped print on any of these would flood the journal from a single
    knob sweep or a few seconds of rendering -- the same reason `_unresolved`
    dedups -- so the FIRST failure is loud and the repeats are quiet. The
    caller's fallback path is unchanged; this only adds the breadcrumb the
    silent `except Exception: pass` never left.
    """
    key = (where, type(exc).__name__)
    if key in _SWALLOW_SEEN:
        return
    _SWALLOW_SEEN.add(key)
    print(f"  ! Fire {where}: {exc} (further repeats of this suppressed)",
          flush=True)


def _hsv(h, s=1.0, v=0.7):
    """HSV -> (r,g,b) each 0-127."""
    import math
    h = h % 1.0
    i = int(h * 6)
    f = h * 6 - i
    p, q, t_ = v*(1-s), v*(1-f*s), v*(1-(1-f)*s)
    rr,gg,bb = [(v,t_,p),(q,v,p),(p,v,t_),(p,q,v),(t_,p,v),(v,p,q)][i%6]
    return int(rr*127), int(gg*127), int(bb*127)


class FireHandler:
    def __init__(self, eng):
        self.eng = eng
        self.cfg = eng.cfg
        fcfg = getattr(self.cfg, "FIRE", {}) or {}
        self.fcfg = fcfg

        self.view = fcfg.get("DEFAULT_VIEW", "seq")
        self._shift = False
        self._alt = False

        # -- Track colour palette (config-overridable; values are 0-127 RGB) ---
        # Colors are assigned once at track-creation time so they travel with
        # the track when it's reordered.  Per-track "color" in SEQ_TRACKS
        # overrides the palette for that specific track.
        # 2026-07-01 ownership refactor: track *building* moved to
        # orc1_seq.Sequencer (self-bootstrapping / add_track) -- this palette
        # is kept here only because Fire still passes its own color override
        # into seq.add_track() so new tracks keep matching this exact
        # sequence (see _add_track below). _DEFAULT_TRACKS / load_seq_state
        # are no longer used from this file -- Sequencer does its own
        # cold-boot bootstrap now (Bug A fix: works with no Fire connected).
        # TRACK_COLORS moved out of FIRE → SEQ dict (2026-07-21); fall back to
        # the legacy FIRE location, then the built-in default, for safety.
        _seqcfg = getattr(self.cfg, "SEQ", {}) or {}
        raw_palette = _seqcfg.get("TRACK_COLORS",
                                  fcfg.get("TRACK_COLORS", TRACK_COLOR))
        self._track_palette = [tuple(c) for c in raw_palette]

        self.view_top = 0          # top visible track row
        self.page = 0              # horizontal step page (page*16 window)
        self._sel_track = 0        # cursor: track the SELECT encoder/edit acts on
        self._track_hue = {}       # tidx -> hue degrees, for ALT+knob2 colour sweep
        self._overlay     = None    # active overlay: "browse" | "track_edit" | "kmap" | None
        self._pretty_lights = False  # party mode 🎉
        self._pl_speed = 1.0         # live master-speed multiplier (knob 0 while active)
        self._pl_mode = 0             # which visual (0=plasma 1=sparkle 2=sweep 3=fire 4=beat)
        self._browse_idx  = 0       # highlighted param index (used when overlay == "browse")
        self._held_seq_pad = None   # (tidx, step) while a step pad is held -- enables note dial-in

        # -- Session E (2026-06-30): subscreen + BROWSER context menu ----------
        # Subscreen: SHIFT+ALT = toggle latch for the mode-jump "second layer"
        # (mode-jump pads only, in this pass -- full per-mode content is future
        # work).  Press SHIFT+ALT to open (latched -- stays open after release);
        # press a pad to jump immediately and close; press SHIFT+ALT again to
        # dismiss without jumping.  _subscreen_latched is always True while the
        # normal subscreen is open; encoder-remap flow also uses it.
        self._subscreen              = False
        self._subscreen_latched      = False
        self._subscreen_pending_view = None   # queued view: previews on OLED, fires on close
        # O4: KEY overlay pinned open (row 3 col 3). Deliberately NOT persisted
        # in _save_fire_state -- a pinned overlay that survived a restart would
        # leave the keyboard covered with no obvious cause.
        self._key_overlay_latched    = False
        # BROWSER context menu (replaces the old ALT/SHIFT/bare-BROWSER view
        # shortcuts entirely -- see _act_browser). OLED-only UI, like the
        # pre-existing browse/kmap overlays.
        self._menu_open = False
        self._menu_idx  = 0
        # Preset picker overlay state
        self._preset_action = None  # "save_ucs"|"load_ucs"|"save_faders"|"load_faders"|"save_knobs"|"load_knobs"
        self._preset_slot   = 0     # 0-7

        # -- Perform-mode chromatic surface ------------------------------------
        self.perform_base_note = fcfg.get("PERFORM_BASE_NOTE", 36)
        # Build action->note reverse maps so _perform_ctrl_press is config-driven.
        # These look up into orc1_config's CTRL_*_NOTES dicts so any remapping
        # there is automatically reflected here without touching this file.
        self._perf_base_note = {v: k for k, v in self.cfg.CTRL_BASE_NOTES.items()}
        # Both extension vocabularies, so an ext pad can address either the
        # scale-degree note (plain press) or the chromatic one (SHIFT).
        self._perf_ext_note  = {v: k for k, v in self.cfg.CTRL_EXT_NOTES.items()}
        self._perf_ext_note.update(
            {v: k for k, v in
             getattr(self.cfg, "CTRL_EXT_DEGREE_NOTES", {}).items()})
        self._perf_mod_note  = {v: k for k, v in self.cfg.CTRL_MOD_NOTES.items()}
        self._pad_held = {}
        self._dir_held = {}       # padnote -> (port_key, ch_1idx, pitch) for direct rows
        self._perf_ext_held = {}  # padnote -> ctrl_note for held extension pads (momentary)
        # Chord row routing: load from persisted JSON, fall back to config, fall back to defaults
        _cr_defaults = fcfg.get("CHORD_ROW_MAP", _DEFAULT_CHORD_ROWS)
        # Row/routing BANKS: GRID <> flips between whole 4-row configs live.
        # _chord_rows always points at the active bank's list, so edits made in
        # the Configure-Rows overlay land in that bank and persist immediately.
        self._chord_banks, self._chord_bank = self._load_chord_rows(_cr_defaults)
        if getattr(self, "_chord_rows_migrated", 0):
            print(f"  Fire chord rows: {self._chord_rows_migrated} row(s) now name their "
                  f"module (follow /matrix)", flush=True)
        if getattr(self, "_chord_rows_unmarked", False):
            self._save_chord_rows()     # writes the v3 "migrated" marker
        self._chord_rows    = self._chord_banks[self._chord_bank]
        self._chord_row_sel = 0   # which row is selected in the configure-rows overlay
        self._chord_row_top = 0   # top logical row shown in the 4-row viewport
        # USER2 = chromatic poly keyboard straight into the internal poly synth
        self.poly_base_note = fcfg.get("POLY_BASE_NOTE", 48)
        self._poly_held = {}
        # _ch_poly / _ch_chip* / _poly_ch_now / _chip_ch_now are LIVE properties
        # (the /matrix bindings, see _module_ch) -- they used to be cached here at
        # startup, so a /matrix move reached the Fire only after an orc1 restart.
        # Poly view's own voice state -- independent of chip view.
        # Defaults to 'poly' (CH_POLY) so poly view feels like a poly synth
        # by default; MUTE buttons switch it just like chip view does.
        self.poly_voice   = 'poly'
        # CHIP = chromatic keyboard for the NES chip voices (one per MUTE row)
        self.chip_base_note = fcfg.get("CHIP_BASE_NOTE", 48)
        # Layout toggle: True = 4ths (5-semitone row stride, bass-guitar feel);
        # False = chromatic (N_COLS stride, original layout). ALT+NOTE cycles.
        self._fourths_mode = True
        self._chip_held = {}      # pad_note -> (pitch_note, midi_channel)
        # Currently selected NES voice -- switches via MUTE1-4 in chip view.
        # Start on Pulse 1 (the most common voice for melodies).
        self.chip_voice   = 'pulse1'
        # Local mirror of NESEngine.coupling_mode/detune_cents. Fire is the
        # source of truth for what mode/detune it THINKS is active -- cycles
        # these locally and pushes the result to the synth via CC72/CC73
        # (see _toggle_mute's chip branch), since there's no shared object
        # across the orc1.py/orc1_synth.py process boundary. If they ever
        # drift (e.g. synth service restarts and resets to defaults), there's
        # no automatic resync -- a known limitation, not a bug.
        self._chip_couple_mode = 'none'
        self._chip_detune_cents = 7   # matches NESEngine's own default
        # P1/P2 stereo spread toggle (ALT+MUTE1 in chip view) -- pans
        # pulse1/pulse2 apart so coupling modes are easier to hear apart.
        # Local mirror only (no live object to read back), same pattern as
        # the coupling mode/detune mirrors above.
        self._chip_p1p2_spread = False

        # -- FX chain view state ---------------------------------------------------
        # _fx_sel_slot: which bottom-row slot pad is currently highlighted (0-7).
        # _fx_chain_names: ordered list of chain names, populated on first
        #   _render_fx() call and kept in sync on every repaint.
        self._fx_sel_slot    = 0
        self._fx_chain_names = []
        # Runtime parameter values tracked by the encoders.
        # Structure: {chain_name: {plugin_idx: {symbol: float}}}
        # Each entry is seeded from the "default" in fx_chains.json on the first
        # encoder touch, then updated live as the user turns knobs.
        # Cleared when a chain is toggled so stale values don't contradict
        # jalv's freshly-reloaded plugin defaults on next start.
        self._fx_param_vals  = {}
        # The most recent parameter touched by an encoder, for OLED display.
        # Set by _dial_fx_param(); cleared on pad-press or view change so
        # the OLED reverts to the chain name / status once the user stops tweaking.
        # Format: (label, current_value, p_min, p_max) or None.
        self._fx_last_param  = None
        # Slot 0 is always "nes_space" (fixed reverb).
        # Slots 1-7 are user-assignable; persisted to orc1_state.json.
        self._fx_slot_assignments = ["nes_space"] + [None] * 7
        self._fx_load_assignments()
        self._fx_held_slot   = None   # row-3 pad currently held in FX view
        self._fx_soft_layer  = None   # set by param-grid pad press; overrides physical layer in FX view
        self._fx_browse_slot = None   # slot whose plugin is being browsed
        self._fx_browse_idx  = 0      # index into the CURRENT bank's item list
        # Rack browser bank: 0 = Favorites, 1 = All, 2.. = categories.  SHIFT +
        # SELECT-turn steps banks; plain SELECT-turn steps items within a bank.
        # (See RackManager.banks()/bank_items().)
        self._fx_bank_idx    = 1      # default to "All" so a fresh box isn't empty
        self._aw_catalog      = None  # Airwindows catalog (loaded lazily)
        # PAT_SONG / PERFORM hold-detection timestamps (the Fire ROUTE grid was
        # removed 2026-08-06 -- routing lives on /patch now, and the auto-remove
        # coupling watches the live JACK graph regardless of surface).
        self._pat_song_down_t  = None   # hold-detection timestamp for PAT_SONG
        self._aw_catalog_keys = []    # sorted list of plugin names

        # -- Fire output -------------------------------------------------------
        self._out = None
        self._out_lock = threading.Lock()
        self._open_output()

        # -- Config: button/dial/select actions --------------------------------
        self._buttons = self._resolve_buttons(fcfg.get("BUTTONS", _DEFAULT_BUTTONS))
        self._dials       = fcfg.get("DIALS", _DEFAULT_DIALS)
        self._dials_shift = fcfg.get("DIALS_SHIFT", _DEFAULT_DIALS_SHIFT)
        self._dials_alt   = fcfg.get("DIALS_ALT", _DEFAULT_DIALS_ALT)
        self._select      = fcfg.get("SELECT", ("scroll_track",))
        self._select_alt  = fcfg.get("SELECT_ALT", ("scroll_voice",))

        # -- Per-view knob mapping (ALT+BROWSER live remap) ---------------------
        # Every VIEW owns its own base/SHIFT/ALT knob layer, each seeded from the
        # DIALS* config above. The maps are edited live in "knob map" mode and,
        # when a KNOBMAP_FILE is configured, persisted to disk so a remap
        # survives the next restart. self._targets is the scrollable menu of
        # assignable destinations (label, dial-action-tuple).
        self._layers       = ("base", "shift", "alt")
        # The free-form "CC OUT..." entry is prepended to the CONFIGURED list
        # (copied, never mutated in place -- it is a module-level config
        # object shared by every handler instance and by the test harness).
        # It sits first because an arbitrary CC send is the most useful thing
        # an assignable encoder can do, and until 2026-07-31 it was the one
        # thing the on-device menu could not express at all: the menu offers
        # only fixed labels, of which the CC entries have their port, channel
        # and CC number hardcoded in config.
        self._targets      = [_KMAP_CC_TARGET] + list(
            fcfg.get("KNOB_TARGETS", _DEFAULT_KNOB_TARGETS))
        self._knobmap_file = fcfg.get("KNOBMAP_FILE")    # None => no persistence
        self._init_dialmaps()
        self._load_knobmap()
        # Post-load strip: remove any legacy voice_param entries from every
        # view except mixer/seq (the only ones with a drum voice to sculpt --
        # see _VOICE_PARAM_VIEWS). Only voice_param is removed; user-assigned
        # cc_out/bpm/none actions survive reboots. Widened 2026-08-04 from
        # faders/universal only to match _target_allowed_in_view's new
        # allowlist, so a chord/poly/chip knob saved with the old bug (before
        # this fix) is also cleaned up on load rather than persisting.
        for _v in VIEWS:
            if _v in ("mixer", "seq"):
                continue
            if _v in self._dialmap:
                for _layer in list(self._dialmap[_v]):
                    self._dialmap[_v][_layer] = {
                        idx: act for idx, act in self._dialmap[_v][_layer].items()
                        if not (act and act[0] == "voice_param")
                    }
        # Set up the chip view dial map for the default voice (Pulse 1).
        # This must run after _init_dialmaps so it overwrites the generic CH9
        # defaults with the correct voice-channel assignments.
        self._select_chip_voice(self.chip_voice)
        # Knob-map overlay runtime state (inert until ALT+BROWSER enters it).
        self._kmap_knob  = 0         # which knob (0..3) is being mapped
        self._kmap_layer = "base"    # which layer: base / shift / alt
        self._kmap_sel   = 0         # cursor into _kmap_targets()
        # The view the knob-map overlay was opened on.  Everything in the
        # overlay -- target list, cursor, assignment, both screens -- reads
        # this, NOT self.view.  The mode buttons are not gated on an open
        # overlay (_set_view only special-cases track_edit), so self.view can
        # change underneath the overlay at any moment; without the pin an
        # assignment lands on whichever view the player happened to bump into.
        self._kmap_view  = None
        # Working copy for the CC OUT editor (overlay "kmap_cc").
        self._kmap_cc    = {"port": "pisound", "channel": 1, "cc": 74}

        # Track-edit overlay: knobs 0-3 directly edit the selected track's props.
        # Auto-entered when SHIFT+REC creates a new track.
        # Session E (2026-06-30): "len" removed -- track length is set ONLY by
        # ALT+step pad now. Knob 3 in track-edit mode is simply unmapped
        # (idx < len(self._te_props) guard in _dispatch_dial already handles
        # a shorter list gracefully).
        self._te_props   = fcfg.get("TRACK_EDIT_KNOBS", ["note", "ch", "vel"])
        # Absolute value per outboard/internal CC target (relative encoders
        # accumulate into this); keyed by (port, ch, cc), centred at 64.
        self._cc_vals    = {}
        # -- Chip/GB expansion (2026-07-20): pitch-bend knob state ---------
        # Accumulated pitchwheel value per (port, ch), centred at 0
        # (-8192..8191).  See _dial_pitch_out.
        self._pitch_vals   = {}
        # Pending spring-back timers per (port, ch): when a bend knob goes
        # idle for _PITCH_RECENTER_S the bend snaps back to centre (0).
        self._pitch_timers = {}
        # NB: _load_fire_state() used to be called HERE, roughly 120 lines too
        # early. It restores _mpc_faders and _universal_pads, neither of which
        # existed yet at this point, so:
        #   * _sync_volume_fader_values() inside the loader raised
        #     AttributeError on _mpc_faders -> swallowed by the loader's bare
        #     `except` -> every restore AFTER that line was silently skipped;
        #   * and on the path where it didn't raise, the assignments below
        #     overwrote what the loader had just restored.
        # Either way a saved fader bank and saved UCS pads could never come
        # back. It now runs at the END of __init__, after every attribute it
        # touches exists. See _test_fire.py for the regression tests.
        # -- MPC fader bank state ----------------------------------------
        # Assignable faders: same philosophy as UCS pads but continuous
        # (tap a column to set value, not momentary). Each fader dict:
        #   cc       CC number 0-127
        #   channel  MIDI channel 1-16
        #   port     "pisound" (DIN) or "synth" (internal)
        #   value    last sent value 0-127 (persisted)
        # fader indices are ints; 4 visible at once; GRID<> banks by 4.
        # Boots as a MASTER/POLY/CHIP/DRUM volume mixer (see
        # _default_volume_faders). Still fully reassignable; a saved fader bank,
        # if the user has made one, replaces these when state loads.
        self._mpc_faders      = self._default_volume_faders()  # fader_idx -> {type,number,channel,port,value,label}
        self._sync_volume_fader_values()   # show the synth's real bus levels, not the boot defaults
        self._mpc_last_fader  = None  # int index of last-tapped fader row
        self._mpc_assign_mode   = False
        self._mpc_assign_target = None  # int fader index being assigned
        self._mpc_edit          = {}    # working copy during assign mode
        self._mpc_view_top = 0

        # -- Universal Control Surface state (Step 16) ----------------------
        # Pad assignments keyed by (row, col). Each value is a dict:
        #   type         "note" or "cc"
        #   number       note number or CC number (0-127)
        #   value        default velocity (notes) or CC value (0-127)
        #   channel      MIDI channel 1-16
        #   port         "pisound" (DIN out) or "synth" (internal)
        #   latch        True = toggle on/off each press, False = momentary
        #   shift_action / alt_action  same shape dict or None
        #   _latched_on  runtime only: current latch state (not persisted)
        #
        # BANKS (2026-07-31): the 64 pads are one bank of several; GRID <>
        # pages between them.  Storage is a dict of bank index -> the same
        # {(row,col): assign} dict this surface has always used, and
        # `_universal_pads` is a PROPERTY onto the current bank.  That shape
        # was chosen deliberately: every existing reader and writer --
        # including orc1.py's UNIVERSAL_SET_PAD / UNIVERSAL_CLEAR_PAD, which
        # REBIND the attribute (`fire._universal_pads = {}`) from another
        # module -- keeps working untouched, and a rebind lands in the
        # current bank instead of silently detaching from the bank table.
        #
        # Bank count is capped at 9 because the OLED prints it as a single
        # character ("PAD 4-3,15 ALT" is exactly the 14-char big-text budget);
        # a 2-digit bank would push that line off the panel, which is the
        # very bug this session exists to fix.
        try:
            _nb = int(fcfg.get("UNIVERSAL_BANKS", 4))
        except (TypeError, ValueError):
            _nb = 4
        self._universal_nbanks        = max(1, min(9, _nb))
        self._universal_bank          = 0
        self._universal_banks         = {i: {} for i in range(self._universal_nbanks)}
        # Per-page (per-bank) KNOB overrides for the universal surface.
        # Kept SEPARATE from _universal_banks (pads) so the _universal_pads
        # property and the cross-module rebinds (`fire._universal_pads = {}`)
        # stay untouched. Shape: {bank_idx: {layer: {knob_idx: action_tuple}}}.
        # Applied as a READ-OVERLAY at dispatch (see _universal_knob_action)
        # over the baseline _dialmap["universal"] -- deliberately NOT the
        # mutate-dialmap-then-restore model the spec first sketched, because a
        # restore window that leaks is the exact bug class this project keeps
        # hitting. Fire reorg Phase 2 (2026-09-02). Authored per-page on the
        # web /control page; on-device kmap still edits the global baseline.
        self._universal_knobs         = {}
        # Per-page (per-bank) FADER rows (Fire reorg Phase 3). A page can make any
        # of its 4 rows a horizontal CC fader (16 pads = one 0-127 level), like
        # the old faders view / seq mixer draw. Separate store, same reasons as
        # _universal_knobs. Shape: {bank: {row: {cc, channel, port, value}}}. A
        # fader row OVERRIDES per-pad behaviour on that row (render + input).
        # Authored on the web; on-device the row just plays (tap = set level).
        self._universal_faders        = {}
        self._last_universal_pad      = None  # (row,col) of last pad touched
        self._universal_assign_mode   = False # True while editing an assignment
        self._universal_assign_target = None  # (row,col) currently being edited
        self._universal_assign_layer  = "base"  # "base"|"shift"|"alt"
        self._universal_edit          = {}    # working copy during assign mode
        self._universal_held          = {}    # (row,col)->True while held down

        # -- Live voice-sculpting state ----------------------------------------
        # (Sample-slot label scan removed 2026-07-21 — the sample slots left the
        # Fire drum editor for the sampler section, so it was dead code.)
        # EDIT_VOICES moved out of FIRE → DRUM_CC (2026-07-21); fall back to the
        # legacy FIRE location, then the built-in default.
        base_voices = getattr(self.cfg, "DRUM_CC", {}).get(
            "EDIT_VOICES", fcfg.get("EDIT_VOICES", _DEFAULT_EDIT_VOICES))
        # Sample slots relocated to the sampler section 2026-07-21 — the Fire
        # drum editor is drums-only now (no more smpl_0..3 appended here).
        self._edit_voices = list(base_voices)
        self._sel_voice   = 0
        # CC layout comes from cfg.DRUM_CC["LAYOUT"] — the SAME data-driven single
        # source of truth the synth decodes: each voice is packed to exactly its
        # params starting at BASE, no reserved slack, HP band gone. LAYOUT maps
        # {voice: {"start": first_cc, "params": [...]}}. Fallback mirrors the
        # config packing (used only if the computed LAYOUT is absent).
        _dcc = getattr(self.cfg, "DRUM_CC", {})
        self._DRUM_CC_BASE = int(_dcc.get("BASE", 20))
        self._DRUM_LAYOUT  = dict(_dcc.get("LAYOUT")
                                  or _fire_fallback_drum_layout(self._DRUM_CC_BASE))
        self._voice_vals = {v: {"level": 100, "tune": 64, "attack": 10,
                                "decay": 70, "filter_freq": 80, "filter_res": 0}
                            for v in self._edit_voices}
        # Reverse lookup: MIDI note -> index in _edit_voices (for track->voice linking)
        try:
            from orc1_synth import DRUM_NOTE_MAP
        except ImportError:
            # Fallback only if the synth module can't be imported. Kept complete
            # and in sync with orc1_synth.DEFAULT_DRUM_NOTE_MAP (incl. alias
            # notes + cowbell) so this dormant copy can't silently drift.
            DRUM_NOTE_MAP = {36:"bass_drum",38:"snare",40:"snare",41:"low_tom",
                             43:"mid_tom",47:"mid_tom",50:"hi_tom",45:"hi_tom",
                             48:"hi_tom",37:"rimshot",39:"clap",42:"closed_hat",
                             44:"closed_hat",46:"open_hat",49:"cymbal",
                             51:"ride",53:"ride_bell",59:"ride",   # 2026-09-23 voices
                             55:"splash",48:"himid_tom",
                             57:"cymbal",56:"cowbell"}
        voice_idx = {v: i for i, v in enumerate(self._edit_voices)}
        self._note_to_voice_idx = {}
        for note, vname in DRUM_NOTE_MAP.items():
            if vname in voice_idx:
                self._note_to_voice_idx[note] = voice_idx[vname]

        # Voice-note order for track-edit voice cycling (drums only — the sample
        # slots relocated to the sampler section 2026-07-21).
        self._voice_note_order = [(36,"bass_drum"),(38,"snare"),(41,"low_tom"),
                    (43,"mid_tom"),(50,"hi_tom"),(37,"rimshot"),(39,"clap"),
                    (42,"closed_hat"),(46,"open_hat"),(49,"cymbal"),(51,"ride"),
                    (53,"ride_bell"),(55,"splash"),(48,"himid_tom"),(56,"cowbell")]
        self._voice_display = {n: v.replace("_", " ")
                               for n, v in self._voice_note_order}

        # -- Parameter scaling (log / linear) ---------------------------------
        # Controls whether drum voice params and outboard CCs use a log curve
        # (more resolution at low values -- natural for volume, envelopes, filter)
        # or stay linear.  Set per parameter name in PARAM_SCALING and per CC
        # number in CC_SCALING.  "log" = pow 0.4 curve; "exp" = pow 2.5 curve.
        self._param_scaling = fcfg.get("PARAM_SCALING", {})
        self._cc_scaling    = fcfg.get("CC_SCALING",    {})

        # -- Seed the drum editor from the SYNTH's saved params ----------------
        # The synth re-applies ~/.orc1_drum_params.json at every boot, but the
        # Fire's _voice_vals (above) was hardcoded to defaults and never loaded
        # that file. So the EDIT screen showed level100/tune64/attack10/decay70
        # for every voice regardless of what was saved -- the "numbers keep
        # coming back" report -- and worse, dialing a knob nudged from that
        # wrong baseline and shipped it, overwriting the real saved value on a
        # restart-heavy workflow. Overlay the saved values here so the display
        # matches the synth. The file stores the CC value SENT (post-curve);
        # _voice_vals holds the PRE-curve display value, so invert the curve
        # (log<->exp, linear identity) so shown value -> _send_drum_param round-
        # trips back to the saved CC. (2026-08-17, mirrors the session-18 web
        # /drums fix; the Fire never got it.)
        _INV_CURVE = {"log": "exp", "exp": "log", "linear": "linear"}
        try:
            from orc1_synth import _load_drum_params as _ldp
            _saved_dp = _ldp()
        except Exception:
            _saved_dp = {}
        if isinstance(_saved_dp, dict):
            for _v, _ps in _saved_dp.items():
                _slot = self._voice_vals.get(_v)
                if not isinstance(_slot, dict) or not isinstance(_ps, dict):
                    continue
                for _p, _cc in _ps.items():
                    if _p not in _slot:
                        continue
                    try:
                        _inv = _INV_CURVE.get(self._param_scaling.get(_p, "linear"),
                                              "linear")
                        _slot[_p] = self._apply_curve(int(_cc), _inv)
                    except Exception:
                        pass

        # -- Sequencer ---------------------------------------------------------
        # 2026-07-01 ownership refactor: Sequencer is now engine-owned --
        # constructed in orc1.py's main() (after arp, before FireHandler) so
        # it exists with zero Fire dependency (Bug A) and the arp-unified-
        # clock subscribe actually engages (Bug C). FireHandler just borrows
        # the existing instance and wires its own render-dirty flag onto it.
        self._stop         = threading.Event()
        self._render_dirty = threading.Event()
        self.seq = eng.seq
        self.seq.step_callback = self._render_dirty.set
        # Keep self.tracks as a direct alias so all existing rendering/editing
        # (read) code continues to work without changes. Writes go through
        # self.seq's lock-guarded methods now, not this alias (Bug B fix).
        self.tracks = self.seq.tracks

        # -- Restore persisted Fire state --------------------------------------
        # Deliberately the LAST thing before the render threads: every
        # attribute the loader writes (_cc_vals, _fx_param_vals, chip_voice,
        # _dialmap, _mpc_faders, _universal_pads) now exists, so nothing it
        # restores can be clobbered by an initialiser further down, and
        # _sync_volume_fader_values() has a real _mpc_faders to work on.
        # Runs before _render()/_oled() below so the first frame already shows
        # the restored state.  (Moved here from ~line 595 — see the note there.)
        self._load_fire_state()

        # -- Render thread (Fire-specific: LED SysEx off the timing path) ------
        self._rt = threading.Thread(target=self._render_thread, daemon=True)
        self._rt.start()
        self._oled_ka = threading.Thread(target=self._oled_keepalive_loop, daemon=True)
        self._oled_ka.start()

        print("  ✓ Fire handler ready")
        print(f"    view={self.view}  tracks={self.n_tracks}  "
              f"out={'yes' if self._out else 'NO-FEEDBACK'}")
        self._render()
        self._oled()

    # -- Output / feedback -----------------------------------------------------

    @property
    def n_tracks(self):
        return len(self.tracks)

    @property
    def seq_running(self):
        """Read-only mirror of Sequencer.running -- keeps render/OLED code unchanged."""
        return self.seq.running

    def _toggle_pretty_lights(self):
        self._pretty_lights = not self._pretty_lights
        if self._pretty_lights:
            import threading as _thr, time as _time
            self._pl_mode = 1   # SPARKLE — the dark sparkly one, now the default
            self._pl_start = _time.time()
            self._pl_last_switch = _time.time()
            self._pl_thread = _thr.Thread(target=self._pretty_lights_loop, daemon=True)
            self._pl_thread.start()
            if fire_oled:
                bpm = self._bpm()
                fire_oled.show_lines([
                    (f"{bpm:.0f}", 2, 8, 4),
                    ("bpm", 50, 42, 1)])
        else:
            self._render()
            self._oled()

    def _pl_next_mode(self):
        """Advance to the next pretty lights visual. Plain STOP while active."""
        if not self._pretty_lights:
            return
        import time as _time
        self._pl_mode = (self._pl_mode + 1) % self._PL_N_MODES
        self._pl_last_switch = _time.time()   # reset auto-cycle timer
        if fire_oled:
            fire_oled.show_lines([(self._PL_MODE_NAMES[self._pl_mode], 2, 20, 3)])

    def _pretty_lights_loop(self):
        import time, math
        while self._pretty_lights:
            try:
                self._render_pretty_lights()
            except Exception:
                pass
            time.sleep(0.05)   # ~20fps

    # Display names per mode index -- single source of truth for the OLED label,
    # the mode count, and the dispatch order. Keep this list, _PL_SPEED_SCALE and
    # the dispatch in _render_pretty_lights index-aligned.
    _PL_MODE_NAMES = ["PLASMA", "SPARKLE", "SWEEP", "FLAME", "BEAT", "SPARSE",
                      "RAIN", "RIPPLE", "COMET", "SCANNER", "WAVE", "VU"]
    _PL_N_MODES = len(_PL_MODE_NAMES)

    # Per-mode speed multiplier applied on top of the shared tempo-locked clock.
    # FLAME was judged the right pace as-is; every other mode read as too fast at
    # the same rate, so they're scaled down independently rather than slowing the
    # shared clock (which would've slowed FLAME too). The new modes' scales are
    # the paces owner dialled in on the Pretty Lights Lab. Rideable live on top
    # of all of these via the master-speed knob (self._pl_speed, knob 0).
    _PL_SPEED_SCALE = {0: 0.4, 1: 0.4, 2: 0.4, 3: 1.0, 4: 0.4, 5: 0.4,
                       6: 0.5, 7: 0.5, 8: 0.6, 9: 0.5, 10: 0.4, 11: 0.4}

    def _render_pretty_lights(self):
        import time, math
        bpm = self._bpm()
        spd = (bpm / 60.0) * 3.0    # tempo-locked: 1 beat = 3 t-units
        m = self._pl_mode
        t = ((time.time() - self._pl_start) * spd
             * self._PL_SPEED_SCALE.get(m, 1.0) * self._pl_speed)
        if   m == 0:  self._render_pl_plasma(t, math)
        elif m == 1:  self._render_pl_sparkle(t, math)
        elif m == 2:  self._render_pl_sweep(t, math)
        elif m == 3:  self._render_pl_flame(t, math)
        elif m == 4:  self._render_pl_beat(t, math)
        elif m == 5:  self._render_pl_sparse(t, math)
        elif m == 6:  self._render_pl_rain(t, math)
        elif m == 7:  self._render_pl_ripple(t, math)
        elif m == 8:  self._render_pl_comet(t, math)
        elif m == 9:  self._render_pl_scanner(t, math)
        elif m == 10: self._render_pl_wave(t, math)
        else:         self._render_pl_vu(t, math)

    def _render_pl_plasma(self, t, math):
        """Original psychedelic sine-wave field."""
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                v = (math.sin(col * 0.55 + t) +
                     math.sin(row * 1.4  + t * 1.3) +
                     math.sin((col + row) * 0.45 + t * 0.8) +
                     math.sin(math.sqrt(col*col + row*row + 0.5) * 0.7 + t * 1.1))
                hue = (v * 0.125 + 0.5)
                brightness = 0.6 + 0.4 * math.sin(t * 2.5 + col * 0.4 + row * 0.9)
                r, g, b = _hsv(hue, 1.0, brightness)
                cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    def _render_pl_sparkle(self, t, math):
        """Deep-space starfield: each pad blinks at its own tempo."""
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                # Unique blink rate and phase per cell, derived from position
                rate   = 0.7 + ((row * 5 + col * 11 + row * col) % 19) * 0.09
                offset = (row * 7.3 + col * 13.7) % (2 * math.pi)
                # cos^8 makes very narrow bright spikes with long dark gaps
                spark  = max(0.0, math.cos(t * rate + offset)) ** 8
                if spark < 0.02:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    hue = (offset / (2 * math.pi) + t * 0.01) % 1.0
                    r, g, b = _hsv(hue, 0.6, spark)
                    cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    def _render_pl_sparse(self, t, math):
        """SPARKLE variant: fewer pads lit at once, harder on/off contrast.
        Same starfield idea as SPARKLE but with a much narrower spike (higher
        power), a higher trigger floor so only the true peak of each pad's
        cycle lights at all, and full-white/near-black instead of a hue wash
        -- max sparseness, max contrast."""
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                rate   = 0.5 + ((row * 5 + col * 11 + row * col) % 19) * 0.06
                offset = (row * 7.3 + col * 13.7) % (2 * math.pi)
                spark  = max(0.0, math.cos(t * rate + offset)) ** 20
                if spark < 0.35:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    hue = (offset / (2 * math.pi)) % 1.0
                    val = min(1.0, spark * 1.3)
                    r, g, b = _hsv(hue, 0.25, val)
                    cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    def _render_pl_sweep(self, t, math):
        """Columns of rainbow light scrolling left-to-right, rows offset for depth."""
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                # Hue advances with column and scrolls over time
                hue = (col / N_COLS * 0.75 + t * 0.04 + row * 0.06) % 1.0
                # Brightness wave travels across the grid
                brightness = 0.45 + 0.55 * (math.sin(col * 0.45 - t * 1.8 + row * 0.7) * 0.5 + 0.5)
                r, g, b = _hsv(hue, 1.0, brightness)
                cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    def _render_pl_flame(self, t, math):
        """Heat rising from a solid bottom row; flames flicker up and cool toward
        the top. Renamed from FIRE 2026-09-05 (the word 'fire' collides with the
        Fire controller / project name everywhere -- FLAME is unambiguous).
        Fix history (the grid is only 4 rows tall, which makes the endpoints
        brutal):
          1. Original: heat_base = (N_ROWS-1-row)/(N_ROWS-1) == 0.0 at the BOTTOM
             row -- the opposite of its own comment -- so row 3 was always black.
          2. First fix: heat_base = row/(N_ROWS-1) lit the bottom but left the TOP
             row (row 0) dead, because a linear ramp is exactly 0.0 at row 0 and
             the flicker was scaled BY heat_base (0 * anything = 0).
          3. Now: heat_base ramps 0.35 -> 1.0 (never 0 at the top), and the flicker
             is ADDED (not multiplied) with a downward bias near the top, so the
             bottom stays a solid base while flames lick up to the top row
             intermittently -- lit some frames, dark others, like real embers.
        The BOTTOM row's math is unchanged from fix #2's shipped bottom (heat_base
        1.0, so heat = 1.0 + flicker), so the base row owner liked is preserved."""
        cells = []
        for row in range(N_ROWS):
            frac = row / (N_ROWS - 1)          # 0.0 top .. 1.0 bottom
            for col in range(N_COLS):
                heat_base = 0.35 + 0.65 * frac  # 0.35 at top -> 1.0 at bottom
                # Flickering combines fast and slow turbulence (slowed down
                # so individual flickers are actually visible).
                flicker = (math.sin(col * 2.1 + t * 3.5) * 0.28 +
                           math.sin(col * 0.8 + t *  2.0 + row * 0.6) * 0.18 +
                           math.sin(col * 3.8 + t *  1.5) * 0.12)
                # Additive flicker, biased down toward the top so upper rows only
                # get intermittent licks while the bottom is a steady base.
                heat = heat_base + flicker - 0.30 * (1 - frac)
                heat = max(0.0, min(1.0, heat))
                if heat < 0.04:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    # hue 0=red → 0.167=yellow as heat rises
                    hue = heat * 0.167
                    val = heat ** 0.55   # gamma: mid-heat stays visible
                    r, g, b = _hsv(hue, 1.0, val)
                    cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    def _render_pl_beat(self, t, math):
        """BPM-locked pulse wave rolling left to right; color shifts each bar."""
        # t is tempo-locked: 1 beat = 3 units, 1 bar (4 beats) = 12 units
        beat_phase = (t / 3.0) % 1.0          # 0→1 per beat
        bar_hue    = (t / 12.0 * 0.25) % 1.0  # hue rotates over 4 bars
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                # Each column is offset by a tiny fraction of a beat → wave propagates right
                col_delay = col * (0.6 / N_COLS)
                local_phase = (beat_phase - col_delay) % 1.0
                # Sharp attack (exp decay) plus a gentler background shimmer
                pulse = math.exp(-local_phase * 5.5)
                shimmer = 0.08 * (math.sin(t * 0.9 + row * 1.1 + col * 0.3) * 0.5 + 0.5)
                brightness = min(1.0, pulse + shimmer)
                hue = (bar_hue + row * 0.25) % 1.0
                r, g, b = _hsv(hue, 1.0, brightness)
                cells.append((pad_index(row, col), r, g, b))
        self._send_pads(cells)

    # -- New modes (2026-09-05): designed/tuned on the Pretty Lights Lab, then
    # ported back here in the same pure (row, col, t) style as the originals.
    def _render_pl_rain(self, t, math):
        """Matrix downpour: one bright-headed green droplet + trail per column."""
        TRAIL, SPAN = 3.0, N_ROWS + 3
        cells = []
        for col in range(N_COLS):
            rate = 0.8 + ((col * 7) % 5) * 0.12
            head = (t * rate + col * 1.7) % SPAN
            for row in range(N_ROWS):
                d = head - row
                if d < 0 or d > TRAIL:
                    cells.append((pad_index(row, col), 0, 0, 0))
                    continue
                b = 1 - d / TRAIL
                if d < 0.55:                                   # near-white head
                    r, g, bl = _hsv(0.34, 0.25, min(1.0, b * 1.2))
                else:                                          # green tail
                    r, g, bl = _hsv(0.34, 0.95, b * 0.9)
                cells.append((pad_index(row, col), r, g, bl))
        self._send_pads(cells)

    def _render_pl_ripple(self, t, math):
        """Concentric rings expanding from a slowly drifting centre."""
        cx = 7.5 + 6 * math.sin(t * 0.5)
        cy = 1.5 + 1.2 * math.sin(t * 0.37)
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                dy = (row - cy) * 2.2   # scale rows -- the grid is 4x wider than tall
                dx = col - cx
                dist = math.sqrt(dx * dx + dy * dy)
                b = max(0.0, math.sin(dist * 0.9 - t * 2.0)) ** 2
                if b < 0.03:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    hue = (dist * 0.03 + t * 0.05) % 1.0
                    r, g, bl = _hsv(hue, 0.85, b)
                    cells.append((pad_index(row, col), r, g, bl))
        self._send_pads(cells)

    def _render_pl_comet(self, t, math):
        """A bright head tracing a snake path with a fading tail."""
        N, TL = N_ROWS * N_COLS, 11.0
        head = (t * 6.0) % N
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                # boustrophedon index: reverse the column on odd rows so the
                # path is one continuous ribbon across the whole grid.
                idx = row * N_COLS + ((N_COLS - 1 - col) if (row % 2) else col)
                d = (head - idx) % N
                if d > TL:
                    cells.append((pad_index(row, col), 0, 0, 0))
                    continue
                b = (1 - d / TL) ** 1.6
                hue = (idx / N + t * 0.1) % 1.0
                if d < 1.0:                                    # white-hot head
                    r, g, bl = _hsv(hue, 0.15, min(1.0, b * 1.1))
                else:
                    r, g, bl = _hsv(hue, 1.0, b)
                cells.append((pad_index(row, col), r, g, bl))
        self._send_pads(cells)

    def _render_pl_scanner(self, t, math):
        """A red bar sweeping side to side (KITT)."""
        p = (t * 3.0) % (2 * (N_COLS - 1))
        x = (2 * (N_COLS - 1) - p) if p > (N_COLS - 1) else p
        cells = []
        for col in range(N_COLS):
            b = max(0.0, 1 - abs(col - x) / 3.0) ** 2
            for row in range(N_ROWS):
                if b < 0.02:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    r, g, bl = _hsv(0.015, 0.9, b)
                    cells.append((pad_index(row, col), r, g, bl))
        self._send_pads(cells)

    def _render_pl_wave(self, t, math):
        """An ocean crest rolling across; cyan at the surface into blue."""
        cells = []
        for col in range(N_COLS):
            surface = 1.5 + 1.3 * math.sin(col * 0.5 - t * 1.5)
            for row in range(N_ROWS):
                b = max(0.0, 1 - abs(row - surface) / 1.6)
                if b < 0.05:
                    cells.append((pad_index(row, col), 0, 0, 0))
                else:
                    hue = 0.55 - 0.08 * b
                    r, g, bl = _hsv(hue, 0.8, 0.15 + 0.85 * b)
                    cells.append((pad_index(row, col), r, g, bl))
        self._send_pads(cells)

    def _render_pl_vu(self, t, math):
        """Per-column spectrum bars, green at the bottom rising to red."""
        cells = []
        for col in range(N_COLS):
            h = ((math.sin(col * 1.3 + t * 2.0) * 0.5 + 0.5) * 0.6 +
                 (math.sin(col * 0.7 - t * 3.1) * 0.5 + 0.5) * 0.4)
            level = h * N_ROWS
            for row in range(N_ROWS):
                from_bottom = N_ROWS - 1 - row
                if from_bottom < level:
                    frac = from_bottom / (N_ROWS - 1)
                    r, g, bl = _hsv(0.33 * (1 - frac), 1.0, 0.85)
                    cells.append((pad_index(row, col), r, g, bl))
                else:
                    cells.append((pad_index(row, col), 0, 0, 0))
        self._send_pads(cells)

    def _oled_keepalive_loop(self):
        """Periodically re-sends the current OLED frame so the Fire hardware
        screensaver never blanks the display mid-session.

        The Fire's hardware screensaver fires in roughly 10s of MIDI silence.
        The keepalive interval must be well under that threshold.  8s gives
        a comfortable 2s margin.  State saves run on a separate 60s cycle so
        the SD card is not hammered on every keepalive tick.

        fire_oled.send_image deduplicates identical content within 20s.  We
        clear that timestamp dict before each keepalive so the SysEx always
        goes out regardless of how recently the frame was last sent."""
        import time as _time
        interval   = float(self.cfg.FIRE.get("OLED_KEEPALIVE_S", 8))
        save_every = 60.0
        _last_save = _time.monotonic()
        while not self._stop.wait(interval):
            try:
                # Bypass send_image dedup so this always transmits.
                if fire_oled:
                    fire_oled.send_image.__defaults__[-1].clear()  # clears _last_t
                self._oled()
                now = _time.monotonic()
                if now - _last_save >= save_every:
                    self._save_fire_state()
                    _last_save = now
            except Exception as _ka_e:
                print(f"  ! keepalive: {_ka_e}", flush=True)

    def rewake(self):
        """Bring the Fire fully back after a USB replug (brain hot-plug path):
        a fresh LED output, a fresh OLED output (fire_oled caches its port by
        NAME and the replugged Fire reuses the name, so without the reset the
        screen stays dark forever), then repaint pads AND screen -- exactly
        what __init__ does at startup."""
        if self._out is not None:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None
        self._open_output()
        if fire_oled:
            try:
                fire_oled.reset_output()
            except Exception as e:
                print(f"  ! Fire OLED reset: {e}", flush=True)
        self._render()
        self._oled()
        # A just-replugged Fire is still booting its firmware for a moment and
        # IGNORES what arrives then -- measured 2026-09-28: the wake paint went
        # out 0.7 s after enumeration and the Fire stayed in its boot animation
        # until a button press repainted it. So repaint pads + screen a few
        # more times over the next ~4 s; whichever lands after boot wakes it.
        def _late_repaints():
            for delay in (0.6, 1.2, 2.2):
                if self._stop.wait(delay):
                    return
                try:
                    if fire_oled:
                        fire_oled.forget_sent()
                    self._render()
                    self._oled()
                except Exception as e:
                    print(f"  ! Fire late repaint: {e}", flush=True)
        threading.Thread(target=_late_repaints, daemon=True,
                         name="fire-rewake").start()

    def _open_output(self):
        """The Fire's output, claimed by NAME (cfg.is_fire_port: Akai's own
        firmware or our firmware's host mode)."""
        if not mido:
            return
        try:
            for n in mido.get_output_names():
                if self.cfg.is_fire_port(n):
                    self._out = mido.open_output(n)
                    return
        except Exception as e:
            print(f"  ! Fire output open failed: {e}")

    def _send_pads(self, cells):
        if not self._out or not cells:
            return
        payload = []
        for idx, r, g, b in cells:
            payload += [idx & 0x3F, r & 0x7F, g & 0x7F, b & 0x7F]
        n = len(payload)
        data = [0x47, 0x7F, 0x43, 0x65, (n >> 7) & 0x7F, n & 0x7F] + payload
        # Share fire_oled's IO_LOCK so an LED-grid SysEx can never interleave on
        # the wire with an OLED frame SysEx (different port + thread) -- that
        # interleave is what glitched the OLED when a knob was turned mid-play.
        # Falls back to the per-handler lock only if fire_oled is unavailable.
        lock = fire_oled.IO_LOCK if fire_oled else self._out_lock
        with lock:
            try:
                self._out.send(mido.Message("sysex", data=data))
            except Exception:
                pass

    def _track_color(self, tidx):
        # Color is baked into the track dict at creation; it travels with the
        # track when reordered rather than being re-derived from position.
        # Brightness (0-127, default full) is a cosmetic render multiplier applied
        # HERE so every pad render -- playhead, on/off, dim -- inherits it in one
        # place; the stored colour stays pristine so the hue dial / web picker keep
        # editing the true hue. bright >= 127 short-circuits to the exact old value.
        t = self.tracks[tidx]
        color = t["color"]
        bright = int(t.get("bright", 127))
        if bright >= 127:
            return color
        return tuple(max(0, min(0x7F, int(c) * bright // 127)) for c in color)

    def _render(self):
        if self._pretty_lights:
            return
        # Session E (2026-06-30): subscreen replaces the normal per-view grid
        # entirely while active (momentary or latched) -- matches the plan's
        # "mode-jump pads render ONLY when subscreen is active" rule.
        if self._subscreen or self._subscreen_latched:
            return self._render_subscreen()
        # KEY overlay (2026-07-28): ALT held on a keyboard view momentarily
        # replaces the grid with the root/scale picker.
        if self._key_overlay_active():
            return self._render_key_overlay()
        if self.view == "seq":
            self._render_channel()
        elif self.view == "mixer":
            self._render_mixer()
        elif self.view == "chord":
            self._render_perform()
        elif self.view == "poly":
            self._render_poly()
        elif self.view == "chip":
            self._render_chip()
        elif self.view == "faders":
            self._render_faders()
        elif self.view == "universal":
            self._render_universal()
        elif self.view == "fx":
            self._render_fx()

    # -- Session E (2026-06-30): subscreen framework --------------------------

    def _update_subscreen(self):
        """SHIFT+ALT toggles the mode-jump subscreen latch.
        Rising edge (both modifiers now held):
          - Not open yet → open AND latch (stays open when modifiers released).
          - Already latched → close (toggle off, no pending fire).
        Releasing modifiers while latched → no-op (subscreen stays open).
        Pad press while latched fires the jump immediately and closes
        (handled by _subscreen_pad_press's latched branch).
        Safety: closes any un-latched subscreen on modifier release (encoder-
        remap flow can still unlatch and re-open as before via BROWSER)."""
        both = self._shift and self._alt
        if both:
            if not self._subscreen_latched:
                # Toggle in: open and latch so it survives modifier release.
                self._subscreen_pending_view = None
                self._subscreen              = True
                self._subscreen_latched      = True
                self._render()
                self._oled()
            else:
                # Toggle out: second SHIFT+ALT dismisses without jumping.
                self._subscreen              = False
                self._subscreen_latched      = False
                self._subscreen_pending_view = None
                self._render()
                self._oled()
        elif self._subscreen and not self._subscreen_latched:
            # Safety: close any un-latched subscreen on modifier release.
            self._subscreen = False
            pending = self._subscreen_pending_view
            self._subscreen_pending_view = None
            if pending:
                if self._overlay in ("kmap", "kmap_cc"):
                    self._overlay = None
                self._set_view(pending)
            else:
                self._render()
                self._oled()

    def _render_subscreen(self):
        """Row 3 cols 8-15 = mode-jump pads (all 8 modes). Everything else blank.
        The queued/pending pad dims to grey-white to confirm selection."""
        cells = [(pad_index(r, c), *OFF) for r in range(N_ROWS) for c in range(N_COLS)]
        pending = self._subscreen_pending_view
        for i, (name, color) in enumerate(_SUBSCREEN_JUMP):
            col = 8 + i
            rgb = (0x30, 0x30, 0x30) if name == pending else color
            cells[pad_index(N_ROWS - 1, col)] = (pad_index(N_ROWS - 1, col), *rgb)
        self._send_pads(cells)

    def _subscreen_pad_press(self, row, col):
        """Mode-jump row (row 3, cols 8-15) -- all 8 modes.
        Subscreen is always latched when opened via SHIFT+ALT toggle, so pad
        press always fires the jump immediately and closes the subscreen."""
        if row == N_ROWS - 1 and 8 <= col <= 15:
            name, _color = _SUBSCREEN_JUMP[col - 8]
            if self._subscreen_latched:
                # Latched: immediate jump + close.
                self._subscreen         = False
                self._subscreen_latched = False
                if self._overlay in ("kmap", "kmap_cc"):
                    self._overlay = None
                self._set_view(name)
            else:
                # Un-latched safety path (shouldn't occur in normal flow):
                # queue + preview; jump fires on modifier release.
                self._subscreen_pending_view = name
                self._render_subscreen()
                self._oled_subscreen()

    def _oled_subscreen(self):
        if not fire_oled:
            return
        try:
            pending = self._subscreen_pending_view
            if pending:
                label = {"faders": "FADERS"}.get(pending, pending.upper())
                fire_oled.show_lines([
                    ("-> " + label, 2, 0, 2),
                    ("release to jump", 2, 28, 1),
                    ("tap another=swap", 2, 44, 1)])
            else:
                fire_oled.show_lines([
                    ("JUMP TO MODE", 2, 0, 2),
                    ("tap a pad=queue", 2, 28, 1),
                    ("release SH/AL=go", 2, 44, 1)])
        except Exception:
            pass

    def _fx_rack(self):
        """The engine's 3-slot FX rack (RackManager) or None if unavailable."""
        return getattr(getattr(self.eng, "fx", None), "rack", None)

    def _fx_after_change(self):
        """Repaint pads + OLED after an async rack rebuild (assign/toggle/move).

        Passed as the rack's done= callback, so it runs on the rack worker
        thread once jalv has finished (re)wiring — same safe pattern the legacy
        chain code used.  No-op if the user has since left the FX view."""
        if self.view == "fx" and not self._overlay:
            try:
                self._render_fx()
                self._oled()
            except Exception:
                pass

    def _render_fx(self):
        """FX RACK view -- the 3 series slots on the bottom row + a ZOIA-style
        parameter visualisation above.  This drives eng.fx.rack (the modern
        3-slot series rack), NOT the legacy fx_chains presets.

        Row 3 (bottom): one pad per rack slot (cols 0..cap-1, cap = 3).
            Very dim grey  = empty slot (no plugin assigned)
            Dim amber      = plugin assigned but disabled/not running (stopped)
            Bright green   = slot enabled and its jalv is running
            White boost    = currently selected slot (stacks on top)

        Rows 0-2, cols 0-3: the selected slot's parameters as a 3×4 grid, one
        row per encoder layer (bare/shift/alt -> controls[0-3]/[4-7]/[8-11]).
        Pad brightness tracks each parameter's current value; the active layer
        row glows brighter.  Only shown while the selected slot is running.

        Rows 0-2, cols 8-15: mode-jump pads (unchanged).

        Controls:
            Pad press (row 3)   = SELECT this slot (adjust its params, no toggle)
            SHIFT + pad press   = toggle the slot on/off (rack.toggle)
            Hold A + press B    = swap the two slots' plugins (rack.move)
            Press an empty slot = open the plugin browser (banked)
            Encoders            = live param on the selected running slot
        """
        cells = []
        rack  = self._fx_rack()
        st    = rack.status() if rack else {"cap": 3, "slots": []}
        cap   = int(st.get("cap", 3))
        slots = st.get("slots", [])

        # Keep the selection inside the rack's slot count.
        if self._fx_sel_slot >= cap:
            self._fx_sel_slot = max(0, cap - 1)

        sel        = slots[self._fx_sel_slot] if self._fx_sel_slot < len(slots) else None
        # Show the param grid whenever the slot HAS a plugin (not only when
        # running) so a stopped-but-assigned slot still visualises + can be
        # tweaked — the values come from the rack's memory either way.
        sel_has    = bool(sel and sel.get("plugin"))
        sel_ctrls  = sel.get("controls", []) if sel else []

        # Base colour for each layer's row at full brightness.
        _ROW_COLOR  = {2: (0x45, 0x20, 0x00),   # row 2 = bare  -> warm amber
                       1: (0x00, 0x2C, 0x28),   # row 1 = shift -> teal
                       0: (0x28, 0x00, 0x38)}    # row 0 = alt   -> violet
        _LAYER_OFF  = {2: 0, 1: 4, 0: 8}         # row -> controls[] base offset
        active_row  = {"base": 2, "shift": 1, "alt": 0}[self._layer_now()]

        for row in range(N_ROWS - 1):   # rows 0, 1, 2
            for col in range(N_COLS):
                # Cols 8-15: mode-jump pads mirroring the subscreen colours.
                if col >= 8:
                    j = col - 8
                    if j < len(_SUBSCREEN_JUMP):
                        _jname, _jcolor = _SUBSCREEN_JUMP[j]
                        _scale = 0.35 if _jname == self.view else 0.55
                        cells.append((pad_index(row, col),
                                      int(_jcolor[0] * _scale),
                                      int(_jcolor[1] * _scale),
                                      int(_jcolor[2] * _scale)))
                    else:
                        cells.append((pad_index(row, col), *OFF))
                    continue
                # Cols 4-7: unused gap — dark.
                # Only cols 0-3 carry param pads (and only when a plugin is loaded).
                if not sel_has or col >= 4:
                    cells.append((pad_index(row, col), *OFF))
                    continue

                # Map (row, col) -> control index in the selected plugin.
                ctrl_idx = _LAYER_OFF[row] + col          # 0-11
                if ctrl_idx >= len(sel_ctrls):
                    # No param at this position -- barely visible so the grid reads.
                    cells.append((pad_index(row, col), 0x02, 0x02, 0x02))
                    continue

                c     = sel_ctrls[ctrl_idx]
                p_min = float(c.get("min", 0.0))
                p_max = float(c.get("max", 1.0))
                cur   = float(c.get("value", c.get("default", (p_min + p_max) / 2.0)))
                frac  = (cur - p_min) / (p_max - p_min) if p_max > p_min else 0.5
                frac  = max(0.0, min(1.0, frac))

                scale = (0.35 + 0.65 * frac) if row == active_row else (0.10 + 0.35 * frac)
                br, bg, bb = _ROW_COLOR[row]
                cells.append((pad_index(row, col),
                              int(min(0x7F, br * scale)),
                              int(min(0x7F, bg * scale)),
                              int(min(0x7F, bb * scale))))

        # -- Row 3 (bottom): one pad per rack slot --------------------------------
        for col in range(N_COLS):
            s = slots[col] if col < len(slots) else None
            if col >= cap or s is None:
                c = OFF                          # beyond the rack — dark
            elif not s.get("plugin"):
                c = (0x02, 0x02, 0x02)           # empty slot — very dim grey
            elif s.get("running"):
                c = (0x00, 0x4A, 0x14)           # green — enabled + running
            else:
                c = (0x18, 0x0C, 0x00)           # dim amber — assigned but off
            if col < cap and col == self._fx_sel_slot:
                c = (min(0x7F, c[0] + 0x40),
                     min(0x7F, c[1] + 0x40),
                     min(0x7F, c[2] + 0x20))
            cells.append((pad_index(N_ROWS - 1, col), *c))

        self._send_pads(cells)

    def _render_channel(self):
        cells    = []
        base_col = self.page * N_COLS
        for row in range(N_ROWS):
            tidx = self.view_top + row
            for col in range(N_COLS):
                if tidx >= self.n_tracks:
                    cells.append((pad_index(row, col), *OFF))
                    continue
                trk  = self.tracks[tidx]
                step = base_col + col
                color = self._track_color(tidx)
                sn    = trk.get("step_notes")
                has_override = bool(sn and step < len(sn) and sn[step] is not None)

                if step >= trk["length"]:
                    c = OFF
                elif step == trk["pos"] and self.seq_running:
                    c = _scale(color, 4, 2) if trk["steps"][step] else PLAYHEAD
                elif trk["steps"][step]:
                    sv = trk.get("step_vels")
                    ov = sv[step] if sv and step < len(sv) else None
                    if has_override:
                        # Step has its own note: warm white tint so the grid
                        # shows you which steps are melodically interesting
                        c = tuple(min(0x7F, x + 0x28) for x in color)
                    elif ov is not None:
                        # Per-step velocity: brightness tracks the value, so
                        # ghost steps read dim and accent steps read bright.
                        c = _vel_tint(color, ov)
                    else:
                        c = color
                else:
                    c = _scale(color, 1, 5 if tidx == self._sel_track else 8)
                if trk["mute"]:
                    c = _scale(c, 1, 8)
                cells.append((pad_index(row, col), *c))
        self._send_pads(cells)

    def _render_mixer(self):
        cells = []
        for row in range(N_ROWS):
            tidx = self.view_top + row
            for col in range(N_COLS):
                if tidx >= self.n_tracks:
                    cells.append((pad_index(row, col), *OFF))
                    continue
                trk = self.tracks[tidx]
                lit = int(trk["vel"] / 127.0 * N_COLS)
                color = self._track_color(tidx)
                c = color if col < lit else _scale(color, 1, 10)
                if trk["mute"]:
                    c = _scale(c, 1, 8)
                cells.append((pad_index(row, col), *c))
        self._send_pads(cells)

    def _render_perform(self):
        """Paint the new perform view.

        Left zone (cols 0-3): 4×4 chord-engine control panel.
          Row 0 -- bases:      MAJ / MIN / SUS / DIM   (active base glows its colour)
          Row 1 -- extensions: 6th / 7th / 9th / 11th  (amber = on, from the scale)
                               SHIFT = 6 / m7 / M7 / 9 chromatic (magenta = on)
          Row 2 -- modifiers:  INV- / INV+ / OCT- / OCT+
          Row 3 -- utility:    CLR / PREV / HOLD / ARP

        Right zone (cols 4-15): chromatic keyboard, 4 full octaves.
          Bottom row (3) = lowest, top row (0) = highest.
          Within each row: col 4 = C, col 5 = C#, … col 15 = B.
          White keys: dim warm white.  Black keys: near-off.
          Current root pitch class: faint chord-colour tint across its column.
          Held pads: bright chord colour.
        """
        st     = self.eng.state
        arp    = getattr(self.eng, "arp", None)
        # Show the base that will actually SOUND, not the stored one -- quality
        # is diatonic unless a base pad has overridden it, so on a non-CHROM
        # scale the lit pad follows the root's scale degree.
        base   = self._effective_base()
        exts   = st.get("chord_exts", set())
        hold   = bool(st.get("hold", False))
        arp_on = bool(arp and arp.enabled)
        # The KEY tonic (not the chord root) -- tinting the key's column is the
        # stable landmark now that the two are separate variables.
        root_pc = st.get("root", 0) % 12        # key tonic pitch class 0-11

        chord_bright = _PERF_BASE_BRIGHT.get(base, (0x30, 0x30, 0x30))

        # Black-key pitch classes within an octave (relative to C)
        _BLACK = {1, 3, 6, 8, 10}

        cells = []

        # -- Control section (cols 0-3) -------------------------------------
        for row in range(N_ROWS):
            for col in range(4):
                kind, param = _PERF_CTRL_LAYOUT[row][col]
                c = _perf_ctrl_color(kind, param, base, exts, hold, arp_on)
                cells.append((pad_index(row, col), *c))

        # -- Keyboard section (cols 4-15) --------------------------------------
        # Each row can be chord (original), direct (raw MIDI to port/ch), or off.
        for row in range(N_ROWS):
            rcfg  = self._chord_cfg(row)
            rtype = rcfg.get("type", "chord")
            pk    = rcfg.get("port") or "pisound"   # port key for direct rows
            for col in range(4, N_COLS):
                pitch    = self._row_pitch(row, col)
                pitch_pc = pitch % 12
                padnote  = PAD_NOTE_BASE + row * N_COLS + col
                pidx     = pad_index(row, col)

                if rtype == "off":
                    cells.append((pidx, *_ROW_COL_OFF))
                    continue

                # Pick per-type colour scheme
                if rtype == "chord":
                    wk = _ROW_COL_CHORD          # white key
                    bk = (2, 1, 1)               # black key
                    hl = chord_bright            # held
                    rt = tuple(max(2, c // 7) for c in chord_bright)   # root tint
                else:  # direct
                    wk = _ROW_COL_DIRECT.get(pk, (4, 6, 4))
                    bk = (1, 1, 2)
                    hl = tuple(min(63, c * 6) for c in wk)
                    rt = wk   # no root-column tint on direct rows

                is_held = (padnote in self._pad_held) or (padnote in self._dir_held)
                if is_held:
                    cells.append((pidx, *hl))
                elif rtype == "chord" and pitch_pc == root_pc:
                    cells.append((pidx, *rt))
                elif pitch_pc in _BLACK:
                    cells.append((pidx, *bk))
                else:
                    cells.append((pidx, *wk))

        self._send_pads(cells)

    def _chord_color(self):
        return _PERF_BASE_BRIGHT.get(
            self.eng.state.get("chord_base", "MAJ"), (0x30, 0x30, 0x30))

    def _perform_ctrl_press(self, row, col, vel):
        """Handle a pad press in the 4-col control section of the perform view.

        Bases (row 0): set chord base via handle_ctrl_note, same as an external
          keyboard sending to CH_CTRL.  Inversion resets on base change.

        Extensions (row 1): MOMENTARY.  Press = add extension; release = remove.
          The padnote is stored in _perf_ext_held so _pad_release can send the
          matching handle_ctrl_note_off.  (Changed from sticky toggle per D3.)

        Modifiers (row 2): INV-/INV+ go through handle_ctrl_note as normal.
          Cols 2-3 are freed (kind "none") -- octave lives on PAT up/down now.

        Utility (row 3): CLR/PREV through handle_ctrl_note.  HOLD and ARP are
          direct engine-state toggles since they're not in CTRL_MOD_NOTES.
        """
        if row >= 4 or col >= 4:
            return
        kind, param = _PERF_CTRL_LAYOUT[row][col]

        if kind == "base":
            note = self._perf_base_note.get(param)
            if note is not None:
                try: self.eng.handle_ctrl_note(note, vel)
                except Exception as e: print(f"  ! Fire perform ctrl-note: {e}", flush=True)

        elif kind == "ext":
            # Momentary: press = add extension, release = remove (via _perf_ext_held).
            # The padnote (Fire's raw note number) is the key; the ctrl_note
            # (the engine's CH_CTRL note for this extension) is the value.
            #
            # SHIFT selects the CHROMATIC vocabulary for this column -- the
            # deliberate off-scale escape hatch.  Plain press uses the scale's
            # own ladder.  Which one was pressed is remembered per-pad by
            # _perf_ext_held, so the release removes the SAME extension that was
            # added even if SHIFT is let go first.
            if self._shift:
                try:
                    param = _PERF_EXT_CHROM[_PERF_EXT_DEGREE.index(param)]
                except ValueError:
                    pass
            ctrl_note = self._perf_ext_note.get(param)
            if ctrl_note is not None:
                padnote = PAD_NOTE_BASE + row * N_COLS + col
                self._perf_ext_held[padnote] = ctrl_note
                try: self.eng.handle_ctrl_note(ctrl_note, vel)
                except Exception as e: print(f"  ! Fire perform ext ctrl-note: {e}", flush=True)

        elif kind == "mod":
            note = self._perf_mod_note.get(param)
            if note is not None:
                try: self.eng.handle_ctrl_note(note, vel)
                except Exception as e: print(f"  ! Fire perform ctrl-note: {e}", flush=True)

        elif kind == "clear":
            note = self._perf_mod_note.get("EXT_CLEAR")
            if note is not None:
                try: self.eng.handle_ctrl_note(note, vel)
                except Exception as e: print(f"  ! Fire perform ctrl-note: {e}", flush=True)

        elif kind == "prev":
            note = self._perf_mod_note.get("PREV_CHORD")
            if note is not None:
                try: self.eng.handle_ctrl_note(note, vel)
                except Exception as e: print(f"  ! Fire perform ctrl-note: {e}", flush=True)

        elif kind == "hold":
            # Toggle chord-hold directly in engine state.
            # "hold" = incoming note-offs for CH12 are ignored so the chord
            # sustains even after releasing the key.  Tap again to clear.
            st = self.eng.state
            st["hold"] = not st.get("hold", False)

        elif kind == "arp":
            # Toggle arpeggiator on/off.
            arp = getattr(self.eng, "arp", None)
            if arp:
                arp.set_enabled(not arp.enabled)
                if not arp.enabled:
                    try: self.eng.send_chord_off()
                    except Exception as e: print(f"  ! Fire arp-off chord-off: {e}", flush=True)

    # -- Chord-row routing helpers -------------------------------------------

    def _load_chord_rows(self, defaults):
        """Load the chord-row BANKS from ~/.orc1_chord_rows.json.

        Returns (banks, active): banks is a list of exactly _CHORD_N_BANKS
        row-configs, EACH padded to exactly _CHORD_ROWS_PER_BANK rows (rows past
        the configured ones are silent 'off' rows); active is the selected index.

        Format v2: {"v":2,"active":i,"banks":[[row,...], ...]} -- per-row octave
        is ABSOLUTE (no hidden (3-row)*12 auto-stack). A v2 bank saved with the
        old 4-row shape is upgraded to _CHORD_ROWS_PER_BANK by padding with off
        rows, so nothing is lost when the stack grew.

        Migration: a bare list of 4 rows is the pre-banks format, whose octave
        was RELATIVE (the play formula added (3-row)*12). Fold that in so the
        sound is preserved, then pad + seat as bank 0. Missing/corrupt banks are
        filled with copies of `defaults` (already absolute) padded the same way.
        """
        _OFF = {"type": "off", "port": None, "channel": None, "octave": 0}
        def _pad(rows):
            rows = [dict(r) for r in rows[:_CHORD_ROWS_PER_BANK]]
            while len(rows) < _CHORD_ROWS_PER_BANK:
                rows.append(dict(_OFF))
            return rows
        def _mk_default():
            return _pad(defaults)
        banks, active = [], 0
        try:
            with open(os.path.expanduser(_CHORD_ROWS_FILE)) as f:
                data = json.load(f)
        except Exception:
            data = None
        version = 0
        if (isinstance(data, dict) and int(data.get("v", 0) or 0) >= 2
                and isinstance(data.get("banks"), list)):
            version = int(data.get("v", 0) or 0)
            active = int(data.get("active", 0) or 0)
            for b in data["banks"]:
                if isinstance(b, list) and b and all(isinstance(r, dict) for r in b):
                    banks.append(_pad(b))
        elif isinstance(data, list) and len(data) == 4:
            # pre-banks format: migrate RELATIVE octave -> ABSOLUTE, seat as bank 0
            banks.append(_pad([
                {**dict(r), "octave": int(r.get("octave") or 0) + (3 - i)}
                for i, r in enumerate(data)
            ]))
        # Shipped literal-channel synth rows -> modules, ONCE: a file already at
        # _CHORD_ROWS_VERSION has been migrated, so a synth ch 8 / 7 row in it
        # is the player's choice and stays a channel. _chord_rows_unmarked
        # tells the caller to re-save (which writes the marker).
        self._chord_rows_migrated = 0
        self._chord_rows_unmarked = data is not None and version < _CHORD_ROWS_VERSION
        if self._chord_rows_unmarked:
            self._chord_rows_migrated = _migrate_chord_banks(banks)
        # pad / truncate to exactly _CHORD_N_BANKS
        banks = banks[:_CHORD_N_BANKS]
        while len(banks) < _CHORD_N_BANKS:
            banks.append(_mk_default())
        active = max(0, min(_CHORD_N_BANKS - 1, active))
        return banks, active

    def _save_chord_rows(self):
        # Atomic write (torn reads are possible -- orc1.py and orc1_web.py both
        # read fire state files from other processes). Persists ALL banks + the
        # active index, so every bank is always saved and reloaded on boot.
        try:
            _atomic_write_json(
                os.path.expanduser(_CHORD_ROWS_FILE),
                {"v": _CHORD_ROWS_VERSION, "active": self._chord_bank,
                 "banks": self._chord_banks})
        except Exception as e:
            print(f"  ! _save_chord_rows: {e}")

    def _chord_cfg(self, prow):
        """Row-config for PHYSICAL Fire row `prow` (0=top .. 3=bottom), seen
        THROUGH the viewport into the tall row stack. Physical row 0 shows
        logical row `_chord_row_top`; scrolling the window (SELECT-turn) moves
        which four of the bank's rows are live. An out-of-range window position
        yields a silent 'off' row, so nothing sounds off the end of the stack."""
        idx = self._chord_row_top + prow
        if 0 <= idx < len(self._chord_rows):
            return self._chord_rows[idx]
        return {"type": "off", "port": None, "channel": None, "octave": 0}

    def _row_pitch(self, row, col):
        """Absolute MIDI pitch for a keyboard pad in the chord view.

        Per-row octave is ABSOLUTE -- it fully sets the row's octave, with no
        hidden (3-row)*12 auto-stack. perform_base_note is the ONE global
        transpose (driven by PAT up/down). Both the render (pad tinting) and the
        play path call this (via the viewport-aware _chord_cfg), so the
        highlighted root column can never disagree with the note that sounds.
        """
        oct_off = int(self._chord_cfg(row).get("octave") or 0)
        return max(0, min(127, self.perform_base_note + (col - 4) + oct_off * 12))

    def _perform_key(self, row, col, padnote, vel):
        """Route a chord-view keyboard pad press/release based on the row config."""
        rcfg    = self._chord_cfg(row)
        rtype   = rcfg.get("type", "chord")
        pitch   = self._row_pitch(row, col)

        if rtype == "chord":
            if vel:
                self._pad_held[padnote] = pitch
                try:
                    self.eng.handle_note_input(pitch, vel)
                except Exception as e:
                    print(f"  ! Fire chord kbd: {e}")
            # note-off for chord rows is handled in _pad_release via _pad_held

        elif rtype == "direct":
            port_key = rcfg.get("port") or "pisound"
            # A synth row may NAME a module: resolved live; unbound = silent.
            ch       = self._resolve_ch(rcfg.get("channel") or 1)
            if not ch:
                return
            if vel:
                self._dir_held[padnote] = (port_key, ch, pitch)
                try:
                    self.eng.note_on_out(pitch, vel, port_key, ch)
                except Exception as e:
                    print(f"  ! Fire direct kbd: {e}")
            else:
                # vel==0 path: note-off from _note_off -> _pad_release,
                # but _perform_key is only called on note-on so this branch
                # is informational only; real note-off in _pad_release.
                pass

        # rtype == "off": silent

    def _render_poly(self):
        """Chromatic keyboard in 4ths layout, routes to active voice (chip_voice).
        Rows are a perfect 4th (7 semitones) apart; held pads glow teal.

        With SCALE SNAP on, in-scale pads are lit brighter than out-of-scale
        ones so the scale shape is visible -- every pad still sounds (the
        out-of-scale ones get pulled to their nearest neighbour), the shading
        just shows you where the true notes are."""
        held_pitches = {
            v[0] if isinstance(v, tuple) else v
            for v in self._poly_held.values()
        }
        pcs = self._scale_pcs() if self.eng.state.get("scale_snap") else None
        # In FILTER mode an out-of-scale pad does not sound at all, where in the
        # snapping modes it sounds pitch-corrected. Those are very different
        # things and used to render identically -- so a filtered pad looked
        # playable and did nothing, which reads as broken hardware. Mark the
        # dead ones red instead of merely dim.
        _filtered = self._scale_mode_now() == "filter"
        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                stride = 5 if self._fourths_mode else N_COLS
                note = self.poly_base_note + (N_ROWS - 1 - row) * stride + col
                if note in held_pitches:
                    cells.append((pad_index(row, col), 0x00, 0x44, 0x44))
                elif pcs is not None and (note % 12) in pcs:
                    cells.append((pad_index(row, col), 2, 8, 8))
                elif pcs is not None and _filtered:
                    cells.append((pad_index(row, col), 0x0E, 0x00, 0x00))
                else:
                    cells.append((pad_index(row, col), 1, 2, 2))
        self._send_pads(cells)

    def _poly_note(self, n, vel, on):
        if not mido or not (0 <= n <= 127):
            return
        ch = self._ch_poly
        if ch is None:          # poly unbound on /matrix: silent
            return
        ch0 = ch - 1
        try:
            if on:
                self.eng.send_synth_midi(mido.Message(
                    "note_on", channel=ch0, note=int(n), velocity=int(vel)))
            else:
                self.eng.send_synth_midi(mido.Message(
                    "note_off", channel=ch0, note=int(n), velocity=0))
        except Exception:
            pass

    def _render_chip(self):
        """CHIP: chromatic keyboard for NES voices.

        Row colours:
          active voice row  -- medium brightness in that voice's colour
          P2 row when coupled to P1 -- amber glow so you can see both are linked
          held pads         -- bright green regardless of row
          other rows        -- dim

        Voice -> row: row 0=Pulse1  row 1=Pulse2  row 2=Triangle
                      row 3=Noise / Game Boy wave (MUTE4 toggles the two)
        """
        # Wave shares MUTE4 / row 3 with noise -- give it row 3 but its own
        # colour (below) so you can see at a glance which of the two is live.
        _voice_row  = {'pulse1': 0, 'pulse2': 1, 'triangle': 2,
                       'noise': 3, 'wave': 3}
        active_row  = _voice_row.get(self.chip_voice, 0)

        # Coupling mode: Fire's own local mirror is the source of truth here
        # (no live `chip` object reachable across the orc1.py/orc1_synth.py
        # process boundary -- see the coupling-mode bugfix writeup).
        couple_mode = self._chip_couple_mode
        coupled     = couple_mode != 'none'

        _row_dim    = [(1, 8, 1), (1, 4, 8), (8, 4, 1), (8, 1, 1)]
        _row_sel    = [(2, 30, 2), (2, 15, 28), (28, 15, 2), (28, 2, 2)]
        _row_couple = (20, 12, 0)   # amber -- P2 linked to P1
        _wave_sel   = (10, 30, 6)   # GB green -- distinguishes wave from noise
        # Colour of the highlighted active-voice row (wave gets its own).
        sel_color   = _wave_sel if self.chip_voice == 'wave' else _row_sel[active_row]

        held_pitches = {
            v[0] if isinstance(v, tuple) else v
            for v in self._chip_held.values()
        }

        # With SCALE SNAP on, knock out-of-scale pads down relative to their own
        # row colour rather than recolouring them -- that way the scale shape
        # becomes visible without losing the active-voice/coupling row coding.
        pcs = self._scale_pcs() if self.eng.state.get("scale_snap") else None

        cells = []
        for row in range(N_ROWS):
            for col in range(N_COLS):
                stride = 5 if self._fourths_mode else N_COLS
                note = self.chip_base_note + (N_ROWS - 1 - row) * stride + col
                pidx = pad_index(row, col)
                if note in held_pitches:
                    cells.append((pidx, 0x00, 0x50, 0x10))
                    continue
                if row == active_row:
                    rgb = sel_color
                elif row == 1 and coupled and active_row == 0:
                    # P2 row glows amber when coupled to P1
                    rgb = _row_couple
                else:
                    rgb = _row_dim[row]
                if pcs is not None and (note % 12) not in pcs:
                    # FILTER silences these rather than correcting them, so they
                    # get their own colour instead of just a dimmer version of
                    # the row -- otherwise a dead pad looks merely quiet.
                    rgb = ((0x0E, 0x00, 0x00) if self._scale_mode_now() == "filter"
                           else tuple(max(0, c // 4) for c in rgb))
                cells.append((pidx, *rgb))
        self._send_pads(cells)

    def _default_volume_faders(self):
        """Seed the fader bank as a MASTER / POLY / CHIP / DRUM volume mixer.

        This is the 'easy-to-reach volume' surface: four horizontal faders that
        send the CH_CTRL volume CCs straight to the internal synth bus (port
        'synth'), where the synth turns them into engine.master / poly.master /
        chip.master / drum_master. The CC numbers and boot positions come from
        orc1_config.SYNTH_VOL so the Fire, the synth, and the web LEVELS sliders
        all agree. Boot values match the engine's own startup gains, so a fader
        must actually be moved before anything changes (no jump at boot). These
        are ordinary fader assignments -- reassign or clear them like any other.
        """
        v  = getattr(self.cfg, "SYNTH_VOL", {}) or {}
        ch = int(v.get("CH", 13))
        def f(cc, val, label):
            return {"type": "cc", "number": int(cc), "value": int(val),
                    "channel": ch, "port": "synth", "label": label}
        return {
            0: f(v.get("MASTER_CC",  7), v.get("MASTER_BOOT",  76), "MASTER"),
            1: f(v.get("POLY_CC",    8), v.get("POLY_BOOT",    64), "POLY"),
            2: f(v.get("CHIP_CC",    9), v.get("CHIP_BOOT",    44), "CHIP"),
            3: f(v.get("DRUM_CC",   10), v.get("DRUM_BOOT",    64), "DRUM"),
        }

    # Where the synth persists the four bus levels (P11). Read-only here — the
    # synth owns this file; the Fire only mirrors it so its faders don't lie.
    # Raw string, expanded at the point of use. Resolved here it would be
    # baked in at import time -- before any test can redirect HOME -- which is
    # the same trap as _CHORD_ROWS_FILE and _SKIPBACK_TRACKS_FILE. Class-body
    # assignments are just as import-time as module-level ones.
    _BUS_LEVELS_FILE = "~/.orc1_bus_levels.json"

    def _bus_level_ccs(self):
        """{(channel, cc): bus_key} for every CC that moves a synth bus level.

        Two families: the CH_CTRL master/poly/chip/drum faders, and CC 13 on the
        poly/chip voice channels (what 'synvol'/'chipvol' send) — both end up on
        the same four engine attributes, so both must be treated as bus levels.
        """
        v  = getattr(self.cfg, "SYNTH_VOL", {}) or {}
        ch = int(v.get("CH", 13))
        out = {
            (ch, int(v.get("MASTER_CC", 7))):  "master",
            (ch, int(v.get("POLY_CC",   8))):  "poly",
            (ch, int(v.get("CHIP_CC",   9))):  "chip",
            (ch, int(v.get("DRUM_CC",  10))):  "drum",
        }
        s = getattr(self.cfg, "SYNTH", {}) or {}
        out[(int(s.get("CH_POLY", 8)), 13)] = "poly"
        out[(int(s.get("CH_CHIP", 9)), 13)] = "chip"
        return out

    def _saved_bus_levels(self):
        """The synth's persisted bus levels as {bus_key: cc}. Missing/corrupt
        file -> {} and callers fall back to the config BOOT values."""
        try:
            import json as _j
            with open(os.path.expanduser(self._BUS_LEVELS_FILE)) as f:
                data = _j.load(f)
            if not isinstance(data, dict):
                return {}
            out = {}
            for k in ("master", "poly", "chip", "drum"):
                val = data.get(k)
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    continue
                if 0 <= val <= 127:
                    out[k] = int(val)
            return out
        except Exception:
            return {}

    def _sync_volume_fader_values(self):
        """Point any fader assigned to a bus level at the level the synth is
        ACTUALLY at.

        Needed because a saved fader bank (written every 60s, so it exists on
        any rig that has ever run) carries its own `value`, which would keep
        showing an old position — e.g. DRUM stuck at 127 from before the boot
        default was lowered, while the bus itself sits at 64. A fader that
        displays the wrong level is a gain jump waiting for the next touch,
        which is the whole class of problem P11 is about.
        """
        v     = getattr(self.cfg, "SYNTH_VOL", {}) or {}
        boot  = {"master": int(v.get("MASTER_BOOT", 76)),
                 "poly":   int(v.get("POLY_BOOT",   64)),
                 "chip":   int(v.get("CHIP_BOOT",   44)),
                 "drum":   int(v.get("DRUM_BOOT",   64))}
        saved   = self._saved_bus_levels()
        bus_cc  = self._bus_level_ccs()
        for assign in self._mpc_faders.values():
            if not isinstance(assign, dict) or assign.get("type") != "cc":
                continue
            key = bus_cc.get((int(assign.get("channel", 0)),
                              int(assign.get("number", -1))))
            if key is None:
                continue
            assign["value"] = saved.get(key, boot[key])

    def _render_faders(self):
        """Assignable fader bank. 4 faders visible at once (one per row).
        Each row is a horizontal CC fader: tap a column to set value 0-127.
          Unassigned fader  very dim amber  (4, 2, 0)
          Assigned, active  warm bar: lit portion bright, dark portion dim
          Assign target row  white highlight
        Each fader has its own CC, channel, and port (pisound/synth).
        GRID <> banks by 4; SELECT turn fine-scrolls one at a time.
        """
        # Warm colours per row (same palette as before, warm/amber)
        _row_bright = [(0x40, 0x18, 0x00),   # row 0: orange
                       (0x38, 0x28, 0x00),   # row 1: amber
                       (0x28, 0x38, 0x00),   # row 2: yellow-green
                       (0x18, 0x30, 0x10)]   # row 3: muted green
        _row_dim    = [(0x06, 0x03, 0x00),
                       (0x05, 0x04, 0x00),
                       (0x04, 0x06, 0x00),
                       (0x03, 0x05, 0x02)]
        cells = []
        tgt   = self._mpc_assign_target if self._mpc_assign_mode else None
        for r in range(N_ROWS):
            fidx   = self._mpc_view_top + r
            assign = self._mpc_faders.get(fidx)
            bright = _row_bright[r]
            dim    = _row_dim[r]
            for c in range(N_COLS):
                if fidx == tgt:
                    rgb = (0x20, 0x20, 0x20)  # assign target: white
                elif assign is None:
                    rgb = (0x04, 0x02, 0x00)  # unassigned: very dim amber
                else:
                    val = assign.get("value", 64)
                    lit = round(val * N_COLS / 127)
                    rgb = bright if c < lit else dim
                cells.append((pad_index(r, c), *rgb))
        self._send_pads(cells)

    # -- MPC fader bank helpers -------------------------------------------

    def _mpc_fader_send(self, fidx, val):
        """Send MIDI for fader fidx at value val via its assigned port.
        Supports CC (control_change) and Note (note_on/note_off) types.
        'number' key is canonical; 'cc' key accepted for legacy assignments.
        Silently does nothing if the fader is unassigned.
        """
        assign = self._mpc_faders.get(fidx)
        if not assign or not mido:
            return
        send = getattr(self.eng, "send_to", None)
        if not send:
            return
        try:
            port = assign.get("port", "pisound")
            typ  = assign.get("type", "cc")
            ch   = int(assign.get("channel", 1)) - 1
            num  = int(assign.get("number", assign.get("cc", 7)))
            v    = int(max(0, min(127, val)))
            if typ == "note":
                msg = (mido.Message("note_on",  channel=ch, note=num, velocity=v)
                       if v > 0 else
                       mido.Message("note_off", channel=ch, note=num, velocity=0))
            else:
                msg = mido.Message("control_change", channel=ch, control=num, value=v)
            if port == "synth":
                self.eng.send_synth_midi(msg)
            else:
                send(port, msg)
        except Exception as e:
            print(f"  ! mpc_fader_send: {e}")

    def _mpc_enter_assign(self, fidx):
        """Enter assignment edit mode for fader at index fidx.
        Seeds working copy from existing assignment or sane defaults.
        """
        self._mpc_assign_mode   = True
        self._mpc_assign_target = fidx
        existing = self._mpc_faders.get(fidx, {})
        self._mpc_edit = {
            "type":    existing.get("type",    "cc"),
            "number":  int(existing.get("number",  existing.get("cc", 7))),
            "value":   int(existing.get("value",   64)),
            "channel": int(existing.get("channel", 1)),
            "port":    existing.get("port",    "pisound"),
        }
        self._render_faders()
        self._oled_mpc_assign()

    def _mpc_assign_confirm(self):
        """SELECT press in MPC assign mode: commit working edit, exit, save."""
        fidx = self._mpc_assign_target
        entry = dict(self._mpc_edit)
        self._mpc_faders[fidx] = entry
        self._mpc_assign_mode   = False
        self._mpc_assign_target = None
        self._save_fire_state()
        self._render_faders()
        self._oled()

    def _mpc_assign_cancel(self):
        """Exit MPC assign mode without saving -- BROWSER escape hatch."""
        self._mpc_assign_mode   = False
        self._mpc_assign_target = None
        self._render_faders()
        self._oled()

    def _mpc_assign_dial(self, idx, step):
        """Encoder turns in MPC fader assign mode.
          Enc 0  number   CC / Note number   0-127
          Enc 1  value    default send value  0-127
          Enc 2  channel  MIDI channel        1-16
          Enc 3  port     pisound (DIN) / synth
          SELECT turn     toggle CC / Note type
        OLED updates after each turn.
        """
        e = self._mpc_edit
        if idx == 0:
            e["number"]  = max(0,  min(127, e.get("number", e.get("cc", 7)) + step))
        elif idx == 1:
            e["value"]   = max(0,  min(127, e.get("value",  64) + step))
        elif idx == 2:
            e["channel"] = max(1,  min(16,  e["channel"] + step))
        elif idx == 3:
            ports = ["pisound", "synth"]
            cur   = e.get("port", "pisound")
            idx_p = ports.index(cur) if cur in ports else 0
            e["port"] = ports[(idx_p + step) % len(ports)]
        self._oled_mpc_assign()

    def _oled_faders(self):
        """OLED for MPC fader bank in normal play mode.
        Shows info for the last-tapped fader, or a hint if none touched.
        """
        if not fire_oled:
            return
        fidx   = self._mpc_last_fader
        bank   = self._mpc_view_top // 4
        if fidx is None:
            fire_oled.show_lines([
                ("FADERS", 2, 0, 2),
                (f"bank {bank}", 2, 20, 1),
                ("tap a row, then", 2, 34, 1),
                ("SEL to assign", 2, 48, 1)])
            return
        assign = self._mpc_faders.get(fidx)
        if assign is None:
            fire_oled.show_lines([
                (f"FADER #{fidx}", 2, 0, 2),
                ("unassigned", 2, 20, 1),
                ("SEL to assign", 2, 34, 1)])
        else:
            # Show a friendly label (e.g. MASTER/POLY/CHIP/DRUM) when the fader
            # has one; otherwise fall back to the generic fader title.  The
            # label is user/config data of unbounded length, so it is the one
            # string here that is legitimately truncated rather than written
            # short -- see fire_oled.fit's docstring for that distinction.
            title = fire_oled.fit(
                f"{assign.get('label') or 'FADER'} #{fidx}", 2, 2,
                why="user fader label")
            fire_oled.show_lines([
                (title, 2, 0, 2),
                (_assign_detail_line(assign), 2, 20, 1),
                (_assign_dest_line(assign), 2, 34, 1)])

    def _oled_mpc_assign(self):
        """OLED while in MPC fader assign mode.
        E0=num  E1=val  E2=ch  E3=port  SELturn=type  SELpress=ok  BROW=cancel.
        """
        if not fire_oled:
            return
        e    = self._mpc_edit
        fidx = self._mpc_assign_target
        # Same four-line layout as the universal pad assign screen -- the
        # faders bank is the continuous counterpart of the pad grid, so it
        # gets the same treatment and shares the same line builders.  The old
        # header alone ("ASSIGN fader #12  SEL=type/ok") was 29 characters
        # against a 14-character budget.
        fire_oled.show_lines([
            (f"FADER #{fidx}", 2, 0, 2),
            (_assign_detail_line(e), 2, 20, 1),
            (_assign_dest_line(e, hint=True), 2, 34, 1),
            (_assign_knob_legend(e), 2, 48, 1)])

    def _render_universal(self):
        """Universal Control Surface pad grid.
          Unassigned         dim purple  (2, 1, 2)
          Note pad           dim blue    (0, 0, 20)
          CC pad             dim green   (0, 20, 0)
          Latched ON         bright blue/green
          Physically held    bright green  (standard chip/poly convention)
          Assign target      white         (20, 20, 20)
        """
        cells = []
        tgt = self._universal_assign_target if self._universal_assign_mode else None
        faders = self._universal_faders.get(self._universal_bank, {})
        for r in range(N_ROWS):
            # A fader row (Phase 3) draws as a horizontal level bar and takes the
            # whole row, overriding any per-pad assignments underneath it.
            frow = faders.get(r)
            if frow is not None:
                val = int(frow.get("value", 0))
                lit = round(val * N_COLS / 127)
                for c in range(N_COLS):
                    rgb = (0, 0x30, 0x30) if c < lit else (0, 0x06, 0x06)  # cyan bar
                    cells.append((pad_index(r, c), *rgb))
                continue
            for c in range(N_COLS):
                key    = (r, c)
                assign = self._universal_pads.get(key)
                if key == tgt:
                    rgb = (20, 20, 20)          # target pad: white
                elif key in self._universal_held:
                    rgb = (0, 40, 0)            # held: bright green
                elif assign is None:
                    rgb = (2, 1, 2)             # unassigned: dim purple
                elif assign.get("_latched_on"):
                    rgb = (0, 0, 50) if assign["type"] == "note" else (0, 50, 0)
                else:
                    rgb = (0, 0, 20) if assign["type"] == "note" else (0, 20, 0)
                cells.append((pad_index(r, c), *rgb))
        self._send_pads(cells)

    # -- Universal Control Surface helpers ---------------------------------

    @property
    def _universal_pads(self):
        """The current bank's {(row,col): assignment} dict.

        A property rather than a plain attribute so that every pre-existing
        call site -- including the ones in orc1.py that rebind it wholesale
        (`fire._universal_pads = {}` in UNIVERSAL_CLEAR_PAD) -- addresses the
        bank the player is actually looking at.  Making this a plain dict per
        bank and swapping the reference would have left those cross-module
        rebinds pointing at an orphaned dict: the pads would look cleared and
        come back on the next bank change.
        """
        return self._universal_banks[self._universal_bank]

    @_universal_pads.setter
    def _universal_pads(self, value):
        self._universal_banks[self._universal_bank] = dict(value or {})

    def _universal_set_bank(self, delta):
        """GRID <> in the universal view: page between pad banks.

        Clamps rather than wraps -- wrapping past the last bank into the
        first makes it impossible to tell "I am at the end" from "I looped",
        and these pads send real MIDI to real gear.  Held pads are released
        first: the note-off for a pad you are still holding belongs to the
        bank it was pressed in, and once the bank changes that assignment is
        no longer reachable from _universal_pads.
        """
        new = max(0, min(self._universal_nbanks - 1, self._universal_bank + delta))
        if new == self._universal_bank:
            return
        self._universal_release_held()
        # An assign in progress belongs to the bank it was opened on; leaving
        # it open across a bank change would commit the edit into the wrong
        # bank on the next SELECT press.
        self._universal_assign_mode   = False
        self._universal_assign_target = None
        self._universal_bank = new
        self._last_universal_pad = None
        self._render_universal()
        self._oled()

    def _universal_release_held(self):
        """Send note-offs for every physically-held universal pad.

        Used when the surface is about to stop being able to address them
        (bank change).  Without this a held note on external gear sustains
        forever, because _pad_release looks the pad up in whatever bank is
        current by then.
        """
        for key in list(self._universal_held):
            assign = self._universal_pads.get(key)
            if assign:
                try:
                    self._universal_fire_pad(key, assign, 0, pressed=False)
                except Exception:
                    pass
            self._universal_held.pop(key, None)

    def _universal_knob_action(self, layer, idx):
        """Effective knob action in the universal view (Fire reorg Phase 2).

        The current page/bank's override for (layer, idx) wins if it exists and
        is non-null; otherwise fall back to the baseline _dialmap["universal"]
        (which itself already carries the global knobmap). Pure read-overlay --
        nothing mutates the dialmap, so there is no restore window to leak when
        you page or leave the view.
        """
        ov = self._universal_knobs.get(self._universal_bank, {}).get(layer, {})
        act = ov.get(idx)
        if act:
            return act
        return self._dialmap.get("universal", {}).get(layer, {}).get(idx)

    def _knob_action_accepted(self, act):
        """Shared accept rule for a knob action: a named target (minus the
        CC-OUT menu sentinel) or a well-formed free-form send. Used by BOTH the
        knobmap loader (_load_knobmap) and the per-page universal validator so
        the two can't drift. Applies NO per-view exclusion -- see
        _valid_universal_knob for the universal-only voice_param exclusion."""
        act = tuple(act) if isinstance(act, (list, tuple)) else None
        if not act:
            return False
        valid = {tuple(a) for _, a in self._targets if tuple(a) != _KMAP_CC_ACTION}
        return act in valid or _well_formed_send_action(act)

    def _valid_universal_knob(self, act):
        """True if `act` is a legal per-page knob action for the universal
        surface: accepted by the shared knob-action rule AND allowed on an
        external control surface by _target_allowed_in_view (send actions +
        "none" only -- no internal ORC targets; drive those by sending their CC)."""
        act = tuple(act) if isinstance(act, (list, tuple)) else None
        if not act or not _target_allowed_in_view(act, "universal"):
            return False
        return self._knob_action_accepted(act)

    def set_universal_knob(self, bank, layer, idx, action):
        """Set (or clear) one per-page knob override, then persist.

        action=None clears the override, falling that knob back to the baseline.
        Returns True if applied. Validates like the knobmap loader and rejects
        (returns False) silently otherwise. Called by the web UNIVERSAL_SET_KNOB
        cmd; safe to call at any time. (Fire reorg Phase 2.)
        """
        try:
            bank = int(bank); idx = int(idx)
        except (TypeError, ValueError):
            return False
        if layer not in self._layers or not (0 <= idx <= 3):
            return False
        if bank not in self._universal_banks:
            return False
        if action is None:
            self._universal_knobs.get(bank, {}).get(layer, {}).pop(idx, None)
        else:
            if not self._valid_universal_knob(action):
                return False
            self._universal_knobs.setdefault(bank, {}).setdefault(
                layer, {})[idx] = tuple(action)
        self._save_fire_state()
        return True

    def _universal_fader_send(self, frow, val):
        """Send the CC for a universal fader row (Phase 3). cc/channel/port from
        the row; value 0-127. Mirrors _mpc_fader_send but CC-only."""
        if not mido:
            return
        send = getattr(self.eng, "send_to", None)
        if not send:
            return
        try:
            port = frow.get("port", "pisound")
            ch   = int(frow.get("channel", 1)) - 1
            cc   = int(frow.get("cc", 7))
            v    = max(0, min(127, int(val)))
            msg  = mido.Message("control_change", channel=ch, control=cc, value=v)
            if port == "synth":
                self.eng.send_synth_midi(msg)
            else:
                send(port, msg)
        except Exception as e:
            print(f"  ! universal_fader_send: {e}")

    def _oled_universal_fader(self, row, val):
        """OLED feedback when a universal fader row is moved."""
        if not fire_oled:
            return
        frow = self._universal_faders.get(self._universal_bank, {}).get(row, {})
        try:
            fire_oled.show_lines([
                (f"FADER row {row + 1}", 2, 0, 2),
                (f"CC{int(frow.get('cc', 7))} ch{int(frow.get('channel', 1))}",
                 2, 20, 1),
                (f"{frow.get('port', 'pisound')}", 2, 34, 1),
                (f"val {int(val)}", 2, 48, 1)])
        except Exception:
            pass

    def set_universal_fader(self, bank, row, cc, channel=1, port="pisound"):
        """Make (bank,row) a horizontal CC fader (Phase 3). Returns True if
        applied. Keeps the current value if the row is already a fader; persists."""
        try:
            bank = int(bank); row = int(row)
            cc = int(cc); channel = int(channel)
        except (TypeError, ValueError):
            return False
        if bank not in self._universal_banks or not (0 <= row < N_ROWS):
            return False
        if not (0 <= cc <= 127 and 1 <= channel <= 16) or not port:
            return False
        cur = self._universal_faders.get(bank, {}).get(row, {})
        self._universal_faders.setdefault(bank, {})[row] = {
            "cc": cc, "channel": channel, "port": str(port),
            "value": int(cur.get("value", 0)),
        }
        self._save_fire_state()
        return True

    def clear_universal_fader(self, bank, row):
        """Remove the fader on (bank,row); the row returns to per-pad. Persists."""
        try:
            bank = int(bank); row = int(row)
        except (TypeError, ValueError):
            return False
        self._universal_faders.get(bank, {}).pop(row, None)
        self._save_fire_state()
        return True

    def _universal_fire_pad(self, key, assign, vel, pressed, latch_state=None):
        """Send the MIDI message for a universal pad press or release.
        Respects SHIFT/ALT to select a sub-action layer if one is defined.
        latch_state=True  -> send note_on  (latch flipped ON)
        latch_state=False -> send note_off (latch flipped OFF)
        latch_state=None  -> normal momentary press/release semantics.
        """
        layer = assign
        if self._shift and assign.get("shift_action"):
            layer = assign["shift_action"]
        elif self._alt and assign.get("alt_action"):
            layer = assign["alt_action"]
        port = layer.get("port", "pisound")
        ch   = int(layer.get("channel", 1))
        send = getattr(self.eng, "send_to", None)
        if not mido or not send:
            return
        try:
            if layer["type"] == "note":
                if latch_state is True:
                    msg = mido.Message("note_on", channel=ch - 1,
                                       note=int(layer["number"]),
                                       velocity=int(layer.get("value", 100)))
                elif latch_state is False:
                    msg = mido.Message("note_off", channel=ch - 1,
                                       note=int(layer["number"]), velocity=0)
                elif pressed:
                    msg = mido.Message("note_on", channel=ch - 1,
                                       note=int(layer["number"]),
                                       velocity=max(1, int(vel)))
                else:
                    msg = mido.Message("note_off", channel=ch - 1,
                                       note=int(layer["number"]), velocity=0)
            elif layer["type"] == "pc":
                # Program Change — fires on press only; number = program (0-127)
                if not pressed:
                    return
                msg = mido.Message("program_change", channel=ch - 1,
                                   program=int(layer["number"]) & 0x7F)
            else:  # cc
                if not pressed:
                    return   # CC fires on press only, not release
                msg = mido.Message("control_change", channel=ch - 1,
                                   control=int(layer["number"]),
                                   value=int(layer.get("value", 0)))
            if port == "synth":
                self.eng.send_synth_midi(msg)
            else:
                send(port, msg)
        except Exception as e:
            print(f"  ! universal_fire_pad: {e}")

    def _universal_enter_assign(self, key):
        """Enter assignment edit mode for the pad at key=(row, col).
        Seeds the working copy from the existing assignment or sane defaults.
        """
        self._universal_assign_mode   = True
        self._universal_assign_target = key
        self._universal_assign_layer  = "base"
        existing = self._universal_pads.get(key, {})
        self._universal_edit = {
            "type":    existing.get("type",    "note"),
            "number":  int(existing.get("number",  60)),
            "value":   int(existing.get("value",   100)),
            "channel": int(existing.get("channel", 1)),
            "port":    existing.get("port",    "pisound"),
            "latch":   bool(existing.get("latch",  False)),
        }
        self._render_universal()
        self._oled_universal_assign()

    def _universal_assign_confirm(self):
        """SELECT press in assign mode: commit the working edit into
        _universal_pads, exit assign mode, and auto-save.
        """
        key   = self._universal_assign_target
        layer = self._universal_assign_layer
        if layer == "base":
            # Keep any existing sub-actions and clear runtime latch state.
            cur   = self._universal_pads.get(key, {})
            entry = dict(self._universal_edit)
            entry["shift_action"] = cur.get("shift_action")
            entry["alt_action"]   = cur.get("alt_action")
            entry["_latched_on"]  = False
            self._universal_pads[key] = entry
        elif layer in ("shift", "alt") and key in self._universal_pads:
            # Only write the sub-action; base must already exist.
            sub = {k: self._universal_edit[k]
                   for k in ("type", "number", "value", "channel", "port")}
            self._universal_pads[key][layer + "_action"] = sub
        self._universal_assign_mode   = False
        self._universal_assign_target = None
        self._save_fire_state()
        self._render_universal()
        self._oled()

    def _universal_assign_cancel(self):
        """Exit assign mode without saving -- BROWSER press escape hatch."""
        self._universal_assign_mode   = False
        self._universal_assign_target = None
        self._render_universal()
        self._oled()

    def _universal_assign_dial(self, idx, step):
        """Encoder turns in assign mode.
          Enc 0  number   CC / Note number    0-127
          Enc 1  value    velocity / CC val   0-127
          Enc 2  channel  MIDI channel        1-16
          Enc 3  port     pisound (DIN) / synth
          SELECT turn     toggle CC / Note type
        OLED updates after each turn so the user can see changes live.
        """
        e = self._universal_edit
        if idx == 0:
            e["number"]  = max(0, min(127, e["number"]  + step))
        elif idx == 1:
            e["value"]   = max(0, min(127, e["value"]   + step))
        elif idx == 2:
            e["channel"] = max(1, min(16,  e["channel"] + step))
        elif idx == 3:
            ports = ["pisound", "synth"]
            cur   = e.get("port", "pisound")
            idx_p = ports.index(cur) if cur in ports else 0
            e["port"] = ports[(idx_p + step) % len(ports)]
        self._oled_universal_assign()

    def _oled_universal(self):
        """OLED in universal view -- normal play mode.
        Shows assignment info for the last-touched pad,
        or a hint if no pad has been touched yet.
        """
        if not fire_oled:
            return
        key = self._last_universal_pad
        if key is None:
            fire_oled.show_lines([
                ("UNIVERSAL", 2, 0, 2),
                ("tap a pad, then", 2, 20, 1),
                ("SEL to assign", 2, 34, 1),
                (self._universal_bank_hint(), 2, 48, 1)])
            return
        r, c   = key
        assign = self._universal_pads.get(key)
        head   = f"PAD {self._universal_bank + 1}-{r},{c}"
        if assign is None:
            fire_oled.show_lines([
                (head, 2, 0, 2),
                ("unassigned", 2, 20, 1),
                ("SEL to assign", 2, 34, 1),
                (self._universal_bank_hint(), 2, 48, 1)])
        else:
            fire_oled.show_lines([
                (head, 2, 0, 2),
                (_assign_detail_line(assign), 2, 20, 1),
                (_assign_dest_line(assign), 2, 34, 1),
                (self._universal_bank_hint(), 2, 48, 1)])

    def _universal_bank_hint(self):
        """Bottom-line hint: which bank you are on and how to change it.

        Shown on every universal screen because the bank is otherwise
        invisible -- the pad grid looks identical in all of them, and a pad
        that is assigned in bank 2 but dark in bank 1 reads as a lost
        assignment.  Worst case 'bank 9/9  <>=page' is 16 characters.
        """
        return (f"bank {self._universal_bank + 1}/{self._universal_nbanks}"
                f" <>=page")

    def _oled_universal_assign(self):
        """OLED while in assign mode -- shows the live working edit state.
        E0=num  E1=val  E2=ch  E3=port  SELturn=type  SELpress=ok  BROW=cancel.
        """
        if not fire_oled:
            return
        e     = self._universal_edit
        key   = self._universal_assign_target
        r, c  = key if key else (0, 0)
        layer_tag = {"base": "", "shift": " SH", "alt": " ALT"}.get(
                     self._universal_assign_layer, "")
        # Four short lines instead of three overlong ones.  The old layout
        # crammed type/number/value/channel/port onto ONE 16-character line
        # and spent the last line on a 41-character legend, so in practice
        # the player saw "NOTE 60  val 100" and "E0=num  E1=val  " -- the
        # channel and the output, the two fields that decide where the MIDI
        # actually goes, never rendered at all.
        fire_oled.show_lines([
            (f"PAD {self._universal_bank + 1}-{r},{c}{layer_tag}", 2, 0, 2),
            (_assign_detail_line(e), 2, 20, 1),
            (_assign_dest_line(e, latch=e.get("latch"), hint=True), 2, 34, 1),
            (_assign_knob_legend(e), 2, 48, 1)])

    def _select_chip_voice(self, voice_name):
        """Switch the active NES voice for the chip view.

        The chip view has four pad rows -- one per NES voice.  Press MUTE1-4
        to select which voice the pads play on and which channel the dials
        target.  The selected voice row glows a different colour on the pads.

        CC assignments after switching:
          Pulse 1 / Pulse 2  -> dial 2 = duty cycle (CC 1)
                                 0-31 = 12.5 %   32-63 = 25 %
                                 64-95 = 50 %    96-127 = 75 %
          Triangle           -> dial 2 = CC 1 (NESEngine ignores it for triangle)
          Noise              -> dial 2 = LFSR mode (CC 3)
                                 0-63 = long LFSR (white-ish noise)
                                 64-127 = short LFSR (metallic / tonal buzz)
        All other dials (cutoff, resonance, ADSR, octave) always address the
        selected voice's channel.
        """
        _voice_map = {
            # voice name  ->  (MODULE the knobs address,  timbre CC)
            # The dial map stores the MODULE, not its channel: the channel is
            # resolved at send time from the live /matrix bindings (2026-09-24).
            'pulse1':   ('chip_p1',    1),
            'pulse2':   ('chip_p2',    1),
            'triangle': ('chip_tri',   1),
            'noise':    ('chip_noise', 3),
            # Game Boy wave channel: CC 1 selects waveform preset,
            # CC 7 sets hardware volume (0/25/50/100%)
            'wave':     ('gb_wave',    1),
            # Poly soft synth -- limits-removed NES voice; CC 1 = waveform select
            # (0-31=sine  32-63=saw  64-95=square  96-127=tri)
            'poly':     ('poly',       1),
        }
        if voice_name not in _voice_map:
            return
        self.chip_voice   = voice_name
        ch, timbre_cc     = _voice_map[voice_name]   # ch = the MODULE key

        # Rebuild the chip view's dial map so every knob targets the new channel.
        # (All other views' dial maps are unchanged.)
        # 2026-08-04: attack/decay moved to base 3/4, timbre/octave to shift
        # 3/4 -- mirrors the same swap in _init_dialmaps' static chip defaults
        # (owner's ask). This function runs on every voice switch and at
        # init, so it's the one that actually governs live behavior; keep it
        # in step with the static table above.
        self._dialmap["chip"]["base"] = {
            0: ("cc_out", "synth", ch, 74),        # filter cutoff
            1: ("cc_out", "synth", ch, 71),        # filter resonance
            2: ("cc_out", "synth", ch,  8),        # attack
            3: ("cc_out", "synth", ch,  9),        # decay
        }
        self._dialmap["chip"]["shift"] = {
            0: ("cc_out", "synth", ch, 70),        # filter mode
            1: ("cc_out", "synth", ch, 11),        # release
            2: ("cc_out", "synth", ch, timbre_cc), # duty (pulse) or LFSR (noise)
            3: ("cc_out", "synth", ch, 75),        # octave transpose
        }
        # -- Chip/GB expansion (2026-07-20): ALT layer = GB expression -----
        # Pulse 1 / Pulse 2 / Wave get pitch bend + vibrato on the ALT
        # layer (hold ALT, turn knobs); Pulse 1 also gets the hardware
        # sweep time.  Sweep shift (CC80) and direction (CC81) don't fit
        # in 4 knobs -- assign them via ALT+BROWSER ("swpshft"/"swpdir")
        # or use the web /chip page.  Voices without bend (noise,
        # triangle, poly) keep the stock ALT layer captured below on the
        # first call, so nothing they had before is lost.
        if not hasattr(self, "_chip_alt_stock"):
            self._chip_alt_stock = dict(self._dialmap["chip"].get("alt", {}))
        if voice_name == 'pulse1':
            self._dialmap["chip"]["alt"] = {
                0: ("pitch_out", "synth", ch),       # pitch bend (springy)
                1: ("cc_out",  "synth", ch, 77),     # vibrato depth
                2: ("cc_out",  "synth", ch, 78),     # vibrato rate
                3: ("cc_out",  "synth", ch, 79),     # sweep time (0 = off)
            }
        elif voice_name in ('pulse2', 'wave'):
            self._dialmap["chip"]["alt"] = {
                0: ("pitch_out", "synth", ch),       # pitch bend (springy)
                1: ("cc_out",  "synth", ch, 77),     # vibrato depth
                2: ("cc_out",  "synth", ch, 78),     # vibrato rate
                3: ("none",),                        # spare -- remappable
            }
        else:
            # noise / triangle / poly: no bend per spec -- restore stock.
            self._dialmap["chip"]["alt"] = dict(self._chip_alt_stock)

        # -- Game Boy wave voice: knobs that actually do something ------------
        # GameboyWaveVoice.set_cc responds to CC1 (waveform preset), CC7
        # (4-step hardware volume), and -- as of 2026-08-04 -- CC8/CC9
        # (attack/decay, added to GameboyWaveVoice's envelope specifically so
        # this voice isn't the only chip voice without them). It still
        # ignores filter/octave -- real DMG CH3 has neither. Override so
        # waveform/volume own the front knobs and attack/decay own base 3/4,
        # same layout as pulse1/pulse2/triangle/noise; ALT (bend/vibrato) is
        # already set above.
        if voice_name == 'wave':
            self._dialmap["chip"]["base"] = {
                0: ("cc_out", "synth", ch, 1),   # waveform preset (named on OLED)
                1: ("cc_out", "synth", ch, 7),   # 4-step GB volume
                2: ("cc_out", "synth", ch, 8),   # attack
                3: ("cc_out", "synth", ch, 9),   # decay
            }
            self._dialmap["chip"]["shift"] = {
                0: ("cc_out", "synth", ch, 1),   # waveform (also on SHIFT for reach)
                1: ("cc_out", "synth", ch, 7),   # volume
                2: ("none",),
                3: ("none",),
            }
        self._sync_chip_dials()
        print(f"  [voice] -> {voice_name}  ({ch} = ch {self._chip_ch_now}  timbre CC {timbre_cc})")

    def _select_poly_voice(self, voice_name):
        """Switch the active voice for poly view, independent of chip view.

        ALWAYS routes to CH_POLY for true polyphony -- voice switching
        changes the PolySynth waveform timbre via CC1, not the MIDI channel.
        NES chip channels are hardware-monophonic; chip view keeps authentic
        per-channel NES routing.  Poly view is always polyphonic.
        """
        # CC1 waveform for each timbre (PolySynth: 0=sine 32=saw
        # 64=square 96=triangle).  Poly uses saw; noise/wave use sine.
        _waveform_cc = {
            'poly':     32,   # saw -- natural polyphonic tone
            'pulse1':   64,   # square (pulse approximation)
            'pulse2':   80,   # square (wider duty position)
            'triangle': 100,  # triangle
            'noise':     0,   # sine (no noise in PolySynth)
            'wave':     16,   # sine (GB wave approximation)
        }
        if voice_name not in _waveform_cc:
            return
        self.poly_voice   = voice_name
        cc_val = _waveform_cc[voice_name]
        ch = self._ch_poly
        if mido and ch is not None:     # poly unbound: nothing to tweak
            try:
                self.eng.send_synth_midi(mido.Message(
                    'control_change', channel=ch - 1,
                    control=1, value=cc_val))
            except Exception as e:
                # The line below unconditionally reports the voice change as
                # having happened -- log a failure here so a silent send
                # miss isn't indistinguishable from success in the journal.
                print(f"  ! Fire poly voice CC1: {e}", flush=True)
        print(f"  [poly] voice -> {voice_name}  CC1={cc_val}  "
              f"(ch {ch if ch is not None else 'UNBOUND'})")

    def _sync_chip_dials(self):
        """Override chip dial-map SHIFT entry 2 based on coupling mode.

        When coupling is 'unison' or 'detune', encoder 2 sweeps detune
        (CC73) for live feel -- far more useful than duty/LFSR in those modes.
        Any other coupling mode restores the voice-appropriate timbre CC.
        Called by _select_chip_voice and after every coupling-mode change.

        2026-08-04: moved from base[2] to shift[2] -- base knobs 3/4 (index
        2/3) now hold attack/decay (owner's ask), with timbre/detune pushed
        down to shift 3/4 to make room. This function used to write base[2]
        unconditionally on every voice switch and coupling change, which would
        have clobbered attack right back after every _select_chip_voice call
        if left pointed at base.
        """
        if not hasattr(self, '_dialmap') or "chip" not in self._dialmap:
            return
        # Coupling is a Pulse-1/Pulse-2 concept only. For the wave/noise/
        # triangle voices, leave their own shift[2] alone (the wave voice in
        # particular sets shift[2] = none deliberately -- writing the detune CC
        # there would just target a CC ch5 ignores).
        if self.chip_voice not in ('pulse1', 'pulse2'):
            return
        ch = self._VOICE_MODULE[self.chip_voice]   # the MODULE, resolved at send time
        if self._chip_couple_mode in ('unison', 'detune'):
            # CC73: detune cents, encoded as round(cents/50*64)+64
            self._dialmap["chip"]["shift"][2] = ("cc_out", "synth", ch, 73)
        else:
            timbre_cc = 3 if self.chip_voice == 'noise' else 1
            self._dialmap["chip"]["shift"][2] = ("cc_out", "synth", ch, timbre_cc)

    def _chip_note_on(self, pitch_note, vel, ch):
        """Send a note-on to a specific NES voice channel (None = the voice's
        module is unbound: silent)."""
        if not mido or ch is None or not (0 <= pitch_note <= 127):
            return
        try:
            self.eng.send_synth_midi(mido.Message(
                "note_on", channel=int(ch) - 1,
                note=int(pitch_note), velocity=int(vel)))
        except Exception:
            pass

    def _chip_note_off(self, pitch_note, ch):
        """Send a note-off to a specific NES voice channel (None = unbound:
        nothing was sent on, so nothing to release)."""
        if not mido or ch is None or not (0 <= pitch_note <= 127):
            return
        try:
            self.eng.send_synth_midi(mido.Message(
                "note_off", channel=int(ch) - 1,
                note=int(pitch_note), velocity=0))
        except Exception as e:
            # A dropped note-off here strands the chip voice sounding
            # forever with no other release path -- log it so a stuck
            # voice has a journal line to explain it.
            print(f"  ! Fire _chip_note_off: {e}", flush=True)

    def _chip_note(self, n, vel, on):
        """Legacy wrapper -- uses the currently selected voice channel."""
        if on:
            self._chip_note_on(n, vel, self._chip_ch_now)
        else:
            self._chip_note_off(n, self._chip_ch_now)

    def _oled(self):
        if not fire_oled:
            return
        # Session E (2026-06-30): new menu/subscreen states take priority over
        # everything else except kmap, which keeps its own OLED even while
        # latched inside a subscreen (see _menuact_remap_encoders).
        if self._menu_open:
            return self._oled_menu()
        if ((self._subscreen or self._subscreen_latched)
                and self._overlay not in ("kmap", "kmap_cc")):
            return self._oled_subscreen()
        if self._key_overlay_active():
            return self._oled_key_overlay()
        ov = self._overlay
        if ov == "chord_rows":
            return self._oled_chord_rows()
        if ov == "browse" and self.view in ("seq", "mixer"):
            return self._oled_browse()
        if ov == "kmap":
            return self._oled_kmap()
        if ov == "kmap_cc":
            return self._oled_kmap_cc()
        if ov == "track_edit" and self.view in ("seq", "mixer"):
            return self._oled_track_edit()
        if ov == "fx_browse":
            return self._fx_browse_oled()
        if ov == "preset_picker":
            return self._oled_preset_picker()
        if ov == "chord_params":
            # 2026-08-04 adversarial review: this overlay was missing from
            # this dispatch table entirely (only set by _oled_chord_param_menu
            # itself, on entry). Any later repaint -- _render() elsewhere, a
            # dial turn -- fell through to the normal chord-view render below,
            # so the screen quietly went back to looking ordinary while pads
            # stayed blocked by the _pad_press overlay guard (dead grid, no
            # on-screen indication why).
            return self._chord_params_oled()
        try:
            bpm = self._bpm()
            bpm_str = f"{bpm:.1f}"
            if self.view == "seq":
                t = (self.tracks[self._sel_track] if self.n_tracks
                     else {"name": "-", "note": 0, "ch": 0})
                voice = (self._edit_voices[self._sel_voice]
                         if self._edit_voices else "")
                ch_drum = int(self.cfg.SYNTH.get("CH_DRUM", 10))
                trk_ch  = int(t.get("ch", 6))
                if trk_ch == ch_drum:
                    line3 = f"k:{voice[:12]}"
                else:
                    line3 = f"{_note_name(t.get('note', 60))} ch{trk_ch}"
                ctx = self._track_ctx_label(t)
                x2 = 42 if self._sel_track >= 9 else 28
                fire_oled.show_lines([
                    (f"{self._sel_track+1}", 2, 2, 4),
                    (fire_oled.fit(f"{ctx} ch{t.get('ch',0)}", x2, 1, why="track context label"),
                     x2, 6, 1),
                    (fire_oled.fit(bpm_str, x2, 1, why="bpm"), x2, 23, 1)])
            elif self.view == "mixer":
                fire_oled.show_lines([("MIXER", 2, 0, 2),
                                      (bpm_str, 84, 3, 1),
                                      (f"rows {self.view_top+1}-{self.view_top+4}", 2, 22, 1),
                                      (f"{self.n_tracks} tracks", 2, 40, 1)])
            elif self.view == "chord":
                st    = self.eng.state
                arp   = getattr(self.eng, "arp", None)
                rn    = getattr(self.eng, "ROOT_NAMES",
                                ["C","C#","D","Eb","E","F","F#","G","Ab","A","Bb","B"])
                root  = rn[st.get("root", 0) % 12]
                base  = self._effective_base()   # what will sound, not what's stored
                scale = st.get("scale", "?")
                exts  = "+".join(sorted(st.get("chord_exts", set()))) or "--"
                hold  = "HLD" if st.get("hold") else "   "
                klk   = "KEY" if st.get("key_lock") else "   "
                # O3: name the chord by its OWN root and show the degree it is in
                # the key, plus where the quality came from.  Without this the
                # readout could not tell you why a chord was the quality it was
                # -- which is exactly what made the base pads feel dead.
                cr    = self._chord_root_name(rn)
                # O4: three distinguishable sources, not two. "FIX" is the
                # LATCHED quality mode (the pads are a persistent setting);
                # "FIX*" is a momentary base-pad override inside DIAT, which the
                # next key press will clear; otherwise the scale degree.
                if st.get("quality_mode") == "FIXED":
                    src = "FIX"
                elif st.get("base_override"):
                    src = "FIX*"
                else:
                    src = self._degree_str()
                # Show arp pattern name (truncated) when arp is on
                if arp and arp.enabled:
                    pat_name = (getattr(arp, "pattern_name", None) or "?")[:8]
                    arp_s = f"A:{pat_name}"
                else:
                    arp_s = "      "
                # Show which octave the bottom keyboard row (R3) starts on.
                # Per-row octave is absolute now, so include R3's offset.
                _r3_oct = int(self._chord_cfg(3).get("octave") or 0)
                bot_oct = ((self.perform_base_note + _r3_oct * 12) // 12) - 1  # MIDI oct
                fire_oled.show_lines([
                    # Chord on its own root; the key is on the line below.
                    (f"{cr} {base}  {exts}", 2, 0, 2),
                    (f"{root}:{scale[:5]} {src}  {hold}{klk}", 2, 28, 1),
                    (f"{bpm:.0f}bpm  {arp_s}", 2, 44, 1),
                ])
            elif self.view == "poly":
                oct = self.poly_base_note // 12
                fire_oled.show_lines([("POLY SYNTH", 2, 0, 2),
                                      (f"oct {oct}  base {self.poly_base_note}", 2, 30, 1)])
            elif self.view == "chip":
                # No live `chip` object reachable across the process boundary
                # (orc1_synth.py owns it) -- Fire's own local mirror is the
                # source of truth, same as _render_chip.
                oct_base    = self.chip_base_note // 12 - 1
                if self.chip_voice == 'wave':
                    # GB wave voice: show the two controls that matter (the
                    # named waveform + the 4-step volume) instead of the P1+P2
                    # coupling readout, which doesn't apply to this channel.
                    # Values come from the CCs the dials sent (defaults match
                    # GameboyWaveVoice's boot state: preset 0 / 100%).
                    ch      = "gb_wave"   # dial values are keyed by MODULE (2026-09-24)
                    # Decode the values the synth actually receives: _dial_cc_out
                    # runs each CC through _scale_cc on send (CC7 volume has a
                    # log curve), so the raw stored value would mis-name the
                    # band. Scale here too so the OLED matches the audio.
                    cc1     = self._scale_cc(1, self._cc_vals.get(("synth", ch, 1), 0))
                    cc7     = self._scale_cc(7, self._cc_vals.get(("synth", ch, 7), 96))
                    wf_name = GB_WAVE_NAMES[min(7, cc1 // 16)]
                    vol_lbl = GB_VOL_LABELS[min(3, cc7 * 4 // 128)]
                    fire_oled.show_lines([
                        (f"GB WAVE  {wf_name}", 2, 0, 2),
                        (f"vol {vol_lbl}  oct{oct_base}", 2, 28, 1),
                        ("K1=wave K2=vol M4=nse", 2, 44, 1)])
                else:
                    couple_mode = self._chip_couple_mode
                    detune_c    = self._chip_detune_cents
                    _couple_labels = {
                        'none':      'P1+P2 free',
                        'unison':    'UNISON',
                        'detune':    f'DETUNE {detune_c:+d}ct',
                        'fifth':     'P2 +5th',
                        'third':     'P2 +3rd',
                        'octave':    'P2 +OCT',
                        'octave_dn': 'P2 -OCT',
                    }
                    couple_str = _couple_labels.get(couple_mode, couple_mode)
                    fire_oled.show_lines([
                        (f"CHIP  {self.chip_voice}", 2, 0, 2),
                        (couple_str, 2, 28, 1),
                        (f"oct{oct_base}  SH+M1=mode", 2, 44, 1)])
            elif self.view == "faders":
                if self._mpc_assign_mode:
                    self._oled_mpc_assign()
                else:
                    self._oled_faders()
            elif self.view == "universal":
                if self._universal_assign_mode:
                    self._oled_universal_assign()
                else:
                    self._oled_universal()
            elif self.view == "fx":
                # FX RACK: show the selected slot.  When a knob was just turned,
                # show the parameter label + value bar instead.
                rack  = self._fx_rack()
                slots = rack.status().get("slots", []) if rack else []
                sel   = slots[self._fx_sel_slot] if self._fx_sel_slot < len(slots) else None
                plugin = sel.get("plugin") if sel else None
                if plugin:
                    name    = (rack.label_for(plugin) or plugin) if rack else plugin
                    running = bool(sel.get("running"))
                    status  = "RUNNING" if running else "stopped"
                    if self._fx_last_param is not None:
                        lbl, val, p_min, p_max = self._fx_last_param
                        frac   = (val - p_min) / (p_max - p_min) if p_max > p_min else 0.5
                        frac   = max(0.0, min(1.0, frac))
                        filled = round(frac * 14)
                        bar    = "[" + "#" * filled + "-" * (14 - filled) + "]"
                        fire_oled.show_lines([
                            ("FX " + str(self._fx_sel_slot + 1) + ": " + name, 2, 0, 2),
                            (lbl + "  " + format(val, ".3f"), 2, 28, 1),
                            (bar, 2, 44, 1)])
                    else:
                        fav = " *" if (rack and rack.is_favorite(plugin)) else ""
                        fire_oled.show_lines([
                            ("FX " + str(self._fx_sel_slot + 1) + ": " + name + fav, 2, 0, 2),
                            (status + " (series)", 2, 28, 1),
                            ("SH+pad=on/off", 2, 44, 1)])
                else:
                    fire_oled.show_lines([
                        ("FX " + str(self._fx_sel_slot + 1) + ": (empty)", 2, 0, 2),
                        ("press pad=browse", 2, 28, 1),
                        ("turn=plug SH=bk", 2, 44, 1)])
        except Exception:
            pass

    def _browse_params_for(self, t):
        """Return the appropriate browse param list for this track.
        Drum tracks get voice-name scrolling; all others get MIDI note editing."""
        ch_drum = int(self.cfg.SYNTH.get("CH_DRUM", 10))
        if int(t.get("ch", 6)) == ch_drum:
            return _DRUM_BROWSE_PARAMS
        return _NOTE_BROWSE_PARAMS

    def _oled_browse(self):
        """BROWSER parameter scroll overlay.
        Shows two lines of the param list; selected item prefixed with >.
        SELECT encoder adjusts value; PATTERN UP/DOWN scrolls."""
        if not fire_oled or not self.n_tracks:
            return
        try:
            t      = self.tracks[self._sel_track]
            params = self._browse_params_for(t)
            n      = len(params)
            idx    = self._browse_idx % n

            def _val(key):
                if key == "voice":
                    return self._voice_display.get(int(t.get("note", 36)),
                                                   _note_name(t.get("note", 36)))
                if key == "note":
                    note = int(t.get("note", 60))
                    if t.get("mode") == "chord":
                        return _CHORD_TONES.get(note, f"T{note}")
                    return f"{_note_name(note)} ({note})"
                if key == "mode": return t.get("mode", "note")
                if key == "ch":   return str(t.get("ch", 1))
                if key == "vel":  return str(t.get("vel", 100))
                if key == "len":  return str(t.get("length", 16))
                return "?"

            header = f"PARAM  t{self._sel_track+1}"
            above  = ""
            if idx > 0:
                k, lbl = params[idx - 1]
                above  = f"  {lbl}: {_val(k)}"
            k, lbl = params[idx]
            cur    = f"> {lbl}: {_val(k)}"
            fire_oled.show_lines([
                (header, 2, 0, 1),
                (fire_oled.fit(above, 2, 1, why="browse param label"), 2, 16, 1),
                (fire_oled.fit(cur,   2, 1, why="browse param label"), 2, 36, 1)])
        except Exception:
            pass

    def _browse_adjust(self, step):
        """Adjust the currently highlighted browse parameter.
        2026-07-01: value resolution stays Fire-side UI logic; the actual
        writes go through seq.set_track_prop (lock-guarded -- Bug B). Mode
        + its conditional note-reset are two separate locked calls rather
        than one atomic one -- acceptable here since this is an encoder-turn
        UI path, not the clock hot path."""
        if not self.n_tracks:
            return
        tidx = self._sel_track
        t      = self.tracks[tidx]
        params = self._browse_params_for(t)
        prop   = params[self._browse_idx % len(params)][0]
        if prop == "voice":
            vno   = list(self._voice_note_order)
            notes = [n for n, _ in vno]
            try:    cur = notes.index(int(t.get("note", 36)))
            except ValueError: cur = 0
            self.seq.set_track_prop(tidx, "note", vno[(cur + step) % len(vno)][0])
            self._sync_voice_to_track()
        elif prop == "note":
            if t.get("mode") == "chord":
                tones = _CHORD_TONE_ORDER
                cur   = tones.index(int(t.get("note", 0))) if int(t.get("note", 0)) in tones else 1
                self.seq.set_track_prop(tidx, "note", tones[(cur + step) % len(tones)])
            else:
                self.seq.set_track_prop(tidx, "note", int(t.get("note", 60)) + step)
        elif prop == "mode":
            modes = ["note", "chord"]
            cur      = modes.index(t.get("mode", "note")) if t.get("mode", "note") in modes else 0
            new_mode = modes[(cur + step) % len(modes)]
            self.seq.set_track_prop(tidx, "mode", new_mode)
            # Reset note to a sensible default for the new mode
            if new_mode == "chord" and int(t.get("note", 60)) > 10:
                self.seq.set_track_prop(tidx, "note", 0)    # default to root tone
            elif new_mode == "note" and int(t.get("note", 0)) <= 4:
                self.seq.set_track_prop(tidx, "note", 48)   # default to C3
        elif prop == "ch":
            self.seq.set_track_prop(tidx, "ch", int(t.get("ch", 1)) + step)
        elif prop == "vel":
            self.seq.set_track_prop(tidx, "vel", int(t.get("vel", 100)) + step * 2)
        # "len" removed Session E (2026-06-30) -- no longer in _DRUM_BROWSE_PARAMS /
        # _NOTE_BROWSE_PARAMS, so this branch is unreachable. Track length is set
        # ONLY by ALT+step pad now.
        self._oled_browse()

    def _oled_kmap(self, confirm=False):
        """Knob-map screen: which knob/layer/view, what it maps to now, and the
        menu target under the cursor."""
        if not fire_oled:
            return
        try:
            cur   = self._kmap_current_action()
            entry = self._kmap_sel_target()
            now   = self._target_label(cur)
            sel   = entry[0] if entry else "-"
            head  = "KNOB MAP OK" if confirm else "KNOB MAP"
            # Four lines rather than three cramped ones: the old layout put
            # "is > will be" on one line clipped to 6 characters a side, so
            # "chipflt" and "chipoct" rendered identically. Labels are config
            # data of unbounded length, so these two are legitimately fit().
            fire_oled.show_lines([
                (head, 2, 0, 2),
                (f"{self._kmap_view_label()} K{self._kmap_knob+1} {self._kmap_layer}",
                 2, 20, 1),
                (fire_oled.fit(f"is:  {now}", 2, 1, why="config target label"), 2, 34, 1),
                (fire_oled.fit(f"set: {sel}", 2, 1, why="config target label"), 2, 48, 1)])
        except Exception:
            pass

    # -- Tempo -----------------------------------------------------------------

    def _bpm(self):
        arp = self.eng.arp
        if not arp:
            return 120.0
        from orc1_arp import CLOCK_EXTERNAL, CLOCK_DRIFT
        if arp.clock_mode == CLOCK_DRIFT:
            return arp.drift_bpm
        if arp.clock_mode == CLOCK_EXTERNAL:
            try:
                return arp._estimated_bpm() or arp.internal_bpm
            except Exception:
                return arp.internal_bpm
        return arp.internal_bpm

    # -- Config resolution -----------------------------------------------------

    def _resolve_buttons(self, name_map):
        out = {}
        for name, action in name_map.items():
            if not action:
                continue
            num = BUTTON_NOTE.get(name)
            if num is None:
                print(f"  ! Fire config: unknown button '{name}'")
                continue
            out[num] = action
        return out

    # -- Per-view knob mapping --------------------------------------------------

    def _init_dialmaps(self):
        """Build self._dialmap[view][layer][knob_idx] = action_tuple.

        Each view gets its own sensible defaults so knobs match the context:
        - channel: drum sculpting (from DIALS/DIALS_SHIFT/DIALS_ALT config)
        - mixer:   track vel/len per visible row + BPM/spread on ALT
        - perform: chord-engine params (spread, oct_offset) + BPM on ALT
        - user2:   poly synth params (filter, res, waveform, octave)

        Per-view overrides from config (DIALS_<VIEW>) take priority over
        these built-in defaults. The user's saved knobmap.json (loaded later
        by _load_knobmap) overrides everything.
        """
        # Shared ALT layer from config (always available as a base)
        cfg_src = {"base": self._dials, "shift": self._dials_shift,
                   "alt":  self._dials_alt}

        # Per-view built-in defaults (only views that differ from the config).
        # channel uses the config values directly (drum sculpting).
        _view_defaults = {
            "mixer": {
                "base":  {0: ("track_vel",), 1: ("track_vel",),
                          2: ("track_vel",), 3: ("track_vel",)},
                # Session E (2026-06-30): track_length removed from every knob
                # layer per the plan's rule ("length set ONLY by ALT+step pad").
                # These knobs are intentionally unassigned now -- remap them via
                # the encoder-remap flow (BROWSER menu) if you want them to do
                # something else.
                "shift": {0: ("none",), 1: ("none",),
                          2: ("none",), 3: ("none",)},
                "alt":   cfg_src["alt"],  # BPM / vel / len / spread
            },
            "chord": {
                "base":  {0: ("spread",), 1: ("oct_offset",),
                          2: ("bpm",), 3: ("none",)},
                "shift": {0: ("spread",), 1: ("oct_offset",),
                          2: ("none",), 3: ("none",)},
                "alt":   cfg_src["alt"],
            },
            "poly": {
                "base":  {0: ("cc_out", "synth", "poly", 74),    # poly cutoff
                          1: ("cc_out", "synth", "poly", 71),    # poly resonance
                          2: ("cc_out", "synth", "poly",  1),    # poly waveform
                          3: ("cc_out", "synth", "poly", 75)},   # poly octave
                "shift": {0: ("cc_out", "synth", "poly", 70),    # poly filter mode
                          1: ("cc_out", "synth", "poly", 11),    # poly release
                          2: ("cc_out", "synth", "poly",  8),    # poly attack
                          3: ("cc_out", "synth", "poly",  9)},   # poly decay
                "alt":   cfg_src["alt"],
            },
            # 2026-08-04: attack/decay moved from shift knobs 3/4 to base
            # knobs 3/4 (owner's ask, after the voice_param menu bug above
            # made "attack" land on the drum kick instead) -- they're the ADSR
            # params he reaches for most, so they don't belong behind SHIFT.
            # Timbre/octave swapped down onto shift to make room.
            "chip": {
                "base":  {0: ("cc_out", "synth", "chip_p1", 74),    # chip cutoff
                          1: ("cc_out", "synth", "chip_p1", 71),    # chip resonance
                          2: ("cc_out", "synth", "chip_p1",  8),    # chip attack
                          3: ("cc_out", "synth", "chip_p1",  9)},   # chip decay
                "shift": {0: ("cc_out", "synth", "chip_p1", 70),    # chip filter mode
                          1: ("cc_out", "synth", "chip_p1", 11),    # chip release
                          2: ("cc_out", "synth", "chip_p1",  1),    # chip timbre (duty/tri/noise)
                          3: ("cc_out", "synth", "chip_p1", 75)},   # chip octave
                "alt":   cfg_src["alt"],
            },
            # Empty base/shift layers stop fallthrough to global drum-voice CCs.
            # _dispatch_dial intercepts assign-mode before the table anyway.
            "faders": {
                "base":  {},
                "shift": {0: ("bpm",)},
                "alt":   cfg_src["alt"],
            },
            # External CONTROL surface: send-only, so it carries NO internal
            # defaults. Every knob is blank until assigned a CC (globally via the
            # knobmap, or per-page). The old bpm / track_vel / spread defaults
            # (shift0=bpm, alt=the shared DIALS_ALT) were debris from when "be a
            # general controller" was a bolt-on -- meaningless on a surface whose
            # whole job is to send signal out. (2026-09-02, Fire reorg cleanup.)
            "universal": {
                "base":  {},
                "shift": {},
                "alt":   {},
            },
        }

        self._dialmap = {}
        for view in VIEWS:
            vdef = _view_defaults.get(view)
            self._dialmap[view] = {}
            for layer in self._layers:
                # Priority: config per-view override > built-in per-view > global config
                cfg_key = f"DIALS_{view.upper()}_{layer.upper()}"
                cfg_override = self.fcfg.get(cfg_key)
                if cfg_override is not None:
                    src = cfg_override
                elif vdef and layer in vdef:
                    src = vdef[layer]
                else:
                    src = cfg_src.get(layer, {})
                self._dialmap[view][layer] = {
                    idx: tuple(action)
                    for idx, action in (src or {}).items()
                }

        # seq view: ALT+knob2 (the one slot deliberately left free in DIALS_ALT)
        # cycles the SELECTED track's colour through the palette -- lets you
        # recolour a track live without opening a menu (owner's ask). Per-track
        # concept, so it lives only in the track-centric seq view. setdefault so a
        # config DIALS_SEQ_ALT[2] override (if the user ever sets one) still wins.
        self._dialmap.setdefault("seq", {}).setdefault("alt", {}).setdefault(
            2, ("track_color",))

        # seq view: ALT+knob3 = track BRIGHTNESS (the render multiplier), the
        # neighbour of knob2's colour. The global DIALS_ALT[3] is arp "spread",
        # which isn't meaningful in the step sequencer, so it's repurposed HERE
        # ONLY -- spread stays on ALT+knob3 in every other view. A user-set
        # DIALS_SEQ_ALT override still wins (respect it via setdefault); otherwise
        # replace the inherited global spread seed.
        _seq_alt = self._dialmap.setdefault("seq", {}).setdefault("alt", {})
        if self.fcfg.get("DIALS_SEQ_ALT") is None:
            _seq_alt[3] = ("track_bright",)
        else:
            _seq_alt.setdefault(3, ("track_bright",))

        # "fx" is not in VIEWS (so it's excluded from the MODE cycle), but
        # _dispatch_dial looks up self._dialmap[self.view] so it needs an entry.
        # All four knobs on all three layers call _dial_fx_param -- the method
        # itself figures out which plugin and param to target based on the active
        # layer (bare/shift/alt) and which slot is selected.
        _fx_knob = ("fx_param",)
        self._dialmap["fx"] = {
            "base":  {0: _fx_knob, 1: _fx_knob, 2: _fx_knob, 3: _fx_knob},
            "shift": {0: _fx_knob, 1: _fx_knob, 2: _fx_knob, 3: _fx_knob},
            "alt":   {0: _fx_knob, 1: _fx_knob, 2: _fx_knob, 3: _fx_knob},
        }

    def _layer_now(self):
        """Which knob layer the held modifiers currently select.
        In FX view a pad-press can set a soft layer; physical SHIFT/ALT override it."""
        if self._shift: return "shift"
        if self._alt:   return "alt"
        if self.view == "fx" and self._fx_soft_layer:
            return self._fx_soft_layer
        return "base"

    def _target_label(self, action):
        """Menu label for an action tuple (falls back to the action name)."""
        if action is None:
            return "-"
        for label, act in self._targets:
            if tuple(act) == tuple(action):
                return label
        return action[0]

    def _kmap_view_now(self):
        """The view the knob-map overlay is editing.

        Pinned at _enter_kmap(); falls back to the live view only if the
        overlay was somehow entered without one.
        """
        v = getattr(self, "_kmap_view", None)
        return v if v in self._dialmap else self.view

    def _kmap_targets(self):
        """Assignable knob targets for the view being edited.

        UNIVERSAL and FADERS are external-MIDI control surfaces: they have no
        drum voice to sculpt, and __init__ already STRIPS every voice_param
        action out of their dial maps when the saved knobmap loads. Offering
        those targets in the menu anyway meant you could pick "attack" there,
        watch it take, and find it silently gone after the next restart --
        the menu promised something the loader would undo. Filtering here
        keeps the two consistent.
        """
        view = self._kmap_view_now()
        return [t for t in self._targets
                if _target_allowed_in_view(t[1], view)]

    def _kmap_sel_target(self):
        """The (label, action) under the cursor, or None if there are none.

        Indexes modulo the list length because _kmap_targets is per-view and
        the view can change while the overlay is open (the mode buttons still
        work), which would otherwise leave the cursor past the end.
        """
        targets = self._kmap_targets()
        if not targets:
            return None
        return targets[self._kmap_sel % len(targets)]

    def _enter_kmap(self):
        """Enter live knob-mapping overlay (ALT+BROWSER). BROWSER always exits."""
        self._overlay    = "kmap"
        self._kmap_view  = self.view
        # Pin the universal PAGE too: in universal, on-device knob assigns write
        # the CURRENT page's per-page override (P2), so a GRID page-flip mid-edit
        # must not silently retarget another page. Harmless (default 0) elsewhere.
        self._kmap_bank  = getattr(self, "_universal_bank", 0)
        self._kmap_knob  = 0
        self._kmap_layer = "base"
        self._sync_kmap_sel()
        self._oled()

    def _kmap_view_label(self):
        """Short label for the kmap screens. In universal, name the PAGE being
        edited (uP<n>) rather than just the view -- knob assigns there are
        per-page now, so which page matters and must be visible."""
        if self._kmap_view_now() == "universal":
            return f"uP{getattr(self, '_kmap_bank', self._universal_bank) + 1}"
        return self._kmap_view_now()[:6]

    def _kmap_current_action(self):
        """The knob action the kmap editor is editing. In universal this is the
        pinned PAGE's per-page override (falling back to the baseline dialmap), so
        the editor seeds from and reports the SAME value the on-device assign
        writes -- not the shared baseline it used to read."""
        layer, knob = self._kmap_layer, self._kmap_knob
        if self._kmap_view_now() == "universal":
            bank = getattr(self, "_kmap_bank", self._universal_bank)
            ov = self._universal_knobs.get(bank, {}).get(layer, {}).get(knob)
            if ov:
                return ov
            return self._dialmap.get("universal", {}).get(layer, {}).get(knob)
        return self._dialmap.get(self._kmap_view_now(), {}).get(layer, {}).get(knob)

    def _kmap_commit_action(self, action):
        """Write a committed knob action to the right store: the current PAGE's
        per-page override in universal (each page gets its own knobs, authored
        on-device via set_universal_knob), else the view's baseline dialmap.
        Persists either way. A 'none' assign in universal CLEARS the override
        (falls the knob back to the blank baseline), which always applies."""
        if self._kmap_view_now() == "universal":
            bank = getattr(self, "_kmap_bank", self._universal_bank)
            act  = None if tuple(action) == ("none",) else tuple(action)
            self.set_universal_knob(bank, self._kmap_layer, self._kmap_knob, act)
        else:
            view = self._kmap_view_now()
            self._dialmap[view][self._kmap_layer][self._kmap_knob] = tuple(action)
            self._save_knobmap()

    # Keep old name as alias so any config/external calls still work.
    _toggle_knob_map = _enter_kmap

    def _sync_kmap_sel(self):
        """Point the menu cursor at the knob's current assignment, so editing
        starts from what's already mapped instead of the top of the list.

        A knob already holding an ARBITRARY cc_out has no named entry to land
        on, so the cursor stays at index 0 -- which is the CC OUT editor.
        That is the right place for it to be: the way to change such a knob is
        to re-open the editor, and it opens seeded with the current values.
        """
        cur = self._kmap_current_action()
        self._kmap_sel = 0
        if cur is not None:
            for i, (_, act) in enumerate(self._kmap_targets()):
                if tuple(act) == tuple(cur):
                    self._kmap_sel = i
                    break

    def _kmap_assign(self):
        """Commit the highlighted menu target to the selected knob/layer of the
        current view, then persist. The CC OUT sentinel opens the editor
        instead of assigning -- it is not a real action."""
        entry = self._kmap_sel_target()
        if entry is None:
            return
        _, action = entry
        if tuple(action) == _KMAP_CC_ACTION:
            self._enter_kmap_cc()
            return
        self._kmap_commit_action(action)
        self._oled_kmap(confirm=True)

    # -- CC OUT editor (2026-07-31) -------------------------------------------
    # Set a knob to an ARBITRARY ("cc_out", port, channel, cc) from the device.
    # Previously the only route to this was the /control web page, and even
    # that did not survive a restart (see _well_formed_send_action).

    def _assign_ports(self):
        """MIDI destinations, enumerated LIVE from the engine's port table.

        Not a hardcoded ["pisound", "synth"]: that pair is what the pad and
        fader assign screens have always offered, so an rtp session or a
        hot-plugged device opened by MIDI_SCAN was unreachable from the
        hardware no matter what was connected. Recognising hardware by name
        and adapting is this project's stated north star; a two-item literal
        is the opposite of it.

        Falls back to the historical pair if the table cannot be read, so the
        editor degrades to the old behaviour rather than to nothing.
        """
        try:
            ports = sorted(k for k in getattr(self.eng, "port_outs", {}) if k)
        except Exception:
            ports = []
        for fallback in ("pisound", "synth"):
            if fallback not in ports:
                ports.append(fallback)
        return ports

    def _enter_kmap_cc(self):
        """Open the CC OUT editor, seeded from the knob's current assignment.

        The TARGET VIEW is pinned here, not read again at commit time. The
        mode buttons are not gated on an open overlay -- `_set_view` only
        special-cases `track_edit` -- so pressing one mid-edit changes
        `self.view` underneath you, and a commit that read `self.view` would
        silently land the assignment on a different view's knob. The screen
        names the pinned view for the same reason.
        """
        if getattr(self, "_kmap_view", None) not in self._dialmap:
            self._kmap_view = self.view
        cur = self._kmap_current_action()
        seed = {"port": "pisound", "channel": 1, "cc": 74}
        if (isinstance(cur, (list, tuple)) and len(cur) == 4
                and cur[0] == "cc_out"):
            try:
                if isinstance(cur[2], str) and _send_ch_ok(str(cur[1]), cur[2]):
                    ch = cur[2]                       # a module name
                else:
                    ch = max(1, min(16, int(cur[2])))
                seed = {"port": str(cur[1]), "channel": ch,
                        "cc": max(0, min(127, int(cur[3])))}
            except (TypeError, ValueError):
                pass
        self._kmap_cc = seed
        self._overlay = "kmap_cc"
        self._oled()

    def _kmap_cc_dial(self, idx, step):
        """Encoders in the CC OUT editor: 0 = CC number, 1 = channel, 2 = port.
        On port "synth" the channel encoder also offers the MODULES (before
        ch 1): a knob that names a module follows it around /matrix."""
        e = self._kmap_cc
        if idx == 0:
            e["cc"] = max(0, min(127, int(e.get("cc", 74)) + step))
        elif idx == 1:
            opts = self._kmap_ch_options(e.get("port", "pisound"))
            cur  = e.get("channel", 1)
            i    = opts.index(cur) if cur in opts else opts.index(1)
            e["channel"] = opts[max(0, min(len(opts) - 1, i + step))]
        elif idx == 2:
            ports = self._assign_ports()
            cur   = e.get("port", "pisound")
            i     = ports.index(cur) if cur in ports else 0
            e["port"] = ports[(i + step) % len(ports)]
            if isinstance(e.get("channel"), str) and e["port"] != "synth":
                e["channel"] = 1          # modules exist only on the synth bus
        else:
            return
        self._oled_kmap_cc()

    def _kmap_ch_options(self, port):
        """Channel choices in the CC OUT editor, in encoder order."""
        mods = [m for m in (getattr(self.cfg, "MODULE_KEYS", None) or sorted(_MODULE_KEYS))
                if m in _MODULE_KEYS] if port == "synth" else []
        return mods + list(range(1, 17))

    def _kmap_cc_commit(self):
        """SELECT press in the CC OUT editor: assign, persist, close.

        Assigns to the view the editor was OPENED on (see _enter_kmap_cc),
        which is not necessarily the current one.
        """
        e = self._kmap_cc
        port = str(e.get("port", "pisound"))
        ch   = e.get("channel", 1)
        if isinstance(ch, str):
            ch = ch if _send_ch_ok(port, ch) else 1   # module only on the synth port
        else:
            ch = int(ch)
        action = ("cc_out", port, ch, int(e.get("cc", 74)))
        self._kmap_commit_action(action)
        self._overlay = "kmap"
        self._sync_kmap_sel()
        self._oled_kmap(confirm=True)

    def _kmap_ch_text(self, ch):
        """'ch 16' for a channel, the module's short name (e.g. 'WAVE') for a
        module -- keeps the editor's channel line within the 16-char panel."""
        if isinstance(ch, str):
            return _MODULE_SHORT.get(ch, ch[:5])
        return f"ch {int(ch)}"

    def _oled_kmap_cc(self):
        """CC OUT editor screen.

        Each line names the encoder that edits it, which is why there is no
        separate legend line: on a 16-character panel a legend costs a whole
        line to say what an inline prefix says for three characters.
        """
        if not fire_oled:
            return
        e = self._kmap_cc
        layer_tag = {"shift": " SH", "alt": " ALT"}.get(self._kmap_layer, "")
        view = self._kmap_view_label()
        # The CC number gets a line to ITSELF. It briefly shared one with the
        # view name -- added to satisfy a review finding -- and in the
        # `universal` view (the longest name, and the one this feature exists
        # for) "universal  E0 cc 74" rendered as "universal  E0 cc": the value
        # was cut off every time, so the knob looked like it did nothing.
        # Reported from the field within minutes of deploy. The width net could
        # not see it because the line was wrapped in fit(), which truncates
        # silently and so opts a line out of the check entirely.
        # The view now rides the channel line, where the worst case
        # ("E1 ch 16  univer") is exactly 16.
        fire_oled.show_lines([
            (f"CC OUT K{self._kmap_knob + 1}{layer_tag}", 2, 0, 2),
            (f"E0 cc {int(e.get('cc', 74))}", 2, 20, 1),
            (f"E1 {self._kmap_ch_text(e.get('channel', 1))}  {view[:6]}", 2, 34, 1),
            (fire_oled.fit(f"E2 {e.get('port', 'pisound')}", 2, 1,
                           why="port key"), 2, 48, 1)])

    def _load_knobmap(self):
        """Merge a persisted knob map (if any) over the config-seeded defaults.

        An action is accepted if it is either (a) one of the named targets in
        self._targets, or (b) a structurally valid free-form send action --
        an arbitrary ("cc_out", port, ch, cc) or ("pitch_out", port, ch). A
        stale or hand-edited file still cannot inject an unknown action: (b)
        is checked field by field by _well_formed_send_action, and both
        handlers already exist and already take exactly these arguments.

        Case (b) was missing until 2026-07-31, which meant arbitrary CC
        assignments -- the entire point of an assignable encoder, and the one
        thing the /control page could do that the device could not -- did not
        survive a restart. See _well_formed_send_action.
        """
        path = self._knobmap_file
        if not path:
            return
        try:
            with open(os.path.expanduser(path)) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as e:
            print(f"  ! Fire knobmap load failed: {e}")
            return
        # The CC OUT sentinel is a menu entry, not an assignable action: it has
        # no _dial_cc_custom handler, so letting it through from a hand-edited
        # file would produce a knob that logs "control mapped to 'cc_custom'
        # but no _dial_cc_custom() exists" and does nothing. The accept rule
        # lives in _knob_action_accepted (shared with the per-page validator so
        # the two can't drift).
        migrated, literal = [], []
        if not isinstance(data, dict):
            data = {}
        # The literal-channel -> module migration runs ONCE: a file carrying
        # _KNOBMAP_VERSION was already migrated, so a synth ch 8 CC in it is
        # the player's choice and must stay a channel (it used to re-run on
        # every boot). "_version" is not a view, so the loop skips it.
        try:
            marked = int(data.get("_version", 0) or 0) >= _KNOBMAP_VERSION
        except (TypeError, ValueError):
            marked = False
        for view, layers in data.items():
            if view not in self._dialmap:
                continue
            for layer, knobs in (layers or {}).items():
                if layer not in self._layers:
                    continue
                for k, action in (knobs or {}).items():
                    try:
                        idx = int(k)
                    except (TypeError, ValueError):
                        continue
                    act = tuple(action) if isinstance(action, (list, tuple)) else None
                    # Shipped literal-channel synth sends -> the module they
                    # were written for (one-time, re-saved below; 2026-09-24).
                    new = act if marked else _migrate_legacy_send(view, act)
                    if new is not act:
                        migrated.append(f"{view}/{layer}/K{idx + 1} ch{act[2]}->{new[2]}")
                        act = new
                    elif (act and act[0] in _SEND_ACTION_TYPES and len(act) >= 3
                            and act[1] == "synth" and not isinstance(act[2], str)):
                        literal.append(f"{view}/{layer}/K{idx + 1} ch{act[2]}")
                    if 0 <= idx <= 3 and self._knob_action_accepted(act):
                        self._dialmap[view][layer][idx] = act
        if migrated:
            print(f"  Fire knobmap: {len(migrated)} knob(s) now name their module "
                  f"(follow /matrix): {', '.join(migrated)}", flush=True)
        if not marked:
            self._save_knobmap()        # writes the "_version" marker
        if literal:
            print(f"  Fire knobmap: still aimed at a synth CHANNEL NUMBER (not a "
                  f"module): {', '.join(literal)}", flush=True)

    def _save_knobmap(self):
        """Write the full per-view knob map to disk (no-op without a file)."""
        path = self._knobmap_file
        if not path:
            return
        out = {view: {layer: {str(idx): list(act) for idx, act in knobs.items()}
                      for layer, knobs in layers.items()}
               for view, layers in self._dialmap.items()}
        out["_version"] = _KNOBMAP_VERSION   # "already migrated" (see _load_knobmap)
        try:
            with open(os.path.expanduser(path), "w") as fh:
                json.dump(out, fh)
        except Exception as e:
            print(f"  ! Fire knobmap save failed: {e}")

    # -- Entry point -----------------------------------------------------------

    def handle_msg(self, msg):
        if not hasattr(msg, "channel"):
            return
        t = msg.type
        if t == "note_on" and msg.velocity > 0:
            self._note_on(msg)
        elif t == "note_off" or (t == "note_on" and msg.velocity == 0):
            self._note_off(msg)
        elif t == "control_change":
            self._cc(msg)

    def _note_on(self, msg):
        n = msg.note
        if PAD_NOTE_BASE <= n <= PAD_NOTE_MAX:
            self._pad_press(n, msg.velocity)
        elif n in DIAL_TOUCH_NOTE:
            # Touching a knob while in map mode selects that knob (and the layer
            # implied by any held modifier) as the one being remapped.
            if self._overlay == "kmap":
                self._kmap_knob  = DIAL_CC.get(n, 0)
                self._kmap_layer = self._layer_now()
                self._sync_kmap_sel()
                self._oled()
        elif n in MUTE_ROW_NOTE:
            self._toggle_mute(MUTE_ROW_NOTE[n])
        elif n in self._buttons:
            self._dispatch_button(self._buttons[n], True)

    def _note_off(self, msg):
        n = msg.note
        if PAD_NOTE_BASE <= n <= PAD_NOTE_MAX:
            self._pad_release(n)
        elif n in self._buttons:
            self._dispatch_button(self._buttons[n], False)

    def _cc(self, msg):
        if msg.control in DIAL_CC:
            self._dispatch_dial(DIAL_CC[msg.control], decode_relative(msg.value))
        elif msg.control == SELECT_CC:
            self._dispatch_select(decode_relative(msg.value))

    # -- Pads ------------------------------------------------------------------

    def _pad_press(self, note, vel):
        row, col = note_to_rc(note)
        # Session E (2026-06-30): subscreen intercepts every pad while active,
        # momentary or latched -- the mode-jump row is the only live content
        # in this pass (full per-mode subscreen content is Step 15).
        if self._subscreen or self._subscreen_latched:
            self._subscreen_pad_press(row, col)
            return
        # KEY overlay owns every pad while ALT is held on a keyboard view --
        # press root/scale/toggles instead of playing notes.
        if self._key_overlay_active():
            self._key_overlay_pad_press(row, col)
            return
        # 2026-08-04 adversarial review: unlike _dispatch_dial/_dispatch_select
        # (which check every self._overlay value, and _menu_open, before
        # falling through to per-view dispatch), _pad_press had NO check for
        # either -- so with the BROWSER menu or any named overlay (kmap,
        # kmap_cc, track_edit, chord_rows, fx_browse, preset_picker) open and
        # visibly owning the OLED, every pad press still fell straight through
        # to the per-view handler below and silently edited live state (e.g.
        # toggling a seq step) underneath the menu the screen was showing.
        # None of these overlays have their own pad semantics today (only
        # dial/SELECT), so the correct behavior is to consume the press and
        # do nothing, exactly like _dispatch_select already does for
        # "kmap_cc" turns that don't apply.
        if self._menu_open or self._overlay is not None:
            return
        if self.view == "seq":
            tidx = self.view_top + row
            if tidx >= self.n_tracks:
                return
            trk = self.tracks[tidx]
            step = self.page * N_COLS + col
            # 2026-07-01 ownership refactor: routed through seq's lock-guarded
            # methods instead of direct trk[...] writes (Bug B).
            if self._alt:
                self.seq.set_track_prop(tidx, "length", step + 1)
            elif self._shift:
                # Cycle this step's velocity tier (None->ghost->normal->accent).
                # No-op on an OFF step. OLED shows the new tier + velocity so the
                # gesture is legible (the old faint LED bump felt like nothing).
                if step < trk["length"] and trk["steps"][step]:
                    self.seq.cycle_step_vel(tidx, step)
                    self._render_channel()
                    self._oled_step_vel(tidx, step)
                    return
            else:
                if step < trk["length"]:
                    self.seq.toggle_step(tidx, step)
                # Record held pad -- SELECT encoder can now dial in a note for this step
                self._held_seq_pad = (tidx, step)
            self._render_channel()
            self._oled()
        elif self.view == "mixer":
            tidx = self.view_top + row
            if tidx < self.n_tracks:
                self.seq.set_track_prop(tidx, "vel", int((col + 1) / N_COLS * 127))
                self._render_mixer()
        elif self.view == "faders":
            fidx = self._mpc_view_top + row
            self._mpc_last_fader = fidx
            if self._mpc_assign_mode:
                # In assign mode a pad tap just updates which fader we're
                # editing -- useful for switching rows without exiting.
                self._mpc_assign_target = fidx
                existing = self._mpc_faders.get(fidx, {})
                self._mpc_edit = {
                    "type":    existing.get("type",    "cc"),
                    "number":  int(existing.get("number",  existing.get("cc", 7))),
                    "value":   int(existing.get("value",   64)),
                    "channel": int(existing.get("channel", 1)),
                    "port":    existing.get("port",    "pisound"),
                }
                self._render_faders()
                self._oled_mpc_assign()
                return
            # Normal play: set fader value from column position and send CC.
            val  = int((col + 1) * 127 // N_COLS)
            assign = self._mpc_faders.get(fidx)
            if assign is not None:
                assign["value"] = val
                self._mpc_fader_send(fidx, val)
            self._render_faders()
            self._oled()
        elif self.view == "universal":
            key = (row, col)
            # Fader row (Phase 3): tap a column to set the level 0-127 + send CC.
            # Takes precedence over per-pad handling (but not over assign mode).
            frow = self._universal_faders.get(
                self._universal_bank, {}).get(row)
            if frow is not None and not self._universal_assign_mode:
                val = int((col + 1) * 127 // N_COLS)
                frow["value"] = val
                self._universal_fader_send(frow, val)
                self._save_fire_state()
                self._render_universal()
                self._oled_universal_fader(row, val)
                return
            self._last_universal_pad = key
            if self._universal_assign_mode:
                # In assign mode, SHIFT/ALT selects which action layer
                # to edit next.  Plain tap just updates the target display.
                if self._shift:
                    self._universal_assign_layer = "shift"
                    self._oled_universal_assign()
                elif self._alt:
                    self._universal_assign_layer = "alt"
                    self._oled_universal_assign()
                return
            assign = self._universal_pads.get(key)
            if assign is None:
                self._oled_universal()   # hint: "unassigned / SELECT to assign"
                return
            if assign.get("latch"):
                # Toggle: flip latch state and send the matching message.
                on = not assign.get("_latched_on", False)
                assign["_latched_on"] = on
                self._universal_fire_pad(key, assign, vel, pressed=True,
                                          latch_state=on)
            else:
                # Momentary: note-on/CC on press; note-off fires on pad release
                # via _pad_release (see below).
                self._universal_held[key] = True
                self._universal_fire_pad(key, assign, vel, pressed=True)
            self._render_universal()
            self._oled()
        elif self.view == "chord":
            if col < 4:
                # Control zone (cols 0-3): chord engine controls.
                # NOT stored in _pad_held -- only keyboard notes need note-off tracking.
                self._perform_ctrl_press(row, col, vel)
            else:
                # Keyboard zone (cols 4-15): routed per row config.
                self._perform_key(row, col, note, vel)
            self._render_perform()
        elif self.view == "poly":
            _stride = 5 if self._fourths_mode else N_COLS
            n  = self.poly_base_note + (N_ROWS - 1 - row) * _stride + col
            n  = self._snap(n)       # scale lock (no-op when SNAP is off)
            if n is None:
                # FILTER mode: off-scale notes are silenced, not moved. Nothing
                # is recorded in _poly_held, so the matching release correctly
                # finds nothing to turn off.
                self._render_poly()
                return
            ch = self._poly_ch_now   # poly view's own active voice
            self._poly_held[note] = (n, ch)
            self._chip_note_on(n, vel, ch)
            self._render_poly()
        elif self.view == "chip":
            _stride = 5 if self._fourths_mode else N_COLS
            pitch_n = self.chip_base_note + (N_ROWS - 1 - row) * _stride + col
            pitch_n = self._snap(pitch_n)   # scale lock (no-op when SNAP is off)
            if pitch_n is None:
                self._render_chip()         # FILTER: silenced, nothing held
                return
            ch_now  = self._chip_ch_now
            self._chip_held[note] = (pitch_n, ch_now)  # remember channel for note-off
            self._chip_note_on(pitch_n, vel, ch_now)
            self._render_chip()

        elif self.view == "fx":
            # Rows 0-2, cols 8-15: mode-jump pads — exit FX and jump to mode.
            if row < N_ROWS - 1 and col >= 8:
                j = col - 8
                if j < len(_SUBSCREEN_JUMP):
                    _jname, _ = _SUBSCREEN_JUMP[j]
                    self._fx_soft_layer  = None
                    self._fx_last_param  = None
                    self._pre_fx_view    = _jname   # so PAT_SONG exit returns here
                    # 2026-08-04 adversarial review: was a direct `self.view =
                    # _jname` assignment, a third hand-rolled view-transition
                    # path that bypassed _set_view()'s overlay/menu/pin
                    # cleanup. Currently harmless here because the KEY-overlay
                    # pin is already cleared by the time you're inside FX view
                    # (_act_fx_view does its own cleanup) -- but that made it
                    # a silent trap waiting for whatever changes upstream next.
                    # Route it through _set_view() like every other transition.
                    self._set_view(_jname)
                return
            # Rows 0-2, cols 0-3: parameter visualisation grid.
            # Pressing a pad selects the encoder layer for that row so the user can
            # turn knobs to edit that layer's params without holding SHIFT/ALT.
            # Physical SHIFT/ALT always take priority (see _layer_now()).
            if row < N_ROWS - 1 and col < 4:
                self._fx_soft_layer = {2: "base", 1: "shift", 0: "alt"}.get(row, "base")
                self._render_fx()   # active-row highlight updates immediately
                self._oled_flash("LAYER", self._fx_soft_layer.upper())
                return
            # Row 3: rack-slot pads.  Drives eng.fx.rack (3 series slots).
            rack = self._fx_rack()
            if row == N_ROWS - 1 and rack is not None:
                st   = rack.status()
                cap  = int(st.get("cap", 3))
                if col >= cap:
                    return
                slots  = st.get("slots", [])
                s      = slots[col] if col < len(slots) else None
                plugin = s.get("plugin") if s else None

                if self._shift and plugin:
                    # SHIFT + assigned pad: TOGGLE the slot on/off. Plain press
                    # just selects, so you can adjust params without flipping the
                    # slot (matches the docstring; owner's 2026-07-21 gesture).
                    self._fx_sel_slot   = col
                    self._fx_held_slot  = col
                    self._fx_last_param = None
                    self._render_fx()   # immediate selection feedback; the rebuild
                    self._oled()        # (1-4s) repaints green via _fx_after_change
                    rack.toggle(col, done=self._fx_after_change)
                elif self._fx_held_slot is not None and col != self._fx_held_slot:
                    # Multi-touch hold A then press B: swap the two slots' plugins
                    # (= reorder within the series). rack.move swaps by delta.
                    a, b = self._fx_held_slot, col
                    self._fx_sel_slot   = col
                    self._fx_held_slot  = col
                    self._fx_last_param = None
                    self._render_fx()   # immediate feedback; rebuild follows
                    self._oled()
                    rack.move(a, b - a, done=self._fx_after_change)
                elif not plugin:
                    # Empty slot: open the (banked) plugin browser.
                    self._fx_sel_slot  = col
                    self._fx_held_slot = col
                    self._fx_browse_enter(col)
                elif col == self._fx_sel_slot:
                    # Press the ALREADY-SELECTED assigned slot again: reopen the
                    # plugin browser to SWAP this slot for a different plugin.
                    # (First press selects; a second press re-browses.) This is
                    # the discoverable reassign gesture -- the old way, holding
                    # the pad while pressing the SELECT knob, still works too.
                    # 2026-07-21: owner couldn't find any way to change a slot
                    # once set; this makes it obvious.
                    self._fx_held_slot = col
                    self._fx_browse_enter(col)
                else:
                    # Plain press on a DIFFERENT assigned slot: SELECT ONLY
                    # (no toggle, no browse -- just move the selection here).
                    self._fx_sel_slot   = col
                    self._fx_held_slot  = col
                    self._fx_last_param = None
                    self._render_fx()
                    self._oled()

    def _pad_release(self, note):
        # Always clear held seq pad -- note editing gesture is done
        if self.view == "seq":
            self._held_seq_pad = None
        if self.view == "fx":
            self._fx_held_slot = None
        # Universal: send note-off for any momentary note pads on release.
        # This runs regardless of current view -- a pad held while in
        # universal must get its note-off even if the user switched views.
        _u_key = note_to_rc(note)
        if _u_key in self._universal_held:
            del self._universal_held[_u_key]
            _u_assign = self._universal_pads.get(_u_key)
            if _u_assign and not _u_assign.get("latch"):
                self._universal_fire_pad(_u_key, _u_assign, 0, pressed=False)
            if self.view == "universal":
                self._render_universal()
        # Chord extension pads: fire note-off so extensions are momentary.
        # Runs regardless of current view (pad may have been held across a
        # view switch). handle_ctrl_note_off removes the ext from chord_exts.
        if note in self._perf_ext_held:
            ctrl_note = self._perf_ext_held.pop(note)
            try:
                self.eng.handle_ctrl_note_off(ctrl_note)
            except Exception as e:
                # Fire-side bookkeeping is already popped above, so a
                # failure here leaves the extension stuck on in the chord
                # engine with nothing to show for it -- log it.
                print(f"  ! Fire chord-ext note-off: {e}", flush=True)
            if self.view == "chord":
                self._render()
        # Direct keyboard rows: fire note-off regardless of current view
        # (user may have switched view while holding a direct pad).
        if note in self._dir_held:
            port_key, ch, pitch = self._dir_held.pop(note)
            try:
                self.eng.note_off_out(pitch, port_key, ch)
            except Exception as e:
                print(f"  ! Fire direct note-off: {e}", flush=True)
            if self.view == "chord":
                self._render()
        if self.view == "chord" and note in self._pad_held:
            root = self._pad_held.pop(note)
            try:
                self.eng.handle_note_off_input(root)
            except Exception as e:
                print(f"  ! Fire pad note-off: {e}", flush=True)
            self._render()
        elif note in self._poly_held:
            held_data = self._poly_held.pop(note)
            if isinstance(held_data, tuple):
                n, ch = held_data
            else:
                n, ch = held_data, self._ch_poly
            self._chip_note_off(n, ch)
            if self.view == "poly":
                self._render()
        elif note in self._chip_held:
            held_data = self._chip_held.pop(note)
            # held_data is (pitch_note, channel) or legacy bare pitch_note
            if isinstance(held_data, tuple):
                pitch_n, ch = held_data
            else:
                pitch_n, ch = held_data, self._ch_chip
            self._chip_note_off(pitch_n, ch)
            if self.view == "chip":
                self._render()

    def _toggle_mute(self, row):
        # In chip view, MUTE1-4 select the active NES/GB voice:
        #   Row 0 = Pulse 1   Row 1 = Pulse 2   Row 2 = Triangle
        #   Row 3 = Noise / Game Boy wave (tap to toggle between them)
        if self.view == "chip":
            if self._shift and row == 0:
                # SHIFT+MUTE1: cycle P1+P2 coupling mode.
                # Fire and orc1_synth.py are separate processes with no
                # shared object (see coupling-mode bugfix writeup) -- Fire
                # cycles its OWN local mode, then pushes the result to the
                # synth as CC72 over the synth bus, same pattern as every
                # other live synth parameter (_select_chip_voice's cc_out
                # dial entries).
                idx = CHIP_COUPLE_MODES.index(self._chip_couple_mode)
                self._chip_couple_mode = CHIP_COUPLE_MODES[(idx + 1) % len(CHIP_COUPLE_MODES)]
                self._send_chip_coupling_cc()
                self._sync_chip_dials()
                self._render_chip()
                self._oled()
                return

            if self._alt and row == 0:
                # ALT+MUTE1: toggle P1/P2 stereo spread. Makes coupling
                # modes (octave/unison especially) easier to hear apart --
                # pulse1 emphasized left, pulse2 emphasized right, instead
                # of both blending into one mono voice. Sent as CC76;
                # auto-enables stereo mode on the synth side since spread
                # is otherwise silently inaudible in the default mono mix.
                self._chip_p1p2_spread = not self._chip_p1p2_spread
                self._send_chip_spread_cc()
                if fire_oled:
                    try:
                        fire_oled.show_lines([
                            ("P1/P2 SPREAD", 2, 0, 2),
                            ("ON" if self._chip_p1p2_spread else "OFF", 2, 30, 1)])
                    except Exception:
                        pass
                return

            if row == 1 and (self._shift or self._alt):
                # SHIFT+MUTE2: detune +1¢    ALT+MUTE2: detune -1¢
                # Hold it down for fast sweep -- each press = 1 cent
                delta = +1 if self._shift else -1
                self._chip_detune_cents = max(-50, min(50, self._chip_detune_cents + delta))
                self._send_chip_detune_cc()
                self._oled()
                return

            # Plain MUTE presses: voice select (unchanged)
            if row < 3:
                self._select_chip_voice(('pulse1', 'pulse2', 'triangle')[row])
            elif row == 3:
                # Tap MUTE4 to cycle: noise -> wave -> noise …
                nxt = 'wave' if self.chip_voice == 'noise' else 'noise'
                self._select_chip_voice(nxt)
            self._render_chip()
            self._oled()
            return
        if self.view == "poly":
            # MUTE1-4  : voice select -- same pool as chip view
            #   plain   -> pulse1 / pulse2 / triangle / noise
            #   ALT     -> MUTE1=wave   MUTE2=poly (returns to CH_POLY)
            #   SHIFT   -> waveform on poly synth (CC1 on CH_POLY)
            #              MUTE1=sine(0)  MUTE2=saw(32)  MUTE3=square(64)  MUTE4=tri(96)
            _POLY_WAVE_CC   = (0, 32, 64, 96)
            _POLY_WAVE_NAME = ("SINE", "SAW", "SQUARE", "TRI")
            if self._shift:
                if mido and row < 4 and self._ch_poly is not None:   # unbound: silent
                    try:
                        self.eng.send_synth_midi(mido.Message(
                            "control_change", channel=self._ch_poly - 1,
                            control=1, value=_POLY_WAVE_CC[row]))
                        if fire_oled:
                            fire_oled.show_lines([
                                ("POLY WAVE", 2, 0, 2),
                                (_POLY_WAVE_NAME[row], 2, 30, 1)])
                    except Exception:
                        pass
            elif self._alt:
                if row == 0:
                    self._select_poly_voice('wave')
                elif row == 1:
                    self._select_poly_voice('poly')
                self._oled()
            else:
                _voices = ('pulse1', 'pulse2', 'triangle', 'noise')
                self._select_poly_voice(_voices[row])
                self._oled()
            self._render_poly()
            return
        if self.view == "chord":
            # MUTE1-4: waveform select for poly synth (chord engine output target)
            #   MUTE1=sine(0)  MUTE2=saw(32)  MUTE3=square(64)  MUTE4=tri(96)
            _CHORD_WAVE_CC   = (0, 32, 64, 96)
            _CHORD_WAVE_NAME = ("SINE", "SAW", "SQUARE", "TRI")
            if (not self._shift and not self._alt and mido and row < 4
                    and self._ch_poly is not None):          # poly unbound: silent
                try:
                    self.eng.send_synth_midi(mido.Message(
                        "control_change", channel=self._ch_poly - 1,
                        control=1, value=_CHORD_WAVE_CC[row]))
                    if fire_oled:
                        fire_oled.show_lines([
                            ("CHORD WAVE", 2, 0, 2),
                            (_CHORD_WAVE_NAME[row], 2, 30, 1)])
                except Exception as e:
                    # send_synth_midi's voice_thru mirror path (unlike its
                    # main bus send) isn't internally guarded, and
                    # fire_oled.show_lines talks to real I2C hardware -- both
                    # can genuinely raise here. Log it so the MUTE button
                    # appearing to do nothing has a journal line behind it.
                    print(f"  ! Fire chord voice CC1: {e}", flush=True)
            return
        # FADERS view: MUTE row toggles fader lane between 0 and last value.
        if self.view == "faders":
            fidx   = self._mpc_view_top + row
            assign = self._mpc_faders.get(fidx)
            if assign is not None:
                cur = assign.get("value", 0)
                if cur > 0:
                    assign["_pre_mute"] = cur
                    assign["value"] = 0
                    self._mpc_fader_send(fidx, 0)
                else:
                    restore = assign.pop("_pre_mute", 64)
                    assign["value"] = restore
                    self._mpc_fader_send(fidx, restore)
                self._render_faders()
                self._oled()
            return
        # In any other non-track view: consume silently so you don't
        # accidentally mute a track you can't even see.
        if self.view not in ("seq", "mixer"):
            return
        tidx = self.view_top + row
        if tidx >= self.n_tracks:
            return

        # 2026-07-01 ownership refactor: routed through seq's lock-guarded
        # solo_track/unmute_all/set_track_mute (one atomic pass under the
        # lock each, instead of an unguarded per-track loop -- Bug B).
        if self._alt:
            # ALT + MUTE row = un-solo everything: restore all tracks to unmuted
            self.seq.unmute_all()
            self._render()
            if fire_oled:
                try:
                    fire_oled.show_lines([("ALL TRACKS", 2, 0, 2),
                                          ("unmuted", 2, 30, 1)])
                except Exception:
                    pass
            return

        if self._shift:
            # SHIFT + MUTE row = solo: mute everything except this track
            self.seq.solo_track(tidx)
            self._render()
            name = self.tracks[tidx].get("name", f"Tr {tidx+1}")
            if fire_oled:
                try:
                    fire_oled.show_lines([("SOLO", 2, 0, 2),
                                          (name, 2, 30, 1)])
                except Exception:
                    pass
            return

        # Plain mute toggle
        self.seq.set_track_mute(tidx)
        self._render()

    def _send_chip_coupling_cc(self):
        """Push the current local coupling mode to orc1_synth as CC72 on
        CH_CHIP (Pulse 1's channel). 0-127 spread evenly across the 7
        coupling modes -- must match the same bucketing NESEngine.set_cc
        uses to decode it (value * 7 // 128)."""
        if not mido or self._ch_chip is None:     # P1 unbound: silent
            return
        idx = CHIP_COUPLE_MODES.index(self._chip_couple_mode)
        # Middle of this mode's bucket, so the synth's own bucketing math
        # decodes it back to the same index even with rounding either way.
        cc_val = min(127, idx * 128 // len(CHIP_COUPLE_MODES) + 9)
        try:
            self.eng.send_synth_midi(mido.Message(
                "control_change", channel=self._ch_chip - 1,
                control=72, value=cc_val))
        except Exception as e:
            _swallowed("chip couple CC", e)

    def _send_chip_detune_cc(self):
        """Push the current local detune amount to orc1_synth as CC73 on
        CH_CHIP. Centered scheme: CC64 = 0ct, CC0/127 = ~-50/+50ct -- must
        match the decode math in NESEngine.set_cc."""
        if not mido or self._ch_chip is None:     # P1 unbound: silent
            return
        cc_val = max(0, min(127, round(self._chip_detune_cents / 50.0 * 64.0) + 64))
        try:
            self.eng.send_synth_midi(mido.Message(
                "control_change", channel=self._ch_chip - 1,
                control=73, value=cc_val))
        except Exception as e:
            _swallowed("chip detune CC", e)

    def _send_chip_spread_cc(self):
        """Push the P1/P2 stereo spread toggle to orc1_synth as CC76 on
        CH_CHIP. Standard on/off convention: 0 = off, 127 = on."""
        if not mido or self._ch_chip is None:     # P1 unbound: silent
            return
        try:
            self.eng.send_synth_midi(mido.Message(
                "control_change", channel=self._ch_chip - 1,
                control=76, value=127 if self._chip_p1p2_spread else 0))
        except Exception as e:
            # Fire's local _chip_p1p2_spread flag and OLED already show the
            # new state by the time this runs -- a failure here means the
            # synth silently never got it, with nothing else to reveal that.
            print(f"  ! Fire chip spread CC: {e}", flush=True)

    # -- Dispatch --------------------------------------------------------------

    def _dispatch_button(self, action, pressed):
        m = getattr(self, "_act_" + action[0], None)
        if m:
            try:
                m(pressed, *action[1:])
            except Exception as e:
                print(f"  ! Fire action '{action[0]}': {e}", flush=True)
        else:
            _unresolved("_act_", action[0])

    def _dispatch_dial(self, idx, step):
        # Pretty lights owns the whole surface, so knobs must not fall through to
        # the normal per-view dispatch. Knob 0 rides master speed live (on top of
        # each mode's baked-in _PL_SPEED_SCALE); the others are inert for now.
        if self._pretty_lights:
            if idx == 0:
                self._pl_speed = max(0.1, min(4.0, self._pl_speed + step * 0.05))
                if fire_oled:
                    fire_oled.show_lines([("SPEED", 2, 8, 3),
                                          (f"{self._pl_speed:.2f}x", 2, 40, 3)])
            return
        # CC OUT editor: knobs edit the destination being built. Must be
        # tested BEFORE the generic per-view dispatch below, or the encoders
        # would drive whatever the view normally maps them to while you are
        # trying to choose a CC number.
        if self._overlay == "kmap_cc":
            self._kmap_cc_dial(idx, step)
            return
        # Knobs are deliberately inert while remapping, so picking a target
        # never also blasts the parameter it's pointing at.
        if self._overlay == "kmap":
            return
        # Chord-row config overlay: knobs edit the selected row's routing.
        if self._overlay == "chord_rows":
            self._chord_rows_dial(idx, step)
            return
        # Universal assign mode: knobs control the assignment being edited.
        # Intercept before the normal view-based dial dispatch.
        if self._universal_assign_mode:
            self._universal_assign_dial(idx, step)
            return
        # MPC fader assign mode: knobs edit the fader assignment being edited.
        if self._mpc_assign_mode:
            self._mpc_assign_dial(idx, step)
            return
        # Track-edit overlay: knobs 0-3 directly edit the selected track's props.
        if self._overlay == "track_edit" and self.view in ("seq", "mixer"):
            if idx < len(self._te_props):
                self._te_edit_prop(idx, step, self._te_props[idx])
            return
        # Per-view layer: SHIFT and ALT each select a different knob layer, and
        # each VIEW has its own independent set (see _init_dialmaps).
        layer = self._layer_now()
        # Universal surface: a per-PAGE (per-bank) knob override wins over the
        # baseline, falling back to it per-knob (Phase 2). Other views read the
        # dialmap directly.
        if self.view == "universal":
            action = self._universal_knob_action(layer, idx)
        else:
            action = self._dialmap.get(self.view, {}).get(layer, {}).get(idx)
        if not action:
            return
        m = getattr(self, "_dial_" + action[0], None)
        if m:
            try:
                m(idx, step, *action[1:])
            except Exception as e:
                print(f"  ! Fire dial '{action[0]}': {e}", flush=True)
        else:
            _unresolved("_dial_", action[0])

    def _dial_fx_param(self, idx, step):
        """Turn a knob in the FX RACK view -> live param on the selected slot.

        Called by _dispatch_dial for any encoder turn while self.view == "fx"
        (chain-edit submode).  idx = encoder 0-3; step = +/-1.

        Encoder-to-control mapping (mirrors the pad grid rows 0-2):
            Bare  layer  -> controls[ idx ]       (0-3)
            SHIFT layer  -> controls[ idx + 4 ]   (4-7)
            ALT   layer  -> controls[ idx + 8 ]   (8-11)
        where controls[] is the selected plugin's parameter list from the rack
        palette.  Each click moves 1% of the control's range (~100 clicks full
        sweep).  The value is owned by the rack (rack.param remembers it), so the
        pad grid + OLED read it straight back from rack.status()."""
        rack = self._fx_rack()
        if rack is None:
            return
        slots = rack.status().get("slots", [])
        if self._fx_sel_slot >= len(slots):
            return
        s = slots[self._fx_sel_slot]
        if not s.get("plugin"):
            return   # empty slot — nothing to edit
        # NB: we allow editing even when the slot is stopped.  rack.param writes
        # the live jalv only if it's running, and always remembers the value, so
        # a slot tweaked while off comes up already dialled-in when enabled
        # (this also matches the Mixtrack SHIFT+strip behaviour).
        ctrls  = s.get("controls", [])
        layer  = self._layer_now()   # "base" | "shift" | "alt"
        offset = {"base": 0, "shift": 4, "alt": 8}[layer]
        ci     = offset + idx
        if ci >= len(ctrls):
            # Empty page -- feedback so it doesn't feel like a broken knob.
            _pg = {"base": 1, "shift": 2, "alt": 3}[layer]
            if fire_oled:
                try:
                    _nm = rack.label_for(s.get("plugin")) or "(empty)"
                    fire_oled.show_lines([
                        ("FX " + str(self._fx_sel_slot + 1) + ": " + _nm, 2, 0, 2),
                        ("P" + str(_pg) + ": no params on this page", 2, 28, 1),
                        ("base=P1  SHIFT=P2  ALT=P3", 2, 44, 1)])
                except Exception:
                    pass
            return

        c      = ctrls[ci]
        symbol = c["symbol"]
        p_min  = float(c.get("min", 0.0))
        p_max  = float(c.get("max", 1.0))
        label  = c.get("label", symbol)
        cur    = float(c.get("value", c.get("default", (p_min + p_max) / 2.0)))

        increment = step * (p_max - p_min) / 100.0
        new_val   = max(p_min, min(p_max, cur + increment))

        # Hand it to the rack: one atomic FIFO write to the live jalv, and the
        # rack remembers it (debounced persist) so status() reflects it at once.
        rack.param(self._fx_sel_slot, symbol, new_val)

        _pg  = {"base": 1, "shift": 2, "alt": 3}[layer]
        _tot = max(1, (len(ctrls) + 3) // 4)
        self._fx_last_param = (label + " P" + str(_pg) + "/" + str(_tot),
                               new_val, p_min, p_max)
        self._render_fx()    # refresh the brightness bars in rows 0-2
        self._oled()         # label + value + bar

    def _dispatch_select(self, step):
        # Step note editing: pad held in channel view -> dial in that step's pitch
        if self._held_seq_pad is not None and self.view == "seq":
            self._edit_step_note(*self._held_seq_pad, step)
            return
        # Browse overlay: encoder adjusts selected parameter.
        if self._overlay == "browse" and self.view in ("seq", "mixer"):
            self._browse_adjust(step)
            return
        # CC OUT editor: everything is on the four knobs, so SELECT-turn is
        # consumed rather than falling through to the arp/track scroll below.
        if self._overlay == "kmap_cc":
            return
        # Kmap overlay: encoder scrolls the target menu.
        if self._overlay == "kmap":
            targets = self._kmap_targets()
            if targets:
                self._kmap_sel = (self._kmap_sel + step) % len(targets)
                self._oled()
            return
        # In assign modes, SELECT turn cycles the type: note → cc → pc → note
        _PAD_TYPES = ["note", "cc", "pc"]
        if self._universal_assign_mode:
            e = self._universal_edit
            cur = e.get("type", "note")
            e["type"] = _PAD_TYPES[(_PAD_TYPES.index(cur) + 1) % len(_PAD_TYPES)] \
                        if cur in _PAD_TYPES else "note"
            self._oled_universal_assign()
            return
        if self._mpc_assign_mode:
            e = self._mpc_edit
            cur = e.get("type", "cc")
            e["type"] = _PAD_TYPES[(_PAD_TYPES.index(cur) + 1) % len(_PAD_TYPES)] \
                        if cur in _PAD_TYPES else "cc"
            self._oled_mpc_assign()
            return
        # Preset picker: SELECT turn scrolls slot.
        if self._overlay == "preset_picker":
            self._preset_slot = (self._preset_slot + step) % 8
            self._oled_preset_picker()
            return
        # BROWSER context menu: SELECT turn scrolls the highlighted item.
        # PATTERN UP/DOWN also navigate as backup (unchanged).
        if self._menu_open:
            items = _MENU_ITEMS.get(self.view, [])
            if items:
                self._menu_idx = (self._menu_idx + step) % len(items)
                self._oled_menu()
            return
        # fx_browse overlay: SELECT turn scrolls plugins; SHIFT+turn = bank.
        if self._overlay == "fx_browse":
            if self._shift:
                self._fx_browse_bank(step)
            else:
                self._fx_browse_scroll(step)
            return
        # chord_rows overlay: SELECT turn adjusts octave for selected row.
        if self._overlay == "chord_rows":
            self._chord_rows_select_turn(step)
            return
        # chord_params overlay: SELECT turn cycles chord type.
        if self._overlay == "chord_params":
            self._chord_type_adjust(step)
            return
        # SHIFT+turn in channel/mixer = move selected track up/down in the list.
        if self._shift and self.view in ("seq", "mixer"):
            self._reorder_track(step)
            return
        if self.view == "faders":
            self._mpc_view_top = max(0, min(60, self._mpc_view_top + step))
            self._render_faders()
            self._oled()
            return
        # chord view (no overlay/menu): SELECT scrolls the tall row-stack
        # viewport; SHIFT+SELECT keeps the arp-pattern scroll it used to own.
        if self.view == "chord" and not self._alt:
            if self._shift:
                self._sel_scroll_pattern(step)
            else:
                self._chord_scroll_viewport(step)
            return
        sel = self._select_alt if self._alt else self._select
        m = getattr(self, "_sel_" + sel[0], None)
        if m:
            try:
                m(step, *sel[1:])
            except Exception as e:
                print(f"  ! Fire select '{sel[0]}': {e}", flush=True)
        else:
            _unresolved("_sel_", sel[0])

    # ==========================================================================
    # BUTTON ACTIONS  (_act_<name>(pressed, *args))
    # ==========================================================================

    def _set_view(self, view):
        if view in VIEWS:
            self.view = view
            # 2026-08-04 adversarial review: every named overlay (browse, kmap,
            # kmap_cc, track_edit, chord_rows, fx_browse, preset_picker) used to
            # survive a view switch except track_edit -- and several of them
            # (preset_picker, kmap, kmap_cc, chord_rows, fx_browse) have no view
            # guard in _oled() at all, so they'd keep painting in the new view
            # while _pad_press (once guarded, see below) or the per-view pad
            # handler treated the grid as belonging to the new view underneath.
            # Changing view is an unambiguous "I'm done with that overlay" the
            # same way it already was for track_edit -- so all of them drop now.
            self._overlay = None
            # Same reasoning for the BROWSER context menu: nothing blocked a
            # VIEW button while it was open, so the menu could keep painting
            # over a view it no longer applies to while pads underneath were
            # live for the new view (menu items are looked up per-view via
            # _MENU_ITEMS.get(self.view, ...), so the menu itself would even
            # silently start showing the WRONG view's items).
            self._menu_open = False
            # A pinned KEY overlay must not follow you between views. It only
            # renders in chord/poly/chip, so leaving for seq would hide it while
            # the latch stayed set -- and it would silently reclaim the whole
            # grid (killing the keyboard) the moment you came back. Changing
            # view is an unambiguous "I'm done here", so drop the pin.
            self._key_overlay_latched = False
            self._render()
            self._oled()

    def _act_view(self, pressed, view):
        if pressed:
            self._set_view(view)

    def _act_toggle_view(self, pressed, a, b):
        """Toggle between two views (Fire reorg 2026-09-02: DRUM = seq <-> mixer).
        In `a` -> go to `b`; in `b` -> go to `a`; anywhere else -> go to `a`
        (the primary of the pair). Routes through _set_view so all the overlay /
        pin cleanup happens exactly as a plain view jump does."""
        if pressed:
            self._set_view(b if self.view == a else a)

    def _act_cycle_view(self, pressed):
        if pressed:
            self._set_view(VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)])

    def _act_shift(self, pressed):
        was_key_overlay = self._key_overlay_active()
        self._shift = pressed
        self._update_subscreen()
        # SHIFT blocks the KEY overlay (_key_overlay_active tests `not shift`).
        # Before the overlay could be PINNED this gap was unreachable: the
        # overlay only existed while ALT was physically held, so SHIFT+ALT
        # always landed in _update_subscreen's own repaint path. With a latched
        # overlay and ALT released, pressing SHIFT alone deactivates the overlay
        # with NO repaint -- the grid keeps showing root/scale pads while
        # presses are routed to the underlying keyboard. The screen would be
        # lying about what a press does. Mirrors _act_alt's edge repaint.
        if was_key_overlay != self._key_overlay_active():
            self._render()
            self._oled()

    def _act_alt(self, pressed):
        was_key_overlay = self._key_overlay_active()
        self._alt = pressed
        self._update_subscreen()
        # Paint the KEY overlay in / out on the ALT edge. _update_subscreen only
        # repaints for its own transitions, and the grid otherwise sits until
        # the next event -- without this the overlay wouldn't appear until you
        # touched something else.
        if was_key_overlay != self._key_overlay_active():
            self._render()
            self._oled()

    def _act_seq_play(self, pressed):
        if not pressed:
            return
        if self.seq.running:
            self.seq.stop()
        else:
            self.seq.play()
            if self.seq.clock_out:
                pass   # Sequencer sends "start" internally
        self._render()
        self._oled()

    def _act_seq_stop(self, pressed):
        if pressed:
            if self._alt:
                self._toggle_pretty_lights()
                return
            if self._shift:
                # SHIFT+STOP = panic. Previously a no-op distinction -- plain
                # STOP and SHIFT+STOP behaved identically (this branch just
                # fell through to plain stop+reset either way), so nothing
                # relied on shift making a difference here. Repurposed as
                # the Fire's panic button.
                self._panic()
                return
            if self._pretty_lights:
                self._pl_next_mode()
                return
            self.seq.stop(reset=True)
            self._render()
            self._oled()

    def _panic(self):
        """All-notes-off safety net, reachable via SHIFT+STOP.
        Covers everything this engine plays through itself -- chord engine
        (via all_notes_off()) plus internal poly synth and every NES/GB
        chip voice (via an explicit CC123 on each of their channels, since
        all_notes_off() only walks the chord engine's own preset-routed
        channels and the direct Fire->synth chip/poly path isn't one of
        those). Deliberately leaves DIN passthrough / external gear alone,
        same as all_notes_off() itself. Also clears any stuck SHIFT/ALT
        flag as a bonus safety net -- see the track-length-reset bugfix for
        why a stuck modifier is exactly the kind of stuck state a panic
        button should clean up."""
        self.eng.all_notes_off()
        if mido:
            # Every tonal module's LIVE channel (unbound ones are skipped --
            # their old cfg channel may now be another module's).
            panic_channels = sorted({
                c for c in (self._module_ch(m) for m in (
                    "mono", "poly", "chip_p1", "chip_p2", "chip_tri",
                    "chip_noise", "chip_noise2", "gb_wave", "chip_pcm"))
                if c})
            for ch in panic_channels:
                try:
                    self.eng.send_synth_midi(mido.Message(
                        "control_change", channel=int(ch) - 1,
                        control=123, value=0))
                except Exception:
                    pass
        # Also halt every internal source that can keep going on its own --
        # the sequencer transport, the arp, and any latched sampler loop --
        # so SHIFT+STOP stops what the Fire itself didn't start (2026-07-19).
        try:
            self.eng.master_stop()
        except Exception as e:
            print(f"  ! Fire master_stop (panic): {e}", flush=True)
        self._shift = False
        self._alt = False
        if fire_oled:
            try:
                fire_oled.show_lines([("PANIC", 2, 0, 2),
                                      ("STOP ALL", 2, 30, 1)])
            except Exception:
                pass

    def _act_seq_clear(self, pressed):
        if not pressed:
            return
        if self._alt:
            # ALT+REC = velocity full-lock toggle. When on, every note-on
            # this engine sends out (chord engine + internal poly/chip
            # synth) goes out at velocity 127 no matter the input velocity --
            # see vel_lock in orc1.py's state dict + note_on_out()/
            # send_synth_midi() for where it's actually enforced. Does not
            # touch DIN passthrough (own playing/sequencing only).
            new_state = not self.eng.state.get("vel_lock", False)
            self.eng.state["vel_lock"] = new_state
            if fire_oled:
                try:
                    fire_oled.show_lines([("VEL LOCK", 2, 0, 2),
                                          ("ON" if new_state else "OFF", 2, 30, 1)])
                except Exception:
                    pass
            return
        # SHIFT+REC appends a new melodic track; plain REC clears visible rows.
        if self._shift:
            self._add_track()
            return
        if self.view in ("seq", "mixer"):
            self._clear_visible_tracks()   # already renders internally

    def _add_track(self):
        """Append a new, empty melodic track (default: internal poly on CH_POLY)
        just after the selected one, then select it. Capped at MAX_TRACKS.
        2026-07-01 ownership refactor: the dict-building + locked insert now
        live in Sequencer.add_track(); this method just supplies Fire's own
        conventions (poly channel, palette color) and updates Fire's own
        selection/scroll/overlay UI state."""
        if self.n_tracks >= MAX_TRACKS:
            return
        ch_poly = int(self.cfg.SYNTH.get("CH_POLY", 8))
        new_color = self._track_palette[self.n_tracks % len(self._track_palette)]
        insert_at = self.seq.add_track(
            after_idx=self._sel_track, color=new_color,
            name=f"note{self.n_tracks+1}", note=48, ch=ch_poly,
            vel=100, dest="synth", length=16, mode="note",
        )
        if insert_at is None:
            return
        self._sel_track = insert_at
        # Keep the freshly-added track on screen.
        if self._sel_track >= self.view_top + N_ROWS:
            self.view_top = self._sel_track - N_ROWS + 1
        # 2026-08-04 adversarial review: SHIFT+REC (the only caller) is
        # reachable from every view, but track_edit's OLED render is gated to
        # seq/mixer -- so calling this from e.g. chord view used to set
        # _overlay="track_edit" with nothing on screen to show for it, while
        # the pad-blocking guard added above now (correctly) kills every pad
        # until that invisible overlay clears some other way. Since the whole
        # point of track_edit is to configure the track you just added, jump
        # to seq view so the overlay it sets is actually the one on screen.
        if self.view not in ("seq", "mixer"):
            self.view = "seq"
        # Auto-enter track-edit so knobs immediately configure the new track.
        self._overlay = "track_edit"
        self._render()
        self._oled()

    def _act_scroll_tracks(self, pressed, delta):
        """PATTERN UP/DOWN -- Session E (2026-06-30) view-aware dispatch.
        See _act_page's docstring for the SEQ/MIXER swap: this button scrolls
        tracks in SEQ and is silent in MIXER (GRID <> does the reverse)."""
        if not pressed:
            return
        # Browse overlay: unchanged, navigates the param list.
        if self._overlay == "browse" and self.view in ("seq", "mixer"):
            if self.n_tracks:
                t = self.tracks[self._sel_track]
                n = len(self._browse_params_for(t))
                self._browse_idx = (self._browse_idx + delta) % n
                self._oled_browse()
            return
        # New BROWSER context menu: navigate items.
        if self._menu_open:
            items = _MENU_ITEMS.get(self.view, [])
            if items:
                self._menu_idx = (self._menu_idx + delta) % len(items)
                self._oled_menu()
            return
        # Preset picker: PAT UP/DOWN also scrolls slot.
        if self._overlay == "preset_picker":
            self._preset_slot = (self._preset_slot + delta) % 8
            self._oled_preset_picker()
            return
        if self.view == "seq":
            self._scroll_tracks(delta)
        elif self.view == "poly":
            # PATTERN ↑↓ = octave shift in poly keyboard view.
            # Config has UP=-1, DOWN=+1; negate so UP = higher pitch.
            self.poly_base_note = max(0, min(108, self.poly_base_note - delta * 12))
            self._render_poly()
            self._oled()
        elif self.view == "chip":
            self.chip_base_note = max(0, min(108, self.chip_base_note - delta * 12))
            self._render_chip()
            self._oled()
        elif self.view == "chord":
            if self._shift:
                # SHIFT+PAT ↑↓ = cycle scale/mode (also on BROWSER->Cycle Scale).
                self._chord_cycle_scale(-delta)
            else:
                # Plain PAT ↑↓ = octave shift -- matches poly/chip views and the
                # Fire's normal PAT-up/down convention (owner, 2026-08-12).
                # Reverts the 2026-07-28 swap that put scale-cycle on plain PAT:
                # that optimised for "scale is the frequent action" but made
                # chord view the ONE view where plain PAT wasn't octave, which
                # broke cross-view muscle memory and read as "screwy" control.
                # Cross-view consistency wins; scale moves to SHIFT+PAT (and the
                # BROWSER menu), octave stays reachable via the OCT± pads too.
                # Ceiling is 96 (C8) to MATCH the OCT± pad clamp -- they were
                # 108 vs 96 before, so the two octave controls disagreed on the
                # top of the range; above ~80 the top rows saturate at 127 (send
                # clamp) anyway, so 96 loses no usable range and keeps them equal.
                self.perform_base_note = max(0, min(96, self.perform_base_note - delta * 12))
                self._render_perform()
                self._oled()
        # mixer / faders / universal: consume silently (Session E
        # redistribution -- GRID <> now owns track-scroll in MIXER).

    def _act_page(self, pressed, delta):
        """GRID <> -- Session E (2026-06-30) view-aware dispatch. Note the
        SEQ/MIXER swap versus the old behavior: GRID now pages steps in SEQ
        and scrolls tracks in MIXER; PATTERN UP/DOWN (_act_scroll_tracks)
        does the opposite split -- scrolls tracks in SEQ, silent in MIXER.
        That's the plan's spec, not a typo."""
        if not pressed:
            return
        if self._menu_open or self._subscreen or self._subscreen_latched:
            return   # consume silently while a menu/subscreen is up
        if self.view == "seq":
            self.page = max(0, min((MAX_LEN // N_COLS) - 1, self.page + delta))
            self._render()
            self._oled()
        elif self.view == "mixer":
            self._scroll_tracks(delta)
        elif self.view == "chord":
            self._cycle_chord_bank(delta)
        elif self.view == "chip":
            self.chip_base_note = max(0, min(108, self.chip_base_note + delta * 12))
            self._render_chip()
            self._oled()
        elif self.view == "poly":
            self.poly_base_note = max(0, min(108, self.poly_base_note + delta * 12))
            self._render_poly()
            self._oled()
        elif self.view == "faders":
            self._mpc_view_top = max(0, min(60, self._mpc_view_top + delta * 4))
            self._render_faders()
            self._oled()
        elif self.view == "universal":
            # Pad banks. Deferred at the original build ("Step 16, not part of
            # this pass") and left silent for long enough that the pages were
            # remembered as a feature that existed; built 2026-07-31.
            self._universal_set_bank(delta)
        elif self.view == "fx":
            if self._alt:
                self._fx_assign_slot(delta)
            else:
                self._cycle_fx_chain(delta)

    def _act_set_base(self, pressed, base):
        # NOTE: not currently dispatched by any action tuple (the live base pads
        # go through the engine's handle_ctrl_note). Kept wired to the engine
        # setter anyway so it can't reintroduce the "quality pad does nothing"
        # bug if it's ever mapped -- see _set_chord_base.
        if pressed:
            self._set_chord_base(base)
            self._retrigger()
            self._oled()

    def _act_toggle_arp(self, pressed):
        if pressed and self.eng.arp:
            self.eng.arp.set_enabled(not self.eng.arp.enabled)
            if not self.eng.arp.enabled:
                self.eng.send_chord_off()
                self.eng.all_notes_off()
            self._oled()

    def _act_note_btn(self, pressed):
        """NOTE button: bare = chip view, SHIFT = toggle chip coupling,
        ALT (in chip/poly/chord) = 4ths/chromatic layout toggle,
        ALT elsewhere = arp on/off."""
        if not pressed:
            return
        if self._alt:
            if self.view in ("chip", "poly", "chord"):
                # ALT+NOTE in melodic views: toggle 4ths / chromatic layout
                self._fourths_mode = not self._fourths_mode
                label = "4THS (bass)" if self._fourths_mode else "CHROMATIC"
                if fire_oled:
                    try:
                        fire_oled.show_lines([("LAYOUT", 2, 0, 2),
                                              (label, 2, 30, 1)])
                    except Exception:
                        pass
                self._render()
            else:
                # ALT+NOTE elsewhere: toggle arp
                if self.eng.arp:
                    self.eng.arp.set_enabled(not self.eng.arp.enabled)
                    if not self.eng.arp.enabled:
                        self.eng.send_chord_off()
                        self.eng.all_notes_off()
                    self._oled()
        elif self._shift:
            # SHIFT+NOTE: cycle coupling  none->poly->chip->both->none
            st  = self.eng.state
            p   = st.get("synth_mirror_poly", False)
            c   = st.get("synth_mirror_chip", False)
            tog = getattr(self.eng, "toggle_synth_mirror", None)
            if tog:
                if not p and not c:  # none -> poly
                    tog("poly")
                elif p and not c:    # poly -> chip
                    tog("poly"); tog("chip")
                elif not p and c:   # chip -> both
                    tog("poly")
                else:               # both -> none
                    tog("poly"); tog("chip")
            self._oled_mirror()
        else:
            # NOTE bare press: jump to chip from anywhere outside; cycle chip → chord → poly → chip within
            _melodic = ["chip", "chord", "poly"]
            if self.view in _melodic:
                _next = _melodic[(_melodic.index(self.view) + 1) % len(_melodic)]
            else:
                _next = "chip"
            self._set_view(_next)
            self._oled()

    def _oled_mirror(self):
        """Show synth coupling state on the OLED."""
        if not fire_oled:
            return
        try:
            st = self.eng.state
            poly = "ON" if st.get("synth_mirror_poly") else "OFF"
            chip = "ON" if st.get("synth_mirror_chip") else "OFF"
            fire_oled.show_lines([
                ("COUPLE", 2, 0, 2),
                (f"poly {poly}", 2, 26, 1),
                (f"chip {chip}", 2, 44, 1)])
        except Exception:
            pass

    def _act_tap_tempo(self, pressed):
        # Tap tempo is now SHIFT+PAT_SONG.
        # Plain PAT_SONG toggles the FX view (handled by _act_fx_view).
        if not pressed:
            return
        if self._alt:
            self.toggle_clock_out()
        elif self._shift:
            self.eng.tap_tempo()
            self._oled()

    def _act_fx_view(self, pressed):
        """PAT_SONG button.
        Tap = toggle FX view entry/exit.
        SHIFT+PAT_SONG = tap tempo (acts on press, not release).
        ALT+PAT_SONG   = clock out toggle (acts on press, not release)."""
        import time as _t
        if pressed:
            if self._shift:
                self.eng.tap_tempo(); self._oled(); return
            if self._alt:
                self.toggle_clock_out(); return
            self._pat_song_down_t = _t.monotonic()
            return
        # ---- release ----
        if self._pat_song_down_t is None:
            return   # released after a modifier combo -- nothing to do
        self._pat_song_down_t = None
        # Toggle view entry/exit.
        # NOTE: this assigns self.view DIRECTLY rather than going through
        # _set_view(), so it must repeat _set_view's pin cleanup by hand -- a
        # pinned KEY overlay owns every pad, and without this a quick trip to
        # the FX view and back silently reclaimed the grid and killed the
        # keyboard again.
        # 2026-08-04 adversarial review: was only clearing the KEY-overlay
        # pin -- a leftover _overlay/_menu_open (BROWSER menu, kmap, etc.)
        # survived this transition too, and since _pad_press now blocks
        # every pad while either is set (see the fix above), a stuck value
        # here silently killed the grid in the NEW view with a completely
        # normal-looking screen. Clear both, same as _set_view() does.
        self._key_overlay_latched = False
        self._overlay   = None
        self._menu_open = False
        if self.view == "fx":
            self.view = getattr(self, "_pre_fx_view", "seq")
            self._fx_soft_layer = None   # clear any pad-selected layer
            self._render()
        else:
            self._pre_fx_view   = self.view
            self.view           = "fx"
            self._fx_last_param = None
            self._render()
        self._oled()

    def _act_noop(self, pressed):
        pass

    # ==========================================================================
    # FX CHAIN HELPERS  (MechaJesus back-end bridge)
    # ==========================================================================
    # The three private helper methods that used to live here
    # (_fx_load_chains, _fx_load_state, _fx_chain_running) have been moved
    # into FXManager (orc1_fx.py) and are now accessed via eng.fx.
    # That makes them available to the web UI and orc1.py command handler too,
    # not just the Fire.  All call sites below use self.eng.fx.* instead.

    def _fx_toggle_chain(self, slot_idx):
        """Start or stop the chain at slot_idx via the FXManager.

        Fire-specific concerns handled here:
          - Resolve slot index -> chain name (Fire owns the slot list)
          - Show immediate OLED feedback before jalv has started
          - Pass _fx_after_toggle as the done callback so pads and param state
            update correctly once jalv is actually running (not on a fixed timer)

        The actual jalv lifecycle (spawning, waiting, JACK wiring) is handled
        by eng.fx.toggle_chain() on a background thread.  This method returns
        immediately and the MIDI thread is never blocked.
        """
        fx = getattr(self.eng, "fx", None)
        if not fx:
            return

        # Refresh slot assignments in case they've changed since the last render
        names = self._fx_chain_names or self._fx_slot_assignments
        if slot_idx >= len(names):
            return

        name = names[slot_idx]
        if not name:
            return  # unassigned slot -- nothing to toggle
        running = fx.chain_running(name)
        verb    = "stopping..." if running else "starting..."

        # Immediate OLED feedback -- jalv takes a few seconds and the user
        # needs to know their button press was registered
        if fire_oled:
            try:
                fire_oled.show_lines([
                    (f"FX {slot_idx + 1}: {name}", 2, 0, 2),
                    (verb, 2, 28, 1)])
            except Exception:
                pass

        # Delegate to FXManager -- runs on a background thread, calls
        # _fx_after_toggle when done so pad colours and param state update correctly
        fx.toggle_chain(name, done=lambda: self._fx_after_toggle(name))

    def _fx_after_toggle(self, chain_name):
        """Called (from the FXManager background thread) once a chain finishes
        starting or stopping.

        On start: jalv has freshly loaded the plugin with default parameter values,
        so any values we were tracking before are now stale.  We clear them so the
        next encoder touch seeds from the plugin's actual defaults rather than
        whatever we had last session.

        On stop: same -- clear so the grid goes dark cleanly on next render.
        """
        # Drop tracked param values for this chain.  Next encoder touch will
        # re-seed from the "default" field in fx_chains.json.
        self._fx_param_vals.pop(chain_name, None)
        self._fx_last_param = None
        if self.view == "fx":
            self._render_fx()
            self._oled()

    def _fx_delayed_render(self):
        """Repaint the FX view after jalv has had time to start or stop.
        Silently ignored if the user has already navigated to another view.
        (Kept for backward compatibility; _fx_after_toggle is now the primary callback.)"""
        if self.view == "fx":
            self._render_fx()
            self._oled()

    # -- MIDI clock output -----------------------------------------------------

    def toggle_clock_out(self):
        """Toggle MIDI clock output on/off (ALT+PAT_SONG or web TOGGLE_CLOCK_OUT)."""
        self.seq.toggle_clock_out()
        self._oled_clock_out()

    def _oled_clock_out(self):
        """Brief OLED flash showing clock-out state."""
        if not fire_oled:
            return
        try:
            state = "ON " if self.seq.clock_out else "OFF"
            ports = " ".join(self.seq._clock_ports)[:14]
            fire_oled.show_lines([
                ("MIDI CLK OUT", 2, 0, 2),
                (f"{state}  {ports}", 2, 30, 1)])
        except Exception:
            pass


    def _act_browser(self, pressed):
        """BROWSER -- universal escape hatch, then opens this mode's context
        menu. Session E (2026-06-30): removed the old ALT->kmap, SHIFT->chip
        -view, and bare-else->poly-view shortcuts entirely, per the plan's
        rule ("BROWSER... Never shortcuts to any specific view"). The seq/
        mixer param-browse overlay is untouched -- it's still the escape-hatch
        priority right below subscreen/menu.

        Priority order, each one always closes/consumed before the next
        gesture opens something new:
          1. subscreen (momentary or latched, e.g. mid encoder-remap)
          2. BROWSER context menu
          3. legacy browse/track_edit/kmap overlay
          4. (nothing open) -> open this mode's context menu

        Also keeps the stuck-SHIFT/ALT safety net added during the bugfix
        pass earlier today: a dropped note-off (USB hiccup, hot-plug rescan,
        Fire firmware glitch) can latch either modifier True forever, turning
        every future plain pad tap into a SHIFT+pad or ALT+pad gesture (the
        mechanism behind the track-length-reset bug). BROWSER clears both
        flags at the end of every press, regardless of which branch fired
        above -- same bound-the-damage-window guarantee as before, just
        re-homed onto the new dispatch shape."""
        if not pressed:
            return
        # Cancel either assign mode before anything else -- they act like
        # lightweight overlays that BROWSER should always escape from.
        if self._mpc_assign_mode:
            self._mpc_assign_cancel()
            self._alt   = False
            self._shift = False
            return
        if self._universal_assign_mode:
            self._universal_assign_cancel()
            self._alt   = False
            self._shift = False
            return
        # O4: a PINNED key overlay is an overlay, so BROWSER escapes it like
        # everything else here. Without this the only way out would be the PIN
        # pad itself, and a pin left on covers the keyboard.
        if self._key_overlay_latched:
            self._key_overlay_latched = False
            self._alt   = False
            self._shift = False
            self._render()
            self._oled()
            return
        if self._subscreen or self._subscreen_latched:
            self._subscreen         = False
            self._subscreen_latched = False
            if self._overlay in ("kmap", "kmap_cc"):
                self._overlay = None
            self._render()
            self._oled()
        elif self._menu_open:
            self._menu_open = False
            self._render()
            self._oled()
        elif self._overlay is not None:
            self._overlay       = None
            self._preset_action = None
            self._oled()
        else:
            # Nothing active -- open this mode's context menu.
            self._menu_open = True
            self._menu_idx  = 0
            self._oled_menu()
        self._alt   = False
        self._shift = False

    # -- Session E (2026-06-30): BROWSER context menu -------------------------
    # Items marked "n/a" below have no _menuact_ method and fall through to a
    # generic "not wired" OLED flash -- see the individual stubs for which
    # later Session E step actually implements them. This is an honest scope
    # boundary, not a bug: owner approved implementing through Step 9 this
    # session; steps 10-18 build the features several of these items reference
    # (default voicing, waveform, MPC MIX, Universal pad assignment, the
    # consolidated state file).
    #
    # "Browse Track Params" preserves the pre-Session-E plain-BROWSER-in-seq/
    # mixer behavior (per-track voice/note/ch/vel scroll+adjust) that the
    # Session E plan's own menu spec never re-homed anywhere -- rather than
    # silently drop a working feature because the plan didn't mention it,
    # it's kept as an explicit menu item. Flagged in the as-built notes.

    def _menuact_browse_params(self, label):
        self._overlay    = "browse"
        self._browse_idx = 0

    def _menuact_save_pattern(self, label):
        try:
            self.seq.save()
            self._oled_flash("SAVED", "pattern -> disk")
        except Exception as e:
            self._oled_flash("SAVE FAILED", str(e)[:22])

    def _menuact_clear_all_steps(self, label):
        self._clear_visible_tracks()
        self._oled_flash("CLEARED", "visible tracks")

    def _menuact_add_track(self, label):
        self._add_track()

    def _menuact_remap_encoders(self, label):
        """New entry point for encoder remapping (Step 9). Reuses the
        existing, already-working kmap state machine (_enter_kmap /
        _kmap_assign / the dial-touch handler in _note_on) -- only the entry
        and exit points changed, not the proven assignment logic itself.
        BROWSER exits (via _act_browser's `_overlay is not None` branch).

        2026-07-31: this used to also set _subscreen/_subscreen_latched and
        call _render_subscreen(), which painted the SHIFT+ALT MODE-JUMP row
        (bottom row, cols 8-15) over the grid -- eight live mode-jump pads
        sitting in the middle of an unrelated screen, reported from the field
        as "an odd remnant from another place".  It also made every other pad
        dead, because _pad_press hands the whole grid to _subscreen_pad_press
        while a subscreen is up.  Nothing needed it: _oled() has always
        special-cased `_overlay == "kmap"` ahead of the subscreen branch, so
        only the PAD half was ever wrong, and _act_browser exits an overlay
        with or without the latch.  The grid now shows the view you are
        actually mapping knobs for.
        """
        self._enter_kmap()
        self._render()

    def _menuact_configure_rows(self, label):
        """Open the chord-row routing overlay.
        Knob 0 = select row, knob 1 = type, knob 2 = port, knob 3 = channel.
        SELECT encoder = octave offset.  BROWSER exits and saves."""
        self._overlay = "chord_rows"
        self._oled()

    def _oled_chord_rows(self):
        """OLED display for the chord-row config overlay.

        Shows a 4-row WINDOW of the tall stack that keeps the selected row
        visible; selected row is marked with '>'. Knob 0=row (scrolls the whole
        stack)  Knob 1=type  Knob 2=port  Knob 3=channel  SELECT=octave
        """
        if not fire_oled:
            return
        # Four rows of small text need a 15px pitch to clear each other's
        # descenders (measured on the real renderer: a scale-1 line occupies
        # roughly y+2..y+15). 4 x 15 = 60 of the panel's 64, which leaves no
        # room for a header -- so the header goes and the rows start at y=0.
        # The previous layout drew row 3 at y=52 and clipped it off the bottom
        # entirely; an intermediate 11px pitch fixed the clipping but made
        # every row overlap the one above it. Rows are self-identifying (R0-R3
        # with a > cursor) and this overlay is only reachable from a menu item
        # that already names it, so the header is the right thing to lose.
        n = len(self._chord_rows)
        # 4-row window that keeps the selected row on screen.
        win = 0 if n <= N_ROWS else max(0, min(self._chord_row_sel - 1, n - N_ROWS))
        lines = []
        for vi in range(min(N_ROWS, n)):
            i     = win + vi
            rcfg  = self._chord_rows[i]
            rtype = rcfg.get("type", "chord")
            sel   = ">" if i == self._chord_row_sel else " "
            if rtype == "chord":
                desc = "chord engine"
            elif rtype == "off":
                desc = "-- off --"
            else:
                pk   = rcfg.get("port") or "pisound"
                ch   = rcfg.get("channel") or 1
                oc   = int(rcfg.get("octave") or 0)
                psh  = _ROW_PORT_SHORT.get(pk, pk[:4])
                oc_s = f"{oc:+d}" if oc != 0 else ""
                desc = f"{psh} {self._ch_label(ch)}{(' '+oc_s+'o') if oc_s else ''}"
            lines.append((fire_oled.fit(f"{sel}R{i} {desc}", 2, 1, why="row routing desc"),
                          2, vi * 15, 1))
        fire_oled.show_lines(lines)

    def _chord_rows_dial(self, idx, step):
        """Knob handler for the chord-row config overlay.

        Knob 0: scroll selected row (0 .. rows-per-bank-1)
        Knob 1: cycle row type (chord / direct / off)
        Knob 2: cycle port (direct rows only)
        Knob 3: channel (direct rows only): modules (synth port) then 1-16
        SELECT encoder (handled separately in _select_turn): octave offset
        """
        row  = self._chord_row_sel
        rcfg = self._chord_rows[row]
        if idx == 0:
            # Select row (CW = next row) across the WHOLE tall stack, not just 4.
            self._chord_row_sel = (row + step) % len(self._chord_rows)
        elif idx == 1:
            # Cycle type
            cur = rcfg.get("type", "chord")
            nxt = _ROW_TYPES[(_ROW_TYPES.index(cur) + step) % len(_ROW_TYPES)]
            rcfg["type"] = nxt
        elif idx == 2:
            # Cycle port (direct rows)
            if rcfg.get("type") == "direct":
                cur = rcfg.get("port") or "pisound"
                if cur not in _ROW_PORTS:
                    cur = _ROW_PORTS[0]
                nxt = _ROW_PORTS[(_ROW_PORTS.index(cur) + step) % len(_ROW_PORTS)]
                rcfg["port"] = nxt
                if isinstance(rcfg.get("channel"), str) and nxt != "synth":
                    rcfg["channel"] = 1       # modules exist only on the synth bus
        elif idx == 3:
            # Channel (direct rows): on port synth the MODULES come first, then
            # 1-16 -- a row that names a module follows it around /matrix.
            if rcfg.get("type") == "direct":
                opts = self._kmap_ch_options(rcfg.get("port") or "pisound")
                cur  = rcfg.get("channel") or 1
                i    = opts.index(cur) if cur in opts else opts.index(1)
                rcfg["channel"] = opts[max(0, min(len(opts) - 1, i + step))]
        self._save_chord_rows()
        self._oled_chord_rows()
        self._render_perform()

    def _chord_rows_select_turn(self, step):
        """SELECT encoder in chord_rows overlay adjusts octave offset of selected row."""
        row  = self._chord_row_sel
        rcfg = self._chord_rows[row]
        oc   = int(rcfg.get("octave") or 0)
        rcfg["octave"] = max(-4, min(4, oc + step))
        self._save_chord_rows()
        self._oled_chord_rows()
        self._render_perform()

    def _menuact_save_state(self, label):
        self._save_fire_state()
        self._oled_flash("SAVE STATE", "saved!")

    def _menuact_set_default_voicing(self, label):
        self._oled_flash("SET VOICING", "not wired yet (Step 10)")

    def _menuact_set_coupling_mode(self, label):
        # Already works today via SHIFT+MUTE1 in chip view -- point there
        # instead of duplicating a second entry point for the same feature.
        self._oled_flash("SET COUPLING", "use SHIFT+MUTE1 in chip view")

    def _menuact_set_detune_amount(self, label):
        self._oled_flash("SET DETUNE", "use SHIFT/ALT+MUTE2 in chip view")

    def _menuact_set_default_waveform(self, label):
        self._oled_flash("SET WAVEFORM", "not wired yet (Step 12/18)")

    def _menuact_mpc_clear_fader(self, label):
        """BROWSER menu: remove the assignment from the last-tapped fader."""
        fidx = self._mpc_last_fader
        if fidx is None:
            self._oled_flash("CLEAR FADER", "tap a fader row first")
            return
        self._mpc_faders.pop(fidx, None)
        self._save_fire_state()
        self._render_faders()
        self._oled_flash(f"FADER #{fidx}", "cleared")

    def _menuact_mpc_clear_all(self, label):
        """BROWSER menu: remove all fader assignments."""
        self._mpc_faders         = {}
        self._mpc_assign_mode    = False
        self._mpc_assign_target  = None
        self._save_fire_state()
        self._render_faders()
        self._oled_flash("MPC FADERS", "all cleared")

    def _menuact_universal_toggle_latch(self, label):
        """BROWSER menu: toggle the latch flag on the last-touched pad.
        Latched pads toggle on/off each press instead of being momentary.
        """
        key = self._last_universal_pad
        if key is None or key not in self._universal_pads:
            self._oled_flash("TOGGLE LATCH", "tap a pad first")
            return
        assign = self._universal_pads[key]
        assign["latch"] = not assign.get("latch", False)
        if not assign["latch"]:
            assign["_latched_on"] = False  # clear stuck-on state when disabling
        self._save_fire_state()
        state = "LATCH ON" if assign["latch"] else "LATCH OFF"
        r, c  = key
        self._oled_flash(f"pad {r},{c}", state)

    def _menuact_universal_clear_pad(self, label):
        """BROWSER menu: remove the assignment from the last-touched pad."""
        key = self._last_universal_pad
        if key is None:
            self._oled_flash("CLEAR PAD", "tap a pad first")
            return
        if key not in self._universal_pads:
            r, c = key
            self._oled_flash(f"pad {r},{c}", "already empty")
            return
        del self._universal_pads[key]
        self._universal_held.pop(key, None)
        self._save_fire_state()
        r, c = key
        self._render_universal()
        self._oled_flash(f"pad {r},{c}", "cleared")

    def _menuact_universal_clear_pads(self, label):
        """BROWSER menu: clear THIS BANK's pad assignments and exit assign mode.

        Scoped to the current bank once banks shipped (2026-07-31). One menu
        item that silently wipes every bank -- up to 9 x 64 assignments, most
        of them not even on screen -- is not a destructive action anybody
        would expect from a grid they can see 64 pads of. Other banks are
        untouched; page to them with GRID <> and clear each deliberately.
        """
        self._universal_release_held()
        self._universal_pads          = {}
        self._universal_held          = {}
        self._universal_assign_mode   = False
        self._universal_assign_target = None
        self._save_fire_state()
        self._render_universal()
        self._oled_flash("UNIVERSAL",
                         f"bank {self._universal_bank + 1} cleared")

    # -- Preset system (2026-07-03) -------------------------------------------
    # Three independent banks stored in ~/fire_presets.json:
    #   "ucs"    -- Universal Control Surface pad assignments
    #   "faders" -- MPC-mix fader assignments
    #   "knobs"  -- encoder dialmap (all views)
    # Each bank holds up to 8 numbered slots (0-7).
    # UI: BROWSER menu -> Save/Load * Preset -> preset_picker overlay.
    # -------------------------------------------------------------------------

    def _load_presets_file(self):
        """Read ~/fire_presets.json; return {} on any error."""
        import json, os
        try:
            with open(os.path.expanduser(_PRESETS_FILE)) as _f:
                return json.load(_f)
        except Exception:
            return {}

    def _save_presets_file(self, data):
        """Write ~/fire_presets.json atomically (best-effort)."""
        import json, os
        try:
            with open(os.path.expanduser(_PRESETS_FILE), "w") as _f:
                json.dump(data, _f, indent=2)
        except Exception as _e:
            print(f"  ! preset save: {_e}")

    def _open_preset_picker(self, action):
        """Open the slot picker overlay for the given action string."""
        self._preset_action = action
        self._preset_slot   = 0
        self._overlay       = "preset_picker"
        self._oled_preset_picker()

    def _menuact_preset_save_ucs(self, label):
        self._open_preset_picker("save_ucs")

    def _menuact_preset_load_ucs(self, label):
        self._open_preset_picker("load_ucs")

    def _menuact_preset_save_faders(self, label):
        self._open_preset_picker("save_faders")

    def _menuact_preset_load_faders(self, label):
        self._open_preset_picker("load_faders")

    def _menuact_preset_save_knobs(self, label):
        self._open_preset_picker("save_knobs")

    def _menuact_preset_load_knobs(self, label):
        self._open_preset_picker("load_knobs")

    def _preset_confirm(self):
        """Confirm the current preset slot action and close the overlay."""
        act  = self._preset_action
        slot = str(self._preset_slot)
        self._overlay       = None
        self._preset_action = None
        presets = self._load_presets_file()

        if act == "save_ucs":
            pads_out = {
                f"{k[0]},{k[1]}": {ek: ev for ek, ev in v.items()
                                    if not ek.startswith("_")}
                for k, v in self._universal_pads.items()
            }
            presets.setdefault("ucs", {})[slot] = {"pads": pads_out}
            self._save_presets_file(presets)
            self._oled_flash(f"PAD preset {self._preset_slot}", "saved")

        elif act == "load_ucs":
            entry = presets.get("ucs", {}).get(slot)
            if entry:
                loaded = {}
                for ks, assign in entry.get("pads", {}).items():
                    r, c = map(int, ks.split(","))
                    loaded[(r, c)] = dict(assign)
                self._universal_pads = loaded
                self._universal_held = {}
                self._render_universal()
                self._save_fire_state()
                self._oled_flash(f"PAD preset {self._preset_slot}", "loaded")
            else:
                self._oled_flash(f"slot {self._preset_slot}", "(empty)")

        elif act == "save_faders":
            faders_out = {str(fi): dict(assign)
                          for fi, assign in self._mpc_faders.items()}
            presets.setdefault("faders", {})[slot] = {"faders": faders_out}
            self._save_presets_file(presets)
            self._oled_flash(f"FADER preset {self._preset_slot}", "saved")

        elif act == "load_faders":
            entry = presets.get("faders", {}).get(slot)
            if entry:
                self._mpc_faders = {int(k): dict(v)
                                    for k, v in entry.get("faders", {}).items()}
                self._render_faders()
                self._save_fire_state()
                self._oled_flash(f"FADER preset {self._preset_slot}", "loaded")
            else:
                self._oled_flash(f"slot {self._preset_slot}", "(empty)")

        elif act == "save_knobs":
            dm_out = {
                view: {layer: {str(i): list(a) for i, a in knobs.items()}
                       for layer, knobs in layers.items()}
                for view, layers in self._dialmap.items()
            }
            presets.setdefault("knobs", {})[slot] = {"dialmap": dm_out}
            self._save_presets_file(presets)
            self._oled_flash(f"KNOB preset {self._preset_slot}", "saved")

        elif act == "load_knobs":
            entry = presets.get("knobs", {}).get(slot)
            valid_actions = {tuple(a) for _, a in self._targets
                             if tuple(a) != _KMAP_CC_ACTION}
            if entry:
                for view, layers in (entry.get("dialmap") or {}).items():
                    if view not in self._dialmap:
                        continue
                    for layer, knobs in (layers or {}).items():
                        if layer not in ("base", "shift", "alt"):
                            continue
                        for k, action in (knobs or {}).items():
                            try:
                                idx = int(k)
                            except (ValueError, TypeError):
                                continue
                            if not (0 <= idx <= 3):
                                continue
                            # isinstance guard, exactly as _load_knobmap has.
                            # Without it a non-list value (a bare int in a
                            # hand-edited preset) raises TypeError inside the
                            # loop, aborting the load partway with no save, no
                            # flash and no on-screen sign anything went wrong.
                            act = (tuple(action)
                                   if isinstance(action, (list, tuple)) else None)
                            # Validate exactly as _load_knobmap does. This
                            # loader had NO validation at all, so a stale or
                            # hand-edited ~/fire_presets.json could install an
                            # action with no handler -- including the CC OUT
                            # menu sentinel, which has no _dial_cc_custom and
                            # would leave the knob permanently dead with only a
                            # deduped journal line to say why. The knobmap
                            # loader was hardened against exactly this; the
                            # preset loader reads a different file with the
                            # same threat model and was missed.
                            if ((act in valid_actions
                                 or _well_formed_send_action(act))
                                    and _target_allowed_in_view(act, view)):
                                self._dialmap[view][layer][idx] = act
                self._save_knobmap()
                self._oled_flash(f"KNOB preset {self._preset_slot}", "loaded")
            else:
                self._oled_flash(f"slot {self._preset_slot}", "(empty)")

    def _oled_preset_picker(self):
        """Slot-picker overlay OLED.
          Line 0 (large): action label + bracketed slot number.
          Line 1 (small): slot usage / content summary.
          Line 2 (small): control hint.
        """
        if not fire_oled:
            return
        act  = self._preset_action or ""
        slot = self._preset_slot
        _lbl = {
            "save_ucs":    "SAVE PAD",   "load_ucs":    "LOAD PAD",
            "save_faders": "SAVE FADRS", "load_faders": "LOAD FADRS",
            "save_knobs":  "SAVE KNOBS", "load_knobs":  "LOAD KNOBS",
        }
        heading = f"{_lbl.get(act, act.upper())}  [{slot}]"
        _bank = {
            "save_ucs": "ucs",  "load_ucs": "ucs",
            "save_faders": "faders", "load_faders": "faders",
            "save_knobs": "knobs",   "load_knobs": "knobs",
        }
        bank = _bank.get(act, "ucs")
        try:
            presets   = self._load_presets_file()
            bank_data = presets.get(bank, {})
            if act.startswith("save"):
                used = sorted(int(k) for k in bank_data if k.isdigit())
                if used:
                    over = " OVR" if str(slot) in bank_data else ""
                    info = "used:" + " ".join(str(u) for u in used) + over
                else:
                    info = "no saved slots yet"
            else:
                entry = bank_data.get(str(slot))
                if entry is None:
                    info = "(empty slot)"
                elif bank == "ucs":
                    n = len(entry.get("pads", {}))
                    info = f"{n} pad{'s' if n != 1 else ''} assigned"
                elif bank == "faders":
                    n = len(entry.get("faders", {}))
                    info = f"{n} fader{'s' if n != 1 else ''} assigned"
                else:
                    n = sum(len(kn) for lyr in entry.get("dialmap", {}).values()
                            for kn in lyr.values())
                    info = f"{n} knob remap{'s' if n != 1 else ''}"
        except Exception:
            info = ""
        fire_oled.show_lines([
            (heading, 2, 0,  2),
            (info,    2, 20, 1),
            ("SEL/PAT=slot", 2, 34, 1),
            ("SEL=ok  BROW=x", 2, 48, 1)])

    def _menu_confirm(self):
        """SELECT press while a BROWSER context menu is open."""
        items = _MENU_ITEMS.get(self.view, [])
        if items:
            label, key = items[self._menu_idx % len(items)]
            m = getattr(self, "_menuact_" + key, None)
            if m:
                m(label)
            else:
                _unresolved("_menuact_", key)
                self._oled_flash("MENU", f"{label}: n/a")
        self._menu_open = False
        self._render()
        self._oled()

    def _oled_menu(self):
        """BROWSER context menu OLED.
        SELECT encoder turn  = scroll items
        SELECT encoder press = confirm highlighted item
        BROWSER              = exit without acting
        PATTERN UP/DOWN      = also navigate (backup)"""
        if not fire_oled:
            return
        try:
            items = _MENU_ITEMS.get(self.view, [])
            if not items:
                fire_oled.show_lines([("MENU", 2, 0, 2), ("(nothing here yet)", 2, 30, 1)])
                return
            idx   = self._menu_idx % len(items)
            above = f"  {items[idx-1][0]}" if idx > 0 else ""
            cur   = f"> {items[idx][0]}"
            below = f"  {items[(idx+1) % len(items)][0]}" if len(items) > 1 else ""
            fire_oled.show_lines([
                (f"MENU  {self.view}", 2, 0, 1),
                (above, 2, 16, 1),
                (cur,   2, 32, 1),
                (below, 2, 48, 1)])
        except Exception:
            pass

    def _oled_flash(self, line1, line2):
        """Brief two-line OLED confirmation, same pattern used throughout
        the file (e.g. _oled_clock_out, SOLO/ALL TRACKS in _toggle_mute)."""
        if not fire_oled:
            return
        try:
            fire_oled.show_lines([(line1, 2, 0, 2), (line2[:22], 2, 30, 1)])
        except Exception:
            pass

    def _clear_visible_tracks(self):
        """Zero steps+velocities (and step-note overrides) on the 4 currently-
        visible tracks. Factored out of _act_seq_clear so the new 'Clear All
        Steps' menu item and the old plain-REC gesture share one
        implementation. 2026-07-01: routed through seq.clear_steps
        (lock-guarded), the same method the web UI's SEQ_CLEAR_STEPS already
        uses -- also clears step_notes now, matching that method's scope."""
        visible = [self.view_top + row for row in range(N_ROWS)
                   if self.view_top + row < self.n_tracks]
        if visible:
            self.seq.clear_steps(*visible)
        self._render()

    def _toggle_solo_selected_track(self):
        """SELECT press in MIXER (no menu open): solo/un-solo the selected
        track. New for Session E -- MIXER's SELECT press used to toggle the
        track-edit overlay (same as SEQ); the plan reassigns that specifically
        to solo/unsolo, track-edit stays SEQ-only. 2026-07-01: routed through
        seq.solo_track/unmute_all (lock-guarded, one atomic pass -- Bug B)."""
        if not self.n_tracks:
            return
        tidx = self._sel_track
        already_solo = all(t["mute"] == (i != tidx) for i, t in enumerate(self.tracks))
        if already_solo:
            self.seq.unmute_all()
        else:
            self.seq.solo_track(tidx)
        self._render()
        name = self.tracks[tidx].get("name", f"Tr {tidx+1}")
        self._oled_flash("UNSOLO" if already_solo else "SOLO", name)

    def _oled_chord_param_menu(self):
        """SELECT press in CHORD: enter interactive chord-type editor.
        SELECT turn cycles chord type; SELECT press exits; BROWSER exits."""
        self._overlay = "chord_params"
        self._chord_params_oled()

    def _chord_params_oled(self):
        """OLED for chord_params overlay: shows current type + neighbours."""
        if not fire_oled:
            return
        try:
            cur = self.eng.state.get("chord_base", "MAJ")
            idx = _CHORD_BASE_NAMES.index(cur) if cur in _CHORD_BASE_NAMES else 0
            prev_n = _CHORD_BASE_NAMES[(idx - 1) % len(_CHORD_BASE_NAMES)]
            next_n = _CHORD_BASE_NAMES[(idx + 1) % len(_CHORD_BASE_NAMES)]
            exts = "+".join(sorted(
                self.eng.state.get("chord_exts", set()))) or "none"
            fire_oled.show_lines([
                ("CHORD TYPE", 2, 0, 1),
                (f"{prev_n} <[{cur}]> {next_n}", 2, 20, 1),
                (fire_oled.fit(f"ext:{exts}", 2, 1, why="chord extension list"), 2, 34, 1),
                ("SEL=done", 2, 48, 1)])
        except Exception:
            pass

    def _chord_type_adjust(self, delta):
        """Cycle chord type by delta; retrigger chord and update OLED.

        Steps from the STORED base, not the effective one.  Since O3 the
        effective label can be a shape with no base pad (AUG, maj7, 6no5) or a
        roman numeral, none of which are in _CHORD_BASE_NAMES -- those all fell
        to index 0, so the first turn jumped to MIN instead of stepping.  The
        stored base is always one of the four and is what _chord_params_oled
        displays, so stepping from it also keeps the prev<cur>next readout
        consistent with what a turn actually does.
        """
        cur = self.eng.state.get("chord_base", "MAJ")
        idx = _CHORD_BASE_NAMES.index(cur) if cur in _CHORD_BASE_NAMES else 0
        self._set_chord_base(_CHORD_BASE_NAMES[(idx + delta) % len(_CHORD_BASE_NAMES)])
        self._retrigger()
        self._chord_params_oled()

    def _oled_voice_summary(self):
        """SELECT press in NES+GB / POLY (no menu open): read-only voice
        parameter summary (same scope note as _oled_chord_param_menu)."""
        if not fire_oled:
            return
        try:
            # Dial values are keyed by the MODULE the knob names (2026-09-24).
            if self.view == "chip":
                ch     = self._VOICE_MODULE.get(self.chip_voice, "chip_p1")
                header = f"CHIP: {self.chip_voice}"
            else:
                ch     = "poly"
                header = "POLY SYNTH"
            cutoff = self._cc_vals.get(("synth", ch, 74), 64)
            reso   = self._cc_vals.get(("synth", ch, 71), 64)
            fire_oled.show_lines([
                (header, 2, 0, 1),
                (f"cut {cutoff}  res {reso}", 2, 22, 1),
                ("(BROWSER = menu)", 2, 42, 1)])
        except Exception:
            pass

    # Fallback list used when the engine module isn't reachable via sys.modules.
    _SCALE_NAMES_FB = ["MAJ","MIN","HMIN","MMIN","DOR","PHR","LYD","MIX","LOC","CHROM"]

    def _eng_mod(self):
        """The engine module -- it runs as __main__, so it can't be imported
        by name.  Same lookup the scale cycling has always used."""
        import sys
        return sys.modules.get("__main__") or sys.modules.get("orc1")

    def _scale_order(self):
        """Presentation order of the scale list (musically grouped), falling
        back to the append-only SCALE_NAMES and then to a local literal."""
        _mod = self._eng_mod()
        return (getattr(_mod, "SCALE_ORDER", None)
                or getattr(_mod, "SCALE_NAMES", None)
                or self._SCALE_NAMES_FB)

    def _scale_groups(self):
        """[(label, [scale, ...]), ...] -- one entry per overlay scale row."""
        _mod = self._eng_mod()
        groups = getattr(_mod, "SCALE_GROUPS", None)
        if groups:
            return list(groups)
        # No grouping available: chunk the flat list into rows of 12.
        order = self._scale_order()
        return [("SCALE", order[i:i + 12]) for i in range(0, len(order), 12)]

    def _set_scale(self, name):
        """Route through the engine's set_scale() when present so every surface
        shares one code path (retrigger behaviour included)."""
        fn = getattr(self._eng_mod(), "set_scale", None)
        if callable(fn):
            try:
                fn(name)
                return
            except Exception as e:
                _swallowed("set_scale", e)
        self.eng.state["scale"] = name          # fallback
        self._retrigger()

    def _set_root(self, root_pc):
        fn = getattr(self._eng_mod(), "set_root", None)
        if callable(fn):
            try:
                fn(root_pc)
                return
            except Exception as e:
                _swallowed("set_root", e)
        self.eng.state["root"] = int(root_pc) % 12
        self._retrigger()

    def _snap(self, note):
        """Apply the instrument-wide scale lock to a note about to be played.

        No-op unless a scale mode is active (and the scale isn't CHROM).
        Callers must store the RETURNED pitch in their held-note dict so the
        matching note-off addresses the same pitch that was sounded.

        ⚠ RETURNS None IN "filter" MODE, meaning the note must not sound and
        must not be recorded as held. Every call site has to handle that -- the
        engine's scale_snap_note() could only return an int before O4.
        On any error this returns the note UNCHANGED rather than None, so a
        failure can never silence the keyboard."""
        fn = getattr(self._eng_mod(), "scale_snap_note", None)
        if not callable(fn):
            return note
        try:
            return fn(note)
        except Exception:
            return note

    def _set_chord_base(self, name):
        """Explicitly pick chord quality. Routes through the engine setter so
        the diatonic override flag is set -- a bare state["chord_base"] write
        is ignored by effective_base() and makes the control look dead."""
        fn = getattr(self._eng_mod(), "set_chord_base", None)
        if callable(fn):
            try:
                fn(name, retrigger=False)
                return
            except Exception as e:
                _swallowed("set_chord_base", e)
        self.eng.state["chord_base"] = name
        # Mirror set_chord_base()'s O4 rule: base_override is the MOMENTARY
        # DIAT override only, never set while FIXED is latched.
        if self.eng.state.get("quality_mode") != "FIXED":
            self.eng.state["base_override"] = True
        self.eng.state["inversion"]     = 0

    def _chord_root_name(self, rn):
        """Name of the pitch class the chord is actually built on.

        Goes through the engine's chord_root_now() so KEY LOCK's in-key pull is
        reflected -- otherwise the OLED would name a root the engine didn't use.
        """
        fn = getattr(self._eng_mod(), "chord_root_now", None)
        if callable(fn):
            try:
                return rn[fn() % 12]
            except Exception as e:
                _swallowed("chord_root_now", e)
        st = self.eng.state
        return rn[int(st.get("chord_root", st.get("root", 0))) % 12]

    def _degree_str(self):
        """Roman numeral of the chord's degree in the key, for the OLED.

        Returns "--" when the chord root is outside the key (in which case the
        engine has fallen through to the fixed-shape path, so there is genuinely
        no degree to show)."""
        _mod = self._eng_mod()
        fn   = getattr(_mod, "current_degree", None)
        if not callable(fn):
            return "   "
        try:
            d = fn()
        except Exception:
            return "   "
        if d is None:
            return "--"
        romans = getattr(_mod, "ROMAN_DEGREES", None) or [
            "I", "II", "III", "IV", "V", "VI", "VII", "VIII"]
        return romans[d] if d < len(romans) else str(d + 1)

    def _effective_base(self):
        """The chord quality that will sound: diatonic for the current root +
        scale, unless a base pad override is active.  Mirrors the engine's
        effective_base() so the Fire's lit pad can't disagree with the audio."""
        fn = getattr(self._eng_mod(), "effective_base", None)
        if callable(fn):
            try:
                return fn()
            except Exception as e:
                _swallowed("effective_base", e)
        return self.eng.state.get("chord_base", "MAJ")

    def _scale_pcs(self):
        """Pitch classes of the active scale, or None when unconstrained.
        Used to shade in-scale pads on the keyboard views."""
        fn = getattr(self._eng_mod(), "scale_pitch_classes", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:
            return None

    def _chord_cycle_scale(self, step):
        """Plain PAT ↑↓ in chord view (also BROWSER->Cycle Scale): step through
        the scale list and retrigger the held chord if one is sounding.
        step=+1 = next scale; step=-1 = previous. OLED flashes the new name.

        Walks the PRESENTATION order (SCALE_ORDER), not the append-only
        SCALE_NAMES order, so stepping lands on musically adjacent scales
        rather than in the order they happened to be added."""
        order = self._scale_order()
        cur   = self.eng.state.get("scale", "CHROM")
        idx   = order.index(cur) if cur in order else 0
        name  = order[(idx + step) % len(order)]
        self._set_scale(name)
        self._oled_flash("SCALE", name)

    def _menuact_cycle_scale(self, label):
        """Chord context menu → Cycle Scale: step forward one scale."""
        self._chord_cycle_scale(+1)

    # -- KEY overlay: hold ALT in chord/poly/chip ----------------------------

    def _key_overlay_active(self):
        """ALT held, on a keyboard view, with nothing else owning the grid.

        SHIFT+ALT is the mode-jump subscreen, so ALT-alone is the only claim
        made here; the subscreen checks still come first everywhere this is
        consulted, this just avoids fighting it.

        O4: the overlay can also be PINNED open (row 3 col 3), because it is the
        only home for the key tonic and having it vanish when you release ALT
        made it useless for actually setting up a tune. A pinned overlay behaves
        exactly like a held one; BROWSER or the PIN pad closes it. SHIFT still
        wins, so the mode-jump subscreen is reachable while pinned."""
        return ((self._alt or self._key_overlay_latched) and not self._shift
                and not self._subscreen and not self._subscreen_latched
                and not self._menu_open
                and self.view in ("chord", "poly", "chip"))

    def _render_key_overlay(self):
        """Grid = 12 root pads (bottom row) + the scale list (rows above) +
        KEY LOCK / SCALE SNAP toggles in the control zone."""
        st       = self.eng.state
        root_pc  = int(st.get("root", 0)) % 12
        cur      = st.get("scale", "CHROM")
        locked   = bool(st.get("key_lock"))
        snapped  = bool(st.get("scale_snap"))

        cells = [(pad_index(r, c), *OFF)
                 for r in range(N_ROWS) for c in range(N_COLS)]

        # Scale rows (0-2): one group per row, left-aligned from col 4.
        for row, (_label, names) in enumerate(self._scale_groups()[:_KEY_ROOT_ROW]):
            dim, bright = _KEY_GROUP_COLORS[row % len(_KEY_GROUP_COLORS)]
            for i, name in enumerate(names):
                col = _KEY_ROOT_COL0 + i
                if col >= N_COLS:
                    break          # group wider than the row; extras unreachable
                rgb = bright if name == cur else dim
                cells[pad_index(row, col)] = (pad_index(row, col), *rgb)

        # Root row (3): chromatic, col 4 = C .. col 15 = B.
        for pc in range(12):
            col = _KEY_ROOT_COL0 + pc
            if pc == root_pc:
                rgb = _KEY_ROOT_SEL
            elif pc in _KEY_WHITE_PCS:
                rgb = _KEY_ROOT_WHITE
            else:
                rgb = _KEY_ROOT_BLACK
            cells[pad_index(_KEY_ROOT_ROW, col)] = (pad_index(_KEY_ROOT_ROW, col), *rgb)

        # Toggles in the control zone: LOCK / scale MODE / QUALITY / PIN.
        cells[pad_index(_KEY_ROOT_ROW, 0)] = (
            pad_index(_KEY_ROOT_ROW, 0), *(_KEY_LOCK_ON if locked else _KEY_TOGGLE_OFF))
        cells[pad_index(_KEY_ROOT_ROW, _KEY_COL_MODE)] = (
            pad_index(_KEY_ROOT_ROW, _KEY_COL_MODE),
            *_KEY_MODE_COLORS.get(self._scale_mode_now(), _KEY_TOGGLE_OFF))
        cells[pad_index(_KEY_ROOT_ROW, _KEY_COL_QUAL)] = (
            pad_index(_KEY_ROOT_ROW, _KEY_COL_QUAL),
            *(_KEY_QUAL_FIXED if st.get("quality_mode") == "FIXED"
              else _KEY_QUAL_DIAT))
        cells[pad_index(_KEY_ROOT_ROW, _KEY_COL_PIN)] = (
            pad_index(_KEY_ROOT_ROW, _KEY_COL_PIN),
            *(_KEY_PIN_ON if self._key_overlay_latched else _KEY_PIN_OFF))

        self._send_pads(cells)

    def _scale_mode_now(self):
        """Active scale mode, resolved the same way the engine resolves it.

        Falls back to the legacy boolean so a Fire talking to a pre-O4 engine
        (or reading pre-O4 saved state) still shows something truthful."""
        st = self.eng.state
        m  = st.get("scale_mode")
        if m in _KEY_MODE_COLORS:
            return m
        return "nearest" if st.get("scale_snap") else "off"

    def _oled_key_overlay(self):
        if not fire_oled:
            return
        st   = self.eng.state
        rn   = getattr(self.eng, "ROOT_NAMES",
                       ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"])
        root = rn[int(st.get("root", 0)) % 12]
        flags = []
        if st.get("key_lock"):
            flags.append("LOCK")
        mode = self._scale_mode_now()
        if mode != "off":
            flags.append(mode.upper())
        flags.append(st.get("quality_mode", "DIAT"))
        try:
            # When PINNED the overlay owns every pad, so the keyboard is parked
            # and NO note will sound until it is closed. That has to be stated
            # outright -- a silent keyboard is indistinguishable from a broken
            # one, and this screen is the only place that can explain it.
            pinned = self._key_overlay_latched
            fire_oled.show_lines([
                (fire_oled.fit(f"{root} {st.get('scale', '?')}", 2, 2, why="scale name"), 2, 0, 2),
                (fire_oled.fit(" ".join(flags) if flags else "free", 2, 1,
                               why="key flag list"),
                 2, 20, 1),
                ("PINNED: keys off" if pinned else "LK MODE QUAL PIN",
                 2, 34, 1),
                ("BROWSER exits" if pinned else "ALT held", 2, 48, 1),
            ])
        except Exception:
            pass

    def _key_overlay_pad_press(self, row, col):
        """Root / scale / toggle presses while the KEY overlay is showing."""
        # Toggles (control zone, bottom row).
        if col < _KEY_ROOT_COL0:
            st = self.eng.state
            if row == _KEY_ROOT_ROW and col == 0:
                st["key_lock"] = not bool(st.get("key_lock"))
                self._oled_flash("KEY LOCK", "ON" if st["key_lock"] else "OFF")
            elif row == _KEY_ROOT_ROW and col == _KEY_COL_MODE:
                # O4: five-way cycle, not a toggle. Routed through the engine's
                # set_scale_mode() so the legacy scale_snap mirror is kept in
                # step -- writing state["scale_mode"] directly here would leave
                # the two disagreeing for every other reader.
                # Take the cycle order from the ENGINE so the Fire and the web
                # button can never disagree about what "next" means. The order
                # is deliberate (nearest first, filter last -- see SCALE_MODES).
                order = tuple(getattr(self._eng_mod(), "SCALE_MODES", None)
                              or ("off", "nearest", "up", "down", "filter"))
                cur   = self._scale_mode_now()
                nxt   = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
                fn    = getattr(self._eng_mod(), "set_scale_mode", None)
                if callable(fn):
                    fn(nxt)
                else:                      # pre-O4 engine: boolean only
                    st["scale_snap"] = (nxt != "off")
                self._oled_flash("SCALE MODE", nxt.upper())
            elif row == _KEY_ROOT_ROW and col == _KEY_COL_QUAL:
                st["quality_mode"] = ("DIAT" if st.get("quality_mode") == "FIXED"
                                      else "FIXED")
                # Clear in BOTH directions, matching orc1.py's
                # TOGGLE_QUALITY_MODE. This pad used to clear only on the way
                # back to DIAT, which re-created the phantom "FIXED*" readout
                # the engine handler was fixed to prevent -- the same bug, just
                # reached through the overlay instead of the web button.
                st["base_override"] = False
                # ...and retrigger, so a chord being HELD changes quality now
                # rather than silently waiting for the next key press while the
                # OLED claims it already changed.
                self._retrigger()
                self._oled_flash("QUALITY", st["quality_mode"])
            elif row == _KEY_ROOT_ROW and col == _KEY_COL_PIN:
                self._key_overlay_latched = not self._key_overlay_latched
                self._oled_flash("KEY OVERLAY",
                                 "PINNED" if self._key_overlay_latched else "OFF")
                if not self._key_overlay_latched and not self._alt:
                    # Unpinned with ALT already released: the overlay is gone,
                    # so repaint the actual view rather than leaving its pads up.
                    self._render(); self._oled()
                    return
            else:
                return
            self._render_key_overlay()
            return

        idx = col - _KEY_ROOT_COL0

        if row == _KEY_ROOT_ROW:
            # Root pad = set the KEY tonic.  Unaffected by KEY LOCK, which since
            # 2026-07-29 pulls an off-key CHORD ROOT into the key and no longer
            # has anything to do with guarding this write (the keyboard can't
            # touch the key at all now -- it sets chord_root).
            self._set_root(idx)
            rn = getattr(self.eng, "ROOT_NAMES",
                         ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"])
            self._oled_flash("ROOT", rn[idx % 12])
        else:
            groups = self._scale_groups()
            if row >= len(groups):
                return
            names = groups[row][1]
            if idx >= len(names):
                return
            self._set_scale(names[idx])
            self._oled_flash("SCALE", names[idx])

        self._render_key_overlay()

    def _cycle_chord_bank(self, delta):
        """GRID <> in CHORD view: switch the active row/routing BANK.

        Each bank is a full 4-row config (types / ports / channels / absolute
        octaves). Edits in Configure-Rows land in the active bank and persist
        immediately, so every bank is always saved and reloaded on boot -- flip
        live between e.g. an all-internal stack and an all-external RTP setup.
        Count is _CHORD_N_BANKS (freely raised, not a fixed limit). Wraps, so you
        can page in either direction without hitting a wall.
        """
        n = len(self._chord_banks)
        if n <= 0:
            return
        self._chord_bank = (self._chord_bank + delta) % n
        # Re-point _chord_rows at the new active bank; edits/renders follow it.
        self._chord_rows = self._chord_banks[self._chord_bank]
        self._chord_row_top = 0          # a fresh bank starts at the top of its stack
        self._save_chord_rows()
        self._render_perform()
        self._oled_flash("ROW BANK", f"{self._chord_bank + 1} / {n}")

    def _chord_scroll_viewport(self, step):
        """SELECT-turn in the chord view: scroll the 4-row viewport up/down the
        tall row stack (SHIFT+SELECT keeps the arp-pattern scroll it used to have).
        Clamped, not wrapped, so the ends are obvious."""
        n = len(self._chord_rows)
        maxtop = max(0, n - N_ROWS)
        new = max(0, min(maxtop, self._chord_row_top + step))
        if new == self._chord_row_top:
            return
        self._chord_row_top = new
        self._render_perform()
        self._oled_flash("ROW WINDOW",
                         f"R{new + 1}-{new + N_ROWS} / {n}")

    def _cycle_chord_voicing(self, delta):
        """No longer bound to GRID (that now flips row banks, 2026-09-03); chord
        voicing lives on the `spread` knob (knob 0 in chord view), which this only
        ever nudged to 3 fixed points anyway. Kept for reference / remap targets.

        GRID <> in CHORD view: cycle through 3 voicing presets. Session E
        judgment call -- there's no existing discrete voicing-preset list in
        the engine, only the continuous 'spread' dial that _dial_spread
        already derives CLOSE/OPEN/SPREAD labels from. This maps GRID L/R
        onto 3 fixed points along that same dial so the gesture does
        something musically sensible without inventing new engine state."""
        presets = [10, 50, 100]   # CLOSE / OPEN / SPREAD representative values
        st  = self.eng.state
        cur = st.get("spread", 64)
        idx = min(range(len(presets)), key=lambda i: abs(presets[i] - cur))
        idx = (idx + delta) % len(presets)
        st["spread"]  = presets[idx]
        st["voicing"] = ("CLOSE" if st["spread"] < 20 else
                         "OPEN" if st["spread"] < 80 else "SPREAD")
        self._retrigger()
        self._oled()

    def _cycle_fx_chain(self, delta):
        """BROWSER turn in FX view: move slot selection across the rack's slots."""
        rack = self._fx_rack()
        n = int(rack.status().get("cap", 3)) if rack else 3
        self._fx_sel_slot   = (self._fx_sel_slot + delta) % n
        self._fx_last_param = None
        self._render_fx()
        self._oled()


    def _fx_load_assignments(self):
        """Load FX slot assignments from ~/orc1_state.json if present."""
        try:
            import json, os
            path = os.path.expanduser("~/orc1_state.json")
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                saved = data.get("fx_slot_assignments")
                if isinstance(saved, list) and len(saved) == 8:
                    self._fx_slot_assignments = [
                        s if isinstance(s, str) else None for s in saved]
        except Exception as e:
            print("  [warn] fx_load_assignments: " + str(e))
        # Slot 0 is always fixed to nes_space regardless of saved state
        self._fx_slot_assignments[0] = "nes_space"

    def _fx_save_assignments(self):
        """Persist FX slot assignments to ~/orc1_state.json."""
        try:
            import json, os
            path = os.path.expanduser("~/orc1_state.json")
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data["fx_slot_assignments"] = self._fx_slot_assignments
            _atomic_write_json(path, data)
        except Exception as e:
            print("  [warn] fx_save_assignments: " + str(e))

    def _save_fire_state(self):
        """Auto-save fire state (CC vals, voice, octave, FX params) into
        ~/orc1_state.json.

        Called from the Save State menu, from a dozen event sites, and
        periodically by _oled_keepalive_loop -- which uses save_every = 60.0,
        NOT the "~25 s" this docstring claimed until 2026-07-30. The real
        interval matters: it is how long a key stays missing from the file if
        anything clobbers it, and how much is lost if power is cut before the
        next save. Writes atomically via _atomic_write_json (see there for why).
        """
        try:
            import json as _j, os as _o
            path = _o.path.expanduser("~/orc1_state.json")
            try:
                with open(path) as _f:
                    data = _j.load(_f)
            except Exception:
                data = {}
            cc_out = {}
            for (port, ch, cc), val in self._cc_vals.items():
                cc_out[str(port) + "|" + str(ch) + "|" + str(cc)] = val
            data["fire_cc_vals"]        = cc_out
            data["fire_chip_voice"]     = self.chip_voice
            data["fire_chip_base_note"] = self.chip_base_note
            data["fire_poly_base_note"] = self.poly_base_note
            data["fire_fx_params"]      = self._fx_param_vals
            # MPC faders: encode int keys as strings for JSON.
            data["fire_mpc_faders"] = {
                str(k): v for k, v in self._mpc_faders.items()
            }
            # Universal pads: JSON requires string keys, so encode
            # (row,col) tuples as "r,c". Skip runtime-only "_" keys
            # (_latched_on) so latch state resets cleanly on restart.
            #
            # TWO keys are written, deliberately:
            #   fire_universal_banks  every bank -- the real state (2026-07-31)
            #   fire_universal_pads   bank 0 only -- the shape orc1_web.py's
            #                         /api/universal_pads and the /control page
            #                         have always read.
            # Writing both keeps the web page working unchanged instead of
            # silently showing nothing the day banks shipped. bank 0 is the
            # mirror because that is the bank the web page edits (its
            # UNIVERSAL_SET_PAD defaults to bank 0), so the two agree.
            data["fire_universal_banks"] = {
                str(bi): {
                    f"{k[0]},{k[1]}": {ek: ev for ek, ev in v.items()
                                       if not ek.startswith("_")}
                    for k, v in pads.items()
                }
                for bi, pads in self._universal_banks.items()
            }
            data["fire_universal_pads"] = \
                data["fire_universal_banks"].get("0", {})
            # Per-page knob overrides (Fire reorg Phase 2). Separate key so it
            # never disturbs the pad keys above. {bank: {layer: {idx: action}}}.
            data["fire_universal_knobs"] = {
                str(bi): {lyr: {str(ix): list(act)
                                for ix, act in knobs.items()}
                          for lyr, knobs in layers.items()}
                for bi, layers in self._universal_knobs.items()
            }
            # Per-page fader rows (Phase 3).
            data["fire_universal_faders"] = {
                str(bi): {str(r): dict(f) for r, f in rows.items()}
                for bi, rows in self._universal_faders.items()
            }
            _atomic_write_json(path, data)
        except Exception as e:
            print("  [warn] _save_fire_state: " + str(e))

    def _load_fire_state(self):
        """Restore fire state on startup. CC values are re-sent to the
        synth so voice params (cutoff, resonance, ADSR, drum CCs) come
        back exactly where they were. FX param dict is restored so knob
        positions continue from where you left off when a chain is toggled."""
        try:
            import json as _j, os as _o
            path = _o.path.expanduser("~/orc1_state.json")
            if not _o.path.exists(path):
                return
            with open(path) as _f:
                data = _j.load(_f)
            if "fire_chip_base_note" in data:
                self.chip_base_note = int(data["fire_chip_base_note"])
            if "fire_poly_base_note" in data:
                self.poly_base_note = int(data["fire_poly_base_note"])
            send = getattr(self.eng, "send_to", None)
            _bus_cc = self._bus_level_ccs()
            for key_str, val in data.get("fire_cc_vals", {}).items():
                parts = key_str.split("|")
                if len(parts) != 3:
                    continue
                port, ch_s, cc_s = parts
                try:
                    # ch may be a MODULE key (knobs that name a module, 2026-09-24)
                    ch = ch_s if ch_s in _MODULE_KEYS else int(ch_s)
                    cc, ival = int(cc_s), int(val)
                    self._cc_vals[(port, ch, cc)] = ival
                    # Remember the knob position but do NOT re-send it if it is
                    # a synth BUS LEVEL: since 2026-07-29 the synth persists
                    # those itself (~/.orc1_bus_levels.json, plan item P11) and
                    # restores them at its own startup. Replaying a stale value
                    # here would overwrite that restore a second or two later
                    # -- silently, and after the synth had already logged the
                    # level it restored.
                    rch = self._resolve_ch(ch)
                    if not rch or (rch, cc) in _bus_cc:
                        continue
                    if mido and send and port == "synth":
                        send(port, mido.Message("control_change",
                                                channel=rch - 1, control=cc,
                                                value=max(0, min(127, ival))))
                except Exception:
                    pass
            saved_fx = data.get("fire_fx_params", {})
            if isinstance(saved_fx, dict):
                self._fx_param_vals.update(saved_fx)
            saved_voice = data.get("fire_chip_voice")
            if saved_voice and saved_voice != self.chip_voice:
                self._select_chip_voice(saved_voice)
            # MPC fader assignments: decode string keys back to ints.
            raw_mpc = data.get("fire_mpc_faders", {})
            if isinstance(raw_mpc, dict):
                loaded_mpc = {}
                for ks, assign in raw_mpc.items():
                    try:
                        loaded_mpc[int(ks)] = dict(assign)
                    except Exception:
                        pass
                # Only replace the default volume mixer if the user actually
                # has a saved fader bank -- an absent/empty key must NOT wipe
                # the MASTER/POLY/CHIP/DRUM defaults seeded in __init__.
                if loaded_mpc:
                    self._mpc_faders = loaded_mpc
            # A saved bank carries its own stale `value` for the bus-level
            # faders; re-point them at what the synth actually restored.
            self._sync_volume_fader_values()
            # Universal pad assignments: decode "r,c" string keys back to
            # tuples. Prefer the banked key; fall back to the pre-2026-07-31
            # single-bank key, which migrates an existing pad grid into bank 0
            # rather than discarding it.
            raw_banks = data.get("fire_universal_banks")
            if not isinstance(raw_banks, dict) or not raw_banks:
                raw_banks = {"0": data.get("fire_universal_pads", {})}
            for bank_s, raw_up in raw_banks.items():
                try:
                    bank = int(bank_s)
                except (TypeError, ValueError):
                    continue
                # A saved file from a box configured with more banks than this
                # one must not create phantom banks GRID can never reach.
                if bank not in self._universal_banks or not isinstance(raw_up, dict):
                    continue
                loaded = {}
                for ks, assign in raw_up.items():
                    try:
                        r, c = map(int, ks.split(","))
                    except Exception:
                        continue
                    if not (0 <= r < N_ROWS and 0 <= c < N_COLS):
                        continue
                    if isinstance(assign, dict):
                        loaded[(r, c)] = dict(assign)
                self._universal_banks[bank] = loaded
            # Per-page knob overrides (Fire reorg Phase 2). Validated the same
            # way the knobmap loader validates, and scoped to existing banks so
            # a file from a more-banked box can't create phantom pages.
            raw_knobs = data.get("fire_universal_knobs")
            if isinstance(raw_knobs, dict):
                for bank_s, layers in raw_knobs.items():
                    try:
                        bank = int(bank_s)
                    except (TypeError, ValueError):
                        continue
                    if bank not in self._universal_banks or not isinstance(layers, dict):
                        continue
                    for lyr, knobs in layers.items():
                        if lyr not in self._layers or not isinstance(knobs, dict):
                            continue
                        for ks, act in knobs.items():
                            try:
                                ix = int(ks)
                            except (TypeError, ValueError):
                                continue
                            if 0 <= ix <= 3 and self._valid_universal_knob(act):
                                self._universal_knobs.setdefault(
                                    bank, {}).setdefault(lyr, {})[ix] = tuple(act)
            # Per-page fader rows (Phase 3), validated + scoped to existing banks.
            raw_f = data.get("fire_universal_faders")
            if isinstance(raw_f, dict):
                for bank_s, rows in raw_f.items():
                    try:
                        bank = int(bank_s)
                    except (TypeError, ValueError):
                        continue
                    if bank not in self._universal_banks or not isinstance(rows, dict):
                        continue
                    for rs, f in rows.items():
                        try:
                            r = int(rs)
                            cc = int(f.get("cc", 7)); ch = int(f.get("channel", 1))
                        except (TypeError, ValueError, AttributeError):
                            continue
                        if (0 <= r < N_ROWS and 0 <= cc <= 127 and 1 <= ch <= 16
                                and f.get("port")):
                            self._universal_faders.setdefault(bank, {})[r] = {
                                "cc": cc, "channel": ch,
                                "port": str(f.get("port", "pisound")),
                                "value": max(0, min(127, int(f.get("value", 0)))),
                            }
        except Exception as e:
            print("  [warn] _load_fire_state: " + str(e))

    def _fx_assign_slot(self, delta):
        """ALT+BROWSER in FX view: quick-cycle the selected slot's plugin through
        the current browse bank (Favorites / All / category) WITHOUT opening the
        full overlay.  '(empty)' clears the slot.  Drives eng.fx.rack."""
        rack = self._fx_rack()
        if rack is None:
            return
        _bank, options = self._fx_browse_options()   # [None] + bank keys
        if not options:
            return
        slots = rack.status().get("slots", [])
        cur = slots[self._fx_sel_slot].get("plugin") if self._fx_sel_slot < len(slots) else None
        try:
            oi = options.index(cur)
        except ValueError:
            oi = 0
        oi = (oi + delta) % len(options)
        new_key = options[oi]
        self._fx_last_param = None
        if new_key is None:
            rack.clear(self._fx_sel_slot, done=self._fx_after_change)
        else:
            rack.assign(self._fx_sel_slot, new_key, done=self._fx_after_change)

        if fire_oled:
            try:
                lbl = (rack.label_for(new_key) or new_key) if new_key else "(empty)"
                fire_oled.show_lines([
                    ("FX " + str(self._fx_sel_slot + 1) + ": " + lbl, 2, 0, 2),
                    ("ALT+<> cycle (" + _bank[:9] + ")", 2, 28, 1),
                    ("<> select slot  press=browse", 2, 44, 1)])
            except Exception:
                pass
        self._render_fx()


    def _aw_display_name(self, key):
        """Return the user-facing plugin name for a chain key.
        Looks up catalog; falls back to a hardcoded map for legacy chains.
        nes_space wraps the Galactic plugin -- show "Galactic".
        Any other legacy chain: show its key as-is.
        """
        if key is None:
            return "(empty)"
        self._aw_load_catalog()
        if key in self._aw_catalog:
            return self._aw_catalog[key].get("name", key)
        # Legacy chain fallback -- map to the underlying plugin name
        _LEGACY = {
            "nes_space":    "Galactic",
            "tape_warmth":  "ToTape6",
            "drum_glue":    "Pressure5",
            "verbity_room": "Verbity",
            "tape_and_glue": "ToTape6+Pressure5",
        }
        return _LEGACY.get(key, key)

    def _aw_load_catalog(self):
        """Lazily load ~/orc1_airwindows_catalog.json (built by build_aw_catalog.py)."""
        if self._aw_catalog is not None:
            return
        import json, os
        path = os.path.expanduser("~/orc1_airwindows_catalog.json")
        try:
            with open(path) as f:
                self._aw_catalog = json.load(f)
            self._aw_catalog_keys = sorted(self._aw_catalog.keys())
        except Exception as e:
            print("  [warn] aw_load_catalog: " + str(e))
            self._aw_catalog = {}
            self._aw_catalog_keys = []

    def _aw_ensure_chain(self, key):
        """Write a single-plugin chain entry into fx_chains.json for key if absent."""
        import json, os
        self._aw_load_catalog()
        if key not in self._aw_catalog:
            return
        chains_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_chains.json")
        try:
            with open(chains_path) as f:
                chains_data = json.load(f)
        except Exception:
            chains_data = {}
        if key in chains_data:
            return   # already present -- do not overwrite hand-tuned presets
        plugin = self._aw_catalog[key]
        client = "fx-" + key + "-0"
        entry = {
            "description": plugin.get("description", key),
            "exclusive": False,
            "plugins": [{
                "uri":         plugin["uri"],
                "jack_in":     ["pisound-in:capture_1",   "pisound-in:capture_2"],
                "jack_out_to": [client + ":inL",           client + ":inR"],
                "out_to_next": [client + ":outL",          client + ":outR"],
                "next_in":     ["pisound-out:playback_1", "pisound-out:playback_2"],
                "params":      plugin.get("params", [])
            }]
        }
        chains_data[key] = entry
        try:
            with open(chains_path, "w") as f:
                json.dump(chains_data, f, indent=2)
            # Invalidate FXManager cache so load_chains() sees the new entry
            _fx = getattr(self.eng, "fx", None)
            if _fx is not None:
                _fx._chains_cache = None
        except Exception as e:
            print("  [warn] aw_ensure_chain: " + str(e))

    # -- Rack plugin browser (banked: Favorites / All / categories) -----------
    # Slot pad on an empty slot opens this.  SELECT-turn scrolls plugins in the
    # current bank; SHIFT+SELECT-turn changes bank; SELECT-press assigns the
    # highlighted plugin to the slot; SHIFT+SELECT-press toggles it as a
    # favourite.  All plugin data comes from eng.fx.rack (palette + banks).

    def _fx_browse_options(self):
        """(bank_name, [None] + plugin_keys) for the current bank.

        The leading None is the '(empty)' entry so you can clear a slot from
        within the browser, matching the pre-rack behaviour."""
        rack = self._fx_rack()
        if rack is None:
            return ("All", [None])
        banks = rack.banks()
        if not banks:
            return ("All", [None])
        self._fx_bank_idx %= len(banks)
        bank = banks[self._fx_bank_idx]
        return (bank, [None] + rack.bank_items(bank))

    def _fx_browse_enter(self, slot):
        """Open the plugin browser for `slot`, pointing at its current plugin."""
        rack = self._fx_rack()
        if rack is None:
            return
        self._fx_browse_slot = slot
        # Don't open onto an empty Favorites bank (looks broken) — fall back to
        # 'All' so there's always something to scroll.
        banks = rack.banks()
        if banks and banks[self._fx_bank_idx % len(banks)] == "Favorites" \
                and not rack.favorites():
            self._fx_bank_idx = banks.index("All") if "All" in banks else 0
        # Point the cursor at whatever the slot currently holds, if that plugin
        # happens to be in the current bank; otherwise start at '(empty)'.
        cur_plugin = None
        st = rack.status().get("slots", [])
        if slot < len(st):
            cur_plugin = st[slot].get("plugin")
        _bank, options = self._fx_browse_options()
        try:
            self._fx_browse_idx = options.index(cur_plugin)
        except ValueError:
            self._fx_browse_idx = 0
        self._overlay = "fx_browse"
        self._fx_browse_oled()

    def _fx_browse_scroll(self, step):
        """SELECT turn: step through plugins in the current bank."""
        _bank, options = self._fx_browse_options()
        n = len(options)
        self._fx_browse_idx = (self._fx_browse_idx + step) % n if n else 0
        self._fx_browse_oled()

    def _fx_browse_bank(self, step):
        """SHIFT+SELECT turn: change browse bank (Favorites / All / category)."""
        rack = self._fx_rack()
        if rack is None:
            return
        banks = rack.banks()
        if not banks:
            return
        self._fx_bank_idx = (self._fx_bank_idx + step) % len(banks)
        self._fx_browse_idx = 0     # restart at the top of the new bank
        self._fx_browse_oled()

    def _fx_browse_fav_toggle(self):
        """SHIFT+SELECT press: toggle the highlighted plugin as a favourite.
        Stays in the browser so you can keep curating."""
        rack = self._fx_rack()
        if rack is None:
            return
        _bank, options = self._fx_browse_options()
        n   = len(options)
        key = options[self._fx_browse_idx % n] if n else None
        if key is None:
            return                  # '(empty)' isn't favouritable
        now = rack.favorites_toggle(key)
        self._oled_flash("FAVORITE" if now else "UNFAVORITED",
                         (rack.label_for(key) or key)[:16])
        self._fx_browse_oled()

    def _fx_browse_oled(self):
        """OLED for the browser.  The current plugin gets the big top line (★ if
        favourited); the bank + position counter and the gesture hint sit below
        at small size so nothing clips on the narrow display."""
        if not fire_oled:
            return
        rack = self._fx_rack()
        bank, options = self._fx_browse_options()
        n   = len(options)
        idx = self._fx_browse_idx % n if n else 0
        cur = options[idx] if n else None

        def _lbl(k):
            if k is None:
                return "(empty)"
            return (rack.label_for(k) if rack else k) or k

        star = " *" if (rack and cur and rack.is_favorite(cur)) else ""
        try:
            fire_oled.show_lines([
                (_lbl(cur)[:13] + star, 2, 0, 2),
                (bank[:10] + "  " + str(idx) + "/" + str(n - 1), 2, 20, 1),
                ("SHtrn=bank", 2, 34, 1),
                ("SHpr=fav", 2, 48, 1)])
        except Exception:
            pass

    def _fx_browse_confirm(self):
        """SELECT press: assign the highlighted plugin to the slot (or clear it
        if '(empty)' is selected), then close the browser."""
        rack = self._fx_rack()
        slot = self._fx_browse_slot
        self._overlay = None
        self._fx_browse_slot = None
        self._fx_last_param = None
        if rack is None or slot is None:
            self._render_fx()
            self._oled()
            return
        _bank, options = self._fx_browse_options()
        n   = len(options)
        key = options[self._fx_browse_idx % n] if n else None
        if key is None:
            rack.clear(slot, done=self._fx_after_change)
        else:
            # assign() enables the slot and resets its params — you almost
            # always want to hear what you just dropped in.
            rack.assign(slot, key, done=self._fx_after_change)
        self._render_fx()
        self._oled()

    def _act_select_press(self, pressed):
        """SELECT encoder press -- Session E (2026-06-30) view-aware dispatch.
        Priority: step-note editing > BROWSER menu confirm > kmap confirm >
        per-view default (SEQ track-edit / MIXER solo / CHORD+chip+poly
        summary / FADERS + UNIVERSAL enter-or-confirm assign / FX slot browse).
        All eight views are implemented; the old "stubs until Steps 13/16/17"
        note here was stale (corrected 2026-09-02)."""
        if not pressed:
            return
        if self._held_seq_pad is not None and self.view == "seq":
            tidx, spos = self._held_seq_pad
            if tidx < self.n_tracks:
                t  = self.tracks[tidx]
                sn = t.get("step_notes")
                if sn and sn[spos] is not None:
                    self.seq.set_step_note(tidx, spos, None)  # lock-guarded (Bug B)
                    self._oled_step_note(tidx, spos)
            return
        if self._overlay == "preset_picker":
            self._preset_confirm()
            return
        if self._menu_open:
            self._menu_confirm()
            return
        if self._overlay == "kmap_cc":
            self._kmap_cc_commit()
            return
        if self._overlay == "kmap":
            self._kmap_assign()
            return
        if self.view == "seq":
            self._overlay = None if self._overlay == "track_edit" else "track_edit"
            self._oled()
        elif self.view == "mixer":
            self._toggle_solo_selected_track()
        elif self.view == "chord":
            if self._overlay == "chord_params":
                # Already in the editor -- press exits.
                self._overlay = None
                self._render()
                self._oled()
            else:
                self._oled_chord_param_menu()
        elif self.view in ("chip", "poly"):
            self._oled_voice_summary()
        elif self.view == "faders":
            if self._mpc_assign_mode:
                self._mpc_assign_confirm()
            elif self._mpc_last_fader is not None:
                self._mpc_enter_assign(self._mpc_last_fader)
            else:
                self._oled_faders()   # nudge: "tap a row first"
        elif self.view == "universal":
            if self._universal_assign_mode:
                # SELECT confirms the current edit.
                self._universal_assign_confirm()
            elif self._last_universal_pad is not None:
                # Enter assign mode for the last-touched pad.
                self._universal_enter_assign(self._last_universal_pad)
            else:
                self._oled_universal()   # nudge: "tap a pad first"
        elif self.view == "fx":
            if self._overlay == "fx_browse":
                # SHIFT+press toggles favourite (stay in browser); plain press
                # assigns the highlighted plugin to the slot and closes.
                if self._shift:
                    self._fx_browse_fav_toggle()
                else:
                    self._fx_browse_confirm()
            elif self._fx_held_slot is not None:
                # Pad held + SELECT press: enter chain browse for this slot.
                self._fx_browse_enter(self._fx_held_slot)
        # (faders / universal / fx are all handled above -- no silent fallthrough.)

    # ==========================================================================
    # DIAL ACTIONS  (_dial_<name>(idx, step, *args))
    # ==========================================================================

    def _dial_bpm(self, idx, step):
        arp = self.eng.arp
        if not arp:
            return
        from orc1_arp import CLOCK_DRIFT
        bpm = self._bpm() + step
        (arp.set_drift_bpm if arp.clock_mode == CLOCK_DRIFT
         else arp.set_internal_bpm)(bpm)
        self._oled()

    def _edit_step_note(self, tidx, spos, delta):
        """Dial in a per-step note override (hold step pad + turn SELECT).
        Chord-mode tracks cycle through tone indices; note-mode tracks step MIDI note.
        The step is automatically switched ON if it wasn't already.
        2026-07-01: resolving the next value is still Fire's own UI logic
        (needs the dial delta + current value), but the actual write goes
        through seq.set_step_note (lock-guarded -- Bug B)."""
        if tidx >= self.n_tracks:
            return
        t  = self.tracks[tidx]
        sn = t.setdefault("step_notes", [None] * MAX_LEN)

        if t.get("mode") == "chord":
            tones    = _CHORD_TONE_ORDER
            base     = sn[spos] if sn[spos] is not None else int(t.get("note", 0))
            cur      = tones.index(base) if base in tones else 1
            new_note = tones[(cur + delta) % len(tones)]
        else:
            base     = sn[spos] if sn[spos] is not None else int(t.get("note", 60))
            new_note = max(0, min(127, base + delta))

        self.seq.set_step_note(tidx, spos, new_note)
        self._render_channel()
        self._oled_step_note(tidx, spos)

    def _oled_step_note(self, tidx, spos):
        """OLED feedback while dialling a step note override."""
        if not fire_oled or tidx >= self.n_tracks:
            return
        try:
            t          = self.tracks[tidx]
            sn         = t.get("step_notes", [None] * MAX_LEN)
            override   = sn and sn[spos] is not None
            note       = int(sn[spos] if override else t.get("note", 60))
            track_note = int(t.get("note", 60))
            ch_drum    = int(self.cfg.SYNTH.get("CH_DRUM", 10))
            mode       = t.get("mode", "note")

            if int(t.get("ch", 6)) == ch_drum:
                note_lbl  = self._voice_display.get(note, _note_name(note))
                dflt_lbl  = self._voice_display.get(track_note, _note_name(track_note))
            elif mode == "chord":
                note_lbl  = _CHORD_TONES.get(note, f"T{note}")
                dflt_lbl  = _CHORD_TONES.get(track_note, f"T{track_note}")
            else:
                note_lbl  = f"{_note_name(note)} ({note})"
                dflt_lbl  = _note_name(track_note)

            fire_oled.show_lines([
                (f"t{tidx+1} step {spos+1}", 2, 0, 1),
                (note_lbl, 2, 16, 2),
                (f"dflt {dflt_lbl}  {'*' if override else 'default'}", 2, 48, 1)])
        except Exception:
            pass

    def _oled_step_vel(self, tidx, spos):
        """OLED feedback while SHIFT-tapping a step through its velocity tiers.
        Shows the tier name and the velocity actually being played."""
        if not fire_oled or tidx >= self.n_tracks:
            return
        try:
            t   = self.tracks[tidx]
            sv  = t.get("step_vels", [None] * MAX_LEN)
            ov  = sv[spos] if sv and spos < len(sv) else None
            try:
                tiers = self.seq._vel_tiers()          # [ghost, normal, accent]
            except Exception:
                tiers = [60, 95, 127]
            names = ["GHOST", "NORMAL", "ACCENT"]
            if ov is None:
                tier_lbl = "DEFAULT"
                vel      = int(t.get("vel", 100))
            else:
                vel      = int(ov)
                tier_lbl = names[tiers.index(ov)] if ov in tiers else "CUSTOM"
            fire_oled.show_lines([
                (f"t{tidx+1} step {spos+1}", 2, 0, 1),
                (tier_lbl, 2, 16, 2),
                (f"vel {vel}", 2, 48, 1)])
        except Exception:
            pass

    def _dial_track_vel(self, idx, step):
        # MIXER view edits the knob's own row; elsewhere the selected track.
        # 2026-07-01: routed through seq.set_track_prop (lock-guarded, Bug B).
        tidx = (self.view_top + idx) if self.view == "mixer" else self._sel_track
        if tidx < self.n_tracks:
            self.seq.set_track_prop(tidx, "vel", self.tracks[tidx]["vel"] + step * 2)
            if self.view == "mixer":
                self._render_mixer()

    def _dial_track_length(self, idx, step):
        # Session E (2026-06-30): kept but deliberately unmapped everywhere --
        # track length is set ONLY by ALT+step pad now (far harder to misfire
        # than a knob). Left in place in case a future remap wants it back.
        # 2026-07-01: routed through seq.set_track_prop (lock-guarded, Bug B).
        tidx = (self.view_top + idx) if self.view == "mixer" else self._sel_track
        if tidx < self.n_tracks:
            self.seq.set_track_prop(tidx, "length", self.tracks[tidx]["length"] + step)
            self._render()
            self._oled()

    def _dial_track_note(self, idx, step):
        """Set the MIDI note the selected track fires (melodic sequencing).
        2026-07-01: routed through seq.set_track_prop (lock-guarded, Bug B)."""
        if self._sel_track < self.n_tracks:
            t = self.tracks[self._sel_track]
            self.seq.set_track_prop(self._sel_track, "note", int(t["note"]) + step)
            self._oled_track()

    def _dial_track_channel(self, idx, step):
        """Set the MIDI channel (1..16) the selected track fires on, e.g. 8 to
        drive the internal poly synth.
        2026-07-01: routed through seq.set_track_prop (lock-guarded, Bug B)."""
        if self._sel_track < self.n_tracks:
            t = self.tracks[self._sel_track]
            self.seq.set_track_prop(self._sel_track, "ch", int(t["ch"]) + step)
            self._oled_track()

    def _oled_track(self):
        if not fire_oled:
            return
        try:
            t = self.tracks[self._sel_track]
            fire_oled.show_lines([
                ("TRACK", 2, 0, 1),
                (f"t{self._sel_track+1} {self._track_ctx_label(t)}", 2, 22, 1),
                (f"note {t['note']}  ch {t['ch']}", 2, 42, 1)])
        except Exception:
            pass

    # Smooth hue sweep for ALT+knob2 -- a palette-index CYCLE strobed between very
    # different hues each detent (epilepsy hazard); walking the hue wheel in small
    # steps keeps adjacent knob positions perceptually close (red->orange->yellow)
    # and reaches the whole spectrum, not just the 8 palette presets.
    _HUE_STEP_DEG = 4.0            # hue degrees per encoder detent
    _HUE_SAT      = 1.0           # vivid
    _HUE_VAL      = 0x40 / 127.0  # match the palette's dim brightness (max chan ~64)

    def _dial_track_color(self, idx, step):
        """ALT+knob2 (seq view): sweep the SELECTED track's colour smoothly around
        the hue wheel. Routed through seq.set_track_prop('color'), which persists
        it instantly to ~/.orc1_seq_colors.json -- no SEQ_SAVE required."""
        tidx = self._sel_track
        if tidx >= self.n_tracks:
            return
        h = self._track_hue.get(tidx)
        if h is None:
            h = self._hue_of(self.tracks[tidx].get("color"))
        h = (h + step * self._HUE_STEP_DEG) % 360.0
        self._track_hue[tidx] = h
        r, g, b = self._hue_rgb(h)
        self.seq.set_track_prop(tidx, "color", f"{r},{g},{b}")
        self._render_channel()
        self._oled_track_color(tidx, h)

    @classmethod
    def _hue_rgb(cls, hue_deg):
        """(hue degrees) -> (r,g,b) on the 0-127 pad scale at the fixed sat/val."""
        r, g, b = colorsys.hsv_to_rgb((hue_deg % 360.0) / 360.0,
                                       cls._HUE_SAT, cls._HUE_VAL)
        return int(round(r * 127)), int(round(g * 127)), int(round(b * 127))

    @staticmethod
    def _hue_of(color):
        """Hue (degrees) of a track's current colour; 0 if missing/grey. Lets the
        relative encoder pick up smoothly from wherever the colour already sits
        (e.g. one chosen on the web), not jump to a fixed start."""
        if not color:
            return 0.0
        try:
            r, g, b = int(color[0]) / 127.0, int(color[1]) / 127.0, int(color[2]) / 127.0
        except (TypeError, ValueError, IndexError):
            return 0.0
        h, _s, _v = colorsys.rgb_to_hsv(r, g, b)
        return h * 360.0

    def _oled_track_color(self, tidx, hue):
        if not fire_oled:
            return
        try:
            r, g, b = self.tracks[tidx].get("color", (0, 0, 0))
            fire_oled.show_lines([
                ("TRACK COLOR", 2, 0, 1),
                (f"t{tidx+1}  hue {int(hue)}", 2, 22, 1),
                (f"rgb {int(r)} {int(g)} {int(b)}", 2, 42, 1)])
        except Exception:
            pass

    _BRIGHT_STEP = 6              # brightness units (0-127) per encoder detent

    def _dial_track_bright(self, idx, step):
        """ALT+knob3 (seq view): dim/brighten the SELECTED track's colour. A
        cosmetic render multiplier (0-127, 127 = full) applied by _track_color and
        the web grid; the stored hue is untouched. Routed through
        seq.set_track_prop('bright'), which persists it instantly to the seq colour
        overlay -- no SEQ_SAVE. Mirrors _dial_track_color's knob2."""
        tidx = self._sel_track
        if tidx >= self.n_tracks:
            return
        cur = int(self.tracks[tidx].get("bright", 127))
        val = max(0, min(127, cur + step * self._BRIGHT_STEP))
        self.seq.set_track_prop(tidx, "bright", val)
        self._render_channel()
        self._oled_track_bright(tidx, val)

    def _oled_track_bright(self, tidx, val):
        if not fire_oled:
            return
        try:
            pct = int(round(val * 100 / 127))
            fire_oled.show_lines([
                ("TRACK BRIGHT", 2, 0, 1),
                (f"t{tidx+1}", 2, 22, 1),
                (f"{pct}%", 2, 42, 1)])
        except Exception:
            pass

    def _note_to_voice_label(self, note):
        """Return the drum voice name for a note, or the note name if no match."""
        vidx = self._note_to_voice_idx.get(int(note))
        if vidx is not None and vidx < len(self._edit_voices):
            return self._edit_voices[vidx]
        return _note_name(note)

    def _track_ctx_label(self, t):
        """Short context string describing what a track drives."""
        ch       = int(t.get("ch", 6))
        ch_drum  = int(self.cfg.SYNTH.get("CH_DRUM",     10))
        ch_mono  = int(self.cfg.SYNTH.get("CH_MONO",     7))
        ch_poly  = int(self.cfg.SYNTH.get("CH_POLY",     8))
        ch_chip  = int(self.cfg.SYNTH.get("CH_CHIP",     9))
        ch_p2    = int(self.cfg.SYNTH.get("CH_CHIP_P2",  6))
        ch_tri   = int(self.cfg.SYNTH.get("CH_CHIP_TRI", 11))
        ch_ns    = int(self.cfg.SYNTH.get("CH_CHIP_NS",  12))
        ch_pcm   = int(self.cfg.SYNTH.get("CH_CHIP_PCM", 13))
        if ch == ch_drum:
            return self._note_to_voice_label(t.get("note", 36))
        if ch == ch_chip:  return "p1"
        if ch == ch_p2:    return "p2"
        if ch == ch_tri:   return "tri"
        if ch == ch_ns:    return "noise"
        if ch == ch_pcm:   return "pcm"
        if ch == ch_poly:  return "poly"
        if ch == ch_mono:  return "mono"
        return f"ext{ch}"

    def _oled_track_edit(self):
        """OLED overlay for track-edit mode.
        Knobs 0-3 control props from self._te_props (note/ch/vel/len).
        Drum tracks show voice name; chord tracks show tone name; others show MIDI note."""
        if not fire_oled or not self.n_tracks:
            return
        try:
            t       = self.tracks[self._sel_track]
            note    = int(t.get("note", 60))
            ch      = int(t.get("ch", 8))
            vel     = int(t.get("vel", 100))
            lng     = int(t.get("length", 16))
            mode    = t.get("mode", "note")
            ch_drum = int(self.cfg.SYNTH.get("CH_DRUM", 10))
            if ch == ch_drum:
                note_label = self._voice_display.get(note, _note_name(note))
                mode_label = "drum"
            elif mode == "chord":
                note_label = _CHORD_TONES.get(note, f"T{note}")
                mode_label = "chord"
            else:
                note_label = f"{_note_name(note)}({note})"
                mode_label = "note"
            # The giant track number occupies x<42, leaving TEN characters
            # per line to its right -- so "bass drum ch10" rendered as
            # "bass drum " and "vel100 len16" as "vel100 len". Four short
            # lines instead of three long ones.
            fire_oled.show_lines([
                (f"{self._sel_track+1}", 2, 2, 4),
                (fire_oled.fit(mode_label, 42, 1, why="track mode label"), 42, 2, 1),
                (fire_oled.fit(note_label, 42, 1, why="track note/voice name"), 42, 17, 1),
                (fire_oled.fit(f"ch{ch} l{lng}", 42, 1, why="track ch/len"), 42, 32, 1),
                (fire_oled.fit(f"vel{vel}", 42, 1, why="track velocity"), 42, 47, 1)])
        except Exception:
            pass

    def _te_edit_prop(self, idx, step, prop):
        """Edit a track property in track-edit mode (called from _dispatch_dial).
        2026-07-01: value resolution (drum voice scroll / chord tone cycle /
        raw note math) stays Fire-side UI logic; the actual write goes
        through seq.set_track_prop (lock-guarded -- Bug B)."""
        if self._sel_track >= self.n_tracks:
            return
        t       = self.tracks[self._sel_track]
        ch_drum = int(self.cfg.SYNTH.get("CH_DRUM", 10))
        is_drum = int(t.get("ch", 6)) == ch_drum

        if prop in ("note", "voice"):
            if is_drum:
                # Drum track: scroll through voice note order by name
                vno = list(self._voice_note_order)
                cur_note = int(t.get("note", 36))
                notes = [n for n, _ in vno]
                try: cur = notes.index(cur_note)
                except ValueError: cur = 0
                new_note = vno[(cur + step) % len(vno)][0]
                self.seq.set_track_prop(self._sel_track, "note", new_note)
                self._sync_voice_to_track()
            elif t.get("mode") == "chord":
                # Chord track: cycle through tone indices
                tones = _CHORD_TONE_ORDER
                cur   = tones.index(int(t.get("note", 0))) if int(t.get("note", 0)) in tones else 1
                self.seq.set_track_prop(self._sel_track, "note", tones[(cur + step) % len(tones)])
            else:
                # Melodic track: raw MIDI note number
                self.seq.set_track_prop(self._sel_track, "note", int(t["note"]) + step)
        elif prop == "ch":
            self.seq.set_track_prop(self._sel_track, "ch", int(t["ch"]) + step)
        elif prop == "vel":
            self.seq.set_track_prop(self._sel_track, "vel", int(t["vel"]) + step * 2)
        # "len" removed Session E (2026-06-30) from the default _te_props list --
        # left here only in case a hand-edited TRACK_EDIT_KNOBS config still
        # names it; track length is set ONLY by ALT+step pad going forward.
        elif prop == "len":
            self.seq.set_track_prop(self._sel_track, "length", int(t["length"]) + step)
            self._render()
        self._oled_track_edit()

    def _dial_spread(self, idx, step):
        st = self.eng.state
        st["spread"] = max(0, min(127, st.get("spread", 64) + step * 4))
        st["voicing"] = ("CLOSE" if st["spread"] < 20 else
                         "OPEN" if st["spread"] < 80 else "SPREAD")
        self._retrigger()

    def _dial_oct_offset(self, idx, step):
        st = self.eng.state
        st["oct_offset"] = max(-2, min(2, st.get("oct_offset", 0) + step))
        self._retrigger()

    def _dial_none(self, idx, step):
        """An intentionally-unassigned knob."""
        pass

    # -- Module -> channel, LIVE (2026-09-24) -----------------------------------
    # The brain's _bindings ({module: channel}) is the truth; it changes when a
    # module is moved on /matrix. Knob actions and the chip/poly keyboards name
    # a module and resolve it here at the moment they send.
    _VOICE_MODULE = {'pulse1': 'chip_p1', 'pulse2': 'chip_p2',
                     'triangle': 'chip_tri', 'noise': 'chip_noise',
                     'wave': 'gb_wave', 'poly': 'poly'}

    def _module_ch(self, module):
        """Live channel of `module`, or None if it is not bound. Without the
        brain's binding table (tests / a stand-alone handler) falls back to the
        cfg.SYNTH channel key."""
        b = getattr(self.eng, "_bindings", None)
        if isinstance(b, dict):
            ch = b.get(module)
            return int(ch) if ch else None
        key = _MODULE_CH_KEY.get(module)
        try:
            return int(self.cfg.SYNTH[key]) if key else None
        except Exception:
            return None

    def _resolve_ch(self, ch):
        """A send action's channel field -> an int channel, or None (unbound)."""
        if isinstance(ch, str):
            return self._module_ch(ch)
        return int(ch)

    def _ch_label(self, ch):
        """OLED label for a send channel: 'ch 8', or 'P2>8' / 'P2>--' for a module."""
        if isinstance(ch, str):
            r = self._module_ch(ch)
            return f"{_MODULE_SHORT.get(ch, ch[:5])}>{r if r else '--'}"
        return f"ch{ch}"

    def forget_synth_channel(self, ch):
        """Drop every held keyboard note the Fire sent on synth channel `ch`.
        The brain calls this when the module on `ch` is rebound/unbound, AFTER
        it released those notes on `ch` itself -- so the pad's later release
        sends nothing, instead of a note-off onto whatever is bound there next."""
        for d in (self._poly_held, self._chip_held):
            for k in [k for k, v in d.items()
                      if isinstance(v, tuple) and len(v) == 2 and v[1] == ch]:
                d.pop(k, None)
        for k in [k for k, v in self._dir_held.items()
                  if v[0] == "synth" and v[1] == ch]:
            self._dir_held.pop(k, None)

    def _live_ch(self, module, key=None, default=None):
        """Channel for a keyboard/voice path: the live binding, or None when the
        module is UNBOUND -- and every caller then sends nothing. (Until
        2026-10-01 an unbound module fell back to its shipped cfg channel, so
        the POLY/CHIP keyboards kept playing whatever module now sat there.)
        `key` / `default` are unused, kept so old call shapes still work; the
        no-binding-table fallback for tests lives in _module_ch."""
        return self._module_ch(module)

    @property
    def _ch_poly(self):     return self._live_ch("poly", "CH_POLY", 8)
    @property
    def _ch_chip(self):     return self._live_ch("chip_p1", "CH_CHIP", 9)
    @property
    def _ch_chip_p2(self):  return self._live_ch("chip_p2", "CH_CHIP_P2", 6)
    @property
    def _ch_chip_tri(self): return self._live_ch("chip_tri", "CH_CHIP_TRI", 11)
    @property
    def _ch_chip_ns(self):  return self._live_ch("chip_noise", "CH_CHIP_NS", 4)
    @property
    def _ch_gb_wave(self):  return self._live_ch("gb_wave", "CH_GB_WAVE", 5)
    # All of these are None when the module is unbound (= send nothing).
    @property
    def _poly_ch_now(self):
        """The poly view always plays the poly module (polyphonic)."""
        return self._ch_poly
    @property
    def _chip_ch_now(self):
        """Channel of the chip view's selected voice (MUTE rows), or None."""
        m = self._VOICE_MODULE.get(getattr(self, "chip_voice", "pulse1"), "chip_p1")
        return self._live_ch(m, _MODULE_CH_KEY.get(m, "CH_CHIP"), 9)

    def _dial_cc_out(self, idx, step, port, ch, cc, scale=2):
        """Send a 0..127 MIDI CC to a named port. Works for outboard gear
        (port "pisound" -> DIN) and the internal synth (port "synth" -> bus),
        since both are registered in the engine's port table. The relative
        encoder accumulates into an absolute value held per (port, ch, cc).
        `ch` may name a MODULE (resolved live, see _module_ch); the remembered
        value is keyed by that name, so it follows the module, not a channel.
        An unbound module sends nothing (the OLED says so)."""
        ch = ch if isinstance(ch, str) else int(ch)
        cc = int(cc)
        key = (port, ch, cc)
        val = self._cc_vals.get(key, 64)
        val = max(0, min(127, val + step * scale))
        self._cc_vals[key] = val
        send_val = self._scale_cc(cc, val)
        rch = self._resolve_ch(ch)
        send = getattr(self.eng, "send_to", None)
        if mido and send and rch:
            try:
                send(port, mido.Message("control_change",
                                        channel=rch - 1, control=cc, value=send_val))
            except Exception as e:
                _swallowed("cc_out send", e)
        self._oled_cc(port, ch, cc, val)

    # -- Chip/GB expansion (2026-07-20): pitch-bend from a relative encoder ----
    # ("pitch_out", port, ch) knob-target type.  Same accumulate-and-send
    # pattern as _dial_cc_out, but emits MIDI pitchwheel (-8192..8191) instead
    # of a CC, and springs back to centre when the knob goes idle -- so it
    # behaves like a real pitch-bend wheel, not a latching offset.
    _PITCH_BEND_STEP  = 256    # bend units per encoder click (~32 clicks/half)
    _PITCH_RECENTER_S = 0.4    # idle seconds before the bend snaps back to 0

    def _dial_pitch_out(self, idx, step, port, ch):
        """Drive MIDI pitchwheel on (port, ch) from a relative encoder.
        Each click moves the bend by _PITCH_BEND_STEP; the value is held in
        self._pitch_vals keyed by (port, ch).  A one-shot timer re-arms on
        every click and, once the knob is idle for _PITCH_RECENTER_S, snaps
        the bend back to centre (pitch 0) -- the "springy" wheel feel.
        `ch` may name a MODULE (resolved at send time in _send_pitch)."""
        ch  = ch if isinstance(ch, str) else int(ch)
        key = (port, ch)
        val = self._pitch_vals.get(key, 0)
        val = max(-8192, min(8191, val + step * self._PITCH_BEND_STEP))
        self._pitch_vals[key] = val
        self._send_pitch(port, ch, val)
        self._oled_pitch(port, ch, val)
        # Re-arm the spring-back timer: cancel any pending one, start fresh.
        old = self._pitch_timers.pop(key, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        t = threading.Timer(self._PITCH_RECENTER_S, self._pitch_recenter,
                            args=(key,))
        t.daemon = True
        self._pitch_timers[key] = t
        t.start()

    def _send_pitch(self, port, ch, pitch):
        """Emit a pitchwheel message via the engine port table (same path as
        _dial_cc_out -- orc1.py's send_to forwards any mido message type).
        `ch` may name a module; an unbound one sends nothing."""
        rch = self._resolve_ch(ch)
        send = getattr(self.eng, "send_to", None)
        if mido and send and rch:
            try:
                send(port, mido.Message("pitchwheel",
                                        channel=rch - 1, pitch=int(pitch)))
            except Exception as e:
                # send_to() already logs a port-send failure internally, but
                # a failure constructing the mido.Message itself (bad
                # channel/pitch) would reach here instead -- log it too so
                # the OLED/state showing the new bend value isn't the only
                # trace of what happened.
                print(f"  ! Fire _send_pitch: {e}", flush=True)

    def _pitch_recenter(self, key):
        """Timer callback: knob went idle -> snap the bend back to centre.
        Guarded so it can never fire into a closed/closing handler."""
        stop = getattr(self, "_stop", None)
        if stop is not None and stop.is_set():
            return
        self._pitch_timers.pop(key, None)
        if self._pitch_vals.get(key, 0) == 0:
            return          # already centred -- nothing to do
        self._pitch_vals[key] = 0
        port, ch = key
        self._send_pitch(port, ch, 0)
        self._oled_pitch(port, ch, 0)

    def _oled_pitch(self, port, ch, val):
        """OLED feedback for a bend knob (mirrors _oled_cc's layout)."""
        if not fire_oled:
            return
        try:
            fire_oled.show_lines([
                ("PITCH BEND", 2, 0, 1),
                (f"{str(port)[:7]} {self._ch_label(ch)}", 2, 22, 1),
                (f"bend = {val:+d}", 2, 42, 1)])
        except Exception:
            pass

    def _mpc_cc_send(self, midi_ch, val):
        """Send CC8 on midi_ch (1-indexed) via the engine send_to path."""
        send = getattr(self.eng, "send_to", None)
        if mido and send:
            try:
                send("pisound", mido.Message("control_change",
                                              channel=midi_ch - 1, control=8,
                                              value=int(val)))
            except Exception:
                pass


    def _oled_cc(self, port, ch, cc, val):
        if not fire_oled:
            return
        try:
            # GB wave voice: name the waveform / volume live as the knob turns,
            # instead of an anonymous "cc1 = 72" (see _select_chip_voice: the
            # wave voice puts waveform on CC1 and 4-step volume on CC7). Decode
            # the scaled value so the label matches what the synth hears.
            if self.view == "chip" and self.chip_voice == "wave" and cc in (1, 7):
                sval = self._scale_cc(cc, val)
                if cc == 1:
                    line2 = f"WAVE {GB_WAVE_NAMES[min(7, sval // 16)]}"
                else:
                    line2 = f"VOL  {GB_VOL_LABELS[min(3, sval * 4 // 128)]}"
                fire_oled.show_lines([
                    ("GB WAVE", 2, 0, 2),
                    (line2, 2, 30, 1)])
                return
            fire_oled.show_lines([
                ("CC SEND", 2, 0, 1),
                (f"{str(port)[:7]} {self._ch_label(ch)}", 2, 22, 1),
                (f"cc{cc} = {val}", 2, 42, 1)])
        except Exception:
            pass

    # -- Parameter scaling -----------------------------------------------------

    def _scale_param(self, param, val):
        """Apply the configured curve to a 0-127 internal value before sending.
        "log"  (pow 0.4): more resolution at low end -- good for volume/envelopes.
        "exp"  (pow 2.5): more resolution at high end -- rarely needed.
        "linear" (default): no transformation.
        """
        mode = self._param_scaling.get(param, "linear")
        return self._apply_curve(val, mode)

    def _scale_cc(self, cc, val):
        """Same as _scale_param but keyed by CC number (int) from CC_SCALING."""
        mode = self._cc_scaling.get(int(cc), "linear")
        return self._apply_curve(val, mode)

    @staticmethod
    def _apply_curve(val, mode):
        if val <= 0:
            return 0
        if val >= 127:
            return 127
        t = val / 127.0
        if mode == "log":
            return round(127 * t ** 0.4)
        if mode == "exp":
            return round(127 * t ** 2.5)
        return val   # linear

    def _voice_has_param(self, voice, param):
        """True if `param` is addressable for `voice` under the data-driven CC
        map (each voice declares exactly its params in cfg.DRUM_CC["LAYOUT"]).
        Toms have no filter; clap has filter_freq but not filter_res; the HP band
        is gone entirely. Lets the editor avoid moving a phantom value for a knob
        that can't reach the synth."""
        # The cymbal's low-pass was removed 2026-08-17 (dark sample + the SVF
        # goes unstable at high cutoffs; Cymbal.set_param now ignores the filter
        # CCs). The CC MAP still carries filter_freq/filter_res for the cymbal --
        # dropping them there would renumber cowbell's CCs and could break
        # outboard/MPC mappings -- so suppress them at the EDITOR level: the
        # sculpt screen shows "--" and the knob does nothing, instead of a value
        # that can't change anything.
        if voice == "cymbal" and param in ("filter_freq", "filter_res"):
            return False
        return param in self._DRUM_LAYOUT.get(voice, {}).get("params", ())

    # -- Live voice sculpting: a knob edits one param of the selected voice ----
    def _dial_voice_param(self, idx, step, param):
        if not self._edit_voices:
            return
        voice = self._edit_voices[self._sel_voice]
        # Skip params the voice can't address (e.g. toms have no filter) so the
        # OLED value doesn't climb while the knob silently does nothing.
        if not self._voice_has_param(voice, param):
            self._oled_sculpt(voice)
            return
        cur = self._voice_vals[voice].get(param, 64)
        cur = max(0, min(127, cur + step * 3))
        self._voice_vals[voice][param] = cur
        self._send_drum_param(voice, param, self._scale_param(param, cur))
        self._oled_sculpt(voice)

    def _send_drum_param(self, voice, param, value):
        """Encode a drum voice param to its CC via cfg.DRUM_CC["LAYOUT"] (voice
        packed to exactly its params from BASE) and send it on CH_DRUM."""
        if not mido:
            return
        d = self._DRUM_LAYOUT.get(voice)
        if not d or param not in d["params"]:
            return
        cc = int(d["start"]) + d["params"].index(param)
        if cc > 127:
            return
        ch0 = int(self.cfg.SYNTH.get("CH_DRUM", 10)) - 1
        try:
            self.eng.send_synth_midi(mido.Message(
                "control_change", channel=ch0, control=cc, value=int(value)))
        except Exception:
            pass

    def _oled_sculpt(self, voice):
        if not fire_oled:
            return
        try:
            vv = self._voice_vals.get(voice, {})
            # Parameter labels vary per voice after the 909 redesign.
            # Row format: (atk_label, dec_label, tun_label, lp_label, q_label)
            _labels = {
                "bass_drum": ("clk", "dec", "tun", "drv", "sub"),
                "snare":     ("snp", "dec", "tun", "frq", "len"),
            }
            atk, dec, tun, lp, q = _labels.get(voice, ("atk", "dec", "tun", "lp", "q"))
            # Voices without a given filter param (toms have neither; clap has
            # cutoff but no resonance) show "--" instead of a phantom value the
            # knob can't move. cowbell now has a real filter (CC space freed).
            _has_lp = self._voice_has_param(voice, "filter_freq")
            _has_q  = self._voice_has_param(voice, "filter_res")
            lp_str = f"{lp}{vv.get('filter_freq',0):>3}" if _has_lp else f"{lp} --"
            q_str  = f"{q}{vv.get('filter_res',0):>3}"  if _has_q  else f"{q} --"
            fire_oled.show_lines([
                (fire_oled.fit("EDIT: " + voice.upper(), 2, 1, why="drum voice name"), 2, 0, 1),
                (f"{atk}{vv.get('attack',0):>3} {dec}{vv.get('decay',0):>3}",
                 2, 17, 1),
                (f"{tun}{vv.get('tune',0):>3} lvl{vv.get('level',0):>3}",
                 2, 33, 1),
                (f"{lp_str} {q_str}", 2, 48, 1)])
        except Exception:
            pass

    # ==========================================================================
    # SELECT ACTIONS  (_sel_<name>(step, *args))
    # ==========================================================================

    def _sel_scroll_voice(self, step):
        # Drum-voice scrolling + the "EDIT: <voice>" sculpt OLED only make
        # sense in the drum sequencer view (the seq kit IS the drum voices).
        # Outside seq -- e.g. ALT+SELECT-turn while in chord/poly/chip/fx --
        # this used to paint the drum EDIT screen and, because the OLED only
        # repaints on the next event, it stuck there ("menu reverts to drum
        # voices no matter where I am"). Gate it to seq so it can't hijack the
        # display elsewhere; do nothing in any other view. (2026-07-22)
        if self.view != "seq":
            return
        if not self._edit_voices:
            return
        self._sel_voice = (self._sel_voice + step) % len(self._edit_voices)
        self._oled_sculpt(self._edit_voices[self._sel_voice])

    def _sel_scroll_pattern(self, step):
        arp = self.eng.arp
        if not arp:
            return
        import orc1_patterns as pat
        order = pat.PATTERN_ORDER
        idx = order.index(arp.pattern_name) if arp.pattern_name in order else 0
        new_name = order[(idx + step) % len(order)]
        arp.set_pattern(new_name, pat.PATTERNS)
        # Always flash the new pattern name: the persistent chord OLED only
        # shows "A:name" while the arp is ENABLED, so changing patterns with the
        # arp off used to give zero feedback ("pattern feels intermittent").
        self._oled_flash("PATTERN", new_name)

    def _sel_scroll_tracks(self, step):
        self._scroll_tracks(step)

    # Views where SELECT-turn falls back to arp-pattern scrolling. These are
    # the ones you PLAY -- having the arp pattern under the encoder while your
    # hands are on the keyboard is the point.
    #
    # 2026-07-31: this used to be "any view that is not seq/mixer", which
    # silently included FADERS and UNIVERSAL. Those are assignable MIDI
    # control surfaces with no relationship to the arp at all, so turning
    # SELECT there changed the arpeggiator out from under you with no
    # on-screen sign it had happened -- reported from the field as "turning
    # the select encoder from the main screen of the universal control mode
    # changes the pattern of the arpeggiator". Naming the views that opt IN
    # keeps a future view from inheriting the same surprise by default.
    _SEL_ARP_FALLBACK_VIEWS = ("chord", "poly", "chip")

    def _sel_scroll_track(self, step):
        """Move the selected-track cursor (channel/mixer views), keeping it
        inside the 4-row viewport. In keyboard/perform views, fall back to
        pattern scroll so the encoder still drives the arp there; in every
        other view SELECT-turn does nothing."""
        if self.view not in ("seq", "mixer") or not self.n_tracks:
            if self.view in self._SEL_ARP_FALLBACK_VIEWS:
                return self._sel_scroll_pattern(step)
            return
        self._sel_track = max(0, min(self.n_tracks - 1, self._sel_track + step))
        if self._sel_track < self.view_top:
            self.view_top = self._sel_track
        elif self._sel_track >= self.view_top + N_ROWS:
            self.view_top = self._sel_track - N_ROWS + 1
        self._sync_voice_to_track()
        self._render()
        self._oled()

    def _sync_voice_to_track(self):
        """Auto-switch the drum voice knob target to match the selected track."""
        if self._sel_track >= self.n_tracks:
            return
        note = self.tracks[self._sel_track].get("note", 0)
        vidx = self._note_to_voice_idx.get(note)
        if vidx is not None:
            self._sel_voice = vidx

    # -- Helpers ---------------------------------------------------------------

    def _scroll_tracks(self, delta):
        maxtop = max(0, self.n_tracks - N_ROWS)
        self.view_top = max(0, min(maxtop, self.view_top + delta))
        self._render()
        self._oled()

    def _reorder_track(self, step):
        """Swap the selected track one position up or down (SHIFT+SELECT turn).
        2026-07-01: routed through seq.reorder_track (lock-guarded, Bug B)."""
        if not self.n_tracks:
            return
        idx     = self._sel_track
        new_idx = self.seq.reorder_track(idx, idx + step)
        if new_idx is None or new_idx == idx:
            return
        self._sel_track = new_idx
        # Keep the moved track inside the visible viewport.
        if self._sel_track < self.view_top:
            self.view_top = self._sel_track
        elif self._sel_track >= self.view_top + N_ROWS:
            self.view_top = self._sel_track - N_ROWS + 1
        self._render()
        self._oled()

    def _retrigger(self):
        eng = self.eng
        st = eng.state
        held = (st.get("trigger_note") is not None) or (
            eng.arp and eng.arp.enabled and eng.arp.current_chord)
        if held:
            mirror = self.cfg.NOTE_INPUT_CH if eng.mirror_active() else None
            eng.play_chord(mirror_ch=mirror)

    # ==========================================================================
    # SEQUENCER CLOCK
    # ==========================================================================

    def clock_tick(self):
        """Delegate external MIDI clock pulse to the Sequencer."""
        self.seq.clock_tick()

    def _render_thread(self):
        """Repaint off the timing-critical path: immediately on edits (dirty)
        and, while running, at ~25fps for a smooth playhead -- so LED SysEx
        never delays note triggering."""
        _last_render_err_log = 0.0
        while not self._stop.is_set():
            dirty = self._render_dirty.wait(0.04)
            self._render_dirty.clear()
            if dirty or self.seq.running:
                try:
                    self._render()
                except Exception as e:
                    # This loop is the ONLY thing that repaints the LED grid,
                    # so a swallowed exception here used to freeze it silently
                    # forever with zero trace. Log it, rate-limited (this can
                    # run at ~25fps) so a wedged render surfaces without
                    # spamming the journal every frame.
                    now = time.monotonic()
                    if now - _last_render_err_log > 5.0:
                        print(f"  ! Fire _render_thread: {e}", flush=True)
                        _last_render_err_log = now

    def close(self):
        self._stop.set()
        # Chip/GB expansion (2026-07-20): cancel any pending pitch-bend
        # spring-back timers so they can't fire into a closed handler
        # (the _stop guard in _pitch_recenter is the second line of defence).
        for _t in list(getattr(self, "_pitch_timers", {}).values()):
            try:
                _t.cancel()
            except Exception:
                pass
        # NOTE: do NOT call self.seq.close() here. Since the 2026-07-01 refactor
        # the seq is owned by the engine, not the Fire. Closing it on Fire
        # teardown/hotplug would set seq._stop and permanently wedge the shared
        # transport. (P1.9c fix, 2026-07-20)


# ==============================================================================
# DEFAULTS (used if orc1_config.py has no FIRE section)
# ==============================================================================

_DEFAULT_TRACKS = [
    {"name": "kick",  "note": 36, "ch": 6, "vel": 115, "length": 16},
    {"name": "snare", "note": 38, "ch": 6, "vel": 110, "length": 16},
    {"name": "chat",  "note": 42, "ch": 6, "vel": 95,  "length": 12},
    {"name": "clap",  "note": 39, "ch": 6, "vel": 105, "length": 16},
    {"name": "bass",  "note": 48, "ch": 7, "vel": 100, "length": 15},
]

_DEFAULT_BUTTONS = {
    "STEP":    ("toggle_view", "universal", "seq"),
    "PERFORM": ("view", "chord"),
    "MODE":    ("cycle_view",),
    "DRUM":    ("toggle_view", "seq", "mixer"),
    "PLAY":    ("seq_play",),
    "STOP":    ("seq_stop",),
    "REC":     ("seq_clear",),
    "SHIFT":   ("shift",),
    "ALT":     ("alt",),
    "PATTERN_UP":   ("scroll_tracks", -1),
    "PATTERN_DOWN": ("scroll_tracks", +1),
    "GRID_LEFT":    ("page", -1),
    "GRID_RIGHT":   ("page", +1),
    "NOTE":     ("note_btn",),
    "PAT_SONG": ("fx_view",),
    "BROWSER":      ("browser",),         # POLY view; ALT+BROWSER = knob map
    "SELECT_PRESS": ("select_press",),    # confirm in knob-map mode
}

# Scrollable menu of things a knob can be mapped to (label, dial-action). Each
# action must name a real _dial_<action> method. "none" parks a knob.
_DEFAULT_KNOB_TARGETS = [
    ("attack",  ("voice_param", "attack")),
    ("decay",   ("voice_param", "decay")),
    ("tune",    ("voice_param", "tune")),
    ("level",   ("voice_param", "level")),
    ("lpfreq",  ("voice_param", "filter_freq")),
    ("lpreso",  ("voice_param", "filter_res")),
    # hpfreq/hpreso removed 2026-07-21 — the per-voice HP band is gone, so those
    # targets were inert no-ops (guarded by _voice_has_param).
    ("bpm",     ("bpm",)),
    ("trkvel",  ("track_vel",)),
    # "trklen"/track_length removed Session E (2026-06-30) -- no longer a valid
    # remap target; track length is set ONLY by ALT+step pad now.
    ("trknote", ("track_note",)),     # selected track's MIDI note
    ("trkchan", ("track_channel",)),  # selected track's MIDI channel
    ("spread",  ("spread",)),
    ("octave",  ("oct_offset",)),
    # Outboard / internal CC sends -- ("cc_out", port, ch, cc). "synth" hits the
    # internal bus; "pisound" goes out the DIN to external gear.
    ("synvol",  ("cc_out", "synth", "poly", 13)),   # poly master volume
    ("chipvol", ("cc_out", "synth", "chip_p1", 13)),   # chip master volume
    ("synwav",  ("cc_out", "synth", "poly", 1)),   # poly waveform select
    ("synflt",  ("cc_out", "synth", "poly", 74)),   # poly filter cutoff
    ("synres",  ("cc_out", "synth", "poly", 71)),   # poly filter resonance
    ("synmode", ("cc_out", "synth", "poly", 70)),   # poly filter LP/HP/BP
    ("synoct",  ("cc_out", "synth", "poly", 75)),   # poly octave
    ("synrel",  ("cc_out", "synth", "poly", 11)),   # poly release
    ("chipwav", ("cc_out", "synth", "chip_p1", 1)),   # chip timbre: duty/tri/noise
    ("chipflt", ("cc_out", "synth", "chip_p1", 74)),   # chip filter cutoff
    ("chipoct", ("cc_out", "synth", "chip_p1", 75)),   # chip octave
    ("dincut",  ("cc_out", "pisound", 10, 74)),  # DIN cutoff to ch10 gear
    ("modwhl",  ("cc_out", "pisound", 10,  1)),  # DIN mod wheel
    # -- Chip/GB expansion (2026-07-20): bend / vibrato / sweep targets ------
    # ("pitch_out", port, ch) = springy pitch-bend knob (see _dial_pitch_out);
    # internal targets NAME the module (resolved live from the /matrix
    # bindings at send time, 2026-09-24) -- p2 used to say ch 10, long stale.
    ("p1bend",  ("pitch_out", "synth", "chip_p1")),     # Pulse 1 pitch bend
    ("p2bend",  ("pitch_out", "synth", "chip_p2")),     # Pulse 2 pitch bend
    ("wvbend",  ("pitch_out", "synth", "gb_wave")),     # GB Wave pitch bend
    ("p1vibd",  ("cc_out", "synth", "chip_p1", 77)),    # Pulse 1 vibrato depth
    ("p1vibr",  ("cc_out", "synth", "chip_p1", 78)),    # Pulse 1 vibrato rate
    ("p2vibd",  ("cc_out", "synth", "chip_p2", 77)),    # Pulse 2 vibrato depth
    ("p2vibr",  ("cc_out", "synth", "chip_p2", 78)),    # Pulse 2 vibrato rate
    ("wvvibd",  ("cc_out", "synth", "gb_wave", 77)),    # GB Wave vibrato depth
    ("wvvibr",  ("cc_out", "synth", "gb_wave", 78)),    # GB Wave vibrato rate
    ("swptim",  ("cc_out", "synth", "chip_p1", 79)),    # P1 sweep time (0 = off)
    ("swpsft",  ("cc_out", "synth", "chip_p1", 80)),    # P1 sweep shift/depth
    ("swpdir",  ("cc_out", "synth", "chip_p1", 81)),    # P1 sweep dir (up/down)
    ("none",    ("none",)),
]

# Parameters shown in the BROWSER scroll menu.
# Session E (2026-06-30): "len"/Len removed from both lists -- track length
# is set ONLY by ALT+step pad now, not via the BROWSER param scroll menu.
_DRUM_BROWSE_PARAMS = [
    ("voice", "Voice"),   # scrolls drum/sample voices by name
    ("ch",    "Ch"),
    ("vel",   "Vel"),
]
_NOTE_BROWSE_PARAMS = [
    ("note",  "Note"),    # MIDI note number + name  e.g. C4 (60)
    ("mode",  "Mode"),    # note | chord
    ("ch",    "Ch"),
    ("vel",   "Vel"),
]

_DEFAULT_EDIT_VOICES = [
    "bass_drum", "snare", "closed_hat", "open_hat", "clap",
    "rimshot", "low_tom", "mid_tom", "hi_tom", "cymbal", "cowbell",
]


def _fire_fallback_drum_layout(base):
    """Mirror of cfg._build_drum_cc_layout — only used if the computed
    DRUM_CC["LAYOUT"] is missing. Returns {voice: {"start": cc, "params": [...]}}
    by packing each voice's params consecutively from `base`."""
    vp = {
        "bass_drum":  ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "snare":      ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "low_tom":    ["level", "tune", "attack", "decay"],
        "mid_tom":    ["level", "tune", "attack", "decay"],
        "hi_tom":     ["level", "tune", "attack", "decay"],
        "rimshot":    ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "clap":       ["level", "tune", "attack", "decay", "filter_freq"],
        "closed_hat": ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "open_hat":   ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "cymbal":     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "cowbell":    ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
    }
    layout, cc = {}, int(base)
    for v, ps in vp.items():
        layout[v] = {"start": cc, "params": list(ps)}
        cc += len(ps)
    return layout

# ==============================================================================
# PERFORM VIEW -- ORCHID CHORD-ENGINE CONTROL LAYOUT
# ==============================================================================
#
# The 64 pads are split into two zones:
#
#   ┌----------------------------------------------------------------------┐
#   │ cols 0-3 (CONTROL)  │  cols 4-15 (KEYBOARD -- 4 chromatic octaves)   │
#   │  MAJ  MIN  DIM  SUS │  C  C# D  D# E  F  F# G  G# A  A# B  (top)  │
#   │   6   m7   M7    9  │  C  C# D  D# E  F  F# G  G# A  A# B         │
#   │  INV- INV+  ·    · │  C  C# D  D# E  F  F# G  G# A  A# B         │
#   │  CLR  PREV HOLD  ARP│  C  C# D  D# E  F  F# G  G# A  A# B  (bot)  │
#   └----------------------------------------------------------------------┘
#
# Each row in the 4-col control section:
#   Row 0 -- chord bases:  MAJ MIN SUS DIM
#   Row 1 -- extensions:   6  m7  M7   9   (sticky toggles; press to add/remove)
#   Row 2 -- modifiers:    INV-  INV+   --   --   (cols 2-3 freed 2026-09-03;
#            the old OCT-/OCT+ pads were redundant with PAT up/down and badly
#            placed. Octave now lives ONLY on PAT; GRID <> flips row banks.)
#   Row 3 -- utility:      CLR   PREV  HOLD  ARP
#
# Each control entry is (kind, param).
_PERF_CTRL_LAYOUT = [
    # row 0: chord bases -- press to activate; lights in that base's colour
    [("base","MAJ"), ("base","MIN"), ("base","SUS"), ("base","DIM")],
    # row 1: extensions -- MOMENTARY.  Plain = the scale's own 6th/7th/9th/11th;
    # SHIFT = the chromatic 6/m7/M7/9 escape hatch (see _PERF_EXT_CHROM).
    [("ext","6th"),  ("ext","7th"),  ("ext","9th"),  ("ext","11th")],
    # row 2: inversion sweep; cols 2-3 are intentionally dark/unassigned.
    [("mod","INV_DOWN"), ("mod","INV_UP"), ("none",None), ("none",None)],
    # row 3: utility buttons
    [("clear",None), ("prev",None), ("hold",None), ("arp",None)  ],
]

# Per-base pad colours (R,G,B) in 0-127 Fire range.
_PERF_BASE_BRIGHT = {
    "MAJ": (0x50, 0x20, 0x00),   # warm gold
    "MIN": (0x00, 0x12, 0x50),   # blue
    "SUS": (0x00, 0x45, 0x18),   # green
    "DIM": (0x28, 0x00, 0x38),   # purple
    # -- O3 (2026-07-29): DIAT derives the shape from the scale, so
    # effective_base() can now return names that are NOT base-pad names.  Given
    # colours here so the keyboard's root tint stays meaningful instead of
    # falling back to flat grey.  Hues are deliberately near their triad family.
    "AUG":    (0x48, 0x00, 0x2A),   # magenta -- the shape the 4 fixed ones lacked
    "SUS2":   (0x00, 0x40, 0x28),
    "6no5":   (0x40, 0x28, 0x00),
    "SUS4/7": (0x10, 0x40, 0x10),
    "maj7":   (0x48, 0x2C, 0x00),
    "7":      (0x50, 0x14, 0x00),
    "m7":     (0x00, 0x1C, 0x48),
    "m7b5":   (0x1C, 0x00, 0x40),
    "dim7":   (0x24, 0x00, 0x34),
    "mMaj7":  (0x10, 0x18, 0x48),
    "+Maj7":  (0x40, 0x00, 0x30),
    "aug7":   (0x44, 0x08, 0x24),
    "6":      (0x44, 0x24, 0x00),
}

# Extension pad vocabularies, by COLUMN.
# Plain press  -> the scale's own ladder (quality decided by the scale).
# SHIFT press  -> the historical chromatic offsets, so the m7/M7 distinction and
#                 every pre-O3 muscle memory stay reachable.  The two layers
#                 deliberately do NOT line up column-for-column: the SHIFT row is
#                 preserved exactly as it always was.
_PERF_EXT_DEGREE = ["6th", "7th", "9th", "11th"]
_PERF_EXT_CHROM  = ["6",   "m7",  "M7",  "9"]
_PERF_BASE_DIM = {k: (max(1,v[0]//6), max(1,v[1]//6), max(1,v[2]//6))
                  for k,v in _PERF_BASE_BRIGHT.items()}

def _perf_ctrl_color(kind, param, chord_base, exts, hold, arp_on):
    """Return (R,G,B) for a single perform-view control pad."""
    if kind == "base":
        return _PERF_BASE_BRIGHT[param] if chord_base == param \
               else _PERF_BASE_DIM.get(param, (6,3,0))
    if kind == "ext":
        # A column lights for EITHER of its two vocabularies, in different
        # colours, so you can see at a glance that an extension is deliberately
        # off-scale (chromatic) rather than following the scale.
        try:
            col = _PERF_EXT_DEGREE.index(param)
        except ValueError:
            return (50, 25, 0) if param in exts else (6, 3, 0)
        if _PERF_EXT_CHROM[col] in exts:
            return (55, 0, 30)        # magenta = chromatic (off-scale) ext
        if _PERF_EXT_DEGREE[col] in exts:
            return (50, 25, 0)        # amber = scale-degree ext
        return (6, 3, 0)
    if kind == "mod":
        return (0, 18, 25)    # dim teal for inversion controls
    if kind == "none":
        return (0, 0, 0)      # freed pad -- dark
    if kind == "clear":
        return (38, 0, 0)     # dim red
    if kind == "prev":
        return (0, 12, 20)    # dim blue-teal
    if kind == "hold":
        return (55, 18, 0) if hold else (10, 4, 0)
    if kind == "arp":
        return (0, 55, 20) if arp_on else (0, 10, 4)
    return (5, 5, 5)


# Knobs sculpt the ALT+rotary-selected drum voice.
_DEFAULT_DIALS = {
    0: ("voice_param", "attack"),
    1: ("voice_param", "decay"),
    2: ("voice_param", "tune"),
    3: ("voice_param", "level"),
}
_DEFAULT_DIALS_SHIFT = {
    0: ("voice_param", "filter_freq"),
    1: ("voice_param", "filter_res"),
    2: ("voice_param", "tune"),
    3: ("voice_param", "level"),
}
_DEFAULT_DIALS_ALT = {
    0: ("bpm",),
    1: ("track_vel",),
    # knob 2: deliberately unassigned -- track length is set via ALT+pad press,
    # which is far more precise and much harder to misfire accidentally.
    3: ("spread",),
    2: ("oct_offset",),  # ALT+encoder3: octave transpose
}
