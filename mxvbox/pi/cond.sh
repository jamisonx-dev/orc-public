#!/bin/bash
# cond_run.sh start [lead_ms] | stop -- the demo conductor, detached, 30 min cap.
cd ~/scratch || exit 1
pkill -f '^(timeout 1800 nice -n 10 )?python3 -u conductor\.py' 2>/dev/null   # anchored: never an ssh line
[ "$1" = stop ] && { python3 -c "import socket;socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'c 000000',('10.55.0.6',9000))"; echo stopped; exit 0; }
setsid nohup timeout 1800 nice -n 10 python3 -u conductor.py "${2:-12}" > conductor.log 2>&1 < /dev/null &
sleep 1
cat conductor.log
