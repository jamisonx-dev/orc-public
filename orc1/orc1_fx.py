# ===============================================================================
# ORC-1 FX MANAGER  (orc1_fx.py)
# MechaJesus integration layer -- instantiated as eng.fx in orc1.py.
# Imported by orc1.py. Do not run directly.
# ===============================================================================
#
# WHY THIS EXISTS
#   Before this file, MechaJesus FX chain control was split across two places:
#
#     orc1_fire.py      -- private _fx_load_chains, _fx_load_state,
#                         _fx_chain_running, _fx_toggle_chain methods baked
#                         directly into the Akai Fire handler.  Only the Fire
#                         could see or control chains.
#
#     fx_chain_manager.py -- standalone CLI called via subprocess whenever the
#                           Fire wanted to start or stop a chain.
#
#   The web UI and orc1.py command handler had no visibility into FX state at
#   all.  Any feature that needed to know "is nes_space running?" had to reach
#   sideways into the Fire's internals, which is the wrong shape for a system
#   that's growing.
#
#   Now:  eng.fx is the one front door for everything MechaJesus.
#   The Fire, the web UI, and orc1.py command handler all call eng.fx.
#   fx_chain_manager.py stays exactly as it is -- it still works as a standalone
#   debug tool from the terminal, and FXManager imports its functions as a
#   library so the jalv logic lives in one place.
#
# ARCHITECTURE
#   FXManager is thin on purpose.  It does not reimplement jalv management.
#   It wraps fx_chain_manager's functions so:
#     - start/stop run on background threads (jalv takes 2-4s to register
#       JACK ports -- we must never block the MIDI thread)
#     - callers get a done_callback so they can update their UI once the
#       operation is actually complete
#     - the rest of the system always has a live, thread-safe query interface
#
# CACHING (added Session C -- encoder-click optimisation)
#   Two things were reading from disk on every single encoder turn:
#     1. _dial_fx_param() called load_chains() -> read fx_chains.json
#     2. send_param() called _fcm.send_param() -> read .fx_state.json
#   Chain definitions never change mid-session, so we load them once and keep
#   them.  FIFO paths only change when a chain starts or stops, so we cache
#   them after start_chain() and clear on stop_chain().
#   The hot path for send_param() is now: dict lookup -> os.open -> os.write ->
#   os.close.  No file I/O at all during encoder turns.
#
# USAGE (from orc1.py)
#   from orc1_fx import FXManager
#   fx = FXManager(debug_fn=print if cfg.DEBUG else None)
#   # eng.fx is now live.  The Fire, web, and command handler all use it.
#
# API AT A GLANCE
#   eng.fx.load_chains()                      -> dict from fx_chains.json
#   eng.fx.load_state()                       -> dict from .fx_state.json
#   eng.fx.chain_running(name, state=None)    -> bool; pass state to avoid re-reads
#   eng.fx.status()                           -> {name: {running, pids, description}}
#   eng.fx.start_chain(name, done=None)       -> async; done() fires when jalv is up
#   eng.fx.stop_chain(name, done=None)        -> async; done() fires when stopped
#   eng.fx.toggle_chain(name, done=None)      -> start if stopped, stop if running
#   eng.fx.send_param(name, symbol, val, plugin_idx=0)  -> bool; real-time, no disk
# ===============================================================================

import json
import os
import subprocess
import sys
import threading

# fx_chain_manager.py lives in the same directory as orc1_fx.py (~/).
# orc1.py always inserts ~ into sys.path before importing us, so this will
# find it in the normal case.  The guard below covers edge cases like running
# tests or importing orc1_fx.py from a different working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import fx_chain_manager as _fcm
    _FCM_AVAILABLE = True
except ImportError:
    _FCM_AVAILABLE = False

# The 3-slot series rack (new-style FX).  Optional import so the preset-chain
# path still works even if the rack module is somehow missing.
try:
    from orc1_fx_rack import RackManager
    _RACK_AVAILABLE = True
except ImportError:
    _RACK_AVAILABLE = False


def _read_fx_io():
    """Return current FX I/O config from ~/.orc1_fx_io.json.
    Keys: mode ('internal'|'hardware'), send_l, send_r, return_l, return_r.
    """
    defaults = {
        "mode":     "hardware",
        "send_l":   "pisound-in:capture_1",
        "send_r":   "pisound-in:capture_2",
        "return_l": "pisound-out:playback_1",
        "return_r": "pisound-out:playback_2",
    }
    try:
        io_file = _fcm.FX_IO_FILE if _FCM_AVAILABLE else os.path.expanduser("~/.orc1_fx_io.json")
        with open(io_file) as f:
            defaults.update(json.load(f))
    except Exception:
        pass
    return defaults


def _jack_silent(cmd, src, dst):
    """Run jack_connect or jack_disconnect silently (no exception on failure)."""
    try:
        subprocess.run([cmd, src, dst], capture_output=True, timeout=3)
    except Exception:
        pass


class FXManager:
    """
    ORC-1 integration layer for the MechaJesus LV2 plugin chain system.

    Wraps fx_chain_manager's start/stop/query functions in a thread-safe,
    callback-driven interface so the whole ORC-1 ecosystem (Akai Fire, web
    UI, orc1.py command handler) can all see and control FX chains through
    a single shared object -- eng.fx -- without blocking the MIDI thread or
    duplicating jalv management logic.

    Instantiated once in orc1.py as the module-level variable 'fx'.
    orc1.py passes itself as the engine (sys.modules[__name__]), so
    eng.fx in the Fire and web layer resolves to this object.
    """

    def __init__(self, debug_fn=None):
        """
        debug_fn(msg) -- optional logging callback.
        Pass 'print' when cfg.DEBUG is True for visibility during development.
        """
        self.dbg = debug_fn or (lambda m: None)

        # -- Session-level caches -----------------------------------------------
        # Chain definitions come from fx_chains.json and never change while
        # ORC-1 is running.  Load once, keep forever.
        self._chains_cache = None

        # FIFO paths per chain -- written here by _run_start() after jalv is up,
        # cleared by _run_stop() when the chain dies.  Populated lazily on first
        # send_param() call if the chain was started outside this session.
        # Format: { chain_name: ["/tmp/jalv-fx-name-0.fifo", ...] }
        self._fifo_cache = {}

        # Internal insert tracking: how many chains are currently holding the
        # synth's direct system:playback connection open (disconnected).
        # When this drops back to 0, we reconnect the direct path.
        self._internal_count = 0
        self._internal_lock  = threading.Lock()
        self._internal_cut   = []      # [(src, dst)] actually disconnected
        self._internal_chains = set()  # legacy chains holding a dry-cut token

        # -- The 3-slot series rack (new-style FX) ------------------------------
        # eng.fx.rack is the front door for the stack-up-to-3 rack, driven by
        # the /fx web page.  (The Akai Fire FX view still drives the legacy
        # preset chains for now — migrating it to the rack is a follow-up.)
        # We pass self so the rack shares this manager's internal dry-cut
        # refcount (internal_acquire/release) instead of fighting over it.
        self.rack = RackManager(debug_fn=debug_fn, fx=self) if _RACK_AVAILABLE else None

        if not _FCM_AVAILABLE:
            self.dbg("FXManager: fx_chain_manager not importable -- FX control disabled")

    # -- Query / state ---------------------------------------------------------
    #
    # These are fast reads from small JSON files, safe to call from any thread.

    def load_chains(self):
        """
        Load and return the full chain definition dict from ~/fx_chains.json.
        Each key is a chain name; each value is the definition (description,
        exclusive flag, plugin list, JACK wiring).

        Result is cached after the first successful load -- chain definitions
        don't change while ORC-1 is running, so there's no reason to hit the
        disk more than once per session.

        Returns {} on any error (file missing, bad JSON, permissions, etc.)
        so callers never have to handle exceptions.
        """
        if self._chains_cache is not None:
            return self._chains_cache      # fast path -- already loaded this session
        if not _FCM_AVAILABLE:
            return {}
        try:
            self._chains_cache = _fcm.load_chains()
            return self._chains_cache
        except Exception as e:
            self.dbg(f"FXManager.load_chains: {e}")
            return {}

    def load_state(self):
        """
        Load and return the running-chain state dict from ~/.fx_state.json.
        fx_chain_manager writes this file every time a chain starts or stops.

        Returns {} if no chains have ever been started (file doesn't exist yet)
        or on any read error.

        Tip: call this once and pass the result to chain_running() in a loop
        so you only hit the disk once per render cycle.
        """
        if not _FCM_AVAILABLE:
            return {}
        try:
            return _fcm.load_state()
        except Exception as e:
            self.dbg(f"FXManager.load_state: {e}")
            return {}

    def chain_running(self, name, state_data=None):
        """
        Return True if the named chain has at least one live jalv PID.

        Checks /proc/<pid> directly rather than trusting the state file alone --
        stale PID entries from a crash or unexpected restart will read as dead.

        state_data: pass a pre-loaded state dict (from load_state()) to avoid
        re-reading from disk on every call in a tight render loop.  If None,
        reads from disk fresh.
        """
        if state_data is None:
            state_data = self.load_state()
        pids = state_data.get(name, {}).get("pids", [])
        return any(os.path.exists(f"/proc/{pid}") for pid in pids)

    def status(self):
        """
        Return a summary dict for every chain defined in fx_chains.json:
            {
              "nes_space":   {"running": True,  "pids": [1234],  "description": "Galactic shimmer ..."},
              "tape_warmth": {"running": False,  "pids": [],      "description": "ToTape6 ..."},
              ...
            }

        Reads fx_chains.json and .fx_state.json once each, then verifies
        each PID against /proc so stale entries read as not running.

        Used by the web UI status panel and the orc1.py 'FX_STATUS' command.
        """
        chains     = self.load_chains()
        state_data = self.load_state()
        result     = {}
        for name, chain_def in chains.items():
            pids       = state_data.get(name, {}).get("pids", [])
            alive_pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
            result[name] = {
                "running":     bool(alive_pids),
                "pids":        alive_pids,
                "description": chain_def.get("description", ""),
                "exclusive":   chain_def.get("exclusive", False),
            }
        return result

    # -- Start / stop ---------------------------------------------------------
    #
    # All three methods return immediately.  The actual work -- spawning jalv,
    # waiting for JACK port registration (2.5s per plugin), wiring connections --
    # happens on a daemon background thread so the MIDI/clock thread is never
    # delayed.  The done callback fires from that background thread once the
    # operation is complete (or fails), so the caller can update its UI.

    def start_chain(self, name, done=None):
        """
        Start a named chain in the background.

        Spawns jalv for each plugin in the chain, waits for JACK ports to
        register, then wires JACK connections and writes .fx_state.json.
        This can take 2-4 seconds for a multi-plugin chain on the Pi 4 --
        which is why it runs on its own thread instead of blocking the caller.

        name:  chain name as it appears in fx_chains.json
        done:  optional no-argument callback, called from the background thread
               when the chain is running (or if startup fails).
               The Fire uses this to trigger a pad-colour re-render.
        """
        if not _FCM_AVAILABLE:
            self.dbg(f"FXManager: can't start '{name}' -- fx_chain_manager unavailable")
            return
        threading.Thread(
            target=self._run_start,
            args=(name, done),
            daemon=True,
        ).start()

    def stop_chain(self, name, done=None):
        """
        Stop a named chain in the background.

        Sends SIGTERM to all jalv PIDs for this chain and removes it from
        .fx_state.json.  The actual kill is nearly instant, but we run it
        off-thread for consistency with start_chain and so done() always
        fires after the state file is updated.

        name:  chain name as it appears in fx_chains.json
        done:  optional no-argument callback, called when the chain is stopped.
        """
        if not _FCM_AVAILABLE:
            return
        threading.Thread(
            target=self._run_stop,
            args=(name, done),
            daemon=True,
        ).start()

    def toggle_chain(self, name, done=None):
        """
        Start if stopped, stop if running.

        The Fire calls this on SHIFT+slot-pad so the pad flip-flops the chain
        state without the caller needing to know the current state first.
        done() is forwarded to whichever operation (start or stop) actually runs.

        Reads current state from disk at call time; the background thread then
        carries out the operation.
        """
        state_data = self.load_state()
        if self.chain_running(name, state_data):
            self.stop_chain(name, done=done)
        else:
            self.start_chain(name, done=done)

    # -- Background thread workers ---------------------------------------------

    def clear_cache(self):
        """Clear chain definition + FIFO caches.
        Call after the FX I/O mode changes so the next start_chain() re-resolves
        port tokens from the updated ~/.orc1_fx_io.json.
        """
        self._chains_cache = None
        self._fifo_cache.clear()
        self.dbg("FXManager: cache cleared")

    # -- Internal-insert dry-path ownership (SHARED refcount) ------------------
    #
    # In INTERNAL mode the FX "send" is the synth's own output and the "return"
    # is the speakers, so normally the synth is wired straight through (dry).
    # Any FX in the path (a legacy chain OR the 3-slot rack) must cut that dry
    # through-connection or you hear dry+wet at once.  BOTH systems share this
    # ONE refcount so they can never fight over it: the dry path is cut on the
    # first consumer and restored only when the LAST consumer leaves.
    # The dry path is the SEND's own direct wire to the RETURN (in internal mode
    # the send is the chosen module's outs and the return is where that module
    # plays). Until 2026-09-28 this was hardcoded to the retired ORC1-Synth, so
    # it cut nothing. The pairs actually cut are REMEMBERED and restored as-is:
    # re-reading fx_io at release time would reconnect the NEW source's ports
    # after a module switch and leave the old one silent.
    def internal_acquire(self):
        """Register one consumer of the internal dry-cut; cut dry on the first.
        Returns True if a token was taken (internal mode), else False."""
        io = _read_fx_io()
        if io.get("mode") != "internal":
            return False
        with self._internal_lock:
            if self._internal_count == 0:
                p = _fcm._get_fx_ports() if _FCM_AVAILABLE else {}
                cut = [(p.get("FX_SEND_L"), p.get("FX_RETURN_L")),
                       (p.get("FX_SEND_R"), p.get("FX_RETURN_R"))]
                self._internal_cut = [(s, d) for s, d in cut if s and d]
                self.dbg(f"FXManager: internal mode — cutting dry path {self._internal_cut}")
                for s, d in self._internal_cut:
                    _jack_silent("jack_disconnect", s, d)
            self._internal_count += 1
        return True

    def internal_release(self):
        """Drop one consumer; restore the dry path when the last one leaves.
        Restores exactly the pairs that were cut, whatever fx_io says now."""
        with self._internal_lock:
            if self._internal_count == 0:
                return
            self._internal_count -= 1
            if self._internal_count == 0:
                self.dbg(f"FXManager: restoring dry path {self._internal_cut}")
                for s, d in self._internal_cut:
                    _jack_silent("jack_connect", s, d)
                self._internal_cut = []

    def _run_start(self, name, callback):
        """Body of the background thread started by start_chain()."""
        io = _read_fx_io()
        ok = False
        try:
            self.dbg(f"FXManager: starting chain '{name}' (mode={io['mode']})...")
            ok = _fcm.start_chain(name)
            if ok:
                self.dbg(f"FXManager: chain '{name}' is running")
                # Cache the FIFO paths now that jalv is up.
                state = _fcm.load_state()
                self._fifo_cache[name] = state.get(name, {}).get("fifos", [])
                self.dbg(f"FXManager: cached FIFOs for '{name}': {self._fifo_cache[name]}")

                # INTERNAL INSERT: cut the source's dry path (shared refcount).
                # Remember that THIS chain holds a token, so its stop releases
                # it even if the mode has changed in between.
                if name not in self._internal_chains and self.internal_acquire():
                    self._internal_chains.add(name)
        except Exception as e:
            self.dbg(f"FXManager: start_chain '{name}' error: {e}")
        self._fire_callback(callback)

    def _run_stop(self, name, callback):
        """Body of the background thread started by stop_chain()."""
        io = _read_fx_io()
        try:
            self.dbg(f"FXManager: stopping chain '{name}'...")
            _fcm.stop_chain(name)
            self.dbg(f"FXManager: chain '{name}' stopped")
            # Clear the cached FIFOs -- the files won't exist after stop.
            self._fifo_cache.pop(name, None)

            # INTERNAL INSERT: drop this chain's dry-cut token, if it holds one.
            if name in self._internal_chains:
                self._internal_chains.discard(name)
                self.internal_release()
        except Exception as e:
            self.dbg(f"FXManager: stop_chain '{name}' error: {e}")
        self._fire_callback(callback)

    # -- Real-time parameter control -------------------------------------------
    #
    # send_param() is the one-liner that makes MechaJesus feel like a ZOIA:
    # encoder turn -> orc1_fire.py calls eng.fx.send_param() -> writes one line
    # to jalv's stdin FIFO -> jalv applies the parameter change in its next
    # audio callback (~10ms away at 512-sample buffers).
    #
    # Hot path (chain was started this session):
    #   _fifo_cache lookup (dict) -> os.open FIFO -> os.write -> os.close
    #   No file I/O, no JSON parsing, no GIL contention beyond the write itself.
    #
    # Cold path (chain was started before this orc1.py session, e.g. from CLI):
    #   Read .fx_state.json once -> cache -> same fast path from then on.

    def send_param(self, chain_name, symbol, value, plugin_idx=0):
        """Send a real-time parameter change to a running jalv plugin.

        chain_name:  chain name as it appears in fx_chains.json
        symbol:      LV2 port symbol for the parameter (e.g. "replace", "pressure")
        value:       float value to set
        plugin_idx:  which plugin in the chain (0 = first; 1 = second in series)

        Returns True on success, False if the chain isn't running or write fails.
        No exceptions are raised -- audio-path callers never need a try/except.
        """
        if not _FCM_AVAILABLE:
            return False

        # -- Locate the FIFO -- from cache if possible --------------------------
        fifos = self._fifo_cache.get(chain_name)

        if fifos is None:
            # Cache miss.  Chain may have been started outside this session
            # (e.g. by fx_chain_manager.py from the terminal, or a previous
            # orc1.py run).  Read the state file once and warm the cache so
            # every subsequent call for this chain is disk-free.
            self.dbg(f"FXManager.send_param: cache miss for '{chain_name}', "
                     f"reading state file once")
            state  = _fcm.load_state()
            fifos  = state.get(chain_name, {}).get("fifos", [])
            self._fifo_cache[chain_name] = fifos

        if plugin_idx >= len(fifos):
            self.dbg(f"FXManager.send_param: plugin_idx {plugin_idx} out of range "
                     f"for chain '{chain_name}' ({len(fifos)} plugin(s))")
            return False

        fifo_path = fifos[plugin_idx]

        # Quick sanity check -- if jalv died the FIFO file disappears.
        if not os.path.exists(fifo_path):
            self.dbg(f"FXManager.send_param: FIFO gone for '{chain_name}' -- "
                     f"jalv may have died; clearing cache entry")
            self._fifo_cache.pop(chain_name, None)
            return False

        # -- Write the parameter command to jalv's stdin FIFO ------------------
        # Format: "symbol = value\n" -- jalv's readline loop in its audio
        # callback picks this up and applies it to the control port.
        # O_NONBLOCK: if jalv has died (no reader), os.open raises ENXIO
        # instead of blocking forever.  The write is atomic (< PIPE_BUF).
        cmd = f"{symbol} = {value:.6f}\n".encode()
        try:
            fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, cmd)
            finally:
                os.close(fd)
            return True

        except OSError as e:
            import errno
            if e.errno == errno.ENXIO:
                self.dbg(f"FXManager.send_param: '{chain_name}'[{plugin_idx}] "
                         f"jalv has no reader -- process died; clearing cache")
                self._fifo_cache.pop(chain_name, None)
            elif e.errno == errno.EAGAIN:
                # FIFO pipe buffer full -- encoder is moving faster than jalv
                # is consuming.  Drop this update; the next tick will catch up.
                self.dbg(f"FXManager.send_param: '{chain_name}'[{plugin_idx}] "
                         f"FIFO full -- encoder too fast, skipping tick")
            else:
                self.dbg(f"FXManager.send_param: {chain_name}[{plugin_idx}] "
                         f"{symbol}={value:.4f}: {e}")
            return False

    # -- Internal helpers ------------------------------------------------------

    def _fire_callback(self, callback):
        """Safely invoke a done-callback from a background thread.

        All three of start_chain / stop_chain / toggle_chain accept an optional
        callback that fires when the operation completes.  This helper wraps the
        call in a try/except so a buggy callback never kills the worker thread.
        """
        if callable(callback):
            try:
                callback()
            except Exception as e:
                self.dbg(f"FXManager: done-callback raised: {e}")
