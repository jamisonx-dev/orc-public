#!/usr/bin/env python3
"""/patch into a SKIPBACK track sets that track's source (orc1_web.py, 2026-11-12).

    python3 _test_patch_skipback.py [-v]

owner: "can't correctly assign skipback/sampling channels". Measured that
evening: he wired BBKit, then Drums, into skipback-rec:t2_L/R on /patch; the
recorder (skipback_recorder._wire) keeps ONLY the source in
~/.orc1_skipback_tracks.json and cut both within 5 s; a forced cut of the
capture feed was re-made by the recorder too. His call: wiring a source into a
skipback track REPLACES its source. So a /patch connect into skipback-rec now
writes the track's manual source (the recorder then keeps it), is never saved
in the /patch saved list (that would be re-made at boot and cut again), and a
disconnect is refused with the way to change it instead of "removed".

SAFETY: jack is never run (_jack, _jackd_servers, _patch_run are faked). HOME
and USERPROFILE point at a temp dir before import, and every file the code
could write is repointed at import; __main__ checks the real files are
byte-identical afterwards.
"""

import json, os, shutil, sys, tempfile

_SANDBOX = tempfile.mkdtemp(prefix="patchsb_suite_")
os.environ["HOME"] = os.environ["USERPROFILE"] = _SANDBOX

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_web as W                                        # noqa: E402
import orc1_config as _cfg_suite                              # noqa: E402

_REAL_HOME = os.path.expanduser("~pibox") if os.name != "nt" else ""
REAL = [p for p in (os.path.join(_REAL_HOME, ".orc1_skipback_tracks.json"),
                    os.path.join(_REAL_HOME, ".orc1_patch.json"))
        if _REAL_HOME and not p.startswith(_SANDBOX)]
W.PATCH_FILE = os.path.join(_SANDBOX, "patch.json")
W.SKIPBACK_TRACKS = os.path.join(_SANDBOX, "tracks.json")
W.SKIPBACK_STATUS = os.path.join(_SANDBOX, "status.json")
W.SKIPBACK_SRC = os.path.join(_SANDBOX, "src.json")
_cfg_suite.MODULE_OUT_FILE = os.path.join(_SANDBOX, "mo.json")

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 32


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


PORTS = [("system:capture_1", "out"), ("system:capture_2", "out"),
         ("system:playback_1", "in"), ("system:playback_2", "in"),
         ("ORC1-Drum:out_L", "out"), ("ORC1-Drum:out_R", "out"),
         ("ORC1-BBKit:out_L", "out"), ("ORC1-BBKit:out_R", "out"),
         ("skipback-rec:t2_L", "in"), ("skipback-rec:t2_R", "in"),
         ("skipback-rec:t3_M", "in")]

TRACKS = [
    {"id": 2, "name": "SMP MAIN", "stereo": True, "length_secs": 60,
     "armed": True, "store": "ram", "persist": True,
     "source": {"mode": "manual", "left": "system:capture_1",
                "right": "system:capture_2"}},
    {"id": 3, "name": "SMP IN3", "stereo": False, "length_secs": 60,
     "armed": True, "store": "ram", "persist": True,
     "source": {"mode": "manual", "left": "system:capture_1"}},
]


class Fake:
    def __init__(self, conns=()):
        self.conns = list(conns)
        self.runs = []

    def jack(self, args, server=None):
        if server is not None:
            return ""
        if args[:2] == ["jack_lsp", "-c"]:
            by = {}
            for a, b in self.conns:
                by.setdefault(a, []).append(b)
                by.setdefault(b, []).append(a)
            out = []
            for p, o in by.items():
                out.append(p)
                out += ["   " + x for x in o]
            return "\n".join(out) + "\n"
        out = []
        for n, d in PORTS:
            out += [n, "\tproperties: %s,terminal," %
                    ("input" if d == "in" else "output"),
                    "\t32 bit float mono audio"]
        return "\n".join(out) + "\n"

    def run(self, tool, a, b):
        self.runs.append((tool, a, b))
        if tool == "jack_connect":
            if (a, b) not in self.conns:
                self.conns.append((a, b))
        elif (a, b) in self.conns:
            self.conns.remove((a, b))
        return None


def setup(tracks=TRACKS, status=None, saved=None):
    with open(W.SKIPBACK_TRACKS, "w") as f:
        json.dump({"tracks": json.loads(json.dumps(tracks))}, f)
    with open(W.SKIPBACK_STATUS, "w") as f:
        json.dump(status or {"tracks": []}, f)
    with open(W.PATCH_FILE, "w") as f:
        json.dump({"version": 2, "connections": saved or []}, f)


def tracks():
    with open(W.SKIPBACK_TRACKS) as f:
        return {t["id"]: t for t in json.load(f)["tracks"]}


def post(fake, body):
    saved = (W._jack, W._jackd_servers, W._patch_run)
    W._jack, W._jackd_servers, W._patch_run = (
        fake.jack, lambda: [(None, "pisound")], fake.run)
    try:
        r = W.app.test_client().post("/api/patch", json=body)
        return r.status_code, r.get_json()
    finally:
        W._jack, W._jackd_servers, W._patch_run = saved
        W._patch_unreachable.clear()


def test_stereo_connect_replaces_source():
    setup()
    f = Fake(conns=[("system:capture_1", "skipback-rec:t2_L"),
                    ("system:capture_2", "skipback-rec:t2_R")])
    code, d = post(f, {"op": "connect",
                       "src": ["ORC1-Drum:out_L", "ORC1-Drum:out_R"],
                       "dst": ["skipback-rec:t2_L", "skipback-rec:t2_R"]})
    check("stereo connect into a skipback track succeeds",
          code == 200 and d.get("ok"), (code, d))
    t = tracks()
    check("the track's source is now the wired pair (what the recorder keeps)",
          t[2]["source"] == {"mode": "manual", "left": "ORC1-Drum:out_L",
                             "right": "ORC1-Drum:out_R"}, t[2]["source"])
    check("the other track is untouched", t[3] == TRACKS[1], t[3])
    check("the rest of the track is untouched",
          {k: v for k, v in t[2].items() if k != "source"} ==
          {k: v for k, v in TRACKS[0].items() if k != "source"}, t[2])
    check("the new wire is also made live at once",
          ("jack_connect", "ORC1-Drum:out_L", "skipback-rec:t2_L") in f.runs, f.runs)
    check("the response says which source was set",
          d.get("skipback_source", {}).get("2", {}).get("left") == "ORC1-Drum:out_L", d)
    check("skipback wires are NOT put in the /patch saved list",
          not any(p[1].startswith("skipback-rec:") for p in W._patch_saved_pairs()),
          W._patch_saved_pairs())


def test_mono_track():
    setup()
    code, d = post(Fake(), {"op": "connect", "src": ["ORC1-BBKit:out_L"],
                            "dst": ["skipback-rec:t3_M"]})
    check("mono connect succeeds", code == 200, (code, d))
    check("a mono track's source is the one wired port",
          tracks()[3]["source"] == {"mode": "manual", "left": "ORC1-BBKit:out_L"},
          tracks()[3]["source"])


def test_one_source_to_both_sides():
    setup()
    code, d = post(Fake(), {"op": "connect", "src": ["ORC1-BBKit:out_L"],
                            "dst": ["skipback-rec:t2_L", "skipback-rec:t2_R"]})
    check("one source fanned to L and R succeeds", code == 200, (code, d))
    s = tracks()[2]["source"]
    check("both sides record it", s.get("left") == s.get("right") ==
          "ORC1-BBKit:out_L", s)


def test_one_side_keeps_the_other():
    auto = json.loads(json.dumps(TRACKS))
    auto[0]["source"] = {"mode": "auto"}
    setup(tracks=auto, status={"tracks": [
        {"id": 2, "resolved": ["system:capture_1", "system:capture_2"]}]})
    code, d = post(Fake(), {"op": "connect", "src": ["ORC1-Drum:out_L"],
                            "dst": ["skipback-rec:t2_L"]})
    check("re-wiring one side succeeds", code == 200, (code, d))
    s = tracks()[2]["source"]
    check("that side changes, the other keeps what it recorded",
          s == {"mode": "manual", "left": "ORC1-Drum:out_L",
                "right": "system:capture_2"}, s)


def test_two_sources_into_one_port_refused():
    setup()
    before = open(W.SKIPBACK_TRACKS).read()
    f = Fake()
    code, d = post(f, {"op": "connect",
                       "src": ["ORC1-Drum:out_L", "ORC1-BBKit:out_L"],
                       "dst": ["skipback-rec:t3_M"]})
    check("two sources into one skipback port is refused",
          code == 400 and not d.get("ok"), (code, d))
    check("refused = tracks file unchanged",
          open(W.SKIPBACK_TRACKS).read() == before)
    check("refused = nothing wired", f.runs == [], f.runs)


def test_disconnect_refused_even_forced():
    setup()
    before = open(W.SKIPBACK_TRACKS).read()
    f = Fake(conns=[("system:capture_1", "skipback-rec:t3_M")])
    for force in (False, True):
        code, d = post(f, {"op": "disconnect", "src": ["system:capture_1"],
                           "dst": ["skipback-rec:t3_M"], "force": force})
        check("disconnect of a skipback feed refused (force=%s)" % force,
              code == 409 and not d.get("ok") and "wire another source" in
              (d.get("error") or ""), (code, d))
        check("no confirm-then-force loop (force=%s)" % force,
              "protected" not in d, d)
    check("disconnect never reached jack", f.runs == [], f.runs)
    check("disconnect left the tracks file alone",
          open(W.SKIPBACK_TRACKS).read() == before)


def test_old_saved_skipback_rows_dropped():
    setup(saved=[{"src": "ORC1-BBKit:out_L", "dst": "skipback-rec:t2_L",
                  "cut": True, "missing_since": None},
                 {"src": "ORC1-Drum:out_L", "dst": "system:playback_1",
                  "cut": False, "missing_since": None}])
    pairs = W._patch_saved_pairs()
    check("an old saved wire into skipback is no longer listed",
          ["ORC1-BBKit:out_L", "skipback-rec:t2_L"] not in pairs, pairs)
    check("ordinary saved wires stay",
          ["ORC1-Drum:out_L", "system:playback_1"] in pairs, pairs)


def test_ordinary_wire_unchanged():
    setup()
    before = open(W.SKIPBACK_TRACKS).read()
    code, d = post(Fake(), {"op": "connect", "src": ["ORC1-Drum:out_L"],
                            "dst": ["system:playback_1"]})
    check("an ordinary connect still works", code == 200 and d.get("ok"), (code, d))
    check("and is still saved", ["ORC1-Drum:out_L", "system:playback_1"]
          in W._patch_saved_pairs(), W._patch_saved_pairs())
    check("and leaves skipback alone", open(W.SKIPBACK_TRACKS).read() == before)


def test_source_labels_follow_the_card():
    saved = W._patch_card
    W._patch_card = lambda: "pisound"
    try:
        pis = W._labeled_sources(["system:capture_1", "system:capture_2"])
        l12 = W._labeled_sources(["system:capture_%d" % i for i in range(1, 15)])
    finally:
        W._patch_card = saved
    labels = [x["label"] for x in pis]
    check("on the pisound, inputs 1-2 are not called Zoom",
          not any("Zoom" in x for x in labels), labels)
    check("they are named after the card", "pisound In 1-2" in labels, labels)
    l12l = [x["label"] for x in l12]
    check("with the L-12 up its names stay",
          "L-12 Master" in l12l and "L-12 In 1-2" in l12l, l12l)


def _snap(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


if __name__ == "__main__":
    before = {p: _snap(p) for p in REAL}
    for t in (test_stereo_connect_replaces_source, test_mono_track,
              test_one_source_to_both_sides, test_one_side_keeps_the_other,
              test_two_sources_into_one_port_refused,
              test_disconnect_refused_even_forced,
              test_old_saved_skipback_rows_dropped, test_ordinary_wire_unchanged,
              test_source_labels_follow_the_card):
        try:
            t()
        except Exception as e:
            check(t.__name__ + " raised", False, repr(e))
    check("the real tracks / patch files were not touched",
          all(_snap(p) == before[p] for p in REAL))
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    # +1 counts this check itself, so EXPECTED equals the printed total.
    check("every check ran (%d expected)" % EXPECTED,
          PASS + FAIL + 1 == EXPECTED, "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
