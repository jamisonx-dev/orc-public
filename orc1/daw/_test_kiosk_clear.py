#!/usr/bin/env python3
"""Regression suite for daw/kiosk_clear.sh and the start paths that call it.

Runs the REAL script under bash against stub `systemctl` / `pgrep` and a fake
logind sessions directory, so it touches nothing on the machine. Needs bash
(run on the Pi). `python3 -B daw/_test_kiosk_clear.py`
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "kiosk_clear.sh")
EXPECTED = 25
_pass = _fail = 0
_root = tempfile.mkdtemp(prefix="orc1_kc_test_")


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL: {name} {detail}")


class Rig:
    def __init__(self, name, states=None):
        self.d = os.path.join(_root, name)
        os.makedirs(os.path.join(self.d, "state"))
        os.makedirs(os.path.join(self.d, "sessions"))
        os.mkfifo(os.path.join(self.d, "sessions", "128.ref"))   # logind keeps these; grep on one blocks forever
        self.calls = os.path.join(self.d, "calls.log")
        open(self.calls, "w").close()
        for u, s in (states or {}).items():
            open(os.path.join(self.d, "state", u), "w").write(s)
        self._stub("systemctl", f'''#!/bin/bash
D={self.d}
case "$1" in
show) u="${{@: -1}}"; cat "$D/state/$u" 2>/dev/null || echo inactive ;;
stop) echo "stop $2" >> "$D/calls.log"; echo inactive > "$D/state/$2" ;;
esac
''')
        self._stub("pgrep", f'#!/bin/bash\n[ -e {self.d}/xorg ] && exit 0 || exit 1\n')

    def _stub(self, name, body):
        p = os.path.join(self.d, name)
        open(p, "w").write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def session(self, sid, tty, state="active"):
        p = os.path.join(self.d, "sessions", sid)
        open(p, "w").write(f"UID=1000\nTTY={tty}\nSTATE={state}\n")
        return p

    def run(self, *keep, wait_tenths=40, timeout=8):
        env = dict(os.environ, SYSTEMCTL=os.path.join(self.d, "systemctl"),
                   PGREP=os.path.join(self.d, "pgrep"),
                   ORC1_SESS_DIR=os.path.join(self.d, "sessions"),
                   WAIT_TENTHS=str(wait_tenths))
        t = time.monotonic()
        try:
            r = subprocess.run(["bash", SCRIPT, *keep], env=env, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:                  # a hang is a FAILED CHECK, not a crash
            r = subprocess.CompletedProcess([], 124, "", "HUNG")
        return r, time.monotonic() - t

    def stopped(self):
        return [l.split()[1] for l in open(self.calls).read().splitlines()]


def later(delay, fn):
    t = threading.Timer(delay, fn)
    t.start()
    return t


def main():
    # -- which kiosks get stopped ----------------------------------------------
    r = Rig("which", {"orc1-visuals": "active", "orc1-hud": "active"})
    res, _ = r.run("orc1-hud")
    check("stops the OTHER kiosk", r.stopped() == ["orc1-visuals"], r.stopped())
    check("never stops the unit about to start", "orc1-hud" not in r.stopped())
    check("exits 0", res.returncode == 0)

    r = Rig("none", {"orc1-visuals": "inactive"})
    r.run("orc1-hud")
    check("an inactive kiosk is left alone (no stop call)", r.stopped() == [])

    r = Rig("busy", {"orc1-visuals": "deactivating", "orc1-hud": "activating"})
    r.run("orc1-daw")
    check("a unit that is still starting or stopping is stopped too",
          sorted(r.stopped()) == ["orc1-hud", "orc1-visuals"], r.stopped())

    r = Rig("daw", {"orc1-visuals": "active", "orc1-hud": "active", "orc1-dj": "inactive"})
    r.run("orc1-daw")
    check("starting DAW mode clears BOTH the visuals and the HUD",
          sorted(r.stopped()) == ["orc1-hud", "orc1-visuals"], r.stopped())

    # -- waiting for the old session to really end ----------------------------------
    r = Rig("wait_sess", {"orc1-visuals": "active"})
    sf = r.session("77", "tty7", "closing")
    later(0.8, lambda: os.unlink(sf))
    res, took = r.run("orc1-hud")
    check("waits while a (closing) tty7 session exists", took >= 0.7, f"took {took:.2f}s")
    check("...and returns promptly once it is gone", took < 3.0, f"took {took:.2f}s")

    r = Rig("other_tty", {})
    r.session("5", "tty1")
    res, took = r.run("orc1-hud")
    check("a session on another tty does not block", took < 0.5, f"took {took:.2f}s")
    check("logind's .ref FIFOs in the sessions directory never hang the script",
          res.returncode == 0 and "HUNG" not in res.stderr, f"rc={res.returncode} {res.stderr[:40]}")

    r = Rig("wait_x", {})
    open(os.path.join(r.d, "xorg"), "w").close()
    later(0.8, lambda: os.unlink(os.path.join(r.d, "xorg")))
    res, took = r.run("orc1-hud")
    check("waits while Xorg is still running", took >= 0.7 and took < 3.0, f"took {took:.2f}s")

    # -- never blocks a start forever ----------------------------------------------
    r = Rig("timeout", {})
    r.session("9", "tty7")
    res, took = r.run("orc1-hud", wait_tenths=5)
    check("a stuck session times out (exit 0, a note on stderr)",
          res.returncode == 0 and "still busy" in res.stderr and took < 3.0,
          f"rc={res.returncode} took={took:.2f}")

    # -- the start paths really call it, BEFORE systemctl start ---------------------
    def before(fn, start_cmd):
        """True when an EXECUTABLE (non-comment) line calls kiosk_clear.sh and a later
        executable line runs `start_cmd` (a comment mentioning the helper must not count)."""
        lines = [l.strip() for l in open(os.path.join(HERE, fn), encoding="utf-8")]
        code = [i for i, l in enumerate(lines) if l and not l.startswith("#")]
        call = [i for i in code if "kiosk_clear.sh" in lines[i]]
        start = [i for i in code if start_cmd in lines[i]]
        return bool(call) and bool(start) and min(call) < min(start)
    check("hud.sh start clears the monitor before starting the HUD",
          before("hud.sh", "systemctl start orc1-hud"))
    check("visuals.sh start clears the monitor before starting the visuals",
          before("visuals.sh", "systemctl start orc1-visuals"))
    check("daw_mode.sh start clears the monitor before starting DAW/DJ",
          before("daw_mode.sh", 'systemctl start "$unit"'))

    # -- the kiosk UNITS: nothing may vhangup tty7 while xinit starts ------------------
    # 2026-10-12, kernel-traced: an ExecStartPost (cap-session) in a tty kiosk inherits
    # StandardInput=tty + TTYVHangup, forks together with xinit (Type=simple) and its
    # vhangup SIGHUPed xinit before PAM -> "Deactivated successfully", dark monitor.
    sysd = os.path.normpath(os.path.join(HERE, "..", "..", "systemd"))

    def directives(fn):
        out = []
        for l in open(os.path.join(sysd, fn), encoding="utf-8"):
            l = l.strip()
            if l and not l.startswith(("#", ";", "[")) and "=" in l:
                k, v = l.split("=", 1)
                out.append((k.strip(), v.strip()))
        return out

    kiosks = sorted(f for f in os.listdir(sysd) if f.endswith(".service")
                    and ("TTYPath", "/dev/tty7") in directives(f))
    check("the four VT7 kiosk units are found",
          kiosks == ["orc1-daw.service", "orc1-dj.service", "orc1-hud.service",
                     "orc1-visuals.service"], kiosks)
    for f in kiosks:
        d = directives(f)
        check(f"{f}: no ExecStartPost/ExecReload (they run beside xinit and hang up tty7)",
              not [k for k, _ in d if k in ("ExecStartPost", "ExecReload")])
        if any(k in ("MemoryHigh", "MemoryMax") for k, _ in d):
            check(f"{f}: its memory caps reach the X session via orc1-kiosk-cap@%N",
                  ("Wants", "orc1-kiosk-cap@%N.service") in d)
    cap = directives("orc1-kiosk-cap@.service")
    check("orc1-kiosk-cap@ has no tty (so it can never hang tty7 up)",
          not [k for k, _ in cap if k.startswith("TTY") or k in ("StandardInput", "PAMName")], cap)
    check("orc1-kiosk-cap@ runs cap-session on its kiosk, after the kiosk started",
          ("After", "%i.service") in cap and ("Type", "oneshot") in cap and
          any(k == "ExecStart" and v.endswith("daw_mode.sh cap-session %i.service") for k, v in cap))

    shutil.rmtree(_root, ignore_errors=True)
    total = _pass + _fail
    print(f"{_pass} passed, {_fail} failed (of {total})")
    if total != EXPECTED:
        print(f"COUNT DRIFT: ran {total}, EXPECTED = {EXPECTED}")
        return 1
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
