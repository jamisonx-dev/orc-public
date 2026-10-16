#!/usr/bin/env python3
"""
GPIO Test Script — Pi + Pisound
Tests momentary switches on free GPIO pins.

BEFORE WIRING: verify these pins are GREEN on the Blokas pinout:
https://blokas.io/pisound/docs/specs/

Wiring (per button):
  One leg → GPIO pin listed below
  Other leg → any GND pin (there are 8 of them)
  No resistor needed — pull_up=True handles it in software

Potentiometers: Pi has no analog inputs — see note at bottom.
"""

from gpiozero import Button
import time
import sys

# ── Pin assignments ────────────────────────────────────────────────
# These are commonly free on Pisound — verify against Blokas green pins
# Change these to match whichever pins you wire up
PIN_A = 17
PIN_B = 22
PIN_C = 27

# ── Setup ──────────────────────────────────────────────────────────
buttons = {}
failed = []

for name, pin in [('A', PIN_A), ('B', PIN_B), ('C', PIN_C)]:
    try:
        buttons[name] = Button(pin, pull_up=True, hold_time=1.0)
        print(f"  ✓ Button {name} → GPIO {pin}")
    except Exception as e:
        failed.append((name, pin, str(e)))
        print(f"  ✗ Button {name} → GPIO {pin} FAILED: {e}")

if failed:
    print(f"\n  {len(failed)} pin(s) failed — check Blokas pinout for conflicts")

print("\n" + "─" * 45)
print("  Press your buttons. Ctrl+C to quit.")
print("─" * 45 + "\n")

# ── Event counters ─────────────────────────────────────────────────
counts = {name: 0 for name in buttons}
last_press = {}

# ── Handlers ──────────────────────────────────────────────────────
def make_handlers(name, pin):
    def on_press():
        counts[name] += 1
        now = time.time()
        gap = ""
        if name in last_press:
            ms = int((now - last_press[name]) * 1000)
            gap = f"  ({ms}ms since last)"
        last_press[name] = now
        print(f"  ▶ Button {name}  GPIO {pin}  press #{counts[name]}{gap}")

    def on_release():
        print(f"    Button {name} released")

    def on_hold():
        print(f"  ■ Button {name}  HELD (1s+)")

    return on_press, on_release, on_hold

for name, btn in buttons.items():
    pin = {'A': PIN_A, 'B': PIN_B, 'C': PIN_C}[name]
    p, r, h = make_handlers(name, pin)
    btn.when_pressed = p
    btn.when_released = r
    btn.when_held = h

# ── Run ────────────────────────────────────────────────────────────
try:
    while True:
        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n\n─ Session summary ─────────────────────────")
    for name in buttons:
        print(f"  Button {name}: {counts[name]} press(es)")
    print("─" * 45)
    print("  Done.\n")
    sys.exit(0)


# ── POTENTIOMETERS ─────────────────────────────────────────────────
#
# The Pi has zero analog inputs — it can't read a pot directly.
#
# Easiest fix with what you have: the Arduino Uno.
# It has 6 analog inputs (A0–A5), reads 0–1023 per pot,
# and can send values over USB serial to the Pi.
# Pi reads /dev/ttyACM0 or /dev/ttyUSB0.
# Trivial Arduino sketch + trivial Python serial reader.
#
# Alternatively: MCP3008 ADC chip (~$3), talks SPI to the Pi,
# gives 8 analog channels. Well supported in gpiozero and
# CircuitPython. But you already have the Arduino, so use that first.
