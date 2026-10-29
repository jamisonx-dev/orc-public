#!/usr/bin/env python3
"""The /fx rack must NOT come back by itself after a brain restart (owner, 2026-10-19).

Boot used to call RackManager.apply(), which resolves the module's return pair while the
card's ports may not exist yet: the rack output got wired to nothing and the module's dry
wire stayed cut (health: DEAD). Now boot only reaps stray jalv clients.

Drives the REAL reap_orphans_and_restore with a stub self (no JACK, no jalv).
    python3 _test_rack_boot.py
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_fx_rack as R

EXPECTED = 4
passed = failed = 0


def check(name, ok):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name)


class Stub:
    def __init__(self):
        self._lock = threading.RLock()
        self._runtime = [{"idx": 0}]
        self.applied = 0
        self.reaped = 0
        self.status_written = 0
        self.fired = 0

    def apply(self, done=None):
        self.applied += 1

    def _reap_stray_clients(self):
        self.reaped += 1

    def _write_status(self):
        self.status_written += 1

    def _fire(self, cb):
        self.fired += 1


def run(flag):
    old = R.RESTORE_AT_BOOT
    R.RESTORE_AT_BOOT = flag
    try:
        s = Stub()
        R.RackManager.reap_orphans_and_restore(s, done=lambda: None)
        return s
    finally:
        R.RESTORE_AT_BOOT = old


check("default is OFF", R.RESTORE_AT_BOOT is False)
s = run(False)
check("boot reaps strays but never applies the saved rack", s.reaped == 1 and s.applied == 0)
check("boot publishes an empty status and calls done", s.status_written == 1 and s.fired == 1 and s._runtime == [])
check("flag on = the old behaviour (apply)", run(True).applied == 1)

print("%d passed, %d failed  (expected %d)" % (passed, failed, EXPECTED))
sys.exit(0 if failed == 0 and passed == EXPECTED else 1)
