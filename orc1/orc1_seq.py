"""
orc1_seq.py — Step sequencer, controller-agnostic.

Owns track data, transport, clock, and note output.  FireHandler (and any
other front-end) holds a reference and drives it via the public API.

Public API
----------
Transport
  seq.play()               start from current positions
  seq.stop(reset=False)    stop; reset=True rewinds all tracks to step 0
  seq.running              bool property

External clock (call from MIDI thread)
  seq.clock_tick()         one 24-PPQN pulse from upstream gear

Tracks (shared list — front-end may mutate dicts directly)
  seq.tracks               list of track dicts
  seq.n_tracks             len(tracks) property
  seq.insert_track(idx, d) insert a pre-built track dict at idx
  seq.remove_track(idx)    remove track at idx

Track management (2026-07-01 ownership refactor — all lock-guarded, the
preferred way to mutate tracks from any front-end; direct dict writes to
seq.tracks[...] still work for reads but should no longer be used for writes)
  seq.add_track(after_idx=None, color=None, **overrides)
                            build + insert a new track, returns its index
  seq.reorder_track(idx, new_idx)   swap two tracks' positions
  seq.set_track_prop(idx, prop, value)   set note/ch/vel/length/mode/name/color/bright
  seq.set_track_mute(idx, mute=None)     toggle if mute is None
  seq.solo_track(idx)        mute every other track, unmute this one
  seq.unmute_all()           clear mute on every track
  seq.set_step_note(tidx, spos, note)    set/clear a per-step note override

Step editing
  seq.toggle_step(tidx, step)
  seq.cycle_step_vel(tidx, step)   cycle a step's velocity tier (ghost/normal/accent)
  seq.set_step_vel(tidx, step, v)  set an exact per-step velocity (None = track default)
  seq.clear_steps(*tidxs)

MIDI clock output
  seq.clock_out              bool property
  seq.set_clock_out(on, ports=None)
  seq.toggle_clock_out()

Callback
  seq.step_callback = fn   called after each advance (e.g. dirty render flag)

Cleanup
  seq.close()
"""

import threading
import time
import json
import os
import datetime

try:
    import mido
except ImportError:
    mido = None

# 24 PPQN / 6 = 4th-note subdivisions at 1/16 step resolution
PULSES_PER_STEP = 6
MAX_LEN         = 64
MAX_TRACKS      = 32

# Fallback per-step velocity tiers [ghost, normal, accent] for when no config
# is reachable (e.g. load_seq_state runs before a Sequencer/eng exists). The
# live values come from SEQ["VEL_TIERS"] via Sequencer._vel_tiers().
SEQ_VEL_TIERS   = [60, 95, 127]

SEQ_STATE_PATH  = os.path.expanduser("~/orc1_seq_state.json")

# Default track colour palette (0-127 per channel), used by add_track() and
# the cold-boot bootstrap when no front-end / config supplies its own.
# Copied from orc1_fire.py's TRACK_COLOR (2026-07-01 ownership refactor —
# moved here so a track's colour no longer depends on Fire existing).
DEFAULT_TRACK_PALETTE = [
    (0x40, 0x00, 0x00), (0x00, 0x40, 0x00), (0x00, 0x00, 0x40),
    (0x40, 0x20, 0x00), (0x30, 0x00, 0x30), (0x00, 0x30, 0x30),
    (0x30, 0x30, 0x00), (0x18, 0x18, 0x18),
]

# Cold-boot fallback tracks, used only when there's no saved state AND no
# SEQ["DEFAULT_TRACKS"] in orc1_config. Mirrors that canonical kit so
# headless/web-only boots (no Fire, no saved pattern) still get a sensible
# starting kit instead of an empty sequencer. Full 909 kit incl. cowbell;
# the dead CH7 "bass" lane was removed 2026-07-21 (CH7/CH_MONO is silent).
_FALLBACK_DEFAULT_TRACKS = [
    {"name": "kick",  "note": 36, "ch": 6, "vel": 115, "length": 16},
    {"name": "snare", "note": 38, "ch": 6, "vel": 110, "length": 16},
    {"name": "chat",  "note": 42, "ch": 6, "vel": 95,  "length": 12},
    {"name": "ohat",  "note": 46, "ch": 6, "vel": 95,  "length": 16},
    {"name": "clap",  "note": 39, "ch": 6, "vel": 105, "length": 16},
    {"name": "rim",   "note": 37, "ch": 6, "vel": 100, "length": 16},
    {"name": "ltom",  "note": 41, "ch": 6, "vel": 100, "length": 16},
    {"name": "mtom",  "note": 43, "ch": 6, "vel": 100, "length": 16},
    {"name": "htom",  "note": 50, "ch": 6, "vel": 100, "length": 16},
    {"name": "cym",   "note": 49, "ch": 6, "vel": 90,  "length": 16},
    {"name": "cowb",  "note": 56, "ch": 6, "vel": 100, "length": 16},
]


def _seq_cfg(eng):
    """Return orc1_config.SEQ (the controller-agnostic sequencer-defaults dict),
    with the legacy FIRE dict as a fallback so a partial config still boots."""
    try:
        cfg = getattr(eng, "cfg", None)
        seqcfg = getattr(cfg, "SEQ", {}) or {}
        if seqcfg:
            return seqcfg
        # Legacy fallback: SEQ_TRACKS/TRACK_COLORS used to live under FIRE.
        return getattr(cfg, "FIRE", {}) or {}
    except Exception:
        return {}


def _resolve_palette(eng):
    """Look up the configured track colour palette (SEQ.TRACK_COLORS in
    orc1_config), falling back to DEFAULT_TRACK_PALETTE. Split out from
    _build_default_tracks so callers that already have a track list (just
    need the palette for add_track()) don't pay to rebuild a default kit."""
    raw_palette = _seq_cfg(eng).get("TRACK_COLORS", DEFAULT_TRACK_PALETTE)
    return [tuple(c) for c in raw_palette]


def _build_default_tracks(eng):
    """Build the initial track list from config (SEQ["DEFAULT_TRACKS"] in
    orc1_config) or the built-in fallback kit above. Same logic that used
    to live in FireHandler.__init__ (orc1_fire.py, pre-2026-07-01), relocated
    here so it runs with zero Fire dependency (Bug A fix). SEQ_TRACKS moved out
    of the FIRE dict 2026-07-21 (the seq is controller-independent)."""
    seqcfg = _seq_cfg(eng)
    palette = _resolve_palette(eng)
    tracks = []
    # New home is SEQ["DEFAULT_TRACKS"]; legacy FIRE["SEQ_TRACKS"] still read via
    # _seq_cfg's fallback for any un-migrated config.
    _default_tracks = seqcfg.get("DEFAULT_TRACKS",
                                 seqcfg.get("SEQ_TRACKS", _FALLBACK_DEFAULT_TRACKS))
    for i, t in enumerate(_default_tracks):
        length  = max(1, min(MAX_LEN, t.get("length", 16)))
        raw_col = t.get("color")
        color   = tuple(raw_col) if raw_col else palette[i % len(palette)]
        tracks.append({
            "name":   t.get("name", "trk"),
            "note":   t.get("note", 36),
            "ch":     t.get("ch", 6),
            "vel":    t.get("vel", 110),
            "dest":   t.get("dest", "synth"),
            "color":  color,
            "bright": max(0, min(127, int(t.get("bright", 127)))),
            "length": length,
            "steps":  [False] * MAX_LEN,
            "step_vels": [None] * MAX_LEN,
            "step_notes": [None] * MAX_LEN,
            "mode":   t.get("mode", "note"),
            "pos":    0,
            "mute":   False,
        })
    return tracks, palette


def load_seq_state(path=None):
    """Load a manually saved sequencer state.
    Returns a list of track dicts ready to pass to Sequencer(), or None if
    no save file exists or it can't be parsed.  Called by FireHandler before
    constructing the Sequencer so saved patterns survive service restarts."""
    path = path or SEQ_STATE_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        tracks = []
        for t in data.get("tracks", []):
            length = max(1, min(MAX_LEN, int(t.get("length", 16))))
            def _pad(lst, fill, n=MAX_LEN):
                lst = list(lst) if lst else []
                return (lst + [fill] * n)[:n]
            # Per-step velocities. Backward compat: pre-2026-08 saves stored a
            # binary accent[] bool (+20 vel); convert True -> accent tier so old
            # patterns keep their accents. New saves carry step_vels directly.
            _sv = t.get("step_vels")
            if _sv is None and t.get("accent") is not None:
                _acc_tier = (SEQ_VEL_TIERS[2] if SEQ_VEL_TIERS else 127)
                _sv = [(_acc_tier if a else None) for a in t.get("accent")]
            tracks.append({
                "name":       t.get("name",  "trk"),
                "ch":         int(t.get("ch",    6)),
                "note":       int(t.get("note", 36)),
                "mode":       t.get("mode",  "note"),
                "vel":        int(t.get("vel",  100)),
                "length":     length,
                "color":      tuple(t.get("color", [80, 0, 0])),
                "bright":     max(0, min(127, int(t.get("bright", 127)))),
                "dest":       t.get("dest", "synth"),
                "mute":       bool(t.get("mute", False)),
                "steps":      _pad(t.get("steps"),      False),
                "step_vels":  _pad(_sv,                 None),
                "step_notes": _pad(t.get("step_notes"), None),
                "pos":        0,
            })
        print(f"[seq] loaded {len(tracks)} track(s) from {path}")
        return tracks if tracks else None
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[seq] state load error: {e}")
        return None


class Sequencer:
    def __init__(self, eng, tracks=None, bpm_fn=None,
                 step_callback=None, clock_ports=None, colors_path=None):
        """
        eng           — orc1 engine (for send_synth_midi / note_on_out / send_to)
        tracks        — list of pre-built track dicts, or None (2026-07-01:
                        self-bootstrapping) to have the Sequencer build its
                        own list: try load_seq_state() first, then fall back
                        to config/built-in defaults via _build_default_tracks.
                        Passing an explicit list still works (front-ends may
                        still hand-build one) but is no longer required.
        bpm_fn        — callable() → float BPM
        step_callback — called after every advance; use for render dirty-flag.
                        May be set/replaced after construction too (e.g. by
                        FireHandler once it exists) — see self.step_callback.
        clock_ports   — list of port names for MIDI clock output
        """
        self._eng          = eng
        self._bpm_fn       = bpm_fn or (lambda: 120.0)
        self.step_callback = step_callback
        # Audio-domain downbeat hook (2026-07-20, Bug-1 clock-drift fix):
        # set by orc1.py; called from _advance() on bar-start steps so the
        # engine can snapshot the skipback ring's write head at the downbeat.
        # Distinct from step_callback (which the Fire owns for LED renders).
        self.bar_cb       = None    # callable, must never block
        self.bar_cb_steps = 16      # steps per bar (orc1.py overrides)

        # ── Track state ───────────────────────────────────────────────────────
        # Shared with the front-end: FireHandler holds self.tracks = seq.tracks
        # and mutates dicts directly for READS (render/display). Writes should
        # go through the lock-guarded methods below (add_track/reorder_track/
        # set_track_prop/set_track_mute/set_step_note/toggle_step/etc) —
        # 2026-07-01 ownership refactor closed the direct-write bypasses that
        # used to race against _advance() on the clock thread (Bug B).
        self._track_lock = threading.Lock()

        self._track_palette = _resolve_palette(eng)
        if tracks is None:
            tracks = load_seq_state()
            if not tracks:
                tracks, _pal = _build_default_tracks(eng)
        self.tracks = list(tracks)   # copy the list; dicts are shared refs

        # ── Per-track colours: instant-persist, decoupled from the pattern save ──
        # Colours are a COSMETIC, not pattern content, so they get their own
        # tiny overlay file that is rewritten the instant a colour is changed
        # (see set_track_prop / _persist_colors) rather than waiting for an
        # explicit SEQ_SAVE. This honours the "you decide when a good pattern is
        # committed" model while letting a colour tweak stick immediately.
        #
        # The path is resolved HERE (construction time), not at import: the Fire
        # test harness builds a Sequencer inside a sandboxed HOME that is then
        # torn down, so a construction-time path lands in that sandbox and any
        # later write from the swept ALT+knob2 dial fails harmlessly instead of
        # polluting the user's real ~/.orc1_seq_colors.json.
        self._colors_path = colors_path or os.path.expanduser("~/.orc1_seq_colors.json")
        # Applied AFTER the pattern load so the latest instant-picked colour wins
        # over whatever colour the pattern was last saved with.
        self._apply_colors_overlay()

        # ── Transport state ───────────────────────────────────────────────────
        self._running    = False
        self._ext_pulse  = 0
        self._last_on    = []     # (note, ch0, dest) awaiting note-off

        # ── Bar-phase anchor (2026-07-07, for quantized sampler grabs) ────────
        # A global step counter (independent of per-track pos/length, which
        # can be polyrhythmic) plus the wall time of the last advance. The
        # musical bar for loop-grabbing = 16 sixteenth-note steps (4/4).
        self._global_step = -1
        self._step_wall   = None

        # ── MIDI clock output ─────────────────────────────────────────────────
        self._clock_out   = False
        self._clock_ports = list(clock_ports or [])

        # ── Recording state ───────────────────────────────────────────────────
        # record_mode: when True, incoming notes (via record_note_on) are
        # written into record_track at the current step position on each advance.
        # Overdub semantics: steps you don't play are left unchanged.
        self.record_mode  = False
        self.record_track = 0           # which track receives recorded notes
        self._rec_note    = None        # (note, vel) currently held, or None

        # ── Clock: subscribe to ArpEngine if available, else run own thread ──
        # When the arp is present, its PPQN-rate loop drives both itself and
        # the sequencer from a single deadline thread — zero drift, perfect sync.
        # The own thread is a fallback for headless/test use without an arp.
        self._stop        = threading.Event()
        self._using_arp   = False

        try:
            _arp = getattr(eng, 'arp', None)
            if _arp is not None and hasattr(_arp, 'subscribe_ticks'):
                _arp.subscribe_ticks(PULSES_PER_STEP, self._advance_if_running)
                self._using_arp = True
                print("  [seq] unified clock: subscribed to ArpEngine")
        except Exception as _e:
            print(f"  [seq] arp subscribe failed, using own clock: {_e}")

        if not self._using_arp:
            self._clk = threading.Thread(
                target=self._step_loop, daemon=True, name="seq-clock")
            self._clk.start()

        # MIDI clock output thread (always independent)
        self._clk_out = threading.Thread(
            target=self._clock_out_loop, daemon=True, name="seq-clkout")
        self._clk_out.start()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def running(self):
        return self._running

    @property
    def n_tracks(self):
        return len(self.tracks)

    @property
    def clock_out(self):
        return self._clock_out

    # ── Transport ─────────────────────────────────────────────────────────────

    def play(self):
        """Start sequencer. Wind back one step so the first advance lands on 0."""
        with self._track_lock:
            for t in self.tracks:
                t["pos"] = t["length"] - 1
        self._ext_pulse   = 0
        self._global_step = -1     # first advance = step 0 = bar downbeat

        # Reset the arp's phase so the arp step counter and the subscriber
        # counter that drives THIS sequencer both start from 0 at the same
        # PPQN pulse.  Without this, the arp and drums fire at random phase
        # offsets relative to each other (up to one full arp step late/early)
        # every time play is pressed.
        if self._using_arp:
            try:
                arp = getattr(self._eng, 'arp', None)
                if arp is not None and hasattr(arp, 'reset_phase'):
                    arp.reset_phase()
            except Exception:
                pass

        self._running     = True
        if self._clock_out:
            self._send_rt("start")

        # ── Instant downbeat (2026-07-27) ────────────────────────────────────
        # Under the unified clock the seq advances only on the arp's PPQN tick
        # subscriber, so without this the first step would wait 6 pulses (one
        # 16th-note, ~125ms @120 / ~165ms @90) before the downbeat sounds — the
        # "starts late" that made the ORC impossible to start in time with a rig.
        # Fire the downbeat NOW: this manual _advance() moves every track
        # length-1 -> 0 and triggers step 0 at the press instant; the subscriber
        # counter is still 0 (reset_phase above zeroed it), so the NEXT
        # clock-driven advance lands on step 1 exactly one 16th later — grid
        # stays locked, zero drift.  bar_cb() (skipback ring anchor) also fires
        # here, marking the downbeat at the true press.
        # ONLY on the arp path: the fallback _step_loop already fires _advance()
        # immediately when _running goes true, so doing it here too would double.
        if self._using_arp:
            try:
                self._advance()               # seq downbeat at the press
                arp = getattr(self._eng, 'arp', None)
                if arp is not None and hasattr(arp, 'fire_downbeat_now'):
                    arp.fire_downbeat_now()   # arp downbeat at the press too
            except Exception:
                pass

    def stop(self, reset=False):
        """Stop. reset=True rewinds all tracks to step 0."""
        self._running = False
        if self._clock_out:
            self._send_rt("stop")
        self._all_off()
        if reset:
            with self._track_lock:
                for t in self.tracks:
                    t["pos"] = 0

    # ── External clock ────────────────────────────────────────────────────────

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_note_on(self, note, vel):
        """Call on every incoming note-on while record_mode is True.
        The note is held until record_note_off clears it; on each step
        advance, whatever note is currently held gets written to the track."""
        self._rec_note = (int(note), int(vel))

    def record_note_off(self, note):
        """Release the held recording note."""
        if self._rec_note and self._rec_note[0] == int(note):
            self._rec_note = None

    def _commit_record(self, trk):
        """Write the currently held note (if any) into trk at its current pos.
        Called from _advance, inside the track lock, AFTER the pos advance."""
        if not self.record_mode:
            return
        p  = trk["pos"]
        sn = trk.setdefault("step_notes", [None] * MAX_LEN)
        if self._rec_note:
            note, vel     = self._rec_note
            trk["steps"][p] = True
            sn[p]           = note
            # Optionally override per-step velocity someday; for now use track vel
        # Overdub: don't touch steps where nothing was played

    def _advance_if_running(self):
        """Callback fired by ArpEngine tick subscriber on each PPQN pulse group.
        Kept fast — no lock, just a flag check before handing off."""
        if not self._stop.is_set() and self._running:
            self._advance()

    def clock_tick(self):
        """External MIDI clock pulse. No-op when unified with ArpEngine —
        the arp fires _advance_if_running via subscription from its own
        clock_tick. Kept for backward compatibility and standalone use."""
        if self._using_arp:
            return
        try:
            from orc1_arp import CLOCK_EXTERNAL
            arp = self._eng.arp
        except Exception:
            return
        if not arp or arp.clock_mode != CLOCK_EXTERNAL or not self._running:
            return
        self._ext_pulse += 1
        if self._ext_pulse >= PULSES_PER_STEP:
            self._ext_pulse = 0
            self._advance()

    # ── Track management ──────────────────────────────────────────────────────

    def insert_track(self, idx, track_dict):
        """Insert a pre-built track dict at idx. Returns actual index inserted."""
        if self.n_tracks >= MAX_TRACKS:
            return None
        idx = max(0, min(idx, self.n_tracks))
        with self._track_lock:
            self.tracks.insert(idx, track_dict)
        return idx

    def remove_track(self, idx):
        """Remove the track at idx."""
        removed = False
        with self._track_lock:
            if 0 <= idx < self.n_tracks:
                self.tracks.pop(idx)
                removed = True
        if removed:
            self._persist_colors()   # keep the colour overlay a faithful mirror

    def add_track(self, after_idx=None, color=None, **overrides):
        """Build and insert a new track. 2026-07-01 ownership refactor —
        relocated from FireHandler._add_track so the web UI's SEQ_ADD (and
        any future non-Fire front-end) can create a track with zero Fire
        dependency (Bug A). Generic defaults below are a melodic "note"
        track; a front-end can override any field (Fire passes its own
        channel/note/color conventions via **overrides / color=).
        Returns the inserted index, or None if MAX_TRACKS is already hit."""
        if self.n_tracks >= MAX_TRACKS:
            return None
        with self._track_lock:
            n = len(self.tracks)
            palette = self._track_palette or DEFAULT_TRACK_PALETTE
            new = {
                "name":   overrides.get("name", f"track{n+1}"),
                "note":   overrides.get("note", 60),
                "ch":     overrides.get("ch", 1),
                "vel":    overrides.get("vel", 100),
                "dest":   overrides.get("dest", "synth"),
                "color":  tuple(color) if color else palette[n % len(palette)],
                "bright": max(0, min(127, int(overrides.get("bright", 127)))),
                "length": overrides.get("length", 16),
                "steps":  [False] * MAX_LEN,
                "step_vels": [None] * MAX_LEN,
                "step_notes": [None] * MAX_LEN,
                "mode":   overrides.get("mode", "note"),
                "pos":    0,
                "mute":   False,
            }
            insert_at = n if after_idx is None else max(0, min(after_idx + 1, n))
            self.tracks.insert(insert_at, new)
        self._persist_colors()       # keep the colour overlay a faithful mirror
        return insert_at

    def reorder_track(self, idx, new_idx):
        """Swap the tracks at idx and new_idx (clamped to valid range).
        Returns the clamped new_idx actually used, or None if idx was
        already out of range / no-op."""
        with self._track_lock:
            n = self.n_tracks
            if not (0 <= idx < n):
                return None
            new_idx = max(0, min(n - 1, new_idx))
            if new_idx == idx:
                return idx
            self.tracks[idx], self.tracks[new_idx] = self.tracks[new_idx], self.tracks[idx]
        self._persist_colors()       # order changed -> re-mirror so colours track
        return new_idx

    # ── Per-track colour + brightness persistence (instant, cosmetic overlay) ──
    # Both colour AND brightness are cosmetic (not pattern content), so they share
    # one instant-persist overlay file, decoupled from SEQ_SAVE. Brightness (0-127,
    # 127 = full) is a render multiplier the web grid and the Fire's _track_color
    # apply to the pristine stored colour.
    def _apply_colors_overlay(self):
        """Overlay the saved per-track colours AND brightness onto the freshly
        built tracks. Best-effort and index-aligned: applies min(len) entries so a
        stale or differently-sized file can never crash or grow the track list.
        Absent file / missing key -> no-op for that field (tracks keep their
        pattern/palette colour and full brightness)."""
        try:
            with open(self._colors_path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        cols = data.get("colors")
        bris = data.get("bright")
        with self._track_lock:
            if isinstance(cols, list):
                for i, c in enumerate(cols):
                    if i >= len(self.tracks):
                        break
                    try:
                        ch3 = [max(0, min(127, int(x))) for x in c]
                        if len(ch3) == 3:
                            self.tracks[i]["color"] = tuple(ch3)
                    except (ValueError, TypeError):
                        pass
            if isinstance(bris, list):
                for i, b in enumerate(bris):
                    if i >= len(self.tracks):
                        break
                    try:
                        self.tracks[i]["bright"] = max(0, min(127, int(b)))
                    except (ValueError, TypeError):
                        pass

    def _persist_colors(self):
        """Rewrite the cosmetic overlay file as a faithful mirror of the current
        tracks' colours AND brightness, in current order. Called on every colour/
        brightness change and on structural changes (add/remove/reorder) so index
        alignment holds across a restart. Atomic (tmp + os.replace); failures are
        swallowed -- a cosmetic that fails to persist is never a crash."""
        with self._track_lock:
            cols = [list(t.get("color", (0, 0, 0))) for t in self.tracks]
            bris = [int(t.get("bright", 127)) for t in self.tracks]
        try:
            tmp = self._colors_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"colors": cols, "bright": bris}, f)
            os.replace(tmp, self._colors_path)
        except OSError:
            pass

    def set_track_prop(self, idx, prop, value):
        """Set one track property, with the same validation/clamping the
        old direct-write call sites used (relocated, not redesigned).
        prop: one of note / ch / vel / length / mode / name / color / bright."""
        persist_colors = False
        with self._track_lock:
            if not (0 <= idx < self.n_tracks):
                return
            t = self.tracks[idx]
            # -1 is a valid value for chord-mode tracks (BASS tone index in
            # _CHORD_TONE_ORDER) -- clamp floor is -1, not 0, so that mode
            # isn't silently corrupted when routed through this generic setter.
            if   prop == "note":   t["note"]   = max(-1,  min(127, int(value)))
            elif prop == "ch":     t["ch"]     = max(1,   min(16,  int(value)))
            elif prop == "vel":    t["vel"]    = max(1,   min(127, int(value)))
            elif prop == "length": t["length"] = max(1,   min(MAX_LEN, int(value)))
            elif prop == "mode":   t["mode"]   = value if value in ("note", "chord") else "note"
            elif prop == "name":   t["name"]   = str(value)[:12]
            elif prop == "color":
                # value is a "r,g,b" string, each channel on the stored 0-127
                # scale (the web display doubles it; the Fire uses it as-is for
                # 7-bit pad RGB). Parse leniently; ignore a malformed value
                # rather than corrupting the track's colour.
                try:
                    ch3 = [max(0, min(127, int(x))) for x in str(value).split(",")]
                    if len(ch3) == 3:
                        t["color"] = tuple(ch3)
                        persist_colors = True
                except (ValueError, TypeError):
                    pass
            elif prop == "bright":
                # Cosmetic render multiplier, 0-127 (127 = full). Persisted in the
                # SAME instant overlay as colour; a malformed value is ignored
                # rather than corrupting the track's brightness.
                try:
                    t["bright"] = max(0, min(127, int(value)))
                    persist_colors = True
                except (ValueError, TypeError):
                    pass
        # Persist OUTSIDE the lock -- _persist_colors takes it itself, and
        # threading.Lock is not re-entrant (nesting would deadlock).
        if persist_colors:
            self._persist_colors()
        return

    def set_track_mute(self, idx, mute=None):
        """Set (or toggle, if mute is None) one track's mute flag."""
        with self._track_lock:
            if not (0 <= idx < self.n_tracks):
                return
            t = self.tracks[idx]
            t["mute"] = (not t["mute"]) if mute is None else bool(mute)

    def solo_track(self, idx):
        """Mute every track except idx (one atomic pass under the lock —
        avoids the momentary inconsistent state N separate set_track_mute
        calls could show a concurrent reader/renderer)."""
        with self._track_lock:
            if not (0 <= idx < self.n_tracks):
                return
            for i, t in enumerate(self.tracks):
                t["mute"] = (i != idx)

    def unmute_all(self):
        """Clear mute on every track (one atomic pass)."""
        with self._track_lock:
            for t in self.tracks:
                t["mute"] = False

    def set_step_note(self, tidx, spos, note):
        """Set (note is not None) or clear (note is None) a per-step pitch
        override. Setting a note also turns the step on, matching the old
        _edit_step_note behavior ("dialling a note implies intent to play
        it"). spos must already be within the track's current length."""
        with self._track_lock:
            if not (0 <= tidx < self.n_tracks):
                return
            t  = self.tracks[tidx]
            sn = t.setdefault("step_notes", [None] * MAX_LEN)
            if not (0 <= spos < len(sn)):
                return
            sn[spos] = None if note is None else int(note)
            if note is not None and spos < t["length"]:
                t["steps"][spos] = True

    # ── Step editing ──────────────────────────────────────────────────────────

    def toggle_step(self, tidx, step):
        with self._track_lock:
            if 0 <= tidx < self.n_tracks:
                t = self.tracks[tidx]
                if step < t["length"]:
                    t["steps"][step] = not t["steps"][step]

    def _vel_tiers(self):
        """Configured per-step velocity tiers [ghost, normal, accent].
        Reads SEQ["VEL_TIERS"] via the eng's config, clamped to 3 ints in 1-127;
        falls back to the module default if unset/malformed."""
        raw = _seq_cfg(self._eng).get("VEL_TIERS", SEQ_VEL_TIERS)
        try:
            tiers = [max(1, min(127, int(v))) for v in raw][:3]
        except Exception:
            tiers = list(SEQ_VEL_TIERS)
        if len(tiers) != 3:
            tiers = list(SEQ_VEL_TIERS)
        return tiers

    def cycle_step_vel(self, tidx, step):
        """Cycle one ON step's velocity through the tier ladder:
        None (track default) -> ghost -> normal -> accent -> None.
        No-op on an OFF step (there is nothing to accent yet)."""
        ladder = [None] + self._vel_tiers()
        with self._track_lock:
            if 0 <= tidx < self.n_tracks:
                t = self.tracks[tidx]
                if step < t["length"] and t["steps"][step]:
                    sv  = t.setdefault("step_vels", [None] * MAX_LEN)
                    cur = sv[step]
                    try:
                        i = ladder.index(cur)
                    except ValueError:
                        # An exact velocity that isn't one of the tiers -> start
                        # the ladder from the top (next SHIFT-tap = ghost).
                        i = 0
                    sv[step] = ladder[(i + 1) % len(ladder)]

    def set_step_vel(self, tidx, step, vel):
        """Set (vel is an int 1-127) or clear (vel is None) a step's velocity
        override. Setting a velocity also turns the step on, mirroring
        set_step_note ("dialling a value implies intent to play it")."""
        with self._track_lock:
            if not (0 <= tidx < self.n_tracks):
                return
            t  = self.tracks[tidx]
            sv = t.setdefault("step_vels", [None] * MAX_LEN)
            if not (0 <= step < len(sv)):
                return
            sv[step] = None if vel is None else max(1, min(127, int(vel)))
            if vel is not None and step < t["length"]:
                t["steps"][step] = True

    def clear_steps(self, *tidxs):
        with self._track_lock:
            for tidx in tidxs:
                if 0 <= tidx < self.n_tracks:
                    t = self.tracks[tidx]
                    t["steps"]      = [False] * MAX_LEN
                    t["step_vels"]  = [None]  * MAX_LEN   # clear per-step velocities too
                    t["step_notes"] = [None]  * MAX_LEN   # clear per-step pitches too

    # ── MIDI clock output ─────────────────────────────────────────────────────

    def set_clock_out(self, enabled, ports=None):
        self._clock_out = enabled
        if ports is not None:
            self._clock_ports = list(ports)
        if not enabled:
            self._send_rt("stop")
        elif self._running:
            self._send_rt("start")

    def toggle_clock_out(self):
        self.set_clock_out(not self._clock_out)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _step_loop(self):
        """Drift-free internal step clock using absolute deadlines."""
        try:
            from orc1_arp import CLOCK_EXTERNAL
        except ImportError:
            CLOCK_EXTERNAL = None

        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                arp = self._eng.arp
                ext = bool(arp and CLOCK_EXTERNAL is not None
                           and arp.clock_mode == CLOCK_EXTERNAL)
            except Exception:
                ext = False

            if self._running and not ext:
                bpm      = max(20.0, self._bpm_fn())
                interval = 60.0 / bpm / 4.0   # 16th-note step
                self._advance()
                next_t += interval
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_t = time.monotonic()  # fell behind — resync, don't drift
            else:
                next_t = time.monotonic()
                self._stop.wait(0.01)

    def _clock_out_loop(self):
        """Emit MIDI clock (24 PPQN) while running, using absolute deadlines."""
        try:
            from orc1_arp import CLOCK_EXTERNAL
        except ImportError:
            CLOCK_EXTERNAL = None

        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                arp = self._eng.arp
                ext = bool(arp and CLOCK_EXTERNAL is not None
                           and arp.clock_mode == CLOCK_EXTERNAL)
            except Exception:
                ext = False

            if self._clock_out and self._running and not ext and mido:
                bpm      = max(20.0, self._bpm_fn())
                interval = 60.0 / bpm / 24.0   # 24 PPQN
                self._send_rt("clock")
                next_t += interval
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_t = time.monotonic()
            else:
                next_t = time.monotonic()
                self._stop.wait(0.005)

    def bar_phase(self, steps_per_bar=16):
        """(seconds_since_last_barline, bar_length_seconds) from the running
        sequencer grid, or None when stopped. This is the PRIMARY quantize
        anchor for sampler bar-grabs — when the seq is playing, its downbeat
        IS the music's downbeat."""
        if not self._running or self._step_wall is None or self._global_step < 0:
            return None
        bpm = max(20.0, self._bpm_fn())
        step_s = 60.0 / bpm / 4.0                     # 16th-note step
        idx    = self._global_step % max(1, int(steps_per_bar))
        since  = idx * step_s + (time.monotonic() - self._step_wall)
        bar_s  = steps_per_bar * step_s
        return (min(since, bar_s), bar_s)

    def _advance(self):
        """Advance all tracks one step and fire triggers."""
        self._global_step += 1
        self._step_wall    = time.monotonic()
        # Downbeat step -> audio-domain anchor mark (same index math as
        # bar_phase(): global_step % steps_per_bar == 0 is the bar's "1").
        if self.bar_cb is not None and \
           self._global_step % max(1, self.bar_cb_steps) == 0:
            try:
                self.bar_cb()
            except Exception:
                pass
        self._flush_off()
        with self._track_lock:
            for idx, trk in enumerate(self.tracks):
                trk["pos"] = (trk["pos"] + 1) % trk["length"]
                # Live recording: write held note into this track if it's the record target
                if self.record_mode and idx == self.record_track:
                    self._commit_record(trk)
                if trk["mute"]:
                    continue
                p = trk["pos"]
                if trk["steps"][p]:
                    sv   = trk.get("step_vels")
                    ov   = sv[p] if sv and sv[p] is not None else None
                    vel  = min(127, max(1, int(ov if ov is not None else trk["vel"])))
                    sn   = trk.get("step_notes")
                    note = (sn[p] if sn and sn[p] is not None else trk["note"])
                    self._trigger(trk, vel, note)
        if self.step_callback:
            try:
                self.step_callback()
            except Exception:
                pass

    def _trigger(self, trk, vel, note=None):
        if note is None:
            note = trk["note"]
        if not mido:
            return

        # Chord-aware mode: resolve tone index via current arp chord.
        # tone -1 = bass (root minus one octave), 0-4 = chord degrees.
        # If no chord is playing yet, the step is silently skipped.
        if trk.get("mode") == "chord":
            tone = int(note)
            try:
                arp   = getattr(self._eng, "arp", None)
                chord = list(arp.current_chord) if arp and arp.current_chord else []
                if not chord:
                    return
                if tone == -1:
                    note = max(0, chord[0] - 12)
                else:
                    note = chord[tone % len(chord)]
            except Exception:
                return

        note = int(note)
        dest = trk["dest"]
        if dest == "synth":
            ch0 = int(trk["ch"]) - 1
            try:
                self._eng.send_synth_midi(
                    mido.Message("note_on", channel=ch0,
                                 note=note, velocity=int(vel)))
            except Exception:
                return
            self._last_on.append((note, ch0, "synth"))
        elif isinstance(dest, dict):
            pk  = dest.get("port")
            ch0 = int(dest.get("ch", 1)) - 1
            try:
                self._eng.note_on_out(note, int(vel), pk, ch0 + 1)
            except Exception:
                return
            self._last_on.append((note, ch0, pk))

    def _flush_off(self):
        if not mido:
            self._last_on = []
            return
        for note, ch0, dest in self._last_on:
            try:
                if dest == "synth":
                    self._eng.send_synth_midi(
                        mido.Message("note_off", channel=ch0,
                                     note=note, velocity=0))
                else:
                    self._eng.note_off_out(note, dest, ch0 + 1)
            except Exception:
                pass
        self._last_on = []

    def _all_off(self):
        self._flush_off()

    def _send_rt(self, msg_type):
        """Send a system-realtime message to all configured clock ports."""
        if not mido:
            return
        send = getattr(self._eng, "send_to", None)
        if not send:
            return
        try:
            msg = mido.Message(msg_type)
        except Exception:
            return
        for port in self._clock_ports:
            try:
                send(port, msg)
            except Exception:
                pass

    def close(self):
        self._stop.set()
        self._all_off()

    def save(self, path=None):
        """Manually save current sequencer state to disk.
        Called explicitly — never auto-saved — so you control when a good
        pattern gets committed.  The file is loaded automatically on restart."""
        path = path or SEQ_STATE_PATH
        with self._track_lock:
            snap = []
            for t in self.tracks:
                snap.append({
                    "name":       t.get("name",  "trk"),
                    "ch":         int(t.get("ch",    6)),
                    "note":       int(t.get("note", 36)),
                    "mode":       t.get("mode",  "note"),
                    "vel":        int(t.get("vel",  100)),
                    "length":     int(t.get("length", 16)),
                    "color":      list(t.get("color", [80, 0, 0])),
                    "bright":     int(t.get("bright", 127)),
                    "dest":       t.get("dest", "synth"),
                    "mute":       bool(t.get("mute", False)),
                    "steps":      list(t.get("steps",      [False]*MAX_LEN)),
                    "step_vels":  list(t.get("step_vels",  [None] *MAX_LEN)),
                    "step_notes": list(t.get("step_notes", [None] *MAX_LEN)),
                })
        data = {
            "version":  1,
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "tracks":   snap,
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            print(f"[seq] saved {len(snap)} track(s) → {path}")
            return True
        except Exception as e:
            print(f"[seq] save error: {e}")
            return False
