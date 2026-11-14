"""orc1_dawsurface_mcu -- the Mackie Control (MCU) dialect for orc1_dawsurface.

The ORC plays a Mackie Control main unit plus N-1 extenders (XT). Each virtual
device is its own MIDI port pair (REAPER: one "Mackie Control" surface on port
0 + one "Mackie Control Extender" per extra port, each with its own offset), 8
strips each; port 0 also carries the master fader, transport, jog, bank keys
and the 7-segment displays. Strip N of the model = port N // 8, strip N % 8.

All wire shapes come from orc1_mcu (see its docstring for what was checked
against REAPER's csurf_mcu.cpp, the Logic Control tables and Ardour, and
where). This file only maps them onto the neutral model.

Handshake: REAPER needs none. A host that sends the device query (Logic,
Ardour) gets the host connection query back; its 0x02 reply gets a
confirmation (lenient by default, `strict=True` = a real MCU's error on a wrong
response). hello() sends the power-on host connection query on every port;
with the default serial "XYZORC1" REAPER treats it as its reset message and
re-sends every strip.
"""
import orc1_mcu as M
from orc1_dawsurface import Adapter, MASTER, OFF, ON, BLINK, VPOT_MODES

LCD_CELLS = 2 * M.LCD_WIDTH            # 2 rows x 56
CELL = 7                               # chars per strip on the scribble strip
_LED_STATE = {M.LED_OFF: OFF, M.LED_ON: ON, M.LED_BLINK: BLINK}
# neutral button names that MCU spells differently
_TO_MCU = {"loop": "cycle"}
_FROM_MCU = {v: k for k, v in _TO_MCU.items()}


class McuAdapter(Adapter):
    has_master = True

    def __init__(self, devices=1, main_dev=M.DEV_MCU, ext_dev=M.DEV_XT,
                 serial=M.DEFAULT_SERIAL, strict=False):
        if int(devices) < 1:
            raise ValueError("need at least the main unit")
        self.devices = int(devices)
        self.n_strips = M.N_STRIPS * self.devices
        self.main_dev, self.ext_dev = main_dev, ext_dev
        self.serial, self.strict = serial, strict
        self.lcd = [[" "] * LCD_CELLS for _ in range(self.devices)]
        self.timecode = [" "] * M.TC_DIGITS        # index 0 = rightmost digit
        self.tc_dots = [False] * M.TC_DIGITS
        self.assignment = [" ", " "]

    # ---- helpers ------------------------------------------------------------
    def dev_id(self, port):
        return self.main_dev if port == 0 else self.ext_dev

    def _loc(self, strip):
        if strip == MASTER:
            return 0, M.MASTER_FADER
        s = int(strip)
        if not 0 <= s < self.n_strips:
            raise IndexError("strip %r out of 0-%d" % (strip, self.n_strips - 1))
        return s // M.N_STRIPS, s % M.N_STRIPS

    def lcd_row(self, port, row):
        """The 56 characters of one scribble-strip row as the host wrote them."""
        b = self.lcd[port]
        return "".join(b[row * M.LCD_WIDTH:(row + 1) * M.LCD_WIDTH])

    def _tc_text(self):
        out = []
        for i in range(M.TC_DIGITS - 1, -1, -1):          # leftmost first
            out.append(self.timecode[i] + ("." if self.tc_dots[i] else ""))
        return "".join(out)

    # ---- gestures -> wire -------------------------------------------------------
    def out_fader(self, strip, value):
        port, ch = self._loc(strip)
        return [(port, M.fader(ch, int(round(value * 16383))))]

    def out_touch(self, strip, on):
        port, ch = self._loc(strip)
        return [(port, M.fader_touch(ch, on))]

    def out_vpot(self, strip, delta):
        if strip == MASTER:
            return []
        port, ch = self._loc(strip)
        return [(port, m) for m in M.vpot_turn(ch, delta)]

    def out_button(self, name, strip, pressed):
        name = _TO_MCU.get(name, name)
        if strip is None:
            return [(0, M.button(name, pressed))]
        port, ch = self._loc(strip)
        if ch == M.MASTER_FADER:
            return []
        return [(port, M.button(name, pressed, strip=ch))]

    def out_jog(self, delta):
        return [(0, m) for m in M.jog_turn(delta)]

    def hello(self):
        return [(p, M.surface_hello(self.dev_id(p), self.serial))
                for p in range(self.devices)]

    # ---- wire -> neutral updates ----------------------------------------------------
    def decode(self, msg, port=0):
        if not 0 <= port < self.devices:
            return [], []
        ev = M.decode_host(msg)
        if ev is None:
            return [], []
        base = port * M.N_STRIPS
        k = ev[0]
        up = []
        if k == "fader":
            ch, v = ev[1], ev[2]
            if ch == M.MASTER_FADER:
                if port == 0:
                    up.append(("fader", MASTER, v / 16383.0))
            else:
                up.append(("fader", base + ch, v / 16383.0))
        elif k == "ring":
            _k, ch, pos, mode, centre = ev
            val = None if pos == 0 else (min(pos, 11) - 1) / 10.0
            up.append(("vpot", base + ch, val, VPOT_MODES[mode], centre))
        elif k == "led":
            _k, name, ch, st = ev
            state = _LED_STATE[st]
            if isinstance(name, int):
                name = "note_%d" % name
            name = _FROM_MCU.get(name, name)
            up.append(("button", name, None if ch is None else base + ch, state))
        elif k in ("meter", "master_meter"):
            _k, ch, lv = ev
            if k == "master_meter":
                if ch != 0 or port != 0:           # left side only stands for the master
                    return [], []
                idx = MASTER
            else:
                idx = base + ch
            if lv == "overload":
                up.append(("meter", idx, None, True))
            elif lv == "clear":
                up.append(("meter", idx, None, False))
            else:
                up.append(("meter", idx, min(lv, M.METER_MAX) / float(M.METER_MAX), None))
        elif k == "segment":
            _k, which, i, ch, dot = ev
            if which == "timecode":
                self.timecode[i], self.tc_dots[i] = ch, dot
                up.append(("display", "timecode", self._tc_text()))
            else:
                self.assignment[i] = ch
                up.append(("display", "assignment", self.assignment[1] + self.assignment[0]))
        elif k == "lcd":
            _k, _dev, off, text = ev
            buf = self.lcd[port]
            for j, c in enumerate(text):
                p = off + j
                if p >= LCD_CELLS:
                    break
                buf[p] = c if 32 <= ord(c) < 127 else " "
            for row in (0, 1):
                line = self.lcd_row(port, row)
                for s in range(M.N_STRIPS):
                    up.append(("label", base + s, row, line[s * CELL:(s + 1) * CELL].strip()))
        reply = M.surface_answer(ev, dev=self.dev_id(port), serial=self.serial,
                                 strict=self.strict)
        return up, ([(port, reply)] if reply is not None else [])
