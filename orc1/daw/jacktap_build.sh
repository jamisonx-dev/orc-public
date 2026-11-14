#!/bin/bash
# jacktap_build.sh -- compile orc_jacktap.c when the binary is missing or
# older than the source. Run by orc1-vistap.service (its ExecStart), so a
# deploy of a new orc_jacktap.c is built on the next start. Exits non-zero
# when the build fails (the unit turns that into exit 78 = no retry loop).
#
# The binary lives OUTSIDE the repo (~/.cache/orc1/) so the mirror stays
# source-only. Needs gcc + the JACK headers (Debian: libjack-jackd2-dev).
# Prints the binary's path on success.
set -eu
HERE=$(dirname "$(readlink -f "$0")")
SRC=$HERE/orc_jacktap.c
OUT=${JACKTAP_BIN:-$HOME/.cache/orc1/orc_jacktap}
mkdir -p "$(dirname "$OUT")"
if [ ! -x "$OUT" ] || [ "$SRC" -nt "$OUT" ]; then
    gcc -O2 -Wall -Wextra -o "$OUT.tmp" "$SRC" -ljack -lrt
    mv "$OUT.tmp" "$OUT"
    echo "built $OUT" >&2
fi
echo "$OUT"
