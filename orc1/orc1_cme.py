"""orc1_cme.py -- the CME U6MIDI Pro's configuration, over SysEx (2026-09-30).

The brain owns the box's config port (USB Port 1) through ONE CmeLink: it reads
the live config, switches presets, and edits the router, and publishes what the
box says to ~/.orc1_cme.json for the /cme page. The port is still a PATCHBAY to
the engine (orc1.py never opens it as a musical input): this link's input
callback keeps only CME frames and drops every other message. The link's
connect/unplug/replug lifecycle (orc1._cme_scan) does not depend on that patchbay
choice; if the port IS opened as an engine input, the brain drops our config
frames there (is_config_frame) so they never reach DIN.

Protocol (PatchForge parts 1+2, then MEASURED on our unit from a USBPcap capture
of UxMIDI Tools connecting, `_planning/cme_tools/`):
  frame     F0 00 20 63 0F 16 <op> <payload> <cksum> F7
  cksum     (op + sum(payload)) & 0x7F   (replies include their own opcode)
  ops       00 control (NEVER sent here) / 01 hello / 02 read / 03 write;
            reply = op | 0x40 (old firmware) or | 0x50 (ours)
  address   section, LENGTH in 5-byte words, index  (a 0-word read = 7C)
  hello     01 00 -> 19-byte identity block
  write     03 <addr> <value>  -> ack 53 <addr>   (persists on the box at once)
  word      5 bytes = one 32-bit number in base-128, low digit FIRST:
            b0 = N & 0x0F, b1 = N>>4, b2 = N>>11, b3 = N>>18, b4 = N>>25 (7 bits each)
            text = 4 chars per word, big-endian in N.
Sections the app reads (section, words, entries): global 04x1[1] (byte 0 = active
preset, 0-based), router 00x1[6] (2 bytes: USB-out mask, DIN-out mask), filters
01x1[12] + 02x1[12], mappers 03x4[16], preset names 07x4[4], device name 0Dx6[1].
Every section reads the ACTIVE preset; switching preset swaps what they return.
"""
import json
import os
import queue
import threading
import time

HDR = [0x00, 0x20, 0x63, 0x0F, 0x16]
OP_HELLO, OP_READ, OP_WRITE = 0x01, 0x02, 0x03
ERR = 0x7C          # no such address
BAD = 0x7E          # request/value rejected (e.g. a filter bit that doesn't exist)
SECTIONS = [            # (key, section, words, entries)
    ("global",  0x04, 1, 1),
    ("router",  0x00, 1, 6),
    ("filt_ch", 0x01, 1, 12),
    ("filt_ty", 0x02, 1, 12),
    ("mappers", 0x03, 4, 16),
    ("names",   0x07, 4, 4),
    ("device",  0x0D, 6, 1),
    ("curves",  0x0B, 32, 4),   # the 4 user curves (128 values each); the app never reads them
]
ROUTER_INPUTS = ["USB In 1", "USB In 2", "USB In 3", "MIDI In 1", "MIDI In 2", "MIDI In 3"]
ROUTER_OUTPUTS = ["USB Out 1", "USB Out 2", "USB Out 3", "MIDI Out 1", "MIDI Out 2", "MIDI Out 3"]
DEFAULT_MAPPER = "00 70 0F 40 3F 00 70 0F 40 3F 0F 70 01 3C 00 07 18 00 00 2D"

# FILTERS (section 01 channels, 02 types), one entry per PORT. MEASURED on the
# wire 2026-09-30 (_planning/cme_tools/cme_filter_map.py): a SET bit BLOCKS.
# Port order = router inputs then outputs; 0-3 + 6-8 measured, 4-5 + 9-11 (MIDI
# In 2-3, MIDI Out 1-3) by that same pattern (unreachable on the test bench).
FILTER_PORTS = ROUTER_INPUTS + ROUTER_OUTPUTS
FILTER_MEASURED = (0, 1, 2, 3, 6, 7, 8)
# channel c (1-16): byte 0 bits 0-1 = ch 1-2, byte 1 = ch 3-9, byte 2 = ch 10-16.
# The box rejects (7E) any other bit.
FILTER_TYPES = [        # (mido type name, byte, bit), each blocked exactly its own
    ("note_off", 0, 0), ("note_on", 0, 1), ("polytouch", 0, 2), ("program_change", 0, 3),
    ("control_change", 1, 0), ("aftertouch", 1, 1), ("pitchwheel", 1, 2), ("sysex", 1, 3),
    ("quarter_frame", 1, 4), ("songpos", 1, 5), ("song_select", 1, 6),
    ("tune_request", 2, 0), ("clock", 2, 1), ("start", 2, 2), ("continue", 2, 3),
    ("stop", 2, 4), ("active_sensing", 2, 5), ("reset", 2, 6),
]
# type bytes 3-4 are ACCEPTED by the box but blocked nothing we sent: unknown, so
# they are always written back exactly as read, never changed.


# ── codec (pure) ─────────────────────────────────────────────────────────────

def checksum(op, payload):
    return (op + sum(payload)) & 0x7F


def frame(op, payload):
    """Data bytes of one request (no F0/F7, as mido wants). Control (00) never."""
    if op not in (OP_HELLO, OP_READ, OP_WRITE):
        raise ValueError("refusing CME opcode %02X" % op)
    payload = [int(b) for b in payload]
    bad = [b for b in payload if not 0 <= b <= 0x7F]
    if bad:     # a codec bug must FAIL here, never write a wrong (masked) value to the box
        raise ValueError("CME payload byte out of 7-bit range: %r" % bad[:4])
    return HDR + [op] + payload + [checksum(op, payload)]


def parse(data):
    """(op, payload) for a CME reply with a good checksum, else None."""
    data = list(data)
    if len(data) < len(HDR) + 2 or data[:len(HDR)] != HDR:
        return None
    op, payload, ck = data[len(HDR)], data[len(HDR) + 1:-1], data[-1]
    if checksum(op, payload) != ck:
        return None
    return op, payload


def is_config_frame(msg):
    """True for a CME config SysEx frame (our header). Never musical: anything
    that reads the CME's port as an engine input must drop these, not _pass them."""
    try:
        return msg.type == "sysex" and list(msg.data[:len(HDR)]) == HDR
    except Exception:
        return False


def reply_matches(op, req_op):
    return (op & 0x0F) == req_op and op & 0x40


def word_decode(b5):
    b = list(b5)
    return (b[4] << 25) | (b[3] << 18) | (b[2] << 11) | (b[1] << 4) | (b[0] & 0x0F)


def word_encode(n):
    return [n & 0x0F, (n >> 4) & 0x7F, (n >> 11) & 0x7F, (n >> 18) & 0x7F, (n >> 25) & 0x7F]


def text_decode(data):
    out = bytearray()
    for i in range(0, len(data) - 4, 5):
        out += word_decode(data[i:i + 5]).to_bytes(4, "big")
    return out.split(b"\0", 1)[0].decode("ascii", "replace")


def mask_to_outputs(lo, hi):
    """Router value bytes -> output indices (0-2 USB Out 1-3, 3-5 MIDI Out 1-3)."""
    return [i for i in range(3) if lo >> i & 1] + [3 + i for i in range(3) if hi >> i & 1]


def outputs_to_mask(outs):
    lo = hi = 0
    for o in outs:
        o = int(o)
        if not 0 <= o <= 5:
            raise ValueError(f"router output {o} out of range 0-5")
        if o < 3:
            lo |= 1 << o
        else:
            hi |= 1 << (o - 3)
    return lo, hi


def ch_pos(c):
    c = int(c)
    if not 1 <= c <= 16:
        raise ValueError(f"MIDI channel {c} out of range 1-16")
    return (0, c - 1) if c <= 2 else (1, c - 3) if c <= 9 else (2, c - 10)


def filter_ch_decode(v):
    return [c for c in range(1, 17) if v[ch_pos(c)[0]] >> ch_pos(c)[1] & 1]


def filter_ch_encode(channels):
    v = [0, 0, 0]
    for c in channels:
        b, bit = ch_pos(c)
        v[b] |= 1 << bit
    return v


def filter_ty_decode(v):
    return [n for n, b, bit in FILTER_TYPES if v[b] >> bit & 1]


def filter_ty_encode(types, keep=(0, 0, 0, 0, 0)):
    """5 bytes blocking `types`; the unknown bytes 3-4 come from `keep` untouched."""
    known = {n: (b, bit) for n, b, bit in FILTER_TYPES}
    v = [0, 0, 0, keep[3] & 0x7F, keep[4] & 0x7F]
    for t in types:
        if t not in known:
            raise ValueError(f"unknown message type {t!r}")
        b, bit = known[t]
        v[b] |= 1 << bit
    return v


# ── MAPPERS (section 03, 4 words) ── MEASURED 2026-09-30 by driving UxMIDI Tools
# through UI Automation while USBPcap recorded every write it made (the full table
# + method: _planning/cme_tools/MAPPER_FORMAT.md; corpus: _cme_mapper_corpus.json).
SRC_TYPES = ["Note On", "Note Off", "Ctrl Change", "Prog Change", "Pitch bend",
             "Chan Aftertouch", "Key Aftertouch", "Notes transpose", "Global channel update"]
DST_TYPES = ["Note On", "Note Off", "Ctrl Change", "Prog Change", "Pitch bend",
             "Chan Aftertouch", "Key Aftertouch", "Filter message"]
SRC_TRANSPOSE, SRC_GLOBAL_CH = 7, 8
NONE_TYPE = 0x0F
# what value 1 / value 2 MEAN for each message type (the app's column titles)
VALUE_NAMES = {0: ("Note", "Velocity"), 1: ("Note", "Velocity"), 2: ("Control", "Amount"),
               3: ("Program", None), 4: ("Bend LSB", "Bend MSB"), 5: ("Amount", None),
               6: ("Note", "Pressure"), 7: ("Note", "Velocity"), 8: (None, None)}
KNOWN_W2 = 0x3FF77FFF     # every w2 bit we can name; the rest go back exactly as read
KNOWN_W3 = 0x00FFFFFF     # w3 top byte (always 0x5A so far) goes back exactly as read
# CURVE field (4 bits per value), MEASURED on the wire: bit 0 = curve on, bits 1-2
# = which user curve (0-3 = the app's "User 1-4"), bit 3 ignored. A curve only
# acts when that value's Follow is OFF. The firmware ignores w3's top byte (0x5A:
# probably the app's own validity marker) and unused type values 9-14 / 8-14.


def curve_user(field):
    return ((field >> 1) & 3) + 1 if field & 1 else None


def curve_field(user):
    return 0 if not user else 1 | ((int(user) - 1) & 3) << 1


def _words(raw):
    return [word_decode(raw[i:i + 5]) for i in range(0, 20, 5)]


def _raw(words):
    out = []
    for w in words:
        out += word_encode(w)
    return out


def _rng(w, hi_shift):
    return [(w >> (hi_shift - 8)) & 0xFF, (w >> hi_shift) & 0xFF]     # [min, max]


def mapper_decode(raw):
    w0, w1, w2, w3 = _words(raw)
    st, dt = w2 & 0xF, (w2 >> 20) & 0xF
    m = {
        "inputs": ([i for i in range(3) if w2 >> (12 + i) & 1]
                   + [3 + i for i in range(3) if w2 >> (16 + i) & 1]),
        "src": {"type": None if st == NONE_TYPE else st,
                "ch_min": ((w2 >> 4) & 0xF) + 1, "ch_max": ((w2 >> 8) & 0xF) + 1,
                "v1": _rng(w0, 24), "v2": _rng(w0, 8)},
        "dst": {"type": None if dt == NONE_TYPE else dt, "ch": ((w2 >> 24) & 0xF) + 1,
                "v1": _rng(w1, 24), "v2": _rng(w1, 8)},
        "keep_original": bool(w2 >> 28 & 1), "transpose_ch": bool(w2 >> 29 & 1),
        "follow": {"ch": bool(w3 & 1), "v1": bool(w3 >> 1 & 1), "v2": bool(w3 >> 2 & 1)},
        "v1": {"use_other": bool(w3 >> 3 & 1), "invert": bool(w3 >> 5 & 1),
               "compress": bool(w3 >> 7 & 1), "curve": (w3 >> 16) & 0xF},
        "v2": {"use_other": bool(w3 >> 4 & 1), "invert": bool(w3 >> 6 & 1),
               "compress": bool(w3 >> 8 & 1), "curve": (w3 >> 20) & 0xF},
        "skip": (w3 >> 9) & 0x7F,
    }
    if m["src"]["type"] == SRC_TRANSPOSE:     # w1's value-1 range holds the transpose
        m["transpose"] = {"semitone": ((w1 >> 24) & 0xFF) - 48, "octave": ((w1 >> 16) & 0xFF) - 4}
    m["active"] = bool(m["inputs"]) and m["src"]["type"] is not None and m["dst"]["type"] is not None
    m["default"] = hx(raw) == DEFAULT_MAPPER
    return m


def _clamp(v, lo, hi, what):
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError(f"{what} {v} out of range {lo}-{hi}")
    return v


def _range(r, what):
    lo, hi = (r or [0, 127])[:2]
    lo, hi = _clamp(lo, 0, 127, what), _clamp(hi, 0, 127, what)
    if lo > hi:
        raise ValueError(f"{what}: min is above max")
    return [lo, hi]


def _obj(v, what):
    """A sub-object of a mapper request: a dict (or absent). Anything else is a
    bad request (ValueError), never an AttributeError deep in the validator."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"mapper {what} must be an object")
    return v


def mapper_validate(m):
    """The app's own rules (it only ever writes a COMPLETE mapper, and some
    choices force others). Returns the normalised mapper or raises ValueError
    (also for a malformed request: not an object, a sub-field of the wrong kind)."""
    if not isinstance(m, dict):
        raise ValueError("mapper must be an object (or 'clear')")
    src, dst = _obj(m.get("src"), "source"), _obj(m.get("dst"), "target")
    if not isinstance(m.get("inputs") or [], (list, tuple)):
        raise ValueError("mapper inputs must be a list")
    # no inputs is allowed: that IS the app's "Disable inputs" state (it writes it)
    ins = sorted({_clamp(i, 0, 5, "input") for i in m.get("inputs") or []})
    st = src.get("type")
    if st is None:
        raise ValueError("pick a source message type")
    st = _clamp(st, 0, 8, "source type")
    dt = 0 if st in (SRC_TRANSPOSE, SRC_GLOBAL_CH) else dst.get("type")   # app: fixed there
    if dt is None:
        raise ValueError("pick a target message type")
    dt = _clamp(dt, 0, 7, "target type")
    cmin = _clamp(src.get("ch_min", 1), 1, 16, "channel")
    cmax = _clamp(src.get("ch_max", 16), 1, 16, "channel")
    if cmin > cmax:
        raise ValueError("source channel min is above max")
    fol = _obj(m.get("follow"), "follow")
    out = {"inputs": ins,
           "src": {"type": st, "ch_min": cmin, "ch_max": cmax,
                   "v1": _range(src.get("v1"), "source value 1"),
                   "v2": _range(src.get("v2"), "source value 2")},
           "dst": {"type": dt, "ch": _clamp(dst.get("ch", 1), 1, 16, "target channel"),
                   "v1": _range(dst.get("v1"), "target value 1"),
                   "v2": _range(dst.get("v2"), "target value 2")},
           "keep_original": bool(m.get("keep_original")), "transpose_ch": bool(m.get("transpose_ch")),
           "follow": {"ch": bool(fol.get("ch", True)), "v1": bool(fol.get("v1", True)),
                      "v2": bool(fol.get("v2", True))},
           "skip": _clamp(m.get("skip", 0), 0, 100, "random skip %")}
    for v in ("v1", "v2"):
        o = _obj(m.get(v), v)
        out[v] = {"use_other": bool(o.get("use_other")), "invert": bool(o.get("invert")),
                  "compress": bool(o.get("compress")),
                  "curve": _clamp(o.get("curve", 0), 0, 15, "curve")}
    if out["transpose_ch"]:
        out["follow"]["ch"] = False          # the app clears channel Follow with it
    if st == SRC_TRANSPOSE:
        t = _obj(m.get("transpose"), "transpose")
        out["transpose"] = {"semitone": _clamp(t.get("semitone", 0), -48, 48, "semitone"),
                            "octave": _clamp(t.get("octave", 0), -4, 4, "octave")}
        out["follow"]["v1"] = False          # app: value-1 Follow is greyed in this mode
    if st == SRC_GLOBAL_CH:
        out["follow"]["ch"] = out["follow"]["v1"] = False   # app: the target channel IS the point
    return out


def mapper_encode(m, base=None):
    """Bytes for a validated mapper. Bits we can't name come from `base` (the
    entry as the box has it) untouched; base None = the factory default entry."""
    m = mapper_validate(m)
    bw = _words(base if base is not None else [int(x, 16) for x in DEFAULT_MAPPER.split()])
    src, dst = m["src"], m["dst"]
    w0 = (src["v1"][1] << 24) | (src["v1"][0] << 16) | (src["v2"][1] << 8) | src["v2"][0]
    if src["type"] == SRC_TRANSPOSE:
        hi = ((m["transpose"]["semitone"] + 48) << 24) | ((m["transpose"]["octave"] + 4) << 16)
    else:
        hi = (dst["v1"][1] << 24) | (dst["v1"][0] << 16)
    w1 = hi | (dst["v2"][1] << 8) | dst["v2"][0]
    w2 = (src["type"] | (src["ch_min"] - 1) << 4 | (src["ch_max"] - 1) << 8
          | sum(1 << (12 + i) for i in m["inputs"] if i < 3)
          | sum(1 << (16 + i - 3) for i in m["inputs"] if i >= 3)
          | dst["type"] << 20 | (dst["ch"] - 1) << 24
          | int(m["keep_original"]) << 28 | int(m["transpose_ch"]) << 29)
    w2 |= bw[2] & ~KNOWN_W2 & 0xFFFFFFFF
    f, a, b = m["follow"], m["v1"], m["v2"]
    w3 = (int(f["ch"]) | int(f["v1"]) << 1 | int(f["v2"]) << 2 | int(a["use_other"]) << 3
          | int(b["use_other"]) << 4 | int(a["invert"]) << 5 | int(b["invert"]) << 6
          | int(a["compress"]) << 7 | int(b["compress"]) << 8 | m["skip"] << 9
          | a["curve"] << 16 | b["curve"] << 20)
    w3 |= bw[3] & ~KNOWN_W3 & 0xFFFFFFFF
    return _raw([w0, w1, w2, w3])


def mapper_describe(m):
    """One line a human can read: what this mapper does."""
    if m.get("default"):
        return "unused"
    ins = ", ".join(ROUTER_INPUTS[i] for i in m["inputs"]) or "(no input)"
    src, dst = m["src"], m["dst"]
    if src["type"] is None or dst["type"] is None:
        return f"incomplete (inputs: {ins})"
    ch = f"ch {src['ch_min']}" + (f"-{src['ch_max']}" if src["ch_max"] != src["ch_min"] else "")
    s = f"{SRC_TYPES[src['type']]} on {ins}, {ch}"
    n1 = VALUE_NAMES.get(src["type"], (None, None))[0]
    if n1 and src["v1"] != [0, 127]:
        s += f" ({n1} {src['v1'][0]}-{src['v1'][1]})"
    if src["type"] == SRC_TRANSPOSE:
        t = m.get("transpose", {})
        s += f" -> transposed {t.get('octave', 0):+d} oct {t.get('semitone', 0):+d} st"
    elif src["type"] == SRC_GLOBAL_CH:
        s += f" -> all to ch {dst['ch']}"
    else:
        s += f" -> {DST_TYPES[dst['type']]}"
        if not m["follow"]["ch"]:
            s += f" ch {dst['ch']}"
        d1 = VALUE_NAMES.get(dst["type"], (None, None))[0]
        if d1 and not m["follow"]["v1"] and dst["v1"] != [0, 127]:
            s += f" ({d1} {dst['v1'][0]}-{dst['v1'][1]})"
    extra = [x for x, on in (("keeps original", m["keep_original"]),
                             ("transposes channel", m["transpose_ch"]),
                             ("inverted", m["v1"]["invert"] or m["v2"]["invert"]),
                             (f"curve User {curve_user(m['v1']['curve']) or curve_user(m['v2']['curve'])}",
                              curve_user(m["v1"]["curve"]) or curve_user(m["v2"]["curve"])),
                             (f"skips {m['skip']}%", m["skip"])) if on]
    if not m["inputs"]:
        extra.insert(0, "DISABLED (no input)")
    return s + ("; " + ", ".join(extra) if extra else "")


def curve_decode(raw160):
    out = []
    for i in range(0, 160, 5):
        out += list(word_decode(raw160[i:i + 5]).to_bytes(4, "big"))
    return out


def hx(data):
    return " ".join(f"{b:02X}" for b in data)


# ── the /cme page's request -> a brain command (pure; orc1_web calls it) ─────

def _preset_no(p, extra=()):
    if isinstance(p, bool) or p not in (1, 2, 3, 4) + tuple(extra):
        raise ValueError("preset must be 1-4")
    return p


def web_command(d):
    """POST /api/cme body -> (brain action, payload). ValueError = a 400, for ANY
    malformed body (never a 500). Edits (route/filter/mapper) may carry
    "preset" = the preset the page computed them from (sent on as "on_preset":
    the link refuses if the box switched since); any request may carry "job"
    (the page's id for this click, echoed back in state["last"])."""
    if not isinstance(d, dict):
        raise ValueError("bad request")
    try:
        what, extra = d.get("what"), {}
        job = d.get("job")
        if job is not None:
            if not isinstance(job, str) or not 0 < len(job) <= 40:
                raise ValueError("bad job id")
            extra["job"] = job
        if what in ("route", "filter", "mapper") and d.get("preset") is not None:
            extra["on_preset"] = _preset_no(d.get("preset"))
        if what == "preset":
            return "CME_PRESET", dict(extra, preset=_preset_no(d.get("preset"), ("next", "prev")))
        if what == "route":
            try:
                inp = int(d.get("input"))
                outs = sorted({int(o) for o in d.get("outputs") or []})
            except (TypeError, ValueError):
                raise ValueError("bad route")
            if not 0 <= inp <= 5 or any(not 0 <= o <= 5 for o in outs):
                raise ValueError("route out of range")
            return "CME_ROUTE", dict(extra, input=inp, outputs=outs)
        if what == "filter":
            names = {n for n, _b, _bit in FILTER_TYPES}
            try:
                port = int(d.get("port"))
                chs = sorted({int(c) for c in d.get("channels") or []})
                tys = sorted({str(t) for t in d.get("types") or []})
            except (TypeError, ValueError):
                raise ValueError("bad filter")
            if not 0 <= port <= 11 or any(not 1 <= c <= 16 for c in chs) or not set(tys) <= names:
                raise ValueError("filter out of range")
            return "CME_FILTER", dict(extra, port=port, channels=chs, types=tys)
        if what == "mapper":
            idx = int(d.get("index"))
            if not 0 <= idx <= 15:
                raise ValueError("mapper must be 1-16")
            mp = d.get("mapper")
            mp = "clear" if mp == "clear" else mapper_validate(mp)
            return "CME_MAPPER", dict(extra, index=idx, mapper=mp)
        if what == "refresh":
            return "CME_REFRESH", extra
    except (TypeError, AttributeError, KeyError) as e:
        raise ValueError(f"bad request ({e})")
    raise ValueError("unknown request")


# ── the link (owned by the brain) ────────────────────────────────────────────

class CmeLink:
    def __init__(self, prefix="u6midi", state_path="~/.orc1_cme.json",
                 mido_mod=None, timeout=0.3, log=print):
        self.prefix = prefix.lower()
        self.state_path = os.path.expanduser(state_path)
        self._mido = mido_mod
        self.timeout = timeout
        self.log = log
        self._out = self._in = None
        self._replies = queue.Queue()
        self._lock = threading.Lock()          # one exchange at a time
        self._jobs = queue.Queue()             # work runs on ONE worker, in order
        self._worker = None
        self._wlock = threading.Lock()         # guards the worker check-then-start
        self.state = {"connected": False}
        self._start_worker()                   # up front: no first-submit race at all

    # -- port handling --
    @property
    def mido(self):
        if self._mido is None:
            import mido
            self._mido = mido
        return self._mido

    def matches(self, name):
        return self.prefix in (name or "").lower()

    def _port1(self, names):
        for n in names:
            nl = n.lower()
            if self.prefix in nl and "port 1" in nl:
                return n
        return None

    def _on_msg(self, msg):
        if msg.type == "sysex" and list(msg.data[:len(HDR)]) == HDR:
            self._replies.put(list(msg.data))
        # everything else on this port is music for other gear: dropped here

    def close(self):
        for p in (self._in, self._out):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        self._in = self._out = None

    def _open(self):
        self.close()
        o = self._port1(self.mido.get_output_names())
        i = self._port1(self.mido.get_input_names())
        if not (o and i):
            return False
        self._out = self.mido.open_output(o)
        self._in = self.mido.open_input(i, callback=self._on_msg)
        return True

    # -- one request / reply --
    def _exchange(self, op, payload, addr=None, refusals=False):
        """Send one frame, return the reply payload (after any address echo
        check), or None on no reply. Reopens the port once if it went quiet.
        With `addr`, a reply for a DIFFERENT address (a stale one) is skipped,
        and a 7C/7E refusal is final: None, or with `refusals` the bare [7C] /
        [7E] (a refusal carries no address to check; it is never resent)."""
        with self._lock:
            for attempt in (0, 1):
                if self._out is None or attempt:
                    try:
                        if not self._open():
                            return None
                    except Exception:
                        return None
                while not self._replies.empty():
                    self._replies.get_nowait()
                try:
                    self._out.send(self.mido.Message("sysex", data=frame(op, payload)))
                except Exception:
                    continue
                end = time.monotonic() + self.timeout
                while time.monotonic() < end:
                    try:
                        data = self._replies.get(timeout=max(0.0, end - time.monotonic()))
                    except queue.Empty:
                        break
                    p = parse(data)
                    if not p or not reply_matches(p[0], op):
                        continue
                    rp = p[1]
                    if addr is not None and rp[:1] in ([ERR], [BAD]):
                        return rp[:1] if refusals else None   # the box said no: final, never resent
                    if addr is not None:
                        if rp[:len(addr)] != list(addr):
                            continue
                        rp = rp[len(addr):]
                    return rp
            return None

    def hello(self):
        return self._exchange(OP_HELLO, [0x00])

    def read(self, sec, words, idx):
        """Value bytes, or None = no answer. Raises LookupError on 7C."""
        # The expected address makes _exchange SKIP a stale reply to an earlier
        # read (it used to take the first 52 reply it saw, then give up = None).
        rp = self._exchange(OP_READ, [sec, words, idx], addr=[sec, words, idx], refusals=True)
        if rp is None or rp == [BAD]:
            return None
        if rp == [ERR]:
            raise LookupError(f"CME says no such address {sec:02X}/{words}/{idx}")
        return rp

    def write(self, sec, words, idx, value):
        rp = self._exchange(OP_WRITE, [sec, words, idx] + list(value), addr=[sec, words, idx])
        return rp is not None

    # -- the config, as the box has it --
    def refresh(self):
        st = {"connected": False, "updated": time.time()}
        ident = self.hello()
        if ident is None:
            self.state = st
            self.publish()
            return st
        st["connected"] = True
        st["identity"] = hx(ident)
        raw = {}
        try:
            for key, sec, words, n in SECTIONS:
                rows = []
                for idx in range(n):
                    v = self.read(sec, words, idx)
                    if v is None:
                        raise IOError(f"no reply reading {key}[{idx}]")
                    rows.append(v)
                raw[key] = rows
        except Exception as e:
            # The box ANSWERED (connected) but its config could not be read in
            # full: publish PARTIAL, with no config at all, so the page offers
            # no editor built from missing data (one click on an empty router
            # row used to write a one-cell mask = wipe that row on the box).
            st["partial"] = True
            st["error"] = str(e)
            self.state = st
            self.publish()
            return st
        st["preset"] = raw["global"][0][0] + 1
        st["global"] = hx(raw["global"][0])
        st["router"] = [mask_to_outputs(*r[:2]) for r in raw["router"]]
        st["preset_names"] = [text_decode(r) for r in raw["names"]]
        st["device_name"] = text_decode(raw["device"][0][:15])
        st["filters"] = [{"channels": filter_ch_decode(raw["filt_ch"][i]),
                          "types": filter_ty_decode(raw["filt_ty"][i]),
                          "unknown_bits": any(raw["filt_ty"][i][3:5])}
                         for i in range(12)]
        # "filtered" = something is actually BLOCKED (unknown bits alone block nothing)
        st["filters_set"] = [i for i, f in enumerate(st["filters"]) if f["channels"] or f["types"]]
        st["mappers_set"] = [i for i, r in enumerate(raw["mappers"]) if hx(r) != DEFAULT_MAPPER]
        st["mappers"] = []
        for r in raw["mappers"]:
            m = mapper_decode(r)
            m["text"] = mapper_describe(m)
            st["mappers"].append(m)
        st["curves"] = [curve_decode(r) for r in raw["curves"]]
        st["raw"] = {k: [hx(r) for r in v] for k, v in raw.items()}
        self.state = st
        self.publish()
        return st

    def set_preset(self, want):
        """want = 1-4, "next" or "prev". Writes ONLY byte 0 of the global record
        (bytes 1-4 = the Settings page go back exactly as read), then re-reads."""
        g = self.read(0x04, 1, 0)
        if not g or len(g) != 5:
            raise IOError("could not read the CME global record -- not writing")
        cur = g[0] + 1
        if want in ("next", "prev"):
            n = (cur % 4) + 1 if want == "next" else ((cur - 2) % 4) + 1
        else:
            n = int(want)
        if not 1 <= n <= 4:
            raise ValueError("CME preset must be 1-4")
        try:
            if n != cur:
                if not self.write(0x04, 1, 0, [n - 1] + g[1:]):
                    raise IOError("CME did not acknowledge the preset write")
        finally:
            st = self.refresh()     # even after a lost ack: show what the box holds
        if st.get("preset") != n:
            raise IOError(f"CME preset read back {st.get('preset')}, wanted {n}")
        return n

    def _expect_preset(self, preset):
        """An edit is computed from ONE preset's config, but every section edits
        the ACTIVE preset. If the box switched since (FCB cmd:CME_PRESET, another
        tab, the box's own button), refuse: the edit would land on the wrong
        preset. preset None = the caller didn't say (brain/pedal): no check."""
        if preset is None:
            return
        want = int(preset)
        g = self.read(0x04, 1, 0)
        if not g or len(g) != 5:
            raise IOError("could not read the CME's active preset -- not writing")
        if g[0] + 1 != want:
            raise IOError(f"the CME is on preset {g[0] + 1} now, not {want} -- "
                          "NOT written; the page has re-read it, check and try again")

    def set_route(self, inp, outputs, preset=None):
        """Router entry for input `inp` (0-5) on the ACTIVE preset = `outputs`.
        `preset` (1-4) = the preset the edit was made on; refused if not active."""
        inp = int(inp)
        if not 0 <= inp <= 5:
            raise ValueError("router input must be 0-5")
        lo, hi = outputs_to_mask(outputs)
        try:
            self._expect_preset(preset)
            if not self.write(0x00, 1, inp, [lo, hi]):
                raise IOError("CME did not acknowledge the router write")
            back = self.read(0x00, 1, inp)
            if not back or back[:2] != [lo, hi]:
                raise IOError(f"router read back {hx(back or [])}, wanted {lo:02X} {hi:02X}")
        finally:
            self.refresh()      # a lost ack / failed read-back must not leave a stale page
        return self.state

    def set_filter(self, port, channels, types, preset=None):
        """Filter for `port` (0-11, FILTER_PORTS) on the ACTIVE preset: block these
        channels (1-16) and these message types (FILTER_TYPES names). The type
        mask's unknown bytes 3-4 are written back exactly as read."""
        port = int(port)
        if not 0 <= port <= 11:
            raise ValueError("filter port must be 0-11")
        chv = filter_ch_encode(channels)
        filter_ty_encode(types)         # bad type names fail before the box is touched
        try:
            self._expect_preset(preset)
            cur = self.read(0x02, 1, port)
            if not cur or len(cur) != 5:
                raise IOError("could not read the filter -- not writing")
            tyv = filter_ty_encode(types, keep=cur)
            if not self.write(0x01, 1, port, chv):
                raise IOError("CME refused the channel filter write")
            if not self.write(0x02, 1, port, tyv):
                raise IOError("CME refused the type filter write (the channel part WAS written)")
            back = (self.read(0x01, 1, port), self.read(0x02, 1, port))
            if back != (chv, tyv):
                raise IOError(f"filter read back {back}, wanted {(chv, tyv)}")
        finally:
            self.refresh()      # e.g. part one written, part two failed: show the truth
        return f"{FILTER_PORTS[port]}: {len(channels)} ch, {len(types)} types blocked"

    def set_mapper(self, index, mapper, preset=None):
        """Mapper `index` (0-15) on the ACTIVE preset. `mapper` = a mapper dict
        (validated with the app's rules) or "clear" (the factory default entry,
        exactly what the app's own reset writes for a mapper)."""
        index = int(index)
        if not 0 <= index <= 15:
            raise ValueError("mapper must be 0-15")
        if mapper != "clear":
            mapper_validate(mapper)     # a bad request fails before the box is touched
        try:
            self._expect_preset(preset)
            if mapper == "clear":
                raw = [int(x, 16) for x in DEFAULT_MAPPER.split()]
            else:
                cur = self.read(0x03, 4, index)
                if not cur or len(cur) != 20:
                    raise IOError("could not read the mapper -- not writing")
                raw = mapper_encode(mapper, base=cur)
            if not self.write(0x03, 4, index, raw):
                raise IOError("CME refused the mapper write")
            back = self.read(0x03, 4, index)
            if back != raw:
                raise IOError(f"mapper read back {hx(back or [])}, wanted {hx(raw)}")
        finally:
            self.refresh()
        return f"mapper {index + 1}: {mapper_describe(mapper_decode(raw))}"

    # -- publish for the web --
    def publish(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            self.log(f"  ! cme state file: {e}")

    # -- run box work off the caller's thread, one job at a time --
    def _start_worker(self):
        with self._wlock:       # check-then-start: two submitters must not start two
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True,
                                                name="cme-worker")
                self._worker.start()

    def submit(self, label, fn, job=None):
        """Queue `fn` on the worker. `job` (the page's id for its own click) is
        echoed in state["last"], so the page waits for ITS result and not for a
        hot-plug / pedal job that happened to finish first."""
        self._start_worker()
        self._jobs.put((label, fn, job))

    def _work(self):
        while True:
            label, fn, job = self._jobs.get()
            try:
                res = fn()
                self.state["last"] = {"what": label, "ok": True, "t": time.time(), "job": job}
                if isinstance(res, dict):       # a refresh: summarise, don't dump
                    res = (f"preset {res.get('preset')}" if res.get("connected")
                           else "not connected")
                self.log(f"  [cme] {label}: ok" + (f" ({res})" if res is not None else ""))
            except Exception as e:
                self.state["last"] = {"what": label, "ok": False, "error": str(e),
                                      "t": time.time(), "job": job}
                self.log(f"  ! [cme] {label}: {e}")
            self.publish()
