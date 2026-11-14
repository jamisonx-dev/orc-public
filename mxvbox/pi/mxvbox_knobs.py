"""The mxvbox module's knobs: CCs on whatever channel /matrix binds "mxvbox" to.

Pure data, no side effects on import: the conductor (conductor.py) applies them, the
/cc page (orc1/cc_surfaces.py) lists them, the BOX tab on /visuals edits them.
The defaults reproduce the look owner signed off on 2026-10-29.

Notes on the same channel: note N cues clip N (mod the clip count) for the next clip bar.
"""

# cc: (short name, default 0-127, what it does)
KNOBS = {
    7:  ("SHOW",        127, "screen brightness: 0 = black, 127 = full; moves fade over ~0.8 s (the box's chip does it)"),
    20: ("HIT FADE",     35, "how long a hit takes to fade back: 0.05 s .. 1.5 s (35 = 0.45 s)"),
    21: ("KICK STYLE",   10, "the kick's reaction, in tenths: 0-12 blur, 13-25 RGB split, 26-38 invert flash, 39-51 zoom punch, 52-63 warp, 64-76 shudder, 77-89 grey (the colour drains), 90-102 hue jump, 103-115 off, 116-127 kaleidoscope snap"),
    22: ("SNARE STYLE",  60, "same choices as KICK STYLE (60 = warp)"),
    23: ("LINES",        30, "0-25 off, 26-51 fine scanlines (1 row in 4), 52-76 every other row, 77-102 1 row in 3, 103-127 blinds (2 rows in 6); with LINES FILL blurred: every other row, 1 in 3, 1 in 4, blinds; a row = half a picture row (3 screen px at 1080p)"),
    24: ("LINES FILL",    0, "0-63 black lines, 64-127 the blurred picture (lines made of the picture, softer)"),
    25: ("CLIPS",        96, "how many of every 4 bars are clips (the rest are patterns): 0 = none .. 127 = all (96 = 3)"),
    26: ("WOBBLE",       64, "how hard the beat-timed ripple / bend is: 0 = off, 64 = as tuned, 127 = double"),
    27: ("WEAVE",        64, "weave bars: share of rows from a second clip, 0 = no weaving .. 127 = all (64 = every other)"),
    28: ("HIT SIZE",     64, "how big the hit reactions are: blur, RGB split, zoom, shake, warp, hue jump, how far the colour drains or inverts (64 = as tuned)"),
    29: ("CHANGE EVERY",  0, "bars between picture changes: 0-31 every bar, 32-63 2, 64-95 4, 96-127 8"),
    30: ("POOL",          0, "which clips: 0 = everything (the whole library, shuffled), N = only pool N (pools are made on the BOX tab)"),
    # the GPU looks (2026-11-02): drawn by the box's GPU, free; every default = off / as filmed
    31: ("KALEIDO",       0, "kaleidoscope: 0-25 off, 26-51 3 mirrors, 52-76 4, 77-102 6, 103-127 8"),
    32: ("SPIN",          0, "the kaleidoscope turning: 0 = still .. 127 = one turn a bar"),
    33: ("RGB SPLIT",     0, "red and blue pulled apart: 0 = off .. 64 = 8 px .. 127 = 32 px (a tenth of the picture), on a curve: fine at the low end"),
    34: ("HUE",           0, "turn the colours round the colour wheel: 0 = as filmed .. 127 = almost a full turn"),
    35: ("SATURATION",   64, "0 = grey, 64 = as filmed, 127 = doubled"),
    36: ("INVERT",        0, "0 = as filmed .. 127 = the negative"),
    37: ("HUE CYCLE",     0, "the colours drifting round by themselves: 0 = still .. 127 = one turn a bar"),
    38: ("CROSSFADE",     0, "clip changes dissolve over 0 = a hard cut .. 127 = 2 s"),
    # SCENES (2026-11-02): pictures the box's GPU draws itself, shown in the PATTERN bars
    39: ("SCENE",         0, "the pattern bars show, in sevenths: 0-18 the patterns (no scene), 19-36 parallax, 37-54 floor, 55-73 tunnel, 74-91 rings, 92-109 spiral, 110-127 op-art"),
    40: ("SCENE SPEED",  64, "how fast the scene moves: 0 = still, 64 = one cycle a bar (follows the tempo), 127 = two"),
    41: ("SCENE DETAIL", 64, "per scene: parallax heat haze / floor turning / tunnel twist / ring density / spiral arms / stripe count"),
    43: ("SCENE CYCLE",   0, "move on to the next scene by itself: 0-25 off, 26-51 every bar, 52-76 every 2 bars, 77-102 every 4, 103-127 every 8 (each new scene picks new clips)"),
    42: ("SCENE VIDEO",   0, "fill the scene with your clips: 0 = drawn colours, 127 = all video (three clips: parallax layers, floor / tunnel tiles, the two halves of rings / spiral / op-art)"),
    44: ("SCENE OVERLAY", 0, "one of your clips laid FLAT over the whole scene, faded: 0 = off .. 127 = only the clip (calms the busy floor / tunnel; 2026-11-02)"),
    # MASK / PEEPHOLE (2026-11-02): a second picture shows THROUGH a shape, on any picture
    45: ("MASK",          0, "the hole's shape, in eighths: 0-15 off, then circle, diamond, square, stripes, rings, bright parts (key), dark parts (key)"),
    46: ("MASK SIZE",    64, "how big the hole is (for the bright / dark keys: how much of the picture keys through)"),
    47: ("MASK FILL",     0, "what shows through, in quarters: 0-31 another clip, 32-63 black, 64-95 the picture's negative, 96-127 the picture mirrored"),
    48: ("MASK MOVE",     0, "the hole drifting round the screen (stripes slide, rings ripple): 0 = still .. 127 = one turn a bar"),
    49: ("MASK PULSE",    0, "the hole breathing in and out on the beat: 0 = steady .. 127 = wide"),
    50: ("MASK HIT",      0, "the kick throws the hole open: 0 = no .. 127 = wide (it closes again over HIT FADE)"),
    # FRAME / PEEPHOLE (2026-11-02): for projecting -- the light only inside a shape (or blocked
    # inside it), the very last step, so it stays put on the wall while the picture moves inside
    51: ("FRAME",         0, "the projected shape, in sixths: 0-21 off (the whole rectangle), then circle, ellipse, diamond, rounded square, your picture"),
    52: ("FRAME SIZE",   80, "how big the lit shape is (your picture: 64 = fills the screen)"),
    53: ("FRAME X",      64, "where it sits, left .. right"),
    54: ("FRAME Y",      64, "where it sits, top .. bottom"),
    55: ("FRAME SOFT",   12, "the edge: 0 = a hard cut-out .. 127 = a wide soft spotlight"),
    56: ("FRAME INVERT",  0, "0-63 = light INSIDE the shape (spotlight), 64-127 = the shape blocked out (dark), the rest lit"),
    57: ("FRAME PICTURE", 0, "which of your 4 frame pictures (upload them on the BOX tab), in quarters"),
    # LISTEN (2026-11-02): the box reacts to what the visuals' LISTEN tab hears (its band, SENS, GATE),
    # even with the visuals kiosk off. Same reactions as KICK / SNARE STYLE.
    58: ("LISTEN STYLE",   109, "the reaction to a LISTEN beat (the shapes' band), like KICK STYLE; 103-115 = off"),
    # ROTATE (2026-11-02, owner: "more control over ... what effects play"): the box picks which
    # looks play, bar by bar, from the ones allowed below; a look you set yourself always wins
    61: ("ROTATE",        0, "a new pick of looks every 0-25 never (off), 26-51 bar, 52-76 2 bars, 77-102 4 bars, 103-127 8 bars"),
    62: ("ROTATE HOW MANY", 0, "how many looks at once, in thirds: 1, 2, 3"),
    63: ("ROTATE KALEIDO", 127, "may the rotation use the kaleidoscope (0-63 no, 64-127 yes)"),
    # (71-76 were 64-69 until 2026-11-03: those are the sustain / pedal CCs, so a pedal on this channel
    # flipped them. owner: "move them". The conductor moves saved values across once: MOVED_CCS.)
    71: ("ROTATE RGB",    127, "...the RGB split"),
    72: ("ROTATE HUE",    127, "...a hue turn"),
    73: ("ROTATE INVERT", 127, "...the negative"),
    74: ("ROTATE WOBBLE", 127, "...the wobble"),
    75: ("ROTATE MASK",   127, "...a mask (a hole with another clip in it)"),
    76: ("ROTATE HITS",     0, "0-63 = KICK / SNARE STYLE stay as set; 64-127 = they change with each pick too"),
    60: ("SCENE POOL",    0, "which clips the SCENES, SCENE OVERLAY and MASK use: 0 = the same as POOL, N = pool N (2026-11-02)"),
    59: ("LISTEN 2 STYLE", 109, "the reaction to the background's band (LISTEN's BACKGROUND band, or band 1 when it has none); 103-115 = off"),
    # WHEN STOPPED (2026-11-03, owner: "free-running with whatever it last was instructed ... should
    # prob be a toggle"): also what happens when the ORC's beat feed goes quiet (the brain down)
    70: ("WHEN STOPPED",  0, "when the ORC stops, in thirds: 0-42 BLACK (as before), 43-85 HOLD (the last picture and looks stay; LISTEN hits show), 86-127 FREE-RUN (clips, scenes and rotation keep changing at the last tempo)"),
}
WHEN_STOPPED = ("black", "hold", "free")                     # WHEN STOPPED, in thirds
ROTATE_ALLOW_CCS = (63, 71, 72, 73, 74, 75)                  # may ROTATE use kaleido / rgb / hue / invert / wobble / mask
ROTATE_HITS_CC = 76
MOVED_CCS = {64: 71, 65: 72, 66: 73, 67: 74, 68: 75, 69: 76}   # old CC -> new (2026-11-03), for saved values
FRAME_SHAPES = (None, "circle", "ellipse", "diamond", "rounded", "picture")   # FRAME, in sixths
MASK_SHAPES = (None, "circle", "diamond", "square", "stripes", "rings", "bright", "dark")   # MASK, in eighths
MASK_FILLS = ("clip", "black", "negative", "mirror")          # MASK FILL, in quarters (gpu.MASK_FILLS)


def pick(v, n):
    """0-127 -> 0..n-1 (an n-way knob)."""
    return max(0, min(n - 1, int(v) * n // 128))
SCENE_NAMES = (None, "parallax", "floor", "tunnel", "rings", "spiral", "opart")   # SCENE, in sevenths
SCENE_CYCLE_BARS = (0, 1, 2, 4, 8)                         # SCENE CYCLE, per fifth (0 = off)


def scene_of(v):
    """0-127 -> the SCENE_NAMES entry (sevenths)."""
    return SCENE_NAMES[max(0, min(len(SCENE_NAMES) - 1, int(v) * len(SCENE_NAMES) // 128))]
KALEIDO_MIRRORS = (0, 3, 4, 6, 8)                          # KALEIDO, per fifth

# KICK / SNARE STYLE: the hit REACTIONS, in knob order (tenths of 0-127), all drawn and faded by the
# box's GPU. Slots 2 / 4 / 6 were the CPU-drawn mirror border / flipped band / deep colour until
# 2026-11-02 (owner: "those cpu ones can go"); new GPU reactions took their places, so blur, off
# and every other saved value still mean what they meant. Never reorder.
REACTIONS = ("blur", "rgb", "invert", "zoom", "warp", "shudder", "grey", "hue", "off", "kaleido")
STYLES = REACTIONS                                           # (the old name; /api/mxvbox sends it)


def reaction(v):
    """0-127 -> the index into REACTIONS (tenths: 0-12, 13-25, ...)."""
    return max(0, min(len(REACTIONS) - 1, int(v) * len(REACTIONS) // 128))
# LINES (black): (every, thick) per fifth, in HALF picture rows (the GPU's thin lines; 1 = 3 screen px at
# 1080p). Until 2026-11-02 these were real-pixel looks for the 2nd screen layer, which is parked, and the
# fallback never drew (the same message's "n 0" cancelled it): the same darkness, 1 in 4 / 2 / 3 / 2 in 6.
LINE_LOOKS = (None, (4, 1), (2, 1), (3, 1), (6, 2))
LINE_LOOKS_BLUR = (None, (2, 1), (3, 1), (4, 1), (6, 2))   # LINES FILL = blurred picture: lines are part of the picture, in HALF picture rows


def fifth(v):
    """0-127 -> 0..4 (the five-way knobs): 0-25, 26-51, 52-76, 77-102, 103-127."""
    return max(0, min(4, int(v) * 5 // 128))


def defaults():
    return {cc: d for cc, (_n, d, _h) in KNOBS.items()}
