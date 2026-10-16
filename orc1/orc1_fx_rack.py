# ===============================================================================
# ORC-1 FX RACK  (orc1_fx_rack.py)
# A 3-slot, series FX rack built on the same jalv machinery as the preset
# chains.  Instantiated by FXManager in orc1_fx.py as eng.fx.rack.
# ===============================================================================
#
# WHAT THIS IS
#   The old model was "FX chains": hand-written presets in fx_chains.json, each
#   a fixed 1-2 plugin combo, only one really usable at a time, and every one
#   wired as its own send/return loop (so two at once ran in PARALLEL, not in
#   series).  owner wanted the opposite: pick ANY single effect from the full
#   installed Airwindows catalogue, and STACK up to three of them one-into-the-
#   next, with a hard ceiling so the Pi can never run out of DSP and xrun.
#
#   This module is that rack.  Three slots.  Each slot holds one plugin chosen
#   from fx_palette.json (generated from the plugins actually installed).  The
#   enabled slots are wired in true series:
#
#       SEND_L/R → slot0 → slot1 → slot2 → RETURN_L/R
#
#   Disabled or empty slots are simply left out of the chain (their jalv isn't
#   even running, so they cost zero DSP).  There is no 4th slot — the cap is
#   structural, which is the whole point: you physically cannot stack enough to
#   blow the DSP budget.
#
# HOW IT REUSES THE EXISTING ENGINE
#   All the hard, proven parts come from fx_chain_manager:
#     - spawn_jalv()      : the FIFO-stdin + RT-priority jalv spawn
#     - jack_connect/disc : JACK wiring
#     - _get_fx_ports()   : resolves SEND_*/RETURN_* tokens to real ports
#                           (respects ~/.orc1_fx_io.json — internal vs hardware)
#   So this file only owns: slot state, series wiring order, the cap, and the
#   internal-insert dry-path handling.  It never re-implements jalv.
#
# STATE (persisted, human-readable) — ~/.orc1_fx_rack.json
#   {
#     "slots": [
#       {"plugin": "galactic", "enabled": true,  "params": {"replace": 0.4}},
#       {"plugin": "drive",    "enabled": false, "params": {}},
#       {"plugin": null,       "enabled": false, "params": {}}
#     ]
#   }
#   "plugin" is a palette key (short name).  "params" remembers knob positions
#   so a rebuild (e.g. after enabling a slot) restores them.
#
# RUNTIME (in-memory only)
#   self._runtime is the list of jalv processes currently live, one per active
#   slot, each: {"idx", "client", "pid", "fifo", "plugin", "entry"}.  The brain
#   owns these processes; on a brain restart we reap any orphaned fx-rack-*
#   jalv and re-apply the saved desired state from scratch.
#
# THREADING
#   Structural changes (assign / enable / disable / reorder / clear) call
#   apply() which does the teardown+rebuild.  apply() runs on a background
#   thread (jalv takes ~1-4s to register JACK ports — never block the MIDI
#   thread) and is serialised by self._lock so two rapid changes can't
#   interleave their wiring.  Live knob turns go through param(), which is a
#   direct FIFO write and never rebuilds anything.
# ===============================================================================

import json
import os
import signal
import threading
import time

# fx_chain_manager holds the shared jalv/JACK primitives.  orc1.py inserts the
# orc1 folder onto sys.path before importing, so this resolves normally.
try:
    import fx_chain_manager as _fcm
    _FCM_AVAILABLE = True
except ImportError:
    _FCM_AVAILABLE = False

RACK_STATE_FILE  = os.path.expanduser("~/.orc1_fx_rack.json")
# The brain writes this on every rebuild so the (separate) web process can see
# which slots are actually live — the cmd socket only acks, it returns no data.
RACK_STATUS_FILE = os.path.expanduser("~/.orc1_fx_rack_status.json")
# User's favourite plugins (short palette keys), persisted so the Fire's
# "Favorites" browse bank survives a restart.  Runtime user state, NOT source —
# lives in $HOME like every other per-user JSON.
FAV_FILE = os.path.expanduser("~/.orc1_fx_favorites.json")
# Hand-curated {plugin_key: category} map that SHIPS with the repo, sitting next
# to this module.  Anything not listed resolves to "Other", so it can never hide
# a plugin.  Regenerate/edit freely — it only drives browse grouping.
CATEGORIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "fx_categories.json")
# Display order for the category browse banks.  Any category present in
# fx_categories.json but missing here is appended alphabetically before "Other".
CATEGORY_ORDER = ["Reverb", "Delay", "EQ/Filter", "Dynamics", "Drive",
                  "Amp/Cab", "Modulation", "Tape/Lofi", "Clip", "Console",
                  "Dither", "Utility", "Other"]
N_SLOTS = 3                      # HARD CAP — do not raise without a DSP re-test.
_CLIENT = "fx-rack-{idx}"        # JACK client name per slot position.
_SAVE_DEBOUNCE_S = 1.0           # coalesce param-persist writes to disk.

# Sentinel so set_slot() can tell "leave unchanged" apart from "set to None".
_UNSET = object()


def _empty_slot():
    return {"plugin": None, "enabled": False, "params": {}}


class RackManager:
    """The 3-slot series FX rack.  One instance, created by FXManager."""

    def __init__(self, debug_fn=None, fx=None):
        self.dbg = debug_fn or (lambda m: None)
        self._fx = fx                      # parent FXManager (shared dry-cut refcount)
        self._lock = threading.Lock()      # serialises apply() rebuilds
        self._runtime = []                 # live jalv, one dict per active slot
        self._palette_cache = None
        self._categories_cache = None      # {key: category} from fx_categories.json
        self._favorites_cache = None       # ordered list of favourited plugin keys
        # Cached desired-state slots list.  We serve load()/status() from memory
        # to skip a per-call SD read — important because status() is on the
        # encoder/touchstrip hot path.  The cache is validated against the file's
        # mtime on every load(), so it self-heals when ANOTHER process rewrites
        # the file.  This matters: the web process holds its OWN read-only
        # RackManager and never calls save(), so without the mtime check its
        # cache froze at web-startup forever and every /fx slider snapped back to
        # the stale value ~4s after you moved it (the audio was correct — only
        # the read-back lied).  save() records the mtime it wrote so the brain
        # never re-reads its own write.
        self._desired_cache = None
        self._desired_mtime = None
        self._holds_internal = False       # True while WE hold an internal dry-cut

        # Live param cache + debounced persist.  Knob turns must NOT touch disk
        # on every tick (RT box — that was a documented xrun source elsewhere).
        # We push to the jalv FIFO live and remember the value in memory, then
        # flush to ~/.orc1_fx_rack.json at most once every _SAVE_DEBOUNCE_S.
        self._param_mem = {}               # {idx: {symbol: value}} pending/last
        self._save_timer = None
        self._save_lock = threading.Lock()

        if not _FCM_AVAILABLE:
            self.dbg("RackManager: fx_chain_manager unavailable — rack disabled")

    # -- Palette ---------------------------------------------------------------

    def palette(self):
        """Return the installed-plugin palette (cached).  Keys are short plugin
        names; values include label + control metadata for the knob UI."""
        if self._palette_cache is None:
            self._palette_cache = _fcm.load_palette() if _FCM_AVAILABLE else {}
        return self._palette_cache

    # -- Desired state (persisted) --------------------------------------------

    def load(self):
        """Return the desired rack config, always exactly N_SLOTS slots.

        Missing file / short list / junk entries are healed to empty slots so
        callers never have to guard the shape.
        """
        # Serve from the in-memory cache only while the file on disk is unchanged
        # (mtime match); otherwise re-read.  A cheap os.stat per call keeps the
        # hot path fast while letting a read-only instance (the web view) notice
        # writes made by the brain in another process.
        try:
            cur_mtime = os.stat(RACK_STATE_FILE).st_mtime
        except Exception:
            cur_mtime = None
        if self._desired_cache is not None and cur_mtime == self._desired_mtime:
            slots = self._desired_cache
        else:
            try:
                with open(RACK_STATE_FILE) as f:
                    slots = json.load(f).get("slots", [])
            except Exception:
                slots = []
            if not isinstance(slots, list):
                slots = []
            self._desired_cache = slots
            self._desired_mtime = cur_mtime
        # Normalise to exactly N_SLOTS well-formed slot dicts (fresh copies, so
        # a caller mutating the result can't corrupt the cache — only save()
        # updates it).
        fixed = []
        for i in range(N_SLOTS):
            s = slots[i] if i < len(slots) and isinstance(slots[i], dict) else {}
            fixed.append({
                "plugin":  s.get("plugin") or None,
                "enabled": bool(s.get("enabled", False)),
                "params":  dict(s.get("params", {})),
            })
        return {"slots": fixed}

    def save(self, desired):
        """Persist the desired config atomically and refresh the memory cache."""
        tmp = RACK_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(desired, f, indent=2)
        os.replace(tmp, RACK_STATE_FILE)
        # Keep the in-memory cache in step with what we just wrote, and record
        # the resulting mtime so load() serves this cache instead of re-reading
        # our own write on the hot path.
        self._desired_cache = desired.get("slots", [])
        try:
            self._desired_mtime = os.stat(RACK_STATE_FILE).st_mtime
        except Exception:
            self._desired_mtime = None

    # -- Mutations (each persists, then triggers a rebuild) --------------------

    def assign(self, idx, plugin, done=None):
        """Put `plugin` (a palette key, or None to clear) into slot `idx`.

        Assigning a new plugin resets that slot's remembered params and enables
        the slot (you almost always want to hear what you just dropped in).
        """
        if not (0 <= idx < N_SLOTS):
            return
        if plugin is not None and plugin not in self.palette():
            self.dbg(f"RackManager.assign: unknown plugin '{plugin}'")
            return
        d = self.load()
        d["slots"][idx]["plugin"] = plugin
        d["slots"][idx]["params"] = {}
        d["slots"][idx]["enabled"] = plugin is not None
        self.save(d)
        self._param_mem.pop(idx, None)      # new plugin — drop stale live params
        self.apply(done=done)

    def clear(self, idx, done=None):
        """Empty slot `idx` completely."""
        if not (0 <= idx < N_SLOTS):
            return
        d = self.load()
        d["slots"][idx] = _empty_slot()
        self.save(d)
        self._param_mem.pop(idx, None)
        self.apply(done=done)

    def set_enabled(self, idx, enabled, done=None):
        """Turn slot `idx` on or off without losing its plugin/params."""
        if not (0 <= idx < N_SLOTS):
            return
        d = self.load()
        d["slots"][idx]["enabled"] = bool(enabled)
        self.save(d)
        self.apply(done=done)

    def toggle(self, idx, done=None):
        """Flip slot `idx` enabled/disabled.  (SHIFT+pad on the Fire.)"""
        if not (0 <= idx < N_SLOTS):
            return
        d = self.load()
        cur = d["slots"][idx]["enabled"]
        # Can't enable an empty slot — nothing to turn on.
        if not cur and not d["slots"][idx]["plugin"]:
            return
        d["slots"][idx]["enabled"] = not cur
        self.save(d)
        self.apply(done=done)

    def move(self, idx, delta, done=None):
        """Move the plugin in slot `idx` to slot idx+delta (swap contents).

        Series order == slot order, so this reorders the effect within the
        chain.  delta is +1 (later) or -1 (earlier).
        """
        j = idx + delta
        if not (0 <= idx < N_SLOTS and 0 <= j < N_SLOTS):
            return
        d = self.load()
        d["slots"][idx], d["slots"][j] = d["slots"][j], d["slots"][idx]
        self.save(d)
        # The live-param cache is keyed by slot position — swap it to match so
        # a moved plugin keeps its not-yet-flushed knob values.
        a = self._param_mem.pop(idx, None)
        b = self._param_mem.pop(j, None)
        if b is not None:
            self._param_mem[idx] = b
        if a is not None:
            self._param_mem[j] = a
        self.apply(done=done)

    def param(self, idx, symbol, value):
        """Set a live control on slot `idx`'s running plugin AND remember it.

        Hot path (this runs on every encoder/slider tick): push the value to
        jalv over the FIFO and stash it in an in-memory cache — NO disk I/O.
        The persist to ~/.orc1_fx_rack.json is debounced (see _schedule_save)
        so a fast knob drag can't hammer the SD card / RT scheduler.  Returns
        True if the live FIFO write landed.
        """
        if not (0 <= idx < N_SLOTS):
            return False
        # Remember in memory (authoritative for this session) + debounce persist.
        self._param_mem.setdefault(idx, {})[symbol] = value
        self._schedule_save()
        # Push to the live jalv if this slot is currently running.
        r = self._runtime_for(idx)
        if not r:
            return False
        return self._fifo_write(r["fifo"], symbol, value)

    def _schedule_save(self):
        """(Re)arm the debounced persist of in-memory params to disk."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(_SAVE_DEBOUNCE_S, self._flush_params)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush_params(self):
        """Merge the in-memory param cache into the desired state on disk.

        Read-modify-write under _lock so it can't race a rebuild's own save.
        """
        with self._lock:
            try:
                d = self.load()
                for idx, syms in self._param_mem.items():
                    if 0 <= idx < N_SLOTS:
                        d["slots"][idx]["params"].update(syms)
                self.save(d)
            except Exception as e:
                self.dbg(f"RackManager._flush_params: {e}")

    # -- Query -----------------------------------------------------------------

    def status(self):
        """Return a UI-ready snapshot of all three slots + the running set.

        {
          "cap": 3,
          "slots": [
            {"idx":0,"plugin":"galactic","label":"Galactic","enabled":true,
             "running":true,"controls":[{symbol,label,min,max,default,value}...]},
            ...
          ]
        }
        """
        d = self.load()
        pal = self.palette()
        running_idx = {r["idx"] for r in self._runtime}
        out = []
        for i, s in enumerate(d["slots"]):
            key = s.get("plugin")
            entry = pal.get(key) if key else None
            controls = []
            if entry:
                live = self._param_mem.get(i, {})
                for c in entry.get("controls", []):
                    # current value: live in-memory override → persisted param
                    # → the plugin's own default.
                    val = live.get(c["symbol"],
                                   s["params"].get(c["symbol"], c.get("default")))
                    controls.append({**c, "value": val})
            out.append({
                "idx": i,
                "plugin": key,
                "label": entry.get("label", key) if entry else None,
                "enabled": bool(s.get("enabled")),
                "running": i in running_idx,
                "controls": controls,
            })
        return {"cap": N_SLOTS, "slots": out}

    def palette_list(self):
        """Return a lightweight [{key,label}] list for pickers, sorted by label."""
        pal = self.palette()
        items = [{"key": k, "label": v.get("label", k)} for k, v in pal.items()]
        items.sort(key=lambda x: x["label"].lower())
        return items

    def label_for(self, key):
        """User-facing label for a palette key (falls back to the key itself)."""
        if not key:
            return None
        return self.palette().get(key, {}).get("label", key)

    # -- Categories (shipped map) + Favourites (user state) --------------------

    def _load_categories(self):
        """Load + cache the {key: category} map from fx_categories.json.

        Missing / unreadable file just means every plugin is "Other" — the
        browser still works, it's only the grouping that's lost."""
        if self._categories_cache is None:
            try:
                with open(CATEGORIES_FILE) as f:
                    data = json.load(f)
                self._categories_cache = {k: str(v) for k, v in data.items()}
            except Exception as e:
                self.dbg(f"RackManager: no categories ({e}); all -> Other")
                self._categories_cache = {}
        return self._categories_cache

    def categories(self):
        """Return an ordered {category: [keys]} over the INSTALLED palette only.

        Keys within each category are sorted by label.  Uncategorised plugins
        fall into "Other".  Category order follows CATEGORY_ORDER, with any
        extra categories appended alphabetically just before "Other"."""
        cmap = self._load_categories()
        pal = self.palette()
        groups = {}
        for key in pal:
            cat = cmap.get(key, "Other")
            groups.setdefault(cat, []).append(key)
        for cat in groups:
            groups[cat].sort(key=lambda k: self.label_for(k).lower())
        # Build the ordered result.
        ordered = {}
        extras = sorted(c for c in groups
                        if c not in CATEGORY_ORDER)
        for cat in [c for c in CATEGORY_ORDER if c != "Other"] + extras:
            if groups.get(cat):
                ordered[cat] = groups[cat]
        if groups.get("Other"):
            ordered["Other"] = groups["Other"]
        return ordered

    def favorites(self):
        """Return the user's favourite plugin keys (persisted order), filtered
        to those still present in the palette."""
        if self._favorites_cache is None:
            favs = []
            try:
                with open(FAV_FILE) as f:
                    favs = json.load(f).get("favorites", [])
            except Exception:
                favs = []
            self._favorites_cache = [k for k in favs if isinstance(k, str)]
        pal = self.palette()
        return [k for k in self._favorites_cache if k in pal]

    def is_favorite(self, key):
        return key in self.favorites()

    def favorites_toggle(self, key):
        """Add `key` to favourites if absent, remove it if present.  Persists
        and returns the NEW state (True = now a favourite)."""
        if key is None or key not in self.palette():
            return False
        # Work off the raw cached list (favorites() filters to palette; toggling
        # should still operate on the stored order).
        if self._favorites_cache is None:
            self.favorites()                # populate the cache
        cur = list(self._favorites_cache)
        if key in cur:
            cur.remove(key)
            now = False
        else:
            cur.append(key)
            now = True
        self._favorites_cache = cur
        try:
            tmp = FAV_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"favorites": cur}, f, indent=2)
            os.replace(tmp, FAV_FILE)
        except Exception as e:
            self.dbg(f"RackManager.favorites_toggle: persist failed: {e}")
        return now

    # -- Browse banks (Favorites + All + one per category) --------------------

    def banks(self):
        """Ordered list of browse-bank names for the Fire's slot browser:
        ['Favorites', 'All', <each non-empty category in order>]."""
        return ["Favorites", "All"] + list(self.categories().keys())

    def bank_items(self, bank):
        """Return the plugin keys shown in `bank`.

        - 'Favorites' : the user's favourites (may be empty)
        - 'All'       : every installed plugin, alphabetical by label
        - <category>  : that category's members, alphabetical by label
        Unknown bank name -> empty list."""
        if bank == "Favorites":
            return list(self.favorites())
        if bank == "All":
            return [it["key"] for it in self.palette_list()]
        return list(self.categories().get(bank, []))

    # -- The rebuild -----------------------------------------------------------

    def apply(self, done=None):
        """Rebuild the live rack to match the desired state (background thread).

        Runs off-thread because spawning jalv blocks a few seconds.  done() is
        an optional no-arg callback fired when the rebuild finishes (UI repaint).
        """
        if not _FCM_AVAILABLE:
            self._fire(done)
            return
        threading.Thread(target=self._apply_worker, args=(done,),
                         daemon=True).start()

    def _apply_worker(self, done):
        with self._lock:
            try:
                self._teardown_locked()
                # Give back our dry-cut BEFORE rebuilding: the source may have
                # changed (another module, or hardware mode), and the token
                # remembers the OLD pairs. The build re-cuts against the current
                # fx_io. Until 2026-09-28 a held token made the re-acquire a
                # no-op: the old source stayed cut and the new one played dry.
                self._release_internal()
                self._build_locked()
            except Exception as e:
                self.dbg(f"RackManager.apply error: {e}")
                # Safety: never leave the dry path cut with no audio flowing.
                # If the build failed and nothing is live, restore dry.
                try:
                    if not self._runtime:
                        self._release_internal()
                except Exception:
                    pass
        self._fire(done)

    def _acquire_internal(self):
        """Cut the synth dry path (via the shared FXManager refcount).  Idempotent
        for this rack — we hold at most one dry-cut token."""
        if self._holds_internal:
            return
        if self._fx is not None and hasattr(self._fx, "internal_acquire"):
            # False in HARDWARE mode: no token taken, so none to give back
            # (a phantom release would steal a legacy chain's token).
            self._holds_internal = bool(self._fx.internal_acquire())
        else:
            self._direct_dry(cut=True)     # standalone/no-parent fallback
            self._holds_internal = True

    def _release_internal(self):
        """Release our dry-cut token; dry is only actually restored when the
        last consumer (rack or legacy chain) lets go."""
        if not self._holds_internal:
            return
        if self._fx is not None and hasattr(self._fx, "internal_release"):
            self._fx.internal_release()
        else:
            self._direct_dry(cut=False)
        self._holds_internal = False

    def _direct_dry(self, cut):
        """Fallback dry-cut used only when there's no parent FXManager (tests /
        standalone).  Mode-aware, mirrors FXManager.internal_acquire/release."""
        io = _read_fx_io()
        if io.get("mode") != "internal":
            return
        ports = _fcm._get_fx_ports()
        fn = _fcm.jack_disconnect if cut else _fcm.jack_connect
        fn(ports["FX_SEND_L"], ports["FX_RETURN_L"])
        fn(ports["FX_SEND_R"], ports["FX_RETURN_R"])

    def _teardown_locked(self):
        """Kill every jalv we currently own and clean up its FIFO."""
        for r in self._runtime:
            try:
                os.kill(r["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            _unlink(r["fifo"])
        self._runtime = []
        # Belt-and-suspenders: reap any stray fx-rack-* jalv from a prior crash.
        self._reap_stray_clients()

    def _build_locked(self):
        """Spawn + wire the enabled/assigned slots in series.  Lock held."""
        desired = self.load()
        pal = self.palette()
        ports = _fcm._get_fx_ports()   # {FX_SEND_L, FX_SEND_R, FX_RETURN_L, FX_RETURN_R}
        io = _read_fx_io()

        # Which slots are actually going to run, in slot order (= series order).
        active = [(i, s) for i, s in enumerate(desired["slots"])
                  if s.get("enabled") and s.get("plugin") in pal]

        new_runtime = []
        for i, s in active:
            entry = pal[s["plugin"]]
            client = _CLIENT.format(idx=i)
            pid, fifo = _fcm.spawn_jalv(client, entry["uri"])
            if pid is None:
                self.dbg(f"RackManager: slot {i} '{s['plugin']}' failed to start; skipping")
                continue
            r = {"idx": i, "client": client, "pid": pid, "fifo": fifo,
                 "plugin": s["plugin"], "entry": entry}
            new_runtime.append(r)
            # Push remembered knob positions into the fresh jalv: the persisted
            # params, overlaid with any not-yet-flushed in-memory values.
            merged = dict(s.get("params", {}))
            merged.update(self._param_mem.get(i, {}))
            for sym, val in merged.items():
                self._fifo_write(fifo, sym, val)

        # Wire the series: SEND → r0 → r1 → ... → RETURN.
        if new_runtime:
            # Wait for the fx_io endpoint ports to exist before wiring. On an orc1
            # restart the rack applies during startup, BEFORE ORC1-FXMix (PartOf=
            # orc1, ordered After it) has registered send_*/wet_* -- a naive
            # connect would silently fail and leave the chain dead until a manual
            # re-apply. Bounded so a genuinely-absent endpoint can't hang boot.
            _wait_for_ports([ports["FX_SEND_L"], ports["FX_SEND_R"],
                             ports["FX_RETURN_L"], ports["FX_RETURN_R"]], 12.0)
            prev_out = [ports["FX_SEND_L"], ports["FX_SEND_R"]]
            for r in new_runtime:
                in_ports = [f"{r['client']}:{p}" for p in r["entry"]["audio_in"]]
                _wire_bus(prev_out, in_ports)
                prev_out = [f"{r['client']}:{p}" for p in r["entry"]["audio_out"]]
            _wire_bus(prev_out, [ports["FX_RETURN_L"], ports["FX_RETURN_R"]])

            # INTERNAL insert: cut the synth's dry through-connection so you
            # don't hear dry+wet.  Done via the SHARED refcount so the rack and
            # any legacy chain can't fight over it (see FXManager.internal_*).
            self._acquire_internal()
        else:
            # Rack empty: drop our dry-cut so sound reaches the speakers again
            # (only actually reconnects if no legacy chain still holds it).
            self._release_internal()

        self._runtime = new_runtime
        self.dbg(f"RackManager: rack rebuilt — {len(new_runtime)} slot(s) live")
        self._write_status()

    def _write_status(self):
        """Dump status() to RACK_STATUS_FILE so the web process can read the
        live running-set.  Best-effort; never raises into the audio path."""
        try:
            tmp = RACK_STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.status(), f)
            os.replace(tmp, RACK_STATUS_FILE)
        except Exception as e:
            self.dbg(f"RackManager._write_status: {e}")

    def reap_orphans_and_restore(self, done=None):
        """Startup hook: kill any leftover fx-rack-* jalv from a previous brain
        run, then re-apply the saved desired state cleanly.  Call once from
        orc1.py after eng.fx is created."""
        with self._lock:
            self._runtime = []
            self._reap_stray_clients()
        self.apply(done=done)

    # -- Small helpers ---------------------------------------------------------

    def _fire(self, callback):
        """Invoke a done-callback safely (a buggy callback must never kill the
        worker thread or leave the rack half-applied)."""
        if callable(callback):
            try:
                callback()
            except Exception as e:
                self.dbg(f"RackManager: done-callback raised: {e}")

    def _runtime_for(self, idx):
        for r in self._runtime:
            if r["idx"] == idx:
                return r
        return None

    def _fifo_write(self, fifo_path, symbol, value):
        """Write one 'symbol = value' command to a jalv FIFO (non-blocking)."""
        if not fifo_path or not os.path.exists(fifo_path):
            return False
        try:
            fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, f"{symbol} = {float(value):.6f}\n".encode())
            finally:
                os.close(fd)
            return True
        except OSError as e:
            self.dbg(f"RackManager._fifo_write: {symbol}={value}: {e}")
            return False

    def _reap_stray_clients(self):
        """Kill any jalv whose JACK client name starts with 'fx-rack-'.

        Uses pkill on the jalv command line (the -n <client> arg carries the
        name).  Silent if none exist.
        """
        try:
            import subprocess
            for i in range(N_SLOTS):
                subprocess.run(["pkill", "-f", f"jalv .*{_CLIENT.format(idx=i)}"],
                               capture_output=True, timeout=3)
        except Exception:
            pass


def _wait_for_ports(names, timeout=12.0):
    """Poll jack_lsp until every port in `names` exists (or timeout). Used to let
    an orc1-startup rack apply wait for external endpoint ports (ORC1-FXMix) that
    come up just after the brain."""
    import subprocess
    end = time.time() + timeout
    need = {n for n in names if n}
    while time.time() < end:
        try:
            have = set(subprocess.run(["jack_lsp"], capture_output=True,
                                      text=True).stdout.split())
        except Exception:
            have = set()
        if need <= have:
            return True
        time.sleep(0.4)
    return False


def _wire_bus(src_ports, dst_ports):
    """Connect one audio bus to the next, tolerating mono/stereo mismatch.

    - equal counts        → straight 1:1
    - dst is mono (1)     → sum both srcs into it
    - src is mono (1)     → fan it out to both dsts
    - otherwise           → zip the shorter length
    """
    if not src_ports or not dst_ports:
        return
    if len(src_ports) == len(dst_ports):
        for s, d in zip(src_ports, dst_ports):
            _fcm.jack_connect(s, d)
    elif len(dst_ports) == 1:
        for s in src_ports:
            _fcm.jack_connect(s, dst_ports[0])
    elif len(src_ports) == 1:
        for d in dst_ports:
            _fcm.jack_connect(src_ports[0], d)
    else:
        for s, d in zip(src_ports, dst_ports):
            _fcm.jack_connect(s, d)


def _read_fx_io():
    """Return the FX I/O config (mode + ports).  Mirrors orc1_fx._read_fx_io;
    kept local so this module has no dependency back on orc1_fx."""
    defaults = {"mode": "hardware"}
    try:
        io_file = _fcm.FX_IO_FILE if _FCM_AVAILABLE else os.path.expanduser("~/.orc1_fx_io.json")
        with open(io_file) as f:
            defaults.update(json.load(f))
    except Exception:
        pass
    return defaults


def _unlink(path):
    try:
        os.unlink(path)
    except Exception:
        pass
