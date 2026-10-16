#!/usr/bin/env python3
"""Regression suite for orc1_padmap (per-device pad maps, the /pads page).

Pure stdlib: a tiny fake mido message, captured emit/action callbacks and a fake
clock. Proves the validator (the one gate the brain and the web share), name
matching, pass-through of unmapped pads, note remaps to a NAMED module resolved at
hit time (unbound = silent, never ch 1), note-offs following the note-on's
destination across a /matrix rebind, actions firing on the HIT only (the SD350's
timer note-off must not re-fire them), the 80 ms double-trigger re-arm, CC rising-
edge actions, the learn hit log, and the brain wiring (source-level: the hook sits
before channel dispatch, PADMAP_SET exists, actions use the pedal vocabulary).

Run:  python3 _test_padmap.py
"""
import json, os, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_padmap as PM
import orc1_pedal as P

EXPECTED = 71
_p = _f = 0


def check(name, cond):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ! FAIL: {name}")


class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


def on(n, v=100, ch=9):
    return Msg("note_on", note=n, velocity=v, channel=ch)


def off(n, ch=9):
    return Msg("note_off", note=n, velocity=0, channel=ch)


def cc(c, v, ch=9):
    return Msg("control_change", control=c, value=v, channel=ch)


MODS = ["drums", "poly", "chip_p1", "sampler", "visuals"]
SRC = "Simmons SD350:Simmons SD350 MIDI 1 32:0"


def make(pads, min_vel=1, binds=None):
    em, ac = [], []
    binds = dict({"drums": 10, "poly": 6, "sampler": 7} if binds is None else binds)
    clock = [100.0]
    pm = PM.PadMap(emit=lambda k, ch, a, b: em.append((k, ch, a, b)),
                   module_ch=lambda m: binds.get(m), run_action=ac.append,
                   log=lambda s: None)
    pm.spawn = pm._run                       # run actions inline for the test
    pm.clock = lambda: clock[0]
    conf, errs = PM.validate_config(
        {"devices": {"Simmons SD350": {"port_match": "Simmons", "min_vel": min_vel,
                                       "pads": pads}}}, MODS)
    pm.set_config(conf)
    return pm, em, ac, binds, clock, errs


def t_validator():
    raw = {"devices": {"Kit": {"port_match": "Simmons", "pads": {
        "note:36": {"do": "note", "module": "drums", "note": 40},
        "note:38": {"do": "note", "module": "nonsense", "note": 40},
        "note:40": {"do": "action", "action": "cmd:SKIPBACK_SAVE_ALL", "min_vel": 50},
        "note:41": {"do": "action", "action": "primary"},
        "note:42": {"do": "action", "action": "page:up"},
        "note:43": {"do": "action", "action": "transition"},
        "note:44": {"do": "action", "action": "cmd:PADMAP_SET"},
        "note:45": {"do": "cc", "module": "poly", "cc": 74},
        "cc:4":    {"do": "note", "module": "drums", "note": 1},
        "cc:5":    {"do": "cc", "module": "poly", "cc": 200},
        "cc:7":    {"do": "cc", "module": "poly", "cc": 74},
        "note:46": {"do": "pass"},
        "note:47": {"do": "pass", "label": "spare"},
        "note:200": {"do": "ignore"},
        "bogus":   {"do": "ignore"},
        "note:48": {"do": "note", "module": "drums", "note": 50, "vel": 0},
        "note:49": {"do": "action", "action": "rc505:tr2_rec_play"},
        "note:50": {"do": "action", "action": "chord:4"},
        "note:51": {"do": "action", "action": "song_next"},
    }}}}
    conf, errs = PM.validate_config(raw, MODS)
    pads = conf["devices"]["Kit"]["pads"]
    check("valid note remap kept", pads.get("note:36") == {"do": "note", "module": "drums",
                                                           "note": 40, "vel": "same"})
    check("unknown module refused", "note:38" not in pads)
    check("action + min_vel kept", pads.get("note:40", {}).get("min_vel") == 50)
    check("'primary' (needs a hold) refused", "note:41" not in pads)
    check("page flips refused", "note:42" not in pads)
    check("transition toggle refused", "note:43" not in pads)
    check("non-pedal cmd refused? (cmd:PADMAP_SET is a valid cmd name)", "note:44" in pads)
    check("note pad can't 'send CC'", "note:45" not in pads)
    check("cc pad can't 'play note'", "cc:4" not in pads)
    check("cc out of range refused", "cc:5" not in pads)
    check("cc remap kept", pads.get("cc:7") == {"do": "cc", "module": "poly", "cc": 74})
    check("bare pass not stored", "note:46" not in pads)
    check("labelled pass kept (learned pad)", pads.get("note:47", {}).get("label") == "spare")
    check("note 200 refused", "note:200" not in pads)
    check("bad key refused", "bogus" not in pads)
    check("vel 0 refused", "note:48" not in pads)
    check("rc505 tap kept", "note:49" in pads)
    check("chord kept", "note:50" in pads)
    check("BeatBuddy gesture kept", "note:51" in pads)
    check("every refusal reported (10)", len(errs) == 10)
    # garbage never crashes
    for junk in (None, [], "x", {"devices": "x"}, {"devices": {"": {}}},
                 {"devices": {"a": {"pads": "x", "min_vel": "loud"}}}):
        try:
            c, e = PM.validate_config(junk, MODS)
            ok = isinstance(c, dict) and "devices" in c
        except Exception:
            ok = False
        check(f"junk config {junk!r:.30} -> clean default", ok)


def t_choices():
    vals = [v for v, _ in PM.ACTION_CHOICES]
    check("every offered action validates", all(PM.valid_action(v) for v in vals))
    check("no hold-style action offered",
          not ({"primary", "transition", "page:up", "page:down"} & set(vals)))
    check("skipback GRAB ALL offered", "cmd:SKIPBACK_SAVE_ALL" in vals)
    check("RC-505 + chords offered", "rc505:tr1_rec_play" in vals and "chord:1" in vals)


def t_routing():
    pm, em, ac, binds, clock, errs = make({
        "note:38": {"do": "note", "module": "poly", "note": 60},
        "note:45": {"do": "note", "module": "drums", "note": 49, "vel": 90},
        "note:48": {"do": "ignore"},
        "note:51": {"do": "note", "module": "chip_p1", "note": 70},
    })
    check("config clean", errs == [])
    check("other device untouched", pm.handle(on(38), "Akai MPK Mini") is False and not em)
    check("no source name -> untouched", pm.handle(on(38), "") is False)
    check("unmapped pad passes through", pm.handle(on(36), SRC) is False and not em)
    check("ignored pad swallowed", pm.handle(on(48), SRC) is True and not em)
    check("clock/sysex never claimed", pm.handle(Msg("clock"), SRC) is False)
    pm.handle(on(38, 77), SRC)
    check("remap -> named module's LIVE channel, hit velocity", em[-1] == ("note_on", 6, 60, 77))
    binds["poly"] = 3                          # /matrix moves poly while the note rings
    pm.handle(off(38), SRC)
    check("note-off follows the note-on's destination", em[-1] == ("note_off", 6, 60, 0))
    pm.handle(on(38, 50), SRC)
    check("next hit uses the NEW binding", em[-1] == ("note_on", 3, 60, 50))
    pm.handle(on(38, 51), SRC)
    check("re-hit releases the previous note first",
          em[-2] == ("note_off", 3, 60, 0) and em[-1] == ("note_on", 3, 60, 51))
    pm.handle(on(45, 20), SRC)
    check("fixed velocity", em[-1] == ("note_on", 10, 49, 90))
    n = len(em)
    check("unbound module: consumed, silent", pm.handle(on(51), SRC) is True and len(em) == n)
    check("orphan note-off swallowed quietly", pm.handle(off(51), SRC) is True and len(em) == n)
    binds["poly"] = 13
    pm.handle(off(38), SRC); n = len(em)
    pm.handle(on(38), SRC)
    check("never emits on the control channel", len(em) == n)


def t_min_vel_and_release():
    pm, em, ac, binds, clock, _ = make({"note:38": {"do": "note", "module": "drums",
                                                    "note": 38}}, min_vel=30)
    pm.handle(on(38, 20), SRC)
    check("ghost hit under device min_vel: silent", em == [])
    pm.handle(on(38, 90), SRC)
    check("real hit plays", em[-1] == ("note_on", 10, 38, 90))
    conf, _ = PM.validate_config({"devices": {}}, MODS)
    pm.set_config(conf)
    check("removing the map releases the sounding note", em[-1] == ("note_off", 10, 38, 0))
    check("...and the device passes through again", pm.handle(on(38), SRC) is False)


def t_actions():
    pm, em, ac, binds, clock, _ = make({
        "note:36": {"do": "action", "action": "cmd:SKIPBACK_SAVE_ALL", "min_vel": 60},
        "note:49": {"do": "action", "action": "rc505:tr2_rec_play"},
        "cc:4":    {"do": "action", "action": "chord:4", "min_vel": 64},
        "cc:11":   {"do": "cc", "module": "poly", "cc": 74},
    })
    pm.handle(on(36, 40), SRC)
    check("soft hit under the pad's min_vel: no action", ac == [])
    pm.handle(on(36, 100), SRC)
    check("hit fires the action once", ac == ["cmd:SKIPBACK_SAVE_ALL"])
    clock[0] += 2.4
    pm.handle(off(36), SRC)
    check("the 2.4 s timer note-off does NOT fire it again", len(ac) == 1)
    pm.handle(on(49, 30), SRC)
    clock[0] += 0.010                           # measured double-trigger: 10 ms
    pm.handle(off(49), SRC); pm.handle(on(49, 47), SRC)
    check("double-trigger within 80 ms fires once", ac.count("rc505:tr2_rec_play") == 1)
    clock[0] += 0.100
    pm.handle(on(49, 60), SRC)
    check("a real 2nd hit later fires again", ac.count("rc505:tr2_rec_play") == 2)
    check("actions emit no notes", em == [])
    pm.handle(cc(4, 10), SRC); pm.handle(cc(4, 63), SRC)
    check("cc under threshold: nothing", "chord:4" not in ac)
    pm.handle(cc(4, 90), SRC)
    check("cc rising past threshold fires", ac.count("chord:4") == 1)
    clock[0] += 1
    pm.handle(cc(4, 120), SRC); pm.handle(cc(4, 100), SRC)
    check("staying above threshold doesn't re-fire", ac.count("chord:4") == 1)
    pm.handle(cc(4, 5), SRC); clock[0] += 1; pm.handle(cc(4, 70), SRC)
    check("falling then rising fires again", ac.count("chord:4") == 2)
    pm.handle(cc(11, 33), SRC)
    check("cc remap -> module channel, value kept", em[-1] == ("cc", 6, 74, 33))
    check("unmapped cc passes", pm.handle(cc(1, 5), SRC) is False)
    # an action that raises must not kill the input thread
    pm.run_action = lambda a: 1 / 0
    clock[0] += 1
    try:
        pm.handle(on(36, 120), SRC); ok = True
    except Exception:
        ok = False
    check("a failing action is contained", ok)


def t_hits():
    d = tempfile.mkdtemp()
    pm, em, ac, binds, clock, _ = make({})
    pm.hits_file = os.path.join(d, "hits.json")
    pm.handle(on(36, 99), SRC); pm.handle(off(36), SRC); pm.handle(cc(4, 50), SRC)
    pm.handle(on(38), "Other device")
    h = json.load(open(pm.hits_file))["hits"]
    check("learn log: hits of a profiled device (note-offs not logged)",
          [(x["key"], x["value"]) for x in h] == [("note:36", 99), ("cc:4", 50)])
    check("hits carry the device + increasing seq",
          h[0]["device"] == "Simmons SD350" and h[1]["seq"] > h[0]["seq"])
    for i in range(50):
        pm.handle(on(40, 1 + i), SRC)
    check("hit log bounded", len(json.load(open(pm.hits_file))["hits"]) == PM.PadMap.HITS_KEEP)
    os.remove(pm.hits_file); os.rmdir(d)


def t_brain_wiring():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py"),
               encoding="utf-8").read()
    rm = src[src.index("def _route_message("):]
    mh = re.search(r"\n    if padmap\.handle\(msg, source_name\):\r?\n        return\r?\n", rm)
    i_hook = mh.start() if mh else -1
    i_pre = rm.find("_pre_stage_channel_ok(")
    i_ch14 = rm.find("if ch == 14:")
    check("brain hooks the pad map in _route_message", i_hook > 0)
    check("...before the pre-stage router and channel dispatch",
          0 < i_hook < i_pre and i_hook < i_ch14)
    check("PADMAP_SET command exists", 'action == "PADMAP_SET"' in src)
    fa = src[src.index("def _padmap_action("):src.index("def _padmap_conf_set(")]
    check("BeatBuddy gestures -> the mind", "_bb_pedal_action(action)" in fa)
    check("other actions = press-only pedal switch",
          "_pedal_switch_action(action, True, False)" in fa)
    check("module resolved live via _module_channel",
          re.search(r"module_ch=lambda m: _module_channel\(m\)", src) is not None)
    web = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1_web.py"),
               encoding="utf-8").read()
    check("web validates with the same validator before sending",
          web.count("_pm.validate_config(") == 2 and 'write_cmd("PADMAP_SET"' in web)


def main():
    for t in (t_validator, t_choices, t_routing, t_min_vel_and_release, t_actions,
              t_hits, t_brain_wiring):
        t()
    total = _p + _f
    print(f"_test_padmap: {_p} passed, {_f} failed ({total} checks)")
    if total != EXPECTED:
        print(f"  ! EXPECTED {EXPECTED} checks, ran {total} -- a test went missing or was added")
        sys.exit(1)
    sys.exit(1 if _f else 0)


if __name__ == "__main__":
    main()
