"""
_test_bb_concurrency.py -- proves the BeatBuddy mind survives being driven by the
three ORC threads at once (clock tick + web/cmd verbs + foot pedal), which the
firmware never faces (it runs one loop). Pure stdlib; runs anywhere.

It hammers process_song() on a tight "clock" thread while worker threads fire
random gestures AND swap songs of different part-counts -- exactly the live
"press STOP / load a song while a fill is triggering" race. Any exception on ANY
thread fails the run.

Set BB_MODULE=<path to an orc1_beatbuddy.py> to test a specific copy (used to
show the UNLOCKED version crashes and the fixed one does not). Default: the
orc1_beatbuddy.py next to this file.
"""
import os, sys, time, random, threading, importlib.util

EXPECTED = 6  # checks reported by this suite

_here = os.path.dirname(os.path.abspath(__file__))
_modpath = os.environ.get("BB_MODULE", os.path.join(_here, "orc1_beatbuddy.py"))
_spec = importlib.util.spec_from_file_location("_bb_under_test", _modpath)
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)


def _make_ir(nparts, seed):
    """A valid multi-part song: intro + outro + nparts*(main,trans,fill)."""
    rng = random.Random(seed)
    tracks = []

    def mktrack():
        # one bar of events, note-on + note-off pairs; nTick = 480
        ev = []
        for t in (0, 120, 240, 360):
            note = rng.choice([36, 38, 42, 46, 49, 51])
            ev.append([t, note, rng.randint(40, 127)])
            ev.append([t + 40, note, 0])
        return {"events": ev, "barLength": 480, "ts": [4, 4], "tpqn": 480, "nTick": 480}

    intro_i = len(tracks); tracks.append(mktrack())
    outro_i = len(tracks); tracks.append(mktrack())
    parts = []
    for _ in range(nparts):
        mi = len(tracks); tracks.append(mktrack())
        ti = len(tracks); tracks.append(mktrack())
        fi = len(tracks); tracks.append(mktrack())
        parts.append({"name": "P", "main": mi, "trans": ti, "fills": [fi]})
    return {"name": f"synth{nparts}", "bpm": 120, "loopSong": 0, "kit": "x",
            "tracks": tracks, "intro": {"main": intro_i}, "outro": {"main": outro_i},
            "parts": parts}


def run_stress(seconds=4.0, n_gesture_threads=3):
    """Return a list of (thread_name, exc) captured across all threads."""
    songs = [bb.build_song_from_ir(_make_ir(np, np))
             for np in (1, 2, 3, 4, 5)]   # different part-counts -> load race
    emitted = []
    mind = bb.BeatBuddyMind(emit=lambda *a: emitted.append(a[0]))
    mind.load_song(songs[3])

    errors = []
    stop = threading.Event()
    err_lock = threading.Lock()

    import traceback as _tb
    def record(name, e):
        with err_lock:
            errors.append((name, repr(e), _tb.format_exc()))

    def clock():
        n = 0
        try:
            while not stop.is_set():
                mind.process_song(1.0, bb.TICKS_PER_CLOCK)
                n += 1
                if (n & 63) == 0:
                    time.sleep(0)   # yield GIL, widen interleave windows
        except Exception as e:
            record("clock", e)

    GESTURES = ["tap", "hold", "release", "double_tap", "stop", "pause",
                "trans", "load", "start"]

    def gestures(idx):
        rng = random.Random(1000 + idx)
        try:
            while not stop.is_set():
                g = rng.choice(GESTURES)
                if g == "trans":
                    mind.external_transition(rng.randint(0, 6))
                elif g == "load":
                    mind.load_song(rng.choice(songs))
                elif g in ("start", "stop", "pause"):
                    getattr(mind, g)()          # these take no time arg
                else:
                    getattr(mind, g)(int(time.monotonic() * 1000))  # tap/hold/release/double_tap
                time.sleep(rng.uniform(0, 0.002))
        except Exception as e:
            record(f"gesture{idx}", e)

    threads = [threading.Thread(target=clock, name="clock")]
    threads += [threading.Thread(target=gestures, args=(i,), name=f"g{i}")
                for i in range(n_gesture_threads)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    # post-condition: drive a few more ticks -- must not raise, must be legal
    try:
        for _ in range(200):
            mind.process_song(1.0, bb.TICKS_PER_CLOCK)
    except Exception as e:
        record("post", e)
    return errors, mind, emitted


def main():
    passed = failed = 0

    def check(cond, label):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  ! {label}")

    print(f"module under test: {_modpath}")
    secs = float(os.environ.get("BB_SECS", "4"))
    nthr = int(os.environ.get("BB_THREADS", "3"))
    errors, mind, emitted = run_stress(seconds=secs, n_gesture_threads=nthr)
    print(f"invariant-net recoveries during run: {getattr(mind, '_recover_count', 'n/a')}")

    if errors:
        print(f"  {len(errors)} thread exception(s); first few:")
        for rec in errors[:8]:
            print(f"      [{rec[0]}] {rec[1]}")
        if os.environ.get("BB_TRACE"):
            print("\n---- first traceback ----\n" + errors[0][2])

    check(not errors, f"no thread raised under concurrent drive ({len(errors)} did)")
    check(mind.PlayerStatus in bb.STATUS_NAME, "final PlayerStatus is a legal enum")
    check(len(emitted) > 0, "the mind emitted notes during the run")
    # a clean STOP must settle to STOPPED
    mind.stop()
    for _ in range(300):
        mind.process_song(1.0, bb.TICKS_PER_CLOCK)
    check(mind.PlayerStatus == bb.STOPPED, "STOP settles the mind to STOPPED")
    # and it can restart cleanly
    mind.tap(0)
    for _ in range(50):
        mind.process_song(1.0, bb.TICKS_PER_CLOCK)
    check(mind.PlayerStatus not in (bb.NO_SONG_LOADED, bb.STOPPED),
          "the mind restarts after a post-stress STOP")
    check(mind.CurrSong is not None, "a song is still loaded at the end")

    total = passed + failed
    print(f"\n{passed} passed, {failed} failed  (expected {EXPECTED})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
