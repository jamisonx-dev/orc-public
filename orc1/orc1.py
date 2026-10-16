#!/usr/bin/env python3
"""
ORC-1 Chord Engine -- Raspberry Pi / Pisound  v8


CHANNEL ARCHITECTURE:
  CH 12  -- note input -> chord engine -> chord output
  CH 13  -- control channel, consumed entirely, never forwarded
  CH 15  -- melody snap input -> scale/chord locked -> melody output
  CH 10  -- always passthrough (drums)
  CH 14  -- always passthrough (MPC control surface, sacred)
  all other channels -> pure passthrough, untouched


DIN vs USB:
  Pisound DIN in -> dedicated relay thread -> DIN out (pure passthrough)
  USB inputs     -> chord engine + melody engine


Edit ~/orc1_config.py and ~/orc1_patterns.py. Restart to apply.
"""


import mido
import time
import threading
import sys
import os
import json
from collections import deque


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_config   as cfg
import orc1_patterns as pat
import orc1_pedal    as _orc1_pedal   # foot-pedal recognition + gesture layer (stdlib only)
import orc1_padmap   as _orc1_padmap  # per-device pad maps (/pads), stdlib only
import orc1_surface_link as _orc1_surface_link  # paints control SURFACES (our Fire firmware) from the pad maps
import orc1_cme      as _orc1_cme     # CME U6MIDI Pro config over SysEx (stdlib; mido lazily)
import orc1_kikpad   as _orc1_kikpad  # KIKPAD bank 2 = sampler + skipback surface (stdlib; mido lazily)
import orc1_kikpad_map as _orc1_kikpad_map  # the KIKPAD's own pad/encoder maps, edited from /kikpad (stdlib)
import orc1_playstate as _orc1_playstate  # live play-state feed for the HUD / kiosk (stdlib only)
from orc1_arp import ArpEngine, CLOCK_EXTERNAL, CLOCK_INTERNAL, CLOCK_DRIFT, PPQN
from orc1_arp import SUBDIVISION_LIST, ARP_SILENT
from orc1_routes import RoutingEngine, build_consumed_set

try:
    from orc1_mixtrack import MixtrackHandler
    _MIXTRACK_AVAILABLE = True
except ImportError:
    _MIXTRACK_AVAILABLE = False

try:
    from orc1_fire import FireHandler
    _FIRE_AVAILABLE = True
except Exception:
    _FIRE_AVAILABLE = False

try:
    from orc1_fx import FXManager
    _FX_AVAILABLE = True
except ImportError:
    _FX_AVAILABLE = False

try:
    import orc1_fx_inserts as _fx_inserts_mod   # arbitrary per-source wet/dry inserts
    _FX_INSERTS_AVAILABLE = True
except Exception:
    _FX_INSERTS_AVAILABLE = False

_FX_IO_FILE = os.path.expanduser("~/.orc1_fx_io.json")

def _read_fx_io_state():
    """Return current FX I/O mode + ports for the state snapshot."""
    defaults = {
        "mode":     "hardware",
        "send_l":   "pisound-in:capture_1",
        "send_r":   "pisound-in:capture_2",
        "return_l": "pisound-out:playback_1",
        "return_r": "pisound-out:playback_2",
    }
    try:
        with open(_FX_IO_FILE) as _f:
            defaults.update(json.load(_f))
    except Exception:
        pass
    return defaults


# -- Chord / scale tables ------------------------------------------------------


CHORD_BASE = {
    "MAJ": [0, 4, 7],
    "MIN": [0, 3, 7],
    "SUS": [0, 5, 7],
    "DIM": [0, 3, 6],
}
CHORD_BASE_NAMES = ["MAJ", "MIN", "SUS", "DIM"]


CHORD_EXTS = {
    "6":  9,
    "m7": 10,
    "M7": 11,
    "9":  14,
}
CHORD_EXT_NAMES = ["6", "m7", "M7", "9"]


# -- Scale-degree extensions (2026-07-29, O3) ----------------------------------
# In DIAT mode an extension is another rung of the scale's own ladder, so the
# SCALE decides whether the 7th is minor, major or diminished -- see
# degree_intervals().  Values are SCALE STEPS from the chord root's degree, not
# semitones: stacking thirds is +2 per rung, so the 7th is step 6 and the 9th is
# step 8.  The 6th is step 5, which is not a stacked third -- it is included
# because a 6th is a common colour and there is a pad for it.
#
# CHORD_EXTS (above) is the CHROMATIC escape hatch and is unchanged: those are
# fixed semitone offsets that deliberately ignore the scale.  Both name sets are
# valid in state["chord_exts"] simultaneously and are resolved per-name in
# get_intervals(), which is what keeps old saved state and the CC maps working.
CHORD_EXT_DEGREES = {
    "6th":  5,
    "7th":  6,
    "9th":  8,
    "11th": 10,
}
CHORD_EXT_DEGREE_NAMES = ["6th", "7th", "9th", "11th"]

# Fallback semitones for a degree-extension used while there is no usable scale
# ladder (FIXED mode, or an unknown scale).  Chosen to match the plainest reading
# of each name so a degree ext is never silently DROPPED -- it just stops being
# scale-aware, which is the same tolerance the fixed path has always had.
CHORD_EXT_DEGREE_FALLBACK = {
    "6th":  9,
    "7th":  10,
    "9th":  14,
    "11th": 17,
}


# SCALE_NAMES is APPEND-ONLY.  CTRL_CC_MAP binds CC 10-19 to ("SCALE", index)
# against this list, so reordering it would silently re-point those CCs at
# different scales.  New scales go on the end; the musical grouping the UI shows
# lives in SCALE_GROUPS below, which is free to be ordered however reads best.
SCALE_NAMES = ["MAJ","MIN","HMIN","MMIN","DOR","PHR","LYD","MIX","LOC","CHROM",
               # -- appended 2026-07-28 --
               "MAJ5","MIN5","BLUES","HIRA","INSEN","KUMOI",
               "WHOLE","DIMHW","DIMWH",
               "HMAJ","PHRD","LYDD","ALT","HUNG","DBLH"]


SCALES = {
    "MAJ":  [0, 2, 4, 5, 7, 9, 11],
    "MIN":  [0, 2, 3, 5, 7, 8, 10],
    "HMIN": [0, 2, 3, 5, 7, 8, 11],   # harmonic minor -- raised 7th
    "MMIN": [0, 2, 3, 5, 7, 9, 11],   # melodic minor -- raised 6th and 7th
    "DOR":  [0, 2, 3, 5, 7, 9, 10],
    "PHR":  [0, 1, 3, 5, 7, 8, 10],
    "LYD":  [0, 2, 4, 6, 7, 9, 11],
    "MIX":  [0, 2, 4, 5, 7, 9, 10],
    "LOC":  [0, 1, 3, 5, 6, 8, 10],
    "CHROM":list(range(12)),

    # -- Pentatonics: 5 notes, no semitone clashes.  With SCALE SNAP on these
    #    make it genuinely impossible to play a sour note.
    "MAJ5": [0, 2, 4, 7, 9],          # major pentatonic
    "MIN5": [0, 3, 5, 7, 10],         # minor pentatonic
    "BLUES":[0, 3, 5, 6, 7, 10],      # minor pentatonic + b5 blue note
    "HIRA": [0, 2, 3, 7, 8],          # hirajoshi (Japanese)
    "INSEN":[0, 1, 5, 7, 10],         # in sen (Japanese)
    "KUMOI":[0, 2, 3, 7, 9],          # kumoi (Japanese)

    # -- Symmetric: no tonal centre, every degree sounds the same.
    "WHOLE":[0, 2, 4, 6, 8, 10],      # whole tone
    "DIMHW":[0, 1, 3, 4, 6, 7, 9, 10],# half-whole diminished (dominant dim)
    "DIMWH":[0, 2, 3, 5, 6, 8, 9, 11],# whole-half diminished

    # -- Seven-note colours.
    "HMAJ": [0, 2, 4, 5, 7, 8, 11],   # harmonic major -- major with b6
    "PHRD": [0, 1, 4, 5, 7, 8, 10],   # phrygian dominant (Spanish / freygish)
    "LYDD": [0, 2, 4, 6, 7, 9, 10],   # lydian dominant (acoustic / overtone)
    "ALT":  [0, 1, 3, 4, 6, 8, 10],   # altered / super locrian
    "HUNG": [0, 2, 3, 6, 7, 8, 11],   # hungarian (gypsy) minor
    "DBLH": [0, 1, 4, 5, 7, 8, 11],   # double harmonic / byzantine
}


# How the scale list is PRESENTED (Fire overlay rows, and the order PAT-up/down
# cycles through).  Grouped so neighbouring scales are musically related instead
# of in historical-append order.  Every name here must exist in SCALES; anything
# in SCALES but missing here still works, it just won't appear on the pad grid.
SCALE_GROUPS = [
    ("MODES",  ["MAJ", "DOR", "PHR", "LYD", "MIX", "MIN", "LOC", "CHROM"]),
    ("PENTA",  ["MAJ5", "MIN5", "BLUES", "HIRA", "INSEN", "KUMOI",
                "WHOLE", "DIMHW", "DIMWH"]),
    ("COLOUR", ["HMIN", "MMIN", "HMAJ", "PHRD", "LYDD", "ALT", "HUNG", "DBLH"]),
]

# Flat presentation order, used for PAT-up/down cycling.
SCALE_ORDER = [s for _label, group in SCALE_GROUPS for s in group]


# DIATONIC_BASE was DELETED 2026-07-29 (O3).  It mapped each scale degree onto
# one of only four fixed shapes (MAJ/MIN/SUS/DIM), which is a labelling exercise,
# not harmonisation -- build_chord_notes() rendered that fixed shape on the root
# and never looked at the scale.  Measured cost of that approach: 5 of 91
# laddered degrees sounded a note OUTSIDE their own scale (HMIN 3, MMIN 3,
# PHRD 6, LYDD 7, ALT 4), HUNG and DBLH had to be excluded entirely, and the
# 11 pentatonic/symmetric scales had no ladder at all.
#
# degree_intervals() replaces it by stacking the scale's OWN degrees, which is
# correct on all 25 scales by construction (verified: 2040 chords across every
# scale x key x degree, 0 outside their scale) and needs no label to play a
# chord.  Names are now only for DISPLAY -- see CHORD_SHAPE_NAMES.


# Display-only names for stacked shapes, keyed by the intervals from the chord
# root.  O3 plays notes and never needs a name; the OLED and the web do.
# ANYTHING NOT IN HERE IS SHOWN AS A DEGREE (e.g. "F# IV"), never as a wrong
# name -- guessing the closest label is exactly the mistake DIATONIC_BASE made.
CHORD_SHAPE_NAMES = {
    (0, 4, 7):  "MAJ",
    (0, 3, 7):  "MIN",
    (0, 5, 7):  "SUS",
    (0, 3, 6):  "DIM",
    (0, 4, 8):  "AUG",     # the shape the 4 fixed ones could never express
    (0, 2, 7):  "SUS2",
    (0, 4, 9):  "6no5",
    (0, 5, 10): "SUS4/7",
    # four-note stacks (a 7th rung added)
    (0, 4, 7, 11): "maj7",
    (0, 4, 7, 10): "7",
    (0, 3, 7, 10): "m7",
    (0, 3, 6, 10): "m7b5",
    (0, 3, 6, 9):  "dim7",
    (0, 3, 7, 11): "mMaj7",
    (0, 4, 8, 11): "+Maj7",
    (0, 4, 8, 10): "aug7",
    (0, 4, 7, 9):  "6",
}

# Roman numerals for the degree readout (index = scale degree, 0-based).
ROMAN_DEGREES = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]


ROOT_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]


# -- Engine state --------------------------------------------------------------
state = {
    "chord_base":      "MAJ",
    # True while a MAJ/MIN/SUS/DIM pad press is overriding the diatonic
    # quality; cleared by the next keyboard press.  See effective_base().
    "base_override":   False,
    "chord_exts":      set(),
    "preset":          cfg.DEFAULT_PRESET,
    "last_multi":      cfg.DEFAULT_PRESET,
    "root_mode":       False,
    "input_ch":        cfg.NOTE_INPUT_CH,
    # "root" is the KEY TONIC.  It is moved only deliberately (KEY overlay, web,
    # ROOT_SET, CC/PC) and is NEVER written by playing the keyboard -- that is
    # what "chord_root" below is for.  Splitting the two (2026-07-29, O3) fixed a
    # real bug: the degree lookup read this variable as an offset from C while
    # scale_pitch_classes() read it as the tonic, so diatonic quality only ever
    # agreed with SCALE SNAP in the key of C (36 of 60 key x scale combinations
    # produced a chord containing a note outside its own scale).
    "root":            0,
    # The pitch class the performer actually pressed.  Its position within the
    # key's scale is the chord's DEGREE, which is what decides quality in DIAT.
    "chord_root":      0,
    "octave":          4,
    "inversion":       0,
    "voicing":         "CLOSE",
    "scale":           "CHROM",
    "key_lock":        False,
    # Instrument-wide scale lock: pulls every note played on the Fire's
    # poly/chip keyboards into the current root+scale.  Distinct from
    # key_lock (which since 2026-07-29 pulls an off-key CHORD ROOT into the key
    # -- see chord_root_now(); note the two round ties in opposite directions)
    # and from melody_mode (which only affects the dedicated melody channel).
    "scale_snap":      False,
    # O4 (2026-07-30): the five-way replacement for the binary scale_snap above.
    # "off" | "filter" | "up" | "down" | "nearest" -- MiniKeys' vocabulary.
    # scale_snap is kept as a derived MIRROR (True whenever mode != "off") so
    # older readers and saved state keep working; scale_mode is the truth.
    "scale_mode":      "off",
    # O4: where the chord QUALITY comes from, made explicit instead of being a
    # silent consequence of base_override.
    #   "DIAT"  -- quality is the scale degree under the chord root. A base pad
    #             is a MOMENTARY override (base_override), cleared by the next
    #             key press. This is the boot default and the 07-28 behaviour.
    #   "FIXED" -- the MAJ/MIN/SUS/DIM pads are a PERSISTENT setting again, the
    #             thing 07-28 removed. A key press does not clear it.
    # Kept separate from base_override on purpose: that flag still means "a
    # momentary override is in force *within* DIAT", and conflating the two is
    # what made the pads feel dead in the first place.
    "quality_mode":    "DIAT",
    "hold":            False,
    "vl_enabled":      True,
    "vl_strength":     1.0,
    "spread":          0,
    "oct_offset":      0,
    "vel_override":    0,
    "velocity":        100,
    # vel_lock -- Fire button toggle (ALT+REC): when True, every note-on this
    # engine sends out (chord engine + internal poly/chip synth) goes out at
    # velocity 127, no matter what the input velocity was. For players who
    # never want to think about velocity. Different from vel_override above:
    # vel_override is a knob-set fixed value for the chord engine only;
    # vel_lock is an all-or-nothing hard lock to max, covering everything
    # this engine plays through itself. Does NOT touch DIN passthrough --
    # external gear playing through the Pi keeps its own velocity.
    "vel_lock":        False,
    "prev_notes":      [],
    "held_notes":      [],
    "trigger_note":    None,
    "trigger_ch":      None,
    "chord_history":   [],
    "arp_passthrough": False,
    # arp_hold -- when True the arp keeps running after the trigger note is
    # released (classic latch behaviour).  When False, releasing the note
    # clears the arp chord so it stops at the end of the current step.
    # Toggle via TOGGLE_ARP_HOLD (web/cmd) or CC 41 on CH_CTRL.
    "arp_hold":        False,
    "live_notes":      {},
    "mirror_input_ch": False,  # CC toggle: mirror chord output to input channel

    # Leading voice
    # An integer position index into the sorted voicing-position list.
    # Driven by a relative rotary CC. 0 = lowest available voicing.
    # Does not reset between chords -- the performer chooses where to leave it.
    "lead_voice_pos":  0,

    # Function key
    # func_held: True while the function pad is physically held
    # func_transpose: semitone offset applied to all output while func held;
    #   reverts to 0 on func key release
    # func_pre_scale / func_pre_pattern: values to return to if the performer
    #   releases func without "committing" -- currently unused (lock-on-release
    #   is the default), but available for future revert behaviour
    "func_held":       False,
    "func_transpose":  0,
    "func_pre_scale":  None,
    "func_pre_pattern":None,

    # Melody snap
    "melody_mode":     "OFF",   # "OFF" "SCALE" "CHORD"
    "melody_map":      {},      # input_note -> snapped_note (for clean note-offs)

    # Synth mirror -- route chord-engine output to internal synth alongside the
    # preset routing. Each flag toggles independently.
    "synth_mirror_poly": False,  # True = also send chords to CH_POLY (8)
    "synth_mirror_chip": False,  # True = also send chords to CH_CHIP (9)
    # Notes held by the mirror (for clean note-offs)
    "_mirror_held":      [],     # [(note, ch0)] currently sounding via mirror

    # External MIDI input routing
    "din_routing":       "engine",  # "passthrough" = DIN relay only; "engine" = same routing as USB

    # Runtime chord output override — redirects all non-synth voice slots to
    # a different port/channel without touching the preset definitions.
    # None = use preset routing as-is.  Persisted to ~/.orc1_chord_out.json.
    "chord_out":         {"port": None, "channel": None},

    # Perform-VE follow — ADDITIVE mirror of the sounding chord to the "performve"
    # port on ch1 so the box's HardTune NaturalPlay re-keys to what we play.
    # Touches NO other routing (unlike chord_out, which redirects).  Feed at least
    # root+3rd; the box follows maj/min (not harmonic-minor/pentatonic — hardware
    # scale-engine limit).  Persisted to ~/.orc1_performve.json.
    "performve_feed":    False,
    "_pve_held":         [],   # notes currently held at the box (for clean re-key)

    # Perform-VE INPUT source (option A) — claim an external chord stream for the
    # box, CONSUMED: matched notes go to 'performve' ch1 ONLY (not to modules /
    # passthrough), so external gear can drive NaturalPlay without occupying an
    # ORC module/reserved channel.  port: None|"usb"|"din"|"both"; channel:
    # None(any)|1-16.  Persisted to ~/.orc1_performve.json.
    "performve_src":     {"port": None, "channel": None},

    # Runtime melody snap output override — redirects melody-snap output from
    # its default (pisound, MELODY_CH=15) to any port/channel.
    # Persisted to ~/.orc1_melody_out.json.
    "melody_out":        {"port": None, "channel": None},

    # Per-slot voice routing overrides — applied on top of the active preset.
    # Sparse dict: only slots present are overridden.
    # None value = silence that slot.  {port, channel} = route there instead.
    # Persisted to ~/.orc1_voicemap.json.
    "voice_map":         {},

    # Last-known CC value per (ch, cc) forwarded to the synth bus by SYNTH_CC.
    # Keyed as strings: state["synth_cc"]["8"]["74"] = 64.
    # Sliders on SYNTH/CHIP/DRUMS pages initialise from this on poll.
    "synth_cc":          {},

    # Per-voice MIDI thru — mirrors synth-bus messages to an external port.
    # Keyed by source channel (str, 1-indexed) -> {port, channel}.
    # e.g. {"6": {"port": "pisound", "channel": 10}}
    # Persisted to ~/.orc1_voice_thru.json.
    "voice_thru":        {},
}


# -- Thread safety -------------------------------------------------------------
_state_lock = threading.Lock()


# -- Tap tempo -----------------------------------------------------------------
_tap_times = deque(maxlen=8)  # average up to 7 inter-tap intervals


# -- Port registry / arp / mixtrack ------------------------------------------
port_outs      = {}
_dynamic_ports = {}   # key -> full ALSA name, opened on demand by MIDI_SCAN
arp       = None
fx        = None    # FXManager -- eng.fx for Fire, web UI, and command handler
fx_inserts = None   # InsertManager -- arbitrary per-source wet/dry fx inserts
mixtrack   = None
fire       = None
seq        = None  # Sequencer -- engine-owned since 2026-07-01 (was fire.seq only)
rc505      = None  # RC505Handler -- OUTPUT driver for the Boss RC-505 (orc1_rc505.py)
beatbuddy  = None  # BeatBuddyMind -- foot-drummer state machine (orc1_beatbuddy.py)
_bb_err_seen = set()  # dedup for logged BeatBuddy tick faults (visibility, not silence)
_bb_next_quit = 0  # pulses remaining until a BB_NEXT auto-quits the transition (advance)
_bb_follow_clock = False  # False: loading a song sets the ORC tempo to the song's
                          # native bpm (default). True: leave the clock alone so the
                          # song plays at the CURRENT tempo (internal set-bpm OR an
                          # external MIDI clock) -- "match the bpm to the clock".
# BeatBuddy OUTPUT (2026-09-23): where the mind's hits go -- a CHOICE (routing is
# choice, not ownership): "bbkit" (the sampled kit host, default), "909" (the ORC
# drum module, BB notes translated by cfg.BB_TO_909), or "port" (any MIDI out --
# the MPC etc. -- on a chosen channel, notes untranslated). Persisted.
_BB_OUT = {"dest": "bbkit", "port": None, "channel": 10}
_BB_OUT_DESTS = ("bbkit", "909", "port")
_BB_PORT_OFF_S = 0.1          # external gear gets a note-off this long after the hit
_bb_pending_offs = []         # [(deadline_monotonic, port_key, ch0, note)]
pedal      = None  # PedalInput -- foot pedal -> beatbuddy (orc1_pedal.py), feedback-safe
_pedal_ports = []  # opened pedal input ports (threaded in run_all_inputs)
_PEDAL_PREFIXES = tuple(getattr(cfg, "PEDAL_PORT_PREFIXES", _orc1_pedal.DEFAULT_PREFIXES))
# PATCHBAY devices (2026-09-30): MIDI routers like the CME U6MIDI Pro. Their USB
# ports are NOT opened as engine inputs: with pisound DIN OUT -> CME In 1 cabled,
# the usb-engine path _passes everything back to the pisound DIN = a loop. They
# stay plain ports you route to/from by choice. Remove a prefix to open it again.
_PATCHBAY_PREFIXES = tuple(p.lower() for p in getattr(cfg, "PATCHBAY_PORT_PREFIXES", ("u6midi",)))
_patchbay_noted = set()   # port names already logged as patchbay (log once, not per scan)
cme = None                # orc1_cme.CmeLink: the brain is the CME's ONLY config owner
kikpad = None             # orc1_kikpad.KikpadSurface: the pad's port :1 (its host bank)
kikpad_map = None         # orc1_kikpad_map.KikpadMapLink: the editor of the pad's own maps (same port)


def _kikpad_open(name, port):
    """The KIKPAD's SECOND port ("KIKPAD:KIKPAD IN <n>:1") is its host surface
    (bank 2): claimed by name, never an engine input, so its presses can't
    play a module or reach the DIN. The pad's music banks stay on port :0.
    (Re)attaching also reopens the paint output: a replugged pad reuses the
    name with a new ALSA port and a stale handle drops silently."""
    global kikpad, kikpad_map
    if kikpad is None:
        kikpad = _orc1_kikpad.KikpadSurface(sys.modules[__name__])
    if kikpad_map is None:
        # the pad's OWN pad/encoder maps (stored on the pad), edited from /kikpad over the same port :1
        kikpad_map = _orc1_kikpad_map.KikpadMapLink(kikpad.send_sysex)
        kikpad.sysex_hook = kikpad_map.on_sysex
    attached = kikpad.attach(name)
    surface = kikpad
    kikpad_map.connected(bool(attached))

    def handler():
        try:
            for msg in port:
                surface.handle(msg)
        except Exception as e:
            print(f"  [kikpad] {name}: {e}", flush=True)
        finally:
            if _untrack_input(name, port):
                surface.detach(name)
                if kikpad_map is not None:
                    kikpad_map.connected(False)
                print(f"  [kikpad] {name} -> disconnected, will reopen on reconnect",
                      flush=True)
    threading.Thread(target=handler, daemon=True, name="kikpad-in").start()


def _is_patchbay(name):
    nl = (name or "").lower()
    return any(p in nl for p in _PATCHBAY_PREFIXES)


_cme_present = set()      # CME config-port names seen on the last scan


def _cme_scan(current, replugged=()):
    """The CME link's OWN lifecycle, independent of the patchbay decision (it
    used to live inside the patchbay branch, so dropping "u6midi" from
    PATCHBAY_PORT_PREFIXES lost every connect/unplug). `current` = input names
    now, `replugged` = USB card names whose address changed (a fast same-name
    replug the name diff can't see). A port that came, or came back under the
    same name, = close + re-read on the link's worker; all gone = unplugged
    (publishes connected:false)."""
    global _cme_present
    try:
        if cme is None:
            return
        now = {n for n in current if cme.matches(n) and "port 1" in n.lower()}
        came = now - _cme_present
        again = {n for n in now & _cme_present if n.split(":", 1)[0] in replugged}
        gone = _cme_present - now
        _cme_present = now
        if came or again:
            cme.submit("connect" if came else "replug",
                       lambda: (cme.close(), cme.refresh())[1])
        elif gone:
            cme.submit("unplugged", lambda: (cme.close(), cme.refresh())[1])
    except Exception as e:       # never let the CME abort a hot-plug scan
        print(f"  ! cme port scan: {e}", flush=True)
_synth_bus = None  # mido virtual port -> orc1_synth MIDI input
_din_clock_last  = 0.0   # monotonic time of the last DIN clock pulse (0 = never seen)
_din_auto_synced = False  # True only when external mode was set by auto-detect (not user)
_clock_owner     = None   # source_label ('din'/'usb') that currently drives the arp
                          # clock; every OTHER source's clock is ignored so a device
                          # cabled to both USB and DIN (RC-505) can't double-tick it.


# -- Output helpers ------------------------------------------------------------


_ps_activity = _orc1_playstate.ActivityTracker()   # synth-bus note-on counters (HUD pulses)


def send_to(port_key, msg):
    port = port_outs.get(port_key)
    if port:
        try:
            port.send(msg)
            if port_key == "synth" and msg.type == "note_on" and msg.velocity > 0:
                _ps_activity.hit(msg.channel + 1, msg.velocity)   # two dict stores
            if cfg.DEBUG:
                print(f"  [-> {port_key}] {msg}")
        except Exception as e:
            print(f"  ! send_to '{port_key}': {e}")
    else:
        if cfg.DEBUG:
            print(f"  [-> {port_key}] SKIPPED")


def note_on_out(note, vel, port_key, ch):
    if 0 <= note <= 127:
        if state.get("vel_lock"):
            vel = 127
        send_to(port_key, mido.Message(
            'note_on', channel=ch-1, note=note, velocity=vel))


def note_off_out(note, port_key, ch):
    if 0 <= note <= 127:
        send_to(port_key, mido.Message(
            'note_off', channel=ch-1, note=note, velocity=0))


def all_notes_off():
    """
    Send all-notes-off and sustain-off only on channels the chord engine
    actually uses -- never on ALWAYS_PASSTHROUGH channels, so drums and
    external gear running through the Pi are not silenced.
    """
    engine_channels = set()
    for preset in cfg.PRESETS.values():
        for slot, dest in preset.items():
            if dest and isinstance(dest, dict):
                c = _dest_channel(dest["port"], dest["channel"])
                if c:
                    engine_channels.add((dest["port"], c - 1))
    engine_channels.add((cfg.BASS_MIRROR_PORT, cfg.BASS_FIXED_CH - 1))

    for pk, ch0 in engine_channels:
        if (ch0 + 1) in cfg.ALWAYS_PASSTHROUGH:
            continue
        port = port_outs.get(pk)
        if port:
            try:
                port.send(mido.Message(
                    'control_change', channel=ch0, control=123, value=0))
                port.send(mido.Message(
                    'control_change', channel=ch0, control=64,  value=0))
            except Exception:
                pass
    _performve_feed_off()   # release any notes held at the Perform-VE too
    print("  PANIC -- chord engine channels only")


def tap_tempo():
    now = time.perf_counter()
    # A tap after a long pause starts a fresh measurement -- otherwise the
    # huge gap interval is averaged in and the first new taps give garbage.
    if _tap_times and (now - _tap_times[-1]) > 4.0:   # 4s gap = fresh start
        _tap_times.clear()
    _tap_times.append(now)
    if len(_tap_times) >= 2:
        intervals = [_tap_times[i]-_tap_times[i-1]
                     for i in range(1, len(_tap_times))]
        avg = sum(intervals) / len(intervals)
        if 0.2 < avg < 3.0:
            bpm = 60.0 / avg
            if arp:
                if arp.clock_mode == CLOCK_DRIFT:
                    arp.set_drift_bpm(bpm)
                else:
                    arp.set_internal_bpm(bpm)
                print(f"  TAP BPM -> {bpm:.1f}")


# -- Internal synth MIDI bus --------------------------------------------------

def _open_synth_bus():
    """Open a mido virtual MIDI output that orc1_synth discovers by name.
    Fails gracefully -- engine works fine without it.
    """
    global _synth_bus
    bus_name = getattr(cfg, "SYNTH", {}).get("INTERNAL_BUS_NAME", "ORC1-Synth-Bus")
    try:
        _synth_bus = mido.open_output(bus_name, virtual=True)
        port_outs["synth"] = _synth_bus   # presets can route chords here -> internal synth
        print(f"  ✓ Synth bus: {bus_name}")
    except Exception as e:
        _synth_bus = None
        print(f"  ! Synth bus unavailable: {e}")


def _synth_bus_guard_once():
    """Disconnect the ORC1-Synth-Bus from any rtpmidid auto-subscriptions.

    rtpmidid subscribes to every visible ALSA MIDI port when an RTP MIDI client
    connects (Mac, phone, DAW).  That leaks every internal sampler command —
    LOOP, PLAY, one-shot triggers, bounds CCs — to the downstream instrument as
    raw MIDI notes with no corresponding note-offs, causing stuck notes that need
    PANIC to clear.  The only subscriber the synth bus should ever have is
    orc1_synth's own RtMidiIn input.
    """
    import re, subprocess as _sp
    try:
        out = _sp.check_output(["aconnect", "-l"], text=True, timeout=5)
    except Exception:
        return

    # Parse aconnect -l: build {client_num: name} and find the synth bus.
    client_names = {}          # client_num_str -> client_name_lower
    cur_client  = None
    cur_port    = None
    bus_client  = None
    bus_port    = None
    bus_targets = []           # list of (target_client_str, target_port_str)

    bus_id = getattr(cfg, "SYNTH", {}).get("INTERNAL_BUS_NAME", "ORC1-Synth-Bus").lower()

    for line in out.split('\n'):
        cm = re.match(r'^client (\d+):\s+\'([^\']*)', line)
        if cm:
            cur_client = cm.group(1)
            cur_port   = None
            client_names[cur_client] = cm.group(2).strip().lower()
            continue
        pm = re.match(r'^\s{4}(\d+)\s+\'([^\']*)', line)
        if pm and cur_client:
            cur_port = pm.group(1)
            if bus_id in pm.group(2).strip().lower():
                bus_client = cur_client
                bus_port   = cur_port
            continue
        tm = re.match(r'^\tConnecting To:\s+(.+)', line)
        if tm and cur_client == bus_client and cur_port == bus_port:
            for conn in tm.group(1).split(','):
                conn = conn.strip().split('[')[0].strip()
                parts = conn.split(':')
                if len(parts) == 2:
                    bus_targets.append((parts[0], parts[1]))

    if not bus_client:
        return

    for tc, tp in bus_targets:
        tname = client_names.get(tc, '')
        if 'rtpmidid' in tname or 'rtpmidi' in tname:
            try:
                _sp.run(['aconnect', '-d', f'{bus_client}:{bus_port}',
                         f'{tc}:{tp}'], timeout=5, capture_output=True)
                print(f'  [synth_bus] blocked leak → {tname} ({tc}:{tp})', flush=True)
            except Exception as e:
                print(f'  [synth_bus] disconnect error: {e}', flush=True)


def _synth_bus_guard_loop():
    """Guard the synth bus against rtpmidid auto-subscriptions forever.
    Polls every 5 s — aconnect -l is ~1ms, negligible cost."""
    time.sleep(4)   # let the synth connect first so its subscriber isn't killed
    while True:
        try:
            _synth_bus_guard_once()
        except Exception:
            pass
        time.sleep(5)


def send_synth_midi(msg):
    """Route a MIDI message to orc1_synth via the internal virtual port.

    Also mirrors the message to any external port configured via voice_thru
    for this channel (A2 routing freedom).  The thru copy uses the configured
    output channel; everything else (note, velocity, CC) is preserved.
    """
    if state.get("vel_lock") and msg.type == "note_on" and msg.velocity > 0:
        # Force to max velocity. Only real note-ons -- leave note_off alone,
        # its velocity field is meaningless/should stay 0.
        msg = msg.copy(velocity=127)
    if _synth_bus:
        try: _synth_bus.send(msg)
        except Exception: pass
        if msg.type == "note_on" and msg.velocity > 0:
            _ps_activity.hit(msg.channel + 1, msg.velocity)   # HUD pulse (2nd synth-bus choke point)
    # Voice thru: mirror to external port if configured for this source channel.
    if hasattr(msg, 'channel') and msg.type in ('note_on', 'note_off', 'control_change'):
        src_ch = str(msg.channel + 1)   # 1-indexed string key
        thru = state.get("voice_thru", {}).get(src_ch)
        if thru and thru.get("port") and thru.get("channel"):
            thru_msg = msg.copy(channel=int(thru["channel"]) - 1)
            send_to(thru["port"], thru_msg)


# -- Live sampler facade (controller-agnostic) --------------------------------
# The ONE public surface every front-end uses (web, Mixtrack, any controller).
# Each call builds a CH_SAMPLER MIDI message and reaches orc1_synth via the
# internal bus. Deck is 'A' / 'B'. Keeps all sampler logic out of front-ends.
# Vocabulary comes from cfg.SAMPLER (single source of truth — orc1_synth.py
# decodes the same entries). Fallbacks match the historical layout.
_SMP_CFG       = getattr(cfg, "SAMPLER", {})
_CH_SAMPLER    = int(_SMP_CFG.get("CH_SAMPLER", 7))
_SMP_CAP_NOTE  = _SMP_CFG.get("NOTE_CAPTURE", {"A": 1,  "B": 2})
_SMP_GATE_NOTE = _SMP_CFG.get("NOTE_GATE",    {"A": 3,  "B": 4})
_SMP_LOOP_NOTE = _SMP_CFG.get("NOTE_LOOP_TOGGLE", {"A": 5, "B": 6})
_SMP_AUDITION_NOTE = _SMP_CFG.get("NOTE_AUDITION", {"A": 23, "B": 24})
_SMP_EXPORT_NOTE = _SMP_CFG.get("NOTE_EXPORT", {"A": 7, "B": 8})
_SMP_SCRUB_CC  = _SMP_CFG.get("CC_SCRUB",     {"A": 16, "B": 17})
_SMP_SPEED_CC  = _SMP_CFG.get("CC_SPEED",     {"A": 18, "B": 19})
_SMP_GAIN_CC   = _SMP_CFG.get("CC_GAIN",      {"A": 20, "B": 21})
_SMP_XF_CC     = int(_SMP_CFG.get("CC_CROSSFADE", 22))
_SMP_CAPMSB_CC = _SMP_CFG.get("CC_CAP_LEN_MSB", {"A": 23, "B": 24})
_SMP_CAPLSB_CC = _SMP_CFG.get("CC_CAP_LEN_LSB", {"A": 25, "B": 26})
_SMP_BEATS_BAR = int(_SMP_CFG.get("BEATS_PER_BAR", 4))
_SMP_EQ_HI_CC  = _SMP_CFG.get("CC_EQ_HI",  {"A": 27, "B": 28})
_SMP_EQ_MID_CC = _SMP_CFG.get("CC_EQ_MID", {"A": 29, "B": 30})
_SMP_EQ_LOW_CC = _SMP_CFG.get("CC_EQ_LOW", {"A": 31, "B": 32})
_SMP_FILTER_CC = _SMP_CFG.get("CC_FILTER", {"A": 33, "B": 34})
_SMP_STOP_NOTE    = _SMP_CFG.get("NOTE_STOP",    {"A": 9,  "B": 10})
_SMP_CUE_NOTE     = _SMP_CFG.get("NOTE_CUE",     {"A": 11, "B": 12})
_SMP_SCRATCH_NOTE = _SMP_CFG.get("NOTE_SCRATCH", {"A": 13, "B": 14})
_SMP_REVERSE_NOTE = _SMP_CFG.get("NOTE_REVERSE", {"A": 21, "B": 22})
_SMP_MASTER_GAIN_CC = int(_SMP_CFG.get("CC_MASTER_GAIN", 35))
# Frame-exact capture + nudge vocab (2026-07-07 capture-timing overhaul).
# Each 28-bit value goes out as four 7-bit CCs big-endian; the length's LSB
# fires the capture in the synth (see orc1_synth._route_sampler).
_SMP_CAP_FRA_CC = {d: [_SMP_CFG.get(f"CC_CAP_FRA_B{b}", {}).get(d, dflt)
                       for b, dflt in ((3, 36 + o), (2, 37 + o), (1, 38 + o), (0, 39 + o))]
                   for d, o in (("A", 0), ("B", 4))}
_SMP_CAP_OFF_CC = {d: [_SMP_CFG.get(f"CC_CAP_OFF_B{b}", {}).get(d, dflt)
                       for b, dflt in ((3, 44 + o), (2, 45 + o), (1, 46 + o), (0, 47 + o))]
                   for d, o in (("A", 0), ("B", 4))}
_SMP_NUDGE_CC   = _SMP_CFG.get("CC_NUDGE", {"A": 52, "B": 53})
_SMP_LOAD_NOTE  = _SMP_CFG.get("NOTE_LOAD_FILE", {"A": 15, "B": 16})
_SMP_SEEK_CC    = _SMP_CFG.get("CC_SEEK", {"A": 54, "B": 55})
_SMP_START_CC   = _SMP_CFG.get("CC_START", {"A": 80, "B": 81})   # play/loop region edges
_SMP_END_CC     = _SMP_CFG.get("CC_END",   {"A": 82, "B": 83})
# Momentary beat-repeat: a 28-bit loop LENGTH (frames, big-endian B3..B0, held
# pending) + a note that starts (on) / stops (off) the stutter.
_SMP_BR_NOTE    = _SMP_CFG.get("NOTE_BEAT_REPEAT", {"A": 25, "B": 26})
_SMP_BR_LEN_CC  = {d: [_SMP_CFG.get(f"CC_BR_LEN_B{b}", {}).get(d, dflt)
                       for b, dflt in ((3, 85 + o), (2, 86 + o), (1, 87 + o), (0, 88 + o))]
                   for d, o in (("A", 0), ("B", 4))}
_SMP_BENDUP_NOTE = _SMP_CFG.get("NOTE_BEND_UP", {"A": 17, "B": 18})
_SMP_BENDDN_NOTE = _SMP_CFG.get("NOTE_BEND_DN", {"A": 19, "B": 20})
_SMP_LOAD_PATH  = _SMP_CFG.get("FILE_LOAD_PATH",
                               os.path.expanduser("~/.orc1_sampler_load_{deck}.json"))
_SMP_FILE_ROOTS = _SMP_CFG.get("FILE_ROOTS", [os.path.expanduser("~/recordings")])
_SMP_RING_RATE  = int(_SMP_CFG.get("RING_RATE", 48000))
_SMP_QUANTIZE   = bool(_SMP_CFG.get("QUANTIZE_CAPTURE", True))
# Bar-grab anchor: "press" (default) ends the window at the player's press;
# "grid" restores the old barline-quantized behaviour. See sampler_capture_bars.
_SMP_CAPTURE_ANCHOR = str(_SMP_CFG.get("CAPTURE_ANCHOR", "press")).lower()
_SMP_PRESS_LAT_CC   = int(_SMP_CFG.get("CC_PRESS_LATENCY", 84))
_SMP_ONSET_CC       = int(_SMP_CFG.get("CC_ONSET_SNAP", 95))
_SMP_DECK_SRC_CC    = _SMP_CFG.get("CC_DECK_SOURCE", {"A": 96, "B": 97})
_SMP_QSNAP      = float(_SMP_CFG.get("QUANTIZE_SNAP", 0.15))
_SMP_QMARGIN_S  = float(_SMP_CFG.get("QUANTIZE_MARGIN_MS", 350)) / 1000.0
# Fallback matches the config default (-99). It used to be 42 here and 85 in
# orc1_sampler.py while config said -99 -- three different numbers for one
# constant. Only reachable if the SAMPLER config block fails to load, but in
# that case a disagreeing fallback silently shifts every capture window by
# 100-600ms, which is exactly the class of bug that is hardest to trace back
# to a default. Keep this equal to SAMPLER["CAPTURE_LATENCY_MS"].
_SMP_CAPTURE_LAT_S = float(_SMP_CFG.get("CAPTURE_LATENCY_MS", -99)) / 1000.0
_SMP_ANCHOR_LAT_S  = float(_SMP_CFG.get("ANCHOR_LATENCY_MS", -9)) / 1000.0

# ── Forward capture (press-as-downbeat; see _planning/ORC1_Forward_Capture_Spec)
# The retroactive grab aligns to the ORC's own bar phase, which has no relation
# to the audio's downbeat -- the "not lined up" complaint. Forward capture takes
# the PLAYER'S PRESS as the downbeat and uses tempo only for bar LENGTH, so it
# never consults _sampler_bar_phase_ex. TAP = fixed N bars; HOLD = record to
# release (end snapped to whole bars). HOLD threshold is read by the Mixtrack
# handler (slice 3); the constants live here with the other sampler config.
_FWD_HOLD_MS  = float(_SMP_CFG.get("FWD_HOLD_THRESHOLD_MS", 280))
_FWD_SNAP     = bool(_SMP_CFG.get("FWD_SNAP_LENGTH_TO_BARS", True))
_FWD_MIN_BARS = int(_SMP_CFG.get("FWD_MIN_BARS", 1))

# ── Audio-domain bar anchor (2026-07-20, Bug-1 clock-drift fix) ──────────────
# Quantized bar-grabs used to locate "the last barline" purely with the
# SOFTWARE clock (time.monotonic via seq/arp bar_phase). The ring audio is
# written on the SOUNDCARD clock — two clocks at slightly different rates —
# so the grab window slid off the true downbeat as the seq ran (measured
# 2026-07-19: ~5ms fresh -> ~175ms after ~15 min; orc1 restart reset it).
# Fix: at every downbeat the seq/arp calls mark(), snapshotting the skipback
# ring's WRITE HEAD (pos file + mtime extrapolation — the same estimator as
# RingBufferReader.head_info, duplicated in stdlib here so the brain doesn't
# import the sampler's numpy stack). At grab time "seconds since the barline"
# is then measured in RING BYTES — the audio's own clock — so it physically
# cannot drift, whatever drives the downbeat (internal clock now, external
# clock later). Software bar_phase remains the FALLBACK (no mark yet, ring
# recorder down, wrap/reset weirdness), which is exactly the old behavior.
_SMP_RING_CHANNELS = int(_SMP_CFG.get("RING_CHANNELS", 2))
_SMP_RING_BPS      = _SMP_RING_RATE * _SMP_RING_CHANNELS * 2   # 2 = S16 width
_SMP_RING_BYTES    = _SMP_RING_BPS * int(_SMP_CFG.get("RING_SECONDS", 1860))
_SMP_POS_PATH      = _SMP_CFG.get("RING_POS_PATH",
                                  os.path.expanduser("~/.skipbuf/ring.pos"))

class _AudioBarAnchor:
    """Snapshot the ring write head at downbeats; answer 'audio seconds since
    the last barline' at grab time. One instance per grid (seq / arp) because
    their bar phases differ. All methods are try/except-total: this runs in
    the clock thread and must never raise or block it."""

    def __init__(self, pos_path, bytes_per_s, ring_bytes,
                 status_path=None, tracks_path=None):
        self._pos_path = pos_path
        self._mark     = None            # (est_head_bytes, monotonic) tuple
        self._status_path = status_path or os.path.expanduser(
            "~/.orc1_skipback_status.json")
        self._tracks_path = tracks_path or os.path.expanduser(
            "~/.orc1_skipback_tracks.json")
        # Geometry as ONE tuple (bps, ring_bytes), matching the _mark
        # single-assignment atomicity precedent below: readers take one
        # reference and can never see a half-updated pair. The constructor
        # values are config-derived FALLBACKS, superseded by the first
        # successful _refresh_geo().
        self._geo = (float(bytes_per_s), float(ring_bytes))
        self._geo_mtimes = None

    # ── geometry (P10) ────────────────────────────────────────────────────────
    # Why: _SMP_RING_BYTES being wrong is not itself a correctness risk (the
    # wrap value fails the bounds check in since_seconds and we fall back to
    # the software clock). But _SMP_RING_BPS is derived from RING_CHANNELS and
    # is BOTH the extrapolation multiplier in _head_now AND the divisor in
    # since_seconds -- so on a MONO ring it halves every anchor result while
    # staying INSIDE the bounds check, silently anchoring grabs half a bar off.
    #
    # The brain needs no ring access: the recorder publishes the exact live
    # modulus as `ring_bytes` per track in the status file. Channels come from
    # the tracks JSON (the status file has NO channels field -- verified).
    # NOTE fill_secs is bytes WRITTEN capped at the ring size, not the ring
    # length, so bps must NOT be derived from ring_bytes/fill_secs -- that is
    # only correct once the ring has filled.

    def _track_id(self):
        """Which track this anchor watches, from the pos path (realpath
        follows the legacy ~/.skipbuf/ring.pos -> t0/ring.pos symlink)."""
        try:
            parent = os.path.basename(os.path.dirname(
                os.path.realpath(self._pos_path)))
            if len(parent) > 1 and parent[0] == "t" and parent[1:].isdigit():
                return int(parent[1:])
        except Exception:
            pass
        return 0

    def _refresh_geo(self):
        """Re-resolve (bps, ring_bytes) from the recorder's own published
        state. Gated on file MTIME rather than a TTL. Drops _mark on any
        geometry change -- otherwise a mark taken under the old bps gets
        compared against a head measured under the new one.

        Called ONLY from since_seconds (control thread, at grab time). Never
        from mark(), whose contract is 'never raise or block the clock
        thread' -- a JSON parse has no business there."""
        try:
            mt = (os.stat(self._status_path).st_mtime,
                  os.stat(self._tracks_path).st_mtime)
        except OSError:
            return
        if mt == self._geo_mtimes:
            return
        self._geo_mtimes = mt
        tid = self._track_id()
        try:
            with open(self._status_path) as f:
                st = json.load(f)
            ring = 0
            for t in st.get("tracks", []):
                if isinstance(t, dict) and t.get("id") == tid:
                    ring = int(t.get("ring_bytes") or 0)
                    break
            with open(self._tracks_path) as f:
                ch = 0
                for t in json.load(f).get("tracks", []):
                    if isinstance(t, dict) and t.get("id") == tid:
                        ch = 2 if t.get("stereo", True) else 1
                        break
        except Exception:
            return
        if ring <= 0 or ch not in (1, 2):
            return
        bps = float(_SMP_RING_RATE * ch * 2)
        # Desired-vs-actual cross-check: the tracks JSON is desired state and
        # can lag the recorder. If the channel count doesn't divide the live
        # modulus, believe neither -- keep what we have.
        if ring % bps:
            return
        geo = (bps, float(ring))
        if geo != self._geo:
            self._geo = geo
            self._mark = None
            print(f"[anchor] t{tid} geometry -> {int(bps)} B/s, "
                  f"{ring} B ({ring / bps:.1f}s); mark dropped", flush=True)

    def _head_now(self, bps=None):
        """Estimated write head RIGHT NOW (bytes, fractional), or None when
        the recorder looks down (pos file missing or mtime stale >2s — at
        that point extrapolation is fiction and grabs are garbage anyway)."""
        try:
            # Callers that have already unpacked _geo pass their bps in, so a
            # geometry change landing mid-call cannot make one since_seconds()
            # use two different bps values (review item 10). mark() passes
            # nothing and reads the tuple itself -- one read, still atomic.
            if bps is None:
                bps, _ring = self._geo
            st  = os.stat(self._pos_path)
            age = time.time() - st.st_mtime
            if not (0.0 <= age <= 2.0):
                return None
            with open(self._pos_path) as f:
                pos = int(f.read().strip())
            return pos + age * bps
        except Exception:
            return None

    def mark(self):
        """Called at each downbeat from the clock thread. Cost: one stat +
        one ~10-byte read on tmpfs, once per bar. Single-assignment store is
        atomic under the GIL — no lock needed."""
        h = self._head_now()
        if h is not None:
            self._mark = (h, time.monotonic())

    def since_seconds(self, bar_s):
        """Audio-domain seconds since the last marked downbeat, or None to
        make the caller fall back to the software clock. Bounds-checked hard:
        a stale mark (grid stopped, bar length changed) or a ring reset must
        never anchor a grab to the wrong place."""
        # Re-resolve geometry first: this is the control thread, and a changed
        # ring drops _mark below, which correctly makes THIS grab fall back to
        # the software clock rather than anchor against a stale modulus.
        self._refresh_geo()
        bps, ring = self._geo               # one read of the atomic tuple
        m = self._mark
        if m is None:
            return None
        h = self._head_now(bps)
        if h is None:
            return None
        head_mark, t_mark = m
        # Mark older than ~1.5 bars = the grid moved on without marking
        # (stopped/paused, or bar length changed) — don't trust it.
        if time.monotonic() - t_mark > bar_s * 1.6 + 0.25:
            return None
        delta = h - head_mark
        if delta < -bps:                 # head wrapped (or recorder reset)
            delta %= ring                # wrap recovers; reset fails bounds
        since = max(0.0, delta / bps)
        if since >= bar_s:               # next downbeat fired but its mark
            since -= bar_s               # hasn't landed yet — rare race
        return since if 0.0 <= since < bar_s else None

_SMP_STATUS_PATH = _SMP_CFG.get("RING_STATUS_PATH",
                                os.path.expanduser("~/.orc1_skipback_status.json"))
_SMP_TRACKS_PATH = _SMP_CFG.get("RING_TRACKS_PATH",
                                os.path.expanduser("~/.orc1_skipback_tracks.json"))
_smp_seq_anchor = _AudioBarAnchor(_SMP_POS_PATH, _SMP_RING_BPS, _SMP_RING_BYTES,
                                  _SMP_STATUS_PATH, _SMP_TRACKS_PATH)
_smp_arp_anchor = _AudioBarAnchor(_SMP_POS_PATH, _SMP_RING_BPS, _SMP_RING_BYTES,
                                  _SMP_STATUS_PATH, _SMP_TRACKS_PATH)


def _smp_note_on(note, vel):
    send_synth_midi(mido.Message("note_on", channel=_CH_SAMPLER - 1,
                                 note=int(note), velocity=int(max(0, min(127, vel)))))

def _smp_cc(cc, val):
    send_synth_midi(mido.Message("control_change", channel=_CH_SAMPLER - 1,
                                 control=int(cc), value=int(max(0, min(127, val)))))

def sampler_capture(deck, seconds=None):
    if seconds is None:
        seconds = int(getattr(cfg, "SAMPLER", {}).get("DEFAULT_CAPTURE_SECONDS", 8))
    if deck in _SMP_CAP_NOTE:
        _smp_note_on(_SMP_CAP_NOTE[deck], max(1, min(127, int(seconds))))

def sampler_gate(deck, on):
    if deck in _SMP_GATE_NOTE:
        if on:
            _smp_note_on(_SMP_GATE_NOTE[deck], 127)
        else:
            send_synth_midi(mido.Message("note_off", channel=_CH_SAMPLER - 1,
                                         note=_SMP_GATE_NOTE[deck], velocity=0))

def sampler_speed(deck, value_0_127):
    if deck in _SMP_SPEED_CC:
        _smp_cc(_SMP_SPEED_CC[deck], value_0_127)

def sampler_gain(deck, value_0_127):
    if deck in _SMP_GAIN_CC:
        _smp_cc(_SMP_GAIN_CC[deck], value_0_127)

def sampler_scrub(deck, delta):
    if deck in _SMP_SCRUB_CC:
        d = int(max(-63, min(63, delta)))
        _smp_cc(_SMP_SCRUB_CC[deck], d if d >= 0 else d + 128)

def sampler_stop(deck):
    """Hard stop a deck (silence + rewind). The obvious STOP button."""
    if deck in _SMP_STOP_NOTE:
        _smp_note_on(_SMP_STOP_NOTE[deck], 127)

def master_stop():
    """True panic / master stop (2026-07-19).

    Halts every INTERNAL sound source that can keep going on its own -- the
    step sequencer, the arpeggiator, and any latched sampler loop -- then
    flushes hanging notes. Reachable from the web PANIC button and the Fire's
    SHIFT+STOP, so the rig can be silenced from either side no matter what
    started it playing (before this, the web PANIC only did note-off on the
    chord channels, so a running seq/arp/loop could only be killed by
    restarting the whole service).

    Each step is guarded independently so a failure in one still runs the
    rest. Step 5 DOES now send CC123 across every synth-bus channel (added
    2026-07-30): stopping each source flushes only the notes that source knows
    about, and a voice stranded by a bug was unreachable by any other means --
    which is why PANIC used to require restarting the service."""
    # 1. Stop the step sequencer transport. seq.stop() also sends its own
    #    all-notes-off for the notes it was playing; reset rewinds to step 0.
    try:
        if seq:
            seq.stop(reset=True)
    except Exception as e:
        print(f"[master_stop] seq: {e}", flush=True)
    # 2. Disable the arpeggiator and release the notes it's currently holding
    #    (set_enabled(False) releases held notes internally), then drop the
    #    current chord so it can't resume generating on the next clock pulse.
    try:
        if arp:
            arp.set_enabled(False)
            arp.clear_chord()
    except Exception as e:
        print(f"[master_stop] arp: {e}", flush=True)
    # 3. Hard-stop both sampler decks (silence + rewind any latched loop) and
    #    abort any armed forward capture so a PANIC can't leave a Timer pending.
    try:
        for _deck in ("A", "B"):
            sampler_capture_fwd_cancel(_deck)
            sampler_stop(_deck)
    except Exception as e:
        print(f"[master_stop] sampler: {e}", flush=True)
    # 4. Existing PANIC cleanup: all-notes-off on the chord-engine channels
    #    and clear the held/prev note bookkeeping.
    try:
        all_notes_off()
        if arp:
            arp._release_all_held()
        state["held_notes"] = []
        state["prev_notes"] = []
    except Exception as e:
        print(f"[master_stop] notes: {e}", flush=True)
    # 5. Silence the INTERNAL SYNTH BUS (2026-07-30). all_notes_off() above
    #    only reaches external ports built from cfg.PRESETS -- it has never
    #    sent anything to the synth, and the synth did not implement CC123
    #    either, so a note stranded on the poly/chip engines survived PANIC
    #    entirely and could only be cleared by restarting the service. The
    #    synth treats CC120/CC123 on any of its channels as a global panic;
    #    one message is enough, but every synth channel is covered so this
    #    still works if that handler is ever made per-channel.
    try:
        _synth_mirror_off()          # drop mirror bookkeeping too
        for _ch1 in sorted(_SYNTH_CHS.keys()) or [8]:
            send_synth_midi(mido.Message(
                'control_change', channel=_ch1 - 1, control=123, value=0))
        print("  PANIC -- synth bus all-notes-off", flush=True)
    except Exception as e:
        print(f"[master_stop] synth bus: {e}", flush=True)

def sampler_cue(deck, index):
    """Settable hotcue press (index 1..4): SET the cue at the live playhead if
    it is empty, else JUMP to it and play -- the synth deck decides which.
    The op travels as the note velocity (1..4 = set-or-jump)."""
    if deck in _SMP_CUE_NOTE:
        _smp_note_on(_SMP_CUE_NOTE[deck], max(1, min(4, int(index))))

def sampler_cue_clear(deck, index):
    """Clear (forget) hotcue `index` (1..4).  Velocity 5..8 tells the synth to
    clear rather than set-or-jump the same cue."""
    if deck in _SMP_CUE_NOTE:
        _smp_note_on(_SMP_CUE_NOTE[deck], max(1, min(4, int(index))) + 4)

def sampler_scratch(deck, on):
    """Engage/disengage vinyl scratch mode (finger on/off the platter)."""
    if deck in _SMP_SCRATCH_NOTE:
        if on:
            _smp_note_on(_SMP_SCRATCH_NOTE[deck], 127)
        else:
            send_synth_midi(mido.Message("note_off", channel=_CH_SAMPLER - 1,
                                         note=_SMP_SCRATCH_NOTE[deck], velocity=0))

def sampler_reverse(deck):
    """Toggle backwards playback on a deck (the record spins the other way).

    A latch, like sampler_loop_toggle: the synth owns the actual flag, so
    the controller never has to guess the current direction and the web page
    and the hardware can never disagree about it."""
    if deck in _SMP_REVERSE_NOTE:
        _smp_note_on(_SMP_REVERSE_NOTE[deck], 127)


def sampler_crossfade(value_0_127):
    _smp_cc(_SMP_XF_CC, value_0_127)

def sampler_master_gain(value_0_127):
    """Master output level for the whole sampler mix (both decks)."""
    _smp_cc(_SMP_MASTER_GAIN_CC, value_0_127)

def sampler_eq(deck, band, value_0_127):
    """band in 'hi'|'mid'|'low'. Raw CC passthrough -- orc1_synth.py decodes
    0-127 into a +/-15dB shelf/peak gain, 64 = flat, matching the "engine
    encodes, synth decodes" split used by every other sampler control."""
    cc_map = {"hi": _SMP_EQ_HI_CC, "mid": _SMP_EQ_MID_CC,
              "low": _SMP_EQ_LOW_CC}.get(band)
    if cc_map and deck in cc_map:
        _smp_cc(cc_map[deck], value_0_127)

def sampler_filter(deck, value_0_127):
    """Raw CC passthrough -- 64 = bypass; <64 sweeps a low-pass down, >64
    sweeps a high-pass up (decoded in orc1_synth.py)."""
    if deck in _SMP_FILTER_CC:
        _smp_cc(_SMP_FILTER_CC[deck], value_0_127)

def sampler_bpm():
    """Current effective BPM: external clock estimate when slaved, else the
    internal/drift BPM. Falls back to 120 with no arp."""
    try:
        if arp:
            return float(arp._estimated_bpm())
    except Exception:
        pass
    return 120.0

def sampler_beat_phase():
    """(seconds_since_last_beat, beat_seconds) locked to the live clock grid,
    or None when no clock is running. Used by the Mixtrack metronome LED so it
    phase-locks to the master clock instead of free-running off a jittery BPM
    estimate. See ArpEngine.beat_phase()."""
    try:
        if arp:
            return arp.beat_phase()
    except Exception:
        pass
    return None

def _smp_send28(cc_list, value):
    """Send a 28-bit value as four 7-bit CCs, big-endian (cc_list = B3..B0)."""
    value = max(0, min((1 << 28) - 1, int(value)))
    for i, cc in enumerate(cc_list):
        _smp_cc(cc, (value >> (7 * (3 - i))) & 127)


def sampler_bar_phase(beats_per_bar=None):
    """(seconds_since_last_barline, bar_seconds) from the best live grid:
    the sequencer's when it's running (its downbeat IS the music's), else
    the arp's PPQN grid (arbitrary phase but consistent between grabs).
    None if no clock anchor exists yet.

    2026-07-20 (Bug-1 drift fix): `since` is rebased onto the AUDIO clock
    via the grid's downbeat ring-head mark (_AudioBarAnchor) whenever a
    fresh mark exists; the software value is only the fallback. NOTE: the
    downbeat marks fire on the DEFAULT bar grid (_SMP_BEATS_BAR, wired at
    startup) — callers passing a different beats_per_bar get the software
    fallback via the anchor's bounds checks. Every current caller uses the
    default."""
    p = _sampler_bar_phase_ex(beats_per_bar)
    return (p[0], p[1]) if p else None


def _sampler_bar_phase_ex(beats_per_bar=None):
    """Like sampler_bar_phase but returns (since, bar_s, anchored) where
    `anchored` says whether `since` came from the audio domain. The capture
    path needs the flag because anchored offsets take a different latency
    constant than software ones (ANCHOR_LATENCY_MS vs CAPTURE_LATENCY_MS —
    see _smp_offset_corrected)."""
    bpb = int(beats_per_bar or _SMP_BEATS_BAR)
    sw = anchor = None
    try:
        if seq:
            p = seq.bar_phase(steps_per_bar=bpb * 4)
            if p:
                sw, anchor = p, _smp_seq_anchor
    except Exception:
        pass
    if sw is None:
        try:
            if arp:
                p = arp.bar_phase(beats_per_bar=bpb)
                if p:
                    sw, anchor = p, _smp_arp_anchor
        except Exception:
            pass
    if sw is None:
        return None
    since, bar_s = sw
    try:
        au = anchor.since_seconds(bar_s)
    except Exception:
        au = None
    return (au, bar_s, True) if au is not None else (since, bar_s, False)


def _smp_offset_corrected(offset_s, anchored):
    """The synth subtracts CAPTURE_LATENCY_MS from every offset it receives
    (one constant, one code path). Audio-anchored offsets cancel the head-
    estimate bias that constant compensates, so they need ANCHOR_LATENCY_MS
    instead (measured ~-9 vs -99; anchored grabs landed ~95ms early without
    this). The engine shifts the SENT offset by (CAPTURE - ANCHOR) so the
    synth's single subtraction nets to the right constant for the path.
    Software-clock offsets pass through untouched (old behavior exactly)."""
    if not anchored:
        return offset_s
    return max(0.0, offset_s + (_SMP_CAPTURE_LAT_S - _SMP_ANCHOR_LAT_S))


def sampler_capture_bars(deck, bars, beats_per_bar=None):
    """Live-loop bar-grab. Length is always N bars at the live tempo, sent to
    the synth in exact FRAMES.

    ANCHOR (config CAPTURE_ANCHOR, 2026-08-18):
      "press" (default) -- the window ends at NOW, the player's PRESS, reaching
        back N bars. The press IS the downbeat. This bypasses the ORC bar grid
        entirely, because the grid's phase has no fixed relation to the music's
        downbeat -- the "don't know what segment it grabs" complaint. Fires an
        immediate end-now frame grab (offset 0); the synth applies the
        press-path latency (PRESS_CAPTURE_LATENCY_MS, live-tunable by ear).
      "grid" -- the old barline-quantized behaviour: the window ends at a
        BARLINE of the live grid (sequencer when running, else the arp PPQN
        grid). Pressing within QUANTIZE_SNAP of the next barline waits for it;
        grabs fire at least QUANTIZE_MARGIN after their barline so the audio has
        reached the ring; playback starts phase-aligned. Falls back to an
        immediate end-now grab when no clock is running."""
    if deck not in _SMP_CAP_FRA_CC:
        return
    bpb    = int(beats_per_bar or _SMP_BEATS_BAR)
    bpm    = max(20.0, min(300.0, sampler_bpm()))
    secs   = float(bars) * bpb * 60.0 / bpm
    frames = max(1, int(round(secs * _SMP_RING_RATE)))

    if _SMP_CAPTURE_ANCHOR != "grid":
        # PRESS-anchored: end the window at the press (now), length N bars.
        # offset 0 selects the synth's end-now/press path + press latency.
        _fwd_fire(deck, 0, frames)
        return

    anchor = _sampler_bar_phase_ex(bpb) if _SMP_QUANTIZE else None
    if not anchor:
        _smp_send28(_SMP_CAP_FRA_CC[deck], frames)   # offset 0 = end at head
        return
    since, bar_s, anchored = anchor
    if bar_s - since <= bar_s * _SMP_QSNAP:
        # Anticipated the upcoming downbeat -> wait for it (+ ring margin).
        delay = (bar_s - since) + _SMP_QMARGIN_S
    elif since < _SMP_QMARGIN_S:
        # Barline just passed; its audio may not be in the ring yet.
        delay = _SMP_QMARGIN_S - since
    else:
        _smp_fire_quantized(deck, frames, since, anchored=anchored)
        return
    threading.Timer(delay, _smp_fire_quantized,
                    args=(deck, frames, None, bpb)).start()


def _smp_fire_quantized(deck, frames, offset_s, beats_per_bar=None,
                        anchored=False):
    """Send offset (ms since the target barline) then length (frames; fires).
    offset_s None = deferred grab -> re-read the grid now for exactness.
    `anchored` = offset_s was measured on the audio clock (different latency
    constant — see _smp_offset_corrected)."""
    if offset_s is None:
        p = _sampler_bar_phase_ex(beats_per_bar)
        offset_s, anchored = (p[0], p[2]) if p else (_SMP_QMARGIN_S, False)
    offset_s = _smp_offset_corrected(max(0.0, offset_s), anchored)
    # One journal line per quantized grab — cheap, and tells you at a glance
    # whether the audio anchor was used (anchored=True) or the grab fell
    # back to the software clock, plus the anchor's health (mark age).
    try:
        _a = _smp_seq_anchor if (seq and getattr(seq, "_running", False)) \
             else _smp_arp_anchor
        _m = _a._mark
        print(f"[grab] deck={deck} anchored={anchored} "
              f"offset={offset_s*1000.0:.1f}ms "
              f"mark_age={'none' if _m is None else round(time.monotonic()-_m[1], 2)} "
              f"head={'ok' if _a._head_now() is not None else 'DOWN'}")
    except Exception as _e:
        print("[grab] dbg failed:", _e)
    _smp_send28(_SMP_CAP_OFF_CC[deck], int(round(offset_s * 1000.0)))
    _smp_send28(_SMP_CAP_FRA_CC[deck], frames)


def sampler_nudge(deck, ms=None, beats=None):
    """Shift a deck's capture window in the ring and re-slice — the 'fix the
    grab by ear' control. Positive = later (toward now). `beats` is converted
    at the live tempo. Sent as 10ms ticks on the relative nudge CC (chunked
    to fit the +/-63 range)."""
    if deck not in _SMP_NUDGE_CC:
        return
    if beats is not None:
        bpm = max(20.0, min(300.0, sampler_bpm()))
        ms  = float(beats) * 60000.0 / bpm
    ticks = int(round(float(ms or 0) / 10.0))
    while ticks:
        step  = max(-63, min(63, ticks))
        ticks -= step
        _smp_cc(_SMP_NUDGE_CC[deck], step if step >= 0 else step + 128)


# ── Forward capture ─────────────────────────────────────────────────────────
# All of this is a brain-side SCHEDULER around the EXISTING unquantized grab
# CCs (_SMP_CAP_OFF_CC / _SMP_CAP_FRA_CC). The target window is always
# [press, press+length]; the synth produces it by grabbing "the past `length`
# ending now" -- so we just fire that at the right wall-clock moment. No new
# ring/DSP code, and it never touches the ORC bar phase (that is the whole
# point -- the PRESS is the downbeat). See the spec in _planning/.
_fwd_timers = {}   # deck -> threading.Timer (armed fixed grab / snapped-up hold)
_fwd_press  = {}   # deck -> monotonic press time while a HOLD is in progress
# Guards the two dicts above. The forward facades are driven from three threads
# -- the Mixtrack MIDI-input thread, the cmd-socket thread, and Timer fire
# threads -- so the cancel-then-arm in _fwd_schedule must be atomic or two
# schedules on one deck can each start a Timer and leak one (only the last is
# recorded, so a later cancel/PANIC can't stop the other). RLock: hold_end and
# hold_start call _fwd_cancel/_fwd_schedule while already holding it.
_fwd_lock   = threading.RLock()


def _fwd_snap_length(hold_s, bar_s, snap_on, min_bars=1):
    """Round a HOLD duration (s) to a whole number of bars when snapping is on.
    Never returns fewer than min_bars bars, so a sub-bar hold still yields a
    clean one-bar loop rather than a stub. With snapping off (or no tempo) the
    raw hold is used unchanged (never negative)."""
    if not snap_on or bar_s <= 0:
        return max(0.0, hold_s)
    bars = max(int(min_bars), int(round(hold_s / bar_s)))
    return bars * bar_s


def _fwd_capture_plan(length_s, elapsed_s, ring_rate):
    """Forward-capture scheduler math (PURE -- unit-tested via ast).

    Target window is [press, press+length_s]; `elapsed_s` is how long ago the
    press was at the moment this runs. Returns (delay_s, offset_ms, frames):
      delay_s   -- wait this long, THEN fire (>= 0).
      offset_ms -- ms back-from-now for the window END at fire time (the synth
                   applies capture latency downstream; this stays pure wall
                   clock). Only meaningful when delay_s == 0.
      frames    -- window length in ring frames.
    If the window end is still in the future (elapsed < length) we wait the
    remainder and grab ending-now (offset 0). If it already passed (a
    snapped-DOWN hold) we fire now and reach back offset = (elapsed - length)."""
    frames    = max(1, int(round(max(0.0, length_s) * ring_rate)))
    remaining = length_s - elapsed_s
    if remaining > 0:
        return (remaining, 0, frames)
    return (0.0, int(round(-remaining * 1000.0)), frames)


def _fwd_bar_s():
    """Seconds per bar at the live tempo (same bpm source as the grab path)."""
    bpm = max(20.0, min(300.0, sampler_bpm()))
    return _SMP_BEATS_BAR * 60.0 / bpm


def _fwd_fire(deck, offset_ms, frames):
    """Emit one forward grab: OFFSET first (always, even 0, so a prior grab's
    offset can't linger) then FRAMES (the trigger). Mirrors _smp_fire_quantized
    ordering."""
    if deck not in _SMP_CAP_FRA_CC:
        return
    _smp_send28(_SMP_CAP_OFF_CC[deck], max(0, int(offset_ms)))
    _smp_send28(_SMP_CAP_FRA_CC[deck], max(1, int(frames)))


def _fwd_schedule(deck, length_s, elapsed_s):
    """Plan the window and either arm a Timer (end in the future) or fire now
    (end already passed). Never stacks two captures on one deck. The cancel +
    arm is done under _fwd_lock so a concurrent schedule on the same deck can't
    leak an untracked Timer; the immediate fire (delay==0) runs OUTSIDE the
    lock so a MIDI send never blocks another thread's schedule."""
    delay, offset_ms, frames = _fwd_capture_plan(length_s, elapsed_s, _SMP_RING_RATE)
    with _fwd_lock:
        _fwd_cancel(deck)
        if delay > 0:
            t = threading.Timer(delay, _fwd_fire, args=(deck, 0, frames))
            t.daemon = True
            _fwd_timers[deck] = t
            t.start()
            return
    _fwd_fire(deck, offset_ms, frames)


def sampler_capture_fwd_fixed(deck, bars):
    """TAP: capture a fixed N bars FORWARD -- now (the press) is the downbeat,
    length = N bars at the live tempo. The sample lands after N bars."""
    if deck not in _SMP_CAP_FRA_CC:
        return
    _fwd_schedule(deck, float(bars) * _fwd_bar_s(), 0.0)


def sampler_capture_fwd_hold_start(deck):
    """HOLD press: mark the downbeat (now). Finalized by _hold_end on release."""
    if deck not in _SMP_CAP_FRA_CC:
        return
    with _fwd_lock:
        _fwd_cancel(deck)
        _fwd_press[deck] = time.monotonic()


def sampler_capture_fwd_hold_end(deck, tap_bars=None):
    """Release of a forward tap-or-hold gesture. The window always STARTS at the
    press (marked by _hold_start); this only decides its LENGTH:

      - press shorter than _FWD_HOLD_MS and `tap_bars` given  -> TAP: fixed
        `tap_bars` bars (the quick-tap gesture, length from tempo).
      - otherwise                                             -> HOLD: record
        length = the held duration, snapped to whole bars (_FWD_SNAP).

    `tap_bars=None` (a web/direct caller with no pad) always takes the HOLD
    branch. No-op if no press is pending (cancelled by a mode flip / PANIC)."""
    with _fwd_lock:
        t0 = _fwd_press.pop(deck, None)
    if t0 is None or deck not in _SMP_CAP_FRA_CC:
        return
    hold_s = max(0.0, time.monotonic() - t0)
    if tap_bars is not None and hold_s * 1000.0 < _FWD_HOLD_MS:
        length_s = float(tap_bars) * _fwd_bar_s()          # TAP -> fixed N bars
    else:
        length_s = _fwd_snap_length(hold_s, _fwd_bar_s(), _FWD_SNAP, _FWD_MIN_BARS)
    _fwd_schedule(deck, length_s, hold_s)


def _fwd_cancel(deck):
    """Drop any armed Timer and any pending hold press for `deck`. Takes
    _fwd_lock (reentrant: callers _fwd_schedule/_hold_start already hold it)."""
    with _fwd_lock:
        t = _fwd_timers.pop(deck, None)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        _fwd_press.pop(deck, None)


def sampler_capture_fwd_cancel(deck):
    """Public: abort an armed/pending forward capture (mode flip, stop, PANIC)."""
    _fwd_cancel(deck)


def sampler_press_trim(ms):
    """Live-tune where a PRESS-anchored bar-grab's window ends, in ms (clamped
    <= 0). 0 = end on the newest recorded sample; NEGATIVE reaches further back.
    Source-dependent, so tuned by ear; the synth persists it. Sent as CC 0..127
    (= ms + 127) on CH_SAMPLER."""
    v = int(max(-127, min(0, int(round(float(ms))))) + 127)
    _smp_cc(_SMP_PRESS_LAT_CC, v)


def sampler_onset_snap(on):
    """Enable/disable onset-snap: after a PRESS grab, the synth refines the loop
    START onto the nearest strong transient. >=64 = on. The synth persists it."""
    _smp_cc(_SMP_ONSET_CC, 127 if on else 0)


def sampler_set_source(deck, track_id):
    """Point a deck's GRAB at a skipback track (0-63). Deck A defaults to t0
    (MAIN master), deck B to t1; either can grab either ring, so you can sample
    two different live streams at once. Sent as a CC on CH_SAMPLER whose VALUE is
    the track id; the sampler host repoints that deck's ring reader and persists
    the choice."""
    if deck in _SMP_DECK_SRC_CC:
        _smp_cc(_SMP_DECK_SRC_CC[deck], int(max(0, min(63, int(track_id)))))


# ── Skipback rings for controllers (2026-09-23) ─────────────────────────────
# The Mixtrack FX1/2/3 buttons became per-deck RING buttons (owner: easier to
# grab from rings other than MAIN). Ring order = the ARMED rings sorted by id, so
# FX1 is MAIN (t0) and the rest follow however many rings exist. Paths resolve
# at CALL time (never import time -- the test-sandbox rule).

def skipback_rings():
    """[{"id", "name", "store"}] of the ARMED skipback rings: RAM rings first,
    then CARD rings, each by id. [] if the tracks config is missing/unreadable
    (never raises into a controller).

    RAM first (2026-09-29, owner): the Mixtrack's FX1-3 pick a deck's grab
    source from the first three of these, and a sampler grab from a CARD ring
    lands early whenever the SD card stalls the writer (the grab can only see
    audio already in ring.pos, not the RT cushion) -- so the controller offers
    the rings that grab cleanly first. The /sampler page orders the same way
    (store comes from the same tracks file)."""
    try:
        with open(os.path.expanduser("~/.orc1_skipback_tracks.json")) as f:
            tracks = json.load(f).get("tracks", [])
        out = [{"id": int(t["id"]), "name": str(t.get("name") or f"T{t['id']}"),
                "store": "card" if t.get("store") == "card" else "ram"}
               for t in tracks
               if isinstance(t, dict) and t.get("armed", True) and "id" in t]
        return sorted(out, key=lambda r: (r["store"] == "card", r["id"]))
    except Exception:
        return []


def sampler_deck_source(deck):
    """The ring (track id) a sampler deck currently grabs from, read from the
    file the sampler host persists on every change -- so a pick made on the web
    shows on the controller LEDs too. Defaults A->0, B->1 (the host's own)."""
    default = {"A": 0, "B": 1}.get(deck, 0)
    try:
        with open(os.path.expanduser("~/.orc1_sampler_deck_src.json")) as f:
            return int(json.load(f).get(deck, default))
    except Exception:
        return default


def skipback_save(track_id, seconds=60, done=None):
    """Save the last `seconds` of skipback ring `track_id` to ~/recordings --
    the same skipback.sh the /skipback page's per-track buttons use. Runs on a
    background thread (a long save takes seconds); `done(ok, filename)` is
    called when it finishes. Returns immediately."""
    # the press is NOW, not when the thread gets to spawn bash: the grab ends
    # here even if the recorder's card writer is stalled (skipback.sh)
    press_env = dict(os.environ, SKIPBACK_PRESS_TS=repr(time.time()))

    def _work():
        import subprocess
        ok, name = False, ""
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "skipback", "skipback.sh")
            r = subprocess.run(["bash", script, str(int(seconds)), "wav",
                                str(int(track_id))], env=press_env,
                               capture_output=True, text=True)  # no timeout:
            # it's a background thread, and killing a slow grab left a hidden
            # .part behind (review 2026-10-01); the script's waits are bounded
            out = (r.stdout or "").strip()
            if r.returncode == 0 and "Saved:" in out:
                ok, name = True, os.path.basename(out.split("Saved:", 1)[1].strip())
                print(f"  💾 skipback t{track_id}: saved last {seconds}s -> {name}",
                      flush=True)
            else:
                print(f"  ! skipback t{track_id} save failed: "
                      f"{(r.stderr or out).strip()[:200]}", flush=True)
        except Exception as e:
            print(f"  ! skipback t{track_id} save failed: {e}", flush=True)
        if done:
            try:
                done(ok, name)
            except Exception:
                pass
    threading.Thread(target=_work, daemon=True, name="skipback-save").start()


_SKIPBACK_ALL_BUSY = threading.Event()


def skipback_save_all(seconds=0, tracks=None, done=None):
    """Sample-aligned grab of several skipback tracks into ONE folder in
    ~/recordings (skipback/skipback_grab_multi.py). seconds 0 = everything the
    chosen tracks hold in common; tracks None/[] = every recording track.
    Background thread like skipback_save; a second press while one is still
    writing is ignored (a pedal stomp twice must not queue two big grabs)."""
    if _SKIPBACK_ALL_BUSY.is_set():
        print("  ! skipback all-tracks save already running -- ignored", flush=True)
        return False
    _SKIPBACK_ALL_BUSY.set()
    press_env = dict(os.environ, SKIPBACK_PRESS_TS=repr(time.time()))

    def _work():
        import subprocess
        ok, name = False, ""
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "skipback", "skipback_grab_multi.py")
            # same stereo joins as the web GRAB ALL card (the web writes it)
            cmd = ["nice", "-n", "15", "ionice", "-c3", "python3", script,
                   str(max(0, int(seconds))), "--joins-file",
                   os.path.expanduser("~/.orc1_skipback_joins.json")]
            if tracks:
                cmd += ["--tracks", ",".join(str(int(t)) for t in tracks)]
            r = subprocess.run(cmd, env=press_env,      # no timeout: see above
                               capture_output=True, text=True)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and "Saved:" in out:
                ok, name = True, os.path.basename(out.split("Saved:", 1)[1].strip())
                print(f"  💾 skipback ALL tracks (aligned) -> {name}", flush=True)
            else:
                print(f"  ! skipback all-tracks save failed: "
                      f"{(r.stderr or out).strip()[:200]}", flush=True)
        except Exception as e:
            print(f"  ! skipback all-tracks save failed: {e}", flush=True)
        finally:
            _SKIPBACK_ALL_BUSY.clear()
        if done:
            try:
                done(ok, name)
            except Exception:
                pass
    threading.Thread(target=_work, daemon=True, name="skipback-save-all").start()
    return True


def module_cc(module, cc, value):
    """Send CC `cc` = `value` to MODULE on whatever channel /matrix binds it to
    right now (the module's own parameter numbers; e.g. the visuals renderer's
    knobs). Unbound = nothing sent, returns False. The named path a surface
    uses instead of a literal channel (see orc1_kikpad's VISUALS page)."""
    ch = _module_channel(module)
    if not ch:
        return False
    send_synth_midi(mido.Message("control_change", channel=int(ch) - 1,
                                 control=int(cc) & 0x7F,
                                 value=int(max(0, min(127, value)))))
    return True


def visuals_power(on):
    """Start / stop VISUALS mode on the monitor via daw/visuals.sh (the same
    script /system and /visuals use; its start refuses while DAW or DJ mode
    holds the screen). Background thread: a start takes seconds."""
    act = "start" if on else "stop"

    def _work():
        import subprocess
        sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daw", "visuals.sh")
        try:
            r = subprocess.run(["sudo", sh, act], capture_output=True, text=True,
                               timeout=30)
            msg = (r.stdout or r.stderr or "").strip()[:160]
            print(f"  visuals {act}: rc={r.returncode} {msg}", flush=True)
        except Exception as e:
            print(f"  ! visuals {act}: {e}", flush=True)
    threading.Thread(target=_work, daemon=True, name="visuals-power").start()


def sampler_set_anchor(mode):
    """Switch the bar-grab anchor at runtime. 'press' (default) ends the window
    at the player's press; 'grid' ends it on a barline of the live ORC clock
    (exact for material clocked TO the ORC, e.g. the BeatBuddy groove)."""
    global _SMP_CAPTURE_ANCHOR
    m = str(mode).lower()
    if m in ("press", "grid"):
        _SMP_CAPTURE_ANCHOR = m
        print(f"[sampler] capture anchor -> {m}", flush=True)


def sampler_loop_toggle(deck):
    """Latch play/stop on a deck (the synth flips the real gate state)."""
    if deck in _SMP_LOOP_NOTE:
        _smp_note_on(_SMP_LOOP_NOTE[deck], 127)

def sampler_audition(deck):
    """One-shot preview -- the /sampler PLAY button. Plays the deck's capture
    through once from the top and stops on its own (the synth's SampleDeck
    arms its _oneshot path). Distinct from LOOP, which latches a repeat."""
    if deck in _SMP_AUDITION_NOTE:
        _smp_note_on(_SMP_AUDITION_NOTE[deck], 127)

def sampler_export(deck):
    """Save the deck's current capture into the samples folder as a WAV
    (the synth process owns the buffer and does the write)."""
    if deck in _SMP_EXPORT_NOTE:
        _smp_note_on(_SMP_EXPORT_NOTE[deck], 127)


def sampler_seek(deck, value_0_127):
    """Needle-drop: jump the deck to strip position/127 of its buffer and
    play from there (declicked deck-side)."""
    if deck in _SMP_SEEK_CC:
        _smp_cc(_SMP_SEEK_CC[deck], value_0_127)


def sampler_bounds(deck, start=None, end=None):
    """Set a deck's play/loop region (fractions 0..1 of the buffer). start/end
    are optional -- omit one to leave that edge untouched (the /sampler drag
    moves one handle at a time). Sends the same CH_SAMPLER CCs a mapped
    controller would, so web and hardware share one path."""
    def _f(x):
        return int(round(min(max(float(x), 0.0), 1.0) * 127))
    if start is not None and deck in _SMP_START_CC:
        _smp_cc(_SMP_START_CC[deck], _f(start))
    if end is not None and deck in _SMP_END_CC:
        _smp_cc(_SMP_END_CC[deck], _f(end))


def sampler_beat_repeat_on(deck, beats=1.0):
    """Engage momentary BEAT-REPEAT on a deck: stutter a tight loop `beats`
    beats long at the live playhead (1.0 = a 1/4 note in 4/4, 0.5 = 1/8,
    0.25 = 1/16). The length in FRAMES is derived HERE from the live tempo +
    ring rate (bpm + sr only -- the synth owns the buffer) and sent as a 28-bit
    value; then a BR note-on starts the stutter with that pending length. The
    synth deck saves its prior region and restores it on release. Pair with
    sampler_beat_repeat_off() -- it is a momentary hold, not a latch."""
    if deck not in _SMP_BR_NOTE or deck not in _SMP_BR_LEN_CC:
        return
    bpm    = max(20.0, min(300.0, sampler_bpm()))
    frames = max(2, int(round(max(0.0, float(beats)) * 60.0 / bpm * _SMP_RING_RATE)))
    _smp_send28(_SMP_BR_LEN_CC[deck], frames)      # pending length (does NOT fire)
    _smp_note_on(_SMP_BR_NOTE[deck], 127)          # note-on = start the stutter


def sampler_beat_repeat_off(deck):
    """Release momentary beat-repeat: the synth restores the deck's prior
    play/loop region (a no-op there if nothing was engaged)."""
    if deck not in _SMP_BR_NOTE:
        return
    send_synth_midi(mido.Message("note_off", channel=_CH_SAMPLER - 1,
                                 note=_SMP_BR_NOTE[deck], velocity=0))


def sampler_pitch_bend(deck, up, on):
    """Momentary beatmatch nudge (PITCH +/- held): note-on engages the bend,
    note-off returns to the pre-bend speed."""
    notes = _SMP_BENDUP_NOTE if up else _SMP_BENDDN_NOTE
    if deck not in notes:
        return
    if on:
        _smp_note_on(notes[deck], 127)
    else:
        send_synth_midi(mido.Message("note_off", channel=_CH_SAMPLER - 1,
                                     note=notes[deck], velocity=0))


# ── File browser (hardware-driven: top encoder scrolls, WHEEL loads) ─────────
_smp_browse_idx  = {"A": 0, "B": 0}
_smp_file_cache  = {"ts": 0.0, "files": []}
_SMP_AUDIO_EXTS  = (".wav", ".mp3", ".flac", ".ogg", ".aif", ".aiff")


def _sampler_file_list():
    """Loadable files across SAMPLER["FILE_ROOTS"], sorted newest-first
    (same view as the web browser). 2s TTL cache — cheap at encoder rate."""
    now = time.time()
    if now - _smp_file_cache["ts"] > 2.0:
        entries = []
        for root in _SMP_FILE_ROOTS:
            root = os.path.expanduser(root)
            try:
                for n in os.listdir(root):
                    if n.lower().endswith(_SMP_AUDIO_EXTS):
                        p = os.path.join(root, n)
                        try:
                            entries.append((os.path.getmtime(p), p))
                        except OSError:
                            pass
            except OSError:
                pass
        entries.sort(key=lambda t: -t[0])
        _smp_file_cache["ts"] = now
        _smp_file_cache["files"] = [p for _, p in entries]
    return _smp_file_cache["files"]


def sampler_browse(deck, delta):
    """Scroll this deck's file-browser selection by delta (encoder ticks)."""
    files = _sampler_file_list()
    if not files:
        return None
    _smp_browse_idx[deck] = (_smp_browse_idx[deck] + int(delta)) % len(files)
    sel = files[_smp_browse_idx[deck]]
    print(f"  [MT] browse {deck}: {os.path.basename(sel)}")
    return sel


def sampler_browsed(deck):
    """Currently browsed filename for a deck (or None)."""
    files = _sampler_file_list()
    if not files:
        return None
    return files[_smp_browse_idx[deck] % len(files)]


def sampler_load_browsed(deck):
    """WHEEL button: load this deck's browsed file (cued)."""
    sel = sampler_browsed(deck)
    if sel:
        print(f"  [MT] load {deck} <- {os.path.basename(sel)}")
        sampler_load_file(deck, sel)


def sampler_load_file(deck, path):
    """File deck: load an audio file onto a deck (cued at 0). Validates the
    path against SAMPLER["FILE_ROOTS"] (recordings + samples folders), writes
    it to the per-deck load file, and fires NOTE_LOAD_FILE — the synth
    decodes on a worker thread and loads. Returns False on a rejected path."""
    if deck not in _SMP_LOAD_NOTE:
        return False
    try:
        real  = os.path.realpath(os.path.expanduser(str(path)))
        roots = [os.path.realpath(os.path.expanduser(r)) for r in _SMP_FILE_ROOTS]
        if not (os.path.isfile(real) and
                any(real == r or real.startswith(r + os.sep) for r in roots)):
            print(f"  ! sampler_load_file: rejected path {path}")
            return False
        dst = _SMP_LOAD_PATH.format(deck=deck)
        tmp = dst + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"path": real}, f)
        os.replace(tmp, dst)
        _smp_note_on(_SMP_LOAD_NOTE[deck], 127)
        return True
    except Exception as e:
        print(f"  ! sampler_load_file failed: {e}")
        return False


# ── Module Matrix: live per-module channel bindings ───────────────────────────
# A module (synth voice) is bound to a MIDI channel, moved, or taken out of the
# fold at will. cfg.SYNTH["CH_<...>"] stays the in-process single source of truth
# (mutated live) because BOTH the synth's listen dispatch AND the chord/arp mirrors
# below read a voice's channel from it -- so they can never diverge. The live map
# also lives here inverted as _bindings = {module_key: channel} (bound modules
# only; absent = out of the fold). Truth persists to ~/.orc1_bindings.json; the
# synth learns live changes via the CH_CTRL bind handshake (orc1_synth._route_midi).
# Design: _planning/ORC1_Module_Matrix_ROADMAP.md (§6/§6a) + _Slice1_Spec.md.
_MM_KEYS       = list(getattr(cfg, "MODULE_KEYS", []))
_MM_CH_KEY     = dict(getattr(cfg, "MODULE_CH_KEY", {}))
_MM_RESERVED   = (cfg.PROTECTED if hasattr(cfg, "PROTECTED")     # live: the /matrix PROTECTED CHANNELS setting
                  else set(getattr(cfg, "RESERVED_CHANNELS", {12, 13, 14, 15})))
_MM_NO_TAKEOUT = set(getattr(cfg, "MODULE_NO_TAKEOUT", {"sampler"}))


def _protect_err(ch):
    """'' or why ch can't be CHOSEN as a destination (the /matrix PROTECTED CHANNELS setting)."""
    f = getattr(cfg, "protect_error", None)
    return f(ch) if f else ""
_MM_ARM_CC     = int(getattr(cfg, "BIND_ARM_CC",   20))
_MM_CLEAR_CC   = int(getattr(cfg, "BIND_CLEAR_CC", 21))
_MM_COMMIT_NOTE = int(getattr(cfg, "BIND_COMMIT_NOTE", 1))
_MM_CTRL_CH    = int(getattr(cfg, "CH_CTRL", 13))
_BINDINGS_FILE = os.path.expanduser(getattr(cfg, "BINDINGS_FILE",
                                            "~/.orc1_bindings.json"))
_bindings = {}   # {module_key: channel}, bound modules only -- filled at boot

# P1/P2 JOIN (slice 2): EPHEMERAL live coupling of chip_p2 onto chip_p1. The DSP
# lives in orc1_synth.NESEngine; the brain only mirrors the state and re-emits
# the three shaping CCs on chip_p1's channel. Not persisted (like the level
# faders) -- a restart returns to mode 'none', so pulse2 is never left silent.
# chip_p2 stays BOUND throughout; "joined" is a display fact the page reads off
# _mm_join["mode"], never a change to the persisted binding table.
_MM_COUPLE_MODES  = list(getattr(cfg, "COUPLE_MODES",
                    ['none', 'unison', 'detune', 'fifth', 'third', 'octave', 'octave_dn']))
_MM_JOIN_MODE_CC   = int(getattr(cfg, "JOIN_MODE_CC",   72))
_MM_JOIN_DETUNE_CC = int(getattr(cfg, "JOIN_DETUNE_CC", 73))
_MM_JOIN_SPREAD_CC = int(getattr(cfg, "JOIN_SPREAD_CC", 76))
_mm_join = {"mode": "none", "detune": 0, "spread": False}

def _module_id(module):
    try:    return _MM_KEYS.index(module)
    except ValueError: return None

def _module_channel(module):
    """The channel a module currently listens on, or None if out of the fold."""
    return _bindings.get(module)

def _rebuild_synth_chs():
    """Rebuild the channels the brain forwards to the synth bus from the live
    bindings (replaces the old fixed cfg.SYNTH sweep)."""
    global _SYNTH_CHS
    _SYNTH_CHS = {int(ch): True for ch in _bindings.values()}

def _save_module_bindings():
    """Persist the live map atomically as {channel: module} (the synth's form)."""
    try:
        data = {"bindings": {str(ch): m for m, ch in _bindings.items()}}
        tmp = _BINDINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())            # durable file before the rename
        os.replace(tmp, _BINDINGS_FILE)
        try:                                # durable directory entry (SD-card power-loss)
            dfd = os.open(os.path.dirname(_BINDINGS_FILE) or ".", os.O_RDONLY)
            try:    os.fsync(dfd)
            finally: os.close(dfd)
        except Exception:
            pass
    except Exception as e:
        print(f"  [bindings save] {e}")

def _mm_seed_from_cfg():
    """First-run map = the shipped channels in cfg.SYNTH (+ the sampler)."""
    seed = {}
    for m, k in _MM_CH_KEY.items():
        if m == "sampler":
            seed[m] = int(_CH_SAMPLER)
        else:
            v = cfg.SYNTH.get(k)
            if v:
                seed[m] = int(v)
    return seed

def _mm_push_cfg(module, channel):
    """Mirror a module's channel into the in-process source of truth so the
    chord/arp mirrors and the synth stay in step. Sampler is special -- its
    facade emits on _CH_SAMPLER, not cfg.SYNTH."""
    global _CH_SAMPLER
    if module == "sampler":
        _CH_SAMPLER = channel
    else:
        k = _MM_CH_KEY.get(module)
        if k:
            cfg.SYNTH[k] = channel

def _load_module_bindings():
    """Restore ~/.orc1_bindings.json -> _bindings. Absent -> seed from cfg and
    write the file (so the synth reads the same). Corrupt -> seed, box still
    boots. Pushes loaded channels into the in-process source of truth."""
    global _bindings
    seed = _mm_seed_from_cfg()
    try:
        with open(_BINDINGS_FILE) as f:
            raw = json.load(f).get("bindings", {})
        loaded = {}
        for c, m in raw.items():
            ci = int(c)
            if 1 <= ci <= 16 and ci not in _MM_RESERVED and m in _MM_KEYS:
                loaded[m] = ci                # invert file's channel->module
        if loaded:
            _bindings = loaded
            for m, ch in _bindings.items():
                _mm_push_cfg(m, ch)
        else:
            _bindings = seed
    except FileNotFoundError:
        _bindings = seed
        _save_module_bindings()
    except Exception as e:
        print(f"  [bindings load] {e}")
        _bindings = seed
    _rebuild_synth_chs()
    print(f"  Module bindings: {dict(sorted(_bindings.items()))}")

def _mm_emit_arm_commit(module, channel):
    """Tell the synth to (re)bind: arm the module on the control channel, then a
    commit note whose CHANNEL is the target. Back-to-back on the reliable
    internal bus; the synth consumes the commit note (it never sounds)."""
    mid = _module_id(module)
    if mid is None:
        return
    send_synth_midi(mido.Message("control_change", channel=_MM_CTRL_CH - 1,
                                 control=_MM_ARM_CC, value=mid))
    send_synth_midi(mido.Message("note_on", channel=channel - 1,
                                 note=_MM_COMMIT_NOTE, velocity=1))

def _mm_emit_clear(module):
    mid = _module_id(module)
    if mid is None:
        return
    send_synth_midi(mido.Message("control_change", channel=_MM_CTRL_CH - 1,
                                 control=_MM_CLEAR_CC, value=mid))

# Modules whose notes SUSTAIN until a note-off (the ones a rebind can strand).
# Drums / bbkit are one-shots; the sampler's notes are deck commands (a note-off
# sweep there would release its gates), and visuals has no voices.
_MM_SUSTAINING = ("poly", "chip_p1", "chip_p2", "chip_tri", "chip_noise",
                  "chip_noise2", "chip_pcm", "gb_wave", "mono")


def _release_module_notes(module):
    """Release whatever `module` is sounding, on its OLD channel, while its host
    still listens there -- call BEFORE the binding moves (2026-10-01 review:
    held notes got their note-off on the old channel after a rebind, which the
    host no longer reads = stuck notes).

    NOT CC123/CC120: every host treats those as a GLOBAL panic on any channel
    (the chip host silences all chip voices, the sampler host stops both
    decks), so a /matrix move would cut the whole rig. Instead: a note-off for
    every note on that one channel -- only the module bound there reacts, and it
    also covers notes the brain never tracked (a DIN keyboard passed through).
    Then the brain-side held state for that channel is forgotten, so a later
    release can't land on whatever gets bound there next."""
    old = _bindings.get(module)
    if not old or module not in _MM_SUSTAINING:
        return
    ch0 = old - 1
    for n in range(128):
        send_synth_midi(mido.Message("note_off", channel=ch0, note=n, velocity=0))
    state["held_notes"] = [(n, pk, c) for (n, pk, c) in state.get("held_notes", [])
                           if not (pk == "synth" and c == old)]
    try:
        if arp is not None:
            with arp._lock:
                arp._held = [(n, pk, c) for (n, pk, c) in arp._held
                             if not (pk == "synth" and c == old)]
    except Exception as e:
        print(f"  ! release {module}: arp held: {e}", flush=True)
    try:
        if fire:
            fire.forget_synth_channel(old)
    except Exception as e:
        print(f"  ! release {module}: Fire held: {e}", flush=True)


def module_bind(module, channel):
    """Reassign `module` to `channel`, live + persisted. Rejects a collision with
    a different bound module, reserved channels, and unknown modules."""
    if module not in _MM_KEYS:
        print_status(f"MODULE_BIND: unknown module {module!r}"); return
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        print_status(f"MODULE_BIND: bad channel {channel!r}"); return
    if not (1 <= channel <= 16) or channel in _MM_RESERVED:
        print_status(f"MODULE_BIND: channel {channel} not bindable (protected)"); return
    holder = next((m for m, c in _bindings.items()
                   if c == channel and m != module), None)
    if holder is not None:
        print_status(f"MODULE_BIND: ch{channel} already held by {holder}"); return
    _synth_mirror_off()                 # release held mirror notes cleanly first
    if _bindings.get(module) != channel:
        _release_module_notes(module)   # ...and anything else it is sounding
    _bindings[module] = channel         # module->channel is 1:1, so this frees the old
    _mm_push_cfg(module, channel)
    _rebuild_synth_chs()
    _save_module_bindings()
    _mm_emit_arm_commit(module, channel)
    print_status(f"MODULE_BIND {module} -> ch{channel}")

def module_unbind(module):
    """Take `module` out of the fold (its channel goes dead). The sampler is the
    always-live deck-control channel and cannot be taken out."""
    if module in _MM_NO_TAKEOUT:
        print_status(f"MODULE_UNBIND: {module} is always live"); return
    if module not in _bindings:
        return
    _synth_mirror_off()
    _release_module_notes(module)       # while its host still listens there
    _bindings.pop(module, None)
    _rebuild_synth_chs()
    _save_module_bindings()
    _mm_emit_clear(module)
    print_status(f"MODULE_UNBIND {module}")


def _mm_join_mode_cc(mode):
    """Bucket a mode name to a CC72 value the synth decodes back to the same
    index. Mirrors orc1_fire._send_chip_coupling_cc: middle-of-bucket + 9."""
    idx = _MM_COUPLE_MODES.index(mode)
    return min(127, idx * 128 // len(_MM_COUPLE_MODES) + 9)


def module_join(mode=None, detune=None, spread=None):
    """Live, EPHEMERAL P1/P2 join. Updates only the fields provided and re-emits
    the matching coupling CC(s) on chip_p1's channel. chip_p1 must be in the
    fold (it is the driver). Not persisted -- see _mm_join."""
    ch = _module_channel("chip_p1")
    if not ch:
        print_status("MODULE_JOIN: chip_p1 is out of the fold -- nothing to drive")
        return
    ch0 = ch - 1
    parts = []
    if mode is not None:
        mode = str(mode)
        if mode not in _MM_COUPLE_MODES:
            print_status(f"MODULE_JOIN: unknown mode {mode!r}"); return
        _mm_join["mode"] = mode
        send_synth_midi(mido.Message("control_change", channel=ch0,
                                     control=_MM_JOIN_MODE_CC,
                                     value=_mm_join_mode_cc(mode)))
        parts.append(f"mode={mode}")
    if detune is not None:
        try:    cents = max(-50, min(50, int(detune)))
        except (TypeError, ValueError):
            print_status(f"MODULE_JOIN: bad detune {detune!r}"); return
        _mm_join["detune"] = cents
        cc_val = max(0, min(127, round(cents / 50.0 * 64.0) + 64))
        send_synth_midi(mido.Message("control_change", channel=ch0,
                                     control=_MM_JOIN_DETUNE_CC, value=cc_val))
        parts.append(f"detune={cents}c")
    if spread is not None:
        on = bool(spread)
        _mm_join["spread"] = on
        send_synth_midi(mido.Message("control_change", channel=ch0,
                                     control=_MM_JOIN_SPREAD_CC,
                                     value=127 if on else 0))
        parts.append(f"spread={'on' if on else 'off'}")
    if parts:
        print_status("MODULE_JOIN " + " ".join(parts))


def _synth_mirror_on(notes, vel):
    """Mirror chord notes to internal synth channels when coupling is active.
    Emits ONLY to a BOUND voice -- an unbound (taken-out) module must never be
    mirrored to, or its notes would fall through to the DIN passthrough and leak.
    Reads the channel from _bindings, the same source the synth listens on."""
    held = []
    for module, flag in (("poly", "synth_mirror_poly"),
                         ("chip_p1", "synth_mirror_chip")):
        if not state.get(flag):
            continue
        mirror_ch = _module_channel(module)      # None = out of the fold
        if not mirror_ch:
            continue
        ch0 = mirror_ch - 1
        for n in notes:
            send_synth_midi(mido.Message(
                "note_on", channel=ch0, note=n, velocity=vel))
            held.append((n, ch0))
    state["_mirror_held"] = held


def _synth_mirror_off():
    """Release any notes held by EITHER synth mirror.

    There are two mirror paths: the chord one (_synth_mirror_notes, which
    records a flat list in state["_mirror_held"]) and the arp one
    (_arp_note_on, per-note in state["_arp_mirror_held"]). Until 2026-07-30
    this only knew about the first, so arp-mirrored notes survived a mirror
    toggle and could hang."""
    for n, ch0 in state.get("_mirror_held", []):
        send_synth_midi(mido.Message(
            "note_off", channel=ch0, note=n, velocity=0))
    state["_mirror_held"] = []
    for n, chs in list(state.get("_arp_mirror_held", {}).items()):
        for ch0 in chs:
            send_synth_midi(mido.Message(
                "note_off", channel=ch0, note=n, velocity=0))
    state["_arp_mirror_held"] = {}


def toggle_synth_mirror(which):
    """Toggle synth mirror coupling. which = 'poly' or 'chip'."""
    key = f"synth_mirror_{which}"
    if key in state:
        _synth_mirror_off()  # clean up held notes before toggling
        state[key] = not state[key]
        on = state[key]
        print(f"  Synth mirror {which} {'ON' if on else 'OFF'}")


# -- Voice router --------------------------------------------------------------


def mirror_active():
    """True when chord output mirrors input channel -- driven by MIRROR_INPUT_CH CC toggle."""
    return state["mirror_input_ch"]


def _dest_channel(pk, ch):
    """A voice destination's channel -> int. On the internal synth bus a
    destination may NAME a module ("chip_p2"): it resolves from the live
    /matrix bindings, None when unbound -- the voice goes silent instead of
    landing on whatever now sits on the module's old channel (2026-09-24)."""
    if isinstance(ch, str):
        return _module_channel(ch) if pk == "synth" else None
    return ch


def resolve(voice_slot, mirror_ch=None):
    # Live voice map overrides take priority over the active preset.
    vm = state["voice_map"]
    if voice_slot in vm:
        entry = vm[voice_slot]
        if entry is None:
            return None, None   # explicitly silenced
        pk = entry.get("port")
        # A missing/null channel is SILENT, not ch 1 (2026-10-01): /routes
        # saved channel:null when its picker could not show the entry's
        # (unbound) module, and "ch 1" then played whatever sat there.
        c = entry.get("channel")
        ch = _dest_channel(pk, c) if c not in (None, "", 0) else None
        return (pk, ch) if pk and ch else (None, None)

    preset = cfg.PRESETS.get(state["preset"], {})
    dest   = preset.get(voice_slot)
    if dest is None:
        return None, None
    pk = dest["port"]
    ch = _dest_channel(pk, dest["channel"])   # a synth dest names its module
    # Chord output override: redirect non-synth destinations at runtime.
    # Internal synth slots (pk == "synth") are left alone so NES/chip presets
    # keep their internal routing.
    co = state["chord_out"]
    if co["port"] and pk != "synth":
        pk = co["port"]
        if co["channel"]:
            ch = co["channel"]
    if mirror_ch and mirror_active():
        if voice_slot == "BASS":
            if state["preset"] == "MONO_BASS2":
                ch = cfg.BASS_FIXED_CH
                pk = cfg.BASS_MIRROR_PORT
            elif state["preset"] == "MONO_BASS":
                ch = mirror_ch
                pk = cfg.BASS_MIRROR_PORT
        else:
            ch = mirror_ch
    if not ch:
        return None, None       # a named module that is not bound: silent
    return pk, ch


def send_chord_on(notes, bass, vel, mirror_ch=None):
    held   = []
    preset = cfg.PRESETS.get(state["preset"], {})
    vm = state["voice_map"]
    # voice_map can force individual-slot mode if any individual slot is overridden
    _IND_SLOTS = ["CHORD_ROOT","CHORD_3RD","CHORD_5TH","CHORD_7TH","CHORD_9TH"]
    vm_forces_individual = any(k in vm and vm[k] is not None for k in _IND_SLOTS)
    # use individual mode when:
    #   (a) vm explicitly sets any individual slot, OR
    #   (b) preset has CHORD_ALL=None and vm doesn't override CHORD_ALL
    use_individual = vm_forces_individual or (
        preset.get("CHORD_ALL") is None and "CHORD_ALL" not in vm
    )


    if not use_individual:
        pk, ch = resolve("CHORD_ALL", mirror_ch)
        if pk and ch:
            for n in notes:
                note_on_out(n, vel, pk, ch)
                held.append((n, pk, ch))
    else:
        slots = ["CHORD_ROOT","CHORD_3RD","CHORD_5TH","CHORD_7TH","CHORD_9TH"]
        for i, slot in enumerate(slots):
            if i >= len(notes):
                break
            pk, ch = resolve(slot, mirror_ch)
            if pk and ch:
                note_on_out(notes[i], vel, pk, ch)
                held.append((notes[i], pk, ch))


    if not use_individual and notes:
        pk, ch = resolve("LEAD", mirror_ch)
        if pk and ch:
            note_on_out(notes[-1], vel, pk, ch)
            held.append((notes[-1], pk, ch))


    pk, ch = resolve("BASS", mirror_ch)
    if pk and ch:
        note_on_out(bass, vel, pk, ch)
        held.append((bass, pk, ch))


    state["held_notes"] = held
    # Synth mirror: also send chord notes to internal synth if coupled.
    _synth_mirror_on(notes, vel)


def send_chord_off():
    for (n, pk, ch) in state["held_notes"]:
        note_off_out(n, pk, ch)
    state["held_notes"] = []
    _synth_mirror_off()


def _performve_feed(notes):
    """ADDITIVE Perform-VE follow: mirror the sounding chord to the 'performve'
    port on MIDI ch1 so its NaturalPlay HardTune re-keys to what we play.

    Releases the PREVIOUS feed first, so each new chord re-keys the box cleanly
    (and, if the box's own harmony voices are ever enabled, they don't pile up).
    A release of the chord is deliberately NOT propagated -- the box should HOLD
    the last key until the next chord, which is exactly what a key-lock wants.
    No-op unless state['performve_feed'] is on and the port exists."""
    if not state.get("performve_feed"):
        return
    for n in state.get("_pve_held", []):
        send_to("performve", mido.Message('note_off', channel=0, note=n, velocity=0))
    held = []
    for n in notes:
        if 0 <= n <= 127:
            send_to("performve", mido.Message('note_on', channel=0, note=n, velocity=64))
            held.append(n)
    state["_pve_held"] = held


def _performve_feed_off():
    """Release any notes held at the Perform-VE (panic / feed disabled)."""
    for n in state.get("_pve_held", []):
        send_to("performve", mido.Message('note_off', channel=0, note=n, velocity=0))
    state["_pve_held"] = []


def _performve_src_hit(ps, source_label, msg_type, msg_channel):
    """True if an incoming message should be CLAIMED for the Perform-VE input
    source -- forwarded to the box on ch1 and consumed.  Notes only; matches the
    configured source ('usb'|'din'|'both') and an optional 1-based channel filter
    (None = any channel).  Pure predicate so it can be unit-tested in isolation.
    CH_CTRL is never claimed -- same reservation as stage:"pre" rules
    (_pre_stage_channel_ok); this check runs before that guard, so it needs its own."""
    if not ps or not ps.get("port"):
        return False
    if msg_type not in ('note_on', 'note_off'):
        return False
    if msg_channel is not None and msg_channel + 1 == cfg.CH_CTRL:
        return False
    if ps["port"] != "both" and ps["port"] != source_label:
        return False
    psch = ps.get("channel")
    return psch is None or msg_channel == psch - 1


def _apply_performve_feed(on):
    """Set the Perform-VE follow toggle: update state, release any notes held at
    the box when disabling, and persist. Returns the resolved bool. (Factored out
    of the SET_PERFORMVE_FEED cmd branch so it is unit-testable.)"""
    on = bool(on)
    with _state_lock:
        state["performve_feed"] = on
    if not on:
        _performve_feed_off()   # drop any held notes when disabling
    _save_performve()
    return on


def _apply_performve_src(port, channel):
    """Set the Perform-VE input source: normalise port, update state, clear any
    note held at the box (CC123 all-notes-off on ch1), and persist. Returns the
    stored src dict. (Factored out of SET_PERFORMVE_SRC so it is unit-testable.)"""
    if port not in (None, "usb", "din", "both"):
        port = None
    with _state_lock:
        state["performve_src"] = {
            "port":    port,
            "channel": int(channel) if channel else None,
        }
    send_to("performve", mido.Message('control_change', channel=0,
                                      control=123, value=0))
    _save_performve()
    return state["performve_src"]


def _apply_performve_cc(cc, value):
    """Send one Control Change to the Perform-VE on its MIDI channel (ch1). The box
    is driven entirely by CCs (see its MIDI implementation, FINDINGS.md §3c); it does
    not report parameter state back over MIDI, so this is fire-and-forget. Returns the
    clamped (cc, value) actually sent, or None if either is out of range. (Factored out
    of the SET_PERFORMVE_CC cmd branch so it is unit-testable.)"""
    try:
        cc = int(cc); value = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= cc <= 127 and 0 <= value <= 127):
        return None
    send_to("performve", mido.Message('control_change', channel=0,
                                      control=cc, value=value))
    return (cc, value)


def _apply_performve_pc(preset):
    """Recall a Perform-VE preset via Program Change on ch1 (PC 0/1/2 = presets 1/2/3).
    Fire-and-forget. Returns the program sent, or None if out of range."""
    try:
        preset = int(preset)
    except (TypeError, ValueError):
        return None
    if not (0 <= preset <= 127):
        return None
    send_to("performve", mido.Message('program_change', channel=0, program=preset))
    return preset


def _pre_stage_channel_ok(ch1):
    """Whether a stage:"pre" rule may claim this (1-based) channel. It may claim
    ANY channel EXCEPT CH_CTRL -- the ORC's own control inbox stays reserved so the
    control surface always has a home (owner: "reclaim ch13 so messages have a
    place to go"). This is the ONE principled exception to routing-is-a-choice: it
    is infrastructure, not a default convenience binding. Everything else is free."""
    return ch1 != cfg.CH_CTRL


def _din_echo_blocked(source_label, out_port):
    """True if forwarding this output would echo a DIN-sourced message straight back
    out the DIN (pisound) -- the BSP-runaway feedback class this file was scarred by
    (one START multiplied to >1200). Such a send is dropped; the message is still
    consumed from channel dispatch (the pre-rule claimed it)."""
    return source_label == "din" and out_port == "pisound"


# SysEx that is a device talking to the PI, never music for the DIN gear. mido
# strips F0/F7, so each prefix is the head of msg.data.
#   00 20 6B     Arturia config/read-back (the /beatstep page reading the BSP)
#   7D 'O' 'R' 'C'  our own Fire firmware's host protocol (its hello reply comes
#                back every 3 s while orc1-firevu runs; wire-measured on the
#                pisound out 2026-10-13)
_DEVICE_SYSEX_PREFIXES = ((0x00, 0x20, 0x6B), (0x7D, 0x4F, 0x52, 0x43))


def _sysex_is_device_chatter(data):
    """True if a SysEx payload (msg.data) is device chatter that must not be
    forwarded to the pisound DIN out."""
    head = tuple(data[:4])
    return any(head[:len(p)] == p for p in _DEVICE_SYSEX_PREFIXES)


# -- Chord engine --------------------------------------------------------------


def degree_intervals(chord_root_pc, key_root_pc, scale_name, ext_steps=()):
    """Intervals FROM THE CHORD ROOT, stacked in the scale's own degrees.

    This is the O3 harmony model (2026-07-29).  A triad is degrees i, i+2, i+4 of
    the active scale; each extension adds a further rung (see CHORD_EXT_DEGREES).
    Because the notes come from the scale, the chord is in-key BY CONSTRUCTION --
    no per-scale table, no closest-shape approximation, and quality (major, minor,
    diminished, augmented, or something with no common name) simply falls out.

    Steps are SCALE steps, not semitones; divmod handles the octave wrap, which
    is what makes this work identically on 5-, 6-, 7- and 8-note scales.

    Returns None when there is no usable ladder -- unknown scale, or a chord root
    that is not in the key.  Callers then fall through to the fixed-shape path,
    which is the same shape of fallthrough the engine has always had.
    """
    # CHROM is NOT a ladder -- it contains every pitch class, so "scale steps"
    # are semitones and stacking thirds would yield (0,2,4), a whole-tone cluster,
    # not a triad.  CHROM means "no key constraint" everywhere else in this file
    # (scale_pitch_classes() returns None for it, snap_note() passes through), so
    # it must fall through to the fixed shapes here too -- which also preserves
    # its pre-O3 behaviour of the base pads being a PERSISTENT quality setting.
    # This matters more than it looks: CHROM is the boot default scale.
    if scale_name == "CHROM":
        return None
    iv = SCALES.get(scale_name)
    if not iv:
        return None
    n = len(iv)
    if n < 3:
        return None
    rel = (int(chord_root_pc) - int(key_root_pc)) % 12
    if rel not in iv:
        return None                       # chord root is not in the key
    deg = iv.index(rel)
    out = []
    for step in (0, 2, 4) + tuple(ext_steps):
        octs, i = divmod(deg + step, n)
        out.append(iv[i] + 12 * octs - rel)
    return sorted(set(out))


def _split_ext_steps(exts):
    """Partition the active extensions into (scale steps, chromatic semitones).

    state["chord_exts"] may hold BOTH vocabularies at once: the scale-degree
    names (CHORD_EXT_DEGREES -- plain ext pads) and the chromatic ones
    (CHORD_EXTS -- the SHIFT escape hatch, and every pre-O3 saved state / CC
    map).  Resolving per-name is what lets the two coexist.
    """
    steps, semis = [], []
    for ext in exts:
        if ext in CHORD_EXT_DEGREES:
            steps.append(CHORD_EXT_DEGREES[ext])
        else:
            semi = CHORD_EXTS.get(ext)
            if semi is not None:
                semis.append(semi)
    return steps, semis


def get_intervals(base_name, exts, chord_root=None):
    """The intervals that will actually sound.

    DIAT (the default): stack the scale's degrees under the chord root.
    FIXED: the historical fixed CHORD_BASE shape, unchanged.

    Two things select FIXED, and they are deliberately different (O4):
      * state["quality_mode"] == "FIXED" -- the LATCHED mode. The quality pads
        are a persistent setting and a key press does not disturb them.
      * state["base_override"] -- a MOMENTARY override inside DIAT, set by a
        base-pad press and cleared by the next key press.
    Before O4 only the second existed, so there was no way to ask for a
    persistent quality at all; that is why the pads felt dead.

    `chord_root` MUST be passed by anything building a chord on a root other
    than the live one -- notably Mixtrack deck B, which has its own root while
    sharing the global key.  Defaulting it to state would silently harmonise
    deck B's root against deck A's degree.  The existing len()-only call sites
    omit it and correctly get the live chord.
    """
    steps, semis = _split_ext_steps(exts)

    if state.get("quality_mode") != "FIXED" and not state.get("base_override"):
        ivs = degree_intervals(chord_root_now() if chord_root is None
                              else int(chord_root) % 12,
                              state["root"], state["scale"], steps)
        if ivs is not None:
            # A chromatic extension deliberately leaves the scale -- that is the
            # entire point of the escape hatch, so it is added as-is.
            return sorted(set(ivs) | set(semis))

    # FIXED path (also the fallthrough for an unknown scale / off-key root).
    ivs = set(CHORD_BASE.get(base_name, [0, 4, 7]))
    for ext in exts:
        semi = CHORD_EXTS.get(ext)
        if semi is None:
            # A degree ext with no ladder to stand on: keep it audible at its
            # plainest reading rather than silently dropping it.
            semi = CHORD_EXT_DEGREE_FALLBACK.get(ext)
        if semi is not None:
            ivs.add(semi)
    return sorted(ivs)


def chord_root_now():
    """The pitch class the chord is built on.

    Normally just what was pressed.  With KEY LOCK on, an off-key press is pulled
    to the nearest pitch class that IS in the key, so every pad yields an in-key
    chord instead of falling through to the fixed-shape path.

    KEY LOCK was REPURPOSED here (2026-07-29, O3).  It used to stop the keyboard
    from overwriting state["root"]; splitting root/chord_root means the keyboard
    can no longer touch the key at all, so the old job no longer exists and the
    toggle would have been left dead.  This is the job it was accidentally
    approximating.  ⚠ Flagged for owner to confirm -- see the plan doc, q3.
    """
    cr = int(state.get("chord_root", state["root"])) % 12
    if not state.get("key_lock"):
        return cr
    iv = SCALES.get(state["scale"])
    if not iv:
        return cr
    pcs = [(int(state["root"]) + i) % 12 for i in iv]
    if cr in pcs:
        return cr
    # Nearest in-key pitch class, measured circularly.  TIES GO UP.
    # ⚠ Note this DISAGREES with _snap_to_pcs() (the melody/SCALE-SNAP path),
    # which scans an ascending list with a strict `<` and therefore ties DOWN:
    # C# in C major goes to D here and to C there.  Left as-is because ties-up
    # matches MiniKeys' snapNearest and reads better for chord roots, but if the
    # two ever need to agree, this is the discrepancy.
    return min(pcs, key=lambda p: (min((p - cr) % 12, (cr - p) % 12),
                                   0 if (p - cr) % 12 <= 6 else 1))


def current_degree():
    """0-based scale degree of the chord root within the key, or None if the
    chord root is not in the key (or the scale has no ladder)."""
    iv = SCALES.get(state["scale"])
    if not iv:
        return None
    rel = (chord_root_now() - int(state["root"])) % 12
    return iv.index(rel) if rel in iv else None


def shape_name(intervals):
    """Display name for a stacked shape, or None when it has no common name.
    Never guesses -- an unnamed shape is reported as a degree instead."""
    if not intervals:
        return None
    return CHORD_SHAPE_NAMES.get(tuple(intervals))


def _ext_sort_key(e):
    """Order extensions for display.  Handles both vocabularies (scale-degree
    and chromatic) so a mixed set can't raise a KeyError -- which the old
    CHORD_EXTS[e] lookup would have done on any degree ext."""
    if e in CHORD_EXT_DEGREES:
        return CHORD_EXT_DEGREES[e]
    return CHORD_EXTS.get(e, 99)


def chord_name_str():
    """Name what will actually SOUND.

    In DIAT the notes come from the scale, so the shape is whatever the degree
    produced -- named from the intervals themselves.  A shape with no common
    name is reported as its roman-numeral degree rather than being forced into
    the nearest label (forcing labels is what DIATONIC_BASE did wrong).
    """
    exts = state["chord_exts"]
    ivs  = get_intervals(state["chord_base"], exts)
    name = shape_name(ivs)

    if name is None:
        # Unnamed stack: fall back to the degree, which is the useful readout in
        # DIAT anyway.  Only if that is unavailable too do we show the intervals.
        deg = current_degree()
        if deg is not None and deg < len(ROMAN_DEGREES):
            return ROMAN_DEGREES[deg]
        return "(" + ",".join(str(i) for i in ivs) + ")"

    if not exts:
        return name
    # The shape name already accounts for any rung the extensions added (maj7,
    # m7b5, ...), so only append the ones the name does not already imply.
    if len(ivs) > 3:
        return name
    return f"{name}+{'+'.join(sorted(exts, key=_ext_sort_key))}"


def effective_base():
    """The chord QUALITY LABEL that will sound -- display and pad-colour only.

    DIAT derives it from the notes the scale produced; FIXED returns the pad the
    performer chose.  Callers that need the real notes must use get_intervals();
    this is a label, and in DIAT it can legitimately be a shape (AUG, 6no5) that
    is not one of the four CHORD_BASE names.  Falls back to the stored base when
    the stack has no common name, so the Fire always has *something* to colour.
    """
    # Must mirror get_intervals()'s selector exactly, or the OLED/pad colours
    # describe a chord that isn't the one sounding.
    if state.get("quality_mode") == "FIXED" or state.get("base_override"):
        return state["chord_base"]
    ivs  = get_intervals(state["chord_base"], state["chord_exts"])
    name = shape_name(ivs)
    if name is not None:
        return name
    # Unnamed stack.  Returning the STORED chord_base here would be a lie: it is
    # never reset, so a HIRA (0,3,8) or a 9th would light the MAJ pad bright gold
    # and hide the web badge while something else entirely was sounding.  Report
    # the degree instead -- always a string, so colour lookups still .get() to a
    # neutral default, and never a quality that isn't playing.
    deg = current_degree()
    if deg is not None and deg < len(ROMAN_DEGREES):
        return ROMAN_DEGREES[deg]
    return "--"


def build_chord_notes(root_semi, base_name, exts, octave,
                      inversion, voicing, spread_cc, oct_offset):
    # Intervals are derived from the root actually being built, so a caller with
    # its own root (Mixtrack deck B) harmonises that root in the global key
    # rather than inheriting the live chord's scale degree.
    intervals = get_intervals(base_name, exts, chord_root=root_semi)
    base      = 12 * (octave + oct_offset) + root_semi
    notes     = [base + i for i in intervals]


    inv = inversion % len(notes)
    for _ in range(inv):
        notes = notes[1:] + [notes[0] + 12]


    if voicing == "OPEN":
        notes = [n + (12 if i % 2 == 1 else 0) for i, n in enumerate(notes)]
    elif voicing == "DROP2" and len(notes) >= 4:
        notes.sort(); notes[-2] -= 12
    elif voicing == "SPREAD":
        spread_semis = int(spread_cc / 127 * 24)
        if len(notes) > 1:
            step  = spread_semis // max(len(notes)-1, 1)
            notes = [notes[0] + i*step for i in range(len(notes))]


    return sorted([max(0, min(127, n)) for n in notes])


def get_all_inversions(notes):
    candidates, working = [], list(notes)
    for _ in range(len(notes)):
        candidates.append(list(working))
        working = working[1:] + [working[0]+12]
    more = []
    for c in candidates:
        more += [[x-12 for x in c], [x+12 for x in c]]
    candidates += more
    return [c for c in candidates if all(0 <= x <= 127 for x in c)]


def get_voice_positions(notes):
    """
    Return all valid voicings of `notes` sorted by ascending average pitch.
    This gives a smooth continuum from the lowest/tightest voicing at index 0
    to the highest/most open voicing at the end -- exactly what the leading
    voice rotary sweeps through.

    We extend get_all_inversions() by also including one extra octave down so
    there is room to go lower than root position, matching the Orchid dial.
    """
    candidates = get_all_inversions(notes)
    # Add one octave-down set for extra downward travel
    extra = [[n - 12 for n in c] for c in candidates]
    candidates += [c for c in extra if all(0 <= n <= 127 for n in c)]
    # Deduplicate and sort by mean pitch (low->high)
    seen, unique = set(), []
    for c in candidates:
        key = tuple(sorted(c))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return sorted(unique, key=lambda c: sum(c) / len(c))


def apply_lead_voice(notes):
    """
    Apply the current lead_voice_pos offset to a chord.
    Called at the end of play_chord after voice leading, so it always operates
    on the voice-led result. lead_voice_pos is clamped to the available range.
    """
    pos = state["lead_voice_pos"]
    if pos == 0:
        return notes  # no offset, fast path
    positions = get_voice_positions(notes)
    if not positions:
        return notes
    idx = max(0, min(len(positions) - 1, pos))
    state["lead_voice_pos"] = idx  # clamp in place
    return sorted(positions[idx])


def decode_relative_cc(value):
    """
    Decode a relative (infinite encoder) CC value to a signed integer step.

    Arturia BSP in relative mode uses offset-binary encoding by default:
      65 = +1,  66 = +2,  ...  127 = +63
      63 = -1,  62 = -2,  ...    1 = -63
      64 = 0 (no movement)

    If your controller uses two's-complement instead, set
    RELATIVE_CC_MODE = "twos" in orc1_config.py and values decode as:
      1..63  = +1..+63
      127..65 = -1..-63
    """
    mode = getattr(cfg, 'RELATIVE_CC_MODE', 'offset')
    if mode == 'twos':
        if value == 0:
            return 0
        return value if value < 64 else value - 128
    else:  # offset binary (BSP default)
        if value == 64:
            return 0
        return value - 64


def voice_lead(prev, new, strength=1.0):
    if not prev or strength < 0.05:
        return new
    candidates = get_all_inversions(new)
    if not candidates:
        return new
    pc = sum(prev) / len(prev)
    def score(c):
        cd = abs(sum(c)/len(c) - pc)
        n  = min(len(prev), len(c))
        mv = sum(abs(sorted(c)[i]-sorted(prev)[i]) for i in range(n))
        return cd * strength + mv * (1.0 - strength*0.3)
    return sorted(min(candidates, key=score))


def _retrigger_chord():
    """Replay the current chord if one is held (trigger or arp).
    Called after state changes that affect chord output (scale, root, etc.)."""
    chord_held = (state["trigger_note"] is not None) or (
        arp and arp.enabled and arp.current_chord)
    if chord_held:
        mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
        play_chord(mirror_ch=mirror_ch)


def set_scale(name, retrigger=True):
    """Set the active scale/mode by name.  Single entry point for every surface
    (Fire overlay, web, CC, PC, cmd socket) so they can't drift in behaviour.
    Unknown names are ignored rather than raising.  Returns the active scale."""
    if name in SCALES:
        state["scale"] = name
        if retrigger:
            _retrigger_chord()
        print_status(f"Scale -> {name}")
    return state["scale"]


def set_root(root_semi, retrigger=True):
    """Set the active root pitch class (0-11).  Same single-entry-point idea as
    set_scale().  Returns the active root."""
    state["root"] = int(root_semi) % 12
    if retrigger:
        _retrigger_chord()
    print_status(f"Root -> {ROOT_NAMES[state['root']]}")
    return state["root"]


def cycle_scale(step):
    """Step through the PRESENTATION order (SCALE_ORDER), not the append-only
    SCALE_NAMES order, so neighbouring scales are musically related."""
    order = SCALE_ORDER or SCALE_NAMES
    cur   = state["scale"]
    idx   = order.index(cur) if cur in order else 0
    return set_scale(order[(idx + step) % len(order)])


def set_chord_base(name, reset_inversion=True, retrigger=True):
    """Explicitly pick the chord quality -- THE single way to set chord_base.

    Setting a base means "I want THIS shape", so it takes the fixed CHORD_BASE
    path instead of stacking scale degrees.  Writing state["chord_base"] on its
    own is not enough -- without a flag the value is ignored and the pad appears
    dead.  Every surface (Fire pads, Fire chord-type menu, external CH_CTRL
    notes) must come through here so they can't drift apart.
    Returns the active base.

    O4: base_override now means specifically "a MOMENTARY override is in force
    inside DIAT", so it is only set when the quality mode is DIAT.  In latched
    FIXED mode the pad press is already persistent and setting the flag would be
    redundant -- worse, it would make every front-end that renders the flag as
    "*" (the web chord badge, the Fire OLED) claim a temporary override during
    what is actually a permanent setting.
    """
    if name not in CHORD_BASE:
        return state["chord_base"]
    state["chord_base"] = name
    if state.get("quality_mode") != "FIXED":
        state["base_override"] = True
    if reset_inversion:
        state["inversion"] = 0
    if retrigger:
        _retrigger_chord()
    return name


def play_chord(vel=None, mirror_ch=None):
    vel = vel or state["velocity"]
    if state["vel_override"] > 0:
        vel = state["vel_override"]


    # DIAT stacks the scale's degrees under the pressed root; FIXED uses the pad
    # the performer chose.  build_chord_notes() gets the CHORD root (what was
    # pressed) -- state["root"] is the KEY tonic and is a different thing.
    base = state["chord_base"]

    exts  = state["chord_exts"]
    raw   = build_chord_notes(
        chord_root_now(), base, exts, state["octave"],
        state["inversion"], state["voicing"],
        state["spread"], state["oct_offset"])
    notes = voice_lead(state["prev_notes"], raw, state["vl_strength"])

    # Leading voice -- reshapes voicing via continuous rotary position
    notes = apply_lead_voice(notes)

    # Func transpose -- semitone shift while function key is held
    if state["func_transpose"]:
        notes = [max(0, min(127, n + state["func_transpose"])) for n in notes]

    bass  = notes[0]-12 if notes[0] >= 24 else notes[0]


    state["prev_notes"] = notes
    # History stores the CHORD root -- PREV_CHORD restores it as the chord root,
    # so storing the key tonic here would recall the wrong chord.
    state["chord_history"].append(
        (chord_root_now(), base, frozenset(exts), list(notes)))
    if len(state["chord_history"]) > 8:
        state["chord_history"].pop(0)


    cname    = chord_name_str()
    mode_str = state["preset"]
    ch_str   = f"ch{mirror_ch}" if mirror_ch else "fixed"
    # Name the chord by its own root, not the key tonic.
    rname    = ROOT_NAMES[chord_root_now() % 12]


    if arp and arp.enabled:
        arp.set_chord(notes, vel, mirror_ch=mirror_ch)
        print(f"  ♩  {rname} {cname}  "
              f"[ARP/{mode_str}/{ch_str}]  {notes}")
    else:
        # Always release the previous chord first. send_chord_on()
        # overwrites the held_notes tracking, so skipping the off while
        # hold was on orphaned the old chord's notes (stuck notes).
        # Hold semantics are enforced in handle_note_off_input(), which
        # ignores note-offs while hold is on.
        send_chord_off()
        send_chord_on(notes, bass, vel, mirror_ch=mirror_ch)
        print(f"  ♪  {rname} {cname}  "
              f"[{mode_str}/{ch_str}]  {notes}  bass={bass}")

    # Perform-VE follow: additive mirror of the sounding chord to the box (ch1),
    # so its NaturalPlay HardTune tracks what we play.  Fires for arp + non-arp.
    _performve_feed(notes)


# -- Melody snap engine --------------------------------------------------------


def scale_pitch_classes():
    """Pitch classes of the current root + scale, or None if unconstrained
    (CHROM contains every pitch class, so snapping to it is a no-op)."""
    if state["scale"] == "CHROM":
        return None
    intervals = SCALES.get(state["scale"])
    if not intervals:
        return None
    root_pc = state["root"] % 12
    return [(root_pc + i) % 12 for i in intervals]


# ORDER IS THE CYCLE ORDER, and it is chosen, not alphabetical.
# "nearest" comes FIRST because it is what the old boolean SCALE SNAP did, so
# the long-standing muscle memory of "press it once to turn snap on" still
# lands somewhere familiar. "filter" is LAST because it is the only mode that
# SILENCES notes -- reaching it by accident on the first press reads as a dead
# keyboard, not as a mode change.
SCALE_MODES = ("off", "nearest", "up", "down", "filter")

def scale_mode_now():
    """The active scale mode, tolerating the legacy binary flag.

    scale_mode is the truth, but a state file saved before O4 (or any caller
    still writing the old boolean) only has scale_snap.  An unrecognised value
    reads as "off" rather than raising -- this is consulted on every note.
    """
    m = state.get("scale_mode")
    if m in SCALE_MODES:
        return m
    return "nearest" if state.get("scale_snap") else "off"


def set_scale_mode(mode):
    """Set the scale mode and keep the legacy scale_snap mirror consistent."""
    if mode not in SCALE_MODES:
        return False
    state["scale_mode"] = mode
    state["scale_snap"] = (mode != "off")
    return True


def scale_snap_note(note):
    """Global scale lock: constrain `note` to the current root+scale.

    Unlike snap_note() -- which serves the dedicated melody channel and follows
    melody_mode -- this is the instrument-wide "can't play a wrong note" lock.

    ⚠ RETURNS None WHEN THE NOTE SHOULD NOT SOUND ("filter" mode). Every caller
    must handle that; before O4 this could only ever return an int. Returns the
    note unchanged when the mode is "off" or the scale is CHROM.

    Modes (MiniKeys' vocabulary):
      off      pass everything through
      filter   off-scale notes are silenced rather than moved -- the only mode
               that can return None
      up       round an off-scale note UP to the next scale tone
      down     round it DOWN
      nearest  round to whichever is closer (the pre-O4 behaviour, which is
               also what the legacy scale_snap=True maps to)
    """
    mode = scale_mode_now()
    if mode == "off":
        return note
    pcs = scale_pitch_classes()
    if pcs is None:            # CHROM -- every pitch class is in the scale
        return note
    if note % 12 in pcs:
        return note
    if mode == "filter":
        return None
    if mode == "nearest":
        return _snap_to_pcs(note, pcs)
    step = 1 if mode == "up" else -1
    # Walk at most an octave; pcs is non-empty here, so this always terminates
    # on a real scale tone rather than running away.
    for d in range(1, 13):
        cand = note + d * step
        if cand % 12 in pcs:
            return cand
    return note


def _snap_to_pcs(note, target_set):
    """Pull `note` to the nearest pitch class in `target_set`, staying as close
    to the original pitch as possible.  Shared by the melody-snap engine and the
    global scale lock -- they differ only in how target_set is built."""
    if not target_set:
        return note

    note_pc  = note % 12
    octave_n = note // 12


    if note_pc in target_set:
        return note  # already in target, no snap needed


    # Find nearest target pitch class
    best_pc   = note_pc
    best_dist = 12
    for pc in target_set:
        dist = min(abs(note_pc - pc), 12 - abs(note_pc - pc))
        if dist < best_dist:
            best_dist = dist
            best_pc   = pc


    # Reconstruct note in same octave (adjust if needed to stay close)
    snapped = octave_n * 12 + best_pc
    # If snap moved us more than a tritone, nudge octave to stay closer
    if abs(snapped - note) > 6:
        if snapped < note:
            snapped += 12
        else:
            snapped -= 12

    # Fold (don't clamp) back into MIDI range.  A plain max(0, min(127, ...))
    # here would change the PITCH CLASS at the extremes -- e.g. note 0 in D
    # major picks B, the octave nudge takes it to -1, and clamping to 0 returns
    # a C, which is not in the scale at all.  Shifting by whole octaves keeps
    # the snapped pitch class intact, which is the entire point of snapping.
    while snapped < 0:
        snapped += 12
    while snapped > 127:
        snapped -= 12

    return snapped


def snap_note(note):
    """
    Snap a note to the current scale or chord tones depending on melody_mode.
    Returns the (possibly modified) note number.
    CHROM scale or mode OFF -> return unchanged.
    """
    mode = state["melody_mode"]
    if mode == "OFF" or state["scale"] == "CHROM":
        return note

    if mode == "CHORD" and state["held_notes"]:
        # Snap to nearest chord tone (pitch class only, then find best octave)
        target_set = list({n % 12 for (n, pk, ch) in state["held_notes"]})
        if not target_set:
            return note
    else:
        # SCALE mode -- snap to nearest scale note
        scale_intervals = SCALES.get(state["scale"], list(range(12)))
        target_set = [(state["root"] + i) % 12 for i in scale_intervals]

    return _snap_to_pcs(note, target_set)


def _melody_dest():
    """Return (port_key, channel) for melody snap output, honouring runtime override."""
    mo = state["melody_out"]
    return (mo["port"] or "pisound", mo["channel"] or cfg.MELODY_CH)


def handle_melody_note_on(note, vel):
    snapped = snap_note(note)
    state["melody_map"][note] = snapped   # track for note-off
    pk, ch = _melody_dest()
    note_on_out(snapped, vel, pk, ch)
    if cfg.DEBUG:
        print(f"  [MELODY {state['melody_mode']}] {note}->{snapped}"
              f"  ({ROOT_NAMES[note%12]}->{ROOT_NAMES[snapped%12]})")


def handle_melody_note_off(note):
    snapped = state["melody_map"].pop(note, note)
    pk, ch = _melody_dest()
    note_off_out(snapped, pk, ch)


# -- Status --------------------------------------------------------------------

_STATE_JSON_PATH = os.path.expanduser("~/orc1_state.json")
_CMD_PATH        = os.path.expanduser("~/orc1_cmd.json")

# Keys in ~/orc1_state.json that belong to a DIFFERENT writer than
# write_state_json().  Everything matching the "fire_" prefix is owned by
# FireHandler._save_fire_state(); these are the stragglers that do not carry
# that prefix.  write_state_json() carries them forward instead of clobbering
# them — see the comment block there for the history.
_FOREIGN_STATE_KEYS = frozenset({
    "fx_slot_assignments",   # FireHandler.fx_save_assignments()
})


def _handle_seq_cmd(action):
    """Dispatch SEQ_* commands from web UI or external controllers.
    Format: SEQ_COMMAND[:arg1[:arg2[...]]]

    2026-07-01 ownership refactor: reaches the module-level `seq` directly
    instead of proxying through `fire.seq` -- these commands now work with
    no Fire connected at all (Bug A). Track mutations route through seq's
    lock-guarded methods instead of poking track dicts directly (Bug B --
    this handler runs on the Flask/cmd-socket thread, which was racing the
    clock thread's _advance() exactly like Fire's pad presses were)."""
    if not seq:
        return
    try:
        parts = action.split(":")
        cmd   = parts[0]

        if   cmd == "SEQ_PLAY":   seq.play()
        elif cmd == "SEQ_STOP":   seq.stop(reset=True)
        elif cmd == "SEQ_SAVE":   seq.save()

        elif cmd == "SEQ_TOGGLE_RECORD":
            seq.record_mode = not seq.record_mode

        elif cmd == "SEQ_REC_TRACK":
            seq.record_track = max(0, min(seq.n_tracks - 1, int(parts[1])))

        elif cmd == "SEQ_TOGGLE_STEP":
            seq.toggle_step(int(parts[1]), int(parts[2]))

        elif cmd == "SEQ_TOGGLE_ACCENT" or cmd == "SEQ_CYCLE_VEL":
            # SHIFT-tap: cycle the step's velocity tier (None->ghost->normal->
            # accent->None). Old cmd name kept as an alias for the web page.
            seq.cycle_step_vel(int(parts[1]), int(parts[2]))

        elif cmd == "SEQ_SET_VEL":
            # SEQ_SET_VEL:tidx:step:vel  (vel < 0 or empty clears the override)
            tidx, spos = int(parts[1]), int(parts[2])
            v = int(parts[3]) if len(parts) > 3 and parts[3] != "" else -1
            seq.set_step_vel(tidx, spos, None if v < 0 else v)

        elif cmd == "SEQ_SET_NOTE":
            tidx, spos, note = int(parts[1]), int(parts[2]), int(parts[3])
            seq.set_step_note(tidx, spos, max(0, min(127, note)))

        elif cmd == "SEQ_CLEAR_NOTE":
            tidx, spos = int(parts[1]), int(parts[2])
            seq.set_step_note(tidx, spos, None)

        elif cmd == "SEQ_MUTE":
            tidx = int(parts[1])
            seq.set_track_mute(tidx)   # toggle

        elif cmd == "SEQ_SET_PROP":
            # SEQ_SET_PROP:tidx:prop:value
            tidx, prop, val = int(parts[1]), parts[2], parts[3]
            prop = "length" if prop == "len" else prop
            seq.set_track_prop(tidx, prop, val)

        elif cmd == "SEQ_ADD":
            seq.add_track()

        elif cmd == "SEQ_DEL":
            tidx = int(parts[1])
            seq.remove_track(tidx)

        elif cmd == "SEQ_CLEAR_STEPS":
            tidx = int(parts[1])
            seq.clear_steps(tidx)

    except Exception as e:
        print(f"[seq cmd] {action}: {e}")
    write_state_json()


def _build_seq_state():
    """Compact serialisable snapshot of all sequencer tracks for the web UI."""
    out = []
    if not seq:
        return out
    for t in seq.tracks:
        length = t.get("length", 16)
        steps  = t.get("steps",  [False]*64)
        svels  = t.get("step_vels", [None]*64)
        sn     = t.get("step_notes", [])
        overrides = {str(i): sn[i] for i in range(min(len(sn), length))
                     if sn and sn[i] is not None}
        vel_ov = {str(i): svels[i] for i in range(min(len(svels), length))
                  if svels and svels[i] is not None}
        out.append({
            "name":       t.get("name",   "trk"),
            "ch":         int(t.get("ch",     6)),
            "note":       int(t.get("note",  36)),
            "mode":       t.get("mode",  "note"),
            "vel":        int(t.get("vel",  100)),
            "length":     int(length),
            "mute":       bool(t.get("mute", False)),
            "pos":        int(t.get("pos",    0)),
            "color":      list(t.get("color", [80, 0, 0])),
            "bright":     int(t.get("bright", 127)),
            "steps":      "".join("1" if steps[i] else "0" for i in range(length)),
            # Legacy loud-step marker for the (unused) web grid: a step reads as
            # "accented" when its velocity override is in the top tier band.
            "accents":    "".join("1" if (i < len(svels) and svels[i] is not None
                                          and svels[i] >= 110) else "0"
                                  for i in range(length)),
            "step_vels":  vel_ov,
            "step_notes": overrides,
        })
    return out


_CHORD_OUT_FILE = os.path.expanduser("~/.orc1_chord_out.json")

def _load_chord_out():
    """Restore chord output override from ~/.orc1_chord_out.json on startup."""
    try:
        with open(_CHORD_OUT_FILE) as f:
            d = json.load(f)
            state["chord_out"] = {
                "port":    d.get("port") or None,
                "channel": int(d["channel"]) if d.get("channel") else None,
            }
        print(f"  chord_out restored: {state['chord_out']}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [chord_out load] {e}")

def _save_chord_out():
    """Persist chord output override atomically."""
    try:
        tmp = _CHORD_OUT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state["chord_out"], f)
        os.replace(tmp, _CHORD_OUT_FILE)
    except Exception as e:
        print(f"  [chord_out save] {e}")


_PERFORMVE_FILE = os.path.expanduser("~/.orc1_performve.json")

def _load_performve():
    """Restore the Perform-VE follow toggle + input source from
    ~/.orc1_performve.json."""
    try:
        with open(_PERFORMVE_FILE) as f:
            d = json.load(f)
        state["performve_feed"] = bool(d.get("on"))
        src = d.get("src") or {}
        port = src.get("port")
        state["performve_src"] = {
            "port":    port if port in ("usb", "din", "both") else None,
            "channel": int(src["channel"]) if src.get("channel") else None,
        }
        print(f"  performve restored: feed={state['performve_feed']} "
              f"src={state['performve_src']}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [performve load] {e}")

def _save_performve():
    """Persist the Perform-VE follow toggle + input source atomically."""
    try:
        tmp = _PERFORMVE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"on":  bool(state["performve_feed"]),
                       "src": dict(state["performve_src"])}, f)
        os.replace(tmp, _PERFORMVE_FILE)
    except Exception as e:
        print(f"  [performve save] {e}")


_VOICE_THRU_FILE = os.path.expanduser("~/.orc1_voice_thru.json")

def _load_voice_thru():
    """Restore per-voice MIDI thru mappings from ~/.orc1_voice_thru.json."""
    try:
        with open(_VOICE_THRU_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            # Validate each entry: must have port (str) and channel (1-16).
            cleaned = {}
            for ch_str, mapping in d.items():
                if (isinstance(mapping, dict)
                        and mapping.get("port")
                        and mapping.get("channel")
                        and 1 <= int(mapping["channel"]) <= 16):
                    cleaned[str(ch_str)] = {
                        "port":    str(mapping["port"]),
                        "channel": int(mapping["channel"]),
                    }
            state["voice_thru"] = cleaned
        if state["voice_thru"]:
            print(f"  voice_thru restored: {state['voice_thru']}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [voice_thru load] {e}")

def _save_voice_thru():
    """Persist voice thru mappings atomically."""
    try:
        tmp = _VOICE_THRU_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state["voice_thru"], f, indent=2)
        os.replace(tmp, _VOICE_THRU_FILE)
    except Exception as e:
        print(f"  [voice_thru save] {e}")


_MELODY_OUT_FILE = os.path.expanduser("~/.orc1_melody_out.json")

def _load_melody_out():
    try:
        with open(_MELODY_OUT_FILE) as f:
            d = json.load(f)
            state["melody_out"] = {
                "port":    d.get("port") or None,
                "channel": int(d["channel"]) if d.get("channel") else None,
            }
        print(f"  melody_out restored: {state['melody_out']}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [melody_out load] {e}")

def _save_melody_out():
    try:
        tmp = _MELODY_OUT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state["melody_out"], f)
        os.replace(tmp, _MELODY_OUT_FILE)
    except Exception as e:
        print(f"  [melody_out save] {e}")


_VOICE_MAP_FILE = os.path.expanduser("~/.orc1_voicemap.json")

def _load_voice_map():
    """Restore per-slot voice routing overrides from ~/.orc1_voicemap.json on startup."""
    try:
        with open(_VOICE_MAP_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            state["voice_map"] = d
            active = len([v for v in d.values() if v is not None])
            print(f"  voice_map restored: {active} slot override(s)")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [voice_map load] {e}")

def _save_voice_map():
    try:
        tmp = _VOICE_MAP_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state["voice_map"], f)
        os.replace(tmp, _VOICE_MAP_FILE)
    except Exception as e:
        print(f"  [voice_map save] {e}")

_CONFIG_OVERRIDES_FILE = os.path.expanduser("~/.orc1_config_overrides.json")

def _save_preset_override(name, preset_dict):
    """Persist a USER_N preset into the config overrides file so it survives restart.
    Also updates cfg.PRESETS[name] in-memory so it takes effect immediately."""
    try:
        try:
            with open(_CONFIG_OVERRIDES_FILE) as f:
                ovr = json.load(f)
            if not isinstance(ovr, dict):
                ovr = {}
        except FileNotFoundError:
            ovr = {}
        ovr[f"PRESETS.{name}"] = preset_dict
        tmp = _CONFIG_OVERRIDES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ovr, f, indent=2)
        os.replace(tmp, _CONFIG_OVERRIDES_FILE)
        # Also patch the live in-memory config so no restart is needed
        cfg.PRESETS[name] = preset_dict
        print(f"  preset override saved: {name}")
    except Exception as e:
        print(f"  [preset save] {e}")


def write_state_json():
    """
    Write current engine state to ~/orc1_state.json atomically.
    Called at the end of every print_status() so the web UI always has
    a fresh snapshot. Uses a temp file + rename so the reader never
    catches a half-written file. Fails silently -- never crashes the engine.

    The JSON contains everything needed to render the web UI:
      chord_base, chord_exts, chord_name, root_name, scale, preset,
      octave, voicing, vl_enabled, key_lock, hold, root_mode,
      arp_passthrough, melody_mode, arp (sub-dict with arp status),
      timestamp (epoch seconds, for the UI to detect staleness)
    """
    try:
        arp_status = {}
        if arp:
            arp_status = {
                "enabled":    arp.enabled,
                "pattern":    arp.pattern_name,
                "rate":       arp.subdivision,
                "clock_mode": arp.clock_mode,
                "bpm":        round(arp._estimated_bpm() or arp.internal_bpm, 1),
                "swing":      round(arp.swing_amount, 2),
                "humanize":   round(arp.humanize, 2),
                "gate":       round(arp.gate_trim, 2),
            }

        snapshot = {
            "timestamp":      time.time(),
            "chord_base":     state["chord_base"],
            # The shape that will actually sound.  In DIAT this is derived from
            # the notes the scale produced, so it can be AUG / 6no5 / a 7th
            # name -- not only one of the four CHORD_BASE labels.
            "chord_base_eff": effective_base(),
            "base_override":  state.get("base_override", False),
            "chord_exts":     sorted(state["chord_exts"], key=_ext_sort_key),
            "chord_name":     chord_name_str(),
            # "root" is the KEY tonic; "chord_root" is what was pressed.  Both
            # are published because the web needs to show the key AND name the
            # chord that is sounding (they are only equal on degree I).
            "root":           state["root"],
            "root_name":      ROOT_NAMES[state["root"]],
            "chord_root":     chord_root_now(),
            "chord_root_name":ROOT_NAMES[chord_root_now() % 12],
            "chord_degree":   current_degree(),
            "scale":          state["scale"],
            # Served rather than hardcoded in the web JS so the picker can
            # never drift from the engine's actual scale list.
            "scales":         list(SCALE_ORDER),
            "scale_snap":     state.get("scale_snap", False),
            # O4: the real five-way mode, plus the list so a picker can never
            # drift from the engine's own vocabulary.
            "scale_mode":     scale_mode_now(),
            "scale_modes":    list(SCALE_MODES),
            # O4: where the chord quality is coming from RIGHT NOW. "DIAT" with
            # base_override true means a momentary pad override is in force --
            # the front-ends show that as DIAT*, not as FIXED.
            "quality_mode":   state.get("quality_mode", "DIAT"),
            "preset":         state["preset"],
            "octave":         state["octave"] + state["oct_offset"],
            "voicing":        state["voicing"],
            "vl_enabled":     state["vl_enabled"],
            "vl_strength":    round(state["vl_strength"], 2),
            "key_lock":       state["key_lock"],
            "key_lock_ch":    state.get("key_lock_ch", 0),
            "hold":           state["hold"],
            "root_mode":      state["root_mode"],
            "arp_passthrough":state["arp_passthrough"],
            "arp_hold":       state.get("arp_hold", False),
            "mirror_input_ch":state["mirror_input_ch"],
            "synth_mirror_poly": state["synth_mirror_poly"],
            "synth_mirror_chip": state["synth_mirror_chip"],
            "din_routing":    state["din_routing"],
            "chord_out":      dict(state["chord_out"]),
            "performve_feed": state["performve_feed"],
            "performve_src":  dict(state["performve_src"]),
            "melody_out":     dict(state["melody_out"]),
            # Per-voice MIDI thru mappings (src_ch string -> {port, channel}).
            "voice_thru":     dict(state["voice_thru"]),
            # Module Matrix: live per-module channel bindings (module -> channel;
            # a module absent from this map is currently out of the fold).
            "bindings":       dict(_bindings),
            # P1/P2 JOIN state (ephemeral). mode != 'none' => chip_p2 is joined to
            # chip_p1; the page reads this to show chip_p2 as "joined -> P1".
            "join":           dict(_mm_join),
            # Channels a voice may never bind to (chord/ctrl/mpc) -- the Module
            # Matrix bus renders these as reserved. Sourced from cfg so the page
            # stays in step with the engine's own bind validation.
            "reserved":       sorted(_MM_RESERVED),
            # Every module the engine knows (cfg MODULE_KEYS, wire-id order), so
            # pages can list UNBOUND modules too (/matrix, /routes voice map).
            "module_keys":    list(_MM_KEYS),
            # Per-slot voice routing overrides (sparse).
            "voice_map":      dict(state["voice_map"]),
            # Current preset's full slot definitions — used by web UI to show
            # what the preset routes each slot to alongside live overrides.
            "preset_map":     dict(cfg.PRESETS.get(state["preset"], {})),
            # External presets — slots use physical MIDI ports, not the internal synth.
            # Used by the routes page to populate quick-select preset buttons.
            "presets_external": [k for k, v in cfg.PRESETS.items()
                                  if any(isinstance(d, dict) and d.get("port") != "synth"
                                         for d in v.values())],
            "midi_devices":   _get_midi_device_names(),
            "active_routes":  router.active_devices() if router else [],
            "seq_running":    seq.running                       if seq else False,
            "seq_record":     getattr(seq, "record_mode", False) if seq else False,
            "seq_rec_track":  getattr(seq, "record_track", 0)    if seq else 0,
            "seq_tracks":     _build_seq_state()                 if seq else [],
            "func_held":      state["func_held"],
            "func_transpose": state["func_transpose"],
            "lead_voice_pos": state["lead_voice_pos"],
            "melody_mode":    state["melody_mode"],
            "arp":            arp_status,
            "beatbuddy":      ({
                "status":  beatbuddy.status_name(),
                "playing": beatbuddy.is_playing(),
                "part":    beatbuddy.PartIndex + 1,
                "song":    (os.path.basename(beatbuddy.CurrSong.name)
                            if beatbuddy.CurrSong else None),
                "bpm":     (beatbuddy.CurrSong.bpm if beatbuddy.CurrSong else None),
                "nPart":   (beatbuddy.CurrSong.nPart if beatbuddy.CurrSong else 0),
                "clock_bpm":  (round(arp._estimated_bpm() or arp.internal_bpm, 1)
                               if arp else None),
                "clock_mode": (arp.clock_mode if arp else None),
                "tempo_mode": ("clock" if _bb_follow_clock else "song"),
                "output":     dict(_BB_OUT),
            } if beatbuddy else {}),
            # Was getattr(fire, "_clock_out", False) -- FireHandler never had
            # that attribute (clock-out state lives on Sequencer), so this
            # always silently reported False. Found while grounding the
            # sequencer ownership refactor; fixed here since seq is now
            # reachable with no Fire dependency anyway.
            "clock_out":      seq.clock_out if seq else False,
            # FX chain status -- empty dict if FXManager isn't available.
            # Each key is a chain name; value has running, pids, description.
            "fx":             fx.status() if fx else {},
            # Current FX I/O routing mode + port names.
            "fx_io":          _read_fx_io_state(),
            # Hardware file-browser selection per deck (top encoder scrolls,
            # WHEEL loads) -- surfaced so the web pages can show what the
            # encoder is pointing at (the Mixtrack has no display).
            "sampler_browse": {
                d: (os.path.basename(sampler_browsed(d) or "") or None)
                for d in ("A", "B")
            },
            # Last CC values forwarded to synth bus via SYNTH_CC command.
            # Keyed as strings so JSON round-trips cleanly.
            "synth_cc":       dict(state["synth_cc"]),
            # Dynamically-opened MIDI output ports (MIDI_SCAN cmd).
            # Key = port key used in send_to(); value = full ALSA port name.
            "dynamic_ports": dict(_dynamic_ports),
        }

        # Universal pad assignments, published live from the Fire handler so
        # the web GET /api/universal_pads always sees the current grid rather
        # than whatever was last flushed to disk.
        #
        # ONLY when a Fire is actually connected.  This used to be an
        # unconditional `... if fire else {}` inside the snapshot literal, so a
        # boot with the Fire unplugged wrote an EMPTY grid over the saved one
        # and the user's UCS assignments were gone for good.  With no Fire
        # there is no live truth to publish, so the key is omitted and the
        # carry-forward below preserves what is on disk.
        #
        # PUBLISHES BANK 0, NOT THE CURRENT BANK (2026-07-31).  `fire.
        # _universal_pads` is now a property onto whichever bank the player is
        # paging through, so reading it here would make this mirror mean "bank
        # 0" when written by the Fire's own _save_fire_state and "bank 2" when
        # written by print_status() a second later -- the same key holding two
        # different things depending on which writer ran last.  The full state
        # lives in `fire_universal_banks`, which the carry-forward below
        # preserves; this key stays pinned to bank 0, which is what
        # /api/universal_pads and every pre-bank client expect.
        if fire:
            _b0 = getattr(fire, "_universal_banks", {}).get(
                0, fire._universal_pads)
            snapshot["fire_universal_pads"] = {
                f"{k[0]},{k[1]}": {
                    ek: ev for ek, ev in v.items()
                    if not ek.startswith("_")
                }
                for k, v in _b0.items()
            }
            snapshot["fire_universal_banks"] = {
                str(bi): {
                    f"{k[0]},{k[1]}": {
                        ek: ev for ek, ev in v.items()
                        if not ek.startswith("_")
                    }
                    for k, v in pads.items()
                }
                for bi, pads in getattr(fire, "_universal_banks",
                                        {0: fire._universal_pads}).items()
            }
            # Per-page knob overrides (Fire reorg Phase 2), published live so the
            # /control page can show/edit the current page's knobs.
            snapshot["fire_universal_knobs"] = {
                str(bi): {lyr: {str(ix): list(act)
                                for ix, act in knobs.items()}
                          for lyr, knobs in layers.items()}
                for bi, layers in getattr(fire, "_universal_knobs", {}).items()
            }
            snapshot["fire_universal_faders"] = {
                str(bi): {str(r): dict(f) for r, f in rows.items()}
                for bi, rows in getattr(fire, "_universal_faders", {}).items()
            }

        # -- Carry forward the keys this function does not own -----------------
        # ~/orc1_state.json has more than one writer.  The Fire handler's
        # _save_fire_state() merges SEVEN fire_* keys into it every 60 s, and
        # fx_save_assignments() writes fx_slot_assignments.  Both of those do a
        # proper read-modify-write; this function did not — it dumped `snapshot`
        # straight over the file, so every key it did not itself define was
        # destroyed on each print_status() and only reappeared at the Fire's
        # next autosave.  Power-cycling inside that window lost the MPC fader
        # bank, chip/poly base notes, saved CC values and FX knob positions
        # outright.  Same clobbering-write pattern that ate the jackd "period"
        # key out of ~/.orc1_audio.json — see CLAUDE.md.
        #
        # Foreign keys are matched by PREFIX, not by an explicit list: adding a
        # new fire_* key to _save_fire_state() must not require a matching edit
        # here to survive.  That coupling is exactly how fire_mpc_faders came to
        # be clobbered while fire_universal_pads (patched in by hand) survived.
        # Anything already in `snapshot` wins — the engine's live value is
        # authoritative for the keys the engine owns.
        try:
            with open(_STATE_JSON_PATH) as f:
                _prev = json.load(f)
            if isinstance(_prev, dict):
                for _k, _v in _prev.items():
                    if _k not in snapshot and (
                        _k.startswith("fire_") or _k in _FOREIGN_STATE_KEYS
                    ):
                        snapshot[_k] = _v
        except Exception:
            pass   # no file yet, or unreadable -> nothing to carry forward

        # Atomic write: temp file in same dir, then rename
        tmp = f"{_STATE_JSON_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, _STATE_JSON_PATH)

    except Exception:
        pass   # never let a JSON write crash the engine


def _playstate_snapshot():
    """What is playing right now, for the HUD (orc1_playstate publishes it).

    READ-ONLY: attribute reads of state the engine already keeps, no locks, no
    I/O, nothing the MIDI or clock threads wait on. A torn read costs one
    stale frame at 30 Hz, which nobody sees."""
    now = time.monotonic()
    running = bool(seq and seq.running)
    bpm = 120.0
    if arp:
        bpm = float(arp._estimated_bpm() or arp.internal_bpm or 120.0)
    gs = int(seq._global_step) if running else -1
    sw = getattr(seq, "_step_wall", None) if seq else None
    tracks = []
    if seq:
        for t in list(seq.tracks):
            ln = int(t.get("length", 16))
            st = t.get("steps", [])
            tracks.append({
                "name": t.get("name", "trk"), "len": ln,
                "pos": int(t.get("pos", 0)), "mute": bool(t.get("mute", False)),
                "steps": "".join("1" if (i < len(st) and st[i]) else "0"
                                 for i in range(ln))})
    snap = {
        "transport": {
            "running": running, "bpm": round(bpm, 1),
            "clock": arp.clock_mode if arp else None,
            "step": gs, "bar_step": (gs % 16) if gs >= 0 else -1,
            "age_ms": int((now - sw) * 1000) if (running and sw) else 0,
            "step_ms": round(15000.0 / max(20.0, bpm), 1),
            "rec": bool(getattr(seq, "record_mode", False)) if seq else False,
        },
        "chord": {
            "name": chord_name_str(), "key": ROOT_NAMES[state["root"]],
            "scale": state["scale"], "degree": current_degree(),
            "preset": state["preset"], "octave": state["octave"] + state["oct_offset"],
        },
        "arp": ({"on": bool(arp.enabled), "pattern": arp.pattern_name,
                 "rate": arp.subdivision} if arp else {}),
        "held": _orc1_playstate.held_names(state.get("held_notes", [])),
        "tracks": tracks,
        "bindings": dict(_bindings),
        "mods": _ps_activity.by_module(_bindings),
        "bb": ({"playing": bool(beatbuddy.is_playing()),
                "part": beatbuddy.PartIndex + 1} if beatbuddy else {}),
    }
    return snap


# -- Autosampler (lazy — jack/numpy only load when a sweep starts) -------------
_autosampler = None

def _get_autosampler():
    global _autosampler
    if _autosampler is None:
        import orc1_autosampler
        _autosampler = orc1_autosampler.AutoSampler(
            getattr(cfg, "AUTOSAMPLER", {}), send_to)
    return _autosampler


# -- Universal Control Surface pad banks (2026-07-31) -------------------------
# The Fire's UCS grew banks (GRID <> pages between them). These helpers let the
# web page address one, defaulting to bank 0 -- which is the bank /control has
# always been editing, so an old client that never sends `bank` keeps hitting
# exactly the pads it did before.

_UCS_STATE_FILE = os.path.expanduser("~/orc1_state.json")


def _ucs_bank(cmd, fire):
    """Resolve the requested bank index, clamped to what actually exists."""
    try:
        bank = int(cmd.get("bank", 0))
    except (TypeError, ValueError):
        return 0
    if fire is not None:
        limit = int(getattr(fire, "_universal_nbanks", 4) or 4)
    else:
        # No Fire connected: fall back to the CONFIGURED count, not to the 9
        # the handler merely caps at. Defaulting to 9 let a web client write
        # pads into banks 4-8 while the Fire was unplugged; those banks are
        # skipped on load (out of range) and then wiped by the next save,
        # because both writers replace fire_universal_banks wholesale rather
        # than merging. Silent, permanent data loss with no error anywhere.
        try:
            limit = int(cfg.FIRE.get("UNIVERSAL_BANKS", 4))
        except (AttributeError, TypeError, ValueError, NameError, KeyError):
            limit = 4
    return max(0, min(max(1, min(9, limit)) - 1, bank))


def _ucs_bank_pads(fire, bank):
    """The live {(row,col): assign} dict for `bank` on a connected Fire."""
    banks = getattr(fire, "_universal_banks", None)
    if isinstance(banks, dict) and bank in banks:
        return banks[bank]
    # Older handler without banks: there is only the one grid.
    return fire._universal_pads


def _ucs_state_update(bank, mutate):
    """Apply `mutate(pads_dict)` to a bank in ~/orc1_state.json, no Fire needed.

    Writes BOTH keys the file now carries -- `fire_universal_banks` (the real
    state) and `fire_universal_pads` (bank 0, the shape orc1_web.py's GET and
    every older client read) -- so the two can never disagree.

    Read-modify-write, and written atomically via a temp file + os.replace:
    this file has seven other `fire_*` keys owned by the Fire handler, and a
    reader landing mid-write would otherwise see a truncated file. Both hazards
    have bitten this project before (the P6 state clobber; the FX/fader torn
    read).
    """
    try:
        with open(_UCS_STATE_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    banks = data.get("fire_universal_banks")
    if not isinstance(banks, dict) or not banks:
        # Migrate the pre-bank single grid into bank 0 rather than dropping it.
        banks = {"0": data.get("fire_universal_pads", {}) or {}}
    pads = banks.get(str(bank))
    if not isinstance(pads, dict):
        pads = {}
    mutate(pads)
    banks[str(bank)] = pads
    data["fire_universal_banks"] = banks
    data["fire_universal_pads"]  = banks.get("0", {})
    tmp = _UCS_STATE_FILE + ".ucs.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _UCS_STATE_FILE)


def _ucs_knob_state_update(bank, layer, mutate):
    """Apply `mutate(knobs_dict)` to one (bank, layer) of fire_universal_knobs in
    ~/orc1_state.json with no Fire connected (Fire reorg Phase 2). knobs_dict is
    {str(idx): [action...]}. Read-modify-write, atomic (see _ucs_state_update)."""
    try:
        with open(_UCS_STATE_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    allk = data.get("fire_universal_knobs")
    if not isinstance(allk, dict):
        allk = {}
    layers = allk.get(str(bank))
    if not isinstance(layers, dict):
        layers = {}
    knobs = layers.get(str(layer))
    if not isinstance(knobs, dict):
        knobs = {}
    mutate(knobs)
    layers[str(layer)] = knobs
    allk[str(bank)] = layers
    data["fire_universal_knobs"] = allk
    tmp = _UCS_STATE_FILE + ".ucsk.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _UCS_STATE_FILE)


def _ucs_fader_state_update(bank, mutate):
    """Apply `mutate(rows_dict)` to one bank of fire_universal_faders in
    ~/orc1_state.json with no Fire connected (Fire reorg Phase 3). rows_dict is
    {str(row): {cc,channel,port,value}}. Read-modify-write, atomic."""
    try:
        with open(_UCS_STATE_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    allf = data.get("fire_universal_faders")
    if not isinstance(allf, dict):
        allf = {}
    rows = allf.get(str(bank))
    if not isinstance(rows, dict):
        rows = {}
    mutate(rows)
    allf[str(bank)] = rows
    data["fire_universal_faders"] = allf
    tmp = _UCS_STATE_FILE + ".ucsf.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _UCS_STATE_FILE)


def _bb_now_ms():
    return int(time.monotonic() * 1000)


def _bb_out_targets(note, vel, out, bbkit_ch, drum_ch, table):
    """PURE: where one BeatBuddy hit goes. Returns a list of
      ("synth", channel_1based, note, vel)  -> the ORC module bus, or
      ("port",  port_key, channel_1based, note, vel) -> an external MIDI out.
    "909" translates the BB note through `table` (BB note -> ORC drum note) and
    drops notes the table does not know; "port" passes the note untranslated."""
    dest = (out or {}).get("dest", "bbkit")
    note, vel = int(note) & 0x7f, max(1, min(127, int(vel)))
    if dest == "909":
        n = (table or {}).get(note)
        return [("synth", int(drum_ch), int(n), vel)] if (n is not None and drum_ch) else []
    if dest == "port":
        port = out.get("port")
        ch = int(out.get("channel") or 10)
        return [("port", port, ch, note, vel)] if (port and 1 <= ch <= 16) else []
    return [("synth", int(bbkit_ch), note, vel)] if bbkit_ch else []


def _bb_flush_offs(now=None, everything=False):
    """Send the note-offs owed to external gear (dest "port") whose time is up
    (or all of them). Called every clock pulse from the BeatBuddy tick."""
    global _bb_pending_offs
    if not _bb_pending_offs:
        return
    now = time.monotonic() if now is None else now
    due = [p for p in _bb_pending_offs if everything or p[0] <= now]
    if not due:
        return
    _bb_pending_offs = [p for p in _bb_pending_offs if p not in due]
    for _t, port, ch0, note in due:
        try:
            send_to(port, mido.Message("note_off", channel=ch0, note=note, velocity=0))
        except Exception:
            pass


def _bb_out_file():
    return os.path.expanduser("~/.orc1_bb_out.json")


def _bb_out_set(dest, port=None, channel=None, persist=True):
    """Choose where BeatBuddy plays. Invalid input leaves the choice unchanged.
    Switching flushes any note-offs still owed to the previous external port."""
    if dest not in _BB_OUT_DESTS:
        return dict(_BB_OUT)
    try:
        ch = int(channel) if channel not in (None, "") else _BB_OUT.get("channel", 10)
    except (TypeError, ValueError):
        ch = _BB_OUT.get("channel", 10)
    if dest == "port" and (not port or not 1 <= ch <= 16):
        return dict(_BB_OUT)
    _bb_flush_offs(everything=True)
    _BB_OUT.update({"dest": dest, "port": port if dest == "port" else _BB_OUT.get("port"),
                    "channel": max(1, min(16, ch))})
    if persist:
        try:
            tmp = _bb_out_file() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_BB_OUT, f)
            os.replace(tmp, _bb_out_file())
        except Exception as e:
            print(f"  ! BeatBuddy output persist failed: {e}", flush=True)
    return dict(_BB_OUT)


def _bb_out_load():
    """Restore the saved BeatBuddy output (missing/corrupt -> BBKit)."""
    try:
        with open(_bb_out_file()) as f:
            d = json.load(f)
        _bb_out_set(d.get("dest", "bbkit"), d.get("port"), d.get("channel"),
                    persist=False)
    except Exception:
        pass


def _bb_song_file():
    return os.path.expanduser("~/.orc1_bb_song.json")


def _bb_song_restore():
    """Reload the last loaded song after a restart. Without it every orc1 restart
    left the mind in NO_SONG_LOADED, where the footswitch/pedal silently does
    nothing (found 2026-09-23 when the FCB's BeatBuddy switch went dead)."""
    try:
        with open(_bb_song_file()) as f:
            path = json.load(f).get("song")
    except Exception:
        return
    if path and os.path.exists(path):
        _bb_song_select(path, persist=False)


def _bb_song_select(name, persist=True):
    """Load a song into the BeatBuddy mind. `name` may be a bare stem, a
    'name.BBS' / 'name.json', or an absolute path; bare names resolve under the
    songs dir. A .json file is an ORC song-IR (Slate / custom, see slate2song.py);
    anything else is a factory .BBS. Sets the ORC internal tempo to the song's
    native bpm (faithful BeatBuddy behaviour -- harmless in external-clock mode,
    where the mind slaves instead)."""
    if not beatbuddy:
        return
    songs_dir = getattr(beatbuddy, "_songs_dir", os.path.expanduser("~/.orc1_bbsongs"))
    cand = name if os.path.isabs(name) else os.path.join(songs_dir, name)
    if not os.path.exists(cand) and not cand.lower().endswith((".bbs", ".json")):
        if os.path.exists(cand + ".BBS"):
            cand = cand + ".BBS"
    try:
        if cand.lower().endswith(".json"):
            beatbuddy.load_ir(cand)
        else:
            beatbuddy.load_bbs(cand)
        if (arp and beatbuddy.CurrSong and beatbuddy.CurrSong.bpm
                and not _bb_follow_clock):
            arp.set_internal_bpm(float(beatbuddy.CurrSong.bpm))
        print(f"  BeatBuddy song loaded: {os.path.basename(cand)} "
              f"(bpm {beatbuddy.CurrSong.bpm}, nPart {beatbuddy.CurrSong.nPart})", flush=True)
        if persist:
            try:
                tmp = _bb_song_file() + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"song": os.path.abspath(cand)}, f)
                os.replace(tmp, _bb_song_file())
            except Exception as e:
                print(f"  ! BeatBuddy song persist failed: {e}", flush=True)
    except Exception as e:
        print(f"  ! BeatBuddy song load {name!r}: {e}", flush=True)


def _bb_kit_select(program):
    """Select the BBKit drumset via a Program Change on the bbkit channel."""
    ch = _module_channel("bbkit")
    if ch is None:
        return
    try:
        send_synth_midi(mido.Message("program_change", channel=int(ch) - 1,
                                     program=int(program) & 0x7f))
    except Exception:
        pass


_bb_kit_idx = 0


def _bb_song_cycle(step):
    """Load the next/prev .BBS in the songs dir (wraps). step = +1 / -1."""
    if not beatbuddy:
        return
    songs_dir = getattr(beatbuddy, "_songs_dir", os.path.expanduser("~/.orc1_bbsongs"))
    try:
        songs = sorted(f for f in os.listdir(songs_dir)
                       if f.lower().endswith((".bbs", ".json")))
    except Exception:
        songs = []
    if not songs:
        return
    cur = os.path.basename(beatbuddy.CurrSong.name) if beatbuddy.CurrSong else None
    i = songs.index(cur) if cur in songs else -step  # so first next -> songs[0]
    _bb_song_select(songs[(i + step) % len(songs)])


def _bb_pedal_action(g):
    """Discrete pedal gesture -> the mind's convenience verbs. Called from
    orc1_pedal (its own thread); the mind's methods are the same the cmd socket
    uses, so this is just another controller-agnostic front-end."""
    if not beatbuddy:
        return
    global _bb_kit_idx
    if g == "tap":
        beatbuddy.tap(_bb_now_ms())
    elif g == "hold":
        beatbuddy.hold(_bb_now_ms())
    elif g == "release":
        beatbuddy.release(_bb_now_ms())
    elif g == "double":
        beatbuddy.double_tap(_bb_now_ms())
    elif g == "start":
        beatbuddy.tap(_bb_now_ms())
    elif g == "stop":
        beatbuddy.stop()
    elif g == "pause":
        beatbuddy.pause()
    elif g == "song_next":
        _bb_song_cycle(+1)
    elif g == "song_prev":
        _bb_song_cycle(-1)
    elif g == "kit_next":
        _bb_kit_idx = (_bb_kit_idx + 1) % 128
        _bb_kit_select(_bb_kit_idx)
    elif g == "kit_prev":
        _bb_kit_idx = (_bb_kit_idx - 1) % 128
        _bb_kit_select(_bb_kit_idx)


def _execute_cmd(cmd):
    """Execute a web UI command dict. Called from the cmd poll thread."""
    action = cmd.get("action", "")
    if action == "TOGGLE_ROOT_MODE":
        state["root_mode"] = not state["root_mode"]
    elif action == "TOGGLE_KEY_LOCK":
        state["key_lock"] = not state["key_lock"]
    elif action == "TOGGLE_HOLD":
        state["hold"] = not state["hold"]
        if not state["hold"]: send_chord_off()
    elif action == "TOGGLE_VL":
        state["vl_enabled"] = not state["vl_enabled"]
        state["vl_strength"] = 1.0 if state["vl_enabled"] else 0.0
    elif action == "TOGGLE_MIRROR":
        state["mirror_input_ch"] = not state["mirror_input_ch"]
        send_chord_off(); all_notes_off()
    elif action == "TOGGLE_ARP_PT":
        state["arp_passthrough"] = not state["arp_passthrough"]
        state["live_notes"].clear(); send_chord_off(); all_notes_off()
    elif action == "PEDAL_CONFIG_SET":
        _pedal_conf_set(cmd.get("config"))
    elif action == "PADMAP_SET":
        _padmap_conf_set(cmd.get("config"))
    elif action == "SURFACE_SHOW":                  # /pads SHOW ON FIRE: back into surface mode, repainted
        surface_link.repaint(cmd.get("device") or None)
    # -- RC-505 looper control (orc1_rc505.py). All no-op if the RC is absent. --
    elif action == "RC505_ACTION" and rc505:
        rc505.do(cmd.get("name", ""), cmd.get("value"))   # named assign action
    elif action == "RC505_CC" and rc505:
        rc505.send_cc(int(cmd.get("cc", -1)), int(cmd.get("value", 0)))
    elif action == "RC505_MEMORY" and rc505:
        rc505.select_memory(int(cmd.get("program", 0)))   # 0-based; unit shows +1
    elif action == "RC505_TRANSPORT" and rc505:
        rc505.transport(bool(cmd.get("start", True)))
    elif action == "RC505_RELOAD_MAP" and rc505:
        rc505.load_map(getattr(cfg, "RC505_CONFIG", "~/rc505_tools/rc505_config.json"))
        print(f"  RC-505 map reloaded ({len(rc505.cc_map)} actions)")

    # -- BeatBuddy foot-drummer (orc1_beatbuddy.py). All no-op if the mind is absent.
    #    The gesture verbs map straight onto the ported firmware grammar. --
    elif action == "BB_SONG_SELECT" and beatbuddy:
        _bb_song_select(cmd.get("song", ""))
    elif action == "BB_KIT_SELECT" and beatbuddy:
        _bb_kit_select(int(cmd.get("program", 0)))
    elif action == "BB_OUTPUT":
        if cmd.get("dest") == "port" and _protect_err(cmd.get("channel") or 10):
            print_status(f"BeatBuddy output refused: {_protect_err(cmd.get('channel') or 10)}")
        else:
            o = _bb_out_set(cmd.get("dest"), cmd.get("port"), cmd.get("channel"))
            print_status("BeatBuddy output -> " + (
                f"port {o['port']} ch{o['channel']}" if o["dest"] == "port" else o["dest"]))
    elif action == "BB_TAP" and beatbuddy:
        beatbuddy.tap(_bb_now_ms())         # main -> drumfill; stopped -> start
    elif action == "BB_HOLD" and beatbuddy:
        beatbuddy.hold(_bb_now_ms())        # press + long-press -> transition
    elif action == "BB_RELEASE" and beatbuddy:
        beatbuddy.release(_bb_now_ms())     # release -> quit transition (advance part)
    elif action == "BB_DOUBLE" and beatbuddy:
        beatbuddy.double_tap(_bb_now_ms())  # outro (if any) else stop, on the bar
    elif action == "BB_PAUSE" and beatbuddy:
        beatbuddy.pause()                   # toggle pause / unpause
    elif action == "BB_STOP" and beatbuddy:
        beatbuddy.stop()
    elif action == "MIXTRACK_SYNC":
        # DJ mode handover: daw_mode.sh flipped /run/orc1-dj-active. Under the
        # scan lock (a hot-plug scan may be waking the Mixtrack right now); if
        # a scan hangs, skip -- the 3 s loop syncs on its own.
        if _hotplug_scan_lock.acquire(timeout=5):
            try:
                _mixtrack_sync_release()
            finally:
                _hotplug_scan_lock.release()
    elif isinstance(action, str) and action.startswith("KIKPAD_MAP_"):
        # The KIKPAD's own maps (orc1_kikpad_map, validated there by web_command). Runs on the map
        # link's worker: the SysEx round trips must never stall this thread.
        # "job" = the page's id for the click, echoed in the state file's last.job.
        km, job = kikpad_map, cmd.get("job")
        if km is None:
            pass
        elif action == "KIKPAD_MAP_READ":
            km.submit("read", km.read_all, job)
        elif action == "KIKPAD_MAP_PAD":
            a = [cmd.get(k) for k in ("bank", "pad", "type", "number", "channel", "colour")]
            km.submit(f"pad {a[0]}/{a[1]}", lambda: km.set_pad(*a), job)
        elif action == "KIKPAD_MAP_ENC":
            a = [cmd.get(k) for k in ("mode", "encoder", "cc", "channel")]
            km.submit(f"encoder {a[0]}/{a[1]}", lambda: km.set_encoder(*a), job)
        elif action == "KIKPAD_MAP_BANKCH":
            a = [cmd.get(k) for k in ("bank", "channel")]
            km.submit(f"bank {a[0]} channel", lambda: km.set_bank_channel(*a), job)
        elif action == "KIKPAD_MAP_RESET":
            w = cmd.get("what")
            km.submit(f"reset {w}", lambda: km.reset(w), job)
    elif action in ("CME_PRESET", "CME_ROUTE", "CME_FILTER", "CME_MAPPER", "CME_REFRESH"):
        # CME U6MIDI Pro config (orc1_cme). Runs on the link's OWN worker: a slow
        # or absent box must never stall this thread (a slow ack makes the web
        # retry through the fallback file = the command would run twice).
        # CME_PRESET {"preset": 1-4|"next"|"prev"} (no arg = next: pedal-friendly).
        # ROUTE/FILTER/MAPPER may carry "on_preset" (1-4: the preset the page
        # computed the edit from; refused if the box switched since -- absent =
        # no check, as from the FCB) and "job" (the page's id, echoed in last).
        if cme is not None:
            job, onp = cmd.get("job"), cmd.get("on_preset")
            if action == "CME_PRESET":
                want = cmd.get("preset", "next")
                cme.submit(f"preset {want}", lambda: cme.set_preset(want), job)
            elif action == "CME_ROUTE":
                inp, outs = cmd.get("input"), cmd.get("outputs") or []
                cme.submit(f"route in {inp} -> {outs}", lambda: cme.set_route(inp, outs, onp), job)
            elif action == "CME_FILTER":
                # {"port": 0-11, "channels": [1-16 to BLOCK], "types": [names to BLOCK]}
                fp, fch, fty = cmd.get("port"), cmd.get("channels") or [], cmd.get("types") or []
                cme.submit(f"filter port {fp}", lambda: cme.set_filter(fp, fch, fty, onp), job)
            elif action == "CME_MAPPER":
                # {"index": 0-15, "mapper": {...} | "clear"} -- validated by the
                # app's own rules in orc1_cme before anything is written
                mi, mm = cmd.get("index"), cmd.get("mapper")
                cme.submit(f"mapper {mi}", lambda: cme.set_mapper(mi, mm, onp), job)
            else:
                cme.submit("refresh", cme.refresh, job)
    elif action == "SKIPBACK_SAVE_ALL":
        # {"seconds": N (0/absent = all held in common), "tracks": [ids]}.
        # Pedal: cmd:SKIPBACK_SAVE_ALL.
        try:
            secs = int(cmd.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            secs = 0
        trk = cmd.get("tracks") or None
        if not isinstance(trk, list):
            trk = None
        skipback_save_all(secs, trk)
    elif action == "BB_NEXT" and beatbuddy:
        # One-shot "transition to the next part": enter the transition fill now,
        # then auto-quit it ~1 beat later on the clock (the quit is bar-quantised,
        # so the fill still plays out its bar before advancing). This gives a
        # single-press "next part" for a web click / MIDI trigger -- no physical
        # hold-duration needed, unlike the HOLD+RELEASE pair.
        global _bb_next_quit
        n = (beatbuddy.CurrSong.nPart + 1) if beatbuddy.CurrSong else 1
        beatbuddy.external_transition(n)     # enter transition; >nPart == next (wraps)
        _bb_next_quit = PPQN                  # pulses (1 beat) until the auto-quit fires
    elif action == "BB_TEMPO_MODE":
        # "song"  -> loading a song sets the ORC tempo to its native bpm (default)
        # "clock" -> leave the clock alone; songs play at the current tempo
        global _bb_follow_clock
        _bb_follow_clock = (str(cmd.get("mode", "song")).lower() == "clock")
        print(f"  BeatBuddy tempo mode: {'FOLLOW CLOCK' if _bb_follow_clock else 'SONG'}",
              flush=True)
    elif action == "BB_MATCH_BPM" and beatbuddy and arp:
        # snap the clock to the loaded song's native bpm on demand (works in
        # either tempo mode; no-op in external/drift clock, which owns tempo)
        if beatbuddy.CurrSong and beatbuddy.CurrSong.bpm and arp.clock_mode != "external":
            bpm = float(beatbuddy.CurrSong.bpm)
            (arp.set_drift_bpm if arp.clock_mode == "drift" else arp.set_internal_bpm)(bpm)
    elif action == "CYCLE_MELODY":
        modes = ["OFF", "SCALE", "CHORD"]
        idx = modes.index(state["melody_mode"]) if state["melody_mode"] in modes else 0
        state["melody_mode"] = modes[(idx + 1) % len(modes)]
    elif action == "TOGGLE_ARP" and arp:
        arp.set_enabled(not arp.enabled)
        if not arp.enabled: send_chord_off(); all_notes_off()
    elif action == "PATTERN_PREV" and arp:
        import orc1_patterns as _p
        order = _p.PATTERN_ORDER
        arp.set_pattern(order[(order.index(arp.pattern_name)-1) % len(order)], _p.PATTERNS)
    elif action == "PATTERN_NEXT" and arp:
        import orc1_patterns as _p
        order = _p.PATTERN_ORDER
        arp.set_pattern(order[(order.index(arp.pattern_name)+1) % len(order)], _p.PATTERNS)
    elif action == "SET_BPM" and arp:
        bpm = max(20.0, min(300.0, float(cmd.get("value", arp.internal_bpm))))
        if arp.clock_mode == "drift": arp.set_drift_bpm(bpm)
        else: arp.set_internal_bpm(bpm)
    elif action == "TAP_TEMPO":
        tap_tempo()
    elif action == "TOGGLE_CLOCK_OUT":
        if fire:
            fire.toggle_clock_out()
    elif action == "TOGGLE_DIN_ROUTING":
        state["din_routing"] = "passthrough" if state["din_routing"] == "engine" else "engine"
    elif action == "SET_CHORD_OUT":
        port = cmd.get("port") or None
        ch   = cmd.get("channel")
        with _state_lock:
            state["chord_out"]["port"]    = port
            state["chord_out"]["channel"] = int(ch) if ch else None
        send_chord_off()   # release any held notes before changing destination
        _save_chord_out()
        print_status(f"chord out → {port or 'preset'} ch{ch or '(preset)'}")
    elif action == "CLEAR_CHORD_OUT":
        with _state_lock:
            state["chord_out"] = {"port": None, "channel": None}
        send_chord_off()
        _save_chord_out()
        print_status("chord out → preset routing")
    elif action == "SET_PERFORMVE_FEED":
        on = _apply_performve_feed(cmd.get("on"))
        print_status(f"Perform-VE follow → {'ON' if on else 'off'}")
    elif action == "SET_PERFORMVE_SRC":
        src = _apply_performve_src(cmd.get("port") or None, cmd.get("channel"))
        print_status(f"Perform-VE source → {src['port'] or 'off'} "
                     f"ch{src['channel'] or '(any)'}")
    elif action == "SET_PERFORMVE_CC":
        sent = _apply_performve_cc(cmd.get("cc"), cmd.get("value"))
        if sent:
            print_status(f"Perform-VE CC{sent[0]} = {sent[1]}")
    elif action == "SET_PERFORMVE_PC":
        pr = _apply_performve_pc(cmd.get("preset"))
        if pr is not None:
            print_status(f"Perform-VE preset → PC{pr}")
    elif action == "SET_MELODY_OUT":
        port = cmd.get("port") or None
        ch   = cmd.get("channel")
        with _state_lock:
            state["melody_out"]["port"]    = port
            state["melody_out"]["channel"] = int(ch) if ch else None
        _save_melody_out()
        print_status(f"melody out → {port or 'pisound'} ch{ch or cfg.MELODY_CH}")
    elif action == "CLEAR_MELODY_OUT":
        with _state_lock:
            state["melody_out"] = {"port": None, "channel": None}
        _save_melody_out()
        print_status(f"melody out → pisound ch{cfg.MELODY_CH} (default)")
    elif action == "SET_VOICE_THRU":
        # Set or update a thru mapping for one source channel.
        # cmd: {src_ch: int 1-16, port: str, channel: int 1-16}
        src_ch  = str(int(cmd.get("src_ch", 0)))
        port    = cmd.get("port") or None
        ch      = cmd.get("channel")
        if src_ch == "0" or not port or not ch:
            print_status("SET_VOICE_THRU: missing src_ch / port / channel")
        else:
            with _state_lock:
                state["voice_thru"][src_ch] = {
                    "port":    port,
                    "channel": int(ch),
                }
            _save_voice_thru()
            print_status(f"voice thru ch{src_ch} → {port} ch{ch}")
    elif action == "CLEAR_VOICE_THRU":
        # Clear thru for one channel (src_ch) or all (src_ch omitted / 0).
        src_ch = str(int(cmd.get("src_ch", 0)))
        with _state_lock:
            if src_ch == "0":
                state["voice_thru"].clear()
                print_status("voice thru → all cleared")
            else:
                state["voice_thru"].pop(src_ch, None)
                print_status(f"voice thru ch{src_ch} → cleared")
        _save_voice_thru()

    elif action == "MODULE_BIND":
        # Module Matrix: reassign a voice's listen channel, live + persisted.
        module_bind(str(cmd.get("module", "")), cmd.get("channel"))

    elif action == "MODULE_UNBIND":
        # Module Matrix: take a voice out of the fold (its channel goes dead).
        module_unbind(str(cmd.get("module", "")))

    elif action == "MODULE_JOIN":
        # Module Matrix: live P1/P2 join. Any of mode / detune / spread; only the
        # provided fields change. Ephemeral -- resets to 'none' on restart.
        module_join(mode=cmd.get("mode"), detune=cmd.get("detune"),
                    spread=cmd.get("spread"))

    elif action == "SET_VOICE_MAP":
        vm = dict(cmd.get("voice_map", {}) or {})
        old_vm = state.get("voice_map") or {}
        for k, v in list(vm.items()):       # a NEW pick of a protected channel is refused (an old one stays)
            ch = v.get("channel") if isinstance(v, dict) else None
            if isinstance(ch, int) and _protect_err(ch) and v != old_vm.get(k):
                print(f"  ! voice_map {k}: {_protect_err(ch)}", flush=True)
                if k in old_vm:
                    vm[k] = old_vm[k]
                else:
                    vm.pop(k)
        with _state_lock:
            state["voice_map"] = vm
        send_chord_off()
        _save_voice_map()
        n_routed = len([v for v in vm.values() if v is not None])
        n_silent = len([v for v in vm.values() if v is None])
        print_status(f"voice_map → {n_routed} routed, {n_silent} silenced")
    elif action == "CLEAR_VOICE_MAP":
        with _state_lock:
            state["voice_map"] = {}
        send_chord_off()
        _save_voice_map()
        print_status("voice_map cleared → preset routing")
    elif action == "SET_PRESET":
        name = cmd.get("preset", "")
        if name in cfg.PRESETS:
            send_chord_off()
            all_notes_off()
            state["preset"] = name
            if name != cfg.DEFAULT_PRESET:
                state["last_multi"] = name
            print_status(f"preset → {name}")
    elif action == "SAVE_AS_PRESET":
        # Save the current voice_map (merged on top of the active preset) as a
        # named USER_N slot.  The result is live immediately (no restart) and
        # persisted to the config overrides file so it survives restarts.
        name = cmd.get("preset", "")
        if name.startswith("USER_") and name in cfg.PRESETS:
            with _state_lock:
                vm  = dict(state["voice_map"])
                cur = state["preset"]
            # Build the full slot dict: start from the current preset, then
            # apply live voice_map overrides so the saved preset captures the
            # exact routing owner is hearing right now.
            VALID_SLOTS = {"CHORD_ALL","CHORD_ROOT","CHORD_3RD","CHORD_5TH",
                           "CHORD_7TH","CHORD_9TH","BASS","LEAD","ARP_DEST"}
            base = {k: v for k, v in cfg.PRESETS.get(cur, {}).items()
                    if k in VALID_SLOTS}
            for slot, dest in vm.items():
                if slot in VALID_SLOTS:
                    base[slot] = dest   # None = silence, dict = routed
            _save_preset_override(name, base)
            with _state_lock:
                state["preset"] = name
                state["voice_map"] = {}   # voice_map is now baked into the preset
            send_chord_off()
            _save_voice_map()
            print_status(f"saved + switched → preset {name}")
        else:
            print_status(f"SAVE_AS_PRESET: invalid slot {name!r}")
    elif action == "TOGGLE_SYNTH_MIRROR_POLY":
        state["synth_mirror_poly"] = not state["synth_mirror_poly"]
        if not state["synth_mirror_poly"]: _synth_mirror_off()
    elif action == "TOGGLE_SYNTH_MIRROR_CHIP":
        state["synth_mirror_chip"] = not state["synth_mirror_chip"]
        if not state["synth_mirror_chip"]: _synth_mirror_off()
    elif action == "MIDI_RESCAN":
        # Force the hot-plug thread to pick up new devices immediately. One
        # scan at a time (_hotplug_scan): if the 3 s loop is mid-scan this is a
        # no-op -- that scan covers it (two at once opened controllers twice).
        threading.Thread(target=_hotplug_scan, args=(False,),
                         daemon=True).start()
    elif action == "PANIC":
        # PANIC is now a true master stop: halts the sequencer, arp, and any
        # latched sampler loop in addition to flushing notes (2026-07-19).
        master_stop()
    elif action == "SAMPLER_CAPTURE":
        sampler_capture(cmd.get("deck", "B"), cmd.get("seconds"))
    elif action == "SAMPLER_CAPTURE_BARS":
        sampler_capture_bars(cmd.get("deck", "B"), float(cmd.get("bars", 4)))
    elif action == "SAMPLER_LOOP":
        sampler_loop_toggle(cmd.get("deck", "B"))
    elif action == "SAMPLER_AUDITION":
        sampler_audition(cmd.get("deck", "B"))
    elif action == "SAMPLER_REVERSE":
        sampler_reverse(cmd.get("deck", "B"))
    elif action == "SAMPLER_EXPORT":
        sampler_export(cmd.get("deck", "B"))
    elif action == "SAMPLER_GATE":
        sampler_gate(cmd.get("deck", "B"), bool(cmd.get("on", True)))
    elif action == "SAMPLER_STOP":
        # Hard stop + rewind (kills a latched loop too). The web STOP used to
        # send GATE off, which only releases the hold-to-play gate — a latched
        # loop kept playing with no obvious way to stop it from the page.
        sampler_stop(cmd.get("deck", "B"))
    elif action == "SAMPLER_SPEED":
        sampler_speed(cmd.get("deck", "B"), int(cmd.get("value", 64)))
    elif action == "SAMPLER_GAIN":
        sampler_gain(cmd.get("deck", "B"), int(cmd.get("value", 96)))
    elif action == "SAMPLER_MASTER_GAIN":
        # Module Matrix LEVEL fader for the sampler module (whole mix, both decks).
        sampler_master_gain(int(cmd.get("value", 96)))
    elif action == "SAMPLER_XFADE":
        sampler_crossfade(int(cmd.get("value", 64)))
    elif action == "SAMPLER_EQ":
        sampler_eq(cmd.get("deck", "B"), cmd.get("band", "mid"),
                   int(cmd.get("value", 64)))
    elif action == "SAMPLER_FILTER":
        sampler_filter(cmd.get("deck", "B"), int(cmd.get("value", 64)))
    elif action == "SAMPLER_BOUNDS":
        sampler_bounds(cmd.get("deck", "B"), cmd.get("start"), cmd.get("end"))
    elif action == "SAMPLER_LOAD_FILE":
        sampler_load_file(cmd.get("deck", "A"), cmd.get("path", ""))
    elif action == "SAMPLER_NUDGE":
        _ms    = cmd.get("ms")
        _beats = cmd.get("beats")
        sampler_nudge(cmd.get("deck", "B"),
                      ms=(float(_ms) if _ms is not None else None),
                      beats=(float(_beats) if _beats is not None else None))
    elif action == "SAMPLER_FWD_FIXED":
        sampler_capture_fwd_fixed(cmd.get("deck", "B"), float(cmd.get("bars", 2)))
    elif action == "SAMPLER_FWD_HOLD_START":
        sampler_capture_fwd_hold_start(cmd.get("deck", "B"))
    elif action == "SAMPLER_FWD_HOLD_END":
        _tb = cmd.get("tap_bars")
        sampler_capture_fwd_hold_end(cmd.get("deck", "B"),
                                     float(_tb) if _tb is not None else None)
    elif action == "SAMPLER_FWD_CANCEL":
        sampler_capture_fwd_cancel(cmd.get("deck", "B"))
    elif action == "SAMPLER_PRESS_TRIM":
        sampler_press_trim(float(cmd.get("ms", 0)))
    elif action == "SAMPLER_ONSET_SNAP":
        sampler_onset_snap(bool(cmd.get("on", True)))
    elif action == "SAMPLER_ANCHOR":
        sampler_set_anchor(cmd.get("mode", "press"))
    elif action == "SAMPLER_SET_SOURCE":
        sampler_set_source(cmd.get("deck", "A"), cmd.get("track", 0))
    elif action == "SAMPLER_BR_ON":
        sampler_beat_repeat_on(cmd.get("deck", "B"), float(cmd.get("beats", 1.0)))
    elif action == "SAMPLER_BR_OFF":
        sampler_beat_repeat_off(cmd.get("deck", "B"))
    elif action == "AUTOSAMPLE_START":
        ok, err = _get_autosampler().start(cmd.get("params", {}))
        print_status("autosample: started" if ok else f"autosample: {err}")
    elif action == "AUTOSAMPLE_CANCEL":
        if _autosampler:
            _autosampler.cancel()
            print_status("autosample: cancel requested")
    elif action == "ARP_RATE_NEXT" and arp:
        from orc1_arp import SUBDIVISION_LIST
        idx = SUBDIVISION_LIST.index(arp.subdivision) if arp.subdivision in SUBDIVISION_LIST else 7
        arp.set_subdivision(SUBDIVISION_LIST[min(idx+1, len(SUBDIVISION_LIST)-1)])
    elif action == "ARP_RATE_PREV" and arp:
        from orc1_arp import SUBDIVISION_LIST
        idx = SUBDIVISION_LIST.index(arp.subdivision) if arp.subdivision in SUBDIVISION_LIST else 7
        arp.set_subdivision(SUBDIVISION_LIST[max(idx-1, 0)])
    elif action == "ARP_GATE_DELTA" and arp:
        arp.set_gate_trim(round(arp.gate_trim + float(cmd.get("value",0.05)), 2))
    elif action == "ARP_SWING_DELTA" and arp:
        arp.set_swing(round(arp.swing_amount + float(cmd.get("value",0.05)), 2))
    elif action == "ARP_HUMANIZE_DELTA" and arp:
        arp.set_humanize(round(arp.humanize + float(cmd.get("value",0.05)), 2))
    elif action == "TOGGLE_CLOCK_SRC" and arp:
        # Cycle: internal -> external -> drift -> internal.
        # "internal"  -- Pi generates its own clock at the set BPM.
        # "external"  -- Pi follows incoming MIDI clock (from DIN or USB gear).
        # "drift"     -- like internal but the BPM can be nudged to drift gently
        #               off the grid.  Useful for subtle feel, dangerous if left
        #               on accidentally -- use the CLK button to get back to INT.
        from orc1_arp import CLOCK_INTERNAL, CLOCK_EXTERNAL, CLOCK_DRIFT
        _modes = [CLOCK_INTERNAL, CLOCK_EXTERNAL, CLOCK_DRIFT]
        _cur   = arp.clock_mode
        _next  = _modes[(_modes.index(_cur) + 1) % len(_modes)] \
                 if _cur in _modes else CLOCK_INTERNAL
        _din_auto_synced = False
        arp.set_clock_mode(_next)
    elif action == "SET_CLOCK_SRC" and arp:
        # Set the clock source directly (used by the web, which only offers
        # INT / EXT -- unlike TOGGLE_CLOCK_SRC's INT->EXT->DRIFT cycle, so the
        # web toggle can never land on DRIFT).  DRIFT is still accepted here
        # and stays reachable via the Fire's ARP_CLOCK_MODE CC.
        #   internal -- Pi generates its own clock at the set BPM.
        #   external -- Pi follows incoming MIDI clock (clock IN, DIN or USB).
        from orc1_arp import CLOCK_INTERNAL, CLOCK_EXTERNAL, CLOCK_DRIFT
        _valid = {CLOCK_INTERNAL, CLOCK_EXTERNAL, CLOCK_DRIFT}
        _mode  = str(cmd.get("mode", "")).lower()
        if _mode in _valid:
            _din_auto_synced = False
            arp.set_clock_mode(_mode)
    elif action == "TOGGLE_ARP_HOLD":
        # ARP HOLD -- when ON the arp keeps looping after you release the note
        # (latch mode).  When OFF the arp stops as soon as the note is released
        # (gate mode).  Turning hold OFF while the arp is running clears the
        # chord immediately so the arp goes quiet at the end of the current step.
        state["arp_hold"] = not state.get("arp_hold", False)
        if not state["arp_hold"] and arp and arp.enabled:
            arp.clear_chord()
        print_status(f"ARP HOLD {'ON' if state['arp_hold'] else 'OFF'}")
    elif action == "SET_KEY_LOCK_CH":
        state["key_lock_ch"] = int(cmd.get("value", state.get("key_lock_ch", 0)))
    elif action == "ROOT_SET":
        set_root(int(cmd.get("value", 0)))
    elif action == "ROOT_STEP":
        set_root(state["root"] + int(cmd.get("value", 1)))
    elif action == "SCALE_NEXT":
        cycle_scale(+1)
    elif action == "SCALE_PREV":
        cycle_scale(-1)
    elif action == "SCALE_SET":
        set_scale(cmd.get("value", ""))
    elif action == "TOGGLE_SCALE_SNAP":
        # Legacy binary toggle, kept so old front-ends and any saved macro keep
        # working. Off <-> nearest, which is exactly what the boolean meant.
        set_scale_mode("off" if scale_mode_now() != "off" else "nearest")
        print_status(f"SCALE MODE {scale_mode_now().upper()}")
    elif action == "SET_SCALE_MODE":
        if set_scale_mode(str(cmd.get("value", "")).lower()):
            print_status(f"SCALE MODE {scale_mode_now().upper()}")
        else:
            return {"ok": False, "error": "bad scale mode"}
    elif action == "CYCLE_SCALE_MODE":
        order = list(SCALE_MODES)
        step  = int(cmd.get("value", 1) or 1)
        set_scale_mode(order[(order.index(scale_mode_now()) + step) % len(order)])
        print_status(f"SCALE MODE {scale_mode_now().upper()}")
    elif action == "SET_QUALITY_MODE":
        v = str(cmd.get("value", "")).upper()
        if v not in ("DIAT", "FIXED"):
            return {"ok": False, "error": "bad quality mode"}
        state["quality_mode"] = v
        # Cleared in BOTH directions -- see TOGGLE_QUALITY_MODE for why the
        # DIAT->FIXED case matters too.
        state["base_override"] = False
        _retrigger_chord()
        print_status(f"QUALITY {v}")
    elif action == "TOGGLE_QUALITY_MODE":
        state["quality_mode"] = ("FIXED" if state.get("quality_mode") != "FIXED"
                                 else "DIAT")
        # Clear the momentary flag in BOTH directions. Going DIAT->FIXED after
        # auditioning a quality pad -- the natural "try it, then lock it in"
        # workflow -- otherwise left base_override set until the next key press,
        # so the readouts showed "FIXED*" and /state reported base_override
        # true: a persistent setting advertising itself as momentary, which is
        # the exact phantom-"*" this design was meant to prevent.
        state["base_override"] = False
        _retrigger_chord()
        print_status(f"QUALITY {state['quality_mode']}")
    elif action == "RELOAD_ROUTES":
        if router:
            router.reload()

    # FX chain commands -- routable from the web UI, command socket, or any
    # external tool that can write to the command socket / orc1_cmd.json.
    # The Fire uses eng.fx directly (not these commands) but having them here
    # means web buttons, scripts, or a future MIDI-to-command layer can also
    # start/stop/toggle chains by name.
    elif action == "FX_START" and fx:
        name = cmd.get("name", "")
        if name:
            fx.start_chain(name)
    elif action == "FX_STOP" and fx:
        name = cmd.get("name", "")
        if name:
            fx.stop_chain(name)
    elif action == "FX_TOGGLE" and fx:
        name = cmd.get("name", "")
        if name:
            fx.toggle_chain(name)

    elif action == "SET_FX_IO":
        # Switch FX I/O mode (INTERNAL vs HARDWARE INSERT) and/or port names.
        # Payload: {mode, send_l, send_r, return_l, return_r}
        # Stops all running chains first (they're wired to old ports).
        # Clears the FXManager cache so the next start re-resolves tokens.
        try:
            io_file = os.path.expanduser("~/.orc1_fx_io.json")
            current = {}
            try:
                with open(io_file) as _f:
                    current = json.load(_f)
            except Exception:
                pass
            # "module" = INTERNAL mode's source, by NAME (resolved live by
            # orc1_config.fx_io_resolve); "" = custom ports.
            for key in ("mode", "module", "send_l", "send_r", "return_l", "return_r"):
                if key in cmd:
                    current[key] = cmd[key]
            tmp = io_file + ".tmp"
            with open(tmp, "w") as _f:
                json.dump(current, _f)
            os.replace(tmp, io_file)
            # Stop all running chains before changing port routing
            if fx:
                running_chains = [n for n, info in fx.status().items() if info.get("running")]
                for _chain in running_chains:
                    fx.stop_chain(_chain)
                import time as _t
                if running_chains:
                    _t.sleep(1.5)  # let stop threads finish
                fx.clear_cache()
                # Reconcile the rack too: its jalv are wired to the OLD ports,
                # so rebuild against the new I/O (also restores the dry path if
                # we just left internal mode).
                if getattr(fx, "rack", None):
                    fx.rack.apply()
        except Exception as _e:
            pass

    elif action == "FX_PARAM" and fx:
        # Real-time LV2 parameter change on a running chain.
        # Payload: {chain, symbol, value, plugin_idx=0}
        chain      = cmd.get("chain", "")
        symbol     = cmd.get("symbol", "")
        value      = cmd.get("value")
        plugin_idx = int(cmd.get("plugin_idx", 0))
        if chain and symbol and value is not None:
            fx.send_param(chain, symbol, float(value), plugin_idx)

    # ── FX RACK (3-slot series stack) ─────────────────────────────────────────
    # The new-style FX: drive eng.fx.rack from the web /fx page and the Fire.
    # All mutations go through here (the brain owns the jalv processes); the web
    # reads state back from ~/.orc1_fx_rack.json + ~/.orc1_fx_rack_status.json.
    elif action == "RACK_ASSIGN" and fx and fx.rack:
        # Drop a plugin into a slot (or None to clear).  Payload: {idx, plugin}
        fx.rack.assign(int(cmd.get("idx", -1)), cmd.get("plugin") or None)
    elif action == "RACK_CLEAR" and fx and fx.rack:
        fx.rack.clear(int(cmd.get("idx", -1)))
    elif action == "RACK_TOGGLE" and fx and fx.rack:
        # Enable/disable a slot without losing its plugin.  Payload: {idx}
        fx.rack.toggle(int(cmd.get("idx", -1)))
    elif action == "RACK_ENABLE" and fx and fx.rack:
        # Explicit on/off.  Payload: {idx, enabled}
        fx.rack.set_enabled(int(cmd.get("idx", -1)), bool(cmd.get("enabled")))
    elif action == "RACK_MOVE" and fx and fx.rack:
        # Reorder within the series.  Payload: {idx, delta}  (delta -1 / +1)
        fx.rack.move(int(cmd.get("idx", -1)), int(cmd.get("delta", 0)))
    elif action == "RACK_PARAM" and fx and fx.rack:
        # Live knob.  Payload: {idx, symbol, value}
        _v = cmd.get("value")
        if _v is not None:
            fx.rack.param(int(cmd.get("idx", -1)), cmd.get("symbol", ""), float(_v))
    elif action == "RACK_APPLY" and fx and fx.rack:
        fx.rack.apply()

    elif action == "SYNTH_CC":
        # Forward a raw CC to the synth bus and remember it for read-back.
        # Payload: {ch, cc, value}  (ch is 1-based; value 0-127)
        try:
            ch    = int(cmd["ch"])
            cc    = int(cmd["cc"])
            value = int(cmd["value"])
            if 1 <= ch <= 16 and 0 <= cc <= 127 and 0 <= value <= 127:
                send_synth_midi(mido.Message("control_change",
                                             channel=ch - 1,
                                             control=cc,
                                             value=value))
                # Record for state read-back (string keys survive JSON round-trip)
                sch = state["synth_cc"]
                if str(ch) not in sch:
                    sch[str(ch)] = {}
                sch[str(ch)][str(cc)] = value
        except (KeyError, ValueError, TypeError):
            pass

    elif action == "SYNTH_PC":
        # Forward a Program Change to the synth bus (2026-09-24: the /chip GB
        # wave SLOTS -- PC 0-15 recalls a slot, or stores into it while CC 115
        # is held). Payload: {ch, program}  (ch 1-based; program 0-127)
        try:
            ch      = int(cmd["ch"])
            program = int(cmd["program"])
            if 1 <= ch <= 16 and 0 <= program <= 127:
                send_synth_midi(mido.Message("program_change",
                                             channel=ch - 1, program=program))
        except (KeyError, ValueError, TypeError):
            pass

    elif action == "SYNTH_PITCH":
        # Forward a pitchwheel (pitch bend) message to the synth bus.
        # Payload: {ch, pitch}  (ch is 1-based; pitch -8192..8191, 0 = centred)
        # Used by the web /chip bend slider + the Fire's pitch_out knob target;
        # the synth applies it to the chip voices (pulse1/pulse2/GB wave).
        try:
            ch    = int(cmd["ch"])
            pitch = int(cmd["pitch"])
            # Clamp pitch into the legal MIDI range rather than rejecting --
            # a slider rounding to -8193 shouldn't silently do nothing.
            pitch = max(-8192, min(8191, pitch))
            if 1 <= ch <= 16:
                send_synth_midi(mido.Message("pitchwheel",
                                             channel=ch - 1,
                                             pitch=pitch))
        except (KeyError, ValueError, TypeError):
            pass

    elif action == "SYNTH_NOTE":
        # Fire-and-forget audition note on the synth bus.
        # Payload: {ch, note, vel, ms}  (ch 1-based; ms capped at 5000)
        try:
            ch   = int(cmd["ch"])
            note = int(cmd["note"])
            vel  = int(cmd.get("vel", 100))
            ms   = min(int(cmd.get("ms", 500)), 5000)
            if 1 <= ch <= 16 and 0 <= note <= 127 and 0 <= vel <= 127:
                ch0 = ch - 1
                send_synth_midi(mido.Message("note_on",  channel=ch0, note=note, velocity=vel))
                threading.Timer(ms / 1000.0,
                                lambda c=ch0, n=note: send_synth_midi(
                                    mido.Message("note_off", channel=c, note=n, velocity=0))
                                ).start()
        except (KeyError, ValueError, TypeError):
            pass

    elif action == "FXMIX_SET":
        # Ride an fx INSERT's wet/dry + return level (audio-rate, glitch-free).
        # Payload: {id?, mix?: 0..1, level?: 0..1}. With `id`, the insert manager
        # resolves that insert's own CH_CTRL mix/level CC pair; without it, the
        # legacy single-mixer pair (3/4). Emits the CCs on the synth bus; the
        # mixer instance slews to them per-sample.
        try:
            _mmcfg = cfg.SYNTH_VOL
            _cc_ch = int(_mmcfg.get("CH", 13)) - 1
            _iid = cmd.get("id")
            _pair = (fx_inserts.insert_ccs(_iid)
                     if (_iid and fx_inserts is not None) else None)
            if _pair:
                _mix_cc, _lvl_cc = _pair
            else:
                _mix_cc = int(_mmcfg.get("FXMIX_MIX_CC", 3))
                _lvl_cc = int(_mmcfg.get("FXMIX_LEVEL_CC", 4))
            if "mix" in cmd:
                _v = max(0, min(127, int(round(float(cmd["mix"]) * 127))))
                send_synth_midi(mido.Message("control_change", channel=_cc_ch,
                                control=_mix_cc, value=_v))
            if "level" in cmd:
                # level 0..1 UI -> 0..127 where 64 = unity (mixer scales /64 -> 0..2)
                _v = max(0, min(127, int(round(float(cmd["level"]) * 127))))
                send_synth_midi(mido.Message("control_change", channel=_cc_ch,
                                control=_lvl_cc, value=_v))
        except (KeyError, ValueError, TypeError):
            pass

    elif action == "FX_INSERT_APPLY":
        # Re-read ~/.orc1_fx_inserts.json and (re)apply -- add/remove/reconfigure
        # inserts live. Runs off the socket thread (it spawns processes + sleeps).
        if fx_inserts is not None:
            try:
                threading.Thread(target=fx_inserts.apply, daemon=True).start()
            except Exception:
                pass

    elif action == "KNOBMAP_SET":
        # Assign a knob target from the web CONTROL page.
        # Payload: {view, layer, knob, target}
        # target is either a named KNOB_TARGETS action tuple OR an arbitrary
        # cc_out tuple: ["cc_out", port_str, channel(1-16), cc(0-127)].
        # 2026-10-01: this used to require a Fire (`... and fire:`), so a
        # /control edit with the Fire unplugged was silently dropped; now it
        # goes straight to ~/fire_knobmap.json and the Fire loads it on plug-in
        # (same fix CHORD_ROWS_SET got 2026-09-24).
        try:
            view   = cmd.get("view", fire.view if fire else None)
            layer  = cmd.get("layer", "base")
            knob   = int(cmd.get("knob", 0))
            target = cmd.get("target")  # list, e.g. ["voice_param","attack"]
            if target is None or layer not in ("base", "shift", "alt") or not (0 <= knob <= 3):
                pass
            else:
                valid_actions = {tuple(t) for _, t in cfg.FIRE.get("KNOB_TARGETS", [])}
                tup = tuple(target)
                # Use the SAME validator the Fire's own loader uses, rather
                # than a second copy of the rule. The copy that lived here
                # accepted channel 0 while _well_formed_send_action requires
                # 1-16, so a CC assigned on channel 0 from /control appeared to
                # save, silently sent nothing (mido rejects channel -1 after
                # the ch-1 conversion, inside a bare except), and was dropped
                # on the next restart -- reopening the very "arbitrary CC does
                # not survive a reboot" bug at one boundary value.
                from orc1_fire import (_well_formed_send_action,
                                       _target_allowed_in_view)
                is_cc_out = _well_formed_send_action(target)
                # Same per-view rule the on-device menu applies: a drum-sculpt
                # target on UNIVERSAL/FADERS applies live, sculpts an unrelated
                # voice, hijacks the OLED, and is then stripped at the next
                # restart while this page still shows it as assigned.
                if is_cc_out and isinstance(target[2], int) and _protect_err(target[2]):
                    print(f"  ! KNOBMAP_SET: rejected {view}/{layer}/knob{knob}: "
                          f"{_protect_err(target[2])}", flush=True)
                elif not ((tup in valid_actions or is_cc_out)
                          and _target_allowed_in_view(tup, view)):
                    print(f"  ! KNOBMAP_SET: rejected {view}/{layer}/knob{knob} "
                          f"-> {target!r}", flush=True)
                elif fire:
                    fire._dialmap.setdefault(view, {}).setdefault(layer, {})[knob] = tup
                    fire._save_knobmap()
                    print_status(f"knobmap {view}/{layer}/knob{knob} → {target[0]}")
                else:
                    from orc1_fire import knobmap_set_offline
                    knobmap_set_offline(cfg.FIRE.get("KNOBMAP_FILE"),
                                        view, layer, knob, tup)
                    print_status(f"knobmap {view}/{layer}/knob{knob} → {target[0]} "
                                 f"(saved; no Fire)")
        except Exception as _e:
            print(f"  ! KNOBMAP_SET: {_e}", flush=True)

    elif action == "UNIVERSAL_SET_PAD":
        # Assign a universal pad from the web CONTROL page.
        # Payload: {row, col, type, number, value, channel, port, latch,
        #           shift_action?, alt_action?}
        # Works whether or not the Fire is plugged in — writes the state file
        # directly if fire is None (Fire reads it on next connect/restart).
        try:
            row  = int(cmd["row"])
            col  = int(cmd["col"])
            if not (0 <= row <= 3 and 0 <= col <= 15):
                raise ValueError("row/col out of range")
            entry = {
                "type":    cmd.get("type", "note"),
                "number":  int(cmd.get("number", 60)),
                "value":   int(cmd.get("value",  100)),
                "channel": int(cmd.get("channel", 1)),
                "port":    cmd.get("port", "pisound"),
                "latch":   bool(cmd.get("latch", False)),
                "_latched_on": False,
            }
            if cmd.get("shift_action"):
                entry["shift_action"] = cmd["shift_action"]
            if cmd.get("alt_action"):
                entry["alt_action"] = cmd["alt_action"]
            _bank = _ucs_bank(cmd, fire)
            if fire:
                _ucs_bank_pads(fire, _bank)[(row, col)] = entry
                fire._save_fire_state()
                # Only repaint if the player is actually looking at this bank.
                if (fire.view == "universal"
                        and getattr(fire, "_universal_bank", 0) == _bank):
                    fire._render_universal()
            else:
                # Fire not connected — persist to state file directly so it
                # loads on next Fire connect/orc1 restart.
                _clean = {k: v for k, v in entry.items() if not k.startswith("_")}
                _ucs_state_update(
                    _bank, lambda p: p.__setitem__(f"{row},{col}", _clean))
            print_status(f"universal pad b{_bank} ({row},{col}) "
                         f"→ {entry['type']} {entry['number']}")
        except Exception as _e:
            pass

    elif action == "UNIVERSAL_CLEAR_PAD":
        # Clear one or all universal pad assignments.
        # Payload: {row, col}  or  {all: true}
        # `all` clears the addressed BANK, not every bank -- same scoping as
        # the Fire's own "Clear This Bank" menu item. A single call that wipes
        # up to 9 x 64 assignments, most of them not on screen anywhere, is not
        # what "clear" means on a page showing one 4x16 grid.
        try:
            _bank = _ucs_bank(cmd, fire)
            _repaint = (fire is not None and fire.view == "universal"
                        and getattr(fire, "_universal_bank", 0) == _bank)
            if cmd.get("all"):
                if fire:
                    _ucs_bank_pads(fire, _bank).clear()
                    fire._save_fire_state()
                    if _repaint:
                        fire._render_universal()
                else:
                    _ucs_state_update(_bank, lambda p: p.clear())
                print_status(f"universal pads: bank {_bank} cleared")
            else:
                row = int(cmd["row"])
                col = int(cmd["col"])
                if fire:
                    _ucs_bank_pads(fire, _bank).pop((row, col), None)
                    fire._save_fire_state()
                    if _repaint:
                        fire._render_universal()
                else:
                    _ucs_state_update(
                        _bank, lambda p: p.pop(f"{row},{col}", None))
                print_status(f"universal pad b{_bank} ({row},{col}) cleared")
        except Exception as _e:
            pass

    elif action == "UNIVERSAL_SET_KNOB":
        # Assign (or clear) ONE per-page knob on the universal surface from the
        # web CONTROL page (Fire reorg Phase 2).
        # Payload: {bank?, layer, knob, knob_action}  -- layer in base|shift|alt,
        #   knob 0-3, knob_action = a list like ["cc_out","synth",8,74] or
        #   ["bpm"], or null/omitted to CLEAR that knob (fall back to baseline).
        #   NB the field is `knob_action`, NOT `action` -- `action` is already
        #   the command-name key this dispatcher switches on.
        # Works with the Fire absent (writes the state file for next connect),
        # exactly like UNIVERSAL_SET_PAD.
        try:
            layer = cmd.get("layer", "base")
            knob  = int(cmd.get("knob", 0))
            act   = cmd.get("knob_action", None)
            if layer not in ("base", "shift", "alt") or not (0 <= knob <= 3):
                raise ValueError("bad layer/knob")
            _bank = _ucs_bank(cmd, fire)
            if fire:
                ok = fire.set_universal_knob(_bank, layer, knob, act)
                if ok and fire.view == "universal" \
                        and getattr(fire, "_universal_bank", 0) == _bank:
                    fire._oled()
                print_status(f"universal knob b{_bank} {layer} k{knob} "
                             f"→ {act if act else 'cleared'}"
                             + ("" if ok else "  [REJECTED]"))
            else:
                if act is None:
                    _ucs_knob_state_update(
                        _bank, layer, lambda kd: kd.pop(str(knob), None))
                else:
                    _ucs_knob_state_update(
                        _bank, layer,
                        lambda kd: kd.__setitem__(str(knob), list(act)))
                print_status(f"universal knob b{_bank} {layer} k{knob} "
                             f"→ {act if act else 'cleared'} (offline)")
        except Exception as _e:
            pass

    elif action == "UNIVERSAL_SET_FADER":
        # Make (or clear) a FADER row on the universal surface (Fire reorg P3).
        # Payload: {bank?, row, cc, channel?, port?}  -- row 0-3; sets that row
        #   as a horizontal CC fader. {bank?, row, clear:true} removes it.
        # Works Fire-connected or absent (writes the state file), like the pads.
        try:
            row = int(cmd.get("row", 0))
            if not (0 <= row <= 3):
                raise ValueError("bad row")
            _bank = _ucs_bank(cmd, fire)
            _clear = bool(cmd.get("clear"))
            if not _clear and _protect_err(cmd.get("channel", 1)):
                raise ValueError(_protect_err(cmd.get("channel", 1)))
            if fire:
                if _clear:
                    ok = fire.clear_universal_fader(_bank, row)
                else:
                    ok = fire.set_universal_fader(
                        _bank, row, cmd.get("cc", 7),
                        cmd.get("channel", 1), cmd.get("port", "pisound"))
                if ok and fire.view == "universal" \
                        and getattr(fire, "_universal_bank", 0) == _bank:
                    fire._render_universal(); fire._oled()
                print_status(f"universal fader b{_bank} row{row} "
                             f"→ {'cleared' if _clear else cmd.get('cc')}"
                             + ("" if ok else "  [REJECTED]"))
            else:
                if _clear:
                    _ucs_fader_state_update(
                        _bank, lambda rd: rd.pop(str(row), None))
                else:
                    cc = int(cmd.get("cc", 7)); ch = int(cmd.get("channel", 1))
                    port = str(cmd.get("port", "pisound"))
                    if not (0 <= cc <= 127 and 1 <= ch <= 16 and port):
                        raise ValueError("bad cc/channel/port")
                    _ucs_fader_state_update(_bank, lambda rd: rd.__setitem__(
                        str(row), {"cc": cc, "channel": ch, "port": port,
                                   "value": int(rd.get(str(row), {}).get("value", 0))}))
                print_status(f"universal fader b{_bank} row{row} "
                             f"→ {'cleared' if _clear else cmd.get('cc')} (offline)")
        except Exception as _e:
            pass

    elif action == "MIDI_SCAN":
        # Scan ALSA MIDI outputs; open anything not already reachable via port_outs.
        # Newly-opened ports land in _dynamic_ports and get written to the state
        # JSON so /api/midi_ports can surface them without another engine cmd.
        try:
            import re as _re
            _skip_sub = (
                "midi through", "pisound-ctl", "rtpmidid:announc",
                "rtmidi",       "orc1-synth",
            )
            # Prefixes already covered by cfg.PORTS values
            _cfg_vals = [v.lower() for v in cfg.PORTS.values()]
            for _name in mido.get_output_names():
                _nl = _name.lower()
                if any(_s in _nl for _s in _skip_sub):
                    continue
                if any(_nl.startswith(_v) for _v in _cfg_vals):
                    continue
                # Derive a short key from the ALSA client name (before ':')
                _client = _name.split(":")[0]
                _key = _re.sub(r'[^a-z0-9]+', '_',
                               _client.lower()).strip('_')[:16] or "usb"
                if _key in cfg.PORTS:          # don't shadow a configured key
                    _key = "dyn_" + _key
                if _key in _dynamic_ports:     # already opened
                    continue
                try:
                    port_outs[_key] = mido.open_output(_name)
                    _dynamic_ports[_key] = _name
                    print_status(f"MIDI_SCAN: opened {_name!r} → port '{_key}'")
                except Exception:
                    pass
        except Exception as _e:
            print_status(f"MIDI_SCAN error: {_e}")

    elif action == "CHORD_ROWS_SET":
        # Update chord row routing from the web CONTROL page.
        # Payload: {rows: [{type, port, channel, octave}, ...], bank?: i}
        # rows = the first N rows of that bank (default: the ACTIVE bank). A
        # direct row's channel is 1-16 or a module key on the synth port.
        # 2026-09-24: this used to REPLACE fire._chord_rows with a 4-row list,
        # which the Fire never saved (it persists _chord_banks), so web edits
        # were lost; and with the Fire unplugged it did nothing at all.
        try:
            from orc1_fire import (_valid_chord_row, _CHORD_ROWS_FILE as _crf,
                                   _CHORD_ROWS_VERSION, _migrate_chord_banks)
            rows = cmd.get("rows", [])
            if not (isinstance(rows, list) and 1 <= len(rows) <= 16
                    and all(_valid_chord_row(r) for r in rows)):
                raise ValueError("bad rows")
            rows = [dict(r) for r in rows]
            if fire:
                bank = int(cmd.get("bank", fire._chord_bank))
                dest = fire._chord_banks[bank]       # the active bank IS _chord_rows
                for i, r in enumerate(rows[:len(dest)]):
                    dest[i] = r
                fire._save_chord_rows()
            else:
                p = os.path.expanduser(_crf)
                try:
                    with open(p) as f:
                        data = json.load(f)
                except Exception:
                    data = None
                if not (isinstance(data, dict) and isinstance(data.get("banks"), list)):
                    data = {"v": _CHORD_ROWS_VERSION, "active": 0, "banks": [[]]}
                # An unmarked file gets its one-time literal-channel ->
                # module migration HERE, before the marker is written (else
                # the Fire would skip it on its next load). Then the web's
                # rows go in as given: those are the player's choice.
                if int(data.get("v", 0) or 0) < _CHORD_ROWS_VERSION:
                    _migrate_chord_banks(data["banks"])
                    data["v"] = _CHORD_ROWS_VERSION
                bank = int(cmd.get("bank", data.get("active", 0) or 0))
                while len(data["banks"]) <= bank:
                    data["banks"].append([])
                dest = data["banks"][bank]
                for i, r in enumerate(rows):
                    if i < len(dest):
                        dest[i] = r
                    else:
                        dest.append(r)
                tmp = p + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, p)
            print_status(f"chord_rows bank {bank} updated from web")
        except Exception as _e:
            print(f"  ! CHORD_ROWS_SET: {_e}", flush=True)

    elif action.startswith("SEQ_"):
        _handle_seq_cmd(action)
    elif action == "RESTART_ORC":
        import subprocess as _sp, threading as _th
        def _restart():
            import time as _t
            _t.sleep(0.4)
            # (orc1-synth is retired, 2026-09-24; the module hosts are
            #  PartOf=orc1, so restarting orc1 restarts them.)
            _sp.Popen(["sudo", "-n", "systemctl", "restart",
                       "orc1.service", "orc1-system.service"])
            _t.sleep(3)
            _sp.Popen(["sudo", "-n", "systemctl", "restart", "orc1-web.service"])
        _th.Thread(target=_restart, daemon=True).start()
    if action:
        print_status(f"[web] {action}")


def _cmd_poll_loop():
    """
    Legacy fallback: polls ~/orc1_cmd.json every 100ms for web UI commands.
    Reads, deletes, executes. Kept for backward compatibility with web
    layers that can't reach the command socket -- the primary transport is
    now the Unix domain socket (see _cmd_socket_loop).
    """
    while True:
        try:
            if os.path.exists(_CMD_PATH):
                with open(_CMD_PATH) as f:
                    cmd = json.load(f)
                os.remove(_CMD_PATH)
                _execute_cmd(cmd)
        except Exception:
            pass
        time.sleep(0.1)


_CMD_SOCK_PATH = os.path.expanduser("~/.orc1_cmd.sock")


def _cmd_socket_loop():
    """
    Primary web-command transport: Unix domain socket at ~/.orc1_cmd.sock.
    orc1_web.py connects and sends one JSON object per line; each line is
    executed immediately and acked with {"ok":true}. Unlike the old file
    handshake, back-to-back commands can't overwrite each other.
    Fails soft: if the socket can't be created, the file poll loop still
    provides the legacy path.
    """
    import socket as _socket
    try:
        try:
            os.unlink(_CMD_SOCK_PATH)
        except FileNotFoundError:
            pass
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(_CMD_SOCK_PATH)
        os.chmod(_CMD_SOCK_PATH, 0o600)
        srv.listen(4)
    except Exception as e:
        print(f"  Web cmd socket unavailable ({e}) -- file polling only")
        return

    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            time.sleep(0.1)
            continue
        try:
            conn.settimeout(2.0)
            buf = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cmd = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    try:
                        _execute_cmd(cmd)
                        conn.sendall(b'{"ok":true}\n')
                    except Exception:
                        try: conn.sendall(b'{"ok":false}\n')
                        except Exception: pass
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass


def print_status(label=""):
    """Log engine state and refresh ~/orc1_state.json.

    The console formatting is wrapped so a bug in it can never take down the
    caller.  This function is invoked from a dozen places including the MIDI
    input threads, and write_state_json() is its LAST statement -- so an
    exception raised while formatting used to (a) leave the state file frozen,
    (b) make every /cmd reply "ok":false for a command that had in fact run, and
    (c) kill din_input_loop(), which has no reopen path.  A cosmetic failure must
    not cost the instrument its MIDI input.
    """
    try:
        _print_status_body(label)
    except Exception as e:
        print(f"  [status] formatting failed: {e}")
    write_state_json()


def _print_status_body(label=""):
    cname = chord_name_str()
    # MUST be _ext_sort_key, not CHORD_EXTS[e]: a scale-degree ext name is not in
    # CHORD_EXTS, so the old lookup raised a KeyError here -- and because
    # write_state_json() is the LAST thing this function does, that took the state
    # file, every /cmd reply, and (on an unguarded DIN/USB controller) the whole
    # MIDI input thread down with it.
    exts  = sorted(state["chord_exts"], key=_ext_sort_key)
    pt    = "  [ARP PASSTHROUGH]" if state["arp_passthrough"] else ""
    print(f"  -- {label}{pt}")
    print(f"     Preset:{state['preset']}  "
          f"{'ROOT' if state['root_mode'] else 'CHORD'} mode  "
          f"Key:{ROOT_NAMES[state['root']]}  "
          f"On:{ROOT_NAMES[chord_root_now() % 12]}  "
          f"Chord:{cname}  Scale:{state['scale']}  "
          # O4: the whole point is that you can SEE where the quality came
          # from. DIAT* = a momentary base-pad override is in force.
          f"Quality:{state.get('quality_mode', 'DIAT')}"
          f"{'*' if state.get('base_override') else ''}  "
          f"Snap:{scale_mode_now().upper()}")
    print(f"     Oct:{state['octave']+state['oct_offset']}  "
          f"Voicing:{state['voicing']}  "
          f"VL:{'ON' if state['vl_enabled'] else 'OFF'}  "
          f"KeyLock:{'ON' if state['key_lock'] else 'OFF'}  "
          f"Hold:{'ON' if state['hold'] else 'OFF'}  "
          f"Melody:{state['melody_mode']}  "
          f"Mirror:{'ON' if state['mirror_input_ch'] else 'OFF'}  "
          f"Func:{'HELD' if state['func_held'] else '--'}  "
          f"Transpose:{state['func_transpose']:+d}st  "
          f"LeadPos:{state['lead_voice_pos']}")
    if exts:
        print(f"     Extensions: {', '.join(exts)}")
    if arp:
        print(f"     {arp.get_status()}")


# -- Arp CC handler ------------------------------------------------------------


def handle_arp_cc(action, value):
    if not arp:
        return
    on = value > 63


    if action == "ARP_TOGGLE":
        arp.set_enabled(on)
        if not on:
            send_chord_off(); all_notes_off()
        print(f"  ARP {'ON' if on else 'OFF'}")
        return
    if action == "ARP_HOLD":
        # >63 = hold on (latch: arp keeps running after note release)
        # <64 = hold off (gate: arp stops when trigger note is released)
        state["arp_hold"] = on
        if not on:
            arp.clear_chord()   # stop immediately if turning hold off mid-run
        print(f"  ARP HOLD {'ON' if on else 'OFF'}")
        return
    if action == "ARP_RATE":
        idx = int((value / 127) * (len(SUBDIVISION_LIST)-1))
        arp.set_subdivision_by_index(idx)
        return
    if action == "ARP_CLOCK_MODE":
        _din_auto_synced = False
        arp.set_clock_mode(
            CLOCK_EXTERNAL if value < 43 else
            CLOCK_INTERNAL if value < 85 else CLOCK_DRIFT)
        return
    if action == "ARP_BPM":
        arp.set_internal_bpm(40.0 + (value/127.0)*240.0)
        return
    if action == "ARP_DRIFT_BPM":
        arp.set_drift_bpm(40.0 + (value/127.0)*240.0)
        return
    if action == "ARP_GATE":
        arp.set_gate_trim(0.1 + (value/127.0)*1.9)
        return
    if action == "ARP_HUMANIZE":
        arp.set_humanize(value/127.0)
        return
    if action == "ARP_SWING":
        arp.set_swing((value/127.0)*0.5)
        return
    if action == "ARP_TAP" and on:
        tap_tempo()
        return
    if action == "ARP_PATTERN_PREV" and on:
        order = pat.PATTERN_ORDER
        idx   = order.index(arp.pattern_name) if arp.pattern_name in order else 0
        name  = order[(idx-1) % len(order)]
        arp.set_pattern(name, pat.PATTERNS)
        print(f"  ARP pattern -> {name}")
        return
    if action == "ARP_PATTERN_NEXT" and on:
        order = pat.PATTERN_ORDER
        idx   = order.index(arp.pattern_name) if arp.pattern_name in order else 0
        name  = order[(idx+1) % len(order)]
        arp.set_pattern(name, pat.PATTERNS)
        print(f"  ARP pattern -> {name}")
        return


# -- Control channel handlers --------------------------------------------------


def handle_func_cc(cc, value):
    """
    Handle a CC that arrived while the function key is held.
    Looks up the action in cfg.FUNC_CC_MAP.

    FUNC_LEAD_VOICE      -- leading voice rotary (also available without func)
    FUNC_SCALE_SWEEP     -- sweep scales in real time; lock on func release
    FUNC_PATTERN_SWEEP   -- sweep arp patterns in real time; lock on func release
    FUNC_TRANSPOSE       -- transpose ±semitones while held; revert on release
    """
    action = cfg.FUNC_CC_MAP.get(cc)
    if action is None:
        # Fall through to normal CC handling if not mapped in func layer
        handle_ctrl_cc(cc, value)
        return

    step = decode_relative_cc(value)

    if action == "FUNC_LEAD_VOICE":
        # Handled identically to the non-func path -- see below
        _do_lead_voice(step)
        return

    if action == "FUNC_SCALE_SWEEP":
        # Step through SCALE_NAMES; retrigger chord immediately so you
        # hear the change as you turn. Scale locks in on func key release.
        idx = SCALE_NAMES.index(state["scale"]) if state["scale"] in SCALE_NAMES else 0
        idx = (idx + step) % len(SCALE_NAMES)
        state["scale"] = SCALE_NAMES[idx]
        mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
        chord_held = (state["trigger_note"] is not None) or (
            arp and arp.enabled and arp.current_chord)
        if chord_held:
            play_chord(mirror_ch=mirror_ch)
        if cfg.DEBUG:
            print(f"  [FUNC] scale -> {state['scale']}")
        return

    if action == "FUNC_PATTERN_SWEEP" and arp:
        # Step through PATTERN_ORDER; retrigger arp immediately.
        # Pattern locks in on func key release.
        order = pat.PATTERN_ORDER
        idx = order.index(arp.pattern_name) if arp.pattern_name in order else 0
        idx = (idx + step) % len(order)
        arp.set_pattern(order[idx], pat.PATTERNS)
        if cfg.DEBUG:
            print(f"  [FUNC] pattern -> {order[idx]}")
        return

    if action == "FUNC_TRANSPOSE":
        # Shift output pitch by ±semitones while held; reverts on release.
        # Range clamped to ±24 (two octaves).
        state["func_transpose"] = max(-24, min(24,
            state["func_transpose"] + step))
        mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
        chord_held = (state["trigger_note"] is not None) or (
            arp and arp.enabled and arp.current_chord)
        if chord_held:
            play_chord(mirror_ch=mirror_ch)
        if cfg.DEBUG:
            print(f"  [FUNC] transpose -> {state['func_transpose']:+d}st")
        return


def _do_lead_voice(step):
    """
    Advance the leading voice position by `step` and retrigger the chord.
    Called from both the func layer and the always-on LEAD_VOICE CC action.
    """
    if not step:
        return
    # Build a temporary chord to measure the position range. Must use the same
    # quality play_chord() will use, or the measured position range won't match
    # the chord that actually sounds.
    base = state["chord_base"]
    raw = build_chord_notes(
        chord_root_now(), base, state["chord_exts"], state["octave"],
        state["inversion"], state["voicing"],
        state["spread"], state["oct_offset"])
    positions = get_voice_positions(raw)
    n_pos = len(positions)
    state["lead_voice_pos"] = max(0, min(n_pos - 1,
        state["lead_voice_pos"] + step))
    # Retrigger if a chord is held
    chord_held = (state["trigger_note"] is not None) or (
        arp and arp.enabled and arp.current_chord)
    if chord_held:
        mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
        play_chord(mirror_ch=mirror_ch)
    if cfg.DEBUG:
        print(f"  [LEAD] pos={state['lead_voice_pos']}/{n_pos-1}")


def handle_ctrl_note(note, vel):
    # Function key -- sets func_held, snapshots revertible state
    if note == cfg.FUNC_KEY_NOTE:
        state["func_held"]       = True
        state["func_transpose"]  = 0   # transpose always starts fresh
        state["func_pre_scale"]  = state["scale"]
        state["func_pre_pattern"]= arp.pattern_name if arp else None
        if cfg.DEBUG:
            print("  [FUNC] key held")
        return

    if note in cfg.CTRL_BASE_NOTES:
        # root_mode fires the chord itself; otherwise set_chord_base retriggers
        # so the quality changes under a held finger.
        set_chord_base(cfg.CTRL_BASE_NOTES[note],
                       retrigger=not state["root_mode"])
        if state["root_mode"]:
            play_chord(vel, mirror_ch=cfg.NOTE_INPUT_CH if mirror_active() else None)
        print(f"  Base -> {state['chord_base']}")
        print_status(); return


    # Both extension vocabularies toggle identically -- only the NAME differs, and
    # get_intervals() resolves scale-degree vs chromatic per-name.
    _ext_note_map = dict(cfg.CTRL_EXT_NOTES)
    _ext_note_map.update(getattr(cfg, "CTRL_EXT_DEGREE_NOTES", {}))
    if note in _ext_note_map:
        ext = _ext_note_map[note]
        if ext in state["chord_exts"]:
            state["chord_exts"].discard(ext)
            print(f"  Ext {ext} OFF -> {chord_name_str()}")
        else:
            state["chord_exts"].add(ext)
            print(f"  Ext {ext} ON  -> {chord_name_str()}")
        if state["root_mode"]:
            play_chord(vel, mirror_ch=cfg.NOTE_INPUT_CH if mirror_active() else None)
        else:
            # Retrigger a held chord so the extension is AUDIBLE on press, not
            # only on the next key press.  The note-off path has always done this
            # (see handle_ctrl_note_off), so without it the pads were asymmetric:
            # pressing did nothing, releasing re-attacked the plain triad.  That
            # asymmetry predates O3 but matters far more now, because these pads
            # are the only route to the scale's own 7th/9th/11th.
            _retrigger_chord()
        print_status(); return


    if note in cfg.CTRL_TRIGGER_NOTES:
        if state["root_mode"]:
            play_chord(vel, mirror_ch=cfg.NOTE_INPUT_CH if mirror_active() else None)
        return


    if note in cfg.CTRL_MOD_NOTES:
        fn = cfg.CTRL_MOD_NOTES[note]
        n_notes = len(get_intervals(state["chord_base"], state["chord_exts"]))
        if fn == "INV_UP":
            state["inversion"] = (state["inversion"]+1) % max(n_notes,1)
        elif fn == "INV_DOWN":
            state["inversion"] = (state["inversion"]-1) % max(n_notes,1)
        elif fn == "OCT_UP":
            state["octave"] = min(8, state["octave"]+1)
        elif fn == "OCT_DOWN":
            state["octave"] = max(0, state["octave"]-1)
        elif fn == "EXT_CLEAR":
            state["chord_exts"].clear()
            print(f"  Extensions cleared -> {chord_name_str()}")
        elif fn == "PREV_CHORD":
            if len(state["chord_history"]) > 1:
                state["chord_history"].pop()
                r, base, exts, _ = state["chord_history"][-1]
                state["chord_root"]    = r      # history stores the CHORD root
                state["chord_base"]    = base
                state["chord_exts"]    = set(exts)
                # Recalling a specific past chord is an explicit choice, so it
                # overrides diatonic too -- otherwise the recalled quality would
                # be immediately overwritten by the scale degree.  In latched
                # FIXED mode the override is already implied (see
                # set_chord_base), and setting it would show a phantom "*".
                if state.get("quality_mode") != "FIXED":
                    state["base_override"] = True
        elif fn == "PANIC":
            all_notes_off()
            if arp: arp._release_all_held()
            state["held_notes"] = []
            state["prev_notes"] = []
        print_status(fn)


def handle_ctrl_note_off(note):
    """Note-off on CH_CTRL. Extension pads are momentary; func key reverts/commits."""
    # Function key release
    if note == cfg.FUNC_KEY_NOTE:
        state["func_held"] = False
        # Transpose reverts on release
        if state["func_transpose"] != 0:
            state["func_transpose"] = 0
            chord_held = (state["trigger_note"] is not None) or (
                arp and arp.enabled and arp.current_chord)
            if chord_held:
                mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
                play_chord(mirror_ch=mirror_ch)
        # Scale and pattern LOCK on release -- no revert (intentional)
        if cfg.DEBUG:
            print(f"  [FUNC] key released  scale={state['scale']}  "
                  f"pattern={arp.pattern_name if arp else '--'}")
        print_status("FUNC release")
        return

    # Momentary release for BOTH extension vocabularies (see handle_ctrl_note).
    _ext_note_map = dict(cfg.CTRL_EXT_NOTES)
    _ext_note_map.update(getattr(cfg, "CTRL_EXT_DEGREE_NOTES", {}))
    if note not in _ext_note_map:
        return
    ext = _ext_note_map[note]
    if ext not in state["chord_exts"]:
        return
    state["chord_exts"].discard(ext)
    print(f"  Ext {ext} released -> {chord_name_str()}")
    chord_held = (state["trigger_note"] is not None) or (
        arp and arp.enabled and arp.current_chord)
    if chord_held:
        play_chord(mirror_ch=cfg.NOTE_INPUT_CH if mirror_active() else None)
    print_status()


def handle_ctrl_cc(cc, value):
    # If function key is held, route to func layer first -- but only for
    # CCs that actually have a FUNC mapping. handle_func_cc falls back to
    # handle_ctrl_cc for unmapped CCs, so diverting unconditionally caused
    # infinite mutual recursion (RecursionError) on any unmapped CC while
    # the function key was held.
    if state["func_held"] and cfg.FUNC_CC_MAP.get(cc) is not None:
        handle_func_cc(cc, value)
        return

    # Always-on: LEAD_VOICE CC works regardless of func key
    action_peek = cfg.CTRL_CC_MAP.get(cc)
    if action_peek == "LEAD_VOICE":
        _do_lead_voice(decode_relative_cc(value))
        return

    arp_action = cfg.ARP_CC_MAP.get(cc)
    if arp_action:
        handle_arp_cc(arp_action, value)
        return


    action = cfg.CTRL_CC_MAP.get(cc)
    if action is None:
        return


    on = value > 63


    if isinstance(action, tuple) and action[0] == "SCALE":
        # Legacy one-CC-per-scale form. Nothing binds it by default since
        # 2026-07-28 (see SCALE_SELECT / SCALE_PC_OFFSET), kept so an existing
        # external mapping doesn't break.
        if on and 0 <= action[1] < len(SCALE_NAMES):
            set_scale(SCALE_NAMES[action[1]])
        return

    if action == "SCALE_SELECT":
        # One knob across the whole list: value 0..127 -> index 0..len-1.
        order = SCALE_ORDER or SCALE_NAMES
        idx   = min(len(order) - 1, value * len(order) // 128)
        set_scale(order[idx])
        return


    if action == "VL_STRENGTH":
        state["vl_strength"] = value/127.0
        state["vl_enabled"]  = value > 0
        return
    if action == "SPREAD":
        state["spread"]  = value
        state["voicing"] = "CLOSE" if value < 20 else ("OPEN" if value < 80 else "SPREAD")
        return
    if action == "OCT_OFFSET":
        state["oct_offset"] = int((value/127.0)*4)-2
        return
    if action == "VEL_OVERRIDE":
        state["vel_override"] = value
        return
    if action == "INV_SWEEP":
        # Map 0-127 across available inversions continuously
        n_notes = len(get_intervals(state["chord_base"], state["chord_exts"]))
        if n_notes > 1:
            state["inversion"] = int((value/127.0) * (n_notes-1))
        return
    if action == "KEY_LOCK":
        state["key_lock"] = on; print_status("KEY_LOCK"); return
    if action == "SCALE_SNAP":
        # MUST go through set_scale_mode(), not a bare mirror write.
        # scale_mode_now() consults state["scale_mode"] FIRST and only falls
        # back to this boolean when scale_mode holds something unrecognised --
        # and "off" is a perfectly valid value.  So writing scale_snap alone
        # left the mode untouched: the note-snapping behaviour ignored this CC
        # entirely while the Fire's poly/chip pad shading (which reads the
        # mirror) flipped.  The result was a control that visibly toggled and
        # audibly did nothing, or worse, pads shaded as locked while notes were
        # not being snapped.  CC 11 on CH_CTRL is live (orc1_config.py), so this
        # is reachable from any UCS pad or external controller.
        set_scale_mode("nearest" if on else "off")
        print_status(f"SCALE MODE {scale_mode_now().upper()}"); return
    if action == "HOLD":
        state["hold"] = on
        if not on: send_chord_off()
        print_status("HOLD"); return
    if action == "VOICING_OPEN"  and on:
        state["voicing"] = "OPEN";  print_status(); return
    if action == "VOICING_CLOSE" and on:
        state["voicing"] = "CLOSE"; print_status(); return
    if action == "VOICING_DROP2" and on:
        state["voicing"] = "DROP2"; print_status(); return
    if action == "VL_ON"  and on:
        state["vl_enabled"] = True;  state["vl_strength"] = 1.0
        print_status("VL ON"); return
    if action == "VL_OFF" and on:
        state["vl_enabled"] = False; state["vl_strength"] = 0.0
        print_status("VL OFF"); return
    if action == "MONO_MULTI":
        if on:
            state["preset"] = state["last_multi"] if state["last_multi"] \
                != cfg.DEFAULT_PRESET else "BASS_SPLIT"
        else:
            state["last_multi"] = state["preset"]
            state["preset"]     = cfg.DEFAULT_PRESET
        send_chord_off(); all_notes_off()
        print_status(f"-> {state['preset']}"); return
    if action == "ROOT_MODE":
        state["root_mode"] = on
        print_status(f"{'ROOT' if on else 'CHORD'} mode"); return
    if action == "ARP_PASSTHROUGH":
        state["arp_passthrough"] = on
        state["live_notes"].clear()
        send_chord_off(); all_notes_off()
        print_status(f"ARP PASSTHROUGH {'ON' if on else 'OFF'}"); return
    if action == "MIRROR_INPUT_CH":
        state["mirror_input_ch"] = on
        send_chord_off(); all_notes_off()
        print_status(f"MIRROR {'ON' if on else 'OFF'}"); return
    if action == "MELODY_MODE" and on:
        # Cycle OFF -> SCALE -> CHORD -> OFF
        modes = ["OFF", "SCALE", "CHORD"]
        idx = modes.index(state["melody_mode"])
        state["melody_mode"] = modes[(idx+1) % len(modes)]
        # Clear any stuck melody notes when switching
        state["melody_map"].clear()
        print_status(f"MELODY -> {state['melody_mode']}"); return


def handle_ctrl_pc(program):
    # Scale select: one PC per scale, direct access (2026-07-28).  Checked first
    # because it lives in the low PC block, well clear of presets and patterns.
    order = SCALE_ORDER or SCALE_NAMES
    s_off = getattr(cfg, "SCALE_PC_OFFSET", 0)
    if s_off <= program < s_off + len(order):
        set_scale(order[program - s_off])
        return

    offset = cfg.ARP_PC_OFFSET
    if offset <= program <= offset + len(pat.PATTERN_ORDER) - 1:
        idx  = program - offset
        name = pat.PATTERN_ORDER[idx]
        if arp:
            arp.set_pattern(name, pat.PATTERNS)
            print(f"  ARP pattern -> {name}  ({idx+1}/{len(pat.PATTERN_ORDER)})")
        return


    preset_name = cfg.PC_PRESET_MAP.get(program)
    if preset_name and preset_name in cfg.PRESETS:
        state["preset"] = preset_name
        if preset_name != cfg.DEFAULT_PRESET:
            state["last_multi"] = preset_name
        send_chord_off(); all_notes_off()
        print_status(f"PRESET -> {preset_name}")


# -- Note input handlers -------------------------------------------------------


def handle_note_input(note, vel):
    """CH NOTE_INPUT_CH -- triggers chord engine."""
    state["velocity"] = vel


    if state["arp_passthrough"]:
        state["live_notes"][note] = vel
        mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
        if arp and arp.enabled:
            arp.set_chord(sorted(state["live_notes"].keys()), vel,
                          mirror_ch=mirror_ch)
        else:
            pk, ch = resolve("CHORD_ALL", mirror_ch)
            if pk and ch:
                note_on_out(note, vel, pk, ch)
                state["held_notes"].append((note, pk, ch))
        return


    with _state_lock:
        state["trigger_note"] = note
        state["trigger_ch"]   = cfg.NOTE_INPUT_CH
        # A new key press ends any base-pad override, so quality goes back to
        # being decided by the scale degree (see get_intervals()).
        state["base_override"] = False
        # The pressed note sets the CHORD root and the octave.  It can no longer
        # touch state["root"] (the KEY tonic) at all -- that is the 2026-07-29
        # split, and it is why key_lock no longer needs to guard this write.
        # An off-key press is handled at build time by chord_root_now().
        state["chord_root"] = note % 12
        state["octave"]     = note // 12


    mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
    if state["root_mode"]:
        if cfg.DEBUG:
            print(f"  [ROOT pre-select] {ROOT_NAMES[chord_root_now() % 12]}"
                  f"  (key {ROOT_NAMES[state['root']]})")
    else:
        play_chord(vel, mirror_ch=mirror_ch)


def handle_note_off_input(note):
    """CH NOTE_INPUT_CH note-off."""
    if state["arp_passthrough"]:
        state["live_notes"].pop(note, None)
        if not (arp and arp.enabled):
            mirror_ch = cfg.NOTE_INPUT_CH if mirror_active() else None
            pk, ch = resolve("CHORD_ALL", mirror_ch)
            if pk and ch:
                note_off_out(note, pk, ch)
            state["held_notes"] = [(n,p,c) for (n,p,c) in state["held_notes"]
                                   if n != note]
        return


    if state["hold"]:
        return


    with _state_lock:
        match = (note == state["trigger_note"] and
                 state["trigger_ch"] == cfg.NOTE_INPUT_CH)
        if match:
            state["trigger_note"] = None
            state["trigger_ch"]   = None


    if match:
        if not (arp and arp.enabled):
            # Arp not running -- release the chord notes as usual
            send_chord_off()
        elif not state.get("arp_hold", False):
            # Arp is running but HOLD is off (gate mode):
            # clear the arp chord so it stops at the end of the current step.
            # The arp stays *enabled* -- a new note will restart it immediately.
            arp.clear_chord()


# -- Per-port input handler ----------------------------------------------------


# -- Synth channel set (built once from config) -------------------------------
_SYNTH_CHS = {}   # {1-based ch -> True} -- populated in main()
router     = None  # RoutingEngine -- initialised in main() after port_outs is ready


def _route_message(msg, source_label="usb", source_name=""):
    """
    Shared routing logic for both USB and DIN inputs.

    source_label = input KIND ("usb"/"din"); source_name = the real input port
    name, so a RoutingEngine rule can single out one device (in_port "self" or a
    device name) instead of every USB device sharing the "usb" label.

    Channel dispatch (in priority order):
      CH 14              -> eavesdrop: ORC-1 may react but ALWAYS forwards
      CH_CTRL (13)       -> control engine, consumed, never forwarded
      NOTE_INPUT_CH (12) -> chord engine
      MELODY_CH (15)     -> melody snap engine
      _SYNTH_CHS         -> internal synth bus (drums/poly/chip/NES voices)
      everything else    -> RoutingEngine (user rules) -> default DIN passthrough

    Non-channel messages (clock, sysex, etc.) pass through and feed arp/fire.
    """
    # Perform-VE input source (option A): claim an external chord stream for the
    # box FIRST and consume it -- matched notes go to 'performve' ch1 only and do
    # NOT fall through to channel dispatch (no module play, no passthrough).  This
    # lets USB/DIN gear drive NaturalPlay without occupying an ORC channel.  Only
    # note_on/note_off are claimed; everything else on that source is untouched.
    _ps = state.get("performve_src")
    if _ps and _performve_src_hit(_ps, source_label, msg.type,
                                  getattr(msg, 'channel', None)):
        send_to("performve", msg.copy(channel=0))
        return

    # Pad maps (/pads): a device with a pad-map profile may claim a pad -- play a
    # note on a named module, send a CC, or run an action -- before any channel
    # dispatch. Pads it doesn't map (and devices without a profile) fall through.
    if padmap.handle(msg, source_name):
        return

    # Anti-feedback (MIDI merge rule): a message that ARRIVED on the DIN input
    # must never be echoed back out the DIN output. pisound is a single physical
    # in/out, so "pass unmatched input through to pisound" is a self-echo. It's
    # harmless for most gear, but any device that re-transmits what it receives
    # (e.g. a BeatStep Pro slaved to our DIN clock, which mirrors transport back
    # out its own DIN out) turns it into a runaway loop:
    #   BSP -> DIN in -> here -> DIN out -> BSP -> DIN in -> ...
    # One sent START was observed multiplying to 1135. `_pass()` drops the echo
    # for DIN-sourced messages; USB-sourced messages still pass through to the
    # DIN synths/gear unchanged. It deliberately does NOT touch: the clock_tick
    # feeding below (needed when the ORC *slaves* to an external DIN clock), the
    # sacred CH14 forward, explicit RoutingEngine rules, or the separate
    # "passthrough" din_routing pure-relay mode.
    def _pass(m):
        if source_label != "din":
            send_to("pisound", m)

    if not hasattr(msg, 'channel'):
        # Device chatter SysEx (Arturia config read-back, our Fire firmware's
        # host-protocol replies) is not musical data -- do NOT leak it to the
        # pisound MIDI output. See _DEVICE_SYSEX_PREFIXES.
        if msg.type == 'sysex' and _sysex_is_device_chatter(msg.data):
            return
        # MIDI clock/transport is CHAIN-THRU'd, but ONLY from the DIN to the DIN,
        # never from a USB input. Rig: MPC (master) -> ORC DIN in, and ORC DIN
        # out -> a downstream BeatStep Pro (and back to the multitimbral MPC).
        # Forwarding the master's clock/start/stop/continue that arrived on the
        # DIN lets the downstream DIN slave lock to it -- this is the ordinary
        # MIDI-thru a chain needs, and dropping it is what stopped the BSP
        # syncing. We must NOT forward transport that arrived on a USB input: the
        # BSP is ALSO on the Pi's USB and re-emits transport there, so a USB->DIN
        # relay bounces straight back to its DIN in and runs away (one START
        # multiplied to >1200). The ORC's own sequencer clock-out (orc1_seq.py
        # _send_rt -- a direct send_to, unaffected here) remains the master when
        # nothing external is feeding us. clock still feeds arp/fire below so the
        # ORC can SLAVE regardless of whether we re-broadcast.
        # ⚠ A device patched to the DIN in AND out at once will still loop -- do
        #   not wire a slave's DIN OUT back into the ORC's DIN IN.
        if msg.type in ('clock', 'start', 'stop', 'continue', 'songpos', 'reset'):
            if source_label == 'din':
                send_to('pisound', msg)      # chain-thru the master clock
        else:
            _pass(msg)
        if msg.type == 'clock' and arp:
            # Single authoritative clock source. The RC-505 mk1 (and any device
            # cabled to BOTH the Pi's USB and DIN at once) mirrors its master
            # clock out both ports, so without this the arp would be ticked
            # TWICE per pulse and run at 2x the true tempo (measured 2026-09-08:
            # RC master 140 BPM -> arp read 279). DIN still auto-establishes
            # external sync; the OWNER is the first source seen while external,
            # and every other source's clock is dropped until the owner goes
            # quiet (the watchdog releases it). clock_tick() is a no-op unless
            # external, so internal/drift are unaffected.
            global _din_clock_last, _din_auto_synced, _clock_owner
            if source_label == 'din':
                _din_clock_last = time.monotonic()
                if arp.clock_mode == CLOCK_INTERNAL:
                    arp.set_clock_mode(CLOCK_EXTERNAL)
                    _din_auto_synced = True
                    _clock_owner = 'din'
            if arp.clock_mode == CLOCK_EXTERNAL:
                if _clock_owner is None:
                    _clock_owner = source_label
                if source_label == _clock_owner:
                    arp.clock_tick()
        if msg.type == 'clock' and fire:
            fire.clock_tick()
        if msg.type == 'start' and seq:
            seq.play()
        if msg.type == 'stop' and seq:
            seq.stop(reset=True)
        return

    # Pre-dispatch routing (option B): a stage:"pre" RoutingEngine rule can CLAIM
    # any channel BEFORE the default channel dispatch below -- so nothing "owns" a
    # channel (owner's principle: routing is a choice, not ownership). A matched
    # pre-rule (forward or consume) is authoritative and returns here. Rules default
    # to stage:"post", so this is inert until a pre-rule exists. Runs after the
    # non-channel block (channel messages only -- clocks never reach here).
    # CH_CTRL (ch13) is never claimable by a pre-rule -- it always reaches the
    # control engine below (see _pre_stage_channel_ok). Every other channel is fair
    # game. A claimed output that would echo DIN->DIN is dropped (_din_echo_blocked).
    if _pre_stage_channel_ok(msg.channel + 1):
        _pre = (router.process(msg, source_label, stage="pre", source_name=source_name)
                if router else None)
        if _pre is not None:
            for _pk, _m in _pre:
                if not _din_echo_blocked(source_label, _pk):
                    send_to(_pk, _m)
            return

    ch = msg.channel + 1

    # Live sequencer recording -- additive, runs before dispatch so it sees
    # every channel regardless of which branch below ultimately handles the
    # message. SEQ_RECORD_CH in config SYNTH dict sets which channel feeds
    # the recorder. Notes still go wherever they normally go; recording is
    # non-destructive. (Moved here 2026-08-04: it used to sit after the
    # dispatch chain's early returns, so it could only ever see channels
    # that fell through to the passthrough branch -- unreachable for every
    # channel a user would plausibly record from.)
    rec_ch = int(getattr(cfg, "SYNTH", {}).get("SEQ_RECORD_CH", 0))
    if rec_ch and ch == rec_ch and seq:
        if seq.record_mode:
            if msg.type == "note_on" and msg.velocity > 0:
                seq.record_note_on(msg.note, msg.velocity)
            elif msg.type == "note_off" or (
                    msg.type == "note_on" and msg.velocity == 0):
                seq.record_note_off(msg.note)

    # CH14: eavesdrop -- always forwarded, ORC-1 may process CH14_RULES
    if ch == 14:
        send_to("pisound", msg)
        return

    # Control channel -- consume entirely, never forward
    if ch == cfg.CH_CTRL:
        if msg.type == 'note_on' and msg.velocity > 0:
            handle_ctrl_note(msg.note, msg.velocity)
        elif msg.type == 'note_off' or (
                msg.type == 'note_on' and msg.velocity == 0):
            handle_ctrl_note_off(msg.note)
        elif msg.type == 'control_change':
            handle_ctrl_cc(msg.control, msg.value)
        elif msg.type == 'program_change':
            handle_ctrl_pc(msg.program)
        return

    # Chord input channel
    if ch == cfg.NOTE_INPUT_CH:
        if msg.type == 'note_on' and msg.velocity > 0:
            handle_note_input(msg.note, msg.velocity)
        elif msg.type == 'note_off' or (
                msg.type == 'note_on' and msg.velocity == 0):
            handle_note_off_input(msg.note)
        else:
            _pass(msg)
        return

    # Melody channel
    if ch == cfg.MELODY_CH:
        if msg.type == 'note_on' and msg.velocity > 0:
            handle_melody_note_on(msg.note, msg.velocity)
        elif msg.type == 'note_off' or (
                msg.type == 'note_on' and msg.velocity == 0):
            handle_melody_note_off(msg.note)
        else:
            _pass(msg)
        return

    # Internal synth channels (drums/mono/poly/chip + all NES voices) -> synth bus
    if ch in _SYNTH_CHS:
        send_synth_midi(msg)
        return

    # External routing via RoutingEngine (user-defined rules + device profiles).
    # Returns None  -> no rules matched, use default passthrough.
    # Returns []    -> rules matched, message suppressed.
    # Returns [..] -> explicit (port, msg) destinations.
    outputs = (router.process(msg, source_label, source_name=source_name)
               if router else None)
    if outputs is not None:
        for port_key, out_msg in outputs:
            # Same anti-feedback guard as the pre stage and _pass(): a message
            # that arrived on the DIN never goes straight back out the DIN (the
            # BSP-runaway class). Post rules were the one path without it
            # (open item from the 2026-09-23 review, closed 2026-09-24).
            if not _din_echo_blocked(source_label, port_key):
                send_to(port_key, out_msg)
    else:
        _pass(msg)   # default passthrough (suppressed for DIN source -- anti-feedback)


def make_usb_handler(port):
    """Handler for a USB input port.  Removes the port from _opened_input_names
    in a finally block so the hotplug scanner re-opens it if the device
    reconnects (e.g. a Launchpad that power-cycles on a loose USB plug)."""
    port_name = port.name   # capture before port potentially goes invalid
    def handler():
        try:
            for msg in port:
                if _orc1_cme.is_config_frame(msg):
                    # The CME's own config replies (to the CmeLink) are never
                    # music: if its port is opened as an engine input (prefix
                    # removed from PATCHBAY_PORT_PREFIXES) don't _pass them to DIN.
                    continue
                _route_message(msg, "usb", port_name)
        except Exception as e:
            print(f"  [usb] {port_name}: {e}")
        finally:
            # Always remove -- whether the loop ended cleanly or threw.
            # This is what lets the hotplug scanner reopen on reconnect.
            # Unless a replug already reopened the name: then it is the NEW
            # port's, and dropping it would open the device twice.
            if _untrack_input(port_name, port):
                if router:
                    router.device_disconnected(port_name)
                print(f"  [usb] {port_name} -> disconnected, will reopen on reconnect")
    return handler


def din_input_loop(din_in):
    """DIN input handler -- mode depends on state['din_routing'].
    'engine'      -> full channel routing (same as USB)
    'passthrough' -> pure relay to DIN out, untouched
    """
    din_name = getattr(din_in, "name", "") or "din"
    try:
        for msg in din_in:
            if state["din_routing"] == "engine":
                _route_message(msg, "din", din_name)
            else:
                # Pure passthrough
                send_to("pisound", msg)
                if not hasattr(msg, 'channel'):
                    if msg.type == 'clock' and arp:
                        arp.clock_tick()
                    if msg.type == 'clock' and fire:
                        fire.clock_tick()
    except Exception as e:
        print(f"  ! DIN input: {e}")


def make_mixtrack_handler_thread(port):
    def handler():
        for msg in port:
            # DJ mode: Mixxx owns the Mixtrack, so its moves drive nothing here
            if mixtrack and not _mixtrack_released:
                mixtrack.handle_msg(msg)
    return handler


def make_fire_handler_thread(port):
    def handler():
        for msg in port:
            if fire: fire.handle_msg(msg)
    return handler


_pedal_exp = {}        # expression pedal -> latest de-glitched value (nothing reads it yet)
_pedal_exp_t = {}      # expression pedal -> monotonic time of its last value


def _pedal_exp_value(name, value):
    """De-glitched expression-pedal value from orc1_pedal -> its CC target in
    cfg.PEDAL_EXP_MAP ({"A": {"port": "synth"|<cfg.PORTS key>, "ch": 1-16,
    "cc": 0-127}}; unset = stored only). A 'moving' line is logged when a pedal
    wakes from rest, so a ghosting pot shows up in the journal."""
    now = time.monotonic()
    if now - _pedal_exp_t.get(name, -1e9) > 2.0:
        print(f"  [pedal] exp {name} moving ({value})", flush=True)
    _pedal_exp[name] = value
    _pedal_exp_t[name] = now
    tgt = (getattr(cfg, "PEDAL_EXP_MAP", None) or {}).get(name)
    if not tgt:
        return
    ch = int(tgt.get("ch", 0))
    # never out on the ORC's own control channel -- the pedal can't re-enter the
    # inbox it is quarantined from, even through a cable loop
    if not 1 <= ch <= 16 or ch == cfg.CH_CTRL:
        return
    msg = mido.Message("control_change", channel=ch - 1,
                       control=int(tgt.get("cc", 1)) & 127, value=value)
    if tgt.get("port", "synth") == "synth":
        send_synth_midi(msg)
    else:
        send_to(tgt["port"], msg)


# ── pedal chords: scale degree N of the current key/scale, via the SAME entry
# points the Fire's chord keys use (handle_note_input / handle_note_off_input).
_pedal_chord_st = {"pitch": None, "timer": None}


def _pedal_degree_pitch(n):
    """Root note for 1-based scale degree n (wraps up an octave past the scale)."""
    iv = SCALES.get(state["scale"]) or SCALES["MAJ"]
    d = n - 1
    octave = int(getattr(cfg, "PEDAL_CHORD_OCTAVE", 4))
    return max(0, min(127, 12 * octave + int(state["root"])
                      + iv[d % len(iv)] + 12 * (d // len(iv))))


def _pedal_chord_off():
    t = _pedal_chord_st["timer"]
    if t:
        t.cancel()
    _pedal_chord_st["timer"] = None
    p = _pedal_chord_st["pitch"]
    _pedal_chord_st["pitch"] = None
    if p is not None:
        handle_note_off_input(p)


def _pedal_chord_on(p):
    _pedal_chord_off()                   # one pedal chord at a time
    _pedal_chord_st["pitch"] = p
    handle_note_input(p, int(getattr(cfg, "PEDAL_CHORD_VEL", 100)))


def _pedal_chord(n, down, momentary):
    p = _pedal_degree_pitch(n)
    if momentary:                        # note mode: sounds while the foot is down
        if down:
            _pedal_chord_on(p)
        elif _pedal_chord_st["pitch"] == p:
            _pedal_chord_off()
        return
    if not down:
        return
    if getattr(cfg, "PEDAL_CHORD_STYLE", "latch") == "stab":
        _pedal_chord_on(p)
        bpm = (arp._estimated_bpm() or arp.internal_bpm) if arp else 120
        secs = float(getattr(cfg, "PEDAL_CHORD_STAB_BEATS", 1)) * 60.0 / max(bpm, 20)
        t = threading.Timer(secs, lambda: _pedal_chord_off()
                            if _pedal_chord_st["pitch"] == p else None)
        t.daemon = True
        _pedal_chord_st["timer"] = t
        t.start()
    elif _pedal_chord_st["pitch"] == p:     # latch: same switch again = stop
        _pedal_chord_off()
    else:                                # latch: rings until the next chord
        _pedal_chord_on(p)


def _pedal_pr_action(action, ch1, bank, program):
    """A Pr-mode number send. The dialled CHANNEL picked `action` (/pedal page)."""
    if action == "bb_song":
        if not beatbuddy:
            return
        songs_dir = getattr(beatbuddy, "_songs_dir", os.path.expanduser("~/.orc1_bbsongs"))
        try:
            songs = sorted(f for f in os.listdir(songs_dir)
                           if f.lower().endswith((".bbs", ".json")))
        except Exception:
            songs = []
        n = bank * 128 + program
        if n < len(songs):
            _bb_song_select(songs[n])
        else:
            print(f"  [pedal] pr: no BeatBuddy song #{n} ({len(songs)} songs)", flush=True)
    elif action.startswith("fwd:"):
        _, port, ch = action.split(":")
        ch = int(ch)
        if not 1 <= ch <= 16 or ch == cfg.CH_CTRL:
            return
        if bank:
            send_to(port, mido.Message("control_change", channel=ch - 1,
                                       control=0, value=bank & 127))
        send_to(port, mido.Message("program_change", channel=ch - 1,
                                   program=program & 127))
    else:
        print(f"  [pedal] unknown pr action {action!r}", flush=True)


# ── pedal config: /pedal page -> PEDAL_CONFIG_SET -> applied live + saved here.
# orc1_pedal.validate_config is the single gate (the web runs the same one).
def _pedal_conf_file():
    return os.path.expanduser("~/.orc1_pedal.json")


def _pedal_conf_apply(conf):
    """Apply an already-validated config to the live pedal + chord settings."""
    for attr, key in (("PEDAL_EXP_MAP", "exp"), ("PEDAL_CHORD_STYLE", "chord_style"),
                      ("PEDAL_CHORD_STAB_BEATS", "stab_beats"),
                      ("PEDAL_CHORD_OCTAVE", "chord_octave")):
        setattr(cfg, attr, conf[key])
    _pedal_chord_off()                   # a remap must not strand a sounding chord
    if pedal:
        with pedal._lock:
            pedal.map = conf["map"]
            pedal.page = {}


def _pedal_conf_load():
    """Saved config (validated) or None. Missing/corrupt -> None = defaults."""
    try:
        with open(_pedal_conf_file()) as f:
            raw = json.load(f)
    except Exception:
        return None
    conf, errs = _orc1_pedal.validate_config(raw, ports=set(cfg.PORTS))
    for e in errs:
        print(f"  ! pedal config: {e}", flush=True)
    return conf


def _pedal_conf_set(raw):
    conf, errs = _orc1_pedal.validate_config(raw, ports=set(cfg.PORTS))
    errs += _orc1_pedal.refuse_protected(conf, _pedal_conf_load(), _protect_err)
    for e in errs:
        print(f"  ! pedal config: {e}", flush=True)
    _pedal_conf_apply(conf)
    try:
        tmp = _pedal_conf_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(conf, f, indent=1)
        os.replace(tmp, _pedal_conf_file())
    except Exception as e:
        print(f"  ! pedal config save failed: {e}", flush=True)
    print("  [pedal] config applied", flush=True)
    return conf


def _pedal_switch_action(action, down, momentary):
    """Every pedal action that isn't a BeatBuddy gesture or a page change."""
    if action.startswith("chord:"):
        arg = action[6:]
        if arg == "stop":
            if down:
                _pedal_chord_off()
        else:
            _pedal_chord(int(arg), down, momentary)
    elif action.startswith("cmd:"):
        if down:
            _execute_cmd({"action": action[4:]})
    elif action.startswith("rc505:"):
        _pedal_rc505(action[6:], down, momentary)
    else:
        print(f"  [pedal] unknown action {action!r}", flush=True)


def _pedal_rc505(spec, down, momentary):
    """A pedal switch pressing an RC-505 assign (orc1_rc505 action name).
    SP (momentary): the RC sees the foot's real press and release, so the RC's
    own hold gestures work. EF (press-only): a complete tap. "<name>:hold": the
    ORC holds the button HOLD_S by itself (hold STOP = clear, from any mode)."""
    if not rc505:
        return
    name, _, mod = spec.partition(":")
    if name not in rc505.cc_map:
        if down:
            print(f"  [pedal] RC-505 has no {name!r} assign (rc505_config.json)", flush=True)
        return
    if mod == "hold":
        if down:
            rc505.hold_for(name)
    elif momentary:
        (rc505.press_down if down else rc505.press_up)(name)
    elif down:
        rc505.tap(name)


# ── pad maps (orc1_padmap, /pads page): a named device's pads -> a note on a
# NAMED module, a CC, or an action (the pedal's action vocabulary, press-only).
# The brain owns ~/.orc1_padmap.json (PADMAP_SET applies + saves); hits for the
# page's learn mode go to a tmpfs file.
def _padmap_conf_file():
    return os.path.expanduser("~/.orc1_padmap.json")


def _padmap_emit(kind, ch, a, b):
    if kind == "cc":
        m = mido.Message("control_change", channel=ch - 1, control=a, value=b)
    else:
        m = mido.Message(kind, channel=ch - 1, note=a, velocity=b)
    send_synth_midi(m)


def _padmap_action(action):
    """A pad's action. BeatBuddy gestures go to the mind; every other pedal
    action as a press-only switch (chord latch/stab per the /pedal setting)."""
    if action in _orc1_pedal.DISCRETE:
        _bb_pedal_action(action)
    else:
        _pedal_switch_action(action, True, False)


def _padmap_conf_set(raw, save=True):
    conf, errs = _orc1_padmap.validate_config(raw, _MM_KEYS)
    for e in errs:
        print(f"  ! padmap config: {e}", flush=True)
    padmap.set_config(conf)
    if save:
        try:
            tmp = _padmap_conf_file() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(conf, f, indent=1)
            os.replace(tmp, _padmap_conf_file())
        except Exception as e:
            print(f"  ! padmap config save failed: {e}", flush=True)
        print(f"  [padmap] config applied ({len(conf['devices'])} device(s))", flush=True)
    return conf


padmap = _orc1_padmap.PadMap(
    emit=_padmap_emit, module_ch=lambda m: _module_channel(m),
    run_action=_padmap_action, log=lambda s: print(s, flush=True),
    hits_file="/tmp/orc1_padmap_hits.json")
try:
    with open(_padmap_conf_file()) as _f:
        _padmap_conf_set(json.load(_f), save=False)
except FileNotFoundError:
    pass
except Exception as _e:
    print(f"  ! padmap config unreadable: {_e}", flush=True)

# Control surfaces (2026-10-14): a pad-map profile marked "surface" is a device the ORC
# draws on (our Fire firmware). The link ARMS it (colours + channel, but the Fire stays on
# its sequencer) when it appears / reboots / the map changes; only /pads SHOW ON FIRE or
# the Fire's BROWSER > ORC surface shows it. Polled once a second from its own thread.
surface_link = _orc1_surface_link.SurfaceLink(
    mido, padmap.surfaces, lambda: _usb_card_idents(), log=lambda s: print(s, flush=True),
    armed_path="/tmp/orc1_surface_armed.json")


def _surface_loop():
    said = False
    while True:
        time.sleep(1.0)
        try:
            surface_link.poll()
            said = False
        except Exception as e:
            if not said:
                print(f"  ! surface link: {e}", flush=True)
                said = True


def _pedal_poll_loop():
    """Flushes the pedal's expression filters (travel ends) + long-press timing.
    Independent of the clock, which only ticks while something is running."""
    seen = set()
    while True:
        time.sleep(0.01)
        if pedal:
            try:
                pedal.poll()
            except Exception as e:
                if repr(e) not in seen:       # 100x/s -- log each fault once
                    seen.add(repr(e))
                    print(f"  [pedal] poll: {e!r}", flush=True)


def make_pedal_handler_thread(port):
    """Handler for a recognised foot-pedal input (FCB1010 via the CH345 adapter).
    ⚠ FEEDBACK-SAFE: routes ONLY into orc1_pedal (-> the mind); it NEVER calls
    _route_message / _pass, so nothing echoes to the pisound DIN out that the
    CH345's DIN is cross-patched to. A pure sink -- that is what breaks the loop."""
    port_name = port.name

    def handler():
        try:
            for msg in port:
                if pedal:
                    try:
                        pedal.handle(msg)
                    except Exception as e:
                        print(f"  [pedal] {port_name}: {e}", flush=True)
        except Exception as e:
            print(f"  [pedal] {port_name}: {e}", flush=True)
        finally:
            if _untrack_input(port_name, port):      # not if replugged since
                print(f"  [pedal] {port_name} -> disconnected, will reopen on reconnect")
    return handler


def _hotplug_pedal(name):
    """Open a hot-plugged pedal input and start its consuming handler."""
    try:
        port = mido.open_input(name)
    except Exception as e:
        print(f"  ! hot-plug pedal {name}: {e}")
        return
    _track_input(name, port)
    threading.Thread(target=make_pedal_handler_thread(port), daemon=True).start()
    print(f"  ✓ hot-plug [pedal]  {name}  -> BeatBuddy pedal (consumed, feedback-safe)")


def run_all_inputs(din_in, usb_ins, mixtrack_port=None, fire_port=None):
    for pport in _pedal_ports:
        threading.Thread(target=make_pedal_handler_thread(pport), daemon=True).start()
        print("  Pedal handler running (BeatBuddy, feedback-safe)")
    if din_in:
        threading.Thread(
            target=din_input_loop,
            args=(din_in,), daemon=True).start()
        print(f"  DIN input running (mode: {state['din_routing']})")

    if mixtrack_port:
        threading.Thread(
            target=make_mixtrack_handler_thread(mixtrack_port),
            daemon=True).start()
        print("  Mixtrack handler running")

    if fire_port:
        threading.Thread(
            target=make_fire_handler_thread(fire_port),
            daemon=True).start()
        print("  Fire handler running")


    for port in usb_ins:
        threading.Thread(
            target=make_usb_handler(port), daemon=True).start()


    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# -- Port discovery ------------------------------------------------------------


def find_name(names, prefix):
    if not prefix: return None
    pl = prefix.lower()
    for n in names:
        if pl in n.lower(): return n
    return None


# Track opened input port names so hot-plug can skip them
_opened_input_names = set()
_hotplug_lock = threading.Lock()
# The mido port behind each opened name (2026-10-01). A same-name replug used
# to drop only the NAME: the old port (its ALSA seq client + rtmidi thread)
# leaked, and its handler's cleanup could later discard the NEW port's name,
# so the next scan opened the controller a second time (double presses). Now
# the replug closes the old port, and a handler's cleanup forgets the name
# only while it still belongs to ITS port.
_opened_input_ports = {}
# One scan at a time (see _hotplug_scan): MIDI_RESCAN ran a scan on its own
# thread beside the 3 s loop, and two scans could both open a new controller.
_hotplug_scan_lock = threading.Lock()


def _track_input(name, port):
    """Record an opened input port under its name."""
    with _hotplug_lock:
        _opened_input_names.add(name)
        _opened_input_ports[name] = port


def _untrack_input(name, port=None):
    """Forget an opened input. With `port`: only while `name` still belongs to
    THAT port -- False means a replug reopened the name since (leave it)."""
    with _hotplug_lock:
        cur = _opened_input_ports.get(name)
        if port is not None and cur is not None and cur is not port:
            return False
        _opened_input_names.discard(name)
        _opened_input_ports.pop(name, None)
        return True


def _drop_input(name):
    """Forget `name` AND close the port we had open under it (a replugged or
    vanished device: frees its seq client; its blocked handler thread parks
    for good, because rtmidi's receive never wakes on close)."""
    with _hotplug_lock:
        _opened_input_names.discard(name)
        old = _opened_input_ports.pop(name, None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass

# Same-name USB replugs (2026-09-28). A replugged controller often comes back
# with the IDENTICAL MIDI port name (same ALSA client number), so the
# name-based scan below only noticed a replug if a 3 s scan happened to run
# while the device was gone -- a quick unplug/replug left the Fire dark
# forever (measured). The USB address (/proc/asound/cardN/usbbus, "bus/dev")
# changes on EVERY plug-in, so a changed address for a known card = replug.
_usb_idents = {}   # ALSA card name (= MIDI port-name prefix) -> "bus/dev"


def _usb_card_idents():
    """{card name: 'bus/dev'} for every USB sound/MIDI card right now."""
    out = {}
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                head, sep, name = line.partition("]: ")
                num = head.strip().split(" ", 1)[0]
                if not sep or not num.isdigit():
                    continue
                name = name.split(" - ", 1)[-1].strip()
                try:
                    with open(f"/proc/asound/card{num}/usbbus") as u:
                        out[name] = u.read().strip()
                except OSError:
                    pass            # not a USB card (pisound, HDMI, ...)
    except OSError:
        pass
    return out


def _get_midi_device_names():
    """Return list of currently opened MIDI input port names (for web UI).
    Filters out internal engine ports (RtMidi outputs, pisound-ctl)."""
    with _hotplug_lock:
        return sorted(n for n in _opened_input_names
                       if "rtmidi" not in n.lower()
                       and "pisound-ctl" not in n.lower()
                       and "midi through" not in n.lower())


def _hotplug_scan_once():
    """Scan for new MIDI devices once. Called from hot-plug loop and web rescan.
    Also sweeps for disappeared ports as a safety net (the handler thread's
    finally block is the primary cleanup, but race windows exist)."""
    mixtrack_pfx = getattr(cfg, "MIXTRACK_PORT_PREFIX", "mixtrack").lower()
    _SKIP = ("pisound-ctl", "midi through", "rtmidi", "orc1-synth")
    try:
        # -- Same-name replugs: the USB address changed under a known card ----
        idents = _usb_card_idents()
        replugged = {n for n, i in idents.items()
                     if _usb_idents.get(n) not in (None, i)}
        _usb_idents.clear()
        _usb_idents.update(idents)
        if replugged:
            with _hotplug_lock:
                stale = [n for n in _opened_input_names
                         if n.split(":", 1)[0] in replugged]
            for name in stale:
                print(f"  ~ replug detected (new USB address): {name}",
                      flush=True)
                _drop_input(name)           # closes the old port too
                if router:
                    router.device_disconnected(name)

        current = set(mido.get_input_names())
        _cme_scan(current, replugged)    # the CME link sees plugs + same-name replugs itself
        _patchbay_noted.intersection_update(current)   # unplugged -> log again on return

        # -- Disappeared ports -------------------------------------------------
        # Belt-and-suspenders: the handler thread's finally block also does this,
        # but if the thread hasn't exited yet the scanner does it here first.
        with _hotplug_lock:
            gone = _opened_input_names - current
        for name in gone:
            _drop_input(name)
            if kikpad and _orc1_kikpad.is_surface_port(name):
                kikpad.detach(name)
                if kikpad_map is not None:
                    kikpad_map.connected(False)      # /kikpad: the pad is gone, its maps unknown
            if router:
                router.device_disconnected(name)

        # -- New ports ---------------------------------------------------------
        with _hotplug_lock:
            new_names = [n for n in current
                         if n not in _opened_input_names
                         and not any(s in n.lower() for s in _SKIP)]
        for name in new_names:
            nl = name.lower()
            if _is_patchbay(name):
                # A router, not an instrument: never an engine input (loop guard).
                if name not in _patchbay_noted:
                    _patchbay_noted.add(name)
                    print(f"  - hot-plug [patchbay  ]  {name}  (not opened)")
                continue
            if _orc1_kikpad.is_surface_port(name):
                try:
                    port = mido.open_input(name)
                except Exception as e:
                    print(f"  ! hot-plug {name}: {e}")
                    continue
                _track_input(name, port)
                _kikpad_open(name, port)
                print(f"  ✓ hot-plug [kikpad    ]  {name}  -> sampler/skipback surface")
                continue
            if mixtrack_pfx in nl or cfg.is_fire_port(name):
                # Controllers reconnect through their own path: reopen the
                # input, restart the handler thread, re-handshake the LEDs.
                # (Previously these were skipped entirely, so a USB replug
                # left the controller dead until an orc1 restart.)
                _hotplug_controller(name, "mixtrack" if mixtrack_pfx in nl
                                    else "fire")
                continue
            if _orc1_pedal.recognised(nl, _PEDAL_PREFIXES):
                # Pedal reconnect -> its own CONSUMING handler (never the generic
                # usb-engine path, which would _pass to the pisound DIN and loop).
                _hotplug_pedal(name)
                continue
            try:
                port = mido.open_input(name)
                _track_input(name, port)
                threading.Thread(
                    target=make_usb_handler(port),
                    daemon=True).start()
                print(f"  ✓ hot-plug [usb engine]  {name}")
                print_status(f"HOTPLUG: {name}")
                if router:
                    router.device_connected(name)
            except Exception as e:
                print(f"  ! hot-plug {name}: {e}")
    except Exception:
        pass


def _hotplug_controller(name, kind):
    """Bring a Fire / Mixtrack back to life after a USB replug.

    Reopens the input port, starts a fresh handler thread, and reopens the
    handler's LED output + wake sequence (the controller sits in its idle
    boot animation until the host opens it — this is the 'handshake').
    Reuses the existing handler object so runtime state survives the replug.
    """
    global mixtrack, fire
    try:
        port = mido.open_input(name)
    except Exception as e:
        print(f"  ! hot-plug {name}: {e}")
        return
    _track_input(name, port)

    if kind == "mixtrack" and _MIXTRACK_AVAILABLE:
        # Read the DJ marker NOW: a Mixtrack first seen mid DJ set (brain
        # started without it) must not paint over Mixxx's LEDs before the
        # loop's next _mixtrack_sync_release.
        _mixtrack_sync_release()
        if mixtrack is None:
            # mid DJ set: no LED output at all (it opens on reclaim)
            mixtrack = MixtrackHandler(sys.modules[__name__],
                                       led_output=not _mixtrack_released)
        elif _mixtrack_released:
            pass                            # DJ mode: Mixxx repaints it
        else:
            try:
                mixtrack._open_output(
                    getattr(cfg, "MIXTRACK_PORT_PREFIX", "mixtrack"))
                mixtrack.wake()
            except Exception as e:
                print(f"  ! mixtrack LED rewake: {e}")
        threading.Thread(target=make_mixtrack_handler_thread(port),
                         daemon=True).start()
        print(f"  ✓ hot-plug [mixtrack  ]  {name}")
        print_status("HOTPLUG: mixtrack")
    elif kind == "fire" and _FIRE_AVAILABLE:
        if fire is None:
            fire = FireHandler(sys.modules[__name__])
        else:
            # Full rewake: fresh LED + OLED outputs, repaint pads AND screen.
            # (Only reopening the LED port left the OLED dark after a replug --
            # fire_oled cached its dead port by name; 2026-09-28.)
            try:
                fire.rewake()
            except Exception as e:
                print(f"  ! fire rewake: {e}")
        threading.Thread(target=make_fire_handler_thread(port),
                         daemon=True).start()
        print(f"  ✓ hot-plug [fire      ]  {name}")
        print_status("HOTPLUG: fire")
    else:
        try:
            port.close()
        except Exception:
            pass


# DJ mode (2026-09-29): while this marker exists, Mixxx owns the Mixtrack --
# the brain ignores its input and closes its LED output (every LED write in
# orc1_mixtrack is gated on handler._out), so the two never fight over the
# lights. orc1-dj.service makes the marker before Mixxx starts and removes it
# however the unit ends (daw/daw_mode.sh mixtrack release|reclaim), then sends
# MIXTRACK_SYNC; the hot-plug loop re-checks every 3 s too, and a brain that
# starts mid-set reads it, so the marker is the single source of truth.
_DJ_MARK = "/run/orc1-dj-active"
_mixtrack_released = False


def _mixtrack_sync_release():
    """Apply the DJ-mode marker to the Mixtrack. Returns the new state."""
    global _mixtrack_released
    want = os.path.exists(_DJ_MARK)
    if want == _mixtrack_released:
        return want
    _mixtrack_released = want
    if want:
        print("  ~ Mixtrack handed to Mixxx (DJ mode): ORC input + LEDs off",
              flush=True)
    else:
        print("  ~ Mixtrack back from Mixxx: ORC input + LEDs on", flush=True)
    if not mixtrack:
        return want
    try:
        if want:
            with mixtrack._out_lock:
                out, mixtrack._out = mixtrack._out, None
            if out is not None:
                out.close()
        else:
            mixtrack._open_output(
                getattr(cfg, "MIXTRACK_PORT_PREFIX", "mixtrack"))
            mixtrack.wake()
    except Exception as e:
        print(f"  ! Mixtrack DJ-mode handover: {e}", flush=True)
    return want


def _hotplug_scan(blocking=True):
    """THE way to scan: one scan (+ DJ-marker sync) at a time. MIDI_RESCAN
    passes blocking=False -- a scan already running covers it. Returns False
    when it skipped."""
    if not _hotplug_scan_lock.acquire(blocking=blocking):
        return False
    try:
        _hotplug_scan_once()
        _mixtrack_sync_release()
    finally:
        _hotplug_scan_lock.release()
    return True


def _hotplug_loop():
    """Periodically check for newly connected USB MIDI devices."""
    while True:
        time.sleep(3)
        _hotplug_scan()


def _din_clock_watchdog():
    """Revert to internal clock when a DIN clock that was AUTO-detected goes quiet
    (> 3 s since last pulse).  Does NOT revert if the user manually set external
    mode -- _din_auto_synced distinguishes the two cases.  Also releases the
    clock owner (set in _route_message) when the mode leaves external or the
    auto-synced DIN owner goes quiet, so a new source can then claim the clock."""
    global _din_auto_synced, _clock_owner
    while True:
        time.sleep(0.5)
        if not arp:
            continue
        if arp.clock_mode != CLOCK_EXTERNAL:
            _clock_owner = None
            continue
        if _din_auto_synced and time.monotonic() - _din_clock_last > 3.0:
            _din_auto_synced = False
            _clock_owner = None
            arp.set_clock_mode(CLOCK_INTERNAL)


def open_ports():
    global port_outs


    ins  = mido.get_input_names()
    outs = mido.get_output_names()


    print("\nAvailable inputs:",  ins)
    print("Available outputs:", outs)
    print()


    for key, prefix in cfg.PORTS.items():
        name = find_name(outs, prefix)
        if name:
            try:
                port_outs[key] = mido.open_output(name)
                print(f"  ✓ out [{key:10s}]  {name}")
            except Exception as e:
                port_outs[key] = None
                print(f"  ! out [{key:10s}]  {e}")
        else:
            port_outs[key] = None
            print(f"  - out [{key:10s}]  not connected")


    print()


    pisound_pfx  = cfg.PORTS.get("pisound","pisound").lower()
    mixtrack_pfx = getattr(cfg,"MIXTRACK_PORT_PREFIX","mixtrack").lower()
    din_in       = None
    usb_ins       = []
    mixtrack_port = None
    fire_port     = None


    _skip_inputs = ("pisound-ctl", "midi through", "rtmidi", "orc1-synth")
    for name in ins:
        if any(s in name.lower() for s in _skip_inputs):
            continue
        if _is_patchbay(name):
            _patchbay_noted.add(name)
            print(f"  - in  [patchbay     ]  {name}  (not opened)")
            continue
        try:
            port = mido.open_input(name)
            _track_input(name, port)
            if _orc1_kikpad.is_surface_port(name):
                _kikpad_open(name, port)
                print(f"  ✓ in  [kikpad       ]  {name}  -> sampler/skipback surface")
            elif pisound_pfx in name.lower():
                din_in = port
                print(f"  ✓ in  [din input    ]  {name}  -> {state['din_routing']}")
            elif mixtrack_pfx in name.lower() and _MIXTRACK_AVAILABLE:
                mixtrack_port = port
                print(f"  ✓ in  [mixtrack     ]  {name}  -> Mixtrack handler")
            elif cfg.is_fire_port(name) and _FIRE_AVAILABLE:
                fire_port = port
                print(f"  ✓ in  [fire         ]  {name}  -> Fire handler")
            elif _orc1_pedal.recognised(name, _PEDAL_PREFIXES):
                _pedal_ports.append(port)
                print(f"  ✓ in  [pedal        ]  {name}  -> BeatBuddy pedal (consumed)")
            else:
                usb_ins.append(port)
                print(f"  ✓ in  [usb engine   ]  {name}  -> engine")
        except Exception as e:
            print(f"  ! in              {name}  {e}")


    return din_in, usb_ins, mixtrack_port, fire_port


# -- Main ----------------------------------------------------------------------


def main():
    global arp


    print("=" * 62)
    print("  ORC-1  --  Raspberry Pi  v8")
    print("  Config:   ~/orc1_config.py")
    print("  Patterns: ~/orc1_patterns.py")
    print("  Ctrl+C to quit")
    print("=" * 62)


    din_in, usb_ins, mixtrack_port, fire_port = open_ports()


    global mixtrack
    if mixtrack_port and _MIXTRACK_AVAILABLE:
        # brain restarted mid DJ set: read the marker BEFORE the handler
        # exists, so it never opens + paints the LEDs over Mixxx's
        _mixtrack_sync_release()
        mixtrack = MixtrackHandler(sys.modules[__name__],
                                   led_output=not _mixtrack_released)
    else:
        print("  Mixtrack not connected -- running without it")

    if not din_in and not usb_ins and not mixtrack_port and not fire_port:
        print("\n  No MIDI inputs found.")
        sys.exit(1)


    def _arp_note_on(note, vel, port_key, ch):
        """Arp note-on: normal output + synth mirror if coupled.
        Mirror is skipped when the arp is already routing directly to the
        internal synth bus (e.g. NES_CHORD preset routes ROOT->P1 etc.)
        to prevent double-triggering the same voice."""
        note_on_out(note, vel, port_key, ch)
        if port_key == "synth":
            return   # already on synth bus -- don't also mirror
        sent = []
        for module, flag in (("poly", "synth_mirror_poly"),
                             ("chip_p1", "synth_mirror_chip")):
            if not state.get(flag):
                continue
            mch = _module_channel(module)        # None = out of the fold
            if not mch:
                continue                         # never leak an unbound voice to DIN
            send_synth_midi(mido.Message(
                "note_on", channel=mch - 1, note=note, velocity=vel))
            sent.append(mch - 1)
        # Remember exactly what this note mirrored, so the note-off releases
        # the same channels no matter what the flags say later (2026-07-30).
        if sent:
            state.setdefault("_arp_mirror_held", {}).setdefault(note, []).extend(sent)

    def _arp_note_off(note, port_key, ch):
        """Arp note-off: normal output + release whatever the matching note-on
        actually mirrored.

        This used to re-read state["synth_mirror_*"] here and mirror the
        note-off only if the flag was STILL set -- so toggling a mirror off
        (or a routing change flipping port_key) while a note was sounding
        meant the note-off was never sent and the voice hung forever. Nor
        were these notes recorded anywhere, so _synth_mirror_off() could not
        clean up after them either; the chord mirror tracked its notes in
        state["_mirror_held"] but this parallel copy tracked nothing.
        Releasing from the per-note registry removes both failure modes."""
        note_off_out(note, port_key, ch)
        for ch0 in state.get("_arp_mirror_held", {}).pop(note, ()):
            send_synth_midi(mido.Message(
                "note_off", channel=ch0, note=note, velocity=0))

    arp = ArpEngine(
        note_on_fn  = _arp_note_on,
        note_off_fn = _arp_note_off,
        all_off_fn  = all_notes_off,
        debug_fn    = print if cfg.DEBUG else None,
    )
    arp.resolve_fn = resolve
    # Audio-domain downbeat marks for quantized grabs (Bug-1 drift fix):
    # snapshot the ring head on every arp-grid bar "1". PPQN imported from
    # orc1_arp so the mark cadence can never desync from arp.bar_phase().
    arp.bar_cb        = _smp_arp_anchor.mark
    arp.bar_cb_pulses = PPQN * _SMP_BEATS_BAR

    # -- Per-tone voice routing ------------------------------------------------
    # The chord engine assigns each chord tone to a named slot:
    #   tone 0 -> CHORD_ROOT, tone 1 -> CHORD_3RD, tone 2 -> CHORD_5TH …
    # Registering voice_resolve_fn lets the arp engine route each step's
    # tone to the matching preset slot rather than always using CHORD_ALL.
    # This makes NES presets work with the arp automatically:
    #   NES_CHORD: ROOT->Pulse1, 3RD->Pulse2, 5TH->Triangle
    #   arp tone 0 plays on P1, tone 1 on P2, tone 2 on Triangle -- correct!
    # A preset can also set "ARP_DEST": {port/channel} to pin all arp notes
    # to one voice while the chord plays the others (see NES_ARP_POLY).
    _ARP_SLOT_ORDER = ["CHORD_ROOT","CHORD_3RD","CHORD_5TH","CHORD_7TH","CHORD_9TH"]

    def _arp_voice_resolve(tone_idx, mirror_ch=None):
        """Route each arp tone to the matching chord voice slot.

        Priority order:
        1. ARP_DEST in current preset  -- explicit override, pins all arp notes
           to one destination (use this to keep arp on P1 while chord fills P2+Tri)
        2. Per-slot by tone index     -- CHORD_ROOT for tone 0, etc.
        3. CHORD_ALL fallback         -- for presets that use a single destination
        If all return None the tone is silenced (intentionally, as with BASS/LEAD-only
        presets where the arp is not expected to play).
        """
        preset = cfg.PRESETS.get(state["preset"], {})
        # 1. Explicit ARP_DEST override — voice_map takes priority over preset.
        #    A SET ARP_DEST that resolves to nothing (voice-map None, or its
        #    module unbound) is ARP_SILENT: silent, NOT a CHORD_ALL fallback.
        if "ARP_DEST" in state["voice_map"]:
            vm_arp = state["voice_map"]["ARP_DEST"]
            if vm_arp is None:
                return ARP_SILENT
            c = _dest_channel(vm_arp.get("port"), vm_arp.get("channel"))
            return (vm_arp.get("port"), c) if vm_arp.get("port") and c else ARP_SILENT
        arp_dest = preset.get("ARP_DEST")
        if arp_dest:
            c = _dest_channel(arp_dest["port"], arp_dest["channel"])
            return (arp_dest["port"], c) if c else ARP_SILENT
        # 2. Slot by tone index
        if 0 <= tone_idx < len(_ARP_SLOT_ORDER):
            pk, ch = resolve(_ARP_SLOT_ORDER[tone_idx], mirror_ch)
            if pk and ch:
                return pk, ch
        # 3. CHORD_ALL fallback
        return resolve("CHORD_ALL", mirror_ch)

    arp.voice_resolve_fn = _arp_voice_resolve
    arp.set_pattern(cfg.ARP_DEFAULT_PATTERN, pat.PATTERNS)
    arp.set_subdivision(cfg.ARP_DEFAULT_RATE)
    arp.set_clock_mode(cfg.ARP_DEFAULT_CLOCK)
    arp.set_internal_bpm(cfg.ARP_DEFAULT_BPM)
    arp.set_gate_trim(cfg.ARP_DEFAULT_GATE)
    arp.set_humanize(cfg.ARP_DEFAULT_HUMANIZE)
    arp.set_swing(cfg.ARP_DEFAULT_SWING)
    arp.set_enabled(cfg.ARP_ENABLED_ON_BOOT)

    # Sequencer -- engine-owned since 2026-07-01 (ownership refactor, Bugs A/B/C).
    # Constructed here: AFTER arp exists (so the "unified clock" arp-subscribe
    # path in orc1_seq.py actually engages -- it reads eng.arp at construction
    # time; building Sequencer before arp existed was Bug C, a silent no-op
    # every boot) and BEFORE FireHandler (which now just borrows this
    # instance via eng.seq, instead of building its own). Unconditional --
    # no fire_port/_FIRE_AVAILABLE guard -- so the sequencer (and the web UI's
    # SEQ_* commands) work with no Fire connected at all (Bug A fix).
    global seq
    from orc1_seq import Sequencer
    _fcfg = getattr(cfg, "FIRE", {}) or {}
    seq = Sequencer(
        eng    = sys.modules[__name__],
        bpm_fn = lambda: (arp._estimated_bpm() or arp.internal_bpm) if arp else 120.0,
        clock_ports = _fcfg.get("CLOCK_OUT_PORTS", ["pisound"]),
    )
    # Audio-domain downbeat marks for quantized grabs (Bug-1 drift fix):
    # snapshot the ring head on every seq bar "1" (16ths, so beats*4).
    seq.bar_cb       = _smp_seq_anchor.mark
    seq.bar_cb_steps = _SMP_BEATS_BAR * 4
    print(f"  Sequencer ready ({seq.n_tracks} track(s), "
          f"{'unified' if seq._using_arp else 'own'} clock)")

    # FX manager -- instantiate here so eng.fx is live before the Fire starts.
    # The Fire's __init__ may call eng.fx to check chain state on first render.
    global fx
    if _FX_AVAILABLE:
        fx = FXManager(debug_fn=print if cfg.DEBUG else None)
        print("  FX manager ready (eng.fx)")
        # Reap any orphaned fx-rack-* jalv left by a previous brain run, then
        # re-apply the saved rack so it survives a restart cleanly.
        if getattr(fx, "rack", None):
            try:
                fx.rack.reap_orphans_and_restore()
                print("  FX rack restored")
            except Exception as _e:
                print(f"  FX rack restore skipped: {_e}")
    else:
        print("  orc1_fx.py not found -- FX control unavailable (eng.fx = None)")

    # FX INSERTS -- arbitrary per-source wet/dry inserts (module outs OR hardware
    # inputs), each its own mixer + jalv chain, no bridging. reap_and_apply kills
    # any insert processes orphaned by a previous brain run, then applies the saved
    # ~/.orc1_fx_inserts.json. Runs on a background thread: it sleeps + spawns
    # per-insert processes that self-heal to their sources as the hosts come up, so
    # it must not block the brain's startup path.
    global fx_inserts
    if _FX_INSERTS_AVAILABLE:
        try:
            fx_inserts = _fx_inserts_mod.InsertManager(dbg=print)
            threading.Thread(target=fx_inserts.reap_and_apply, daemon=True).start()
            # Self-heal: a jackd restart (L-12 power cycle / auto-off) kills every
            # insert and the brain does not restart with jackd -- without this the
            # fx silently vanish until the next brain restart (measured 2026-09-24).
            threading.Thread(target=fx_inserts.heal_forever, daemon=True).start()
            print("  FX inserts manager ready")
        except Exception as _e:
            print(f"  FX inserts manager skipped: {_e}")

    # Module Matrix: load the live channel bindings (persisted map, or the shipped
    # seed on first run) and build the synth-bus channel set from them. Replaces
    # the old fixed cfg.SYNTH sweep -- channels are now bound/moved/removed live.
    _load_module_bindings()
    print(f"  Synth channels: {sorted(_SYNTH_CHS.keys())}")

    # Routing engine -- must be initialised after port_outs is ready
    global router
    router = RoutingEngine(cfg, port_outs)
    print("  Routing engine ready")

    # RC-505 output handler -- drives the looper over the dedicated "rc505" USB out
    # (transport/PC/CC only, never clock/DIN). Exists even if the RC is absent /
    # in storage mode; send_to("rc505",...) then no-ops until it reappears.
    global rc505
    try:
        from orc1_rc505 import RC505Handler
        rc505 = RC505Handler(send=lambda m: send_to("rc505", m),
                             config_path=getattr(cfg, "RC505_CONFIG",
                                                 "~/rc505_tools/rc505_config.json"),
                             channel=getattr(cfg, "RC505_CHANNEL", 1))
        print(f"  RC-505 handler ready ({len(rc505.cc_map)} actions, "
              f"out={'connected' if port_outs.get('rc505') else 'absent'})")
    except Exception as e:
        rc505 = None
        print(f"  ! RC-505 handler init: {e}")

    # BeatBuddy mind -- the foot-drummer state machine (orc1_beatbuddy.py, a faithful
    # port of the firmware songPlayer). Emits GM drum notes on the "bbkit" module's
    # bound channel; ORC1-BBKit renders them. Driven by the arp clock via
    # subscribe_ticks (every PPQN pulse, TICKS_PER_CLOCK=20), so it holds its own
    # transport whether or not the arp is arping, on internal OR external clock.
    global beatbuddy
    try:
        import orc1_beatbuddy as _bbmod

        def _bb_emit(note, vel, delay, ratio, part_id, pickup):
            if vel <= 0:
                return  # BeatBuddy voices are one-shots; the kit host ignores note-offs
            # Output is a CHOICE (_BB_OUT): the BBKit, the 909, or any MIDI port.
            for t in _bb_out_targets(note, vel, _BB_OUT, _module_channel("bbkit"),
                                     _module_channel("drums"),
                                     getattr(cfg, "BB_TO_909", {})):
                try:
                    if t[0] == "synth":
                        send_synth_midi(mido.Message("note_on", channel=t[1] - 1,
                                                     note=t[2], velocity=t[3]))
                    else:
                        _k, port, ch, n, v = t
                        send_to(port, mido.Message("note_on", channel=ch - 1,
                                                   note=n, velocity=v))
                        _bb_pending_offs.append((time.monotonic() + _BB_PORT_OFF_S,
                                                 port, ch - 1, n))
                except Exception:
                    pass

        beatbuddy = _bbmod.BeatBuddyMind(emit=_bb_emit)
        _bb_out_load()                     # restore the chosen output (default BBKit)
        beatbuddy._songs_dir = os.path.expanduser(
            getattr(cfg, "BB_SONGS_DIR", "~/.orc1_bbsongs"))

        # Foot pedal -> the mind (orc1_pedal). button_cb feeds the ported firmware
        # button grammar directly; action_cb runs the discrete gestures. A pure
        # sink: it drives the mind and emits to NO port (feedback-safe).
        global pedal
        pedal = _orc1_pedal.PedalInput(
            button_cb=lambda ev, t: beatbuddy.button_callback(ev, t),
            action_cb=_bb_pedal_action,
            mapping=getattr(cfg, "PEDAL_MAP", None),
            log_fn=lambda tag, s: print(f"  [{tag}] {s}", flush=True),
            # never None: a pedal port is ALWAYS behind a front door
            profile=getattr(cfg, "PEDAL_PROFILE", None) or _orc1_pedal.FCB_EF_PROFILE,
            exp_cb=_pedal_exp_value,
            switch_cb=_pedal_switch_action,
            pr_cb=_pedal_pr_action)
        _saved = _pedal_conf_load()      # the /pedal page's saved choices win
        if _saved:
            _pedal_conf_apply(_saved)
        threading.Thread(target=_pedal_poll_loop, daemon=True).start()

        if arp:
            _bb_last = [beatbuddy.PlayerStatus]

            def _bb_tick():
                global _bb_next_quit
                # The mind is internally serialized + self-recovering (orc1_beatbuddy
                # _serialized lock + the process_song invariant net), so this should
                # never raise. But the arp swallows subscriber exceptions silently,
                # so if something unforeseen ever DID raise here we would get a
                # silent wedge -- log it once so a fault is visible, not invisible.
                try:
                    beatbuddy.process_song(1.0, _bbmod.TICKS_PER_CLOCK)
                except Exception as e:
                    sig = repr(e)
                    if sig not in _bb_err_seen:
                        _bb_err_seen.add(sig)
                        print(f"  ! BeatBuddy tick fault (logged once): {sig}", flush=True)
                # BB_NEXT auto-quit: a beat after entering the transition, request
                # the quit so the mind advances to the next part (bar-quantised).
                if _bb_next_quit > 0:
                    _bb_next_quit -= 1
                    if _bb_next_quit == 0:
                        try:
                            beatbuddy.external_transition(0)   # quit -> advance
                        except Exception:
                            pass
                if pedal:
                    pedal.poll()
                _bb_flush_offs()          # note-offs owed to an external output
                # Refresh the state file on a mind status change so the web UI's
                # /state reflects self-driven transitions (intro->main, the
                # bar-quantized stop) -- print_status is otherwise event-driven.
                st = beatbuddy.PlayerStatus
                if st != _bb_last[0]:
                    _bb_last[0] = st
                    try:
                        write_state_json()
                    except Exception:
                        pass
            arp.subscribe_ticks(1, _bb_tick)
        _bb_song_restore()
        print(f"  BeatBuddy mind ready (songs {beatbuddy._songs_dir}, "
              f"ch={_module_channel('bbkit')}, pedal prefixes={list(_PEDAL_PREFIXES)})")
    except Exception as e:
        beatbuddy = None
        print(f"  ! BeatBuddy mind init: {e}")

    global cme
    cme = _orc1_cme.CmeLink(prefix=getattr(cfg, "CME_PORT_PREFIX", "u6midi"),
                            log=lambda s: print(s, flush=True))
    _cme_scan(set(mido.get_input_names()))

    threading.Thread(target=_cmd_socket_loop, daemon=True).start()
    threading.Thread(target=_cmd_poll_loop, daemon=True).start()
    print("  Web cmd socket: ~/.orc1_cmd.sock  (fallback poll: ~/orc1_cmd.json)")
    threading.Thread(target=_hotplug_loop, daemon=True).start()
    print("  MIDI hot-plug scanner running (3s interval)")
    threading.Thread(target=_surface_loop, daemon=True, name="surface").start()
    threading.Thread(target=_din_clock_watchdog, daemon=True).start()
    print("  DIN clock watchdog running (auto-sync external/internal)")
    _open_synth_bus()
    threading.Thread(target=_synth_bus_guard_loop, daemon=True).start()

    # Restore chord/melody output overrides, voice thru, and voice map from persisted files.
    _load_chord_out()
    _load_performve()
    _load_melody_out()
    _load_voice_thru()
    _load_voice_map()

    # Restore session-persistent flags from last saved state.
    # Only a small set of flags are restored -- things the user expects to
    # "stick" across restarts: coupling state, and nothing that depends on
    # live MIDI connections being in a specific state.
    _RESTORE_KEYS = ("synth_mirror_poly", "synth_mirror_chip")
    try:
        _sj = os.path.expanduser("~/orc1_state.json")
        if os.path.exists(_sj):
            with open(_sj) as _f:
                _saved = json.load(_f)
            for _k in _RESTORE_KEYS:
                if _k in _saved and isinstance(_saved[_k], bool):
                    state[_k] = _saved[_k]
            # O4 modes are STRINGS, so they cannot ride the bool loop above.
            # They are deliberate, persistent settings -- exactly what this
            # restore exists for -- and without this a restart silently put a
            # player back in DIAT/off with no error and no indication beyond
            # the sound changing.  Both values are validated rather than
            # trusted: the file is user-editable and a bad value here would be
            # consulted on every note.
            _qm = _saved.get("quality_mode")
            if _qm in ("DIAT", "FIXED"):
                state["quality_mode"] = _qm
            _sm = _saved.get("scale_mode")
            if _sm in SCALE_MODES:
                set_scale_mode(_sm)      # keeps the scale_snap mirror in step
            print(f"  Restored: mirror_poly={state['synth_mirror_poly']} "
                  f"mirror_chip={state['synth_mirror_chip']} "
                  f"quality={state['quality_mode']} snap={scale_mode_now()}")
    except Exception as _e:
        print(f"  [warn] state restore failed: {_e}")


    _orc1_playstate.PlayStatePublisher(_playstate_snapshot).start()
    print("  Play-state feed: /dev/shm/orc1_playstate.json (30 Hz max, on change)")
    print()
    print_status("STARTUP")
    print()
    print(f"  DIN in:       {state['din_routing']} mode")
    print(f"  CH {cfg.NOTE_INPUT_CH:2d}:        chord engine input/output")
    print(f"  CH {cfg.CH_CTRL:2d}:        control (consumed)")
    print(f"  CH {cfg.MELODY_CH:2d}:        melody snap ({state['melody_mode']})")
    print(f"  CH {sorted(cfg.ALWAYS_PASSTHROUGH)}:  always passthrough")
    print(f"  CH {sorted(_SYNTH_CHS.keys())}:  internal synth bus")
    print(f"  all others:   passthrough")
    print()
    print(f"  ARP: {'ON' if cfg.ARP_ENABLED_ON_BOOT else 'OFF'}  "
          f"Pattern:{cfg.ARP_DEFAULT_PATTERN}  "
          f"Rate:{cfg.ARP_DEFAULT_RATE}  "
          f"Clock:{cfg.ARP_DEFAULT_CLOCK}  "
          f"BPM:{cfg.ARP_DEFAULT_BPM}")
    print(f"  Scales: {SCALE_NAMES}")
    print(f"  Patterns: {len(pat.PATTERN_ORDER)} loaded")
    print("-" * 62)

    # Fire -- constructed last (2026-07-01 ownership refactor moved this from
    # before arp to after arp/seq/fx/router/threads): it now just borrows
    # eng.seq instead of building the Sequencer itself, and this ordering
    # also means eng.fx is genuinely live before Fire starts (matching the
    # FX-manager comment's original intent, which construction order used
    # to violate silently).
    global fire
    if fire_port and _FIRE_AVAILABLE:
        fire = FireHandler(sys.modules[__name__])
    else:
        print("  Fire not connected -- running without it")

    try:
        run_all_inputs(din_in, usb_ins, mixtrack_port, fire_port)
    except KeyboardInterrupt:
        pass


    print("\n\nShutting down -- all notes off...")
    if arp:
        arp.set_enabled(False)
        arp._stop_internal.set()
    all_notes_off()
    time.sleep(0.15)
    for port in port_outs.values():
        if port:
            try: port.close()
            except: pass
    for p in [p for p in [din_in]+usb_ins if p]:
        try: p.close()
        except: pass
    print("  Done.")


if __name__ == "__main__":
    main()
