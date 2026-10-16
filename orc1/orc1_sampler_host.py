#!/usr/bin/env python3
"""
ORC-1 Sampler host -- the live-remix sample decks as their OWN JACK client
process, split out of orc1_synth (2026-09-02, "audio host split" step 1).
============================================================================

WHY THIS EXISTS
---------------
orc1_synth.py was a monolith: 909 drums + poly + the whole chip family + the
sampler all rendered in ONE JACK client on ONE card. The sampler does not share
any rendering with the tone engines -- it just reads the skipback ring and gets
summed in -- so it is the cleanest module to peel off first. Pulling it into its
own process is the first concrete step toward "each module on its own audio
host": this host can stay on the shared jackd for now, or be pointed at its own
card later by nothing more than a JACK server name.

HOW IT STAYS BEHAVIOUR-IDENTICAL
--------------------------------
  * MIDI is ALSA-seq, which fans a source out to many readers for free. This
    host opens the SAME `ORC1-Synth-Bus` input the synth reads (matched by
    SYNTH["MIDI_PORT_PREFIX"]). The brain already forwards CH_SAMPLER to that
    bus, so NO brain change is needed. The main synth still reads the bus too --
    it now handles ONLY the one-shot `smpl_*` slots on CH_SAMPLER (those are
    drum voices on the drum bus; see orc1_synth._route_sampler_os). This host
    handles the deck controls (orc1_synth._route_sampler).
  * All the sampler DSP, the RingBufferReader, and every _SMP_* CC/note table
    are REUSED from orc1_synth by importing it -- importing has no side effects
    (its work is under `if __name__ == '__main__'`). One source of truth.
  * The host registers out_L/out_R and auto-connects them to the physical
    outputs, so the sampler is audible by default exactly as when it rode the
    synth's main mix. /patch (which enumerates the live JACK graph) shows
    ORC1-Sampler:out_L/R automatically and can rewire it anywhere.

KNOWN LIMITS (step 1; solidify later)
-------------------------------------
  * The sampler channel is resolved from ~/.orc1_bindings.json at STARTUP. If
    you rebind the sampler to a different channel live (module matrix), restart
    orc1-sampler. Live rebind-follow is deliberately out of scope for step 1.
  * The synth's overall master (CH_CTRL CC7) no longer scales the sampler -- it
    is its own module now. A fixed boot master (SYNTH.MASTER_LEVEL) is applied
    so default loudness matches; balance further with the sampler's own gain
    (CC35) or /patch.
  * orc1_health / _test_fire still expect ORC1-Synth:samp_L/R; those move to
    ORC1-Sampler:out_L/R and will be updated when the split solidifies.

SAFETY: opens ONE JACK client and ONE ALSA-seq MIDI input; reads the skipback
ring read-only (same as the synth did). Renders on the JACK RT thread; MIDI
thread only calls into orc1_sampler (plain attribute writes), never DSP.
"""

import os
import sys
import time
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S            # reuse constants, _route_sampler, _SMP_* tables
import orc1_sampler               # the deck DSP + ring reader

JACK_NAME = S._SAMPLER_CFG.get("SAMPLER_JACK_NAME", "ORC1-Sampler")
# The sampler used to ride the synth's summed mix, which applied the overall
# master before the limiter. Keep the boot master here so default loudness is
# unchanged; live master (CH_CTRL CC7) is a synth-mix concept that no longer
# owns this independent module.
BOOT_MASTER = float(S.MASTER_LEVEL)


class SamplerHost:
    """Minimal engine shim -- just the surface orc1_synth._route_sampler needs
    (an object with `.sampler`). The one-shot smpl_* slots (which _route_sampler
    would have driven via drum_trigger/drum_cc) stay in the main synth, so this
    shim needs no drum surface at all."""

    def __init__(self):
        self.sampler = None
        # Per-host brickwall limiter (audio host split removed the summed-mix one).
        self.lim = S.PeakLimiter()
        try:
            _ring = orc1_sampler.RingBufferReader(
                S._SAMPLER_CFG.get("RING_PATH", "/home/pibox/.skipbuf/ring.raw"),
                S._SAMPLER_CFG.get("RING_POS_PATH", "/home/pibox/.skipbuf/ring.pos"),
                buffer_secs=S._SAMPLER_CFG.get("RING_SECONDS", 1860),
                rate=S._SAMPLER_CFG.get("RING_RATE", 48000),
                channels=S._SAMPLER_CFG.get("RING_CHANNELS", 2),
                tracks_path=S._SAMPLER_CFG.get("RING_TRACKS_PATH",
                                               "~/.orc1_skipback_tracks.json"))
            # Sampler internals run at the RING rate (its capture/nudge/phase math
            # is pinned to it); the JACK server rate (S.SAMPLE_RATE, read from the
            # client in main) is passed as out_rate so each deck resamples its ring
            # buffer to the output on read. Equal on the 48k main card => no-op.
            self.sampler = orc1_sampler.SamplerManager(
                _ring, _ring.rate, cfg=S._SAMPLER_CFG, out_rate=S.SAMPLE_RATE)
            print(f"[sampler-host] Sampler ready (ring tap read-only)", flush=True)
        except Exception as _e:
            self.sampler = None
            print(f"[sampler-host] ! Sampler unavailable: {_e}", flush=True)

    def render(self, n_frames):
        """Return (L, R) float32 arrays. The sampler renders mono today, so both
        channels get the same buffer (identical to how it sat in the old mix)."""
        if self.sampler is None:
            z = np.zeros(n_frames, dtype=np.float32)
            return z, z
        s = self.sampler.render(n_frames)
        if s is None:
            s = np.zeros(n_frames, dtype=np.float32)
        s = (s * BOOT_MASTER).astype(np.float32)
        s = self.lim.process([s])[0]
        return s, s

    def panic(self):
        """CC120/123 on any channel: stop both decks so a stuck gate/loop can be
        silenced without restarting the service (mirrors the synth's panic)."""
        smp = self.sampler
        if smp is None:
            return
        for deck in ("A", "B"):
            try:
                smp.control(deck, "gate", False)
            except Exception:
                pass
            try:
                smp.control(deck, "stop")
            except Exception:
                pass


def _sampler_channels():
    """Channels currently bound to the 'sampler' module, read from the live
    bindings snapshot orc1_synth loaded at import. Falls back to CH_SAMPLER."""
    chs = {ch for ch, m in S._BINDINGS.items() if m == "sampler"}
    return chs or {S.CH_SAMPLER}


def _make_router(host):
    """Return route(msg): dispatches CH_SAMPLER deck messages to the sampler.

    The sampler channel set is captured at startup and updated live from the
    SAME bind arm/commit/clear handshake the synth honours, so a rebind done
    while the host is running is followed without a restart (best-effort)."""
    samp_chs = _sampler_channels()
    state = {"armed": False}   # True while 'sampler' is armed for a commit note

    def route(msg):
        if not hasattr(msg, "channel"):
            return
        ch = msg.channel + 1   # mido is 0-indexed

        # Panic on any channel.
        if msg.type == "control_change" and msg.control in (120, 123):
            host.panic()
            return

        # Bind ARM / CLEAR ride the control channel (brain-only).
        if ch == S._VOL_CH and msg.type == "control_change":
            if msg.control == S._BIND_ARM_CC:
                state["armed"] = (S._MODULE_BY_ID.get(msg.value) == "sampler")
                return
            if msg.control == S._BIND_CLEAR_CC:
                if S._MODULE_BY_ID.get(msg.value) == "sampler":
                    samp_chs.clear()
                state["armed"] = False
                return

        # Commit note: while sampler is armed, the next sentinel note's channel
        # becomes the sampler's channel (one module per channel).
        if (state["armed"] and msg.type == "note_on" and msg.velocity > 0
                and msg.note == S._BIND_COMMIT_NOTE
                and 1 <= ch <= 16 and ch not in S._RESERVED_CH):
            samp_chs.clear()
            samp_chs.add(ch)
            state["armed"] = False
            return

        if ch in samp_chs:
            S._route_sampler(host, msg)

    return route


def midi_input_thread(host, stop_event):
    """Open the ORC1-Synth-Bus input (same prefix the synth uses) and route
    sampler messages. Mirrors orc1_synth.midi_input_thread's resilience: a
    routing bug never kills the pump, and port I/O errors trigger a reconnect."""
    import mido
    prefix = S._SYNTH_CFG.get("MIDI_PORT_PREFIX", "").lower()
    route = _make_router(host)

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

    _last_route_err = 0.0
    while not stop_event.is_set():
        port_name = find_port()
        if not port_name:
            print("[sampler-host] No MIDI port found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        print(f"[sampler-host] MIDI in: {port_name}", flush=True)
        try:
            with mido.open_input(port_name) as port:
                for msg in port:
                    if stop_event.is_set():
                        break
                    try:
                        route(msg)
                    except Exception as re:
                        _now = time.time()
                        if _now - _last_route_err > 1.0:
                            _last_route_err = _now
                            print(f"[sampler-host] MIDI route error "
                                  f"({getattr(msg, 'type', '?')}): {re}",
                                  flush=True)
        except Exception as e:
            print(f"[sampler-host] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def run_jack(client, host, stop_event):
    """Start the JACK client (out_L/out_R), auto-connect to the physical outs so
    the sampler is audible by default, and run until stop_event is set."""
    out_L = client.outports.register("out_L")
    out_R = client.outports.register("out_R")

    @client.set_process_callback
    def process(frames):
        L, R = host.render(frames)
        out_L.get_array()[:] = L
        out_R.get_array()[:] = R

    @client.set_shutdown_callback
    def shutdown(status, reason):
        print(f"[sampler-host] JACK shutdown: {reason}", flush=True)
        stop_event.set()

    with client:
        S.autoconnect_module_out(client, out_L, out_R, "sampler",
                                 log=lambda m: print(f"[sampler-host] {m}",
                                                     flush=True))
        print(f"[sampler-host] JACK client running as '{JACK_NAME}'", flush=True)
        stop_event.wait()
    print("[sampler-host] JACK client stopped", flush=True)


def main():
    # Open the JACK client FIRST so we know the server's actual rate before
    # building the sampler. S.SAMPLE_RATE is set to the SERVER rate (used by the
    # host's PeakLimiter and passed to the manager as out_rate). The sampler DECK
    # keeps its buffer at the RING rate (48k) and scales its READ-STEP in render()
    # to out_rate -- it does NOT resample at load. Server via the JACK_SERVER env
    # (a per-card jackd may run at 44100, e.g. the Mixtrack Pro 3).
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
        print(f"[sampler-host] JACK server '{_srv}' unreachable ({_e}); "
              f"falling back to the default server", flush=True)
        client = jack.Client(JACK_NAME, servername=None)
    S.SAMPLE_RATE = client.samplerate
    print(f"[sampler-host] JACK server '{_srv or 'default'}' sample rate "
          f"{client.samplerate} Hz", flush=True)
    host = SamplerHost()
    stop_event = threading.Event()

    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            print("[sampler-host] SIGTERM -- stopping", flush=True)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception as _se:
        print(f"[sampler-host] SIGTERM handler not installed: {_se}", flush=True)

    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    midi_t = threading.Thread(target=midi_input_thread,
                              args=(host, stop_event), daemon=True)
    midi_t.start()

    try:
        run_jack(client, host, stop_event)
    except KeyboardInterrupt:
        print("\n[sampler-host] Interrupted", flush=True)
    except Exception as e:
        print(f"[sampler-host] Fatal: {e}", flush=True)
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
