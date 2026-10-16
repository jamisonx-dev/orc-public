#!/usr/bin/env python3
"""Regression suite for orc1_beatbuddy (THE MIND -- the BeatBuddy state machine).

Pure stdlib, self-contained: the grammar tests drive a SYNTHETIC in-memory song
(distinct marker notes per section, clean 4/4 geometry) so they run anywhere --
laptop or Pi -- with no jack/mido/numpy and no factory content. The loader tests
run against a real factory .BBS when one is reachable and otherwise emit counted
[SKIPPED] checks so the total stays invariant (the project's silent-shrink rule).

Every grammar transition is asserted AND mutation-proven: reverting a handler
(tap->fill, hold->transition, release->advance, double->outro) or the trig-position
quantization fails a specific check.

Run:  python3 _test_beatbuddy.py
"""
import os, sys, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_beatbuddy as bb

EXPECTED = 62
_p = _f = 0


def check(name, cond):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ! FAIL: {name}")


# ---------------------------------------------------------------------------
# Synthetic song: 4/4, tpqn 480, one-bar sections (barLen 1920), a distinct
# marker note per section so note provenance is unambiguous:
#   INTRO=42  PART1 main=36 (+snare 38)  PART1 fill=50  PART1 trans=49
#   PART2 main=37  OUTRO=51
# ---------------------------------------------------------------------------
BAR = 1920  # 4 * 480


def _track(events, ntick=BAR):
    t = bb.Track()
    t.event = list(events)
    t.nTick = ntick
    t.barLength = BAR
    t.timeSigNum = 4
    t.timeSigDen = 4
    t.tpqn = 480
    t.pickupNotesLength = 0
    bb._adjust_trig_length(t)   # trigPos = 0.7*bar, as the loader does
    return t


def build_song(with_intro=True, with_outro=True, with_trans=True,
               with_fill=True, n_parts=2):
    s = bb.Song()
    s.bpm = 120
    s.nPart = n_parts
    s.tracks = []

    def add(tr):
        s.tracks.append(tr)
        return len(s.tracks) - 1

    # part1 main
    p1_main = add(_track([(0, 36, 100), (960, 38, 100)]))
    # part1 fill (1 bar of tom)
    p1_fill = add(_track([(0, 50, 100), (480, 50, 90), (960, 50, 90), (1440, 50, 90)]))
    # part1 trans (crash across the bar)
    p1_trans = add(_track([(0, 49, 110), (960, 49, 100)]))
    # part2 main
    p2_main = add(_track([(0, 37, 100), (960, 37, 90)]))
    intro_i = add(_track([(0, 42, 100), (960, 42, 90)])) if with_intro else -1
    outro_i = add(_track([(0, 51, 110), (960, 51, 90)])) if with_outro else -1

    part1 = bb.SongPart()
    part1.mainLoopIndex = p1_main
    part1.transFillIndex = p1_trans if with_trans else -1
    part1.drumFillIndex = [p1_fill if with_fill else -1] + [-1] * 7
    part1.nDrumFill = 1 if with_fill else 0

    part2 = bb.SongPart()
    part2.mainLoopIndex = p2_main
    part2.transFillIndex = p1_trans if with_trans else -1
    part2.drumFillIndex = [p1_fill if with_fill else -1] + [-1] * 7
    part2.nDrumFill = 1 if with_fill else 0

    s.part = [part1, part2][:n_parts]
    s.nPart = len(s.part)
    s.intro = bb.SongPart()
    s.intro.mainLoopIndex = intro_i
    s.outro = bb.SongPart()
    s.outro.mainLoopIndex = outro_i
    return s


class Rec:
    """Records emitted note-ons as (part_id, note, vel)."""
    def __init__(self):
        self.notes = []

    def emit(self, note, vel, delay, ratio, pid, pu):
        if vel > 0:
            self.notes.append((pid, note, vel))

    def clear(self):
        self.notes = []

    def pids(self):
        return set(n[0] for n in self.notes)

    def note_set(self):
        return set(n[1] for n in self.notes)


def mind(**kw):
    r = Rec()
    m = bb.BeatBuddyMind(r.emit)
    m.load_song(build_song(**kw))
    return m, r


def run(m, n, ratio=1.0):
    for _ in range(n):
        m.process_song(ratio, bb.TICKS_PER_CLOCK)


def run_until(m, target, nmax=400):
    for i in range(nmax):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == target:
            return i + 1
    return -1


def run_collect_until(m, r, target, nmax=400):
    """Run collecting emitted notes, stop when target reached. Returns pulses."""
    for i in range(nmax):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == target:
            return i + 1
    return -1


# ===========================================================================
# Tests
# ===========================================================================

def test_load_and_reset():
    m, r = mind()
    check("load -> STOPPED", m.PlayerStatus == bb.STOPPED)
    check("load -> part index 0", m.PartIndex == 0)
    check("load -> master tick 0", m.MasterTick == 0)
    check("load -> CurrPart None until started", m.CurrPart is None)
    check("no song loaded when song is None",
          bb.BeatBuddyMind().PlayerStatus == bb.NO_SONG_LOADED)


def test_start_into_intro_then_main():
    m, r = mind()
    m.tap()
    m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("tap from stopped -> INTRO", m.PlayerStatus == bb.INTRO)
    # collect notes across the whole intro (until it hands off to main)
    r.clear()
    pulses = run_until(m, bb.PLAYING_MAIN_TRACK)
    check("INTRO -> PLAYING_MAIN_TRACK on its own", pulses > 0)
    check("INTRO played intro notes (42)", 42 in r.note_set())
    check("INTRO notes carry INTR_FILL_ID", bb.INTR_FILL_ID in r.pids())
    check("after intro, part index 0", m.PartIndex == 0)
    r.clear()
    run(m, 100)   # a full bar of main
    check("main plays part1 main note (36)", 36 in r.note_set())
    check("main notes carry MAIN_PART_ID", bb.MAIN_PART_ID in r.pids())


def test_no_intro_starts_in_main():
    m, r = mind(with_intro=False)
    m.tap()
    m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("no-intro tap -> PLAYING_MAIN_TRACK directly",
          m.PlayerStatus == bb.PLAYING_MAIN_TRACK)


def test_tap_drumfill_returns_same_part():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK)          # start
    run(m, 4)                                             # settle in main
    part_before = m.PartIndex
    m.tap()                                               # request a fill
    check("tap in main -> DRUMFILL_WAITING_TRIG", m.RequestFlag == bb.DRUMFILL_REQUEST
          or m.PlayerStatus in (bb.DRUMFILL_WAITING_TRIG, bb.PLAYING_MAIN_TRACK))
    # advance to the fill
    got_active = False
    r.clear()
    for _ in range(200):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == bb.DRUMFILL_ACTIVE:
            got_active = True
        if got_active and m.PlayerStatus == bb.PLAYING_MAIN_TRACK:
            break
    check("drumfill became ACTIVE", got_active)
    check("drumfill emitted fill note (50)", 50 in r.note_set())
    check("drumfill notes carry DRUM_FILL_ID", bb.DRUM_FILL_ID in r.pids())
    check("after fill -> PLAYING_MAIN_TRACK", m.PlayerStatus == bb.PLAYING_MAIN_TRACK)
    check("drumfill RETURNS to same part", m.PartIndex == part_before)


def test_trig_quantization_late_tap_defers():
    # A tap in the LAST 30% of the bar (past trigPos = 70%) must DEFER the fill to
    # the next bar boundary: WAITING_TRIG persists, main keeps playing, no fill note
    # leaks until the bar rolls. (An early tap correctly fires quickly -- that is not
    # what this guards.) Mutation-sensitive to trigPos: set it to barLength and a
    # late tap would floor to the current bar and fire immediately.
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK)
    trig = m.CurrSong.tracks[m.CurrPart.mainLoopIndex].trigPos   # 1344
    # step until we are just past trigPos within a bar
    for _ in range(500):
        if trig < (m.MasterTick % BAR) < BAR - 40:
            break
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("positioned past trigPos in the bar", (m.MasterTick % BAR) > trig)
    m.tap()
    m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("late tap -> WAITING_TRIG (deferred)", m.PlayerStatus == bb.DRUMFILL_WAITING_TRIG)
    r.clear()
    saw_fill_early = False
    # remainder of this bar should keep playing main, not the fill
    for _ in range(20):
        if m.PlayerStatus != bb.DRUMFILL_WAITING_TRIG:
            break
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if 50 in r.note_set():
            saw_fill_early = True
    check("deferred: no fill note before the bar boundary", not saw_fill_early)


def test_hold_transition_loops():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.hold()
    got = run_until(m, bb.TRANFILL_ACTIVE)
    check("hold in main -> TRANFILL_ACTIVE", got > 0)
    r.clear()
    run(m, 100)   # a full bar of the looping transition
    check("transition emits trans note (49)", 49 in r.note_set())
    check("transition notes carry TRAN_FILL_ID", bb.TRAN_FILL_ID in r.pids())
    check("transition keeps looping (still active / part unchanged)",
          m.PlayerStatus == bb.TRANFILL_ACTIVE and m.PartIndex == 0)


def test_release_advances_part():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.hold(); run_until(m, bb.TRANFILL_ACTIVE); run(m, 4)
    check("in transition on part 1", m.PartIndex == 0)
    m.release()
    pulses = run_until(m, bb.PLAYING_MAIN_TRACK)
    check("release ends transition -> PLAYING_MAIN_TRACK", pulses > 0)
    check("release ADVANCES to part 2", m.PartIndex == 1)
    r.clear(); run(m, 4)
    check("part 2 plays its own main note (37)", 37 in r.note_set())


def test_double_tap_outro_then_stop():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.double_tap(time_ms=1000)
    got_outro = False
    r.clear()
    for _ in range(300):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == bb.OUTRO:
            got_outro = True
        if m.PlayerStatus == bb.STOPPED:
            break
    check("double-tap -> OUTRO", got_outro)
    check("outro emits outro note (51)", 51 in r.note_set())
    check("outro notes carry OUTR_FILL_ID", bb.OUTR_FILL_ID in r.pids())
    check("after outro -> STOPPED", m.PlayerStatus == bb.STOPPED)


def test_double_tap_no_outro_plays_to_end():
    m, r = mind(with_outro=False)
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.double_tap(time_ms=1000)
    m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("no-outro double-tap -> PLAYING_MAIN_TRACK_TO_END",
          m.PlayerStatus == bb.PLAYING_MAIN_TRACK_TO_END)
    got = run_until(m, bb.STOPPED)
    check("plays to end then STOPPED", got > 0)


def test_pause_unpause():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.pause()
    m.process_song(1.0, bb.TICKS_PER_CLOCK)
    check("pause -> PAUSED", m.PlayerStatus == bb.PAUSED)
    r.clear(); run(m, 6)
    check("PAUSED emits no notes", len(r.notes) == 0)
    m.pause()  # external_pause is a toggle -> unpause
    got = run_until(m, bb.PLAYING_MAIN_TRACK, nmax=400)
    check("unpause resumes playing", got > 0
          or m.PlayerStatus in (bb.DRUMFILL_WAITING_TRIG, bb.DRUMFILL_ACTIVE))


def test_stop_from_playing():
    m, r = mind()
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.stop()
    check("external stop -> STOPPED", m.PlayerStatus == bb.STOPPED)
    check("stop clears CurrPart", m.CurrPart is None)
    check("stop resets part index", m.PartIndex == 0)


def test_autopilot_dormant():
    m, r = mind()
    check("APPtr is None (no autopilot)", m.APPtr is None)
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 30)
    check("BeatCounter stays 0 without autopilot", m.BeatCounter == 0)


def test_adjust_helpers():
    # _adjust_trig_length: trigPos = 70% of a bar
    t = bb.Track(); t.barLength = 1920
    bb._adjust_trig_length(t)
    check("adjust_trig_length = 70% bar", t.trigPos == int(0.70 * 1920))
    # _adjust_length pads a short main loop up to a whole number of beats/bars.
    # 3 beats (1440) in 4/4 -> +1 beat = 1920 (one bar). (ports adjust_length verbatim)
    t2 = bb.Track(); t2.timeSigNum = 4; t2.timeSigDen = 4; t2.nTick = 1440
    bb._adjust_length(t2)
    check("adjust_length pads 3 beats -> full bar (1920)", t2.nTick == 1920)
    t3 = bb.Track(); t3.timeSigNum = 4; t3.timeSigDen = 4; t3.nTick = 1920
    bb._adjust_length(t3)
    check("adjust_length leaves whole bar unchanged", t3.nTick == 1920)


def test_sync_tick_math():
    # tap at/under trig limit floors to current bar; above -> next bar
    lo = bb.BeatBuddyMind._calc_start_bar_sync_tick(100, 1920, 1344)
    hi = bb.BeatBuddyMind._calc_start_bar_sync_tick(1500, 1920, 1344)
    check("start-bar sync floors under trig limit", lo == 0)
    check("start-bar sync rolls to next bar past trig limit", hi == 1920)
    q = bb.BeatBuddyMind._calc_tranfill_quit_sync_tick(100, 1920, 1)
    check("tranfill quit sync = next bar", q == 1920)


def test_note_provenance_full_pass():
    """One full pass, asserting each section's marker note carries its part id."""
    m, r = mind()
    seen = {}   # note -> set of part_ids

    def cap(note, vel, delay, ratio, pid, pu):
        if vel > 0:
            seen.setdefault(note, set()).add(pid)
    m.emit = cap
    m.tap(); run_until(m, bb.PLAYING_MAIN_TRACK)   # intro then main
    run(m, 4)
    m.tap()                                        # fill
    for _ in range(200):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == bb.PLAYING_MAIN_TRACK and 50 in seen:
            break
    m.hold(); run_until(m, bb.TRANFILL_ACTIVE); run(m, 4)
    m.release(); run_until(m, bb.PLAYING_MAIN_TRACK); run(m, 4)
    m.double_tap(time_ms=1000)
    for _ in range(300):
        m.process_song(1.0, bb.TICKS_PER_CLOCK)
        if m.PlayerStatus == bb.STOPPED:
            break
    check("intro note 42 = INTR_FILL_ID", seen.get(42) == {bb.INTR_FILL_ID})
    check("main note 36 = MAIN_PART_ID", bb.MAIN_PART_ID in seen.get(36, set()))
    check("fill note 50 = DRUM_FILL_ID", seen.get(50) == {bb.DRUM_FILL_ID})
    check("trans note 49 = TRAN_FILL_ID", seen.get(49) == {bb.TRAN_FILL_ID})
    check("part2 note 37 = MAIN_PART_ID", seen.get(37) == {bb.MAIN_PART_ID})
    check("outro note 51 = OUTR_FILL_ID", seen.get(51) == {bb.OUTR_FILL_ID})


# ---- loader tests against real factory content (counted [SKIPPED] if absent) ----

def _find_factory_bbs():
    """Factory .BBS songs for the loader tests, or [] (-> counted [SKIPPED]).
    BOUNDED search (2026-09-23): the old recursive glob of here/../.. walked the
    ENTIRE filesystem when the suite ran from a shallow path (a scratch copy in
    /tmp/x made that `/`) and hung for 10+ minutes. Now: never walk the filesystem
    root or a top-level dir ("/", "/home", "C:/Users"), at most 6 levels deep,
    hidden/system dirs skipped (the old glob skipped hidden dirs too). First root
    with hits wins, same order as before."""
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [
        os.getcwd(),
        here,
        os.path.join(here, "..", ".."),
        os.path.join(here, "..", "..", ".."),
    ]
    skip = {"proc", "sys", "dev", "node_modules", "__pycache__"}
    for root in roots:
        rp = os.path.realpath(root)
        parts = [p for p in rp.replace("\\", "/").split("/") if p]
        if parts and parts[0].endswith(":"):          # Windows drive letter
            parts = parts[1:]
        if len(parts) < 2:                              # "/", "/home", "C:/Users"
            continue
        base = rp.rstrip(os.sep).count(os.sep)
        hits = []
        for dp, dns, fns in os.walk(rp):
            if dp.rstrip(os.sep).count(os.sep) - base >= 6:
                dns[:] = []
            dns[:] = [d for d in dns if not d.startswith(".") and d not in skip]
            hits += [os.path.join(dp, f) for f in fns if f.endswith(".BBS")]
        if hits:
            return sorted(hits)
    return []


def test_loader_real_bbs():
    files = _find_factory_bbs()
    N = 6  # this test always contributes 6 checks (invariant count)
    if not files:
        for i in range(N):
            check(f"loader [SKIPPED: no factory .BBS reachable] #{i + 1}", True)
        return
    # pick a full-featured song (intro + outro + a transition + a fill)
    chosen = None
    for p in files:
        try:
            s = bb.load_bbs(p)
        except Exception:
            continue
        if (s.intro.mainLoopIndex >= 0 and s.outro.mainLoopIndex >= 0
                and any(pt.transFillIndex >= 0 for pt in s.part)
                and any(pt.nDrumFill > 0 for pt in s.part)):
            chosen = s
            break
    if chosen is None:
        chosen = bb.load_bbs(files[0])
    check("loader: nPart > 0", chosen.nPart > 0)
    check("loader: tracks decoded", len(chosen.tracks) > 0)
    ml = chosen.tracks[chosen.part[0].mainLoopIndex]
    check("loader: main loop has events", len(ml.event) > 0)
    check("loader: main loop nTick > 0", ml.nTick > 0)
    check("loader: trigPos == 70% of barLength (adjust applied)",
          ml.trigPos == int(0.70 * ml.barLength))
    check("loader: main nTick padded to whole bars",
          ml.timeSigNum > 0 and (ml.nTick % (4 * 480 // ml.timeSigDen)) % ml.timeSigNum == 0)


def main():
    tests = [
        test_load_and_reset,
        test_start_into_intro_then_main,
        test_no_intro_starts_in_main,
        test_tap_drumfill_returns_same_part,
        test_trig_quantization_late_tap_defers,
        test_hold_transition_loops,
        test_release_advances_part,
        test_double_tap_outro_then_stop,
        test_double_tap_no_outro_plays_to_end,
        test_pause_unpause,
        test_stop_from_playing,
        test_autopilot_dormant,
        test_adjust_helpers,
        test_sync_tick_math,
        test_note_provenance_full_pass,
        test_loader_real_bbs,
    ]
    for t in tests:
        t()
    print(f"\n{_p} passed, {_f} failed  (expected {EXPECTED})")
    if _p != EXPECTED or _f:
        print("  !! COUNT/FAIL MISMATCH -- regression")
        sys.exit(1)


if __name__ == "__main__":
    main()
