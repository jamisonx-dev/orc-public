#!/usr/bin/env python3
"""BeatBuddy hit timing + double-hit suppression (2026-10-19).

owner heard double hits on BeatBuddy transitions (909 output). Measured cause:
the mind places each hit part-way into a clock pulse (`delay`, in player ticks)
and the brain's emit dropped it, so flams/rolls in a fill landed on one instant;
some factory songs also layer the same note twice on one tick. Pure stdlib; the
orc1.py wiring is checked at source level (orc1.py cannot be imported).
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_beatbuddy as bb

_p = _f = 0
def check(ok, name, detail=""):
    global _p, _f
    if ok:
        _p += 1; print("  ok   ", name)
    else:
        _f += 1; print("  FAIL ", name, detail)

# --- hit_delay_seconds -----------------------------------------------------
P = 0.020   # 20 ms pulse
check(bb.hit_delay_seconds(0, P) == 0.0, "delay 0 ticks = at the pulse edge")
check(abs(bb.hit_delay_seconds(10, P) - 0.010) < 1e-9, "delay 10 of 20 ticks = half a pulse")
check(abs(bb.hit_delay_seconds(18, P) - 0.018) < 1e-9, "delay 18 ticks = 18 ms at a 20 ms pulse")
check(bb.hit_delay_seconds(-5, P) == 0.0, "negative delay (pickup) never goes early")
check(bb.hit_delay_seconds(10_000, P) <= 4 * P + 1e-9, "absurd delay is capped (4 pulses)")
check(bb.hit_delay_seconds(10, 0) == 0.0, "zero pulse length is safe")

# --- PulseDeduper ----------------------------------------------------------
d = bb.PulseDeduper()
check(d.seen(36, 0.0) is False, "first hit of a note is played")
check(d.seen(36, 0.0) is True, "same note at the same instant is dropped")
check(d.seen(36, 5.0) is True, "same note 5 ticks later is dropped (layered kick)")
check(d.seen(36, 18.0) is False, "same note 18 ticks later is a real flam, played")
check(d.seen(38, 0.0) is False, "a different note at the same instant is played")
d.new_pulse()
check(d.seen(36, 0.0) is True, "end of the last pulse -> start of the next: 2 ticks apart, dropped")
d.new_pulse(); d.new_pulse()
check(d.seen(36, 0.0) is False, "a note 40+ ticks later is a new hit")

# --- replay real songs: nothing may collapse -------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
SONG_DIRS = [os.path.expanduser("~/.orc1_bbsongs"),
             os.path.join(HERE, "..", "..", "_beatbuddy", "slate_songs")]
songs = []
for sd in SONG_DIRS:
    if os.path.isdir(sd):
        songs += sorted(os.path.join(sd, f) for f in os.listdir(sd)
                        if f.lower().endswith((".bbs", ".json")) and "catalog" not in f)
songs = songs[:60]
BEAT = 24
raw_same_instant = deduped_same_instant = hits = 0
for path in songs:
    ev = []; pulse = [0]
    m = bb.BeatBuddyMind(emit=lambda n, v, dl, r, p, pk: ev.append((pulse[0], n, dl)) if v > 0 else None)
    try:
        (m.load_ir if path.endswith(".json") else m.load_bbs)(path)
    except Exception:
        continue
    def tick(n):
        for _ in range(n):
            m.process_song(1.0, bb.TICKS_PER_CLOCK); pulse[0] += 1
    m.tap(); tick(BEAT * 16); m.tap(); tick(BEAT * 12)
    m.external_transition(m.PartIndex + 2); tick(BEAT * 6); m.external_transition(0); tick(BEAT * 16)
    dd = bb.PulseDeduper(); cur = 0
    for p, n, dl in ev:
        while cur < p:
            dd.new_pulse(); cur += 1
        hits += 1
        if dd.seen(n, dl):
            deduped_same_instant += 1
if songs:
    check(hits > 0, f"replayed {len(songs)} songs through taps/fills/transitions ({hits} hits)")
    check(deduped_same_instant < hits * 0.02,
          "same-instant repeats are rare enough to be layering, not the norm",
          f"{deduped_same_instant}/{hits}")
else:
    check(True, "[SKIPPED: no song files here]"); check(True, "[SKIPPED: no song files here]")

# --- wiring nets -----------------------------------------------------------
SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8", errors="replace").read()
emit = SRC[SRC.index("def _bb_emit("):SRC.index("beatbuddy = _bbmod.BeatBuddyMind")]
check("hit_delay_seconds(delay" in emit and "_bb_later(" in emit,
      "_bb_emit honours the mind's sub-pulse delay")
check('dest != "bbkit" and _bb_dedupe.seen(t[:-1], delay)' in emit,
      "_bb_emit drops same-instant repeats for non-sampler outputs")
tick_src = SRC[SRC.index("def _bb_tick("):SRC.index("arp.subscribe_ticks(1, _bb_tick)")]
check("_bb_dedupe.new_pulse()" in tick_src and "_bb_pulse_s" in tick_src,
      "the clock tick resets the deduper and measures the pulse length")

# --- per-drum mixer (BB MIX card) -------------------------------------------
check([bb.note_group(n) for n in (36, 38, 42, 51, 49, 37, 45, 99)] ==
      ["kick", "snare", "hat", "ride", "crash", "rim", "tom", "perc"],
      "note_group sorts BeatBuddy notes into drums; unknown notes are perc")
check(bb.scale_velocity(100, 1.0) == 100, "level 100% leaves velocity alone")
check(bb.scale_velocity(100, 0) == 0, "level 0 mutes (velocity 0 = do not play)")
check(bb.scale_velocity(100, 0.5) == 50, "level 50% halves velocity")
check(bb.scale_velocity(100, 2.0) == 127 and bb.scale_velocity(120, 9) == 127,
      "boost clamps to 127 (and gain is capped at 200%)")
check(bb.scale_velocity(1, 0.1) == 1, "a quiet level never rounds a hit to silence")
check(bb.clamp_gain("x") == 1.0 and bb.clamp_gain(float("nan")) == 1.0 and bb.clamp_gain(-3) == 0.0,
      "garbage level -> 100%, negative -> 0")
check("scale_velocity(t[-1], _BB_MIX.get(_bbmod.target_group(dest, t[-2])" in emit,
      "_bb_emit applies the per-drum level (after the dedupe)")
check('action == "BB_MIX"' in SRC and '"mix":        dict(_BB_MIX)' in SRC and "_bb_mix_load()" in SRC
      and "_bb_mix_set(" in SRC,
      "BB_MIX cmd, /state mix field, and restore-at-start are wired")

# --- review fixes (2026-10-19): keys by what is PLAYED, the delay cap, the note-off list lock -------------------
check([bb.target_group("909", n) for n in (36, 38, 42, 46, 41, 47, 48, 50, 49, 55, 51, 53, 37, 39, 56)] ==
      ["kick", "snare", "hat", "hat", "tom", "tom", "tom", "tom", "crash", "crash", "ride", "ride", "rim", "rim", "perc"]
      and bb.target_group("bbkit", 47) == "hat" and bb.target_group("port", 35) == "kick",
      "909 output: MIX drums follow the 909 voice that plays (47 = mid tom there, a hat on BBKit; 35 = a BeatBuddy kick)")
d2 = bb.PulseDeduper()
k909_snare = ("synth", 10, 38)
check(d2.seen(k909_snare, 0.0) is False and d2.seen(k909_snare, 2.0) is True and d2.seen(("synth", 10, 36), 2.0) is False
      and d2.seen(("port", "mpc", 10, 38), 2.0) is False,
      "the dedupe keys on the target (two BeatBuddy notes folded onto one 909 voice count once; another voice/port does not)")
d3 = bb.PulseDeduper()
d3.seen("k", 0.0); d3.new_pulse(); d3.new_pulse(); d3.new_pulse(); d3.new_pulse(); d3.new_pulse()
check(d3.seen("k", 1000.0) is False and d3.seen("k", 80.0) is True,
      "an absurd delay is capped (80 ticks) the same way the player caps it, so the two can never disagree")
_sd = SRC[SRC.index("def _bb_send_target("):SRC.index("def _bb_delayed_run(")]
_fl = SRC[SRC.index("def _bb_flush_offs("):SRC.index("def _bb_out_file(")]
check("with _bb_offs_lock:" in _sd and "_bb_pending_offs.append" in _sd and "with _bb_offs_lock:" in _fl
      and "_bb_offs_lock = threading.Lock()" in SRC,
      "the pending note-off list is lock-guarded in both the delayed-hit worker's send and the tick's flush (a lost append = a stuck note)")

EXPECTED = 31
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
sys.exit(1 if (_f or _p + _f != EXPECTED) else 0)
