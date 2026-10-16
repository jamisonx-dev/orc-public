#!/usr/bin/env python3
"""orc1_bt_route.py -- event-driven Bluetooth output router.

WHY THIS EXISTS
===============
A paused/resumed phone tears its A2DP transport down and rebuilds it, and
`bluealsa-aplay` gives each rebuilt stream a BRAND-NEW JACK client name
(`bluealsa-aplay.P.<pid>.<N>`, N incrementing). You cannot make a routing
"stick" to a name that is single-use. `bluealsa-aplay` auto-connects each new
client to its cached asound.conf default (system:playback_1/2 = main / follows
whatever card jackd owns), which is exactly the right DEFAULT -- but it means a
NON-default choice (e.g. "put BT on the L-12 even though jackd fell back to
pisound") evaporates on the next pause/resume.

This client fixes only that gap. It sits DORMANT and does nothing until a
`bluealsa-aplay.*:out_*` port appears; then it wires that port to the output
saved in ~/.orc1_bt_out.json and drops any other connection on it. On the next
resume the new client appears and it does the same, instantly.

SAFETY
======
* It is an ordinary JACK client, like pisound-in / l12-out. It cannot own or
  crash jackd; if it dies it just vanishes and all audio keeps running.
* It passes NO audio and registers NO audio ports -- zero DSP, no xrun risk.
* The ONLY wires it ever touches are the bluealsa client's own output ports.
  It never disconnects the synth or any other routing.
* JACK forbids graph edits from inside the port-registration callback, so the
  callback only sets an event; a worker thread does the actual connect/disconnect.
* When the saved output IS the default (out-1-2), it leaves the auto-connect
  alone -- so a default routing needs no disconnect/reconnect at all (no blip),
  and works even if this process is not running.
"""

import json
import os
import re
import threading

import jack

PREF_FILE = os.path.expanduser("~/.orc1_bt_out.json")

# Friendly id -> (left port, right port). "out-1-2" resolves to system:playback,
# i.e. whatever card jackd currently owns == "main, follows the card".
DEST = {
    "out-1-2":     ("system:playback_1",      "system:playback_2"),
    "out-3-4":     ("system:playback_3",      "system:playback_4"),
    "pisound-out": ("pisound-out:playback_1", "pisound-out:playback_2"),
    "l12":         ("l12-out:playback_1",     "l12-out:playback_2"),
}
DEFAULT_ID = "out-1-2"            # bluealsa-aplay's own auto-connect target
BT_PORT_RE = re.compile(r"^bluealsa-aplay\..*:out_(\d+)$")


def load_dest_id():
    """Return the saved destination id, falling back to the default on any problem."""
    try:
        with open(PREF_FILE) as f:
            did = json.load(f).get("out", DEFAULT_ID)
    except Exception:
        did = DEFAULT_ID
    return did if did in DEST else DEFAULT_ID


def main():
    client = jack.Client("bt-route", no_start_server=True)
    wake = threading.Event()

    # If jackd goes away (card switch, crash), exit promptly so systemd restarts
    # us and we reconnect to the fresh server -- rather than hang on a dead client.
    @client.set_shutdown_callback
    def _on_shutdown(status, reason):
        os._exit(0)

    # --- callback: NO graph edits here, just flag the worker --------------------
    @client.set_port_registration_callback
    def _on_port(port, registered):
        try:
            if registered and BT_PORT_RE.match(port.name):
                wake.set()
        except Exception:
            pass

    # --- worker: the only place that edits the graph ---------------------------
    def worker():
        # Do an initial sweep too, in case a BT client is already present.
        pending = True
        while True:
            if not pending:
                wake.wait()
            wake.clear()
            pending = False
            # brief settle so bluealsa-aplay's own auto-connect lands first,
            # otherwise we'd race it and it could re-add the default under us.
            if wake.wait(timeout=0.10):
                # another port arrived mid-settle; loop and handle them together
                pending = True
                continue

            did = load_dest_id()
            lport, rport = DEST[did]
            try:
                all_ports = {pp.name for pp in client.get_ports()}
                bt_ports = [p for p in client.get_ports(is_output=True, is_audio=True)
                            if BT_PORT_RE.match(p.name)]
            except Exception:
                continue

            for p in bt_ports:
                m = BT_PORT_RE.match(p.name)
                idx = int(m.group(1))
                target = lport if (idx % 2 == 0) else rport   # out_000->L, out_001->R

                # SAFETY 1: if the chosen target port does not exist right now
                # (e.g. dest=l12 but jackd owns the L-12 directly so l12-out is
                # absent, or a device is unplugged), do NOT touch this wire.
                # BT stays on bluealsa's own default auto-connect == main, which
                # is always a sane place. A stale preference can never silence BT.
                if target not in all_ports:
                    continue

                try:
                    current = [c.name for c in client.get_all_connections(p)]
                except Exception:
                    continue

                if target in current:
                    # already where we want it; just prune any stray extra wires
                    for name in current:
                        if name != target:
                            try:
                                client.disconnect(p.name, name)
                            except Exception:
                                pass
                    continue

                # SAFETY 2: connect the target FIRST and confirm it took, and only
                # THEN remove the old wire(s). Never disconnect before we know the
                # new destination is live -- that is what orphaned BT into silence.
                try:
                    client.connect(p.name, target)
                except jack.JackError:
                    continue   # connect refused -> leave existing wiring untouched
                except Exception:
                    continue
                try:
                    now = [c.name for c in client.get_all_connections(p)]
                except Exception:
                    now = []
                if target not in now:
                    continue   # connect did not take -> leave existing wiring untouched
                for name in now:
                    if name != target:
                        try:
                            client.disconnect(p.name, name)
                        except Exception:
                            pass

    threading.Thread(target=worker, daemon=True).start()
    client.activate()
    print("[bt-route] active; dest =", load_dest_id(), flush=True)
    try:
        threading.Event().wait()   # run until the service stops us
    finally:
        client.deactivate()
        client.close()


if __name__ == "__main__":
    main()
