#!/usr/bin/env python3
"""orc1_kikpad_link -- tell the KIKPAD (Midiplus SmartPad, custom firmware) the ORC's key.

The pad's sequencer has melodic patterns whose rows are scale degrees. With its
SCALE source set to HOST it plays in the key the ORC is in. This process reads the
brain's live state from the web process (GET /state: "root", "scale", "scales") and
sends, on the pad's PRIVATE host port (its 2nd port, USB cable 1; any channel):

    CC 10 = root pitch class 0-11
    CC 11 = scale index into the ORC's SCALE_ORDER (the pad's table is the same list)

Until 2026-10-14 this was MIDI channel 16 on the pad's MUSIC port, which took channel 16 away from every other
device; the pad firmware of that night listens to no channel on its music port and takes these on port :1.

It sends on change, when the pad (re)appears, and every HEARTBEAT_S seconds (a pad
that rebooted has forgotten it). It never opens the pad as an input, so it cannot
affect the brain's own handling of the pad. Standalone, tiny, nice'd: skipback comes
first.

THE PAD'S ALSA PORT IS DESTROYED AND RECREATED on every USB reboot or replug (the
client number is often reused). A handle opened before that is subscribed to nothing
and drops messages silently: mido raises no error. So the handle is dropped when the
pad vanishes from the port list, and reopened before every heartbeat.

    python3 orc1_kikpad_link.py            run forever (the systemd unit does this)
    python3 orc1_kikpad_link.py --once     send once and exit (exit 1 if it could not)
"""
import json
import os
import sys
import time
import urllib.request

STATE_URL = "http://127.0.0.1:5000/state"
PAD_PORT_PREFIX = "kikpad"       # ALSA names look like "KIKPAD:KIKPAD IN 40:0"; the client number can move on replug
CHANNEL = 0                      # any: the host port :1 is a private wire, the pad ignores the channel there
CC_ROOT = 10
CC_SCALE = 11
# The key changes rarely, and every poll is a line in the web process's access log (journal = the SD card the
# skipback rings share): 0.5 s polling was ~170k lines a day. 3 s is plenty responsive for a root / scale change.
POLL_S = 3.0
HEARTBEAT_S = 10.0
RESCAN_S = 2.0

# The order the firmware's table was built from (orc1.SCALE_ORDER). The pad and this
# list must agree; the live /state list is checked against it so a change in the ORC
# is reported instead of silently sending wrong indexes.
SCALE_ORDER = ["MAJ", "DOR", "PHR", "LYD", "MIX", "MIN", "LOC", "CHROM",
               "MAJ5", "MIN5", "BLUES", "HIRA", "INSEN", "KUMOI", "WHOLE", "DIMHW", "DIMWH",
               "HMIN", "MMIN", "HMAJ", "PHRD", "LYDD", "ALT", "HUNG", "DBLH"]


def log(msg):
    print(f"[kikpad-link] {msg}", flush=True)


def scale_index(name):
    """Index of an ORC scale name in the pad's table, or None if the pad does not know it."""
    try:
        return SCALE_ORDER.index(str(name).upper())
    except ValueError:
        return None


def key_from_state(state):
    """(root 0-11, scale index) from a /state dict, or None when it is not usable."""
    try:
        root = int(state["root"]) % 12
    except (KeyError, TypeError, ValueError):
        return None
    idx = scale_index(state.get("scale"))
    if idx is None:
        return None
    return root, idx


def messages_for(root, idx):
    """The two (channel, control, value) tuples that carry the key to the pad."""
    return [(CHANNEL, CC_ROOT, root), (CHANNEL, CC_SCALE, idx)]


def fetch_state():
    with urllib.request.urlopen(STATE_URL, timeout=1.5) as r:
        return json.loads(r.read().decode("utf-8"))


def find_pad_port(names):
    """The pad's HOST port (ALSA port 1 = USB cable 1), never its music port 0."""
    for n in names:
        if n.lower().startswith(PAD_PORT_PREFIX) and n.rsplit(":", 1)[-1] == "1":
            return n
    return None


def port_gone(open_name, names):
    """True when the port we hold open is no longer in the system's list (the pad rebooted or left)."""
    return open_name is None or open_name not in names


def heartbeat_due(now, last_sent_at):
    return now - last_sent_at >= HEARTBEAT_S


def run(once=False, max_loops=None, clock=time.monotonic, sleep=time.sleep):
    import mido                      # imported late: the pure helpers above need no MIDI library
    if hasattr(os, "nice"):            # not on Windows
        try:
            os.nice(10)
        except OSError:
            pass

    out = None
    out_name = None
    last_sent = None
    last_sent_at = 0.0
    last_scan = None
    warned_order = False
    loops = 0

    def drop(why):
        nonlocal out
        log(why)
        try:
            out.close()
        except Exception:
            pass
        out = None

    while True:
        loops += 1
        now = clock()
        scan_due = last_scan is None or now - last_scan >= RESCAN_S or once

        # Notice a pad that rebooted or left: its old handle is dead even if the name comes back
        if out is not None and scan_due:
            last_scan = now
            try:
                if port_gone(out_name, mido.get_output_names()):
                    drop("pad port vanished; will reopen")
            except Exception as e:
                drop(f"port scan failed ({e}); will reopen")

        # (re)open the pad's port when it is missing
        if out is None and scan_due:
            last_scan = now
            try:
                name = find_pad_port(mido.get_output_names())
                if name:
                    out = mido.open_output(name)
                    out_name = name
                    last_sent = None            # a freshly (re)opened pad has not heard the key
                    log(f"pad port open: {name}")
            except Exception as e:                # ALSA hiccup while the pad enumerates
                log(f"port open failed: {e}")
                out = None

        if out is not None:
            try:
                state = fetch_state()
            except Exception:
                state = None                      # web process restarting: keep the last key, try again

            if state is not None:
                if not warned_order and list(state.get("scales", SCALE_ORDER)) != SCALE_ORDER:
                    log("WARNING: the ORC's scale list differs from the pad's table; "
                        "rebuild the firmware table before trusting scale indexes")
                    warned_order = True
                key = key_from_state(state)
                if key is None and once:
                    log("ORC key not usable (unknown scale?)")
                    return 1
                if key is not None and (key != last_sent or heartbeat_due(now, last_sent_at)):
                    try:
                        if key == last_sent:         # a heartbeat: take a fresh handle first (see the docstring)
                            out.close()
                            out = mido.open_output(out_name)
                        for ch, cc, val in messages_for(*key):
                            out.send(mido.Message("control_change", channel=ch, control=cc, value=val))
                        if key != last_sent:
                            log(f"sent root {key[0]} scale {SCALE_ORDER[key[1]]} ({key[1]})")
                        last_sent, last_sent_at = key, now
                        if once:
                            return 0
                    except Exception as e:        # the pad went away mid-send
                        drop(f"send failed ({e}); will reopen")
            elif once:
                log("ORC state not available")
                return 1

        elif once:
            log("no KIKPAD port found")
            return 1

        if max_loops is not None and loops >= max_loops:
            return 0
        sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(run(once="--once" in sys.argv))
