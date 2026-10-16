#!/usr/bin/env python3
"""_test_cme.py -- orc1_cme: the CME U6MIDI Pro config link (2026-09-30).

Codec checks use bytes captured off OUR unit (USBPcap of UxMIDI Tools, and the
Pi's first dump) plus PatchForge's published examples. The link runs against a
FakeBox that speaks the measured protocol: hello -> identity, read -> value or
7C, write -> ack 53 + address; four presets, each with its own router, and
every section reading the ACTIVE preset. No hardware, no real files.

Mutation-prove (each turns checks red):
  - set_preset: write the whole record from scratch (drop `+ g[1:]`) -> the
    "only byte 0 changes" check
  - frame(): allow opcode 00 -> the "never control" check
  - word_decode: swap byte order -> the name / word checks
  - _on_msg: queue every message -> the "music dropped" check
  - set_preset: skip the read-back -> the lying-box check
  - filter_ty_encode: zero bytes 3-4 instead of keeping them -> the unknown-bits checks
  - ch_pos: put ch 1-2 in byte 2 -> the channel table check
  - _exchange: drop the 7E/7C early return -> the never-resent check
  - mapper_encode: drop the base unknown bits -> the unknown-bits check
  - mapper_validate: drop the transpose rule -> the app-identical corpus check
  - set_mapper: skip the read-back -> the lying-box mapper check
  (2026-10-01 review fixes, test_review_fixes / test_brain_lifecycle:)
  - refresh: drop `partial = True` -> the partial-read check
  - _expect_preset: return at once -> the preset-race checks
  - set_route / set_filter: refresh after the try, not in `finally` -> the stale-page checks
  - read: drop addr= -> the stale-reply check
  - _start_worker: drop the lock / CmeLink: don't start it in __init__ -> the worker checks
  - frame: mask with & 0x7F again -> the codec range check
  - mapper_validate: drop the non-dict guards -> the malformed-mapper check
  - _cme_scan: drop the `again` (replug) set -> the lifecycle check
  - make_usb_handler: drop the is_config_frame skip -> the DIN-leak check
  - cme.js full(): drop `!st.partial` -> the page guard check
"""
import ast
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orc1_cme as C  # noqa: E402

EXPECTED = 100
_res = []


def check(name, ok, detail=""):
    _res.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def B(h):
    return list(bytes.fromhex(h.replace(" ", "")))


# real bytes from our unit (connect_frames.tsv / cme_backup_20260930_first.json)
IDENT = B("00 13 00 07 06 01 02 70 52 0A 00 04 47 10 16 02 0F 05 12 00 00")
NAMES = ["03 57 4C 1C 28 01 03 04 5D 32 00 00 00 00 31 00 00 00 00 00",
         "03 57 4C 1C 28 02 03 04 5D 32 00 00 00 00 00 00 00 00 00 00",
         "03 57 4C 1C 28 03 03 04 5D 32 00 00 00 00 00 00 00 00 00 00",
         "03 57 4C 1C 28 04 03 04 5D 32 00 00 00 00 00 00 00 00 00 00"]
DEVICE = ("09 54 49 4D 2A 00 05 24 12 22 00 00 60 1B 39 07 00 40 7E 7A "
          "00 70 07 40 3F 00 70 0F 40 3F")
# curve 1 exactly as the Pi read it off our unit (stock "x2 then clamp")
CURVE1_RAW = B("06 40 40 00 00 0E 40 41 02 04 05 31 22 44 07 0C 21 23 46 0B 03 12 04 08 0F "
               "0A 02 45 49 12 00 73 25 4B 15 06 53 66 0C 19 0C 33 27 0E 1C 02 04 68 0F 1F "
               "07 64 28 51 21 0C 34 49 52 24 01 05 6A 13 27 06 55 0A 15 29 0A 15 0B 56 2B "
               "0F 55 0B 57 2D 02 16 0C 18 30 06 56 0C 59 31 09 16 0D 5A 33 0D 46 6D 1A 35 "
               "00 77 4D 5B 36 02 27 2E 1C 38 05 47 6E 5C 39 07 67 4E 5D 3A 09 07 0F 5E 3B "
               "0A 27 4F 5E 3C 0C 37 6F 5E 3D 0D 57 0F 1F 3E 0E 67 2F 5F 3E 0E 67 4F 1F 3F "
               "0F 77 6F 5F 3F 0F 77 6F 5F 3F")
# Preset 1b mapper 1 as read off his box: transpose mode with a leftover +79 st
P1_M1 = B("00 70 0F 40 3F 00 70 0F 40 3F 07 70 00 00 00 03 00 00 00 2D")
FACTORY_ROUTER = [[0, 1], [0, 2], [0, 4], [1, 0], [2, 0], [4, 0]]
P1_ROUTER = [[0, 1], [0, 2], [0, 4], [1, 1], [2, 0], [4, 6]]


# ── fake box + fake mido ─────────────────────────────────────────────────────

class Msg:
    def __init__(self, type, data=(), **kw):
        self.type, self.data = type, tuple(data)


class FakeBox:
    def __init__(self, silent=False):
        self.silent = silent
        self.lie = False                   # ack writes but don't apply them
        self.sent = []                     # every request frame (data bytes)
        self.cb = None
        self.preset = 1                    # 0-based: preset 2, as his box was
        self.settings = [0x30, 0x08, 0x00, 0x00]
        self.routers = [list(map(list, P1_ROUTER))] + [list(map(list, FACTORY_ROUTER))
                                                       for _ in range(3)]
        self.fch = [[[0, 0, 0] for _ in range(12)] for _ in range(4)]
        self.fty = [[[0, 0, 0, 0, 0] for _ in range(12)] for _ in range(4)]
        self.fty[1][4] = [0, 0, 0, 0x11, 0x22]     # unknown bytes set: must survive edits
        self.rejected = 0
        self.mappers = [[B(C.DEFAULT_MAPPER) for _ in range(16)] for _ in range(4)]
        self.curves = [list(CURVE1_RAW) for _ in range(4)]
        self.fail_read_sec = None          # never answer reads of this section
        self.drop_ack = False              # apply writes, but the ack is lost
        self.reject_types = False          # refuse (7E) every type-filter write
        self.stale_next = False            # next read: a stale reply for another address first
        self.on_write = None               # hook(sec, idx) before a write is applied

    def reply(self, op, payload):
        if self.cb and not self.silent:
            self.cb(Msg("sysex", C.HDR + [op] + payload + [C.checksum(op, payload)]))

    def recv(self, data):
        self.sent.append(list(data))
        op, payload = C.parse(data)
        if op == 0x01:
            return self.reply(0x51, IDENT)
        sec, words, idx = payload[:3]
        if op == 0x02 and sec == self.fail_read_sec:
            return None
        if op == 0x02 and self.stale_next:
            self.stale_next = False        # a late answer to an earlier read, then the real one
            self.reply(0x52, [0x00, 1, 5] + list(self.routers[self.preset][5]))
        if op == 0x03 and self.on_write:
            self.on_write(sec, idx)
        if op == 0x03 and sec == 0x02 and self.reject_types:
            self.rejected += 1
            return self.reply(0x53, [0x7E])
        if op == 0x02:
            v = self.value(sec, words, idx)
            if v is None:
                return self.reply(0x52, [C.ERR])
            return self.reply(0x52, [sec, words, idx] + v)
        if op == 0x03:
            v = payload[3:]
            if self.lie:
                pass
            elif (sec, words, idx) == (0x04, 1, 0):
                self.preset, self.settings = v[0], v[1:]
            elif sec == 0x00 and words == 1 and idx < 6:
                self.routers[self.preset][idx] = v[:2]
            elif sec == 0x01 and idx < 12:
                if len(v) != 3 or v[0] & ~0x03:       # only 16 channel bits exist (measured)
                    self.rejected += 1
                    return self.reply(0x53, [0x7E])
                self.fch[self.preset][idx] = v
            elif sec == 0x03 and words == 4 and idx < 16:
                self.mappers[self.preset][idx] = v
            elif sec == 0x02 and idx < 12:
                if len(v) != 5 or v[0] & ~0x0F:       # byte 0 has 4 type bits (measured)
                    self.rejected += 1
                    return self.reply(0x53, [0x7E])
                self.fty[self.preset][idx] = v
            if self.drop_ack:
                return None
            return self.reply(0x53, [sec, words, idx])

    def value(self, sec, words, idx):
        if words == 0:
            return None
        if sec == 0x04 and idx == 0:
            return [self.preset] + self.settings
        if sec == 0x00 and idx < 6:
            return list(self.routers[self.preset][idx])
        if sec == 0x01 and idx < 12:
            return list(self.fch[self.preset][idx])
        if sec == 0x02 and idx < 12:
            return list(self.fty[self.preset][idx])
        if sec == 0x03 and idx < 16:
            return list(self.mappers[self.preset][idx])
        if sec == 0x0B and words == 32 and idx < 4:
            return list(self.curves[idx])
        if sec == 0x07 and idx < 4:
            return B(NAMES[idx])
        if sec == 0x0D and idx < 1:
            return B(DEVICE)
        return None


class FakeOut:
    def __init__(self, box):
        self.box = box

    def send(self, m):
        self.box.recv(list(m.data))

    def close(self):
        pass


class FakeIn:
    def close(self):
        pass


class FakeMido:
    Message = Msg
    P1 = "U6MIDI Pro:U6MIDI Pro Port 1 40:0"

    def __init__(self, box):
        self.box = box

    def get_output_names(self):
        return [self.P1, "pisound:pisound MIDI PS 28:0"]

    def get_input_names(self):
        return self.get_output_names()

    def open_output(self, name):
        return FakeOut(self.box)

    def open_input(self, name, callback=None):
        self.box.cb = callback
        return FakeIn()


def link(box, tmp):
    return C.CmeLink(state_path=os.path.join(tmp, "cme.json"), mido_mod=FakeMido(box),
                     timeout=0.2, log=lambda *a: None)


# ── tests ────────────────────────────────────────────────────────────────────

def test_codec():
    check("hello frame = the app's captured bytes (01 00, checksum 01)",
          C.frame(0x01, [0x00]) == C.HDR + [0x01, 0x00, 0x01])
    check("read frame for router entry 0 = the app's (02 00 01 00 03)",
          C.frame(0x02, [0x00, 0x01, 0x00]) == C.HDR + B("02 00 01 00 03"))
    try:
        C.frame(0x00, [0x06])
        never = False
    except ValueError:
        never = True
    check("the control opcode (00) can never be framed", never)
    good = C.HDR + B("52 04 01 00 01 30 08 00 00 10")
    check("a real reply parses (our unit's global record)",
          C.parse(good) == (0x52, B("04 01 00 01 30 08 00 00")))
    check("a bad checksum is rejected", C.parse(good[:-1] + [0x11]) is None)
    check("PatchForge's AAAA block decodes to 'AAAA'",
          C.word_decode(B("01 14 28 50 20")) == 0x41414141)
    check("PatchForge's example decodes to 'ABCDEFGH'",
          C.text_decode(B("04 34 48 50 20 08 74 48 51 22")) == "ABCDEFGH")
    check("default mapper word 0 = [127, 0, 127, 0]",
          C.word_decode(B("00 70 0F 40 3F")).to_bytes(4, "big") == bytes([127, 0, 127, 0]))
    rt = all(C.word_decode(C.word_encode(n)) == n
             for n in (0, 1, 0x41414141, 0x7F007F00, 0xFFFFFFFF, 0x12345678))
    check("word encode/decode round-trips", rt)
    check("our preset names decode", [C.text_decode(B(n)) for n in NAMES]
          == ["Preset 1b", "Preset 2", "Preset 3", "Preset 4"])
    check("our device record starts with its name",
          C.text_decode(B(DEVICE)[:15]) == "U6MIDI Pro")
    check("factory router bytes = the crossover",
          [C.mask_to_outputs(*r) for r in FACTORY_ROUTER] == [[3], [4], [5], [0], [1], [2]])
    check("outputs -> mask -> outputs round-trips",
          C.mask_to_outputs(*C.outputs_to_mask([0, 2, 3, 5])) == [0, 2, 3, 5])


def test_filter_codec():
    # the measured table (cme_filter_map.py on the bench, 2026-09-30)
    want = {1: [1, 0, 0], 2: [2, 0, 0], 3: [0, 1, 0], 9: [0, 0x40, 0], 10: [0, 0, 1], 16: [0, 0, 0x40]}
    check("channel bits = the measured table (ch1-2 byte 0, 3-9 byte 1, 10-16 byte 2)",
          all(C.filter_ch_encode([c]) == v for c, v in want.items()))
    import random
    rnd = random.Random(7)
    ok = True
    for _ in range(200):
        chs = sorted(rnd.sample(range(1, 17), rnd.randint(0, 16)))
        ok &= C.filter_ch_decode(C.filter_ch_encode(chs)) == chs
    check("channel encode/decode round-trips (200 random sets)", ok)
    tw = {"note_off": [1, 0, 0], "program_change": [8, 0, 0], "control_change": [0, 1, 0],
          "sysex": [0, 8, 0], "clock": [0, 0, 2], "reset": [0, 0, 0x40]}
    check("type bits = the measured table (PC in byte 0, CC byte 1, clock/reset byte 2)",
          all(C.filter_ty_encode([t])[:3] == v for t, v in tw.items()))
    check("all 18 types round-trip and no two share a bit",
          C.filter_ty_decode(C.filter_ty_encode([n for n, _, _ in C.FILTER_TYPES]))
          == [n for n, _, _ in C.FILTER_TYPES]
          and len({(b, bit) for _, b, bit in C.FILTER_TYPES}) == 18)
    check("type bytes 3-4 (unknown) are kept exactly as read",
          C.filter_ty_encode(["clock"], keep=[9, 9, 9, 0x11, 0x22]) == [0, 0, 2, 0x11, 0x22])
    bad = 0
    for f in (lambda: C.filter_ch_encode([0]), lambda: C.filter_ch_encode([17]),
              lambda: C.filter_ty_encode(["banana"])):
        try:
            f()
        except ValueError:
            bad += 1
    check("channel 0 / 17 and unknown type names are refused", bad == 3)


def test_mapper_codec():
    corpus = json.load(open(os.path.join(HERE, "_cme_mapper_corpus.json")))["entries"]
    raws = [B(e["hex"]) for e in corpus]
    check(f"corpus: {len(raws)} entries the app wrote (2026-09-30 sweeps) are all there",
          len(raws) == 117)
    check("every app-written mapper round-trips byte for byte (decode -> encode on its own base)",
          all(C.mapper_encode(C.mapper_decode(r), base=r) == r for r in raws))
    bad = [corpus[i]["step"] for i, r in enumerate(raws) if C.mapper_encode(C.mapper_decode(r)) != r]
    check("from the factory default, the editor builds EXACTLY the app bytes for all 117",
          not bad, str(bad[:3]))
    byname = {e["step"]: B(e["hex"]) for e in corpus}
    m = C.mapper_decode(byname["src ch min = 9"])
    check("fields land where measured: src ch min = 9 decodes to ch_min 9, CC on USB In 1",
          m["src"]["ch_min"] == 9 and m["src"]["type"] == 2 and m["inputs"] == [0])
    m = C.mapper_decode(byname["octave = +4"])
    check("Notes transpose: octave = +4 decodes to octave +4, semitone 0",
          m["transpose"] == {"octave": 4, "semitone": 0})
    m = C.mapper_decode(byname["random skip = Skip 100%"])
    check("random skip 100% decodes to 100", m["skip"] == 100)
    base = C.mapper_encode({"inputs": [0], "src": {"type": 2}, "dst": {"type": 0}})
    w = C._words(base)
    w[2] |= 1 << 15
    w[3] = (w[3] & 0x00FFFFFF) | (0x33 << 24)
    odd = C._raw(w)
    out = C._words(C.mapper_encode(C.mapper_decode(odd), base=odd))
    check("bits we cannot name (w2 bit 15, w3 top byte) go back exactly as the box had them",
          out[2] >> 15 & 1 and out[3] >> 24 == 0x33)
    v = C.mapper_validate({"inputs": [0], "src": {"type": 7}, "dst": {"type": 3},
                           "follow": {"v1": True}, "transpose": {"octave": 1}})
    check("app rule: Notes transpose fixes the target type and switches value-1 Follow off",
          v["dst"]["type"] == 0 and v["follow"]["v1"] is False)
    v = C.mapper_validate({"inputs": [0], "src": {"type": 8}, "dst": {"type": 4}})
    check("app rule: Global channel update fixes the target type, channel Follow off",
          v["dst"]["type"] == 0 and v["follow"]["ch"] is False)
    v = C.mapper_validate({"inputs": [0], "src": {"type": 2}, "dst": {"type": 0},
                           "transpose_ch": True, "follow": {"ch": True}})
    check("app rule: Transpose channel clears channel Follow", v["follow"]["ch"] is False)
    v = C.mapper_validate({"inputs": [], "src": {"type": 2}, "dst": {"type": 0}})
    check("no inputs is allowed: that is the app Disable-inputs state", v["inputs"] == [])
    refused = 0
    for bad_m in ({"inputs": [0], "src": {}, "dst": {"type": 0}},
                  {"inputs": [0], "src": {"type": 2}, "dst": {}},
                  {"inputs": [0], "src": {"type": 2, "ch_min": 9, "ch_max": 3}, "dst": {"type": 0}},
                  {"inputs": [0], "src": {"type": 2, "v1": [90, 10]}, "dst": {"type": 0}},
                  {"inputs": [7], "src": {"type": 2}, "dst": {"type": 0}},
                  C.mapper_decode(P1_M1)):
        try:
            C.mapper_validate(bad_m)
        except ValueError:
            refused += 1
    check("refused: no source/target type, upside-down channel or value range, bad input, "
          "and the leftover +79-semitone entry (outside the app +-48)", refused == 6)
    check("the leftover entry still DECODES (shown, not choked on)",
          C.mapper_decode(P1_M1)["transpose"]["semitone"] == 79)
    t = C.mapper_describe(C.mapper_decode(byname["src v1 max = 64"]))
    check("plain-words description",
          t == "Ctrl Change on USB In 1, ch 1-16 (Control 0-64) -> Note On ch 1", t)
    check("unused mapper is described as unused",
          C.mapper_describe(C.mapper_decode(B(C.DEFAULT_MAPPER))) == "unused")
    check("curve field (measured on the wire): User 1-4 = 1/3/5/7, off = 0, bit 3 ignored",
          [C.curve_field(u) for u in (None, 1, 2, 3, 4)] == [0, 1, 3, 5, 7]
          and [C.curve_user(f) for f in (0, 1, 3, 5, 7, 2, 9, 15)] == [None, 1, 2, 3, 4, None, 1, 4])
    m = C.mapper_decode(C.mapper_encode({"inputs": [0], "src": {"type": 2}, "dst": {"type": 2},
                                         "follow": {"v2": False}, "v2": {"curve": C.curve_field(3)}}))
    check("the description names the curve", "curve User 3" in C.mapper_describe(m), C.mapper_describe(m))
    c = C.curve_decode(CURVE1_RAW)
    check("curve 1 off our unit decodes to 128 values: 0, 2, 4 ... then held at 127",
          len(c) == 128 and c[:4] == [0, 2, 4, 6] and c[-1] == 127 and max(c) == 127)


def test_link(tmp):
    box = FakeBox()
    L = link(box, tmp)
    st = L.refresh()
    check("refresh: connected, preset 2, names + device name read",
          st["connected"] and st["preset"] == 2 and st["preset_names"][0] == "Preset 1b"
          and st["device_name"] == "U6MIDI Pro", str({k: st.get(k) for k in ("connected", "preset", "error")}))
    check("refresh: router as output lists, nothing filtered, mappers default",
          st["router"] == [[3], [4], [5], [0], [1], [2]] and st["filters_set"] == []
          and st["mappers_set"] == [])
    pub = json.load(open(os.path.join(tmp, "cme.json")))
    check("refresh publishes the state file (valid JSON, same preset)", pub["preset"] == 2)

    box.sent.clear()
    L.set_preset(1)
    writes = [f for f in box.sent if f[5] == 0x03]
    check("set_preset(1): exactly one write = 03 04 01 00 00 30 08 00 00",
          writes == [C.HDR + B("03 04 01 00 00 30 08 00 00 40")], str(writes))
    check("... only byte 0 changed: the Settings bytes went back as read",
          box.settings == [0x30, 0x08, 0x00, 0x00])
    check("... and the re-read shows preset 1's own router",
          L.state["preset"] == 1 and L.state["router"][3] == [0, 3])
    L.set_preset("prev")
    check("'prev' from 1 wraps to 4", L.state["preset"] == 4)
    L.set_preset("next")
    check("'next' from 4 wraps to 1", L.state["preset"] == 1)
    L.set_preset(2)

    box.sent.clear()
    for bad in (0, 5, "x"):
        try:
            L.set_preset(bad)
        except Exception:
            pass
    check("invalid presets (0, 5, junk) write nothing",
          not [f for f in box.sent if f[5] == 0x03])

    box.sent.clear()
    L.set_route(3, [0, 3])
    writes = [f for f in box.sent if f[5] == 0x03]
    check("set_route(MIDI In 1 -> USB Out 1 + MIDI Out 1) = 03 00 01 03 01 01",
          writes == [C.HDR + B("03 00 01 03 01 01 09")], str(writes))
    check("... the box now has it and the state shows it",
          box.routers[1][3] == [1, 1] and L.state["router"][3] == [0, 3])
    check("... only that input changed",
          [r for i, r in enumerate(box.routers[1]) if i != 3]
          == [r for i, r in enumerate(FACTORY_ROUTER) if i != 3])
    box.sent.clear()
    for args in ((6, [0]), (0, [6]), (-1, [])):
        try:
            L.set_route(*args)
        except Exception:
            pass
    check("router input/output out of range writes nothing",
          not [f for f in box.sent if f[5] == 0x03])

    box.lie = True
    outcomes = []
    for call in (lambda: L.set_preset(3), lambda: L.set_route(0, [0])):
        try:
            call()
            outcomes.append("accepted")
        except IOError:
            outcomes.append("refused")
    box.lie = False
    check("a box that ACKS a preset write but doesn't switch is caught by the read-back",
          outcomes[0] == "refused" and L.state["preset"] == 2, str(outcomes))
    check("... same for a router write that doesn't stick", outcomes[1] == "refused")

    box.sent.clear()
    L.set_filter(4, [10, 1], ["clock", "sysex"])
    writes = [f[5:-1] for f in box.sent if f[5] == 0x03]
    check("set_filter writes the channel + type masks for that port, nothing else",
          writes == [B("03 01 01 04 01 00 01"), B("03 02 01 04 00 08 02 11 22")], str(writes))
    check("... the unknown type bytes on the box survived the edit",
          box.fty[1][4][3:] == [0x11, 0x22])
    f4 = L.state["filters"][4]
    check("... state decodes it back: ch 1 + 10, clock + sysex blocked",
          f4["channels"] == [1, 10] and f4["types"] == ["sysex", "clock"]
          and 4 in L.state["filters_set"], str(f4))
    L.set_filter(4, [], [])
    check("pass-everything clears it (unknown bytes still kept)",
          box.fch[1][4] == [0, 0, 0] and box.fty[1][4] == [0, 0, 0, 0x11, 0x22]
          and L.state["filters"][4]["channels"] == [] and L.state["filters"][4]["types"] == [])
    box.sent.clear()
    for args in ((12, [], []), (0, [17], []), (0, [], ["nope"])):
        try:
            L.set_filter(*args)
        except Exception:
            pass
    check("bad port / channel / type writes nothing",
          not [f for f in box.sent if f[5] == 0x03])
    L.refresh()
    check("refresh reads all 16 mappers (described) and the 4 curves",
          len(L.state["mappers"]) == 16 and L.state["mappers"][0]["text"] == "unused"
          and len(L.state["curves"]) == 4 and L.state["curves"][0][:3] == [0, 2, 4])
    box.sent.clear()
    want = {"inputs": [0, 3], "src": {"type": 6, "v1": [60, 63]},
            "dst": {"type": 2, "v1": [18, 18]}, "follow": {"v1": False}}
    L.set_mapper(13, want)
    writes = [f for f in box.sent if f[5] == 0x03]
    check("set_mapper writes exactly the encoded entry to mapper 14, nothing else",
          len(writes) == 1 and writes[0][5:9] == [0x03, 0x03, 0x04, 13]
          and writes[0][9:29] == C.mapper_encode(want), str(writes))
    check("... and the state describes it from the read-back",
          L.state["mappers"][13]["text"].startswith(
              "Key Aftertouch on USB In 1, MIDI In 1, ch 1-16 (Note 60-63) -> Ctrl Change"),
          L.state["mappers"][13]["text"])
    L.set_mapper(13, "clear")
    check("clear writes the factory default entry (what the app reset writes)",
          box.mappers[1][13] == B(C.DEFAULT_MAPPER) and L.state["mappers"][13]["default"])
    box.sent.clear()
    for args in ((16, want), (0, {"inputs": [0], "src": {}, "dst": {"type": 0}})):
        try:
            L.set_mapper(*args)
        except Exception:
            pass
    check("bad mapper index / incomplete mapper writes nothing",
          not [f for f in box.sent if f[5] == 0x03])
    box.lie = True
    try:
        L.set_mapper(2, want)
        lied = "accepted"
    except IOError:
        lied = "refused"
    box.lie = False
    check("a box that acks a mapper write but keeps the old one is caught by the read-back",
          lied == "refused")
    box.sent.clear()
    t0 = time.monotonic()
    ok = L.write(0x01, 1, 0, [0x7F, 0x7F, 0x7F])
    check("a value the box REJECTS (7E) fails at once and is never resent",
          ok is False and box.rejected == 1 and time.monotonic() - t0 < 0.15
          and len([f for f in box.sent if f[5] == 0x03]) == 1)

    try:
        L.read(0x00, 0, 0)
        err = False
    except LookupError:
        err = True
    check("a 0-word read raises (the box's 7C sentinel)", err)

    L._replies.queue.clear()
    L._on_msg(Msg("note_on"))
    L._on_msg(Msg("sysex", [0x7E, 0x7F, 0x06, 0x01]))
    check("music and foreign SysEx on the port are dropped, not queued",
          L._replies.empty())


def test_silent(tmp):
    box = FakeBox(silent=True)
    L = link(box, tmp)
    t0 = time.monotonic()
    st = L.refresh()
    check("silent box: refresh says connected:false, no exception",
          st["connected"] is False)
    check("... and gives up quickly (one reopen + retry, short timeout)",
          time.monotonic() - t0 < 1.5, f"{time.monotonic() - t0:.2f}s")
    box.sent.clear()
    try:
        L.set_preset(1)
        raised = False
    except IOError:
        raised = True
    check("silent box: set_preset raises and sends NO write",
          raised and not [f for f in box.sent if f[5] == 0x03])
    check("no control (00) frame was ever sent, anywhere",
          not [f for f in box.sent if f[5] == 0x00])


def test_worker(tmp):
    box = FakeBox()
    L = link(box, tmp)
    order, done = [], threading.Event()
    L.submit("a", lambda: order.append(("a", threading.current_thread().name)))
    L.submit("bad", lambda: 1 / 0)
    L.submit("b", lambda: (order.append(("b", threading.current_thread().name)), done.set()))
    done.wait(2)
    time.sleep(0.05)
    check("submitted jobs run in order on ONE worker thread, off the caller",
          [o[0] for o in order] == ["a", "b"] and order[0][1] == order[1][1]
          and order[0][1] != threading.current_thread().name, str(order))
    check("a failing job doesn't stop the worker; the last result is published",
          L.state.get("last", {}).get("what") == "b"
          and json.load(open(os.path.join(tmp, "cme.json")))["last"]["ok"] is True)


def test_brain_wiring():
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_execute_cmd")
    body = ast.get_source_segment(src, fn)
    seg = body[body.index('"CME_PRESET", "CME_ROUTE", "CME_FILTER", "CME_MAPPER", "CME_REFRESH"'):]
    seg = seg[:seg.index("elif action == \"SKIPBACK_SAVE_ALL\"")]
    check("brain: CME commands only ever QUEUE work on the link (never block the cmd thread)",
          seg.count("cme.submit(") == 5 and "lambda: cme.set_filter" in seg
          and "lambda: cme.set_mapper" in seg and "cme.set_preset(want)" in seg
          and "lambda: cme.set_preset" in seg)
    check("brain: CME_PRESET with no argument = next (pedal-friendly)",
          'cmd.get("preset", "next")' in seg)
    check("brain: edits pass the page's on_preset to the link, every job passes its id",
          'cmd.get("on_preset")' in seg and "set_route(inp, outs, onp)" in seg
          and "set_filter(fp, fch, fty, onp)" in seg and "set_mapper(mi, mm, onp)" in seg
          and seg.count(", job)") == 5)


def _writes(box):
    return [f for f in box.sent if f[5] == 0x03]


def test_review_fixes(tmp):
    """2026-10-01 review findings: partial reads, preset races, stale pages,
    stale replies, worker start, codec range, malformed requests."""
    # 1: connected but a section read fails -> PARTIAL, nothing editable published
    box = FakeBox()
    box.fail_read_sec = 0x03                      # mappers never answer
    L = link(box, tmp)
    st = L.refresh()
    check("partial read: connected + partial + the error, and NO config at all",
          st["connected"] and st.get("partial") is True and "mappers[0]" in st.get("error", "")
          and not any(k in st for k in ("router", "filters", "mappers", "preset")), str(st))
    box.fail_read_sec = None
    st = L.refresh()
    check("... a full re-read clears partial", st["connected"] and not st.get("partial")
          and st["preset"] == 2)

    js = open(os.path.join(HERE, "static", "js", "cme.js"), encoding="utf-8").read()
    check("page: full() = connected AND not partial AND a known preset",
          'st.connected && !st.partial && typeof st.preset === "number"' in js)
    check("page: router cells only editable with every row read (else disabled, click ignored)",
          "var routerOk = ok && Array.isArray(st.router) && st.router.length === ins.length" in js
          and "b.disabled = !cur || !!busy" in js and "if (!cur || !full()) return;" in js
          and "renderFilters(ok, names)" in js and "renderMappers(ok, names)" in js)
    check("page: mapper SAVE/CLEAR + filter sends refuse without full data",
          "if (!mappersOk()) return;" in js and "if (writeOk())" in js
          and "fl.length !== ports.length" in js and "if (!full()) return;" in js)

    # 2: an edit computed on preset N is refused once the box is on another preset
    box = FakeBox()
    L = link(box, tmp)
    L.refresh()
    box.sent.clear()
    try:
        L.set_route(3, [0, 3], preset=1)          # page saw preset 1; box is on 2
        out = "written"
    except IOError as e:
        out = str(e)
    check("route made on preset 1, box now on 2: refused, NOTHING written",
          "preset 2 now, not 1" in out and not _writes(box) and box.routers[1][3] == [1, 0], out)
    refused = 0
    for call in (lambda: L.set_filter(4, [1], [], preset=3),
                 lambda: L.set_mapper(0, "clear", preset=4)):
        try:
            call()
        except IOError:
            refused += 1
    check("... same for filter + mapper edits (no write)", refused == 2 and not _writes(box))
    L.set_route(3, [0, 3], preset=2)
    L.set_route(4, [1])                           # no preset = brain/pedal: unchanged
    check("matching preset writes; no preset (brain/FCB) still writes as before",
          box.routers[1][3] == [1, 1] and box.routers[1][4] == [2, 0])
    act, pl = C.web_command({"what": "route", "input": 3, "outputs": [0], "preset": 2, "job": "w1"})
    check("web: the page's preset goes on as on_preset (job too); CME_PRESET unaffected",
          act == "CME_ROUTE" and pl.get("on_preset") == 2 and pl.get("job") == "w1"
          and C.web_command({"what": "preset", "preset": 3}) == ("CME_PRESET", {"preset": 3})
          and "on_preset" not in C.web_command({"what": "mapper", "index": 0, "mapper": "clear"})[1])

    # 3: a write that fails AFTER touching the box still refreshes the page's state
    box = FakeBox()
    L = link(box, tmp)
    L.refresh()
    box.drop_ack = True
    try:
        L.set_route(0, [2])
        res = "ok"
    except IOError:
        res = "raised"
    box.drop_ack = False
    check("lost ack: the job fails, but the state was re-read and shows the box's truth",
          res == "raised" and L.state["router"][0] == [2] and box.routers[1][0] == [4, 0])
    box.reject_types = True
    try:
        L.set_filter(5, [3], ["clock"])
        res = "ok"
    except IOError as e:
        res = str(e)
    box.reject_types = False
    check("filter part two refused: error says part one WAS written, state shows ch 3 blocked",
          "WAS written" in res and L.state["filters"][5]["channels"] == [3]
          and L.state["filters"][5]["types"] == [], res)

    # 4: a stale 52 reply (another address) is skipped, not taken as the answer
    box.stale_next = True
    v = L.read(0x00, 1, 2)
    check("read skips a stale reply for another address and returns the real value",
          v == box.routers[1][2], str(v))

    # 5: the job id is echoed so the page matches ITS click
    done = threading.Event()
    L.submit("bg", lambda: None)                  # a hot-plug style job, no id
    L.submit("mine", lambda: done.set(), job="w42")
    done.wait(2)
    time.sleep(0.05)
    check("worker echoes the job id in last (None for background jobs)",
          L.state["last"].get("job") == "w42"
          and "s.last.job === busyJob" in js)

    # 6: the worker exists from construction, and racing submitters start only one
    L2 = link(FakeBox(), tmp)
    check("worker is running right after construction", L2._worker is not None and L2._worker.is_alive())
    started = []
    real = C.threading

    class SlowThread(real.Thread):
        def start(self):
            started.append(self)
            time.sleep(0.05)                       # widen the check-then-start window
            super().start()

    class Shim:
        Thread, Lock, Event = SlowThread, real.Lock, real.Event
    L2._worker = None                              # as if it had died
    C.threading = Shim
    try:
        go = real.Barrier(6)
        ts = [real.Thread(target=lambda: (go.wait(), L2.submit("x", lambda: None))) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(3)
    finally:
        C.threading = real
    check("6 racing submits start exactly ONE worker", len(started) == 1, str(len(started)))

    # 8: the codec refuses out-of-range bytes instead of masking them
    bad = 0
    for p in ([0x80], [0x00, 0x1FF], [-1]):
        try:
            C.frame(0x03, p)
        except ValueError:
            bad += 1
    check("frame() raises on a byte > 0x7F or < 0 (never writes a masked value)", bad == 3)

    # 9: malformed mapper requests are a ValueError (= 400), never an AttributeError (500)
    errs = []
    for mp in ([1, 2], "nope", {"src": 5, "dst": {"type": 0}}, {"inputs": 3, "src": {"type": 2}},
               {"src": {"type": 2}, "dst": {"type": 0}, "follow": "x"}):
        try:
            C.mapper_validate(mp)
            errs.append("accepted")
        except ValueError:
            errs.append("400")
        except Exception as e:
            errs.append(type(e).__name__)
    check("mapper_validate: list / string / wrong-kind sub-fields -> ValueError", errs == ["400"] * 5, str(errs))
    errs = []
    for body in ({"what": "mapper", "index": 0, "mapper": [1]}, {"what": "mapper", "index": 0, "mapper": "x"},
                 {"what": "mapper", "index": "a"}, ["what"], {"what": "route", "input": 0, "preset": True},
                 {"what": "preset", "preset": True}, {"what": "refresh", "job": 7}, {"what": "zz"}):
        try:
            C.web_command(body)
            errs.append("accepted")
        except ValueError:
            errs.append("400")
        except Exception as e:
            errs.append(type(e).__name__)
    check("web_command: every malformed body is a ValueError (400)", errs == ["400"] * 8, str(errs))
    web = open(os.path.join(HERE, "orc1_web.py"), encoding="utf-8").read()
    fn = web[web.index("def api_cme_set"):]
    fn = fn[:fn.index("\n\n\n")]
    check("web: /api/cme POST = web_command, ValueError -> 400",
          "_c.web_command(" in fn and "except ValueError" in fn and "400" in fn)
    check("page: a stored octave outside -4..4 is warned about (like the semitone)",
          "stored octave " in js and "m.transpose.octave < -4 || m.transpose.octave > 4" in js)


def test_brain_lifecycle():
    """7 + 10: the CME link sees plugs and same-name replugs on its own,
    whatever PATCHBAY_PORT_PREFIXES says; its config frames never reach DIN."""
    src = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    calls = []

    class StubCme:
        def matches(self, n):
            return "u6midi" in n.lower()

        def submit(self, label, fn, job=None):
            calls.append(label)
    ns = {"cme": StubCme(), "_cme_present": set(), "print": lambda *a, **k: None}
    exec(fns["_cme_scan"], ns)
    scan = ns["_cme_scan"]
    P1 = "U6MIDI Pro:U6MIDI Pro Port 1 40:0"
    others = {"U6MIDI Pro:U6MIDI Pro Port 2 40:1", "pisound:pisound MIDI PS 28:0"}
    scan(others | {P1})
    scan(others | {P1})
    scan(others | {P1}, {"U6MIDI Pro"})          # same name, new USB address
    scan(others | {P1}, {"Some Other Card"})
    scan(others)
    scan(others)
    check("_cme_scan: connect once, replug on a new USB address, unplugged once",
          calls == ["connect", "replug", "unplugged"], str(calls))
    check("_cme_scan does not depend on the patchbay decision",
          "_is_patchbay" not in fns["_cme_scan"] and "_PATCHBAY" not in fns["_cme_scan"])
    hp = fns["_hotplug_scan_once"]
    check("hot-plug scan feeds the link every scan (with the replugged set); old hook gone",
          "_cme_scan(current, replugged)" in hp and "_cme_port_event" not in src)
    uh = fns["make_usb_handler"]
    check("engine USB input drops CME config frames before _route_message (never _pass to DIN)",
          "is_config_frame(msg)" in uh
          and uh.index("is_config_frame(msg)") < uh.index("_route_message(msg"))
    check("is_config_frame: a CME reply yes; music / foreign SysEx no",
          C.is_config_frame(Msg("sysex", C.HDR + [0x51, 0x00, 0x51]))
          and not C.is_config_frame(Msg("sysex", [0x7E, 0x7F, 0x06, 0x01]))
          and not C.is_config_frame(Msg("note_on")))


def main():
    print("CME U6MIDI Pro link suite")
    tmp = tempfile.mkdtemp(prefix="cme_test_")
    try:
        test_codec()
        test_filter_codec()
        test_mapper_codec()
        test_link(tmp)
        test_silent(tmp)
        test_worker(tmp)
        test_brain_wiring()
        test_review_fixes(tmp)
        test_brain_lifecycle()
    finally:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)
    passed = sum(_res)
    print(f"\n{passed} passed, {len(_res) - passed} failed  (expected {EXPECTED})")
    if len(_res) != EXPECTED:
        print(f"COUNT DRIFT: ran {len(_res)} checks, EXPECTED = {EXPECTED}")
        return 1
    return 0 if passed == len(_res) else 1


if __name__ == "__main__":
    sys.exit(main())
