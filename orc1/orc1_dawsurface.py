"""orc1_dawsurface -- the ORC AS a DAW control surface: the dialect-neutral model.

The DAW (REAPER, Pro Tools, Logic ...) is the HOST; the ORC is the surface, with
every controller in the rig behind it. This file is the shared core. It knows
nothing about MIDI, Mackie, HUI or OSC: a dialect ADAPTER (orc1_dawsurface_mcu
today; HUI and REAPER OSC later) turns gestures into wire messages and wire
messages into the neutral updates below.

    surf = DawSurface(McuAdapter(devices=2), send=lambda port, msg: ...)
    surf.subscribe(lambda ev: ...)          # ("strip", 3, "name", "Bass"), ...
    surf.set_fader(3, 0.7)                  # ORC-side gestures -> wire messages
    surf.press("mute", 5); surf.turn(2, +3); surf.bank(+1); surf.press("play")
    surf.receive(msg, port=1)               # DAW -> model, returns change events

The model holds what the DAW last told us (so a Fire / BSP / HUD display layer
can read it) and only emits an event when a value really changes.

Strips are numbered 0 .. adapter.n_strips-1 across all of the adapter's virtual
devices (MCU: main + extenders, 8 each; OSC may have any number); MASTER is the
master strip when the dialect has one. Banking is a GESTURE (bank / channel
left-right): MCU hosts re-send the strips themselves; a dialect that banks on
the surface side can do it inside its adapter.

Values are neutral: fader 0.0-1.0, vpot ring 0.0-1.0 or None (ring off) plus a
mode name, buttons OFF / ON / BLINK (BLINK is truthy: REAPER's MCU mode shows a
soloed track as a BLINKING solo LED), meter 0.0-1.0 + an overload flag.

THE ADAPTER CONTRACT (what a new dialect implements):
    n_strips, has_master
    surface_banking               False = the HOST banks (MCU: bank / channel are buttons the DAW
                                  obeys and re-sends the strips); True = the strips are ALL the
                                  DAW's tracks by number and banking is the surface's business
                                  (OSC: bank / channel gestures send nothing)
    out_fader(strip, value01)     out_touch(strip, on)    out_vpot(strip, delta)
    out_button(name, strip, pressed)                      out_jog(delta)
    out_pan(strip, value01)       an absolute pan (0.5 = centre); [] = the dialect has none
    out_fader_db(strip, db)       an exact level in dB; None = the dialect can't (the core
                                  then sends out_fader instead)
    hello()                       -> messages announcing the surface (may be [])
    decode(msg, port)             -> (updates, replies)
  every out_* / hello returns a list of (port, message); `message` is whatever
  the dialect's transport sends (MIDI dicts for MCU, OSC tuples later).
  Updates (adapter -> model):
    ("fader", strip, value01)
    ("vpot", strip, value01-or-None, mode, centre)
    ("button", name, strip-or-None, OFF/ON/BLINK)
    ("label", strip, row, text)        row 0 = the name line, 1 = the 2nd line
    ("meter", strip, level01-or-None, overload True/False/None)
    ("pan", strip, value01)            dialects that send pan as such (0.5 = centre)
    ("display", key, text)             e.g. "timecode", "assignment"
    ("colour", strip, (r, g, b))       dialects that know track colours

Stdlib only, no ports opened, no threads: the brain integration (a later round)
owns the MIDI ports and calls receive() / the gestures.
"""

MASTER = "master"
OFF, ON, BLINK = False, True, "blink"
STRIP_BUTTONS = ("rec", "solo", "mute", "select")
TRANSPORT = ("play", "stop", "record", "rewind", "fast_fwd", "loop")
VPOT_MODES = ("dot", "boost_cut", "wrap", "spread")

_STRIP_FIELDS = ("fader", "touched", "vpot", "vpot_mode", "vpot_centre", "pan",
                 "rec", "solo", "mute", "select", "name", "name2", "meter",
                 "overload", "colour")


class Strip(object):
    """One channel strip as the DAW last described it."""
    __slots__ = ("index",) + _STRIP_FIELDS

    def __init__(self, index):
        self.index = index
        self.fader = None          # None = the DAW hasn't said yet
        self.touched = False
        self.vpot = None
        self.vpot_mode = "dot"
        self.vpot_centre = False
        self.pan = None            # only dialects that send pan as such fill this
        self.rec = self.solo = self.mute = self.select = OFF
        self.name = ""
        self.name2 = ""
        self.meter = 0.0
        self.overload = False
        self.colour = None         # (r, g, b) 0-255 when the dialect knows it

    def as_dict(self):
        d = {f: getattr(self, f) for f in _STRIP_FIELDS}
        d["index"] = self.index
        return d


class Adapter(object):
    """Base class for a dialect; see the contract in the module docstring."""
    n_strips = 0
    has_master = False
    surface_banking = False

    def out_fader(self, strip, value):
        return []

    def out_touch(self, strip, on):
        return []

    def out_vpot(self, strip, delta):
        return []

    def out_button(self, name, strip, pressed):
        return []

    def out_jog(self, delta):
        return []

    def out_pan(self, strip, value):
        return []

    def out_fader_db(self, strip, db):
        return None

    def hello(self):
        return []

    def decode(self, msg, port=0):
        return [], []


def _clamp01(v):
    v = float(v)
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


class DawSurface(object):
    def __init__(self, adapter, send=None):
        self.adapter = adapter
        self.send = send                     # send(port, message), optional
        self.strips = [Strip(i) for i in range(int(adapter.n_strips))]
        self.master = Strip(MASTER) if adapter.has_master else None
        self.transport = {k: OFF for k in TRANSPORT}
        self.leds = {}                       # any other button LED, by name
        self.display = {}                    # "timecode", "assignment", ...
        self._subs = []

    # ---- subscription ---------------------------------------------------------
    def subscribe(self, fn):
        """fn(event) for every change: ("strip", idx, field, value),
        ("transport", name, value), ("led", name, value), ("display", key, text)."""
        self._subs.append(fn)
        return fn

    def unsubscribe(self, fn):
        if fn in self._subs:
            self._subs.remove(fn)

    def _emit(self, ev, events):
        events.append(ev)
        for fn in list(self._subs):
            fn(ev)

    def strip(self, idx):
        if idx == MASTER:
            if self.master is None:
                raise IndexError("this dialect has no master strip")
            return self.master
        i = int(idx)
        if not 0 <= i < len(self.strips):
            raise IndexError("strip %r out of 0-%d" % (idx, len(self.strips) - 1))
        return self.strips[i]

    def _set_strip(self, idx, field, value, events):
        s = self.strip(idx)
        if getattr(s, field) != value:
            setattr(s, field, value)
            self._emit(("strip", s.index, field, value), events)

    def _out(self, msgs):
        msgs = list(msgs)
        if self.send is not None:
            for port, m in msgs:
                self.send(port, m)
        return msgs

    # ---- ORC-side gestures -> outgoing messages ---------------------------------
    def set_fader(self, idx, value):
        """Move strip `idx`'s fader (or MASTER) to 0.0-1.0 (clamped)."""
        v = _clamp01(value)
        self._set_strip(idx, "fader", v, [])
        return self._out(self.adapter.out_fader(idx, v))

    def set_fader_db(self, idx, db, value):
        """Move a fader to an exact level in dB where the dialect can say so (`value` = the same
        position 0.0-1.0, for the model and for a dialect that can't)."""
        v = _clamp01(value)
        self._set_strip(idx, "fader", v, [])
        msgs = self.adapter.out_fader_db(idx, float(db))
        return self._out(self.adapter.out_fader(idx, v) if msgs is None else msgs)

    def set_pan(self, idx, value):
        """An absolute pan 0.0-1.0 (0.5 = centre), for a dialect that has one."""
        v = _clamp01(value)
        msgs = self.adapter.out_pan(idx, v)
        if msgs:
            self._set_strip(idx, "pan", v, [])
        return self._out(msgs)

    def clear(self):
        """Forget every strip (a dialect that pages its strips: the old page's values are stale).
        Silent: the caller repaints."""
        self.strips = [Strip(i) for i in range(len(self.strips))]

    def touch(self, idx, on=True):
        """Fader touch (hosts use it for touch automation and to stop chasing)."""
        self.strip(idx)
        self._set_strip(idx, "touched", bool(on), [])
        return self._out(self.adapter.out_touch(idx, bool(on)))

    def turn(self, idx, delta):
        """Turn strip `idx`'s V-pot by `delta` detents (relative, +right)."""
        self.strip(idx)
        if not int(delta):
            return []
        return self._out(self.adapter.out_vpot(idx, int(delta)))

    def button(self, name, idx=None, pressed=True):
        """Hold or release a button. Strip buttons (rec / solo / mute / select /
        vpot_push) need `idx`; transport and global ones don't."""
        if idx is not None:
            self.strip(idx)
        return self._out(self.adapter.out_button(name, idx, bool(pressed)))

    def press(self, name, idx=None):
        """A tap: press then release. The model does NOT toggle anything itself:
        the DAW answers with the new LED state, which arrives via receive()."""
        return self.button(name, idx, True) + self.button(name, idx, False)

    def jog(self, delta):
        if not int(delta):
            return []
        return self._out(self.adapter.out_jog(int(delta)))

    def bank(self, direction):
        """Shift the DAW's strips by one bank (+1 right, -1 left)."""
        return self.press("bank_right" if direction > 0 else "bank_left")

    def channel(self, direction):
        """Shift the DAW's strips by one strip."""
        return self.press("channel_right" if direction > 0 else "channel_left")

    def hello(self):
        """Announce the surface (MCU: also asks REAPER to re-send everything)."""
        return self._out(self.adapter.hello())

    # ---- DAW -> model ------------------------------------------------------------
    def receive(self, msg, port=0):
        """Apply one incoming message. Returns the change events; any reply the
        dialect owes the host (a handshake) goes out through `send`."""
        updates, replies = self.adapter.decode(msg, port)
        events = []
        for u in updates:
            self._apply(u, events)
        self._out(replies)
        return events

    def _apply(self, u, events):
        kind = u[0]
        if kind == "fader":
            _k, idx, v = u
            if self.strip(idx).touched:
                return                  # a held fader doesn't chase the host
            self._set_strip(idx, "fader", _clamp01(v), events)
        elif kind == "vpot":
            _k, idx, v, mode, centre = u
            self._set_strip(idx, "vpot", None if v is None else _clamp01(v), events)
            self._set_strip(idx, "vpot_mode", mode, events)
            self._set_strip(idx, "vpot_centre", bool(centre), events)
        elif kind == "button":
            _k, name, idx, state = u
            if idx is not None:
                if name in STRIP_BUTTONS:
                    self._set_strip(idx, name, state, events)
                else:
                    self._set_led("%s:%s" % (name, idx), state, events)
            elif name in TRANSPORT:
                if self.transport.get(name) != state:
                    self.transport[name] = state
                    self._emit(("transport", name, state), events)
            else:
                self._set_led(name, state, events)
        elif kind == "label":
            _k, idx, row, text = u
            self._set_strip(idx, "name" if row == 0 else "name2", text, events)
        elif kind == "meter":
            _k, idx, level, over = u
            if level is not None:
                self._set_strip(idx, "meter", _clamp01(level), events)
            if over is not None:
                self._set_strip(idx, "overload", bool(over), events)
        elif kind == "display":
            _k, key, text = u
            if self.display.get(key) != text:
                self.display[key] = text
                self._emit(("display", key, text), events)
        elif kind == "colour":
            _k, idx, rgb = u
            self._set_strip(idx, "colour", tuple(rgb), events)
        elif kind == "pan":
            _k, idx, v = u
            self._set_strip(idx, "pan", _clamp01(v), events)

    def _set_led(self, name, state, events):
        if self.leds.get(name, OFF) != state:
            self.leds[name] = state
            self._emit(("led", name, state), events)

    # ---- reading -------------------------------------------------------------------
    def snapshot(self):
        return {"strips": [s.as_dict() for s in self.strips],
                "master": self.master.as_dict() if self.master else None,
                "transport": dict(self.transport), "leds": dict(self.leds),
                "display": dict(self.display)}
