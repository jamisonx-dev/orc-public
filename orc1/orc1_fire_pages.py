"""
orc1_fire_pages.py -- the Akai Fire's UNIVERSAL pages, stored ON the Fire (2026-10-27).

Our Fire firmware (v0.31+, hello version 3) keeps 8 UNIVERSAL pages in its own flash:
each pad (two layers: BASE and SHIFT) sends a note / CC / program change, and 4 banks x
4 knobs send a CC or pitch bend. The /control page edits them; the brain carries the
SysEx (it owns the "ORC FW seq" port through orc1_scenes.FireBarLink).

Byte layout and protocol: fire_firmware fw/fireseq/USER_GUIDE.md ("The host protocol",
commands 10-15) and uni.h / uni.c / main.c (uni_host_*, HC_UREAD, HC_ULIST) on the
`universal-mode` branch. Cross-checked against the C, not only the doc:
  - uni_check / entry_ok: version 1, name ASCII 32-126, pad type 1-6, colour 0-16,
    knob type 1-2, knob channel 0-15, knob byte 5 = 0, no duplicate pad+layer / knob;
  - uni_sum: the plain byte sum & 0x3FFF;
  - uni_chunk_check: chunks in order, 24 bytes each except the last;
  - uni_ack: answers <cmd> <result> <page> <done hi> <done lo>.

Everything here is pure apart from the injected `send` / `version` callables, so
_test_fire_pages.py drives it against a fake Fire.

Threads: the brain's MIDI input thread calls handle_sysex() for every SysEx; it only
puts our replies on a queue and returns at once. The socket thread runs one
transaction at a time (self._io), sending a request and waiting on that queue.
"""

import queue
import threading
import time

ORC_HDR = [0x7D, 0x4F, 0x52, 0x43]           # F0 7D 'O' 'R' 'C' <cmd> ... F7
CMD_BEGIN, CMD_CHUNK, CMD_COMMIT, CMD_READ, CMD_LIST, CMD_CLEAR = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15
PAGE_CMDS = (CMD_BEGIN, CMD_CHUNK, CMD_COMMIT, CMD_READ, CMD_LIST, CMD_CLEAR)
PAGES_VERSION = 3                              # hello version that has 10-15 (fw v0.31)

PAGES, HDR, ENT, PADS, KNOBS = 8, 9, 6, 64, 16
MAX_LEN = HDR + ENT * (PADS * 2 + KNOBS)       # 873
BUDGET = 4096                                  # all 8 pages together (the Fire's own figure is in 14)
CHUNK = 24
VER = 1
NAME_LEN = 8
GREY = 16

# pad types, in the firmware's numbering (uni.h UT_*: 0 = off = no entry)
PAD_TYPES = ["note", "note_latch", "cc_hold", "cc_toggle", "cc_press", "pc"]     # codes 1..6
PAD_TYPE_LABEL = {"note": "NOTE", "note_latch": "NOTE LATCH", "cc_hold": "CC HOLD",
                  "cc_toggle": "CC TOGGLE", "cc_press": "CC PRESS", "pc": "PROG CHANGE"}
KNOB_TYPES = ["cc", "pb"]                                                         # codes 1..2
# the fader palette (main.c FAD_COL_NAME, same order) + grey (uni.c UNI_COL_NAME)
COLOURS = ["red", "orange", "amber", "yellow", "lime", "green", "mint", "teal",
           "cyan", "sky", "blue", "indigo", "purple", "magenta", "pink", "white", "grey"]
# their RGB on the Fire (main.c FAD_RGB, brightness 0-7; grey = MR_GREY)
COLOUR_RGB7 = [(7, 0, 0), (7, 2, 0), (7, 4, 0), (6, 6, 0), (3, 7, 0), (0, 7, 0), (0, 7, 3), (0, 6, 5),
               (0, 5, 7), (0, 3, 7), (0, 0, 7), (3, 0, 7), (5, 0, 7), (7, 0, 6), (7, 2, 4), (6, 6, 6),
               (3, 3, 3)]

RESULTS = {
    0: "ok",
    1: "the Fire refused the request (bad arguments)",
    2: "the Fire is busy (another write or a save is running)",
    3: "out of order (the Fire lost track of this write)",
    4: "wrong checksum (the page arrived damaged)",
    5: "the Fire found the page invalid",
    6: "no room: all 8 pages together are over the Fire's budget",
    7: "the Fire's save area needs a clean-up, which it only does while stopped: stop the Fire and send again",
    8: "the Fire could not write its flash",
}
R_OK, R_BAD, R_BUSY, R_ORDER, R_SUM, R_INVALID, R_FULL, R_ROOM, R_FAIL = range(9)


def result_text(code):
    return RESULTS.get(code, f"unknown answer {code} from the Fire")


class PageError(ValueError):
    """A page that the Fire would refuse (said in plain words)."""


# ---------- numbers ----------

def split14(n):
    n = int(n)
    if not 0 <= n < 16384:
        raise PageError(f"{n} does not fit in 14 bits")
    return [(n >> 7) & 0x7F, n & 0x7F]


def join14(hi, lo):
    return ((hi & 0x7F) << 7) | (lo & 0x7F)


def page_sum(b):
    """uni_sum: every byte added up, 14 bits."""
    return sum(b) & 0x3FFF


def len_ok(n):
    return HDR <= n <= MAX_LEN and (n - HDR) % ENT == 0


def chunks(b):
    """[(offset, bytes)]: 24 bytes each except the last (uni_chunk_check)."""
    return [(o, list(b[o:o + CHUNK])) for o in range(0, len(b), CHUNK)]


# ---------- the page format ----------

def _int(v, what, lo, hi):
    try:
        if isinstance(v, bool):
            raise ValueError
        n = int(v)
        if isinstance(v, float) and v != n:
            raise ValueError
    except (TypeError, ValueError):
        raise PageError(f"{what} must be a whole number, got {v!r}")
    if not lo <= n <= hi:
        raise PageError(f"{what} must be {lo}-{hi}, got {n}")
    return n


def _layer(v, what):
    if v in ("base", "BASE"):
        return 0
    if v in ("shift", "SHIFT"):
        return 1
    return _int(v, what, 0, 1)


def clean_name(name):
    name = "" if name is None else str(name)
    if len(name) > NAME_LEN:
        raise PageError(f"name is {len(name)} characters, at most {NAME_LEN}")
    for ch in name:
        if not 0x20 <= ord(ch) <= 0x7E:
            raise PageError(f"name: {ch!r} is not a plain letter, digit or sign")
    return name


def encode_page(page):
    """A page dict -> the bytes the Fire stores.

    page = {"name": "DRUMS",
            "pads":  [{"pad": 0-63, "layer": 0 (BASE) / 1 (SHIFT), "type": PAD_TYPES,
                       "channel": 1-16, "number": 0-127, "on": 0-127, "off": 0-127,
                       "colour": 0-16}],
            "knobs": [{"slot": 0-15 (bank * 4 + knob), "type": "cc" / "pb",
                       "channel": 1-16, "cc": 0-127, "start": 0-127}]}
    Channels are 1-16 here (as everywhere on the web), 0-15 on the wire. Entries go
    out sorted (pads by pad + layer, then knobs by slot), so equal pages encode equal."""
    if not isinstance(page, dict):
        raise PageError("a page must be an object")
    name = clean_name(page.get("name", ""))
    out = [VER] + [ord(c) for c in name.ljust(NAME_LEN)]
    seen = set()
    pads = page.get("pads") or []
    knobs = page.get("knobs") or []
    if not isinstance(pads, list) or not isinstance(knobs, list):
        raise PageError("pads and knobs must be lists")
    ents = []
    for p in pads:
        if not isinstance(p, dict):
            raise PageError("each pad must be an object")
        pad = _int(p.get("pad"), "pad", 0, PADS - 1)
        layer = _layer(p.get("layer", 0), f"pad {pad} layer")
        where = f"pad {pad} ({'SHIFT' if layer else 'BASE'})"
        t = p.get("type")
        if t not in PAD_TYPES:
            raise PageError(f"{where}: type must be one of {', '.join(PAD_TYPES)}, got {t!r}")
        ch = _int(p.get("channel", 1), f"{where} channel", 1, 16)
        num = _int(p.get("number", 0), f"{where} number", 0, 127)
        on = _int(p.get("on", 127), f"{where} on value", 0, 127)
        off = _int(p.get("off", 0), f"{where} off value", 0, 127)
        col = _int(p.get("colour", GREY), f"{where} colour", 0, GREY)
        key = ("p", pad, layer)
        if key in seen:
            raise PageError(f"{where} is set twice")
        seen.add(key)
        ents.append(((0, pad, layer), [pad, layer | (col << 1),
                                       ((PAD_TYPES.index(t) + 1) << 4) | (ch - 1), num, on, off]))
    for k in knobs:
        if not isinstance(k, dict):
            raise PageError("each knob must be an object")
        slot = _int(k.get("slot"), "knob slot", 0, KNOBS - 1)
        where = f"knob {slot // 4 + 1}.{slot % 4 + 1}"
        t = k.get("type")
        if t not in KNOB_TYPES:
            raise PageError(f"{where}: type must be cc or pb, got {t!r}")
        ch = _int(k.get("channel", 1), f"{where} channel", 1, 16)
        cc = _int(k.get("cc", 0), f"{where} CC", 0, 127)
        start = _int(k.get("start", 64 if t == "pb" else 0), f"{where} start value", 0, 127)
        key = ("k", slot)
        if key in seen:
            raise PageError(f"{where} is set twice")
        seen.add(key)
        ents.append(((1, slot, 0), [PADS + slot, KNOB_TYPES.index(t) + 1, ch - 1, cc, start, 0]))
    for _, e in sorted(ents, key=lambda x: x[0]):
        out.extend(e)
    check_bytes(out)
    return out


def check_bytes(b):
    """uni_check in Python: raises PageError for anything the Fire would refuse."""
    b = list(b)
    if not len_ok(len(b)):
        raise PageError(f"a page is 9 + 6 x entries bytes, at most {MAX_LEN}; got {len(b)}")
    if any((not isinstance(x, int)) or x < 0 or x > 127 for x in b):
        raise PageError("every byte must be 0-127")
    if b[0] != VER:
        raise PageError(f"page version {b[0]}, this ORC knows version {VER}")
    if any(not 0x20 <= x <= 0x7E for x in b[1:HDR]):
        raise PageError("the name has a character that is not plain ASCII")
    seen = set()
    for o in range(HDR, len(b), ENT):
        e = b[o:o + ENT]
        if e[0] < PADS:
            t = e[2] >> 4
            if (e[1] >> 1) > GREY:
                raise PageError(f"pad {e[0]}: colour {e[1] >> 1} (0-16)")
            if not 1 <= t <= len(PAD_TYPES):
                raise PageError(f"pad {e[0]}: type code {t} (1-{len(PAD_TYPES)})")
            key = e[0] * 2 + (e[1] & 1)
        elif e[0] < PADS + KNOBS:
            if not 1 <= e[1] <= len(KNOB_TYPES) or e[2] > 15 or e[5] != 0:
                raise PageError(f"knob slot {e[0] - PADS}: bad entry {e}")
            key = 2 * PADS + e[0] - PADS
        else:
            raise PageError(f"entry at byte {o}: {e[0]} is neither a pad (0-63) nor a knob (64-79)")
        if key in seen:
            raise PageError(f"entry at byte {o} repeats a pad / knob already set")
        seen.add(key)
    return b


def decode_page(b):
    """The Fire's bytes -> a page dict (the shape encode_page takes)."""
    b = check_bytes(b)
    out = {"name": "".join(chr(x) for x in b[1:HDR]).rstrip(" "), "pads": [], "knobs": []}
    for o in range(HDR, len(b), ENT):
        e = b[o:o + ENT]
        if e[0] < PADS:
            out["pads"].append({"pad": e[0], "layer": e[1] & 1, "colour": e[1] >> 1,
                                "type": PAD_TYPES[(e[2] >> 4) - 1], "channel": (e[2] & 15) + 1,
                                "number": e[3], "on": e[4], "off": e[5]})
        else:
            out["knobs"].append({"slot": e[0] - PADS, "type": KNOB_TYPES[e[1] - 1],
                                 "channel": e[2] + 1, "cc": e[3], "start": e[4]})
    out["pads"].sort(key=lambda p: (p["pad"], p["layer"]))
    out["knobs"].sort(key=lambda k: k["slot"])
    return out


def empty_page():
    return [VER] + [0x20] * NAME_LEN


# ---------- frames (mido sysex data: no F0 / F7) ----------

def frame_begin(page, n):
    return ORC_HDR + [CMD_BEGIN, page] + split14(n)


def frame_chunk(off, data):
    return ORC_HDR + [CMD_CHUNK] + split14(off) + list(data)


def frame_commit(s):
    return ORC_HDR + [CMD_COMMIT] + split14(s)


def frame_read(page, off):
    return ORC_HDR + [CMD_READ, page] + split14(off)


def frame_list():
    return ORC_HDR + [CMD_LIST]


def frame_clear(page):
    return ORC_HDR + [CMD_CLEAR, page]


def parse_ack(d):
    """10 / 11 / 12 / 15 answer -> {cmd, result, page, done}."""
    return {"cmd": d[4], "result": d[5], "page": d[6], "done": join14(d[7], d[8])}


def parse_read(d):
    """13 answer -> {result, page, len, off, data}."""
    return {"result": d[5], "page": d[6], "len": join14(d[7], d[8]), "off": join14(d[9], d[10]),
            "data": list(d[11:])}


def parse_list(d):
    """14 answer -> the info dict."""
    n = d[5]
    if len(d) < 13 + 12 * n:
        raise PageError(f"page list from the Fire is short ({len(d)} bytes for {n} pages)")
    pages = []
    for p in range(n):
        q = d[13 + 12 * p: 25 + 12 * p]
        pages.append({"page": p, "len": join14(q[0], q[1]), "sum": join14(q[2], q[3]),
                      "name": "".join(chr(x) if 0x20 <= x <= 0x7E else "?" for x in q[4:12]).rstrip(" "),
                      "entries": max(0, (join14(q[0], q[1]) - HDR) // ENT)})
    return {"pages": pages, "budget": join14(d[6], d[7]), "used": join14(d[8], d[9]),
            "writing": None if d[10] == 127 else d[10], "current": d[11], "showing": bool(d[12])}


# ---------- talking to the Fire ----------

class FirePages:
    """One page transaction at a time, replies matched by command (+ page / offset).

    send(data)  -> sends one SysEx (data without F0/F7) to the Fire; raises on failure.
    version()   -> (connected, hello version or None).
    is_ours(nm) -> True when a SysEx source port is our seq Fire.
    Every public call returns a dict with "ok" first (the brain's socket reply)."""

    REPLY_S = 2.5          # a chunk answer (the Fire drops a write silent for 3 s)
    BEGIN_S = 4.0          # begin / commit / clear may wait for a flash clean-up (~1-2 s)
    BUSY_S = 3.0           # keep retrying "busy" this long
    BUSY_WAIT_S = 0.05     # the guide: retry in ~50 ms
    LOCK_S = 0.5           # a second caller waits this long for the one running

    def __init__(self, send, version, is_ours, log=print, clock=time.monotonic, sleep=time.sleep):
        self.send, self.version, self.is_ours = send, version, is_ours
        self.log, self.clock, self.sleep = log, clock, sleep
        self._q = queue.Queue()
        self._io = threading.Lock()
        self.stray = 0                      # replies nobody was waiting for (late / unasked)

    # -- MIDI input thread --
    def handle_sysex(self, data, source_name=""):
        """True = a page reply from our Fire (queued, consumed). Never blocks."""
        d = list(data or ())
        if len(d) < 5 or d[:4] != ORC_HDR or d[4] not in PAGE_CMDS:
            return False
        if not self.is_ours(source_name):
            return False
        self._q.put(d)
        return True

    # -- plumbing --
    def _ready(self):
        """None when the Fire can take page commands, else the reason in words."""
        try:
            connected, v = self.version()
        except Exception as e:
            return f"cannot ask the Fire link ({e})"
        if not connected:
            return "no Fire with our firmware connected (FLAME seq / ORC FW seq)"
        if v is None:
            return "the Fire has not answered hello yet: try again in a few seconds"
        if v < PAGES_VERSION:
            return (f"Fire firmware too old: it says version {v}; UNIVERSAL pages need "
                    f"v0.31 or newer (version {PAGES_VERSION})")
        return None

    def _drain(self):
        while True:
            try:
                self._q.get_nowait()
                self.stray += 1
            except queue.Empty:
                return

    def _ask(self, frame, cmd, timeout, match=None):
        """Send one request, wait for the answer to `cmd` that `match`es. None = timeout."""
        self.send(frame)
        end = self.clock() + timeout
        while True:
            left = end - self.clock()
            if left <= 0:
                return None
            try:
                d = self._q.get(timeout=left)
            except queue.Empty:
                return None
            if d[4] == cmd and len(d) >= (11 if cmd == CMD_READ else 9) and (match is None or match(d)):
                return d
            self.stray += 1

    def _ask_busy(self, frame, cmd, timeout, match=None):
        """_ask, retrying while the Fire answers busy. -> (reply or None, tries)."""
        end = self.clock() + self.BUSY_S
        tries = 0
        while True:
            tries += 1
            d = self._ask(frame, cmd, timeout, match)
            if d is None or d[5] != R_BUSY or self.clock() >= end:
                return d, tries
            self.sleep(self.BUSY_WAIT_S)

    def _run(self, fn, *a):
        why = self._ready()
        if why:
            out = {"ok": False, "error": why}
            out.update(self.status())
            return out
        if not self._io.acquire(timeout=self.LOCK_S):
            return {"ok": False, "error": "busy: another page transfer to the Fire is running", "busy": True}
        try:
            self._drain()
            return fn(*a)
        except PageError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"sending to the Fire failed: {e}"}
        finally:
            self._io.release()

    @staticmethod
    def _page(page):
        return _int(page, "page", 0, PAGES - 1)

    # -- 14: info --
    def info(self):
        return self._run(self._info)

    def _info(self):
        d = self._ask(frame_list(), CMD_LIST, self.REPLY_S)
        if d is None:
            return {"ok": False, "error": "the Fire did not answer the page list"}
        out = {"ok": True}
        out.update(parse_list(d))
        return out

    # -- 13: read --
    def read(self, page):
        return self._run(self._read, self._page(page))

    def _read(self, page, tries=3):
        for _ in range(tries):
            got, total, off, moved = [], None, 0, False
            while total is None or off < total:
                o = off
                d = self._ask(frame_read(page, o), CMD_READ, self.REPLY_S,
                              lambda d, o=o: d[6] == page and join14(d[9], d[10]) == o)
                if d is None:
                    return {"ok": False, "page": page,
                            "error": f"the Fire stopped answering while reading page {page + 1} (at byte {o})"}
                r = parse_read(d)
                if r["result"] != R_OK:
                    return {"ok": False, "page": page, "error": result_text(r["result"]), "code": r["result"]}
                if total is None:
                    total = r["len"]
                elif r["len"] != total:     # edited on the Fire mid-read: start over
                    moved = True
                    break
                if not r["data"] and off < total:
                    return {"ok": False, "page": page, "error": f"the Fire sent no bytes at {off} of {total}"}
                got.extend(r["data"])
                off += len(r["data"])
            if moved:
                continue
            got = got[:total]
            out = {"ok": True, "page": page, "len": total, "sum": page_sum(got), "bytes": got}
            try:
                out["data"] = decode_page(got)
            except PageError as e:
                out["data"], out["invalid"] = None, str(e)
            return out
        return {"ok": False, "page": page, "error": "the page kept changing on the Fire while reading it"}

    # -- 10 / 11 / 12: write --
    def write(self, page, data):
        """data = a page dict (encoded here) or the raw bytes. Writes, then reads it back."""
        page = self._page(page)
        b = check_bytes(data) if isinstance(data, (list, tuple)) else encode_page(data)
        return self._run(self._write, page, b)

    def _fail(self, page, step, code=None, why=None, done=0, total=0):
        msg = why or result_text(code)
        out = {"ok": False, "page": page, "step": step, "error": f"{step}: {msg}", "done": done, "len": total}
        if code is not None:
            out["code"] = code
        self.log(f"[fire pages] page {page + 1} {step}: {msg}")
        return out

    def _write(self, page, b):
        n = len(b)
        d, _ = self._ask_busy(frame_begin(page, n), CMD_BEGIN, self.BEGIN_S, lambda d: d[6] == page)
        if d is None:
            return self._fail(page, "begin", why="the Fire did not answer", total=n)
        a = parse_ack(d)
        if a["result"] != R_OK:
            return self._fail(page, "begin", a["result"], total=n)
        for off, part in chunks(b):
            d, _ = self._ask_busy(frame_chunk(off, part), CMD_CHUNK, self.REPLY_S)
            if d is None:
                return self._fail(page, "send", why=f"the Fire stopped answering after {off} of {n} bytes",
                                  done=off, total=n)
            a = parse_ack(d)
            if a["result"] != R_OK:
                return self._fail(page, "send", a["result"], done=off, total=n)
            if a["done"] != off + len(part):
                return self._fail(page, "send", why=f"the Fire is out of step ({a['done']} bytes in flash, "
                                                    f"{off + len(part)} sent)", done=off, total=n)
        s = page_sum(b)
        d = self._ask(frame_commit(s), CMD_COMMIT, self.BEGIN_S)
        if d is None:
            return self._fail(page, "commit", why="the Fire did not confirm the page", done=n, total=n)
        a = parse_ack(d)
        if a["result"] != R_OK:
            return self._fail(page, "commit", a["result"], done=n, total=n)
        return self._verify(page, b, {"ok": True, "page": page, "len": n, "sum": s})

    def _verify(self, page, b, out):
        back = self._read(page)
        if not back.get("ok"):
            out.update(ok=False, verified=False,
                       error=f"saved, but reading it back failed: {back.get('error')}")
            return out
        if back["bytes"] != list(b):
            first = next((i for i, (x, y) in enumerate(zip(back["bytes"], b)) if x != y),
                         min(len(back["bytes"]), len(b)))
            out.update(ok=False, verified=False,
                       error=f"the Fire answered OK but its page differs from what was sent (byte {first})")
            return out
        out.update(verified=True, data=back.get("data"), bytes=back["bytes"])
        return out

    # -- 15: clear --
    def clear(self, page):
        return self._run(self._clear, self._page(page))

    def _clear(self, page):
        d, _ = self._ask_busy(frame_clear(page), CMD_CLEAR, self.BEGIN_S, lambda d: d[6] == page)
        if d is None:
            return self._fail(page, "clear", why="the Fire did not answer")
        a = parse_ack(d)
        if a["result"] != R_OK:
            return self._fail(page, "clear", a["result"])
        return self._verify(page, empty_page(), {"ok": True, "page": page, "len": HDR})

    def status(self):
        try:
            connected, v = self.version()
        except Exception:
            connected, v = False, None
        why = self._ready()
        return {"connected": bool(connected), "version": v, "ready": why is None, "why": why}


def socket_cmd(pages, cmd):
    """The brain's FIRE_PAGES_* socket actions -> one reply dict ("ok" first)."""
    a = cmd.get("action", "")
    try:
        if a == "FIRE_PAGES_INFO":
            r = pages.info()
            r.update(pages.status())
            return r
        if a == "FIRE_PAGE_READ":
            return pages.read(cmd.get("page"))
        if a == "FIRE_PAGE_WRITE":
            data = cmd.get("bytes") if cmd.get("bytes") is not None else cmd.get("data")
            return pages.write(cmd.get("page"), data)
        if a == "FIRE_PAGE_CLEAR":
            return pages.clear(cmd.get("page"))
    except PageError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown Fire page action {a}"}
