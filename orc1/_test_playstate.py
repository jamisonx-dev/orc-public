#!/usr/bin/env python3
"""Regression suite for the live play-state feed (orc1_playstate + the brain's
_playstate_snapshot). Pure stdlib, no hardware, touches only a temp dir.

orc1.py cannot be imported (it opens hardware), so the snapshot function is
pulled out by source text and exec'd against stubs -- the project's usual idiom.
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_playstate as ps   # noqa: E402

EXPECTED = 42
_pass = _fail = 0
_tmp = tempfile.mkdtemp(prefix="orc1_ps_test_")


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL: {name} {detail}")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(snaps, path=None, heartbeat=1.0):
    """A publisher whose snapshot_fn walks through `snaps` (last one repeats)."""
    state = {"i": 0}

    def fn():
        i = min(state["i"], len(snaps) - 1)
        v = snaps[i]
        if isinstance(v, Exception):
            raise v
        return v
    clk = Clock()
    p = ps.PlayStatePublisher(fn, path=path or os.path.join(_tmp, "ps.json"),
                              heartbeat=heartbeat, mono=clk, wall=lambda: 1234.5)
    return p, state, clk


def load(p):
    with open(p.path) as f:
        return json.load(f)


def main():
    # -- helpers ------------------------------------------------------------
    check("note_name middle C", ps.note_name(60) == "C4")
    check("note_name sharp + low", ps.note_name(37) == "C#2")
    check("held_names sorted + unique",
          ps.held_names([(64, "synth", 6), (60, "synth", 6), (60, "pisound", 1)])
          == ["C4", "E4"])
    check("held_names survives garbage", ps.held_names(None) == [])

    a = ps.ActivityTracker()
    check("activity empty", a.by_module({"poly": 6}) == {})
    a.hit(6, 100)
    a.hit(6, 90)
    a.hit(9, 50)
    m = a.by_module({"poly": 6, "chip_p1": 9, "drums": 10})
    check("activity counts hits", m["poly"] == {"hits": 2, "vel": 90})
    check("activity per module", m["chip_p1"]["hits"] == 1)
    check("unplayed/unbound module absent", "drums" not in m)
    check("activity follows a REBIND (module named, channel live)",
          a.by_module({"poly": 9})["poly"]["hits"] == 1)

    # -- publisher: change detection ----------------------------------------
    base = {"transport": {"running": True, "step": 3, "age_ms": 10}, "held": []}
    p, st, clk = make([base])
    check("first tick writes", p.tick() is True)
    d = load(p)
    check("stamped schema/time/count", d["v"] == 1 and d["t"] == 1234.5 and d["n"] == 1)
    check("payload carried", d["transport"]["step"] == 3)
    check("identical snapshot = no write", p.tick() is False and p.writes == 1)

    st["i"] = 1
    p._fn = lambda: {"transport": {"running": True, "step": 3, "age_ms": 999},
                     "held": []}
    check("only the age changed = no write (volatile ignored)", p.tick() is False)

    p._fn = lambda: {"transport": {"running": True, "step": 4, "age_ms": 5},
                     "held": []}
    check("a real change writes", p.tick() is True and load(p)["transport"]["step"] == 4)
    check("write counter increments", load(p)["n"] == 2)

    clk.t += 0.5
    check("inside the heartbeat window = quiet", p.tick() is False)
    clk.t += 0.6
    check("heartbeat re-writes an idle state", p.tick() is True and load(p)["n"] == 3)

    # -- publisher: failure handling ----------------------------------------
    p2, st2, _ = make([RuntimeError("boom")])
    check("snapshot exception is swallowed", p2.tick() is False and p2.errors == 1)
    p3, _, _ = make(["not a dict"])
    check("non-dict snapshot is rejected", p3.tick() is False and p3.errors == 1)
    bad = ps.PlayStatePublisher(lambda: {"x": 1},
                                path=os.path.join(_tmp, "no_such_dir", "f.json"))
    check("unwritable path is swallowed, no thread death",
          bad.tick() is False and bad.errors == 1)
    check("failed write leaves no tmp litter",
          not any(n.endswith(".tmp") for n in os.listdir(_tmp)))

    # -- atomicity: a reader never sees a torn file --------------------------
    src = open(os.path.join(HERE, "orc1_playstate.py")).read()
    check("write goes through os.replace",
          "os.replace(tmp, self.path)" in src)

    # -- read() / staleness ---------------------------------------------------
    d, age = ps.read(p.path, wall=lambda: 1234.5 + 2.0)
    check("read reports age since the brain last wrote", d and abs(age - 2.0) < 1e-6)
    check("read of a missing file", ps.read(os.path.join(_tmp, "nope")) == (None, None))

    # -- SSE tail --------------------------------------------------------------
    path = os.path.join(_tmp, "sse.json")
    with open(path, "w") as f:
        f.write('{"a":1}')
    mt = [1]
    calls = {"n": 0}

    class S:
        def __init__(self, ns):
            self.st_mtime_ns = ns

    def fake_stat(_):
        return S(mt[0])

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 2:        # file changes on the second pause
            with open(path, "w") as f:
                f.write('{"a":2}')
            mt[0] = 2
        elif calls["n"] > 4:
            calls["stop"] = True
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 10.0         # every call looks like 10 s later
        return clock["t"]
    ev = list(ps.sse_events(path, stop=lambda: calls.get("stop", False),
                            sleep=fake_sleep, stat=fake_stat, mono=mono,
                            keepalive=15.0))
    datas = [e for e in ev if e.startswith("data:")]
    check("SSE sends the first state immediately", datas and '"a":1' in datas[0])
    check("SSE sends again only when the file changed",
          len(datas) == 2 and '"a":2' in datas[1])
    check("SSE frames are well formed", all(e.endswith("\n\n") for e in ev))
    check("SSE keepalive when quiet", any(e.startswith(": keepalive") for e in ev))

    # -- the brain's snapshot function, run against stubs -----------------------
    tree = ast.parse(open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read())
    fn_src = next(ast.get_source_segment(open(os.path.join(HERE, "orc1.py"),
                  encoding="utf-8").read(), n) for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_playstate_snapshot")

    class Arp:
        enabled = True
        pattern_name = "up"
        subdivision = "1/16"
        clock_mode = "internal"
        internal_bpm = 90.0

        def _estimated_bpm(self):
            return 90.0

    class Seq:
        running = True
        _global_step = 21
        _step_wall = time.monotonic() - 0.05
        record_mode = False
        tracks = [{"name": "kick", "length": 4, "pos": 2, "mute": False,
                   "steps": [True, False, True, False]}]

    g = {"time": time, "seq": Seq(), "arp": Arp(), "beatbuddy": None,
         "ROOT_NAMES": ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"],
         "chord_name_str": lambda: "Cmaj7", "current_degree": lambda: 1,
         "state": {"root": 7, "scale": "major", "preset": "MONO_CLEAN", "octave": 3,
                   "oct_offset": 1, "held_notes": [(67, "synth", 6)]},
         "_bindings": {"poly": 6}, "_ps_activity": ps.ActivityTracker(),
         "_orc1_playstate": ps}
    g["_ps_activity"].hit(6, 110)
    exec(fn_src, g)
    s = g["_playstate_snapshot"]()
    json.dumps(s)    # must be serialisable
    tr = s["transport"]
    check("snapshot: transport + bar step", tr["running"] and tr["step"] == 21
          and tr["bar_step"] == 5 and tr["bpm"] == 90.0)
    check("snapshot: step length matches the tempo (16th @ 90)", tr["step_ms"] == 166.7)
    check("snapshot: age of the current step, not zero", 30 <= tr["age_ms"] < 2000)
    check("snapshot: chord/key/held/activity/tracks",
          s["chord"]["key"] == "G" and s["held"] == ["G4"]
          and s["mods"]["poly"]["hits"] == 1 and s["tracks"][0]["steps"] == "1010")
    g["seq"].running = False
    s2 = g["_playstate_snapshot"]()
    check("snapshot: stopped = step -1, age 0",
          s2["transport"]["step"] == -1 and s2["transport"]["age_ms"] == 0
          and s2["transport"]["bar_step"] == -1)

    # -- both synth-bus choke points must feed the activity tracker ------------
    # (found live: send_synth_midi writes straight to the bus and never passes
    # through send_to, so hooking only send_to hid web / Fire / module notes)
    brain_src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    for fname in ("send_to", "send_synth_midi"):
        seg = next(ast.get_source_segment(brain_src, n) for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == fname)
        check(f"{fname} feeds the HUD activity tracker",
              "_ps_activity.hit(" in seg)

    # -- the phone HUD page (/hud) is wired in, and cannot inject markup ----------------
    def rd(rel):
        return open(os.path.join(HERE, rel), encoding="utf-8").read()
    js, web = rd("static/js/hud.js"), rd("orc1_web.py")
    check("hud.js never uses innerHTML / insertAdjacentHTML / document.write "
          "(track + chord names are user data)",
          not re.search(r"innerHTML|insertAdjacentHTML|document\.write|outerHTML",
                        re.sub(r"/\*.*?\*/|//[^\n]*", "", js, flags=re.S)))   # code only: comments may say the word
    check("hud.js reads the SSE stream and has a polling fallback",
          "new EventSource('/api/hud/stream')" in js and "fetch('/api/hud')" in js)
    check("/hud route renders hud.html",
          re.search(r"@app\.route\('/hud'\)\s*def hud_page\(\):.*?render_template\('hud\.html'\)", web, re.S) is not None)
    check("hud.html loads hud.js", "/static/js/hud.js" in rd("templates/hud.html"))
    check("the nav has a HUD link and health checks the page",
          'href="/hud"' in rd("templates/base.html") and '"/hud"' in rd("orc1_health.py"))
    check("/system links to the phone HUD", 'href="/hud"' in rd("templates/system.html"))

    shutil.rmtree(_tmp, ignore_errors=True)
    total = _pass + _fail
    print(f"{_pass} passed, {_fail} failed (of {total})")
    if total != EXPECTED:
        print(f"COUNT DRIFT: ran {total}, EXPECTED = {EXPECTED}")
        return 1
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
