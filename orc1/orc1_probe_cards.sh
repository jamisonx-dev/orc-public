#!/bin/bash
# orc1_probe_cards.sh -- what does each USB audio card offer? (read-only, never opens a device)
# Reads /proc/asound, so it is safe while jackd owns the card. Use it right after plugging a
# new interface in (e.g. a Zoom R24): it shows the card NAME to pick on the web page / put in
# ~/.orc1_audio.json, the USB id, and per direction the channels, sample rates and formats.
# The rig's jackd runs at 48000 Hz: a card that lacks it needs a rate change, not a card choice.
for c in /proc/asound/card[0-9]*; do
	[ -f "$c/usbid" ] || continue
	n=${c##*card}
	echo "== card $n: $(sed -n 1p /proc/asound/cards | head -0)$(awk -v n="$n" '$1==n {print $2}' /proc/asound/cards | tr -d '[]') ($(cat $c/usbid))"
	ls $c/stream* >/dev/null 2>&1 || { echo "   no streams (MIDI-only?)"; continue; }
	awk '
		/^Playback:|^Capture:/ { dir=$1 }
		/Channels:/ { ch=$2 }
		/Format:/ { fmt=$2 }
		# Rates comes after Format/Channels/Endpoint in each altset: print there
		/Rates:/ { sub(/^ *Rates: /, ""); print "   " dir " ch=" ch " rates=" $0 " fmt=" fmt }
	' $c/stream* | sort -u
done
echo
echo "jackd: $(cat ~/.jackdrc 2>/dev/null)"
echo "chosen card file: $(cat ~/.orc1_audio.json 2>/dev/null)"
