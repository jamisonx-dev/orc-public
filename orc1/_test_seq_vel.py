#!/usr/bin/env python3
"""_test_seq_vel.py — per-step velocity / accent-tier regression suite.

Covers the 2026-08-17 migration of the sequencer's binary `accent` bool to a
per-step VELOCITY value with configurable tiers (ghost/normal/accent). Unlike
the chord suites this IMPORTS orc1_seq directly (it is a plain module, not
__main__) and drives the real Sequencer methods + the real _advance() playback
path. A tiny fake `mido` is injected when the real one is absent so the suite
runs on a bare PC as well as on the Pi.

Run:  python3 _test_seq_vel.py [-v]
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_seq as S

EXPECTED = 54

# ── Inject a minimal fake mido if the real one is missing (PC runs) ──────────
class _FakeMsg:
    def __init__(self, kind, channel=0, note=0, velocity=0):
        self.type = kind
        self.channel = channel
        self.note = note
        self.velocity = velocity
class _FakeMido:
    Message = _FakeMsg
if S.mido is None:
    S.mido = _FakeMido()

TIERS = [60, 95, 127]   # ghost, normal, accent — must match the fake cfg below

class _FakeCfg:
    # Fresh dicts per instance -- a class-level dict would let one test's
    # VEL_TIERS mutation leak into the next test (it did, first draft).
    def __init__(self):
        self.SEQ = {
            "VEL_TIERS": list(TIERS),
            "DEFAULT_TRACKS": [
                {"name": "kick",  "note": 36, "ch": 10, "vel": 115, "length": 16},
                {"name": "snare", "note": 38, "ch": 10, "vel": 110, "length": 16},
            ],
            "TRACK_COLORS": [[0x50, 0, 0], [0, 0x48, 0]],
        }
        self.SYNTH = {"CH_DRUM": 10}

class _FakeEng:
    def __init__(self):
        self.cfg = _FakeCfg()
        self.arp = None
        self.sent = []            # captured note_on/off messages
    def send_synth_midi(self, msg):
        self.sent.append(msg)
    def note_on_out(self, *a, **k):
        pass
    def note_off_out(self, *a, **k):
        pass
    def send_to(self, *a, **k):
        pass


# ── tiny test harness ────────────────────────────────────────────────────────
_PASS = 0
_FAIL = 0
_VERBOSE = "-v" in sys.argv

def check(cond, label):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        if _VERBOSE:
            print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_TMP_FILES = []
def _tmp_colors_path():
    """A fresh temp path for the per-track colour overlay, guaranteed absent so
    the Sequencer's construction-time load is a no-op. Keeps every test off the
    real ~/.orc1_seq_colors.json. Cleaned up in __main__."""
    fd, p = tempfile.mkstemp(suffix=".seqcolors.json")
    os.close(fd)
    os.remove(p)
    _TMP_FILES.append(p)
    return p

def _new_seq(tracks=None, colors_path=None):
    """Build a Sequencer with an explicit (or default) track list — never
    touches disk (tracks is passed so load_seq_state is not consulted; the
    colour overlay is pinned to a throwaway temp file)."""
    eng = _FakeEng()
    if tracks is None:
        tracks, _ = S._build_default_tracks(eng)
    seq = S.Sequencer(eng, tracks=tracks, bpm_fn=lambda: 120.0,
                      colors_path=colors_path or _tmp_colors_path())
    return eng, seq


# ── 1. defaults carry step_vels, all None ────────────────────────────────────
def t_defaults():
    eng, seq = _new_seq()
    t = seq.tracks[0]
    check("step_vels" in t, "default track has step_vels")
    check(len(t["step_vels"]) == S.MAX_LEN, "step_vels length == MAX_LEN")
    check(all(v is None for v in t["step_vels"]), "step_vels all None at boot")
    check("accent" not in t, "no leftover 'accent' key on default track")


# ── 2. _vel_tiers reads config, falls back on garbage ────────────────────────
def t_vel_tiers():
    eng, seq = _new_seq()
    check(seq._vel_tiers() == TIERS, "_vel_tiers reads SEQ['VEL_TIERS']")
    eng.cfg.SEQ["VEL_TIERS"] = ["junk"]
    check(seq._vel_tiers() == S.SEQ_VEL_TIERS, "malformed tiers -> module default")
    eng.cfg.SEQ["VEL_TIERS"] = [10, 20]           # wrong length
    check(seq._vel_tiers() == S.SEQ_VEL_TIERS, "wrong-length tiers -> module default")
    eng.cfg.SEQ["VEL_TIERS"] = [999, -5, 64]      # out of MIDI range
    check(seq._vel_tiers() == [127, 1, 64], "tiers clamped to 1..127")


# ── 3. cycle_step_vel ladder + OFF-step no-op ────────────────────────────────
def t_cycle():
    eng, seq = _new_seq()
    seq.toggle_step(0, 0)                    # turn step 0 ON
    ladder = [None] + TIERS
    for i in range(1, len(ladder) + 1):
        seq.cycle_step_vel(0, 0)
        expect = ladder[i % len(ladder)]
        check(seq.tracks[0]["step_vels"][0] == expect,
              f"cycle {i}: step_vels[0] == {expect}")
    # OFF step is a no-op (nothing to accent yet)
    seq.cycle_step_vel(0, 5)
    check(seq.tracks[0]["step_vels"][5] is None, "cycle on OFF step is a no-op")


# ── 4. set_step_vel: set / clamp / clear / turns step on ─────────────────────
def t_set():
    eng, seq = _new_seq()
    seq.set_step_vel(0, 3, 100)
    check(seq.tracks[0]["step_vels"][3] == 100, "set_step_vel stores value")
    check(seq.tracks[0]["steps"][3] is True, "set_step_vel turns the step ON")
    seq.set_step_vel(0, 3, 999)
    check(seq.tracks[0]["step_vels"][3] == 127, "set_step_vel clamps high to 127")
    seq.set_step_vel(0, 3, 0)
    check(seq.tracks[0]["step_vels"][3] == 1, "set_step_vel clamps low to 1")
    seq.set_step_vel(0, 3, None)
    check(seq.tracks[0]["step_vels"][3] is None, "set_step_vel(None) clears override")


# ── 5. playback velocity resolution via the REAL _advance() ──────────────────
def _fire_step(seq, eng, tidx, sidx, vel_override):
    """Arm one step at sidx with the given override, advance so pos lands on
    it, and return the velocity actually emitted on the synth bus."""
    t = seq.tracks[tidx]
    t["steps"] = [False] * S.MAX_LEN
    t["step_vels"] = [None] * S.MAX_LEN
    t["step_notes"] = [None] * S.MAX_LEN
    t["length"] = 16
    t["mute"] = False
    t["pos"] = sidx - 1            # advance() will bump pos to sidx
    t["steps"][sidx] = True
    t["step_vels"][sidx] = vel_override
    # mute every other track so only this one emits
    for j, o in enumerate(seq.tracks):
        if j != tidx:
            o["mute"] = True
    eng.sent.clear()
    seq._advance()
    ons = [m for m in eng.sent if m.type == "note_on"]
    return ons[-1].velocity if ons else None

def t_playback():
    eng, seq = _new_seq()
    trk_vel = seq.tracks[0]["vel"]          # 115 for kick
    check(_fire_step(seq, eng, 0, 2, None) == trk_vel,
          "no override -> plays track default vel")
    check(_fire_step(seq, eng, 0, 2, 60) == 60, "ghost override -> vel 60")
    check(_fire_step(seq, eng, 0, 2, 127) == 127, "accent override -> vel 127")
    check(_fire_step(seq, eng, 0, 2, 95) == 95, "normal override -> vel 95")


# ── 6. clear_steps wipes step_vels ───────────────────────────────────────────
def t_clear():
    eng, seq = _new_seq()
    seq.set_step_vel(0, 1, 100)
    seq.set_step_vel(0, 2, 60)
    seq.clear_steps(0)
    check(all(v is None for v in seq.tracks[0]["step_vels"]),
          "clear_steps clears all step_vels")
    check(all(s is False for s in seq.tracks[0]["steps"]),
          "clear_steps clears steps too")


# ── 7. save/load round-trips step_vels ───────────────────────────────────────
def t_save_load():
    eng, seq = _new_seq()
    seq.set_step_vel(0, 4, 60)
    seq.set_step_vel(0, 5, 127)
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        seq.save(path)
        tracks = S.load_seq_state(path)
        check(tracks is not None, "load_seq_state returns tracks")
        check(tracks[0]["step_vels"][4] == 60, "round-trip preserves ghost vel")
        check(tracks[0]["step_vels"][5] == 127, "round-trip preserves accent vel")
        check(tracks[0]["step_vels"][0] is None, "round-trip preserves None steps")
    finally:
        os.remove(path)


# ── 8. backward-compat: old accent[] bool -> accent-tier velocity ────────────
def t_backcompat():
    # Hand-craft a pre-migration save file (accent list, no step_vels).
    old = {
        "version": 1,
        "tracks": [{
            "name": "kick", "ch": 10, "note": 36, "mode": "note", "vel": 115,
            "length": 4, "color": [80, 0, 0], "dest": "synth", "mute": False,
            "steps":  [True, True, False, True],
            "accent": [False, True, False, True],
        }],
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w") as f:
            json.dump(old, f)
        tracks = S.load_seq_state(path)
        sv = tracks[0]["step_vels"]
        acc = S.SEQ_VEL_TIERS[2]
        check(sv[0] is None, "old accent False -> None")
        check(sv[1] == acc, "old accent True -> accent tier")
        check(sv[3] == acc, "old accent True (step 3) -> accent tier")
        check("accent" not in tracks[0], "no 'accent' key after migration load")
    finally:
        os.remove(path)


# ── 9. set_track_prop('color') parses/clamps + round-trips ───────────────────
def t_color():
    eng, seq = _new_seq()
    orig = seq.tracks[0]["color"]
    seq.set_track_prop(0, "color", "10,64,127")
    check(seq.tracks[0]["color"] == (10, 64, 127), "color prop parses r,g,b")
    seq.set_track_prop(0, "color", "999,-5,0")
    check(seq.tracks[0]["color"] == (127, 0, 0), "color prop clamps to 0..127")
    # malformed values leave the colour untouched
    seq.set_track_prop(0, "color", "1,2")
    check(seq.tracks[0]["color"] == (127, 0, 0), "too-few channels -> no change")
    seq.set_track_prop(0, "color", "a,b,c")
    check(seq.tracks[0]["color"] == (127, 0, 0), "non-numeric -> no change")
    # a set colour survives a save/load round-trip
    seq.set_track_prop(0, "color", "20,30,40")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        seq.save(path)
        tracks = S.load_seq_state(path)
        check(tracks[0]["color"] == (20, 30, 40), "color round-trips through save/load")
    finally:
        os.remove(path)


# ── 10. colour auto-persists instantly to the overlay + reloads on next boot ──
def t_color_autopersist():
    cpath = _tmp_colors_path()
    eng, seq = _new_seq(colors_path=cpath)
    # No file until the first colour change (construction load was a no-op).
    check(not os.path.exists(cpath), "no overlay file before any colour change")
    seq.set_track_prop(0, "color", "20,30,40")
    check(os.path.exists(cpath), "colour change writes overlay instantly (no save())")
    with open(cpath) as f:
        data = json.load(f)
    check(data["colors"][0] == [20, 30, 40], "overlay records the new colour by index")
    # A brand-new Sequencer pointed at the same overlay picks the colour back up,
    # overriding the default-palette colour the tracks would otherwise carry.
    _, seq2 = _new_seq(colors_path=cpath)
    check(seq2.tracks[0]["color"] == (20, 30, 40), "overlay reapplied on next construction")
    # Overlay wins over a pattern-saved colour (latest instant pick beats the
    # colour the pattern was last saved with).
    trks = [{"name": "kick", "ch": 10, "note": 36, "mode": "note", "vel": 115,
             "length": 16, "color": [80, 0, 0], "dest": "synth", "mute": False,
             "steps": [False]*S.MAX_LEN, "step_vels": [None]*S.MAX_LEN,
             "step_notes": [None]*S.MAX_LEN, "pos": 0}]
    _, seq3 = _new_seq(tracks=trks, colors_path=cpath)
    check(seq3.tracks[0]["color"] == (20, 30, 40), "overlay overrides pattern-saved colour")
    # A structural change re-mirrors the overlay so index alignment holds.
    seq3.add_track()
    with open(cpath) as f:
        after_add = json.load(f)
    check(len(after_add["colors"]) == seq3.n_tracks,
          "add_track re-mirrors overlay to current track count")


# ── 11. set_track_prop('bright') sets/clamps + round-trips ───────────────────
def t_bright():
    eng, seq = _new_seq()
    check(int(seq.tracks[0].get("bright", 127)) == 127, "default brightness is full (127)")
    seq.set_track_prop(0, "bright", 64)
    check(seq.tracks[0]["bright"] == 64, "bright prop sets an integer value")
    seq.set_track_prop(0, "bright", 999)
    check(seq.tracks[0]["bright"] == 127, "bright clamps high to 127")
    seq.set_track_prop(0, "bright", -5)
    check(seq.tracks[0]["bright"] == 0, "bright clamps low to 0")
    seq.set_track_prop(0, "bright", "nope")
    check(seq.tracks[0]["bright"] == 0, "malformed bright -> no change")
    seq.set_track_prop(0, "bright", 90)
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        seq.save(path)
        tracks = S.load_seq_state(path)
        check(tracks[0]["bright"] == 90, "bright round-trips through save/load")
    finally:
        os.remove(path)


# ── 12. brightness auto-persists to the SAME overlay as colour + reloads ──────
def t_bright_autopersist():
    cpath = _tmp_colors_path()
    eng, seq = _new_seq(colors_path=cpath)
    seq.set_track_prop(0, "bright", 40)
    check(os.path.exists(cpath), "bright change writes the cosmetic overlay instantly")
    with open(cpath) as f:
        data = json.load(f)
    check(data.get("bright", [None])[0] == 40, "overlay records the new brightness by index")
    # colour + brightness share ONE overlay file
    seq.set_track_prop(0, "color", "20,30,40")
    with open(cpath) as f:
        data = json.load(f)
    check("colors" in data and "bright" in data,
          "overlay carries BOTH colours and brightness")
    # reapplied on next construction, overriding a pattern-saved brightness
    _, seq2 = _new_seq(colors_path=cpath)
    check(seq2.tracks[0]["bright"] == 40, "brightness overlay reapplied on next construction")
    trks = [{"name": "kick", "ch": 10, "note": 36, "mode": "note", "vel": 115,
             "length": 16, "color": [80, 0, 0], "bright": 127, "dest": "synth",
             "mute": False, "steps": [False]*S.MAX_LEN, "step_vels": [None]*S.MAX_LEN,
             "step_notes": [None]*S.MAX_LEN, "pos": 0}]
    _, seq3 = _new_seq(tracks=trks, colors_path=cpath)
    check(seq3.tracks[0]["bright"] == 40, "brightness overlay overrides pattern-saved value")


TESTS = [t_defaults, t_vel_tiers, t_cycle, t_set, t_playback,
         t_clear, t_save_load, t_backcompat, t_color, t_color_autopersist,
         t_bright, t_bright_autopersist]

if __name__ == "__main__":
    for fn in TESTS:
        if _VERBOSE:
            print(f"\n{fn.__name__}")
        fn()
    for _p in _TMP_FILES:                    # tidy up any overlay temp files
        try:
            os.remove(_p)
        except OSError:
            pass
    total = _PASS + _FAIL
    print(f"\n{_PASS} passed, {_FAIL} failed  (total {total})")
    if total != EXPECTED:
        print(f"WARNING: EXPECTED {EXPECTED} checks, ran {total} -- a net may "
              f"have silently changed.")
        sys.exit(2)
    sys.exit(1 if _FAIL else 0)
