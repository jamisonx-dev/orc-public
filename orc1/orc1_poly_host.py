#!/usr/bin/env python3
"""
ORC-1 Poly host -- the polyphonic tone engine as its OWN JACK client process,
split out of orc1_synth (2026-09-02, "audio host split" step 3a).
============================================================================

WHY / HOW  (see orc1_drum_host.py / orc1_sampler_host.py for the pattern)
------------------------------------------------------------------------
Poly was co-rendered with chip inside one SynthEngine.render() loop. This host
lifts the poly half into its own process so it can sit on its own card/jackd
later, with nothing bridged. It REUSES the whole PolySynth engine (voices, ADSR,
filter, ring mod, octave, waveform) and the poly persistence/bus-level helpers by
importing orc1_synth (import has no side effects -- all its runtime work is under
`if __name__ == '__main__'`). What it re-expresses locally is only the RENDER
HARNESS (queue drain + sample-accurate segmented note-event loop), because that
lived inside SynthEngine.render() and served poly + chip at once -- each module
getting its own harness is exactly what "each module its own process" means.

  * MIDI: ALSA-seq fan-out -- opens the same ORC1-Synth-Bus the (retiring) synth
    reads; that bus is owned by the BRAIN (orc1.py), not the synth, so it survives
    the synth losing poly. The brain already forwards the poly channel, so NO
    brain change is needed. Handles the poly channel (notes + CC, incl. the CC13
    LEVEL) and the CH_CTRL poly-bus level CC, plus the bind handshake.
  * Audio: registers out_L/out_R, auto-connected to the physical outs so poly is
    audible by default. /patch enumerates the live graph so ORC1-Poly:out_L/R
    shows up automatically and can be rewired (later, to its own card).
  * Persistence: poly patch params (~/.orc1_poly_params.json) and the "poly" bus
    level in the SHARED ~/.orc1_bus_levels.json -- both REUSE the synth's flushers
    (S._flush_poly_params / S._flush_bus_levels), which merge against the raw file
    so the synth's/other hosts' keys are never clobbered.

GAIN STAGING (reproduce what the summed mix gave poly for free)
--------------------------------------------------------------
PolySynth.render() already applies the poly bus master (perceptual fader taper *
POLY_FADER_CEIL) internally. The old SynthEngine then multiplied by the OVERALL
master (bus-levels "master", boot MASTER_LEVEL) before the limiter. This host
applies that overall master as a STATIC gain so default loudness is unchanged --
exactly the choice the sampler host made. The overall master is a summed-mix
concept the independent module no longer belongs to, so it is not live here (the
CH_CTRL MASTER_CC fader no longer affects poly); the poly BUS level (CC8 / CC13)
still is. The old master limiter protected the SUMMED mix and is not reproduced
per-module (matches the drum/sampler hosts).

SAFETY: one JACK client, one ALSA-seq MIDI input, no ring reads. The MIDI thread
only enqueues; the JACK RT thread renders; persistence runs on a non-RT thread.
"""

import os
import sys
import time
import queue
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S    # PolySynth, config constants, persistence helpers

JACK_NAME = S._SYNTH_CFG.get("POLY_JACK_NAME", "ORC1-Poly")


def _static_master():
    """The overall master to bake in as a static gain: the saved bus-levels
    'master' if present, else the config MASTER_LEVEL -- so default loudness is
    identical to what the summed synth produced."""
    saved = S._load_bus_levels()
    if "master" in saved:
        return saved["master"] / 127.0
    return S.MASTER_LEVEL


class PolyHost:
    """The PolySynth engine with a queue-drained, sample-accurate render harness
    lifted from SynthEngine.render() (poly half only)."""

    def __init__(self):
        # Poly patch-persistence surface (matches S._flush_poly_params). MIDI
        # thread writes, persist thread drains. Must exist before MIDI starts.
        self._poly_param_pending = {}    # {cc: value}
        self._poly_params_dirty  = False

        self.poly = S.PolySynth()

        # -- Poly bus level restore (from the shared bus-levels file) ----------
        # PolySynth boots master=0.5; a saved "poly" level wins.
        _saved = S._load_bus_levels()
        if "poly" in _saved:
            self.poly.master = _saved["poly"] / 127.0
        # Bus-level persistence surface (matches S._flush_bus_levels /
        # S._bus_level_cc, which reads engine.poly.master for key "poly").
        self._bus_touched      = set()
        self._bus_levels_dirty = False

        # -- Poly patch restore (auto-remember-last, like drums/synth) ----------
        # set_cc is idempotent, so replaying saved CCs reproduces the patch.
        # CC13 (master) is skipped -- the bus-levels file owns every master.
        _pn = 0
        for _cc, _val in S._load_poly_params().items():
            try:
                _cc = int(_cc)
                if _cc in S._POLY_PERSIST_SKIP:
                    continue
                self.poly.set_cc(_cc, int(_val))
                _pn += 1
            except Exception:
                pass

        # Overall master baked in as a static gain (see module docstring).
        self.master_static = _static_master()
        print(f"[poly-host] poly bus level = "
              f"{max(0, min(127, int(round(self.poly.master*127))))}"
              f"  master(static) = {self.master_static:.2f}"
              f"  patch: {_pn} cc", flush=True)

        self.queue    = queue.Queue(maxsize=512)
        self._jack_ft = None   # set by run_jack(): callable -> current JACK frame time
        # Per-host brickwall limiter (audio host split removed the summed-mix one).
        self.lim = S.PeakLimiter()

    # -- MIDI-thread side (enqueue only; never touches DSP) --------------------
    def _enqueue(self, item):
        try:
            ft = self._jack_ft() if self._jack_ft is not None else None
        except Exception:
            ft = None
        # Release/panic events must never be dropped on overflow -- a lost
        # note-off strands the voice with no other release path (the 2026-07-30
        # stuck-note fix). render() drains every ~21ms block, so a bounded wait
        # is cheap and cannot deadlock against it.
        critical = item[0] in ('poly_off', 'all_off')
        try:
            if critical:
                self.queue.put((ft, item), timeout=0.5)
            else:
                self.queue.put_nowait((ft, item))
        except queue.Full:
            if critical:
                print(f"[poly-host] WARNING: dropped {item[0]!r} after queue "
                      f"overflow -- a voice may be stuck", flush=True)

    def poly_note_on(self, note, velocity):
        self._enqueue(('poly_on', note, velocity))

    def poly_note_off(self, note):
        self._enqueue(('poly_off', note))

    def poly_cc(self, cc, value):
        self._enqueue(('poly_cc', cc, value))

    def all_sound_off(self):
        """Panic: release every held poly voice. Rides the TIMED path so it can't
        overtake a note-on queued in the same block (2026-07-30)."""
        self._enqueue(('all_off',))

    def _ev_offset(self, ft, frame_time, n_frames):
        """Sample offset of an event inside this block (copy of
        SynthEngine._ev_offset -- pure arithmetic, no self state)."""
        if ft is None or frame_time is None:
            return 0
        base = (int(frame_time) - n_frames) & 0xFFFFFFFF
        off  = (int(ft) - base) & 0xFFFFFFFF
        if off < n_frames:
            return off
        return (n_frames - 1) if off < 0x80000000 else 0

    def _apply_note_event(self, item):
        """Apply one timed poly event at its segment boundary (note-ons AND
        note-offs ride the same path so the off can't overtake the on within a
        block -- the 2026-07-30 stuck-note fix)."""
        kind = item[0]
        if kind == 'poly_on':
            _, note, vel = item
            self.poly.note_on(note, vel)
        elif kind == 'poly_off':
            _, note = item
            self.poly.note_off(note)
        elif kind == 'all_off':
            try:
                self.poly.all_notes_off()
            except Exception as e:
                print(f"[poly-host] panic: {e}", flush=True)

    def render(self, n_frames, frame_time=None):
        """Drain the queue, then render the poly bus mono, sample-accurate.
        Returns a mono float32 buffer scaled by the static overall master
        (the poly bus master is already applied inside PolySynth.render)."""
        note_evs = []
        while not self.queue.empty():
            try:
                ft, item = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind in ('poly_on', 'poly_off', 'all_off'):
                note_evs.append((self._ev_offset(ft, frame_time, n_frames), item))
                continue
            if kind == 'poly_cc':
                _, cc, val = item
                self.poly.set_cc(cc, val)
                if cc == S._ENGINE_MASTER_CC:
                    # The /synth LEVEL slider + the Fire 'synvol' knob write the
                    # poly bus master here too, so mark it for persistence (else
                    # a later restart resurrects a stale, maybe louder value).
                    self._bus_touched.add("poly")
                    self._bus_levels_dirty = True
                else:
                    # Patch persistence (auto-remember-last).
                    self._poly_param_pending[cc] = val
                    self._poly_params_dirty = True

        buf = np.empty(n_frames, dtype=np.float32)
        bounds = sorted({0, n_frames} | {off for off, _ in note_evs})
        for bi in range(len(bounds) - 1):
            s, e = bounds[bi], bounds[bi + 1]
            for off, item in note_evs:
                if off == s:
                    self._apply_note_event(item)
            seg = e - s
            buf[s:e] = self.poly.render(seg)

        buf *= self.master_static
        return self.lim.process([buf])[0]


def _make_router(eng):
    """route(msg): the poly channel's notes/CC -> the poly engine; CH_CTRL poly
    level CC -> the bus master; the bind handshake keeps the poly channel in
    sync (mirrors orc1_synth._route_midi's poly branch + the drum host router)."""
    poly_chs = {ch for ch, m in S._BINDINGS.items() if m == "poly"} or {S.CH_POLY}
    state = {"armed": False}

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1

        # Panic (CC120 All Sound Off / CC123 All Notes Off) on any channel.
        if msg.type == "control_change" and msg.control in (120, 123):
            eng.all_sound_off()
            return

        # Bind ARM / CLEAR + poly bus level (control channel, brain-only).
        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                state["armed"] = (S._MODULE_BY_ID.get(msg.value) == "poly")
                return
            if msg.control == S._BIND_CLEAR_CC:
                if S._MODULE_BY_ID.get(msg.value) == "poly":
                    poly_chs.clear()
                state["armed"] = False
                return
            if msg.control == S._VOL_POLY_CC:
                eng.poly.master = msg.value / 127.0
                eng._bus_touched.add("poly")
                eng._bus_levels_dirty = True
                return
            # MASTER_CC / other control CCs: the overall master is static here
            # (a summed-mix concept), so ignore.
            return

        # Commit note: bind poly to this channel (one module per channel).
        if (state["armed"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            poly_chs.clear()
            poly_chs.add(ch)
            state["armed"] = False
            return

        if ch in poly_chs:
            if msg.type == "note_on" and msg.velocity > 0:
                eng.poly_note_on(msg.note, msg.velocity)
            elif msg.type == "note_off" or (
                    msg.type == "note_on" and msg.velocity == 0):
                eng.poly_note_off(msg.note)
            elif msg.type == "control_change":
                eng.poly_cc(msg.control, msg.value)

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
            print("[poly-host] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[poly-host] MIDI in: {port_name}", flush=True)
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
                            print(f"[poly-host] MIDI route error "
                                  f"({getattr(msg, 'type', '?')}): {re}", flush=True)
        except Exception as e:
            print(f"[poly-host] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def persist_thread(eng, stop_event, poll_s=1.5):
    """Non-RT: flush pending poly params and the poly bus level (REUSING the
    synth's flushers -- both merge against the raw file so no other owner's
    keys are clobbered)."""
    while not stop_event.wait(poll_s):
        if eng._bus_levels_dirty:
            S._flush_bus_levels(eng)
        if eng._poly_params_dirty:
            S._flush_poly_params(eng)


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
        print(f"[poly-host] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "poly",
                                 log=lambda m: print(f"[poly-host] {m}",
                                                     flush=True))
        print(f"[poly-host] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[poly-host] JACK client stopped", flush=True)


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
        print(f"[poly-host] JACK server '{_srv}' unreachable ({_e}); "
              f"falling back to the default server", flush=True)
        client = jack.Client(JACK_NAME, servername=None)
    S.SAMPLE_RATE = client.samplerate
    print(f"[poly-host] JACK server '{_srv or 'default'}' sample rate "
          f"{client.samplerate} Hz", flush=True)
    eng = PolyHost()
    stop_event = threading.Event()

    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[poly-host] SIGTERM -- stopping", flush=True)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[poly-host] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread, args=(eng, stop_event),
                     daemon=True).start()
    threading.Thread(target=persist_thread, args=(eng, stop_event),
                     daemon=True).start()

    try:
        run_jack(client, eng, stop_event)
    except KeyboardInterrupt:
        print("\n[poly-host] Interrupted", flush=True)
    except Exception as e:
        print(f"[poly-host] Fatal: {e}", flush=True)
    finally:
        stop_event.set()
        # Final flush so a level/param moved in the last poll window survives.
        try:
            if eng._bus_levels_dirty:
                S._flush_bus_levels(eng)
            if eng._poly_params_dirty:
                S._flush_poly_params(eng)
        except Exception as e:
            print(f"[poly-host] final flush failed: {e}", flush=True)


if __name__ == "__main__":
    main()
