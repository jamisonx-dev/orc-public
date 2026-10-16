# ORC-1

A headless Linux music instrument: a chord engine, a 909-style drum / chip /
poly synth, a step sequencer, an arpeggiator, an FX-chain host, and a
retroactive audio recorder ("skipback") with a live sampler — all driven by
hardware controllers recognised by name (Akai Fire, Numark Mixtrack Pro 3) and
playable through any ALSA/JACK audio interface.

## Getting started
See **docs/INSTALL.md** for a full install on a fresh Linux box, and
**docs/ARCHITECTURE.md** for how the pieces fit together.

Out of the box everything runs on the internal synth — no external gear or
mixer required. Bring your own controllers and interface; edit `PORTS` in
`orc1/orc1_config.py` to route to your own hardware.
