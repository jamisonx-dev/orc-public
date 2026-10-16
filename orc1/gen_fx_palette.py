#!/usr/bin/env python3
"""
gen_fx_palette.py -- build fx_palette.json from the LV2 plugins installed on
this machine (primarily the Airwindows catalog).

WHY THIS EXISTS
    The FX rack lets you drop any single installed effect into a slot. To show
    knobs for a slot's plugin we need each plugin's control ports (symbol,
    label, min, max, default) and its audio in/out port symbols. Rather than
    hand-maintain that (the old fx_chains.json was hand-written and drifted
    from reality), we read it straight from the plugins with `lv2info`.

    Run this once on the target box (or whenever plugins are added/removed):

        python3 gen_fx_palette.py                 # all airwindows plugins
        python3 gen_fx_palette.py --all           # every installed LV2 plugin
        python3 gen_fx_palette.py --out /path.json

    It writes fx_palette.json next to this script by default. That file is what
    the rack loads at runtime. It is machine-specific (depends on what's
    installed), so regenerate it on each box -- that keeps the rack fully
    "recognize what's here and adapt" rather than tied to one install.

HOW IT PARSES
    `lv2info <uri>` prints a block per port. We walk the text line by line,
    starting a new port record at every "Port N:" and collecting the fields
    that follow (Type lines, Symbol, Name, Minimum, Maximum, Default). A port
    is an audio input if it has both AudioPort and InputPort type lines; an
    audio output if AudioPort + OutputPort; a control input if ControlPort +
    InputPort. Control OUTPUT ports (meters, etc.) are ignored -- you can't
    set them.
"""

import subprocess
import json
import os
import sys

AUDIO = "http://lv2plug.in/ns/lv2core#AudioPort"
CONTROL = "http://lv2plug.in/ns/lv2core#ControlPort"
INPUT = "http://lv2plug.in/ns/lv2core#InputPort"
OUTPUT = "http://lv2plug.in/ns/lv2core#OutputPort"

HERE = os.path.dirname(os.path.abspath(__file__))


def list_uris(all_plugins=False):
    """Return sorted plugin URIs from lv2ls (airwindows only unless --all)."""
    out = subprocess.run(["lv2ls"], capture_output=True, text=True).stdout
    uris = [u.strip() for u in out.splitlines() if u.strip()]
    if not all_plugins:
        uris = [u for u in uris if "airwindows" in u.lower()]
    return sorted(uris)


def parse_plugin(uri):
    """Run lv2info on one URI and return a palette entry dict, or None on error.

    Returns:
        {
          "uri": ...,
          "label": human name,
          "audio_in":  [port symbols],   # usually ["inL","inR"]
          "audio_out": [port symbols],   # usually ["outL","outR"]
          "controls":  [{symbol,label,min,max,default}, ...]
        }
    """
    try:
        text = subprocess.run(["lv2info", uri],
                              capture_output=True, text=True).stdout
    except Exception:
        return None

    label = None
    audio_in, audio_out, controls = [], [], []

    # Per-port accumulator. We flush it when the next "Port N:" starts or at EOF.
    cur = None

    def flush(port):
        if not port:
            return
        types = port["types"]
        sym = port.get("symbol")
        if not sym:
            return
        is_audio = AUDIO in types
        is_control = CONTROL in types
        is_in = INPUT in types
        is_out = OUTPUT in types
        if is_audio and is_in:
            audio_in.append(sym)
        elif is_audio and is_out:
            audio_out.append(sym)
        elif is_control and is_in:
            # A control INPUT port = a knob we can set.
            controls.append({
                "symbol": sym,
                "label": port.get("name", sym),
                "min": port.get("min", 0.0),
                "max": port.get("max", 1.0),
                "default": port.get("default",
                                    port.get("min", 0.0)),
            })
        # control OUTPUT ports (meters) are intentionally dropped.

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Name:") and label is None:
            label = line.split(":", 1)[1].strip()
        elif line.startswith("Port "):
            flush(cur)
            cur = {"types": []}
        elif cur is not None:
            # Type URIs appear both on the "Type:  http://...#AudioPort" line
            # AND on bare continuation lines ("http://...#InputPort"). Detect
            # each known type as a substring so we catch both forms.
            for _t in (AUDIO, CONTROL, INPUT, OUTPUT):
                if _t in line and _t not in cur["types"]:
                    cur["types"].append(_t)
            if line.startswith("Symbol:"):
                cur["symbol"] = line.split(":", 1)[1].strip()
            elif line.startswith("Name:"):
                cur["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Minimum:"):
                cur["min"] = _num(line)
            elif line.startswith("Maximum:"):
                cur["max"] = _num(line)
            elif line.startswith("Default:"):
                cur["default"] = _num(line)
    flush(cur)

    if not label:
        label = uri.rsplit("/", 1)[-1]
    return {
        "uri": uri,
        "label": label,
        "audio_in": audio_in,
        "audio_out": audio_out,
        "controls": controls,
    }


def _num(line):
    """Parse the float off a 'Field:   1.234000' line; None if unparseable."""
    try:
        return float(line.split(":", 1)[1].strip())
    except Exception:
        return None


def main():
    all_plugins = "--all" in sys.argv
    out_path = os.path.join(HERE, "fx_palette.json")
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    uris = list_uris(all_plugins)
    palette = {}
    skipped = []
    for uri in uris:
        entry = parse_plugin(uri)
        # Only keep plugins we can actually use as a stereo/mono insert:
        # at least one audio in AND one audio out.
        if entry and entry["audio_in"] and entry["audio_out"]:
            # Key by the short name (last URI segment) -- stable and readable.
            key = uri.rsplit("/", 1)[-1]
            palette[key] = entry
        else:
            skipped.append(uri)

    # Sort keys for a stable, diff-friendly file.
    palette = {k: palette[k] for k in sorted(palette)}

    with open(out_path, "w") as f:
        json.dump(palette, f, indent=2)

    print(f"wrote {out_path}")
    print(f"  {len(palette)} usable plugins")
    if skipped:
        print(f"  {len(skipped)} skipped (no usable audio in+out):")
        for u in skipped:
            print(f"    - {u}")


if __name__ == "__main__":
    main()
