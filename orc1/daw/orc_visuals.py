#!/usr/bin/env python3
"""ORC-1 visuals: glowing line-drawing scenes on the HDMI monitor, driven by
the music on the internal MIDI bus.

Run by visuals_session.sh inside the orc1-visuals kiosk X session. Draws on
the GPU (OpenGL 3.1 via moderngl), so the glow and trails cost the CPU almost
nothing. NOT a JACK client: it only READS MIDI (its knobs from the ORC's port
ORC-visuals; drum hits / notes observed on ORC1-Synth-Bus, since 2026-11-09),
so it cannot touch the audio graph or skipback.

What the music does:
  * a KICK (note 36/35 on a module bound to "drums" or "bbkit") = the beat
    pulse: lines swell and the trails lurch outward
  * a SNARE/CLAP (37-40) = a smaller flash
  * the lowest held TONAL note (any other bound module) sets the colour,
    walked round the circle of fifths, so related chords get related colours
  * how busy the playing is nudges the speed
  * the AUDIO (any JACK port picked on /visuals, default the master) comes
    from orc1-vistap, a tiny C listen-only JACK client, through shared
    memory; without it, off skipback's RAM ring (AudioTap). A bass
    hit in the BEAT BAND (CC 32/33, default 30-150 Hz) pulses like a kick,
    loudness swells the lines and the speed. Knob CC 28 scales it (0 = off); CC 29-31 = the
    beat's attack, release and sensitivity.
Nothing playing = the scenes drift on their own.

Scenes: tunnel, mystify, tube, harmonograph, warp, hypercube, scope, ridgeline,
rose, lorenz, chladni, and the SHAPE MACHINES (vis_shapes.py): knot, polytope,
hopf, shape4d.

Knobs: CCs on the channel the "visuals" module is bound to (see KNOBS);
their positions persist in ~/.orc1_visuals_state.json. `usage: orc_visuals.py
[scene] [--secs N] [--fps N]`: no scene = cycle every N s (default 60);
fps default 30 (MEASURED 2026-09-29 at 1280x720: 60 fps = ~61% of one core,
the trails hide the difference). SIGUSR1 = next
scene (visuals.sh next); SIGUSR2 = re-read ~/.orc1_visuals (visuals.sh set).
"""
import colorsys
import ctypes
import mmap
import struct
import json
import math
import os
import queue
import random
import re
import signal
import sys
import threading
import time
import zlib

import numpy as np
# NEVER let SDL open a sound device. pygame.init() starts SDL's AUDIO too:
# it grabbed the Pi's onboard headphone card (card 0) and underran it every
# ~30 ms (12,599 "snd_pcm_recover underrun" journal lines in an hour,
# 2026-09-29), and skipback-rec ran late twice inside that storm. The
# renderer makes no sound: dummy audio driver + display-only init.
os.environ["SDL_AUDIODRIVER"] = "dummy"
import pygame
import moderngl

BINDINGS = os.path.expanduser("~/.orc1_bindings.json")
CONF = os.path.expanduser("~/.orc1_visuals")      # visuals.sh set writes it
DRUM_MODULES = ("drums", "bbkit")
KICKS = (35, 36)
SNARES = (37, 38, 39, 40)
TAU = math.tau

# Knobs: CCs on the channel the "visuals" module is bound to on /matrix (any
# controller, a Fire knob naming the module, or the /system VISUALS card).
# 64 = the scene's own setting for all of 21-27.
SCENE_CC = 20            # 0 = cycle; otherwise picks a scene by value range
KNOBS = {
    21: "speed",         # x0.25 .. x4
    22: "twist",         # extra spin of the trails, either way
    23: "trails",        # shorter .. longer
    24: "zoom",          # trails pull in .. rush out
    25: "bright",        # dimmer .. brighter
    26: "beat",          # 0 = ignore the kick, 127 = twice the swell
    27: "colour",        # shift the palette round the wheel
    28: "audio",         # 0 = ignore the audio, 127 = twice the reaction
    29: "attack",        # audio beat swell-in: 0 = instant, 1 = one frame .. 250 ms
    30: "release",       # audio beat fade: 0.08 .. 1.5 s
    31: "sens",          # audio beat sensitivity: low = only big hits
    32: "band_lo",       # the beat band's low edge (20 Hz .. 16 kHz, log)
    33: "band_hi",       # the beat band's high edge
    # BACKGROUND (2026-09-30): an EarthBound-style bent pattern BEHIND the lines.
    # It rests at black (bg_level 0) and only the music lifts it, then it fades
    # back: owner wants the stark black behind the geometry kept.
    34: "bg_pattern",    # which pattern (buckets over BG_PATTERNS)
    35: "bg_mode",       # how it bends (buckets over BG_MODES)
    36: "bg_level",      # resting level: 0 = black when nothing plays
    37: "bg_react",      # how much beats / notes / loudness lift it (0 = never)
    38: "bg_lfo",        # 0 = off, else a slow breath (0.02 .. 1 Hz)
    39: "bg_warp",       # wave amplitude
    40: "bg_freq",       # wave frequency down the screen
    41: "bg_speed",      # wave speed
    42: "bg_scale",      # pattern tile size
    43: "bg_cycle",      # palette cycling speed (0 = still)
    # PEEPHOLE (2026-09-30): the last stage; outside the shape is pure black,
    # lines included -- for a projector, so the picture needn't be a rectangle.
    44: "mask_shape",    # buckets over MASK_SHAPES (0 = off = the full screen)
    45: "mask_size",
    46: "mask_soft",     # hard edge .. soft vignette
    47: "mask_x",        # 64 = centred
    48: "mask_y",
    49: "mask_fit",      # 0 = the scene is just clipped; 127 = shrunk to fit inside
    50: "bg_set",        # built-in / hero / monster 1.. (vis_patterns.pattern_sets)
    51: "mask_room",     # FIT leaves this much room round the shape for the trails
    52: "logo",          # 0 range = the chord's shape; then each logo (vis_logos.logo_list)
    53: "weight",        # line weight, every scene: 64 = its own; up = the overloaded glow
    54: "gate",          # audio noise gate: 0 = off, else -90 .. -40 dBFS (56 = -68)
    # CAMERA + MODS (2026-10-12, owner: "reactive lfo stuff on the shapes like
    # I have on the background"). The camera moves the finished lines (every
    # scene); TRAIL ZOOM / TRAIL TWIST (24 / 22) only move the trails. Each mod
    # target has a REACT (beats + loudness, shaped by ATTACK / RELEASE) and an
    # LFO depth; one LFO, free or locked to the ORC's bar. All rest at 0 = off.
    # 64-69 are skipped on purpose (sustain / pedal CCs other gear may send).
    55: "size",          # camera size: x1/3 .. x3, 64 = as drawn
    56: "angle",         # camera angle: -180 .. +180 deg, 64 = upright
    57: "lfo_rate",      # the mods' LFO, FREE: 0.02 .. 4 Hz (log)
    58: "lfo_sync",      # buckets over LFO_SYNC: free, or 4 bars .. 1/4 bar
    # BACKGROUND BEAT BAND (2026-10-12, owner: one detector for the shapes,
    # another for the background). Off = the background follows band 1
    # (CC 32/33 + SENS 31), as it always did.
    59: "band2_lo",      # band 2's low edge (log, 20 Hz .. 16 kHz, like 32)
    60: "band2_hi",      # band 2's high edge
    61: "sens2",         # band 2's own SENS (like 31)
    62: "band2_on",      # < 64 = the background follows band 1; >= 64 = its own band
    70: "size_react", 71: "size_lfo",       # swell on beats / breathe
    72: "spin_react", 73: "spin_lfo",       # twist on beats / rock
    74: "glow_react", 75: "glow_lfo",       # brighter on beats / pulse
    76: "sway_react", 77: "sway_lfo",       # nudged on beats / drift (figure 8)
    78: "trail_react", 79: "trail_lfo",     # longer trails on beats / breathe
    # RASTER BARS + DEPTH BLUR (2026-10-24): on top of ANY bend mode
    80: "bg_bars",       # copper bars: 0 = off .. full (each band its own colour)
    81: "bg_bars_size",  # how tall a bar is (4 .. 64 scanline rows)
    82: "bg_bars_speed", # 64 = still; either side rolls them up / down; beats shove them
    83: "bg_dof",        # depth blur: 0 = sharp; the skew modes blur what is far away
    # PICTURE LOOK (2026-10-24): for your pictures, and any tile too
    84: "bg_wrap",       # buckets over BG_WRAPS: repeat / mirror (no seam) / once
    85: "bg_crisp",      # 0 = the picture's own greys; else cut to black / white at this level
    86: "bg_invert",     # >= 64 = black <-> white
    87: "bg_thick",      # fatten thin lines (0 = off; cuts at the middle if CRISP is 0)
    # LAYERS + COLOUR (2026-10-24, owner: an automatable lightshow for a
    # projector over a band, so every look is a CC). Each layer has its own
    # level (0 = gone: background alone, lines alone), hue, saturation and a
    # palette SLOT (vis_palettes: set up on /visuals, picked by the knob).
    88: "lines_level",   # the lines layer, trails included: 0 = off .. 127 = full (squared)
    89: "bg_master",     # the background layer on top of LEVEL / REACT: 0 = off .. 127 = full
    90: "lines_hue",     # 64 = as drawn; -180 .. +180 degrees round the wheel
    91: "lines_sat",     # 64 = as drawn; 0 = white / grey; up = more vivid
    92: "lines_cycle",   # the lines' hue turning by itself: 0 = still
    93: "bg_hue",        # 64 = as now; -180 .. +180 degrees (BG HUE CYCLE = CC 43)
    94: "bg_sat",        # 64 = as now; 0 = grey; up = more vivid
    95: "lines_pal",     # 0 range = the scene's own colours, then palette slots 1..8
    96: "bg_pal",        # 0 range = the rainbow, then palette slots 1..8
}
MOD_TARGETS = ("size", "spin", "glow", "sway", "trail")
LFO_SYNC = ("free", "4 bars", "2 bars", "1 bar", "1/2 bar", "1/4 bar")
LFO_SYNC_BARS = (None, 4.0, 2.0, 1.0, 0.5, 0.25)
# knobs that rest somewhere other than the middle (64)
KNOB_DEFAULT = {29: 12,                   # ATTACK ~ 20 ms
                32: 8, 33: 38,            # = 30 Hz .. 150 Hz, the kick band
                34: 0, 35: 0, 36: 0, 38: 0, 44: 0, 46: 24, 49: 127, 50: 0, 51: 56, 52: 0, 54: 56,
                58: 0, 59: 8, 60: 38, 62: 0, **{cc: 0 for cc in range(70, 80)},
                80: 0, 83: 0, 84: 0, 85: 0, 86: 0, 87: 0,
                88: 127, 89: 127, 92: 0, 95: 0, 96: 0}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vis_patterns                                   # noqa: E402
import vis_shapes as VS                               # noqa: E402
import vis_logos                                      # noqa: E402
import vis_palettes                                   # noqa: E402

BG_PATTERNS = vis_patterns.BUILTIN                    # drawn by make_pattern
# "skew walls" + "skew floor" (2026-10-24, after GameHut / Coding Secrets on The
# Adventures of Batman & Robin, Mega Drive): the Genesis's two scroll tables at
# once. Walls = per-COLUMN vertical scroll (16 px strips, as on the Genesis;
# 20 of them filled its 320 px screen, 80 fill 1280): each strip slides up/down by its distance from its
# wall's centre, times how far that wall sits from the screen centre, so walls
# lean away at the sides and swing round as they scroll past (the buildings).
# Floor = per-LINE horizontal scroll: rows further from the horizon slide
# faster, so a floor and a ceiling run off to a black horizon (parallax).
# Spin = the same two tables as three shears = a chunky rotation that rocks;
# stretch = scanlines repeat toward the bottom (the Batwing stage's buildings
# stretching to the street); floor + skyline = the floor plus a far row of
# buildings above the horizon scrolling at a quarter speed (2026-10-24 later).
# Only ever APPEND here: a saved knob value is remapped by count (MODES_N in
# the state file), like the SET knob.
BG_MODES = ("wave", "interlace", "squeeze", "two layers", "wave + squeeze",
            "skew walls", "skew floor", "spin", "stretch", "floor + skyline")
LEGACY_MODES_N = 5       # a state file without "modes_n" is from the 5-mode days
MASK_SHAPES = ("off", "circle", "rounded", "hexagon", "diamond", "triangle", "star")
# how the background texture fills the screen (REPEAT, CC 84). repeat = the
# old behaviour (a tile is one seamless repeat); mirror = every other copy
# flipped, so ANY picture repeats without a seam (but half the copies read
# backwards); once = one copy centred on the screen, black around it.
BG_WRAPS = ("repeat", "mirror", "once")
# How a beat moves the background (2026-10-24 tuning, measured off-screen).
# SWAY: the beat's swell (Presence.sway, 0..1) multiplies BG WARP by 1 + this.
# WARP is a wall's lean / a spin's angle / a stretch in the newer modes, and
# x 2.5 there lurched the whole picture (a beat moved spin 70, walls 52,
# floor 14, floor + skyline 7), so those swell less. SURGE: a beat also
# pushes the newer modes' scroll forward (x 1 + this at full swell), so the
# floor rushes at you and the walls slide on the beat; built up over time,
# so the motion stays smooth. The five wave modes are as owner tuned them.
BG_SWAY = {5: 0.6, 7: 0.4, 8: 0.6}     # skew walls, spin, stretch; the rest 1.5
BG_SURGE = 2.0
# each shape's INSCRIBED radius (the biggest circle round its centre that stays
# inside), in the shader's units (size 1 = half the screen height). FIT keeps
# the scene inside that circle. Calibrated by rendering each mask on the Pi's
# GPU and measuring the nearest black pixel (_test_visuals re-measures it).
MASK_INRADIUS = (0.0, 1.0, 1.0, 1.0, 0.7071, 0.5774, 0.654)


def bucket(v, names):
    """A 0..127 knob -> an index into names, in equal ranges (the page's
    buttons send each range's middle)."""
    return min(len(names) - 1, max(0, int(v)) * len(names) // 128)
STATE = os.path.expanduser("~/.orc1_visuals_state.json")   # knobs + scene


# The SET knob (CC 50) splits 0..127 over the pattern sets, so ADDING a set
# moves every set's range (2026-10-12: the CUSTOM set made 5 -> 6; owner's
# saved 89 = monster 2 would have become monster 3). The state file records
# how many sets its SET value was meant for; a file without it is from the
# 5-set days. New sets are only ever APPENDED, so set i stays set i.
LEGACY_SETS_N = 5


def sets_count():
    return len(vis_patterns.pattern_sets(()))


def remap_set_knob(v, was_n, now_n):
    """A SET knob value meant for was_n sets -> the same set among now_n."""
    if was_n == now_n or was_n < 1 or now_n < 1:
        return v
    return bucket_mid(min(bucket(v, [0] * was_n), now_n - 1), now_n)


def load_knobs():
    k = {cc: KNOB_DEFAULT.get(cc, 64) for cc in KNOBS}
    try:
        with open(STATE) as f:
            st = json.load(f)
        saved = st.get("knobs", {})
        for cc in KNOBS:
            v = saved.get(KNOBS[cc])
            if isinstance(v, int) and 0 <= v <= 127:
                k[cc] = v
        if isinstance(saved.get("bg_set"), int):
            was = st.get("sets_n", LEGACY_SETS_N)
            k[50] = remap_set_knob(k[50], was if isinstance(was, int) else LEGACY_SETS_N, sets_count())
        if isinstance(saved.get("bg_mode"), int):
            # same trick for the BEND MODE knob (CC 35): 5 -> 10 modes moved the ranges
            was = st.get("modes_n", LEGACY_MODES_N)
            k[35] = remap_set_knob(k[35], was if isinstance(was, int) else LEGACY_MODES_N, len(BG_MODES))
    except Exception:
        pass
    return k


def save_state(knobs, scene, cycling):
    """Knob positions survive a restart; the web card reads this back."""
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"knobs": {KNOBS[cc]: v for cc, v in knobs.items()},
                       "scene": scene, "cycling": cycling, "sets_n": sets_count(),
                       "modes_n": len(BG_MODES)}, f)
        os.replace(tmp, STATE)
    except Exception as e:
        print(f"[visuals] state save: {e}", flush=True)


class StateSaver:
    """save_state() on its own thread. The frame loop used to write the file
    itself, and an SD card that stalls for a moment froze the picture:
    MEASURED 2026-10-24, 4 of an hour's 6 hitches were "save" (109-351 ms)
    while owner moved knobs. put() only hands over a copy and returns; the
    thread writes the newest one (older ones still waiting are skipped)."""

    def __init__(self, save=None):
        self._save = save or save_state
        self._lock = threading.Lock()
        self._next = None
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="visuals-save").start()

    def put(self, knobs, scene, cycling):
        with self._lock:
            self._next = (dict(knobs), scene, cycling)
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._next = self._next, None
            if job is not None:
                self._save(*job)


# ---------------------------------------------------------------- music state

class Music:
    """What the MIDI thread has heard, read by the render loop. Plain
    attribute writes/reads (GIL-atomic); no lock needed for this."""

    def __init__(self):
        self.kick_t = -99.0
        self.kick_v = 0.0
        self.snare_t = -99.0
        self.hue = random.random() * 360.0    # target hue from the chord
        self.held = {}                        # (ch, note) -> note
        self.last_note_t = -99.0
        self.note_t, self.note_v = -99.0, 0.0  # the last tonal note-on
        self.rate = 0.0                       # note-ons per second, smoothed
        self.knobs = load_knobs()             # CC -> 0..127 (see KNOBS)
        self.knobs_dirty = False
        self.scene_req = None                 # CC 20 pick, for the render loop
        self._drum_chs = set()
        self._vis_ch = None                   # our own channel (0-based)
        self._bind_t = -99.0

    def _read_bindings(self, now):
        if now - self._bind_t > 2.0:          # /matrix moves apply live
            self._bind_t = now
            try:
                with open(BINDINGS) as f:
                    b = json.load(f).get("bindings", {})
                self._drum_chs = {int(ch) - 1 for ch, m in b.items()
                                  if m in DRUM_MODULES}
                self._vis_ch = next((int(ch) - 1 for ch, m in b.items()
                                     if m == "visuals"), None)
            except Exception:
                pass

    def drum_channels(self, now):
        self._read_bindings(now)
        return self._drum_chs

    def on_msg(self, msg):
        now = time.monotonic()
        self._read_bindings(now)
        if msg.type == "control_change" and msg.channel == self._vis_ch:
            if msg.control == SCENE_CC:
                self.scene_req = msg.value
            elif msg.control in KNOBS:
                self.knobs[msg.control] = msg.value
                self.knobs_dirty = True
            return
        if msg.type not in ("note_on", "note_off"):
            return
        if msg.channel == self._vis_ch:       # our channel: knobs only (the
            return                            # bind handshake's note lands here)
        drums = self._drum_chs
        on = msg.type == "note_on" and msg.velocity > 0
        if msg.channel in drums:
            if on and msg.note in KICKS:
                self.kick_t, self.kick_v = now, 0.5 + msg.velocity / 254.0
            elif on and msg.note in SNARES:
                self.snare_t = now
            if on:
                self._count(now)
            return
        if msg.channel == 12:                 # ch13 = the control channel
            return
        key = (msg.channel, msg.note)
        if on:
            self.held[key] = msg.note
            self.note_t, self.note_v = now, 0.4 + msg.velocity / 212.0
            self._count(now)
            low = min(self.held.values())
            self.hue = ((low % 12) * 7 % 12) * 30.0   # circle of fifths
        else:
            self.held.pop(key, None)

    def _count(self, now):
        dt = now - self.last_note_t
        self.last_note_t = now
        self.rate = self.rate * 0.85 + (0.15 / max(dt, 0.05))


def is_bus_port(pname):
    """A port name (the part in quotes / after the client) is the brain's synth bus."""
    return "orc1-synth-bus" in pname.lower()


def find_bus(names):
    """The brain's internal MIDI bus among the input names, or None."""
    return next((n for n in names if is_bus_port(n)), None)


# 2026-11-09: the visuals are not a synth, so their knobs come from the ORC (the brain) on its OWN port
# "ORC-visuals", never the synth bus (owner: "don't ever move anything that doesn't need an internal synthesis onto
# the synth"). The synth bus is still READ, as an observer, for the drum hits / notes the pictures react to.
# Exact port name (same rule as orc1_config.is_orc_port, copied: this renderer stands alone): "ORC-visuals" is never
# confused with our own client "orc-visuals-in".
ORC_PORT = "ORC-visuals"


def port_part(alsa_name):
    """'RtMidiOut Client:ORC-visuals 140:0' -> 'ORC-visuals' (= orc1_config.port_part)."""
    m = re.match(r"^[^:]*:(.*?)(?: \d+:\d+)?$", alsa_name or "")
    return m.group(1) if m else ""


def find_orc_port(names, port=ORC_PORT):
    """The ORC's port named exactly `port` among the input names, or None."""
    return next((n for n in names if port_part(n) == port), None)


MIDI_CLIENT = "orc-visuals-in"      # our client on the synth bus (hits, notes); the subscription check finds it
CTL_CLIENT = "orc-visuals-ctl"      # our client on the ORC's ORC-visuals port (the knobs)
SEQ_CLIENTS = "/proc/asound/seq/clients"


def seq_listening(text, me=MIDI_CLIENT, is_src=is_bus_port):
    """The kernel's sequencer listing (/proc/asound/seq/clients) -> True when
    some port of the client named `me` is Connected From a port whose name
    passes is_src (default: ORC1-Synth-Bus), i.e. the CURRENT source still feeds us."""
    bus, mine, cname, sub = set(), [], None, False
    for ln in text.splitlines():
        m = re.match(r'Client\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m:
            cid, cname = m.group(1), m.group(2)
            continue
        m = re.match(r'\s+Port\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m and cname is not None:
            if is_src(m.group(2).strip()):
                bus.add(f"{cid}:{m.group(1)}")
            sub = cname == me
            continue
        m = re.match(r"\s+Connected From:\s*(.*)", ln)
        if m and sub:
            mine += [a.split("[")[0].strip() for a in m.group(1).split(",")]
    return any(a in bus for a in mine)


def _safe_cb(music):
    """rtmidi's callback -> music.on_msg, never raising into the rtmidi thread
    (it used to be this thread's own loop, where an error was logged and the
    port reopened): log it, at most every 5 s."""
    import mido
    last = [0.0]

    def cb(event, _data=None):
        try:
            music.on_msg(mido.Message.from_bytes(event[0]))
        except Exception as e:
            if time.monotonic() - last[0] > 5.0:
                last[0] = time.monotonic()
                print(f"[visuals] MIDI message error: {e}", flush=True)
    return cb


def _read_seq():
    try:
        with open(SEQ_CLIENTS) as f:
            return f.read()
    except OSError:
        return None


def midi_thread(music, rt=None, read_seq=_read_seq, sleep=time.sleep, check_s=1.0, stop=None,
                client=MIDI_CLIENT, find=find_bus, is_src=is_bus_port):
    """Listen to ONE source (default the brain's bus, ORC1-Synth-Bus; since 2026-11-09 also run with
    client=CTL_CLIENT on the ORC's exact "ORC-visuals" port for the knobs) and follow it when the brain
    restarts. A restart destroys the bus and makes a new one, usually under a
    new ALSA client number; a port opened on the old one then just goes quiet
    (no error), so every knob silently stopped reaching the visuals after a
    brain restart until the kiosk restarted (2026-10-24, measured: the
    renderer still on 132:0, the new bus on 139:0).
    So: ONE rtmidi client of our own (MIDI_CLIENT) for the whole run (listing
    + reopening reuse it: mido's get_input_names made and dropped two ALSA
    clients per call), and every check_s the kernel's listing says whether
    that client is still Connected From the CURRENT bus. Not = reopen. That
    also catches a bus recreated under the same name and number (the
    subscription goes with the old port). No listing (not Linux): reopen when
    the bus's name changes. Messages arrive on rtmidi's callback."""
    if rt is None:
        import rtmidi
        rt = rtmidi.MidiIn(name=client)
        rt.ignore_types(sysex=True, timing=True, active_sense=True)   # notes + CCs only
    cb = _safe_cb(music)
    name = None
    while stop is None or not stop():
        try:
            ports = rt.get_ports()
        except Exception:
            ports = []
        now = find(ports)
        text = read_seq()
        fine = name is not None and now == name and (text is None or seq_listening(text, client, is_src))
        if name is not None and not fine:
            print(f"[visuals] MIDI ({client}) {name} lost (now {now}): reconnecting", flush=True)
            try:
                rt.close_port()
            except Exception:
                pass
            name = None
        if name is None and now:
            try:
                rt.open_port(ports.index(now))
                # after EVERY open: python-rtmidi's close_port() drops the
                # callback (measured on the Pi 2026-10-24: set once, a close +
                # reopen delivered nothing; the visuals went deaf at their
                # first reconnect)
                try:
                    rt.cancel_callback()
                except Exception:
                    pass
                rt.set_callback(cb)
                name = now
                print(f"[visuals] MIDI in ({client}): {name}", flush=True)
            except Exception as e:
                print(f"[visuals] MIDI error: {e}, retrying", flush=True)
        sleep(check_s)
    try:
        rt.close_port()
    except Exception:
        pass


# ---------------------------------------------------------------- audio tap

# The LISTEN detector (the tap reader, the beat detector, the band / SENS helpers) lives in
# vis_listen.py since 2026-11-02 (numpy only: the MXV box conductor runs it too).
from vis_listen import (SKIPBUF, TAP_SHM, TAP_SRC, TapShm, cc_to_hz, hz_to_cc, AudioTap,  # noqa: E402,F401
                        tap_ports, audio_onset, audio_gate, band2_on)


AUDIO_PUB = "/tmp/orc1_visuals_audio.json"


_PORTS_CACHE = [-99.0, None]


def publish_audio(tap, beat_age, level, fps, extra=None):
    """What the /visuals page draws: the spectrum, the beat band (as set and
    as really heard), when the last beat fired, and the latency parts
    (tmpfs, ~25 writes/s, ~1 KB; the page streams it over SSE)."""
    try:
        lo, hi = tap.band
        now = time.monotonic()
        if now - _PORTS_CACHE[0] > 1.0:
            _PORTS_CACHE[:] = [now, tap_ports()]
        d = {"t": time.time(), "spec": tap.spec, "band": [round(lo), round(hi)],
             "band_true": [round(v) for v in tap.band_true],
             "band2_on": bool(tap.band2_on),
             "band2_true": [round(v) for v in tap.band2_true],
             "spec_runs": tap.spec_runs,
             "beat_age": round(beat_age, 3),
             "level": None if level is None else round(level, 3),
             "source": tap.source,
             "ring": os.path.basename(tap.dir or ""),
             "tap_ports": _PORTS_CACHE[1] if tap.source == "tap" else None,
             "cadence_ms": round(tap.cadence, 1),
             "age_ms": round(tap.age_ms, 1) if tap.source == "tap" else None,
             "win_ms": round(tap.N * 1000.0 / (tap.rate or 48000)),
             "fps": fps}
        d.update(extra or {})
        tmp = AUDIO_PUB + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, AUDIO_PUB)
    except Exception:
        pass


def vsync_plan(fps, refresh):
    """How many monitor refreshes each frame should last, or 0 = no vsync.
    MEASURED 2026-09-30: unsynced, the renderer ran at ~62 fps on a 60.00 Hz
    monitor (it slept between frames and flipped whenever ready), so every
    ~0.5 s a frame showed twice or was skipped -- a small regular jerk that a
    smooth scene (tube) makes plain. Locking to the refresh fixes it when fps
    divides the refresh (60 -> every refresh, 30 -> every 2nd); 45 on 60 Hz
    can't be even, so it keeps the old sleep pacing."""
    if not fps or not refresh or fps > refresh * 1.02:
        return 0
    n = refresh / float(fps)
    k = int(round(n))
    return k if k >= 1 and abs(n - k) < 0.05 else 0


def frame_dt(raw, refresh, interval):
    """The time step the motion uses. Locked to the refresh, a frame lasts a
    WHOLE number of refreshes, so the measured time (which jitters by a few
    ms) is snapped to that: every frame moves the same amount and the motion
    stays even; a missed refresh still counts as two."""
    if not interval or not refresh:
        return raw
    return max(interval, int(round(raw * refresh))) / float(refresh)


def set_swap_interval(n):
    """Ask SDL (the one pygame uses: Debian's python3-pygame links the system
    libSDL2) to sync buffer swaps to every n-th refresh; 0 = off. -> True if
    the driver accepted it."""
    try:
        sdl = ctypes.CDLL("libSDL2-2.0.so.0")
        return sdl.SDL_GL_SetSwapInterval(int(n)) == 0
    except (OSError, AttributeError):
        return False


SHOT_PNG = "/tmp/orc1_visuals_shot.png"


def png_bytes(raw, W, H, flip=True, channels=3):
    """Packed RGB (channels 3) or RGBA (4) rows -> a PNG file's bytes, stdlib
    only. flip = the rows come bottom-up (GL's order). The heavy part,
    zlib.compress, RELEASES the GIL; pygame.image.save held it for the whole
    encode, so a shot from the writer thread still froze the frame loop
    ~0.7 s (measured 2026-10-12)."""
    stride = W * channels
    mv = memoryview(raw)
    rows = range(H - 1, -1, -1) if flip else range(H)
    data = b"".join(b"\x00" + mv[y * stride:(y + 1) * stride] for y in rows)

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    colour = {3: 2, 4: 6}[channels]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, colour, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data, 3)) + chunk(b"IEND", b""))


def save_shot(ctx, W, H):
    """visuals.sh shot (SIGWINCH): the finished frame (what the monitor gets)
    -> SHOT_PNG. The read has to happen here, on the frame loop (the GL
    context's thread); the PNG is made in a thread. MEASURED 2026-10-12 on
    the Pi (V3D, 1024x768): an RGB read cost ~125 ms (Mesa converts on the
    CPU), RGBA ~40 ms; reading into a GL buffer and mapping it a few frames
    later cost MORE (~45 + ~47 ms), so it is one plain RGBA read."""
    try:
        rgba = ctx.screen.read(components=4)
    except Exception as e:
        print(f"[visuals] shot: {e}", flush=True)
        return
    threading.Thread(target=write_shot, args=(rgba, W, H), daemon=True).start()


def write_shot(rgba, W, H):
    """GL's bottom-up RGBA rows -> SHOT_PNG, top-down. The screen's alpha is
    255 everywhere (measured), so the PNG keeps it: dropping it is a ~40 ms
    numpy copy that holds the GIL. Only if a pixel is not opaque (a display
    with real alpha) is it dropped, so the picture never comes out see-through."""
    try:
        a = np.frombuffer(rgba, np.uint8)
        if a[3::4].min() == 255:
            png = png_bytes(rgba, W, H, channels=4)
        else:
            png = png_bytes(a.reshape(-1, 4)[:, :3].tobytes(), W, H)
        tmp = SHOT_PNG[:-4] + ".tmp.png"
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, SHOT_PNG)
    except Exception as e:
        print(f"[visuals] shot: {e}", flush=True)


def conf_fps(default):
    """Line 3 of ~/.orc1_visuals = frames per second (30 / 45 / 60)."""
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
        v = int(lines[2]) if len(lines) > 2 and lines[2].strip().isdigit() else 0
        return v if v in (30, 45, 60) else default
    except (OSError, ValueError):
        return default


class AudioPulse:
    """Beat envelope: a hit SWELLS to its full height over `attack` s (a
    smooth ramp), then fades with `release`. ATTACK only shapes the rise.

    MEASURED 2026-10-12 (owner: the controls felt "slightly screwy"): the
    old envelope chased a target that was already fading, so a slow ATTACK
    also SHRANK the beat -- ATTACK 127 + a short RELEASE peaked at 29% of a
    full hit, and RELEASE changed the size too. Now every hit reaches full
    height. At ATTACK 0 this is exactly the old envelope (frame for frame;
    _test_visuals checks it), which is owner's setting."""

    def __init__(self, attack=0.02, release=0.35):
        self.attack, self.release = attack, release
        self.v = 0.0
        self.peak = 0.0
        self._pend = 0.0                         # hits since the last step
        self._from, self._t = 0.0, None          # _t = time into the rise, None = falling

    def hit(self, strength):
        self._pend = max(self._pend, strength)

    def step(self, dt):
        fall = self.v * math.exp(-dt / max(self.release, 1e-3))
        if self._pend > 0.0:
            s, self._pend = self._pend, 0.0
            # a new hit only counts if it's bigger than where the pulse is
            # heading (the peak while rising, the faded value while falling)
            if s > (self.peak if self._t is not None else fall):
                self.peak, self._from, self._t = s, self.v, 0.0
        if self._t is None:
            self.v = fall
            return self.v
        # rise first, THEN fall: an instant attack must show the hit at full
        # size on its own frame (falling first shaved ~10% off)
        if self.attack <= 1e-3:
            x = 1.0
        else:
            self._t += dt
            x = min(1.0, self._t / self.attack)
        if x >= 1.0:
            self.v, self._t = self.peak, None
        else:
            self.v = self._from + (self.peak - self._from) * (0.5 - 0.5 * math.cos(math.pi * x))
        return self.v


def audio_attack(v):
    """ATTACK knob (CC 29) -> seconds: 0 = instant, 1 = one 60 fps frame
    (16 ms) .. 127 = 250 ms, log, so every notch is visible. The old map
    (0.15 * x**3) left the bottom 40% of the knob under one frame."""
    v = max(0, min(127, int(v)))
    return 0.0 if v == 0 else 0.016 * (0.25 / 0.016) ** ((v - 1) / 126.0)


def audio_release(v):
    """RELEASE knob (CC 30) -> seconds: 0.08 .. 1.5, 64 ~ 0.35 (log)."""
    return 0.08 * (1.5 / 0.08) ** (max(0, v) / 127.0)


def audio_amount(v):
    """AUDIO knob (CC 28) -> reaction amount: a curve, so the low half is
    genuinely subtle (1 -> 0.002, 32 -> 0.35, 64 -> 1, 127 -> 2)."""
    return min(2.0, (max(0, v) / 64.0) ** 1.5)


# ---------------------------------------------------------- layers + colour

def layer_gain(v):
    """A LEVEL knob (LINES LEVEL / BACKGROUND LEVEL) -> its gain: 0 = off,
    127 = 1 (= the old look), squared so the bottom fades gently."""
    return (max(0, min(127, v)) / 127.0) ** 2


def sat_mult(v):
    """A SATURATION knob -> a multiplier on the colour's distance from grey:
    64 = 1 (as drawn), 0 = grey / white, 127 = 2 (more vivid)."""
    return max(0, min(127, v)) / 64.0


def hue_deg(v):
    """A HUE knob -> degrees round the wheel: 64 = 0, 0 = -180, 127 = ~+177."""
    return 180.0 * (v - 64) / 64.0


def palette_grad(pal, h):
    """A palette (list of (r, g, b) 0..1) as a loop, read at h (array, 0..1
    wraps) -> colours [n, 3]: blends from one colour to the next and back to
    the first, so a hue that turns round the wheel walks the whole palette."""
    pal = np.asarray(pal, "f4").reshape(-1, 3)
    n = len(pal)
    x = (np.asarray(h, "f4") % 1.0) * n
    i = np.floor(x).astype(int) % n
    f = (x - np.floor(x))[:, None]
    f = f * f * (3.0 - 2.0 * f)                         # smooth through each colour
    return pal[i] * (1.0 - f) + pal[(i + 1) % n] * f


def recolour(rgb, sat=1.0, pal=None):
    """The LINES layer's colour knobs on every segment's colour (rows of r, g,
    b, any scale: additive lines + old_light go above 1). pal = a palette or
    None: the segment's hue picks a place on the palette loop and its
    brightness (max of r, g, b) is kept. sat = sat_mult(): pulls toward (0)
    or pushes away from (> 1) the grey of the same brightness, so 0 = white
    lines at full brightness. Returns rgb itself, untouched, at sat 1 and no
    palette (the old look, bit for bit)."""
    if pal is None and sat == 1.0:
        return rgb
    v = rgb.max(axis=1)
    if pal is not None:
        mn = rgb.min(axis=1)
        c = np.maximum(v - mn, 1e-6)
        r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        h = np.where(v == r, (g - b) / c, np.where(v == g, 2.0 + (b - r) / c, 4.0 + (r - g) / c)) / 6.0
        out = palette_grad(pal, h) * v[:, None]
    else:
        out = rgb
    if sat != 1.0:
        out = np.maximum(v[:, None] + (out - v[:, None]) * sat, 0.0)
    return out.astype("f4")


# ------------------------------------------------------------- background

def fit_texture(g, max_side):
    """A grey image array -> one no bigger than max_side on either side
    (the GPU's GL_MAX_TEXTURE_SIZE), shrunk by whole-number steps."""
    k = -(-max(g.shape) // max_side)                     # ceil
    return g if k <= 1 else g[::k, ::k]


def cache_evict(cache, keep_tiles, keep_pics, pic_set):
    """Drop the oldest file tiles beyond keep_tiles, and the oldest PICTURES
    beyond keep_pics (a picture is up to 4 MB, a tile ~64 KB: the visuals
    session is capped at 200 MB). Built-ins (set 0) always stay. -> the
    dropped keys. `cache` keeps insertion order (oldest first)."""
    pics = [k for k in cache if k[0] == pic_set]
    tiles = [k for k in cache if k[0] not in (0, pic_set)]
    gone = pics[:max(0, len(pics) - keep_pics)] + tiles[:max(0, len(tiles) - keep_tiles)]
    for k in gone:
        cache.pop(k, None)
    return gone


def make_pattern(name, n=256, shift=0.0):
    """One seamless grey tile, float32 [n, n] in 0..1: every feature repeats a
    whole number of times across the tile, so it wraps without a seam. Black
    (0) = gaps, which stay black on screen -- the colour comes from the
    palette, not the tile. `shift` offsets the pattern (in tiles); the test
    uses it to prove a half-tile shift equals a half-tile roll = seamless."""
    y, x = np.mgrid[0:n, 0:n].astype("f4") / n + shift  # 0..1, wraps at 1
    tau = 2 * np.pi
    if name == "checker":
        v = ((np.floor(x * 8) + np.floor(y * 8)) % 2)
    elif name == "stripes":
        v = 0.5 + 0.5 * np.cos(tau * 6 * (x + y))
        v = np.clip((v - 0.35) * 3, 0, 1)
    elif name == "dots":
        cx, cy = (x * 8) % 1 - 0.5, (y * 8) % 1 - 0.5
        v = np.clip((0.32 - np.hypot(cx, cy)) * 12, 0, 1)
    elif name == "truchet":
        cells = 8
        rng = np.random.RandomState(7)
        flip = rng.randint(0, 2, (cells, cells))
        cx, cy = (x * cells) % 1, (y * cells) % 1
        f = flip[(y * cells).astype(int) % cells, (x * cells).astype(int) % cells]
        cx = np.where(f == 1, 1 - cx, cx)
        d = np.minimum(np.abs(np.hypot(cx, cy) - 0.5), np.abs(np.hypot(cx - 1, cy - 1) - 0.5))
        v = np.clip((0.09 - d) * 14, 0, 1)
    elif name == "waves":
        d = np.abs(((y * 10 + 0.12 * np.sin(tau * 3 * x)) % 1) - 0.5)
        v = np.clip((0.12 - np.abs(d - 0.25)) * 10, 0, 1)
    elif name == "diamonds":
        cx, cy = (x * 6) % 1 - 0.5, (y * 6) % 1 - 0.5
        d = np.abs(cx) + np.abs(cy)
        v = np.clip((0.06 - np.abs(d - 0.35)) * 16, 0, 1)
    elif name == "rings":
        cx, cy = (x * 4) % 1 - 0.5, (y * 4) % 1 - 0.5
        v = 0.5 + 0.5 * np.cos(tau * 5 * np.hypot(cx, cy) * 2)
        v = np.clip((v - 0.6) * 3, 0, 1)
    elif name == "plaid":
        v = (0.5 + 0.5 * np.cos(tau * 5 * x)) * 0.6 + (0.5 + 0.5 * np.cos(tau * 7 * y)) * 0.6
        v = np.clip(v - 0.55, 0, 1) * 1.6
    elif name == "bricks":
        row = np.floor(y * 8)
        bx = (x * 4 + 0.5 * (row % 2)) % 1
        by = (y * 8) % 1
        v = ((np.minimum(bx, 1 - bx) > 0.04) & (np.minimum(by, 1 - by) > 0.08)).astype("f4") * 0.8
    elif name == "zigzag":
        d = np.abs(((y * 8 + np.abs(((x * 8) % 1) - 0.5)) % 1) - 0.5)
        v = np.clip((0.14 - d) * 8, 0, 1)
    else:
        v = np.zeros((n, n), "f4")
    return np.clip(v, 0, 1).astype("f4")


class Presence:
    """How much of the background shows (0 = black). It RESTS at the level
    knob (default 0 = black); beats, kicks, notes and loudness lift it with
    a quick rise, and it falls back over ~0.7 s. The LFO, when on, is a slow
    breath on top. owner 2026-09-30: keep the stark black behind the
    geometry most of the time; let the music bring the background in."""

    RISE, FALL = 0.04, 0.7
    SWAY = 0.15              # s: how fast the motion swell follows

    def __init__(self):
        self.env = 0.0
        self.sway = 0.0          # the slow copy of env that swells the MOTION
        self.ph = 0.0

    def step(self, dt, drive, rest, react, lfo_hz):
        k = self.RISE if drive > self.env else self.FALL
        self.env += (drive - self.env) * (1.0 - math.exp(-dt / k))
        # the sway amplitude follows a SLOWER copy: with the 40 ms envelope a
        # beat yanked the whole background at once, which read as a hitch
        # (owner 2026-09-30); brightness still punches on the beat
        self.sway += (self.env - self.sway) * (1.0 - math.exp(-dt / self.SWAY))
        lfo = 0.0
        if lfo_hz > 0:
            self.ph = (self.ph + dt * lfo_hz) % 1.0
            lfo = 0.4 * (0.5 - 0.5 * math.cos(math.tau * self.ph))
        return max(0.0, min(1.0, rest + react * self.env + lfo))


def bg_params(kn, H):
    """Background knob positions (0..127 by name) -> the shader's numbers."""
    u = lambda k: max(0, kn[k]) / 127.0                   # noqa: E731
    return {
        "rest": u("bg_level") ** 2,
        "react": kn["bg_react"] / 64.0,                    # 0 .. 2
        "lfo_hz": 0.0 if kn["bg_lfo"] == 0 else 0.02 * 50 ** u("bg_lfo"),
        "amp": 120.0 * u("bg_warp") ** 2 * H / 720.0,      # px (64 ~ 30)
        "freq": 0.01 * 50 ** u("bg_freq"),                 # rad per row
        "speed": 6.0 * u("bg_speed"),                      # rad/s
        "scale": 64.0 * 8 ** u("bg_scale") * H / 720.0,    # built-in tile px
        "unit": 1.5 * 4 ** ((kn["bg_scale"] - 64) / 64.0) * H / 720.0,  # px per file-tile unit
        "cycle": 0.5 * u("bg_cycle") ** 2,                 # palette turns/s
        "bars": u("bg_bars"),                              # 0 .. 1
        "bars_rows": 4.0 * 16 ** u("bg_bars_size"),        # rows per bar, 4 .. 64
        "bars_rate": 3.0 * (kn["bg_bars_speed"] - 64) / 64.0,   # bars/s, - = the other way
        # mipmap levels, 0 .. 7: a tile square can be ~128 texels, so it takes ~7
        # halvings to melt it (at 5 a big checker still looked crisp)
        "dof": 7.0 * u("bg_dof"),
        "wrap": bucket(kn["bg_wrap"], BG_WRAPS),
        # CRISP 1..127 -> the cut level 0.05 .. 0.95 (0 = off)
        "crisp": 0.0 if kn["bg_crisp"] <= 0 else 0.05 + 0.9 * (kn["bg_crisp"] - 1) / 126.0,
        "invert": 1 if kn["bg_invert"] >= 64 else 0,
        # extra mipmap levels, 0 .. 3 on a squared curve: the low half fattens
        # lines gently (64 ~ 0.75), the top end melts them into blobs (owner
        # liked the blobs: "looks fantastic"), so both are on the one knob
        "thick": 3.0 * u("bg_thick") ** 2,
    }


def mask_params(kn):
    """Peephole knob positions -> the shader's numbers. size 1 = the shape
    spans the screen height; x/y 64 = centred."""
    u = lambda k: max(0, kn[k]) / 127.0                   # noqa: E731
    return {
        "shape": bucket(kn["mask_shape"], MASK_SHAPES),
        "size": 0.2 + 1.4 * u("mask_size"),
        "soft": 0.002 + 0.5 * u("mask_soft") ** 2,
        "pos": ((kn["mask_x"] - 64) / 64.0, (kn["mask_y"] - 64) / 64.0),
        "fit": u("mask_fit"),
        # the shape fills this share of the peephole (1 = to the edge). The
        # trails' spiral forms as they drift OUTWARD: fitted to the very edge
        # they were cut off at once ("lost the twisting", owner 2026-09-30)
        "room": 1.0 - 0.5 * u("mask_room"),
    }


PLAYSTATE = "/dev/shm/orc1_playstate.json"     # the brain's live feed (orc1_playstate.py)


class Tempo:
    """The ORC's tempo + bar position, read from the brain's play-state feed
    (written on change + a 1 s heartbeat; never asks the brain). -> bars(): the
    sequencer's position in bars while it runs (its global step + how long
    since that step), else None; bpm: the last tempo seen (120 until then)."""

    EVERY = 0.1
    # how far past the newest step the bar may be extrapolated. A step is
    # read up to one poll (0.1 s) + the brain's publish lag (<= 33 ms) after
    # it began, so the cap must cover a whole step PLUS that, or the bar
    # freezes and jumps 10x a second (2026-10-12 review measured the synced
    # LFO's speed wobbling 0.66x-1.11x at 120 BPM with a 1.5-step cap)
    SLACK = 0.25
    # the brain rewrites the feed at least once a second (heartbeat): older
    # than this = the brain is gone, so stop following its frozen bar
    STALE = 2.5

    def __init__(self, path=PLAYSTATE, wall=time.time):
        self.path, self.wall = path, wall
        self.bpm, self.running = 120.0, False
        self._step, self._step_wall, self._step_s = -1, 0.0, 0.125
        self._t, self._mt = -99.0, None
        self._file_t = 0.0

    def poll(self, now):
        if now - self._t < self.EVERY:
            return
        self._t = now
        try:
            mt = os.stat(self.path).st_mtime_ns
            if mt == self._mt:
                return
            with open(self.path) as f:
                d = json.load(f)
            self._mt = mt
            tr = d.get("transport") or {}
            self.bpm = float(tr.get("bpm") or 120.0)
            self.running = bool(tr.get("running")) and int(tr.get("step", -1)) >= 0
            self._step = int(tr.get("step", -1))
            self._step_s = float(tr.get("step_ms") or 125.0) / 1000.0
            # when that step started, on the wall clock (the file's own stamp
            # minus how old the step already was when it was written)
            self._file_t = float(d.get("t") or 0.0)
            self._step_wall = self._file_t - float(tr.get("age_ms") or 0) / 1000.0
        except (OSError, ValueError, TypeError):
            self.running = False

    def bars(self):
        now = self.wall()
        if not self.running or now - self._file_t > self.STALE:
            return None
        into = max(0.0, min(now - self._step_wall, self._step_s + self.SLACK))
        return (self._step + into / self._step_s) / 16.0


class ModLFO:
    """The mods' one LFO: phase 0..1. FREE = its own rate; SYNCED = locked to
    the ORC's bar while the sequencer runs (it glides onto the bar's phase,
    never jumps), and at the ORC's tempo when it is stopped."""

    PULL = 0.15            # s: how fast a synced LFO glides onto the bar

    def __init__(self):
        self.ph = 0.0

    def step(self, dt, free_hz, sync_bars, tempo):
        if sync_bars is None:
            self.ph = (self.ph + dt * free_hz) % 1.0
            return self.ph
        rate = tempo.bpm / 240.0 / sync_bars          # cycles/s (240 = 4 beats x 60 s)
        self.ph = (self.ph + dt * rate) % 1.0
        b = tempo.bars()
        if b is not None:
            err = ((b / sync_bars) % 1.0 - self.ph + 0.5) % 1.0 - 0.5
            self.ph = (self.ph + err * (1.0 - math.exp(-dt / self.PULL))) % 1.0
        return self.ph


def bg_pulse(knobs, a_p, b_p):
    """The audio beat that lifts the BACKGROUND: band 2's pulse when it has
    its own band, else band 1's (the shapes'), exactly as before."""
    return b_p if band2_on(knobs) else a_p


def lfo_rate(v):
    """LFO RATE (CC 57) -> Hz, free mode: 0.02 .. 4, log (64 ~ 0.29)."""
    return 0.02 * 200.0 ** (max(0, min(127, v)) / 127.0)


def mod_params(knobs, hit, ph, H):
    """The camera + mods for one frame. knobs = CC -> 0..127; hit = the beat
    envelope (kicks + audio beats, shaped by ATTACK / RELEASE, + loudness),
    0..1.2 (capped by the caller); ph = the LFO phase. -> size (x), angle (rad), sway (dx, dy px),
    glow (gain factor), trail (added to the TRAILS knob, -1..1 units)."""
    u = lambda cc: max(0, min(127, knobs.get(cc, KNOB_DEFAULT.get(cc, 64)))) / 127.0   # noqa: E731
    c = lambda cc: (knobs.get(cc, 64) - 64) / 64.0                                     # noqa: E731
    s = math.sin(TAU * ph)
    react = {t: u(70 + 2 * i) * hit for i, t in enumerate(MOD_TARGETS)}
    lfo = {t: u(71 + 2 * i) for i, t in enumerate(MOD_TARGETS)}
    # sway: beats push along a direction that turns each bar of the LFO
    # (so it isn't always the same way), the LFO draws a slow figure 8
    sa = TAU * ph * 0.25
    return {
        "size": 3.0 ** c(55) * 2.0 ** (react["size"] * 1.0 + lfo["size"] * 0.6 * s),
        "angle": math.pi * c(56) + react["spin"] * math.pi / 6 + lfo["spin"] * math.pi / 4 * s,
        "sway": ((react["sway"] * 0.08 * math.cos(sa) + lfo["sway"] * 0.2 * s) * H,
                 (react["sway"] * 0.08 * math.sin(sa) + lfo["sway"] * 0.12 * math.sin(2 * TAU * ph)) * H),
        "glow": 2.0 ** (react["glow"] * 1.5 + lfo["glow"] * 1.0 * s),
        "trail": react["trail"] * 1.0 + lfo["trail"] * 0.8 * s,
    }


def camera(fit, mods, W, H):
    """Fold the camera (size, angle, sway) into the FIT transform -> the
    Lines.array argument (c0, M, c1), or `fit` unchanged when the camera is
    at rest (so the defaults draw exactly what they did before). The camera
    works AFTER fit, about the peephole's centre, so FIT keeps fitting the
    scene's own extent and a beat-swell isn't cancelled by it."""
    size, ang, (dx, dy) = mods["size"], mods["angle"], mods["sway"]
    if size == 1.0 and ang == 0.0 and dx == 0.0 and dy == 0.0:
        return fit
    if fit is None:
        c0 = c1 = np.array([W / 2.0, H / 2.0], "f4")
        k = 1.0
    else:
        c0, k, c1 = fit
    ca, sa = math.cos(ang), math.sin(ang)
    M = np.array([[ca, -sa], [sa, ca]], "f4") * (k * size)
    return c0, M, c1 + np.array([dx, -dy], "f4")


class Fit:
    """Keeps the scene INSIDE the peephole (owner 2026-09-30: "can the
    hypercube stay within the mask?"). The frame's lines are scaled about
    the screen centre so their extent lands inside the shape's inscribed
    circle, and moved to the mask's centre. The extent it fits to jumps UP
    at once (a line never pokes out) and eases DOWN over ~3 s, so a turning
    hypercube doesn't pump in and out. Points move before the glow quads
    are built, so the lines keep their thickness."""

    FALL = 3.0

    def __init__(self):
        self.r = None

    def plan(self, radius, mp, W, H, dt):
        """radius = the frame's line extent in px (Lines.radius) -> the
        Lines.array fit (c0, k, c1), or None when there's nothing to do."""
        if not mp["shape"] or mp["fit"] <= 0:
            self.r = None
            return None
        rc = min(max(radius or 0.0, 0.1 * H), math.hypot(W, H) / 2)
        if self.r is None or rc > self.r:
            self.r = rc
        else:
            self.r += (rc - self.r) * (1.0 - math.exp(-dt / self.FALL))
        r_in = max(4.0, (MASK_INRADIUS[mp["shape"]] - mp["soft"]) * mp["size"] * H / 2 * 0.95
                   * mp.get("room", 1.0))
        c0 = np.array([W / 2.0, H / 2.0], "f4")
        c1 = np.array([W / 2.0 * (1 + mp["pos"][0]), H / 2.0 * (1 - mp["pos"][1])], "f4")
        f = mp["fit"]
        k = 1.0 + (r_in / self.r - 1.0) * f
        return c0, k, c0 + (c1 - c0) * f


# ------------------------------------------------------------------- drawing

def rgb(h, l=0.6, a=1.0):
    r, g, b = colorsys.hls_to_rgb((h % 360.0) / 360.0, l, 1.0)
    return (r * a, g * a, b * a)


WEIGHT = [1.0]        # the WEIGHT knob's glow-width factor, set each frame
OLD_LIGHT = [True]    # the scene's Scene.old_light, set each frame
OLD_LIGHT_MAX = 8.0   # cap: a near-zero-length segment would otherwise blow up
LINE_COLS = 13        # a.xy b.xy  pa.xy (before a)  nb.xy (after b)  r g b  radius  flags
LINE_MAX = 10000      # segments a frame (= the old 60000-vertex cap)
# the 6 vertices of every segment's quad: (end: 0 = a, 1 = b, side)
LINE_CORNERS = np.array([[0, 1], [0, -1], [1, 1], [1, 1], [0, -1], [1, -1]], "f4")
# flags: 1 = joined (round caps, corners split), 2 = has a point before a, 4 = after b


def line_quads(rows):
    """CPU twin of LINE_VS's quad (tests + off-screen tools): Lines.array
    rows -> (n * 6, 3) corners [x, y, side], in the order the GPU draws them."""
    a, b, r = rows[:, 0:2], rows[:, 2:4], rows[:, 11:12]
    joined = (rows[:, 12:13].astype(int) & 1) > 0
    d = b - a
    ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-4)[:, None]
    t = d / ln
    n = np.stack([-t[:, 1], t[:, 0]], 1)
    ext = np.where(joined, 1.0, 0.5)
    end, side = LINE_CORNERS[:, 0], LINE_CORNERS[:, 1]
    base = np.where(end[None, :, None] < 0.5, (a - t * r * ext)[:, None], (b + t * r * ext)[:, None])
    pos = base + (n * r)[:, None, :] * side[None, :, None]
    return np.concatenate([pos, np.broadcast_to(side[None, :, None], pos.shape[:2] + (1,))],
                          2).reshape(-1, 3).astype("f4")


def link_joints(R):
    """R = the joined rows of Lines.array (a view, filled in place): for each
    segment end, the far point of the ONE other segment OF THE SAME LINE
    (same colour + width) that shares that end exactly (cols 4:6 before a,
    6:8 after b) + the flags. Shared ends are bit-for-bit equal (cut from the
    same points). Rows in drawing order mostly chain (b_i == a_i+1): one
    compare links those; only the ends left over (a ring's closing corner, a
    contour cell's neighbour) are sorted. Same line only: where a tube ring
    closes, its struts end on the same point (4 ends), and a corner split
    between two widths would show a step. Every numpy call costs ~0.05 ms on
    the Pi, so this stays short (2026-10-24)."""
    n = len(R)
    K = R[:, 0:4].view(np.uint64)                # each end's (x, y) bits as one int
    A = R[:, 8:12].view(np.uint64)               # colour + width bits
    ln_id = A[:, 0] * np.uint64(0x9E3779B97F4A7C15) ^ A[:, 1]   # one int per line look
    ka, kb = K[:, 0], K[:, 1]
    link = (kb[:-1] == ka[1:]) & (ln_id[:-1] == ln_id[1:])
    has_a = np.zeros(n, bool)
    has_b = np.zeros(n, bool)
    has_a[1:] = link
    has_b[:-1] = link
    R[1:, 4:6] = R[:-1, 0:2]                     # the previous segment's a
    R[:-1, 6:8] = R[1:, 2:4]                     # the next segment's b
    ia, ib = np.flatnonzero(~has_a), np.flatnonzero(~has_b)
    if len(ia) + len(ib) >= 2:
        rows = np.concatenate([ia, ib])
        is_b = np.arange(len(rows)) >= len(ia)
        pt = np.concatenate([ka[ia], kb[ib]])
        lid = ln_id[rows]
        keys = pt ^ (lid * np.uint64(0xC2B2AE3D27D4EB4F))     # point + line look, one int
        o = np.argsort(keys, kind="stable")
        eq = keys[o[1:]] == keys[o[:-1]]
        pad = np.concatenate([[False], eq, [False]])
        two = np.flatnonzero(pad[1:-1] & ~pad[:-2] & ~pad[2:])   # exactly two ends meet
        e1, e2 = o[two], o[two + 1]
        keep = ((rows[e1] != rows[e2])           # not a zero-length segment's own ends
                & (pt[e1] == pt[e2]) & (lid[e1] == lid[e2]))     # (and no hash coincidence)
        x = np.concatenate([e1[keep], e2[keep]])
        y = np.concatenate([e2[keep], e1[keep]])
        rx, ry = rows[x], rows[y]
        far = np.where(is_b[y][:, None], R[ry, 0:2], R[ry, 2:4])   # the partner's OTHER end
        xa = ~is_b[x]
        R[rx[xa], 4:6] = far[xa]
        R[rx[~xa], 6:8] = far[~xa]
        has_a[rx[xa]] = True
        has_b[rx[~xa]] = True
    R[:, 12] = 1.0 + 2.0 * has_a + 4.0 * has_b


def line_weight(v):
    """WEIGHT knob (CC 53) -> glow width factor: 0 -> 0.4, 64 -> 1 (the
    scene's own), 127 -> 2.5. owner 2026-09-30, of a first cut that blew
    out to a white blur: "I really liked the overloaded look" -- so it's a
    choice, not a fix."""
    return 2.5 ** ((max(0, min(127, v)) - 64) / 64.0)


class Lines:
    """Collects one frame's lines as plain segments, then hands the GPU one
    row per segment (array()). MEASURED 2026-09-29: a numpy build per add()
    call cost ~99% of a core on tube / warp (hundreds of short lines a
    frame); batched, the per-line cost is a few list appends."""

    def __init__(self):
        self.seg = []          # plain (join=False): (ax, ay, bx, by, r, g, b, radius)
        self.blocks = []       # numpy (n, 8) blocks of the same columns
        self.jseg = []         # joined (the default), same columns
        self.jblocks = []      # joined numpy blocks

    def add_segments(self, ab, col, width, join=True):
        """ab = (n, 4) array of segment ends [ax, ay, bx, by], one colour.
        Joined (default): round ends, and where two segments share an end
        exactly, that corner is split cleanly (link_joints)."""
        if len(ab):
            blk = np.empty((len(ab), 8), "f4")
            blk[:, 0:4] = ab
            blk[:, 4:7] = col
            blk[:, 7] = width * 3.0
            (self.jblocks if join else self.blocks).append(blk)

    def add(self, pts, col, width, closed=False, join=True):
        """Joined (default): the segments meet cleanly at the corners (round
        ends, each corner split along its bisector), so no doubled hot dot
        and no square stubs past a corner; on a curve of short segments
        those dots were the "dotted line" look (owner, 2026-10-24: every
        scene joined, brightness matched with Scene.gain). join=False = the
        old overlapping ends."""
        r = width * 3.0                          # glow reaches 3x the core
        c0, c1, c2 = col
        seg = self.jseg if join else self.seg
        prev = None
        for q in pts:
            if prev is not None:
                seg.append((prev[0], prev[1], q[0], q[1], c0, c1, c2, r))
            prev = q
        if closed and len(pts) > 2:
            q = pts[0]
            seg.append((prev[0], prev[1], q[0], q[1], c0, c1, c2, r))

    def _stack(self):
        # built ONCE per frame: radius() (FIT) and array() both need it; two
        # builds of tube's segment list cost ~1.5 ms a frame (measured).
        # Rows: the plain ones (seg, blocks), then the joined (jseg, jblocks).
        n = (len(self.seg), len(self.blocks), len(self.jseg), len(self.jblocks))
        if getattr(self, "_S_n", None) == n:
            return self._S
        parts = (([np.asarray(self.seg, dtype="f4")] if self.seg else []) + self.blocks
                 + ([np.asarray(self.jseg, dtype="f4")] if self.jseg else []) + self.jblocks)
        S = None if not parts else (np.concatenate(parts) if len(parts) > 1 else parts[0])
        self._S, self._S_n = S, n
        return S

    def radius(self, c0):
        """How far the frame's lines reach from c0 (px), ignoring the
        farthest 5% (a warp streak flying off the screen)."""
        S = self._stack()
        if S is None:
            return 0.0
        P = S[:, 0:4].reshape(-1, 2)
        d2 = (P[:, 0] - c0[0]) ** 2 + (P[:, 1] - c0[1]) ** 2
        k = min(len(d2) - 1, int(0.95 * (len(d2) - 1)))
        return float(np.sqrt(np.partition(d2, k)[k]))      # the 95th percentile, cheaply

    def array(self, fit=None):
        """-> one row per segment (LINE_COLS floats); the GPU builds each
        glow quad from its row (LINE_VS, one instance per segment).
        fit = (c0, k, c1): scale the points about c0 by k and move them to
        c1 (Fit.plan) before the quads are built = same line width. k may
        be a 2x2 matrix (camera(): scale + rotate). MEASURED 2026-10-24:
        spreading every segment into 6 vertices in numpy cost ~3 ms a frame
        on ridgeline; per segment, with the corner maths on the GPU, it's
        a fraction."""
        S = self._stack()
        if S is None:
            return None
        a, b = S[:, 0:2], S[:, 2:4]
        if fit is not None:
            c0, k, c1 = fit
            if np.ndim(k) == 2:
                a = (a - c0) @ k.T + c1
                b = (b - c0) @ k.T + c1
            else:
                a = (a - c0) * k + c1
                b = (b - c0) * k + c1
        m = len(S)
        j0 = m - len(self.jseg) - sum(len(q) for q in self.jblocks)   # joined rows: [j0:]
        out = np.empty((m, LINE_COLS), "f4")
        out[:, 0:2], out[:, 2:4] = a, b
        out[:, 8:11] = S[:, 4:7]
        out[:, 11] = S[:, 7] * WEIGHT[0]
        out[:j0, 4:8] = 0.0
        out[:j0, 12] = 0.0                       # plain: the old overlapping quad
        if j0 < m:
            # link_joints fills a segment's before / after points only where it
            # HAS a neighbour; start them at 0, or the rest keep np.empty's
            # garbage (sometimes NaN: the GPU ignores an unflagged one, but it
            # made "every scene draws finite geometry" fail now and then, 2026-10-24)
            out[j0:, 4:8] = 0.0
            link_joints(out[j0:])                # BEFORE old light: it tells lines apart by colour
            if OLD_LIGHT[0]:
                # the brightness the scene was tuned on: a plain segment's
                # quad ran half a glow past each end, so it gave (length +
                # radius) / length times a joined one's light (doubled at
                # every joint = the dots). Joined + this = the same light,
                # spread evenly.
                dJ = out[j0:, 2:4] - out[j0:, 0:2]
                lJ = np.maximum(np.hypot(dJ[:, 0], dJ[:, 1]), 1e-3)
                out[j0:, 8:11] *= np.minimum(1.0 + out[j0:, 11] / lJ, OLD_LIGHT_MAX)[:, None]
        return out


class Scene:
    decay = 0.80      # how much of the last frame survives (trails)
    zoom = 1.0        # >1 = trails rush outward (tunnel feel)
    spin = 0.0        # trail rotation per frame, radians
    step = 1.0        # 60 fps frames this frame stands for (x speed knob)
    old_light = True  # joined lines keep the old overlapping lines' brightness (Lines.array)
    ctx = None        # SceneCtx: the audio tap + the MIDI state (render loop)

    def __init__(self, w, h):
        self.W, self.H = w, h


class SceneCtx:
    """What a scene may look at besides (f, hue, p): the audio tap and the
    MIDI state. The chord helpers remember the LAST chord held, so a scene
    keeps its shape between chords instead of collapsing."""

    def __init__(self, audio=None, music=None):
        self.audio, self.music = audio, music
        self._last = [48, 52, 55]                 # C major until you play

    def notes(self):
        held = sorted(set(self.music.held.values())) if self.music else []
        if held:
            self._last = held[:6]
        return self._last


class Tunnel(Scene):
    decay, zoom = 0.72, 1.012
    old_light = False   # owner judged the joined tunnel as it is (2026-10-24)

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        n = 4 + int((math.sin(f * .002) + 1) * 2)
        cx = W / 2 + math.sin(f * .007) * W * .12
        cy = H / 2 + math.cos(f * .0053) * H * .13
        for i in range(30):
            z = (i / 30 + f * .004) % 1
            r = z ** 2.4 * W * .95 * (1 + p * .12)
            a = f * .006 + i * .18 + math.sin(f * .003) * z * 2
            ox = cx + (W / 2 - cx) * z
            oy = cy + (H / 2 - cy) * z
            pts = [(ox + math.cos(a + k * TAU / n) * r,
                    oy + math.sin(a + k * TAU / n) * r) for k in range(n)]
            L.add(pts, rgb(hue + i * 9, .45 + z * .25, min(1, z * 3)),
                  .6 + z * 3.5 + p * z * 3, True)


class Mystify(Scene):
    decay, spin = 0.90, 0.002

    def __init__(self, w, h):
        super().__init__(w, h)
        self.v = [[random.random() * .4, random.random() * .4,
                   (random.random() - .5) * .006, (random.random() - .5) * .006]
                  for _ in range(4)]

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        for v in self.v:
            v[0] += v[2] * (1 + p * 2) * self.step
            v[1] += v[3] * (1 + p * 2) * self.step
            if not .02 < v[0] < .48:
                v[2] *= -1
            if not .02 < v[1] < .48:
                v[3] *= -1
        R = H * .95
        for s in range(6):
            a = s * TAU / 6 + f * .002
            ca, sa = math.cos(a), math.sin(a)
            for m in (1, -1):
                pts = [(W / 2 + v[0] * R * ca - v[1] * R * m * sa,
                        H / 2 + v[0] * R * sa + v[1] * R * m * ca)
                       for v in self.v]
                L.add(pts, rgb(hue + s * 20, .55 + p * .2),
                      1.4 + p * 2.5, True)


class Tube(Scene):
    decay, zoom = 0.62, 1.006
    old_light = False   # owner judged the joined tube as it is (2026-10-24)

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        k0 = H / 540.0
        rings, seg = 22, 14
        rs = []
        for i in range(rings):
            z = (i / rings + f * .005) % 1
            d = 1 / (.05 + (1 - z) * 1.2) * (1 + p * .1)
            bx = math.sin(f * .01 + z * 4) * 140 * (1 - z) * k0
            by = math.cos(f * .008 + z * 3) * 90 * (1 - z) * k0
            ring = [(W / 2 + (math.cos(an) * 80 * k0 + bx) * d,
                     H / 2 + (math.sin(an) * 80 * k0 + by) * d)
                    for an in (k * TAU / seg + f * .003 + z * 1.5
                               for k in range(seg))]
            rs.append((z, ring))
        rs.sort()
        for j, (z, ring) in enumerate(rs):
            L.add(ring, rgb(hue + 80 + z * 120, .5, min(1, z * 2)),
                  .5 + z * 2.5 + p * z * 2, True)
            if j + 1 < len(rs):
                nxt = rs[j + 1][1]
                L.add_segments(np.array([ring[k] + nxt[k] for k in range(0, seg, 2)], "f4"),
                               rgb(hue - 20 + z * 100, .5, z * .7), .4 + z * 1.5)


class Harmonograph(Scene):
    decay = 0.985

    def __init__(self, w, h):
        super().__init__(w, h)
        self.i = 0

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        k0 = H / 540.0
        f1 = 2.01 + math.sin(f * .0004) * .01
        pts = [self.last] if getattr(self, "last", None) else []
        for _ in range(max(2, int(60 * self.step))):
            s = self.i * .02
            e = math.exp(-.0012 * (self.i % 6000))
            self.i += 1
            pts.append((W / 2 + e * k0 * (math.sin(f1 * s + f * .001) * 300 +
                                          math.sin(2.99 * s + 1) * 120),
                        H / 2 + e * k0 * (math.sin(3 * s + .5) * 170 +
                                          math.sin(f1 * s + 2) * 80)))
        self.last = pts[-1]
        L.add(pts, rgb(hue + self.i * .05, .55 + p * .25), 1.2 + p * 3)


class Warp(Scene):
    """Hyperspace: streaks fly out of a wandering centre; the kick throws
    them forward."""
    decay, zoom = 0.80, 1.02

    def __init__(self, w, h):
        super().__init__(w, h)
        self.s = [[random.random() * TAU, random.random()] for _ in range(140)]

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        cx = W / 2 + math.sin(f * .004) * W * .08
        cy = H / 2 + math.cos(f * .0031) * H * .08
        R = math.hypot(W, H) * .6
        for st in self.s:
            z0 = st[1]
            st[1] += (.004 + z0 * .02) * (1 + p * 3) * self.step
            if st[1] > 1:
                st[0], st[1] = random.random() * TAU, random.random() * .1
                continue
            z1 = st[1]
            a = st[0] + f * .001
            ca, sa = math.cos(a), math.sin(a)
            r0, r1 = z0 ** 2 * R, z1 ** 2 * R
            L.add([(cx + ca * r0, cy + sa * r0), (cx + ca * r1, cy + sa * r1)],
                  rgb(hue + st[0] * 20, .55 + z1 * .3, min(1, z1 * 4)),
                  .5 + z1 * 2.5)


class Hypercube(Scene):
    """A tesseract turning through 4D, in perspective; the kick punches it
    toward you."""
    decay, spin = 0.82, -0.003

    def __init__(self, w, h):
        super().__init__(w, h)
        self.v = [[(i >> b & 1) * 2 - 1 for b in range(4)] for i in range(16)]
        self.e = [(i, i ^ (1 << b)) for i in range(16) for b in range(4)
                  if i < i ^ (1 << b)]

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        a1, a2, a3 = f * .004, f * .0063, f * .0029
        c1, s1 = math.cos(a1), math.sin(a1)
        c2, s2 = math.cos(a2), math.sin(a2)
        c3, s3 = math.cos(a3), math.sin(a3)
        scale = H * .28 * (1 + p * .25)
        pts, depth = [], []
        for x, y, z, w in self.v:
            x, w = x * c1 - w * s1, x * s1 + w * c1        # xw plane
            y, z = y * c2 - z * s2, y * s2 + z * c2        # yz plane
            x, z = x * c3 - z * s3, x * s3 + z * c3        # xz plane
            k4 = 2.2 / (3.2 - w)                           # 4D -> 3D
            x, y, z = x * k4, y * k4, z * k4
            k3 = 3.0 / (4.0 - z)                           # 3D -> screen
            pts.append((W / 2 + x * k3 * scale, H / 2 + y * k3 * scale))
            depth.append(min(1.0, max(0.15, (w + 1.5) / 3)))
        for i, j in self.e:
            d = (depth[i] + depth[j]) / 2
            L.add([pts[i], pts[j]], rgb(hue + 40 + d * 90, .45 + d * .25, d),
                  .8 + d * 2.5 + p * 1.5)


class Scope(Scene):
    """XY oscilloscope ("oscilloscope music"): the mixer's own stereo audio,
    left against right, rotated 45 degrees (a goniometer: mono = a vertical
    line, stereo width = the figure blooms). Nothing to detect, so no beat
    delay; the picture IS the sound. With no audio yet it draws a Lissajous
    from the chord you hold."""
    decay = 0.55

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        tap = self.ctx.audio if self.ctx else None
        fr = getattr(tap, "frames", None)
        fresh = fr is not None and time.monotonic() - tap.frames_t < 0.5
        if fresh:
            seg = fr[-1600:]                  # the newest ~33 ms
            g = 0.45 / max(tap.env * 1.4, 1e-6)
            if seg.shape[1] >= 2:
                l, r = seg[:, 0], seg[:, 1]
            else:                             # mono ring: x(t) vs x(t + 1 ms)
                l, r = seg[:-48, 0], seg[48:, 0]
            x = (l - r) * g
            y = (l + r) * g
            pts = np.stack([W / 2 + np.clip(x, -1.2, 1.2) * H * .42,
                            H / 2 - np.clip(y, -1.2, 1.2) * H * .42], 1)[::2]
        else:
            n = self.ctx.notes() if self.ctx else [48, 52, 55]
            a, b = 2 ** ((n[1] - n[0]) / 12), 2 ** ((n[-1] - n[0]) / 12)
            t = np.linspace(0, TAU * 4, 700) + f * .01
            pts = np.stack([W / 2 + np.sin(a * t + f * .003) * H * .38,
                            H / 2 + np.sin(b * t) * H * .38], 1)
        ab = np.concatenate([pts[:-1], pts[1:]], 1)
        L.add_segments(ab, rgb(hue + 160, .6 + p * .1), 0.9 + p * 1.2)


class Ridgeline(Scene):
    """Spectrum history as a mountain range ("Unknown Pleasures"): each new
    spectrum is a ridge at the front, older ones recede. HIDDEN-LINE removal
    on the CPU -- the renderer only ADDS light, so a near ridge can't paint
    black over a far one; instead a far ridge is only drawn where it rises
    above every nearer ridge (a skyline swept front to back)."""
    decay = 0.30
    ROWS = 26
    BUCKETS = 160       # 400 cost ~20 ms a frame on the Pi (measured); the
                        # spectrum has 48 bands, so 160 columns lose nothing

    def __init__(self, w, h):
        super().__init__(w, h)
        self.rows, self._spec_id = [], None

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        tap = self.ctx.audio if self.ctx else None
        spec = getattr(tap, "spec", None)
        if spec is not None and id(spec) != self._spec_id and any(v is not None for v in spec):
            self._spec_id = id(spec)
            v = np.array([-90.0 if d is None else d for d in spec])
            self.rows.insert(0, np.clip((v + 72) / 66, 0, 1) ** 1.5)
            del self.rows[self.ROWS:]
        elif not self.rows or (tap is None or not tap.live):
            # no audio: a gentle idle range so the scene isn't empty
            k = np.arange(48)
            self.rows.insert(0, 0.15 + 0.1 * np.sin(k * .4 + f * .05) ** 2)
            del self.rows[self.ROWS:]
        # ALL ridges at once (MEASURED: a loop per ridge cost ~24 ms a frame
        # = 72% of a core once 32 ridges had filled in). Every ridge is
        # sampled AT the skyline's columns (fixed samples left gaps that far
        # ridges showed through as dashes); the skyline is a running minimum
        # of screen-y from the front ridge back.
        R = np.array(self.rows)                   # (rows, bands), FRONT first
        nr, nb = R.shape
        z = np.arange(nr) / self.ROWS
        y0 = H * .92 - z * H * .6
        sx = 1 - z * .45
        amp = H * .32 * (1 - z * .5) * (1 + p * .25 * (1 - z))
        B = self.BUCKETS
        cx = (np.arange(B) + .5) * (W / B)
        fi = ((cx[None, :] - W / 2) / (W * .82 * sx[:, None]) + .5) * (nb - 1)
        inside = (fi >= 0) & (fi <= nb - 1)
        fi = np.clip(fi, 0, nb - 1 - 1e-6)
        i0 = fi.astype(int)
        fr = fi - i0
        ri = np.arange(nr)[:, None]
        rv = R[ri, i0] * (1 - fr) + R[ri, i0 + 1] * fr
        Y = np.where(inside, y0[:, None] - rv * amp[:, None], np.inf)
        ahead = np.vstack([np.full((1, B), np.inf),
                           np.minimum.accumulate(Y, axis=0)[:-1]])
        vis = inside & (Y < ahead - 0.5)
        rr, cc = np.nonzero(vis[:, :-1] & vis[:, 1:])
        if len(rr):
            cols = np.array([rgb(hue + 200 + r * 3, .6, 1 - r / self.ROWS * .75)
                             for r in range(nr)], "f4")
            ab = np.stack([cx[cc], Y[rr, cc], cx[cc + 1], Y[rr, cc + 1]], 1)
            L.add_segments(ab, cols[rr], 1.1)


class Rose(Scene):
    """Chord roses: each held note is a circle spinning at its FREQUENCY
    RATIO to the lowest (a major triad = 1 : 1.26 : 1.5); the tip of the
    chained circles draws the figure. Consonant chords close into simple
    rosettes, dissonant ones knot up."""
    decay = 0.94

    def __init__(self, w, h):
        super().__init__(w, h)
        self.th, self.last = 0.0, None

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        ks = [2 ** ((n - notes[0]) / 12) for n in notes]
        R = H * .42 / sum(1 / (j + 1) for j in range(len(ks)))
        pts = [self.last] if self.last else []
        for _ in range(max(2, int(40 * self.step))):
            self.th += .035
            x = y = 0.0
            for j, k in enumerate(ks):
                a = R / (j + 1) * (1 + p * .06)
                x += a * math.cos(k * self.th * 2 + j)
                y += a * math.sin(k * self.th * 2 + j)
            pts.append((W / 2 + x, H / 2 + y))
        self.last = pts[-1]
        L.add(pts, rgb(hue + (notes[0] % 12) * 30, .58), 1.1 + p)


class Lorenz(Scene):
    """The Lorenz attractor (the chaotic butterfly), one ever-growing path
    turning in 3D. The kick pushes rho up, bending the wings on the beat."""
    decay = 0.975

    def __init__(self, w, h):
        super().__init__(w, h)
        self.q = [0.1, 0.0, 0.0]
        self.a = 0.0

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        rho = 28 + p * 6
        k = H / 540.0
        x, y, z = self.q
        self.a += .004 * self.step
        ca, sa = math.cos(self.a), math.sin(self.a)
        pts = []
        for _ in range(max(2, int(60 * self.step))):
            d = .006
            x, y, z = (x + d * 10 * (y - x), y + d * (x * (rho - z) - y),
                       z + d * (x * y - 8 / 3 * z))
            px = x * ca - y * sa
            pts.append((W / 2 + px * 11 * k, H * .92 - z * 8.5 * k))
        if not all(map(math.isfinite, (x, y, z))) or abs(z) > 500:
            x, y, z = 0.1, 0.0, 0.0              # never let it run away
        self.q = [x, y, z]
        L.add(pts, rgb(hue + 20 + z * 4, .58), 1.1 + p * 1.5)


class Chladni(Scene):
    """Chladni figures: where sand gathers on a vibrating square plate, the
    zero lines of cos(n pi x) cos(m pi y) - cos(m pi x) cos(n pi y) (drifting
    to the + form and back). The mode numbers (n, m) come from the chord's
    intervals, so each chord has its own plate. Drawn as glowing contour
    lines (vectorised marching squares), so it stays in the line style."""
    decay = 0.80
    GX, GY = 120, 68

    def __init__(self, w, h):
        super().__init__(w, h)
        self.mix = 0.0
        u = np.linspace(0, 1, self.GX)
        v = np.linspace(0, 1, self.GY)
        self.U, self.V = np.meshgrid(u, v)
        self._tab = {}

    def _cos(self, k, axis):
        """cos(k pi u) over the grid, cached: the plate only changes when the
        mode numbers do, so the field is two multiplies a frame."""
        key = (k, axis)
        if key not in self._tab:
            self._tab[key] = np.cos(k * math.pi * (self.U if axis == 0 else self.V))
        return self._tab[key]

    @staticmethod
    def contours(Z, sx, sy):
        """Marching squares for the Z = 0 lines -> (n, 4) segments in
        pixels (grid step sx, sy). Saddle cells (4 crossings) join 0-1 / 2-3.
        Only the cells a line passes through are worked on (a few hundred of
        ~8000): MEASURED on the Pi, doing every cell cost 11.4 ms a frame."""
        P = Z > 0
        pa, pb, pc, pd = P[:-1, :-1], P[:-1, 1:], P[1:, 1:], P[1:, :-1]
        gy, gx = np.nonzero((pa != pb) | (pb != pc) | (pc != pd) | (pa != pd))
        if not len(gx):
            return np.zeros((0, 4), "f4")
        a, b = Z[gy, gx], Z[gy, gx + 1]          # top-left, top-right
        c, d = Z[gy + 1, gx + 1], Z[gy + 1, gx]  # bottom-right, bottom-left

        def cross(z0, z1):
            with np.errstate(divide="ignore", invalid="ignore"):
                t = z0 / (z0 - z1)
            return ((z0 > 0) != (z1 > 0)), np.clip(np.nan_to_num(t, nan=.5), 0, 1)
        e = []                                   # the 4 edges: mask, x, y
        m, t = cross(a, b); e.append((m, gx + t, gy + 0.0))       # top
        m, t = cross(b, c); e.append((m, gx + 1.0, gy + t))       # right
        m, t = cross(d, c); e.append((m, gx + t, gy + 1.0))       # bottom
        m, t = cross(a, d); e.append((m, gx + 0.0, gy + t))       # left
        M = np.stack([k[0] for k in e], -1)
        X = np.stack([k[1] for k in e], -1)
        Y = np.stack([k[2] for k in e], -1)
        cnt = M.sum(-1)
        out = []
        two = cnt == 2
        if two.any():
            idx = np.argsort(~M[two], axis=-1, kind="stable")[:, :2]
            xs = np.take_along_axis(X[two], idx, -1)
            ys = np.take_along_axis(Y[two], idx, -1)
            out.append(np.stack([xs[:, 0], ys[:, 0], xs[:, 1], ys[:, 1]], 1))
        four = cnt == 4
        if four.any():
            for i, j in ((0, 1), (2, 3)):
                out.append(np.stack([X[four][:, i], Y[four][:, i],
                                     X[four][:, j], Y[four][:, j]], 1))
        if not out:
            return np.zeros((0, 4), "f4")
        S = np.concatenate(out).astype("f4")
        S[:, 0::2] *= sx
        S[:, 1::2] *= sy
        return S

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        iv = [(n - notes[0]) % 12 for n in notes[1:]] or [4, 7]
        n = 2 + iv[0] % 5
        m = 3 + (iv[-1] if len(iv) > 1 else iv[0] + 3) % 6
        if n == m:
            m += 1
        self.mix = math.sin(f * .004)
        Z = (self._cos(n, 0) * self._cos(m, 1)
             - self.mix * self._cos(m, 0) * self._cos(n, 1))
        side = min(W, H) * .92                   # a square plate, centred
        S = self.contours(Z, side / (self.GX - 1), side / (self.GY - 1) * (self.GY - 1) / (self.GX - 1))
        S[:, 0::2] += (W - side) / 2
        S[:, 1::2] += (H - side * (self.GY - 1) / (self.GX - 1)) / 2
        L.add_segments(S, rgb(hue + n * 30 + m * 12, .55 + p * .15), 0.9 + p * 1.2)


def depth_lines(L, A, B, d, hue, p, width=1.0, light=0.5, buckets=4):
    """Segments A->B (n, 2) coloured by depth d (0 far .. 1 near): a few
    depth buckets, one numpy block each (far = dim + thin, near = bright)."""
    for b in range(buckets):
        lo, hi = b / buckets, (b + 1) / buckets
        m = (d >= lo) & ((d < hi) if b < buckets - 1 else (d <= hi))
        if not m.any():
            continue
        z = (lo + hi) / 2
        a = min(1.0, 0.18 + 0.82 * z)
        L.add_segments(np.concatenate([A[m], B[m]], 1),
                       rgb(hue + z * 90, light + z * 0.2, a), width * (0.6 + 1.6 * z) + p * 1.2)


def rot3(P, ax, ay, az):
    """Rotate (..., 3) points about x, then y, then z."""
    cx, sx, cy, sy, cz, sz = (math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay),
                              math.cos(az), math.sin(az))
    R = (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
         @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
         @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]))
    return P @ R.T


class Knot(Scene):
    """SHAPE MACHINE: a tube swept along a torus knot (the tube, generalised).
    The chord's lowest note picks the knot; the tube's cross-section is a
    superformula from the chord (as many lobes as notes) that breathes
    between round, star and flower. The kick fattens it."""
    decay, spin = 0.86, 0.0025
    N, K = 96, 12
    # first cut (2026-09-30 screenshot) blew out to white: ~1100 bright glow
    # lines overlapping additively. Fewer rings, thinner, dimmer.

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        pk, qk = VS.TORUS_KNOTS[(notes[0] % 12) % len(VS.TORUS_KNOTS)]
        sec = VS.chord_shape(notes, f * 0.0012, k=self.K)
        tube = VS.sweep(VS.torus_knot(pk, qk, n=self.N), sec, 0.17 * (1 + p * 0.5))
        tube = rot3(tube, f * 0.0031, f * 0.0047, f * 0.0019)
        xy, d = VS.project3(tube)
        pts = xy * (H * 0.30) + (W / 2, H / 2)
        n, k = self.N, self.K
        ri = np.arange(0, n, 4)                           # every 4th ring
        A = pts[ri].reshape(-1, 2)
        B = np.roll(pts[ri], -1, 1).reshape(-1, 2)
        dd = ((d[ri] + np.roll(d[ri], -1, 1)) / 2).ravel() * 0.8
        depth_lines(L, A, B, dd, hue + 40, p * 0.5, 0.45, light=0.42)
        cj = np.arange(0, k, 3)                           # every 3rd seam
        A = pts[:, cj].reshape(-1, 2)
        B = np.roll(pts, -1, 0)[:, cj].reshape(-1, 2)
        dd = ((d[:, cj] + np.roll(d, -1, 0)[:, cj]) / 2).ravel() * 0.7
        depth_lines(L, A, B, dd, hue - 30, p * 0.5, 0.4, light=0.38)


class Polytope(Scene):
    """SHAPE MACHINE: the regular 4D polytopes (the hypercube's family: the
    5-cell, tesseract, 16-cell, 24-cell, 600-cell) and duoprisms, turning
    through 4D so they pass through themselves. The chord's lowest note picks
    the shape; a change crossfades over ~1.2 s."""
    decay, spin = 0.84, -0.002
    FADE = 1.2

    def __init__(self, w, h):
        super().__init__(w, h)
        self.cur, self.prev, self.mix = None, None, 1.0
        self.geo = {}

    def _geo(self, name):
        if name not in self.geo:
            self.geo[name] = VS.polytope(name)
        return self.geo[name]

    def _draw_one(self, L, name, f, hue, p, alpha):
        V, E = self._geo(name)
        R = VS.rotate4(V, (f * .0017, 0, f * .004, f * .0053, f * .0023, f * .0031))
        xy, d = VS.project4(R)
        pts = xy * (self.H * .30 * (1 + p * .25)) + (self.W / 2, self.H / 2)
        dd = (d[E[:, 0]] + d[E[:, 1]]) / 2 * alpha
        depth_lines(L, pts[E[:, 0]], pts[E[:, 1]], dd, hue, p, 0.8 if len(E) > 200 else 1.2)

    def draw(self, L, f, hue, p):
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        want = VS.POLYTOPES[(notes[0] % 12) % len(VS.POLYTOPES)]
        if want != self.cur:
            self.prev, self.cur, self.mix = self.cur, want, 0.0
        self.mix = min(1.0, self.mix + self.step / 60.0 / self.FADE)
        if self.prev and self.mix < 1.0:
            self._draw_one(L, self.prev, f, hue, p, 1.0 - self.mix)
        self._draw_one(L, self.cur, f, hue, p, self.mix)


class Hopf(Scene):
    """SHAPE MACHINE: the Hopf fibration -- every point of a sphere is a
    circle in 4D; the circles over each line of latitude fill a torus and
    every one links every other. Turned in 4D and projected, the nested
    tori slide through each other. More notes = more circles per torus."""
    decay, spin = 0.9, 0.0015

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        per = 6 + 2 * min(3, len(notes))
        lats = (-0.75, -0.2, 0.35)
        P = VS.hopf_fibers(lats=lats, per=per, n=48,
                           rot=(0, f * .0021, f * .0037, 0, f * .0029, 0))
        P = rot3(P, f * .0017, f * .0026, 0)
        xy, d = VS.project3(P * 0.55)
        r = np.median(np.hypot(xy[..., 0], xy[..., 1])) + 1e-6
        pts = xy / r * (H * .22 * (1 + p * .2)) + (W / 2, H / 2)
        for i in range(len(lats)):                        # one colour per torus
            sl = slice(i * per, (i + 1) * per)
            A = pts[sl].reshape(-1, 2)
            B = np.roll(pts[sl], -1, 1).reshape(-1, 2)
            dd = ((d[sl] + np.roll(d[sl], -1, 1)) / 2).ravel()
            depth_lines(L, A, B, dd, hue + 20 + i * 100, p * 0.6, 0.5, light=0.45, buckets=3)


class Shape4D(Scene):
    """SHAPE MACHINE: any flat shape pushed out into 4D (four copies joined
    corner to corner -- a square would give a tesseract) and turned inside
    out like the hypercube. The shape is the chord's superformula, breathing;
    or, with a logo picked (LOGO knob), the logo's longest line."""
    decay, spin = 0.83, -0.0025

    def __init__(self, w, h):
        super().__init__(w, h)
        self._opolys, self._oshape = None, None

    def draw(self, L, f, hue, p):
        notes = self.ctx.notes() if self.ctx else [48, 52, 55]
        polys = LOGOS.pick(LOGOS_KNOB[0], time.monotonic()) if LOGOS else None
        if polys and polys is not self._opolys:
            # outline() is pure-Python RDP: once per logo, not every frame.
            # Held by reference (not id()), so a reloaded logo's new list
            # can never be mistaken for the old one.
            self._opolys, self._oshape = polys, np.asarray(vis_logos.outline(polys))
        shape = (self._oshape if polys
                 else VS.chord_shape(notes, f * 0.0009, k=96))    # smooth: 96, not 36
        V, E = VS.extrude_4d(shape, depth=0.35 if polys else 0.6)
        k4 = 4 * len(shape)                               # outlines first, then joins
        R = VS.rotate4(V, (f * .001, f * .0021, f * .0043, f * .0029, f * .0036, 0))
        xy, d = VS.project4(R)
        pts = xy * (self.H * .30 * (1 + p * .25)) + (self.W / 2, self.H / 2)
        dd = (d[E[:, 0]] + d[E[:, 1]]) / 2
        O = E[:k4]
        # only the four OUTLINES are drawn -- the real shape. The joins between
        # the copies read as "a wrestling ring with bolt posts" and owner
        # asked for them gone (2026-09-30); WEIGHT thickens just the shape.
        depth_lines(L, pts[O[:, 0]], pts[O[:, 1]], dd[:k4] * 0.9, hue + 60, p * 0.6, 0.8, light=0.46)


class LogoBank:
    """Logos and line drawings (vis_logos): the LOGO knob (CC 52) picks one
    ("chord" first = none). Files are parsed in a thread (a big SVG can take
    a while) and re-listed every 5 s, so a logo uploaded on /visuals shows
    up without a restart. Each parse (or failure) remembers the file it
    came from (path, mtime, size): a re-upload under the same name, a fixed
    file or a delete is picked up at the next re-list, not only at restart."""

    def __init__(self):
        self.names, self.paths = [], {}
        self.cache, self.failed, self.want = {}, set(), None
        self.sigs = {}                    # name -> file signature it was parsed from
        self._list_t = -99.0
        self._ev = threading.Event()
        threading.Thread(target=self._loader, daemon=True).start()

    @staticmethod
    def _sig(path):
        try:
            st = os.stat(path)
            return (path, st.st_mtime_ns, st.st_size)
        except (OSError, TypeError):
            return None

    def refresh(self, now):
        if now - self._list_t > 5.0:
            self._list_t = now
            lst = vis_logos.logo_list()
            self.names = [n for n, _ in lst]
            self.paths = dict(lst)
            # forget what was parsed from a file that changed or went away
            for name in set(self.cache) | set(self.failed):
                if self.sigs.get(name) != self._sig(self.paths.get(name)):
                    self.cache.pop(name, None)
                    self.failed.discard(name)
                    self.sigs.pop(name, None)

    def pick(self, knob, now):
        """-> the chosen logo's polylines (list of (n, 2) arrays) or None
        (the "chord" choice, or not loaded yet)."""
        self.refresh(now)
        i = bucket(knob, ["chord"] + self.names)
        if i == 0:
            return None
        name = self.names[i - 1]
        got = self.cache.get(name)
        if got is None and name not in self.failed:
            self.want = name
            self._ev.set()
        return got

    def _loader(self):
        while True:
            self._ev.wait()
            self._ev.clear()
            name = self.want
            if not name or name in self.cache:
                continue
            path = self.paths.get(name)
            sig = self._sig(path)                 # BEFORE reading: a newer file re-loads
            try:
                polys = vis_logos.load(path)
                self.sigs[name] = sig
                self.cache[name] = [np.asarray(q, "f4") for q in polys]
            except Exception as e:
                self.sigs[name] = sig
                self.failed.add(name)
                print(f"[visuals] logo {name}: {e}", flush=True)


LOGOS = None                  # the render loop's LogoBank (scenes read it)

FPS_LOG = "/tmp/orc1_visuals_fps.log"


def append_bounded(path, line, max_bytes=64 * 1024, keep=200):
    """Append one line to a log in /tmp, which is tmpfs = RAM on the Pi: past
    max_bytes it is rewritten to its last `keep` lines (a line every 30 s
    used to grow it forever, ~0.6 MB a day). Never raises."""
    try:
        with open(path, "a") as fh:
            print(line, file=fh)
        if os.path.getsize(path) > max_bytes:
            with open(path) as fh:
                tail = fh.readlines()[-keep:]
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                fh.writelines(tail)
            os.replace(tmp, path)
    except OSError:
        pass


class Logo(Scene):
    """A LOGO / line drawing (LOGO knob; the first one when set to "chord"):
    turning gently in 3D, a bright trace running along its strokes, pulsing
    on the beat."""
    decay, spin = 0.86, 0.0

    def __init__(self, w, h):
        super().__init__(w, h)
        self._key, self._cum = None, None

    def draw(self, L, f, hue, p):
        W, H = self.W, self.H
        polys = LOGOS.pick(LOGOS_KNOB[0], time.monotonic()) if LOGOS else None
        if polys is None and LOGOS and LOGOS.names:
            polys = LOGOS.pick(bucket_mid(1, len(LOGOS.names) + 1), time.monotonic())
        if not polys:
            return
        allp = np.concatenate(polys)
        P3 = np.concatenate([allp, np.zeros((len(allp), 1), "f4")], 1)
        P3 = rot3(P3, math.sin(f * .0023) * .35, math.sin(f * .0031) * .9, math.sin(f * .0017) * .15)
        xy, d = VS.project3(P3 * 1.0)
        pts = xy * (H * .36 * (1 + p * .12)) + (W / 2, H / 2)
        # segments within each polyline (not across polyline ends)
        n = np.cumsum([len(q) for q in polys])
        idx = np.arange(len(allp) - 1)
        idx = idx[~np.isin(idx + 1, n[:-1])]
        A, B = pts[idx], pts[idx + 1]
        dd = (d[idx] + d[idx + 1]) / 2 * 0.75 + 0.1
        depth_lines(L, A, B, dd, hue, p * 0.6, 0.8, light=0.45, buckets=3)
        # the trace: the last ~10% of the drawing's length before a moving head
        key = (id(polys), len(idx))
        if key != self._key:
            seg = np.hypot(*(allp[idx + 1] - allp[idx]).T)
            self._cum, self._key = np.cumsum(seg) / max(seg.sum(), 1e-9), key
        head = (f * 0.0025) % 1.0
        m = (self._cum <= head) & (self._cum > head - 0.1)
        if m.any():
            L.add_segments(np.concatenate([A[m], B[m]], 1), rgb(hue + 180, .75, 1.0), 2.2 + p * 2)


def bucket_mid(i, n):
    """The knob value in the middle of range i of n (what a page button sends):
    the middle of the values bucket() maps to i. (i*128+64)//n rounded into
    range i-1 once n > 66, so 30 tiles were unpickable (2026-10-12)."""
    lo, hi = -(-i * 128 // n), -(-(i + 1) * 128 // n) - 1
    return min(127, (lo + hi) // 2)


LOGOS_KNOB = [0]              # the render loop keeps the LOGO knob here


SCENES = {"tunnel": Tunnel, "mystify": Mystify, "tube": Tube,
          "harmonograph": Harmonograph, "warp": Warp, "hypercube": Hypercube,
          "scope": Scope, "ridgeline": Ridgeline, "rose": Rose,
          "lorenz": Lorenz, "chladni": Chladni,
          "knot": Knot, "polytope": Polytope, "hopf": Hopf, "shape4d": Shape4D,
          "logo": Logo}


# -------------------------------------------------------------------- shaders

QUAD_VS = """#version 140
in vec2 p; out vec2 uv;
void main(){ uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0, 1); }"""

FADE_FS = """#version 140
in vec2 uv; out vec4 o;
uniform sampler2D prev; uniform float decay, zoom, spin; uniform vec2 ctr;
void main(){
  vec2 q = uv - ctr;                  // ctr = the peephole's centre (or 0.5)
  float c = cos(spin), s = sin(spin);
  q = mat2(c, -s, s, c) * q / zoom;
  vec3 col = texture(prev, q + ctr).rgb * decay;
  o = vec4(max(col - 0.002, 0.0), 1.0);    // the floor kills ghost haze
}"""

LINE_VS = """#version 140
// one INSTANCE per segment (a Lines.array row); the 6 vertices of its quad
// come from LINE_CORNERS: end (0 = a, 1 = b) and side (+1 / -1). The quad
// is mirrored on the CPU by line_quads().
in float end, side;
in vec2 a, b, pa, nb; in vec3 col; in float rad, flags;
out float d; out vec3 c; out float u, sl, ca, cb; uniform vec2 res;

// a joined corner: the plane through the shared end, square to the bisector
// of the two segments' directions; > 0 = this segment's side. e = the
// neighbour's direction INTO the corner, along the travel. (Here, per
// vertex on the GPU: on the CPU it cost ~1 ms a frame.)
float corner(vec2 e, vec2 t, vec2 n, float along){
  float el = length(e);
  if (el < 1e-6) return 1.0;
  vec2 h = e / el + t;
  float hl = length(h);
  if (hl < 1e-3) return 1.0;           // a hairpin: no clean split, no clip
  h /= hl;
  return along * dot(h, t) + side * dot(h, n);
}

void main(){
  vec2 dd = b - a;
  float ln = max(length(dd), 1e-4);
  vec2 t = dd / ln, n = vec2(-t.y, t.x);
  int fl = int(flags + 0.5);
  bool jn = (fl & 1) != 0;
  float ext = jn ? 1.0 : 0.5;          // plain: half a glow of overlap; joined: a whole round cap
  vec2 pos = (end < 0.5 ? a - t * rad * ext : b + t * rad * ext) + n * rad * side;
  d = side; c = col;
  sl = jn ? ln / rad : 0.0;            // along the segment, in glow radii
  u = jn ? (end < 0.5 ? -1.0 : sl + 1.0) : 0.0;
  ca = (fl & 2) != 0 ? corner(a - pa, t, n, u) : 1.0;
  cb = (fl & 4) != 0 ? -corner(nb - b, t, n, u - sl) : 1.0;
  gl_Position = vec4(pos / res * 2.0 - 1.0, 0, 1) * vec4(1, -1, 1, 1); }"""

LINE_FS = """#version 140
in float d; in vec3 c; in float u, sl, ca, cb; out vec4 o;
void main(){
  // a joined corner: the neighbour draws the far side of the bisector. A
  // 1-px crossfade, not a hard cut: a pixel centred ON the bisector was
  // drawn by both or by neither (float rounding), a 1-px sparkle.
  float w = clamp(0.5 + ca / max(fwidth(ca), 1e-6), 0.0, 1.0)
          * clamp(0.5 + cb / max(fwidth(cb), 1e-6), 0.0, 1.0);
  if (w <= 0.0) discard;
  float e = max(max(-u, u - sl), 0.0); // past a joined end: a round cap (plain lines: 0)
  float x = d * d + e * e;
  float f = exp(-x * 30.0) + 0.28 * exp(-x * 4.0);   // hot core + soft glow
  o = vec4(c * f * w, 1.0);
}"""

BG_FS = """#version 140
// the background, rendered at 1/Composer.BG_DIV resolution (px = full-res
// pixels): an EarthBound-style bent pattern; each ~224th of the screen is one
// "scanline" row whose read is shifted by A*sin(F*row + S*t). Modes 5-9 are
// the Mega Drive fake-3D scroll tricks (see BG_MODES).
out vec4 o;
uniform sampler2D pat;
uniform float t, div;
uniform int bg_mode;
uniform float bg_amp, bg_freq, bg_speed, bg_cycle, bg_hue, rowh;
uniform vec2 res;              // the full screen, px (the skew modes' centre)
uniform float bg_trav;        // the skew modes' scroll so far (BG SPEED x time, + beat pushes)
uniform vec2 bg_tile;          // one tile's size on screen, px (any shape)
uniform float bg_bars;         // RASTER BARS amount: 0 = off
uniform float bg_bars_rows;    // scanline rows per bar
uniform float bg_bars_ph;      // the bars' roll, in bars (the renderer advances it)
uniform float bg_dof;          // DEPTH BLUR: the most blur, in mipmap levels (0 = sharp)
uniform int bg_wrap;           // REPEAT: 0 repeat, 1 mirror, 2 once (BG_WRAPS)
uniform float bg_crisp;        // CRISP: the black/white cut level, 0 = off
uniform int bg_invert;         // INVERT: 1 = black <-> white
uniform float bg_thick;        // THICKEN: extra mipmap levels read before the cut
uniform vec2 bg_texsize;       // the texture's size, texels (THICKEN's footprint)
uniform float bg_sat;          // BG SATURATION: x on the colour's distance from grey (1 = as before)
uniform int bg_pal_n;          // BG PALETTE: 0 = the rainbow, else this many colours in bg_pal
uniform vec3 bg_pal[4];

vec3 hsv(float h, float s, float v) {
  vec3 k = clamp(abs(mod(h * 6.0 + vec3(0.0, 4.0, 2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
  return v * mix(vec3(1.0), k, s);
}

// the background's colour at hue h (wraps) and brightness v. No palette: the
// rainbow at saturation 0.75 x BG SATURATION below the middle (= exactly the
// old colour at 1), and 0.75 .. 1 across the top half (0.75 x 2 would hit 1
// at 2/3 of the knob and the rest would do nothing).
// A palette: a loop through its colours (vis_palettes), smooth between them,
// then BG SATURATION pulls it toward / pushes it from the grey of its brightness.
vec3 bgcol(float h, float v) {
  if (bg_pal_n <= 0)
    return hsv(h, bg_sat <= 1.0 ? 0.75 * bg_sat : min(1.0, 0.75 + 0.25 * (bg_sat - 1.0)), v);
  float x = fract(h) * float(bg_pal_n);
  int i = int(floor(x)) % bg_pal_n, j = (i + 1) % bg_pal_n;
  float f = fract(x);
  f = f * f * (3.0 - 2.0 * f);
  vec3 c = mix(bg_pal[i], bg_pal[j], f);
  float m = max(c.r, max(c.g, c.b));
  return v * max(vec3(m) + (c - vec3(m)) * bg_sat, 0.0);
}

// every pattern read goes through here. The tile has mipmaps (smaller, blurrier
// copies made by the GPU when the tile is loaded): blur = which copy to read,
// 0 = the full-size tile, exactly what the plain read gave before DEPTH BLUR.
// Then the PICTURE LOOK, all off (= the old read) at their defaults:
//   REPEAT  mirror = every other copy flipped (no seam); once = one copy
//           centred on the screen, black outside it (a scrolling mode brings
//           it round again: one copy per screen-and-a-bit, never two at once)
//   THICKEN reads a blurrier copy, where a thin line has spread wider, and
//           cuts it at a lower level: the line comes back fatter
//   CRISP   cuts the grey at a level: pure black / white (the cut softens
//           with DEPTH BLUR, so a blurred picture still looks blurred)
//   INVERT  black <-> white
// tile = the size on screen this read uses (px), e.g. the skyline's smaller one
float samp(vec2 uv, float blur, vec2 tile) {
  vec2 u = uv;
  if (bg_wrap == 2) {
    u = uv - 0.5 * res / tile + 0.5;                     // the screen centre = the picture's
    // the skew/floor/spin/stretch modes add a scroll that grows forever: wrap
    // it, or the one copy slides off and never comes back (2026-10-24 review)
    vec2 span = res / tile + 1.0, m = 0.5 * (span - 1.0);
    u = mod(u + m, span) - m;                            // [0, 1] (no scroll) is unchanged
    if (u.x < 0.0 || u.y < 0.0 || u.x > 1.0 || u.y > 1.0) return 0.0;
  } else if (bg_wrap == 1) {
    u = 1.0 - abs(mod(uv, 2.0) - 1.0);                   // 0..1..0..1: a mirror at every edge
  }
  float b = max(blur, 0.0);
  float cut = bg_crisp > 0.0 ? bg_crisp : (bg_thick > 0.0 ? 0.5 : 0.0);
  // CRISP / THICKEN read the copy where one texel = one background pixel
  // (fp: how many texels a pixel covers, as mipmap levels): a picture shrunk
  // on screen keeps its thin lines whole instead of breaking into dots.
  // THICKEN then reads bg_thick levels blurrier (the line has spread) and
  // cuts lower. With both off: the plain read, as before.
  float fp = cut > 0.0
      ? max(0.0, log2(max(div * bg_texsize.x / tile.x, div * bg_texsize.y / tile.y))) : 0.0;
  float v = textureLod(pat, u, b + fp + bg_thick).r;
  if (cut > 0.0) {
    cut *= exp2(-bg_thick - 0.5 * fp);                   // a spread line is dimmer: cut lower
    float w = 0.02 + 0.06 * b;
    v = smoothstep(cut - w, cut + w, v);
  }
  if (bg_invert == 1) v = 1.0 - v;
  return v;
}

// the 16 px Genesis column x (centre) and the scanline row y (centre) a pixel sits in
float colx(float x) { float w = 16.0 * res.y / 720.0; return (floor(x / w) + 0.5) * w; }
float rowy(float y) { return (floor(y / rowh) + 0.5) * rowh; }

float layer(vec2 px, float amp, float freq, float speed, int mode, vec2 tile, float ph) {
  float row = floor(px.y / rowh);
  float off = amp * sin(freq * row + speed * t + ph);
  vec2 q = px;
  if (mode == 0) q.x += off;
  else if (mode == 1) q.x += (mod(row, 2.0) < 1.0) ? off : -off;
  else if (mode == 2) q.y += off;
  else { q.x += off; q.y += 0.6 * amp * sin(0.7 * freq * row - speed * t + ph); }
  return samp(q / tile, bg_dof, tile);
}

// SKEW WALLS (Batman & Robin's buildings). The screen is cut into 16 px columns;
// the pattern scrolls sideways and is grouped into walls (WALLS knob = BG FREQ
// sets how many across the screen). Each column is slid UP/DOWN by
//   lean * (wall's distance from screen centre) * (column's distance from wall centre)
// so a wall dead centre is flat, walls to the sides lean away, and as a wall
// scrolls across the centre its lean swings from one way to the other.
// DEPTH BLUR: the more a wall leans (the further away it is), the blurrier.
float walls(vec2 px, float amp, float freq, float speed, vec2 tile) {
  float s = bg_trav * tile.x * 0.15;              // the sideways scroll, px
  float n = 1.0 + freq * 20.0;                    // walls across the screen
  float P = res.x / n;                            // one wall's width, px
  // the 16 px columns ride WITH the scroll (cut in world x, not screen x):
  // cut on the screen, each column flipped between wall and gap all at once,
  // so the black gaps jumped 8 px every few frames while the pattern glided
  // (MEASURED 2026-10-24: gap edges moved on 30 of 89 frames, in 4 px steps)
  float cw = 16.0 * res.y / 720.0;
  float wx = (floor((px.x + s) / cw) + 0.5) * cw; // this column's centre in world x (still chunky)
  float wi = floor(wx / P);
  float xl = wx - (wi + 0.5) * P;                 // the column's offset in its wall, -P/2 .. P/2
  if (abs(xl) > 0.42 * P) return 0.0;             // a black gap between walls
  // the wall's centre vs the screen centre, -1 .. 1 (a wall half off-screen
  // leans no harder than one at the edge, or its columns alias to mush)
  float k = clamp(((wi + 0.5) * P - s - 0.5 * res.x) / (0.5 * res.x), -1.0, 1.0);
  float lean = amp / (40.0 * res.y / 720.0);      // BG WARP: 64 ~ a 3:4 slope at the edge
  vec2 q = vec2(px.x + s, px.y + lean * k * xl);
  return samp(q / tile, bg_dof * abs(k), tile);
}

// SKEW FLOOR (line-scroll parallax). The horizon is the screen's middle; a
// floor below and (ceiling = true) a ceiling above. Each scanline row is a
// depth: rows far from the horizon are near and slide fast, rows by the horizon
// slide slowly and fade to black. BG WARP bends the far end sideways (a road
// curving away), BG FREQ sets how fast the curve wanders. DEPTH BLUR: far rows
// blur, the rows nearest you stay sharp.
float floorlines(vec2 px, float amp, float freq, float speed, vec2 tile, bool ceiling) {
  float y = rowy(px.y);                           // one depth per row (chunky scanlines)
  if (!ceiling && y > 0.5 * res.y) return 0.0;    // (GL's y runs up: above the horizon)
  float d = abs(y - 0.5 * res.y) / (0.5 * res.y); // 0 at the horizon .. 1 at the edge
  if (d < 0.08) return 0.0;                       // shortcut: the fade below is 0 here anyway
  float z = 1.0 / d;                              // depth: far = big
  float s = bg_trav * tile.x * 0.3;               // the near rows' scroll, px
  float bend = amp * 2.0 * (1.0 - d) * (1.0 - d) * sin(freq * 4.0 * t);   // the road curves far away
  // every row moves the same WORLD distance s, but a far row's pixels cover
  // more world (x 0.5 * z), so on screen the near rows move fastest = parallax
  vec2 q = vec2((px.x - 0.5 * res.x + bend) * 0.5 * z + s, 0.25 * res.y * z);
  // fade to black toward the horizon: far rows squeeze many tiles into one
  // pixel and turn to moire, so they never show at full brightness
  return samp(q / tile, bg_dof * (1.0 - d), tile) * smoothstep(0.08, 0.45, d);
}

// FLOOR + SKYLINE: the floor (no ceiling), and above the horizon a row of
// buildings filled with the pattern (its windows), scrolling at a quarter of
// the nearest floor's speed: a far layer = a second parallax layer. Building
// heights are a fixed pseudo-random per building; the sky stays black.
float skyline(vec2 px, float amp, float freq, float speed, vec2 tile) {
  if (px.y < 0.5 * res.y) return floorlines(px, amp, freq, speed, tile, false);
  float yh = rowy(px.y) - 0.5 * res.y;            // height above the horizon, px
  float s = bg_trav * tile.x * 0.3 * 0.25;        // the far layer: a quarter of the near floor
  float bw = 0.07 * res.x;                        // one building's width
  float cw = 16.0 * res.y / 720.0;
  float bx = (floor((px.x + s) / cw) + 0.5) * cw; // world-anchored columns (see walls): no gap jumps
  float bi = floor(bx / bw);
  float hsh = fract(sin(bi * 12.9898) * 43758.5453);          // the same height every time
  float h = (0.15 + 0.55 * hsh) * 0.5 * res.y;
  if (bx - bi * bw < 0.12 * bw || yh > h) return 0.0;         // gap between buildings / sky
  return 0.8 * samp(vec2(bx, yh) / (tile * 0.35), bg_dof, tile * 0.35);      // the farthest layer: the full blur
}

// SPIN: the two scroll tables again, used for a ROTATION (a rotation is three
// shears in a row: sideways by row, up/down by column, sideways by row). Each
// shear is chunky (whole scanline rows, whole 16 px columns), so it turns like
// a Genesis faking it. It rocks: BG WARP = how far (up to ~1 radian),
// BG FREQ = how fast it rocks, BG SPEED = a sideways drift underneath.
float spin(vec2 px, float amp, float freq, float speed, vec2 tile) {
  float a = clamp(amp / (30.0 * res.y / 720.0), 0.0, 3.0) * 0.35 * sin(freq * 10.0 * t);
  float ta = tan(0.5 * a), sa = sin(a);
  float x1 = px.x - 0.5 * res.x - ta * (rowy(px.y) - 0.5 * res.y);       // shear 1: by row
  float y2 = px.y - 0.5 * res.y + sa * (colx(x1 + 0.5 * res.x) - 0.5 * res.x);   // shear 2: by column
  float x3 = x1 - ta * (rowy(y2 + 0.5 * res.y) - 0.5 * res.y);           // shear 3: by row
  vec2 q = vec2(x3 + 0.5 * res.x + bg_trav * tile.x * 0.15, y2 + 0.5 * res.y);
  return samp(q / tile, bg_dof, tile);
}

// STRETCH (the Batwing stage's buildings stretching down to the street): every
// scanline row reads a source row, and toward the bottom of the screen the
// same source rows repeat, so the pattern stretches downward. It pumps:
// BG WARP = how much, BG FREQ = how fast it pumps, BG SPEED = sideways scroll.
float stretch(vec2 px, float amp, float freq, float speed, vec2 tile) {
  float u = 1.0 - rowy(px.y) / res.y;             // 0 = the top of the screen .. 1 = the bottom
  float k = clamp(amp / (30.0 * res.y / 720.0), 0.0, 4.0) * (0.6 + 0.4 * sin(freq * 10.0 * t));
  float us = 1.0 - pow(1.0 - u, 1.0 + k);         // flat near the bottom = rows repeat there
  // + a slow climb, so the stretched bottom rows keep cycling through the
  // pattern instead of sitting on one repeated row (a mortar line = a black band)
  vec2 q = vec2(px.x + bg_trav * tile.x * 0.15, (1.0 - us) * res.y + bg_trav * tile.y * 0.05);
  return samp(q / tile, bg_dof, tile);
}

void main(){
  vec2 px = gl_FragCoord.xy * div;
  float v;
  if (bg_mode == 5)
    v = walls(px, bg_amp, bg_freq, bg_speed, bg_tile);
  else if (bg_mode == 6)
    v = floorlines(px, bg_amp, bg_freq, bg_speed, bg_tile, true);
  else if (bg_mode == 7)
    v = spin(px, bg_amp, bg_freq, bg_speed, bg_tile);
  else if (bg_mode == 8)
    v = stretch(px, bg_amp, bg_freq, bg_speed, bg_tile);
  else if (bg_mode == 9)
    v = skyline(px, bg_amp, bg_freq, bg_speed, bg_tile);
  else if (bg_mode == 3)
    v = 0.5 * (layer(px, bg_amp, bg_freq, bg_speed, 0, bg_tile, 0.0)
             + layer(px, bg_amp * 0.7, bg_freq * 1.3, -bg_speed * 0.8, 1, bg_tile * 1.5, 1.7));
  else
    v = layer(px, bg_amp, bg_freq, bg_speed, bg_mode == 4 ? 3 : bg_mode, bg_tile, 0.0);
  float hue = bg_hue + v * 0.35 + t * bg_cycle;
  // RASTER BARS (the copper-bar trick: a new colour on every scanline band).
  // Bands of bg_bars_rows rows each get their own hue step and a bright-middle
  // gradient; bg_bars_ph rolls them. At bg_bars 0 this changes nothing.
  float ph = floor(floor(px.y / rowh) / 2.0) * 2.0 / bg_bars_rows + bg_bars_ph;
  hue += bg_bars * floor(ph) * 0.13;
  float lit = mix(1.0, 0.2 + 0.8 * sin(3.14159265 * fract(ph)), bg_bars);
  o = vec4(bgcol(hue, v * lit), 1.0);   // gaps stay black
}"""

MASK_FS = """#version 140
// the peephole, drawn into a texture only when its settings change (per
// pixel every frame, the star alone cost 8 ms on the Pi)
out vec4 o;
uniform vec2 res, mask_pos;
uniform int mask_shape;
uniform float mask_size, mask_soft;

// signed distances (negative inside), p in units of half the screen height
float sd_shape(int s, vec2 p) {
  if (s == 1) return length(p) - 1.0;
  if (s == 2) {                                  // rounded rectangle, screen-shaped
    vec2 b = vec2(res.x / res.y, 1.0) - 0.25;
    vec2 q = abs(p) - b;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - 0.25;
  }
  if (s == 3) {                                  // hexagon
    const vec3 k = vec3(-0.866025404, 0.5, 0.577350269);
    p = abs(p);
    p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
    p -= vec2(clamp(p.x, -k.z * 1.0, k.z * 1.0), 1.0);
    return length(p) * sign(p.y);
  }
  if (s == 4) return (abs(p.x) + abs(p.y) - 1.0) * 0.7071;   // diamond
  if (s == 5) {                                  // triangle, point up
    const float k = 1.732050808;
    p.x = abs(p.x) - 1.0;
    p.y = p.y + 1.0 / k;
    if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    p.x -= clamp(p.x, -2.0, 0.0);
    return -length(p) * sign(p.y);
  }
  // star (5 points)
  float an = 3.141593 / 5.0, en = 3.141593 / 2.4;
  vec2 acs = vec2(cos(an), sin(an)), ecs = vec2(cos(en), sin(en));
  float bn = mod(atan(p.x, p.y), 2.0 * an) - an;
  p = length(p) * vec2(cos(bn), abs(sin(bn)));
  p -= acs;
  p += ecs * clamp(-dot(p, ecs), 0.0, acs.y / ecs.y);
  return length(p) * sign(p.x);
}

void main(){
  vec2 p = (gl_FragCoord.xy - res * 0.5 * (1.0 + mask_pos)) / (res.y * 0.5 * mask_size);
  float d = sd_shape(mask_shape, p);
  o = vec4(1.0 - smoothstep(-mask_soft, mask_soft, d), 0.0, 0.0, 1.0);
}"""

SHOW_FS = """#version 140
in vec2 uv; out vec4 o;
uniform sampler2D img, bgt, mskt;
uniform float flash, gain, bg_amt;
uniform int use_mask;
void main(){
  vec3 lines = texture(img, uv).rgb * gain;
  vec3 col = lines;
  if (bg_amt > 0.001) {
    float lum = max(lines.r, max(lines.g, lines.b));
    col += texture(bgt, uv).rgb * bg_amt * 0.55 * (1.0 - clamp(lum * 1.5, 0.0, 1.0));  // lines punch through
  }
  col = col / (1.0 + col * 0.35) + flash * 0.06;     // soft highlight rolloff
  if (use_mask > 0) col *= texture(mskt, uv).r;      // outside the peephole = black
  o = vec4(col, 1.0);
}"""


class Composer:
    """The last stages of a frame, split so the costly parts run rarely or
    small. MEASURED 2026-09-30 on the Pi's V3D at 1280x720: computing the star
    mask per pixel every frame cost 8 ms, the full-res background 5.5 ms, so
    tube + star + background ran at 46 fps. Now:
      * the MASK is drawn into a texture only when its settings change;
      * the BACKGROUND renders at 1/BG_DIV resolution (1/4 of the pixels;
        EarthBound itself was 256x224) and is smoothed up;
      * SHOW just composites: lines + background + mask, three lookups.
    `target` lets the tests render off-screen through the very same code."""

    BG_DIV = 2

    def __init__(self, ctx, W, H):
        self.ctx, self.W, self.H = ctx, W, H
        quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], "f4"))
        self.show = ctx.program(vertex_shader=QUAD_VS, fragment_shader=SHOW_FS)
        self.bgp = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BG_FS)
        self.maskp = ctx.program(vertex_shader=QUAD_VS, fragment_shader=MASK_FS)
        self.show_va = ctx.vertex_array(self.show, [(quad, "2f", "p")])
        self.bg_va = ctx.vertex_array(self.bgp, [(quad, "2f", "p")])
        self.mask_va = ctx.vertex_array(self.maskp, [(quad, "2f", "p")])
        self.mask_tex = ctx.texture((W, H), 1)
        self.mask_fbo = ctx.framebuffer([self.mask_tex])
        bw, bh = max(1, W // self.BG_DIV), max(1, H // self.BG_DIV)
        self.bg_tex = ctx.texture((bw, bh), 3)
        self.bg_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.bg_tex.repeat_x = self.bg_tex.repeat_y = False
        self.bg_fbo = ctx.framebuffer([self.bg_tex])
        self.bgp["pat"].value = 0
        self.bgp["div"].value = float(self.BG_DIV)
        self.bgp["rowh"].value = H / 224.0            # EarthBound's 224 scanlines
        self.bgp["res"].value = (float(W), float(H))  # the skew modes need the screen centre
        self.maskp["res"].value = (W, H)
        self.show["img"].value = 0
        self.show["bgt"].value = 1
        self.show["mskt"].value = 2
        self.mask_key = None
        self.use_mask = 0
        self.amt = 0.0

    def set_mask(self, shape, size, soft, pos):
        """-> True when the mask texture was redrawn."""
        key = (int(shape), round(size, 4), round(soft, 4), (round(pos[0], 4), round(pos[1], 4)))
        if key == self.mask_key:
            return False
        self.mask_key = key
        self.use_mask = 1 if shape > 0 else 0
        if shape > 0:
            m = self.maskp
            m["mask_shape"].value = int(shape)
            m["mask_size"].value = float(size)
            m["mask_soft"].value = float(soft)
            m["mask_pos"].value = (float(pos[0]), float(pos[1]))
            self.mask_fbo.use()
            self.ctx.disable(moderngl.BLEND)
            self.mask_va.render(moderngl.TRIANGLE_STRIP)
        return True

    def background(self, pat_tex, amt, mode, amp, freq, speed, tile, cycle, hue, t,
                   bars=0.0, bars_rows=16.0, bars_ph=0.0, dof=0.0,
                   wrap=0, crisp=0.0, invert=0, thick=0.0, sat=1.0, pal=None, trav=None):
        """Render the background (small) when it shows at all. sat =
        sat_mult(BG SATURATION); pal = a palette's (r, g, b) list or None.
        trav = the skew modes' scroll so far (None = t * speed, steady)."""
        self.amt = amt if (pat_tex is not None and amt > 0.001) else 0.0
        if not self.amt:
            return
        g = self.bgp
        g["bg_mode"].value = int(mode)
        g["bg_amp"].value = float(amp)
        g["bg_freq"].value = float(freq)
        g["bg_speed"].value = float(speed)
        g["bg_tile"].value = (float(tile[0]), float(tile[1]))
        g["bg_cycle"].value = float(cycle)
        g["bg_hue"].value = float(hue)
        g["t"].value = float(t)
        g["bg_trav"].value = float(t * speed if trav is None else trav)
        g["bg_bars"].value = float(bars)
        g["bg_bars_rows"].value = float(bars_rows)
        g["bg_bars_ph"].value = float(bars_ph)
        g["bg_dof"].value = float(dof)
        g["bg_wrap"].value = int(wrap)
        g["bg_crisp"].value = float(crisp)
        g["bg_invert"].value = int(invert)
        g["bg_thick"].value = float(thick)
        g["bg_texsize"].value = tuple(float(x) for x in pat_tex.size)
        g["bg_sat"].value = float(sat)
        pal = list(pal or [])[:4]
        g["bg_pal_n"].value = len(pal)
        if pal:
            g["bg_pal"].value = [tuple(float(x) for x in c) for c in pal + [pal[-1]] * (4 - len(pal))]
        self.bg_fbo.use()
        self.ctx.disable(moderngl.BLEND)
        pat_tex.use(0)
        self.bg_va.render(moderngl.TRIANGLE_STRIP)

    def show_frame(self, img_tex, flash, gain, target=None):
        (target or self.ctx.screen).use()
        self.ctx.disable(moderngl.BLEND)
        img_tex.use(0)
        self.bg_tex.use(1)
        self.mask_tex.use(2)
        sh = self.show
        sh["flash"].value = float(flash)
        sh["gain"].value = float(gain)
        sh["bg_amt"].value = float(self.amt)
        sh["use_mask"].value = self.use_mask
        self.show_va.render(moderngl.TRIANGLE_STRIP)


# ----------------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    secs, fps = 60.0, 30
    if "--fps" in args:
        i = args.index("--fps")
        fps = int(args[i + 1])
        del args[i:i + 2]
    if "--secs" in args:
        i = args.index("--secs")
        secs = float(args[i + 1])
        del args[i:i + 2]
    hold = args[0] if args and args[0] in SCENES else None
    order = [hold] if hold else list(SCENES)
    fps = conf_fps(fps)                   # the /visuals page's FPS choice

    pygame.display.init()          # display ONLY (not pygame.init(): see top)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 1)
    screen = pygame.display.set_mode(
        (0, 0), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    W, H = screen.get_size()
    try:
        pygame.display.set_caption("ORC visuals")
    except Exception:
        pass

    ctx = moderngl.create_context(require=310, libgl="libGL.so.1")
    try:
        refresh = float(pygame.display.get_current_refresh_rate()) or 60.0
    except Exception:
        refresh = 60.0

    def pace(fps):
        """-> the swap interval in force (0 = sleep pacing)."""
        n = vsync_plan(fps, refresh)
        if n and set_swap_interval(n):
            return n
        set_swap_interval(0)
        return 0
    vsync = pace(fps)
    print(f"[visuals] {refresh:.2f} Hz monitor, {fps} fps, "
          f"{'vsync every %d refresh(es)' % vsync if vsync else 'no vsync (sleep pacing)'}",
          flush=True)
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], "f4"))
    fade = ctx.program(vertex_shader=QUAD_VS, fragment_shader=FADE_FS)
    line = ctx.program(vertex_shader=LINE_VS, fragment_shader=LINE_FS)
    line["res"].value = (W, H)
    fade_va = ctx.vertex_array(fade, [(quad, "2f", "p")])

    # ping-pong accumulation buffers (RGBA16F keeps the long trails smooth)
    tex = [ctx.texture((W, H), 4, dtype="f2") for _ in range(2)]
    for t in tex:
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        t.repeat_x = t.repeat_y = False
    fbo = [ctx.framebuffer([t]) for t in tex]
    for fb in fbo:
        fb.clear(0, 0, 0, 1)

    vbo = ctx.buffer(reserve=LINE_COLS * 4 * LINE_MAX, dynamic=True)
    line_va = ctx.vertex_array(line, [
        (ctx.buffer(LINE_CORNERS.tobytes()), "1f 1f", "end", "side"),
        (vbo, "2f 2f 2f 2f 3f 1f 1f /i", "a", "b", "pa", "nb", "col", "rad", "flags")])

    # background pattern tile: one grey texture, re-made when the pattern (and
    # maybe its shape) changes. Tiles come from vis_patterns' sets.
    PAT_N = 256
    pat = {"tex": None, "key": None, "units": (1.0, 1.0)}

    def pat_upload(entry):
        data, (cols, rows), units = entry
        t = pat["tex"]
        if t is None or t.size != (cols, rows):
            if t is not None:
                t.release()
            t = ctx.texture((cols, rows), 1, dtype="f1", alignment=1)
            t.repeat_x = t.repeat_y = True
            pat["tex"] = t
        t.write(data)
        t.build_mipmaps()               # the blurrier copies DEPTH BLUR reads (cheap, per tile change)
        t.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        pat["units"] = units
    sets = vis_patterns.pattern_sets(BG_PATTERNS)
    tile_want = queue.Queue()
    tile_failed = set()
    tile_asked = set()                  # queued or loaded: ask once, not per frame
    # every tile is built OFF the frame loop: truchet alone takes ~80 ms on
    # the Pi (a visible hitch, measured), a file tile needs rsvg + a PNG load.
    # Built-ins are made at start; a file tile when first picked. The cache
    # keeps the built-ins + the last TILE_KEEP file tiles (memory cap 200M).
    pat_cache = {}
    TILE_KEEP = 16
    PIC_KEEP = 4                        # pictures held in memory at once (<= 4 MB each)
    max_tex = int(ctx.info.get("GL_MAX_TEXTURE_SIZE", 2048) or 2048)
    cust = {"t": -99.0, "sig": vis_patterns.custom_sig(), "pal": vis_palettes.sig()}
    pals = vis_palettes.as_rgb()        # the palette SLOTS (re-read when /visuals saves them)

    def _build_patterns():
        for i, n in enumerate(BG_PATTERNS):
            g = make_pattern(n, PAT_N)
            pat_cache[(0, i)] = ((g[::-1] * 255).astype("u1").tobytes(), (PAT_N, PAT_N), None)
        while True:
            key = tile_want.get()
            if key in pat_cache or key in tile_failed:
                continue
            _, folder, names = sets[key[0]]
            if key[1] >= len(names):
                continue                      # the CUSTOM set shrank meanwhile
            slug = names[key[1]]
            try:
                g, units = vis_patterns.load_tile(folder, slug)
            except Exception as e:
                with pat_lock:
                    tile_failed.add(key)
                print(f"[visuals] pattern {folder}/{names[key[1]]}: {e}", flush=True)
                continue
            try:
                g = fit_texture(g, max_tex)   # a picture bigger than the GPU allows: shrink it
                # image rows run top-down, a texture's bottom-up: flip, so a
                # skull tile isn't upside down. Pictures arrive as bytes already.
                g8 = g if g.dtype == np.uint8 else (g * 255).astype("u1")
                entry = (g8[::-1].tobytes(), (g.shape[1], g.shape[0]), units)
                del g, g8
                pic_set = len(sets) - 1       # PICTURES is always the last set
                is_pic = key[0] == pic_set
                with pat_lock:                # the frame loop also prunes pat_cache
                    for old in cache_evict(pat_cache, TILE_KEEP - (0 if is_pic else 1),
                                           PIC_KEEP - (1 if is_pic else 0), pic_set):
                        tile_asked.discard(old)
                    now_names = sets[key[0]][2]
                    if key[1] >= len(now_names) or now_names[key[1]] != slug:
                        tile_asked.discard(key)   # an upload/delete moved it: ask again
                        continue
                    pat_cache[key] = entry
            except Exception as e:            # never let this thread die: no tile would load again
                print(f"[visuals] pattern {folder}/{slug}: {e}", flush=True)
    pat_lock = threading.Lock()
    threading.Thread(target=_build_patterns, daemon=True).start()
    comp = Composer(ctx, W, H)
    presence = Presence()
    fitter = Fit()
    global LOGOS
    LOGOS = LogoBank()
    t0 = time.monotonic()
    bg_clock = 0.0
    bars_ph = 0.0              # the RASTER BARS' roll (in bars)
    bg_trav = 0.0              # the newer bend modes' scroll so far (beats push it)
    lines_ph = 0.0             # the lines' LINES CYCLE hue turn (degrees)

    music = Music()
    threading.Thread(target=midi_thread, args=(music,), daemon=True).start()          # synth bus: hits, notes
    threading.Thread(target=midi_thread, args=(music,), daemon=True,                  # the ORC: our knobs
                     kwargs={"client": CTL_CLIENT, "find": find_orc_port,
                             "is_src": lambda pname: pname == ORC_PORT}).start()

    state = {"next": False, "reload": False, "shot": False}
    # SIGWINCH = save the finished frame to SHOT_PNG (visuals.sh shot): an X
    # screen grab of a full-screen GL window returns a stale image (measured)
    signal.signal(signal.SIGWINCH, lambda *_: state.update(shot=True))
    signal.signal(signal.SIGUSR1, lambda *_: state.update(next=True))
    # SIGUSR2 = re-read ~/.orc1_visuals (visuals.sh set): change scene / cycle
    # time IN PLACE. A restart flashed X's cursor and cost a GL teardown.
    signal.signal(signal.SIGUSR2, lambda *_: state.update(reload=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    names = list(SCENES)
    si = 0
    scene = SCENES[order[si]](W, H)
    print(f"[visuals] {W}x{H}, scene {order[si]}", flush=True)
    scene_t0 = time.monotonic()
    clock = pygame.time.Clock()
    f = 0.0
    hue = music.hue
    cur = 0
    frames, rep_t = 0, time.monotonic()
    late, worst = 0, 0.0
    cpu_sum, cpu_max, swap_sum = 0.0, 0.0, 0.0   # frame cost, for the fps log
    jit = []                     # |measured frame time - the refresh step it stands for|
    last = rep_t
    phase = {}                          # the last frame's time per step (the hitch log)
    cc_hold = None           # a CC 20 pick overrides cycling until CC 20 = 0
    audio = AudioTap()
    aud = {"lvl": 0.0, "beat_t": -99.0, "beat2_t": -99.0, "pub_t": 0.0}
    apulse = AudioPulse()
    bpulse = AudioPulse()          # band 2's beats (the background, when band2_on)
    mpulse = AudioPulse()          # the MIDI kicks, same ATTACK / RELEASE (the mods)
    seen_kick = music.kick_t
    tempo, mlfo = Tempo(), ModLFO()
    sctx = SceneCtx(audio, music)
    save_t, saved = 0.0, None
    saver = StateSaver()

    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
        now = time.monotonic()

        if state["reload"]:
            state["reload"] = False
            try:
                with open(CONF) as fh:
                    lines = fh.read().splitlines()
            except OSError:
                lines = ["orc"]
            mode = lines[0].strip() or "orc"
            fps = conf_fps(fps)
            vsync = pace(fps)
            if mode == "orc":
                hold, order = None, list(SCENES)
                if len(lines) > 1 and lines[1].strip().isdigit() and int(lines[1]) > 0:
                    secs = float(lines[1])
                si, scene_t0, cc_hold = -1, now - 1e9, None   # step on below
            elif mode.startswith("orc:") and mode[4:] in SCENES:
                hold, order, si, cc_hold = mode[4:], [mode[4:]], 0, None
                scene, scene_t0 = SCENES[hold](W, H), now
                print(f"[visuals] scene {hold} (held)", flush=True)
            else:
                return                  # classic / an XScreenSaver name: the
                                        # session loop starts that instead

        # CC 20: 0 = back to cycling, otherwise a scene by value range
        req, music.scene_req = music.scene_req, None
        if req is not None:
            cc_hold = None if req == 0 else names[min(len(names) - 1,
                                                      (req - 1) * len(names) // 127)]
            if cc_hold and cc_hold != type(scene).__name__.lower():
                scene, scene_t0 = SCENES[cc_hold](W, H), now
                print(f"[visuals] scene {cc_hold} (knob)", flush=True)
        cycling = not hold and cc_hold is None
        if state["next"] or (cycling and now - scene_t0 > secs):
            state["next"] = False
            cc_hold = None
            si = (si + 1) % len(order)
            scene = SCENES[order[si]](W, H)
            scene_t0 = now
            print(f"[visuals] scene {order[si]}", flush=True)
        name = type(scene).__name__.lower()

        # knobs (64 = the scene's own setting)
        kn = {KNOBS[cc]: (v - 64) / 64.0 for cc, v in music.knobs.items()}
        speed_k = 2.0 ** (kn["speed"] * 2)                  # x0.25 .. x4
        beat_k = music.knobs[26] / 64.0                     # 0 .. 2

        raw_dt = now - last               # what really happened (the log)
        dt = min(frame_dt(raw_dt, refresh, vsync), 0.1)
        last = now
        if raw_dt > 0.09 and frames > 2:
            # one line per visible hitch, with where the LAST frame's time went
            # (2026-10-24: the 30 s summary couldn't say what a 500 ms stall was)
            print(f"[visuals] hitch {time.strftime('%H:%M:%S')} {raw_dt * 1000:.0f} ms: "
                  + " ".join(f"{k} {v * 1000:.0f}" for k, v in phase.items())
                  + f" other {(raw_dt - sum(phase.values())) * 1000:.0f}", flush=True)

        # audio (the mixer's master, read off skipback's ring). owner
        # 2026-09-29: the first cut was "very jumpy, even at the lowest
        # setting" -- beats were an INSTANT full-size step on nearly every
        # eighth note, loudness was barely smoothed, and treble bursts
        # flickered the whole screen. Now: beats swell in (~60 ms) and fade
        # (~350 ms) through AudioPulse, loudness glides over ~0.4 s, no audio
        # screen flash at all, and the knob is a curve (audio_amount).
        audio_k = audio_amount(music.knobs[28])
        apulse.attack = audio_attack(music.knobs[29])
        apulse.release = audio_release(music.knobs[30])
        audio.ONSET = audio_onset(music.knobs[31])
        audio.gate = audio_gate(music.knobs[54])
        audio.set_band(cc_to_hz(music.knobs[32]), cc_to_hz(music.knobs[33]))
        audio.band2_on = band2_on(music.knobs)
        audio.ONSET2 = audio_onset(music.knobs[61])
        audio.set_band2(cc_to_hz(music.knobs[59]), cc_to_hz(music.knobs[60]))
        bpulse.attack, bpulse.release = apulse.attack, apulse.release
        # always listen (~1 ms), so the page's spectrum works with AUDIO off
        a_beat, a_beat2, a_lvl = audio.update(now)
        if a_beat > 0:
            aud["beat_t"] = now
            if audio_k > 0:
                apulse.hit(0.5 + 0.5 * a_beat)
        if a_beat2 > 0:
            aud["beat2_t"] = now
            if audio_k > 0:
                bpulse.hit(0.5 + 0.5 * a_beat2)
        if audio_k > 0 and a_lvl is not None:
            aud["lvl"] += (a_lvl - aud["lvl"]) * (1.0 - math.exp(-dt / 0.4))
        elif audio_k <= 0:
            aud["lvl"] = 0.0
        a_p = apulse.step(dt)
        b_p = bpulse.step(dt)
        # the mods' beat: MIDI kicks + audio beats through ATTACK / RELEASE,
        # plus loudness -- one signal for every REACT knob
        mpulse.attack, mpulse.release = apulse.attack, apulse.release
        if music.kick_t != seen_kick:
            seen_kick = music.kick_t
            mpulse.hit(music.kick_v)
        # capped at 1.2: with AUDIO at 127 it reached ~2.4, so a full REACT
        # knob swung twice its range (glow ~x20; 2026-10-12 review)
        hit = min(1.2, max(mpulse.step(dt), audio_k * a_p) + 0.2 * audio_k * aud["lvl"])
        sync = LFO_SYNC_BARS[bucket(music.knobs[58], LFO_SYNC)]
        if sync is not None:
            tempo.poll(now)
        mods = mod_params(music.knobs, hit, mlfo.step(dt, lfo_rate(music.knobs[57]), sync, tempo), H)
        if now - aud["pub_t"] > 0.04:         # the /visuals page streams this (25/s)
            aud["pub_t"] = now
            publish_audio(audio, now - aud["beat_t"], a_lvl, fps,
                          {"lfo": round(mlfo.ph, 3), "hit": round(hit, 3),
                           "beat2_age": round(now - aud["beat2_t"], 3),
                           "synced": sync is not None and tempo.bars() is not None,
                           "bpm": round(tempo.bpm, 1) if sync is not None else None})

        # music -> motion (the MIDI kick and the audio beat both pulse it)
        p = beat_k * max(
            music.kick_v * math.exp(-(now - music.kick_t) * 9.0),
            audio_k * a_p)
        p += 0.2 * audio_k * aud["lvl"]                     # loudness swells it
        flash = min(1.0, beat_k) * math.exp(-(now - music.snare_t) * 14.0)
        playing = now - music.last_note_t < 8.0
        speed = speed_k * (1.0 + (min(music.rate, 8.0) * 0.06 if playing else 0.0)
                           + 0.25 * audio_k * aud["lvl"])
        step = speed * dt * 60.0      # the scenes were tuned per 60 fps frame
        scene.step = step
        scene.ctx = sctx
        f += step
        bg_clock += dt             # the steady (whole-refresh) clock, not wall time
        dh = ((music.hue - hue + 180) % 360) - 180        # glide the colour
        hue += dh * 0.04
        hue += 0.03 * step                                # slow drift

        kraw = {KNOBS[cc]: v for cc, v in music.knobs.items()}
        # the lines' own colour (2026-10-24): HUE turns them, CYCLE keeps them
        # turning (0.5 x knob^2 turns/s, like the background's), on top of COLOUR
        lines_ph = (lines_ph + dt * 360.0 * 0.5 * (kraw["lines_cycle"] / 127.0) ** 2) % 360.0
        L = Lines()
        LOGOS_KNOB[0] = music.knobs[52]                   # the LOGO knob, for logo/shape4d
        WEIGHT[0] = line_weight(music.knobs[53])
        OLD_LIGHT[0] = scene.old_light
        lines_g, bg_g = layer_gain(kraw["lines_level"]), layer_gain(kraw["bg_master"])
        if lines_g > 0:                   # LINES LEVEL 0: nothing to see, so nothing to draw
            scene.draw(L, f, hue + kn["colour"] * 180.0 + hue_deg(kraw["lines_hue"]) + lines_ph, p)
        mp = mask_params(kraw)
        c_scr = (W / 2.0, H / 2.0)
        fitp = fitter.plan(L.radius(c_scr) if mp["shape"] and mp["fit"] > 0 else 0.0,
                           mp, W, H, dt)
        data = L.array(camera(fitp, mods, W, H))
        if data is not None:
            # SATURATION + PALETTE on the stored colours, after the joints are
            # linked (colour tells lines apart there); identity at their defaults
            li = vis_palettes.slot_of(kraw["lines_pal"])
            data[:, 8:11] = recolour(data[:, 8:11], sat_mult(kraw["lines_sat"]),
                                     None if li is None else pals[li])

        # trails: frame-rate independent (a per-60fps-frame value ** frames)
        fr = dt * 60.0
        keep = 1.0 - (1.0 - scene.decay) * 2.0 ** (-(kn["trails"] + mods["trail"]) * 2)
        keep = min(0.995, max(0.3, keep)) ** fr
        zoom = (scene.zoom + kn["zoom"] * 0.02 + p * 0.01) ** fr
        spin = (scene.spin + kn["twist"] * 0.02) * fr

        # 1) fade + warp the last frame into the other buffer
        src, dst = cur, 1 - cur
        fbo[dst].use()
        tex[src].use(0)
        fade["decay"].value = keep
        fade["zoom"].value = zoom
        fade["spin"].value = spin
        fade["ctr"].value = ((0.5 * (1 + mp["pos"][0]), 0.5 * (1 + mp["pos"][1]))
                             if mp["shape"] else (0.5, 0.5))
        ctx.disable(moderngl.BLEND)
        fade_va.render(moderngl.TRIANGLE_STRIP)
        # 2) add this frame's lines, additively
        if data is not None:
            data = data[:LINE_MAX]
            vbo.orphan()                  # never wait on last frame's draw
            vbo.write(data.tobytes())
            ctx.enable(moderngl.BLEND)
            ctx.blend_func = moderngl.ONE, moderngl.ONE
            line_va.render(moderngl.TRIANGLES, vertices=6, instances=len(data))
        # 3) show it: background (small), mask (on change), composite
        # background: rests at black, the music lifts it (Presence)
        bp = bg_params(kraw, H)
        # your CUSTOM uploads / deletes show up within a second, no restart
        if now - cust["t"] > 1.0:
            cust["t"] = now
            psig = vis_palettes.sig()                 # palettes saved on /visuals
            if psig != cust["pal"]:
                cust["pal"] = psig
                pals = vis_palettes.as_rgb()
            sig = vis_patterns.custom_sig()
            if sig != cust["sig"]:
                cust["sig"] = sig
                new_sets = vis_patterns.pattern_sets(BG_PATTERNS)
                # CUSTOM and PICTURES (the last two sets) are the ones you change
                mine = {i for i, st in enumerate(new_sets) if st[1] in (vis_patterns.CUSTOM, vis_patterns.PICTURES)}
                with pat_lock:                # the tile builder thread writes these too
                    sets[:] = new_sets
                    for k_ in [k_ for k_ in pat_cache if k_[0] in mine]:
                        pat_cache.pop(k_, None)
                    for s_ in (tile_asked, tile_failed):
                        s_.difference_update({k_ for k_ in s_ if k_[0] in mine})
                if pat["key"] is not None and pat["key"][0] in mine:
                    pat["key"] = None         # reload it (it may have been replaced)
        # (not `si`: that is the SCENE index of the cycle above; reusing it here
        # made every cycle step jump to scene number "pattern set + 1", so on the
        # PICTURES set the cycle kept making a fresh ridgeline: 2026-10-24)
        bset = bucket(kraw["bg_set"], sets)
        key = (bset, bucket(kraw["bg_pattern"], sets[bset][2])) if sets[bset][2] else pat["key"]
        if key != pat["key"]:
            entry = pat_cache.get(key)
            if entry is not None:               # not built yet = keep the last
                pat_upload(entry)
                pat["key"] = key
            elif key not in tile_failed and key not in tile_asked:
                tile_asked.add(key)
                tile_want.put(key)
        drive = max(music.kick_v * math.exp(-(now - music.kick_t) * 6.0),
                    music.note_v * math.exp(-(now - music.note_t) * 4.0),
                    audio_k * bg_pulse(music.knobs, a_p, b_p), 0.7 * audio_k * aud["lvl"])
        amt = presence.step(dt, drive, bp["rest"], bp["react"], bp["lfo_hz"])
        amt *= bg_g                                   # BACKGROUND LEVEL: 0 = off whatever plays
        bi = vis_palettes.slot_of(kraw["bg_pal"])
        u = pat["units"]
        bars_dir = -1.0 if bp["bars_rate"] < 0 else 1.0
        bars_ph = (bars_ph + dt * (bp["bars_rate"] + bars_dir * 2.0 * hit * bp["bars"])) % 1000.0
        bmode = bucket(kraw["bg_mode"], BG_MODES)
        bg_trav = (bg_trav + dt * bp["speed"] * (1.0 + BG_SURGE * presence.sway)) % 20000.0
        comp.background(pat["tex"], amt, bmode,
                        bp["amp"] * (1.0 + BG_SWAY.get(bmode, 1.5) * presence.sway), bp["freq"], bp["speed"],
                        (bp["scale"], bp["scale"]) if u is None else
                        (u[0] * bp["unit"], u[1] * bp["unit"]),
                        bp["cycle"],
                        ((hue + kn["colour"] * 180.0 + hue_deg(kraw["bg_hue"])) % 360.0) / 360.0,
                        bg_clock % 3600.0, bp["bars"], bp["bars_rows"], bars_ph, bp["dof"],
                        bp["wrap"], bp["crisp"], bp["invert"], bp["thick"],
                        sat_mult(kraw["bg_sat"]),
                        None if bi is None else pals[bi], bg_trav)
        comp.set_mask(mp["shape"], mp["size"], mp["soft"], mp["pos"])
        # LINES LEVEL scales the lines layer, trails included (0 = background
        # only). The snare flash lights the whole picture, so it follows the
        # brighter layer: both off = black, flash included (a projector blackout)
        comp.show_frame(tex[dst], flash * max(lines_g, bg_g),
                        2.0 ** (kn["bright"] * 1.5) * mods["glow"] * lines_g)
        if state["shot"]:
            state["shot"] = False
            save_shot(ctx, W, H)
        t_cpu = time.monotonic()
        pygame.display.flip()
        t_swap = time.monotonic()
        jit.append(abs(raw_dt - dt) * 1000)
        cpu_sum += t_cpu - now
        cpu_max = max(cpu_max, t_cpu - now)
        swap_sum += t_swap - t_cpu
        cur = dst

        # knob positions + scene -> disk (at most every 2 s, only on change),
        # written by the saver's thread: a stalled SD card can't freeze a frame
        snap = (tuple(sorted(music.knobs.items())), name, cycling)
        if snap != saved and now - save_t > 2.0:
            saver.put(music.knobs, name, cycling)
            saved, save_t = snap, now
        t_save = time.monotonic()

        if vsync:
            clock.tick()                  # the swap waits for the refresh
        else:
            clock.tick(fps)
        t_tick = time.monotonic()
        phase = {"frame": t_cpu - now, "flip": t_swap - t_cpu, "save": t_save - t_swap,
                 "tick": t_tick - t_save}
        frames += 1
        late += raw_dt > 0.05             # a frame 50%+ late = a visible hitch
        worst = max(worst, raw_dt)
        if now - rep_t > 30:
            # also to /tmp: this X session's stdout doesn't reliably reach
            # the journal
            line = (f"{frames / (now - rep_t):.0f} fps, {late} late frames, "
                    f"worst gap {worst * 1000:.0f} ms, scene {name}, "
                    f"{'vsync %d' % vsync if vsync else 'no vsync'}, "
                    f"cpu {cpu_sum / max(frames, 1) * 1000:.1f} ms avg "
                    f"{cpu_max * 1000:.0f} max, swap {swap_sum / max(frames, 1) * 1000:.1f} ms avg, "
                    f"clock jitter p95 {np.percentile(jit, 95) if jit else 0:.1f} ms")
            print(f"[visuals] {line}", flush=True)
            append_bounded(FPS_LOG, f"{time.strftime('%H:%M:%S')} {line}")
            frames, rep_t, late, worst = 0, now, 0, 0.0
            cpu_sum, cpu_max, swap_sum = 0.0, 0.0, 0.0
            jit = []


if __name__ == "__main__":
    main()
