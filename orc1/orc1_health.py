#!/usr/bin/env python3
"""ORC-1 health check -- report what is ACTUALLY true on this box right now.

    python3 ~/orc/orc1/orc1_health.py          # problems only
    python3 ~/orc/orc1/orc1_health.py -v       # every check, including passes
    python3 ~/orc/orc1/orc1_health.py --json   # machine-readable, for the timer

WHY THIS EXISTS
---------------
This project's recurring failure is not broken code, it is UNCHECKED CLAIMS.
Something is built and verified once, the rig changes underneath it, and
nothing re-checks. The wreckage is always the same shape:

  * A pad or route names a JACK port that no longer exists. jack_connect
    fails, the failure is discarded, the pad still lights. (The SKP route pad
    was dead for TEN DAYS; out_L2/out_R2 the same; L12-C wants
    system:playback_5/6 on a card with four outputs.)
  * A service is `active` while delivering nothing -- a bridge wired to
    hardware unplugged weeks ago, or alsa_in on a dead stream. Every status
    screen says green. (Both pisound bridges ran for over a month connected
    to nothing; the Perform-VE pair burned 25% of the CPU streaming sixteen
    channels into a silence detector.)
  * A feature works when you set it up and is gone after the next restart,
    while its UI still draws it as active. (All twenty saved audio routes.)

So this script ASSERTS NOTHING. Every line is measured when it runs: port
existence from the live graph, ring motion from the write head actually
moving, signal from the bytes in the ring, MIDI wiring from ALSA. Where a
thing cannot be measured it says so instead of guessing.

CONTRACT -- READ-ONLY, safe to run while playing.
  * Writes exactly one file, and only when you ask: the JSON report, to the
    path given by --out (atomically). Nothing else. It never connects or
    disconnects a port, never restarts anything, never opens a JACK client of
    its own -- opening one on a live RT graph is the very risk this guards.
  * Reads orc1_fire.py's route tables via `ast` WITHOUT importing it, the
    same side-effect-free technique the chord test suite uses, so it cannot
    trip module-level hardware code.
  * Costs roughly 3 s per armed skipback track (it samples each ring twice,
    3 s apart, to prove the write head is really moving) plus up to 4 s per
    web page if orc1-web is hung -- so ~10 s on a healthy two-track rig, and
    noticeably longer when something is actually wrong.

ACKNOWLEDGING THINGS YOU HAVE ALREADY DECIDED
---------------------------------------------
A checker that keeps reporting things you have ruled on is one you stop
reading, and a checker you stop reading is the next thing that lies to you.
So every finding has a stable id, and ~/.orc1_health_expect.json can accept
any of them with a reason:

    { "graph.unconnected.pisound-in": "hat is always fitted; FX loop rehooks" }

Accepted findings print as [IDLE] with your reason and do not count as dead.
Nothing is acknowledged by default -- silence has to be chosen, per item, by
you.

Exit code: 0 if nothing is DEAD, 1 otherwise, so it can gate a deploy.
"""

import argparse
import array
import ast
import json
import math
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ORC1_DIR = os.path.join(HOME, "orc", "orc1")
TRACKS_JSON = os.path.join(HOME, ".orc1_skipback_tracks.json")
STATUS_JSON = os.path.join(HOME, ".orc1_skipback_status.json")
AUDIO_JSON = os.path.join(HOME, ".orc1_audio.json")
FX_IO_JSON = os.path.join(HOME, ".orc1_fx_io.json")
FX_RACK_JSON = os.path.join(HOME, ".orc1_fx_rack.json")
OVERRIDES_JSON = os.path.join(HOME, ".orc1_config_overrides.json")
STATE_JSON = os.path.join(HOME, "orc1_state.json")
EXPECT_JSON = os.path.join(HOME, ".orc1_health_expect.json")
SKIPBUF = os.path.join(HOME, ".skipbuf")

# The per-module audio hosts (the audio-host split retired orc1-synth). These
# lists named only orc1-synth until 2026-09-24, so a DEAD module host -- e.g.
# orc1-bbkit after it exited 0 on a jackd-restart race -- was reported by
# nothing: not SERVICES, not LOGS.
HOST_UNITS = ["orc1-drum", "orc1-sampler", "orc1-poly", "orc1-chip",
              "orc1-mono", "orc1-bbkit"]

UNITS = ["orc1", "orc1-jackd"] + HOST_UNITS + [
         "orc1-web", "orc1-system",
         "skipback-record", "bt-route", "pisound-in", "pisound-out",
         "performve-in", "performve-out", "performve-watchdog",
         "orc-bt-jack", "rtpmidid",
         # enabled controller services that shipped without a line here (2026-10-14): a dead one was invisible
         "orc1-firevu", "orc1-kikpad", "orc1-zoomfx"]

# Units whose journals are worth scanning for unrecovered errors.
LOG_UNITS = ["orc1", "orc1-jackd"] + HOST_UNITS + ["orc1-web", "skipback-record"]

# Log lines that are noisy but self-healing. A boot race where a client
# retries and then succeeds is not a fault, and reporting it as one is how a
# checker trains you to ignore it. Anything matching these is only reported
# if the unit ALSO failed to reach a working state.
TRANSIENT_LOG = (
    "retrying in",
    "jackd not reachable",
    "waiting for",
    "BDB2034",              # jackd's Berkeley param-DB warning; harmless
    "Cannot open DB environment",
)

# Log lines jackd prints alongside every xrun -- reported (and attributed) by the
# xrun section, so the LOGS section must not count the same event again.
XRUN_COMPANION = (
    "ProcessGraphAsyncMaster: Process error",
)

# Clients that are legitimately not audio destinations, so "connected to
# nothing" is not a finding for them.
NOT_A_DESTINATION = frozenset({"ve-watchdog"})

# Web pages that should exist. A 404 here means a template or route was lost.
# Kept in step with base.html's nav bar. This list was WRONG when first
# written -- it omitted /beatstep, so the check reported "12 of 12 pages OK"
# while never looking at one of them. A checker with an incomplete list is a
# checker that reports a clean bill of health it did not earn.
WEB_PAGES = ["/", "/synth", "/chip", "/drums", "/seq", "/sampler", "/skipback",
             "/fx", "/control", "/matrix", "/patch", "/routes", "/system",
             "/beatstep", "/rc505", "/pedal", "/pads", "/kikpad", "/visuals", "/cme", "/performve", "/hud", "/screens"]

VERBOSE = False
EXPECT = {}
RESULTS = []            # (level, id, message)


# ── finding plumbing ─────────────────────────────────────────────────────────

def _record(level, fid, msg):
    RESULTS.append({"level": level, "id": fid, "msg": msg})


def ok(fid, msg):
    _record("ok", fid, msg)


def warn(fid, msg):
    """A warning -- also acknowledgeable.

    Some permanently-true conditions are warnings, not dead findings: an
    output-only MIDI device, a deliberately redundant override. If those
    could not be silenced they would reappear every run forever, which is
    the alarm fatigue this tool exists to avoid.
    """
    if fid in EXPECT:
        _record("idle", fid, "%s  [accepted: %s]" % (msg, EXPECT[fid]))
    else:
        _record("warn", fid, msg)


def dead(fid, msg):
    """A dead finding, unless this exact id has been acknowledged."""
    if fid in EXPECT:
        _record("idle", fid, "%s  [accepted: %s]" % (msg, EXPECT[fid]))
    else:
        _record("dead", fid, msg)


def slug(text, n=40, collapse_digits=False):
    """Stable, id-safe fragment of a message.

    Finding ids must be UNIQUE per distinct problem: an id shared by two
    findings means acknowledging one silently suppresses the other, which
    would turn this tool into the very thing it exists to catch.

    Digits are KEPT by default, because in this domain they are the whole
    identity -- pulse1 vs pulse2, capture_13 vs capture_14, L12-A vs L12-B.
    An earlier version stripped them and collapsed exactly those pairs into
    one id; the self-check below caught it. Only free-text log messages pass
    collapse_digits=True, where a varying timestamp or count in the text
    would otherwise make the id differ between runs.
    """
    t = str(text).lower()
    if collapse_digits:
        t = re.sub(r"\d+", "#", t)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:n] or "x"


CARD_HEAD_WARN_S = 20.0     # card head this far behind: warn (cushion ~43.7 s)
CARD_HEAD_DEAD_S = 40.0     # ... this far: audio is about to be lost


def _card_motion(tid, ch, ring_bytes):
    """MOTION for a CARD track (2026-09-29). Its writer drains in BURSTS: the
    SD card stalls it for seconds, then it catches up faster than real time,
    so a 3 s pos-rate sample reads "too fast" or "frozen" on a healthy track
    (measured: t1 at ratio 1.109 while its 30 s rate was exact). Judge what
    matters instead, from the recorder's own heads.json (each head's pos +
    JACK frame + "t" come from one atomic tuple):
      * consistency: bytes moved == frames moved x frame size, exactly
      * keeping up: the head is recent enough that the ~43.7 s RT cushion
        still covers the stall (warn >= CARD_HEAD_WARN_S, dead >=
        CARD_HEAD_DEAD_S)
      * and it moves at all across a longer look when it looked stuck.
    Returns the head's pos (for the SIGNAL check), or None."""
    fid = "skip.t%s" % tid
    hpath = os.path.join(SKIPBUF, "heads.json")

    def head():
        with open(hpath) as f:
            h = json.load(f)
        return h["tracks"][str(tid)]

    try:
        a = head()
        time.sleep(3.0)
        b = head()
        if b["frame"] == a["frame"]:                 # stalled right now?
            time.sleep(12.0)
            b = head()
        age = time.time() - float(b.get("t", 0))
        fb = ch * 2
        dframes = (b["frame"] - a["frame"]) & 0xFFFFFFFF
        dbytes = (b["pos"] - a["pos"]) % ring_bytes
        if dframes == 0:
            dead(fid + ".frozen",
                 "CARD write head has NOT MOVED in 15 s (head %.0f s old) -- "
                 "the ring is frozen" % age)
        elif dbytes != (dframes * fb) % ring_bytes:
            dead(fid + ".consistency",
                 "CARD head moved %d bytes for %d frames (expected %d) -- "
                 "ring writes are not tracking the audio"
                 % (dbytes, dframes, dframes * fb))
        elif age >= CARD_HEAD_DEAD_S:
            dead(fid + ".behind",
                 "CARD writer is %.0f s behind -- past the RT cushion's "
                 "safe margin, audio is about to be dropped" % age)
        elif age >= CARD_HEAD_WARN_S:
            warn(fid + ".behind",
                 "CARD writer is %.0f s behind (cushion ~43 s) -- the card "
                 "is stalling hard" % age)
        else:
            ok(fid + ".motion",
               "CARD head advancing, byte-exact to its frames, %.1f s "
               "behind (cushion ~43 s)" % age)
            note("motion: CARD head %.1f s behind, bytes == frames x %d"
                 % (age, fb))
        return int(b["pos"])
    except Exception as e:
        warn(fid + ".motion", "could not measure CARD motion: %s" % e)
        return None


def note(msg):
    _record("note", None, msg)


def hdr(t):
    _record("hdr", None, t)


def counts():
    c = {"ok": 0, "warn": 0, "dead": 0, "idle": 0}
    for r in RESULTS:
        if r["level"] in c:
            c[r["level"]] += 1
    return c


def self_check():
    """Verify this script's own finding ids are unique.

    Two findings sharing an id means acknowledging one silently suppresses
    the other -- a checker that hides a real fault, which is precisely the
    failure this tool exists to catch. Rather than trust that it never
    happens, detect it and say so out loud.
    """
    seen, dupes = {}, set()
    for r in RESULTS:
        fid = r.get("id")
        if not fid or r["level"] in ("hdr", "note"):
            continue
        if fid in seen and seen[fid] != r["msg"]:
            dupes.add(fid)
        seen[fid] = r["msg"]
    for fid in sorted(dupes):
        _record("warn", None,
                "SELF-CHECK: finding id '%s' is used by more than one "
                "distinct finding -- acknowledging it would silence both. "
                "This is a bug in orc1_health.py, not in your rig." % fid)


def emit_text():
    marks = {"ok": "[ OK ]", "warn": "[WARN]", "dead": "[DEAD]",
             "idle": "[IDLE]"}
    for r in RESULTS:
        lv = r["level"]
        if lv == "hdr":
            print("\n" + "=" * 70)
            print("  " + r["msg"])
            print("=" * 70)
        elif lv == "note":
            print("        " + r["msg"])
        elif lv == "ok" and not VERBOSE:
            continue
        else:
            print("  %s %s" % (marks[lv], r["msg"]))

    c = counts()
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print("  %d passing, %d warning(s), %d accepted, %d DEAD"
          % (c["ok"], c["warn"], c["idle"], c["dead"]))
    if c["dead"]:
        print("\n  DEAD = names something that does not exist, or claims to")
        print("  work while doing nothing. These are the failures that look")
        print("  fine from every status screen.")
        print("\n  To accept one deliberately, put its id in")
        print("  %s with a reason:" % EXPECT_JSON)
        for r in RESULTS:
            if r["level"] == "dead":
                print('      "%s": "why this is fine"' % r["id"])
                break
    if not VERBOSE:
        print("\n  (-v to see everything that passed)")


def run(cmd, timeout=15):
    """Run a command, return (rc, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        return -1, str(e)


def read_json(path):
    """Load a JSON object, or None.

    Deliberately returns None for anything that is not a dict. A truthy
    non-dict (a JSON array at the top level -- a shape this project's state
    files have genuinely had) sails straight past an `or {}` guard and then
    raises AttributeError on the first .get(). A diagnostic that cannot be
    crashed by a malformed file is the whole point.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def read_json_list(path):
    """Load a JSON array, or None. Same reasoning as read_json()."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, list) else None


# ── JACK graph ───────────────────────────────────────────────────────────────

def jack_graph():
    """Parse the live graph -> (ports, conns, audio, up)."""
    rc, out = run(["jack_lsp", "-c"])
    if rc != 0:
        return set(), {}, set(), False

    conns, cur = {}, None
    for line in out.splitlines():
        if not line.strip():
            continue
        if line[0] not in " \t":
            cur = line.strip()
            conns[cur] = []
        elif cur:
            conns[cur].append(line.strip())

    # Audio ports only. A MIDI port with no JACK connection is normal here --
    # MIDI is routed through ALSA seq, not JACK -- so mixing the two produces
    # false alarms.
    audio = set()
    rc2, out2 = run(["jack_lsp", "-t", "-p"])
    if rc2 == 0:
        cur2 = None
        for line in out2.splitlines():
            if not line.strip():
                continue
            if line[0] not in " \t":
                cur2 = line.strip()
            elif cur2 and "audio" in line.lower():
                audio.add(cur2)

    return set(conns), conns, audio, True


def client_of(port):
    return port.split(":", 1)[0]


# ── sections ─────────────────────────────────────────────────────────────────

def sec_system():
    hdr("SYSTEM")
    try:
        with open("/proc/loadavg") as f:
            note("load average: " + " ".join(f.read().split()[:3]))
    except Exception:
        pass

    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.split()[0])
        avail = mem.get("MemAvailable", 0) // 1024
        note("MemAvailable: %d MB" % avail)
        # The skipback RAM guard refuses to arm a track below a 400 MB floor,
        # so this is the number that decides whether tracks can be added.
        if avail < 400:
            warn("sys.ram.low",
                 "MemAvailable %d MB is under the 400 MB floor the skipback "
                 "RAM guard needs -- it will REFUSE to arm tracks" % avail)
        else:
            ok("sys.ram", "memory headroom fine (%d MB)" % avail)
    except Exception:
        warn("sys.ram.unreadable", "/proc/meminfo unreadable")

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            c = int(f.read().strip()) / 1000.0
        note("SoC temperature: %.1f C" % c)
        if c >= 80:
            warn("sys.temp",
                 "%.1f C -- the Pi throttles near 80 C, which shows up as "
                 "xruns" % c)
        else:
            ok("sys.temp", "temperature fine (%.1f C)" % c)
    except Exception:
        pass

    rc, out = run(["df", "-h", SKIPBUF])
    if rc == 0 and len(out.splitlines()) > 1:
        f = out.splitlines()[-1].split()
        if len(f) >= 4:
            note("ring tmpfs: %s of %s used (%s free)" % (f[2], f[1], f[3]))

    rc, out = run(["df", "-h", "/"])
    if rc == 0 and len(out.splitlines()) > 1:
        f = out.splitlines()[-1].split()
        if len(f) >= 5:
            note("SD card: %s of %s used (%s free)" % (f[2], f[1], f[3]))
            try:
                if int(f[4].rstrip("%")) >= 90:
                    warn("sys.disk", "SD card %s full -- recordings and logs "
                                     "will start failing" % f[4])
                else:
                    ok("sys.disk", "SD card has room (%s free)" % f[3])
            except Exception:
                pass


def sec_audio_engine(up):
    hdr("AUDIO ENGINE (jackd)")
    if not up:
        dead("jack.down",
             "jack_lsp could not open a client. If audio is still playing, "
             "jackd is WEDGED -- it refuses new clients while existing ones "
             "keep running. Fix: sudo systemctl restart orc1-jackd")
        return

    rc, out = run(["ps", "-eo", "args"])
    line = next((l for l in out.splitlines() if "jackd " in l and "-d" in l
                 and "grep" not in l), None)
    if not line:
        dead("jack.missing",
             "jack_lsp answered but no jackd process found -- inconsistent")
        return

    # This rig writes `-dhw:L12` (no space), but a space-separated `-d hw:X`
    # is equally valid and appears in other launchers -- accept both, per the
    # hardware-agnostic rule. Getting this wrong would report a permanent
    # false "card mismatch" on a correctly configured box.
    m_card = re.search(r"-d\s*(hw:\S+)", line)
    m_rate = re.search(r"-r\s*(\d+)", line)
    m_per = re.search(r"-p\s*(\d+)", line)
    card = m_card.group(1) if m_card else "?"
    rate = int(m_rate.group(1)) if m_rate else 0
    per = int(m_per.group(1)) if m_per else 0
    note("running on %s at %d Hz, period %d" % (card, rate, per))

    # Config vs reality. The period key has been destroyed by a clobbering
    # write before, silently dropping jackd to the card default and costing
    # ~1 xrun/min for days. Comparing file to process is the only way to see.
    cfg = read_json(AUDIO_JSON)
    if cfg is None:
        note("no %s -- jackd used its fallback card order" % AUDIO_JSON)
    else:
        want_p = cfg.get("period")
        if want_p is None:
            warn("jack.period.missing",
                 "%s has no 'period' key -- jackd fell back to the card "
                 "default (%d). This key has been eaten by a clobbering "
                 "write before." % (AUDIO_JSON, per))
        else:
            try:
                want_p = int(want_p)
            except Exception:
                warn("jack.period.bad",
                     "%s has period=%r, which is not a number"
                     % (AUDIO_JSON, want_p))
                want_p = per
            if want_p != per:
                warn("jack.period.mismatch",
                     "config wants period %s, jackd is running %d -- restart "
                     "orc1-jackd to apply" % (want_p, per))
            else:
                ok("jack.period", "period matches config (%d)" % per)

        want_c = cfg.get("card")
        if want_c and want_c.lower() not in card.lower():
            warn("jack.card.mismatch",
                 "config wants card '%s', jackd is on '%s' -- the card is "
                 "resolved at jackd START only, so a device plugged in later "
                 "is not used until orc1-jackd restarts" % (want_c, card))
        elif want_c:
            ok("jack.card", "card matches config (%s)" % card)

    # xruns, attributed. "Whose fault" is far more useful than "how many".
    rc, out = run(["journalctl", "-u", "orc1-jackd", "-b", "--no-pager"], 30)
    clients = {}
    for l in out.splitlines():
        m = re.search(r"XRun: client = (\S+)", l)
        if m:
            clients[m.group(1)] = clients.get(m.group(1), 0) + 1
    total = sum(clients.values())
    note("xruns since boot: %d" % total)
    if total == 0:
        ok("jack.xruns", "0 xruns since boot")
    else:
        for c, n in sorted(clients.items(), key=lambda kv: -kv[1]):
            warn("jack.xruns.%s" % c,
                 "%d xrun(s) caused by '%s' -- a late client glitches the "
                 "WHOLE graph, including anything being recorded" % (n, c))


def sec_graph(ports, conns, audio):
    hdr("JACK GRAPH -- who is actually wired to something")
    by_client = {}
    for p in ports:
        if p not in audio:
            continue
        d = by_client.setdefault(client_of(p), {"ports": 0, "conns": 0})
        d["ports"] += 1
        d["conns"] += len(conns.get(p, []))

    for c in sorted(by_client):
        d = by_client[c]
        if d["conns"]:
            ok("graph.connected.%s" % c,
               "%s: %d audio ports, %d connection(s)" % (c, d["ports"], d["conns"]))
        elif c in NOT_A_DESTINATION:
            ok("graph.notdest.%s" % c,
               "%s: no connections, expected (not an audio destination)" % c)
        else:
            # The finding that caught both pisound bridges and both VE
            # bridges. Ports plus zero connections means per-period work on
            # the RT graph for nothing -- and alsa_in/alsa_out resample
            # against their own clock, which is expensive and can go late.
            dead("graph.unconnected.%s" % c,
                 "%s: %d audio ports, CONNECTED TO NOTHING -- costing CPU on "
                 "the real-time graph and moving no audio" % (c, d["ports"]))

    live = [c for c in sorted(by_client) if by_client[c]["conns"]]
    note("carrying audio: " + (", ".join(live) if live else "NOTHING"))


def _track_ports(trk):
    """Resolve a track's real port names from its CHANNEL COUNT.

    Deliberately not hardcoded: a stereo track registers t<id>_L/_R, a mono
    one a single t<id>_M -- and stereo is one click away on /skipback.
    Hardcoding the stereo pair is exactly how the route table came to name
    ports that did not exist.
    """
    tid = trk.get("id")
    if trk.get("stereo"):
        return ["skipback-rec:t%d_L" % tid, "skipback-rec:t%d_R" % tid]
    return ["skipback-rec:t%d_M" % tid]


def sec_skipback(ports, conns):
    hdr("SKIPBACK -- measured from the audio, not from systemctl")

    if run(["systemctl", "is-active", "skipback-record"])[1].strip() != "active":
        dead("skip.down", "skipback-record is not active -- THE RING RECORDER "
                          "IS NOT RUNNING")
        return
    note("skipback-record: active")

    # RT scheduling. Every JACK client's MAIN thread is SCHED_OTHER, so
    # `chrt -p <pid>` tells you nothing -- the callback thread is what must be
    # FIFO. This ran at normal priority for weeks unnoticed.
    pid = run(["systemctl", "show", "skipback-record", "-p", "MainPID",
               "--value"])[1].strip()
    if pid.isdigit():
        rc, out = run(["ps", "-L", "-o", "cls,rtprio", "-p", pid])
        rows = [l.split() for l in out.splitlines()[1:] if l.split()]
        fifo = [r for r in rows if r[0] == "FF"]
        if fifo:
            ok("skip.rt", "real-time callback thread (FIFO prio %s)" % fifo[0][1])
            note("real-time: yes (FIFO prio %s of %d threads)"
                 % (fifo[0][1], len(rows)))
        else:
            dead("skip.rt",
                 "NO real-time thread -- the recorder's JACK callback runs at "
                 "normal priority and will drop audio under load. Check "
                 "skipback-record.service.d/realtime.conf")
        try:
            with open("/proc/%s/status" % pid) as f:
                for line in f:
                    if line.startswith("VmLck"):
                        kb = int(line.split()[1])
                        if kb < 1024:
                            dead("skip.memlock",
                                 "only %d kB locked -- JACK's RT memlock "
                                 "request failed" % kb)
                        else:
                            ok("skip.memlock", "memory locked: %d kB" % kb)
        except Exception:
            pass

    cfg = read_json(TRACKS_JSON) or {}
    status = read_json(STATUS_JSON) or {}
    # `or []` rather than a .get default: a key that is PRESENT and null does
    # not trigger the default, and `for t in None` raises. This project's
    # state files have been half-written before.
    st_by_id = {t.get("id"): t for t in (status.get("tracks") or [])
                if isinstance(t, dict)}

    # A stale status file means the recorder stopped publishing -- which has
    # historically looked identical to everything being fine.
    ts = status.get("ts")
    if ts:
        age = time.time() - ts
        if age > 60:
            dead("skip.status.stale",
                 "status file is %.0f s old -- the recorder has stopped "
                 "publishing, so every UI reading it is showing the past"
                 % age)
        else:
            ok("skip.status", "status fresh (%.0f s old)" % age)

    tracks = [t for t in (cfg.get("tracks") or []) if isinstance(t, dict)]
    if not tracks:
        dead("skip.notracks", "no tracks configured in %s" % TRACKS_JSON)
        return

    for trk in tracks:
        tid = trk.get("id")
        ch = 2 if trk.get("stereo") else 1
        try:
            secs = int(trk.get("length_secs") or 0)
        except Exception:
            warn("skip.t%s.length" % tid,
                 "track %s has length_secs=%r, which is not a number"
                 % (tid, trk.get("length_secs")))
            continue
        note("")
        note("--- track %s '%s' (%s, %d s) ---"
             % (tid, trk.get("name", "?"), "stereo" if ch == 2 else "mono", secs))

        if not trk.get("armed"):
            note("disarmed -- not recording, by configuration")
            continue

        # Per-PORT ids, not per-track: on a stereo track a shared id would
        # let an acknowledgement for one channel blind-spot the other.
        # n=40, not 20: truncation happens AFTER digits are preserved, so a
        # short cap re-creates the collision the digit fix removed --
        # "performve-in:capture_1".."_8" all cut to the same 20 chars.
        for p in _track_ports(trk):
            pid_ = "skip.t%s.port.%s" % (tid, slug(p, 40))
            if p in ports:
                ok(pid_, "port %s exists" % p)
            else:
                dead(pid_,
                     "port %s does NOT exist -- the recorder never registered "
                     "it, nothing can reach this track" % p)

        resolved = (st_by_id.get(tid) or {}).get("resolved") or []
        if not resolved:
            dead("skip.t%s.nosource" % tid,
                 "no source resolved -- this track is recording nothing")
        for src in resolved:
            sid = "skip.t%s.source.%s" % (tid, slug(src, 40))
            if src not in ports:
                dead(sid, "source %s does not exist on this rig" % src)
            elif not any(src in conns.get(p, []) for p in _track_ports(trk)):
                dead(sid,
                     "source %s exists but is NOT connected to the track -- "
                     "status may still say 'recording'" % src)
            else:
                ok(sid, "source %s connected" % src)
        if resolved:
            note("source: " + ", ".join(resolved))

        # Ring geometry. A mismatch means the sampler reads with the wrong
        # stride -- heard as double speed an octave high, or a grab that
        # silently returns nothing.
        rpath = os.path.join(SKIPBUF, "t%s" % tid, "ring.raw")
        expect = secs * 48000 * ch * 2
        try:
            real = os.path.getsize(rpath)
        except Exception:
            dead("skip.t%s.ring" % tid, "ring file %s is missing" % rpath)
            continue
        if real != expect:
            dead("skip.t%s.geometry" % tid,
                 "ring is %d bytes but config (%d s x %dch) implies %d -- "
                 "grabs will read at the wrong stride"
                 % (real, secs, ch, expect))
        else:
            ok("skip.t%s.geometry" % tid, "ring geometry matches config")
            note("ring: %d MB, geometry matches config" % (real // 1048576))

        # MOTION -- the check that beats is-active. A recorder can be alive,
        # retrying, and reporting RECORDING while the write head has not moved
        # for minutes. That has happened here for 7 minutes straight.
        ppath = os.path.join(SKIPBUF, "t%s" % tid, "ring.pos")
        p1 = None
        if trk.get("store") == "card":
            p1 = _card_motion(tid, ch, real)       # its head, for SIGNAL below
        else:
          try:
            p0 = int(open(ppath).read().strip())
            t0 = time.time()
            time.sleep(3.0)
            p1 = int(open(ppath).read().strip())
            dt = time.time() - t0
            moved = (p1 - p0) % real
            rate = moved / dt
            want = 48000 * ch * 2
            ratio = rate / want
            if moved == 0:
                dead("skip.t%s.frozen" % tid,
                     "write head has NOT MOVED in %.1f s -- the ring is "
                     "frozen while the recorder reports recording" % dt)
            elif abs(ratio - 1.0) > 0.05:
                dead("skip.t%s.rate" % tid,
                     "ring advancing at %.0f B/s, expected %d (ratio %.3f) -- "
                     "not tracking real time" % (rate, want, ratio))
            else:
                ok("skip.t%s.motion" % tid,
                   "advancing at real time (ratio %.4f)" % ratio)
                note("motion: advancing at real time (ratio %.4f)" % ratio)
          except Exception as e:
            warn("skip.t%s.motion" % tid, "could not measure motion: %s" % e)

        # SIGNAL. A ring can advance perfectly and record digital silence --
        # a dead patch cable is indistinguishable from a healthy recorder in
        # every status field there is. Report the level; do NOT call silence a
        # fault, because this script cannot know whether anything is playing.
        if p1 is None:
            continue
        try:
            bps = 48000 * ch * 2
            peak = 0
            with open(rpath, "rb") as f:
                for back in (5, 60, 300):
                    off = (p1 - back * bps) % real
                    if off + bps > real:
                        continue
                    f.seek(off)
                    a = array.array("h")
                    a.frombytes(f.read(bps)[: bps // 2 * 2])
                    if a:
                        peak = max(peak, max(a), -min(a))
            db = -99.0 if peak <= 0 else 20 * math.log10(peak / 32768.0)
            if peak <= 4:
                note("signal: SILENT (peak %d/32768, %.1f dBFS) -- normal if "
                     "nothing is playing, a fault if something is" % (peak, db))
            else:
                ok("skip.t%s.signal" % tid, "signal present (%.1f dBFS)" % db)
                note("signal: present, peak %.1f dBFS" % db)
        except Exception as e:
            warn("skip.t%s.signal" % tid, "could not meter signal: %s" % e)




def _phys_playback():
    """Physical playback ports in JACK order -- the same parse as orc1_web's
    _mo_phys_ports() and the hosts' S.phys_playback_ports(), so all three agree
    on what 'out-3-4' means."""
    rc, out = run(["jack_lsp", "-t", "-p"])
    if rc != 0:
        return []
    res, cur = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        if line[0] not in " \t":
            cur = {"name": line.strip(), "audio": False, "inp": False, "phys": False}
            res.append(cur)
        elif cur is not None:
            s = line.strip().lower()
            if "audio" in s:
                cur["audio"] = True
            if s.startswith("properties:"):
                cur["inp"] = cur["inp"] or "input" in s
                cur["phys"] = cur["phys"] or "physical" in s
    return [p["name"] for p in res if p["audio"] and p["inp"] and p["phys"]]


def _reach_through_fx(have, side, conns, ports):
    """Every port a module output reaches, looking THROUGH effects in its path:

      * an fx INSERT: module -> ORC1-FX-<id>:dry_<side> -> ORC1-FX-<id>:out_<side>
      * the /fx RACK (INTERNAL mode, a slot on): module -> fx-rack-<n>:<in> ->
        ... -> fx-rack-<m>:<out> -> return. Plugin port names vary per plugin
        and a slot may be mono, so EVERY port of a rack client is followed (one
        hop at a time, visited set, so a chain of slots is walked to its end).

    Until 2026-10-01 only the insert hop was followed, so a module feeding an
    enabled rack read as DEAD ("not wired where saved")."""
    reach, todo, seen = set(have), list(have), set()
    while todo:
        d = todo.pop()
        client = d.split(":", 1)[0]
        if client in seen:
            continue
        if client.startswith("ORC1-FX-") and d.endswith(":dry_" + side):
            seen.add(client)
            nxt = set(conns.get(client + ":out_" + side, []))
        elif client.startswith("fx-rack-"):
            seen.add(client)
            nxt = set()
            for p in ports:
                if p.startswith(client + ":"):
                    nxt |= set(conns.get(p, []))
        else:
            continue
        nxt -= reach
        reach |= nxt
        todo.extend(nxt)
    return reach


def sec_module_outputs(ports, conns):
    """Is each module host wired where its SAVED output says (added 2026-09-24)?

    Post audio-host split, this is the routing fact that matters: each module
    owns one output pair, saved in ~/.orc1_module_outputs.json and applied by
    the host at start (autoconnect_module_out). A module that is running but
    not on its pair is silent where you expect it -- a status screen shows the
    host 'active' and nothing else. Extra wires (fan-out added on /patch) are
    the player's own and only noted."""
    hdr("MODULE OUTPUTS -- is each module wired where its saved setting says?")
    try:
        sys.path.insert(0, ORC1_DIR)
        import orc1_config as _cfg
        keys, names = _cfg.MODULE_OUT_KEYS, _cfg.MODULE_JACK_NAMES
    except Exception as e:
        warn("modout.config", "could not read module output config: %s" % e)
        return
    phys = _phys_playback()
    for key in keys:
        jk = names.get(key)
        if not jk:
            continue
        L, R = jk + ":out_L", jk + ":out_R"
        if L not in ports or R not in ports:
            note("%-8s %s not in the graph (host not running -- see SERVICES)"
                 % (key, jk))
            continue
        dest = _cfg.load_module_out(key)
        pair = _cfg.resolve_module_out(dest, phys)
        have_l, have_r = set(conns.get(L, [])), set(conns.get(R, []))
        if pair is None:
            if dest == "none":
                ok("modout.%s" % key, "%s: output set to NONE (renders, wired "
                   "nowhere) -- as saved" % key)
            else:
                warn("modout.%s" % key, "%s: no physical outputs to wire to" % key)
            continue
        label = dest or "default (main pair)"
        # An fx INSERT or the /fx RACK may sit in the path (both cut the
        # direct wire) -- correctly wired; follow them to where they land.
        via_l = _reach_through_fx(have_l, "L", conns, ports)
        via_r = _reach_through_fx(have_r, "R", conns, ports)
        if pair[0] in via_l and pair[1] in via_r:
            if (pair[0] not in have_l) or (pair[1] not in have_r):
                ok("modout.%s" % key, "%s -> fx -> %s / %s as saved (%s)"
                   % (key, pair[0], pair[1], label))
                continue
        if pair[0] in have_l and pair[1] in have_r:
            ok("modout.%s" % key, "%s -> %s / %s as saved (%s)"
               % (key, pair[0], pair[1], label))
        else:
            dead("modout.%s" % key,
                 "%s is saved to %s (%s / %s) but is NOT wired there -- live: "
                 "L->%s  R->%s. It is silent on its configured output. Fix: "
                 "/patch MODULE OUTPUTS -> RE-APPLY, or restart %s"
                 % (key, label, pair[0], pair[1],
                    ", ".join(sorted(have_l)) or "nothing",
                    ", ".join(sorted(have_r)) or "nothing", "orc1-" + key))
        extra = (have_l - {pair[0]}) | (have_r - {pair[1]})
        if extra:
            note("%-8s also wired to %s (fan-out, player's own)"
                 % (key, ", ".join(sorted(extra))))


def sec_midi():
    hdr("MIDI -- is every controller actually reaching the brain?")
    rc, out = run(["aconnect", "-l"])
    if rc != 0:
        warn("midi.unavailable", "aconnect unavailable -- cannot check MIDI")
        return

    # Parse clients and their connections. Client NUMBERS are not stable --
    # they are assigned in plug order and changed across today's reboot -- so
    # everything here matches by NAME.
    clients = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"client (\d+): '([^']*)' \[type=(\w+)", line)
        if m:
            cur = m.group(1)
            clients[cur] = {"name": m.group(2).strip(), "type": m.group(3),
                            "to": [], "from": []}
        elif cur:
            if "Connecting To:" in line:
                clients[cur]["to"] += re.findall(r"(\d+):\d+", line)
            elif "Connected From:" in line:
                clients[cur]["from"] += re.findall(r"(\d+):\d+", line)

    brain = {cid for cid, c in clients.items() if "RtMidi" in c["name"]}
    if not brain:
        dead("midi.brain",
             "no RtMidi clients -- orc1 has opened no MIDI ports at all")
        return

    hw = {cid: c for cid, c in clients.items()
          if c["type"] == "kernel" and c["name"] not in ("System", "Midi Through")}

    # Virtual (user-type) MIDI clients -- rtpmidid's network ports, a
    # Bluetooth bridge -- are real MIDI paths too. They are not held to the
    # same "must be wired to the brain" rule (many are endpoints in their own
    # right), but they are LISTED so this section has no silent blind spot.
    virt = [c["name"] for cid, c in clients.items()
            if c["type"] == "user" and "RtMidi" not in c["name"]]
    if virt:
        note("virtual MIDI clients present (not checked): " + ", ".join(virt))

    if not hw:
        note("no hardware MIDI devices present")
        return

    for cid, c in sorted(hw.items(), key=lambda kv: kv[1]["name"]):
        n = c["name"]
        into = [t for t in c["to"] if t in brain]
        outof = [f for f in c["from"] if f in brain]
        if into and outof:
            ok("midi.%s" % n, "%s: in + out both wired to the brain" % n)
            note("%-18s in + out wired" % n)
        elif into:
            ok("midi.%s" % n, "%s: input wired to the brain" % n)
            note("%-18s input wired (brain does not send to it)" % n)
        elif outof:
            warn("midi.%s" % n,
                 "%s: the brain SENDS to it but reads nothing from it" % n)
        else:
            # Present on USB, invisible to the instrument. This is what a
            # controller that "just stopped working" looks like.
            dead("midi.%s" % n,
                 "%s is connected to the Pi but NOT wired to the brain -- it "
                 "is plugged in and the instrument cannot hear it" % n)


def sec_logs():
    hdr("LOGS -- unrecovered errors since boot")
    now = time.time()
    for u in LOG_UNITS:
        # -o short-unix so every line carries an epoch timestamp. WHEN an error
        # last happened is as important as whether it happened: a burst of
        # jackd "Process error" / "CheckRead error" lines during a service
        # restart is an artifact of that restart, and reporting it hours later
        # with no age reads exactly like a live fault. An undated error log is
        # how a checker starts crying wolf.
        rc, out = run(["journalctl", "-u", u, "-b", "--no-pager",
                       "-o", "short-unix"], 30)
        if rc != 0:
            continue
        # IGNORECASE matters: this project's own most-cited failure prints
        # ALL-CAPS ("ERROR system:playback_5 not a valid port"), as does
        # Python's logging.error(). A case-sensitive match would scroll the
        # exact class of bug this tool exists for straight past.
        pat = r"traceback|exception|\[warn\]|! Fire|\berror\b"
        hits = [l for l in out.splitlines() if re.search(pat, l, re.IGNORECASE)]
        # Only errors from the RUN THAT IS LIVE NOW are live faults. systemd
        # tags every line with the unit's invocation id; a line from an earlier
        # run belongs to a process that has since been restarted (a deliberate
        # jackd restart, an L-12 power cycle). Counting the whole boot reported
        # 6 "DEAD" jackd findings after ordinary L-12 power cycles (2026-09-24)
        # -- crying wolf. A live jackd that logged "read error, stopping" and is
        # still that same process stays DEAD, which is exactly the freeze.
        # No invocation id (unit not running) -> whole boot, as before.
        inv = run(["systemctl", "show", "-p", "InvocationID", "--value", u])[1].strip()
        if inv:
            rc2, cur = run(["journalctl", "_SYSTEMD_INVOCATION_ID=%s" % inv,
                            "-b", "--no-pager", "-o", "short-unix"], 30)
            if rc2 == 0:
                live = set(cur.splitlines())
                earlier = [l for l in hits if l not in live]
                hits = [l for l in hits if l in live]
                if earlier:
                    note("%-16s %d error line(s) from EARLIER runs of this unit "
                         "(restarted since -- history, not live)"
                         % (u, len(earlier)))
        # A boot race that retried and then succeeded is not a fault, and
        # reporting it as one trains you to ignore this section.
        # jackd logs "ProcessGraphAsyncMaster: Process error" right after EVERY
        # "XRun: client = X" line -- it is the same event. The xrun section
        # already reports it (a WARN, attributed to the client); counting it here
        # as well made one startup xrun read as a DEAD fault.
        hits = [l for l in hits if not any(t in l for t in XRUN_COMPANION)]
        real = [l for l in hits if not any(t in l for t in TRANSIENT_LOG)]
        transient = len(hits) - len(real)
        if not hits:
            ok("log.%s" % u, "%s: clean" % u)
            continue
        if transient:
            note("%-16s %d transient (retried and recovered)" % (u, transient))
        if not real:
            ok("log.%s" % u, "%s: only transient startup retries" % u)
            continue
        # One id per DISTINCT message, so acknowledging a known-benign line
        # cannot silence a different, genuinely new error from the same unit.
        # Repeats of the same message collapse into one finding with a count.
        groups = {}
        for l in real:
            msg = l.split("]: ", 1)[-1].strip()[:150]
            try:
                when = float(l.split(None, 1)[0])
            except Exception:
                when = 0.0
            g = groups.setdefault(slug(msg, 48, collapse_digits=True),
                                  [msg, 0, 0.0])
            g[1] += 1
            g[2] = max(g[2], when)
        for key, (msg, n, when) in list(groups.items())[:6]:
            bits = []
            if n > 1:
                bits.append("x%d" % n)
            if when:
                age = max(0.0, now - when)
                bits.append("last %s" % (
                    "%.0fs ago" % age if age < 90 else
                    "%.0fm ago" % (age / 60) if age < 5400 else
                    "%.1fh ago" % (age / 3600)))
            suffix = "  (%s)" % ", ".join(bits) if bits else ""
            dead("log.%s.%s" % (u, key), "%s: %s%s" % (u, msg, suffix))
        if len(groups) > 6:
            note("%-16s ... and %d more distinct message(s)"
                 % (u, len(groups) - 6))


def _config_defaults():
    """Read orc1_config's DEFAULTS from source, without importing it.

    Importing is wrong here, not merely impure: orc1_config APPLIES the
    override file at import time (its "RUNTIME OVERRIDES" block). So the
    imported value of an overridden key IS the override, and comparing the
    two can only ever report "identical to the default" -- which is exactly
    the false positive an earlier version of this function produced for all
    three of the live overrides. Reading the literal from source is the only
    way to see what the default actually is.

    Returns (values, names). `names` is every top-level name the module
    assigns; `values` only those whose value is a plain literal. The two are
    NOT the same and must not be conflated: SYNTH and SAMPLER are built from
    module-level constants rather than written as literals, so they never
    evaluate -- and an earlier version read "could not evaluate" as "key does
    not exist" and reported two perfectly live overrides as dead. Absent from
    `names` means genuinely gone; present in `names` but not `values` means
    this checker cannot tell, and it must say so instead of guessing.
    """
    values, names = {}, set()
    try:
        with open(os.path.join(ORC1_DIR, "orc1_config.py")) as f:
            tree = ast.parse(f.read())
    except Exception:
        return values, names
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
                try:
                    values[t.id] = ast.literal_eval(node.value)
                except Exception:
                    pass
    return values, names


def sec_config():
    hdr("CONFIG OVERRIDES -- do they still point at real keys?")
    ov = read_json(OVERRIDES_JSON)
    if ov is None:
        # Distinguish "no file" (the healthy default) from "file present but
        # unreadable" -- the second is a real anomaly and collapsing them into
        # one reassuring line is how a checker starts lying.
        if os.path.exists(OVERRIDES_JSON):
            dead("cfg.corrupt",
                 "%s exists but is not a readable JSON object -- every "
                 "override in it is being ignored" % OVERRIDES_JSON)
        else:
            note("no overrides set")
        return
    if not ov:
        note("no overrides set (file present but empty)")
        return
    defaults, names = _config_defaults()
    if not names:
        warn("cfg.unreadable", "could not read orc1_config.py")
        return

    for key, val in sorted(ov.items()):
        parts = key.split(".")
        if parts[0] not in names:
            # Genuinely absent from the module: this override does nothing,
            # forever, while still showing on /system as if it were in force.
            dead("cfg.orphan.%s" % slug(key),
                 "override '%s' = %r targets '%s', which orc1_config no "
                 "longer defines -- it has no effect but still shows as set"
                 % (key, val, parts[0]))
            continue
        try:
            cur = defaults[parts[0]]
            for p in parts[1:]:
                cur = cur[p]
        except Exception:
            # The name exists but its default is not a readable literal. Say
            # that plainly; do NOT promote "I cannot tell" into a finding.
            note("%-28s %r   (set; default not statically readable)"
                 % (key, val))
            continue
        if cur == val:
            warn("cfg.redundant.%s" % slug(key),
                 "override '%s' = %r is identical to the default -- harmless, "
                 "but it is not overriding anything" % (key, val))
        else:
            ok("cfg.%s" % slug(key), "override '%s' = %r (default %r)"
               % (key, val, cur))
            note("%-28s %r   (default %r)" % (key, val, cur))
    note("NOTE: overrides apply at PROCESS START -- a changed value is not "
         "live until the owning service restarts.")


def sec_state():
    hdr("STATE FILE")
    try:
        age = time.time() - os.path.getmtime(STATE_JSON)
    except Exception:
        dead("state.missing", "%s does not exist" % STATE_JSON)
        return
    st = read_json(STATE_JSON)
    if st is None:
        dead("state.corrupt",
             "%s is not valid JSON -- every web page reading it is blank"
             % STATE_JSON)
        return
    note("last written %.0f s ago, %d keys" % (age, len(st)))
    # This file is event-written, not periodic, so age alone is not a fault.
    # But print_status() writes it as its LAST statement -- so if that
    # function ever raises, the file freezes and every /cmd reply reports
    # failure for commands that actually ran. Hours of staleness is the tell.
    if age > 7200:
        warn("state.stale",
             "state file is %.1f h old. It is event-written so idle time is "
             "normal, but print_status() writes it last -- a freeze here has "
             "historically meant that function is raising." % (age / 3600.0))
    else:
        ok("state.fresh", "state file written %.0f s ago" % age)


def sec_fx(ports):
    hdr("FX")
    io = read_json(FX_IO_JSON) or {}
    # INTERNAL names a module; judge the ports it resolves to NOW, not the
    # ones stored at write time.
    try:
        sys.path.insert(0, ORC1_DIR)
        import orc1_config as _cfg
        io = _cfg.fx_io_resolve(io, _phys_playback)
    except Exception:
        pass
    note("mode: %s%s" % (io.get("mode", "?"),
                         (" (module %s)" % io["module"]) if io.get("module") else ""))
    # The INTERNAL source/return only matter while a rack slot is ON. With every
    # slot off nothing is in the signal path, so an unset source is a choice,
    # not a fault (owner doesn't use the FX; 2026-10-14).
    rack_on = any(s.get("enabled") for s in
                  (read_json(FX_RACK_JSON) or {}).get("slots", []))
    if io.get("mode") == "internal" and not io.get("module"):
        if rack_on:
            warn("fx.io.module", "FX INTERNAL mode has no source module -- pick one on /fx")
        else:
            note("no source module picked (fine: no rack slot is on)")
    internal_mod = io.get("mode") == "internal" and io.get("module")
    if rack_on and internal_mod and not (io.get("return_l") and io.get("return_r")):
        # The return follows the module's /patch output; NONE (or no physical
        # outs) resolves to "" and used to pass without a word (2026-10-01).
        if io.get("return_none"):
            warn("fx.io.return",
                 "FX INTERNAL on '%s', whose output is set to NONE -- the FX "
                 "return is wired nowhere, so the rack is unheard. Set an output "
                 "on /patch MODULE OUTPUTS (or pick another module on /fx)"
                 % internal_mod)
        else:
            warn("fx.io.return",
                 "FX INTERNAL on '%s': its output resolves to no physical "
                 "outputs -- the FX return is wired nowhere" % internal_mod)
    # DAW/DJ mode stops the module hosts on purpose (engines off); their ports
    # are then missing by design, not dead (same rule sec_services uses).
    engines_off = os.path.exists(_DAW_OFF_MARK)
    try:
        mod_clients = set(_cfg.MODULE_JACK_NAMES.values())
    except Exception:
        mod_clients = set()
    for k in ("send_l", "send_r", "return_l", "return_r"):
        p = io.get(k)
        if p and p not in ports:
            if engines_off and p.split(":", 1)[0] in mod_clients:
                _record("idle", "fx.io.%s" % k,
                        "FX %s '%s' is absent: its host is stopped by DAW/DJ "
                        "mode (engines off) -- back when REAPER/Mixxx quits"
                        % (k, p))
            else:
                dead("fx.io.%s" % k,
                     "FX %s names '%s', which does not exist on this rig" % (k, p))
        elif p:
            ok("fx.io.%s" % k, "FX %s -> %s exists" % (k, p))

    rack = read_json(FX_RACK_JSON) or {}
    slots = rack.get("slots", [])
    if not slots:
        note("rack: no slots configured")
        return
    on = [s for s in slots if s.get("enabled")]
    if not on:
        note("rack: %d slot(s) configured, NONE enabled -- there are no "
             "effects in your signal path" % len(slots))
        return
    for i, s in enumerate(slots):
        if not s.get("enabled"):
            continue
        name = s.get("plugin", "?")
        have = [p for p in ports if p.startswith("fx-rack-%d:" % i)]
        if have:
            ok("fx.slot.%d" % i, "slot %d '%s' running (%d ports)"
               % (i, name, len(have)))
            note("slot %d: %s running" % (i, name))
        else:
            # The desired-state file still says enabled even if jalv died.
            dead("fx.slot.%d" % i,
                 "slot %d '%s' is enabled but has NO JACK ports -- jalv is "
                 "not running, the effect is not in the path" % (i, name))


def sec_web():
    hdr("WEB PAGES")
    import urllib.request
    base = "http://127.0.0.1:5000"
    if run(["systemctl", "is-active", "orc1-web"])[1].strip() != "active":
        dead("web.down", "orc1-web is not active -- no control pages at all")
        return
    bad = []
    for page in WEB_PAGES:
        try:
            with urllib.request.urlopen(base + page, timeout=4) as r:
                code = r.status
        except Exception as e:
            code = getattr(e, "code", 0) or 0
        if code == 200:
            ok("web%s" % page, "%s -> 200" % page)
        else:
            bad.append(page)
            dead("web%s" % page,
                 "%s -> %s (the nav links to it; it will 404 in your face)"
                 % (page, code or "no response"))
    note("%d of %d pages OK" % (len(WEB_PAGES) - len(bad), len(WEB_PAGES)))

    # The /patch saved-patch reconciler is a BACKGROUND THREAD inside orc1-web.
    # If it dies, every page still renders, every endpoint still answers 200,
    # and systemctl still says active -- while saved wires silently stop being
    # restored. That is the phantom-UP shape this project has hit three times,
    # so the thread publishes a heartbeat and this reads it. A checker that
    # cannot check must say so rather than stay quiet.
    try:
        with urllib.request.urlopen(base + "/api/patch/saved", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        warn("patch.reconcile.unreadable",
             "could not read the /patch reconciler heartbeat (%s) -- cannot "
             "tell whether saved wires are being restored" % e)
        d = None
    if isinstance(d, dict):
        age = d.get("last_pass_age")
        every = d.get("reconcile_secs") or 10
        nsaved = len(d.get("connections") or [])
        if age is None:
            if nsaved:
                dead("patch.reconcile.never",
                     "%d wire(s) are saved but the /patch reconciler has "
                     "NEVER completed a pass -- none of them will be restored"
                     % nsaved)
            else:
                ok("patch.reconcile", "reconciler has not run yet; nothing "
                                      "is saved, so nothing is owed")
        elif age > every * 3:
            dead("patch.reconcile.stalled",
                 "the /patch reconciler last ran %.0fs ago but is supposed to "
                 "run every %ss -- its thread is dead or wedged, so saved "
                 "wires are NOT being restored. Every page still looks fine. "
                 "Restart orc1-web." % (age, every))
        else:
            ok("patch.reconcile",
               "reconciler alive (last pass %.1fs ago, %d wire(s) saved)"
               % (age, nsaved))


_DAW_OFF_MARK = "/run/orc1-daw-engines-off"      # daw/daw_mode.sh
_DAW_OFF_HOSTS = ("orc1-drum", "orc1-sampler", "orc1-poly", "orc1-chip",
                  "orc1-mono", "orc1-bbkit")


def sec_services():
    hdr("SERVICES")
    for u in UNITS:
        act = run(["systemctl", "is-active", u])[1].strip()
        ena = run(["systemctl", "is-enabled", u])[1].strip()
        if act == "active":
            note("%-22s active   (%s at boot)" % (u, ena))
            ok("svc.%s" % u, "%s active" % u)
        elif (u in _DAW_OFF_HOSTS and os.path.exists(_DAW_OFF_MARK)
                and act == "inactive"):
            # DAW/DJ mode stopped the engines on purpose (daw_mode.sh engines-
            # off, the default since 2026-09-29); they come back when the app
            # quits. Not a fault -- but say so, so it never hides a real one.
            _record("idle", "svc.%s" % u,
                    "%s stopped by DAW/DJ mode (engines off) -- restarts when "
                    "REAPER/Mixxx quits" % u)
        elif ena == "enabled":
            # Enabled means it is meant to be running. Not running is a fault.
            dead("svc.%s" % u,
                 "%s is '%s' but ENABLED at boot -- it is supposed to be "
                 "running and is not" % (u, act))
        else:
            note("%-22s %-8s (%s at boot)" % (u, act, ena))


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    global VERBOSE, EXPECT
    ap = argparse.ArgumentParser(
        description="Report what is actually true on the ORC-1 right now. "
                    "Read-only; safe to run while playing.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show every passing check, not just problems")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output (for the timer / web card)")
    ap.add_argument("--out", metavar="PATH",
                    help="write the JSON report to PATH atomically (implies "
                         "--json). The ONLY file this script ever writes, and "
                         "only where you point it.")
    args = ap.parse_args()
    if args.out:
        args.json = True
    VERBOSE = args.verbose

    EXPECT = read_json(EXPECT_JSON) or {}
    if not isinstance(EXPECT, dict):
        EXPECT = {}

    ports, conns, audio, up = jack_graph()

    # Every section is isolated. All output is buffered and printed at the
    # end, so an uncaught exception anywhere would otherwise discard the
    # WHOLE report -- including the sections that already succeeded. A
    # diagnostic that goes dark because one check tripped on a malformed file
    # is worse than one that admits a gap and prints the rest.
    def safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:
            warn("section.%s" % slug(getattr(fn, "__name__", "?")),
                 "section %s CRASHED (%s: %s) -- everything it checks is "
                 "UNVERIFIED in this report"
                 % (getattr(fn, "__name__", "?"), type(e).__name__, e))

    safe(sec_system)
    safe(sec_audio_engine, up)
    if up:
        safe(sec_graph, ports, conns, audio)
    safe(sec_skipback, ports, conns)
    if up:
        safe(sec_module_outputs, ports, conns)
        safe(sec_fx, ports)
    safe(sec_midi)
    safe(sec_logs)
    safe(sec_config)
    safe(sec_state)
    safe(sec_web)
    safe(sec_services)
    self_check()

    c = counts()
    if args.json:
        payload = json.dumps({
            "ts": time.time(),
            "counts": c,
            "findings": [r for r in RESULTS
                         if r["level"] in ("dead", "warn", "idle")],
        }, indent=2)
        if args.out:
            # Atomic: temp file + os.replace, so the web UI can never read a
            # half-written report and render a blank or wrong health card.
            # Same discipline the recorder uses for ring.pos.
            tmp = args.out + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(payload)
                os.replace(tmp, args.out)
            except Exception as e:
                sys.stderr.write("could not write %s: %s\n" % (args.out, e))
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                return 2
        else:
            print(payload)
    else:
        print("ORC-1 health check -- %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("read-only: no ports touched, no files written, no services "
              "restarted")
        emit_text()
    return 1 if c["dead"] else 0


if __name__ == "__main__":
    sys.exit(main())
