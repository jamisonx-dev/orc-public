"""_test_frames.py -- your FRAME pictures (frames.py): fitting to the box's screen, the preview,
the bytes for the box. python3 + Pillow (run on the Pi). Writes only into a temp folder."""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frames as FR                                     # noqa: E402
from PIL import Image                                   # noqa: E402

EXPECTED = 6
passed, failed = [0], [0]


def check(cond, label, extra=""):
    (passed if cond else failed)[0] += 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else "  " + str(extra)))


tmp = tempfile.mkdtemp(prefix="frames_test_")
try:
    sq = FR.fit(Image.new("L", (100, 100), 255))          # a white square picture
    check(sq.size == (FR.SW, FR.SH) and sq.mode == "L", "fit: the box screen's size, grey")
    px = sq.load()
    check(px[FR.SW // 2, FR.SH // 2] == 255 and px[10, FR.SH // 2] == 0 and px[FR.SW - 10, FR.SH // 2] == 0,
          "a square picture is shown WHOLE, centred, black either side (fit, not crop)")
    wide = FR.fit(Image.new("L", (4000, 500), 255)).load()
    check(wide[FR.SW // 2, 10] == 0 and wide[FR.SW // 2, FR.SH // 2] == 255, "a very wide one: black above and below")
    data = FR.store(2, sq, store=tmp)
    check(len(data) == FR.SW * FR.SH and os.path.exists(FR.preview_path(2, tmp)) and data[(FR.SH // 2) * FR.SW + FR.SW // 2:][:1] == b"\xff",
          "store: the preview PNG kept, one byte a pixel for the box, top row first")
    try:
        FR.store(5, sq, store=tmp)
        bad = False
    except ValueError:
        bad = True
    check(bad, "only slots 1-4")
    check(FR.box_cmd(3) == "mkdir -p /storage/orc/frames && cat > /storage/orc/frames/p3.l8.tmp && "
          "mv /storage/orc/frames/p3.l8.tmp /storage/orc/frames/p3.l8", "the box writes it whole, then renames (never half a file)")
finally:
    shutil.rmtree(tmp)

total = passed[0] + failed[0]
print("%d passed, %d failed  (expected %d)" % (passed[0], failed[0], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed[0] == 0 and total == EXPECTED else 1)
