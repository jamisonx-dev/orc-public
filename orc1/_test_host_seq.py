#!/usr/bin/env python3
"""_test_host_seq.py -- the module-host SEQUENCES + DAW/DJ mode, on stubs.

2026-10-01 (review fixes). SKIPBACK COMES FIRST: stopping (or starting) the
six module hosts in one go made skipback-rec miss a deadline (measured
2026-09-28). This suite runs the REAL scripts -- orc1_hosts.sh,
orc1_gentle_restart.sh, daw/daw_mode.sh -- with `systemctl`, `sleep`,
`runuser` and `loginctl` replaced by PATH stubs that only LOG, and checks the
ORDER of what they would do:

  - no systemctl call ever stops or starts more than one host
  - stops 0.7 s apart in STOP order, starts 1 s apart in START order
  - DAW/DJ engines-off marker: gentle restart + web "restart synth" do NOT
    start the hosts; only `daw_mode.sh restore` does, and it removes the
    marker AFTER the last start
  - daw_mode.sh start/stop: one at a time (flock), activating/deactivating =
    busy (refused), status reports busy
  - cap-session never fails and finds the scope from the unit's MainPID
  - the web's /api/restart routes "synth"/"orc1" through the scripts (source)

Touches no real systemd, no /run (ORC1_RUN_DIR / ORC1_PROC point at temp
dirs), needs no root. Safe while the rig runs. Runs on the Pi (real flock)
and in Git Bash on the PC (a flock stub, mkdir-based, if none is installed).

Mutation-prove (each turns checks red): orc1_hosts.sh `stop_all` naming all
six in one call; restore's `rm -f "$MARK"` moved before the start; gentle
restart calling `start force`; daw_mode.sh's lock re-exec removed;
running_unit back to `is-active`; cap-session's final `exit 0` removed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED = 44
_res = []
HOSTS_STOP = "orc1-bbkit orc1-chip orc1-mono orc1-poly orc1-sampler orc1-drum".split()
HOSTS_START = "orc1-drum orc1-sampler orc1-poly orc1-chip orc1-mono orc1-bbkit".split()
BASH = shutil.which("bash") or "/bin/bash"


def check(name, ok, detail=""):
    _res.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def posix(p):
    if os.name != "nt":
        return p
    return subprocess.run([BASH, "-c", 'cygpath -u "$1"', "_", p],
                          capture_output=True, text=True).stdout.strip()


SYSTEMCTL = r'''#!/bin/bash
# stub: log every call (+ whether the engines-off marker exists), fake states
st="$STUB_STATE"
m=0; [ -e "$ORC1_RUN_DIR/orc1-daw-engines-off" ] && m=1
echo "systemctl $* marker=$m" >> "$STUB_LOG"
case "$1" in
show)
    prop=$3; u=$5
    case "$prop" in
    ActiveState) cat "$st/$u.state" 2>/dev/null || echo inactive ;;
    MainPID) cat "$st/$u.pid" 2>/dev/null || echo 0 ;;
    MemoryHigh|MemoryMax) cat "$st/$u.$prop" 2>/dev/null || echo infinity ;;
    esac
    ;;
start)
    u=$2
    [ -e "$st/$u.fail" ] && exit 1
    if [ -e "$st/$u.block" ]; then
        echo "BLOCKED $u" >> "$STUB_LOG"
        n=0                         # never hang the suite (a broken lock = 2 blocked starts)
        until [ -e "$st/release" ]; do
            n=$((n + 1)); [ $n -gt 300 ] && { echo "GAVE UP $u" >> "$STUB_LOG"; exit 1; }
            "$REAL_SLEEP" 0.05
        done
    fi
    echo active > "$st/$u.state"
    ;;
stop) echo inactive > "$st/$2.state" ;;
set-property) exit "$(cat "$st/setprop.rc" 2>/dev/null || echo 0)" ;;
is-active) echo active ;;
esac
exit 0
'''
SLEEP = '#!/bin/bash\necho "sleep $*" >> "$STUB_LOG"\n'
RUNUSER = '#!/bin/bash\necho "runuser $*" >> "$STUB_LOG"\nexit 1\n'
LOGINCTL = r'''#!/bin/bash
echo "loginctl $*" >> "$STUB_LOG"
case "$1" in
list-sessions) cat "$STUB_STATE/sessions" 2>/dev/null ;;
show-session) cat "$STUB_STATE/session-$5.state" 2>/dev/null || echo active ;;
esac
'''
# only where util-linux flock is missing (Git Bash): same CLI subset, mkdir lock
FLOCK = r'''#!/bin/bash
w=0; n=0; E=1
while :; do
    case "$1" in
    -w) w=$2; shift 2 ;;
    -n) n=1; shift ;;
    -s) shift ;;
    -E) E=$2; shift 2 ;;
    *) break ;;
    esac
done
f=$1; shift
touch "$f" 2>/dev/null
d="$f.stubdir"; t=0
until mkdir "$d" 2>/dev/null; do
    [ $n = 1 ] && exit "$E"
    t=$((t + 1)); [ $t -gt $((w * 20)) ] && exit "$E"
    "$REAL_SLEEP" 0.05
done
"$@"; rc=$?
rmdir "$d"
exit $rc
'''


class Rig:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="hostseq_")
        self.bin = os.path.join(self.root, "bin")
        self.run = os.path.join(self.root, "run")
        self.state = os.path.join(self.root, "state")
        self.proc = os.path.join(self.root, "proc")
        for d in (self.bin, self.run, self.state, self.proc):
            os.makedirs(d)
        self.log = os.path.join(self.root, "log")
        stubs = {"systemctl": SYSTEMCTL, "sleep": SLEEP, "runuser": RUNUSER,
                 "loginctl": LOGINCTL}
        if not shutil.which("flock"):
            stubs["flock"] = FLOCK
        for name, body in stubs.items():
            p = os.path.join(self.bin, name)
            with open(p, "w", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        real_sleep = subprocess.run([BASH, "-c", "command -v sleep"],
                                    capture_output=True, text=True).stdout.strip()
        self.env = dict(os.environ,
                        STUB_LOG=posix(self.log), STUB_STATE=posix(self.state),
                        ORC1_RUN_DIR=posix(self.run), ORC1_PROC=posix(self.proc),
                        REAL_SLEEP=real_sleep, ORC1_DAW_LOCK_WAIT="1")
        self.env.pop("ORC1_DAW_LOCKED", None)
        self.env.pop("ORC1_HOSTS_LOCKED", None)
        # PATH: stubs first (bash itself resolves the rest)
        cur = subprocess.run([BASH, "-c", 'echo "$PATH"'], capture_output=True,
                             text=True, env=self.env).stdout.strip()
        self.env["PATH"] = posix(self.bin) + ":" + cur
        got = subprocess.run([BASH, "-c", "command -v systemctl"], capture_output=True,
                             text=True, env=self.env).stdout.strip()
        if got != posix(self.bin) + "/systemctl":
            raise SystemExit(f"SAFETY: systemctl stub not first on PATH ({got!r}) -- abort")

    def marker(self, on=True):
        p = os.path.join(self.run, "orc1-daw-engines-off")
        if on:
            open(p, "w").close()
        elif os.path.exists(p):
            os.remove(p)

    def has_marker(self):
        return os.path.exists(os.path.join(self.run, "orc1-daw-engines-off"))

    def setstate(self, name, value):
        with open(os.path.join(self.state, name), "w", newline="\n") as f:
            f.write(value + "\n")

    def sh(self, script, *args, wait=True):
        cmd = [BASH, posix(os.path.join(HERE, script))] + list(args)
        if not wait:
            return subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        r = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def lines(self):
        try:
            with open(self.log) as f:
                return [l.rstrip("\n") for l in f]
        except FileNotFoundError:
            return []

    def calls(self, verb):
        """[(units, marker)] for every `systemctl <verb> ...`."""
        out = []
        for l in self.lines():
            parts = l.split()
            if len(parts) > 2 and parts[0] == "systemctl" and parts[1] == verb:
                out.append((parts[2:-1], parts[-1] == "marker=1"))
        return out

    def clear(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def host_seq(rig, verb):
    """The hosts `verb`ed, in order -- and whether every call named ONE unit."""
    cs = [c for c in rig.calls(verb) if any(u in HOSTS_STOP for u in c[0])]
    return [c[0][0] if len(c[0]) == 1 else c[0] for c in cs], all(len(c[0]) == 1 for c in cs)


def spaced(rig, verb, gap):
    """Every host `verb` call is followed by `sleep gap` before the next one."""
    ls = [l for l in rig.lines() if l.startswith(f"systemctl {verb} orc1-") or l.startswith("sleep")]
    ok = True
    for i, l in enumerate(ls):
        if l.startswith(f"systemctl {verb}"):
            ok &= i + 1 < len(ls) and ls[i + 1] == f"sleep {gap}"
    return ok


def test_hosts_sh():
    rig = Rig()
    try:
        rc, _ = rig.sh("orc1_hosts.sh", "stop")
        seq, single = host_seq(rig, "stop")
        check("hosts stop: one unit per call, in STOP order", single and seq == HOSTS_STOP, str(seq))
        check("... 0.7 s between each stop", spaced(rig, "stop", "0.7"))
        rig.clear()
        rc, _ = rig.sh("orc1_hosts.sh", "start")
        seq, single = host_seq(rig, "start")
        check("hosts start: one unit per call, in START order, 1 s apart",
              rc == 0 and single and seq == HOSTS_START and spaced(rig, "start", "1"), str(seq))
        rig.clear()
        rc, _ = rig.sh("orc1_hosts.sh", "restart")
        ls = [l for l in rig.lines() if l.startswith("systemctl")]
        s_seq, s1 = host_seq(rig, "stop")
        t_seq, t1 = host_seq(rig, "start")
        last_stop = max(i for i, l in enumerate(ls) if l.startswith("systemctl stop"))
        first_start = min(i for i, l in enumerate(ls) if l.startswith("systemctl start"))
        check("hosts restart: all six stopped one at a time, THEN started one at a time",
              s1 and t1 and s_seq == HOSTS_STOP and t_seq == HOSTS_START
              and last_stop < first_start and not rig.calls("restart"))
        rig.clear()
        rig.marker()
        rc, out = rig.sh("orc1_hosts.sh", "restart")
        check("hosts restart with the engines-off marker: touches nothing, exit 4",
              rc == 4 and not rig.calls("stop") and not rig.calls("start"), f"rc={rc}")
        rc, out = rig.sh("orc1_hosts.sh", "start")
        check("hosts start with the marker: skipped (exit 4), says why",
              rc == 4 and not rig.calls("start") and "DAW/DJ" in out, f"rc={rc} {out}")
        rc, _ = rig.sh("orc1_hosts.sh", "start", "force")
        seq, single = host_seq(rig, "start")
        check("hosts start force (the restore): starts them despite the marker",
              rc == 0 and single and seq == HOSTS_START)
        rc, _ = rig.sh("orc1_hosts.sh", "bogus")
        check("bad subcommand: usage, exit 2", rc == 2)
    finally:
        rig.close()


def test_gentle():
    rig = Rig()
    try:
        rc, _ = rig.sh("orc1_gentle_restart.sh")
        ls = [l for l in rig.lines() if l.startswith("systemctl")]
        s_seq, s1 = host_seq(rig, "stop")
        t_seq, t1 = host_seq(rig, "start")
        r = [i for i, l in enumerate(ls) if l.startswith("systemctl restart orc1")]
        check("gentle restart: hosts stopped one by one in STOP order", s1 and s_seq == HOSTS_STOP)
        check("... then orc1 restarted (once) ...", len(r) == 1
              and r[0] > max(i for i, l in enumerate(ls) if l.startswith("systemctl stop")))
        check("... then the hosts started one by one in START order",
              t1 and t_seq == HOSTS_START
              and min(i for i, l in enumerate(ls) if l.startswith("systemctl start")) > r[0])
        rig.clear()
        rig.marker()
        rc, _ = rig.sh("orc1_gentle_restart.sh")
        check("gentle restart during DAW/DJ engines-off: brain restarted, hosts NOT started",
              len(rig.calls("restart")) == 1 and not rig.calls("start"),
              str(rig.calls("start")))
        check("... and the marker is left for the app's restore", rig.has_marker())
    finally:
        rig.close()


def test_restore():
    rig = Rig()
    try:
        rc, _ = rig.sh("daw/daw_mode.sh", "restore")
        check("restore without the marker: a no-op", rc == 0 and not rig.lines())
        rig.marker()
        rc, _ = rig.sh("daw/daw_mode.sh", "restore")
        starts = rig.calls("start")
        seq, single = host_seq(rig, "start")
        check("restore: six starts, one at a time, START order", single and seq == HOSTS_START, str(seq))
        check("... the marker still EXISTS during every start (removed after the last)",
              starts and all(m for _, m in starts), str(starts))
        check("... and is gone once restore finishes", rc == 0 and not rig.has_marker())
    finally:
        rig.close()


def test_daw_start_stop():
    rig = Rig()
    try:
        rc, _ = rig.sh("daw/daw_mode.sh", "start", "dj")
        ls = [l for l in rig.lines() if l.startswith("systemctl stop") or l.startswith("systemctl start")]
        seq, single = host_seq(rig, "stop")
        check("start dj: engines stopped one at a time in STOP order", single and seq == HOSTS_STOP)
        check("... with the marker made BEFORE the first stop",
              all(m for _, m in rig.calls("stop")))
        check("... and orc1-dj started only after the last host stop",
              ls and ls[-1].startswith("systemctl start orc1-dj"), str(ls[-1:]))
        for st, want_rc in (("deactivating", 75), ("activating", 75), ("active", 0)):
            rig.clear()
            rig.setstate("orc1-dj.state", st)
            rc, out = rig.sh("daw/daw_mode.sh", "start")
            check(f"start while orc1-dj is {st}: refused (exit {want_rc}), nothing stopped/started",
                  rc == want_rc and not rig.calls("stop") and not rig.calls("start"),
                  f"rc={rc} {out.strip()}")
        rig.clear()
        rig.setstate("orc1-dj.state", "deactivating")
        rc, _ = rig.sh("daw/daw_mode.sh", "stop")
        check("stop while already deactivating: exit 0, no second stop",
              rc == 0 and not rig.calls("stop"))
        rig.setstate("orc1-dj.state", "activating")
        rc, _ = rig.sh("daw/daw_mode.sh", "stop")
        check("stop while still activating: busy (75), the app is not killed",
              rc == 75 and not rig.calls("stop"))
        rig.setstate("orc1-dj.state", "active")
        rig.clear()
        rc, _ = rig.sh("daw/daw_mode.sh", "stop")
        check("stop of a running Mixxx: asks it first (daw_close via runuser), then stops the unit",
              any(l.startswith("runuser") and "mixxx" in l for l in rig.lines())
              and [c[0] for c in rig.calls("stop")] == [["orc1-dj"]])
        # a start job that fails before the unit ever ran -> restore ourselves
        rig.setstate("orc1-dj.state", "inactive")
        rig.marker(False)
        rig.clear()
        rig.setstate("orc1-daw.fail", "1")
        rc, _ = rig.sh("daw/daw_mode.sh", "start")
        seq, _s = host_seq(rig, "start")
        check("start whose unit never starts: engines brought back, marker removed",
              rc == 1 and seq == HOSTS_START and not rig.has_marker(), f"rc={rc} {seq}")
    finally:
        rig.close()


def _status(rig):
    rc, out = rig.sh("daw/daw_mode.sh", "status")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"_raw": out}


def test_lock_and_status():
    rig = Rig()
    try:
        st = _status(rig)
        check("status idle: busy false, state inactive, active false",
              st.get("busy") is False and st.get("state") == "inactive"
              and st.get("active") is False, str(st))
        rig.setstate("orc1-dj.block", "1")      # `systemctl start orc1-dj` hangs
        a = rig.sh("daw/daw_mode.sh", "start", "dj", wait=False)
        t0 = time.monotonic()
        while "BLOCKED orc1-dj" not in rig.lines() and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        check("(first start reached `systemctl start orc1-dj` and is holding there)",
              "BLOCKED orc1-dj" in rig.lines())
        st = _status(rig)
        check("status while a start runs: busy true (the page keeps START/STOP disabled)",
              st.get("busy") is True, str(st))
        n_stops = len(rig.calls("stop"))
        rc, out = rig.sh("daw/daw_mode.sh", "start", "dj")
        check("a SECOND start meanwhile: refused (76, busy), stops nothing more",
              rc == 76 and len(rig.calls("stop")) == n_stops
              and len([c for c in rig.calls("start") if c[0] == ["orc1-dj"]]) == 1,
              f"rc={rc} {out.strip()}")
        rc, out = rig.sh("daw/daw_mode.sh", "stop")
        check("a STOP meanwhile: refused too (it would race the start)", rc == 76, f"rc={rc}")
        open(os.path.join(rig.state, "release"), "w").close()
        a.wait(30)
        check("the first start then completes normally", a.returncode == 0, str(a.returncode))
        st = _status(rig)
        check("status after: Mixxx active, not busy",
              st.get("active") is True and st.get("app") == "mixxx" and st.get("busy") is False,
              str(st))
        # the unit hooks never take the start/stop lock (no self-deadlock)
        src = open(os.path.join(HERE, "daw", "daw_mode.sh")).read()
        locked = src.split("case \"${1:-}\" in", 2)[1].split("esac", 1)[0]
        check("only start|stop take the lock (the unit's hooks run while start holds it)",
              locked.strip().startswith("start|stop)"), locked[:40])
    finally:
        rig.close()


def test_cap_session():
    rig = Rig()
    try:
        rc, _ = rig.sh("daw/daw_mode.sh", "cap-session", "orc1-dj.service")
        check("cap-session, no caps on the unit: nothing set", rc == 0 and not rig.calls("set-property"))
        rig.setstate("orc1-dj.service.MemoryHigh", "943718400")
        rig.setstate("orc1-dj.service.MemoryMax", "1153433600")
        rig.setstate("orc1-dj.service.pid", "4242")
        os.makedirs(os.path.join(rig.proc, "4242"))
        with open(os.path.join(rig.proc, "4242", "cgroup"), "w", newline="\n") as f:
            f.write("0::/user.slice/user-1000.slice/session-35.scope\n")
        # loginctl's FIRST tty7 session is an old one, still around
        rig.setstate("sessions", " 31 1000 pibox seat0 900 user tty7 no -\n"
                                 " 35 1000 pibox seat0 4242 user tty7 no -")
        rig.setstate("session-31.state", "online")
        rc, out = rig.sh("daw/daw_mode.sh", "cap-session", "orc1-dj.service")
        sp = rig.calls("set-property")
        rig.setstate("session-31.state", "closing")
        check("cap-session: the scope comes from the unit's MainPID (session-35, not the old 31)",
              rc == 0 and len(sp) == 1 and sp[0][0][1] == "session-35.scope", str(sp))
        rig.setstate("setprop.rc", "1")
        rc, out = rig.sh("daw/daw_mode.sh", "cap-session", "orc1-dj.service")
        check("cap-session: set-property FAILING still exits 0 (never stops Mixxx)",
              rc == 0 and "UNCAPPED" in out, f"rc={rc}")
        os.remove(os.path.join(rig.state, "setprop.rc"))
        os.remove(os.path.join(rig.proc, "4242", "cgroup"))
        rig.clear()
        rc, out = rig.sh("daw/daw_mode.sh", "cap-session", "orc1-dj.service")
        sp = rig.calls("set-property")
        check("cap-session fallback (no cgroup): skips the CLOSING tty7 session",
              rc == 0 and len(sp) == 1 and sp[0][0][1] == "session-35.scope", str(sp))
    finally:
        rig.close()


def test_sources():
    import ast
    web = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(web).body
              if isinstance(n, ast.FunctionDef) and n.name == "api_restart")
    seg = ast.get_source_segment(web, fn)
    check('web: "synth" restarts through orc1_hosts.sh restart (one at a time)',
          '"synth": [_HOSTS_SH, "restart"]' in web and "orc1/orc1_hosts.sh" in web)
    check('web: "orc1" restarts through orc1_gentle_restart.sh', '"orc1":  [_GENTLE_SH]' in web
          and "orc1_gentle_restart.sh" in web)
    i_mark = seg.find("os.path.exists(_DAW_OFF_MARK)")
    i_scr = seg.find("_RESTART_SCRIPTS[service]")
    i_sys = seg.find('"systemctl", "restart"')
    check("api_restart: engines-off refusal, then the scripts, BEFORE any plain systemctl restart",
          0 < i_mark < i_scr < i_sys and "        if service in _RESTART_SCRIPTS:\n" in seg,
          f"{i_mark} {i_scr} {i_sys}")
    # nothing else carries its own six-host loop any more
    loops = []
    for rel in ("orc1_gentle_restart.sh", os.path.join("daw", "daw_mode.sh")):
        s = open(os.path.join(HERE, rel)).read()
        if 'systemctl stop "$h"' in s or 'systemctl start "$h"' in s:
            loops.append(rel)
    check("gentle restart + daw_mode.sh use orc1_hosts.sh (no private host loops)",
          loops == [], str(loops))
    for rel in ("orc1_hosts.sh", "orc1_gentle_restart.sh", os.path.join("daw", "daw_mode.sh")):
        b = open(os.path.join(HERE, rel), "rb").read()
        if b"\r" in b:
            loops.append(rel)
    check("the shell scripts are LF (bash on the Pi chokes on CRLF)", loops == [], str(loops))


def main():
    print("module-host sequence + DAW/DJ mode suite (stubbed systemctl)")
    t0 = time.monotonic()
    test_hosts_sh()
    test_gentle()
    test_restore()
    test_daw_start_stop()
    test_lock_and_status()
    test_cap_session()
    test_sources()
    passed = sum(_res)
    print(f"\n{passed} passed, {len(_res) - passed} failed  ({time.monotonic() - t0:.1f}s)")
    if len(_res) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_res)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if passed == len(_res) else 1


if __name__ == "__main__":
    sys.exit(main())
