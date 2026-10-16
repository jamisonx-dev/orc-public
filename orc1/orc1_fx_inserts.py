#!/usr/bin/env python3
"""
ORC-1 fx-insert manager -- arbitrary, independent, assignable fx INSERTS
(2026-09-03).
============================================================================

An INSERT places a wet/dry fx unit on ONE stereo source (a module output like
ORC1-Poly:out_L/R, OR a hardware input like system:capture_1/2 for a pisound fx
loop) and sends the blend to ONE destination. Many inserts run at once, each
independent -- NO summing/bridging between them (the per-module-independence
north star, applied to fx). Each insert is:

    src -> ORC1-FX-<id>:dry
    ORC1-FX-<id>:send -> [ jalv chain ] -> ORC1-FX-<id>:wet
    ORC1-FX-<id>:out  -> dst        (and the direct src->dst is cut)

This manager reads ~/.orc1_fx_inserts.json and, per insert, launches one
orc1_fxmix instance (which owns its own src/dst reconcile + wet/dry blend) plus a
jalv chain wired send -> p0 -> ... -> pN -> wet. Control: each insert gets a
unique CH_CTRL mix/level CC pair (CC_BASE + 2*slot); the brain's FXMIX_SET {id}
cmd emits them, the instance slews to them per-sample.

CONFIG  ~/.orc1_fx_inserts.json
    {"inserts": [
        {"id": "poly", "src": ["ORC1-Poly:out_L","ORC1-Poly:out_R"],
         "dst": ["system:playback_1","system:playback_2"],
         "chain": ["Reverb"]},
        ...]}
    'chain' is a list of palette plugin keys (empty = a bare wet/dry gain with no
    effect, i.e. dry passthrough). src/dst are JACK port-name pairs.

Standalone:  python3 orc1_fx_inserts.py apply | stop | status
"""

import os
import sys
import json
import time
import signal
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

INSERTS_FILE = os.path.expanduser("~/.orc1_fx_inserts.json")
FXMIX_PY     = os.path.join(HERE, "orc1_fxmix.py")
# Per-insert CH_CTRL mix/level CC pairs. 40+ is clear of the bus-level CCs (7-11),
# the bind CCs (20/21) and the legacy FXMix pair (3/4). slot i -> (40+2i, 41+2i).
CC_BASE      = 40

try:
    import fx_chain_manager as _fcm      # spawn_jalv, jack_connect, load_palette
    _FCM = True
except Exception:
    _FCM = False

# Reuse the rack's tested helpers instead of re-implementing them: _wire_bus
# tolerates mono/stereo mismatch (a mono plugin in an insert chain must not drop
# a channel), and _wait_for_ports is the shared jack_lsp port-waiter.
try:
    from orc1_fx_rack import _wire_bus as _rack_wire_bus
    from orc1_fx_rack import _wait_for_ports as _rack_wait_ports
except Exception:
    _rack_wire_bus = _rack_wait_ports = None


def load_inserts():
    try:
        with open(INSERTS_FILE) as f:
            d = json.load(f)
        ins = d.get("inserts", []) if isinstance(d, dict) else []
        return [i for i in ins if isinstance(i, dict) and i.get("id")
                and len(i.get("src", [])) == 2 and len(i.get("dst", [])) == 2]
    except Exception:
        return []


def _wait_ports(names, timeout=10.0):
    """Wait for JACK ports to appear. Delegates to the rack's shared poller
    (_wait_for_ports); short-sleep fallback only if the rack isn't importable."""
    if _rack_wait_ports:
        return _rack_wait_ports(names, timeout)
    time.sleep(1.0)
    return True


def _insert_pids(proc_root="/proc"):
    """PIDs of fx-INSERT processes only, matched on their exact arguments:
        mixer:   ... orc1_fxmix.py ... --name ORC1-FX-<id> ...
        plugin:  jalv -n fxins-<id>-<n> <uri>
    This replaced a `pgrep -f` regex on those two words, which matched ANY command line
    merely CONTAINING those words -- it killed the caller's own ssh session
    twice on 2026-09-24 (a `py_compile orc1_fxmix.py` in the same command), and
    would kill an editor with the file open, or the separate rack mixer
    (--name ORC1-FXMix, no dash after FX)."""
    me = os.getpid()
    pids = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return pids
    for e in entries:
        if not e.isdigit() or int(e) == me:
            continue
        try:
            with open(os.path.join(proc_root, e, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        pairs = list(zip(argv, argv[1:]))
        mixer = (any(os.path.basename(a) == "orc1_fxmix.py" for a in argv)
                 and any(k == "--name" and v.startswith("ORC1-FX-") for k, v in pairs))
        plugin = (os.path.basename(argv[0]).startswith("jalv")
                  and any(k == "-n" and v.startswith("fxins-") for k, v in pairs))
        if mixer or plugin:
            pids.append(int(e))
    return pids


def stop_insert_processes(timeout=2.0, proc_root="/proc", kill=None):
    """Stop every insert process in ORDER: jalv plugins first, then the mixers,
    each group given `timeout` s to close its JACK client cleanly. Returns the
    number signalled.

    Signalling a mixer and its plugin at the SAME instant (what systemd does to
    the brain's whole cgroup on `systemctl stop orc1`) made jackd log 20-40
    "Broken pipe / CheckRes / DeactivateClient write error" lines per restart,
    measured 2026-09-24 -- each side closing while jackd was still notifying the
    other. Either one alone closes cleanly (0 lines). Used by orc1.service's
    ExecStop (the brain has no SIGTERM handler of its own). `kill` is
    injectable for tests (default os.kill)."""
    kill = kill or os.kill

    def _alive(p):
        # A process that exited but whose parent (the brain) hasn't reaped it
        # yet is a zombie: kill-0 still succeeds, so check its state too.
        try:
            kill(p, 0)
        except OSError:
            return False
        try:
            with open(os.path.join(proc_root, str(p), "stat")) as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return True
    pids = _insert_pids(proc_root)
    plugins, mixers = [], []
    for p in pids:
        try:
            with open(os.path.join(proc_root, str(p), "cmdline"), "rb") as f:
                first = f.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        (plugins if os.path.basename(first).startswith("jalv") else mixers).append(p)
    n = 0
    for group in (plugins, mixers):
        for p in group:
            try:
                kill(p, signal.SIGTERM)
                n += 1
            except Exception:
                pass
        end = time.monotonic() + timeout
        while any(_alive(p) for p in group) and time.monotonic() < end:
            time.sleep(0.05)
    return n


def _pid_alive(pid):
    """True while `pid` is running. Reaps it if it is our exited child -- a
    child that exited stays a ZOMBIE until waited for, and os.kill(pid, 0)
    succeeds on a zombie, so a kill-0 check alone would call a dead mixer
    alive forever."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done != pid
    except ChildProcessError:            # not our child: fall back to kill-0
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:          # exists, owned by someone else
            return True
        except OSError:
            return False
    except Exception:
        return True                      # unsure -> don't restart


# heal() restarts a dead insert only once the JACK graph has been QUIET (same
# set of clients) for this long, so it never lands in the middle of the module
# hosts' staggered start and xruns them.
HEAL_SETTLE_S = 4.0
_now = time.monotonic


def _jack_clients():
    """frozenset of JACK client names right now, or None if jackd is down."""
    try:
        r = subprocess.run(["jack_lsp"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return frozenset(l.split(":", 1)[0] for l in r.stdout.splitlines() if ":" in l)


class InsertManager:
    """Spawns + wires the configured inserts. Tracks child PIDs so it can stop
    them. One orc1_fxmix process + one jalv per chain plugin, per insert."""

    def __init__(self, dbg=print):
        self.dbg = dbg
        self._runtime = {}   # id -> {"mix": pid, "chain": [pids], "name": str,
                             #        "mix_cc": int, "level_cc": int}
        # apply() (web FX_INSERT_APPLY thread), reap_and_apply() (brain start)
        # and heal() (its own thread) all mutate _runtime + spawn/kill processes.
        import threading as _th
        self._lock = _th.RLock()

    # -- CC addressing --------------------------------------------------------
    @staticmethod
    def ccs_for(slot):
        return CC_BASE + 2 * slot, CC_BASE + 2 * slot + 1

    def insert_ccs(self, insert_id):
        """The (mix_cc, level_cc) for a live insert id, or None."""
        r = self._runtime.get(insert_id)
        return (r["mix_cc"], r["level_cc"]) if r else None

    # -- lifecycle ------------------------------------------------------------
    def apply(self):
        with self._lock:
            cfg = load_inserts()
            want_ids = {i["id"] for i in cfg}
            # stop inserts no longer wanted
            for iid in list(self._runtime):
                if iid not in want_ids:
                    self._stop_insert(iid)
            # (re)start each wanted insert that isn't already running
            for slot, ins in enumerate(cfg):
                if ins["id"] not in self._runtime:
                    self._start_insert(slot, ins)

    # -- self-heal (2026-09-24) -----------------------------------------------
    # MEASURED: a jackd restart kills every insert's mixer (and its jalv chain),
    # and nothing brought them back until the next BRAIN restart -- the source's
    # host re-autoconnects to dst, so it plays DRY and the fx silently vanish.
    # jackd restarts are routine now (an L-12 power cycle or auto-off triggers
    # one via udev), so the manager heals itself: any insert whose mixer has
    # exited is restarted as soon as jackd answers again.
    def heal(self):
        """Restart any insert whose mixer process has exited. Returns the ids
        restarted. Does nothing while jackd is down, nor until it has been
        answering for HEAL_SETTLE_S: restarting straight away lands the insert
        in the middle of the module hosts' staggered start and xruns the graph
        (measured: 3 xruns = 3 skipback glitches). The next pass retries."""
        restarted = []
        with self._lock:
            dead = [iid for iid, r in self._runtime.items()
                    if not _pid_alive(r["mix"])]
            if not dead:
                # Steady state: forget the graph snapshot, so the settle wait
                # always starts from when a death is first SEEN.
                self._graph = None
                return restarted
            # Wait for a QUIET graph: jackd up and its set of clients unchanged
            # for HEAL_SETTLE_S. A fixed delay after "jackd up" was not enough --
            # measured 2026-09-24, the insert restarted 0.2 s before BBKit came
            # up and xrun'd it. The set stops changing once every host is in.
            clients = _jack_clients()
            now = _now()
            if clients is None:
                self._graph = None
                return restarted
            if clients != getattr(self, "_graph", None):
                self._graph, self._graph_since = clients, now
                return restarted
            if now - self._graph_since < HEAL_SETTLE_S:
                return restarted
            slots = {ins["id"]: (n, ins) for n, ins in enumerate(load_inserts())}
            for iid in dead:
                r = self._runtime.pop(iid)
                for pid in r["chain"]:          # a chain that outlived its mixer
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                    _pid_alive(pid)             # reap if it was our child
                if iid in slots:
                    self.dbg(f"[fxins] {iid}: mixer exited (jackd restart?) "
                             f"-- restarting the insert")
                    self._start_insert(*slots[iid])
                    restarted.append(iid)
        return restarted

    def heal_forever(self, interval=3.0):
        """Background loop for the brain: heal() every `interval` seconds."""
        while True:
            try:
                self.heal()
            except Exception as e:
                self.dbg(f"[fxins] heal error: {e}")
            time.sleep(interval)

    def _start_insert(self, slot, ins):
        iid = ins["id"]
        name = f"ORC1-FX-{iid}"
        mix_cc, level_cc = self.ccs_for(slot)
        # 1) the wet/dry mixer instance (owns its src/dst reconcile)
        p = subprocess.Popen(
            ["python3", "-u", FXMIX_PY, "--name", name,
             "--src", ",".join(ins["src"]), "--dst", ",".join(ins["dst"]),
             "--mix-cc", str(mix_cc), "--level-cc", str(level_cc)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        if not _wait_ports([f"{name}:send_L", f"{name}:wet_L"], 10.0):
            self.dbg(f"[fxins] {iid}: mixer ports never appeared; skipping chain")
            self._runtime[iid] = {"mix": p.pid, "chain": [], "name": name,
                                  "mix_cc": mix_cc, "level_cc": level_cc,
                                  "src": ins["src"], "dst": ins["dst"]}
            return
        # 2) the jalv chain: send -> p0 -> ... -> pN -> wet
        chain_pids = []
        prev = [f"{name}:send_L", f"{name}:send_R"]
        if _FCM:
            pal = _fcm.load_palette()
            for n, key in enumerate(ins.get("chain", []) or []):
                entry = pal.get(key)
                if not entry:
                    self.dbg(f"[fxins] {iid}: plugin '{key}' not in palette; skip")
                    continue
                client = f"fxins-{iid}-{n}"
                pid, fifo = _fcm.spawn_jalv(client, entry["uri"])
                if pid is None:
                    self.dbg(f"[fxins] {iid}: '{key}' failed to start")
                    continue
                chain_pids.append(pid)
                ins_ports = [f"{client}:{s}" for s in entry["audio_in"]]
                self._wire(prev, ins_ports)
                prev = [f"{client}:{s}" for s in entry["audio_out"]]
        self._wire(prev, [f"{name}:wet_L", f"{name}:wet_R"])
        self._runtime[iid] = {"mix": p.pid, "chain": chain_pids, "name": name,
                              "mix_cc": mix_cc, "level_cc": level_cc,
                              "src": ins["src"], "dst": ins["dst"]}
        self.dbg(f"[fxins] {iid}: {ins['src']} -> {ins.get('chain', [])} -> "
                 f"{ins['dst']}  (mix CC{mix_cc}/level CC{level_cc})")

    def _stop_insert(self, iid):
        r = self._runtime.pop(iid, None)
        if not r:
            return
        for pid in [r["mix"]] + r["chain"]:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        # Restore the source's DIRECT path. The killed mixer had CUT src->dst to
        # sit in the insert; without this, removing an insert leaves the source
        # silent, because a module host only auto-connects to dst at its OWN
        # startup (not when an insert on it goes away). Give the mixer's ports a
        # moment to drop first so the reconnect isn't racing the teardown.
        if _FCM and r.get("src") and r.get("dst"):
            time.sleep(0.3)
            for s, d in zip(r["src"], r["dst"]):
                _fcm.jack_connect(s, d)
        self.dbg(f"[fxins] stopped {iid} (restored {r.get('src')} -> {r.get('dst')})")

    def stop_all(self):
        for iid in list(self._runtime):
            self._stop_insert(iid)

    def reap_and_apply(self):
        """Kill any insert processes left by a PREVIOUS brain run, then apply the
        current config from scratch. Idempotent across brain restarts: the module
        hosts restart with the brain and re-auto-connect to their dst, so a source
        that was dropped from the config is not left silent (its old mixer, now
        reaped, had cut that path). Call once on brain start."""
        with self._lock:
            stop_insert_processes()   # plugins first, then mixers (_insert_pids)
            time.sleep(0.5)      # let them drop their JACK connections
            self._runtime.clear()
            self.apply()

    def _wire(self, src_ports, dst_ports):
        """Wire one bus to the next, mono/stereo-TOLERANT (a mono plugin in the
        chain must not drop a channel). Uses the rack's _wire_bus; a plain 1:1
        zip is only the fallback when the rack module isn't importable."""
        if _rack_wire_bus is not None:
            _rack_wire_bus(src_ports, dst_ports)
        elif _FCM:
            for s, d in zip(src_ports, dst_ports):
                _fcm.jack_connect(s, d)

    def status(self):
        return {iid: {"name": r["name"], "mix_cc": r["mix_cc"],
                      "level_cc": r["level_cc"],
                      "chain_procs": len(r["chain"])}
                for iid, r in self._runtime.items()}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "apply"
    m = InsertManager()
    if cmd == "apply":
        m.apply()
        print(json.dumps(m.status(), indent=2))
        # keep children alive: they are start_new_session, so they persist after
        # this process exits. (When run under the brain, the brain holds the mgr.)
    elif cmd == "status":
        print(json.dumps(m.status(), indent=2))
    elif cmd == "stop":
        # stop every insert mixer + its jalv plugins, plugins first (see
        # stop_insert_processes). Also orc1.service's ExecStop.
        print(f"stopped {stop_insert_processes()} insert process(es)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
