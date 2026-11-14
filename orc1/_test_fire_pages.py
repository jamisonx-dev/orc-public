#!/usr/bin/env python3
"""
The Fire's UNIVERSAL pages (orc1_fire_pages.py) + their brain hooks + the /control web side (2026-10-27).

    python3 -B _test_fire_pages.py          (PC or Pi; stdlib. Flask + AF_UNIX for the web part,
                                             gcc + the firmware's uni.c for the C cross-check:
                                             each missing piece turns its checks into SKIPPED, never fewer)

Covers:
  1. the page codec: the exact byte layout (pad / knob entries, layer bit, colour, type nibble,
     channel 1-16 -> 0-15), name rules, sorting, round trips, every refusal, the 14-bit split, the
     checksum (incl. wrap past 16383), chunking (24 + the rest), the 873-byte maximum, every frame;
  2. a CROSS-CHECK AGAINST THE FIRMWARE: uni.c (fw/fireseq on branch universal-mode) is compiled with a
     tiny harness, and its uni_check / uni_sum / uni_pad_enc / uni_knob_enc / uni_press /
     uni_chunk_check / uni_begin_check / constants / colour names are compared with this module.
     Source: $FIRE_UNI_SRC, else ../../fire_firmware/fw/fireseq, else ~/fire_fw/fw/fireseq;
  3. the write / read / clear / info state machine against a FAKE FIRE that behaves like main.c's
     uni_host_*: acks, busy retry, every failure code in words, out of order, timeouts, wrong sum,
     out-of-step done counts, read-back mismatch, stale replies, a page edited mid-read, the
     one-transfer-at-a-time lock, replies arriving on another thread, firmware too old / no Fire;
  4. the brain: orc1.py routes page replies to fire_pages BEFORE FireBarLink, _execute_cmd hands the
     FIRE_PAGE* actions to socket_cmd, FirePages is built on fire_bar_link; FireBarLink.host_send /
     link_version against a fake mido;
  5. the web: /api/fire_pages* relay exactly the right action through the real command socket to a
     fake brain, check a page before sending it, and /control carries the new card (no PORT, no
     ALT, no fader rows) with JS that talks to /api/fire_pages only.

SAFETY: HOME is a temp dir before anything is imported (the web's command socket lives inside it,
so nothing can reach the live brain); nothing here opens a MIDI port; the Fire is never touched.

Mutation-prove (see the report in the session): drop the busy retry, the done == offset check,
the read-length restart, the drain, the version gate, the sort, the channel - 1, the reply
hand-off order in orc1.py -> each turns a check red.
"""
import ast
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REAL_HOME = os.path.expanduser("~")
SANDBOX = tempfile.mkdtemp(prefix="orc_fire_pages_")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX

import orc1_fire_pages as F      # noqa: E402

EXPECTED = 89
passed = failed = skipped = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label} {extra}")


def skip(label, why):
    global passed, skipped
    passed += 1
    skipped += 1
    print(f"  [SKIPPED: {why}] {label}")


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except F.PageError as e:
        return str(e)
    return None


def src(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


rnd = random.Random(20261027)


def rand_page(full=False):
    pads, knobs = [], []
    for pad in range(64):
        for layer in (0, 1):
            if full or rnd.random() < 0.3:
                pads.append({"pad": pad, "layer": layer, "type": rnd.choice(F.PAD_TYPES),
                             "channel": rnd.randint(1, 16), "number": rnd.randint(0, 127),
                             "on": rnd.randint(0, 127), "off": rnd.randint(0, 127),
                             "colour": rnd.randint(0, 16)})
    for slot in range(16):
        if full or rnd.random() < 0.5:
            knobs.append({"slot": slot, "type": rnd.choice(F.KNOB_TYPES), "channel": rnd.randint(1, 16),
                          "cc": rnd.randint(0, 127), "start": rnd.randint(0, 127)})
    name = "".join(chr(rnd.randint(0x21, 0x7E)) for _ in range(rnd.randint(0, 8)))
    rnd.shuffle(pads)
    rnd.shuffle(knobs)
    return {"name": name, "pads": pads, "knobs": knobs}


def norm(p):
    return {"name": p["name"].rstrip(" "),
            "pads": sorted(p["pads"], key=lambda x: (x["pad"], x["layer"])),
            "knobs": sorted(p["knobs"], key=lambda x: x["slot"])}


# =============================================================== 1. codec
check("empty page = version 1 + 8 spaces (9 bytes)",
      F.encode_page({"name": "", "pads": [], "knobs": []}) == [1] + [32] * 8 == F.empty_page())

b = F.encode_page({"name": "DRUMS", "pads": [
    {"pad": 5, "layer": 0, "type": "note", "channel": 10, "number": 36, "on": 100, "off": 0, "colour": 3},
    {"pad": 5, "layer": 1, "type": "pc", "channel": 1, "number": 7, "on": 1, "off": 2, "colour": 16}],
    "knobs": [{"slot": 6, "type": "pb", "channel": 2, "cc": 0, "start": 64}]})
check("exact bytes: header, pad 5 BASE note ch10, pad 5 SHIFT prog change grey, knob slot 6 pitch bend ch2",
      b == [1, 68, 82, 85, 77, 83, 32, 32, 32,
            5, 0 | (3 << 1), (1 << 4) | 9, 36, 100, 0,
            5, 1 | (16 << 1), (6 << 4) | 0, 7, 1, 2,
            64 + 6, 2, 1, 0, 64, 0], b)
codes = [F.encode_page({"pads": [{"pad": 0, "type": t, "channel": 1}]})[11] >> 4 for t in F.PAD_TYPES]
check("pad type codes 1-6 in the high nibble (note, latch, CC hold, CC toggle, CC press, prog change)",
      codes == [1, 2, 3, 4, 5, 6], codes)
check("knob type codes: cc = 1, pb = 2",
      [F.encode_page({"knobs": [{"slot": 0, "type": t}]})[10] for t in ("cc", "pb")] == [1, 2])
check("channel 16 -> nibble 15, channel 1 -> 0",
      F.encode_page({"pads": [{"pad": 0, "type": "note", "channel": 16}]})[11] & 15 == 15
      and F.encode_page({"knobs": [{"slot": 0, "type": "cc", "channel": 1}]})[11] == 0)
check("layer 'shift' / 'SHIFT' / 1 all = the layer bit",
      all(F.encode_page({"pads": [{"pad": 3, "layer": l, "type": "note"}]})[10] & 1 == 1
          for l in ("shift", "SHIFT", 1)))

ok_rt = True
for _ in range(200):
    p = rand_page()
    if F.decode_page(F.encode_page(p)) != norm(p):
        ok_rt = False
        break
check("200 random pages round-trip encode -> decode", ok_rt)
p = rand_page()
q = dict(p, pads=list(reversed(p["pads"])), knobs=list(reversed(p["knobs"])))
check("entry order does not change the bytes (sorted: pads by pad + layer, then knobs)",
      F.encode_page(p) == F.encode_page(q))
full = rand_page(full=True)
fb = F.encode_page(full)
check("a full page (128 pad entries + 16 knobs) = 873 bytes, the maximum", len(fb) == 873 == F.MAX_LEN)
check("checksum = byte sum & 0x3FFF, and a full page wraps past 16383",
      F.page_sum(fb) == sum(fb) % 16384 and sum(fb) > 16383, (sum(fb),))
check("split14 / join14 round trip (0, 127, 128, 873, 16383); 16384 refused",
      all(F.join14(*F.split14(n)) == n for n in (0, 127, 128, 873, 16383))
      and F.split14(873) == [6, 105] and raises(F.split14, 16384) is not None)
ch = F.chunks(fb)
check("873 bytes = 37 chunks, 24 each except the last 9, offsets in order, nothing lost",
      len(ch) == 37 and all(len(c) == 24 for _, c in ch[:-1]) and len(ch[-1][1]) == 9
      and [o for o, _ in ch] == list(range(0, 873, 24)) and sum((c for _, c in ch), []) == fb)
check("9 bytes = 1 chunk; 33 bytes = 24 + 9",
      [len(c) for _, c in F.chunks([1] * 9)] == [9] and [len(c) for _, c in F.chunks([1] * 33)] == [24, 9])

bad_pages = {
    "a pad set twice": {"pads": [{"pad": 1, "type": "note"}, {"pad": 1, "type": "pc"}]},
    "pad 64": {"pads": [{"pad": 64, "type": "note"}]},
    "channel 0": {"pads": [{"pad": 1, "type": "note", "channel": 0}]},
    "channel 17": {"pads": [{"pad": 1, "type": "note", "channel": 17}]},
    "colour 17": {"pads": [{"pad": 1, "type": "note", "colour": 17}]},
    "unknown type": {"pads": [{"pad": 1, "type": "cc"}]},
    "number 128": {"pads": [{"pad": 1, "type": "note", "number": 128}]},
    "knob slot 16": {"knobs": [{"slot": 16, "type": "cc"}]},
    "knob set twice": {"knobs": [{"slot": 2, "type": "cc"}, {"slot": 2, "type": "pb"}]},
    "knob type": {"knobs": [{"slot": 2, "type": "nrpn"}]},
    "name of 9": {"name": "ABCDEFGHI"},
    "name not ascii": {"name": "café"},
    "a bool as a number": {"pads": [{"pad": True, "type": "note"}]},
    "2.5 as a number": {"pads": [{"pad": 2.5, "type": "note"}]},
    "not an object": ["x"],
}
errs = {k: raises(F.encode_page, v) for k, v in bad_pages.items()}
check("encode_page refuses each bad page with words", all(errs.values()),
      [k for k, v in errs.items() if not v])

E = F.empty_page()
bad_bytes = {
    "version 2": [2] + E[1:],
    "name byte 31": [1, 31] + E[2:],
    "pad type 0": E + [1, 0, 0x00, 1, 1, 1],
    "pad type 7": E + [1, 0, 0x70, 1, 1, 1],
    "colour 17": E + [1, 34, 0x10, 1, 1, 1],
    "knob type 0": E + [64, 0, 0, 1, 1, 0],
    "knob type 3": E + [64, 3, 0, 1, 1, 0],
    "knob channel 16": E + [64, 1, 16, 1, 1, 0],
    "knob byte 5 not 0": E + [64, 1, 0, 1, 1, 1],
    "entry 80": E + [80, 1, 0, 1, 1, 0],
    "byte 128": E + [1, 0, 0x10, 128, 1, 1],
    "duplicate key": E + [1, 0, 0x10, 1, 1, 1] + [1, 2, 0x20, 1, 1, 1],
    "length 10": E + [1],
    "length 879": [1] + [32] * 8 + [0] * 870,
}
errs = {k: raises(F.check_bytes, v) for k, v in bad_bytes.items()}
check("check_bytes refuses every byte-level fault uni_check refuses", all(errs.values()),
      [k for k, v in errs.items() if not v])
check("SHIFT entry for the same pad is NOT a duplicate", raises(
    F.check_bytes, E + [1, 0, 0x10, 1, 1, 1] + [1, 1, 0x10, 1, 1, 1]) is None)

check("frames: 10 / 11 / 12 / 13 / 14 / 15 byte for byte",
      F.frame_begin(3, 873) == [0x7D, 0x4F, 0x52, 0x43, 0x10, 3, 6, 105]
      and F.frame_chunk(24, [1, 2]) == [0x7D, 0x4F, 0x52, 0x43, 0x11, 0, 24, 1, 2]
      and F.frame_commit(16383) == [0x7D, 0x4F, 0x52, 0x43, 0x12, 127, 127]
      and F.frame_read(7, 130) == [0x7D, 0x4F, 0x52, 0x43, 0x13, 7, 1, 2]
      and F.frame_list() == [0x7D, 0x4F, 0x52, 0x43, 0x14]
      and F.frame_clear(2) == [0x7D, 0x4F, 0x52, 0x43, 0x15, 2])
lst = F.ORC_HDR + [0x14, 8, 32, 0, 0, 81, 127, 2, 1]
for pg in range(8):
    ln = 9 + 6 * pg
    lst += F.split14(ln) + F.split14(1000 + pg) + [ord(c) for c in f"PAGE{pg}".ljust(8)]
info = F.parse_list(lst)
check("parse_list: budget 4096, used 81, writing none, current 2, showing, per page len / sum / name / entries",
      info["budget"] == 4096 and info["used"] == 81 and info["writing"] is None and info["current"] == 2
      and info["showing"] is True and info["pages"][3] == {"page": 3, "len": 27, "sum": 1003,
                                                           "name": "PAGE3", "entries": 3}, info)
check("parse_list refuses a short reply", raises(F.parse_list, lst[:40]) is not None)
txt = [F.result_text(c) for c in range(9)]
check("every result code 0-8 has its own words; an unknown code says so",
      len(set(txt)) == 9 and all(txt) and "unknown" in F.result_text(42)
      and "budget" in txt[6] and "stop the Fire" in txt[7] and "busy" in txt[2])


# =============================================================== 2. firmware cross-check
HARNESS = r"""
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "uni.h"
int main(void) {
    char cmd[8];
    while (scanf("%7s", cmd) == 1) {
        if (!strcmp(cmd, "C")) {             /* C n b0 b1 ... : uni_check + uni_sum */
            int n; scanf("%d", &n);
            uint8_t *b = malloc(n + 1);
            for (int i = 0; i < n; i++) { int v; scanf("%d", &v); b[i] = (uint8_t)v; }
            printf("%d %d\n", uni_check(b, (uint32_t)n), uni_sum(b, (uint32_t)n));
            free(b);
        } else if (!strcmp(cmd, "P")) {      /* P pad layer type ch num on off col */
            int a[8]; for (int i = 0; i < 8; i++) scanf("%d", &a[i]);
            uni_pad_t p = {(uint8_t)a[2], (uint8_t)a[3], (uint8_t)a[4], (uint8_t)a[5], (uint8_t)a[6], (uint8_t)a[7]};
            uint8_t e[UNI_ENT]; uni_pad_enc(e, a[0], a[1], p);
            printf("%d %d %d %d %d %d\n", e[0], e[1], e[2], e[3], e[4], e[5]);
        } else if (!strcmp(cmd, "K")) {      /* K slot type ch cc init */
            int a[5]; for (int i = 0; i < 5; i++) scanf("%d", &a[i]);
            uni_knob_t k = {(uint8_t)a[1], (uint8_t)a[2], (uint8_t)a[3], (uint8_t)a[4]};
            uint8_t e[UNI_ENT]; uni_knob_enc(e, a[0], k);
            printf("%d %d %d %d %d %d\n", e[0], e[1], e[2], e[3], e[4], e[5]);
        } else if (!strcmp(cmd, "X")) {      /* X type : uni_press status / length / release */
            int t; scanf("%d", &t);
            uni_pad_t p = {(uint8_t)t, 2, 60, 100, 5, 0};
            uint8_t tog = 0, m[3] = {0, 0, 0}; int n = 0;
            int rel = uni_press(p, &tog, m, &n);
            printf("%d %d %d\n", m[0], n, rel);
        } else if (!strcmp(cmd, "H")) {      /* H off n done len : uni_chunk_check */
            unsigned a[4]; for (int i = 0; i < 4; i++) scanf("%u", &a[i]);
            printf("%d\n", uni_chunk_check(a[0], a[1], a[2], a[3]));
        } else if (!strcmp(cmd, "B")) {      /* B page len used : uni_begin_check */
            int pg; unsigned ln, us; scanf("%d %u %u", &pg, &ln, &us);
            printf("%d\n", uni_begin_check(pg, ln, us));
        } else if (!strcmp(cmd, "Z")) {
            printf("%d %d %d %d %d %d %d %u %d %d %d %d\n", UNI_PAGES, UNI_HDR, UNI_ENT, UNI_PADS, UNI_KNOBS,
                   UNI_MAX_LEN, UNI_CHUNK, UNI_BUDGET, UNI_VER, UNI_NAME, UNI_COL_GREY, UT_N);
            for (int i = 0; i < UNI_COLS; i++) printf("%s\n", UNI_COL_NAME[i]);
        }
        fflush(stdout);
    }
    return 0;
}
"""


def find_fw():
    cands = [os.environ.get("FIRE_UNI_SRC"),
             os.path.join(HERE, "..", "..", "fire_firmware", "fw", "fireseq"),
             os.path.join(REAL_HOME, "fire_fw", "fw", "fireseq")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "uni.c")) and os.path.isfile(os.path.join(c, "uni.h")):
            with open(os.path.join(c, "uni.h")) as f:
                if "UNI_MAX_LEN" in f.read():
                    return os.path.abspath(c)
    return None


FW_CHECKS = 9
fw = find_fw()
gcc = shutil.which("gcc") or shutil.which("cc")
harness = None
if fw and gcc:
    hd = tempfile.mkdtemp(prefix="uni_harness_", dir=SANDBOX)
    with open(os.path.join(hd, "h.c"), "w") as f:
        f.write(HARNESS)
    exe = os.path.join(hd, "h")
    r = subprocess.run([gcc, "-std=c99", "-O1", "-I", fw, os.path.join(hd, "h.c"), os.path.join(fw, "uni.c"),
                        "-o", exe], capture_output=True, text=True)
    if r.returncode == 0:
        harness = exe
    else:
        print("  harness build failed:", r.stderr[:400])

if harness is None:
    for i in range(FW_CHECKS):
        skip(f"firmware cross-check {i + 1}", "no gcc" if not gcc else "no fw/fireseq uni.c (universal-mode)")
else:
    print(f"  (firmware source: {fw})")

    def run(lines):
        out = subprocess.run([harness], input="\n".join(lines) + "\n", capture_output=True, text=True,
                             timeout=60).stdout
        return out.splitlines()

    z = run(["Z"])
    consts = list(map(int, z[0].split()))
    check("fw constants: 8 pages, header 9, entry 6, 64 pads, 16 knobs, max 873, chunk 24, budget 4096, "
          "version 1, name 8, grey 16, 7 pad types incl. off",
          consts == [F.PAGES, F.HDR, F.ENT, F.PADS, F.KNOBS, F.MAX_LEN, F.CHUNK, F.BUDGET, F.VER,
                     F.NAME_LEN, F.GREY, len(F.PAD_TYPES) + 1], consts)
    check("fw colour names = ours, in order", z[1:] == F.COLOURS, z[1:])

    good, broken = [], []
    for _ in range(150):
        good.append(F.encode_page(rand_page()))
    for k in bad_bytes:
        broken.append(bad_bytes[k])
    for _ in range(150):                     # random single-byte damage: both sides must agree
        g = list(rnd.choice(good))
        i = rnd.randrange(len(g))
        g[i] = rnd.randint(0, 127)
        broken.append(g)
    allp = good + broken
    res = run([f"C {len(x)} " + " ".join(map(str, x)) for x in allp])
    agree, sums = True, True
    for x, line in zip(allp, res):
        rc, s = map(int, line.split())
        ours = 0 if F.PageError and raises(F.check_bytes, x) is None else 5
        if rc != ours:
            agree = False
            print("   disagree:", x[:20], rc, ours)
            break
        if s != F.page_sum(x):
            sums = False
    check("uni_check and check_bytes agree on 150 good + 164 damaged pages", agree and len(res) == len(allp))
    check("uni_sum == page_sum on all of them", sums)

    lines, want = [], []
    for _ in range(100):
        pad, layer, t = rnd.randrange(64), rnd.randrange(2), rnd.randint(1, 6)
        chn, num, on, off, col = rnd.randint(1, 16), rnd.randint(0, 127), rnd.randint(0, 127), \
            rnd.randint(0, 127), rnd.randint(0, 16)
        lines.append(f"P {pad} {layer} {t} {chn - 1} {num} {on} {off} {col}")
        want.append(F.encode_page({"pads": [{"pad": pad, "layer": layer, "type": F.PAD_TYPES[t - 1],
                                             "channel": chn, "number": num, "on": on, "off": off,
                                             "colour": col}]})[9:])
        slot, kt, kc, cc, st = rnd.randrange(16), rnd.randint(1, 2), rnd.randint(1, 16), rnd.randint(0, 127), \
            rnd.randint(0, 127)
        lines.append(f"K {slot} {kt} {kc - 1} {cc} {st}")
        want.append(F.encode_page({"knobs": [{"slot": slot, "type": F.KNOB_TYPES[kt - 1], "channel": kc,
                                              "cc": cc, "start": st}]})[9:])
    got = [list(map(int, l.split())) for l in run(lines)]
    check("uni_pad_enc / uni_knob_enc bytes == ours for 100 random pads + 100 knobs", got == want)

    pr = [tuple(map(int, l.split())) for l in run([f"X {t}" for t in range(1, 7)])]
    # status (wire channel 2 = 0x?2), message length, release kind (1 note off, 2 CC off, 0 none)
    check("uni_press proves the type numbering: 1 note, 2 note latch, 3 CC hold, 4 CC toggle, 5 CC press, 6 PC",
          pr == [(0x92, 3, 1), (0x92, 3, 0), (0xB2, 3, 2), (0xB2, 3, 0), (0xB2, 3, 0), (0xC2, 2, 0)], pr)

    lines, ok = [], True
    for n in list(range(9, 874, 6)):
        done = 0
        for off, part in F.chunks([1] * n):
            lines.append(f"H {off} {len(part)} {done} {n}")
            done += len(part)
    res = run(lines)
    check("uni_chunk_check accepts every chunk we send, in order, for every page length 9-873",
          res and all(r == "0" for r in res), len(res))
    res = run(["H 24 24 0 873", "H 0 23 0 873", "H 0 24 0 20"])
    check("uni_chunk_check refuses a skipped offset (3) and a short middle chunk / overrun (1)",
          res == ["3", "1", "1"], res)
    res = run(["B 0 9 0", "B 8 9 0", "B 0 10 0", "B 0 873 3224", "B 0 873 3223", "B 0 874 0"])
    check("uni_begin_check: ok / bad page / bad length / over budget (6) / just fits / too long",
          res == ["0", "1", "1", "6", "0", "1"], res)


# =============================================================== 3. the state machine vs a fake Fire
class FakeFire:
    """main.c's uni_host_* in miniature: one write at a time, chunks in order, sum at commit."""
    NAME = "ORC FW seq:ORC FW seq MIDI 1 24:0"

    def __init__(self, fp=None, threaded=False):
        self.fp = fp
        self.threaded = threaded
        self.store = {p: F.empty_page() for p in range(8)}
        self.state, self.buf, self.len, self.page = 0, [], 0, 0
        self.sent = []
        self.begin_codes, self.clear_codes = [], []
        self.chunk_fault = {}            # chunk number -> result code, "drop", or "done"
        self.commit_code = None
        self.corrupt = False
        self.read_mutate = False
        self.len_change_reads = 0        # the next N reads at offset > 0 see a longer page
        self.chunk_no = 0
        self.extra = []                  # frames to deliver before the next reply (stale / unasked)
        self.extra_on_chunk = {}         # chunk number -> frames delivered just before its answer

    def reply(self, d):
        for x in self.extra:
            self._deliver(x)
        self.extra = []
        self._deliver(d)

    def _deliver(self, d):
        if self.threaded:
            threading.Thread(target=lambda: (time.sleep(0.003), self.fp.handle_sysex(d, self.NAME)),
                             daemon=True).start()
        else:
            self.fp.handle_sysex(d, self.NAME)

    def ack(self, cmd, res, page, done=None):
        done = len(self.buf) if done is None else done
        self.reply(F.ORC_HDR + [cmd, res, page & 0x7F] + F.split14(done))

    def send(self, d):
        d = list(d)
        self.sent.append(d)
        assert d[:4] == F.ORC_HDR
        c = d[4]
        if c == 0x10:
            pg, n = d[5], F.join14(d[6], d[7])
            if self.begin_codes:
                return self.ack(0x10, self.begin_codes.pop(0), pg, 0)
            self.state, self.buf, self.len, self.page, self.chunk_no = 0, [], 0, pg, 0
            if pg > 7 or not F.len_ok(n):
                return self.ack(0x10, 1, pg, 0)
            self.state, self.len = 1, n
            return self.ack(0x10, 0, pg, 0)
        if c == 0x11:
            off, data = F.join14(d[5], d[6]), d[7:]
            k = self.chunk_no
            self.extra = self.extra + self.extra_on_chunk.pop(k, [])
            self.chunk_no += 1
            f = self.chunk_fault.get(k)
            if f == "drop":
                return
            if isinstance(f, int):
                return self.ack(0x11, f, self.page)
            if self.state != 1 or off != len(self.buf):
                return self.ack(0x11, 3, self.page if self.state else 127)
            if not data or len(data) > 24 or off + len(data) > self.len or (len(data) != 24 and off + len(data) != self.len):
                return self.ack(0x11, 1, self.page)
            self.buf.extend(data)
            if f == "done":
                return self.ack(0x11, 0, self.page, len(self.buf) - 1)
            return self.ack(0x11, 0, self.page)
        if c == 0x12:
            s = F.join14(d[5], d[6])
            if self.state != 1 or len(self.buf) != self.len:
                return self.ack(0x12, 3, self.page)
            if self.commit_code is not None:
                self.state = 0
                return self.ack(0x12, self.commit_code, self.page)
            cand = list(self.buf)
            if self.corrupt:
                cand[-1] ^= 1
            self.state = 0
            if F.page_sum(cand) != s:
                return self.ack(0x12, 4, self.page)
            try:
                F.check_bytes(cand)
            except F.PageError:
                return self.ack(0x12, 5, self.page)
            self.store[self.page] = cand
            return self.ack(0x12, 0, self.page)
        if c == 0x13:
            pg, off = d[5], F.join14(d[6], d[7])
            data = list(self.store[pg])
            if self.read_mutate and len(data) > 9:
                data[-2] = (data[-2] + 1) & 0x7F
            if off > 0 and self.len_change_reads:
                self.len_change_reads -= 1
                data = data + [5, 0, 0x10, 1, 1, 1]
            n = len(data)
            if off > n:
                return self.reply(F.ORC_HDR + [0x13, 1, pg] + F.split14(n) + F.split14(off))
            part = data[off:off + 24]
            return self.reply(F.ORC_HDR + [0x13, 0, pg] + F.split14(n) + F.split14(off) + part)
        if c == 0x14:
            used = sum(len(v) for v in self.store.values() if len(v) > 9)
            out = F.ORC_HDR + [0x14, 8] + F.split14(4096) + F.split14(used) + [127, 1, 0]
            for pg in range(8):
                v = self.store[pg]
                out += F.split14(len(v)) + F.split14(F.page_sum(v)) + v[1:9]
            return self.reply(out)
        if c == 0x15:
            pg = d[5]
            if self.clear_codes:
                return self.ack(0x15, self.clear_codes.pop(0), pg, 0)
            self.store[pg] = F.empty_page()
            return self.ack(0x15, 0, pg, 9)


def make(version=(True, 3), threaded=False, **fake_kw):
    fake = FakeFire(threaded=threaded)
    logs = []
    fp = F.FirePages(send=fake.send, version=lambda: version, is_ours=lambda n: "orc fw seq" in (n or "").lower(),
                     log=logs.append)
    fp.REPLY_S, fp.BEGIN_S, fp.BUSY_S, fp.BUSY_WAIT_S, fp.LOCK_S = 0.3, 0.4, 0.25, 0.01, 0.05
    fake.fp = fp
    for k, v in fake_kw.items():
        setattr(fake, k, v)
    return fp, fake


def cmds(fake):
    return [d[4] for d in fake.sent]


for ver, word in (((False, None), "no Fire"), ((True, None), "hello"), ((True, 2), "too old")):
    fp, fake = make(version=ver)
    r = [fp.info(), fp.read(0), fp.write(0, {"name": "X"}), fp.clear(0)]
    check(f"version {ver}: every action refused with '{word}', nothing sent",
          all(not x["ok"] and word in x["error"] for x in r) and fake.sent == [], r[0])
fp, fake = make(version=(True, 2))
check("too old says which firmware is needed (v0.31 / version 3) and status() carries version 2",
      "v0.31" in fp.info()["error"] and fp.status()["version"] == 2 and fp.status()["ready"] is False)

fp, fake = make()
r = fp.info()
check("info: page list parsed (budget, current, 8 pages, names)",
      r["ok"] and r["budget"] == 4096 and r["current"] == 1 and len(r["pages"]) == 8, r)

fp, fake = make()
r = fp.write(4, full)
check("write a full page: begin, 37 chunks, commit, then a read-back of 37 chunks; stored = sent; verified",
      r["ok"] and r["verified"] and fake.store[4] == fb
      and cmds(fake) == [0x10] + [0x11] * 37 + [0x12] + [0x13] * 37, (r, cmds(fake)[:5]))
check("write returns the read-back page decoded (what the Fire now plays)",
      r["data"] == norm(full) and r["len"] == 873 and r["sum"] == F.page_sum(fb))
check("the begin frame carries page and length; the commit carries the 14-bit sum",
      fake.sent[0] == F.frame_begin(4, 873) and fake.sent[38] == F.frame_commit(F.page_sum(fb)))

fp, fake = make(begin_codes=[2, 2])
r = fp.write(1, {"name": "B"})
check("begin busy twice -> retried, then written",
      r["ok"] and r["verified"] and cmds(fake)[:3] == [0x10, 0x10, 0x10], cmds(fake))
fp, fake = make(begin_codes=[2] * 1000)
t0 = time.monotonic()
r = fp.write(1, {"name": "B"})
check("busy for good -> gives up after BUSY_S with 'busy' in words, no chunk sent",
      not r["ok"] and "busy" in r["error"] and r["step"] == "begin" and 0x11 not in cmds(fake)
      and time.monotonic() - t0 < 2, r)
for code, word in ((6, "budget"), (7, "stop the Fire"), (1, "bad arguments"), (8, "flash")):
    fp, fake = make(begin_codes=[code])
    r = fp.write(0, {"name": "Q"})
    check(f"begin answer {code} -> '{word}', step begin, nothing more sent",
          not r["ok"] and word in r["error"] and r.get("code") == code and cmds(fake) == [0x10], r)

fp, fake = make(chunk_fault={2: 3})
r = fp.write(0, full)
check("chunk answered 'out of order' -> stops there, in words, with how far it got",
      not r["ok"] and "out of order" in r["error"] and r["done"] == 48 and cmds(fake).count(0x11) == 3
      and 0x12 not in cmds(fake), r)
fp, fake = make(chunk_fault={1: 2})
r = fp.write(0, full)
check("a chunk answered busy is sent again (same offset) and the write completes",
      r["ok"] and fake.sent[2][5:7] == fake.sent[3][5:7] == F.split14(24), [d[5:7] for d in fake.sent[:4]])
fp, fake = make(chunk_fault={4: "drop"})
r = fp.write(0, full)
check("a chunk never answered -> 'stopped answering after 96 of 873 bytes', no commit",
      not r["ok"] and "after 96 of 873" in r["error"] and 0x12 not in cmds(fake), r)
fp, fake = make(chunk_fault={3: "done"})
r = fp.write(0, full)
check("a chunk ack whose done count is off -> 'out of step', no commit",
      not r["ok"] and "out of step" in r["error"] and 0x12 not in cmds(fake), r)
fp, fake = make(corrupt=True)
r = fp.write(0, full)
check("the Fire sees a wrong sum (4) -> 'wrong checksum', the old page stays",
      not r["ok"] and "checksum" in r["error"] and r["step"] == "commit" and fake.store[0] == F.empty_page(), r)
for code, word in ((7, "stop the Fire"), (8, "flash"), (5, "invalid")):
    fp, fake = make(commit_code=code)
    r = fp.write(0, {"name": "C"})
    check(f"commit answer {code} -> '{word}'", not r["ok"] and word in r["error"] and r["step"] == "commit", r)
fp, fake = make(read_mutate=True)
r = fp.write(0, full)
check("Fire says OK but the read-back differs -> not ok, verified False, says where",
      not r["ok"] and r["verified"] is False and "differs" in r["error"], r)

fp, fake = make()
fake.store[2] = fb
r = fp.read(2)
check("read reassembles a 37-chunk page and decodes it", r["ok"] and r["bytes"] == fb and r["data"] == norm(full))
fp, fake = make(len_change_reads=1)
fake.store[2] = fb
r = fp.read(2)
check("page length changes mid-read (edited on the Fire) -> starts over and returns one consistent page",
      r["ok"] and r["len"] == len(r["bytes"]) == 873 and cmds(fake).count(0x13) == 2 + 37, cmds(fake).count(0x13))
fp, fake = make(len_change_reads=100)
fake.store[2] = fb
r = fp.read(2)
check("a page that keeps changing -> gives up with words", not r["ok"] and "kept changing" in r["error"], r)
fp, fake = make()
fake.store[3] = [1] + [32] * 8 + [70, 0, 0, 0, 0, 0]       # knob type 0: the Fire would never store this
r = fp.read(3)
check("an undecodable page reads ok with data None and the reason", r["ok"] and r["data"] is None and r["invalid"], r)

fp, fake = make()
fp.handle_sysex(F.ORC_HDR + [0x10, 6, 0, 0, 0], FakeFire.NAME)      # a late "full" begin answer (old write)
fp.handle_sysex(F.ORC_HDR + [0x11, 0, 0, 0, 24], FakeFire.NAME)     # a stale ack from an old write
fp.handle_sysex(F.ORC_HDR + [0x13, 0, 0, 0, 9, 0, 0] + F.empty_page(), FakeFire.NAME)
r = fp.write(0, full)
check("stale replies left in the queue are dropped before a new transfer", r["ok"] and r["verified"], r)
fp, fake = make()
fake.extra = [F.ORC_HDR + [0x10, 6, 6, 0, 0]]                        # an answer for ANOTHER page first
r = fp.write(0, {"name": "P"})
check("a begin answer for another page is ignored (counted as stray)", r["ok"] and fp.stray >= 1, r)
fp, fake = make()
fake.extra_on_chunk = {0: [F.ORC_HDR + [0x15, 0, 0, 0, 24], F.ORC_HDR + [0x12, 0, 0, 0, 24]]}
r = fp.write(0, full)
check("while waiting for a chunk answer, answers to OTHER commands (15, 12) with the same numbers are ignored",
      r["ok"] and r["verified"], r)

fp, fake = make()
check("handle_sysex: a page reply from our Fire is consumed",
      fp.handle_sysex(F.ORC_HDR + [0x12, 0, 0, 0, 9], FakeFire.NAME) is True and fp._q.qsize() == 1)
check("handle_sysex: not our port / a bar message (0F) / a short frame / another vendor -> not consumed",
      fp.handle_sysex(F.ORC_HDR + [0x12, 0, 0, 0, 9], "FL STUDIO FIRE") is False
      and fp.handle_sysex(F.ORC_HDR + [0x0F, 1, 127, 3], FakeFire.NAME) is False
      and fp.handle_sysex(F.ORC_HDR, FakeFire.NAME) is False
      and fp.handle_sysex([0x00, 0x20, 0x6B, 0x7F, 0x42, 0x12], FakeFire.NAME) is False)

fp, fake = make(threaded=True)
r = fp.write(6, full)
check("replies arriving on another thread (like the brain's MIDI input thread) work",
      r["ok"] and r["verified"] and fake.store[6] == fb, r)

fp, fake = make()
gate, inside = threading.Event(), threading.Event()
orig = fake.send


def slow(d):
    if d[4] == 0x10:
        inside.set()
        gate.wait(2)
    orig(d)


fake.send = fp.send = slow
out = {}
th = threading.Thread(target=lambda: out.setdefault("a", fp.write(0, {"name": "A"})))
th.start()
inside.wait(2)
second = fp.write(1, {"name": "B"})
gate.set()
th.join(3)
check("one transfer at a time: a second write while one runs -> 'busy', the first completes",
      not second["ok"] and second.get("busy") and out.get("a", {}).get("ok"), (second, out))

fp, fake = make()
fake.store[5] = fb
r = fp.clear(5)
check("clear: the page is empty on the Fire, checked by a read-back", r["ok"] and r["verified"]
      and fake.store[5] == F.empty_page() and cmds(fake) == [0x15, 0x13], (r, cmds(fake)))
fp, fake = make(clear_codes=[2, 2])
r = fp.clear(5)
check("clear busy -> retried", r["ok"] and cmds(fake)[:3] == [0x15, 0x15, 0x15], cmds(fake))
fp, fake = make(clear_codes=[7])
r = fp.clear(5)
check("clear refused while playing (7) -> in words", not r["ok"] and "stop the Fire" in r["error"], r)

fp, fake = make()
r = F.socket_cmd(fp, {"action": "FIRE_PAGE_WRITE", "page": 2, "data": {"name": "SOCK", "pads": [
    {"pad": 0, "type": "cc_toggle", "channel": 3, "number": 20, "on": 127, "off": 0, "colour": 5}]}})
check("socket_cmd FIRE_PAGE_WRITE writes the page dict", r["ok"] and r["verified"] and fake.store[2][1:5] == [83, 79, 67, 75])
r = F.socket_cmd(fp, {"action": "FIRE_PAGE_READ", "page": 2})
check("socket_cmd FIRE_PAGE_READ", r["ok"] and r["data"]["pads"][0]["type"] == "cc_toggle")
r = F.socket_cmd(fp, {"action": "FIRE_PAGES_INFO"})
check("socket_cmd FIRE_PAGES_INFO carries the link status too", r["ok"] and r["connected"] and r["version"] == 3)
r = F.socket_cmd(fp, {"action": "FIRE_PAGE_CLEAR", "page": 2})
check("socket_cmd FIRE_PAGE_CLEAR", r["ok"] and fake.store[2] == F.empty_page())
r = [F.socket_cmd(fp, {"action": "FIRE_PAGE_READ", "page": 8}),
     F.socket_cmd(fp, {"action": "FIRE_PAGE_WRITE", "page": 0, "data": {"pads": [{"pad": 0, "type": "x"}]}}),
     F.socket_cmd(fp, {"action": "FIRE_PAGE_NOPE"})]
check("socket_cmd: page 8, a bad page, an unknown action -> ok false with words",
      all(not x["ok"] and x["error"] for x in r), r)


# =============================================================== 4. the brain
brain = src("orc1.py")
rm = re.search(r"def _route_message\(.*?\n(?=def )", brain, re.S).group(0)
i_pages = rm.find("fire_pages.handle_sysex(msg.data, source_name)")
i_bar = rm.find("fire_bar_link.handle_sysex(msg.data, source_name)")
check("orc1._route_message hands SysEx to fire_pages BEFORE fire_bar_link (which swallows ORC frames)",
      0 < i_pages < i_bar, (i_pages, i_bar))
check("orc1 builds FirePages on fire_bar_link's port, version and port test",
      re.search(r"fire_pages = _orc1_fire_pages\.FirePages\(\s*send=fire_bar_link\.host_send, "
                r"version=fire_bar_link\.link_version,\s*is_ours=fire_bar_link\.is_ours", brain) is not None)
check("fire_pages is created after fire_bar_link (module order)",
      0 < brain.find("fire_bar_link = _orc1_scenes.FireBarLink(") < brain.find("fire_pages = _orc1_fire_pages.FirePages("))

tree = ast.parse(brain)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_execute_cmd")
code = ast.get_source_segment(brain, fn)
seen = []
stub = type("M", (), {"socket_cmd": staticmethod(lambda pages, c: seen.append((pages, c["action"])) or
                                                 {"ok": True, "via": c["action"]})})
ns = {"_orc1_fire_pages": stub, "fire_pages": "FP", "print_status": lambda *a: None,
      "_scene_cmd": lambda c: {"ok": True}, "state": {"key_lock": False}}
exec(code, ns)
acts = ["FIRE_PAGES_INFO", "FIRE_PAGE_READ", "FIRE_PAGE_WRITE", "FIRE_PAGE_CLEAR"]
check("_execute_cmd returns socket_cmd's reply for every FIRE_PAGE* action (the socket sends it back)",
      all(ns["_execute_cmd"]({"action": a}) == {"ok": True, "via": a} for a in acts)
      and seen == [("FP", a) for a in acts])

import orc1_scenes as S   # noqa: E402


class FakeOut:
    def __init__(self):
        self.msgs = []

    def send(self, m):
        self.msgs.append(m)

    def close(self):
        pass


class FakeMido:
    def __init__(self):
        self.names = []
        self.out = FakeOut()

    def get_output_names(self):
        return self.names

    def open_output(self, n):
        return self.out

    class Message:
        def __init__(self, kind, data):
            self.type, self.data = kind, data


fm = FakeMido()
link = S.FireBarLink(fm, idents=lambda: {}, log=lambda *a: None)
e = None
try:
    link.host_send(F.frame_list())
except RuntimeError as ex:
    e = str(ex)
check("FireBarLink.host_send with no Fire -> raises; link_version = (False, None)",
      e and "FLAME seq / ORC FW seq" in e and link.link_version() == (False, None))
fm.names = ["ORC FW seq:ORC FW seq MIDI 1 24:0"]
link.poll()
link.handle_sysex(S.ORC_HDR + [0x00, 3], "ORC FW seq:ORC FW seq MIDI 1 24:0")
fm.out.msgs.clear()
link.host_send(F.frame_list())
check("FireBarLink.host_send sends one sysex on the seq port; link_version = (True, 3)",
      [(m.type, list(m.data)) for m in fm.out.msgs] == [("sysex", F.frame_list())]
      and link.link_version() == (True, 3))
check("FireBarLink.is_ours (FirePages' port test) matches the seq port only",
      link.is_ours("ORC FW seq:ORC FW seq MIDI 1 24:0") and not link.is_ours("FL STUDIO FIRE:x"))


# =============================================================== 5. the web
WEB_CHECKS = 11
try:
    import flask    # noqa: F401
    have_flask = hasattr(socket, "AF_UNIX")
except ImportError:
    have_flask = False

if not have_flask:
    for i in range(WEB_CHECKS):
        skip(f"web check {i + 1}", "no flask / AF_UNIX")
else:
    import orc1_web as W    # noqa: E402
    check("web: the command socket is inside the sandbox", W.CMD_SOCK.startswith(SANDBOX), W.CMD_SOCK)
    client = W.app.test_client()

    r = client.get("/api/fire_pages").get_json()           # no brain listening yet
    check("web: GET /api/fire_pages with the ORC down -> ok false + meta (17 colours, 6 types, 8 pages)",
          r["ok"] is False and "not running" in r["error"] and len(r["meta"]["colours"]) == 17
          and [t["key"] for t in r["meta"]["pad_types"]] == F.PAD_TYPES and r["meta"]["pages"] == 8, r)

    got, replies = [], queue.Queue()

    def brain_loop(srv):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            buf = b""
            conn.settimeout(3)
            while True:
                c = conn.recv(4096)
                if not c:
                    break
                buf += c
            got.append(json.loads(buf.split(b"\n")[0]))
            try:
                rep = replies.get_nowait()
            except queue.Empty:
                rep = {"ok": True}
            conn.sendall((json.dumps(rep) + "\n").encode())
            conn.close()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(W.CMD_SOCK)
    srv.listen(4)
    threading.Thread(target=brain_loop, args=(srv,), daemon=True).start()

    replies.put({"ok": True, "connected": True, "version": 3, "pages": [], "budget": 4096, "used": 0})
    r = client.get("/api/fire_pages").get_json()
    check("web: GET /api/fire_pages relays FIRE_PAGES_INFO and adds meta",
          got[-1] == {"action": "FIRE_PAGES_INFO"} and r["ok"] and r["version"] == 3 and "meta" in r, r)
    r = client.get("/api/fire_pages/3").get_json()
    check("web: GET /api/fire_pages/3 -> FIRE_PAGE_READ page 3", got[-1] == {"action": "FIRE_PAGE_READ", "page": 3})
    rr = client.get("/api/fire_pages/8")
    check("web: page 8 -> 400, nothing sent", rr.status_code == 400 and got[-1]["action"] == "FIRE_PAGE_READ")
    n0 = len(got)
    page = {"name": "WEB", "pads": [{"pad": 1, "layer": 1, "type": "cc_hold", "channel": 4, "number": 64,
                                     "on": 127, "off": 0, "colour": 2}],
            "knobs": [{"slot": 15, "type": "pb", "channel": 1, "cc": 0, "start": 64}]}
    replies.put({"ok": True, "verified": True})
    r = client.post("/api/fire_pages/2", json=page).get_json()
    check("web: POST a page -> FIRE_PAGE_WRITE with the page dict, the reply relayed",
          got[-1] == {"action": "FIRE_PAGE_WRITE", "page": 2, "data": page} and r["verified"], got[-1])
    bad = dict(page, pads=[dict(page["pads"][0], channel=17)])
    rr = client.post("/api/fire_pages/2", json=bad)
    check("web: a bad page (channel 17) -> 400 with words, never reaches the brain",
          rr.status_code == 400 and "channel" in rr.get_json()["error"] and len(got) == n0 + 1)
    rr = client.post("/api/fire_pages/2", data="nope", content_type="application/json")
    check("web: a body that is not JSON -> 400", rr.status_code == 400 and len(got) == n0 + 1)
    client.post("/api/fire_pages/6/clear")
    check("web: POST clear -> FIRE_PAGE_CLEAR page 6", got[-1] == {"action": "FIRE_PAGE_CLEAR", "page": 6})

    html = client.get("/control").get_data(as_text=True)
    card = html[html.find('id="universal-card"'):html.find("<!-- KNOB MAP")]
    check("web: /control has the FIRE UNIVERSAL PAGES card (status, name, BASE/SHIFT, SEND TO FIRE, knobs) and "
          "no PORT, no ALT, no fader rows; the scope note names the ORC Fire firmware",
          "FIRE UNIVERSAL PAGES" in card and 'id="fup-status"' in card and 'id="fup-name"' in card
          and "ucs-tab-shift" in card and "SEND TO FIRE" in card and 'id="fup-kgrid"' in card
          and "ucs-port" not in html and "ucs-tab-alt" not in html and "uk-fader" not in html
          and "km-bank" not in html and "ORC Fire firmware" in html[:html.find('id="universal-card"')], card[:200])
    js = src("static/js/control.js")
    check("web: control.js talks to /api/fire_pages only (no /api/universal_*), hides the old 'universal' view",
          "/api/fire_pages" in js and "/api/universal_" not in js and "v !== 'universal'" in js)
    srv.close()

os.environ["HOME"] = os.environ["USERPROFILE"] = REAL_HOME
shutil.rmtree(SANDBOX, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed" + (f"  ({skipped} SKIPPED)" if skipped else ""))
if passed + failed != EXPECTED:
    print(f"EXPECTED {EXPECTED} checks, ran {passed + failed}: a check was added or removed")
    failed += 1
sys.exit(1 if failed else 0)
