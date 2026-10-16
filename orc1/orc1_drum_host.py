#!/usr/bin/env python3
"""
ORC-1 Drum host -- the 909 drum voices as their OWN JACK client process, split
out of orc1_synth (2026-09-02, "audio host split" step 2).
============================================================================

WHY / HOW  (see orc1_sampler_host.py for the pattern this follows)
-----------------------------------------------------------------
The drums were co-rendered with poly + chip inside one SynthEngine.render()
loop. This host lifts the drum half into its own process so it can sit on its
own card/jackd later, with nothing bridged. It REUSES every DrumVoice class, the
GM note map, the drum CC decode, the choke groups and the param-persistence
helpers by importing orc1_synth (import has no side effects). What it re-expresses
locally is only the RENDER HARNESS (queue drain + sample-accurate segmented loop +
choke), because that lived inside SynthEngine.render() and served all three
modules at once -- each module getting its own harness is exactly what "each
module its own process" means.

  * MIDI: ALSA-seq fan-out -- opens the same ORC1-Synth-Bus the synth reads; the
    brain already forwards CH_DRUM, so NO brain change is needed. Handles CH_DRUM
    (drum triggers on GM notes + the packed drum CC map, which now includes the
    one-shot smpl_* slots at CC 79+) and the CH_CTRL drum-bus level CC.
  * Audio: registers out_L/out_R, auto-connected to the physical outs so drums
    are audible by default. /patch shows ORC1-Drum:out_L/R and can rewire it
    (e.g. back to the outs 3/4 the drum bus used to be patched to).
  * Persistence: per-voice drum params (~/.orc1_drum_params.json, reusing the
    synth's flusher) and the "drum" bus level in the SHARED ~/.orc1_bus_levels.json
    -- written with a read-modify-write MERGE so the synth's master/poly/chip
    keys in the same file are never clobbered (and vice-versa).

KNOWN LIMITS (step 2; solidify later)
-------------------------------------
  * The drum channel is resolved from ~/.orc1_bindings.json at startup and then
    followed live via the bind handshake (same as the sampler host).
  * The synth's overall master (CH_CTRL CC7) no longer scales drums -- they are
    their own module. Drums keep their own bus level (CH_CTRL CC10, persisted).

SAFETY: one JACK client, one ALSA-seq MIDI input, read-only ring? no -- drums
read no ring. MIDI thread only enqueues; the JACK RT thread renders. Persistence
runs on a non-RT thread.
"""

import os
import sys
import glob
import time
import queue
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S    # DrumVoice classes, maps, choke, persistence helpers

JACK_NAME = S._SYNTH_CFG.get("DRUM_JACK_NAME", "ORC1-Drum")


def _load_drum_bus_level():
    """Read the 'drum' key from the shared bus-levels file (0-127), or None."""
    try:
        import json
        with open(S.BUS_LEVELS_FILE) as f:
            data = json.load(f)
        v = data.get("drum") if isinstance(data, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return int(v) if 0 <= v <= 127 else None
    except Exception:
        return None


def _flush_drum_bus_level(engine):
    """Persist ONLY the 'drum' key via the shared, flock-protected writer. The
    bus-levels file is multi-writer since the audio host split (poly/chip/drum/
    mono), so the read-modify-write merge + cross-process lock live in ONE place
    (S._bus_level_write), not a copy per host."""
    engine._drum_bus_dirty = False
    S._bus_level_write(
        {"drum": max(0, min(127, int(round(engine.drum_master * 127.0))))})


class DrumEngine:
    """The 909 voices + one-shot smpl_* slots, with a queue-drained,
    sample-accurate render harness lifted from SynthEngine.render()."""

    def __init__(self):
        self.voices = {
            "bass_drum":  S.BassDrum(),
            "snare":      S.Snare(),
            "low_tom":    S.Tom('low'),
            "mid_tom":    S.Tom('mid'),
            "hi_tom":     S.Tom('hi'),
            "rimshot":    S.Rimshot(),
            "clap":       S.Clap(),
            "closed_hat": S.HiHat('closed'),
            "open_hat":   S.HiHat('open'),
            "cymbal":     S.Cymbal(),
            "ride":       S.Ride(),        # 2026-09-23: the 909's own ride
            "ride_bell":  S.RideBell(),
            "splash":     S.Splash(),
            "himid_tom":  S.Tom('himid'),
            "cowbell":    S.Cowbell(),
        }
        self._load_samples()   # smpl_0..N one-shot slots (drum voices on CH_DRUM)

        # Param persistence surface (matches S._flush_drum_params /
        # S._drum_param_persist_thread expectations).
        self._drum_param_pending = {}
        self._drum_params_dirty  = False
        for _name, _params in S._load_drum_params().items():
            _v = self.voices.get(_name)
            if not _v:
                continue
            for _p, _cc in _params.items():
                try:
                    _v.set_param(_p, int(_cc))
                except Exception:
                    pass

        self.queue    = queue.Queue(maxsize=512)
        self._jack_ft = None   # set by run_jack(): callable -> current JACK frame time
        # Per-host brickwall limiter (audio host split removed the summed-mix one).
        self.lim = S.PeakLimiter()

        # Drum-bus master (CH_CTRL drum CC), restored from the shared file.
        self.drum_master     = S._DRUM_BOOT_CC / 127.0
        self._drum_bus_dirty = False
        _saved = _load_drum_bus_level()
        if _saved is not None:
            self.drum_master = _saved / 127.0
        print(f"[drum-host] drum bus level = {int(round(self.drum_master*127))}"
              + ("" if _saved is None else " (restored)"), flush=True)

    def _load_samples(self):
        """smpl_0..N one-shot slots (mirrors SynthEngine._load_samples)."""
        os.makedirs(S.SAMPLES_DIR, exist_ok=True)
        _all = sorted(glob.glob(os.path.join(S.SAMPLES_DIR, "*.wav")))
        # The kit's sampled voices (crash, ride, bell, splash) are not slots.
        wavs = [w for w in _all
                if os.path.basename(w).lower() not in S.KIT_SAMPLE_FILES
                ][:S.N_SAMPLE_SLOTS]
        for i in range(S.N_SAMPLE_SLOTS):
            path = wavs[i] if i < len(wavs) else None
            self.voices["smpl_%d" % i] = S.SampleVoice(path)

    # -- MIDI-thread side (enqueue only; never touches DSP) --------------------
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
            # Drum hits + CCs are non-critical (one-shots with their own decay;
            # a dropped CC just misses one update). No stuck-note risk.
            pass

    def drum_trigger(self, voice_name, velocity):
        self._enqueue(('drum', voice_name, velocity))

    def drum_cc(self, voice_name, param, cc_value):
        self._enqueue(('drum_cc', voice_name, param, cc_value))

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

    def render(self, n_frames, frame_time=None):
        """Drain the queue, then render the drum bus mono, sample-accurate.
        Returns a mono float32 buffer already scaled by the drum-bus master."""
        note_evs = []
        while not self.queue.empty():
            try:
                ft, item = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == 'drum':
                note_evs.append((self._ev_offset(ft, frame_time, n_frames), item))
            elif kind == 'drum_cc':
                _, name, param, val = item
                v = self.voices.get(name)
                if v:
                    v.set_param(param, val)
                    self._drum_param_pending.setdefault(name, {})[param] = val
                    self._drum_params_dirty = True

        buf = np.zeros(n_frames, dtype=np.float32)
        bounds = sorted({0, n_frames} | {off for off, _ in note_evs})
        for bi in range(len(bounds) - 1):
            s, e = bounds[bi], bounds[bi + 1]
            for off, item in note_evs:
                if off == s:
                    _, name, vel = item
                    v = self.voices.get(name)
                    if v:
                        v.trigger(vel)
                        grp = S._CHOKE.get(name)
                        if grp:
                            for other in grp:
                                if other != name and other in self.voices:
                                    self.voices[other].choke()
            seg = e - s
            for v in self.voices.values():
                buf[s:e] += v.render(seg)

        # Drum-bus master * ceiling (same shaping the synth applied).
        buf *= self.drum_master * S.DRUM_FADER_CEIL
        return self.lim.process([buf])[0]


def _make_router(eng):
    """route(msg): CH_DRUM triggers/CCs -> the drum engine; CH_CTRL drum level
    CC -> the bus master; the bind handshake keeps the drum channel(s) in sync."""
    drum_chs = {ch for ch, m in S._BINDINGS.items() if m == "drums"} or {S.CH_DRUM}
    state = {"armed": False}

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1

        # Panic (CC120/123): drums are one-shots with their own decay, left
        # alone -- exactly as the synth did.
        if msg.type == "control_change" and msg.control in (120, 123):
            return

        # Bind ARM / CLEAR (control channel, brain-only).
        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                state["armed"] = (S._MODULE_BY_ID.get(msg.value) == "drums")
                return
            if msg.control == S._BIND_CLEAR_CC:
                if S._MODULE_BY_ID.get(msg.value) == "drums":
                    drum_chs.clear()
                state["armed"] = False
                return
            # Drum-bus level (CH_CTRL drum CC).
            if msg.control == S._VOL_DRUM_CC:
                eng.drum_master = msg.value / 127.0
                eng._drum_bus_dirty = True
                return

        # Commit note: bind drums to this channel (one module per channel).
        if (state["armed"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            drum_chs.clear()
            drum_chs.add(ch)
            state["armed"] = False
            return

        if ch in drum_chs:
            if msg.type == "note_on" and msg.velocity > 0:
                voice = S.DRUM_NOTE_MAP.get(msg.note)
                if voice:
                    eng.drum_trigger(voice, msg.velocity)
            elif msg.type == "control_change":
                S._route_drum_cc(eng, msg.control, msg.value)

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
            print("[drum-host] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[drum-host] MIDI in: {port_name}", flush=True)
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
                            print(f"[drum-host] MIDI route error "
                                  f"({getattr(msg, 'type', '?')}): {re}", flush=True)
        except Exception as e:
            print(f"[drum-host] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def persist_thread(eng, stop_event, poll_s=1.5):
    """Non-RT: flush pending drum-voice params and the drum-bus level."""
    while not stop_event.wait(poll_s):
        if eng._drum_params_dirty:
            S._flush_drum_params(eng)
        if eng._drum_bus_dirty:
            _flush_drum_bus_level(eng)


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
        print(f"[drum-host] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "drum",
                                 log=lambda m: print(f"[drum-host] {m}",
                                                     flush=True))
        print(f"[drum-host] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[drum-host] JACK client stopped", flush=True)


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
        print(f"[drum-host] JACK server '{_srv}' unreachable ({_e}); "
              f"falling back to the default server", flush=True)
        client = jack.Client(JACK_NAME, servername=None)
    S.SAMPLE_RATE = client.samplerate
    print(f"[drum-host] JACK server '{_srv or 'default'}' sample rate "
          f"{client.samplerate} Hz", flush=True)
    eng = DrumEngine()
    stop_event = threading.Event()

    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[drum-host] SIGTERM -- stopping", flush=True)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[drum-host] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread, args=(eng, stop_event),
                     daemon=True).start()
    threading.Thread(target=persist_thread, args=(eng, stop_event),
                     daemon=True).start()

    try:
        run_jack(client, eng, stop_event)
    except KeyboardInterrupt:
        print("\n[drum-host] Interrupted", flush=True)
    except Exception as e:
        print(f"[drum-host] Fatal: {e}", flush=True)
    finally:
        stop_event.set()
        # Final flush so a level/param moved in the last poll window survives.
        try:
            if eng._drum_params_dirty:
                S._flush_drum_params(eng)
            if eng._drum_bus_dirty:
                _flush_drum_bus_level(eng)
        except Exception as e:
            print(f"[drum-host] final flush failed: {e}", flush=True)


if __name__ == "__main__":
    main()
