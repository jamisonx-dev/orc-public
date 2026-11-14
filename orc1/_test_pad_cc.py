#!/usr/bin/env python3
"""
Pads sending ANY CC (or note) to an ORC module or to an OUTPUT PORT + CHANNEL (2026-10-19).

    python3 _test_pad_cc.py [-v]

owner: "can it not be set to send an arbitrary cc to a channel?" A NOTE pad (a pad
controller, a drum pad) can send a CC: value = its hit velocity or fixed, behaviour
press / momentary (value then 0 on the release: needs a device with a real release) / toggle. A note or CC can
go to a named ORC module (resolved live from /matrix) or to a cfg.PORTS output on its own
channel (never 13). Also checks the page offers it, labelled, and the brain routes a port.

Pure: no port, no file, no service; the web part uses Flask's test client.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_padmap as P                                      # noqa: E402

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 34
HERE = os.path.dirname(os.path.abspath(__file__))
MODS = ["drums", "poly"]
PORTS = {"pisound", "rtp"}
PADS_IN = "LPD8:LPD8 MIDI 1 32:0"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def conf_with(pads, name="Pads", match="LPD8", ports=PORTS):
    raw = {"devices": {name: {"port_match": match, "pads": pads}}}
    return P.validate_config(raw, MODS, ports)


class SX:                     # a pad hit as the device sends it: a note-on, vel 0 = the release (a note-off)
    def __init__(s, pad, vel=100):
        s.type, s.note, s.velocity, s.channel = ("note_on" if vel else "note_off"), pad, vel, 9


class N:
    def __init__(s, t, note, vel=100, ch=9):
        s.type, s.note, s.velocity, s.channel = t, note, vel, ch


class C:
    def __init__(s, cc, val, ch=0):
        s.type, s.control, s.value, s.channel = "control_change", cc, val, ch


# ── validation ────────────────────────────────────────────────────────────────
c, e = conf_with({
    "note:0": {"do": "cc", "module": "poly", "cc": 74, "value": 100, "mode": "momentary"},
    "note:1": {"do": "cc", "port": "pisound", "ch": 10, "cc": 20, "value": "vel", "mode": "toggle"},
    "note:2": {"do": "note", "port": "rtp", "ch": 3, "note": 60, "vel": "same"},
    "note:3": {"do": "cc", "port": "nosuch", "ch": 1, "cc": 1},
    "note:4": {"do": "cc", "port": "pisound", "ch": 13, "cc": 1},
    "note:5": {"do": "cc", "module": "poly", "cc": 1, "mode": "sometimes"},
    "note:6": {"do": "note", "module": "drums", "note": 36, "vel": "same"}})
p = c["devices"]["Pads"]["pads"]
check("a NOTE pad may send a CC to a module (value + mode kept)",
      p.get("note:0") == {"do": "cc", "module": "poly", "cc": 74, "value": 100, "mode": "momentary"}, p.get("note:0"))
check("... or to an output port + channel, value = hit velocity",
      p.get("note:1") == {"do": "cc", "port": "pisound", "ch": 10, "cc": 20, "value": "vel", "mode": "toggle"},
      p.get("note:1"))
check("a note can go to a port + channel too", p.get("note:2") == {"do": "note", "port": "rtp", "ch": 3,
                                                                   "note": 60, "vel": "same"}, p.get("note:2"))
check("an unknown output port is refused + reported",
      "note:3" not in p and any("output port" in x for x in e), e)
check("channel 13 (the ORC's control channel) is refused on a port", "note:4" not in p)
check("an unknown CC behaviour is refused", "note:5" not in p)
check("an old module note entry is unchanged", p.get("note:6") == {"do": "note", "module": "drums", "note": 36,
                                                                   "vel": "same"})
c2, _ = conf_with({"note:0": {"do": "cc", "module": "poly", "cc": 7}})
check("a note-pad CC defaults to value 127, press", c2["devices"]["Pads"]["pads"]["note:0"] ==
      {"do": "cc", "module": "poly", "cc": 7, "value": 127, "mode": "press"})
c3, e3 = P.validate_config({"devices": {"Fire": {"port_match": "ORC FW", "surface": True, "pads": {
    "note:0": {"do": "cc", "module": "poly", "cc": 7, "mode": "momentary"}}}}}, MODS, PORTS)
check("an old Fire SURFACE profile is dropped and reported (its pads were pad numbers, not notes)",
      c3["devices"] == {} and any("surface" in x for x in e3), (c3, e3))
c4, _ = conf_with({"cc:4": {"do": "cc", "module": "poly", "cc": 74}})
check("a CC input sending a CC keeps its old shape (value passes through)",
      c4["devices"]["Pads"]["pads"]["cc:4"] == {"do": "cc", "module": "poly", "cc": 74})
c5, _ = conf_with({"note:2": {"do": "note", "port": "anything_ok", "ch": 3, "note": 60}}, ports=None)
check("ports=None checks only the name's shape", "note:2" in c5["devices"]["Pads"]["pads"])

# ── runtime ──────────────────────────────────────────────────────────────────
sent = []
pm = P.PadMap(emit=lambda *a: sent.append(a), module_ch=lambda m: {"poly": 6, "drums": 10}.get(m),
              run_action=lambda a: None, log=lambda s: None)
mono = [0.0]
pm.clock = lambda: mono[0]
pm.set_config(c)
pm.handle(SX(0, 90), PADS_IN)
check("momentary: the press sends the fixed value to the module's LIVE channel",
      sent == [("cc", 6, 74, 100)], sent)
del sent[:]
pm.handle(SX(0, 0), PADS_IN)
check("momentary: the release sends 0", sent == [("cc", 6, 74, 0)], sent)
del sent[:]
pm.handle(SX(0, 0), PADS_IN)
check("a release without a press sends nothing", sent == [])
pm.handle(SX(1, 77), PADS_IN)
check("toggle ON: the hit velocity to the OUTPUT PORT on its channel",
      sent == [("cc", 10, 20, 77, "pisound")], sent)
del sent[:]
mono[0] += 0.01
pm.handle(SX(1, 77), PADS_IN)
check("a double-trigger 10 ms later does not toggle straight back", sent == [], sent)
mono[0] += 1.0
pm.handle(SX(1, 50), PADS_IN)
check("the next real press toggles OFF (0)", sent == [("cc", 10, 20, 0, "pisound")], sent)
del sent[:]
pm.handle(SX(1, 0), PADS_IN)
check("a toggle's release sends nothing", sent == [])
pm.handle(SX(2, 99), PADS_IN)
check("a note pad to a port: note_on on that port + channel", sent == [("note_on", 3, 60, 99, "rtp")], sent)
del sent[:]
pm.handle(SX(2, 0), PADS_IN)
check("... and its release is the note_off there", sent == [("note_off", 3, 60, 0, "rtp")], sent)
del sent[:]
pm.handle(SX(2, 99), PADS_IN)
del sent[:]
pm.set_config(c)
check("a remap releases a sounding PORT note on its port", sent == [("note_off", 3, 60, 0, "rtp")], sent)
del sent[:]
pm.handle(SX(6, 99), PADS_IN)
check("a module note still goes to the internal bus (no port argument)", sent == [("note_on", 10, 36, 99)], sent)
del sent[:]
pmu = P.PadMap(emit=lambda *a: sent.append(a), module_ch=lambda m: None, run_action=lambda a: None,
               log=lambda s: None)
pmu.set_config(c)
pmu.handle(SX(0, 90), PADS_IN)
check("an UNBOUND module stays silent (never ch 1)", sent == [], sent)
cc_in, _ = P.validate_config({"devices": {"K": {"port_match": "Knobs", "pads": {
    "cc:4": {"do": "cc", "port": "pisound", "ch": 2, "cc": 30}}}}}, MODS, PORTS)
pmc = P.PadMap(emit=lambda *a: sent.append(a), module_ch=lambda m: 6, run_action=lambda a: None,
               log=lambda s: None)
pmc.set_config(cc_in)
pmc.handle(C(4, 88), "Knobs")
check("a CC input forwards its value to a PORT as another CC", sent == [("cc", 2, 30, 88, "pisound")], sent)
del sent[:]
plain, _ = P.validate_config({"devices": {"S": {"port_match": "Simmons", "pads": {
    "note:38": {"do": "cc", "module": "poly", "cc": 64, "value": "vel", "mode": "press"}}}}}, MODS, PORTS)
pms = P.PadMap(emit=lambda *a: sent.append(a), module_ch=lambda m: 6, run_action=lambda a: None,
               log=lambda s: None)
pms.set_config(plain)
pms.handle(N("note_on", 38, 64), "Simmons SD350")
pms.handle(N("note_off", 38, 0), "Simmons SD350")
check("a drum pad can send a CC on the hit (press mode); its timer note-off sends nothing",
      sent == [("cc", 6, 64, 64)], sent)

# ── the brain routes a port ──────────────────────────────────────────────────
src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_padmap_emit")
bus, out = [], []


class _M:
    def __init__(s, kind, **k):
        s.kind, s.k = kind, k


ns = {"mido": type("m", (), {"Message": _M}), "send_synth_midi": lambda m: bus.append(m),
      "send_to": lambda p, m: out.append((p, m))}
exec(compile(ast.Module([fn], []), "orc1.py", "exec"), ns)
ns["_padmap_emit"]("cc", 10, 20, 77, "pisound")
check("brain: a port CC goes out that port (channel 0-based), not the synth bus",
      not bus and out and out[0][0] == "pisound" and out[0][1].kind == "control_change"
      and out[0][1].k == {"channel": 9, "control": 20, "value": 77}, (bus, out))
ns["_padmap_emit"]("note_on", 10, 36, 99)
check("brain: a module note still goes to the synth bus", len(bus) == 1 and bus[0].kind == "note_on")
check("brain: the pad map is validated against the real output ports",
      "_orc1_padmap.validate_config(raw, _MM_KEYS, ports=set(cfg.PORTS))" in src)

# ── the web + the page ───────────────────────────────────────────────────────
web = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
check("web: GET /api/padmap lists the output ports and validates against them",
      '"ports": _pedal_ports(),' in web and web.count("ports=set(_pedal_ports())") >= 4)
js = open(os.path.join(HERE, "static", "js", "pads.js"), encoding="utf-8").read()
check("page: a NOTE pad can 'send a CC'", '["cc", "send a CC"], ["action", "action"], ["grab", "skipback grab"]' in js)
check("page: 'momentary' is offered on any device",
      '["momentary", "momentary: value, 0 on release"]' in js and "if (surface)" not in js)
check("page: To = ORC modules + Output ports, a port shows a Channel line",
      'el("optgroup", { label: "Output ports" })' in js and 'line("Channel"' in js)
check("page: every setting is a labelled line, label first (no trailing 'on' / 'note')",
      'line("Does", [doSel])' in js and 'el("span", { text: "on" })' not in js
      and 'el("span", { text: "note" })' not in js)

if __name__ == "__main__":
    check("every check ran (%d expected)" % EXPECTED, PASS + FAIL + 1 == EXPECTED,
          "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
