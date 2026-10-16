"""vis_logos.py -- logos and line drawings for the VISUALS (2026-09-30).

Reads an SVG line drawing into plain polylines the renderer can draw with its
glow lines (the `logo` scene, and `shape4d` pushing a logo's outline into
4D). Standard library only, so the light web process uses it too (upload
check + a preview drawn from the parsed lines, never the raw SVG).

Understood: <path> (M L H V C S Q T A Z, absolute and relative; curves and
arcs flattened into short segments), <line>, <polyline>, <polygon>, <rect>,
<circle>, <ellipse>, and transform= (matrix, translate, scale, rotate,
skewX, skewY) on any element or group. Ignored: text, images, <defs>,
<clipPath>/<mask>/<symbol>/<pattern> contents, <use>, styles and scripts
(nothing is ever executed). Fills are drawn as their OUTLINES -- the look is
line art either way.

Where logos come from: orc1/daw/logos/*.svg (shipped, drawn for the ORC) and
~/orc1_logos/*.svg (yours; the /visuals page uploads there).
"""
import math
import os
import re
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED = os.path.join(HERE, "logos")
MAX_POINTS = 4000          # after simplifying; a logo is drawn every frame
MAX_BYTES = 2 * 1024 * 1024
# The 2 MB cap bounds the FILE, not what it expands to: a dense relative curve
# ("q1 1 1 1 ...") costs ~8 bytes and flattens to 24 points, so 2 MB could
# become millions of (x, y) tuples (> 1 GB in orc1-web, which has no memory
# cap, and in the renderer). So the parser counts raw points AS IT GOES and
# gives up past this many (the shipped drawings need ~2-9k).
MAX_RAW_POINTS = 200000


class TooDetailed(ValueError):
    """The drawing expands to more than MAX_RAW_POINTS raw points."""


def user_dir():
    return os.path.expanduser("~/orc1_logos")


def logo_list():
    """-> [(name, path)]: shipped first, then yours (sorted by name). Names
    are unique: an upload with a shipped drawing's name REPLACES it (in its
    place in the list) rather than showing up twice on the LOGO knob; delete
    the upload and the shipped one is back."""
    out = {}
    for d in (SHIPPED, user_dir()):
        try:
            names = sorted(f for f in os.listdir(d) if f.lower().endswith(".svg"))
        except OSError:
            continue
        for f in names:
            out[f[:-4]] = os.path.join(d, f)
    return list(out.items())


# ------------------------------------------------------------------ transforms

def _mul(a, b):
    """2x3 affine matrices (a, b, c, d, e, f) = [[a c e], [b d f]]; a then b? -> a*b."""
    a1, b1, c1, d1, e1, f1 = a
    a2, b2, c2, d2, e2, f2 = b
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_transform(s):
    m = IDENT
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(_NUM, args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0] if v else 0, v[1] if len(v) > 1 else 0)
        elif name == "scale":
            sx = v[0] if v else 1
            t = (sx, 0, 0, v[1] if len(v) > 1 else sx, 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            c, sn = math.cos(a), math.sin(a)
            t = (c, sn, -sn, c, 0, 0)
            if len(v) >= 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mul(m, t)
    return m


def _apply(m, pts):
    a, b, c, d, e, f = m
    return [(a * x + c * y + e, b * x + d * y + f) for x, y in pts]


# ----------------------------------------------------------------------- paths

def _cubic(p0, p1, p2, p3, n):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def _quad(p0, p1, p2, n):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def _arc(p0, rx, ry, phi, large, sweep, p1):
    """SVG endpoint arc -> points (the spec's F.6.5 conversion)."""
    if p0 == p1:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [p1]
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1 / rx) ** 2 + (y1 / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cx1, cy1 = co * rx * y1 / ry, -co * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(6, int(abs(dt) / (math.pi / 32)))
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return out


def parse_path(d, curve_n=24, limit=MAX_RAW_POINTS):   # 24 pieces per curve: smooth; RDP trims straight bits
    """SVG path data -> list of polylines (lists of (x, y)). Raises
    TooDetailed once it has made more than `limit` points."""
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM, d or "")
    polys, cur = [], []
    x = y = sx = sy = 0.0
    lastc = lastq = None
    cmd = None
    i = 0

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    def flag():
        # arc flags may be written without separators ("a1 1 0 011 1")
        nonlocal i
        t = toks[i]
        if t in ("0", "1"):
            i += 1
            return int(t)
        v = t[0]
        toks[i] = t[1:]
        return int(v)

    done = 0                                   # points in finished polylines
    while i < len(toks):
        if done + len(cur) > limit:
            raise TooDetailed("too detailed (over %d points)" % MAX_RAW_POINTS)
        t = toks[i]
        if re.fullmatch(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    polys.append(cur)
                    done += len(cur)
                    cur = [(sx, sy)]
                x, y = sx, sy
                lastc = lastq = None
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        try:
            if C == "M":
                if len(cur) > 1:
                    polys.append(cur)
                    done += len(cur)
                x, y = ox + num(), oy + num()
                sx, sy = x, y
                cur = [(x, y)]
                cmd = "l" if rel else "L"          # further pairs are lines
                lastc = lastq = None
            elif C == "L":
                x, y = ox + num(), oy + num()
                cur.append((x, y))
                lastc = lastq = None
            elif C == "H":
                x = ox + num()
                cur.append((x, y))
                lastc = lastq = None
            elif C == "V":
                y = oy + num()
                cur.append((x, y))
                lastc = lastq = None
            elif C == "C":
                c1 = (ox + num(), oy + num())
                c2 = (ox + num(), oy + num())
                p = (ox + num(), oy + num())
                cur += _cubic((x, y), c1, c2, p, curve_n)
                x, y = p
                lastc, lastq = c2, None
            elif C == "S":
                c1 = (2 * x - lastc[0], 2 * y - lastc[1]) if lastc else (x, y)
                c2 = (ox + num(), oy + num())
                p = (ox + num(), oy + num())
                cur += _cubic((x, y), c1, c2, p, curve_n)
                x, y = p
                lastc, lastq = c2, None
            elif C == "Q":
                c1 = (ox + num(), oy + num())
                p = (ox + num(), oy + num())
                cur += _quad((x, y), c1, p, curve_n)
                x, y = p
                lastq, lastc = c1, None
            elif C == "T":
                c1 = (2 * x - lastq[0], 2 * y - lastq[1]) if lastq else (x, y)
                p = (ox + num(), oy + num())
                cur += _quad((x, y), c1, p, curve_n)
                x, y = p
                lastq, lastc = c1, None
            elif C == "A":
                rx, ry, phi = num(), num(), num()
                large, sweep = flag(), flag()
                p = (ox + num(), oy + num())
                cur += _arc((x, y), rx, ry, phi, large, sweep, p)
                x, y = p
                lastc = lastq = None
            else:
                i += 1
        except (IndexError, ValueError, TypeError):
            break
    if done + len(cur) > limit:
        raise TooDetailed("too detailed (over %d points)" % MAX_RAW_POINTS)
    if len(cur) > 1:
        polys.append(cur)
    return polys


def _ellipse(cx, cy, rx, ry, n=48):
    pts = [(cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n))
           for k in range(n)]
    return pts + [pts[0]]


SKIP_TAGS = {"defs", "clipPath", "mask", "symbol", "pattern", "style", "script", "text",
             "image", "metadata", "title", "desc", "marker", "linearGradient", "radialGradient",
             "filter", "foreignObject", "use"}


def _f(el, k, d=0.0):
    try:
        return float(re.findall(_NUM, el.get(k, ""))[0])
    except (IndexError, ValueError):
        return d


def parse_svg(text, limit=MAX_RAW_POINTS):
    """SVG text -> list of polylines [(x, y), ...] in the drawing's own units
    (y down). Raises ValueError when it isn't SVG or holds no lines, and
    TooDetailed (a ValueError) past `limit` points in all (circles and
    polylines count too, not just paths)."""
    root = ET.fromstring(text)
    if not root.tag.endswith("svg"):
        raise ValueError("not an SVG")
    out = []
    count = [0]

    def walk(el, m):
        tag = el.tag.split("}")[-1]
        if tag in SKIP_TAGS:
            return
        m = _mul(m, parse_transform(el.get("transform")))
        polys = []
        if tag == "path":
            polys = parse_path(el.get("d"), limit=limit - count[0])
        elif tag == "line":
            polys = [[(_f(el, "x1"), _f(el, "y1")), (_f(el, "x2"), _f(el, "y2"))]]
        elif tag in ("polyline", "polygon"):
            v = [float(x) for x in re.findall(_NUM, el.get("points", ""))]
            pts = list(zip(v[0::2], v[1::2]))
            if tag == "polygon" and pts:
                pts.append(pts[0])
            polys = [pts]
        elif tag == "rect":
            x, y, w, h = _f(el, "x"), _f(el, "y"), _f(el, "width"), _f(el, "height")
            if w > 0 and h > 0:
                polys = [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]]
        elif tag == "circle":
            r = _f(el, "r")
            if r > 0:
                polys = [_ellipse(_f(el, "cx"), _f(el, "cy"), r, r)]
        elif tag == "ellipse":
            rx, ry = _f(el, "rx"), _f(el, "ry")
            if rx > 0 and ry > 0:
                polys = [_ellipse(_f(el, "cx"), _f(el, "cy"), rx, ry)]
        count[0] += sum(len(p) for p in polys)
        if count[0] > limit:
            raise TooDetailed("too detailed (over %d points)" % MAX_RAW_POINTS)
        out.extend(_apply(m, p) for p in polys if len(p) > 1)
        for ch in el:
            walk(ch, m)

    try:
        walk(root, IDENT)
    except RecursionError:                     # thousands of nested <g>s
        raise ValueError("groups nested too deep")
    if not out:
        raise ValueError("no lines found in the SVG")
    return out


# --------------------------------------------------------------- tidy + fit

def _rdp(pts, tol):
    """Ramer-Douglas-Peucker: drop points that don't change the line by > tol."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy) or 1e-12
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            x0, y0 = pts[i]
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / ln if ln > 1e-12 else math.hypot(x0 - x1, y0 - y1)
            if d > best:
                best, bi = d, i
        if best > tol and bi > 0:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def normalize(polys, max_points=MAX_POINTS):
    """Centre on the drawing's middle, scale so its farthest point is 1, and
    simplify until the whole drawing has <= max_points points."""
    xs = [x for p in polys for x, _ in p]
    ys = [y for p in polys for _, y in p]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    r = max(math.hypot(x - cx, y - cy) for p in polys for x, y in p) or 1.0
    P = [[((x - cx) / r, (y - cy) / r) for x, y in p] for p in polys]
    tol = 0.0005
    while sum(len(p) for p in P) > max_points and tol < 0.05:
        P = [_rdp(p, tol) for p in P]
        tol *= 2
    return [p for p in P if len(p) > 1]


def load(path):
    """-> normalised polylines of one SVG file (raises on a bad file)."""
    if os.path.getsize(path) > MAX_BYTES:
        raise ValueError("SVG too big (> 2 MB)")
    with open(path, encoding="utf-8", errors="replace") as f:
        return normalize(parse_svg(f.read()))


def outline(polys, max_points=160):
    """The drawing's biggest line (by length) -- what shape4d pushes into 4D --
    as its OWN points: sharp corners stay exactly where they are and curves
    keep their detail; only points that add nothing are dropped (RDP) to fit
    max_points. (It used to resample to 36 evenly spaced points, which cut
    the corners off a bolt and made an O a polygon: owner asked for
    smoother lines, 2026-09-30.) A closed line comes back without its
    repeated end point (extrude_4d closes it)."""
    def length(p):
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(p, p[1:]))
    p = [tuple(map(float, q)) for q in max(polys, key=length)]
    closed = len(p) > 2 and math.hypot(p[0][0] - p[-1][0], p[0][1] - p[-1][1]) < 1e-6
    tol = 0.0002
    while len(p) > max_points + (1 if closed else 0) and tol < 0.2:
        p = _rdp(p, tol)
        tol *= 1.6
    if closed:
        p = p[:-1]
    return p[:max_points]
