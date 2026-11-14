"""_test_make_clips.py -- the clip converter for real: ffmpeg + etcpak (run on the Pi, which has
both; the inbox uses this converter). Makes a 1 s test video, converts it both ways, checks the
sizes and that the compressed frames decode to the right colours.

    python3 _test_make_clips.py
"""
import os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_clips as MC                                   # noqa: E402

EXPECTED = 6
passed, failed = [0], [0]


def check(cond, label, extra=""):
    (passed if cond else failed)[0] += 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else "  " + str(extra)))


TABLES = ((2, 8), (5, 17), (9, 29), (13, 42), (18, 60), (24, 80), (33, 106), (47, 183))


def etc1_pixel(data, width, x, y):
    """One pixel of an ETC1 image, decoded here (etcpak's own decoder segfaults on the Pi,
    measured 2026-11-02). The ETC1 spec (OES_compressed_ETC1_RGB8_texture), both modes."""
    b = bytearray(data[((y // 4) * (width // 4) + x // 4) * 8:][:8])
    bx, by = x % 4, y % 4
    diff, flip = b[3] & 2, b[3] & 1
    second = (by >= 2) if flip else (bx >= 2)
    if diff:
        base = []
        for c in b[:3]:
            v, d = c >> 3, c & 7
            v = v + (d - 8 if d > 3 else d) if second else v
            base.append((v << 3) | (v >> 2))
    else:
        base = [((c & 15) if second else (c >> 4)) * 17 for c in b[:3]]
    table = TABLES[(b[3] >> 2) & 7] if second else TABLES[b[3] >> 5]
    k = bx * 4 + by
    msb = (((b[4] << 8) | b[5]) >> k) & 1
    lsb = (((b[6] << 8) | b[7]) >> k) & 1
    mod = (table[0], table[1], -table[0], -table[1])[(msb << 1) | lsb]
    return [max(0, min(255, c + mod)) for c in base]


tmp = tempfile.mkdtemp(prefix="mkclips_")
try:
    # 1 s, 25 fps: the left half red, the right half blue (a 16:9 picture, so the fill crops nothing)
    src = os.path.join(tmp, "test.mp4")
    subprocess.check_call(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=640x360:d=1:r=25",
                           "-f", "lavfi", "-i", "color=blue:s=640x360:d=1:r=25", "-filter_complex",
                           "[0][1]overlay=x=320", "-pix_fmt", "yuv420p", src])
    enc = MC.etc_encoder()
    check(enc is not None, "etcpak is installed here (the inbox needs it to make sharp clips)")
    f, size = MC.build_one(src, 12, os.path.join(tmp, "a"))
    etc = os.path.join(tmp, "a", "clip.etc")
    check(f == 15 and os.path.exists(etc) and size == os.path.getsize(etc) == 15 * MC.EW * MC.EH // 2
          and not os.path.exists(os.path.join(tmp, "a", "clip.raw")),
          "a video -> clip.etc: 15 fps, 640x360 at half a byte a pixel, nothing else", (f, size))
    first = open(etc, "rb").read()[:MC.EW * MC.EH // 2]
    px = lambda x, y: etc1_pixel(first, MC.EW, x, y)
    # decoded in the order of the bytes etcpak was handed: B, G, R (what the box GPU sees)
    l, r = px(100, 180), px(540, 180)
    check(l[2] > 200 and l[0] < 60 and r[0] > 200 and r[2] < 60,
          "the frames hold the picture (red left, blue right, in the box's B,G,R order)", (list(l), list(r)))
    import json
    meta = json.load(open(os.path.join(tmp, "a", "meta.json")))
    check(meta["frames"] == 15 and meta["bytes"] == size and meta["name"] == "test.mp4", "meta.json says what it is", meta)
    f2, size2 = MC.build_one(src, 12, os.path.join(tmp, "b"), encode=False)
    check(f2 == 15 and size2 == 15 * MC.W * MC.H * 4 and os.path.exists(os.path.join(tmp, "b", "clip.raw")),
          "encode=False: the old raw kind (320x180 BGRA)", (f2, size2))
    try:
        MC.build_one(os.path.join(tmp, "nothing.mp4"), 12, os.path.join(tmp, "c"))
        bad = False
    except SystemExit as e:
        bad = "ffmpeg could not read" in str(e)
    check(bad, "an unreadable video says so (the inbox moves it to failed/ with the reason)")
finally:
    shutil.rmtree(tmp)

total = passed[0] + failed[0]
print("%d passed, %d failed  (expected %d)" % (passed[0], failed[0], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed[0] == 0 and total == EXPECTED else 1)
