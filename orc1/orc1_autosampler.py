#!/usr/bin/env python3
"""
ORC-1 autosampler -- sweep a synth over MIDI, record every note, and emit a
ready-to-load MPC1000/JJOS2XL program (WAVs with smpl root-note chunks + .PGM).

Design (see _planning/ORC1_Autosampler_PGM_Spec.md):
- Runs INSIDE the brain (orc1.py) as a background job thread. MIDI goes out
  through the brain's normal `send_to(port_key, msg)`, so any PORTS entry --
  or the internal synth bus -- can be the target instrument.
- Audio comes in through this module's OWN JACK client, opened only while a
  job runs (nothing added to the RT synth process; no always-on client to
  trip the jackd-wedge). Capture source = any JACK output port pair, exactly
  like the skipback source card (default: the internal synth, so a sweep
  works with zero external gear).
- No latency dance: capture starts BEFORE the note-on and the trim pass eats
  the known-silent head.
- Export: per-kit normalization (one gain for the whole instrument), fade-out
  declick, sox resample 48k->44.1k 16-bit (MPC native), smpl root chunk
  written into every WAV (JJOS2XL INST mode derives keygroups from it),
  .PGM built by pgmlib. One folder = one program = copy to the MPC.

Status is published to ~/.orc1_autosample_job.json (atomic) after every step;
the web page polls it. Control = AUTOSAMPLE_START / AUTOSAMPLE_CANCEL over
the cmd socket.
"""

import json
import os
import shutil
import subprocess
import threading
import time

STATE_PATH = os.path.expanduser("~/.orc1_autosample_job.json")

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(n):
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 2}"


class AutoSampler:
    """One job at a time. Construct once; start()/cancel()/status()."""

    def __init__(self, cfg, send_midi_fn):
        self.cfg = dict(cfg or {})
        self._send = send_midi_fn          # send_midi_fn(port_key, mido.Message)
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._status = {"running": False, "phase": "idle"}

    # -- public ---------------------------------------------------------------

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, params):
        if self.running():
            return False, "a sweep is already running"
        p = self._merge_params(params or {})
        err = self._validate(p)
        if err:
            self._publish({"running": False, "phase": "error", "error": err})
            return False, err
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(p,),
                                        daemon=True, name="autosampler")
        self._thread.start()
        return True, None

    def cancel(self):
        self._cancel.set()

    def status(self):
        with self._lock:
            return dict(self._status)

    # -- params ---------------------------------------------------------------

    def _merge_params(self, params):
        c = self.cfg
        g = lambda k, d: params.get(k.lower(), c.get(k, d))
        p = {
            "kit":        str(params.get("kit", "") or "SWEEP").strip(),
            "port":       str(g("PORT", "synth")),
            "channel":    int(g("CHANNEL", 8)),
            "note_lo":    int(g("NOTE_LO", 36)),
            "note_hi":    int(g("NOTE_HI", 96)),
            "note_step":  int(g("NOTE_STEP", 3)),
            "velocities": [int(v) for v in g("VELOCITIES", [127])],
            "hold_s":     float(g("HOLD_S", 2.0)),
            "tail_s":     float(g("TAIL_S", 1.5)),
            "gap_s":      float(g("GAP_S", 0.3)),
            "capture_l":  str(g("CAPTURE_L", "ORC1-Poly:out_L")),
            "capture_r":  str(g("CAPTURE_R", "ORC1-Poly:out_R")),
            "stereo":     bool(g("STEREO", False)),
            "trim_db":    float(g("TRIM_DB", -60.0)),
            "fade_ms":    float(g("FADE_MS", 10.0)),
            "target_peak": float(g("TARGET_PEAK", 0.9)),
            "max_gain":   float(g("MAX_GAIN", 20.0)),
            "prog_change": params.get("prog_change"),
            "dry_run":    bool(params.get("dry_run", False)),
            "out_root":   os.path.expanduser(
                c.get("OUT_ROOT", "~/recordings/autosample")),
        }
        return p

    @staticmethod
    def _validate(p):
        if not (0 <= p["note_lo"] <= p["note_hi"] <= 127):
            return "bad note range"
        if p["note_step"] < 1:
            return "note step must be >= 1"
        if not p["velocities"] or any(not 1 <= v <= 127 for v in p["velocities"]):
            return "velocities must be 1-127"
        if not (1 <= p["channel"] <= 16):
            return "channel must be 1-16"
        n = len(range(p["note_lo"], p["note_hi"] + 1, p["note_step"]))
        if n > 64:
            return (f"{n} notes > 64 pads -- raise the step or shrink the "
                    f"range (the MPC fills gaps from smpl root notes)")
        if n * len(p["velocities"]) > 256:
            return "too many samples (max 256 per sweep)"
        return None

    # -- status publishing ------------------------------------------------------

    def _publish(self, upd):
        with self._lock:
            self._status.update(upd)
            snap = dict(self._status)
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    # -- the job ----------------------------------------------------------------

    def _run(self, p):
        import mido
        steps = [(n, v)
                 for n in range(p["note_lo"], p["note_hi"] + 1, p["note_step"])
                 for v in p["velocities"]]
        self._publish({"running": True, "phase": "starting", "error": None,
                       "kit": p["kit"], "step": 0, "total": len(steps),
                       "note": None, "dir": None, "warnings": [],
                       "started": time.time(), "dry_run": p["dry_run"]})
        client = None
        warnings = []
        try:
            # optional program change first (sample a specific patch)
            if p["prog_change"] is not None:
                self._send(p["port"], mido.Message(
                    "program_change", channel=p["channel"] - 1,
                    program=int(p["prog_change"]) & 0x7F))
                time.sleep(0.2)

            if p["dry_run"]:
                self._dry_run(p, steps, mido)
                return

            import numpy as np
            import jack

            client = jack.Client("orc1-autosampler", no_start_server=True)
            sr = client.samplerate
            n_ch = 2 if p["stereo"] else 1
            inports = [client.inports.register(f"in_{i+1}")
                       for i in range(n_ch)]

            # capture state shared with the process callback
            cap = {"buf": None, "idx": 0, "on": False}

            @client.set_process_callback
            def process(frames):
                if not cap["on"]:
                    return
                b, i = cap["buf"], cap["idx"]
                if b is None or i + frames > b.shape[1]:
                    cap["on"] = False          # buffer full -- stop silently
                    return
                for c_i, port in enumerate(inports):
                    b[c_i, i:i + frames] = port.get_array()
                cap["idx"] = i + frames

            client.activate()
            srcs = [p["capture_l"], p["capture_r"]][:n_ch]
            for src, port in zip(srcs, inports):
                try:
                    client.connect(src, port)
                except jack.JackError as e:
                    raise RuntimeError(f"can't connect capture source "
                                       f"'{src}': {e}")

            takes = []                          # (note, vel, float32 [ch, n])
            hit_secs = 0.15 + p["hold_s"] + p["tail_s"] + 0.5
            for i, (note, vel) in enumerate(steps):
                if self._cancel.is_set():
                    raise KeyboardInterrupt
                self._publish({"phase": "sampling", "step": i,
                               "note": f"{_note_name(note)} v{vel}"})
                cap["buf"] = np.zeros((n_ch, int(hit_secs * sr)),
                                      dtype="float32")
                cap["idx"] = 0
                cap["on"] = True
                time.sleep(0.15)                          # pre-roll
                self._send(p["port"], mido.Message(
                    "note_on", channel=p["channel"] - 1,
                    note=note, velocity=vel))
                self._sleep(p["hold_s"])
                self._send(p["port"], mido.Message(
                    "note_off", channel=p["channel"] - 1,
                    note=note, velocity=0))
                self._sleep(p["tail_s"])
                cap["on"] = False
                takes.append((note, vel, cap["buf"][:, :cap["idx"]].copy()))
                cap["buf"] = None
                self._sleep(p["gap_s"])

            client.deactivate(); client.close(); client = None

            # -- process + export ------------------------------------------------
            self._publish({"phase": "processing", "note": None})
            processed, peak = [], 0.0
            thresh = 10 ** (p["trim_db"] / 20.0)
            for note, vel, buf in takes:
                mono_probe = np.max(np.abs(buf), axis=0)
                nz = np.nonzero(mono_probe > thresh)[0]
                if len(nz) == 0:
                    warnings.append(f"{_note_name(note)} v{vel}: silent -- "
                                    f"skipped (check routing/levels)")
                    continue
                a = max(0, nz[0] - int(0.003 * sr))       # keep 3ms pre-attack
                b = min(buf.shape[1], nz[-1] + int(0.010 * sr))
                cut = buf[:, a:b].copy()
                fade = int(p["fade_ms"] / 1000.0 * sr)
                if fade > 1 and cut.shape[1] > fade:
                    cut[:, -fade:] *= np.linspace(1.0, 0.0, fade,
                                                  dtype="float32")
                peak = max(peak, float(np.max(np.abs(cut))))
                processed.append((note, vel, cut))
            if not processed:
                raise RuntimeError("every take came back silent -- wrong "
                                   "capture source, or the synth isn't "
                                   "listening on that port/channel")
            gain = min(p["target_peak"] / peak if peak > 0 else 1.0,
                       p["max_gain"])

            import pgmlib
            kit8 = pgmlib.sanitize_name(p["kit"])[:8].strip() or "SWEEP"
            out_dir = os.path.join(p["out_root"], kit8)
            os.makedirs(out_dir, exist_ok=True)
            tmp_dir = os.path.join(out_dir, ".work48k")
            os.makedirs(tmp_dir, exist_ok=True)

            multi_vel = len(p["velocities"]) > 1
            wavs = []
            self._publish({"phase": "exporting", "dir": out_dir})
            import wave as wavemod
            for note, vel, cut in processed:
                if self._cancel.is_set():
                    raise KeyboardInterrupt
                nm = f"{kit8}-{_note_name(note)}"
                if multi_vel:
                    nm += f"-v{vel}"
                nm = pgmlib.sanitize_name(nm)
                raw48 = os.path.join(tmp_dir, nm + ".wav")
                pcm = (np.clip(cut * gain, -1.0, 1.0) * 32767.0
                       ).astype("<i2")
                with wavemod.open(raw48, "wb") as w:
                    w.setnchannels(n_ch)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(pcm.T.tobytes())
                out = os.path.join(out_dir, nm + ".WAV")
                # --guard: rate-conversion ripple can overshoot full scale on
                # harmonically dense sources (measured: 0.9 target hit 1.0 on
                # a chip-voice E2E) — guard auto-trims instead of clipping.
                r = subprocess.run(
                    ["sox", "--guard", raw48, "-r", "44100", "-b", "16", out],
                    capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError(f"sox failed on {nm}: "
                                       f"{r.stderr.strip()[:200]}")
                pgmlib.set_wav_root(out, note)
                wavs.append((note, vel, out))

            # -- build the program ----------------------------------------------
            self._publish({"phase": "building pgm"})
            if multi_vel:
                warnings.append("multiple velocity layers captured; .PGM "
                                "built from the loudest layer only (JJOS2XL "
                                "INST velocity layering not yet "
                                "hardware-verified)")
                top = max(p["velocities"])
                pgm_wavs = [w for n, v, w in wavs if v == top]
            else:
                pgm_wavs = [w for n, v, w in wavs]
            prog, notes, bwarns = pgmlib.build_inst(pgm_wavs)
            warnings.extend(bwarns)
            pgm_path = os.path.join(out_dir, kit8 + ".PGM")
            prog.save(pgm_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)

            self._publish({
                "running": False, "phase": "done", "note": None,
                "step": len(steps), "dir": out_dir,
                "pgm": os.path.basename(pgm_path),
                "wavs": len(wavs), "gain_db": round(20 * __import__("math")
                                                    .log10(gain), 1),
                "warnings": warnings})
        except KeyboardInterrupt:
            self._all_off(p)
            self._publish({"running": False, "phase": "cancelled",
                           "warnings": warnings})
        except Exception as e:
            self._all_off(p)
            msg = str(e)
            if "server_failed" in msg or "server_error" in msg:
                # orc1-jackd owns jackd since 2026-07-14 -- restarting the
                # synth no longer reaps or replaces it, so the old advice
                # ("restart orc1-synth") left the wedge in place.
                msg += (" -- jackd is wedged (rejects new clients while "
                        "audio still plays). Fix: restart the JACK owner "
                        "(sudo systemctl restart orc1-jackd), then sweep "
                        "again.")
            self._publish({"running": False, "phase": "error",
                           "error": msg, "warnings": warnings})
        finally:
            if client is not None:
                try:
                    client.deactivate(); client.close()
                except Exception:
                    pass

    def _dry_run(self, p, steps, mido):
        """Play the sweep without recording -- routing/level check."""
        for i, (note, vel) in enumerate(steps):
            if self._cancel.is_set():
                self._all_off(p)
                self._publish({"running": False, "phase": "cancelled"})
                return
            self._publish({"phase": "dry run", "step": i,
                           "note": f"{_note_name(note)} v{vel}"})
            self._send(p["port"], mido.Message(
                "note_on", channel=p["channel"] - 1, note=note, velocity=vel))
            self._sleep(min(p["hold_s"], 0.6))
            self._send(p["port"], mido.Message(
                "note_off", channel=p["channel"] - 1, note=note, velocity=0))
            self._sleep(0.15)
        self._publish({"running": False, "phase": "done",
                       "step": len(steps), "dry_run": True})

    def _sleep(self, secs):
        """Cancellable sleep."""
        end = time.time() + secs
        while time.time() < end:
            if self._cancel.is_set():
                raise KeyboardInterrupt
            time.sleep(min(0.1, max(0.0, end - time.time())))

    def _all_off(self, p):
        try:
            import mido
            self._send(p["port"], mido.Message(
                "control_change", channel=p["channel"] - 1,
                control=123, value=0))
            for n in range(p["note_lo"], p["note_hi"] + 1):
                self._send(p["port"], mido.Message(
                    "note_off", channel=p["channel"] - 1, note=n, velocity=0))
        except Exception:
            pass
