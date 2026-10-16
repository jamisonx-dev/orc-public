#!/usr/bin/env python3
"""
ORC-1 SampleDeck -- offline DSP measurement rig.
================================================

WHY THIS EXISTS
---------------
The turntable feel (scratch / slow / speed / reverse) has been reworked
several times and never landed.  Every previous attempt reasoned about the
code; nothing ever RENDERED it and looked at the samples.  This does.

SampleDeck has no JACK dependency -- it is pure numpy over a buffer -- so it
can be driven block-by-block offline and measured exactly.  The core metric
is the DISCONTINUITY: the largest sample-to-sample jump in the output,
compared against the largest jump the source signal itself can produce at
that playback rate.  A ratio near 1.0 means the deck is reading the waveform
continuously.  A big ratio means a click, and the block index tells you which
control caused it.

    python3 _test_sampler_dsp.py         # run the suite
    python3 _test_sampler_dsp.py -v      # print the per-block traces

SAFETY: constructs SampleDeck directly.  Opens no JACK client, reads no ring
buffer, touches no file, imports nothing from the running engine.  Safe to
run on the Pi while the instrument is playing.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np                                           # noqa: E402
from orc1_sampler import SampleDeck, SamplerManager          # noqa: E402

SR    = 48000
BLOCK = 1024                     # the Pi's live JACK period
FREQ  = 220.0                    # test tone: low enough to be smooth


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------

def make_tone(seconds=2.0, freq=FREQ, sr=SR):
    """A pure sine holding a WHOLE number of cycles, so the buffer's own loop
    seam is continuous.  Without that, a synthetic buffer's end-to-start wrap
    is a discontinuity in the SOURCE and every loop-seam measurement blames
    the deck for the test's own signal.  (Real captures get a seam crossfade
    in _load_slice for exactly this reason.)"""
    cycles = max(1, round(seconds * freq))
    n = int(round(cycles * sr / freq))
    t = np.arange(n, dtype=np.float64) / sr
    return (0.8 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def expected_max_step(rate, freq=FREQ, sr=SR, amp=0.8):
    """Largest |x[n+1]-x[n]| a sine of this frequency can produce when read at
    `rate` samples per output sample."""
    return abs(amp * 2 * np.pi * freq * abs(rate) / sr) * 1.35   # +35% slack


class Rig:
    """Drive a deck block by block, keeping the concatenated output so we can
    look for discontinuities ACROSS block boundaries (where they live)."""

    def __init__(self, tone_seconds=2.0, **deck_kw):
        self.deck = SampleDeck("T", sample_rate=SR, **deck_kw)
        self.deck.load_buffer(make_tone(tone_seconds))
        self.out = []
        self.block_gain = []       # peak of each block, for amp-step analysis
        self.rates = []            # platter velocity at the end of each block
        self.marks = {}            # label -> sample index in self.out

    def mark(self, label):
        self.marks[label] = sum(len(b) for b in self.out)

    def run(self, blocks, before=None):
        for i in range(blocks):
            if before:
                before(i)
            b = self.deck.render(BLOCK)
            if b is None:
                b = np.zeros(BLOCK, dtype=np.float32)
            self.out.append(b)
            self.block_gain.append(float(np.max(np.abs(b))))
            self.rates.append(float(self.deck._jog_step_prev))

    def peak_rate(self, default=1.0):
        """Fastest the platter actually turned during the run.

        Slope limits must be taken from the speed REACHED, not from
        _JOG_MAX_VEL: the clamp ceiling is 8x, which makes the allowed step so
        large that a full-scale gain glitch slips under it.  An earlier version
        of this rig used the ceiling and passed a mutation that reintroduced
        the flat per-block gate.
        """
        r = [abs(x) for x in self.rates] or [default]
        return max(max(r), default)

    def signal(self):
        return np.concatenate(self.out) if self.out else np.zeros(1)

    def max_step(self, lo=None, hi=None):
        s = self.signal()[lo:hi]
        if len(s) < 2:
            return 0.0
        return float(np.max(np.abs(np.diff(s))))

    def step_at(self, idx, span=4):
        """The jump right at one boundary -- where a per-block gain change or
        a position teleport shows up."""
        s = self.signal()
        a = max(0, idx - span)
        b = min(len(s), idx + span)
        if b - a < 2:
            return 0.0
        return float(np.max(np.abs(np.diff(s[a:b]))))


class RaceDeck(SampleDeck):
    """A deck that runs one control call PART-WAY THROUGH render().

    SampleDeck's state is written by the MIDI thread and read by the JACK
    audio callback with no lock, so a control call really can land in the
    middle of render().  A serial test can never reproduce that -- which is
    why three race fixes originally survived their own mutation tests.

    _apply_tone() is called after this block's samples have been built from
    _pos and before the advance / release-fade / declick tail: exactly the
    window every one of those bugs lives in.  Overriding it lets a test say
    "this control call arrives HERE" and get a deterministic answer.

    ⚠ SCOPE, HONESTLY: this covers the window FROM THE _pos READ ONWARD, not
    all of render().  It structurally cannot reach anything that happens
    before that read, and a real click hid in exactly that blind spot -- the
    declick token used to be claimed ~85 lines above the _pos read, so a
    jump() arriving in between rendered the new position with the token
    already spent (measured: a 0.78 step against a 0.023 max slope).  That
    gap is now closed by construction: render() reads _data and _pos and
    claims the token in three adjacent statements, and
    test_token_is_claimed_next_to_the_reads holds the ordering, and the
    advance guard hands the token back if a call slips into the gap.  If you
    ever move those apart, this class will NOT catch what you let back in.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._inject = None

    def inject_mid_render(self, fn):
        """Run fn(deck) once, inside the next render(), mid-block."""
        self._inject = fn

    def _apply_tone(self, out, n_frames):
        if self._inject is not None:
            fn, self._inject = self._inject, None
            fn(self)
        return super()._apply_tone(out, n_frames)


def race_rig(tone_seconds=2.0):
    r = Rig.__new__(Rig)
    r.deck = RaceDeck("T", sample_rate=SR)
    r.deck.load_buffer(make_tone(tone_seconds))
    r.out, r.block_gain, r.rates, r.marks = [], [], [], {}
    return r


# The TOTAL is a check: a lost/renamed test must fail the run, not shrink it.
EXPECTED = 125
RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail))
    return bool(ok)


# ---------------------------------------------------------------------------
# 1. Baseline -- steady playback must be perfectly continuous
# ---------------------------------------------------------------------------

def test_steady_playback_continuous():
    r = Rig()
    r.run(20)
    got  = r.max_step()
    want = expected_max_step(1.0)
    check(got <= want,
          "steady playback has no discontinuity",
          f"max step {got:.5f} vs sine slope limit {want:.5f}")


# ---------------------------------------------------------------------------
# 2. Scratch engage / release -- the platter touch
# ---------------------------------------------------------------------------

def test_platter_touch_does_not_click():
    """Touching the capacitive platter with the deck playing.

    render() computes `amp` per block and multiplies the WHOLE block by it.
    On the block where scratch engages, _jog_engaged is False and _scratch is
    True, so amp is 0.0 -- the output drops from full level to digital
    silence between two adjacent samples.  set_scratch() does set _fade_left,
    but that ramp goes 0->1 (a fade IN) and is applied AFTER the amp multiply,
    so it ramps silence into silence and declicks nothing.
    """
    r = Rig()
    r.run(8)
    r.mark("touch")
    r.deck.set_scratch(True)
    r.run(8)
    idx  = r.marks["touch"]
    got  = r.step_at(idx)
    want = expected_max_step(1.0)
    check(got <= want,
          "platter touch does not click",
          f"jump at touch {got:.5f} vs slope limit {want:.5f} "
          f"({got / want:.1f}x)")


def test_platter_release_does_not_click():
    r = Rig()
    r.deck.set_scratch(True)
    r.run(8)
    r.mark("release")
    r.deck.set_scratch(False)
    r.run(8)
    idx  = r.marks["release"]
    got  = r.step_at(idx)
    want = expected_max_step(1.0)
    check(got <= want,
          "platter release does not click",
          f"jump at release {got:.5f} vs slope limit {want:.5f} "
          f"({got / want:.1f}x)")


# ---------------------------------------------------------------------------
# 3. A real scratch gesture
# ---------------------------------------------------------------------------

def _scratch_gesture(r, ticks_per_block, blocks):
    """Spin the wheel at a steady rate for `blocks` blocks."""
    def before(i):
        for _ in range(abs(ticks_per_block)):
            r.deck.scrub(200.0 * (1 if ticks_per_block > 0 else -1))
    r.run(blocks, before=before)


def test_scratch_gesture_is_continuous():
    """A scratch must read the waveform continuously.  The per-block `amp`
    gate (min(1, |step|/0.15)) changes in one jump at each block boundary and
    is applied flat across the block, so any block-to-block change in platter
    speed also steps the AMPLITUDE -- audible as grit on every 21ms boundary
    throughout the gesture, independent of the waveform itself."""
    r = Rig()
    r.deck.set_scratch(True)
    r.run(2)
    r.mark("spin")
    _scratch_gesture(r, 3, 10)          # forward spin
    _scratch_gesture(r, -3, 10)         # reverse it, like a real scratch
    r.run(6)                            # let it brake to silence
    idx = r.marks["spin"]
    s   = r.signal()[idx:]
    peak_rate = r.peak_rate()
    got  = float(np.max(np.abs(np.diff(s)))) if len(s) > 1 else 0.0
    want = expected_max_step(peak_rate)
    check(got <= want,
          "scratch gesture reads the waveform continuously",
          f"max step {got:.5f} vs slope limit at rate {peak_rate:.2f} "
          f"{want:.5f} ({got / max(want, 1e-9):.1f}x)")


def test_groove_gate_does_not_step():
    """The gate that silences a stopped groove must ramp ACROSS a block.

    Applied flat, it multiplies the whole block by one number, so every block
    boundary where the platter speed changed is a gain step -- grit through
    the whole gesture.  Measured as an inter-sample discontinuity during a
    BRAKE, which is where the gate actually moves (at full speed it sits at
    1.0 and measures nothing).
    """
    r = Rig()
    r.run(4)
    r.mark("brake")
    r.deck.set_scratch(True)      # hand on the platter, then let it stop
    r.run(30)
    s = r.signal()[r.marks["brake"]:]
    got  = float(np.max(np.abs(np.diff(s)))) if len(s) > 1 else 0.0
    want = expected_max_step(r.peak_rate())
    check(got <= want, "groove gate does not step between blocks",
          f"max step during brake {got:.5f} vs slope limit {want:.5f} "
          f"({got / max(want, 1e-9):.1f}x)")


# ---------------------------------------------------------------------------
# 3b. Turntable feel -- a hand BRAKES a record, it does not mute it
# ---------------------------------------------------------------------------

def _audible_ms(sig, thresh=0.02):
    idx = np.where(np.abs(sig) > thresh)[0]
    return (float(idx[-1] + 1) / SR * 1000.0) if len(idx) else 0.0


def test_platter_touch_brakes_rather_than_mutes():
    """Putting a hand on a spinning record slows it to a stop -- the pitch
    falls and you hear it happen.  It does not cut to silence.

    The mechanism is that set_scratch() leaves the platter velocity alone and
    only changes what it is heading towards; teleporting the velocity to zero
    instead makes the deck mute in a single block.  (The click tests alone do
    NOT catch that regression -- the declicker hides the step while leaving
    the feel wrong -- which is why this test exists.)
    """
    r = Rig()
    r.run(4)
    r.mark("touch")
    r.deck.set_scratch(True)
    r.run(30)                                  # no jog input: pure brake
    tail = r.signal()[r.marks["touch"]:]
    ms   = _audible_ms(tail)
    rates = [abs(x) for x in r.rates[4:]]
    falling = sum(1 for a, b in zip(rates, rates[1:]) if b < a - 1e-6)
    check(ms >= 30.0 and falling >= 3,
          "platter touch brakes the record instead of muting it",
          f"audible for {ms:.0f}ms after touch (want >=30), "
          f"{falling} decelerating blocks (want >=3)")


def test_platter_touch_reaches_silence():
    """...and it does actually stop, rather than droning on."""
    r = Rig()
    r.run(4)
    r.deck.set_scratch(True)
    r.run(40)
    tail = np.concatenate(r.out[-5:])
    check(float(np.max(np.abs(tail))) < 1e-3,
          "braked platter comes to a stop",
          f"peak after brake {float(np.max(np.abs(tail))):.5f}")


def test_platter_release_spins_up():
    """Letting go lets the motor pull the platter back up to speed, audibly.
    Snapping straight back to full rate is what the old code did."""
    r = Rig()
    r.run(4)
    r.deck.set_scratch(True)
    r.run(30)                                  # brake to a stop
    n_before = len(r.rates)
    r.deck.set_scratch(False)                  # let go
    r.run(60)
    rates = [abs(x) for x in r.rates[n_before:]]
    if not rates:
        check(False, "release produced blocks to measure", "")
        return
    rising = sum(1 for a, b in zip(rates, rates[1:]) if b > a + 1e-6)
    # blocks taken to get within 5% of normal speed
    settle = next((i for i, v in enumerate(rates) if v > 0.95), len(rates))
    settle_ms = settle * BLOCK / SR * 1000.0
    check(rising >= 3 and settle_ms >= 100.0 and rates[-1] > 0.95,
          "platter spins back up to speed instead of snapping",
          f"{rising} accelerating blocks, reached speed in "
          f"{settle_ms:.0f}ms (want >=100), final rate {rates[-1]:.3f}")


# ---------------------------------------------------------------------------
# 4. Direction -- reverse must actually reverse
# ---------------------------------------------------------------------------

def test_reverse_plays_backwards():
    """Spinning the platter backwards must drive the read head backwards.

    Measured on the platter VELOCITY, not on _pos: the read head wraps modulo
    the buffer, so a fast reverse spin can lap past zero and land at a HIGHER
    position number while genuinely playing backwards.  (An earlier version of
    this test compared raw positions and reported a working reverse as broken.)
    """
    r = Rig()
    r.deck.set_scratch(True)
    r.run(2)
    _scratch_gesture(r, -4, 6)
    vel = r.deck._jog_vel
    check(vel < 0, "backward spin drives the platter backwards",
          f"platter velocity {vel:.3f} (want < 0)")


def test_reverse_read_head_moves_backwards():
    """Stronger than the velocity check: the READ POSITION must decrease
    monotonically through a backward spin.

    Measured with the buffer wrap unwrapped.  (Comparing raw _pos values, or
    correlating forward audio against time-reversed backward audio, both give
    false failures -- the first because a fast spin laps past zero, the second
    because the two gestures cover different regions of the buffer.)
    """
    r = Rig()
    r.deck.set_scratch(True)
    r.run(2)
    n = len(r.deck._data) - 1
    positions = []

    def before(i):
        for _ in range(4):
            r.deck.scrub(-200.0)
        positions.append(r.deck._pos)
    r.run(8, before=before)
    positions.append(r.deck._pos)
    # unwrap the modulo so a lap past zero reads as continued travel
    unwrapped, off = [], 0.0
    for i, p in enumerate(positions):
        if i and p > positions[i - 1] + n / 2:
            off -= n
        unwrapped.append(p + off)
    deltas = np.diff(unwrapped)
    check(len(deltas) and bool(np.all(deltas <= 0)),
          "backward spin moves the read head monotonically backwards",
          f"{int(np.sum(deltas > 0))} forward steps out of {len(deltas)}")


def test_deck_exposes_a_reverse_control():
    """Reverse must be reachable as a control, not only by physically
    spinning the jog backwards (which was the case until 2026-08-01)."""
    has = all(hasattr(SampleDeck, n) for n in ("set_reverse", "toggle_reverse"))
    check(has, "deck exposes a reverse control",
          "expected set_reverse() and toggle_reverse() on SampleDeck")


def test_reverse_turns_the_record_around():
    """Engaging reverse must take the platter through a stop and back up to
    speed the other way -- not teleport the read rate to its negative."""
    r = Rig()
    r.run(6)
    n_before = len(r.rates)
    r.deck.set_reverse(True)
    r.run(60)
    rates = r.rates[n_before:]
    crossings = sum(1 for a, b in zip(rates, rates[1:]) if a > 0 >= b)
    settled = rates[-1]
    # it must pass through zero rather than jumping over it
    near_zero = sum(1 for v in rates if abs(v) < 0.5)
    check(settled < -0.95 and crossings == 1 and near_zero >= 2,
          "reverse spins the record around through a stop",
          f"settled at {settled:.3f} (want < -0.95), {crossings} zero "
          f"crossing(s), {near_zero} block(s) near a standstill")


def test_reverse_transition_does_not_click():
    r = Rig()
    r.run(6)
    r.mark("rev")
    r.deck.set_reverse(True)
    r.run(60)
    s = r.signal()[r.marks["rev"]:]
    got  = float(np.max(np.abs(np.diff(s))))
    want = expected_max_step(r.peak_rate())
    check(got <= want, "reverse transition does not click",
          f"max step {got:.5f} vs slope limit {want:.5f}")


def test_reverse_returns_to_forward():
    r = Rig()
    r.run(4)
    r.deck.set_reverse(True)
    r.run(60)
    r.deck.set_reverse(False)
    r.run(60)
    check(r.rates[-1] > 0.95, "reverse releases back to forward play",
          f"final rate {r.rates[-1]:.3f}")


def test_reverse_survives_speed_and_bend():
    """_rate stays a positive magnitude, so the pitch fader and the momentary
    bend must keep working while the record runs backwards."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(1.0))
    d.set_reverse(True)
    d.set_speed(1.5)
    for _ in range(80):
        d.render(BLOCK)
    backwards_fast = d._jog_step_prev
    d.pitch_bend(True, up=True)
    for _ in range(40):
        d.render(BLOCK)
    bent = d._jog_step_prev
    check(backwards_fast < -1.4 and abs(bent) > abs(backwards_fast),
          "speed and bend still work in reverse",
          f"rate {backwards_fast:.3f} -> bent {bent:.3f}")


# ---------------------------------------------------------------------------
# 5. Speed / pitch
# ---------------------------------------------------------------------------

def test_speed_change_is_glided_not_snapped():
    """A pitch-fader move must glide, not jump-cut.  The glide uses
    g = n_frames / _SPEED_GLIDE_FRAMES, which SNAPS to the full target in one
    block whenever the JACK period is >= the glide length -- the same defect
    class that made the drum CCs click (CLAUDE.md 2026-07-28).  Assert real
    headroom at the live period, not just that it happens to work today."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(1.0))
    d.set_speed(2.0)
    d.render(BLOCK)
    after_one = d._rate
    check(after_one < 1.9,
          "speed glide survives one JACK block",
          f"rate went 1.0 -> {after_one:.3f} in one {BLOCK}-frame block "
          f"(glide length {d._SPEED_GLIDE_FRAMES} frames)")


def test_speed_change_is_continuous():
    r = Rig()
    r.run(6)
    r.mark("speed")
    r.deck.set_speed(1.6)
    r.run(12)
    idx  = r.marks["speed"]
    got  = r.max_step(lo=idx)
    want = expected_max_step(1.6)
    check(got <= want, "speed change stays continuous",
          f"max step {got:.5f} vs slope limit {want:.5f}")


def test_pitch_bend_returns_to_prior_speed():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(1.0))
    d.set_speed(1.25)
    for _ in range(20):
        d.render(BLOCK)
    base = d._rate
    d.pitch_bend(True, up=True)
    for _ in range(20):
        d.render(BLOCK)
    bent = d._rate
    d.pitch_bend(False)
    for _ in range(20):
        d.render(BLOCK)
    back = d._rate
    check(bent > base and abs(back - base) < 1e-3,
          "pitch bend engages and returns to the prior speed",
          f"base {base:.4f} -> bent {bent:.4f} -> back {back:.4f}")


# ---------------------------------------------------------------------------
# 6. Transport edges
# ---------------------------------------------------------------------------

def test_hotcue_jump_does_not_click():
    r = Rig()
    r.run(8)
    r.mark("jump")
    r.deck.jump(0.5)
    r.run(8)
    idx  = r.marks["jump"]
    got  = r.step_at(idx)
    want = expected_max_step(1.0)
    check(got <= want, "hotcue jump does not click",
          f"jump {got:.5f} vs slope limit {want:.5f}")


def test_loop_wrap_does_not_click():
    """The loop seam: positions wrap via mod(n-1), so the read head jumps from
    the end of the buffer back to the start every pass."""
    r = Rig(tone_seconds=0.05)          # short buffer -> many wraps
    r.run(20)
    got  = r.max_step()
    want = expected_max_step(1.0)
    check(got <= want, "loop wrap does not click",
          f"max step {got:.5f} vs slope limit {want:.5f}")


def test_stop_does_not_click():
    """CUE / hard stop.  render() returns None the moment _playing() goes
    false, so the output drops from whatever the waveform was sitting at
    straight to digital silence with no ramp at all."""
    r = Rig()
    r.run(8)
    r.mark("stop")
    r.deck.stop()
    r.run(4)
    got  = r.step_at(r.marks["stop"])
    want = expected_max_step(1.0)
    check(got <= want, "hard stop does not click",
          f"jump {got:.5f} vs slope limit {want:.5f}")


def test_loop_toggle_off_does_not_click():
    """PLAY pressed a second time -- same instant-silence path as stop."""
    r = Rig()
    r.run(8)
    r.mark("off")
    r.deck.gate_toggle()
    r.run(4)
    got  = r.step_at(r.marks["off"])
    want = expected_max_step(1.0)
    check(got <= want, "loop toggle off does not click",
          f"jump {got:.5f} vs slope limit {want:.5f}")


def test_cancelled_release_does_not_rewind_later():
    """A stop that is INTERRUPTED must not leave its rewind armed.

    stop() defers the rewind until the fade completes.  gate()/gate_toggle()/
    jump() can cancel that fade -- and if they clear only _release_left, the
    rewind flag survives with nothing left to reset it.  The next ordinary
    gate-off (which is meant to stop and HOLD position) then completes, sees
    the stale flag, and snaps to 0: an unrequested rewind arriving long after
    the stop that armed it.
    """
    bad = []
    for label, resume in (("gate", lambda d: d.gate(True)),
                          ("toggle", lambda d: d.gate_toggle()),
                          ("jump", lambda d: d.jump(0.5))):
        r = Rig()
        r.run(6)
        r.deck.stop()                       # arms the deferred rewind
        resume(r.deck)                      # cancels the fade before it ends
        r.run(10)
        pos = r.deck._pos
        r.deck.gate_toggle() if r.deck._latched else r.deck.gate(False)
        r.run(10)                           # let the plain release complete
        if r.deck._pos == 0.0 and pos != 0.0:
            bad.append(f"{label}: rewound to 0 from {pos:.0f}")
    check(not bad, "a cancelled stop does not rewind a later release",
          " | ".join(bad))


def test_cued_load_during_release_is_silent():
    """load_buffer_cued() promises a stopped, silent deck.  A load landing
    while a stop fade is still counting down must not render the new buffer
    and fade it out -- that is audible content from a cued deck."""
    r = Rig()
    r.run(6)
    r.deck.stop()
    r.deck.load_buffer_cued(make_tone(1.0))   # lands mid-fade
    r.out.clear()
    r.run(6)
    peak = float(np.max(np.abs(r.signal())))
    check(peak < 1e-6, "a cued load during a release stays silent",
          f"peak after cued load {peak:.6f}")


def test_double_stop_does_not_teleport_the_fade():
    """Two stop()s before a render must not move _pos out from under the
    in-flight fade (the tail would play from the top of the buffer)."""
    r = Rig()
    r.run(6)
    pos_at_stop = r.deck._pos
    r.deck.stop()
    r.deck.stop()                              # second call, same block
    check(r.deck._pos == pos_at_stop,
          "a second stop does not teleport the pending fade",
          f"_pos moved {pos_at_stop:.0f} -> {r.deck._pos:.0f} while "
          f"{r.deck._release_left} frames of fade were pending")


def test_fresh_onset_respects_reverse():
    """_jog_step_prev seeds the intra-block rate ramp.  Setting it to the
    unsigned _rate on a fresh capture/load/stop while reverse is engaged makes
    the next block ramp from +rate to -rate -- a whole sign flip crammed into
    one block instead of the intended motor glide, so a fragment of
    FORWARD audio plays before the deck settles back into reverse."""
    bad = []
    onsets = {
        "load_buffer":      lambda d: d.load_buffer(make_tone(1.0)),
        "load_buffer_cued": lambda d: d.load_buffer_cued(make_tone(1.0)),
        "stop":             lambda d: d.stop(),
    }
    for label, onset in onsets.items():
        d = SampleDeck("T", sample_rate=SR)
        d.load_buffer(make_tone(1.0))
        d.set_reverse(True)
        for _ in range(80):
            d.render(BLOCK)                    # settle into reverse
        onset(d)
        if d._jog_step_prev >= 0:
            bad.append(f"{label}: _jog_step_prev = {d._jog_step_prev:.3f}")
    check(not bad, "every fresh onset keeps the reverse direction",
          " | ".join(bad) + "  (want < 0)")


def test_cue_then_play_starts_from_the_top():
    """CUE is documented as 'hard stop + rewind'.  Pressing PLAY straight
    after it -- an utterly ordinary DJ double-tap -- must start from the top.

    stop() defers its rewind until the fade completes, so PLAY cancels the
    fade before the rewind ever runs.  If the cancel merely DISCARDS the
    pending rewind, the deck relaunches from wherever CUE was pressed.
    """
    bad = []
    for label, play in (("gate", lambda d: d.gate(True)),
                        ("toggle", lambda d: d.gate_toggle())):
        r = Rig()
        r.run(8)
        assert r.deck._pos > 0
        r.deck.stop()                      # CUE
        play(r.deck)                       # PLAY, before the fade finishes
        if r.deck._pos != 0.0:
            bad.append(f"{label}: resumed at {r.deck._pos:.0f}, not 0")
    check(not bad, "CUE then PLAY starts from the top", " | ".join(bad))


def test_declick_token_survives_a_mid_render_jump():
    """A control call landing part-way through render() belongs to the NEXT
    block.  If render() consumes _dc_pending for a block whose samples were
    already built from the OLD position, the real discontinuity -- one block
    later -- gets no declick at all: a click, produced by the declicker."""
    r = Rig()
    r.run(4)
    d = r.deck
    bad = []

    # (a) The invariant: _declick must use the token CLAIMED at the top of
    #     render(), not re-read the flag.  A flag set after the claim has to
    #     survive to the next block -- that is what stops a mid-render jump
    #     having its declick spent on a block built from the old position.
    d._dc_pending = True
    d._declick(np.zeros(BLOCK, dtype=np.float32), BLOCK, dc_now=False)
    if not d._dc_pending:
        bad.append("_declick consumed a token it was not given")

    # (b) And a token that IS claimed gets applied: the block starts exactly
    #     where the previous one ended, with no step.
    d._dc_pending = True
    d.jump(0.37)
    d._dc_pending = True
    last = d._last_out
    blk = d.render(BLOCK)
    if blk is None:
        bad.append("no audio after jump")
    elif abs(float(blk[0]) - last) > 1e-4:
        bad.append(f"block starts at {float(blk[0]):.5f}, "
                   f"previous ended at {last:.5f}")
    if d._dc_pending:
        bad.append("claimed token was not consumed")
    check(not bad, "a declick token is consumed by the block that needs it",
          " | ".join(bad))


def test_buffer_swapped_mid_render_keeps_its_position():
    """load_buffer sets the position it wants (0).  render() must not then
    advance a position derived from the OLD buffer's length over the top of
    it -- a capture landing on a playing deck would start at an arbitrary
    offset instead of at frame 0."""
    r = race_rig(2.0)
    r.run(6)
    d = r.deck
    old_n = len(d._data)
    d.inject_mid_render(lambda dk: dk.load_buffer(make_tone(0.05)))
    r.run(1)
    check(d._pos == 0.0 and len(d._data) < old_n,
          "a buffer loaded mid-render keeps its own start position",
          f"_pos={d._pos:.1f} after a {len(d._data)}-frame buffer landed "
          f"mid-block (old buffer was {old_n})")


def test_jump_mid_render_keeps_its_declick():
    """A jump() landing mid-render belongs to the NEXT block -- the first one
    whose samples come from the new position.  If render() consumes the
    declick token for the block already built from the OLD position, the real
    discontinuity arrives unsmoothed: a click, from the declicker."""
    r = race_rig()
    r.run(4)
    d = r.deck
    d.inject_mid_render(lambda dk: dk.jump(0.5))
    r.run(1)
    pending_after = d._dc_pending
    r.mark("landed")
    r.run(4)
    got = r.step_at(r.marks["landed"])
    want = expected_max_step(1.0)
    check(pending_after and got <= want,
          "a jump landing mid-render still gets declicked",
          f"token survived={pending_after}, step at the real transition "
          f"{got:.5f} vs limit {want:.5f}")


def test_jump_mid_render_lands_where_it_asked():
    """A hotcue / needle-drop landing mid-render must land ON its target.

    render() builds this block from _pos and then advances _pos at the tail.
    If the tail re-reads a _pos that a mid-render jump() just changed, it
    writes `target + travel` -- overshooting by a whole block (~21ms at 1x,
    more at speed).  The buffer-identity guard does NOT cover this: jump()
    changes only the position, never `_data`.
    """
    r = race_rig()
    r.run(4)
    d = r.deck
    n = len(d._data)
    target = 0.5
    d.inject_mid_render(lambda dk: dk.jump(target))
    r.run(1)
    want = target * (n - 1)
    off = abs(d._pos - want)
    check(off < 1.0,
          "a jump landing mid-render lands on its target",
          f"_pos {d._pos:.1f}, target {want:.1f}, overshoot {off:.1f} frames")


def test_rewind_mid_render_is_not_overshot():
    """Same guard, via the other path that moves only _pos: the deferred
    rewind applied inside _cancel_release() when playback resumes."""
    r = race_rig()
    r.run(6)
    d = r.deck
    d.stop()                                   # arms the deferred rewind

    def resume(dk):
        dk.gate(True)                          # applies the rewind to 0
    d.inject_mid_render(resume)
    r.run(1)
    check(d._pos < 1.0,
          "a rewind applied mid-render is not overshot",
          f"_pos {d._pos:.1f} after a mid-render rewind to 0")


def test_reverse_while_stopped_starts_in_reverse():
    """SYNC pressed on a STOPPED deck, then PLAY, must start backwards.

    A stopped record has no momentum to reverse.  Engaging the flywheel here
    seeds it with the direction the deck was last turning (stop() leaves
    _jog_step_prev at the old sign), so the first second of playback runs
    FORWARD and slews into reverse over the motor time constant.
    """
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(1.0))
    for _ in range(20):
        d.render(BLOCK)
    d.stop()
    for _ in range(6):
        d.render(BLOCK)                        # let the release finish
    d.set_reverse(True)                        # SYNC while stopped
    d.gate_toggle()                            # PLAY
    rates = []
    for _ in range(6):
        d.render(BLOCK)
        rates.append(d._jog_step_prev)
    forward = [x for x in rates if x > 0.05]
    check(not forward and rates[0] < 0,
          "reverse engaged while stopped starts backwards on play",
          f"first blocks after PLAY: {[round(x, 3) for x in rates]}")


def test_token_is_claimed_next_to_the_reads():
    """Source-order guard: render() must read self._data and self._pos and
    then claim the declick token, in adjacent statements.

    A load_buffer() landing between those two points would otherwise have its
    token claimed by a block rendered from the OLD buffer, so the transition
    into the new content -- one block later -- goes unsmoothed.  That window
    sits EARLIER than RaceDeck's injection point (_apply_tone), so no
    behavioural test in this file can reach it.

    NOTE the direction: the claim comes AFTER the reads.  An earlier version of
    this guard asserted claim-FIRST -- which is the ordering that CAUSED a
    full-scale click, and it was the only check that failed on the correct fix.
    The gap is narrowed, not eliminated: a control call landing between the
    reads and the claim is caught at runtime instead, by the advance guard
    handing the token back (test_mid_render_control_hands_the_token_back).
    """
    import inspect
    src = inspect.getsource(SampleDeck.render)
    claim = src.find("dc_now = self._dc_pending")
    read_data = src.find("data = self._data")
    read_pos = src.find("pos0 = self._pos")
    bad = []
    if min(claim, read_data, read_pos) < 0:
        bad.append("could not locate all three statements")
    else:
        if not (read_data < read_pos < claim):
            bad.append(f"order is data@{read_data} pos@{read_pos} "
                       f"claim@{claim}; want data, then pos, then claim")
        # "adjacent" = no substantial work between them.  The first version of
        # this guard asserted the claim came FIRST, which is the ordering that
        # CAUSED the click it was meant to prevent -- it was the only check
        # that failed on the correct fix.
        gap = src[read_data:claim].count("\n")
        if gap > 8:
            bad.append(f"{gap} lines between the buffer read and the claim; "
                       f"they must be adjacent or a control call fits between")
    check(not bad,
          "the declick token is claimed immediately after the data+pos reads",
          " | ".join(bad))


def test_scratch_on_a_reversed_deck_brakes_backwards():
    """Touching the platter on a deck playing BACKWARDS must brake it from
    its actual (negative) speed, not whip it forward.

    set_scratch() seeds the flywheel from the deck's current rate; using the
    unsigned _rate instead of the signed _drive() flips the direction.  It
    only bites once the flywheel has DISENGAGED -- about 1.5s after a reverse
    settles -- which is why a quick test misses it entirely.
    """
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(2.0))
    d.set_reverse(True)
    for _ in range(90):                     # let reverse settle AND disengage
        d.render(BLOCK)
    if d._jog_engaged:
        check(False, "flywheel disengaged before the touch",
              "still engaged after 90 blocks -- test cannot reach the bug")
        return
    rate_before = d._jog_step_prev
    d.set_scratch(True)
    seeded = d._jog_vel
    rates = []
    for _ in range(4):
        d.render(BLOCK)
        rates.append(d._jog_step_prev)
    check(rate_before < 0 and seeded < 0 and all(x <= 0 for x in rates),
          "a platter touch on a reversed deck brakes backwards",
          f"rate before {rate_before:.3f}, seeded {seeded:+.3f}, "
          f"then {[round(x, 3) for x in rates]}")


def test_groove_gate_threshold_does_not_mute_a_slow_record():
    """_SILENT_SPEED is why a slowed record drops in PITCH instead of
    vanishing.  Nothing caught reverting it (0.02 -> the old 0.15) because
    every other check measures duration or discontinuity, not LEVEL AT A
    GIVEN SPEED -- and both values keep the deck audible long enough.

    A record at 10% speed is still a record: full level, two octaves down.
    """
    bad = []
    # RMS, not peak, and over enough blocks to cover a whole cycle at the
    # SLOWEST speed tested.  At 5% a single block spans only a fraction of a
    # wave, so its peak is a function of where in the waveform it landed, not
    # of the gate -- measuring peak makes this test read ~66% even when the
    # gate is wide open.  Full-scale RMS of this tone is 0.8/sqrt(2).
    full_rms = 0.8 / np.sqrt(2.0)
    for speed, want_min in ((0.30, 0.9), (0.10, 0.9), (0.05, 0.9)):
        d = SampleDeck("T", sample_rate=SR)
        d.load_buffer(make_tone(2.0))
        d.render(BLOCK)
        d._scratch = True                   # hand on the platter...
        d._jog_engaged = True
        d._amp_prev = 1.0
        blocks = int(np.ceil((SR / FREQ) / speed / BLOCK)) + 2
        acc = []
        for _ in range(blocks):
            d._jog_vel = speed              # hold it there against friction
            b = d.render(BLOCK)
            if b is not None:
                acc.append(b)
        rel = (float(np.sqrt(np.mean(np.concatenate(acc) ** 2))) / full_rms
               if acc else 0.0)
        if rel < want_min:
            bad.append(f"{speed*100:.0f}% speed -> {rel*100:.0f}% level "
                       f"(want >={want_min*100:.0f}%)")
    check(not bad, "a slow record keeps its level, it does not fade out",
          " | ".join(bad))


def test_stop_then_nudge_keeps_the_fade():
    """CUE, then nudge the loop by ear -- an ordinary DJ gesture.

    swap_buffer() must drop only the pending REWIND (which would clobber the
    position just nudged to), not the stop FADE.  Calling the full
    _cancel_release() zeroed _release_left as well, cutting the deck off
    mid-waveform at ~25x the signal's own slope instead of letting it fade.
    Deterministic -- no race required.
    """
    r = Rig()
    r.run(6)
    r.mark("stop")
    r.deck.stop()
    r.deck.swap_buffer(make_tone(1.0))     # the nudge
    r.run(6)
    got = r.step_at(r.marks["stop"])
    want = expected_max_step(1.0)
    check(got <= want,
          "a nudge after CUE keeps the stop fade",
          f"step {got:.5f} vs limit {want:.5f} ({got / want:.1f}x)")


def test_cued_load_on_a_playing_deck_fades():
    """The /sampler LOAD button, pressed while the deck is PLAYING.

    Clearing the gate flags alone makes the next render() return None straight
    from full level -- a full-scale cut mid-waveform.  It must fade, the same
    way CUE does.  (A cued load on an ALREADY-STOPPED deck must still be
    silent -- that is test_cued_load_during_release_is_silent, and the two
    cases are why load_buffer_cued branches on the prior play state.)
    """
    r = Rig()
    r.run(6)
    r.mark("load")
    r.deck.load_buffer_cued(make_tone(1.0))
    r.run(6)
    got = r.step_at(r.marks["load"])
    want = expected_max_step(1.0)
    check(got <= want, "a cued load on a playing deck fades instead of cutting",
          f"step {got:.5f} vs limit {want:.5f} ({got / want:.1f}x)")


def test_render_reads_playing_state_once():
    """render() must latch _playing() ONCE.

    It used to ask twice -- once to compute `releasing`, once to decide
    whether to return None.  A stop() landing between the two calls set
    _release_left AFTER `releasing` had latched False, so render returned None
    from full level: a 34.5x step, then a whole block of silence, then back up
    for the fade.  Structural, because with one read the window no longer
    exists for a behavioural test to hit.
    """
    import inspect
    src = inspect.getsource(SampleDeck.render)
    n = src.count("self._playing()")
    check(n == 1, "render() latches the play state once",
          f"found {n} calls to self._playing() in render(); want exactly 1")


def test_mid_render_control_hands_the_token_back():
    """When the advance guard fires -- a control call moved _pos or swapped
    the buffer mid-render -- the declick token must be HANDED BACK.

    This block was built from the old state, so the discontinuity the call
    created is still ahead; spending the token here leaves the block that
    actually contains the new content unsmoothed.  Same failure round 6 fixed
    at a larger scale.
    """
    r = race_rig()
    r.run(4)
    d = r.deck
    # Arm a token, then move _pos mid-render WITHOUT setting the flag again.
    # Injecting jump() instead would not test anything: jump() sets
    # _dc_pending itself, so the flag ends up True whether the guard hands it
    # back or not, and the mutation passes.  A bare _pos move isolates the
    # invariant -- "a token was claimed, the guard fired, give it back".
    d._dc_pending = True
    n = len(d._data)
    d.inject_mid_render(lambda dk: setattr(dk, "_pos", 0.25 * (n - 1)))
    r.run(1)
    handed_back = d._dc_pending
    r.mark("landed")
    r.run(4)
    got = r.step_at(r.marks["landed"])
    want = expected_max_step(1.0)
    check(handed_back and got <= want,
          "a mid-render control call gets its declick token back",
          f"token back={handed_back}, step at the real transition "
          f"{got:.5f} vs limit {want:.5f}")


def test_groove_gate_applies_through_the_reverse_turnaround():
    """Flipping the reverse button on a playing deck must brake the platter to
    a stop, HOLD it silent for a beat, then spin it up the other way -- the
    stop-and-flip of a hand-reversed record.  A groove that isn't moving makes
    no sound, so somewhere across the near-zero-speed stretch there must be a
    genuinely silent block.

    NB: it is wrong to grab the single slowest block and check its peak -- the
    dwell holds several blocks at rate 0 and the FIRST of them is still fading
    the pitch-dive out, so its peak is high.  Check that the QUIETEST near-zero
    block actually reaches silence.
    """
    r = Rig()
    r.run(4)
    r.deck.set_reverse(True)
    r.run(60)
    rates = [abs(x) for x in r.rates]
    # True silence, not merely quiet: with the platter frozen at the flip the
    # read head sits still, so WITHOUT the gate the block is a frozen DC at
    # whatever tone value it stopped on (~0.02 here) -- an audible offset, not
    # silence.  The gate forces amp to 0 across the hold, so require < 1e-3,
    # which the gate delivers and the frozen-DC does not.
    near_zero = [float(np.max(np.abs(r.out[i])))
                 for i in range(len(rates)) if rates[i] < 0.05]
    quietest = min(near_zero) if near_zero else 1.0
    check(bool(near_zero) and quietest < 1e-3,
          "the groove goes silent at the reverse turnaround",
          f"quietest of {len(near_zero)} near-zero block(s) peaks {quietest:.6f} "
          f"(want < 1e-3 -- a stopped groove makes no sound)")


def test_declick_token_is_never_dropped():
    """A claimed token must never be swallowed by a path that renders
    nothing -- the next real discontinuity would then go unsmoothed."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(1.0))
    d.gate(True)
    d._data = np.zeros(1, dtype=np.float32)    # degenerate buffer
    d._dc_pending = True
    out = d.render(BLOCK)
    check(out is None and d._dc_pending,
          "a token is handed back when nothing is rendered",
          f"render returned {type(out).__name__}, pending={d._dc_pending}")


def test_release_cancelled_mid_render_is_not_zeroed():
    """render() latches `releasing` at the top.  If playback is re-requested
    before the fade block runs, the counter is 0 -- and zeroing `out` from
    index 0 would hard-silence the very block play was just asked for."""
    r = race_rig()
    r.run(6)
    d = r.deck
    d.stop()                                    # start the fade
    d.inject_mid_render(lambda dk: dk.gate(True))   # PLAY lands mid-render
    r.out.clear()
    r.run(1)
    peak = float(np.max(np.abs(r.signal())))
    check(peak > 1e-3, "a release cancelled mid-render is not hard-zeroed",
          f"block peak {peak:.6f} after play was re-requested mid-render")


def test_level_is_correct_not_just_continuous():
    """Every other check here measures a DIFFERENCE, so a whole-block gain
    error is structurally invisible to them -- scaling a smooth signal never
    introduces a new inter-sample step.  Assert absolute level too."""
    bad = []
    for g, want in ((1.0, 0.8), (0.5, 0.4), (0.25, 0.2)):
        d = SampleDeck("T", sample_rate=SR)
        d.load_buffer(make_tone(1.0))
        d.set_gain(g)
        peak = 0.0
        for _ in range(12):
            b = d.render(BLOCK)
            if b is not None:
                peak = max(peak, float(np.max(np.abs(b))))
        if abs(peak - want) > 0.02:
            bad.append(f"gain {g}: peak {peak:.3f}, want {want:.3f}")
    check(not bad, "absolute output level tracks gain", " | ".join(bad))


def test_brake_decays_smoothly_not_in_steps():
    """'>=3 decelerating blocks and still audible' is satisfied by a 3-step
    staircase mute, which sounds like a stutter, not a hand on a record.
    Assert the SHAPE: the rate must decay monotonically and the ratio between
    successive steps must stay in a narrow band (an exponential has a constant
    ratio; a staircase does not)."""
    r = Rig()
    r.run(4)
    r.deck.set_scratch(True)
    r.run(14)
    rates = [abs(x) for x in r.rates[4:] if abs(x) > 1e-4]
    if len(rates) < 6:
        check(False, "brake produced enough blocks to measure", str(rates))
        return
    ratios = [b / a for a, b in zip(rates, rates[1:]) if a > 1e-6]
    mono = all(b <= a + 1e-9 for a, b in zip(rates, rates[1:]))
    spread = max(ratios) / max(min(ratios), 1e-9)
    check(mono and spread < 1.5,
          "the brake decays smoothly rather than in steps",
          f"monotonic={mono} ratio spread={spread:.2f} "
          f"(exponential ~1.0, staircase >>1)")


def test_stop_is_silent_and_rewound():
    r = Rig()
    r.run(6)
    r.deck.stop()
    r.run(4)
    tail = np.concatenate(r.out[-3:])
    check(float(np.max(np.abs(tail))) < 1e-6 and r.deck._pos == 0.0,
          "stop silences and rewinds",
          f"tail peak {float(np.max(np.abs(tail))):.6f} pos {r.deck._pos}")


# ---------------------------------------------------------------------------
# 7. One-shot audition -- the /sampler PLAY button (2026-08-06)
# ---------------------------------------------------------------------------
# Decks LOOP by default (_loop is hardcoded True and never cleared), so before
# this the only way to hear a deck was the LOOP latch -- there was no "play it
# once" preview like the synth pages' AUDITION.  audition() arms _oneshot,
# which flips render() onto its play-through-and-stop path (dead code until
# now) instead of the _loop wrap.  These tests pin that it plays exactly once,
# stops itself, does not click, respects reverse, and does NOT poison the
# ordinary LOOP that comes after it.

def test_deck_exposes_an_audition_control():
    check(hasattr(SampleDeck, "audition"),
          "deck exposes a one-shot audition control",
          "expected SampleDeck.audition()")


def test_audition_plays_once_then_stops():
    """PLAY: the clip plays through and the deck stops ITSELF -- _playing() and
    _oneshot both False, rewound to the top ready for the next press."""
    r = Rig(tone_seconds=0.2)
    r.deck.audition()
    r.out.clear()
    r.run(40)
    sig = r.signal()
    peaked = float(np.max(np.abs(sig)))
    tail   = np.concatenate(r.out[-6:])
    tail_peak = float(np.max(np.abs(tail)))
    stopped = (not r.deck._playing()) and (not r.deck._oneshot)
    check(peaked > 0.3 and tail_peak < 1e-3 and stopped and r.deck._pos == 0.0,
          "audition plays the clip once and stops itself",
          f"peak {peaked:.3f} (want >0.3), tail peak {tail_peak:.6f} (want ~0), "
          f"playing={r.deck._playing()} oneshot={r.deck._oneshot} "
          f"pos={r.deck._pos:.1f}")


def test_audition_does_not_loop():
    """The whole point of PLAY vs LOOP: a one-shot falls silent after one pass,
    where LOOP would still be at full level.  Run well past several buffer
    lengths and assert the back half is silent -- deleting the `not _oneshot`
    gate in render() (so it wraps) fails here."""
    r = Rig(tone_seconds=0.1)          # ~4.7 blocks per pass
    r.deck.audition()
    r.out.clear()
    r.run(60)                          # ~13 passes if it were looping
    sig  = r.signal()
    back = sig[len(sig) // 2:]
    back_peak = float(np.max(np.abs(back)))
    check(back_peak < 1e-3,
          "audition does not loop -- it is silent after one pass",
          f"back-half peak {back_peak:.6f} (a loop would still be full level)")


def test_audition_start_does_not_click():
    """PLAY pressed on a deck that is already looping jumps _pos to the top;
    that jump must be declicked like a hotcue."""
    r = Rig(tone_seconds=0.5)
    r.run(6)                           # deck loops from Rig init
    r.mark("aud")
    r.deck.audition()
    r.run(6)
    got  = r.step_at(r.marks["aud"])
    want = expected_max_step(1.0)
    check(got <= want, "audition start does not click",
          f"jump {got:.5f} vs slope limit {want:.5f} "
          f"({got / max(want, 1e-9):.1f}x)")


def test_audition_end_fades_not_cuts():
    """Reaching the end sets _held/_latched False; without the release fade the
    next render() returns None straight from full level -- a full-scale step.
    Deleting the fade block at the one-shot end fails here."""
    r = Rig(tone_seconds=0.1)
    r.deck.audition()
    r.out.clear()
    r.run(40)
    sig  = r.signal()
    got  = float(np.max(np.abs(np.diff(sig)))) if len(sig) > 1 else 0.0
    want = expected_max_step(1.0)
    check(got <= want, "audition end fades instead of cutting",
          f"max step {got:.5f} vs slope limit {want:.5f} "
          f"({got / max(want, 1e-9):.1f}x)")


def test_audition_respects_reverse():
    """A reversed deck must audition from the TAIL backwards.  Starting a
    reversed deck at 0 would hit _pos < 0 on the first block and stop instantly,
    auditioning nothing."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_tone(0.2))
    d.set_reverse(True)
    for _ in range(30):
        d.render(BLOCK)                # settle into reverse (looping)
    d.audition()
    start_pos = d._pos
    n = len(d._data) - 1
    peak = 0.0
    for _ in range(60):
        b = d.render(BLOCK)
        if b is not None:
            peak = max(peak, float(np.max(np.abs(b))))
    stopped = (not d._playing()) and (not d._oneshot)
    check(start_pos > n * 0.9 and peak > 0.3 and stopped,
          "a reversed audition starts at the tail and stops",
          f"start pos {start_pos:.0f}/{n} (want near {n}), peak {peak:.3f}, "
          f"playing={d._playing()} oneshot={d._oneshot}")


def test_loop_after_audition_loops_again():
    """Regression: LOOP pressed after an audition must give a normal repeating
    loop, not inherit the one-shot's stop-at-end.  gate_toggle() clears
    _oneshot; if it did not, the loop would stop after one pass."""
    r = Rig(tone_seconds=0.1)
    r.deck.audition()
    r.run(30)                          # let the one-shot finish
    assert not r.deck._playing()
    r.deck.gate_toggle()               # LOOP
    r.out.clear()
    r.run(40)                          # ~8 buffer lengths
    sig  = r.signal()
    back = sig[len(sig) // 2:]
    back_peak = float(np.max(np.abs(back)))
    check((not r.deck._oneshot) and back_peak > 0.3,
          "LOOP after an audition loops again instead of stopping",
          f"oneshot={r.deck._oneshot}, back-half peak {back_peak:.3f} "
          f"(want full level -- it should still be looping)")


# ---------------------------------------------------------------------------
# 8. Play/loop REGION -- start + end markers (2026-08-10)
# ---------------------------------------------------------------------------
# ONE region per deck is both the trim (PLAY/one-shot runs start->end) and the
# loop (LOOP wraps start->end).  A RAMP buffer (value == position fraction) lets
# these tests read the playhead straight off the output: samples outside
# [start,end] mean the region was ignored.  Defaults span the whole buffer, so
# every path reduces to the legacy behaviour the section above already pins.

def make_ramp(seconds=1.0, sr=SR):
    """A 0..1 ramp: sample value EQUALS its position fraction, so the output
    value is a direct readout of where the read head is."""
    n = int(round(seconds * sr))
    return np.linspace(0.0, 1.0, n, dtype=np.float32)


def test_deck_exposes_bounds_controls():
    ok = all(hasattr(SampleDeck, m) for m in
             ("set_bounds", "set_bounds_start", "set_bounds_end", "_region_frames"))
    check(ok, "deck exposes the play/loop region controls",
          "expected set_bounds / set_bounds_start / set_bounds_end / _region_frames")


def test_default_region_is_the_whole_buffer():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    si, ei = d._region_frames(n)
    check(d._start_frac == 0.0 and d._end_frac == 1.0 and si == 0 and ei == n,
          "a fresh capture's region is the whole buffer (legacy behaviour)",
          f"frac {d._start_frac}..{d._end_frac}, frames {si}..{ei} vs 0..{n}")


def test_region_loop_wraps_within_bounds():
    """The strong one: a looped region must read ONLY inside [start,end].  With
    a ramp buffer, any output value outside the region is the read head leaving
    it -- deleting the region wrap would let the value sweep the whole 0..1."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))          # value == position fraction
    d.set_bounds(0.25, 0.5)
    out = []
    for _ in range(60):                    # >> region length: several wraps
        b = d.render(BLOCK)
        if b is not None:
            out.append(b)
    sig = np.concatenate(out[3:]) if len(out) > 3 else np.zeros(1)   # skip onset declick
    mn, mx = float(sig.min()), float(sig.max())
    check(mn >= 0.25 - 0.06 and mx <= 0.5 + 0.06,
          "a looped region reads only within [start,end]",
          f"observed {mn:.3f}..{mx:.3f} (want ~0.25..0.50; a full buffer reaches ~0..1)")


def test_region_oneshot_stops_at_the_region_end():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.25))
    d.set_bounds(0.2, 0.6)
    si, ei = d._region_frames(len(d._data))
    d.audition()
    peak = 0.0
    for _ in range(80):
        b = d.render(BLOCK)
        if b is not None:
            peak = max(peak, float(np.max(b)))     # ramp value == position frac
    stopped = (not d._playing()) and (not d._oneshot)
    # peak <= end+tol proves it did NOT read past the region end (a reverted
    # end-test would run on to the buffer end, value ~1.0).
    check(0.3 < peak <= 0.6 + 0.06 and stopped and abs(d._pos - si) <= 2,
          "a one-shot over a region stops at the end and rewinds to the start",
          f"peak {peak:.3f} (want ~0.6, not ~1.0), playing={d._playing()} "
          f"oneshot={d._oneshot} pos {d._pos:.1f} vs region start {si}")


def test_stop_rewinds_to_region_start_not_zero():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    d.set_bounds(0.3, 0.7)
    si, ei = d._region_frames(len(d._data))
    for _ in range(6):
        d.render(BLOCK)
    d.stop()
    for _ in range(6):
        d.render(BLOCK)
    check(abs(d._pos - si) <= 2,
          "stop rewinds to the region start, not to 0",
          f"pos {d._pos:.1f} vs region start {si} (0 would be the old behaviour)")


def test_region_reverse_starts_and_stops_at_the_ends():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.25))
    d.set_bounds(0.2, 0.6)
    si, ei = d._region_frames(len(d._data))
    d.set_reverse(True)
    d.audition()
    started_at_end = abs(d._pos - (ei - 1)) <= 2
    peak = 0.0
    minpos = float(ei)
    for _ in range(80):
        b = d.render(BLOCK)
        if b is not None:
            peak = max(peak, float(np.max(b)))
        if d._playing():
            minpos = min(minpos, d._pos)           # lowest read position while sounding
    # minpos ~ si proves reverse stopped at the region START, not at 0 -- read
    # off _pos, immune to the release fade pulling the OUTPUT toward zero.  A
    # reverted end-test would run the head all the way down to ~0.
    check(started_at_end and peak > 0.3 and minpos >= si - 2 * BLOCK and not d._playing(),
          "a reversed one-shot region starts at the end and stops within the region",
          f"start-at-end={started_at_end} peak {peak:.3f} minpos {minpos:.0f} "
          f"(want >= {si - 2 * BLOCK}) playing={d._playing()} pos {d._pos:.1f}")


def test_set_bounds_orders_and_clamps_edges():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    d.set_bounds(0.8, 0.2)                       # reversed args -> swapped
    ordered = d._start_frac < d._end_frac and abs(d._start_frac - 0.2) < 0.01
    d.set_bounds(0.5, 0.5)                       # zero width -> minimal region
    nonzero = d._end_frac > d._start_frac
    d.set_bounds(-1.0, 2.0)                      # out of range -> clamped
    clamped = d._start_frac == 0.0 and d._end_frac == 1.0
    check(ordered and nonzero and clamped,
          "set_bounds orders the edges, keeps a minimal region, and clamps 0..1",
          f"ordered={ordered} nonzero={nonzero} clamped={clamped} "
          f"({d._start_frac:.3f}..{d._end_frac:.3f})")


def test_set_bounds_pulls_an_outside_playhead_into_the_region():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    d._pos = 0.9 * (n - 1)                       # outside the region set next
    d._dc_pending = False
    d.set_bounds(0.1, 0.4)
    si, ei = d._region_frames(n)
    check(si <= d._pos <= ei - 1 and d._dc_pending,
          "set_bounds pulls an out-of-region playhead to the edge and declicks",
          f"pos {d._pos:.1f} into [{si},{ei - 1}], dc_pending={d._dc_pending}")


def test_bounds_edges_move_independently():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    d.set_bounds_start(0.3)
    d.set_bounds_end(0.7)
    check(abs(d._start_frac - 0.3) < 0.01 and abs(d._end_frac - 0.7) < 0.01,
          "start and end edges move independently (one CC / one handle each)",
          f"region {d._start_frac:.3f}..{d._end_frac:.3f} (want 0.30..0.70)")


def test_write_view_reports_the_region():
    """The page draws the markers from the view JSON, so it must carry the
    region.  Guards the field being dropped from _write_view."""
    import inspect
    src = inspect.getsource(SamplerManager._write_view)
    check('"start"' in src and "_start_frac" in src,
          "the waveform view JSON carries the region (start/end) for the page",
          "expected _write_view to include \"start\" and _start_frac")


def test_write_view_uses_a_per_thread_tmp():
    """refresh_view() (region drag / CC) fires _write_view many times a second,
    so its writer threads run concurrently.  A SHARED '<path>.tmp' lets them
    interleave into one file -> corrupt 'Extra data' JSON the page can't parse.
    The tmp must be per-thread (threading.get_ident)."""
    import inspect
    src = inspect.getsource(SamplerManager._write_view)
    check("get_ident" in src,
          "the view write uses a per-thread tmp (no shared '<path>.tmp')",
          "expected a unique tmp name (threading.get_ident) in _write_view")


def test_concurrent_view_writes_stay_valid_json():
    """Hammer _write_view from many threads and assert the landed file always
    parses -- the exact corruption ('Extra data') the per-thread tmp fixes.
    SAFE: redirects ~ to a throwaway tempdir for the duration, so it never
    touches the live ~/.orc1_sampler_view_*.json."""
    import json, tempfile, threading, shutil, os as _os, time as _time
    import orc1_sampler as _mod
    d = tempfile.mkdtemp(prefix="orc_view_")
    real = _mod.os.path.expanduser
    _mod.os.path.expanduser = (lambda p: _os.path.join(d, p[2:])
                               if isinstance(p, str) and p.startswith("~/") else real(p))
    ok, errs = True, []
    try:
        mgr = SamplerManager.__new__(SamplerManager)     # skip __init__ (no ring)
        mgr.sr = SR
        deck = SampleDeck("a", sample_rate=SR); deck.load_buffer(make_ramp(0.5))
        mgr.decks = {"A": deck}
        body = deck._data
        def hammer():
            for _ in range(40):
                mgr._write_view("A", body)               # what refresh_view calls
        ths = [threading.Thread(target=hammer) for _ in range(6)]
        for t in ths: t.start()
        for t in ths: t.join()
        _time.sleep(0.4)                                 # let daemon writers land
        with open(_os.path.join(d, ".orc1_sampler_view_A.json")) as f:
            json.load(f)                                 # single valid object or raise
    except Exception as e:
        ok = False; errs.append(str(e))
    finally:
        _mod.os.path.expanduser = real
        shutil.rmtree(d, ignore_errors=True)
    check(ok, "concurrent view writes always leave valid JSON",
          " | ".join(errs) or "parsed clean")


def test_bounds_cc_is_wired_into_the_synth_route():
    """The deck method is proven above; this proves the CC actually REACHES it
    -- the project's oldest bug class is a control not wired to its function.
    Reads the source rather than importing (orc1_synth opens JACK)."""
    with open(os.path.join(HERE, "orc1_synth.py")) as f:
        src = f.read()
    check("_SMP_START" in src and "set_bounds_start" in src
          and "_SMP_END" in src and "set_bounds_end" in src
          and "refresh_view" in src,
          "the region CCs are decoded to set_bounds_start/end (+view echo) in _route_sampler",
          "expected _SMP_START/_SMP_END -> set_bounds_* + refresh_view in orc1_synth.py")


def test_bounds_cmd_is_wired_into_the_facade():
    with open(os.path.join(HERE, "orc1.py")) as f:
        src = f.read()
    check('"SAMPLER_BOUNDS"' in src and "def sampler_bounds" in src,
          "the SAMPLER_BOUNDS cmd maps to the sampler_bounds facade",
          "expected SAMPLER_BOUNDS dispatch + sampler_bounds() in orc1.py")


# ---------------------------------------------------------------------------
# Deck persistence (survive a reboot / synth restart)
# ---------------------------------------------------------------------------
#
# These construct a SamplerManager (not just a SampleDeck), which the rest of
# this suite avoids -- so they keep the same "safe to run on the live rig"
# contract by: (1) writing ONLY into a throwaway tempdir via DECK_PERSIST_DIR,
# never the real ~/.orc1_sampler_deck_*.wav; (2) silencing _write_view so a
# restore can't clobber the live ~/.orc1_sampler_view_*.json; (3) using a dummy
# ring (persist/restore never touch the ring). The tempdir is removed after.

class _QuietMgr(SamplerManager):
    """SamplerManager whose _write_view is a no-op, so persistence tests never
    write the web view JSON into the real $HOME."""
    def _write_view(self, deck, body):
        pass


def _mk_mgr(persist_dir, persist=True):
    cfg = {"DECK_PERSIST_DIR": persist_dir, "PERSIST_DECKS": persist,
           "CAPTURE_NORMALIZE": False}
    return _QuietMgr(object(), SR, cfg=cfg)      # object() = dummy ring


def _wait_file(path, timeout=3.0):
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        if os.path.exists(path):                 # os.replace makes it atomic
            return True
        _t.sleep(0.01)
    return False


def test_persist_writes_a_file_per_deck():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m = _mk_mgr(d)
        tone = make_tone(0.2)
        m._persist_deck("A", tone)
        pa = m._deck_persist_path("A")
        ok = _wait_file(pa)
        check(ok and os.path.getsize(pa) > 44,
              "persisting a deck writes a non-empty WAV",
              f"path={pa} exists={os.path.exists(pa)}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_persist_round_trips_into_a_fresh_manager():
    """The whole feature: grab into deck A, build a BRAND-NEW manager pointing
    at the same dir (a reboot), and deck A is loaded again."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m1 = _mk_mgr(d)
        tone = make_tone(0.2)
        n_in = len(tone)
        m1._persist_deck("A", tone)
        assert _wait_file(m1._deck_persist_path("A")), "write never landed"
        m2 = _mk_mgr(d)                          # <-- the "reboot"
        da = m2.decks["A"]
        got = len(da._data)
        check(da.loaded() and abs(got - n_in) <= 1,
              "a persisted deck reloads into a fresh manager",
              f"restored {got} frames (in {n_in}), loaded={da.loaded()}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_restored_deck_is_cued_not_playing():
    """A restored deck must sit stopped at 0 (like a file load), not blast off
    into looped playback the instant the synth boots."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m1 = _mk_mgr(d)
        m1._persist_deck("B", make_tone(0.2))
        assert _wait_file(m1._deck_persist_path("B"))
        m2 = _mk_mgr(d)
        db = m2.decks["B"]
        check(db.loaded() and (not db._playing()) and db._pos == 0.0,
              "a restored deck is cued (stopped at 0), not playing",
              f"playing={db._playing()} pos={db._pos}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_restored_audio_matches_within_16bit():
    """Round-trip fidelity: the reloaded body equals the original to within
    16-bit quantization (the persist format is mono s16)."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m1 = _mk_mgr(d)
        tone = make_tone(0.2)
        m1._persist_deck("A", tone)
        assert _wait_file(m1._deck_persist_path("A"))
        m2 = _mk_mgr(d)
        got = m2.decks["A"]._data
        n = min(len(got), len(tone))
        err = float(np.max(np.abs(got[:n] - tone[:n]))) if n else 1.0
        check(err < 2.0 / 32768.0,
              "restored audio matches the original within 16-bit",
              f"max abs error {err:.6f} (want < {2/32768:.6f})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_persist_disabled_writes_nothing():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m = _mk_mgr(d, persist=False)
        m._persist_deck("A", make_tone(0.2))
        import time as _t
        _t.sleep(0.2)                            # give any errant thread a chance
        wrote = os.path.exists(m._deck_persist_path("A"))
        check(not wrote,
              "PERSIST_DECKS=False writes no deck file",
              f"unexpected file at {m._deck_persist_path('A')}" if wrote else "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_missing_persist_file_leaves_deck_empty():
    """A fresh install / wiped $HOME must boot with empty decks, no crash."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="orc_persist_")
    try:
        m = _mk_mgr(d)                           # dir exists, no wavs in it
        check((not m.decks["A"].loaded()) and (not m.decks["B"].loaded()),
              "no persisted file => decks boot empty, no error",
              f"A.loaded={m.decks['A'].loaded()} B.loaded={m.decks['B'].loaded()}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_persist_is_wired_into_the_load_paths():
    """Wiring net (this project's oldest bug class): a behavioural test proves
    _persist_deck WORKS; only reading the source proves it's actually CALLED
    from the grab and file-load paths and that restore runs at init. If someone
    unhooks it, the feature silently stops without any behavioural test failing
    unless it drives the full manager."""
    src = open(os.path.join(HERE, "orc1_sampler.py"), encoding="utf-8").read()
    calls = src.count("self._persist_deck(")        # the 2 call sites
    inits = ("if self.persist_decks:\n            self._restore_decks()" in src
             and "def _restore_decks" in src)
    wired = calls >= 2 and inits
    check(wired,
          "_persist_deck is wired into the load paths and restore runs at init",
          f"self._persist_deck calls={calls} (want >=2), restore wired={inits}")


# ---------------------------------------------------------------------------
# Tone-control declick (2026-08-12): filter/EQ knobs + crossfader must not pop
# ---------------------------------------------------------------------------

def _click_ratio(sig):
    """Worst first-diff AT a block boundary relative to the LOCAL median first-
    diff around it. ~1-2 = continuous (a louder-but-smooth signal reads ~1); a
    big ratio is a real discontinuity at the seam. Robust to amplitude changes
    (a boosting EQ makes the whole signal steeper, not just the boundary)."""
    d = np.abs(np.diff(sig.astype(np.float64)))
    worst = 0.0
    for b in range(BLOCK, len(sig), BLOCK):
        lo = max(0, b - BLOCK // 2)
        hi = min(len(d), b + BLOCK // 2)
        loc = float(np.median(d[lo:hi])) + 1e-9
        worst = max(worst, d[b - 1] / loc)
    return worst


def _play_filter(sweep):
    """Loop a tone, apply `sweep(deck, block_index)` each block, return the
    concatenated output past the initial gate ramp."""
    r = Rig(2.0)                              # decks auto-play after load_buffer
    r.run(6)                                  # settle
    r.run(40, before=lambda i: sweep(r.deck, i))
    sig = np.concatenate(r.out)
    assert float(np.max(np.abs(sig[BLOCK * 8:]))) > 0.05, "test signal is silent"
    return sig[BLOCK * 8:]                     # skip the settle


def test_sweep_filter_turnon_no_pop():
    """Engaging the sweep filter from center (bypass->active) must not pop.
    Without the turn-on state prime in _declicked_process the biquad starts
    from zero state mid-signal -- measured ~16x the local step (2026-08-12)."""
    def sweep(d, i):
        if i == 2:
            d.set_filter(-1.0)                 # slam to full low-pass
    ratio = _click_ratio(_play_filter(sweep))
    check(ratio < 2.2, "sweep filter turn-on does not pop",
          f"boundary/local click ratio {ratio:.1f} (want < 2.2; ~2.5 without sub-block)")


def test_sweep_filter_roundtrip_smooth():
    """A full filter round-trip (center -> full LP -> center) stays continuous;
    the sub-block coefficient glide keeps the active sweep from zippering."""
    def sweep(d, i):
        if i == 2:
            d.set_filter(-1.0)
        elif i == 20:
            d.set_filter(0.0)
    ratio = _click_ratio(_play_filter(sweep))
    check(ratio < 2.5, "sweep filter round-trip is smooth",
          f"boundary/local click ratio {ratio:.1f} (want < 2.5; ~3.7 without sub-block)")


def test_crossfader_ramps_no_pop():
    """A fast crossfader move must ramp the equal-power gains across the block,
    not step them at the seam. Drives the real SamplerManager.render()."""
    import types
    ring = types.SimpleNamespace(channels=2, buffer_bytes=0, rate=SR)
    mgr = SamplerManager(ring, sample_rate=SR)
    for k in ("A", "B"):
        mgr.decks[k].load_buffer(make_tone(2.0))   # auto-plays
    out = []
    for i in range(40):
        mgr.set_crossfade(i / 39.0)           # sweep A -> B over the run
        b = mgr.render(BLOCK)
        out.append(b if b is not None else np.zeros(BLOCK, dtype=np.float32))
    sig = np.concatenate(out)
    assert float(np.max(np.abs(sig))) > 0.05, "crossfader test signal is silent"
    ratio = _click_ratio(sig[BLOCK * 2:])
    check(ratio < 2.0, "crossfader move ramps without a pop",
          f"boundary/local click ratio {ratio:.1f} (want < 2.0; ~2.2 unramped)")


# ---------------------------------------------------------------------------
# Settable hotcues (set at the live playhead, jump back, clear)
# ---------------------------------------------------------------------------

def test_deck_exposes_settable_cue_controls():
    ok = all(hasattr(SampleDeck, m) for m in ("set_cue", "cue", "clear_cue"))
    check(ok, "the deck exposes set_cue / cue / clear_cue",
          "expected settable-hotcue methods on SampleDeck")


def test_cues_default_empty():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    check(d._cues == [None, None, None, None],
          "a fresh deck has four empty cues",
          f"cues {d._cues}")


def test_set_cue_stores_the_live_playhead():
    """SET must capture WHERE the head is now -- the whole point of a settable
    cue. Drive the ramp forward, set a cue, and confirm the stored fraction
    matches the current position (ramp value == position fraction)."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    for _ in range(20):                          # advance the head
        d.render(BLOCK)
    here = d._pos / (n - 1)
    d.set_cue(0)
    check(d._cues[0] is not None and abs(d._cues[0] - here) < 0.005,
          "set_cue stores the live playhead as a fraction",
          f"stored {d._cues[0]} vs live {here:.4f}")


def test_cue_sets_when_empty_then_jumps_when_set():
    """The one-action DJ semantic: first press on an empty pad SETS (does not
    move the head); the next press JUMPS back to that point. Set a cue near the
    top, run the head far away, press again, and confirm it returns."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    # First press: empty -> set at ~0. Head must not teleport.
    d.cue(0)
    set_here = d._cues[0]
    for _ in range(40):                          # run far from the cue
        d.render(BLOCK)
    far = d._pos
    d.cue(0)                                     # second press -> jump back
    # read _pos right after the jump: render() would advance it a block first
    landed = d._pos / (n - 1)
    check(set_here is not None and abs(set_here) < 0.02
          and far > n * 0.3 and abs(landed - set_here) < 0.02,
          "cue() sets an empty pad, then jumps to it on the next press",
          f"set={set_here} ran to {far:.0f} landed {landed:.3f}")


def test_cue_jump_lands_at_a_middle_cue():
    """A cue set mid-buffer jumps back to the MIDDLE, not to 0 -- proves the
    stored fraction is honoured, not a hardcoded restart."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    for _ in range(30):
        d.render(BLOCK)
    mid = d._pos / (n - 1)
    d.cue(1)                                      # set cue 1 mid-buffer
    d.jump(0.0)                                   # send the head to the top
    d.render(BLOCK)
    d.cue(1)                                      # jump back to the middle cue
    landed = d._pos / (n - 1)                     # read before render() advances it
    check(0.2 < mid < 0.9 and abs(landed - mid) < 0.02,
          "a mid-buffer cue jumps back to the middle, not to 0",
          f"cue={mid:.3f} landed {landed:.3f}")


def test_clear_cue_forgets_it_and_next_press_re_sets():
    """After clear, the pad is empty again, so the next press SETS at the new
    playhead rather than jumping to the old point."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    d.cue(2)                                      # set cue 2 at ~0
    old = d._cues[2]
    d.clear_cue(2)
    cleared = d._cues[2] is None
    for _ in range(30):
        d.render(BLOCK)
    d.cue(2)                                      # empty again -> set at new pos
    re_set = d._cues[2]
    check(old is not None and cleared and re_set is not None
          and re_set > 0.1 and abs(re_set - old) > 0.1,
          "clear_cue forgets a cue; the next press re-sets at the new playhead",
          f"old {old} cleared={cleared} re_set {re_set}")


def test_set_cue_is_a_noop_on_an_empty_deck():
    """A cue against no audio is meaningless -- set_cue must not store a
    position (and must not raise) when nothing is loaded."""
    d = SampleDeck("T", sample_rate=SR)
    raised = False
    try:
        d.set_cue(0)
        d.cue(0)
    except Exception:
        raised = True
    check(not raised and d._cues[0] is None,
          "set/cue on an empty deck stores nothing and does not raise",
          f"raised={raised} cues {d._cues}")


def test_cue_index_out_of_range_is_ignored():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    before = list(d._cues)
    raised = False
    try:
        d.cue(-1); d.cue(4); d.set_cue(99); d.clear_cue(7)
    except Exception:
        raised = True
    check(not raised and d._cues == before,
          "an out-of-range cue index is a no-op, not a crash or a stray write",
          f"raised={raised} cues {d._cues}")


def test_cue_jump_does_not_click():
    """A jump to a settable cue must declick like any other jump -- it routes
    through jump(), which the older hotcue-click test already covers, but a cue
    set MID-buffer then jumped to is the real gesture, so measure it."""
    r = Rig()
    n = len(r.deck._data)
    r.run(20)
    r.deck.set_cue(0)                             # cue at wherever the head is
    r.run(30)                                     # play on
    r.deck.cue(0)                                 # jump back to the cue
    r.run(20)
    got  = r.max_step()
    want = expected_max_step(1.0)
    check(got <= want,
          "jumping to a settable cue does not click",
          f"max step {got:.5f} vs sine slope limit {want:.5f}")


def test_capture_wipes_cues():
    """New audio -> the old cue fractions point at nothing meaningful. A fresh
    capture (load_buffer) must clear them, matching the region reset."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    for _ in range(10):
        d.render(BLOCK)
    d.set_cue(0); d.set_cue(3)
    had = d._cues[0] is not None and d._cues[3] is not None
    d.load_buffer(make_ramp(0.5))                 # fresh capture
    check(had and d._cues == [None, None, None, None],
          "a fresh capture wipes all cues",
          f"had={had} after {d._cues}")


def test_file_load_wipes_cues():
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    for _ in range(10):
        d.render(BLOCK)
    d.set_cue(1)
    had = d._cues[1] is not None
    d.load_buffer_cued(make_ramp(0.5))            # file-deck load
    check(had and d._cues == [None, None, None, None],
          "a file load wipes all cues",
          f"had={had} after {d._cues}")


def test_cues_survive_a_buffer_swap():
    """A nudge/swap_buffer shifts the window but keeps the region proportional;
    cues are fractions too, so they must survive it (only capture/load wipe)."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(0.5))
    for _ in range(10):
        d.render(BLOCK)
    d.set_cue(2)
    kept = d._cues[2]
    d.swap_buffer(make_ramp(0.5))
    check(kept is not None and d._cues[2] == kept,
          "cues survive a buffer swap (only a fresh capture/load clears them)",
          f"before {kept} after {d._cues[2]}")


def test_write_view_reports_the_cues():
    """The page (and any future web control) reads cue state from the view
    JSON, so it must carry them. Guards the field being dropped."""
    import inspect
    src = inspect.getsource(SamplerManager._write_view)
    check('"cues"' in src and "_cues" in src,
          "the waveform view JSON carries the cues for the page",
          "expected _write_view to include \"cues\" and _cues")


def test_cue_note_is_wired_into_the_synth_route():
    """The deck methods are proven above; this proves the CUE NOTE actually
    reaches them with the set-or-jump / clear split -- the project's oldest bug
    class is a control not wired to its function. Reads source (synth opens
    JACK)."""
    with open(os.path.join(HERE, "orc1_synth.py")) as f:
        src = f.read()
    check("_SMP_CUE" in src and '"cue"' in src and '"clear_cue"' in src
          and "refresh_view" in src,
          "the cue note decodes to cue/clear_cue (+view echo) in _route_sampler",
          "expected _SMP_CUE -> cue/clear_cue + refresh_view in orc1_synth.py")


def test_cue_facades_are_wired():
    with open(os.path.join(HERE, "orc1.py")) as f:
        src = f.read()
    check("def sampler_cue(" in src and "def sampler_cue_clear(" in src,
          "the sampler_cue / sampler_cue_clear facades exist in orc1.py",
          "expected both cue facades in orc1.py")


# ---------------------------------------------------------------------------
# Press-anchored capture (2026-08-18): the bar-grab ends at the player's PRESS
# (offset_ms == 0), NOT the ORC bar grid, and that end-now path takes its OWN
# latency (press_latency_s), live-tunable + persisted.
# ---------------------------------------------------------------------------

class _FakeRing:
    rate = SR
    channels = 2
    buffer_bytes = SR * 2 * 2 * 10           # 10 s stereo s16
    def refresh_geometry(self):  return False
    def head_info(self):         return (self.buffer_bytes // 2, 0)   # (pos, extra)


def _mgr_with_fake_ring():
    m = SamplerManager.__new__(SamplerManager)     # skip __init__ (opens no ring)
    m.ring          = _FakeRing()
    m.sr            = SR
    m.max_seconds   = 60
    m.latency_s     = -0.099
    m.press_latency_s = 0.0
    # per-source press trim (default slot only) + onset-snap OFF, so the plain
    # capture tests below exercise the pre-2026-09-15 window placement exactly.
    m._press_default_s      = 0.0
    m._press_by_source      = {}
    m._skipback_status_path = "/nonexistent"       # -> "__default__" key
    m.onset_snap            = False
    m.onset_snap_ms         = 120.0
    m.onset_snap_sens       = 0.35
    m._seam_frames  = 0
    m.decks         = {"B": object()}
    # Per-deck grab source (2026-09-15): both decks read the one fake ring
    # (track 0), so these pre-existing capture tests exercise the same path.
    m._rings        = {0: m.ring}
    m._deck_src     = {"A": 0, "B": 0}
    m._captured     = {}
    m._load_slice   = (lambda deck, end, frames, gain=None, phase_ms=None:
                       m._captured.__setitem__("end", int(end)))
    return m


def test_capture_frames_routes_press_by_offset():
    """offset 0 => end-now/press path (press=True); offset>0 => grid path. This
    is the wiring that makes a bar-grab press-anchored -- the project's oldest
    bug class is a control not reaching its function."""
    m = SamplerManager.__new__(SamplerManager)
    calls = []
    m._do_capture = (lambda deck, frames, offset_ms=0.0, phase_align=False,
                     press=False: calls.append((offset_ms, phase_align, press)))
    m.capture_frames("B", 1000, 0)
    m.capture_frames("B", 1000, 350)
    check(calls[0] == (0.0, False, True) and calls[1] == (350.0, True, False),
          "capture_frames: offset 0 -> press path, offset>0 -> grid path",
          f"calls={calls}")


def test_do_capture_end_now_uses_press_latency():
    """The end-now grab must place its window using press_latency_s, not the
    grid latency_s -- otherwise 'press-anchored' still carries the -99ms grid
    compensation and lands ~99ms early. Mutation: use latency_s for both -> the
    two window ends coincide and the delta collapses to 0."""
    m  = _mgr_with_fake_ring()
    fb = m.ring.channels * 2
    m._do_capture("B", 1000, offset_ms=0.0, phase_align=False, press=True)
    end_press = m._captured["end"]
    m._do_capture("B", 1000, offset_ms=0.0, phase_align=False, press=False)
    end_grid  = m._captured["end"]
    delta    = (end_press - end_grid) % m.ring.buffer_bytes
    expected = (int((0.0 - m.latency_s) * m.ring.rate) * fb) % m.ring.buffer_bytes
    check(delta == expected and end_press != end_grid,
          "end-now capture uses press_latency_s, not the grid latency_s",
          f"delta={delta} expected={expected} press={end_press} grid={end_grid}")


def _bare_trim_mgr(trim_path, status_path="/nonexistent"):
    """A SamplerManager with only the fields the trim methods touch."""
    m = SamplerManager.__new__(SamplerManager)
    m._press_trim_path      = trim_path
    m._skipback_status_path = status_path
    m._press_default_s      = 0.0
    m._press_by_source      = {}
    m.press_latency_s       = 0.0
    m.onset_snap            = True
    return m


def test_press_trim_per_source_persists_and_restores():
    """set_press_latency clamps <=0, stores the value against the CURRENT source
    key, persists the by_source dict, and a fresh manager restores it (a by-ear
    trim survives a synth restart). Mutation: drop the by_source write in
    set_press_latency -> restore reads an empty dict and the value is lost."""
    import tempfile, json as _json
    p = os.path.join(tempfile.mkdtemp(), "trim.json")
    m = _bare_trim_mgr(p)
    m.set_press_latency(-70)
    ok_set   = abs(m.press_latency_s - (-0.070)) < 1e-9
    saved    = _json.load(open(p))
    ok_saved = saved["by_source"]["__default__"] == -70.0
    m.set_press_latency(50)                       # positive -> clamped to 0
    ok_clamp = m.press_latency_s == 0.0
    m.set_press_latency(-70)
    m2 = _bare_trim_mgr(p)
    m2._restore_press_trim()
    ok_restore = abs(m2.press_latency_s - (-0.070)) < 1e-9
    check(ok_set and ok_saved and ok_clamp and ok_restore,
          "press trim per-source: clamps <=0, persists by_source, restores",
          f"set={ok_set} saved={ok_saved} clamp={ok_clamp} restore={ok_restore}")


def test_press_trim_keys_off_the_live_source():
    """Two capture sources keep independent trims, and _do_capture applies the
    CURRENT source's trim (keyed off the skipback status file's track-0 resolved
    ports). Mutation: make _do_capture use a fixed press_latency_s -> switching
    source stops changing the applied value."""
    import tempfile, json as _json
    d = tempfile.mkdtemp()
    status = os.path.join(d, "status.json")
    def _status(ports):
        with open(status, "w") as f:
            _json.dump({"tracks": [{"id": 0, "resolved": ports}]}, f)
    m = _mgr_with_fake_ring()
    m._press_trim_path      = os.path.join(d, "trim.json")
    m._skipback_status_path = status
    _status(["ORC1-Poly:out_L", "ORC1-Poly:out_R"])       # source A
    keyA = m._current_source_key()
    m.set_press_latency(-50)
    _status(["system:capture_13", "system:capture_14"])    # source B (L12 return)
    keyB = m._current_source_key()
    m.set_press_latency(-20)
    ok_keys  = keyA != keyB and "__default__" not in (keyA, keyB)
    ok_store = (m._press_by_source[keyA] == -50.0 and
                m._press_by_source[keyB] == -20.0)
    m._do_capture("B", 4800, offset_ms=0.0, press=True)    # source B live -> -20
    ok_applyB = abs(m.press_latency_s - (-0.020)) < 1e-9
    _status(["ORC1-Poly:out_L", "ORC1-Poly:out_R"])        # back to A -> -50
    m._do_capture("B", 4800, offset_ms=0.0, press=True)
    ok_switchA = abs(m.press_latency_s - (-0.050)) < 1e-9
    check(ok_keys and ok_store and ok_applyB and ok_switchA,
          "press trim is per-source: independent values, applied by live source",
          f"keys={ok_keys} store={ok_store} applyB={ok_applyB} switchA={ok_switchA}")


# ── PER-DECK GRAB SOURCE (2026-09-15) ─────────────────────────────────────────
# Each deck grabs from its own skipback ring, so deck A can sample the MAIN
# master (track 0) while deck B samples a different stream (track 1) at once.

class _SrcRing:
    """Ring stand-in with a distinct head position + size so a capture's computed
    window end reveals WHICH ring a deck actually read."""
    def __init__(self, secs, pos):
        self.rate         = SR
        self.channels     = 2
        self.bytes_per_s  = SR * 2 * 2
        self.buffer_bytes = self.bytes_per_s * secs
        self.tracks_path  = "/nonexistent"
        self._pos         = pos
    def refresh_geometry(self):  return False
    def head_info(self):         return (self._pos, 0)


def _deck_src_mgr(deck_src=None, rings=None, src_path=None, status="/nonexistent"):
    """Minimal SamplerManager exposing only the per-deck-source surface."""
    import tempfile
    m = SamplerManager.__new__(SamplerManager)        # skip __init__ (opens no ring)
    m.sr, m.max_seconds     = SR, 60
    m.latency_s             = -0.099
    m.press_latency_s       = 0.0
    m._press_default_s      = 0.0
    m._press_by_source      = {}
    m._skipback_status_path = status
    m.onset_snap            = False
    m.onset_snap_ms, m.onset_snap_sens = 120.0, 0.35
    m._seam_frames          = 0
    m.decks                 = {"A": object(), "B": object()}
    m._rings                = dict(rings or {0: _SrcRing(10, SR * 2)})
    m.ring                  = m._rings[0]
    m._deck_src             = dict(deck_src or {"A": 0, "B": 1})
    m._skipbuf_root         = "/nonexistent"
    m._deck_src_path        = src_path or os.path.join(tempfile.mkdtemp(), "decksrc.json")
    m._cap_meta             = {}                      # as __init__ sets it
    m._captured             = {}
    m._load_slice = (lambda deck, end, frames, gain=None, phase_ms=None:
                     m._captured.__setitem__(deck, int(end)))
    return m


def test_deck_source_defaults_set_clamp_persist_restore():
    """set_deck_source clamps 0-63, ignores junk, persists the deck->track map,
    and a fresh manager restores it. Mutation: drop the `_deck_src[deck]=tid`
    write -> the value never persists and restore keeps the boot default."""
    import tempfile, json as _json
    p = os.path.join(tempfile.mkdtemp(), "decksrc.json")
    m = _deck_src_mgr(src_path=p)
    ok_def = m._deck_src == {"A": 0, "B": 1}
    m.set_deck_source("B", 3)
    ok_set   = m._deck_src["B"] == 3
    m.set_deck_source("A", 99)                     # out of range -> ignored
    ok_range = m._deck_src["A"] == 0
    m.set_deck_source("B", "nope")                 # non-int -> ignored
    ok_junk  = m._deck_src["B"] == 3
    saved    = _json.load(open(p))
    ok_saved = int(saved["B"]) == 3 and int(saved["A"]) == 0
    m2 = _deck_src_mgr(src_path=p)
    m2._restore_deck_src()
    ok_rest  = m2._deck_src == {"A": 0, "B": 3}
    check(ok_def and ok_set and ok_range and ok_junk and ok_saved and ok_rest,
          "deck source: defaults A0/B1, clamps 0-63, persists + restores",
          f"def={ok_def} set={ok_set} range={ok_range} junk={ok_junk} "
          f"saved={ok_saved} rest={ok_rest}")


def test_ring_for_selects_per_deck():
    """_ring_for returns each deck's OWN ring, and repointing a deck follows.
    Mutation: make _ring_for ignore the deck -> both decks return one ring."""
    rA, rB = _SrcRing(10, 111), _SrcRing(20, 222)
    m = _deck_src_mgr(deck_src={"A": 0, "B": 1}, rings={0: rA, 1: rB})
    ok_split   = m._ring_for("A") is rA and m._ring_for("B") is rB
    m.set_deck_source("A", 1)                      # repoint A onto ring 1
    ok_repoint = m._ring_for("A") is rB
    check(ok_split and ok_repoint,
          "_ring_for gives each deck its own ring; repoint follows",
          f"split={ok_split} repoint={ok_repoint}")


def test_do_capture_reads_the_decks_ring():
    """Deck A on ring0, deck B on ring1 with a DIFFERENT head position: each
    deck's computed window end must reflect ITS ring. Mutation: _ring_for returns
    self.ring regardless of deck -> deck B's end collapses onto ring0."""
    rA, rB = _SrcRing(10, 100000), _SrcRing(10, 999999)
    m = _deck_src_mgr(deck_src={"A": 0, "B": 1}, rings={0: rA, 1: rB})
    m._do_capture("A", 4800, offset_ms=0.0, press=False)
    m._do_capture("B", 4800, offset_ms=0.0, press=False)
    fb = 4
    def _end(r):
        behind = int((0.0 - m.latency_s) * r.rate) * fb
        behind = max(behind, 0)
        behind -= behind % fb
        return (r._pos - behind) % r.buffer_bytes
    ok = (m._captured.get("A") == _end(rA) and m._captured.get("B") == _end(rB)
          and m._captured["A"] != m._captured["B"])
    check(ok, "_do_capture computes each deck's window against its own ring",
          f"A={m._captured.get('A')} B={m._captured.get('B')} "
          f"eA={_end(rA)} eB={_end(rB)}")


def test_current_source_key_follows_deck():
    """The per-source press-trim key follows the deck's grab source; deck=None
    keeps the historical track-0 behaviour. Mutation: hardcode id 0 -> deck B's
    key stops reflecting track 1's source and the two decks share a trim slot."""
    import tempfile, json as _json
    status = os.path.join(tempfile.mkdtemp(), "status.json")
    with open(status, "w") as f:
        _json.dump({"tracks": [
            {"id": 0, "resolved": ["system:capture_13", "system:capture_14"]},
            {"id": 1, "resolved": ["system:capture_3"]}]}, f)
    m = _deck_src_mgr(deck_src={"A": 0, "B": 1}, status=status)
    kA, kB, k0 = (m._current_source_key("A"),
                  m._current_source_key("B"),
                  m._current_source_key())
    ok = (kA == "system:capture_13,system:capture_14"
          and kB == "system:capture_3" and k0 == kA and kA != kB)
    check(ok, "_current_source_key follows the deck's source (None -> track 0)",
          f"kA={kA} kB={kB} k0={k0}")


def test_press_trim_migrates_legacy_flat_format():
    """A pre-2026-09-15 flat {"press_latency_ms": x} file loads into the
    '__default__' slot (an existing by-ear trim is preserved) and the file is
    upgraded in place to the by_source format."""
    import tempfile, json as _json
    p = os.path.join(tempfile.mkdtemp(), "trim.json")
    with open(p, "w") as f:
        _json.dump({"press_latency_ms": -29.0}, f)
    m = _bare_trim_mgr(p)
    m._restore_press_trim()
    ok_slot    = m._press_by_source.get("__default__") == -29.0
    ok_applied = abs(m.press_latency_s - (-0.029)) < 1e-9
    upgraded   = _json.load(open(p))
    ok_upgrade = "by_source" in upgraded and "press_latency_ms" not in upgraded
    check(ok_slot and ok_applied and ok_upgrade,
          "legacy flat press-trim migrates into __default__ and upgrades the file",
          f"slot={ok_slot} applied={ok_applied} upgrade={ok_upgrade}")


def test_press_anchor_bar_grab_wired_in_engine():
    with open(os.path.join(HERE, "orc1.py")) as f:
        src = f.read()
    check('_SMP_CAPTURE_ANCHOR != "grid"' in src
          and "def sampler_press_trim(" in src
          and 'action == "SAMPLER_PRESS_TRIM"' in src,
          "orc1.py: press-anchor branch + press-trim facade + cmd are wired",
          "expected press-anchor branch, sampler_press_trim, SAMPLER_PRESS_TRIM")


def test_press_latency_cc_wired_in_synth():
    with open(os.path.join(HERE, "orc1_synth.py")) as f:
        src = f.read()
    check("_SMP_PRESS_LAT" in src and "set_press_latency" in src,
          "orc1_synth.py: CC_PRESS_LATENCY decodes to set_press_latency",
          "expected _SMP_PRESS_LAT + set_press_latency in _route_sampler")


# ---------------------------------------------------------------------------
# Momentary BEAT-REPEAT (stutter) -- hold squeezes the play/loop region to a
# tight loop at the live playhead; release restores the prior region. It reuses
# set_bounds (clamp + playhead-pull + declick), so it is just a very short loop.
# ---------------------------------------------------------------------------

def test_deck_exposes_beat_repeat_controls():
    ok = all(hasattr(SampleDeck, m) for m in ("beat_repeat_on", "beat_repeat_off"))
    check(ok, "the deck exposes beat_repeat_on / beat_repeat_off",
          "expected momentary beat-repeat methods on SampleDeck")


def test_beat_repeat_squeezes_region_to_the_length_at_the_playhead():
    """Engage must set the region to [pos, pos+len] at the LIVE playhead. Read
    the region width against the requested length and the start against _pos."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    n = len(d._data)
    for _ in range(20):                              # advance the head
        d.render(BLOCK)
    pos_frac = d._pos / (n - 1)
    length = int(0.05 * SR)                          # ~2400 frames
    d.beat_repeat_on(length)
    si, ei = d._region_frames(n)
    width_frac = (ei - si) / (n - 1)
    check(abs(d._start_frac - pos_frac) < 0.01
          and abs(width_frac - length / (n - 1)) < 0.01,
          "beat_repeat_on squeezes the region to `length` at the live playhead",
          f"start {d._start_frac:.3f} vs pos {pos_frac:.3f}, width {width_frac:.4f} "
          f"vs len {length/(n-1):.4f}")


def test_beat_repeat_loops_only_within_the_slice():
    """The strong behavioural one: while held, a ramp deck must read ONLY inside
    the tight slice -- deleting the set_bounds() in beat_repeat_on would let the
    output sweep the whole buffer (0..1)."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))                    # value == position fraction
    n = len(d._data)
    for _ in range(25):
        d.render(BLOCK)
    pos_frac = d._pos / (n - 1)
    length = int(0.05 * SR)
    d.beat_repeat_on(length)
    lo_want = pos_frac
    hi_want = pos_frac + length / (n - 1)
    out = []
    for _ in range(80):                              # >> slice length: many wraps
        b = d.render(BLOCK)
        if b is not None:
            out.append(b)
    sig = np.concatenate(out[3:]) if len(out) > 3 else np.zeros(1)   # skip declick
    mn, mx = float(sig.min()), float(sig.max())
    check(mn >= lo_want - 0.06 and mx <= hi_want + 0.06,
          "beat-repeat loops only within the tight slice",
          f"observed {mn:.3f}..{mx:.3f} (want ~{lo_want:.3f}..{hi_want:.3f}; "
          f"the whole buffer would reach ~0..1)")


def test_beat_repeat_saves_and_restores_the_prior_region():
    """Release restores EXACTLY the region that was playing before -- a whittle,
    the full buffer, anything. Mutation: dropping the restore leaves the tight
    loop in place."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    d.set_bounds(0.3, 0.7)                            # a pre-existing (whittled) region
    for _ in range(6):
        d.render(BLOCK)
    d.beat_repeat_on(int(0.03 * SR))
    saved_ok = (d._br_saved is not None
                and abs(d._br_saved[0] - 0.3) < 0.01
                and abs(d._br_saved[1] - 0.7) < 0.01)
    tight_ok = (d._end_frac - d._start_frac) < 0.2   # squeezed while held
    d.beat_repeat_off()
    restored = abs(d._start_frac - 0.3) < 0.01 and abs(d._end_frac - 0.7) < 0.01
    check(saved_ok and tight_ok and restored and d._br_saved is None,
          "beat-repeat saves the prior region and restores it on release",
          f"saved={d._br_saved} tight={tight_ok} restored={restored} "
          f"region {d._start_frac:.3f}..{d._end_frac:.3f}")


def test_beat_repeat_re_press_keeps_the_original_region():
    """A second engage mid-hold (a different subdivision) must NOT overwrite the
    saved region with the current tight one. Mutation: removing the
    `if _br_saved is None` guard makes release restore the tight loop instead of
    the original 0.2..0.8."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    d.set_bounds(0.2, 0.8)
    for _ in range(6):
        d.render(BLOCK)
    d.beat_repeat_on(int(0.04 * SR))                 # first engage -> saves 0.2..0.8
    for _ in range(6):
        d.render(BLOCK)
    d.beat_repeat_on(int(0.02 * SR))                 # re-press with a shorter slice
    d.beat_repeat_off()
    check(abs(d._start_frac - 0.2) < 0.01 and abs(d._end_frac - 0.8) < 0.01,
          "a re-press keeps the original region for the release to restore",
          f"restored region {d._start_frac:.3f}..{d._end_frac:.3f} (want 0.20..0.80)")


def test_beat_repeat_off_without_engage_is_a_noop():
    """A stray release (never engaged) must not touch the region or raise."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    d.set_bounds(0.25, 0.75)
    before = (d._start_frac, d._end_frac)
    raised = False
    try:
        d.beat_repeat_off()
    except Exception:
        raised = True
    check(not raised and (d._start_frac, d._end_frac) == before and d._br_saved is None,
          "beat_repeat_off with nothing engaged is a clean no-op",
          f"raised={raised} region {d._start_frac:.3f}..{d._end_frac:.3f}")


def test_beat_repeat_on_empty_deck_is_a_noop():
    """No audio -> no region to squeeze; must not save state or raise."""
    d = SampleDeck("T", sample_rate=SR)
    raised = False
    try:
        d.beat_repeat_on(2400)
        d.beat_repeat_off()
    except Exception:
        raised = True
    check(not raised and d._br_saved is None,
          "beat-repeat on an empty deck stores nothing and does not raise",
          f"raised={raised} br_saved={d._br_saved}")


def test_capture_clears_a_held_beat_repeat():
    """A fresh capture while beat-repeat is held must drop _br_saved (the saved
    fractions point at the old buffer) and reset the region to full -- so the
    dangling release is a no-op. Mutation: dropping the reset line leaves
    _br_saved set across the new capture."""
    d = SampleDeck("T", sample_rate=SR)
    d.load_buffer(make_ramp(1.0))
    for _ in range(6):
        d.render(BLOCK)
    d.beat_repeat_on(int(0.05 * SR))
    held = d._br_saved is not None
    d.load_buffer(make_ramp(0.5))                    # fresh capture mid-hold
    check(held and d._br_saved is None
          and d._start_frac == 0.0 and d._end_frac == 1.0,
          "a fresh capture clears a held beat-repeat and resets the region",
          f"held={held} after br_saved={d._br_saved} "
          f"region {d._start_frac}..{d._end_frac}")


def test_beat_repeat_note_is_wired_into_the_synth_route():
    """Wiring net (oldest bug class): the BR note/length CCs must actually reach
    beat_repeat_on/off. Reads source (synth opens JACK)."""
    with open(os.path.join(HERE, "orc1_synth.py")) as f:
        src = f.read()
    check("_SMP_BR" in src and "_SMP_BR_LEN" in src
          and '"beat_repeat_on"' in src and '"beat_repeat_off"' in src,
          "the BR note + length CCs decode to beat_repeat_on/off in _route_sampler",
          "expected _SMP_BR / _SMP_BR_LEN -> beat_repeat_on/off in orc1_synth.py")


def test_beat_repeat_facade_and_cmd_are_wired():
    with open(os.path.join(HERE, "orc1.py")) as f:
        src = f.read()
    check("def sampler_beat_repeat_on(" in src
          and "def sampler_beat_repeat_off(" in src
          and '"SAMPLER_BR_ON"' in src and '"SAMPLER_BR_OFF"' in src
          and "_smp_send28(_SMP_BR_LEN_CC" in src
          and "_smp_note_on(_SMP_BR_NOTE" in src,
          "orc1.py: BR facades derive length + send CCs/note, and the cmds map to them",
          "expected sampler_beat_repeat_on/off + SAMPLER_BR_ON/OFF + length send in orc1.py")


# ---------------------------------------------------------------------------
# Onset-snap (2026-09-15): after a PRESS grab, refine the loop START onto the
# nearest strong transient. Pure detector tested directly; the ring read + the
# _do_capture wiring tested with fakes.
# ---------------------------------------------------------------------------

def _burst(sr, secs=0.030, hz=90.0, tau=0.008):
    """A short percussive transient: fast attack, quick exponential decay."""
    L = int(secs * sr)
    env = np.exp(-np.arange(L) / (tau * sr))
    return (0.8 * env * np.sin(2 * np.pi * hz * np.arange(L) / sr)).astype(np.float32)


def test_onset_index_locates_a_transient():
    """_onset_index finds a sharp transient offset from the naive start (within
    a few ms) so the loop can snap to the downbeat. Mutation: return None always
    (or return naive_idx) -> this fails."""
    from orc1_sampler import _onset_index
    R = int(0.120 * SR)                                    # radius; naive = R
    rng = np.random.default_rng(1)
    seg = (rng.standard_normal(2 * R) * 0.001).astype(np.float32)   # quiet floor
    onset = R - int(0.030 * SR)                            # 30 ms before naive
    b = _burst(SR)
    seg[onset:onset + len(b)] += b
    idx = _onset_index(seg, R, SR, 0.35)
    ok = idx is not None and abs(idx - onset) <= int(0.006 * SR)
    check(ok, "onset detector locates a transient near its true position",
          f"idx={idx} onset={onset} tol={int(0.006*SR)}")


def test_onset_index_ignores_a_pad():
    """A steady transient-free pad returns None -> the press start is left
    untouched (no snapping on sustained material). Mutation: drop the
    sensitivity gate -> envelope wobble gets snapped and this fails."""
    from orc1_sampler import _onset_index
    R = int(0.120 * SR)
    t = np.arange(2 * R) / SR
    seg = (0.5 * np.sin(2 * np.pi * 55.0 * t)).astype(np.float32)
    idx = _onset_index(seg, R, SR, 0.35)
    check(idx is None,
          "onset detector ignores a steady pad (no false snap)", f"idx={idx}")


def test_onset_index_biases_to_the_nearest_transient():
    """With two equal transients, the one NEARER the naive start wins, so a snap
    can only make a SMALL correction, never jump to a different beat. Mutation:
    drop the distance weighting -> the far transient can win and this fails."""
    from orc1_sampler import _onset_index
    R = int(0.120 * SR)
    seg = np.zeros(2 * R, dtype=np.float32)
    b = _burst(SR)
    near = R - int(0.020 * SR)                             # 20 ms from naive
    far  = R - int(0.100 * SR)                             # 100 ms from naive
    seg[near:near + len(b)] += b
    seg[far:far + len(b)]   += b
    idx = _onset_index(seg, R, SR, 0.35)
    ok = idx is not None and abs(idx - near) < abs(idx - far)
    check(ok, "onset detector prefers the transient nearest the naive start",
          f"idx={idx} near={near} far={far}")


def test_onset_shift_reads_ring_and_returns_byte_delta():
    """_onset_shift_bytes reads a segment around the start and returns a
    frame-aligned byte delta toward the transient. Mutation: return 0 -> the
    delta is lost and this fails."""
    class _SegRing:
        rate = SR
        channels = 2
        buffer_bytes = SR * 2 * 2 * 30
        def __init__(self, seg): self._seg = seg
        def snapshot_frames(self, n, mono=True, end_byte=None):
            return self._seg[:n]
    R = int(0.120 * SR)
    seg = np.zeros(2 * R, dtype=np.float32)
    onset = R - int(0.040 * SR)
    b = _burst(SR)
    seg[onset:onset + len(b)] += b
    m = SamplerManager.__new__(SamplerManager)
    m.ring            = _SegRing(seg)
    m.onset_snap_ms   = 120.0
    m.onset_snap_sens = 0.35
    fb = 4
    delta = m._onset_shift_bytes(1_000_000, fb)
    ok = (delta % fb == 0 and delta < 0
          and abs(delta // fb - (onset - R)) <= int(0.006 * SR))
    check(ok, "_onset_shift_bytes returns a frame-aligned delta toward the transient",
          f"delta_frames={delta // fb} want~{onset - R}")


def test_do_capture_applies_onset_snap_only_when_enabled():
    """_do_capture shifts the window end by the onset delta ONLY when onset_snap
    is on. Mutation: ignore self.onset_snap in _do_capture -> the OFF and ON
    window ends coincide and this fails."""
    m = _mgr_with_fake_ring()
    m.onset_snap = False
    m._do_capture("B", 4800, offset_ms=0.0, press=True)
    end_off = m._captured["end"]
    m.onset_snap = True
    m._onset_shift_bytes = lambda start_byte, fb, ring=None: -8 * fb  # force -8 frame snap
    m._do_capture("B", 4800, offset_ms=0.0, press=True)
    end_on = m._captured["end"]
    want = (end_off - 8 * (m.ring.channels * 2)) % m.ring.buffer_bytes
    check(end_on != end_off and end_on == want,
          "_do_capture applies the onset-snap shift only when enabled",
          f"off={end_off} on={end_on} want={want}")


class _WritingRing:
    """A mono ring that RECORDS in real time, like the skipback daemon: `pos`
    (the flushed head) advances with the wall clock from pos0. Bytes at/after
    `pos` have not been written THIS lap, so they read as the STALE marker --
    what the real ring holds there (audio from one ring-length ago, or zeros)."""
    STALE = 0.5
    def __init__(self, tape, pos0_frames, advance=True):
        self.rate, self.channels = SR, 1
        self.bytes_per_s  = SR * 2
        self.tape         = np.asarray(tape, dtype=np.float32)
        self.buffer_bytes = len(self.tape) * 2
        self.tracks_path  = "/nonexistent"
        self._pos0, self._t0, self._adv = int(pos0_frames), time.monotonic(), advance
    def refresh_geometry(self): return False
    def _pos_frames(self):
        dt = (time.monotonic() - self._t0) if self._adv else 0.0
        return min(len(self.tape) - 1, self._pos0 + int(dt * self.rate))
    def head_info(self): return (self._pos_frames() * 2, 0)
    def snapshot_frames(self, n, mono=True, end_byte=None):
        end = (self._pos_frames() if end_byte is None else int(end_byte) // 2)
        out = self.tape[max(0, end - n):end].copy()
        pos = self._pos_frames()
        first = end - len(out)
        if pos < end:                                  # unwritten this lap
            out[max(0, pos - first):] = self.STALE
        return out


def _later_snap_rig(advance=True, early_ms=60.0):
    """Press EARLY: the real downbeat is `early_ms` AFTER the naive loop start,
    so onset-snap must move the window LATER -- past where the recorder has
    written. Returns (manager, ring, frames, onset_frame, captured)."""
    frames = SR                                        # a 1 s loop
    pos0   = 3 * SR
    tape   = np.zeros(4 * SR, dtype=np.float32)
    seam   = int(0.015 * SR)
    naive_start = pos0 - seam - frames                 # press trim 0 -> end = pos - seam
    onset  = naive_start + int(early_ms / 1000.0 * SR)
    b = _burst(SR)
    tape[onset:onset + len(b)] += b
    ring = _WritingRing(tape, pos0, advance=advance)
    m = _deck_src_mgr(deck_src={"A": 0, "B": 0}, rings={0: ring})
    m._seam_frames = seam
    m.onset_snap   = True
    got = {}
    def _grab(deck, end, frames, gain=None, phase_ms=None):
        # read EXACTLY what the real _load_slice reads: the window + seam pad
        got["end"] = int(end)
        got["buf"] = ring.snapshot_frames(frames + seam, end_byte=int(end) + seam * 2)
    m._load_slice = _grab
    return m, ring, frames, onset, got


def test_later_onset_snap_never_reads_unwritten_ring():
    """A LATER snap (press a hair before the downbeat -- the common case) moves the
    window end past the flushed head `pos`: the loop's tail + seam blend would be
    audio from one ring-lap ago. The capture must wait for the recorder (bounded),
    then read only written audio -- and still start ON the transient.
    Mutation: drop the wait in _do_capture -> STALE bytes land in the loop."""
    m, ring, frames, onset, got = _later_snap_rig(advance=True)
    m._do_capture("A", frames, offset_ms=0.0, press=True)
    buf = got.get("buf")
    stale = int(np.sum(np.isclose(buf, ring.STALE))) if buf is not None else -1
    check(buf is not None and stale == 0,
          "later onset-snap: loop never contains unwritten (stale-lap) ring audio",
          f"stale samples in loop+seam = {stale}")
    start = got["end"] // 2 - frames
    check(abs(start - onset) <= int(0.006 * SR),
          "later onset-snap: the loop still starts ON the transient",
          f"start={start} onset={onset} diff_ms={(start - onset) / SR * 1000:.1f}")


def test_later_onset_snap_with_stalled_recorder_falls_back_safely():
    """If the recorder never advances (daemon down), the wait must give up
    (bounded) and pull the end back to written audio rather than read stale bytes
    or hang the MIDI thread. Mutation: drop the fallback -> stale bytes."""
    m, ring, frames, onset, got = _later_snap_rig(advance=False)
    t0 = time.monotonic()
    m._do_capture("A", frames, offset_ms=0.0, press=True)
    took = time.monotonic() - t0
    buf = got.get("buf")
    stale = int(np.sum(np.isclose(buf, ring.STALE))) if buf is not None else -1
    check(buf is not None and stale == 0 and took < 1.0,
          "stalled recorder: later snap falls back to written audio, bounded wait",
          f"stale={stale} took={took:.2f}s")


def test_earlier_onset_snap_does_not_wait():
    """An EARLIER snap is already inside written audio -- it must not pay the
    wait. Guards against a fix that sleeps on every capture."""
    m, ring, frames, onset, got = _later_snap_rig(advance=True, early_ms=-60.0)
    t0 = time.monotonic()
    m._do_capture("A", frames, offset_ms=0.0, press=True)
    took = time.monotonic() - t0
    start = got["end"] // 2 - frames
    check(took < 0.05 and abs(start - onset) <= int(0.006 * SR),
          "earlier onset-snap: snaps back onto the transient with no wait",
          f"took={took * 1000:.0f}ms start={start} onset={onset}")


def test_switching_deck_source_keeps_nudging_the_loops_own_ring():
    """A loaded loop's capture window lives in the ring it was GRABBED from
    (_cap_meta "tid"). With per-deck ring buttons you switch sources freely, so
    after the deck is pointed at ANOTHER ring a nudge must re-slice the loop's
    OWN ring, from that ring's head -- never the new ring at a meaningless
    offset (random audio from the other stream), and never go dead. A window
    with no "tid" gets the OLD source pinned on it at the switch.
    Mutation: nudge using the deck's current source instead of meta "tid" ->
    the tid/end checks fail."""
    rA, rB = _SrcRing(10, SR * 4 * 5), _SrcRing(8, SR * 4 * 3)
    m = _deck_src_mgr(deck_src={"A": 0, "B": 1}, rings={0: rA, 1: rB})
    class _D:
        def loaded(self): return True
    m.decks = {"A": _D(), "B": _D()}
    m._seam_frames = 0
    m._cap_meta = {"A": {"end_byte": SR * 4 * 4, "frames": SR, "gain": 1.0, "tid": 0},
                   "B": {"end_byte": SR * 4 * 2, "frames": SR, "gain": 1.0}}
    sliced = []
    m._load_slice = (lambda deck, end, frames, gain=None, phase_ms=None,
                     preserve=False, tid=None: sliced.append((deck, int(end), tid)))
    m.set_deck_source("A", 1)                          # A now grabs ring 1
    m.nudge("A", 20)                                   # ...but its loop is in ring 0
    fb = rA.channels * 2
    want_end = (SR * 4 * 4 + int(round(0.020 * SR)) * fb) % rA.buffer_bytes
    ok_a = sliced[:1] == [("A", want_end, 0)]
    m.set_deck_source("B", 0)                          # legacy window, no tid
    ok_pin = m._cap_meta["B"].get("tid") == 1
    check(ok_a and ok_pin,
          "after a source switch, nudge re-slices the LOOP's own ring from its "
          "own head; a tid-less window is pinned to the old ring",
          f"sliced={sliced} want={('A', want_end, 0)} B.tid={m._cap_meta['B'].get('tid')}")


def test_onset_snap_wired_in_synth_and_engine():
    with open(os.path.join(HERE, "orc1_synth.py")) as f:
        s = f.read()
    with open(os.path.join(HERE, "orc1.py")) as f:
        b = f.read()
    ok = ("_SMP_ONSET" in s and "set_onset_snap" in s
          and "def sampler_onset_snap(" in b
          and 'action == "SAMPLER_ONSET_SNAP"' in b)
    check(ok, "onset-snap: CC decodes in synth, facade + cmd wired in engine",
          f"synth={'_SMP_ONSET' in s and 'set_onset_snap' in s} "
          f"engine={'def sampler_onset_snap(' in b}")


def test_capture_anchor_toggle_wired_in_engine():
    with open(os.path.join(HERE, "orc1.py")) as f:
        b = f.read()
    ok = ("def sampler_set_anchor(" in b
          and 'action == "SAMPLER_ANCHOR"' in b
          and "global _SMP_CAPTURE_ANCHOR" in b)
    check(ok, "capture-anchor PRESS/GRID toggle: facade + cmd wired in engine",
          "expected sampler_set_anchor + SAMPLER_ANCHOR + global _SMP_CAPTURE_ANCHOR")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    verbose = "-v" in sys.argv
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception:
            import traceback
            RESULTS.append((False, f"{t.__name__} RAISED",
                            traceback.format_exc().strip().splitlines()[-1]))
    npass = sum(1 for ok, _, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    for ok, label, detail in RESULTS:
        if ok and not verbose:
            continue
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")
    print(f"\n  {npass} passed, {nfail} failed  ({len(RESULTS)} checks)")
    if len(RESULTS) != EXPECTED:
        print(f"  COUNT DRIFT: {len(RESULTS)} checks ran, expected {EXPECTED}")
        return 1
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
