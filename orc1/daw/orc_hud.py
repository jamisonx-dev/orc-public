#!/usr/bin/env python3
"""ORC-1 HUD: an always-on play-state screen for the HDMI monitor.

Reads /dev/shm/orc1_playstate.json (published by the brain, see
orc1_playstate.py) and draws it: tempo, transport + bar/beat/step, the key and
chord, the sequencer tracks with a playhead, which modules are sounding, the
held notes, arp / BeatBuddy.

Deliberately light (the Pi's RAM and the recorder come first):
  * plain 2D software drawing -- no GL, no browser;
  * it only READS a tmpfs file, never talks to the brain, never joins JACK;
  * it redraws at 30 fps only while something moves (transport running, a
    module pulse fading, new data), otherwise ~6 fps;
  * the kiosk script runs it at SCHED_IDLE.

Everything that decides WHAT is shown (the Feed, the Pulses, bar_pos) is plain
Python with no pygame, so the suite tests it without a display.

SIGUSR1 saves the frame it is showing to /tmp/orc1_hud_shot.png.
"""
import json
import math
import os
import signal
import sys
import time

# pygame.init() would start SDL AUDIO and hold the Pi's headphone card open
# (the 2026-09-29 xrun hunt). Never initialise audio here.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

PATH = "/dev/shm/orc1_playstate.json"
STALE_S = 2.5            # the brain heartbeats every 1 s
SHOT = "/tmp/orc1_hud_shot.png"

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# -- palette (stark black, like the visuals) -------------------------------------
BG = (6, 6, 8)
PANEL = (16, 17, 22)
TEXT = (228, 230, 236)
DIM = (96, 100, 112)
AMBER = (255, 178, 40)
GREEN = (70, 225, 125)
RED = (240, 70, 70)
CYAN = (70, 195, 240)
TRACK_COLORS = [(235, 80, 80), (240, 150, 50), (235, 210, 60), (110, 215, 90),
                (60, 205, 190), (70, 150, 240), (150, 110, 240), (225, 95, 200)]

REGIONS = ("hdr", "trk", "mod", "ftr")      # repainted (and flipped) independently
ANIM_DT = 1.0 / 15.0     # fades (beat lamps, module lights) repaint at most this often;
                         # new DATA (a step, a chord) is still drawn the moment it arrives

SHORT = {"chip_p1": "P1", "chip_p2": "P2", "chip_tri": "TRI", "chip_noise": "NOISE",
         "chip_noise2": "NOISE2", "chip_pcm": "PCM", "gb_wave": "WAVE",
         "poly": "POLY", "mono": "MONO", "drums": "DRUMS", "bbkit": "BBKIT",
         "sampler": "SMPL", "visuals": "VIS"}


# ============================== model (no pygame) =================================

def bar_pos(step):
    """Global 16th-note step -> (bar, beat, sixteenth), all 1-based, or None
    while stopped (step < 0). 4/4, the same grid the sampler quantises to."""
    if step is None or step < 0:
        return None
    return (step // 16 + 1, (step % 16) // 4 + 1, step % 4 + 1)


def beat_flash(transport, since_s, tau=0.12):
    """0..1 flash that is 1 on a quarter-note boundary and decays after it.
    `since_s` = seconds since the CURRENT step began (age at publish plus the
    time since we received it)."""
    if not transport.get("running") or transport.get("step", -1) < 0:
        return 0.0
    into_beat = (transport["step"] % 4) * (transport.get("step_ms", 125.0) / 1000.0) + since_s
    return math.exp(-max(0.0, into_beat) / tau)


def _content_key(d):
    """The frame without the fields that change on every write (stamp, counter,
    the step's age at publish) -- the same rule the publisher uses."""
    c = {k: v for k, v in d.items() if k not in ("t", "n")}
    tr = c.get("transport")
    if isinstance(tr, dict):
        c["transport"] = {k: v for k, v in tr.items() if k != "age_ms"}
    return json.dumps(c, sort_keys=True, separators=(",", ":"))


class Feed:
    """The latest published state. poll() re-reads the file only when its mtime
    changed. `recv` is the monotonic time we first saw the current frame, so
    the playhead can be extrapolated between frames."""

    def __init__(self, path=PATH):
        self.path = path
        self.state = None
        self.recv = 0.0
        self._mtime = None
        self._key = None
        self.changed = False      # True for the poll that picked up NEW CONTENT

    def poll(self, mono):
        self.changed = False
        try:
            st = os.stat(self.path)
            m = (st.st_mtime_ns, st.st_ino)   # os.replace = new inode, even inside one mtime tick
        except OSError:
            if self.state is not None:
                self.state, self._mtime, self._key, self.changed = None, None, None, True
            return
        if m == self._mtime:
            return
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return                # keep the last good frame; try again next poll
        if not isinstance(d, dict):
            return
        self._mtime = m
        key = _content_key(d)
        if self.state is not None and key == self._key:
            # the brain's once-a-second heartbeat: same content, fresh stamp. Keep
            # `recv` (the playhead extrapolates from the frame that carried the step)
            # and do NOT report a change, or the screen never idles.
            self.state["t"] = d.get("t", self.state.get("t"))
            return
        self.state, self._key, self.recv, self.changed = d, key, mono, True

    def age(self, wall):
        """Seconds since the brain wrote it, or None with no data."""
        if self.state is None:
            return None
        return max(0.0, wall - float(self.state.get("t", 0)))

    def is_stale(self, wall):
        a = self.age(wall)
        return a is None or a > STALE_S


class Pulses:
    """Per-module light that jumps on each new hit and fades (~0.22 s)."""

    def __init__(self, tau=0.22):
        self.tau = tau
        self._last = {}
        self.level = {}
        self._t = None

    def update(self, mods, now):
        dt = 0.0 if self._t is None else max(0.0, now - self._t)
        self._t = now
        k = math.exp(-dt / self.tau)
        for name in list(self.level):
            self.level[name] *= k
            if self.level[name] < 0.01:
                del self.level[name]
        for name, v in (mods or {}).items():
            hits = v.get("hits", 0)
            prev = self._last.get(name)
            self._last[name] = hits
            if prev is None:
                continue                      # first sight: history is not a hit
            if hits != prev:                  # != so a brain restart (count resets) still flashes
                vel = min(127, max(1, int(v.get("vel", 100))))
                self.level[name] = max(self.level.get(name, 0.0), 0.35 + 0.65 * vel / 127.0)

    def get(self, name):
        return self.level.get(name, 0.0)

    def active(self):
        return bool(self.level)


def module_order(bindings):
    """Bound modules in MIDI-channel order -> [(module, channel)]."""
    try:
        return sorted(((m, int(c)) for m, c in bindings.items()), key=lambda x: (x[1], x[0]))
    except Exception:
        return []


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ============================== drawing (pygame) ==================================

class Hud:
    def __init__(self, size, path=PATH):
        import pygame
        self.pg = pygame
        pygame.font.init()
        self.W, self.H = size
        self.u = self.H / 720.0
        self.feed = Feed(path)
        self.pulses = Pulses()
        self._fonts = {}
        self._text = {}
        self._now = 0.0
        self._grid = None
        self._grid_key = None
        self.dirty = set(REGIONS)     # which screen regions need repainting this tick
        self._was_fast = True
        self._was_stale = None
        self._was_flash = False
        self._was_pulse = False
        self._pend = set()            # fade repaints waiting for the next animation slot
        self._last_anim = -1e9

    @property
    def redraw(self):
        return bool(self.dirty)

    def rect(self, name):
        """Screen rectangle of a region. Heights match draw()'s layout."""
        u, W, H = self.u, self.W, self.H
        top, foot = int(128 * u), H - int(66 * u)
        strip = foot - int(76 * u)
        return {"hdr": (0, 0, W, top), "trk": (0, top, W, strip - top),
                "mod": (0, strip, W, foot - strip), "ftr": (0, foot, W, H - foot)}[name]

    # -- text ---------------------------------------------------------------------
    def font(self, px, bold=False):
        key = (int(px), bold)
        f = self._fonts.get(key)
        if f is None:
            try:
                f = self.pg.font.Font(FONT_BOLD if bold else FONT_REG, int(px))
            except Exception:
                f = self.pg.font.Font(None, int(px * 1.3))
            self._fonts[key] = f
        return f

    def fit(self, text, px, maxw, bold=False):
        """Largest font size <= px at which `text` is at most maxw pixels wide."""
        w = self.font(px, bold).size(text)[0]
        return px if w <= maxw else max(10, int(px * maxw / max(1, w)))

    def label(self, surf, text, x, y, px, color=TEXT, bold=False, anchor="l"):
        """Draw text; anchor l/r/c horizontally from x, y = top. Returns width."""
        key = (text, int(px), bold, color)
        img = self._text.get(key)
        if img is None:
            if len(self._text) > 600:
                self._text.clear()
            img = self.font(px, bold).render(text, True, color)
            self._text[key] = img
        w = img.get_width()
        x0 = x if anchor == "l" else (x - w if anchor == "r" else x - w // 2)
        surf.blit(img, (int(x0), int(y)))
        return w

    # -- frame --------------------------------------------------------------------
    def tick(self, mono, wall):
        """Update the model and decide what to repaint (self.dirty). Returns True
        while the screen should animate fast."""
        self.feed.poll(mono)
        s = self.feed.state
        self.pulses.update(s.get("mods") if s else None, mono)
        tr = (s or {}).get("transport", {})
        running = bool(tr.get("running"))
        fast = running or self.pulses.active() or (mono - self.feed.recv < 0.6)
        stale = self.feed.is_stale(wall)
        flash = (beat_flash(tr, (mono - self.feed.recv) + tr.get("age_ms", 0) / 1000.0)
                 > 0.01) if s else False
        pulse = self.pulses.active()
        # Nothing moving and nothing new = leave the glass alone (X keeps the last
        # frame). Each region repaints only when ITS inputs changed, plus one more
        # time after its motion stops so the last fade lands.
        if flash or self._was_flash:
            self._pend.add("hdr")                 # beat lamps fading
        if pulse or self._was_pulse:
            self._pend.add("mod")                 # module lights fading
        if self.feed.changed or stale != self._was_stale:
            self.dirty = set(REGIONS)             # new content (or the banner): everything
            self._pend.clear()
            self._last_anim = mono
        else:
            self.dirty = set()
            if self._pend and mono - self._last_anim >= ANIM_DT:
                self.dirty, self._pend = self._pend, set()
                self._last_anim = mono
        self._was_fast, self._was_stale = fast, stale
        self._was_flash, self._was_pulse = flash, pulse
        return fast

    def draw(self, surf, mono, wall, which=None):
        """Paint the regions in `which` (default all). Returns the rects painted,
        for pygame.display.update(rects) -- flipping the whole frame to X costs
        as much as drawing it."""
        self._now = mono
        u, W, H = self.u, self.W, self.H
        s = self.feed.state
        if s is None:
            surf.set_clip(None)
            surf.fill(BG)
            self._no_signal(surf, True)
            return [(0, 0, W, H)]
        pad = int(16 * u)
        tr = s.get("transport", {})
        ch = s.get("chord", {})
        since = (mono - self.feed.recv) + tr.get("age_ms", 0) / 1000.0
        stale = self.feed.is_stale(wall)
        top = int(128 * u)
        foot = H - int(66 * u)
        strip = foot - int(76 * u)
        rects = []
        for name in REGIONS:
            if which is not None and name not in which:
                continue
            r = self.rect(name)
            surf.set_clip(r)
            surf.fill(BG, r)
            if name == "hdr":
                self._header(surf, s, tr, ch, since, pad, stale)
            elif name == "trk":
                self._tracks(surf, s, tr, pad, top, strip - int(10 * u))
            elif name == "mod":
                self._modules(surf, s, pad, strip, foot - int(8 * u))
            else:
                self._footer(surf, s, ch, pad, foot)
            rects.append(r)
        surf.set_clip(None)
        if stale and (which is None or len(rects) == len(REGIONS)):
            self._no_signal(surf, False)          # banner on top of the dimmed last frame
        return rects

    def _no_signal(self, surf, never):
        msg = "NO SIGNAL FROM THE ORC BRAIN" if never else "BRAIN NOT UPDATING"
        self.label(surf, msg, self.W // 2, self.H // 2 - 30 * self.u, 40 * self.u,
                   RED, True, "c")
        self.label(surf, "waiting for /dev/shm/orc1_playstate.json",
                   self.W // 2, self.H // 2 + 20 * self.u, 20 * self.u, DIM, False, "c")

    def _header(self, surf, s, tr, ch, since, pad, stale):
        pg, u, W = self.pg, self.u, self.W
        dim = (lambda c: mix(BG, c, 0.35)) if stale else (lambda c: c)
        # tempo
        self.label(surf, f"{tr.get('bpm', 0):.1f}", pad, 14 * u, 70 * u, dim(AMBER), True)
        clk = {"internal": "INT", "external": "EXT", "drift": "DRIFT"}.get(tr.get("clock"), "--")
        self.label(surf, f"BPM  {clk}", pad + 4 * u, 92 * u, 20 * u, DIM)
        # transport badge
        bx = int(W * 0.27)
        if tr.get("rec"):
            txt, col = "REC", RED
        elif tr.get("running"):
            txt, col = "PLAY", GREEN
        else:
            txt, col = "STOP", DIM
        bw, bh = int(118 * u), int(46 * u)
        pg.draw.rect(surf, mix(BG, col, 0.22 if not stale else 0.08),
                     (bx, int(18 * u), bw, bh), border_radius=int(8 * u))
        pg.draw.rect(surf, dim(col), (bx, int(18 * u), bw, bh), width=max(2, int(2 * u)),
                     border_radius=int(8 * u))
        self.label(surf, txt, bx + bw // 2, 26 * u, 30 * u, dim(col), True, "c")
        # bar / beat / step
        bp = bar_pos(tr.get("step", -1)) if tr.get("running") else None
        pos_w = int(W * 0.40)
        ptxt = f"BAR {bp[0]:03d}   BEAT {bp[1]}   STEP {bp[2]}" if bp else "BAR ---   BEAT -   STEP -"
        self.label(surf, ptxt, bx, 80 * u, self.fit(ptxt, 24 * u, pos_w), dim(TEXT) if bp else DIM)
        # four beat lamps
        flash = beat_flash(tr, since)
        cur = ((tr.get("step", 0) % 16) // 4) if tr.get("running") and tr.get("step", -1) >= 0 else -1
        for i in range(4):
            on = (i == cur)
            c = mix(PANEL, AMBER, 0.25 + 0.75 * flash) if on else mix(BG, DIM, 0.35)
            pg.draw.circle(surf, c, (bx + bw + int(34 * u) + int(i * 30 * u), int(41 * u)),
                           int(10 * u))
        # chord + key
        rx = W - pad
        cname = ch.get("name", "--") or "--"
        name_w = rx - (bx + bw + int(150 * u))
        self.label(surf, cname, rx, 10 * u, self.fit(cname, 74 * u, name_w, True), dim(TEXT), True, "r")
        key = f"{ch.get('key', '?')}  {ch.get('scale', '')}   {ch.get('preset', '')}   oct {ch.get('octave', '')}"
        key_w = rx - (bx + pos_w + int(16 * u))
        self.label(surf, key, rx, 92 * u, self.fit(key, 20 * u, key_w), DIM, False, "r")
        pg.draw.line(surf, mix(BG, DIM, 0.4), (pad, int(120 * u)), (W - pad, int(120 * u)), 1)

    def _tracks(self, surf, s, tr, pad, y0, y1):
        pg, u, W = self.pg, self.u, self.W
        tracks = s.get("tracks") or []
        if not tracks:
            self.label(surf, "no sequencer tracks", pad, y0 + 10 * u, 22 * u, DIM)
            return
        label_w = int(112 * u)
        maxlen = max(1, max(int(t.get("len", 16)) for t in tracks))
        cw = (W - 2 * pad - label_w) / maxlen
        rowh = min(int(46 * u), (y1 - y0) / max(1, len(tracks)))
        # The grid (every step of every track) only changes when a pattern is
        # edited, so it is drawn once into a cached surface and blitted; the
        # per-frame work is just the playhead cells on top.
        key = (W, self.H, pad, y0, y1, tuple(
            (t.get("name"), t.get("len"), t.get("steps"), bool(t.get("mute"))) for t in tracks))
        if key != self._grid_key:
            self._grid_key = key
            self._grid = pg.Surface((W, max(1, y1 - y0)))
            self._grid.fill(BG)
            for ri, t in enumerate(tracks):
                self._draw_row(self._grid, t, ri, label_w, cw, rowh, pad, 0, head=False)
        surf.blit(self._grid, (0, y0))
        if tr.get("running"):
            for ri, t in enumerate(tracks):
                self._draw_row(surf, t, ri, label_w, cw, rowh, pad, y0, head=True)

    def _cell_rect(self, i, ri, label_w, cw, rowh, pad, y0):
        u = self.u
        return (int(pad + label_w + i * cw), int(y0 + ri * rowh) + 1,
                max(2, int(cw) - max(1, int(2 * u))), max(4, int(rowh) - max(2, int(4 * u))))

    def _draw_row(self, surf, t, ri, label_w, cw, rowh, pad, y0, head):
        """head=False: the whole row. head=True: only the playhead cell, over a grid."""
        pg, u = self.pg, self.u
        ln = int(t.get("len", 16))
        steps = t.get("steps", "")
        col = TRACK_COLORS[ri % len(TRACK_COLORS)]
        muted = bool(t.get("mute"))
        radius = max(1, int(3 * u))
        if not head:
            y = int(y0 + ri * rowh)
            self.label(surf, (t.get("name", "trk"))[:9], pad, y + max(0, (rowh - 20 * u) / 2 - 2),
                       18 * u, DIM if muted else mix(col, TEXT, 0.35))
        cells = [int(t.get("pos", 0))] if head else range(ln)
        for i in cells:
            if not (0 <= i < ln):
                continue
            on = i < len(steps) and steps[i] == "1"
            if on:
                c = mix(BG, col, 0.28 if muted else 1.0)
                if head:
                    c = mix(c, (255, 255, 255), 0.55)
            else:
                c = mix(BG, DIM, 0.34 if (i // 4) % 2 == 0 else 0.20)
                if head:
                    c = mix(c, TEXT, 0.55)
            pg.draw.rect(surf, c, self._cell_rect(i, ri, label_w, cw, rowh, pad, y0),
                         border_radius=radius)

    def _modules(self, surf, s, pad, y0, y1):
        pg, u, W = self.pg, self.u, self.W
        mods = module_order(s.get("bindings") or {})
        if not mods:
            return
        n = len(mods)
        gap = int(8 * u)
        bw = (W - 2 * pad - gap * (n - 1)) / n
        h = y1 - y0
        for i, (name, chn) in enumerate(mods):
            lv = self.pulses.get(name)
            x = int(pad + i * (bw + gap))
            base = mix(BG, DIM, 0.16)
            pg.draw.rect(surf, mix(base, CYAN, lv), (x, y0, int(bw), h), border_radius=int(8 * u))
            if lv > 0.05:
                pg.draw.rect(surf, mix(DIM, CYAN, lv), (x, y0, int(bw), h),
                             width=max(1, int(2 * u)), border_radius=int(8 * u))
            tc = mix(DIM, (255, 255, 255), lv)
            nm = SHORT.get(name, name.upper()[:7])
            self.label(surf, nm, x + bw / 2, y0 + 8 * u, self.fit(nm, 22 * u, bw - 8 * u, True),
                       tc, True, "c")
            self.label(surf, f"ch {chn}", x + bw / 2, y0 + 8 * u + 28 * u, 16 * u, DIM, False, "c")

    def _footer(self, surf, s, ch, pad, y):
        u, W = self.u, self.W
        held = s.get("held") or []
        if held:
            self.label(surf, "  ".join(held[:8]), pad, y, 40 * u, AMBER, True)
        else:
            self.label(surf, "-", pad, y, 40 * u, mix(BG, DIM, 0.5), True)
        arp = s.get("arp") or {}
        if arp.get("on"):
            self.label(surf, f"ARP {arp.get('pattern', '')} {arp.get('rate', '')}",
                       int(W * 0.55), y + 6 * u, 24 * u, GREEN, True)
        else:
            self.label(surf, "ARP OFF", int(W * 0.55), y + 6 * u, 24 * u, mix(BG, DIM, 0.7))
        bb = s.get("bb") or {}
        if bb:
            on = bb.get("playing")
            self.label(surf, f"BB {'PLAY' if on else 'STOP'} P{bb.get('part', '')}",
                       W - pad, y + 6 * u, 24 * u, GREEN if on else mix(BG, DIM, 0.7), True, "r")


# ============================== main loop =========================================

def main():
    import pygame
    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.mouse.set_visible(False)
    hud = Hud(screen.get_size())
    want_shot = []
    signal.signal(signal.SIGUSR1, lambda *_: want_shot.append(1))
    clock = pygame.time.Clock()
    frames, t_log, draw_s, flip_s = 0, time.monotonic(), 0.0, 0.0
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return 0
        mono, wall = time.monotonic(), time.time()
        fast = hud.tick(mono, wall)
        if hud.dirty:
            t0 = time.perf_counter()
            rects = hud.draw(screen, mono, wall, hud.dirty)
            t1 = time.perf_counter()
            pygame.display.update(rects)
            draw_s += t1 - t0
            flip_s += time.perf_counter() - t1
            frames += 1
        if want_shot:
            del want_shot[:]
            try:
                pygame.image.save(screen, SHOT)
            except Exception as e:
                print(f"hud: shot failed: {e}", flush=True)
        if mono - t_log >= 60:
            print(f"hud: {frames / (mono - t_log):.1f} drawn fps, "
                  f"draw {1000 * draw_s / max(1, frames):.1f} ms + "
                  f"flip {1000 * flip_s / max(1, frames):.1f} ms per frame", flush=True)
            frames, t_log, draw_s, flip_s = 0, mono, 0.0, 0.0
        clock.tick(30 if fast else 6)


if __name__ == "__main__":
    sys.exit(main() or 0)
