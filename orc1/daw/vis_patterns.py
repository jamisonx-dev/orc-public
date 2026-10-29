"""vis_patterns.py -- the VISUALS background's pattern sets (2026-09-30).

Three sources of repeating tiles:
  * "built-in": drawn by orc_visuals.make_pattern (no files)
  * "hero":     patterns/hero/*.svg     (Hero Patterns, CC BY 4.0)
  * "monster":  patterns/monster/*.svg  (Pattern Monster, MIT)
  * "custom":   ~/orc1_patterns/*.svg  (your own uploads, 2026-10-12; there
                even when empty, so the set count stays put; at most PAGE
                tiles, so it is never split into pages)
  * "pictures": ~/orc1_pictures/*.png (2026-10-24: your own art / images,
                uploaded as PNG, JPG, GIF, WebP, BMP or SVG and stored as one
                grey PNG each, at most PIC_MAX px on the longer side). The LAST
                set, there even when empty. A picture is NOT one repeat of a
                pattern: the renderer's REPEAT knob (CC 84) shows it repeated,
                mirrored (no seam) or once.
See patterns/CREDITS.md. A MIDI knob has 128 steps, so a set bigger than 128
is split into pages ("monster 1", "monster 2", ...): SET (CC 50) picks the
page, PATTERN (CC 34) picks a tile in it, and every tile is reachable.

Each SVG is ONE repeat of its pattern. It is rasterised once, with
rsvg-convert (Debian: librsvg2-bin), into ~/.cache/orc1/patterns/<set>/
<slug>.png at a whole-number scale of its own width x height, so the texture
holds exactly one period and repeats without a seam, whatever its shape.
Used by the renderer (the tile) and the web (the /visuals gallery). Light:
stdlib only at import; numpy + pygame are imported only to load a tile.
"""
import os
import re
import subprocess
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PAT_DIR = os.path.join(HERE, "patterns")
PAGE = 128
# the built-in set, drawn in code by orc_visuals.make_pattern; the page's
# built-in buttons list the same names (visuals.js BG_PATTERNS)
BUILTIN = ("checker", "stripes", "dots", "truchet", "waves", "diamonds",
           "rings", "plaid", "bricks", "zigzag")
TARGET = 256        # a tile's longer side renders to about this many pixels


def cache_dir():
    return os.path.expanduser("~/.cache/orc1/patterns")


CUSTOM = "custom"
MAX_UPLOAD = 512 * 1024            # bytes: a tile is one small repeat


def user_dir():
    """Your uploaded tiles (resolved at call time: tests sandbox HOME)."""
    return os.path.expanduser("~/orc1_patterns")


PICTURES = "pictures"
# A picture is stored grey, at most PIC_MAX px on its longer side. Why 2048:
# the background is drawn at HALF the screen (640 x 360 on the 1280 x 720
# monitor), and at its default size a picture fills the screen height (360
# background rows), so 2048 still holds detail when you zoom in ~5x. Each
# stored picture is <= 4 MB of grey in memory (2048 x 2048 x 1 byte; about
# 5.3 MB on the GPU with its mipmaps), and the visuals session is capped at
# 200 MB (MemoryHigh), so the renderer keeps only PIC_KEEP pictures loaded.
PIC_MAX = 2048
PIC_UPLOAD_MAX = 16 * 1024 * 1024    # bytes: a phone photo fits (12 MP JPEG ~ 3-8 MB)
PIC_SVG_MAX = 2 * 1024 * 1024        # an SVG drawing (text) may be bigger than a tile
PIC_MAX_PIXELS = 40_000_000          # decoded size, before it is shrunk (a 40 MP photo)
PIC_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")


def pictures_dir():
    """Your pictures (resolved at call time: tests sandbox HOME)."""
    return os.path.expanduser("~/orc1_pictures")


def folder_dir(folder):
    """A set's folder name -> where its files live (SVGs; PNGs for pictures)."""
    if folder == CUSTOM:
        return user_dir()
    if folder == PICTURES:
        return pictures_dir()
    return os.path.join(PAT_DIR, folder)


def _slugs(folder):
    d = folder_dir(folder)
    ext = ".png" if folder == PICTURES else ".svg"
    try:
        return sorted(f[:-4] for f in os.listdir(d) if f.endswith(ext) and not f.startswith("."))
    except OSError:
        return []


def pattern_sets(builtin=()):
    """-> [(set name, folder or None, [names])], in knob order. `builtin` =
    orc_visuals.BG_PATTERNS (passed in: this module must not import the
    renderer)."""
    out = [("built-in", None, list(builtin))]
    for folder in ("hero", "monster"):
        names = _slugs(folder)
        pages = [names[i:i + PAGE] for i in range(0, len(names), PAGE)]
        for n, page in enumerate(pages, 1):
            label = folder if len(pages) == 1 else f"{folder} {n}"
            out.append((label, folder, page))
    out.append((CUSTOM, CUSTOM, _slugs(CUSTOM)[:PAGE]))
    out.append((PICTURES, PICTURES, _slugs(PICTURES)[:PAGE]))   # always LAST (only APPEND sets)
    return out


def svg_size(path):
    with open(path, encoding="utf-8") as f:
        head = f.read(400)
    m = re.search(r'width="([\d.]+)" height="([\d.]+)"', head)
    if m:                                 # every shipped tile (fast path)
        return float(m.group(1)), float(m.group(2))
    with open(path, encoding="utf-8") as f:
        return _size_of(f.read())


def _size_of(text):
    """width/height from the root <svg> (any order, "px" allowed), else its
    viewBox. Raises ValueError when neither gives a positive size."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    num = re.compile(r"^\s*([\d.]+)\s*(px)?\s*$")
    w, h = (num.match(root.get(k, "")) for k in ("width", "height"))
    if w and h:
        size = float(w.group(1)), float(h.group(1))
    else:
        vb = re.findall(r"[-\d.]+", root.get("viewBox", ""))
        if len(vb) != 4:
            raise ValueError("no size: give the <svg> a width and height (or a viewBox)")
        size = float(vb[2]), float(vb[3])
    if not (size[0] > 0 and size[1] > 0):
        raise ValueError("the drawing's size is zero")
    return size


def tile_scale(w, h):
    """The whole-number scale a w x h tile renders at: its longer side near
    TARGET px, never below 1x, and a tiny tile (4 x 4, 40 x 1) not above 64x."""
    return max(1, min(64, round(TARGET / max(w, h))))


def tile_png(folder, slug):
    """-> the cached PNG for one tile, rasterising it first if it is missing
    or older than its SVG. Raises on failure (no rsvg-convert, bad SVG)."""
    if folder == PICTURES:                  # stored ready to use (save_picture)
        return os.path.join(pictures_dir(), slug + ".png")
    svg = os.path.join(folder_dir(folder), slug + ".svg")
    out_dir = os.path.join(cache_dir(), folder)
    png = os.path.join(out_dir, slug + ".png")
    if os.path.exists(png) and os.path.getmtime(png) >= os.path.getmtime(svg):
        return png
    os.makedirs(out_dir, exist_ok=True)
    w, h = svg_size(svg)
    k = tile_scale(w, h)
    # the web (gallery) and the renderer can rasterise the same tile at once:
    # each writes its OWN temp file (pid + thread) and only a complete one is
    # renamed into place, so nobody ever publishes (or reads) a torn PNG
    tmp = "%s.%d.%d.tmp.png" % (png, os.getpid(), threading.get_ident())
    try:
        subprocess.run(["rsvg-convert", "-w", str(max(1, round(w * k))), "-h", str(max(1, round(h * k))),
                        "-o", tmp, svg], check=True, capture_output=True, timeout=20)
        os.replace(tmp, png)
    finally:
        try:
            os.remove(tmp)                # only left over when rsvg failed
        except OSError:
            pass
    return png


def load_tile(folder, slug):
    """-> (grey float32 [rows, cols] in 0..1, (width, height) in tile units).
    Grey = the red channel (the tiles are grey on black). A PICTURE comes back
    as uint8 [rows, cols] 0..255 instead (see below)."""
    import numpy as np
    import pygame
    png = tile_png(folder, slug)
    if folder == PICTURES:
        # stored grey: read it straight as bytes (uint8 [rows, cols]). The
        # float route below costs ~50 MB per 2048 px picture in a session
        # capped at 200 MB (2026-10-24 review); the renderer takes either.
        from PIL import Image
        with Image.open(png) as im:
            a = np.asarray(im.convert("L"))
        return a, picture_units(a.shape[1], a.shape[0])
    a = pygame.surfarray.array3d(pygame.image.load(png))[:, :, 0].T
    return a.astype("f4") / 255.0, svg_size(os.path.join(folder_dir(folder), slug + ".svg"))


def picture_units(w, h):
    """A w x h px picture -> its size in tile units, so the SCALE knob's
    default (1.5 px per unit at 720) shows it as tall as the screen, whatever
    its pixel count, aspect kept."""
    return (480.0 * w / h, 480.0)


# ---- your own tiles (the CUSTOM set) -------------------------------------------

_BANNED_TAGS = ("script", "image", "foreignObject", "iframe", "audio", "video")


def check_upload(text):
    """An uploaded SVG tile -> (width, height) in its own units, or raises
    ValueError with a reason a person can act on. Safety: no DOCTYPE /
    ENTITY (XML tricks), no script / embedded images / foreign content, and
    every link must point inside the file (#id) -- rsvg-convert renders it,
    so nothing in it may reach another file or the network."""
    if len(text.encode("utf-8")) > MAX_UPLOAD:
        raise ValueError("too big (512 KB max): a pattern is one small repeat")
    _check_svg_safety(text)
    return _size_of(text)


def _check_svg_safety(text):
    """The SVG rules both tiles and pictures follow (see check_upload)."""
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        raise ValueError("DOCTYPE / ENTITY declarations aren't allowed")
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError("not valid SVG/XML: %s" % e)
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("not an SVG drawing")
    for el in root.iter():
        tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
        if tag in _BANNED_TAGS:
            raise ValueError("<%s> isn't allowed in a pattern (lines and shapes only)" % tag)
        for k, v in el.attrib.items():
            if k.split("}")[-1] == "href" and not v.startswith("#"):
                raise ValueError("links to other files aren't allowed (%s)" % v[:60])
            if "url(" in v.replace(" ", "").replace("url(#", ""):
                raise ValueError("url() to another file isn't allowed")
        body = (el.text or "").replace(" ", "")
        if tag == "style" and ("@import" in body or "url(" in body.replace("url(#", "")):
            raise ValueError("styles may not load other files")


def tile_stats(png):
    """-> (brightest red 0..255, share of the tile that is dark): the ORC
    reads a tile's brightness from its RED channel, gaps = black.
    Transparent counts as black (that's how it shows on screen)."""
    from PIL import Image, ImageChops
    im = Image.open(png).convert("RGBA")
    r = ImageChops.multiply(im.getchannel("R"), im.getchannel("A"))
    hist = r.histogram()
    total = sum(hist)
    brightest = max((i for i, n in enumerate(hist) if n), default=0)
    dark = sum(hist[:16]) / float(total or 1)
    return brightest, dark


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "pattern"


def save_upload(filename, text):
    """Check + rasterise + store one of YOUR tiles. -> (slug, warning or None).
    Raises ValueError (nothing stored) when it can't be used."""
    w, h = check_upload(text)
    slug = slugify(os.path.splitext(os.path.basename(filename or "pattern"))[0])
    have = _slugs(CUSTOM)
    if slug not in have and len(have) >= PAGE:
        raise ValueError("CUSTOM is full (%d patterns): delete one first" % PAGE)
    d = user_dir()
    os.makedirs(d, exist_ok=True)
    tmp_svg = os.path.join(d, ".%s.%d.upload" % (slug, os.getpid()))
    tmp_png = tmp_svg + ".png"
    with open(tmp_svg, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        k = tile_scale(w, h)
        try:
            subprocess.run(["rsvg-convert", "-w", str(max(1, round(w * k))), "-h", str(max(1, round(h * k))),
                            "-o", tmp_png, tmp_svg], check=True, capture_output=True, timeout=20)
        except subprocess.CalledProcessError as e:
            raise ValueError("it doesn't render: %s" % (e.stderr or b"").decode("utf-8", "replace")[:120])
        except subprocess.TimeoutExpired:
            raise ValueError("it took too long to render")
        brightest, dark = tile_stats(tmp_png)
        if brightest < 40:
            raise ValueError("it would draw nothing: the ORC reads brightness from the RED part of each "
                             "colour, and this drawing has almost none. Draw in white or grey on black "
                             "(or on transparent)")
        warn = None
        if dark < 0.02:
            warn = ("no black gaps: it will cover the whole background (the black between "
                    "shapes comes from the black parts of the tile)")
        os.replace(tmp_svg, os.path.join(d, slug + ".svg"))
        try:                                      # drop a replaced tile's old cached raster
            os.remove(os.path.join(cache_dir(), CUSTOM, slug + ".png"))
        except OSError:
            pass
        return slug, warn
    finally:
        for f in (tmp_svg, tmp_png):
            try:
                os.remove(f)
            except OSError:
                pass


def delete_upload(slug):
    """Remove one of YOUR tiles (and its cached raster). -> True if it existed."""
    if slug not in _slugs(CUSTOM):
        return False
    os.remove(os.path.join(user_dir(), slug + ".svg"))
    try:
        os.remove(os.path.join(cache_dir(), CUSTOM, slug + ".png"))
    except OSError:
        pass
    return True


def custom_sig():
    """Changes whenever an upload / delete changes the CUSTOM or PICTURES folder."""
    out = []
    for d in (user_dir(), pictures_dir()):
        try:
            out.append(os.stat(d).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


# ---- your own art / images (the PICTURES set) ------------------------------------

def _svg_to_png(text, tmp_dir):
    """A checked SVG drawing -> a temp PNG path, at most PIC_MAX px on its
    longer side (rsvg-convert, like the tiles). Raises ValueError."""
    if len(text.encode("utf-8")) > PIC_SVG_MAX:
        raise ValueError("too big (2 MB max for an SVG drawing)")
    _check_svg_safety(text)
    w, h = _size_of(text)
    k = PIC_MAX / max(w, h)
    # unique names: the web serves uploads on threads of ONE process, so a
    # pid-only name let two SVG uploads at once swap or delete each other's files
    fd, svg = tempfile.mkstemp(prefix=".pic.", suffix=".svg", dir=tmp_dir)
    png = svg + ".png"
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    done = False
    try:
        subprocess.run(["rsvg-convert", "-w", str(max(1, round(w * k))), "-h", str(max(1, round(h * k))),
                        "-o", png, svg], check=True, capture_output=True, timeout=30)
        done = True
    except FileNotFoundError:
        raise ValueError("SVG pictures need rsvg-convert on the Pi (apt install librsvg2-bin)")
    except subprocess.CalledProcessError as e:
        raise ValueError("it doesn't render: %s" % (e.stderr or b"").decode("utf-8", "replace")[:120])
    except subprocess.TimeoutExpired:
        raise ValueError("it took too long to render")
    finally:
        for p in ((svg,) if done else (svg, png)):   # a failed render may leave half a PNG
            try:
                os.remove(p)
            except OSError:
                pass
    return png


# EXIF orientation -> the PIL transpose that turns a phone photo upright
# (the same table ImageOps.exif_transpose uses; Image.Transpose values)
_EXIF_TURN = {2: 0, 3: 3, 4: 1, 5: 5, 6: 4, 7: 6, 8: 2}


def picture_grey(data):
    """An uploaded image file's bytes -> a PIL grey ("L") image, at most
    PIC_MAX px on its longer side. Phone photos are turned upright (EXIF),
    transparent = black (that's how it shows on screen), colour -> its
    brightness (luminance: unlike a tile, blue counts). Raises ValueError."""
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = PIC_MAX_PIXELS          # bigger = refused by PIL itself
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w * h > PIC_MAX_PIXELS:
            raise ValueError("too many pixels (%d x %d; 40 megapixels max)" % (w, h))
        if im.format == "JPEG":
            im.draft("RGB", (PIC_MAX, PIC_MAX))      # decode a big JPEG small: fast, little memory
        orient = im.getexif().get(0x0112, 1)         # read now: the shrunk copy has no EXIF
        im.load()
        # GREY FIRST, in strips, then shrink (2026-10-24 review): converting /
        # compositing a 40 MP picture at full size took ~600 MB in orc1-web on
        # a 1.8 GB Pi (measured), and a big allocation can wedge jackd
        # (skipback). Strips keep every colour copy small; the full-size grey
        # is 1 byte a pixel. Peak ~ the decode + a quarter of it.
        if im.mode != "L":
            w, h = im.size
            g = Image.new("L", (w, h))
            for y in range(0, h, 256):
                g.paste(_grey(im.crop((0, y, w, min(h, y + 256)))), (0, y))
            im = g
        if max(im.size) > PIC_MAX:
            im.thumbnail((PIC_MAX, PIC_MAX), Image.LANCZOS, reducing_gap=2.0)
        if orient in _EXIF_TURN:                     # = ImageOps.exif_transpose, on the small copy
            im = im.transpose(_EXIF_TURN[orient])
    except ValueError:
        raise
    except Exception as e:                           # not an image / truncated / a decompression bomb
        raise ValueError("not an image this can read (%s)" % (str(e)[:80] or type(e).__name__))
    return im


def _grey(im):
    """Any PIL image (a strip of one) -> grey "L": transparent = black, colour
    -> its brightness (luminance)."""
    from PIL import Image
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA"):
        rgba = im.convert("RGBA")
        im = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba)
    return im.convert("L")


def save_picture(filename, data):
    """Check + convert + store one of YOUR pictures (bytes of a PNG / JPG /
    GIF / WebP / BMP / SVG file). -> (slug, warning or None). Raises
    ValueError (nothing stored) when it can't be used. Same name = replaces."""
    if len(data) > PIC_UPLOAD_MAX:
        raise ValueError("too big (16 MB max)")
    name = os.path.basename(filename or "picture")
    slug = slugify(os.path.splitext(name)[0])
    have = _slugs(PICTURES)
    if slug not in have and len(have) >= PAGE:
        raise ValueError("PICTURES is full (%d pictures): delete one first" % PAGE)
    d = pictures_dir()
    os.makedirs(d, exist_ok=True)
    svg_png = None
    try:
        head = data[:512].lstrip().lower()
        if name.lower().endswith(".svg") or head.startswith(b"<?xml") or head.startswith(b"<svg"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("not a text SVG file")
            svg_png = _svg_to_png(text, d)
            with open(svg_png, "rb") as f:
                data = f.read()
        g = picture_grey(data)
        lo, hi = g.getextrema()
        if hi < 24:
            raise ValueError("it's all black: nothing would show (INVERT can flip a dark "
                             "drawing, but this one has nothing in it)")
        warn = None
        if hi - lo < 40:
            warn = "very low contrast: try CRISP to cut it to black and white"
        fd, tmp = tempfile.mkstemp(prefix=".%s." % slug, suffix=".tmp.png", dir=d)
        try:
            with os.fdopen(fd, "wb") as f:
                g.save(f, format="PNG", optimize=True)
            os.replace(tmp, os.path.join(d, slug + ".png"))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return slug, warn
    finally:
        if svg_png:
            try:
                os.remove(svg_png)
            except OSError:
                pass


def delete_picture(slug):
    """Remove one of YOUR pictures. -> True if it existed."""
    if slug not in _slugs(PICTURES):
        return False
    os.remove(os.path.join(pictures_dir(), slug + ".png"))
    return True
