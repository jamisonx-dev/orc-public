# -*- coding: utf-8 -*-
"""_test_slotpolicy.py -- the box's slot decisions (Python 2.7 on the box, or 3 anywhere).

    python _test_slotpolicy.py
"""
from __future__ import print_function
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slotpolicy as SP                 # noqa: E402

EXPECTED = 27
res = {"p": 0, "f": 0}


def check(cond, label, extra=""):
    if cond:
        res["p"] += 1
    else:
        res["f"] += 1
        print("  FAIL:", label, extra)


def slot(cid, nbytes=100, ready=True, shown=0, last=0.0):
    return {"id": cid, "bytes": nbytes, "blur": False, "ready": ready, "shown": shown, "last": last}


LIB = [{"id": c, "bytes": 100, "blur": False} for c in ("a", "b", "c", "d")]
e, p = SP.next_wanted(LIB, {0: slot("a"), 1: slot("b")}, 0)
check(e["id"] == "c" and p == 3, "next_wanted: the next clip in order not in RAM", (e, p))
e, p = SP.next_wanted(LIB, {0: slot("a")}, 3)
check(e["id"] == "d" and p == 0, "...round robin from the pointer, wrapping")
e, p = SP.next_wanted(LIB, {0: slot("a")}, 0, priority="d")
check(e["id"] == "d" and p == 0, "a cued clip goes first and leaves the pointer alone")
e, p = SP.next_wanted(LIB, dict((i, slot(c)) for i, c in enumerate("abcd")), 1)
check(e is None and p == 1, "everything in RAM: nothing to load")
check(SP.choose_slot({0: slot("a"), 2: slot("b")}, 100, set(), 1000) == ("free", 1), "room left: the lowest free slot")
full = {0: slot("a", 400, shown=2, last=5.0), 1: slot("b", 400, shown=1, last=2.0), 2: slot("c", 150, shown=0)}
check(SP.choose_slot(full, 100, set(), 1000) == ("evict", 1), "full: replace the slot shown longest ago")
check(SP.choose_slot(full, 100, set([1]), 1000) == ("evict", 0), "...never one on screen (busy)")
check(SP.choose_slot({0: slot("a", 900, shown=0)}, 200, set(), 1000) is None, "...never one not shown yet: wait")
check(SP.choose_slot({0: slot("a", 900, shown=3, ready=False)}, 200, set(), 1000) is None, "...never one still arriving")
young = {0: dict(slot("a", 900, shown=3), t=100.0)}
check(SP.choose_slot(young, 200, set(), 1000, now=130.0, min_age=60.0) is None,
      "...never one in RAM less than min_age (no nonstop rolling)")
check(SP.choose_slot(young, 200, set(), 1000, now=161.0, min_age=60.0) == ("evict", 0),
      "...and it can go once it has been there min_age")
check(SP.choose_slot(dict((s, slot("x", 1, shown=1)) for s in range(16)), 1, set(), 10 ** 9) == ("evict", 0),
      "16 slots in use: replace even when the bytes would fit")
sl = {0: slot("a", shown=2), 1: slot("b", shown=1, last=9.0), 2: slot("c", shown=1, last=3.0), 3: slot("d", ready=False)}
check(SP.pick_clip(sl) == 2, "pick: least shown, then shown longest ago")
check(SP.pick_clip(sl, exclude=set([2])) == 1, "...excluding the one already on screen")
check(SP.pick_clip({0: slot("a", ready=False)}) is None, "nothing ready: None")
check(SP.pick_clip(sl, pool=set(["a", "b"])) == 1, "pool: only its clips (b: least shown of a/b)")
check(SP.pick_clip(sl, pool=set(["zz"])) == 2, "pool with nothing in RAM yet: anything, never a blank screen")
mixed = {0: slot("a", 400, shown=0, last=1.0), 1: dict(slot("b", 400, shown=5), t=99.0)}
check(SP.choose_slot(mixed, 300, set(), 1000, now=100.0, min_age=60.0, keep=set(["b", "c"])) == ("evict", 0),
      "pool picked: a clip outside it goes at once (even unshown, any age)")
check(SP.choose_slot(mixed, 300, set([0]), 1000, now=100.0, min_age=60.0, keep=set(["b", "c"])) is None,
      "...but never the one on screen, and pool clips keep min_age")
# FAVOURITES / NEVER (2026-11-02, the BOX tab's library controls)
e, p = SP.next_wanted(LIB, {0: slot("a")}, 1, prefer=set(["d"]))
check(e["id"] == "d" and p == 1, "next_wanted: a favourite not in RAM is fetched before the round robin", (e, p))
e, p = SP.next_wanted(LIB, {0: slot("a"), 1: slot("d")}, 1, prefer=set(["d"]))
check(e["id"] == "b", "...and once it is in RAM, the round robin goes on", e)
fav = {0: slot("a", shown=3, last=1.0), 1: slot("b", shown=2, last=1.0)}
check(SP.pick_clip(fav, favs=set(["a"])) == 0 and SP.pick_clip(fav) == 1,
      "pick: a favourite's showings count half (shown 3 = 1.5 < 2): it comes up about twice as often")
check(SP.pick_clip(sl, never=set(["c"])) == 1 and SP.pick_clip({0: slot("a")}, never=set(["a"])) is None,
      "pick: a NEVER clip is never shown, even when it is all there is (the picture stays)")
# PLAYLISTS: in your order, wrapping, never stuck
ram = {0: slot("c"), 1: slot("a"), 2: slot("b")}
order = ["a", "b", "c"]
seq, pos = [], -1
for _ in range(5):
    s_, pos, f_ = SP.next_in_order(order, ram, pos)
    seq.append(ram[s_]["id"])
check(seq == ["a", "b", "c", "a", "b"], "playlist: in YOUR order, then round again", seq)
s_, j_, f_ = SP.next_in_order(["a", "x", "b"], ram, 0)
check(ram[s_]["id"] == "b" and j_ == 2 and f_ == "x",
      "a clip not in RAM yet is passed over this time (no stall) and fetched for next time", (s_, j_, f_))
s_, j_, f_ = SP.next_in_order(["a", "b", "y"], ram, 0)
check(ram[s_]["id"] == "b" and f_ == "y", "...and the missing one just AHEAD is fetched before it is due", f_)
s_, j_, f_ = SP.next_in_order(["x", "y"], ram, -1)
check(s_ is None and f_ == "x" and SP.next_in_order([], ram, 3) == (None, 3, None),
      "nothing of the playlist in RAM: no pick (the picture stays), the first one fetched; empty: nothing")

total = res["p"] + res["f"]
print("%d passed, %d failed  (expected %d)" % (res["p"], res["f"], EXPECTED))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
sys.exit(0 if res["f"] == 0 and total == EXPECTED else 1)
