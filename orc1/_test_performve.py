#!/usr/bin/env python3
"""
Perform-VE follow (additive chord feed) regression suite.

orc1.py can't be imported (runs as __main__, opens hardware), so — like the chord
suites — AST-extract the two feed helpers and exec them against stubs, then source-net
the wiring sites.  Pure stdlib; safe to run while the rig is in use.

Mutation-proof it: revert the prev-release loop in _performve_feed -> the re-key test
fails; drop the _performve_feed(notes) call in play_chord -> the wiring net fails.
"""
import ast, os, sys, json, tempfile, re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

def func_src(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SRC, node)
    raise SystemExit(f"function {name!r} not found in orc1.py")

# ---- stub environment ------------------------------------------------------
class Msg:
    def __init__(self, type, channel=0, note=0, velocity=0, control=0, value=0, program=0):
        self.type = type; self.channel = channel; self.note = note
        self.velocity = velocity; self.control = control; self.value = value
        self.program = program
    def __repr__(self):
        return f"{self.type}(ch={self.channel},n={self.note},v={self.velocity},cc={self.control},pg={self.program})"
class MidoStub:
    Message = Msg

SENT = []
def send_to(port, msg): SENT.append((port, msg))

state = {"performve_feed": False, "_pve_held": []}
class _Cfg:
    CH_CTRL = 13
NS = {"mido": MidoStub, "send_to": send_to, "state": state, "cfg": _Cfg}
exec(func_src("_performve_feed"), NS)
exec(func_src("_performve_feed_off"), NS)
exec(func_src("_performve_src_hit"), NS)
feed = NS["_performve_feed"]
feed_off = NS["_performve_feed_off"]
src_hit = NS["_performve_src_hit"]

# ---- tiny harness ----------------------------------------------------------
_p = _f = 0
def check(cond, label):
    global _p, _f
    if cond: _p += 1
    else:
        _f += 1; print(f"  FAIL: {label}")

def reset(on):
    SENT.clear(); state["performve_feed"] = on; state["_pve_held"] = []

# 1. OFF -> nothing sent, nothing held
reset(False); feed([60, 64, 67])
check(SENT == [], "feed OFF sends nothing")
check(state["_pve_held"] == [], "feed OFF holds nothing")

# 2. ON -> one note_on per note, all on ch0 (MIDI ch1), velocity>0
reset(True); feed([60, 64, 67])
ons = [m for _, m in SENT if m.type == "note_on"]
check(len(ons) == 3 and all(m.channel == 0 for m in ons), "feed ON sends 3 note_ons on ch1")
check([m.note for m in ons] == [60, 64, 67], "feed ON sends the exact notes")
check(all(m.velocity > 0 for m in ons), "feed ON note_ons are audible velocity")
check(state["_pve_held"] == [60, 64, 67], "feed ON tracks held notes")

# 3. RE-KEY -> releases the previous chord first, then sends the new one
SENT.clear(); feed([62, 65, 69])
offs = [m for _, m in SENT if m.type == "note_off"]
ons2 = [m for _, m in SENT if m.type == "note_on"]
check([m.note for m in offs] == [60, 64, 67], "re-key releases the previous chord")
check([m.note for m in ons2] == [62, 65, 69], "re-key sends the new chord")
# ordering: every off precedes the first on (clean re-key)
kinds = [m.type for _, m in SENT]
check(kinds.index("note_off") < kinds.index("note_on"), "re-key: offs before ons")
check(state["_pve_held"] == [62, 65, 69], "re-key updates held notes")

# 4. feed_off -> releases held, clears
SENT.clear(); feed_off()
check([m.note for _, m in SENT] == [62, 65, 69] and all(m.type == "note_off" for _, m in SENT),
      "feed_off releases all held notes")
check(state["_pve_held"] == [], "feed_off clears held")

# 5. out-of-range notes are skipped
reset(True); feed([-1, 60, 200, 72])
check([m.note for _, m in SENT if m.type == "note_on"] == [60, 72], "out-of-range notes skipped")

# 6. OFF while notes held is still a no-send re-key guard (toggling off via helper)
reset(True); feed([48, 52, 55])
state["performve_feed"] = False; SENT.clear(); feed([50, 53, 57])
check(SENT == [], "feed goes silent immediately when toggled off")

# ---- input-source predicate (option A: consume an external chord stream) ---
NOTE = "note_on"
check(src_hit(None, "usb", NOTE, 0) is False, "src: None config never hits")
check(src_hit({"port": None}, "usb", NOTE, 0) is False, "src: port None never hits")
check(src_hit({"port": "usb"}, "usb", NOTE, 5) is True, "src: usb matches usb")
check(src_hit({"port": "usb"}, "din", NOTE, 5) is False, "src: usb does not match din")
check(src_hit({"port": "din"}, "din", NOTE, 5) is True, "src: din matches din")
check(src_hit({"port": "both"}, "usb", NOTE, 5) is True, "src: both matches usb")
check(src_hit({"port": "both"}, "din", NOTE, 5) is True, "src: both matches din")
check(src_hit({"port": "usb"}, "usb", "control_change", 5) is False, "src: non-note not claimed")
check(src_hit({"port": "usb"}, "usb", "note_off", 5) is True, "src: note_off is claimed")
# 1-based channel filter
check(src_hit({"port": "usb", "channel": 3}, "usb", NOTE, 2) is True, "src: channel 3 matches msg ch2 (0-based)")
check(src_hit({"port": "usb", "channel": 3}, "usb", NOTE, 4) is False, "src: channel filter rejects other channels")
check(src_hit({"port": "usb", "channel": None}, "usb", NOTE, 9) is True, "src: channel None = any channel")
# CH_CTRL(13) stays the ORC's control inbox -- same reservation as pre-rules
check(src_hit({"port": "both", "channel": None}, "usb", NOTE, 12) is False,
      "src: channel ANY never claims CH_CTRL(13) notes")
check(src_hit({"port": "usb", "channel": 13}, "usb", NOTE, 12) is False,
      "src: an explicit channel-13 source still cannot claim CH_CTRL")

# ---- wiring nets (source-level) -------------------------------------------
play = func_src("play_chord")
alloff = func_src("all_notes_off")
route_n = func_src("_route_message").replace("\r\n", "\n")
check("_performve_feed(notes)" in play, "play_chord calls _performve_feed(notes)")
check("_performve_feed_off()" in alloff, "all_notes_off calls _performve_feed_off()")
check("_performve_src_hit(" in route_n, "_route_message calls _performve_src_hit()")
check('send_to("performve", msg.copy(channel=0))\n        return' in route_n,
      "_route_message forwards the claimed note then returns (consumes)")
check('action == "SET_PERFORMVE_FEED"' in SRC, "SET_PERFORMVE_FEED cmd action present")
check('action == "SET_PERFORMVE_SRC"' in SRC, "SET_PERFORMVE_SRC cmd action present")
check("_load_performve()" in SRC and "_save_performve()" in SRC, "persistence load+save wired")
check(not re.search(r'"performve"\s*:\s*"Perform-VE"', SRC),
      "port lives in config, not hardcoded in orc1.py")
check('"performve_feed": state["performve_feed"]' in SRC
      and 'dict(state["performve_src"])' in SRC,
      "/state snapshot includes performve_feed + performve_src")

# ── persistence round-trip (FUNCTIONAL, temp file — never the live json) ──────
_pf = tempfile.mktemp(suffix="_pve.json")
NSP = {"json": json, "os": os, "_PERFORMVE_FILE": _pf, "print": lambda *a, **k: None,
       "state": {"performve_feed": False, "performve_src": {"port": None, "channel": None}}}
exec(func_src("_load_performve"), NSP); exec(func_src("_save_performve"), NSP)
_load = NSP["_load_performve"]; _save = NSP["_save_performve"]; st = NSP["state"]
st["performve_feed"] = True; st["performve_src"] = {"port": "din", "channel": 5}; _save()
st["performve_feed"] = False; st["performve_src"] = {"port": None, "channel": None}; _load()
check(st["performve_feed"] is True and st["performve_src"] == {"port": "din", "channel": 5},
      "persistence: save->load round-trips feed + src")
open(_pf, "w").write('{"on": true}')                       # legacy single-key file
st["performve_src"] = {"port": "usb", "channel": 3}; _load()
check(st["performve_feed"] is True and st["performve_src"] == {"port": None, "channel": None},
      "persistence: legacy {on}-only file migrates (src -> defaults)")
open(_pf, "w").write("{not valid json")
st["performve_feed"] = True; _load()
check(st["performve_feed"] is True, "persistence: corrupt JSON does not crash or clobber")
open(_pf, "w").write('{"on": false, "src": {"port": "evil", "channel": 9}}'); _load()
check(st["performve_src"]["port"] is None and st["performve_src"]["channel"] == 9,
      "persistence: non-whitelisted port rejected on load")
try: os.remove(_pf)
except OSError: pass

# ── cmd-handler behavior: the disable->release + CC123-flush + persist wiring ──
class _Lock:
    def __enter__(self): return self
    def __exit__(self, *a): return False
_calls = {"off": 0, "save": 0}; _SENT2 = []
NSA = {"_state_lock": _Lock(), "mido": MidoStub, "send_to": lambda p, m: _SENT2.append((p, m)),
       "_performve_feed_off": lambda: _calls.__setitem__("off", _calls["off"] + 1),
       "_save_performve":     lambda: _calls.__setitem__("save", _calls["save"] + 1),
       "state": {"performve_feed": False, "performve_src": {}}}
exec(func_src("_apply_performve_feed"), NSA); exec(func_src("_apply_performve_src"), NSA)
_afeed = NSA["_apply_performve_feed"]; _asrc = NSA["_apply_performve_src"]; sta = NSA["state"]
_calls.update(off=0, save=0); r = _afeed(True)
check(r is True and sta["performve_feed"] is True and _calls["off"] == 0 and _calls["save"] == 1,
      "cmd: enable feed sets state + persists, does NOT release")
_calls.update(off=0, save=0); _afeed(False)
check(sta["performve_feed"] is False and _calls["off"] == 1,
      "cmd: disable feed RELEASES notes held at the box")
_SENT2.clear(); _calls.update(save=0); out = _asrc("din", 5)
_cc = [m for _, m in _SENT2 if m.type == "control_change" and m.control == 123 and m.channel == 0]
check(out == {"port": "din", "channel": 5} and len(_cc) == 1 and _calls["save"] == 1,
      "cmd: set src normalises + sends CC123 flush + persists")
out = _asrc("garbage", None)
check(out["port"] is None and out["channel"] is None, "cmd: set src rejects a bad port -> None")

# ── CC / PC fire-and-forget handlers ──────────────────────────────────────────
_SENT3 = []
NSC = {"mido": MidoStub, "send_to": lambda p, m: _SENT3.append((p, m))}
exec(func_src("_apply_performve_cc"), NSC); exec(func_src("_apply_performve_pc"), NSC)
_acc = NSC["_apply_performve_cc"]; _apc = NSC["_apply_performve_pc"]

_SENT3.clear(); r = _acc(53, 127)
_m = _SENT3[0][1] if _SENT3 else None
check(r == (53, 127) and _SENT3 and _SENT3[0][0] == "performve" and _m.type == "control_change"
      and _m.control == 53 and _m.value == 127 and _m.channel == 0,
      "cmd: CC sends control_change on performve ch1 + returns (cc,value)")
_SENT3.clear(); r = _acc(200, 10)
check(r is None and not _SENT3, "cmd: CC out-of-range cc -> None, nothing sent")
_SENT3.clear(); r = _acc(20, 999)
check(r is None and not _SENT3, "cmd: CC out-of-range value -> None, nothing sent")
_SENT3.clear(); r = _acc("x", 5)
check(r is None and not _SENT3, "cmd: CC non-int -> None, nothing sent")
_SENT3.clear(); r = _apc(2)
_m = _SENT3[0][1] if _SENT3 else None
check(r == 2 and _m and _m.type == "program_change" and _m.program == 2 and _m.channel == 0,
      "cmd: PC sends program_change on performve ch1 + returns program")
_SENT3.clear(); r = _apc(200)
check(r is None and not _SENT3, "cmd: PC out-of-range -> None, nothing sent")

check('action == "SET_PERFORMVE_CC"' in SRC, "SET_PERFORMVE_CC cmd action present")
check('action == "SET_PERFORMVE_PC"' in SRC, "SET_PERFORMVE_PC cmd action present")

EXPECTED = 53
total = _p + _f
print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or total != EXPECTED:
    sys.exit(1)
