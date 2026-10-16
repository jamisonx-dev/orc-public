"""Offline tests for forward-capture scheduler math (2026-08-12).

Runs against the REAL orc1.py source -- the two PURE helpers (`_fwd_snap_length`,
`_fwd_capture_plan`) are extracted with `ast` and exec'd in isolation, the same
technique the chord suites use (orc1.py can't be imported: it runs as __main__
and opens hardware).

    python3 _test_fwd_capture.py

Zero side effects: no MIDI, no JACK, no files, nothing on the live rig. Covers
only the deterministic scheduler math; the CC send / Timer / tempo wiring is
verified live (slice 1 rig check) and by the Mixtrack suite (slice 3).
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, "orc1.py")
WANT_FUNCS = ["_fwd_snap_length", "_fwd_capture_plan"]

EXPECTED = 83   # keep in step with the checks below and the test-orc table


def load():
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    ns, found = {}, set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            exec(ast.get_source_segment(src, node), ns)
            found.add(node.name)
    missing = set(WANT_FUNCS) - found
    if missing:
        sys.exit("FATAL: could not extract from orc1.py: " + ", ".join(sorted(missing)))
    return ns


NS = load()
snap = NS["_fwd_snap_length"]
plan = NS["_fwd_capture_plan"]

RATE = 48000
_p = _f = 0


def ok(cond, label):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ! {label}")


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ── _fwd_snap_length ─────────────────────────────────────────────────────────
# snapping ON: round to nearest whole bar, floor at min_bars.
ok(approx(snap(3.1, 2.0, True), 4.0), "snap 3.1s@2s -> 2 bars (4.0)")
ok(approx(snap(3.9, 2.0, True), 4.0), "snap 3.9s@2s -> 2 bars (4.0)")
ok(approx(snap(1.9, 2.0, True), 2.0), "snap 1.9s@2s -> 1 bar (2.0)")
ok(approx(snap(4.1, 2.0, True), 4.0), "snap 4.1s@2s -> 2 bars (4.0)")
ok(approx(snap(5.2, 2.0, True), 6.0), "snap 5.2s@2s -> 3 bars (6.0)")
# sub-bar hold still yields at least min_bars (never a stub loop).
ok(approx(snap(0.3, 2.0, True), 2.0), "snap 0.3s -> floor 1 bar (2.0)")
ok(approx(snap(0.0, 2.0, True), 2.0), "snap 0.0s -> floor 1 bar (2.0)")
ok(approx(snap(0.3, 2.0, True, 2), 4.0), "snap floor min_bars=2 -> 4.0")
# snapping OFF: raw hold, unchanged, never negative.
ok(approx(snap(3.1, 2.0, False), 3.1), "snap OFF -> raw 3.1")
ok(approx(snap(0.5, 2.0, False), 0.5), "snap OFF -> raw 0.5")
ok(approx(snap(-1.0, 2.0, False), 0.0), "snap OFF clamps negative -> 0")
# no tempo (bar_s <= 0): can't snap, fall back to raw hold.
ok(approx(snap(3.1, 0.0, True), 3.1), "no tempo -> raw hold 3.1")
ok(approx(snap(3.1, -2.0, True), 3.1), "negative bar_s -> raw hold 3.1")

# ── _fwd_capture_plan: frames ────────────────────────────────────────────────
ok(plan(2.0, 0.0, RATE)[2] == 96000, "frames 2.0s@48k = 96000")
ok(plan(1.0, 0.0, RATE)[2] == 48000, "frames 1.0s@48k = 48000")
ok(plan(0.0, 0.0, RATE)[2] == 1, "frames floor at 1 (never 0)")
ok(plan(-5.0, 0.0, RATE)[2] == 1, "frames floor at 1 for negative length")

# ── _fwd_capture_plan: branch behaviour ──────────────────────────────────────
# Model A / snapped-up hold: end is in the FUTURE -> wait remainder, offset 0.
d, off, fr = plan(2.0, 0.0, RATE)
ok(approx(d, 2.0) and off == 0, "fixed 2 bars: delay=2.0, offset=0")
d, off, fr = plan(4.0, 3.5, RATE)
ok(approx(d, 0.5) and off == 0, "hold snapped-up: delay=0.5, offset=0")
# Exactly at the boundary: fire now, offset 0 (not a spurious wait).
d, off, fr = plan(2.0, 2.0, RATE)
ok(approx(d, 0.0) and off == 0, "elapsed==length: fire now, offset 0")
# Snapped-DOWN hold: end already PASSED -> fire now, reach back offset_ms.
d, off, fr = plan(2.0, 2.3, RATE)
ok(approx(d, 0.0) and off == 300, "hold snapped-down: fire now, offset=300ms")
d, off, fr = plan(4.0, 4.05, RATE)
ok(approx(d, 0.0) and off == 50, "hold snapped-down small: offset=50ms")
# delay is never negative.
for L, E in [(1.0, 5.0), (2.0, 2.0), (3.0, 0.0), (0.0, 4.0)]:
    ok(plan(L, E, RATE)[0] >= 0.0, f"delay >= 0 for L={L} E={E}")

# ── THE INVARIANT: the produced window is ALWAYS [press, press+length] ────────
# Whatever branch fires, reconstruct the window in seconds relative to the
# press: end = (elapsed + delay) - offset_ms/1000; start = end - frames/rate.
# start must be 0 (the press) and end must be `length`. This is the whole
# correctness claim of the scheduler in one property.
def window_rel_press(length_s, elapsed_s):
    d, off, fr = plan(length_s, elapsed_s, RATE)
    end = (elapsed_s + d) - off / 1000.0
    start = end - fr / RATE
    return start, end

for length_s in (0.5, 1.0, 2.0, 4.0, 7.3):
    for elapsed_s in (0.0, 0.25, length_s, length_s + 0.4, length_s * 2):
        start, end = window_rel_press(length_s, elapsed_s)
        ok(approx(start, 0.0, 2e-3), f"window START=press  L={length_s} E={elapsed_s}")
        ok(approx(end, length_s, 2e-3), f"window END=+length  L={length_s} E={elapsed_s}")

# ── hold_end tap/hold DECISION (behavioral; the mutation the reviewer found) ──
# Extract sampler_capture_fwd_hold_end and drive it in a controlled namespace
# (fake clock, stub _fwd_schedule/_fwd_bar_s). Covers the tap-vs-hold branch and
# the tap_bars-None guard -- previously untested, so flipping `<` to `>` or
# dropping the guard survived every green suite.
import ast as _ast
import threading as _threading

_src2 = open(SRC_PATH, encoding="utf-8").read()
_HE = None
for _n in _ast.parse(_src2).body:
    if isinstance(_n, _ast.FunctionDef) and _n.name == "sampler_capture_fwd_hold_end":
        _HE = _ast.get_source_segment(_src2, _n)
if _HE is None:
    sys.exit("FATAL: could not extract sampler_capture_fwd_hold_end from orc1.py")


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


_sched = []
_HENS = {
    "_fwd_lock": _threading.RLock(),
    "_fwd_press": {},
    "_SMP_CAP_FRA_CC": {"B": [1, 2, 3, 4]},
    "_FWD_HOLD_MS": 280.0,
    "_FWD_SNAP": True,
    "_FWD_MIN_BARS": 1,
    "_fwd_bar_s": (lambda: 2.0),                 # 2.0 s per bar (fixed for test)
    "_fwd_snap_length": snap,                    # the REAL helper
    "_fwd_schedule": (lambda deck, length_s, elapsed_s:
                      _sched.append((deck, round(length_s, 6), round(elapsed_s, 6)))),
    "time": _FakeClock(),
    "float": float,
}
exec(_HE, _HENS)
_hold_end = _HENS["sampler_capture_fwd_hold_end"]
_CLK = _HENS["time"]
_PRESS = _HENS["_fwd_press"]


def he(press_t, release_t, tap_bars, pending=True):
    _sched.clear()
    _PRESS.clear()
    if pending:
        _PRESS["B"] = press_t
    _CLK.t = release_t
    _hold_end("B", tap_bars)
    return list(_sched)


# TAP: short hold + tap_bars -> fixed N bars (length = N * bar_s = N*2.0)
ok(he(0.0, 0.10, 2) == [("B", 4.0, 0.1)], "hold_end TAP 0.10s,2bars -> 4.0s @ elapsed 0.1")
ok(he(0.0, 0.279, 2) == [("B", 4.0, 0.279)], "hold_end 279ms -> TAP (just under 280)")
# HOLD: >= threshold -> snapped held duration
ok(he(0.0, 0.280, 2) == [("B", 2.0, 0.28)], "hold_end 280ms boundary -> HOLD (snap), not tap")
ok(he(0.0, 1.5, 2) == [("B", 2.0, 1.5)], "hold_end HOLD 1.5s -> snap 1 bar (2.0)")
ok(he(0.0, 5.2, 1) == [("B", 6.0, 5.2)], "hold_end HOLD 5.2s -> snap 3 bars (6.0)")
# tap_bars=None (web/direct caller) + short hold -> HOLD branch, NOT tap
ok(he(0.0, 0.10, None) == [("B", 2.0, 0.1)], "hold_end tap_bars=None short -> HOLD floor, not tap")
# no pending press -> no schedule
ok(he(0.0, 1.0, 2, pending=False) == [], "hold_end with no pending press -> no schedule")

print("=" * 58)
print(f"  {_p} passed, {_f} failed, {_p + _f} checks")
if _p + _f != EXPECTED:
    print(f"  ! EXPECTED {EXPECTED} checks, ran {_p + _f} (suite drifted)")
    _f += 1
print("=" * 58)
sys.exit(1 if _f else 0)
