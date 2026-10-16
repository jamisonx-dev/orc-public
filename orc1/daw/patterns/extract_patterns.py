#!/usr/bin/env python3
"""extract_patterns.py -- rebuild the VISUALS pattern tiles from their sources.

Run once on any machine (stdlib only); the output SVGs are committed, so the
Pi never needs this. It records exactly how the tiles were made:

  python3 extract_patterns.py <hero app.js> <pattern-monster _index.js>

Sources (downloaded 2026-09-30, see CREDITS.md for the licences):
  * Hero Patterns by Steve Schoger -- github.com/sschoger/hero-patterns
    js/app.js (the site's pattern list; each entry is one SVG tile).
    CC BY 4.0 (stated on heropatterns.com).
  * Pattern Monster -- github.com/catchspider2002/svelte-svg-patterns
    src/routes/_index.js (a JSON list: tile width/height, mode, and '~'
    separated paths, one per colour layer). MIT.

What each output tile is: ONE repeat of the pattern, white/grey shapes on a
black background (black = gap, which stays black on screen; the renderer's
palette gives the colour), at the tile's own width x height. Changes from
the originals: recoloured (Hero's #000 fill -> white; Pattern Monster's
colour layers -> greys 1.0, 0.72, 0.5, 0.34 so the palette still separates
them), a black background added, outlines given a fixed stroke width.
"""
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GREYS = ("#ffffff", "#b8b8b8", "#808080", "#575757", "#404040")
STROKE = 1.2          # Pattern Monster outline width, in tile units


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def hero(src):
    text = open(src, encoding="utf-8").read()
    items = re.findall(r"name:\s*'([^']+)',\s*image:\s*'(<svg.*?</svg>)'", text, re.S)
    out = []
    for name, svg in items:
        m = re.search(r'width="([\d.]+)" height="([\d.]+)"', svg)
        w, h = float(m.group(1)), float(m.group(2))
        inner = re.sub(r"^<svg[^>]*>|</svg>$", "", svg.strip())
        inner = inner.replace('fill="#000"', 'fill="#fff"')
        out.append((slugify(name), name, w, h, inner))
    return out


def monster(src):
    text = open(src, encoding="utf-8").read()
    data = json.loads(text[text.index("["):text.rindex("]") + 1])
    out = []
    for d in data:
        w, h = float(d["width"]), float(d["height"])
        layers = []
        for i, path in enumerate(d["path"].split("~")):
            g = GREYS[min(i, len(GREYS) - 1)]
            if d["mode"] == "fill":
                layers.append(f"<g fill='{g}' stroke='none'>{path}</g>")
            else:
                layers.append(f"<g fill='none' stroke='{g}' stroke-width='{STROKE}' "
                              f"stroke-linecap='round' stroke-linejoin='round'>{path}</g>")
        out.append((d["slug"], d["title"], w, h, "".join(layers)))
    return out


def write(dirname, tiles):
    d = os.path.join(HERE, dirname)
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if f.endswith(".svg"):
            os.remove(os.path.join(d, f))
    for slug, title, w, h, inner in tiles:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}" height="{h:g}" '
               f'viewBox="0 0 {w:g} {h:g}"><title>{html.escape(title)}</title>'
               f'<rect width="{w:g}" height="{h:g}" fill="#000"/>{inner}</svg>\n')
        with open(os.path.join(d, slug + ".svg"), "w", encoding="utf-8", newline="\n") as f:
            f.write(svg)
    print(f"{dirname}: {len(tiles)} tiles")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    write("hero", hero(sys.argv[1]))
    write("monster", monster(sys.argv[2]))
