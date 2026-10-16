#!/usr/bin/env python3
"""
fx_chain_manager.py — MechaJesus FX chain manager
Reads ~/fx_chains.json, starts/stops jalv plugin instances,
manages JACK connections, and sends real-time parameter changes.

Usage:
  fx_chain_manager.py start <chain_name>
  fx_chain_manager.py stop  <chain_name>
  fx_chain_manager.py list
  fx_chain_manager.py status
  fx_chain_manager.py send <chain_name> <plugin_idx> <symbol> <value>
"""

import sys, os, json, subprocess, signal, time, shlex

def _set_rt_priority():
    """preexec_fn: set SCHED_FIFO priority 9 on the forked child before exec.

    Called in the child process after fork() and fd setup but before execve().
    Using preexec_fn (not 'chrt' in the bash cmd) avoids PATH lookup issues and
    RLIMIT_RTPRIO failures that leave chrt as a ghost PID: when chrt exits with
    EPERM the bash PID can be immediately recycled by the kernel, causing
    _wait_for_jack_ports to watch the wrong process for the full 10-second
    timeout.  preexec_fn raises an exception that Python catches in the parent
    if it fails, giving a clear error instead of a silent 10-second hang.

    Requires LimitRTPRIO=95 in /etc/systemd/system/orc1.service.d/realtime.conf.
    Falls back to SCHED_OTHER silently if not permitted (FX chains still work).
    SAFETY: only syscalls here — no Python I/O, no imports (no lock acquisition
    in a post-fork multithreaded process).
    """
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(9))
    except Exception:
        pass   # no LimitRTPRIO yet; fall back to SCHED_OTHER

CHAINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_chains.json")
STATE_FILE  = os.path.expanduser("~/.fx_state.json")
JALV        = "/usr/bin/jalv"   # or jalv.gtk if installed

# Template for the FIFO path for a given jalv client.
# The FIFO is how we send live parameter commands to a running jalv instance.
# One FIFO per plugin (one per jalv process), named after the JACK client name
# so it's easy to find and debug from the terminal.
_FIFO_TEMPLATE = "/tmp/jalv-{client_name}.fifo"

# Runtime FX I/O override file.  Written by the web UI (POST /api/fx/io) or
# by the SET_FX_IO command.  If absent, falls back to orc1_config.FX, then
# to the historical pisound defaults.
FX_IO_FILE = os.path.expanduser("~/.orc1_fx_io.json")

# Config-level fallback (loaded once at import for the static defaults)
try:
    import orc1_config as _cfg
    _CFG_FX_PORTS = {
        "FX_SEND_L":   _cfg.FX["SEND_PORTS"][0],
        "FX_SEND_R":   _cfg.FX["SEND_PORTS"][1],
        "FX_RETURN_L": _cfg.FX["RETURN_PORTS"][0],
        "FX_RETURN_R": _cfg.FX["RETURN_PORTS"][1],
    }
except Exception:
    _CFG_FX_PORTS = {
        "FX_SEND_L":   "pisound-in:capture_1",
        "FX_SEND_R":   "pisound-in:capture_2",
        "FX_RETURN_L": "pisound-out:playback_1",
        "FX_RETURN_R": "pisound-out:playback_2",
    }


def _get_fx_ports():
    """Return the current FX_SEND_*/FX_RETURN_* → JACK port name mapping.

    Reads ~/.orc1_fx_io.json at call time so the web UI can switch modes
    (INTERNAL vs HARDWARE INSERT) without restarting the engine.
    Falls back to orc1_config values, then to pisound historical defaults.
    """
    try:
        with open(FX_IO_FILE) as _f:
            _d = json.load(_f)
        # INTERNAL mode names a module: resolve its live out pair + destination
        # (orc1_config.fx_io_resolve), never a stored port that may be stale.
        try:
            _d = _cfg.fx_io_resolve(_d, _cfg.jack_phys_playback)
        except Exception:
            pass
        return {
            "FX_SEND_L":   _d.get("send_l", _CFG_FX_PORTS["FX_SEND_L"]),
            "FX_SEND_R":   _d.get("send_r", _CFG_FX_PORTS["FX_SEND_R"]),
            "FX_RETURN_L": _d.get("return_l", _CFG_FX_PORTS["FX_RETURN_L"]),
            "FX_RETURN_R": _d.get("return_r", _CFG_FX_PORTS["FX_RETURN_R"]),
        }
    except Exception:
        return dict(_CFG_FX_PORTS)


def _resolve_ports(port_list):
    """Substitute FX_SEND_*/FX_RETURN_* tokens with real JACK port names."""
    ports = _get_fx_ports()
    return [ports.get(p, p) for p in port_list]


def load_chains():
    with open(CHAINS_FILE) as f:
        data = json.load(f)
    # Skip any non-dict entries (e.g. a "_notes" documentation key)
    chains = {k: v for k, v in data.items() if isinstance(v, dict)}
    # Resolve FX port tokens in every plugin's jack_in / next_in fields.
    for chain in chains.values():
        for plugin in chain.get("plugins", []):
            if plugin.get("jack_in"):
                plugin["jack_in"] = _resolve_ports(plugin["jack_in"])
            if plugin.get("next_in"):
                plugin["next_in"] = _resolve_ports(plugin["next_in"])
    return chains


PALETTE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fx_palette.json")


def load_palette():
    """Return the installed-plugin palette from fx_palette.json.

    Each key is a short plugin name (last URI segment, e.g. "galactic"); each
    value is {uri, label, audio_in[], audio_out[], controls[]}.  This file is
    generated per-box by gen_fx_palette.py from the LV2 plugins actually
    installed — so the FX rack adapts to whatever effects are present rather
    than a hand-maintained list.  Returns {} if the file is missing.
    """
    try:
        with open(PALETTE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def jack_connect(src, dst):
    r = subprocess.run(["jack_connect", src, dst], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! jack_connect {src} → {dst}: {r.stderr.strip()}")
    else:
        print(f"  ✓ connected {src} → {dst}")


def jack_disconnect(src, dst):
    subprocess.run(["jack_disconnect", src, dst], capture_output=True)


def get_jalv_client_name(chain_name, plugin_idx):
    return f"fx-{chain_name}-{plugin_idx}"


def _wait_for_jack_ports(client_name, pid, timeout=10.0):
    """Poll jack_lsp until the given client's ports appear in JACK.

    Replaces the old fixed time.sleep(2.5).  On kernel 6.12 jalv registered
    in ~1-2s; on 6.18 it takes longer.  Polling means we proceed the instant
    ports are ready (fast on any kernel) and never time-out prematurely on a
    slower one.

    client_name:  JACK client name to watch for (e.g. "fx-nes_space-0")
    pid:          jalv process PID — if it dies we bail immediately
    timeout:      seconds before giving up (default 10; generous for slow boots)

    Returns True if ports appeared, False if we timed out or jalv died.
    """
    deadline = time.monotonic() + timeout
    search   = client_name + ":"   # e.g. "fx-nes_space-0:" — unique in jack_lsp output

    while time.monotonic() < deadline:
        # If jalv died there's nothing to wait for.
        if not os.path.exists(f"/proc/{pid}"):
            print(f"    jalv PID {pid} died before registering JACK ports")
            return False

        result = subprocess.run(["jack_lsp"], capture_output=True, text=True)
        if search in result.stdout:
            # Ports are live — report how long it actually took for the log.
            elapsed = timeout - (deadline - time.monotonic())
            print(f"  [{client_name}] JACK ports ready ({elapsed:.1f}s)")
            return True

        time.sleep(0.2)   # check 5× per second — responsive but not hammering

    print(f"  ! [{client_name}] ports never appeared in JACK after {timeout:.0f}s")
    return False


def spawn_jalv(client_name, uri):
    """Spawn ONE jalv instance for `uri` as JACK client `client_name`.

    This is the tricky bit shared by both the preset-chain path (start_chain,
    below) and the live FX rack (orc1_fx_rack.py).  Keeping it in one place
    means the FIFO-stdin trick and the RT-priority spawn can never drift
    between the two callers.

    Returns (pid, fifo_path) on success, or (None, None) if jalv never
    registered its JACK ports (caller should treat that as a failed start).

    ── FIFO stdin approach ───────────────────────────────────────────────────
    jalv reads parameter commands from stdin, one per line ("symbol = value").
    We feed it a named FIFO as stdin, held open by jalv itself so it never
    sees EOF and never exits.  The trick needs TWO execs:

        exec 3<>"$FIFO"          ← open FIFO O_RDWR as fd 3 (never blocks:
                                   same process is reader AND writer)
        exec jalv ... < "$FIFO"  ← open FIFO O_RDONLY as stdin (doesn't block
                                   because fd 3 already holds the write end)

    jalv inherits fd 3, so stdin always has a writer and never delivers EOF.
    Killing the jalv PID closes fd 3 and the FIFO drains.  A single combined
    "exec jalv < $FIFO 3>$FIFO" DEADLOCKS on kernel 6.18 — must be two execs.

    RT scheduling uses preexec_fn (a direct os.sched_setscheduler in the
    forked child) rather than 'chrt' — see _set_rt_priority for why.
    """
    fifo_path = _FIFO_TEMPLATE.format(client_name=client_name)
    jalv_args = " ".join(shlex.quote(a) for a in [JALV, "-n", client_name, uri])
    bash_cmd = (
        f"FIFO={shlex.quote(fifo_path)}; "
        f"mkfifo \"$FIFO\" 2>/dev/null; "
        f"exec 3<>\"$FIFO\"; "
        f"exec {jalv_args} < \"$FIFO\" 2>/dev/null"
    )
    proc = subprocess.Popen(
        ["bash", "-c", bash_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,      # detach from the calling process's session
        preexec_fn=_set_rt_priority,
    )
    print(f"  jalv started: {uri} (client {client_name}  PID {proc.pid})")
    if not _wait_for_jack_ports(client_name, proc.pid, timeout=10.0):
        return None, None
    return proc.pid, fifo_path


def start_chain(chain_name):
    chains = load_chains()
    state  = load_state()

    if chain_name not in chains:
        print(f"Unknown chain: {chain_name}")
        return False

    if chain_name in state and state[chain_name].get("pids"):
        # Verify the PIDs are actually alive before declaring it running
        alive = [p for p in state[chain_name]["pids"]
                 if os.path.exists(f"/proc/{p}")]
        if alive:
            print(f"Chain {chain_name} already running (PIDs {alive})")
            return True
        else:
            print(f"  Stale state for {chain_name} — cleaning up and restarting")
            _cleanup_fifos(state[chain_name])
            del state[chain_name]
            save_state(state)

    chain   = chains[chain_name]
    plugins = chain.get("plugins", [])
    pids    = []
    fifos   = []   # ← new: FIFO path per plugin, for send_param()

    if not plugins:
        print(f"Chain {chain_name} has no plugins defined — nothing to start")
        return False

    # If this chain is marked exclusive, stop everything else that's running first.
    if chain.get("exclusive", False):
        running = [n for n, info in state.items()
                   if n != chain_name and info.get("pids")
                   and any(os.path.exists(f"/proc/{p}") for p in info["pids"])]
        for other in running:
            print(f"  (exclusive) stopping {other}...")
            stop_chain(other)
        state = load_state()   # refresh after stopping

    print(f"Starting chain: {chain_name} — {chain.get('description','')}")

    # Spawn one jalv per plugin using the shared spawn_jalv helper (the
    # FIFO-stdin + RT-priority spawn now lives there so the rack shares it).
    for i, plugin in enumerate(plugins):
        client_name = get_jalv_client_name(chain_name, i)
        pid, fifo_path = spawn_jalv(client_name, plugin["uri"])
        if pid is None:
            print(f"  ! Aborting chain '{chain_name}' — "
                  f"jalv never became ready for plugin [{i}]")
            # Kill any siblings we already started so we don't leak jalv procs.
            for p in pids:
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            return False
        pids.append(pid)
        fifos.append(fifo_path)

    # Wire JACK connections now that all jalv instances are up
    print(f"  Wiring JACK connections...")
    for i, plugin in enumerate(plugins):
        for src, dst in zip(plugin.get("jack_in", []),
                            plugin.get("jack_out_to", [])):
            jack_connect(src, dst)
        for src, dst in zip(plugin.get("out_to_next", []),
                            plugin.get("next_in", [])):
            jack_connect(src, dst)

    # Save state — includes both PIDs (for lifecycle management) and FIFO paths
    # (so send_param() knows where to write parameter commands later).
    state[chain_name] = {"pids": pids, "fifos": fifos, "enabled": True}
    save_state(state)
    print(f"Chain {chain_name} running")
    return True


def stop_chain(chain_name):
    state = load_state()

    if chain_name not in state or not state[chain_name].get("pids"):
        print(f"Chain {chain_name} not running")
        return

    for pid in state[chain_name]["pids"]:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  Killed PID {pid}")
        except ProcessLookupError:
            pass

    # Clean up the FIFOs so they don't litter /tmp between runs.
    # mkfifo in start_chain uses 2>/dev/null so a fresh FIFO will always be created.
    _cleanup_fifos(state[chain_name])

    del state[chain_name]
    save_state(state)
    print(f"Chain {chain_name} stopped")


def _cleanup_fifos(chain_state):
    """Remove any FIFO files left from a running chain.  Silently ignores missing files."""
    for fifo_path in chain_state.get("fifos", []):
        try:
            os.unlink(fifo_path)
            print(f"  Removed FIFO: {fifo_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  ! Could not remove FIFO {fifo_path}: {e}")


def send_param(chain_name, plugin_idx, symbol, value):
    """Send a real-time parameter change to a running jalv plugin.

    Writes "symbol = value\\n" to the plugin's FIFO stdin.  jalv reads the
    command and applies the parameter change immediately to the running audio.

    chain_name:  name of the chain as it appears in fx_chains.json
    plugin_idx:  which plugin in the chain (0 = first, 1 = second in series chains)
    symbol:      LV2 port symbol for the parameter (e.g. "replace", "pressure")
    value:       float value to set

    Returns True on success, False if the chain isn't running or the FIFO can't be written.
    """
    state = load_state()
    if chain_name not in state:
        print(f"send_param: chain '{chain_name}' is not running")
        return False

    fifos = state[chain_name].get("fifos", [])
    if plugin_idx >= len(fifos):
        print(f"send_param: plugin_idx {plugin_idx} out of range "
              f"(chain '{chain_name}' has {len(fifos)} plugin(s))")
        return False

    fifo_path = fifos[plugin_idx]
    if not os.path.exists(fifo_path):
        print(f"send_param: FIFO not found: {fifo_path}  (jalv may have died)")
        return False

    # Build the jalv interactive command: "symbol = value\n"
    # jalv reads these with fgets() from its stdin (the FIFO) and applies them
    # to the plugin's control port immediately.  Values outside [min, max] are
    # clamped by jalv; we don't need to range-check here.
    cmd = f"{symbol} = {value:.6f}\n".encode()

    try:
        # Open the FIFO write end with O_NONBLOCK so we never block waiting for
        # jalv to read.  If jalv has died (no reader), this raises OSError(ENXIO).
        # If it's alive, the open succeeds immediately and the write is atomic
        # (our command is well under PIPE_BUF = 4096 bytes).
        fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, cmd)
        finally:
            os.close(fd)
        return True

    except OSError as e:
        import errno
        if e.errno == errno.ENXIO:
            print(f"send_param: jalv for '{chain_name}'[{plugin_idx}] has no reader "
                  f"— did the process die?")
        elif e.errno == errno.EAGAIN:
            print(f"send_param: FIFO buffer full for '{chain_name}'[{plugin_idx}] "
                  f"— encoder moving too fast?")
        else:
            print(f"send_param: {chain_name}[{plugin_idx}] {symbol}={value:.4f}: {e}")
        return False


def list_chains():
    chains = load_chains()
    state  = load_state()
    print(f"{'Chain':<20} {'Running':<10} Description")
    print("-" * 60)
    for name, chain in chains.items():
        running = "yes" if name in state and state[name].get("pids") else "no"
        print(f"{name:<20} {running:<10} {chain.get('description','')}")


def status():
    state = load_state()
    if not state:
        print("No chains running")
        return
    for name, info in state.items():
        pids  = info.get("pids",  [])
        fifos = info.get("fifos", [])
        alive = [p for p in pids if os.path.exists(f"/proc/{p}")]
        print(f"{name}: {len(alive)}/{len(pids)} plugins alive  "
              f"PIDs: {alive}  FIFOs: {fifos}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "start"  and len(sys.argv) > 2: start_chain(sys.argv[2])
    elif cmd == "stop" and len(sys.argv) > 2: stop_chain(sys.argv[2])
    elif cmd == "list":   list_chains()
    elif cmd == "status": status()
    elif cmd == "send" and len(sys.argv) > 5:
        # e.g.  fx_chain_manager.py send nes_space 0 replace 0.75
        send_param(sys.argv[2], int(sys.argv[3]), sys.argv[4], float(sys.argv[5]))
    else:
        print(__doc__)
