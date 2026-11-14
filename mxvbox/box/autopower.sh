#!/bin/sh
# On the MXV box (run once): power on = boot, no waiting for the power button.
# The vendor u-boot's preboot starts with det_pwdn, which holds a cold power-up in
# standby (red LED) until the button is pressed (measured 2026-10-29: a plug pull
# with pfd_s805p_is_pwdn=false still waited). This removes just that step.
# Undo: fw_setenv preboot "det_pwdn; $(fw_printenv -n preboot)"
set -e
fw_printenv > /storage/orc/uboot_env_backup_$(date +%Y%m%d%H%M%S).txt
old=$(fw_printenv -n preboot)
case "$old" in
    "det_pwdn; "*) new=${old#det_pwdn; } ;;
    *) echo "preboot does not start with det_pwdn; nothing changed"; exit 0 ;;
esac
fw_setenv preboot "$new"
echo "now: $(fw_printenv -n preboot | cut -c1-80)..."
