# ═══════════════════════════════════════════════════════════════════════════════
# ORC-1 CONFIG v8
# Edit this file freely. Restart orc1.py to apply changes.
# ═══════════════════════════════════════════════════════════════════════════════


import os
_ORC1_DIR = os.path.dirname(os.path.abspath(__file__))   # portable: repo dir, not hardcoded

DEBUG = False


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════════════════════
# CH 14 is ALWAYS passthrough — hardcoded, cannot be changed.
# CH 10 is ALWAYS passthrough — drums.
# Everything not listed below also passes through untouched.


# Control channel — consumed entirely, never forwarded
CH_CTRL = 13


# Live volume controls (master + per-engine) — sent on CH_CTRL of the INTERNAL
# synth bus. That channel carries nothing else on the bus (the brain consumes it
# for the Fire's control surface and never forwards it), so these four CCs can
# never collide with a note or a param CC. Single source of truth shared by the
# synth (which decodes them into engine.master / poly.master / chip.master /
# drum_master) and by the Fire faders + web LEVELS sliders (which encode them).
# BOOT values (0-127) are just the fader/slider START positions, picked to match
# the engine's own startup gains so nothing jumps until you actually move one:
#   master 0.60 (== MASTER_LEVEL) · poly 0.50 · chip 0.35 · drum 0.50
# DRUM_BOOT is the one exception: the synth DERIVES its drum-bus startup gain
# from it (drum_master = DRUM_BOOT/127), so this key is the single source of
# truth for that bus rather than a display value that mirrors one.
# Since 2026-07-29 all four levels PERSIST across a synth restart
# (~/.orc1_bus_levels.json, plan item P11), so these BOOT values only apply
# to a bus that has never been moved — they are a safety net, not the level.
SYNTH_VOL = {
    "CH":          13,   # == CH_CTRL
    "MASTER_CC":    7,
    "POLY_CC":      8,
    "CHIP_CC":      9,
    "DRUM_CC":     10,
    "MONO_CC":     11,   # ORC1-Mono bus level (2026-09-03); 11 was free on CH_CTRL
    "MONO_BOOT":   64,   # round(0.50 * 127)
    "FXMIX_MIX_CC":   3, # ORC1-FXMix wet/dry (2026-09-03); 3/4 free on CH_CTRL
    "FXMIX_LEVEL_CC": 4, # ORC1-FXMix return level (unity at 64, up to 2x)
    "MASTER_BOOT": 76,   # round(0.60 * 127)
    "POLY_BOOT":   64,   # round(0.50 * 127)
    "CHIP_BOOT":   44,   # round(0.35 * 127)
    # Was 127 (unity) — unity as the default for the loudest bus is aggressive,
    # and before persistence every restart silently slammed the drums back to
    # it mid-session. 64 = 0.50 = -6 dB is a conservative starting point.
    "DRUM_BOOT":   64,   # round(0.50 * 127)
}


# Chord engine input/output channel
NOTE_INPUT_CH = 12
NOTE_OUTPUT_CH = 12 # default chord output channel (used by mirror presets)


# Melody snap channel — input and output
MELODY_CH = 15


# Channels that always pass through untouched (in addition to CH 14)
ALWAYS_PASSTHROUGH = {10, 14}


# ── Internal synth voice channels, named ONCE ─────────────────────────────────
# PRESETS below is defined before the SYNTH dict, and used to hardcode these as
# bare integers. When drums moved to GM ch10 on 2026-07-30 and CH_CHIP_P2 took
# ch6, four PRESETS entries still said `"channel": 10` meaning "Pulse 2" -- so
# selecting NES_CHORD / NES_FULL / NES_ARP_POLY silently sent the chord's 3rd
# (and NES_FULL's bass) to the DRUM channel, where ordinary chord pitches match
# no drum note and simply vanish. Nothing errored.
# These constants exist so a channel is named in exactly one place: SYNTH below
# is built from them too, so PRESETS and SYNTH can no longer drift apart.
_CH_DRUM     = 10   # GM drum channel
_CH_MONO     = 7
_CH_POLY     = 8
_CH_CHIP     = 9    # NES Pulse 1
_CH_CHIP_P2  = 6    # NES Pulse 2  (took ch6 when drums moved to GM ch10)
_CH_CHIP_TRI = 11
_CH_CHIP_NS  = 4
_CH_GB_WAVE  = 5
_CH_CHIP_PCM = 16


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE MATRIX — live per-module channel binding
# ═══════════════════════════════════════════════════════════════════════════════
# A "module" is a synth voice you bind onto a MIDI channel at will, move to another
# channel, or take out of the fold entirely. The channel a module listens on is still
# stored in cfg.SYNTH["CH_<...>"] (the in-process single source of truth, mutated
# live) — both the synth's LISTEN dispatch AND the brain's chord/arp OUTPUT read it,
# so they can never diverge. These constants are the shared vocabulary the brain and
# synth agree on for the bind handshake. Live map persists to ~/.orc1_bindings.json.
# Full design: _planning/ORC1_Module_Matrix_ROADMAP.md (§6/§6a) + _Slice1_Spec.md.
MODULE_KEYS = [                       # list order fixes the id used on the wire
    "drums", "poly", "chip_p1", "chip_p2", "chip_tri",
    "chip_noise", "chip_pcm", "gb_wave", "sampler",
    "mono",                           # appended 2026-09-03 (ORC1-Mono host); a
                                      # NEW id at the END keeps every existing id
                                      # stable. Not a chip voice -> not in
                                      # MODULE_VOICE; the mono host owns its channel.
    "bbkit",                          # appended 2026-09-14 (ORC1-BBKit host, the
                                      # sampled BeatBuddy kit engine). Same shape as
                                      # mono: new id at the END, owns its channel via
                                      # bindings, not a chip voice.
    "chip_noise2",                    # appended 2026-09-24: a SECOND noise voice
                                      # (NESEngine 'noise2', buzzy by default) so
                                      # white + buzzy noise sound at once. Starts
                                      # unbound; bind it from /matrix, or reach it
                                      # from the noise channel via CC 114 key split.
    "visuals",                        # appended 2026-09-29: the HDMI VISUALS scenes
                                      # (daw/orc_visuals.py). NOT a sound module and
                                      # no host: it only reads CC 20-27 on its bound
                                      # channel off the synth bus (a Fire knob naming
                                      # "visuals", any controller, or the /system
                                      # card). Starts unbound; bind it from /matrix or
                                      # the /system VISUALS card.
    "zoomfx",                         # appended 2026-10-13: the Zoom MultiStomp pedal
                                      # (MS-70CDR) driven by plain CCs on its bound
                                      # channel. NOT a sound module, no audio host:
                                      # orc1_zoomfx.py reads the synth bus and owns
                                      # the pedal's USB port. Starts unbound.
]
# module key -> the cfg.SYNTH["CH_*"] key holding its channel. NB the sampler's
# channel lives in cfg.SAMPLER["CH_SAMPLER"] (== CH_MONO 7), NOT cfg.SYNTH, and its
# facade emits on the brain's own _CH_SAMPLER — so the sampler is handled specially
# (reassign updates _CH_SAMPLER + the synth's CH_SAMPLER; it is never taken out).
MODULE_CH_KEY = {
    "drums": "CH_DRUM", "poly": "CH_POLY", "chip_p1": "CH_CHIP",
    "chip_p2": "CH_CHIP_P2", "chip_tri": "CH_CHIP_TRI", "chip_noise": "CH_CHIP_NS",
    "chip_pcm": "CH_CHIP_PCM", "gb_wave": "CH_GB_WAVE", "sampler": "CH_SAMPLER",
}
# module key -> synth voice name (the chip family shares one render/CC body).
MODULE_VOICE = {
    "chip_p1": "pulse1", "chip_p2": "pulse2", "chip_tri": "triangle",
    "chip_noise": "noise", "chip_pcm": "pcm", "gb_wave": "wave",
    "chip_noise2": "noise2",
}
# Channels a module may never be bound to — each is consumed for its own purpose.
# 14 is the MPC control channel. 15 (MELODY_CH) is NOT reserved: melody-snap
# output goes to a physical port, not the synth bus, so a voice may live on 15.
RESERVED_CHANNELS = {NOTE_INPUT_CH, CH_CTRL, 14}             # 12, 13, 14
# The sampler is the always-live deck-control channel: it may be REASSIGNED but never
# taken out of the fold (its facade always needs a channel to emit on).
MODULE_NO_TAKEOUT = {"sampler"}
# Bind handshake, carried on CH_CTRL of the INTERNAL synth bus (the brain emits it;
# a controller's CH_CTRL is never forwarded to the synth, so it cannot be spoofed):
#   CC BIND_ARM_CC   value = module id  -> arm: the NEXT note-on's channel is the target
#   CC BIND_CLEAR_CC value = module id  -> take that module out of the fold
# CC 20/21 on CH_CTRL are free (the synth uses only 7/8/9/10 = the volume CCs there).
BIND_ARM_CC   = 20
BIND_CLEAR_CC = 21
# The commit is a note-on on the TARGET channel, but it must be a note no player
# or sequencer would ever send, or an autonomous note could be mistaken for the
# commit (and vel is not safe -- vel_lock rewrites it). A fixed SENTINEL note
# number the brain sends and the synth requires closes that window.
BIND_COMMIT_NOTE = 1
BINDINGS_FILE = "~/.orc1_bindings.json"   # persisted channel->module (bound only)

# ── PROTECTED CHANNELS (2026-10-14, owner: "protect those channels", with an on/off per channel) ──
# A protected channel can't be PICKED as a destination anywhere in the ORC (module binding on /matrix,
# pedal targets, BeatBuddy / chord / voice-thru outs, Fire knob + fader targets, pad-map surfaces).
# RESERVED_CHANNELS above is only the shipped default; the live set is protected_channels(), edited on
# /matrix (PROTECTED CHANNELS card) and stored in PROTECTED_FILE (the web process is its only writer).
# CH_CTRL is LOCKED on: the brain consumes everything arriving on it and the bind handshake rides on it,
# so "unprotecting" it would break the ORC, not free a channel.
# Protection is checked when something is CHOSEN (saved / bound), never when old config is loaded, and
# a channel with a module bound to it can't be switched on (move the module first), so switching
# protection on never silently drops anything.
PROTECTED_FILE = "~/.orc1_protected_channels.json"
PROTECTED_DEFAULT = {NOTE_INPUT_CH: "chord", CH_CTRL: "ORC", 14: "MPC"}   # short: shown in the /matrix cells
PROTECTED_LOCKED = {CH_CTRL}
_prot_cache = {"key": None, "conf": None}


def protected_config():
    """{channel: {"on": bool, "name": str}} for every channel that has an entry (the defaults when the file is
    missing or unreadable). Cheap: re-read only when the file's mtime changes."""
    import json as _json
    p = os.path.expanduser(PROTECTED_FILE)
    try:
        key = os.stat(p).st_mtime_ns
    except OSError:
        key = None
    if _prot_cache["key"] == key and _prot_cache["conf"] is not None:
        return _prot_cache["conf"]
    conf = {c: {"on": True, "name": n} for c, n in PROTECTED_DEFAULT.items()}
    if key is not None:
        try:
            with open(p) as f:
                raw = _json.load(f).get("channels", {})
            conf = {}
            for c, e in raw.items():
                ci = int(c)
                if 1 <= ci <= 16 and isinstance(e, dict):
                    conf[ci] = {"on": e.get("on") is True, "name": str(e.get("name", ""))[:24]}
        except Exception:
            conf = {c: {"on": True, "name": n} for c, n in PROTECTED_DEFAULT.items()}
    for c in PROTECTED_LOCKED:
        conf[c] = {"on": True, "name": (conf.get(c) or {}).get("name") or PROTECTED_DEFAULT.get(c, "")}
    _prot_cache.update(key=key, conf=conf)
    return conf


def protected_channels():
    """The live set of protected channels (1-based)."""
    return {c for c, e in protected_config().items() if e["on"]}


class _LiveProtected:
    """A set-like LIVE view of protected_channels(): `ch in PROTECTED`, iteration and len() always reflect the
    current file, so code written against the old constant set (the brain's _MM_RESERVED, the hosts'
    _RESERVED_CH) follows the /matrix setting without a restart."""
    def __contains__(self, ch):
        return ch in protected_channels()

    def __iter__(self):
        return iter(sorted(protected_channels()))

    def __len__(self):
        return len(protected_channels())

    def __repr__(self):
        return f"PROTECTED{sorted(protected_channels())}"


PROTECTED = _LiveProtected()


def protect_error(ch):
    """'' when ch may be chosen as a destination, else a sentence saying why not."""
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        return "bad channel"
    if ch in protected_channels():
        name = protected_config().get(ch, {}).get("name")
        return (f"ch {ch} is protected" + (f" ({name})" if name else "")
                + ("" if ch in PROTECTED_LOCKED else " -- switch that off on /matrix to use it"))
    return ""

# BeatBuddy foot-drummer (orc1_beatbuddy.py, the mind). Factory .BBS songs live
# here on the Pi (staged out of the repo, like recordings/kits). BB_SONG_SELECT
# resolves a bare name under this dir.
BB_SONGS_DIR = "~/.orc1_bbsongs"

# BeatBuddy -> 909 (2026-09-23). When the BeatBuddy output is "909", each BB kit
# note is translated to an ORC drum-module note (GM numbers the drum host maps:
# 36 BD, 38 SD, 37 rim, 39 clap, 42 closed hat, 46 open hat, 41/47/50 low/mid/hi
# tom, 49 crash, 51 ride, 56 cowbell). owner: FOLD to the nearest voice (every
# hit sounds) -- he'd rather the extra voices existed, so as the drum module
# grows (ride bell, splash, china...) re-point these at the new voices. A BB
# note missing here is dropped. BB notes = the kit.json layout (GM-based).
BB_TO_909 = {
    36: 36,                                   # kick / djembe bass
    1: 38, 38: 38,                            # snare / new snare / djembe open
    35: 38, 40: 38, 41: 38,                   # brush sweeps -> snare
    37: 37, 75: 37, 76: 37, 86: 37, 87: 37,   # cross stick, clave, jam block, cascara -> rim
    39: 39,                                   # clap / stick-on-stick
    42: 42, 44: 42,                           # closed hat, foot hat
    46: 46, 47: 46,                           # open hat, foot splash
    54: 42, 70: 42, 71: 42, 82: 42, 68: 42, 73: 42, 80: 42,   # short shakers -> closed hat
    101: 42, 102: 42, 104: 42,
    69: 46, 74: 46, 81: 46,                   # long cabasa / guiro / guira -> open hat
    43: 41, 78: 41, 79: 41, 103: 41,          # tom 4, tumba, shekere bass -> low tom
    45: 47, 58: 47, 61: 47, 64: 47, 66: 47, 67: 47,   # tom 3, low hand drums -> mid tom
    48: 48,                                   # tom 2 -> hi-mid tom (its own voice)
    50: 50, 60: 50, 62: 50, 63: 50, 65: 50,   # tom 1, high hand drums -> hi tom
    49: 49, 52: 49, 57: 49, 90: 49,           # crashes, bow-crash -> crash
    55: 55,                                   # splash -> splash (its own voice)
    51: 51, 59: 51, 91: 51,                   # ride, ride 2 -> ride
    53: 53,                                   # ride bell -> ride bell (its own voice)
    56: 56, 88: 56, 89: 56,                   # cowbell + timbale bells -> cowbell
}

# Foot pedal for the BeatBuddy mind (orc1_pedal.py). Recognised by USB/MIDI NAME.
# The FCB1010 has no USB; it reaches the Pi via a generic USB->DIN adapter that
# enumerates as "CH345" (QinHeng CH345 cable) -- hence the prefixes below.
# ⚠ The pedal input is handled on its own thread and CONSUMED (never passed to a
# DIN out), which is what stops the CH345<->pisound DIN cross-patch from looping.
# PEDAL_MAP: leave as None to use orc1_pedal.DEFAULT_MAP; override once the real
# FCB's messages are known (they are logged to the journal as "[pedal] ...").
PEDAL_PORT_PREFIXES = ["ch345", "fcb"]   # the CH345 adapter IS the FCB (owner, 2026-09-23)
# PATCHBAY_PORT_PREFIXES: MIDI routers whose USB ports the brain does NOT open as
# engine inputs (name substrings, case-insensitive). The CME U6MIDI Pro: with the
# pisound DIN out cabled into it, opening it as an input loops MIDI (2026-09-30).
# Also any device with its OWN handler process: the Zoom MultiStomp ("zoom ms",
# owned by orc1_zoomfx.py; opened here its SysEx replies went out the DIN).
PATCHBAY_PORT_PREFIXES = ["u6midi", "zoom ms"]
# PEDAL_PROFILE: the front door -- only these messages are accepted, all else dropped.
# None = orc1_pedal.FCB_EF_PROFILE (FCB1010/EurekaPROM in EF mode, measured).
# The FCB's MODE button picks the page family: "ef" (toggle, press only), "notes"
# (the FCB's SP mode: press + release, switches 3-10 only -- 1/2 are relays), "pc"
# (every switch identical = one button). EF switches 9/10 = page down/up.
# PEDAL_MAP None = orc1_pedal.DEFAULT_FCB_MAP (EF 1-6 = chords I-VI; SP 3-8 = chords
# I-VI held, SP 9 = BeatBuddy footswitch; pc = BB tap).
# Shape: {"ef": {"all": {9: "page:down", 10: "page:up"}, "pages": [{1: "chord:1",
# ...}, {...}]}, ...}; actions are listed above DEFAULT_FCB_MAP.
PEDAL_PROFILE = None
PEDAL_MAP = None
# Chords from the pedal: EF (no release) -> "latch" (rings until the next chord, same
# switch again = stop) or "stab" (fixed length). Note mode always sounds while held.
PEDAL_CHORD_STYLE = "latch"
PEDAL_CHORD_STAB_BEATS = 1
PEDAL_CHORD_OCTAVE = 4        # degree I root = 12*octave + key root (4 -> C = note 48)
PEDAL_CHORD_VEL = 100
# Expression pedals -> CC: {"A": {"port": "synth" | a PORTS key, "ch": 1-16, "cc": n}}.
# None / missing = not sent anywhere. Channel 13 (CH_CTRL) is always refused.
PEDAL_EXP_MAP = {"A": None, "B": None}

# ── Module Matrix: P1/P2 JOIN (slice 2) ──────────────────────────────────────
# The engine already couples pulse1->pulse2 (NESEngine): playing chip_p1's
# channel also sounds pulse2, shaped by three CCs sent on chip_p1's channel.
# The brain mirrors the join state and re-emits these on demand; the DSP + the
# decode already exist in orc1_synth.NESEngine.set_cc. This list MUST match
# NES_COUPLE_MODES in orc1_synth.py (and CHIP_COUPLE_MODES in orc1_fire.py) --
# the same bucketing (idx * 128 // len) maps a mode name to a CC72 value.
COUPLE_MODES     = ['none', 'unison', 'detune', 'fifth', 'third', 'octave', 'octave_dn']
JOIN_MODE_CC     = 72   # which join (7 modes bucketed across 0-127)
JOIN_DETUNE_CC   = 73   # detune amount, DETUNE mode only. CC64 = 0ct, 0/127 = -/+50ct
JOIN_SPREAD_CC   = 76   # stereo spread on/off (0-63 off, 64-127 on)


# ═══════════════════════════════════════════════════════════════════════════════
# PORT DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════
# Mixtrack port detection — substring match against MIDI port names.
# "mixtrack" matches "Numark Mixtrack Pro 3" — change only if needed.
MIXTRACK_PORT_PREFIX = "mixtrack"

# SHIFT + FX1/2/3 (ring buttons) saves this many seconds of that skipback ring
# to ~/recordings (2026-09-23). skipback.sh clamps to the ring's own length.
MIXTRACK_RING_SAVE_SECS = 60

PORTS = {
    # MIDI OUTPUT DESTINATIONS.  Key = the name you reference in presets and
    # CHORD_ROW_MAP; value = a substring of the ALSA/JACK MIDI port name
    # (see `aconnect -l`).  "synth" is special-cased (the internal synth bus)
    # and is always available without an entry here.
    "pisound": "pisound",           # DIN + USB MIDI on the pisound HAT
    # "laptop": "rtpmidid:YOUR_SESSION",  # example: RTP-MIDI network session to a computer/DAW
    "rc505":   "RC-505 MIDI 1",     # Boss RC-505 looper (USB port 1) -- CONTROL TARGET.
                                    # Driven by orc1_rc505.py: transport/PC/CC only,
                                    # NEVER clock (Sync=AUTO(USB) would slave+artifact),
                                    # NEVER the DIN (feedback loop). Not in CLOCK_OUT_PORTS.
    "performve": "Perform-VE",      # TC-Helicon Perform-VE (USB). Feed it a chord
                                    # (>=2 notes, root+3rd) on ch1 to drive HardTune
                                    # NaturalPlay key-lock. Follows maj/min; NOT
                                    # harmonic-minor/pentatonic (box scale-engine limit).
    # Add your own external gear below, e.g.:
    # "sampler": "SP-404",          # a connected hardware sampler (name substring)
}

# ── RC-505 control bridge (BeatStep -> ORC -> RC) ──────────────────────────────
RC505_CONFIG       = "~/rc505_tools/rc505_config.json"   # authored map (handler reads it)


# ═══════════════════════════════════════════════════════════════════════════════
# MIRROR SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
# Mirror mode is now a CC toggle (MIRROR_INPUT_CH) — applies to any preset.
# BASS_FIXED_CH / BASS_MIRROR_PORT still used by the MONO_BASS2 preset.
BASS_FIXED_CH    = 2
BASS_MIRROR_PORT = "pisound"


# ═══════════════════════════════════════════════════════════════════════════════
# TIMBRAL PRESETS
# ═══════════════════════════════════════════════════════════════════════════════
# VOICE SLOTS:
# CHORD_ALL   all chord notes together (polyphonic) — arp falls back here
# CHORD_ROOT  tone 0 (root)                         — arp uses this for tone 0
# CHORD_3RD   tone 1                                — arp uses this for tone 1
# CHORD_5TH   tone 2                                — arp uses this for tone 2
# CHORD_7TH   tone 3                                — arp uses this for tone 3
# CHORD_9TH   tone 4                                — arp uses this for tone 4
# BASS        one octave below root
# LEAD        highest note of chord
# ARP_DEST    optional override: pin ALL arp tones to one {port, channel}
#             regardless of individual slot assignments.  Use this when you
#             want the arp cycling on one voice (e.g. Pulse 1) while the chord
#             plays sustained harmony on the others (e.g. P2 + Triangle).
#
# ARP ROUTING BEHAVIOUR:
# The arp engine routes each step's tone through voice_resolve_fn (see orc1.py).
# It tries ARP_DEST first, then the per-tone slot (CHORD_ROOT for tone 0, etc.),
# then falls back to CHORD_ALL.  Setting CHORD_ALL: None and defining individual
# slots is enough — the arp will automatically distribute across them.
# Example: NES_CHORD has CHORD_ROOT→P1, CHORD_3RD→P2, CHORD_5TH→Triangle.
# With the arp, tone 0 goes to P1, tone 1 to P2, tone 2 to Triangle —
# exactly how the NES APU arpeggiated chords from a single audio triangle wave.
#
# Mirror presets use NOTE_OUTPUT_CH for chord channel instead of the
# value specified here.
# Set any slot to None to silence that voice.
# Avoid channels 10, 13, 14, 15 as destinations.
# ═══════════════════════════════════════════════════════════════════════════════
# Internal ("synth") destinations NAME the module ("chip_p2"), resolved live from
# the /matrix bindings (orc1._dest_channel, 2026-09-24). They were the _CH_*
# constants frozen at import: after chip P2 moved to ch 8 the NES presets sent
# their 3rd to ch 6 = the POLY.
PRESETS = {


    # Plays the chord engine's voiced chords on the INTERNAL poly synth (CH 8),
    # nothing out the DIN. Select this to hear PERFORM chords internally.
    "POLY_INT": {
        "CHORD_ALL": {"port": "synth", "channel": "poly"},
        "BASS": None,
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    # ── NES / Gameboy presets ──────────────────────────────────────────────────
    # Each voice is monophonic. Root→P1, 3rd→P2, 5th→Triangle gives an
    # authentic 3-voice APU chord. Triangle has no amplitude envelope — it just
    # appears and disappears, like the real hardware.

    "NES_CHORD": {
        # 3-voice NES chord: root on P1, 3rd on P2, 5th on Triangle
        "CHORD_ALL":  None,
        "CHORD_ROOT": {"port": "synth", "channel": "chip_p1"},   # Pulse 1
        "CHORD_3RD":  {"port": "synth", "channel": "chip_p2"},  # Pulse 2
        "CHORD_5TH":  {"port": "synth", "channel": "chip_tri"},  # Triangle
        "CHORD_7TH":  None,
        "CHORD_9TH":  None,
        "BASS":       None,
        "LEAD":       None,
    },

    "NES_LEAD": {
        # Monophonic melody on Pulse 1
        "CHORD_ALL":  {"port": "synth", "channel": "chip_p1"},
        "CHORD_ROOT": None, "CHORD_3RD": None, "CHORD_5TH": None,
        "CHORD_7TH":  None, "CHORD_9TH": None,
        "BASS":       None, "LEAD": None,
    },

    "NES_BASS_LEAD": {
        # Triangle as bass (one octave below root), Pulse 1 as lead melody
        "CHORD_ALL":  None,
        "BASS":       {"port": "synth", "channel": "chip_tri"},  # Triangle — natural bass
        "LEAD":       {"port": "synth", "channel": "chip_p1"},   # Pulse 1 — melody
        "CHORD_ROOT": None, "CHORD_3RD": None, "CHORD_5TH": None,
        "CHORD_7TH":  None, "CHORD_9TH": None,
    },

    "NES_FULL": {
        # All five chord voices spread across both pulse channels + triangle
        # 7th and 9th are silent (hardware limit)
        "CHORD_ALL":  None,
        "CHORD_ROOT": {"port": "synth", "channel": "chip_p1"},   # P1
        "CHORD_3RD":  {"port": "synth", "channel": "chip_p2"},  # P2
        "CHORD_5TH":  {"port": "synth", "channel": "chip_tri"},  # Triangle
        "CHORD_7TH":  None,
        "CHORD_9TH":  None,
        "BASS":       {"port": "synth", "channel": "chip_p2"},  # Bass doubles P2
        "LEAD":       {"port": "synth", "channel": "chip_p1"},   # Lead doubles P1
    },

    "NES_ARP_POLY": {
        # Classic NES texture: sustained 2-voice chord on P2+Triangle while
        # the arp cycles through all chord tones on Pulse 1 only.
        # ARP_DEST pins the arp to P1 regardless of which tone is playing.
        # The chord still distributes 3rd→P2 and 5th→Triangle for a full sound.
        # Great at fast arp rates (1/16 or 1/32) — the P1 cycles so quickly
        # it sounds like it's playing all the chord tones simultaneously.
        "CHORD_ALL":  None,
        "CHORD_ROOT": None,                               # Root silent (arp covers it)
        "CHORD_3RD":  {"port": "synth", "channel": "chip_p2"},  # 3rd sustained on P2
        "CHORD_5TH":  {"port": "synth", "channel": "chip_tri"},  # 5th sustained on Triangle
        "CHORD_7TH":  None,
        "CHORD_9TH":  None,
        "BASS":       None,
        "LEAD":       None,
        # ARP_DEST: pin all arp tones to Pulse 1 regardless of tone index
        "ARP_DEST":   {"port": "synth", "channel": "chip_p1"},
    },


    "MONO_CLEAN": {
        "CHORD_ALL": {"port": "pisound", "channel": 12}, # mirrored
        "BASS": None,
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "MONO_BASS": {
        "CHORD_ALL": {"port": "pisound", "channel": 12}, # mirrored
        "BASS": {"port": "pisound", "channel": 12}, # mirrored
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "MONO_BASS2": {
        "CHORD_ALL": {"port": "pisound", "channel": 12}, # mirrored
        "BASS": {"port": "pisound", "channel": 2}, # BASS_FIXED_CH
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "BASS_SPLIT": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 2},
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "LEAD_OUT": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 2},
        "LEAD": {"port": "pisound", "channel": 3},
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "FULL_SPLIT": {
        "CHORD_ALL": None,
        "CHORD_ROOT": {"port": "pisound", "channel": 12},
        "CHORD_3RD": {"port": "pisound", "channel": 2},
        "CHORD_5TH": {"port": "pisound", "channel": 3},
        "CHORD_7TH": {"port": "pisound", "channel": 4},
        "CHORD_9TH": {"port": "pisound", "channel": 5},
        "BASS": {"port": "pisound", "channel": 6},
        "LEAD": {"port": "pisound", "channel": 7},
    },


    "MPC_MAC": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "mac", "channel": 1},
        "LEAD": {"port": "mac", "channel": 2},
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "USER_1": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 12},
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "USER_2": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 12},
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "USER_3": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 12},
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },


    "USER_4": {
        "CHORD_ALL": {"port": "pisound", "channel": 12},
        "BASS": {"port": "pisound", "channel": 12},
        "LEAD": None,
        "CHORD_ROOT": None,
        "CHORD_3RD": None,
        "CHORD_5TH": None,
        "CHORD_7TH": None,
        "CHORD_9TH": None,
    },
}


DEFAULT_PRESET = "MONO_CLEAN"


# ═══════════════════════════════════════════════════════════════════════════════
# MIDI ROUTING  (orc1_routes.py)
# ═══════════════════════════════════════════════════════════════════════════════
# Static rules active regardless of what devices are connected.
# Per-device profiles (auto-activated on hotplug) live in ~/orc1_routes.json.
#
# Rule fields:
#   in_port    port name substring or "any"               default "any"
#   in_ch      1-16, [list], or "any"                    default "any"
#   in_type    "note" | "cc" | "pc" | "any"              default "any"
#   match      [min, max] note/cc range                   default None (all)
#   out_port   port key (see PORTS above)                 default "pisound"
#   out_ch     1-16 or "same"                             default "same"
#   transform  None | "bank_align" | "transpose"          default None
#   semitones  int (only used with "transpose")           default 0
#   action     "forward" | "consume"                      default "forward"
#
# bank_align: renumber notes so match[0] maps to note 36 (SP-404 banks)
# transpose:  shift pitch by semitones
#
# Example — controller on CH1 routing to SP-404 banks A-D:
# MIDI_REMAP = [
#     {"in_ch": 1, "in_type": "note", "match": [36,  51], "out_port": "pisound", "out_ch": 1},
#     {"in_ch": 1, "in_type": "note", "match": [52,  67], "out_port": "pisound", "out_ch": 2, "transform": "bank_align"},
#     {"in_ch": 1, "in_type": "note", "match": [68,  83], "out_port": "pisound", "out_ch": 3, "transform": "bank_align"},
#     {"in_ch": 1, "in_type": "note", "match": [84,  99], "out_port": "pisound", "out_ch": 4, "transform": "bank_align"},
# ]
MIDI_REMAP = []

# CH14 eavesdrop rules: ORC-1 actions triggered by incoming CH14 CCs/notes.
# The message is ALWAYS forwarded to pisound after processing — nothing blocked.
# Format: {"in_type": "cc", "match": [cc_num, cc_num], "action": "ORC1_ACTION_NAME"}
# (action names mirror CTRL_CC_MAP values above)
CH14_RULES = []


# ═══════════════════════════════════════════════════════════════════════════════
# PC → PRESET MAP (PC on CH_CTRL)
# ═══════════════════════════════════════════════════════════════════════════════
PC_PRESET_MAP = {
    40: "MONO_CLEAN",
    41: "MONO_BASS",
    42: "MONO_BASS2",
    43: "BASS_SPLIT",
    44: "LEAD_OUT",
    45: "FULL_SPLIT",
    46: "MPC_MAC",
    47: "USER_1",
    48: "USER_2",
    49: "USER_3",
    50: "USER_4",
    # NES presets
    51: "NES_CHORD",
    52: "NES_LEAD",
    53: "NES_BASS_LEAD",
    54: "NES_FULL",
    55: "POLY_INT",      # internal poly synth — chords on CH8
    56: "NES_ARP_POLY",  # arp on P1, chord on P2+Triangle
}


# ═══════════════════════════════════════════════════════════════════════════════
# CHORD BASE TYPE SELECT (note_on on CH_CTRL)
# Maps note → "MAJ" "MIN" "SUS" "DIM"
# In CHORD mode: silent. In ROOT mode: fires immediately.
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_BASE_NOTES = {
    36: "MAJ",
    37: "MIN",
    38: "SUS",
    39: "DIM",
}


# ═══════════════════════════════════════════════════════════════════════════════
# CHORD EXTENSION TOGGLES (note_on on CH_CTRL)
# Maps note → "6" "m7" "M7" "9" — each press toggles on/off, stacks freely
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_EXT_NOTES = {
    40: "6",
    41: "m7",
    42: "M7",
    43: "9",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SCALE-DEGREE EXTENSION TOGGLES (note_on on CH_CTRL) -- added 2026-07-29 (O3)
# Maps note → "6th" "7th" "9th" "11th".  These stack another rung of the ACTIVE
# SCALE's ladder, so the scale decides each one's quality (in C major the V gets
# a real dominant 7 and the vii an m7b5, automatically).
#
# CTRL_EXT_NOTES above is the CHROMATIC escape hatch and is unchanged: fixed
# semitone offsets that deliberately ignore the scale, reachable on the Fire via
# SHIFT + the extension pads.  Keeping both means the m7/M7 distinction is still
# available when you want to force it.
#
# Notes 44-47 were free: 36-39 bases, 40-43 chromatic exts, 52-67 triggers,
# 68-74 modifiers, 75 the function key.
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_EXT_DEGREE_NOTES = {
    44: "6th",
    45: "7th",
    46: "9th",
    47: "11th",
}


# ═══════════════════════════════════════════════════════════════════════════════
# CHORD TRIGGERS (note_on on CH_CTRL, ROOT mode only)
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_TRIGGER_NOTES = {
    52: True, 53: True, 54: True, 55: True,
    56: True, 57: True, 58: True, 59: True,
    60: True, 61: True, 62: True, 63: True,
    64: True, 65: True, 66: True, 67: True,
}


# ═══════════════════════════════════════════════════════════════════════════════
# MODIFIER NOTES (note_on on CH_CTRL)
# Valid actions:
# INV_UP inversion up
# INV_DOWN inversion down
# OCT_UP octave up
# OCT_DOWN octave down
# EXT_CLEAR clear all extensions
# PREV_CHORD return to previous chord
# PANIC all notes off
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_MOD_NOTES = {
    68: "INV_UP",
    69: "INV_DOWN",
    70: "OCT_UP",
    71: "OCT_DOWN",
    72: "EXT_CLEAR",
    73: "PREV_CHORD",
    74: "PANIC",
}


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTION KEY
# ═══════════════════════════════════════════════════════════════════════════════
# A single held note on CH_CTRL activates the function layer.
# While held, CCs route through FUNC_CC_MAP instead of CTRL_CC_MAP.
# On release:
#   FUNC_TRANSPOSE  → reverts to 0 (pitch snaps back)
#   FUNC_SCALE_SWEEP / FUNC_PATTERN_SWEEP → locks in the selection
#
# Set FUNC_KEY_NOTE to whichever pad you want as your shift key.
# It must not appear in CTRL_BASE_NOTES, CTRL_EXT_NOTES, CTRL_TRIGGER_NOTES,
# or CTRL_MOD_NOTES.
FUNC_KEY_NOTE = 75   # default: pad above the modifier row — change freely

# ── Relative encoder mode ─────────────────────────────────────────────────────
# "offset"  — Arturia BSP default: 65=+1 63=-1 64=no change
# "twos"    — two's complement:    1=+1  127=-1
RELATIVE_CC_MODE = "offset"

# ── Function key CC map ───────────────────────────────────────────────────────
# Maps CC number → func action while FUNC_KEY_NOTE is held.
# If a CC is not listed here, it falls through to the normal CTRL_CC_MAP.
#
# FUNC_LEAD_VOICE     relative rotary → reshapes chord voicing tone-by-tone
# FUNC_SCALE_SWEEP    relative rotary → sweep scales live, lock on release
# FUNC_PATTERN_SWEEP  relative rotary → sweep arp patterns live, lock on release
# FUNC_TRANSPOSE      relative rotary → transpose ±semitones, revert on release
FUNC_CC_MAP = {
    1: "FUNC_SCALE_SWEEP",    # CC 1 (normally VL strength) → scale sweep
    2: "FUNC_PATTERN_SWEEP",  # CC 2 (normally spread)      → pattern sweep
    3: "FUNC_TRANSPOSE",      # CC 3 (normally oct offset)  → live transpose
    5: "FUNC_LEAD_VOICE",     # CC 5 (normally inv sweep)   → lead voice
}

# ── Always-on CC actions (no func key needed) ─────────────────────────────────
# Add "LEAD_VOICE" to CTRL_CC_MAP on whichever CC you assign to a dedicated
# lead-voice rotary. It uses RELATIVE_CC_MODE for decoding.
# Example: 5: "LEAD_VOICE"  (already in CTRL_CC_MAP below)

# ═══════════════════════════════════════════════════════════════════════════════
# CC MAP (CC on CH_CTRL)
# ═══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS (0-127):
# "VL_STRENGTH" voice leading strength
# "SPREAD" voicing crossfade (0=CLOSE ~64=OPEN 127=SPREAD)
# "OCT_OFFSET" octave offset (-2 to +2)
# "VEL_OVERRIDE" fixed velocity (0=use key velocity)
# "INV_SWEEP" sweep through all inversions continuously (rotary)
#
# LATCHED (>63=ON <=63=OFF):
# "KEY_LOCK" pull an off-key chord root onto the nearest in-key note, so every
#            pad yields an in-key chord (repurposed 2026-07-29 -- it used to
#            freeze the root, a job the root/chord_root split removed)
# "SCALE_SNAP" instrument-wide scale lock — pulls played notes into root+scale
# "HOLD" sustain chord
# "VOICING_OPEN" open voicing
# "VOICING_CLOSE" close voicing
# "VOICING_DROP2" drop 2 voicing
# "VL_ON" voice leading on
# "VL_OFF" voice leading off
# "MONO_MULTI" >63=MULTI <=63=MONO
# "ROOT_MODE" >63=ROOT <=63=CHORD
# "ARP_PASSTHROUGH" bypass chord engine, feed notes direct to arp
# "MIRROR_INPUT_CH" >63=mirror chord output to input channel (global, any preset)
# "MELODY_MODE" cycles OFF → SCALE → CHORD on each >63 trigger
#
# "SCALE_SELECT" continuous — sweeps the whole scale list (orc1.SCALE_ORDER).
#     For direct access to one named scale, send a PROGRAM CHANGE instead
#     (SCALE_PC_OFFSET + index).  The legacy ("SCALE", index) tuple form is
#     still honoured if you bind it, but nothing uses it by default.
# ═══════════════════════════════════════════════════════════════════════════════
CTRL_CC_MAP = {
    # Continuous knobs
    1: "VL_STRENGTH",
    2: "SPREAD",
    3: "OCT_OFFSET",
    4: "VEL_OVERRIDE",
    5: "LEAD_VOICE",  # relative rotary → leading voice (tone-by-tone voicing reshape)
                      # set to "INV_SWEEP" for simple 0-127 inversion sweep instead


    # Scale select — ONE continuous CC sweeps the whole scale list (value is
    # scaled across SCALE_ORDER, so a knob walks through every scale and the
    # mapping keeps working as scales are added).
    #
    # This replaced a block of ten one-CC-per-scale bindings (old CC 10-19,
    # 2026-07-28).  That cost 10 CCs to address 10 scales and would have cost 25
    # to address the current list; for pinpoint "jump straight to this scale"
    # use PROGRAM CHANGE instead — see SCALE_PC_OFFSET below.
    # CC 11-19 are now SPARE.
    10: "SCALE_SELECT",


    11: "SCALE_SNAP",   # instrument-wide scale lock (>63=ON)
    # CC 12-19 spare (freed 2026-07-28 from the old per-scale select block)

    # Latched toggles
    20: "KEY_LOCK",
    21: "HOLD",
    22: "VOICING_OPEN",
    23: "VOICING_CLOSE",
    24: "VOICING_DROP2",
    25: "VL_ON",
    26: "VL_OFF",
    27: "MONO_MULTI",
    28: "ROOT_MODE",
    29: "ARP_PASSTHROUGH",  # >63=bypass chord engine, arp notes directly
    84: "MIRROR_INPUT_CH", # >63=mirror all chord output to input channel
    # CC 30-83 spare (30+ ARP_CC_MAP; 85=MELODY_MODE below)
    # Melody mode cycles OFF→SCALE→CHORD on each >63 trigger
    # Assign to a momentary button for easy cycling
    85: "MELODY_MODE",
}


# ═══════════════════════════════════════════════════════════════════════════════
# ARPEGGIATOR CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
ARP_ENABLED_ON_BOOT = False
ARP_DEFAULT_PATTERN = "UP"
ARP_DEFAULT_RATE = "1/8"
ARP_DEFAULT_CLOCK = "internal"
ARP_DEFAULT_BPM = 120.0
ARP_DEFAULT_GATE = 1.0
ARP_DEFAULT_HUMANIZE = 0.0
ARP_DEFAULT_SWING = 0.0


ARP_CC_MAP = {
    30: "ARP_TOGGLE",
    31: "ARP_RATE",
    32: "ARP_CLOCK_MODE",
    33: "ARP_BPM",
    34: "ARP_DRIFT_BPM",
    35: "ARP_GATE",
    36: "ARP_HUMANIZE",
    37: "ARP_SWING",
    38: "ARP_PATTERN_PREV",
    39: "ARP_PATTERN_NEXT",
    40: "ARP_TAP",
    # ARP_HOLD — when value > 63 the arp latches (keeps running after note release).
    # When value < 64 the arp stops as soon as you release the triggering note.
    # Map to a toggle button on any controller to switch between latch and gate modes.
    41: "ARP_HOLD",
}


ARP_PC_OFFSET = 60

# ── Scale select via PROGRAM CHANGE (PC on CH_CTRL) ───────────────────────────
# PC (SCALE_PC_OFFSET + n) selects the n-th scale in orc1.SCALE_ORDER — one
# message, direct access, no CC burned per scale.  Sits in the free PC block
# below the preset map (40-56) and the arp patterns (60+), with room for the
# scale list to grow to ~40 before it would collide with presets.
SCALE_PC_OFFSET = 0

# ═══════════════════════════════════════════════════════════════════════════════
# SYNTH ENGINE CONFIG  (orc1_synth.py)
# ═══════════════════════════════════════════════════════════════════════════════
# CH_DRUM and CH_MONO must not conflict with NOTE_INPUT_CH (12), CH_CTRL (13),
# or MELODY_CH (15).
#
# CH_DRUM moved 6 -> 10 on 2026-07-30: **10 is the General MIDI drum channel**,
# which is what every other box on the rig assumes, and until this change a
# GM-convention drum note arriving on ch10 hit CH_CHIP_P2 and played the chip
# pulse2 voice instead. CH_CHIP_P2 took over the vacated ch6 -- a straight swap,
# no third channel moved. Anything that reads these from config follows
# automatically; SEQ.DEFAULT_TRACKS below was updated to match.
#
# ch10 is also in ALWAYS_PASSTHROUGH, and that stays true: that set is consulted
# ONLY by all_notes_off() (to avoid silencing external gear) and a startup
# print -- it is NOT part of routing dispatch, and ch10 was already claimed by
# the synth as CH_CHIP_P2, so the swap changes no routing semantics. The
# internal drum voices are one-shots and the synth's own CC120/CC123 panic
# covers the synth bus, so skipping ch10 there costs nothing.
#
# ch14 is owner's MPC control channel -- leave it alone.
SYNTH = {
    # MIDI channel for drum voice triggers (GM note mapping). 10 = GM standard.
    "CH_DRUM":        _CH_DRUM,
    # MIDI channel for mono synth notes + CC
    # MonoSynth is retired for good (buggy + redundant with the chip voices'
    # monophonic waveforms). CH_MONO is reserved/silent, NOT an alias for
    # CH_POLY -- see orc1_synth.py's _route_midi, where it is intentionally
    # unhandled. It coincides with CH_SAMPLER's default (7), so a note sent
    # here is consumed by the sampler's action-slot matcher, not PolySynth.
    "CH_MONO":         _CH_MONO,
    "CH_POLY":         _CH_POLY,
    # MIDI channel for the NES-style chip voice (2 pulse + triangle + noise).
    # Point a sequencer track here (trkchan knob) or play it from a mapped knob.
    "CH_CHIP":         _CH_CHIP,    # NES Pulse 1
    "CH_CHIP_P2":      _CH_CHIP_P2,    # NES Pulse 2 — took ch6 when drums moved to GM ch10
    "CH_CHIP_TRI":    _CH_CHIP_TRI,    # NES Triangle  (no amplitude envelope — authentic APU)
    "CH_CHIP_NS":     _CH_CHIP_NS,     # NES Noise — CH4 is clean and clear of everything
    # Game Boy wave channel (programmable 32-sample 4-bit wavetable).
    # CH5 is free and clear — nothing else uses it.
    # This is the voice behind the Tetris bass, Link's Awakening bass,
    # and countless other classic GB melodies.
    "CH_GB_WAVE":     _CH_GB_WAVE,
    "CH_CHIP_PCM":    _CH_CHIP_PCM,    # NES PCM       (16 = well clear of ctrl/chord/melody)
    # Pitch-bend range for the chip voices, in SEMITONES (± range).
    # A MIDI pitchwheel message on the Pulse 1 / Pulse 2 / GB Wave channels
    # bends the note by up to this many semitones in either direction.
    # 12 = a full octave each way (classic chiptune slides); 2 = subtle,
    # guitar-style bends. Web-overridable on /system (restart synth to apply).
    "CHIP_BEND_RANGE": 12,

    # MIDI channel for live sequencer recording.
    # Notes on this channel are written into the record track as the sequencer runs.
    # 0 = disabled. Any non-zero channel works; pick one that isn't used for anything else.
    # The note still goes wherever it normally goes — recording is additive.
    # Example: set to 2, play a keyboard on CH2 → your melody goes into the sequencer.
    "SEQ_RECORD_CH":  0,
    # JACK client name (shows in JACK patchbay)
    "JACK_NAME":       "ORC1-Synth",
    # Auto-connect to system playback ports on startup
    "JACK_AUTOCONNECT": True,
    # Master output level (0.0-1.0). Headroom for inter-voice mixing.
    # Lowered from 0.75 to 0.60 to stop clipping pops when bass-heavy voices
    # (909 kick + bass synth) land on the same step. Raise downstream to taste.
    "MASTER_LEVEL":    0.60,
    # Sub oscillator HPF cutoff in Hz.
    # Filters infrasonic content that would clip inputs and can't be
    # reproduced by speakers anyway. 40Hz is safe; go lower if you want
    # more sub extension and your downstream gear can handle it.
    "SUB_HPF_HZ":      40.0,
    # MIDI port prefix for the synth's own MIDI input thread.
    # Empty string = use first available port. Set to e.g. "pisound"
    # to listen on the DIN input, or leave empty to share the BSP port.
    # Internal MIDI bus name — must match INTERNAL_BUS_NAME above.
    # orc1.py creates a virtual port with this name; orc1_synth connects to it.
    "MIDI_PORT_PREFIX": "ORC1-Synth-Bus",
    "INTERNAL_BUS_NAME": "ORC1-Synth-Bus",
    # Override any drum note mapping here (merges with GM defaults).
    # e.g. "DRUM_NOTE_MAP": {36: "bass_drum", 60: "snare"} remaps middle C to snare
    "DRUM_NOTE_MAP":   {},

    # (BUS_OUTPUTS + MAIN_MIX_BUSES removed 2026-09-24: they configured the
    #  retired single-process synth's per-bus direct outs / main-mix sum. Each
    #  module host now has its own out_L/out_R -- see MODULE OUTPUTS on /patch.)

}


# ═══════════════════════════════════════════════════════════════════════════════
# FX CHAIN PORTS  (fx_chain_manager.py / fx_chains.json)
# ═══════════════════════════════════════════════════════════════════════════════
# The FX chains route audio through an external send/return loop (an insert on
# your audio interface or mixer).  Set SEND_PORTS to the JACK ports that carry
# the pre-FX audio, and RETURN_PORTS to where the processed audio should land.
#
# Tokens used in fx_chains.json:
#   FX_SEND_L / FX_SEND_R    — resolved to SEND_PORTS[0] / [1]
#   FX_RETURN_L / FX_RETURN_R — resolved to RETURN_PORTS[0] / [1]
#
# Defaults: pisound hardware insert (the historical setup on this Pi).
# Change both lists to match your own interface — any JACK port pair works.
FX = {
    "SEND_PORTS":   ["pisound-in:capture_1",  "pisound-in:capture_2"],
    "RETURN_PORTS": ["pisound-out:playback_1", "pisound-out:playback_2"],
}


# ═══════════════════════════════════════════════════════════════════════════════
# DRUM CC MAP — single source of truth
# ═══════════════════════════════════════════════════════════════════════════════
# The synth (orc1_synth.py) DECODES this map; controller handlers (orc1_fire.py,
# orc1_mixtrack.py) ENCODE it. Before consolidation each file carried its own
# copy — edit HERE and both sides stay in sync.
DRUM_CC = {
    "BASE": 20,          # first main-param CC on CH_DRUM
    # ── DATA-DRIVEN PER-VOICE ALLOCATION (2026-07-21 redesign) ───────────────
    # Each voice declares ONLY the params it actually uses, in the order they
    # occupy CCs. The CC map is COMPUTED below (_build_drum_cc_layout) by packing
    # these in voice order starting at BASE — a voice owns exactly len(params)
    # consecutive CCs, no reserved slack.
    #
    # This replaced the old rigid 8-CC/voice stride (6 main + 2 HP). Two things
    # changed by owner's call:
    #   • The HP band is GONE. Every voice had 2 reserved high-pass CCs that were
    #     bypassed-by-default and virtually never dialed — 20 CCs of dead space.
    #     The post-HP filter object still exists in DrumVoice (a no-op while
    #     bypassed) but is no longer addressable, so "the HP can go" holds.
    #   • Sample slots MOVED OUT. smpl_0..3 used to masquerade as drum voices
    #     here; they now live in the sampler section (SAMPLER["ONESHOT_*"]) — see
    #     that block. They are no longer drum voices at all.
    # Net effect: cowbell finally gets its real filter_freq/filter_res (the whole
    # reason this started), toms drop their unused filter slots, and ~half the
    # channel is now free for future voices.
    #
    # To add a param to a voice: append it to that voice's list. To add a voice:
    # add an entry. Synth (decode), Fire (encode) and the web all read the
    # COMPUTED maps below, so the three can never drift out of sync.
    "VOICE_PARAMS": {
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
        # One-shot sample slots MOVED BACK to the drum channel 2026-09-02 (audio
        # host split): smpl_0..3 are drum voices again, packed AFTER cowbell so
        # they land in previously-free CC space (bass_drum..cowbell fill 20..78;
        # these take 79..102) with zero collision -- the 2026-07-21 relocation to
        # CH_SAMPLER (done for CC room, before the HP band was reclaimed) is no
        # longer needed. Triggered at DRUM_NOTE_MAP notes 60..63; the /sampler
        # one-shot card reads this layout via /api/oneshot. The sampler channel
        # is now purely the live-remix decks (their own process).
        "smpl_0":     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "smpl_1":     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "smpl_2":     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        "smpl_3":     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],
        # RIDE (2026-09-23): the 909's missing 11th voice, a sampled ride like
        # the crash. APPENDED LAST so no existing voice's CCs move (outboard/MPC
        # mappings stay valid): it lands on CCs 103-106. No filter params -- the
        # sample-voice low-pass is unstable at high cutoffs (see Cymbal).
        "ride":       ["level", "tune", "attack", "decay"],
        # More kit voices (2026-09-23, owner: have the voices rather than fold),
        # also appended LAST so nothing moves: ride bell 107-110, splash 111-114,
        # hi-mid tom 115-118 (the channel's last free main-param CCs before 120).
        "ride_bell":  ["level", "tune", "attack", "decay"],
        "splash":     ["level", "tune", "attack", "decay"],
        "himid_tom":  ["level", "tune", "attack", "decay"],
    },
    # Which drum voices are exposed for selection/sculpting, in display order.
    # Controller-agnostic (moved out of the FIRE dict 2026-07-21) — the Fire
    # drum editor, the web /drums page, and any future controller all read this.
    "EDIT_VOICES": ["bass_drum", "snare", "closed_hat", "open_hat", "clap",
                    "rimshot", "low_tom", "mid_tom", "himid_tom", "hi_tom",
                    "cymbal", "splash", "ride", "ride_bell", "cowbell"],
}


def _build_drum_cc_layout(dcc):
    """Compute the drum CC map from VOICE_PARAMS by packing each voice's params
    into consecutive CCs starting at BASE (packing order = VOICE_PARAMS insertion
    order, which is stable in Python 3.7+). Returns three views so every consumer
    reads the same single source of truth:
      LAYOUT      {voice: {"start": first_cc, "params": [...]}}   — encode (Fire, web)
      CC_TO_VP    {cc: (voice, param)}                            — decode (synth)
      VOICE_OFFSET{voice: first_cc - BASE}                        — backward-compat alias
    """
    base = int(dcc.get("BASE", 20))
    layout, cc_to_vp, voice_off = {}, {}, {}
    cc = base
    for voice, params in dcc.get("VOICE_PARAMS", {}).items():
        layout[voice]     = {"start": cc, "params": list(params)}
        voice_off[voice]  = cc - base
        for p in params:
            cc_to_vp[cc] = (voice, p)
            cc += 1
    return layout, cc_to_vp, voice_off


(DRUM_CC["LAYOUT"],
 DRUM_CC["CC_TO_VP"],
 DRUM_CC["VOICE_OFFSET"]) = _build_drum_cc_layout(DRUM_CC)
# Flat param-name list (any order), kept for consumers that just want "is this a
# known drum param?" — the authoritative per-voice ordering lives in LAYOUT.
DRUM_CC["PARAMS"] = ["level", "tune", "attack", "decay", "filter_freq", "filter_res"]


# ═══════════════════════════════════════════════════════════════════════════════
# SEQUENCER DEFAULTS  (controller-agnostic)
# ═══════════════════════════════════════════════════════════════════════════════
# The default track list + colour palette the step sequencer boots with when
# there's no saved pattern. Moved out of the FIRE dict 2026-07-21 — the seq is
# headless/controller-independent, so its defaults shouldn't live under a
# controller. The Fire, the web /seq page, and orc1_seq.py all read from here.
SEQ = {
    # One track per built-in drum voice (all on CH_DRUM = 10, GM). Scroll with
    # PATTERN +/- on the Fire. Most are 16 steps; the closed hat defaults to 12
    # for an instant polyrhythm. Set any length live with ALT+pad. Point a
    # track outboard with "dest": {"port": "pisound", "ch": 10} to fire the MPC.
    "DEFAULT_TRACKS": [
        {"name": "kick",  "note": 36, "ch": 10, "vel": 115, "length": 16},
        {"name": "snare", "note": 38, "ch": 10, "vel": 110, "length": 16},
        {"name": "chat",  "note": 42, "ch": 10, "vel": 95,  "length": 12},
        {"name": "ohat",  "note": 46, "ch": 10, "vel": 95,  "length": 16},
        {"name": "clap",  "note": 39, "ch": 10, "vel": 105, "length": 16},
        {"name": "rim",   "note": 37, "ch": 10, "vel": 100, "length": 16},
        {"name": "ltom",  "note": 41, "ch": 10, "vel": 100, "length": 16},
        {"name": "mtom",  "note": 43, "ch": 10, "vel": 100, "length": 16},
        {"name": "htom",  "note": 50, "ch": 10, "vel": 100, "length": 16},
        {"name": "cym",   "note": 49, "ch": 10, "vel": 90,  "length": 16},
        {"name": "cowb",  "note": 56, "ch": 10, "vel": 100, "length": 16},
    ],
    # Per-step velocity tiers [ghost, normal, accent]. SHIFT-tapping an ON step
    # on the Fire cycles that step through None(=track default) -> ghost ->
    # normal -> accent -> None. Pad brightness tracks the assigned velocity, so
    # ghost steps read dim and accent steps read bright. Values are MIDI
    # velocities 1-127; edit to taste. Replaces the old binary accent (+20 vel).
    "VEL_TIERS": [60, 95, 127],
    # Track colour palette. Each track gets one at creation; the colour travels
    # with the track when reordered. [R, G, B], each channel 0-127 (Fire range).
    # Override a single track's colour with "color" in DEFAULT_TRACKS. Tracks
    # cycle through the list; add entries to extend it.
    "TRACK_COLORS": [
        [0x50, 0x00, 0x00],   # 0  red        — kick
        [0x00, 0x48, 0x00],   # 1  green       — snare
        [0x00, 0x00, 0x50],   # 2  blue        — closed hat
        [0x50, 0x28, 0x00],   # 3  orange      — clap
        [0x38, 0x00, 0x38],   # 4  purple      — open hat
        [0x00, 0x38, 0x38],   # 5  teal        — ride/cymbal
        [0x40, 0x40, 0x00],   # 6  yellow      — tom
        [0x00, 0x30, 0x18],   # 7  dark green  — bass
        [0x28, 0x10, 0x00],   # 8  brown       — perc
        [0x18, 0x18, 0x40],   # 9  indigo      — melodic
        [0x40, 0x00, 0x20],   # 10 crimson     — synth lead
        [0x10, 0x30, 0x10],   # 11 sage        — pad
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLER — live-remix sample decks (controller-agnostic)
# ═══════════════════════════════════════════════════════════════════════════════
# The sampler taps the skipback ring buffer READ-ONLY and plays captured audio
# back through the synth. skipback itself is never modified. Any front-end
# (Mixtrack, web page, other controllers) drives the SAME engine via CH_SAMPLER.
SAMPLER = {
    # MIDI channel the synth listens on for sampler control. Reuses the
    # reserved/silent CH_MONO (7) — already routed to the internal synth bus,
    # so nothing in the routing layer needs to change.
    "CH_SAMPLER":              7,
    "RING_PATH":               os.path.expanduser("~/.skipbuf/ring.raw"),
    "RING_POS_PATH":           os.path.expanduser("~/.skipbuf/ring.pos"),
    # RING_SECONDS / RING_CHANNELS are STARTUP FALLBACKS ONLY (2026-07-29).
    # The real ring is sized per-track by the recorder from
    # ~/.orc1_skipback_tracks.json (track 0 = 1200s today, not 1860), so
    # RingBufferReader re-resolves size from os.stat and channels from that
    # same tracks file on every capture. These values only apply in the window
    # before the first successful resolve (e.g. the reader was built before
    # ring.raw existed). Do NOT "fix" 1860 to 1200 and consider the geometry
    # problem solved -- editing one number here just moves which track length
    # happens to be wrong.
    "RING_SECONDS":            1860,
    "RING_RATE":               48000,
    "RING_CHANNELS":           2,
    # Where the reader reads per-track channel count from. Kept as a config
    # key (not an import) because orc1_sampler.py is contractually
    # NumPy + stdlib only and must never import orc1_config.
    "RING_TRACKS_PATH":        os.path.expanduser("~/.orc1_skipback_tracks.json"),
    # The recorder's published status. Read by the brain's bar anchor for the
    # live ring modulus (`ring_bytes` per track) -- the recorder is the
    # authority on the ring's real geometry, so the anchor asks it rather than
    # recomputing from RING_SECONDS/RING_CHANNELS above.
    "RING_STATUS_PATH":        os.path.expanduser("~/.orc1_skipback_status.json"),
    "DEFAULT_CAPTURE_SECONDS": 8,
    "MAX_CAPTURE_SECONDS":     60,   # raised from 25 for bar captures (8 bars @ 60bpm = 32s)
    "CLICK_FADE_MS":           4,
    # Jog "record feel" sensitivity — TWO knobs, because the two gestures need
    # very different gains (see the long note in orc1_sampler.SampleDeck):
    #   JOG_SENSITIVITY         = FREE-SPIN (no finger on the platter top). The
    #     record is playing normally and friction pulls it back toward normal
    #     speed, so the jog only BENDS the pitch while you keep spinning — this
    #     needs a HIGH gain or the bend is imperceptible. (This is the "mess with
    #     the recording by hand" feel.)
    #   JOG_SCRATCH_SENSITIVITY = SCRATCH (finger down). Rest speed is a dead
    #     stop, so the same movement reads dramatically — wants a LOWER gain.
    # Tune each to taste (~0.1 calm .. ~1.0 hot). History (2026-07-28): started
    # as one knob at 1.0 (way too twitchy) → 0.25 → 0.15 (killed the free-spin
    # bend) → split into two.
    "JOG_SENSITIVITY":          0.08,   # free-spin pitch-bend (no touch) — 0.3 too fast/runaway
    "JOG_SCRATCH_SENSITIVITY":  0.05,   # scratch (finger on platter) — 0.12 too twitchy, 0.04 slightly dead
    # Capture normalization (added 2026-07-05): the ring records the mixer's
    # USB master return, whose level depends on the mixer's fader positions
    # (measured ~-35 dB on the L12 — raw captures played back inaudibly).
    # Each capture is peak-normalized so playback level doesn't depend on
    # the interface's return level. Near-silent grabs are left un-boosted.
    "CAPTURE_NORMALIZE":       True,
    "CAPTURE_TARGET_PEAK":     0.9,    # normalize to this peak (0..1)
    "CAPTURE_MAX_GAIN":        100.0,  # gain ceiling (=+40 dB)
    "CAPTURE_SILENCE_PEAK":    1e-4,   # below this peak: leave as silence
    # (Per-deck chord/sampler mode removed 2026-08-01 -- both Mixtrack decks
    # are sampler decks and PAD_MODE is a deliberate no-op.)

    # Engine↔synth MIDI vocabulary on CH_SAMPLER — single source of truth.
    # The facade (orc1.py sampler_*) ENCODES these; the synth DECODES them.
    # Front-ends never see this — they only call the facade.
    "NOTE_CAPTURE": {"A": 1,  "B": 2},    # note-on; velocity = seconds
    "NOTE_GATE":    {"A": 3,  "B": 4},    # note-on = play, note-off = stop
    "NOTE_LOOP_TOGGLE": {"A": 5, "B": 6}, # latch play/stop (live looping)
    "NOTE_AUDITION": {"A": 23, "B": 24},  # note-on = one-shot play-through (web PLAY button)
    "NOTE_EXPORT":  {"A": 7, "B": 8},     # save deck capture -> samples/ as WAV
    "NOTE_STOP":    {"A": 9,  "B": 10},   # hard stop + rewind (obvious STOP)
    "NOTE_CUE":     {"A": 11, "B": 12},   # hotcue jump; velocity 1..4 = cue point
    "NOTE_SCRATCH": {"A": 13, "B": 14},   # note-on/off = vinyl scratch on/off
    "NOTE_REVERSE": {"A": 21, "B": 22},   # note-on = toggle backwards playback
    "CC_SCRUB":     {"A": 16, "B": 17},   # relative, offset-binary (jog)
    "CC_SPEED":     {"A": 18, "B": 19},   # 0-127 -> 0.25x..4x
    "CC_GAIN":      {"A": 20, "B": 21},   # 0-127 -> 0..1.5
    "CC_CROSSFADE": 22,                   # 0-127 -> 0..1
    # Bar-accurate capture length: 14-bit deciseconds over two CCs.
    # MSB is held pending; LSB completes the value AND fires the capture.
    "CC_CAP_LEN_MSB": {"A": 23, "B": 24},
    "CC_CAP_LEN_LSB": {"A": 25, "B": 26},

    # Bar math for ("sample_capture_bars", ...) — beats per bar (4/4 assumed).
    "BEATS_PER_BAR":  4,

    # 3-band EQ (Mixtrack EQ_HI/EQ_MID/EQ_LOW knobs) + one-knob sweep filter
    # (Mixtrack FILTER knob). Raw CC 0-127 passthrough, same "engine encodes,
    # synth decodes" split as everything else above — orc1_synth.py turns
    # these into +/-15dB shelf/peak gains (EQ) or a -1..+1 LP<->HP sweep
    # position (filter); 64 = flat/bypass on all four.
    "CC_EQ_HI":     {"A": 27, "B": 28},
    "CC_EQ_MID":    {"A": 29, "B": 30},
    "CC_EQ_LOW":    {"A": 31, "B": 32},
    "CC_FILTER":    {"A": 33, "B": 34},
    "CC_MASTER_GAIN": 35,                 # single CC -> master sampler output level

    # ── Play/loop region (start + end markers, 2026-08-10) ────────────────────
    # ONE region per deck serves as BOTH the trim (PLAY/one-shot runs
    # start->end) and the loop (LOOP wraps start->end). Each CC is a 0..127
    # absolute position = fraction of the buffer for that edge, decoded in
    # _route_sampler -> set_bounds_start/end. Sits at CC 80..83, clear of the
    # deck controls (<=55) and the one-shot bank (56..79). Exposed so a
    # controller knob can be mapped to it later; the /sampler waveform drag
    # sends these same CCs via SAMPLER_BOUNDS.
    "CC_START":     {"A": 80, "B": 81},
    "CC_END":       {"A": 82, "B": 83},

    # ── Momentary BEAT-REPEAT (stutter) ──────────────────────────────────────
    # Hold = squeeze the play/loop region to a tight loop AT the live playhead
    # (`length` frames long); release = restore the prior region. The brain
    # computes `length` from the live tempo + ring rate (bpm+sr only -- the
    # synth owns the buffer) and sends it as a 28-bit FRAMES value over four
    # CCs big-endian; the value is held PENDING (unlike capture, the LSB does
    # NOT fire). NOTE_BEAT_REPEAT on = start the stutter with the pending
    # length, off = stop. CC 85..92 sit clear of the region CCs (80..83), the
    # press-latency CC (84) and the one-shot bank (56..79); notes 25/26 are
    # clear of every deck-control note (1..24) and the one-shot bank (60..63).
    "NOTE_BEAT_REPEAT": {"A": 25, "B": 26},
    "CC_BR_LEN_B3":     {"A": 85, "B": 89},
    "CC_BR_LEN_B2":     {"A": 86, "B": 90},
    "CC_BR_LEN_B1":     {"A": 87, "B": 91},
    "CC_BR_LEN_B0":     {"A": 88, "B": 92},

    # ── ONE-SHOT SAMPLE BANK (relocated from the drum channel 2026-07-21) ─────
    # The generic sample-playback slots (smpl_0..3) used to masquerade as drum
    # voices on CH_DRUM. owner's call: sample playback belongs with the sampler
    # section, not the drum kit. They now live HERE — triggered on CH_SAMPLER by
    # ONESHOT_NOTE_BASE..+SLOTS-1 (notes 60..63 — the SAME numbers they always
    # used, just on the sampler channel now, so anything that "called" them
    # (sequencer track, external MIDI, a /sampler button) still can) and sculpted
    # via a dedicated CC block starting at ONESHOT_CC_BASE, clear of the deck
    # controls above (which end at CC 55). Files load from the synth's samples/
    # dir (cymbal.wav excluded — that's the drum-kit Cymbal voice). Addressed on
    # the /sampler web page. The synth REUSES the same SampleVoice DSP + the
    # drum_trigger/drum_cc enqueue path — only the control surface (channel,
    # notes, CCs, UI) moved, so the sound is byte-identical to before.
    "ONESHOT_SLOTS":     4,
    "ONESHOT_NOTE_BASE": 60,              # notes 60..63 on CH_SAMPLER
    "ONESHOT_CC_BASE":   56,              # 6 params/slot × 4 slots -> CC 56..79
    "ONESHOT_PARAMS":    ["level", "tune", "attack", "decay", "filter_freq", "filter_res"],

    # ── Capture timing overhaul (2026-07-07) ─────────────────────────────────
    # Constant delay between "you heard it" and "it landed in the ring"
    # (synth -> skipback-bridge alsa_out (2x1024 frames) -> ALSA loopback ->
    # arecord -> 8192-byte pipe chunks). The capture math shifts every grab
    # window LATER by this much (ring content lags heard audio). MEASURED
    # 2026-07-07 on the Pi via the kick-alignment test (4-on-floor kicks,
    # quantized 1-bar grabs, onset positions in the captured loop): 55ms
    # read ~20ms early, 30ms read ~20ms late, 42ms centers the onsets on
    # the grid within the test's ~10ms resolution (residual jitter is the
    # clock anchor + timer wakeups, not a constant). Re-measure if the
    # bridge period size or the skipback chunk size ever changes.
    "CAPTURE_LATENCY_MS":   -99,
    # Latency constant for AUDIO-ANCHORED grabs (2026-07-20 Bug-1 fix): when
    # "time since the barline" is measured in ring bytes (the downbeat ring-
    # head marks) instead of the software clock, the head-estimate bias that
    # CAPTURE_LATENCY_MS compensates cancels out of the offset — applying the
    # full -99 then pulled every anchored window ~95ms early (measured, kick
    # test). Same semantics as CAPTURE_LATENCY_MS but for the anchored path;
    # the engine shifts the sent offset by (CAPTURE - ANCHOR) so the synth's
    # single subtraction nets to the right constant per path. MEASURED
    # 2026-07-20: kicks at ~95ms early with 0 correction -> -9 centers them
    # ~5ms into the loop (the validated pre-drift placement).
    "ANCHOR_LATENCY_MS":    -9,
    # ── Capture anchor (2026-08-18) ──────────────────────────────────────────
    # Where a bar-grab's window ENDS:
    #   "press" (default) = at the player's PRESS, reaching back N bars. The
    #                       press IS the downbeat. The ORC's own bar grid has
    #                       no fixed relation to the music's downbeat, so
    #                       grid-quantized grabs land on an arbitrary phase
    #                       ("don't know what segment it grabs") — press-anchor
    #                       fixes that, symmetric with forward capture.
    #   "grid"            = the old barline-quantized behaviour below
    #                       (QUANTIZE_CAPTURE / SNAP / MARGIN).
    "CAPTURE_ANCHOR":       "press",
    # Latency for the PRESS/end-now path (offset_ms == 0), SEPARATE from the
    # grid path's CAPTURE_LATENCY_MS. 0 = end on the newest recorded sample;
    # NEGATIVE reaches further back. Source-dependent (SYNTH DIRECT vs an L12
    # round-trip differ), so it is LIVE-TUNABLE by ear via CC_PRESS_LATENCY /
    # the SAMPLER_PRESS_TRIM cmd and PERSISTED to PRESS_TRIM_PATH.
    "PRESS_CAPTURE_LATENCY_MS": 0,
    "CC_PRESS_LATENCY":     84,     # CH_SAMPLER CC, 0..127 -> -127..0 ms
    "PRESS_TRIM_PATH":      os.path.expanduser("~/.orc1_sampler_press_trim.json"),
    # PER-SOURCE press trim (2026-09-15): the trim above is now kept PER capture
    # SOURCE (one number can't null two paths' round-trips at once). The source
    # key is track 0's live `resolved` ports, read from this status file.
    "SKIPBACK_STATUS_PATH": os.path.expanduser("~/.orc1_skipback_status.json"),
    # ONSET-SNAP (2026-09-15): after a PRESS grab, snap the loop start to the
    # nearest strong transient within +/- ONSET_SNAP_MS (computes the downbeat
    # from the audio, not the finger). SENSITIVITY = the rise a real transient
    # must clear, as a fraction of the loudest frame; higher = fussier. A grab
    # with no clear transient near the start is left exactly press-anchored.
    "ONSET_SNAP":             True,
    "ONSET_SNAP_MS":          120.0,
    "ONSET_SNAP_SENSITIVITY": 0.35,
    "CC_ONSET_SNAP":          95,   # CH_SAMPLER CC, >=64 = on, <64 = off
    # PER-DECK GRAB SOURCE (2026-09-15): each deck grabs from its OWN skipback
    # ring, so deck A can sample the MAIN master (track 0) while deck B samples a
    # different stream (track 1 = a mono aux) at the same time. The CC value is
    # the skipback TRACK ID (0-63) that deck grabs from. Track 0's ring is the
    # primary reader (the legacy ~/.skipbuf/ring.raw symlink); others resolve
    # ~/.skipbuf/t<id>/ring.{raw,pos} and self-resolve mono/stereo from the tracks
    # JSON. Defaults: deck A -> t0 (MAIN), deck B -> t1. Persisted to DECK_SRC_PATH
    # so a deck keeps its stream across a restart.
    "CC_DECK_SOURCE":   {"A": 96, "B": 97},  # CH_SAMPLER CC, value = track id 0-63
    "DECK_SRC_DEFAULT": {"A": 0, "B": 1},    # deck -> skipback track id at boot
    "DECK_SRC_PATH":    os.path.expanduser("~/.orc1_sampler_deck_src.json"),
    "SKIPBUF_ROOT":     os.path.expanduser("~/.skipbuf"),
    # Loop-seam declick: the last N ms of a loop crossfades into audio that
    # CONTINUES past the window end, so the wrap point is seamless instead
    # of a hard splice that clicks on every pass.
    "SEAM_FADE_MS":         15,
    # Bar-quantized grabs: the capture window ends on the nearest BARLINE of
    # the live clock (sequencer grid when running, else the arp PPQN grid)
    # instead of wherever the button press happened to land. SNAP = pressed
    # within this fraction of a bar BEFORE the next barline, the grab waits
    # for (and uses) that barline — a musician anticipating the downbeat
    # means the bar that's about to end. MARGIN_MS = grabs fire at least
    # this long after their target barline so the barline's audio has
    # actually reached the ring (must cover CAPTURE_LATENCY_MS + one ring
    # chunk ~43ms with headroom).
    "QUANTIZE_CAPTURE":     True,
    "QUANTIZE_SNAP":        0.15,
    "QUANTIZE_MARGIN_MS":   350,

    # What a freshly grabbed loop does the instant it lands:
    #   False (default) = load CUED — the deck sits stopped at the loop
    #                     start, armed but silent, until you press PLAY/LOOP.
    #   True            = auto-arm — the loop starts looping immediately,
    #                     phase-aligned to the live grid (the old behavior;
    #                     handy for hands-free live looping from a hardware
    #                     controller like the Mixtrack hotcue pads).
    # Changed to cued 2026-07-19: with no Mixtrack connected, auto-arm just
    # made every web bar-grab blast a loop over everything. Applies to ALL
    # capture paths (web + controller); restart orc1-synth to take effect.
    "CAPTURE_AUTO_ARM":     False,

    # Frame-exact capture vocabulary: 28-bit values over four 7-bit CCs,
    # big-endian (B0 = LSB). The decisecond MSB/LSB pair above rounds loop
    # length to 100ms — up to ~50ms of drift against the sequencer on every
    # pass; frames are exact. CC_CAP_OFF_* = window END OFFSET in
    # MILLISECONDS before "now" (how long ago the window should end — the
    # quantizer sets this to "time since the target barline"); held pending.
    # CC_CAP_FRA_B0 completes the length AND fires the capture, consuming
    # any pending offset (no pending offset = 0 = end at the ring head,
    # the old immediate behavior).
    "CC_CAP_FRA_B3": {"A": 36, "B": 40},
    "CC_CAP_FRA_B2": {"A": 37, "B": 41},
    "CC_CAP_FRA_B1": {"A": 38, "B": 42},
    "CC_CAP_FRA_B0": {"A": 39, "B": 43},
    "CC_CAP_OFF_B3": {"A": 44, "B": 48},
    "CC_CAP_OFF_B2": {"A": 45, "B": 49},
    "CC_CAP_OFF_B1": {"A": 46, "B": 50},
    "CC_CAP_OFF_B0": {"A": 47, "B": 51},
    # Post-capture nudge: shift a deck's capture WINDOW back/forward in the
    # ring and re-slice it (the ring holds 31 minutes — the audio to fix a
    # slightly-off grab is still in there). Offset-binary relative CC; one
    # tick = 10ms of window shift, positive = later (toward "now").
    "CC_NUDGE":      {"A": 52, "B": 53},

    # ── File decks (2026-07-07): load recordings onto a deck ─────────────────
    # Any audio file sox can read (wav/mp3/flac/ogg/aiff) is decoded to 48k
    # mono on a worker thread and loaded CUED (stopped at position 0, loop
    # on) — the deck then plays/scratches/EQs it like any capture, and since
    # the ring hears the synth (SYNTH DIRECT), bar-grabbing a playing file
    # into the other deck IS live resampling. Paths can't travel over the
    # MIDI vocab, so the brain writes the validated path to a per-deck JSON
    # file and fires NOTE_LOAD_FILE; the synth reads the file and loads.
    "NOTE_LOAD_FILE": {"A": 15, "B": 16},
    "FILE_LOAD_PATH": os.path.expanduser("~/.orc1_sampler_load_{deck}.json"),
    # Where the file browser looks (and the only roots loads are allowed
    # from). Recordings = skipback saves; samples = the synth's kit folder —
    # drop any of your own recordings into either and they're loadable.
    "FILE_ROOTS": [os.path.expanduser("~/recordings"),
                   os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "samples")],
    # Cap on decoded length (600s mono float32 ≈ 115MB — fine on the Pi 4;
    # longer files are truncated, not rejected).
    "LOAD_MAX_SECONDS": 600,

    # ── Deck persistence across reboots / synth restarts ─────────────────────
    # True = whatever is loaded in each deck is written to a fixed mono 16-bit
    # WAV (DECK_PERSIST_DIR/.orc1_sampler_deck_<A|B>.wav) on every grab / file
    # load / nudge, and reloaded CUED (stopped at 0, loop-armed, silent) when
    # the SamplerManager rebuilds. Turn off to have decks always boot empty.
    "PERSIST_DECKS":    True,
    "DECK_PERSIST_DIR": "~",   # $HOME (runtime/user state, not repo source)

    # ── Top-section controls (2026-07-07, Mixtrack top row mapped) ───────────
    # Needle-drop seek: absolute strip position 0-127 -> jump to that
    # fraction of the deck's buffer and play (declicked in the deck).
    "CC_SEEK":       {"A": 54, "B": 55},
    # Momentary beatmatch nudge (PITCH +/- buttons): note-on = bend engaged,
    # note-off = return to the pre-bend speed. Classic DJ pitch-bend.
    "NOTE_BEND_UP":  {"A": 17, "B": 18},
    "NOTE_BEND_DN":  {"A": 19, "B": 20},
    "BEND_FACTOR":   1.04,     # +/-4% while held
    # Pitch fader range: center = exactly 1.0x, ends = +/- this many octaves
    # (1.0 => 0.5x..2x). Drop to ~0.12 for a tight DJ-style +/-8% fader.
    "SPEED_RANGE_OCTAVES": 1.0,
}


# ═══════════════════════════════════════════════════════════════════════════════
# MIXTRACK PRO 3 — CONTROL ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════════════════════
# Edit these dicts to remap what each Mixtrack control does. Restart orc1.py
# to apply. The hardware mapping (which CC/note number each control sends) is
# fixed by the Mixtrack itself and is NOT changeable here — those constants
# live in orc1_mixtrack.py as NOTE_* and CC_*.
#
# Dict keys are symbolic names (resolved by orc1_mixtrack.py to MIDI numbers).
# Dict values are action tuples: (action_name, *args).
# Set any entry to None — or just delete it — to disable that control.
# Controls not listed are silently consumed; nothing ever leaks downstream.
#
# ───────────────────────────────────────────────────────────────────────────────
# AVAILABLE BUTTON ACTIONS  (use in MIXTRACK_*_BUTTONS dicts)
# ───────────────────────────────────────────────────────────────────────────────
# ⚠ THIS LIST IS ENFORCED. _test_mixtrack.py::test_documented_actions_exist
# parses it and fails if a name here has no matching _act_/_cc_ method. It went
# stale once already -- after chord mode was deleted 2026-08-01 it still listed
# ~25 removed actions, and because DEBUG is False by default, wiring one into a
# map produces a control that does NOTHING with no error anywhere. Keep it true.
#
#   ("sample_capture", "A"|"B")                 grab last DEFAULT_CAPTURE_SECONDS
#   ("sample_capture", "A"|"B", "12")           grab last 12 s (any 1-60)
#   ("sample_capture_bars", "A"|"B", "4")       grab last N bars at the live BPM
#                                               (external clock or internal), loop
#   ("sample_loop_toggle", "A"|"B")             latch play/stop (live looping)
#   ("sample_stop", "A"|"B")                    hard stop + rewind
#   ("sample_gate", "A"|"B")                    hold to play (momentary)
#   ("sample_cue", "A"|"B", "1".."4")           settable hotcue: press empty=SET
#                                               at playhead, press set=JUMP+play,
#                                               SHIFT+press=clear
#   ("sample_reverse", "A"|"B")                 latch backwards playback
#   ("sample_pitch_bend", "A"|"B", "up"|"down") momentary beatmatch nudge
#   ("sample_export", "A"|"B")                  save deck capture -> samples/ WAV
#   ("sample_load_browsed", "A"|"B")            load the browsed file onto a deck
#   ("fx_toggle_slot", "0"|"1"|"2")             toggle an FX rack slot on/off
#   ("ring_button", "A"|"B", "1".."3")         point the deck's GRAB at the n-th
#                                               armed skipback ring (lit = current);
#                                               SHIFT+press = save that ring's last
#                                               MIXTRACK_RING_SAVE_SECS to ~/recordings
#   ("tap_tempo",)                              tap tempo
#   ("mixtrack_shift",)                         Mixtrack-local SHIFT modifier
#   ("jog_touch",)                              capacitive platter -> scratch
#   ("consume",)                                silently consume (explicit no-op)
#
# ───────────────────────────────────────────────────────────────────────────────
# AVAILABLE CC ACTIONS  (use in MIXTRACK_*_CCS dicts)
# ───────────────────────────────────────────────────────────────────────────────
#   ("sample_scrub", "A"|"B")   jog -> platter velocity (x4 with SHIFT held)
#   ("sample_speed", "A"|"B")   fader -> playback speed (64 = 1x)
#   ("sample_gain",  "A"|"B")   fader -> sample deck gain
#   ("sample_eq", "A"|"B",      knob -> sampler EQ band, +/-15dB, 64 = flat
#                 "hi"|"mid"|"low")
#   ("sample_filter", "A"|"B")  knob -> sweep filter, 64 = bypass,
#                               <64 low-pass sweeps down, >64 high-pass up
#   ("sample_browse", "A"|"B")  encoder -> file browser (x5 with SHIFT held)
#   ("sample_seek", "A"|"B")    touchstrip -> needle drop
#   ("sample_crossfade",)       fader -> sampler A/B output crossfade
#   ("smart_crossfader",)       master crossfader -> blends deck A against B
#   ("sampler_master_gain",)    master gain -> overall sampler output level
#   ("fx_rack_param",)          SHIFT+touchstrip -> sweep active rack slot param
#   ("consume",)                silently consume (explicit no-op)
# ═══════════════════════════════════════════════════════════════════════════════


# ── DECK MAPS — both decks are SAMPLER decks ─────────────────────────────────
# CHORD MODE REMOVED 2026-08-01. owner had never used it, and it was hostile
# by construction: the chord-mode jog was bound to relative_lead_voice (deck 1)
# and relative_pattern (deck 2), i.e. ONE LIST-STEP PER TICK on a wheel that
# emits dense ticks — so spinning it ripped through voicings and arp patterns.
# Gone with it: the deck-1 chord surface, the deck-2 arp + deck-B chord staging
# surface, the deck-2 SHIFT layer, the per-deck PAD_MODE toggle, and the
# crossfader chord morph (the crossfader now blends deck A against deck B).
#
# ⚠ THERE ARE NO SHIFT MAPS, DELIBERATELY. SHIFT is a per-action MODIFIER that
# handlers read themselves (x4 coarse scrub, x5 browse, SHIFT+touchstrip -> FX
# param). A non-empty SHIFT map is EXCLUSIVE in _handle_deck, and the deck-2
# one — left behind as an "inert consume layer" when drum edit was removed on
# 2026-07-21 — silently disabled all 36 of deck B's sampler controls until
# 2026-08-01. Do not reintroduce one without re-reading _handle_deck.
#
# EQ_HI/EQ_MID/EQ_LOW/FILTER drive the deck's 3-band EQ + sweep filter.

MIXTRACK_DECK1_SAMPLER_BUTTONS = {
    # ── TOP ROW pads = GRAB a loop (last N bars @ live BPM, starts looping).
    #    IMPORTANT (fixed 2026-07-27): this unit's top-row pads send MIDI notes
    #    0x13-0x16 (= the LOOP_IN/OUT/ON/HALVE names), NOT 0x17-0x1a (PAD1-4).
    #    Verified by live MIDI capture. The old map used PAD1-4, which THIS
    #    Mixtrack never sends -- so grabbing a loop from the pads never worked
    #    (hence having to grab from the web). Now the physical top row grabs.
    "LOOP_IN":    ("sample_capture_bars", "A", "1"),   # top pad 1 -> grab 1 bar
    "LOOP_OUT":   ("sample_capture_bars", "A", "2"),   # top pad 2 -> grab 2 bars
    "LOOP_ON":    ("sample_capture_bars", "A", "4"),   # top pad 3 -> grab 4 bars
    "LOOP_HALVE": ("sample_capture_bars", "A", "8"),   # top pad 4 -> grab 8 bars
    # The hardware PAD MODE button CYCLES the top row through THREE note banks
    # (verified by capture 2026-07-27): loop (0x13-0x16, above), sampler
    # (0x20-0x23), pad (0x17-0x1a). We can't stop it cycling, so map ALL banks
    # to the same grabs -> the top pads grab 1/2/4/8 no matter which bank the
    # button lands on. This is what kills the "grab only works sometimes" feel.
    "SAMPLER1":   ("sample_capture_bars", "A", "1"),   # top pad 1 (sampler bank)
    "SAMPLER2":   ("sample_capture_bars", "A", "2"),
    "SAMPLER3":   ("sample_capture_bars", "A", "4"),
    "SAMPLER4":   ("sample_capture_bars", "A", "8"),
    "PAD1":       ("sample_capture_bars", "A", "1"),   # top pad 1 (pad bank)
    "PAD2":       ("sample_capture_bars", "A", "2"),
    "PAD3":       ("sample_capture_bars", "A", "4"),
    "PAD4":       ("sample_capture_bars", "A", "8"),

    # ── TRANSPORT: the two big buttons are START/STOP.
    "PLAY":     ("sample_loop_toggle", "A"),         # PLAY  = start / stop toggle
    "CUE":      ("sample_stop",        "A"),         # CUE   = hard stop + rewind
    "SYNC":     ("sample_reverse",     "A"),         # SYNC  = play backwards (latch)

    # ── BOTTOM ROW pads = SETTABLE HOTCUES (notes 0x1b-0x1e): press an empty
    #    pad to SET a cue at the live playhead, press a set pad to JUMP there +
    #    play, SHIFT+pad to clear.  Pad lit yellow = armed, dark = empty.
    "HOTCUE1":  ("sample_cue", "A", "1"),            # hotcue 1
    "HOTCUE2":  ("sample_cue", "A", "2"),            # hotcue 2
    "HOTCUE3":  ("sample_cue", "A", "3"),            # hotcue 3
    "HOTCUE4":  ("sample_cue", "A", "4"),            # hotcue 4

    # ── TOP SECTION (mapped 2026-07-07, MIDI captured same day) ──
    # FX1-3 = RING buttons (2026-09-23; the FX rack was parked): FXn points this
    # deck's GRAB at the n-th ARMED skipback ring (FX1 = MAIN), lit = current;
    # SHIFT+FXn saves that ring's last MIXTRACK_RING_SAVE_SECS to ~/recordings.
    # (Old binding, if the rack ever returns: ("fx_toggle_slot", "0"|"1"|"2").)
    "FX1":      ("ring_button", "A", "1"),           # 1st armed ring (MAIN)
    "FX2":      ("ring_button", "A", "2"),           # 2nd armed ring
    "FX3":      ("ring_button", "A", "3"),           # 3rd armed ring
    "TAP":      ("tap_tempo",),
    "PITCH_UP": ("sample_pitch_bend", "A", "up"),    # momentary beatmatch nudge
    "PITCH_DN": ("sample_pitch_bend", "A", "down"),
    "WHEEL_MODE": ("sample_load_browsed", "A"),      # WHEEL btn = load browsed file

    # PAD_MODE dead on purpose (2026-07-27): it used to flip the deck between
    # sampler and chord "personalities" (the "mind of its own" footgun) AND the
    # hardware button did nothing else useful here. The Mixtrack is a dedicated
    # sampler surface now, so this button is a silent no-op.
    # PAD MODE toggles this deck's top-row capture direction (retro<->forward).
    # ⚠ Provisional binding: the hardware PAD MODE button may ALSO cycle the
    # unit's internal pad note-ranges. All top-row ranges map alike, so pads
    # stay live, but the hardware pad-mode LED can read out of sync -- rebind to
    # a dedicated free button if that's annoying (forward is also on SHIFT+pad).
    "PAD_MODE": ("mixtrack_fwd_mode",),
    "SHIFT":    ("mixtrack_shift",),
    "JOG_TOUCH":("jog_touch",),                      # touch platter = scratch
}

MIXTRACK_DECK1_SAMPLER_CCS = {
    "JOG":          ("sample_scrub", "A"),
    "PITCH_HI":     ("sample_speed", "A"),
    "LINE_FADER":   ("sample_gain",  "A"),
    "EQ_HI":        ("sample_eq",    "A", "hi"),
    "EQ_MID":       ("sample_eq",    "A", "mid"),
    "EQ_LOW":       ("sample_eq",    "A", "low"),
    "FILTER":       ("sample_filter","A"),

    "BEAT_KNOB":    ("sample_browse", "A"),          # top encoder = file browser
    "STRIP":        ("sample_seek",   "A"),          # touchstrip = needle drop
    "STRIP_SHIFT":  ("fx_rack_param",),              # SHIFT+strip = sweep active rack slot param
}

MIXTRACK_DECK2_SAMPLER_BUTTONS = {
    # ── TOP ROW pads = GRAB a loop. Same note-mapping fix as Deck 1 (top-row
    #    pads send notes 0x13-0x16 on this unit, not PAD1-4 / 0x17-0x1a).
    "LOOP_IN":    ("sample_capture_bars", "B", "1"),   # top pad 1 -> grab 1 bar
    "LOOP_OUT":   ("sample_capture_bars", "B", "2"),   # top pad 2 -> grab 2 bars
    "LOOP_ON":    ("sample_capture_bars", "B", "4"),   # top pad 3 -> grab 4 bars
    "LOOP_HALVE": ("sample_capture_bars", "B", "8"),   # top pad 4 -> grab 8 bars
    # All three PAD MODE banks -> same grabs (see Deck 1 note).
    "SAMPLER1":   ("sample_capture_bars", "B", "1"),
    "SAMPLER2":   ("sample_capture_bars", "B", "2"),
    "SAMPLER3":   ("sample_capture_bars", "B", "4"),
    "SAMPLER4":   ("sample_capture_bars", "B", "8"),
    "PAD1":       ("sample_capture_bars", "B", "1"),
    "PAD2":       ("sample_capture_bars", "B", "2"),
    "PAD3":       ("sample_capture_bars", "B", "4"),
    "PAD4":       ("sample_capture_bars", "B", "8"),

    # ── TRANSPORT: the two big buttons are START/STOP.
    "PLAY":     ("sample_loop_toggle", "B"),         # PLAY  = start / stop toggle
    "CUE":      ("sample_stop",        "B"),         # CUE   = hard stop + rewind
    "SYNC":     ("sample_reverse",     "B"),         # SYNC  = play backwards (latch)

    # ── BOTTOM ROW pads = SETTABLE HOTCUES: press empty=SET at playhead, press
    #    set=JUMP+play, SHIFT+pad=clear.  Lit yellow=armed, dark=empty.
    "HOTCUE1":  ("sample_cue", "B", "1"),            # hotcue 1
    "HOTCUE2":  ("sample_cue", "B", "2"),            # hotcue 2
    "HOTCUE3":  ("sample_cue", "B", "3"),            # hotcue 3
    "HOTCUE4":  ("sample_cue", "B", "4"),            # hotcue 4

    # ── TOP SECTION: deck 2's FX1-3 = RING buttons for deck B (see Deck 1).
    "FX1":      ("ring_button", "B", "1"),           # 1st armed ring (MAIN)
    "FX2":      ("ring_button", "B", "2"),           # 2nd armed ring
    "FX3":      ("ring_button", "B", "3"),           # 3rd armed ring
    "TAP":      ("tap_tempo",),
    "PITCH_UP": ("sample_pitch_bend", "B", "up"),    # momentary beatmatch nudge
    "PITCH_DN": ("sample_pitch_bend", "B", "down"),
    "WHEEL_MODE": ("sample_load_browsed", "B"),      # WHEEL btn = load browsed file

    # PAD_MODE dead on purpose (see Deck 1) — no more accidental personality flip.
    # PAD MODE toggles this deck's top-row capture direction (retro<->forward).
    # ⚠ Provisional binding: the hardware PAD MODE button may ALSO cycle the
    # unit's internal pad note-ranges. All top-row ranges map alike, so pads
    # stay live, but the hardware pad-mode LED can read out of sync -- rebind to
    # a dedicated free button if that's annoying (forward is also on SHIFT+pad).
    "PAD_MODE": ("mixtrack_fwd_mode",),
    "SHIFT":    ("mixtrack_shift",),
    "JOG_TOUCH":("jog_touch",),                      # touch platter = scratch
}

MIXTRACK_DECK2_SAMPLER_CCS = {
    "JOG":          ("sample_scrub", "B"),
    "PITCH_HI":     ("sample_speed", "B"),
    "LINE_FADER":   ("sample_gain",  "B"),
    "EQ_HI":        ("sample_eq",    "B", "hi"),
    "EQ_MID":       ("sample_eq",    "B", "mid"),
    "EQ_LOW":       ("sample_eq",    "B", "low"),
    "FILTER":       ("sample_filter","B"),

    "BEAT_KNOB":    ("sample_browse", "B"),          # top encoder = file browser
    "STRIP":        ("sample_seek",   "B"),          # touchstrip = needle drop
    "STRIP_SHIFT":  ("fx_rack_param",),              # SHIFT+strip = sweep active rack slot param
}


# ── Master section ────────────────────────────────────────────────────────────
MIXTRACK_MASTER_CCS = {
    # Crossfader blends sampler deck A against deck B.
    "CROSSFADER":   ("smart_crossfader",),
    # Master GAIN knob -> overall sampler output level (both decks).
    "MASTER_GAIN":  ("sampler_master_gain",),
    # Cue Mix / Cue Gain are the Mixtrack's own hardware headphone controls
    # (mix between its 2 audio outs + headphone volume) -- left unmapped on
    # purpose so they keep doing their hardware job.
}

# Master-section BUTTONS. The two LOAD buttons "load" a fresh loop into a deck
# (grab the last N bars and start looping) -- the DJ-intuitive way to load a
# sample. Change the bar count here to taste.
MIXTRACK_MASTER_BUTTONS = {
    "LOAD1":  ("sample_capture_bars", "A", "4"),   # grab 4 bars into deck A
    "LOAD2":  ("sample_capture_bars", "B", "4"),   # grab 4 bars into deck B
}


# ═══════════════════════════════════════════════════════════════════════════════
# AKAI FIRE — CONTROL SURFACE + STEP SEQUENCER  (orc1_fire.py)
# ═══════════════════════════════════════════════════════════════════════════════
# Edit these to remap the Fire; restart orc1.py to apply. Hardware note/CC
# numbers are fixed by the device and live in orc1_fire.py (BUTTON_NOTE etc.) —
# verified on this unit with the OLED-guided fire_mapper.py.
#
# Symbolic button names (resolve to notes in orc1_fire.py):
#   STEP NOTE DRUM PERFORM SHIFT ALT PAT_SONG PLAY STOP REC
#   PATTERN_UP PATTERN_DOWN BROWSER GRID_LEFT GRID_RIGHT
#   SELECT_PRESS MODE  MUTE1..MUTE4
#
# BUTTON actions (value = (action, *args)):
#   ("view","seq"|"mixer"|"chord"|"chip"|"poly"|"faders"|"universal"|"fx")
#                                                  jump to a view (Session E: 8 modes)
#   ("cycle_view",)                               MODE-style cycle
#   ("shift",) ("alt",)                           hold-modifiers
#   ("seq_play",) ("seq_stop",) ("seq_clear",)    transport
#       (SHIFT+REC appends a new melodic track on CH_POLY, then selects it)
#   ("scroll_tracks",-1|+1)                       move the 4-row viewport
#   ("page",-1|+1)                                step page (lengths > 16)
#   ("toggle_arp",) ("tap_tempo",)                engine
#   ("set_base","MAJ"|"MIN"|"SUS"|"DIM")          chord base
#   ("noop",)                                     consume silently
#   MUTE1..4 always mute the matching visible row.
#
# DIAL actions (dict idx 0..3 -> (action,)): bpm, track_vel, spread, oct_offset.
#   In MIXER view track_vel acts per-row. track_length is NOT a valid dial
#   target (Session E, 2026-06-30) — length is set ONLY by ALT+step pad.
# SELECT knob: ("scroll_pattern",) or ("scroll_tracks",)
#
# CHANNEL view: pad = toggle step; ALT+pad = set that track's LENGTH to the
# pressed column; SHIFT+pad = toggle accent on an existing step.
#
# Each track plays the INTERNAL synth by default ("dest":"synth", drums on
# CH_DRUM, melodic on CH_MONO). For mixed kits, point a track outboard:
#   "dest": {"port": "pisound", "ch": 10}   -> fires that step to the MPC on DIN
# ═══════════════════════════════════════════════════════════════════════════════

# MIDI port names the Fire HANDLER claims (case-insensitive substring match):
#   "FL STUDIO FIRE" = Akai's own firmware;
#   "ORC FW host"    = our custom firmware's host mode (power on holding
#                      SHIFT+ALT), which speaks Akai's protocol to the ORC.
# Was the bare substring "FIRE" until 2026-10-13: it grabbed ANY device with
# "fire" in its name (a test firmware called "ORC FIRE hello", 2026-10-06).
# Our sequencer firmware ("ORC FW seq") must NOT match: it is a plain instrument.
FIRE_PORT_NAMES = ["FL STUDIO FIRE", "ORC FW host"]


def is_fire_port(name):
    """True if a MIDI port name belongs to a Fire the handler should drive."""
    nl = (name or "").lower()
    return any(p.lower() in nl for p in FIRE_PORT_NAMES)


FIRE = {
    "DEFAULT_VIEW":      "universal",   # Fire reorg (2026-09-02): boots to the
                                        # universal control surface (was "seq";
                                        # "channel" before Session E). See
                                        # _planning/ORC1_Fire_Reorg_Spec.md P1.
    "PERFORM_BASE_NOTE": 36,        # bottom-left pad note in perform view

    # Universal Control Surface pad banks.  The 4x16 grid is one bank of this
    # many; GRID <> pages between them in the universal view, and the web
    # /control page gets a matching bank selector.  Capped at 9 by the handler
    # because the OLED prints the bank as a single character.
    "UNIVERSAL_BANKS": 4,

    # SEQ_TRACKS moved 2026-07-21 → the controller-agnostic SEQ dict above
    # (SEQ["DEFAULT_TRACKS"]). The seq is headless; its defaults don't belong
    # under a controller. Cowbell lane added + dead CH7 bass lane removed there.

    "BUTTONS": {
        # Fire reorg (2026-09-02, Fire_Reorg_Spec P1):
        #   STEP    -> toggle universal control surface <-> seq (the "main page")
        #              (boots on universal; from anywhere else STEP -> universal)
        #   DRUM    -> toggle seq <-> seq mixer
        #   PERFORM -> PARKED on chord for now; freed for a new job pending
        #              owner's OPEN #1 decision (do NOT assume it's universal).
        "STEP":    ("toggle_view", "universal", "seq"),
        "DRUM":    ("toggle_view", "seq", "mixer"),
        "PERFORM": ("view", "chord"),
        "BROWSER": ("browser",),          # context menu; no view shortcut (Session E)
        "MODE":    ("cycle_view",),
        "PLAY":    ("seq_play",),
        "STOP":    ("seq_stop",),
        "REC":     ("seq_clear",),
        "SHIFT":   ("shift",),
        "ALT":     ("alt",),
        "PATTERN_UP":   ("scroll_tracks", -1),
        "PATTERN_DOWN": ("scroll_tracks", +1),
        "GRID_LEFT":    ("page", -1),
        "GRID_RIGHT":   ("page", +1),
        "NOTE":     ("note_btn",),       # bare=couple poly, SHIFT=couple chip, ALT=arp
        "PAT_SONG": ("fx_view",),    # bare=FX view, SHIFT=tap tempo, ALT=clock out
        "SELECT_PRESS": ("select_press",),  # confirm/OK in knob-map mode
    },

    # Live drum sculpting: ALT+rotary picks which voice the knobs edit; the 4
    # knobs sculpt it. SHIFT held = filter layer; ALT held = sequencer layer.
    # ("voice_param", X) where X in level/tune/attack/decay/filter_freq/filter_res.
    # EDIT_VOICES (the voice list) moved 2026-07-21 → DRUM_CC["EDIT_VOICES"]
    # (controller-agnostic — it's a drum-kit property, not a Fire property).

    "DIALS": {                       # no modifier — sculpt selected voice
        0: ("voice_param", "attack"),
        1: ("voice_param", "decay"),
        2: ("voice_param", "tune"),
        3: ("voice_param", "level"),
    },
    "DIALS_SHIFT": {                 # SHIFT held — filter layer
        0: ("voice_param", "filter_freq"),
        1: ("voice_param", "filter_res"),
        2: ("voice_param", "tune"),
        3: ("voice_param", "level"),
    },
    "DIALS_ALT": {                   # ALT held — sequencer layer
        0: ("bpm",),
        1: ("track_vel",),
        # knob 2: deliberately unassigned (Session E, 2026-06-30) — track
        # length is set ONLY by ALT+step pad now, far harder to misfire than
        # a knob. Was ("track_length",) before this session.
        3: ("spread",),
    },

    # SELECT encoder: in STEP/MIXER it scrolls the SELECTED track (cursor, the
    # brighter lane); in PERFORM/POLY it scrolls the arp pattern. ALT+SELECT
    # scrolls the drum sculpt voice. (Use ("scroll_pattern",) to restore the
    # old always-pattern behaviour.)
    "SELECT":     ("scroll_track",),
    "SELECT_ALT": ("scroll_voice",),   # ALT + rotary scrolls the edited voice

    # ── Live knob mapping (ALT+BROWSER) ────────────────────────────────────────
    # Each VIEW (seq/mixer/chord/chip/poly/faders/universal/fx) keeps its own base/SHIFT/ALT knob
    # layers, seeded from DIALS* above. To remap on the device:
    #   1) ALT+BROWSER            enter KNOB MAP mode (knobs go inert)
    #   2) touch a knob           pick which knob to map (hold SHIFT or ALT
    #                             while touching to map that knob's SHIFT/ALT
    #                             layer instead of its base layer)
    #   3) turn SELECT            scroll the target menu (KNOB_TARGETS below)
    #   4) press SELECT           assign + save
    #   5) ALT+BROWSER            leave map mode
    # Maps persist to KNOBMAP_FILE and reload on boot. Set it to None to keep
    # mappings in-memory only (reset on restart).
    #
    # Target actions:
    #   ("voice_param", X)        sculpt the rotary-selected drum voice
    #                             (X = level/tune/attack/decay/filter_freq/filter_res)
    #   ("bpm",) ("track_vel",) ("spread",) ("oct_offset",)  -- track_length
    #                             removed Session E (2026-06-30), ALT+step pad only
    #   ("track_note",) ("track_channel",)   edit the SELECTED track's output
    #                             note / MIDI channel (point a track at ch 8 to
    #                             play the internal poly synth melodically)
    #   ("cc_out", port, ch, cc)  send a 0..127 MIDI CC to a named port. The
    #                             knob (a relative encoder) accumulates an
    #                             absolute value, centred at 64. port "synth"
    #                             = internal synth bus; "pisound" = the DIN out
    #                             to external gear. ch is 1-based.
    #                             On port "synth" ch may instead NAME A MODULE
    #                             ("poly", "chip_p2", ...) = whatever channel
    #                             /matrix has it on right now.
    #   ("none",)                 park the knob (does nothing)
    # ── Track-edit mode (BROWSER or SELECT-press in channel/mixer) ─────────────
    # Maps knob index 0-3 to the track property each knob controls while the
    # track-edit overlay is open. Available props: "note", "ch", "vel", "len".
    # Reorder or repeat as you like; any unrecognised prop is silently skipped.
    "SAMPLES_DIR":       os.path.join(_ORC1_DIR, "samples"),
    # "len" removed Session E (2026-06-30) — track length set ONLY by ALT+step pad.
    "TRACK_EDIT_KNOBS": ["voice", "ch", "vel"],

    # MIDI clock output (24 PPQN).  Toggle with ALT+PAT_SONG or web TOGGLE_CLOCK_OUT.
    # Lists port keys from orc1_config.PORTS that receive clock, Start, and Stop.
    # Defaults OFF at startup regardless. Add "cme" here to also clock the U6MIDI.
    "CLOCK_OUT_PORTS": ["pisound"],

    # TRACK_COLORS moved 2026-07-21 → SEQ["TRACK_COLORS"] (controller-agnostic;
    # the palette is a sequencer property shared by the Fire and the web /seq).

    # ── Parameter scaling ────────────────────────────────────────────────────
    # Controls the response curve when a knob edits a drum voice parameter or
    # an outboard/internal CC.  Applies to the OUTPUT sent to the synth; the
    # internal 0-127 accumulator stays linear so OLED readings make sense.
    #
    # PARAM_SCALING  — keyed by voice_param name string
    # CC_SCALING     — keyed by CC number (int)
    #
    # Values:
    #   "log"    (pow 0.4)  — more resolution at low end.  Good for level,
    #                         attack, decay, filter_freq.  Feels like a log pot.
    #   "exp"    (pow 2.5)  — more resolution at high end.  Rarely needed.
    #   "linear" (default)  — no transformation.
    "PARAM_SCALING": {
        "level":       "log",
        "attack":      "log",
        "decay":       "log",
        "filter_freq": "log",
        "filter_res":  "linear",
        "hp_freq":     "log",
        "hp_res":      "linear",
        "tune":        "linear",
    },
    "CC_SCALING": {
        # Keyed by CC number.  Examples:
        7:  "log",    # channel volume  (General MIDI standard)
        11: "log",    # expression
        74: "log",    # filter cutoff
        75: "log",    # filter env depth / octave range
    },

    "KNOBMAP_FILE": "~/fire_knobmap.json",
    "KNOB_TARGETS": [
        ("attack",  ("voice_param", "attack")),
        ("decay",   ("voice_param", "decay")),
        ("tune",    ("voice_param", "tune")),
        ("level",   ("voice_param", "level")),
        ("lpfreq",  ("voice_param", "filter_freq")),
        ("lpreso",  ("voice_param", "filter_res")),
        ("bpm",     ("bpm",)),
        ("trkvel",  ("track_vel",)),
        # "trklen"/track_length removed Session E (2026-06-30).
        ("trknote", ("track_note",)),     # selected track's MIDI note
        ("trkchan", ("track_channel",)),  # selected track's MIDI channel (8=poly)
        ("spread",  ("spread",)),
        ("octave",  ("oct_offset",)),
        # Outboard / internal CC sends. An internal ("synth") send NAMES the module
        # ("poly", "chip_p1", ...): the Fire resolves it from the live /matrix
        # bindings at send time, so moving a module never strands a knob
        # (2026-09-24; these were literal ch 8/9). Outboard sends keep a channel:
        ("synwav",  ("cc_out", "synth", "poly", 1)),   # poly waveform select
        ("synflt",  ("cc_out", "synth", "poly", 74)),   # poly filter cutoff
        ("synres",  ("cc_out", "synth", "poly", 71)),   # poly filter resonance
        ("synmode", ("cc_out", "synth", "poly", 70)),   # poly filter LP/HP/BP
        ("synoct",  ("cc_out", "synth", "poly", 75)),   # poly octave (-2..+2)
        ("synrel",  ("cc_out", "synth", "poly", 11)),   # poly release
        ("chipwav", ("cc_out", "synth", "chip_p1", 1)),   # chip timbre: duty/tri/noise
        ("chipflt", ("cc_out", "synth", "chip_p1", 74)),   # chip filter cutoff
        ("chipoct", ("cc_out", "synth", "chip_p1", 75)),   # chip octave
        ("dincut",  ("cc_out", "pisound", 10, 74)),  # DIN cutoff to ch10 gear
        ("modwhl",  ("cc_out", "pisound", 10,  1)),  # DIN mod wheel
        ("none",    ("none",)),
    ],

    # ── Chord view keyboard row routing ────────────────────────────────────────
    # Each of the 4 pad rows in chord view can be independently routed.
    # Index 0 = top row, index 3 = bottom row.
    #
    # type "chord"  : sends root note to chord engine (original behaviour)
    # type "direct" : sends raw MIDI note to port/channel (bypasses chord engine)
    # type "off"    : row is silent / dark
    #
    # "octave" is an additional octave offset on top of the row's natural position
    # (e.g. -1 brings a mid row down to bass range).  0 = no offset.
    #
    # To send to the internal synth voices, use port "synth" and the appropriate
    # channel: 8=poly, 9=chip-pulse1, 10=chip-pulse2, 11=tri, 4=noise, 7=bass-tone.
    #
    # These defaults can be overridden live from the Fire (BROWSER → Configure Rows)
    # and are persisted to ~/.orc1_chord_rows.json.
    # Per-row "octave" is ABSOLUTE (2026-09-03): it fully sets the row's octave
    # offset from perform_base_note, with NO hidden (3-row)*12 auto-stack. The
    # [+3,+2,+1,0] defaults reproduce exactly the old C5/C4/C3/C2 stack
    # (perform_base_note default 36 = C2). This is the handler's real default
    # (it wins over orc1_fire._DEFAULT_CHORD_ROWS via fcfg.get), so keep the two
    # in step -- octave 0 here would collapse every row onto the base octave.
    "CHORD_ROW_MAP": [
        # A "synth" row NAMES the module it plays (resolved live from the /matrix
        # bindings, 2026-09-24 -- these were literal ch 8/9/7; after the poly
        # moved, row 0 played chip P2 and row 2 hit the SAMPLER's channel).
        # Row 0 (top — highest octave): direct → internal poly synth
        {"type": "direct", "port": "synth", "channel": "poly",    "octave": 3},
        # Row 1: direct → internal chip pulse 1
        {"type": "direct", "port": "synth", "channel": "chip_p1", "octave": 2},
        # Row 2: direct → internal bass tone (the mono synth)
        {"type": "direct", "port": "synth", "channel": "mono",    "octave": 1},
        # Row 3 (bottom — lowest octave): chord engine (original behaviour)
        {"type": "chord",  "port": None,    "channel": None, "octave": 0},
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOSAMPLER — MIDI note sweep → JACK capture → MPC-ready WAVs + .PGM
# ═══════════════════════════════════════════════════════════════════════════════
# Job engine lives in orc1_autosampler.py (runs inside orc1.py as a thread,
# opens its own JACK client only while sweeping). Every value here is a
# DEFAULT — the web card can override any of them per job.
# MIDI target = any PORTS key ("synth" = the internal synth bus);
# capture source = any JACK output port pair (defaults to the poly module so a
# sweep works with zero external gear — pick an L12 input pair to sample
# outboard synths).
AUTOSAMPLER = {
    "OUT_ROOT":   os.path.expanduser("~/recordings/autosample"),
    "PORT":       "synth",              # MIDI destination (PORTS key)
    "CHANNEL":    8,                    # the web card fills in poly's live channel
    "CAPTURE_L":  "ORC1-Poly:out_L",
    "CAPTURE_R":  "ORC1-Poly:out_R",
    "NOTE_LO":    36,                   # C2
    "NOTE_HI":    96,                   # C7
    "NOTE_STEP":  3,                    # every minor 3rd (JJOS fills gaps
                                        # from the WAV smpl root notes)
    "VELOCITIES": [127],
    "HOLD_S":     2.0,                  # note-on duration
    "TAIL_S":     1.5,                  # record after note-off (release/verb)
    "GAP_S":      0.3,                  # settle time between hits
    "STEREO":     False,                # mono default (MPC RAM is finite)
    "TRIM_DB":    -60.0,                # head/tail silence threshold
    "FADE_MS":    10.0,                 # declick fade at the cut tail
    "TARGET_PEAK": 0.9,                 # per-KIT normalization target
    "MAX_GAIN":   20.0,                 # gain cap (26 dB) for quiet sources
}


# ═══════════════════════════════════════════════════════════════════════════════
# RUNTIME OVERRIDES  (Phase 9, 2026-07-13)
# ═══════════════════════════════════════════════════════════════════════════════
# Flat dotted-key JSON object: {"SYNTH.MASTER_LEVEL": 0.6, "SAMPLER.QUANTIZE_SNAP": 0.2}
# Applied at import time.  A corrupt/absent file is LOGGED and IGNORED — boot
# never fails.  Edit via the web /system CONFIG OVERRIDES card, or directly.
# Restart the relevant service to apply (the web card tells you which one).
_OVR_PATH = os.path.expanduser("~/.orc1_config_overrides.json")
try:
    import json as _json_ovr, logging as _log_ovr
    _ovr_log = _log_ovr.getLogger("orc1_config")
    with open(_OVR_PATH) as _fh_ovr:
        _ovr = _json_ovr.load(_fh_ovr)
    if not isinstance(_ovr, dict):
        raise TypeError("overrides file must be a JSON object")
    _g = globals()
    for _key, _val in _ovr.items():
        try:
            parts = _key.split(".")
            if len(parts) == 1:
                if parts[0] not in _g:
                    raise KeyError(f"no top-level name {parts[0]!r}")
                _g[parts[0]] = _val
            else:
                obj = _g.get(parts[0])
                if obj is None:
                    raise KeyError(f"no config section {parts[0]!r}")
                for p in parts[1:-1]:
                    obj = obj[p]
                obj[parts[-1]] = _val
            _ovr_log.info("orc1_config override: %s = %r", _key, _val)
        except Exception as _e:
            _ovr_log.warning("orc1_config override skipped (%s): %s", _key, _e)
except FileNotFoundError:
    pass  # no overrides file — normal on first boot
except Exception as _e:
    import logging as _log_ovr2
    _log_ovr2.getLogger("orc1_config").warning("orc1_config override file error: %s", _e)


# ═══════════════════════════════════════════════════════════════════════════════
# PER-MODULE OUTPUT ROUTING  (2026-09-15)  — single source of truth
# ═══════════════════════════════════════════════════════════════════════════════
# The audio-host split (2026-09-02) gave every module its own JACK client, but
# each host blindly auto-connected out_L/out_R to the first physical playback
# pair on startup. Routing a module elsewhere (via /patch) therefore left it ALSO
# on the mains, and a host restart re-added the mains even after a manual cut.
# This is where "module -> destination" lives, so the HOSTS (via orc1_synth) and
# the WEB endpoint agree without either importing the other's heavy module. The
# hosts read it at startup; /api/module_outputs rewrites it live and persists it.
# Nothing else owns a module's out ports (the /patch reconciler ignores wires it
# never saved), so the two never fight. Absent/invalid -> first physical pair
# (the historical default), so a rig with no file is byte-for-byte unchanged.
import os as _os_mo

MODULE_OUT_FILE = _os_mo.path.expanduser("~/.orc1_module_outputs.json")

# One entry per audio host. Order = display order in the web UI.
MODULE_OUT_KEYS = ["drum", "poly", "chip", "sampler", "mono", "bbkit"]

# routing key -> the module's JACK client name (whose out_L/out_R get rewired).
MODULE_JACK_NAMES = {
    "drum":    "ORC1-Drum",
    "poly":    "ORC1-Poly",
    "chip":    "ORC1-Chip",
    "sampler": "ORC1-Sampler",
    "mono":    "ORC1-Mono",
    "bbkit":   "ORC1-BBKit",
}

# routing key -> human label for the UI.
MODULE_LABELS = {
    "drum":    "Drums",
    "poly":    "Poly",
    "chip":    "Chip",
    "sampler": "Sampler",
    "mono":    "Mono",
    "bbkit":   "BeatBuddy Kit",
}


def resolve_module_out(dest_id, phys):
    """Map a saved dest id to a (portL, portR) pair, or None for 'no output'.

      "none"        -> None (the module renders but is wired nowhere)
      "out-<a>-<b>" -> (phys[a-1], phys[b-1]) if both 1-based indices exist
      anything else / out-of-range / None -> the FIRST physical pair (main)

    Card-agnostic: indices are positions in the LIVE physical-port list, so
    "out-3-4" means "the 3rd and 4th outputs of whatever card is fitted" and
    falls back to main on a card that does not have them. `phys` is the ordered
    list of physical playback port names (hosts get it from the JACK client, the
    web from jack_lsp — the SAME order, so the two agree on what "out-3-4" is)."""
    if dest_id == "none":
        return None
    if isinstance(dest_id, str) and dest_id.startswith("out-"):
        parts = dest_id.split("-")
        if len(parts) == 3:
            try:
                a, b = int(parts[1]), int(parts[2])
            except ValueError:
                a = b = 0
            if 1 <= a <= len(phys) and 1 <= b <= len(phys):
                return (phys[a - 1], phys[b - 1])
    if len(phys) >= 2:
        return (phys[0], phys[1])
    if len(phys) == 1:
        return (phys[0], phys[0])
    return None


def load_module_out(module_key):
    """The saved dest id for a module, or None (= use the default pair). A
    missing or corrupt file is 'no override', never an error."""
    import json as _json_mo
    try:
        with open(MODULE_OUT_FILE) as _f:
            _d = _json_mo.load(_f)
        _v = _d.get(module_key)
        return _v if isinstance(_v, str) else None
    except Exception:
        return None


# -- FX rack I/O (legacy /fx rack, ~/.orc1_fx_io.json) -- 2026-09-28 ----------
# HARDWARE mode = explicit send/return ports (a physical cable loop). INTERNAL
# mode names a MODULE ("module": "poly"): the send is that module's outs and the
# return is wherever its module output (above) sends it, resolved at READ time,
# so a /patch move or a module-output change never leaves the rack pointing at a
# stale port. Until 2026-09-28 INTERNAL hardcoded the retired ORC1-Synth client
# (and, in the live file, the never-started ORC1-FXMix), so the rack's send was
# a port that did not exist. owner's choice: one module at a time, by name.
FX_IO_HARDWARE = {
    "mode":     "hardware",
    "send_l":   "pisound-in:capture_1",
    "send_r":   "pisound-in:capture_2",
    "return_l": "pisound-out:playback_1",
    "return_r": "pisound-out:playback_2",
}


def jack_phys_playback():
    """Physical playback ports in JACK order via jack_lsp (same order the hosts
    and the web's _mo_phys_ports see). [] if jackd is unreachable."""
    import subprocess as _sp
    try:
        out = _sp.run(["jack_lsp", "-t", "-p"], capture_output=True,
                      text=True, timeout=3).stdout
    except Exception:
        return []
    res, cur = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = [line.strip(), False, False, False]   # name, audio, input, phys
            res.append(cur)
        elif cur is not None:
            s = line.strip().lower()
            if "audio" in s:
                cur[1] = True
            if s.startswith("properties:"):
                cur[2] = cur[2] or "input" in s
                cur[3] = cur[3] or "physical" in s
    return [r[0] for r in res if r[1] and r[2] and r[3]]


def fx_io_resolve(io, phys):
    """Return a copy of an fx_io dict with INTERNAL + module resolved to live
    ports. `phys` = physical playback list, or a callable returning it (only
    called when needed, so HARDWARE mode never touches jackd). Any other shape
    (hardware, or internal with custom ports and no module) passes through."""
    out = dict(io)
    mod = out.get("module")
    if out.get("mode") != "internal" or mod not in MODULE_JACK_NAMES:
        return out
    jk = MODULE_JACK_NAMES[mod]
    out["send_l"], out["send_r"] = jk + ":out_L", jk + ":out_R"
    dest = load_module_out(mod)
    pair = resolve_module_out(dest, phys() if callable(phys) else phys)
    out["return_l"], out["return_r"] = pair if pair else ("", "")
    out["return_none"] = (dest == "none")
    return out


def fx_io_apply(current, data):
    """Fold a /api/fx/io POST body into the current fx_io. Pure: returns
    (new_io, error_or_None); the caller resolves + persists.

      {"preset": "hardware"}               -> pisound loop ports (module kept,
                                              so INTERNAL remembers its pick)
      {"preset": "internal", "module": m}  -> INTERNAL on module m (m omitted =
                                              the remembered one; none = error)
      {"module": m}                        -> INTERNAL on m ("" = custom ports)
      {"mode": ...}                        -> that mode, WITH its ports (a bare
                                              label change once left an internal
                                              dry-cut on hardware ports = silence)
      explicit send_*/return_*             -> custom ports; drops the module so
                                              a resolver can't override them
    """
    data = dict(data or {})
    new = dict(current)
    preset = data.pop("preset", None)
    if preset == "hardware":
        data.setdefault("mode", "hardware")
        for k in ("send_l", "send_r", "return_l", "return_r"):
            data.setdefault(k, FX_IO_HARDWARE[k])
    elif preset == "internal":
        data["mode"] = "internal"
    elif preset is not None:
        return current, "unknown preset %r" % (preset,)
    mod = data.get("module")
    if mod not in (None, "") and mod not in MODULE_JACK_NAMES:
        return current, "unknown module %r" % (mod,)
    if mod and "mode" not in data:
        data["mode"] = "internal"
    ports_given = any(k in data for k in ("send_l", "send_r", "return_l", "return_r"))
    mode = data.get("mode", new.get("mode"))
    if mode not in ("hardware", "internal"):
        return current, "unknown mode %r" % (mode,)
    if mode == "internal" and not ports_given:
        if "module" not in data:
            data["module"] = new.get("module") or ""
        if not data["module"]:
            return current, "INTERNAL needs a module to take its sound from"
    elif mode == "hardware" and not ports_given and new.get("mode") != "hardware":
        for k in ("send_l", "send_r", "return_l", "return_r"):
            data[k] = FX_IO_HARDWARE[k]
    if ports_given and mode == "internal" and not data.get("module"):
        data["module"] = ""          # custom internal ports: nothing to resolve
    for k in ("mode", "module", "send_l", "send_r", "return_l", "return_r"):
        if k in data:
            new[k] = data[k]
    return new, None


def load_fx_io(phys=None):
    """~/.orc1_fx_io.json over the hardware defaults, resolved (fx_io_resolve).
    The path is expanded at CALL time so a sandboxed HOME is honoured."""
    import json as _json_fx
    d = dict(FX_IO_HARDWARE)
    try:
        with open(_os_mo.path.expanduser("~/.orc1_fx_io.json")) as _f:
            d.update(_json_fx.load(_f))
    except Exception:
        pass
    return fx_io_resolve(d, phys if phys is not None else jack_phys_playback)
