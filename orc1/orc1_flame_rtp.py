#!/usr/bin/env python3
"""The Fire's DAW mode over the network (2026-11-13, owner: "Yes, add it").

The Fire (FLAME firmware v0.36+) has its own USB MIDI port for a DAW, "FLAME DAW", that speaks Mackie
Control / HUI in the firmware. Plugged into the Pi, a computer can only reach it over the network. This
keeps rtpmidid's port for the PC's rtpMIDI session named "FLAME rtp" (cfg.FLAME_RTP_PORT_NAMES) wired
BOTH WAYS to the Fire's DAW port (cfg.FIRE_DAW_PORT_NAMES), and re-wires whenever either comes back
(a Fire replug / reflash, a PC rtpMIDI reconnect, an rtpmidid restart). Nothing else is touched: it
only ever adds those two connections.

On the PC: rtpMIDI, a new session named "FLAME rtp" connected to pibox; in REAPER, the Mackie Control
surface's MIDI in / out = "FLAME rtp". On the Fire: BROWSER > MCU/HUI/DAW.

No mido (aconnect only: nothing to leak, see orc1_midisafe). Service: orc1-flame-rtp.
"""
import os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_config as cfg

POLL_S = 2.0


def parse(listing):
    """aconnect -l -> list of dicts: client, port, cname, pname, to (set of "c:p" it connects to)."""
    ports, client, cname = [], None, ""
    for line in listing.splitlines():
        m = re.match(r"^client (\d+):\s+'([^']*)'", line)
        if m:
            client, cname = m.group(1), m.group(2).strip()
            continue
        m = re.match(r"^\s+(\d+)\s+'([^']*)'", line)
        if m and client is not None:
            ports.append({"client": client, "port": m.group(1), "cname": cname,
                          "pname": m.group(2).strip(), "to": set()})
            continue
        m = re.match(r"^\s+Connecting To:\s*(.+)$", line)
        if m and ports:
            for t in m.group(1).split(","):
                t = t.strip().split("[")[0].strip()
                if re.match(r"^\d+:\d+$", t):
                    ports[-1]["to"].add(t)
    return ports


def _has(name, wanted):
    n = name.lower()
    return any(w.lower() in n for w in wanted)


def find(ports):
    """(rtp "c:p" or None, fire "c:p" or None). rtp = rtpmidid's session port (never its port 0, the
    announcements) named FLAME_RTP_PORT_NAMES; fire = the Fire's DAW port, never an rtpmidid port."""
    rtp = fire = None
    for p in ports:
        addr = f"{p['client']}:{p['port']}"
        is_rtpd = "rtpmidi" in p["cname"].lower()
        if rtp is None and is_rtpd and p["port"] != "0" and _has(p["pname"], cfg.FLAME_RTP_PORT_NAMES):
            rtp = addr
        elif fire is None and not is_rtpd and _has(p["pname"], cfg.FIRE_DAW_PORT_NAMES):
            fire = addr
    return rtp, fire


def plan(listing):
    """The connections to add now: [(src, dst)], each direction only when it is missing."""
    ports = parse(listing)
    rtp, fire = find(ports)
    if not rtp or not fire:
        return []
    to = {f"{p['client']}:{p['port']}": p["to"] for p in ports}
    return [(s, d) for s, d in ((rtp, fire), (fire, rtp)) if d not in to.get(s, set())]


def main():
    print(f"flame-rtp: wiring rtpMIDI {cfg.FLAME_RTP_PORT_NAMES} <-> the Fire's DAW port {cfg.FIRE_DAW_PORT_NAMES}",
          flush=True)
    last = None
    while True:
        try:
            out = subprocess.run(["aconnect", "-l"], capture_output=True, text=True, timeout=5).stdout
            state = find(parse(out))
            if state != last:
                print(f"flame-rtp: rtp session {state[0] or 'not there'}, Fire DAW port {state[1] or 'not there'}",
                      flush=True)
                last = state
            for s, d in plan(out):
                r = subprocess.run(["aconnect", s, d], capture_output=True, text=True, timeout=5)
                print(f"flame-rtp: connected {s} -> {d}" if r.returncode == 0
                      else f"flame-rtp: connect {s} -> {d} failed: {r.stderr.strip()}", flush=True)
        except Exception as e:
            print(f"flame-rtp: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
