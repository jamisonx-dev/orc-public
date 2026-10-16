#!/usr/bin/env python3
"""
ORC-1 Mono host -- the layered monophonic synth as its OWN JACK client process
(2026-09-03). The 5th module of the audio host split.
============================================================================

WHY / HOW  (see orc1_poly_host.py / orc1_chip_host.py for the pattern)
---------------------------------------------------------------------
Mono is a NEW engine, not a co-rendered one -- so unlike the poly/chip split there
was nothing to lift out of a shared render loop. `S.MonoSynth` (in orc1_synth.py,
the shared library) is a self-contained monophonic LAYERED tone generator:

  * one note at a time (last-note priority), built from up to N oscillator LAYERS
    stacked into that note -- each with its own waveform / semitone / detune /
    gain (the joined-P1&P2 voice-split style: "one big note", not chords),
  * PORTAMENTO (per-sample glide between notes; legato keeps the envelope),
  * NOTE-TRACKING ring mod (carrier = glided base freq x ratio, exact).

The host just wraps it: a queue-drained sample-accurate render harness (mono
note-on/off + CC), MIDI off the shared ORC1-Synth-Bus (owned by the BRAIN, which
already forwards any bound channel -> NO brain code change), out_L/out_R
auto-connected to the outs, its own PeakLimiter, and persistence.

  * Persistence: mono patch params (~/.orc1_mono_params.json) via the library's
    S._flush_mono_params, and the "mono" key of the SHARED ~/.orc1_bus_levels.json
    with a read-modify-write MERGE (like the drum host's "drum") so no other
    owner's keys are clobbered.
  * Gain staging: MonoSynth.render applies the mono bus master (perceptual taper x
    MONO_FADER_CEIL) internally; the host bakes the overall master in as a STATIC
    gain (saved "master", else MASTER_LEVEL) like the other hosts, then limits.

SAFETY: one JACK client, one ALSA-seq MIDI input, no ring reads. MIDI thread
enqueues only; the JACK RT thread renders; persistence runs on a non-RT thread.
"""

import os
import sys
import time
import queue
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S    # MonoSynth, config constants, persistence helpers

JACK_NAME = S._SYNTH_CFG.get("MONO_JACK_NAME", "ORC1-Mono")


def _static_master():
    saved = S._load_bus_levels()
    if "master" in saved:
        return saved["master"] / 127.0
    return S.MASTER_LEVEL


def _load_mono_bus_level():
    """Read the 'mono' key from the shared bus-levels file (0-127), or None."""
    try:
        import json
        with open(S.BUS_LEVELS_FILE) as f:
            data = json.load(f)
        v = data.get("mono") if isinstance(data, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return int(v) if 0 <= v <= 127 else None
    except Exception:
        return None


def _flush_mono_bus_level(engine):
    """Persist ONLY the 'mono' key via the shared, flock-protected writer -- the
    bus-levels file is multi-writer since the audio host split, so the merge +
    cross-process lock live in ONE place (S._bus_level_write)."""
    engine._mono_bus_dirty = False
    S._bus_level_write(
        {"mono": max(0, min(127, int(round(engine.mono.master * 127.0))))})


class MonoHost:
    """S.MonoSynth wrapped in a queue-drained, sample-accurate render harness."""

    def __init__(self):
        # Mono patch-persistence surface (matches S._flush_mono_params).
        self._mono_param_pending = {}    # {cc: value}
        self._mono_params_dirty  = False

        self.mono = S.MonoSynth()

        # -- Mono bus level restore (shared bus-levels file) --------------------
        self.mono.master = S._MONO_BOOT_CC / 127.0
        self._mono_bus_dirty = False
        _saved = _load_mono_bus_level()
        if _saved is not None:
            self.mono.master = _saved / 127.0

        # -- Mono patch restore (auto-remember-last) ----------------------------
        _mn = 0
        for _cc, _val in S._load_mono_params().items():
            try:
                _cc = int(_cc)
                if _cc in S._MONO_PERSIST_SKIP:
                    continue
                self.mono.set_cc(_cc, int(_val))
                _mn += 1
            except Exception:
                pass

        self.master_static = _static_master()
        print(f"[mono-host] mono bus level = "
              f"{max(0, min(127, int(round(self.mono.master*127))))}"
              f"  master(static) = {self.master_static:.2f}"
              f"  patch: {_mn} cc"
              + ("" if _saved is None else "  (level restored)"), flush=True)

        self.queue    = queue.Queue(maxsize=512)
        self._jack_ft = None
        # Per-host brickwall limiter.
        self.lim = S.PeakLimiter()

    # -- MIDI-thread side (enqueue only) ---------------------------------------
    def _enqueue(self, item):
        try:
            ft = self._jack_ft() if self._jack_ft is not None else None
        except Exception:
            ft = None
        critical = item[0] in ('mono_off', 'all_off')
        try:
            if critical:
                self.queue.put((ft, item), timeout=0.5)
            else:
                self.queue.put_nowait((ft, item))
        except queue.Full:
            if critical:
                print(f"[mono-host] WARNING: dropped {item[0]!r} after queue "
                      f"overflow -- a note may hang", flush=True)

    def mono_note_on(self, note, velocity):
        self._enqueue(('mono_on', note, velocity))

    def mono_note_off(self, note):
        self._enqueue(('mono_off', note))

    def mono_cc(self, cc, value):
        self._enqueue(('mono_cc', cc, value))

    def all_sound_off(self):
        self._enqueue(('all_off',))

    def _ev_offset(self, ft, frame_time, n_frames):
        if ft is None or frame_time is None:
            return 0
        base = (int(frame_time) - n_frames) & 0xFFFFFFFF
        off  = (int(ft) - base) & 0xFFFFFFFF
        if off < n_frames:
            return off
        return (n_frames - 1) if off < 0x80000000 else 0

    def _apply_note_event(self, item):
        kind = item[0]
        if kind == 'mono_on':
            _, note, vel = item
            self.mono.note_on(note, vel)
        elif kind == 'mono_off':
            _, note = item
            self.mono.note_off(note)
        elif kind == 'all_off':
            try:
                self.mono.all_notes_off()
            except Exception as e:
                print(f"[mono-host] panic: {e}", flush=True)

    def render(self, n_frames, frame_time=None):
        """Drain the queue, then render the mono voice, sample-accurate. Mono, so
        L==R; scaled by the static overall master (mono bus master applied inside
        MonoSynth.render), then the per-host limiter."""
        note_evs = []
        while not self.queue.empty():
            try:
                ft, item = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind in ('mono_on', 'mono_off', 'all_off'):
                note_evs.append((self._ev_offset(ft, frame_time, n_frames), item))
                continue
            if kind == 'mono_cc':
                _, cc, val = item
                self.mono.set_cc(cc, val)
                if cc == S._ENGINE_MASTER_CC:
                    self._mono_bus_dirty = True   # CC13 = mono bus master
                else:
                    self._mono_param_pending[cc] = val
                    self._mono_params_dirty = True

        buf = np.empty(n_frames, dtype=np.float32)
        bounds = sorted({0, n_frames} | {off for off, _ in note_evs})
        for bi in range(len(bounds) - 1):
            s, e = bounds[bi], bounds[bi + 1]
            for off, item in note_evs:
                if off == s:
                    self._apply_note_event(item)
            seg = e - s
            buf[s:e] = self.mono.render(seg)

        buf *= self.master_static
        return self.lim.process([buf])[0]


def _make_router(eng):
    """route(msg): the mono channel's notes/CC -> the mono engine; CH_CTRL mono
    level CC -> the bus master; the bind handshake keeps the mono channel in sync
    (mirrors the poly host router)."""
    mono_chs = {ch for ch, m in S._BINDINGS.items() if m == "mono"}
    state = {"armed": False}

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1

        if msg.type == "control_change" and msg.control in (120, 123):
            eng.all_sound_off()
            return

        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                state["armed"] = (S._MODULE_BY_ID.get(msg.value) == "mono")
                return
            if msg.control == S._BIND_CLEAR_CC:
                if S._MODULE_BY_ID.get(msg.value) == "mono":
                    mono_chs.clear()
                state["armed"] = False
                return
            if msg.control == S._VOL_MONO_CC:
                eng.mono.master = msg.value / 127.0
                eng._mono_bus_dirty = True
                return
            return

        if (state["armed"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            mono_chs.clear()
            mono_chs.add(ch)
            state["armed"] = False
            return

        if ch in mono_chs:
            if msg.type == "note_on" and msg.velocity > 0:
                eng.mono_note_on(msg.note, msg.velocity)
            elif msg.type == "note_off" or (
                    msg.type == "note_on" and msg.velocity == 0):
                eng.mono_note_off(msg.note)
            elif msg.type == "control_change":
                eng.mono_cc(msg.control, msg.value)

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
        for n in names:
            if "btn" not in n.lower() and "button" not in n.lower():
                return n
        return names[0] if names else None

    _last_err = 0.0
    while not stop_event.is_set():
        port_name = find_port()
        if not port_name:
            print("[mono-host] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[mono-host] MIDI in: {port_name}", flush=True)
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
                            print(f"[mono-host] MIDI route error "
                                  f"({getattr(msg, 'type', '?')}): {re}", flush=True)
        except Exception as e:
            print(f"[mono-host] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def persist_thread(eng, stop_event, poll_s=1.5):
    while not stop_event.wait(poll_s):
        if eng._mono_params_dirty:
            S._flush_mono_params(eng)
        if eng._mono_bus_dirty:
            _flush_mono_bus_level(eng)


def run_jack(client, eng, stop_event):
    out_L = client.outports.register("out_L")
    out_R = client.outports.register("out_R")
    eng._jack_ft = (lambda: int(client.frame_time))

    @client.set_process_callback
    def process(frames):
        buf = eng.render(frames, frame_time=int(client.last_frame_time))
        out_L.get_array()[:] = buf
        out_R.get_array()[:] = buf

    @client.set_shutdown_callback
    def shutdown(status, reason):
        print(f"[mono-host] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "mono",
                                 log=lambda m: print(f"[mono-host] {m}",
                                                     flush=True))
        print(f"[mono-host] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[mono-host] JACK client stopped", flush=True)


def main():
    # Open the JACK client FIRST so we can honor the server's actual sample rate
    # (a per-card jackd may run at 44100, e.g. the Mixtrack Pro 3) BEFORE building
    # the engine -- every engine reads S.SAMPLE_RATE at construction. Server chosen
    # by the JACK_SERVER env (unset = the default/main server).
    import jack
    _srv = os.environ.get("JACK_SERVER") or None
    try:
        client = jack.Client(JACK_NAME, servername=_srv)
    except Exception as _e:
        if _srv is None:
            raise
        # Assigned card's server is unreachable (e.g. a USB card unplugged at
        # boot). Fall back to the default server so the module PLAYS on the main
        # out instead of dying -- better than silence on this analog-summed rig.
        print(f"[mono-host] JACK server '{_srv}' unreachable ({_e}); "
              f"falling back to the default server", flush=True)
        client = jack.Client(JACK_NAME, servername=None)
    S.SAMPLE_RATE = client.samplerate
    print(f"[mono-host] JACK server '{_srv or 'default'}' sample rate "
          f"{client.samplerate} Hz", flush=True)
    eng = MonoHost()
    stop_event = threading.Event()

    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[mono-host] SIGTERM -- stopping", flush=True)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[mono-host] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread, args=(eng, stop_event),
                     daemon=True).start()
    threading.Thread(target=persist_thread, args=(eng, stop_event),
                     daemon=True).start()

    try:
        run_jack(client, eng, stop_event)
    except KeyboardInterrupt:
        print("\n[mono-host] Interrupted", flush=True)
    except Exception as e:
        print(f"[mono-host] Fatal: {e}", flush=True)
    finally:
        stop_event.set()
        try:
            if eng._mono_params_dirty:
                S._flush_mono_params(eng)
            if eng._mono_bus_dirty:
                _flush_mono_bus_level(eng)
        except Exception as e:
            print(f"[mono-host] final flush failed: {e}", flush=True)


if __name__ == "__main__":
    main()
