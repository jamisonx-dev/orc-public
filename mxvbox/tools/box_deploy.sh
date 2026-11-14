#!/bin/bash
# box_deploy.sh -- put the MXV box player (mxvbox/box/*.py) on the box, the safe way. Run from the PC
# (Git Bash) in orc/:   bash mxvbox/tools/box_deploy.sh [test|install]
#   test     stage the files in /storage/orc/gpu_stage on the box and run all three box suites THERE
#            (the live player is untouched)
#   install  the same, then (only if every suite passed) back up the live files to
#            /storage/orc/backups/<date-time>/, copy the staged ones in, restart orcflash, show the journal
# The box is reached THROUGH THE PI (it is on the Pi's eth0 cable or the home WiFi), at the address the
# conductor last used (/dev/shm/orc1_mxvbox_status.json "addr"): never by mxvbox.local (mDNS drops
# out for minutes). Deploy the BOX before the Pi side when a change spans both (the Pi sends the box
# commands it must understand). After it: commit + push (CLAUDE.md "Git").
set -e
MODE=${1:-test}
PI=${PI:-pibox@pibox.local}
HERE=$(cd "$(dirname "$0")/../box" && pwd)
FILES="gpu.py orcflash.py slotpolicy.py _test_gpu.py _test_orcflash.py _test_slotpolicy.py"
BOX='A=$(python3 -c "import json;print(json.load(open(\"/dev/shm/orc1_mxvbox_status.json\")).get(\"addr\",\"\"))"); ssh -o BatchMode=yes -o ServerAliveInterval=10 -o HostName=$A mxvbox'

cd "$HERE"
for f in gpu.py orcflash.py slotpolicy.py; do python -m py_compile "$f" 2>/dev/null || py -m py_compile "$f"; done
echo "== staging + testing on the box (live player untouched)"
tar --force-local -cf - $FILES | ssh -o BatchMode=yes -o ServerAliveInterval=10 "$PI" "$BOX \"rm -rf /storage/orc/gpu_stage && mkdir -p /storage/orc/gpu_stage && cd /storage/orc/gpu_stage && tar xf - && python -m py_compile gpu.py orcflash.py slotpolicy.py && \
  r=0; for t in _test_slotpolicy.py _test_orcflash.py _test_gpu.py; do out=\\\$(nice -n 10 python -B \\\$t 2>&1); echo \\\"\\\$out\\\" | grep -E 'FAIL|passed|Error|COUNT DRIFT' | sed \\\"s/^/  \\\$t: /\\\"; echo \\\"\\\$out\\\" | tail -1 | grep -q ', 0 failed' || r=1; done; exit \\\$r\""
echo "== all three suites passed"
[ "$MODE" = install ] || { echo "(test only: nothing installed; run with 'install' to go live)"; exit 0; }
echo "== installing"
ssh -o BatchMode=yes -o ServerAliveInterval=10 "$PI" "$BOX \"set -e; B=/storage/orc/backups/\\\$(date +%Y%m%d_%H%M%S); mkdir -p \\\$B; cd /storage/orc; cp $FILES \\\$B/; cp gpu_stage/*.py .; rm -f *.pyo; systemctl restart orcflash; sleep 6; echo \\\"orcflash: \\\$(systemctl is-active orcflash)  (backup: \\\$B)\\\"; journalctl -u orcflash -n 6 --no-pager | cut -c1-170\""
