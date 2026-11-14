#!/bin/bash
# install.sh -- install ORC-1 on a Debian-family Linux machine (Raspberry Pi OS, Debian, Ubuntu).
#
#   git clone <this repo> ~/orc && cd ~/orc && bash install.sh
#
# Run it as the user the ORC will run as (not root; it calls sudo itself). It does what
# docs/INSTALL.md describes by hand: packages, the audio group and real-time limits, the
# systemd units (with YOUR user name and folder in place of the reference machine's
# "pibox" and /home/pibox/orc), then enables and starts the core services and checks them.
# Safe to run again.
#
#   --yes            no questions: core install only, plus whatever --with-* flags name
#   --with-fx        also the packaged LV2 effects (Calf, x42) and rebuild the FX palette
#   --with-monitor   also the HDMI monitor modes (HUD, VISUALS, DAW, DJ): packages + units
#   --sudoers        give this user passwordless sudo (the web page's buttons need it)
#   --no-start       install everything but do not enable or start any service
#   --help
#
# NOT done here (see docs/INSTALL.md): the Airwindows effects (built from source), the
# Pi WiFi / hotspot mode, and the services for optional gear (their unit files are copied
# but left off: enable the ones you own).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
U="$(id -un)"; G="$(id -gn)"; H="$HOME"
REF_USER=pibox; REF_HOME=/home/pibox; REF_REPO=/home/pibox/orc
YES=0; FX=""; MON=""; SUDOERS=""; START=1
CORE="orc1-jackd skipback-record orc1 orc1-drum orc1-poly orc1-chip orc1-mono orc1-sampler orc1-bbkit orc1-web orc1-system"

for a in "$@"; do
    case "$a" in
        --yes) YES=1 ;;
        --with-fx) FX=1 ;;
        --with-monitor) MON=1 ;;
        --sudoers) SUDOERS=1 ;;
        --no-start) START=0 ;;
        --help|-h) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m[install] %s\033[0m\n' "$*"; }
note() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; WARNED=1; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
WARNED=0
ask() {   # ask "question" -> 0 for yes. With --yes every question is answered no.
    [ "$YES" = 1 ] && return 1
    local r; read -r -p "[install] $1 [y/N] " r </dev/tty || return 1
    case "$r" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ── 0. can this machine take it? ─────────────────────────────────────────────
[ "$(id -u)" != 0 ] || die "run this as the user the ORC will run as, not as root (it uses sudo itself)"
command -v apt-get >/dev/null || die "this installer needs apt (Debian, Raspberry Pi OS, Ubuntu). Follow docs/INSTALL.md by hand on other systems"
[ -d /run/systemd/system ] || die "systemd is not running here; the ORC is a set of systemd services"
[ -f "$REPO/orc1/orc1.py" ] || die "run install.sh from inside the ORC repo (orc1/orc1.py not found beside it)"
case "$REPO$H$U" in *[[:space:]\#\&\|]*) die "the repo path, home folder and user name must not contain spaces or # & |" ;; esac
sudo -v || die "sudo is needed"

say "Installing ORC-1 from $REPO for user $U"

# ── 1. packages ──────────────────────────────────────────────────────────────
say "System packages"
PKGS="jackd2 jack-example-tools alsa-utils zita-ajbridge sox libsox-fmt-mp3 jalv curl
      python3 python3-flask python3-mido python3-rtmidi python3-numpy python3-scipy
      python3-pil python3-jack-client"
[ -n "$FX" ] || { ask "Also install the packaged LV2 effects (Calf, x42) for the FX pages?" && FX=1 || true; }
[ -n "$MON" ] || { ask "Also install the HDMI monitor modes (HUD, VISUALS, DAW, DJ)?" && MON=1 || true; }
[ -z "$FX" ]  || PKGS="$PKGS calf-plugins x42-plugins lilv-utils"
[ -z "$MON" ] || PKGS="$PKGS xinit xserver-xorg fonts-dejavu-core python3-pygame python3-moderngl librsvg2-bin xscreensaver-data libjack-jackd2-dev gcc"
# jackd2 asks one question (real-time priority for the audio group): answer yes for it
echo "jackd2 jackd/tweak_rt_limits boolean true" | sudo debconf-set-selections
sudo apt-get update -q
# jack_lsp / jack_connect: their own package on current Debian / Ubuntu, part of jackd2 on older ones
apt-cache show jack-example-tools >/dev/null 2>&1 || PKGS="${PKGS/jack-example-tools/}"
# shellcheck disable=SC2086
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q $PKGS

# every Python library the ORC imports must load; anything apt could not give comes from pip
MISSING=""
for m in flask mido rtmidi numpy scipy PIL jack; do
    python3 -c "import $m" 2>/dev/null || MISSING="$MISSING $m"
done
if [ -n "$MISSING" ]; then
    note "not importable after apt:$MISSING -- trying pip"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pip python3-dev
    PIPNAMES=""
    for m in $MISSING; do
        case "$m" in rtmidi) PIPNAMES="$PIPNAMES python-rtmidi" ;; PIL) PIPNAMES="$PIPNAMES Pillow" ;; jack) PIPNAMES="$PIPNAMES JACK-Client" ;; *) PIPNAMES="$PIPNAMES $m" ;; esac
    done
    # shellcheck disable=SC2086
    pip3 install --user --break-system-packages $PIPNAMES 2>/dev/null || pip3 install --user $PIPNAMES
    for m in $MISSING; do python3 -c "import $m" || die "Python cannot import $m"; done
fi
note "Python libraries load: flask mido rtmidi numpy scipy PIL jack"

# ── 2. real-time audio for this user ─────────────────────────────────────────
say "Audio group and real-time limits"
sudo usermod -aG audio "$U"
if ! grep -rqsE '^\s*@audio\s+-\s+rtprio' /etc/security/limits.d/ /etc/security/limits.conf; then
    printf '@audio   -  rtprio     95\n@audio   -  memlock    unlimited\n' | sudo tee /etc/security/limits.d/audio.conf >/dev/null
    note "wrote /etc/security/limits.d/audio.conf"
fi

# ── 3. this user and folder in place of the reference machine's ──────────────
fit() {   # stdin -> stdout: the reference machine's user / folders replaced by this one's
    sed -e "s#$REF_REPO#$REPO#g" -e "s#$REF_HOME#$H#g" \
        -e "s#^User=$REF_USER\$#User=$U#" -e "s#^Group=$REF_USER\$#Group=$G#" \
        -e "s#runuser -u $REF_USER #runuser -u $U #g" -e "s#chown $REF_USER:$REF_USER #chown $U:$G #g"
}
# scripts in the repo that carry the reference user or folder (the list in docs/INSTALL.md)
INTREE="orc1/orc1_web.py orc1/orc1_sampler_host.py orc1/orc1_net.py orc1/orc1_netroot.py orc1/orc1_nettrust.py
        orc1/orc1_samba_trust.py orc1/orc1_netmode.sh orc1/orc1_netwatch.sh orc1/orc1_button.sh
        orc1/orc1_boxlink_setup.sh orc1/orc1_jackd_start_bound.sh orc1/daw/daw_mode.sh orc1/daw/visuals.sh"
if [ "$U" != "$REF_USER" ] || [ "$REPO" != "$REF_REPO" ]; then
    say "Fitting the scripts to user $U and folder $REPO"
    for f in $INTREE; do
        [ -f "$REPO/$f" ] || { warn "$f not found (skipped)"; continue; }
        fit < "$REPO/$f" > "$REPO/$f.fit~"
        if cmp -s "$REPO/$f" "$REPO/$f.fit~"; then rm -f "$REPO/$f.fit~"
        else cat "$REPO/$f.fit~" > "$REPO/$f"; rm -f "$REPO/$f.fit~"; note "edited $f"; fi
    done
    note "(git will show those files as changed: that is this step, keep the changes)"
fi
chmod +x "$REPO"/orc1/*.sh "$REPO"/orc1/daw/*.sh "$REPO"/orc1/skipback/*.sh 2>/dev/null || true

# ── 4. folders the ORC writes to ─────────────────────────────────────────────
mkdir -p "$H/recordings" "$H/.skipbuf"

# ── 5. systemd units ─────────────────────────────────────────────────────────
say "systemd units"
SD=/etc/systemd/system
put() {   # put <file in the repo> <destination>: fitted copy, root-owned, 644
    fit < "$1" | sudo tee "$2" >/dev/null; sudo chmod 644 "$2"
}
n=0
for f in "$REPO"/systemd/*.service "$REPO"/systemd/*.timer; do
    put "$f" "$SD/$(basename "$f")"; n=$((n + 1))
done
for d in "$REPO"/systemd/*.service.d; do
    b="$(basename "$d")"
    [ "$b" = wsdd2.service.d ] && continue          # the reference machine's network setup, not the ORC
    sudo mkdir -p "$SD/$b"
    for f in "$d"/*.conf; do put "$f" "$SD/$b/$(basename "$f")"; n=$((n + 1)); done
done
# The five hosts' link to the audio server is not in the repo: orc1_assign_module_out.sh
# writes it (and re-writes it when a module is sent to another card from /patch). On a new
# machine nothing has written it yet, so write the same "main server" file it would.
for m in drum poly chip mono sampler; do
    dd="$SD/orc1-$m.service.d"; sudo mkdir -p "$dd"
    if [ ! -f "$dd/50-jack-server.conf" ]; then
        printf '# Generated by orc1_assign_module_out.sh -- %s on the DEFAULT (main) server.\n[Unit]\nAfter=orc1-jackd.service\nBindsTo=orc1-jackd.service\n' "$m" \
            | sudo tee "$dd/50-jack-server.conf" >/dev/null
        n=$((n + 1))
    fi
done
note "$n unit files and drop-ins in $SD"

sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp "$REPO"/systemd/journald.conf.d/*.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald || warn "could not restart journald (the log settings apply after a reboot)"
fit < "$REPO/udev/99-zoom-l12.rules" | sudo tee /etc/udev/rules.d/99-zoom-l12.rules >/dev/null
sudo udevadm control --reload 2>/dev/null || true
sudo systemctl daemon-reload

# ── 6. things that need a yes ────────────────────────────────────────────────
if ! sudo -n true 2>/dev/null; then
    [ -n "$SUDOERS" ] || { ask "The ORC's web page restarts services, switches sound cards and shuts down through sudo WITHOUT a password. Give user $U passwordless sudo?" && SUDOERS=1 || true; }
    if [ -n "$SUDOERS" ]; then
        t="$(mktemp)"; echo "$U ALL=(ALL) NOPASSWD: ALL" > "$t"
        sudo visudo -cf "$t" >/dev/null && sudo install -m 440 -o root -g root "$t" /etc/sudoers.d/010_orc1-nopasswd
        rm -f "$t"; note "wrote /etc/sudoers.d/010_orc1-nopasswd"
    else
        warn "no passwordless sudo: the web page's restart / sound card / power buttons and the monitor modes will not work"
    fi
fi
if [ -f /boot/firmware/cmdline.txt ] && ! grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null \
   && ! grep -q 'cgroup_enable=memory' /boot/firmware/cmdline.txt; then
    if ask "Raspberry Pi: turn on the memory cgroup (protects the skipback recorder's memory; edits /boot/firmware/cmdline.txt, needs a reboot)?"; then
        sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.orc1-backup
        sudo sed -i '1 s/$/ cgroup_enable=memory/' /boot/firmware/cmdline.txt
        note "done (backup: /boot/firmware/cmdline.txt.orc1-backup). Reboot when the install is finished."
    else
        note "memory cgroup left off (docs/INSTALL.md, 'Kernel: enable the memory cgroup')"
    fi
fi
if [ -n "$FX" ]; then
    say "FX palette"
    ( cd "$REPO" && python3 orc1/gen_fx_palette.py --all ) || warn "the FX palette was not rebuilt (python3 orc1/gen_fx_palette.py --all)"
fi

# ── 7. start and check ───────────────────────────────────────────────────────
if [ "$START" = 0 ]; then
    say "Installed, nothing started (--no-start). To start: sudo systemctl enable --now $CORE orc1-health.timer"
    exit 0
fi
say "Starting the core services"
if ! grep -q '^ *[0-9]' /proc/asound/cards 2>/dev/null; then
    warn "no sound card found (/proc/asound/cards): the audio server waits 90 s for one and the sound hosts cannot start without it"
fi
# shellcheck disable=SC2086
sudo systemctl enable -q $CORE orc1-health.timer
sudo systemctl start orc1-jackd          # the audio server first: everything else is its client
sudo systemctl start skipback-record
sudo systemctl start orc1                # the brain; the six sound hosts follow it
# shellcheck disable=SC2086
sudo systemctl start $CORE orc1-health.timer

say "Checking"
FAIL=0
ok()  { note "  ok    $*"; }
bad() { note "  FAIL  $*"; FAIL=1; }
for i in $(seq 1 30); do    # the hosts start one after another
    # shellcheck disable=SC2086
    [ "$(systemctl is-active $CORE | grep -c '^active$')" = 11 ] && break
    sleep 2
done
for u in $CORE; do
    if [ "$(systemctl is-active "$u")" = active ]; then ok "$u is running"; else bad "$u is $(systemctl is-active "$u" || true)  (journalctl -u $u -n 30)"; fi
done
PORTS="$(sudo -u "$U" env JACK_NO_START_SERVER=1 jack_lsp 2>/dev/null || true)"
if sudo journalctl -u orc1 -n 40 --no-pager 2>/dev/null | grep -q "No MIDI inputs found"; then
    note "  ->    the brain (orc1) stops when the machine has NO MIDI input at all, and it takes the sound"
    note "        hosts down with it each time it retries. Plug in any MIDI controller or interface; it"
    note "        starts by itself within a few seconds (it retries every 5 s)."
fi
for p in Drum Poly Chip Mono Sampler BBKit; do
    if echo "$PORTS" | grep -q "^ORC1-$p:"; then ok "ORC1-$p has its audio ports"; else bad "ORC1-$p has no audio ports"; fi
done
if sudo -u "$U" aconnect -l 2>/dev/null | grep -q "ORC1-Synth-Bus"; then ok "the MIDI bus ORC1-Synth-Bus exists"; else bad "no ORC1-Synth-Bus MIDI port"; fi
T=""
for i in $(seq 1 15); do
    T="$(curl -s -m 5 http://localhost:5000/ | grep -o '<title>[^<]*</title>' | head -1 || true)"
    [ -n "$T" ] && break; sleep 2
done
if [ -n "$T" ]; then ok "the web page answers on port 5000 ($T)"; else bad "no web page on port 5000"; fi
A="$(cat "$H"/.skipbuf/t*/ring.pos 2>/dev/null | head -1 || true)"; sleep 2
B="$(cat "$H"/.skipbuf/t*/ring.pos 2>/dev/null | head -1 || true)"
if [ -n "$A" ] && [ -n "$B" ] && [ "$A" != "$B" ]; then ok "skipback is recording (the ring moved $(( (B - A) / 2 )) bytes a second)"; else bad "skipback is not recording (~/.skipbuf/t*/ring.pos is not moving)"; fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$FAIL" = 0 ]; then
    say "ORC-1 is installed and running.  Open  http://${IP:-localhost}:5000  (or http://$(hostname).local:5000)"
else
    say "Installed, but some checks FAILED (above). docs/INSTALL.md has a troubleshooting section."
fi
id -nG "$U" | grep -qw audio && [ -z "$(id -nG | tr ' ' '\n' | grep -x audio)" ] && note "log out and in again (or reboot) so your own shell is in the audio group"
note "optional gear (KIKPAD, Zoom pedal, DAW surface, FLAME Fire, BeatStep lights, MXV box): unit files are in place but off; see docs/INSTALL.md"
[ "$WARNED" = 0 ] || note "there were warnings above"
exit "$FAIL"
