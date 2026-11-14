# orcflash.py -- MXV box pattern player (Python 2.7, LibreELEC, Kodi STOPPED).
#
# Draws bold low-res patterns straight into the framebuffer: a small 320x180 buffer,
# stretched to the whole screen by the Amlogic OSD scaler, two pages, paint the hidden
# one then flip (the flip waits for the next refresh: no tearing, lands within a frame).
#
# Listens on UDP 9000 for commands from the Pi only: 10.55.0.1 on the cable, plus the
# Pi Wi-Fi address its conductor writes to /dev/shm/orc/pi_addrs over ssh. One command per line:
#   c RRGGBB                      solid colour
#   v N C1 [C2 ...]               N vertical stripes, cycling the colours
#   h N C1 [C2 ...]               N horizontal stripes
#   k N C1 C2                     checkerboard, N cells across
#   s F C1 C2                     split: left fraction F (0-1) in C1, rest C2 (a wipe = several of these)
#   o C1 C2 R                     a centred box, R (0-1) of the screen's size, in C1 on C2
#   p next [F]                    play the next clip (the box picks: least shown of those in RAM)
#   g N                           only clips of pool N (lib/pools.json, 1-based); 0 = everything
#   p cue N [F]                   play library clip N (mod the library; fetched first if not in RAM)
#   p N [F]                       play the clip in slot N (looping) from frame F; patterns take over again
#   x next AMT                    weave another clip the box picks
#   y L                           blur the whole picture (standing): 0 sharp .. 4 heaviest, fractions in
#                                 between. Every blur is made live by the GPU (gpu.py); none without it.
#                                 (The CPU-drawn borders / bands / colour flash, b m f, were removed
#                                 2026-11-02 at owner's say-so: the GPU hit reactions replace them.)
#   z AMT                         interlace: AMT of the rows heavily blurred (0.5 = every other)
#   x N AMT                       weave: AMT of the rows from clip N, playing alongside (-1 = off)
#   l EVERY THICK blur            spaced BLURRED lines: of every EVERY rows, THICK blurred (by the GPU); l 0 = off
#                                 (coloured l lines were dropped 2026-11-02: use n, the GPU draws its colours)
#   n EVERY THICK FILL            THIN lines: like l, but counted in half picture rows (n 0 = off)
#   a TARGET SECS                 fade the whole picture to brightness TARGET (0 black .. 255 full) over
#                                 SECS seconds (0 = at once), done by the chip
#   t EVERY THICK [ALPHA]       black scanlines on the 2nd screen layer, in REAL pixels (1080p rows,
#                                 scaled on other modes), ALPHA 0-255 (255); t 0 = off
#   K MIRRORS SPIN / R PX / C HUE SAT INV CYCLE / X SECS
#                                 the GPU looks: kaleidoscope, RGB split, colour, crossfade (see GFX)
#   h KIND AMT FADE_MS            a hit reaction the box fades itself (see HITS)
#   S NAME SPEED DETAIL           a scene the GPU draws itself (see SCENE); S off
#   q                             quit (restores nothing: restart kodi to get the GUI back)
#
# The box OWNS its clip library: /storage/orc/lib/<id>/clip.etc (ETC1 640x360, the GPU's own format)
# or the older clip.raw (raw BGRA frames at W x H), CLIP_FPS, + meta.json, put there by the Pi's
# inbox (mxvbox/pi/inbox.py) or the PC tool (mxvbox/tools/make_clips.py), listed in lib/index.json. It keeps RAM SLOTS
# (/dev/shm/orc/sN.*) filled from it with a low-priority copy process, so the drawing never
# waits on the card; the Pi only sends signals (owner 2026-10-29: "the idea of sending
# this box basic signals and it creating this stuff is extremely appealing").
# Status for the Pi (BOX tab): /dev/shm/orc/status.json.
#
# MAP OF THIS FILE (2026-11-03; search for the function names, line numbers drift):
#   screen + fade ..... init_fb, set_alpha, start_fade/fade_tick, init_layer/set_layer (CPU fallback)
#   patterns .......... px, bands, row_of, frame, pattern (+ cache, PATTERN_OPS)
#   looks (GPU) ....... gfx_command, xf_* (crossfade), frame_command/frame_now (FRAME), mask_command/
#                       mask_now (MASK), hit_command/hit_env (HITS), scene_command/scene_now (SCENE),
#                       gfx_now (everything the GPU needs this frame)
#   CPU drawing ....... mix_rows, line_rows, weave_rows, expand, compose, blur_mask (no-GPU path)
#   showing a frame ... show (GPU or framebuffer), meter_report (the journal's frame meters)
#   RAM slots ......... clip_count, clip_frame, drop_slot, load_slot, load_clips, adopt_slots, busy
#   library ........... scan_library, pool_ids, loader_pool, scene_pool, playlist, pick_next, cue, mark_shown
#   loader ............ helper/HelperJob (the one copy shell), trash_slot_files, clear_slot_files, loader_tick
#   status + clips .... write_status, clip_tick
#   commands .......... run_commands (every UDP command; validate BEFORE changing state, unknown = ignored)
#   main loop ......... main (guarded by loop_error), Laps (slow-loop meter), sync_hdmi, init_gpu
# Why one file: ~40 pieces of state (CLIPS, play, SCENE, MASK, LOADER ...) are shared module globals
# and the tests patch several of them (SLOT_DIR, drop_slot, fd ...). A split means moving that state
# into objects first; judged not worth the risk on 2026-11-03 (the review's suggested split:
# commands / looks / patterns / library / slots / fbscreen / main).
import gc, glob, json, math, mmap, os, select, shutil, socket, struct, fcntl, subprocess, sys, time, traceback
import slotpolicy as SP
import gpu as GPUMOD

# The GPU (gpu.py) puts each composed picture on the screen when it can; without it (no EGL, or
# this file exists) the picture goes into the framebuffer the old way. Same looks either way.
GPU_OFF = "/storage/orc/gpu_off"
GPU = [None]                     # the gpu.Screen while it draws

W, H = 320, 180
OUT_W, OUT_H = 1280, 720
# The screen page is VK times taller than the picture (every picture row is written VK times, in
# expand()), so the fine-lines look can draw a line 1/VK of a picture row thick: at 1080p a picture
# row is 6 screen lines tall, a fine line 3 (owner 2026-10-31: "the scan line options are so huge").
VK = 2
HO = H * VK
# The HDMI output: a box that booted with no screen sits on the analog (576cvbs) mode and never
# uses a screen plugged in later. sync_hdmi() fixes that. HDMI_MODE_FILE holds one word.
HDMI_HPD = "/sys/class/amhdmitx/amhdmitx0/hpd_state"
DISPLAY_MODE = "/sys/class/display/mode"
HDMI_MODE_FILE = "/storage/orc/hdmi_mode"          # "720p" (default) or "1080p"
HDMI_MODES = {"720p": (1280, 720), "1080p": (1920, 1080)}
PI = "10.55.0.1"                 # the Pi on the eth0 cable: always obeyed
PI_ADDRS = "/dev/shm/orc/pi_addrs"  # + the Pi's Wi-Fi address, written by its conductor over ssh
PORT = 9000


def read_pi_addrs(path=PI_ADDRS):
    """-> the set of addresses to obey: PI plus every valid IPv4 in PI_ADDRS."""
    ok = set([PI])
    try:
        with open(path) as f:
            for a in f.read().split():
                try:
                    if len(a.split(".")) == 4:
                        socket.inet_aton(a)
                        ok.add(a)
                except socket.error:
                    pass
    except (IOError, OSError):
        pass
    return ok
FBIOGET_VSCREENINFO, FBIOPUT_VSCREENINFO, FBIOGET_FSCREENINFO, FBIOPAN_DISPLAY = 0x4600, 0x4601, 0x4602, 0x4606
FBIOPUT_OSD_SET_GBL_ALPHA = 0x4500      # Amlogic: the whole screen's opacity 0-255, done by the chip (free)

# a TARGET SECS: fade the whole picture to brightness TARGET (0 black .. 255 full) over SECS. The chip
# blends the screen against black, so a fade costs the box nothing (a software fade was 36-40 ms/frame).
FADE = {"cur": 255.0, "frm": 255.0, "to": 255.0, "t0": 0.0, "dur": 0.0, "applied": None}


def fade_alpha(frm, to, t0, dur, now):
    """The brightness `now` into a fade from frm to to that began at t0 and lasts dur seconds."""
    if dur <= 0 or now >= t0 + dur:
        return float(to)
    return frm + (to - frm) * (now - t0) / dur


def set_alpha(a):
    """Put brightness a (0-255) on the screen; False when there is no screen (tests) or the chip refuses."""
    a = int(round(max(0, min(255, a))))
    if fd is None:
        return False
    if a != FADE["applied"]:
        try:
            fcntl.ioctl(fd, FBIOPUT_OSD_SET_GBL_ALPHA, struct.pack("I", a))
        except (IOError, OSError):
            return False
        FADE["applied"] = a
    return True


def start_fade(target, secs, now):
    FADE.update(frm=FADE["cur"], to=float(max(0, min(255, target))), t0=now, dur=max(0.0, secs))


def fade_tick(now):
    """Advance a running fade by one step. True while one is still running."""
    if FADE["cur"] == FADE["to"]:
        return False
    FADE["cur"] = fade_alpha(FADE["frm"], FADE["to"], FADE["t0"], FADE["dur"], now)
    set_alpha(FADE["cur"])
    return FADE["cur"] != FADE["to"]


def sysw(name, val, dev="fb0"):
    with open("/sys/class/graphics/%s/%s" % (dev, name), "w") as f:
        f.write(val)



# Until init_fb() runs (main() calls it) the layout is the plain 320x180x4 one, so the
# drawing functions can be imported and tested without a screen (_test_orcflash.py).
fd = v = fb = None
LINE = W * 4
PAGE = LINE * H                  # one picture (a clip frame, a pattern)
PAGEO = LINE * HO                # one screen page (the picture, rows repeated VK times)
PAD = b""
shown = [0]


def init_fb():
    """Open /dev/fb0 at W x H, two pages, Amlogic free_scale up to OUT_W x OUT_H."""
    global fd, v, LINE, PAGE, PAGEO, PAD, fb
    fd = os.open("/dev/fb0", os.O_RDWR)
    v = bytearray(fcntl.ioctl(fd, FBIOGET_VSCREENINFO, b"\0" * 160))
    struct.pack_into("6I", v, 0, W, HO, W, HO * 2, 0, 0)
    fcntl.ioctl(fd, FBIOPUT_VSCREENINFO, bytes(v))
    v = bytearray(fcntl.ioctl(fd, FBIOGET_VSCREENINFO, b"\0" * 160))
    sysw("free_scale", "0")
    sysw("free_scale_axis", "0 0 %d %d" % (W - 1, HO - 1))
    sysw("window_axis", "0 0 %d %d" % (OUT_W - 1, OUT_H - 1))
    sysw("free_scale", "0x10001")
    LINE = struct.unpack("I", fcntl.ioctl(fd, FBIOGET_FSCREENINFO, b"\0" * 80)[44:48])[0]
    PAGE = LINE * H
    PAGEO = LINE * HO
    PAD = b"\0" * (LINE - W * 4)
    fb = mmap.mmap(fd, PAGEO * 2)


# The black scanlines live on the SECOND screen layer (OSD2 = /dev/fb1), drawn at the screen's own
# resolution, so a line can be 1 real pixel thick whatever the video's 320x180 grid. Scanlines only
# change from row to row, so the layer is a LAYER_W-wide strip the chip stretches across the screen
# (a full 1080p layer does not fit: fb1 has 1 MiB). Measured on the box 2026-11-01.
LAYER_W = 16
LAYER_OFF = "/storage/orc/layer_off"       # if this file exists the 2nd-layer scanlines are not used
LAYER ={"fb": None, "line": 0, "look": None}   # look = (every, thick, alpha) in 1080p pixel rows


def strip_bytes(look, height, line):
    """The strip's pixels (B G R A rows, `line` bytes each, `height` rows). look = (every, thick,
    alpha 0-255) counted in 1080-line pixels, scaled to this screen's height; None = see-through."""
    clear = b"\0" * line
    if not look:
        return clear * height
    s = height / 1080.0
    every = max(2, int(round(look[0] * s)))
    thick = max(1, min(every - 1, int(round(look[1] * s))))
    dark = (b"\0\0\0" + struct.pack("B", max(0, min(255, look[2])))) * LAYER_W + b"\0" * (line - LAYER_W * 4)
    return b"".join(dark if y % every < thick else clear for y in range(height))


def init_layer():
    """Make /dev/fb1 an ARGB strip LAYER_W x OUT_H stretched over the whole screen. False when the
    kernel will not (the thin half-row lines are used instead). The file LAYER_OFF switches it off:
    on 2026-11-01 the layer covered the whole picture (the chip showed its see-through rows opaque)."""
    if os.path.exists(LAYER_OFF):
        print("orcflash: scanline layer switched off (%s): thin half-row lines instead" % LAYER_OFF)
        LAYER["fb"] = None
        return False
    try:
        fd1 = os.open("/dev/fb1", os.O_RDWR)
        v1 = bytearray(fcntl.ioctl(fd1, FBIOGET_VSCREENINFO, b"\0" * 160))
        struct.pack_into("7I", v1, 0, LAYER_W, OUT_H, LAYER_W, OUT_H, 0, 0, 32)
        for off, o in ((32, 16), (44, 8), (56, 0), (68, 24)):       # red, green, blue, alpha
            struct.pack_into("3I", v1, off, o, 8, 0)
        fcntl.ioctl(fd1, FBIOPUT_VSCREENINFO, bytes(v1))
        fix = fcntl.ioctl(fd1, FBIOGET_FSCREENINFO, b"\0" * 80)
        smem, line = struct.unpack_from("I", fix, 20)[0], struct.unpack_from("I", fix, 44)[0]
        if line < LAYER_W * 4 or smem < line * OUT_H:
            raise IOError("layer too small")
        LAYER.update(fb=mmap.mmap(fd1, smem), line=line, look=None)
        LAYER["fb"][0:line * OUT_H] = strip_bytes(None, OUT_H, line)
        sysw("blank", "0", "fb1")
        sysw("free_scale_axis", "0 0 %d %d" % (LAYER_W - 1, OUT_H - 1), "fb1")
        sysw("window_axis", "0 0 %d %d" % (OUT_W - 1, OUT_H - 1), "fb1")
        sysw("free_scale", "0x10001", "fb1")
        return True
    except (IOError, OSError) as e:
        print("orcflash: no scanline layer (%s): thin half-row lines instead" % e)
        LAYER["fb"] = None
        return False


def set_layer(look):
    """Draw the scanline look (every, thick, alpha) or None on the layer. False = no layer."""
    if LAYER["fb"] is None:
        return False
    if look != LAYER["look"]:
        LAYER["fb"][0:LAYER["line"] * OUT_H] = strip_bytes(look, OUT_H, LAYER["line"])
        LAYER["look"] = look
    return True


def px(hexc):
    hexc = hexc.lstrip("#")
    r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
    return chr(b) + chr(g) + chr(r) + "\xff"


def bands(total, n):
    """Split total into n integer widths that add up exactly."""
    n = max(1, min(n, total))
    return [(i + 1) * total // n - i * total // n for i in range(n)]


def row_of(segs):
    return b"".join(px(c) * w for c, w in segs) + PAD


def frame(cmd):
    op, a = cmd[0], cmd[1:]
    if op == "c":
        return row_of([(a[0], W)]) * H
    if op == "v":
        cols = a[1:]
        return row_of([(cols[i % len(cols)], w) for i, w in enumerate(bands(W, int(a[0])))]) * H
    if op == "h":
        cols = a[1:]
        return b"".join(row_of([(cols[i % len(cols)], W)]) * h for i, h in enumerate(bands(H, int(a[0]))))
    if op == "k":
        cells = bands(W, int(a[0]))
        size = W // max(1, int(a[0]))
        r1 = row_of([(a[1 + i % 2], w) for i, w in enumerate(cells)])
        r2 = row_of([(a[1 + (i + 1) % 2], w) for i, w in enumerate(cells)])
        out, y, i = [], 0, 0
        while y < H:
            h = min(size, H - y)
            out.append((r1 if i % 2 == 0 else r2) * h)
            y += h; i += 1
        return b"".join(out)
    if op == "s":
        left = int(round(max(0.0, min(1.0, float(a[0]))) * W))
        return row_of([(a[1], left), (a[2], W - left)]) * H
    if op == "o":
        r = max(0.0, min(1.0, float(a[2])))
        bh, bw = min(int(round(r * H)), H), min(int(round(r * W)), W)   # same shape as the screen
        y0, x0 = (H - bh) // 2, (W - bw) // 2
        outer = row_of([(a[1], W)])
        inner = row_of([(a[1], x0), (a[0], bw), (a[1], W - x0 - bw)])
        return outer * y0 + inner * bh + outer * (H - y0 - bh)
    raise ValueError("unknown op " + op)


base = [None, None]                # the last clip frame / pattern, and its (clip, frame) or None


# Animated row effects, run by the box's own clock (the Pi only sets them):
#   w AMP CYCLES SPEED    wobble: each row slides sideways AMP px on a sine, CYCLES waves down
#                         the screen, moving SPEED cycles/s (0 AMP = off)
# (The CPU raster bars, r, were REMOVED 2026-11-02 with the compressed clips, whose pixels the CPU
# can't touch: owner, "drop the raster". An r command is ignored.)
FX = {"w": None}


def animate(data, now):
    w = FX["w"]
    if GPU[0] is not None:
        w = None                                 # the GPU wobbles (smoothly, for free): gfx_now
    if not w:
        return data
    rows = [data[y * LINE:(y + 1) * LINE] for y in range(H)]
    amp, cyc, spd = w
    ph = 2 * math.pi * spd * now
    k = 2 * math.pi * cyc / H
    for y in range(H):
        s = (int(amp * math.sin(k * y + ph)) % W) * 4
        if s:
            row = rows[y]
            rows[y] = row[s:W * 4] + row[:s] + PAD
    return b"".join(rows)


def fx_active():
    return bool(FX["w"] or (WEAVE["n"] is not None and WEAVE["amt"] > 0) or gfx_moving())


# The GPU LOOKS (2026-11-02), standing until changed, animated by the box's own clock; GPU only
# (without it these commands are kept but draw nothing):
#   K MIRRORS SPIN          kaleidoscope: MIRRORS wedges (0 / 1 = off), turning SPIN turns/s
#   R PX                    RGB split: red and blue pulled PX picture pixels apart (0 = off)
#   C HUE SAT INV CYCLE     colour: hue turned HUE degrees, saturation SAT (1 = as filmed, 0 = grey),
#                           INV 0-1 toward the negative, the hue drifting CYCLE turns/s
#   X SECS                  crossfade: a new clip (or a pattern after a clip) dissolves in over SECS
#                           (0 = a hard cut)
GFX = {"k": (0, 0.0), "r": 0.0, "c": (0.0, 1.0, 0.0, 0.0), "x": 0.0}
XF = {"t0": None, "src": None}           # the crossfade running now: when, and from what


def gfx_command(cmd):
    """K / R / C / X: set a GPU look. True when cmd was one (the caller redraws)."""
    op, a = cmd[0], cmd[1:]
    if op == "K":
        GFX["k"] = (max(0, min(16, int(a[0]))), float(a[1]) if len(a) > 1 else 0.0)
    elif op == "R":
        GFX["r"] = max(0.0, min(32.0, float(a[0])))
    elif op == "C":
        v = [float(x) for x in a] + [0.0, 1.0, 0.0, 0.0][len(a):]
        GFX["c"] = (v[0], max(0.0, min(4.0, v[1])), max(0.0, min(1.0, v[2])), v[3])
    elif op == "X":
        GFX["x"] = max(0.0, min(10.0, float(a[0])))
        if GFX["x"] == 0:
            XF.update(t0=None, src=None)
    else:
        return False
    return True


def xf_progress(now):
    """How far the running crossfade is, 0-1 (eased), or None when none runs."""
    if XF["t0"] is None or GFX["x"] <= 0:
        return None
    x = (now - XF["t0"]) / GFX["x"]
    if x >= 1:
        XF.update(t0=None, src=None)
        return None
    return x * x * (3 - 2 * x)                   # smoothstep: no jolt at either end


def start_xf(now):
    """A clip starts (or a pattern takes over from a clip): fade from what is on screen now."""
    if GPU[0] is None or GFX["x"] <= 0 or base[0] is None:
        return
    if play["n"] is not None and play["n"] in CLIPS:  # a clip was playing: it keeps playing out
        frm = ("clip", play["n"], play["t0"], play["f0"])
    elif SCENE["name"]:                               # a scene: fade out of ITS last frame (copied on the
        GPU[0].snapshot()                             # GPU), not the black picture it is drawn over
        frm = ("snap",)
    else:
        frm = ("still", base[0])
    XF.update(t0=now, src=frm)


def xf_frame(now):
    """The outgoing picture now: the old clip still running on its own clock, or the still."""
    frm = XF["src"]
    if frm is None:
        return None
    if frm[0] == "clip":
        m = CLIPS.get(frm[1])
        if m is None:
            return None
        i = (frm[3] + int((now - frm[2]) * CLIP_FPS)) % clip_count(frm[1], m)
        return clip_frame(frm[1], m, i)
    if frm[0] == "snap":
        return GPUMOD.SNAP                       # (already on the GPU: Screen.snapshot)
    return frm[1]


# HIT REACTIONS (2026-11-02), faded by the box itself (smooth, one message per hit):
#   h KIND AMT FADE_MS      KIND at AMT, held HIT_HOLD s, then easing back to nothing over FADE_MS
#     blur AMT 0-4 | rgb AMT px RGB split | zoom AMT (0.15 = 15 % closer) | shudder AMT px shake
#     | hue AMT degrees | invert AMT 0-1 | grey AMT 0-1 (the colour drains) | warp AMT px (a wave)
#     | kaleido AMT mirrors (snaps on, off when the fade is half done)
# Added on top of the standing looks (GFX). GPU only.
HIT_HOLD = 0.03                          # a hit holds at full this long, then fades
HIT_KINDS = ("blur", "rgb", "zoom", "shudder", "hue", "invert", "grey", "warp", "kaleido", "mask")

# MASK / PEEPHOLE (2026-11-02, owner asked for "masking/peephole stuff"), GPU only:
#   M SHAPE SIZE FILL MOVE PULSE PULSE_HZ
#     SHAPE 0 = off, 1-7 = gpu.MASK_SHAPES (circle diamond square stripes rings bright dark)
#     SIZE 0-1 (for bright / dark: how much of the picture keys through)
#     FILL 0-3 = gpu.MASK_FILLS: another clip (the box picks one, on its own clock) / black /
#          the picture's negative / the picture mirrored
#     MOVE = turns/s the hole drifts round the screen (stripes / rings: slide / ripple), 0 = still
#     PULSE 0-1 = how much the size breathes, PULSE_HZ how fast
#   h mask AMT FADE_MS (a kick, say) opens the hole by AMT and lets it close again.
MASK = {"shape": 0, "size": 0.5, "fill": 0, "move": 0.0, "pulse": 0.0, "pulse_hz": 0.0, "clip": None, "t0": 0.0}


# FRAME / PEEPHOLE (2026-11-02): the light only inside a shape (a spotlight), or blocked inside it
# (INVERT), as the LAST step on the GPU, fixed on the wall while the picture moves:
#   F SHAPE SIZE X Y SOFT INVERT [PIC]   SHAPE 0 = off, 1-5 = gpu.FRAME_SHAPES (5 = your picture PIC,
#     1-4, from FRAME_DIR/pN.l8: SW x SH grey bytes, white = light); X / Y = the centre, 0-1 from
#     the top left; SOFT = the edge's fade, 0-1 of the screen height
FRAME = {"shape": 0, "size": 0.6, "x": 0.5, "y": 0.5, "soft": 0.02, "inv": 0, "pic": 1}
FRAME_DIR = "/storage/orc/frames"
_frame_pics = {}                              # n -> (mtime, bytes)


def frame_command(cmd):
    """F SHAPE SIZE X Y SOFT INVERT [PIC] (F 0 = off). True when cmd was one."""
    if cmd[0] != "F" or len(cmd) < 2:
        return False
    v = [float(x) for x in cmd[1:8]] + [0.0] * (8 - len(cmd))
    shape = int(v[0]) if 0 <= int(v[0]) <= len(GPUMOD.FRAME_SHAPES) else 0
    FRAME.update(shape=shape, size=max(0.02, min(2.0, v[1])), x=max(0.0, min(1.0, v[2])), y=max(0.0, min(1.0, v[3])),
                 soft=max(0.0, min(1.0, v[4])), inv=1 if v[5] >= 0.5 else 0, pic=max(1, min(4, int(v[6]) or 1)))
    return True


def frame_picture(n):
    """Your frame picture n (SW x SH grey bytes), re-read when its file changes; None if there is none."""
    p = os.path.join(FRAME_DIR, "p%d.l8" % n)
    try:
        mt = os.stat(p).st_mtime
    except OSError:
        _frame_pics.pop(n, None)
        return None
    have = _frame_pics.get(n)
    if have is None or have[0] != mt:
        with open(p, "rb") as f:
            data = f.read()
        if len(data) != GPUMOD.SW * GPUMOD.SH:
            return None
        have = _frame_pics[n] = (mt, data)
    return have[1]


def frame_now():
    """The frame for the GPU (gpu.Screen._frame's fr), or None when off."""
    if not FRAME["shape"]:
        return None
    pic = frame_picture(FRAME["pic"]) if FRAME["shape"] == 5 else None
    return (FRAME["shape"], FRAME["size"], FRAME["x"], FRAME["y"], FRAME["soft"] * 0.5, FRAME["inv"], pic)


def mask_command(cmd):
    """M SHAPE SIZE FILL MOVE PULSE PULSE_HZ (M 0 = off). True when cmd was one."""
    if cmd[0] != "M" or len(cmd) < 2:
        return False
    v = [float(x) for x in cmd[1:7]] + [0.0] * (7 - len(cmd))
    shape = int(v[0]) if 0 <= int(v[0]) <= len(GPUMOD.MASK_SHAPES) else 0
    MASK.update(shape=shape, size=max(0.0, min(1.0, v[1])), fill=int(max(0, min(3, v[2]))),
                move=v[3], pulse=max(0.0, min(1.0, v[4])), pulse_hz=v[5])
    if not shape:
        HITS.pop("mask", None)       # only mask_now ages it out: left here it kept the box redrawing forever
    if shape and MASK["fill"] == 0 and MASK["clip"] not in CLIPS:
        mask_new_clip(time.time())
    return True


def mask_new_clip(now):
    """A clip for the hole: the least shown in RAM other than the one playing."""
    MASK["clip"] = pick_next(exclude=set(x for x in (play["n"],) if x is not None), pool=scene_pool())
    MASK["t0"] = now
    if MASK["clip"] is not None:
        mark_shown(MASK["clip"])


def mask_now(now):
    """The mask for the GPU (gpu.Screen._mask's mk), or None when off."""
    if not MASK["shape"]:
        return None
    size = MASK["size"]
    if MASK["pulse"]:
        size *= 1.0 + MASK["pulse"] * 0.6 * math.sin(2 * math.pi * MASK["pulse_hz"] * now)
    size += hit_env("mask", now)
    a = 2 * math.pi * MASK["move"] * now
    cx, cy = (0.5 + 0.3 * math.sin(a), 0.5 + 0.25 * math.sin(a * 1.3 + 1.0)) if MASK["move"] and MASK["shape"] <= 3 else (0.5, 0.5)
    frame = None
    if MASK["fill"] == 0:
        s = MASK["clip"]
        m = CLIPS.get(s)
        if m is None:
            mask_new_clip(now)
            s = MASK["clip"]
            m = CLIPS.get(s)
        if m is not None:
            frame = clip_frame(s, m, int((now - MASK["t0"]) * CLIP_FPS) % clip_count(s, m))
    return (MASK["shape"], size, MASK["fill"], MASK["move"] * now, cx, cy, frame)
WARP_WAVE = (2.0, 1.5)                   # a warp hit with no standing wobble: 2 waves, 1.5 cycles/s
HITS = {}                                # kind -> (amount, t0, fade seconds)


def hit_command(cmd):
    """h KIND AMT FADE_MS: start a hit reaction. True when cmd was one."""
    if cmd[0] != "h" or len(cmd) < 3 or cmd[1] not in HIT_KINDS:
        return False
    HITS[cmd[1]] = (float(cmd[2]), time.time(), max(0.01, float(cmd[3]) / 1000.0 if len(cmd) > 3 else 0.45))
    return True


def hit_env(kind, now):
    """The hit's size now: its amount through the hold, then amount * (1 - x)^2 (eased) to 0."""
    h = HITS.get(kind)
    if h is None:
        return 0.0
    amt, t0, fade = h
    age = now - t0
    if age < HIT_HOLD:
        return amt
    x = (age - HIT_HOLD) / fade
    if x >= 1:
        del HITS[kind]
        return 0.0
    return amt * (1 - x) ** 2


# SCENES (2026-11-02): pictures the GPU draws itself (gpu.SCENES), shown like a pattern:
#   S NAME SPEED DETAIL     NAME = parallax / floor / tunnel / rings / spiral / opart, SPEED cycles/s,
#                           DETAIL 0-1 (warp / turn / twist / density, by scene); S off = no scene
# A scene takes over like a pattern (crossfading out of a clip when CROSSFADE is set); a clip or a
# pattern takes over from it. Without the GPU a scene is black.
#   S NAME SPEED DETAIL VIDEO  VIDEO 0-1 = how much the layers / tiles are filled with your clips
#                              (SCENE VIDEO): three clips, picked from RAM when the scene starts, each
#                              playing on its own clock, kept safe from the loader while it runs.
#   S ... VIDEO OVERLAY        OVERLAY 0-1: the first of those clips laid FLAT over the whole scene,
#                              faded in by that much (2026-11-02, owner: the floor and tunnel are
#                              "jarring and could just maybe use a faded clip on top")
SCENE = {"name": None, "speed": 0.5, "detail": 0.5, "video": 0.0, "overlay": 0.0, "clips": []}
SCENE_CLIPS = 3


def scene_command(cmd):
    """S NAME SPEED DETAIL [VIDEO] / S off. True when cmd was one."""
    if cmd[0] != "S" or len(cmd) < 2:
        return False
    if cmd[1] not in GPUMOD.SCENES:
        SCENE["name"] = None
        return True
    new = dict(name=cmd[1], speed=float(cmd[2]) if len(cmd) > 2 else 0.5,     # parsed FIRST: a bad
               detail=max(0.0, min(1.0, float(cmd[3]))) if len(cmd) > 3 else 0.5,   # number changes nothing
               video=max(0.0, min(1.0, float(cmd[4]))) if len(cmd) > 4 else 0.0,
               overlay=max(0.0, min(1.0, float(cmd[5]))) if len(cmd) > 5 else 0.0)
    if play["n"] is not None:
        start_xf(time.time())                    # a scene after a clip: dissolve (CROSSFADE)
    play["n"] = None
    fresh = SCENE["name"] != cmd[1]              # a scene STARTS (not the same one re-sent each bar)
    SCENE.update(new)
    if fresh or not [s for s, t in SCENE["clips"] if s in CLIPS]:
        pick_scene_clips(time.time())
    return True


def pick_scene_clips(now):
    """Three clips for the scene's layers: the least shown in RAM (repeats when fewer are loaded)."""
    out = []
    for i in range(SCENE_CLIPS):
        s = pick_next(exclude=set(x for x, t in out), pool=scene_pool())
        if s is None:
            break
        mark_shown(s)
        out.append((s, now - i * 0.7))           # each on its own clock (a little apart)
    SCENE["clips"] = out


def scene_frames(now):
    """The scene clips' frames now (the ones still in RAM) as ((slot, frame index), bytes), or None.
    The key lets the GPU skip a frame it already has (clips run at 15 fps, the screen redraws at 30)."""
    out = []
    for s, t0 in SCENE["clips"]:
        m = CLIPS.get(s)
        if m is not None:
            i = int((now - t0) * CLIP_FPS) % clip_count(s, m)
            out.append(((s, i), clip_frame(s, m, i)))
    return out or None


def scene_now(now):
    """The scene for the GPU: (name, speed, detail[, video, frames, overlay]), or None."""
    if not SCENE["name"]:
        return None
    base_ = (SCENE["name"], SCENE["speed"], SCENE["detail"])
    frames = scene_frames(now) if SCENE["video"] > 0 or SCENE["overlay"] > 0 else None
    return base_ + (SCENE["video"], frames, SCENE["overlay"]) if frames else base_


def gfx_moving():
    """True while a GPU look changes by itself (so the screen must be redrawn ~30 times a second)."""
    if GPU[0] is None:
        return False
    k, c = GFX["k"], GFX["c"]
    return bool((k[0] >= 2 and k[1]) or c[3] or XF["t0"] is not None or HITS or SCENE["name"] or MASK["shape"])


def gfx_now(now):
    """Everything the GPU needs for the looks at `now` (gpu.fx_uniforms reads it): the standing
    looks with the running hit reactions on top."""
    hue, sat, inv, cyc = GFX["c"]
    k = GFX["k"]
    kal = HITS.get("kaleido")
    if kal is not None and hit_env("kaleido", now) > kal[0] * 0.25:   # (1-x)^2 = 1/4: half the fade
        k = (int(kal[0]), k[1])
    sh = hit_env("shudder", now)
    w, wp = FX["w"], hit_env("warp", now)
    if wp:
        w = (w[0] + wp, w[1], w[2]) if w else (wp,) + WARP_WAVE
    grey = min(1.0, hit_env("grey", now))
    return {"t": now, "w": w, "k": k, "r": GFX["r"] + hit_env("rgb", now),
            "c": (hue + hit_env("hue", now), sat * (1 - grey), min(1.0, inv + hit_env("invert", now)), cyc),
            "z": hit_env("zoom", now),
            "scene": scene_now(now),
            "mask": mask_now(now),
            "frame": frame_now(),
            "s": (sh * math.sin(now * 97.0), sh * math.cos(now * 71.0)) if sh else (0.0, 0.0),
            "xf": xf_progress(now)}


# z AMT: 0 = sharp .. 1 = all blurred. In between, that share of rows is blurred (by the GPU,
# blur_mask), spread evenly down the screen (bit-reversed order), so it dissolves.
FOCUS = [0.0]
_RANK = []
for _y in range(H):
    _r, _b, _v = 0, 1, _y
    while _b < 256:
        _r = (_r << 1) | (_v & 1); _v >>= 1; _b <<= 1
    _RANK.append(_r / 256.0)


def mix_rows(data, other, amt, skip=()):
    """amt (0-1) of the rows from other, spread evenly down the screen (bit-reversed order).
    Rows in skip (the scanlines' black rows) are left out of the count, so the share is
    taken from the rows you can actually see."""
    if other is None or amt <= 0:
        return data
    if amt >= 1 and not skip:
        return other
    out, j = [], 0
    for y in range(H):
        if y in skip:
            out.append(data[y * LINE:(y + 1) * LINE])
            continue
        out.append(other[y * LINE:(y + 1) * LINE] if _RANK[j] < amt else data[y * LINE:(y + 1) * LINE])
        j += 1
    return b"".join(out)


_lrows = {"key": None, "rows": frozenset()}


def line_rows():
    """The picture rows the lines looks paint completely (empty when off): the weave leaves
    them out. A fine line (half a picture row) only counts when all its output rows are lines.
    Cached per look (it was rebuilt every frame: 3.3 ms of the weave's cost, measured 2026-11-02)."""
    key = (LINES[0], FINE[0])
    if key == _lrows["key"]:
        return _lrows["rows"]
    rows = set()
    ln = LINES[0]
    if ln:
        rows.update(y for y in range(H) if y % ln[0] < ln[1])
    fn = FINE[0]
    if fn:
        rows.update(y for y in range(H) if all(r % fn[0] < fn[1] for r in range(y * VK, y * VK + VK)))
    _lrows.update(key=key, rows=frozenset(rows))
    return _lrows["rows"]


_wrows = {"key": None, "rows": None}


def weave_rows(amt, skip):
    """Which picture rows the weave takes from the other clip, exactly as mix_rows picks them:
    H bytes, 255 = the other clip's row. For the GPU's weave pass. Cached per (amt, skip)."""
    key = (amt, skip)
    if key == _wrows["key"]:
        return _wrows["rows"]
    out, j = [], 0
    for y in range(H):
        if y in skip:
            out.append(b"\x00")
            continue
        out.append(b"\xff" if amt >= 1 or _RANK[j] < amt else b"\x00")
        j += 1
    _wrows.update(key=key, rows=b"".join(out))
    return _wrows["rows"]


# n EVERY THICK FILL: the thin ("fine") lines look, like "l" but counted in OUTPUT rows (a picture
# row is VK of them): n 2 1 000000 = a black line every other output row (half as thick as l 2 1).
FINE = [None]


def expand(data):
    """The picture as the screen page (the framebuffer way): every picture row written VK times;
    with the fine-lines look, the output rows r with r % every < thick are the fill instead of the
    picture (a "blur" fill needs the GPU: no lines here)."""
    rows = [data[y * LINE:(y + 1) * LINE] for y in range(H)]
    fn = FINE[0]
    if not fn or fn[2] == "blur":
        out = []
        for r in rows:
            out += [r] * VK
        return b"".join(out)
    every, thick, fill = fn
    solid = px(fill) * W + PAD
    return b"".join(solid if r % every < thick else rows[r // VK] for r in range(HO))


# y L: the whole picture blurred, L = 0 (sharp) .. 4 (heaviest); the GPU does it (gpu.py).
BLUR = [0]


# x N AMT: weave -- AMT (0-1) of the rows come from clip N, running on its own clock
WEAVE = {"n": None, "amt": 0.0, "t0": 0.0}


def weave_frame(now):
    n = WEAVE["n"]
    m = CLIPS.get(n)
    if m is None or WEAVE["amt"] <= 0:
        return None
    i = int((now - WEAVE["t0"]) * CLIP_FPS) % clip_count(n, m)
    return clip_frame(n, m, i)


# l EVERY THICK FILL: in every EVERY rows, the first THICK are FILL (RRGGBB or "blur"):
# l 2 1 000000 = scanlines, l 6 3 000000 = wide blinds. l 0 = off.
LINES = [None]


def line_fill(s):
    """A lines fill: "blur" or RRGGBB. Anything else raises HERE, when the command arrives: an
    unchecked bad colour used to be stored and then crash the player at the next draw."""
    s = s.lstrip("#")
    if s == "blur":
        return s
    if len(s) != 6:
        raise ValueError("bad colour " + s)
    int(s, 16)
    return s


def lines_command(cmd):
    """l EVERY THICK blur: the spaced blurred lines (l 0 = off). Any other fill turns them OFF:
    the CPU's coloured l lines were dropped 2026-11-02 (the GPU's thin lines, n, do colours)."""
    every = int(cmd[1])
    fill = cmd[3].lstrip("#") if len(cmd) > 3 else ""
    if every < 2 or fill != "blur":
        LINES[0] = None
    else:
        LINES[0] = (every, max(1, min(every - 1, int(cmd[2]))), "blur")


_bmask = {"key": None, "mask": None}


def blur_mask():
    """Which pixels of the composed picture the GPU shows heavily BLURRED: the interlace rows (z),
    then the spaced blurred lines (l).
    W x H bytes, 255 = blurred; None = none. Cached per look. Whole rows only since the CPU borders
    and bands went (2026-11-02)."""
    ln, amt = LINES[0], FOCUS[0]
    key = (amt, ln)
    if key == _bmask["key"]:
        return _bmask["mask"]
    rows = [amt >= 1 or (amt > 0 and _RANK[y] < amt) for y in range(H)]
    if ln:
        every, thick, fill = ln
        for y in range(H):
            if y % every < thick:
                rows[y] = fill == "blur"
    mask = b"".join((b"\xff" if v else b"\x00") * W for v in rows) if any(rows) else None
    _bmask.update(key=key, mask=mask)
    return mask


def compose(data):
    """data with the CPU's effects, which are only the stand-ins for when there is NO GPU: the
    weave and the wobble. With the GPU it is data itself (a compressed frame's pixels are the GPU's
    only); everything (blur, lines, hits, looks, scenes, weave) is drawn there. The CPU's coloured
    l lines and raster bars were dropped 2026-11-02 (owner: "drop the raster and that other")."""
    if GPU[0] is not None:
        return data
    now = time.time()
    if WEAVE["n"] is not None:
        data = mix_rows(data, weave_frame(now), WEAVE["amt"], skip=line_rows())
    return animate(data, now)


shown_at = [0.0]
# PACING with the GPU (2026-11-02): a swap returns right at a screen refresh (60 Hz). The next frame
# starts PACE_S later, just past the following refresh, so it lands on the one after: every frame
# exactly 2 refreshes apart, a steady 30 fps, as long as a frame takes under ~15 ms (it takes 6-10).
# (Drawing every 33 ms from the START of the last draw put frames 33 / 50 ms apart: the scenes'
# stutter. Drawing every refresh gave ~45 fps, a mix of 1- and 2-refresh frames.)
shown_end = [0.0]
PACE_S = 0.0177
# A frame is LATE when it misses its slot: its draw (from the start to the swap) over 26 ms.
LATE_S = 0.026
GAP_S = 0.1             # no new frame this long while something moves = a pause (see show)


# frame meter: what one draw costs (compose = the effects, flip = waiting for the refresh)
METER = {"t0": time.time(), "n": 0, "compose": 0.0, "worst": 0.0, "over": 0}


def show(data, src=None):
    """data = the picture; src = (clip, frame) when it is a clip frame (kept for redraws)."""
    base[0], base[1] = data, src
    if GPU[0] is not None:
        # GAP meter (2026-11-02): while something moves (or a clip plays: a frame every 67 ms) the
        # screen should never go GAP_S without a new frame. A gap is a pause the late-frame meter
        # can't see (it only times a draw), so name what was going on.
        idle = time.time() - shown_end[0]
        was_moving = METER.get("moving")              # (as of the last frame: a clip starting after a still
        METER["moving"] = fx_active() or play["n"] is not None   # pattern is not a gap)
        if idle > GAP_S and shown_end[0] and was_moving and time.time() - METER.get("gap_t", 0) > 1.0:
            METER["gap_t"] = time.time()
            print("gap %.0f ms with no new frame: copying %s, last load %.1f s ago, scene %s, crossfade %s"
                  % (idle * 1000, LOADER["entry"]["id"] if LOADER["p"] is not None else "-",
                     time.time() - LOADER.get("done_t", 0.0), SCENE["name"] or "-", "on" if XF["t0"] is not None else "-"))
            sys.stdout.flush()
    if GPU[0] is not None:                            # never start before PACE_S after the last swap
        gap = shown_end[0] + PACE_S - time.time()
        if 0 < gap <= PACE_S:
            time.sleep(gap)
    t0 = shown_at[0] = time.time()
    if GPU[0] is not None:
        c0 = os.times()
        pic = compose(data)
        t1 = time.time()
        c1 = os.times()
        # the GPU makes every blur itself (y, z, blur borders / bands / lines), for any picture,
        # and the looks (wobble, kaleidoscope, RGB split, colour, crossfade)
        pt = [time.time()]                            # pre-draw step times (the late line names them)
        fx = gfx_now(t0)
        pt.append(time.time())
        wf = weave_frame(t0) if WEAVE["n"] is not None else None
        if wf is not None:                            # the weave, on the GPU (25-55 ms in Python live)
            fx["weave"] = (wf, weave_rows(WEAVE["amt"], line_rows()))
        pt.append(time.time())
        prev = xf_frame(t0) if fx["xf"] is not None else None
        blur = max(BLUR[0], hit_env("blur", t0))     # the standing blur, or a blur hit if heavier
        bm = blur_mask()
        pt.append(time.time())
        METER["pre"] = "/".join(str(int(round((pt[i + 1] - pt[i]) * 1000))) for i in range(3))
        GPU[0].draw(pic, FINE[0], blur, bm, fx, prev)   # waits for the refresh, like the flip below
        t2 = shown_end[0] = time.time()               # (= a screen refresh: the pacing's anchor)
        METER["n"] += 1
        METER["compose"] += t1 - t0
        METER["worst"] = max(METER["worst"], t2 - t0)
        METER["over"] += (t2 - t0) > LATE_S
        if t2 - t0 > LATE_S and t2 - METER.get("late_t", 0) > 1.0:   # what was going on (1 line a second at most)
            METER["late_t"] = t2
            # compose CPU = the player's own CPU time in compose: near the wall time = real work there;
            # far below it = the player was stalled (another process, or the kernel)
            print("late frame %.0f ms (compose %.0f, its CPU %.0f, draw %.0f = looks/weave/outgoing %s "
                  "+ gpu picture/outgoing/blur/screen/swap %s): "
                  "copying %s, last load %.1f s ago, hits %s, crossfade %s, weave %s, scene %s, blur %.1f"
                  % ((t2 - t0) * 1000, (t1 - t0) * 1000, (c1[0] + c1[1] - c0[0] - c0[1]) * 1000, (t2 - t1) * 1000,
                     METER.get("pre", "-"), "/".join(str(x) for x in getattr(GPU[0], "stages", ())) or "-",
                     LOADER["entry"]["id"] if LOADER["p"] is not None else "-", t2 - LOADER.get("done_t", 0.0),
                     ",".join(sorted(HITS)) or "-", "on" if prev is not None else "-",
                     "on" if WEAVE["n"] is not None else "-", SCENE["name"] or "-", blur))
            sys.stdout.flush()
        return
    hidden = 1 - shown[0]
    pic = expand(compose(data))
    t1 = time.time()
    fb[hidden * PAGEO:(hidden + 1) * PAGEO] = pic
    struct.pack_into("2I", v, 16, 0, hidden * HO)
    fcntl.ioctl(fd, FBIOPAN_DISPLAY, bytes(v))
    shown[0] = hidden
    t2 = time.time()
    METER["n"] += 1
    METER["compose"] += t1 - t0
    METER["worst"] = max(METER["worst"], t2 - t0)
    METER["over"] += (t2 - t0) > 1 / 30.0


def meter_report(now):
    """Every 10 s, one journal line: frames drawn, compose average, worst draw, over 33 ms."""
    if now - METER["t0"] < 10:
        return
    n = max(1, METER["n"])
    print("frames %d (%.0f/s)  compose avg %.1f ms  worst draw %.0f ms  late: %d"
          % (METER["n"], METER["n"] / (now - METER["t0"]), 1000 * METER["compose"] / n,
             1000 * METER["worst"], METER["over"]))
    sys.stdout.flush()
    METER.update(t0=now, n=0, compose=0.0, worst=0.0, over=0)


CLIP_FPS = 15.0
SLOT_DIR = "/dev/shm/orc"
CLIPS = {}                       # slot -> mmap of its frames
ETC = set()                      # the slots whose clip is COMPRESSED (sN.etc: gpu.Etc frames; GPU only)
_files = {}                      # slot -> [open files], closed on drop
# A clip is clip.raw (W x H BGRA frames) or, since 2026-11-02, clip.etc: ETC1 frames at the GPU
# surface's size (640x360, 4x the pixels in half the RAM). Only the GPU can draw a .etc, so without
# it they are left out of the library.
CLIP_FILES = ("clip.etc", "clip.raw")     # (the compressed kind first when both are there)


def clip_count(s, m):
    """How many frames slot s's clip m holds."""
    return len(m) // (GPUMOD.Etc.SIZE if s in ETC else PAGE)


_last_frame = {}                 # slot -> (frame index, frame): the SAME object while it is due


def clip_frame(s, m, i):
    """Frame i of slot s's clip m: raw bytes, or a gpu.Etc for a compressed clip. Asked again for
    the frame it gave last, it returns that very object, so the GPU can skip sending it again (the
    screen redraws ~35 times a second, a clip changes 15: crossfades into scenes ran late)."""
    last = _last_frame.get(s)
    if last is not None and last[0] == i:
        return last[1]
    if s in ETC:
        n = GPUMOD.Etc.SIZE
        fr = GPUMOD.Etc(m[i * n:(i + 1) * n])
    else:
        fr = m[i * PAGE:(i + 1) * PAGE]
    _last_frame[s] = (i, fr)
    return fr


def drop_slot(s):
    """Let go of slot s (the Pi deletes / replaces its files next)."""
    m = CLIPS.pop(s, None)
    ETC.discard(s)
    _last_frame.pop(s, None)
    if m is not None:
        m.close()
    for f in _files.pop(s, []):
        f.close()
    if play["n"] == s:
        play["n"] = None                 # the last frame stays on screen until the next picture
    if WEAVE["n"] == s:
        WEAVE["n"] = None


def load_slot(s):
    """(Re)open slot s from SLOT_DIR. True if it now holds a clip."""
    drop_slot(s)
    p = os.path.join(SLOT_DIR, "s%d.raw" % s)
    etc = not os.path.exists(p) and GPU[0] is not None
    if etc:
        p = os.path.join(SLOT_DIR, "s%d.etc" % s)
    if PAD or not os.path.exists(p) or os.path.getsize(p) < (GPUMOD.Etc.SIZE if etc else PAGE):
        return False
    fs = [open(p, "rb")]
    CLIPS[s] = mmap.mmap(fs[0].fileno(), 0, prot=mmap.PROT_READ)
    _files[s] = fs
    if etc:
        ETC.add(s)
    return True


def load_clips():
    """At start: open every slot already in SLOT_DIR (they survive a player restart,
    not a box reboot: /dev/shm is RAM)."""
    if PAD:
        print("clips need line == W*4; this fb pads lines, clips off")
        return
    if not os.path.isdir(SLOT_DIR):
        os.makedirs(SLOT_DIR)
    for name in os.listdir(SLOT_DIR):
        if name.startswith("trash_"):                 # dropped before a restart: not yet deleted
            try:
                os.remove(os.path.join(SLOT_DIR, name))
            except OSError:
                pass
        elif name.startswith("in") and name[2:].isdigit():   # a copy cut off by a restart (up to ~40 MB
            shutil.rmtree(os.path.join(SLOT_DIR, name), ignore_errors=True)   # nothing counted)
        elif name.startswith("s") and name.endswith((".raw", ".etc")) and "_" not in name and name[1:-4].isdigit():
            load_slot(int(name[1:-4]))

play = {"n": None, "t0": 0.0, "f0": 0, "last": -1}

LIB_DIR = "/storage/orc/lib"
STATUS = os.path.join(SLOT_DIR, "status.json")
# of /dev/shm (remounted to 640 MB by the service). 620 MB froze the box solid on 2026-11-02: with the
# slots full only ~120 MB stayed free, a GPU test run took the rest, and with no swap it thrashed
# (pings answered, ssh could not even greet) until owner power-cycled it. 520 leaves ~220 MB.
SLOT_BUDGET = 520 * 2 ** 20
SLOT_MAX = 24                    # slots at most (short clips are small, so more than 16 can fit)
# a clip stays in RAM at least this long before it can be swapped, and the loader swaps one at most
# every ROLL_GAP_S. At 20 s / no gap it rolled the whole library through RAM, a 40 MB copy every ~2 s,
# and the box's late frames (the snags owner saw) clustered in those windows (2026-11-02).
SLOT_MIN_S = 60.0
ROLL_GAP_S = 8.0
LIB = {"entries": [], "mt": None, "scan_t": 0.0}
SLOTINFO = {}                    # slot -> {id, bytes, ready, shown, last}
POOLS = {"list": [], "n": 0, "n2": 0}   # lib/pools.json [{name, ids}]; n = the clips' pool (g N), n2 = the
                                  # scenes' (g2 N, 0 = the same as the clips'); 0 = everything
# lib/flags.json {"fav": [ids], "never": [ids]} (the BOX tab writes it, 2026-11-02): a FAVOURITE is
# loaded first and shown about twice as often, a NEVER clip is not loaded or shown.
FLAGS = {"fav": set(), "never": set()}
# Q ID: PLAY NEXT (the BOX tab): the clip bars' next pick is that clip, once it is in RAM (it is
# fetched first). Q - = forget it.
QUEUE = {"id": None}
LOADER = {"p": None, "slot": None, "entry": None, "pointer": 0, "priority": None, "t": 0.0,
          "status_t": 0.0, "roll_t": 0.0}


def scan_library(force=False):
    """Re-read the card's library when its folder changed; write the tiny index.json."""
    try:
        mt = os.stat(LIB_DIR).st_mtime
    except OSError:
        LIB["entries"] = []
        return
    if mt == LIB["mt"] and not force:
        return
    out = []
    for cid in sorted(os.listdir(LIB_DIR)):
        try:
            with open(os.path.join(LIB_DIR, cid, "meta.json")) as f:
                m = json.load(f)
            for fname in CLIP_FILES if GPU[0] is not None else ("clip.raw",):
                raw = os.path.join(LIB_DIR, cid, fname)
                if os.path.exists(raw):              # bytes = what a RAM slot really takes
                    out.append({"id": cid, "bytes": os.path.getsize(raw), "file": fname,
                                "frames": int(m.get("frames", 0)), "name": m.get("name", cid)})
                    break
        except (IOError, OSError, ValueError):
            pass
    LIB["entries"], LIB["mt"] = out, mt
    try:                                         # the pools (the Pi's BOX tab writes them)
        with open(os.path.join(LIB_DIR, "pools.json")) as f:
            POOLS["list"] = [{"name": unicode(p.get("name", ""))[:40], "ids": [str(i) for i in p.get("ids", [])],
                              "order": bool(p.get("order"))}       # order = a PLAYLIST (your order, looping)
                             for p in json.load(f) if isinstance(p, dict)]
    except (IOError, OSError, ValueError, TypeError, AttributeError):
        POOLS["list"] = []
    try:                                         # favourites / never (the BOX tab writes them)
        with open(os.path.join(LIB_DIR, "flags.json")) as f:
            fl = json.load(f)
        FLAGS["fav"], FLAGS["never"] = set(str(i) for i in fl.get("fav", [])), set(str(i) for i in fl.get("never", []))
    except (IOError, OSError, ValueError, TypeError, AttributeError):
        FLAGS["fav"], FLAGS["never"] = set(), set()
    ids = set(e["id"] for e in out)
    for s in [s for s, i in SLOTINFO.items() if i["id"] not in ids]:
        drop_slot(s)                             # a clip deleted from the library leaves RAM too
        clear_slot_files(s)                      # (if it was on screen, the next p next moves on)
        del SLOTINFO[s]
        print("slot %d: its clip was deleted from the library, let go" % s)
        sys.stdout.flush()
    # Writing index.json changes the library folder's own time, so the folder time is read again
    # AFTER the write, and the file is only written when it changes. Until 2026-11-02 the time was
    # taken before the write: every 5 s check then saw a "changed" folder and re-read all ~80
    # clips' meta.json from flash on the drawing thread, 80-180 ms (the 'little pauses' owner
    # saw for days; found by the slow-loop meter).
    idx = json.dumps([{"id": e["id"], "name": e["name"], "frames": e["frames"]} for e in out])
    try:
        with open(os.path.join(LIB_DIR, "index.json")) as f:
            same = f.read() == idx
    except (IOError, OSError):
        same = False
    try:
        if not same:                             # (unchanged: keep the time read BEFORE the scan, so a
            with open(os.path.join(LIB_DIR, "index.json.tmp"), "w") as f:     # clip added during it still counts)
                f.write(idx)
            os.rename(os.path.join(LIB_DIR, "index.json.tmp"), os.path.join(LIB_DIR, "index.json"))
            LIB["mt"] = os.stat(LIB_DIR).st_mtime
    except (IOError, OSError):
        pass


def adopt_slots():
    """At start: the slots already in RAM (a player restart keeps /dev/shm) with their .id."""
    meta = dict((e["id"], e) for e in LIB["entries"])
    for s in list(CLIPS):
        try:
            cid = open(os.path.join(SLOT_DIR, "s%d.id" % s)).read().strip()
        except IOError:
            drop_slot(s)
            clear_slot_files(s)              # a copy cut off between its mv and its .id: RAM nothing counted
            continue
        e = meta.get(cid, {})
        SLOTINFO[s] = {"id": cid, "bytes": e.get("bytes", 0) or len(CLIPS[s]),
                       "ready": True, "shown": 0, "last": 0.0, "t": time.time()}


def busy():
    """The slots on screen right now: never evicted (the clip, the weave, a scene's clips)."""
    on = set(x for x in (play["n"], WEAVE["n"]) if x is not None)
    if SCENE["name"]:
        on.update(s for s, t in SCENE["clips"])
    if MASK["shape"] and MASK["fill"] == 0 and MASK["clip"] is not None:
        on.add(MASK["clip"])                     # (the clip seen through the mask)
    src = XF["src"]
    if XF["t0"] is not None and src and src[0] == "clip":
        on.add(src[1])                           # (the clip a crossfade is fading OUT of: evicting it
    return on                                    #  turned the fade into a cut, 2026-11-03 review)


def mark_shown(s):
    if s in SLOTINFO:
        SLOTINFO[s]["shown"] += 1
        SLOTINFO[s]["last"] = time.time()


def pool_ids(n=None):
    """Pool n's clip ids that are in the library (default: the active one, g N), or None =
    everything (pool 0, a pool that doesn't exist, or one whose clips are all gone)."""
    n = POOLS["n"] if n is None else n
    if not 1 <= n <= len(POOLS["list"]):
        return None
    have = set(e["id"] for e in LIB["entries"])
    ids = set(POOLS["list"][n - 1]["ids"]) & have
    return ids or None


def loader_pool():
    """What the loader keeps in RAM: the clips' pool and the scene pool together (None = everything)."""
    a, b = pool_ids(), (pool_ids(POOLS["n2"]) if POOLS["n2"] else None)
    if a is None or (POOLS["n2"] and b is None):
        return None
    return a | (b or set())


def scene_pool():
    """The pool the scenes, the overlay and the mask take their clips from: SCENE POOL (g2 N), or
    the clips' own pool when it is 0."""
    return pool_ids(POOLS["n2"]) if POOLS["n2"] else pool_ids()


# PLAYLISTS (2026-11-02): a pool marked "order" plays its clips in YOUR order, looping, when POOL
# picks it (NEVER clips and clips gone from the library are left out). pos = the index played last.
PLAYLIST = {"n": None, "pos": -1}


def playlist():
    """The active playlist's clip ids in order, or None when POOL is not a playlist."""
    n = POOLS["n"]
    if not 1 <= n <= len(POOLS["list"]) or not POOLS["list"][n - 1].get("order"):
        return None
    if PLAYLIST["n"] != n:                       # another playlist: from its start
        PLAYLIST.update(n=n, pos=-1)
    have = set(e["id"] for e in LIB["entries"])
    return [i for i in POOLS["list"][n - 1]["ids"] if i in have and i not in FLAGS["never"]]


def pick_next(exclude=(), pool=None, main=False):
    """The slot to show next. main = the clip bars' own pick: a clip queued with Q (PLAY NEXT on the
    BOX tab) comes first once it is in RAM. Favourites come up about twice as often; NEVER clips not."""
    if main and QUEUE["id"]:
        for s, i in SLOTINFO.items():
            if i["id"] == QUEUE["id"] and i["ready"] and s not in exclude:
                QUEUE["id"] = None
                return s
    pl = playlist()
    if main and pl is not None:                 # a PLAYLIST: the next one in your order
        s, j, fetch = SP.next_in_order(pl, SLOTINFO, PLAYLIST["pos"], exclude)
        if fetch and not QUEUE["id"]:
            LOADER["priority"] = fetch           # (the one coming up is fetched before it is due)
        if s is not None:
            PLAYLIST["pos"] = j
        return s
    return SP.pick_clip(SLOTINFO, exclude, pool if pool is not None else pool_ids(),
                        favs=FLAGS["fav"], never=FLAGS["never"])


def cue(index):
    """Library clip `index` (mod): its slot if in RAM, else None (and it is fetched first)."""
    if not LIB["entries"]:
        return None
    cid = LIB["entries"][index % len(LIB["entries"])]["id"]
    for s, i in SLOTINFO.items():
        if i["id"] == cid and i["ready"]:
            return s
    LOADER["priority"] = cid
    return None


_trash_n = [0]


def trash_slot_files(s):
    """Slot s's files renamed out of the way at once (a rename is instant); the helper deletes
    them before its next copy (load_clips ignores names with a "_")."""
    for name in ("s%d.raw" % s, "s%d.etc" % s, "s%d.id" % s):
        _trash_n[0] += 1
        try:
            os.rename(os.path.join(SLOT_DIR, name), os.path.join(SLOT_DIR, "trash_%d_%s" % (_trash_n[0], name)))
        except OSError:
            pass


# The COPY HELPER (2026-11-02): one small shell, started early while the player is small, runs
# each copy at idle priority, one at a time. Starting each copy with subprocess.Popen forked the
# whole player (with 500+ MB of clips mapped): 50-120 ms on the drawing thread every ~8 s while the
# library rolled (found by the slow-loop meter).
_helper = [None]
HELPER_LOOP = 'while read -r l; do sh -c "$l"; echo "rc $?"; done'


def helper():
    h = _helper[0]
    if h is None or h.poll() is not None:
        h = _helper[0] = subprocess.Popen(["nice", "-n", "19", "ionice", "-c", "3", "sh", "-c", HELPER_LOOP],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return h


class HelperJob(object):
    """One script run by the helper, polled like a subprocess (poll() / returncode)."""

    TIMEOUT_S = 180.0                            # a 40 MB copy takes seconds; a hung one stopped the library for good

    def __init__(self, script):
        self.h, self.returncode, self.t0 = helper(), None, time.time()
        try:
            self.h.stdin.write(script.replace("\n", " ") + "\n")
            self.h.stdin.flush()
        except (IOError, OSError):
            self.returncode = 1                  # the helper died: this copy failed, the next starts a new one

    def poll(self):
        if self.returncode is None and select.select([self.h.stdout], [], [], 0)[0]:
            line = self.h.stdout.readline()
            self.returncode = int(line.split()[1]) if line.startswith("rc ") else 1
        if self.returncode is None and time.time() - self.t0 > self.TIMEOUT_S:
            try:
                self.h.kill()                    # the next job starts a fresh helper
            except OSError:
                pass
            print("copy helper stuck %.0f s: restarted" % self.TIMEOUT_S)
            self.returncode = 1
        return self.returncode


def clear_slot_files(s):
    for name in ("s%d.raw" % s, "s%d.etc" % s, "s%d.id" % s):
        try:
            os.remove(os.path.join(SLOT_DIR, name))
        except OSError:
            pass


def loader_tick(now):
    """Keep the RAM slots filled from the card, one low-priority copy at a time."""
    p = LOADER["p"]
    if p is not None:
        if p.poll() is None:
            return                               # still copying
        s, e = LOADER["slot"], LOADER["entry"]
        LOADER["p"] = None
        scan_library()                           # deleted while it was being copied in?
        still = any(x["id"] == e["id"] for x in LIB["entries"])
        if p.returncode == 0 and still and load_slot(s):
            SLOTINFO[s] = {"id": e["id"], "bytes": e["bytes"],
                           "ready": True, "shown": 0, "last": 0.0, "t": time.time()}
            if LOADER["priority"] == e["id"]:
                LOADER["priority"] = None
            print("slot %d <- %s (%.0f MB)" % (s, e["id"], e["bytes"] / 2.0 ** 20))
            LOADER["done_t"] = time.time()            # (the late-frame line says how long ago)
        else:
            clear_slot_files(s)
            print("slot %d: copy of %s FAILED (rc %s)" % (s, e["id"], p.returncode))
        sys.stdout.flush()
    if now - LOADER["t"] < 0.5:
        return
    LOADER["t"] = now
    if now - LIB["scan_t"] > 5:
        LIB["scan_t"] = now
        scan_library()
    pool = loader_pool()
    never = FLAGS["never"]
    lib = [e for e in LIB["entries"] if (pool is None or e["id"] in pool) and e["id"] not in never]
    for want_id in (QUEUE["id"], LOADER["priority"]):   # a queued / cued clip outside the pool or NEVER:
        if want_id and not any(e["id"] == want_id for e in lib):    # fetched anyway (a cue never was)
            lib += [e for e in LIB["entries"] if e["id"] == want_id]
    want, ptr = SP.next_wanted(lib, SLOTINFO, LOADER["pointer"], LOADER["priority"], prefer=FLAGS["fav"])
    if want is None:
        return
    allowed = set(e["id"] for e in lib)
    keep = (allowed | set([want["id"]])) if (pool or never) else None   # (anything else in RAM goes first)
    where = SP.choose_slot(SLOTINFO, want["bytes"], busy(), SLOT_BUDGET, max_slots=SLOT_MAX, now=now, min_age=SLOT_MIN_S, keep=keep)
    if where is None:
        return
    kind, s = where
    if kind == "evict":
        rolling = keep is None or SLOTINFO[s]["id"] in keep   # (a clip OUTSIDE the picked pool goes at once)
        if rolling and now - LOADER["roll_t"] < ROLL_GAP_S:
            return
        LOADER["roll_t"] = now
        drop_slot(s)
        trash_slot_files(s)                      # (deleting a 20-40 MB RAM file here stalled the drawing)
        del SLOTINFO[s]
        return
    src_dir = os.path.join(LIB_DIR, want["id"])
    tmp = os.path.join(SLOT_DIR, "in%d" % s)
    fname = want.get("file", "clip.raw")
    script = ("rm -f {slots}/trash_*; rm -rf {t} && mkdir -p {t} && cp {d}/{f} {t}/s{s}.{x} && "
              "mv {t}/* {slots}/ && rmdir {t} && echo {cid} > {slots}/s{s}.id").format(
                  t=tmp, d=src_dir, f=fname, x=fname[-3:], s=s, slots=SLOT_DIR, cid=want["id"])
    clear_slot_files(s)
    LOADER.update(p=HelperJob(script), slot=s, entry=want, pointer=ptr)


def write_status(now):
    if now - LOADER["status_t"] < 2:
        return
    LOADER["status_t"] = now
    st = {"t": now, "library": len(LIB["entries"]),
          "loading": LOADER["entry"]["id"] if LOADER["p"] is not None else None,
          "slots": [{"slot": s, "id": i["id"], "shown": i["shown"]}
                    for s, i in sorted(SLOTINFO.items())],
          "playing": SLOTINFO.get(play["n"], {}).get("id"),
          "pool": POOLS["n"], "pools": [p["name"] for p in POOLS["list"]],
          "pool2": POOLS["n2"], "queued": QUEUE["id"],
          "playlist_pos": PLAYLIST["pos"] + 1 if PLAYLIST["n"] == POOLS["n"] and playlist() is not None else 0}
    try:
        data = json.dumps(st)                    # one write: Python 2's json.dump writes token by token,
        with open(STATUS + ".tmp", "w") as f:    # 3.5 ms vs 0.5 ms on the box (measured 2026-11-03)
            f.write(data)
        os.rename(STATUS + ".tmp", STATUS)
    except (IOError, OSError):
        pass


def clip_tick(now):
    """Show the clip frame due now (if it changed). Returns seconds until the next one."""
    if play["n"] is None:
        return None
    m = CLIPS.get(play["n"])
    if m is None:
        play["n"] = None
        return None
    count = clip_count(play["n"], m)
    i = (play["f0"] + int((now - play["t0"]) * CLIP_FPS)) % count
    if i != play["last"]:
        show(clip_frame(play["n"], m, i), (play["n"], i))
        play["last"] = i
    nxt = play["t0"] + (int((now - play["t0"]) * CLIP_FPS) + 1) / CLIP_FPS
    return max(0.0, nxt - time.time())


cache = {}
CACHE_MAX = 24        # a pattern is 230 KB: 200 of them (the old cap) was ~46 MB on a box that froze from RAM
PATTERN_OPS = ("c", "v", "h", "k", "s", "o")
_unknown = {"t": 0.0}


def unknown_command(line):
    """Ignore a command this player does not know (one journal line every 10 s at most)."""
    if time.time() - _unknown["t"] > 10:
        _unknown["t"] = time.time()
        print("ignored unknown command %r" % line[:80])
        sys.stdout.flush()


def pattern(cmd):
    key = " ".join(cmd)
    f = cache.get(key)
    if f is None:
        f = frame(cmd)
        if len(cache) >= CACHE_MAX:
            cache.clear()
        cache[key] = f
    return f


def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (IOError, OSError):
        return ""


def sync_hdmi():
    """With a screen on the HDMI port (hpd 1), make the video output the wanted HDMI mode.
    True when it had to change the mode (the screen setup is then stale: the caller restarts
    the player, which re-applies it in init_fb). OUT_W/OUT_H follow the mode either way."""
    global OUT_W, OUT_H
    if read_sys(HDMI_HPD) != "1":
        return False
    want = read_sys(HDMI_MODE_FILE)
    if want not in HDMI_MODES:
        want = "720p"
    cur = read_sys(DISPLAY_MODE)
    if cur in HDMI_MODES:
        OUT_W, OUT_H = HDMI_MODES[cur]
    if cur == want:
        return False
    with open(DISPLAY_MODE, "w") as f:
        f.write(want)
    OUT_W, OUT_H = HDMI_MODES[want]
    time.sleep(1.5)                  # let the mode switch settle
    return True


def init_gpu(make=None):
    """Draw through the GPU unless GPU_OFF exists or it will not start. True = it draws.
    The 2nd-layer scanlines stay off (the `t` command falls back to the thin lines, which
    the GPU draws at its own rows); the fade still goes through fb0 (the chip, set_alpha)."""
    global fd
    if os.path.exists(GPU_OFF):
        print("orcflash: GPU switched off (%s): framebuffer drawing" % GPU_OFF)
        return False
    try:
        GPU[0] = (make or GPUMOD.Screen)(W, H, OUT_W, OUT_H, sysw=sysw)
    except Exception as e:
        print("orcflash: no GPU (%s): framebuffer drawing" % e)
        GPU[0] = None
        return False
    fd = GPU[0].fd
    print("orcflash: drawing through the GPU (%dx%d surface)" % (GPUMOD.SW, GPUMOD.SH))
    return True


_err = {"t": 0.0, "n": 0}


def loop_error():
    """An error in the main loop: log it (the traceback every 10 s at most, with a count), back off
    a moment so a repeating one can't spin, and carry on with the last picture."""
    _err["n"] += 1
    if time.time() - _err["t"] > 10:
        _err["t"] = time.time()
        print("main loop error (%d so far):" % _err["n"])
        traceback.print_exc()
        sys.stdout.flush()
    time.sleep(0.05)


LOOP_SLOW_S = 0.08      # a stretch of the main loop's own work this long = a visible pause
_slow_t = [0.0]


class Laps(object):
    """Times the main loop's steps; a stretch of work over LOOP_SLOW_S prints one journal line
    naming each step's share (1 a second at most). Added 2026-11-02: owner still saw pauses that
    the late-frame meter (which times only a draw) never showed."""

    def __init__(self, t):
        self.reset(t)

    def reset(self, t):
        self.t, self.parts = t, []
        # (2026-11-03 soak: slow loops where SEVERAL steps were slow at once = the whole process
        # stalled. The line now says whether Python's full garbage collection ran, and how much CPU
        # the process itself used: much less CPU than time = it was waiting / not scheduled)
        self.gc2, self.cpu = gc.get_count()[2], sum(os.times()[:2])

    def __call__(self, name):
        now = time.time()
        self.parts.append((name, now - self.t))
        self.t = now

    def report(self):
        total = sum(d for n, d in self.parts)
        if total > LOOP_SLOW_S and time.time() - _slow_t[0] > 1.0:
            _slow_t[0] = time.time()
            cpu = (sum(os.times()[:2]) - self.cpu) * 1000
            print("slow loop %.0f ms: %s | cpu %.0f ms%s" % (
                total * 1000, ", ".join("%s %.0f" % (n, d * 1000) for n, d in self.parts if d > 0.002),
                cpu, ", full GC ran" if gc.get_count()[2] < self.gc2 else ""))
            sys.stdout.flush()
        self.parts = []


def run_commands(lines):
    """Apply the received command lines in order. Returns (pending, redraw): a new picture to
    show (or None) and whether only an overlay changed. Pulled out of main() 2026-11-03 so the
    tests can throw bad / unknown commands at the real parser."""
    pending, redraw = None, False
    for line in lines:
        cmd = line.split()
        if not cmd:
            continue
        try:
            if cmd[0] == "q":
                sys.exit(0)
            if cmd[0] == "p":
                if cmd[1] == "next":                 # p next [F]: F stays at cmd[2]
                    # not the clip already on screen (it restarted from frame 0, mostly with
                    # favourites), unless it is the only one there is
                    n = pick_next(exclude=set(x for x in (play["n"],) if x is not None), main=True)
                    if n is None:
                        n = pick_next(main=True)
                elif cmd[1] == "cue":                # p cue N [F] -> F moved to cmd[2]
                    n = cue(int(cmd[2]))
                    if n is None:
                        n = pick_next()
                    cmd = cmd[:2] + cmd[3:]
                else:
                    n = int(cmd[1])
                if n in CLIPS:
                    mark_shown(n)
                    start_xf(time.time())            # (only with a CROSSFADE time set)
                    SCENE["name"] = None                 # a clip takes over from a scene
                    # keep a flash that is already running: a kick ON the downbeat may land first
                    play.update(n=n, t0=time.time(), f0=int(cmd[2]) if len(cmd) > 2 else 0, last=-1)
                    pending = None
                continue
            if gfx_command(cmd) or hit_command(cmd) or mask_command(cmd) or frame_command(cmd):
                redraw = True
                continue
            if scene_command(cmd):
                if SCENE["name"]:
                    pending = pattern(["c", "000000"])   # the GPU draws the scene over this
                else:
                    redraw = True
                continue
            if cmd[0] == "l":
                lines_command(cmd)
                redraw = True
                continue
            if cmd[0] == "a":
                start_fade(int(cmd[1]), float(cmd[2]) if len(cmd) > 2 else 0.0, time.time())
                continue
            if cmd[0] == "t":
                every = int(cmd[1])
                look = None if every < 2 else (every, max(1, min(every - 1, int(cmd[2]))),
                                               max(0, min(255, int(cmd[3]))) if len(cmd) > 3 else 255)
                if not set_layer(look):          # no second layer: the half-row lines, about as thick
                    FINE[0] = (max(2, int(round(look[0] / 3.0))), max(1, int(round(look[1] / 3.0))), "000000") if look else None
                    redraw = True
                continue
            if cmd[0] == "n":
                every = int(cmd[1])
                if every < 2:
                    FINE[0] = None
                else:
                    FINE[0] = (every, max(1, min(every - 1, int(cmd[2]))), line_fill(cmd[3] if len(cmd) > 3 else "000000"))
                redraw = True
                continue
            if cmd[0] == "y":
                BLUR[0] = max(0.0, min(4.0, float(cmd[1])))   # fractions: the GPU fades smoothly
                redraw = True
                continue
            if cmd[0] == "x":
                n = int(cmd[1]) if cmd[1] != "next" else pick_next(busy())
                if n is None:
                    n = -1
                amt = float(cmd[2]) if len(cmd) > 2 else 0.5
                if n < 0 or amt <= 0 or n not in CLIPS:     # (a slot not in RAM: weave_frame draws nothing,
                    WEAVE["n"] = None                       #  but fx_active kept the box redrawing)
                else:
                    if WEAVE["n"] != n:
                        WEAVE["t0"] = time.time()
                        mark_shown(n)                # (never counted: x next kept picking the same clip)
                    WEAVE["n"], WEAVE["amt"] = n, min(1.0, amt)
                redraw = True
                continue
            if cmd[0] == "g2":                       # g2 N: the scenes' pool (0 = the clips')
                POOLS["n2"] = max(0, int(cmd[1]))
                continue
            if cmd[0] == "Q":                        # Q ID: play this clip next (Q - = forget)
                cid = cmd[1] if len(cmd) > 1 else "-"
                QUEUE["id"] = cid if any(e["id"] == cid for e in LIB["entries"]) else None
                LOADER["priority"] = QUEUE["id"] or LOADER["priority"]
                continue
            if cmd[0] == "g":                        # g N: play pool N only (0 = everything)
                n = max(0, int(cmd[1]))
                if n != POOLS["n"]:
                    POOLS["n"] = n
                    print("pool %d (%s)" % (n, POOLS["list"][n - 1]["name"] if 1 <= n <= len(POOLS["list"]) else "everything"))
                    sys.stdout.flush()
                continue
            if cmd[0] == "z":
                FOCUS[0] = max(0.0, min(1.0, float(cmd[1])))
                redraw = True
                continue
            if cmd[0] == "w":
                amp = int(cmd[1])
                FX["w"] = (amp, float(cmd[2]), float(cmd[3])) if amp > 0 else None
                redraw = True
                continue
            if cmd[0] not in PATTERN_OPS:
                # anything else is IGNORED (2026-11-03 review): it used to fall through to the
                # pattern code, which stopped the clip / scene BEFORE failing on it, so one
                # unknown command (a newer conductor, the dead b m f r, load / drop) blanked the show
                unknown_command(line)
                continue
            f = pattern(cmd)                             # build it FIRST: a bad one changes nothing
            SCENE["name"] = None                         # a pattern takes over from a scene
            if play["n"] is not None:                    # a pattern takes over from a clip:
                start_xf(time.time())                    # the clip dissolves out (CROSSFADE)
            play["n"] = None
            pending = f
        except SystemExit:
            raise
        except Exception as e:
            print("bad command %r: %s" % (line, e))
            sys.stdout.flush()
    return pending, redraw


def main():
    helper()                         # the copy helper: forked now, while this process is still small
    sync_hdmi()                      # a screen already plugged in: right mode before the screen setup
    if not init_gpu():
        init_fb()
        init_layer()
    FADE.update(cur=255.0, frm=255.0, to=255.0, applied=None)
    set_alpha(255)                   # always start fully visible (a player restart in the middle of a fade)
    load_clips()
    scan_library(force=True)
    adopt_slots()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    show(frame(["c", "000000"]))
    allow, allow_t = [read_pi_addrs()], [time.time()]
    print("orcflash: %dx%d -> %dx%d, listening on udp %d (from %s only), %d clips" % (
        W, H, OUT_W, OUT_H, PORT, " ".join(sorted(allow[0])), len(CLIPS)))
    sys.stdout.flush()
    # effects animate at ~30 fps: on the framebuffer timed from the last draw's start; with the GPU
    # PACE_S after the last swap (a refresh), so every frame lands exactly 2 refreshes after the last
    FX_DT = PACE_S if GPU[0] is not None else 1 / 30.0
    since = (lambda: time.time() - shown_end[0]) if GPU[0] is not None else (lambda: time.time() - shown_at[0])
    hdmi_t = [time.time()]
    while True:
        try:
            now = time.time()
            lap = Laps(now)
            if now - allow_t[0] > 2.0:            # the Pi's Wi-Fi address can change (DHCP)
                allow_t[0], allow[0] = now, read_pi_addrs()
            lap("addrs")
            if now - hdmi_t[0] > 2.0:             # a screen plugged in later (or unplugged and back)
                hdmi_t[0] = now
                if sync_hdmi():
                    print("orcflash: HDMI mode switched to %dx%d, restarting to re-apply the screen setup" % (OUT_W, OUT_H))
                    sys.stdout.flush()
                    sys.exit(0)              # systemd restarts it in 2 s; the RAM slots survive
            lap("hdmi")
            meter_report(now)
            loader_tick(now)
            lap("loader")
            write_status(now)
            lap("status")
            wait = clip_tick(time.time())
            lap("clip")
            if fx_active() and base[0] is not None:
                late = since()
                if late >= FX_DT:
                    show(base[0], base[1])                # same picture, the effect moved on
                    late = 0.0
                wait = FX_DT - late if wait is None else min(wait, FX_DT - late)
            wait = 0.5 if wait is None else min(wait, 0.5)        # the loader looks in twice a second
            if fade_tick(time.time()):
                wait = min(wait, 0.02)                            # a fade steps ~50 times a second
            lap("fx")
            lap.report()
            if not select.select([sock], [], [], wait)[0]:
                continue
            lap.reset(time.time())                                # (the wait itself is not work)
            # take EVERY queued command, in order, then draw only the end result once: a burst
            # (a downbeat's clip change + its kick flash) loses nothing and never runs behind
            lines = []
            sock.setblocking(False)
            try:
                while True:
                    d, a = sock.recvfrom(4096)
                    if a[0] in allow[0]:
                        lines.extend(d.decode("ascii", "ignore").splitlines())
            except socket.error:
                pass
            pending, redraw = run_commands(lines)
            lap("cmds %d" % len(lines))
            if pending is not None:
                show(pending)
            elif redraw and base[0] is not None:
                show(base[0], base[1])                    # only an overlay changed: same picture, new overlay
            lap("draw")
            lap.report()
        except Exception:
            # nothing may stop the drawing (2026-11-03 review): an error here used to kill the
            # player, which came back black with every setting lost until the Pi re-sent them
            loop_error()


if __name__ == "__main__":
    main()
