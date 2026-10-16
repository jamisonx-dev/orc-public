"""
orc1_padmap.py -- per-device PAD MAPS: turn a drum pad (or any note / CC from a
named device) into a note on a NAMED module, a different CC, or an ORC action.

Why: a drum brain's pads end up "in places that are very much not a drumset"
(owner, 2026-10-07, about the Simmons SD350). Each pad should be choosable:
keep playing what it played, play something else, or DO something (grab skipback,
chord IV, RC-505 track 2 ...). Nothing here knows any particular device -- a
profile matches an input port by NAME (the ORC north star) and is created on /pads.

Config (~/.orc1_padmap.json, owned by the BRAIN, which applies + saves it on
PADMAP_SET; the web validates with the SAME validate_config):

    {"version": 1,
     "devices": {"<name>": {"port_match": "Simmons", "min_vel": 1,
                            "pads": {"note:36": {...}, "cc:4": {...}}}}}

A pad entry ("do" picks what it does; a pad with no entry passes through untouched):
    {"do": "pass"}                                     unchanged (same as no entry)
    {"do": "ignore"}                                   swallowed
    {"do": "note", "module": "drums", "note": 38, "vel": "same" | 1-127}
    {"do": "cc",   "module": "poly",  "cc": 74}        (cc pads only)
    {"do": "action", "action": "cmd:SKIPBACK_SAVE_ALL", "min_vel": 40}

Modules are NAMED and resolved live from /matrix at hit time (unbound = silent),
never a literal channel -- owner's rule since session 39.

PADS ARE TRIGGERS. Measured on the SD350 (2026-10-07): its note-off is not a
release -- it arrives right before the next hit or ~2.4 s after the hit. So an
action fires on the HIT only and the note-off is swallowed (otherwise the timeout
would fire it again); held-gesture actions (BeatBuddy "primary", page flips) are
not offered. A CC pad's action fires when the value RISES through its threshold.
The same capture caught a pad DOUBLE-TRIGGER (two note-ons 10 ms apart from one
hit), so an action re-arms only REARM_S after it fired: a toggle (RC rec/play,
arp on/off) fired twice in 10 ms would cancel itself out.

SURFACES (2026-10-14): a device profile with "surface": true is a CONTROL SURFACE the
ORC draws on: owner's own Akai Fire firmware ("ORC FW ...") in its surface mode sends
each pad hit as SysEx F0 7D 'ORC' 0C <pad 0-63> <vel, 0 = release> F7 (firmware v0.21:
NO MIDI channel, so the Fire's own sequencer keeps every channel and all its real notes
pass straight through), and shows the colour each pad entry names ("color": one of
COLORS). A "note:<n>" entry = pad n. orc1_surface_link.py arms / shows the surface and
paints it; surface_frames() builds those messages. A surface pad with NO entry is silent.
"surface_ch" is legacy (v0.19/v0.20 firmware sent notes on it): accepted, unused.

This module is pure: no mido, no ports. The brain hands it `emit(kind, ch, a, b)`
(kind = "note_on" | "note_off" | "cc", ch 1-16), `module_ch(name) -> 1-16 | None`
and `run_action(action)`.
"""

import copy
import json
import os
import re
import threading
import time

import orc1_pedal

CTRL_CH = 13                      # the ORC's control channel: never an output

# Actions a pad may run: every pedal switch action that makes sense for a
# press-only trigger. "primary" needs a hold, "transition" toggles across presses
# of ONE switch, page flips belong to the FCB.
_NOT_FOR_PADS = {"primary", "transition", "page:up", "page:down"}
EXTRA_CHOICES = [("cmd:SKIPBACK_SAVE_ALL", "Skipback: GRAB ALL tracks"),
                 ("cmd:ARP_TOGGLE", "Arp on/off"),
                 ("cmd:FX_TOGGLE", "FX on/off"),
                 ("cmd:CME_PRESET", "CME: next preset")]
ACTION_CHOICES = ([c for c in orc1_pedal.SWITCH_CHOICES
                   if c[0] and c[0] not in _NOT_FOR_PADS]
                  + [c for c in EXTRA_CHOICES
                     if c[0] not in {v for v, _ in orc1_pedal.SWITCH_CHOICES}])

# Pad colours for a surface, as the Fire's LED levels (R, G, B each 0-7). Ordered for the /pads picker.
COLORS = [("red", (7, 0, 0)), ("orange", (7, 2, 0)), ("yellow", (6, 5, 0)), ("green", (0, 7, 0)),
          ("cyan", (0, 6, 6)), ("blue", (0, 0, 7)), ("purple", (4, 0, 7)), ("pink", (7, 0, 3)),
          ("white", (5, 5, 5)), ("dim", (1, 1, 1))]
COLOR_RGB = dict(COLORS)
SURFACE_CH_DEFAULT = 16
ORC_HDR = [0x7D, 0x4F, 0x52, 0x43]  # the Fire firmware's host protocol: F0 7D 'O' 'R' 'C' <cmd> ... F7
SURF_PADS, SURF_PER_MSG = 64, 10    # its 4 x 16 pads; 0A carries at most 10 pads (a 40-byte SysEx buffer)
SURF_PAD_HDR = ORC_HDR + [0x0C]     # Fire -> ORC (v0.21): a surface pad hit, 0C <pad 0-63> <vel, 0 = release>

NOTE_DOS = ("pass", "ignore", "note", "action")
CC_DOS = ("pass", "ignore", "cc", "action")
_KEY_RE = re.compile(r"^(note|cc):(\d{1,3})$")


def valid_action(a):
    return (isinstance(a, str) and a not in _NOT_FOR_PADS
            and orc1_pedal.valid_switch_action(a) and a != "")


def _int(v, lo, hi):
    n = int(v)
    if not lo <= n <= hi:
        raise ValueError(f"{n} not in {lo}-{hi}")
    return n


def _clean_pad(key, e, modules):
    """One pad entry -> cleaned dict, or raise ValueError."""
    kind = _KEY_RE.match(key).group(1)
    if not isinstance(e, dict):
        raise ValueError("not an object")
    do = e.get("do", "pass")
    if do not in (NOTE_DOS if kind == "note" else CC_DOS):
        raise ValueError(f"{kind} pad can't do {do!r}")
    out = {"do": do}
    if do in ("note", "cc"):
        mod = e.get("module")
        if mod not in modules:
            raise ValueError(f"unknown module {mod!r}")
        out["module"] = mod
        if do == "note":
            out["note"] = _int(e.get("note"), 0, 127)
            v = e.get("vel", "same")
            out["vel"] = "same" if v in ("same", None, "") else _int(v, 1, 127)
        else:
            out["cc"] = _int(e.get("cc"), 0, 127)
    elif do == "action":
        if not valid_action(e.get("action")):
            raise ValueError(f"bad action {e.get('action')!r}")
        out["action"] = e["action"]
        if e.get("min_vel") not in (None, ""):
            out["min_vel"] = _int(e.get("min_vel"), 1, 127)
    if isinstance(e.get("label"), str) and e["label"].strip():
        out["label"] = e["label"].strip()[:24]
    c = e.get("color")
    if c not in (None, "", "off"):
        if c not in COLOR_RGB:
            raise ValueError(f"unknown colour {c!r}")
        out["color"] = c
    return out


def default_config():
    return {"version": 1, "devices": {}}


def validate_config(conf, modules):
    """Return (clean_config, errors). Anything invalid is dropped and reported,
    never passed through. `modules` = the module keys a pad may target."""
    errs, out = [], default_config()
    modules = set(modules or ())
    devs = conf.get("devices") if isinstance(conf, dict) else None
    for name, d in (devs.items() if isinstance(devs, dict) else ()):
        name = str(name).strip()[:40]
        if not name or not isinstance(d, dict):
            errs.append(f"device {name!r}: dropped"); continue
        pm = str(d.get("port_match") or name).strip()
        if not pm:
            errs.append(f"{name}: empty port match"); continue
        dev = {"port_match": pm[:40], "min_vel": 1, "pads": {}}
        try:
            dev["min_vel"] = _int(d.get("min_vel", 1), 1, 127)
        except (TypeError, ValueError):
            errs.append(f"{name}: bad min_vel")
        if d.get("surface") is True:
            dev["surface"] = True
            try:
                ch = _int(d.get("surface_ch", SURFACE_CH_DEFAULT), 1, 16)
                if ch == CTRL_CH:
                    raise ValueError("ch 13 is the ORC's control channel")
                dev["surface_ch"] = ch
            except (TypeError, ValueError) as ex:
                errs.append(f"{name}: surface channel: {ex}")
                dev["surface_ch"] = SURFACE_CH_DEFAULT
        pads = d.get("pads") if isinstance(d.get("pads"), dict) else {}
        for key, e in pads.items():
            m = _KEY_RE.match(str(key))
            if not m or int(m.group(2)) > 127:
                errs.append(f"{name}: bad pad {key!r}"); continue
            try:
                p = _clean_pad(str(key), e, modules)
            except (TypeError, ValueError) as ex:
                errs.append(f"{name} {key}: {ex}"); continue
            if p["do"] != "pass" or "label" in p or "color" in p:
                dev["pads"][str(key)] = p
        out["devices"][name] = dev
    return out, errs


class PadMap:
    """The live pad map. Thread-safe; handle() runs on the input threads."""

    HITS_KEEP = 32
    REARM_S = 0.08                  # an action pad ignores re-hits this soon after

    def __init__(self, emit, module_ch, run_action, log=print, hits_file=None):
        self.emit, self.module_ch, self.run_action, self.log = emit, module_ch, run_action, log
        self.hits_file = hits_file          # tmpfs file the /pads page polls (learn)
        self._lock = threading.Lock()
        self.conf = default_config()
        self._held = {}                     # (device, src_ch, src_note) -> (ch, note)
        self._cc_last = {}                  # (device, cc) -> last value (edge detect)
        self.hits = []                      # newest last: {device, key, value, t}
        self._seq = 0
        self._fired = {}                    # (device, key) -> monotonic time of last action
        self.clock = time.monotonic
        # actions run off the input thread (a chord / RC tap must not stall MIDI in)
        self.spawn = lambda a: threading.Thread(target=self._run, args=(a,),
                                                daemon=True).start()

    def set_config(self, conf):
        """Apply an already-validated config. Sounding mapped notes are released
        on their old destination so a remap can't strand one."""
        with self._lock:
            held, self._held = self._held, {}
            self.conf = copy.deepcopy(conf)
            self._cc_last = {}
        for ch, note in held.values():
            self._emit("note_off", ch, note, 0)

    def _emit(self, kind, ch, a, b):
        try:
            self.emit(kind, ch, a, b)
        except Exception as e:
            self.log(f"  ! padmap emit: {e}")

    def device_for(self, source_name):
        n = (source_name or "").lower()
        if not n:
            return None, None
        for name, d in self.conf["devices"].items():
            if d["port_match"].lower() in n:
                return name, d
        return None, None

    def _record(self, dev, key, value):
        self._seq += 1
        self.hits.append({"device": dev, "key": key, "value": value,
                          "t": round(time.time(), 3), "seq": self._seq})
        del self.hits[:-self.HITS_KEEP]
        if self.hits_file:
            try:
                tmp = self.hits_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"hits": self.hits}, f)
                os.replace(tmp, self.hits_file)
            except Exception:
                pass

    def handle(self, msg, source_name):
        """True = the pad map consumed `msg` (the brain must not route it on).
        False = not ours (no profile for this device, or the pad passes through)."""
        t = getattr(msg, "type", None)
        if t not in ("note_on", "note_off", "control_change", "sysex"):
            return False
        with self._lock:
            dev, d = self.device_for(source_name)
            if not dev:
                return False
            if d.get("surface"):
                # A surface is our Fire firmware (v0.21+). Its surface pads are NOT MIDI notes: they arrive as
                # F0 7D 'ORC' 0C <pad> <vel> F7, so they take no channel from anyone (owner 2026-10-14: "they
                # recognise each other, they can just talk however"). Every real note / CC on the port is the
                # Fire's OWN sequencer and passes through untouched.
                if t != "sysex":
                    return False
                data = list(getattr(msg, "data", ()))
                if data[:5] != SURF_PAD_HDR or len(data) < 7:
                    return False
                pad, vel = data[5] & 0x3F, data[6] & 0x7F
                return self._note(dev, d, _SurfHit("note_on" if vel else "note_off", pad, vel))
            if t == "sysex":
                return False
            if t == "control_change":
                return self._cc(dev, d, msg)
            return self._note(dev, d, msg)

    def _note(self, dev, d, msg):
        on = msg.type == "note_on" and msg.velocity > 0
        key = f"note:{msg.note}"
        if on:
            self._record(dev, key, msg.velocity)
        e = d["pads"].get(key)
        hk = (dev, msg.channel, msg.note)
        if not e and d.get("surface"):
            return True                     # a surface pad with no job: silent, never "whatever is on ch 16"
        if not e or e["do"] == "pass":
            return False
        if e["do"] == "ignore":
            return True
        if e["do"] == "action":
            if on and msg.velocity >= e.get("min_vel", d["min_vel"]):
                self._fire(dev, key, e["action"])
            return True                     # the note-off is never a gesture
        # do == "note"
        old = self._held.pop(hk, None)
        if old:
            self._emit("note_off", old[0], old[1], 0)
        if not on:
            return True
        if msg.velocity < d["min_vel"]:
            return True
        ch = self.module_ch(e["module"])
        if not ch or int(ch) == CTRL_CH:
            return True                     # unbound module: silent, never "ch 1"
        vel = msg.velocity if e["vel"] == "same" else e["vel"]
        self._held[hk] = (int(ch), e["note"])
        self._emit("note_on", int(ch), e["note"], vel)
        return True

    def _cc(self, dev, d, msg):
        key = f"cc:{msg.control}"
        prev = self._cc_last.get((dev, msg.control), 0)
        self._cc_last[(dev, msg.control)] = msg.value
        if msg.value != prev:
            self._record(dev, key, msg.value)
        e = d["pads"].get(key)
        if not e or e["do"] == "pass":
            return False
        if e["do"] == "ignore":
            return True
        if e["do"] == "action":
            th = e.get("min_vel", 64)
            if prev < th <= msg.value:
                self._fire(dev, key, e["action"])
            return True
        ch = self.module_ch(e["module"])
        if ch and int(ch) != CTRL_CH:
            self._emit("cc", int(ch), e["cc"], msg.value)
        return True

    def surfaces(self):
        """[(name, device)] for the profiles that are surfaces (a copy: safe off the lock)."""
        with self._lock:
            return [(n, copy.deepcopy(d)) for n, d in self.conf["devices"].items() if d.get("surface")]

    def _fire(self, dev, key, action):
        now = self.clock()
        last = self._fired.get((dev, key))
        if last is not None and now - last < self.REARM_S:
            return                          # a pad double-trigger, not a 2nd hit
        self._fired[(dev, key)] = now
        self.spawn(action)

    def _run(self, action):
        try:
            self.run_action(action)
        except Exception as e:
            self.log(f"  ! padmap action {action!r}: {e}")


class _SurfHit:
    """A surface pad hit (from the 0C SysEx) in the shape _note() reads. channel None: it has none."""
    def __init__(self, type, note, velocity):
        self.type, self.note, self.velocity, self.channel = type, note, velocity, None


def surface_frames(dev):
    """The SysEx payloads (without F0 / F7) that put a surface device in surface mode and paint all 64 pads:
    [0B 01 <ch>] then 0A <first> <count> <r g b ...> in runs of SURF_PER_MSG. A pad's colour = its "note:<n>" entry's
    "color" (n 0-63); anything else is dark. Pure: what the link sends, and what the tests check against the firmware's
    own parsing rules (ctl.c)."""
    return [surface_on(dev.get("surface_ch", SURFACE_CH_DEFAULT))] + surface_colours(dev)


def surface_on(ch):
    """0B 01 <ch>: show the surface, pads on channel ch (1-16). The firmware also remembers ch + that a surface exists,
    so BROWSER > ORC surface can bring it back later."""
    return ORC_HDR + [0x0B, 1, ch]


def surface_colours(dev):
    """The 0A messages painting all 64 pads (the firmware keeps them even while the surface is hidden)."""
    rgb = [(0, 0, 0)] * SURF_PADS
    for key, e in dev.get("pads", {}).items():
        m = _KEY_RE.match(key)
        if m and m.group(1) == "note" and int(m.group(2)) < SURF_PADS and e.get("color") in COLOR_RGB:
            rgb[int(m.group(2))] = COLOR_RGB[e["color"]]
    out = []
    for first in range(0, SURF_PADS, SURF_PER_MSG):
        run = rgb[first:first + SURF_PER_MSG]
        out.append(ORC_HDR + [0x0A, first, len(run)] + [v for c in run for v in c])
    return out


def surface_off():
    """The payload that hands the pads back to the device's own sequencer."""
    return ORC_HDR + [0x0B, 0]
