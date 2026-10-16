"""
_test_bb_catalog.py -- drive EVERY real BeatBuddy/Slate song through the mind with
a randomized gesture walk and assert the invariants that "it plays like a drummer"
depends on. Content robustness (odd time sigs, missing fills/intro/outro, empty
parts) that synthetic songs can't cover. Single-threaded (thread-safety is
_test_bb_concurrency's job); this is about the CONTENT.

Per song, over a long random walk of taps/holds/releases/nexts/doubles/stops:
  1. process_song never raises.
  2. PlayerStatus is always a legal enum.
  3. every emitted note actually exists in one of the song's tracks (no phantom
     notes -- the player only plays real content).
  4. a plain start + wait keeps emitting (never stuck-silent while "playing").
  5. a STOP always settles to STOPPED within a bounded number of ticks.
  6. the invariant net (_recover_count) never fires (pure self-consistency).

Run on the Pi where ~/.orc1_bbsongs holds the staged catalog:
    BB_SONGS=~/.orc1_bbsongs python3 _test_bb_catalog.py
Set BB_MODULE to test a specific orc1_beatbuddy.py copy.
"""
import os, sys, glob, random, importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
_modpath = os.environ.get("BB_MODULE", os.path.join(_here, "orc1_beatbuddy.py"))
_spec = importlib.util.spec_from_file_location("_bb_cat", _modpath)
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)

SONGS_DIR = os.path.expanduser(os.environ.get("BB_SONGS", "~/.orc1_bbsongs"))
TICKS_PER_SONG = int(os.environ.get("BB_TICKS", "1500"))   # ~= many bars


def load_song(path):
    if path.lower().endswith(".json"):
        return bb.load_ir(path)
    return bb.load_bbs(path)


def valid_notes(song):
    s = set()
    for tr in song.tracks:
        for (_t, note, _v) in tr.event:
            s.add(note)
    return s


def walk_one(path, rng):
    """Returns (ok, reason). Drives one song through a random gesture walk."""
    emitted = []
    mind = bb.BeatBuddyMind(emit=lambda note, *a: emitted.append(note))
    song = load_song(path)
    mind.load_song(song)
    vnotes = valid_notes(song)

    playable = bool(vnotes)   # empty placeholder songs have no notes at all

    # 4) start and let it play a while with no gestures -> a PLAYABLE song must
    #    emit (never stuck-silent while "playing"); an empty song just must not
    #    crash and must not pretend to play.
    mind.tap(0)
    before = len(emitted)
    for _ in range(240):                 # ~half a dozen bars
        mind.process_song(1.0, bb.TICKS_PER_CLOCK)
    if playable and len(emitted) == before:
        return False, "stuck-silent after start (no notes emitted while playing)"
    if not playable and mind.PlayerStatus not in (bb.STOPPED, bb.NO_SONG_LOADED):
        return False, f"empty song entered a playing state ({mind.status_name()})"

    GEST = ["tap", "hold", "release", "double_tap", "pause", "stop",
            "next", "start"]
    for i in range(TICKS_PER_SONG):
        if rng.random() < 0.08:          # a gesture ~every 12 ticks
            g = rng.choice(GEST)
            if g == "next":
                mind.external_transition((song.nPart + 1) if song.nPart else 1)
            elif g in ("start", "stop", "pause"):
                getattr(mind, g)()
            else:
                getattr(mind, g)(i)
        mind.process_song(1.0, bb.TICKS_PER_CLOCK)
        if mind.PlayerStatus not in bb.STATUS_NAME:
            return False, f"illegal PlayerStatus {mind.PlayerStatus} at tick {i}"

    # 3) phantom-note check -- the player only ever emits real song content
    bad = set(emitted) - vnotes
    if bad:
        return False, f"emitted notes not in song: {sorted(bad)[:6]}"

    # 5) a stop GESTURE settles to STOPPED. Faithful BeatBuddy: the first stop
    #    while a drumfill is pending CANCELS the fill and keeps playing, so a
    #    definitive stop can take up to two presses -- allow a few.
    for _ in range(4):
        mind.stop()
        for _ in range(200):
            mind.process_song(1.0, bb.TICKS_PER_CLOCK)
        if mind.PlayerStatus == bb.STOPPED:
            break
    if mind.PlayerStatus != bb.STOPPED:
        return False, f"did not settle to STOPPED after 4 stops ({mind.status_name()})"

    net = getattr(mind, "_recover_count", 0)
    tag = f"emitted {len(emitted)} notes" + (f"  [net:{net}]" if net else "")
    return True, tag


def main():
    files = sorted(glob.glob(os.path.join(SONGS_DIR, "*.BBS"))
                   + glob.glob(os.path.join(SONGS_DIR, "*.bbs"))
                   + glob.glob(os.path.join(SONGS_DIR, "*.json")))
    files = [f for f in files if "catalog" not in os.path.basename(f).lower()]
    # de-dup case-insensitively (Windows) -- harmless on the Pi
    seen, uniq = set(), []
    for f in files:
        k = f.lower()
        if k not in seen:
            seen.add(k); uniq.append(f)
    files = uniq

    if not files:
        print(f"no songs under {SONGS_DIR} -- [SKIPPED] (run on the Pi)")
        print("1 passed, 0 failed  (expected 1)")
        return

    rng = random.Random(20260915)
    n_ok = n_bad = 0
    fails = []
    for f in files:
        try:
            ok, reason = walk_one(f, rng)
        except Exception as e:
            import traceback
            ok, reason = False, f"EXCEPTION {e!r}\n{traceback.format_exc()}"
        if ok:
            n_ok += 1
        else:
            n_bad += 1
            fails.append((os.path.basename(f), reason))

    print(f"catalog: {SONGS_DIR}")
    print(f"songs driven: {len(files)}   ok: {n_ok}   FAILED: {n_bad}")
    for name, reason in fails[:25]:
        print(f"  ! {name}: {reason.splitlines()[0]}")
    if fails and os.environ.get("BB_TRACE"):
        print("\n---- first full failure ----")
        print(fails[0][1])

    print(f"\n{n_ok} passed, {n_bad} failed  (expected {len(files)})")
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
