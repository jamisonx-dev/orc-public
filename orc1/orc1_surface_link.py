"""
orc1_surface_link.py -- keep the ORC's control SURFACES painted (2026-10-14).

A pad-map profile with "surface": true (orc1_padmap, /pads) is a device the ORC draws
its own controls on: owner's Akai Fire running our firmware. This link finds the
device's MIDI OUTPUT by name, puts it in surface mode and paints every pad from the
profile (orc1_padmap.surface_frames), then repaints whenever the profile changes.

Recognised by NAME, two ways: the profile's port_match, AND "ORC FW" in the port name
(only our firmware speaks this protocol; a profile matching any other device is skipped
and said once in the journal).

When it (re)sends:
  - the port appears, or comes back after a reboot. A Fire reboot recreates its port,
    often under the SAME name, and a handle opened before then drops every message
    silently (measured, orc1-firevu 2026-10-13). So the device's USB address
    (/proc/asound/cardN/usbbus, which changes on every plug-in / reboot) is part of
    the link's identity: a new address = a new link;
  - the profile's pads / colours / channel change (/pads saves -> PADMAP_SET);
  - NOT on a timer: the player leaves surface mode with a mode button on the Fire, and
    the ORC must not drag them back every few seconds.
A profile switched off as a surface gets "surface off" once, so its pads go back to
the device's own sequencer.

SHOWN ONLY WHEN ASKED (owner, 2026-10-14: the Fire kept opening on the surface
instead of his steps). When the Fire appears or reboots the link ARMS the surface
(0B on, so the firmware learns the channel and that a surface exists; the colours;
then 0B off): the Fire stays on the sequencer, and BROWSER > ORC surface on the Fire
or SHOW ON FIRE on /pads (repaint) brings it up. A map change only resends the
colours (a channel change re-arms). The firmware's 0B on also switches off running
lights, so a Fire already armed (same USB address, same channel) is not re-armed
after a BRAIN restart: `armed_path` remembers it (tmpfs, gone on a Pi reboot, and a
Fire reboot changes the address anyway).

Pure apart from the injected mido module, so it runs against a fake in
_test_surface.py. The brain calls poll() about once a second from a daemon thread.
"""

import json
import os
import threading

import orc1_padmap

PROTOCOL_NAME = "orc fw"           # our firmware's USB / port names all start "ORC FW"


class SurfaceLink:
    def __init__(self, mido_mod, surfaces, idents, log=print, armed_path=None):
        """surfaces() -> [(name, device dict)] (PadMap.surfaces); idents() -> {ALSA card name: 'bus/dev'}.
        armed_path: a JSON file {"<port>|<usb address>": channel} of Fires already armed (None = remember nothing)."""
        self.mido, self.surfaces, self.idents, self.log = mido_mod, surfaces, idents, log
        self.armed_path = armed_path
        self.links = {}             # profile name -> {"port", "ident", "out", "sig", "ch", "show"}
        self._said = set()
        # poll() (the link thread) and paint_pad() (a hold light, the tap blink) share one port
        self._send_lock = threading.RLock()
        self.painted = 0            # +1 per full colour send: a blinker re-sends what it showed

    def paint_pad(self, name, pad, rgb):
        """Set ONE pad's colour on profile `name`'s Fire right now (0A, count 1): a grab pad lit while held, the tap
        pad's blink. rgb = (r, g, b) 0-7. False if that Fire is not linked. Never raises."""
        with self._send_lock:
            link = self.links.get(name)
            if not link or link.get("out") is None:
                return False
            try:
                r, g, b = (max(0, min(7, int(v))) for v in rgb)
                link["out"].send(self.mido.Message(
                    "sysex", data=orc1_padmap.ORC_HDR + [0x0A, int(pad) & 0x3F, 1, r, g, b]))
                return True
            except Exception as e:
                self._say_once(("paint", name, type(e).__name__), f"{name}: cannot paint pad {pad} ({e})")
                return False

    def _armed(self):
        try:
            with open(self.armed_path) as f:
                a = json.load(f)
            return a if isinstance(a, dict) else {}
        except Exception:
            return {}

    def _set_armed(self, key, ch):
        if not self.armed_path:
            return
        a = self._armed()
        a[key] = ch
        try:
            tmp = self.armed_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(a, f)
            os.replace(tmp, self.armed_path)
        except Exception as e:
            self._say_once(("armed", type(e).__name__), f"cannot write {self.armed_path} ({e})")

    def _say_once(self, key, msg):
        if key not in self._said:
            self._said.add(key)
            self.log(f"[surface] {msg}")

    def _close(self, link):
        out = link.get("out")
        link["out"] = None
        if out is not None:
            try:
                out.close()
            except Exception:
                pass

    def _send_all(self, link, frames):
        for f in frames:
            link["out"].send(self.mido.Message("sysex", data=f))

    @staticmethod
    def _find(outs, dev):
        pm = dev["port_match"].lower()
        mine = [n for n in outs if pm in n.lower()]
        ours = [n for n in mine if PROTOCOL_NAME in n.lower()]
        return (ours[0] if ours else None), bool(mine and not ours)

    def repaint(self, name=None):
        """SHOW the surface at the next poll (surface on + all colours): /pads SHOW ON FIRE. The only path that puts
        the Fire on the surface; name = one profile, None = all."""
        for n, link in self.links.items():
            if name is None or n == name:
                link["sig"] = None
                link["show"] = True

    def poll(self):
        with self._send_lock:
            self._poll()

    def _poll(self):
        try:
            outs = list(self.mido.get_output_names())
        except Exception as e:
            self._say_once(("list", type(e).__name__), f"cannot list MIDI outputs ({e}); will keep trying")
            return
        try:
            idents = self.idents() or {}
        except Exception:
            idents = {}
        want = dict(self.surfaces())

        for name in [n for n in self.links if n not in want]:      # no longer a surface: hand the pads back
            link = self.links.pop(name)
            if link.get("out") is not None:
                try:
                    link["out"].send(self.mido.Message("sysex", data=orc1_padmap.surface_off()))
                    self.log(f"[surface] {name}: surface off (pads back to the device)")
                except Exception:
                    pass
            self._close(link)

        for name, dev in want.items():
            port, foreign = self._find(outs, dev)
            link = self.links.setdefault(name, {"port": None, "ident": None, "out": None, "sig": None,
                                                "ch": None, "show": False})
            if port is None:
                if link["out"] is not None:
                    self.log(f"[surface] {name}: {link['port']} is gone; will re-arm it when it is back")
                self._close(link)
                link.update(port=None, ident=None, sig=None, ch=None)
                if foreign:
                    self._say_once(("foreign", name), f"{name}: '{dev['port_match']}' matches a device that is not "
                                   "our Fire firmware ('ORC FW ...'): it cannot be a surface")
                continue
            ident = idents.get(port.split(":", 1)[0].strip())
            ch = dev.get("surface_ch", orc1_padmap.SURFACE_CH_DEFAULT)
            colours = orc1_padmap.surface_colours(dev)
            sig = repr((ch, colours))
            fresh = link["out"] is None or link["port"] != port or link["ident"] != ident
            if not fresh and link["sig"] == sig and not link["show"]:
                continue
            key = f"{port}|{ident}"
            if fresh:
                knows = ident is not None and self._armed().get(key) == ch     # armed before a brain restart
            else:
                knows = link["ch"] == ch
            show = link["show"]
            frames = ([orc1_padmap.surface_on(ch)] if show or not knows else []) + colours
            if not show and not knows:
                frames.append(orc1_padmap.surface_off())                       # armed, but the Fire stays on its steps
            try:
                if fresh:
                    self._close(link)
                    link["out"] = self.mido.open_output(port)
                    link.update(port=port, ident=ident)
                self._send_all(link, frames)
                self.painted += 1
                link.update(sig=sig, ch=ch, show=False)
                if ident is not None:
                    self._set_armed(key, ch)
                what = "shown" if show else ("armed" if not knows else "colours sent")
                self.log(f"[surface] {name}: {what} on {port} "
                         "(BROWSER > ORC surface on the Fire or SHOW ON FIRE on /pads shows it)")
            except Exception as e:
                self._say_once(("send", name, type(e).__name__), f"{name}: cannot reach {port} ({e}); will retry")
                self._close(link)
                link.update(sig=None)


# -- TAP TEMPO pad blink (2026-10-19) -------------------------------------------------
# owner: "can we get a blinking tap tempo pad for the clock?". Any surface pad whose action
# is tap tempo (orc1_padmap.TAP_ACTION) blinks the ORC clock's beat. Phase-locked to the
# clock's own beat boundary (ArpEngine.beat_phase: `since` resets on the real pulse), the
# rule the Mixtrack metronome LED learned: never free-run a timer off a BPM estimate.
#   ORC on its own clock (taps set it): rest = the pad's colour, dim; each beat = the
#     colour at full; beat 1 of the bar = white.
#   ORC following an OUTSIDE clock (the Fire's clock out, a DIN master): the same blink in
#     GREY, because a tap does nothing then (the outside clock sets the tempo).
#   No clock running: the dim rest colour, steady.
# Only sends when a pad's shown colour CHANGES (2 small SysEx per beat), and nothing at all
# when no profile has a tap pad.

FLASH_S = 0.09
DEFAULT_TAP_RGB = (0, 7, 0)


def _dim(rgb):
    return tuple((max(1, v // 3) if v else 0) for v in rgb)


def tap_colour(base, flash, downbeat, external):
    """PURE: the (r, g, b) a tap pad shows. base = its configured colour."""
    if external:
        return (6, 6, 6) if flash and downbeat else ((3, 3, 3) if flash else (1, 1, 1))
    if flash:
        return (7, 7, 7) if downbeat else tuple(base)
    return _dim(base)


class TapBlinker:
    def __init__(self, link, surfaces):
        """link: a SurfaceLink (paint_pad, painted); surfaces() -> [(name, device)] (PadMap.surfaces)."""
        self.link, self.surfaces = link, surfaces
        self._pads = []             # [(name, pad, base rgb)], refreshed by refresh()
        self._last_since = None
        self._flash_until = 0.0
        self._downbeat = False
        self._shown = {}            # (name, pad) -> rgb last sent
        self._painted = None

    def refresh(self):
        """Re-read which pads are tap pads (call ~2x a second, not every tick)."""
        pads = []
        for name, dev in self.surfaces():
            for pad in orc1_padmap.tap_pads(dev):
                c = (dev["pads"].get(f"note:{pad}") or {}).get("color")
                pads.append((name, pad, orc1_padmap.COLOR_RGB.get(c, DEFAULT_TAP_RGB)))
        self._pads = pads
        return bool(pads)

    def tick(self, now, beat, bar, external):
        """now = monotonic s; beat = (since_beat, beat_s) or None (no clock); bar = (since_bar, bar_s) or None;
        external = the ORC is following an outside clock."""
        if not self._pads:
            return
        if self.link.painted != self._painted:          # a full repaint overwrote what we showed
            self._painted = self.link.painted
            self._shown.clear()
        if beat is None:
            self._last_since = None
            self._flash_until = 0.0
        else:
            since, beat_s = beat
            if self._last_since is None or since < self._last_since - 1e-3:
                # a NEW beat (the phase wrapped). Beat 1 = the bar line is under half a beat ago.
                if self._last_since is not None:
                    self._flash_until = now + FLASH_S
                    self._downbeat = bool(bar and beat_s > 0 and bar[0] < 0.5 * beat_s)
            self._last_since = since
        flash = now < self._flash_until
        for name, pad, base in self._pads:
            rgb = tap_colour(base, flash, self._downbeat, external)
            if self._shown.get((name, pad)) != rgb:
                if self.link.paint_pad(name, pad, rgb):
                    self._shown[(name, pad)] = rgb
