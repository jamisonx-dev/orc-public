#!/bin/sh
# On the MXV box: restart the orcflash pattern player.
for p in $(ps | grep '[p]ython -u /storage/orc/orcflash' | awk '{print $1}'); do kill "$p"; done
sleep 1
(nohup python -u /storage/orc/orcflash.py > /storage/orc/orcflash.log 2>&1 < /dev/null &)
sleep 2
cat /storage/orc/orcflash.log
ps | grep -c '[p]ython -u /storage/orc/orcflash'
