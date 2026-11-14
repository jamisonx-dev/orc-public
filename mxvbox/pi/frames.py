"""Your FRAME pictures for the MXV box (2026-11-02): a picture of any shape whose WHITE parts let the
projector's light through and whose BLACK parts stay dark ("not have everything be a rectangle").

The web page (/api/mxvbox/frame/N) decodes an upload with daw/vis_patterns.picture_grey (big pictures,
SVG, the memory limits: all handled there), then fit() makes it the box screen's size, centred on
black, and store() keeps a PNG here on the Pi (the page's preview) and returns the raw grey bytes the
box reads (/storage/orc/frames/pN.l8, one byte a pixel, SW x SH, top row first).
"""
import os

SW, SH = 640, 360                       # the box GPU's surface (gpu.SW, gpu.SH)
SLOTS = 4
STORE = os.path.expanduser("~/.orc1_box_frames")
BOX_DIR = "/storage/orc/frames"


def fit(img):
    """A PIL grey ("L") image -> SW x SH, the whole picture shown (fit, not cropped), centred on black."""
    from PIL import Image
    w, h = img.size
    k = min(SW / float(w), SH / float(h))
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    resample = getattr(Image, "LANCZOS", getattr(Image, "BICUBIC", 3))
    out = Image.new("L", (SW, SH), 0)
    out.paste(img.convert("L").resize((nw, nh), resample), ((SW - nw) // 2, (SH - nh) // 2))
    return out


def preview_path(n, store=STORE):
    return os.path.join(store, "p%d.png" % n)


def store(n, img, store=STORE):
    """Keep slot n's preview PNG on the Pi; return the bytes for the box."""
    if not 1 <= n <= SLOTS:
        raise ValueError("frame picture slots are 1-%d" % SLOTS)
    os.makedirs(store, exist_ok=True)
    tmp = preview_path(n, store) + ".tmp"
    img.save(tmp, format="PNG")
    os.replace(tmp, preview_path(n, store))
    data = img.tobytes()
    assert len(data) == SW * SH
    return data


def box_cmd(n):
    """The shell command (run ON the box) that takes slot n's bytes on stdin."""
    return ("mkdir -p {d} && cat > {d}/p{n}.l8.tmp && mv {d}/p{n}.l8.tmp {d}/p{n}.l8").format(d=BOX_DIR, n=int(n))
