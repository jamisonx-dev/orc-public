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
    {"do": "cc",   "module": "poly",  "cc": 74}        a CC input: its value passes through
    {"do": "cc",   "module": "poly",  "cc": 74,         a NOTE pad (2026-10-19): sends the CC
     "value": "vel" | 0-127, "mode": "press" | "momentary" | "toggle"}
    {"do": "action", "action": "cmd:SKIPBACK_SAVE_ALL", "min_vel": 40}

Destination of "note" / "cc": a NAMED module ("module"), or an OUTPUT PORT + CHANNEL
({"port": "pisound", "ch": 10}: a cfg.PORTS key, ch 1-16, never 13 = the ORC's control
channel). owner 2026-10-19: "can it not be set to send an arbitrary cc to a
channel?" External gear has real channels; ORC modules are still NAMED and resolved
live from /matrix at hit time (unbound = silent), never a literal channel on the
internal bus -- owner's rule since session 39.
A note pad's CC: "press" sends `value` on the hit; "momentary" sends it on the hit and
0 on the release (the device must send a REAL release: see PADS ARE TRIGGERS); "toggle"
alternates `value` / 0 on each hit. "vel" = the hit's velocity.

PADS ARE TRIGGERS. Measured on the SD350 (2026-10-07): its note-off is not a
release -- it arrives right before the next hit or ~2.4 s after the hit. So an
action fires on the HIT only and the note-off is swallowed (otherwise the timeout
would fire it again); held-gesture actions (BeatBuddy "primary", page flips) are
not offered. A CC pad's action fires when the value RISES through its threshold.
The same capture caught a pad DOUBLE-TRIGGER (two note-ons 10 ms apart from one
hit), so an action re-arms only REARM_S after it fired: a toggle (RC rec/play,
arp on/off) fired twice in 10 ms would cancel itself out.

NO FIRE SURFACE ANY MORE (2026-11-13). Until Fire firmware v0.35 a profile marked
"surface": true was a pad page the ORC painted on our Fire firmware (hits as SysEx 0C,
colours as 0A / 0B). Firmware v0.36 removed that page (ORC mode replaced it), so the
whole surface feature left the ORC too. A saved profile still marked "surface" is
DROPPED on load and reported: its "note:<n>" entries meant pad numbers, never MIDI
notes, and must not start remapping the Fire sequencer's real notes.

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

# Our Fire firmware's port is "ORC FW seq" up to v0.35b and "FLAME seq" after the rename
# (2026-11-12). Names in one group mean the same port, so a profile saved with either
# still finds the Fire (cfg.FIRE_SEQ_PORT_NAMES lists both too).
FIRE_FW_NAME_ALIASES = (("orc fw seq", "flame seq"),)


def port_name_matches(match, port_name):
    """Case-insensitive substring match of `match` in `port_name`, where our
    firmware's old and new names (FIRE_FW_NAME_ALIASES) count as the same."""
    m, n = (match or "").lower().strip(), (port_name or "").lower()
    if not m or not n:
        return False
    if m in n:
        return True
    for group in FIRE_FW_NAME_ALIASES:
        for a in group:
            others = [b for b in group if b != a]
            if a in m:                                   # the profile holds one whole name: try it with the other
                if any(m.replace(a, b) in n for b in others):
                    return True
            elif len(m) >= 3 and m in a:                 # the profile is part of one name ("ORC FW")
                if any(b in n for b in others):
                    return True
    return False

# Actions a pad may run: every pedal switch action that makes sense for a
# press-only trigger. "primary" needs a hold, "transition" toggles across presses
# of ONE switch, page flips belong to the FCB.
_NOT_FOR_PADS = {"primary", "transition", "page:up", "page:down"}
EXTRA_CHOICES = [("cmd:SKIPBACK_SAVE_ALL", "Skipback: GRAB ALL tracks"),
                 ("cmd:ARP_TOGGLE", "Arp on/off"),
                 ("cmd:FX_TOGGLE", "FX on/off"),
                 ("cmd:CME_PRESET", "CME: next preset")]
# Recall scene N (orc1_scenes, 2026-10-26), with the scene's own timing.
EXTRA_CHOICES += [(f"scene:{n}", f"Recall scene {n}") for n in range(1, 129)]
ACTION_CHOICES = ([c for c in orc1_pedal.SWITCH_CHOICES
                   if c[0] and c[0] not in _NOT_FOR_PADS]
                  + [c for c in EXTRA_CHOICES
                     if c[0] not in {v for v, _ in orc1_pedal.SWITCH_CHOICES}])

NOTE_DOS = ("pass", "ignore", "note", "cc", "action", "grab")
CC_MODES = ("press", "momentary", "toggle")      # a NOTE pad sending a CC
_PORT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")

# SKIPBACK GRAB pads (2026-10-19, owner: "trigger that amount with a press of a
# fire pad"; "the sample is usually going to be either quantized bar amounts or a
# 'sample while holding the button down'"; seconds don't fit long grabs).
#   {"do": "grab", "scope": "all" | "track", "track": <id>,     (scope track)
#    "tracks": "armed" | [<id>, ...],                            (scope all)
#    "len": "bars" | "hold" | "time" | "all",
#    "bars": 1-256, "secs": 1-7200 (time), "snap": true|false (hold: whole bars)}
# bars / time / all END AT THE PRESS (the press is the downbeat: the sampler's PRESS
# anchor). hold = press to release; snapped, it keeps the PRESS as the start and
# rounds the length to whole bars (never under one), the sampler's forward capture.
# hold needs a device that sends a REAL release (a keyboard, most pad controllers).
# The SD350's note-off is a timer, not a release (measured 2026-10-07): on a device
# like that a hold grabs the timer's length, so pick bars / time there.
GRAB_LENS = ("bars", "hold", "time", "all")
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


def _clean_dest(e, modules, ports):
    """A note / cc entry's destination: {"module": m} or {"port": p, "ch": 1-16}."""
    if e.get("port") not in (None, ""):
        p = str(e["port"])
        if not _PORT_RE.match(p) or (ports is not None and p not in ports):
            raise ValueError(f"unknown output port {p!r}")
        ch = _int(e.get("ch"), 1, 16)
        if ch == CTRL_CH:
            raise ValueError("ch 13 is the ORC's control channel")
        return {"port": p, "ch": ch}
    mod = e.get("module")
    if mod not in modules:
        raise ValueError(f"unknown module {mod!r}")
    return {"module": mod}


def _clean_pad(key, e, modules, ports=None):
    """One pad entry -> cleaned dict, or raise ValueError. ports = the output port
    keys a pad may send to (None = don't check the name, only its shape)."""
    kind = _KEY_RE.match(key).group(1)
    if not isinstance(e, dict):
        raise ValueError("not an object")
    do = e.get("do", "pass")
    if do not in (NOTE_DOS if kind == "note" else CC_DOS):
        raise ValueError(f"{kind} pad can't do {do!r}")
    out = {"do": do}
    if do in ("note", "cc"):
        out.update(_clean_dest(e, modules, ports))
        if do == "note":
            out["note"] = _int(e.get("note"), 0, 127)
            v = e.get("vel", "same")
            out["vel"] = "same" if v in ("same", None, "") else _int(v, 1, 127)
        else:
            out["cc"] = _int(e.get("cc"), 0, 127)
            if kind == "note":                       # a NOTE pad sending a CC
                v = e.get("value", 127)
                out["value"] = "vel" if v == "vel" else _int(v, 0, 127)
                m = e.get("mode", "press")
                if m not in CC_MODES:
                    raise ValueError(f"CC mode {m!r}")
                out["mode"] = m
    elif do == "action":
        if not valid_action(e.get("action")):
            raise ValueError(f"bad action {e.get('action')!r}")
        out["action"] = e["action"]
        if e.get("min_vel") not in (None, ""):
            out["min_vel"] = _int(e.get("min_vel"), 1, 127)
    elif do == "grab":
        out.update(_clean_grab(e))
        if e.get("min_vel") not in (None, ""):
            out["min_vel"] = _int(e.get("min_vel"), 1, 127)
    if isinstance(e.get("label"), str) and e["label"].strip():
        out["label"] = e["label"].strip()[:24]
    return out                      # an old entry's "color" (the Fire surface's pad colour) is dropped


def _clean_grab(e):
    """The grab fields of a pad entry, cleaned (raise ValueError)."""
    out = {}
    scope = e.get("scope", "all")
    if scope not in ("all", "track"):
        raise ValueError(f"grab scope {scope!r}")
    out["scope"] = scope
    if scope == "track":
        out["track"] = _int(e.get("track"), 0, 63)
    else:
        t = e.get("tracks", "armed")
        if t in ("armed", None, "", []):
            out["tracks"] = "armed"
        elif isinstance(t, list) and len(t) <= 16:
            out["tracks"] = sorted({_int(x, 0, 63) for x in t})
        else:
            raise ValueError("grab tracks must be 'armed' or a list of track ids")
    ln = e.get("len", "bars")
    if ln not in GRAB_LENS:
        raise ValueError(f"grab length {ln!r}")
    out["len"] = ln
    if ln == "bars":
        out["bars"] = _int(e.get("bars", 4), 1, 256)
    elif ln == "time":
        out["secs"] = _int(e.get("secs", 60), 1, 7200)
    elif ln == "hold":
        out["snap"] = bool(e.get("snap", True))
    return out


def grab_plan(e, press_ts, release_ts, now, bpm, beats_per_bar=4):
    """PURE: what a grab pad grabs. -> (seconds, end_ts, delay_s, tracks)
      seconds  -- length to grab (0 = everything the tracks hold in common)
      end_ts   -- wall-clock time the grab ENDS at (the grab tool's SKIPBACK_PRESS_TS)
      delay_s  -- wait this long before running it (a snapped-UP hold ends in the future)
      tracks   -- None = every recording (armed) track, else the track ids
    bars / time / all end at the press; hold runs press -> release (None = not
    released yet: no grab)."""
    if e.get("scope") == "track":
        tracks = [int(e["track"])]
    else:
        tracks = None if e.get("tracks", "armed") == "armed" else list(e["tracks"])
    bpm = max(20.0, min(300.0, float(bpm or 120.0)))
    bar_s = max(1, int(beats_per_bar)) * 60.0 / bpm
    ln = e.get("len", "bars")
    if ln == "hold":
        if release_ts is None:
            return None
        held = max(0.0, release_ts - press_ts)
        if e.get("snap", True):
            secs = max(1, int(round(held / bar_s))) * bar_s
        else:
            secs = held
        end = press_ts + secs
        return (secs, end, max(0.0, end - now), tracks)
    if ln == "bars":
        secs = int(e.get("bars", 4)) * bar_s
    elif ln == "time":
        secs = float(e.get("secs", 60))
    else:
        secs = 0.0
    return (secs, press_ts, 0.0, tracks)


def default_config():
    return {"version": 1, "devices": {}}


def validate_config(conf, modules, ports=None):
    """Return (clean_config, errors). Anything invalid is dropped and reported,
    never passed through. `modules` = the module keys a pad may target; `ports` =
    the output port keys (cfg.PORTS) a pad may send to (None = shape check only)."""
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
        if d.get("surface"):
            # a Fire pad-page profile (gone with Fire firmware v0.36): its entries were pad numbers, not notes
            errs.append(f"{name}: dropped (it was a Fire surface page; the Fire firmware no longer has one)")
            continue
        pads = d.get("pads") if isinstance(d.get("pads"), dict) else {}
        for key, e in pads.items():
            m = _KEY_RE.match(str(key))
            if not m or int(m.group(2)) > 127:
                errs.append(f"{name}: bad pad {key!r}"); continue
            try:
                p = _clean_pad(str(key), e, modules, ports)
            except (TypeError, ValueError) as ex:
                errs.append(f"{name} {key}: {ex}"); continue
            if p["do"] != "pass" or "label" in p:
                dev["pads"][str(key)] = p
        out["devices"][name] = dev
    return out, errs


class PadMap:
    """The live pad map. Thread-safe; handle() runs on the input threads."""

    HITS_KEEP = 32
    REARM_S = 0.08                  # an action pad ignores re-hits this soon after

    def __init__(self, emit, module_ch, run_action, log=print, hits_file=None,
                 run_grab=None):
        """run_grab(entry, press_ts, release_ts): a grab pad fired (release_ts None
        unless it is a hold)."""
        self.emit, self.module_ch, self.run_action, self.log = emit, module_ch, run_action, log
        self.run_grab = run_grab
        self._holding = {}                  # (device, key) -> press wall time of a HOLD grab
        self._toggle = {}                   # (device, key) -> a TOGGLE CC pad is on
        self.wall = time.time               # grab timestamps: the grab tool's clock
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
        self.spawn_grab = lambda fn: threading.Thread(target=fn, daemon=True,
                                                      name="padmap-grab").start()

    def set_config(self, conf):
        """Apply an already-validated config. Sounding mapped notes are released
        on their old destination so a remap can't strand one."""
        with self._lock:
            held, self._held = self._held, {}
            self._holding = {}              # a remap mid-hold: no grab
            self.conf = copy.deepcopy(conf)
            self._cc_last = {}
            self._toggle = {}
        for ch, note, port in held.values():
            self._emit("note_off", ch, note, 0, port)

    def _emit(self, kind, ch, a, b, port=None):
        """port None = the ORC's internal synth bus (a module); else an output port key."""
        try:
            if port is None:
                self.emit(kind, ch, a, b)
            else:
                self.emit(kind, ch, a, b, port)
        except Exception as e:
            self.log(f"  ! padmap emit: {e}")

    def _dest(self, e):
        """(channel, port) for a note / cc entry, or None = silent. A module is resolved
        LIVE from /matrix (unbound = silent, never ch 1); a port has its own channel."""
        if e.get("port"):
            return int(e["ch"]), e["port"]
        ch = self.module_ch(e.get("module"))
        if not ch or int(ch) == CTRL_CH:
            return None
        return int(ch), None

    def device_for(self, source_name):
        n = (source_name or "").lower()
        if not n:
            return None, None
        for name, d in self.conf["devices"].items():
            if port_name_matches(d["port_match"], n):
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
        if t not in ("note_on", "note_off", "control_change"):
            return False
        with self._lock:
            dev, d = self.device_for(source_name)
            if not dev:
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
        if not e or e["do"] == "pass":
            return False
        if e["do"] == "ignore":
            return True
        if e["do"] == "action":
            if on and msg.velocity >= e.get("min_vel", d["min_vel"]):
                self._fire(dev, key, e["action"])
            return True                     # the note-off is never a gesture
        if e["do"] == "grab":
            self._grab(dev, d, key, e, on, msg.velocity)
            return True
        if e["do"] == "cc":
            self._note_cc(dev, d, key, e, on, msg.velocity)
            return True
        # do == "note"
        old = self._held.pop(hk, None)
        if old:
            self._emit("note_off", old[0], old[1], 0, old[2])
        if not on:
            return True
        if msg.velocity < d["min_vel"]:
            return True
        dest = self._dest(e)
        if not dest:
            return True                     # unbound module: silent, never "ch 1"
        ch, port = dest
        vel = msg.velocity if e["vel"] == "same" else e["vel"]
        self._held[hk] = (ch, e["note"], port)
        self._emit("note_on", ch, e["note"], vel, port)
        return True

    def _note_cc(self, dev, d, key, e, on, vel):
        """A NOTE pad sending a CC: press / momentary (0 on the release) / toggle."""
        mode = e.get("mode", "press")
        dest = self._dest(e)
        if not dest:
            return
        ch, port = dest
        if not on:
            if mode == "momentary" and (dev, key) in self._toggle:
                self._toggle.pop((dev, key))
                self._emit("cc", ch, e["cc"], 0, port)
            return
        if vel < e.get("min_vel", d["min_vel"]):
            return
        val = vel if e.get("value") == "vel" else int(e.get("value", 127))
        if mode == "toggle":
            now = self.clock()
            last = self._fired.get((dev, key))
            if last is not None and now - last < self.REARM_S:
                return                      # a double-trigger would toggle straight back
            self._fired[(dev, key)] = now
            if self._toggle.pop((dev, key), False):
                val = 0
            else:
                self._toggle[(dev, key)] = True
        elif mode == "momentary":
            self._toggle[(dev, key)] = True  # pressed: the release sends 0
        self._emit("cc", ch, e["cc"], val, port)

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
        dest = self._dest(e)
        if dest:
            self._emit("cc", dest[0], e["cc"], msg.value, dest[1])
        return True

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

    def _grab(self, dev, d, key, e, on, vel):
        """A grab pad. bars / time / all: grab on the hit (the press = the end).
        hold: the press starts it, the RELEASE grabs press -> release."""
        hk = (dev, key)
        if e.get("len") == "hold":
            if on:
                if vel < e.get("min_vel", d["min_vel"]) or hk in self._holding:
                    return                  # soft hit, or a re-press while already held
                self._holding[hk] = self.wall()
            elif hk in self._holding:
                start = self._holding.pop(hk)
                self._spawn_grab(e, start, self.wall())
            return
        if not on or vel < e.get("min_vel", d["min_vel"]):
            return
        now = self.clock()
        last = self._fired.get(hk)
        if last is not None and now - last < self.REARM_S:
            return                          # a pad double-trigger, not a 2nd grab
        self._fired[hk] = now
        self._spawn_grab(e, self.wall(), None)

    def _spawn_grab(self, e, press_ts, release_ts):
        if not self.run_grab:
            return
        e = copy.deepcopy(e)

        def go():
            try:
                self.run_grab(e, press_ts, release_ts)
            except Exception as ex:
                self.log(f"  ! padmap grab: {ex}")
        self.spawn_grab(go)
