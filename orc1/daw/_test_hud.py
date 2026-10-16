#!/usr/bin/env python3
"""Regression suite for the HUD kiosk renderer (daw/orc_hud.py).

Run on the Pi (needs pygame + numpy): `python3 -B daw/_test_hud.py`.
No display is opened: drawing goes to an in-memory pygame.Surface. The model
code (Feed / Pulses / bar_pos / beat_flash) is tested without pygame at all.
Layout claims are checked on rendered PIXELS, by diffing two renders that
differ in exactly one thing.
"""
import json
import os
import re
import shutil
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc_hud as H   # noqa: E402

EXPECTED = 64
_pass = _fail = 0
_tmp = tempfile.mkdtemp(prefix="orc1_hud_test_")
W, HT = 1280, 720


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL: {name} {detail}")


def state(**kw):
    s = {
        "v": 1, "t": 1000.0, "n": 1,
        "transport": {"running": False, "bpm": 120.0, "clock": "internal", "step": -1,
                      "bar_step": -1, "age_ms": 0, "step_ms": 125.0, "rec": False},
        "chord": {"name": "MAJ", "key": "C", "scale": "CHROM", "degree": 0,
                  "preset": "MONO_CLEAN", "octave": 4},
        "arp": {"on": False, "pattern": "UP", "rate": "1/8"},
        "held": [], "bb": {},
        "tracks": [{"name": "kick", "len": 16, "pos": 0, "mute": False, "steps": "0" * 16},
                   {"name": "snare", "len": 16, "pos": 0, "mute": False, "steps": "0" * 16}],
        "bindings": {"poly": 6, "drums": 10, "chip_p1": 9},
        "mods": {},
    }
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k] = {**s[k], **v}
        else:
            s[k] = v
    return s


def write(path, s):
    tmp = path + ".t"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, path)


class Rig:
    """A Hud over a temp feed file, rendering to a Surface."""

    def __init__(self, s=None, name="feed.json"):
        import pygame
        self.pg = pygame
        self.path = os.path.join(_tmp, name)
        if s is not None:
            write(self.path, s)
        self.hud = H.Hud((W, HT), self.path)
        self.surf = pygame.Surface((W, HT))
        self.mono = 50.0
        self.wall = 1000.5          # 0.5 s after the state's t

    def render(self, mono=None, wall=None):
        self.mono = self.mono if mono is None else mono
        self.wall = self.wall if wall is None else wall
        self.hud.tick(self.mono, self.wall)
        self.hud.draw(self.surf, self.mono, self.wall)
        import numpy as np
        return np.array(self.pg.surfarray.array3d(self.surf)).transpose(1, 0, 2).copy()  # (H, W, 3)

    def set(self, s):
        write(self.path, s)


def diff_box(a, b):
    import numpy as np
    d = (a != b).any(axis=2)
    if not d.any():
        return None
    ys, xs = np.where(d)
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()), int(d.sum()))


def count(img, fn, region=None):
    import numpy as np
    if region:
        x0, y0, x1, y1 = region
        img = img[y0:y1, x0:x1]
    r, g, b = (img[..., i].astype(int) for i in range(3))
    return int(fn(r, g, b).sum())


is_green = lambda r, g, b: (g > 180) & (r < 120) & (b < 170)
is_red = lambda r, g, b: (r > 200) & (g < 110) & (b < 110)
is_cyan = lambda r, g, b: (b > 150) & (g > 130) & (r < 120)


def main():
    u = HT / 720.0
    top, foot = int(128 * u), HT - int(66 * u)
    strip = foot - int(76 * u)

    # ---------------- pure model ----------------
    check("bar_pos step 0 = 1.1.1", H.bar_pos(0) == (1, 1, 1))
    check("bar_pos wraps the bar at 16", H.bar_pos(16) == (2, 1, 1) and H.bar_pos(17) == (2, 1, 2))
    check("bar_pos last step of bar 4", H.bar_pos(63) == (4, 4, 4))
    check("bar_pos stopped = None", H.bar_pos(-1) is None and H.bar_pos(None) is None)

    tr = {"running": True, "step": 4, "step_ms": 125.0}
    check("beat_flash full on a beat", abs(H.beat_flash(tr, 0.0) - 1.0) < 1e-9)
    check("beat_flash decays within the beat",
          0.0 < H.beat_flash({**tr, "step": 5}, 0.0) < H.beat_flash(tr, 0.05) < 1.0)
    check("beat_flash zero when stopped",
          H.beat_flash({**tr, "running": False}, 0.0) == 0.0
          and H.beat_flash({**tr, "step": -1}, 0.0) == 0.0)

    p = H.Pulses()
    p.update({"poly": {"hits": 5, "vel": 100}}, 0.0)
    check("pulses: first sight is history, not a hit", p.get("poly") == 0.0)
    p.update({"poly": {"hits": 6, "vel": 127}}, 0.1)
    check("pulses: a new hit lights the module", p.get("poly") >= 0.99)
    p.update({"poly": {"hits": 6, "vel": 127}}, 0.1 + 0.22 * 0.6931)
    check("pulses: fade has a ~0.22 s time constant", 0.45 < p.get("poly") < 0.55)
    p.update({"poly": {"hits": 6, "vel": 127}}, 5.0)
    check("pulses: fully faded = inactive", not p.active())
    p.update({"poly": {"hits": 1, "vel": 60}}, 5.1)
    check("pulses: brain restart (count reset) still flashes", p.get("poly") > 0.3)
    p.update({"poly": {"hits": 2, "vel": 1}}, 9.0)
    check("pulses: soft hit is dimmer than a hard one", 0.3 < p.get("poly") < 0.5)

    check("module_order by channel then name",
          H.module_order({"poly": 6, "drums": 10, "mono": 3, "chip_p1": 6})
          == [("mono", 3), ("chip_p1", 6), ("poly", 6), ("drums", 10)])

    # ---------------- feed ----------------
    fp = os.path.join(_tmp, "f.json")
    f = H.Feed(fp)
    f.poll(1.0)
    check("feed: no file = no state", f.state is None and f.age(1000.0) is None
          and f.is_stale(1000.0))
    write(fp, state())
    f.poll(2.0)
    check("feed: reads a new file", f.state and f.changed and f.recv == 2.0)
    f.poll(3.0)
    check("feed: unchanged mtime is not re-read", not f.changed and f.recv == 2.0)
    check("feed: fresh vs stale by the brain's own timestamp",
          not f.is_stale(1001.0) and f.is_stale(1000.0 + H.STALE_S + 0.1))
    hb = state(t=1000.9, n=2)                     # identical content, newer stamp + count
    write(fp, hb)
    f.poll(3.5)
    check("feed: a heartbeat-only rewrite is not a change (screen can idle)",
          not f.changed and f.recv == 2.0)
    check("feed: ...but the stale clock still follows the brain's stamp",
          abs(f.age(1001.0) - 0.1) < 1e-6)
    with open(fp, "w") as fh:
        fh.write("{ torn json")
    os.utime(fp, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    f.poll(4.0)
    check("feed: bad JSON keeps the last good frame", f.state is not None and f.state["n"] == 1)
    os.unlink(fp)
    f.poll(5.0)
    check("feed: file removed = back to no state", f.state is None and f.changed)

    # ---------------- rendering (pixels) ----------------
    r = Rig()
    img = r.render()
    check("render: no feed shows a red NO SIGNAL message", count(img, is_red) > 200)

    r = Rig(state())
    base = r.render()
    check("render: a live stopped frame has no red", count(base, is_red) == 0)
    check("render: stopped = no green (PLAY badge off)", count(base, is_green) == 0)

    r.set(state(t=1000.0, n=2, transport={"running": True, "step": 0}))
    run = r.render()
    check("render: running lights the green PLAY badge", count(run, is_green) > 100)

    stale = r.render(wall=1000.0 + H.STALE_S + 5)
    check("render: a dead brain overlays the red banner", count(stale, is_red) > 200)

    # playhead: only the track cells change, and only two columns' worth
    r.set(state(n=3, transport={"running": True, "step": 0},
                tracks=[{"name": "kick", "len": 16, "pos": 0, "mute": False, "steps": "0" * 16}]))
    a = r.render()
    r.set(state(n=4, transport={"running": True, "step": 1},
                tracks=[{"name": "kick", "len": 16, "pos": 1, "mute": False, "steps": "0" * 16}]))
    b = r.render(mono=50.0)
    box = diff_box(a, b)
    cw = (W - 2 * int(16 * u) - int(112 * u)) / 16
    check("playhead: moving it changes pixels", box is not None)
    below = diff_box(a[top:], b[top:])      # the header's step counter also moves, by design
    check("playhead: below the header, only the tracks band changes",
          below and below[3] + top <= strip, f"below={below} top={top} strip={strip}")
    # the header's step counter + beat lamps also change; require the track part:
    tb = diff_box(a[top:strip], b[top:strip])
    check("playhead: spans exactly the two cells it left and entered",
          tb and (tb[2] - tb[0]) < 2 * cw + 4, f"tb={tb} cw={cw:.1f}")

    # step on vs off in a row
    r.set(state(n=5, tracks=[{"name": "kick", "len": 16, "pos": 0, "mute": False,
                              "steps": "1" * 16}]))
    on = r.render()
    r.set(state(n=6, tracks=[{"name": "kick", "len": 16, "pos": 0, "mute": False,
                              "steps": "0" * 16}]))
    off = r.render()
    band = (0, top, W, top + int(46 * u))
    check("steps: lit steps are brighter than empty ones",
          on[band[1]:band[3]].sum() > off[band[1]:band[3]].sum() * 1.3)
    r.set(state(n=7, tracks=[{"name": "kick", "len": 16, "pos": 0, "mute": True,
                              "steps": "1" * 16}]))
    muted = r.render()
    check("steps: a muted track is dimmed",
          muted[band[1]:band[3]].sum() < on[band[1]:band[3]].sum() * 0.8)

    # module pulse
    r.set(state(n=8, mods={"poly": {"hits": 1, "vel": 100}}))
    r.render(mono=60.0)                                   # first sight (history)
    quiet = r.render(mono=60.05)
    r.set(state(n=9, mods={"poly": {"hits": 2, "vel": 100}}))
    hit = r.render(mono=60.1)
    zone = (0, strip, W, foot)
    check("modules: a hit lights its box",
          count(hit, is_cyan, zone) > count(quiet, is_cyan, zone) + 300)
    gone = r.render(mono=63.0)
    check("modules: the light fades back out",
          count(gone, is_cyan, zone) <= count(quiet, is_cyan, zone) + 5)

    # held notes
    r.set(state(n=10, held=["C4", "E4", "G4"]))
    held = r.render(mono=70.0)
    r.set(state(n=11, held=[]))
    nohold = r.render(mono=70.0)
    fb = diff_box(held[foot:], nohold[foot:])
    check("footer: held notes are drawn in the footer", fb and fb[4] > 300)

    # arp + bb (separately: one must not be able to vouch for the other)
    r.set(state(n=12, arp={"on": True}))
    arp_on = r.render(mono=71.0)
    r.set(state(n=13, arp={"on": False}))
    arp_off = r.render(mono=71.0)
    fz = (0, foot, W, HT)
    check("footer: ARP goes green when on",
          count(arp_on, is_green, fz) > count(arp_off, is_green, fz) + 100)
    r.set(state(n=14, bb={"playing": True, "part": 2}))
    bb_on = r.render(mono=71.0)
    check("footer: BeatBuddy goes green when playing",
          count(bb_on, is_green, fz) > count(arp_off, is_green, fz) + 100)

    # worst-case size renders, and quickly enough
    big = state(n=15, transport={"running": True, "step": 7},
                tracks=[{"name": f"t{i}", "len": 64, "pos": i, "mute": i % 5 == 0,
                         "steps": "10" * 32} for i in range(16)],
                bindings={f"m{i}": i + 1 for i in range(13)},
                mods={f"m{i}": {"hits": 1, "vel": 100} for i in range(13)})
    r.set(big)
    import time as _t
    r.render(mono=80.0)
    t0 = _t.perf_counter()
    for k in range(5):
        r.render(mono=80.0 + k * 0.03)
    per = (_t.perf_counter() - t0) / 5
    check("worst case (16 x 64 steps, 13 modules) renders", per < 0.25, f"{per*1000:.0f} ms")

    # tick(): fast while anything moves, slow when idle
    r.set(state(n=14, transport={"running": True, "step": 2}))
    check("tick: running = animate fast", r.hud.tick(90.0, 1000.0) is True)
    r.set(state(n=15, transport={"running": False}))
    r.hud.tick(100.0, 1000.0)
    check("tick: idle after 0.6 s = slow", r.hud.tick(101.0, 1000.0) is False)

    # narrow screens: nothing may overflow its slot (the Pi falls back to 1024x768 with no monitor)
    import pygame
    narrow = H.Hud((1024, 768), os.path.join(_tmp, "narrow.json"))
    w1 = narrow.font(22).size("NOISE2")[0]
    fitted = narrow.fit("NOISE2", 22, w1 - 15)
    check("fit: shrinks text to the slot, never grows it",
          fitted < 22 and narrow.font(fitted).size("NOISE2")[0] <= w1 - 15
          and narrow.fit("OK", 22, 500) == 22)
    write(os.path.join(_tmp, "narrow.json"), big)
    narrow.tick(1.0, 1000.5)
    narrow.draw(pygame.Surface((1024, 768)), 1.0, 1000.5)
    check("render: the worst-case frame also draws on a 1024x768 screen", True)

    # redraw bookkeeping: an idle, unchanged screen is never redrawn
    q = Rig(state(n=20), name="idle.json")
    q.hud.tick(10.0, 1000.5)
    check("idle: first sight draws", q.hud.redraw)
    q.hud.tick(11.0, 1000.5)
    q.hud.tick(12.0, 1000.5)
    check("idle: nothing moving, nothing new = no redraw", q.hud.redraw is False)
    write(q.path, state(n=21, t=1000.8))              # heartbeat only
    q.hud.tick(13.0, 1000.9)
    check("idle: a heartbeat does not force a redraw", q.hud.redraw is False)
    q.hud.tick(14.0, 1000.0 + H.STALE_S + 1)
    check("idle: going stale redraws once (banner)", q.hud.redraw is True)
    q.hud.tick(15.0, 1000.0 + H.STALE_S + 1)
    check("idle: ...and only once", q.hud.redraw is False)

    # per-region repainting: only what changed is repainted (and flipped to X)
    run_s = state(n=30, transport={"running": True, "step": 4, "age_ms": 0})
    z = Rig(run_s, name="regions.json")
    z.hud.tick(100.0, 1000.5)
    check("regions: new content repaints everything", z.hud.dirty == set(H.REGIONS))
    z.hud.tick(100.2, 1000.5)
    check("regions: only the beat lamps fading = header only", z.hud.dirty == {"hdr"})
    z.hud.tick(100.9, 1000.5)
    check("regions: the header repaints once MORE after the flash ends (last fade lands)",
          "hdr" in z.hud.dirty)
    z.hud.tick(101.0, 1000.5)
    check("regions: between beats, mid-pattern, nothing is repainted", z.hud.dirty == set())
    write(z.path, state(n=31, transport={"running": True, "step": 4}, mods={"poly": {"hits": 1, "vel": 90}}))
    z.hud.tick(102.0, 1000.6)                     # new content incl. first sight of a module
    write(z.path, state(n=32, transport={"running": True, "step": 4}, mods={"poly": {"hits": 2, "vel": 90}}))
    z.hud.tick(103.0, 1000.6)
    z.hud.tick(103.1, 1000.6)
    check("regions: a module pulse fading repaints the module strip only",
          "mod" in z.hud.dirty and "trk" not in z.hud.dirty and "ftr" not in z.hud.dirty)

    # each region gets one more repaint after ITS motion stops, so the last fade lands
    pz = Rig(state(n=50, mods={"poly": {"hits": 1, "vel": 100}}), name="fade.json")
    pz.hud.tick(300.0, 1000.5)                    # first sight (history)
    write(pz.path, state(n=51, mods={"poly": {"hits": 2, "vel": 100}}))
    pz.hud.tick(300.1, 1000.5)                    # the hit
    pz.hud.tick(300.2, 1000.5)
    check("fade: module strip repaints while the light is up", "mod" in pz.hud.dirty)
    pz.hud.tick(310.0, 1000.5)                    # fully faded -> pulse inactive now
    check("fade: ...and once MORE after it goes out (the last fade lands)", "mod" in pz.hud.dirty)
    pz.hud.tick(310.1, 1000.5)
    check("fade: ...then never again", pz.hud.dirty == set())

    # fades are throttled to ~15 fps, but never LOST: the wait carries over
    th = Rig(state(n=60, transport={"running": True, "step": 4}), name="throttle.json")
    th.hud.tick(400.0, 1000.5)                    # content: everything, anim clock resets
    th.hud.tick(400.02, 1000.5)                   # flash is up, but 20 ms is too soon
    check("throttle: a fade repaint inside the animation interval waits", th.hud.dirty == set())
    th.hud.tick(400.09, 1000.5)
    check("throttle: ...and is delivered at the next slot, not dropped", th.hud.dirty == {"hdr"})
    th.hud.tick(400.1, 1000.5)
    check("throttle: new DATA is never throttled",
          (write(th.path, state(n=61, transport={"running": True, "step": 5})) or True)
          and th.hud.tick(400.11, 1000.5) is not None and th.hud.dirty == set(H.REGIONS))

    # a partial draw touches ONLY its own rectangle
    zr = Rig(state(n=40, chord={"name": "AAA"}, held=["C3"]), name="part.json")
    full1 = zr.render(mono=200.0)
    zr.set(state(n=41, chord={"name": "BBB"}, held=["D3", "F3"]))
    zr.hud.tick(200.5, 1000.5)
    check("regions: a LATER content change (not just the first frame) repaints everything",
          zr.hud.dirty == set(H.REGIONS))
    import numpy as _np
    zr.hud.draw(zr.surf, 200.5, 1000.5, {"hdr"})
    part = _np.array(zr.pg.surfarray.array3d(zr.surf)).transpose(1, 0, 2)
    full2 = Rig(state(n=41, chord={"name": "BBB"}, held=["D3", "F3"]), name="part2.json").render(mono=200.5)
    hx = zr.hud.rect("hdr")
    fx = zr.hud.rect("ftr")
    check("partial draw: the header is repainted to the new state",
          (part[hx[1]:hx[1] + hx[3]] == full2[hx[1]:hx[1] + hx[3]]).all())
    check("partial draw: the footer is left exactly as it was",
          (part[fx[1]:] == full1[fx[1]:]).all() and not (full1[fx[1]:] == full2[fx[1]:]).all())
    rects = zr.hud.draw(zr.surf, 200.5, 1000.5, {"mod", "ftr"})
    check("draw returns exactly the rectangles it painted",
          sorted(map(tuple, rects)) == sorted([zr.hud.rect("mod"), zr.hud.rect("ftr")]))
    zr.hud.draw(zr.surf, 200.5, 1000.5, None)
    check("regions tile the screen without gaps or overlap",
          sum(zr.hud.rect(n)[3] for n in H.REGIONS) == HT
          and all(zr.hud.rect(a)[1] + zr.hud.rect(a)[3] == zr.hud.rect(b)[1]
                  for a, b in zip(H.REGIONS, H.REGIONS[1:])))

    # source nets
    src = open(os.path.join(HERE, "orc_hud.py"), encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("never pygame.init() (would open the headphone card via SDL audio)",
          "pygame.init()" not in code and 'setdefault("SDL_AUDIODRIVER", "dummy")' in code)

    shutil.rmtree(_tmp, ignore_errors=True)
    total = _pass + _fail
    print(f"{_pass} passed, {_fail} failed (of {total})")
    if total != EXPECTED:
        print(f"COUNT DRIFT: ran {total}, EXPECTED = {EXPECTED}")
        return 1
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
