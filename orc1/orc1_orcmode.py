"""
orc1_orcmode.py -- ORC MODE on our Fire firmware (2026-11-11; fw v0.34, hello version 9).

"ORC mode" on the Fire (BROWSER > ORC mode, or FIRE_HOST on the command socket) hands the
whole Fire to the ORC: pads, buttons, knobs, SELECT and the screen. The Fire's own
sequencer keeps playing underneath. SHIFT + BROWSER on the Fire leaves.

The ORC already has a complete Fire surface: orc1_fire.FireHandler (the views, the chord
rows, the keyboards, FX, faders, the screen pages), written for Akai's firmware. This
module wakes that handler for our firmware instead of writing a second one:

  Fire -> ORC   F0 7D 'ORC' 21 <0|1> F7        ORC mode went off / on
                F0 7D 'ORC' 22 <note> <vel> F7  a pad / button, as Akai's note numbers
                F0 7D 'ORC' 23 <k> <rel> F7     knob 0-3 or SELECT (4), relative
     These are SysEx, not notes, because the sequencer's own notes arrive on the same
     port. They are turned back into the note / CC messages the handler was written for.
  ORC -> Fire   the handler's own pad-colour (47 7F 43 65) and screen (47 7F 43 0E)
                SysEx, unchanged; the firmware decodes them in ORC mode.
                F0 7D 'ORC' 21 02 F7 "still here", once a second: the Fire drops back to
                its sequencer by itself after 5 s without one.

ONE CONNECTION: everything goes out through the brain's existing link to the Fire
(orc1_scenes.FireBarLink.host_send: its port, its lock). Nothing here opens a port.

Pure apart from what is injected, so it runs against fakes in _test_fire_host.py.
"""

import threading
import time

ORC_HDR = [0x7D, 0x4F, 0x52, 0x43]
CMD_MODE, MSG_INPUT, MSG_TURN, CMD_CURVE = 0x21, 0x22, 0x23, 0x24
MODE_OFF, MODE_ON, MODE_ALIVE = 0, 1, 2
HOST_VERSION = 9            # v0.34: ORC mode (since v0.33 = 8) WITH the fine brightness the colours here count on
KEEPALIVE_S = 1.0           # the Fire gives up after 5 s (hostm.h HM_QUIET_MS)
TOUCH_GAP_S = 0.4           # a knob still for this long is "let go"; its next turn is a new touch
SELECT_K = 4
DIAL_CC_BASE, SELECT_CC = 16, 118   # orc1_fire DIAL_CC / SELECT_CC (checked in the test)

# ---- pad colours -------------------------------------------------------------------
# Fire fw v0.34 (hello version 9) gives every pad LED about 200 brightness steps (a long
# and a short slot per chain pass; the smallest step is about 0.4 % on-time) and applies a
# curve itself: with its curve at 20, brightness = (value / 127) squared. So the handler's
# palette (dim 20, lit 40-50 of 127) is sent AS IT IS: dim 20 -> 2.5 % on-time, lit 50 ->
# 15.5 %, 127 -> full, and a value too small to reach half the smallest step (1-6) is dark.
# (2026-11-12: the handler's resting colours were 1-14, tuned on Akai's firmware: dark or one
# step here. They were raised IN orc1_fire.py to 9-28; the step view's empty steps, 10-16,
# were already right by owner's eyes, which is why nothing is lifted here.)
# What is left here is ONE knob (FIRE_HOST_CURVE, kept until the brain restarts): the curve
# x 10 the pads should follow. 20 (the default) sends the values untouched. Lower lifts the
# dim colours (10 = brightness straight in line with the value), higher pushes them down
# (30 = cubed). It is done by bending the value before the Fire squares it, so every
# brightness the hardware has stays reachable.
# History, 2026-11-11, on the 22-step firmware (v0.33): channel by channel every dim colour
# landed on the same lowest step; a hue-keeping lift (curve 6) made owner say "pads that
# are supposed to be dim are almost as bright as the ones that are lit and it seems to color
# all the pads"; a straight map was "still too bright". 22 steps could not do it.
PAD_CMD = [0x47, 0x7F, 0x43, 0x65]
FW_CURVE = 20               # what the Fire is told to use (24 20): brightness = (value / 127) ** 2
CURVE_DEFAULT = 20
CURVE_MIN, CURVE_MAX = 10, 30


def pad_value(c, curve=CURVE_DEFAULT):
    """One 0-127 colour value from the handler -> the value to send, so that the Fire's
    squared curve gives (c / 127) ** (curve / 10). curve 20 = unchanged."""
    c = max(0, min(127, int(c)))
    if not c or curve == FW_CURVE:
        return c
    return max(0, min(127, int(127.0 * (c / 127.0) ** (curve / float(FW_CURVE)) + 0.5)))


def remap_pads(data, curve=CURVE_DEFAULT):
    """A pad-colour SysEx's data (47 7F 43 65 <len hi> <len lo> {pad r g b}...) with every
    colour bent by the curve; anything else comes back unchanged."""
    data = list(data)
    if data[:4] != PAD_CMD or len(data) < 6:
        return data
    out = data[:6]
    cells = data[6:]
    for i in range(0, len(cells) - len(cells) % 4, 4):
        out += [cells[i]] + [pad_value(x, curve) for x in cells[i + 1:i + 4]]
    return out + cells[len(cells) - len(cells) % 4:]


class HostOut:
    """What the handler (and fire_oled) send to instead of a MIDI port: SysEx goes out
    through the shared link, anything else is dropped (the handler only sends SysEx to
    the Fire). Never raises: a Fire that just went away must not break a render."""

    def __init__(self, send):
        self._send = send
        self.sent = 0
        self.failed = 0
        self.curve = CURVE_DEFAULT      # pad colours are remapped on the way out (remap_pads)

    def send(self, msg):
        if getattr(msg, "type", None) != "sysex":
            return
        try:
            self._send(remap_pads(msg.data, self.curve))
            self.sent += 1
        except Exception:
            self.failed += 1

    def close(self):
        pass


class NullOut:
    """Where the screen goes while ORC mode is off and no stock Fire is plugged in."""

    def send(self, msg):
        pass

    def close(self):
        pass


class FireHost:
    def __init__(self, mido_mod, send, link_version, is_ours, get_fire, make_fire,
                 fire_mod=None, oled_mod=None, log=print, clock=time.monotonic,
                 run_async=None):
        """send(data) = FireBarLink.host_send; link_version() -> (connected, version);
        is_ours(port name); get_fire() -> the FireHandler or None; make_fire() builds one
        (the brain's global). fire_mod / oled_mod = orc1_fire / fire_oled (their HOST_OUT
        hooks). run_async(fn) runs the wake-up off the MIDI input thread."""
        self.mido, self._send, self.link_version, self.is_ours = mido_mod, send, link_version, is_ours
        self.get_fire, self.make_fire = get_fire, make_fire
        self.fire_mod, self.oled_mod, self.log, self.clock = fire_mod, oled_mod, log, clock
        self.run_async = run_async or (lambda fn: threading.Thread(
            target=fn, daemon=True, name="fire-host-wake").start())
        self.out = HostOut(send)
        self.on = False
        self._alive_t = 0.0
        self._turn_t = {}
        self._down = set()          # pads / buttons the Fire said are held (Akai note numbers)
        self._lock = threading.RLock()
        self.inputs = 0             # 22 / 23 frames handled (status, tests)

    # ---- Fire -> ORC ----
    def handle_sysex(self, data, source_name=""):
        """True = an ORC-mode frame (21 / 22 / 23) from our Fire, consumed."""
        data = list(data or ())
        if data[:4] != ORC_HDR or len(data) < 6 or data[4] not in (CMD_MODE, MSG_INPUT, MSG_TURN):
            return False
        if not self.is_ours(source_name):
            return False
        cmd = data[4]
        if cmd == CMD_MODE:
            self._state(bool(data[5]))
            return True
        if not self.on or len(data) < 7:
            return True             # a press from before the handler was up: nothing to give it to
        fire = self.get_fire()
        if fire is None:
            return True
        a, b = data[5] & 0x7F, data[6] & 0x7F
        try:
            if cmd == MSG_INPUT:
                (self._down.add if b else self._down.discard)(a)
                fire.handle_msg(self.mido.Message("note_on" if b else "note_off",
                                                  channel=0, note=a, velocity=b))
            elif a == SELECT_K:
                fire.handle_msg(self.mido.Message("control_change", channel=0,
                                                  control=SELECT_CC, value=b))
            elif a < 4:
                now = self.clock()
                if now - self._turn_t.get(a, -1e9) > TOUCH_GAP_S:   # the stock Fire's knob-touch note
                    fire.handle_msg(self.mido.Message("note_on", channel=0,
                                                      note=DIAL_CC_BASE + a, velocity=127))
                self._turn_t[a] = now
                fire.handle_msg(self.mido.Message("control_change", channel=0,
                                                  control=DIAL_CC_BASE + a, value=b))
            self.inputs += 1
        except Exception as e:
            self.log(f"  ! [fire host] input {data[4:7]}: {e}")
        return True

    def _state(self, on):
        with self._lock:
            if on == self.on:
                return
            self.on = on
            self._alive_t = 0.0
            self._turn_t.clear()
        if on:
            self.log("[fire host] ORC mode ON: the ORC has the Fire's pads, buttons, knobs and screen")
            self.run_async(self._wake)
        else:
            self.log("[fire host] ORC mode off: the Fire is its own sequencer again")
            self._sleep()

    def _wake(self):
        """Point the handler + the screen at the shared link, then repaint everything."""
        try:
            self._send(ORC_HDR + [CMD_CURVE, FW_CURVE])   # the Fire squares; remap_pads bends on top of that
            if self.fire_mod is not None:
                self.fire_mod.HOST_OUT = self.out
            if self.oled_mod is not None:
                self.oled_mod.HOST_OUT = self.out
            fire = self.get_fire()
            if fire is None:
                fire = self.make_fire()      # paints pads + screen as it starts
            if fire is not None:
                fire.rewake()                # (also right after make: a second paint costs nothing)
        except Exception as e:
            self.log(f"  ! [fire host] wake: {e}")

    def _sleep(self):
        """ORC mode ended (SHIFT + BROWSER, the Fire unplugged ...). Whatever was still held
        never gets its release from the Fire: let go of it here, through the handler's own
        release code, so no chord or key is left sounding."""
        fire = self.get_fire()
        held, self._down = sorted(self._down), set()
        for note in held:
            try:
                if fire is not None:
                    fire.handle_msg(self.mido.Message("note_off", channel=0, note=note, velocity=0))
            except Exception as e:
                self.log(f"  ! [fire host] release {note}: {e}")
        try:
            if self.fire_mod is not None:
                self.fire_mod.HOST_OUT = None
            if self.oled_mod is not None:
                self.oled_mod.HOST_OUT = NullOut()   # the handler's screen updates go nowhere
            if fire is not None:
                fire._out = None             # no pad colours to a Fire that is sequencing
        except Exception as e:
            self.log(f"  ! [fire host] sleep: {e}")

    # ---- ORC -> Fire ----
    def poll(self):
        """Call a few times a second. Keeps ORC mode alive; notices a Fire that went away."""
        connected, version = self.link_version()
        if not connected:
            if self.on:
                self.log("[fire host] the Fire is gone")
                self._state(False)
            return
        if not self.on:
            return
        now = self.clock()
        if now - self._alive_t >= KEEPALIVE_S:
            self._alive_t = now
            try:
                self._send(ORC_HDR + [CMD_MODE, MODE_ALIVE])
            except Exception:
                pass

    def request(self, on):
        """Ask the Fire to enter / leave ORC mode. -> (ok, why). The Fire answers 21 <state>,
        and only that answer changes self.on."""
        connected, version = self.link_version()
        if not connected:
            return False, "no Fire with our firmware connected (FLAME seq / ORC FW seq)"
        if version is None:
            return False, "the Fire has not answered hello yet"
        if version < HOST_VERSION:
            return False, (f"Fire firmware version {version} has no ORC mode "
                           f"(needs v0.34, hello version {HOST_VERSION})")
        try:
            self._send(ORC_HDR + [CMD_MODE, MODE_ON if on else MODE_OFF])
            return True, ""
        except Exception as e:
            return False, f"send failed: {e}"

    def set_curve(self, x10):
        """The pad colour curve x 10: 10 (straight: dim colours lifted) .. 20 (the default:
        the handler's values as they are) .. 30 (dim colours pushed down). Applied here
        (remap_pads) on top of the Fire's own squared curve; kept until the brain restarts."""
        try:
            x10 = int(x10)
        except (TypeError, ValueError):
            return False, "curve must be a number 10-30"
        if not CURVE_MIN <= x10 <= CURVE_MAX:
            return False, f"curve must be {CURVE_MIN}-{CURVE_MAX}"
        self.out.curve = x10
        fire = self.get_fire()
        if self.on and fire is not None:
            try:
                fire.rewake()                # repaint through the new curve
            except Exception:
                pass
        return True, ""

    def status(self):
        connected, version = self.link_version()
        return {"ok": True, "on": self.on, "connected": connected, "version": version,
                "capable": bool(connected and version is not None and version >= HOST_VERSION),
                "inputs": self.inputs, "sent": self.out.sent, "failed": self.out.failed,
                "curve": self.out.curve}


def socket_cmd(host, cmd):
    """FIRE_HOST {on: bool} / FIRE_HOST_STATUS / FIRE_HOST_CURVE {curve: 10-30} -> reply dict."""
    action = cmd.get("action")
    if action == "FIRE_HOST_STATUS":
        return host.status()
    if action == "FIRE_HOST":
        ok, why = host.request(bool(cmd.get("on", True)))
    elif action == "FIRE_HOST_CURVE":
        ok, why = host.set_curve(cmd.get("curve", CURVE_DEFAULT))
    else:
        return {"ok": False, "error": f"unknown action {action}"}
    r = host.status()
    r["ok"] = ok
    if not ok:
        r["error"] = why
    return r
