#!/bin/bash
# pisound FX-loop return (2026-07-14). Only bridge when jackd is NOT driving the
# pisound (i.e. the L12/another card is the main out). When jackd is on
# hw:pisound the main output owns the device, so the bridge can't grab it and
# would crash-loop with "Device or resource busy" -- in that case we idle instead.
#
# 2026-08-09: switched the bridge from `alsa_out` (jack-example-tools) to
# `zita-j2a` (zita-ajbridge). The pisound runs on its own clock, independent of
# the L12 that jackd locks to, so anything routed here MUST be async-resampled.
# alsa_out's resampler drifted audibly -- sustained poly chords routed to the
# pisound popped/glitched with zero JACK xruns (the artifact is inside the
# bridge, not the graph). zita-j2a is the purpose-built high-quality adaptive
# resampler for exactly this second-card case. Same JACK client name and
# playback_1/2 ports, so the saved /patch (~/.orc1_patch.json) reconnects.
sleep 1
if pgrep -af jackd 2>/dev/null | grep -q 'hw:pisound'; then
    echo "[pisound-out] jackd is on hw:pisound - FX-return loop disabled (no device conflict)."
    exec sleep infinity
fi
exec /usr/bin/zita-j2a -d hw:pisound -j pisound-out -r 48000 -p 1024 -n 2
