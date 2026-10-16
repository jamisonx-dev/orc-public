"""
fire_oled.py - Akai Fire OLED text rendering over MIDI SysEx.

Implements the SEGGER reverse-engineered warped-bit pixel remap
(blog.segger.com/decoding-the-akai-fire-part-3). The 128x64 1-bit display
is packed into a 1175-byte payload with a 4-byte band/column header
(00 07 00 7F = whole display) and sent as:

    F0 47 7F 43 0E <len_hi> <len_lo> <1175 payload bytes> F7

Text is rendered with Pillow into a 128x64 1-bit image, then each lit pixel
is plotted through the remap. Reusable by orc1_fire.py for live readouts.
"""

import mido
import threading

OLED_W, OLED_H = 128, 64

# One serialization point for EVERY MIDI write to the Fire. The OLED frame
# (send_image, below) and the LED grid (orc1_fire._send_pads) are BIG SysEx
# messages sent to the same physical Fire over separate mido ports and from
# separate threads (the OLED coalescer worker vs the handler's ~25 fps LED
# render thread while a sequence plays). Without a shared lock their USB-MIDI
# packets interleave and the device draws a corrupted OLED frame -- the glitch
# seen when turning a knob during playback. Both senders take THIS lock so a
# frame is always handed to the device whole. Held only around the .send().
IO_LOCK = threading.Lock()

# SEGGER _aBitMutate[8][7]: reference bit number for each (Y%8, X%7) cell.
_BITMUTATE = (
    (13, 19, 25, 31, 37, 43, 49),
    ( 0, 20, 26, 32, 38, 44, 50),
    ( 1,  7, 27, 33, 39, 45, 51),
    ( 2,  8, 14, 34, 40, 46, 52),
    ( 3,  9, 15, 21, 41, 47, 53),
    ( 4, 10, 16, 22, 28, 48, 54),
    ( 5, 11, 17, 23, 29, 35, 55),
    ( 6, 12, 18, 24, 30, 36, 42),
)


def _new_buf():
    buf = bytearray(1175)
    buf[0] = 0x00   # start band
    buf[1] = 0x07   # end band  (8 bands of 8px = full height)
    buf[2] = 0x00   # start column
    buf[3] = 0x7F   # end column (full width)
    return buf


def _plot(buf, x, y, on=1):
    if 0 <= x < 128 and 0 <= y < 64:
        X = x + 128 * (y // 8)      # unwind 128x64 -> 1024x8
        Y = y % 8
        rb = _BITMUTATE[Y][X % 7]
        idx = 4 + (X // 7) * 8 + rb // 7
        bit = 1 << (rb % 7)
        if on:
            buf[idx] |= bit
        else:
            buf[idx] &= ~bit


def image_to_buf(img):
    """img: PIL image, 128x64, nonzero pixel = lit."""
    buf = _new_buf()
    px = img.load()
    for y in range(OLED_H):
        for x in range(OLED_W):
            if px[x, y]:
                _plot(buf, x, y, 1)
    return buf


def sysex_payload(buf):
    n = len(buf)
    return [0x47, 0x7F, 0x43, 0x0E, (n >> 7) & 0x7F, n & 0x7F] + list(buf)


def _is_fire_port(name):
    """Same rule as the brain (orc1_config.is_fire_port): Akai's firmware or our
    firmware's host mode. Falls back to Akai's name when used outside the ORC."""
    try:
        import orc1_config
        return orc1_config.is_fire_port(name)
    except Exception:
        return "fl studio fire" in (name or "").lower()


def find_fire_output():
    for n in mido.get_output_names():
        if _is_fire_port(n):
            return n
    return None


# The OLED's own output connection, cached per port NAME. After a USB replug
# the Fire comes back under the SAME name, so a cached port would keep sending
# into the dead old connection forever (screen stays dark while the pads, which
# reopen their own port, come back). reset_output() drops it; the brain's
# hot-plug path calls it via FireHandler.rewake() (2026-09-28).
_OUT_CACHE = {}
_LAST = {}
_LAST_T = {}


def reset_output():
    """Forget the cached OLED output + dedupe state (call after a replug)."""
    for name, out in list(_OUT_CACHE.items()):
        try:
            out.close()
        except Exception:
            pass
    _OUT_CACHE.clear()
    _LAST.clear()
    _LAST_T.clear()


def forget_sent():
    """Drop the 'identical frame, skip it' memory so the next frame is really
    sent (a just-replugged Fire has a blank screen whatever we sent before)."""
    _LAST.clear()
    _LAST_T.clear()


def send_image(img, port=None, _cache=_OUT_CACHE, _last=_LAST, _last_t=_LAST_T):
    import time as _time
    name = port or find_fire_output()
    if name is None:
        raise RuntimeError("Akai Fire MIDI output not found")
    out = _cache.get(name)
    if out is None:
        out = _cache[name] = mido.open_output(name)
    buf = image_to_buf(img)
    now = _time.monotonic()
    if _last.get(name) == buf and (now - _last_t.get(name, 0)) < 20:
        return  # identical and sent recently -- skip to avoid OLED blink
    with IO_LOCK:   # never interleave with an LED-grid SysEx (see IO_LOCK note)
        out.send(mido.Message("sysex", data=sysex_payload(buf)))
        _last[name] = bytes(buf)
        _last_t[name] = now


# --------------------------------------------------------------------------- #
# Text rendering (Pillow)                                                      #
# --------------------------------------------------------------------------- #
_FONT_PATH      = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
_font_cache = {}

def _get_font(bold=False, size=9):
    key = (bold, size)
    if key not in _font_cache:
        from PIL import ImageFont
        path = _FONT_PATH_BOLD if bold else _FONT_PATH
        try:
            _font_cache[key] = ImageFont.truetype(path, size)
        except Exception:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# --------------------------------------------------------------------------- #
# Text budget                                                                  #
# --------------------------------------------------------------------------- #
# DejaVuSansMono is MONOSPACE at every size -- advance width is a fixed
# 0.602 em -- so how many characters fit on a line is pure arithmetic and
# needs no PIL.  That is deliberate: it lets _test_fire.py assert line
# lengths off-hardware, on a PC with no font stack, which is the only way
# this class of bug gets caught before a player finds it.
#
# Measured against PIL on the Pi 2026-07-31 (DejaVuSansMono 13/14/20/36):
# advance/size = 0.60216 / 0.60156 / 0.60234 / 0.60200, giving budgets of
# 16 / 14 / 10 / 5 characters at the usual x=2 on the 128px panel.
#
# WHY THIS EXISTS: every assign screen on the Fire was written longer than
# the panel can show.  "ASSIGN 0,1  SEL=type/ok" rendered as "ASSIGN 0,1  SE"
# and the detail line was cut off right after the value, hiding the channel
# and output on every NOTE and CC assignment -- for as long as the feature
# has existed.  Nothing measured it, so nothing caught it.
_SCALE_PT = {1: 13, 2: 14, 3: 20, 4: 36}
_ADV_EM   = 0.602


def scale_pt(scale):
    """Point size render() will use for a given scale -- same branching."""
    if scale <= 1:
        return _SCALE_PT[1]
    if scale == 2:
        return _SCALE_PT[2]
    if scale == 3:
        return _SCALE_PT[3]
    return _SCALE_PT[4]


def char_w(scale):
    """Advance width in pixels of one character at this scale."""
    return _ADV_EM * scale_pt(scale)


def max_chars(scale=1, x=2):
    """How many characters fit from x to the right edge at this scale."""
    return max(0, int((OLED_W - x) // char_w(scale)))


def fits(text, x=2, scale=1):
    """True if text renders fully inside the panel."""
    return len(text) <= max_chars(scale, x)


def line_h(scale):
    """Vertical space one line occupies below its y, descenders included.

    Slightly conservative.  Measured on the Pi: a scale-1 line drawn at y=48
    reaches y=63 (the last row of the 64px panel), and a scale-2 line at y=0
    reaches y=13.
    """
    return scale_pt(scale) + 3


def fits_box(text, x=2, y=0, scale=1):
    """True if text renders fully inside the panel horizontally AND
    vertically.  A line placed too low is clipped just as silently as one
    written too long."""
    return fits(text, x, scale) and 0 <= y and y + line_h(scale) <= OLED_H


# Every truncation fit() actually performs, as (why, text, budget). A test
# reads and clears this. WHY THIS EXISTS: fit() is an escape hatch that makes a
# line INVISIBLE to the width check -- fits() is trivially true for anything
# already truncated -- so wrapping a line in fit() silently opts it out of the
# net. That is exactly how "universal  E0 cc 74" shipped rendering as
# "universal  E0 cc": the CC number, the entire point of that screen, was cut
# off in the one view the feature exists for, and 129 green checks said nothing.
TRUNCATIONS = []


def fit(text, x=2, scale=1, why=None):
    """Truncate text to what will actually render.

    `why` is REQUIRED whenever this can actually truncate, and names the
    variable-length thing being cut -- a plugin name, a track name, a port
    key. _test_fire.py fails on any untagged truncation, because an untagged
    one means a fixed string (or worse, a VALUE) is being silently clipped,
    and the fix for that is to write the line shorter, not to hide it here.
    """
    budget = max_chars(scale, x)
    if len(text) > budget:
        TRUNCATIONS.append((why, text, budget))
    return text[:budget]


def render(draws):
    """draws: list of (text, x, y, scale). Returns a 128x64 mode-'1' image.
    scale=1 → small mono 8px, scale=2 → bold 14px, scale=3+ → bold 20px."""
    from PIL import Image, ImageDraw
    img = Image.new("1", (OLED_W, OLED_H), 0)
    for text, x, y, scale in draws:
        if scale <= 1:
            font = _get_font(bold=False, size=13)
        elif scale == 2:
            font = _get_font(bold=True, size=14)
        elif scale == 3:
            font = _get_font(bold=True, size=20)
        else:
            font = _get_font(bold=True, size=36)
        ImageDraw.Draw(img).text((x, y), text, fill=1, font=font)
    return img


# --------------------------------------------------------------------------- #
# Push coalescer                                                               #
# --------------------------------------------------------------------------- #
# render() + image_to_buf() is ~7 ms of GIL-holding pure-Python per frame
# (measured on a Pi 4: 4.6 ms PIL render + 2.3 ms the 8192-pixel pack). A Fire
# knob sweep emits detents far faster than one every 7 ms, and each detent used
# to force a full repaint synchronously on the Fire INPUT thread. Under CPython's
# GIL that starves the SEPARATE thread feeding arp.clock_tick() from incoming
# MIDI clock, so an externally-synced sequence audibly DRAGS while you turn a
# knob (worst on the drum-voice sculpt screen, but it is not drum-specific --
# any rapid Fire knob hits it).
#
# The fix is to coalesce: show_lines() only stores the LATEST draws for a port
# and wakes a single daemon worker. The worker renders at most once per
# _MIN_INTERVAL and always paints a trailing frame, so the value still tracks
# the knob but the redundant intermediate frames -- unreadable at sweep speed --
# are never rendered at all. This cuts both the number of renders and moves the
# render off the input thread, bounding OLED GIL load to ~7 ms per _MIN_INTERVAL.
import threading as _threading

_MIN_INTERVAL        = 0.04            # cap actual OLED pushes to ~25 fps
_oled_lock           = _threading.Lock()
_oled_pending        = {}             # port -> latest draws awaiting render
_oled_last_t         = {}             # port -> monotonic of last actual push
_oled_wake           = _threading.Event()
_oled_worker_started = False


def _oled_worker():
    import time as _time
    while True:
        _oled_wake.wait()
        _oled_wake.clear()
        # Drain every port that has a pending frame, honouring _MIN_INTERVAL.
        while True:
            with _oled_lock:
                if not _oled_pending:
                    break
                port = next(iter(_oled_pending))
                last = _oled_last_t.get(port, 0.0)
            wait = last + _MIN_INTERVAL - _time.monotonic()
            if wait > 0:
                _time.sleep(wait)      # let further detents coalesce into pending
            with _oled_lock:
                draws = _oled_pending.pop(port, None)
                _oled_last_t[port] = _time.monotonic()
            if draws is not None:
                try:
                    send_image(render(draws), port=port)
                except Exception:
                    pass


def _ensure_oled_worker():
    global _oled_worker_started
    if _oled_worker_started:
        return
    with _oled_lock:
        if _oled_worker_started:
            return
        t = _threading.Thread(target=_oled_worker, name="oled-coalesce",
                              daemon=True)
        t.start()
        _oled_worker_started = True


def show_lines(draws, port=None):
    """Queue a frame for the OLED. Non-blocking: the actual render + SysEx push
    happens on the coalescing worker, at most once per _MIN_INTERVAL per port,
    with the latest draws winning. See the Push coalescer note above for why."""
    _ensure_oled_worker()
    with _oled_lock:
        _oled_pending[port] = draws
    _oled_wake.set()


def show_lines_now(draws, port=None):
    """Synchronous render + push, bypassing the coalescer. For the rare frame
    that must be on the glass before this call returns (e.g. a boot banner)."""
    send_image(render(draws), port=port)


if __name__ == "__main__":
    show_lines_now([
        ("ORC-1", 2, 0, 2),
        ("FIRE LINKED", 2, 22, 1),
        ("120.0 BPM   STOP", 2, 40, 1),
    ])
    print("OLED updated")
