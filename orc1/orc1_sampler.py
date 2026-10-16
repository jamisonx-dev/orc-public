#!/usr/bin/env python3
"""
ORC-1 Sampler Service  —  controller-agnostic live-remix sample decks.

This module is the DSP + capture core. It is deliberately SELF-CONTAINED
(NumPy + stdlib only) so it can be:
  - imported by orc1_synth.py and given a JACK bus, and
  - run standalone to verify the ring-buffer tap:  python3 orc1_sampler.py --selftest 8

DESIGN CONTRACT
  * skipback is untouched. We only ever READ ~/.skipbuf/ring.raw + ring.pos as a
    snapshot; we never write them, never touch ~/recordings, never stop the daemon.
  * All ring reads + buffer building happen in the CONTROL thread. The finished
    float buffer is handed to a deck via an atomic reference swap (load_buffer),
    so the JACK render thread never does I/O or allocation.
  * A deck reuses the same variable-rate interpolated playback that SampleVoice
    uses in orc1_synth.py (jog-scrub and pitch are just changes to _pos / _rate).

Phase 1 scope (this file): RingBufferReader + a playable mono SampleDeck
(capture / gate / speed / gain / scrub / loop / click-fade) + a 2-deck
SamplerManager with crossfade. Hotcues, reverse and stereo are still
stubbed for a future phase.

2026-07-07: added a 3-band EQ (low shelf / mid peak / high shelf) and a
one-knob DJ-style sweep filter (low-pass below center, high-pass above) per
deck, driven by the Mixtrack's EQ_HI/EQ_MID/EQ_LOW/FILTER knobs. Implemented
locally (not imported from orc1_synth.py's BiquadFilter) so this module stays
import-safe in both directions -- orc1_synth.py already imports this file, so
this file must never import back.
"""

import os
import time
import numpy as np

try:                                    # scipy's C lfilter, minus ~48 MB of
    from orc1_lfilter import lfilter as _lfilter   # scipy.signal (orc1_lfilter.py)
except Exception:                       # pragma: no cover -- scipy is present
    _lfilter = None                     # on the Pi; degrade to flat/bypass if not


# ==============================================================================
# RING BUFFER READER  —  read-only snapshot of the skipback buffer
# ==============================================================================

class RingBufferReader:
    """
    Read-only view of the skipback ring buffer. NEVER writes.

    The skipback daemon writes S16_LE interleaved stereo at `rate` into a fixed
    circular file and records the write head in `pos_path` (a byte offset).
    Since 2026-07-07 the daemon flushes the head EVERY ~43ms chunk (it used to
    flush every ~2.13s, which made every capture end a uniformly-random 0-2s
    in the past). The pos file's mtime doubles as a timestamp anchor: head()
    estimates how far the true head has moved since the flush, so captures can
    be placed on the musical grid to within a few ms.
    """

    def __init__(self, ring_path, pos_path,
                 buffer_secs=1860, rate=48000, channels=2,
                 tracks_path="~/.orc1_skipback_tracks.json"):
        self.ring_path   = os.path.expanduser(ring_path)
        self.pos_path    = os.path.expanduser(pos_path)
        self.rate        = int(rate)
        self.channels    = int(channels)
        self.bytes_per_s = self.rate * self.channels * 2      # 2 = S16 width
        self.buffer_bytes = self.bytes_per_s * int(buffer_secs)
        # PRECEDENCE RULE (2026-07-29): buffer_secs/channels passed here are
        # only FALLBACKS for the window before the first successful resolve.
        # Both call sites pass channels= explicitly, so without this rule the
        # resolve below would be a no-op in production. The first successful
        # refresh_geometry() supersedes them.
        self.tracks_path = os.path.expanduser(tracks_path)
        self._geo_key    = None       # (st_size, st_ino, channels) last resolved
        self._geo_resolved = False    # has a resolve ever succeeded?
        # Best-effort resolve up front so the reader is correct from the first
        # grab. This can legitimately fail: skipback-record and orc1-synth are
        # both only After=orc1-jackd, so the reader can be built before
        # ring.raw exists. That is exactly why the per-capture refresh exists
        # -- it rescues a session that started in the fallback.
        try:
            self.refresh_geometry()
        except Exception:
            pass

    # ---- geometry ------------------------------------------------------------
    # Why this exists: buffer_bytes used to be derived from the STATIC
    # SAMPLER.RING_SECONDS (1860) while the real ring is sized PER TRACK from
    # ~/.orc1_skipback_tracks.json (track 0 = 1200s today). 357,120,000 vs
    # 230,400,000. All three failure directions were measured on the Pi:
    #   A over-estimate (live until this fix): a WRAPPING read comes back short
    #     -> _load_slice bails -> GRAB silently does nothing.
    #   B under-estimate (any length_secs > 1860, one bare number input away):
    #     full-length audio from the WRONG moment, silently.
    #   C wrong channel count (mono track): frame step 2.00 -> 2:1 compression
    #     -> double speed, an octave HIGH.
    # Rate is pinned 48000 both ends (skipback_recorder.py), so it is not a
    # fourth direction.
    #
    # MUST be called only from SamplerManager._do_capture and .nudge (both
    # control-thread). render() never touches ring geometry, so the JACK RT
    # thread cannot observe a torn size/channels pair and no lock is needed.

    def _resolve_track_id(self):
        """Which skipback track this ring belongs to, derived from the ring
        path rather than hardcoded to 0. realpath() follows the legacy
        ~/.skipbuf/ring.raw -> t0/ring.raw symlink, so the parent directory is
        the recorder's real per-track dir. Returns None if it isn't a t<N> dir
        (e.g. the recorder's documented proto_t0), in which case channels stay
        as they are -- guessing would be worse than leaving them alone."""
        try:
            parent = os.path.basename(os.path.dirname(os.path.realpath(self.ring_path)))
        except Exception:
            return None
        if len(parent) > 1 and parent[0] == "t" and parent[1:].isdigit():
            return int(parent[1:])
        return None

    def _resolve_channels(self, st_size):
        """Channel count for this ring, from the tracks JSON -- the same source
        _sb_track0_ports(), orc1_fire._route_ports() and skipback.sh already
        use, so all four agree.

        The tracks JSON is DESIRED state and can disagree with the ring on disk
        (a <=5s reconcile lag, or unboundedly while a track is torn down or
        RAM-blocked). So a candidate is only accepted when it actually DIVIDES
        the real file: st_size % (rate * ch * 2) == 0 and the implied length
        matches the configured length_secs. If it doesn't check out we keep the
        current value rather than trusting the file."""
        tid = self._resolve_track_id()
        if tid is None:
            return self.channels
        try:
            import json
            with open(self.tracks_path) as f:
                tracks = json.load(f).get("tracks", [])
        except Exception:
            return self.channels
        for t in tracks:
            if not isinstance(t, dict) or t.get("id") != tid:
                continue
            ch = 2 if t.get("stereo", True) else 1
            try:
                length = int(t.get("length_secs", 0))
            except (TypeError, ValueError):
                return self.channels
            # Cross-check against the real file before believing it.
            if length > 0 and st_size == self.rate * ch * 2 * length:
                return ch
            return self.channels        # config disagrees with disk: keep ours
        return self.channels            # no such track in the config

    def refresh_geometry(self):
        """Re-resolve ring SIZE and CHANNELS together from the live ring.
        Returns True if the geometry changed (callers must then treat any
        remembered ring offsets as invalid).

        Size comes from os.stat, not a config constant. A dangling symlink or a
        zero-length file (init_ring's open(path,"wb") truncate window) leaves
        the last-known geometry in place -- we never fall back to the static
        constant mid-session, because that is the bug being fixed.

        Change key is (st_size, st_ino, channels). st_ino is load-bearing:
        disarm->rearm produces the SAME size and channels with a NEW inode and
        a zeroed ring (without it, a nudge would re-slice into fresh silence
        under a playing deck), and a recorder restart with an edited config
        skips teardown entirely and can land the same size AND inode with
        different geometry -- which is why channels is in the key too."""
        try:
            st = os.stat(self.ring_path)        # follows the t0/ symlink
        except OSError:
            return False
        if st.st_size <= 0:
            return False
        ch = self._resolve_channels(st.st_size)
        key = (st.st_size, st.st_ino, ch)
        if key == self._geo_key:
            return False
        changed = self._geo_resolved            # first resolve is not a "change"
        self._geo_key = key
        self._geo_resolved = True
        self.channels = int(ch)
        # bytes_per_s MUST be recomputed with channels: under mono it doubles,
        # which inflates head_info()'s extrapolation AND its 1s clamp, pushing
        # `behind` past the flushed head.
        self.bytes_per_s = self.rate * self.channels * 2
        self.buffer_bytes = int(st.st_size)
        return changed

    def _read_pos(self):
        try:
            with open(self.pos_path) as f:
                return int(f.read().strip()) % self.buffer_bytes
        except Exception:
            return 0

    def available(self):
        """True if the ring file exists and is a whole number of frames.

        This used to compare getsize() against a config-derived buffer_bytes.
        Now that buffer_bytes IS getsize(), that form degenerates to
        `getsize(x) == getsize(x)` -- measured True even against a completely
        wrong ring, i.e. a reassuring lie. Only _selftest calls it."""
        try:
            size = os.path.getsize(self.ring_path)
        except OSError:
            return False
        frame = self.channels * 2
        return size > 0 and size % frame == 0

    def head_info(self):
        """Return (pos, est_extra_bytes).

        `pos` = the last FLUSHED write head — every byte behind it is
        guaranteed written. `est_extra` = how many bytes the daemon has
        probably written SINCE that flush, estimated from the pos file's
        mtime (it's rewritten atomically with every ~43ms chunk, so mtime ==
        'when pos was true'). pos+est_extra ≈ the head right NOW — use it as
        the time reference for offsets, but never read past `pos`. est_extra
        is clamped to 1s: a stale mtime means the daemon is down, at which
        point extrapolation would be fiction."""
        try:
            st = os.stat(self.pos_path)
            with open(self.pos_path) as f:
                pos = int(f.read().strip()) % self.buffer_bytes
            extra = max(0.0, time.time() - st.st_mtime) * self.bytes_per_s
            frame = self.channels * 2
            extra = int(extra) - int(extra) % frame
            return pos, min(extra, self.bytes_per_s)
        except Exception:
            return self._read_pos(), 0

    def snapshot(self, seconds, mono=True, end_byte=None):
        """
        Return the last `seconds` of audio ending at `end_byte` (a ring byte
        offset; default = the current write head).

        Returns float32 in [-1, 1]: shape (N,) if mono else (N, 2).
        Uses the exact wrap math from skipback.sh. Read pos first, then the
        span — a torn sample at the wrap point is inaudible and needs no lock.
        """
        seconds = max(0.01, float(seconds))
        n_frames = int(seconds * self.rate)
        return self.snapshot_frames(n_frames, mono=mono, end_byte=end_byte)

    def snapshot_frames(self, n_frames, mono=True, end_byte=None):
        """Frame-exact snapshot: the last `n_frames` frames ending at
        `end_byte` (default = current write head). Frame precision matters
        for loops — the old seconds->deciseconds path rounded loop length to
        100ms, which drifted audibly against the sequencer on every pass."""
        frame = self.channels * 2
        bytes_needed = max(frame, int(n_frames) * frame)
        bytes_needed = min(bytes_needed, self.buffer_bytes)

        end = self._read_pos() if end_byte is None else int(end_byte) % self.buffer_bytes
        end -= end % frame
        start = (end - bytes_needed) % self.buffer_bytes

        # A missing / renamed / disarmed ring must never raise out of here.
        # This runs on the synth's MIDI thread, and an exception escaping used
        # to exit the `with mido.open_input(...)` block in _midi_thread and
        # drop every message for 2s -- note-offs vanished and held notes hung.
        # Returning b"" decodes to a clean 0-length array below, which both
        # callers already handle correctly (_load_slice bails on
        # len(buf) < frames; _selftest guards).
        try:
            with open(self.ring_path, "rb") as ring:
                if start < end:
                    ring.seek(start)
                    raw = ring.read(bytes_needed)
                else:
                    # wraps the end of the file
                    ring.seek(start)
                    raw = ring.read(self.buffer_bytes - start)
                    ring.seek(0)
                    raw += ring.read(end)
        except Exception as e:
            print(f"[sampler] ring read failed ({self.ring_path}): {e}",
                  flush=True)
            raw = b""

        # Whole frames only. np.frombuffer raises ValueError on an odd byte
        # count, and reshape(-1, 2) on an odd sample count. Both become
        # reachable once buffer_bytes is derived from os.stat instead of a
        # config constant (a short read at the wrap, or a ring whose size
        # isn't a whole number of frames), so this is a dependency of the
        # geometry fix -- not polish.
        raw = raw[:len(raw) - len(raw) % frame]
        try:
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if self.channels == 2:
                audio = audio.reshape(-1, 2)
                if mono:
                    audio = audio.mean(axis=1)
        except Exception as e:
            print(f"[sampler] ring decode failed ({len(raw)}B): {e}",
                  flush=True)
            audio = (np.zeros((0, 2), dtype=np.float32)
                     if (self.channels == 2 and not mono)
                     else np.zeros(0, dtype=np.float32))
        return np.ascontiguousarray(audio, dtype=np.float32)


# ==============================================================================
# TONE SHAPING  —  3-band EQ + one-knob sweep filter (per deck)
# ==============================================================================
# Both classes are vectorised RBJ-cookbook biquads run through
# scipy.signal.lfilter, the same trick orc1_synth.BiquadFilter uses: a
# continuously-playing deck costs microseconds of C per block instead of a
# per-sample Python loop, and the two-sample filter state (zi) carries across
# render() calls so there's no click at block boundaries. Coefficients are
# only rebuilt when the (glide-smoothed) knob value actually moves enough to
# matter. Each filter is its own transparent bypass at its flat/center
# default, and degrades to passing audio through untouched if scipy is
# somehow unavailable -- never crashes, worst case is "no EQ".

def _biquad_process(filt, x):
    """Run one biquad block (filt exposes _b/_a/_zi/_bypass), carrying state.
    Bypass is a cheap passthrough. Knob-sweep declicking is NOT done here -- it
    lives in SampleDeck._apply_tone, which glides the parameters and runs the
    block in sub-blocks so the coefficient change is spread across the block
    instead of stepping at the seam (measured: without it the sweep filter
    zippers ~4x the local step; with it ~1.7x, 2026-08-12)."""
    if filt._bypass or _lfilter is None:
        return x
    y, filt._zi = _lfilter(filt._b, filt._a, x, zi=filt._zi)
    return y.astype(np.float32)


class _ShelfPeakFilter:
    """One band of a 3-band EQ: low shelf, high shelf, or mid peak, with a
    gain in dB (0 = flat). `kind` is 'LOWSHELF' | 'HIGHSHELF' | 'PEAK'."""

    def __init__(self, sr, kind, freq_hz, q=0.9):
        self.sr      = sr
        self.kind    = kind
        self.freq    = float(freq_hz)
        self.q       = float(q)
        self.gain_db = 0.0
        self._zi     = np.zeros(2, dtype=np.float64)
        self._design()

    def set_gain(self, gain_db):
        gain_db = float(np.clip(gain_db, -15.0, 15.0))
        if abs(gain_db - self.gain_db) < 0.05:
            return
        self.gain_db = gain_db
        self._design()

    def _design(self):
        self._bypass = abs(self.gain_db) < 0.05
        if self._bypass or _lfilter is None:
            return
        A  = 10.0 ** (self.gain_db / 40.0)
        w0 = 2.0 * np.pi * self.freq / self.sr
        cw, sw = np.cos(w0), np.sin(w0)
        alpha  = sw / (2.0 * self.q)

        if self.kind == "PEAK":
            b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
            a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
        else:
            sq = 2.0 * np.sqrt(A) * alpha
            if self.kind == "LOWSHELF":
                b0 =      A * ((A + 1) - (A - 1) * cw + sq)
                b1 =  2 * A * ((A - 1) - (A + 1) * cw)
                b2 =      A * ((A + 1) - (A - 1) * cw - sq)
                a0 =          (A + 1) + (A - 1) * cw + sq
                a1 =     -2 * ((A - 1) + (A + 1) * cw)
                a2 =          (A + 1) + (A - 1) * cw - sq
            else:   # HIGHSHELF
                b0 =      A * ((A + 1) + (A - 1) * cw + sq)
                b1 = -2 * A * ((A - 1) + (A + 1) * cw)
                b2 =      A * ((A + 1) + (A - 1) * cw - sq)
                a0 =          (A + 1) - (A - 1) * cw + sq
                a1 =      2 * ((A - 1) - (A + 1) * cw)
                a2 =          (A + 1) - (A - 1) * cw - sq
        self._b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
        self._a = np.array([1.0,     a1 / a0,  a2 / a0], dtype=np.float64)

    def process(self, x):
        return _biquad_process(self, x)


class _SweepFilter:
    """One-knob DJ-style filter: center (0) = bypass. Negative sweeps a
    low-pass closed (darker); positive sweeps a high-pass open (thinner).
    A little resonance builds in toward the extremes for character."""

    def __init__(self, sr):
        self.sr  = sr
        self.pos = 0.0                    # -1..+1
        self._zi = np.zeros(2, dtype=np.float64)
        self._design()

    def set_position(self, pos):
        pos = float(np.clip(pos, -1.0, 1.0))
        if abs(pos - self.pos) < 0.002:
            return
        self.pos = pos
        self._design()

    def _design(self):
        self._bypass = abs(self.pos) < 0.01
        if self._bypass or _lfilter is None:
            return
        # Symmetric one-knob sweep: full range on BOTH sides so each end is a
        # real effect. LP sweeps 20kHz->28Hz (fully dark); HP sweeps 28Hz->
        # 20kHz (fully thin). The old curve only swept the HP up to 6kHz with
        # heavy resonance, so turning the knob "up" did almost nothing audible
        # -- it felt biased to the low-pass side (measured: HP centroid barely
        # moved). Cutoff is log-mapped so the sweep is perceptually even, and
        # resonance is kept mild (no whistling / level bump at the extremes).
        if self.pos < 0:
            t      = -self.pos
            cutoff = 20000.0 * (28.0 / 20000.0) ** t        # 20kHz -> 28Hz
            mode   = "LP"
        else:
            t      = self.pos
            cutoff = 28.0 * (20000.0 / 28.0) ** t           # 28Hz -> 20kHz
            mode   = "HP"
        cutoff = min(cutoff, self.sr * 0.45)
        q = 0.707 + 0.7 * (abs(self.pos) ** 2)   # mild resonance at the extremes
        w0 = 2.0 * np.pi * cutoff / self.sr
        cw, sw = np.cos(w0), np.sin(w0)
        alpha = sw / (2.0 * q)
        if mode == "HP":
            b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        else:
            b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0 = 1 + alpha
        self._b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
        self._a = np.array([1.0, (-2 * cw) / a0, (1 - alpha) / a0],
                           dtype=np.float64)

    def process(self, x):
        return _biquad_process(self, x)


def _onset_index(seg, naive_idx, sr, sensitivity):
    """Find the sample index of the strongest transient in `seg` (mono float),
    biased toward `naive_idx` (the un-snapped loop start), or None if there is
    no clear onset. Used to snap a press-grabbed loop's start onto the real
    downbeat. Pure numpy, no I/O -- unit-tested directly with synthetic buffers.

    Method: a coarse per-frame RMS envelope, its half-wave-rectified rise
    (spectral-flux-lite), then the rising frame whose (rise - mild distance
    penalty) score is largest. Gated so flat/sustained material -- where the
    rise never clears `sensitivity` x the loudest frame -- returns None and the
    caller leaves the press start untouched. Distance-weighting keeps it snapping
    to the NEAREST strong transient, so it can only make a small correction, never
    jump to a different beat."""
    n = len(seg)
    if n < 64 or sr <= 0:
        return None
    hop = max(1, int(round(sr * 0.002)))              # 2 ms frames
    nf  = n // hop
    if nf < 8:
        return None
    x = np.asarray(seg[:nf * hop], dtype=np.float64).reshape(nf, hop)
    env = np.sqrt(np.mean(x * x, axis=1) + 1e-20)     # per-frame RMS
    peak = float(env.max())
    if peak < 1e-4:                                   # essentially silence
        return None
    # A short RMS frame RIPPLES within each cycle of low-pitched content (a 2 ms
    # window of a 55 Hz sine swings from ~0 at a zero-crossing to full at a
    # crest), so a raw frame-to-frame rise flags steady bass as an onset. Judge
    # each frame by its CONTRAST against a trailing-median baseline instead:
    #   contrast = (env - baseline) / (env + baseline)   in [0, 1]
    # A real transient jumps far above its recent baseline (contrast -> 1); the
    # sub-cycle ripple of a steady tone barely clears its own median (~0.1), so
    # the sensitivity gate rejects it. K spans >~1 cycle of typical bass, and
    # the first K frames are skipped (their baseline sample is too small to be a
    # stable reference) -- which also keeps a snap to a SMALL correction.
    K = max(3, int(round(0.030 / 0.002)))             # ~30 ms baseline / warm-up
    contrast = np.zeros(nf)
    for i in range(K, nf):
        b = float(np.median(env[i - K:i]))
        e = float(env[i])
        contrast[i] = (e - b) / (e + b + 1e-20)
    contrast[contrast < 0.0] = 0.0
    contrast[env < 0.15 * peak] = 0.0                 # ignore near-silent frames
    # An onset is the LEADING EDGE of a transient -- the first frame whose
    # contrast crosses the sensitivity gate. Its contrast stays high for several
    # frames after the attack (a plateau), so picking the argmax would land on
    # the plateau's trailing edge, not the attack. Take rising crossings and
    # keep the one NEAREST the naive start (so a snap is a small correction, and
    # among several transients the closest downbeat wins).
    above = contrast >= sensitivity
    if not np.any(above):
        return None
    edges = list(np.where(above[1:] & ~above[:-1])[0] + 1)   # rising crossings
    if not edges:                                      # already above at frame K
        edges = [int(np.argmax(above))]
    # One transient's contrast plateau can briefly dip below the gate, making a
    # spurious second crossing a few frames later. Merge crossings closer than
    # ~a plateau (K frames) into ONE onset, kept at its FIRST (attack) edge;
    # genuinely separate downbeats are far more than K frames apart.
    onsets, last = [edges[0]], edges[0]
    for ed in edges[1:]:
        if ed - last > K:
            onsets.append(ed)
        last = ed
    naive_f = naive_idx / float(hop)
    onsets = np.array(onsets)
    k = int(onsets[int(np.argmin(np.abs(onsets - naive_f)))])
    # Place the start at the beginning of the jumping frame so the attack is
    # INSIDE the loop (a start a hair early leaves a tiny lead-in; a hair late
    # clips the transient -- early is the safer side of a downbeat).
    return int(k * hop)


# ==============================================================================
# SAMPLE DECK  —  one scrubbable, gated, looping voice
# ==============================================================================

class SampleDeck:
    """
    One live sample deck. Holds a captured buffer and plays it with
    variable-rate interpolation (same technique as SampleVoice._render_signal).

    Realtime rules:
      * render() only reads state; it never allocates the buffer or does I/O.
      * State changes (buffer swap, gate, speed, scrub) come from the control
        thread and are plain attribute writes — a stale read for one block is
        harmless.
    """

    def __init__(self, name, sample_rate=48000, click_fade_ms=4.0,
                 jog_sensitivity=1.0, jog_scratch_sensitivity=None,
                 out_rate=None):
        self.name   = name
        self.sr     = int(sample_rate)
        # JACK output rate. The buffer + ALL capture/fade/phase/region/nudge math
        # run at self.sr (the ring's 48k, where they are correct and pinned); when
        # the JACK server runs at a different rate (a per-card jackd at 44100, e.g.
        # the Mixtrack), render() scales ONLY its read-step by sr/out_rate, so a 48k
        # buffer plays at the right pitch AND loop length. The deck's own fractional
        # interpolating read is the resampler. Defaults to sr -> ratio 1.0 ->
        # byte-identical to the single-rate world; nothing but the read-step uses it.
        self._out_rate   = int(out_rate) if out_rate else int(sample_rate)
        self._rate_ratio = self.sr / float(self._out_rate)
        self._data  = np.zeros(1, dtype=np.float32)   # active buffer
        self._pos   = 0.0
        self._rate  = 1.0                             # playback speed (pitch+tempo)
        self._target_rate = 1.0                       # where _rate is gliding to
        # The Mixtrack's pitch fader (mapped to speed) is an absolute-position
        # control -- any touch or brush jumps straight to wherever the fader
        # physically sits, and set_speed() used to write _rate instantly. That
        # combination is what made speed feel like it "had a mind of its own":
        # a bumped fader snapped the resampled pitch/tempo immediately. Now
        # set_speed() only moves the target; render() glides _rate toward it
        # over ~SPEED_GLIDE_FRAMES, so both accidental brushes and deliberate
        # moves sound like an intentional pitch bend instead of a jump-cut.
        self._SPEED_GLIDE_FRAMES = max(1, int(self.sr * 0.04))   # ~40ms glide
        self._bend_saved = None   # pre-bend speed while PITCH +/- is held
        self._gain  = 1.0
        # Playing state is split into two independent flags so a momentary
        # control (hold-to-play) and a latch control (loop toggle) can't
        # stomp on each other. What used to be one shared "_gate" bool meant
        # releasing the hold-to-play pad would silence a deck that had been
        # independently latched into a running loop -- looked like the
        # sampler stopping itself for no reason. Audible playback is now
        # `_held or _latched`; see _playing().
        self._held    = False                         # momentary gate down?
        self._latched = False                         # loop-toggle latch on?
        self._loop  = True                            # decks loop by default
        # One-shot audition (the /sampler PLAY button): play the buffer through
        # exactly once, then fade out and rewind on its own.  While set it makes
        # render() take the play-through-and-stop path instead of the _loop wrap
        # -- the path that was otherwise dead code, since _loop is always True.
        self._oneshot = False
        # Play/loop REGION, stored as fractions (0..1) of the buffer.  ONE
        # region is both the trim (audition/PLAY runs start->end) and the loop
        # (LOOP wraps start->end).  The defaults span the whole buffer, so every
        # render/transport path below reduces EXACTLY to its pre-region form
        # (si=0, ei=n).  Reset to full on a fresh capture / file load; kept
        # across a nudge (the window shifts, the region stays proportional).
        self._start_frac = 0.0
        self._end_frac   = 1.0
        # Settable HOTCUES: four memory points, each a fraction (0..1) of the
        # buffer or None if unset.  DJ semantics -- press an empty pad to SET a
        # cue at the live playhead, press a set pad to JUMP there (see cue()).
        # EPHEMERAL, like the region: a fresh capture / file load wipes them,
        # because the fractions are meaningless against a different buffer.
        self._cues = [None, None, None, None]
        # Momentary BEAT-REPEAT: while held, the region is squeezed to a tight
        # loop at the live playhead; _br_saved holds the region to restore on
        # release (None = not engaged).  It IS just a very short loop region, so
        # it reuses set_bounds' clamp + playhead-pull + declick -- no new render
        # code.  EPHEMERAL, like the region/cues: a fresh capture/load clears it.
        self._br_saved = None
        # Jog scrub is modeled as a continuous playback VELOCITY (can be
        # negative = reverse), not a position jump. The old approach moved
        # `_pos` by a lump of frames every jog tick, which is a sample-domain
        # teleport -- audible as pops/crud no matter how it's declicked,
        # because the waveform itself is discontinuous at the jump. Treating
        # jog input as an extra rate that decays back to 0 when jogging
        # stops keeps the read position scanning continuously, so reverse
        # sounds like tape running backward instead of glitching.
        # ── Jog / platter model (rev. 2026-07-27: true record feel) ─────────
        # The jog is a FLYWHEEL whose velocity tracks how fast you spin the
        # wheel: faster spin = faster playback, slower = slower, backward =
        # reverse. The earlier fix made reverse work but played at ONE fixed
        # scrub speed regardless of how hard you spun (it used a single tick's
        # size, ignoring spin RATE -- the Mixtrack mostly emits +/-1 ticks and
        # encodes speed in how OFTEN they arrive). Now every jog tick adds an
        # impulse and render() turns the impulses-per-block into a smoothed
        # platter velocity, so both how far AND how fast you turn map to speed.
        # When you stop turning, the platter eases back to normal playback (the
        # "motor") -- unless a finger is on the capacitive top (scratch), where
        # it instead coasts to silence, like braking a record by hand.
        self._jog_impulse = 0.0   # frames of jog travel since last render (control thread writes)
        self._jog_vel     = 0.0   # current platter velocity (playback-rate units; <0 = reverse)
        self._jog_active  = 0     # frames left in the "still being spun" window
        self._jog_engaged = False # True only while the flywheel is driving the deck.
                                  # A normal PLAY leaves this False, so playback
                                  # starts AT speed instead of spinning up from 0.
        self._jog_step_prev = 1.0 # the read-rate (playback speed) at the END of
                                  # the last render block. render() uses it to
                                  # ramp the rate smoothly ACROSS a block when
                                  # the speed is changing (scratch / spin-up /
                                  # spin-down), so the pitch slides continuously
                                  # instead of jumping in ~21ms stairs — the
                                  # stair-stepping is what makes a scrub sound
                                  # grainy/gurgly rather than like a real record.
        # Flywheel: each jog tick ACCELERATES the platter; friction bleeds the
        # velocity back toward the rest speed every block, so when your hand
        # slows or stops, the audio smoothly slows too (instead of holding the
        # last speed -- the "glitchy, doesn't slow down" feel).
        #
        # TWO acceleration gains, because the two gestures feel completely
        # different at the same gain:
        #   * FREE-SPIN (no finger on the platter top): the record plays at
        #     normal speed and friction constantly pulls it back toward normal,
        #     so the jog only BENDS the pitch while you keep spinning. Needs a
        #     HIGH gain -- a low one is dragged back to 1x so fast the bend is
        #     imperceptible (this was the "can't bend without touching" bug).
        #   * SCRATCH (finger down): the platter's rest speed is a dead STOP, so
        #     the same movement reads dramatically (you're moving away from
        #     silence). Wants a LOWER gain or it's twitchy.
        # render() picks the right one each block from self._scratch. If a
        # scratch gain isn't supplied, fall back to the free-spin one.
        _scr_sens = (jog_sensitivity if jog_scratch_sensitivity is None
                     else jog_scratch_sensitivity)
        self._JOG_ACCEL_FREE    = 0.0052 * float(jog_sensitivity)   # no-touch bend
        self._JOG_ACCEL_SCRATCH = 0.0052 * float(_scr_sens)         # finger-down scratch
        # ── Platter physics (rev. 2026-08-01: mimic an actual record) ──────
        # Velocity approaches its rest target exponentially with a TIME
        # CONSTANT, and which constant applies depends on what is touching the
        # platter -- a hand and a motor are not the same thing:
        #
        #   * BRAKE  (finger down): a hand grips the record and stops it fast.
        #     The pitch falls to a halt in a couple of hundred ms.
        #   * MOTOR  (finger off): direct drive pulls the platter back up to
        #     speed.  A Technics SL-1200 reaches 33rpm in ~0.7s, which is
        #     about a 0.25s time constant -- slow enough that you HEAR the
        #     pitch rise when you let go, which is the sound everyone
        #     recognises as releasing a record.
        #
        # This replaced a single fixed per-block retention factor.  It is also
        # what removes the touch/release clicks: the old code TELEPORTED the
        # platter velocity (to 0 on touch, to full rate on release), and a
        # velocity teleport with a flat amplitude gate on top of it is a step
        # discontinuity no declicker can hide.  Braking and spinning up are
        # continuous, so there is nothing to hide.
        self._MOTOR_TAU_S = 0.06             # motor pulls platter to speed (0.15 = sluggish return)
        self._BRAKE_TAU_S = 0.02             # hand stops platter (0.06 = loose/slipping; 0.02 = vinyl grip)
        self._JOG_MAX_VEL  = 2.0             # clamp a hard spin (4.0 reached too easily; 2.0 = fast not fire)
        # A real groove makes no sound only when it is genuinely not moving.
        # The value this replaces was 0.15 -- i.e. the deck went silent at 15%
        # speed, roughly two and a half octaves down, which is very much still
        # audible on a record.  That is why a slowed platter vanished instead
        # of dropping in pitch.
        self._SILENT_SPEED = 0.01  # gate below 1% of normal speed (0.02 caught brake-decay artifact at tight BRAKE_TAU)
        # Motor-driven turnaround is a SEPARATE regime from hand braking.  When
        # the reverse button flips the motor, the platter whips through zero in
        # a single ~21ms block (tight MOTOR_TAU) -- the closest it is ever
        # sampled is ~1.8% speed -- so the 1% hand-braking gate above never
        # bites and the deck thumps through the flip at full level.  A wider
        # gate here ducks that one near-zero block to near-silence (the moment
        # a stopped groove makes no sound) without touching the hand-braking
        # decay feel.  Only affects the motor case (_jog_engaged, no hand): a
        # settled slow bend has _jog_engaged False, so it is unaffected.
        self._SILENT_SPEED_MOTOR = 0.06  # gate below 6% of normal speed on motor spin-up / turnaround
        # Reverse-button turnaround: brake the platter to zero, HOLD it silent
        # for a beat, then spin up the other way -- the stop-and-flip of a
        # hand-reversed record.  Riding the tight MOTOR_TAU straight through
        # zero crosses in under one block, so there is no silent instant at all
        # (a near-DC thump).  _turn_pending arms the hold when the flip is
        # requested; the hold (_turn_dwell_left frames) begins the moment the
        # braking platter actually reaches zero, then releases into reverse.
        # Only the reverse BUTTON path arms this -- jogging the wheel is left
        # to ride the flywheel unchanged.
        self._TURN_DWELL_FRAMES = int(self.sr * 0.035)   # ~35ms silent hold at the flip
        self._turn_dwell_left   = 0
        self._turn_pending      = False
        # Play the record backwards.  Kept separate from _rate (which stays a
        # positive magnitude) so the pitch fader, the speed glide and the
        # momentary bend all keep working unchanged in either direction --
        # reverse only flips which way the motor is pulling the platter.
        self._reverse = False
        self._JOG_ACTIVE_FRAMES = max(1, int(self.sr * 0.12))   # ~120ms "still spinning" window
        # Vinyl / turntable capacitive mode (finger on the platter top). When
        # on, a spun-then-held platter coasts to SILENCE (braked record) instead
        # of resuming playback; releasing resumes the motor.
        self._scratch = False
        # ── Declick (rev. 2026-08-01) ──────────────────────────────────────
        # The mechanism this replaces was a fade-IN FROM ZERO: on any
        # discontinuity it ramped the new block up from 0.0.  That is only
        # correct when the previous output was ALREADY silence.  With the deck
        # sounding it CREATES the step it exists to prevent -- the output drops
        # to digital zero for one sample and then ramps back up.  Measured at
        # 6.7x the signal's own maximum slope on a platter touch, a hotcue
        # jump, a needle-drop seek and a buffer swap alike, and the stop paths
        # had no ramp at all (render() simply returned None).  See
        # _test_sampler_dsp.py, which fails on every one of those if this is
        # reverted.
        #
        # Two mechanisms replace it, because stopping and teleporting are
        # different problems:
        #   * RELEASE -- stop / gate-off fade the REAL AUDIO out over
        #     _fade_frames, so the deck stops rather than being cut off.
        #   * OFFSET DECAY -- an in-place jump (hotcue, seek, scratch engage,
        #     buffer swap) leaves a step between the last sample emitted and
        #     the first new one.  Add that difference onto the new content and
        #     decay it to zero over a short window: the step disappears with no
        #     lookahead and no gap.  This also subsumes the old fade-in --
        #     starting from silence gives an offset of -new[0], which IS a ramp
        #     up from zero.
        self._fade_frames   = max(1, int(self.sr * click_fade_ms / 1000.0))
        self._release_left  = 0       # >0 => fading the audio out to a stop
        self._rewind_after_release = False
        self._DC_FRAMES     = max(8, int(self.sr * 0.0015))   # ~1.5ms
        self._dc_pending    = False   # a discontinuity happened; measure it
        self._dc_off        = 0.0     # the step currently being decayed away
        self._dc_left       = 0
        self._last_out      = 0.0     # last sample actually emitted
        # Scratch gate level at the END of the last block.  The gate
        # (amp = |speed|/_SILENT_SPEED) used to be applied FLAT across
        # the whole block, so every block boundary during a brake was a gain
        # step -- measured at up to 3.0x between adjacent blocks, heard as
        # grit throughout the gesture.  render() now ramps it across the block,
        # the same way _jog_step_prev already ramps the read RATE.
        self._amp_prev      = 1.0

        # 3-band EQ + sweep filter (Mixtrack EQ_HI/EQ_MID/EQ_LOW/FILTER
        # knobs). Targets glide in the same way _target_rate does above --
        # a knob bump shouldn't zipper the tone any more than a fader bump
        # should snap the pitch.
        self._eq_lo_db  = 0.0; self._eq_lo_target  = 0.0
        self._eq_mid_db = 0.0; self._eq_mid_target = 0.0
        self._eq_hi_db  = 0.0; self._eq_hi_target  = 0.0
        self._filt_pos  = 0.0; self._filt_target   = 0.0
        # ~80ms. Was 30ms, but at a 1024-frame block that is glide=0.71 -- each
        # block jumped 71% of the way to target, a big per-block coefficient
        # step. Together with the sub-block gliding below this slower glide keeps
        # the active-region sweep gentle without feeling sluggish.
        self._TONE_GLIDE_FRAMES = max(1, int(self.sr * 0.08))   # ~80ms
        # While a tone knob is MOVING, glide+process the block in this many
        # sub-blocks so the biquad coefficient change is spread across the block
        # instead of stepping once at the seam (measured: sweep-filter zipper
        # 4.9 -> 1.8, boundary-vs-local, 2026-08-12). Only pays the extra
        # lfilter calls during the (transient) sweep; steady = 1 block.
        self._TONE_SUBBLOCKS = 8
        self._eq_lo  = _ShelfPeakFilter(self.sr, "LOWSHELF",   220.0)
        self._eq_mid = _ShelfPeakFilter(self.sr, "PEAK",      1000.0, q=1.0)
        self._eq_hi  = _ShelfPeakFilter(self.sr, "HIGHSHELF", 3200.0)
        self._filt   = _SweepFilter(self.sr)

        # Phase 2 TODO: _reverse, _hotcues[4], stereo buffers.

    # ---- control-thread API (atomic-ish attribute writes) -------------------

    def load_buffer(self, data):
        """Arm a freshly captured buffer. Called via the synth's queue-drain."""
        if data is None or len(data) < 2:
            data = np.zeros(1, dtype=np.float32)
        self._data = np.ascontiguousarray(data, dtype=np.float32)
        self._pos  = 0.0
        self._jog_step_prev = self._drive()               # fresh onset: no rate-glide from a stale speed
        self._dc_pending = True                        # declick the onset
        self._cancel_release()                         # cancel any fade-out
        self._oneshot = False                          # a fresh capture loops, as before
        self._latched = True                           # capture auto-arms looped play
        self._start_frac = 0.0                          # fresh capture plays the whole buffer
        self._end_frac   = 1.0
        self._cues = [None, None, None, None]           # cues meaningless against new audio
        self._br_saved = None                           # drop any held beat-repeat region

    def load_buffer_cued(self, data):
        """Load a buffer STOPPED at position 0 (file-deck load). Unlike
        load_buffer (capture), loading a file shouldn't blast off instantly —
        a DJ loads a track cued and starts it on the LOOP/PLAY control."""
        if data is None or len(data) < 2:
            return
        was_playing = self._playing()
        self._data = np.ascontiguousarray(data, dtype=np.float32)
        self._pos  = 0.0
        self._held = False
        self._latched = False
        self._oneshot = False
        self._start_frac = 0.0                          # a freshly loaded file plays whole
        self._end_frac   = 1.0
        self._cues = [None, None, None, None]           # cues meaningless against new audio
        self._br_saved = None                           # drop any held beat-repeat region
        self._jog_vel = 0.0
        self._jog_step_prev = self._drive()               # fresh onset: no rate-glide from a stale speed
        self._dc_pending = True
        # Two different situations, and treating them alike breaks one of them:
        #
        #  * the deck was ALREADY stopped (or mid stop-fade) -- a cued load
        #    must stay SILENT.  Letting the in-flight fade run would render the
        #    NEW buffer and fade it out: audible content from a deck that is
        #    contractually stopped at position 0.
        #  * the deck was PLAYING -- clearing the gate flags alone makes the
        #    next render() return None straight from full level, a 24.6x cut
        #    mid-waveform.  This is the /sampler LOAD button.  Give it the same
        #    short fade stop() gets; the few ms of new content at a decaying
        #    level is exactly what stopping sounds like, and is what the deck
        #    already does for CUE.
        if was_playing:
            self._release_left = self._fade_frames
            self._rewind_after_release = False
        else:
            self._cancel_release()

    def swap_buffer(self, data):
        """Replace the buffer while KEEPING play state and relative position —
        used by the post-capture window nudge, so adjusting a loop's start/end
        by ear doesn't restart or silence it."""
        if data is None or len(data) < 2:
            return
        old_n = len(self._data)
        frac  = (self._pos / (old_n - 1)) if old_n > 1 else 0.0
        self._data = np.ascontiguousarray(data, dtype=np.float32)
        self._pos  = float(frac * (len(self._data) - 1))
        self._oneshot = False                          # a window nudge is not a one-shot
        self._dc_pending = True                        # declick the swap
        # Drop only the pending REWIND (it would clobber the position we just
        # nudged to) -- keep any stop fade running.  _cancel_release() zeroes
        # _release_left too, which threw away the fade stop() had armed: CUE
        # then nudge-by-ear is an ordinary gesture, and it cut the deck off
        # mid-waveform at 24.6x instead of letting it fade.
        self._rewind_after_release = False

    def _playing(self):
        return self._held or self._latched

    def _drive(self):
        """Signed playback rate -- the direction the motor is turning the
        platter.  _rate is always a positive MAGNITUDE so the pitch fader and
        the momentary bend behave identically in both directions."""
        return -self._rate if self._reverse else self._rate

    def _region_frames(self, n):
        """Integer [start, end) frame bounds of the play/loop region for a
        buffer of length n.  The region is stored as fractions; the defaults
        (0.0, 1.0) return (0, n) -- the whole buffer -- so every path reduces to
        its pre-region behaviour.  `end` is EXCLUSIVE and clamped so
        end - start >= 2, because the interpolator always needs a partner
        sample."""
        if n < 2:
            return 0, n
        si = int(self._start_frac * (n - 1))
        ei = int(round(self._end_frac * (n - 1))) + 1        # exclusive
        si = max(0, min(si, n - 2))
        ei = max(si + 2, min(ei, n))
        return si, ei

    def _rewind_pos(self):
        """Where 'rewind to the top' lands with a region set: the region START
        going forward, the region END playing in reverse (so a reversed clip has
        material to play back through -- mirrors audition())."""
        si, ei = self._region_frames(len(self._data))
        return float(ei - 1) if self._reverse else float(si)

    def set_bounds(self, start_frac, end_frac):
        """Set the play/loop region as fractions (0..1) of the buffer.  ONE
        region serves as both the trim (audition/PLAY runs start->end) and the
        loop (LOOP wraps start->end).  Edges are ordered and held at least a
        couple of frames apart so the loop math always has a partner sample.
        If the playhead ends up OUTSIDE the new region it is pulled to the
        nearest edge and declicked -- the same as a hotcue jump -- so dragging
        the start past a playing head doesn't leave it reading outside the
        loop."""
        s = float(min(max(start_frac, 0.0), 1.0))
        e = float(min(max(end_frac,   0.0), 1.0))
        if e < s:
            s, e = e, s
        n = len(self._data)
        min_frac = 2.0 / max(2, n - 1)          # >=2 frames of region
        if e - s < min_frac:
            e = min(1.0, s + min_frac)
            if e - s < min_frac:                # region pinned against the tail
                s = max(0.0, e - min_frac)
        self._start_frac = s
        self._end_frac   = e
        si, ei = self._region_frames(n)
        if self._pos < si or self._pos > ei - 1:
            self._pos = float(si if self._pos < si else ei - 1)
            self._dc_pending = True

    def set_bounds_start(self, frac):
        """Move only the region START (one controller knob / one drag handle)."""
        self.set_bounds(frac, self._end_frac)

    def set_bounds_end(self, frac):
        """Move only the region END."""
        self.set_bounds(self._start_frac, frac)

    def beat_repeat_on(self, length_frames):
        """Engage momentary BEAT-REPEAT: stutter a tight loop `length_frames`
        long, starting at the LIVE playhead, until beat_repeat_off().

        It is deliberately NOT new render code -- it just squeezes the play/loop
        region down to [pos, pos+len] via set_bounds(), which already clamps,
        pulls the playhead in and declicks.  The prior region is saved so the
        release restores exactly what was playing (a whittle, the full buffer,
        anything).  The save only happens on the FIRST engage, so a re-press
        (or a different subdivision) mid-hold can't overwrite the real region
        with the tight one -- the brain sends a fresh length each press.

        No-op on an empty deck or a nonsense length.  Does NOT force playback:
        if the deck is stopped nothing sounds and the release is a clean no-op,
        matching 'hold = stutter the loop that's already running'."""
        n = len(self._data)
        if n < 4 or length_frames < 2:
            return
        if self._br_saved is None:
            self._br_saved = (self._start_frac, self._end_frac)
        pos_frac = self._pos / (n - 1)
        len_frac = float(length_frames) / (n - 1)
        self.set_bounds(pos_frac, pos_frac + len_frac)

    def beat_repeat_off(self):
        """Release beat-repeat: restore the region saved at engage (a no-op if
        never engaged -- e.g. a stray note-off, or a capture cleared it)."""
        if self._br_saved is None:
            return
        s, e = self._br_saved
        self._br_saved = None
        self.set_bounds(s, e)

    def _cancel_release(self, honour_rewind=True):
        """Abort an in-flight stop fade because playback resumed.

        Two things must happen, and getting either wrong is a real bug that
        was actually shipped:

        * The pending rewind must be APPLIED, not merely discarded.  stop()
          defers its rewind until the fade completes, so a CUE-then-PLAY
          double-tap -- an utterly ordinary DJ gesture -- cancels the fade
          before it ever runs.  Just clearing the flag makes the deck relaunch
          from wherever CUE was pressed instead of the top, silently breaking
          CUE's "hard stop + rewind" contract.
        * The flag must be cleared either way.  Nothing else resets it except a
          release that completes UNINTERRUPTED, so leaving it set means the
          next ordinary gate-off -- which is meant to stop and HOLD position --
          completes, sees the stale flag, and snaps to 0: an unrequested rewind
          arriving long after the stop() that armed it.

        honour_rewind=False is for callers that have just set _pos themselves
        (jump, swap_buffer): an explicit position request supersedes a pending
        rewind, and applying it would clobber the position they just chose.
        """
        if honour_rewind and self._rewind_after_release:
            self._pos = self._rewind_pos()
        self._rewind_after_release = False
        self._release_left = 0

    def gate(self, on):
        """Momentary hold-to-play. Independent of the loop latch below --
        releasing this never stops a deck that's latched into a loop."""
        on = bool(on)
        self._oneshot = False                          # a held gate is not a one-shot
        if on and not self._playing():
            self._dc_pending = True                    # declick on (re)start
            self._cancel_release()                     # cancel any fade-out
        was = self._playing()
        self._held = on
        if on:
            si, ei = self._region_frames(len(self._data))
            if self._pos >= ei - 1 or self._pos < si:
                self._pos = self._rewind_pos()
        elif was and not self._playing():
            self._release_left = self._fade_frames     # fade out, don't cut

    def gate_toggle(self):
        """Latch play/stop — the live-looping start/stop. Independent of the
        momentary gate() above, so the two controls can't fight over one
        shared play flag (that conflict used to make loops stop the moment
        you touched the hold-to-play pad)."""
        was_playing = self._playing()
        self._oneshot = False                          # LOOP means loop, not a one-shot
        self._latched = not self._latched
        if self._latched and not was_playing:
            self._dc_pending = True
            self._cancel_release()                     # cancel any fade-out
            si, ei = self._region_frames(len(self._data))
            if self._pos >= ei - 1 or self._pos < si:
                self._pos = self._rewind_pos()
        elif was_playing and not self._playing():
            self._release_left = self._fade_frames     # fade out, don't cut

    def audition(self):
        """One-shot preview -- the /sampler PLAY button.

        Play the captured buffer through ONCE from the top, then fade out and
        rewind on its own.  Distinct from gate()/gate_toggle(), which run
        against the always-on _loop wrap and never stop by themselves.  We arm
        _oneshot so render() takes its play-through-and-stop branch; when that
        branch reaches the end of the buffer it disarms _oneshot and starts a
        release fade (see render()).

        Respects the deck's current speed and reverse direction: a reversed
        deck starts at the tail so there is a clip to play backwards through
        (starting a reversed deck at 0 would hit _pos < 0 on the first block
        and stop instantly)."""
        n = len(self._data)
        if n < 2:
            return
        self._cancel_release(honour_rewind=False)     # kill any fade; our pos wins
        self._oneshot = True
        self._held    = False
        self._latched = True                          # keep _playing() true, no held gate
        self._pos = self._rewind_pos()                # region start (or region end in reverse)
        self._jog_vel = 0.0
        self._jog_engaged = False
        self._jog_step_prev = self._drive()           # fresh onset: no rate-glide
        self._dc_pending = True                       # declick the jump to the start

    def set_speed(self, ratio):
        self._target_rate = float(np.clip(ratio, 0.01, 8.0))
        if self._bend_saved is not None:
            self._bend_saved = self._target_rate   # fader moved mid-bend

    def pitch_bend(self, on, up=True, factor=1.04):
        """Momentary beatmatch nudge (the PITCH +/- buttons): while held,
        playback runs slightly fast/slow; release returns to the pre-bend
        speed. Rides on the existing speed glide so it sounds like leaning
        on a record, not a jump-cut."""
        if on:
            if self._bend_saved is None:
                self._bend_saved = self._target_rate
            f = factor if up else 1.0 / factor
            self._target_rate = float(np.clip(self._bend_saved * f, 0.01, 8.0))
        elif self._bend_saved is not None:
            self._target_rate = self._bend_saved
            self._bend_saved = None

    def set_gain(self, g):
        self._gain = float(np.clip(g, 0.0, 2.0))

    def set_eq(self, band, gain_db):
        """band in 'hi'|'mid'|'low'. Sets the glide TARGET; render() eases
        the actual filter gain toward it every block (see _TONE_GLIDE_FRAMES)."""
        gain_db = float(np.clip(gain_db, -15.0, 15.0))
        if band == "hi":
            self._eq_hi_target = gain_db
        elif band == "mid":
            self._eq_mid_target = gain_db
        elif band == "low":
            self._eq_lo_target = gain_db

    def set_filter(self, pos):
        """pos: -1..+1, 0 = bypass. Sets the glide target (see set_eq)."""
        self._filt_target = float(np.clip(pos, -1.0, 1.0))

    def scrub(self, delta_frames):
        """One jog tick. Accumulate the wheel movement (frames of record
        travel) as an impulse and mark the jog 'active'. render() integrates
        the impulses that arrive per block into a smoothed platter velocity, so
        BOTH how far and how fast you spin the wheel translate into playback
        speed + direction (spin faster -> more/bigger ticks per block -> faster
        playback; backward ticks -> reverse)."""
        self._jog_impulse += float(delta_frames)
        self._jog_active = self._JOG_ACTIVE_FRAMES

    def stop(self):
        """Hard stop: silence the deck and rewind to the top (the obvious
        'stop' button -- unlike gate_toggle which only flips the latch).

        The rewind is DEFERRED until the fade-out finishes.  render() returns
        None the instant _playing() goes false, so zeroing _pos here used to
        cut the output from wherever the waveform happened to be straight to
        digital silence -- a full-scale step, measured at 6.7x the signal's
        own slope.  The deck now fades out over _fade_frames and rewinds when
        that completes; _release_left is what keeps render() alive to do it.
        """
        if self._playing():
            self._release_left = self._fade_frames
            self._rewind_after_release = True
        elif self._release_left <= 0:
            # Only rewind immediately when nothing is still fading. A second
            # stop() before the first fade finishes would otherwise jump _pos
            # to 0 mid-fade, so the tail would play from the top of the buffer
            # instead of from where the deck actually stopped -- a content
            # jump that neither the declicker nor the level ramp smooths.
            self._pos = self._rewind_pos()
        self._held = False
        self._latched = False
        self._oneshot = False
        self._jog_vel = 0.0
        self._jog_impulse = 0.0
        self._jog_active = 0
        self._jog_engaged = False
        self._jog_step_prev = self._drive()               # next onset starts clean, no rate-glide
        self._turn_dwell_left = 0
        self._turn_pending    = False

    def jump(self, frac):
        """Hotcue: jump to a fraction (0..1) of the buffer and play from there.
        Declicks the jump so it doesn't pop."""
        n = len(self._data)
        if n < 2:
            return
        frac = float(min(max(frac, 0.0), 0.999))
        self._pos = frac * (n - 1)
        self._oneshot = False                           # a hotcue is normal latched play
        if not self._playing():
            self._latched = True
            self._cancel_release(honour_rewind=False)   # the jump target wins
        self._dc_pending = True

    # ── Settable hotcues ─────────────────────────────────────────────────
    def _cue_ok(self, i):
        return isinstance(i, int) and 0 <= i < len(self._cues)

    def set_cue(self, i):
        """Store the LIVE playhead as cue `i` (0..3).  No-op on an empty deck
        (a cue against no audio is meaningless).  Stored as a FRACTION so it
        stays valid across a nudge/swap_buffer, exactly like the region."""
        if not self._cue_ok(i):
            return
        n = len(self._data)
        if n < 2:
            return
        self._cues[i] = float(min(max(self._pos / (n - 1), 0.0), 0.999))

    def cue(self, i):
        """DJ hotcue press: SET the cue if it is empty, otherwise JUMP to it
        and play.  One action so the deck (the state authority) decides -- the
        controller never has to know whether a pad is armed."""
        if not self._cue_ok(i):
            return
        if self._cues[i] is None:
            self.set_cue(i)
        else:
            self.jump(self._cues[i])

    def clear_cue(self, i):
        """Forget cue `i` (SHIFT+pad)."""
        if self._cue_ok(i):
            self._cues[i] = None

    def set_scratch(self, on):
        """Finger on / off the capacitive platter.

        Physical model, NOT a mute.  Putting a hand on a spinning record does
        not silence it -- it BRAKES it: the pitch falls as the platter slows
        and the sound stops when the groove stops.  Taking the hand off lets
        the motor pull it back up to speed, and you hear the pitch rise.

        So both directions leave the platter velocity exactly where it is and
        only change what it is heading TOWARDS (see `rest` in render()) and
        how fast it gets there (BRAKE vs MOTOR time constant).  The previous
        version teleported _jog_vel to 0 here, which is what made a platter
        touch cut to silence in one sample -- measured at 6.7x the signal's
        own slope, i.e. a click -- and made a release jump straight back to
        full speed with no spin-up.
        """
        on = bool(on)
        if on == self._scratch:
            return
        self._scratch = on
        # Engage the flywheel and seed it with the speed the record is
        # ACTUALLY turning at, so braking starts from the current pitch
        # instead of from a standstill.
        if not self._jog_engaged:
            # _drive(), not _rate: _rate is an unsigned MAGNITUDE.  Seeding the
            # flywheel with it on a deck that is playing BACKWARDS flips the
            # sign, so touching the platter whipped a reversed record FORWARD
            # and then braked it the wrong way.  Only bites once the flywheel
            # has disengaged (~1.5s after a reverse settles), which is why a
            # quick test misses it -- but SYNC then a platter touch is an
            # ordinary gesture on both Mixtrack decks.
            self._jog_vel = self._drive()
        self._jog_engaged = True
        self._jog_impulse = 0.0
        self._jog_active = 0

    def set_reverse(self, on):
        """Run the record backwards (or forwards again).

        Rides the platter model rather than negating the read rate outright:
        the flywheel is engaged from the speed the record is CURRENTLY turning
        at and the motor's rest target flips sign, so the deck decelerates,
        passes through a stop -- silent for that instant, because a groove
        that isn't moving makes no sound -- and comes back up to speed the
        other way.  That is what spinning a record the other direction sounds
        like, and it costs nothing extra: the same brake/spin-up path that
        handles the platter does it.
        """
        on = bool(on)
        if on == self._reverse:
            return
        self._reverse = on
        if self._playing():
            # A turning record has momentum: ride the platter model so it
            # decelerates, passes through a stop and comes back up the other
            # way.  Seed from the ACTUAL current read rate, not from _rate, so
            # a flip mid-scratch or mid-bend starts where the record really is.
            if not self._jog_engaged:
                self._jog_vel = self._jog_step_prev
            self._jog_engaged = True
            # Arm the brake-hold-flip.  Not while a hand is on the platter --
            # then the finger, not the motor, owns the turnaround.
            self._turn_dwell_left = 0
            self._turn_pending    = not self._scratch
        else:
            # A STOPPED record has no momentum to reverse, so starting it must
            # begin in the requested direction immediately.  Engaging the
            # flywheel here would seed it with the direction the deck was
            # last turning (stop() leaves _jog_step_prev at the old sign), and
            # the next PLAY would audibly run FORWARD for most of a second
            # before slewing into reverse.
            self._jog_vel = 0.0
            self._jog_engaged = False
            self._jog_step_prev = self._drive()
            self._turn_dwell_left = 0
            self._turn_pending    = False

    def toggle_reverse(self):
        self.set_reverse(not self._reverse)

    def loaded(self):
        return len(self._data) > 1

    def status(self):
        return {
            "name": self.name,
            "loaded": self.loaded(),
            "length_s": round(len(self._data) / self.sr, 2) if self.loaded() else 0.0,
            "playing": self._playing(),
            "speed": round(self._rate, 3),
            "reverse": self._reverse,
            "scratch": self._scratch,
            "gain": round(self._gain, 3),
        }

    # ---- realtime render ----------------------------------------------------

    def render(self, n_frames):
        """Return an (n_frames,) float32 block, or None if silent."""
        # A stopped deck keeps rendering while a release fade-out is in
        # flight, so stopping sounds like a deck stopping rather than the
        # signal being cut mid-waveform.
        # ONE read of _playing(), used for both decisions.  Asking twice
        # let a stop() landing between the two calls set _release_left AFTER
        # `releasing` had already latched False -- so the second test returned
        # None from full level.  Measured: 0.79 straight to digital zero, then
        # a whole 21ms block of silence, then back up to -0.79 for the fade.
        # A 34.5x step off the CUE and LOOP buttons.
        playing = self._playing()
        releasing = (not playing) and self._release_left > 0
        if not playing and not releasing:
            if self._last_out != 0.0:
                self._last_out = 0.0
            return None

        # Claim the declick token NOW, before _data and _pos are read.  Control calls land on the MIDI thread and can arrive
        # part-way through this function: a jump() that sets _dc_pending after
        # the read below belongs to the NEXT block (the first one that actually
        # contains the new position), so leaving the flag set for that call is
        # correct.  Consuming it here-and-now would declick a transition that
        # has not happened yet AND spend the one-shot flag, leaving the real
        # discontinuity completely unsmoothed -- a click, from the very
        # mechanism built to prevent clicks.
        # Read the buffer and the position ADJACENTLY, then claim the
        # declick token IMMEDIATELY after.  The ordering is the whole point:
        #
        #   * a control call landing BEFORE these reads is fully represented --
        #     this block gets its new position AND its token, so the
        #     discontinuity it created is smoothed here;
        #   * one landing AFTER leaves the token set for the NEXT block, which
        #     is the first block that can contain its new position.
        #
        # Claiming the token far from the _pos read is what made this subtle:
        # the claim used to sit ~85 lines above it, and a jump() arriving in
        # that gap got the WORST of both -- the block DID render from the new
        # position, but the token had already been consumed as False, so the
        # real discontinuity was never smoothed.  Measured at a step of 0.78
        # against the signal's own 0.023 max slope: a full-scale click,
        # produced by the mechanism built to prevent clicks.  The touchstrip
        # calls jump() on EVERY CC, so one needle-drop swipe rolls that die
        # dozens of times.
        data = self._data
        n = len(data)
        pos0 = self._pos          # the position THIS block is built from
        dc_now = self._dc_pending
        if dc_now:
            self._dc_pending = False
        if n < 2:
            # Nothing was rendered, so hand the token back rather than
            # swallowing it -- a claimed-then-dropped token means the next
            # real discontinuity goes unsmoothed.
            if dc_now:
                self._dc_pending = True
            if self._last_out != 0.0:
                self._last_out = 0.0
            return None

        amp = 1.0
        # ── Flywheel jog velocity (shared by touch + no-touch) ───────────────
        # Turn this block's accumulated jog impulses into an instantaneous
        # platter speed, then smooth toward it. While the wheel is being spun
        # (recent tick within the active window) the platter follows your hand
        # -- fast spin = fast, slow spin = slow, backward = reverse. When you
        # stop, it eases toward a REST target: normal playback rate (the motor)
        # with no finger down, or a full stop (silence) with a finger on the
        # capacitive top (braking a record).
        imp = self._jog_impulse
        self._jog_impulse = 0.0
        if self._jog_active > 0:
            self._jog_active = max(0, self._jog_active - n_frames)
        # glide the transport rate toward set_speed()'s target (this is the
        # no-touch rest speed, so a bent-pitch deck returns to the bent speed)
        if self._rate != self._target_rate:
            g = min(1.0, n_frames / self._SPEED_GLIDE_FRAMES)
            self._rate += (self._target_rate - self._rate) * g
            if abs(self._rate - self._target_rate) < 1e-4:
                self._rate = self._target_rate

        # The flywheel only runs once you've actually touched the wheel; an
        # untouched, normally-playing deck skips it entirely (step = rate), so
        # PLAY never "spins up".
        if imp != 0.0:
            self._jog_engaged = True
        # Where the platter is HEADING: a hand on it is pulling it to a stop,
        # nothing on it means the motor is pulling it to playback speed -- in
        # whichever direction the motor is currently set to turn.
        rest = 0.0 if self._scratch else self._drive()
        # A hand on the platter cancels a pending motor turnaround -- the finger
        # owns it from here (braking to silence via the _scratch path instead).
        if self._scratch:
            self._turn_pending = False
            self._turn_dwell_left = 0
        if self._turn_dwell_left > 0:
            # Mid-hold: the platter is stopped at the flip.  Pin it at zero
            # (silent via the motor gate below) and count the hold down; the
            # normal friction update is skipped so nothing pulls it off zero.
            self._jog_vel = 0.0
            step = 0.0
            self._turn_dwell_left = max(0, self._turn_dwell_left - n_frames)
        elif self._jog_engaged:
            # accelerate on this block's jog movement, then approach rest with
            # the appropriate time constant -- a hand grips it to a halt fast,
            # a direct-drive motor takes a beat to bring it back up to speed
            # (and you HEAR that as the pitch rising, which is the point).
            accel = self._JOG_ACCEL_SCRATCH if self._scratch else self._JOG_ACCEL_FREE
            self._jog_vel += imp * accel
            tau = self._BRAKE_TAU_S if self._scratch else self._MOTOR_TAU_S
            fric = float(np.exp(-n_frames / (self.sr * tau)))
            self._jog_vel = rest + (self._jog_vel - rest) * fric
            self._jog_vel = float(np.clip(self._jog_vel,
                                          -self._JOG_MAX_VEL if (self._scratch or rest < 0.0) else 0.0,
                                          self._JOG_MAX_VEL))
            step = self._jog_vel
            # Reverse turnaround: the platter is braking toward the flipped rest
            # and has just reached (or crossed) zero -- snap it to a dead stop
            # and start the silent hold.  Detected by the velocity landing on
            # rest's sign, which with the tight tau happens the first block it
            # gets past zero.
            if (self._turn_pending
                    and (self._jog_vel == 0.0 or (self._jog_vel < 0.0) == (rest < 0.0))):
                self._turn_pending = False
                self._turn_dwell_left = self._TURN_DWELL_FRAMES
                self._jog_vel = 0.0
                step = 0.0
            # once it has settled at rest AND the hand is off, disengage so
            # playback is perfectly clean again (no lingering flywheel math)
            elif (not self._scratch and self._jog_active <= 0
                    and abs(self._jog_vel - rest) < 5e-3):
                self._jog_engaged = False
                self._jog_vel = rest
                step = rest
        else:
            step = rest
            self._jog_vel = rest

        # A groove makes no sound only when it is not MOVING -- and that is
        # true whether a hand stopped it or the motor has not spun it up yet.
        # Gating on `_scratch` instead meant a released platter came back at
        # full level while still crawling, i.e. a near-DC thump on the way up.
        # At normal speed this is min(1.0, 1/0.02) = 1.0, so it costs nothing
        # anywhere except the last couple of percent of speed.
        # A hand on the platter (_scratch) brakes to a stop and should keep
        # sounding as it decays -- narrow gate.  A motor-driven turnaround /
        # spin-up (no hand) whips through zero too fast for the narrow gate to
        # catch, so it gets the wider one and actually goes quiet at the cross.
        if self._scratch:
            amp = min(1.0, abs(step) / self._SILENT_SPEED)
        elif self._jog_engaged:
            amp = min(1.0, abs(step) / self._SILENT_SPEED_MOTOR)

        # Build the per-sample read positions. When the platter speed ('step')
        # is unchanged from last block — normal steady playback — the read head
        # advances at a fixed rate, a simple ramp (identical to before, no cost).
        # But when the speed CHANGED (scratching, spinning up/down), holding one
        # speed for the whole ~21ms block makes the pitch jump in 21ms stairs,
        # heard as a grainy/gurgly scrub. So when the speed is changing we
        # interpolate the read rate linearly from last block's ending speed to
        # this block's, so the pitch slides continuously instead of stepping.
        # Output-rate conversion: the buffer is at self.sr (48k), but this block
        # must produce n_frames OUTPUT frames at the JACK rate. The read head
        # therefore advances (step * sr/out_rate) buffer-frames per output frame,
        # so a 48k grab plays at correct pitch and the loop lasts the right time.
        # rr is 1.0 when out_rate == sr => the two lines below reduce EXACTLY to
        # the single-rate originals (byte-identical). The musical `step` (amp gate,
        # _jog_step_prev compare) stays in 1.0=normal units; only traversal scales.
        rr = self._rate_ratio
        if step == self._jog_step_prev:
            positions = pos0 + np.arange(n_frames, dtype=np.float64) * (step * rr)
            travel = n_frames * step * rr
        else:
            rates = np.linspace(self._jog_step_prev, step, n_frames,
                                dtype=np.float64) * rr   # per-sample rate, ramped
            cs = np.cumsum(rates)
            positions = pos0 + (cs - rates)         # exclusive prefix sum: 1st read = _pos
            travel = float(cs[-1])                  # total record-frames moved this block
        self._jog_step_prev = step

        # Snapshot the wrap decision ONCE and use it for both the read below
        # and the advance at the tail, so a _oneshot flip on the MIDI thread
        # cannot make the two disagree within one block (build looped, advance
        # non-looped or vice versa).  _loop is always True; the one-shot
        # audition is the only thing that turns wrapping off (see audition()).
        loop_now = self._loop and not self._oneshot
        # Play/loop REGION [si, ei), snapshotted ONCE (like loop_now) so a
        # set_bounds on the MIDI thread can't make the read below and the
        # advance at the tail disagree within a block.  Defaults span the whole
        # buffer (si=0, ei=n), so both branches reduce EXACTLY to the old
        # full-buffer wrap/clamp.
        si, ei = self._region_frames(n)
        Li = ei - si
        if loop_now:
            # Wrap within the region, interpolating across the seam by wrapping
            # the partner index back to the region START -- the same technique
            # as the old full-buffer mod-n wrap (which dropped the final sample
            # and jumped data[n-2]->data[0]), generalised to [si, ei).
            rel = np.mod(positions - si, Li)
            positions = si + rel
            i0 = si + rel.astype(np.int64) % Li
            i1 = si + (i0 - si + 1) % Li
        else:
            i0 = np.clip(positions.astype(np.int64), si, ei - 2)
            i1 = i0 + 1
        fr = (positions - np.floor(positions)).astype(np.float32)
        out = (data[i0] * (1.0 - fr) + data[i1] * fr).astype(np.float32)

        # 3-band EQ + sweep filter -- glide toward the knob targets, then run
        # the block through whichever bands are actually doing anything
        # (each filter is its own bypass at 0/flat, so idle bands are free).
        out = self._apply_tone(out, n_frames)

        # advance / stop  (travel = frames moved this block, from the ramp above)
        #
        # A control call on the MIDI thread can land part-way through this
        # function.  If it did, IT owns the position and this advance must not
        # run -- otherwise the deck lands `travel` frames past wherever it was
        # asked to go.  Two distinct ways that happens, and both must be
        # checked: `_data is not data` catches load_buffer/swap_buffer (which
        # also invalidate `n`), while `_pos != pos0` catches the calls that
        # move ONLY the position -- jump() (hotcue, needle-drop seek) and the
        # rewind inside _cancel_release().  Checking the buffer alone left the
        # commonest gesture on the controller unprotected.
        if self._data is not data or self._pos != pos0:
            # A control call landed mid-render.  It owns the position, so skip
            # the advance -- and HAND THE DECLICK TOKEN BACK, because this
            # block was built from the old state and the discontinuity the
            # call created is still ahead of us.  Spending the token here is
            # the same failure round 6 fixed at a larger scale: the block that
            # actually contains the new content gets no declick.  Measured up
            # to 32x on a load landing between the data and pos reads.
            if dc_now:
                self._dc_pending = True
        elif loop_now:
            self._pos = float(si + (pos0 + travel - si) % Li)
        else:
            self._pos = pos0 + travel
            if self._pos >= ei - 1 or self._pos < si:
                if self._oneshot:
                    # One-shot audition reached the end of the clip.  Stop
                    # UNCONDITIONALLY -- do NOT consult the jog_driving guard
                    # below: ordinary forward playback parks _jog_vel at the
                    # play rate every block (see the step/rest logic above), so
                    # that guard is True during a normal audition and would
                    # clamp the deck at the end forever instead of stopping it.
                    # Hand off to a release fade + rewind so the tail ramps down
                    # (not a hard cut) and the deck is cued at the top for the
                    # next PLAY.  This block still renders in full -- `releasing`
                    # was latched from the pre-stop play state at the top of
                    # render().
                    #
                    # _oneshot stays ARMED through the fade and is cleared only
                    # when the rewind completes (below).  Clearing it here would
                    # flip loop_now back to True for the fade block, whose _pos
                    # sits at the buffer END -- so the wrap would jump across the
                    # loop seam and click (measured 6.8x).  Keeping it armed
                    # keeps the fade on the non-loop path, reading the clamped
                    # tail steadily.
                    self._held = False
                    self._latched = False
                    self._release_left = self._fade_frames
                    self._rewind_after_release = True
                else:
                    jog_driving = (self._scratch or self._jog_active > 0
                                   or abs(self._jog_vel) > 1e-3)
                    if jog_driving:
                        # scrubbing off either end of a non-looping clip just
                        # clamps; the wheel is still driving it, don't stop the
                        # deck (this also stops a reverse scrub from tripping the
                        # play-off-the-end path and silencing the deck).
                        self._pos = min(max(self._pos, float(si)), float(ei - 1))
                    else:
                        self._held = False
                        self._latched = False

        # Groove-stopped gate, RAMPED across the block.  Applying it flat was
        # a gain step at every block boundary the platter speed changed --
        # measured at up to 3.0x between adjacent blocks while braking, which
        # is the grit you hear through a scratch.  Same idea as the read-rate
        # ramp above: interpolate from where the gate was at the end of the
        # last block to where it is now.
        if amp != 1.0 or self._amp_prev != 1.0:
            out *= np.linspace(self._amp_prev, amp, n_frames,
                               dtype=np.float32)
        self._amp_prev = amp

        out = out * self._gain

        # Release fade-out: the deck was stopped, so bring the audio down
        # instead of cutting it, then rewind if stop() asked for it.
        # `releasing` was latched at the top of render(); re-read the counter
        # here because a control call on the MIDI thread can cancel the release
        # in between.  When it does, k is 0 -- and falling through would run
        # `out[0:] = 0.0` and hard-zero the very block playback was just
        # re-requested for.
        k = min(self._release_left, n_frames) if releasing else 0
        if k > 0:
            t0 = self._release_left / self._fade_frames
            t1 = (self._release_left - k) / self._fade_frames
            out[:k] *= np.linspace(t0, t1, k, endpoint=False,
                                   dtype=np.float32)
            out[k:] = 0.0
            self._release_left -= k
            if self._release_left <= 0 and self._rewind_after_release:
                self._pos = self._rewind_pos()
                self._rewind_after_release = False
                self._oneshot = False    # one-shot fully done: cued at the region top

        out = self._declick(out, n_frames, dc_now)
        self._last_out = float(out[-1]) if n_frames else self._last_out
        return out

    def _declick(self, out, n_frames, dc_now=None):
        """Remove a step discontinuity by OFFSET DECAY.

        On an in-place jump (hotcue, needle-drop seek, buffer swap, a fresh
        capture) the first new sample has no relationship to the last one
        emitted.  Measure that difference once, add it to the new content, and
        decay it to zero over ~1.5ms: the output starts exactly where the
        previous block ended and slides onto the new waveform, with no gap and
        no lookahead.

        This replaced a ramp that faded the new block IN FROM ZERO, which only
        declicks when the previous output was already silence -- otherwise it
        manufactures a full-scale step down to 0 and then ramps back up.  That
        is what made every hotcue and every platter touch click.  Note the
        starting-from-silence case still works here and needs no special
        handling: _last_out is 0, so the offset is -out[0] and the decay IS a
        fade-in.
        """
        if dc_now is None:                 # direct callers keep the old flow
            dc_now = self._dc_pending
            self._dc_pending = False
        if dc_now:
            self._dc_off = self._last_out - (float(out[0]) if n_frames else 0.0)
            self._dc_left = self._DC_FRAMES
        if self._dc_left > 0 and n_frames:
            k = min(self._dc_left, n_frames)
            t0 = self._dc_left / self._DC_FRAMES
            t1 = (self._dc_left - k) / self._DC_FRAMES
            t = np.linspace(t0, t1, k, endpoint=False, dtype=np.float32)
            # raised cosine: reaches zero with zero slope, so the end of the
            # decay is as smooth as the start
            out[:k] += self._dc_off * 0.5 * (1.0 - np.cos(np.pi * t))
            self._dc_left -= k
        return out

    def _apply_tone(self, out, n_frames):
        """Glide the EQ gains / filter position toward their knob targets,
        then run the block through each band. Cheap to call every block --
        the biquads themselves are lfilter (C), and each one is a no-op
        bypass while its target is flat/center (see _ShelfPeakFilter /
        _SweepFilter's own _bypass check)."""
        glide = min(1.0, n_frames / self._TONE_GLIDE_FRAMES)
        # Sub-block only while something is actually moving; steady tone = one
        # pass (no extra cost). Threshold matches each band's redesign skip.
        moving = (abs(self._eq_lo_target  - self._eq_lo_db)  > 0.05 or
                  abs(self._eq_mid_target - self._eq_mid_db) > 0.05 or
                  abs(self._eq_hi_target  - self._eq_hi_db)  > 0.05 or
                  abs(self._filt_target   - self._filt_pos)  > 0.002)
        k = self._TONE_SUBBLOCKS if (moving and n_frames >= self._TONE_SUBBLOCKS) else 1
        g = glide if k == 1 else 1.0 - (1.0 - glide) ** (1.0 / k)
        sub = n_frames // k
        for s in range(k):
            lo = s * sub
            hi = n_frames if s == k - 1 else (s + 1) * sub
            self._eq_lo_db  += (self._eq_lo_target  - self._eq_lo_db)  * g
            self._eq_mid_db += (self._eq_mid_target - self._eq_mid_db) * g
            self._eq_hi_db  += (self._eq_hi_target  - self._eq_hi_db)  * g
            self._filt_pos  += (self._filt_target   - self._filt_pos)  * g
            seg = out[lo:hi]
            self._eq_lo.set_gain(self._eq_lo_db);   seg = self._eq_lo.process(seg)
            self._eq_mid.set_gain(self._eq_mid_db); seg = self._eq_mid.process(seg)
            self._eq_hi.set_gain(self._eq_hi_db);   seg = self._eq_hi.process(seg)
            self._filt.set_position(self._filt_pos); seg = self._filt.process(seg)
            out[lo:hi] = seg
        return out


# ==============================================================================
# SAMPLER MANAGER  —  owns the decks, capture, and crossfade
# ==============================================================================

class SamplerManager:
    """
    Owns N decks + the RingBufferReader + the crossfade. The synth calls
    render() from the JACK callback and mixes the result into a 'sampler' bus.

    Capture happens in the control thread: read the ring snapshot, then hand the
    buffer to the deck (attribute swap). No locks touch the render path.
    """

    def __init__(self, ring, sample_rate=48000, cfg=None, out_rate=None):
        cfg = cfg or {}
        self.ring = ring
        self.sr   = int(sample_rate)
        # Internal/buffer math (capture counts, seam, phase seed, fades) all live
        # at self.sr = the RING rate (48k). out_rate is the JACK server rate, passed
        # to each deck so its render read-step converts the 48k buffer to the output
        # rate (a per-card jackd at 44100). Equal => no-op; see SampleDeck._out_rate.
        self.out_rate = int(out_rate) if out_rate else int(sample_rate)
        self.default_seconds = int(cfg.get("DEFAULT_CAPTURE_SECONDS", 8))
        self.max_seconds     = int(cfg.get("MAX_CAPTURE_SECONDS", 25))
        # Capture normalization: the skipback ring records the mixer's USB
        # master return, whose level depends entirely on the mixer's gain
        # staging (measured ~-35 dB on the L12 with normal fader positions —
        # captures play back as whispers without makeup gain). Normalizing
        # each capture to a target peak makes the sampler agnostic to the
        # audio interface's return level. Near-silent grabs are left alone
        # so we never blow the noise floor up to full scale.
        self.normalize    = bool(cfg.get("CAPTURE_NORMALIZE", True))
        self.target_peak  = float(cfg.get("CAPTURE_TARGET_PEAK", 0.9))
        self.max_gain     = float(cfg.get("CAPTURE_MAX_GAIN", 100.0))
        self.silence_peak = float(cfg.get("CAPTURE_SILENCE_PEAK", 1e-4))
        # Capture timing (2026-07-07): constant heard->ring delay compensated
        # on every grab, plus the loop-seam crossfade length (see _load_slice).
        # Fallback matches the config default (-99); see the note in orc1.py.
        # This said 85 while orc1.py said 42 and config said -99.
        self.latency_s    = float(cfg.get("CAPTURE_LATENCY_MS", -99)) / 1000.0
        # PRESS-anchored capture (2026-08-18): the bar-grab now ends at the
        # PLAYER'S PRESS (offset_ms == 0 on the frame path), NOT the ORC's bar
        # grid — the grid's phase has no relation to the music's downbeat, which
        # was the "don't know what segment it grabs" complaint. That end-now
        # path takes its OWN latency (press_latency_s), separate from the grid
        # path's latency_s, because it is source-dependent (SYNTH DIRECT vs an
        # L12 round-trip differ) and tuned live by ear. 0 = end on the newest
        # recorded sample; more NEGATIVE reaches further back. Persisted so a
        # by-ear trim survives a synth restart (overrides the config default).
        self._press_trim_path = os.path.expanduser(
            cfg.get("PRESS_TRIM_PATH", "~/.orc1_sampler_press_trim.json"))
        # PER-SOURCE press trim (2026-09-15): one shared trim can't serve two
        # physical capture paths at once -- SYNTH DIRECT and an L12 return have
        # DIFFERENT round-trip latencies, which is exactly why a single by-ear
        # value only ever got the loop "very close". So the trim is now kept
        # PER SOURCE, keyed off track 0's live capture ports. The skipback
        # status file names those ports (`resolved`); it is read-only here (the
        # skipback system owns it) and a missing/garbled file -> "__default__".
        self._skipback_status_path = os.path.expanduser(
            cfg.get("SKIPBACK_STATUS_PATH", "~/.orc1_skipback_status.json"))
        # Fallback press latency for a source with no stored trim yet.
        self._press_default_s = float(cfg.get("PRESS_CAPTURE_LATENCY_MS", 0)) / 1000.0
        self._press_by_source = {}          # source-key -> latency ms (loaded below)
        self.press_latency_s  = self._press_default_s
        # ONSET-SNAP (2026-09-15): after a PRESS grab, refine the loop START to
        # the nearest strong transient within +/- onset_snap_ms. This computes
        # the downbeat from the AUDIO instead of the finger, killing the
        # residual human press jitter the single trim never could. The start is
        # a full bar behind the window end, so the search reads only audio
        # already written to the ring. Conservative: no clear transient near
        # the start -> no shift, so it can never wreck a pad/sustained loop.
        self.onset_snap        = bool(cfg.get("ONSET_SNAP", True))
        self.onset_snap_ms     = float(cfg.get("ONSET_SNAP_MS", 120.0))
        self.onset_snap_sens   = float(cfg.get("ONSET_SNAP_SENSITIVITY", 0.35))
        self._restore_press_trim()
        self._seam_frames = max(0, int(self.sr *
                                float(cfg.get("SEAM_FADE_MS", 15)) / 1000.0))
        # False = a freshly grabbed loop loads CUED (stopped at start, armed
        # but silent) instead of auto-arming into instant looped playback.
        # See CAPTURE_AUTO_ARM in orc1_config.py.
        self.auto_arm     = bool(cfg.get("CAPTURE_AUTO_ARM", False))
        # Per-deck capture-window metadata (ring end byte / length / gain) so
        # nudge() can re-slice the SAME grab shifted in time. The ring holds
        # 31 minutes — the audio to fix a slightly-off grab is still in it.
        self._cap_meta    = {}
        # File-deck load cap (decoded mono float32; 600s ≈ 115MB).
        self._load_max_s  = float(cfg.get("LOAD_MAX_SECONDS", 600))
        fade = float(cfg.get("CLICK_FADE_MS", 4.0))
        jog_sens = float(cfg.get("JOG_SENSITIVITY", 1.0))   # free-spin (no-touch) bend scale
        # scratch (finger-down) scale — defaults to the free-spin value if unset
        jog_scr  = float(cfg.get("JOG_SCRATCH_SENSITIVITY", jog_sens))
        self.decks = {
            "A": SampleDeck("deck_a", sample_rate, fade, jog_sens, jog_scr,
                            out_rate=self.out_rate),
            "B": SampleDeck("deck_b", sample_rate, fade, jog_sens, jog_scr,
                            out_rate=self.out_rate),
        }
        self._xf = 0.5                                  # 0=all A, 1=all B
        # Previous block's equal-power gains, so render() can RAMP the gain
        # across the block instead of stepping it. An unsmoothed scalar gain
        # per block pops on a fast crossfader move (measured ~2.2x the signal's
        # own step at the block seam, 2026-08-12).
        self._ga_prev = float(np.cos(self._xf * np.pi / 2.0))
        self._gb_prev = float(np.sin(self._xf * np.pi / 2.0))
        self._master_gain = 1.0                         # master output level (0..2)
        # Deck persistence: keep whatever's loaded in each deck across a reboot
        # (orc1-synth restart rebuilds this manager with EMPTY decks otherwise).
        # On every grab/load/nudge the deck body is written to a fixed mono
        # 16-bit WAV in $HOME (runtime/user state, not repo source -- same place
        # as ~/.orc1_sampler_view_*.json); on init we reload each one CUED
        # (stopped at 0, loop-armed, silent), exactly the state load_file leaves
        # a freshly-loaded file in. The persisted body is ALREADY normalized, so
        # restore never re-applies gain.
        self.persist_decks = bool(cfg.get("PERSIST_DECKS", True))
        self._persist_dir  = os.path.expanduser(cfg.get("DECK_PERSIST_DIR", "~"))
        if self.persist_decks:
            self._restore_decks()
        # ── PER-DECK GRAB SOURCE (2026-09-15) ─────────────────────────────────
        # Each deck grabs from its OWN skipback ring, so deck A can sample the
        # MAIN master (track 0) while deck B samples a different stream (track 1
        # = a mono aux). Track 0 is the primary reader built above (the legacy
        # ~/.skipbuf/ring.raw symlink); other tracks resolve
        # ~/.skipbuf/t<id>/ring.{raw,pos} LAZILY on first use. RingBufferReader
        # self-resolves size + channels from the tracks JSON, so a mono ring
        # reads as mono with no extra config. Persisted so a deck keeps its
        # stream across a restart. All of this is control-thread only (capture /
        # nudge); render() never touches ring geometry, so no lock is needed.
        self._skipbuf_root  = os.path.expanduser(cfg.get("SKIPBUF_ROOT", "~/.skipbuf"))
        self._deck_src_path = os.path.expanduser(
            cfg.get("DECK_SRC_PATH", "~/.orc1_sampler_deck_src.json"))
        self._rings = {0: self.ring}                 # track_id -> RingBufferReader
        _dsd = cfg.get("DECK_SRC_DEFAULT", {"A": 0, "B": 1})
        self._deck_src = {"A": int(_dsd.get("A", 0)), "B": int(_dsd.get("B", 1))}
        self._restore_deck_src()

    # ---- control thread -----------------------------------------------------

    def capture(self, deck, seconds=None):
        """Grab the last `seconds` from the ring into `deck`'s buffer
        (immediate, unquantized — the plain GRAB button)."""
        secs = self.default_seconds if seconds is None else seconds
        secs = max(0.1, min(float(secs), self.max_seconds))
        self._do_capture(deck, int(round(secs * self.sr)))

    def capture_frames(self, deck, frames, offset_ms=0.0):
        """Frame-exact capture. offset_ms == 0 => PRESS-anchored (the window
        ends at NOW, the player's press) — uses press_latency_s. offset_ms > 0
        => GRID-anchored: the engine placed the end on a barline `offset_ms`
        before now, and playback starts PHASE-ALIGNED (the loop drops in locked
        to the live groove) — uses latency_s. The two paths take DIFFERENT
        latency constants (see __init__)."""
        off = float(offset_ms)
        self._do_capture(deck, int(frames), offset_ms=off,
                         phase_align=(off > 0), press=(off == 0))

    def _do_capture(self, deck, frames, offset_ms=0.0, phase_align=False,
                    press=False):
        d = self.decks.get(deck)
        if d is None:
            return
        frames = int(max(1, min(frames, int(self.max_seconds * self.sr))))
        ring = self._ring_for(deck)             # the skipback ring THIS deck grabs
        # Re-resolve ring geometry BEFORE anything derives from it. Placement
        # is load-bearing: `fb` on the next line is the first read of
        # ring.channels, so refreshing any later would mix a new size with an
        # old frame width for the rest of this call. If the ring changed shape
        # (resized / disarmed+rearmed / stereo flipped), every remembered ring
        # offset is now meaningless -- drop them so nudge can't re-slice
        # against a stale modulus.
        try:
            if ring.refresh_geometry():
                self._cap_meta.clear()
                print(f"[sampler] ring geometry changed -> "
                      f"{ring.buffer_bytes}B "
                      f"{ring.channels}ch "
                      f"({ring.buffer_bytes / max(1, ring.bytes_per_s):.1f}s)"
                      f"; capture windows dropped", flush=True)
        except Exception as e:
            print(f"[sampler] geometry refresh failed: {e}", flush=True)
        if ring.buffer_bytes <= 0:
            # Belt-and-braces: every `% buffer_bytes` below would raise. Not
            # reachable with today's config (the constructor fallback is
            # nonzero and refresh_geometry skips st_size <= 0), but geometry
            # is now filesystem-derived, so refuse rather than trust it.
            print("[sampler] capture skipped: ring geometry unresolved",
                  flush=True)
            return
        fb = ring.channels * 2                         # bytes per ring frame
        pos, extra = ring.head_info()
        # Window end, measured in bytes BEHIND the flushed head `pos`.
        # The ring's copy of the audio arrives LATE by the pipeline latency
        # (audio heard at wall time T is written at T+L), so to end the
        # window on audio heard at (now - offset) we count back offset MINUS
        # latency from the true head (pos + extra). Sign verified 2026-07-07
        # with the kick-alignment test: adding L put the barline kick ~2L
        # late into the loop; subtracting centers it on the grid.
        # ring.rate, not self.sr: this converts ms -> RING bytes. Identical
        # today (both pinned 48000) but naming the right clock keeps the two
        # domains from silently fusing.
        # Press/end-now path (offset 0) uses press_latency_s; the grid path
        # uses latency_s. Same formula, path-specific constant.
        if press:
            # Per-source press trim: each physical capture path (SYNTH DIRECT
            # vs an L12 return) keeps its own by-ear latency, resolved from the
            # live source at grab time so switching sources needs no re-tune.
            self.press_latency_s = self._trim_for(self._current_source_key(deck))
        lat_s  = self.press_latency_s if press else self.latency_s
        behind = (int((offset_ms / 1000.0 - lat_s) * ring.rate) * fb
                  - extra)
        # The seam blend reads _seam_frames PAST the window end, and only
        # bytes behind `pos` are guaranteed written — clamp accordingly.
        behind = max(behind, self._seam_frames * fb)
        behind -= behind % fb
        end = (pos - behind) % ring.buffer_bytes
        if press and self.onset_snap and frames > 0:
            # Onset-snap: shift the whole window so the loop START lands on the
            # nearest strong transient (the real downbeat). The start is a full
            # bar behind `end`, so the +/- search reads only ring audio that is
            # already written. The shift moves the END by the same amount. A
            # LATER shift (press a hair before the downbeat -- the common case)
            # can be up to onset_snap_ms, but `end` sits only the seam/press-trim
            # (~15-29 ms) behind the flushed head -- so the loop's tail + seam
            # read would land in bytes not yet written this lap (audio from a
            # ring-length ago). _await_written waits for the recorder instead.
            # (Until 2026-09-23 this comment claimed the headroom covered it.)
            try:
                start_byte = (end - frames * fb) % ring.buffer_bytes
                shift = self._onset_shift_bytes(start_byte, fb, ring)
                if shift:
                    end = (end + shift) % ring.buffer_bytes
                    if shift > 0:
                        end = self._await_written(ring, end, fb)
            except Exception as e:
                print(f"[sampler] onset-snap skipped: {e}", flush=True)
        phase_ms = offset_ms if phase_align else None
        self._load_slice(deck, end, frames, gain=None, phase_ms=phase_ms)

    def _await_written(self, ring, end, fb):
        """Return a window end whose read (end + the seam pad) lies entirely at
        or behind the ring's flushed head `pos` -- i.e. only audio written THIS
        lap. Polls the head (the recorder flushes every ~43 ms) for up to the
        snap range + two flushes; a later snap usually needs a few tens of ms.
        If the head never gets there (recorder stalled), pull the end back to
        the last safe byte rather than read stale audio or block the MIDI
        thread. Runs on the capture (control) thread, never in render()."""
        B = ring.buffer_bytes
        pad = self._seam_frames * fb
        deadline = time.monotonic() + self.onset_snap_ms / 1000.0 + 0.1
        while True:
            pos, _extra = ring.head_info()
            ahead = (end + pad - pos) % B          # >0 and small => unwritten
            if ahead == 0 or ahead >= B // 2:
                return end
            if time.monotonic() >= deadline:
                safe = (pos - pad) % B
                safe -= safe % fb
                print(f"[sampler] onset-snap: recorder did not reach the snapped "
                      f"end; pulled back {ahead / max(1, ring.bytes_per_s) * 1000:.0f} ms "
                      f"to written audio", flush=True)
                return safe
            time.sleep(0.005)

    def _onset_shift_bytes(self, start_byte, fb, ring=None):
        """Read a short segment of the ring around the loop START and return the
        byte offset (multiple of `fb`, can be +/-) that moves the start onto the
        nearest strong transient, or 0 if onset-snap finds none. Pure ring I/O +
        the pure detector `_onset_index`; the START is ~a bar behind the head so
        both search directions read already-written audio. `ring` is the deck's
        grab ring (defaults to the primary reader for callers that don't pass one)."""
        ring = ring if ring is not None else self.ring
        R = int(round(self.onset_snap_ms / 1000.0 * ring.rate))   # frames
        if R < 8:
            return 0
        # read [start-R, start+R) : end_byte = start + R
        read_end = (int(start_byte) + R * fb) % ring.buffer_bytes
        seg = ring.snapshot_frames(2 * R, mono=True, end_byte=read_end)
        if seg is None or len(seg) < 2 * R:
            return 0
        idx = _onset_index(seg, R, ring.rate, self.onset_snap_sens)
        if idx is None:
            return 0
        delta = int(idx) - R                         # frames, +/-, |delta| < R
        if delta == 0:
            return 0
        print(f"[sampler] onset-snap: start shifted {delta / ring.rate * 1000.0:+.0f} ms",
              flush=True)
        return delta * fb

    # ---- per-deck grab source -----------------------------------------------

    def _make_ring(self, tid):
        """Return the RingBufferReader for skipback track `tid`, building it on
        first use. Track 0 is the primary reader (built in __init__ from the
        legacy ~/.skipbuf/ring.raw symlink); others resolve
        ~/.skipbuf/t<tid>/ring.{raw,pos}. buffer_secs/channels are only the
        pre-resolve fallback -- RingBufferReader.refresh_geometry() reads the
        real size + channel count from the tracks JSON, so a mono ring self-
        corrects to mono on construction (or on the first grab if the ring file
        does not exist yet)."""
        tid = int(tid)
        r = self._rings.get(tid)
        if r is not None:
            return r
        base = os.path.join(self._skipbuf_root, "t%d" % tid)
        r = RingBufferReader(
            os.path.join(base, "ring.raw"),
            os.path.join(base, "ring.pos"),
            buffer_secs=max(1, int(self.ring.buffer_bytes /
                                   max(1, self.ring.bytes_per_s))),
            rate=self.ring.rate,
            channels=self.ring.channels,
            tracks_path=self.ring.tracks_path)
        self._rings[tid] = r
        return r

    def _ring_for(self, deck):
        """The RingBufferReader a deck currently GRABS from. Falls back to the
        primary reader if a bad track id ever slips in, so capture can never be
        broken by the source selection."""
        try:
            return self._make_ring(self._deck_src.get(deck, 0))
        except Exception as e:
            print(f"[sampler] deck {deck} ring build failed ({e}); "
                  f"using primary", flush=True)
            return self.ring

    def set_deck_source(self, deck, track_id):
        """Point a deck's GRAB at skipback track `track_id` (0-63). Persisted so
        a deck keeps its stream across a restart. Lazy + cheap: building the
        reader opens no file until the first grab, and geometry self-resolves."""
        if deck not in self.decks:
            return
        try:
            tid = int(track_id)
        except (TypeError, ValueError):
            return
        if not 0 <= tid <= 63:
            return
        meta = self._cap_meta.get(deck)
        if meta is not None and "tid" not in meta:
            # The loaded loop's window is a byte offset into the ring it was
            # grabbed from. Pin that ring on it BEFORE the source changes, so a
            # later nudge keeps editing THIS loop in ITS ring (never slices the
            # new stream at a meaningless offset). _load_slice stamps "tid" on
            # every grab; this covers any window made before that.
            meta["tid"] = int(self._deck_src.get(deck, 0))
        self._deck_src[deck] = tid
        try:
            self._make_ring(tid)                 # warm so the first grab isn't cold
        except Exception:
            pass
        self._persist_deck_src()
        print(f"[sampler] deck {deck} grab source -> track {tid}", flush=True)

    def _persist_deck_src(self):
        """Atomically write the deck -> track-id map."""
        try:
            import json as _json, tempfile
            d = os.path.dirname(self._deck_src_path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                _json.dump({k: int(v) for k, v in self._deck_src.items()}, f)
            os.replace(tmp, self._deck_src_path)
        except Exception as e:
            print(f"[sampler] deck-src persist failed: {e}", flush=True)

    def _restore_deck_src(self):
        """Load the persisted deck -> track-id map, clamped to 0-63. A missing or
        garbled file leaves the boot defaults (A->0, B->1) untouched."""
        try:
            import json as _json
            with open(self._deck_src_path) as f:
                d = _json.load(f)
            for deck in ("A", "B"):
                if deck in d:
                    tid = int(d[deck])
                    if 0 <= tid <= 63:
                        self._deck_src[deck] = tid
        except Exception:
            pass

    def _current_source_key(self, deck=None):
        """Normalized key for a deck's live capture source (the ring that deck
        grabs from). Different physical paths -- SYNTH DIRECT vs an L12 return --
        have different round-trip latencies, so the press trim is kept per
        source. `deck` None => track 0 (the historical behaviour, used by the
        global CC84 trim + restore). Derived from the skipback status file's
        `resolved` ports; any read/parse problem falls back to '__default__' so
        a grab never fails on a missing file."""
        tid = int(self._deck_src.get(deck, 0)) if deck is not None else 0
        try:
            import json as _json
            with open(self._skipback_status_path) as f:
                st = _json.load(f)
            for t in st.get("tracks", []):
                if int(t.get("id", -1)) == tid:
                    res = [str(p) for p in (t.get("resolved") or []) if p]
                    if res:
                        return ",".join(sorted(res))
                    break
        except Exception:
            pass
        return "__default__"

    def _trim_for(self, key):
        """Press latency (seconds) for a source key: its own stored trim, else
        the '__default__' slot, else the config default."""
        by = self._press_by_source
        if key in by:
            return by[key] / 1000.0
        if "__default__" in by:
            return by["__default__"] / 1000.0
        return self._press_default_s

    def _persist_trim_file(self):
        """Atomically write the per-source trim dict + the onset-snap flag."""
        try:
            import json as _json, tempfile
            d = os.path.dirname(self._press_trim_path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                _json.dump({"by_source": self._press_by_source,
                            "onset_snap": bool(self.onset_snap)}, f)
            os.replace(tmp, self._press_trim_path)
        except Exception as e:
            print(f"[sampler] trim persist failed: {e}", flush=True)

    def set_press_latency(self, ms):
        """Live trim for the PRESS-anchored grab's end point, in ms (clamped
        <= 0; 0 = newest recorded sample, negative reaches further back).
        Stored against the CURRENT capture source so each physical path keeps
        its own by-ear value. Persisted atomically so it survives a restart."""
        try:
            ms = max(-300.0, min(0.0, float(ms)))
        except (TypeError, ValueError):
            return
        key = self._current_source_key()
        self._press_by_source[key] = ms
        self.press_latency_s = ms / 1000.0
        self._persist_trim_file()
        print(f"[sampler] press latency[{key}] -> {ms:.0f} ms", flush=True)

    def set_onset_snap(self, on):
        """Enable/disable onset-snap loop-start refinement (persisted)."""
        self.onset_snap = bool(on)
        self._persist_trim_file()
        print(f"[sampler] onset-snap -> {'on' if self.onset_snap else 'off'}",
              flush=True)

    def _restore_press_trim(self):
        """Load the per-source trim dict + onset flag. Migrates the legacy flat
        {"press_latency_ms": x} format into the '__default__' slot so an
        existing by-ear trim is preserved. Sets press_latency_s from the
        current source's trim."""
        try:
            import json as _json
            with open(self._press_trim_path) as f:
                data = _json.load(f)
            if isinstance(data.get("by_source"), dict):
                self._press_by_source = {
                    str(k): max(-300.0, min(0.0, float(v)))
                    for k, v in data["by_source"].items()}
                if "onset_snap" in data:
                    self.onset_snap = bool(data["onset_snap"])
            elif "press_latency_ms" in data:            # legacy flat -> default
                ms = max(-300.0, min(0.0, float(data["press_latency_ms"])))
                self._press_by_source = {"__default__": ms}
                self._persist_trim_file()               # upgrade the file in place
            self.press_latency_s = self._trim_for(self._current_source_key())
            print(f"[sampler] press trim restored: "
                  f"{len(self._press_by_source)} source(s), "
                  f"onset-snap {'on' if self.onset_snap else 'off'}", flush=True)
        except Exception:
            pass

    def _load_slice(self, deck, end_byte, frames, gain=None,
                    phase_ms=None, preserve=False, tid=None):
        """Read [end-frames, end) from the ring into a deck, with the
        loop-seam crossfade: the first _seam_frames of the loop start as the
        CONTINUATION of the window end and melt into the true start, so the
        wrap point is seamless instead of a click on every pass. Remembers
        the window (for nudge) and writes the waveform-view file.
        `tid` = the ring to read (nudge passes the ring the loop CAME FROM);
        None = the deck's current grab source (a fresh capture)."""
        d = self.decks.get(deck)
        if d is None:
            return
        if tid is None:
            tid = int(self._deck_src.get(deck, 0))
            ring = self._ring_for(deck)         # the ring THIS deck grabs from
        else:
            ring = self._make_ring(tid)         # the ring this LOOP came from
        F  = self._seam_frames
        fb = ring.channels * 2
        read_end = (int(end_byte) + F * fb) % ring.buffer_bytes
        buf = ring.snapshot_frames(frames + F, mono=True,
                                   end_byte=read_end)
        if len(buf) < frames:
            return
        F = min(F, len(buf) - frames)
        # Normalization: compute gain once per grab and REUSE it on nudge
        # re-slices, so shifting the window can't pump the level around.
        if self.normalize and buf.size:
            if gain is None:
                peak = float(np.max(np.abs(buf[:frames])))
                gain = (min(self.target_peak / peak, self.max_gain)
                        if peak > self.silence_peak else 1.0)
            buf = (buf * gain).astype(np.float32)
        else:
            gain = 1.0
        body = np.ascontiguousarray(buf[:frames], dtype=np.float32)
        if 0 < F < frames:
            up = np.linspace(0.0, 1.0, F, dtype=np.float32)
            body[:F] = body[:F] * up + buf[frames:frames + F] * (1.0 - up)
        # Short boundary fades: guarantee a silent seam regardless of where
        # the window lands in the audio (the render loop wraps at n-1, so a
        # non-zero crossing produces a tiny pop every loop pass). 5ms is
        # inaudible in any loop longer than ~100ms. Applied after the seam
        # crossfade so the two don't fight: the crossfade handles continuity
        # across the wrap; these fades ensure the wrap is near-silent.
        _sg = min(int(self.sr * 0.005), frames // 8)  # 5ms, max 1/8 of loop
        if _sg > 1:
            _ramp = np.linspace(0.0, 1.0, _sg, dtype=np.float32)
            body[:_sg]          *= _ramp          # fade in from silence
            body[frames - _sg:] *= _ramp[::-1]   # fade out to silence
        self._cap_meta[deck] = {"end_byte": int(end_byte) % ring.buffer_bytes,
                                "frames": int(frames), "gain": float(gain),
                                "tid": int(tid)}   # the ring this window lives in
        if preserve:
            # nudge re-slice: keep whatever play state the deck already has
            d.swap_buffer(body)
        elif self.auto_arm:
            # auto-arm: start looping the grab immediately, phase-locked to
            # the live grid so it drops in on the groove (old default).
            d.load_buffer(body)
            if phase_ms is not None and len(body) > 1:
                # the window ends phase_ms ago at a barline, so "now" is
                # phase_ms into the loop
                d._pos = float(((phase_ms / 1000.0) * self.sr) % (len(body) - 1))
        else:
            # cued (default): load stopped at the loop start, armed but
            # silent, waiting for the user to press PLAY/LOOP. phase_ms is
            # irrelevant when we're not auto-playing.
            d.load_buffer_cued(body)
        self._write_view(deck, body)
        self._persist_deck(deck, body)

    def load_file(self, deck, path, max_seconds=None):
        """File deck: decode an audio file (anything sox reads — wav, mp3,
        flac...) to 48k mono and load it into a deck CUED (stopped at 0,
        loop on). The decode runs on a worker thread (a 300s mp3 takes a
        couple of seconds on the Pi) so the MIDI/control thread never
        blocks. Once loaded, the deck plays/scratches/EQs the file exactly
        like a capture — and bar-grabbing its playback into the other deck
        is live resampling for free (the ring hears the synth)."""
        d = self.decks.get(deck)
        if d is None:
            return
        max_s = float(max_seconds or self._load_max_s)
        import subprocess, threading

        def _work():
            try:
                cmd = ["sox", str(path), "-t", "raw", "-r", str(self.sr),
                       "-e", "signed", "-b", "16", "-c", "1", "-",
                       "trim", "0", str(max_s)]
                r = subprocess.run(cmd, capture_output=True, timeout=180)
                raw = r.stdout
                if len(raw) < 4:
                    print(f"[sampler] load_file {deck}: decode produced no "
                          f"audio ({path}): {r.stderr.decode()[:200]}",
                          flush=True)
                    return
                buf = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                if self.normalize and buf.size:
                    peak = float(np.max(np.abs(buf)))
                    if peak > self.silence_peak:
                        buf = buf * min(self.target_peak / peak, self.max_gain)
                buf = np.ascontiguousarray(buf, dtype=np.float32)
                self._cap_meta.pop(deck, None)   # not a ring window: nudge n/a
                d.load_buffer_cued(buf)
                self._write_view(deck, buf)
                self._persist_deck(deck, buf)
                print(f"[sampler] deck {deck} <- {len(buf)/self.sr:.1f}s "
                      f"from {path}", flush=True)
            except Exception as e:
                print(f"[sampler] load_file failed: {e}", flush=True)

        threading.Thread(target=_work, daemon=True).start()

    def nudge(self, deck, delta_ms):
        """Shift a deck's capture window by delta_ms in the ring and re-slice
        it — the 'fix the grab by ear' control. Positive = later (toward
        now). Play state, loop phase and normalization gain are preserved;
        only the audio under the window changes."""
        # FIRST statement, above the `meta = ...` read -- placement is
        # load-bearing and the wrong order was MEASURED to produce a
        # 24-second window error: `meta` binds before the refresh, so
        # clearing _cap_meta afterwards leaves the already-bound stale meta
        # in hand and the math below mixes two different moduli. Refreshing
        # first means a changed ring makes this no-op via the existing
        # `not meta` guard (same outcome as load_file's _cap_meta.pop) --
        # logged, because an unexplained silent no-op is its own bug report.
        # The window lives in the ring the loop was GRABBED from (meta "tid"),
        # NOT necessarily the deck's current source: with per-deck ring buttons
        # you switch sources freely, and a nudge must keep editing the loop you
        # have (2026-09-23; before, a source switch had to drop the window).
        _m0 = self._cap_meta.get(deck) or {}
        tid = _m0.get("tid")
        try:
            ring = (self._make_ring(tid) if tid is not None
                    else self._ring_for(deck))
        except Exception:
            ring = self._ring_for(deck)
        try:
            if ring.refresh_geometry():
                self._cap_meta.clear()
                print(f"[sampler] nudge {deck}: ring geometry changed "
                      f"({ring.buffer_bytes}B {ring.channels}ch) -- "
                      f"capture window dropped, nudge ignored", flush=True)
        except Exception as e:
            print(f"[sampler] geometry refresh failed: {e}", flush=True)
        meta = self._cap_meta.get(deck)
        d = self.decks.get(deck)
        if not meta or d is None or not d.loaded():
            return
        fb = ring.channels * 2
        # ring.rate: ms -> RING bytes (see _do_capture).
        delta_b = int(round(float(delta_ms) / 1000.0 * ring.rate)) * fb
        if delta_b == 0:
            return
        pos, _extra = ring.head_info()
        B  = ring.buffer_bytes
        F  = self._seam_frames
        # NOTE: meta["frames"] and F are SYNTH frames being multiplied by the
        # RING frame width. Valid only because both rates are pinned to 48000
        # (skipback_recorder.py:88 and the synth's JACK rate). If the ring rate
        # ever becomes independent, these need converting, not just renaming.
        total_b = (meta["frames"] + F) * fb
        behind  = (pos - meta["end_byte"]) % B         # window age in bytes
        behind -= delta_b                              # + = later = closer to now
        # keep the whole window (plus seam pad) inside readable ring history,
        # with 1s of slack off the oldest edge
        behind  = max(F * fb, min(behind, B - total_b - ring.rate * fb))
        behind -= behind % fb
        new_end = (pos - behind) % B
        if new_end == meta["end_byte"]:
            return
        self._load_slice(deck, new_end, meta["frames"],
                         gain=meta.get("gain"), preserve=True,
                         tid=meta.get("tid"))

    def _write_view(self, deck, body):
        """Write a small waveform-peaks JSON (~/.orc1_sampler_view_<deck>.json,
        atomic, on a throwaway thread) so the web UI can SHOW what got
        grabbed instead of leaving captures a guessing game."""
        try:
            n = len(body)
            if n < 2:
                return
            buckets = 240
            idx = np.linspace(0, n, buckets + 1).astype(np.int64)
            peaks = [[round(float(body[idx[i]:max(idx[i] + 1, idx[i + 1])].min()), 3),
                      round(float(body[idx[i]:max(idx[i] + 1, idx[i + 1])].max()), 3)]
                     for i in range(buckets)]
            _d = self.decks.get(deck)
            data = {"deck": deck, "length_s": round(n / self.sr, 3),
                    "ts": time.time(), "peaks": peaks,
                    "start": round(float(getattr(_d, "_start_frac", 0.0)), 4),
                    "end":   round(float(getattr(_d, "_end_frac", 1.0)), 4),
                    "cues":  [None if c is None else round(float(c), 4)
                              for c in getattr(_d, "_cues", [None] * 4)]}
            import threading, json
            path = os.path.expanduser(f"~/.orc1_sampler_view_{deck}.json")

            # UNIQUE tmp name per write: refresh_view() (region drag / CC) can
            # fire this many times a second, so several writer threads run at
            # once.  A shared "<path>.tmp" let them interleave into the same
            # file before os.replace -- producing a corrupt, double-object JSON
            # ("Extra data") that the page then fails to parse.  A per-thread
            # tmp keeps each write whole; os.replace is atomic, last writer
            # wins, and every intermediate file is valid.
            def _w():
                tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
                try:
                    with open(tmp, "w") as f:
                        json.dump(data, f)
                    os.replace(tmp, path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass

            threading.Thread(target=_w, daemon=True).start()
        except Exception:
            pass

    def control(self, deck, action, *args):
        d = self.decks.get(deck)
        if d is None:
            return
        fn = getattr(d, action, None)
        if callable(fn):
            fn(*args)

    def refresh_view(self, deck):
        """Rewrite a deck's waveform-view JSON to reflect a LIVE region change
        (set_bounds via a mapped CC or the /sampler drag), so a page (re)load
        and any controller-driven change show the correct start/end markers --
        not the region as of the last capture.  Cheap: reuses _write_view (the
        peaks recompute is a few hundred min/max ops; only the file write is
        threaded).  No-op on an empty deck."""
        d = self.decks.get(deck)
        if d is not None and d.loaded():
            self._write_view(deck, d._data)

    def export(self, deck, out_dir):
        """Save a deck's captured buffer to out_dir as a mono 16-bit 48 kHz
        WAV. The disk write happens on a throwaway daemon thread so the
        control/MIDI thread never blocks on I/O."""
        d = self.decks.get(deck)
        if d is None or not d.loaded():
            print(f"[sampler] export {deck}: nothing captured")
            return
        buf, sr = d._data, self.sr        # reference grab — safe vs swaps
        import threading, time as _time, os as _os, wave as _wave

        def _write():
            try:
                _os.makedirs(out_dir, exist_ok=True)
                name = _time.strftime(f"capture_{deck}_%Y%m%d_%H%M%S.wav")
                path = _os.path.join(out_dir, name)
                pcm = (np.clip(buf, -1.0, 1.0) * 32767.0).astype('<i2')
                with _wave.open(path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(pcm.tobytes())
                print(f"[sampler] exported {len(buf)/sr:.2f}s -> {path}",
                      flush=True)
            except Exception as e:
                print(f"[sampler] export failed: {e}", flush=True)

        threading.Thread(target=_write, daemon=True).start()

    # ---- deck persistence (survive a reboot / synth restart) ----------------

    def _deck_persist_path(self, deck):
        return os.path.join(self._persist_dir,
                            f".orc1_sampler_deck_{deck}.wav")

    def _persist_deck(self, deck, buf):
        """Write a deck's current body to disk so it survives a restart.
        Called right after _write_view on every grab / file-load / nudge. Mono
        16-bit 48k WAV, atomic (tmp + os.replace), on a throwaway daemon thread
        so the control/MIDI thread never blocks on I/O."""
        if not self.persist_decks:
            return
        try:
            data = np.ascontiguousarray(buf, dtype=np.float32)
        except Exception:
            return
        if data.size < 2:
            return
        sr   = self.sr
        path = self._deck_persist_path(deck)
        import threading, wave as _wave

        def _w():
            try:
                pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype('<i2')
                tmp = path + ".tmp"
                with _wave.open(tmp, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(pcm.tobytes())
                os.replace(tmp, path)
            except Exception as e:
                print(f"[sampler] persist {deck} failed: {e}", flush=True)

        threading.Thread(target=_w, daemon=True).start()

    def _restore_decks(self):
        """Reload each deck's persisted body at startup, CUED (stopped at 0,
        loop-armed, silent). Synchronous and per-deck fault-isolated: a missing
        or unreadable file just leaves that deck empty and never blocks the
        synth from coming up. The body on disk is already normalized, so this
        does NOT re-apply capture gain."""
        import wave as _wave
        for deck, d in self.decks.items():
            path = self._deck_persist_path(deck)
            try:
                if not os.path.exists(path):
                    continue
                with _wave.open(path, "rb") as w:
                    nch     = w.getnchannels()
                    sw      = w.getsampwidth()
                    nframes = w.getnframes()
                    raw     = w.readframes(nframes)
                if sw != 2 or len(raw) < 4:
                    print(f"[sampler] restore {deck}: unexpected format "
                          f"({sw*8}-bit), skipped", flush=True)
                    continue
                buf = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                if nch > 1:                       # fold to mono if ever stereo
                    buf = buf.reshape(-1, nch).mean(axis=1)
                buf = np.ascontiguousarray(buf, dtype=np.float32)
                self._cap_meta.pop(deck, None)    # restored: no live ring window
                d.load_buffer_cued(buf)
                self._write_view(deck, buf)
                print(f"[sampler] deck {deck} restored "
                      f"{len(buf)/self.sr:.1f}s from {path}", flush=True)
            except Exception as e:
                print(f"[sampler] restore {deck} failed: {e}", flush=True)

    def set_crossfade(self, value01):
        self._xf = float(np.clip(value01, 0.0, 1.0))

    def set_master_gain(self, g):
        """Master output level for the whole sampler mix (both decks)."""
        self._master_gain = float(np.clip(g, 0.0, 2.0))

    def status(self):
        return {"crossfade": round(self._xf, 3),
                "decks": {k: v.status() for k, v in self.decks.items()}}

    # ---- realtime -----------------------------------------------------------

    def render(self, n_frames):
        """Mix all decks with equal-power crossfade. Returns (n,) float32 or None."""
        a = self.decks["A"].render(n_frames)
        b = self.decks["B"].render(n_frames)
        if a is None and b is None:
            return None
        # equal-power crossfade gains, RAMPED across the block from the previous
        # block's gains so a fast fader move is a smooth per-sample glide, not a
        # step at the seam (declick, 2026-08-12).
        ga = float(np.cos(self._xf * np.pi / 2.0))
        gb = float(np.sin(self._xf * np.pi / 2.0))
        ga_r = np.linspace(self._ga_prev, ga, n_frames, dtype=np.float32)
        gb_r = np.linspace(self._gb_prev, gb, n_frames, dtype=np.float32)
        self._ga_prev = ga
        self._gb_prev = gb
        out = np.zeros(n_frames, dtype=np.float32)
        if a is not None:
            out += a * ga_r
        if b is not None:
            out += b * gb_r
        if self._master_gain != 1.0:
            out *= self._master_gain
        return out


# ==============================================================================
# STANDALONE SELF-TEST  —  proves the ring tap without touching the synth
# ==============================================================================

def _selftest(seconds):
    import wave
    ring = RingBufferReader(
        os.path.expanduser("~/.skipbuf/ring.raw"),
        os.path.expanduser("~/.skipbuf/ring.pos"),
        buffer_secs=1860, rate=48000, channels=2)
    print(f"ring file present + correct size: {ring.available()}")
    print(f"write head (ring.pos bytes): {ring._read_pos()}")
    buf = ring.snapshot(seconds, mono=True)
    dur = len(buf) / ring.rate
    peak = float(np.abs(buf).max()) if len(buf) else 0.0
    rms  = float(np.sqrt(np.mean(buf**2))) if len(buf) else 0.0
    print(f"captured {len(buf)} frames = {dur:.2f}s  peak={peak:.3f}  rms={rms:.4f}")
    out = f"/tmp/sampler_selftest_{int(seconds)}s.wav"
    pcm = np.clip(buf * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ring.rate)
        w.writeframes(pcm.tobytes())
    print(f"wrote {out}")
    if peak < 1e-4:
        print("NOTE: near-silent capture — either nothing was playing, or the "
              "skipback daemon isn't currently feeding the ring.")


if __name__ == "__main__":
    import sys
    secs = 8
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        if i + 1 < len(sys.argv):
            secs = float(sys.argv[i + 1])
    _selftest(secs)
