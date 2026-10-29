#!/usr/bin/env python3
"""Poly voice STEAL clicks (orc1_synth.PolyVoice.note_on, 2026-10-19).

    python3 _test_poly_steal.py [-v]

owner heard pops "most when I play quickly" on the poly, ring mod on
(depth 92, ratio 2.0) and a long release (CC11 117). Playing fast with a long
release fills every voice with a fading note, so a new note STEALS one that is
still sounding. note_on then reset the ring-mod carrier to phase 0 and swapped
the velocity in one sample: a gain step in the middle of an audible sound.

Rendered offline with his patch, block-split exactly at each note-on like the
host's segmented event loop. A click = the jump across a note-on boundary
compared with the largest sample-to-sample step in the audio around it. The
patch's triangle wave has no jumps of its own, so the signal's own steps are
small and an added discontinuity stands out.

Pure numpy; imports orc1_synth (no JACK, no MIDI, no files).
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S                                      # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 8

# owner's live poly patch (~/.orc1_poly_params.json, 2026-10-19)
HIS_PATCH = {1: 100, 8: 19, 9: 10, 10: 111, 11: 117, 12: 92, 14: 68, 15: 0}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def synth(patch):
    p = S.PolySynth()
    for cc, v in patch.items():
        p.set_cc(cc, v)
    return p


def play(p, events, total):
    """events: [(sample, 'on'|'off', note, vel)], rendered split AT each event
    (the host's sample-accurate segmented loop). Returns (signal, [on idx])."""
    out, t, ons = [], 0, []
    for at, kind, note, vel in sorted(events, key=lambda e: e[0]):
        if at > t:
            out.append(p.render(at - t))
            t = at
        if kind == "on":
            ons.append(at)
            p.note_on(note, vel)
        else:
            p.note_off(note)
    out.append(p.render(total - t))
    return np.concatenate(out), ons


def fast_run(patch, n_notes=24, gap=2400, hold=1800):
    """Quick playing: a note every 50 ms, held 37 ms, velocities alternating."""
    ev = []
    for i in range(n_notes):
        note = 48 + (i * 7) % 24
        vel = 120 if i % 2 else 50
        ev.append((i * gap, "on", note, vel))
        ev.append((i * gap + hold, "off", note, 0))
    return play(synth(patch), ev, n_notes * gap + 4800)


def boundary_ratio(y, k, win=240):
    """Jump INTO sample k vs the biggest step in the audio around it."""
    jump = abs(float(y[k]) - float(y[k - 1]))
    around = np.concatenate((np.diff(y[k - win:k - 1]), np.diff(y[k + 1:k + win])))
    ref = float(np.max(np.abs(around))) + 1e-6
    return jump / ref, jump


def worst_steal(patch):
    y, ons = fast_run(patch)
    steals = ons[6:]                    # 6 voices: from the 7th note on, all steal
    rs = [boundary_ratio(y, k) for k in steals]
    return max(r for r, _j in rs), max(j for _r, j in rs)


def test_steals_click_free_with_ring_mod():
    r, j = worst_steal(HIS_PATCH)
    check("his patch (ring mod 92 / ratio 2.0, long release), fast playing: "
          "no note-on jump > 2x the audio's own steps", r < 2.0,
          "worst ratio %.1f (jump %.3f)" % (r, j))


def test_steals_click_free_without_ring_mod():
    patch = {**HIS_PATCH, 12: 0}
    r, j = worst_steal(patch)
    check("same playing, ring mod OFF: no note-on jump either (velocity swap)",
          r < 2.0, "worst ratio %.1f (jump %.3f)" % (r, j))


def test_fixed_carrier_too():
    patch = {**HIS_PATCH, 15: 60}        # untracked carrier
    r, j = worst_steal(patch)
    check("ring mod on a FIXED carrier (CC15): no steal click", r < 2.0,
          "worst ratio %.1f (jump %.3f)" % (r, j))


def test_fresh_note_still_starts_at_carrier_phase_0():
    """The reset exists so a note from SILENCE always sounds the same: keep it."""
    p = synth(HIS_PATCH)
    p.note_on(60, 100)
    a = p.render(4000)
    p.note_off(60)
    p.render(48000 * 8)                         # long release: let it go idle
    check("voice is idle again before the repeat",
          all(not v.active() for v in p.voices))
    p.note_on(60, 100)
    b = p.render(4000)
    # oscillator phase is free-running, so compare the carrier (env x rm part)
    va = p.voices[0]
    check("a note from silence restarts the carrier at phase 0",
          abs(va.rm_phase - ((S._midi_to_hz(60) * 2.0 / va.sr * 4000) % 1.0)) < 1e-6,
          va.rm_phase)


def test_stolen_voice_keeps_carrier_running():
    p = synth(HIS_PATCH)
    p.note_on(60, 100)
    p.render(3000)
    v = p.voices[0]
    before = v.rm_phase
    v.note_on(64, 100, p.waveform, p.attack, p.decay, p.sustain, p.release, 99)
    check("re-triggering a SOUNDING voice keeps its carrier phase (no jump)",
          v.rm_phase == before, (before, v.rm_phase))


def test_velocity_glides_on_a_steal():
    p = synth({**HIS_PATCH, 12: 0})
    p.note_on(60, 20)
    p.render(3000)
    v = p.voices[0]
    v.note_on(60, 127, p.waveform, p.attack, p.decay, p.sustain, p.release, 99)
    g = np.atleast_1d(v._vel_gain(400)).astype(float)
    # starts at ~the OLD velocity (20/127), ends at the new one (1.0)
    check("a steal glides velocity old -> new instead of stepping",
          v.velocity == 1.0 and g[0] < 0.2 and abs(g[-1] - 1.0) < 1e-6,
          (v.velocity, g[0], g[-1]))


if __name__ == "__main__":
    for t in (test_steals_click_free_with_ring_mod,
              test_steals_click_free_without_ring_mod, test_fixed_carrier_too,
              test_fresh_note_still_starts_at_carrier_phase_0,
              test_stolen_voice_keeps_carrier_running,
              test_velocity_glides_on_a_steal):
        try:
            t()
        except Exception as e:
            check(t.__name__ + " raised", False, repr(e))
    check("every check ran (%d expected)" % EXPECTED,
          PASS + FAIL + 1 == EXPECTED, "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
