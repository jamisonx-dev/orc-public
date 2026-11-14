#!/usr/bin/env python3
"""Regression suite for the SCENES engine (orc1_scenes.py) and its brain hooks.

    python3 -B _test_scenes.py        (PC or Pi; stdlib only)

Covers: import hygiene; the store (round trip, atomic write, damaged file, slot
range, lock file); capture (registry reads, LIVE brain values instead of the
lagging state file, unread reasons, refusals); recall timing for every mode
with a fake clock (ORC beats / bars) and with fake Fire bar messages; a newer
recall replacing a pending one; nothing playing = at once; the Fire pattern
sent one bar early when the Fire is the bar source; FireBarLink against a fake
mido (version 1 = no 0E and no 0D, version 2 = 0E 01 + 0F in + 0D out, a
reboot re-hellos); ch 13 CC 42 decode; pad / FCB "scene:N"; the real registry
through a real engine; and source checks that orc1.py's hooks call the engine
(orc1.py cannot be imported: its functions are lifted out with ast and run
against stubs, the way _test_chord_o3.py does it).

SAFETY: HOME / USERPROFILE point at a temp dir from import on, so nothing here
can write a real ~/.orc1_* file; the last check proves the real ones are
byte-identical.
"""
import ast
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.expanduser("~")


# Files this suite (or the code it drives) could write. Not a ~/.orc1_* glob:
# live services rewrite some of those all the time (skipback status, ...).
WATCH = (".orc1_scenes.json", ".orc1_scenes.json.lock", ".orc1_poly_params.json", ".orc1_fx_rack.json",
         ".orc1_padmap.json", ".orc1_pedal.json", ".orc1_bindings.json", ".orc1_bus_levels.json",
         ".orc1_bb_mix.json", ".orc1_visuals_state.json", ".cache/orc1/settings_registry.json")
# (~/orc1_state.json is NOT watched: the live brain rewrites it on every print_status.)


def _real_files():
    out = {}
    for p in [os.path.join(REAL_HOME, n) for n in WATCH]:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[p] = hashlib.md5(f.read()).hexdigest()
    return out


_BEFORE = _real_files()
SANDBOX = tempfile.mkdtemp(prefix="orc1_scenes_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX
sys.path.insert(0, HERE)

import orc1_scenes as S                       # noqa: E402

EXPECTED = 139
passed = failed = skipped = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def skip(name, why):
    global passed, skipped
    passed += 1
    skipped += 1
    print(f"  ok   {name}  [SKIPPED: {why}]")


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


# --- 0. import hygiene ------------------------------------------------------
code = ("import os, sys, builtins; opened = []\n"
        "_o = builtins.open\n"
        "def spy(*a, **k):\n    opened.append(a[0]); return _o(*a, **k)\n"
        "builtins.open = spy\n"
        f"sys.path.insert(0, {HERE!r})\n"
        "import orc1_scenes as M\n"
        "bad = [m for m in ('orc1_config', 'orc1_settings_registry', 'mido') if m in sys.modules]\n"
        "print(len(opened), bad, [v for k, v in vars(M).items() if not k.startswith('__') and isinstance(v, str) and os.path.isabs(v)])\n")
r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                   env=dict(os.environ, HOME=SANDBOX, USERPROFILE=SANDBOX))
check("import has no side effects (no file opened, no ORC module, no ~ expanded)",
      r.returncode == 0 and r.stdout.strip() == "0 [] []", (r.stdout + r.stderr)[-300:])


# --- fakes ------------------------------------------------------------------
class FakeReg:
    """id -> entry; read values come from self.vals; recall = one cc per id
    (fire.pattern -> the FIRE_PATTERN action, like the real registry)."""
    def __init__(self):
        self.vals = {"poly.cc74": 33, "drums.x": 9, "sampler.A.gain": None}
        self.ents = {i: {"id": i, "sceneable": True, "read": {"kind": "json"}} for i in self.vals}
        self.ents["tempo.bpm"] = {"id": "tempo.bpm", "sceneable": True, "read": {"kind": "state"}}
        self.ents["fire.pattern"] = {"id": "fire.pattern", "sceneable": True, "read": {"kind": "brain"}}
        self.ents["level.master"] = {"id": "level.master", "sceneable": False, "read": {"kind": "json"}}
        self.state_file = {"tempo.bpm": 100.0}       # the LAGGING ~/orc1_state.json

    def by_id(self):
        return self.ents

    def read_one(self, e, cache=None):
        i = e["id"]
        if i in self.state_file:
            return self.state_file[i], "state", ""
        v = self.vals.get(i)
        return v, ("file" if v is not None else None), ("" if v is not None else "no readable owner")

    def build_recall(self, values, bindings, ctx=None):
        msgs, sk = [], []
        for i, v in sorted(values.items()):
            if v is None:
                sk.append((i, "no value"))
            elif i == "fire.pattern":
                msgs.append({"type": "action", "cmd": {"action": "FIRE_PATTERN", "pattern": v}, "id": i})
            else:
                msgs.append({"type": "cc", "channel": 1, "cc": len(i), "value": v, "id": i})
        return msgs, sk


class FakeFire:
    def __init__(self, version=2):
        self.version, self.sent, self.pattern, self.bars = version, [], 0, True
        self.song, self.stops = False, 0

    def in_song_mode(self):
        return self.song

    def stopped(self):
        self.stops += 1
        self.pattern = self.song = None

    def bars_enabled(self):
        return self.bars and self.version >= 2

    def current_pattern(self):
        return self.pattern

    def send_pattern(self, n):
        if self.version < 2:
            return False, "Fire firmware version 1 has no queue-pattern command"
        self.sent.append(n)
        return True, ""


T = [1000.0]
SENT = []
ACTIONS = []


def make(fire=None, playing=True, reg=None, brain=None, store_name="scenes.json"):
    SENT.clear()
    ACTIONS.clear()
    st = S.SceneStore(os.path.join(SANDBOX, store_name))
    play = [playing]
    eng = S.SceneEngine(
        st, reg or FakeReg(),
        send_synth_cc=lambda ch, cc, v: SENT.append(("cc", ch, cc, v)),
        call_ctrl_cc=lambda cc, v: SENT.append(("ctrl_cc", cc, v)),
        call_ctrl_pc=lambda p: SENT.append(("ctrl_pc", p)),
        run_action=lambda c: ACTIONS.append(c),
        fire=fire, is_playing=lambda: play[0], brain_read=brain,
        clock=lambda: T[0], wall=lambda: 1.0, spawn=lambda fn: fn(), log=lambda s: None)
    return eng, st, play


def landed():
    return [x for x in SENT if x[0] == "cc"]


# --- 1. store -----------------------------------------------------------------
st = S.SceneStore(os.path.join(SANDBOX, "store1.json"))
st.update(5, lambda s: s.update(name="Verse", timing="BARS_2", values={"poly.cc74": 10}))
st.update(128, lambda s: s["values"].update({"a": 1}))
d = json.load(open(st.path))
check("store round trip: slot holds name, timing, values; file = {version, slots}",
      st.get(5) == {"name": "Verse", "timing": "BARS_2", "values": {"poly.cc74": 10}}
      and d["version"] == 1 and set(d["slots"]) == {"5", "128"})
leftovers = [p for p in os.listdir(SANDBOX) if p.endswith(".tmp")]
check("write is tmp + rename (no tmp file left behind)", not leftovers, leftovers)
lock_ok = (S._fcntl is None) or os.path.exists(st.path + ".lock")
check("writes take an flock on <file>.lock (Linux)", lock_ok)
st.update(5, lambda s: s.update(name="", timing=S.DEFAULT_TIMING, values={}))
check("a slot with nothing left is dropped (= empty)", st.get(5) is None and "5" not in st.load()["slots"])
bad = []
for slot in (0, 129, "x", None):
    try:
        st.update(slot, lambda s: None)
        bad.append(slot)
    except ValueError:
        pass
check("slot must be 1-128", not bad, bad)
with open(st.path, "w") as f:
    f.write("{ broken")
check("a damaged store reads as empty, and the next write repairs it",
      st.load() == {"version": 1, "slots": {}}
      and st.update(3, lambda s: s["values"].update(x=1)) is None and st.get(3)["values"] == {"x": 1})
with open(st.path, "w") as f:
    json.dump({"slots": {"7": {"timing": "WHENEVER", "values": {"a": 1}}, "999": {"values": {}}}}, f)
check("junk timing -> default, out-of-range slot dropped",
      st.get(7)["timing"] == "NEXT_BAR" and set(st.load()["slots"]) == {"7"})
check("clear empties a slot", st.clear(7) is True and st.get(7) is None and st.clear(7) is False)

# --- 2. capture ---------------------------------------------------------------------
eng, st, play = make(brain=lambda ids: {i: {"tempo.bpm": 123.4, "fire.pattern": 6}[i] for i in ids})
r = eng.capture(1, ["poly.cc74", "tempo.bpm", "fire.pattern", "sampler.A.gain"])
check("capture stores each ticked setting's current value",
      r["ok"] and st.get(1)["values"] == {"poly.cc74": 33, "tempo.bpm": 123.4, "fire.pattern": 6,
                                          "sampler.A.gain": None}, r)
check("capture reads brain-held settings LIVE (123.4), not the lagging state file (100.0)",
      st.get(1)["values"]["tempo.bpm"] == 123.4)
check("an unreadable setting is kept ticked with value None and a reason",
      "sampler.A.gain" in r["unread"] and st.get(1)["values"]["sampler.A.gain"] is None)
eng.capture(1, ["drums.x"])
check("capture REPLACES the slot's settings (only the ticked ones remain)",
      st.get(1)["values"] == {"drums.x": 9})
eng.capture(1, ["poly.cc74"], merge=True)
check("capture merge=True adds to them", st.get(1)["values"] == {"drums.x": 9, "poly.cc74": 33})
check("capture refuses unknown / not-sceneable ids",
      not eng.capture(1, ["nope"])["ok"] and not eng.capture(1, ["level.master"])["ok"]
      and st.get(1)["values"] == {"drums.x": 9, "poly.cc74": 33})
eng2, st2, _ = make(brain=None, store_name="nobrain.json")
eng2.capture(1, ["tempo.bpm"])
check("without a brain reader, capture falls back to the registry read (state file)",
      st2.get(1)["values"] == {"tempo.bpm": 100.0})
r = eng.set(2, name="Chorus", timing="BARS_4", values={"poly.cc74": 50, "sampler.A.gain": 90})
check("set: name, timing, values by hand (value None allowed)",
      r["ok"] and st.get(2) == {"name": "Chorus", "timing": "BARS_4",
                                "values": {"poly.cc74": 50, "sampler.A.gain": 90}})
check("set refuses a bad timing / unknown id",
      not eng.set(2, timing="SOON")["ok"] and not eng.set(2, values={"zz": 1})["ok"])
eng.set(2, remove=["sampler.A.gain"])
check("set remove= unticks a setting", st.get(2)["values"] == {"poly.cc74": 50})

# --- 3. recall timing (ORC clock) ---------------------------------------------------
eng, st, play = make()
eng.set(1, values={"poly.cc74": 11})
eng.set(2, values={"drums.x": 22})
eng.set(3, values={"poly.cc74": 33}, timing="BARS_2")

r = eng.recall(1, "IMMEDIATE")
check("IMMEDIATE lands at once while playing", r["when"] == "now" and landed() == [("cc", 1, 9, 11)], SENT)
SENT.clear()
play[0] = False
r = eng.recall(1, "NEXT_BAR")
check("nothing playing: NEXT_BAR lands at once", r["when"] == "now" and len(landed()) == 1)
SENT.clear()
r = eng.recall(1, "BARS_4")
check("nothing playing: BARS_4 lands at once too", r["when"] == "now" and len(landed()) == 1)
play[0] = True
SENT.clear()
r = eng.recall(1, "NEXT_BEAT")
check("NEXT_BEAT waits", r["when"] == "pending" and not SENT and eng.pending is not None)
eng.orc_beat(False)
check("NEXT_BEAT lands on the next beat (any beat)", len(landed()) == 1 and eng.pending is None)
eng.orc_beat(True)
check("a landed recall fires once only", len(landed()) == 1)
SENT.clear()
eng.recall(1)                                   # the scene's own timing: NEXT_BAR
check("no timing given = the scene's default (NEXT_BAR)", eng.pending["timing"] == "NEXT_BAR")
eng.orc_beat(False)
eng.orc_beat(False)
check("NEXT_BAR ignores beats 2-4", not SENT and eng.pending is not None)
eng.orc_beat(True)
check("NEXT_BAR lands on the next downbeat", len(landed()) == 1 and eng.pending is None)
SENT.clear()
eng.recall(3)
eng.orc_beat(True)
check("BARS_2 (scene default): not on the 1st bar", not SENT and eng.pending["bars"] == 1)
eng.orc_beat(True)
check("BARS_2 lands on the 2nd bar start", len(landed()) == 1)
SENT.clear()
eng.recall(1, "BARS_4")
for _ in range(3):
    eng.orc_beat(True)
check("BARS_4: not after 3 bars", not SENT)
eng.orc_beat(True)
check("BARS_4 lands on the 4th bar start", len(landed()) == 1)
SENT.clear()
eng.recall(1, "NEXT_BAR")
eng.recall(2, "NEXT_BAR")
eng.orc_beat(True)
check("a newer recall replaces the pending one (only scene 2 lands)",
      landed() == [("cc", 1, 7, 22)], SENT)
SENT.clear()
eng.recall(1, "BARS_2")
eng.orc_beat(True)
eng.recall(2, "BARS_2")
eng.orc_beat(True)
check("the replacement counts from ITS press (1 bar in: still pending)", not SENT and eng.pending["slot"] == 2)
eng.orc_beat(True)
check("... and lands on its own 2nd bar", landed() == [("cc", 1, 7, 22)])
SENT.clear()
eng.recall(1, "BARS_4")
eng.recall(2, "IMMEDIATE")
eng.orc_beat(True)
eng.orc_beat(True)
check("an IMMEDIATE recall cancels the pending one", landed() == [("cc", 1, 7, 22)] and eng.pending is None)
SENT.clear()
eng.recall(1, "BARS_4")
eng.poll()
check("poll while playing leaves a pending recall alone", not SENT and eng.pending is not None)
play[0] = False
eng.poll()
check("transport stopped while pending: poll lands it", len(landed()) == 1 and eng.pending is None
      and eng.last["why"] == "stopped")
play[0] = True
check("recall of an empty slot / bad slot / bad timing is refused",
      not eng.recall(99)["ok"] and not eng.recall(0)["ok"] and not eng.recall(1, "SOON")["ok"])
eng.recall(1, "NEXT_BAR")
eng.clear(1)
check("clearing the pending scene's slot cancels it", eng.pending is None)
check("status reports the last recall and the bar source",
      eng.status()["last"]["slot"] == 1 and eng.status()["bar_source"] == "orc")
check("CC 42 decode: value N -> scene N (1-127), 0 -> None (a button's release), clamped",
      S.slot_for_cc_value(0) is None and S.slot_for_cc_value(1) == 1 and S.slot_for_cc_value(5) == 5
      and S.slot_for_cc_value(127) == 127 and S.slot_for_cc_value(200) == 127)

# the burst dispatches every message kind through its own sender
eng, st, play = make()


class KindReg(FakeReg):
    def build_recall(self, values, bindings, ctx=None):
        return ([{"type": "cc", "channel": 4, "cc": 74, "value": 5, "id": "a"},
                 {"type": "ctrl_cc", "cc": 31, "value": 64, "id": "b"},
                 {"type": "ctrl_pc", "program": 63, "id": "c"},
                 {"type": "action", "cmd": {"action": "SET_BPM", "value": 99}, "id": "d"},
                 {"type": "action", "cmd": {"action": "RACK_PARAM", "idx": 0}, "id": "e",
                  "require": {"rack_plugin": [0, "x"]}}], [("f", "module unbound")])


eng.R = KindReg()
eng.require_ok = lambda req: False
st.update(9, lambda s: s["values"].update(a=1))
eng.recall(9, "IMMEDIATE")
check("burst: synth CC, ctrl CC, ctrl PC and actions each reach their sender; a failed "
      "require and registry skips are reported",
      SENT == [("cc", 4, 74, 5), ("ctrl_cc", 31, 64), ("ctrl_pc", 63)]
      and ACTIONS == [{"action": "SET_BPM", "value": 99}] and eng.last["sent"] == 4
      and {x[0] for x in eng.last["skipped"]} == {"e", "f"}, (SENT, ACTIONS, eng.last))
rp = os.path.join(SANDBOX, "rack.json")
with open(rp, "w") as f:
    json.dump({"slots": [{"plugin": "x"}]}, f)
check("_rack_require_ok: lands only when the slot still holds that plugin",
      S._rack_require_ok({"rack_plugin": [0, "x"]}, rp) and not S._rack_require_ok({"rack_plugin": [0, "y"]}, rp))

# --- 4. Fire bar messages as the bar source ---------------------------------------
ff = FakeFire(2)
eng, st, play = make(fire=ff)
eng.set(1, values={"poly.cc74": 11, "fire.pattern": 4})
eng.set(2, values={"poly.cc74": 12})
check("no Fire bar yet: bar source = the ORC clock", eng.bar_source() == "orc")
eng.fire_bar(0, 127, 0)
check("a 0F arrived: bar source = fire", eng.bar_source() == "fire" and eng.fire_bars_active())
play[0] = False
check("the Fire's bars alone count as playing (ORC stopped)", eng.playing())
play[0] = True
eng.recall(2, "NEXT_BAR")
eng.orc_beat(True)
check("Fire source: an ORC downbeat does NOT land a bar recall", not SENT)
T[0] += 2.0
eng.fire_bar(0, 127, 1)
check("Fire source: the Fire's 0F lands it", len(landed()) == 1)
SENT.clear()
eng.recall(1, "NEXT_BAR")
check("Fire pattern goes out ONE BAR EARLY (at the press for NEXT_BAR), the rest waits",
      ff.sent == [4] and not SENT)
T[0] += 2.0
eng.fire_bar(4, 127, 2)
check("... the rest lands on the Fire's bar, 0D not sent twice", len(landed()) == 1 and ff.sent == [4])
ff.sent.clear()
SENT.clear()
eng.recall(1, "BARS_2")
check("BARS_2: no 0D at the press", ff.sent == [])
T[0] += 2.0
eng.fire_bar(4, 127, 3)
check("BARS_2: 0D after the 1st Fire bar (one bar before landing)", ff.sent == [4] and not SENT)
T[0] += 2.0
eng.fire_bar(4, 127, 4)
check("BARS_2: lands on the 2nd Fire bar", len(landed()) == 1 and ff.sent == [4])
ff.sent.clear()
SENT.clear()
ff.pattern = 7
eng.recall(1, "NEXT_BAR")
eng.recall(2, "NEXT_BAR")
check("a replaced recall whose 0D went out is cancelled on the Fire (re-queue the pattern playing)",
      ff.sent == [4, 7], ff.sent)
ff.sent.clear()
SENT.clear()
eng.recall(1, "NEXT_BEAT")
eng.orc_beat(False)
check("NEXT_BEAT under the Fire: the next ORC beat, 0D in the burst", len(landed()) == 1 and ff.sent == [4])
ff.sent.clear()
SENT.clear()
T[0] += 60.0
check("the Fire's bars stopped coming: source back to the ORC clock", eng.bar_source() == "orc")
eng.recall(1, "NEXT_BAR")
check("ORC source: the Fire pattern is NOT sent early", ff.sent == [])
eng.orc_beat(True)
check("ORC source: 0D goes out with the burst on the ORC bar", ff.sent == [4] and len(landed()) == 1)
ff1 = FakeFire(1)
eng, st, play = make(fire=ff1)
eng.set(1, values={"poly.cc74": 11, "fire.pattern": 4})
eng.fire_bar(0, 127, 0)                         # (a v1 Fire never sends 0F; bars_enabled is False)
check("Fire v1: never the bar source", eng.bar_source() == "orc")
eng.recall(1, "IMMEDIATE")
check("Fire v1: fire.pattern skipped with the reason, the rest of the scene lands",
      len(landed()) == 1 and any(x[0] == "fire.pattern" and "version 1" in x[1] for x in eng.last["skipped"]),
      eng.last)
eng, st, play = make(fire=None)
eng.set(1, values={"fire.pattern": 4})
eng.recall(1, "IMMEDIATE")
check("no Fire at all: fire.pattern skipped", any(x[0] == "fire.pattern" for x in eng.last["skipped"]))


# --- 5. FireBarLink against a fake mido ---------------------------------------------
class FMsg:
    def __init__(self, type, data=()):
        self.type, self.data = type, list(data)


class FOut:
    def __init__(self, name, log):
        self.name, self.log, self.closed = name, log, False

    def send(self, m):
        self.log.append((self.name, list(m.data)))

    def close(self):
        self.closed = True


class FMido:
    Message = FMsg

    def __init__(self):
        self.outs, self.log, self.opened = [], [], []

    def get_output_names(self):
        return list(self.outs)

    def open_output(self, name):
        o = FOut(name, self.log)
        self.opened.append(o)
        return o


H = S.ORC_HDR
PORT = "ORC FW seq:ORC FW seq MIDI 1 24:0"
m = FMido()
ids = {"ORC FW seq": "1/5"}
bars = []
LT = [0.0]
link = S.FireBarLink(m, idents=lambda: ids, on_bar=lambda *a: bars.append(a), log=lambda s: None,
                     clock=lambda: LT[0])
link.poll()
check("no port: nothing opened", not m.opened)
m.outs = ["Some Synth:out 20:0", PORT]
link.poll()
check("port appears: hello sent (00), nothing else", m.log == [(PORT, H + [0x00])], m.log)
link.poll()
check("no hello repeat before the retry time", len(m.log) == 1)
LT[0] += 3.1
link.poll()
check("hello retried after 3 s without a reply", len(m.log) == 2)
check("a foreign source's ORC frame is not consumed",
      link.handle_sysex(H + [0x00, 2], "Other:port") is False and link.version is None)
check("hello reply version 1 consumed", link.handle_sysex(H + [0x00, 1], PORT) is True and link.version == 1)
m.log.clear()
for _ in range(5):
    LT[0] += 3.1
    link.poll()
check("version 1 (v0.29d): NO 0E bar-messages command, no more hellos; only the crash-record ask (06), once",
      m.log == [(PORT, H + [0x06])], m.log)
m.log.clear()
check("version 1: send_pattern refused, nothing sent",
      link.send_pattern(3)[0] is False and "version 1" in link.send_pattern(3)[1] and m.log == [])
check("version 1: bars_enabled False", link.bars_enabled() is False)
fm_flame = FMido(); fm_flame.outs = ["FLAME seq:FLAME seq MIDI 1 28:0"]
S.FireBarLink(fm_flame, idents=lambda: {}, log=lambda s: None, clock=lambda: 0.0).poll()
check("the renamed firmware ('FLAME seq', 2026-11-12) is linked with the default port names: hello sent",
      fm_flame.log == [("FLAME seq:FLAME seq MIDI 1 28:0", H + [0x00])], fm_flame.log)
ids["ORC FW seq"] = "1/6"                       # flashed to v0.30: new USB address
link.poll()
check("reboot (new USB address): reopened, hello again",
      m.opened[-1].name == PORT and m.log == [(PORT, H + [0x00])] and m.opened[0].closed, m.log)
link.handle_sysex(H + [0x00, 2], PORT)
link.poll()
check("version 2: 0E 01 (bar messages on) sent once", m.log[-1] == (PORT, H + [0x0E, 1]) and link.bars_enabled())
link.poll()
check("0E not repeated", sum(1 for x in m.log if x[1][4] == 0x0E) == 1)
check("0F -> on_bar(pattern, song slot, bar), pattern remembered",
      link.handle_sysex(H + [0x0F, 5, 127, 9], PORT) and bars == [(5, 127, 9)] and link.current_pattern() == 5)
check("version 2: send_pattern sends 0D n", link.send_pattern(12) == (True, "") and m.log[-1] == (PORT, H + [0x0D, 12]))
check("send_pattern range-checks 0-31", link.send_pattern(32)[0] is False and m.log[-1][1][-1] == 12)
link.handle_sysex(H + [0x0D, 1, 6, 12, 0], PORT)
check("0D reply updates the current pattern", link.current_pattern() == 6)
m.outs = []
link.poll()
check("port gone: closed, version forgotten, bars off",
      m.opened[-1].closed and link.version is None and not link.bars_enabled())

# --- 5b. crash records over the same link (folded in from orc1-firevu, 2026-11-12) --------
def crash_reply(valid=1, total=3, pc=0x11, up=0x22, t4count=0, rv=3):
    d = H + [0x06, rv, valid, 8, 2, 1, total]                  # version, valid, cause, stage, count, total
    words = [0] * {1: 7, 2: 10, 3: 16}[rv]
    words[0], words[6] = pc, up
    if rv == 3:
        words[10] = t4count
    for w in words:
        d += [(w >> (7 * i)) & 0x7F for i in range(5)]
    return d


check("crash reply lengths match host_vu.py's table (46 / 61 / 91)",
      [len(crash_reply(rv=v)) for v in (1, 2, 3)] == [46, 61, 91])
cm, cids, crashes = FMido(), {"ORC FW seq": "1/5"}, []
CT = [0.0]
cl = S.FireBarLink(cm, idents=lambda: cids, log=lambda s: None, clock=lambda: CT[0],
                   on_crash=lambda d: crashes.append(list(d)))
cm.outs = [PORT]
cl.poll()
check("crash record: not asked before the hello reply", [x[1][4] for x in cm.log] == [0x00], cm.log)
cl.handle_sysex(H + [0x00, 9], PORT)
cl.poll()
cl.poll()
check("crash record: 06 asked once after the hello reply, before 0E",
      [x[1][4] for x in cm.log] == [0x00, 0x06, 0x0E], cm.log)
check("valid record: consumed, on_crash called once with it",
      cl.handle_sysex(crash_reply(), PORT) is True and crashes == [crash_reply()])
cl.handle_sysex(crash_reply(), PORT)
cl.handle_sysex(crash_reply(t4count=5), PORT)
check("the same crash again (or only the misread counter moved): not handed on again", len(crashes) == 1)
cl.handle_sysex(crash_reply(valid=0, total=4), PORT)
check("an empty record (valid 0): consumed, not handed on", len(crashes) == 1)
cl.handle_sysex(crash_reply()[:-1], PORT)
check("a reply of the wrong length is ignored", len(crashes) == 1)
cl.handle_sysex(crash_reply(total=4, pc=0x33), PORT)
check("a new crash (other total / PC): handed on", len(crashes) == 2 and crashes[-1] == crash_reply(total=4, pc=0x33))
cl.handle_sysex(crash_reply(total=5), "Other:port")
check("a crash reply from a foreign port is not ours", len(crashes) == 2)
cids["ORC FW seq"] = "1/6"                       # the Fire rebooted (a crash restart): new USB address
cm.log.clear()
cl.poll()
cl.handle_sysex(H + [0x00, 9], PORT)
cl.poll()
check("after a reboot: 06 asked again on the new connection", (PORT, H + [0x06]) in cm.log, cm.log)
cl.handle_sysex(crash_reply(total=4, pc=0x33), PORT)
check("the record seen before the reboot is not handed on twice", len(crashes) == 2)
bad = S.FireBarLink(cm, idents=lambda: cids, log=lambda s: None, on_crash=lambda d: 1 / 0)
check("an on_crash that raises does not break the link",
      bad.handle_sysex(crash_reply(total=9), PORT) is True)
_b = src("orc1.py")
_save = _b[_b.find("def _fire_crash_save("):_b.find("fire_bar_link = _orc1_scenes.FireBarLink(")]
check("orc1.py: the link's on_crash = _fire_crash_save, which runs host_crash.py --save (never --clear)",
      "on_crash=_fire_crash_save)" in _b and '"--save"' in _save and "--clear" not in _save
      and "host_crash.py" in _save and "start_new_session=True" in _save)

# --- 6. real registry through a real engine -----------------------------------------
import orc1_settings_registry as R            # noqa: E402
import orc1_config as C                       # noqa: E402
ff = FakeFire(2)
SENT.clear()
ACTIONS.clear()
st = S.SceneStore(os.path.join(SANDBOX, "real.json"))
eng = S.SceneEngine(st, R, send_synth_cc=lambda ch, cc, v: SENT.append(("cc", ch, cc, v)),
                    call_ctrl_cc=lambda cc, v: SENT.append(("ctrl_cc", cc, v)),
                    call_ctrl_pc=lambda p: SENT.append(("ctrl_pc", p)),
                    run_action=lambda c: ACTIONS.append(c), fire=ff,
                    bindings=lambda: {"poly": 3}, recall_ctx=lambda: {"clock_mode": "internal"},
                    is_playing=lambda: False, clock=lambda: T[0], spawn=lambda fn: fn(), log=lambda s: None)
r = eng.set(4, values={"poly.cc74": 90, "tempo.bpm": 101.5, "fire.pattern": 2, "level.drum": 70})
eng.recall(4)
lvl = {k: cc for cc, _l, k in __import__("cc_surfaces").bus_levels(C)}
check("real registry: poly CC on its bound channel, drum bus on ch 13, SET_BPM, Fire 0D",
      r["ok"] and ("cc", 3, 74, 90) in SENT and ("cc", C.CH_CTRL, lvl["DRUM_CC"], 70) in SENT
      and {"action": "SET_BPM", "value": 101.5} in ACTIONS and ff.sent == [2]
      and not any(a.get("action") == "FIRE_PATTERN" for a in ACTIONS), (SENT, ACTIONS, ff.sent))
check("real registry: fire.pattern is a sceneable registry setting set by FIRE_PATTERN",
      R.by_id()["fire.pattern"]["sceneable"] and R.by_id()["fire.pattern"]["set"]["action"] == S.FIRE_PATTERN_ACTION)

# --- 7. config + pad / FCB action ----------------------------------------------------
check("cfg.SCENE_RECALL_CC = 42, free in FUNC / CTRL / ARP maps",
      C.SCENE_RECALL_CC == 42 and all(42 not in d for d in (C.FUNC_CC_MAP, C.CTRL_CC_MAP, C.ARP_CC_MAP)))
import orc1_pedal as P                        # noqa: E402
import orc1_padmap as PM                      # noqa: E402
check("pad / FCB action scene:N valid for 1-128 only",
      P.valid_switch_action("scene:1") and P.valid_switch_action("scene:128")
      and not P.valid_switch_action("scene:0") and not P.valid_switch_action("scene:129")
      and PM.valid_action("scene:7"))
check("/pads offers Recall scene 1-128",
      [v for v, _ in PM.ACTION_CHOICES if v.startswith("scene:")] == [f"scene:{n}" for n in range(1, 129)])

# --- 8. orc1.py hooks (lifted out with ast, run against stubs) ----------------------
BRAIN = src("orc1.py")
TREE = ast.parse(BRAIN)


def fn(name):
    node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(BRAIN, node)


def lift(name, ns):
    exec(fn(name), ns)
    return ns[name]


calls = []
ns = {"cfg": C, "state": {"func_held": False}, "_orc1_scenes": S,
      "_scene_recall": lambda slot, timing=None, source="": calls.append(("recall", slot, source)),
      "handle_func_cc": lambda *a: calls.append(("func",) + a),
      "handle_arp_cc": lambda *a: calls.append(("arp",) + a),
      "_do_lead_voice": lambda *a: None, "decode_relative_cc": lambda v: v, "print_status": lambda *a: None}
h = lift("handle_ctrl_cc", ns)
h(C.SCENE_RECALL_CC, 5)
h(C.SCENE_RECALL_CC, 0)
h(30, 127)
check("brain: ch 13 CC 42 value 5 -> recall scene 5, value 0 ignored; other CCs untouched",
      calls == [("recall", 5, "ch13 cc"), ("arp", "ARP_TOGGLE", 127)], calls)
calls.clear()
ns["state"]["func_held"] = True
h(C.SCENE_RECALL_CC, 1)
check("brain: CC 42 recalls even with the function key held", calls == [("recall", 1, "ch13 cc")], calls)

calls.clear()
ns2 = {"_scene_recall": lambda slot, timing=None, source="": calls.append(("recall", slot)),
       "_execute_cmd": lambda c: calls.append(("cmd", c)), "_pedal_chord_off": lambda: None,
       "_pedal_chord": lambda *a: None, "_pedal_rc505": lambda *a: None, "print": lambda *a, **k: None}
psa = lift("_pedal_switch_action", ns2)
psa("scene:12", True, False)
psa("scene:12", False, False)
check("brain: pad / FCB action scene:12 recalls scene 12 on the press only", calls == [("recall", 12)], calls)

calls.clear()
ns3 = {"_scene_cmd": lambda c: {"ok": True, "via": c["action"]}, "state": {"key_lock": False},
       "print_status": lambda *a: None}
ex = lift("_execute_cmd", ns3)
acts = ["SCENE_CAPTURE", "SCENE_RECALL", "SCENE_SET", "SCENE_CLEAR", "SCENE_LIST", "SETTINGS_READ", "FIRE_PATTERN"]
check("brain: _execute_cmd hands every scene action to _scene_cmd and returns its reply",
      all(ex({"action": a}) == {"ok": True, "via": a} for a in acts))
check("brain: other actions still run their own branch (reply None)",
      ex({"action": "TOGGLE_KEY_LOCK"}) is None and ns3["state"]["key_lock"] is True)


class FScenes:
    def __init__(self):
        self.calls = []

    def list(self):
        return {"ok": True, "slots": {}, "status": {}}

    def capture(self, slot, ids, merge=False):
        self.calls.append(("capture", slot, ids, merge))
        return {"ok": True}

    def set(self, slot, **k):
        self.calls.append(("set", slot, k))
        return {"ok": True}

    def clear(self, slot):
        self.calls.append(("clear", slot))
        return {"ok": True}


fs, sends = FScenes(), []
ns4 = {"scenes": fs, "_settings_live": lambda ids: {i: 1 for i in ids},
       "_scene_recall": lambda slot, timing=None, source="": {"ok": True, "when": "pending", "slot": slot,
                                                               "timing": timing, "src": source},
       "fire_bar_link": type("L", (), {"send_pattern": lambda self, n: sends.append(n) or (n < 32, "x"),
                                       "in_song_mode": lambda self: False})(),
       "print_status": lambda *a: None}
sc = lift("_scene_cmd", ns4)
r1 = sc({"action": "SCENE_RECALL", "slot": 3, "timing": "BARS_2"})
r2 = sc({"action": "SETTINGS_READ", "ids": ["tempo.bpm"]})
r3 = sc({"action": "SCENE_CAPTURE", "slot": 2, "ids": ["a"]})
r4 = sc({"action": "FIRE_PATTERN", "pattern": 40})
r5 = sc({"action": "SCENE_SET", "slot": 2, "name": "x"})
check("brain: _scene_cmd shapes (recall slot+timing from the web, SETTINGS_READ values, capture ids, "
      "FIRE_PATTERN error, SET fields)",
      r1 == {"ok": True, "when": "pending", "slot": 3, "timing": "BARS_2", "src": "web"}
      and r2 == {"ok": True, "values": {"tempo.bpm": 1}} and r3 == {"ok": True}
      and r4["ok"] is False and sends == [40]
      and fs.calls[0] == ("capture", 2, ["a"], False) and fs.calls[1][0] == "set"
      and fs.calls[1][2]["name"] == "x", (r1, r2, r4, fs.calls))


class FArp:
    def __init__(self):
        self._pulse_total = 0


beats = []
ns5 = {"seq": None, "arp": FArp(), "PPQN": 24, "_SMP_BEATS_BAR": 4, "_scene_last_step": [None],
       "beatbuddy": None, "_scene_last_bb": [None],
       "scenes": type("E", (), {"orc_beat": lambda self, d: beats.append((ns5["arp"]._pulse_total, d))})()}
pulse = lift("_scene_pulse", ns5)
for t in range(1, 98):
    ns5["arp"]._pulse_total = t
    pulse()
check("brain: _scene_pulse on the arp grid = a beat every 24 pulses, bar 1 every 96 (= beat_phase math)",
      beats == [(1, True), (25, False), (49, False), (73, False), (97, True)], beats)
beats.clear()


class FSeq:
    running, _global_step = True, -1


ns5["seq"] = FSeq()
got = []
ns5["scenes"] = type("E", (), {"orc_beat": lambda self, d: got.append((ns5["seq"]._global_step, d))})()
for gs in range(0, 18):
    ns5["seq"]._global_step = gs
    pulse()
    pulse()                                     # several pulses per step: one event per step only
check("brain: _scene_pulse on the running seq = a beat every 4 steps, bar 1 every 16 (step 0 = PLAY)",
      got == [(0, True), (4, False), (8, False), (12, False), (16, True)], got)


class FA:
    enabled, pattern_name, subdivision, clock_mode = True, "UP", "1/16", "internal"
    swing_amount, humanize, gate_trim, internal_bpm = 0.25, 0.0, 1.0, 133.0

    def _estimated_bpm(self):
        return 0


with open(os.path.join(SANDBOX, "orc1_state.json"), "w") as f:     # the LAGGING file
    json.dump({"arp": {"bpm": 90.0, "rate": "1/8"}, "root": 0}, f)
with open(os.path.join(SANDBOX, ".orc1_poly_params.json"), "w") as f:
    json.dump({"74": 55}, f)
SONG = [False]
ns6 = {"_orc1_settings": R, "state": {"root": 5, "scale": "MAJ", "quality_mode": "DIAT", "preset": "X",
                                      "voicing": "OPEN", "vl_strength": 0.5, "key_lock": True},
       "scale_mode_now": lambda: "off", "arp": FA(), "beatbuddy": None, "_bb_follow_clock": False,
       "fire_bar_link": type("L", (), {"current_pattern": lambda self: 9,
                                       "in_song_mode": lambda self: SONG[0]})()}
live = lift("_settings_live", ns6)(["tempo.bpm", "arp.rate", "harmony.root", "fire.pattern", "poly.cc74"])
check("brain: SETTINGS_READ reads tempo / arp / harmony LIVE (not the stale state file), the Fire "
      "pattern from the link, the rest from its owner's file",
      live == {"tempo.bpm": 133.0, "arp.rate": "1/16", "harmony.root": 5, "fire.pattern": 9,
               "poly.cc74": 55}, live)


def body_has(name, *needles):
    b = fn(name)
    return all(x in b for x in needles)


check("brain: _route_message feeds Fire SysEx to fire_bar_link BEFORE dropping device chatter",
      body_has("_route_message", "fire_bar_link.handle_sysex(msg.data, source_name)")
      and fn("_route_message").index("fire_bar_link.handle_sysex")
      < fn("_route_message").index("_sysex_is_device_chatter(msg.data)"))
mb = next(ast.get_source_segment(BRAIN, n) for n in TREE.body if isinstance(n, ast.FunctionDef)
          and "seq.bar_cb       = _smp_seq_anchor.mark" in (ast.get_source_segment(BRAIN, n) or ""))
check("brain: the clock feed is subscribed after the sequencer's and the BeatBuddy's subscribers, "
      "and the scenes loop is started",
      "arp.subscribe_ticks(1, _scene_pulse)" in mb
      and mb.index("seq.bar_cb       = _smp_seq_anchor.mark") < mb.index("arp.subscribe_ticks(1, _bb_tick)")
      < mb.index("arp.subscribe_ticks(1, _scene_pulse)")
      and "threading.Thread(target=_scene_loop" in mb)
check("brain: the engine is built on the registry with the brain's own senders and the Fire link",
      all(x in BRAIN for x in ("_orc1_scenes.SceneEngine(", "_orc1_scenes.SceneStore()", "fire=fire_bar_link",
                               "call_ctrl_cc=lambda cc, v: handle_ctrl_cc(cc, v)",
                               "run_action=lambda c: _execute_cmd(c)", "brain_read=_settings_live"))
      and body_has("write_state_json", '"scenes":         _scene_status()'))

# the command socket really replies with the action's data (a Unix socket: Linux only)
if not hasattr(socket, "AF_UNIX"):
    skip("brain: command socket replies {\"ok\":...,data} for a dict, {\"ok\":true} otherwise", "no AF_UNIX here")
else:
    sock = os.path.join(SANDBOX, "cmd.sock")
    ns7 = {"os": os, "time": time, "json": json, "_CMD_SOCK_PATH": sock, "print": lambda *a, **k: None,
           "_execute_cmd": lambda c: ({"ok": True, "slots": {"1": {"name": "V"}}} if c["action"] == "SCENE_LIST"
                                      else ({"ok": False, "error": "empty"} if c["action"] == "SCENE_RECALL"
                                            else None))}
    threading.Thread(target=lift("_cmd_socket_loop", ns7), daemon=True).start()
    replies = []
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    for a in ("SCENE_LIST", "SCENE_RECALL", "PANIC"):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2)
        c.connect(sock)
        c.sendall((json.dumps({"action": a}) + "\n").encode())
        c.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            ch = c.recv(4096)
            if not ch:
                break
            buf += ch
        c.close()
        replies.append(buf)
    check("brain: command socket replies {\"ok\":...,data} for a dict, {\"ok\":true} otherwise "
          "(ok first, no spaces: the web's 64-byte '\"ok\":true' check still works)",
          replies[0].startswith(b'{"ok":true,') and json.loads(replies[0])["slots"] == {"1": {"name": "V"}}
          and replies[1].startswith(b'{"ok":false') and replies[2] == b'{"ok":true}\n', replies)

# --- 8b. review fixes (2026-10-26) ----------------------------------------------------
# (1) FX rack on/off is never sceneable (RACK_ENABLE rebuilds every jalv)
by_real = R.by_id()
rk = [i for i in by_real if i.startswith("fx.rack.") and i.endswith(".enabled")]
check("review 1: fx.rack.*.enabled is NOT sceneable (apply() respawns every jalv), with the reason",
      rk and all(not by_real[i]["sceneable"] and "apply()" in by_real[i]["why"] for i in rk)
      and R.build_recall({rk[0]: True}, {})[0] == [], rk)


# (1b) a recall sends only changes, judged where the current value is exact
class UReg(FakeReg):
    def __init__(self):
        super().__init__()
        self.ents = {"tempo.bpm": {"id": "tempo.bpm", "sceneable": True, "read": {"kind": "state"},
                                   "set": {"kind": "action"}},
                     "poly.cc74": {"id": "poly.cc74", "sceneable": True, "read": {"kind": "json"},
                                   "set": {"kind": "cc"}},
                     "bb.song": {"id": "bb.song", "sceneable": True, "read": {"kind": "json"},
                                 "set": {"kind": "action"}}}
        self.vals = {"poly.cc74": 33, "bb.song": "/a"}
        self.state_file = {}


eng, st, play = make(reg=UReg(), brain=lambda ids: {"tempo.bpm": 120.0})
eng.set(1, values={"tempo.bpm": 120.0, "poly.cc74": 33, "bb.song": "/b"})
eng.recall(1, "IMMEDIATE")
check("review 1: unchanged brain value skipped (tempo 120 = 120); a CC is still sent even when its "
      "lagging host file agrees; a changed action setting is sent",
      sorted(m[2] for m in SENT) == [len("bb.song"), len("poly.cc74")]
      and eng.last["unchanged"] == ["tempo.bpm"], (SENT, eng.last))
eng.set(1, values={"bb.song": "/a"})
eng.recall(1, "IMMEDIATE")
check("review 1: an action setting read from its file and equal is skipped (bb.song /a)",
      eng.last["sent"] == 0 and eng.last["unchanged"] == ["bb.song"], eng.last)

# (2) the registry is never built on a brain thread; the brain loads a JSON cache
built = list(R._CACHE)
orig_build = R._build
R._CACHE = None
R.set_build_allowed(False)
try:
    R.registry()
    nr = False
except R.NotReady:
    nr = True
eng = S.SceneEngine(S.SceneStore(os.path.join(SANDBOX, "nr.json")), R, lambda *a: None, lambda *a: None,
                    lambda *a: None, lambda *a: None, spawn=lambda f: f(), log=lambda s: None)
eng.store.update(1, lambda s: s["values"].update({"poly.cc74": 1}))
check("review 2: build switched off + no cache -> NotReady; scene calls answer 'settings list loading'",
      nr and eng.recall(1)["error"] == S.NOT_READY and eng.capture(1, ["poly.cc74"])["error"] == S.NOT_READY
      and R._CACHE is None)
r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "orc1_settings_registry.py"), "--build-cache"],
                   capture_output=True, text=True, env=dict(os.environ, HOME=SANDBOX, USERPROFILE=SANDBOX))
cache_path = os.path.join(SANDBOX, ".cache", "orc1", "settings_registry.json")


def _boom():
    raise AssertionError("built in-process")


R._build = _boom
t0 = time.perf_counter()
ok = R.load_cache()
load_ms = (time.perf_counter() - t0) * 1000
print(f"  (registry cache load: {load_ms:.1f} ms)")
check("review 2: a separate process writes the cache; load_cache loads it without building; same entries",
      r.returncode == 0 and os.path.isfile(cache_path) and ok and R.ready()
      and R._CACHE == built and len(R.registry()) >= len(built), (r.stdout + r.stderr)[-300:])
with open(cache_path) as f:
    cdata = json.load(f)
cdata["sig"][0][1] += 1                          # a source "changed" since the cache was built
with open(cache_path, "w") as f:
    json.dump(cdata, f)
R._CACHE = None
check("review 2: a cache whose source signature no longer matches is NOT loaded",
      R.load_cache() is False and R._CACHE is None)
R.set_build_allowed(True)
nbuild = [0]


def _slow():
    nbuild[0] += 1
    time.sleep(0.2)
    return list(built)


R._build = _slow
ths = [threading.Thread(target=R.registry) for _ in range(3)]
for th in ths:
    th.start()
for th in ths:
    th.join()
check("review 2: the in-process cache is locked (3 threads at once build it ONCE)", nbuild[0] == 1, nbuild)
R._build = orig_build
R._CACHE, R._CACHE_SIG = built, R.source_signature()
check("review 2: the brain switches building off, builds the cache in a separate niced process and "
      "never calls registry() on its own threads",
      "_orc1_settings.set_build_allowed(False)" in BRAIN and "--build-cache" in fn("_scene_registry_cache")
      and '"nice"' in fn("_scene_registry_cache") and "_orc1_settings.registry()" not in BRAIN
      and "except _orc1_settings.NotReady" in fn("_scene_cmd"))

# (3) + (4) the Fire queue and song mode
ff = FakeFire(2)
ff.pattern = 7
eng, st, play = make(fire=ff)
eng.set(1, values={"poly.cc74": 11, "fire.pattern": 4})
eng.set(2, values={"poly.cc74": 12, "fire.pattern": 5}, timing="BARS_4")
eng.fire_bar(7, 127, 0)
eng.recall(1, "NEXT_BAR")                       # 0D 4 goes out early
eng.recall(2)                                   # BARS_4: its own 0D only 3 bars later
check("review 3: replaced by a recall that won't send its 0D now -> queue cleared at once (re-send 7)",
      ff.sent == [4, 7], ff.sent)
ff.sent.clear()
ff.song = True
SENT.clear()
eng.recall(1, "IMMEDIATE")
check("review 4: song mode -> fire.pattern skipped ('Fire in song mode'), no clearing 0D, rest lands",
      ff.sent == [] and len(landed()) == 1
      and any(x[0] == "fire.pattern" and x[1] == "Fire in song mode" for x in eng.last["skipped"]), eng.last)
ff.song = None
eng.recall(1, "NEXT_BAR")
ff.sent.clear()
eng.recall(2)
check("review 4: song mode UNKNOWN -> never a clearing 0D", ff.sent == [], ff.sent)
link2 = S.FireBarLink(FMido(), on_bar=lambda *a: None, log=lambda s: None)
link2.handle_sysex(H + [0x0F, 3, 2, 0], PORT)
a = link2.in_song_mode()
link2.handle_sysex(H + [0x0F, 3, 127, 1], PORT)
b = link2.in_song_mode()
link2.handle_sysex(H + [0x0D, 1, 6, 127, 1], PORT)
c = link2.in_song_mode()
link2.stopped()
check("review 4: song mode from 0F (slot != 127) and the 0D reply's flag; stopped() forgets pattern + song",
      a is True and b is False and c is True and link2.in_song_mode() is None and link2.current_pattern() is None)
SONG[0] = True
l1 = lift("_settings_live", ns6)(["fire.pattern"])
SONG[0] = None
l2 = lift("_settings_live", ns6)(["fire.pattern"])
SONG[0] = False
check("review 4: fire.pattern captured only in pattern mode", l1 == {"fire.pattern": None} == l2)

# (7) the Fire's bars: a shorter grace, a STOP drops it, stopping forgets its state
ff = FakeFire(2)
eng, st, play = make(fire=ff)
eng.fire_bar(0, 127, 0)
T[0] += 2.0
eng.fire_bar(0, 127, 1)
eng.poll()
T[0] += 2.4
a = eng.fire_bars_active()
T[0] += 0.2
b = eng.fire_bars_active()
eng.poll()
check("review 7: Fire source lingers ~1.25 bar lengths (2.4 s of a 2 s bar yes, 2.6 s no); "
      "poll then tells the link it stopped", a and not b and ff.stops == 1, (a, b, ff.stops))
eng.fire_bar(0, 127, 2)
eng.fire_stop()
check("review 7: a STOP from the Fire drops it as the bar source at once", not eng.fire_bars_active())
check("review 7: the brain sends a STOP from the Fire port to the link and the engine",
      body_has("_route_message", "fire_bar_link.is_ours(source_name)", "fire_bar_link.stopped()",
               "scenes.fire_stop()"))

# (5) the burst: paced, failures counted, state written once
sleeps, guard, seen = [], [False], []


class BigReg(FakeReg):
    def build_recall(self, values, bindings, ctx=None):
        return [{"type": "cc", "channel": 1, "cc": k, "value": 1, "id": f"x{k}"} for k in range(70)], []


@__import__("contextlib").contextmanager
def g():
    guard[0] = True
    yield
    guard[0] = False


def snd(ch, cc, v):
    seen.append(guard[0])
    if cc == 5:
        return False
    if cc == 6:
        raise OSError("bus gone")


eng = S.SceneEngine(S.SceneStore(os.path.join(SANDBOX, "big.json")), BigReg(), snd, lambda *a: None,
                    lambda *a: None, lambda *a: None, spawn=lambda f: f(), log=lambda s: None,
                    sleep=lambda s: sleeps.append(s), burst_guard=g)
eng.store.update(1, lambda s: s["values"].update({"poly.cc74": 1}))
eng.recall(1, "IMMEDIATE")
check("review 5: 70 messages go in chunks of 32 with a short sleep between (2 sleeps of 2 ms)",
      sleeps == [S.PACE_S, S.PACE_S] and S.CHUNK == 32 and S.PACE_S <= 0.003, sleeps)
check("review 5: a send that returns False or raises counts as FAILED (68 sent, 2 failed)",
      eng.last["sent"] == 68 and eng.last["failed"] == 2 and len(eng.last["errors"]) == 2, eng.last)
check("review 5: every send runs inside the burst guard, which is released after",
      seen and all(seen) and guard[0] is False)
qns = {"_status_quiet": threading.local(), "write_state_json": lambda: calls.append("write"),
       "_print_status_body": lambda l: calls.append("body"), "contextlib": __import__("contextlib"),
       "print": lambda *a, **k: None}
ps = lift("print_status", qns)
quiet = qns["contextlib"].contextmanager(lift("_scene_quiet", qns))   # ast drops the decorator
calls.clear()
with quiet():
    ps("x")
ps("y")
check("review 5: inside the burst guard print_status neither logs nor writes the state (once after)",
      calls == ["body", "write"] and "@contextlib.contextmanager\ndef _scene_quiet" in BRAIN
      and "burst_guard=_scene_quiet" in BRAIN, calls)
bns = {"mido": type("M", (), {"Message": lambda *a, **k: type("Msg", (), {"copy": lambda s, **k: s})()}),
       "_synth_bus": None, "state": {}, "send_to": lambda *a: None}
try:
    lift("_scene_send_cc", bns)(4, 74, 1)
    raised = False
except RuntimeError:
    raised = True
check("review 5: the brain's scene CC sender RAISES when the synth bus is not open", raised)

# (6) playing: stale pulses, transport beats pulses, the BeatBuddy's own bar
check("review 6a: clock pulses count only if one came within 2 beats at the last tempo",
      S.clock_running(100.0, 120, 100.9) and not S.clock_running(100.0, 120, 101.1)
      and not S.clock_running(None, 120, 1.0))
check("review 6b: an outside clock ticking after a STOP is stopped; after START/CONTINUE or with no "
      "transport seen it plays; the seq / BeatBuddy / held arp always play",
      not S.playing_from(False, False, False, True, True, "stop")
      and S.playing_from(False, False, False, True, True, "start")
      and S.playing_from(False, False, False, True, True, "continue")
      and S.playing_from(False, False, False, True, True, None)
      and not S.playing_from(False, False, False, True, False, None)
      and S.playing_from(True, False, False, False, False, "stop")
      and S.playing_from(False, True, False, False, False, None))


class PArp:
    clock_mode, enabled, current_chord, internal_bpm = "external", False, None, 120.0

    def __init__(self):
        self._last_pulse_wall = time.monotonic()

    def _estimated_bpm(self):
        return 120.0


pns = {"arp": PArp(), "seq": None, "beatbuddy": None, "CLOCK_EXTERNAL": "external", "_orc1_scenes": S,
       "time": time, "_scene_transport": ["stop"]}
sp = lift("_scene_playing", pns)
p_stop = sp()
pns["_scene_transport"][0] = "start"
p_start = sp()
pns["arp"]._last_pulse_wall -= 5.0
p_stale = sp()
check("review 6: the brain's _scene_playing uses transport + stale pulses (stop: no, start: yes, "
      "pulses 5 s old: no)", (p_stop, p_start, p_stale) == (False, True, False))
check("review 6b: the brain records every outside START / STOP / CONTINUE",
      body_has("_route_message", "_scene_transport[0] = msg.type"))


class FBB:
    beats = []

    def is_playing(self):
        return True

    def get_beat_in_bar(self):
        return self.beats[0]


got = []
bns2 = {"seq": None, "arp": FArp(), "beatbuddy": FBB(), "PPQN": 24, "_SMP_BEATS_BAR": 4,
        "_scene_last_step": [None], "_scene_last_bb": [None],
        "scenes": type("E", (), {"orc_beat": lambda self, d: got.append(d)})()}
pulse2 = lift("_scene_pulse", bns2)
for b in [3, 3, 0, 0, 0, 1, 1, 2, 2, 0]:          # a 3/4 BeatBuddy bar, several pulses per beat
    FBB.beats = [b]
    bns2["arp"]._pulse_total += 1
    pulse2()
check("review 6c: while the BeatBuddy plays, its own beat-in-bar is the grid (downbeat on beat 0)",
      got == [False, True, False, False, True], got)

# (9) one persistent worker thread
idents, done = [], threading.Event()
eng = S.SceneEngine(S.SceneStore(os.path.join(SANDBOX, "w.json")), FakeReg(), lambda *a: None,
                    lambda *a: None, lambda *a: None, lambda *a: None, log=lambda s: None)
for k in range(3):
    eng.spawn(lambda: idents.append(threading.get_ident()))
eng.spawn(done.set)
done.wait(2)
check("review 9: bursts run on ONE persistent worker thread (a queue), not a thread per recall",
      isinstance(eng.spawn, S.Worker) and len(idents) == 3 and len(set(idents)) == 1
      and idents[0] == eng.spawn.thread.ident and idents[0] != threading.get_ident(), idents)

# --- 9. the real HOME untouched -------------------------------------------------------
os.environ["HOME"] = os.environ["USERPROFILE"] = REAL_HOME
shutil.rmtree(SANDBOX, ignore_errors=True)
after = _real_files()
check("real ~/.orc1_* and ~/orc1_state.json byte-identical", after == _BEFORE,
      sorted(k for k in set(after) | set(_BEFORE) if after.get(k) != _BEFORE.get(k)))

print(f"\n{passed} passed, {failed} failed" + (f"  ({skipped} SKIPPED)" if skipped else ""))
if passed + failed != EXPECTED:
    print(f"EXPECTED {EXPECTED} checks, ran {passed + failed}: a check was added or removed")
    failed += 1
sys.exit(1 if failed else 0)
