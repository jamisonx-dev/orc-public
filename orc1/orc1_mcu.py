"""orc1_mcu -- Mackie Control Universal (MCU) message codec, stdlib only.

Makes the ORC MCU-aware: it can READ what an MCU surface sends (decode) and
build what an MCU host sends back to one (encode). No MIDI ports are opened
here; the brain feeds it mido messages and sends what it returns.

What MCU looks like on the wire (the Logic/Mackie Control protocol, channel 1):

  V-pot turn       CC 16-23            relative, sign-magnitude: bit 6 = turning
                                       left, bits 0-5 = how far (0x01 = +1,
                                       0x41 = -1)
  V-pot LED ring   CC 48-55 (out)      0x00-0x0B position, bits 4-5 = ring mode
                                       (0 dot, 1 boost/cut, 2 wrap, 3 spread),
                                       bit 6 = the centre LED
  Fader            pitch bend, ch 1-8  14-bit absolute, ch 9 = master fader
  Jog wheel        CC 60               relative, sign-magnitude like a V-pot
  Buttons          note on, ch 1       velocity 127 = press, 0 = release;
                                       the same note sent back lights its LED
  Scribble strip   SysEx F0 00 00 66 <dev> 12 <offset> <ascii...> F7
                   (dev 0x14 = MCU main unit, 0x15 = an XT extender);
                   2 rows x 56 chars, offset 0 = row 1, 56 = row 2

HUI (the Pro Tools dialect) is NOT decoded yet: it is a different protocol
(zone/port switch pairs, a ping the host must answer once a second). Add it as
its own codec when a HUI device needs it.

BOTH DIRECTIONS (2026-11-06). The first half of this file reads a surface /
talks to one as a host (the BSP follow-knobs). The second half ("the ORC AS
the surface") is the mirror image, used by orc1_dawsurface: encode what a
surface SENDS (V-pot turns, fader moves + touch, button presses, jog) and
decode_host() what a host sends a surface (motor faders, ring LEDs, button
LEDs incl. blink, the LCD, meters, the 7-segment digits, the handshake).

Checked against real sources on 2026-11-06, not memory:
  * REAPER's own MCU code, csurf_mcu.cpp in the REAPER SDK
    (https://github.com/justinfrankel/reaper-sdk, reaper-plugins/reaper_csurf/):
    fader touch = notes 0x68+ (velocity >= 0x7F = touched); fader in = pitch
    bend, LSB first; V-pot in = CC 0x10-0x17, sign-magnitude (bit 6 = left);
    jog CC 0x3C, >= 0x41 = backwards; LCD = F0 00 00 66 <0x14 / 0x15 for
    "MCUEX"> 12 <pos> ... F7; meters = D0 (strip<<4 | 0x0-0xD), 0xF clears,
    "0xe turns on clip indicator"; ring = CC 0x30+strip, value 1 + (x*11>>7);
    LEDs 0x7F on, solo sent as 1 (= blink); bank 0x2E/0x2F, channel
    0x30/0x31, transport 0x5B-0x5F, flip 0x32, global 0x33; assignment digits
    = CC 0x4A/0x4B, timecode CC 0x40+.
    REAPER needs NO HANDSHAKE: it never sends a device query and never waits
    for a reply. It pushes the whole state when it starts, and when it hears
    F0 00 00 66 <dev> 01 58 59 5A ("XYZ", 9 bytes compared) from the surface it
    re-sends everything (MCUReset). surface_hello() below sends exactly that
    shape (default serial "XYZORC1") so the ORC can ask for a full refresh.
  * The Logic Control / Mackie Control protocol tables collected in TouchMCU's
    doc (https://github.com/EMATech/TouchMCU/blob/main/doc/mackie_control_protocol.md):
    handshake 0x00 device query -> 0x01 host connection query (7-byte serial +
    4-byte challenge) -> host 0x02 reply (serial + 4-byte response) -> 0x03
    confirmation (or 0x04 error); 0x13 version request -> 0x14 reply; meter
    levels 0x0 <-60 dB .. 0xC 0 dB, 0xD > 0 dB, 0xE set overload, 0xF clear;
    LED velocity 0 off / 1 blink / 127 on; touch notes 104-111 + 112 master;
    ring bits: 6 = centre LED, 5-4 = mode, 3-0 = 0x0 (off) .. 0xB; 7-segment
    CC 0x40-0x49 = timecode digits RIGHT to left, 0x4A-0x4B = assignment,
    bit 6 = the dot, 0x01-0x1A = A-Z, 0x20-0x3F = ASCII; full button list.
  * Ardour's MCU code (https://github.com/Ardour/ardour, libs/surfaces/mackie/
    surface.cc): says the device query, F0 00 00 66 14 00 F7, only finishes
    the challenge for Logic Control ids 0x10/0x11; sends a MASTER meter as
    channel pressure on MIDI channel 2 (D1). NOTE: its challenge response line 2
    reads `l[2] >> l[3]` where the TouchMCU table says `c[2] >> 4`; this file
    follows the table (only Logic is known to check it; not tested on Logic).
"""

MCU_CH = 0                 # every MCU CC / button rides MIDI channel 1 (0-based 0)
VPOT_CC0 = 16              # CC 16..23 = V-pot 1..8 turns
VPOT_RING_CC0 = 48         # CC 48..55 = V-pot 1..8 LED rings (host -> surface)
JOG_CC = 60
N_STRIPS = 8
MASTER_FADER = 8           # pitch bend channel 9 (0-based 8)

DEV_MCU = 0x14
DEV_XT = 0x15
SYSEX_HDR = (0x00, 0x00, 0x66)     # Mackie manufacturer id
LCD_CMD = 0x12
LCD_WIDTH = 56                     # chars per row (8 strips x 7)

RING_DOT, RING_BOOST, RING_WRAP, RING_SPREAD = 0, 1, 2, 3

# Button notes: the standard Logic Control layout. Strip buttons are 8 wide.
_STRIP_BUTTONS = (("rec", 0), ("solo", 8), ("mute", 16), ("select", 24),
                  ("vpot_push", 32), ("fader_touch", 104))
BUTTONS = {
    46: "bank_left", 47: "bank_right", 48: "channel_left", 49: "channel_right",
    50: "flip", 91: "rewind", 92: "fast_fwd", 93: "stop", 94: "play",
    95: "record", 96: "up", 97: "down", 98: "left", 99: "right",
    100: "zoom", 101: "scrub", 112: "master_touch",
    # the rest of the Logic Control layout (TouchMCU table, 2026-11-06)
    40: "assign_track", 41: "assign_send", 42: "assign_pan", 43: "assign_plugin",
    44: "assign_eq", 45: "assign_instrument", 51: "global_view",
    52: "name_value", 53: "smpte_beats",
    54: "f1", 55: "f2", 56: "f3", 57: "f4", 58: "f5", 59: "f6", 60: "f7", 61: "f8",
    62: "midi_tracks", 63: "inputs", 64: "audio_tracks", 65: "audio_instruments",
    66: "aux", 67: "busses", 68: "outputs", 69: "user",
    70: "shift", 71: "option", 72: "control", 73: "alt",
    74: "read_off", 75: "write", 76: "trim", 77: "touch", 78: "latch", 79: "group",
    80: "save", 81: "undo", 82: "cancel", 83: "enter", 84: "markers", 85: "nudge",
    86: "cycle", 87: "drop", 88: "replace", 89: "click", 90: "global_solo",
    102: "user_a", 103: "user_b",
    113: "smpte_led", 114: "beats_led", 115: "rude_solo",     # LEDs only
}
BUTTON_NOTES = {v: k for k, v in BUTTONS.items()}


def _signmag(value):
    """MCU relative value -> signed step (0x01 = +1, 0x41 = -1, 0 = no move)."""
    step = value & 0x3F
    return -step if value & 0x40 else step


def decode(msg):
    """One mido message -> an MCU event tuple, or None if it isn't MCU:
        ("vpot", strip 0-7, delta)
        ("fader", strip 0-8, value 0-16383)     (strip 8 = master)
        ("jog", delta)
        ("button", name, strip-or-None, pressed)
    """
    t = msg.type
    if t == "control_change" and msg.channel == MCU_CH:
        c = msg.control
        if VPOT_CC0 <= c < VPOT_CC0 + N_STRIPS:
            return ("vpot", c - VPOT_CC0, _signmag(msg.value))
        if c == JOG_CC:
            return ("jog", _signmag(msg.value))
        return None
    if t == "pitchwheel" and msg.channel <= MASTER_FADER:
        return ("fader", msg.channel, msg.pitch + 8192)
    if t in ("note_on", "note_off") and msg.channel == MCU_CH:
        n = msg.note
        pressed = t == "note_on" and msg.velocity > 0
        for name, base in _STRIP_BUTTONS:
            if base <= n < base + N_STRIPS:
                return ("button", name, n - base, pressed)
        if n in BUTTONS:
            return ("button", BUTTONS[n], None, pressed)
    return None


# ---- encode: what an MCU host sends to the surface ---------------------------
# Each returns plain (type, fields) dicts so callers pick their own MIDI library;
# to_mido() turns one into a mido.Message.

def vpot_ring(strip, value, mode=RING_DOT, centre=False):
    """LED ring for V-pot `strip` showing `value` 0-127 (as 1-11 ring LEDs)."""
    pos = 1 + int(round(max(0, min(127, value)) * 10 / 127.0))
    v = (mode & 3) << 4 | pos | (0x40 if centre else 0)
    return {"type": "control_change", "channel": MCU_CH,
            "control": VPOT_RING_CC0 + strip, "value": v}


def fader(strip, value14):
    """Move motor fader `strip` (0-8) to 0-16383."""
    v = max(0, min(16383, int(value14)))
    return {"type": "pitchwheel", "channel": strip, "pitch": v - 8192}


def button_led(name_or_note, on, strip=None):
    """Light / clear a button LED. Name from BUTTONS, or a strip button name
    ("mute", ...) with its strip, or a raw note number."""
    if isinstance(name_or_note, int):
        note = name_or_note
    else:
        note = BUTTON_NOTES.get(name_or_note)
        if note is None:
            base = dict(_STRIP_BUTTONS)[name_or_note]
            note = base + int(strip)
    return {"type": "note_on", "channel": MCU_CH, "note": note,
            "velocity": 127 if on else 0}


def lcd(text, row=0, col=0, dev=DEV_MCU):
    """Scribble-strip SysEx data (without F0/F7, as mido wants it). Non-ASCII
    and control characters become spaces; text is clipped to the row."""
    off = row * LCD_WIDTH + col
    room = LCD_WIDTH - col
    chars = [(ord(ch) if 32 <= ord(ch) < 127 else 32) for ch in str(text)[:room]]
    return {"type": "sysex", "data": SYSEX_HDR + (dev, LCD_CMD, off) + tuple(chars)}


def strip_labels(names, row=0, dev=DEV_MCU):
    """Eight names, each clipped / padded to its 7-char strip, as one LCD write."""
    cells = [(str(n or "")[:6]).ljust(7) for n in list(names)[:N_STRIPS]]
    cells += [" " * 7] * (N_STRIPS - len(cells))
    return lcd("".join(cells), row=row, dev=dev)


def to_mido(d, mido):
    """One encode() dict -> mido.Message (mido passed in so this stays stdlib)."""
    fields = {k: v for k, v in d.items() if k != "type"}
    return mido.Message(d["type"], **fields)


# =============================================================================
# The ORC AS the surface (2026-11-06): what a surface SENDS, and decode_host()
# for what a host sends to a surface. Same plain dicts as above.
# =============================================================================

LED_OFF, LED_BLINK, LED_ON = 0, 1, 2           # decode_host() button-LED states
METER_MAX = 0x0D                               # 0xC = 0 dB, 0xD = over 0 dB
METER_SET_OVERLOAD, METER_CLEAR_OVERLOAD = 0x0E, 0x0F
TC_CC0 = 0x40          # CC 0x40-0x49: timecode digits, RIGHTMOST first
TC_DIGITS = 10
ASSIGN_CC0 = 0x4A      # CC 0x4A-0x4B: the 2-digit assignment display, right first
SX_DEVICE_QUERY, SX_HOST_QUERY, SX_HOST_REPLY = 0x00, 0x01, 0x02
SX_CONFIRM, SX_ERROR = 0x03, 0x04
SX_VERSION_REQ, SX_VERSION_REPLY = 0x13, 0x14
SX_METER_MODE, SX_GLOBAL_METER_MODE = 0x20, 0x21
DEFAULT_SERIAL = "XYZORC1"    # 7 ASCII; "XYZ" = REAPER's refresh trigger (see top)
DEFAULT_CHALLENGE = (0x01, 0x02, 0x03, 0x04)
DEFAULT_VERSION = "V1.00"     # 5 ASCII


def _note_for(name_or_note, strip=None):
    if isinstance(name_or_note, int):
        return name_or_note
    note = BUTTON_NOTES.get(name_or_note)
    if note is not None:
        return note
    base = dict(_STRIP_BUTTONS)[name_or_note]       # KeyError = unknown name
    s = int(strip)
    if not 0 <= s < N_STRIPS:
        raise ValueError("strip %r out of 0-%d" % (strip, N_STRIPS - 1))
    return base + s


def _relative(control, delta):
    """A signed step -> sign-magnitude CC messages, split into chunks of 63."""
    out = []
    d = int(delta)
    while d:
        step = max(-63, min(63, d))
        out.append({"type": "control_change", "channel": MCU_CH, "control": control,
                    "value": (0x40 | -step) if step < 0 else step})
        d -= step
    return out


def vpot_turn(strip, delta):
    """Surface -> host: V-pot `strip` turned `delta` detents (list, may be empty)."""
    if not 0 <= int(strip) < N_STRIPS:
        raise ValueError("strip %r out of 0-7" % (strip,))
    return _relative(VPOT_CC0 + int(strip), delta)


def jog_turn(delta):
    """Surface -> host: the jog wheel turned `delta` (list, may be empty)."""
    return _relative(JOG_CC, delta)


def button(name_or_note, pressed, strip=None):
    """Surface -> host: a button press (velocity 127) or release (velocity 0).
    Strip buttons: rec / solo / mute / select / vpot_push / fader_touch."""
    return {"type": "note_on", "channel": MCU_CH, "note": _note_for(name_or_note, strip),
            "velocity": 127 if pressed else 0}


def fader_touch(strip, touched):
    """Surface -> host: fader `strip` (0-7, 8 = master) touched / let go."""
    s = int(strip)
    if s == MASTER_FADER:
        return button("master_touch", touched)
    return button("fader_touch", touched, strip=s)


def meter(strip, level):
    """Host -> surface (encode, for tests and a host role): channel pressure."""
    return {"type": "aftertouch", "channel": MCU_CH,
            "value": (int(strip) & 7) << 4 | (int(level) & 0x0F)}


def led(name_or_note, state, strip=None):
    """Host -> surface: an LED as LED_OFF / LED_BLINK / LED_ON (0 / 1 / 127)."""
    v = {LED_OFF: 0, LED_BLINK: 1, LED_ON: 127}[state]
    return {"type": "note_on", "channel": MCU_CH, "note": _note_for(name_or_note, strip),
            "velocity": v}


def segment(index, char, dot=False, cc0=TC_CC0):
    """Host -> surface: one 7-segment digit (index 0 = rightmost)."""
    o = ord(char)
    c = o - 0x40 if 0x40 <= o < 0x60 else (o if 0x20 <= o < 0x40 else 0x20)
    if 0x60 < o <= 0x7A:                       # lower case -> the letter
        c = o - 0x60
    return {"type": "control_change", "channel": MCU_CH, "control": cc0 + int(index),
            "value": c | (0x40 if dot else 0)}


def _segment_char(v):
    """7-segment code -> char. Code 0 is a BLANK digit, not '@': REAPER's csurf_mcu zeroes its
    digit buffer and sends 0x00 for every position it doesn't fill (live status 2026-11-06 read
    "@@1@1@@000"); '@' can't be drawn on 7 segments anyway."""
    c = v & 0x3F
    if c == 0:
        return " "
    return chr(c + 0x40) if c < 0x20 else chr(c)


def sysex(cmd, payload=(), dev=DEV_MCU):
    return {"type": "sysex", "data": SYSEX_HDR + (dev, cmd) + tuple(payload)}


def _ascii7(text, n):
    s = str(text)[:n].ljust(n)
    return tuple((ord(ch) if 32 <= ord(ch) < 127 else 32) for ch in s)


def challenge_response(c):
    """The Logic Control handshake answer to a 4-byte challenge (TouchMCU table)."""
    c0, c1, c2, c3 = (int(x) for x in c)
    return (0x7F & (c0 + (c1 ^ 0x0A) - c3),
            0x7F & ((c2 >> 4) ^ (c0 + c3)),
            0x7F & ((c3 - (c2 << 2)) ^ (c0 | c1)),
            0x7F & (c1 - c2 + (0xF0 ^ (c3 << 4))))


def surface_hello(dev=DEV_MCU, serial=DEFAULT_SERIAL, challenge=DEFAULT_CHALLENGE):
    """Surface -> host: 'host connection query' (what a real MCU sends at power-on
    and in answer to a device query). With the default serial REAPER reads it as
    its reset message and re-sends the whole surface state."""
    return sysex(SX_HOST_QUERY, _ascii7(serial, 7) + tuple(int(x) & 0x7F for x in challenge), dev)


def host_reply(serial, response, dev=DEV_MCU):
    """Host -> surface (encode, for tests): the 0x02 connection reply."""
    return sysex(SX_HOST_REPLY, _ascii7(serial, 7) + tuple(response), dev)


def device_query(dev=DEV_MCU):
    """Host -> surface (encode, for tests): F0 00 00 66 <dev> 00 F7."""
    return sysex(SX_DEVICE_QUERY, (), dev)


def _f(msg, name, default=None):
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def decode_host(msg):
    """What a HOST sends to a surface -> an event tuple, or None if not MCU:
        ("fader", strip 0-8, value 0-16383)            motor fader (8 = master)
        ("ring", strip, pos 0-11, mode 0-3, centre)     pos 0 = ring off
        ("led", name, strip-or-None, LED_OFF/BLINK/ON)  name may be a raw note int
        ("meter", strip, level 0-13 | "overload" | "clear")
        ("master_meter", side 0-1, level | "overload" | "clear")   Ardour's D1
        ("segment", "timecode"/"assignment", index, char, dot)
        ("lcd", dev, offset, text)
        ("sysex", dev, cmd, data-tuple)                  any other Mackie SysEx
    Accepts mido messages or the plain dicts the encoders here return."""
    t = _f(msg, "type")
    ch = _f(msg, "channel", 0)
    if t == "pitchwheel" and 0 <= ch <= MASTER_FADER:
        return ("fader", ch, _f(msg, "pitch") + 8192)
    if t == "control_change":
        c, v = _f(msg, "control"), _f(msg, "value")
        if ch == MCU_CH and VPOT_RING_CC0 <= c < VPOT_RING_CC0 + N_STRIPS:
            return ("ring", c - VPOT_RING_CC0, v & 0x0F, (v >> 4) & 3, bool(v & 0x40))
        if ch in (0, 15) and TC_CC0 <= c < TC_CC0 + TC_DIGITS:
            return ("segment", "timecode", c - TC_CC0, _segment_char(v), bool(v & 0x40))
        if ch in (0, 15) and ASSIGN_CC0 <= c < ASSIGN_CC0 + 2:
            return ("segment", "assignment", c - ASSIGN_CC0, _segment_char(v), bool(v & 0x40))
        return None
    if t in ("note_on", "note_off") and ch == MCU_CH:
        n, v = _f(msg, "note"), _f(msg, "velocity", 0)
        if t == "note_off" or v == 0:
            st = LED_OFF
        elif v == 127:
            st = LED_ON
        elif v & 1:
            st = LED_BLINK
        else:
            st = LED_ON
        for name, base in _STRIP_BUTTONS:
            if base <= n < base + N_STRIPS:
                return ("led", name, n - base, st)
        return ("led", BUTTONS.get(n, n), None, st)
    if t == "aftertouch" and ch in (0, 1):
        v = _f(msg, "value")
        s, lv = v >> 4, v & 0x0F
        lv = "overload" if lv == METER_SET_OVERLOAD else ("clear" if lv == METER_CLEAR_OVERLOAD else lv)
        if ch == 1:
            return ("master_meter", s, lv)
        return ("meter", s, lv) if s < N_STRIPS else None
    if t == "sysex":
        d = tuple(_f(msg, "data") or ())
        if len(d) < 5 or d[:3] != SYSEX_HDR:
            return None
        dev, cmd, rest = d[3], d[4], d[5:]
        if cmd == LCD_CMD and rest:
            return ("lcd", dev, rest[0], "".join(chr(b) for b in rest[1:]))
        return ("sysex", dev, cmd, rest)
    return None


def surface_answer(ev, dev=DEV_MCU, serial=DEFAULT_SERIAL,
                   challenge=DEFAULT_CHALLENGE, version=DEFAULT_VERSION, strict=False):
    """The surface's reply to a handshake decode_host() event, or None.
    Device query -> host connection query; host reply -> confirmation (or, when
    `strict` and the response is wrong, the error a real MCU sends); version
    request -> version reply. Lenient by default: hosts that answer wrong (or
    never) still get a working surface."""
    if not ev or ev[0] != "sysex":
        return None
    cmd, rest = ev[2], ev[3]
    if cmd == SX_DEVICE_QUERY:
        return surface_hello(dev, serial, challenge)
    if cmd == SX_HOST_REPLY:
        ok = tuple(rest[7:11]) == challenge_response(challenge)
        return sysex(SX_CONFIRM if (ok or not strict) else SX_ERROR, _ascii7(serial, 7), dev)
    if cmd == SX_VERSION_REQ:
        return sysex(SX_VERSION_REPLY, _ascii7(version, 5), dev)
    return None
