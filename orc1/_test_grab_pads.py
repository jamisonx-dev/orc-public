#!/usr/bin/env python3
"""
Skipback GRAB pads (2026-10-19; the Fire surface they started on left 2026-11-13).

    python3 _test_grab_pads.py [-v]

owner: trigger a skipback grab of a length he set up "with a press of a fire pad in
orc mode"; "the sample is usually going to be either quantized bar amounts or a 'sample
while holding the button down'"; seconds don't fit long grabs. Fire firmware v0.36
dropped its ORC pad page, so a grab pad is now a pad on ANY device with a pad map; the
old "surface" profiles are dropped on load (their entries were pad numbers, not notes).

Covers: orc1_padmap grab validation + grab_plan (bars at the live tempo end at the press,
hold = press -> release snapped to whole bars from the press, time, everything, tracks),
PadMap press / hold / release from real note messages, an old surface profile being
dropped, and the brain's
_padmap_grab (extracted from orc1.py by source) with skipback_save_all stubbed.

Pure: no port, no JACK, no file outside a temp dir, never runs a grab.
"""
import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_padmap as P                                      # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 38
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


MODS = ["drums", "poly"]
PADS_IN = "LPD8:LPD8 MIDI 1 32:0"


class SX:                     # a pad hit as the device sends it: a note-on, vel 0 = the release (a note-off)
    def __init__(s, pad, vel=100):
        s.type, s.note, s.velocity, s.channel = ("note_on" if vel else "note_off"), pad, vel, 9


def conf_with(pads, surface=False, name="Pads", match="LPD8"):
    raw = {"devices": {name: {"port_match": match, "pads": pads}}}
    if surface:
        raw["devices"][name]["surface"] = True
    return P.validate_config(raw, MODS)


# ── validation ────────────────────────────────────────────────────────────────
c, e = conf_with({"note:8": {"do": "grab", "len": "bars", "bars": 8, "color": "blue"},
                  "note:9": {"do": "grab", "len": "hold"},
                  "note:10": {"do": "grab", "scope": "track", "track": 2, "len": "time", "secs": 720},
                  "note:11": {"do": "grab", "len": "all", "tracks": [3, 2, 2]},
                  "note:12": {"do": "grab", "len": "minutes"},
                  "note:13": {"do": "grab", "len": "bars", "bars": 0}})
pads = c["devices"]["Pads"]["pads"]
check("bars grab kept with its length; the old surface colour is dropped",
      pads.get("note:8") == {"do": "grab", "scope": "all", "tracks": "armed", "len": "bars",
                             "bars": 8}, pads.get("note:8"))
check("a hold grab is kept on an ordinary device, snap defaults ON",
      pads.get("note:9", {}).get("len") == "hold" and pads["note:9"].get("snap") is True, pads.get("note:9"))
check("one track + m:ss time kept", pads.get("note:10") == {"do": "grab", "scope": "track", "track": 2,
                                                         "len": "time", "secs": 720}, pads.get("note:10"))
check("fixed tracks are de-duplicated and sorted", pads.get("note:11", {}).get("tracks") == [2, 3],
      pads.get("note:11"))
check("an unknown length is refused + reported", "note:12" not in pads and any("length" in x for x in e), e)
check("0 bars is refused", "note:13" not in pads)
c2, e2 = conf_with({"note:8": {"do": "grab", "len": "bars", "bars": 4}}, surface=True, name="Fire", match="ORC FW")
check("an old Fire SURFACE profile is dropped whole and reported (its entries were pad numbers, not notes)",
      "Fire" not in c2["devices"] and any("Fire" in x and "surface" in x for x in e2), (c2, e2))
both = P.validate_config({"devices": {"Fire": {"port_match": "ORC FW", "surface": True, "pads": {}},
                                      "Simmons": {"port_match": "Simmons",
                                                  "pads": {"note:36": {"do": "grab", "len": "bars", "bars": 4}}}}}, MODS)[0]
check("... and the other profiles in the same file are kept",
      list(both["devices"]) == ["Simmons"] and "note:36" in both["devices"]["Simmons"]["pads"], both)
c4, e4 = conf_with({"cc:4": {"do": "grab", "len": "bars", "bars": 4}})
check("a CC can't be a grab pad", "cc:4" not in c4["devices"]["Pads"]["pads"] and e4)

# ── grab_plan: the math ───────────────────────────────────────────────────────
G = P.grab_plan
bars4 = {"scope": "all", "tracks": "armed", "len": "bars", "bars": 4}
p = G(bars4, 1000.0, None, 1000.5, 120, 4)
check("4 bars at 120 BPM, 4/4 = 8.0 s, ending at the PRESS, no wait, armed tracks",
      p == (8.0, 1000.0, 0.0, None), p)
p = G(dict(bars4, bars=1), 1000.0, None, 1000.0, 90, 3)
check("1 bar of 3/4 at 90 BPM = 2.0 s", abs(p[0] - 2.0) < 1e-9, p)
p = G({"scope": "track", "track": 5, "len": "time", "secs": 720}, 50.0, None, 50.0, 120)
check("time 12:00 = 720 s of track 5 only", p == (720.0, 50.0, 0.0, [5]), p)
p = G({"scope": "all", "tracks": [1, 2], "len": "all"}, 50.0, None, 50.0, 120)
check("everything = 0 s (the grab tool's 'all held in common') of the fixed tracks", p == (0.0, 50.0, 0.0, [1, 2]), p)
hold = {"scope": "all", "tracks": "armed", "len": "hold", "snap": True}
check("a hold not yet released grabs nothing", G(hold, 10.0, None, 11.0, 120) is None)
p = G(hold, 10.0, 16.3, 16.3, 120, 4)          # 6.3 s held, bar 2.0 s -> 3 bars
check("snapped hold rounds to whole bars FROM THE PRESS (6.3 s -> 3 bars = 6 s, ends before the release)",
      p == (6.0, 16.0, 0.0, None), p)
p = G(hold, 10.0, 17.2, 17.2, 120, 4)          # 7.2 s -> 4 bars = 8 s, ends at 18.0 (future)
check("a hold snapped UP waits until its window has been recorded",
      abs(p[0] - 8.0) < 1e-9 and abs(p[1] - 18.0) < 1e-9 and abs(p[2] - 0.8) < 1e-9, p)
p = G(hold, 10.0, 10.3, 10.3, 120, 4)
check("a sub-bar hold still grabs one whole bar", abs(p[0] - 2.0) < 1e-9, p)
p = G(dict(hold, snap=False), 10.0, 13.25, 13.25, 120)
check("an unsnapped hold grabs exactly press -> release", p == (3.25, 13.25, 0.0, None), p)
p = G(bars4, 0.0, None, 0.0, 0)
check("no tempo -> 120 BPM, never a zero-length grab", p[0] == 8.0, p)

# ── PadMap: press / hold / release from real notes ───────────────────────────
grabs = []
pm = P.PadMap(emit=lambda *a: None, module_ch=lambda m: 10, run_action=lambda a: None, log=lambda s: None,
              run_grab=lambda e, a, b: grabs.append((e["len"], a, b)))
pm.spawn_grab = lambda fn: fn()                 # synchronous for the test
clk = [100.0]
pm.wall = lambda: clk[0]
mono = [0.0]
pm.clock = lambda: mono[0]
pm.set_config(c)
check("a bars pad grabs on the HIT", pm.handle(SX(8), PADS_IN) is True and grabs == [("bars", 100.0, None)], grabs)
mono[0] += 0.01
pm.handle(SX(8), PADS_IN)
check("a double-trigger 10 ms later is not a 2nd grab", len(grabs) == 1, grabs)
pm.handle(SX(8, 0), PADS_IN)
check("its release does nothing", len(grabs) == 1)
mono[0] += 1.0
pm.handle(SX(8), PADS_IN)
check("a real 2nd hit a second later is a 2nd grab", len(grabs) == 2)
del grabs[:]
clk[0] = 200.0
pm.handle(SX(9), PADS_IN)
check("a HOLD press grabs nothing yet", grabs == [], grabs)
clk[0] = 203.0
pm.handle(SX(9), PADS_IN)
clk[0] = 207.5
pm.handle(SX(9, 0), PADS_IN)
check("the RELEASE grabs press -> release; a 2nd press while held did not restart it",
      grabs == [("hold", 200.0, 207.5)], grabs)
pm.handle(SX(9, 0), PADS_IN)
check("a stray release with no press grabs nothing", len(grabs) == 1)
pm.handle(SX(9), PADS_IN)
pm.set_config(c)
pm.handle(SX(9, 0), PADS_IN)
check("a remap mid-hold drops the hold (its release grabs nothing)", len(grabs) == 1, grabs)
lowc, _ = conf_with({"note:8": {"do": "grab", "len": "bars", "bars": 4, "min_vel": 60}})
pm.set_config(lowc)
mono[0] += 5
del grabs[:]
pm.handle(SX(8, 30), PADS_IN)
check("a hit softer than its min velocity doesn't grab", grabs == [])
sx = type("O", (), {"type": "sysex", "data": [0x7D, 0x4F, 0x52, 0x43, 0x0C, 8, 100]})()
check("the Fire's old surface-hit SysEx (0C) is nobody's pad any more: not consumed, no grab",
      pm.handle(sx, PADS_IN) is False and grabs == [], grabs)
check("the surface code is gone from the pad map (no colours, no 0A / 0B frames, no hold light)",
      not any(hasattr(P, n) for n in ("COLORS", "COLOR_RGB", "surface_frames", "surface_on", "surface_colours",
                                      "surface_off", "SURF_PAD_HDR", "tap_pads", "TAP_ACTION"))
      and not hasattr(pm, "surfaces") and not hasattr(pm, "on_hold"))

# ── the brain: _padmap_grab and the wiring (by source) ───────────────────────
src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
tree = ast.parse(src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_padmap_grab")
calls, timers = [], []


class _T:
    def __init__(s, delay, f, args=(), kwargs=None):
        timers.append((delay, f, args, kwargs or {}))

    def start(s):
        pass


ns = {"_orc1_padmap": P, "time": type("t", (), {"time": staticmethod(lambda: 300.0)}),
      "sampler_bpm": lambda: 120.0, "_SMP_BEATS_BAR": 4,
      "skipback_save_all": lambda *a, **k: calls.append((a, k)),
      "threading": type("th", (), {"Timer": _T}), "print": lambda *a, **k: None}
exec(compile(ast.Module([fn], []), "orc1.py", "exec"), ns)
ns["_padmap_grab"](bars4, 299.0, None)
check("brain: a bars grab calls skipback_save_all(8 s, armed) ENDING AT THE PRESS",
      calls == [((8.0, None), {"end_ts": 299.0})], calls)
ns["_padmap_grab"]({"scope": "track", "track": 2, "len": "hold", "snap": True}, 295.0, 300.2)
check("brain: a snapped-UP hold is scheduled, not run early",
      len(calls) == 1 and timers and abs(timers[-1][0] - 1.05) < 1e-6
      and timers[-1][2] == (6.0, [2]) and timers[-1][3] == {"end_ts": 301.0}, (calls, timers))
check("brain: the pad map is given the grab callback",
      "padmap.run_grab = _padmap_grab" in src)
check("brain: nothing of the Fire surface link is left (no link, no blink thread, no SURFACE_SHOW)",
      not any(w in src for w in ("surface_link", "_tap_blink_loop", "_surface_loop", "SURFACE_SHOW", "on_hold"))
      and not os.path.exists(os.path.join(HERE, "orc1_surface_link.py")))
sa = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "skipback_save_all")
sa_src = ast.get_source_segment(src, sa)
check("brain: skipback_save_all ends at end_ts and passes fractional seconds",
      "float(end_ts) if end_ts is not None else time.time()" in sa_src and '"%.6f" % max(0.0, float(seconds))' in sa_src)

# ── the page offers it ───────────────────────────────────────────────────────
js = open(os.path.join(HERE, "static", "js", "pads.js"), encoding="utf-8").read()
html = open(os.path.join(HERE, "templates", "pads.html"), encoding="utf-8").read()
web = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
check("/pads offers 'skipback grab' and 'while held' on any device",
      '["grab", "skipback grab"]' in js and '["hold", "while held"]' in js)
check("/pads has no surface controls left (no tick box, no SHOW ON FIRE, no colour picker, no show route)",
      not any(w in js + html for w in ("surface", "pm-show", "SHOW ON FIRE", "colorCell", "pm-grid"))
      and "/api/padmap/show" not in web and "_pm.COLORS" not in web)

if __name__ == "__main__":
    check("every check ran (%d expected)" % EXPECTED, PASS + FAIL + 1 == EXPECTED,
          "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
