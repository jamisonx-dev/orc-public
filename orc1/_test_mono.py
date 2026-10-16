#!/usr/bin/env python3
"""MonoSynth engine + mono host regression (2026-09-03).

Renders real blocks and MEASURES the headline features: mono last-note priority,
PORTAMENTO glide, LAYERED oscillators (an octave layer puts energy at 2f),
NOTE-TRACKING ring mod (sidebands track pitch; a fixed-Hz carrier does not),
plus set_cc mapping and the host-router dispatch. Pure stdlib + numpy; imports
orc1_synth (side-effect free) and orc1_mono_host (jack/mido deferred).

Run on the Pi:  python3 _test_mono.py
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_synth as S

EXPECTED_TOTAL = 25
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ! {label}")

SR = S.SAMPLE_RATE
N  = 2048

def render_secs(m, secs):
    """Render `secs` of audio in N-frame blocks, concatenated."""
    nblk = max(1, int(secs * SR / N))
    return np.concatenate([m.render(N) for _ in range(nblk)])

def dom_freq(buf):
    """Dominant frequency (Hz) via FFT peak, ignoring DC."""
    if buf.size < 64:
        return 0.0
    w = np.abs(np.fft.rfft(buf * np.hanning(buf.size)))
    w[0] = 0.0
    return float(np.argmax(w) * SR / buf.size)

def energy_at(buf, f, tol=0.06):
    """Fraction of spectral energy within +/-tol*f of frequency f."""
    mag = np.abs(np.fft.rfft(buf * np.hanning(buf.size)))
    freqs = np.fft.rfftfreq(buf.size, 1.0 / SR)
    tot = float(mag.sum()) or 1.0
    band = mag[(freqs > f * (1 - tol)) & (freqs < f * (1 + tol))].sum()
    return float(band) / tot

A4 = 69   # 440 Hz

# -- 1. sound / silence -------------------------------------------------------
m = S.MonoSynth()
check(np.max(np.abs(m.render(N))) < 1e-7, "idle: silent")
m.note_on(A4, 100)
check(np.max(np.abs(render_secs(m, 0.1))) > 0.01, "note_on: produces audio")
f = dom_freq(render_secs(m, 0.2))
check(abs(f - 440.0) < 20, f"pitch tracks the note (440Hz, got {f:.0f})")

# -- 2. mono last-note priority -----------------------------------------------
m = S.MonoSynth()
m.note_on(60, 100)          # C4 ~261.6
m.note_on(72, 100)          # C5 ~523.3  (last note wins)
render_secs(m, 0.05)
check(abs(dom_freq(render_secs(m, 0.2)) - 523.3) < 25, "last-note priority: newest note sounds")
m.note_off(72)              # release C5 -> fall back to still-held C4
render_secs(m, 0.05)
check(abs(dom_freq(render_secs(m, 0.2)) - 261.6) < 20, "note_off of top note falls back to the held note")
m.note_off(60)
render_secs(m, 1.0)         # let release finish
check(m.env_state == 'idle', "releasing the last note goes idle")

# -- 3. portamento glide ------------------------------------------------------
# glide_time is the one-pole TIME CONSTANT (63% point); full arrival ~5 tau.
m = S.MonoSynth()
m.set_cc(5, 40)             # glide tau ~0.63s
m.note_on(48, 100)          # low note first
render_secs(m, 0.3)         # settle
start_f = m.freq
m.note_on(84, 100)          # jump up 3 octaves -> should GLIDE, not jump
m.render(N)                 # one block into the glide
mid_f = m.freq
check(start_f < mid_f < S._midi_to_hz(84), "portamento: freq moves toward target but has NOT arrived")
render_secs(m, 5.0)         # ~8 tau -> effectively arrived
check(abs(m.freq - S._midi_to_hz(84)) < 10, "portamento: reaches the target eventually")
# glide off -> instant
m2 = S.MonoSynth()
m2.set_cc(5, 0)
m2.note_on(48, 100); m2.render(N)
m2.note_on(84, 100)
check(abs(m2.freq - S._midi_to_hz(84)) < 1, "glide=0: pitch jumps instantly")

# -- 4. layered oscillators (octave layer -> energy at 2f) --------------------
m = S.MonoSynth()
m.set_cc(1, 0)              # layer0 sine (clean fundamental)
m.note_on(A4, 100)
base = render_secs(m, 0.3)
e2_before = energy_at(base, 880.0)
m2 = S.MonoSynth()
m2.set_cc(1, 0)            # layer0 sine
m2.set_cc(23, 0)          # layer1 sine
m2.set_cc(21, int(round((12 + 24) / 48.0 * 127)))   # layer1 semitone = +12
m2.set_cc(24, 64)         # layer1 gain ~unity
m2.set_cc(20, 127)        # layer1 ON
m2.note_on(A4, 100)
lay = render_secs(m2, 0.3)
e2_after = energy_at(lay, 880.0)
check(e2_after > e2_before + 0.05, "octave layer adds energy at 2f (880Hz)")
check(energy_at(lay, 440.0) > 0.05, "base layer still present at f (440Hz)")
# layer OFF removes it again
m2.set_cc(20, 0)
check(m2.layers[1]['on'] is False, "layer1 on/off CC toggles the layer")

# -- 5. note-tracking ring mod ------------------------------------------------
# depth on, ratio 1.0 -> sidebands at f*(1+-1) = DC and 2f. Track: at a HIGHER
# note the upper sideband moves WITH the note (constant interval).
def upper_sideband_ratio(note):
    mm = S.MonoSynth()
    mm.set_cc(1, 0)          # sine base
    mm.set_cc(14, 24)        # ratio 1.0 detent (_RM_RATIOS[3]) -> sideband at 2f
    mm.set_cc(15, 0)         # carrier Hz 0 = TRACK
    mm.set_cc(12, 127)       # full depth
    mm.note_on(note, 100)
    buf = render_secs(mm, 0.3)
    f0 = S._midi_to_hz(note)
    return energy_at(buf, 2 * f0)     # tracked upper sideband sits at 2f
trk69 = upper_sideband_ratio(69)
check(upper_sideband_ratio(57) > 0.03, "ring mod: tracked upper sideband present (low note)")
check(trk69 > 0.03, "ring mod: tracked upper sideband present (high note)")
# fixed-Hz carrier is a real DIFFERENCE: the sideband sits at f+fixed, NOT at 2f,
# so a fixed carrier puts clearly less energy at 2f than tracking does.
mf = S.MonoSynth()
mf.set_cc(1, 0); mf.set_cc(12, 127); mf.set_cc(15, 65)   # fixed carrier ~300Hz
mf.note_on(69, 100)
buf = render_secs(mf, 0.3)
check(energy_at(buf, 880.0) < 0.5 * trk69,
      "fixed-Hz carrier puts less at 2f than tracking (control test)")

# -- 6. set_cc mapping --------------------------------------------------------
m = S.MonoSynth()
m.set_cc(5, 127);  check(abs(m.glide_time - 2.0) < 1e-6, "CC5 sets portamento time")
m.set_cc(26, 127); check(m.layers[2]['semi'] == 24, "layer2 semitone maps to +24 at CC127")
m.set_cc(26, 0);   check(m.layers[2]['semi'] == -24, "layer2 semitone maps to -24 at CC0")
m.set_cc(27, 127); check(abs(m.layers[2]['cents'] - 50.0) < 1e-6, "layer2 detune maps to +50c at CC127")
m.set_cc(1, 96);   check(m.layers[0]['wave'] == 'triangle', "CC1 selects layer0 waveform")

# -- 7. host router dispatch --------------------------------------------------
import mido
import orc1_mono_host as MH

class FakeMono:
    def __init__(self):
        self.calls = []
        self.mono = type("M", (), {"master": 0.0})()
        self._mono_bus_dirty = False
    def mono_note_on(self, n, v):  self.calls.append(("on", n, v))
    def mono_note_off(self, n):    self.calls.append(("off", n))
    def mono_cc(self, c, v):       self.calls.append(("cc", c, v))
    def all_sound_off(self):       self.calls.append(("panic",))

S._BINDINGS = dict(S._DEFAULT_BINDINGS); S._BINDINGS[3] = "mono"
eng = FakeMono(); route = MH._make_router(eng)
route(mido.Message("note_on",  channel=2, note=60, velocity=100))   # ch3
check(("on", 60, 100) in eng.calls, "host: mono note dispatches on its bound channel")
route(mido.Message("note_off", channel=2, note=60, velocity=0))
check(("off", 60) in eng.calls, "host: note_off dispatches")
route(mido.Message("control_change", channel=2, control=74, value=90))
check(("cc", 74, 90) in eng.calls, "host: CC dispatches")
route(mido.Message("control_change", channel=S._VOL_CH - 1, control=S._VOL_MONO_CC, value=100))
check(abs(eng.mono.master - 100 / 127) < 1e-6 and eng._mono_bus_dirty,
      "host: CH_CTRL MONO_CC sets the bus master + marks persist")
route(mido.Message("control_change", channel=0, control=120, value=0))
check(("panic",) in eng.calls, "host: panic fires")

print(f"\n_test_mono: {PASS} passed, {FAIL} failed  (expected {EXPECTED_TOTAL})")
if PASS != EXPECTED_TOTAL:
    print(f"  !! total {PASS} != expected {EXPECTED_TOTAL} -- a group may not have run")
sys.exit(1 if (FAIL or PASS != EXPECTED_TOTAL) else 0)
