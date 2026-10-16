#!/usr/bin/env python3
"""
ORC-1 Chip host -- the NES/Game Boy chip engine as its OWN JACK client process,
split out of orc1_synth (2026-09-02, "audio host split" step 3b, the last engine).
============================================================================

WHY / HOW  (see orc1_poly_host.py / orc1_drum_host.py for the pattern)
---------------------------------------------------------------------
Chip was the last engine co-rendered with poly inside SynthEngine.render(). This
host lifts it into its own process so it can sit on its own card/jackd later, with
nothing bridged. It REUSES the whole NESEngine (four APU voices + PCM + GB wave,
P1/P2 coupling + spread, per-voice pitch bend / vibrato / sweep, ring mod, filter)
and the chip persistence/bus-level helpers by importing orc1_synth (import has no
side effects). What it re-expresses locally is only the RENDER HARNESS (queue drain
+ sample-accurate segmented note-event loop), because that lived inside
SynthEngine.render() and served poly + chip at once.

  * MIDI: ALSA-seq fan-out -- opens the same ORC1-Synth-Bus (owned by the BRAIN, so
    it survives the synth retiring), and the brain already forwards the chip
    channels, so NO brain change is needed. Handles every chip-family channel
    (chip_p1/p2/tri/noise/pcm + gb_wave -> the NESEngine voice via cfg.MODULE_VOICE:
    notes, CC, and pitchwheel) plus the CH_CTRL chip-bus level CC and the bind
    handshake.
  * Audio: registers out_L/out_R. Chip is rendered mono then run through
    NESEngine.get_p1p2_stereo(), which returns a genuine L/R pair when P1/P2 spread
    is on and (L==R) mono otherwise -- so the dedicated chip out carries the spread
    directly (the old synth needed a separate stereo_mode flag; here it's inherent,
    and default-off behaviour is byte-identical mono). Auto-connected to the outs.
  * Persistence: chip patch params (~/.orc1_chip_params.json, nested by voice) and
    the "chip" key of the SHARED ~/.orc1_bus_levels.json -- both REUSE the synth's
    flushers (S._flush_chip_params / S._flush_bus_levels), which merge against the
    raw file so no other owner's keys are clobbered. The P1/P2 coupling CCs
    (72/73/76) are EXCLUDED from persistence by S._CHIP_PERSIST_SKIP -- the join is
    ephemeral by design (session 15).

GAIN STAGING: NESEngine.render() already applies the chip bus master (perceptual
taper * CHIP_FADER_CEIL) and its own filter internally. The old SynthEngine then
multiplied by the OVERALL master before the limiter; this host bakes that overall
master in as a STATIC gain (saved bus-levels "master", else MASTER_LEVEL) so default
loudness is unchanged -- the sampler/poly-host choice. The MASTER_CC fader no longer
affects chip; the chip BUS level (CC9 / CC13) still does.

SAFETY: one JACK client, one ALSA-seq MIDI input, no ring reads. MIDI thread
enqueues only; the JACK RT thread renders; persistence runs on a non-RT thread.
"""

import os
import sys
import time
import queue
import threading
import json

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S    # NESEngine, config constants, persistence helpers

JACK_NAME = S._SYNTH_CFG.get("CHIP_JACK_NAME", "ORC1-Chip")


def _static_master():
    saved = S._load_bus_levels()
    if "master" in saved:
        return saved["master"] / 127.0
    return S.MASTER_LEVEL


class ChipHost:
    """The NESEngine with a queue-drained, sample-accurate render harness lifted
    from SynthEngine.render() (chip half only)."""

    def __init__(self):
        # Chip patch-persistence surface (matches S._flush_chip_params, nested by
        # voice). MIDI thread writes, persist thread drains. Must exist before MIDI.
        self._chip_param_pending = {}    # {voice: {cc: value}}
        self._chip_params_dirty  = False

        _chip_pcm = os.path.expanduser(
            S._SYNTH_CFG.get("CHIP_PCM_DIR", os.path.join(S.SAMPLES_DIR, "chip")))
        self.chip = S.NESEngine(sample_dir=_chip_pcm)

        # -- Chip bus level restore (from the shared bus-levels file) -----------
        _saved = S._load_bus_levels()
        if "chip" in _saved:
            self.chip.master = _saved["chip"] / 127.0
        self._bus_touched      = set()
        self._bus_levels_dirty = False

        # -- Chip patch restore (auto-remember-last) ----------------------------
        # set_cc is idempotent per voice. CC13 (master) + coupling 72/73/76 are
        # skipped (bus-owned / ephemeral join) -- see S._CHIP_PERSIST_SKIP.
        _cn = 0
        for _voice, _ccs in S._load_chip_params().items():
            if not isinstance(_ccs, dict):
                continue
            for _cc, _val in _ccs.items():
                try:
                    _cc = int(_cc)
                    if _cc in S._CHIP_PERSIST_SKIP:
                        continue
                    self.chip.set_cc(_voice, _cc, int(_val))
                    _cn += 1
                except Exception:
                    pass

        # -- GB wave table state (2026-09-24): slots / custom table / harmonic
        # knob positions, in their own file (see S._GB_HARM_CC0 for why).
        # Applied AFTER the CC replay above so it wins over a replayed CC1.
        self._wave_file = os.path.expanduser(S.GB_WAVE_STATE_FILE)
        try:
            with open(self._wave_file) as f:
                self.chip._wave.import_state(json.load(f))
            print("[chip-host] gb wave state restored", flush=True)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[chip-host] gb wave state not restored: {e}", flush=True)
        self.chip._wave.state_dirty = False

        self.master_static = _static_master()
        print(f"[chip-host] chip bus level = "
              f"{max(0, min(127, int(round(self.chip.master*127))))}"
              f"  master(static) = {self.master_static:.2f}"
              f"  patch: {_cn} cc", flush=True)

        self.queue    = queue.Queue(maxsize=512)
        self._jack_ft = None
        # Per-host brickwall limiter (audio host split removed the summed-mix one).
        self.lim = S.PeakLimiter()

    # -- MIDI-thread side (enqueue only) ---------------------------------------
    def _enqueue(self, item):
        try:
            ft = self._jack_ft() if self._jack_ft is not None else None
        except Exception:
            ft = None
        critical = item[0] in ('chip_off', 'all_off')
        try:
            if critical:
                self.queue.put((ft, item), timeout=0.5)
            else:
                self.queue.put_nowait((ft, item))
        except queue.Full:
            if critical:
                print(f"[chip-host] WARNING: dropped {item[0]!r} after queue "
                      f"overflow -- a voice may be stuck", flush=True)

    def chip_note_on(self, voice, note, velocity):
        self._enqueue(('chip_on', voice, note, velocity))

    def chip_note_off(self, note, voice=None):
        self._enqueue(('chip_off', voice, note))

    def chip_cc(self, voice, cc, value):
        self._enqueue(('chip_cc', voice, cc, value))

    def chip_pitch(self, voice, pitch):
        self._enqueue(('chip_pitch', voice, pitch))

    def chip_pc(self, voice, program):
        self._enqueue(('chip_pc', voice, program))

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
        if kind == 'chip_on':
            _, voice, note, vel = item
            self.chip.note_on(voice, note, vel)
        elif kind == 'chip_off':
            _, voice, note = item
            self.chip.note_off(note, voice)
        elif kind == 'all_off':
            try:
                self.chip.all_notes_off()
            except Exception as e:
                print(f"[chip-host] panic: {e}", flush=True)

    def render(self, n_frames, frame_time=None):
        """Drain the queue, then render the chip bus, sample-accurate, to a
        stereo L/R pair (P1/P2 spread carried through get_p1p2_stereo), scaled by
        the static overall master (chip bus master is applied inside render)."""
        note_evs = []
        while not self.queue.empty():
            try:
                ft, item = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind in ('chip_on', 'chip_off', 'all_off'):
                note_evs.append((self._ev_offset(ft, frame_time, n_frames), item))
                continue
            if kind == 'chip_pitch':
                _, voice, pitch = item
                if hasattr(self.chip, 'set_pitchwheel'):
                    self.chip.set_pitchwheel(voice, pitch)
            elif kind == 'chip_pc':
                _, voice, prog = item
                self.chip.program_change(voice, prog)
            elif kind == 'chip_cc':
                _, voice, cc, val = item
                self.chip.set_cc(voice, cc, val)
                if cc == S._ENGINE_MASTER_CC:
                    # /chip LEVEL sliders + the Fire 'chipvol' knob write the chip
                    # bus master here too (NESEngine scales by 0.7 on this path, so
                    # persistence stores the resulting GAIN); mark it dirty.
                    self._bus_touched.add("chip")
                    self._bus_levels_dirty = True
                elif cc not in S._CHIP_PERSIST_SKIP:
                    # Patch persistence; coupling 72/73/76 stay out (ephemeral join).
                    self._chip_param_pending.setdefault(voice, {})[cc] = val
                    self._chip_params_dirty = True
                # (The old synth auto-enabled stereo_mode when CC76 spread turned
                #  on; here get_p1p2_stereo carries the spread directly, so there is
                #  no mode flag to flip.)

        L = np.empty(n_frames, dtype=np.float32)
        R = np.empty(n_frames, dtype=np.float32)
        bounds = sorted({0, n_frames} | {off for off, _ in note_evs})
        for bi in range(len(bounds) - 1):
            s, e = bounds[bi], bounds[bi + 1]
            for off, item in note_evs:
                if off == s:
                    self._apply_note_event(item)
            seg = e - s
            # render_stereo = the old render()+get_p1p2_stereo() exactly while
            # every voice is centred; per-voice pan (CC 86) since 2026-09-24.
            _cl, _cr = self.chip.render_stereo(seg)
            L[s:e] = _cl
            R[s:e] = _cr

        L *= self.master_static
        R *= self.master_static
        # Stereo-linked limiter (one gain from the louder side keeps the image).
        L, R = self.lim.process([L, R])
        return L, R


def _make_router(eng):
    """route(msg): every chip-family channel's notes/CC/pitch -> the chip engine
    (via cfg.MODULE_VOICE); CH_CTRL chip level CC -> the bus master; the bind
    handshake keeps the chip channel->module map in sync (mirrors
    orc1_synth._route_midi's chip branch + the drum/poly host routers)."""
    # ch -> chip module key (chip_p1/.../gb_wave); voice resolved at dispatch.
    chip_map = {ch: m for ch, m in S._BINDINGS.items() if m in S._CHIP_MODULES}
    state = {"armed_mod": None}   # a chip module key pending a commit note

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1

        # Panic on any channel.
        if msg.type == "control_change" and msg.control in (120, 123):
            eng.all_sound_off()
            return

        # Bind ARM / CLEAR + chip bus level (control channel, brain-only).
        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                m = S._MODULE_BY_ID.get(msg.value)
                state["armed_mod"] = m if m in S._CHIP_MODULES else None
                return
            if msg.control == S._BIND_CLEAR_CC:
                m = S._MODULE_BY_ID.get(msg.value)
                if m in S._CHIP_MODULES:
                    for c in [c for c, mm in chip_map.items() if mm == m]:
                        del chip_map[c]
                state["armed_mod"] = None
                return
            if msg.control == S._VOL_CHIP_CC:
                eng.chip.master = msg.value / 127.0
                eng._bus_touched.add("chip")
                eng._bus_levels_dirty = True
                return
            return

        # Commit note: bind the armed chip module to this channel (one module per
        # channel; also drop that module's old channel).
        if (state["armed_mod"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            m = state["armed_mod"]
            for c in [c for c, mm in chip_map.items() if mm == m or c == ch]:
                del chip_map[c]
            chip_map[ch] = m
            state["armed_mod"] = None
            return

        m = chip_map.get(ch)
        if m is None:
            return
        voice = S._MODULE_VOICE.get(m)
        if voice is None:
            return
        if msg.type == "note_on" and msg.velocity > 0:
            eng.chip_note_on(voice, msg.note, msg.velocity)
        elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0):
            eng.chip_note_off(msg.note, voice)
        elif msg.type == "control_change":
            eng.chip_cc(voice, msg.control, msg.value)
        elif msg.type == "pitchwheel":
            eng.chip_pitch(voice, msg.pitch)
        elif msg.type == "program_change":
            # GB wave slot recall / store (2026-09-24); other voices ignore it.
            eng.chip_pc(voice, msg.program)

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
            print("[chip-host] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[chip-host] MIDI in: {port_name}", flush=True)
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
                            print(f"[chip-host] MIDI route error "
                                  f"({getattr(msg, 'type', '?')}): {re}", flush=True)
        except Exception as e:
            print(f"[chip-host] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def flush_wave_state(eng):
    """Write the GB wave slots/custom table/knobs (atomic tmp+rename). Only the
    chip host writes this file, so no merge is needed."""
    w = eng.chip._wave
    if not w.state_dirty:
        return
    w.state_dirty = False           # cleared first: an edit during the write re-dirties
    try:
        data = w.export_state()
        tmp = eng._wave_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, eng._wave_file)
    except Exception as e:
        w.state_dirty = True
        print(f"[chip-host] gb wave state save failed: {e}", flush=True)


def persist_thread(eng, stop_event, poll_s=1.5):
    """Non-RT: flush pending chip params and the chip bus level (REUSING the
    synth's flushers -- both merge against the raw file), plus the GB wave
    table state."""
    while not stop_event.wait(poll_s):
        if eng._bus_levels_dirty:
            S._flush_bus_levels(eng)
        if eng._chip_params_dirty:
            S._flush_chip_params(eng)
        flush_wave_state(eng)


def run_jack(client, eng, stop_event):
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
        print(f"[chip-host] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "chip",
                                 log=lambda m: print(f"[chip-host] {m}",
                                                     flush=True))
        print(f"[chip-host] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[chip-host] JACK client stopped", flush=True)


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
        print(f"[chip-host] JACK server '{_srv}' unreachable ({_e}); "
              f"falling back to the default server", flush=True)
        client = jack.Client(JACK_NAME, servername=None)
    S.SAMPLE_RATE = client.samplerate
    print(f"[chip-host] JACK server '{_srv or 'default'}' sample rate "
          f"{client.samplerate} Hz", flush=True)
    eng = ChipHost()
    stop_event = threading.Event()

    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[chip-host] SIGTERM -- stopping", flush=True)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[chip-host] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread, args=(eng, stop_event),
                     daemon=True).start()
    threading.Thread(target=persist_thread, args=(eng, stop_event),
                     daemon=True).start()

    try:
        run_jack(client, eng, stop_event)
    except KeyboardInterrupt:
        print("\n[chip-host] Interrupted", flush=True)
    except Exception as e:
        print(f"[chip-host] Fatal: {e}", flush=True)
    finally:
        stop_event.set()
        try:
            if eng._bus_levels_dirty:
                S._flush_bus_levels(eng)
            if eng._chip_params_dirty:
                S._flush_chip_params(eng)
            flush_wave_state(eng)
        except Exception as e:
            print(f"[chip-host] final flush failed: {e}", flush=True)


if __name__ == "__main__":
    main()
