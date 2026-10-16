#!/usr/bin/env python3
"""ORC-1 · BeatStep Pro parameter READ-BACK helper.

The BSP answers a read request with the current LIVE value of a parameter:
  request:  F0 00 20 6B 7F 42 01 00 <paramId> <item> F7
  reply:    F0 00 20 6B 7F 42 02 00 <paramId> <item> <value> F7   (+ a 1C ack)

Sending is done with `amidi -S` (rawmidi hw:x,0,0 — bidirectional, proven to
elicit replies); capture is done with python-rtmidi co-subscribed to the BSP's
seq output port (ALSA seq allows this alongside the orc1 brain, which also holds
the device). Pure-rtmidi SENDING does NOT reach the device, so the split matters.

Usage:
  echo '[["41","06"],["03","20"]]' | python3 bsp_readback.py
  python3 bsp_readback.py '[["41","06"],["03","20"]]'
Output (stdout): {"ok": true, "values": {"4106": 13, "0320": 74}, "missing": []}
"""
import sys, json, time, subprocess, tempfile, os
try:
    import rtmidi
except Exception as e:
    print(json.dumps({"ok": False, "error": f"rtmidi import: {e}"})); sys.exit(0)

HDR = [0x00, 0x20, 0x6B, 0x7F, 0x42]


def _amidi_port():
    """rawmidi port string for the BSP config port (hw:X,0,0)."""
    import re
    try:
        out = subprocess.run(["amidi", "-l"], capture_output=True, text=True, timeout=4).stdout
        for line in out.splitlines():
            if "BeatStep Pro Arturia Be" in line:
                m = re.search(r'(hw:\d+,\d+,\d+)', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def _open_in():
    """Open the BSP config seq port for input. The BSP exposes two ports; the
    config replies come on the "Arturia Be" one (mirrors amidi's hw:X,0,0).
    Prefer it explicitly; only fall back to any 'beatstep' match. Log the choice
    to stderr so an all-missing read is diagnosable (wrong port vs real drops)."""
    mi = rtmidi.MidiIn()
    ports = mi.get_ports()
    pick = None
    for i, name in enumerate(ports):
        if "arturia be" in name.lower():
            pick = i; break
    if pick is None:
        for i, name in enumerate(ports):
            if "beatstep" in name.lower():
                pick = i; break
    if pick is None:
        return None
    print(f"[bsp_readback] IN port: {ports[pick]}", file=sys.stderr)
    mi.open_port(pick)
    mi.ignore_types(sysex=False, timing=True, active_sense=True)
    return mi


def _send_batch(port, pairs):
    """Send all request messages in one .syx burst via amidi -s."""
    buf = bytearray()
    for pid, item in pairs:
        buf += bytes([0xF0] + HDR + [0x01, 0x00, pid, item, 0xF7])
    with tempfile.NamedTemporaryFile(suffix=".syx", delete=False) as f:
        f.write(buf); path = f.name
    try:
        # A dropped/slow send is already covered by the retry passes, so a
        # single amidi failure must NOT abort the whole read.
        subprocess.run(["amidi", "-p", port, "-s", path],
                       capture_output=True, text=True, timeout=6)
    except Exception:
        pass
    finally:
        try: os.unlink(path)
        except OSError: pass


def _collect(mi, wanted, window):
    """Drain rtmidi input for `window` seconds; return {(pid,item): value} for
    the requested pairs ONLY. Replies are broadcast on the shared BSP seq port,
    so a co-running reader (2nd browser) sees THIS reader's requests AND the
    other's — counting foreign replies would let us `break` early with the wrong
    values. Match strictly against `wanted`."""
    found = {}
    t0 = time.time()
    while time.time() - t0 < window:
        m = mi.get_message()
        if not m:
            time.sleep(0.002); continue
        d = m[0]
        # reply: F0 00 20 6B 7F 42 02 00 pid item val F7  (len 12)
        if len(d) == 12 and d[1:6] == HDR and d[6] == 0x02 and d[7] == 0x00:
            key = (d[8], d[9])
            if key in wanted:
                found[key] = d[10]
                if len(found) >= len(wanted):
                    break
    return found


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        pairs = [(int(p, 16), int(i, 16)) for p, i in json.loads(raw)]
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad input: {e}"})); return
    if not pairs:
        print(json.dumps({"ok": True, "values": {}, "missing": []})); return

    port = _amidi_port()
    if not port:
        print(json.dumps({"ok": False, "error": "BSP not found (amidi)"})); return
    mi = _open_in()
    if mi is None:
        print(json.dumps({"ok": False, "error": "BSP not found (rtmidi in)"})); return

    try:
        time.sleep(0.15)
        while mi.get_message():
            pass
        found = {}
        # The BSP drops requests when flooded (a 95-message burst yields ~30
        # replies), so pace: send small chunks and collect between them.
        CHUNK = 6
        for c in range(0, len(pairs), CHUNK):
            grp = pairs[c:c + CHUNK]
            _send_batch(port, grp)
            found.update(_collect(mi, set(grp), window=0.13))
        # retry stragglers in small chunks (not one-at-a-time -- that made the
        # worst case ~65 process spawns and crept toward the endpoint timeout),
        # bounded by a wall-clock budget so a wedged device can't overrun it.
        deadline = time.time() + 6.0
        for _ in range(3):
            missing = [pr for pr in pairs if pr not in found]
            if not missing or time.time() > deadline:
                break
            for c in range(0, len(missing), CHUNK):
                if time.time() > deadline:
                    break
                grp = missing[c:c + CHUNK]
                _send_batch(port, grp)
                found.update(_collect(mi, set(grp), window=0.12))
        values = {f"{p:02X}{i:02X}": v for (p, i), v in found.items()}
        miss = [f"{p:02X}{i:02X}" for (p, i) in pairs if (p, i) not in found]
        print(json.dumps({"ok": True, "values": values, "missing": miss}))
    finally:
        mi.close_port()


if __name__ == "__main__":
    main()
