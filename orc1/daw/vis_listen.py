"""vis_listen.py -- the LISTEN tab's ears, shared (2026-11-02): orc1-vistap's audio (a tiny C
listen-only JACK client, daw/orc_jacktap.c) read from shared memory, the spectrum and the BEAT
detector on one or two bands, and the knob -> Hz / SENS / GATE helpers. Moved out of orc_visuals.py
(which imports pygame) so the MXV box conductor can hear exactly what LISTEN hears even while the
visuals kiosk is off. numpy only; import-safe (nothing runs until AudioTap.update is called)."""
import ctypes
import json
import math
import mmap
import os
import struct
import time

import numpy as np


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


def audio_onset(v):
    """SENS knob (CC 31) -> how far the bass must jump above its last second
    to count as a beat: 0 = 3x (big hits only) .. 127 = 1.3x (everything)."""
    return 3.0 - 1.7 * (max(0, v) / 127.0)


def audio_gate(v):
    """GATE knob (CC 54) -> rms threshold: 0 = off; else -90 .. -40 dBFS
    (default 56 = -68: under the quietest Bluetooth music measured on the
    L-12 master, -66, and over the hiss between songs, -68 .. -80)."""
    return 0.0 if v <= 0 else 10 ** ((-90.0 + 50.0 * min(127, v) / 127.0) / 20.0)


def band2_on(knobs):
    """BACKGROUND OWN BAND switch (CC 62): >= 64 = the background has its own
    beat band (59/60 + SENS 61); below = it follows band 1, as before."""
    return knobs.get(62, 0) >= 64


# The LISTEN knobs (CCs of the "visuals" module): their names in the renderer's state file
# (~/.orc1_visuals_state.json, orc_visuals.KNOBS) and resting values (orc_visuals.KNOB_DEFAULT).
LISTEN_KNOBS = {31: ("sens", 64), 32: ("band_lo", 8), 33: ("band_hi", 38), 54: ("gate", 56),
                59: ("band2_lo", 8), 60: ("band2_hi", 38), 61: ("sens2", 64), 62: ("band2_on", 0)}
VIS_STATE = os.path.expanduser("~/.orc1_visuals_state.json")


def listen_knobs(state_path=None):
    """The LISTEN knobs as last saved by the visuals renderer {cc: 0-127}, resting values where unset."""
    out = dict((cc, d) for cc, (_n, d) in LISTEN_KNOBS.items())
    try:
        with open(state_path or VIS_STATE) as f:
            saved = json.load(f).get("knobs", {})
        for cc, (name, _d) in LISTEN_KNOBS.items():
            if isinstance(saved.get(name), int):
                out[cc] = max(0, min(127, saved[name]))
    except (OSError, ValueError, AttributeError):
        pass
    return out


def apply_knobs(tap, k):
    """Set an AudioTap's bands / SENS / GATE from LISTEN knob values, as the renderer does."""
    tap.ONSET = audio_onset(k[31])
    tap.gate = audio_gate(k[54])
    tap.set_band(cc_to_hz(k[32]), cc_to_hz(k[33]))
    tap.band2_on = band2_on(k)
    tap.ONSET2 = audio_onset(k[61])
    tap.set_band2(cc_to_hz(k[59]), cc_to_hz(k[60]))
