#!/usr/bin/env python3
"""
Clock / transport chain-thru to the DIN out (2026-10-23).

The MPC on the DIN waits for MIDI START from the Fire's sequencer firmware ("ORC FW seq",
a USB input). USB transport is dropped by default (the BeatStep Pro loop), so the Fire's
port is let through by cfg.CLOCK_THRU_USB_PORTS. This drives the REAL _route_message,
pulled out of orc1.py by source (orc1.py can't be imported), with send_to recorded.
Pure stdlib; touches no port, no file, no service. Safe to run while the rig is in use.

Mutation-prove: drop `or cfg.is_clock_thru_port(source_name)` in orc1.py -> the Fire
checks fail; make it `source_label != 'din'`-style permissive -> the BeatStep check fails.
"""
import ast
import os
import sys

import orc1_config as cfg

EXPECTED = 12
_p = _f = 0


def check(cond, label, extra=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label} {extra}")


SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orc1.py")
with open(SRC, encoding="utf-8") as fh:
    text = fh.read()
fn = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "_route_message")

sent = []


class Pad:
    def handle(self, msg, name):
        return False


class Msg:                      # a non-channel message, as mido gives it (no .channel)
    def __init__(self, t):
        self.type = t


ns = {"state": {}, "padmap": Pad(), "send_to": lambda port, m: sent.append((port, m.type)),
      "cfg": cfg, "arp": None, "fire": None, "seq": None, "router": None,
      "_sysex_is_device_chatter": lambda d: False, "_performve_src_hit": lambda *a: False}
exec(compile(ast.Module([fn], []), SRC, "exec"), ns)
route = ns["_route_message"]

FIRE = "ORC FW seq:ORC FW seq MIDI 1 32:0"
BSP = "Arturia BeatStep Pro:Arturia BeatStep Pro MIDI 1 24:0"
DIN = "pisound:pisound MIDI PS-2C6MZ6V 20:0"

for t in ("start", "clock", "stop", "continue"):
    sent.clear()
    route(Msg(t), "usb", FIRE)
    check(sent == [("pisound", t)], f"the Fire sequencer's {t} reaches the DIN out", sent)

for t in ("start", "clock"):
    sent.clear()
    route(Msg(t), "usb", BSP)
    check(sent == [], f"another USB device's {t} is still dropped (BeatStep loop guard)", sent)

sent.clear()
route(Msg("start"), "usb", "")
check(sent == [], "a USB start with no port name is dropped", sent)

sent.clear()
route(Msg("start"), "din", DIN)
check(sent == [("pisound", "start")], "the DIN in's start still chain-thrus", sent)

check(cfg.is_clock_thru_port(FIRE), "is_clock_thru_port: the Fire sequencer firmware")
check(not cfg.is_clock_thru_port("ORC FW host:ORC FW host MIDI 1 32:0"), "... not the Fire's host mode")
check(not cfg.is_clock_thru_port("FL STUDIO FIRE:FL STUDIO FIRE MIDI 1 32:0"), "... not Akai's firmware")
check(not cfg.is_clock_thru_port(BSP), "... not the BeatStep Pro")

print(f"{_p} passed, {_f} failed  (expected {EXPECTED})")
if _f or (_p + _f) != EXPECTED:
    print("FAIL: count drift" if not _f else "FAIL")
    sys.exit(1)
