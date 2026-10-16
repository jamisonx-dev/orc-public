# ═══════════════════════════════════════════════════════════════════════════════
# ORC-1 ARP ENGINE
# Arpeggiator — clock, step sequencer, chord queue management.
# Imported by orc1.py. Do not run directly.
# ═══════════════════════════════════════════════════════════════════════════════

import random
import time
import threading
import mido

# ── MIDI clock constants ──────────────────────────────────────────────────────
PPQN = 24   # MIDI clock pulses per quarter note (standard)

# Subdivision definitions — (pulses_per_step, display_name)
SUBDIVISIONS = {
    "1/4":    (24,  "1/4"),
    "1/4.":   (36,  "1/4."),
    "1/4t":   (16,  "1/4t"),
    "1/8":    (12,  "1/8"),
    "1/8.":   (18,  "1/8."),
    "1/8t":   (8,   "1/8t"),
    "1/16":   (6,   "1/16"),
    "1/16.":  (9,   "1/16."),
    "1/16t":  (4,   "1/16t"),
    "1/32":   (3,   "1/32"),
    "1/32t":  (2,   "1/32t"),
}

# Ordered list for CC-sweep selection (index 0-10 mapped across 0-127)
SUBDIVISION_LIST = [
    "1/32t", "1/32", "1/16t", "1/16.", "1/16",
    "1/8t",  "1/8.", "1/8",   "1/4t",  "1/4.", "1/4"
]

# Clock modes
CLOCK_EXTERNAL = "external"
CLOCK_INTERNAL = "internal"
CLOCK_DRIFT    = "drift"

# What voice_resolve_fn returns for "this tone is DELIBERATELY silent" -- an
# ARP_DEST that is set but whose module is unbound (or a voice-map ARP_DEST of
# None). Distinct from (None, None) = "no opinion", which falls back to
# CHORD_ALL; without it a pinned-but-unbound arp leaked onto CHORD_ALL's voice
# (review, 2026-10-01).
ARP_SILENT = ("<silent>", None)


class ArpEngine:
    """
    Arpeggiator engine. Runs independently of the chord engine.
    Call set_chord() whenever a new chord fires.
    Call clock_tick() for each incoming MIDI clock pulse (external mode).
    """

    def __init__(self, note_on_fn, note_off_fn, all_off_fn, debug_fn=None):
        """
        note_on_fn(note, vel, port_key, ch)   — send a note on
        note_off_fn(note, port_key, ch)        — send a note off
        all_off_fn()                           — panic
        debug_fn(msg)                          — optional debug print
        """
        self.note_on    = note_on_fn
        self.note_off   = note_off_fn
        self.all_off    = all_off_fn
        self.dbg        = debug_fn or (lambda m: None)

        # ── State ─────────────────────────────────────────────────────────────
        self.enabled       = False
        self.clock_mode    = CLOCK_INTERNAL
        self.subdivision   = "1/8"
        self.internal_bpm  = 120.0
        self.drift_bpm     = 120.0
        self.humanize      = 0.0      # 0.0-1.0, max ~20ms timing jitter
        self.gate_trim     = 1.0      # global gate multiplier
        self.swing_amount  = 0.0      # 0.0-0.5, overrides pattern swing if >0

        # ── Pattern state ─────────────────────────────────────────────────────
        self.pattern_name  = "UP"
        self.pattern       = None     # current pattern dict
        self.step_idx      = 0        # current step index
        self.pending_chord = None     # queued chord waiting for next step
        self.current_chord = None     # chord currently being arpeggiated
        self.current_vel   = 100      # velocity from triggering note
        self.mirror_ch     = None     # mirror channel from triggering note

        # Voice routing callback — set by orc1.py
        # resolve_fn(voice_slot, mirror_ch) → (port_key, channel)
        self.resolve_fn    = None

        # ── Held notes tracker ────────────────────────────────────────────────
        self._held = []   # list of (note, port_key, ch) currently sounding

        # ── External clock ────────────────────────────────────────────────────
        self._ext_pulse_count  = 0
        self._ext_last_pulse   = None
        self._ext_pulse_times  = []   # rolling window for BPM estimate
        self._pulses_per_step  = SUBDIVISIONS[self.subdivision][0]
        self._lock             = threading.Lock()

        # ── Internal pulse counter (counts PPQN pulses toward each step) ─────
        self._int_pulse_count  = 0

        # ── Bar-phase anchor (2026-07-07, for quantized sampler grabs) ───────
        # Every PPQN pulse (either clock) bumps a running total and stamps a
        # monotonic wall time; bar_phase() turns that into "seconds since the
        # last barline". Used as the FALLBACK grid when the sequencer isn't
        # running (the seq's own bar_phase() wins when it is).
        self._pulse_total     = 0
        self._last_pulse_wall = None

        # ── Audio-domain downbeat hook (2026-07-20, Bug-1 clock-drift fix) ───
        # When set (by orc1.py), bar_cb is called on the pulse that IS a
        # downbeat of the sampler's bar grid, so the engine can snapshot the
        # skipback ring's write head at that exact moment. Grabs then measure
        # "since the barline" on the AUDIO clock instead of this monotonic
        # one, which drifts against the soundcard (~10ms/min, measured).
        self.bar_cb        = None       # callable, must never block
        self.bar_cb_pulses = PPQN * 4   # pulses per bar (orc1.py overrides)

        # ── Tick subscribers — called on every PPQN pulse from either clock ──
        # Each entry: [pulses_per_call, current_count, callback]
        # subscribe_ticks(6, fn) → fn called at 16th-note rate (matches seq step)
        self._tick_subscribers = []

        # ── Internal clock thread ─────────────────────────────────────────────
        self._internal_thread  = None
        self._stop_internal    = threading.Event()
        self._step_event       = threading.Event()  # fired to advance one step

        self._start_internal_clock()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_pattern(self, name, patterns_dict):
        """Switch to a named pattern. Takes effect on next step."""
        if name in patterns_dict:
            with self._lock:
                self.pattern_name = name
                self.pattern      = patterns_dict[name]
            self.dbg(f"ARP pattern → {name}")

    def set_chord(self, notes, vel, mirror_ch=None):
        """
        Queue a new chord. Transition happens on next step boundary.
        notes    — sorted list of MIDI note numbers from chord engine
        vel      — velocity to use
        mirror_ch — mirror channel (None for fixed preset routing)
        """
        with self._lock:
            self.pending_chord = list(notes)
            self.current_vel   = vel
            self.mirror_ch     = mirror_ch

    def set_enabled(self, on):
        with self._lock:
            self.enabled = on
        if not on:
            self._release_all_held()

    def clear_chord(self):
        """Stop the arp by discarding the current and pending chord.
        Called when arp_hold is OFF and the trigger note is released.
        The arp stays enabled — it will resume if a new chord is fed in.
        Thread-safe: can be called from the MIDI input thread."""
        with self._lock:
            self.pending_chord = None
            self.current_chord = None
        self._release_all_held()

    def set_subdivision(self, name):
        if name in SUBDIVISIONS:
            with self._lock:
                self.subdivision      = name
                self._pulses_per_step = SUBDIVISIONS[name][0]
            self.dbg(f"ARP rate → {name}")

    def set_subdivision_by_index(self, idx):
        idx = max(0, min(idx, len(SUBDIVISION_LIST)-1))
        self.set_subdivision(SUBDIVISION_LIST[idx])

    def set_clock_mode(self, mode):
        with self._lock:
            self.clock_mode = mode
        self.dbg(f"ARP clock → {mode}")
        if mode in (CLOCK_INTERNAL, CLOCK_DRIFT):
            self._restart_internal_clock()

    def set_internal_bpm(self, bpm):
        bpm = max(20.0, min(300.0, bpm))
        with self._lock:
            self.internal_bpm = bpm
        self.dbg(f"ARP BPM → {bpm:.1f}")

    def set_drift_bpm(self, bpm):
        bpm = max(20.0, min(300.0, bpm))
        with self._lock:
            self.drift_bpm = bpm

    def set_humanize(self, amount):
        """amount: 0.0-1.0"""
        with self._lock:
            self.humanize = max(0.0, min(1.0, amount))

    def set_gate_trim(self, trim):
        """trim: 0.1-2.0 multiplier on all gate values"""
        with self._lock:
            self.gate_trim = max(0.1, min(2.0, trim))

    def set_swing(self, amount):
        """amount: 0.0-0.5. Overrides pattern swing if >0."""
        with self._lock:
            self.swing_amount = max(0.0, min(0.5, amount))

    def subscribe_ticks(self, pulses_per_call, callback):
        """Register a callback fired every N PPQN pulses from either clock.
        Runs on the clock thread — keep callbacks fast.
        subscribe_ticks(6, fn)  → fn called at 16th-note rate
        subscribe_ticks(12, fn) → fn called at 8th-note rate
        etc."""
        self._tick_subscribers.append([int(pulses_per_call), 0, callback])

    def clock_tick(self):
        """Call this for every incoming MIDI clock pulse (external mode)."""
        if self.clock_mode != CLOCK_EXTERNAL:
            return

        now = time.perf_counter()
        with self._lock:
            # Track pulse timing for BPM display
            if self._ext_last_pulse is not None:
                interval = now - self._ext_last_pulse
                self._ext_pulse_times.append(interval)
                if len(self._ext_pulse_times) > 24:
                    self._ext_pulse_times.pop(0)
            self._ext_last_pulse = now

            self._ext_pulse_count += 1
            if self._ext_pulse_count >= self._pulses_per_step:
                self._ext_pulse_count = 0
                fire = True
            else:
                fire = False

        if fire:
            self._advance_step()

        # Fire tick subscribers on every PPQN pulse regardless of arp step
        self._fire_pulse(internal=False)

    def get_status(self):
        """Return a status string for display."""
        with self._lock:
            bpm = self._estimated_bpm()
            return (f"ARP:{'ON' if self.enabled else 'OFF'}  "
                    f"Pattern:{self.pattern_name}  "
                    f"Rate:{self.subdivision}  "
                    f"Clock:{self.clock_mode}  "
                    f"BPM:{bpm:.1f}  "
                    f"Step:{self.step_idx}")

    # ── Internal clock ────────────────────────────────────────────────────────

    def _step_interval_seconds(self):
        """Return seconds per step for current mode and subdivision.

        In EXTERNAL mode this uses the BPM estimated from incoming clock
        pulses, so gate durations and swing delays track the master clock
        instead of the (possibly very different) internal/drift BPM."""
        pps = SUBDIVISIONS[self.subdivision][0]  # pulses per step
        if self.clock_mode == CLOCK_DRIFT:
            bpm = self.drift_bpm
        elif self.clock_mode == CLOCK_EXTERNAL:
            bpm = self._estimated_bpm()   # falls back to internal_bpm if no pulses yet
        else:
            bpm = self.internal_bpm
        if bpm <= 0:
            bpm = self.internal_bpm or 120.0
        # seconds per pulse = 60 / (bpm * PPQN)
        return (60.0 / (bpm * PPQN)) * pps

    def _start_internal_clock(self):
        self._stop_internal.clear()
        # Per-thread stop event: a restart signals the OLD thread's event
        # and hands the new thread a fresh one. Previously the shared
        # event was set then cleared 50ms later -- an old thread mid-sleep
        # (up to seconds at low BPM) woke to find it cleared and kept
        # running, leaving two clock threads and a double-rate arp.
        stop = threading.Event()
        self._clock_stop = stop
        self._internal_thread = threading.Thread(
            target=self._internal_clock_loop, args=(stop,), daemon=True)
        self._internal_thread.start()

    def reset_phase(self):
        """Reset all pulse counters and subscriber counters to 0 without
        restarting the clock thread.  Call this whenever the sequencer starts
        (seq.play()) so the arp step phase and the drum-step phase are locked
        to the same PPQN pulse — the first drum hit and the first arp note
        both fire exactly N pulses from now instead of at random offsets.
        Also resets step_idx so the arp pattern restarts from the top.
        Thread-safe: only touches counters, never touches the clock thread."""
        # 6 = PULSES_PER_STEP from orc1_seq (24 PPQN / 4 = one 16th-note step).
        # Subscribers (including the sequencer) each fire 6 pulses after reset,
        # so the first drum hit lands at pulse 6.  Pre-loading _int_pulse_count
        # to (_pulses_per_step - 6) means the arp also fires at pulse 6 and the
        # two are exactly in phase.  Clamped to 0 for subdivisions faster than
        # 1/16 (pps <= 6) where the arp fires multiple times per seq step anyway.
        SEQ_STEP_PULSES = 6
        with self._lock:
            pps = self._pulses_per_step
            self._int_pulse_count = max(0, pps - SEQ_STEP_PULSES)
            self._ext_pulse_count = max(0, pps - SEQ_STEP_PULSES)
            self.step_idx         = 0
            self._pulse_total     = 0
            self._last_pulse_wall = None
            for sub in self._tick_subscribers:
                sub[1] = 0

    def fire_downbeat_now(self):
        """Fire the arp's downbeat (pattern step 0) immediately — called from
        Sequencer.play() so the arp's first note lands at the PLAY press instead
        of N pulses later (matches the seq's instant downbeat, 2026-07-27).

        Must be called RIGHT AFTER reset_phase() (which set step_idx=0 and
        pre-loaded the pulse counters).  We override those counters to 0 so the
        NEXT pulse-driven arp step fires a full _pulses_per_step later — without
        this, reset_phase's (pps-6) pre-load would make the second arp step come
        only 6 pulses (one 16th) after this one, double-timing it.

        Safe no-op if the arp is disabled or has no chord (_advance_step returns
        early).  Called WITHOUT holding self._lock — _advance_step takes it
        (threading.Lock is non-reentrant), so we release before calling it."""
        with self._lock:
            self.step_idx         = 0
            self._int_pulse_count = 0
            self._ext_pulse_count = 0
        self._advance_step()   # fires pattern step 0 now (step_idx -> 1)

    def _restart_internal_clock(self):
        old = getattr(self, "_clock_stop", None)
        if old is not None:
            old.set()
            old.wait(0.06)
        # Reset internal pulse counters so everything re-aligns on restart
        self._int_pulse_count = 0
        self._pulse_total     = 0
        for sub in self._tick_subscribers:
            sub[1] = 0
        self._start_internal_clock()

    def _internal_clock_loop(self, stop):
        """Single PPQN-rate deadline loop — drives both arp steps and tick subscribers.
        Firing at pulse rate (24× per quarter note) instead of step rate means the
        arp and anything subscribed via subscribe_ticks() share a single clock thread
        with zero drift between them."""
        next_t = time.monotonic()
        while not (self._stop_internal.is_set() or stop.is_set()):
            with self._lock:
                mode = self.clock_mode
            if mode in (CLOCK_INTERNAL, CLOCK_DRIFT):
                bpm  = self.drift_bpm if mode == CLOCK_DRIFT else self.internal_bpm
                bpm  = max(20.0, bpm)
                # One PPQN pulse interval
                pulse_interval = 60.0 / (bpm * PPQN)
                next_t += pulse_interval
                delay = next_t - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
                else:
                    next_t = time.monotonic()   # fell behind — resync, don't drift
                if not (self._stop_internal.is_set() or stop.is_set()):
                    self._fire_pulse(internal=True)
            else:
                next_t = time.monotonic()
                stop.wait(0.01)

    def _fire_pulse(self, internal=False):
        """One PPQN-rate pulse. Counts toward the arp's own step AND fires subscribers.
        Called from the internal clock thread (internal=True) or clock_tick() (external).

        IMPORTANT — subscriber order:
          Tick subscribers (e.g. the step sequencer) fire FIRST, then the arp step
          advances.  This matters because _advance_step() sleeps inside the clock thread
          when swing > 0.  If subscribers fired after that sleep, the sequencer would
          inherit the arp's swing timing unintentionally.  Firing first keeps the
          sequencer locked to the raw PPQN grid regardless of arp swing settings.
        """
        # ── Bar-phase anchor (see __init__) ──────────────────────────────────
        self._pulse_total    += 1
        self._last_pulse_wall = time.monotonic()
        # Downbeat pulse -> let the engine snapshot the ring head (audio-domain
        # anchor for quantized grabs). Same index math as bar_phase(): the
        # pulse numbered (total-1) % pulses_per_bar == 0 is the bar's "1".
        if self.bar_cb is not None and \
           (self._pulse_total - 1) % max(1, self.bar_cb_pulses) == 0:
            try:
                self.bar_cb()
            except Exception:
                pass

        # ── Tick subscribers first (unaffected by arp swing sleep) ───────────
        for sub in self._tick_subscribers:
            sub[1] += 1
            if sub[1] >= sub[0]:
                sub[1] = 0
                try:
                    sub[2]()
                except Exception:
                    pass

        # ── Arp step counter (may sleep for swing inside _advance_step) ───────
        if internal:
            with self._lock:
                self._int_pulse_count += 1
                pps      = self._pulses_per_step
                fire_arp = (self._int_pulse_count >= pps)
                if fire_arp:
                    self._int_pulse_count = 0
            if fire_arp:
                self._advance_step()

    # ── Step advancement ──────────────────────────────────────────────────────

    def _advance_step(self):
        """Fire the next arp step. Called from clock thread."""
        if not self.enabled:
            return

        with self._lock:
            # Swap in pending chord at step boundary
            if self.pending_chord is not None:
                self.current_chord = self.pending_chord
                self.pending_chord = None
                # Don't reset step_idx — smooth continuation

            if self.current_chord is None or self.pattern is None:
                return

            steps    = self.pattern["steps"]
            if not steps:
                return

            step     = steps[self.step_idx % len(steps)]
            step_idx = self.step_idx
            self.step_idx = (self.step_idx + 1) % len(steps)

            notes      = list(self.current_chord)
            vel        = self.current_vel
            mirror_ch  = self.mirror_ch
            humanize   = self.humanize
            gate_trim  = self.gate_trim
            swing      = self.swing_amount if self.swing_amount > 0 \
                         else self.pattern.get("swing", 0.0)

        # Humanize timing — tiny random delay
        if humanize > 0:
            jitter = random.uniform(0, humanize * 0.02)  # max ~20ms
            time.sleep(jitter)

        # Swing — delay even steps
        if swing > 0 and step_idx % 2 == 1:
            interval = self._step_interval_seconds()
            time.sleep(interval * swing)

        self._play_step(step, notes, vel, mirror_ch, gate_trim)

    def _play_step(self, step, chord_notes, base_vel, mirror_ch, gate_trim):
        """Execute one arp step."""
        if step["rest"]:
            return

        if step["tie"]:
            # Tie — don't attack. If the previous step had gate > 1.0, its notes
            # are still sounding; this step lets them continue without a new hit.
            # Only musically useful when the preceding step has gate > 1.0.
            return

        # Probabilistic step — treat as rest if random roll misses
        prob = step.get("prob", 1.0)
        if prob < 1.0 and random.random() > prob:
            return

        # Release previously held notes
        self._release_all_held()

        # Resolve tones to actual MIDI notes
        oct_offset = step.get("oct", 0)
        step_vel   = max(1, min(127, int(base_vel * step.get("vel", 1.0))))
        gate       = max(0.05, min(2.0, step.get("gate", 0.8) * gate_trim))
        tones      = step.get("tones", [0])

        notes_to_play = []
        for tone_idx in tones:
            note = self._tone_to_note(tone_idx, oct_offset, chord_notes)
            if note is not None:
                # Keep tone_idx alongside the note so _get_output_route can
                # pick the matching voice slot (CHORD_ROOT, CHORD_3RD, etc.)
                # for each tone — enabling NES multi-voice arp distribution.
                notes_to_play.append((tone_idx, note))

        if not notes_to_play:
            return

        # Play notes — each tone may route to a different voice slot
        held_this_step = []
        for tone_idx, note in notes_to_play:
            pk, ch = self._get_output_route(mirror_ch, tone_idx=tone_idx)
            if not pk or not ch:
                continue
            self.note_on(note, step_vel, pk, ch)
            held_this_step.append((note, pk, ch))

        if not held_this_step:
            return

        with self._lock:
            self._held = held_this_step

        # Schedule note-off after gate duration
        step_dur = self._step_interval_seconds()
        gate_dur = step_dur * gate

        def release():
            time.sleep(gate_dur)
            for (n, p, c) in held_this_step:
                self.note_off(n, p, c)
            # Remove from held if still there
            with self._lock:
                self._held = [x for x in self._held if x not in held_this_step]

        threading.Thread(target=release, daemon=True).start()

    def _release_all_held(self):
        """Immediately release all currently held arp notes."""
        with self._lock:
            held = list(self._held)
            self._held = []
        for (n, pk, ch) in held:
            self.note_off(n, pk, ch)

    def _tone_to_note(self, tone_idx, oct_offset, chord_notes):
        """
        Convert a tone index to a MIDI note number.
        tone_idx 0 = root, 1 = 3rd, 2 = 5th etc.
        tone_idx -1 = bass (one octave below root)
        oct_offset shifts the result by octaves.
        """
        if not chord_notes:
            return None

        if tone_idx == -1:
            # Bass note — one octave below root
            note = chord_notes[0] - 12
        elif tone_idx < 0:
            # Further below — wrap around
            note = chord_notes[0] + (tone_idx * 12)
        else:
            idx  = tone_idx % len(chord_notes)
            note = chord_notes[idx]

        note += oct_offset * 12
        note  = max(0, min(127, note))
        return note

    def _get_output_route(self, mirror_ch, tone_idx=None):
        """Return (port_key, channel) for an arp note.

        When voice_resolve_fn is set (registered by orc1.py after init), it
        is called with tone_idx so each arp tone can be routed to a different
        voice slot.  This is what makes NES arp distribution work:
          tone 0 → CHORD_ROOT → Pulse 1
          tone 1 → CHORD_3RD  → Pulse 2
          tone 2 → CHORD_5TH  → Triangle
        Falls back to CHORD_ALL when voice_resolve_fn is absent or returns None,
        so all existing presets that use CHORD_ALL continue to work unchanged.
        ARP_SILENT from voice_resolve_fn means silent: no CHORD_ALL fallback.
        """
        if tone_idx is not None:
            vfn = getattr(self, "voice_resolve_fn", None)
            if vfn:
                pk, ch = vfn(tone_idx, mirror_ch)
                if (pk, ch) == ARP_SILENT:
                    return None, None
                if pk and ch:
                    return pk, ch
        if self.resolve_fn:
            return self.resolve_fn("CHORD_ALL", mirror_ch)
        return "pisound", 1

    def _estimated_bpm(self):
        """Estimate current BPM from external clock pulses, or return internal.

        Snapshots _ext_pulse_times (it's mutated by the MIDI clock thread);
        callers may or may not hold self._lock, so no locking here."""
        times = list(self._ext_pulse_times)
        if self.clock_mode == CLOCK_EXTERNAL and times:
            avg_interval = sum(times) / len(times)
            if avg_interval > 0:
                return 60.0 / (avg_interval * PPQN)
        if self.clock_mode == CLOCK_DRIFT:
            return self.drift_bpm
        return self.internal_bpm

    def bar_phase(self, beats_per_bar=4):
        """(seconds_since_last_barline, bar_length_seconds) on the PPQN grid,
        or None if no pulse has fired yet. Used by the sampler's quantized
        capture when the sequencer isn't running — the bar '1' is wherever
        the pulse counter says it is (arbitrary but CONSISTENT, so repeated
        grabs land on the same grid). Lock-free reads: a one-pulse-stale
        value costs at most one pulse interval of accuracy."""
        last = self._last_pulse_wall
        total = self._pulse_total
        if last is None or total <= 0:
            return None
        bpm = self._estimated_bpm()
        if not bpm or bpm <= 0:
            return None
        pulse_int = 60.0 / (bpm * PPQN)
        ppb   = PPQN * max(1, int(beats_per_bar))
        idx   = (total - 1) % ppb                     # last-fired pulse in bar
        since = idx * pulse_int + (time.monotonic() - last)
        bar_s = ppb * pulse_int
        return (min(since, bar_s), bar_s)

    def beat_phase(self):
        """(seconds_since_last_beat, beat_seconds) phase-locked to the live
        clock's PPQN grid, or None when no clock is actively running.

        This exists for the metronome LED. Reading _estimated_bpm() and
        free-running a timer smears badly under external clock: the estimate
        wobbles per-pulse (bursty USB clock, jitter), and a modulo against a
        distant phase anchor turns that wobble into wild flicker. Here the beat
        BOUNDARY is anchored to an actual clock pulse (_pulse_total % PPQN == 0),
        so `since` resets on the real beat and jitter in the estimate can only
        scale it by a tiny fraction — it cannot move the boundary. Works for
        both the internal clock (internal pulses) and external sync (incoming
        clock pulses); returns None when the last pulse is stale (clock
        stopped/paused) so the caller can fall back to a free-running tick.
        Lock-free read, same one-pulse staleness contract as bar_phase()."""
        last  = self._last_pulse_wall
        total = self._pulse_total
        if last is None or total <= 0:
            return None
        bpm = self._estimated_bpm()
        if not bpm or bpm <= 0:
            return None
        pulse_int = 60.0 / (bpm * PPQN)
        beat_s    = PPQN * pulse_int
        age       = time.monotonic() - last
        if age > max(0.5, 1.5 * beat_s):      # no pulse for >~1.5 beats -> stopped
            return None
        idx   = (total - 1) % PPQN            # pulses since the last beat boundary
        since = idx * pulse_int + age
        return (min(since, beat_s), beat_s)
