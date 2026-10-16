"""
orc1_rc505.py -- Boss RC-505 mk1 OUTPUT handler for the ORC brain.

The RC-505 is a CONTROL TARGET: the ORC sends it transport / program-change /
CC, driven by the per-memory ASSIGN map authored with rc505_tools. This module
contains the RC's quirks so they don't leak into the brain:

  * It is driven over a DEDICATED USB-MIDI out (port key "rc505" = "RC-505 MIDI 1"),
    NEVER the DIN (the RC is on USB+DIN at once -> a DIN send loops back, the
    session-19 feedback shape).
  * It is NEVER sent MIDI CLOCK. The RC's Sync=AUTO(USB) means an incoming USB
    clock makes it SLAVE and time-stretch-artifact a live loop (owner-confirmed).
    So there is deliberately no clock method here; transport Start/Stop is fine.
  * Control CCs match the authored assign map (rc505_config.json). Momentary
    trigger targets are pressed (127 then 0); continuous targets take a 0-127 value.
  * MIDI notes do nothing on the RC, so this handler never sends them.
  * The RC treats each assign CC like a physical pedal: it acts on a 127 only
    after it has seen that CC's 0 ("release"). A source that sends 127 on every
    press and no release (the FCB's EF mode) works ONCE, then never again, so
    every press from here ends in its own release (press_down/press_up/tap/hold_for).

Decoupled: the brain injects a `send(mido.Message)` callable, so this module has
no import cycle and is unit-testable with a fake send.
"""
import json, os, re, threading, time

# Target names that take a continuous 0-127 value (vs a momentary press).
_CONTINUOUS = ("LEVEL", "PAN", "COMP", "REVERB", "CONTROL", "RHYTHM PATTERN")

# The practical RC-505 assign TARGETS (the named 0-79 set; the CC-out mirror
# targets 80+ are not useful for control). Used by the web page to list every
# action and show which are unmapped. Order = the unit's target index order.
TARGET_NAMES = [
    "TR1 REC/PLAY", "TR2 REC/PLAY", "TR3 REC/PLAY", "TR4 REC/PLAY", "TR5 REC/PLAY",
    "TR1 PLAY/STOP", "TR2 PLAY/STOP", "TR3 PLAY/STOP", "TR4 PLAY/STOP", "TR5 PLAY/STOP",
    "TR1 CLEAR", "TR2 CLEAR", "TR3 CLEAR", "TR4 CLEAR", "TR5 CLEAR",
    "TR1 UNDO/REDO", "TR2 UNDO/REDO", "TR3 UNDO/REDO", "TR4 UNDO/REDO", "TR5 UNDO/REDO",
    "TR1 PLAY LEVEL", "TR2 PLAY LEVEL", "TR3 PLAY LEVEL", "TR4 PLAY LEVEL", "TR5 PLAY LEVEL",
    "TR1 PAN", "TR2 PAN", "TR3 PAN", "TR4 PAN", "TR5 PAN",
    "TGT TR REC/PLAY", "TGT TR STOP", "TGT TR CLEAR", "TGT TR UNDO/REDO", "TGT TR PLAY LEV",
    "ALL START/STOP", "ALL CLEAR", "UNDO/REDO", "RHYTHM LEVEL", "RHYTHM PATTERN", "MEMORY LEVEL",
    "TEMPO (TAP)", "MASTER COMP", "MASTER REVERB", "OVERDUB MODE", "INPUT FX ON/OFF", "TRACK FX ON/OFF",
    "INPUT FX A CONTROL", "INPUT FX B CONTROL", "INPUT FX C CONTROL",
    "TRACK FX A CONTROL", "TRACK FX B CONTROL", "TRACK FX C CONTROL",
    "INPUT FX A TYP INC", "INPUT FX A TYP DEC", "INPUT FX B TYP INC", "INPUT FX B TYP DEC",
    "INPUT FX C TYP INC", "INPUT FX C TYP DEC", "TRACK FX A TYP INC", "TRACK FX A TYP DEC",
    "TRACK FX B TYP INC", "TRACK FX B TYP DEC", "TRACK FX C TYP INC", "TRACK FX C TYP DEC",
    "INPUT FX CONTROL", "TRACK FX CONTROL", "INPUT FX TYP INC", "INPUT FX TYP DEC",
    "TRACK FX TYP INC", "TRACK FX TYP DEC", "INPUT FX INC", "INPUT FX DEC", "TRACK FX INC", "TRACK FX DEC",
    "TARGET TRACK INC", "TARGET TRACK DEC", "MEMORY INC", "MEMORY DEC", "INPUT LEVEL",
]

def all_targets():
    """Every practical target as {name, target, continuous} (for the web UI)."""
    return [{"name": action_name(t), "target": t, "continuous": is_continuous(t)}
            for t in TARGET_NAMES]

# Fallback map (matches the shipped rc505_config.json) if no config file is found.
# Laid out for foot control (2026-10-05): the selected-track controls first, then
# direct per-track ones. Memory select needs no slot (Program Change is hardcoded).
_DEFAULT_ASSIGNS = [
    (1, "TGT TR REC/PLAY"), (2, "TGT TR STOP"),
    (3, "TARGET TRACK INC"), (4, "TARGET TRACK DEC"),
    (5, "ALL START/STOP"), (6, "UNDO/REDO"),
    (7, "TR1 PLAY/STOP"), (8, "TR2 PLAY/STOP"), (9, "TR3 PLAY/STOP"),
    (10, "TR4 PLAY/STOP"), (11, "TR5 PLAY/STOP"),
    (12, "TR1 REC/PLAY"), (13, "TR2 REC/PLAY"), (14, "TR3 REC/PLAY"),
    (15, "TR4 REC/PLAY"), (16, "TR5 REC/PLAY"),
]

def action_name(target):
    """"TR1 REC/PLAY" -> "tr1_rec_play"; "MASTER COMP" -> "master_comp"."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", target.lower())).strip("_")

def is_continuous(target):
    t = target.upper()
    return any(k in t for k in _CONTINUOUS)


class RC505Handler:
    def __init__(self, send, config_path=None, channel=1):
        """send: callable(mido.Message). channel: RC MIDI Rx channel (1-16; RC
        Omni defaults ON so any channel is accepted)."""
        self._send = send
        self._mido = None            # resolved lazily so tests need no mido
        self.channel = max(1, min(16, int(channel)))
        self.current_memory = 0      # tracked from our own PC sends (best-effort)
        self.cc_map = {}             # action_name -> {"cc", "continuous", "target"}
        self.by_cc = {}              # cc -> action_name
        self._held = {}              # cc -> {"token", "t", "timer", "locked"}
        self._hlock = threading.Lock()
        self._token = 0
        self.load_map(config_path)

    # ---- map loading ----------------------------------------------------------
    def load_map(self, config_path=None):
        assigns = None
        if config_path:
            p = os.path.expanduser(config_path)
            try:
                cfg = json.load(open(p))
                assigns = [(a["src"]["cc"], a["tgt"])
                           for a in cfg.get("assigns", {}).get("assigns", [])
                           if isinstance(a.get("src"), dict) and "cc" in a["src"]]
            except Exception:
                assigns = None
        if not assigns:
            assigns = list(_DEFAULT_ASSIGNS)
        self.cc_map, self.by_cc = {}, {}
        for cc, target in assigns:
            name = action_name(target)
            self.cc_map[name] = {"cc": int(cc), "continuous": is_continuous(target),
                                 "target": target}
            self.by_cc[int(cc)] = name
        return self.cc_map

    # ---- low-level sends ------------------------------------------------------
    def _msg(self, **kw):
        if self._mido is None:
            import mido
            self._mido = mido
        return self._mido.Message(**kw)

    def send_cc(self, cc, value):
        if not (0 <= int(cc) <= 127 and 0 <= int(value) <= 127):
            return False
        self._send(self._msg(type="control_change", channel=self.channel-1,
                             control=int(cc), value=int(value)))
        return True

    # RC quirk: a MOMENT-mode assign needs a real press DURATION -- 127 followed
    # immediately by 0 (same MIDI burst) is missed by the RC's control scan, so a
    # trigger (MEMORY INC, REC/PLAY, ...) does nothing. A small gap fixes it.
    PRESS_GAP_S = 0.04

    def press_cc(self, cc):
        """Momentary press: max, hold PRESS_GAP_S, then min (one RC assign trigger)."""
        ok1 = self.send_cc(cc, 127)
        time.sleep(self.PRESS_GAP_S)
        ok2 = self.send_cc(cc, 0)
        return ok1 and ok2

    # ---- held presses (foot switches). Non-blocking: callers are MIDI input
    # threads, so every release is a timer, never a sleep. ----------------------
    HOLD_S = 4.0         # hold_for() default: long enough for the RC's hold-STOP = clear
    HOLD_MAX_S = 30.0    # a release lost on the wire can't hold an RC button forever

    def _timer(self, secs, fn):
        t = threading.Timer(secs, fn)
        t.daemon = True
        t.start()
        return t

    def _press(self, name, locked, release_after):
        """127 now; 0 after `release_after` s. Refused while that CC is already
        down (a second 127 would be ignored by the RC anyway, and it must not cut
        short a running timed hold)."""
        e = self.cc_map.get(name)
        if not e:
            return False
        cc = e["cc"]
        with self._hlock:
            if cc in self._held:
                return False
            self._token += 1
            tok = self._token
            self._held[cc] = {"token": tok, "t": time.monotonic(), "locked": locked,
                              "timer": self._timer(release_after,
                                                   lambda: self._release(cc, tok))}
            self.send_cc(cc, 127)
        return True

    def _release(self, cc, token):
        with self._hlock:
            h = self._held.get(cc)
            if not h or h["token"] != token:       # a stale timer from an older press
                return
            del self._held[cc]
            h["timer"].cancel()
            self.send_cc(cc, 0)

    def press_down(self, name):
        """Foot DOWN: the RC sees the button held until press_up (so its own hold
        gestures work, e.g. hold STOP = clear). Continuous targets get 127."""
        e = self.cc_map.get(name)
        if e and e["continuous"]:
            return self.send_cc(e["cc"], 127)
        return self._press(name, False, self.HOLD_MAX_S)

    def press_up(self, name):
        """Foot UP: release, but never sooner than PRESS_GAP_S after the press (a
        0-length press is missed). Ignored during a hold_for()."""
        e = self.cc_map.get(name)
        if not e or e["continuous"]:
            return False
        cc = e["cc"]
        with self._hlock:
            h = self._held.get(cc)
            if not h or h["locked"]:
                return False
            h["timer"].cancel()
            left = self.PRESS_GAP_S - (time.monotonic() - h["t"])
            tok = h["token"]
            if left > 0:
                h["timer"] = self._timer(left, lambda: self._release(cc, tok))
                return True
        self._release(cc, tok)
        return True

    def tap(self, name):
        """A complete press for a press-only source: 127, PRESS_GAP_S, 0."""
        e = self.cc_map.get(name)
        if e and e["continuous"]:
            return self.send_cc(e["cc"], 127)
        return self._press(name, True, self.PRESS_GAP_S)

    def hold_for(self, name, secs=None):
        """The ORC holds the button `secs` (default HOLD_S) by itself -- a hold
        gesture from a press-only source. The foot's release doesn't cut it short."""
        e = self.cc_map.get(name)
        if not e or e["continuous"]:
            return False
        return self._press(name, True, self.HOLD_S if secs is None else float(secs))

    def held(self):
        """CCs currently held down (for tests / status)."""
        with self._hlock:
            return sorted(self._held)

    def select_memory(self, program):
        if not (0 <= int(program) <= 98):        # RC-505 has 99 memories (0-98)
            return False
        self.current_memory = int(program)
        self._send(self._msg(type="program_change", channel=self.channel-1,
                             program=int(program)))
        return True

    def bank(self, delta):
        """Relative memory move (PC-banking) -- switches the whole RC control
        surface, since each memory has its own 16 assigns. Hardcoded PC path,
        costs no assign slot."""
        return self.select_memory(max(0, min(98, self.current_memory + int(delta))))

    def transport(self, start):
        self._send(self._msg(type="start" if start else "stop"))
        return True

    # ---- named-action API (what the web and the pedal call) -----------------
    def do(self, name, value=None):
        """Fire a mapped action by name. Continuous targets use `value` (0-127,
        default 127); trigger targets press (127->0). Returns False if unmapped."""
        e = self.cc_map.get(name)
        if not e:
            return False
        if e["continuous"]:
            return self.send_cc(e["cc"], 127 if value is None else int(value))
        if value is not None and int(value) == 0:
            return self.send_cc(e["cc"], 0)          # explicit release
        return self.press_cc(e["cc"])

    def actions(self):
        """List available action names + their cc/target/kind (for the web UI)."""
        return {n: dict(e) for n, e in self.cc_map.items()}
