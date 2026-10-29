#!/usr/bin/env python3
"""
Skipback GRAB pads + the blinking TAP TEMPO pad (2026-10-19).

    python3 _test_grab_pads.py [-v]

owner: trigger a skipback grab of a length he set up "with a press of a fire pad in
orc mode"; "the sample is usually going to be either quantized bar amounts or a 'sample
while holding the button down'"; seconds don't fit long grabs; and "a blinking tap
tempo pad for the clock".

Covers: orc1_padmap grab validation + grab_plan (bars at the live tempo end at the press,
hold = press -> release snapped to whole bars from the press, time, everything, tracks),
PadMap press / hold / release on a surface, the one-pad paint frame against the Fire
firmware's own parse rules, TapBlinker against a fake clock, and the brain's
_padmap_grab (extracted from orc1.py by source) with skipback_save_all stubbed.

Pure: no port, no JACK, no file outside a temp dir, never runs a grab.
"""
import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_padmap as P                                      # noqa: E402
import orc1_surface_link as L                                # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 50
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
FIRE_IN = "ORC FW seq:ORC FW seq MIDI 1 32:0"


class SX:                     # a surface pad hit: F0 7D 'ORC' 0C <pad> <vel> F7
    def __init__(s, pad, vel=100):
        s.type, s.data = "sysex", P.SURF_PAD_HDR + [pad, vel]


def conf_with(pads, surface=True, name="Fire", match="ORC FW"):
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
pads = c["devices"]["Fire"]["pads"]
check("bars grab kept with its length + colour",
      pads.get("note:8") == {"do": "grab", "scope": "all", "tracks": "armed", "len": "bars",
                             "bars": 8, "color": "blue"}, pads.get("note:8"))
check("hold on a surface is kept, snap defaults ON",
      pads.get("note:9", {}).get("len") == "hold" and pads["note:9"].get("snap") is True, pads.get("note:9"))
check("one track + m:ss time kept", pads.get("note:10") == {"do": "grab", "scope": "track", "track": 2,
                                                         "len": "time", "secs": 720}, pads.get("note:10"))
check("fixed tracks are de-duplicated and sorted", pads.get("note:11", {}).get("tracks") == [2, 3],
      pads.get("note:11"))
check("an unknown length is refused + reported", "note:12" not in pads and any("length" in x for x in e), e)
check("0 bars is refused", "note:13" not in pads)
c2, e2 = conf_with({"note:36": {"do": "grab", "len": "hold"}}, surface=False, name="Simmons", match="Simmons")
check("a HOLD grab on a non-surface (Simmons: timer note-offs) is refused with the reason",
      "note:36" not in c2["devices"]["Simmons"]["pads"] and any("surface" in x for x in e2), e2)
c3, _ = conf_with({"note:36": {"do": "grab", "len": "bars", "bars": 4}}, surface=False, name="Simmons", match="Simmons")
check("a BARS grab on a non-surface is fine", "note:36" in c3["devices"]["Simmons"]["pads"])
c4, e4 = conf_with({"cc:4": {"do": "grab", "len": "bars", "bars": 4}})
check("a CC can't be a grab pad", "cc:4" not in c4["devices"]["Fire"]["pads"] and e4)

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

# ── PadMap: press / hold / release on the surface ────────────────────────────
grabs, holds = [], []
pm = P.PadMap(emit=lambda *a: None, module_ch=lambda m: 10, run_action=lambda a: None, log=lambda s: None,
              run_grab=lambda e, a, b: grabs.append((e["len"], a, b)),
              on_hold=lambda d, pad, h: holds.append((d, pad, h)))
pm.spawn_grab = lambda fn: fn()                 # synchronous for the test
clk = [100.0]
pm.wall = lambda: clk[0]
mono = [0.0]
pm.clock = lambda: mono[0]
pm.set_config(c)
check("a bars pad grabs on the HIT", pm.handle(SX(8), FIRE_IN) is True and grabs == [("bars", 100.0, None)], grabs)
mono[0] += 0.01
pm.handle(SX(8), FIRE_IN)
check("a double-trigger 10 ms later is not a 2nd grab", len(grabs) == 1, grabs)
pm.handle(SX(8, 0), FIRE_IN)
check("its release does nothing", len(grabs) == 1)
mono[0] += 1.0
pm.handle(SX(8), FIRE_IN)
check("a real 2nd hit a second later is a 2nd grab", len(grabs) == 2)
del grabs[:]
clk[0] = 200.0
pm.handle(SX(9), FIRE_IN)
check("a HOLD press grabs nothing yet and lights the pad", grabs == [] and holds == [("Fire", 9, True)], (grabs, holds))
pm.handle(SX(9), FIRE_IN)
check("a 2nd press while held does not restart it", len(holds) == 1)
clk[0] = 207.5
pm.handle(SX(9, 0), FIRE_IN)
check("the RELEASE grabs press -> release and puts the pad back",
      grabs == [("hold", 200.0, 207.5)] and holds[-1] == ("Fire", 9, False), (grabs, holds))
pm.handle(SX(9, 0), FIRE_IN)
check("a stray release with no press grabs nothing", len(grabs) == 1)
pm.handle(SX(9), FIRE_IN)
pm.set_config(c)
check("a remap mid-hold drops the hold and un-lights the pad (no grab)",
      len(grabs) == 1 and holds[-1] == ("Fire", 9, False), (grabs, holds))
lowc, _ = conf_with({"note:8": {"do": "grab", "len": "bars", "bars": 4, "min_vel": 60}})
pm.set_config(lowc)
mono[0] += 5
del grabs[:]
pm.handle(SX(8, 30), FIRE_IN)
check("a hit softer than its min velocity doesn't grab", grabs == [])

# ── tap pads + the one-pad paint frame ───────────────────────────────────────
tc, _ = conf_with({"note:15": {"do": "action", "action": P.TAP_ACTION, "color": "orange"},
                   "note:14": {"do": "action", "action": "cmd:ARP_TOGGLE"},
                   "note:3": {"do": "action", "action": P.TAP_ACTION}})
check("tap_pads finds the tap-tempo pads only", P.tap_pads(tc["devices"]["Fire"]) == [3, 15],
      P.tap_pads(tc["devices"]["Fire"]))


class Out:
    def __init__(s):
        s.sent = []

    def send(s, m):
        s.sent.append(m.data)


class FakeMido:
    class Message:
        def __init__(s, kind, data=()):
            s.type, s.data = kind, list(data)


link = L.SurfaceLink(FakeMido, lambda: [], lambda: {}, log=lambda s: None)
check("paint_pad on a Fire that isn't linked says False", link.paint_pad("Fire", 3, (7, 0, 0)) is False)
out = Out()
link.links["Fire"] = {"out": out}
link.paint_pad("Fire", 63, (9, -1, 4))
f = out.sent[-1] if out.sent else []
check("paint_pad sends ONE 0A pad: <pad> count 1, levels clamped to 0-7",
      f == P.ORC_HDR + [0x0A, 63, 1, 7, 0, 4], f)
check("... a frame the firmware accepts (first + count <= 64, fits the 40-byte buffer)",
      f[5] + f[6] <= 64 and len(f) + 2 < 40)

# ── TapBlinker ────────────────────────────────────────────────────────────────


class PaintLink:
    def __init__(s):
        s.painted, s.calls = 0, []

    def paint_pad(s, name, pad, rgb):
        s.calls.append((pad, tuple(rgb)))
        return True


pl = PaintLink()
tb = L.TapBlinker(pl, lambda: [("Fire", tc["devices"]["Fire"])])
check("refresh() sees the tap pads", tb.refresh() is True)
ORANGE = P.COLOR_RGB["orange"]
tb.tick(0.0, None, None, False)
check("no clock: both pads sit at their colour, DIM (green default for the uncoloured one)",
      sorted(pl.calls) == [(3, L._dim(L.DEFAULT_TAP_RGB)), (15, L._dim(ORANGE))], pl.calls)
n = len(pl.calls)
tb.tick(0.01, None, None, False)
check("nothing is re-sent while nothing changes", len(pl.calls) == n)
tb.tick(1.00, (0.30, 0.5), (1.30, 2.0), False)      # clock runs: first reading, no flash yet
tb.tick(1.25, (0.005, 0.5), (1.505, 2.0), False)    # the phase wrapped: a beat (not beat 1)
check("a new beat flashes the pad at its FULL colour", (15, ORANGE) in pl.calls[n:], pl.calls[n:])
m = len(pl.calls)
tb.tick(1.25 + L.FLASH_S + 0.01, (0.10, 0.5), (1.60, 2.0), False)
check("the flash ends after FLASH_S (back to dim)", (15, L._dim(ORANGE)) in pl.calls[m:], pl.calls[m:])
m = len(pl.calls)
tb.tick(1.75, (0.004, 0.5), (0.004, 2.0), False)    # wrapped at the bar line
check("beat 1 of the bar flashes WHITE", (15, (7, 7, 7)) in pl.calls[m:], pl.calls[m:])
tb.tick(2.0, (0.25, 0.5), (0.25, 2.0), True)        # mid-beat, now following an outside clock
m = len(pl.calls)
tb.tick(2.25, (0.004, 0.5), (0.504, 2.0), True)
check("following an OUTSIDE clock the blink is grey (a tap does nothing then)",
      (15, (3, 3, 3)) in pl.calls[m:] and not any(c[1] == ORANGE for c in pl.calls[m:]), pl.calls[m:])
m = len(pl.calls)
pl.painted += 1                                      # a full repaint overwrote the pads
tb.tick(2.25 + 0.02, (0.03, 0.5), (0.53, 2.0), True)
check("after a full repaint the blinker re-sends what it shows", len(pl.calls) > m)
pl2 = PaintLink()
tb2 = L.TapBlinker(pl2, lambda: [("Fire", c["devices"]["Fire"])])
tb2.refresh()
tb2.tick(0.0, (0.0, 0.5), (0.0, 2.0), False)
check("no tap pad = the blinker sends nothing at all", pl2.calls == [])
check("tap_colour is pure: inside 0-7", all(0 <= v <= 7 for fl in (0, 1) for db in (0, 1) for ex in (0, 1)
                                            for v in L.tap_colour((7, 2, 0), fl, db, ex)))

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
check("brain: the pad map is given the grab + hold-light callbacks",
      "padmap.run_grab = _padmap_grab" in src and "padmap.on_hold = _padmap_hold_light" in src)
check("brain: the tap-blink thread is started",
      'threading.Thread(target=_tap_blink_loop' in src)
sa = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "skipback_save_all")
sa_src = ast.get_source_segment(src, sa)
check("brain: skipback_save_all ends at end_ts and passes fractional seconds",
      "float(end_ts) if end_ts is not None else time.time()" in sa_src and '"%.6f" % max(0.0, float(seconds))' in sa_src)

# ── the page offers it ───────────────────────────────────────────────────────
js = open(os.path.join(HERE, "static", "js", "pads.js"), encoding="utf-8").read()
check("/pads offers 'skipback grab' and 'while held' only on a surface",
      '["grab", "skipback grab"]' in js and 'if (surface) lens.splice(1, 0, ["hold", "while held"])' in js)

if __name__ == "__main__":
    check("every check ran (%d expected)" % EXPECTED, PASS + FAIL + 1 == EXPECTED,
          "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
