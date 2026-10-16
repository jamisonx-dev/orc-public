"""vis_patterns.py -- the VISUALS background's pattern sets (2026-09-30).

Three sources of repeating tiles:
  * "built-in": drawn by orc_visuals.make_pattern (no files)
  * "hero":     patterns/hero/*.svg     (Hero Patterns, CC BY 4.0)
  * "monster":  patterns/monster/*.svg  (Pattern Monster, MIT)
  * "custom":   ~/orc1_patterns/*.svg  (your own uploads, 2026-10-12; always
                the LAST set, even when empty, so the set count stays put;
                at most PAGE tiles, so it is never split into pages)
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


def folder_dir(folder):
    """A set's folder name -> where its SVGs live."""
    return user_dir() if folder == CUSTOM else os.path.join(PAT_DIR, folder)


def _slugs(folder):
    d = folder_dir(folder)
    try:
        return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".svg"))
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
    Grey = the red channel (the tiles are grey on black)."""
    import numpy as np
    import pygame
    png = tile_png(folder, slug)
    a = pygame.surfarray.array3d(pygame.image.load(png))[:, :, 0].T
    return a.astype("f4") / 255.0, svg_size(os.path.join(folder_dir(folder), slug + ".svg"))


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
    return _size_of(text)


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
    """Changes whenever an upload / delete changes the CUSTOM folder."""
    try:
        return os.stat(user_dir()).st_mtime_ns
    except OSError:
        return None
