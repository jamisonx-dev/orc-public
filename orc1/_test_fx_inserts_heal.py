#!/usr/bin/env python3
"""
FX inserts self-heal (2026-09-24).

MEASURED on the rig: a jackd restart (now routine -- an L-12 power cycle or
auto-off triggers one) killed every fx insert, the source's host re-autoconnected
to dst and played DRY, and nothing restored the insert until the next brain
restart. InsertManager.heal() restarts any insert whose mixer has exited, once
jackd answers again; the brain runs heal_forever() on a thread.

Drives the REAL InsertManager.heal() and _pid_alive() with real child processes
(`true` / `sleep`). _start_insert, load_inserts and _jack_up are stubbed, so no
JACK client, jalv or config file is touched -- safe while the rig is in use.

Mutation-prove it: make _pid_alive a bare kill-0 (no waitpid) -> the zombie check
fails; restart without waiting for a quiet graph -> the settle checks fail; drop the brain's
heal_forever thread -> the wiring net fails.
"""
import os, sys, time, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_fx_inserts as FI

_p = _f = 0
def check(cond, label):
    global _p, _f
    if cond: _p += 1
    else: _f += 1; print(f"  FAIL: {label}")

def dead_child():
    # A raw fork, not subprocess.Popen: a GC'd Popen is quietly reaped by the
    # subprocess module itself, which hides the zombie case this test exists for
    # (found by the mutation run -- a kill-0-only _pid_alive passed with Popen).
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    time.sleep(0.2)                      # exited, NOT waited -> a real zombie
    return pid

def live_child():
    return subprocess.Popen(["sleep", "30"])

# ── _pid_alive ────────────────────────────────────────────────────────────────
lp = live_child()
check(FI._pid_alive(lp.pid) is True, "_pid_alive: a running child is alive")
check(FI._pid_alive(dead_child()) is False,
      "_pid_alive: an exited (zombie) child is DEAD -- kill-0 alone says alive")
check(FI._pid_alive(1) is True, "_pid_alive: a process we don't own (pid 1) is alive")
lp.kill(); lp.wait()

# ── heal() ────────────────────────────────────────────────────────────────────
CFG = [{"id": "a", "src": ["S:L", "S:R"], "dst": ["D:1", "D:2"], "chain": []},
       {"id": "b", "src": ["T:L", "T:R"], "dst": ["D:1", "D:2"], "chain": ["x"]}]
FI.load_inserts = lambda: CFG
GRAPH = {"g": frozenset({"system", "ORC1-Drum"})}   # None = jackd down
FI._jack_clients = lambda: GRAPH["g"]
CLOCK = {"t": 1000.0}
FI._now = lambda: CLOCK["t"]
FI.HEAL_SETTLE_S = 4.0

def mgr():
    m = FI.InsertManager(dbg=lambda *_: None)
    m.started = []
    m._start_insert = lambda slot, ins: m.started.append((slot, ins["id"]))
    return m

def rt(mix_pid, chain=()):
    return {"mix": mix_pid, "chain": list(chain), "name": "n",
            "mix_cc": 40, "level_cc": 41, "src": [], "dst": []}

def settle_and_heal(m):
    """First pass snapshots the graph, second (after the settle time, graph
    unchanged) restarts."""
    first = m.heal()
    CLOCK["t"] += 5.0
    return first, m.heal()

# dead mixer -> restarted at its CONFIG slot (keeps its CC pair), live one alone
m = mgr(); alive = live_child()
m._runtime = {"a": rt(alive.pid), "b": rt(dead_child())}
first, got = settle_and_heal(m)
check(first == [] and got == ["b"] and m.started == [(1, "b")],
      "heal restarts ONLY the insert whose mixer died, at its config slot (1)")
check("a" in m._runtime, "heal leaves a live insert alone")
alive.kill(); alive.wait()

# jackd down -> nothing restarted, insert kept for the next pass
m = mgr(); GRAPH["g"] = None
m._runtime = {"b": rt(dead_child())}
check(settle_and_heal(m) == ([], []) and m.started == [] and "b" in m._runtime,
      "heal waits while jackd is down (next pass retries)")
GRAPH["g"] = frozenset({"system", "ORC1-Drum"})
check(settle_and_heal(m)[1] == ["b"], "...and restarts it once jackd is up and quiet")

# graph still CHANGING (hosts coming up one by one) -> keep waiting
m = mgr()
m._runtime = {"b": rt(dead_child())}
m.heal(); CLOCK["t"] += 5.0
GRAPH["g"] = frozenset({"system", "ORC1-Drum", "ORC1-BBKit"})   # a host arrived
check(m.heal() == [], "settle: a client appearing restarts the wait (the BBKit xrun)")
CLOCK["t"] += 2.0
check(m.heal() == [], "settle: still waiting 2 s into the new quiet period")
CLOCK["t"] += 3.0
check(m.heal() == ["b"], "settle: restarted once the graph was unchanged >= HEAL_SETTLE_S")
# a stale snapshot from an earlier event must not skip the wait
m = mgr(); alive = live_child()
m._runtime = {"a": rt(alive.pid)}
m._graph, m._graph_since = GRAPH["g"], CLOCK["t"] - 100     # left over
m.heal()                                   # steady state -> snapshot forgotten
m._runtime = {"a": rt(dead_child())}
check(m.heal() == [], "settle: a stale snapshot is reset in steady state, so the wait applies")
alive.kill(); alive.wait()

# a chain process that outlived its mixer is killed before the restart
m = mgr(); chain = live_child()
m._runtime = {"b": rt(dead_child(), [chain.pid])}
settle_and_heal(m)
time.sleep(0.2)
check(chain.poll() is not None, "heal kills the orphaned jalv chain of a dead insert")

# a dead insert that is no longer configured is dropped, not restarted
m = mgr()
m._runtime = {"gone": rt(dead_child())}
check(settle_and_heal(m) == ([], []) and "gone" not in m._runtime and m.started == [],
      "a dead insert no longer in the config is dropped, not restarted")

# ── the startup reaper kills ONLY insert processes (2026-09-24) ───────────────
import tempfile
PROC = tempfile.mkdtemp()
FAKE = {
    101: ["python3", "-u", "/home/pibox/orc/orc1/orc1_fxmix.py", "--name",
          "ORC1-FX-poly", "--src", "a,b", "--dst", "c,d"],          # insert mixer
    102: ["/usr/bin/jalv", "-n", "fxins-poly-0", "urn:x"],           # insert plugin
    201: ["bash", "-c", "cd ~/orc/orc1 && python3 -m py_compile orc1_fxmix.py"],  # my ssh
    202: ["nano", "/home/pibox/orc/orc1/orc1_fxmix.py"],             # an editor
    203: ["python3", "-u", "/home/pibox/orc/orc1/orc1_fxmix.py", "--name",
          "ORC1-FXMix"],                                              # rack mixer
    204: ["/usr/bin/jalv", "-n", "fxrack-0", "urn:y"],               # rack plugin
    205: ["grep", "fxins-poly"],                                     # a search
}
for pid, argv in FAKE.items():
    os.makedirs(os.path.join(PROC, str(pid)))
    with open(os.path.join(PROC, str(pid), "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
os.makedirs(os.path.join(PROC, "self"))            # non-numeric entries are skipped
got = sorted(FI._insert_pids(PROC))
check(got == [101, 102], "reaper matches exactly the insert mixer + its plugin (got %s)" % got)
check(not set(got) & {201, 202, 203, 204, 205},
      "reaper spares ssh/editor/grep that NAME the file, the rack mixer and rack plugins")
_FIS = open(os.path.join(HERE, "orc1_fx_inserts.py"), encoding="utf-8").read()
check("$(pgrep" not in _FIS and "pgrep -f '" not in _FIS
      and _FIS.count("stop_insert_processes()") >= 2,
      "reap_and_apply + CLI stop use the exact matcher; no pgrep -f left")

# ordered stop: plugins FIRST, then mixers (simultaneous = jackd broken-pipe spam)
calls, gone = [], set()
def fake_kill(pid, sig):
    if sig == 0:
        if pid in gone:
            raise ProcessLookupError(pid)
        return
    calls.append(pid); gone.add(pid)
FAKE[103] = ["python3", "-u", "/x/orc1_fxmix.py", "--name", "ORC1-FX-chip"]   # 2nd mixer
FAKE[104] = ["/usr/bin/jalv", "-n", "fxins-chip-0", "urn:z"]                  # 2nd plugin
for pid in (103, 104):
    os.makedirs(os.path.join(PROC, str(pid)))
    with open(os.path.join(PROC, str(pid), "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in FAKE[pid]) + b"\0")
n = FI.stop_insert_processes(timeout=0.5, proc_root=PROC, kill=fake_kill)
check(n == 4 and set(calls[:2]) == {102, 104} and set(calls[2:]) == {101, 103},
      "ordered stop: both plugins signalled BEFORE both mixers (got %s)" % calls)
check(not set(calls) & {201, 202, 203, 204, 205},
      "ordered stop never signals the look-alikes")
os.makedirs(os.path.join(PROC, "999"))
with open(os.path.join(PROC, "999", "cmdline"), "wb") as f:
    f.write(b"/usr/bin/jalv\0-n\0fxins-z-0\0u\0")
with open(os.path.join(PROC, "999", "stat"), "w") as f:
    f.write("999 (jalv) Z 1 1 1")
never_dies = lambda pid, sig: calls.append(pid) if sig else None   # kill-0 always ok
# 999 is a ZOMBIE (exited, unreaped by its parent): it must not hold up the stop.
# Test it alone -- the never-dying fakes would legitimately wait their timeout.
import shutil
for pid in (101, 102, 103, 104):
    shutil.rmtree(os.path.join(PROC, str(pid)))
t0 = time.monotonic()
FI.stop_insert_processes(timeout=1.5, proc_root=PROC, kill=never_dies)
check(time.monotonic() - t0 < 1.0,
      "a ZOMBIE insert process counts as gone (the stop doesn't wait out its timeout)")

# ── wiring nets ───────────────────────────────────────────────────────────────
SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
check("target=fx_inserts.heal_forever" in SRC,
      "the brain starts the heal_forever thread")
FSRC = open(os.path.join(HERE, "orc1_fx_inserts.py"), encoding="utf-8").read()
check(FSRC.count("with self._lock:") >= 3,
      "apply / heal / reap_and_apply share the manager lock")

EXPECTED = 21
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or (_p + _f) != EXPECTED:
    sys.exit(1)
