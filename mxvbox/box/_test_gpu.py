# -*- coding: utf-8 -*-
"""_test_gpu.py -- the MXV box's GPU screen (Python 2.7, runs ON THE BOX, needs its Mali GPU).

    python /storage/orc/_test_gpu.py

Draws OFF SCREEN (a pbuffer: nothing on the screen changes) and checks the GPU's picture pixel
by pixel against the framebuffer path's own output (orcflash.expand) where both can draw it, the
GPU's blur (it is the only blur since 2026-11-02: no stored blur copies), which pixels the blur
looks blur (orcflash.blur_mask), and how orcflash picks the GPU or falls back.
"""
from __future__ import print_function
import os, struct, sys, tempfile, shutil, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def mem_available_mb(path="/proc/meminfo"):
    """MB the box can still give out. Its kernel (3.10) has no MemAvailable line, so: free + buffers +
    cache - shared (the clip slots are tmpfs: counted as cache, but they can never be freed)."""
    m = {}
    try:
        lines = open(path).read().splitlines()
    except (IOError, OSError):
        return None
    for ln in lines:
        parts = ln.split()
        try:
            m[ln.split(":")[0]] = int(parts[1])
        except (IndexError, ValueError):
            continue                             # a line it can't read is skipped, not fatal
    if "MemAvailable" in m:
        return m["MemAvailable"] // 1024
    if "MemFree" not in m:
        return None
    return (m["MemFree"] + m.get("Buffers", 0) + m.get("Cached", 0) - m.get("Shmem", 0)) // 1024


# This suite takes the GPU and ~100 MB. With the RAM full and no swap the box does not crash, it
# FREEZES (2026-11-02: power-cycled by hand). So it refuses to start when memory is short.
MIN_FREE_MB = 180
_free = mem_available_mb()
if _free is not None and _free < MIN_FREE_MB:
    print("NOT RUN: only %d MB free (need %d). The box freezes when it runs out (no swap): wait for the "
          "clip loader to settle, or stop orcflash first." % (_free, MIN_FREE_MB))
    sys.exit(2)
import orcflash as F                      # noqa: E402
import gpu as G                           # noqa: E402

EXPECTED = 168
passed = [0]
failed = [0]


def check(cond, label, extra=""):
    if cond:
        passed[0] += 1
    else:
        failed[0] += 1
        print("  FAIL:", label, extra)


W, H, LINE = F.W, F.H, F.LINE

# --- the pure helper -----------------------------------------------------------------
check(G.line_uniforms(None)[2] == 0.0, "no thin lines: mode 0")
check(G.line_uniforms((2, 1, "ff8000")) == (2.0, 1.0, 1.0, (1.0, 128 / 255.0, 0.0)),
      "a colour fill: every / thick / mode 1 / the colour 0-1", repr(G.line_uniforms((2, 1, "ff8000"))))
check(G.line_uniforms((3, 1, "blur"))[2] == 2.0, "a blur fill: mode 2 (the GPU's own heaviest blur)")
m = G.line_mask(6, 2, 360)
check(len(m) == 360 and [y for y in range(24) if m[y] != b"\0"] == [0, 1, 6, 7, 12, 13, 18, 19],
      "line_mask 6/2: rows 0-1 of every 6, top first", repr(m[:24]))
check(G.line_mask(3, 2, 360)[357:] == b"\xff\xff\0", "line_mask reaches the bottom row exactly")
check(all(0 <= v < 1 for name in G.SCENES for v in G.scene_phases(name, 0.7, 0.5, 12345.678)),
      "scene phases always wrap to 0-1")
fast = [G.scene_phases("opart", 100.0, 0.5, t) for t in (10.0, 10.1)]
check(abs(fast[1][0] - fast[0][0]) <= 0.2 + 1e-9, "the photosensitivity cap: no phase turns faster than 2 cycles/s", repr(fast))
check(G.scene_phases("floor", 1.0, 0.0, 0.3)[1] == 0.0 and G.scene_phases("floor", 1.0, 1.0, 0.3)[1] > 0
      and G.scene_phases("nope", 1.0, 0.5, 3.0) == (0, 0, 0, 0),
      "DETAIL sets how much the floor turns (0 = straight on); an unknown scene doesn't move")
u0 = G.fx_uniforms(None, 320)
check(u0["kal"][0] == 0 and u0["rgb"] == 0 and u0["col"][3] == 0 and u0["xf"][0] == 0 and u0["wob"][0] == 0,
      "no looks: every look off in the shader", repr(u0))
u1 = G.fx_uniforms({"t": 1.0, "k": (6, 0.25), "r": 3.2, "c": (0.0, 1.0, 0.0, 0.5), "w": (16, 2.0, 0.25), "xf": 1.7}, 320)
check(abs(u1["kal"][1] - G.TAU / 4) < 1e-9 and abs(u1["col"][0] - G.TAU / 2) < 1e-9 and u1["col"][3] == 1.0
      and abs(u1["rgb"] - 0.01) < 1e-12 and u1["wob"][:2] == (0.05, 2.0) and u1["xf"] == (1.0, 1.0),
      "spin / hue cycle / wobble phases follow the box's clock; px -> picture widths; crossfade clamped", repr(u1))
u2 = G.fx_uniforms({"t": 1e9 + 0.25, "k": (4, 1.0), "c": (0.0, 1.0, 0.0, 1.0), "w": (5, 1.0, 1.0)}, 320)
check(all(0 <= v < G.TAU for v in (u2["kal"][1], u2["col"][0], u2["wob"][2])) and abs(u2["kal"][1] - G.TAU / 4) < 1e-4,
      "a huge time still gives phases inside one turn (the GPU's 16-bit maths can't hold big numbers)", repr(u2))
check(G.fx_uniforms({"k": (1, 0.5)}, 320)["kal"][0] == 0 and G.fx_uniforms({"c": (360.0, 1.0, 0.0, 0.0)}, 320)["col"][3] == 0,
      "1 mirror = no kaleidoscope; a full 360 hue turn with nothing else = colour off")


# --- the real GPU, off screen ----------------------------------------------------------
def picture(seed):
    """Every pixel different-ish, B G R A; the top-left pixel pure red, bottom-right pure blue."""
    out = []
    for y in range(H):
        out.append(b"".join(chr((x * 3 + seed) & 255) + chr((y * 5 + seed) & 255) + chr((x + y) & 255) + "\xff"
                            for x in range(W)))
    p = bytearray(b"".join(out))
    p[0:4] = b"\x00\x00\xff\xff"
    p[len(p) - 4:] = b"\xff\x00\x00\xff"
    return bytes(p)


def as_gpu(page):
    """orcflash's screen page (W x HO, BGRA) as the GPU surface should show it: RGBA, each pixel twice."""
    out = []
    for i in range(0, len(page), 4):
        b, g, r = page[i], page[i + 1], page[i + 2]
        out.append((r + g + b + "\xff") * 2)
    return b"".join(out)


def diff(a, b):
    """-> (number of differing pixels, first (x, y) that differs)."""
    n, first = 0, None
    for i in range(0, min(len(a), len(b)), 4):
        if a[i:i + 4] != b[i:i + 4]:
            n += 1
            if first is None:
                first = ((i // 4) % G.SW, (i // 4) // G.SW)
    return n + abs(len(a) - len(b)) // 4, first


try:
    S = G.Screen(W, H, 1920, 1080, offscreen=True)
except Exception as e:
    S = None
    print("  no GPU here (%s)" % e)
check(S is not None, "the GPU starts off screen (EGL + shader compile + link)")

if S is not None:
    P = picture(0)
    S.draw(P)
    out = S.read()
    check(len(out) == G.SW * G.SH * 4, "the surface is SW x SH RGBA")
    check(out[0:4] == b"\xff\x00\x00\xff", "top-left pixel is red: colours swizzled right and the picture is the right way up",
          repr(out[0:4]))
    check(out[-4:] == b"\x00\x00\xff\xff", "bottom-right pixel is blue", repr(out[-4:]))
    F.FINE[0] = None
    n, at = diff(out, as_gpu(F.expand(P)))
    check(n == 0, "no lines: every pixel equals the framebuffer path", "%d differ, first %s" % (n, at))
    for look in ((2, 1, "000000"), (6, 2, "ff8000"), (3, 2, "102030")):
        F.FINE[0] = look
        S.draw(P, look)
        n, at = diff(S.read(), as_gpu(F.expand(P)))
        check(n == 0, "thin lines %r: every pixel equals the framebuffer path" % (look,), "%d differ, first %s" % (n, at))
    S.draw(P, blur=4.0)
    full = S.read()                                   # the whole picture at the heaviest blur
    F.FINE[0] = (3, 1, "blur")
    S.draw(P, F.FINE[0])
    o, sharp = S.read(), as_gpu(F.expand(P))          # (expand can't blur: it draws no lines for this)
    mask = G.line_mask(3, 1, G.SH)
    rw = G.SW * 4
    plain_same = all(o[y * rw:(y + 1) * rw] == sharp[y * rw:(y + 1) * rw] for y in range(G.SH) if mask[y] == b"\0")
    lines_blurred = all(all(abs(ord(a) - ord(b)) <= 1 for a, b in zip(o[y * rw:(y + 1) * rw], full[y * rw:(y + 1) * rw]))
                        for y in range(G.SH) if mask[y] != b"\0")         # (+-1: rounding of the mix)
    check(plain_same and lines_blurred and full != sharp,
          "thin lines filled with blur: the line rows ARE the heaviest blur, the rest stays sharp")
    F.FINE[0] = None

    # --- the GPU's own blur (y) ---
    def solid(bgra):
        return bgra * (W * H)

    def dot():
        """An 8x8 white block in the middle (a single pixel's light thins below 1/255 by level 3)."""
        p = bytearray(solid(b"\0\0\0\xff"))
        for y in range(86, 94):
            for x in range(156, 164):
                p[(y * W + x) * 4:(y * W + x) * 4 + 4] = b"\xff\xff\xff\xff"
        return bytes(p)

    def stats(level, pic):
        """-> (brightest red value on the surface, how many pixels are lit, total light) after blurring pic."""
        S.draw(pic, blur=level)
        o = S.read()
        vals = [ord(o[i]) for i in range(0, len(o), 4)]
        return max(vals), sum(1 for v in vals if v > 2), sum(vals)

    S.draw(P, blur=0.0)
    n, at = diff(S.read(), as_gpu(F.expand(P)))
    check(n == 0, "blur 0: exactly the sharp picture", "%d differ, first %s" % (n, at))
    S.draw(solid(b"\x40\x80\xc0\xff"), blur=4.0)
    o = S.read()
    worst = max(max(abs(ord(o[i]) - 0xc0), abs(ord(o[i + 1]) - 0x80), abs(ord(o[i + 2]) - 0x40))
                for i in range(0, len(o), 4))
    check(worst <= 2, "a flat colour blurred at 4 stays that colour everywhere, edges too", "worst off by %d" % worst)
    D = dot()
    st = [stats(lv, D) for lv in (1, 2, 3, 4)]
    check(all(st[i][0] >= st[i + 1][0] for i in range(3)) and st[3][0] < st[0][0]
          and all(st[i][1] < st[i + 1][1] for i in range(3)),
          "a white block: each level spreads it wider and dimmer", repr(st))
    total0 = 64 * 4 * 255
    check(all(abs(s_[2] - total0) < total0 * 0.1 for s_ in st), "the blur keeps the light (no level loses over 10 %)",
          repr([s_[2] for s_ in st]))
    mid = stats(2.5, D)
    check(st[2][0] < mid[0] < st[1][0], "blur 2.5 lies between 2 and 3", "%r vs %r %r" % (mid, st[1], st[2]))
    half = b"".join((b"\xff\xff\xff\xff" * W) if y < H // 2 else (b"\0\0\0\xff" * W) for y in range(H))
    S.draw(half, blur=4.0)
    o = S.read()
    top, bottom = ord(o[(20 * G.SW + 300) * 4]), ord(o[(340 * G.SW + 300) * 4])
    check(top > 200 and bottom < 50, "blur keeps the picture where it is (white top half stays on top)",
          "%d / %d" % (top, bottom))
    top = b"\xff" * (W * (H // 2)) + b"\x00" * (W * (H - H // 2))
    S.draw(P)
    sharp = S.read()
    S.draw(P, bmask=top)
    o = S.read()
    rw, half_rows = G.SW * 4, G.SH // 2
    changed_top = sum(1 for y in range(half_rows) if o[y * rw:(y + 1) * rw] != sharp[y * rw:(y + 1) * rw])
    check(changed_top == half_rows and o[half_rows * rw:] == sharp[half_rows * rw:],
          "a blur mask blurs exactly its pixels (top half blurred, bottom half untouched)", "%d rows" % changed_top)
    S.gl.glFinish()
    t0 = time.time()
    for i in range(20):
        S.draw(P, blur=3.5)
    S.gl.glFinish()
    ms = (time.time() - t0) * 50
    print("  blur 3.5 draw: %.1f ms per frame (off screen)" % ms)
    check(ms < 12, "blur 3.5 costs under 12 ms a frame", "%.1f ms" % ms)

    # --- the looks (2026-11-02): kaleidoscope, RGB split, colour, wobble, crossfade ---
    def px_at(o, x, y):
        i = (y * G.SW + x) * 4
        return ord(o[i]), ord(o[i + 1]), ord(o[i + 2])

    def close(a, b, tol=2):
        return all(abs(ord(a[i]) - ord(b[i])) <= tol for i in range(len(a)))

    S.draw(P)
    plain = S.read()
    S.draw(P, fx={"t": 5.0, "w": None, "k": (0, 0.3), "r": 0.0, "c": (0.0, 1.0, 0.0, 0.0), "xf": None})
    check(S.read() == plain, "every look at its neutral setting: exactly the plain picture")
    # a SMOOTH picture: P's colours wrap (255 -> 0 in one pixel), and 16-bit maths landing a hair
    # either side of a wrap would look like a broken mirror
    smooth = b"".join(chr(x * 255 // W) + chr(y * 255 // H) + chr(128) + "\xff" for y in range(H) for x in range(W))
    S.draw(smooth)
    splain = S.read()
    S.draw(smooth, fx={"t": 0.0, "k": (6, 0.0)})
    o = S.read()
    rw = G.SW * 4
    off = sum(1 for y in range(G.SH // 2) for x in range(0, G.SW, 4)
              if not close(o[y * rw + x * 4:y * rw + x * 4 + 3], o[(G.SH - 1 - y) * rw + x * 4:(G.SH - 1 - y) * rw + x * 4 + 3], 6))
    check(o != splain and off < G.SW // 4 * G.SH // 2 // 100, "kaleidoscope 6: a mirrored picture (top half = bottom half upside down)",
          "%d sampled pixels off" % off)
    line = b"".join(((b"\xff\xff\xff\xff" if x == 160 else b"\0\0\0\xff") * 1) for y in range(H) for x in range(W))
    S.draw(line, fx={"t": 0.0, "r": 4.0})
    o = S.read()
    cols = [max(range(G.SW), key=lambda x: px_at(o, x, 180)[ch]) for ch in (0, 1, 2)]
    check(cols[1] in (320, 321) and abs((cols[1] - cols[0]) - 8) <= 1 and abs((cols[2] - cols[1]) - 8) <= 1,
          "RGB split 4: red 4 picture px one way, blue 4 the other, green stays", repr(cols))
    red = b"\0\0\xff\xff" * (W * H)
    S.draw(red, fx={"t": 0.0, "c": (180.0, 1.0, 0.0, 0.0)})
    r_, g_, b_ = px_at(S.read(), 300, 180)
    check(r_ < 60 and g_ > 120 and b_ > 120, "hue 180: red turns cyan", repr((r_, g_, b_)))
    S.draw(P, fx={"t": 0.0, "c": (0.0, 0.0, 0.0, 0.0)})
    o = S.read()
    grey = all(abs(ord(o[i]) - ord(o[i + 1])) <= 3 and abs(ord(o[i + 1]) - ord(o[i + 2])) <= 3 for i in range(0, len(o), 16))
    check(grey, "saturation 0: grey")
    S.draw(b"\0\0\0\xff" * (W * H), fx={"t": 0.0, "c": (0.0, 1.0, 1.0, 0.0)})
    check(min(px_at(S.read(), 100, 100)) >= 250, "invert 1: black turns white")
    # (a bent picture is read smoothly since 2026-11-02, so this compares a SMOOTH picture, within a few
    # levels: row 90 = a quarter down, where the wave peaks, shifted 10 picture px = 20 screen px)
    sm = b"".join(chr(x * 255 // W) + chr(y * 255 // H) + chr(128) + "\xff" for y in range(H) for x in range(W))
    S.draw(sm)
    smp = S.read()
    S.draw(sm, fx={"t": 0.0, "w": (10, 1.0, 0.0)})
    o = S.read()
    peak = all(close(o[90 * rw + x * 4:90 * rw + x * 4 + 3], smp[90 * rw + (x + 20) * 4:90 * rw + (x + 20) * 4 + 3], 4)
               for x in range(0, G.SW - 20, 16))
    top = all(close(o[x * 4:x * 4 + 3], smp[x * 4:x * 4 + 3], 4) for x in range(0, G.SW, 16))
    check(peak and top and S.last_smooth, "GPU wobble: a quarter down the picture moves 10 px (the wave's peak), "
          "the top row stays, and the bent picture is read smoothly")
    S.draw(P)
    check(not S.last_smooth and S.read() == plain, "nothing bends it: the chunky pixels, exactly as before")
    Q = picture(77)
    S.draw(Q)
    qpic = S.read()
    S.draw(P, fx={"t": 0.0, "xf": 0.5}, prev=Q)
    mid = S.read()
    halfway = all(abs(ord(mid[i]) - (ord(plain[i]) + ord(qpic[i])) / 2.0) <= 2 for i in range(0, len(mid), 4))
    check(halfway, "crossfade 0.5: every pixel halfway between the outgoing and the new picture")
    S.draw(P, fx={"t": 0.0, "xf": 0.0}, prev=Q)
    a0 = S.read()
    S.draw(P, fx={"t": 0.0, "xf": 1.0}, prev=Q)
    check(a0 == qpic and S.read() == plain, "crossfade 0 = all the outgoing picture, 1 = all the new one")
    S.draw(P, fx={"t": 0.0, "xf": 0.5}, prev=None)
    check(S.read() == plain, "no outgoing picture: no crossfade")
    mask_all = b"\xff" * (W * (H // 3)) + b"\x00" * (W * (H - H // 3))
    S.gl.glFinish()
    t0 = time.time()
    for i in range(20):
        S.draw(P, (3, 1, "000000"), 2.5, mask_all,
               {"t": i * 0.03, "w": (8, 1.5, 1.0), "k": (6, 0.1), "r": 3.0, "c": (40.0, 1.3, 0.2, 0.05), "xf": 0.4}, Q)
    S.gl.glFinish()
    ms = (time.time() - t0) * 50
    print("  every look at once: %.1f ms per frame (off screen)" % ms)
    check(ms < 30, "every look at once (+ blur 2.5, a blur mask, lines, a crossfade) under 30 ms a frame", "%.1f ms" % ms)

    # --- zoom + shake (the hit reactions' GPU part) ---
    S.draw(smooth)
    sp = S.read()
    S.draw(smooth, fx={"t": 0.0, "z": 0.5})
    o = S.read()
    near = [px_at(sp, x, 180) for x in (212, 213, 214)]
    check(px_at(o, 320, 180) == px_at(sp, 320, 180) and px_at(o, 160, 180) in near,
          "zoom 0.5: the middle stays put, a point a quarter in comes from a third in (50 % closer)",
          repr((px_at(o, 160, 180), near)))
    S.draw(smooth, fx={"t": 0.0, "s": (32.0, 0.0)})
    o = S.read()
    check(all(px_at(o, x, 100) in [px_at(sp, x + 64 + d, 100) for d in (-1, 0, 1)] for x in (10, 200, 400)),
          "shake 32 px: the picture is read 32 picture px (64 screen px) over")

    # --- the WEAVE on the GPU: exactly the rows the CPU weave (mix_rows) takes ---
    Qw = picture(31)
    for amt, fine in ((0.5, None), (0.3, None), (0.5, (4, 1, "000000"))):
        F.FINE[0], F.LINES[0] = fine, None
        skip = F.line_rows()
        S.draw(P, fine, fx={"t": 0.0, "weave": (Qw, F.weave_rows(amt, skip))})
        n, at = diff(S.read(), as_gpu(F.expand(F.mix_rows(P, Qw, amt, skip=skip))))
        check(n == 0, "GPU weave %.1f%s = the CPU weave, every pixel" % (amt, " + thin lines" if fine else ""),
              "%d differ, first %s" % (n, at))
    F.FINE[0] = None

    # --- SCENES: pictures the GPU draws itself ---
    def scene_read(name, phases, detail=0.5):
        """Scene `name` drawn with these exact phases (not from the clock), read back from the GPU."""
        gl_ = S.gl
        gl_.glBindFramebuffer(G.GL_FRAMEBUFFER, S.scene_tgt[1])
        gl_.glViewport(0, 0, W, H)
        gl_.glUseProgram(S.p_scene)
        gl_.glUniform4f(S.u_sp, *[G.C.c_float(x) for x in phases])
        gl_.glUniform4f(S.u_sc, G.C.c_float(G.SCENES.index(name) + 1), G.C.c_float(detail), G.C.c_float(0), G.C.c_float(0))
        gl_.glVertexAttribPointer(0, 2, 0x1406, 0, 0, S._quad)
        gl_.glEnableVertexAttribArray(0)
        gl_.glDrawArrays(5, 0, 4)
        buf = G.C.create_string_buffer(W * H * 4)
        gl_.glReadPixels(0, 0, W, H, G.GL_RGBA, G.GL_UNSIGNED_BYTE, buf)
        gl_.glBindFramebuffer(G.GL_FRAMEBUFFER, 0)
        return buf.raw

    def mean_diff(a, b):
        return sum(abs(ord(a[i]) - ord(b[i])) for i in range(0, len(a), 16)) / float(len(a) // 16)

    ok_all, seam_bad, still = True, [], []
    for name in G.SCENES:
        S.draw(P, fx={"t": 0.0, "scene": (name, 0.0, 0.5)})
        a = S.read()
        S.draw(P, fx={"t": 0.7, "scene": (name, 1.0, 0.5)})
        b = S.read()
        if S.gl.glGetError() != 0 or len(set(a[i:i + 3] for i in range(0, len(a), 256))) < 2:
            ok_all = False
        if a == b:
            still.append(name)
        # seamless: stepping ACROSS the wrap (0.995 -> 0) looks like any small step (0 -> 0.005).
        # (Comparing with half a cycle proves nothing: the tunnel looks the same half a cycle on.)
        # Each phase on its own (the others at 0): a seam in one layer (say the far ridge) is too small to
        # show when every layer moves at once.
        for i, rate in enumerate(G.SCENE_RATES[name]):
            if not rate:
                continue
            z0, zb, za = (scene_read(name, tuple(v if j == i else 0.0 for j in range(4))) for v in (0.0, 0.995, 0.005))
            across, step_ = mean_diff(zb, z0), mean_diff(z0, za)
            if not across <= 3 * step_ + 1:
                seam_bad.append((name, i, round(across, 2), round(step_, 2)))
    check(ok_all, "every scene draws (no GL error, more than one colour)")
    z = scene_read("opart", (0.0, 0.0, 0.0, 0.0), 0.5)
    shades = set(z[(90 * W + x) * 4] for x in range(W))
    check(len(shades) > 4, "SOFT EDGES: a stripe edge fades across a pixel (in-between shades, not 2 flat colours)",
          "%d shades" % len(shades))
    check(still == [], "every scene moves with time", repr(still))
    check(seam_bad == [], "every scene wraps seamlessly (a cycle later it is back where it started)", repr(seam_bad))
    S.draw(P, fx={"t": 0.0, "scene": ("parallax", 0.0, 0.0)})
    o = S.read()
    top = set(px_at(o, x, 2) for x in range(0, G.SW, 8))
    bottom = [px_at(o, x, G.SH - 2) for x in range(0, G.SW, 2)]
    flips = sum(1 for i in range(1, len(bottom)) if bottom[i] != bottom[i - 1])
    check(len(top) <= 2 and flips >= 4 and sum(max(top)) < sum(max(bottom)),
          "parallax the right way up: plain dark sky on top, the striped ground at the bottom",
          repr((sorted(top), flips)))
    # SCENE VIDEO: the layers / tiles filled with clips (here three solid "clips": red, green, blue)
    RED_, GRN_, BLU_ = b"\0\0\xff\xff" * (W * H), b"\0\xff\0\xff" * (W * H), b"\xff\0\0\xff" * (W * H)
    vids = (RED_, GRN_, BLU_)
    S.draw(P, fx={"t": 0.0, "scene": ("rings", 0.0, 0.5)})
    drawn = S.read()
    S.draw(P, fx={"t": 0.0, "scene": ("rings", 0.0, 0.5, 0.0, vids)})
    check(S.read() == drawn, "SCENE VIDEO 0: exactly the drawn scene, clips or not")

    def colours(o):
        """Which of red / green / blue lead the pixels (dark ones ignored)."""
        seen = set()
        for i in range(0, len(o), 64):
            px_ = [ord(o[i]), ord(o[i + 1]), ord(o[i + 2])]
            if max(px_) > 20:
                seen.add("rgb"[px_.index(max(px_))])
        return seen
    S.draw(P, fx={"t": 0.0, "scene": ("rings", 0.0, 0.5, 1.0, vids)})
    check(colours(S.read()) == set("rg"), "rings, all video: the two ring sets show clips A and B (red, green), nothing drawn")
    S.draw(P, fx={"t": 0.0, "scene": ("tunnel", 0.0, 0.5, 1.0, vids)})
    check(colours(S.read()) == set("rg"), "tunnel, all video: the tiles alternate clips A and B")
    S.draw(P, fx={"t": 0.0, "scene": ("parallax", 0.0, 0.5, 1.0, vids)})
    o = S.read()
    check(colours(o) == set("rgb") and max(px_at(o, 320, 10)) == px_at(o, 320, 10)[0],
          "parallax, all video: three clips across the layers (the sky is clip A, red)", repr(colours(o)))
    S.draw(P, fx={"t": 0.0, "scene": ("floor", 0.0, 0.5, 1.0, (RED_,))})
    check(colours(S.read()) == set("r"), "one clip in RAM: every layer uses it (no missing texture)")
    keyed = (((1, 0), RED_), ((2, 0), GRN_), ((3, 0), BLU_))
    S.draw(P, fx={"t": 0.0, "scene": ("rings", 0.0, 0.5, 1.0, keyed)})
    u0 = S.uploads
    S.draw(P, fx={"t": 0.03, "scene": ("rings", 0.0, 0.5, 1.0, keyed)})
    still = S.uploads == u0
    o_same = S.read()
    S.draw(P, fx={"t": 0.06, "scene": ("rings", 0.0, 0.5, 1.0, (((1, 1), GRN_),) + keyed[1:])})
    check(still and S.uploads == u0 + 1 and colours(o_same) == set("rg") and colours(S.read()) == set("g"),
          "a clip frame the layer already has is not sent again; a new one is (and shows: clip A is green now)",
          (u0, S.uploads))
    S.draw(P, fx={"t": 0.0, "scene": ("parallax", 0.0, 0.5), "c": (180.0, 1.0, 0.0, 0.0)})
    hued = S.read()
    check(hued != o, "the looks work on a scene too (hue 180 changes it)")
    # (the best of two runs, under 35 ms: the live player shares the GPU while this runs, and one
    # busy run read 33.6 ms against 27.9 quiet, 2026-11-03. A real frame rarely has all of this on.)
    best = None
    for run_ in range(2):
        S.gl.glFinish()
        t0 = time.time()
        for i in range(20):
            S.draw(P, (3, 1, "000000"), 2.5, mask_all,
                   {"t": i * 0.03, "w": (8, 1.5, 1.0), "k": (6, 0.1), "r": 3.0, "c": (40.0, 1.3, 0.2, 0.05), "xf": 0.4,
                    "scene": ("tunnel", 0.5, 0.5), "mask": (1, 0.5, 0, i * 0.03, 0.4, 0.5, Q if i % 2 else P)}, Q)
        S.gl.glFinish()
        ms = (time.time() - t0) * 50
        best = ms if best is None else min(best, ms)
    ms = best
    print("  a scene + every look at once: %.1f ms per frame (off screen, best of 2)" % ms)
    check(ms < 35, "a scene + a masked clip + every look + blur + lines + crossfade under 35 ms a frame", "%.1f ms" % ms)

    # --- COMPRESSED clips (ETC1 at SW x SH, 2026-11-02) ---
    # built by hand, block by block (no encoder on the box): a block = base colour (5 bits each,
    # "differential" mode) + a modifier table per half + 2 bits per pixel. Like a raw frame the
    # GPU sees B, G, R in r, g, b, so a block's "r" field is the picture's BLUE.
    def etc_block(b5, g5, r5, cols=0, table=0):
        """One 4x4 block of the picture colour (b5, g5, r5); cols = a 4-bit mask of the columns pushed
        down (-big), the rest up (+big)."""
        bits = 0
        for x in range(4):
            if cols >> x & 1:
                bits |= 0xF << (x * 4)
        # index (msb, lsb): 00 +small, 01 +big, 10 -small, 11 -big; msb word first
        word = struct.pack(">HH", bits, 0xFFFF) if cols else b"\0" * 4
        return struct.pack("BBBB", b5 << 3, g5 << 3, r5 << 3, (table << 5) | (table << 2) | 2) + word

    def etc_frame(top, bottom=None):
        bw, bh = G.SW // 4, G.SH // 4
        return G.Etc(b"".join((top if by < bh // 2 or bottom is None else bottom) * bw for by in range(bh)))

    ERED = etc_frame(etc_block(0, 0, 31), etc_block(31, 0, 0))      # top half red, bottom half blue
    check(len(ERED) == G.Etc.SIZE == G.SW * G.SH // 2, "an ETC1 frame at the surface's size is half a byte a pixel")
    S.draw(ERED)
    o = S.read()
    r_t, r_b = px_at(o, 10, 10), px_at(o, G.SW - 10, G.SH - 10)
    check(r_t[0] > 240 and max(r_t[1:]) < 20 and r_b[2] > 240 and max(r_b[:2]) < 20,
          "a compressed frame shows the right colours, the right way up (red top, blue bottom)", repr((r_t, r_b)))
    # 1-pixel stripes: only a full 640-wide picture can show them (a raw clip's pixel is 2 screen px wide)
    STRIPES = etc_frame(etc_block(16, 16, 16, cols=0b1010, table=7))
    S.draw(STRIPES)
    o = S.read()
    row = [px_at(o, x, 100)[1] for x in range(G.SW)]
    flips = sum(1 for x in range(1, G.SW) if abs(row[x] - row[x - 1]) > 200)
    check(flips >= G.SW - 2, "a compressed clip reaches the screen at the FULL 640x360 (1-pixel stripes survive)",
          "%d flips of %d" % (flips, G.SW - 1))
    S.draw(P)
    check(S.read() == plain, "a raw frame after a compressed one on the same texture: exactly as before")
    S.draw(STRIPES, blur=4.0)
    o = S.read()
    row = [px_at(o, x, 180)[1] for x in range(100, 540)]
    check(max(row) - min(row) < 40 and 60 < sum(row) / len(row) < 200,
          "the GPU blurs a compressed clip too (the stripes melt to grey)", repr((min(row), max(row))))
    S.draw(P, fx={"t": 0.0, "weave": (ERED, b"\xff" * H)})
    o = S.read()
    check(px_at(o, 10, 10)[0] > 240 and px_at(o, G.SW - 10, G.SH - 10)[2] > 240,
          "the weave can take its rows from a compressed clip")
    S.draw(STRIPES, fx={"t": 0.0, "weave": (P, b"\x00" * H)})
    o = S.read()
    row = [px_at(o, x, 100)[1] for x in range(G.SW)]
    check(sum(1 for x in range(1, G.SW) if abs(row[x] - row[x - 1]) > 200) >= G.SW - 2,
          "...and weave over one at its full 640 wide (no rows taken: the compressed picture whole)")
    S.draw(P, fx={"t": 0.0, "xf": 0.0}, prev=ERED)
    o = S.read()
    check(px_at(o, 10, 10)[0] > 240 and px_at(o, G.SW - 10, G.SH - 10)[2] > 240,
          "a crossfade can start from a compressed clip")
    S.draw(P, fx={"t": 0.0, "scene": ("floor", 0.0, 0.5, 1.0, (etc_frame(etc_block(0, 31, 0)),))})
    check(colours(S.read()) == set("g"), "a scene's video layers take compressed clips")
    S.draw(P, fx={"t": 0.0, "scene": ("floor", 0.0, 0.5, 1.0, (RED_,))})
    check(colours(S.read()) == set("r"), "...and raw ones again after them")

    # --- SCENE OVERLAY: a clip laid flat over the scene, faded (2026-11-02) ---
    S.draw(P, fx={"t": 0.0, "scene": ("tunnel", 0.0, 0.5)})
    drawn_t = S.read()
    S.draw(P, fx={"t": 0.0, "scene": ("tunnel", 0.0, 0.5, 0.0, (GRN_,), 0.0)})
    ov0 = S.read()
    S.draw(P, fx={"t": 0.0, "scene": ("tunnel", 0.0, 0.5, 0.0, (GRN_,), 1.0)})
    ov1 = S.read()
    S.draw(P, fx={"t": 0.0, "scene": ("tunnel", 0.0, 0.5, 0.0, (GRN_,), 0.5)})
    ovh = S.read()
    half_ok = all(abs(ord(ovh[i + 1]) - (ord(drawn_t[i + 1]) + 255) / 2.0) <= 6 for i in range(0, len(ovh), 4 * 97))
    check(ov0 == drawn_t and colours(ov1) == set("g") and min(px_at(ov1, x, 180)[1] for x in range(0, G.SW, 40)) > 240
          and half_ok,
          "SCENE OVERLAY: 0 = the scene as drawn, 1 = only the clip, flat over everything, 0.5 = halfway")
    # --- a clip taking over from a SCENE fades out of the scene, not out of black (2026-11-02) ---
    S.draw(P, fx={"t": 0.0, "scene": ("rings", 0.0, 0.5)})
    scene_img = S.read()
    S.snapshot()
    S.draw(Q, fx={"t": 0.0, "xf": 0.0}, prev=G.SNAP)
    out_ = S.read()
    diffs = [abs(ord(out_[i]) - ord(scene_img[i])) for i in range(0, len(out_), 4 * 53)]
    dark = sum(1 for i in range(0, len(out_), 4 * 53) if max(ord(c) for c in out_[i:i + 3]) < 12)
    check(sum(diffs) / float(len(diffs)) < 20 and dark < len(diffs) // 2,
          "crossfade start after a scene = the scene's last frame (it was the black under it)",
          (sum(diffs) / float(len(diffs)), dark, len(diffs)))
    S.draw(Q, fx={"t": 0.0, "xf": 1.0}, prev=G.SNAP)
    check(S.read() == qpic, "...and it ends on the new picture")
    # --- FRAME / PEEPHOLE (2026-11-02): the light only inside a shape, the LAST step ---
    def framed(fr, pic=None, **more):
        fx_ = {"t": 0.0, "frame": fr}
        fx_.update(more)
        S.draw(pic if pic is not None else P, fx=fx_)
        return S.read()
    o = framed((1, 0.6, 0.5, 0.5, 0.01, 0, None))
    check(px_at(o, 320, 180) == px_at(plain, 320, 180) and max(px_at(o, 3, 3)) == 0 and max(px_at(o, 636, 356)) == 0,
          "FRAME circle: the picture inside, NO light outside (a spotlight)", (px_at(o, 320, 180), px_at(o, 3, 3)))
    o = framed((1, 0.6, 0.5, 0.5, 0.01, 1, None))
    check(max(px_at(o, 320, 180)) == 0 and px_at(o, 3, 3) == px_at(plain, 3, 3), "FRAME INVERT: an area blocked out, the rest lit")
    d0 = S.frames_drawn
    framed((1, 0.6, 0.5, 0.5, 0.01, 1, None))
    check(S.frames_drawn == d0, "an unchanged frame is not drawn again (it costs nothing per frame)")
    o = framed((1, 0.6, 0.5, 0.5, 0.01, 0, None), k=(6, 0.0), c=(0.0, 1.0, 1.0, 0.0))
    check(max(px_at(o, 3, 3)) == 0 and max(px_at(o, 320, 180)) > 0,
          "the frame comes AFTER the looks: a kaleidoscope + negative inside, the corners still dark")
    o = framed((1, 0.25, 0.2, 0.3, 0.01, 0, None))
    check(px_at(o, 128, 108) == px_at(plain, 128, 108) and max(px_at(o, 320, 180)) == 0, "FRAME X / Y: the spotlight sits where it is told")
    row = [max(px_at(framed((1, 0.6, 0.5, 0.5, 0.15, 0, None), pic=b"\xff\xff\xff\xff" * (W * H)), x, 180)) for x in range(0, G.SW)]
    check(len([v for v in row if 20 < v < 235]) > 20, "FRAME SOFT: a wide soft edge (a spotlight, not a cut-out)")
    halfpic = b"".join((b"\xff" if x < G.SW // 2 else b"\x00") for y in range(G.SH) for x in range(G.SW))
    o = framed((5, 1.0, 0.5, 0.5, 0.0, 0, halfpic))
    check(px_at(o, 100, 180) == px_at(plain, 100, 180) and max(px_at(o, 540, 180)) == 0,
          "FRAME PICTURE: your own shape (white = light, black = none)")
    o = framed((5, 1.0, 0.5, 0.5, 0.0, 0, None))
    check(max(px_at(o, 3, 3)) == 0 and px_at(o, 320, 180) == px_at(plain, 320, 180), "...no picture yet: a circle stands in")
    S.draw(P)
    check(S.read() == plain, "no frame: the whole picture again")
    # --- MASK / PEEPHOLE (2026-11-02): a second picture through a shape ---
    def masked(shape, size, fill, frame=None, pic=None, cx=0.5, cy=0.5, ph=0.0, scene=None):
        fx_ = {"t": 0.0, "mask": (shape, size, fill, ph, cx, cy, frame)}
        if scene:
            fx_["scene"] = scene
        S.draw(pic if pic is not None else P, fx=fx_)
        return S.read()
    o = masked(1, 0.5, 1)
    centre, corner = px_at(o, 320, 180), px_at(o, 4, 4)
    check(max(centre) < 8 and corner == px_at(plain, 4, 4),
          "MASK circle, BLACK through it: the middle is black, the corner is the picture untouched", (centre, corner))
    o = masked(1, 0.5, 2)
    pc = px_at(plain, 320, 180)
    check(all(abs(a - (255 - b)) <= 2 for a, b in zip(px_at(o, 320, 180), pc)), "...the NEGATIVE through it", (px_at(o, 320, 180), pc))
    o = masked(1, 0.5, 0, GRN_)
    check(px_at(o, 320, 180)[1] > 240 and max(px_at(o, 320, 180)[0], px_at(o, 320, 180)[2]) < 10,
          "...ANOTHER CLIP through it (here a green one)")
    o = masked(1, 0.5, 0, ERED)
    check(px_at(o, 320, 60)[0] > 240, "...a compressed clip through it too")
    row = [px_at(masked(1, 0.5, 1), x, 180)[0] for x in range(0, G.SW)]
    edge_mid = [v for v in row if 20 < v < max(row) - 20]
    check(len(edge_mid) >= 2, "the hole's edge fades across screen pixels (no staircase when it moves)", len(edge_mid))
    half = b"".join((b"\xff\xff\xff\xff" if x < W // 2 else b"\x10\x10\x10\xff") for y in range(H) for x in range(W))
    o = masked(6, 0.5, 1, pic=half)
    check(max(px_at(o, 100, 180)) < 8 and min(px_at(o, 540, 180)) >= 14,
          "MASK bright key: the bright half keys through (black), the dark half stays", (px_at(o, 100, 180), px_at(o, 540, 180)))
    o = masked(7, 0.5, 1, pic=half)
    check(min(px_at(o, 100, 180)) > 240 and max(px_at(o, 540, 180)) < 8, "MASK dark key: the other way round")
    o = masked(4, 0.5, 1, pic=b"\xff\xff\xff\xff" * (W * H))
    flips = sum(1 for x in range(1, G.SW) if (px_at(o, x, 180)[0] > 128) != (px_at(o, x - 1, 180)[0] > 128))
    check(14 <= flips <= 18, "MASK stripes: 8 black stripes across", flips)
    o = masked(1, 0.5, 1, scene=("rings", 0.0, 0.5))
    check(max(px_at(o, 320, 180)) < 8 and o != masked(1, 0.0, 1, scene=("rings", 0.0, 0.5)),
          "a SCENE can be masked too")
    o2 = masked(1, 0.3, 1, cx=0.2, cy=0.3)
    check(max(px_at(o2, int(0.2 * G.SW), int(0.3 * G.SH))) < 8 and px_at(o2, 320, 180) == px_at(plain, 320, 180),
          "the hole moves where it is told (centre 0.2, 0.3)")
    # repeat frames are not sent again (the screen redraws ~35 a second, a clip changes 15)
    S.draw(Q)
    s0 = S.skipped
    S.draw(Q)
    same_q = S.read() == qpic
    S.draw(P)
    check(S.skipped == s0 + 1 and same_q and S.read() == plain,
          "the same frame object again: not uploaded (and still shown); a new one is", (s0, S.skipped))

# --- blur_mask: which pixels the blur looks blur ----------------------------------------------
# Held to a plain per-pixel SPEC of the layers (z, then l), written differently. (Borders and bands
# were layers too until the CPU reactions went, 2026-11-02.)
def look(z=0.0, l=None, w=None):
    F.FOCUS[0], F.LINES[0], F.FX["w"] = z, l, w


def spec():
    amt, ln = F.FOCUS[0], F.LINES[0]
    out = bytearray(W * H)
    for y in range(H):
        v = amt >= 1 or (amt > 0 and F._RANK[y] < amt)
        if ln and y % ln[0] < ln[1]:
            v = ln[2] == "blur"
        for x in range(W):
            out[y * W + x] = 255 if v else 0
    return bytes(out)


CASES = [
    ("z 0.5", dict(z=0.5)),
    ("z 1", dict(z=1.0)),
    ("z 0.3", dict(z=0.3)),
    ("l 4 2 blur", dict(l=(4, 2, "blur"))),
    ("black lines over z 1", dict(z=1.0, l=(3, 1, "000000"))),
    ("blurred lines + z 0.3", dict(z=0.3, l=(5, 1, "blur"))),
    ("wobble + z 0.5", dict(z=0.5, w=(7, 3.0, 0.5))),
]
for name, kw in CASES:
    look(**kw)
    want = spec()
    got = F.blur_mask() or b"\x00" * len(want)
    nd = sum(1 for i in range(len(want)) if got[i] != want[i])
    check(want.count(b"\xff") > 0 and nd == 0, "blur mask = the spec's blurred pixels: %s" % name,
          "%d differ (of %d blurred)" % (nd, want.count(b"\xff")))
look()
check(F.blur_mask() is None, "no blur look: no mask")

# --- weave_rows: the same rows mix_rows takes (the reference IS mix_rows, with a marker clip) ---
MARKW = b"\x05\x06\x07\xff" * (W * H)
for amt, ln, fine in ((0.5, None, None), (0.3, None, None), (1.0, None, None), (0.5, (3, 1, "blur"), (4, 1, "000000"))):
    F.LINES[0], F.FINE[0] = ln, fine
    skip = F.line_rows()
    mixed = F.mix_rows(picture(2), MARKW, amt, skip=skip)
    want = b"".join(b"\xff" if mixed[y * LINE:y * LINE + 4] == MARKW[:4] else b"\x00" for y in range(H))
    check(F.weave_rows(amt, skip) == want, "weave_rows %.1f%s = the rows mix_rows takes" % (amt, " + lines" if ln else ""))
F.LINES[0] = F.FINE[0] = None
a1 = F.line_rows()
check(F.line_rows() is a1, "line_rows is cached while the look doesn't change (it was rebuilt every frame)")
F.LINES[0] = (2, 1, "blur")
check(F.line_rows() is not a1 and 0 in F.line_rows(), "...and rebuilt when it does")
F.LINES[0] = None

# --- orcflash picks the GPU or falls back -------------------------------------------------
tmp = tempfile.mkdtemp()
F.GPU_OFF = os.path.join(tmp, "gpu_off")
fd0 = F.fd


class Fake(object):
    def __init__(self, *a, **k):
        self.fd, self.draws = 1234, []

    def draw(self, pic, fine=None, blur=0.0, bmask=None, fx=None, prev=None):
        self.draws.append((pic, fine, blur, bmask, fx, prev))

    def snapshot(self):
        self.snaps = getattr(self, "snaps", 0) + 1


def boom(*a, **k):
    raise IOError("no EGL here")


open(F.GPU_OFF, "w").close()
check(F.init_gpu(Fake) is False and F.GPU[0] is None, "gpu_off file: the framebuffer way")
os.remove(F.GPU_OFF)
check(F.init_gpu(boom) is False and F.GPU[0] is None and F.fd == fd0, "GPU won't start: the framebuffer way, fd untouched")
check(F.init_gpu(Fake) is True and F.fd == 1234, "GPU starts: it draws, and the fade uses its fb0 handle")
fake = F.GPU[0]
P = picture(5) if S is None else P
F.show(P)
check(len(fake.draws) == 1 and fake.draws[0][0] == F.compose(P) and fake.draws[0][1] is None,
      "show() hands the GPU the composed picture (no screen page, no lines)")
F.FINE[0] = (2, 1, "blur")
F.show(P, (98, 0))
check(fake.draws[-1][1] == (2, 1, "blur"), "thin lines filled with blur: the GPU gets the look (it makes the blur)")
F.FINE[0] = None
F.BLUR[0] = 2.5
F.show(P, (98, 0))
check(fake.draws[-1][2] == 2.5 and fake.draws[-1][0] == P, "y 2.5 with the GPU: the GPU gets 2.5, the picture untouched")
F.show(P)
check(fake.draws[-1][2] == 2.5 and fake.draws[-1][0] == P, "a pattern blurs too (no mirror-border fallback with the GPU)")
F.BLUR[0] = 0
look(z=1.0, l=(4, 1, "blur"))
F.show(P, (98, 0))
check(fake.draws[-1][0] == P and fake.draws[-1][3] is F.blur_mask() and fake.draws[-1][3] is not None,
      "blur looks leave the composed picture alone; show() hands the GPU the blur mask")
look()
F.CLIPS[46] = picture(6) * 2
F.WEAVE.update(n=46, amt=0.5, t0=time.time())
F.show(P)
wv = fake.draws[-1][4].get("weave")
check(fake.draws[-1][0] == P and wv is not None and wv[0] == picture(6) and wv[1] == F.weave_rows(0.5, F.line_rows()),
      "with the GPU the weave is its pass: the picture goes unwoven, the GPU gets the other frame + its rows")
F.WEAVE.update(n=None)
del F.CLIPS[46]
F.show(P)
check(fake.draws[-1][4].get("weave") is None, "no weave: nothing extra for the GPU")

# --- the looks' commands, the crossfade's bookkeeping, the wobble moving to the GPU ---
check(F.gfx_command(["K", "6", "0.1"]) and F.gfx_command(["R", "3"]) and F.gfx_command(["C", "90", "1.2", "0", "0.05"])
      and F.gfx_command(["X", "1.5"]) and not F.gfx_command(["Z", "1"]),
      "K / R / C / X are the looks' commands (anything else is not)")
check(F.GFX == {"k": (6, 0.1), "r": 3.0, "c": (90.0, 1.2, 0.0, 0.05), "x": 1.5}, "...and they set the looks", repr(F.GFX))
F.gfx_command(["C", "30"])
check(F.GFX["c"] == (30.0, 1.0, 0.0, 0.0), "C with only a hue: the rest at neutral", repr(F.GFX["c"]))
F.show(P)
fx = fake.draws[-1][4]
check(fx["k"] == (6, 0.1) and fx["r"] == 3.0 and fx["c"] == (30.0, 1.0, 0.0, 0.0) and abs(fx["t"] - time.time()) < 1,
      "show() hands the GPU the looks, at the box's clock now")
check(F.gfx_moving(), "a spinning kaleidoscope keeps the screen redrawing")
F.gfx_command(["K", "0", "0"])
F.gfx_command(["C", "0", "1", "0", "0"])
check(not F.gfx_moving(), "nothing moving by itself: no extra redraws")
F.FX["w"] = (10, 1.0, 0.0)
check(F.compose(P) == P and F.gfx_now(1.0)["w"] == (10, 1.0, 0.0),
      "with the GPU the wobble is the GPU's (the composed picture is not shifted)")
F.FX["w"] = None
# crossfade from a still (a pattern) and from a clip still playing out
P9 = picture(9)                                 # (made first: building a picture takes ~0.4 s here)
F.show(P)
t = time.time()
F.start_xf(t)
F.XF["t0"] = t - 0.75                           # 0.75 s into the 1.5 s crossfade
F.show(P9)
d = fake.draws[-1]
check(d[5] == P and abs(d[4]["xf"] - 0.5) < 0.05, "a crossfade from a still: the old picture goes along, halfway at half time", repr(d[4]["xf"]))
F.XF["t0"] = t - 2.0
F.show(P9)
check(fake.draws[-1][5] is None and fake.draws[-1][4]["xf"] is None and F.XF["t0"] is None,
      "after the crossfade time: no old picture, the crossfade is over")
F.CLIPS[44] = picture(1) + picture(2) + picture(3)
F.play.update(n=44, t0=100.0, f0=1, last=-1)
F.start_xf(100.0)
check(F.XF["src"] == ("clip", 44, 100.0, 1) and F.xf_frame(100.0 + 1.5 / 15.0) == picture(3),
      "a crossfade from a clip: the old clip keeps playing out on its own clock")
F.play.update(n=None)
del F.CLIPS[44]
F.XF.update(t0=None, src=None)
F.gfx_command(["X", "0"])
F.show(P)
F.start_xf(time.time())
check(F.XF["t0"] is None, "CROSSFADE 0: hard cuts, as before")
F.GFX.update(k=(0, 0.0), r=0.0, c=(0.0, 1.0, 0.0, 0.0), x=0.0)

# --- hit reactions: h KIND AMT FADE_MS, faded by the box ---
check(F.hit_command(["h", "rgb", "12", "450"]) and not F.hit_command(["h", "3", "ff0000", "0000ff"])
      and not F.hit_command(["c", "000000"]), "h KIND AMT MS starts a hit; 'h 3 ...' is still the stripes pattern")
t0 = F.HITS["rgb"][1]
check(F.hit_env("rgb", t0 + 0.01) == 12.0 and abs(F.hit_env("rgb", t0 + 0.03 + 0.225) - 3.0) < 1e-3,
      "a hit holds 30 ms at full, then eases out: (1 - x)^2 = a quarter at half the fade")
check(F.hit_env("rgb", t0 + 0.6) == 0.0 and "rgb" not in F.HITS, "after the fade it is gone")
F.gfx_command(["R", "3"])
F.hit_command(["h", "rgb", "12", "450"])
F.hit_command(["h", "hue", "90", "450"])
F.hit_command(["h", "invert", "0.8", "450"])
F.gfx_command(["C", "10", "1", "0.5", "0"])
g = F.gfx_now(F.HITS["rgb"][1] + 0.01)
check(g["r"] == 15.0 and g["c"][0] == 100.0 and g["c"][2] == 1.0,
      "hits ride ON TOP of the standing looks (RGB 3 + 12, hue 10 + 90, invert capped at 1)", repr((g["r"], g["c"])))
check(F.gfx_moving(), "a running hit keeps the screen redrawing")
F.HITS.clear()
F.GFX.update(r=0.0, c=(0.0, 1.0, 0.0, 0.0))
F.hit_command(["h", "kaleido", "6", "400"])
tk = F.HITS["kaleido"][1]
check(F.gfx_now(tk + 0.01)["k"][0] == 6 and F.gfx_now(tk + 0.03 + 0.15)["k"][0] == 6 and F.gfx_now(tk + 0.03 + 0.25)["k"][0] == 0,
      "a kaleidoscope hit snaps on, and off when its fade is half done")
F.hit_command(["h", "shudder", "6", "450"])
F.hit_command(["h", "zoom", "0.12", "450"])
ts = F.HITS["shudder"][1]
offs = [F.gfx_now(ts + 0.01 + i * 0.003)["s"] for i in range(8)]
check(all(abs(a) <= 6 and abs(b) <= 6 for a, b in offs) and len(set(offs)) > 4 and F.gfx_now(ts + 0.01)["z"] == 0.12,
      "shudder shakes (moving, never past its size); zoom punches in by its amount", repr(offs[:3]))
F.HITS.clear()
check(not F.gfx_moving() and F.gfx_now(1.0)["z"] == 0.0 and F.gfx_now(1.0)["s"] == (0.0, 0.0), "no hits: no zoom, no shake")
# blur / warp / grey: the reactions that took over from the CPU ones (2026-11-02)
F.hit_command(["h", "blur", "4", "450"])
F.show(P)
check(abs(fake.draws[-1][2] - 4.0) < 0.05, "a blur hit: the GPU draws the blur (level 4 at the start)", repr(fake.draws[-1][2]))
F.BLUR[0] = 1.0
F.HITS["blur"] = (4.0, time.time() - 0.03 - 0.45 * 0.5, 0.45)   # half way: 4 * (1 - 0.5)^2 = 1
F.show(P)
check(abs(fake.draws[-1][2] - 1.0) < 0.1, "...a standing blur and a blur hit: the heavier one shows")
F.BLUR[0] = 0
F.HITS.clear()
F.hit_command(["h", "warp", "10", "450"])
tw = F.HITS["warp"][1]
check(F.gfx_now(tw + 0.01)["w"] == (10.0, 2.0, 1.5), "a warp hit with no standing wobble: a 10 px wave", repr(F.gfx_now(tw + 0.01)["w"]))
F.FX["w"] = (6, 1.0, 0.5)
check(F.gfx_now(tw + 0.01)["w"] == (16.0, 1.0, 0.5), "...on top of a standing wobble it adds to it")
F.FX["w"] = None
F.hit_command(["h", "grey", "1", "450"])
F.gfx_command(["C", "0", "1.5", "0", "0"])
tg = F.HITS["grey"][1]
check(F.gfx_now(tg + 0.01)["c"][1] == 0.0 and abs(F.gfx_now(tg + 0.03 + 0.225)["c"][1] - 1.5 * 0.75) < 1e-3,
      "a grey hit drains the colour (saturation to 0), then it comes back")
F.HITS.clear()
F.gfx_command(["C", "0", "1", "0", "0"])
check(all(F.hit_command(["h", k_, "1", "100"]) for k_ in ("blur", "warp", "grey", "invert")),
      "h blur / warp / grey / invert are hit reactions")
F.HITS.clear()

# --- scenes: S NAME SPEED DETAIL ---
F.CLIPS[45] = picture(4) * 2
F.play.update(n=45, t0=time.time(), f0=0, last=-1)
F.gfx_command(["X", "1"])
F.show(P)
check(F.scene_command(["S", "tunnel", "0.5", "0.3"])
      and (F.SCENE["name"], F.SCENE["speed"], F.SCENE["detail"], F.SCENE["video"]) == ("tunnel", 0.5, 0.3, 0.0)
      and F.play["n"] is None and F.XF["t0"] is not None,
      "S tunnel 0.5 0.3: the scene takes over from the clip (and dissolves it out with CROSSFADE on)")
F.show(P)
fx = fake.draws[-1][4]
F.XF.update(t0=None, src=None)                  # (a running crossfade would keep it redrawing anyway)
check(fx["scene"] == ("tunnel", 0.5, 0.3) and F.gfx_moving(), "show() hands the GPU the scene, and it keeps redrawing")
check(F.scene_command(["S", "off"]) and F.SCENE["name"] is None and not F.scene_command(["s", "0.5", "a", "b"]),
      "S off = no scene; 's ...' is still the split pattern")
F.scene_command(["S", "rings", "1", "2"])
check(F.SCENE["detail"] == 1.0, "DETAIL is clamped to 0-1")
F.SCENE["name"] = None
# SCENE VIDEO: three clips picked when a scene STARTS, kept while it is re-sent, safe from the loader
saved_info = dict(F.SLOTINFO)
F.SLOTINFO.clear()
for s_ in (60, 61, 62, 63):
    F.CLIPS[s_] = picture(s_) * 2
    F.SLOTINFO[s_] = {"id": "c%d" % s_, "bytes": 1, "ready": True, "shown": 0, "last": 0.0, "t": 0.0}
F.scene_command(["S", "rings", "1", "0.5", "1"])
first = [s_ for s_, t_ in F.SCENE["clips"]]
check(len(set(first)) == 3 and set(first) <= set([60, 61, 62, 63]) and set(first) <= F.busy(),
      "a scene with VIDEO starts: three different clips, and the loader may not evict them", repr(first))
F.show(P)
sc_ = fake.draws[-1][4]["scene"]
check(len(sc_) == 6 and sc_[3] == 1.0 and sc_[5] == 0.0 and len(sc_[4]) == 3 and sc_[4][0][1] in [picture(s_) for s_ in first]
      and sc_[4][0][0][0] == first[0],
      "show() hands the GPU the scene with the video amount and the three clips' frames (keyed by slot + frame)")
F.scene_command(["S", "rings", "1", "0.5", "1"])
check([s_ for s_, t_ in F.SCENE["clips"]] == first, "the same scene re-sent next bar keeps its clips")
F.scene_command(["S", "tunnel", "1", "0.5", "1"])
check(63 in [s_ for s_, t_ in F.SCENE["clips"]], "a NEW scene picks again (the least shown: the clip not used yet)")
F.scene_command(["S", "tunnel", "1", "0.5", "0"])
F.show(P)
check(len(fake.draws[-1][4]["scene"]) == 3, "VIDEO 0: no clips handed on (drawn colours only)")
F.gfx_command(["X", "0.5"])                       # a scene -> a clip, with CROSSFADE on
F.scene_command(["S", "rings", "1", "0.5", "0"])
n_snap = getattr(fake, "snaps", 0)
F.start_xf(time.time())
check(F.XF["src"] == ("snap",) and getattr(fake, "snaps", 0) == n_snap + 1 and F.xf_frame(time.time()) is G.SNAP,
      "a clip after a SCENE fades out of the scene's last frame (copied on the GPU), not the black under it")
F.XF.update(t0=None, src=None)
F.gfx_command(["X", "0"])
F.scene_command(["S", "tunnel", "1", "0.5", "0", "0.4"])
F.show(P)
sc_ = fake.draws[-1][4]["scene"]
check(F.SCENE["overlay"] == 0.4 and len(sc_) == 6 and sc_[3] == 0.0 and sc_[5] == 0.4 and len(sc_[4]) == 3,
      "S ... VIDEO 0 OVERLAY 0.4: the clips ARE handed on (the overlay needs them), with the overlay amount")
s_a = F.SCENE["clips"][0][0]
fa, fb = F.clip_frame(s_a, F.CLIPS[s_a], 0), F.clip_frame(s_a, F.CLIPS[s_a], 0)
fc = F.clip_frame(s_a, F.CLIPS[s_a], 1 % F.clip_count(s_a, F.CLIPS[s_a]))
check(fa is fb and (fc is not fa or F.clip_count(s_a, F.CLIPS[s_a]) == 1),
      "the same clip frame again = the same object (so the GPU skips re-sending it); the next = a new one")
F.SCENE.update(name=None, clips=[], overlay=0.0)
# the MASK through the player: M command, its clip (kept from the loader), the kick opening it
F.mask_command(["M", "1", "0.4", "0", "0", "0", "0"])
F.show(P)
mk_ = fake.draws[-1][4].get("mask")
check(mk_ is not None and mk_[0] == 1 and abs(mk_[1] - 0.4) < 1e-6 and mk_[2] == 0 and mk_[6] is not None
      and F.MASK["clip"] in F.busy(), "M 1 0.4 0: a circle with another clip through it; that clip is kept from the loader", mk_[:6])
F.hit_command(["h", "mask", "0.5", "450"])
check(abs(F.gfx_now(time.time())["mask"][1] - 0.9) < 0.01, "h mask 0.5: the kick opens the hole by 0.5 (then it closes)")
F.HITS.clear()
F.mask_command(["M", "3", "0.5", "1", "0.25", "1", "2"])
a_, b_ = F.mask_now(10.0), F.mask_now(10.125)
check(a_[2] == 1 and a_[6] is None and (a_[4], a_[5]) != (b_[4], b_[5]) and a_[1] != b_[1],
      "M square, black, MOVE + PULSE: the hole drifts and breathes (no clip needed for black)", (a_[:6], b_[:6]))
F.mask_command(["M", "0"])
F.show(P)
check(fake.draws[-1][4].get("mask") is None and F.MASK["clip"] not in F.busy() or F.MASK["clip"] is None,
      "M 0: no mask, its clip free again")
F.MASK.update(shape=0, clip=None)
# the FRAME through the player: F command, your frame pictures from FRAME_DIR
fdir = tempfile.mkdtemp(prefix="frames_")
saved_fdir, F.FRAME_DIR = F.FRAME_DIR, fdir
F.frame_command(["F", "1", "0.5", "0.3", "0.7", "0.2", "1"])
fr_ = F.gfx_now(time.time())["frame"]
check(fr_ == (1, 0.5, 0.3, 0.7, 0.1, 1, None), "F 1 0.5 0.3 0.7 0.2 1: a circle at (0.3, 0.7), soft edge, inverted", fr_)
F.frame_command(["F", "5", "1", "0.5", "0.5", "0", "0", "2"])
none_yet = F.frame_now()[6]
with open(os.path.join(fdir, "p2.l8"), "wb") as f:
    f.write(b"\x80" * (G.SW * G.SH))
got = F.frame_now()[6]
check(none_yet is None and got is not None and len(got) == G.SW * G.SH and F.frame_now()[6] is got,
      "F 5 ... 2: your frame picture 2, read once from the box's storage (none there yet = None)")
F.frame_command(["F", "0"])
check(F.gfx_now(time.time())["frame"] is None, "F 0: no frame")
F.FRAME_DIR = saved_fdir
shutil.rmtree(fdir)
check(not set([60, 61, 62, 63]) & F.busy(), "no scene: its clips are free for the loader again")
for s_ in (60, 61, 62, 63):
    del F.CLIPS[s_]
F.SLOTINFO.clear()
F.SLOTINFO.update(saved_info)
del F.CLIPS[45]
F.XF.update(t0=None, src=None)
F.gfx_command(["X", "0"])

# --- COMPRESSED clips through the player: library, loader, slots, frames (fake GPU) ---
saved_dirs = (F.LIB_DIR, F.SLOT_DIR)
lib2, slots2 = tempfile.mkdtemp(prefix="etc_lib_"), tempfile.mkdtemp(prefix="etc_slots_")
F.LIB_DIR, F.SLOT_DIR = lib2, slots2
F.LIB.update(entries=[], mt=None, scan_t=0.0)
F.SLOTINFO.clear()
F.LOADER.update(p=None, slot=None, entry=None, pointer=0, priority=None, t=0.0, roll_t=0.0)
EF = [G.Etc(chr(65 + k) * G.Etc.SIZE) for k in range(3)]
for cid, name, data in (("ee", "clip.etc", b"".join(EF)), ("rr", "clip.raw", picture(1) + picture(2))):
    os.makedirs(os.path.join(lib2, cid))
    with open(os.path.join(lib2, cid, name), "wb") as f:
        f.write(data)
    with open(os.path.join(lib2, cid, "meta.json"), "w") as f:
        f.write('{"name": "%s.mp4", "frames": 2}' % cid)
F.GPU[0] = None
F.scan_library(force=True)
no_gpu = [e["id"] for e in F.LIB["entries"]]
F.GPU[0] = fake
F.scan_library(force=True)
ent = dict((e["id"], e) for e in F.LIB["entries"])
check(no_gpu == ["rr"] and sorted(ent) == ["ee", "rr"] and ent["ee"]["file"] == "clip.etc"
      and ent["ee"]["bytes"] == 3 * G.Etc.SIZE,
      "the library lists a compressed clip (its real size) only when the GPU draws", repr(no_gpu))
os.makedirs(os.path.join(lib2, "bb"))                  # a folder holding BOTH kinds
for name, data in (("clip.raw", picture(1)), ("clip.etc", EF[0]), ("meta.json", '{"name": "bb.mp4"}')):
    with open(os.path.join(lib2, "bb", name), "wb") as f:
        f.write(data)
F.scan_library(force=True)
with_gpu = dict((e["id"], e["file"]) for e in F.LIB["entries"]).get("bb")
F.GPU[0] = None
F.scan_library(force=True)
without = dict((e["id"], e["file"]) for e in F.LIB["entries"]).get("bb")
F.GPU[0] = fake
shutil.rmtree(os.path.join(lib2, "bb"))
F.scan_library(force=True)
check(with_gpu == "clip.etc" and without == "clip.raw",
      "a clip folder with both kinds: the compressed one with the GPU, the raw one without", (with_gpu, without))
t_end = time.time() + 20
while len(F.SLOTINFO) < 2 and time.time() < t_end:
    F.LOADER["t"] = 0.0
    F.loader_tick(time.time())
    time.sleep(0.05)
ee_s = [s_ for s_, i_ in F.SLOTINFO.items() if i_["id"] == "ee"]
ee_s = ee_s[0] if ee_s else None
check(ee_s is not None and os.path.exists(os.path.join(slots2, "s%d.etc" % ee_s)) and ee_s in F.ETC
      and F.clip_count(ee_s, F.CLIPS[ee_s]) == 3,
      "the loader copies it to RAM as sN.etc and the player opens it as a compressed clip of 3 frames",
      repr(sorted(os.listdir(slots2))))
if ee_s is not None:
    tn = time.time()
    F.play.update(n=ee_s, t0=tn, f0=1, last=-1)
    F.clip_tick(tn)
    d = fake.draws[-1][0]
    check(isinstance(d, G.Etc) and d == EF[1], "playing it hands the GPU whole compressed frames (frame 1 of 3)")
    F.LINES[0], F.FX["w"] = (3, 1, "blur"), (10, 1.0, 0.0)
    P1 = picture(1)
    check(F.compose(EF[2]) is EF[2] and F.compose(P1) is P1,
          "with the GPU the CPU touches no frame (compressed or raw): every look is the GPU's")
    F.LINES[0], F.FX["w"] = None, None
    F.WEAVE.update(n=ee_s, amt=0.5, t0=tn)
    check(isinstance(F.weave_frame(tn), G.Etc), "a compressed clip can be the weave's other clip")
    F.WEAVE.update(n=None, amt=0.0)
    F.play.update(n=None)
    F.GPU[0] = None
    check(F.load_slot(ee_s) is False and ee_s not in F.ETC and ee_s not in F.CLIPS,
          "without the GPU a compressed slot is not opened (the framebuffer can't draw it)")
    F.GPU[0] = fake
for s_ in list(F.CLIPS):
    F.drop_slot(s_)
F.SLOTINFO.clear()
F.LIB_DIR, F.SLOT_DIR = saved_dirs
F.LIB.update(entries=[], mt=None, scan_t=0.0)
shutil.rmtree(lib2)
shutil.rmtree(slots2)
F.GPU[0], F.fd = None, fd0
shutil.rmtree(tmp)

total = passed[0] + failed[0]
print("%d passed, %d failed  (expected %d)" % (passed[0], failed[0], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed[0] == 0 and total == EXPECTED else 1)
