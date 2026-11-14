#!/bin/bash
# Run EVERY regression suite under ~/orc (found on disk, never a hand-kept list) and compare
# each suite's pass count with _test_counts.txt. Prints one line per suite:
#   OK / BROKEN (crashed or failed) / DRIFT (count differs from the file) / NEW (not in the file)
# and lists counts-file entries whose suite no longer exists (GONE).
# Box-only suites (mxvbox/box/) are skipped: run those on the box.
# Safe while the rig is in use (the suites are offline; run politely at nice 15). ~4 min.
#   bash ~/orc/orc1/run_all_tests.sh            (after adding checks: update _test_counts.txt)
cd ~/orc || exit 1
COUNTS=~/orc/orc1/_test_counts.txt
LOG=$(mktemp -d /tmp/orc_tests_XXXX)
declare -A want
while read -r s n _; do [[ -z "$s" || "$s" == \#* ]] && continue; want[$s]=$n; done < "$COUNTS"
bad=0; seen=()
for f in $(find . \( -name "_test_*.py" -o -name "test_*.py" \) -not -path "*/_archive/*" -not -path "./mxvbox/box/*" | sort); do
  rel=${f#./}; seen+=("$rel"); b=$(basename "$f" .py)
  ( cd "$(dirname "$f")" && timeout 600 nice -n 15 python3 -B "$b.py" > "$LOG/$b.log" 2>&1 ); rc=$?
  got=$(grep -aoE "[0-9]+ passed|[0-9]+/[0-9]+ tests passed" "$LOG/$b.log" | tail -1 | grep -oE "^[0-9]+")
  fails=$(grep -aoE "[0-9]+ failed" "$LOG/$b.log" | tail -1 | grep -oE "^[0-9]+")
  exp=${want[$rel]}
  if [[ $rc -ne 0 || -z "$got" || "${fails:-0}" != 0 ]]; then st=BROKEN; bad=1
  elif [[ -z "$exp" ]]; then st=NEW; bad=1
  elif [[ "$got" != "$exp" ]]; then st=DRIFT; bad=1
  else st=OK; fi
  printf "%-7s %-40s got=%-5s want=%-5s rc=%s\n" "$st" "$rel" "${got:--}" "${exp:--}" "$rc"
done
for s in "${!want[@]}"; do [[ " ${seen[*]} " == *" $s "* ]] || { echo "GONE    $s (in _test_counts.txt, no such file)"; bad=1; }; done
echo "logs: $LOG"
exit $bad
