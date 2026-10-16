#!/bin/bash
# kiosk_clear.sh [unit-to-keep ...] -- make the monitor free before a kiosk starts.
#
# VISUALS, the HUD, DAW and DJ mode all run on VT7 / display :0, and their units
# Conflict with each other. Conflicts alone is a RACE: the new unit's xinit starts
# in the same second the old one's login session is still closing, joins that
# dying session, and is taken down with it (measured 2026-10-08: HUD -> VISUALS
# left nothing running; systemd logged "Deactivated successfully" with no scope).
# So: stop the other kiosks explicitly, then wait until their X server AND their
# tty7 logind session are really gone.
#
#   kiosk_clear.sh orc1-hud       # about to start the HUD: stop VISUALS (and DAW/DJ), keep the HUD
#   kiosk_clear.sh                # about to start DAW/DJ: stop VISUALS and the HUD
#
# Always exits 0 (a start must not be refused by its own tidy-up); it says so on
# stderr when the monitor was still busy after the wait.
#
# Test hooks (daw/_test_kiosk_clear.py): SYSTEMCTL, ORC1_SESS_DIR, PGREP, WAIT_TENTHS.
SYSTEMCTL=${SYSTEMCTL:-systemctl}
SESS=${ORC1_SESS_DIR:-/run/systemd/sessions}
PGREP=${PGREP:-pgrep}
WAIT=${WAIT_TENTHS:-100}                 # 10 s

keep=" $* "
for u in orc1-visuals orc1-hud orc1-daw orc1-dj; do
    case "$keep" in *" $u "*) continue ;; esac
    st=$($SYSTEMCTL show -p ActiveState --value "$u" 2>/dev/null)
    case "$st" in
    active|activating|deactivating) $SYSTEMCTL stop "$u" ;;
    esac
done

busy() {
    $PGREP -x Xorg >/dev/null 2>&1 && return 0
    # a logind session on tty7 still exists (a closing one counts: that is the race).
    # REGULAR FILES ONLY: logind also keeps <id>.ref named pipes in that directory,
    # and grep opening a FIFO blocks forever (found live 2026-10-08: hud.sh start hung).
    local f
    for f in "$SESS"/*; do
        [ -f "$f" ] || continue
        grep -qs '^TTY=tty7$' "$f" 2>/dev/null && return 0
    done
    return 1
}

for _ in $(seq "$WAIT"); do
    busy || exit 0
    sleep 0.1
done
echo "kiosk_clear: the monitor (Xorg / a tty7 session) was still busy after $((WAIT / 10)) s" >&2
exit 0
