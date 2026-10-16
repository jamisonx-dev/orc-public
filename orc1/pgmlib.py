#!/usr/bin/env python3
"""pgmlib — MPC1000 / JJOS2XL program (.PGM) and pad-mode (.PMD) file tool.

Pure stdlib, runs anywhere (PC against a CF card / USB-mounted MPC, or Pi).

Architecture note: the raw bytes are the source of truth. Files load into a
bytearray; every accessor reads/writes offsets in place. Anything we haven't
decoded yet passes through untouched, so load->save is byte-identical by
construction — safe on a live library even though the JJOS2XL format is only
partially reverse-engineered (see _planning/pgm_re/FINDINGS.md).

Format knowledge:
  Stock PGM v1.00 layout: https://www.mybunnyhug.org/fileformats/pgm/
  JJOS2XL extensions ("MPC1000 PGM 6.00"): reverse-engineered from owner's
  own files — header flag bytes (0x17 = INST program), chop index at sample
  slot +0x17, pad+0x00 = sample-memory rank, INST key mapping via WAV 'smpl'
  unity notes (not stored in the PGM).
  PMD "PAD_MODE 1.0": 0x20 header + 10 slots x 0x280; per slot four 64-pad
  arrays: type[64] @+0x00, number[64] @+0x40, value[64] @+0x80, flag[64]
  @+0xC0 (remainder undecoded, preserved verbatim).

Usage:
  pgmlib.py inspect FILE.PGM|FILE.PMD
  pgmlib.py diff A B                    (any two same-size files; PGM/PMD aware)
  pgmlib.py pmd-set FILE.PMD --slot N --pad A01[-A16] --type note|cc|pc|VAL
                    [--num N] [--value N] [--flag N] [-o OUT]
  pgmlib.py pmd-copy-slot FILE.PMD --from N --to N [-o OUT]
"""
import struct, sys, os, argparse

NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]


def write_file(path, data):
    """Write bytes (or a Program/PatternBank) to path.

    Clears the read-only attribute and retries if needed (common for files
    copied off CF cards / MPC USB storage)."""
    if hasattr(data, 'data'):      # Program or PatternBank — extract bytes
        data = bytes(data.data)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except PermissionError:
        import stat
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
        with open(path, "wb") as f:
            f.write(data)

def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 2}" if 0 <= n <= 127 else "?"

def pad_label(i):
    return f"{'ABCD'[i // 16]}{i % 16 + 1:02d}"  # A01..D16

def parse_pad(s):
    s = s.strip().upper()
    if s.isdigit():
        i = int(s)
    else:
        bank = "ABCD".index(s[0]); i = bank * 16 + int(s[1:]) - 1
    if not 0 <= i <= 63:
        raise ValueError(f"pad out of range: {s}")
    return i


# ═════════════════════════════════════════════════════════════════ PGM ═════

class Program:
    SIZE = 10756  # 0x2A04, stock and JJOS2XL alike
    PAD_BASE, PAD_STRIDE, SLOT_STRIDE = 0x18, 0xA4, 0x18

    # pad-param fields, offset relative to pad-param block (stock v1.00 names)
    PAD_FIELDS = {
        0x02: "voice_overlap", 0x03: "mute_group", 0x06: "attack",
        0x07: "decay", 0x08: "decay_mode", 0x0B: "vel_to_level",
        0x11: "f1_type", 0x12: "f1_freq", 0x13: "f1_res", 0x18: "f1_vel",
        0x19: "f2_type", 0x1A: "f2_freq", 0x1B: "f2_res", 0x20: "f2_vel",
        0x2F: "mix_level", 0x30: "mix_pan", 0x31: "output",
        0x32: "fx_send", 0x33: "fx_level", 0x34: "filter_att",
    }

    def __init__(self, data):
        if len(data) != self.SIZE:
            raise ValueError(f"not a PGM: size {len(data)} != {self.SIZE}")
        self.data = bytearray(data)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    def save(self, path):
        write_file(path, bytes(self.data))

    # header --------------------------------------------------------------
    @property
    def version(self):
        return self.data[0x04:0x14].rstrip(b"\x00").decode("ascii", "replace")

    @property
    def is_jjos(self):
        return b"6.00" in self.data[0x04:0x14]

    @property
    def inst_flag(self):          # hdr 0x17: 1 = INST-type program (JJOS2XL)
        return self.data[0x17]

    @property
    def chop_flag(self):          # hdr 0x15: seen 1 on chop-derived programs
        return self.data[0x15]

    # sample slots ----------------------------------------------------------
    def _slot(self, p, s):
        return self.PAD_BASE + p * self.PAD_STRIDE + s * self.SLOT_STRIDE

    def sample(self, p, s):
        o = self._slot(p, s)
        d = self.data
        return {
            "name": d[o:o+16].rstrip(b"\x00").decode("ascii", "replace"),
            "level": d[o+0x11], "range_lo": d[o+0x12], "range_hi": d[o+0x13],
            "tuning": struct.unpack_from("<h", d, o+0x14)[0],
            "play_mode": d[o+0x16], "chop": d[o+0x17],
        }

    def set_sample_name(self, p, s, name):
        name = sanitize_name(name)
        o = self._slot(p, s)
        self.data[o:o+16] = name.encode("ascii").ljust(16, b"\x00")

    # pad params -----------------------------------------------------------
    def _padblk(self, p):
        return self.PAD_BASE + p * self.PAD_STRIDE + 0x60

    def pad_params(self, p):
        o = self._padblk(p)
        out = {n: self.data[o+off] for off, n in self.PAD_FIELDS.items()}
        out["sample_rank"] = self.data[o]        # JJOS: 1-based alpha rank
        return out

    # midi maps ------------------------------------------------------------
    @property
    def pad_note(self):
        return list(self.data[0x2918:0x2958])

    @property
    def note_pad(self):
        return list(self.data[0x2958:0x29D8])

    def set_pad_note(self, p, note):
        """Assign MIDI note to pad p and keep the inverse table consistent."""
        if not (0 <= note <= 127):
            raise ValueError("note out of range")
        self.data[0x2918 + p] = note
        self.rebuild_note_map()

    def set_pad_notes(self, notes):
        if len(notes) != 64:
            raise ValueError("need 64 notes")
        self.data[0x2918:0x2958] = bytes(notes)
        self.rebuild_note_map()

    def rebuild_note_map(self):
        """Regenerate note->pad from pad->note (lowest pad wins a note)."""
        npad = bytearray([64] * 128)
        for p in range(63, -1, -1):
            npad[self.data[0x2918 + p]] = p
        self.data[0x2958:0x29D8] = npad

    # reporting ------------------------------------------------------------
    def inspect(self):
        L = [f"{self.version}  ({'JJOS2XL' if self.is_jjos else 'stock'})",
             f"hdr flags 0x14-0x17: {self.data[0x14:0x18].hex(' ')}"
             f"   type: {'INST' if self.inst_flag else 'DRUM'}"
             f"{' +chop' if self.chop_flag else ''}"]
        used = 0
        for p in range(64):
            row = []
            for s in range(4):
                sm = self.sample(p, s)
                if sm["name"]:
                    t = f" tune{sm['tuning']:+d}" if sm["tuning"] else ""
                    c = f" chop{sm['chop']}" if sm["chop"] else ""
                    r = (f" v{sm['range_lo']}-{sm['range_hi']}"
                         if (sm["range_lo"], sm["range_hi"]) != (0, 127) else "")
                    row.append(f"s{s}:{sm['name']} lv{sm['level']}{r}{t}{c}"
                               f" {'note-on' if sm['play_mode'] else '1shot'}")
            if row:
                used += 1
                pp = self.pad_params(p)
                extras = []
                if pp["mute_group"]: extras.append(f"mute{pp['mute_group']}")
                if pp["voice_overlap"]: extras.append("mono")
                if pp["f1_type"]: extras.append(f"f1t{pp['f1_type']}@{pp['f1_freq']}")
                note = self.pad_note[p]
                L.append(f"pad {pad_label(p)} (note {note} {note_name(note)})"
                         f"{' [' + ' '.join(extras) + ']' if extras else ''}")
                for r in row:
                    L.append(f"    {r}")
        L.append(f"{used} pads in use")
        pn = self.pad_note
        L.append("pad->note: chromatic 36-99" if pn == list(range(36, 100))
                 else f"pad->note: custom {pn}")
        return "\n".join(L)


# ═════════════════════════════════════════════════════════════════ PMD ═════

class PadMode:
    """JJOS2XL PAD_MODE 1.0 file: pads transmit notes / CCs / prog changes.

    Layout (reverse-engineered, FINDINGS.md sessions 5+):
      0x00 u16 size (0x1920)   0x04 'PAD_MODE 1.0'
      0x20 + slot*0x280 : per-slot block, 10 slots:
        +0x00 type[64]    observed: 0=note(default) 2=CC 3=prog change
        +0x40 number[64]  CC number / program number
        +0x80 value[64]   fixed CC value
        +0xC0 flag[64]    observed 0/1 per pad (semantics TBD — momentary?)
        +0x100..0x27F     undecoded, preserved verbatim
    """
    SIZE, SLOTS, SLOT_SIZE, BASE = 6432, 10, 0x280, 0x20
    TYPES = {0: "note", 2: "cc", 3: "pc"}
    TYPE_IDS = {v: k for k, v in TYPES.items()}

    def __init__(self, data):
        if len(data) != self.SIZE or data[0x04:0x10] != b"PAD_MODE 1.0":
            raise ValueError("not a PAD_MODE 1.0 PMD file")
        self.data = bytearray(data)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    @classmethod
    def blank(cls):
        d = bytearray(cls.SIZE)
        struct.pack_into("<H", d, 0, cls.SIZE)
        d[0x04:0x10] = b"PAD_MODE 1.0"
        return cls(d)

    def save(self, path):
        write_file(path, bytes(self.data))

    def _arr(self, slot, which):
        return self.BASE + slot * self.SLOT_SIZE + which * 0x40

    def get(self, slot, pad):
        return {k: self.data[self._arr(slot, w) + pad]
                for w, k in enumerate(("type", "num", "value", "flag"))}

    def set(self, slot, pad, type=None, num=None, value=None, flag=None):
        for w, v in enumerate((type, num, value, flag)):
            if v is not None:
                if not 0 <= v <= 255:
                    raise ValueError("byte value out of range")
                self.data[self._arr(slot, w) + pad] = v

    def slot_used(self, slot):
        o = self.BASE + slot * self.SLOT_SIZE
        return any(self.data[o:o + self.SLOT_SIZE])

    def copy_slot(self, src, dst):
        so = self.BASE + src * self.SLOT_SIZE
        do = self.BASE + dst * self.SLOT_SIZE
        self.data[do:do + self.SLOT_SIZE] = self.data[so:so + self.SLOT_SIZE]

    def inspect(self):
        L = ["PAD_MODE 1.0"]
        for sl in range(self.SLOTS):
            if not self.slot_used(sl):
                continue
            L.append(f"-- slot {sl} --")
            for p in range(64):
                e = self.get(sl, p)
                if not any(e.values()):
                    continue
                t = self.TYPES.get(e["type"], f"type{e['type']}")
                if t == "cc":
                    desc = f"CC {e['num']:3d}  value {e['value']:3d}"
                elif t == "pc":
                    desc = f"PC {e['num']:3d}"
                elif t == "note":
                    desc = f"note (default){' num=' + str(e['num']) if e['num'] else ''}"
                else:
                    desc = str(e)
                L.append(f"  {pad_label(p)}: {desc}"
                         f"{'  flag=' + str(e['flag']) if e['flag'] else ''}")
            # undecoded residue check
            o = self.BASE + sl * self.SLOT_SIZE
            resid = self.data[o + 0x100:o + self.SLOT_SIZE]
            if any(resid):
                L.append(f"  (slot {sl}: undecoded bytes at +0x100.. — preserved)")
        return "\n".join(L)


# ═════════════════════════════════════════════════════════════════ PAT ═════

class PatternBank:
    """JJOS2XL pattern bank (.PAT) — pad-triggerable note patterns.

    Reverse-engineered from CNM_PATBANK01a.PAT (FINDINGS.md session 7):
      0x00 u32 file size, 0x04 'PATTERN BANK1.0', 0x14 12 unknown bytes
      (preserved), then 16-byte records from 0x20:
        pattern header: 00 00 00 00 00 8A <flag> <bars> <name[8]>
        event:          <time u32> 00 90 <note> <vel> <dur u32> 00 <pad> 00 00
        pattern end:    <last time u32> 00 FF ...
        bank end:       FF FF FF 7F 00 FF ...
      Times/durations in ticks @ 96 PPQN (bar of 4/4 = 384).
    """
    MAGIC = b"PATTERN BANK1.0"
    PPQN = 96

    def __init__(self, data):
        if data[0x04:0x13] != self.MAGIC:
            raise ValueError("not a PATTERN BANK1.0 file")
        self.data = bytearray(data)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    def save(self, path):
        write_file(path, bytes(self.data))

    def patterns(self):
        """Parse into [{name, bars, flag, events:[{time,note,vel,dur,pad}]}]."""
        out, cur = [], None
        pos = 0x20
        d = self.data
        while pos + 16 <= len(d):
            rec = d[pos:pos+16]
            status = rec[5]
            if status == 0x8A:
                cur = {"name": rec[8:16].rstrip(b"\x00").decode("ascii", "replace"),
                       "flag": rec[6], "bars": rec[7], "events": []}
                out.append(cur)
            elif status == 0xFF:
                if rec[:4] == b"\xff\xff\xff\x7f":
                    break                      # bank end
                cur = None                     # pattern end
            elif (status & 0xF0) == 0x90 and cur is not None:
                cur["events"].append({
                    "time": struct.unpack_from("<I", rec, 0)[0],
                    "note": rec[6], "vel": rec[7],
                    "dur": struct.unpack_from("<I", rec, 8)[0],
                    "pad": rec[13]})
            pos += 16
        return out

    # writing ---------------------------------------------------------------
    # Observed in a real save: bytes 0x14-0x1F of the header (unknown, with
    # 'Z' filler runs). Preserved when editing; used verbatim for new banks.
    _HDR_UNKNOWN = bytes.fromhex("005a5a5a000000005a5a5a5a")

    @classmethod
    def blank(cls):
        d = bytearray(0x20)
        d[0x04:0x13] = cls.MAGIC
        d[0x14:0x20] = cls._HDR_UNKNOWN
        d += b"\xff\xff\xff\x7f" + bytes([0, 0xFF]) + bytes(10)   # bank end
        struct.pack_into("<I", d, 0, len(d))
        return cls(bytes(d))

    def set_patterns(self, pats):
        """Rebuild the whole bank from [{name,flag,bars,events}] structures.
        Serializing self.patterns() back reproduces the file byte-for-byte."""
        out = bytearray(self.data[:0x20])            # header preserved
        for p in pats:
            # Pattern names are laxer than sample names — real banks contain
            # '.' (e.g. "q.Test02"). Allow any printable ASCII, 8 chars max.
            name = "".join(c for c in str(p["name"]) if 0x20 <= ord(c) < 0x7F)[:8]
            out += bytes(4) + bytes([0, 0x8A, p.get("flag", 0) & 0xFF,
                                     max(1, min(int(p["bars"]), 255))])
            out += name.encode("ascii").ljust(8, b"\x00")
            evs = sorted(p["events"], key=lambda e: e["time"])
            last = 0
            for e in evs:
                t, note = int(e["time"]), int(e["note"])
                vel, dur, pad = int(e["vel"]), int(e["dur"]), int(e["pad"])
                # dur 0 exists in real MPC-written banks (zero-gate hits) —
                # only reject truly impossible values.
                if not (0 <= note <= 127 and 1 <= vel <= 127
                        and 0 <= pad <= 63 and t >= 0 and dur >= 0):
                    raise ValueError(f"bad event in '{name}': {e}")
                out += struct.pack("<I4BI2BH", t, 0, 0x90, note, vel,
                                   dur, 0, pad, 0)
                last = t
            out += struct.pack("<I", last) + bytes([0, 0xFF]) + bytes(10)
        out += b"\xff\xff\xff\x7f" + bytes([0, 0xFF]) + bytes(10)
        struct.pack_into("<I", out, 0, len(out))
        self.data = out

    def inspect(self):
        pats = self.patterns()
        L = [f"PATTERN BANK 1.0 — {len(pats)} patterns "
             f"(96 PPQN, 4/4 bar = 384 ticks)"]
        for i, p in enumerate(pats):
            L.append(f"-- {i}: {p['name']}  ({p['bars']} bar"
                     f"{'s' if p['bars'] != 1 else ''}, "
                     f"{len(p['events'])} notes, flag 0x{p['flag']:02X})")
            for e in p["events"]:
                bar, r = divmod(e["time"], 4 * self.PPQN)
                beat, tick = divmod(r, self.PPQN)
                L.append(f"    {bar+1}.{beat+1}.{tick:02d}  "
                         f"pad {pad_label(e['pad'])}  "
                         f"{note_name(e['note'])}({e['note']})  "
                         f"vel {e['vel']:3d}  gate {e['dur']}t")
        return "\n".join(L)


# ══════════════════════════════════════════════════════ JJOS2XL template ═══
# Byte-exact defaults observed across owner's real JJOS2XL saves
# (see _planning/pgm_re/FINDINGS.md). MIXXXCTRL02.PGM is the reference
# "blank" program; these constants are verified against it in the tests.

_PAD_DEFAULT = bytes.fromhex(
    "00 00 00 00 00 00 00 00 00 64 14 32 1e 1e 28 65"
    "00 00 64 00 00 00 64 0a 00 00 64 00 24 63 00 0a"
    "00 00 1e 00 00 00 00 00 00 00 00 00 00 00 00 64"
    "32 00 00 21 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00".replace(" ", ""))
# Empty-slot defaults: level 80, range 0-127. play_mode byte is 0 here to
# match MIXXXCTRL02 (the blank reference); other real saves have 1 there —
# the MPC accepts both, and used slots always set it explicitly.
_SLOT_DEFAULT = bytes(16) + bytes.fromhex("0050007f00000000")
_SLIDER_FOOTER_DEFAULT = bytes.fromhex(
    "000000010088" + "78000b00001e000100" + "88"
    + "78000b00001e000100" + "88" + "78000b00001e000100" + "88"
    + "78000b00001e00")


def blank_jjos(inst=False):
    """A byte-faithful default JJOS2XL program (what the MPC shows as new)."""
    d = bytearray(Program.SIZE)
    struct.pack_into("<H", d, 0, Program.SIZE)
    d[0x04:0x14] = b"MPC1000 PGM 6.00"
    d[0x16] = 0x06
    d[0x17] = 0x01 if inst else 0x00
    for p in range(64):
        base = Program.PAD_BASE + p * Program.PAD_STRIDE
        for s in range(3):
            d[base + s * 0x18: base + (s + 1) * 0x18] = _SLOT_DEFAULT
        d[base + 3 * 0x18 + 1] = 0x15 if p == 0 else 0x6D   # JJOS constant
        d[base + 0x60: base + 0xA4] = _PAD_DEFAULT
    d[0x2918:0x2958] = bytes(range(36, 100))                # pad -> note
    npad = bytearray([64] * 128)
    for p in range(64):
        npad[36 + p] = p
    d[0x2958:0x29D8] = npad                                 # note -> pad
    d[0x29D9:0x2A04] = _SLIDER_FOOTER_DEFAULT
    return Program(bytes(d))


# ═══════════════════════════════════════════════════ WAV smpl root notes ═══

def wav_info(path):
    """(channels, rate, bits, frames, root_note_or_None) without loading data."""
    d = open(path, "rb").read(4096)
    full = None
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    ch = rate = bits = frames = None
    root = None
    pos = 12
    data = open(path, "rb").read()
    while pos + 8 <= len(data):
        cid = data[pos:pos+4]
        sz = struct.unpack_from("<I", data, pos+4)[0]
        if cid == b"fmt ":
            ch, rate = struct.unpack_from("<HI", data, pos+10)
            bits = struct.unpack_from("<H", data, pos+22)[0]
        elif cid == b"data":
            if ch and bits:
                frames = sz // (ch * bits // 8)
        elif cid == b"smpl":
            root = struct.unpack_from("<I", data, pos+8+12)[0]
        pos += 8 + sz + (sz & 1)
    return ch, rate, bits, frames, root


def _jjos_smpl_chunk(rate, start, end, note, loops_block=b""):
    """A byte-exact JJOS2XL-shaped smpl chunk.

    Matched byte-for-byte against owner's MPC-written WAVs (6STRBA_*,
    FINDINGS sessions 8-9, 11): manufacturer 0x01000047 (Akai), product 0x5E
    (MPC1000), samplePeriod = int(1e9/rate) — TRUNCATED (22675 at 44.1k, not
    22676 from round()); cbSamplerData = 20 carrying {u16 2, u16 60, 8x00,
    u32 start, u32 end} — confirmed from Session 11 loop RE (end at offset 16,
    not 14 as originally coded — JJOS uses HH8xII not HH6xII2x). start/end are
    the MPC's sample edit points. Loop records ride in loops_block.

    Loop record format (24 bytes each, FINDINGS session 11 from live JJOS save):
      cuePointID = 0x706F6F6C ('loop' ASCII LE), type = 2 (crossfade loop;
      regular forward loop presumed type=0 but not yet confirmed), start/end in
      frames, fraction=0, playCount=0 (infinite).

    CRITICAL: this chunk MUST sit BEFORE the data chunk (see set_wav_root).
    JJOS2XL ignores a smpl chunk that follows data."""
    sd = struct.pack("<HH8xII", 2, 60, int(start), int(end))
    numloops = len(loops_block) // 24
    body = struct.pack("<9I", 0x01000047, 0x5E,
                       int(1_000_000_000 / rate), note,
                       0, 0, 0, numloops, len(sd)) + loops_block + sd
    return struct.pack("<4sI", b"smpl", len(body)) + body


def _jjos_loop_record(start, end, loop_type=2):
    """24-byte RIFF smpl loop record in JJOS2XL's observed format.

    loop_type: 2 = crossfade (confirmed from Session 11 live MPC save).
    0 = forward loop (standard RIFF, not yet hardware-confirmed for JJOS).
    cuePointID 'loop' is what JJOS writes (0x706F6F6C LE)."""
    return struct.pack("<6I",
                       0x706F6F6C,   # cuePointID = b'loop'
                       loop_type,
                       int(start), int(end),
                       0,            # fraction
                       0)            # playCount (0 = infinite)


def set_wav_root(path, note, out=None):
    """Write/update the smpl chunk so JJOS2XL INST mode knows the root key.

    JJOS2XL only reads a smpl chunk placed BEFORE the data chunk, so the chunk
    is always (re)inserted immediately before data — this is the fix for the
    stacked-keygroup bug (FINDINGS session 9). Existing sample edit points
    (samplerData start/end) and loop records are preserved; the unity note and
    samplePeriod are (re)written correctly."""
    data = bytearray(open(path, "rb").read())
    ch, bits, rate, frames = 1, 16, 44100, 0
    smpl_at = smpl_sz = None
    pos = 12
    while pos + 8 <= len(data):
        cid = bytes(data[pos:pos+4])
        sz = struct.unpack_from("<I", data, pos+4)[0]
        if cid == b"fmt ":
            ch, rate = struct.unpack_from("<HI", data, pos+10)
            bits = struct.unpack_from("<H", data, pos+22)[0]
        elif cid == b"data":
            frames = sz // max(ch * bits // 8, 1)
        elif cid == b"smpl":
            smpl_at, smpl_sz = pos, sz
        pos += 8 + sz + (sz & 1)
    # preserve edit points / loops from an existing chunk, if present
    start, end, loops_block = 0, max(frames - 1, 0), b""
    if smpl_at is not None:
        numloops, cb = struct.unpack_from("<II", data, smpl_at + 8 + 28)
        p = smpl_at + 8 + 36
        if numloops > 0:
            loops_block = bytes(data[p:p + 24 * numloops]); p += 24 * numloops
        sd = bytes(data[p:p + cb])
        if len(sd) >= 20:
            start, end = struct.unpack_from("<II", sd, 12)  # HH8xII: start@12, end@16
        # excise the old chunk — its position and/or shape may be wrong
        del data[smpl_at: smpl_at + 8 + smpl_sz + (smpl_sz & 1)]
    new = _jjos_smpl_chunk(rate, start, end, note, loops_block)
    # insert immediately before the data chunk
    pos, ins = 12, len(data)
    while pos + 8 <= len(data):
        cid = bytes(data[pos:pos+4])
        sz = struct.unpack_from("<I", data, pos+4)[0]
        if cid == b"data":
            ins = pos; break
        pos += 8 + sz + (sz & 1)
    data[ins:ins] = new
    struct.pack_into("<I", data, 4, len(data) - 8)
    write_file(out or path, bytes(data))


_NOTE_RE = None

def note_from_name(name):
    """Parse a trailing note name out of a filename: '6STRBA_-G#2' -> 56.
    Convention: MIDI = (octave + 2) * 12 + semitone (C-2 = 0), matching the
    smpl chunks in owner's real multisample WAVs (B1 -> 47, D2 -> 50)."""
    global _NOTE_RE
    import re
    if _NOTE_RE is None:
        _NOTE_RE = re.compile(r"([A-Ga-g])([#b]?)(-?\d)\s*$")
    m = _NOTE_RE.search(name)
    if not m:
        return None
    semi = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[m.group(1).upper()]
    semi += {"#": 1, "b": -1, "": 0}[m.group(2)]
    midi = (int(m.group(3)) + 2) * 12 + semi
    return midi if 0 <= midi <= 127 else None


# ═══════════════════════════════════════════════════════ program builders ═══

def build_inst(wav_paths, prog_name=""):
    """Folder of per-note WAVs -> JJOS2XL INST program.

    Root note per WAV: existing smpl chunk, else parsed from filename
    (e.g. 'JUNO-C3.wav'); smpl chunk is written back into the WAV either way
    (the MPC derives keygroups from it). Samples land on consecutive pads
    sorted by root note, tune=0, full range — exactly like 6strBAx.PGM.
    Returns (Program, notes, warnings)."""
    warnings, entries = [], []
    for w in wav_paths:
        base = os.path.splitext(os.path.basename(w))[0]
        name16 = sanitize_name(base)
        if name16 != base:
            warnings.append(f"{base}: name adjusted to '{name16}' (MPC rules)")
        ch, rate, bits, frames, root = wav_info(w)
        if rate != 44100:
            warnings.append(f"{base}: {rate} Hz (MPC1000 native is 44100)")
        if bits != 16:
            warnings.append(f"{base}: {bits}-bit (MPC1000 wants 16-bit)")
        if root is None:
            root = note_from_name(base)
            if root is None:
                warnings.append(f"{base}: SKIPPED — no smpl chunk and no "
                                f"note name in filename")
                continue
        set_wav_root(w, root)                    # ensure the chunk exists
        entries.append((root, name16))
    if not entries:
        raise ValueError("no usable WAVs")
    if len(entries) > 64:
        raise ValueError(f"{len(entries)} samples > 64 pads")
    entries.sort()                               # by root note, low to high
    prog = blank_jjos(inst=True)
    ranks = {n: i + 1 for i, n in enumerate(sorted(nm for _, nm in entries))}
    roots = [e[0] for e in entries]
    n = len(roots)
    for pad, (root, nm) in enumerate(entries):
        o = prog._slot(pad, 0)
        prog.data[o:o+16] = nm.encode("ascii").ljust(16, b"\x00")
        prog.data[o+0x11] = 100                  # level
        # Split points: midpoint between adjacent root notes.
        # INST programs store LOW/HIGH in pad params (+0x1C/+0x1D), not the
        # slot bytes (+0x12/+0x13) — confirmed by diffing 6strBAx vs defaults.
        lo = 0   if pad == 0     else (roots[pad - 1] + roots[pad]) // 2 + 1
        hi = 127 if pad == n - 1 else (roots[pad] + roots[pad + 1]) // 2
        prog.data[o+0x12], prog.data[o+0x13] = lo, hi   # slot bytes (harmless)
        pb = prog._padblk(pad)
        prog.data[pb + 0x1C] = lo               # INST LOW  (what JJOS reads)
        prog.data[pb + 0x1D] = hi               # INST HIGH (what JJOS reads)
        prog.data[o+0x16] = 1                    # note-on
        prog.data[pb] = ranks[nm]                # sample-memory rank
    return prog, [e[0] for e in entries], warnings


def build_drum(wav_paths, one_shot=True):
    """Folder of one-shots -> drum program, pads A01.. in order."""
    warnings = []
    if len(wav_paths) > 64:
        raise ValueError("more than 64 samples")
    prog = blank_jjos(inst=False)
    names = []
    for w in wav_paths:
        base = os.path.splitext(os.path.basename(w))[0]
        nm = sanitize_name(base)
        if nm != base:
            warnings.append(f"{base}: name adjusted to '{nm}'")
        ch, rate, bits, frames, _ = wav_info(w)
        if rate != 44100:
            warnings.append(f"{base}: {rate} Hz (MPC1000 native is 44100)")
        names.append(nm)
    ranks = {n: i + 1 for i, n in enumerate(sorted(names))}
    for pad, nm in enumerate(names):
        o = prog._slot(pad, 0)
        prog.data[o:o+16] = nm.encode("ascii").ljust(16, b"\x00")
        prog.data[o+0x11] = 100
        prog.data[o+0x12], prog.data[o+0x13] = 0, 127
        prog.data[o+0x16] = 0 if one_shot else 1
        prog.data[prog._padblk(pad)] = ranks[nm]
    return prog, warnings


# ═══════════════════════════════════════════════════════════════ helpers ═══

VALID = set(" !#$%&'()-@_{}")

def sanitize_name(name):
    """MPC sample-name rules: <=16 chars, ASCII, alnum + limited punctuation.
    Same function the ORC autosampler will use, so names can't drift."""
    out = "".join(c if (c.isalnum() or c in VALID) else "_" for c in name)
    return out[:16]

def diff_files(pa, pb):
    a, b = open(pa, "rb").read(), open(pb, "rb").read()
    if len(a) != len(b):
        print(f"sizes differ: {len(a)} vs {len(b)}"); return
    runs, i = [], 0
    while i < len(a):
        if a[i] != b[i]:
            j = i
            while j < len(a) and (a[j] != b[j]
                    or (j + 2 < len(a) and a[j+1:j+3] != b[j+1:j+3])):
                j += 1
            runs.append((i, j)); i = j
        else:
            i += 1
    print(f"{sum(j - i for i, j in runs)} bytes differ in {len(runs)} region(s)")
    for i, j in runs:
        print(f"  0x{i:04X}-0x{j-1:04X}  A: {a[i:j].hex(' ')[:60]}")
        print(f"                B: {b[i:j].hex(' ')[:60]}")
        if len(a) == Program.SIZE:
            for off in range(i, j):
                loc = locate_pgm_offset(off)
                if loc: print(f"      0x{off:04X}: {loc}  {a[off]} -> {b[off]}")

def locate_pgm_offset(off):
    if 0x14 <= off < 0x18:
        return f"header flag byte 0x{off:02X}"
    if Program.PAD_BASE <= off < 0x2918:
        rel = off - Program.PAD_BASE
        p, r = divmod(rel, Program.PAD_STRIDE)
        if r < 0x60:
            s, sr = divmod(r, 0x18)
            fld = {0x11: "level", 0x12: "range_lo", 0x13: "range_hi",
                   0x14: "tuning.lo", 0x15: "tuning.hi", 0x16: "play_mode",
                   0x17: "chop"}.get(sr, f"name[{sr}]" if sr < 16 else f"+0x{sr:02X}")
            return f"pad {pad_label(p)} slot{s} {fld}"
        fld = Program.PAD_FIELDS.get(r - 0x60,
              "sample_rank" if r == 0x60 else f"+0x{r - 0x60:02X}")
        return f"pad {pad_label(p)} param {fld}"
    if 0x2918 <= off < 0x2958: return f"pad->note[{pad_label(off - 0x2918)}]"
    if 0x2958 <= off < 0x29D8: return f"note->pad[note {off - 0x2958}]"
    if off >= 0x29D9: return f"slider/footer +0x{off - 0x29D9:02X}"
    return None

def open_any(path):
    with open(path, "rb") as f:
        head = f.read(0x20)
    if len(head) >= 0x13 and head[0x04:0x13] == PatternBank.MAGIC:
        return PatternBank.load(path)
    size = os.path.getsize(path)
    if size == Program.SIZE: return Program.load(path)
    if size == PadMode.SIZE: return PadMode.load(path)
    raise ValueError(f"unrecognized file (size {size})")


# ═══════════════════════════════════════════════════════════════════ CLI ═══

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inspect"); sp.add_argument("file")
    sp = sub.add_parser("diff"); sp.add_argument("a"); sp.add_argument("b")

    sp = sub.add_parser("pmd-set", help="edit pad(s) in a PMD slot")
    sp.add_argument("file")
    sp.add_argument("--slot", type=int, default=0)
    sp.add_argument("--pad", required=True,
                    help="A01, C12, or range A01-A16")
    sp.add_argument("--type", help="note|cc|pc or numeric")
    sp.add_argument("--num", type=int)
    sp.add_argument("--value", type=int)
    sp.add_argument("--flag", type=int)
    sp.add_argument("--ramp", help="LO:HI — spread values across the pad range")
    sp.add_argument("-o", "--out", help="output path (default: in place)")

    sp = sub.add_parser("pmd-copy-slot")
    sp.add_argument("file")
    sp.add_argument("--src", dest="src", type=int, required=True)
    sp.add_argument("--dst", dest="dst", type=int, required=True)
    sp.add_argument("-o", "--out")

    sp = sub.add_parser("build-inst",
                        help="folder of per-note WAVs -> INST .PGM")
    sp.add_argument("folder")
    sp.add_argument("-o", "--out", help="output .PGM (default <folder>.PGM)")

    sp = sub.add_parser("build-drum",
                        help="folder of one-shot WAVs -> drum .PGM")
    sp.add_argument("folder")
    sp.add_argument("-o", "--out")
    sp.add_argument("--note-on", action="store_true",
                    help="gated instead of one-shot")

    sp = sub.add_parser("set-root", help="write root note into a WAV")
    sp.add_argument("wav")
    sp.add_argument("note", help="MIDI number or name like C3 / G#2")

    a = ap.parse_args()

    if a.cmd == "inspect":
        print(open_any(a.file).inspect())

    elif a.cmd == "diff":
        diff_files(a.a, a.b)

    elif a.cmd == "pmd-set":
        pm = PadMode.load(a.file)
        if "-" in a.pad:
            lo, hi = (parse_pad(x) for x in a.pad.split("-"))
            pads = list(range(lo, hi + 1))
        else:
            pads = [parse_pad(a.pad)]
        t = None
        if a.type is not None:
            t = PadMode.TYPE_IDS.get(a.type.lower(),
                                     int(a.type) if a.type.isdigit() else None)
            if t is None: sys.exit(f"unknown type {a.type}")
        vals = [a.value] * len(pads)
        if a.ramp:
            lo, hi = (int(x) for x in a.ramp.split(":"))
            n = max(len(pads) - 1, 1)
            vals = [round(lo + (hi - lo) * i / n) for i in range(len(pads))]
        for pd, vv in zip(pads, vals):
            pm.set(a.slot, pd, type=t, num=a.num, value=vv, flag=a.flag)
        out = a.out or a.file
        pm.save(out)
        print(f"wrote {out}")
        print(pm.inspect())

    elif a.cmd == "pmd-copy-slot":
        pm = PadMode.load(a.file)
        pm.copy_slot(a.src, a.dst)
        out = a.out or a.file
        pm.save(out)
        print(f"slot {a.src} -> {a.dst}, wrote {out}")

    elif a.cmd in ("build-inst", "build-drum"):
        import glob as _g
        wavs = sorted(_g.glob(os.path.join(a.folder, "*.wav"))
                      + _g.glob(os.path.join(a.folder, "*.WAV")))
        if not wavs:
            sys.exit("no WAVs in folder")
        if a.cmd == "build-inst":
            prog, notes, warns = build_inst(wavs)
            info = f"{len(notes)} samples, {note_name(notes[0])}..{note_name(notes[-1])}"
        else:
            prog, warns = build_drum(wavs, one_shot=not a.note_on)
            info = f"{len(wavs)} pads"
        for w in warns:
            print("  !", w)
        out = a.out or os.path.join(
            a.folder, sanitize_name(os.path.basename(os.path.abspath(a.folder))) + ".PGM")
        prog.save(out)
        print(f"wrote {out}  ({info})")

    elif a.cmd == "set-root":
        note = int(a.note) if a.note.isdigit() else note_from_name(a.note)
        if note is None:
            sys.exit(f"can't parse note {a.note}")
        set_wav_root(a.wav, note)
        print(f"{a.wav}: root = {note} ({note_name(note)})")


if __name__ == "__main__":
    main()
