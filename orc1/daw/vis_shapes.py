"""vis_shapes.py -- geometry for the VISUALS "shape machine" scenes (2026-09-30).

Pure numpy, no drawing: orc_visuals' scenes turn these points into lines.
owner found the hypercube and the tube "entrancing"; these generalise them:

  * SWEEP (the tube, generalised): a cross-section shape carried along a
    path -- a torus knot, with a superformula section (one formula that makes
    polygons, stars and flowers and morphs smoothly between them).
  * 4D (the hypercube, generalised): the regular 4D polytopes, duoprisms,
    any flat shape pushed out into 4D, and the Hopf fibration -- all rotated
    in 4D (which turns them inside out) and projected to the screen.

Coordinates are unit-sized; the scene scales them to the screen.
"""
import math

import numpy as np

PHI = (1 + 5 ** 0.5) / 2


# ---------------------------------------------------------------- 2D shapes

def superformula(m, n1, n2, n3, a=1.0, b=1.0, k=96):
    """Gielis' superformula: r(t) = (|cos(m t/4)/a|^n2 + |sin(m t/4)/b|^n3)^(-1/n1).
    m = how many lobes, n1..n3 = how round/pinched. -> (k, 2), max radius 1."""
    t = np.linspace(0, 2 * np.pi, k, endpoint=False)
    c = np.abs(np.cos(m * t / 4) / a) ** n2
    s = np.abs(np.sin(m * t / 4) / b) ** n3
    r = (c + s + 1e-9) ** (-1.0 / n1)
    r = r / np.max(r)
    return np.stack([r * np.cos(t), r * np.sin(t)], 1)


def chord_shape(notes, phase, k=96):
    """A superformula shape from the chord: as many lobes as notes (at least
    3), a 7th/9th adds more; phase (0..1, slow time) breathes n1..n3 so the
    shape morphs between round, star and flower."""
    pcs = sorted({n % 12 for n in notes}) or [0]
    m = max(3, len(pcs)) + (2 if len(pcs) >= 4 else 0)
    w = 0.5 + 0.5 * math.sin(2 * math.pi * phase)
    n1 = 0.6 + 1.4 * w               # below ~0.5 the lobes pinch to spikes
    n2 = 1.0 + 3.0 * (1 - w)
    return superformula(m, n1, n2, n2, k=k)


# ------------------------------------------------------------------ sweeps

TORUS_KNOTS = [(2, 3), (2, 5), (3, 4), (3, 5), (2, 7), (3, 7), (4, 5), (5, 6)]


def torus_knot(p, q, n=180, R=1.0, r=0.42):
    """A (p, q) torus knot: winds p times round the hole and q times through
    it. -> (n, 3), closed."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    rr = R + r * np.cos(q * t)
    return np.stack([rr * np.cos(p * t), rr * np.sin(p * t), r * np.sin(q * t)], 1)


def sweep(path, section, radius):
    """Carry `section` (k, 2) along a CLOSED `path` (n, 3) with rotation-
    minimising frames (no sudden flips), closing the twist left at the end
    so the tube joins itself. -> (n, k, 3) points."""
    n = len(path)
    T = np.roll(path, -1, 0) - np.roll(path, 1, 0)
    T /= np.linalg.norm(T, axis=1, keepdims=True) + 1e-12
    Nv = np.cross(T[0], [0.0, 0.0, 1.0] if abs(T[0][2]) < 0.9 else [1.0, 0.0, 0.0])
    Nv /= np.linalg.norm(Nv)
    Ns = np.empty((n, 3))
    Ns[0] = Nv
    for i in range(1, n):                       # parallel transport
        v = Ns[i - 1] - np.dot(Ns[i - 1], T[i]) * T[i]
        Ns[i] = v / (np.linalg.norm(v) + 1e-12)
    Bs = np.cross(T, Ns)
    # the transported frame comes back rotated by some angle: spread that
    # angle along the tube so the last ring meets the first
    end = Ns[-1] - np.dot(Ns[-1], T[0]) * T[0]
    ang = math.atan2(np.dot(np.cross(end, Ns[0]), T[0]), np.dot(end, Ns[0]))
    a = np.linspace(0, ang, n, endpoint=False)[:, None]
    N2 = Ns * np.cos(a) + Bs * np.sin(a)
    B2 = np.cross(T, N2)
    sx, sy = section[:, 0] * radius, section[:, 1] * radius
    return path[:, None, :] + N2[:, None, :] * sx[None, :, None] + B2[:, None, :] * sy[None, :, None]


# ---------------------------------------------------------------------- 4D

def _even_perms4():
    out = []
    import itertools
    for p in itertools.permutations(range(4)):
        inv = sum(1 for i in range(4) for j in range(i + 1, 4) if p[i] > p[j])
        if inv % 2 == 0:
            out.append(p)
    return out


def _edges_by_distance(V, tol=1e-4):
    """Edges = every pair at the smallest distance (true for all of these)."""
    d = np.linalg.norm(V[:, None, :] - V[None, :, :], axis=2)
    iu = np.triu_indices(len(V), 1)
    dmin = d[iu].min()
    mask = np.abs(d[iu] - dmin) < tol * max(1.0, dmin)
    return np.stack([iu[0][mask], iu[1][mask]], 1)


def polytope(name):
    """-> (verts (n, 4) scaled to radius 1, edges (m, 2))."""
    import itertools
    if name == "tesseract":
        V = np.array(list(itertools.product((-1, 1), repeat=4)), float)
    elif name == "16-cell":
        V = np.array([s * np.eye(4)[i] for i in range(4) for s in (-1, 1)])
    elif name == "5-cell":
        V = np.array([[1, 1, 1, -1 / 5 ** 0.5], [1, -1, -1, -1 / 5 ** 0.5],
                      [-1, 1, -1, -1 / 5 ** 0.5], [-1, -1, 1, -1 / 5 ** 0.5],
                      [0, 0, 0, 5 ** 0.5 - 1 / 5 ** 0.5]], float)
    elif name == "24-cell":
        V = np.array([v for i, j in itertools.combinations(range(4), 2)
                      for si in (-1, 1) for sj in (-1, 1)
                      for v in [np.eye(4)[i] * si + np.eye(4)[j] * sj]])
    elif name == "600-cell":
        vs = [s * np.eye(4)[i] for i in range(4) for s in (-1, 1)]
        vs += [np.array(v) / 2 for v in itertools.product((-1, 1), repeat=4)]
        base = (PHI / 2, 0.5, 1 / (2 * PHI), 0.0)
        for p in _even_perms4():
            for signs in itertools.product((-1, 1), repeat=3):
                b = [base[0] * signs[0], base[1] * signs[1], base[2] * signs[2], 0.0]
                vs.append(np.array([b[p[i]] for i in range(4)]))
        V = np.unique(np.round(np.array(vs), 9), axis=0)
    elif name.startswith("duoprism"):
        a, b = (int(x) for x in name.split("-")[1].split("x"))
        V = np.array([[math.cos(2 * math.pi * i / a), math.sin(2 * math.pi * i / a),
                       math.cos(2 * math.pi * j / b), math.sin(2 * math.pi * j / b)]
                      for i in range(a) for j in range(b)])
    else:
        raise ValueError(name)
    V = V - V.mean(0)
    V = V / np.max(np.linalg.norm(V, axis=1))
    if name.startswith("duoprism"):
        # two edge lengths (a 3x8's triangle sides are longer than its octagon
        # sides), so "the shortest pairs" would miss half: build them outright
        E = np.array([e for i in range(a) for j in range(b)
                      for e in ((i * b + j, ((i + 1) % a) * b + j),
                                (i * b + j, i * b + (j + 1) % b))])
        return V.astype(float), np.sort(E, axis=1)
    return V.astype(float), _edges_by_distance(V)


POLYTOPES = ("5-cell", "tesseract", "16-cell", "24-cell", "duoprism-5x5",
             "duoprism-3x8", "600-cell")


def extrude_4d(poly, depth=0.6):
    """A flat closed shape (k, 2) pushed out along z and then w: four copies
    of it joined corner to corner -- the "any shape into 4D" machine (a
    square gives a tesseract). `depth` = how far apart the copies sit (a
    logo reads better closer: 0.35). -> (verts (4k, 4), edges): the first
    4k edges are the four OUTLINES, the rest are the JOINS between copies
    (the scene draws the joins thin, or a bolt looked like "a wrestling ring
    with bolt posts" -- owner 2026-09-30)."""
    k = len(poly)
    P = np.asarray(poly, float) / max(1e-9, np.max(np.linalg.norm(poly, axis=1)))
    V, E = [], []
    a = depth
    for c, (z, w) in enumerate(((-a, -a), (a, -a), (-a, a), (a, a))):
        V += [[x, y, z, w] for x, y in P]
        E += [(c * k + i, c * k + (i + 1) % k) for i in range(k)]
    step = max(1, k // 8)                       # join every few corners, not all
    for i in range(0, k, step):
        E += [(i, k + i), (2 * k + i, 3 * k + i), (i, 2 * k + i), (k + i, 3 * k + i)]
    return np.array(V), np.array(E)


def rotate4(V, angles):
    """Rotate (n, 4) points in the six planes xy, xz, xw, yz, yw, zw."""
    V = np.array(V, float, copy=True)
    for (i, j), a in zip(((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)), angles):
        if a:
            c, s = math.cos(a), math.sin(a)
            xi, xj = V[:, i].copy(), V[:, j].copy()
            V[:, i], V[:, j] = xi * c - xj * s, xi * s + xj * c
    return V


def project4(V, d4=2.6, d3=3.2):
    """4D -> 3D (perspective along w) -> 2D (perspective along z).
    -> (xy (n, 2), depth (n,) in 0..1, 1 = nearest)."""
    k4 = d4 / (d4 + 0.3 - V[:, 3])
    P3 = V[:, :3] * k4[:, None]
    k3 = d3 / (d3 + 0.5 - P3[:, 2])
    xy = P3[:, :2] * k3[:, None]
    z = P3[:, 2] + V[:, 3] * 0.5
    depth = np.clip((z - z.min()) / max(1e-9, np.ptp(z)), 0, 1) if len(z) > 1 else np.ones(1)
    return xy, depth


def project3(P, d3=3.2):
    """3D -> 2D perspective. -> (xy, depth 0..1)."""
    k3 = d3 / (d3 + 0.5 - P[..., 2])
    xy = P[..., :2] * k3[..., None]
    z = P[..., 2]
    depth = np.clip((z - z.min()) / max(1e-9, np.ptp(z)), 0, 1)
    return xy, depth


def hopf_fibers(lats=(-0.7, -0.25, 0.2, 0.6), per=14, n=64, rot=(0, 0, 0, 0, 0, 0), pole=1.6):
    """Hopf fibration: each point of a sphere is a circle in 4D; circles over
    a line of latitude fill a torus, all linked. Rotated in 4D, then
    projected to 3D from a point `pole` along w: pole = 1 is the true
    stereographic projection, which blows circles that pass near the pole up
    into streaks across the screen (seen 2026-09-30); 1.6 keeps them round.
    All fibers in one numpy pass. -> (len(lats) * per, n, 3), each closed."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)[None, :]
    lat = np.repeat(np.asarray(lats, float), per)[:, None]
    ph = np.tile(2 * np.pi * np.arange(per) / per, len(lats))[:, None]
    a, b, c = np.cos(lat) * np.cos(ph), np.cos(lat) * np.sin(ph), np.sin(lat)
    k = 1 / np.sqrt(2 * (1 + c))
    X = np.stack([(1 + c) * np.cos(t) + 0 * a, a * np.sin(t) - b * np.cos(t),
                  a * np.cos(t) + b * np.sin(t), (1 + c) * np.sin(t) + 0 * a], -1) * k[..., None]
    F, N = X.shape[0], X.shape[1]
    X = rotate4(X.reshape(-1, 4), rot).reshape(F, N, 4)
    return X[..., :3] / (pole - X[..., 3:4])
