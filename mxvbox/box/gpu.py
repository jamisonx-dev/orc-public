# gpu.py -- the MXV box's GPU screen (Python 2.7 + ctypes, Mali-450 OpenGL ES 2.0).
#
# orcflash composes each 320x180 picture as before; this puts it on the screen through the GPU
# instead of copying it into the framebuffer: one texture upload (~0.5 ms) and one draw, straight
# onto /dev/fb0 through EGL (fbdev window), synced to the refresh. The chip then scales the
# SW x SH surface to the screen (free_scale). The thin lines (orcflash `n`) are drawn here, at the
# surface's own rows. Measured on the box 2026-11-02: 60 fps with a kaleidoscope / crossfade.
#
# BLUR is made here too, live, at any strength 0-4 (orcflash `y`, the kick hit): a "dual Kawase"
# blur, the usual cheap mobile-GPU one: the picture is shrunk in halves (160x90 .. 20x12), each
# shrink averaging 5 samples, then grown back up averaging 8. Level k = k shrinks; a fraction
# mixes two neighbouring levels, so a hit can fade back smoothly. No stored blur copies needed.
#
# Import-safe: the GL libraries load only when a Screen is made (tests import the pure helpers).
import ctypes as C, fcntl, os, struct, time

SW, SH = 640, 360                       # the GPU's surface: the picture doubled, VK = 2 rows per row
FBIOGET_VSCREENINFO, FBIOPUT_VSCREENINFO = 0x4600, 0x4601
GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x1401
GL_TEXTURE0, GL_NEAREST, GL_LINEAR, GL_CLAMP = 0x84C0, 0x2600, 0x2601, 0x812F
GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FRAMEBUFFER_COMPLETE = 0x8D40, 0x8CE0, 0x8CD5
BLUR_MAX = 4
GL_ETC1_RGB8 = 0x8D64                   # OES_compressed_ETC1_RGB8_texture (the Mali-450 has it)


SNAP = "<the scene's last frame, copied on the GPU by Screen.snapshot>"   # draw(prev=SNAP)


class Etc(str):
    """One COMPRESSED clip frame: ETC1, SW x SH (the surface's own size, 4x the pixels of a raw
    320x180 frame in half its bytes; 2026-11-02). The encoder (etcpak, on the Pi) is handed the
    box's BGRA bytes as they are, so the GPU sees it exactly like a raw frame (B, G, R in r, g, b:
    the shaders' swizzles hold; measured on the box). The CPU can't touch its pixels."""
    W, H = SW, SH
    SIZE = SW * SH // 2                  # 4 bits a pixel


# The screen pass flips y (texture row 0 = the picture's top = the screen's top); the blur
# passes don't (texture row 0 stays the top in every blur texture).
VS = ("attribute vec2 p; varying vec2 uv;"
      "void main(){ uv = vec2(p.x * 0.5 + 0.5, 0.5 - p.y * 0.5); gl_Position = vec4(p, 0., 1.); }")
VS_FBO = ("attribute vec2 p; varying vec2 uv;"
          "void main(){ uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0., 1.); }")
# pic / blur textures hold BGRA as RGBA: .zyxw puts the colours back (blur passes don't swizzle).
# ln.z = fill: 0 off / 1 colour / 2 the heaviest blur (b4). WHICH rows are lines comes from `mask` (1 x SH, built on the
# CPU by line_mask): the Mali's fragment maths is 16-bit, and mod() on row numbers put lines on wrong rows.
# bl = blur: x = 1 on, y = the mix from blo to bhi (0-1).
# The LOOKS (2026-11-02, all free on the GPU; see fx_uniforms): they bend WHERE the picture is read
# (q), so hits, blur and the crossfade bend with it; the thin lines stay on the screen's own rows.
#   wob = wobble (x: sideways amount in picture widths, y: waves down the screen, z: phase)
#   kal = kaleidoscope (x: mirrors, 0 = off; y: turn in radians), rgb = RGB split (picture widths)
#   col = colour (x: hue turn in radians, y: saturation, z: invert 0-1, w: 1 = on)
#   xf  = crossfade (x: 1 = on, y: 0 = all the outgoing picture `prev` .. 1 = all the new one)
#   zs  = zoom + shake (x: zoom, 0 = none, 0.2 = 20 % closer; y, z: shift in picture widths/heights)
FS = """precision mediump float;
varying vec2 uv;
uniform sampler2D pic;
uniform sampler2D mask;
uniform sampler2D blo;
uniform sampler2D bhi;
uniform vec4 ln;
uniform vec3 lncol;
uniform vec2 bl;
uniform sampler2D bmask;
uniform sampler2D b4;
uniform float bm;
uniform sampler2D prev;
uniform vec3 wob;
uniform vec2 kal;
uniform float rgb;
uniform vec4 col;
uniform vec2 xf;
uniform vec3 zs;
uniform sampler2D fr;
uniform float fron;
vec3 pix(vec2 q){
    vec4 c = bl.x > 0.5 ? mix(texture2D(blo, q), texture2D(bhi, q), bl.y) : texture2D(pic, q);
    if (bm > 0.5 && texture2D(bmask, q).r > 0.5)
        c = texture2D(b4, q);                        /* interlace rows, blur borders / bands / lines */
    if (xf.x > 0.5)
        c = mix(texture2D(prev, q), c, xf.y);
    return c.zyx;
}
void main(){
    vec2 q = uv;
    if (zs.x != 0.0 || zs.y != 0.0 || zs.z != 0.0)
        q = clamp(0.5 + (q - 0.5) / (1.0 + zs.x) + zs.yz, 0.0, 1.0);
    if (wob.x != 0.0)
        q.x = fract(q.x + wob.x * sin(6.2832 * wob.y * q.y + wob.z));
    if (kal.x > 0.5) {
        vec2 p = (q - 0.5) * vec2(1.7778, 1.0);
        float s = 6.2832 / kal.x;
        float a = mod(atan(p.y, p.x) + kal.y, s);
        if (a > s * 0.5) a = s - a;                  /* fold: every wedge a mirror of the first */
        q = 0.5 + length(p) * vec2(cos(a), sin(a)) / vec2(1.7778, 1.0);
    }
    vec3 c = rgb > 0.0 ? vec3(pix(q + vec2(rgb, 0.0)).r, pix(q).g, pix(q - vec2(rgb, 0.0)).b) : pix(q);
    if (col.w > 0.5) {
        float y = dot(c, vec3(0.299, 0.587, 0.114));
        vec2 iq = vec2(dot(c, vec3(0.596, -0.274, -0.322)), dot(c, vec3(0.211, -0.523, 0.312)));
        float cs = cos(col.x), sn = sin(col.x);
        iq = vec2(iq.x * cs - iq.y * sn, iq.x * sn + iq.y * cs) * col.y;
        c = vec3(y + 0.956 * iq.x + 0.621 * iq.y, y - 0.272 * iq.x - 0.647 * iq.y, y - 1.106 * iq.x + 1.703 * iq.y);
        c = clamp(mix(c, 1.0 - c, col.z), 0.0, 1.0);
    }
    if (ln.z > 0.5) {
        if (texture2D(mask, vec2(0.5, uv.y)).r > 0.5)
            c = ln.z > 1.5 ? texture2D(b4, uv).zyx : lncol;
    }
    if (fron > 0.5)
        c *= texture2D(fr, uv).r;                    /* FRAME: the light only where the frame lets it */
    gl_FragColor = vec4(c, 1.0);
}
"""

# FRAME / PEEPHOLE (2026-11-02, owner: projected, "block out an area / have a spotlight made of cool
# things / not have everything be a rectangle"): the LAST step, after every look, so the shape stays
# put on the wall while the picture moves inside it. Outside the frame = no light at all (or inside,
# inverted). Its texture is drawn only when a frame setting changes. fc = (centre x, y, size, soft
# edge), fi = invert; shapes 1-4 drawn, 5 = your own picture (white = light; fit into the screen).
FRAME_SHAPES = ("circle", "ellipse", "diamond", "rounded", "picture")
FRAME_GLSL = (
    "soft(length(p) - s * 0.9, w)",                                                      # circle
    "soft(length((uv - fc.xy) / max(vec2(0.5 * s), 0.01)) - 1.0, w * 2.0 / max(s, 0.05))",   # ellipse
    "soft(abs(p.x) + abs(p.y) - s * 1.2, w)",                                            # diamond
    "soft(length(max(abs(p) - vec2(s * 0.8 - 0.12), 0.0)) - 0.12, w)",                   # rounded square
    "inside(0.5 + (uv - fc.xy) / max(s, 0.01)) * texture2D(fp, 0.5 + (uv - fc.xy) / max(s, 0.01)).r",   # picture
)


def frame_fs(shape):
    return ("precision mediump float;\nvarying vec2 uv;\nuniform sampler2D fp;\nuniform vec4 fc;\nuniform float fi;\n"
            "float soft(float d, float w){ return clamp(0.5 - d / max(w, 0.003), 0.0, 1.0); }\n"
            "float inside(vec2 q){ return step(0.0, q.x) * step(q.x, 1.0) * step(0.0, q.y) * step(q.y, 1.0); }\n"
            "void main(){\n    vec2 p = (uv - fc.xy) * vec2(1.7778, 1.0);\n    float s = fc.z, w = fc.w;\n"
            "    float m = %s;\n    m = mix(m, 1.0 - m, fi);\n    gl_FragColor = vec4(m, m, m, 1.0);\n}\n"
            % FRAME_GLSL[shape - 1])
# hp = half a texel of the SOURCE texture
FS_DOWN = """precision mediump float;
varying vec2 uv;
uniform sampler2D src;
uniform vec2 hp;
void main(){
    gl_FragColor = (texture2D(src, uv) * 4.0 + texture2D(src, uv - hp) + texture2D(src, uv + hp)
                    + texture2D(src, uv + vec2(hp.x, -hp.y)) + texture2D(src, uv - vec2(hp.x, -hp.y))) / 8.0;
}
"""
FS_UP = """precision mediump float;
varying vec2 uv;
uniform sampler2D src;
uniform vec2 hp;
void main(){
    vec4 s = texture2D(src, uv + vec2(-hp.x * 2.0, 0.0)) + texture2D(src, uv + vec2(hp.x * 2.0, 0.0))
           + texture2D(src, uv + vec2(0.0, hp.y * 2.0)) + texture2D(src, uv + vec2(0.0, -hp.y * 2.0))
           + (texture2D(src, uv + vec2(-hp.x, hp.y)) + texture2D(src, uv + vec2(hp.x, hp.y))
            + texture2D(src, uv + vec2(hp.x, -hp.y)) + texture2D(src, uv + vec2(-hp.x, -hp.y))) * 2.0;
    gl_FragColor = s / 12.0;
}
"""

# SCENES (2026-11-02): pictures the GPU DRAWS (no clip, no art), into the picture slot at the picture's
# own size (320x180: chunky, like the clips and the consoles they're borrowed from), so every look,
# blur and hit reaction works on them. Written as BGR (the screen pass swaps it back). uv.y = 0 is the
# TOP. sp = four phases, each 0-1 and wrapped on the CPU (scene_phases): every pattern repeats exactly
# once per phase, so the wrap is seamless. sc.x = which scene (SCENES index + 1), sc.y = DETAIL 0-1.
# WEAVE (2026-11-02): orcflash's x look (rows from a second clip) as a GPU pass BEFORE everything else,
# so blur, hits and looks see the woven picture as before. rows = 1 x h, 255 = take the other clip's
# row (orcflash.weave_rows: exactly mix_rows' choice). In Python it cost 25-55 ms a frame live.
FS_WEAVE = """precision mediump float;
varying vec2 uv;
uniform sampler2D src;
uniform sampler2D other;
uniform sampler2D rows;
void main(){
    gl_FragColor = texture2D(rows, vec2(0.5, uv.y)).r > 0.5 ? texture2D(other, uv) : texture2D(src, uv);
}
"""

# MASK / PEEPHOLE (2026-11-02): a second picture shows THROUGH a shape (or where the picture is bright
# / dark). One pass at the surface's size, after the weave. mk = (shape 1-7, size 0-1, fill, phase):
# fill 0 = another clip, 1 = black, 2 = the picture's negative, 3 = the picture mirrored. mc = the
# hole's centre. Edges fade across a few screen pixels (no staircase when it moves).
MASK_SHAPES = ("circle", "diamond", "square", "stripes", "rings", "bright", "dark")
MASK_FILLS = ("clip", "black", "negative", "mirror")
# One small program per (shape, fill), built the first time it is used: no branching at all. (A chain
# of if / else on a uniform came out WRONG on the Mali-450: every shape a circle, measured 2026-11-02;
# working out all of them and keeping one by weight was right but cost ~10 ms a frame.)
MASK_SHAPE_GLSL = (
    "soft(length(p) - s * 0.9, 0.012)",                                        # circle
    "soft(abs(p.x) + abs(p.y) - s * 1.2, 0.012)",                              # diamond
    "soft(max(abs(p.x), abs(p.y)) - s * 0.8, 0.012)",                          # square
    "soft(abs(fract(uv.x * 8.0 + mk.w) - 0.5) - s * 0.5, 0.04)",               # stripes (slide)
    "soft(abs(fract(length(p) * 6.0 - mk.w) - 0.5) - s * 0.5, 0.05)",          # rings (ripple)
    "clamp((dot(a.rgb, vec3(0.333)) - (1.0 - s)) / 0.08 + 0.5, 0.0, 1.0)",     # bright key
    "clamp((1.0 - dot(a.rgb, vec3(0.333)) - (1.0 - s)) / 0.08 + 0.5, 0.0, 1.0)",   # dark key
)
MASK_FILL_GLSL = ("texture2D(other, uv).rgb", "vec3(0.0)", "1.0 - a.rgb", "texture2D(src, vec2(1.0 - uv.x, uv.y)).rgb")


def mask_fs(shape, fill):
    """The mask program's fragment shader for MASK_SHAPES[shape - 1] with MASK_FILLS[fill]."""
    return ("precision mediump float;\nvarying vec2 uv;\nuniform sampler2D src;\nuniform sampler2D other;\n"
            "uniform vec4 mk;\nuniform vec2 mc;\n"
            "float soft(float d, float w){ return clamp(0.5 - d / w, 0.0, 1.0); }\n"
            "void main(){\n    vec4 a = texture2D(src, uv);\n    vec2 p = (uv - mc) * vec2(1.7778, 1.0);\n"
            "    float s = mk.y;\n    float m = %s;\n    gl_FragColor = vec4(mix(a.rgb, %s, m), 1.0);\n}\n"
            % (MASK_SHAPE_GLSL[shape - 1], MASK_FILL_GLSL[fill]))

SCENES = ("parallax", "floor", "tunnel", "rings", "spiral", "opart")
FS_SCENE = """#extension GL_OES_standard_derivatives : enable
precision mediump float;
varying vec2 uv;
uniform vec4 sp;
uniform vec4 sc;
uniform sampler2D va;
uniform sampler2D vb;
uniform sampler2D vc;
const float T = 6.2832;
const vec3 DARK = vec3(0.03, 0.0, 0.07);
const vec3 LITE = vec3(0.55, 0.42, 0.68);          /* moderate contrast: kinder to the eyes */
/* SCENE VIDEO (sc.z 0-1): each layer / tile mixes from its drawn colour to one of three clips (va vb
   vc). Tiles MIRROR at their edges (1 - |2 fract(x/2) - 1|), so a clip repeated across a layer shows
   no seam. The clips hold BGRA as RGBA: .bgr = their true colour. */
vec3 V(float i, vec2 c){
    vec2 m = 1.0 - abs(fract(c * 0.5) * 2.0 - 1.0);
    return (i < 0.5 ? texture2D(va, m) : i < 1.5 ? texture2D(vb, m) : texture2D(vc, m)).bgr;
}
/* SOFT EDGES (2026-11-02, owner: "the staircase effect"): every edge fades across ONE screen pixel,
   measured with fwidth (how much x changes from this pixel to the next). Stripes finer than a pixel
   fade to their average instead of shimmering. Kept out of per-pixel ifs (derivatives need that). */
float px1(float x){ return max(fwidth(x), 0.0001); }
float sq(float x){ return clamp(0.5 + (0.25 - abs(fract(x) - 0.5)) / px1(x), 0.0, 1.0); }  /* soft stripes */
float edge(float v){ return clamp(v / px1(v) + 0.5, 0.0, 1.0); }                          /* soft v > 0 */
float xr(float a, float b){ return a + b - 2.0 * a * b; }                                  /* soft xor */
void main(){
    vec2 q = uv;                                     /* q.y = 0 = texture row 0 = the picture's TOP */
    vec2 p = (q - 0.5) * vec2(1.7778, 1.0);
    float r = max(length(p), 0.001);
    float vid = sc.z;
    vec3 c = DARK;
    if (sc.x < 1.5) {                                /* PARALLAX: sky, far ridge, near hills, striped ground */
        c = mix(vec3(0.06, 0.01, 0.13), vec3(0.28, 0.05, 0.22), q.y * 1.4);
        float band = sq(q.y * 18.0 + 0.25) * (1.0 - edge(q.y - 0.4));   /* console-style sky bands */
        c *= 0.9 + 0.1 * band;
        c = mix(c, V(0.0, vec2(q.x * 1.2 + sp.x * 0.5, q.y * 1.6)) * 0.45, vid);   /* the sky: clip A, far + dim */
        float w = sc.y * 0.04 * sin(q.y * 70.0 + sp.w * T);              /* heat haze, line by line */
        float x1 = q.x + sp.x;
        float on1 = edge(q.y - (0.40 + 0.06 * sin(T * 2.0 * x1) + 0.03 * sin(T * 5.0 * x1)));
        c = mix(c, mix(vec3(0.10, 0.05, 0.24), V(1.0, vec2(x1 * 2.0, q.y * 2.0)) * 0.65, vid), on1);
        float x2 = q.x + sp.y + w;
        float on2 = edge(q.y - (0.56 + 0.07 * sin(T * 3.0 * x2) + 0.03 * sin(T * 7.0 * x2)));
        c = mix(c, mix(vec3(0.07, 0.10, 0.26), V(2.0, vec2(x2 * 1.6, q.y * 2.0)) * 0.85, vid), on2);
        float d = clamp((q.y - 0.76) / 0.24, 0.0, 1.0);                   /* 0 far .. 1 near */
        float u = (q.x - 0.5 + w) * (2.0 + d * 10.0) + sp.z;               /* the stripes widen as they come close */
        float st = sq(u + 0.25);
        vec3 drawn = mix(vec3(0.05, 0.02, 0.12), vec3(0.20, 0.08, 0.30), st);
        vec3 film = V(0.0, vec2(u * 0.5, d)) * mix(0.7, 1.0, st);          /* clip A, in perspective */
        c = mix(c, mix(drawn, film, vid) * (0.6 + 0.4 * d), edge(q.y - 0.76));
    } else if (sc.x < 2.5) {                         /* FLOOR: a Mode-7 checkerboard to the horizon */
        c = mix(vec3(0.01, 0.0, 0.04), vec3(0.16, 0.03, 0.20), q.y * 2.2);
        c = mix(c, V(2.0, vec2(q.x, q.y * 2.0)) * 0.4, vid);                /* the sky: clip C, dim */
        float z = 0.18 / max(q.y - 0.40, 0.01);
        float x = (q.x - 0.5) * z * 1.8;
        float a = sp.y * T, cs = cos(a), sn = sin(a);
        vec2 w = vec2(x * cs - z * sn, x * sn + z * cs);
        vec2 tile = vec2(w.x * 2.0, w.y * 2.0 + sp.x * 2.0);
        float k = xr(sq(tile.x * 0.5), sq(tile.y * 0.5));
        vec3 fl = mix(mix(DARK, LITE, k), mix(V(0.0, tile * 2.0), V(1.0, tile * 2.0), k), vid);
        c = mix(c, fl * clamp(1.4 - z * 0.25, 0.0, 1.0), edge(q.y - 0.42));
    } else if (sc.x < 3.5) {                         /* TUNNEL: flying down a checkered tube */
        float a = atan(p.y, p.x) / T + sp.y + sc.y * 0.08 / r;
        float d = 0.3 / r + sp.x;
        float k = xr(sq(d * 2.0), sq(a * 6.0));
        vec2 tc = vec2(a * 24.0, d * 8.0);
        c = mix(mix(DARK, LITE, k), mix(V(0.0, tc), V(1.0, tc), k), vid) * clamp(r * 2.2, 0.0, 1.0);
    } else if (sc.x < 4.5) {                         /* RINGS: two drifting ring sets = moire */
        float n = 18.0 + sc.y * 30.0;
        vec2 c1 = vec2(0.18 * sin(sp.x * T), 0.12 * cos(sp.x * T));
        float k = xr(sq(length(p - c1) * n - sp.y), sq(length(p + c1) * n));
        c = mix(mix(DARK, LITE, k), mix(V(0.0, q * 2.0), V(1.0, q * 2.0), k), vid);
    } else if (sc.x < 5.5) {                         /* SPIRAL: a turning hypnotic spiral */
        float arms = 2.0 + floor(sc.y * 6.0);
        float k = sq(atan(p.y, p.x) / T * arms + log(r) * 2.0 - sp.x);
        c = mix(mix(DARK, LITE, k), mix(V(0.0, q * 2.0), V(1.0, q * 2.0), k), vid) * clamp(r * 3.0, 0.2, 1.0);
    } else {                                         /* OP-ART: Riley-style bending stripes */
        float n = 16.0 + sc.y * 24.0;
        float x = q.x * n + 1.5 * sin(q.y * T * 2.0 + sp.x * T) * (0.5 + q.x);
        float k = sq(x);
        c = mix(mix(DARK, LITE, k), mix(V(0.0, q * 2.0), V(1.0, q * 2.0), k), vid);
    }
    c = mix(c, V(0.0, q), sc.w);                     /* OVERLAY: clip A flat over it all, faded */
    gl_FragColor = vec4(c.b, c.g, c.r, 1.0);
}
"""
# how fast each scene's four phases turn, in cycles per SPEED unit (orcflash's S SPEED is cycles/s):
# far ridge, near hills, ground stripes, haze | travel, turn (x DETAIL) | depth, twist | drift, rings |
# spin | wave. The stripe rates stay low: fast high-contrast stripes can trigger photosensitive fits.
SCENE_RATES = {"parallax": (0.03, 0.08, 0.5, 0.4), "floor": (1.0, 0.1, 0, 0), "tunnel": (1.0, 0.12, 0, 0),
               "rings": (0.12, 0.5, 0, 0), "spiral": (0.5, 0, 0, 0), "opart": (0.25, 0, 0, 0)}
SCENE_MAX_RATE = 2.0                     # cycles/s at most for any phase (the photosensitivity cap)


def scene_phases(name, speed, detail, t):
    """The four phases (each wrapped to 0-1) of scene `name` at time t, SPEED cycles/s, DETAIL 0-1."""
    rates = list(SCENE_RATES.get(name, (0, 0, 0, 0)))
    if name == "floor":
        rates[1] *= detail * 2                   # DETAIL = how much the floor turns
    return tuple((min(SCENE_MAX_RATE, abs(r * speed)) * (1 if r * speed >= 0 else -1) * t) % 1.0 for r in rates)


# Swap every SWAP_INTERVAL screen refreshes (60 Hz). 1 = every refresh: orcflash then draws every
# refresh and the swap paces it (60 fps; a frame costs ~6 ms of GPU). 2 was tried on 2026-11-02: this
# driver waits 2 refreshes from the CALL, not from the last swap, so frames came ~38 ms apart (26 fps).
# A one-word file overrides it for tuning by eye.
SWAP_INTERVAL = 1
SWAP_FILE = "/storage/orc/swap_interval"


def swap_interval(path=SWAP_FILE):
    try:
        n = int(open(path).read().strip())
        return n if 1 <= n <= 4 else SWAP_INTERVAL
    except (IOError, OSError, ValueError):
        return SWAP_INTERVAL


def line_uniforms(fine):
    """orcflash's FINE look (every, thick, fill) -> (every, thick, mode, (r, g, b) 0-1).
    mode 0 = no lines, 1 = a colour, 2 = the GPU's heaviest blur (fill "blur")."""
    if not fine:
        return (2.0, 0.0, 0.0, (0.0, 0.0, 0.0))
    every, thick, fill = fine
    if fill == "blur":
        return (float(every), float(thick), 2.0, (0.0, 0.0, 0.0))
    h = fill.lstrip("#")
    return (float(every), float(thick), 1.0,
            (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0))


def line_mask(every, thick, rows):
    """One byte per surface row, TOP row first: 255 where the thin lines look draws a line."""
    every, thick = int(every), int(thick)
    return b"".join(b"\xff" if every > 0 and y % every < thick else b"\x00" for y in range(rows))


def blur_levels(level):
    """Blur strength 0-BLUR_MAX -> (lo, hi, mix): show lo mixed toward hi by mix (levels 0..4,
    0 = sharp). None = no blur at all."""
    level = max(0.0, min(float(BLUR_MAX), float(level)))
    if level <= 0.001:
        return None
    hi = min(BLUR_MAX, int(-(-level // 1)))                          # ceil
    return (hi - 1, hi, level - (hi - 1))


TAU = 6.283185307179586


def fx_uniforms(fx, w, h=None):
    """The looks (orcflash builds fx each draw) -> the shader's numbers, all at time fx["t"].
    fx keys (all optional): "w" wobble (amp px, waves, cycles/s), "k" kaleidoscope (mirrors,
    turns/s), "r" RGB split px, "c" colour (hue degrees, saturation, invert 0-1, hue turns/s),
    "xf" crossfade progress 0-1 (or None), "z" zoom (0.2 = 20 % closer), "s" shake (dx, dy px).
    w, h = the picture's size in px. Phases are taken
    mod one turn HERE: the GPU's 16-bit maths can't hold a large time."""
    fx = fx or {}
    t = fx.get("t", 0.0)
    wb = fx.get("w")
    wob = (wb[0] / float(w), wb[1], (TAU * wb[2] * t) % TAU) if wb and wb[0] else (0.0, 0.0, 0.0)
    k = fx.get("k")
    kal = (float(k[0]), (TAU * k[1] * t) % TAU) if k and k[0] >= 2 else (0.0, 0.0)
    rgb = fx.get("r", 0.0) / float(w)
    c = fx.get("c")
    if c and (c[0] % 360 or c[1] != 1.0 or c[2] > 0 or c[3]):
        col = ((TAU * (c[0] / 360.0 + c[3] * t)) % TAU, float(c[1]), float(c[2]), 1.0)
    else:
        col = (0.0, 1.0, 0.0, 0.0)
    x = fx.get("xf")
    xf = (1.0, max(0.0, min(1.0, x))) if x is not None else (0.0, 1.0)
    sh = fx.get("s") or (0.0, 0.0)
    zs = (max(-0.9, float(fx.get("z", 0.0))), sh[0] / float(w), sh[1] / float(h or w * 9 // 16))
    return {"wob": wob, "kal": kal, "rgb": rgb, "col": col, "xf": xf, "zs": zs}


def bends(u):
    """True when a look moves WHERE the picture is read (kaleidoscope, wobble / warp, zoom, shake,
    RGB split): the screen pass then reads the picture smoothly (2026-11-02, owner: the staircase
    on moving edges). A picture nothing bends keeps its chunky pixels."""
    return bool(u["kal"][0] or u["wob"][0] or u["rgb"] or any(u["zs"]))


def blur_sizes(w, h):
    """The shrink sizes: level k = (w, h) halved k times (at least 1 px)."""
    return [(max(1, w >> k), max(1, h >> k)) for k in range(1, BLUR_MAX + 1)]


class Screen(object):
    """The GPU surface. offscreen=True draws into a pbuffer (tests: nothing on the screen changes)."""

    def __init__(self, w, h, out_w, out_h, offscreen=False, sysw=None):
        self.w, self.h, self.fd = w, h, None
        if not offscreen:
            self.fd = os.open("/dev/fb0", os.O_RDWR)
            v = bytearray(fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, b"\0" * 160))
            struct.pack_into("6I", v, 0, SW, SH, SW, SH * 2, 0, 0)   # two pages: the EGL swap chain
            fcntl.ioctl(self.fd, FBIOPUT_VSCREENINFO, bytes(v))
            sysw("free_scale", "0")
            sysw("free_scale_axis", "0 0 %d %d" % (SW - 1, SH - 1))
            sysw("window_axis", "0 0 %d %d" % (out_w - 1, out_h - 1))
            sysw("free_scale", "0x10001")
        egl = self.egl = C.CDLL("libEGL.so.1")
        gl = self.gl = C.CDLL("libGLESv2.so.2")
        for f in ("eglGetDisplay", "eglCreateContext", "eglCreateWindowSurface", "eglCreatePbufferSurface"):
            getattr(egl, f).restype = C.c_void_p
        VP = C.c_void_p
        d = self.d = egl.eglGetDisplay(VP(0))
        if not egl.eglInitialize(VP(d), None, None):
            raise IOError("eglInitialize failed")
        kind = 1 if offscreen else 4                                 # EGL_PBUFFER_BIT / EGL_WINDOW_BIT
        attrs = (C.c_int * 13)(0x3033, kind, 0x3040, 4, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3038)
        cfg, n = VP(), C.c_int()
        if not egl.eglChooseConfig(VP(d), attrs, C.byref(cfg), 1, C.byref(n)) or n.value < 1:
            raise IOError("no EGL config")
        if offscreen:
            s = egl.eglCreatePbufferSurface(VP(d), cfg, (C.c_int * 5)(0x3057, SW, 0x3056, SH, 0x3038))
        else:
            class FBW(C.Structure):
                _fields_ = [("width", C.c_ushort), ("height", C.c_ushort)]
            self._win = FBW(SW, SH)                                  # Mali's fbdev native window
            s = egl.eglCreateWindowSurface(VP(d), cfg, C.byref(self._win), None)
        if not s:
            raise IOError("no EGL surface (%#x)" % egl.eglGetError())
        self.s = s
        ctx = egl.eglCreateContext(VP(d), cfg, VP(0), (C.c_int * 3)(0x3098, 2, 0x3038))
        if not ctx or not egl.eglMakeCurrent(VP(d), VP(s), VP(s), VP(ctx)):
            raise IOError("no GL context (%#x)" % egl.eglGetError())
        egl.eglSwapInterval(VP(d), swap_interval())                  # the swaps keep the beat (see SWAP_FILE)
        gl.glPixelStorei(0x0CF5, 1)                                  # 1-byte rows (the 1-wide mask)
        self._quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
        # the screen program
        self.prog = self._program(VS, FS)
        gl.glUseProgram(self.prog)
        self.tex = [self._texture(w, h, GL_NEAREST), self._texture(w, h, GL_NEAREST)]   # picture, crossfade's outgoing
        self.mtex, self.mlook = self._texture(1, SH, GL_NEAREST, fmt=0x1909), None   # GL_LUMINANCE
        self.btex, self.bmask_now = self._texture(w, h, GL_NEAREST, fmt=0x1909), None  # which pixels blur
        for name, unit in (("pic", 0), ("prev", 1), ("mask", 2), ("blo", 3), ("bhi", 4), ("bmask", 5), ("b4", 6), ("fr", 7)):
            gl.glUniform1i(gl.glGetUniformLocation(self.prog, name), unit)
        self.u_fron = gl.glGetUniformLocation(self.prog, "fron")
        # FRAME: its own target at the surface's size, drawn only when the frame changes
        self.frame_tgt, self.frame_key, self.p_frames = self._target(SW, SH), None, {}
        self.fptex = self._texture(SW, SH, GL_LINEAR, fmt=0x1909)          # your frame picture (GL_LUMINANCE)
        self.fpic_now = None
        self.frames_drawn = 0                                                # (tests)
        self.u_ln = gl.glGetUniformLocation(self.prog, "ln")
        self.u_col = gl.glGetUniformLocation(self.prog, "lncol")
        self.u_bl = gl.glGetUniformLocation(self.prog, "bl")
        self.u_bm = gl.glGetUniformLocation(self.prog, "bm")
        self.u_fx = dict((n, gl.glGetUniformLocation(self.prog, n)) for n in ("wob", "kal", "rgb", "col", "xf", "zs"))
        # the blur: shrink textures D1..D4, grow-back textures per size, results at D1's size
        self.p_down, self.p_up = self._program(VS_FBO, FS_DOWN), self._program(VS_FBO, FS_UP)
        self.p_scene = self._program(VS_FBO, FS_SCENE)
        self.u_sp = gl.glGetUniformLocation(self.p_scene, "sp")
        self.u_sc = gl.glGetUniformLocation(self.p_scene, "sc")
        gl.glUseProgram(self.p_scene)
        for name, unit in (("va", 0), ("vb", 1), ("vc", 2)):
            gl.glUniform1i(gl.glGetUniformLocation(self.p_scene, name), unit)
        # the scene's three clips: smooth (a perspective floor of chunky pixels shimmers). TWO textures
        # each, used in turn: a new frame goes into the one the GPU is NOT reading (writing a texture a
        # frame still in flight makes the driver wait), and only when the clip's frame changed. Every
        # frame re-sending all three made the video scenes 34-36 ms a frame, ~20 fps, a stutter (2026-11-02).
        self.stex = [[self._texture(w, h, GL_LINEAR), self._texture(w, h, GL_LINEAR)] for i in range(3)]
        self.scur, self.skey = [0, 0, 0], [None, None, None]
        self.uploads = 0                                                     # scene clip frames sent (tests)
        self.stages = ()                         # the last draw's stage times in ms (picture, outgoing, blur, screen, swap)
        self._etc = {}                                                       # texture id -> holds an Etc frame now
        # texture id -> the frame OBJECT it holds: the same object again (a redraw between clip frames:
        # the screen draws ~35 a second, a clip changes 15) is not sent again. Frames are immutable
        # strings, so the same object = the same picture. (2026-11-02: crossfades into scenes ran late.)
        self._held, self.skipped = {}, 0
        self.last_smooth = False                                             # did the last frame read smoothly (tests)
        gl.glUseProgram(self.prog)
        self.p_weave = self._program(VS_FBO, FS_WEAVE)
        gl.glUseProgram(self.p_weave)
        for name, unit in (("src", 0), ("other", 1), ("rows", 2)):
            gl.glUniform1i(gl.glGetUniformLocation(self.p_weave, name), unit)
        self.wtex = self._texture(w, h, GL_NEAREST)                         # the woven clip's frame
        self.wrtex, self.wrows_now = self._texture(1, h, GL_NEAREST, fmt=0x1909), None   # which rows
        self.weave_tgt = self._target(SW, SH)                               # (a compressed clip's full size)
        self.p_masks = {}                                                   # (shape, fill) -> (program, mk, mc)
        self.ktex = self._texture(w, h, GL_LINEAR)                          # the clip seen through the mask
        self.mask_tgt = self._target(SW, SH)
        gl.glBindTexture(GL_TEXTURE_2D, self.weave_tgt[0])
        for p_ in (0x2801, 0x2800):
            gl.glTexParameteri(GL_TEXTURE_2D, p_, GL_NEAREST)
        gl.glUseProgram(self.prog)
        # scenes at the picture's size, read SMOOTHLY when the screen scales them up: with the soft
        # edges that is no staircase, at ~6 ms. At the surface size (640x360) they cost 11-20 ms, too much
        # for the steady 30 fps (measured 2026-11-02).
        self.scene_tgt = self._target(w, h)
        self.u_hp = {}
        for pr in (self.p_down, self.p_up):
            gl.glUseProgram(pr)
            gl.glUniform1i(gl.glGetUniformLocation(pr, "src"), 0)
            self.u_hp[pr] = gl.glGetUniformLocation(pr, "hp")
        gl.glUseProgram(self.prog)
        self.sizes = blur_sizes(w, h)
        self.down = [self._target(*sz) for sz in self.sizes]                 # level k-1 index
        self.upt = [self._target(*sz) for sz in self.sizes[:-1]]             # grow-back steps
        self.res = [self._target(*self.sizes[0]) for i in range(3)]          # blo, bhi, heaviest
        self._blur_frame = None

    def _texture(self, w, h, filt, fmt=GL_RGBA):
        gl = self.gl
        t = C.c_uint()
        gl.glGenTextures(1, C.byref(t))
        gl.glBindTexture(GL_TEXTURE_2D, t)
        for p_, v_ in ((0x2801, filt), (0x2800, filt), (0x2802, GL_CLAMP), (0x2803, GL_CLAMP)):
            gl.glTexParameteri(GL_TEXTURE_2D, p_, v_)
        gl.glTexImage2D(GL_TEXTURE_2D, 0, fmt, w, h, 0, fmt, GL_UNSIGNED_BYTE, None)
        return t

    def _target(self, w, h):
        """A texture the GPU can draw into: (texture, framebuffer, w, h), linear-filtered."""
        gl = self.gl
        t = self._texture(w, h, GL_LINEAR)
        fb = C.c_uint()
        gl.glGenFramebuffers(1, C.byref(fb))
        gl.glBindFramebuffer(GL_FRAMEBUFFER, fb)
        gl.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0)
        st = gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, 0)
        if st != GL_FRAMEBUFFER_COMPLETE:
            raise IOError("blur target %dx%d incomplete (%#x)" % (w, h, st))
        return (t, fb, w, h)

    def _program(self, vs, fs):
        gl = self.gl
        pr = gl.glCreateProgram()
        for kind, src in ((0x8B31, vs), (0x8B30, fs)):
            sh = gl.glCreateShader(kind)
            cs = C.c_char_p(src)
            gl.glShaderSource(sh, 1, C.byref(cs), None)
            gl.glCompileShader(sh)
            ok = C.c_int()
            gl.glGetShaderiv(sh, 0x8B81, C.byref(ok))
            if not ok.value:
                buf = C.create_string_buffer(2000)
                gl.glGetShaderInfoLog(sh, 2000, None, buf)
                raise IOError("shader: " + buf.value)
            gl.glAttachShader(pr, sh)
        gl.glBindAttribLocation(pr, 0, "p")
        gl.glLinkProgram(pr)
        ok = C.c_int()
        gl.glGetProgramiv(pr, 0x8B82, C.byref(ok))
        if not ok.value:
            raise IOError("shader program did not link")
        return pr

    def _bind(self, unit, tex):
        self.gl.glActiveTexture(GL_TEXTURE0 + unit)
        self.gl.glBindTexture(GL_TEXTURE_2D, tex)

    def _put(self, unit, tex, frame):
        """Bind tex on unit and fill it with frame: raw BGRA (w x h) or an Etc frame (SW x SH). A
        texture changes shape when the kind changes (an ETC1 texture can only be replaced whole)."""
        gl = self.gl
        self._bind(unit, tex)
        if self._held.get(tex.value) is frame:                       # it holds this very frame already
            self.skipped += 1
            return
        etc = isinstance(frame, Etc)
        if etc:
            gl.glCompressedTexImage2D(GL_TEXTURE_2D, 0, GL_ETC1_RGB8, Etc.W, Etc.H, 0, len(frame), C.c_char_p(frame))
        else:
            if self._etc.get(tex.value):                             # back to a raw frame: make it RGBA again
                gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.w, self.h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
            gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.w, self.h, GL_RGBA, GL_UNSIGNED_BYTE, C.c_char_p(frame))
        self._etc[tex.value] = etc
        self._held[tex.value] = frame

    def snapshot(self):
        """Copy the scene's last frame into the crossfade's outgoing picture (tex[1]); pass SNAP as
        draw()'s prev to fade out of it. A clip taking over from a scene used to fade out of the
        black picture the scene is drawn over: a cut to black, then a fade in (owner, 2026-11-02)."""
        gl = self.gl
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.scene_tgt[1])
        self._bind(1, self.tex[1])
        gl.glCopyTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 0, 0, self.w, self.h, 0)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, 0)
        self._held[self.tex[1].value], self._etc[self.tex[1].value] = SNAP, False

    def _upload(self, i, data):
        self._put(i, self.tex[i], data)
        for p_ in (0x2801, 0x2800):                   # chunky by default: a frame that was read smoothly
            self.gl.glTexParameteri(GL_TEXTURE_2D, p_, GL_NEAREST)   # must not leave the next one blended

    def _pass(self, prog, src_tex, src_w, src_h, dst):
        """One blur pass: draw src (w x h) into the target dst with prog."""
        gl = self.gl
        gl.glBindFramebuffer(GL_FRAMEBUFFER, dst[1])
        gl.glViewport(0, 0, dst[2], dst[3])
        gl.glUseProgram(prog)
        gl.glUniform2f(self.u_hp[prog], C.c_float(0.5 / src_w), C.c_float(0.5 / src_h))
        self._bind(0, src_tex)
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(5, 0, 4)

    def _blurred(self, k, out):
        """Blur level k (1..4) of the current picture, into result target `out`; returns its texture.
        Needs the shrinks D1..Dk made (see _shrink)."""
        if k == 1:
            return self.down[0][0]
        src = self.down[k - 1]
        for j in range(k - 2, 0, -1):                                # grow back to D2's size ...
            self._pass(self.p_up, src[0], src[2], src[3], self.upt[j])
            src = self.upt[j]
        self._pass(self.p_up, src[0], src[2], src[3], out)           # ... and the last step to D1's
        return out[0]

    def _scene(self, sc, t):
        """Draw scene sc = (name, speed, detail[, video 0-1, frames]) at time t into the scene target; its
        texture. frames = up to three w x h BGRA frames, each plain or (key, bytes): a key the layer
        already shows is not sent again. They fill the layers / tiles by `video` (0 = the drawn colours
        only, and nothing is uploaded). sc[5] = OVERLAY 0-1: the first clip laid flat over the whole
        scene, faded in by that much (it needs the frames too)."""
        gl = self.gl
        name, speed, detail = sc[:3]
        vid = max(0.0, min(1.0, sc[3])) if len(sc) > 3 else 0.0
        frames = sc[4] if len(sc) > 4 else None
        ov = max(0.0, min(1.0, sc[5])) if len(sc) > 5 else 0.0
        if not frames:
            vid = ov = 0.0
        ph = scene_phases(name, speed, detail, t)
        if vid > 0 or ov > 0:
            for i in range(3):
                fr = frames[min(i, len(frames) - 1)]
                key, data = fr if isinstance(fr, tuple) else (None, fr)
                if key is None or key != self.skey[i]:
                    n = 1 - self.scur[i]                             # the copy the GPU isn't reading
                    self._put(i, self.stex[i][n], data)
                    self.scur[i], self.skey[i] = n, key
                    self.uploads += 1
                self._bind(i, self.stex[i][self.scur[i]])
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.scene_tgt[1])
        gl.glViewport(0, 0, self.w, self.h)
        gl.glUseProgram(self.p_scene)
        gl.glUniform4f(self.u_sp, *[C.c_float(x) for x in ph])
        gl.glUniform4f(self.u_sc, C.c_float(SCENES.index(name) + 1), C.c_float(max(0.0, min(1.0, detail))),
                       C.c_float(vid), C.c_float(ov))
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(5, 0, 4)
        return self.scene_tgt[0]

    def _weave(self, src0, frame, rows):
        """The weave pass: src0 with `rows` (h bytes, 255 = theirs) from `frame`, into the weave target."""
        gl = self.gl
        self._put(1, self.wtex, frame)
        self._bind(2, self.wrtex)
        if rows is not self.wrows_now:                               # (orcflash caches it: only on a change)
            gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 1, self.h, 0x1909, GL_UNSIGNED_BYTE, C.c_char_p(rows))
            self.wrows_now = rows
        self._bind(0, src0)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.weave_tgt[1])
        gl.glViewport(0, 0, self.weave_tgt[2], self.weave_tgt[3])
        gl.glUseProgram(self.p_weave)
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(5, 0, 4)
        return self.weave_tgt[0]

    def _frame(self, fr):
        """Draw the FRAME texture for fr = (shape 1-5, size, cx, cy, soft, invert, picture or None)
        when it changed. picture = SW x SH grey bytes (white = light)."""
        shape, size, cx, cy, soft, inv, pic = fr
        if shape == 5 and pic is None:
            shape = 1                                                # no picture yet: a circle
        key = (shape, round(size, 4), round(cx, 4), round(cy, 4), round(soft, 4), inv, id(pic) if shape == 5 else None)
        if key == self.frame_key:
            return
        gl = self.gl
        if shape == 5 and pic is not self.fpic_now:
            self._bind(0, self.fptex)
            gl.glTexImage2D(GL_TEXTURE_2D, 0, 0x1909, SW, SH, 0, 0x1909, GL_UNSIGNED_BYTE, C.c_char_p(pic))
            self.fpic_now = pic
        if shape not in self.p_frames:
            pr = self._program(VS_FBO, frame_fs(shape))
            gl.glUseProgram(pr)
            gl.glUniform1i(gl.glGetUniformLocation(pr, "fp"), 0)
            self.p_frames[shape] = (pr, gl.glGetUniformLocation(pr, "fc"), gl.glGetUniformLocation(pr, "fi"))
        pr, u_fc, u_fi = self.p_frames[shape]
        self._bind(0, self.fptex)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.frame_tgt[1])
        gl.glViewport(0, 0, SW, SH)
        gl.glUseProgram(pr)
        gl.glUniform4f(u_fc, C.c_float(cx), C.c_float(cy), C.c_float(size), C.c_float(soft))
        gl.glUniform1f(u_fi, C.c_float(1.0 if inv else 0.0))
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(5, 0, 4)
        self.frame_key = key
        self.frames_drawn += 1

    def _mask(self, src0, mk):
        """The mask pass: src0 with mk's second picture through its shape, into the mask target.
        mk = (shape 1-7, size, fill 0-3, phase, cx, cy, clip frame or None)."""
        gl = self.gl
        shape, size, fill, ph, cx, cy, frame = mk
        if fill == 0:
            if frame is None:
                fill = 1                                             # no clip to show: black
            else:
                self._put(1, self.ktex, frame)
        self._bind(1, self.ktex)
        self._bind(0, src0)
        key = (int(shape), int(fill))
        if key not in self.p_masks:                                  # built the first time (a few ms, once)
            pr = self._program(VS_FBO, mask_fs(*key))
            gl.glUseProgram(pr)
            for name, unit in (("src", 0), ("other", 1)):
                gl.glUniform1i(gl.glGetUniformLocation(pr, name), unit)
            self.p_masks[key] = (pr, gl.glGetUniformLocation(pr, "mk"), gl.glGetUniformLocation(pr, "mc"))
        pr, u_mk, u_mc = self.p_masks[key]
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.mask_tgt[1])
        gl.glViewport(0, 0, self.mask_tgt[2], self.mask_tgt[3])
        gl.glUseProgram(pr)
        gl.glUniform4f(u_mk, C.c_float(shape), C.c_float(max(0.0, min(1.5, size))), C.c_float(fill), C.c_float(ph % 1.0))
        gl.glUniform2f(u_mc, C.c_float(cx), C.c_float(cy))
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(5, 0, 4)
        return self.mask_tgt[0]

    def _shrink(self, upto, src0, size=None):
        """Make D1..D<upto> from the picture src0 (sampled smoothly for this); size = its (w, h)."""
        gl = self.gl
        self._bind(0, src0)
        gl.glTexParameteri(GL_TEXTURE_2D, 0x2801, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, 0x2800, GL_LINEAR)
        src = (src0, None) + (size or (self.w, self.h))
        for k in range(upto):
            self._pass(self.p_down, src[0], src[2], src[3], self.down[k])
            src = self.down[k]
        self._bind(0, src0)
        gl.glTexParameteri(GL_TEXTURE_2D, 0x2801, GL_NEAREST)        # sharp again for the screen
        gl.glTexParameteri(GL_TEXTURE_2D, 0x2800, GL_NEAREST)

    def draw(self, pic, fine=None, blur=0.0, bmask=None, fx=None, prev=None):
        """Put the w x h BGRA picture on the screen, blurred by `blur` (0 sharp .. 4), with the thin
        lines look `fine` (orcflash FINE: (every, thick, fill) or None; a "blur" fill = the heaviest
        blur). bmask (w x h bytes, 255 = blur this pixel, orcflash.blur_mask) swaps those pixels
        for the heaviest blur, before the thin lines. fx = the looks (fx_uniforms); prev = the
        outgoing picture while a crossfade (fx["xf"]) runs. fx["scene"] = (name, speed, detail): the
        GPU draws that scene as the picture instead (pic is then ignored). fx["weave"] = (frame, rows):
        those rows (orcflash.weave_rows) come from the other clip's frame."""
        gl = self.gl
        tt = [time.time()]                                           # stage times (late frames say them)
        sc = (fx or {}).get("scene")
        size = (self.w, self.h)                                      # the picture texture's (for the blur)
        if sc and sc[0] in SCENES:
            src0 = self._scene(sc, (fx or {}).get("t", 0.0))
        else:
            self._upload(0, pic)
            src0 = self.tex[0]
            if isinstance(pic, Etc):
                size = (Etc.W, Etc.H)
            wv = (fx or {}).get("weave")
            if wv:
                src0 = self._weave(src0, wv[0], wv[1])
                size = self.weave_tgt[2:]
        mk = (fx or {}).get("mask")
        if mk:                                                       # (a scene can be masked too)
            src0 = self._mask(src0, mk)
            size = self.mask_tgt[2:]
        tt.append(time.time())                                       # 1: the picture (upload / scene / weave / mask)
        u = fx_uniforms(fx, self.w, self.h)
        if prev is None:
            u["xf"] = (0.0, 1.0)
        else:
            self._upload(1, prev)
        tt.append(time.time())                                       # 2: the outgoing picture
        lv = blur_levels(blur)
        line_blur = bool(fine) and fine[2] == "blur"
        need = max(lv[1] if lv else 0, BLUR_MAX if line_blur or bmask else 0)
        if need:
            self._shrink(need, src0, size)
        lo = hi = None
        if lv:
            lo = src0 if lv[0] == 0 else self._blurred(lv[0], self.res[0])
            hi = self._blurred(lv[1], self.res[1])
        b4 = self._blurred(BLUR_MAX, self.res[2]) if line_blur or bmask else None
        if b4 is not None:
            self._bind(6, b4)
        if bmask:
            if bmask is not self.bmask_now:                          # a new mask (orcflash caches it)
                self._bind(5, self.btex)
                gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.w, self.h, 0x1909, GL_UNSIGNED_BYTE,
                                   C.c_char_p(bmask))
                self.bmask_now = bmask
            self._bind(5, self.btex)
        frm = (fx or {}).get("frame")
        if frm:
            self._frame(frm)
        tt.append(time.time())                                       # 3: the blur (+ a changed frame)
        gl.glUseProgram(self.prog)
        gl.glUniform1f(self.u_fron, C.c_float(1.0 if frm else 0.0))
        if frm:
            self._bind(7, self.frame_tgt[0])
        gl.glUniform1f(self.u_bm, C.c_float(1.0 if bmask else 0.0))
        f = C.c_float
        gl.glUniform3f(self.u_fx["wob"], *[f(x) for x in u["wob"]])
        gl.glUniform2f(self.u_fx["kal"], *[f(x) for x in u["kal"]])
        gl.glUniform1f(self.u_fx["rgb"], f(u["rgb"]))
        gl.glUniform4f(self.u_fx["col"], *[f(x) for x in u["col"]])
        gl.glUniform2f(self.u_fx["xf"], *[f(x) for x in u["xf"]])
        gl.glUniform3f(self.u_fx["zs"], *[f(x) for x in u["zs"]])
        self._bind(1, self.tex[1])
        gl.glBindFramebuffer(GL_FRAMEBUFFER, 0)
        gl.glViewport(0, 0, SW, SH)
        gl.glUseProgram(self.prog)
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(0)
        self._bind(0, src0)
        if lv:
            self._bind(3, lo)
            self._bind(4, hi)
        gl.glUniform2f(self.u_bl, C.c_float(1.0 if lv else 0.0), C.c_float(lv[2] if lv else 0.0))
        every, thick, mode, col = line_uniforms(fine)
        if mode and (every, thick) != self.mlook:                    # a new look: rebuild the row mask
            self._bind(2, self.mtex)
            gl.glTexImage2D(GL_TEXTURE_2D, 0, 0x1909, 1, SH, 0, 0x1909, GL_UNSIGNED_BYTE,
                            C.c_char_p(line_mask(every, thick, SH)))   # GL_LUMINANCE
            self.mlook = (every, thick)
        self._bind(2, self.mtex)
        gl.glUniform4f(self.u_ln, C.c_float(every), C.c_float(thick), C.c_float(mode), C.c_float(SH))
        gl.glUniform3f(self.u_col, *[C.c_float(x) for x in col])
        # read the picture SMOOTHLY when a look bends it, or when it is a scene (soft edges, scaled up),
        # else its big pixels turn every moving diagonal into a staircase; a clip nothing bends keeps
        # its chunky pixels. Set every frame (the blur passes leave the filter as they like).
        smooth = bends(u) or bool(sc and sc[0] in SCENES)
        self._bind(0, src0)
        for p_ in (0x2801, 0x2800):
            gl.glTexParameteri(GL_TEXTURE_2D, p_, GL_LINEAR if smooth else GL_NEAREST)
        gl.glDrawArrays(5, 0, 4)
        self.last_smooth = smooth
        tt.append(time.time())                                       # 4: the screen pass
        self.egl.eglSwapBuffers(C.c_void_p(self.d), C.c_void_p(self.s))
        tt.append(time.time())                                       # 5: the swap (waits for the GPU + the refresh)
        self.stages = tuple(int(round((tt[i + 1] - tt[i]) * 1000)) for i in range(len(tt) - 1))

    def read(self):
        """The surface as SH rows of SW RGBA pixels, TOP row first (tests; slow: ~30 ms)."""
        buf = C.create_string_buffer(SW * SH * 4)
        self.gl.glReadPixels(0, 0, SW, SH, GL_RGBA, GL_UNSIGNED_BYTE, buf)
        raw = buf.raw
        return b"".join(raw[(SH - 1 - y) * SW * 4:(SH - y) * SW * 4] for y in range(SH))
