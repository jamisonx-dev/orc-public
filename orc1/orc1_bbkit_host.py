#!/usr/bin/env python3
"""
ORC-1 BeatBuddy Kit host -- a SAMPLED, full-articulation drum-kit engine as its
own JACK client process (2026-09-14, BeatBuddy module step 1 "kit host").
============================================================================

WHY / HOW  (follows the orc1_drum_host.py / orc1_sampler_host.py pattern)
-------------------------------------------------------------------------
This is the SOUND half of the ORC-native BeatBuddy. Unlike the 909 drum host
(11 synthesised voices), this plays real multisampled kits at FULL fidelity:
every articulation is its own note, each with velocity layers + round-robin
variants + choke groups + polyphony -- exactly the model BeatBuddy `.DRM` kits
use (we cracked that format; see _beatbuddy/tools/ + _planning/ORC1_BeatBuddy_Spec.md).

  * KITS: loaded from a directory of `<kit>/kit.json` manifests (the openbbm-
    compatible model our drm2kit.py emits: note -> instrument -> velocity layers
    -> round-robin WAVs, + choke/poly/volume). One kit is resident at a time.
    A Program Change on the kit channel selects among the scanned kits.
  * MIDI: ALSA-seq fan-out -- opens the same ORC1-Synth-Bus the other modules
    read. Handles its bound channel (note_on -> articulation, program_change ->
    kit select) and the CH_CTRL bbkit-bus level CC. Uses the standard bind
    handshake (ARM/COMMIT/CLEAR) so the module matrix can (re)bind it live.
  * AUDIO: registers out_L/out_R (real stereo -- kits are stereo), auto-connected
    to the physical outs. Per-host PeakLimiter (reused from orc1_synth).
  * PERSIST: the "bbkit" bus level in the shared ~/.orc1_bus_levels.json (merge
    write via S._bus_level_write) and the last-selected kit in ~/.orc1_bbkit.json.

The MIDI thread only ENQUEUES; the JACK RT thread renders. Kit (re)loads run on
a background thread and are swapped in atomically (active voices keep their own
buffer refs, so an in-flight hit is never yanked out from under the RT thread).

⚠ The velocity->gain curve here (linear vel/127 * volumes) is a reasonable v1;
matching BeatBuddy's exact playback gain would port soundManager.c -- flagged for
an ear pass, but LAYER SELECTION and ROUND-ROBIN (the audible part) are already
exactly the kit's own mapping.
"""

import os
import sys
import json
import glob
import time
import queue
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S          # SAMPLE_RATE, PeakLimiter, bind consts, helpers

JACK_NAME     = S._SYNTH_CFG.get("BBKIT_JACK_NAME", "ORC1-BBKit")
MODULE_NAME   = "bbkit"
SR            = S.SAMPLE_RATE
# bbkit bus-level CC on the control channel (CC7 master/8 poly/9 chip/10 drum/
# 11 mono are taken; 12 is the next free). Overridable from config.
_VOL_BBKIT_CC = int(S._SYNTH_CFG.get("BBKIT_VOL_CC", 12))
_BBKIT_BOOT   = int(S._SYNTH_CFG.get("BBKIT_BOOT", 100))
# Fallback channel used only until the module matrix binds "bbkit" (config step).
_BBKIT_CH_FALLBACK = int(S._SYNTH_CFG.get("BBKIT_CH", 5))
CEIL          = float(S._SYNTH_CFG.get("BBKIT_FADER_CEIL", 1.0))

KITS_DIR = S._SYNTH_CFG.get("BBKIT_DIR") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "kits")
STATE_FILE = os.path.expanduser("~/.orc1_bbkit.json")
_CHOKE_FADE = max(1, int(0.006 * SR))     # ~6ms declick fade for choke/steal


# ============================================================================
#  Kit data model
# ============================================================================
class Layer:
    __slots__ = ("lo", "hi", "rr", "_i")
    def __init__(self, lo, hi, rr):
        self.lo, self.hi, self.rr, self._i = lo, hi, rr, 0
    def pick(self):
        """Round-robin through this layer's variants (BeatBuddy behaviour)."""
        buf = self.rr[self._i % len(self.rr)]
        self._i += 1
        return buf


class Instrument:
    __slots__ = ("note", "name", "choke", "poly", "gain", "layers")
    def __init__(self, note, name, choke, poly, volume, layers):
        self.note   = note
        self.name   = name
        self.choke  = choke                       # 0 = none, 1..15 = group
        self.poly   = poly if poly and poly > 0 else 64
        self.gain   = (volume or 100) / 100.0     # per-instrument volume
        self.layers = layers                      # [Layer], ascending lo
    def layer_for(self, vel):
        for L in self.layers:
            if L.lo <= vel <= L.hi:
                return L
        return self.layers[-1] if self.layers else None


def _read_wav_stdlib(path):
    """stdlib PCM WAV reader (8/16/24/32-bit) -> (sr, float32 [N,ch] in [-1,1]).
    Fallback for when scipy isn't installed."""
    import wave
    w = wave.open(path, "rb")
    try:
        nch, sw, sr, n = (w.getnchannels(), w.getsampwidth(),
                          w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    finally:
        w.close()
    if sw == 3:                                   # 24-bit -> 32-bit
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16))
        val = np.where(val & 0x800000, val - 0x1000000, val)
        data = val.reshape(-1, nch).astype(np.float32) / 8388608.0
    else:
        dt = {1: np.uint8, 2: np.int16, 4: np.int32}[sw]
        data = np.frombuffer(raw, dtype=dt).reshape(-1, nch).astype(np.float32)
        if sw == 1:
            data = (data - 128.0) / 128.0
        else:
            data /= float(np.iinfo(dt).max + 1)
    return sr, data


def _resample_to_sr(data, sr):
    """Resample [N,ch] from sr to SR. scipy (FFT, best) if present, else a
    numpy linear-interp fallback (fine for drum one-shots)."""
    if sr == SR:
        return data
    new_n = int(round(len(data) * SR / sr))
    if new_n < 1:
        return data
    try:
        from scipy.signal import resample
        return resample(data, new_n, axis=0).astype(np.float32)
    except Exception:
        x_old = np.linspace(0.0, 1.0, len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_n, endpoint=False)
        return np.column_stack([np.interp(x_new, x_old, data[:, c])
                                for c in range(data.shape[1])]).astype(np.float32)


def _load_wav_stereo(path):
    """Read a WAV -> float32 [N,2] at SR (resampled if needed). Dynamics are
    PRESERVED (no per-sample peak normalise -- the velocity layers ARE the
    dynamics); we only convert int PCM to [-1,1]."""
    try:
        import scipy.io.wavfile as wf
        sr, data = wf.read(path)
        if data.dtype.kind in "iu":
            data = data.astype(np.float32) / float(np.iinfo(data.dtype).max + 1)
        else:
            data = data.astype(np.float32)
    except ImportError:
        sr, data = _read_wav_stdlib(path)
    if data.ndim == 1:
        data = np.column_stack([data, data])
    elif data.shape[1] == 1:
        data = np.column_stack([data[:, 0], data[:, 0]])
    elif data.shape[1] > 2:
        data = data[:, :2]
    data = _resample_to_sr(data, sr)
    return np.ascontiguousarray(data, dtype=np.float32)


# ── STREAMED SAMPLES (2026-09-23) ────────────────────────────────────────────
# A kit used to be decoded whole into float32 RAM: ~2x its on-card size (~270 MB
# private for the 102 MB Standard kit), held for the life of the process -- and
# owner's rule is that the skipback recorder comes first. Now a 16-bit / 48 kHz
# PCM sample keeps only its HEAD (first BBKIT_HEAD_MS, default 100 ms) in RAM as
# 16-bit, and memory-maps the rest straight from the WAV on the card. The kernel
# page cache holds whatever has been read -- reclaimable, so under memory pressure
# the kernel drops it before anything that matters.
# Latency: MEASURED on this Pi's microSD (caches dropped): a cold 4 KB read is
# 3.8 ms median / 7 ms worst; the next 256 KB (~1.4 s of stereo audio) 7-10 ms --
# vs the 100 ms head a hit starts playing from. Reads are requested AHEAD of the
# audio loop (posix_fadvise WILLNEED: at kit load, and again on every incoming
# note in the MIDI thread, which runs before the JACK callback plays it), so the
# render normally only touches resident pages. Proven on the rig by the process's
# major-fault counter staying flat during playback.
# Anything that is not plain 16-bit PCM at SR (4 kit files are 32-bit) falls back
# to the old full float32 load -- identical sound, just resident.
HEAD_MS = float(S._SYNTH_CFG.get("BBKIT_HEAD_MS", 100.0))
_HEAD_FRAMES = max(1, int(SR * HEAD_MS / 1000.0))
_POSIX_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", None)


def _wav_pcm_info(path):
    """(rate, channels, bits, data_offset, frames, fmt_tag) from a RIFF/WAVE
    header, or None if it is not a parseable WAV. fmt_tag 1 = PCM (0xFFFE =
    WAVE_FORMAT_EXTENSIBLE; its sub-format is PCM for integer data)."""
    import struct
    with open(path, "rb") as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            return None
        fmt = None
        while True:
            ck = f.read(8)
            if len(ck) < 8:
                return None
            cid, size = ck[:4], struct.unpack("<I", ck[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                tag, ch, rate = struct.unpack("<HHI", body[:8])
                bits = struct.unpack("<H", body[14:16])[0]
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]   # sub-format GUID head
                fmt = (rate, ch, bits, tag)
            elif cid == b"data":
                if fmt is None:
                    return None
                rate, ch, bits, tag = fmt
                frames = size // max(1, ch * bits // 8)
                return rate, ch, bits, f.tell(), frames, tag
            else:
                f.seek(size + (size & 1), 1)
            if cid == b"fmt " and size & 1:
                f.seek(1, 1)


class SampleBuf:
    """One kit sample. Presents as a [frames, 2] stereo sample (like the old
    float32 array: `.shape`, `len()`, `np.asarray()`), but a 16-bit/SR PCM file
    keeps only its HEAD in RAM and streams the rest from the card (see above).
    `seg(pos, k)` returns float32 [k, ch] exactly as the old array slice was:
    int16 -> float32 / 32768 (a power of two, so bit-identical)."""
    __slots__ = ("path", "n", "ch", "head", "mm", "full", "off", "fb")

    def __init__(self, path):
        self.path, self.head, self.mm, self.full = path, None, None, None
        info = None
        try:
            info = _wav_pcm_info(path)
        except Exception:
            info = None
        if info and info[0] == SR and info[2] == 16 and info[5] == 1 and info[4] > 0:
            rate, ch, bits, off, frames, tag = info
            self.n, self.ch, self.off, self.fb = frames, ch, off, ch * 2
            self.mm = np.memmap(path, dtype="<i2", mode="r", offset=off,
                                shape=(frames, ch))
            self.head = np.array(self.mm[:_HEAD_FRAMES])           # RAM copy
        else:
            self.full = _load_wav_stereo(path)                    # old path
            self.n, self.ch = len(self.full), 2

    @property
    def streamed(self):
        return self.mm is not None

    @property
    def shape(self):
        return (self.n, 2)

    def __len__(self):
        return self.n

    def seg(self, pos, k):
        """float32 [k, ch] for frames [pos, pos+k). Head frames come from RAM;
        later ones from the mapped file (normally already resident)."""
        if self.full is not None:
            return self.full[pos:pos + k]
        H = len(self.head)
        end = pos + k
        if end <= H:
            raw = self.head[pos:end]
        elif pos >= H:
            raw = self.mm[pos:end]
        else:
            raw = np.concatenate((self.head[pos:H], self.mm[H:end]))
        return raw.astype(np.float32) * np.float32(1.0 / 32768.0)

    def warm(self):
        """Ask the kernel to read this sample's body (past the head) into the
        page cache ASYNCHRONOUSLY. Never called from the JACK callback."""
        if self.mm is None or _POSIX_WILLNEED is None or self.n <= len(self.head):
            return
        try:
            fd = os.open(self.path, os.O_RDONLY)
            try:
                start = self.off + len(self.head) * self.fb
                os.posix_fadvise(fd, start, (self.n - len(self.head)) * self.fb,
                                 _POSIX_WILLNEED)
            finally:
                os.close(fd)
        except Exception:
            pass

    def __array__(self, dtype=None, copy=None):
        """Materialize as the old float32 [N, 2] (tests / introspection only)."""
        a = self.seg(0, self.n)
        if a.shape[1] == 1:
            a = np.column_stack((a[:, 0], a[:, 0]))
        elif a.shape[1] > 2:
            a = a[:, :2]
        a = np.ascontiguousarray(a, dtype=np.float32)
        return a if dtype is None else a.astype(dtype)


def load_kit(path):
    """Build {note:int -> Instrument} from a <kit>/kit.json + its WAVs."""
    with open(os.path.join(path, "kit.json"), encoding="utf-8") as f:
        man = json.load(f)
    cache = {}
    def getwav(fn):
        if fn not in cache:
            try:
                cache[fn] = SampleBuf(os.path.join(path, fn))
                cache[fn].warm()                    # async read-ahead, off the RT path
            except Exception as e:
                print(f"[bbkit] wav load failed {fn}: {e}", flush=True)
                cache[fn] = np.zeros((1, 2), dtype=np.float32)
        return cache[fn]
    instruments = {}
    for note_s, ins in man.get("instruments", {}).items():
        layers = []
        for L in ins.get("layers", []):
            rr = [getwav(v["wav"]) for v in L.get("rr", []) if v.get("wav")]
            if rr:
                layers.append(Layer(int(L["lo"]), int(L.get("hi", 127)), rr))
        if layers:
            note = int(note_s)
            instruments[note] = Instrument(note, ins.get("name", f"n{note}"),
                                           int(ins.get("choke", 0)),
                                           int(ins.get("poly", 0)),
                                           int(ins.get("volume", 100)), layers)
    return man.get("name", os.path.basename(path)), instruments


# ============================================================================
#  Playback voice (one live sample hit)
# ============================================================================
class Voice:
    __slots__ = ("buf", "pos", "gain", "note", "choke", "pre", "fade_left", "fade_len", "done")
    def __init__(self, buf, gain, note, choke, pre):
        self.buf   = buf
        self.pos   = 0
        self.gain  = gain
        self.note  = note
        self.choke = choke
        self.pre   = pre          # silent frames before start within the FIRST block
        self.fade_left = -1       # -1 = not fading
        self.fade_len  = _CHOKE_FADE
        self.done  = False

    def start_fade(self):
        if self.fade_left < 0:
            self.fade_left = self.fade_len

    def mix(self, outL, outR, n):
        start = self.pre
        self.pre = 0
        if start >= n:
            self.pre = start - n
            return
        avail = len(self.buf) - self.pos
        k = min(n - start, avail)
        if k <= 0:
            self.done = True
            return
        if isinstance(self.buf, np.ndarray):
            seg = self.buf[self.pos:self.pos + k]
        else:
            seg = self.buf.seg(self.pos, k)     # SampleBuf: head RAM / mapped file
        r = 1 if seg.shape[1] > 1 else 0        # mono sample -> same data both sides
        g = self.gain
        if self.fade_left >= 0:
            f0 = self.fade_left / self.fade_len
            f1 = max(0, self.fade_left - k) / self.fade_len
            ramp = np.linspace(f0, f1, k, endpoint=False, dtype=np.float32)
            outL[start:start + k] += seg[:, 0] * (g * ramp)
            outR[start:start + k] += seg[:, r] * (g * ramp)
            self.fade_left -= k
            if self.fade_left <= 0:
                self.done = True
        else:
            outL[start:start + k] += seg[:, 0] * g
            outR[start:start + k] += seg[:, r] * g
        self.pos += k
        if self.pos >= len(self.buf):
            self.done = True


# ============================================================================
#  Engine
# ============================================================================
def _load_bbkit_bus_level():
    try:
        with open(S.BUS_LEVELS_FILE) as f:
            v = json.load(f).get("bbkit")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 127 else None
    except Exception:
        return None


class KitEngine:
    def __init__(self):
        self.queue   = queue.Queue(maxsize=1024)
        self._jack_ft = None
        self.lim     = S.PeakLimiter()
        self.voices  = []                       # active Voice list (RT-owned)
        self.instruments = {}                   # note -> Instrument (swapped atomically)
        self.kit_name = None

        # scan available kits
        self.kits = []
        if os.path.isdir(KITS_DIR):
            for d in sorted(glob.glob(os.path.join(KITS_DIR, "*"))):
                if os.path.isfile(os.path.join(d, "kit.json")):
                    self.kits.append(d)
        self._kit_idx = 0

        # bus master
        self.master = _BBKIT_BOOT / 127.0
        self._bus_dirty = False
        _saved = _load_bbkit_bus_level()
        if _saved is not None:
            self.master = _saved / 127.0

        # choose initial kit: last-used name if present, else index 0
        want = None
        try:
            with open(STATE_FILE) as f:
                want = json.load(f).get("kit")
        except Exception:
            pass
        start = 0
        if want:
            for i, d in enumerate(self.kits):
                if os.path.basename(d) == want:
                    start = i
                    break
        if self.kits:
            self._do_load(start)
        else:
            print(f"[bbkit] no kits found under {KITS_DIR}", flush=True)
        print(f"[bbkit] {len(self.kits)} kit(s); bus level "
              f"{int(round(self.master*127))}"
              + ("" if _saved is None else " (restored)"), flush=True)

    # -- kit loading (background-thread safe; atomic swap) ---------------------
    def _do_load(self, idx):
        idx = idx % len(self.kits)
        path = self.kits[idx]
        try:
            name, instruments = load_kit(path)
        except Exception as e:
            print(f"[bbkit] kit load failed {path}: {e}", flush=True)
            return
        self.instruments = instruments          # atomic ref swap
        self.kit_name = name
        self._kit_idx = idx
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"kit": os.path.basename(path)}, f)
        except Exception:
            pass
        print(f"[bbkit] kit -> {name!r} ({len(instruments)} instruments, "
              f"{os.path.basename(path)})", flush=True)

    def select_kit(self, idx):
        if not self.kits:
            return
        threading.Thread(target=self._do_load, args=(idx,), daemon=True).start()

    # -- MIDI-thread side (enqueue only) --------------------------------------
    def _enqueue(self, item):
        ft = None
        if self._jack_ft is not None:
            try:
                ft = self._jack_ft()
            except Exception:
                ft = None
        try:
            self.queue.put_nowait((ft, item))
        except queue.Full:
            pass

    def note_on(self, note, vel):
        self._enqueue(("note", note, vel))
        # MIDI thread, BEFORE the JACK callback plays it: re-request every
        # round-robin variant of the layer this hit will use (the RT side
        # picks the variant), in case the page cache dropped it. Async,
        # cheap when already resident. Never done in render().
        try:
            ins = self.instruments.get(note)
            L = ins.layer_for(vel) if ins is not None else None
            for b in (L.rr if L is not None else ()):
                if hasattr(b, "warm"):
                    b.warm()
        except Exception:
            pass

    def program(self, prog):
        self._enqueue(("pc", prog))

    def _ev_offset(self, ft, frame_time, n):
        if ft is None or frame_time is None:
            return 0
        base = (int(frame_time) - n) & 0xFFFFFFFF
        off  = (int(ft) - base) & 0xFFFFFFFF
        if off < n:
            return off
        return (n - 1) if off < 0x80000000 else 0

    def _trigger(self, note, vel, off):
        ins = self.instruments.get(note)
        if ins is None:
            return
        L = ins.layer_for(vel)
        if L is None:
            return
        buf = L.pick()
        gain = (vel / 127.0) * ins.gain
        # choke: fade group-mates
        if ins.choke:
            for v in self.voices:
                if v.choke == ins.choke and not v.done:
                    v.start_fade()
        # polyphony: fade oldest same-note voices beyond the limit
        same = [v for v in self.voices if v.note == note and v.fade_left < 0 and not v.done]
        if len(same) >= ins.poly:
            for v in same[:len(same) - ins.poly + 1]:
                v.start_fade()
        self.voices.append(Voice(buf, gain, note, ins.choke, off))

    def render(self, n, frame_time=None):
        trigs = []
        while not self.queue.empty():
            try:
                ft, item = self.queue.get_nowait()
            except queue.Empty:
                break
            k = item[0]
            if k == "note":
                trigs.append((self._ev_offset(ft, frame_time, n), item[1], item[2]))
            elif k == "pc":
                self.select_kit(item[1])
        for off, note, vel in trigs:
            self._trigger(note, vel, off)

        outL = np.zeros(n, dtype=np.float32)
        outR = np.zeros(n, dtype=np.float32)
        for v in self.voices:
            v.mix(outL, outR, n)
        if self.voices:
            self.voices = [v for v in self.voices if not v.done]

        m = self.master * CEIL
        outL *= m
        outR *= m
        return self.lim.process([outL, outR])


def _make_router(eng):
    chs = {ch for ch, m in S._BINDINGS.items() if m == MODULE_NAME} or {_BBKIT_CH_FALLBACK}
    state = {"armed": False}

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1
        if msg.type == "control_change" and msg.control in (120, 123):
            # all-notes-off: fade everything
            for v in eng.voices:
                v.start_fade()
            return
        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                state["armed"] = (S._MODULE_BY_ID.get(msg.value) == MODULE_NAME)
                return
            if msg.control == S._BIND_CLEAR_CC:
                if S._MODULE_BY_ID.get(msg.value) == MODULE_NAME:
                    chs.clear()
                state["armed"] = False
                return
            if msg.control == _VOL_BBKIT_CC:
                eng.master = msg.value / 127.0
                eng._bus_dirty = True
                return
        if (state["armed"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            chs.clear()
            chs.add(ch)
            state["armed"] = False
            return
        if ch in chs:
            if msg.type == "note_on" and msg.velocity > 0:
                eng.note_on(msg.note, msg.velocity)
            elif msg.type == "program_change":
                eng.program(msg.program)

    return route


def midi_input_thread(eng, stop_event):
    import mido
    prefix = S._SYNTH_CFG.get("MIDI_PORT_PREFIX", "").lower()
    route = _make_router(eng)

    def find_port():
        names = mido.get_input_names()
        if prefix:
            for n in names:
                if prefix in n.lower():
                    return n
            return None
        return names[0] if names else None

    _last_err = 0.0
    while not stop_event.is_set():
        port_name = find_port()
        if not port_name:
            print("[bbkit] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[bbkit] MIDI in: {port_name}", flush=True)
        try:
            with mido.open_input(port_name) as port:
                for msg in port:
                    if stop_event.is_set():
                        break
                    try:
                        route(msg)
                    except Exception as re:
                        _now = time.time()
                        if _now - _last_err > 1.0:
                            _last_err = _now
                            print(f"[bbkit] MIDI route error "
                                  f"({getattr(msg,'type','?')}): {re}", flush=True)
        except Exception as e:
            print(f"[bbkit] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def persist_thread(eng, stop_event, poll_s=1.5):
    while not stop_event.wait(poll_s):
        if eng._bus_dirty:
            eng._bus_dirty = False
            S._bus_level_write({"bbkit": max(0, min(127, int(round(eng.master * 127.0))))})


def run_jack(eng, stop_event):
    import jack
    client = jack.Client(JACK_NAME)
    out_L = client.outports.register("out_L")
    out_R = client.outports.register("out_R")
    eng._jack_ft = (lambda: int(client.frame_time))

    @client.set_process_callback
    def process(frames):
        L, R = eng.render(frames, frame_time=int(client.last_frame_time))
        out_L.get_array()[:] = L
        out_R.get_array()[:] = R

    @client.set_shutdown_callback
    def shutdown(status, reason):
        print(f"[bbkit] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "bbkit",
                                 log=lambda m: print(f"[bbkit] {m}", flush=True))
        print(f"[bbkit] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[bbkit] JACK client stopped", flush=True)


def main():
    eng = KitEngine()
    stop_event = threading.Event()
    try:
        import signal as _signal
        def _on_term(_s, _f):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[bbkit] SIGTERM -- stopping", flush=True)
            stop_event.set()
        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[bbkit] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread, args=(eng, stop_event), daemon=True).start()
    threading.Thread(target=persist_thread, args=(eng, stop_event), daemon=True).start()
    fatal = False
    try:
        run_jack(eng, stop_event)
    except KeyboardInterrupt:
        print("\n[bbkit] Interrupted", flush=True)
    except Exception as e:
        print(f"[bbkit] Fatal: {e}", flush=True)
        # Exit NON-zero so systemd's Restart=on-failure retries. Swallowing this
        # exited 0 ("Deactivated successfully") and the unit stayed dead: seen
        # 2026-09-24 when an L-12 hotplug restarted orc1-jackd and bbkit came up
        # while jackd was still priming the card (server_failed). The other hosts
        # open their JACK client OUTSIDE this try, so theirs raised and retried.
        fatal = True
    finally:
        stop_event.set()
        try:
            if eng._bus_dirty:
                S._bus_level_write({"bbkit": max(0, min(127, int(round(eng.master * 127.0))))})
        except Exception:
            pass
    if fatal:
        sys.exit(1)


if __name__ == "__main__":
    main()
