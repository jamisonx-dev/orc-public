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
