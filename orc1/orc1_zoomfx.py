#!/usr/bin/env python3
"""orc1_zoomfx -- a Zoom MultiStomp (MS-70CDR; MS-50G / MS-60B untested) as an ORC module: "zoomfx".

Throw plain CCs at the module's channel and the pedal follows. Bind "zoomfx" to a channel on
/matrix like any module; this process reads that channel off the ORC1-Synth-Bus (ALSA fans the
bus out to every reader, like the module hosts and the VISUALS renderer) and speaks the pedal's
SysEx for you. It OWNS the pedal's USB port: the brain lists "zoom ms" in PATCHBAY_PORT_PREFIXES,
so it never opens the pedal (it used to forward the pedal's SysEx out the pisound DIN).

CC map on the bound channel (effect slots are the pedal's 1-6, left to right):
    slot 1: CC 20 on/off (>= 64 on), CC 21-29 knob 1-9
    slot 2: CC 30, 31-39      slot 3: CC 40, 41-49      slot 4: CC 50, 51-59
    slot 5: CC 70, 71-79      slot 6: CC 80, 81-89      (60-69 skipped: sustain/pedal CCs)
    program change 0-49 = patch 1-50
    CC 90 = tuner (the pedal's CC 74), CC 91 = tuner, other mode (the pedal's CC 75), >= 64 on
    CC 92 = STORE the current patch into its own memory slot (fires on the rise through 64)
A knob CC 0-127 is scaled to that knob's real range for whatever effect sits in the slot
(zoomfx_params.json, read out of the effect files); an effect without a table gets the raw value
(the pedal clamps to its max). A CC aimed at an EMPTY slot is dropped (never tested on the pedal).
Fast sweeps are coalesced: only the newest value of each knob is sent.

Slots 4-6 need the 6SLOT-EDIT firmware patch (zoom ms70cdr tools/FORMAT.md); on stock firmware
the pedal silently ignores edits to slots 4-6.

The pedal side (all MEASURED on owner's MS-70CDR, 2026-10-13):
    F0 52 00 <dev> 50 F7                 editor mode on: the pedal reports ITS OWN knob turns as `31`
                                         and patch changes as a full `28` dump
    F0 52 00 <dev> 31 s p lsb msb F7     edit slot s param p (0 = on/off, 2.. = knobs)
    F0 52 00 <dev> 29 F7 -> 28 <packed>  read the edit buffer (6 x 18-byte effect blocks, tempo, name)
    F0 52 00 <dev> 32 01 00 00 n 0 0 0 0 0 F7   store the edit buffer into patch n
The live state goes to /dev/shm/orc1_zoomfx.json (for a /zoom page or the HUD).

    python3 orc1_zoomfx.py          run forever (the systemd unit does this)
"""
import json
import os
import threading
import time

PORT_MATCH = "zoom ms"                 # ALSA name "ZOOM MS Series:ZOOM MS Series MIDI 1 40:0"
BUS_MATCH = "orc1-synth-bus"
MODULE = "zoomfx"
BINDINGS = os.path.expanduser("~/.orc1_bindings.json")
STATE_PATH = "/dev/shm/orc1_zoomfx.json"
PARAMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zoomfx_params.json")

SLOT_BASES = (20, 30, 40, 50, 70, 80)  # slot n on/off = base, knob k = base + k
CC_TUNER, CC_TUNER2, CC_STORE = 90, 91, 92
DEVICES = {0x61: ("MS-70CDR", 6), 0x58: ("MS-50G", 6), 0x5F: ("MS-60B", 4)}
PATCH_LEN = 122                        # unpacked bytes of an MS-50G/70CDR patch
SEND_GAP_S = 0.004                     # spacing between edits to the pedal (MEASURE before lowering)
BIND_REREAD_S = 2.0
RESCAN_S = 2.0


def log(msg):
    print(f"[zoomfx] {msg}", flush=True)


# ---------------------------------------------------------------- the patch format

def unpack7(data):
    """Zoom 7-in-8: each group = 1 byte of high bits (bit 6 -> first byte) + up to 7 bytes."""
    out = []
    for i in range(0, len(data), 8):
        hi = data[i]
        for k, b in enumerate(data[i + 1:i + 8]):
            out.append(b | (0x80 if hi & (1 << (6 - k)) else 0))
    return bytes(out)


def pack7(data):
    out = []
    for i in range(0, len(data), 7):
        grp = data[i:i + 7]
        out.append(sum(1 << (6 - k) for k, b in enumerate(grp) if b & 0x80))
        out.extend(b & 0x7F for b in grp)
    return out


# (shift, width) of P0..P8 inside one effect block's little-endian bitfield
PARAM_BITS = [(29, 13), (42, 13), (55, 13)] + [(68 + 8 * k, 8) for k in range(5)] + [(128, 8)]


def parse_patch(u):
    """Unpacked 122-byte patch -> {fx: [{on, id, params[9]}] x6, tempo, name}."""
    fx = []
    for s in range(6):
        n = int.from_bytes(u[18 * s:18 * s + 18], "little")
        fx.append(dict(on=n & 1, id=(n >> 1) & ((1 << 28) - 1),
                       params=[(n >> sh) & ((1 << w) - 1) for sh, w in PARAM_BITS]))
    m = u[108:111]
    return dict(fx=fx, tempo=((m[1] & 0xE0) >> 5) | ((m[2] & 0x1F) << 3),
                name=bytes(u[111:121]).decode("ascii", "replace").rstrip("\x00 "))


def load_params(path=PARAMS_PATH):
    """{effect id int: (name, [(param name, max), ...])} -- empty on any error."""
    try:
        with open(path) as f:
            raw = json.load(f)
        return {int(k, 16): (v[0], [tuple(p) for p in v[1]]) for k, v in raw.items()}
    except (OSError, ValueError, TypeError, IndexError):
        return {}


# ---------------------------------------------------------------- CC -> action

def cc_action(cc, value, patch, params, slots=6):
    """One CC on the module's channel -> what to do, or None.
    ("edit", slot 0-5, param, value) | ("tuner", pedal_cc, value) | ("store",) -- STORE is
    edge-triggered by the caller."""
    if cc == CC_TUNER:
        return ("tuner", 74, value)
    if cc == CC_TUNER2:
        return ("tuner", 75, value)
    if cc == CC_STORE:
        return ("store",) if value >= 64 else None
    for slot, base in enumerate(SLOT_BASES[:slots]):
        if base <= cc <= base + 9:
            fx = patch["fx"][slot] if patch else None
            if not fx or not fx["id"]:
                return None                    # empty slot (or no patch read yet): drop
            k = cc - base
            if k == 0:
                return ("edit", slot, 0, 1 if value >= 64 else 0)
            info = params.get(fx["id"])
            if info is not None:
                if k > len(info[1]):
                    return None                # the effect has no such knob
                mx = info[1][k - 1][1]
                return ("edit", slot, k + 1, (value * mx + 63) // 127)
            return ("edit", slot, k + 1, value)
    return None


def edit_msg(dev, slot, param, value):
    return [0xF0, 0x52, 0x00, dev, 0x31, slot, param, value & 0x7F, (value >> 7) & 0x7F, 0xF7]


def sx(dev, *body):
    return [0xF0, 0x52, 0x00, dev] + list(body) + [0xF7]


# ---------------------------------------------------------------- the pedal link

class ZoomLink:
    """Owns the pedal's port: connects by name, re-connects after a replug, keeps the live patch
    (from our own edits AND the pedal's reports), and sends coalesced edits."""

    def __init__(self, mido_mod, params, clock=time.monotonic):
        self.mido = mido_mod
        self.params = params
        self.clock = clock
        self.lock = threading.Lock()
        self.out = self.inp = None
        self.port_name = None
        self.dev = None
        self.model, self.slots = None, 6
        self.patch = None                       # parsed edit buffer
        self.program = None
        self.pending = {}                       # (slot, param) -> value, newest wins
        self.other = []                         # queued non-edit messages (bytes lists)
        self.dirty = True                       # state file needs a rewrite
        self._store_high = False
        self._want_patch = 0.0                  # time to re-read the edit buffer (0 = no)

    # -- connection
    def find_port(self):
        names = self.mido.get_output_names()
        return next((n for n in names if PORT_MATCH in n.lower()), None)

    def connect(self):
        name = self.find_port()
        if not name:
            return False
        iname = next((n for n in self.mido.get_input_names() if PORT_MATCH in n.lower()), None)
        if not iname:
            return False
        self.out = self.mido.open_output(name)
        self.inp = self.mido.open_input(iname, callback=self._on_pedal)
        self.port_name = name
        self.out.send(self.mido.Message("sysex", data=[0x7E, 0x00, 0x06, 0x01]))  # identity
        t_end = self.clock() + 1.5
        while self.dev is None and self.clock() < t_end:
            time.sleep(0.02)
        if self.dev is None:
            log(f"{name}: no identity reply, assuming MS-70CDR")
            self.dev, self.model, self.slots = 0x61, "MS-70CDR", 6
        self._send(sx(self.dev, 0x50))          # editor mode: the pedal reports its own edits
        self._send(sx(self.dev, 0x33))          # current program
        self._send(sx(self.dev, 0x29))          # current patch
        log(f"connected: {self.model} on {name}")
        self.dirty = True
        return True

    def drop(self):
        for p in (self.inp, self.out):
            try:
                p and p.close()
            except Exception:
                pass
        self.out = self.inp = self.port_name = None
        self.dev = self.patch = self.program = None
        self.dirty = True

    def still_there(self):
        return self.port_name is not None and self.port_name in self.mido.get_output_names()

    # -- pedal -> us
    def _on_pedal(self, msg):
        if msg.type == "program_change":
            self.program = msg.program
            self._want_patch = self.clock() + 0.15
            self.dirty = True
            return
        if msg.type != "sysex":
            return
        d = list(msg.data)
        if d[:4] == [0x7E, 0x00, 0x06, 0x02] and len(d) >= 6 and d[4] == 0x52:
            self.dev = d[5]
            self.model, self.slots = DEVICES.get(self.dev, (f"Zoom {self.dev:#04x}", 6))
            return
        if d[:3] != [0x52, 0x00, self.dev] or len(d) < 4:
            return
        cmd = d[3]
        with self.lock:
            if cmd == 0x28:
                u = unpack7(d[4:])
                if len(u) >= PATCH_LEN:
                    self.patch = parse_patch(u[:PATCH_LEN])
                    self.dirty = True
            elif cmd == 0x31 and len(d) >= 8 and self.patch:   # the pedal's own knob turn
                slot, p, v = d[4], d[5], d[6] | d[7] << 7
                if 0 <= slot < 6:
                    if p == 0:
                        self.patch["fx"][slot]["on"] = v & 1
                    elif 2 <= p <= 10:
                        self.patch["fx"][slot]["params"][p - 2] = v
                    self.dirty = True

    # -- us -> pedal
    def _send(self, data):
        if self.out is not None:
            self.out.send(self.mido.Message.from_bytes(data))

    def handle_cc(self, cc, value):
        if self.dev is None:
            return
        if cc == CC_STORE:                      # edge: fire once per rise through 64
            hi = value >= 64
            fire, self._store_high = hi and not self._store_high, hi
            if fire and self.program is not None:
                with self.lock:
                    self.other.append(sx(self.dev, 0x32, 0x01, 0, 0, self.program, 0, 0, 0, 0, 0))
            return
        with self.lock:
            act = cc_action(cc, value, self.patch, self.params, self.slots)
            if not act:
                return
            if act[0] == "edit":
                _, slot, p, v = act
                self.pending[(slot, p)] = v
                fx = self.patch["fx"][slot]
                if p == 0:
                    fx["on"] = v
                else:
                    fx["params"][p - 2] = v
                self.dirty = True
            elif act[0] == "tuner":
                self.other.append([0xB0, act[1], act[2]])

    def handle_program(self, program):
        if self.dev is None or not 0 <= program < 50:
            return
        with self.lock:
            self.other.append([0xC0, program])
        self.program = program
        self._want_patch = self.clock() + 0.15
        self.dirty = True

    def flush(self):
        """Send everything queued, spaced by SEND_GAP_S. Called from the link loop."""
        with self.lock:
            other, self.other = self.other, []
            pend, self.pending = self.pending, {}
        for m in other:
            self._send(m)
            time.sleep(SEND_GAP_S)
        for (slot, p), v in pend.items():
            self._send(edit_msg(self.dev, slot, p, v))
            time.sleep(SEND_GAP_S)
        if self._want_patch and self.clock() >= self._want_patch:
            self._want_patch = 0.0
            self._send(sx(self.dev, 0x29))

    # -- state for pages / HUD
    def state(self, channel):
        fx = []
        for s, e in enumerate((self.patch or {}).get("fx", [])):
            info = self.params.get(e["id"])
            names = info[1] if info else []
            fx.append(dict(slot=s + 1, on=e["on"], id=f"{e['id']:08X}",
                           name=info[0] if info else ("" if not e["id"] else "?"),
                           params=[dict(name=n, max=mx, value=e["params"][i])
                                   for i, (n, mx) in enumerate(names)]))
        return dict(connected=self.dev is not None, device=self.model, channel=channel,
                    program=self.program, patch=(self.patch or {}).get("name"),
                    tempo=(self.patch or {}).get("tempo"), fx=fx, t=time.time())


# ---------------------------------------------------------------- the module channel

class Binding:
    """The zoomfx module's channel (0-based) from ~/.orc1_bindings.json, re-read so /matrix
    moves apply live. None = unbound = the pedal is left alone."""

    def __init__(self, path=BINDINGS, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.ch, self._t = None, -1e9

    def channel(self):
        now = self.clock()
        if now - self._t >= BIND_REREAD_S:
            self._t = now
            try:
                with open(self.path) as f:
                    b = json.load(f).get("bindings", {})
                self.ch = next((int(c) - 1 for c, m in b.items() if m == MODULE), None)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return self.ch


def on_bus(msg, binding, link):
    ch = binding.channel()
    if ch is None or getattr(msg, "channel", None) != ch:
        return
    if msg.type == "control_change":
        link.handle_cc(msg.control, msg.value)
    elif msg.type == "program_change":
        link.handle_program(msg.program)


def write_state(st, path=STATE_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def main():
    import mido
    params = load_params()
    log(f"{len(params)} effect tables loaded")
    link, binding = ZoomLink(mido, params), Binding()

    def bus_thread():
        while True:
            name = next((n for n in mido.get_input_names() if BUS_MATCH in n.lower()), None)
            if not name:
                time.sleep(3)
                continue
            log(f"bus: {name}")
            try:
                with mido.open_input(name) as port:
                    for msg in port:
                        on_bus(msg, binding, link)
            except Exception as e:
                log(f"bus error: {e}, retrying")
                time.sleep(2)

    threading.Thread(target=bus_thread, daemon=True).start()
    last_scan = last_state = 0.0
    last_ch = None
    while True:
        now = time.monotonic()
        ch = binding.channel()                  # a /matrix move must reach the state file too
        if ch != last_ch:
            last_ch, link.dirty = ch, True
        if now - last_scan >= RESCAN_S:
            last_scan = now
            try:
                if link.port_name and not link.still_there():
                    log("pedal gone")
                    link.drop()
                if link.port_name is None:
                    link.connect()
            except Exception as e:
                log(f"connect error: {e}")
                link.drop()
        if link.dev is not None:
            try:
                link.flush()
            except Exception as e:
                log(f"send error: {e}")
                link.drop()
        if link.dirty and now - last_state >= 0.1:
            last_state, link.dirty = now, False
            try:
                write_state(link.state(None if binding.ch is None else binding.ch + 1))
            except OSError:
                pass
        time.sleep(0.005)


if __name__ == "__main__":
    main()
