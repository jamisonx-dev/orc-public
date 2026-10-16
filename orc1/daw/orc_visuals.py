#!/usr/bin/env python3
"""ORC-1 visuals: glowing line-drawing scenes on the HDMI monitor, driven by
the music on the internal MIDI bus.

Run by visuals_session.sh inside the orc1-visuals kiosk X session. Draws on
the GPU (OpenGL 3.1 via moderngl), so the glow and trails cost the CPU almost
nothing. NOT a JACK client: it only READS the ORC1-Synth-Bus (ALSA-seq fans
the bus out to every reader, like the module hosts), so it cannot touch the
audio graph or skipback.

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
}
MOD_TARGETS = ("size", "spin", "glow", "sway", "trail")
LFO_SYNC = ("free", "4 bars", "2 bars", "1 bar", "1/2 bar", "1/4 bar")
LFO_SYNC_BARS = (None, 4.0, 2.0, 1.0, 0.5, 0.25)
# knobs that rest somewhere other than the middle (64)
KNOB_DEFAULT = {29: 12,                   # ATTACK ~ 20 ms
                32: 8, 33: 38,            # = 30 Hz .. 150 Hz, the kick band
                34: 0, 35: 0, 36: 0, 38: 0, 44: 0, 46: 24, 49: 127, 50: 0, 51: 56, 52: 0, 54: 56,
                58: 0, 59: 8, 60: 38, 62: 0, **{cc: 0 for cc in range(70, 80)}}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vis_patterns                                   # noqa: E402
import vis_shapes as VS                               # noqa: E402
import vis_logos                                      # noqa: E402

BG_PATTERNS = vis_patterns.BUILTIN                    # drawn by make_pattern
BG_MODES = ("wave", "interlace", "squeeze", "two layers", "wave + squeeze")
MASK_SHAPES = ("off", "circle", "rounded", "hexagon", "diamond", "triangle", "star")
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
    except Exception:
        pass
    return k


def save_state(knobs, scene, cycling):
    """Knob positions survive a restart; the web card reads this back."""
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"knobs": {KNOBS[cc]: v for cc, v in knobs.items()},
                       "scene": scene, "cycling": cycling, "sets_n": sets_count()}, f)
        os.replace(tmp, STATE)
    except Exception as e:
        print(f"[visuals] state save: {e}", flush=True)


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


def midi_thread(music):
    import mido
    while True:
        name = next((n for n in mido.get_input_names()
                     if "orc1-synth-bus" in n.lower()), None)
        if not name:
            time.sleep(3)
            continue
        print(f"[visuals] MIDI in: {name}", flush=True)
        try:
            with mido.open_input(name) as port:
                for msg in port:
                    music.on_msg(msg)
        except Exception as e:
            print(f"[visuals] MIDI error: {e}, retrying", flush=True)
            time.sleep(2)


# ---------------------------------------------------------------- audio tap

SKIPBUF = os.path.expanduser("~/.skipbuf")
TAP_SHM = "/dev/shm/orc1_vistap"
TAP_SRC = os.path.expanduser("~/.orc1_vistap")    # the tap's source ports


class TapShm:
    """Reads what orc1-vistap (daw/orc_jacktap.c) writes: the newest audio
    of one JACK port pair, float32 stereo, straight from the realtime
    callback -- no skipback writer pass in between. Header layout =
    struct tap_hdr in the C file (64 bytes)."""

    HDR = struct.Struct("<8s4IQd4I8x")
    MAX_AGE = 0.5            # s: an older newest-period = the tap is stuck

    def __init__(self, path=TAP_SHM):
        self.path = path
        self.mm, self.ino, self._chk_t = None, None, -99.0
        self.rate, self.period, self.wpos = 48000, 1024, None
        self.age = 0.0       # s: how old the newest audio was when read

    def _map(self, now):
        if now - self._chk_t < 1.0 and self.mm is not None:
            return
        self._chk_t = now
        try:
            st = os.stat(self.path)
        except OSError:
            self.mm = self.ino = None
            return
        if self.mm is not None and st.st_ino == self.ino:
            return
        try:
            with open(self.path, "rb") as f:
                self.mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
            self.ino, self.wpos = st.st_ino, None
        except (OSError, ValueError):
            self.mm = self.ino = None

    def header(self):
        (magic, rate, ch, cap, period, wpos, mono, conn, alive, xruns,
         pid) = self.HDR.unpack_from(self.mm, 0)
        return dict(magic=magic, rate=rate, ch=ch, cap=cap, period=period,
                    wpos=wpos, mono=mono, conn=conn, alive=alive, xruns=xruns)

    def read(self, now, n):
        """-> ("off", None) the tap can't be used; ("same", None) nothing new
        since the last call; ("new", frames[n, 2])."""
        self._map(now)
        if self.mm is None:
            return "off", None
        try:
            h = self.header()
        except (struct.error, ValueError):
            return "off", None
        if (h["magic"] != b"ORCTAP1\0" or not h["alive"] or not h["conn"]
                or h["ch"] != 2 or n > h["cap"] // 2
                or time.monotonic() - h["mono"] > self.MAX_AGE):
            return "off", None
        self.rate, self.period = h["rate"] or 48000, h["period"] or 1024
        w = h["wpos"]
        if w == self.wpos or w < n:           # nothing new / just started
            return "same", None
        cap = h["cap"]
        start = (w - n) % cap
        a = np.frombuffer(self.mm, "<f4", count=cap * 2,
                          offset=self.HDR.size).reshape(cap, 2)
        if start + n <= cap:
            fr = a[start:start + n].copy()
        else:
            fr = np.concatenate((a[start:], a[:start + n - cap]))
        # lapped while copying (reader stalled > cap - n frames): drop it.
        # The C writer stores a period's frames BEFORE it publishes wpos, so
        # one period past the wpos we re-read may already be overwritten:
        # the copied span is safe only while it is within cap - period.
        if self.HDR.unpack_from(self.mm, 0)[5] - (w - n) > cap - self.period:
            return "same", None
        self.wpos = w
        self.age = max(0.0, time.monotonic() - h["mono"])
        return "new", fr


def cc_to_hz(v):
    """Band knobs (CC 32/33) -> Hz, log from 20 Hz (0) to 16 kHz (127)."""
    return 20.0 * 800.0 ** (max(0, min(127, v)) / 127.0)


def hz_to_cc(hz):
    return int(round(127 * math.log(max(20.0, hz) / 20.0) / math.log(800.0)))


class AudioTap:
    """Reacts to whatever is playing through the mixer (Bluetooth, the rig,
    anything on the L-12 master) WITHOUT joining JACK itself. Two sources:

    1. the TAP (TapShm): orc1-vistap, a C listen-only JACK client, copies
       any port pair (~/.orc1_vistap, picked on /visuals) into shared memory
       every JACK period (21 ms). Used whenever it is running, connected and
       fresh.
    2. otherwise the newest audio of a skipback RAM ring (tmpfs), the way the
       sampler does -- what this did before the tap existed (2026-09-30).
    Neither can make the recorder late: the Python side only reads memory.

    Which ring: a RAM stereo track named like MAIN (the master), else any RAM
    ring, else anything (re-picked every 5 s from heads.json, so a /skipback
    layout change follows). A card ring works but its ring.pos can stall
    for seconds behind SD writes, so it's the last choice.

    LATENCY (measured 2026-09-29): the recorder publishes ring.pos every
    ~43 ms, so audio is 0-43 ms old when it arrives here; nothing on this
    side can beat that, and the recorder is not ours to hurry. The BEAT
    looks at only the newest SHORT window (1024 frames = 21 ms; it was 43 ms)
    so a hit shows as soon as it lands; the LONG window (4096 = 85 ms) is
    only for the spectrum the /visuals page draws.

    Each update() gives (beat, 0.0, level):
      beat  = an onset in the BEAT BAND (set_band; default 30-150 Hz) jumping
              above its last ~1.3 s by ONSET x, 0..1, at most one per GAP s
      level = loudness 0..1 after an automatic gain that follows the music
              over ~15 s, so a quiet Bluetooth stream drives it fully
      beat2 = the same test on a SECOND band (2026-10-12, owner: one band
              for the shapes, another for the background), with its own SENS
              (ONSET2); 0.0 while band2_on is False (the background then
              follows band 1, exactly as before). It reads the same FFT, so
              it costs one masked sum.
    (The middle slot was once a treble screen flash: removed, it flickered.)"""

    N = 1024                                  # beat window (~21 ms)
    N_LONG = 4096                             # spectrum window (~85 ms)
    FLOOR = 1e-3                              # -60 dBFS rms
    ONSET = 2.0     # the band must jump to 2x its recent average
    ONSET2 = 2.0    # the same for band 2 (its own SENS)
    GAP = 0.3       # s between beats at least (0.25 let eighths through)
    band2_on = False
    SPEC_BANDS = 48                           # log bands 20 Hz .. 16 kHz
    SPEC_EVERY = 0.04                         # s between spectrum updates (25/s)
    gate = 10 ** (-68 / 20.0)                 # rms; set live from the GATE knob

    def __init__(self, root=None, shm=None):
        self.root = root or SKIPBUF
        self.shm = TapShm(shm or TAP_SHM)
        self.source = None                    # "tap" | "ring" | None
        self.age_ms = 0.0                     # tap: newest audio's age at read
        self.dir, self.size, self.ch = None, 0, 2
        self._pick_t = -99.0
        self._pos = None
        self._pos_t = None
        self.cadence = 0.0                    # ms between ring.pos updates
        # Auto-gain envelope (rms). Its FLOOR is the sensitivity: MEASURED
        # 2026-09-29 on the L-12 master, quiet Bluetooth music ran -55..-66
        # dBFS rms and the gaps between songs -68..-80, so a -60 dBFS floor
        # lets quiet music reach full level while the gaps stay low.
        self.env = self.FLOOR
        self.hist, self.hist2 = [], []
        self.last_beat = self.last_beat2 = -99.0
        self.win = np.hanning(self.N).astype("f4")
        self.win_l = np.hanning(self.N_LONG).astype("f4")
        self.spec = [None] * self.SPEC_BANDS
        self.frames, self.frames_t = None, -99.0
        self._spec_t = -99.0
        self.band = self.band2 = (None, None)
        self.rate = None
        self._set_rate(48000)
        self.set_band(30.0, 150.0)
        self.set_band2(30.0, 150.0)

    @property
    def live(self):
        """True while there is a source to hear (tap or ring)."""
        return self.source is not None

    def _set_rate(self, rate):
        """FFT bins for the stream's sample rate (the tap reports JACK's)."""
        if rate == self.rate:
            return
        self.rate = rate
        self.freq = np.fft.rfftfreq(self.N, 1.0 / rate)
        self.freq_l = np.fft.rfftfreq(self.N_LONG, 1.0 / rate)
        edges = 20.0 * 800.0 ** (np.arange(self.SPEC_BANDS + 1) / self.SPEC_BANDS)
        # a low band narrower than one bin (~12 Hz) shows its nearest bin
        self.spec_idx = [np.nonzero((self.freq_l >= lo) & (self.freq_l < hi))[0]
                         for lo, hi in zip(edges[:-1], edges[1:])]
        self.spec_idx = [ix if len(ix) else
                         np.array([int(np.argmin(np.abs(self.freq_l - (lo * hi) ** 0.5)))])
                         for ix, lo, hi in zip(self.spec_idx, edges[:-1], edges[1:])]
        # every band is one run of neighbouring bins: [start, end) pairs,
        # interleaved, so ONE np.maximum.reduceat gives all 48 maxima (the
        # odd slots are junk and dropped). A per-band Python loop cost ~1 ms.
        self._spec_ri = np.array([v for ix in self.spec_idx for v in (ix[0], ix[-1] + 1)], np.intp)
        # the page draws neighbouring bands that read the SAME bins as one
        # wide bar (the bass end: 12 bands under 100 Hz, ~8 real bins) --
        # run lengths over the 48 bands, in order (they sum to SPEC_BANDS)
        runs, prev = [], None
        for ix in self.spec_idx:
            key = (int(ix[0]), int(ix[-1]))
            if key == prev:
                runs[-1] += 1
            else:
                runs.append(1)
            prev = key
        self.spec_runs = runs
        lo, hi = self.band
        if lo is not None:                    # re-fit the band to the new bins
            self.band = (None, None)
            self.set_band(lo, hi)
        lo, hi = getattr(self, "band2", (None, None))
        if lo is not None:
            self.band2 = (None, None)
            self.set_band2(lo, hi)

    def set_band(self, lo, hi):
        """The frequencies the BEAT listens to. At least one FFT bin always
        counts (the short window's bins are ~47 Hz apart)."""
        lo, hi = sorted((float(lo), float(hi)))
        if (lo, hi) == self.band:
            return
        self.band = (lo, hi)
        self.band_m, self.band_true = self._mask(lo, hi)
        self.hist = []                        # a new band = a new baseline

    def set_band2(self, lo, hi):
        """Band 2 (the background's, when band2_on): same rules as set_band."""
        lo, hi = sorted((float(lo), float(hi)))
        if (lo, hi) == self.band2:
            return
        self.band2 = (lo, hi)
        self.band2_m, self.band2_true = self._mask(lo, hi)
        self.hist2 = []

    def _mask(self, lo, hi):
        """-> (the short window's bins in lo..hi, what they REALLY hear)."""
        m = (self.freq >= lo) & (self.freq <= hi)
        if not m.any():                       # narrower than a bin: nearest
            m = np.zeros_like(m)
            m[int(np.argmin(np.abs(self.freq - (lo + hi) / 2)))] = True
        # what the beat REALLY hears (2026-10-12: the readout said 60-90 Hz
        # while the only bin it used sat at 94): the chosen bins, each
        # spreading +-1 bin (a Hann window's -6 dB width is 2 bins)
        f = self.freq[m]
        step = self.freq[1]
        return m, (max(20.0, float(f.min() - step)), float(f.max() + step))

    def _onset(self, e, h, last, onset, rms, gated, now):
        """One band's beat test: its energy e against its last ~30 readings
        (h, updated here). -> strength 0..1 (0 = no beat)."""
        avg = (sum(h) / len(h)) if h else e
        h.append(e)
        if len(h) > 30:                       # ~1.3 s of history
            del h[0]
        if (len(h) >= 8 and e > avg * onset and rms > 2e-5 and not gated
                and now - last > self.GAP):
            return min(1.0, (e / max(avg, 1e-12) - 1.0) / 2.0)
        return 0.0

    def _pick(self, now):
        if now - self._pick_t < 5.0:
            return
        self._pick_t = now
        try:
            with open(os.path.join(self.root, "heads.json")) as f:
                tr = json.load(f).get("tracks", {})
        except Exception:
            self.dir = None
            return

        def rank(t):
            ram = t.get("store", "ram") == "ram"
            main = "MAIN" in str(t.get("name", "")).upper()
            return (not ram, not (main and t.get("channels") == 2), not main)
        best = sorted((t for t in tr.values() if t.get("dir")), key=rank)
        if not best:
            self.dir = None
            return
        t = best[0]
        self.dir = os.path.join(self.root, t["dir"])
        self.size = int(t.get("buffer_bytes", 0))
        self.ch = int(t.get("channels", 2))

    def _read(self, now):
        try:
            with open(os.path.join(self.dir, "ring.pos")) as f:
                pos = int(f.read().strip() or 0)
        except Exception:
            return None
        if pos == self._pos or self.size <= 0:
            return None                       # recorder hasn't moved: no news
        if self._pos_t is not None:
            self.cadence += ((now - self._pos_t) * 1000 - self.cadence) * 0.1
        self._pos, self._pos_t = pos, now
        fb = 2 * self.ch
        n = self.N_LONG * fb
        start = (pos - n) % self.size
        start -= start % fb
        try:
            with open(os.path.join(self.dir, "ring.raw"), "rb") as f:
                f.seek(start)
                raw = f.read(min(n, self.size - start))
                if len(raw) < n:
                    f.seek(0)
                    raw += f.read(n - len(raw))
        except Exception:
            return None
        if len(raw) != n:
            return None
        a = np.frombuffer(raw, "<i2").astype("f4") / 32768.0
        self.frames = a.reshape(-1, self.ch)  # the XY scope wants L and R
        self.frames_t = now
        return self.frames.mean(axis=1)

    def _read_tap(self, now):
        """The tap first: fresher (one JACK period, no writer pass) and any
        port. -> (usable, mono samples or None when nothing new)."""
        st, fr = self.shm.read(now, self.N_LONG)
        if st == "off":
            return False, None
        self._set_rate(self.shm.rate)
        self.cadence = self.shm.period * 1000.0 / self.shm.rate
        if fr is None:
            return True, None
        self.age_ms += (self.shm.age * 1000 - self.age_ms) * 0.1
        self.ch = 2
        self.frames = fr
        self.frames_t = now
        return True, fr.mean(axis=1)

    def update(self, now):
        ok, long = self._read_tap(now)
        if ok:
            if self.source != "tap":
                self.source, self.hist, self.hist2 = "tap", [], []
        else:
            self._pick(now)
            if not self.dir:
                self.source = None
                return 0.0, 0.0, 0.0
            if self.source != "ring":
                # a fresh baseline; the ring's old pos means nothing now
                self.source, self.hist, self._pos, self._pos_t = "ring", [], None, None
                self.hist2 = []
                self._set_rate(48000)
            long = self._read(now)
        if long is None:
            return 0.0, 0.0, None             # None = keep the last level
        m = long[-self.N:]                    # the newest 21 ms
        rms = float(np.sqrt(np.mean(m * m)))
        self.env = max(rms, self.env * 0.9985, self.FLOOR)
        # the GATE: below it is silence, whatever the auto gain would make of
        # it. The gain keeps turning itself up in quiet (to catch quiet
        # Bluetooth), so hiss and the gaps between songs read as "playing"
        # and fired false beats + lifted the background (owner 2026-09-30)
        gated = rms < self.gate
        level = 0.0 if gated else (min(1.0, rms / self.env) if rms > 1e-5 else 0.0)
        sp = np.abs(np.fft.rfft(m * self.win))
        p2 = sp * sp
        beat = self._onset(float(p2[self.band_m].sum()), self.hist, self.last_beat,
                           self.ONSET, rms, gated, now)
        if beat:
            self.last_beat = now
        beat2 = 0.0
        if self.band2_on:
            beat2 = self._onset(float(p2[self.band2_m].sum()), self.hist2, self.last_beat2,
                                self.ONSET2, rms, gated, now)
            if beat2:
                self.last_beat2 = now
        # the spectrum for the page + ridgeline, relative to the auto gain so a
        # quiet stream still fills the display. At most every SPEC_EVERY s: the
        # tap delivers audio every 21 ms (the ring every 43), and a 4096-point
        # FFT per delivery cost ~3 ms a frame on the Pi (measured 2026-09-30).
        if now - self._spec_t >= self.SPEC_EVERY:
            self._spec_t = now
            if gated:
                # below the GATE = silence, on the display too. MEASURED
                # 2026-10-12: with the L-12 master silent, its own hiss
                # (~-115 dBFS) still drew bars up to a quarter high, because
                # the display is scaled against the auto gain's -60 dBFS
                # floor and reaches 72 dB below it (owner: "lots of
                # readings" with everything turned down)
                self.spec = [None] * self.SPEC_BANDS
                return beat, beat2, level
            sl = np.abs(np.fft.rfft(long * self.win_l)) / (self.N_LONG / 4)
            g = 1.0 / max(self.env, 1e-6)
            m = np.maximum.reduceat(np.append(sl, 0.0), self._spec_ri)[::2]
            self.spec = np.round(20 * np.log10(m * g + 1e-9), 1).tolist()
        return beat, beat2, level


AUDIO_PUB = "/tmp/orc1_visuals_audio.json"


def tap_ports():
    """The tap's source as the page shows it: [left, right] (one entry for a
    mono source). Mirrors orc_jacktap.c read_source()."""
    try:
        with open(TAP_SRC) as f:
            lines = [ln.strip() for ln in f.read().splitlines()[:2]]
    except OSError:
        return ["system:capture_1", "system:capture_2"]
    left = lines[0] if lines else ""
    right = lines[1] if len(lines) > 1 and lines[1] else left
    return [left] if right == left else [left, right]


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


def audio_onset(v):
    """SENS knob (CC 31) -> how far the bass must jump above its last second
    to count as a beat: 0 = 3x (big hits only) .. 127 = 1.3x (everything)."""
    return 3.0 - 1.7 * (max(0, v) / 127.0)


def audio_gate(v):
    """GATE knob (CC 54) -> rms threshold: 0 = off; else -90 .. -40 dBFS
    (default 56 = -68: under the quietest Bluetooth music measured on the
    L-12 master, -66, and over the hiss between songs, -68 .. -80)."""
    return 0.0 if v <= 0 else 10 ** ((-90.0 + 50.0 * min(127, v) / 127.0) / 20.0)


def audio_amount(v):
    """AUDIO knob (CC 28) -> reaction amount: a curve, so the low half is
    genuinely subtle (1 -> 0.002, 32 -> 0.35, 64 -> 1, 127 -> 2)."""
    return min(2.0, (max(0, v) / 64.0) ** 1.5)


# ------------------------------------------------------------- background

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


def band2_on(knobs):
    """BACKGROUND OWN BAND switch (CC 62): >= 64 = the background has its own
    beat band (59/60 + SENS 61); below = it follows band 1, as before."""
    return knobs.get(62, 0) >= 64


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


def line_weight(v):
    """WEIGHT knob (CC 53) -> glow width factor: 0 -> 0.4, 64 -> 1 (the
    scene's own), 127 -> 2.5. owner 2026-09-30, of a first cut that blew
    out to a white blur: "I really liked the overloaded look" -- so it's a
    choice, not a fix."""
    return 2.5 ** ((max(0, min(127, v)) - 64) / 64.0)


class Lines:
    """Collects one frame's lines as plain segments, then builds every glow
    quad in ONE numpy pass (6 verts / segment). MEASURED 2026-09-29: a numpy
    build per add() call cost ~99% of a core on tube / warp (hundreds of
    short lines a frame); batched, the per-line cost is a few list appends."""

    def __init__(self):
        self.seg = []          # (ax, ay, bx, by, r, g, b, radius)
        self.blocks = []       # numpy (n, 8) blocks of the same columns

    def add_segments(self, ab, col, width):
        """ab = (n, 4) array of segment ends [ax, ay, bx, by], one colour."""
        if len(ab):
            blk = np.empty((len(ab), 8), "f4")
            blk[:, 0:4] = ab
            blk[:, 4:7] = col
            blk[:, 7] = width * 3.0
            self.blocks.append(blk)

    def add(self, pts, col, width, closed=False):
        r = width * 3.0                          # glow reaches 3x the core
        c0, c1, c2 = col
        seg = self.seg
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
        # builds of tube's segment list cost ~1.5 ms a frame (measured)
        n = (len(self.seg), len(self.blocks))
        if getattr(self, "_S_n", None) == n:
            return self._S
        parts = ([np.asarray(self.seg, dtype="f4")] if self.seg else []) + self.blocks
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
        """fit = (c0, k, c1): scale the points about c0 by k and move them to
        c1 (Fit.plan) BEFORE the glow quads are built = same line width.
        k may be a 2x2 matrix (camera(): scale + rotate)."""
        S = self._stack()
        if S is None:
            return None
        a, b, col, r = S[:, 0:2], S[:, 2:4], S[:, 4:7], S[:, 7:8] * WEIGHT[0]
        if fit is not None:
            c0, k, c1 = fit
            if np.ndim(k) == 2:
                a = (a - c0) @ k.T + c1
                b = (b - c0) @ k.T + c1
            else:
                a = (a - c0) * k + c1
                b = (b - c0) * k + c1
        d = b - a
        ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-4)[:, None]
        n = np.stack([-d[:, 1], d[:, 0]], 1) / ln * r
        t = d / ln * r * 0.5                     # overlap = soft joints
        a0, b0 = a - t, b + t
        m = len(S)
        v = np.empty((m, 6, 6), "f4")
        v[:, :, 0:2] = np.stack([a0 + n, a0 - n, b0 + n, b0 + n, a0 - n, b0 - n], 1)
        v[:, :, 2] = np.array([1, -1, 1, 1, -1, -1], "f4")
        v[:, :, 3:6] = col[:, None, :]
        return v.reshape(-1, 6)


class Scene:
    decay = 0.80      # how much of the last frame survives (trails)
    zoom = 1.0        # >1 = trails rush outward (tunnel feel)
    spin = 0.0        # trail rotation per frame, radians
    step = 1.0        # 60 fps frames this frame stands for (x speed knob)
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
                for k in range(0, seg, 2):
                    L.add([ring[k], nxt[k]], rgb(hue - 20 + z * 100, .5, z * .7),
                          .4 + z * 1.5)


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
in vec2 pos; in float side; in vec3 col;
out float d; out vec3 c; uniform vec2 res;
void main(){ d = side; c = col;
  gl_Position = vec4(pos / res * 2.0 - 1.0, 0, 1) * vec4(1, -1, 1, 1); }"""

LINE_FS = """#version 140
in float d; in vec3 c; out vec4 o;
void main(){
  float x = d * d;
  float f = exp(-x * 30.0) + 0.28 * exp(-x * 4.0);   // hot core + soft glow
  o = vec4(c * f, 1.0);
}"""

BG_FS = """#version 140
// the background, rendered at 1/Composer.BG_DIV resolution (px = full-res
// pixels): an EarthBound-style bent pattern; each ~224th of the screen is one
// "scanline" row whose read is shifted by A*sin(F*row + S*t)
out vec4 o;
uniform sampler2D pat;
uniform float t, div;
uniform int bg_mode;
uniform float bg_amp, bg_freq, bg_speed, bg_cycle, bg_hue, rowh;
uniform vec2 bg_tile;          // one tile's size on screen, px (any shape)

vec3 hsv(float h, float s, float v) {
  vec3 k = clamp(abs(mod(h * 6.0 + vec3(0.0, 4.0, 2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
  return v * mix(vec3(1.0), k, s);
}

float layer(vec2 px, float amp, float freq, float speed, int mode, vec2 tile, float ph) {
  float row = floor(px.y / rowh);
  float off = amp * sin(freq * row + speed * t + ph);
  vec2 q = px;
  if (mode == 0) q.x += off;
  else if (mode == 1) q.x += (mod(row, 2.0) < 1.0) ? off : -off;
  else if (mode == 2) q.y += off;
  else { q.x += off; q.y += 0.6 * amp * sin(0.7 * freq * row - speed * t + ph); }
  return texture(pat, q / tile).r;
}

void main(){
  vec2 px = gl_FragCoord.xy * div;
  float v;
  if (bg_mode == 3)
    v = 0.5 * (layer(px, bg_amp, bg_freq, bg_speed, 0, bg_tile, 0.0)
             + layer(px, bg_amp * 0.7, bg_freq * 1.3, -bg_speed * 0.8, 1, bg_tile * 1.5, 1.7));
  else
    v = layer(px, bg_amp, bg_freq, bg_speed, bg_mode == 4 ? 3 : bg_mode, bg_tile, 0.0);
  o = vec4(hsv(bg_hue + v * 0.35 + t * bg_cycle, 0.75, v), 1.0);   // gaps stay black
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

    def background(self, pat_tex, amt, mode, amp, freq, speed, tile, cycle, hue, t):
        """Render the background (small) when it shows at all."""
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

    vbo = ctx.buffer(reserve=6 * 4 * 60000, dynamic=True)
    line_va = ctx.vertex_array(line, [(vbo, "2f 1f 3f", "pos", "side", "col")])

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
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            pat["tex"] = t
        t.write(data)
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
    cust = {"t": -99.0, "sig": vis_patterns.custom_sig()}

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
                tile_failed.add(key)
                print(f"[visuals] pattern {folder}/{names[key[1]]}: {e}", flush=True)
                continue
            files = [k for k in pat_cache if k[0] != 0]
            for old in files[:max(0, len(files) - TILE_KEEP + 1)]:
                pat_cache.pop(old, None)
                tile_asked.discard(old)
            now_names = sets[key[0]][2]
            if key[1] >= len(now_names) or now_names[key[1]] != slug:
                tile_asked.discard(key)       # an upload/delete moved it: ask again
                continue
            # image rows run top-down, a texture's bottom-up: flip, so a
            # skull tile isn't upside down
            pat_cache[key] = ((g[::-1] * 255).astype("u1").tobytes(),
                              (g.shape[1], g.shape[0]), units)
    threading.Thread(target=_build_patterns, daemon=True).start()
    comp = Composer(ctx, W, H)
    presence = Presence()
    fitter = Fit()
    global LOGOS
    LOGOS = LogoBank()
    t0 = time.monotonic()
    bg_clock = 0.0

    music = Music()
    threading.Thread(target=midi_thread, args=(music,), daemon=True).start()

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

        L = Lines()
        LOGOS_KNOB[0] = music.knobs[52]                   # the LOGO knob, for logo/shape4d
        WEIGHT[0] = line_weight(music.knobs[53])
        scene.draw(L, f, hue + kn["colour"] * 180.0, p)
        kraw = {KNOBS[cc]: v for cc, v in music.knobs.items()}
        mp = mask_params(kraw)
        c_scr = (W / 2.0, H / 2.0)
        fitp = fitter.plan(L.radius(c_scr) if mp["shape"] and mp["fit"] > 0 else 0.0,
                           mp, W, H, dt)
        data = L.array(camera(fitp, mods, W, H))

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
            data = data[:60000]
            vbo.orphan()                  # never wait on last frame's draw
            vbo.write(data.tobytes())
            ctx.enable(moderngl.BLEND)
            ctx.blend_func = moderngl.ONE, moderngl.ONE
            line_va.render(moderngl.TRIANGLES, vertices=len(data))
        # 3) show it: background (small), mask (on change), composite
        # background: rests at black, the music lifts it (Presence)
        bp = bg_params(kraw, H)
        # your CUSTOM uploads / deletes show up within a second, no restart
        if now - cust["t"] > 1.0:
            cust["t"] = now
            sig = vis_patterns.custom_sig()
            if sig != cust["sig"]:
                cust["sig"] = sig
                sets[:] = vis_patterns.pattern_sets(BG_PATTERNS)
                ci = len(sets) - 1
                for k_ in [k_ for k_ in pat_cache if k_[0] == ci]:
                    pat_cache.pop(k_, None)
                for s_ in (tile_asked, tile_failed):
                    s_.difference_update({k_ for k_ in s_ if k_[0] == ci})
                if pat["key"] is not None and pat["key"][0] == ci:
                    pat["key"] = None         # reload it (it may have been replaced)
        si = bucket(kraw["bg_set"], sets)
        key = (si, bucket(kraw["bg_pattern"], sets[si][2])) if sets[si][2] else pat["key"]
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
        u = pat["units"]
        comp.background(pat["tex"], amt, bucket(kraw["bg_mode"], BG_MODES),
                        bp["amp"] * (1.0 + 1.5 * presence.sway), bp["freq"], bp["speed"],
                        (bp["scale"], bp["scale"]) if u is None else
                        (u[0] * bp["unit"], u[1] * bp["unit"]),
                        bp["cycle"], ((hue + kn["colour"] * 180.0) % 360.0) / 360.0,
                        bg_clock % 3600.0)
        comp.set_mask(mp["shape"], mp["size"], mp["soft"], mp["pos"])
        comp.show_frame(tex[dst], flash, 2.0 ** (kn["bright"] * 1.5) * mods["glow"])
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

        # knob positions + scene -> disk (at most every 2 s, only on change)
        snap = (tuple(sorted(music.knobs.items())), name, cycling)
        if snap != saved and now - save_t > 2.0:
            save_state(music.knobs, name, cycling)
            saved, save_t = snap, now

        if vsync:
            clock.tick()                  # the swap waits for the refresh
        else:
            clock.tick(fps)
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
