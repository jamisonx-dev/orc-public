#!/usr/bin/env python3
"""orc1_kikpad -- the KIKPAD's bank 2 as an ORC surface: the sampler decks + skipback.

The pad (Midiplus SmartPad, custom firmware, jamisonx-dev/smartpad-kikpad) has a
HOST bank (bank 2) that decides nothing itself: it reports presses, releases and
encoder turns on its SECOND USB-MIDI port ("KIKPAD:KIKPAD IN <client>:1"), and the
host paints the bank back on that same port. The brain claims that port by NAME
(never as an engine input), so no channel or CC is reserved anywhere: the pad's
music banks keep using port :0 exactly as before.

Every action calls a NAMED brain function (sampler_capture_bars, sampler_cue,
skipback_save, ...), which resolves the sampler module's live /matrix binding
itself. Nothing here knows a sampler channel or CC.

Wire (MIDI channel 1 on port :1; the firmware's HOST SURFACE block is the spec):
    pad -> host   note 0-63 = pad press/release, note 64-87 = button (MS1 = 64),
                  CC 0-7 = encoder (65 = +1, 63 = -1), CC 100 = 1 shown / 0 left
    host -> pad   note 0-63 vel = pad colour (6-bit rgbRGB), note 64-87 = button LED,
                  CC 1 = pads black, CC 4 = button LEDs off

LAYOUT (row 0 = top). Deck A = rows 0-2, deck B = rows 3-5:
    row +0  GRAB last 1/2/4/8 bars | NEXT 1/2/4 bars | HOLD (grab while held)
    row +1  LOOP  STOP  REVERSE  PLAY-ONCE | CUE 1-4 (SET + cue = clear)
    row +2  STUTTER 1, 1/2, 1/4, 1/8 beat (held) | JUMP 0, 1/4, 1/2, 3/4
    row 6   skipback tracks (the first 8 ARMED tracks by id): press = save it
    row 7   length 30s 1m 2m 5m 10m ALL | (free) | GRAB ALL (every track, aligned)
Buttons: MS1 / MS2 = the encoders control deck A / deck B; SET = shift (held).
Encoders: speed, gain, filter, EQ low, EQ mid, EQ high (the selected deck),
crossfade, sampler master.

One page: the sampler + skipback above (LEFT lit). Until 2026-10-24 RIGHT showed a
VISUALS page; it was removed at owner's request: the KIKPAD only stood in as a test
controller for the visuals in one session, and the two are not meant to go together.
The visuals are driven by CC from any controller (daw/orc_visuals.py KNOBS).

Pure logic: the engine (the orc1 module) and the MIDI output are injected, so the
whole surface runs under test with fakes. mido is only touched in attach().
"""
import json
import os
import threading
import time

PORT_PREFIX = "kikpad"
CH = 0                                   # the surface wire is MIDI channel 1
SHOWN_CC = 100

# ---- colours: 6-bit rgbRGB (bit 0-2 = B G R at 2/3, bit 3-5 = b g r adding 1/3) --
def col(r, g, b):
    """Levels 0-3 per channel -> the pad's 6-bit colour."""
    v = 0
    for lvl, hi, lo in ((r, 4, 32), (g, 2, 16), (b, 1, 8)):
        if lvl & 2:
            v |= hi
        if lvl & 1:
            v |= lo
    return v


BLACK = 0
WHITE = col(3, 3, 3)
RED = col(3, 0, 0)
GREEN = col(0, 3, 0)
BLUE = col(0, 0, 3)
YELLOW = col(3, 3, 0)
ORANGE = col(3, 1, 0)
MAGENTA = col(3, 0, 3)
CYAN = col(0, 3, 3)
DIM_GREY = col(1, 1, 1)
DECK_COL = {"A": col(0, 2, 3), "B": col(3, 1, 0)}      # deck A blue-cyan, deck B orange
DECK_DIM = {"A": col(0, 1, 1), "B": col(1, 1, 0)}

# ---- layout ------------------------------------------------------------------
DECK_ROW = {"A": 0, "B": 3}
GRAB_BARS = (1, 2, 4, 8)
NEXT_BARS = (1, 2, 4)
STUTTER_BEATS = (1.0, 0.5, 0.25, 0.125)
JUMP_FRACS = (0.0, 0.25, 0.5, 0.75)
SB_TRACK_ROW = 6
SB_LEN_ROW = 7
SB_LENGTHS = (30, 60, 120, 300, 600, 0)     # 0 = ALL of the ring
SB_ALL_COL = 7

# buttons (the firmware's BT_* order)
BT_MS1, BT_MS2, BT_MS3, BT_MS4 = 0, 1, 2, 3
BT_LEFT, BT_RIGHT = 10, 11
BT_SET = 15
BTN_BASE = 64

# encoders: (name, per-deck?, step, start value)
ENCODERS = (("speed", True, 1, 64), ("gain", True, 2, 85), ("filter", True, 2, 64),
            ("eq_low", True, 2, 64), ("eq_mid", True, 2, 64), ("eq_hi", True, 2, 64),
            ("crossfade", False, 2, 64), ("master", False, 2, 85))
TONE_RESET = {"filter": 64, "eq_low": 64, "eq_mid": 64, "eq_hi": 64}   # what a grab resets deck-side

FLASH_S = 1.5
POLL_S = 0.25


def is_surface_port(name):
    """The pad's SECOND port: "KIKPAD:KIKPAD IN 32:1" (ALSA client:port at the end).
    By name and port number, so a moved client number still matches."""
    n = (name or "").strip()
    if not n.lower().startswith(PORT_PREFIX):
        return False
    tail = n.rsplit(" ", 1)[-1]
    return tail.count(":") == 1 and tail.split(":")[1] == "1"


def pad_action(idx):
    """What a pad index means: (kind, deck, value) or None. Pure, for the tests."""
    if not 0 <= idx < 64:
        return None
    row, c = divmod(idx, 8)
    for deck, base in DECK_ROW.items():
        r = row - base
        if r == 0:
            if c < 4:
                return ("grab", deck, GRAB_BARS[c])
            if c < 7:
                return ("next", deck, NEXT_BARS[c - 4])
            return ("hold", deck, None)
        if r == 1:
            return (("loop", "stop", "reverse", "play")[c], deck, None) if c < 4 \
                else ("cue", deck, c - 3)
        if r == 2:
            return ("stutter", deck, STUTTER_BEATS[c]) if c < 4 \
                else ("jump", deck, JUMP_FRACS[c - 4])
    if row == SB_TRACK_ROW:
        return ("sb_track", None, c)
    if row == SB_LEN_ROW:
        if c < len(SB_LENGTHS):
            return ("sb_len", None, SB_LENGTHS[c])
        if c == SB_ALL_COL:
            return ("sb_all", None, None)
    return None


class KikpadSurface:
    def __init__(self, engine, home=None, clock=time.monotonic, wall=time.time, log=None):
        self.e = engine
        self.home = home or os.path.expanduser("~")
        self.clock = clock
        self.wall = wall
        self._log = log or (lambda m: print(f"[kikpad] {m}", flush=True))
        self.lock = threading.RLock()
        self.out = None                    # anything with .send(mido-like msg)
        self.out_name = None
        self._msg = None                   # mido.Message factory (set in attach / tests)
        self.sysex_hook = None             # gets each SysEx from the pad (the map editor's replies)
        self.sent_pads = [None] * 64
        self.sent_btns = [None] * 24
        self.held = set()                  # pad indexes physically down
        self.shift = False
        self.enc_deck = "A"
        self.enc = {"A": {}, "B": {}, "": {}}
        for name, per_deck, _, start in ENCODERS:
            for d in (("A", "B") if per_deck else ("",)):
                self.enc[d][name] = start
        self.sb_len = 60
        self.sb_busy = {}                  # track id -> True while saving
        self.sb_all_busy = False
        self.flash = {}                    # pad idx -> (colour, until)
        self.decks = {"A": {"loaded": False, "cues": [None] * 4},
                      "B": {"loaded": False, "cues": [None] * 4}}
        self.tracks = []                   # [{"id","store","live"}] first 8 armed by id
        self._view_mtime = {}
        self._poll = None
        self._stop = threading.Event()

    # ---- output ------------------------------------------------------------
    def attach(self, out_name, opener=None, msg_factory=None):
        """(Re)open the pad's port :1 for painting. A replugged pad reuses the
        name with a NEW ALSA port, and a stale handle drops silently, so this
        always closes the old one and opens fresh."""
        if opener is None or msg_factory is None:
            import mido
            opener = opener or mido.open_output
            msg_factory = msg_factory or mido.Message
        with self.lock:
            self._close_out()
            try:
                self.out = opener(out_name)
                self.out_name = out_name
                self._msg = msg_factory
            except Exception as e:
                self.out = None
                self._log(f"cannot open {out_name}: {e}")
                return False
            self.repaint()
        self._start_poll()
        self._log(f"surface on {out_name}")
        return True

    def detach(self, name=None):
        with self.lock:
            if name is None or name == self.out_name:
                self._release_all()
                self._close_out()

    def _close_out(self):
        if self.out is not None:
            try:
                self.out.close()
            except Exception:
                pass
        self.out = None
        self.out_name = None

    def _send(self, kind, **kw):
        if self.out is None or self._msg is None:
            return
        try:
            self.out.send(self._msg(kind, channel=CH, **kw))
        except Exception as e:
            self._log(f"paint failed: {e}")

    def send_sysex(self, data):
        """One SysEx to the pad on port :1 (data without F0/F7). Raises if the pad is not attached,
        so the map editor reports it instead of waiting for a reply that cannot come."""
        with self.lock:
            if self.out is None or self._msg is None:
                raise RuntimeError("the KIKPAD's port :1 is not open")
            self.out.send(self._msg("sysex", data=list(data)))

    def stop(self):
        self._stop.set()

    # ---- input -------------------------------------------------------------
    def handle(self, msg):
        """One message from port :1. Never raises (a bad press must not kill
        the reader thread)."""
        if msg.type == "sysex":                  # the map editor's replies (orc1_kikpad_map), never the surface's
            hook = self.sysex_hook
            if hook is not None:
                try:
                    hook(list(msg.data))
                except Exception as e:
                    self._log(f"sysex hook: {e}")
            return
        try:
            with self.lock:
                self._handle(msg)
                self.render()
        except Exception as e:
            self._log(f"handle {msg}: {e}")

    def _handle(self, msg):
        t = msg.type
        if getattr(msg, "channel", CH) != CH:
            return
        if t == "control_change":
            if msg.control == SHOWN_CC:
                if msg.value:
                    self.repaint()
                else:
                    self._release_all()          # left bank 2 mid-press: no stuck stutter/hold
                return
            if 0 <= msg.control < len(ENCODERS):
                d = 1 if msg.value >= 64 else -1
                if msg.value == 64:
                    return
                self._encoder(msg.control, d)
            return
        if t not in ("note_on", "note_off"):
            return
        down = t == "note_on" and msg.velocity > 0
        n = msg.note
        if n < 64:
            if down:
                if n in self.held:
                    return
                self.held.add(n)
                self._pad(n, True)
            elif n in self.held:
                self.held.discard(n)
                self._pad(n, False)
        elif n < BTN_BASE + 24:
            self._button(n - BTN_BASE, down)

    def _release_all(self):
        for n in sorted(self.held):
            self.held.discard(n)
            self._pad(n, False)
        self.shift = False

    def _call(self, fn, *a):
        f = getattr(self.e, fn, None)
        if f is None:
            self._log(f"engine has no {fn}")
            return None
        try:
            return f(*a)
        except Exception as e:
            self._log(f"{fn}{a}: {e}")
            return None

    def _pad(self, idx, down):
        act = pad_action(idx)
        if act is None:
            return
        kind, deck, v = act
        if kind == "hold":
            self._call("sampler_capture_fwd_hold_start" if down
                       else "sampler_capture_fwd_hold_end", deck)
            if not down:
                self._tone_reset(deck)
            return
        if kind == "stutter":
            if down:
                self._call("sampler_beat_repeat_on", deck, v)
            else:
                self._call("sampler_beat_repeat_off", deck)
            return
        if not down:
            return
        if kind == "grab":
            self._call("sampler_capture_bars", deck, v)
            self._tone_reset(deck)
        elif kind == "next":
            self._call("sampler_capture_fwd_fixed", deck, v)
            self._tone_reset(deck)
        elif kind == "loop":
            self._call("sampler_loop_toggle", deck)
        elif kind == "stop":
            self._call("sampler_stop", deck)
        elif kind == "reverse":
            self._call("sampler_reverse", deck)
        elif kind == "play":
            self._call("sampler_audition", deck)
        elif kind == "cue":
            self._call("sampler_cue_clear" if self.shift else "sampler_cue", deck, v)
        elif kind == "jump":
            self._call("sampler_seek", deck, int(round(v * 127)))
        elif kind == "sb_len":
            self.sb_len = v
        elif kind == "sb_track":
            self._skipback_track(v, idx)
        elif kind == "sb_all":
            self._skipback_all(idx)

    def _tone_reset(self, deck):
        # the sampler flattens EQ + filter on every fresh grab: follow it
        self.enc[deck].update(TONE_RESET)

    def _button(self, b, down):
        if b == BT_SET:
            self.shift = down
        elif not down:
            return
        elif b in (BT_MS1, BT_MS2):
            self.enc_deck = "A" if b == BT_MS1 else "B"

    def _encoder(self, i, d):
        name, per_deck, step, _ = ENCODERS[i]
        deck = self.enc_deck if per_deck else ""
        v = max(0, min(127, self.enc[deck][name] + d * step))
        if v == self.enc[deck][name]:
            return
        self.enc[deck][name] = v
        if name == "speed":
            self._call("sampler_speed", deck, v)
        elif name == "gain":
            self._call("sampler_gain", deck, v)
        elif name == "filter":
            self._call("sampler_filter", deck, v)
        elif name.startswith("eq_"):
            self._call("sampler_eq", deck, name[3:], v)
        elif name == "crossfade":
            self._call("sampler_crossfade", v)
        elif name == "master":
            self._call("sampler_master_gain", v)

    # ---- skipback ----------------------------------------------------------
    def _skipback_track(self, slot, idx):
        if slot >= len(self.tracks):
            return
        tr = self.tracks[slot]
        tid = tr["id"]
        if self.sb_busy.get(tid):
            return                              # one save per track at a time
        secs = self.sb_len or int(tr.get("length_secs") or 86400)
        self.sb_busy[tid] = True

        def done(ok, _name, tid=tid, idx=idx):
            with self.lock:
                self.sb_busy.pop(tid, None)
                self.flash[idx] = (GREEN if ok else RED, self.clock() + FLASH_S)
                self.render()
        r = self._call("skipback_save", tid, secs, done)
        if r is False:                          # (skipback_save returns None today)
            self.sb_busy.pop(tid, None)

    def _skipback_all(self, idx):
        if self.sb_all_busy:
            return

        def done(ok, _name, idx=idx):
            with self.lock:
                self.sb_all_busy = False
                self.flash[idx] = (GREEN if ok else RED, self.clock() + FLASH_S)
                self.render()
        self.sb_all_busy = True
        if not self._call("skipback_save_all", self.sb_len, None, done):
            self.sb_all_busy = False            # refused (one already writing)
            self.flash[idx] = (RED, self.clock() + FLASH_S)

    # ---- state from disk (polled) -----------------------------------------
    def refresh(self):
        """Re-read what the pad shows: the sampler decks' view files (cues,
        loaded) and the skipback tracks + heads. Cheap; mtime-gated."""
        for deck in ("A", "B"):
            p = os.path.join(self.home, f".orc1_sampler_view_{deck}.json")
            try:
                m = os.path.getmtime(p)
            except OSError:
                self.decks[deck] = {"loaded": False, "cues": [None] * 4}
                continue
            if self._view_mtime.get(deck) == m:
                continue
            try:
                with open(p) as f:
                    v = json.load(f)
                cues = list(v.get("cues") or [None] * 4)[:4]
                self.decks[deck] = {"loaded": float(v.get("length_s") or 0) > 0,
                                    "cues": cues + [None] * (4 - len(cues))}
                self._view_mtime[deck] = m
            except Exception:
                pass                            # mid-write: next poll
        tracks = []
        try:
            with open(os.path.join(self.home, ".orc1_skipback_tracks.json")) as f:
                for t in json.load(f).get("tracks", []):
                    if isinstance(t, dict) and "id" in t and t.get("armed", True):
                        tracks.append({"id": int(t["id"]),
                                       "store": "card" if t.get("store") == "card" else "ram",
                                       "length_secs": t.get("length_secs")})
        except Exception:
            pass
        tracks.sort(key=lambda t: t["id"])
        heads = {}
        try:
            with open(os.path.join(self.home, ".skipbuf", "heads.json")) as f:
                heads = json.load(f).get("tracks", {})
        except Exception:
            pass
        now = self.wall()
        for t in tracks:
            h = heads.get(str(t["id"]))
            # a CARD head can trail by seconds when the SD card stalls (measured
            # up to ~10 s), so it gets more slack before it reads as stopped
            slack = 20.0 if t["store"] == "card" else 5.0
            t["live"] = bool(h) and now - float(h.get("t", 0)) < slack
        self.tracks = tracks[:8]

    def _start_poll(self):
        if self._poll is not None and self._poll.is_alive():
            return

        def loop():
            while not self._stop.is_set():
                try:
                    with self.lock:
                        if self.out is not None:
                            self.refresh()
                            self.render()
                except Exception as e:
                    self._log(f"poll: {e}")
                self._stop.wait(POLL_S)
        self._poll = threading.Thread(target=loop, daemon=True, name="kikpad-surface")
        self._poll.start()

    # ---- painting ----------------------------------------------------------
    def frame(self):
        """The 64 pad colours + 24 button LEDs the pad should show now."""
        now = self.clock()
        blink = int(now * 4) % 2 == 0
        pads = [BLACK] * 64
        btns = [False] * 24
        btns[BT_LEFT] = True                    # the one page (the sampler) is showing
        btns[BT_SET] = self.shift
        btns[BT_MS1] = self.enc_deck == "A"
        btns[BT_MS2] = self.enc_deck == "B"
        self._sampler_frame(pads, blink, now)
        return pads, btns

    def _sampler_frame(self, pads, blink, now):
        for idx in range(64):
            act = pad_action(idx)
            if act is None:
                continue
            kind, deck, v = act
            c = BLACK
            if deck:
                loaded = self.decks[deck]["loaded"]
                if kind in ("grab", "next"):
                    c = DECK_DIM[deck]
                elif kind == "hold":
                    c = RED
                elif kind == "loop":
                    c = GREEN if loaded else DIM_GREY
                elif kind == "stop":
                    c = RED if loaded else DIM_GREY
                elif kind == "reverse":
                    c = MAGENTA if loaded else DIM_GREY
                elif kind == "play":
                    c = CYAN if loaded else DIM_GREY
                elif kind == "cue":
                    c = YELLOW if self.decks[deck]["cues"][v - 1] is not None else DIM_GREY
                elif kind == "stutter":
                    c = col(2, 0, 2)
                elif kind == "jump":
                    c = DECK_COL[deck] if loaded else DIM_GREY
            elif kind == "sb_track":
                if v < len(self.tracks):
                    tr = self.tracks[v]
                    if self.sb_busy.get(tr["id"]):
                        c = WHITE if blink else BLACK
                    elif not tr["live"]:
                        c = RED
                    else:
                        c = GREEN if tr["store"] == "ram" else BLUE
            elif kind == "sb_len":
                c = WHITE if v == self.sb_len else DIM_GREY
            elif kind == "sb_all":
                c = (WHITE if blink else BLACK) if self.sb_all_busy else ORANGE
            if idx in self.held:
                c = WHITE
            f = self.flash.get(idx)
            if f:
                if f[1] > now:
                    c = f[0]
                else:
                    self.flash.pop(idx, None)
            pads[idx] = c

    def render(self):
        """Send only what changed since the last paint."""
        if self.out is None:
            return
        pads, btns = self.frame()
        for i, c in enumerate(pads):
            if self.sent_pads[i] != c:
                if c:
                    self._send("note_on", note=i, velocity=c)
                else:
                    self._send("note_off", note=i, velocity=0)
                self.sent_pads[i] = c
        for b, on in enumerate(btns):
            if self.sent_btns[b] != on:
                self._send("note_on", note=BTN_BASE + b, velocity=127 if on else 0)
                self.sent_btns[b] = on

    def repaint(self):
        """Forget what the pad shows and paint everything (a reboot, a re-entry)."""
        self.sent_pads = [None] * 64
        self.sent_btns = [None] * 24
        try:
            self.refresh()
        except Exception as e:
            self._log(f"refresh: {e}")
        self.render()
