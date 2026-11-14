"""_test_netmode.py -- orc1_netmode.sh's boot fallback and its WATCHER (2026-11-03), driven against a
FAKE nmcli (and fake sleep / systemd-run / logger) in a temp folder: the real network is never touched.

After a power cut the router came back after the Pi; the boot fallback started the hotspot and the Pi
sat on it for 4 hours. Now the fallback leaves a marker and a watcher rejoins a known WiFi once it is
in range again; a hotspot chosen by hand (the button) is left alone.

    python3 _test_netmode.py          (needs bash + flock: run it on the Pi)
"""
import fcntl, json, os, shutil, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "orc1_netmode.sh")
EXPECTED = 10
passed, failed = [0], [0]


def check(cond, label, extra=""):
    (passed if cond else failed)[0] += 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else "  " + str(extra)))


# The fake NetworkManager: state.json = {"active": name|"", "visible_from": scan number the home WiFi
# appears at (0 = at once, -1 = never), "scans": n, "up_fails": bool}; known profiles: HomeWifi + pb-hotspot.
FAKE_NMCLI = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
st = json.load(open(S))
a = sys.argv[1:]
open(os.environ["FAKE_CALLS"], "a").write(" ".join(a) + "\n")
def visible():
    v = st.get("visible_from", -1)
    return v >= 0 and st.get("scans", 0) >= v
def save():
    json.dump(st, open(S, "w"))
j = " ".join(a)
if "con show --active" in j:
    if st["active"]: print(st["active"])
elif "GENERAL.STATE dev show eth0" in j:
    print("20 (unavailable)")
elif "GENERAL.STATE dev show wlan0" in j:
    print("100 (connected)" if st["active"] else "30 (disconnected)")
elif "GENERAL.CONNECTION dev show wlan0" in j:
    print(st["active"])
elif "802-11-wireless.ssid con show" in j:
    print(a[-1])
elif "NAME,TYPE,AUTOCONNECT-PRIORITY con show" in j:
    print("HomeWifi:802-11-wireless:10\npb-hotspot:802-11-wireless:0")
elif "NAME,TYPE con show" in j:
    print("HomeWifi:802-11-wireless\npb-hotspot:802-11-wireless")
elif "dev wifi list" in j:
    st["scans"] = st.get("scans", 0) + 1
    save()
    print("NetA\nNetB" + ("\nHomeWifi" if visible() else ""))
elif "con up pb-hotspot" in j:
    st["active"] = "pb-hotspot"; save()
elif "con up HomeWifi" in j:
    if visible() and not st.get("up_fails"):
        st["active"] = "HomeWifi"; save()
    else:
        sys.exit(10)
elif "con down pb-hotspot" in j:
    st["active"] = ""; save()
elif "dev connect" in j:
    if visible() and not st.get("up_fails"):
        st["active"] = "HomeWifi"; save()
    else:
        sys.exit(10)
'''

tmp = tempfile.mkdtemp(prefix="netmode_test_")
try:
    bin_ = os.path.join(tmp, "bin")
    os.makedirs(bin_)

    def stub(name, body):
        p = os.path.join(bin_, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    stub("nmcli", FAKE_NMCLI)
    stub("sleep", "#!/bin/sh\nexit 0\n")                          # (time passes instantly)
    stub("logger", "#!/bin/sh\nexit 0\n")
    stub("systemd-run", '#!/bin/sh\necho "systemd-run $*" >> "$FAKE_CALLS"\nexit 0\n')
    stub("sync", "#!/bin/sh\nexit 0\n")
    st_path, calls = os.path.join(tmp, "state.json"), os.path.join(tmp, "calls.log")
    marker, lock = os.path.join(tmp, "fallback"), os.path.join(tmp, "lock")
    env = dict(os.environ, PATH=bin_ + ":" + os.environ["PATH"], FAKE_STATE=st_path, FAKE_CALLS=calls,
               ORC1_NETMODE_FALLBACK=marker, ORC1_NETMODE_LOCK=lock, ORC1_NETMODE_LOG=os.path.join(tmp, "log"), ORC1_NETMODE_COMMON=os.path.join(tmp, "no_common.sh"),
               ORC1_NETMODE_WAIT="3", ORC1_NETMODE_FAST="0", ORC1_NETMODE_FAST_FOR="0", ORC1_NETMODE_SLOW="0")

    def state(**kw):
        d = {"active": "", "visible_from": -1, "scans": 0, "up_fails": False}
        d.update(kw)
        json.dump(d, open(st_path, "w"))
        open(calls, "w").close()

    def run(*args, timeout=60):
        return subprocess.run(["bash", SCRIPT] + list(args), env=env, capture_output=True, text=True, timeout=timeout)

    def now():
        return json.load(open(st_path))

    # 1. boot with no network in range: the hotspot, the marker, the watcher started
    state()
    r = run("auto")
    log = open(calls).read()
    check(now()["active"] == "pb-hotspot" and os.path.exists(marker) and "systemd-run" in log and " watch" in log,
          "auto, nothing in range: the hotspot comes up, marked as a FALLBACK, and the watcher is started", r.stdout[-300:])
    # 2. the watcher: the home WiFi comes back on the 3rd look -> it rejoins, the marker goes, it stops
    state(active="pb-hotspot", visible_from=3)
    r = run("watch")
    s = now()
    check(s["active"] == "HomeWifi" and not os.path.exists(marker) and s["scans"] >= 3,
          "watch: the home WiFi back in range (3rd look) -> rejoined by itself, the fallback over", (s, r.stdout[-300:]))
    check("con down pb-hotspot" not in open(calls).read().split("dev wifi list")[0] if "dev wifi list" in open(calls).read() else False,
          "...it LOOKED with the hotspot still up (never dropped it just to look)")
    # 3. a hotspot you picked by hand is left alone: ap clears the marker, the watcher stops at once
    open(marker, "w").close()
    state(active="HomeWifi", visible_from=0)
    run("ap")
    check(now()["active"] == "pb-hotspot" and not os.path.exists(marker), "ap by hand (the button): the hotspot, NOT marked as a fallback")
    state(active="pb-hotspot", visible_from=0)
    run("watch")
    check(now()["active"] == "pb-hotspot" and "con up HomeWifi" not in open(calls).read(),
          "...so the watcher leaves it alone even with the home WiFi in range")
    # 4. the button (toggle) while on a fallback hotspot: home WiFi, marker cleared
    open(marker, "w").close()
    state(active="pb-hotspot", visible_from=0)
    run("toggle")
    check(now()["active"] == "HomeWifi" and not os.path.exists(marker), "the button during a fallback: home WiFi, the fallback over")
    # 5. rejoin while a button press holds the lock: 'not yet' (exit 1), so the watcher keeps going
    open(marker, "w").close()
    state(active="pb-hotspot", visible_from=0)
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run("rejoin")
    check(r.returncode == 1 and os.path.exists(marker) and now()["active"] == "pb-hotspot",
          "rejoin while the button is busy: exit 1 (try later), the marker kept", r.returncode)
    # 6. the home WiFi seen but joining fails: back on the hotspot, still a fallback, still watched
    state(active="pb-hotspot", visible_from=0, up_fails=True)
    r = run("rejoin")
    check(r.returncode == 1 and os.path.exists(marker) and now()["active"] == "pb-hotspot",
          "rejoin fails (seen but won't join): back on the hotspot, still a fallback", (r.returncode, now()))
    # 7. the hotspot went down some other way: the watcher stops and clears the marker
    state(active="", visible_from=-1)
    run("watch")
    check(not os.path.exists(marker) and "con up" not in open(calls).read(), "the hotspot already down: the watcher just stops")
    # 8. boot with the home WiFi: no hotspot, no marker, no watcher
    state(active="HomeWifi", visible_from=0)
    run("auto")
    check(now()["active"] == "HomeWifi" and not os.path.exists(marker) and "systemd-run" not in open(calls).read(),
          "auto with the home WiFi up: nothing to do")
finally:
    shutil.rmtree(tmp)

total = passed[0] + failed[0]
print("%d passed, %d failed  (expected %d)" % (passed[0], failed[0], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if failed[0] == 0 and total == EXPECTED else 1)
