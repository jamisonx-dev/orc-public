# ORC-1 Installation Guide

This guide covers installing ORC-1 on a fresh Linux machine. It documents **implemented reality** as verified on the reference Pi (2026-09-21). See `ARCHITECTURE.md` for how the pieces fit together once running.

> **Known gap, read this first:** the repo's `orc/systemd/` directory is currently **out of date** — it still ships only `orc1`, `orc1-jackd`, `orc1-synth`, `orc1-web`, `orc1-system`, `skipback-record`. The units that the *current* architecture actually needs — the six per-module host services, the `orc1-bridge@` template, and `bt-route` — live only in `/etc/systemd/system/` on the reference Pi and have not been committed back. Until they are, a truly fresh install must copy those unit files off the reference machine. `orc1-synth` in the repo is **retired** and should not be enabled (see below).

---

## Hardware

**Required**
- A Raspberry Pi (tested on Pi 4/5) or any Linux box that can run JACK with a real audio interface.
- An audio interface supported by ALSA/JACK — see "Supported audio interfaces" below.

**Optional controllers** (ORC-1 detects them by USB/MIDI name; anything absent is silently skipped)
- Akai Fire
- Numark Mixtrack Pro 3 (its onboard `M3` soundcard can also serve as an audio output — see the bridge note below)

**Supported audio interfaces**
ORC-1 picks the master output card automatically in this order: user choice (set from the web UI, saved to `~/.orc1_audio.json`) → Zoom L-12 → pisound → the first other real ALSA playback card. HDMI, Loopback, and the onboard Headphones jack are excluded from auto-selection. A *second* card (e.g. the Mixtrack's `M3`, a Perform-VE) is not selected as master — it is pulled into the running graph on demand as a bridge (see ARCHITECTURE.md → "Audio output").

---

## System packages

```bash
sudo apt update
sudo apt install -y \
    jackd2 \
    jack-tools \
    alsa-utils \
    zita-ajbridge \
    sox \
    libsox-fmt-mp3 \
    jalv \
    python3-pip \
    python3-dev
```

**What each one is for:**
- `jackd2` — the JACK audio server (`jackd`, started by `orc1-jackd`).
- `jack-tools` — `jack_connect`, `jack_lsp`, `jack_disconnect` (routing + watchdog checks).
- `alsa-utils` — `aplay`, `aconnect`, `amidi` (card enumeration, MIDI wiring).
- `zita-ajbridge` — provides `zita-j2a` / `zita-a2j`, the adaptive resamplers used to bridge a second soundcard into the one JACK graph. **Required** if you want any card other than the jackd master (the Mixtrack's audio, a Perform-VE, pisound-as-second-card). The older `alsa_in`/`alsa_out` example tools are deliberately NOT used — their resampler drifts audibly.
- `sox` + `libsox-fmt-mp3` — decodes audio files (wav/mp3/flac/ogg/aiff) for the file-deck feature and MP3 export.
- `jalv` — LV2 plugin host (runs FX chain/insert plugins as JACK clients).
- `python3-pip` / `python3-dev` — for the Python packages below.

**ALSA Loopback module** (used by the legacy skipback path and available as a generic loopback):
```bash
sudo modprobe snd-aloop
echo "snd-aloop" | sudo tee -a /etc/modules   # survive reboots
```

**JACK real-time permissions** — add your user to `audio` and set RT limits:
```bash
sudo usermod -aG audio $USER
newgrp audio      # or log out/in
```
If `/etc/security/limits.d/audio.conf` doesn't exist (it usually does with jackd2):
```
@audio   -  rtprio     95
@audio   -  memlock    unlimited
@audio   -  nice       -19
```
> Note: PAM limits like the above do **not** apply to systemd services (they don't go through PAM). Every real-time unit therefore also carries explicit `LimitRTPRIO`/`LimitMEMLOCK` directives in its drop-in — see the skipback note below.

---

## Python packages

From inside `orc/orc1/`:
```bash
pip3 install -r requirements.txt
```
This installs: `flask`, `mido[ports-rtmidi]` (MIDI I/O), `numpy` (all audio DSP), `JACK-Client` (Python JACK bindings), `scipy` (EQ/filter DSP).

### Kernel: enable the memory cgroup (skipback memory protection)

Raspberry Pi OS boots with the memory cgroup **disabled**, and then every
`MemoryMin` / `MemoryLow` / `MemoryHigh` / `MemoryMax` line in the units is
silently ignored (the skipback recorder's memory reservation and the DJ-mode
Mixxx cap, measured 2026-09-29). Append `cgroup_enable=memory` to the single
line in `/boot/firmware/cmdline.txt`, then reboot:

```bash
sudo sed -i '1 s/$/ cgroup_enable=memory/' /boot/firmware/cmdline.txt
sudo reboot
# after: memory must be listed
cat /sys/fs/cgroup/cgroup.controllers
```

---

## Optional: the monitor modes (HUD, VISUALS, DAW, DJ)

Plug a monitor into the Pi's HDMI port and the ORC can use it on demand: the **HUD** (live play-state), **VISUALS**, and a full-screen **DAW** (REAPER) or **DJ** (Mixxx) mode. Each is a systemd unit that runs its own X session on VT7. None starts at boot, so a headless install needs none of this.

Packages (this is what is installed on the working Pi, Debian 13 "trixie"; it is not a tested clean install):

```bash
sudo apt install xinit xserver-xorg fonts-dejavu-core python3-pygame python3-numpy   # the HUD needs just these
sudo apt install python3-moderngl librsvg2-bin xscreensaver-data                    # VISUALS also uses these
sudo apt install libjack-jackd2-dev                                                  # VISUALS' audio tap builds itself from C on first start
```

Install the units (copy, never `enable`: they have no `[Install]` section) and make the scripts executable:

```bash
sudo cp ~/orc/systemd/orc1-hud.service ~/orc/systemd/orc1-visuals.service ~/orc/systemd/orc1-daw.service ~/orc/systemd/orc1-dj.service /etc/systemd/system/
sudo systemctl daemon-reload
chmod +x ~/orc/orc1/daw/*.sh
```

Start and stop them with the **HUD / VISUALS / DAW / DJ buttons on `/system`**, or `sudo ~/orc/orc1/daw/hud.sh start|stop` (and `visuals.sh`, `daw_mode.sh start [dj]`). Always use these rather than a bare `systemctl start`: the scripts first free the monitor (`daw/kiosk_clear.sh`), because the four units conflict and a direct switch races the old X session. The unit files name the user `pibox` and `/home/pibox/orc`; edit them if yours differs. With no monitor attached the Pi's X falls back to 1024x768; the HUD adapts, but a real screen is the intended use.

The **phone HUD** (`http://pibox.local:5000/hud`) needs none of this: it is a normal web page.

## LV2 plugins (FX)

The FX system builds its palette from whatever LV2 plugins are installed. On the reference rig that palette spans **Airwindows**, **Calf**, and **x42** (curated down to a small set of "keeper" favourites; the rest stay browsable). See `FX_SYSTEM.md` for the as-built FX model.

```bash
# Airwindows (built from source gets the full collection)
sudo apt install -y build-essential lv2-dev
git clone https://github.com/hannesbraun/airwindows-lv2.git
cd airwindows-lv2 && make && sudo make install

# Calf + x42 (packaged)
sudo apt install -y calf-plugins x42-plugins
```
Verify plugins are visible to the host:
```bash
lv2ls | grep -Ei 'hannesbraun|calf|x42'
```
Then (re)build the palette the UI reads:
```bash
python3 orc1/gen_fx_palette.py --all
```
FX are entirely optional — ORC-1 runs without them; the FX pages just won't do anything.

---

## Directory structure

ORC-1 separates **source** (in the repo) from **runtime state** (in `$HOME`). Create the runtime dirs:
```bash
mkdir -p ~/recordings
mkdir -p ~/.skipbuf
```
The per-track ring buffers (`~/.skipbuf/t<id>/ring.raw`, each hundreds of MB) are created automatically by the skipback recorder on first start. Track config lives in `~/.orc1_skipback_tracks.json`.

---

## Configuration (edit for your rig)

Out of the box ORC-1 makes sound entirely on its **internal voices** and can record their output — no external gear or mixer required. Everything below is only needed to route to your own hardware.

- **MIDI output destinations** live in `PORTS` in `orc1/orc1_config.py`. The shipped config defines only `pisound` (plus the always-available internal synth bus). Add a line per device — key = the name you reference in presets/row maps, value = a substring of the ALSA/JACK MIDI port name from `aconnect -l`.
- **Audio output card** is auto-selected, but you can pick one explicitly from the web UI's **AUDIO OUT** card on `/system` (persisted to `~/.orc1_audio.json`).
- **Per-module output routing** — which physical output pair each voice (drum/poly/chip/sampler/mono/bbkit) sounds out of is set on the `/patch` page and persisted to `~/.orc1_module_outputs.json` (card-agnostic; see ARCHITECTURE.md).
- **Skipback / sampler capture** — each skipback track records a configurable JACK source, edited on the `/skipback` page (persisted to `~/.orc1_skipback_tracks.json`). The reference rig records track 0 from the L-12's stereo return (`system:capture_13/14`); the recorder's last-resort fallback is `system:capture_1/2`.

All of these are runtime/`$HOME` state — **not** part of the repo, so personal routing never has to be committed.

---

## systemd services

The reference machine runs these units, all as user `pibox`:

| Unit | What it runs | Notes |
|---|---|---|
| `orc1` | `orc1.py` | The brain — MIDI routing, chord engine, arp, sequencer, web socket, sampler facade |
| `orc1-jackd` | `orc1_jackd_start.sh` → `jackd` | **Sole owner of the JACK server.** Resolves the master card and runs jackd in the foreground; everything else is a JACK *client* |
| `orc1-drum` / `orc1-poly` / `orc1-chip` / `orc1-sampler` / `orc1-mono` / `orc1-bbkit` | `orc1_<mod>_host.py` | The six per-module audio hosts (2026-09 split). Each is `PartOf=orc1`, `BindsTo=orc1-jackd`, `JACK_NO_START_SERVER=1` |
| `orc1-web` | `orc1_web.py` | Flask web UI on port 5000; **not** `PartOf=orc1` (restart separately when web files change) |
| `orc1-system` | `orc-control/orc1_system.py` | Pi health stats on port 5002 (proxied by `/system`) |
| `skipback-record` | `skipback/skipback_recorder.py` | Multitrack ring recorder; JACK client `skipback-rec` |
| `orc1-bridge@<card>.<dir>` | `orc1_bridge_start.sh` | On-demand ALSA↔JACK zita bridge, started from `/patch`. No `[Install]` — never auto-starts at boot |
| `bt-route` | Bluetooth output router | Pins the renaming `bluealsa-aplay` client |
| `orc1-kikpad` | `orc1_kikpad_link.py` | **Optional** (only with the Midiplus SmartPad / KIKPAD firmware): sends the ORC's key to the pad. Unit file in `orc/systemd/`; needs `mido` (already required) and the web process running |

**`orc1-synth` is RETIRED** — disabled and inactive. `orc1_synth.py` is now a shared DSP *library* imported by the six hosts, not a service. Do **not** enable `orc1-synth`; its `inactive` state is correct, not a fault.

> **Repo gap (2026-09-21):** the unit files for the six module hosts, `orc1-bridge@.service`, and `bt-route` are **not yet in `orc/systemd/`** — only the older set is. Until they're committed, copy them from `/etc/systemd/system/` on the reference Pi. The steps below assume the units are present in `/etc/systemd/system/`.

**Critical drop-in for the module hosts (each `orc1-*` host):**
- RT priority + memlock and the couplings `PartOf=orc1.service` (follows a brain restart — a *MIDI* coupling) and `BindsTo=orc1-jackd.service` (a soundcard switch = `restart orc1-jackd`, which cascades the hosts so they reconnect).

**Critical drop-in for `skipback-record`:**
- `realtime.conf` — RT priority + memlock, byte-identical to a host's. **Do not skip it.** Without it the unit inherits `LimitRTPRIO=0` / `LimitMEMLOCK=8388608`, JACK's `AcquireSelfRealTime` fails, and the always-on ring recorder runs its callback at `SCHED_OTHER` with unlocked memory. The tell is three lines at every recorder start:
  ```
  Cannot lock down 107350048 byte memory area (Cannot allocate memory)
  Cannot use real-time scheduling (RR/5) (1: Operation not permitted)
  JackClient::AcquireSelfRealTime error
  ```
  The ~102 MB is JACK's own per-client memlock request, not the ring size.

> **Retired units, do not install/enable:** `orc1-synth` (see above), and the pre-2026-07-19 `skipback-bridge` / `zoom-skipback` ALSA-Loopback bridge (subsumed by `skipback_recorder.py`).

### Enable and start
```bash
sudo systemctl enable orc1 orc1-jackd orc1-web orc1-system skipback-record \
                      orc1-drum orc1-poly orc1-chip orc1-sampler orc1-mono orc1-bbkit
sudo systemctl start orc1-jackd     # JACK server first — everything else is a client
sudo systemctl start skipback-record
sudo systemctl start orc1           # brain; the six hosts follow (PartOf) once enabled
sudo systemctl start orc1-web orc1-system
# orc1-bridge@* and bt-route start on demand / as needed, not at boot.
# Only if you own the SmartPad running the KIKPAD firmware:
#   sudo cp ~/orc/systemd/orc1-kikpad.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now orc1-kikpad
```

---

## Verification checklist

```bash
# Core services green?
systemctl is-active orc1 orc1-jackd orc1-web orc1-system skipback-record \
                    orc1-drum orc1-poly orc1-chip orc1-sampler orc1-mono orc1-bbkit

# JACK running and the module hosts' ports registered?
jack_lsp | grep ORC1        # expect ORC1-Drum/Poly/Chip/Sampler/Mono/BBKit :out_L/out_R
                            # (there is NO ORC1-Synth anymore)

# MIDI ports present?
aconnect -l

# Web UI reachable?
curl -s http://localhost:5000/ | grep -o '<title>[^<]*</title>'

# Sampler self-test (reads a ring, reports RMS):
python3 orc1/orc1_sampler.py --selftest 3
```
Expected: all `active`; `ORC1-Drum:out_L` … `ORC1-Sampler:out_R` present in `jack_lsp`; web returns an HTML title; selftest prints a ring RMS (near-zero on a fresh boot before any audio plays).

---

## Troubleshooting

**A module host stays `inactive` after a restart**
Usually a stale `jackd` holding the device. `orc1-jackd` reaps stale servers in its own `ExecStartPre`, so: `sudo systemctl restart orc1-jackd` (this cascades the hosts via `BindsTo`). Do **not** add `pkill jackd` anywhere — it would kill the shared server out from under skipback and every other client. (`orc1-synth` being inactive is normal and expected — it's retired.)

**Web UI loads but controls do nothing**
The brain (`orc1`) may be down while the web process stays up (it's not `PartOf`). Check `systemctl is-active orc1` and `journalctl -u orc1 -n 30`.

**Sampler captures come back silent**
Check the skipback track's source on `/skipback` — if it points at an L-12 channel but the L-12 isn't connected or its master fader is down, the ring is silent.

**`jack_lsp` hangs or returns `server_error 0x21`**
jackd has wedged (running but rejecting new clients — can also be triggered by a large memory allocation elsewhere). `sudo systemctl restart orc1-jackd` reaps the stale server and starts fresh; `BindsTo` cascades the hosts.

**A module won't come out of a second card**
Bringing a card into the graph and routing a module to it are **two** steps on `/patch`: first start the card (CARDS section → an `orc1-bridge@<card>.<dir>` zita bridge), then route the module's output to it (module-outputs). Until the bridge is started the card has no JACK ports to target. `zita-ajbridge` must be installed.

**Jog wheel on the Mixtrack moves samples wildly**
Make sure you're running the deployed `orc1_mixtrack.py` with `_rel()` two's-complement decoding for relative CCs.
