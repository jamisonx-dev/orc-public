#!/usr/bin/env python3
"""Module Matrix — slice 1 engine-core regression + leak tests.

Run on the Pi:  python3 _test_module_bind.py

Two halves:
  * SYNTH / HOST side is IMPORTED directly (orc1_synth's import is side-effect
    free; the host modules likewise defer jack/mido). All engines split to their
    own processes 2026-09-02 and the synth's own dispatcher (`_route_midi`,
    `SynthEngine`) was removed 2026-09-24 (asserted gone), so the LIVE dispatch +
    bind handshake are driven through the poly + chip host `_make_router`s with a
    fake engine, off the live `S._BINDINGS`.
  * BRAIN side can't be imported (orc1.py opens hardware as __main__), so its
    Module-Matrix functions are extracted by AST and exec'd against stubs -- the
    same technique as _test_chord_o3.py. The headline there is the TAKE-OUT LEAK
    test: an unbound voice must never be mirrored (or it leaks to the DIN).

Expected: EXPECTED_TOTAL checks, 0 failed. A LOW total is a regression even at
"0 failed" -- a whole group silently not running.
"""

import os, sys, ast, json, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EXPECTED_TOTAL = 66   # was 52; +4 when the poly+chip split moved dispatch tests
                      # onto the live host routers (audio host split, 2026-09-02);
                      # +10 rebind/unbind releases held notes (2026-10-01)
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ! {label}")


# ═══════════════════════════════════════════════════════════════════════════
# SYNTH SIDE — imported, real dispatch
# ═══════════════════════════════════════════════════════════════════════════
import mido
import orc1_synth as S

class FakeEng:
    def __init__(self):
        self.calls = []
        self._bus_touched = set()
        self._bus_levels_dirty = False
        self.master = self.drum_master = 0.0
        self.poly = type("P", (), {"master": 0.0})()
        self.chip = type("C", (), {"master": 0.0})()
    def all_sound_off(self):              self.calls.append(("panic",))
    def drum_trigger(self, v, vel):       self.calls.append(("drum", v, vel))
    def poly_note_on(self, n, vel):       self.calls.append(("poly_on", n, vel))
    def poly_note_off(self, n):           self.calls.append(("poly_off", n))
    def poly_cc(self, c, v):              self.calls.append(("poly_cc", c, v))
    def chip_note_on(self, voice, n, vel):self.calls.append(("chip_on", voice, n, vel))
    def chip_note_off(self, n, voice):    self.calls.append(("chip_off", n, voice))
    def chip_cc(self, voice, c, v):       self.calls.append(("chip_cc", voice, c, v))
    def chip_pitch(self, voice, p):       self.calls.append(("chip_pitch", voice, p))

def m_note(ch1, note=60, vel=100):
    return mido.Message("note_on", channel=ch1 - 1, note=note, velocity=vel)
def m_cc(ch1, cc, val):
    return mido.Message("control_change", channel=ch1 - 1, control=cc, value=val)

def id_of(key):
    return next(i for i, k in S._MODULE_BY_ID.items() if k == key)
def ch_of(key):
    return next(c for c, m in S._BINDINGS.items() if m == key)

def reset_synth():
    S._BINDINGS = dict(S._DEFAULT_BINDINGS)

# -- host-router dispatch (the LIVE dispatchers) ------------------------------
# ALL engines split to their own processes 2026-09-02 (audio host split): drums,
# sampler, poly (step 3a) and chip (step 3b). The synth's old `_route_midi`
# dispatched nothing after that and was removed 2026-09-24. The live dispatch +
# bind handshake live in each host's `_make_router` (which reads `S._BINDINGS`,
# so reset_synth() below seeds it). We drive those real routers here; that the
# synth library has no dispatcher left is asserted first.
import orc1_poly_host as PH
import orc1_chip_host as CH

def m_pw(ch1, pitch):
    return mido.Message("pitchwheel", channel=ch1 - 1, pitch=pitch)

# The synth library has NO dispatcher of its own any more (all host-owned): the
# retired single-process runtime was removed 2026-09-24, so nothing can listen
# on the bus as a second, conflicting "ORC1-Synth" if the old unit is started.
check(not any(hasattr(S, n) for n in ("_route_midi", "SynthEngine",
                                      "midi_input_thread", "run_jack", "main")),
      "synth library has no dispatcher/runtime (all dispatch host-owned)")

# -- POLY host router ---------------------------------------------------------
reset_synth()
poly_ch = ch_of("poly")
peng = FakeEng(); proute = PH._make_router(peng)
proute(m_note(poly_ch, 60, 100))
check(("poly_on", 60, 100) in peng.calls, "poly host: note dispatches on bound channel")
proute(mido.Message("note_off", channel=poly_ch - 1, note=60, velocity=0))
check(("poly_off", 60) in peng.calls, "poly host: note_off dispatches")
proute(m_cc(poly_ch, 74, 100))
check(("poly_cc", 74, 100) in peng.calls, "poly host: CC dispatches")
proute(mido.Message("control_change", channel=0, control=120, value=0))
check(("panic",) in peng.calls, "poly host: panic (CC120) fires")

peng2 = FakeEng(); proute2 = PH._make_router(peng2)
proute2(m_cc(S._VOL_CH, S._VOL_POLY_CC, 100))
check(peng2.poly.master == 100 / 127 and "poly" in peng2._bus_touched,
      "poly host: CH_CTRL level sets the bus master + marks persist")

# arm -> commit rebind on the poly host
peng3 = FakeEng(); proute3 = PH._make_router(peng3)
proute3(m_cc(S._VOL_CH, S._BIND_ARM_CC, id_of("poly")))
proute3(m_note(3, S._BIND_COMMIT_NOTE, 1))       # sentinel commit on ch3
proute3(m_note(3, 67, 80))
check(("poly_on", 67, 80) in peng3.calls, "poly host: rebinds to the new channel via the handshake")
check(("poly_on", S._BIND_COMMIT_NOTE, 1) not in peng3.calls, "poly host: commit note is consumed, never sounded")
peng3.calls.clear()
proute3(m_note(poly_ch, 67, 80))
check(("poly_on", 67, 80) not in peng3.calls, "poly host: the old channel no longer sounds")

peng4 = FakeEng(); proute4 = PH._make_router(peng4)
proute4(m_cc(S._VOL_CH, S._BIND_CLEAR_CC, id_of("poly")))
proute4(m_note(poly_ch, 60, 100))
check(("poly_on", 60, 100) not in peng4.calls, "poly host: CLEAR removes the channel")

# -- CHIP host router ---------------------------------------------------------
reset_synth()
p1_ch  = ch_of("chip_p1"); wav_ch = ch_of("gb_wave")
ceng = FakeEng(); croute = CH._make_router(ceng)
croute(m_note(p1_ch, 62, 90))
check(("chip_on", "pulse1", 62, 90) in ceng.calls, "chip host: chip_p1 dispatches as pulse1")
croute(m_note(wav_ch, 64, 80))
check(("chip_on", "wave", 64, 80) in ceng.calls, "chip host: gb_wave dispatches as 'wave'")
croute(mido.Message("note_off", channel=p1_ch - 1, note=62, velocity=0))
check(("chip_off", 62, "pulse1") in ceng.calls, "chip host: note_off dispatches with its voice")
croute(m_cc(p1_ch, 74, 100))
check(("chip_cc", "pulse1", 74, 100) in ceng.calls, "chip host: CC dispatches per voice")
croute(m_pw(p1_ch, 4096))
check(("chip_pitch", "pulse1", 4096) in ceng.calls, "chip host: pitchwheel dispatches per voice")
croute(mido.Message("control_change", channel=0, control=123, value=0))
check(("panic",) in ceng.calls, "chip host: panic (CC123) fires")

ceng2 = FakeEng(); croute2 = CH._make_router(ceng2)
croute2(m_cc(S._VOL_CH, S._VOL_CHIP_CC, 100))
check(ceng2.chip.master == 100 / 127 and "chip" in ceng2._bus_touched,
      "chip host: CH_CTRL level sets the bus master + marks persist")

# F1 sentinel guard + arm -> commit rebind on the chip host
ceng3 = FakeEng(); croute3 = CH._make_router(ceng3)
croute3(m_cc(S._VOL_CH, S._BIND_ARM_CC, id_of("chip_p1")))
croute3(m_note(p1_ch, 64, 90))                   # a real note while armed
check(("chip_on", "pulse1", 64, 90) in ceng3.calls, "chip host F1: a real note while armed still sounds (not eaten)")
croute3(m_note(3, S._BIND_COMMIT_NOTE, 1))       # sentinel commit on ch3
ceng3.calls.clear()
croute3(m_note(3, 67, 80))
check(("chip_on", "pulse1", 67, 80) in ceng3.calls, "chip host: chip_p1 rebinds to the new channel")

# a commit on a RESERVED channel is ignored
ceng4 = FakeEng(); croute4 = CH._make_router(ceng4)
croute4(m_cc(S._VOL_CH, S._BIND_ARM_CC, id_of("chip_p1")))
croute4(m_note(13, S._BIND_COMMIT_NOTE, 100))    # sentinel on RESERVED ch13
ceng4.calls.clear()
croute4(m_note(13, 60, 100))
check(("chip_on", "pulse1", 60, 100) not in ceng4.calls, "chip host: commit ignores a RESERVED channel")

# -- bindings file load -------------------------------------------------------
_d = tempfile.mkdtemp()
S._BINDINGS_FILE = os.path.join(_d, "b.json")
with open(S._BINDINGS_FILE, "w") as f:
    f.write("{ not valid json")
check(S._load_bindings() == dict(S._DEFAULT_BINDINGS), "corrupt bindings file -> shipped defaults")
with open(S._BINDINGS_FILE, "w") as f:
    json.dump({"bindings": {"3": "poly", "10": "drums"}}, f)
_b = S._load_bindings()
check(_b.get(3) == "poly" and _b.get(10) == "drums", "valid bindings file loads inverted map")
with open(S._BINDINGS_FILE, "w") as f:
    json.dump({"bindings": {"13": "poly"}}, f)
check(S._load_bindings() == dict(S._DEFAULT_BINDINGS), "reserved channel in file rejected")


# ═══════════════════════════════════════════════════════════════════════════
# BRAIN SIDE — AST-extracted, exec'd against stubs
# ═══════════════════════════════════════════════════════════════════════════
import orc1_config as cfg

_SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_WANT = {"module_bind", "module_unbind", "_module_channel", "_module_id",
         "_rebuild_synth_chs", "_save_module_bindings", "_mm_seed_from_cfg",
         "_mm_push_cfg", "_load_module_bindings", "_mm_emit_arm_commit",
         "_mm_emit_clear", "_synth_mirror_on", "module_join", "_mm_join_mode_cc",
         "_release_module_notes"}
_segs = [ast.get_source_segment(_SRC, n) for n in _TREE.body
         if isinstance(n, ast.FunctionDef) and n.name in _WANT]
check(len(_segs) == len(_WANT), f"extracted all {len(_WANT)} brain module-matrix functions")
# module-level constant the release helper reads (an Assign, not a FunctionDef)
_segs += [ast.get_source_segment(_SRC, n) for n in _TREE.body
          if isinstance(n, ast.Assign)
          and any(getattr(t, "id", None) == "_MM_SUSTAINING" for t in n.targets)]

# F5 net: _arp_note_on is nested inside main() (AST-unreachable to exec), so assert
# at SOURCE level that its mirror emit is gated on the module being bound. A
# regression that drops the gate would otherwise ship green.
_arp_fn = next((n for n in ast.walk(_TREE)
                if isinstance(n, ast.FunctionDef) and n.name == "_arp_note_on"), None)
_arp_src = ast.get_source_segment(_SRC, _arp_fn) if _arp_fn else ""
check("_module_channel(" in _arp_src and "if not mch" in _arp_src,
      "arp mirror emit is gated on the module being bound (leak net)")

sent = []
def send_synth_midi(msg): sent.append(msg)
def print_status(*a, **k): pass
mirror_off = [0]
def _synth_mirror_off(): mirror_off[0] += 1
brain_state = {"synth_mirror_poly": True, "synth_mirror_chip": False,
               "_mirror_held": []}
_tmp = tempfile.mkdtemp()

ns = {
    "cfg": cfg, "state": brain_state, "mido": mido, "json": json, "os": os,
    "send_synth_midi": send_synth_midi, "print_status": print_status,
    "_synth_mirror_off": _synth_mirror_off,
    "_CH_SAMPLER": int(cfg.SAMPLER.get("CH_SAMPLER", 7)),
    "_SYNTH_CHS": {},
    "_MM_KEYS": list(cfg.MODULE_KEYS), "_MM_CH_KEY": dict(cfg.MODULE_CH_KEY),
    "_MM_RESERVED": set(cfg.RESERVED_CHANNELS),
    "_MM_NO_TAKEOUT": set(cfg.MODULE_NO_TAKEOUT),
    "_MM_ARM_CC": cfg.BIND_ARM_CC, "_MM_CLEAR_CC": cfg.BIND_CLEAR_CC,
    "_MM_COMMIT_NOTE": cfg.BIND_COMMIT_NOTE, "_MM_CTRL_CH": cfg.CH_CTRL,
    "_BINDINGS_FILE": os.path.join(_tmp, "bind.json"), "_bindings": {},
    "_MM_COUPLE_MODES": list(cfg.COUPLE_MODES),
    "_MM_JOIN_MODE_CC": cfg.JOIN_MODE_CC, "_MM_JOIN_DETUNE_CC": cfg.JOIN_DETUNE_CC,
    "_MM_JOIN_SPREAD_CC": cfg.JOIN_SPREAD_CC,
    "_mm_join": {"mode": "none", "detune": 0, "spread": False},
    "arp": None, "fire": None,
}
exec("\n\n".join(_segs), ns)

# Work on a private copy of SYNTH so the test can't disturb the imported config.
cfg.SYNTH = dict(cfg.SYNTH)

ns["_load_module_bindings"]()
check(ns["_bindings"].get("poly") == 8, "brain seeds poly channel from cfg (8)")
check(ns["_bindings"].get("sampler") == 7, "brain seeds sampler channel (7)")
check(8 in ns["_SYNTH_CHS"] and 10 in ns["_SYNTH_CHS"], "_SYNTH_CHS built from bindings")

before = dict(ns["_bindings"])
ns["module_bind"]("poly", 10)                    # 10 held by drums
check(ns["_bindings"] == before, "bind COLLISION rejected (10 held by drums)")
ns["module_bind"]("poly", 13)                    # reserved
check(ns["_bindings"].get("poly") == 8, "bind to RESERVED channel rejected")
ns["module_bind"]("nope", 3)                     # unknown module
check("nope" not in ns["_bindings"], "bind of UNKNOWN module rejected")
check(15 not in cfg.RESERVED_CHANNELS, "ch15 (melody) is NOT reserved")
ns["module_bind"]("poly", 15)                    # 15 no longer reserved -> allowed
check(ns["_bindings"].get("poly") == 15, "voice can bind to ch15 (unreserved)")

sent.clear()
ns["module_bind"]("poly", 3)                     # valid: 3 is free
check(ns["_bindings"].get("poly") == 3, "valid reassign moves poly to 3")
check(cfg.SYNTH["CH_POLY"] == 3, "cfg.SYNTH follows the bind (no desync)")
check(3 in ns["_SYNTH_CHS"] and 8 not in ns["_SYNTH_CHS"], "_SYNTH_CHS rebuilt on bind")
check(any(getattr(x, "control", None) == cfg.BIND_ARM_CC for x in sent),
      "bind emits the ARM handshake to the synth")
check(any(x.type == "note_on" and x.channel == 2 for x in sent),
      "bind emits the commit note on the target channel (3)")

ns["module_bind"]("sampler", 1)                  # 1 is free (non-reserved, unheld)
check(ns["_CH_SAMPLER"] == 1, "sampler reassign updates the brain's _CH_SAMPLER")
check(ns["_bindings"].get("sampler") == 1, "sampler reassign moves its binding")

ns["module_unbind"]("chip_noise")
check("chip_noise" not in ns["_bindings"], "take-out removes the module")
ns["module_unbind"]("sampler")
check("sampler" in ns["_bindings"], "sampler CANNOT be taken out of the fold")

# -- REBIND / UNBIND RELEASES HELD NOTES (2026-10-01) ------------------------
# A note held on a module's OLD channel used to get its note-off there after the
# move -- a channel the host no longer reads = a stuck note. The release must go
# out on the OLD channel BEFORE the bind handshake, must NOT be CC120/123 (every
# host treats those as a GLOBAL panic), and the brain must forget what it held.
import threading as _thr
class _Arp:
    def __init__(self): self._lock = _thr.Lock(); self._held = []
class _Fire:
    def __init__(self): self.forgot = []
    def forget_synth_channel(self, ch): self.forgot.append(ch)
ns["arp"], ns["fire"] = _Arp(), _Fire()
ns["_bindings"] = {"poly": 3, "chip_p1": 9, "drums": 10, "sampler": 7}
brain_state["held_notes"] = [(60, "synth", 3), (64, "synth", 9), (60, "pisound", 3)]
ns["arp"]._held = [(67, "synth", 3), (72, "synth", 9)]
sent.clear()
ns["module_bind"]("poly", 4)
_offs = [i for i, x in enumerate(sent) if x.type == "note_off" and x.channel == 2]
_arm = [i for i, x in enumerate(sent) if getattr(x, "control", None) == cfg.BIND_ARM_CC]
check(len(_offs) == 128, "rebind: a note-off for every note on the OLD channel (3)")
check(_offs and _arm and max(_offs) < min(_arm),
      "rebind: the release goes out BEFORE the bind handshake (host still listening)")
check(not any(getattr(x, "control", None) in (120, 123) for x in sent),
      "rebind: no CC120/123 (a global panic in every host)")
check(not any(x.type == "note_off" and x.channel != 2 for x in sent),
      "rebind: no other channel is touched")
check(brain_state["held_notes"] == [(64, "synth", 9), (60, "pisound", 3)],
      "rebind: held chord notes on the old synth channel are forgotten (others kept)")
check(ns["arp"]._held == [(72, "synth", 9)] and ns["fire"].forgot == [3],
      "rebind: the arp + Fire forget their notes on the old channel")
sent.clear()
ns["module_bind"]("poly", 4)                     # same channel again
check(not any(x.type == "note_off" for x in sent), "re-bind to the SAME channel releases nothing")
sent.clear()
ns["module_unbind"]("chip_p1")
_offs = [i for i, x in enumerate(sent) if x.type == "note_off" and x.channel == 8]
_clr = [i for i, x in enumerate(sent) if getattr(x, "control", None) == cfg.BIND_CLEAR_CC]
check(len(_offs) == 128 and _clr and max(_offs) < min(_clr),
      "unbind: notes released on the old channel (9) BEFORE the clear")
sent.clear()
ns["module_bind"]("drums", 5)
check(not any(x.type == "note_off" for x in sent),
      "drums (one-shots) get no note-off sweep on a move")
sent.clear()
ns["module_bind"]("sampler", 1)
check(not any(x.type == "note_off" for x in sent),
      "the sampler (notes = deck commands) gets no note-off sweep")
ns["arp"], ns["fire"] = None, None

# -- THE LEAK TEST: an unbound voice must never be mirrored -------------------
ns["_bindings"] = {"drums": 10}                  # poly is OUT of the fold
brain_state["synth_mirror_poly"] = True
sent.clear()
ns["_synth_mirror_on"]([60, 64], 100)
check(len(sent) == 0, "LEAK TEST: unbound poly is NOT mirrored (no DIN leak)")

ns["_bindings"] = {"poly": 8}                     # bound again
sent.clear()
ns["_synth_mirror_on"]([60], 100)
check(len(sent) == 1 and sent[0].channel == 7, "bound poly IS mirrored on its channel")


# ═══════════════════════════════════════════════════════════════════════════
# P1/P2 JOIN (slice 2) — ephemeral coupling CCs on chip_p1's channel
# ═══════════════════════════════════════════════════════════════════════════
# The brain's COUPLE_MODES must match the synth's list, or the CC72 bucket the
# brain sends decodes to a DIFFERENT mode in the synth.
check(list(cfg.COUPLE_MODES) == list(S.NES_COUPLE_MODES),
      "cfg.COUPLE_MODES matches synth NES_COUPLE_MODES (bucket round-trips)")

# CC72 round-trip: every mode the brain buckets must decode back to itself using
# the synth's own decode math (value * len // 128).
_MODES = list(cfg.COUPLE_MODES)
def _synth_decode72(v):
    return min(len(_MODES) - 1, v * len(_MODES) // 128)
_rt_ok = all(_synth_decode72(ns["_mm_join_mode_cc"](m)) == i
             for i, m in enumerate(_MODES))
check(_rt_ok, "every join mode buckets to a CC72 the synth decodes back to it")

# chip_p1 lives on ch3 now (moved above), drums on 10. Join must emit on chip_p1's
# LIVE channel, not a hardcoded one.
ns["_bindings"] = {"chip_p1": 3, "chip_p2": 6, "drums": 10}
sent.clear()
ns["module_join"](mode="detune")
_m72 = [x for x in sent if getattr(x, "control", None) == cfg.JOIN_MODE_CC]
check(len(_m72) == 1 and _m72[0].channel == 2,
      "join mode emits CC72 on chip_p1's LIVE channel (3 -> ch0 2)")
check(_synth_decode72(_m72[0].value) == _MODES.index("detune"),
      "the CC72 value decodes to the requested mode (detune)")
check(ns["_mm_join"]["mode"] == "detune", "join mode is mirrored in _mm_join state")

sent.clear()
ns["module_join"](detune=25)
_m73 = [x for x in sent if getattr(x, "control", None) == cfg.JOIN_DETUNE_CC]
check(len(_m73) == 1 and _m73[0].value == round(25 / 50.0 * 64.0) + 64,
      "join detune emits CC73 centered (25c -> 96)")
check(ns["_mm_join"]["detune"] == 25 and ns["_mm_join"]["mode"] == "detune",
      "only the provided field changes (mode survives a detune-only call)")

sent.clear()
ns["module_join"](spread=True)
_m76 = [x for x in sent if getattr(x, "control", None) == cfg.JOIN_SPREAD_CC]
check(len(_m76) == 1 and _m76[0].value == 127 and ns["_mm_join"]["spread"] is True,
      "join spread ON emits CC76=127")
sent.clear()
ns["module_join"](spread=False)
_m76 = [x for x in sent if getattr(x, "control", None) == cfg.JOIN_SPREAD_CC]
check(len(_m76) == 1 and _m76[0].value == 0 and ns["_mm_join"]["spread"] is False,
      "join spread OFF emits CC76=0")

# detune is clamped to +/-50c
sent.clear()
ns["module_join"](detune=999)
check(ns["_mm_join"]["detune"] == 50, "join detune clamps to +50c")

# unknown mode is rejected and does not mutate state
sent.clear()
ns["_mm_join"]["mode"] = "detune"
ns["module_join"](mode="bogus")
check(ns["_mm_join"]["mode"] == "detune" and len(sent) == 0,
      "unknown join mode rejected, no CC emitted, state unchanged")

# chip_p1 OUT of the fold: join is a no-op (nothing to drive), no CC leaks
ns["_bindings"] = {"drums": 10}                   # chip_p1 absent
sent.clear()
ns["module_join"](mode="unison")
check(len(sent) == 0, "join with chip_p1 out of the fold emits nothing")


# ═══════════════════════════════════════════════════════════════════════════
print(f"\n_test_module_bind: {PASS} passed, {FAIL} failed  (expected {EXPECTED_TOTAL})")
if PASS != EXPECTED_TOTAL and FAIL == 0:
    print(f"  !! total {PASS} != expected {EXPECTED_TOTAL} -- a group may not have run")
sys.exit(1 if (FAIL or PASS != EXPECTED_TOTAL) else 0)
