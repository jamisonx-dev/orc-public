#!/usr/bin/env python3
"""
ORC-1 FX mixer -- one INSTANCE of an audio-rate wet/dry INSERT (2026-09-03).
============================================================================

An fx insert takes ONE stereo SOURCE (any JACK pair -- a module output like
ORC1-Poly:out_L/R, or a hardware input like system:capture_1/2 for a pisound fx
loop), runs it through a jalv fx chain at 100% wet, and blends dry<->wet at
AUDIO RATE, sending the result to ONE stereo DESTINATION. Many independent
instances can run at once, each on its own source, with NO bridging/summing
between them -- the per-module-independence north star, applied to fx.

Why audio-rate here: LV2 control ports step once per audio block, so a plugin's
own dry/wet clicks when you ride it. The host does the blend at sample rate
instead (plugin stays 100% wet), so mix + level are rideable with no glitch.

Signal (per instance):
    src -> dry_L/R
    send_L/R (= dry passthrough) -> [ jalv chain, 100% wet ] -> wet_L/R
    out_L/R = (dry*(1-mix) + wet*mix) * level  (slewed, limited) -> dst
    (the src -> dst DIRECT path is removed -- the insert replaces it)

The chain (send -> plugin(s) -> wet) is wired by the fx-insert manager / rack;
this process owns only src->dry, out->dst, and cutting src->dst, plus the blend.

CLI:
    orc1_fxmix.py --name ORC1-FX-poly \
        --src ORC1-Poly:out_L,ORC1-Poly:out_R \
        --dst system:playback_1,system:playback_2 \
        --mix-cc 3 --level-cc 4

Control: reads the given mix/level CCs on CH_CTRL off the shared ORC1-Synth-Bus
(the brain's FXMIX_SET {id} cmd emits them). mix 0..1, level 0..2 (unity 64).
"""

import os
import sys
import time
import argparse
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S    # SAMPLE_RATE, bus/config constants, PeakLimiter


class FxMixer:
    """Sample-accurate wet/dry crossfade + return level. mix and level each slew
    linearly toward their target over ~RAMP_MS so no control move ever steps."""

    def __init__(self, ramp_ms=25.0):
        self.sr        = S.SAMPLE_RATE
        # Default mix=0 = DRY passthrough (bypass): an insert must pass its source
        # through even before/without a chain, so inserting one never silences the
        # source. Dial mix up to blend in the wet fx.
        self.mix       = 0.0   # current (slewed)
        self.mix_t     = 0.0   # target
        self.level     = 1.0
        self.level_t   = 1.0
        self._slew = 1.0 / max(1.0, (ramp_ms / 1000.0) * self.sr)
        # Output limiter: a hot return / reverb build-up must not clip the dst.
        self.lim = S.PeakLimiter()

    def set_mix(self, v):    self.mix_t   = min(1.0, max(0.0, float(v)))
    def set_level(self, v):  self.level_t = min(2.0, max(0.0, float(v)))

    def _slew_ramp(self, cur, tgt, n):
        if cur == tgt:
            return None, cur
        k    = np.arange(1, n + 1, dtype=np.float32)
        raw  = cur + np.sign(tgt - cur) * self._slew * k
        lo, hi = (cur, tgt) if tgt >= cur else (tgt, cur)
        ramp = np.clip(raw, lo, hi).astype(np.float32)
        return ramp, float(ramp[-1])

    def process(self, dry_l, dry_r, wet_l, wet_r):
        n = wet_l.size
        mramp, self.mix   = self._slew_ramp(self.mix,   self.mix_t,   n)
        lramp, self.level = self._slew_ramp(self.level, self.level_t, n)
        m = self.mix   if mramp is None else mramp
        g = self.level if lramp is None else lramp
        out_l = (dry_l * (1.0 - m) + wet_l * m) * g
        out_r = (dry_r * (1.0 - m) + wet_r * m) * g
        return tuple(self.lim.process([out_l.astype(np.float32),
                                       out_r.astype(np.float32)]))


def _make_router(mix, mix_cc, level_cc):
    """route(msg): this instance's CH_CTRL mix/level CCs -> the mixer targets."""
    def route(msg):
        if not hasattr(msg, "channel"):
            return
        if (msg.channel + 1) == S._VOL_CH and msg.type == "control_change":
            if msg.control == mix_cc:
                mix.set_mix(msg.value / 127.0)
            elif msg.control == level_cc:
                mix.set_level(msg.value / 64.0)   # unity at 64, up to 2x
    return route


def midi_input_thread(mix, mix_cc, level_cc, stop_event):
    import mido
    prefix = S._SYNTH_CFG.get("MIDI_PORT_PREFIX", "").lower()
    route = _make_router(mix, mix_cc, level_cc)

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
            time.sleep(3)
            continue
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
                            print(f"[fxmix] MIDI route error: {re}", flush=True)
        except Exception as e:
            print(f"[fxmix] MIDI error: {e}, retrying in 2s...", flush=True)
            time.sleep(2)


def run_jack(mix, name, src, dst, stop_event):
    """src, dst are [L, R] JACK port-name lists. This instance connects src->dry,
    out->dst, and CUTS src->dst (the insert replaces the direct path). The chain
    (send->plugins->wet) is wired externally by the manager/rack."""
    import jack
    client = jack.Client(name)
    dL = client.inports.register("dry_L"); dR = client.inports.register("dry_R")
    wL = client.inports.register("wet_L"); wR = client.inports.register("wet_R")
    sL = client.outports.register("send_L"); sR = client.outports.register("send_R")
    oL = client.outports.register("out_L"); oR = client.outports.register("out_R")

    @client.set_process_callback
    def process(frames):
        dl = dL.get_array(); dr = dR.get_array()
        sL.get_array()[:] = dl           # send bus = dry passthrough (-> chain)
        sR.get_array()[:] = dr
        ol, orr = mix.process(dl, dr, wL.get_array(), wR.get_array())
        oL.get_array()[:] = ol
        oR.get_array()[:] = orr

    @client.set_shutdown_callback
    def shutdown(status, reason):
        print(f"[fxmix {name}] JACK shutdown: {reason}", flush=True)
        stop_event.set()
        # EXIT NOW. Falling through to the `with client:` exit (deactivate/close
        # against a dead server) WEDGES this process forever -- measured
        # 2026-09-24: after an orc1-jackd restart the mixer stayed alive, holding
        # no ports, so the insert manager's heal() saw a live pid and never
        # restarted the insert (fx silently gone). Same libjack trap the skipback
        # recorder hit on 09-23. Nothing here needs flushing; the manager
        # restarts the insert from its config.
        os._exit(75)

    def _reconcile():
        """Own this instance's fixed wiring: src->dry, out->dst, and CUT src->dst
        (the module auto-connects src->dst = playback; the insert must sit in that
        path). Additive/idempotent, polled so it survives a src or dst restart."""
        pairs_in  = [(src[0], "dry_L"), (src[1], "dry_R")]
        pairs_out = [("out_L", dst[0]), ("out_R", dst[1])]

        def _pass():
            try:
                for s, d in pairs_in:
                    try: client.connect(s, f"{name}:{d}")
                    except Exception: pass
                for o, d in pairs_out:
                    try: client.connect(f"{name}:{o}", d)
                    except Exception: pass
                # cut the DIRECT src->dst so we don't hear dry twice
                for s in src:
                    for d in dst:
                        try: client.disconnect(s, d)
                        except Exception: pass
            except Exception:
                pass

        _pass()                             # wire IMMEDIATELY -- stop_event.wait
        while not stop_event.wait(3.0):     # would otherwise leave the insert with
            _pass()                         # no path (src->dst uncut) for 3s

    with client:
        print(f"[fxmix {name}] insert: {src} -> [chain] -> {dst}", flush=True)
        threading.Thread(target=_reconcile, daemon=True).start()
        stop_event.wait()
    print(f"[fxmix {name}] stopped", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--src", required=True, help="srcL,srcR JACK ports")
    ap.add_argument("--dst", required=True, help="dstL,dstR JACK ports")
    ap.add_argument("--mix-cc",   type=int, default=int(getattr(S, "_FXMIX_MIX_CC", 3)))
    ap.add_argument("--level-cc", type=int, default=int(getattr(S, "_FXMIX_LEVEL_CC", 4)))
    args = ap.parse_args()
    src = args.src.split(","); dst = args.dst.split(",")
    if len(src) != 2 or len(dst) != 2:
        print("--src and --dst each need L,R", flush=True); sys.exit(2)

    mix = FxMixer()
    stop_event = threading.Event()
    try:
        import signal as _signal

        def _on_term(_sig, _frm):
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            stop_event.set()

        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception:
        pass

    # Load mido + its rtmidi backend HERE, before the MIDI thread starts and
    # before the JACK client goes live. Importing them inside the thread ran
    # concurrently with activation and held the GIL long enough to starve the
    # RT callback: the insert xrun'd the whole graph (skipback included) on most
    # starts -- measured 2026-09-24.
    S.preload_midi_backend()   # before the MIDI thread + JACK activation (GIL -> xruns, 2026-09-24)
    threading.Thread(target=midi_input_thread,
                     args=(mix, args.mix_cc, args.level_cc, stop_event),
                     daemon=True).start()
    try:
        run_jack(mix, args.name, src, dst, stop_event)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[fxmix] Fatal: {e}", flush=True)
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
