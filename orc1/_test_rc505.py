#!/usr/bin/env python3
"""Regression suite for orc1_rc505.RC505Handler (the RC-505 output driver).
Pure stdlib -- stubs the mido Message builder so it runs anywhere. Asserts the
control semantics (momentary press vs continuous value, memory PC, transport) and
the load-bearing quirk guard: the handler NEVER emits clock."""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_rc505 as R

EXPECTED = 57
_p = _f = 0
def ok(c, m):
    global _p, _f
    if c: _p += 1
    else: _f += 1; print(f"  FAIL: {m}")

def make():
    """Handler whose sends are captured as plain dicts (no mido needed)."""
    sent = []
    h = R.RC505Handler(send=lambda m: sent.append(m), config_path=None, channel=1)
    h._msg = lambda **kw: dict(kw)     # capture as dicts instead of mido.Message
    return h, sent

# ---- name / classification helpers ----
ok(R.action_name("TR1 REC/PLAY") == "tr1_rec_play", "action_name track")
ok(R.action_name("ALL START/STOP") == "all_start_stop", "action_name all")
ok(R.action_name("MASTER COMP") == "master_comp", "action_name comp")
ok(R.is_continuous("MASTER COMP") and R.is_continuous("MASTER REVERB"), "comp/rev continuous")
ok(R.is_continuous("TR1 PLAY LEVEL") and R.is_continuous("TR3 PAN"), "level/pan continuous")
ok(not R.is_continuous("ALL START/STOP") and not R.is_continuous("TR1 REC/PLAY"), "triggers not continuous")
ok(not R.is_continuous("MEMORY INC"), "memory inc not continuous")

# ---- default map ----
h, sent = make()
ok(len(h.cc_map) == 16, f"default map 16 entries (got {len(h.cc_map)})")
ok(h.cc_map["tgt_tr_rec_play"]["cc"] == 1 and h.cc_map["tgt_tr_stop"]["cc"] == 2, "selected-track rec/stop -> cc1/2")
ok(h.cc_map["target_track_inc"]["cc"] == 3 and h.cc_map["target_track_dec"]["cc"] == 4, "track inc/dec -> cc3/4")
ok(h.cc_map["all_start_stop"]["cc"] == 5 and h.cc_map["undo_redo"]["cc"] == 6, "all start/stop cc5, undo cc6")
ok(h.cc_map["tr1_play_stop"]["cc"] == 7 and h.cc_map["tr5_rec_play"]["cc"] == 16
   and not h.cc_map["tr1_rec_play"]["continuous"], "per-track play/stop 7-11, rec/play 12-16, triggers")
ok(not any(e["continuous"] for e in h.cc_map.values()), "foot map: every entry is a trigger")
ok(h.by_cc[1] == "tgt_tr_rec_play" and h.by_cc[16] == "tr5_rec_play", "by_cc reverse map")
# the shipped rc505_config.json (PC copy) must equal the fallback map
_shipped = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "rc505 tools",
                        "rc505_config.json")
for _cand in (_shipped, os.path.expanduser("~/rc505_tools/rc505_config.json")):
    if os.path.exists(_cand):
        hs, _ = make(); hs.load_map(_cand)
        ok({n: e["cc"] for n, e in hs.cc_map.items()} == {n: e["cc"] for n, e in h.cc_map.items()},
           f"{_cand}: assigns == fallback map")
        break
else:
    ok(False, "no rc505_config.json found to compare with the fallback map")

# ---- map from a config file ----
cfgp = os.path.join(tempfile.gettempdir(), "rc505_test_cfg.json")
json.dump({"assigns": {"assigns": [
    {"slot": 1, "src": {"cc": 20}, "tgt": "TR2 PAN"},
    {"slot": 2, "src": {"cc": 64}, "tgt": "ALL CLEAR"}]}}, open(cfgp, "w"))
h2, _ = make(); h2.load_map(cfgp)
ok(len(h2.cc_map) == 2, "config map size 2")
ok(h2.cc_map["tr2_pan"]["cc"] == 20 and h2.cc_map["tr2_pan"]["continuous"], "config tr2_pan cc20 continuous")
ok(h2.cc_map["all_clear"]["cc"] == 64 and not h2.cc_map["all_clear"]["continuous"], "config all_clear cc64 trigger")
os.remove(cfgp)

# ---- do(): trigger = press (127 then 0) ----
h, sent = make(); sent.clear()
ok(h.do("all_start_stop") is True, "do trigger returns True")
ok(len(sent) == 2, f"trigger press = 2 msgs (got {len(sent)})")

ok(sent[0] == {"type": "control_change", "channel": 0, "control": 5, "value": 127}, "press msg1 = CC5 127")
ok(sent[1]["value"] == 0 and sent[1]["control"] == 5, "press msg2 = CC5 0")

# ---- do(): continuous = single CC with value (a map that has them) ----
def make_cont():
    h, sent = make()
    p = os.path.join(tempfile.gettempdir(), "rc505_test_cont.json")
    json.dump({"assigns": {"assigns": [
        {"slot": 1, "src": {"cc": 15}, "tgt": "MASTER COMP"},
        {"slot": 2, "src": {"cc": 16}, "tgt": "MASTER REVERB"},
        {"slot": 3, "src": {"cc": 2}, "tgt": "TR1 REC/PLAY"}]}}, open(p, "w"))
    h.load_map(p); os.remove(p)
    return h, sent
h, sent = make_cont(); sent.clear()
h.do("master_comp")
ok(len(sent) == 1 and sent[0]["control"] == 15 and sent[0]["value"] == 127, "continuous default -> one CC15=127")
sent.clear(); h.do("master_reverb", 64)
ok(len(sent) == 1 and sent[0]["control"] == 16 and sent[0]["value"] == 64, "continuous value -> CC16=64")

# ---- do(): trigger with explicit 0 = release only ----
h, sent = make(); sent.clear(); h.do("tr1_rec_play", 0)
ok(len(sent) == 1 and sent[0]["control"] == 12 and sent[0]["value"] == 0, "trigger value 0 -> single release")

# ---- do(): unmapped ----
h, sent = make(); sent.clear()
ok(h.do("does_not_exist") is False and len(sent) == 0, "unmapped -> False, no send")

# ---- send_cc bounds ----
h, sent = make(); sent.clear()
ok(h.send_cc(200, 0) is False and len(sent) == 0, "cc>127 rejected")
ok(h.send_cc(15, 200) is False and len(sent) == 0, "value>127 rejected")
ok(h.send_cc(15, 100) is True and sent[-1]["value"] == 100, "valid cc accepted")

# ---- memory + transport ----
h, sent = make(); sent.clear()
ok(h.select_memory(4) is True and sent[-1] == {"type": "program_change", "channel": 0, "program": 4}, "select_memory PC4")
ok(h.select_memory(200) is False, "memory>127 rejected")
sent.clear(); h.transport(True); ok(sent[-1]["type"] == "start", "transport start")
h.transport(False); ok(sent[-1]["type"] == "stop", "transport stop")

# ---- channel respected ----
h3, s3 = make(); h3.channel = 10; h3._msg = lambda **kw: dict(kw)
h3.send_cc(1, 1); ok(s3[-1]["channel"] == 9, "channel 10 -> midi channel 9")

# ---- THE QUIRK GUARD: no API ever emits clock/continue/songpos ----
h, sent = make(); sent.clear()
for name in h.cc_map:
    h.do(name)
h.select_memory(3); h.transport(True); h.transport(False); h.send_cc(7, 7); h.press_cc(9)
bad = [m for m in sent if m.get("type") in ("clock", "continue", "songpos", "reset")]
ok(len(bad) == 0, f"handler never emits clock/transport-sync junk (got {bad})")

# ---- memory tracking + clamps ----
h, _ = make()
h.select_memory(5); ok(h.current_memory == 5, "select_memory tracks current_memory")
ok(h.select_memory(99) is False, "memory 99 rejected (>98)")
h.current_memory = 0; h.bank(-1); ok(h.current_memory == 0, "bank clamps at 0")
h.current_memory = 98; h.bank(+1); ok(h.current_memory == 98, "bank clamps at 98")

# ---- actions() shape ----
ok(set(h.actions()["tgt_tr_stop"]) >= {"cc", "continuous", "target"}, "actions() exposes cc/continuous/target")

# ---- held presses (foot switches): non-blocking, every press ends in a release ----
import time as _t
R.RC505Handler.PRESS_GAP_S = 0.04
def cc_vals(sent, cc):
    return [m["value"] for m in sent if m.get("control") == cc]

# SP-style: the RC sees the real press and release
h, sent = make(); sent.clear()
ok(h.press_down("tgt_tr_stop") is True and cc_vals(sent, 2) == [127], "press_down -> CC2 127 only")
_t.sleep(0.2)
ok(cc_vals(sent, 2) == [127] and h.held() == [2], "while the foot is down the RC button stays held")
ok(h.press_down("tgt_tr_stop") is False and cc_vals(sent, 2) == [127], "a 2nd down while held is refused")
h.press_up("tgt_tr_stop")
ok(cc_vals(sent, 2) == [127, 0] and h.held() == [], "press_up after the gap -> CC2 0 at once")

# a release sooner than the gap is delayed to the gap, never dropped
h, sent = make(); sent.clear()
t0 = _t.monotonic(); h.press_down("undo_redo"); h.press_up("undo_redo")
ok(cc_vals(sent, 6) == [127], "early release: 0 not sent yet")
_t.sleep(0.1)
ok(cc_vals(sent, 6) == [127, 0] and h.held() == [], "early release: 0 arrives after the gap")

# EF-style tap: a complete press without blocking the caller
h, sent = make(); sent.clear()
t0 = _t.monotonic(); h.tap("all_start_stop"); dt = _t.monotonic() - t0
ok(dt < 0.02 and cc_vals(sent, 5) == [127], f"tap returns at once ({dt*1000:.0f} ms), 127 sent")
_t.sleep(0.1)
ok(cc_vals(sent, 5) == [127, 0], "tap: release follows by itself")
h.tap("all_start_stop"); _t.sleep(0.1)
ok(cc_vals(sent, 5) == [127, 0, 127, 0], "two taps = two full presses (the RC needs the 0 between)")

# hold_for: the ORC holds the button itself; the foot's release can't cut it short
h, sent = make(); sent.clear()
ok(h.hold_for("tgt_tr_stop", 0.3) is True, "hold_for accepted")
h.press_up("tgt_tr_stop"); _t.sleep(0.1)
ok(cc_vals(sent, 2) == [127], "hold_for: still held at 0.1 s despite a release")
ok(h.tap("tgt_tr_stop") is False, "hold_for: a new press during the hold is refused")
_t.sleep(0.35)
ok(cc_vals(sent, 2) == [127, 0] and h.held() == [], "hold_for: released after its time")
ok(R.RC505Handler.HOLD_S >= 2.5, "default clear hold outlasts the RC's ~2 s hold-STOP")

# a lost release can't hold a button forever
h, sent = make(); h.HOLD_MAX_S = 0.15; sent.clear()
h.press_down("tr1_play_stop"); _t.sleep(0.3)
ok(cc_vals(sent, 7) == [127, 0], "no release ever -> auto-released after HOLD_MAX_S")

# a stale timer from an old press must not release a newer one
# (a cancelled Timer can still fire if it was already running: simulate that call)
h, sent = make(); sent.clear()
h.press_down("tr2_play_stop"); old_tok = h._held[8]["token"]
_t.sleep(0.05); h.press_up("tr2_play_stop")
h.press_down("tr2_play_stop"); h._release(8, old_tok)
ok(cc_vals(sent, 8) == [127, 0, 127] and h.held() == [8], "new press held; old timer didn't release it")
h.press_up("tr2_play_stop")

ok(h.press_down("nope") is False and h.tap("nope") is False and h.hold_for("nope") is False,
   "unmapped names refused")

print(f"\n_test_rc505: {_p} passed, {_f} failed  (expected {EXPECTED})")
if _p != EXPECTED or _f:
    sys.exit(1)
