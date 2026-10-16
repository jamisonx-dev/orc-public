#!/usr/bin/env python3
"""Regression suite for the SAVED PATCH half of /patch  (orc1_web.py).

    python3 _test_patch_persist.py [-v]

SAFETY -- read this before editing.

This suite imports orc1_web and drives the REAL functions, which is the only
way to prove the deployed code works (see _test_fire.py for the same argument,
and for what happened the one time a test escaped its sandbox and overwrote the
live routing table).

Three things make that safe here, and ALL must stay true:

  1. orc1_web.PATCH_FILE is repointed at a temp file for every case. Nothing
     touches ~/.orc1_patch.json. __main__ checks its mtime before and after.
  2. jack is never run. _patch_ports / _patch_conns / _patch_run are replaced
     with fakes, so no jack_connect or jack_disconnect can reach the graph.
  3. Rig.__exit__ restores all four globals and clears the module state even
     when the body raised, so a failing test cannot leak a stub.

Importing orc1_web is side-effect free: the reconcile thread is started in its
__main__ block, deliberately, so importing does not silently acquire a thread
that rewires live audio. test_reconcile_thread_is_wired asserts that start line
still exists, because deleting it leaves every other check in this file green.

WHY THE STANDING NETS LIVE IN __main__ AND NOT IN TESTS:
The first version put them in the sweep, where sorted() ran them 15th of 20 --
and eight tests call `del ISSUED[:]`, so the disconnect net inspected an empty
list on every run. It could not fail. It was a comment, which is the exact
failure this project keeps naming. They now run LAST, over ALL_ISSUED, which is
never cleared.
"""

import ast, json, os, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_web as W                                        # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
REAL_PATCH_FILE = W.PATCH_FILE          # captured before anything repoints it
HERE = os.path.dirname(os.path.abspath(__file__))

# EXPECTED is itself a check. A suite that silently SHRINKS still prints
# "0 failed" -- that is how three nets went missing from _test_fire.py for a
# month while every run looked green (157 -> 154, and nobody compared). Rename
# or lose a test and the total assertion in __main__ goes red.
EXPECTED = 126

# ISSUED is per-test scratch and IS cleared. ALL_ISSUED is the standing record
# and is NEVER cleared. See the docstring.
ISSUED = []
ALL_ISSUED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + detail) if detail else ""))


# --------------------------------------------------------------- harness ---

class Rig:
    """A fake JACK graph plus a temp saved-patch file."""

    def __init__(self, ports=(), conns=(), fail=None):
        self.ports = list(ports)
        self.conns = [tuple(c) for c in conns]
        self.fail = fail or {}          # (a,b) -> error string
        self.tmp = tempfile.mkdtemp(prefix="patchtest_")
        self.path = os.path.join(self.tmp, "saved.json")

    def __enter__(self):
        self._saved = (W.PATCH_FILE, W._patch_ports, W._patch_conns,
                       W._patch_run)
        W.PATCH_FILE = self.path
        self.port_calls = 0
        W._patch_ports = self._ports
        W._patch_conns = lambda ports=None: [list(c) for c in self.conns]
        W._patch_run = self._run
        W._patch_seen_up.clear()
        W._patch_last_error.clear()
        return self

    def __exit__(self, *a):
        (W.PATCH_FILE, W._patch_ports, W._patch_conns,
         W._patch_run) = self._saved
        W._patch_seen_up.clear()
        W._patch_last_error.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def _ports(self):
        self.port_calls += 1
        return [{"name": n, "dir": "out", "audio": True} for n in self.ports]

    def _run(self, tool, a, b):
        ISSUED.append((tool, a, b))
        ALL_ISSUED.append((tool, a, b))
        err = self.fail.get((a, b))
        if err:
            return err
        if tool == "jack_connect":
            if (a, b) not in self.conns:
                self.conns.append((a, b))
        else:
            self.conns = [c for c in self.conns if c != (a, b)]
        return None

    # ---- the saved file, as this suite wants to see and seed it ----
    def write(self, pairs):
        """Seed v1 pairs -- also proves the old shape still loads."""
        with open(self.path, "w") as f:
            json.dump({"version": 1, "connections": [list(p) for p in pairs]},
                      f)

    def write_entries(self, entries):
        with open(self.path, "w") as f:
            json.dump({"version": 2, "connections": entries}, f)

    def raw(self):
        with open(self.path) as f:
            return f.read()

    def entries(self):
        # No file at all is a legitimate state -- "nothing was ever saved"
        # writes nothing, and that must read as empty rather than blowing up
        # the case that is checking exactly that.
        #
        # Normalises v1 pairs too: a seeded v1 file that nothing has needed to
        # rewrite is STILL v1 on disk, and that is correct behaviour (an idle
        # pass must not rewrite the file), so the harness has to read both.
        if not os.path.exists(self.path):
            return []
        out = []
        for c in json.load(open(self.path))["connections"]:
            if isinstance(c, dict):
                out.append(c)
            else:
                out.append({"src": c[0], "dst": c[1], "cut": False,
                            "missing_since": None})
        return out

    def rows(self):
        return [(e["src"], e["dst"]) for e in self.entries()]

    def entry(self, src, dst):
        for e in self.entries():
            if e["src"] == src and e["dst"] == dst:
                return e
        return None


A, B = "ORC1-Synth:out_L", "system:playback_1"
C, D = "ORC1-Synth:poly_L", "system:playback_3"


# ============================================================ THE GUARD ====
# An empty graph means "no information", not "everything is gone". jackd being
# down looks exactly like every saved port having vanished, and pruning on that
# reading would delete the patch precisely when the rig is broken.

def test_empty_graph_never_prunes():
    del ISSUED[:]
    with Rig(ports=[], conns=[]) as r:
        r.write([[A, B], [C, D]])
        old, W.PATCH_STALE_SECS = W.PATCH_STALE_SECS, 0   # maximally eager
        try:
            for _ in range(5):
                rep = W._patch_reconcile()
        finally:
            W.PATCH_STALE_SECS = old
        check("empty graph: nothing dropped", r.rows() == [(A, B), (C, D)],
              "file became %r" % (r.rows(),))
        check("empty graph: reports not-ok", rep["ok"] is False)
        check("empty graph: says why",
              "jackd" in (rep.get("error") or "").lower())
        check("empty graph: issues no jack calls at all", not ISSUED,
              "issued %r" % (ISSUED,))


def test_empty_graph_never_connects():
    del ISSUED[:]
    with Rig(ports=[], conns=[]) as r:
        r.write([[A, B]])
        W._patch_reconcile()
        check("empty graph: no jack_connect", not ISSUED)


# ============================= NEVER FIGHT ANOTHER SUBSYSTEM ==============
# A wire that goes away while both its ports stayed present was cut on purpose
# -- by a Fire route pad, by /skipback, by the command line. Re-asserting it
# silently undoes that control. Ports vanishing and returning is a restart,
# which nobody chose, and IS restored.

def test_cut_while_ports_present_is_not_refought():
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[(A, B)]) as r:
        r.write([[A, B]])
        W._patch_reconcile()                       # observes it up
        check("wire seen up is not re-issued", not ISSUED)

        r.conns = []                               # a Fire route pad goes off
        rep = W._patch_reconcile()
        check("cut wire is NOT reconnected", not ISSUED,
              "issued %r" % (ISSUED,))
        check("cut wire is reported as cut", rep["cut"] == [[A, B]])
        check("cut wire is marked in the file",
              r.entry(A, B)["cut"] is True)
        check("cut wire is NOT deleted", r.rows() == [(A, B)])

        for _ in range(4):                         # and stays hands-off
            W._patch_reconcile()
        check("cut wire is not re-fought on later passes", not ISSUED)


def test_cut_is_remembered_across_a_web_restart():
    """The cut mark is in the FILE, not just in memory.

    Otherwise `systemctl restart orc1-web` starts with an empty seen-up set,
    decides it has never seen the wire, and re-asserts every wire the Fire
    deliberately cut -- the fight, merely delayed.
    """
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": None}])
        W._patch_seen_up.clear()                   # a fresh process
        rep = W._patch_reconcile()
        check("a cut wire is not re-asserted by a fresh process", not ISSUED,
              "issued %r" % (ISSUED,))
        check("it is still reported as cut", rep["cut"] == [[A, B]])


def test_restart_restores_because_ports_vanished():
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[(A, B)]) as r:
        r.write([[A, B]])
        W._patch_reconcile()                       # seen up
        r.ports = [B]                              # the synth restarts
        r.conns = []
        W._patch_reconcile()
        check("ports gone: nothing issued", not ISSUED)
        check("ports gone: not marked cut", r.entry(A, B)["cut"] is False)
        r.ports = [A, B]                           # and comes back
        rep = W._patch_reconcile()
        check("wire is restored after a restart", rep["done"] == [[A, B]])
        check("graph actually has it again", (A, B) in r.conns)


def test_fresh_boot_restores_everything():
    """A power cycle: nothing has ever been seen up, so nothing is a cut."""
    del ISSUED[:]
    with Rig(ports=[A, B, C, D], conns=[]) as r:
        r.write([[A, B], [C, D]])
        W._patch_seen_up.clear()
        rep = W._patch_reconcile()
        check("cold boot restores every saved wire",
              sorted(rep["done"]) == sorted([[A, B], [C, D]]),
              "done=%r" % (rep["done"],))
        check("cold boot leaves nothing cut", rep["cut"] == [])


def test_load_forces_a_cut_wire_back():
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": None}])
        rep = W._patch_reconcile(force=True)
        check("LOAD re-applies a cut wire", rep["done"] == [[A, B]])
        check("LOAD clears the cut mark", r.entry(A, B)["cut"] is False)


def test_remaking_a_cut_wire_rearms_it():
    with Rig(ports=[A, B], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": 1.0}])
        W._patch_saved_add([[A, B]])
        check("re-making a cut wire un-cuts it",
              r.entry(A, B)["cut"] is False)
        check("re-making clears its stale timer",
              r.entry(A, B)["missing_since"] is None)
        check("re-making does not duplicate the entry", len(r.rows()) == 1)


def test_a_wire_that_comes_back_up_is_uncut():
    with Rig(ports=[A, B], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": None}])
        W._patch_reconcile()
        r.conns = [(A, B)]                    # the Fire pad goes back on
        W._patch_reconcile()
        check("a cut wire that returns is no longer marked cut",
              r.entry(A, B)["cut"] is False)


# ================================================== FILE HANDLING =========

def test_corrupt_file_moved_aside():
    with Rig() as r:
        with open(r.path, "w") as f:
            f.write("{this is not json")
        rows = W._patch_saved_load()
        check("corrupt file reads as empty", rows == [])
        check("corrupt file is moved aside, not deleted",
              os.path.exists(r.path + ".bad"))
        check("corrupt file no longer in place", not os.path.exists(r.path))


def test_malformed_entries_dropped():
    with Rig() as r:
        r.write_entries([{"src": A, "dst": B}, {"src": "noColon", "dst": B},
                         {"src": A}, "junk", [A, 7], [A], {"src": A, "dst": 7},
                         [C, D]])
        rows = W._patch_saved_load()
        check("malformed entries dropped",
              [(e["src"], e["dst"]) for e in rows] == [(A, B), (C, D)],
              "got %r" % (rows,))


def test_v1_pairs_still_load():
    """A file written before cut/missing_since existed must still load."""
    with Rig() as r:
        r.write([[A, B]])
        rows = W._patch_saved_load()
        check("v1 pair file loads", [(e["src"], e["dst"]) for e in rows]
              == [(A, B)])
        check("v1 entry defaults to not-cut", rows[0]["cut"] is False)
        check("v1 entry defaults to no stale timer",
              rows[0]["missing_since"] is None)


def test_write_is_atomic_and_dedupes():
    with Rig() as r:
        W._patch_saved_write([[A, B], [A, B], [C, D]])
        check("write dedupes", r.rows() == [(A, B), (C, D)])
        check("write leaves valid json", json.loads(r.raw())["version"] == 2)
        check("no temp file left behind",
              not os.path.exists(r.path + ".tmp"))


def test_write_never_touches_the_destination_directly():
    """Atomicity, tested by its observable consequence rather than asserted.

    A plain open(path,"w") TRUNCATES and then streams, so an interruption --
    or a power cut on a headless instrument writing to an SD card -- leaves an
    unparseable file, which _patch_saved_load() then reads as "you have no
    saved patch" and the next write cements. This is the exact failure that bit
    _save_routing_file() on 2026-08-03, one file over.

    Suppressing the rename makes the difference visible: an atomic writer has
    put nothing in the destination yet, a truncating one has already destroyed
    what was there.
    """
    with Rig() as r:
        r.write([[C, D]])
        calls, real = [], os.replace
        os.replace = lambda a, b: calls.append((a, b))
        try:
            W._patch_saved_write([[A, B]])
        finally:
            os.replace = real
        check("write finishes with a rename into place",
              len(calls) == 1 and calls[0][1] == r.path,
              "os.replace calls: %r" % (calls,))
        check("write goes to a temp file, not the destination",
              calls and calls[0][0] != r.path)
        check("destination still holds the old patch until the rename",
              r.rows() == [(C, D)], "destination is now %r" % (r.rows(),))
        for c in calls:
            if os.path.exists(c[0]):
                os.unlink(c[0])


def test_write_fsyncs_the_file():
    """fsync is the difference between "written" and "on the card"."""
    with Rig() as r:
        # Counting "any fsync happened" is NOT enough: the directory is fsynced
        # too, so deleting the FILE fsync left that check green. Identify the
        # directory fd and require a sync of something that is not it.
        # Compared by ORDER, not by fd number: the file is closed before the
        # directory is opened, so the kernel hands out the same fd for both
        # and any identity comparison collapses. The file sync is simply the
        # one that happens before the directory is ever opened.
        events = []
        real_fsync, real_open = os.fsync, os.open

        def spy_fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def spy_open(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            events.append("open")
            return fd

        os.fsync, os.open = spy_fsync, spy_open
        try:
            W._patch_saved_write([[A, B]])
        finally:
            os.fsync, os.open = real_fsync, real_open
        first_open = events.index("open") if "open" in events else len(events)
        check("the saved FILE is fsynced, not just its directory",
              "fsync" in events[:first_open], "events %r" % (events,))


def test_missing_file_is_empty_not_error():
    with Rig() as r:
        check("absent file reads as empty", W._patch_saved_load() == [])
        check("absent file is not created by a read",
              not os.path.exists(r.path))


# ================================================== RECONCILE BEHAVIOUR ===

def test_connects_only_what_is_missing():
    del ISSUED[:]
    with Rig(ports=[A, B, C, D], conns=[(A, B)]) as r:
        r.write([[A, B], [C, D]])
        rep = W._patch_reconcile()
        check("already-up wire not re-issued",
              ISSUED == [("jack_connect", C, D)], "issued %r" % (ISSUED,))
        check("missing wire reported as done", rep["done"] == [[C, D]])
        check("reconcile reports ok", rep["ok"] is True)
        check("both wires still saved", r.rows() == [(A, B), (C, D)])


def test_idle_pass_does_not_rewrite_the_file():
    """Ten seconds is 8,640 writes a day. The SD card is not a scratchpad."""
    with Rig(ports=[A, B], conns=[(A, B)]) as r:
        r.write([[A, B]])
        W._patch_reconcile()                    # may normalise v1 -> v2
        before = (os.path.getmtime(r.path), r.raw())
        time.sleep(0.02)
        for _ in range(3):
            W._patch_reconcile()
        after = (os.path.getmtime(r.path), r.raw())
        check("an idle reconcile pass writes nothing", before == after,
              "file changed on an idle pass")


def test_no_jack_at_all_when_nothing_is_saved():
    """The shipped default is an empty file. It must cost literally nothing."""
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[]) as r:
        rep = W._patch_reconcile()
        check("nothing saved: jack graph never enumerated", r.port_calls == 0,
              "_patch_ports called %d times" % r.port_calls)
        check("nothing saved: no jack calls", not ISSUED)
        check("nothing saved: reports ok", rep["ok"] is True)
        check("nothing saved: empty report", rep["saved"] == [])


def test_failed_connect_is_reported_and_kept():
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[], fail={(A, B): "cannot connect"}) as r:
        r.write([[A, B]])
        rep = W._patch_reconcile()
        check("failed wire not reported as done", rep["done"] == [])
        check("failed wire reported as failed",
              len(rep["failed"]) == 1 and
              rep["failed"][0]["error"] == "cannot connect")
        check("failed reconcile is not ok", rep["ok"] is False)
        check("failed wire stays saved -- ports exist, it may recover",
              r.rows() == [(A, B)])
        check("the failure is remembered for the UI",
              W._patch_last_error.get((A, B)) == "cannot connect")


def test_missing_port_kept_until_stale_then_dropped():
    del ISSUED[:]
    with Rig(ports=[A], conns=[]) as r:            # B is gone
        r.write([[A, B]])
        rep = W._patch_reconcile()
        check("missing port: not dropped immediately", r.rows() == [(A, B)])
        check("missing port: reported pending", rep["pending"] == [[A, B]])
        check("missing port: stale timer started",
              isinstance(r.entry(A, B)["missing_since"], (int, float)))
        check("missing port: no jack call", not ISSUED)

        # Age it past the window by backdating the stamp IN THE FILE, which
        # is also what proves the timer survives an orc1-web restart.
        e = r.entry(A, B)
        r.write_entries([{"src": A, "dst": B, "cut": False,
                          "missing_since": e["missing_since"]
                          - (W.PATCH_STALE_SECS + 1)}])
        rep = W._patch_reconcile()
        check("missing port: dropped once stale", r.rows() == [])
        check("missing port: drop is reported", rep["dropped"] == [[A, B]])
        check("missing port: STILL no jack call -- the surviving side is "
              "left alone", not ISSUED, "issued %r" % (ISSUED,))


def test_stale_timer_is_wall_clock_and_survives_a_restart():
    """The timer lives in the file, not in memory.

    In-memory it measured orc1-web UPTIME, so a 24h window could never expire
    on a box that restarts, and a short one expired during a soundcheck.
    """
    with Rig(ports=[A], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": False,
                          "missing_since": time.time() - 10}])
        W._patch_seen_up.clear()                   # a fresh process
        W._patch_reconcile()
        kept = r.entry(A, B)
        check("a fresh process keeps the existing stale timer",
              kept and abs(kept["missing_since"] - (time.time() - 10)) < 2,
              "timer is %r" % (kept,))


def test_clock_going_backwards_does_not_break_the_timer():
    """This Pi has no RTC; NTP corrects the clock after boot."""
    with Rig(ports=[A], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": False,
                          "missing_since": time.time() + 99999}])
        W._patch_reconcile()
        e = r.entry(A, B)
        check("a stamp from the future is reset, not trusted",
              e and e["missing_since"] <= time.time() + 1,
              "timer is %r" % (e,))
        check("and the wire is not dropped for it", r.rows() == [(A, B)])


def test_prune_can_be_disabled():
    with Rig(ports=[A], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": False,
                          "missing_since": time.time()
                          - (W.PATCH_STALE_SECS + 1)}])
        rep = W._patch_reconcile(prune=False)
        check("prune=False keeps a stale wire", r.rows() == [(A, B)])
        check("prune=False reports nothing dropped", rep["dropped"] == [])


def test_connect_disabled_reports_pending():
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[]) as r:
        r.write([[A, B]])
        rep = W._patch_reconcile(connect=False)
        check("connect=False issues nothing", not ISSUED)
        check("connect=False reports pending", rep["pending"] == [[A, B]])


# ============================================ THE CONTROL IS WIRED UP =====

def test_reconcile_thread_is_wired():
    """The loop is the feature. Delete its start line and every behavioural
    check in this file still passes -- so assert the wiring at the source."""
    src = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    started = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name == "Thread":
                for kw in node.keywords:
                    if kw.arg == "target" and \
                            getattr(kw.value, "id", None) == \
                            "_patch_reconcile_thread":
                        started.append(node)
    check("the reconcile thread is actually started", len(started) == 1,
          "found %d Thread(target=_patch_reconcile_thread) calls"
          % len(started))
    check("it is started as a daemon",
          any(kw.arg == "daemon" for n in started for kw in n.keywords))
    # The values are a contract, not decoration: the interval must be short
    # enough to restore promptly, the stale window long enough that a service
    # restart or a card swap cannot eat the patch.
    # A CORRECTNESS BOUND: the poll must be shorter than the port-absence gap
    # of a service restart, or the gap is missed and the restart is misread as
    # a deliberate cut. Measured on this rig: orc1-synth's ports are away for
    # ~4.2s. At the old 10s this failed live.
    check("reconcile interval is shorter than a restart's port gap",
          1 <= W.PATCH_RECONCILE_SECS <= 4,
          "PATCH_RECONCILE_SECS=%r -- a restart gap of ~4.2s would be missed"
          % W.PATCH_RECONCILE_SECS)
    check("stale window is far longer than any restart",
          W.PATCH_STALE_SECS >= 3600,
          "PATCH_STALE_SECS=%r" % W.PATCH_STALE_SECS)


def test_thread_loop_survives_a_raising_pass():
    """A pass that raises must not kill the loop or spin it hot."""
    calls, slept = [], []
    real_rec, real_sleep = W._patch_reconcile, time.sleep

    # The loop is bounded on the RECONCILE call, not on sleep. Bounding it on
    # sleep meant that deleting the sleep -- the very mutation this test
    # exists to catch -- made the suite HANG instead of fail. A test that
    # hangs is worse than one that fails: under the test-orc harness it just
    # stops, with no count and no verdict.
    def boom(*a, **k):
        calls.append(1)
        if len(calls) >= 3:
            raise KeyboardInterrupt          # break out of the infinite loop
        raise RuntimeError("jack_lsp exploded")

    def fake_sleep(n):
        slept.append(n)

    W._patch_reconcile, time.sleep = boom, fake_sleep
    try:
        W._patch_reconcile_thread()
    except KeyboardInterrupt:
        pass
    finally:
        W._patch_reconcile, time.sleep = real_rec, real_sleep
    check("a raising pass does not stop the loop", len(calls) == 3,
          "ran %d passes" % len(calls))
    check("a raising pass still sleeps -- no hot loop",
          slept == [W.PATCH_RECONCILE_SECS] * 2, "slept %r" % (slept,))


def test_heartbeat_is_published():
    """If the thread dies, everything else still renders. Publish its pulse."""
    real_rec, real_sleep = W._patch_reconcile, time.sleep
    W._patch_last_pass[0] = None
    seen = []

    def one_pass(*a, **k):
        # Bounded on the reconcile call for the same reason as above.
        seen.append(1)
        if len(seen) >= 2:
            raise KeyboardInterrupt
        return {"ok": True}

    W._patch_reconcile = one_pass
    time.sleep = lambda n: None
    try:
        W._patch_reconcile_thread()
    except KeyboardInterrupt:
        pass
    finally:
        W._patch_reconcile, time.sleep = real_rec, real_sleep
    check("a completed pass records a heartbeat",
          W._patch_last_pass[0] is not None)

    W.app.config["TESTING"] = True
    with Rig(ports=[], conns=[]):
        d = json.loads(W.app.test_client().get("/api/patch/saved").data)
    check("the heartbeat is exposed to the page",
          isinstance(d.get("last_pass_age"), (int, float)))
    check("so is the interval it should beat at",
          d.get("reconcile_secs") == W.PATCH_RECONCILE_SECS)


def test_patch_run_treats_already_connected_as_success():
    """Reconcile idempotency rests on this clause."""
    import subprocess

    class R:
        returncode = 1
        stdout = b""
        stderr = b"ERROR: ports are already connected"

    real = subprocess.run
    subprocess.run = lambda *a, **k: R()
    try:
        err = W._patch_run("jack_connect", A, B)
    finally:
        subprocess.run = real
    check("'already connected' counts as success", err is None,
          "got %r" % (err,))

    class R2:
        returncode = 1
        stdout = b""
        stderr = b"ERROR: no such port"

    subprocess.run = lambda *a, **k: R2()
    try:
        err = W._patch_run("jack_connect", A, B)
    finally:
        subprocess.run = real
    check("a real error is still an error", err == "ERROR: no such port")


# ================================================== INTENT BOOKKEEPING ====

def test_add_and_remove():
    with Rig() as r:
        W._patch_saved_add([[A, B]])
        W._patch_saved_add([[C, D]])
        check("add accumulates", r.rows() == [(A, B), (C, D)])
        W._patch_saved_add([[A, B]])
        check("add is idempotent", r.rows() == [(A, B), (C, D)])
        W._patch_saved_remove([[A, B]])
        check("remove takes one out", r.rows() == [(C, D)])
        W._patch_saved_remove([[A, B]])
        check("removing an absent wire is harmless", r.rows() == [(C, D)])


def test_remove_clears_the_error_memory():
    with Rig() as r:
        W._patch_saved_add([[A, B]])
        W._patch_last_error[(A, B)] = "boom"
        W._patch_saved_remove([[A, B]])
        check("removing a wire forgets its error",
              (A, B) not in W._patch_last_error)


# ================================================== ENDPOINT SEMANTICS ====

def _post(client, body):
    r = client.post("/api/patch/saved", json=body)
    return r.status_code, json.loads(r.data)


def test_endpoints():
    W.app.config["TESTING"] = True
    client = W.app.test_client()

    del ISSUED[:]
    with Rig(ports=[A, B, C, D], conns=[(A, B), (C, D)]) as r:
        code, d = _post(client, {"op": "save"})
        check("save snapshots the live graph",
              d["ok"] and r.rows() == [(A, B), (C, D)])
        check("save reports the count", d["count"] == 2)
        check("save issues no jack calls", not ISSUED)

        code, d = _post(client, {"op": "forget", "src": A, "dst": B})
        check("forget removes from the file", r.rows() == [(C, D)])
        check("forget does NOT unplug it", (A, B) in r.conns)
        check("forget issues no jack calls", not ISSUED)

        code, d = _post(client, {"op": "reset"})
        check("reset empties the file", r.rows() == [])
        check("reset leaves the graph alone",
              r.conns == [(A, B), (C, D)] and not ISSUED)

        code, d = _post(client, {"op": "nonsense"})
        check("unknown op is a 400", code == 400 and not d["ok"])

        code, d = _post(client, {"op": "forget", "src": A})
        check("forget without dst is a 400", code == 400)

    # SAVE clears the stale timers, or a re-saved wire inherits an old one.
    with Rig(ports=[A, B], conns=[(A, B)]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": 1.0}])
        _post(client, {"op": "save"})
        e = r.entry(A, B)
        check("save clears a stale timer", e["missing_since"] is None)
        check("save clears a cut mark", e["cut"] is False)

    # SAVE must refuse when there is no graph, rather than saving an empty
    # patch over a good one and calling it success.
    del ISSUED[:]
    with Rig(ports=[], conns=[]) as r:
        r.write([[A, B]])
        code, d = _post(client, {"op": "save"})
        check("save on an unreachable graph is refused", code == 503)
        check("save on an unreachable graph keeps the old file",
              r.rows() == [(A, B)], "file is now %r" % (r.rows(),))

    # LOAD reports failure honestly.
    with Rig(ports=[A, B], conns=[], fail={(A, B): "no such port"}) as r:
        r.write([[A, B]])
        code, d = _post(client, {"op": "load"})
        check("load with a failing wire is not 200", code == 409)
        check("load names the failure",
              d["failed"] and d["failed"][0]["error"] == "no such port")


def test_connect_endpoint_records_intent():
    W.app.config["TESTING"] = True
    client = W.app.test_client()
    del ISSUED[:]
    with Rig(ports=[A, B], conns=[]) as r:
        # /api/patch POST validates direction, so give it a directional graph.
        W._patch_ports = lambda: [{"name": A, "dir": "out", "audio": True},
                                  {"name": B, "dir": "in", "audio": True}]
        client.post("/api/patch", json={"op": "connect",
                                        "src": [A], "dst": [B]})
        check("a wire made here is saved", r.rows() == [(A, B)])
        client.post("/api/patch", json={"op": "disconnect",
                                        "src": [A], "dst": [B],
                                        "force": True})
        check("a wire removed here is forgotten", r.rows() == [])


def test_module_outputs_are_protected():
    """Each module's CONFIGURED output pair is guarded against a patchbay
    misclick; a fan-out wire the player added is not; skipback still is.
    (Until 2026-09-23 the guard named only the retired ORC1-Synth, so after
    the audio host split no module output was protected at all.)"""
    import orc1_config as cfg
    P = ["system:playback_%d" % i for i in (1, 2, 3, 4)]
    DL, DR = "ORC1-Drum:out_L", "ORC1-Drum:out_R"
    PL, PR = "ORC1-Poly:out_L", "ORC1-Poly:out_R"
    tmp = tempfile.mkdtemp(prefix="modout_")
    old_file = cfg.MODULE_OUT_FILE
    cfg.MODULE_OUT_FILE = os.path.join(tmp, "mo.json")   # NEVER the live file
    try:
        with open(cfg.MODULE_OUT_FILE, "w") as f:
            json.dump({"poly": "out-3-4"}, f)             # drum = default pair
        conns = [(DL, P[0]), (DR, P[1]), (PL, P[2]), (PR, P[3]),
                 (PL, P[0]),                              # player's fan-out
                 ("system:capture_3", "skipback-rec:t1_M")]
        prot = W._patch_protected(conns=conns, phys=P)
        check("module guard: default-pair module (drum) protected",
              (DL, P[0]) in prot and (DR, P[1]) in prot, repr(sorted(prot)))
        check("module guard: module on a chosen pair (poly 3-4) protected",
              (PL, P[2]) in prot and (PR, P[3]) in prot, repr(sorted(prot)))
        check("module guard: a player-added fan-out wire is NOT protected",
              (PL, P[0]) not in prot)
        check("module guard: the reason names MODULE OUTPUTS",
              "MODULE OUTPUTS" in prot.get((DL, P[0]), ""))
        check("module guard: skipback wiring still protected",
              ("system:capture_3", "skipback-rec:t1_M") in prot)
        # A graph with no module wires never pays for a phys-port lookup.
        calls = []
        old_phys = W._mo_phys_ports
        W._mo_phys_ports = lambda: calls.append(1) or P
        try:
            W._patch_protected(conns=[("system:capture_3",
                                       "skipback-rec:t1_M")])
        finally:
            W._mo_phys_ports = old_phys
        check("module guard: no jack lookup when no module wire is present",
              not calls)
        # End to end: an unforced disconnect of a module's output is refused
        # (409) and NOTHING is issued to jackd.
        W.app.config["TESTING"] = True
        client = W.app.test_client()
        del ISSUED[:]
        with Rig(ports=[DL, P[0]], conns=[(DL, P[0])]):
            W._patch_ports = lambda: [{"name": DL, "dir": "out", "audio": True},
                                      {"name": P[0], "dir": "in", "audio": True}]
            old_phys = W._mo_phys_ports
            W._mo_phys_ports = lambda: P
            try:
                resp = client.post("/api/patch", json={"op": "disconnect",
                                                       "src": [DL], "dst": [P[0]]})
            finally:
                W._mo_phys_ports = old_phys
            check("module guard: unforced disconnect is refused with 409",
                  resp.status_code == 409 and resp.get_json().get("protected"))
            check("module guard: refused disconnect issued no jack call",
                  not ISSUED, repr(ISSUED))
    finally:
        cfg.MODULE_OUT_FILE = old_file
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_connect_is_not_saved():
    W.app.config["TESTING"] = True
    client = W.app.test_client()
    with Rig(ports=[A, B], conns=[], fail={(A, B): "nope"}) as r:
        W._patch_ports = lambda: [{"name": A, "dir": "out", "audio": True},
                                  {"name": B, "dir": "in", "audio": True}]
        d = json.loads(client.post("/api/patch",
                                   json={"op": "connect", "src": [A],
                                         "dst": [B]}).data)
        check("failed connect reports not-ok", not d["ok"])
        check("a wire that never took is NOT saved", r.rows() == [],
              "saved %r" % (r.rows(),))


def test_patch_get_reports_the_saved_list():
    """The page marks which wires survive a power cycle from this key."""
    W.app.config["TESTING"] = True
    client = W.app.test_client()
    with Rig(ports=[A, B], conns=[(A, B)]) as r:
        r.write([[A, B]])
        real_card, W._patch_card = W._patch_card, lambda: "TEST"
        try:
            d = json.loads(client.get("/api/patch").data)
        finally:
            W._patch_card = real_card
        check("GET /api/patch reports the saved wires",
              d.get("saved") == [[A, B]], "saved=%r" % (d.get("saved"),))


def test_saved_state_labels():
    W.app.config["TESTING"] = True
    client = W.app.test_client()
    with Rig(ports=[A, B, C], conns=[(A, B)]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": False,
                          "missing_since": None},
                         {"src": C, "dst": D, "cut": False,
                          "missing_since": None},
                         {"src": C, "dst": B, "cut": False,
                          "missing_since": None}])
        d = json.loads(client.get("/api/patch/saved").data)
        st = {(c["src"], c["dst"]): c["state"] for c in d["connections"]}
        check("connected wire reads live", st[(A, B)] == "live")
        check("both ports present but down reads waiting",
              st[(C, B)] == "waiting")
        check("absent port reads missing", st[(C, D)] == "missing")

    with Rig(ports=[A, B], conns=[]) as r:
        r.write_entries([{"src": A, "dst": B, "cut": True,
                          "missing_since": None}])
        d = json.loads(client.get("/api/patch/saved").data)
        check("a wire cut elsewhere reads cut",
              d["connections"][0]["state"] == "cut")

    with Rig(ports=[A, B], conns=[], fail={(A, B): "nope"}) as r:
        r.write([[A, B]])
        W._patch_reconcile()
        d = json.loads(client.get("/api/patch/saved").data)
        check("a restore that keeps failing reads failing, not waiting",
              d["connections"][0]["state"] == "failing")
        check("and it says what broke",
              d["connections"][0]["error"] == "nope")

    with Rig(ports=[], conns=[]) as r:
        r.write([[A, B]])
        d = json.loads(client.get("/api/patch/saved").data)
        check("no graph reads unknown, never missing",
              d["connections"][0]["state"] == "unknown")


# ===================================================== STANDING NETS ======
# NOT in the sweep -- __main__ runs these last, over the never-cleared record.

def net_never_disconnects():
    """Nothing in the saved-patch code may tear down audio.

    The whole point of this subsystem is that it only ever ADDS wires. RESET,
    forget, a stale drop and a hand-off all forget things without unplugging
    them, which is what makes them safe to press mid-performance.
    """
    bad = [x for x in ALL_ISSUED if x[0] != "jack_connect"]
    # The one deliberate exception is the /api/patch disconnect endpoint, which
    # is the player explicitly asking. It is driven by exactly one test.
    allowed = [("jack_disconnect", A, B)]
    unexpected = [x for x in bad if list(x) not in [list(a) for a in allowed]]
    check("no unexpected jack_disconnect was ever issued", not unexpected,
          "issued %r" % (unexpected,))
    check("the net actually saw traffic -- it is not inspecting an empty list",
          len(ALL_ISSUED) > 5, "ALL_ISSUED has %d entries" % len(ALL_ISSUED))


def net_live_file_untouched():
    check("the real ~/.orc1_patch.json was never repointed away",
          W.PATCH_FILE == REAL_PATCH_FILE,
          "PATCH_FILE left repointed at %s" % W.PATCH_FILE)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
NETS = [net_never_disconnects, net_live_file_untouched]

if __name__ == "__main__":
    live_before = (os.path.exists(REAL_PATCH_FILE),
                   os.path.getmtime(REAL_PATCH_FILE)
                   if os.path.exists(REAL_PATCH_FILE) else None)
    for t in TESTS:
        if VERBOSE:
            print(t.__name__)
        t()
    for n in NETS:
        if VERBOSE:
            print(n.__name__)
        n()
    # Re-checked by hand as well as by the net above, because "the suite did
    # not touch the live file" is the one claim a broken suite would also make.
    live_after = (os.path.exists(REAL_PATCH_FILE),
                  os.path.getmtime(REAL_PATCH_FILE)
                  if os.path.exists(REAL_PATCH_FILE) else None)
    check("live saved-patch file unchanged on disk", live_before == live_after,
          "%r -> %r" % (live_before, live_after))
    # A suite that quietly shrinks still prints "0 failed".
    # +1 counts this check itself, so EXPECTED equals the printed total.
    check("every check ran (%d expected)" % EXPECTED,
          PASS + FAIL + 1 == EXPECTED, "counted %d" % (PASS + FAIL + 1))
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
