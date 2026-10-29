"""vis_palettes.py -- the VISUALS colour palettes (2026-10-24).

PALETTE SLOTS: SLOTS palettes of 1..MAX_COLOURS colours each, set up ahead of
time on /visuals and picked live by a knob per layer (LINES PALETTE CC 95,
BACKGROUND PALETTE CC 96: 0 range = off = the scene's own colours, then one
range per slot). owner 2026-10-24: the visuals are an automatable lightshow
for a projector over a band, so a palette is a CC-chosen slot, never a
page-only setting. A palette is a loop: a layer's colour wheel is laid round
its colours in order, blending from one to the next and back to the first.

Stored in ~/.orc1_visuals_palettes.json (the web writes it, the renderer
re-reads it when it changes). Light: stdlib only, shared by the web and the
renderer.
"""
import json
import os
import re
import tempfile
import threading

SLOTS = 8
MAX_COLOURS = 4
DEFAULTS = (
    ("#ff3b1f", "#ffb000", "#ffe9a0"),           # 1 fire
    ("#00e5ff", "#2f6bff", "#e8fbff"),           # 2 ice
    ("#ff2bd6", "#00f0ff"),                      # 3 neon: magenta / cyan
    ("#ff5e3a", "#ff2a68", "#7a2bff"),           # 4 sunset
    ("#39ff14", "#0aa36b", "#d7ff5c"),           # 5 acid green
    ("#ffffff",),                                # 6 white
    ("#ff0000", "#00ff00", "#0000ff"),           # 7 RGB
    ("#ffd27a", "#ff8c1a", "#ffffff", "#8a5cff"),  # 8 gold + violet
)
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def path():
    """Where the palettes live (resolved at call time: tests sandbox HOME)."""
    return os.path.expanduser("~/.orc1_visuals_palettes.json")


def check(slots):
    """A list of SLOTS lists of '#rrggbb' -> the same, normalised to lower
    case. Raises ValueError with the reason when it can't be used."""
    if not isinstance(slots, (list, tuple)) or len(slots) != SLOTS:
        raise ValueError("need exactly %d palettes" % SLOTS)
    out = []
    for i, p in enumerate(slots):
        if not isinstance(p, (list, tuple)) or not 1 <= len(p) <= MAX_COLOURS:
            raise ValueError("palette %d needs 1 to %d colours" % (i + 1, MAX_COLOURS))
        for c in p:
            if not isinstance(c, str) or not _HEX.match(c):
                raise ValueError("palette %d: %r is not a colour like #ff8800" % (i + 1, c))
        out.append([c.lower() for c in p])
    return out


def load():
    """-> the SLOTS palettes as lists of '#rrggbb' (the defaults when the file
    is missing or broken)."""
    try:
        with open(path(), encoding="utf-8") as f:
            return check(json.load(f)["slots"])
    except Exception:
        return [list(p) for p in DEFAULTS]


_LOCK = threading.Lock()              # the web serves requests on threads


def _write(slots):
    p = path()
    fd, tmp = tempfile.mkstemp(prefix=".orc1_visuals_palettes.", suffix=".tmp", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"slots": slots}, f)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save(slots):
    """Check + store all SLOTS (DEFAULTS on /visuals). -> the stored slots.
    Raises ValueError (nothing written)."""
    slots = check(slots)
    with _LOCK:
        _write(slots)
    return slots


def save_slot(i, colours):
    """Check + store ONE palette, leaving the others as they are on disk (two
    open /visuals pages each change only the slot they edit). -> all slots."""
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < SLOTS:
        raise ValueError("no palette %r (1 to %d)" % (i, SLOTS))
    with _LOCK:
        slots = load()
        slots[i] = colours
        slots = check(slots)
        _write(slots)
    return slots


def sig():
    """Changes whenever the palettes file does."""
    try:
        st = os.stat(path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def rgb(hexcol):
    """'#rrggbb' -> (r, g, b) floats 0..1."""
    return tuple(int(hexcol[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


def as_rgb():
    """-> the SLOTS palettes as lists of (r, g, b) floats 0..1."""
    return [[rgb(c) for c in p] for p in load()]


def slot_of(knob, slots=SLOTS):
    """A PALETTE knob 0..127 -> None (off) or the slot index 0..slots-1: the
    knob is split into slots + 1 equal ranges, the first = off (like every
    picker knob: a button sends its range's middle)."""
    i = min(slots, max(0, int(knob)) * (slots + 1) // 128)
    return None if i == 0 else i - 1
