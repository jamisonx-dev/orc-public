"""orc1_dawsurf_bridge -- the "daw" module: Fire faders <-> a DAW as a Mackie Control surface.

    python3 orc1_dawsurf_bridge.py        (run by systemd as orc1-dawsurf; Ctrl-C / SIGTERM stops)

Fire -> DAW: a CC on the channel the "daw" module is bound to (/matrix) reaches the ORC's port
ORC-daw through the brain (2026-11-09: never the synth bus). This bridge reads that port, keeps that channel,
and turns the mapped CCs (orc1_config.DAWSURF_FADER_CC / DAWSURF_BANK_CC) into gestures on the
neutral surface model (orc1_dawsurface.DawSurface + the Mackie dialect orc1_dawsurface_mcu),
which go out as MCU messages on a DEDICATED network-MIDI port (rtpmidid, a PC rtpMIDI session
named "ORC DAW", cfg.DAWSURF_PORT_NAMES). REAPER sees one Mackie Control unit (8 strips + master).

DAW -> Fire: REAPER's MCU feedback on that port goes into the model; every fader change it
reports goes to the Fire as the same CC on the same channel, so its FADERS mode follows.

TWO DIALECTS (cfg.DAWSURF_DIALECT, 2026-11-09). "mcu" = all of the above: REAPER's one 8-strip Mackie
window, every surface follows it. "osc" = REAPER's OSC surface with orc1/daw/ORC.ReaperOSC over UDP
(OscLink, cfg.DAWSURF_OSC_*; dialect orc1_dawsurface_osc): the model holds EVERY track by number
(512 a page, paged past that), so each surface has its own VIEW (a window of tracks), shared or
independent per surface (cfg.DAWSURF_VIEW_MODES, toggled at run time by cfg.DAWSURF_VIEW_CC). The
BSP's Mackie port is an INPUT surface either way: its messages are translated here, never passed to
the DAW, so it works the same with REAPER on OSC.

EVERYTHING IS MAPPABLE (orc1_dawsurf_bind). Every bus message is looked up in one BINDING table
(shipped defaults = the CC / note maps below + the view CCs, plus ~/.orc1_dawsurf_bindings.json) and
runs an ACTION (fader / pan / mute / solo / rec / select / transport / bank / shift / goto_bank /
view_mode) on a VIEW (fire / bsp / cc); the Fire's page and the BSP port call the same actions.

NO ECHO LOOPS. One table, `fire_val[(ch, cc)]` = the 7-bit value the Fire is known to show, is
consulted in BOTH directions: a Fire CC equal to it is not news (an echo of what we just sent
the Fire, or a repeat) and goes nowhere; a DAW fader whose 7-bit value equals it is not sent to
the Fire (an echo of the Fire's own move, or a 14-bit wobble inside one CC step). A value only
travels when it changes what the other side shows, so nothing can bounce for ever. On top of
that, while a Fire fader moves the bridge holds the MCU fader TOUCH (released
DAWSURF_TOUCH_RELEASE_S after the last move): REAPER treats it as a touched fader (touch
automation), and the model ignores the DAW's fader reports for that strip meanwhile, so a late
DAW echo can never yank the fader under the user's finger. A bank press lets every touch go
first, so the new bank's faders always reach the Fire.

THE FIRE'S DAW PAGE (fw v0.31d+, hello version >= PAGE_VERSION). Our firmware's host SysEx
(F0 7D 'O' 'R' 'C' <cmd> ... F7, the frame orc1_scenes.FireBarLink uses). The bridge asks the
Fire's version itself (hello 00, answered 00 <version>) on every Fire (re)appearance, retrying
like FireBarLink, and reads the answer on its own subscription to the Fire's output (the brain
reads the same output; its FireBarLink swallows every ORC frame from the seq Fire, so a 1D / 1E /
hello answer never reaches its router or the DIN). With the page:
  Pi -> Fire  1A n                  n rows (9 = 8 strips of the bank + master in row 8; 0 = none:
                                    the DAW port is gone). Every row is re-sent with 1B after it.
              1B row R G B lvl name a row in full (RGB 0-127, level 0-127, 0-8 printable ASCII)
              1C row lvl            level only
  Fire -> Pi  1D row lvl            the user moved that row -> MCU touch + fader (as a CC move)
              1E dir                scrolled past the top (00) / bottom (01) -> bank left / right
`page_shown` = what the Fire is known to show per row (the page's fire_val): a row goes out only
when it differs, as 1C when only the level changed. Rows are sent from tick() (every TICK_S), at
most one message per row per PAGE_MIN_GAP_S, latest value wins: REAPER's bank re-send (names +
faders of 8 strips) and a moving REAPER fader (tens of bends/s) collapse to one row each per gap.
A 1D updates page_shown BEFORE the move goes to the model, so the model's change never comes back
as a 1C; a 1D equal to what the Fire already shows is ignored.
Names = the MCU scribble strip (6 chars). Colours: MCU has none -> the strip's model colour (a
dialect that knows it), else REAPER's web remote (cfg.DAWSURF_REAPER_WEB, polled in a thread,
tracks matched to strips by NAME with a bank counter as the tie-break / fallback), else a fixed
palette by strip. The CC path stays: CCs IN from any controller always work; CCs OUT to the
Fire are skipped while the page is up unless cfg.DAWSURF_CC_WITH_PAGE.

FOLLOWS, all checked every CHECK_S:
  the bus      across brain restarts (the visuals / conductor rule: ONE rtmidi client, check
               /proc/asound/seq/clients that it is still Connected From ORC-daw, and set
               the callback again after EVERY open_port: close_port() drops it);
  the binding  ~/.orc1_bindings.json re-read on change; unbound = the bridge closes the DAW and
               Fire ports and idles; a move to another channel re-sends the faders on the new one;
  the Fire     cfg.DAWSURF_FIRE_PORT_NAMES (our firmware's "ORC FW seq") or is_fire_port();
               a replug (same name, new client:port) repaints its faders from the model;
  the DAW port appears / disappears quietly (logged once each way); every (re)appearance sends
               the MCU hello, which makes REAPER re-send its whole state.

The bridge's own ALSA clients are named cfg.DAWSURF_CLIENT* so the brain skips them (their out
ports are readable: the brain's hot-plug would open them as controllers otherwise) and so
cfg.is_dawsurf_port() never matches them. Status for the web: /dev/shm/orc1_dawsurf_status.json.
Import-safe: nothing opens a port or a file until main().
"""
import json
import os
import re
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import orc1_config as cfg                                  # noqa: E402
from orc1_dawsurface import DawSurface, MASTER             # noqa: E402
from orc1_dawsurface_mcu import McuAdapter                 # noqa: E402
from orc1_dawsurface_osc import OscAdapter                 # noqa: E402
import orc1_dawsurface_osc as OSC                          # noqa: E402
import orc1_dawsurf_bind as BIND                           # noqa: E402
from orc1_dawsurf_bind import SURFACES                     # noqa: E402

MODULE = "daw"
STATUS = "/dev/shm/orc1_dawsurf_status.json"
CHECK_S = 1.0            # ports / binding / bus check + status write
TICK_S = 0.02            # touch release + page flush resolution
# 2026-11-09: the DAW bridge is not a synth, so its module messages come from the ORC on its OWN port "ORC-daw",
# never the synth bus (owner: "don't ever move anything that doesn't need an internal synthesis onto the synth").
# Matched by EXACT port name: "ORC-daw" must never be confused with this bridge's own "orc1-dawsurf-..." clients.
ORC_PORT = cfg.orc_port_name(MODULE)

# ---- the Fire's DAW page (host SysEx, see the docstring) ----
ORC_HDR = [0x7D, 0x4F, 0x52, 0x43]       # F0 7D 'O' 'R' 'C' <cmd> ... F7
CMD_HELLO = 0x00
CMD_PAGE_ROWS, CMD_PAGE_ROW, CMD_PAGE_LEVEL = 0x1A, 0x1B, 0x1C
MSG_PAGE_MOVE, MSG_PAGE_SCROLL = 0x1D, 0x1E
PAGE_VERSION = 6                         # hello version of the DAW page this speaks (fw v0.31f: 14-bit levels,
                                         # 1E <dir> <count>; v0.31d/e = 5 = the old 7-bit banks-of-8 page)
PAGE_WINDOW = 4                          # the Fire shows 4 rows: the tracks from REAPER's current one, then MASTER
MASTER_ROW = 8                           # (kept for the CC path's names; the page places MASTER itself)
PAGE_NAME_MAX = 8
PAGE_MIN_GAP_S = 0.02                    # at most one message per row per this
HELLO_RETRY_S, HELLO_SLOW_S, HELLO_FAST_TRIES = 3.0, 30.0, 5    # as orc1_scenes.FireBarLink
# fixed row colours by strip (RGB 0-127): red, orange, yellow, green, cyan, blue, violet, pink
PAGE_PALETTE = [(127, 20, 20), (127, 60, 0), (115, 105, 0), (25, 115, 25),
                (0, 105, 105), (20, 50, 127), (75, 30, 127), (127, 30, 85)]
PAGE_MASTER_RGB = (100, 100, 100)
PAGE_MASTER_NAME = "MASTER"
PAGE_EMPTY_NAME = "--"                    # a strip with no track behind it
MCU_NAME_CHARS = 6                       # REAPER's csurf_mcu writes 6 chars of a track name


# ------------------------------------------------------------------ raw MIDI <-> orc1_mcu dicts
def to_bytes(d):
    """One orc1_mcu message dict -> raw MIDI bytes (list), or None for a type we never send."""
    t, ch = d.get("type"), int(d.get("channel", 0)) & 0x0F
    if t == "pitchwheel":
        v = max(0, min(16383, int(d["pitch"]) + 8192))
        return [0xE0 | ch, v & 0x7F, v >> 7]
    if t == "note_on":
        return [0x90 | ch, int(d["note"]) & 0x7F, int(d.get("velocity", 0)) & 0x7F]
    if t == "note_off":
        return [0x80 | ch, int(d["note"]) & 0x7F, int(d.get("velocity", 0)) & 0x7F]
    if t == "control_change":
        return [0xB0 | ch, int(d["control"]) & 0x7F, int(d["value"]) & 0x7F]
    if t == "aftertouch":
        return [0xD0 | ch, int(d["value"]) & 0x7F]
    if t == "sysex":
        return [0xF0] + [int(b) & 0x7F for b in d["data"]] + [0xF7]
    return None


def from_bytes(b):
    """Raw MIDI bytes -> the dict shape orc1_mcu.decode_host reads, or None."""
    if not b:
        return None
    s = b[0]
    if s == 0xF0:
        data = b[1:-1] if b[-1] == 0xF7 else b[1:]
        return {"type": "sysex", "data": tuple(data)}
    kind, ch = s & 0xF0, s & 0x0F
    if kind == 0xE0 and len(b) >= 3:
        return {"type": "pitchwheel", "channel": ch, "pitch": ((b[2] << 7) | b[1]) - 8192}
    if kind == 0x90 and len(b) >= 3:
        return {"type": "note_on", "channel": ch, "note": b[1], "velocity": b[2]}
    if kind == 0x80 and len(b) >= 3:
        return {"type": "note_off", "channel": ch, "note": b[1], "velocity": b[2]}
    if kind == 0xB0 and len(b) >= 3:
        return {"type": "control_change", "channel": ch, "control": b[1], "value": b[2]}
    if kind == 0xD0 and len(b) >= 2:
        return {"type": "aftertouch", "channel": ch, "value": b[1]}
    return None


def cc7(v01):
    return int(round(max(0.0, min(1.0, float(v01))) * 127))


def fire_tap_fine(col):
    """The 14-bit level a Fire pad tap in column col (1-16) sends: the firmware's own maths
    (column -> 0-127 as FADERS, then ctl_daw_tap_fine)."""
    lvl = ((col - 1) * 127 + 7) // 15
    return (lvl * 16383 + 63) // 127


def _knee(kd=None):
    """(the unity column's Fire level, the DAW's 0 dB as 0-16383). kd: the DAW's 0 dB when the dialect
    has measured it (OSC learns it from REAPER), else cfg.DAWSURF_UNITY_DAW (REAPER's MCU, measured)."""
    if kd is None or not 0 < kd < 16383:
        kd = int(getattr(cfg, "DAWSURF_UNITY_DAW", 11730))
    return fire_tap_fine(getattr(cfg, "DAWSURF_UNITY_COLUMN", 12)), int(kd)


def fire_to_daw(f, kd=None):
    """Fire page level (0-16383) -> DAW fader (0-16383): two straight lines meeting at the knee, so
    the unity column's tap is exactly the DAW's 0 dB and both ends stay at 0 / 16383."""
    kf, kd = _knee(kd)
    f = max(0, min(16383, int(f)))
    if f <= kf:
        return int(round(f * kd / float(kf)))
    return int(round(kd + (f - kf) * (16383 - kd) / float(16383 - kf)))


def daw_to_fire(d, kd=None):
    """The inverse of fire_to_daw."""
    kf, kd = _knee(kd)
    d = max(0, min(16383, int(d)))
    if d <= kd:
        return int(round(d * kf / float(kd)))
    return int(round(kf + (d - kd) * (16383 - kf) / float(16383 - kd)))


def fine14(v01):
    """0.0-1.0 -> the DAW page's 14-bit level 0-16383 (Mackie faders are 14-bit)."""
    return int(round(max(0.0, min(1.0, float(v01))) * 16383))


def orc_frame(cmd, *data):
    """One host SysEx to our Fire firmware as raw bytes (F0 ... F7), every data byte 7-bit."""
    return [0xF0] + ORC_HDR + [cmd & 0x7F] + [int(b) & 0x7F for b in data] + [0xF7]


def page_name(text, n=PAGE_NAME_MAX):
    """A row name: printable ASCII only, at most n chars, outer spaces dropped."""
    return "".join(c for c in (text or "") if 32 <= ord(c) < 127).strip()[:n]


def parse_reaper_tracks(text, bgr=False):
    """REAPER web remote `GET /_/TRACK` -> {tracknumber: (name, (r, g, b) 0-127 or None)}.
    Lines: TRACK \\t number \\t name \\t flags \\t volume \\t pan \\t peak \\t meter pos \\t width
    \\t panmode \\t sendcnt \\t recvcnt \\t hwoutcnt \\t color (REAPER's main.js; 0 = master).
    color: 0 = no custom colour (None); else the low 24 bits, documented as 0xaarrggbb and
    drawn by REAPER's own pages as #rrggbb (the 0x1000000 'custom colour set' flag is dropped);
    bgr=True reads them as Windows 0xBBGGRR instead."""
    out = {}
    for ln in (text or "").splitlines():
        f = ln.rstrip("\r").split("\t")
        if len(f) < 14 or f[0] != "TRACK":
            continue
        try:
            num, col = int(f[1]), int(float(f[13]))
        except ValueError:
            continue
        rgb = None
        if col:
            c = col & 0xFFFFFF
            hi, mid, lo = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
            r, g, b = (lo, mid, hi) if bgr else (hi, mid, lo)
            rgb = (r >> 1, g >> 1, b >> 1)
        out[num] = (f[2], rgb)
    return out


def mcu_name_matches(strip_name, track_name, number):
    """Does an MCU scribble-strip name show this REAPER track? REAPER writes the first 6 chars
    of the name, or the track NUMBER ("  01  ") for an unnamed track."""
    s = (strip_name or "").strip()
    if not s:
        return False
    t = (track_name or "").strip()
    if t and page_name(t[:MCU_NAME_CHARS], MCU_NAME_CHARS) == s:
        return True
    return s.isdigit() and int(s) == number and (not t or t.lower() == "track %d" % number)


# ------------------------------------------------------------------ the module's channel
class Binding:
    """The daw module's channel (1-based) from ~/.orc1_bindings.json ({"bindings": {"<ch>":
    "<module>"}}), re-read when the file changes. None = unbound. A file that can't be read
    keeps the last answer (the brain rewrites it atomically, but never trust that)."""

    def __init__(self, path=None, module=MODULE):
        self.path = os.path.expanduser(path or getattr(cfg, "BINDINGS_FILE", "~/.orc1_bindings.json"))
        self.module = module
        self.ch = None
        self._mt = None

    def channel(self):
        try:
            mt = os.stat(self.path).st_mtime_ns
        except OSError:
            return self.ch
        if mt != self._mt:
            try:
                with open(self.path) as f:
                    b = json.load(f).get("bindings", {})
                self.ch = next((int(c) for c, m in b.items() if m == self.module), None)
                self._mt = mt
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return self.ch


# ------------------------------------------------------------------ the bridge logic (pure)
class Bridge:
    """Everything that decides. send_daw(msg_dict) / send_fire(raw_bytes) do the writing; the
    runtime only feeds it messages and port up/down news. Not thread-safe by itself: the
    runtime calls every method under `self.lock`."""

    def __init__(self, send_daw, send_fire, fader_cc=None, bank_cc=None,
                 touch_release_s=None, clock=time.monotonic, cc_with_page=None, log=None,
                 send_surface=None, bank_notes=None, send_banklight=None, dialect=None,
                 bindings=None, view_modes=None, osc_tracks=None):
        self.send_daw, self.send_fire, self.clock = send_daw, send_fire, clock
        self.send_surface = send_surface or (lambda raw: None)   # a hardware Mackie surface (the BSP)
        self.surface_up = False
        self.surface_pb = {}           # strip -> the surface's last ABSOLUTE fader value (we use deltas)
        self.bank_notes = dict(getattr(cfg, "DAWSURF_BANK_NOTES", {}) if bank_notes is None else bank_notes)
        self.send_banklight = send_banklight or (lambda raw: None)   # the BSP's main port (pad LEDs)
        self.banklight_up = False
        self.lit_bank = None           # the bank pad the BSP was last told to hold ON
        self.lit_state = None          # (bank, on) last sent: on flips while the view roams (blink)
        self.log = log or (lambda text: print("dawsurf: " + text, flush=True))
        self.fader_cc = dict(getattr(cfg, "DAWSURF_FADER_CC", {}) if fader_cc is None else fader_cc)
        self.bank_cc = dict(getattr(cfg, "DAWSURF_BANK_CC", {}) if bank_cc is None else bank_cc)
        self.touch_release_s = float(getattr(cfg, "DAWSURF_TOUCH_RELEASE_S", 0.4)
                                     if touch_release_s is None else touch_release_s)
        self.cc_with_page = bool(getattr(cfg, "DAWSURF_CC_WITH_PAGE", False)
                                 if cc_with_page is None else cc_with_page)
        self.lock = threading.RLock()
        self.dialect = str(dialect or getattr(cfg, "DAWSURF_DIALECT", "mcu")).lower()
        if self.dialect == "osc":
            adapter = OscAdapter(int(osc_tracks or getattr(cfg, "DAWSURF_OSC_TRACKS", 512)))
        else:
            self.dialect = "mcu"
            adapter = McuAdapter(devices=1)
        self.osc = bool(adapter.surface_banking)      # True: every track by number, the views are ours
        self.surf = DawSurface(adapter, send=self._to_daw)
        self.surf.subscribe(self._on_event)
        # what each control does (orc1_dawsurf_bind): the shipped table from the maps above + the user's file
        if bindings is None:
            bindings = BIND.BindingTable(BIND.default_bindings(self.fader_cc, self.bank_cc, self.bank_notes),
                                         log=self.log)
        elif isinstance(bindings, list):
            bindings = BIND.BindingTable(bindings, path="", log=self.log)
        self.binds = bindings
        self.trig_prev = {}            # (ch, cc) -> last value (a press = the rise through 64)
        # views: each surface's window of tracks (absolute, 0-based first track)
        modes = dict(getattr(cfg, "DAWSURF_VIEW_MODES", {}))
        modes.update(view_modes or {})
        self.view_mode = {v: (modes.get(v) if modes.get(v) in BIND.MODES else "shared") for v in SURFACES}
        self.view_off = {v: 0 for v in SURFACES}
        self.shared_off = 0
        self.surf_led_shown = {}       # (button, BSP slot) -> what the BSP was last told
        self.ch = None                 # 1-based daw channel, None = unbound
        self.daw_up = False
        self._daw_hello_t = None       # the MCU hello to the DAW: last sent, how many times
        self._daw_hello_tries = 0
        self._daw_state_heard = False  # a fader position has come FROM the DAW since it connected
        self.fire_up = False
        self.fire_val = {}             # (ch, cc) -> 0-127 the Fire is known to show
        self.touch_until = {}          # strip -> clock time to let the touch go
        # the Fire's DAW page
        self.fire_version = None       # hello answer; None = not (yet) answered
        self._hello_t = None
        self._hello_tries = 0
        self.page_rows_shown = None    # what the Fire was last told by 1A; None = unknown
        self.page_shown = {}           # row -> (rgb, level, name) the Fire is known to show
        self.page_dirty = set()        # rows that may differ from page_shown
        self.row_sent_t = {}           # row -> clock time of its last 1B / 1C
        # colours from REAPER's web remote
        self.web_tracks = None         # {number: (name, rgb or None)}, None = no web remote
        self.web_colour = {}           # strip 0-7 -> rgb 0-127 of the REAPER track it shows
        self.bank_offset = 0           # REAPER track index of strip 1, minus 1 (best guess)
        self._offset_stale = False
        self.n = {"to_daw": 0, "to_fire": 0, "from_fire": 0, "from_daw": 0, "hello": 0,
                  "fire_hello": 0, "page_rows": 0, "page_row": 0, "page_level": 0,
                  "page_move": 0, "page_scroll": 0, "from_surface": 0, "to_surface": 0, "bank_jump": 0,
                  "action": 0, "view_mode": 0, "osc_page": 0}

    # ---- outgoing --------------------------------------------------------------
    def _to_daw(self, port, msg):
        if self.daw_up and port == 0:
            self.send_daw(msg)
            self.n["to_daw"] += 1

    def page_capable(self):
        """The Fire is there and answered a hello with the DAW page's version."""
        return (self.fire_up and self.fire_version is not None
                and self.fire_version >= PAGE_VERSION)

    def page_active(self):
        """The Fire has the DAW page and the module is on."""
        return self.page_capable() and self.ch is not None

    def _bch(self, b):
        """A binding's channel (1-based), None while it names the daw channel and that is unbound."""
        return self.ch if b["ch"] == BIND.DAW else b["ch"]

    def _fire_cc(self, ch, num, v):
        if ch is None or not self.fire_up:
            return
        if self.page_active() and not self.cc_with_page:
            return                     # the page shows it; fire_val stays = what the Fire shows
        self.send_fire([0xB0 | (ch - 1), num & 0x7F, v & 0x7F])
        self.fire_val[(ch, num)] = v
        self.n["to_fire"] += 1

    def _on_event(self, ev):
        """Model change -> the Fire. CC: only when it changes what the Fire shows (the one rule
        that stops every echo loop). Page: the row is marked; flush_page() decides."""
        if len(ev) == 4 and ev[0] == "strip" and ev[2] in SURFACE_LED_BASE:
            k = self.slot_in_view("bsp", ev[1])
            if k is not None and 0 <= k < 8:
                self._surface_led(ev[2], k, ev[3])
        if len(ev) == 4 and ev[0] == "strip":
            if ev[2] in ("fader", "name", "colour") or (self.osc and ev[2] == "name2"):
                # the window's rows follow the strips + MASTER: mark them all (an unchanged row
                # sends nothing, flush_page compares with what the Fire shows)
                self.page_dirty.update(range(PAGE_WINDOW))
                if ev[2] == "name":
                    self._offset_stale = True
            if ev[2] == "fader" and ev[3] is not None and self.ch is not None:
                for b in self.binds.feedback():
                    if self.view_strip(b["view"], b["slot"]) == ev[1]:
                        ch = self._bch(b)
                        if ch is not None and self.fire_val.get((ch, b["num"])) != cc7(ev[3]):
                            self._fire_cc(ch, b["num"], cc7(ev[3]))

    def push_to_fire(self):
        """Every fader the DAW has told us about -> its feedback CC (after a (re)connect / rebind /
        a view move). Nothing while no DAW is connected (2026-11-09)."""
        if self.ch is None or not self.daw_up:
            return
        for b in sorted(self.binds.feedback(), key=lambda b: (str(b["ch"]), b["num"])):
            s = self.view_strip(b["view"], b["slot"])
            ch = self._bch(b)
            if s is None or ch is None:
                continue
            v = self.surf.strip(s).fader
            if v is not None and self.fire_val.get((ch, b["num"])) != cc7(v):
                self._fire_cc(ch, b["num"], cc7(v))

    # ---- views: each surface's window of tracks ----------------------------------------------------
    def effective_mode(self, view):
        """Independent needs the OSC dialect; with Mackie every view follows REAPER's one window."""
        return self.view_mode.get(view, "shared") if self.osc else "shared"

    def view_first(self, view):
        """The (absolute, 0-based) track on the view's slot 0. Mackie: REAPER's window (its display,
        else the bank counter's guess); OSC: ours."""
        if not self.osc:
            w = self.window_first()
            return self.bank_offset if w is None else w
        return self.view_off[view] if self.effective_mode(view) == "independent" else self.shared_off

    def view_strip(self, view, slot):
        """Model strip shown on a view's slot (MASTER for "master"), None = nothing there."""
        if slot == "master" or slot == MASTER:
            return MASTER
        slot = int(slot)
        if not self.osc:
            return slot if 0 <= slot < self.surf.adapter.n_strips else None
        a = self.surf.adapter
        rel = self.view_first(view) + slot - a.page * a.n_strips
        return rel if 0 <= rel < a.n_strips else None

    def slot_in_view(self, view, strip):
        """The view's slot showing a model strip, None = not in view (or the master)."""
        if not isinstance(strip, int):
            return None
        if not self.osc:
            return strip
        a = self.surf.adapter
        return a.page * a.n_strips + strip - self.view_first(view)

    def n_tracks(self):
        """OSC: REAPER's track count. The web remote's list wins (MEASURED 2026-11-09: REAPER's OSC
        reports slots past the last track as "Track N", -inf, exactly like a real unnamed track);
        without it, as far as the OSC traffic says (+1 when the page is full: there may be more)."""
        if self.web_tracks:
            return self._n_tracks()
        a = self.surf.adapter
        return a.page * a.n_strips + a.track_count() + (1 if a.page_full() else 0)

    def _has_track(self, s):
        if s is None:
            return False
        if s == MASTER:
            return True
        if self.osc:
            if self.web_tracks:
                return 1 <= self.surf.adapter.track_number(s) <= self._n_tracks()
            e = self.surf.adapter.exists.get(s)
            return bool(page_name(self.surf.strip(s).name)) if e is None else e
        return bool(page_name(self.surf.strip(s).name))

    def _shared_views(self):
        return [v for v in SURFACES if self.effective_mode(v) == "shared"]

    def _move_view(self, view, first):
        """OSC: put a view's slot 0 on track `first` (clamped); a shared view moves them all."""
        first = max(0, int(first))
        if self.effective_mode(view) == "independent":
            if first == self.view_off[view]:
                return False
            self.view_off[view] = first
            moved = [view]
        else:
            if first == self.shared_off:
                return False
            self.shared_off = first
            moved = self._shared_views()
        self.release_touches()
        self._ensure_page(first)
        self._views_changed(moved)
        return True

    def _ensure_page(self, first):
        """OSC past DAWSURF_OSC_TRACKS: switch REAPER's bank to the page holding `first`."""
        a = self.surf.adapter
        want = first // a.n_strips
        if want != a.page:
            self.release_touches()
            msgs = a.set_page(want)
            self.surf.clear()
            for port, m in msgs:
                self._to_daw(port, m)
            self.surf_led_shown.clear()
            self.n["osc_page"] += 1
            self.log("tracks %d-%d: REAPER bank %d" % (want * a.n_strips + 1, (want + 1) * a.n_strips, want + 1))
            self._views_changed(SURFACES)

    def _views_changed(self, views):
        if "fire" in views:
            self.page_dirty.update(range(PAGE_WINDOW))
        if "cc" in views:
            self.push_to_fire()
        if "bsp" in views:
            self._paint_surface()
        if self.osc:
            self.bank_offset = self.view_first("fire")

    def set_view_mode(self, surface, mode="toggle"):
        """A view (or "all") -> shared / independent / the other one. Going independent keeps the
        window where it is; going shared snaps it onto the common one."""
        views = SURFACES if surface == "all" else (surface,)
        changed = []
        for v in views:
            old = self.view_mode[v]
            new = ("independent" if old == "shared" else "shared") if mode == "toggle" else mode
            if new not in BIND.MODES or new == old:
                continue
            if new == "independent":
                self.view_off[v] = self.shared_off if self.osc else self.view_first(v)
            self.view_mode[v] = new
            changed.append(v)
            self.n["view_mode"] += 1
        if changed:
            self.log("views: %s%s" % (", ".join("%s %s" % (v, self.view_mode[v]) for v in changed),
                                      "" if self.osc else " (Mackie: every view follows REAPER's one window)"))
            if self.osc:
                self.release_touches()
                self._ensure_page(min(self.view_first(v) for v in changed))
                self._views_changed(changed)
        return changed

    def release_touches(self):
        for s in list(self.touch_until):
            del self.touch_until[s]
            self.surf.touch(s, False)

    # ---- the page ------------------------------------------------------------------
    def _send_hello(self):
        if self.fire_up:
            self.send_fire(orc_frame(CMD_HELLO))
            self._hello_t = self.clock()
            self._hello_tries += 1
            self.n["fire_hello"] += 1

    def _page_reset(self):
        self.page_rows_shown = None
        self.page_shown.clear()
        self.page_dirty.clear()
        self.row_sent_t.clear()

    def page_targets(self):
        """What the Fire's rows show, top down (2026-11-09, owner: one list, tracks then MASTER, one track
        per SELECT detent): REAPER's strips from the first, while they hold a track, then MASTER once the
        tracks run out inside the 4-row window. REAPER names an unnamed track by its number, so a blank
        strip = no track."""
        t = []
        for i in range(PAGE_WINDOW):
            s = self.view_strip("fire", i)
            if s is not None and self._has_track(s):
                t.append(s)
            else:
                break
        if len(t) < PAGE_WINDOW:
            t.append(MASTER)
        return t

    def _real_tracks(self):
        """How many of REAPER's 8 strips hold a track, from the first (a blank one ends the list)."""
        n = 0
        for i in range(self.surf.adapter.n_strips):
            if not page_name(self.surf.strip(i).name):
                break
            n += 1
        return n

    def _want_rows(self):
        return len(self.page_targets()) if (self.page_active() and self.daw_up) else 0

    def row_colour(self, row):
        if row == MASTER:
            return PAGE_MASTER_RGB
        c = self.surf.strip(row).colour                 # a dialect that knows colours (0-255)
        if c is not None:
            return tuple(max(0, min(255, int(x))) >> 1 for x in c)
        if self.osc:                                    # tracks by number: the web remote's list directly
            t = (self.web_tracks or {}).get(self.surf.adapter.track_number(row))
            if t is not None and t[1] is not None:
                return tuple(t[1])
            return PAGE_PALETTE[(self.surf.adapter.track_number(row) - 1) % len(PAGE_PALETTE)]
        if row in self.web_colour:
            return self.web_colour[row]
        return PAGE_PALETTE[row % len(PAGE_PALETTE)]

    def row_state(self, row):
        """(rgb, 14-bit level, name) the Fire should show on page row `row`."""
        tg = self.page_targets()
        tgt = tg[row] if row < len(tg) else MASTER
        s = self.surf.strip(tgt)
        name = PAGE_MASTER_NAME if tgt == MASTER else page_name(s.name)
        if not name and self.osc and tgt != MASTER and self._has_track(tgt):
            name = str(self.surf.adapter.track_number(tgt))     # an unnamed track: its number (as MCU)
        if not name:
            # REAPER names an unnamed TRACK by its number, so a blank strip = no track there (a bank
            # past the last one). "--" reads as empty; the Fire would show "row N" (2026-11-06).
            name = PAGE_EMPTY_NAME
        return (self.row_colour(tgt), daw_to_fire(fine14(s.fader), self.unity_daw()) if s.fader is not None
                else 0, name)

    def unity_daw(self):
        """The DAW's 0 dB as 0-16383 when the dialect measured it (OSC), else None (cfg's value)."""
        u = getattr(self.surf.adapter, "unity01", None)
        return int(round(u * 16383)) if u is not None else None

    def flush_page(self):
        """Send what changed. The row count first (1A, at once), then each dirty row whose last
        message is PAGE_MIN_GAP_S old: 1C when only its level differs, else 1B. A row sent too
        recently stays dirty and goes on a later tick with its latest value."""
        if not self.fire_up:
            return
        want = self._want_rows()
        if self.page_capable() and want != self.page_rows_shown:
            self.send_fire(orc_frame(CMD_PAGE_ROWS, want))
            self.n["page_rows"] += 1
            self.page_rows_shown = want
            self.page_shown.clear()    # after a 1A every row is sent in full again
            self.row_sent_t.clear()    #   at once, in this same flush
            self.page_dirty = set(range(want))
        if not want or self.page_rows_shown != want:
            self.page_dirty.clear()
            return
        if self._offset_stale:
            self._resolve_offset()
        now = self.clock()
        for row in sorted(self.page_dirty):
            if row >= want:
                self.page_dirty.discard(row)
                continue
            last = self.row_sent_t.get(row)
            if last is not None and now - last < PAGE_MIN_GAP_S:
                continue
            self.page_dirty.discard(row)
            st, old = self.row_state(row), self.page_shown.get(row)
            if st == old:
                continue
            hi, lo = (st[1] >> 7) & 0x7F, st[1] & 0x7F
            if old is not None and old[0] == st[0] and old[2] == st[2]:
                self.send_fire(orc_frame(CMD_PAGE_LEVEL, row, hi, lo))
                self.n["page_level"] += 1
            else:
                name = [ord(c) for c in st[2]]
                self.send_fire(orc_frame(CMD_PAGE_ROW, row, *(list(st[0]) + [hi, lo] + name)))
                self.n["page_row"] += 1
            self.page_shown[row] = st
            self.row_sent_t[row] = now

    def bank(self, direction, view="cc"):
        """One bank left (-1) / right (+1), from a bank CC or the Fire scrolling past an end.
        OSC: the view moves 8 tracks (right only while offset + 8 < the tracks, as REAPER's MCU)."""
        if self.osc:
            first = self.view_first(view)
            if direction > 0:
                if first + 8 < self.n_tracks():
                    self._move_view(view, first + 8)
            else:
                self._move_view(view, max(0, first - 8))
            return
        self.release_touches()         # so the new bank's faders reach the Fire
        self.surf.bank(direction)
        n = self._n_tracks()
        if direction > 0 and (n is None or self.bank_offset + 8 < n):
            self.bank_offset += 8      # REAPER: no bank right once offset + 8 >= the tracks
        elif direction < 0:
            self.bank_offset = max(0, self.bank_offset - 8)
        self._offset_stale = True

    # ---- REAPER web remote colours -----------------------------------------------------
    def _n_tracks(self):
        if not self.web_tracks:
            return None
        return max(self.web_tracks)          # numbers are 0 (master) .. N

    def set_tracks(self, tracks):
        """The web remote's track list ({number: (name, rgb)}), or None = no web remote."""
        self.web_tracks = dict(tracks) if tracks else None
        self._resolve_offset()

    def _resolve_offset(self):
        """Which REAPER track strip 1 shows: the offset whose track names match the most strip
        names (ties -> nearest the bank counter); no match at all -> the bank counter."""
        self._offset_stale = False
        if self.osc:                   # OSC knows each strip's track number: row_colour looks it up
            self.web_colour = {}
            self.page_dirty.update(range(PAGE_WINDOW))
            return
        old = dict(self.web_colour)
        if not self.web_tracks:
            self.web_colour = {}
        else:
            names = [s.name for s in self.surf.strips]
            best_key, best_o = None, self.bank_offset
            for o in range(0, max(1, self._n_tracks())):
                score = 0
                for i, nm in enumerate(names):
                    t = self.web_tracks.get(o + i + 1)
                    if t is not None and mcu_name_matches(nm, t[0], o + i + 1):
                        score += 1
                key = (score, -abs(o - self.bank_offset))
                if best_key is None or key > best_key:
                    best_key, best_o = key, o
            if best_key is not None and best_key[0] > 0:
                self.bank_offset = best_o
            self.web_colour = {}
            for i in range(len(self.surf.strips)):
                t = self.web_tracks.get(self.bank_offset + i + 1)
                if t is not None and t[1] is not None:
                    self.web_colour[i] = tuple(t[1])
        for i in set(old) | set(self.web_colour):
            if old.get(i) != self.web_colour.get(i):
                self.page_dirty.add(i)

    # ---- a hardware Mackie surface (the BeatStep Pro) on the same window ------------------------
    def _surface_led(self, name, slot, state):
        if not self.surface_up or not (0 <= slot < 8):
            return
        on = bool(state)
        if self.surf_led_shown.get((name, slot)) == on:
            return                     # the BSP already shows it
        self.surf_led_shown[(name, slot)] = on
        self.send_surface([0x90, SURFACE_LED_BASE[name] + slot, 0x7F if on else 0])
        self.n["to_surface"] += 1

    def _paint_surface(self):
        """The BSP's 8 x 4 step lights from the strips its view shows (a slot with nothing = off)."""
        for i in range(8):
            s = self.view_strip("bsp", i)
            st = self.surf.strip(s) if s is not None and s != MASTER else None
            for name in SURFACE_LED_BASE:
                self._surface_led(name, i, getattr(st, name, False) if st is not None else False)

    def surface_connected(self, up):
        """The BSP's Mackie port (re)appeared / went: forget its fader memory, light its steps."""
        self.surface_up = bool(up)
        self.surface_pb.clear()
        self.surf_led_shown.clear()
        if up:
            self._paint_surface()

    def on_surface(self, raw):
        """One message from the BSP's Mackie port: V-pot turns, faders (used as DELTAS: the BSP
        remembers its own position per knob, so after a bank jump an absolute value would make
        the new track leap), and the solo / mute / rec / select buttons. Its 8 strips are the
        "bsp" view's slots."""
        if len(raw) < 3:
            return
        st, a, b = raw[0] & 0xF0, raw[1], raw[2]
        ch = raw[0] & 0x0F
        if st == 0xB0 and 16 <= a <= 23:
            d = (b & 0x3F) * (-1 if b & 0x40 else 1)
            s = self.view_strip("bsp", a - 16)
            if d and s is not None:
                self.n["from_surface"] += 1
                self.surf.turn(s, d)
        elif st == 0xE0 and ch < 8:
            s = self.view_strip("bsp", ch)
            v = (b << 7) | a
            last = self.surface_pb.get(ch)
            self.surface_pb[ch] = v
            if last is None:
                return                 # the first reading only says where the BSP's knob sits
            if v != last:
                delta = v - last
            elif v == 0:
                delta = -SURFACE_STEP  # at its own bottom the BSP repeats 0 per click (measured):
            elif v >= 16383 - SURFACE_STEP + 1:
                delta = SURFACE_STEP   #   = one more click down (/ up at its top), so the track can
            else:                      #   go all the way though the BSP's knob is pinned
                return
            if s is None:
                return
            cur = self.surf.strip(s).fader
            base = fine14(cur) if cur is not None else 0
            self.n["from_surface"] += 1
            self._fader_move(s, max(0, min(16383, base + delta)), 16383.0)
        elif st in (0x90, 0x80) and 0 <= a < 32:
            name = ("rec", "solo", "mute", "select")[a // 8]
            s = self.view_strip("bsp", a % 8)
            if s is not None:
                self.n["from_surface"] += 1
                self.surf.button(name, s, st == 0x90 and b > 0)

    def window_first(self):
        """0-based REAPER track shown on strip 1, from REAPER's own 2-digit assignment display
        ("01" = track 1); None when it isn't a number (then the bank counter is the guess)."""
        t = (self.surf.display.get("assignment") or "").strip()
        return int(t) - 1 if t.isdigit() and int(t) > 0 else None

    def goto_bank(self, bank, view="bsp"):
        """A bank pad: move the window so strip 1 = track 8*bank + 1 (MCU bank presses for whole 8s,
        channel presses for the rest; REAPER stops at its last track by itself). OSC: the view
        jumps there, or to the last bank that holds a track."""
        if self.osc:
            last = max(0, self.n_tracks() - 1)
            if self._move_view(view, min(8 * bank, 8 * (last // 8))):
                self.n["bank_jump"] += 1
            return
        cur = self.window_first()
        if cur is None:
            cur = self.bank_offset
        delta = 8 * bank - cur
        if not delta:
            return
        self.release_touches()
        sign = 1 if delta > 0 else -1
        for _ in range(abs(delta) // 8):
            self.surf.bank(sign)
        for _ in range(abs(delta) % 8):
            self.surf.channel(sign)
        self.bank_offset = max(0, cur + delta)
        self._offset_stale = True
        self.n["bank_jump"] += 1

    def _pad_led(self, bank, value):
        items = getattr(cfg, "DAWSURF_BANK_LED_ITEMS", [])
        if 0 <= bank < len(items):
            self.send_banklight([0xF0, 0x00, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, items[bank], value, 0xF7])

    def banklight_connected(self, up):
        """The BSP's main port (re)appeared / went: the next tick lights the bank pad again."""
        self.banklight_up = bool(up)
        self.lit_bank = None
        self.lit_state = None

    def update_banklight(self):
        """Hold the pad for the bank on screen ON (blinking ~1 s while the BSP's view roams on its
        own); hand the previous one back (0x40) to the BSP."""
        if not self.banklight_up:
            return
        want = self.current_bank() if (self.daw_up and self.ch is not None) else None
        on = True
        if want is not None and self.effective_mode("bsp") == "independent":
            on = int(self.clock() / BANK_BLINK_HALF_S) % 2 == 0
        if (want, on) == self.lit_state:
            return
        if self.lit_bank is not None and self.lit_bank != want:
            self._pad_led(self.lit_bank, 0x40)
        if want is not None:
            self._pad_led(want, 0x7F if on else 0)
        self.lit_bank = want
        self.lit_state = (want, on)

    def current_bank(self):
        """Which bank pad shows the window (the bank holding strip 1's track), or None. OSC: the
        "bsp" view's (the bank pads' own)."""
        cur = self.view_first("bsp") if self.osc else self.window_first()
        return None if cur is None else cur // 8

    # ---- news from the runtime ---------------------------------------------------
    def set_channel(self, ch):
        ch = int(ch) if ch is not None else None
        if ch == self.ch:
            return False
        self.release_touches()
        self.ch = ch
        self.fire_val.clear()
        self.trig_prev.clear()
        if ch is not None:
            self.push_to_fire()
        return True

    def daw_connected(self, up):
        self.daw_up = bool(up)
        self._daw_hello_t, self._daw_hello_tries = None, 0
        self._daw_state_heard = False
        if up:
            self._send_daw_hello()
            if self.fire_up:               # the Fire waited for a DAW (2026-11-09): now it hears from us
                if self.fire_version is None:
                    self._send_hello()
                self.push_to_fire()

    def _send_daw_hello(self):
        self.n["hello"] += 1
        self._daw_hello_t = self.clock()
        self._daw_hello_tries += 1
        self.surf.hello()              # REAPER: "XYZ" serial = re-send the whole surface state

    def daw_state_seen(self):
        """The DAW has sent at least one fader position since it (re)connected (REAPER sends every
        strip's on a reset). Fader moves from the Fire fill the model too, so they don't count."""
        return self._daw_state_heard

    def fire_connected(self, up):
        """The Fire (re)appeared or went. Forget its version and page (a replug is a reboot),
        ask hello again; the CC faders are repainted at once (an old Fire answers 3)."""
        self.fire_up = bool(up)
        self.fire_val.clear()          # a (re)plugged Fire shows nothing we know of
        self.fire_version = None
        self._hello_t, self._hello_tries = None, 0
        self._page_reset()
        if up and self.daw_up:             # no DAW = we say nothing to the Fire (2026-11-09: no plug-in hiccup)
            self._send_hello()
            self.push_to_fire()

    def on_fire(self, raw):
        """One raw message off the Fire's own output. Only our host frames matter (CCs from the
        Fire reach the bridge through the brain and the bus)."""
        if len(raw) < 7 or raw[0] != 0xF0 or raw[-1] != 0xF7 or list(raw[1:5]) != ORC_HDR:
            return
        cmd, data = raw[5], list(raw[6:-1])
        if cmd == CMD_HELLO:
            if not data:
                return                 # a hello REQUEST (not from the Fire's output, never mind)
            v = data[0]
            if v != self.fire_version:
                self.fire_version = v
                self._page_reset()     # unknown: the next flush tells the Fire its rows
                self.log("Fire firmware version %d: %s" % (
                    v, "DAW page" if v >= PAGE_VERSION else "no DAW page, faders by CC"))
        elif cmd == MSG_PAGE_MOVE and len(data) >= 3:
            row, v = data[0], (data[1] & 0x7F) << 7 | (data[2] & 0x7F)     # 14-bit (fw v0.31f)
            if not self.page_rows_shown or row >= self.page_rows_shown:
                return
            tg = self.page_targets()
            if row >= len(tg):
                return
            old = self.page_shown.get(row)
            if old is not None:
                if old[1] == v:
                    return             # what the Fire already shows: no news
                self.page_shown[row] = (old[0], v, old[2])     # BEFORE the model hears it
            self.n["page_move"] += 1
            kd = self.unity_daw()
            # the Fire's scale -> the DAW's (0 dB knee); the unity column itself = exactly 0 dB where the
            # dialect can say dB (OSC: /track/N/volume/db 0), else the knee's position (Mackie: as before)
            db = 0.0 if v == _knee(kd)[0] else None
            self._fader_move(tg[row], fire_to_daw(v, kd), 16383.0, db=db)
        elif cmd == MSG_PAGE_SCROLL and data:
            if not self.page_rows_shown:
                return
            self.n["page_scroll"] += 1
            count = max(1, min(127, data[1])) if len(data) > 1 else 1
            self.shift(-1 if data[0] == 0 else +1, count, view="fire")

    def shift(self, direction, count=1, view="fire"):
        """The Fire's SELECT on the DAW page: move REAPER's view `count` tracks (MCU channel left /
        right). Forward stops once the last track + MASTER fill the 4 rows (the list's end). OSC:
        the view moves; the Fire's stops there, any other view stops with the last track on slot 1."""
        if self.osc:
            first = self.view_first(view)
            room = (PAGE_WINDOW - 1) if view == "fire" else 1
            top = max(0, self.n_tracks() - room)
            new = first + direction * count
            new = min(new, max(top, first)) if direction > 0 else max(0, new)
            self._move_view(view, new)
            return
        if direction > 0:
            count = min(count, max(0, self._real_tracks() - (PAGE_WINDOW - 1)))
        if count <= 0:
            return
        self.release_touches()
        for _ in range(count):
            self.surf.channel(direction)
        self.bank_offset = max(0, self.bank_offset + direction * count)
        self._offset_stale = True

    def _fader_move(self, s, v, full=127.0, db=None):
        """A user's fader move (a CC 0-127, or a page row 0-16383): hold the touch, move the fader
        (to an exact dB too when given and the dialect can say it)."""
        if not self.surf.strip(s).touched:
            self.surf.touch(s, True)
        self.touch_until[s] = self.clock() + self.touch_release_s
        if db is None:
            self.surf.set_fader(s, v / full)
        else:
            self.surf.set_fader_db(s, db, v / full)

    def on_bus(self, raw):
        """One raw message off the ORC's port ORC-daw: a CC or a note that a binding (orc1_dawsurf_bind) maps
        to an action. Nothing at all while the daw module is unbound."""
        if self.ch is None or len(raw) < 3:
            return
        st, num, v = raw[0] & 0xF0, raw[1], raw[2]
        ch = (raw[0] & 0x0F) + 1
        if st == 0xB0:
            kind = "cc"
        elif st in (0x80, 0x90):
            kind = "note"
        else:
            return
        b = self.binds.find(kind, ch, num, self.ch)
        if b is not None:
            self.do(b, kind, ch, num, v, st == 0x90 and v > 0)

    def _edge(self, kind, ch, num, v, on):
        """(pressed, released) for this message: a note-on / note-off, or a CC through 64."""
        if kind == "note":
            return on, not on
        prev = self.trig_prev.get((ch, num), 0)
        self.trig_prev[(ch, num)] = v
        return v >= 64 > prev, prev >= 64 > v

    def do(self, b, kind="cc", ch=None, num=None, v=127, on=True):
        """Run one bound action (a binding dict, see orc1_dawsurf_bind) for one message."""
        a = b["action"]
        if a in ("fader", "pan") and kind == "cc" and not b.get("rel"):
            s = self.view_strip(b["view"], b["slot"])
            if a == "fader":
                if self.fire_val.get((ch, num)) == v:
                    return             # what the Fire already shows: an echo, or no news
                self.fire_val[(ch, num)] = v       # BEFORE set_fader: its own model event then matches
                if s is None:
                    return
                self.n["from_fire"] += 1
                self._fader_move(s, v)
            elif s is not None:
                self.n["action"] += 1
                self.surf.set_pan(s, v / 127.0)    # Mackie has no absolute pan: sends nothing
            return
        if a in ("fader", "pan"):                  # relative: 64 = still, 65.. up, 63.. down
            if kind != "cc" or v == 64:
                return
            s = self.view_strip(b["view"], b["slot"])
            if s is None:
                return
            ticks = v - 64
            self.n["action"] += 1
            if a == "pan":
                self.surf.turn(s, ticks)
            else:
                cur = self.surf.strip(s).fader
                self._fader_move(s, max(0.0, min(1.0, (cur or 0.0) + ticks * b["step"])), 1.0)
            return
        pressed, released = self._edge(kind, ch, num, v, on)
        if a == "transport" and b["name"] in BIND.TRANSPORT_HOLD:
            if pressed or released:
                self.n["action"] += 1
                self.surf.button(b["name"], None, pressed)
            return
        if not pressed:
            return
        self.n["action"] += 1
        if a in ("mute", "solo", "rec", "select"):
            s = self.view_strip(b["view"], b["slot"])
            if s is not None and s != MASTER:
                self.surf.press(a, s)
        elif a == "transport":
            self.surf.press(b["name"])
        elif a == "bank":
            self.bank(b["dir"], b["view"])
        elif a == "shift":
            self.shift(1 if b["n"] > 0 else -1, abs(b["n"]), b["view"])
        elif a == "goto_bank":
            if self.daw_up:
                if self.osc and self.view_first(b["view"]) // 8 == b["bank"]:
                    # the ALREADY-LIT pad (the bank this view is on): toggle follow / roam
                    # (owner 2026-11-09); the pad blinks while the view roams on its own
                    self.set_view_mode(b["view"], "toggle")
                else:
                    self.goto_bank(b["bank"], b["view"])
        elif a == "view_mode":
            self.set_view_mode(b["surface"], b["mode"])

    def on_daw(self, raw):
        """One message from the DAW: raw MIDI bytes (Mackie) or an OSC (address, args) tuple."""
        if self.osc:
            if not raw or not isinstance(raw[0], str):
                return
            self.n["from_daw"] += 1
            if raw[0].endswith("/volume"):
                self._daw_state_heard = True   # a fader position FROM the DAW (meters don't count)
            self.surf.receive(raw, 0)
            return
        d = from_bytes(raw)
        if d is not None:
            self.n["from_daw"] += 1
            if d.get("type") == "pitchwheel":
                self._daw_state_heard = True   # a fader position FROM the DAW (meters / clock don't count)
            self.surf.receive(d, 0)

    def tick(self):
        now = self.clock()
        for s, t in list(self.touch_until.items()):
            if now >= t:
                del self.touch_until[s]
                self.surf.touch(s, False)
        if self.fire_up and self.daw_up and self.ch is not None and self.fire_version is None:
            wait = HELLO_RETRY_S if self._hello_tries < HELLO_FAST_TRIES else HELLO_SLOW_S
            if self._hello_t is None or now - self._hello_t >= wait:
                self._send_hello()
        # 2026-11-06, MEASURED live: after a bridge restart the hello went out the moment the rtpmidid
        # port reappeared, before the network session had reconnected, so REAPER never saw it and the
        # model stayed empty (page rows blank, level 0). Re-send it until REAPER's state arrives.
        if self.daw_up and not self.daw_state_seen():
            wait = HELLO_RETRY_S if self._daw_hello_tries < HELLO_FAST_TRIES else HELLO_SLOW_S
            if self._daw_hello_t is None or now - self._daw_hello_t >= wait:
                self._send_daw_hello()
        self.update_banklight()
        self.flush_page()

    def status(self):
        def strip(s):
            return {k: getattr(s, k) for k in ("index", "name", "name2", "fader", "touched",
                                                "mute", "solo", "rec", "select", "meter")}
        rows = [{"row": r, "rgb": list(st[0]), "level": st[1], "name": st[2]}
                for r, st in sorted(self.page_shown.items())]
        page = {"fire_version": self.fire_version, "active": self.page_active(),
                "rows_shown": self.page_rows_shown, "rows": rows,
                "pending": sorted(self.page_dirty), "cc_with_page": self.cc_with_page,
                "colours": "web" if self.web_colour else "palette",
                "web_tracks": len(self.web_tracks) if self.web_tracks else 0,
                "bank_offset": self.bank_offset}
        if self.osc:                   # 512 strips: only the ones holding a track (at most 128)
            shown = [s for s in self.surf.strips if self._has_track(s.index)][:128]
        else:
            shown = self.surf.strips
        views = {v: {"mode": self.view_mode[v], "effective": self.effective_mode(v),
                     "first": self.view_first(v)} for v in SURFACES}
        osc = None
        if self.osc:
            a = self.surf.adapter
            osc = {"page": a.page, "tracks": self.n_tracks(), "unity01": a.unity01,
                   "strips": a.n_strips}
        return {"module": MODULE, "channel": self.ch, "daw": self.daw_up, "fire": self.fire_up,
                "dialect": self.dialect, "osc": osc, "views": views,
                "bindings": [dict(b) for b in self.binds.all()],
                "binding_errors": list(self.binds.errors),
                "fader_cc": {str(k): v for k, v in sorted(self.fader_cc.items())},
                "bank_cc": {str(k): v for k, v in sorted(self.bank_cc.items())},
                "strips": [strip(s) for s in shown],
                "master": strip(self.surf.master) if self.surf.master else None,
                "transport": dict(self.surf.transport), "display": dict(self.surf.display),
                "page": page, "counts": dict(self.n)}


# ------------------------------------------------------------------ runtime: ports
def seq_listening(text, me):
    """/proc/asound/seq/clients -> True when a port of client `me` is Connected From the port
    named exactly ORC_PORT ("ORC-daw", the ORC's port for this module; was ORC1-Synth-Bus until 2026-11-09)."""
    bus, mine, cname, sub = set(), [], None, False
    for ln in text.splitlines():
        m = re.match(r'Client\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m:
            cid, cname = m.group(1), m.group(2)
            continue
        m = re.match(r'\s+Port\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m and cname is not None:
            if m.group(2).strip() == ORC_PORT:
                bus.add("%s:%s" % (cid, m.group(1)))
            sub = cname == me
            continue
        m = re.match(r"\s+Connected From:\s*(.*)", ln)
        if m and sub:
            mine += [a.split("[")[0].strip() for a in m.group(1).split(",")]
    return any(a in bus for a in mine)


def usb_card_idents():
    """{ALSA card name: 'bus/dev'} for every USB sound / MIDI card now (a copy of orc1._usb_card_idents: this is a
    separate process). The USB address changes on every plug-in and every reboot of the device."""
    out = {}
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                head, sep, name = line.partition("]: ")
                num = head.strip().split(" ", 1)[0]
                if not sep or not num.isdigit():
                    continue
                name = name.split(" - ", 1)[-1].strip()
                try:
                    with open(f"/proc/asound/card{num}/usbbus") as u:
                        out[name] = u.read().strip()
                except OSError:
                    pass
    except OSError:
        pass
    return out


def port_usb_ident(port, idents=usb_card_idents):
    """The USB address of the card behind an rtmidi port name ('ORC FW seq:ORC FW seq MIDI 1 32:0' -> card
    'ORC FW seq'), or None."""
    try:
        return (idents() or {}).get((port or "").split(":", 1)[0].strip())
    except Exception:
        return None


class Port:
    """ONE rtmidi client for the whole run (a new client per check would leak ALSA clients).
    check() re-lists the ports: returns "new" when the target just (re)appeared (a replug = the
    same name with a new client:port number), else whether it is open. Inputs get their
    callback set again after EVERY open_port (python-rtmidi's close_port() drops it).
    ident(port name) -> the device's USB address (port_usb_ident), for the Fire (2026-11-12): a Fire reboot often
    brings its port back under the SAME name and client number, and a handle opened before then drops every message
    silently (measured, orc1-firevu 2026-10-13), so a new USB address is a new port too, as in orc1_scenes.FireBarLink."""

    def __init__(self, client, label, match, callback=None, sysex=False, ident=None):
        import rtmidi
        self.rt = rtmidi.MidiIn(name=client) if callback else rtmidi.MidiOut(name=client)
        if callback:
            self.rt.ignore_types(sysex=not sysex, timing=True, active_sense=True)
        self.label, self.match, self.callback = label, match, callback
        self.ident_of = ident
        self.name = None
        self.ident = None

    def check(self, extra_ok=True):
        try:
            ports = self.rt.get_ports()
        except Exception:
            ports = []
        idx = next((i for i, p in enumerate(ports) if self.match(p)), None)
        now_ident = self.ident_of(ports[idx]) if (self.ident_of and idx is not None) else None
        if self.name is not None and (idx is None or ports[idx] != self.name or not extra_ok
                                      or now_ident != self.ident):
            self.close()
        if idx is not None and self.name is None:
            try:
                self.rt.open_port(idx, self.label)
                if self.callback:
                    try:
                        self.rt.cancel_callback()
                    except Exception:
                        pass
                    self.rt.set_callback(self.callback)
                self.name = ports[idx]
                self.ident = now_ident
                return "new"
            except Exception as e:
                print("dawsurf: could not open %s (%s)" % (ports[idx], e), flush=True)
                self.close()
        return self.name is not None

    def write(self, data):
        if self.name is not None:
            self.rt.send_message(data)

    def close(self):
        try:
            if self.name is not None:
                self.rt.close_port()
        except Exception:
            pass
        self.name = None


class BriefPort(Port):
    """An OUTPUT held open only for the moment of a write (2026-11-09): keeping the BSP's MAIN port
    open locked its rawmidi device, so `amidi` (the /beatstep page's writes + bsp_readback) got
    'Device or resource busy'. check() only watches that the port exists."""

    def check(self, extra_ok=True):
        try:
            ports = self.rt.get_ports()
        except Exception:
            ports = []
        name = next((p for p in ports if self.match(p)), None)
        new = name is not None and name != self.name
        self.name = name
        return "new" if new else name is not None

    def write(self, data):
        if self.name is None:
            return
        ports = self.rt.get_ports()
        idx = next((i for i, p in enumerate(ports) if p == self.name), None)
        if idx is None:
            return
        self.rt.open_port(idx, self.label)
        try:
            self.rt.send_message(data)
        finally:
            self.rt.close_port()

    def close(self):
        self.name = None


def is_fire_out(name):
    nl = (name or "").lower()
    if cfg.DAWSURF_CLIENT.lower() in nl:
        return False
    return cfg.is_fire_port(name) or any(p.lower() in nl for p in cfg.DAWSURF_FIRE_PORT_NAMES)


def write_status(st, path=STATUS):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


FIRE_PORTS = ("fire_in", "fire")
SURFACE_PORTS = ("surf_in", "surf_out", "bank_led")
BANK_BLINK_HALF_S = 0.5                  # the lit bank pad blinks 0.5 s on / 0.5 s off while the BSP roams
SURFACE_STEP = 128                       # one click of the BSP's fader knobs (measured: 128 per click)
SURFACE_LED_BASE = {"rec": 0, "solo": 8, "mute": 16, "select": 24}   # Mackie button notes per strip


def is_banklight_port(name):
    nl = (name or "").lower()
    return (cfg.DAWSURF_CLIENT.lower() not in nl
            and any(s.lower() in nl for s in getattr(cfg, "DAWSURF_BANK_LED_PORTS", [])))


def is_mcu_surface(name):
    """A hardware Mackie surface port (cfg.DAWSURF_MCU_SURFACES), never one of the bridge's own."""
    nl = (name or "").lower()
    return (cfg.DAWSURF_CLIENT.lower() not in nl
            and any(s.lower() in nl for s in getattr(cfg, "DAWSURF_MCU_SURFACES", [])))


class OscLink:
    """REAPER's OSC surface over UDP, shaped like a Port for the Runtime (check() -> "new" / bool,
    .name, write(), close()). One socket bound on listen_port for the whole link; a reader thread
    decodes every datagram FROM the configured host only (anything else on the port is dropped)
    and hands each (address, args) to `callback`. host None = not set up: the link stays down and
    nothing is sent. Opened by the runtime's first check (never at import / construction)."""

    def __init__(self, host, send_port, listen_port, callback, log=None, sock_factory=None):
        self.host, self.send_port, self.listen_port = host, int(send_port), int(listen_port)
        self.callback = callback
        self.log = log or (lambda text: print("dawsurf: " + text, flush=True))
        self.sock_factory = sock_factory
        self.sock = None
        self.addr = None
        self.name = None
        self._stop = False
        self.dropped = 0

    def _open(self):
        import socket
        self.addr = socket.gethostbyname(self.host)
        s = self.sock_factory() if self.sock_factory else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)   # a refresh = a burst
        except OSError:
            pass
        s.bind(("0.0.0.0", self.listen_port))
        s.settimeout(0.5)
        self.sock, self._stop = s, False
        threading.Thread(target=self._reader, args=(s,), name="dawsurf-osc", daemon=True).start()

    def _reader(self, s):
        while not self._stop and self.sock is s:
            try:
                data, src = s.recvfrom(65535)
            except OSError as e:
                if "timed out" in str(e):
                    continue
                return                 # closed
            if src[0] != self.addr:
                self.dropped += 1
                continue
            try:
                msgs = OSC.osc_decode(data)
            except OSC.OscError:
                self.dropped += 1
                continue
            for m in msgs:
                self.callback((m[0], tuple(m[1])))

    def check(self, extra_ok=True):
        if self.sock is not None:
            return True
        if not self.host:
            return False
        try:
            self._open()
        except OSError as e:
            self.log("OSC link %s:%d / listen %d failed (%s)" % (self.host, self.send_port, self.listen_port, e))
            self.close()
            return False
        self.name = "osc %s:%d (listening on %d)" % (self.addr, self.send_port, self.listen_port)
        return "new"

    def write(self, msg):
        if self.sock is not None:
            self.sock.sendto(OSC.encode(msg), (self.addr, self.send_port))

    def close(self):
        self._stop = True
        s, self.sock, self.name = self.sock, None, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


class Runtime:
    """One port / binding check (every CHECK_S). `ports` = {"bus", "daw_in", "daw_out", "fire",
    "fire_in"} -> Port-like objects (check(extra_ok) -> "new" / bool, .name, close()); tests pass
    fakes. "fire_in" (the Fire's own output: hello answers, page moves) is optional."""

    def __init__(self, br, ports, binding, client=None, log=None, daw_alive=None):
        self.br, self.ports, self.binding = br, ports, binding
        # 2026-11-09 (owner: "daw mode stuff really only needs to be going on when i have a daw connected"):
        # with OSC the DAW 'port' is just an address, there whether REAPER runs or not, so the bridge thought
        # the DAW was always up and talked to the Fire (hello, page, fader CCs) on every plug-in. daw_alive()
        # = REAPER really answers (its web remote); None = port presence decides, as before (Mackie).
        self.daw_alive = daw_alive
        self.client = client or cfg.DAWSURF_CLIENT
        self.log = log or (lambda text: print("dawsurf: " + text, flush=True))
        self.said = {}

    def say(self, key, up, text):
        if self.said.get(key) != up:          # each state change logged once, not every second
            self.said[key] = up
            self.log(text)

    def check(self, seqtext=None):
        P, br = self.ports, self.br
        b = P["bus"]
        listening = seqtext is None or b.name is None or seq_listening(seqtext, self.client + "-bus")
        if b.check(extra_ok=listening) == "new":
            self.log("listening on %s" % b.name)
        self.say("bus", b.name is not None, "ORC port %s" % (b.name or "not found, waiting"))
        ch = self.binding.channel()
        try:
            with br.lock:
                if br.binds.refresh():                  # the user's binding file changed
                    br.fire_val.clear()
                    br.push_to_fire()
                if br.set_channel(ch):
                    self.log("daw module %s" % ("on channel %d" % ch if ch else "unbound: idle"))
                if ch is None:
                    br.flush_page()                     # a page on the Fire goes (1A 0) first
                    for k in ("daw_in", "daw_out") + FIRE_PORTS + SURFACE_PORTS:
                        if k in P:
                            P[k].close()
                    br.daw_up = br.fire_up = br.surface_up = False
                    self.said.pop("daw", None)
                    self.said.pop("fire", None)
                else:
                    r_in = P["daw_in"].check()          # input first: the hello's answer
                    r_out = P["daw_out"].check()        #   must have somewhere to land
                    port_up = P["daw_out"].name is not None and P["daw_in"].name is not None
                    up = port_up and (self.daw_alive is None or bool(self.daw_alive()))
                    if up and ("new" in (r_in, r_out) or not br.daw_up):
                        br.daw_connected(True)          # (re)appeared: hello
                    elif not up and br.daw_up:
                        br.daw_connected(False)
                    self.say("daw", up, "DAW port %s" % (
                        P["daw_out"].name if up else
                        "%s, but REAPER is not answering: DAW mode idle" % P["daw_out"].name if port_up else
                        "not there (PC / its rtpMIDI session off?)"))
                    r_fin = P["fire_in"].check() if "fire_in" in P else None   # first: the
                    r = P["fire"].check()                                       # hello's answer
                    fup = P["fire"].name is not None
                    if "new" in (r, r_fin) or fup != br.fire_up:
                        br.fire_connected(fup)          # (re)appeared: hello, CC repaint
                    self.say("fire", fup, "Fire %s" % (P["fire"].name if fup else "not there"))
                    if "surf_in" in P:
                        rs_in = P["surf_in"].check()
                        rs_out = P["surf_out"].check()
                        sup = P["surf_in"].name is not None and P["surf_out"].name is not None
                        if sup != br.surface_up or (sup and "new" in (rs_in, rs_out)):
                            br.surface_connected(sup)
                        self.say("surf", sup, "Mackie surface %s" % (P["surf_in"].name if sup else "not there"))
                    if "bank_led" in P:
                        rb = P["bank_led"].check()
                        bup = P["bank_led"].name is not None
                        if bup != br.banklight_up or (bup and rb == "new"):
                            br.banklight_connected(bup)
        except Exception as e:                           # a write to a port that just vanished
            self.log("port error (%s), reconnecting" % e)
            for k in ("daw_in", "daw_out") + FIRE_PORTS + SURFACE_PORTS:
                if k in P:
                    P[k].close()
            with br.lock:
                br.daw_up = br.fire_up = br.surface_up = False
            self.said.pop("daw", None)
            self.said.pop("fire", None)
        with br.lock:
            st = br.status()
        st.update({"bus": b.name is not None, "daw_port": P["daw_out"].name,
                   "fire_port": P["fire"].name,
                   "fire_in_port": P["fire_in"].name if "fire_in" in P else None})
        return st


class ReaperWeb:
    """Track colours from REAPER's web remote (cfg.DAWSURF_REAPER_WEB): GET <url>/_/TRACK every
    poll_s while the Fire's DAW page is up, on its OWN thread (an HTTP wait never holds the
    bridge lock or a MIDI callback). Three failures in a row = back to the palette."""
    FAILS_BEFORE_DROP = 3

    def __init__(self, br, url=None, fetch=None, poll_s=None, bgr=None, timeout=0.8, log=None):
        self.br = br
        self.url = (url or getattr(cfg, "DAWSURF_REAPER_WEB", None) or "").rstrip("/")
        self.poll_s = float(poll_s if poll_s is not None else
                            getattr(cfg, "DAWSURF_REAPER_WEB_POLL_S", 1.5))
        self.bgr = bool(getattr(cfg, "DAWSURF_REAPER_WEB_BGR", False) if bgr is None else bgr)
        self.timeout = timeout
        self.fetch = fetch or self._http
        self.log = log or (lambda text: print("dawsurf: " + text, flush=True))
        self.fails = 0
        self.ok = None                 # last state logged
        self.probe = False             # True: poll even when the page is down (REAPER's liveness, OSC)
        self.alive = None              # None = not asked yet, True = answering, False = FAILS_BEFORE_DROP misses

    def _http(self, url):
        import urllib.request
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return r.read(256 * 1024).decode("utf-8", "replace")

    def poll_once(self):
        """One poll (skipped unless the page is up, or this is the liveness probe). True = tracks read."""
        with self.br.lock:
            wanted = self.br.page_active() and self.br.daw_up
        if not (wanted or self.probe) or not self.url:
            return False
        try:
            tracks = parse_reaper_tracks(self.fetch(self.url + "/_/TRACK"), bgr=self.bgr)
        except Exception as e:
            self.fails += 1
            if self.fails >= self.FAILS_BEFORE_DROP:
                self.alive = False
                with self.br.lock:
                    if self.br.web_tracks is not None:
                        self.br.set_tracks(None)
                if self.ok is not False:
                    self.ok = False
                    self.log("REAPER web remote %s not answering (%s): palette colours%s" % (
                        self.url, e, "; REAPER counted as NOT running" if self.probe else ""))
            return False
        self.fails = 0
        self.alive = True
        with self.br.lock:
            self.br.set_tracks(tracks)
        if self.ok is not True:
            self.ok = True
            self.log("REAPER web remote %s: %d tracks, track colours on" % (self.url, len(tracks)))
        return True

    def start(self, stop):
        def run():
            while not stop["now"]:
                try:
                    self.poll_once()
                except Exception as e:            # never let the thread die silently
                    print("dawsurf: web poll error (%s)" % e, flush=True)
                time.sleep(self.poll_s)
        t = threading.Thread(target=run, name="dawsurf-web", daemon=True)
        t.start()
        return t


def main():
    C = cfg.DAWSURF_CLIENT
    ports = {}

    osc = str(getattr(cfg, "DAWSURF_DIALECT", "mcu")).lower() == "osc"

    def send_daw(msg):
        if osc:
            ports["daw_out"].write(msg)              # (address, args): OscLink encodes it
            return
        raw = to_bytes(msg)
        if raw is not None:
            ports["daw_out"].write(raw)

    def send_fire(raw):
        ports["fire"].write(raw)

    def send_surface(raw):
        if ports.get("surf_out") is not None and ports["surf_out"].name is not None:
            ports["surf_out"].write(raw)

    def send_banklight(raw):
        if ports.get("bank_led") is not None and ports["bank_led"].name is not None:
            ports["bank_led"].write(raw)

    br = Bridge(send_daw, send_fire, send_surface=send_surface, send_banklight=send_banklight)
    web = ReaperWeb(br) if getattr(cfg, "DAWSURF_REAPER_WEB", None) else None

    def on_bus(event, _data=None):
        try:
            with br.lock:
                br.on_bus(event[0])
        except Exception as e:                           # never let a callback die silently
            print("dawsurf: bus message error (%s)" % e, flush=True)

    def on_daw(event, _data=None):
        try:
            with br.lock:
                br.on_daw(event[0])
        except Exception as e:
            print("dawsurf: DAW message error (%s)" % e, flush=True)

    def on_fire(event, _data=None):
        try:
            with br.lock:
                br.on_fire(event[0])
        except Exception as e:
            print("dawsurf: Fire message error (%s)" % e, flush=True)

    def on_surface(event, _data=None):
        try:
            with br.lock:
                br.on_surface(event[0])
        except Exception as e:
            print("dawsurf: surface message error (%s)" % e, flush=True)

    ports["bus"] = Port(C + "-bus", "bus in", lambda p: cfg.is_orc_port(p, MODULE), callback=on_bus)
    if osc:                            # REAPER over UDP: one link is both directions
        ports["daw_in"] = ports["daw_out"] = OscLink(
            getattr(cfg, "DAWSURF_OSC_HOST", None), int(getattr(cfg, "DAWSURF_OSC_SEND_PORT", 8100)),
            int(getattr(cfg, "DAWSURF_OSC_LISTEN_PORT", 9100)),
            callback=lambda msg: on_daw((msg, None)))
    else:
        ports["daw_in"] = Port(C + "-daw-in", "daw in", cfg.is_dawsurf_port, callback=on_daw, sysex=True)
        ports["daw_out"] = Port(C + "-daw-out", "daw out", cfg.is_dawsurf_port)
    ports["fire"] = Port(C + "-fire", "fire faders", is_fire_out, ident=port_usb_ident)
    ports["fire_in"] = Port(C + "-fire-in", "fire page in", is_fire_out, callback=on_fire, sysex=True,
                            ident=port_usb_ident)
    ports["surf_in"] = Port(C + "-surf-in", "mackie surface in", is_mcu_surface, callback=on_surface)
    ports["surf_out"] = Port(C + "-surf-out", "mackie surface out", is_mcu_surface)
    ports["bank_led"] = BriefPort(C + "-bankled", "bank pad lights", is_banklight_port)
    if web is not None and osc:
        web.probe = True               # polls even with the page down: it is how we know REAPER is there
    rt = Runtime(br, ports, Binding(), C,
                 daw_alive=(lambda: web.alive is True) if (web is not None and osc) else None)
    stop = {"now": False}

    def _bye(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print("dawsurf: started; %s, map %s" % (
        "REAPER OSC %s:%s, listening on %s" % (cfg.DAWSURF_OSC_HOST, cfg.DAWSURF_OSC_SEND_PORT,
                                               cfg.DAWSURF_OSC_LISTEN_PORT) if osc
        else "DAW port %s" % cfg.DAWSURF_PORT_NAMES, cfg.DAWSURF_FADER_CC), flush=True)
    if web is not None:
        web.start(stop)
    next_check = 0.0
    last_err = None
    while not stop["now"]:
        now = time.time()
        if now >= next_check:
            next_check = now + CHECK_S
            try:
                with open("/proc/asound/seq/clients") as f:
                    seqtext = f.read()
            except OSError:
                seqtext = None
            st = rt.check(seqtext)
            st["t"] = now
            write_status(st)
        try:
            with br.lock:
                br.tick()
            last_err = None
        except Exception as e:
            if str(e) != last_err:             # once per new error, not 50 times a second
                last_err = str(e)
                print("dawsurf: tick error (%s)" % e, flush=True)
        time.sleep(TICK_S)
    try:
        with br.lock:
            br.release_touches()       # REAPER must not be left holding a touched fader
            br.daw_up = False
            br.flush_page()            # the Fire's DAW page goes with the bridge (1A 0)
    except Exception:
        pass
    for p in ports.values():
        p.close()
    write_status({"t": time.time(), "module": MODULE, "stopped": True})
    print("dawsurf: stopped", flush=True)


if __name__ == "__main__":
    main()
