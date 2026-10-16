#!/usr/bin/env python3
"""
ORC-1 shared DSP library (was the single-process "ORC-1 Synth Engine").

NOT A RUNNING SERVICE. Since the 2026-09-02 audio-host split every module runs
in its own JACK host -- orc1_{drum,poly,chip,mono,sampler,bbkit}_host.py -- and
each one `import orc1_synth as S` to reuse the voices / engines (909 drums,
PolySynth, MonoSynth, NESEngine, SampleVoice), the channel + CC tables, the
module bindings, the persistence helpers, PeakLimiter and the sampler router.
Importing it has no side effects (no JACK, no MIDI). The old process runtime
(SynthEngine, its MIDI thread/_route_midi, run_jack/main, the main-mix
coupling) was removed 2026-09-24; `orc1-synth.service` is retired.

  In every host: MIDI thread -> event queue -> JACK process callback. Triggers
  from MIDI are passed via a thread-safe queue so the MIDI thread never blocks
  audio. All synthesis is NumPy-vectorised (releases GIL) so the JACK thread
  stays real-time safe in practice.

MIDI CHANNELS (set in orc1_config.py SYNTH dict, or override below):
  CH_DRUM (default 10, GM)  -- drum voice triggers on GM note numbers
  CH_MONO (default 7)  -- RESERVED, currently UNASSIGNED (freed 2026-06-30).
                         Used to alias into the same PolySynth pool as
                         CH_POLY (MonoSynth was retired); that made CH7 and
                         CH8 functionally identical, so the alias was
                         removed. Still claimed in config and still treated
                         as an internal-synth channel (not DIN passthrough)
                         so it's ready to become something else later -- a
                         real mono voice, or anything -- without touching
                         routing elsewhere. A note-on here currently does
                         nothing.
  CH_POLY (default 8)  -- poly tone-gen notes + its CC
  CH_CHIP (default 9)  -- NES chip poly (2 pulse + triangle + noise). Same CC
                         map; CC1 picks the timbre: 0-25 pulse 12.5%, 26-51
                         pulse 25%, 52-77 pulse 50%, 78-103 triangle, 104+ noise

DRUM NOTE MAP (GM standard, remappable via SYNTH['DRUM_NOTE_MAP']):
  36 Bass Drum   38 Snare     41 Low Tom    43 Mid Tom    50 Hi Tom
  37 Rimshot     39 Clap      42 Closed Hat 46 Open Hat   49 Cymbal (crash)
  51 Ride (53 ride bell, 59 ride 2 -> ride)

TONE-GEN CC (PolySynth on CH_POLY):
  1  Waveform   (0-31 sine, 32-63 saw, 64-95 square, 96-127 triangle)
  8  Attack     (0-127 -> 2ms-4s)
  9  Decay      (0-127 -> 5ms-4s)
  10 Sustain    (0-127 -> 0-1)
  11 Release    (0-127 -> 5ms-4s)
  13 Master level
  70 Filter mode    (0-42 LP, 43-85 HP, 86-127 BP)
  71 Filter resonance (0-127 -> 0.707 flat .. ~10 sharp)
  74 Filter cutoff   (0-127 -> 80Hz-18kHz exp; 127 = open/bypass, transparent)
  75 Octave transpose (0-127 -> -2..+2 octaves, 64 = 0)
  (Filter is one vectorised biquad on the summed output; default open = clean.)

DRUM CC (on CH_DRUM):
  Base CC 20. Each voice has 6 consecutive CCs.
  Voice offsets: BD=0 SD=6 LT=12 MT=18 HT=24 RM=30 CL=36 CH=42 OH=48 CY=54
  Per voice: base+0=level  base+1=tune  base+2=attack  base+3=decay
             base+4=filter_freq  base+5=filter_res
  Examples: Snare decay = CC 20+6+3 = 29
            Closed hat decay = CC 20+42+3 = 65

INSTALL:
  pip install JACK-Client numpy --break-system-packages
"""

import sys
import os
import time
import math
import numpy as np

# -- Optional config import ----------------------------------------------------
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import orc1_config as _cfg
    _SYNTH_CFG = getattr(_cfg, "SYNTH", {})
    _SAMPLER_CFG = getattr(_cfg, "SAMPLER", {})
    _DRUM_CC_CFG = getattr(_cfg, "DRUM_CC", {})
except ImportError:
    _SYNTH_CFG = {}
    _SAMPLER_CFG = {}
    _DRUM_CC_CFG = {}

# orc1_sampler import removed 2026-09-03: the sampler split to orc1_sampler_host
# (which imports it directly), and the synth no longer instantiates any
# orc1_sampler class -- so importing it here just made all five hosts (which do
# `import orc1_synth as S`) transitively load it for nothing.

# -- Global configuration ------------------------------------------------------
SAMPLE_RATE    = _SYNTH_CFG.get("SAMPLE_RATE",    48000)
BUFFER_SIZE    = _SYNTH_CFG.get("BUFFER_SIZE",    512)
CH_DRUM        = _SYNTH_CFG.get("CH_DRUM",        10)    # 1-indexed
CH_SAMPLER     = int(_SAMPLER_CFG.get("CH_SAMPLER", _SYNTH_CFG.get("CH_MONO", 7)))
CH_MONO        = _SYNTH_CFG.get("CH_MONO",        7)
CH_POLY        = _SYNTH_CFG.get("CH_POLY",        8)
CH_CHIP        = _SYNTH_CFG.get("CH_CHIP",        9)    # NES Pulse 1
CH_CHIP_P2     = _SYNTH_CFG.get("CH_CHIP_P2",    6)    # NES Pulse 2
CH_CHIP_TRI    = _SYNTH_CFG.get("CH_CHIP_TRI",   11)    # NES Triangle
CH_CHIP_NS     = _SYNTH_CFG.get("CH_CHIP_NS",    12)    # NES Noise
CH_GB_WAVE     = _SYNTH_CFG.get("CH_GB_WAVE",     5)    # Game Boy wave channel (CH3 analogue)
CH_CHIP_PCM    = _SYNTH_CFG.get("CH_CHIP_PCM",   16)    # NES PCM  (16 = away from everything)

# Lookup table: MIDI channel -> NES voice name (built once at module load)
_CHIP_CH_VOICE = {
    CH_CHIP:     'pulse1',
    CH_CHIP_P2:  'pulse2',
    CH_CHIP_TRI: 'triangle',
    CH_CHIP_NS:  'noise',
    CH_CHIP_PCM: 'pcm',    # 16 -- well clear of control (13), chord (12), melody (15)
    CH_GB_WAVE:  'wave',   # 5  -- Game Boy programmable wave channel
}

# -- Module Matrix: live channel bindings ------------------------------------
# `_BINDINGS = {channel -> module_key}` is the LIVE dispatch authority (replaces
# the old fixed `ch == CH_POLY` / `ch in _CHIP_CH_VOICE` comparisons).
# The CH_* constants above are now only the boot SEED and the startup prints;
# nothing else in this file reads them. A module can be REASSIGNED to any free
# channel or taken OUT of the fold (absent from _BINDINGS = nothing listens).
# Truth persists to ~/.orc1_bindings.json (the brain is the sole writer); the
# hosts learn live changes via the CH_CTRL bind handshake (each host's
# _make_router tracks its own channel from S._BINDINGS at startup).
try:
    import orc1_config as _mmcfg
    _MODULE_VOICE   = dict(getattr(_mmcfg, "MODULE_VOICE", {}))
    _MODULE_KEYS    = list(getattr(_mmcfg, "MODULE_KEYS", []))
    _RESERVED_CH    = (_mmcfg.PROTECTED if hasattr(_mmcfg, "PROTECTED")   # live /matrix setting
                       else set(getattr(_mmcfg, "RESERVED_CHANNELS", {12, 13, 14, 15})))
    _BIND_ARM_CC    = int(getattr(_mmcfg, "BIND_ARM_CC",   20))
    _BIND_CLEAR_CC  = int(getattr(_mmcfg, "BIND_CLEAR_CC", 21))
    _BIND_COMMIT_NOTE = int(getattr(_mmcfg, "BIND_COMMIT_NOTE", 1))
    _BINDINGS_FILE  = os.path.expanduser(getattr(_mmcfg, "BINDINGS_FILE",
                                                 "~/.orc1_bindings.json"))
except Exception:
    _MODULE_VOICE, _MODULE_KEYS = {}, []
    _RESERVED_CH = {12, 13, 14, 15}
    _BIND_ARM_CC, _BIND_CLEAR_CC, _BIND_COMMIT_NOTE = 20, 21, 1
    _BINDINGS_FILE = os.path.expanduser("~/.orc1_bindings.json")
# chip-family module keys (dispatched through the shared chip render/CC body)
_CHIP_MODULES = set(_MODULE_VOICE) or {
    "chip_p1", "chip_p2", "chip_tri", "chip_noise", "chip_pcm", "gb_wave"}
# module id (index in MODULE_KEYS) -> key, for decoding the arm/clear handshake
_MODULE_BY_ID = {i: k for i, k in enumerate(_MODULE_KEYS)}

# Shipped-default bindings (first-run seed; equals what the brain would persist).
# Reserved-filtered so a stale/failed-config CH_* fallback (e.g. CH_CHIP_NS=12)
# can never seed a voice onto a reserved channel.
_DEFAULT_BINDINGS = {ch: m for ch, m in {
    CH_DRUM: "drums", CH_POLY: "poly", CH_SAMPLER: "sampler",
    CH_CHIP: "chip_p1", CH_CHIP_P2: "chip_p2", CH_CHIP_TRI: "chip_tri",
    CH_CHIP_NS: "chip_noise", CH_CHIP_PCM: "chip_pcm", CH_GB_WAVE: "gb_wave",
}.items() if ch not in _RESERVED_CH}

def _load_bindings():
    """Read ~/.orc1_bindings.json -> {int channel: module_key}. Absent/corrupt ->
    the shipped defaults, so a bad file can never brick the synth (Phase-9 rule)."""
    try:
        import json as _json          # orc1_synth has no module-level json
        with open(_BINDINGS_FILE) as f:
            raw = _json.load(f).get("bindings", {})
        out = {}
        for c, m in raw.items():
            ci = int(c)
            # Require a KNOWN module (align with the brain's loader) so a
            # hand-edited bad name can't leave the two processes disagreeing.
            if (1 <= ci <= 16 and ci not in _RESERVED_CH
                    and isinstance(m, str)
                    and (m in _MODULE_KEYS or not _MODULE_KEYS)):
                out[ci] = m
        return out or dict(_DEFAULT_BINDINGS)
    except Exception:
        return dict(_DEFAULT_BINDINGS)

_BINDINGS   = _load_bindings()   # {channel -> module_key}, dispatch authority
# Pitch-bend range for chip voices, in semitones (± range for pitchwheel
# messages on the Pulse 1 / Pulse 2 / GB Wave channels). See orc1_config.py.
CHIP_BEND_RANGE = float(_SYNTH_CFG.get("CHIP_BEND_RANGE", 12))
MASTER_LEVEL   = _SYNTH_CFG.get("MASTER_LEVEL",   0.8)
SUB_HPF_HZ     = _SYNTH_CFG.get("SUB_HPF_HZ",     40.0)  # infrasonic protection
JACK_NAME        = _SYNTH_CFG.get("JACK_NAME",       "ORC1-Synth")
JACK_AUTOCONNECT = _SYNTH_CFG.get("JACK_AUTOCONNECT", True)

# ── Per-module output routing (2026-09-15) ───────────────────────────────────
# The audio-host split gave every module its own JACK client (ORC1-Poly, -Chip,
# ...), but each host BLINDLY auto-connected out_L/out_R to the first physical
# playback pair on startup -- so routing a module to another output pair (via
# /patch) left the module ALSO on the mains, and a host restart re-added the
# mains even after a manual cut. Now each host asks WHERE to connect at startup.
# The resolver + saved-file are the SINGLE SOURCE OF TRUTH in orc1_config
# (numpy-free, so the light web process shares it without importing the synth);
# this is only the JACK-client-facing wrapper the hosts call from run_jack().


def phys_playback_ports(client):
    """The card's physical audio INPUT ports (JACK 'playback' = the speakers),
    in JACK's own order. These are the destinations a module out connects TO."""
    try:
        return [p.name for p in client.get_ports(
            is_physical=True, is_input=True, is_audio=True)]
    except Exception:
        return []


def preload_midi_backend():
    """Import mido + load its rtmidi backend NOW, on the calling (main) thread.

    Call it BEFORE starting a host's MIDI thread and before the JACK client
    activates. Every host imported mido INSIDE its MIDI thread, which started
    right before activation, so the import (a big C-extension load) held the GIL
    while the new RT callback needed it -> the host xrun'd the WHOLE graph, every
    skipback ring included. Measured 2026-09-24: orc1_fxmix 9 of 13 starts -> 0 of
    16 with this; ORC1-Drum 2 of 10 solo restarts. Idempotent and never raises."""
    try:
        import mido
        mido.get_input_names()
    except Exception:
        pass


def autoconnect_module_out(client, out_L, out_R, module_key, log=print):
    """Connect a module's stereo out to its configured destination (default =
    the first physical pair). Honours JACK_AUTOCONNECT. Returns the connected
    pair as a list, or [] for 'none'/failure. Called from each host run_jack().

    Resolution + the saved dest id live in orc1_config (resolve_module_out /
    load_module_out) so the web endpoint agrees without importing this module."""
    if not JACK_AUTOCONNECT:
        return []
    try:
        import orc1_config as _cfg_mo
        phys = phys_playback_ports(client)
        pair = _cfg_mo.resolve_module_out(
            _cfg_mo.load_module_out(module_key), phys)
        if not pair:
            log(f"output: none (not auto-connected)")
            return []
        client.connect(out_L, pair[0])
        client.connect(out_R, pair[1])
        log(f"output -> {list(pair)}")
        return list(pair)
    except Exception as e:
        log(f"output auto-connect warning: {e}")
        return []


# -- Fader response shaping (2026-08-05) --------------------------------------
# The poly/chip voices are intrinsically hot (bright saws / stacked squares), so
# a LINEAR fader (gain = cc/127) crammed the entire usable range into the bottom
# few percent of travel and full-throttle was ear-splitting -- you could not sit
# the synths under transient drums without burying the fader at the very bottom.
# Fix: keep the STORED master as the raw fader position 0..1 (so bus-level
# persistence and the web/Fire fader DISPLAY round-trip unchanged -- nothing here
# touches set_cc, the restore path, or _bus_level_cc), and shape it only at
# render time. Synth buses get a squared (perceptual) taper times a ceiling that
# caps what full fader can produce; drums are transient and less crammed, so they
# get a plain ceiling for headroom (no squaring). Ceilings tunable via config;
# these defaults were dialled by ear on 2026-08-05. Raising a ceiling makes that
# bus's MAX louder; it does not change where silence or unity sit.
POLY_FADER_CEIL = float(_SYNTH_CFG.get("POLY_FADER_CEIL", 0.35))
CHIP_FADER_CEIL = float(_SYNTH_CFG.get("CHIP_FADER_CEIL", 0.25))
DRUM_FADER_CEIL = float(_SYNTH_CFG.get("DRUM_FADER_CEIL", 0.60))
MONO_FADER_CEIL = float(_SYNTH_CFG.get("MONO_FADER_CEIL", 0.35))  # mono host, poly-like

def _synth_fader_gain(master, ceil):
    """Perceptual (squared) fader taper with a ceiling. `master` is the raw fader
    position 0..1 (what set_cc stores and the display shows); returns the actual
    output gain to multiply the bus buffer by. Squaring puts most of the travel's
    resolution in the quiet region where the ear needs it."""
    return master * master * ceil


class PeakLimiter:
    """Stereo-linked brickwall peak limiter -- the summed-mix limiter lifted out
    of the retired SynthEngine (2026-09-03, audio host split) so each module host
    can protect its OWN output. Below CEIL it is fully transparent (bit-identical,
    normal playing untouched); above it, it applies smooth GAIN reduction -- instant
    attack so no sample ever exceeds CEIL, ~80ms release -- rather than reshaping
    the wave, so peaks are held cleanly with no distortion. Stereo-linked (one gain
    from the louder channel) so a stereo image never shifts when only one side
    peaks. State is per-instance (`_g`); construct one per host.

    NOTE: this makes each module's OWN out self-protecting (the right thing for the
    separate-cards future). It cannot stop the SUM of several modules sharing one
    physical out pair from clipping -- that is inherent to summing at the output and
    is solved by giving each module its own out. owner's call: no shared master
    audio bus."""

    def __init__(self, ceil=0.98, release_s=0.080):
        self.ceil      = float(ceil)
        self.release_s = float(release_s)
        self._g        = 1.0   # gain-reduction state (1.0 = no reduction)

    def process(self, chans):
        """chans = list of float32 buffers (1 = mono, 2 = stereo L/R), modified in
        place-safe (returns new arrays). Same math as SynthEngine._limit's inner
        block, minus the master multiply (the host applies its own master first)."""
        n = chans[0].size if chans else 0
        if n == 0:
            return chans
        peak = 0.0
        for b in chans:
            if b.size:
                p = float(np.max(np.abs(b)))
                if p > peak:
                    peak = p
        target = 1.0 if peak <= self.ceil else self.ceil / peak
        g0 = self._g
        if target < g0:
            # fast attack: whole block at target -> guaranteed no overshoot
            g1   = target
            ramp = np.full(n, target, dtype=np.float32)
        else:
            # smooth release back up toward unity (~80ms time constant)
            rel  = float(np.exp(-n / (self.release_s * SAMPLE_RATE)))
            g1   = target + (g0 - target) * rel
            ramp = np.linspace(g0, g1, n, dtype=np.float32)
        self._g = g1
        return [(b * ramp).astype(np.float32) for b in chans]

# -- Live volume controls (master + per-engine) --------------------------------
# Four "volume" CCs arrive on the control channel (default 13) of the internal
# synth bus and set, respectively, the overall master, poly, chip, and drum-bus
# levels. That channel carries nothing else on the bus, so these can't collide
# with any note or param CC. Single source of truth: orc1_config.SYNTH_VOL
# (the Fire faders + web LEVELS sliders send exactly these CCs). Fallbacks below
# match the config so the synth still works if the dict is ever absent.
try:
    import orc1_config as _volcfg
    _SYNTH_VOL = getattr(_volcfg, "SYNTH_VOL", {})
    _VOL_CH    = int(getattr(_volcfg, "CH_CTRL", 13))
except Exception:
    _SYNTH_VOL, _VOL_CH = {}, 13
_VOL_CH        = int(_SYNTH_VOL.get("CH", _VOL_CH))
_VOL_MASTER_CC = int(_SYNTH_VOL.get("MASTER_CC", 7))
_VOL_POLY_CC   = int(_SYNTH_VOL.get("POLY_CC",   8))
_VOL_CHIP_CC   = int(_SYNTH_VOL.get("CHIP_CC",   9))
_VOL_DRUM_CC   = int(_SYNTH_VOL.get("DRUM_CC",  10))
_VOL_MONO_CC   = int(_SYNTH_VOL.get("MONO_CC",  11))   # ORC1-Mono bus level
_MONO_BOOT_CC  = max(0, min(127, int(_SYNTH_VOL.get("MONO_BOOT", 64))))
_FXMIX_MIX_CC   = int(_SYNTH_VOL.get("FXMIX_MIX_CC",   3))  # ORC1-FXMix wet/dry
_FXMIX_LEVEL_CC = int(_SYNTH_VOL.get("FXMIX_LEVEL_CC", 4))  # ORC1-FXMix return level

# The OTHER way poly.master / chip.master get written: CC 13 on a voice channel,
# decoded by PolySynth.set_cc / NESEngine.set_cc. That is what the /synth and
# /chip page LEVEL sliders and the Fire's 'synvol'/'chipvol' knobs send, so the
# bus-level persistence has to watch this CC as well as the CH_CTRL pair above.
_ENGINE_MASTER_CC = 13

# Drum-bus startup gain, and the single source of truth for it. Clamped and
# defaulted because SYNTH_VOL.DRUM_BOOT is reachable from the /system CONFIG
# OVERRIDES card via ~/.orc1_config_overrides.json -- a typo there must not be
# able to stop the synth booting (total silence, no obvious cause) or hand the
# RT path a >unity drum bus.
try:
    _DRUM_BOOT_CC = max(0, min(127, int(_SYNTH_VOL.get("DRUM_BOOT", 64))))
except Exception:
    _DRUM_BOOT_CC = 64

# Debounce window for monophonic chip voices.  Two note-ons for the same note
# arriving within this window are treated as one held note (pad double-trigger
# protection).  30ms is tight enough to not affect fast intentional playing.
_MONO_DEBOUNCE_S = 0.030

# NES dual-voice coupling modes for P1+P2.
# When active, playing a note on pulse1 automatically drives pulse2 too.
NES_COUPLE_MODES = ['none', 'unison', 'detune', 'fifth', 'third', 'octave', 'octave_dn']

# Semitone offsets for interval modes (pulse2 relative to pulse1)
_NES_COUPLE_SEMIS = {
    'none':      None,
    'unison':    0,
    'detune':    0,     # same semitone, pitch bent in cents -- special case
    'fifth':     7,
    'third':     4,
    'octave':    12,
    'octave_dn': -12,
}
# (The retired single-process synth's per-bus outs + main-mix coupling --
#  BUS_OUTPUTS / BUS_NAMES / MAIN_MIX_BUSES / ~/.orc1_mainmix.json -- were
#  removed 2026-09-24. Each module host now owns its own out_L/out_R, see
#  autoconnect_module_out above.)

# -- GM drum note map (note number -> voice name) -------------------------------
DEFAULT_DRUM_NOTE_MAP = {
    36: "bass_drum",
    38: "snare",
    40: "snare",       # electric snare -> same voice
    41: "low_tom",
    43: "mid_tom",
    47: "mid_tom",
    50: "hi_tom",
    45: "hi_tom",
    37: "rimshot",
    39: "clap",
    42: "closed_hat",
    44: "closed_hat",  # pedal hat
    46: "open_hat",
    49: "cymbal",
    51: "ride",        # 2026-09-23: the 909's own ride (was folded onto the crash)
    53: "ride_bell",   # GM ride bell (its own voice, 2026-09-23)
    55: "splash",      # GM splash cymbal (was silent)
    48: "himid_tom",   # GM hi-mid tom (was a duplicate of hi_tom)
    59: "ride",        # GM ride cymbal 2
    57: "cymbal",
    56: "cowbell",
}
DRUM_NOTE_MAP = {**DEFAULT_DRUM_NOTE_MAP, **_SYNTH_CFG.get("DRUM_NOTE_MAP", {})}

# Choke groups: triggering any member instantly silences the others (909 hats).
_CHOKE_GROUPS = [
    {"closed_hat", "open_hat"},          # closed cuts open -- standard 909
    {"low_tom", "mid_tom", "hi_tom"},    # toms cut each other -- stops freq entanglement on rapid fills
]
_CHOKE = {n: g for g in _CHOKE_GROUPS for n in g}

# -- Drum CC map (data-driven per-voice, 2026-07-21 redesign) ------------------
# Single source of truth: cfg.DRUM_CC (orc1_config.py) packs each voice's
# VOICE_PARAMS into consecutive CCs from BASE and exposes the computed maps. The
# synth DECODES via CC_TO_VP {cc:(voice,param)}; the Fire/Mixtrack ENCODE via
# LAYOUT {voice:{start,params}}. The old rigid 8-CC stride, the HP band, and the
# in-drum sample slots are all gone (see cfg notes). The fallback below only
# covers running without the DRUM_CC config block and mirrors the same packing.
DRUM_CC_BASE = int(_DRUM_CC_CFG.get("BASE", 20))


def _fallback_drum_layout():
    """Same packing as cfg._build_drum_cc_layout — only used if the config
    block is missing. Returns (cc_to_vp {cc:(voice,param)}, voice_off {voice:off})."""
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
    cc_to_vp, off = {}, {}
    cc = DRUM_CC_BASE
    for v, ps in vp.items():
        off[v] = cc - DRUM_CC_BASE
        for p in ps:
            cc_to_vp[cc] = (v, p)
            cc += 1
    return cc_to_vp, off


_cfg_cc_to_vp = _DRUM_CC_CFG.get("CC_TO_VP")
if _cfg_cc_to_vp:
    # config keys may be int already; normalise to int just in case.
    DRUM_CC_TO_VP = {int(k): tuple(v) for k, v in _cfg_cc_to_vp.items()}
    DRUM_VOICE_CC_OFFSET = dict(_DRUM_CC_CFG.get("VOICE_OFFSET", {}))
else:
    DRUM_CC_TO_VP, DRUM_VOICE_CC_OFFSET = _fallback_drum_layout()

# Sample playback slots — RELOCATED to the sampler section 2026-07-21. The WAV
# voices are still created in SynthEngine.voices as smpl_0..N and rendered by the
# drum bus (identical DSP), but they are TRIGGERED and SCULPTED from CH_SAMPLER
# now (SAMPLER["ONESHOT_*"]), not the drum channel — see _route_sampler. So they
# are NO LONGER registered into DRUM_NOTE_MAP or the drum CC map.
N_SAMPLE_SLOTS = int(_SAMPLER_CFG.get("ONESHOT_SLOTS", 4))
SAMPLES_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

# One-shot sample slots are DRUM VOICES again (2026-09-02, audio host split):
# they trigger on CH_DRUM at notes ONESHOT_NOTE_BASE..+N-1 (the 60..63 they
# always used) and their sculpt CCs are packed into the drum CC map from
# cfg.DRUM_CC VOICE_PARAMS (smpl_0..N appended after cowbell -> CC 79+). This
# reverses the 2026-07-21 relocation to CH_SAMPLER: the sampler channel is now
# purely the live-remix decks, which run in their own process (orc1_sampler_host).
_OS_NOTE_BASE = int(_SAMPLER_CFG.get("ONESHOT_NOTE_BASE", 60))
for _i in range(N_SAMPLE_SLOTS):
    DRUM_NOTE_MAP.setdefault(_OS_NOTE_BASE + _i, f"smpl_{_i}")


# ==============================================================================
# FILTER
# ==============================================================================

class StateVariableFilter:
    """
    Two-pole state variable filter -- LP, HP, or BP.
    Chamberlin topology: stable, tunable at runtime.
    The sample loop is Python but runs on small buffers so it's fast enough.
    """
    def __init__(self, cutoff_hz, resonance=0.0, mode='LP'):
        self.sr       = SAMPLE_RATE
        self.mode     = mode
        self.low      = 0.0
        self.band     = 0.0
        self._bypass  = False  # set True on transparent instances; cleared on first real set()
        self.set(cutoff_hz, resonance)

    def set(self, cutoff_hz=None, resonance=None):
        if cutoff_hz  is not None:
            fc        = np.clip(cutoff_hz, 20, self.sr * 0.47)
            self.f    = float(2 * np.sin(np.pi * fc / self.sr))
            self.f    = min(self.f, 1.95)
        if resonance  is not None:
            self.q    = float(1.0 - np.clip(resonance, 0.0, 0.98))
        # Caller may enable bypass before set() is called; leave it alone here.
        # Bypass is only cleared explicitly by callers that know the filter is
        # now doing real work (see DrumVoice.set_param / SampleVoice.set_param).

    def process(self, signal):
        # Bypass: return input untouched when filter is at a transparent default
        # (e.g. HP@20Hz or LP@18kHz with zero resonance).  The flag is cleared
        # the first time the user actually dials in a non-transparent setting.
        if self._bypass:
            return signal
        out  = np.empty_like(signal)
        low  = self.low
        band = self.band
        f    = self.f
        q    = self.q
        mode = self.mode
        for i, x in enumerate(signal):
            low  = low  + f * band
            high = x    - low - q * band
            band = f    * high + band
            # Safety net (2026-07-28, found while testing fast param sweeps):
            # a Chamberlin SVF isn't guaranteed stable while its coefficients
            # are being modulated with energy already in the state -- certain
            # resonance/cutoff combinations under a fast sweep can diverge to
            # NaN/Inf (reproduced independent of the 2026-07-28 smoothing
            # work -- pre-existing). Once that happens the recursion NEVER
            # recovers on its own, and if a non-finite sample reaches a
            # shared bus it can poison everything summed with it. Clamp each
            # state variable back to 0 the instant it goes non-finite or
            # absurdly large for a normalized audio signal -- costs at most
            # one sample's discontinuity instead of a permanent lockup.
            if not (math.isfinite(low) and -8.0 < low < 8.0):
                low = 0.0
            if not (math.isfinite(band) and -8.0 < band < 8.0):
                band = 0.0
            if not (math.isfinite(high) and -8.0 < high < 8.0):
                high = 0.0
            out[i] = low if mode == 'LP' else (high if mode == 'HP' else band)
        self.low  = low
        self.band = band
        return out


# scipy's C lfilter WITHOUT importing scipy.signal (~48 MB private per host on
# the Pi, x every host). Bit-identical output; falls back to the real import,
# then to None (= filter bypassed). See orc1_lfilter.py.
try:
    from orc1_lfilter import lfilter as _lfilter
except Exception:                     # pragma: no cover - scipy always present on Pi
    _lfilter = None


class BiquadFilter:
    """
    Vectorised RBJ biquad -- LP / HP / BP -- run through scipy.signal.lfilter so a
    continuously-held chord costs microseconds in C, NOT a per-sample Python
    loop (which is why this, not StateVariableFilter, sits on the sustained
    poly/mono output). Coefficients are recomputed only when a parameter
    changes; the two-sample filter state (zi) is carried across JACK blocks so
    there are no clicks at buffer boundaries.

    A wide-open low-pass bypasses entirely (returns the input untouched), so the
    filter is free and tonally transparent until you actually dial the cutoff
    down -- and it degrades to a clean bypass if scipy is somehow unavailable.
    """
    MODES = ('LP', 'HP', 'BP')

    def __init__(self, sr, cutoff=18000.0, q=0.707, mode='LP'):
        self.sr     = sr
        self.cutoff = float(cutoff)
        self.q      = float(q)
        self.mode   = mode if mode in self.MODES else 'LP'
        self._zi    = np.zeros(2, dtype=np.float64)
        self._design()

    def set(self, cutoff=None, q=None, mode=None):
        if cutoff is not None:
            self.cutoff = float(np.clip(cutoff, 20.0, self.sr * 0.45))
        if q is not None:
            self.q = float(max(0.3, q))
        if mode is not None and mode in self.MODES:
            self.mode = mode
        self._design()

    def _design(self):
        # Transparent when a low-pass is opened to the top of its range.
        self._bypass = (self.mode == 'LP' and self.cutoff >= 17999.0)
        w0    = 2.0 * np.pi * self.cutoff / self.sr
        cw    = np.cos(w0)
        sw    = np.sin(w0)
        alpha = sw / (2.0 * self.q)
        if self.mode == 'HP':
            b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        elif self.mode == 'BP':
            b0, b1, b2 = alpha, 0.0, -alpha
        else:                                            # LP
            b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0 = 1 + alpha
        self._b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
        self._a = np.array([1.0, (-2 * cw) / a0, (1 - alpha) / a0],
                           dtype=np.float64)

    def process(self, x):
        if self._bypass or _lfilter is None:
            return x
        y, self._zi = _lfilter(self._b, self._a, x, zi=self._zi)
        return y.astype(np.float32)


# -- CC -> synth-filter parameter maps (shared by MonoSynth + PolySynth) ---------

def _cc_to_cutoff(v):
    """0..127 -> 80 Hz .. 18 kHz, exponential (musical), 127 == open/bypass."""
    return 80.0 * (18000.0 / 80.0) ** (v / 127.0)

def _cc_to_q(v):
    """0..127 -> 0.707 (flat) .. ~10 (sharp resonant peak)."""
    return 0.707 + (v / 127.0) * 9.3

def _cc_to_mode(v):
    """0..42 LP, 43..85 HP, 86..127 BP."""
    return 'LP' if v < 43 else ('HP' if v < 86 else 'BP')

def _cc_to_octave(v):
    """0..127 -> -2..+2 octaves, centred (64 ≈ 0)."""
    return int(round(v / 127.0 * 4)) - 2


# -- Pitch-tracked ring modulation (2026-08-05) --------------------------------
# A hardware ring modulator (the Moog ones owner was asking about) needs a
# pitch-to-CV converter to make the carrier follow what you play -- and that
# tracker has to GUESS the fundamental from audio, which is what makes it glitch
# on chords, transients and low notes. Inside this file there is nothing to
# guess: every PolyVoice already stores the exact frequency of its own note. So
# the carrier is simply freq x ratio, per voice, and the tracking is exact.
#
# Why that matters musically: the sidebands land at f*(1±ratio), so the interval
# between them and the note is CONSTANT across the keyboard. A fixed-Hz carrier
# (the classic untracked behaviour, still available on CC15) instead gives an
# interval that changes with every note -- consonant in one octave, clangorous
# in the next. That is the whole difference tracking buys you.

_RM_RATIOS = (
    0.25,       # two octaves below the note
    0.5,        # one octave below
    0.75,
    1.0,        # unison: sidebands at DC and the octave -- hollow, sub-heavy
    1.25,       # major third
    1.3333333,  # perfect fourth
    1.4142136,  # irrational -> inharmonic, the classic bell
    1.5,        # perfect fifth
    2.0,        # octave: leaves f and 3f -- hollow, square-ish
    2.5,
    3.0,
    3.5,
    4.0,
    5.0,
    6.0,
    8.0,
)

_RM_DEPTH_RAMP_S = 0.008    # depth smoothing; see RingMod / PolyVoice._rm_gain


def _cc_to_rm_ratio(v):
    """0..127 -> one of the 16 _RM_RATIOS detents (8 CC steps each)."""
    return _RM_RATIOS[min(len(_RM_RATIOS) - 1, v // 8)]


def _cc_to_rm_hz(v):
    """0 -> 0.0 (= pitch-tracking mode); 1..127 -> 20 Hz..4 kHz exponentially.

    Returning exactly 0.0 for CC 0 is load-bearing: RingMod treats >0 as "use a
    fixed carrier", so the bottom of the knob is how you get tracking back."""
    if v <= 0:
        return 0.0
    return float(20.0 * (4000.0 / 20.0) ** ((v - 1) / 126.0))


class RingMod:
    """Live-editable ring-mod settings shared by every voice of one engine.

    Voices hold a REFERENCE to this object, not a copy of its values. That is
    deliberate: waveform and ADSR are pushed onto the voice at note-on, which is
    fine for things you set up before playing, but it would make a ring-mod knob
    DEAD during a sustained chord -- exactly when you want to sweep it. Reading
    through the reference costs one attribute lookup per block and makes the
    control live. NESEngine already does the same thing by hand for CC1 duty.
    """
    __slots__ = ('depth', 'ratio', 'fixed_hz')

    def __init__(self):
        self.depth    = 0.0     # 0 = bypass, and the render path stays free
        self.ratio    = 1.0     # carrier = ratio x the note's own frequency
        self.fixed_hz = 0.0     # >0 overrides ratio: untracked, classic mode


class OnePoleLPF:
    """Fast single-pole LPF for smoothing (e.g. portamento, envelope follower)."""
    def __init__(self, cutoff_hz):
        self._set(cutoff_hz)
        self.y = 0.0

    def _set(self, hz):
        self.alpha = float(1 - np.exp(-2 * np.pi * hz / SAMPLE_RATE))

    def process(self, signal):
        out   = np.empty_like(signal)
        y     = self.y
        alpha = self.alpha
        for i, x in enumerate(signal):
            y     = y + alpha * (x - y)
            out[i] = y
        self.y = y
        return out

    def set_cutoff(self, hz):
        self._set(hz)


# ==============================================================================
# ADSR (for mono synth -- sample-accurate state machine)
# ==============================================================================

class ADSR:
    """
    Classic ADSR envelope generator, sample-accurate.
    Uses linear attack, exponential decay/release (more musical).
    """
    def __init__(self, attack=0.005, decay=0.1, sustain=0.7, release=0.2):
        self.sr       = SAMPLE_RATE
        self.attack   = attack
        self.decay    = decay
        self.sustain  = sustain
        self.release  = release
        self.state    = 'idle'
        self.level    = 0.0
        self.velocity = 1.0

    def trigger(self, velocity=1.0):
        self.velocity = velocity / 127.0
        self.state    = 'attack'

    def release_note(self):
        if self.state != 'idle':
            self.state = 'release'

    def process(self, n_frames):
        out = np.empty(n_frames, dtype=np.float32)
        for i in range(n_frames):
            if self.state == 'idle':
                self.level = 0.0
            elif self.state == 'attack':
                self.level += 1.0 / max(1, self.attack * self.sr)
                if self.level >= 1.0:
                    self.level = 1.0
                    self.state = 'decay'
            elif self.state == 'decay':
                # Exponential decay toward sustain
                target  = self.sustain
                self.level += (target - self.level) * (1 - np.exp(-1 / max(1, self.decay * self.sr * 0.2)))
                if abs(self.level - target) < 1e-4:
                    self.level = target
                    self.state = 'idle' if target < 1e-4 else 'sustain'
            elif self.state == 'sustain':
                self.level = self.sustain
            elif self.state == 'release':
                self.level *= np.exp(-1 / max(1, self.release * self.sr * 0.2))
                if self.level < 1e-5:
                    self.level = 0.0
                    self.state = 'idle'
            out[i] = self.level * self.velocity
        return out

    @property
    def active(self):
        return self.state != 'idle'


# ==============================================================================
# DRUM VOICES
# ==============================================================================

class DrumVoice:
    """Base class for all drum voices. Subclasses implement _render_signal()."""
    def __init__(self, name):
        self.name       = name
        self.sr         = SAMPLE_RATE
        self.active     = False
        self.age        = 0        # samples since last trigger
        self.velocity   = 1.0
        self.level      = 0.8
        self.tune       = 0.0      # semitone offset
        self.attack_time= 0.002    # wave designer attack
        self.decay_time = 0.3
        # Generic smoothing (2026-07-28): any CC-driven float can be routed
        # through _glide_to()/_apply_glides() instead of being snapped the
        # instant a CC arrives. Turning a knob while a voice is mid-ring
        # changes that value DURING active playback -- since the envelope/
        # oscillator/filter is evaluated continuously from age-since-trigger,
        # an instant jump is a real discontinuity in the output between one
        # JACK block and the next: an audible click/step, worse the faster
        # you turn the knob (more CC messages/sec = more discontinuities/sec).
        # Used for decay/level/pitch/filter-cutoff-type params everywhere
        # below; NOT used for things that are already discrete by nature
        # (booleans, choke state) or genuinely unreachable via any CC today
        # (the HP band, removed from the addressable map 2026-07-21).
        self._glide_targets = {}                          # {attr_name: target_value}
        self._GLIDE_FRAMES  = max(1, int(self.sr * 0.03))  # ~30ms time constant (matches the sampler's tone glide)
        self._choking   = False    # choke-group fast fade-out
        self._choke_pos = 0
        self._last_sample  = 0.0   # last rendered sample value (for retrigger de-click)
        self._retrig_from  = 0.0   # non-zero while fading out the old hit's tail
        self._retrig_pos   = 0
        self._RETRIG_FADE  = max(1, int(self.sr * 0.001))  # 1ms = 48 samples
        self.hp_filt    = StateVariableFilter(20, resonance=0.0, mode="HP")  # post HP, 20Hz = transparent
        self.hp_filt._bypass = True   # no-op until user dials hp_freq/hp_res above defaults

    def trigger(self, velocity_int):
        # Flush any pending glide straight to its target before the hit
        # starts (2026-07-28 review fix): render() skips _apply_glides()
        # entirely while a voice is inactive, so a CC that arrived while
        # this voice was silent would otherwise sit queued and then audibly
        # slide into place over the first ~30ms of the NEW hit instead of
        # just playing at the value that was actually dialed in -- a real
        # regression vs. the old instant-apply behavior for the (very
        # common) "adjust between hits, then play" workflow. No audio is
        # playing yet, so there's no click risk from applying it instantly.
        if self._glide_targets:
            for attr, target in self._glide_targets.items():
                setattr(self, attr, target)
            self._glide_targets.clear()
        if self.active and abs(self._last_sample) > 1e-5:
            self._retrig_from = self._last_sample
            self._retrig_pos  = 0
        else:
            self._retrig_from = 0.0
        self.active    = True
        self.age       = 0
        self.velocity  = velocity_int / 127.0
        self._choking  = False   # cancel any in-progress choke -- new hit starts clean
        self._choke_pos = 0
        self._on_trigger()

    def _on_trigger(self):
        """Called at trigger time. Subclasses reset oscillator state here."""
        pass

    def _glide_to(self, attr, target):
        """Queue `attr` to ease toward `target` -- see _apply_glides()."""
        self._glide_targets[attr] = target

    def _apply_glides(self, n_frames):
        """Advance every pending glide by n_frames worth of the shared ~30ms
        time constant. Call once per render(), before anything reads a
        glided attribute (subclasses that add their own glided attrs, e.g. a
        filter cutoff, must call this too -- it's already called once via
        _envelope(), which every voice's render() calls first).

        Uses an exponential step (1 - exp(-n_frames/_GLIDE_FRAMES)) rather
        than a linear-clamped fraction (2026-07-28 review fix): a linear
        `min(1.0, n_frames/_GLIDE_FRAMES)` reaches exactly 1.0 -- an instant
        full snap in a SINGLE block -- whenever the JACK period is >=
        _GLIDE_FRAMES. This Pi runs period 1024 (~21ms @48kHz), which is
        bigger than a naive 20ms target, so the original formula collapsed
        to a one-block snap on this exact hardware -- defeating the whole
        fix. The exponential form asymptotically approaches but never
        reaches 1.0 in one step, so the glide always spreads across
        multiple blocks no matter how large a single JACK period is
        (period-agnostic, matching the project's hardware-agnostic goal)."""
        if not self._glide_targets:
            return
        g = 1.0 - math.exp(-n_frames / self._GLIDE_FRAMES)
        for attr in list(self._glide_targets.keys()):
            target = self._glide_targets[attr]
            cur = getattr(self, attr)
            cur += (target - cur) * g
            if abs(cur - target) < 1e-5:
                cur = target
                del self._glide_targets[attr]
            setattr(self, attr, cur)

    def set_param(self, param, cc_value):
        """Generic CC setter -- subclasses may override for extra params."""
        v = cc_value / 127.0
        if param == 'level':
            self._glide_to('level', v)
        elif param == 'attack':
            self._glide_to('attack_time', _cc_to_time(cc_value, 0.001, 0.5))
        elif param == 'decay':
            # 2026-07-28: ceiling raised 2s -> 10s. The exponential curve was
            # already correct (short values get fine resolution, long values
            # sweep fast) -- the problem was the ceiling itself: at 2s max,
            # "5 seconds" wasn't reachable at all, so the whole top of the
            # dial only ever bought you a middling ~2s. With a 10s ceiling
            # the SAME curve puts 5s at ~89% of the dial's travel, so most of
            # the knob covers the musically useful range and only the last
            # ~11% reaches into genuinely long territory.
            self._glide_to('decay_time', _cc_to_time(cc_value, 0.02, 10.0))
        elif param == 'tune':
            # Dead in the base class -- every voice overrides 'tune' with its
            # own pitch mapping (freq_hi/freq_lo/tune_factor/etc. below), so
            # this plain attribute is never actually read. Left instant.
            self.tune        = (cc_value - 64) / 64.0 * 24  # ±24 semitones
        elif param == 'hp_freq':
            # HP band removed from the addressable CC map 2026-07-21 --
            # unreachable via any CC today (see CLAUDE.md). Left instant;
            # not worth gliding a control that can never be live-turned.
            self.hp_filt._bypass = False   # filter is now doing real work
            self.hp_filt.set(cutoff_hz=20 + cc_value / 127.0 * 3000)
        elif param == 'hp_res':
            self.hp_filt._bypass = False
            self.hp_filt.set(resonance=cc_value / 127.0 * 0.8)

    def _envelope(self, n_frames):
        """
        Analytical percussive envelope: linear attack + exponential decay.
        Returns float32 numpy array.
        """
        # Ease every pending glide (decay_time/level/attack_time/etc., plus
        # whatever a subclass queued) BEFORE using them below -- this is the
        # fix for the turn-it-fast click/step bug: a live knob change now
        # moves smoothly across ~20ms instead of snapping mid-ring.
        self._apply_glides(n_frames)

        idx  = np.arange(self.age, self.age + n_frames, dtype=np.float32)
        t    = idx / self.sr

        atk_samps = max(1, self.attack_time * self.sr)
        attack    = np.minimum(idx / atk_samps, 1.0)

        t_decay   = np.maximum(0.0, t - self.attack_time)
        decay     = np.exp(-_RT60_K * t_decay / max(1e-4, self.decay_time))

        env       = attack * decay * self.velocity * self.level
        return env.astype(np.float32)

    def _is_silent(self, env):
        return bool(env[-1] < 5e-5)

    def choke(self):
        """Fast fade-to-silence for choke groups (e.g. closed hat cuts open
        hat). ~6ms fade so there's no click."""
        if self.active:
            self._choking   = True
            self._choke_pos = 0

    def render(self, n_frames):
        if not self.active:
            return np.zeros(n_frames, dtype=np.float32)
        env    = self._envelope(n_frames)
        signal = self._render_signal(n_frames, env)
        signal = self.hp_filt.process(signal)
        if self._retrig_from != 0.0:
            fl = self._RETRIG_FADE
            remaining = fl - self._retrig_pos
            if remaining > 0:
                n = min(remaining, n_frames)
                k = np.arange(self._retrig_pos, self._retrig_pos + n, dtype=np.float32)
                signal[:n] = signal[:n] + self._retrig_from * (1.0 - k / fl)
                self._retrig_pos += n
            if self._retrig_pos >= fl:
                self._retrig_from = 0.0
        if self._choking:
            fl = max(1, int(0.006 * self.sr))
            k  = np.arange(self._choke_pos, self._choke_pos + n_frames, dtype=np.float32)
            signal = signal * np.clip(1.0 - k / fl, 0.0, 1.0).astype(np.float32)
            self._choke_pos += n_frames
            if self._choke_pos >= fl:
                self.active   = False
                self._choking = False
        self.age += n_frames
        # Only cull a voice AFTER it's past its attack ramp. During the attack
        # the envelope legitimately sits near zero, so _is_silent() (which looks
        # at the last sample of the segment just rendered) would otherwise kill a
        # note that was just triggered mid-block in a very short render segment.
        # The sample-accurate-starts fix (2026-07-20) splits each block into
        # segments at every note offset; a hit landing near the block end (or
        # an _ev_offset overshoot clamped to n_frames-1) produces a 1-sample
        # segment whose only sample is the attack's zero point -> the voice was
        # deactivated the instant it triggered and rendered silence forever
        # after. THAT was the "randomly dropped drum hits" bug (fixed 2026-07-21).
        # atk_samps mirrors _envelope()'s attack length; before the ramp is done
        # a near-zero env means "just started", not "finished decaying".
        atk_samps = max(1, self.attack_time * self.sr)
        if self.age >= atk_samps and self._is_silent(env):
            self.active = False
        if signal.size:
            self._last_sample = float(signal[-1])
        return signal

    def _render_signal(self, n_frames, env):
        raise NotImplementedError


def _cc_to_time(cc, t_min, t_max):
    """Map CC 0-127 to time range with exponential scaling."""
    return float(t_min * (t_max / t_min) ** (cc / 127.0))


# RT60 convention (2026-07-28): the standard way to define a "decay time" in
# audio (same idea as reverb RT60) is "time to drop by 60dB" (1/1000 amplitude),
# NOT a raw 1/e exponential time constant. Every decay-shaped envelope below
# used to treat decay_time/click_time as a bare tau in exp(-t/tau), which only
# reaches 37% amplitude at t=tau and needs ~5x tau to become inaudible -- e.g.
# a "500ms" kick decay was actually ringing for ~2.3-3.5s in practice, which is
# why it kept getting cut off by the next hit regardless of how far away it
# was, and why almost the entire CC range felt like "too long". Multiplying
# the exponent by _RT60_K makes decay_time/click_time mean what they say:
# roughly inaudible BY that many seconds, not merely "somewhat quieter".
_RT60_K = math.log(1000.0)   # ln(1000) =~ 6.908 -- exp(-6.908) = -60dB


def _tune_factor(semitones):
    return 2.0 ** (semitones / 12.0)


# -- Bass Drum -----------------------------------------------------------------

class BassDrum(DrumVoice):
    """
    909 bass drum: sine oscillator with exponential pitch sweep.

    The click IS the pitch sweep -- the oscillator starts at freq_hi and drops
    to freq_lo over pitch_decay seconds.  This is how the TR-808 circuit works:
    the same oscillator briefly runs at a higher frequency at trigger time,
    producing the knock character via the rapid downward pitch transition.  No
    separate noise source is needed or wanted -- a separate high-frequency noise
    burst lands slightly off-time (filter settling delay) and sounds like a
    different instrument layered on top rather than part of the hit.

    attack controls sweep depth (freq_hi/freq_lo ratio) and sweep speed
    (pitch_decay).  0 = no sweep = pure sine, no click.  127 = dramatic pitch
    drop + fast sweep = hard, 808-style knock.

    CC map:
      level       -- overall level
      tune        -- pitch (scales freq_hi and freq_lo together; preserves
                     the attack ratio so tune and attack don't fight)
      attack      -- click/knock character: 0 = none, 127 = hard knock
      decay       -- body sustain length
      filter_freq -- drive: body saturation / soft-clip amount
      filter_res  -- body-lo frequency trim (sub-bass fundamental)
    """
    def __init__(self):
        super().__init__("bass_drum")
        # _attack_ratio = freq_hi / freq_lo; stored so tune can maintain it.
        # Default 5.0 → freq_hi=250 Hz at the default freq_lo=50 Hz, which is
        # the code-default medium knock character (≈ attack CC 73).
        self._attack_ratio = 5.0
        self.freq_hi     = 250.0   # Hz at trigger  (= freq_lo * _attack_ratio)
        self.freq_lo     = 50.0    # Hz at rest
        self.pitch_decay = 0.015   # seconds for pitch sweep (tighter than old 0.05)
        self.decay_time  = 0.5
        self.attack_time = 0.004   # body envelope attack -- always fast
        self.drive       = 0.3     # body saturation amount (0 – 0.9)
        self.phase       = 0.0

    def _on_trigger(self):
        self.phase = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            tf     = _tune_factor((cc_value - 64) / 64.0 * 24)
            new_lo = 50.0 * tf
            self._glide_to('freq_lo', new_lo)
            # Maintain the current attack ratio so tune and attack are
            # independent -- changing pitch doesn't reset click character.
            self._glide_to('freq_hi', new_lo * self._attack_ratio)
        elif param == 'attack':
            # Sweep DEPTH: ratio of freq_hi to freq_lo.
            # 0 = freq_hi = freq_lo (no pitch sweep, no click).
            # 127 = freq_hi = 8 × freq_lo (dramatic pitch drop, hard knock).
            ratio = 1.0 + cc_value / 127.0 * 7.0
            self._attack_ratio = ratio
            self._glide_to('freq_hi', self.freq_lo * ratio)
            # Sweep SPEED: shorter pitch_decay at higher attack for extra snap.
            # 127 → ~6 ms (808-like); 0 → ~35 ms (slow, soft onset; moot when
            # ratio=1 and there is nothing to sweep).
            self._glide_to('pitch_decay', _cc_to_time(127 - cc_value, 0.006, 0.035))
            # Body attack_time always stays fast -- override super()'s glide.
            self._glide_to('attack_time', 0.004)
        elif param == 'filter_freq':
            # Drive: body saturation / distortion amount
            self._glide_to('drive', cc_value / 127.0 * 0.9)
        elif param == 'filter_res':
            # Sub-bass fundamental trim (also written by 'tune' above -- last
            # write wins, same pre-existing behaviour as before this change)
            self._glide_to('freq_lo', 30.0 + cc_value / 127.0 * 80.0)

    def _render_signal(self, n_frames, env):
        t      = np.arange(self.age, self.age + n_frames, dtype=np.float64) / self.sr
        # Exponential pitch sweep: freq_hi → freq_lo over pitch_decay seconds.
        # The rapid initial pitch drop IS the click -- same mechanism as the
        # TR-808 circuit.  No separate noise source.
        freq   = self.freq_lo + (self.freq_hi - self.freq_lo) * np.exp(-t / self.pitch_decay)
        dp     = 2 * np.pi * freq / self.sr
        phases = self.phase + np.cumsum(dp)
        self.phase = float(phases[-1] % (2 * np.pi))
        sine   = np.sin(phases).astype(np.float32)

        # Body: drive/saturate then shape with main envelope
        body = sine.copy()
        if self.drive > 0.01:
            k    = 1 + self.drive * 12
            body = (np.tanh(body * k) / np.tanh(k)).astype(np.float32)
        return body * env


# -- Snare ---------------------------------------------------------------------

class Snare(DrumVoice):
    """
    909 snare: two sine tones (body) + bandpass-filtered noise (snap).

    The key insight from the TR-909 circuit: tone and noise have SEPARATE
    envelopes.  The noise decays fast (the percussive "crack"), while the
    sine body can sustain -- giving that characteristic snap-then-ring.

    CC map (base offset 6, CCs 26-31 on CH_DRUM):
      level       -- overall level
      tune        -- body pitch (shifts both sine frequencies)
      attack      -- "Snappy": tone/noise balance (0=all snap, 127=all body).
                    Attack time is always 2 ms -- snares are percussive.
      decay       -- tone body decay time (0.02 – 2.0 s)
      filter_freq -- noise BP centre 200 – 4200 Hz (noise colour/character)
      filter_res  -- snap length: noise envelope decay 10 – 400 ms
    """
    def __init__(self):
        super().__init__("snare")
        self.freq1       = 185.0
        self.freq2       = 330.0
        self.tone_mix    = 0.5      # 0 = all noise/snap, 1 = all tone/body
        self.decay_time  = 0.2
        self.attack_time = 0.002    # always short -- snare is always percussive
        self.noise_decay = 0.06     # noise snap envelope (60 ms default)
        self.phase1      = 0.0
        self.phase2      = 0.0
        # BP on noise: sweep centre for the classic 909 snap character.
        # Resonance fixed at 0.2 -- wide enough to sound natural.
        # filter_res CC controls noise_decay (snap length) instead.
        self._noise_cutoff = 2000.0   # glide target/actual for noise_filt's centre freq
        self.noise_filt  = BiquadFilter(SAMPLE_RATE, cutoff=self._noise_cutoff, q=1.5, mode='BP')

    def _on_trigger(self):
        self.phase1 = 0.0
        self.phase2 = 0.0
        # Reset filter state so every hit starts clean regardless of tempo
        self.noise_filt._zi[:] = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            tf = _tune_factor((cc_value - 64) / 64.0 * 24)
            self._glide_to('freq1', 185.0 * tf)
            self._glide_to('freq2', 330.0 * tf)
        elif param == 'attack':
            # "Snappy" -- tone/noise balance only.
            # Override the base-class attack_time: snare is always percussive
            # (re-queue the glide target too -- see BassDrum's 'attack' for why).
            self._glide_to('attack_time', 0.002)
            self._glide_to('tone_mix', cc_value / 127.0)
        elif param == 'filter_freq':
            # Noise BP centre 200–4200 Hz
            self._glide_to('_noise_cutoff', 200 + cc_value / 127.0 * 4000)
        elif param == 'filter_res':
            # Snap length: noise envelope decay 10–400 ms
            self._glide_to('noise_decay', _cc_to_time(cc_value, 0.01, 0.4))

    def _render_signal(self, n_frames, env):
        # Re-apply the (possibly still-gliding) noise BP centre every block --
        # cheap (a few coefficient recalcs) and means a filter_freq sweep
        # doesn't snap the coefficients mid-ring either.
        self.noise_filt.set(cutoff=self._noise_cutoff)

        # -- Tone body (two sines, main envelope) -----------------------------
        dp1  = 2 * np.pi * self.freq1 / self.sr
        dp2  = 2 * np.pi * self.freq2 / self.sr
        p1   = self.phase1 + np.arange(1, n_frames + 1) * dp1
        p2   = self.phase2 + np.arange(1, n_frames + 1) * dp2
        self.phase1 = float(p1[-1] % (2 * np.pi))
        self.phase2 = float(p2[-1] % (2 * np.pi))
        tone = (np.sin(p1) * 0.6 + np.sin(p2) * 0.4).astype(np.float32)

        # -- Noise snap (own faster envelope -- the "crack") -------------------
        idx       = np.arange(self.age, self.age + n_frames, dtype=np.float32)
        t         = idx / self.sr
        atk_s     = max(1, self.attack_time * self.sr)
        noise_env = (np.minimum(idx / atk_s, 1.0) *
                     np.exp(-t / max(1e-4, self.noise_decay)) *
                     self.velocity * self.level).astype(np.float32)
        noise = (self.noise_filt.process(
            np.random.uniform(-1.0, 1.0, n_frames).astype(np.float32)) * noise_env)

        # -- Mix: snap decays fast, body sustains -----------------------------
        # tone_mix=0 -> all snap (noise), tone_mix=1 -> all body (tone)
        return tone * self.tone_mix * env + noise * (1.0 - self.tone_mix)


# -- Tom -----------------------------------------------------------------------

class Tom(DrumVoice):
    """
    909 tom: same engine as bass drum (sine + pitch envelope) but
    higher frequencies and longer decays. Three flavors: low/mid/hi.
    """
    _DEFAULTS = {
        'low': (95,  50,  0.08, 0.32),   # decay tightened from 0.45 -- less low-end ring
        'mid': (120, 65,  0.07, 0.24),   # decay tightened from 0.35
        'hi':  (160, 85,  0.06, 0.18),   # decay tightened from 0.25
        # hi-mid (2026-09-23): a 4th tom between mid and hi, so 4-tom grooves
        # (BeatBuddy) land on 4 distinct voices. GM 48 "Hi-Mid Tom".
        'himid': (140, 75, 0.065, 0.21),
    }

    def __init__(self, flavor='low'):
        super().__init__(f"{flavor}_tom")
        hi, lo, pdec, dec         = self._DEFAULTS[flavor]
        self.freq_hi, self.freq_lo = float(hi), float(lo)
        self.pitch_decay           = pdec
        self.decay_time            = dec
        self.attack_time           = 0.003
        self.phase                 = 0.0

    def _on_trigger(self):
        self.phase = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            tf = _tune_factor((cc_value - 64) / 64.0 * 24)
            base_hi, base_lo, _, _ = self._DEFAULTS[
                self.name.split('_')[0]]
            self._glide_to('freq_hi', base_hi * tf)
            self._glide_to('freq_lo', base_lo * tf)

    def _render_signal(self, n_frames, env):
        t      = np.arange(self.age, self.age + n_frames, dtype=np.float64) / self.sr
        freq   = self.freq_lo + (self.freq_hi - self.freq_lo) * np.exp(-t / self.pitch_decay)
        dp     = 2 * np.pi * freq / self.sr
        phases = self.phase + np.cumsum(dp)
        self.phase = float(phases[-1] % (2 * np.pi))
        return (np.sin(phases) * env).astype(np.float32)


# -- Rimshot -------------------------------------------------------------------

class Rimshot(DrumVoice):
    """
    909 rimshot: very short burst of bandpass-filtered noise.
    Classic 'tick' sound.
    """
    def __init__(self):
        super().__init__("rimshot")
        self.decay_time  = 0.04
        self.attack_time = 0.0005
        self._cutoff     = 1200.0   # glide target/actual, mirrors filt's cutoff
        self._q          = 2.0      # glide target/actual, mirrors filt's q
        self.filt        = BiquadFilter(SAMPLE_RATE, cutoff=self._cutoff, q=self._q, mode='BP')

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            # Also written by 'filter_freq' below -- last write wins, same
            # pre-existing behavior as before this change.
            self._glide_to('_cutoff', 800 + (cc_value - 64) / 64.0 * 800)
        elif param == 'filter_freq':
            self._glide_to('_cutoff', 500 + cc_value / 127.0 * 4000)
        elif param == 'filter_res':
            self._glide_to('_q', 0.7 + cc_value / 127.0 * 7.3)

    def _render_signal(self, n_frames, env):
        # Re-apply the (possibly still-gliding) cutoff/q every block -- cheap,
        # and means turning tune/filter_freq/filter_res doesn't snap the
        # filter coefficients mid-ring either.
        self.filt.set(cutoff=self._cutoff, q=self._q)
        noise = np.random.uniform(-1.0, 1.0, n_frames).astype(np.float32)
        return self.filt.process(noise) * env


# -- Clap ----------------------------------------------------------------------

class Clap(DrumVoice):
    """
    909 clap: 3-4 rapid noise bursts followed by a longer noisy tail.
    The multiple bursts give the 'clap' transient; the tail is the room.
    """
    def __init__(self):
        super().__init__("clap")
        self.decay_time  = 0.12
        self.attack_time = 0.001
        self._cutoff     = 900.0   # glide target/actual, mirrors filt's cutoff
        self.filt        = BiquadFilter(SAMPLE_RATE, cutoff=self._cutoff, q=0.8, mode='HP')
        # Burst timings derived from seconds so they're sample-rate independent.
        # Spacings match the 909 circuit's characteristic triple-flam.
        _sr = SAMPLE_RATE
        self._burst_times = [int(t * _sr) for t in (0.0, 0.000167, 0.000333, 0.000542)]
        self._burst_len   = max(1, int(0.000125 * _sr))   # ≈6 samples per burst hit
        self._burst_blend = max(1, int(0.00104  * _sr))   # ≈50 samples -- burst/tail blend window

    def _on_trigger(self):
        self.filt._zi[:] = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'filter_freq':
            self._glide_to('_cutoff', 300 + cc_value / 127.0 * 3000)

    def _render_signal(self, n_frames, env):
        self.filt.set(cutoff=self._cutoff)   # re-apply the (possibly gliding) cutoff every block
        noise = np.random.uniform(-1.0, 1.0, n_frames).astype(np.float32)
        burst_env = np.zeros(n_frames, dtype=np.float32)
        for bt in self._burst_times:
            local = bt - self.age
            if 0 <= local < n_frames:
                burst_env[local:min(local + self._burst_len, n_frames)] = 1.0
        combined = np.where(self.age < self._burst_blend,
                            noise * (burst_env * 0.6 + env * 0.4),
                            noise * env)
        return self.filt.process(combined.astype(np.float32))


# -- Hi-Hat --------------------------------------------------------------------

class HiHat(DrumVoice):
    """
    909 hi-hat: 6 square wave oscillators at inharmonic ratios,
    passed through a high-pass filter. Open/closed differ only in decay time.
    The inharmonic frequency ratios are what give the metallic 909 hat sound.
    """
    # Frequencies from TR-909 circuit analysis
    HAT_FREQS = [205.3, 304.4, 369.9, 522.2, 800.0, 1051.8]

    def __init__(self, mode='closed'):
        super().__init__(f"{mode}_hat")
        self.mode        = mode
        self.decay_time  = 0.06 if mode == 'closed' else 0.4
        self.attack_time = 0.001
        self.hpf_cutoff  = 6500.0
        self._q          = 0.707    # glide target/actual, mirrors filt's q
        # BiquadFilter uses SciPy's C lfilter -- fully vectorised, no Python loop.
        # For the hi-hat HP the exact per-sample sweep isn't needed (cutoff is
        # set once), so Biquad gives identical sound at a fraction of the CPU cost.
        self.filt        = BiquadFilter(SAMPLE_RATE, cutoff=self.hpf_cutoff,
                                        q=self._q, mode='HP')
        self.phases      = [0.0] * 6
        self.tune_factor = 1.0

    def _on_trigger(self):
        self.phases = [0.0] * 6

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            self._glide_to('tune_factor', _tune_factor((cc_value - 64) / 64.0 * 12))
        elif param == 'filter_freq':
            self._glide_to('hpf_cutoff', 2000 + cc_value / 127.0 * 10000)
        elif param == 'filter_res':
            # Q: 0.5 (wide/soft) -> 4.5 (tight/resonant)
            self._glide_to('_q', 0.5 + cc_value / 127.0 * 4.0)

    def _render_signal(self, n_frames, env):
        # Re-apply the (possibly still-gliding) cutoff/q every block.
        self.filt.set(cutoff=self.hpf_cutoff, q=self._q)
        sig = np.zeros(n_frames, dtype=np.float32)
        for i, (f, ph) in enumerate(zip(self.HAT_FREQS, self.phases)):
            freq   = f * self.tune_factor
            dp     = freq / self.sr
            t      = ph + np.arange(1, n_frames + 1) * dp
            # Square wave from sawtooth phase
            sq     = np.sign(np.sin(2 * np.pi * t)).astype(np.float32)
            sig   += sq
            self.phases[i] = float(t[-1] % 1.0)
        sig /= 6.0   # normalize to ±1
        return self.filt.process(sig) * env


# -- Cowbell -------------------------------------------------------------------

class Cowbell(DrumVoice):
    """
    808-style cowbell: two square-wave oscillators at inharmonic ratios through
    a bandpass filter.  The TR-909 has no cowbell but it is a classic drum-machine
    addition -- frequencies match the TR-808 circuit (562 Hz + 845 Hz).

    Tune shifts both oscillators together.  Level, tune, attack, and decay all
    respond to the base-class set_param() handlers.

    CC sculpting: cowbell got its own real filter_freq/filter_res band in the
    2026-07-21 drum-CC redesign (see CLAUDE.md) -- the note above about it
    being unreachable is STALE, corrected 2026-07-28 when a code review
    caught that this filter's cutoff/q were still snapping instantly instead
    of gliding like every other reachable filter in this file.
    Triggerable now via GM note 56.
    """
    _FREQ1 = 562.0   # TR-808 cowbell oscillator 1
    _FREQ2 = 845.0   # TR-808 cowbell oscillator 2 (ratio ≈ 1.503 -- inharmonic clang)

    def __init__(self):
        super().__init__("cowbell")
        self.freq1       = self._FREQ1
        self.freq2       = self._FREQ2
        self.decay_time  = 0.6
        self.attack_time = 0.001
        self._cutoff     = 2500.0   # glide target/actual, mirrors filt's cutoff
        self._q          = 1.2      # glide target/actual, mirrors filt's q
        self.filt        = BiquadFilter(SAMPLE_RATE, cutoff=self._cutoff, q=self._q, mode='BP')
        self.phase1      = 0.0
        self.phase2      = 0.0

    def _on_trigger(self):
        self.phase1 = 0.0
        self.phase2 = 0.0
        self.filt._zi[:] = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == 'tune':
            tf = _tune_factor((cc_value - 64) / 64.0 * 24)
            self._glide_to('freq1', self._FREQ1 * tf)
            self._glide_to('freq2', self._FREQ2 * tf)
        elif param == 'filter_freq':
            self._glide_to('_cutoff', 800 + cc_value / 127.0 * 5000)
        elif param == 'filter_res':
            self._glide_to('_q', 0.7 + cc_value / 127.0 * 6.0)

    def _render_signal(self, n_frames, env):
        # Re-apply the (possibly still-gliding) cutoff/q every block.
        self.filt.set(cutoff=self._cutoff, q=self._q)
        dp1 = self.freq1 / self.sr
        dp2 = self.freq2 / self.sr
        t1  = self.phase1 + np.arange(1, n_frames + 1) * dp1
        t2  = self.phase2 + np.arange(1, n_frames + 1) * dp2
        self.phase1 = float(t1[-1] % 1.0)
        self.phase2 = float(t2[-1] % 1.0)
        sq1 = np.sign(np.sin(2 * np.pi * t1)).astype(np.float32)
        sq2 = np.sign(np.sin(2 * np.pi * t2)).astype(np.float32)
        return self.filt.process((sq1 + sq2) * 0.5) * env


# ==============================================================================
# MONO SYNTH
# ==============================================================================

class _MonoSynthRetired:
    """DEAD CODE (2026-09-03). Superseded by the real layered MonoSynth further
    down this file (portamento + layered oscillators + note-tracking ring mod,
    hosted by orc1_mono_host -> ORC1-Mono). Nothing instantiates this; kept only
    as rollback reference and removed in the same clean-out as the retired
    SynthEngine. The original one-oscillator toy below never shipped.

    Simple monophonic TONE GENERATOR -- fully vectorised (no per-sample Python
    loops), so it's cheap enough to run dense patterns without XRuns.

    One oscillator, selectable waveform: sine / saw / square / triangle.
    Vectorised ADSR amplitude envelope. Mono last-note priority.

    CC map (on CH_MONO):
       1  waveform select   (0-31 sine, 32-63 saw, 64-95 square, 96-127 tri)
       8  attack    9 decay   10 sustain   11 release
      13  master level
    """
    WAVEFORMS = ('sine', 'saw', 'square', 'triangle')

    def __init__(self):
        self.sr        = SAMPLE_RATE
        self.waveform  = 'saw'
        self.phase     = 0.0
        self.freq      = 220.0

        # ADSR params (seconds / 0-1)
        self.attack    = 0.005
        self.decay     = 0.15
        self.sustain   = 0.7
        self.release   = 0.25

        # ADSR runtime state
        self.env_state = 'idle'
        self.level     = 0.0
        self.velocity  = 1.0

        self.master    = 0.7
        self._notes    = []
        self.active    = False

        # Octave transpose (CC75) and a vectorised LP/HP/BP filter (CC70/71/74).
        self.octave    = 0
        self.filt      = BiquadFilter(self.sr)

    # -- Note handling (mono, last-note priority) ------------------------------
    def note_on(self, note, velocity):
        self.freq     = _midi_to_hz(note + 12 * self.octave)
        self._notes.append(note)
        self.active   = True
        self.velocity = velocity / 127.0
        self.env_state = 'attack'

    def note_off(self, note):
        if note in self._notes:
            self._notes.remove(note)
        if self._notes:
            self.freq = _midi_to_hz(self._notes[-1] + 12 * self.octave)  # fall back, keep sounding
        else:
            self.env_state = 'release'
            self.active = False

    def set_cc(self, cc, value):
        v = value / 127.0
        if cc == 1:
            self.waveform = self.WAVEFORMS[min(3, value // 32)]
        elif cc == 8:
            self.attack  = _cc_to_time(value, 0.002, 4.0)
        elif cc == 9:
            self.decay   = _cc_to_time(value, 0.005, 4.0)
        elif cc == 10:
            self.sustain = v
        elif cc == 11:
            self.release = _cc_to_time(value, 0.005, 4.0)
        elif cc == 13:
            self.master  = v
        elif cc == 74:                       # filter cutoff
            self.filt.set(cutoff=_cc_to_cutoff(value))
        elif cc == 71:                       # filter resonance
            self.filt.set(q=_cc_to_q(value))
        elif cc == 70:                       # filter mode LP/HP/BP
            self.filt.set(mode=_cc_to_mode(value))
        elif cc == 75:                       # octave transpose
            self.octave = _cc_to_octave(value)

    # -- Vectorised oscillator -------------------------------------------------
    def _osc(self, n):
        dp   = self.freq / self.sr
        ph   = self.phase + dp * np.arange(1, n + 1, dtype=np.float64)
        self.phase = float(ph[-1] % 1.0)
        frac = (ph % 1.0).astype(np.float32)
        w = self.waveform
        if w == 'sine':
            return np.sin(2 * np.pi * frac).astype(np.float32)
        if w == 'square':
            return np.where(frac < 0.5, 1.0, -1.0).astype(np.float32)
        if w == 'triangle':
            return (2.0 * np.abs(2.0 * frac - 1.0) - 1.0).astype(np.float32)
        return (2.0 * frac - 1.0).astype(np.float32)   # saw (default)

    # -- Vectorised ADSR (sample-accurate, chunked by state) -------------------
    def _env(self, n):
        out = np.empty(n, dtype=np.float32)
        sr  = self.sr
        i   = 0
        while i < n:
            st = self.env_state
            if st == 'idle':
                out[i:] = 0.0
                self.level = 0.0
                break
            if st == 'sustain':
                out[i:] = self.sustain
                self.level = self.sustain
                break
            if st == 'attack':
                rate   = 1.0 / max(1.0, self.attack * sr)
                remain = int(np.ceil((1.0 - self.level) / rate)) if rate > 0 else 1
                take   = max(1, min(remain, n - i))
                ramp   = self.level + rate * np.arange(1, take + 1, dtype=np.float32)
                np.minimum(ramp, 1.0, out=ramp)
                out[i:i + take] = ramp
                self.level = float(ramp[-1])
                i += take
                if self.level >= 1.0 - 1e-9:
                    self.level = 1.0
                    self.env_state = 'decay'
                continue
            if st == 'decay':
                target = self.sustain
                a = 1.0 - np.exp(-1.0 / max(1.0, self.decay * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = target + (self.level - target) * (1.0 - a) ** k
                done = np.abs(curve - target) < 1e-4
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1])
                i += take
                if np.abs(self.level - target) < 1e-4:
                    self.level = target
                    self.env_state = 'idle' if target < 1e-4 else 'sustain'
                continue
            if st == 'release':
                r = np.exp(-1.0 / max(1.0, self.release * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = self.level * (r ** k)
                done = curve < 1e-5
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1])
                i += take
                if self.level < 1e-5:
                    self.level = 0.0
                    self.env_state = 'idle'
                continue
            # Unknown state -- fail safe
            out[i:] = 0.0
            break
        return out * self.velocity

    # -- Render ----------------------------------------------------------------
    def render(self, n_frames):
        if not self.active and self.env_state == 'idle':
            return np.zeros(n_frames, dtype=np.float32)
        sig = self._osc(n_frames) * self._env(n_frames) * self.master
        return self.filt.process(sig).astype(np.float32)

def _midi_to_hz(note):
    return 440.0 * 2 ** ((note - 69) / 12.0)


class MonoSynth:
    """Monophonic LAYERED tone generator (2026-09-03, hosted by orc1_mono_host ->
    ORC1-Mono).

    ONE note at a time (last-note priority), but that note is built from up to
    N oscillator LAYERS stacked together -- each layer with its own waveform,
    SEMITONE offset, DETUNE (cents) and gain -- so you dial "one big note"
    (unison / octave / fifth stacks, the joined-P1&P2 voice-split style) instead
    of chords like the poly, or the chip's monaural voice restrictions. Adds:

      * PORTAMENTO -- the base pitch GLIDES from the previous note to the new one
        over `glide_time`; a per-sample frequency ramp (not a per-block step) so
        the glide never zippers. Legato: while a note is held, a new note glides
        WITHOUT retriggering the envelope; from silence it retriggers.
      * NOTE-TRACKING ring mod -- carrier = (glided) base freq x ratio, exact
        (every layer's pitch is known, so nothing has to guess a fundamental --
        the same reason the poly/chip tracking is glitch-free). CC15 > 0 gives the
        classic fixed-Hz carrier instead.

    Shared vectorised ADSR + biquad filter + master, same idioms as PolySynth.

    CC map (on the mono channel):
       1  layer0 waveform          5  portamento time (0 = instant)
       8/9/10/11  ADSR            12/14/15  ring mod depth / ratio / carrier-Hz
      13  master                  70/71/74  filter mode / res / cutoff   75 octave
      19  layer0 gain
      20/21/22/23/24  layer1  on / semitone / detune / waveform / gain
      25/26/27/28/29  layer2  on / semitone / detune / waveform / gain
    """
    WAVEFORMS = ('sine', 'saw', 'square', 'triangle')
    N_LAYERS  = 3

    def __init__(self):
        self.sr = SAMPLE_RATE
        # Per-layer config; layer 0 is the always-present base.
        self.layers = [{'on': (i == 0), 'wave': 'saw', 'semi': 0, 'cents': 0.0,
                        'gain': 1.0, 'phase': 0.0} for i in range(self.N_LAYERS)]
        # Shared ADSR
        self.attack, self.decay, self.sustain, self.release = 0.005, 0.15, 0.7, 0.25
        self.env_state = 'idle'
        self.level     = 0.0
        self.velocity  = 1.0
        self.master    = 0.7
        self.octave    = 0
        self.filt      = BiquadFilter(self.sr)
        # Portamento
        self.glide_time  = 0.0     # seconds; 0 = instant jump
        self.freq        = 220.0   # current (glided) base freq
        self.target_freq = 220.0
        self._notes      = []      # mono note stack (newest last)
        self.active      = False
        # Note-tracking ring mod (shared settings; carrier tracks self.freq)
        self.rm            = RingMod()
        self.rm_phase      = 0.0
        self._rm_depth_cur = 0.0

    # -- Note handling (mono, last-note priority, legato glide) ----------------
    def _note_freq(self, note):
        return _midi_to_hz(note + 12 * self.octave)

    def note_on(self, note, velocity):
        retrig = self.env_state in ('idle', 'release')   # from silence -> retrig
        if note in self._notes:
            self._notes.remove(note)
        self._notes.append(note)
        self.target_freq = self._note_freq(note)
        self.velocity    = velocity / 127.0
        self.active      = True
        if self.glide_time <= 0.0:
            self.freq = self.target_freq
        if retrig:
            self.env_state = 'attack'
            self.rm_phase  = 0.0
            self._rm_depth_cur = self.rm.depth
        # (legato: env untouched, pitch glides toward the new target)

    def note_off(self, note):
        if note in self._notes:
            self._notes.remove(note)
        if self._notes:                        # fall back to the last held note
            self.target_freq = self._note_freq(self._notes[-1])
            if self.glide_time <= 0.0:
                self.freq = self.target_freq
        else:
            if self.env_state not in ('idle',):
                self.env_state = 'release'
            self.active = False

    def all_notes_off(self):
        """Panic: drop every held note and release."""
        self._notes.clear()
        if self.env_state not in ('idle',):
            self.env_state = 'release'
        self.active = False

    def set_cc(self, cc, value):
        v = value / 127.0
        if   cc == 1:  self.layers[0]['wave'] = self.WAVEFORMS[min(3, value // 32)]
        elif cc == 5:  self.glide_time = v * 2.0            # 0..2s (0 = instant)
        elif cc == 8:  self.attack  = _cc_to_time(value, 0.002, 4.0)
        elif cc == 9:  self.decay   = _cc_to_time(value, 0.005, 4.0)
        elif cc == 10: self.sustain = v
        elif cc == 11: self.release = _cc_to_time(value, 0.005, 4.0)
        elif cc == 12: self.rm.depth = v
        elif cc == 13: self.master  = v
        elif cc == 14: self.rm.ratio = _cc_to_rm_ratio(value)
        elif cc == 15: self.rm.fixed_hz = _cc_to_rm_hz(value)
        elif cc == 19: self.layers[0]['gain'] = v * 2.0     # 0..2 (unity at 64)
        elif cc == 70: self.filt.set(mode=_cc_to_mode(value))
        elif cc == 71: self.filt.set(q=_cc_to_q(value))
        elif cc == 74: self.filt.set(cutoff=_cc_to_cutoff(value))
        elif cc == 75: self.octave = _cc_to_octave(value)
        else:
            for li, base in ((1, 20), (2, 25)):            # layer 1 / layer 2
                if base <= cc <= base + 4:
                    L   = self.layers[li]
                    off = cc - base
                    if   off == 0: L['on']    = value >= 64
                    elif off == 1: L['semi']  = int(round(value / 127.0 * 48)) - 24
                    elif off == 2: L['cents'] = (value / 127.0 * 100.0) - 50.0
                    elif off == 3: L['wave']  = self.WAVEFORMS[min(3, value // 32)]
                    elif off == 4: L['gain']  = v * 2.0
                    break

    # -- Portamento: per-sample glide of the base frequency --------------------
    def _glide_freq_ramp(self, n):
        f0, ft = self.freq, self.target_freq
        if self.glide_time <= 0.0 or abs(ft - f0) < 1e-6:
            self.freq = ft
            return np.full(n, ft, dtype=np.float64)
        a = 1.0 - np.exp(-1.0 / max(1.0, self.glide_time * self.sr))
        k = np.arange(1, n + 1, dtype=np.float64)
        ramp = ft + (f0 - ft) * (1.0 - a) ** k     # one-pole approach to target
        self.freq = float(ramp[-1])
        return ramp

    @staticmethod
    def _wave(frac, w):
        if w == 'sine':
            return np.sin(2 * np.pi * frac).astype(np.float32)
        if w == 'square':
            return np.where(frac < 0.5, 1.0, -1.0).astype(np.float32)
        if w == 'triangle':
            return (2.0 * np.abs(2.0 * frac - 1.0) - 1.0).astype(np.float32)
        return (2.0 * frac - 1.0).astype(np.float32)          # saw (default)

    def _osc(self, freq_ramp, n):
        """Sum the enabled layers, each transposed by its semitone+cents offset.
        Each layer integrates the (glided) freq ramp so portamento carries the
        whole stack together."""
        out = np.zeros(n, dtype=np.float32)
        for L in self.layers:
            if not L['on']:
                continue
            mult = 2.0 ** ((L['semi'] + L['cents'] / 100.0) / 12.0)
            dp   = (freq_ramp * mult) / self.sr
            ph   = L['phase'] + np.cumsum(dp)
            L['phase'] = float(ph[-1] % 1.0)
            frac = (ph % 1.0).astype(np.float32)
            out += self._wave(frac, L['wave']) * float(L['gain'])
        return out

    def _rm_gain(self, target, n):
        """Depth ramped toward target (copy of PolyVoice._rm_gain -- a flat
        per-block depth would click on every knob tick)."""
        cur = self._rm_depth_cur
        if abs(target - cur) < 1e-6:
            self._rm_depth_cur = target
            return target
        step = 1.0 / max(1.0, _RM_DEPTH_RAMP_S * self.sr)
        k    = min(n, int(np.ceil(abs(target - cur) / step)))
        out  = np.empty(n, dtype=np.float32)
        sign = 1.0 if target > cur else -1.0
        ramp = cur + sign * step * np.arange(1, k + 1, dtype=np.float32)
        if sign > 0:
            np.minimum(ramp, target, out=ramp)
        else:
            np.maximum(ramp, target, out=ramp)
        out[:k] = ramp
        out[k:] = target
        self._rm_depth_cur = target
        return out

    def _ring_mod(self, sig, freq_ramp, n):
        rm = self.rm
        target = rm.depth
        if target <= 0.0 and self._rm_depth_cur <= 0.0:
            return sig
        if rm.fixed_hz > 0.0:
            cf = np.full(n, rm.fixed_hz, dtype=np.float64)   # classic untracked
        else:
            cf = freq_ramp * rm.ratio                        # note-tracking
        ph = self.rm_phase + np.cumsum(cf / self.sr)
        self.rm_phase = float(ph[-1] % 1.0)
        carrier = np.sin(2 * np.pi * (ph % 1.0)).astype(np.float32)
        d = self._rm_gain(target, n)
        return (sig * ((1.0 - d) + d * carrier)).astype(np.float32)

    def _env(self, n):
        out = np.empty(n, dtype=np.float32)
        sr  = self.sr
        i   = 0
        while i < n:
            st = self.env_state
            if st == 'idle':
                out[i:] = 0.0; self.level = 0.0; break
            if st == 'sustain':
                out[i:] = self.sustain; self.level = self.sustain; break
            if st == 'attack':
                rate   = 1.0 / max(1.0, self.attack * sr)
                remain = int(np.ceil((1.0 - self.level) / rate)) if rate > 0 else 1
                take   = max(1, min(remain, n - i))
                ramp   = self.level + rate * np.arange(1, take + 1, dtype=np.float32)
                np.minimum(ramp, 1.0, out=ramp)
                out[i:i + take] = ramp
                self.level = float(ramp[-1]); i += take
                if self.level >= 1.0 - 1e-9:
                    self.level = 1.0; self.env_state = 'decay'
                continue
            if st == 'decay':
                target = self.sustain
                a = 1.0 - np.exp(-1.0 / max(1.0, self.decay * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = target + (self.level - target) * (1.0 - a) ** k
                done = np.abs(curve - target) < 1e-4
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1]); i += take
                if np.abs(self.level - target) < 1e-4:
                    self.level = target
                    self.env_state = 'idle' if target < 1e-4 else 'sustain'
                continue
            if st == 'release':
                r = np.exp(-1.0 / max(1.0, self.release * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = self.level * (r ** k)
                done = curve < 1e-5
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1]); i += take
                if self.level < 1e-5:
                    self.level = 0.0; self.env_state = 'idle'
                continue
            out[i:] = 0.0; break
        return out

    def render(self, n):
        if self.env_state == 'idle':
            return np.zeros(n, dtype=np.float32)
        if self.env_state == 'release' and self.level < 0.003:
            self.env_state = 'idle'; self.level = 0.0
            return np.zeros(n, dtype=np.float32)
        fr  = self._glide_freq_ramp(n)
        sig = self._ring_mod(self._osc(fr, n), fr, n)
        sig = sig * self._env(n) * self.velocity * _synth_fader_gain(self.master, MONO_FADER_CEIL)
        return self.filt.process(sig).astype(np.float32)


# ==============================================================================
# SYNTH ENGINE
# ==============================================================================

class PolyVoice:
    """One vectorised tone-gen voice: sine/saw/square/triangle + ADSR."""
    def __init__(self, sr):
        self.sr        = sr
        self.note      = None
        self.phase     = 0.0
        self.freq      = 440.0
        self.waveform  = 'saw'
        self.attack    = 0.005
        self.decay     = 0.2
        self.sustain   = 0.7
        self.release   = 0.3
        self.env_state = 'idle'
        self.level     = 0.0
        self.velocity  = 1.0
        self.start_age = 0
        # -- Ring modulation (2026-08-05). `rm` is a shared RingMod owned by the
        # engine, or None. None = no ring mod at all, which is what ChipSynth's
        # voices get (that class is not on the live signal path -- SynthEngine
        # builds a NESEngine for the chip bus -- so it is left alone).
        self.rm        = None
        self.rm_phase  = 0.0
        self._rm_depth_cur = 0.0

    def note_on(self, note, vel, wf, a, d, s, r, age, octave=0):
        self.note      = note                       # original note (for note_off match)
        self.freq      = _midi_to_hz(note + 12 * octave)
        self.waveform  = wf
        self.attack, self.decay, self.sustain, self.release = a, d, s, r
        self.velocity  = vel / 127.0
        self.env_state = 'attack'
        self.start_age = age
        # Start every note at the same carrier phase so repeated notes sound
        # identical, and snap the smoothed depth to its target: the ramp exists
        # to stop a KNOB MOVE clicking, not to fade the effect in at note-on.
        self.rm_phase  = 0.0
        self._rm_depth_cur = self.rm.depth if self.rm is not None else 0.0

    def note_off(self):
        if self.env_state not in ('idle', 'release'):
            self.env_state = 'release'

    def active(self):
        return self.env_state != 'idle'

    def _osc(self, n):
        dp   = self.freq / self.sr
        ph   = self.phase + dp * np.arange(1, n + 1, dtype=np.float64)
        self.phase = float(ph[-1] % 1.0)
        frac = (ph % 1.0).astype(np.float32)
        w = self.waveform
        if w == 'sine':
            return np.sin(2 * np.pi * frac).astype(np.float32)
        if w == 'square':
            return np.where(frac < 0.5, 1.0, -1.0).astype(np.float32)
        if w == 'triangle':
            return (2.0 * np.abs(2.0 * frac - 1.0) - 1.0).astype(np.float32)
        return (2.0 * frac - 1.0).astype(np.float32)

    def _env(self, n):
        out = np.empty(n, dtype=np.float32)
        sr  = self.sr
        i   = 0
        while i < n:
            st = self.env_state
            if st == 'idle':
                out[i:] = 0.0; self.level = 0.0; break
            if st == 'sustain':
                out[i:] = self.sustain; self.level = self.sustain; break
            if st == 'attack':
                rate   = 1.0 / max(1.0, self.attack * sr)
                remain = int(np.ceil((1.0 - self.level) / rate)) if rate > 0 else 1
                take   = max(1, min(remain, n - i))
                ramp   = self.level + rate * np.arange(1, take + 1, dtype=np.float32)
                np.minimum(ramp, 1.0, out=ramp)
                out[i:i + take] = ramp
                self.level = float(ramp[-1]); i += take
                if self.level >= 1.0 - 1e-9:
                    self.level = 1.0; self.env_state = 'decay'
                continue
            if st == 'decay':
                target = self.sustain
                a = 1.0 - np.exp(-1.0 / max(1.0, self.decay * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = target + (self.level - target) * (1.0 - a) ** k
                done = np.abs(curve - target) < 1e-4
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1]); i += take
                if np.abs(self.level - target) < 1e-4:
                    self.level = target
                    self.env_state = 'idle' if target < 1e-4 else 'sustain'
                continue
            if st == 'release':
                r = np.exp(-1.0 / max(1.0, self.release * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = self.level * (r ** k)
                done = curve < 1e-5
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self.level = float(curve[take - 1]); i += take
                if self.level < 1e-5:
                    self.level = 0.0; self.env_state = 'idle'
                continue
            out[i:] = 0.0; break
        return out

    def _rm_gain(self, target, n):
        """Depth for this block, ramped toward `target`.

        Returns a scalar when the depth is already there (the common case, no
        array cost) and a per-sample ramp while it is moving. A flat per-block
        depth would be an amplitude STEP on every knob tick -- the same click
        class as the drum-CC and limiter-attack defects already on record in
        CLAUDE.md. Deliberately the same shape as GameboyWaveVoice._gain.
        """
        cur = self._rm_depth_cur
        if abs(target - cur) < 1e-6:
            self._rm_depth_cur = target
            return target
        step = 1.0 / max(1.0, _RM_DEPTH_RAMP_S * self.sr)
        k    = min(n, int(np.ceil(abs(target - cur) / step)))
        out  = np.empty(n, dtype=np.float32)
        sign = 1.0 if target > cur else -1.0
        ramp = cur + sign * step * np.arange(1, k + 1, dtype=np.float32)
        if sign > 0:
            np.minimum(ramp, target, out=ramp)
        else:
            np.maximum(ramp, target, out=ramp)
        out[:k] = ramp
        out[k:] = target
        self._rm_depth_cur = float(out[-1])
        return out

    def _ring_mod(self, sig, n):
        """Multiply `sig` by a sine carrier locked to this voice's own pitch.

        Must be called AFTER _osc(), not before: ChipVoice recomputes self.freq
        at the top of every _osc() from base_freq x pitch_mult, so reading freq
        first would use last block's value and the carrier would lag a bend.
        """
        rm = self.rm
        if rm is None:
            return sig
        target = rm.depth
        # Bypass only when the ramp has also finished -- returning early while
        # _rm_depth_cur is still above zero would turn "depth knob to zero" into
        # a hard cut, which is the exact click the ramp exists to prevent.
        if target <= 0.0 and self._rm_depth_cur <= 0.0:
            return sig
        # fixed_hz > 0 = classic untracked carrier. Otherwise track the note.
        # self.freq already carries pitch bend, vibrato and the DMG sweep for
        # chip voices, so the carrier follows all three for free.
        cf = rm.fixed_hz if rm.fixed_hz > 0.0 else self.freq * rm.ratio
        dp = cf / self.sr
        ph = self.rm_phase + dp * np.arange(1, n + 1, dtype=np.float64)
        self.rm_phase = float(ph[-1] % 1.0)
        carrier = np.sin(2 * np.pi * (ph % 1.0)).astype(np.float32)
        d = self._rm_gain(target, n)
        return (sig * ((1.0 - d) + d * carrier)).astype(np.float32)

    def render(self, n):
        if self.env_state == 'idle':
            return None
        # Early-exit: if we're very deep into the release tail (level below
        # the noise floor), mark idle now so the JACK callback doesn't pay
        # NumPy costs for an inaudible voice.  0.003 ≈ −50 dB headroom.
        if self.env_state == 'release' and self.level < 0.003:
            self.env_state = 'idle'
            self.level     = 0.0
            return None
        # Ring mod sits between the oscillator and the envelope, so the note's
        # own ADSR still shapes the modulated result rather than a raw multiply.
        return (self._ring_mod(self._osc(n), n)
                * self._env(n) * self.velocity).astype(np.float32)


class PolySynth:
    """
    Polyphonic tone generator -- a small pool of PolyVoice (default 8) with
    voice allocation + oldest-note stealing. Shares one waveform/ADSR/master;
    new notes pick up the current settings. CC map matches MonoSynth.
    """
    def __init__(self, n_voices=6):
        self.sr       = SAMPLE_RATE
        self.voices   = [PolyVoice(self.sr) for _ in range(n_voices)]
        self.waveform = 'saw'
        self.attack   = 0.005
        self.decay    = 0.2
        self.sustain  = 0.7
        self.release  = 0.3
        self.master   = 0.5
        self._age     = 0
        # Octave transpose (CC75) + shared LP/HP/BP output filter (CC70/71/74).
        self.octave   = 0
        self.filt     = BiquadFilter(self.sr)
        # Pitch-tracked ring mod (CC12/14/15), shared by every voice by
        # reference so a knob move is heard on already-held notes.
        self.rm       = RingMod()
        for _v in self.voices:
            _v.rm = self.rm

    def note_on(self, note, vel):
        self._age += 1
        # Prefer a fully idle slot first, then a voice already in its release
        # tail (it's fading out, stealing it causes the least audible disruption),
        # and only as a last resort steal the oldest actively-sounding voice.
        v = next((x for x in self.voices if x.env_state == 'idle'), None)
        if v is None:
            v = next((x for x in self.voices if x.env_state == 'release'), None)
        if v is None:
            v = min(self.voices, key=lambda x: x.start_age)
        v.note_on(note, vel, self.waveform, self.attack,
                  self.decay, self.sustain, self.release, self._age, self.octave)

    def note_off(self, note):
        for v in self.voices:
            if v.note == note and v.env_state not in ('idle', 'release'):
                v.note_off()

    def all_notes_off(self):
        """Release every sounding voice regardless of note number (panic).
        Added 2026-07-30 for the CC120/CC123 panic path."""
        for v in self.voices:
            if v.env_state not in ('idle', 'release'):
                v.note_off()

    def set_cc(self, cc, value):
        v = value / 127.0
        if cc == 1:
            self.waveform = ('sine', 'saw', 'square', 'triangle')[min(3, value // 32)]
        elif cc == 8:
            self.attack  = _cc_to_time(value, 0.002, 4.0)
        elif cc == 9:
            self.decay   = _cc_to_time(value, 0.005, 4.0)
        elif cc == 10:
            self.sustain = v
        elif cc == 11:
            self.release = _cc_to_time(value, 0.005, 4.0)
        elif cc == 13:
            self.master  = v
        elif cc == 12:                       # ring mod depth (0 = bypass)
            self.rm.depth = v
        elif cc == 14:                       # ring mod ratio (16 detents)
            self.rm.ratio = _cc_to_rm_ratio(value)
        elif cc == 15:                       # ring mod carrier Hz (0 = track pitch)
            self.rm.fixed_hz = _cc_to_rm_hz(value)
        elif cc == 74:                       # filter cutoff
            self.filt.set(cutoff=_cc_to_cutoff(value))
        elif cc == 71:                       # filter resonance
            self.filt.set(q=_cc_to_q(value))
        elif cc == 70:                       # filter mode LP/HP/BP
            self.filt.set(mode=_cc_to_mode(value))
        elif cc == 75:                       # octave transpose
            self.octave = _cc_to_octave(value)

    def render(self, n):
        out = np.zeros(n, dtype=np.float32)
        any_active = False
        for vc in self.voices:
            if vc.active():
                s = vc.render(n)
                if s is not None:
                    out += s
                any_active = True
        if any_active:
            out *= _synth_fader_gain(self.master, POLY_FADER_CEIL)
            out = self.filt.process(out)     # one cheap C-vectorised filter pass
        return out


# ==============================================================================
# NES-STYLE CHIP VOICE  (2A03: pulse w/ duty, stepped triangle, LFSR-ish noise)
# ==============================================================================

def _chip_wave(v):
    """CC1 value -> (kind, duty), sweeping the four classic NES timbres:
    pulse 12.5% / 25% / 50%, stepped triangle, then noise."""
    if v < 26:
        return ('pulse', 0.125)
    if v < 52:
        return ('pulse', 0.25)
    if v < 78:
        return ('pulse', 0.5)
    if v < 104:
        return ('triangle', 0.5)
    return ('noise', 0.5)


# -- Authentic Game Boy LFSR noise tables (chip/GB expansion 2026-07-20) -------
# The real GB noise channel is a 15-bit Linear Feedback Shift Register:
# each clock, feedback = bit0 XOR bit1, the register shifts right, and the
# feedback goes into bit 14. In "7-bit (short) mode" the feedback ALSO goes
# into bit 6, which shrinks the repeat period to 127 steps -- that's the
# famous periodic, metallic, almost-tonal GB noise. 15-bit mode repeats
# every 32767 steps and sounds essentially white.
#
# Instead of stepping the register in the audio thread, we play the whole
# sequence out ONCE here at import time into two ±1.0 float arrays and just
# index into them during rendering (see ChipVoice._osc_noise). That is both
# hardware-accurate AND more real-time-safe than the old np.random refill
# (no random-number allocation in the JACK callback).
def _gb_lfsr_seq(short_mode):
    n   = 127 if short_mode else 32767      # full period of each mode
    reg = 0x7FFF                            # hardware seeds the register all-ones
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        # Output = complement of bit0 mapped to ±1 (bit0=0 -> +1, bit0=1 -> -1).
        out[i] = 1.0 if (reg & 1) == 0 else -1.0
        fb  = (reg ^ (reg >> 1)) & 1        # feedback = bit0 XOR bit1
        reg >>= 1
        reg |= fb << 14                     # feedback into bit 14 (15-bit register)
        if short_mode:
            # 7-bit mode: feedback ALSO overwrites bit 6 -> period 127
            reg = (reg & ~(1 << 6)) | (fb << 6)
    return out

_GB_LFSR_15 = _gb_lfsr_seq(False)   # long / white-ish (32767 steps)
_GB_LFSR_7  = _gb_lfsr_seq(True)    # short / metallic (127 steps)


def _polyblep(t, dt):
    """Vectorised PolyBLEP residual for band-limiting one step discontinuity.

    `t` is the phase array in [0,1); `dt` is the normalised frequency
    (cycles/sample = freq/sr). Returns the correction to ADD at a rising edge
    (and to SUBTRACT at a falling edge). Away from an edge it is zero, so it only
    softens the two samples straddling each discontinuity -- which is exactly the
    aliasing energy that a naive square folds back down. Cheap: two boolean masks,
    no per-sample Python.
    """
    dt = float(dt)
    out = np.zeros_like(t)
    if dt <= 0.0:
        return out
    m = t < dt                       # just after the discontinuity
    x = t[m] / dt
    out[m] = x + x - x * x - 1.0
    m = t > 1.0 - dt                 # just before the next discontinuity
    x = (t[m] - 1.0) / dt
    out[m] = x * x + x + x + 1.0
    return out


class ChipVoice(PolyVoice):
    """One chip channel. Reuses PolyVoice's vectorised ADSR/render; only the
    oscillator changes. 'pulse' is a naive (deliberately aliased -- that's the
    8-bit charm) variable-duty square; 'triangle' is quantised to 15 levels
    like the NES; 'noise' plays the authentic GB LFSR bit sequence with a
    sample-and-hold clock that tracks the note pitch. All vectorised -- no
    per-sample Python."""

    def __init__(self, sr):
        super().__init__(sr)
        self.kind = 'pulse'
        self.duty = 0.5
        # Pulse anti-alias (PolyBLEP), OPT-IN, default OFF -> byte-identical to
        # the raw aliased square (the 8-bit charm). Toggled per patch by CC 85 on
        # a pulse channel; see NESEngine.set_cc. Only the pulse osc reads it.
        self.aa   = False
        self._nseq  = _GB_LFSR_15   # which LFSR table noise reads (15- or 7-bit)
        self._ni    = 0             # noise read position, in SAMPLES
        self._nhold = 1             # samples held per LFSR step (sets noise pitch)
        # -- Pitch modulation (chip/GB expansion 2026-07-20) -------------------
        # base_freq = the note's own frequency (set at note-on / octave change /
        # detune coupling). pitch_mult = combined bend × vibrato × sweep factor,
        # written by NESEngine once per audio block. The oscillator recomputes
        # self.freq = base_freq × pitch_mult at the top of every block, so
        # nothing else should write self.freq directly any more -- write
        # base_freq instead and let the recompute pick it up.
        self.base_freq  = 440.0
        self.pitch_mult = 1.0

    def note_on(self, note, vel, kind, duty, a, d, s, r, age, octave=0):
        # Drive the inherited envelope/freq; waveform field is unused here.
        super().note_on(note, vel, 'saw', a, d, s, r, age, octave)
        self.kind  = kind
        self.duty  = duty
        self._ni   = 0
        self._nhold = max(1, int(round(self.sr / max(40.0, self.freq))))
        # Capture the note's frequency as the pitch-modulation base.
        # (pitch_mult deliberately NOT reset here -- a held pitchwheel bend
        # carries across to the next note, standard MIDI behaviour. NESEngine
        # refreshes pitch_mult every block anyway.)
        self.base_freq = self.freq

    def _osc(self, n):
        if self.kind == 'noise':
            return self._osc_noise(n)
        # Recompute effective frequency from base × modulation each block.
        # For triangle (and any voice nobody bends) pitch_mult stays 1.0,
        # so this is exactly the old behaviour.
        self.freq = self.base_freq * self.pitch_mult
        dp   = self.freq / self.sr
        ph   = self.phase + dp * np.arange(1, n + 1, dtype=np.float64)
        self.phase = float(ph[-1] % 1.0)
        frac = (ph % 1.0).astype(np.float32)
        if self.kind == 'triangle':
            tri = 2.0 * np.abs(2.0 * frac - 1.0) - 1.0
            return (np.round(tri * 7.0) / 7.0).astype(np.float32)   # 15-step
        # pulse -- naive (aliased) by default; PolyBLEP-corrected when aa is on.
        out = np.where(frac < self.duty, 1.0, -1.0).astype(np.float64)
        if self.aa:
            # Rising edge at phase 0, falling edge at phase == duty.
            out = out + _polyblep(frac, dp) - _polyblep((frac - self.duty) % 1.0, dp)
        return out.astype(np.float32)

    def _osc_noise(self, n):
        # Authentic LFSR noise: sample-and-hold read of the precomputed
        # ±1 sequence. Each LFSR step is held for `_nhold` samples (that's
        # what sets the perceived pitch), and the read position wraps at
        # the end of the table so the sequence repeats exactly like the
        # hardware register would. Fully vectorised, no allocation beyond
        # the output-sized index/lookup arrays (same idiom as the pulse osc).
        seq  = self._nseq
        L    = seq.shape[0]                       # 32767 (long) or 127 (short)
        hold = max(1, self._nhold)
        if self.pitch_mult != 1.0:
            # Glide / slide / arp on noise (2026-09-24): the note-on hold is
            # base_freq's; a pitch mod re-derives it the same way. Untouched
            # (1.0) keeps the original hold exactly.
            hold = max(1, int(round(self.sr / max(40.0, self.base_freq * self.pitch_mult))))
        # Sample k of this block sits at absolute sample position _ni + k;
        # integer-divide by the hold length to get the LFSR step index,
        # modulo table length to wrap.
        idx = ((self._ni + np.arange(n, dtype=np.int64)) // hold) % L
        out = seq[idx]
        # Advance the persistent position (wrap at one full table pass so
        # the counter never grows unbounded).
        self._ni = (self._ni + n) % (L * hold)
        return out


class ChipSynth:
    """Polyphonic NES-style chip instrument (default 4 voices). Mirrors
    PolySynth -- shared waveform/ADSR/octave/filter, oldest-note stealing -- but
    each note captures the chip timbre selected by CC1. Lives on CH_CHIP so it
    plays alongside the 909 and the other synths."""

    def __init__(self, n_voices=4):
        self.sr       = SAMPLE_RATE
        self.voices   = [ChipVoice(self.sr) for _ in range(n_voices)]
        self.kind     = 'pulse'
        self.duty     = 0.5
        self.attack   = 0.002
        self.decay    = 0.12
        self.sustain  = 0.8
        self.release  = 0.15
        self.master   = 0.35      # squares are hot; leave headroom
        self.octave   = 0
        self._age     = 0
        self.filt     = BiquadFilter(self.sr)

    def note_on(self, note, vel):
        self._age += 1
        v = next((x for x in self.voices if not x.active()), None)
        if v is None:
            v = min(self.voices, key=lambda x: x.start_age)
        v.note_on(note, vel, self.kind, self.duty, self.attack,
                  self.decay, self.sustain, self.release, self._age, self.octave)

    def note_off(self, note):
        for v in self.voices:
            if v.note == note and v.env_state not in ('idle', 'release'):
                v.note_off()

    def set_cc(self, cc, value):
        v = value / 127.0
        if cc == 1:
            self.kind, self.duty = _chip_wave(value)
        elif cc == 8:
            self.attack  = _cc_to_time(value, 0.001, 3.0)
        elif cc == 9:
            self.decay   = _cc_to_time(value, 0.005, 3.0)
        elif cc == 10:
            self.sustain = v
        elif cc == 11:
            self.release = _cc_to_time(value, 0.005, 3.0)
        elif cc == 13:
            self.master  = v * 0.7
        elif cc == 74:
            self.filt.set(cutoff=_cc_to_cutoff(value))
        elif cc == 71:
            self.filt.set(q=_cc_to_q(value))
        elif cc == 70:
            self.filt.set(mode=_cc_to_mode(value))
        elif cc == 75:
            self.octave = _cc_to_octave(value)

    def render(self, n):
        out = np.zeros(n, dtype=np.float32)
        any_active = False
        for vc in self.voices:
            if vc.active():
                s = vc.render(n)
                if s is not None:
                    out += s
                any_active = True
        if any_active:
            out *= self.master
            out = self.filt.process(out)
        return out



# â”€â”€ Sample Voice â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# ==============================================================================
# NES / GAMEBOY ENGINE  -- authentic hardware voice limits
# ==============================================================================
#
# Hardware channels (all monophonic -- new note immediately replaces old):
#   Pulse 1   -- variable duty: 12.5 / 25 / 50 / 75 %
#   Pulse 2   -- same oscillator, independent pitch
#   Triangle  -- 15-step quantised; NO amplitude envelope (real APU behaviour)
#   Noise     -- sample-and-hold; two LFSR modes (long = white, short = buzzy)
#   PCM       -- one-shot .wav playback from ~/samples/chip/ folder
#
# CC map (same channel as ChipSynth, CH_CHIP):
#   CC  1   channel select  0-25=P1  26-51=P2  52-76=tri  77-101=noise  102+=PCM
#   CC  2   pulse duty      0-31=12.5%  32-63=25%  64-95=50%  96-127=75%
#   CC  3   noise mode      0-63=long (musical)  64-127=short (metallic buzz)
#   CC  8   attack          shared ADSR (triangle ignores -- it is always instant)
#   CC  9   decay
#   CC 10   sustain
#   CC 11   release
#   CC 13   master level
#   CC 70/71/74/75  filter mode/Q/cutoff/octave  (same as ChipSynth)
#   CC 72   P1+P2 coupling mode  0-127 across the 7 NES_COUPLE_MODES
#   CC 73   P1+P2 detune (DETUNE mode only)  centered, 64=0ct, 0/127=~-50/+50ct
#   CC 76   P1/P2 stereo spread toggle  0-63=off  64-127=on (auto-enables
#           SynthEngine.stereo_mode when turned on -- silent no-op in mono)


class PCMChannel:
    """
    One-shot sample bank for the NES PCM / Gameboy sample channel.

    Loads every .wav in sample_dir alphabetically on startup.
    MIDI note number mod count selects which sample triggers.
    No note-off (like the real DPCM channel -- trigger and it plays to the end).

    Put short samples in ~/samples/chip/ for the authentic feel; any .wav
    works and gets resampled to the engine rate on load.
    """

    def __init__(self, sr, sample_dir=None):
        self.sr       = sr
        self._samples = []
        self._data    = None
        self._pos     = 0.0
        self._vel     = 1.0
        self._playing = False
        if sample_dir:
            self._load(os.path.expanduser(sample_dir))

    def _load(self, path):
        import glob as _gl
        if not os.path.isdir(path):
            print(f"[chip pcm] folder not found: {path}")
            return
        for fp in sorted(_gl.glob(os.path.join(path, "*.wav"))):
            try:
                from orc1_wavio import read_wav as _read_wav, resample as _rs
                sr, d = _read_wav(fp)                # no scipy (orc1_wavio.py)
                if d.ndim > 1:
                    d = d.mean(axis=1)
                d = d.astype(np.float32)
                if np.abs(d).max() > 1.0:
                    d /= 32768.0
                if sr != self.sr:
                    print(f"[chip pcm] WARNING {os.path.basename(fp)} is {sr} Hz, "
                          f"not {self.sr}: resampled at load", flush=True)
                    d = _rs(d, int(len(d) * self.sr / sr)).astype(np.float32)
                peak = np.abs(d).max()
                if peak > 0:
                    d = d / peak * 0.8
                self._samples.append(d)
                print(f"[chip pcm] {os.path.basename(fp)}  ({len(d)/self.sr:.2f}s)")
            except Exception as e:
                print(f"[chip pcm] failed {fp}: {e}")
        if self._samples:
            print(f"[chip pcm] {len(self._samples)} sample(s) ready")

    def note_on(self, note, vel, *_, **__):
        if not self._samples:
            return
        self._data    = self._samples[int(note) % len(self._samples)]
        self._pos     = 0.0
        self._vel     = vel / 127.0
        self._playing = True

    def note_off(self, note):
        pass   # one-shot -- plays to end regardless

    def render(self, n):
        if not self._playing or self._data is None:
            return None
        data  = self._data
        nd    = len(data)
        if nd < 2 or self._pos >= nd - 1:
            self._playing = False
            return None
        positions = self._pos + np.arange(n, dtype=np.float64)
        n_valid   = int((positions < nd - 1).sum())
        out = np.zeros(n, dtype=np.float32)
        if n_valid > 0:
            p   = positions[:n_valid]
            i0  = p.astype(np.int64)
            fr  = (p - i0).astype(np.float32)
            out[:n_valid] = (data[i0] * (1.0 - fr) + data[i0 + 1] * fr) * self._vel
        if n_valid < n:
            self._playing = False
        self._pos = float(positions[n_valid - 1] + 1.0) if n_valid else float(nd)
        return out


# ==============================================================================
# GAME BOY WAVE CHANNEL  (CH3 analogue)
# ==============================================================================
#
# 8 classic waveforms, each 32 samples × 4-bit (values 0-15).
# The DMG DAC maps these values inverted: 0 -> +1.0 V, 15 -> -1.0 V.
# Selected via CC 1 on the wave channel:  0-15=triangle  16-31=sine
#   32-47=saw_up  48-63=saw_dn  64-79=square  80-95=pulse25
#   96-111=double_saw  112-127=warmth

_GB_WAVE_PRESETS = [
    # triangle
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    # sine
    [8, 9, 10, 12, 13, 14, 14, 15, 15, 15, 14, 14, 13, 12, 10, 9, 8, 6, 5, 3, 2, 1, 1, 0, 0, 0, 1, 1, 2, 3, 5, 6],
    # saw_up
    [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15],
    # saw_dn
    [15, 15, 14, 14, 13, 13, 12, 12, 11, 11, 10, 10, 9, 9, 8, 8, 7, 7, 6, 6, 5, 5, 4, 4, 3, 3, 2, 2, 1, 1, 0, 0],
    # square
    [15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # pulse25
    [0, 0, 0, 0, 0, 0, 0, 0, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15],
    # double_saw
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    # warmth
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 15, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0],
]

# Volume levels: hardware-accurate 4-step scale.
# Maps CC 7 value -> gain applied to output.
#   0-31  = mute (0%)       32-63  = quarter (25%)
#   64-95 = half (50%)      96-127 = full (100%)
# 2026-08-04: this quantisation is now OPT-IN (CC 22 >= 64). By default CC 7 is
# a continuous fader -- the 4-step ladder is a DMG register limit, not a sound,
# and its 0.25-wide jumps were themselves a click source.
_GB_VOL_GAINS = [0.0, 0.25, 0.5, 1.0]

# Click-suppression ramp lengths (seconds). Short enough to be inaudible as a
# fade, long enough that the step they replace stops being a transient.
_GB_GAIN_RAMP_S  = 0.008   # CC 7 / volume-mode gain changes
_GB_XFADE_S      = 0.005   # wave-table swap (CC 1 / CC 23 / custom edits)
_GB_PANIC_FADE_S = 0.005   # all-notes-off fade -- see GameboyWaveVoice.silence

# -- Wave SHAPING (2026-09-24) ---------------------------------------------------
# Drawing 32 separate 4-bit values is tedious on any surface; LSDJ's answer is a
# synth screen where a few parameters GENERATE the table. Ours: 8 harmonic knobs.
#   CC 102-109  harmonic 1-8 level (0 = absent). Any move rebuilds the table from
#               the sum of sines, normalised to the full 0-15 range, and switches
#               playback to the custom table (crossfaded like every other swap).
#   CC 115      STORE arm (momentary, >=64 held): the next Program Change STORES
#               the playing table into that slot instead of recalling it.
#   PC 0-15     recall slot 1-16 (an empty slot is ignored).
# Slots 1-8 start as the 8 presets. The slots, the custom table and the harmonic
# knob positions persist in their own file (~/.orc1_gb_wave.json, owned by the
# chip host) because the per-CC patch file keeps only the LAST value of each CC
# -- which is why a CC20/CC21-drawn wave never survived a restart.
_GB_HARM_CC0   = 102
_GB_HARM_N     = 8
_GB_STORE_CC   = 115
_GB_N_SLOTS    = 16


def _gb_harmonic_table(levels):
    """8 harmonic levels (0-127) -> a 32-step 4-bit table (list of 0-15 ints),
    normalised so the waveform always uses the full 16 levels. All levels 0 ->
    a flat (silent) table."""
    i = np.arange(32, dtype=np.float64)
    s = np.zeros(32, dtype=np.float64)
    for h, lv in enumerate(levels, start=1):
        if lv:
            s += (lv / 127.0) * np.sin(2.0 * np.pi * h * i / 32.0)
    lo, hi = float(s.min()), float(s.max())
    if hi - lo < 1e-9:
        return [8] * 32
    return [int(v) for v in np.round((s - lo) / (hi - lo) * 15.0)]


class GameboyWaveVoice:
    """
    Emulates the Game Boy DMG CH3 programmable wave channel.

    Hardware constraints honoured:
      - 32 4-bit samples in Wave RAM, looping continuously while active
      - Monophonic (one note at a time)
      - DAC output is inverted (a HIGHER sample value is a LOWER voltage)

    Hardware constraints DELIBERATELY NOT honoured by default -- each is an
    opt-in switch, because each was a click source (2026-08-04):
      - volume is continuous, not the 4-level DMG ladder      (CC 22 >= 64)
      - a retrigger keeps phase instead of resetting to 0     (CC 24 >= 64)
      - there is an amplitude envelope; real CH3 has none     (CC 8/9/10/11)

    The DAC is also centred on each table's own mean rather than on a fixed
    7.5, to remove DC (see _dac). That shifts a non-zero-mean preset rather
    than scaling it, so peak sample magnitude is no longer 1.0 for every
    preset -- pulse25 spans [-0.5, +1.5]. Peak-to-peak is unchanged.

    2026-08-04: attack/decay ADDED on top of the above (CC8/CC9) -- real DMG
    CH3 has no envelope at all, but every other chip voice (pulse/triangle/
    noise) has attack+decay wired to its base knobs 3/4, and the wave voice
    having neither felt broken rather than authentic (owner's ask, after
    those knobs on chip's base layer 3/4 went dead for wave specifically).
    A deliberate deviation from real hardware, not a bug fix: shapes the
    AMPLITUDE the same way ChipVoice's ADSR does.

    2026-08-04 (second pass) -- DE-CLICK + DE-RESTRICT. Every known pop source
    in this voice is now removed, and the hardware limits that caused them are
    opt-in switches rather than the only behaviour:

      1. NOTE-OFF WAS AN INSTANT CUTOFF -- the output jumped from whatever
         sample the table happened to be on straight to 0. That is the loudest
         pop in the voice and it fired on EVERY note. There is now a real
         release stage (CC 11, sustain via CC 10), matching the other chip
         voices; the voice stays _active until release finishes.
      2. PHASE RESET ON EVERY TRIGGER -- table index 0 is an extreme value in
         6 of the 8 presets (0 -> +1.0 out), so a retrigger while still
         sounding stepped the signal across the full DAC range. Phase now
         resets only when the voice starts from silence; a retrigger of a
         sounding note keeps phase continuity. CC 24 >= 64 restores the
         authentic always-reset behaviour.
      3. DC OFFSET -- the presets are not zero-mean. pulse25 averages 11.25/15,
         i.e. a -0.5 DC offset, so starting or stopping that waveform thumped
         the whole chip bus even with a perfect envelope (an envelope ramps the
         OFFSET too, which is a thump by definition). The DAC conversion now
         removes the table's own mean instead of assuming 7.5, so every preset
         AND any custom table is DC-free. Waveform shape is untouched.
      4. TABLE SWAPS WERE INSTANT -- changing preset (CC 1), toggling the
         custom table (CC 23) or drawing into Wave RAM (CC 21) replaced the
         table between one sample and the next. Swaps now crossfade over
         _GB_XFADE_S.
      5. VOLUME WAS A 4-STEP LADDER with 0.25-wide instantaneous jumps: both a
         restriction and a click. CC 7 is continuous by default (CC 22 >= 64
         restores the 4 hardware steps), and the gain change is ramped over
         _GB_GAIN_RAMP_S either way.

    CC assignments (same MIDI channel as note-on, CH5 by default):
      CC  1  -- waveform (0-15=triangle  16-31=sine  32-47=saw_up  48-63=saw_dn
                          64-79=square  80-95=pulse25  96-111=double_saw  112-127=warmth)
      CC  7  -- volume   (continuous; 4 hardware steps when CC 22 >= 64)
      CC  8  -- attack
      CC  9  -- decay
      CC 10  -- sustain  (2026-08-04)
      CC 11  -- release  (2026-08-04)
      CC 22  -- volume mode   (>=64 authentic 4-step, <64 continuous)
      CC 24  -- retrigger mode(>=64 authentic phase reset, <64 phase-continuous)

    Frequency: standard 12-TET from MIDI note.  Unlike the real hardware's
    period register, no octave offset is applied -- MIDI C4 = 261.63 Hz.
    The wave channel was typically used one octave below lead voices in
    original Gameboy music, so sending notes one octave lower than your
    melody line is musically correct for bass usage.
    """

    def __init__(self, sr=48000):
        self._sr       = float(sr)
        self._wave     = list(_GB_WAVE_PRESETS[0])  # triangle by default
        # Cached float32 view of _wave AND its mean (2026-08-04 review):
        # render() used to do np.asarray(list) + table.mean() every block.
        # On this numpy build ndarray.mean() costs ~35us REGARDLESS of size
        # (it is the Python-level _methods._mean wrapper, not arithmetic), so
        # that was 46us of every block -- ~25% of the voice's render cost --
        # recomputing a number that only changes when the table changes.
        # Both are refreshed in _set_table(), the single place a table swap
        # happens. This also kills the _wave/_custom aliasing hazard: the
        # cache is an independent copy, so an in-place CC21 edit cannot
        # change the table the RT path is reading until _set_table says so.
        self._tbl      = np.asarray(self._wave, dtype=np.float32).copy()
        self._tbl_mean = float(self._tbl.mean())
        self._vol_cc   = 127    # raw CC 7 value (mode-independent)
        self._vol_quant= False  # CC 22: True = authentic 4-step ladder
        self._gain_cur = 1.0    # smoothed output gain (see _GB_GAIN_RAMP_S)
        self._freq     = 0.0    # base note frequency in Hz (set at note-on)
        self._phase    = 0.0    # position within the 32-sample table (0.0-32.0)
        self._active   = False
        # -- Pitch modulation (chip/GB expansion 2026-07-20) -------------------
        # _freq above is the BASE (note) frequency; pitch_mult is the combined
        # bend × vibrato factor written by NESEngine once per audio block.
        # render() multiplies them together, so bends never lose the note pitch.
        self.pitch_mult = 1.0
        # -- Custom Wave RAM ("9th slot", chip/GB expansion 2026-07-20) --------
        # A user-drawable 32×4-bit table alongside the 8 presets, edited over
        # MIDI: CC20 latches a step index (0-31), CC21 writes a value (0-15)
        # at that index, CC23 >=64 switches playback to the custom table
        # (<64, or any CC1 preset select, reverts to preset mode).
        # Starts as a copy of preset 0 so activating it before drawing
        # anything still makes a sane sound.
        self._custom     = list(_GB_WAVE_PRESETS[0])
        self._use_custom = False
        self._cwi        = 0    # latched custom-write index (0-31), set by CC20
        self._preset_idx = 0    # last preset chosen via CC1 (for CC23 revert)
        # -- Amplitude envelope (2026-08-04, see class docstring) --------------
        # Defaults match the other chip voices (ChipVoice.__init__) so the
        # shared chip A/D/S/R knobs mean the same thing on this channel.
        self.attack        = 0.002
        self.decay         = 0.12
        self.sustain       = 0.8    # CC 10 (2026-08-04); was hardwired to 0.0
        self.release       = 0.15   # CC 11 (2026-08-04); was an instant cutoff
        self._env_state    = 'idle' # idle -> attack -> decay -> sustain -> release
        self._env_level    = 0.0
        # -- Table crossfade state (2026-08-04) --------------------------------
        self._xf_prev      = None   # np array of the table being faded OUT
        self._xf_prev_mean = 0.0    # its cached mean (see _tbl_mean above)
        self._xf_left      = 0      # samples of crossfade remaining
        # -- Retrigger mode (2026-08-04): CC 24 -- see class docstring ---------
        self._hard_retrig = False
        # Release time actually in force. Normally == self.release; PANIC
        # overrides it to a fast fade (see silence()) so a 3-second CC11
        # release cannot delay an all-notes-off by 3 seconds.
        self._rel_time = self.release
        # -- Wave shaping + slots (2026-09-24, see _GB_HARM_CC0) ---------------
        self._harm        = [0] * _GB_HARM_N
        self._slots       = ([list(p) for p in _GB_WAVE_PRESETS] +
                             [None] * (_GB_N_SLOTS - len(_GB_WAVE_PRESETS)))
        self._store_armed = False
        # Set whenever something the wave-state file holds changes; the chip
        # host's persist thread writes the file and clears it (non-RT).
        self.state_dirty  = False

    # -- Wave-state persistence (2026-09-24) -----------------------------------
    def export_state(self):
        """Everything ~/.orc1_gb_wave.json holds, as plain JSON types."""
        return {"slots": [list(s) if s is not None else None for s in self._slots],
                "custom": list(self._custom), "use_custom": bool(self._use_custom),
                "preset": int(self._preset_idx), "harm": list(self._harm)}

    def import_state(self, d):
        """Restore from export_state() output. Tolerant: any malformed field is
        skipped, so a bad file can never stop the chip host booting."""
        def _table(t):
            if (isinstance(t, list) and len(t) == 32
                    and all(isinstance(v, int) and 0 <= v <= 15 for v in t)):
                return list(t)
            return None
        slots = d.get("slots")
        if isinstance(slots, list):
            for k in range(min(_GB_N_SLOTS, len(slots))):
                if slots[k] is None:
                    self._slots[k] = None
                else:
                    t = _table(slots[k])
                    if t is not None:
                        self._slots[k] = t
        h = d.get("harm")
        if (isinstance(h, list) and len(h) == _GB_HARM_N
                and all(isinstance(v, int) and 0 <= v <= 127 for v in h)):
            self._harm = list(h)
        p = d.get("preset")
        if isinstance(p, int) and 0 <= p < len(_GB_WAVE_PRESETS):
            self._preset_idx = p
        c = _table(d.get("custom"))
        if c is not None:
            self._custom = c
        if d.get("use_custom") and c is not None:
            self._use_custom = True
            self._set_table(self._custom)
        elif "use_custom" in d:
            self._use_custom = False
            self._set_table(list(_GB_WAVE_PRESETS[self._preset_idx]))

    def program_change(self, program):
        """PC 0-15: recall that slot -- or, while CC 115 STORE is held, save the
        playing table into it. An empty slot recalls nothing."""
        if not 0 <= program < _GB_N_SLOTS:
            return
        if self._store_armed:
            self._slots[program] = [int(v) for v in self._wave]
            self.state_dirty = True
            return
        slot = self._slots[program]
        if slot is None:
            return
        self._custom     = list(slot)
        self._use_custom = True
        self._set_table(self._custom)
        self.state_dirty = True

    def _gain_target(self):
        """Output gain CC 7 is asking for, in whichever volume mode is set."""
        if self._vol_quant:
            return _GB_VOL_GAINS[min(3, self._vol_cc * 4 // 128)]
        return self._vol_cc / 127.0

    def _xf_clear(self):
        self._xf_prev = None
        self._xf_left = 0

    def _set_table(self, table):
        """Swap the live wave table, crossfading from the old one so the swap
        cannot step the signal (2026-08-04). No fade when silent -- nothing to
        fade from, and it would just delay the first audible block."""
        new = np.asarray(table, dtype=np.float32).copy()
        if self._active and self._env_level > 1e-4:
            base, base_mean = self._tbl, self._tbl_mean
            if self._xf_left > 0 and self._xf_prev is not None:
                # A swap DURING a fade used to discard the fade and snapshot
                # the incoming table, which leaked ~41% of the discontinuity
                # it was meant to remove -- and on a Wave-RAM drag (one CC21
                # per gesture step) that is the NORMAL case, not a rare one.
                # The DAC is linear in the table, so the exact waveform being
                # heard right now IS the linear mix of the two tables at the
                # fade's current position. Blend them and fade from that.
                xf_n = max(1, int(_GB_XFADE_S * self._sr))
                t    = float(xf_n - self._xf_left) / xf_n
                base = self._xf_prev * (1.0 - t) + base * t
                base_mean = float(base.mean())
            self._xf_prev      = base
            self._xf_prev_mean = base_mean
            self._xf_left      = int(_GB_XFADE_S * self._sr)
        self._wave     = table
        self._tbl      = new
        self._tbl_mean = float(new.mean())

    def note_on(self, note, vel):
        """Trigger the wave channel.

        Phase resets ONLY when starting from silence (2026-08-04). The real
        DMG resets phase on every trigger, which steps the DAC mid-waveform
        on a retrigger -- CC 24 >= 64 restores that if wanted.
        """
        self._freq = _midi_to_hz(note)
        if self._hard_retrig or not self._active or self._env_level <= 1e-4:
            self._phase = 0.0
            # Starting from silence: land the CC 7 gain IMMEDIATELY instead of
            # ramping to it. The ramp exists to smooth an AUDIBLE change, and
            # there is nothing audible to smooth from -- ramping here started
            # the note at the gain in force before the last silence, with the
            # 2ms envelope already fully open under an 8ms ramp. Measured
            # +21dB overshoot on the first note after a volume change made
            # between phrases, and a 0.74 burst after asking for MUTE.
            self._gain_cur = self._gain_target()
            # Belt-and-braces: every path that clears _active (release
            # completing, silence()) already clears the fade, so as of
            # 2026-08-04 this line is REDUNDANT and no test covers it --
            # deleting it does not turn the suite red, and that is stated
            # rather than papered over. It stays because "starting a note
            # from silence" is the one place a stale fade could blend a dead
            # table into a new note's opening samples, and a future path that
            # deactivates the voice some other way would reintroduce that.
            self._xf_clear()
        self._active = True
        self._env_state = 'attack'
        # _env_level is NOT reset here -- a fast retrigger continues from
        # its current level (same reasoning as PolyVoice), so it doesn't
        # click back down to 0 and re-ramp on every note in a fast run.

    def restrike(self):
        """Retrigger (NESEngine CC 113): restart the envelope from silence. Only
        ever called while the retrigger gate has faded the output to 0, so the
        level jump is inaudible."""
        if self._freq > 0:
            self._active    = True
            self._env_state = 'attack'
            self._env_level = 0.0

    def note_off(self, note):
        """Enter the release stage. This used to be an instant cutoff, which
        was the voice's biggest click source (see class docstring)."""
        if self._env_state not in ('idle', 'release'):
            self._rel_time  = self.release
            self._env_state = 'release'

    def silence(self):
        """PANIC path -- silence this voice NOW, whatever CC 11 says.

        note_off() releases, and CC 11 can set that to 3 seconds, so routing
        all_notes_off() through it would have made a panic take 3 seconds to
        quiet this channel -- on an instrument whose stuck-note history is
        the reason the panic path exists at all. A hard cut would be a click,
        so this is a fixed fast fade instead: audibly immediate, still
        de-clicked.
        """
        if self._active and self._env_level > 1e-4:
            self._rel_time  = _GB_PANIC_FADE_S
            self._env_state = 'release'
        else:
            self._active    = False
            self._env_state = 'idle'
            self._env_level = 0.0
            self._xf_clear()

    def _env(self, n):
        """Block-vectorised A/D/S/R ramp, same algorithm as PolyVoice._env
        (see orc1_synth.py ~line 1374). Clearing _active when release finishes
        is what lets NESEngine.render() stop mixing this voice."""
        out = np.empty(n, dtype=np.float32)
        sr  = self._sr
        i   = 0
        while i < n:
            st = self._env_state
            if st == 'idle':
                out[i:] = 0.0; self._env_level = 0.0; break
            if st == 'sustain':
                if abs(self._env_level - self.sustain) > 1e-4:
                    # CC 10 moved under a held note. Writing the new level
                    # flat stepped the envelope between two blocks -- measured
                    # 6.5x the waveform's own inter-sample step, i.e. a click,
                    # in the voice whose whole point is not clicking. Ease to
                    # it on the decay curve instead. Terminal: the decay
                    # branch below advances i and returns here at most once.
                    self._env_state = 'decay'
                    continue
                out[i:] = self.sustain; self._env_level = self.sustain
                break
            if st == 'attack':
                rate   = 1.0 / max(1.0, self.attack * sr)
                remain = int(np.ceil((1.0 - self._env_level) / rate)) if rate > 0 else 1
                take   = max(1, min(remain, n - i))
                ramp   = self._env_level + rate * np.arange(1, take + 1, dtype=np.float32)
                np.minimum(ramp, 1.0, out=ramp)
                out[i:i + take] = ramp
                self._env_level = float(ramp[-1]); i += take
                if self._env_level >= 1.0 - 1e-9:
                    self._env_level = 1.0; self._env_state = 'decay'
                continue
            if st in ('decay', 'release'):
                rel    = (st == 'release')
                target = 0.0 if rel else self.sustain
                t_s    = self._rel_time if rel else self.decay
                a = 1.0 - np.exp(-1.0 / max(1.0, t_s * sr * 0.2))
                k = np.arange(1, (n - i) + 1, dtype=np.float64)
                curve = target + (self._env_level - target) * (1.0 - a) ** k
                done = np.abs(curve - target) < 1e-4
                take = int(np.argmax(done)) + 1 if done.any() else (n - i)
                out[i:i + take] = curve[:take].astype(np.float32)
                self._env_level = float(curve[take - 1]); i += take
                if np.abs(self._env_level - target) < 1e-4:
                    self._env_level = target
                    if rel or target < 1e-4:
                        # Silent -- drop out of the mix (NESEngine.render gates
                        # on _active) instead of rendering zeros forever.
                        # `target < 1e-4` covers a DECAY to a sustain of 0,
                        # which is the "authentic pluck" setting: without it
                        # the voice sat in 'sustain' at level 0 with
                        # _active True FOREVER, measured at 5.5% of one core
                        # per held-then-decayed note. PolyVoice._env has done
                        # exactly this since it was written (see ~line 1404);
                        # this is the divergence the docstring denied.
                        self._env_state = 'idle'
                        self._active    = False
                        self._xf_clear()
                    else:
                        self._env_state = 'sustain'
                continue
            out[i:] = 0.0; break
        return out

    def set_cc(self, cc, value):
        if cc == 1:
            # Waveform select -- 8 presets, 16 CC values each.
            # Selecting a preset always drops out of custom-table mode.
            idx = min(7, value // 16)
            self._preset_idx = idx
            self._use_custom = False
            self._set_table(list(_GB_WAVE_PRESETS[idx]))
            self.state_dirty = True
        elif _GB_HARM_CC0 <= cc < _GB_HARM_CC0 + _GB_HARM_N:
            # Harmonic 1-8 level (CC 102-109, 2026-09-24). Rebuilds the custom
            # table from all 8 levels and plays it; replaces whatever custom
            # wave was there (store it to a slot first to keep it).
            self._harm[cc - _GB_HARM_CC0] = max(0, min(127, value))
            self._custom     = _gb_harmonic_table(self._harm)
            self._use_custom = True
            self._set_table(self._custom)
            self.state_dirty = True
        elif cc == _GB_STORE_CC:
            # STORE arm (CC 115, momentary): while held (>=64), the next
            # Program Change saves the playing table into that slot.
            self._store_armed = value >= 64
        elif cc == 7:
            # Volume. Continuous by default; CC 22 selects the authentic
            # 4-step hardware ladder. Either way the CHANGE is ramped in
            # render() -- an instantaneous gain step is a click.
            self._vol_cc = max(0, min(127, value))
        elif cc == 22:
            # Volume mode: >= 64 = authentic 4-step, < 64 = continuous.
            self._vol_quant = value >= 64
        elif cc == 24:
            # Retrigger mode: >= 64 = authentic phase reset on every trigger.
            self._hard_retrig = value >= 64
        elif cc == 10:
            # Sustain level (2026-08-04). 0 = the note plucks to silence
            # (what this voice used to do unconditionally); 127 = holds flat,
            # which is what real DMG CH3 does since it has no envelope.
            self.sustain = max(0.0, min(1.0, value / 127.0))
        elif cc == 11:
            # Release (2026-08-04) -- same range as the other chip voices'
            # CC11. This is what replaced the instant note-off cutoff.
            self.release = _cc_to_time(value, 0.005, 3.0)
        elif cc == 8:
            # Attack (2026-08-04, see class docstring) -- same range as the
            # other chip voices' CC8 (ChipVoice, via NESEngine.set_cc).
            self.attack = _cc_to_time(value, 0.001, 3.0)
        elif cc == 9:
            # Decay (2026-08-04) -- same range as the other chip voices' CC9.
            self.decay = _cc_to_time(value, 0.005, 3.0)
        elif cc == 20:
            # Custom Wave RAM: latch which of the 32 steps CC21 will write.
            self._cwi = min(31, value * 32 // 128)
        elif cc == 21:
            # Custom Wave RAM: write a 4-bit value (0-15) at the latched step.
            # If the custom table is currently playing, self._wave IS this
            # same list (aliased on activate below), so the edit is audible
            # immediately -- render() re-reads the table every block.
            # 2026-08-04: an in-place edit of the LIVE table is a waveform
            # discontinuity like any other, so it crossfades too. _set_table
            # snapshots the pre-edit table, then re-points at the same list.
            # The aliasing trap that used to need a snapshot-before-write
            # here is GONE: _tbl is an independent copy, so mutating
            # _custom cannot change what the render path reads until
            # _set_table refreshes the cache -- which is also what arms the
            # crossfade, from the correct pre-edit table.
            self._custom[self._cwi] = min(15, value * 16 // 128)
            if self._use_custom:
                self._set_table(self._custom)
            self.state_dirty = True
        elif cc == 23:
            # Activate (>=64) / deactivate (<64) the custom table.
            if value >= 64:
                self._use_custom = True
                self._set_table(self._custom)   # alias: live edits are heard
            else:
                self._use_custom = False
                self._set_table(list(_GB_WAVE_PRESETS[self._preset_idx]))
            self.state_dirty = True

    def render(self, n):
        if not self._active or self._freq <= 0:
            return np.zeros(n, dtype=np.float32)

        # Vectorised wavetable playback -- no Python loop in the hot path.
        # Phase advances at 32× frequency so one full wave cycle = one period.
        # Effective frequency = base note freq × pitch modulation (bend/vibrato,
        # written by NESEngine each block; 1.0 when nothing is bending).
        phase_inc = 32.0 * (self._freq * self.pitch_mult) / self._sr
        phases    = (self._phase +
                     np.arange(n, dtype=np.float64) * phase_inc) % 32.0
        # Advance stored phase for the next render call
        self._phase = float((phases[-1] + phase_inc) % 32.0)

        # Nearest-neighbour wavetable lookup (4-bit, 16 amplitude levels)
        indices  = phases.astype(np.int32) % 32
        analog   = self._dac(self._tbl, self._tbl_mean, indices)

        # Crossfade out of the previous table if one was just swapped in
        # (2026-08-04) -- an instant table change is a step discontinuity.
        if self._xf_left > 0 and self._xf_prev is not None:
            m    = min(self._xf_left, n)
            xf_n = max(1, int(_GB_XFADE_S * self._sr))
            done = xf_n - self._xf_left           # samples already faded
            t    = ((done + np.arange(1, m + 1, dtype=np.float32)) /
                    float(xf_n)).clip(0.0, 1.0)
            old  = self._dac(self._xf_prev, self._xf_prev_mean, indices[:m])
            analog[:m] = old * (1.0 - t) + analog[:m] * t
            self._xf_left -= m
            if self._xf_left <= 0:
                self._xf_clear()

        env = self._env(n)
        return (analog * self._gain(n) * env).astype(np.float32)

    @staticmethod
    def _dac(table, mean, indices):
        """4-bit table -> analog, DC-FREE (2026-08-04).

        The DMG DAC is inverted and centred on 7.5. Using a fixed 7.5 centre
        leaves a DC offset on any table that is not zero-mean -- pulse25
        averages 11.25, i.e. -0.5 DC, so simply starting or stopping that
        waveform thumped the bus no matter how clean the envelope was (an
        envelope scales the offset too). Centring on the table's own mean
        removes that while leaving the waveform's SHAPE bit-identical.

        `mean` is passed in rather than computed here: it only changes when
        the table does, and ndarray.mean() costs ~35us a call on this build
        whatever the array size. See the _tbl/_tbl_mean cache in __init__.

        NOTE the amplitude consequence, which the class docstring's
        "sample 0 -> +1.0" bullet no longer describes: mean-centring SHIFTS
        each preset rather than scaling it, so a table whose mean is not 7.5
        now peaks above 1.0 -- pulse25 spans [-0.5, +1.5]. Peak-to-peak is
        unchanged at 2.0 and the chip bus master is 0.35, so there is no
        clipping risk; but the peak SAMPLE is 1.5x on that preset.
        """
        return (mean - table[indices]) / 7.5

    def _gain(self, n):
        """Output gain for this block, ramped toward the CC 7 target.

        Returns a scalar when the gain is already there (the common case, no
        array cost) and a per-sample ramp while it is moving. Both the 4-step
        ladder and the continuous fader route through here -- a gain STEP is
        an amplitude discontinuity, which is a click regardless of how
        authentic the value it steps to happens to be.
        """
        target = self._gain_target()
        if abs(target - self._gain_cur) < 1e-6:
            self._gain_cur = target
            return target
        step = 1.0 / max(1.0, _GB_GAIN_RAMP_S * self._sr)
        k    = min(n, int(np.ceil(abs(target - self._gain_cur) / step)))
        out  = np.empty(n, dtype=np.float32)
        sign = 1.0 if target > self._gain_cur else -1.0
        ramp = self._gain_cur + sign * step * np.arange(1, k + 1, dtype=np.float32)
        if sign > 0:
            np.minimum(ramp, target, out=ramp)
        else:
            np.maximum(ramp, target, out=ramp)
        out[:k] = ramp
        out[k:] = target
        self._gain_cur = float(out[-1])
        return out



# ==============================================================================
# PER-VOICE PERFORMANCE MODS (2026-09-24) -- tracker-style glide / pan / arp /
# trill / pitch slide / tremolo / retrigger for every chip voice.
# ==============================================================================
# Each is a CC on the voice's OWN channel (so any controller can drive it; the
# MPK / Fire / web just point knobs at these numbers). Every default is "off" and
# the render path skips straight past a voice with nothing on -- so an untouched
# patch renders byte-identically to before this layer existed.
#   CC   5  glide (portamento) time      0 = off, else 5 ms .. 2 s
#   CC  86  pan                          64 = centre (balance law: centre = unity)
#   CC  87  arp interval A               0-24 semitones (0 = off)
#   CC  88  arp interval B               0-24 semitones; A only (or B only) = TRILL
#   CC  89  arp / trill speed            4 .. 60 steps/s
#   CC  90  pitch slide amount           64 = none, 0 = -24 st, 127 = +24 st
#   CC 110  pitch slide time             10 ms .. 2 s (to reach the full amount)
#   CC 111  tremolo depth                0 = off
#   CC 112  tremolo rate                 0.5 .. 20 Hz
#   CC 113  retrigger rate               0 = off, else 1 .. 40 strikes/s (while held)
# Arp steps and retrigger strikes are SAMPLE-accurate (the voice is rendered in
# segments split at each step), not snapped to the audio block -- a block-snapped
# 60 Hz arp alternates 16/21 ms steps, which is audible as a limp.
_CHIP_RETRIG_EDGE_S = 0.002   # retrigger gate fade in/out -- the restrike is hidden in it
_CHIP_STEREO_HOLD_S = 0.5     # stay on the stereo path this long after pans re-centre


def _cc_to_semis(v):
    """0..127 -> 0..24 semitones."""
    return int(round(v * 24 / 127.0))


def _cc_to_bipolar_semis(v):
    """0..127 -> -24..+24 semitones, 64 = 0."""
    return int(round((v - 64) / 64.0 * 24))


class _ChipMod:
    """Settings + runtime state of one chip voice's performance mods."""

    def __init__(self):
        self.glide_t    = 0.0    # CC 5   seconds; 0 = off
        self.pan        = 64     # CC 86
        self.arp_a      = 0      # CC 87  semitones
        self.arp_b      = 0      # CC 88  semitones
        self.arp_hz     = 15.0   # CC 89
        self.slide_semi = 0      # CC 90
        self.slide_t    = 0.1    # CC 110
        self.trem_depth = 0.0    # CC 111
        self.trem_hz    = 6.0    # CC 112
        self.retrig_hz  = 0.0    # CC 113; 0 = off
        # runtime
        self.glide_semi = 0.0    # current glide offset (semitones), moves to 0
        self.glide_rate = 0.0    # semitones / second
        self.slide_el   = 0.0    # seconds since note start (slide progress)
        self.arp_pos    = 0.0    # samples into the current arp step
        self.arp_idx    = 0
        self.trem_ph    = 0.0    # 0..1, free-running
        self.rt_pos     = 0.0    # samples since the last strike
        self.gL = 1.0            # pan gains actually in force (ramped)
        self.gR = 1.0
        self.live = False        # sounded last block (else a pan lands without a ramp)

    def arp_steps(self):
        """Semitone offsets to cycle, or None when arp/trill is off."""
        if not self.arp_a and not self.arp_b:
            return None
        return [0] + [s for s in (self.arp_a, self.arp_b) if s]

    def pan_target(self):
        """Balance law: centre = (1, 1), so a centred voice is untouched."""
        p = self.pan
        if p <= 64:
            return 1.0, p / 64.0
        return (127 - p) / 63.0, 1.0

    def note_start(self, prev_hz, new_hz):
        """A note began on this voice. prev_hz = the pitch it was sounding at
        (None if silent) -- glide starts there and runs to the new note."""
        if self.glide_t > 0.0 and prev_hz and new_hz and new_hz > 0.0:
            self.glide_semi = 12.0 * float(np.log2(prev_hz / new_hz))
            self.glide_rate = abs(self.glide_semi) / self.glide_t
        else:
            self.glide_semi = 0.0
        self.slide_el = 0.0
        self.arp_pos  = 0.0
        self.arp_idx  = 0
        self.rt_pos   = 0.0

    def pitch_semis(self, dt):
        """Glide + slide offset for this block (value at block start), then
        advance both by dt seconds."""
        semi = self.glide_semi
        if self.slide_semi:
            t = self.slide_t
            frac = 1.0 if t <= 0.0 else min(1.0, self.slide_el / t)
            semi += self.slide_semi * frac
            self.slide_el += dt
        if self.glide_semi:
            step = self.glide_rate * dt
            if abs(self.glide_semi) <= step:
                self.glide_semi = 0.0
            else:
                self.glide_semi -= step if self.glide_semi > 0 else -step
        return semi


class NESEngine:
    """
    Replaces ChipSynth with authentic NES/Gameboy voice limits.
    API-compatible with ChipSynth (same note_on / note_off / set_cc / render).

    Each hardware channel is monophonic: new note immediately replaces old.
    Playing two simultaneous pulse melodies means alternating CC1 between
    Pulse 1 and Pulse 2, just like writing to the real APU registers.
    """

    # 'noise2' (2026-09-24): a SECOND, independent noise voice so white and
    # buzzy noise can sound at once (drums). Its own module key chip_noise2 binds
    # it to any channel; CC 114 on the noise channel can also key-split notes
    # onto it so one pad bank plays both. Appended LAST so render/sum order of
    # the original four is unchanged.
    _CHANNELS = ('pulse1', 'pulse2', 'triangle', 'noise', 'noise2')
    _NOISE_VOICES = ('noise', 'noise2')
    # Voices the per-voice mods render path walks, in the original sum order.
    _VOICES = _CHANNELS + ('wave',)

    def __init__(self, sample_dir=None):
        sr = SAMPLE_RATE
        self.sr = sr
        self._ch    = {name: ChipVoice(sr) for name in self._CHANNELS}
        self._pcm   = PCMChannel(sr, sample_dir)
        # Game Boy programmable wave channel -- sits alongside the NES voices.
        self._wave  = GameboyWaveVoice(sr)
        # No _active needed -- voice is determined by the MIDI channel notes arrive on
        self._p1_duty = 0.5
        self._p2_duty = 0.5
        self._noise_long  = True    # noise:  CC3 <64 = white (15-bit LFSR)
        self._noise2_long = False   # noise2: defaults to BUZZY so it is useful at once
        # CC 114 on the noise channel: 0 = off, else notes >= this number play
        # noise2 instead. _split_notes remembers which held notes went where, so
        # each note-off releases the voice its note-on started even if the split
        # point moved in between.
        self._noise_split = 0
        self._split_notes = {}
        # Per-voice performance mods (glide/pan/arp/slide/tremolo/retrigger).
        self._mod = {name: _ChipMod() for name in self._VOICES + ('pcm',)}
        self._pm  = {name: 1.0 for name in self._VOICES}   # this block's pitch mult
        self._st_hold = 0     # samples left on the stereo path after re-centring
        self.attack  = 0.002
        self.decay   = 0.12
        self.sustain = 0.8
        self.release = 0.15
        self.master  = 0.35
        self.octave  = 0
        self._age    = 0
        self.filt    = BiquadFilter(sr)
        # Right-channel twin of filt for the panned (stereo) path. Every filter
        # CC sets both (_filt_set); on the mono path its state is kept equal to
        # filt's so switching to stereo mid-note cannot step the output.
        self.filt_R  = BiquadFilter(sr)
        # Pitch-tracked ring mod (CC12/14/15), shared across the four APU
        # channels by reference. The GB wave channel and PCM have their own
        # render() and are deliberately NOT covered -- see _test_ringmod.py.
        self.rm      = RingMod()
        for _v in self._ch.values():
            _v.rm = self.rm

        # Per-voice monophonic note stacks.
        # Each stack is a list of [note, velocity, timestamp, hold_count] entries,
        # newest at the end.  hold_count > 1 means a double-trigger was absorbed.
        self._stacks = {name: [] for name in list(self._CHANNELS) + ['wave']}

        # -- Dual-voice coupling (P1+P2 played together from one keyboard) --
        # coupling_mode: one of NES_COUPLE_MODES
        # detune_cents:  how far P2 is detuned from P1 in DETUNE mode.
        #                +7 cents gives ~1.8Hz beating at A4 -- subtle shimmer.
        #                +15 cents gives ~3.8Hz -- faster chorus effect.
        self.coupling_mode = 'none'
        self.detune_cents  = 7

        # -- P1/P2 stereo spread (audibility aid for coupling modes) --
        # Coupling modes like octave/unison are subtle in mono -- same
        # timbre stacked at a musical interval blends together rather than
        # sounding like two voices. When this is on, get_p1p2_stereo() below
        # emphasises pulse1 toward the left channel and pulse2 toward the
        # right. Only has an audible effect once SynthEngine.stereo_mode is
        # also on -- turning this on auto-enables stereo_mode (see the
        # 'chip_cc' dispatch in SynthEngine.render()), since otherwise the
        # spread is silently discarded by the mono downmix.
        self.p1p2_spread   = False
        self._last_p1_solo = None   # this block's pulse1-only signal
        self._last_p2_solo = None   # this block's pulse2-only signal

        # ══════════════════════════════════════════════════════════════════
        # Pitch modulation state (chip/GB expansion 2026-07-20)
        # Bend + vibrato exist independently for pulse1 / pulse2 / wave
        # (each arrives on its own MIDI channel). Every audio block,
        # _update_pitch_mod() combines them (× the pulse1 sweep) into each
        # voice's pitch_mult; the voices multiply that into their base_freq.
        # ══════════════════════════════════════════════════════════════════
        # Manual pitchwheel value per voice, -8192..8191 (0 = centred).
        self._bend      = {'pulse1': 0, 'pulse2': 0, 'wave': 0}
        # Vibrato: depth in CENTS (CC77: 0-127 -> 0-100¢, 0 = off) and
        # rate in Hz (CC78: 0-127 -> 0.1-12 Hz).
        self._vib_depth = {'pulse1': 0.0, 'pulse2': 0.0, 'wave': 0.0}
        self._vib_rate  = {'pulse1': 5.0, 'pulse2': 5.0, 'wave': 5.0}
        # Free-running LFO phase (0..1) -- deliberately NOT reset at note-on,
        # so vibrato feels like tracker vibrato (owner's call, spec §7.3).
        self._vib_phase = {'pulse1': 0.0, 'pulse2': 0.0, 'wave': 0.0}

        # -- Hardware sweep (Pulse 1 only -- authentic DMG NR10) -----------
        # Settings (survive across notes, set by CC79/80/81):
        self._sweep_time  = 0      # 0-7; 0 = sweep off. Step every time × 7.8ms
        self._sweep_shift = 0      # 0-7; 0 = steps change nothing
        self._sweep_up    = True   # True = frequency rises (CC81 < 64)
        # Runtime state (re-armed on every pulse1 note-on):
        self._sweep_on    = False  # armed + running for the current note
        self._sweep_X0    = 0      # 11-bit GB freq register value at note-on
        self._sweep_X     = 0      # current register value (steps move this)
        self._sweep_acc   = 0.0    # seconds accumulated toward the next step
        self._sweep_mult  = 1.0    # swept_freq / base_freq (composes w/ bend)

    def _couple_note_on(self, note, vel):
        """Drive pulse2 according to the current coupling mode.
        Called automatically from note_on when coupling_mode != 'none'
        and the incoming voice is pulse1.
        """
        mode = self.coupling_mode
        if mode == 'none':
            return
        self._age += 1
        if mode == 'detune':
            # Same note but P2 frequency shifted by detune_cents
            p2 = self._ch['pulse2']
            prev = self._voice_hz('pulse2')
            p2.note_on(note, vel, 'pulse', self._p2_duty,
                       self.attack, self.decay, self.sustain, self.release,
                       self._age, self.octave)
            # Override frequency with cents offset applied.
            # Written to base_freq (not .freq) so the per-block pitch-mod
            # recompute (base_freq × pitch_mult) keeps the detune instead of
            # clobbering it back to the plain note frequency.
            base_freq = _midi_to_hz(note + 12 * self.octave)
            p2.base_freq = p2.freq = base_freq * (2.0 ** (self.detune_cents / 1200.0))
            self._mod['pulse2'].note_start(prev, p2.base_freq)
            return
        semis = _NES_COUPLE_SEMIS.get(mode, 0)
        coupled_note = max(0, min(127, note + semis))
        p2 = self._ch['pulse2']
        prev = self._voice_hz('pulse2')
        p2.note_on(coupled_note, vel, 'pulse', self._p2_duty,
                   self.attack, self.decay, self.sustain, self.release,
                   self._age, self.octave)
        self._mod['pulse2'].note_start(prev, p2.base_freq)

    # -- helpers for the per-voice mods / noise2 (2026-09-24) -----------------
    def _voice_hz(self, name):
        """The pitch a voice is sounding at right now (for glide), or None."""
        if name == 'wave':
            w = self._wave
            return w._freq * w.pitch_mult if (w._active and w._freq > 0) else None
        v = self._ch.get(name)
        if v is None or not v.active() or v.note is None:
            return None
        return v.base_freq * v.pitch_mult

    def _nseq_for(self, voice):
        """The LFSR table a noise voice plays (each noise voice has its own CC3)."""
        long_ = self._noise2_long if voice == 'noise2' else self._noise_long
        return _GB_LFSR_15 if long_ else _GB_LFSR_7

    def _filt_set(self, **kw):
        self.filt.set(**kw)
        self.filt_R.set(**kw)

    def _couple_note_off(self, note):
        """Release pulse2 when coupling is active and pulse1 is released.
        Only silences P2 if it's currently sounding -- won't cut off an
        independently sequenced P2 note that happened to overlap.
        """
        if self.coupling_mode == 'none':
            return
        p2 = self._ch['pulse2']
        if p2.env_state not in ('idle', 'release'):
            p2.note_off()

    def set_coupling(self, mode):
        """Set the dual-voice coupling mode. Silences P2 on mode change
        so there's no orphaned note from the previous mode."""
        if mode in NES_COUPLE_MODES:
            self.coupling_mode = mode
            p2 = self._ch['pulse2']
            if p2.env_state not in ('idle', 'release'):
                p2.note_off()
            print(f"  [NES] coupling -> {mode}"
                  + (f"  ({self.detune_cents:+d}¢)" if mode == 'detune' else ""))

    def cycle_coupling(self):
        """Step forward through NES_COUPLE_MODES. Returns new mode name."""
        idx = NES_COUPLE_MODES.index(self.coupling_mode)
        self.set_coupling(NES_COUPLE_MODES[(idx + 1) % len(NES_COUPLE_MODES)])
        return self.coupling_mode

    def set_detune(self, cents):
        """Set detune amount in cents (clamped ±50). Applies immediately
        if P2 is currently sounding in DETUNE mode."""
        self.detune_cents = max(-50, min(50, int(cents)))
        if self.coupling_mode == 'detune':
            p2 = self._ch['pulse2']
            if p2.note is not None and p2.env_state not in ('idle', 'release'):
                # base_freq, not .freq -- see _couple_note_on for why.
                base_freq = _midi_to_hz(p2.note + 12 * self.octave)
                p2.base_freq = p2.freq = base_freq * (2.0 ** (self.detune_cents / 1200.0))

    def _apply_live_adsr(self):
        """Push the current shared attack/decay/sustain/release onto every
        currently-active pulse1/pulse2/noise voice, so an envelope change
        affects a note that's already sounding instead of only the NEXT
        trigger. Deliberately excludes 'triangle' -- the NES triangle
        channel has no volume envelope in real hardware, so its note_on
        always hardcodes instant on/off ADSR values regardless of these
        shared settings (see note_on below); pushing the shared envelope
        onto it here would wrongly give it one.
        Safe to call on every ADSR CC: _env() (in PolyVoice) already
        re-reads .attack/.decay/.sustain/.release every render block, this
        just refreshes them sooner than the next note_on would. Sustain
        changing mid-decay retargets the decay curve smoothly; attack/decay/
        release changing mid-ramp changes the RATE going forward from the
        current level, not the level itself -- no click either way."""
        for name in ('pulse1', 'pulse2', 'noise', 'noise2'):
            v = self._ch[name]
            if v.active():
                v.attack, v.decay, v.sustain, v.release = (
                    self.attack, self.decay, self.sustain, self.release)

    def _apply_live_octave(self):
        """Re-pitch every currently-active chip voice (including triangle
        and noise -- octave affects pitch, not envelope shape, so unlike
        _apply_live_adsr there's no reason to exclude triangle here) to the
        new octave. Unlike duty/ADSR this IS an audible instant jump (a
        full octave) rather than a smooth parameter change -- that's just
        what changing octave transpose on a held note means. Only affects
        notes already sounding at the moment the dial moves; new notes
        already pick up self.octave naturally via note_on."""
        for voice in self._ch.values():
            if voice.active() and voice.note is not None:
                # Write BOTH: base_freq so the pulse voices' per-block
                # pitch-mod recompute (base_freq × pitch_mult) uses the new
                # octave, and .freq directly so voices that don't recompute
                # (noise reads _nhold, not freq) stay consistent too.
                voice.base_freq = voice.freq = _midi_to_hz(voice.note + 12 * self.octave)

    def note_on(self, voice, note, vel):
        """voice: 'pulse1' | 'pulse2' | 'triangle' | 'noise' | 'pcm' | 'wave'"""
        self._age += 1

        # PCM is one-shot -- no stack semantics, pass straight through.
        if voice == 'pcm':
            self._pcm.note_on(note, vel)
            return

        # Noise key split (CC 114): notes at/above the split play noise2.
        if voice == 'noise':
            if self._noise_split and note >= self._noise_split:
                self._split_notes[note] = 'noise2'
                voice = 'noise2'

        now = time.perf_counter()

        # -- Note stack: absorb double-triggers, push new notes ----------------
        stack    = self._stacks.get(voice, [])
        existing = next((e for e in stack if e[0] == note), None)
        if existing and (now - existing[2]) < _MONO_DEBOUNCE_S:
            # Same note within debounce window -- pad double-trigger absorbed.
            existing[3] += 1
            return
        if existing:
            # Same note outside debounce window = intentional retrigger.
            stack.remove(existing)
        stack.append([note, vel, now, 1])
        self._stacks[voice] = stack

        # -- Trigger the hardware voice ----------------------------------------
        prev_hz = self._voice_hz(voice) if voice in self._mod else None
        if voice == 'wave':
            self._wave.note_on(note, vel)
            self._mod['wave'].note_start(prev_hz, self._wave._freq)
            return
        if voice not in self._ch:
            return
        ch_voice = self._ch[voice]
        if voice == 'triangle':
            # Authentic NES: triangle has no volume control -- always on or off.
            ch_voice.note_on(note, 127, 'triangle', 0.5,
                             0.001, 0.0, 1.0, 0.001, self._age, self.octave)
        elif voice in self._NOISE_VOICES:
            ch_voice.note_on(note, vel, 'noise', 0.5,
                             self.attack, self.decay, self.sustain, self.release,
                             self._age, self.octave)
            # Authentic LFSR: the mode (CC3) selects WHICH precomputed bit
            # sequence plays -- 15-bit long/white vs 7-bit short/metallic.
            # (Replaces the old "_nhold // 8" fake, which only raised the
            # pitch; pitch now stays with _nhold in both modes.)
            ch_voice._nseq = self._nseq_for(voice)
        else:
            duty = self._p1_duty if voice == 'pulse1' else self._p2_duty
            ch_voice.note_on(note, vel, 'pulse', duty,
                             self.attack, self.decay, self.sustain, self.release,
                             self._age, self.octave)
            if voice == 'pulse1':
                # Re-arm the hardware sweep for the new note (no-op if off).
                self._sweep_rearm()
            # If coupling active and this is pulse1, also drive pulse2.
            # Pulse2 arriving independently (seq track) does NOT trigger
            # coupling -- that would override the sequenced note.
            if voice == 'pulse1' and self.coupling_mode != 'none':
                self._couple_note_on(note, vel)
        self._mod[voice].note_start(prev_hz, ch_voice.base_freq)

    def note_off(self, note, on_voice=None):
        """Monophonic note-off with stack fallback and double-trigger protection.
        Releasing a note falls back to the previously held note if one exists.
        Double-triggers are absorbed -- hold_count must reach zero to truly release.

        on_voice = the chip channel the note-off actually arrived on. Pass it
        whenever it is known (2026-07-30). Without it this scans EVERY stack and
        releases the first channel holding that note number, so the same note
        held on two chip voices releases the WRONG one -- and the other is then
        stranded, because its own note-off has already been consumed. The
        voice-blind scan is kept only as a fallback for callers that genuinely
        do not know the channel.
        """
        # A SPECIFIED voice is always trusted, even if it has no stack. 'pcm' is
        # a real, addressable chip voice (CH_CHIP_PCM) but is deliberately
        # stackless -- it is a one-shot. Requiring `on_voice in self._stacks`
        # here meant a PCM note-off fell through to the voice-blind scan and
        # could release an unrelated voice holding the same note number, which
        # is exactly the bug this signature was added to kill.
        # Noise key split: release the voice this note's note-on actually used.
        # (get, not pop: a double-trigger's first note-off only decrements the
        # hold count, and the second must still find noise2.)
        # Only if noise2 still holds it -- otherwise it is a plain noise note.
        if (on_voice == 'noise' and note in self._split_notes
                and any(e[0] == note for e in self._stacks['noise2'])):
            on_voice = self._split_notes[note]
        if on_voice is not None:
            items = [(on_voice, self._stacks.get(on_voice, []))]
        else:
            items = list(self._stacks.items())
        for voice, stack in items:
            entry = next((e for e in stack if e[0] == note), None)
            if entry is None:
                continue

            # Decrement hold count -- absorbs double-trigger note-offs
            entry[3] -= 1
            if entry[3] > 0:
                return

            stack.remove(entry)
            if voice == 'noise2':
                self._split_notes.pop(note, None)

            if stack:
                # Notes remain: resume the most recently pressed (last entry)
                prev_note, prev_vel, _, _ = stack[-1]
                prev_hz = self._voice_hz(voice) if voice in self._mod else None
                if voice == 'wave':
                    self._wave.note_on(prev_note, prev_vel)
                    self._mod['wave'].note_start(prev_hz, self._wave._freq)
                elif voice in self._ch:
                    ch_voice = self._ch[voice]
                    self._age += 1
                    if voice == 'triangle':
                        ch_voice.note_on(prev_note, 127, 'triangle', 0.5,
                                         0.001, 0.0, 1.0, 0.001, self._age, self.octave)
                    elif voice in self._NOISE_VOICES:
                        ch_voice.note_on(prev_note, prev_vel, 'noise', 0.5,
                                         self.attack, self.decay, self.sustain,
                                         self.release, self._age, self.octave)
                        # Same LFSR table select as the main note_on path.
                        ch_voice._nseq = self._nseq_for(voice)
                    else:
                        duty = self._p1_duty if voice == 'pulse1' else self._p2_duty
                        ch_voice.note_on(prev_note, prev_vel, 'pulse', duty,
                                         self.attack, self.decay, self.sustain,
                                         self.release, self._age, self.octave)
                        if voice == 'pulse1':
                            # A stack-fallback retrigger is a real note start:
                            # re-arm the sweep just like the main note_on path.
                            self._sweep_rearm()
                    self._mod[voice].note_start(prev_hz, ch_voice.base_freq)
            else:
                # Stack empty -- silence the voice
                if voice == 'wave':
                    self._wave.note_off(note)
                elif voice in self._ch:
                    ch_voice = self._ch[voice]
                    if ch_voice.env_state not in ('idle', 'release'):
                        ch_voice.note_off()
                    # If this was pulse1 and coupling is on, release pulse2 too
                    if voice == 'pulse1' and self.coupling_mode != 'none':
                        self._couple_note_off(note)
            return

    def reset_stacks(self):
        """Clear all note stacks -- call on panic/all-notes-off.

        NOTE: this only clears the BOOKKEEPING; the ChipVoices keep sounding.
        For an actual panic use all_notes_off() below, which does both. (This
        method had no callers at all until 2026-07-30.)"""
        for stack in self._stacks.values():
            stack.clear()

    def all_notes_off(self):
        """Release every held note on every chip channel (panic).
        Clears the stacks AND silences the voices -- reset_stacks() alone
        leaves them ringing, which is why a stranded note used to survive
        everything short of a synth restart."""
        for stack in self._stacks.values():
            stack.clear()
        self._split_notes.clear()
        for v in self._ch.values():
            if v.env_state not in ('idle', 'release'):
                v.note_off()
        try:
            # silence(), NOT note_off(): note_off now RELEASES, and CC 11 can
            # make that 3 seconds long -- a panic must not wait for it.
            self._wave.silence()
        except Exception:
            pass

    def set_cc(self, voice, cc, value):
        """voice: which channel the CC arrived on -- used for duty/noise mode;
        ADSR and filter CCs are shared across all voices.
        The 'wave' voice handles CC 1 (waveform), CC 7 (volume) and the
        custom Wave RAM CCs (20/21/23) internally."""
        # -- Vibrato (chip/GB expansion): per-voice depth + rate ---------------
        # Handled BEFORE the wave early-return below, because vibrato state
        # lives here on NESEngine (per voice name) even for the wave channel.
        # CC77: depth 0-127 -> 0-100 cents (0 = vibrato off).
        # CC78: rate  0-127 -> 0.1-12 Hz.
        if cc == 77 and voice in self._vib_depth:
            self._vib_depth[voice] = value / 127.0 * 100.0
            return
        if cc == 78 and voice in self._vib_rate:
            self._vib_rate[voice] = 0.1 + value / 127.0 * 11.9
            return
        # -- Hardware sweep settings (Pulse 1 only -- authentic DMG) -----------
        # CC79: sweep time 0-127 -> 0-7 (0 = off; step every time × 7.8ms).
        # CC80: sweep shift 0-127 -> 0-7 (how big each step is: freq >> shift).
        # CC81: direction -- < 64 = up (frequency rises), >= 64 = down.
        # New settings take effect at the NEXT pulse1 note-on (each note
        # re-arms the sweep), except time = 0 which shuts it off immediately.
        if voice == 'pulse1' and cc in (79, 80, 81):
            if cc == 79:
                self._sweep_time = min(7, value * 8 // 128)
                if self._sweep_time == 0:
                    self._sweep_on   = False
                    self._sweep_mult = 1.0
            elif cc == 80:
                self._sweep_shift = min(7, value * 8 // 128)
            else:   # cc == 81
                self._sweep_up = value < 64
            return
        # -- Ring mod (2026-08-05): engine-wide, like ADSR, so it is handled
        # BEFORE the wave early-return below -- otherwise the knob would do
        # nothing whenever the chip bus happened to be on the wave channel.
        if cc in (12, 14, 15):
            if cc == 12:
                self.rm.depth = value / 127.0
            elif cc == 14:
                self.rm.ratio = _cc_to_rm_ratio(value)
            else:
                self.rm.fixed_hz = _cc_to_rm_hz(value)
            return
        # -- Per-voice performance mods (2026-09-24, see _ChipMod) -------------
        # Before the wave early-return so the wave voice gets them too.
        m = self._mod.get(voice)
        if m is not None:
            if cc == 5:
                # Glide / portamento time on this voice: 0 = off, else 5 ms-2 s.
                m.glide_t = 0.0 if value == 0 else _cc_to_time(value, 0.005, 2.0)
                return
            if cc == 86:
                # Pan: 64 = centre, 0 = hard left, 127 = hard right.
                m.pan = max(0, min(127, value))
                return
            if cc == 87:
                # Arp interval A, 0-24 semitones (0 = off). A alone = trill.
                m.arp_a = _cc_to_semis(value)
                return
            if cc == 88:
                # Arp interval B, 0-24 semitones (0 = off).
                m.arp_b = _cc_to_semis(value)
                return
            if cc == 89:
                # Arp / trill speed, 4-60 steps per second.
                m.arp_hz = 4.0 * 15.0 ** (value / 127.0)
                return
            if cc == 90:
                # Pitch slide from each note's start: 64 = none, 0/127 = -/+24 st.
                m.slide_semi = _cc_to_bipolar_semis(value)
                return
            if cc == 110:
                # Pitch slide time (to reach the full CC 90 amount), 10 ms-2 s.
                m.slide_t = _cc_to_time(value, 0.01, 2.0)
                return
            if cc == 111:
                # Tremolo depth: 0 = off, 127 = full volume dip.
                m.trem_depth = value / 127.0
                return
            if cc == 112:
                # Tremolo rate, 0.5-20 Hz.
                m.trem_hz = 0.5 * 40.0 ** (value / 127.0)
                return
            if cc == 113:
                # Retrigger: 0 = off, else 1-40 re-strikes per second while held.
                m.retrig_hz = 0.0 if value == 0 else 40.0 ** ((value - 1) / 126.0)
                return
        if voice == 'noise' and cc == 114:
            # Noise KEY SPLIT: 0 = off, else notes >= this note number play the
            # second noise voice (noise2) -- white + buzzy from one pad bank.
            self._noise_split = max(0, min(127, value))
            return
        if voice == 'wave':
            self._wave.set_cc(cc, value)
            return
        v = value / 127.0
        if cc == 1:
            # Duty cycle for pulse channels (CC1 now means duty, not voice-select)
            duty = (0.125, 0.25, 0.5, 0.75)[min(3, value // 32)]
            if voice == 'pulse1':
                self._p1_duty = duty
                # Live-apply: push straight onto the voice object too, not
                # just the engine-level default used at the NEXT note-on.
                # _osc() re-reads .duty every block, so this takes effect
                # on an already-sustaining note immediately, no extra cost.
                self._ch['pulse1'].duty = duty
            elif voice == 'pulse2':
                self._p2_duty = duty
                self._ch['pulse2'].duty = duty
        elif cc == 3:
            # Noise LFSR mode (only meaningful on the noise channel):
            # < 64 = 15-bit long/white, >= 64 = 7-bit short/metallic.
            # Each noise voice has its own mode (noise2 since 2026-09-24).
            if voice == 'noise':
                self._noise_long = value < 64
                # Live-apply to a note that's already sounding too (the read
                # position wraps safely across table lengths, see _osc_noise).
                self._ch['noise']._nseq = self._nseq_for('noise')
            elif voice == 'noise2':
                self._noise2_long = value < 64
                self._ch['noise2']._nseq = self._nseq_for('noise2')
        elif cc == 8:
            self.attack  = _cc_to_time(value, 0.001, 3.0)
            self._apply_live_adsr()
        elif cc == 9:
            self.decay   = _cc_to_time(value, 0.005, 3.0)
            self._apply_live_adsr()
        elif cc == 10:
            self.sustain = v
            self._apply_live_adsr()
        elif cc == 11:
            self.release = _cc_to_time(value, 0.005, 3.0)
            self._apply_live_adsr()
        elif cc == 13:
            self.master  = v * 0.7
        elif cc == 74:
            self._filt_set(cutoff=_cc_to_cutoff(value))
        elif cc == 71:
            self._filt_set(q=_cc_to_q(value))
        elif cc == 70:
            self._filt_set(mode=_cc_to_mode(value))
        elif cc == 75:
            self.octave = _cc_to_octave(value)
            self._apply_live_octave()
        elif cc == 76:
            # P1/P2 stereo spread toggle (see get_p1p2_stereo). Standard
            # on/off CC convention: 0-63 off, 64-127 on. Whether this also
            # turns on SynthEngine.stereo_mode is handled one level up, in
            # SynthEngine's 'chip_cc' queue dispatch -- this class doesn't
            # hold a reference back to its owning SynthEngine.
            self.p1p2_spread = value >= 64
        elif cc == 72:
            # P1+P2 dual-voice coupling mode. Fire sends this instead of
            # calling cycle_coupling() directly, because Fire and this synth
            # are two separate processes with no shared Python object -- see
            # the "NES coupling modes" bugfix writeup for the full story.
            # 0-127 maps evenly across the 7 NES_COUPLE_MODES, in order.
            idx = min(len(NES_COUPLE_MODES) - 1, value * len(NES_COUPLE_MODES) // 128)
            self.set_coupling(NES_COUPLE_MODES[idx])
        elif cc == 73:
            # Dual-voice detune amount, DETUNE coupling mode only. Centered
            # scheme (matches the tune/pitch CCs elsewhere in this file):
            # CC 64 = 0 cents, CC 0/127 = roughly -50/+50 cents.
            self.set_detune(round((value - 64) / 64.0 * 50))
        elif cc == 85:
            # Pulse anti-alias (PolyBLEP) -- OPT-IN, default off. Band-limits the
            # raw square, taming its aliasing -- notably the cyclic 'fizz' of a
            # DETUNED P1+P2 pair in certain ranges -- at a small cost to the raw
            # 8-bit grit. Engine-wide across BOTH pulse voices so one toggle
            # covers a coupled/detuned pair (the CC may arrive on either pulse
            # channel). >=64 = on. Persisted per patch (not in _CHIP_PERSIST_SKIP);
            # _osc re-reads .aa every block so it takes effect on a sustaining note.
            on = value >= 64
            self._ch['pulse1'].aa = on
            self._ch['pulse2'].aa = on

    # ══════════════════════════════════════════════════════════════════════
    # Pitch modulation (chip/GB expansion 2026-07-20)
    # ══════════════════════════════════════════════════════════════════════

    def set_pitchwheel(self, voice, pitch):
        """Store a MIDI pitchwheel value (-8192..8191) for pulse1 / pulse2 /
        wave. Noise and triangle deliberately ignore bends (spec §2.1).
        The actual pitch change happens in _update_pitch_mod each block."""
        if voice in self._bend:
            self._bend[voice] = max(-8192, min(8191, int(pitch)))

    def _sweep_rearm(self):
        """Reset + re-arm the Pulse-1 hardware sweep for a fresh note.
        Called from every pulse1 note-on (including stack-fallback
        retriggers). Converts the note's frequency into the GB 11-bit
        frequency-register value X (f = 131072 / (2048 - X)); sweep steps
        then move X exactly like DMG register writes would."""
        self._sweep_mult = 1.0
        self._sweep_acc  = 0.0
        if self._sweep_time <= 0:
            self._sweep_on = False
            return
        freq = max(1.0, self._ch['pulse1'].base_freq)
        X = 2048 - int(round(131072.0 / freq))
        X = max(0, min(2047, X))
        self._sweep_X0 = X
        self._sweep_X  = X
        self._sweep_on = True

    def _sweep_advance(self, dt):
        """Advance the Pulse-1 sweep by dt seconds and return the current
        frequency multiplier (swept_freq / note_freq). One sweep step fires
        every `time × 7.8ms` (the hardware's 128 Hz sweep clock); each step
        moves the register by X >> shift. Register frequency is
        f = 131072/(2048-X), so the multiplier is a clean period ratio:
        (2048 - X0) / (2048 - X_now). Leaving the 11-bit range silences the
        note, exactly like hardware overflow."""
        if not self._sweep_on:
            return self._sweep_mult    # holds its last value (1.0 if unused)
        self._sweep_acc += dt
        interval = self._sweep_time * 0.0078   # time × 7.8ms per step
        while self._sweep_acc >= interval:
            self._sweep_acc -= interval
            # shift = 0 means "no change" per the spec (X >> 0 would be a
            # whole-register jump, the opposite of what a beginner expects).
            # 'continue' (not 'break') so the accumulator still drains and
            # can't build up a burst of steps for later.
            if self._sweep_shift <= 0:
                continue
            delta = self._sweep_X >> self._sweep_shift
            # "Up" means the FREQUENCY rises = register value X increases
            # (hardware NR10 naming is period-centric and backwards from
            # the musical meaning; we honour the musical meaning here).
            newX = self._sweep_X + delta if self._sweep_up else self._sweep_X - delta
            if newX < 0 or newX > 2047:
                # Overflow: silence the note like the hardware does.
                # note_off() gives a clickless release instead of a hard cut.
                v = self._ch['pulse1']
                if v.env_state not in ('idle', 'release'):
                    v.note_off()
                self._sweep_on   = False
                self._sweep_mult = 1.0   # don't transpose any later unswept note
                break
            self._sweep_X = newX
            self._sweep_mult = ((2048.0 - self._sweep_X0) /
                                max(1.0, 2048.0 - self._sweep_X))
        return self._sweep_mult

    def _update_pitch_mod(self, n):
        """Once per audio block: combine manual bend × vibrato (× sweep for
        pulse1) into each voice's pitch_mult. Pure scalar math on three
        voices -- costs nothing, RT-safe. The vibrato LFO phase accumulates
        continuously here (free-running), never reset by note-ons."""
        dt = n / float(self.sr)
        sweep_mult = self._sweep_advance(dt)
        for name in self._VOICES:
            mult = 1.0
            if name in self._bend:
                # Manual pitchwheel: -8192..8191 -> ± CHIP_BEND_RANGE semitones.
                bend = self._bend[name]
                mult = 2.0 ** ((bend / 8192.0) * CHIP_BEND_RANGE / 12.0) if bend else 1.0
                # Vibrato: sine LFO in cents on top of the bend.
                depth = self._vib_depth[name]
                ph = (self._vib_phase[name] + self._vib_rate[name] * dt) % 1.0
                self._vib_phase[name] = ph
                if depth > 0.0:
                    cents = depth * np.sin(2.0 * np.pi * ph)
                    mult *= 2.0 ** (cents / 1200.0)
                if name == 'pulse1':
                    mult *= sweep_mult
            # Glide + pitch slide (2026-09-24), every voice incl. triangle/noise.
            semi = self._mod[name].pitch_semis(dt)
            if semi:
                mult *= 2.0 ** (semi / 12.0)
            self._pm[name] = mult
            # Hand the combined factor to the voice; its oscillator applies
            # it to base_freq at the top of its next render. (An arp step is
            # multiplied on per segment in _render_voice.)
            self._set_pm(name, mult)

    def _set_pm(self, name, mult):
        if name == 'wave':
            self._wave.pitch_mult = mult
        else:
            self._ch[name].pitch_mult = mult

    def _voice_raw(self, name, n):
        """One voice's plain render (None when silent) -- exactly what the mix
        used before the mods layer existed."""
        if name == 'wave':
            w = self._wave
            return w.render(n) if w._active else None
        v = self._ch[name]
        return v.render(n) if v.active() else None

    def _restrike(self, name):
        """Retrigger strike: restart the envelope from silence (the retrigger
        gate is at 0 at this instant, so the level jump is inaudible)."""
        if name == 'wave':
            self._wave.restrike()
            return
        v = self._ch[name]
        if v.note is not None:
            v.env_state = 'attack'
            v.level     = 0.0

    def _render_voice(self, name, n):
        """Render one voice with its arp / retrigger / tremolo applied.
        Nothing on -> the plain render, untouched (byte-identical)."""
        m     = self._mod[name]
        steps = m.arp_steps()
        rt_on = m.retrig_hz > 0.0
        if steps is None and not rt_on and m.trem_depth <= 0.0:
            return self._voice_raw(name, n)
        held = bool(self._stacks.get(name))
        active = self._wave._active if name == 'wave' else self._ch[name].active()
        if not active and not (rt_on and held):
            return None
        sr = float(self.sr)
        events = {}          # sample offset -> ['arp' | 'rt', ...]

        def _schedule(pos, period, tag):
            if pos >= period:
                pos = pos % period
            b = period - pos                  # > 0: next boundary, in samples
            while b < n:
                events.setdefault(int(b), []).append(tag)   # floor keeps it < n
                b += period
            return (pos + n) % period

        if steps is not None:
            m.arp_pos = _schedule(m.arp_pos, sr / m.arp_hz, 'arp')
        if rt_on:
            P = sr / m.retrig_hz
            rt0 = m.rt_pos % P
            m.rt_pos = _schedule(m.rt_pos, P, 'rt')
        out = np.zeros(n, dtype=np.float32)
        cuts = sorted({0, n} | {k for k in events if k < n})
        for i in range(len(cuts) - 1):
            s, e = cuts[i], cuts[i + 1]
            for tag in events.get(s, ()):
                if tag == 'arp':
                    m.arp_idx += 1
                elif held:
                    self._restrike(name)
            if steps is not None:
                self._set_pm(name, self._pm[name] *
                             2.0 ** (steps[m.arp_idx % len(steps)] / 12.0))
            sig = self._voice_raw(name, e - s)
            if sig is not None:
                out[s:e] = sig
        if steps is not None:
            m.arp_idx %= len(steps)
        if rt_on:
            # Gate each strike period with short fades so the restrike (which
            # restarts the envelope from 0) always lands in silence -- no click.
            edge = min(_CHIP_RETRIG_EDGE_S * sr, P / 4.0)
            t = np.mod(rt0 + np.arange(n, dtype=np.float64), P)
            gate = np.minimum(np.minimum(t / edge, (P - t) / edge), 1.0)
            out *= np.maximum(gate, 0.0).astype(np.float32)
        if m.trem_depth > 0.0:
            ph = m.trem_ph + m.trem_hz * np.arange(n, dtype=np.float64) / sr
            m.trem_ph = float((m.trem_ph + m.trem_hz * n / sr) % 1.0)
            amp = 1.0 - m.trem_depth * 0.5 * (1.0 - np.cos(2.0 * np.pi * ph))
            out *= amp.astype(np.float32)
        return out

    def _render_voices(self, n):
        """[(name, signal)] for every voice that sounded this block, in the
        original mix order (pulse1, pulse2, triangle, noise, noise2, wave, pcm)."""
        # Advance bend/vibrato/sweep/glide/slide first, so this block is
        # rendered at the already-updated pitch.
        self._update_pitch_mod(n)
        sigs = []
        for name in self._VOICES:
            s = self._render_voice(name, n)
            if s is not None:
                sigs.append((name, s))
        pcm = self._pcm.render(n)
        if pcm is not None:
            sigs.append(('pcm', pcm))
        # Stash this block's raw (pre-master/pre-filter) pulse1/pulse2-only
        # signals for the P1/P2 spread (get_p1p2_stereo / render_stereo).
        self._last_p1_solo = next((s for nm, s in sigs if nm == 'pulse1'), None)
        self._last_p2_solo = next((s for nm, s in sigs if nm == 'pulse2'), None)
        return sigs

    def _mix_mono(self, sigs, n):
        out = np.zeros(n, dtype=np.float32)
        for _, s in sigs:
            out += s
        if sigs:
            out *= _synth_fader_gain(self.master, CHIP_FADER_CEIL)
            out = self.filt.process(out)
        return out

    def render(self, n):
        """Mono mix (pans ignored). The chip host uses render_stereo()."""
        return self._mix_mono(self._render_voices(n), n)

    def render_stereo(self, n):
        """(L, R) with per-voice pan (CC 86) and the P1/P2 spread.

        While every voice is centred this is EXACTLY the old mono path, L == R
        (the balance pan law makes centre unity), and filt_R's state is kept
        equal to filt's so a pan move mid-note cannot step the right channel.
        After the last voice re-centres it stays on the stereo path for
        _CHIP_STEREO_HOLD_S so the two filter states re-converge first."""
        sigs = self._render_voices(n)
        centred = all(m.pan == 64 for m in self._mod.values())
        if centred:
            self._st_hold = max(0, self._st_hold - n)
        else:
            self._st_hold = int(_CHIP_STEREO_HOLD_S * self.sr)
        sounding = {name for name, _ in sigs}
        if centred and self._st_hold <= 0:
            for name, m in self._mod.items():
                m.gL = m.gR = 1.0
                m.live = name in sounding
            mono = self._mix_mono(sigs, n)
            self.filt_R._zi = self.filt._zi.copy()
            return self.get_p1p2_stereo(n, mono)
        L = np.zeros(n, dtype=np.float32)
        R = np.zeros(n, dtype=np.float32)
        for name, s in sigs:
            m = self._mod[name]
            tL, tR = m.pan_target()
            if not m.live:
                # Starting from silence: land on the pan position at once. A
                # ramp here would smear the note's first block across both
                # sides (and a resonant filter then rings it out on the wrong
                # side) -- there is nothing audible to smooth FROM.
                m.gL, m.gR = tL, tR
            if m.gL == tL and m.gR == tR:
                gL, gR = tL, tR
            else:
                # Ramp a pan change across the block -- a gain step clicks.
                r = np.arange(1, n + 1, dtype=np.float32) / n
                gL = m.gL + (tL - m.gL) * r
                gR = m.gR + (tR - m.gR) * r
                m.gL, m.gR = tL, tR
            L += s * gL
            R += s * gR
        for name, m in self._mod.items():
            m.live = name in sounding
            if not m.live:               # silent voice: nothing to ramp
                m.gL, m.gR = m.pan_target()
        if sigs:
            g = _synth_fader_gain(self.master, CHIP_FADER_CEIL)
            L *= g
            R *= g
            L = self.filt.process(L)
            R = self.filt_R.process(R)
        if self.p1p2_spread:
            g = _synth_fader_gain(self.master, CHIP_FADER_CEIL)
            if self._last_p1_solo is not None:
                L = L + self._last_p1_solo * g * 0.6
            if self._last_p2_solo is not None:
                R = R + self._last_p2_solo * g * 0.6
        return L, R

    def program_change(self, voice, program):
        """PC on a chip channel: the wave voice's slot recall/store. Other
        voices ignore it."""
        if voice == 'wave':
            self._wave.program_change(program)

    def get_p1p2_stereo(self, n, mono_mix):
        """Build a stereo (L, R) pair that makes pulse1 easier to hear on
        the left and pulse2 on the right -- an audibility aid for telling
        coupling modes apart (octave/unison especially are subtle in mono,
        same timbre stacked at an interval blends together). Returns
        (mono_mix, mono_mix) unchanged when p1p2_spread is off, so callers
        can call this unconditionally without an extra branch.

        Implementation note: rather than surgically removing pulse1/pulse2
        from the already-filtered mono_mix (which would mean running the
        shared, stateful self.filt biquad a second time per block on a
        different signal, and risk corrupting its internal state), this
        ADDS extra hard-panned copies of the raw pre-filter pulse1/pulse2
        signal on top of the normal, unmodified, fully-mixed mono_mix.
        Pulse1/pulse2 are still present in both channels (as part of the
        ordinary centred mix) but boosted toward their assigned side --
        that's enough separation for the ear to latch onto without ever
        touching the filter's state. `mono_mix` must be this same block's
        render() return value (i.e. call this right after render(), same n).
        """
        if not self.p1p2_spread:
            return mono_mix, mono_mix
        z  = np.zeros(n, dtype=np.float32)
        # Same taper as the main chip mix (above), so the spread copies stay
        # proportional to mono_mix instead of drifting to a different level.
        g  = _synth_fader_gain(self.master, CHIP_FADER_CEIL)
        p1 = (self._last_p1_solo if self._last_p1_solo is not None else z) * g
        p2 = (self._last_p2_solo if self._last_p2_solo is not None else z) * g
        boost = 0.6   # extra energy added to the "home" side only
        L = mono_mix + p1 * boost
        R = mono_mix + p2 * boost
        return L, R


class SampleVoice(DrumVoice):
    """Plays a WAV file. Tune shifts playback rate (+-2 oct). HP post-filter
    via base class. Level/attack/decay via base-class envelope."""
    def __init__(self, path=None):
        super().__init__("sample")
        self._data = np.zeros(1, dtype=np.float32)
        self._pos  = 0.0
        self._rate = 1.0
        self._loaded_path = None
        self._lp_cutoff = 18000.0   # glide target/actual, mirrors lp_filt's cutoff
        self._lp_res    = 0.0       # glide target/actual, mirrors lp_filt's resonance
        self.lp_filt = StateVariableFilter(self._lp_cutoff, resonance=self._lp_res, mode="LP")
        self.lp_filt._bypass = True   # no-op until user dials filter_freq/filter_res
        if path:
            self.load(path)

    def load(self, path):
        try:
            from orc1_wavio import read_wav as _read_wav, resample as _rs
            sr, data = _read_wav(path)             # no scipy (orc1_wavio.py)
            if data.ndim > 1:
                data = data.mean(axis=1)
            data = data.astype(np.float32)
            if np.abs(data).max() > 1.0:
                data /= 32768.0
            if sr != SAMPLE_RATE:
                # Drum samples are meant to be 16-bit / 48 kHz on disk (owner,
                # 2026-09-23) -- say so loudly instead of silently resampling.
                print(f"[synth] WARNING {os.path.basename(path)} is {sr} Hz, not "
                      f"{SAMPLE_RATE}: resampled at load -- convert the file", flush=True)
                data = _rs(data, int(len(data) * SAMPLE_RATE / sr)).astype(np.float32)
            peak = np.abs(data).max()
            if peak > 0:
                data = data / peak * 0.9
            self._data = data
            self.name  = os.path.splitext(os.path.basename(path))[0]
            self.decay_time = len(data) / SAMPLE_RATE + 0.05
            self._loaded_path = path
            print(f"[synth] sample: {self.name} ({len(data)/SAMPLE_RATE:.2f}s)")
        except Exception as e:
            print(f"[synth] sample load failed {path}: {e}")
            self._data = np.zeros(1, dtype=np.float32)
            self.name  = "error"

    def _on_trigger(self):
        self._pos = 0.0

    def set_param(self, param, cc_value):
        super().set_param(param, cc_value)
        if param == "tune":
            self._glide_to('_rate', 2.0 ** ((cc_value - 64) / 64.0 * 24 / 12.0))
        elif param == "decay":
            # 2026-07-28: ceiling raised 6s -> 20s, same reasoning as the base
            # DrumVoice decay above -- 5s now lands at ~77% of the dial
            # instead of ~96%, leaving real headroom for a long wash/ring-out
            # instead of hitting the wall right after the musical range.
            # Glide target, not decay_time directly -- see base __init__.
            self._glide_to('decay_time', _cc_to_time(cc_value, 0.05, 20.0))
        elif param == "filter_freq":
            self.lp_filt._bypass = False
            self._glide_to('_lp_cutoff', 80 + cc_value / 127.0 * 17920)
        elif param == "filter_res":
            self.lp_filt._bypass = False
            self._glide_to('_lp_res', cc_value / 127.0 * 0.95)

    def _render_signal(self, n_frames, env):
        # Re-apply the (possibly still-gliding) cutoff/resonance every block.
        self.lp_filt.set(cutoff_hz=self._lp_cutoff, resonance=self._lp_res)
        data = self._data
        n = len(data)
        if n < 2:
            self.active = False
            return np.zeros(n_frames, dtype=np.float32)
        positions = self._pos + np.arange(n_frames, dtype=np.float64) * self._rate
        n_valid = int((positions < (n - 1)).sum())
        out = np.zeros(n_frames, dtype=np.float32)
        if n_valid > 0:
            p  = positions[:n_valid]
            i0 = p.astype(np.int64)
            fr = (p - i0).astype(np.float32)
            out[:n_valid] = data[i0] * (1.0 - fr) + data[i0 + 1] * fr
        if n_valid < n_frames:
            self.active = False
        self._pos = float(positions[n_valid - 1] + self._rate) if n_valid else float(n)
        out = self.lp_filt.process(out)
        return out * env


# -- Cymbal --------------------------------------------------------------------
# The real TR-909 crash is a sample, not a synthesised voice.  This slot loads
# ~/samples/cymbal.wav on startup.  Rename your file to match, or change the
# constant below to match your file.  If the file is missing the slot stays
# silent but all CC controls (level, tune, decay, filter) still wire up cleanly.

CYMBAL_SAMPLE = os.path.join(SAMPLES_DIR, "cymbal.wav")
RIDE_SAMPLE   = os.path.join(SAMPLES_DIR, "ride.wav")
RIDE_BELL_SAMPLE = os.path.join(SAMPLES_DIR, "ride_bell.wav")
SPLASH_SAMPLE    = os.path.join(SAMPLES_DIR, "splash.wav")
# The samples that ARE drum-kit voices -- never picked up as one-shot slots.
KIT_SAMPLE_FILES = ("cymbal.wav", "ride.wav", "ride_bell.wav", "splash.wav")


class Cymbal(SampleVoice):
    """
    909 crash cymbal: sample playback with standard DrumVoice controls.

    CC map (offset 54, CCs 74-79 on CH_DRUM):
      level       -- overall level
      tune        -- playback pitch / speed
      attack      -- envelope attack time
      decay       -- how long the crash rings out before fading
      filter_freq -- low-pass cutoff (bypassed wide-open by default)
      filter_res  -- low-pass resonance
    """
    def __init__(self):
        super().__init__(CYMBAL_SAMPLE)
        self.name = "cymbal"   # keep voice name clean regardless of filename

    def set_param(self, param, cc_value):
        # NO low-pass on the cymbal. Two measured reasons (2026-08-17):
        #   1. cymbal.wav carries no energy above ~8 kHz, so an LP only ever
        #      dulls it -- there is nothing up there to shape.
        #   2. The SampleVoice SVF (Chamberlin topology) goes UNSTABLE at high
        #      cutoffs. A saved filter_freq=127 (cutoff 18 kHz) turned the crash
        #      into a sub-bass blob (centroid 14 Hz, peak ~5.0, clipping) and
        #      was re-applied on every synth restart. Dropping filter_freq/
        #      filter_res here keeps lp_filt permanently bypassed; level/tune/
        #      attack/decay still behave exactly as on any other sample voice.
        if param in ("filter_freq", "filter_res"):
            return
        super().set_param(param, cc_value)


class Ride(Cymbal):
    """
    909 RIDE (2026-09-23) -- the 909's 11th voice, which the ORC lacked (ride
    notes used to fold onto the crash). Like the real 909's cymbals it is a
    sample: samples/ride.wav, a 21" Bosphorus ride (48 kHz, from the Standard
    BeatBuddy kit's vel 69-87 layer). Same controls + the same no-low-pass rule
    as the crash. CC map (cfg.DRUM_CC): level/tune/attack/decay on CCs 103-106,
    appended after every existing voice so nothing else moved.
    """
    def __init__(self):
        SampleVoice.__init__(self, RIDE_SAMPLE)
        self.name = "ride"


class RideBell(Cymbal):
    """Ride BELL (2026-09-23): the same 21" Bosphorus ride struck on the bell
    (Standard kit vel 53-77 layer). GM note 53. CCs 107-110. Crash rules (no LP)."""
    def __init__(self):
        SampleVoice.__init__(self, RIDE_BELL_SAMPLE)
        self.name = "ride_bell"


class Splash(Cymbal):
    """SPLASH (2026-09-23): a Bosphorus splash (Standard kit main layer). GM
    note 55 (was silent). CCs 111-114. Crash rules (no LP)."""
    def __init__(self):
        SampleVoice.__init__(self, SPLASH_SAMPLE)
        self.name = "splash"


# -- Per-voice drum CC persistence (2026-07-28) --------------------------------
# Dialed-in voice params (level/tune/attack/decay/filter_*) used to live only
# in the DrumVoice instance's memory, so every synth restart reset every
# voice back to its hardcoded default (e.g. the 909 kick's stock 0.5s decay).
# Saved here as {voice_name: {param: cc_value}} and re-applied at startup.
DRUM_PARAMS_FILE = os.path.expanduser("~/.orc1_drum_params.json")


def _load_drum_params():
    """Missing/corrupt file -> empty dict; voices just keep their code
    defaults, exactly like before this feature existed."""
    try:
        import json as _json
        with open(DRUM_PARAMS_FILE) as f:
            data = _json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


# -- Bus level persistence (2026-07-29, plan item P11) -------------------------
# The four bus masters (engine.master / poly.master / chip.master / drum_master)
# used to live only in memory, so EVERY synth restart -- a card switch, a
# `restart orc1-jackd` cascading through BindsTo, an orc1 restart via PartOf, a
# crash-restart -- silently threw away the owner's gain staging and put the drum
# bus back at its boot value. Per-voice drum params already persisted (above),
# which made it worse: lower the drum bus, restart, and the voice levels are
# exactly where you left them while the bus alone has jumped back up.
#
# Stored as raw CC values (0-127), the same units every front-end speaks (Fire
# faders, web LEVELS sliders, SYNTH_CC), so a restored level is bit-identical to
# what the CC path would have produced -- what you saved is what you get.
BUS_LEVELS_FILE = os.path.expanduser("~/.orc1_bus_levels.json")

# key -> (owner attribute path). "poly"/"chip" hang off sub-engines. "drum" was
# removed 2026-09-02 (the drum host owns the "drum" key of the SHARED bus-levels
# file now, writing it with a read-modify-write merge; this synth writes only
# master/poly/chip and never touches "drum").
_BUS_KEYS = ("master", "poly", "chip")


def _load_bus_levels():
    """Missing/corrupt file -> empty dict; every bus keeps its code/config boot
    gain, exactly like before this feature existed. Unknown keys and
    out-of-range values are dropped rather than trusted -- a hand-edited file
    must never be able to hand the RT path a bogus gain."""
    try:
        import json as _json
        with open(BUS_LEVELS_FILE) as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k in _BUS_KEYS:
            v = data.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if 0 <= v <= 127:
                out[k] = int(v)
        return out
    except Exception:
        return {}


# -- Poly / chip patch persistence (auto-remember-last, like drums) ------------
# Drum voice params already survive a restart (above); poly and chip synth
# patches did NOT -- only their bus master was persisted, so every restart threw
# away waveform, ADSR, ring mod, filter, etc.  That asymmetry is what made
# persistence "feel inconsistent": drum tweaks stuck, synth tweaks evaporated.
# These two files close the gap, using the SAME debounced persist thread and the
# SAME read-modify-write merge discipline as the drum/bus code.  set_cc is a
# pure, idempotent CC->state map on both engines, so replaying the saved values
# at boot reproduces the patch exactly.
POLY_PARAMS_FILE = os.path.expanduser("~/.orc1_poly_params.json")
CHIP_PARAMS_FILE = os.path.expanduser("~/.orc1_chip_params.json")
MONO_PARAMS_FILE = os.path.expanduser("~/.orc1_mono_params.json")  # mono host

# CC13 (_ENGINE_MASTER_CC) is deliberately EXCLUDED from both files: the
# bus-levels system (~/.orc1_bus_levels.json) is the single source of truth for
# every master, and giving master two owners that restore in different orders is
# exactly the kind of double-write bug CLAUDE.md keeps warning about.
_POLY_PERSIST_SKIP = {_ENGINE_MASTER_CC}
# Chip additionally excludes the P1/P2 coupling CCs (72 mode / 73 detune /
# 76 spread).  The module-matrix "join" is EPHEMERAL by design (session 15):
# the synth's coupling_mode does not persist, so a restored coupling would boot
# chip_p2 expecting a join the matrix now shows as 'none'.  Persisting these
# would silently reintroduce that documented trap.
_CHIP_PERSIST_SKIP = {_ENGINE_MASTER_CC, 72, 73, 76}
# 2026-09-24: the GB wave's TABLE state lives in its own file
# (~/.orc1_gb_wave.json, see _GB_HARM_CC0) -- the custom-RAM draw CCs 20/21, the
# custom on/off 23, the harmonic knobs 102-109 and the momentary STORE arm 115
# are not replayed from the per-CC patch file (replaying the LAST CC20/21 pair
# rebuilt nothing useful, and replaying the knobs would overwrite a recalled slot).
_CHIP_PERSIST_SKIP |= {20, 21, 23, 115} | set(range(_GB_HARM_CC0, _GB_HARM_CC0 + _GB_HARM_N))
GB_WAVE_STATE_FILE = "~/.orc1_gb_wave.json"
# Mono skips only CC13 master (bus-levels owns every master). Everything else --
# waveform, ADSR, portamento, ring mod, filter, and the per-layer semitone/detune/
# wave/gain -- is remembered, like poly.
_MONO_PERSIST_SKIP = {_ENGINE_MASTER_CC}


def _load_mono_params():
    """Missing/corrupt -> {}; mono keeps its code defaults. {str(cc): value}."""
    try:
        import json as _json
        with open(MONO_PARAMS_FILE) as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _flush_mono_params(engine):
    """Drain the mono patch pending dict to disk (read-modify-write merge, atomic
    replace) -- same shape as _flush_poly_params."""
    pending = engine._mono_param_pending
    engine._mono_param_pending = {}
    engine._mono_params_dirty  = False
    if not pending:
        return
    try:
        import json as _json
        data = _load_mono_params()
        for cc, val in pending.items():
            data[str(cc)] = int(val)
        tmp = MONO_PARAMS_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, MONO_PARAMS_FILE)
    except Exception as e:
        print(f"[synth] mono param save failed: {e}", flush=True)


def _load_poly_params():
    """Missing/corrupt -> {}; poly keeps its code defaults, exactly like before
    this feature existed. {str(cc): value}."""
    try:
        import json as _json
        with open(POLY_PARAMS_FILE) as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_chip_params():
    """Missing/corrupt -> {}. {voice: {str(cc): value}} -- chip CCs are
    per-voice (duty/vibrato/sweep) as well as engine-wide (ADSR/ring mod), so
    the store is keyed by the voice the CC arrived on."""
    try:
        import json as _json
        with open(CHIP_PARAMS_FILE) as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bus_level_cc(engine, key):
    """Current level of one bus as a 0-127 CC value."""
    if   key == "master": g = engine.master
    elif key == "poly":   g = engine.poly.master
    elif key == "chip":   g = engine.chip.master
    else:                 return None
    return max(0, min(127, int(round(float(g) * 127.0))))


def _flush_drum_params(engine):
    pending = engine._drum_param_pending
    engine._drum_param_pending = {}
    engine._drum_params_dirty  = False
    if not pending:
        return
    try:
        import json as _json
        data = _load_drum_params()
        for name, params in pending.items():
            data.setdefault(name, {}).update(params)
        tmp = DRUM_PARAMS_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, DRUM_PARAMS_FILE)
    except Exception as e:
        print(f"[synth] drum param save failed: {e}", flush=True)


def _bus_level_write(updates):
    """Cross-process-safe read-modify-write of the SHARED bus-levels file.

    The audio host split made this file MULTI-WRITER: the poly + chip hosts (via
    _flush_bus_levels below), the drum host and the mono host all RMW it on their
    ~1.5s persist tick. A bare RMW can interleave -- A reads, B reads the same
    snapshot, B writes its key, A writes back the STALE snapshot and clobbers B's
    fresh value (and the shared .tmp name is itself a race). An advisory flock on
    a sidecar serialises the whole read->merge->os.replace so every writer sees
    the others' committed keys. Merges against the RAW file (not the
    _BUS_KEYS-filtered view) so a key a future component adds is never dropped.
    `updates` = {key: 0-127}. Best-effort: any failure just skips the write.

    This is the single writer path -- the drum/mono host helpers call it too, so
    the merge/lock discipline lives in one place (was three near-identical RMWs)."""
    if not updates:
        return
    try:
        import json as _json
        lockf = None
        try:
            import fcntl
            lockf = open(BUS_LEVELS_FILE + ".lock", "w")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        except Exception:
            lockf = None            # no fcntl (non-Linux dev) -> unlocked best-effort
        try:
            try:
                with open(BUS_LEVELS_FILE) as f:
                    data = _json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data.update(updates)
            tmp = BUS_LEVELS_FILE + ".tmp"
            with open(tmp, "w") as f:
                _json.dump(data, f, indent=2)
            os.replace(tmp, BUS_LEVELS_FILE)
        finally:
            if lockf is not None:
                lockf.close()       # releases the flock
    except Exception as e:
        print(f"[synth] bus level save failed: {e}", flush=True)


def _flush_bus_levels(engine):
    """Snapshot the buses touched this session and write them out.

    Deliberately reads the levels STRAIGHT off the engine at flush time rather
    than draining a pending queue: the value on the engine is by definition the
    truth, so no CC can ever be lost in a swap race, and a fader sweep costs one
    write per poll instead of one per tick. Only buses actually moved this
    session are written, so an untouched bus keeps following its config/code
    boot gain instead of being frozen by a stale file."""
    engine._bus_levels_dirty = False
    touched = tuple(engine._bus_touched)
    if not touched:
        return
    updates = {}
    for key in touched:
        cc = _bus_level_cc(engine, key)
        if cc is not None:
            updates[key] = cc
    _bus_level_write(updates)   # locked, cross-process-safe (see _bus_level_write)


def _flush_poly_params(engine):
    """Drain the poly patch pending dict to disk. Same read-modify-write merge
    as _flush_drum_params: load the raw file, overlay this session's moved CCs,
    atomic-replace. JSON keys are strings; the loader int()s them back."""
    pending = engine._poly_param_pending
    engine._poly_param_pending = {}
    engine._poly_params_dirty  = False
    if not pending:
        return
    try:
        import json as _json
        data = _load_poly_params()
        for cc, val in pending.items():
            data[str(cc)] = int(val)
        tmp = POLY_PARAMS_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, POLY_PARAMS_FILE)
    except Exception as e:
        print(f"[synth] poly param save failed: {e}", flush=True)


def _flush_chip_params(engine):
    """As _flush_poly_params, but the store is nested by voice."""
    pending = engine._chip_param_pending
    engine._chip_param_pending = {}
    engine._chip_params_dirty  = False
    if not pending:
        return
    try:
        import json as _json
        data = _load_chip_params()
        for voice, ccs in pending.items():
            slot = data.setdefault(voice, {})
            if not isinstance(slot, dict):
                slot = {}
                data[voice] = slot
            for cc, val in ccs.items():
                slot[str(cc)] = int(val)
        tmp = CHIP_PARAMS_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, CHIP_PARAMS_FILE)
    except Exception as e:
        print(f"[synth] chip param save failed: {e}", flush=True)


# (The retired orc1-synth process -- SynthEngine, its MIDI thread and
#  _route_midi, the persist thread -- was removed 2026-09-24. Each module
#  now runs its own host: orc1_{drum,poly,chip,mono,sampler,bbkit}_host.py.)


# -- Sampler control (CH_SAMPLER) ---------------------------------------------
# Note/CC vocabulary is an internal engine<->synth contract; front-ends never
# touch it (they call orc1.py's sampler_* facade). Deck is 'A' / 'B'.
# Vocabulary comes from cfg.SAMPLER (single source of truth — the orc1.py
# facade encodes the same entries). Inverted here: {note/cc: deck}.
def _smp_inv(key, default):
    return {v: k for k, v in _SAMPLER_CFG.get(key, default).items()}

_SMP_CAP   = _smp_inv("NOTE_CAPTURE", {"A": 1,  "B": 2})   # vel = capture secs
_SMP_GATE  = _smp_inv("NOTE_GATE",    {"A": 3,  "B": 4})   # on = play, off = stop
_SMP_LOOP  = _smp_inv("NOTE_LOOP_TOGGLE", {"A": 5, "B": 6})  # latch play/stop
_SMP_AUDITION = _smp_inv("NOTE_AUDITION", {"A": 23, "B": 24})  # one-shot play-through (web PLAY)
_SMP_REVERSE = _smp_inv("NOTE_REVERSE", {"A": 21, "B": 22})  # latch backwards
_SMP_EXPORT = _smp_inv("NOTE_EXPORT", {"A": 7, "B": 8})      # save capture -> samples/
_SMP_SCRUB = _smp_inv("CC_SCRUB",     {"A": 16, "B": 17})  # offset-binary rel (jog)
_SMP_SPEED = _smp_inv("CC_SPEED",     {"A": 18, "B": 19})  # CC 0-127 -> 0.25x..4x
_SMP_GAIN  = _smp_inv("CC_GAIN",      {"A": 20, "B": 21})  # CC 0-127 -> 0..1.5
_SMP_SPEED_OCT = float(_SAMPLER_CFG.get("SPEED_RANGE_OCTAVES", 1.0))
_SMP_XF    = int(_SAMPLER_CFG.get("CC_CROSSFADE", 22))     # CC 0-127 -> xfade 0..1
# Fine-grained capture length (bar-accurate loops): a 14-bit value in
# DECIseconds, split over two CCs. MSB arrives first and is held pending;
# the LSB completes the value AND triggers the capture. This keeps the
# 'velocity = whole seconds' path fully intact for plain captures.
_SMP_CAP_MSB = _smp_inv("CC_CAP_LEN_MSB", {"A": 23, "B": 24})
_SMP_CAP_LSB = _smp_inv("CC_CAP_LEN_LSB", {"A": 25, "B": 26})
_smp_pending_msb = {}                                      # deck -> 0..127
# 3-band EQ + sweep filter (Mixtrack EQ_HI/EQ_MID/EQ_LOW/FILTER knobs).
# Raw CC 0-127, 64 = flat/bypass on all four -- decoded to +/-15dB (EQ) or
# a -1..+1 LP<->HP sweep position (filter) right below, then handed to the
# deck's set_eq/set_filter (orc1_sampler.py owns the actual DSP).
_SMP_EQ_HI  = _smp_inv("CC_EQ_HI",  {"A": 27, "B": 28})
_SMP_EQ_MID = _smp_inv("CC_EQ_MID", {"A": 29, "B": 30})
_SMP_EQ_LOW = _smp_inv("CC_EQ_LOW", {"A": 31, "B": 32})
_SMP_FILTER = _smp_inv("CC_FILTER", {"A": 33, "B": 34})
_SMP_START = _smp_inv("CC_START", {"A": 80, "B": 81})  # region start: CC 0-127 -> fraction
_SMP_END   = _smp_inv("CC_END",   {"A": 82, "B": 83})  # region end:   CC 0-127 -> fraction
_SMP_STOP    = _smp_inv("NOTE_STOP",    {"A": 9,  "B": 10})  # hard stop + rewind
_SMP_CUE     = _smp_inv("NOTE_CUE",     {"A": 11, "B": 12})  # vel 1..4 = hotcue
_SMP_SCRATCH = _smp_inv("NOTE_SCRATCH", {"A": 13, "B": 14})  # on/off = vinyl mode
_SMP_MASTER_GAIN = int(_SAMPLER_CFG.get("CC_MASTER_GAIN", 35))  # CC 0-127 -> 0..1.5x
_SMP_PRESS_LAT   = int(_SAMPLER_CFG.get("CC_PRESS_LATENCY", 84))  # 0..127 -> -127..0 ms
_SMP_ONSET       = int(_SAMPLER_CFG.get("CC_ONSET_SNAP", 95))    # >=64 on, <64 off
_SMP_DECK_SRC    = _smp_inv("CC_DECK_SOURCE", {"A": 96, "B": 97}) # value = skipback track id (0-63)
# Frame-exact capture + window nudge (2026-07-07 capture-timing overhaul).
# Two 28-bit values over four CCs each, big-endian: FRA = loop length in
# FRAMES (the decisecond pair above rounds to 100ms, which drifts against
# the sequencer on every pass); OFF = window end offset in MILLISECONDS
# before now (the quantizer's "time since the target barline"), held
# pending. FRA's B0 (LSB) fires the capture and consumes any pending OFF.
# CC_NUDGE: relative offset-binary, 1 tick = 10ms of capture-window shift.
_SMP_CAP28 = {}
for _kind, _base_a in (("FRA", 36), ("OFF", 44)):
    for _b in (3, 2, 1, 0):
        _m = _SAMPLER_CFG.get(f"CC_CAP_{_kind}_B{_b}",
                              {"A": _base_a + (3 - _b),
                               "B": _base_a + 4 + (3 - _b)})
        for _deck, _cc in _m.items():
            _SMP_CAP28[int(_cc)] = (_deck, _kind, _b)
_smp_pend28 = {}                               # (deck, kind) -> {byte_i: val}
_SMP_NUDGE  = _smp_inv("CC_NUDGE", {"A": 52, "B": 53})
# Momentary BEAT-REPEAT: a 28-bit loop LENGTH in frames over four CCs
# big-endian (held pending, does NOT fire) + a note that starts/stops the
# stutter. On note-on we read the pending length and squeeze the region;
# note-off restores it. Mirrors the CAP28 accumulate pattern.
_SMP_BR      = _smp_inv("NOTE_BEAT_REPEAT", {"A": 25, "B": 26})
_SMP_BR_LEN  = {}                              # cc -> (deck, byte_i)
for _b in (3, 2, 1, 0):
    _m = _SAMPLER_CFG.get(f"CC_BR_LEN_B{_b}",
                          {"A": 85 + (3 - _b), "B": 89 + (3 - _b)})
    for _deck, _cc in _m.items():
        _SMP_BR_LEN[int(_cc)] = (_deck, _b)
_smp_br_pend = {}                              # deck -> {byte_i: val}
# File decks: paths can't travel over MIDI, so the brain writes the target
# path to a per-deck JSON file and fires NOTE_LOAD_FILE; we read it here.
_SMP_LOAD = _smp_inv("NOTE_LOAD_FILE", {"A": 15, "B": 16})
# Top-section controls (2026-07-07): needle-drop seek + beatmatch bend.
_SMP_SEEK    = _smp_inv("CC_SEEK", {"A": 54, "B": 55})
_SMP_BENDUP  = _smp_inv("NOTE_BEND_UP", {"A": 17, "B": 18})
_SMP_BENDDN  = _smp_inv("NOTE_BEND_DN", {"A": 19, "B": 20})
_SMP_BEND_F  = float(_SAMPLER_CFG.get("BEND_FACTOR", 1.04))
_SMP_LOAD_PATH = _SAMPLER_CFG.get(
    "FILE_LOAD_PATH", os.path.expanduser("~/.orc1_sampler_load_{deck}.json"))

# One-shot sample bank: MOVED BACK to CH_DRUM 2026-09-02 (audio host split).
# The smpl_0..N SampleVoice slots are drum voices again -- triggered on CH_DRUM
# at DRUM_NOTE_MAP notes 60..63 and sculpted via the drum CC map (cfg.DRUM_CC
# VOICE_PARAMS appends smpl_0..N after cowbell). Nothing sampler-side handles
# them any more; the sampler channel is purely the live-remix decks (this file's
# _route_sampler, driven from orc1_sampler_host). See the 2026-07-21 relocation
# note in cfg.DRUM_CC for the history this reverses.


def _smp_load_file(smp, deck):
    try:
        import json as _json
        with open(_SMP_LOAD_PATH.format(deck=deck)) as f:
            path = _json.load(f).get("path", "")
        if path:
            smp.load_file(deck, path)
    except Exception as e:
        print(f"[sampler] load trigger for {deck} failed: {e}", flush=True)


def _smp_reset_tone(smp, deck):
    """Reset EQ and filter to bypass on a fresh capture or file load.
    Prevents stale controller state (e.g. from a prior Mixtrack session or a
    previous web-slider position) from attenuating a newly grabbed deck.
    NOT called on nudge (swap_buffer) — nudge intentionally keeps play state."""
    smp.control(deck, "set_eq",     "hi",  0.0)   # 0 dB = flat
    smp.control(deck, "set_eq",     "mid", 0.0)
    smp.control(deck, "set_eq",     "low", 0.0)
    smp.control(deck, "set_filter", 0.0)           # 0.0 = center/bypass


def _route_sampler(engine, msg):
    smp = getattr(engine, "sampler", None)
    if smp is None:
        return
    if msg.type == 'note_on' and msg.velocity > 0:
        if msg.note in _SMP_CAP:
            deck = _SMP_CAP[msg.note]
            smp.capture(deck, msg.velocity)                  # velocity = seconds
            _smp_reset_tone(smp, deck)                       # Bug 3: clear stale EQ/filter
        elif msg.note in _SMP_GATE:
            smp.control(_SMP_GATE[msg.note], "gate", True)
        elif msg.note in _SMP_LOOP:
            smp.control(_SMP_LOOP[msg.note], "gate_toggle")
        elif msg.note in _SMP_AUDITION:
            smp.control(_SMP_AUDITION[msg.note], "audition")
        elif msg.note in _SMP_REVERSE:
            smp.control(_SMP_REVERSE[msg.note], "toggle_reverse")
        elif msg.note in _SMP_EXPORT:
            smp.export(_SMP_EXPORT[msg.note], SAMPLES_DIR)
        elif msg.note in _SMP_STOP:
            smp.control(_SMP_STOP[msg.note], "stop")
        elif msg.note in _SMP_CUE:
            # SETTABLE hotcues, op carried in velocity: 1..4 = set-or-jump cue
            # idx 0..3 (deck decides which), 5..8 = clear that cue.
            deck = _SMP_CUE[msg.note]
            v = msg.velocity
            if 1 <= v <= 4:
                smp.control(deck, "cue", v - 1)
                smp.refresh_view(deck)                # echo cue state to the page
            elif 5 <= v <= 8:
                smp.control(deck, "clear_cue", v - 5)
                smp.refresh_view(deck)
        elif msg.note in _SMP_SCRATCH:
            smp.control(_SMP_SCRATCH[msg.note], "set_scratch", True)
        elif msg.note in _SMP_BR:
            # Momentary beat-repeat START: read the pending 28-bit length and
            # squeeze the region to a tight loop at the live playhead.
            deck = _SMP_BR[msg.note]
            pend = _smp_br_pend.get(deck, {})
            frames = sum(pend.get(i, 0) << (7 * i) for i in range(4))
            if frames >= 2:
                smp.control(deck, "beat_repeat_on", frames)
        elif msg.note in _SMP_LOAD:
            deck = _SMP_LOAD[msg.note]
            _smp_load_file(smp, deck)
            _smp_reset_tone(smp, deck)                       # Bug 3: clear stale EQ/filter
        elif msg.note in _SMP_BENDUP:
            smp.control(_SMP_BENDUP[msg.note], "pitch_bend", True, True, _SMP_BEND_F)
        elif msg.note in _SMP_BENDDN:
            smp.control(_SMP_BENDDN[msg.note], "pitch_bend", True, False, _SMP_BEND_F)
    elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
        if msg.note in _SMP_GATE:
            smp.control(_SMP_GATE[msg.note], "gate", False)
        elif msg.note in _SMP_BR:
            # Momentary beat-repeat RELEASE: restore the prior play/loop region.
            smp.control(_SMP_BR[msg.note], "beat_repeat_off")
        elif msg.note in _SMP_SCRATCH:
            smp.control(_SMP_SCRATCH[msg.note], "set_scratch", False)
        elif msg.note in _SMP_BENDUP:
            smp.control(_SMP_BENDUP[msg.note], "pitch_bend", False, True)
        elif msg.note in _SMP_BENDDN:
            smp.control(_SMP_BENDDN[msg.note], "pitch_bend", False, False)
    elif msg.type == 'control_change':
        c, v = msg.control, msg.value
        if c in _SMP_SCRUB:
            delta = v if v < 64 else v - 128          # offset-binary relative
            smp.control(_SMP_SCRUB[c], "scrub", delta * 64.0)   # frames/tick
        elif c in _SMP_SPEED:
            # Pitch fader: CENTER (64) = exactly 1.0x, log-symmetric range of
            # +/-SPEED_RANGE_OCTAVES (default 1 => 0.5x..2x at the ends).
            # The old curve (0.25*4^(v/127)) put 1.0x at the fader TOP and
            # ~0.5x at center — "normal" lived at the end of the throw.
            ratio = 2.0 ** ((v - 64) / 63.5 * _SMP_SPEED_OCT)
            smp.control(_SMP_SPEED[c], "set_speed", ratio)
        elif c in _SMP_GAIN:
            smp.control(_SMP_GAIN[c], "set_gain", v / 127.0 * 1.5)
        elif c == _SMP_XF:
            smp.set_crossfade(v / 127.0)
        elif c == _SMP_MASTER_GAIN:
            smp.set_master_gain(v / 127.0 * 1.5)      # 0..1.5x, unity near 85
        elif c == _SMP_PRESS_LAT:
            # Live press-anchored capture trim: CC 0..127 -> -127..0 ms.
            smp.set_press_latency(v - 127)
        elif c == _SMP_ONSET:
            # Onset-snap loop-start refinement: >=64 on, <64 off.
            smp.set_onset_snap(v >= 64)
        elif c in _SMP_DECK_SRC:
            # Per-deck GRAB source: point this deck at skipback track `v` (0-63).
            smp.set_deck_source(_SMP_DECK_SRC[c], v)
        elif c in _SMP_CAP_MSB:
            _smp_pending_msb[_SMP_CAP_MSB[c]] = v
        elif c in _SMP_CAP_LSB:
            deck = _SMP_CAP_LSB[c]
            msb  = _smp_pending_msb.pop(deck, 0)
            ds   = (msb << 7) | v                     # deciseconds, 14-bit
            if ds > 0:
                smp.capture(deck, ds / 10.0)          # float seconds
                _smp_reset_tone(smp, deck)            # Bug 3: clear stale EQ/filter
        elif c in _SMP_CAP28:
            deck, kind, b = _SMP_CAP28[c]
            _smp_pend28.setdefault((deck, kind), {})[b] = v
            if kind == "FRA" and b == 0:              # length LSB fires
                fra = _smp_pend28.pop((deck, "FRA"), {})
                off = _smp_pend28.pop((deck, "OFF"), {})
                frames = sum(fra.get(i, 0) << (7 * i) for i in range(4))
                off_ms = sum(off.get(i, 0) << (7 * i) for i in range(4))
                if frames > 0:
                    smp.capture_frames(deck, frames, off_ms)
                    _smp_reset_tone(smp, deck)        # Bug 3: clear stale EQ/filter
        elif c in _SMP_BR_LEN:
            # Beat-repeat loop length: accumulate a 28-bit frame count; held
            # pending (unlike CAP28's FRA, the LSB does not fire -- the BR
            # note starts it, so length always arrives first).
            deck, b = _SMP_BR_LEN[c]
            _smp_br_pend.setdefault(deck, {})[b] = v
        elif c in _SMP_NUDGE:
            delta = v if v < 64 else v - 128          # offset-binary relative
            smp.nudge(_SMP_NUDGE[c], delta * 10.0)    # ticks of 10ms
        elif c in _SMP_SEEK:
            # needle-drop: absolute strip position -> fraction of the buffer
            smp.control(_SMP_SEEK[c], "jump", v / 127.0)
        elif c in _SMP_EQ_HI:
            smp.control(_SMP_EQ_HI[c], "set_eq", "hi", (v - 64) / 63.0 * 15.0)
        elif c in _SMP_EQ_MID:
            smp.control(_SMP_EQ_MID[c], "set_eq", "mid", (v - 64) / 63.0 * 15.0)
        elif c in _SMP_EQ_LOW:
            smp.control(_SMP_EQ_LOW[c], "set_eq", "low", (v - 64) / 63.0 * 15.0)
        elif c in _SMP_FILTER:
            smp.control(_SMP_FILTER[c], "set_filter", (v - 64) / 63.0)
        elif c in _SMP_START:
            # region START edge: absolute 0..127 -> fraction of the buffer
            smp.control(_SMP_START[c], "set_bounds_start", v / 127.0)
            smp.refresh_view(_SMP_START[c])        # echo the new markers to the page
        elif c in _SMP_END:
            # region END edge (both the trim end and the loop-back point)
            smp.control(_SMP_END[c], "set_bounds_end", v / 127.0)
            smp.refresh_view(_SMP_END[c])


def _route_drum_cc(engine, cc, value):
    """Map a CH_DRUM CC to (voice, param) via the data-driven map computed in
    cfg.DRUM_CC (each voice packed to exactly its VOICE_PARAMS, from BASE). The
    HP band and the in-drum sample slots were removed 2026-07-21 — an unknown CC
    simply does nothing."""
    vp = DRUM_CC_TO_VP.get(cc)
    if vp:
        engine.drum_cc(vp[0], vp[1], value)
