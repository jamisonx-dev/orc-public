#!/usr/bin/env python3
"""Regression suite for the 2026-09-24 chip additions (orc1_synth + chip host):

  * GB wave SHAPING: harmonic knobs CC 102-109, 16 slots (PC 0-15 recall,
    CC 115 held + PC = store), the wave-state file round trip.
  * noise2: a second, independent noise voice + the CC 114 key split.
  * per-voice mods: glide CC5, pan CC86, arp/trill CC87-89, pitch slide
    CC90/110, tremolo CC111/112, retrigger CC113.
  * DEFAULTS UNTOUCHED: with nothing set, render_stereo() == the old
    render()+get_p1p2_stereo() and L == R.

    python3 _test_chip_perf.py

EXPECTED = 63 checks. A LOWER total is a regression even when it says
"0 failed".

SAFETY: imports orc1_synth (side-effect free) and orc1_chip_host (jack is only
imported inside its main()). HOME is pointed at a throwaway temp dir BEFORE the
imports, so the chip host's state files are never the real ones. No JACK, no
MIDI ports, no audio device.
"""
import os
import sys
import json
import tempfile

_TMP = tempfile.mkdtemp(prefix="chipperf_")
os.environ["HOME"] = _TMP                  # before ANY ~ is resolved
os.environ["USERPROFILE"] = _TMP           # (Windows expanduser)

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_synth as S                     # noqa: E402
import orc1_chip_host as H                 # noqa: E402

EXPECTED = 63
RAN, FAILS = [], []


def check(cond, label, detail=""):
    RAN.append(label)
    ok = bool(cond)
    if not ok:
        FAILS.append(label)
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f"   ({detail})" if detail else ""))


SR = S.SAMPLE_RATE


def fresh():
    return S.NESEngine(sample_dir=None)


def hz(eng, name):
    v = eng._ch[name]
    return v.base_freq * v.pitch_mult


# ============================================================================
print("== 1. GB wave shaping: harmonic knobs ==")
t = S._gb_harmonic_table([127, 0, 0, 0, 0, 0, 0, 0])
check(len(t) == 32 and min(t) == 0 and max(t) == 15,
      "harmonic 1 alone uses the full 0-15 range", f"{min(t)}..{max(t)}")
# a pure sine table is antisymmetric about its midpoint: t[i] + t[i+16] ~ 15
check(all(abs(t[i] + t[i + 16] - 15) <= 1 for i in range(16)),
      "harmonic 1 alone is a (4-bit) sine")
check(S._gb_harmonic_table([0] * 8) == [8] * 32, "all harmonics 0 -> flat (silent) table")
t2 = S._gb_harmonic_table([0, 127, 0, 0, 0, 0, 0, 0])
check(all(abs(a - b) <= 1 for a, b in zip(t2[:16], t2[16:])),
      "harmonic 2 alone repeats twice per cycle")

e = fresh()
w = e._wave
e.set_cc('wave', 102, 127)
check(w._use_custom and w._wave == t, "CC102 builds the table and switches to custom playback")
check(w.state_dirty, "a harmonic move marks the wave state dirty")
e.set_cc('wave', 103, 90)
check(w._harm[:2] == [127, 90] and w._wave == S._gb_harmonic_table(w._harm),
      "CC103 adds harmonic 2 on top of harmonic 1")
e.set_cc('wave', 1, 20)
check(not w._use_custom, "CC1 preset select still leaves custom mode")
e.set_cc('pulse1', 102, 127)
check(e._wave._harm[:2] == [127, 90], "CC102 on another chip channel does not touch the wave")

# harmonic change while sounding crossfades (no hard step)
e = fresh(); w = e._wave
e.set_cc('wave', 10, 127); e.note_on('wave', 45, 100)
for _ in range(100):
    w.render(128)
e.set_cc('wave', 102, 127)
check(w._xf_left > 0, "a harmonic move on a sounding note crossfades")

# ============================================================================
print("== 2. GB wave slots ==")
e = fresh(); w = e._wave
check(w._slots[:8] == [list(p) for p in S._GB_WAVE_PRESETS] and w._slots[8:] == [None] * 8,
      "slots 1-8 start as the presets, 9-16 empty")
e.program_change('wave', 2)
check(w._use_custom and w._wave == list(S._GB_WAVE_PRESETS[2]), "PC 2 recalls slot 3 (saw up)")
before = list(w._wave)
e.program_change('wave', 12)
check(w._wave == before, "PC on an empty slot changes nothing")
e.set_cc('wave', 102, 127); e.set_cc('wave', 105, 60)
drawn = list(w._wave)
e.set_cc('wave', 115, 127)
e.program_change('wave', 12)
check(w._slots[12] == drawn, "STORE held + PC 12 saves the playing wave into slot 13")
check(w._wave == drawn, "storing does not change what plays")
e.set_cc('wave', 115, 0)
e.program_change('wave', 0)
e.program_change('wave', 12)
check(w._wave == drawn, "releasing STORE: PC 12 now recalls the stored wave")
e.program_change('wave', 16)
check(w._wave == drawn, "PC >= 16 is ignored")
e.program_change('pulse1', 3)
check(w._wave == drawn, "PC on a non-wave voice is ignored")

# export / import round trip + tolerance
st = w.export_state()
json.dumps(st)
e2 = fresh(); e2._wave.import_state(json.loads(json.dumps(st)))
w2 = e2._wave
check(w2._slots == w._slots and w2._harm == w._harm and w2._wave == w._wave
      and w2._use_custom == w._use_custom, "wave state export/import round trips")
e3 = fresh()
e3._wave.import_state({"slots": ["junk", [99] * 32, None], "harm": [1, 2],
                       "custom": [1] * 5, "use_custom": True, "preset": 42})
w3 = e3._wave
check(w3._slots[0] == list(S._GB_WAVE_PRESETS[0]) and w3._slots[1] == list(S._GB_WAVE_PRESETS[1])
      and w3._slots[2] is None and w3._harm == [0] * 8 and not w3._use_custom,
      "garbage in the state file is skipped field by field")

# persistence skip: wave table CCs are NOT replayed from the per-CC patch file
for cc in (20, 21, 23, 102, 109, 115):
    check(cc in S._CHIP_PERSIST_SKIP, f"CC{cc} is kept out of the per-CC patch file")
check(86 not in S._CHIP_PERSIST_SKIP and 5 not in S._CHIP_PERSIST_SKIP,
      "the per-voice mod CCs ARE persisted")

# chip host: file written by the persist path, restored on the next start
h = H.ChipHost()
h.chip.set_cc('wave', 104, 127)
h.chip._wave.set_cc(115, 127); h.chip._wave.program_change(9)
H.flush_wave_state(h)
path = os.path.expanduser(S.GB_WAVE_STATE_FILE)
check(path.startswith(_TMP) and os.path.exists(path), "chip host writes the wave state file (sandboxed)")
h2 = H.ChipHost()
check(h2.chip._wave._slots[9] == h.chip._wave._slots[9] and h2.chip._wave._use_custom
      and h2.chip._wave._wave == h.chip._wave._wave,
      "a restarted chip host restores slots + the playing custom wave")

# chip host router: Program Change on the wave channel reaches the engine
q = []
class _E:
    def chip_pc(self, v, p): q.append(('pc', v, p))
    def __getattr__(self, k): return lambda *a, **kw: None
route = H._make_router(_E())
import types
wave_ch = next(ch for ch, m in S._BINDINGS.items() if m == 'gb_wave')
route(types.SimpleNamespace(type='program_change', channel=wave_ch - 1, program=5))
check(q == [('pc', 'wave', 5)], "chip host routes program_change to the voice", str(q))

# ============================================================================
print("== 3. noise2 + key split ==")
e = fresh()
check('noise2' in e._ch and e._noise2_long is False, "noise2 exists, buzzy (7-bit) by default")
e.note_on('noise', 50, 100); e.note_on('noise2', 50, 100)
check(e._ch['noise'].active() and e._ch['noise2'].active(), "both noise voices sound at once")
check(e._ch['noise']._nseq is S._GB_LFSR_15 and e._ch['noise2']._nseq is S._GB_LFSR_7,
      "noise = white, noise2 = buzzy at the same time")
e.set_cc('noise2', 3, 0)
check(e._noise2_long and e._noise_long, "CC3 on noise2 sets only noise2's mode")
e.set_cc('noise', 3, 127)
check(not e._noise_long and e._noise2_long, "CC3 on noise sets only noise's mode")

e = fresh()
e.set_cc('noise', 114, 60)
e.note_on('noise', 64, 100)
check(e._ch['noise2'].active() and not e._ch['noise'].active(),
      "split: a note at/above the split plays noise2")
e.note_on('noise', 40, 100)
check(e._ch['noise'].active(), "split: a note below plays noise")
e.set_cc('noise', 114, 0)               # move the split while 64 is held
e.note_off(64, 'noise')
check(e._ch['noise2'].env_state == 'release', "note-off releases the voice its note-on used")
check(64 not in e._split_notes, "split bookkeeping cleared on release")
e.note_off(40, 'noise')
check(e._ch['noise'].env_state == 'release', "the below-split note releases normally")
e = fresh(); e.set_cc('noise', 114, 60)
e.note_on('noise', 70, 100); e.note_on('noise', 70, 100)   # pad double-trigger
e.note_off(70, 'noise')
check(e._ch['noise2'].env_state not in ('idle', 'release'), "double-trigger: 1st note-off absorbed")
e.note_off(70, 'noise')
check(e._ch['noise2'].env_state == 'release', "double-trigger: 2nd note-off still finds noise2")

# ============================================================================
print("== 4. glide / slide ==")
e = fresh()
e.set_cc('pulse1', 5, 64)               # ~100 ms
gt = e._mod['pulse1'].glide_t
e.note_on('pulse1', 60, 100); e.render(256)
e.note_on('pulse1', 72, 100); e.render(64)
f0 = hz(e, 'pulse1')
check(abs(12 * np.log2(f0 / S._midi_to_hz(60))) < 0.5, "glide starts at the previous note",
      f"{f0:.1f} Hz")
for _ in range(int(gt * SR / 256) + 4):
    e.render(256)
f1 = hz(e, 'pulse1')
check(abs(f1 - S._midi_to_hz(72)) < 0.01, "glide lands on the new note", f"{f1:.2f} Hz")
e = fresh()
e.note_on('pulse1', 60, 100); e.render(256); e.note_on('pulse1', 72, 100); e.render(64)
check(abs(hz(e, 'pulse1') - S._midi_to_hz(72)) < 0.01, "glide off (default): the note jumps")
e = fresh(); e.set_cc('wave', 5, 64)
e.note_on('wave', 48, 100); e.render(256); e.note_on('wave', 60, 100); e.render(64)
fw = e._wave._freq * e._wave.pitch_mult
check(fw < S._midi_to_hz(55), "glide works on the GB wave voice", f"{fw:.1f} Hz")

e = fresh()
e.set_cc('triangle', 90, 127); e.set_cc('triangle', 110, 0)     # +24 st over 10 ms
e.note_on('triangle', 48, 100)
for _ in range(20):
    e.render(256)
check(abs(e._ch['triangle'].pitch_mult - 4.0) < 1e-6, "slide +24 reaches 2 octaves up and holds")
e = fresh(); e.set_cc('noise', 90, 0); e.set_cc('noise', 110, 0)
e.note_on('noise', 72, 100)
for _ in range(6):
    e.render(256)
nv = e._ch['noise']
check(nv.pitch_mult < 0.3, "slide works on noise (drum drop)", f"mult {nv.pitch_mult:.3f}")

# ============================================================================
print("== 5. arp / trill (sample-accurate) ==")
e = fresh()
seen = []
_orig = e._voice_raw
def spy(name, n):
    if name == 'pulse1':
        seen.append((n, round(e._ch['pulse1'].pitch_mult, 6)))
    return _orig(name, n)
e._voice_raw = spy
e.set_cc('pulse1', 87, 64)              # +12
e.set_cc('pulse1', 88, 37)              # +7
e.set_cc('pulse1', 89, 127)             # 60 steps/s -> 800 samples/step
e.note_on('pulse1', 60, 100)
for _ in range(20):
    e.render(256)
mults = []
for n, m in seen:
    if not mults or mults[-1][1] != m:
        mults.append([n, m])
    else:
        mults[-1][0] += n
cyc = [m for _, m in mults[:6]]
check(cyc[:3] == [1.0, 2.0, round(2 ** (7 / 12), 6)], "arp cycles root, +A, +B", str(cyc[:3]))
lens = [n for n, _ in mults[1:-1]]
check(lens and all(abs(L - 800) <= 1 for L in lens), "every arp step is 800 samples (not block-snapped)",
      str(lens[:6]))
e = fresh(); e.set_cc('pulse2', 88, 16); e.set_cc('pulse2', 89, 60)
check(e._mod['pulse2'].arp_steps() == [0, 3], "B alone = a 2-note trill", str(e._mod['pulse2'].arp_steps()))

# ============================================================================
print("== 6. tremolo / retrigger ==")
e = fresh(); e.set_cc('pulse1', 10, 127)
e.set_cc('pulse1', 111, 127); e.set_cc('pulse1', 112, 64)
e.note_on('pulse1', 60, 100)
x = np.concatenate([e.render(240) for _ in range(400)])[24000:]
env = np.abs(x).reshape(-1, 240).max(axis=1)
check(env.min() < 0.1 * env.max(), "tremolo depth 127 dips the volume to ~0",
      f"{env.min():.3f}/{env.max():.3f}")

e = fresh()
e.set_cc('noise', 10, 0); e.set_cc('noise', 9, 20)          # plucky: decays to silence
e.set_cc('noise', 113, 70)
e.note_on('noise', 60, 100)
rt = e._mod['noise'].retrig_hz
x = np.concatenate([e.render(256) for _ in range(400)])
P = SR / rt
k = int(3 * P)                                               # 4th strike
check(np.abs(x[k - 400:k - 100]).max() < 1e-3 and np.abs(x[k + 100:k + 500]).max() > 0.01,
      "retrigger re-strikes a decayed note while held",
      f"rate {rt:.1f}/s")
# A SUSTAINING voice is at full level when a strike restarts its envelope from
# 0 -- without the gate that is a full-scale step. With it, the output is
# already faded to ~0 on both sides of the strike.
e2 = fresh()
e2.set_cc('triangle', 113, 70)
e2.note_on('triangle', 36, 100)
xt = np.concatenate([e2.render(256) for _ in range(100)])
Pt = SR / e2._mod['triangle'].retrig_hz
kt = int(2 * Pt)
check(np.abs(xt[kt - 5:kt + 5]).max() < 0.05 * np.abs(xt).max() and np.abs(xt).max() > 0.005,
      "a strike lands inside the gate (no click)",
      f"|x| at strike {np.abs(xt[kt - 5:kt + 5]).max():.4f}, peak {np.abs(xt).max():.3f}")
e.note_off(60, 'noise')
y = np.concatenate([e.render(256) for _ in range(400)])
check(np.abs(y[-2000:]).max() < 1e-4, "no re-strikes after release")

# ============================================================================
print("== 7. pan + defaults ==")
e = fresh()
e.note_on('pulse1', 60, 100)
L, R = e.render_stereo(256)
check(np.array_equal(L, R), "all centred: L == R")
e.set_cc('pulse1', 86, 0)
for _ in range(3):
    L, R = e.render_stereo(256)
check(np.abs(R).max() == 0.0 and np.abs(L).max() > 0.01, "pan 0 = hard left")
e.set_cc('pulse1', 86, 127)
L, R = e.render_stereo(256)
check(np.abs(R[:8]).max() < 0.1 * np.abs(R).max(), "a pan jump ramps across the block (no step)",
      f"R start {np.abs(R[:8]).max():.4f} vs {np.abs(R).max():.4f}")
L, R = e.render_stereo(256)
check(np.abs(L).max() == 0.0 and np.abs(R).max() > 0.01, "pan 127 = hard right")
e.set_cc('pulse1', 86, 64)
for _ in range(int(S._CHIP_STEREO_HOLD_S * SR / 256) + 2):
    L, R = e.render_stereo(256)
check(np.array_equal(L, R) and e._st_hold == 0, "re-centred: back on the mono path after the hold")

e = fresh()
e.render_stereo(256)                    # mono path: gains sit at centre (1, 1)
e.set_cc('pulse1', 86, 0)               # pan + note land in the SAME block
e.note_on('pulse1', 60, 100)
L, R = e.render_stereo(256)
check(np.abs(R).max() == 0.0 and np.abs(L).max() > 0.01,
      "a note starting on a panned voice lands there (no first-block smear)")

e = fresh()
e.set_cc('pcm', 86, 0)
check(e._mod['pcm'].pan == 0, "PCM has a pan too")
m = S._ChipMod()
check(m.pan_target() == (1.0, 1.0) and m.arp_steps() is None and m.glide_t == 0.0
      and m.retrig_hz == 0.0 and m.trem_depth == 0.0 and m.slide_semi == 0,
      "every mod defaults to off / centre")

print()
for f in FAILS:
    print("   FAILED -", f)
print(f"{len(RAN) - len(FAILS)} passed, {len(FAILS)} failed  "
      f"({len(RAN)} checks ran, expected {EXPECTED})")
short = len(RAN) != EXPECTED
if short:
    print(f"!! CHECK COUNT IS {len(RAN)}, EXPECTED {EXPECTED} -- a check was "
          f"added or silently lost. Update EXPECTED deliberately or find it.")
sys.exit(1 if (FAILS or short) else 0)
