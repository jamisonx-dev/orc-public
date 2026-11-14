"""_test_bsplights.py -- orc1_bsplights' logic (pure python3, no rig, no MIDI).

    python3 _test_bsplights.py

Sandboxed: HOME / USERPROFILE point at a temp dir BEFORE import (CONF resolves at import),
nothing reaches a BSP (the sender writes into a list), and the real ~/.orc1_bsplights.json
is checked byte-identical at the end. The step-clock simulation guards the conductor's old
bug: stepping from each (late, jittery) feed update SKIPPED or doubled steps.
"""
import hashlib, os, random, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.expanduser("~/.orc1_bsplights.json")


def md5(p):
    try:
        return hashlib.md5(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


real_before = md5(REAL)
SANDBOX = tempfile.mkdtemp(prefix="bsplights_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX
sys.path.insert(0, HERE)
import orc1_bsplights as L            # noqa: E402

EXPECTED = 45
passed = failed = 0


def check(cond, label, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", label, extra)


W, OFF, BLU, CYN = L.COL["white"], L.COL["off"], L.COL["blue"], L.COL["cyan"]


def trk(name, steps, mute=False):
    return {"name": name, "len": len(steps), "pos": 0, "mute": mute, "steps": steps}


def feed(t, running=True, step=0, age_ms=0, step_ms=125.0, tracks=None, bindings=None, mods=None):
    return {"t": t, "transport": {"running": running, "step": step if running else -1,
                                  "age_ms": age_ms, "step_ms": step_ms, "bpm": 120.0},
            "tracks": tracks or [], "bindings": bindings or {}, "mods": mods or {}}


# ---- sandbox + config ---------------------------------------------------------------
check(L.CONF.startswith(SANDBOX), "CONF resolved inside the sandbox", L.CONF)
check(L.load_conf()["show"] == "manual", "no config file -> manual (the page keeps today's behaviour)")
L.save_conf({"show": "clock", "track": "kick", "junk": 1})
c = L.load_conf()
check(c == {"show": "clock", "track": "kick"}, "save/load round trip, unknown keys dropped", c)
try:
    L.save_conf({"show": "disco"}); bad = False
except ValueError:
    bad = True
check(bad, "an unknown show is refused")
with open(L.CONF, "w") as f:
    f.write("{not json")
check(L.load_conf()["show"] == "manual", "a broken config file -> manual, no crash")

# ---- frame bytes ----------------------------------------------------------------------
check(bytes(L.frame_bytes(0, 0x10)).hex(" ") == "f0 00 20 6b 7f 42 02 00 10 30 10 f7", "step 1 blue frame exact")
check(bytes(L.frame_bytes(15, L.RELEASE)).hex(" ") == "f0 00 20 6b 7f 42 02 00 10 3f 40 f7", "step 16 release frame exact")

# ---- show: track ------------------------------------------------------------------------
tracks = [trk("kick", "1000100010001000"), trk("snare", "0000100000001000")]
f = L.show_track(feed(0, tracks=tracks), 2, {"track": "kick"})
check(f[0] == L.colour_of(0) and f[4] == L.colour_of(0) and f[1] == OFF, "track: notes in the track colour", f)
check(f[2] == W, "track: playhead white at step 3", f)
f = L.show_track(feed(0, tracks=tracks), 4, {"track": "SNARE"})
check(f[4] == W and f[12] == L.colour_of(1), "track: picked by name (case-insensitive), playhead over a note = white", f)
f = L.show_track(feed(0, tracks=[trk("kick", "1111000011110000", mute=True)]), 5, {"track": "kick"})
check(f == [OFF] * 5 + [W] + [OFF] * 10, "track: muted = only the playhead", f)
long32 = "1" + "0" * 15 + "0" * 16
long32 = long32[:16] + "01" + long32[18:]
f = L.show_track(feed(0, tracks=[trk("long", long32)]), 20, {"track": "long"})
check(f[1] == L.colour_of(0) and f[0] == OFF and f[4] == W, "track: 32 steps -> the page under the playhead (step 21 = page 2)", f)
f = L.show_track(feed(0, tracks=tracks), None, {"track": "kick"})
check(W not in f and f[0] == L.colour_of(0), "track: stopped = notes, no playhead", f)
check(L.show_track(feed(0, tracks=[]), 3, {"track": "x"}) == [OFF] * 16, "track: no tracks = all off")
f = L.show_track(feed(0, tracks=tracks), 0, {"track": "nosuch"})
check(f[4] == L.colour_of(0), "track: unknown name falls back to the first track", f)

# ---- show: clock ------------------------------------------------------------------------
f = L.show_clock(feed(0), 6, {})
check(f[0] == CYN and f[4] == BLU and f[8] == BLU and f[12] == BLU and f[6] == W, "clock: beat 1 cyan, beats blue, step white", f)
check(L.show_clock(feed(0), 16, {})[0] == W, "clock: on the downbeat the playhead wins (white)")
check(L.show_clock(feed(0), None, {}) == [OFF] * 16, "clock: stopped = dark")

# ---- show: tracks -----------------------------------------------------------------------
tt = [trk("a", "1000"), trk("b", "0000"), trk("c", "0100", mute=True), trk("d", "0100")]
f = L.show_tracks(feed(0, tracks=tt), 4, {})
check(f[0] == W, "tracks: a hit on the current step = white", f)
check(f[1] == OFF and f[2] == OFF, "tracks: empty or muted track = off", f)
check(f[3] == L.colour_of(3), "tracks: has notes, no hit now = its colour", f)
check(L.show_tracks(feed(0, tracks=tt), None, {})[0] == L.colour_of(0), "tracks: stopped = colours, no white")
check(len(L.show_tracks(feed(0, tracks=[trk(str(i), "1") for i in range(20)]), 0, {})) == 16, "tracks: only 16 shown")

# ---- show: modules ----------------------------------------------------------------------
mf = L.ModuleFlash()
b = {"drums": 10, "poly": 6, "chip_p1": 9}
f0 = mf.frame(feed(0, bindings=b, mods={"drums": {"hits": 5}}), 0.0)
check(f0 == [OFF] * 16, "modules: the first look only baselines (no flash on start)", f0)
f1 = mf.frame(feed(0, bindings=b, mods={"drums": {"hits": 6}, "poly": {"hits": 1}}), 0.01)
check(f1[0] == L.colour_of(0) and f1[1] == L.colour_of(1), "modules: rising counters flash their step", f1)
check(f1[2] == OFF, "modules: a module that did not play stays off", f1)
f2 = mf.frame(feed(0, bindings=b, mods={"drums": {"hits": 6}, "poly": {"hits": 1}}), 0.01 + L.FLASH_S + 0.01)
check(f2 == [OFF] * 16, "modules: the flash ends after FLASH_S", f2)
f3 = mf.frame(feed(0, bindings=b, mods={"drums": {"hits": 2}}), 1.0)
check(f3[0] == OFF, "modules: a counter that drops (brain restart) re-baselines, no flash", f3)

# ---- the sender -------------------------------------------------------------------------
sent = []
lt = L.Lights(lambda b: sent.append(bytes(b)))
lt.send([OFF] * 16)
check(len(sent) == 16, "first frame paints all 16 steps", len(sent))
sent.clear(); lt.send([OFF] * 16)
check(sent == [], "the same frame again sends nothing")
fr = [OFF] * 16; fr[3] = W; fr[9] = BLU
sent.clear(); lt.send(fr)
check(len(sent) == 2 and sent[0][9] == 0x33 and sent[1][9] == 0x39, "only the 2 changed steps are sent", [s.hex() for s in sent])
sent.clear(); lt.forget(); lt.send(fr)
check(len(sent) == 16, "after forget() (BSP reconnect) everything is repainted", len(sent))
sent.clear(); lt.release_all()
check(len(sent) == 16 and all(s[10] == L.RELEASE for s in sent), "release_all hands every step back")

# ---- the engine -------------------------------------------------------------------------
sent.clear()
eng = L.Engine(L.Lights(lambda b: sent.append(bytes(b))))
eng.tick(feed(100.0), {"show": "manual"}, 100.0)
check(len(sent) == 16 and all(s[10] == L.RELEASE for s in sent), "manual: hands every step back once")
sent.clear(); eng.tick(feed(100.0), {"show": "manual"}, 100.1)
check(sent == [], "manual: then stays silent (the page owns the lights)")
sent.clear(); eng.tick(feed(100.0), {"show": "off"}, 100.2)
check(sent == [], "off after manual: already handed back, nothing more")
sent.clear(); eng.tick(feed(90.0, running=False, tracks=[trk("a", "1000")]), {"show": "tracks"}, 100.0)
check(len(sent) == 16 and all(s[10] == OFF for s in sent), "stale feed (brain gone): dark, even on a show with colours")
sent.clear(); eng.tick(None, {"show": "clock"}, 100.0)
check(sent == [], "no feed at all: stays dark, no resend")
sent.clear(); eng.tick(feed(100.0), {"show": "off"}, 100.0)
check(len(sent) == 16 and all(s[10] == L.RELEASE for s in sent), "driven -> off: every step handed back")

# ---- the step clock: next_due never goes back, and skips the backlog after a stall -------
k, due = L.next_due(0.0, 0.125, 10, 0.5)
check(k == 11, "next_due: a relock that puts 'now' at step 4 still shows step 11 next (never back)", (k, due))
k, due = L.next_due(0.0, 0.125, 2, 1.0)
check(k == 8 and due == 1.0, "next_due: after a stall it jumps to the current step (no burst)", (k, due))

# ---- the step clock: late, jittery feed updates never skip or double a step -------------
random.seed(7)
eng = L.Engine(L.Lights(lambda b: None))
conf = {"show": "clock"}
step_s, t0, now, ks = 0.125, 1000.0, 1000.0, []
last_feed = None
while now < t0 + 30:
    if last_feed is None or random.random() < 0.3:          # the feed updates ~every 3rd pass, late
        gstep = int((now - t0) / step_s)
        age = (now - (t0 + gstep * step_s)) * 1000 + random.uniform(-3, 3)
        last_feed = feed(now - random.uniform(0, 0.03), step=gstep, age_ms=max(0, age), step_ms=step_s * 1000)
    w = eng.tick(last_feed, conf, now)
    if not ks or ks[-1] != eng.k:
        ks.append(eng.k)
    now += min(w, 0.01)
diffs = {b - a for a, b in zip(ks, ks[1:])}
check(diffs == {1}, "30 s of jittery feed: every step shown once, in order", sorted(diffs)[:5])
check(abs(len(ks) - 240) <= 2, "30 s at 120 bpm = ~240 steps", len(ks))
off_by = abs((t0 + ks[-1] * step_s) - now)
check(off_by < 0.15, "the shown step stays in time with the real clock", off_by)

# ---- the real file was never touched ----------------------------------------------------
check(md5(REAL) == real_before, "real ~/.orc1_bsplights.json untouched")

total = passed + failed
print("%d passed, %d failed  (expected %d)" % (passed, failed, EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed == 0 and total == EXPECTED else 1)
