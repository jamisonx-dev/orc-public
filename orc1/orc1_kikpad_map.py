#!/usr/bin/env python3
"""orc1_kikpad_map -- edit the KIKPAD's own pad + encoder maps (they live ON THE PAD).

owner (2026-10-15): the maps must work standalone, e.g. with a host box driving
the MPC, so they are stored in the pad's flash by its firmware
(smartpad-kikpad `mod_kikpad_remap.h`). This is only the EDITOR: the brain speaks
the firmware's SysEx on the pad's private port :1 (the port it already claims for
bank 2), and publishes what the pad REPLIED to a state file the /kikpad page
reads. The pad is the truth; the file is a read-back cache, rebuilt on attach.

Wire (both ways, on port :1):  F0 7D 4B 50 <cmd> <args> F7
    01            hello            -> 01 proto banks modes pads
    02 b i        get pad          -> 02 b i type number channel colour
    03 b i t n c col  set pad      -> 02 ...  (what was stored)
    04 m e        get encoder      -> 04 m e cc channel
    05 m e cc ch  set encoder      -> 04 ...
    06 b          get bank channel -> 06 b channel
    07 b ch       set bank channel -> 06 ...
    08 what       reset            -> 08 what  (0 / 1 bank, 2 encoders + bank channels, 127 all)
    09            save state       -> 09 dirty (1 = waiting for the pad's autosave)
type 0 off / 1 note / 2 cc / 3 toggle; pad channel 0-15, 16 = the bank's channel.

Pure stdlib; the brain passes in a send function (mido) and feeds replies to
on_sysex() from the pad's reader thread. Tests: _test_kikpad_map.py.
"""
import json
import os
import queue
import threading
import time

HDR = (0x7D, 0x4B, 0x50)
TYPES = ("off", "note", "cc", "toggle")
BANKS = 2
MODES = 8
PADS = 64
CH_BANK = 16
STATE_FILE = "/tmp/orc1_kikpad_map.json"     # tmpfs: a read-back cache, the pad is the truth
TIMEOUT_S = 0.25
# The bottom-row buttons that pick an encoder mode, in firmware order (BT_VOLUME .. BT_CONTROL4)
MODE_NAMES = ("VOLUME", "SEND A", "SEND B", "PAN", "CONTROL1", "CONTROL2", "CONTROL3", "CONTROL4")
BANK_NAMES = ("Bank 0 (red, MPC face)", "Bank 1 (green, bass 4ths)")


def body(data):
    """The command + args of a pad SysEx (mido's data: no F0/F7), or None if it is not ours."""
    d = list(data or ())
    if len(d) < 4 or tuple(d[:3]) != HDR:
        return None
    return d[3:]


def colour_rgb(c):
    """The pad palette (rgbRGB: capitals = 2/3 intensity, low bits; lowercase = 1/3, high bits) as #rrggbb."""
    c = int(c) & 0x3F
    r = (c >> 2 & 1) * 170 + (c >> 5 & 1) * 85
    g = (c >> 1 & 1) * 170 + (c >> 4 & 1) * 85
    b = (c & 1) * 170 + (c >> 3 & 1) * 85
    return "#%02x%02x%02x" % (r, g, b)


def _int(v, lo, hi, what):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: not a number")
    if not lo <= n <= hi:
        raise ValueError(f"{what}: {n} is outside {lo}-{hi}")
    return n


def web_command(d):
    """The /kikpad POST body -> (brain action, payload). Every validation lives here, so the
    tests check exactly what the endpoint sends. Raises ValueError for anything malformed.
    A "job" string (the page's id for this click) rides along and comes back in state.last."""
    if not isinstance(d, dict):
        raise ValueError("body must be an object")
    action, payload = _web_command(d)
    job = d.get("job")
    if isinstance(job, str) and 0 < len(job) <= 40:
        payload["job"] = job
    return action, payload


def _web_command(d):
    op = d.get("op")
    if op == "read":
        return "KIKPAD_MAP_READ", {}
    if op == "pad":
        t = d.get("type")
        if t not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        ch = d.get("channel")
        ch = CH_BANK if ch in ("bank", None) else _int(ch, 1, 16, "channel") - 1
        return "KIKPAD_MAP_PAD", {"bank": _int(d.get("bank"), 0, BANKS - 1, "bank"),
                                  "pad": _int(d.get("pad"), 0, PADS - 1, "pad"),
                                  "type": TYPES.index(t),
                                  "number": _int(d.get("number", 0), 0, 127, "number"),
                                  "channel": ch,
                                  "colour": _int(d.get("colour", 0), 0, 63, "colour")}
    if op == "encoder":
        return "KIKPAD_MAP_ENC", {"mode": _int(d.get("mode"), 0, MODES - 1, "mode"),
                                  "encoder": _int(d.get("encoder"), 0, 7, "encoder"),
                                  "cc": _int(d.get("cc"), 0, 127, "cc"),
                                  "channel": _int(d.get("channel"), 1, 16, "channel") - 1}
    if op == "bank_channel":
        return "KIKPAD_MAP_BANKCH", {"bank": _int(d.get("bank"), 0, BANKS - 1, "bank"),
                                     "channel": _int(d.get("channel"), 1, 16, "channel") - 1}
    if op == "reset":
        what = d.get("what")
        codes = {"bank0": 0, "bank1": 1, "encoders": 2, "all": 127}
        if what not in codes:
            raise ValueError("what must be bank0, bank1, encoders or all")
        return "KIKPAD_MAP_RESET", {"what": codes[what]}
    raise ValueError("op must be read, pad, encoder, bank_channel or reset")


def empty_map():
    return {"banks": [{"channel": None, "pads": [None] * PADS} for _ in range(BANKS)],
            "encoders": [[None] * 8 for _ in range(MODES)]}


class KikpadMapLink:
    """One request at a time on the pad's port :1, on a worker thread of its own (a slow or
    absent pad never stalls the brain's command thread)."""

    def __init__(self, send, state_file=STATE_FILE, timeout=TIMEOUT_S, log=None,
                 wall=time.time, start=True):
        self._send = send                      # send(list_of_data_bytes) -> raises if the port is gone
        self.state_file = state_file
        self.timeout = timeout
        self._log = log or (lambda m: print(f"[kikpad-map] {m}", flush=True))
        self.wall = wall
        self._replies = queue.Queue()
        self._jobs = queue.Queue()
        self._req = threading.Lock()
        self.state = {"connected": False, "supported": None, "busy": False, "dirty": None,
                      "error": None, "last": None, "updated": None, "map": empty_map(),
                      "modes": list(MODE_NAMES), "bank_names": list(BANK_NAMES)}
        self._worker = None
        if start:
            self._worker = threading.Thread(target=self._run, daemon=True, name="kikpad-map")
            self._worker.start()

    # ---- wire ---------------------------------------------------------------
    def on_sysex(self, data):
        """A SysEx from the pad's port :1 (the reader thread). Ours -> the waiting request."""
        b = body(data)
        if b:
            self._replies.put(b)

    def _ask(self, cmd, args, want):
        """Send one request and wait for the reply whose first bytes are `want`. None = no reply."""
        with self._req:
            while not self._replies.empty():
                try:
                    self._replies.get_nowait()
                except queue.Empty:
                    break
            self._send(list(HDR) + [cmd] + [int(a) & 0x7F for a in args])
            end = time.monotonic() + self.timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                try:
                    r = self._replies.get(timeout=left)
                except queue.Empty:
                    return None
                if r[:len(want)] == list(want):
                    return r

    # ---- one operation each (worker thread) ---------------------------------
    def _store_pad(self, r):
        _, b, i, t, n, ch, col = r[:7]
        self.state["map"]["banks"][b]["pads"][i] = {
            "type": TYPES[t] if t < len(TYPES) else "off", "number": n,
            "channel": "bank" if ch == CH_BANK else ch + 1, "colour": col, "rgb": colour_rgb(col)}

    def _store_enc(self, r):
        _, m, e, cc, ch = r[:5]
        self.state["map"]["encoders"][m][e] = {"cc": cc, "channel": ch + 1}

    def _store_bank(self, r):
        _, b, ch = r[:3]
        self.state["map"]["banks"][b]["channel"] = ch + 1

    def hello(self):
        r = self._ask(0x01, [], [0x01])
        if r is None or len(r) < 5:
            return False
        return r[1] >= 1 and r[2] >= BANKS and r[3] >= MODES and r[4] >= PADS

    def read_all(self):
        """Read every map back from the pad. Raises RuntimeError on a missing reply."""
        if not self.hello():
            self.state["supported"] = False
            raise RuntimeError("the pad did not answer the map editor's hello "
                               "(firmware without the editor, or the pad is not on port :1)")
        self.state["supported"] = True
        m = empty_map()
        self.state["map"] = m
        for b in range(BANKS):
            r = self._ask(0x06, [b], [0x06, b])
            if r is None:
                raise RuntimeError(f"no reply for bank {b}'s channel")
            self._store_bank(r)
            for i in range(PADS):
                r = self._ask(0x02, [b, i], [0x02, b, i])
                if r is None:
                    raise RuntimeError(f"no reply for bank {b} pad {i}")
                self._store_pad(r)
        for mo in range(MODES):
            for e in range(8):
                r = self._ask(0x04, [mo, e], [0x04, mo, e])
                if r is None:
                    raise RuntimeError(f"no reply for encoder {e + 1} of mode {mo}")
                self._store_enc(r)
        self.poll_dirty()

    def set_pad(self, b, i, t, n, ch, col):
        r = self._ask(0x03, [b, i, t, n, ch, col], [0x02, b, i])
        if r is None:
            raise RuntimeError(f"the pad did not confirm bank {b} pad {i}")
        self._store_pad(r)
        self.state["dirty"] = True              # the pad saves ~2 s later; _settle() asks it then
        return r

    def set_encoder(self, m, e, cc, ch):
        r = self._ask(0x05, [m, e, cc, ch], [0x04, m, e])
        if r is None:
            raise RuntimeError(f"the pad did not confirm encoder {e + 1} of mode {m}")
        self._store_enc(r)
        self.state["dirty"] = True              # the pad saves ~2 s later; _settle() asks it then
        return r

    def set_bank_channel(self, b, ch):
        r = self._ask(0x07, [b, ch], [0x06, b])
        if r is None:
            raise RuntimeError(f"the pad did not confirm bank {b}'s channel")
        self._store_bank(r)
        self.state["dirty"] = True              # the pad saves ~2 s later; _settle() asks it then
        return r

    def reset(self, what):
        r = self._ask(0x08, [what], [0x08, what])
        if r is None:
            raise RuntimeError("the pad did not confirm the reset")
        self.read_all()                          # every value may have changed: read back the truth

    def poll_dirty(self):
        r = self._ask(0x09, [], [0x09])
        self.state["dirty"] = None if r is None or len(r) < 2 else bool(r[1])
        return self.state["dirty"]

    # ---- jobs ---------------------------------------------------------------
    def submit(self, label, fn, job=None):
        self._jobs.put((label, fn, job))

    def connected(self, on):
        """The brain attached (or lost) the pad's port :1. On attach, read everything back."""
        self.state["connected"] = bool(on)
        if on:
            self.submit("read", self.read_all)
        else:
            self.state["supported"] = None
            self.state["map"] = empty_map()
            self.write_state()

    def run_job(self, label, fn, job=None):
        self.state["busy"] = True
        self.write_state()
        try:
            fn()
            self.state["error"] = None
            self.state["last"] = {"op": label, "ok": True, "job": job}
        except Exception as e:
            self.state["error"] = str(e)
            self.state["last"] = {"op": label, "ok": False, "error": str(e), "job": job}
            self._log(f"{label}: {e}")
        finally:
            self.state["busy"] = False
            self.write_state()

    def _run(self):
        while True:
            label, fn, job = self._jobs.get()
            self.run_job(label, fn, job)
            if label != "status" and self._jobs.empty() and self.state.get("dirty"):
                self._settle()

    def _settle(self):
        """After an edit the pad saves 2 s later: check once it should have, so the page can say so."""
        time.sleep(2.6)
        if self._jobs.empty():
            try:
                self.poll_dirty()
            except Exception:
                pass
            self.write_state()

    def write_state(self):
        self.state["updated"] = self.wall()
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self._log(f"cannot write {self.state_file}: {e}")
