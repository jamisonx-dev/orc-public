"""orc1_dawsurf_bind -- the DAW surface's ACTIONS and the BINDING table that maps any control to them.

owner, 2026-11-09: "everything, ultimately, is a control surface that could be mapped." So the
bridge (orc1_dawsurf_bridge) does not hard-wire a controller: every MIDI message it reads off
the ORC's port ORC-daw (never the synth bus, 2026-11-09) is looked up here, and what it finds is an ACTION. The Fire's DAW page and the
BeatStep Pro's Mackie port are built-in surfaces that call the same actions in the bridge.

VIEWS. A view is a window of tracks one surface shows: "fire" (the Fire's DAW page, 4 rows),
"bsp" (the BeatStep Pro's 8 Mackie strips + its bank pads), "cc" (the generic CC strips: the
shipped fader CCs, bank CCs). Each view is SHARED (follows one common window) or INDEPENDENT (its
own offset), chosen per view (cfg.DAWSURF_VIEW_MODES, or a view_mode action at run time).
Independent views need the OSC dialect (the bridge then knows every track); with Mackie there is
only REAPER's one 8-strip window, so every view is shared whatever is asked.

THE ACTIONS (one dict per binding; "view" defaults to "cc"; "slot" = 0-based strip in that view,
or "master"):
    fader      slot          the track's volume. CC 0-127 = the fader position (absolute), or
                             "rel": true = a relative knob (64 = still, 65.. up, 63.. down; one
                             tick = "step" of the 0-1 range, default 1/128)
    pan        slot          the pan. Absolute CC 0-127 (OSC only: Mackie has no absolute pan)
                             or "rel": true (a V-pot turn per tick, both dialects)
    mute / solo / rec / select   slot     a press toggles it (REAPER answers with the new state)
    transport  name          play / stop / record / loop (a press) or rewind / fast_fwd (held)
    bank       dir  (+1/-1)  the view moves 8 tracks (stops at the last track)
    shift      n    (+/-)    the view moves n tracks
    goto_bank  bank (0-..)   the view jumps to tracks 8*bank+1 ..
    view_mode  surface, mode ("toggle" / "shared" / "independent"); surface may be "all"
A press = a note-on (velocity > 0) or a CC rising through 64; a release = a note-off / velocity 0
or a CC falling below 64 (used by rewind / fast_fwd only).

THE BINDING FILE: ~/.orc1_dawsurf_bindings.json (cfg.DAWSURF_BINDINGS_FILE), re-read when it changes:
    {"bindings": [
       {"cc": 74, "ch": 3, "action": "pan", "slot": 2, "view": "cc"},
       {"note": 60, "action": "transport", "name": "play"},
       {"cc": 20, "action": "fader", "slot": 4, "view": "bsp", "feedback": true},
       {"cc": 31, "action": "none"}
     ],
     "replace_defaults": false}
  "cc" or "note" = the control; "ch" = 1-16 or "daw" (the channel the daw module is bound to in
  /matrix; the default). "feedback": true on a fader binding = the DAW's moves are sent back as that
  CC to the Fire port (the shipped fader CCs do this). "action": "none" switches a shipped binding
  off. A file binding REPLACES the shipped one on the same control; "replace_defaults": true drops
  every shipped binding. Bad entries are skipped and logged, never fatal.
  The bridge reads the ORC's port ORC-daw, which carries only the channel the daw module is bound
  to in /matrix (2026-11-09: no longer the synth bus). A binding on another channel never arrives:
  the daw channel is the place for anything.

THE SHIPPED DEFAULTS reproduce the 2026-11-09 behaviour, from orc1_config: DAWSURF_FADER_CC (CC 20-27
= strips 1-8 of the "cc" view, 28 = master, all with feedback), DAWSURF_BANK_CC (29 / 30 = bank
left / right of the "cc" view), DAWSURF_BANK_NOTES (the BSP's control-mode pads 36-51 = goto_bank
on the "bsp" view), DAWSURF_VIEW_CC (CC 31 / 32 / 33 = toggle fire / bsp / cc between shared and
independent).

Pure: no ports, no threads; the file is read only when a table is built / refreshed.
"""
import json
import os

import orc1_config as cfg

SURFACES = ("fire", "bsp", "cc")
MODES = ("shared", "independent")
STRIP_ACTIONS = ("fader", "pan", "mute", "solo", "rec", "select")
TRANSPORT_PRESS = ("play", "stop", "record", "loop")
TRANSPORT_HOLD = ("rewind", "fast_fwd")
ACTIONS = STRIP_ACTIONS + ("transport", "bank", "shift", "goto_bank", "view_mode", "none")
DAW = "daw"


def _slot_ok(s):
    return s == "master" or (isinstance(s, int) and not isinstance(s, bool) and s >= 0)


def check_binding(b):
    """A binding dict -> (normalised dict, None) or (None, why it was refused)."""
    if not isinstance(b, dict):
        return None, "not an object"
    d = dict(b)
    kinds = [k for k in ("cc", "note") if k in d]
    if len(kinds) != 1:
        return None, "needs exactly one of cc / note"
    kind = kinds[0]
    num = d[kind]
    if not isinstance(num, int) or isinstance(num, bool) or not 0 <= num <= 127:
        return None, "%s must be 0-127" % kind
    ch = d.get("ch", DAW)
    if ch != DAW and (not isinstance(ch, int) or isinstance(ch, bool) or not 1 <= ch <= 16):
        return None, 'ch must be 1-16 or "daw"'
    a = d.get("action")
    if a not in ACTIONS:
        return None, "unknown action %r" % (a,)
    view = d.get("view", "cc")
    if view not in SURFACES:
        return None, "unknown view %r" % (view,)
    out = {"kind": kind, "num": num, "ch": ch, "action": a, "view": view}
    if a in STRIP_ACTIONS:
        if not _slot_ok(d.get("slot")):
            return None, 'slot must be a strip number from 0 or "master"'
        out["slot"] = d["slot"]
        if a in ("fader", "pan"):
            out["rel"] = bool(d.get("rel", False))
            step = d.get("step", 1 / 128.0)
            if not isinstance(step, (int, float)) or isinstance(step, bool) or not 0 < step <= 1:
                return None, "step must be a number above 0, at most 1"
            out["step"] = float(step)
            out["feedback"] = bool(d.get("feedback", False)) and a == "fader" and not out["rel"]
    elif a == "transport":
        if d.get("name") not in TRANSPORT_PRESS + TRANSPORT_HOLD:
            return None, "transport name must be one of %s" % ", ".join(TRANSPORT_PRESS + TRANSPORT_HOLD)
        out["name"] = d["name"]
    elif a == "bank":
        if d.get("dir") not in (1, -1):
            return None, "dir must be 1 or -1"
        out["dir"] = d["dir"]
    elif a == "shift":
        n = d.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not n:
            return None, "n must be a non-zero whole number"
        out["n"] = n
    elif a == "goto_bank":
        n = d.get("bank")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return None, "bank must be a whole number from 0"
        out["bank"] = n
    elif a == "view_mode":
        if d.get("surface") not in SURFACES + ("all",):
            return None, 'surface must be fire, bsp, cc or "all"'
        if d.get("mode", "toggle") not in MODES + ("toggle",):
            return None, "mode must be toggle, shared or independent"
        out["surface"], out["mode"] = d["surface"], d.get("mode", "toggle")
    return out, None


def default_bindings(fader_cc=None, bank_cc=None, bank_notes=None, view_cc=None):
    """The shipped table (today's behaviour), from orc1_config unless given."""
    fader_cc = getattr(cfg, "DAWSURF_FADER_CC", {}) if fader_cc is None else fader_cc
    bank_cc = getattr(cfg, "DAWSURF_BANK_CC", {}) if bank_cc is None else bank_cc
    bank_notes = getattr(cfg, "DAWSURF_BANK_NOTES", {}) if bank_notes is None else bank_notes
    view_cc = getattr(cfg, "DAWSURF_VIEW_CC", {}) if view_cc is None else view_cc
    out = []
    for num, slot in sorted(fader_cc.items()):
        out.append({"cc": num, "action": "fader", "slot": slot, "view": "cc", "feedback": True})
    for num, d in sorted(bank_cc.items()):
        out.append({"cc": num, "action": "bank", "dir": d, "view": "cc"})
    for num, bank in sorted(bank_notes.items()):
        out.append({"note": num, "action": "goto_bank", "bank": bank, "view": "bsp"})
    for num, surface in sorted(view_cc.items()):
        out.append({"cc": num, "action": "view_mode", "surface": surface, "mode": "toggle"})
    return out


class BindingTable:
    """(kind, channel, number) -> binding. Built from the shipped defaults + the user's file; the
    file is re-read when its mtime changes (refresh()). find() is what the bridge calls per message."""

    def __init__(self, defaults=None, path=None, log=None):
        self.defaults = default_bindings() if defaults is None else list(defaults)
        p = path if path is not None else getattr(cfg, "DAWSURF_BINDINGS_FILE", "~/.orc1_dawsurf_bindings.json")
        self.path = os.path.expanduser(p) if p else None
        self.log = log or (lambda text: print("dawsurf: " + text, flush=True))
        self._mt = None
        self.errors = []
        self.user = []
        self.replace = False
        self._index = {}
        self._build()
        self.refresh()

    def _build(self):
        idx = {}
        for src in (() if self.replace else self.defaults, self.user):
            for raw in src:
                b, why = check_binding(raw)
                if b is None:
                    continue
                idx[(b["kind"], b["ch"], b["num"])] = b
        self._index = {k: b for k, b in idx.items() if b["action"] != "none"}

    def refresh(self):
        """Re-read the user's file if it changed. True = the table changed."""
        if not self.path:
            return False
        try:
            mt = os.stat(self.path).st_mtime_ns
        except OSError:
            mt = None
        if mt == self._mt:
            return False
        self._mt = mt
        user, replace, errors = [], False, []
        if mt is not None:
            try:
                with open(self.path) as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not isinstance(data.get("bindings", []), list):
                    raise ValueError('expected {"bindings": [...]}')
                replace = bool(data.get("replace_defaults", False))
                for i, raw in enumerate(data.get("bindings", [])):
                    b, why = check_binding(raw)
                    if b is None:
                        errors.append("binding %d %r: %s" % (i, raw, why))
                    else:
                        user.append(raw)
            except (OSError, ValueError) as e:
                self.log("bindings file %s unreadable (%s): keeping the last table" % (self.path, e))
                return False
        self.user, self.replace, self.errors = user, replace, errors
        for e in errors:
            self.log("bindings: skipped " + e)
        self._build()
        self.log("bindings: %d shipped%s + %d from %s" % (
            0 if replace else len(self.defaults), " (replaced)" if replace else "", len(user), self.path))
        return True

    def find(self, kind, ch, num, daw_ch):
        """The binding for a message on channel ch (1-based), or None. A literal channel wins over
        "daw" when both would match."""
        b = self._index.get((kind, ch, num))
        if b is None and daw_ch is not None and ch == daw_ch:
            b = self._index.get((kind, DAW, num))
        return b

    def all(self):
        return sorted(self._index.values(), key=lambda b: (b["kind"], str(b["ch"]), b["num"]))

    def feedback(self):
        """Fader bindings whose DAW moves go back out as their CC."""
        return [b for b in self._index.values() if b.get("feedback") and b["kind"] == "cc"]
