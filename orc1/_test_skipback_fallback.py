#!/usr/bin/env python3
"""Skipback MANUAL-source fallback to the pisound (regression suite, 2026-10-23).

owner: "the pisound is the onboard audio. it is the one thing you know will be
connected, so it's the fallback". Measured the same day: track 2 (manual source
= the L-12 master, system:capture_13/14) recorded SILENCE while the main jackd
had fallen back to the pisound, because a manual source never fell back.

Drives the REAL Recorder._reconcile resolve/wire loop (everything around it
stubbed) and the REAL _main_jackd_card against a fake /proc.

Pure: no JACK server, no ring files, no config. Safe while the rig runs.
Mutation-prove: drop the fallback branch in _reconcile -> checks 2-4, 6 fail;
make pisound_inputs ignore the main card -> check 5 fails; count the ALSA `-n`
as a server name in _main_jackd_card -> the /proc checks fail.
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "skipback"))
try:
    import jack  # noqa: F401
except Exception:
    sys.modules["jack"] = types.ModuleType("jack")
import skipback_recorder as SR

EXPECTED = 16
PASS = FAIL = 0
LOGS = []


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {label}" + (f"  -- {detail}" if detail else ""))


class _Port:
    def __init__(self, name):
        self.name = name


class _Client:
    def __init__(self):
        self.names = set()

    def get_ports(self):
        return [_Port(n) for n in self.names]


class _Trk:
    def __init__(self, c):
        self.id = c["id"]
        self.cfg = dict(c)
        self.resolved = []
        self.connected = False
        self.fallback = False


def manual(tid, left, right=None, stereo=True):
    return {"id": tid, "name": f"T{tid}", "armed": True, "stereo": stereo,
            "length_secs": 60, "mode": "manual", "left": left, "right": right}


def auto(tid):
    return {"id": tid, "name": f"T{tid}", "armed": True, "stereo": True,
            "length_secs": 60, "mode": "auto", "left": None, "right": None}


def recorder(cfgs):
    r = SR.Recorder.__new__(SR.Recorder)
    r._cfg_tracks = cfgs
    r._blocked = set()
    r._rt_trouble = r._rt_dropped = False
    r.tracks = {c["id"]: _Trk(c) for c in cfgs}
    r.client = _Client()
    for m in ("_refresh_config", "_refresh_src_override", "_apply_ram_guard",
              "_publish_rt", "_write_status"):
        setattr(r, m, lambda: None)
    r._structural_change = lambda old, new: False
    r._effective_cfg = lambda trk: trk.cfg
    r._wire = lambda trk, names: None
    return r


def run(r, names, main_card):
    r.client.names = set(names)
    SR._main_jackd_card = lambda proc="/proc": main_card
    r._reconcile()


SR.log = lambda msg: LOGS.append(msg)
REAL_MAIN_CARD = SR._main_jackd_card
L12 = ["system:capture_%d" % i for i in range(1, 15)]
PISOUND = ["system:capture_1", "system:capture_2"]

# 1. own source present -> own source, no fallback
r = recorder([manual(2, "system:capture_13", "system:capture_14")])
run(r, L12, "L12")
t = r.tracks[2]
check(t.resolved == ["system:capture_13", "system:capture_14"] and not t.fallback,
      "own manual source used while it exists", f"{t.resolved} fb={t.fallback}")

# 2. L-12 gone, main jackd on the pisound -> the pisound inputs, logged
LOGS.clear()
run(r, PISOUND, "pisound")
check(t.resolved == PISOUND and t.fallback, "source gone + main on pisound -> pisound inputs",
      f"{t.resolved} fb={t.fallback}")
check(any("pisound" in m for m in LOGS), "the switch to the pisound is logged", str(LOGS))

# 3. still gone: no repeated log line
LOGS.clear()
run(r, PISOUND, "pisound")
check(LOGS == [], "no log spam while it stays on the pisound", str(LOGS))

# 4. L-12 back -> own source again, logged
run(r, L12, "L12")
check(t.resolved == ["system:capture_13", "system:capture_14"] and not t.fallback,
      "own source back -> used again", f"{t.resolved} fb={t.fallback}")
check(any("own source back" in m for m in LOGS), "the switch back is logged", str(LOGS))

# 5. source gone but the main jackd is on ANOTHER card: its capture_1/2 are not
#    the pisound -> do not take them (keep the own source, i.e. silence)
run(r, PISOUND, "M3")
check(t.resolved == ["system:capture_13", "system:capture_14"] and not t.fallback,
      "main on another card -> no fake 'pisound'", f"{t.resolved} fb={t.fallback}")

# 6. the pisound-in bridge counts, whatever card the main is on
run(r, ["pisound-in:capture_1", "pisound-in:capture_2", "system:capture_1"], "M3")
check(t.resolved == list(SR.PISOUND_BRIDGE) and t.fallback, "pisound-in bridge used as fallback",
      f"{t.resolved}")

# 7. a mono manual track falls back to the left input only
r = recorder([manual(3, "ORC1-Gone:out_L", stereo=False)])
run(r, PISOUND, "pisound")
check(r.tracks[3].resolved == ["system:capture_1"], "mono manual -> pisound input 1 only",
      f"{r.tracks[3].resolved}")

# 8. JACK listing failed (no names): never declare a fallback on no information
r = recorder([manual(2, "system:capture_13", "system:capture_14")])
run(r, [], "pisound")
check(not r.tracks[2].fallback, "empty port list -> no fallback")

# 9. AUTO tracks unchanged: L-12 master when present, else capture_1/2
r = recorder([auto(0)])
run(r, L12, "L12")
a1 = list(r.tracks[0].resolved)
run(r, PISOUND, "pisound")
check(a1 == ["system:capture_13", "system:capture_14"] and r.tracks[0].resolved == PISOUND
      and not r.tracks[0].fallback, "auto track keeps its own chain", f"{a1} {r.tracks[0].resolved}")

# 10. _main_jackd_card against a fake /proc
SR._main_jackd_card = REAL_MAIN_CARD


def fake_proc(cmdlines):
    d = tempfile.mkdtemp(prefix="sbproc_")
    os.makedirs(os.path.join(d, "self"))
    for pid, args in cmdlines.items():
        os.makedirs(os.path.join(d, str(pid)))
        with open(os.path.join(d, str(pid), "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in args) + b"\0")
    return d


MAIN_PI = ["/usr/bin/jackd", "-R", "--port-max", "256", "-dalsa", "-dhw:pisound",
           "-r", "48000", "-p", "1024", "-n", "2"]
NAMED_PI = ["/usr/bin/jackd", "-n", "pisound", "-R", "-dalsa", "-dhw:pisound", "-n", "2"]
MAIN_L12 = ["/usr/bin/jackd", "-R", "-dalsa", "-dhw:L12", "-r", "48000", "-n", "2"]
check(SR._main_jackd_card(fake_proc({10: MAIN_PI})) == "pisound",
      "main jackd on pisound (ALSA -n is not a server name)")
check(SR._main_jackd_card(fake_proc({10: NAMED_PI, 11: MAIN_L12})) == "L12",
      "named pisound server skipped, main on L12 found")
check(SR._main_jackd_card(fake_proc({10: NAMED_PI, 12: ["/usr/bin/python3", "x.py"]})) == "",
      "only a named server -> no main card")
check(SR._main_jackd_card(fake_proc({})) == "", "no jackd at all -> ''")

# 11. the status file reports the fallback
src = open(os.path.join(HERE, "skipback", "skipback_recorder.py"), encoding="utf-8").read()
check('"fallback": bool(trk and trk.fallback)' in src, "status JSON carries 'fallback'")

print(f"{PASS} passed, {FAIL} failed")
if PASS + FAIL != EXPECTED:
    print(f"  FAIL: expected {EXPECTED} checks, ran {PASS + FAIL}")
    sys.exit(1)
sys.exit(1 if FAIL else 0)
