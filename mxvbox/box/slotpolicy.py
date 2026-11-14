# -*- coding: utf-8 -*-
"""Which clip goes into which RAM slot, and which one to show (Python 2.7 AND 3).

Used by the box player (orcflash.py), which owns its clip library on its own card
(owner 2026-10-29: "i like the idea of the box being able to handle itself"). Pure
functions of plain dicts, so _test_slotpolicy.py checks every decision without a box.

  library: [{id, bytes, blur, frames, name}]          (what is on the card)
  slots:   {slot: {id, bytes, blur, ready, shown, last}}  (what is in RAM)
  pool:    a set of clip ids, or None = everything (owner 2026-10-29: hand-picked
           "pools of similar things" picked live, plus a randomized everything list)
"""


def next_wanted(library, slots, pointer, priority=None, prefer=()):
    """(the library entry to load next or None, the new round-robin pointer).
    A cued clip (priority id) goes first, then a FAVOURITE (prefer: ids) not in RAM yet;
    otherwise the next one, in library order from the pointer, that no slot holds yet."""
    held = set(i["id"] for i in slots.values())
    if priority and priority not in held:
        for e in library:
            if e["id"] == priority:
                return e, pointer
    for e in library:
        if e["id"] in prefer and e["id"] not in held:
            return e, pointer
    n = len(library)
    for k in range(n):
        e = library[(pointer + k) % n]
        if e["id"] not in held:
            return e, (pointer + k + 1) % n
    return None, pointer


def choose_slot(slots, need, busy, budget, max_slots=16, now=0.0, min_age=0.0, keep=None):
    """Where a clip of `need` bytes goes: ("free", s) when it fits as it is, else
    ("evict", s) = the READY slot shown longest ago that is not on screen (busy), has
    been shown at least once and has been in RAM at least min_age s (its "t" = when it
    arrived); None = wait (nothing may be replaced yet). Without min_age the library
    rolled through RAM nonstop (a clip every 2-7 s, measured 2026-10-29).
    keep = the active pool's ids: a slot holding a clip OUTSIDE it goes first, at once
    (shown or not, any age), so a pool you pick fills RAM in seconds."""
    used = sum(i["bytes"] for i in slots.values())
    free = [s for s in range(max_slots) if s not in slots]
    if free and used + need <= budget:
        return "free", free[0]
    if keep is not None:
        out = [s for s, i in slots.items() if i["ready"] and s not in busy and i["id"] not in keep]
        if out:
            return "evict", min(out, key=lambda s: (slots[s]["last"], s))
    cand = [s for s, i in slots.items() if i["ready"] and s not in busy and i["shown"] >= 1
            and now - i.get("t", 0.0) >= min_age]
    if not cand:
        return None
    return "evict", min(cand, key=lambda s: (slots[s]["last"], s))


def next_in_order(order, slots, pos, exclude=()):
    """A PLAYLIST (2026-11-02, owner: "numbered playlists. Like pools, but not stuck on random"):
    order = its clip ids in your order, pos = the index played last (-1 = none yet).
    -> (slot to show or None, its index, the id to fetch next or None). The next clip after pos that
    is in RAM, wrapping round; a clip not in RAM yet is passed over this time (no stall) and the
    first such one AHEAD is the one to fetch, so the list fills in as it plays."""
    n = len(order)
    if not n:
        return None, pos, None
    by_id = dict((i["id"], s) for s, i in slots.items() if i["ready"] and s not in exclude)
    held = set(i["id"] for i in slots.values())
    fetch = None
    for k in range(1, n + 1):
        j = (pos + k) % n
        cid = order[j]
        if cid in by_id:
            if fetch is None:                    # (the one just after it, if it is missing)
                for k2 in range(1, n):
                    c2 = order[(j + k2) % n]
                    if c2 not in held:
                        fetch = c2
                        break
                    if c2 in by_id:
                        break
            return by_id[cid], j, fetch
        if fetch is None and cid not in held:
            fetch = cid
    return None, pos, fetch


def pick_clip(slots, exclude=(), pool=None, favs=(), never=()):
    """The READY slot to show: the least shown, then the one shown longest ago. With a
    pool, only its clips (while none of them is in RAM yet: anything, never a blank).
    A FAVOURITE counts its showings at half, so it comes up about twice as often; a
    NEVER clip is never shown (None when nothing else is in RAM: the picture stays)."""
    cand = [s for s, i in slots.items() if i["ready"] and s not in exclude and i["id"] not in never]
    if pool is not None:
        cand = [s for s in cand if slots[s]["id"] in pool] or cand
    if not cand:
        return None
    return min(cand, key=lambda s: (slots[s]["shown"] * (0.5 if slots[s]["id"] in favs else 1.0), slots[s]["last"], s))
