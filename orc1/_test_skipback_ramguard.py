#!/usr/bin/env python3
"""Skipback recorder -- SKIPBACK COMES FIRST (regression suite, 2026-09-23).

owner: "the skipback is the most important thing here. everything else can go
jump in a lake if the skipback isn't functioning." Measured the same day, the
recorder went silently OFF (while systemctl said "active") two ways:
  * the RAM guard tore down RUNNING rings on any dip under the 400 MB floor
    (a /patch bridge; one sampler grab), and refused to arm otherwise;
  * after a jackd restart the in-process reconnect retried forever, wedged on
    libjack's stale shm mapping.
This drives the REAL Recorder._apply_ram_guard (faked MemAvailable + ring
residency) and the REAL Recorder.run() loop (fake jack module).

Pure: no JACK server, no ring files, no config. Safe while the rig runs.
Mutation-prove: put the floor back into `blocked` -> checks 1-3 fail; make the
lost-connection branch reconnect instead of exiting -> check 7 fails (hangs
are caught by a timeout).
"""
import os
import sys
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "skipback"))
try:
    import jack  # noqa: F401
except Exception:
    sys.modules["jack"] = types.ModuleType("jack")
import skipback_recorder as SR

MB = 1024 * 1024
PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {label}" + (f"  -- {detail}" if detail else ""))


def cfg(tid, secs, stereo=True, name=None):
    return {"id": tid, "name": name or f"T{tid}", "armed": True,
            "stereo": stereo, "length_secs": secs, "dir": f"t{tid}"}


class _Trk:
    def __init__(self, c):
        self.cfg = dict(c)


def guard(cfgs, running, mem_avail_mb, resident):
    r = SR.Recorder.__new__(SR.Recorder)
    r.root = "/nonexistent"
    r._cfg_tracks = cfgs
    r._meminfo_warned = True
    r._blocked_logged = set()
    r._blocked = set()
    r.tracks = {tid: _Trk(next(x for x in cfgs if x["id"] == tid)) for tid in running}
    old = SR._mem_available_bytes, SR._ring_resident_bytes, SR.log
    SR._mem_available_bytes = lambda: int(mem_avail_mb * MB)
    SR._ring_resident_bytes = lambda p: resident.get(
        int(os.path.basename(os.path.dirname(p))[1:]), 0)
    SR.log = lambda *a, **k: None
    try:
        r._apply_ram_guard()
    finally:
        SR._mem_available_bytes, SR._ring_resident_bytes, SR.log = old
    return r._blocked, getattr(r, "_ram_low", set())


MAIN, T1 = cfg(0, 1200, True, "MAIN"), cfg(1, 900, False)

# 1. running rings + RAM under the floor: never stopped
b, low = guard([MAIN, T1], running=[0, 1], mem_avail_mb=250, resident={0: 0, 1: 0})
check(b == set(), "RAM under the floor never STOPS a running ring", f"blocked={b}")

# 2. AFTER A REBOOT: rings empty (must regrow), RAM tight -> armed anyway, warned
b, low = guard([MAIN, T1], running=[], mem_avail_mb=250, resident={})
check(b == set() and low == {0, 1},
      "after a reboot with tight RAM, empty rings ARM anyway (warned, not refused)",
      f"blocked={b} ram_low={low}")

# 3. a length change under pressure is armed too (skipback first)
b, low = guard([cfg(0, 1500, True, "MAIN")], running=[], mem_avail_mb=250, resident={})
check(b == set(), "a resized ring under RAM pressure is still armed", f"blocked={b}")

# 4. plenty of RAM: nothing warned
b, low = guard([MAIN, T1], running=[], mem_avail_mb=1400, resident={})
check(b == set() and low == set(), "with RAM to spare: armed, no warning",
      f"blocked={b} ram_low={low}")

# 5. the user's own 900 MB ring-size budget still applies
big = [cfg(0, 2600, True), cfg(1, 2600, True)]        # 499 MB each > 900 MiB
b, low = guard(big, running=[0, 1], mem_avail_mb=1400, resident={})
check(b == {1}, "the configured 900 MB ring budget still binds", f"blocked={b}")


# --- the run() loop: never wedge in-process; exit for a fresh process --------
class _JErr(Exception):
    pass


def _rec_for_run():
    r = SR.Recorder.__new__(SR.Recorder)
    r.config_path, r.src_path = "/nonexistent", None
    r._stop, r._jack_down = threading.Event(), threading.Event()
    r.client, r.tracks, r._cfg_mtime = None, {}, None
    r._writer_loop = lambda *a: None
    r._write_status = lambda: None
    r._teardown_track = lambda trk, delete_files=False: None
    r._process = r._on_shutdown = r._on_xrun = lambda *a: None
    return r


def run_with(fake_jack, rec, timeout=5.0):
    """Run rec.run() in a thread; return its SystemExit code, or 'HUNG'."""
    old = SR.jack, SR.migrate_legacy_config, SR.log, SR.JACK_RETRY_SECS, \
        SR.FRESH_PROCESS_AFTER_SECS
    SR.jack, SR.migrate_legacy_config, SR.log = fake_jack, (lambda *a: None), \
        (lambda *a, **k: None)
    SR.JACK_RETRY_SECS, SR.FRESH_PROCESS_AFTER_SECS = 0.01, 0.05
    out = {}

    def go():
        try:
            rec.run()
            out["code"] = "returned"
        except SystemExit as e:
            out["code"] = e.code
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    rec._stop.set()                                  # unstick a hung loop
    (SR.jack, SR.migrate_legacy_config, SR.log, SR.JACK_RETRY_SECS,
     SR.FRESH_PROCESS_AFTER_SECS) = old
    return out.get("code", "HUNG")


# 6. jackd never reachable: bounded retries, then exit for a fresh process
never = types.SimpleNamespace(JackError=_JErr)
never.Client = lambda *a, **k: (_ for _ in ()).throw(_JErr("server_failed"))
code = run_with(never, _rec_for_run())
check(code == SR.EXIT_RECONNECT,
      "no jackd: bounded retries, then EXIT so systemd starts a fresh process",
      f"exit={code}")


# 7. connected, then the server goes away mid-run: exit (never reconnect in-process)
class _Client:
    samplerate, blocksize = 48000, 1024
    made = 0

    def __init__(self, *a, **k):
        _Client.made += 1

    def set_process_callback(self, f): pass
    def set_shutdown_callback(self, f): pass
    def set_xrun_callback(self, f): pass
    def activate(self): pass
    def deactivate(self): pass
    def close(self): pass


lost = types.SimpleNamespace(JackError=_JErr, Client=_Client)
rec = _rec_for_run()
rec._reconcile = lambda: rec._jack_down.set()      # the server dies after connect
code = run_with(lost, rec)
check(code == SR.EXIT_RECONNECT and _Client.made == 1,
      "connection lost mid-run: EXIT for a fresh process, no in-process reconnect",
      f"exit={code} clients_made={_Client.made}")

EXPECTED = 7
print(f"{PASS} passed, {FAIL} failed  (expected {EXPECTED})")
sys.exit(1 if (FAIL or PASS + FAIL != EXPECTED) else 0)
