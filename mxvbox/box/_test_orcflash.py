# -*- coding: utf-8 -*-
"""_test_orcflash.py -- the MXV box player's drawing (Python 2.7, runs ON THE BOX).

    python /storage/orc/_test_orcflash.py

Imports orcflash WITHOUT opening the screen (init_fb only runs in main()) and checks
every drawing operation pixel by pixel on fake frames: patterns, overlays (colour /
scanlines, row mixing, weave, wobble, and what the no-GPU fallback does with the blur
looks (every blur is the GPU's: _test_gpu.py; the CPU borders / bands / colour flash went 2026-11-02).
"""
from __future__ import print_function
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orcflash as F                      # noqa: E402

EXPECTED = 94
passed = [0]
failed = [0]


def check(cond, label, extra=""):
    if cond:
        passed[0] += 1
    else:
        failed[0] += 1
        print("  FAIL:", label, extra)


W, H, LINE = F.W, F.H, F.LINE


def at(data, x, y):
    return data[y * LINE + x * 4:y * LINE + x * 4 + 4]


def row(data, y):
    return data[y * LINE:(y + 1) * LINE]


RED, BLUE, GRN, BLK = F.px("ff0000"), F.px("0000ff"), F.px("00ff00"), F.px("000000")


def gradient():
    """A frame where every pixel is unique-ish: B = x, G = y, R = x+y."""
    out = []
    for y in range(H):
        out.append(b"".join(chr(x & 255) + chr(y & 255) + chr((x + y) & 255) + "\xff" for x in range(W)))
    return b"".join(out)


G = gradient()


def reset():
    F.FX["w"] = None
    F.FOCUS[0] = 0.0
    F.BLUR[0] = 0
    F.LINES[0] = None
    F.FINE[0] = None
    F.WEAVE.update(n=None, amt=0.0)


# ---- import-safe --------------------------------------------------------------------
check(F.fb is None and F.fd is None, "importing opens no framebuffer (init_fb runs in main())")
check(F.LINE == W * 4 and F.PAD == b"", "the default layout is plain 320x180x4")

# ---- patterns ---------------------------------------------------------------------------
c = F.frame(["c", "ff0000"])
check(len(c) == LINE * H and at(c, 0, 0) == RED and at(c, W - 1, H - 1) == RED, "c = a solid colour")
v = F.frame(["v", "2", "ff0000", "0000ff"])
check(at(v, 0, 5) == RED and at(v, W - 1, 5) == BLUE and at(v, W // 2 - 1, 0) == RED and at(v, W // 2, 0) == BLUE,
      "v 2 = two vertical stripes split exactly in the middle")
h = F.frame(["h", "3", "ff0000", "0000ff", "00ff00"])
check(at(h, 5, 0) == RED and at(h, 5, H // 2) == BLUE and at(h, 5, H - 1) == GRN, "h 3 = three horizontal bands")
k = F.frame(["k", "8", "ff0000", "0000ff"])
check(at(k, 0, 0) == RED and at(k, 40, 0) == BLUE and at(k, 0, 40) == BLUE and at(k, 40, 40) == RED,
      "k 8 = a checkerboard of 40 px cells")
s = F.frame(["s", "0.25", "ff0000", "0000ff"])
check(at(s, 79, 9) == RED and at(s, 80, 9) == BLUE, "s 0.25 = the left quarter in the first colour")

# ---- the CPU only composes the weave / bars / lines now ---------------------------------
reset()
check(F.compose(G) == G, "nothing on: the composed picture is the picture")
check(not hasattr(F, "OV") and not hasattr(F, "flip_px"), "the CPU borders / bands are gone (2026-11-02)")
check(not hasattr(F, "spaced") and not hasattr(F, "bar_rows") and "r" not in F.FX,
      "the CPU's coloured l lines and raster bars are gone too (2026-11-02, with the compressed clips)")
F.lines_command(["l", "4", "2", "blur"])
blur_l = F.LINES[0]
F.lines_command(["l", "2", "1", "000000"])
check(blur_l == (4, 2, "blur") and F.LINES[0] is None, "l with blur: blurred lines; l with a colour: lines OFF", (blur_l, F.LINES[0]))
F.LINES[0] = None

# ---- row mixing, blur looks without the GPU ----------------------------------------------------
reset()
other = F.frame(["c", "00ff00"])
half = F.mix_rows(G, other, 0.5)
n_other = sum(1 for y in range(H) if row(half, y) == row(other, y))
check(n_other == H // 2, "mix 0.5 = exactly half the rows from the other picture", n_other)
check(all((row(half, y) == row(other, y)) == (y % 2 == 0) for y in range(H)),
      "...spread down the screen, not bunched: at 0.5 it is exactly every other row")
third = sum(1 for y in range(H) if row(F.mix_rows(G, other, 0.25), y) == row(other, y))
check(40 <= third <= 50, "mix 0.25 takes about a quarter", third)
reset()
F.GPU[0] = None
F.BLUR[0] = 4
check(F.compose(G) == G, "no GPU: a blur draws nothing at all (the mirror-border stand-in went with the CPU borders)")
reset()
F.FOCUS[0] = 1.0
F.LINES[0] = (3, 1, "blur")
check(F.compose(G) == G, "the blur looks (z, blurred lines) leave the composed picture alone: the GPU blurs them")

# ---- weave + wobble ---------------------------------------------------------------------
reset()
F.CLIPS.clear()
F.CLIPS[0] = F.frame(["c", "ff0000"]) * 3             # a fake 3-frame red clip in slot 0
F.WEAVE.update(n=0, amt=0.5, t0=0.0)
wv = F.compose(G)
check(sum(1 for y in range(H) if row(wv, y) == row(c, y)) == H // 2, "x 0 0.5 weaves clip 0 into every other line")
F.LINES[0] = (2, 1, "blur")
wl = F.compose(G)
check(all(row(wl, y) == row(G, y) for y in range(0, H, 2)) and
      sum(1 for y in range(1, H, 2) if row(wl, y) == row(c, y)) == H // 4,
      "weave + lines: the line rows stay the picture's and the woven clip takes half the VISIBLE rows (it used to hide under the lines)")
reset()
F.FX["w"] = (10, 1.0, 0.0)
w = F.compose(G)
pixels = lambda r: sorted(r[i:i + 4] for i in range(0, W * 4, 4))
check(row(w, 0) == row(G, 0) and row(w, H // 4) != row(G, H // 4) and pixels(row(w, H // 4)) == pixels(row(G, H // 4)),
      "wobble slides rows sideways (row 0 still, a quarter down moved) without losing pixels")

# ---- who the box obeys: the cable Pi + the Wi-Fi address its conductor wrote --------
import shutil, tempfile                                # noqa: E402
pa = os.path.join(tempfile.mkdtemp(prefix="orcflash_pa_"), "pi_addrs")
check(F.read_pi_addrs(pa) == set(["10.55.0.1"]), "no pi_addrs file: only the cable Pi is obeyed")
with open(pa, "w") as f:
    f.write("192.168.1.20 junk 300.1.2.3 1.2.3 ;rm\n")
check(F.read_pi_addrs(pa) == set(["10.55.0.1", "192.168.1.20"]),
      "pi_addrs adds the Pi's Wi-Fi address and ignores anything that isn't an IPv4", sorted(F.read_pi_addrs(pa)))
shutil.rmtree(os.path.dirname(pa))

# ---- RAM slots (rolling library): load / drop, in a temp dir, never /dev/shm/orc ------
F.CLIPS.clear()
tmp = tempfile.mkdtemp(prefix="orcflash_test_")
F.SLOT_DIR = tmp
with open(os.path.join(tmp, "s3.raw"), "wb") as f:
    f.write(F.frame(["c", "0000ff"]) * 2)
check(F.load_slot(3) and 3 in F.CLIPS and len(F.CLIPS[3]) == 2 * F.PAGE, "load S opens a slot's frames")
check(not F.load_slot(5) and 5 not in F.CLIPS, "load of an empty slot holds nothing (no s5.raw)")
F.play.update(n=3, t0=0.0, f0=0, last=-1)
F.WEAVE.update(n=3, amt=0.5)
F.drop_slot(3)
check(3 not in F.CLIPS and F.play["n"] is None and F.WEAVE["n"] is None,
      "drop S lets go of the slot and stops playing / weaving it")
check(F.clip_tick(1.0) is None, "a dropped slot cannot be drawn (clip_tick stops quietly)")
F.CLIPS.clear()
F.load_clips()
check(sorted(F.CLIPS) == [3], "at start, every slot already in the folder is opened")
F.drop_slot(3)
shutil.rmtree(tmp)

# ---- the box's own library: index, self-loading slots, p next / cue, pools ------
import time as _time                                   # noqa: E402
lib = tempfile.mkdtemp(prefix="orcflash_lib_")
F.LIB_DIR, F.SLOT_DIR = lib, tempfile.mkdtemp(prefix="orcflash_slots_")
F.LIB.update(entries=[], mt=None, scan_t=0.0)
F.SLOTINFO.clear()
F.CLIPS.clear()
F.LOADER.update(p=None, slot=None, entry=None, pointer=0, priority=None, t=0.0)
for cid in ("aa", "bb"):
    os.makedirs(os.path.join(lib, cid))
    with open(os.path.join(lib, cid, "clip.raw"), "wb") as f:
        f.write(F.frame(["c", "ff0000" if cid == "aa" else "0000ff"]) * 2)
    with open(os.path.join(lib, cid, "meta.json"), "w") as f:   # an OLD meta: bytes counted blur copies
        f.write('{"name": "%s.mp4", "frames": 2, "blur": true, "bytes": %d}' % (cid, 4 * F.PAGE))
F.scan_library(force=True)
idx = open(os.path.join(lib, "index.json")).read()
check([e["id"] for e in F.LIB["entries"]] == ["aa", "bb"] and '"aa"' in idx and '"bb"' in idx,
      "the box indexes its own library (lib/index.json)")
_time.sleep(0.05)                       # (this kernel moves a folder's time on in jiffies)
os.makedirs(os.path.join(lib, "zz"))
with open(os.path.join(lib, "zz", "clip.raw"), "wb") as f:
    f.write(F.frame(["c", "00ff00"]))
with open(os.path.join(lib, "zz", "meta.json"), "w") as f:
    f.write('{"name": "zz.mp4", "frames": 1}')
F.scan_library()                        # re-reads, and WRITES a new index.json
e1 = F.LIB["entries"]
seen_new = "zz" in [e["id"] for e in e1]
_time.sleep(0.05)
F.scan_library()
F.scan_library()
still = F.LIB["entries"] is e1          # its own index write must not count as a change
shutil.rmtree(os.path.join(lib, "zz"))
_time.sleep(0.05)
F.scan_library()
check(still and seen_new and "zz" not in [e["id"] for e in F.LIB["entries"]],
      "nothing changed: no re-read (writing index.json used to make every 5 s check re-read the whole "
      "library on the drawing thread, 80-180 ms); a clip added or removed: re-read", (still, seen_new))
t_end = _time.time() + 20
while len(F.SLOTINFO) < 2 and _time.time() < t_end:   # the loader copies card -> RAM itself
    F.LOADER["t"] = 0.0
    F.loader_tick(_time.time())
    _time.sleep(0.05)
check(sorted(i["id"] for i in F.SLOTINFO.values()) == ["aa", "bb"] and len(F.CLIPS) == 2,
      "the loader fills RAM slots from the card by itself (a separate low-priority copy)", F.SLOTINFO)
aa_s = [s for s, i in F.SLOTINFO.items() if i["id"] == "aa"][0]
bb_s = [s for s, i in F.SLOTINFO.items() if i["id"] == "bb"][0]
check(all(e["bytes"] == 2 * F.PAGE for e in F.LIB["entries"]) and F.SLOTINFO[aa_s]["bytes"] == 2 * F.PAGE
      and not any(k.endswith("_y4.raw") for k in os.listdir(F.SLOT_DIR)),
      "a clip's size = its clip.raw (an old meta's blur bytes are ignored), and only clip.raw is copied to RAM")
first = F.pick_next()
F.mark_shown(first)
check(F.pick_next() != first and F.SLOTINFO[first]["shown"] == 1, "p next: the least shown loaded clip, then the other")
check(F.cue(1) == bb_s and F.cue(3) == bb_s, "cue N = library clip N (mod the library)")
with open(os.path.join(lib, "pools.json"), "w") as f:
    f.write('[{"name": "reds", "ids": ["aa", "gone"]}, {"name": "empty", "ids": ["gone"]}]')
F.scan_library(force=True)
F.POOLS["n"] = 1
check(F.pool_ids() == set(["aa"]) and [F.pick_next() for _ in range(3)] == [aa_s] * 3,
      "pool 1 (from lib/pools.json): only its clips that exist, and p next stays inside it")
F.POOLS["n"] = 2
check(F.pool_ids() is None, "a pool whose clips are all gone = everything")
F.POOLS["n"] = 7
check(F.pool_ids() is None, "a pool number that doesn't exist = everything")
F.POOLS["n"] = 0
# the SCENE POOL (g2 N): scenes / overlay / mask pick from their own pool, the loader keeps both
F.POOLS["n"], F.POOLS["n2"] = 0, 1
check(F.scene_pool() == set(["aa"]) and F.pool_ids() is None and F.loader_pool() is None,
      "SCENE POOL 1 + clips' pool everything: scenes pick from reds, the loader keeps everything")
F.POOLS["n"] = 1
check(F.loader_pool() == set(["aa"]), "...both pools picked: the loader keeps what either needs")
F.POOLS["n"], F.POOLS["n2"] = 0, 0
check(F.scene_pool() is None, "SCENE POOL 0 = the clips' own pool")
# FAVOURITES / NEVER from lib/flags.json, PLAY NEXT (Q)
with open(os.path.join(lib, "flags.json"), "w") as f:
    f.write('{"fav": ["bb"], "never": ["aa"]}')
F.scan_library(force=True)
check(F.FLAGS == {"fav": set(["bb"]), "never": set(["aa"])} and all(F.pick_next() == bb_s for _ in range(3)),
      "lib/flags.json read with the library; a NEVER clip is not shown (p next keeps to bb)")
F.QUEUE["id"] = "aa"
q1 = F.pick_next(main=True)
check(q1 == aa_s and F.QUEUE["id"] is None and F.pick_next(main=True) == bb_s,
      "PLAY NEXT (Q aa): the clip bars' next pick is aa, once, even a NEVER clip (you asked for it)")
os.remove(os.path.join(lib, "flags.json"))
F.scan_library(force=True)
check(F.FLAGS == {"fav": set(), "never": set()}, "no flags file: no favourites, nothing barred")
# PLAYLISTS: a pool with "order" plays in YOUR order, looping
with open(os.path.join(lib, "pools.json"), "w") as f:
    f.write('[{"name": "reds", "ids": ["aa", "gone"]}, {"name": "set one", "ids": ["bb", "gone", "aa"], "order": true}]')
F.scan_library(force=True)
F.POOLS["n"] = 2
played = [F.SLOTINFO[F.pick_next(main=True)]["id"] for _ in range(5)]
check(played == ["bb", "aa", "bb", "aa", "bb"],
      "POOL 2 = a playlist: bb, aa, bb, aa ... in its order (the missing clip passed over)", played)
F.POOLS["n"] = 1
r1 = F.pick_next(main=True)
F.POOLS["n"] = 2
check(F.playlist() == ["bb", "aa"] and F.PLAYLIST["pos"] >= 0 and F.SLOTINFO[F.pick_next(main=True)]["id"] in ("bb", "aa"),
      "a plain pool picked between: the playlist keeps its place, clips gone from the library are left out")
F.POOLS["n"] = 1
F.pick_next(main=True)
check(F.playlist() is None, "POOL on a plain pool: not a playlist (random, as before)")
F.POOLS["n"] = 0
# the loader is PACED: a rolling swap at most every ROLL_GAP_S (the snags came from a 40 MB copy every ~2 s)
real_choose, evicted = F.SP.choose_slot, []
F.SP.choose_slot = lambda *a, **k: ("evict", aa_s)
real_drop = F.drop_slot
F.drop_slot = lambda s: evicted.append(s)
F.LIB["entries"].append({"id": "cc", "bytes": 2 * F.PAGE, "frames": 2, "name": "cc.mp4"})   # wanted, not in RAM
saved = dict(F.SLOTINFO)
F.LOADER.update(p=None, roll_t=0.0, t=0.0)
F.LIB["scan_t"] = _time.time()
now0 = 1000.0
F.loader_tick(now0)
F.SLOTINFO.update(saved)
F.LOADER["t"] = 0.0
F.loader_tick(now0 + 1.0)                              # 1 s later: too soon for another
F.SLOTINFO.update(saved)
F.LOADER["t"] = 0.0
F.loader_tick(now0 + F.ROLL_GAP_S + 0.1)               # after the gap: the next one goes
check(evicted == [aa_s, aa_s] and F.SLOT_MIN_S >= 60,
      "the loader swaps a clip at most every ROLL_GAP_S, and a clip stays at least a minute", evicted)
del evicted[:]
F.SLOTINFO.update(saved)
F.POOLS["list"], F.POOLS["n"] = [{"name": "p", "ids": ["cc"]}], 1      # a pool without aa: aa is OUTSIDE it
F.LOADER.update(t=0.0, roll_t=now0 + F.ROLL_GAP_S + 0.1)
F.loader_tick(now0 + F.ROLL_GAP_S + 0.5)
check(evicted == [aa_s], "...but a clip outside the picked pool goes at once (a pool still fills RAM in seconds)", evicted)
F.SP.choose_slot, F.drop_slot = real_choose, real_drop
F.POOLS["list"], F.POOLS["n"] = [], 0
F.LIB["entries"].pop()
names = os.listdir(F.SLOT_DIR)
check("s%d.raw" % aa_s not in names and any(n.startswith("trash_") and n.endswith("s%d.raw" % aa_s) for n in names),
      "an evicted clip's files are RENAMED aside at once, not deleted on the drawing thread", sorted(names))
# the copy helper: one small shell, started once, runs each copy (and deletes the trash first)
j1 = F.HelperJob("exit 3")
pid1 = F._helper[0].pid
t_end = _time.time() + 5
while j1.poll() is None and _time.time() < t_end:
    _time.sleep(0.02)
F.SLOTINFO.pop(aa_s, None)
F.LOADER.update(p=None, t=0.0)
t_end = _time.time() + 20
kinds = set()
while not any(i["id"] == "aa" for i in F.SLOTINFO.values()) and _time.time() < t_end:   # aa is wanted again
    F.LOADER["t"] = 0.0
    F.loader_tick(_time.time())
    kinds.add(type(F.LOADER["p"]).__name__)
    _time.sleep(0.05)
check(j1.returncode == 3 and kinds <= set(["NoneType", "HelperJob"]) and "HelperJob" in kinds and F._helper[0].pid == pid1
      and any(i["id"] == "aa" for i in F.SLOTINFO.values()) and not any(n.startswith("trash_") for n in os.listdir(F.SLOT_DIR)),
      "copies run in the ONE helper (no fork of the big player per copy), report their exit code, and the "
      "next copy clears the trash", (j1.returncode, sorted(os.listdir(F.SLOT_DIR))))
reset()
shutil.rmtree(lib)
shutil.rmtree(F.SLOT_DIR)

# --- the screen page is VK times taller; thin lines are drawn in the extra rows ---
reset()
VK = F.VK
page = F.expand(G)
check(VK == 2 and len(page) == F.PAGE * VK and F.PAGEO == F.PAGE * VK and
      all(row(page, VK * y + k) == row(G, y) for y in (0, 17, H - 1) for k in range(VK)),
      "expand: every picture row is written VK times (the screen page is VK x taller)")
F.FINE[0] = (2, 1, "000000")
page = F.expand(G)
check(all(row(page, r) == (BLK * W if r % 2 == 0 else row(G, r // VK)) for r in range(0, 2 * H, 7)),
      "n 2 1 = thin scanlines: every other OUTPUT row black, half a picture row thick")
F.FINE[0] = (3, 1, "00ff00")
page = F.expand(G)
check(all(row(page, r) == (GRN * W if r % 3 == 0 else row(G, r // VK)) for r in range(2 * H)),
      "n 3 1 00ff00 = a coloured thin line every third output row")
F.FINE[0] = (4, 3, "000000")
check(F.line_rows() == frozenset(y for y in range(H) if y % 2 == 0),
      "the weave skips only the picture rows a thin look paints COMPLETELY (4/3: even rows, not odd)")
F.FINE[0] = (2, 1, "000000")
check(F.line_rows() == frozenset(), "thin scanlines (n 2 1) cover no picture row completely")
F.FINE[0] = (2, 1, "blur")
check(F.expand(G) == b"".join(row(G, y) * VK for y in range(H)),
      "no GPU, blur fill: no lines (only the GPU blurs), still the full-height page")
reset()

# --- the black scanlines on the 2nd screen layer (real pixels, a LAYER_W-wide strip) ---
SL = 64


def srow(s, y):
    return s[y * SL:(y + 1) * SL]


def alpha_of(s, y):
    return ord(srow(s, y)[3:4])


s1080 = F.strip_bytes((6, 2, 255), 1080, SL)
check(len(s1080) == SL * 1080 and [alpha_of(s1080, y) for y in range(8)] == [255, 255, 0, 0, 0, 0, 255, 255]
      and srow(s1080, 0)[:4] == b"\0\0\0\xff" and srow(s1080, 2) == b"\0" * SL,
      "t 6 2: 2 black real pixels every 6 rows on a 1080 strip, the rest see-through")
s720 = F.strip_bytes((6, 3, 255), 720, SL)
check([alpha_of(s720, y) for y in range(5)] == [255, 255, 0, 0, 255],
      "on a 720 screen the 1080p look scales by 2/3 (6/3 -> 4/2)")
check(F.strip_bytes(None, 1080, SL) == b"\0" * SL * 1080, "no look = a fully see-through strip")
sc = F.strip_bytes((4, 9, 255), 1080, SL)
check([alpha_of(sc, y) for y in range(4)] == [255, 255, 255, 0], "thick is clamped below every (4/9 -> 4/3)")
sa = F.strip_bytes((4, 1, 128), 1080, SL)
check(alpha_of(sa, 0) == 128 and alpha_of(sa, 1) == 0, "ALPHA sets how dark the line is (128 = half)")

# --- fade to black / brightness (the chip's whole-screen opacity) ---
import time
check([F.fade_alpha(255, 0, 10.0, 2.0, t) for t in (10.0, 11.0, 12.0, 13.0)] == [255.0, 127.5, 0.0, 0.0],
      "a 2 s fade from full to black: full at the start, half at 1 s, black at 2 s, and it stays there")
check(F.fade_alpha(40, 200, 5.0, 0.0, 5.0) == 200.0, "a 0 s fade goes straight to the target")
F.FADE.update(cur=100.0, frm=100.0, to=100.0, t0=0.0, dur=0.0)
F.start_fade(0, 1.0, 5.0)
check(F.FADE["frm"] == 100.0 and F.FADE["to"] == 0.0 and F.FADE["t0"] == 5.0,
      "a new fade starts from where the brightness is NOW (no jump when one is already running)")
F.start_fade(999, 1.0, 5.0)
check(F.FADE["to"] == 255.0, "a target above 255 is clamped")
F.FADE.update(cur=255.0, frm=255.0, to=255.0, t0=0.0, dur=0.0)
F.start_fade(0, 1.0, time.time() - 0.5)
running = F.fade_tick(time.time())
mid = F.FADE["cur"]
F.FADE["t0"] = time.time() - 5.0
done = not F.fade_tick(time.time())
check(running and 0 < mid < 255 and done and F.FADE["cur"] == 0.0,
      "fade_tick: mid-fade it reports running with a brightness between, at the end it is exactly the target")
F.FADE.update(cur=255.0, frm=255.0, to=255.0, t0=0.0, dur=0.0, applied=None)

# --- HDMI: a screen plugged in after boot must switch the output off the analog default ---
import tempfile
hd = tempfile.mkdtemp()
F.HDMI_HPD, F.DISPLAY_MODE = os.path.join(hd, "hpd"), os.path.join(hd, "mode")
F.HDMI_MODE_FILE = os.path.join(hd, "hdmi_mode")
F.time.sleep = lambda s: None


def hdmi(hpd, mode, want=None):
    for p, val in ((F.HDMI_HPD, hpd), (F.DISPLAY_MODE, mode)):
        with open(p, "w") as f:
            f.write(val + "\n")
    if want is not None:
        with open(F.HDMI_MODE_FILE, "w") as f:
            f.write(want + "\n")
    F.OUT_W, F.OUT_H = 1280, 720
    changed = F.sync_hdmi()
    return changed, open(F.DISPLAY_MODE).read().strip(), (F.OUT_W, F.OUT_H)


check(hdmi("0", "576cvbs", "720p") == (False, "576cvbs", (1280, 720)),
      "no screen on HDMI: the video mode is left alone")
check(hdmi("1", "576cvbs") == (True, "720p", (1280, 720)),
      "a screen plugged in on the analog default, no mode file: switched to 720p")
check(hdmi("1", "720p", "1080p") == (True, "1080p", (1920, 1080)),
      "mode file says 1080p: switched, and the output size follows")
check(hdmi("1", "1080p", "1080p") == (False, "1080p", (1920, 1080)),
      "already in the wanted mode: no switch, no restart; output size still follows the mode")
check(hdmi("1", "720p", "garbage") == (False, "720p", (1280, 720)),
      "an unknown word in the mode file means 720p")
shutil.rmtree(hd)

# ── the command parser takes bad input without harm (2026-11-03 review) ──────────────────────
# Before: anything unknown fell through to the pattern code, which stopped the clip / scene
# BEFORE failing; a bad `n` colour was stored and crashed the player at its next draw.
def playing(n=7):
    F.play.update(n=n, t0=0.0, f0=0, last=-1)
    F.SCENE["name"] = None


for bad in ("bogus 1 2", "b 3", "load 2", "drop 2", "h blur 0.5", "h zzz 1 100", "c", "v x ff0000",
            "s nope ff0000 00ff00", "c zzzzzz"):
    playing()
    pend, _r = F.run_commands([bad])
    check(F.play["n"] == 7 and pend is None, "a bad / unknown command leaves the clip playing: %r" % bad,
          F.play["n"])
F._unknown["t"] = 0.0
F.run_commands(["bogus 1 2"])
check(F._unknown["t"] > 0, "an unknown op is ignored as unknown (never reaches the pattern code)")
playing()
scene = sorted(F.GPUMOD.SCENES)[0]
F.run_commands(["S %s fast 0.5" % scene])
check(F.play["n"] == 7 and F.SCENE["name"] is None, "S with a bad number leaves the clip playing")
playing()
F.FINE[0] = None
F.run_commands(["n 2 1 fff", "n 2 1 zzzzzz"])
check(F.FINE[0] is None, "a bad lines colour is refused when it arrives (not stored to crash the draw)", F.FINE[0])
F.run_commands(["n 4 1 #ff0000"])
check(F.FINE[0] == (4, 1, "ff0000"), "a good lines colour still works", F.FINE[0])
F.run_commands(["n 4 1 blur"])
check(F.FINE[0] == (4, 1, "blur"), "n ... blur still works", F.FINE[0])
F.FINE[0] = None
playing()
pend, _r = F.run_commands(["c ff0000"])
check(F.play["n"] is None and pend is not None and at(pend, 0, 0) == RED, "a good pattern still takes over from the clip")
F.HITS["mask"] = (1.0, 0.0, 0.5)
F.run_commands(["M 0"])
check("mask" not in F.HITS, "the mask off drops its hit (it kept the box redrawing forever)")
F.WEAVE["n"] = None
F.run_commands(["x 12345 0.5"])
check(F.WEAVE["n"] is None, "a weave of a slot not in RAM is off (it kept the box redrawing for nothing)")
F.cache.clear()
for i in range(F.CACHE_MAX * 3):
    F.pattern(["c", "%06x" % i])
check(len(F.cache) <= F.CACHE_MAX, "the pattern cache stays small", len(F.cache))
F.play["n"] = None

# ---- smaller review fixes (2026-11-03, later) ------------------------------------------------
F.XF.update(t0=1.0, src=("clip", 42, 0.0, 0))
check(42 in F.busy(), "the clip a crossfade fades out of is not evicted mid-fade")
F.XF.update(t0=None, src=None)
keep_si, keep_cl, keep_q = dict(F.SLOTINFO), dict(F.CLIPS), F.QUEUE["id"]
F.SLOTINFO.clear()
F.CLIPS.clear()
for s in (1, 2):
    F.SLOTINFO[s] = {"id": "c%d" % s, "bytes": 1, "ready": True, "shown": 0, "last": 0.0, "t": 0.0}
    F.CLIPS[s] = b"x"
F.QUEUE["id"] = None
F.FLAGS["fav"], F.FLAGS["never"] = set(), set()
F.POOLS["n"] = 0
F.play.update(n=1, t0=0.0, f0=0, last=-1)
F.SLOTINFO[1]["shown"], F.SLOTINFO[2]["shown"] = 0, 5     # the on-screen clip is the least shown
F.run_commands(["p next"])
check(F.play["n"] == 2, "p next never restarts the clip already on screen when another is there", F.play["n"])
del F.SLOTINFO[2], F.CLIPS[2]
F.run_commands(["p next"])
check(F.play["n"] == 1, "...but with only one clip, p next still plays it", F.play["n"])
F.SLOTINFO[2] = {"id": "c2", "bytes": 1, "ready": True, "shown": 0, "last": 0.0, "t": 0.0}
F.CLIPS[2] = b"x"
F.WEAVE["n"] = None
before = F.SLOTINFO[2]["shown"]
F.run_commands(["x 2 0.5"])
check(F.WEAVE["n"] == 2 and F.SLOTINFO[2]["shown"] == before + 1, "a weave clip counts as shown")
F.WEAVE["n"] = None
F.play["n"] = None
F.SLOTINFO.clear(); F.SLOTINFO.update(keep_si)
F.CLIPS.clear(); F.CLIPS.update(keep_cl)
F.QUEUE["id"] = keep_q

# ---- the status file + the slow-loop line (2026-11-03 soak) -----------------------------------
import json as _json                                    # noqa: E402
sd = tempfile.mkdtemp(prefix="orcflash_status_")
keep_status = F.STATUS
F.STATUS = os.path.join(sd, "status.json")
F.LOADER["status_t"] = 0.0
F.write_status(_time.time())
try:
    st = _json.load(open(F.STATUS))
except (IOError, ValueError):
    st = {}
check(all(k in st for k in ("t", "library", "slots", "playing", "pool", "queued")) and not os.path.exists(F.STATUS + ".tmp"),
      "the status file is whole JSON with its keys (written in one go now)", sorted(st))
F.STATUS = keep_status
shutil.rmtree(sd)
import io as _io                                        # noqa: E402
lap = F.Laps(_time.time())
lap.parts = [("status", 0.05), ("fx", 0.04)]
F._slow_t[0] = 0.0
buf, real_out = _io.BytesIO(), sys.stdout
sys.stdout = buf
try:
    lap.report()
finally:
    sys.stdout = real_out
out = buf.getvalue()
check(out.startswith("slow loop 90 ms: status 50, fx 40 | cpu "), "a slow loop names its steps and the process's own CPU time", out)

total = passed[0] + failed[0]
print("%d passed, %d failed  (expected %d)" % (passed[0], failed[0], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed[0] == 0 and total == EXPECTED else 1)
