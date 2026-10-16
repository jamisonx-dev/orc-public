#!/usr/bin/env python3
"""BeatBuddy OUTPUT choice (2026-09-23): BBKit / 909 / any MIDI port.

orc1.py can't be imported (runs as __main__, opens hardware), so -- like the
performve / chord suites -- the real functions are AST-extracted and exec'd
against stubs. Pure stdlib + orc1_config. Safe while the rig runs.

Mutation-prove: make "909" pass notes untranslated -> the 909 checks fail;
drop the note-off scheduling / the flush -> the port note-off checks fail;
drop a table row -> the coverage check names the missing note.
"""
import ast, os, sys, json, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_config as cfg

SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def fsrc(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise SystemExit(f"{name} not found in orc1.py")


class Msg:
    def __init__(self, type, channel=0, note=0, velocity=0):
        self.type, self.channel, self.note, self.velocity = type, channel, note, velocity


class Mido:
    Message = Msg


SENT = []
TMP = tempfile.mkdtemp(prefix="bbout_")
NS = {"mido": Mido, "time": time, "json": json, "os": os,
      "send_to": lambda port, m: SENT.append((port, m.type, m.channel, m.note)),
      "print": lambda *a, **k: None,
      "_BB_OUT": {"dest": "bbkit", "port": None, "channel": 10},
      "_BB_OUT_DESTS": ("bbkit", "909", "port"), "_bb_pending_offs": []}
for f in ("_bb_out_targets", "_bb_flush_offs", "_bb_out_file", "_bb_out_set", "_bb_out_load"):
    exec(fsrc(f), NS)
NS["_bb_out_file"] = lambda: os.path.join(TMP, "bb_out.json")   # never the real file
T = NS["_bb_out_targets"]

_p = _f = 0


def check(cond, label, detail=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label}" + (f"  -- {detail}" if detail else ""))


TABLE = cfg.BB_TO_909
# 1. default BBKit: the BB note, untranslated, on the bbkit channel
check(T(53, 100, {"dest": "bbkit"}, 2, 10, TABLE) == [("synth", 2, 53, 100)],
      "BBKit: note passes untranslated to the kit channel")
# 2. 909: translated onto the DRUM channel
check(T(36, 90, {"dest": "909"}, 2, 10, TABLE) == [("synth", 10, 36, 90)]
      and T(53, 90, {"dest": "909"}, 2, 10, TABLE) == [("synth", 10, 53, 90)]
      and T(55, 90, {"dest": "909"}, 2, 10, TABLE) == [("synth", 10, 55, 90)]
      and T(48, 90, {"dest": "909"}, 2, 10, TABLE) == [("synth", 10, 48, 90)]
      and T(52, 90, {"dest": "909"}, 2, 10, TABLE) == [("synth", 10, 49, 90)],
      "909: kick, ride bell, splash, tom 2 hit their OWN voices; bow-crash folds to crash")
check(T(33, 90, {"dest": "909"}, 2, 10, TABLE) == [],
      "909: a note with no 909 counterpart (metronome click) is dropped")
# 3. coverage: every note the BeatBuddy kits on the card use (measured
#    2026-09-23 from all 10 kit.json files) translates, except the metronome.
KIT_NOTES = [1, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
             51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67,
             68, 69, 70, 71, 73, 74, 75, 76, 78, 79, 80, 81, 82, 86, 87, 88, 89,
             90, 91, 101, 102, 103, 104]
missing = [n for n in KIT_NOTES if n not in TABLE]
check(not missing, "909: every BeatBuddy kit note has a translation (fold, never drop)",
      f"missing {missing}")
VOICES = {36, 37, 38, 39, 41, 42, 46, 47, 48, 49, 50, 51, 53, 55, 56}
check(set(TABLE.values()) <= VOICES, "909: the table only targets real 909 voice notes",
      f"stray targets {sorted(set(TABLE.values()) - VOICES)}")
# 4. MIDI port: untranslated, on the chosen port + channel; no port -> nothing
check(T(53, 80, {"dest": "port", "port": "pisound", "channel": 10}, 2, 10, TABLE)
      == [("port", "pisound", 10, 53, 80)],
      "MIDI out: untranslated note on the chosen port and channel")
check(T(53, 80, {"dest": "port", "port": None, "channel": 10}, 2, 10, TABLE) == [],
      "MIDI out with no port chosen sends nothing (no crash)")
# 5. note-offs owed to external gear: sent when due, not before
NS["_bb_pending_offs"][:] = [(100.0, "pisound", 9, 53)]
NS["_bb_flush_offs"](now=99.9)
early = list(SENT)
NS["_bb_flush_offs"](now=100.1)
check(early == [] and SENT == [("pisound", "note_off", 9, 53)] and not NS["_bb_pending_offs"],
      "external note-off is sent at its deadline, not before", f"early={early} sent={SENT}")
# 6. choosing an output: validated, persisted, flushes owed note-offs
SENT.clear()
NS["_bb_pending_offs"][:] = [(1e12, "pisound", 9, 42)]
o = NS["_bb_out_set"]("909")
saved = json.load(open(os.path.join(TMP, "bb_out.json")))
check(o["dest"] == "909" and saved["dest"] == "909" and SENT and SENT[0][1] == "note_off",
      "set output: applied, persisted, and owed note-offs flushed first",
      f"o={o} saved={saved} sent={SENT}")
before = dict(NS["_BB_OUT"])
NS["_bb_out_set"]("bogus")
NS["_bb_out_set"]("port", None, 10)                 # port dest without a port
check(NS["_BB_OUT"] == before, "invalid output choices are ignored",
      f"{NS['_BB_OUT']} vs {before}")
NS["_bb_out_set"]("port", "pisound", 3)
NS["_BB_OUT"].update({"dest": "bbkit"})             # simulate a restart's defaults
NS["_bb_out_load"]()
check(NS["_BB_OUT"] == {"dest": "port", "port": "pisound", "channel": 3},
      "the choice survives a restart (load restores it)", str(NS["_BB_OUT"]))
# 7. wiring nets (source level)
emit = SRC[SRC.index("def _bb_emit("):SRC.index("beatbuddy = _bbmod.BeatBuddyMind")]
check("_bb_out_targets(" in emit and "send_to(port" in emit and "_bb_pending_offs.append" in emit,
      "_bb_emit routes every hit through _bb_out_targets (+ schedules port note-offs)")
check("_bb_flush_offs()" in SRC[SRC.index("def _bb_tick("):SRC.index("arp.subscribe_ticks(1, _bb_tick)")],
      "the BeatBuddy clock tick flushes due note-offs")
check('action == "BB_OUTPUT"' in SRC and '"output":     dict(_BB_OUT)' in SRC
      and "_bb_out_load()" in SRC,
      "BB_OUTPUT cmd, /state output field, and restore-at-start are wired")

import shutil
shutil.rmtree(TMP, ignore_errors=True)
EXPECTED = 14
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
sys.exit(1 if (_f or _p + _f != EXPECTED) else 0)
