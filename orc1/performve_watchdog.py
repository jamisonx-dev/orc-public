#!/usr/bin/env python3
"""Health watchdog for the TC-Helicon Perform-VE JACK bridges (2026-07-30).

WHY THIS EXISTS
    The Perform-VE only yields capture audio while a USB playback stream is
    open, and `alsa_in` does not report an error when that stops being true --
    it emits pure digital silence. Worse, when the device is unplugged
    `alsa_in` logs `err = -19` (ENODEV) and then SPINS instead of exiting, so
    systemd's Restart=on-failure never fires and `systemctl is-active` happily
    reports "active running" with zero JACK ports. Both failures are invisible
    to every normal health check.

HOW IT DETECTS A DEAD BRIDGE
    Two independent signals, neither of which a working bridge can trip:
      1. performve-in's capture ports are missing from the JACK graph.
      2. Every useful capture channel (1-6) is EXACTLY digital zero across the
         whole sample window. A live Perform-VE always shows a converter noise
         floor around -88 dBFS even with nothing plugged in, so all-bits-zero
         means the stream is dead -- not that the room is quiet. (Channels 7/8
         are permanently "Unused" and always zero, hence 1-6 only.)

RECOVERY
    A per-unit restart does NOT work: restarting performve-out while
    performve-in still holds a dead capture handle comes back silent. Only the
    ordered stop-both-then-start-capture path recovers, which is what runs
    here (starting performve-in pulls performve-out up first and waits for its
    ports).

If the card itself is absent (genuinely unplugged) this just waits -- the
bridge start script idles in that state by design, and restarting would loop.
"""
import subprocess
import sys
import threading
import time

import jack
import numpy as np

CARD = "PerformVE"
CLIENT = "performve-in"
CHANNELS = (1, 2, 3, 4, 5, 6)      # 7/8 are "Unused" and always digital zero
CHECK_EVERY = 20.0                 # seconds between health checks
WINDOW = 1.5                       # seconds of audio per check
FAILS_BEFORE_ACTION = 2            # consecutive bad checks before restarting
COOLDOWN = 180.0                   # seconds to wait after a recovery attempt


def log(msg):
    print("[ve-watchdog] %s" % msg, flush=True)


def card_present():
    try:
        with open("/proc/asound/cards") as f:
            return ("[%s" % CARD) in f.read()
    except OSError:
        return False


def ports_present(client):
    try:
        found = client.get_ports("%s:capture_" % CLIENT, is_audio=True, is_output=True)
        return len(found) >= max(CHANNELS)
    except Exception:
        return False


def recover():
    """Ordered restart -- the only sequence that actually brings capture back."""
    log("recovering: stop both, then start performve-in (pulls playback first)")
    try:
        subprocess.run(["sudo", "systemctl", "stop", "performve-in", "performve-out"],
                       check=False, timeout=30)
        time.sleep(3)
        subprocess.run(["sudo", "systemctl", "start", "performve-in"],
                       check=False, timeout=60)
        log("recovery command issued")
    except Exception as e:
        log("recovery FAILED: %s" % e)


class Meter:
    """Persistent JACK client that reports whether capture carries real samples."""

    def __init__(self):
        self.client = jack.Client("ve-watchdog", no_start_server=True)
        self.ins = [self.client.inports.register("i%d" % c) for c in CHANNELS]
        self.lock = threading.Lock()
        self.nonzero = 0
        self.frames = 0
        self.client.set_process_callback(self._process)
        self.client.activate()

    def _process(self, frames):
        with self.lock:
            for p in self.ins:
                a = p.get_array()
                if np.count_nonzero(a):
                    self.nonzero += 1
            self.frames += frames

    def connect(self):
        ok = 0
        for i, c in enumerate(CHANNELS):
            src = "%s:capture_%d" % (CLIENT, c)
            try:
                self.client.connect(src, self.ins[i])
                ok += 1
            except Exception:
                pass                       # already connected, or port gone
        return ok

    def disconnect(self):
        for p in self.ins:
            try:
                for con in self.client.get_all_connections(p):
                    self.client.disconnect(con, p)
            except Exception:
                pass

    def sample(self, secs):
        with self.lock:
            self.nonzero = 0
            self.frames = 0
        time.sleep(secs)
        with self.lock:
            return self.nonzero, self.frames


def main():
    log("starting (check every %.0fs, %.1fs window)" % (CHECK_EVERY, WINDOW))
    meter = None
    bad = 0
    last_action = 0.0
    while True:
        try:
            if not card_present():
                log("card '%s' absent -- waiting (not restarting)" % CARD)
                bad = 0
                time.sleep(CHECK_EVERY)
                continue

            if meter is None:
                meter = Meter()

            if not ports_present(meter.client):
                bad += 1
                log("capture ports MISSING (%d/%d)" % (bad, FAILS_BEFORE_ACTION))
            else:
                meter.connect()
                nz, frames = meter.sample(WINDOW)
                if frames == 0:
                    bad += 1
                    log("no audio cycles observed (%d/%d)" % (bad, FAILS_BEFORE_ACTION))
                elif nz == 0:
                    bad += 1
                    log("capture is ALL DIGITAL ZERO over %.1fs -- dead stream (%d/%d)"
                        % (WINDOW, bad, FAILS_BEFORE_ACTION))
                else:
                    if bad:
                        log("healthy again")
                    bad = 0

            if bad >= FAILS_BEFORE_ACTION and (time.time() - last_action) > COOLDOWN:
                if meter is not None:
                    meter.disconnect()
                recover()
                last_action = time.time()
                bad = 0
                # our ports pointed at the old client; rebuild next loop
                try:
                    meter.client.deactivate()
                    meter.client.close()
                except Exception:
                    pass
                meter = None
                time.sleep(10)

        except Exception as e:
            log("check error: %s" % e)
            try:
                if meter:
                    meter.client.close()
            except Exception:
                pass
            meter = None
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
