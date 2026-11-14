#!/usr/bin/env python3
"""The skipback TRACK picker: plain names + immediate source / arm (2026-11-12).

    python3 _test_skipback_picker.py [-v]

owner: "it's all really confusing". One "Records" picker for /skipback and
/sampler (static/js/sb_tracks.js) in plain names ("pisound In 1-2", "L-12
Master", "Drums"), applied at once by POST /api/skipback/tracks/<id>/source.
A pair = stereo, a single input = mono: "what i pick, always" (skipback is
ephemeral, so the width change is not asked about). Tracks are shown by name,
never as t1 / t2. Checks the names, the endpoint, that the RECORDER accepts
and resolves exactly what was picked, and the set-vs-getting words.

SAFETY: jack is never run (_jack_sources / _patch_card faked). HOME and
USERPROFILE point at a temp dir before import; every file the code could
write is repointed; __main__ checks the real tracks file is byte-identical.
"""

import json, os, shutil, sys, tempfile, types

_SANDBOX = tempfile.mkdtemp(prefix="sbpick_suite_")
os.environ["HOME"] = os.environ["USERPROFILE"] = _SANDBOX
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "skipback"))
try:
    import jack  # noqa: F401
except Exception:
    sys.modules["jack"] = types.ModuleType("jack")
import orc1_web as W                                        # noqa: E402
import skipback_recorder as SR                              # noqa: E402

REAL = (os.path.join(os.path.expanduser("~pibox"), ".orc1_skipback_tracks.json")
        if os.name != "nt" else None)
W.SKIPBACK_TRACKS = os.path.join(_SANDBOX, "tracks.json")
W.SKIPBACK_STATUS = os.path.join(_SANDBOX, "status.json")
W.SKIPBACK_SRC = os.path.join(_SANDBOX, "src.json")
W.PATCH_FILE = os.path.join(_SANDBOX, "patch.json")

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 52


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


MODS = ["ORC1-Drum:out_L", "ORC1-Drum:out_R", "ORC1-BBKit:out_L", "ORC1-BBKit:out_R"]
PISOUND = ["system:capture_1", "system:capture_2"] + MODS
L12 = ["system:capture_%d" % i for i in range(1, 15)] + MODS

TRACKS = [
    {"id": 1, "name": "T1", "stereo": False, "length_secs": 900, "armed": False,
     "store": "card", "persist": True,
     "source": {"mode": "manual", "left": "system:capture_3"}},
    {"id": 2, "name": "SMP MAIN", "stereo": True, "length_secs": 60, "armed": True,
     "store": "ram", "persist": True,
     "source": {"mode": "manual", "left": "system:capture_13",
                "right": "system:capture_14"}},
    {"id": 5, "name": "BIG", "stereo": False, "length_secs": 7200, "armed": True,
     "store": "ram", "persist": True,
     "source": {"mode": "manual", "left": "system:capture_1"}},
]


class Rig:
    def __init__(self, ports, card="pisound"):
        self.ports, self.card = ports, card

    def __enter__(self):
        # free RAM is the PI's, not the test's: the guard runs budget-only
        self.saved = (W._jack_sources, W._patch_card, W._sb_mem_available_mb)
        W._jack_sources = lambda: list(self.ports)
        W._patch_card = lambda: self.card
        W._sb_mem_available_mb = lambda: None
        return self

    def __exit__(self, *a):
        W._jack_sources, W._patch_card, W._sb_mem_available_mb = self.saved


def setup(tracks=TRACKS, status=None):
    with open(W.SKIPBACK_TRACKS, "w") as f:
        json.dump({"tracks": json.loads(json.dumps(tracks))}, f)
    with open(W.SKIPBACK_STATUS, "w") as f:
        json.dump(status or {"tracks": []}, f)


def tracks():
    with open(W.SKIPBACK_TRACKS) as f:
        return {t["id"]: t for t in json.load(f)["tracks"]}


def post(url, body):
    r = W.app.test_client().post(url, json=body)
    return r.status_code, r.get_json()


def test_names_on_the_pisound():
    with Rig(PISOUND):
        ch = W._sb_choices()
    by = {c["key"]: c for c in ch}
    check("AUTO is the first choice", ch[0]["key"] == "auto", ch[0])
    check("the card's pair is named after the card",
          by.get("system:capture_1|system:capture_2", {}).get("label") == "pisound In 1-2", by)
    check("nothing says Zoom on the pisound",
          not any("Zoom" in c["label"] or "L-12" in c["label"] for c in ch),
          [c["label"] for c in ch])
    check("a single input is a mono choice",
          by.get("system:capture_1", {}).get("right") == "" and
          by["system:capture_1"]["label"] == "pisound In 1", by.get("system:capture_1"))
    check("modules are named plainly", by.get("ORC1-Drum:out_L|ORC1-Drum:out_R", {})
          .get("label") == "Drums", by)


def test_names_on_the_l12():
    with Rig(L12, card="L12"):
        ch = W._sb_choices()
    labels = [c["label"] for c in ch]
    by = {c["key"]: c for c in ch}
    check("the L-12 master is one choice",
          by.get("system:capture_13|system:capture_14", {}).get("label") == "L-12 Master", labels)
    check("and not also an 'In 13-14' pair", "L-12 In 13-14" not in labels, labels)
    check("its pairs say L-12", "L-12 In 1-2" in labels, labels)
    check("its single inputs too", "L-12 In 3" in labels, labels)


def test_set_vs_getting_words():
    setup()
    with Rig(PISOUND):
        st = {"resolved": ["system:capture_1", "system:capture_2"]}
        w = W._sb_track_words(TRACKS[1], st, None, None)
        w1 = W._sb_track_words(TRACKS[0], {}, None, None)
    check("a missing L-12 pick is still named", w["source_label"] == "L-12 Master", w)
    check("and marked not connected now", w["source_present"] is False, w)
    check("what it really gets, in words", w["now_label"] == "pisound In 1-2", w)
    check("set and getting differ -> said", w["now_differs"] is True, w)
    check("a real name is kept", w["display_name"] == "SMP MAIN", w)
    check("a T<n> name is never shown", w1["display_name"] == "L-12 In 3"
          or w1["display_name"] == "In 3", w1)


def _recorder_takes(t, names):
    cfg, why = SR._valid_track(t, set())
    if cfg is None:
        return None, why
    return SR.Recorder._resolve(None, cfg, set(names)), None


def test_pick_a_module_stereo():
    setup()
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks/1/source",
                       {"key": "ORC1-Drum:out_L|ORC1-Drum:out_R"})
    check("picking a module succeeds", code == 200 and d.get("ok"), (code, d))
    t = tracks()[1]
    check("a pair makes the track STEREO (it was mono)", t["stereo"] is True, t)
    check("the source is the picked pair",
          t["source"] == {"mode": "manual", "left": "ORC1-Drum:out_L",
                          "right": "ORC1-Drum:out_R"}, t["source"])
    check("its T1 name follows the source", t["name"] == "Drums", t["name"])
    res, why = _recorder_takes(t, PISOUND)
    check("the recorder accepts it and records exactly the picked ports",
          res == ["ORC1-Drum:out_L", "ORC1-Drum:out_R"], (res, why))
    check("other tracks untouched", tracks()[2] == TRACKS[1], tracks()[2])


def test_pick_one_input_mono():
    setup()
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks/2/source", {"key": "system:capture_2"})
    t = tracks()[2]
    check("one input succeeds", code == 200, (code, d))
    check("one input makes the track MONO", t["stereo"] is False, t)
    check("with no right port", t["source"] == {"mode": "manual",
                                                 "left": "system:capture_2"}, t["source"])
    check("a real name is never renamed", t["name"] == "SMP MAIN", t["name"])
    res, why = _recorder_takes(t, PISOUND)
    check("the recorder records that one port", res == ["system:capture_2"], (res, why))


def test_pick_auto():
    setup()
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks/1/source", {"key": "auto"})
    t = tracks()[1]
    check("AUTO succeeds", code == 200, (code, d))
    check("AUTO = stereo, auto mode", t["stereo"] is True and
          t["source"] == {"mode": "auto"}, t)
    res, why = _recorder_takes(t, PISOUND)
    check("the recorder resolves AUTO on the pisound to its inputs",
          res == ["system:capture_1", "system:capture_2"], (res, why))


def test_custom_ports():
    setup()
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks/1/source",
                       {"left": "ORC1-BBKit:out_L", "right": ""})
    t = tracks()[1]
    check("custom single port accepted", code == 200 and t["stereo"] is False and
          t["source"]["left"] == "ORC1-BBKit:out_L", (code, t))


def test_refusals_leave_the_file_alone():
    setup()
    before = open(W.SKIPBACK_TRACKS).read()
    with Rig(PISOUND):
        c1, d1 = post("/api/skipback/tracks/9/source", {"key": "auto"})
        c2, d2 = post("/api/skipback/tracks/1/source", {"key": "nonsense"})
        # BIG is 7200 s mono (659 MB); stereo would be 1318 MB > the 900 MB budget
        c3, d3 = post("/api/skipback/tracks/5/source",
                      {"key": "system:capture_1|system:capture_2"})
        c4, d4 = post("/api/skipback/tracks/2/arm", {"armed": "yes"})
    check("unknown track = 404", c1 == 404, (c1, d1))
    check("a key that is no port = 400", c2 == 400, (c2, d2))
    check("over the RAM budget = refused with why", c3 == 400 and "budget" in
          (d3.get("error") or ""), (c3, d3))
    check("arm needs true/false", c4 == 400, (c4, d4))
    check("every refusal left the tracks file unchanged",
          open(W.SKIPBACK_TRACKS).read() == before)


def test_arm():
    setup()
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks/1/arm", {"armed": True})
        on = tracks()[1]["armed"]
        code2, d2 = post("/api/skipback/tracks/1/arm", {"armed": False})
    check("ARM applies at once", code == 200 and on is True, (code, d))
    check("OFF applies at once", code2 == 200 and tracks()[1]["armed"] is False, (code2, d2))
    check("arming leaves the source alone", tracks()[1]["source"] == TRACKS[0]["source"])


def test_legacy_shim_removed():
    setup()
    with open(W.SKIPBACK_SRC, "w") as f:
        json.dump({"mode": "auto"}, f)
    with Rig(PISOUND):
        post("/api/skipback/tracks/2/source", {"key": "system:capture_1|system:capture_2"})
    check("a source pick retires the legacy track-0 src.json",
          not os.path.exists(W.SKIPBACK_SRC))


def test_get_carries_words_and_choices():
    setup(status={"tracks": [{"id": 2, "resolved": ["system:capture_1",
                                                      "system:capture_2"],
                              "recording": True, "connected": True}]})
    with Rig(PISOUND):
        r = W.app.test_client().get("/api/skipback/tracks")
    d = r.get_json()
    t2 = [t for t in d["tracks"] if t["id"] == 2][0]
    check("GET lists the choices", d.get("choices") and d["choices"][0]["key"] == "auto",
          d.get("choices"))
    check("GET says what a track is set to", t2.get("source_label") == "L-12 Master", t2)
    check("and what it is getting", t2.get("now_label") == "pisound In 1-2", t2)
    check("and its name", t2.get("display_name") == "SMP MAIN", t2)


def test_new_track_named_after_its_source():
    setup()
    rows = [dict(TRACKS[1])] + [{"id": None, "name": "", "stereo": True,
            "length_secs": 30, "armed": False, "store": "ram", "persist": True,
            "source": {"mode": "manual", "left": "ORC1-BBKit:out_L",
                       "right": "ORC1-BBKit:out_R"}}]
    with Rig(PISOUND):
        code, d = post("/api/skipback/tracks", {"action": "save", "tracks": rows})
    names = [t["name"] for t in (d.get("tracks") or [])]
    check("a new nameless track is named after its source, not T<n>",
          code == 200 and "BeatBuddy Kit" in names, (code, d))


def test_pages_use_the_picker():
    js = open(os.path.join(HERE, "static", "js", "sb_tracks.js"), encoding="utf-8").read()
    sk = open(os.path.join(HERE, "static", "js", "skipback.js"), encoding="utf-8").read()
    sm = open(os.path.join(HERE, "static", "js", "sampler.js"), encoding="utf-8").read()
    th = open(os.path.join(HERE, "templates", "skipback.html"), encoding="utf-8").read()
    tm = open(os.path.join(HERE, "templates", "sampler.html"), encoding="utf-8").read()
    check("the shared picker posts to the per-track source endpoint",
          "/api/skipback/tracks/${tid}/source" in js)
    check("/skipback and /sampler both load it",
          "sb_tracks.js" in th and "sb_tracks.js" in tm)
    check("/sampler's track rows use it", "SBT.options(" in sm and "smpTrackSrc" in sm)
    check("/sampler lists every track, not only armed ones",
          "armed.concat(off)" in sm)
    check("/skipback's rows use it", "SBT.options(" in sk and "SBT.setSource(" in sk)
    check("/skipback no longer shows t<id> badges", "`t${t.id}`" not in sk
          and "'t' + t)" not in sk)


def _snap(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except (OSError, TypeError):
        return None


if __name__ == "__main__":
    before = _snap(REAL)
    for t in (test_names_on_the_pisound, test_names_on_the_l12,
              test_set_vs_getting_words, test_pick_a_module_stereo,
              test_pick_one_input_mono, test_pick_auto, test_custom_ports,
              test_refusals_leave_the_file_alone, test_arm, test_legacy_shim_removed,
              test_get_carries_words_and_choices,
              test_new_track_named_after_its_source, test_pages_use_the_picker):
        try:
            t()
        except Exception as e:
            check(t.__name__ + " raised", False, repr(e))
    check("the real tracks file was not touched", _snap(REAL) == before)
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    check("every check ran (%d expected)" % EXPECTED,
          PASS + FAIL + 1 == EXPECTED, "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
