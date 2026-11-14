#!/bin/sh
# Called by udev (99-zoom-l12.rules) when the Zoom L-12 USB device goes away --
# powered off, auto power-off, or unplugged.
#
# WHY (measured 2026-09-24): when the L-12 vanishes, jackd logs "capture device
# disconnected ... read error, stopping" and then just SITS there -- the process
# stays up, the graph stops running, and both skipback rings FREEZE. Nothing
# falls back to the pisound; the rig stays silent until the L-12 returns. A
# restart makes orc1_jackd_start.sh pick the next card (pisound), so skipback
# keeps recording and the modules keep playing.
#
# Only restart when jackd is actually ON the L-12 -- if it's already on another
# card, a restart would just cost skipback a ~10 s gap for nothing.
if pgrep -f 'jackd .*hw:L12' >/dev/null 2>&1; then
    logger -t orc1-l12 "L-12 gone while jackd was on it -- restarting orc1-jackd (falls back to next card)"
    /usr/bin/systemctl --no-block restart orc1-jackd.service
else
    logger -t orc1-l12 "L-12 gone; jackd not on it -- nothing to do"
fi
