"""orc1_dawsurface_osc -- the REAPER OSC dialect for orc1_dawsurface, plus a stdlib OSC 1.0 codec.

REAPER talks to the ORC through its own OSC control surface, set up with the pattern file
orc1/daw/ORC.ReaperOSC (install steps in that file). Unlike Mackie Control (8 strips that REAPER
banks), ONE OSC device here sees every track BY NUMBER: the pattern file's DEVICE_TRACK_COUNT
(512, cfg.DAWSURF_OSC_TRACKS) is the bank size and the bank only moves when we say so
(DEVICE_TRACK_BANK_FOLLOWS DEVICE). Model strip i = REAPER track (page * n_strips + i + 1);
`surface_banking` is True, so the bridge keeps its own windows (views) into the strips and the
model's bank / channel gestures send nothing. Past n_strips the bridge pages: set_page(p) =
`/device/track/bank/select p+1` (REAPER's banks are 1-based) + a refresh, and the strips mean the
next n_strips tracks.

What was checked, and where (2026-11-09):
  * every address / flag below is a pattern from REAPER 7.81's Default.ReaperOSC (read on the Pi):
    n = normalized float 0-1 ("the track fader position in the REAPER window ... the actual
    volume ... depends on the REAPER track fader preference settings"), f = raw float (volume/db
    = dB), b = 0/1, t = toggle with no argument or 1, s = string, i = int; @ = the track's number
    inside the device's bank ("/bankedit 2 ... /track/1/volume as targeting the volume for
    track 9" with 8-track banks).
  * 41743 = the action "Control surface: Refresh all surfaces" (the string is in /opt/REAPER/reaper;
    the number from three independent users of REAPER OSC, not from REAPER's docs): REAPER re-sends
    every pattern = our hello.
  * NOT seen live yet (no REAPER was driven this round): what REAPER sends for a slot past the
    last track. The code takes an empty TRACK_NUMBER string (or no number and an empty name) as
    "no track", and a non-empty number OR name as a track; REAPER's tracks are contiguous, so the
    count = the highest slot holding one. Check on the rig: names of slots past the end.

LEVELS. REAPER's normalized volume is its own fader shape (preferences), the same shape its MCU
code uses (csurf_mcu.cpp: volToInt14 = DB2SLIDER(dB) * 16383 / 1000, DB2SLIDER being REAPER's
API), so 0 dB should sit at the MCU's measured 11730 / 16383 = 0.71599. Not assumed: the pattern
file asks for volume AND volume/db, and `unity01` is LEARNED from any pair REAPER reports at
0.00 dB (None until then; the bridge falls back on cfg.DAWSURF_UNITY_DAW). And out_fader_db sends
an exact dB (`/track/N/volume/db`), so the Fire's unity column lands on 0.0 dB whatever the shape.
The master has no dB pattern in REAPER's list: its unity tap goes as the learned position.

Messages in and out are (address, args) tuples; args may be plain Python values or typed
("f", 0.5) pairs (osc_message). The UDP socket belongs to the bridge's runtime, not to this file.
Stdlib only, no socket opened, no thread.
"""
import re
import struct

from orc1_dawsurface import Adapter, MASTER, OFF, ON

REFRESH_ACTION = 41743                 # "Control surface: Refresh all surfaces"
PAN_STEP = 0.01                        # one V-pot detent = 1 % of the pan range (= 2 % L/R)
UNITY_DB_TOL = 0.005                   # a dB report this close to 0 = REAPER's 0 dB
_TRACK = re.compile(r"^/track/(\d+)/(name|number/str|volume|volume/db|pan|mute|solo|recarm|select|vu)$")
_BUTTON_ADDR = {"mute": "mute", "solo": "solo", "rec": "recarm", "select": "select"}
_FROM_ADDR = {v: k for k, v in _BUTTON_ADDR.items()}
_TRANSPORT_T = {"play": "/play", "stop": "/stop", "record": "/record", "loop": "/repeat"}
_TRANSPORT_B = {"rewind": "/rewind", "fast_fwd": "/forward"}
_TRANSPORT_IN = dict([(v, k) for k, v in _TRANSPORT_T.items()] + [(v, k) for k, v in _TRANSPORT_B.items()])


# ------------------------------------------------------------------ OSC 1.0 codec
class OscError(ValueError):
    pass


def _pad(b):
    return b + b"\0" * (-len(b) % 4)


def _ostr(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return _pad(s + b"\0")


def _typed(a):
    """A value -> (tag, value). bool before int (bool is an int in Python)."""
    if isinstance(a, tuple) and len(a) == 2 and isinstance(a[0], str) and len(a[0]) == 1:
        return a
    if a is None:
        return ("N", None)
    if isinstance(a, bool):
        return ("T" if a else "F", None)
    if isinstance(a, int):
        return ("i", a)
    if isinstance(a, float):
        return ("f", a)
    if isinstance(a, (bytes, bytearray)):
        return ("b", bytes(a))
    if isinstance(a, str):
        return ("s", a)
    raise OscError("can't send %r over OSC" % (a,))


def osc_message(address, args=()):
    """One OSC message -> bytes. args: plain values (int -> i, float -> f, str -> s, bool -> T/F,
    None -> N, bytes -> b) or (tag, value) pairs to force a type."""
    if not address.startswith("/"):
        raise OscError("an OSC address starts with '/': %r" % address)
    tags, data = ",", b""
    for a in args:
        t, v = _typed(a)
        tags += t
        if t == "i":
            data += struct.pack(">i", int(v))
        elif t == "f":
            data += struct.pack(">f", float(v))
        elif t == "h":
            data += struct.pack(">q", int(v))
        elif t == "d":
            data += struct.pack(">d", float(v))
        elif t in "sS":
            data += _ostr(v)
        elif t == "b":
            data += struct.pack(">i", len(v)) + _pad(bytes(v))
        elif t not in "TFNI":
            raise OscError("unsupported OSC type tag %r" % t)
    return _ostr(address) + _ostr(tags) + data


def osc_bundle(elements, timetag=1):
    """A bundle of already-encoded messages / bundles (timetag 1 = immediately)."""
    out = _ostr("#bundle") + struct.pack(">Q", timetag)
    for e in elements:
        out += struct.pack(">i", len(e)) + e
    return out


def _rstr(data, i):
    end = data.find(b"\0", i)
    if end < 0:
        raise OscError("unterminated OSC string at %d" % i)
    s = data[i:end].decode("utf-8", "replace")
    return s, end + 1 + (-(end + 1 - i) % 4)


def osc_decode(data):
    """Bytes (one UDP datagram) -> [(address, [args])]; bundles are flattened in order. Raises
    OscError on a malformed packet."""
    data = bytes(data)
    if data.startswith(b"#bundle\0"):
        if len(data) < 16:
            raise OscError("short bundle")
        out, i = [], 16
        while i < len(data):
            if i + 4 > len(data):
                raise OscError("bundle element size cut short")
            n = struct.unpack(">i", data[i:i + 4])[0]
            if n < 0 or i + 4 + n > len(data):
                raise OscError("bundle element runs past the packet")
            out += osc_decode(data[i + 4:i + 4 + n])
            i += 4 + n
        return out
    addr, i = _rstr(data, 0)
    if not addr.startswith("/"):
        raise OscError("not an OSC message")
    if i >= len(data):
        return [(addr, [])]                    # no type tag string (old OSC): no arguments
    tags, i = _rstr(data, i)
    if not tags.startswith(","):
        raise OscError("bad type tag string %r" % tags)
    args = []
    for t in tags[1:]:
        if t in "if":
            if i + 4 > len(data):
                raise OscError("argument cut short")
            args.append(struct.unpack(">i" if t == "i" else ">f", data[i:i + 4])[0])
            i += 4
        elif t in "hdt":
            if i + 8 > len(data):
                raise OscError("argument cut short")
            args.append(struct.unpack({"h": ">q", "d": ">d", "t": ">Q"}[t], data[i:i + 8])[0])
            i += 8
        elif t in "sS":
            s, i = _rstr(data, i)
            args.append(s)
        elif t == "b":
            n = struct.unpack(">i", data[i:i + 4])[0]
            args.append(data[i + 4:i + 4 + n])
            i += 4 + n + (-n % 4)
        elif t in "cmr":
            args.append(struct.unpack(">I", data[i:i + 4])[0])
            i += 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t in "NI":
            args.append(None)
        else:
            raise OscError("unknown OSC type tag %r" % t)
    return [(addr, args)]


def encode(msg):
    """An adapter message (address, args) -> bytes."""
    return osc_message(msg[0], msg[1] if len(msg) > 1 else ())


# ------------------------------------------------------------------ the dialect
def _num(args):
    for a in args:
        if isinstance(a, bool):
            return 1.0 if a else 0.0
        if isinstance(a, (int, float)):
            return float(a)
    return None


def _text(args):
    return next((a for a in args if isinstance(a, str)), "")


class OscAdapter(Adapter):
    has_master = True
    surface_banking = True

    def __init__(self, n_strips=512):
        if int(n_strips) < 1:
            raise ValueError("need at least one strip")
        self.n_strips = int(n_strips)
        self.page = 0                   # REAPER bank - 1: strip i = track page * n_strips + i + 1
        self.unity01 = None             # the normalized position REAPER reports at 0.00 dB
        self.pan = {}                   # strip -> last pan REAPER told us (V-pot turns step from it)
        self.selected = {}              # strip -> REAPER's select state (a press toggles it)
        self.exists = {}                # strip -> True / False once REAPER said (number / name)
        self._numbered = set()          # strips whose existence came from TRACK_NUMBER
        self._fresh = {}                # strip -> {"n": pos, "db": dB} since the last pairing

    # ---- helpers ------------------------------------------------------------------
    def _addr(self, strip, what):
        if strip == MASTER:
            return "/master/" + what
        s = int(strip)
        if not 0 <= s < self.n_strips:
            raise IndexError("strip %r out of 0-%d" % (strip, self.n_strips - 1))
        return "/track/%d/%s" % (s + 1, what)

    def track_number(self, strip):
        """REAPER's track number (1-based, absolute) shown on a strip of the current page."""
        return self.page * self.n_strips + int(strip) + 1

    def track_count(self):
        """Tracks REAPER has told us about on this page: 1 + the highest strip holding one."""
        have = [s for s, e in self.exists.items() if e]
        return max(have) + 1 if have else 0

    def page_full(self):
        """The last strip of the page holds a track: there may be more on the next page."""
        return bool(self.exists.get(self.n_strips - 1))

    def set_page(self, page):
        """Show REAPER's tracks page * n_strips + 1 ...: forget this page, ask REAPER for that one."""
        page = max(0, int(page))
        self.page = page
        self.pan.clear()
        self.selected.clear()
        self.exists.clear()
        self._numbered.clear()
        self._fresh.clear()
        return [(0, ("/device/track/bank/select", (page + 1,))), (0, ("/action", (REFRESH_ACTION,)))]

    # ---- gestures -> wire -----------------------------------------------------------
    def out_fader(self, strip, value):
        return [(0, (self._addr(strip, "volume"), (float(value),)))]

    def out_fader_db(self, strip, db):
        if strip == MASTER:
            return None                 # REAPER's list has no master dB pattern
        return [(0, (self._addr(strip, "volume/db"), (float(db),)))]

    def out_touch(self, strip, on):
        if strip == MASTER:
            return []
        return [(0, (self._addr(strip, "volume/touch"), (1 if on else 0,)))]

    def out_pan(self, strip, value):
        if strip != MASTER:
            self.pan[int(strip)] = float(value)
        return [(0, (self._addr(strip, "pan"), (float(value),)))]

    def out_vpot(self, strip, delta):
        if strip == MASTER:
            return []
        cur = self.pan.get(int(strip), 0.5)
        v = min(1.0, max(0.0, cur + PAN_STEP * int(delta)))
        return self.out_pan(strip, v)

    def out_button(self, name, strip, pressed):
        if strip is None:
            if name in _TRANSPORT_T:
                return [(0, (_TRANSPORT_T[name], ()))] if pressed else []
            if name in _TRANSPORT_B:
                return [(0, (_TRANSPORT_B[name], (1 if pressed else 0,)))]
            return []                   # bank / channel: the bridge's views, not REAPER's
        if strip == MASTER or not pressed or name not in _BUTTON_ADDR:
            return []
        if name == "select":
            on = not self.selected.get(int(strip), False)
            return [(0, (self._addr(strip, "select"), (1 if on else 0,)))]
        return [(0, (self._addr(strip, _BUTTON_ADDR[name] + "/toggle"), ()))]

    def out_jog(self, delta):
        return [(0, ("/scrub", (float(delta),)))]

    def hello(self):
        """Tell REAPER our bank size + page, then ask for everything (the refresh action)."""
        return ([(0, ("/device/track/count", (self.n_strips,))),
                 (0, ("/device/track/bank/select", (self.page + 1,))),
                 (0, ("/action", (REFRESH_ACTION,)))])

    # ---- wire -> neutral updates ------------------------------------------------------
    def _learn(self, strip, key, value):
        f = self._fresh.setdefault(strip, {})
        f[key] = value
        if "n" in f and "db" in f:
            if abs(f["db"]) <= UNITY_DB_TOL and 0.0 < f["n"] < 1.0:
                self.unity01 = f["n"]
            f.clear()

    def decode(self, msg, port=0):
        try:
            addr, args = msg[0], list(msg[1]) if len(msg) > 1 else []
        except (TypeError, IndexError):
            return [], []
        if not isinstance(addr, str):
            return [], []
        up = []
        m = _TRACK.match(addr)
        if m:
            s = int(m.group(1)) - 1
            if not 0 <= s < self.n_strips:
                return [], []
            what = m.group(2)
            if what == "name":
                name = _text(args)
                up.append(("label", s, 0, name))
                if s not in self._numbered:
                    self.exists[s] = bool(name.strip())
            elif what == "number/str":
                self._numbered.add(s)
                self.exists[s] = bool(_text(args).strip())
                up.append(("label", s, 1, _text(args).strip()))
            else:
                v = _num(args)
                if v is None:
                    return [], []
                if what == "volume":
                    up.append(("fader", s, v))
                    self._learn(s, "n", v)
                elif what == "volume/db":
                    self._learn(s, "db", v)
                elif what == "pan":
                    self.pan[s] = v
                    up.append(("pan", s, v))
                elif what == "vu":
                    up.append(("meter", s, v, None))
                else:
                    name = _FROM_ADDR[what]
                    if name == "select":
                        self.selected[s] = v >= 0.5
                    up.append(("button", name, s, ON if v >= 0.5 else OFF))
            return up, []
        v = _num(args)
        if addr == "/master/volume" and v is not None:
            up.append(("fader", MASTER, v))
        elif addr == "/master/vu" and v is not None:
            up.append(("meter", MASTER, v, None))
        elif addr == "/master/pan" and v is not None:
            up.append(("pan", MASTER, v))
        elif addr in _TRANSPORT_IN and v is not None:
            up.append(("button", _TRANSPORT_IN[addr], None, ON if v >= 0.5 else OFF))
        return up, []
