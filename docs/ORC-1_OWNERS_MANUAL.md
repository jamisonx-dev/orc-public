# ORC-1 / PIBOX - Owner's Manual

*Last checked against the code: 2026-10-16 (audio topology, controllers, skipback, services and file lists; see ARCHITECTURE.md for the authoritative system map).*

---

## What this thing is

PIBOX is a Raspberry Pi running **ORC-1**, a custom MIDI instrument and synthesizer engine. The Akai Fire is its main control surface - 64 pads, 4 dials, a bunch of buttons (the Fire sections below describe the handler used with Akai's own firmware; with this project's custom Fire firmware the Fire runs its own sequencer and screens, see ARCHITECTURE.md). The Pisound HAT gives it stereo audio + DIN MIDI I/O. A Zoom L-12 (when present) is the usual master audio interface. The whole thing runs headless, boots in ~30 seconds, and connects to your gear like any other MIDI/audio device.

**Hardware:**
- Raspberry Pi (pibox) - hostname `pibox`, user `pibox`
- Akai Fire (USB)
- Pisound HAT (I2S audio - stereo in + stereo out, DIN MIDI in + out)
- Zoom L-12 (USB audio interface - the usual master card; exposes `system:playback_1-4` + `system:capture_1-14` on the reference rig)

**JACK audio topology (authoritative detail in `ARCHITECTURE.md`):**
- One **default** JACK server, owned by `orc1-jackd`, at 48 kHz. The master card is resolved `~/.orc1_audio.json` → L-12 → pisound → (after waiting up to 90 s) the first real card. NOT fixed to the L-12.
- Each voice runs in **its own host process** (2026-09 split) and publishes its own ports: `ORC1-Drum`, `ORC1-Poly`, `ORC1-Chip`, `ORC1-Sampler`, `ORC1-Mono`, `ORC1-BBKit`, each with `out_L`/`out_R`. **There is no `ORC1-Synth` client and no single summed-mix port** — the old `ORC1-Synth:out/drum/poly/chip/samp` names and `SYNTH.MAIN_MIX_BUSES` are gone.
- Which physical pair each voice sounds out of is set per module on `/patch` (persisted `~/.orc1_module_outputs.json`).
- A voice (all but BBKit) can be sent to **another card**: the CARD picker on `/patch` starts that card's own named JACK server (`orc1-jackd@<card>`) and moves the voice's host onto it. Such a voice is not in the default graph. An on-demand `zita` bridge (also from `/patch`) is what pulls a second card's *ports* into the default graph, e.g. to capture from it; it is never `alsa_in`/`alsa_out`.
- Skipback capture: each track records a JACK source you choose on `/skipback`. With the source on `auto`, the recorder tries the L-12 master return (`system:capture_13/14`), then pisound's inputs, then the card's first stereo input. The recorder owns its own wiring, so change it on `/skipback`, not by hand.

---

## Starting and stopping

Everything runs as systemd services. You generally never need to touch them - the Pi auto-starts on boot.

```bash
# Restart the brain (MIDI/chord/arp/seq). The six voice hosts follow it (PartOf).
sudo systemctl restart orc1

# Restart the JACK server itself (soundcard switch, or jackd wedged).
# This cascades the voice hosts automatically via BindsTo.
sudo systemctl restart orc1-jackd

# Restart just one voice host if needed
sudo systemctl restart orc1-poly    # or orc1-drum / orc1-chip / orc1-sampler / orc1-mono / orc1-bbkit

# Status check
systemctl status orc1
systemctl is-active orc1-drum orc1-poly orc1-chip orc1-sampler orc1-mono orc1-bbkit

# Watch live logs
journalctl -fu orc1
```

> **`orc1-synth` is retired** — it is disabled and inactive on purpose (`orc1_synth.py` is now a shared library the hosts import, not a service). Don't restart or enable it.

**If the Zoom L-12 isn't on when the Pi boots:** turn it on, then run `sudo systemctl restart orc1-jackd`. The card is resolved by the JACK owner, so restarting a voice host alone will *not* switch cards. Give it ~5 seconds after restart for connections to appear.

**SSH access:** key-based, no password/TOTP for the key path:
```bash
ssh pibox@pibox.local
```

---

## The control surface at a glance

```
+---------------------------------------------------------------+
|  [STEP] [NOTE] [DRUM] [PERF]    SHIFT  ALT  PAT/SONG          |
|                                                               |
|  [PAD ROW 0: cols 0-15]         MUTE1                         |
|  [PAD ROW 1: cols 0-15]         MUTE2                         |
|  [PAD ROW 2: cols 0-15]         MUTE3                         |
|  [PAD ROW 3: cols 0-15]         MUTE4                         |
|                                                               |
|  DIAL0  DIAL1  DIAL2  DIAL3                                   |
|  [SELECT encoder]                                             |
|                                                               |
|  [BROWSER]  PAT UP  PAT DN  GRID L  GRID R                    |
|  [MODE]     PLAY  STOP  REC                                   |
+---------------------------------------------------------------+
```

**Key modifier rules:**
- **SHIFT + anything** = alternate function (tap tempo when combined with PAT/SONG)
- **ALT + anything** = tertiary function (clock-out toggle on PAT/SONG)
- **BROWSER** = cancel / back / open menu (context-sensitive)
- **MODE** = cycle through views in order
- **SELECT (turn)** = scroll / navigate
- **SELECT (press)** = confirm

---

## Views

*These views belong to the Fire handler (`orc1_fire.py`), which the brain uses for a Fire on Akai's firmware or on the custom firmware's host mode. A Fire on the custom firmware's normal sequencer mode is a standalone instrument and does not use them.*

Press **MODE** to cycle through the eight views (`seq, mixer, chord, chip, poly, faders, universal, fx`). The box **boots into `universal`** (Fire reorg, 2026-09-02). The top buttons are quick-jumps (current bindings): **STEP** toggles between `universal` and `seq`; **DRUM** toggles between `seq` and `mixer`; **PERFORM** jumps to `chord`; **PAT/SONG** jumps to `fx`; **NOTE** couples the chord engine to the poly synth (SHIFT: the chip voices, ALT: the arpeggiator). The OLED always shows which view you're in. (Button bindings are data in `orc1_config.py` and can be changed.)

### `seq` - Step sequencer

The main view. 4 rows x 16 columns = 64 steps visible at once. Each row is one track. Tracks can have different lengths (polyrhythm). The playhead scrolls across; pads light up for active steps.

- **Pads**: toggle steps on/off. Bright = active. Step with accent = extra bright.
- **MUTE1-4**: mute/unmute the visible rows
- **GRID LEFT/RIGHT**: page horizontally (steps 1-16, 17-32, etc.)
- **PAT UP/DOWN**: scroll vertically through tracks
- **SELECT turn**: pick the selected track (SHIFT + turn moves it up or down the list). **Hold a step pad and turn SELECT** to give that step its own note; hold a step pad and press SELECT to clear it.
- **SELECT press**: open the track editor (press again to close)
- **PLAY / STOP**: transport. **REC** clears the visible tracks, **SHIFT+REC** adds a track, **ALT+REC** toggles velocity lock (every note goes out at 127).
- **ALT + pad**: set the track length at that step
- **Dials**: sculpt the selected drum voice (attack, decay, tune, level; with SHIFT the filter and tune/level). With ALT held: tempo, track velocity, track colour, track brightness.

### `mixer` - Per-track velocity mixer

Dials control velocity for the 4 visible tracks. Pads show velocity bars. SELECT press solos the selected track.

### `chord` - Chord control and chromatic keyboard

Plays chords into the engine. The left 4x4 block is the chord-engine control panel and the rest of the grid is a chromatic keyboard.

- **Control panel (columns 0-3)**: row 0 = chord base (MAJ, MIN, SUS, DIM); row 1 = extensions 6th / 7th / 9th / 11th (held while pressed; with SHIFT the chromatic 6 / m7 / M7 / 9); row 2 = INV- / INV+; row 3 = CLR, PREV, HOLD, ARP.
- **Keyboard (columns 4-15)**: four octaves, lowest on the bottom row, one note per column starting at C. Each row plays where the chord-row map sends it (set on `/control`; by default top to bottom: the poly synth, chip pulse 1, the mono synth, and the chord engine).
- **PAT UP/DOWN**: octave
- **DIAL0 / DIAL1 / DIAL2**: chord spread, octave offset, tempo
- **SELECT turn**: scroll the row stack (SHIFT: arp pattern). **SELECT press**: chord parameter menu.

### `chip` - NES/GameBoy voices

Direct access to the chip voices: Pulse 1, Pulse 2, Triangle, Noise.

- **Pads**: play notes
- **DIAL0-3**: cutoff, resonance, attack, decay (with SHIFT: filter mode, release, timbre, octave), sent to the chip's first pulse voice
- **BROWSER menu**: Coupling Mode (none / unison / detune / fifth / third / octave / octave_dn) and Detune Amount
- **SELECT press**: voice summary

### `poly` - Internal poly synth

A keyboard in fourths (each row a perfect 4th above the one below) into the polyphonic synth.

- **Pads**: play notes
- **DIAL0-3**: cutoff, resonance, waveform, octave (with SHIFT: filter mode, release, attack, decay)
- **SELECT press**: voice summary

(The dial lists are the built-in defaults; a map you saved in `~/fire_knobmap.json` overrides them, see Encoder remapping.)

### `faders` - assignable fader bank

The continuous counterpart of the universal pad grid: long assignable faders
that send MIDI CC (or note/PC) anywhere, alongside the pads and rotaries.
Boots as a MASTER/POLY/CHIP/DRUM bus mixer; every lane is reassignable.

- **Tap a lane, then press SELECT**: enter assignment mode for that fader
- In assign mode: **E0** number, **E1** value, **E2** channel, **E3** port;
  **SELECT turn** cycles note/CC/PC; **SELECT press** confirms; **BROWSER** cancels
- **GRID < >**: page the bank by 4 lanes
- **BROWSER menu** -> "Save Faders" / "Load Faders" for 8 snapshot slots

(This view was called `mpc_mix` before 2026-07-13.)

### `universal` - Universal control surface

Assigns pads freely to any MIDI note, CC, or program change on any channel/port. Each pad can be momentary or latching.

Its purpose is sending MIDI OUT of the box - every other view is the
instrument's internal control surface.

- **Tap a pad, then press SELECT**: enter assignment mode for that pad
- In assign mode: **E0** number, **E1** value, **E2** channel, **E3** port;
  **SELECT turn** cycles note/CC/PC; **SELECT press** confirms; **BROWSER** cancels
- **GRID < >**: page between pad banks (4 by default, `FIRE["UNIVERSAL_BANKS"]`,
  max 9). The OLED always shows which bank you are on. Each bank holds its own
  64 assignments and all of them persist.
- **BROWSER menu** -> "Save Pads" / "Load Pads", "Save Knobs" / "Load Knobs",
  "Clear This Pad" and "Clear Bank" (scoped to the bank on screen, not all of them), "Toggle Latch"

### `fx` - FX chains

Manage LV2 plugin chains on the Fire. This drives the **legacy single-chain rack**
(`orc1_fx_rack.py`), which still backs the Fire fx view and the `/fx` web page.

- **Row 3 pads (bottom row)**: FX slots. Lit = a chain is assigned. Bright = running.
- **SELECT press on a lit slot**: select it (shows on OLED)
- **PAT UP/DOWN**: scroll through the available chains in the library
- **SELECT press (confirming a chain)**: assign it to the slot
- **DIAL0-3**: control parameters of the selected slot's plugin

The plugin palette spans Airwindows, Calf, and x42, curated to a small set of
"keeper" favourites (the rest stay browsable by category). Chain names come from
the live palette, not a fixed list. The **current, as-built FX model** — arbitrary
per-source wet/dry *inserts* (`orc1_fx_inserts.py` / `orc1_fxmix.py`), rideable at
audio rate, with no bridging between modules — is documented in `FX_SYSTEM.md`;
read that before touching FX.

> **Removed:** the Fire **ROUTE submode** (source→FX→destination routing on the
> pad grid, hold-PAT/SONG to enter) was **removed entirely on 2026-08-06**. All
> audio patching is now web-only on the **`/patch`** page (bring a card into the
> graph, then route each module's output). Anything you remember about ARM pads,
> the P.1/POLY/DRM/MAIN source pads, or `~/orc1_routing.json` as the live route
> table is gone — `/patch` persists to `~/.orc1_patch.json`.

---

## Web control pages

The Pi serves a phone-friendly web control surface at **http://pibox.local:5000**. It is one Flask app; every page shares the same dark theme, the live/stale/dead engine dot in the header, and a single ~1.5 s state poll. Editable fields are focus-guarded, so a value you are mid-edit is never overwritten by a poll refresh. As of 2026-09-15 the navigation is a **grouped expanding rail** at the bottom (category buttons — PLAY · SIGNAL · SAMPLE · VOICES · RHYTHM · CONTROL · SYS — that open a sub-row of pages; the current page's group auto-opens).

The table below lists the **main** pages; the live nav has more (per-controller pages such as `/beatstep`, `/rc505`, `/cc`, `/mono`, `/beatbuddy`). The two most important routing pages are **`/patch`** and **`/skipback`**:

| Path | What it is |
|------|-----------|
| `/` (PLAY) | Everything you touch while playing |
| `/patch` | **Audio patchbay** — bring a card into the graph (CARDS), route each module's output, wire arbitrary JACK connections. This is where per-module output routing and second-card bridges live. |
| `/matrix` | Per-voice **MIDI channel** binding, P1/P2 join, levels |
| `/routes` | MIDI routing matrix + chord/melody-out overrides |
| `/hud` | **HUD**: the live play-state on your phone (the same screen the monitor kiosk draws). See [HUD mode](#hud-mode-live-play-state-on-the-hdmi-monitor) |
| `/cme` | **CME U6MIDI Pro** MIDI router, read back live from the box: switch its 4 presets, click its 6x6 router (USB In 1-3 / DIN In 1-3 -> USB Out 1-3 / DIN Out 1-3). Per-port **filters** (12 ports: block any of the 16 channels and any of 18 message types; lit = passes). **Mappers**: all 16 listed in plain words; click one to edit it (inputs, source/target type, channels, value ranges, follow/invert/curve, Notes transpose octave/semitone/random skip, Global channel update) with the same rules as the UxMIDI Tools app. The 4 user **curves** are drawn (edit those in the app). Changes save on the box at once. The ORC never uses the CME's ports as instrument inputs (it's a patchbay; `PATCHBAY_PORT_PREFIXES` in the config). From the FCB / any trigger: `cmd:CME_PRESET` = next preset. |
| `/skipback` | Skipback track config — what each ring records |
| `/synth` | The polyphonic synth voice |
| `/chip` | NES + GameBoy chip voices |
| `/mono` | Mono/lead voice |
| `/drums` | 909 drums + sample slots |
| `/seq` | Step sequencer |
| `/sampler` | Live-loop / DJ sampler decks (+ capture source per deck) |
| `/fx` | FX chains (legacy rack; see `FX_SYSTEM.md` for the current insert model) |
| `/control` | Fire knob map, pads, chord rows |
| `/pads` | **Pad maps**: give any device's pads (a drum brain, a pad controller) their own job — a note on a named module, a CC, or an action. See [Pad maps](#pad-maps-drum-brains-and-other-pads) |
| `/cc` | The CC map: every control-change number the instrument uses, generated from the code. It rebuilds itself (a ~3 s first load) whenever the code has changed since it was last built, so its "Generated" date is the date of the last code change you've viewed it after |
| `/system` | Pi health, audio out, config |

### PLAY - `/`
The home page. A BPM block (tempo readout, -5/-1/+1/+5, TAP, clock source INT/EXT/DRIFT, CLK OUT), a MIDI + AUDIO card (connected external devices with RESCAN, DIN routing THRU/ENGINE, the POLY and CHIP chord-mirror toggles, STEREO mode with pan presets and per-bus pan sliders), and the chord/arp card. The chord card holds the root and scale pickers, voicing, STAGE ROOT / AUTO CHORD, DIAT LOCK, VL, MIRROR, NOTE THRU, the PANIC and RESTART buttons, and the full arpeggiator section (on/hold, pattern, rate, gate, swing, humanize). A compact PI HEALTH card at the bottom links to SYS.

### SYNTH - `/synth`
Sliders for the poly voice on MIDI channel 8: waveform, attack, decay, sustain, release, level, filter cutoff / resonance / mode, and octave. Sliders read back the last value the engine forwarded to the synth bus from any source (web, Fire, or Mixtrack), so the page reflects hardware moves too.

### CHIP - `/chip`
Per-voice panels for the chip engine: Pulse 1, Pulse 2, Triangle, Noise, GameBoy Wave, and PCM. A banner at the top of the page shows where chip audio is currently routed and, when the chip pair is unconnected (common on a 2-output card), offers a one-tap FIX to send chip audio to the main output pair. This is how you get the NES/GB voices to actually make sound without editing config. Includes the Pulse 1+2 couple mode, detune, and stereo spread controls, plus per-voice test-tone buttons. The newer controls (wave harmonics and slots, the second noise voice, glide, pan, arp/trill, slide, tremolo, retrigger) are not on this page yet - see **Chip voices** below.

### DRUM - `/drums`
Fourteen voice panels - ten 909 drum voices plus four sample slots - each exposing its six drum parameters (level, tune, attack, decay, filter freq, filter res) and the high-pass band. Each voice has an audition trigger. The sample-slot list shows which four WAVs from the `samples/` folder are currently loaded (the synth loads the first four alphabetically). Pattern programming lives on SEQ.

### SEQ - `/seq`
The full step sequencer: tracks, steps, per-step note edit, track length, transport, and the record-channel picker. Behaves as it always has, restyled onto the shared shell.

### SMPL - `/sampler`
Both sampler decks with GRAB 8s and 1/2/4/8-bar capture, LOOP / REV / STOP / SAVE, the waveform view with the nudge row, speed and volume, and the crossfader. File decks let you browse `~/recordings` and the samples folder and load any audio file onto a deck. The skipback source card (SYNTH DIRECT / L12 MASTER presets plus free port pickers) lives here - it is what the sampler hears. Per-deck EQ and filter sliders mirror the Mixtrack's knobs.

### FX - `/fx`
Lists the FX chains from `fx_chains.json` with a RUN toggle and parameter sliders per chain (labels and ranges come from the chain definition), and shows which chains the Mixtrack FX buttons are bound to. An FX I/O card switches the send/return between HARDWARE INSERT (the pisound loop) and INTERNAL (processes the synth output with no external cabling) and warns when the configured ports are not present on the active card.

### CTRL - `/control`
The universal control surface editor: the Fire's four knobs across three layers (BASE/SHIFT/ALT) with a target dropdown, the chord-row routing editor (each of the four Fire pad rows to the chord engine, a direct port/channel, or off), the universal pad-grid editor, a live MIDI port scan, connected-controller status with RESCAN, and the voice map. Changes apply immediately and persist; the Fire's own on-hardware mapping stays in sync.

### SYS - `/system`
Pi health: CPU temperature and power status, CPU/RAM/disk gauges, service status, and recent recordings. The AUDIO OUT card picks the output card; the BLUETOOTH card makes the Pi discoverable as "pibox". A CONFIG OVERRIDES card edits whitelisted config keys (persisted to `~/.orc1_config_overrides.json`, with a per-key restart chip when needed). Reboot and shutdown live on the separate system dashboard at **http://pibox.local:5002**, linked from this page.

The **HUD** card on this page starts and stops the [HUD kiosk](#hud-mode-live-play-state-on-the-hdmi-monitor) on the monitor.

### MODULE MATRIX - `/matrix`
One row per voice ("module"), where you decide **which MIDI channel each voice listens on** - voices have no fixed home channel. Tap a module's CH chip to arm it, then pick any free cell on the 1-16 channel bus to move it there (live, and it persists across reboots). The power button brings a module into the fold or takes it out (freeing its channel); the sampler is always in. **Protected channels** (12 chord-in, 13 ORC control, 14 MPC by default) are struck out and can't be picked; the PROTECTED card on this page switches each one on or off (13 is locked on, because the brain consumes it). Protection is checked when you *choose* a channel, so a binding that already exists is never dropped. The setting is saved in `~/.orc1_protected_channels.json`. Each row also has a live LEVEL fader (ephemeral - not saved) and a **params ▸** drawer listing that voice's CCs, and CHIP P1 carries a **JOIN P2** control that couples pulse 2 to pulse 1 at an interval (none / unison / detune / fifth / third / octave / octave-down) - the join is live and resets to none on restart. **CHIP NS2** is the second noise voice; it starts out of the fold, so give it a channel here if you want it on its own (or reach it through the noise channel's key split - see **Chip voices**). Audio routing stays on `/patch`; this page is MIDI binding + level + join.

### ROUTES - `/routes`
The full MIDI routing matrix editor, plus the chord-out and melody-out override cards. Reached from the "OUT" links on the sound pages rather than from the nav bar.

---

## Sampling from the Mixtrack

Both Mixtrack decks are sampler decks. In the one pad layout you use, the **top four pads** grab loops and the **bottom four** are hotcues; **PLAY** loops, **CUE** stops, **SYNC** reverses.

**Grab direction - retroactive vs forward.** The top pads capture in one of two directions:

- **Retroactive (default, purple pads):** a top pad grabs the *last* 1/2/4/8 bars (pads left→right) and loops it immediately - the skipback grab.
- **Forward (blue pads):** the pad captures the *next* bars instead, with **your press as the downbeat** (not the ORC's internal clock - so it lines up with what you're actually playing). Tempo only sets the bar length.
  - **Tap** a pad = capture a fixed N bars forward, then loop.
  - **Hold** a pad = record from your press until you release; the end snaps to the next bar. The pad **blinks while it records** and flashes when the grab commits.

**Switching direction:** press **PAD MODE** to toggle a deck between retroactive and forward (the top row turns purple↔blue to show which). **SHIFT + a top pad** does the opposite direction for one grab without changing the mode.

**Tempo.** The **TAP** button sets the tempo (tap it in time), and its **LED flashes on every beat** so you can see the current tempo at a glance - the flash re-syncs to your taps. Forward-capture bar lengths follow this tempo, so tap it in before a forward grab.

> The PAD MODE binding is provisional - if pressing it also cycles the unit's built-in pad banks in a way you don't want, it can be moved to another button.

---

## Chip voices - wave shaping, second noise, performance CCs

Every control below is a plain MIDI message on **that voice's own channel** (whatever
channel `/matrix` has it on), so any controller can drive it: point an MPK Mini's knobs
and pads at these numbers, map them on the Fire's universal surface, or send them from a
sequencer. Everything starts **off**, so a voice sounds exactly as it did until you touch
these. All of it except the momentary STORE button is remembered across restarts.

### GameBoy wave - shape it instead of drawing it

The wave voice plays a 32-step, 16-level wave. Instead of drawing 32 numbers, you mix
**eight harmonics** and the ORC builds the wave from them:

| Control | What it does |
|---|---|
| CC 102-109 | Harmonic 1-8 level (0 = absent). Any move rebuilds the wave and plays it. |
| Program Change 0-15 | Recall wave slot 1-16. An empty slot does nothing. |
| CC 115 held (>= 64) + Program Change | **Store** the wave you're hearing into that slot. |

- Turning a harmonic knob **replaces** the current wave - store it to a slot first if you
  want to keep it.
- Slots 1-8 start as the eight preset shapes (triangle, sine, saw up, saw down, square,
  pulse 25%, double saw, warm); slots 9-16 start empty.
- The slots, the current wave and the knob positions are saved in `~/.orc1_gb_wave.json`.
- CC 1 still picks one of the eight presets directly, and CC 20/21/23 still write the
  wave one step at a time (the old drawing method).

### Two noise voices at once

The noise voice's white/buzzy switch (CC 3) used to cover the whole channel, so a beat
couldn't use both. There is now a **second noise voice**, `CHIP NS2` on `/matrix`
(buzzy by default, with its own CC 3). Two ways to use it:

- **Give it its own channel** on `/matrix` (it starts unassigned - you choose).
- **Key split:** CC 114 on the noise channel sets a note number; notes at or above it play
  the second noise voice. One pad bank then plays white on some pads and buzzy on others.
  CC 114 = 0 turns the split off.

### Performance CCs - every chip voice

Pulse 1, Pulse 2, Triangle, both noise voices and the GameBoy wave all take these (PCM
takes pan only):

| CC | Control | Range |
|---|---|---|
| 5 | Glide (portamento) | 0 = off, then 5 ms to 2 s |
| 86 | Pan | 64 = centre, 0 = hard left, 127 = hard right |
| 87 | Arp note A | 0-24 semitones above the note (0 = off) |
| 88 | Arp note B | 0-24 semitones (0 = off). Set only A **or** only B for a two-note **trill** |
| 89 | Arp / trill speed | 4 to 60 steps per second |
| 90 | Pitch slide amount | 64 = none, 0 = down 2 octaves, 127 = up 2 octaves |
| 110 | Pitch slide time | 10 ms to 2 s to reach the full amount |
| 111 | Tremolo depth | 0 = off |
| 112 | Tremolo rate | 0.5 to 20 Hz |
| 113 | Retrigger rate | 0 = off, then 1 to 40 re-strikes per second while the key is held |

- The arp works on a single held key, tracker-style; it is separate from the ORC's main
  arpeggiator.
- Slide starts from each new note and holds at the end: a high noise note with a downward
  slide makes a laser or kick drop.
- Retrigger re-strikes the note's envelope, so it sounds best on short, plucky settings
  (hi-hat rolls on noise).
- Pan puts each chip voice in its own place in the stereo picture of the chip output
  (`ORC1-Chip`), on top of the older Pulse 1+2 spread.

> **Not on the web pages yet:** `/chip` has no controls for any of this, and the GB WAVE
> **params ▸** drawer on `/matrix` doesn't list CC 102-109 / 115 or the performance CCs.
> Drive them from a controller for now.

---

## Presets

Three independent preset banks, 8 slots each. Access via **BROWSER menu** in the relevant view.

| Bank | Where | Menu items |
|------|-------|------------|
| Pad assignments | `universal` view | Save Pads / Load Pads |
| Fader assignments | `faders` view | Save Faders / Load Faders |
| Knob map | `universal` view | Save Knobs / Load Knobs |

Stored in `~/fire_presets.json`. Picking a slot with SELECT turn, confirming with SELECT press. BROWSER cancels.

---

## Encoder remapping

Any dial can be remapped, per view and per layer (base / SHIFT / ALT). BROWSER
menu -> "Remap Encoders" in the view whose knobs you want to change. Touch a
knob to pick it (hold SHIFT or ALT while touching to edit that layer), turn
SELECT to scroll the target list, press SELECT to assign. BROWSER exits.

The first entry in the list is **CC OUT**, which opens an editor for an
ARBITRARY destination: **E0** CC number, **E1** channel, **E2** port, SELECT to
commit. The port list is enumerated live from the engine, so anything currently
connected - the pisound DIN, the internal synth bus, an rtp session, a
hot-plugged USB device found by SCAN PORTS - is selectable. Every other entry
in the list is a fixed, named target whose port/channel/CC are set in
`orc1_config.py`.

The whole overlay is pinned to the view it was opened on, so a stray mode
button press mid-edit cannot land the assignment on a different view's knob.

`universal` and `faders` do not offer the drum-sculpt targets: those views have
no drum voice, and the loader strips such an assignment at startup, so offering
them would promise something that silently reverts.

Stored in `~/fire_knobmap.json`, and arbitrary CC assignments survive a restart
(before 2026-08-01 they did not - the loader accepted only the named targets).

---

## Skipback recorder

A separate always-on service (`skipback-record`) that continuously buffers audio into per-track ring buffers, so you can keep something *after* it happened. Each track records a configurable JACK source, set on the **`/skipback`** web page (persisted to `~/.orc1_skipback_tracks.json`). A track can record any JACK port; on the reference rig the L-12 exposes 14 capture channels, all recordable (its stereo master is `system:capture_13/14`). Only tracks marked `armed` in the tracks file record. The source picker also lists each ORC module's own output (`Module: Poly`, …) whenever that module is running.

**QUICK ADD** (the picker next to + ADD TRACK) makes a track in one pick, already named and wired. Pick a **stereo pair** (a named pair such as `Zoom Track 9-10`, or any `In n/n+1` input pair) for one stereo track, or a **mono channel** (`In 5`, a single module output) for a mono track. Then press SAVE.

The skipback JACK connections are **owned and re-reconciled by the recorder every watchdog pass**. `/patch` deliberately cannot steal a skipback source, so change it on `/skipback`, not by hand. Port names depend on the track's channel count: a stereo track 0 is `t0_L`/`t0_R`, and a mono track is a single `t0_M`.

### Where a track's ring lives: RAM or CARD

Each track has a **RAM / CARD** switch on `/skipback`.

| | **RAM** (default) | **CARD** |
|---|---|---|
| Ring file | `~/.skipbuf/t<id>/ring.raw` (tmpfs = memory) | `/mnt/skipring/t<id>/ring.raw` (8 GiB ext4 partition on the boot card) |
| Budget | **900 MB total** for all RAM rings (top meter) | the card partition, ~7.9 GB (second meter) |
| After a reboot | empty | **PERSIST** (default): history resumes. **FRESH**: starts empty each boot |
| Cost | ~0.35 GB RAM per mono channel-hour | ~0.35 GB card per mono channel-hour, plus ~8 MB of RAM buffer per channel |

What each ring costs: at 16-bit/48 kHz, one mono channel is 96,000 bytes/s. One hour of one channel is ~0.35 GB, and **14 L-12 channels × 1 hour ≈ 4.8 GB**. That only fits on CARD.

How CARD tracks work:
- **Only the audio goes on the card.** The tiny position file (`ring.pos`, rewritten ~24×/s) stays in RAM, and a `ring.raw` symlink in the RAM track dir points at the card file, so every reader (grabs, the sampler) finds the ring in the usual place.
- **The ring file is reserved at full size when the track arms.** It can't run out of space mid-recording.
- **Persistence:** once a second the recorder flushes each card ring and then records every head position in one file, `/mnt/skipring/checkpoint.json`. After a reboot, or even a power cut, a PERSIST track resumes where that checkpoint says, losing only the last second or so. Measured 2026-09-28: after a real reboot, 14/14 tracks resumed at their exact pre-reboot head, with the audio before it byte-identical.
- **Card stalls are expected and absorbed.** SD cards pause for seconds while they reorganise internally: up to ~10.6 s measured on the reference card. Each CARD channel therefore gets a ~43 s memory buffer in front of the card (RAM tracks get ~11 s). The recorder also has memory reserved that no other program can take (256 MB guaranteed, protected up to 1 GB). RAM and CARD tracks have **separate writer threads**, so a card stall never touches a RAM track (measured 2026-09-29: with one shared writer, RAM tracks dropped audio during card stalls). `/skipback` status reports the slowest card checkpoint and writer passes since start (`ckpt_max_ms`, `writer_card_max_ms`, `writer_ram_max_ms`); the card figures are your margin against that 43 s.
- **Sampler decks should grab from a RAM track.** A grab can only see audio already written to the ring, so during a card stall a grab from a CARD track would land early by the stall. A typical setup keeps long history on CARD tracks and small 60 s RAM tracks for the sampler. Each deck's grab source defaults to t0 (deck A) and t1 (deck B) and is whatever you pick on `/sampler`; the choice is kept in `~/.orc1_sampler_deck_src.json`. The Mixtrack's FX1-3 and the `/sampler` list offer RAM tracks first, and `/sampler` labels CARD tracks.
- **Safety:** a CARD track **refuses to arm while `/mnt/skipring` isn't mounted**, because it would otherwise silently fill the system partition. RAM tracks keep recording regardless.
- **Switching a track between RAM and CARD rebuilds its ring**, so the audio it currently holds is lost. The page asks first.
- **Trim.** Linux's weekly `fstrim.timer` is on. Run `sudo fstrim -v /` and `sudo fstrim -v /mnt/skipring` once after cloning or re-imaging a card, so the card knows which space is free.

### Grabbing

- **One track:** the 5s/30s/60s/5m/10m buttons under each track, `skipback/skipback.sh <secs> [wav|mp3] [track]`, the pisound button (track 0), or the Mixtrack (SHIFT+FXn). The grab **streams** the ring straight into a WAV in `~/recordings`, 4 MB at a time. It never loads the whole span into memory: a full 20-minute stereo grab dips free RAM by ~71 MB (it used to be ~360 MB).
- **Grabs end at your press, even during a card stall.** The caller stamps the press time (`SKIPBACK_PRESS_TS`). If a track's head hasn't moved since before the press (its writer is stalled), the grab waits for it (up to 15 s, `SKIPBACK_FRESH_WAIT`), then trims back to the press. It never ends early at the stalled head, and never late. A head that never moves (recorder stopped) is grabbed as-is after the wait. GRAB ALL does the same per track, keeping every file sample-aligned.
- **GRAB ALL (sample-aligned):** the GRAB ALL card on `/skipback` (tap a track to leave it out; 30s/60s/5m/10m/ALL), `POST /api/skipback/grab_all {seconds, tracks}`, or the brain command `SKIPBACK_SAVE_ALL` (e.g. an FCB switch set to `cmd:SKIPBACK_SAVE_ALL`; a second press while one is still saving is ignored).
  - It writes **one folder**, `~/recordings/multi_<n>tr_<secs>s_<time>/`, with one WAV per track plus `manifest.json`. Every WAV starts on **the same sample** and has the same length, so they line up in a DAW at zero.
  - It works because the recorder stamps every audio period with the JACK clock and publishes every track's exact head in `~/.skipbuf/heads.json`.
  - **ALL** means everything the chosen tracks hold in common since the recorder last started. Older history, such as pre-reboot audio on a CARD track, is kept, but it isn't aligned, so reach it with single-track grabs.
  - **STEREO JOINS:** on the GRAB ALL card, join two MONO tracks (L + R) and every GRAB ALL writes them as ONE stereo WAV (`t03+t04_<names>.wav`), still on the same sample as the rest. Joins are saved on the Pi (`~/.orc1_skipback_joins.json`), so the FCB's `SKIPBACK_SAVE_ALL` uses them too. A join that can't apply (a track is stereo, or left out of this grab) is written as separate files, and the page and `manifest.json` (`joins_skipped`) say why. Recording is unchanged: tracks stay mono in the rings, and a join only shapes the saved files.
  - If a track ever drops audio (status shows `⚠ N dropped period(s)`), the manifest flags it. Audio before the drop may sit one period (~21 ms) early on that track.

**Aligning other gear:** anything plugged into the L-12's inputs arrives on one converter clock, already sample-aligned with every other L-12 channel. That's the simplest way to capture outboard gear, laptops and phones together. A track fed from a different card (a bridge) or over the network arrives a constant few tens of ms later.

---

## DAW mode (REAPER on an HDMI monitor)

The ORC runs headless. For editing recordings (e.g. a GRAB ALL folder) there's an on-demand **DAW mode**: REAPER fullscreen on a monitor on the Pi's HDMI port, with a USB mouse and keyboard.

- **Start/stop from the web:** the **DAW / DJ MODE** card on `/system` has START DAW, START DJ (MIXXX), the dimmer DAW · KEEP ENGINES / DJ · KEEP ENGINES, and STOP. It shows what's running, whether the engines are off, and whether a monitor is plugged in. From a shell: `sudo ~/orc/orc1/daw/daw_mode.sh start [dj] [engines-on] | stop [force] | status`. The unit has no `[Install]` section, so it never starts at boot.
- **The engines go OFF by default** (decided 2026-09-29, after Mixxx plus all six engines swapped the Pi and skipback itself dropped audio: 12 gaps). Starting either app stops the six module hosts (drums, sampler, poly, chip, mono, BBKit) one at a time, to give REAPER their CPU and RAM. They come back by themselves, one at a time, **however** the DAW ends (quit, crash, STOP), because the unit's `ExecStopPost` runs `daw_mode.sh restore`. Skipback, jackd and the brain are never touched. Measured 2026-09-29: 0 xruns, 0 gaps through a full cycle.
- **Stop:** quit REAPER, or press STOP DAW. STOP **asks** REAPER to quit, exactly like its close box (`daw/daw_close.py` sends WM_DELETE_WINDOW). With unsaved edits, REAPER asks you on the monitor first, and the page says it's still open. REAPER is deliberately not killed: killing it mid-cycle costs one xrun on the whole graph (measured). `stop force` kills it anyway. Either way the X session ends and the unit stops by itself.
- **What it runs** (`systemd/orc1-daw.service` → `orc1/daw/daw_session.sh`): a minimal X server (Xorg + `xinit`, no desktop) with Openbox and **REAPER 7.81 for Linux aarch64** (`/opt/REAPER`, installed 2026-09-28). It runs in its own logind session on VT7.
- **Resolution:** set `DAW_RES` (e.g. `1280x720`) in the unit's environment to lower it.
- **Audio:** REAPER joins the ORC's own JACK server (`JACK_NO_START_SERVER=1`, never a second server). The L-12 inputs, the ORC modules and skipback are all ordinary JACK ports to it.
- **Plugins:** REAPER's own ReaPlugs (ReaEQ, ReaComp, ReaXcomp, ReaDelay, ReaVerb/ReaVerbate, ReaGate, ReaLimit, ReaPitch, ReaTune, …) and JSFX are built in. REAPER also loads LV2 and CLAP. Windows VSTs do not run on the Pi.
- **Extensions** (`~/.config/REAPER/UserPlugins/`): **SWS 2.14.0.7** (with its 82 groove templates in `Data/Grooves`) and **ReaPack 1.2.6**.
  - Add repositories in Extensions → ReaPack → Import repositories.
  - ReaPack's official list of 75 repository URLs is saved at `~/reaper_install/reapack_repositories.txt` for pasting.
  - ReaPack only offers packages that run on this platform. Most are Lua/EEL scripts and JSFX, which run anywhere.
- **License:** REAPER runs as a full 60-day evaluation. Import your own license in REAPER (Options → Preferences → General → License). One license covers any OS.
- **Measured cost:** REAPER ~220 MB and Xorg ~110 MB RSS at idle; with the engines running there's still ~770 MB free.

## DJ mode (Mixxx + the Mixtrack on an HDMI monitor)

**Mixxx 2.5.0** (Debian package, installed 2026-09-29) fullscreen on the same HDMI setup as DAW mode. The two share VT7, so only one runs at a time.

- **Start/stop:** START DJ (MIXXX) (engines off) / DJ · KEEP ENGINES / STOP on the `/system` card, or `sudo ~/orc/orc1/daw/daw_mode.sh start dj [engines-on]` (`systemd/orc1-dj.service` → `orc1/daw/dj_session.sh`). Engines-off and STOP behave exactly as in DAW mode. **Measured with the engines kept on (plus a library copy + Mixxx's first scan):** Mixxx ~780 MB, the Pi swapping, skipback missed deadlines and dropped 12 periods. Keep them off for DJ sets. STOP asks Mixxx to quit; with a deck playing, Mixxx asks you on the monitor first.
- **The Mixtrack belongs to Mixxx while it runs.** The unit makes `/run/orc1-dj-active` before Mixxx starts. The brain then ignores the Mixtrack's input and closes its LED output, so the two never fight over the lights. When the unit ends, however it ends, the marker goes and the brain takes the Mixtrack back (LEDs wake). The brain also re-checks every 3 s and at startup, and a replug mid-set stays with Mixxx. Mixxx's own mapping is **Numark Mixtrack (Pro) 3**: enable it in Preferences → Controllers.
- **Audio:** Mixxx joins the ORC's JACK server (`~/.mixxx/soundconfig.xml`): **Master → outputs 1/2** (the L-12 mains, so skipback records the set), **Headphones → outputs 3/4**. The Mixtrack's own outputs are a later step.
- **Library:** `~/Music` only (test tracks in `~/Music/test`). **Don't add `~/recordings`:** scanning ~900 MB of skipback grabs pushed Mixxx to ~1 GB RSS and the Pi into swap (measured).
- **Memory guard:** the unit caps Mixxx (`MemoryHigh=900M`, `MemoryMax=1100M`; loosened from 700M/900M on 2026-09-29, because squeezing an already-bloated Mixxx made its own audio late, and skipback now has a reserved floor of its own) and makes it the OOM killer's first pick, so it can never starve the skipback recorder. The caps are copied onto Mixxx's login session by `daw_mode.sh cap-session`, or they'd bind nothing.
- **Mixxx is a JACK client from the moment it starts,** even before any sound settings, and even with no ports (so `jack_lsp` doesn't show it). Don't kill it: that cost xruns (measured). STOP asks it to quit.

## VISUALS mode (line scenes on the HDMI monitor)

Glowing geometric line scenes, full-screen on the HDMI monitor, that move with the music: the ORC's own drums and chords, and any audio in the rig you choose (the L-12 master by default, so Bluetooth and everything on the mixer counts; or one input, or one module). It's one of the four VT7 kiosks (`systemd/orc1-visuals.service` → `orc1/daw/visuals_session.sh`), and it brings its audio listener `orc1-vistap` with it; starting DAW or DJ mode takes the monitor over from it. Besides `/visuals` and any MIDI controller, it can be played from the SmartPad: bank 2, RIGHT = the [VISUALS page](#bank-2-the-orc-surface-sampler-skipback-visuals) (scenes, peephole, background, START/STOP, every knob on seven encoder banks).

### The /visuals page (VIS in the nav)

Reorganised 2026-10-12. Every slider shows its value in real units (ms, Hz, ×, °) and a one-line hint; double-click any slider to reset it.
- **SHOW:** START / STOP / NEXT, a button per scene (holds it), CYCLE with its time, CLASSIC (the old XScreenSaver patterns), and FRAME RATE 30 / 45 / 60 (60 = ~17 ms less delay for roughly twice the CPU). 30 and 60 are locked to the monitor's refresh, so motion is perfectly even; **45 can't divide a 60 Hz screen evenly and will show a slight regular stutter**, so prefer 30 or 60. A scene change lands at once, without a restart. The **LOGO** picker folds out underneath (it opens by itself when LOGO or SHAPE4D is held). `/system` keeps a small START / STOP card that links here.
- **LISTEN:** **SOURCE** picks what the visuals listen to: any input pair (e.g. `system:capture_13 + capture_14` = the L-12 master on this rig), any single input (mono), any module's output (drums, poly, ...), or **skipback ring (the old way)**. A change takes effect within half a second, no restart. Below it, a live spectrum of what the visuals hear (streamed ~25 times a second; bars jump up and fall smoothly), scaled to the music's own level so a quiet stream still fills it. The frequency labels sit at their true places on the log scale. In the bass, neighbouring bands that come from the same measurement are drawn as one wide bar instead of repeats. **Drag across it to choose the BEAT BAND**, the frequencies that count as a beat (the lit band; drag an edge to adjust). Kicks live around 40-120 Hz (the default is 30-150 Hz), snares 150-300 Hz, hats 6-12 kHz. The dot lights on every beat it hears.
  - **Two bands: SHAPES and BACKGROUND** (2026-10-12). DRAG SETS THE BAND FOR picks which one a drag edits. Out of the box the background follows the shapes' band. Picking BACKGROUND gives it its **OWN BAND** (cyan, CC 59/60), starting as a copy of the shapes' band and SENS, so nothing changes until you move it. It also gets its own **SENS BG** (CC 61) and its own beat light. **BACKGROUND = SAME AS SHAPES** (CC 62 below 64) goes back to one band. Typical split: kick for the shapes (40-120 Hz), snare or hats for the background (150-300 Hz / 6-12 kHz). Two bands inside the low bass mostly hear the same thing (the ~47 Hz limit below). Both bands share ATTACK / RELEASE and the GATE; the background still also rises with kicks, notes and loudness, as before. Band 2 reads the same analysis as band 1, so it costs nothing measurable.
  - **The dotted outline is what the detector really hears.** It looks at only the newest 21 ms so it reacts fast, and at that length it can only tell frequencies ~47 Hz apart (47, 94, 141 Hz ...), each blurred about ±47 Hz. A 60-90 Hz band therefore really hears ~47-141 Hz; the default 30-150 hears ~20-188 Hz. The header shows both. (Before 2026-10-12 the page showed only the band you dragged, and its 1k / 10k labels sat over ~570 Hz / ~3 kHz.)

  Sliders underneath:
  - **AUDIO**: how much the audio moves the picture (down = off; a curve, so the low half is subtle)
  - **SENS**: a beat = the band jumps to this many times its last second (low = big hits only, 3×; high = 1.3×)
  - **ATTACK**: how fast a beat swells in: 0 = instant, 1 = one frame, up to 250 ms. **A beat always reaches its full size**; ATTACK only shapes the rise. (Before 2026-10-12 a slow ATTACK also shrank the beat, down to 29% of a hit with a short RELEASE, and the bottom 40% of the knob did nothing visible. ATTACK 0 behaves exactly as before.)
  - **RELEASE**: how long a hit lingers (0.08-1.5 s)
  - **GATE** (CC 54): anything quieter counts as silence (default −68 dBFS; all the way down = off). The auto gain keeps turning itself up in quiet passages to catch quiet Bluetooth, so without the gate hiss and the gaps between songs fired false beats: 8 s of hiss at −72 dBFS gave 13 beats, and with the gate none.

  The readout under it shows the delay from sound to picture (see below).
- **LINES:** SIZE (the whole picture ×1/3 .. ×3) and ANGLE (turn it −180..+180°): a camera on the finished lines, every scene, line thickness kept. Then SPEED, BEAT (the scene's own built-in kick reaction), BRIGHT, WEIGHT (line thickness; up = the overloaded, blown-out glow), COLOUR. The middle of each slider is the scene's own setting; CENTRE ALL resets the card.
- **TRAILS:** TRAILS (how long the ghosts of past frames last), **TRAIL ZOOM** (the ghosts get sucked in or rush outward) and **TRAIL TWIST** (the ghosts spin). TRAIL ZOOM / TWIST move only the ghosts, never the shape, so they do almost nothing with TRAILS low: the page dims them and says so. (These were called ZOOM and TWIST on the old LOOK card; same CCs.)
- **MOVEMENT:** background-style REACT + LFO for the shapes. One row each for SIZE, ROTATION, BRIGHTNESS, SWAY (a drift; the LFO draws a figure 8) and TRAILS, with a REACT slider (how much the beat moves it: drum kicks and the audio beat, shaped by LISTEN's ATTACK / RELEASE, plus loudness; separate from LINES' BEAT on purpose, so with BEAT at 0 only the MOVEMENT reactions follow the kick) and an LFO slider (how far the LFO swings it). All rest at 0 = off. One shared LFO: **SYNC** FREE (then RATE, 0.02-4 Hz) or 4 bars / 2 bars / 1 bar / 1/2 bar / 1/4 bar, locked to the ORC's bar while the sequencer runs (it glides onto the bar, never jumps) and at the ORC's tempo while it is stopped. The tempo comes from the brain's play-state feed. Live meters show the beat and the LFO.
- **BACKGROUND** and **PEEPHOLE:** see below.
- **MIDI:** which channel the sliders use (see Knobs), with a BIND button when none is set, and a fold-out list of every control's CC.

### What moves it

- **The audio you picked in SOURCE.** A tiny listen-only program, **`orc1-vistap`**, sits in the audio engine (JACK) and copies the chosen ports into memory every 21 ms; the visuals read that. It is written in C and does nothing but copy, so it cannot make the audio or the recorder glitch (a Python listener could: it would have to wait on the busy renderer). It has input ports only, so it can never change the sound. If it isn't running, the visuals fall back to reading skipback's RAM recording of the master, as they did before 2026-09-30. A beat = the BEAT BAND's energy jumping well above its last second. Loudness swells the lines and the speed. An automatic gain follows the music over ~15 s, so late-night quiet playback still drives it fully (its floor is -60 dBFS, set from the measured Bluetooth level, so the hiss between songs stays dark).
- **The ORC's own MIDI** (the internal bus):
  - a **kick** (note 35/36 on the drums or BeatBuddy-kit channel) pulses the scene
  - a **snare / clap** flashes it
  - the **lowest held note** sets the colour, round the circle of fifths, so related chords get related colours
  - busier playing speeds it up

  The bus carries no MIDI clock, which is why the ORC beat comes from kicks.
- Nothing playing = the scenes drift on their own.

### Background (EarthBound-style)

A repeating pattern behind the lines, bent the way the battle backgrounds in EarthBound bend theirs: the screen is cut into ~224 bands (the SNES's scanlines), and each band reads the pattern shifted by `amplitude × sin(frequency × band + speed × time)`.
- **Set + pattern:** SET picks a set of tiles and the gallery below it shows them; tap one.
  - **built-in** (10, drawn by the ORC): checker, stripes, dots, truchet (random quarter-circle tiles), waves, diamonds, rings, plaid, bricks, zigzag.
  - **hero** (87): Hero Patterns by Steve Schoger, CC BY 4.0.
  - **monster 1 / 2 / 3** (330 in pages of 128, so every tile has its own knob value): Pattern Monster, MIT.
  - **custom** (2026-10-12): your own tiles, always the last set (up to 128), kept in `~/orc1_patterns/`. With CUSTOM selected, an UPLOAD box appears under the gallery (and DELETE THIS ONE for the selected tile). A tile is an SVG drawing of ONE repeat (any canvas size: the drawing's width/height or viewBox only sets its shape; SCALE sets its size on screen). Draw it so it wraps seamlessly, in white or grey on black or transparent: the ORC adds the colour and reads only brightness, from the RED part of each colour, so a pure-blue drawing would show nothing. Every upload is checked before it is kept: 512 KB max; no DOCTYPE/ENTITY, scripts, embedded images or links to other files (the Pi renders it, so nothing in it may reach anything else); it must render; one that would draw nothing is refused with the reason, one with no black gaps is kept with a warning (it would cover the whole background). The same file name replaces your earlier one. New uploads appear in the running visuals within a second. (Adding this set moved every SET knob position's meaning, so a saved SET value is converted once to keep pointing at the same set; the state file now records the set count.)

  The file tiles live in `orc1/daw/patterns/` (with `CREDITS.md` and the script that converted them), and are rasterised once into `~/.cache/orc1/patterns/` (needs `rsvg-convert`, Debian `librsvg2-bin`). All are seamless, grey on black, and **their gaps stay black**; multi-colour Pattern Monster tiles keep their layers as different greys, so the palette colours them differently. A tile keeps its own shape (not forced square), and SCALE sizes file tiles in proportion to their own size.
- **Bend mode:** WAVE (bands slide sideways), INTERLACE (every other band the opposite way: the helix look), SQUEEZE (bands bunch up and stretch), TWO LAYERS (a wave layer and an interlaced layer blended), WAVE + SQUEEZE.
- A beat swells the sway gently (over ~150 ms) while the brightness still punches at once; a 40 ms yank read as a hitch.
- **LEVEL** = how much shows when nothing plays. **It starts at 0: black.** **REACT** = how much the music brings it in: kicks, notes, the audio beat and loudness. It rises quickly and fades back to black over ~0.7 s, and a hit also swells the bend. **LFO** = a slow breath on top (0 = off, else 0.02-1 Hz).
- **WARP / FREQ / SPEED** shape the wave, **SCALE** the tile size, **CYCLE** how fast the colours turn. The colours follow the chord (the same hue as the lines, shifted by COLOUR).
- The lines always punch through it, so the geometry stays on top.
- The background renders at **half resolution** and is smoothed up (a quarter of the pixels; EarthBound itself was 256×224). A very fine tile therefore looks a little soft and its thinnest gaps turn grey; raise SCALE if that matters.
- Cost (measured 2026-09-30 on the Pi's GPU at 1280×720): tube + background + star peephole = a steady **60 fps**. Before the half-res background and the drawn-once mask (below) that combination ran at 46 fps. Patterns load in the background, so switching never stutters.

### Peephole (for a projector)

The very last step before the screen: **everything outside the shape is pure black**, lines and background included, so a projector can throw a circle, a star, a hexagon... instead of a rectangle.
- **Shapes:** OFF (the whole screen), circle, rounded (a rounded rectangle the shape of the screen), hexagon, diamond, triangle, star.
- **SIZE** (middle = about the screen's height), **SOFT** (hard edge .. soft vignette), **X / Y** (middle = centred).
- The shape is drawn once into a texture when you change it, then just looked up each frame (computed per pixel per frame, the star alone cost 8 ms of the 16.7 ms a 60 fps frame has).
- **ROOM** (CC 51): FIT fits the scene to only part of the shape (default ~78%) so the trails have room to swirl outward; at 0 it fits right to the edge, which cuts the spiral off (the "lost twist").
- **FIT** (on by default): the scene is shrunk to sit INSIDE the shape and moved with it, trails included, so a hypercube stays whole inside the circle instead of being cut off at its edge. It fits to the shape's inscribed circle (measured for every shape on the GPU). When the scene reaches further (a hypercube turning), it shrinks at once so nothing pokes out, and it grows back over a few seconds so it doesn't pump. Line thickness stays the same. Tube and tunnel reach past the screen by design, so FIT puts their whole on-screen view inside the shape and the shape trims the rest. FIT at 0 = the shape just cuts the scene off at its edge.

### The scenes

`orc1/daw/orc_visuals.py`, drawn on the Pi's GPU (OpenGL 3.1): **tunnel**, **mystify** (mirrored six ways), **tube**, **harmonograph**, **warp** (hyperspace streaks), **hypercube** (a 4D cube turning through itself), **scope** (an XY goniometer of the real stereo audio: the picture IS the sound), **ridgeline** (the spectrum's recent history as a skyline), **rose** (the held chord's notes as spin ratios), **lorenz** (the chaotic attractor), **chladni** (vibrating-plate figures that change with the chord).

**Shape machines** (2026-09-30, `orc1/daw/vis_shapes.py`), the tube and the hypercube generalised:
- **knot**: a tube swept along a torus knot. The chord's lowest note picks the knot (trefoil, cinquefoil, ...), and the tube's cross-section is a superformula shape with as many lobes as the chord has notes; it breathes between round, star and flower.
- **polytope**: the hypercube's family turning through 4D: 5-cell, tesseract, 16-cell, 24-cell, 600-cell (120 corners, 720 edges) and two duoprisms. The chord's lowest note picks one, and a change crossfades.
- **hopf**: the Hopf fibration. Every point of a sphere is a circle in 4D; the circles over each latitude fill a torus, and every circle links every other. Three coloured tori slide through each other; more notes = more circles.
- **shape4d**: any flat shape pushed out into 4D and turned inside out like the hypercube (a square would give a tesseract). The shape is the chord's superformula, or the outline of the logo you picked.
- **logo**: a logo or line drawing turning gently in 3D, with a bright trace running along its lines, pulsing on the beat.

### Logos and line drawings

The **LOGO** picker on /visuals (folds out under SHOW; CC 52): CHORD SHAPE (no logo) or a drawing. It shows a preview and lets you **upload your own SVG line art** (2 MB max) into `~/orc1_logos/`, or delete your uploads. Shipped with the ORC (drawn for it, `orc1/daw/logos/`): an ORC-1 wordmark, a lightning bolt, a spiral eye. `orc1/daw/vis_logos.py` reads paths (every command, curves and arcs), lines, polylines, polygons, rects, circles, ellipses and transforms; fills become their outlines, and text, images, `<defs>` and scripts are ignored (nothing in an SVG is ever run, and the page's preview is drawn from the parsed lines, never the uploaded file). Big drawings are simplified to 4000 points. The logo scene uses the first drawing when LOGO is set to CHORD SHAPE.

The shape machines and the logo scene were measured holding 60 fps on the Pi. **Classic** = XScreenSaver's line patterns, drawn on the CPU (stock qix and vermiculate cost Xorg ~75% of a core, so qix runs as a single polygon and vermiculate is left out).

### Knobs (MIDI)

Every slider is a CC on the channel the **visuals** module is bound to (on `/matrix`, where its LVL = brightness, or with the page's BIND; unbound until you pick one). So any controller, or a Fire knob set to the visuals module, moves them too:

| CC | Control |
|---|---|
| 20 | scene (0 = cycle) |
| 21-27 | speed, trail twist, trails, trail zoom, bright, beat, colour |
| 28 | audio amount |
| 29 | attack (0 = instant; default ~20 ms) |
| 30 | release |
| 31 | sens |
| 32 / 33 | beat band low / high (log, 20 Hz-16 kHz) |
| 34 | background pattern (the range is split evenly over the chosen set's tiles) |
| 50 | pattern set (built-in, hero, monster 1-3) |
| 35 | background bend mode (split over the 5 modes) |
| 36-43 | background level, react, lfo, warp, freq, speed, scale, cycle |
| 44 | peephole shape (split over off + the 6 shapes) |
| 45-49 | peephole size, soft, x, y, fit |
| 51 | peephole ROOM: how much space FIT leaves round the shape for the trails' spiral (default ~22%) |
| 52 | logo (CHORD SHAPE, then each drawing) |
| 53 | WEIGHT: line thickness in every scene (middle = the scene's own, up = the overloaded glow, ~×2.5) |
| 54 | audio GATE (0 = off, else −90 .. −40 dBFS; default −68) |
| 55 / 56 | camera SIZE (×1/3 .. ×3) / ANGLE (−180 .. +180°); 64 = as drawn |
| 59 / 60 | the BACKGROUND's own beat band low / high (log, 20 Hz-16 kHz; used only with OWN BAND on) |
| 61 | SENS BG: the background band's own SENS |
| 62 | BACKGROUND OWN BAND: below 64 = the background follows the shapes' band (default), 64+ = its own |
| 57 | MOVEMENT LFO rate when FREE (0.02-4 Hz) |
| 58 | MOVEMENT LFO sync (split over FREE, 4 bars, 2 bars, 1 bar, 1/2 bar, 1/4 bar) |
| 70-79 | MOVEMENT react / LFO pairs: size 70/71, rotation 72/73, brightness 74/75, sway 76/77, trails 78/79 (0 = off) |

CC 64-69 are skipped on purpose (sustain and the other pedal numbers that gear may send). Positions survive a restart.

### Delay (sound → picture)

Measured parts, with the tap (the normal case):
- new audio arrives every **21.3 ms** (one JACK period) and is ~8-10 ms old when the renderer reads it
- the beat window is **21 ms**
- then a frame or two of drawing

That's about **60 ms at 60 fps** (~80 ms at 30 fps), plus your monitor's own delay (TVs add a lot; a "game mode" helps). The LISTEN card shows the live figures.

On the old skipback-ring path, skipback hands over new audio only every ~43 ms: measured at the renderer's frames, that audio was **19 ms older in the median, 41 ms at p90 and up to 67 ms** older than the tap's (2026-09-30). That path is still there as the fallback and as the "skipback ring" SOURCE.

### Shell and cost

- **Shell:** `sudo ~/orc/orc1/daw/visuals.sh start|stop`; `visuals.sh next`, `set orc [secs]` (cycle), `set tunnel|mystify|tube|harmonograph|warp|hypercube|scope|ridgeline|rose|lorenz|chladni|knot|polytope|hopf|shape4d` (hold one), `set classic [secs]`, `set <XScreenSaver name>`, `fps 30|45|60`, `source <left port> [right port]` / `source ring` (what it listens to), `shot` (save the frame on the monitor to `/tmp/orc1_visuals_shot.png`: the renderer writes it itself, because an X screen grab of the GL window is stale), `list`, `status`.
- **Cost (measured):** ~10-18% of one core at 30 fps, including the listening (~1 ms a frame); ~110 MB. It runs at the Pi's lowest CPU priority (`SCHED_IDLE`) and is memory-capped (200M / 300M) so it can never crowd skipback. It writes its frame timing to `/tmp/orc1_visuals_fps.log` every 30 s: fps, late frames, worst gap, `vsync N` (locked to every N-th refresh), and the frame's cost (`cpu` = the frame's own work, `swap` = waiting for the screen). The listener `orc1-vistap` costs 0.1-0.4% of one core and ~2 MB of its own memory; it starts and stops with the visuals, builds itself from `orc1/daw/orc_jacktap.c` on its first start (needs `gcc` and `libjack-jackd2-dev`), and rejoins by itself within ~5 s if JACK restarts.

---

## HUD mode (live play-state on the HDMI monitor)

A status screen for the instrument, full-screen on the HDMI monitor: what is playing *right now*, readable from across the room. It is the fourth monitor kiosk (`systemd/orc1-hud.service` → `orc1/daw/hud_session.sh` → `orc1/daw/orc_hud.py`). Start and stop it from the **HUD card on `/system`**, or `sudo ~/orc/orc1/daw/hud.sh start|stop|shot|status`. VISUALS, DAW and DJ mode share the monitor: starting any of them replaces the HUD, and starting the HUD replaces them.

What it shows, top to bottom:

- **Tempo and clock** (BPM, INT / EXT / DRIFT), the **transport** (PLAY / STOP / REC), the position as **BAR / BEAT / STEP**, four **beat lamps** that flash on each quarter note, and the **chord** with the key, scale, preset and octave.
- **Every sequencer track** as a row of steps with a playhead. A muted track is dimmed; a track with its own length wraps on its own (polymeter).
- **The modules**, in MIDI-channel order, each lighting up when it plays a note (brighter for a harder hit, fading over about a quarter of a second). Only modules bound on `/matrix` appear, and they follow a rebind on their own.
- **Held notes**, the **arp** (pattern and rate) and **BeatBuddy** (play/stop and part).
- If the brain is not running the screen says **NO SIGNAL FROM THE ORC BRAIN**; if it stops updating, the last picture is dimmed with a **BRAIN NOT UPDATING** banner. It never shows a frozen frame as if it were live.

It is deliberately light: no browser (one would cost too much of the Pi's memory), it runs at the lowest CPU priority, and it redraws only what changed. Measured: about 0.1% of a core when nothing moves, about 14% while the sequencer plays, with skipback unaffected. `hud.sh shot` saves what it is showing to `/tmp/orc1_hud_shot.png`.

**On your phone: `/hud`** (HUD in the bottom bar, or "OPEN PHONE HUD" on `/system`). The same screen, drawn live in the browser: tempo, transport, BAR/BEAT/STEP with the beat lamps, the chord, every track with its playhead, a light per module, held notes, arp and BeatBuddy. It needs no monitor and works alongside the kiosk. A LIVE / BRAIN NOT UPDATING bar at the top tells you whether the data is current (the picture dims when it is not). A hidden tab (phone locked) drops its connection and reconnects when you come back, so it does not drain the battery; up to 4 screens can hold the live stream, further ones fall back to polling by themselves.

The feed behind it is `/dev/shm/orc1_playstate.json`, written by the brain whenever something changes. Anything else can read it too: `GET /api/hud` (the JSON) and `GET /api/hud/stream` (live events). Design and measurements: [ARCHITECTURE](ARCHITECTURE.md#the-live-play-state-feed-and-the-hud).

---

## SmartPad (Midiplus SmartPad running KIKPAD firmware)

A Midiplus SmartPad reflashed with custom firmware (the open KIKPAD firmware, customised; the firmware source is **not** in this repo). Five banks (MODE1 cycles them): 0 = MPC1000 face controls, 1 = bass layout in 4ths, **2 = the ORC surface** (the sampler, skipback and VISUALS played from the pad, painted by the ORC: see [Bank 2](#bank-2-the-orc-surface-sampler-skipback-visuals) below), 3 = fader bank, and 4 = a **standalone step sequencer** that keeps its patterns in the pad's own flash: 8 patterns A-H of 8 rows x up to 64 steps, drum or melodic (rows = scale degrees), accents, per-step ratchets and probability, per-track loop lengths (polymeter), a one-track 64-step view, motion recording (the 8 encoders record CC moves per step) and song chaining (16 slots x 1-8 bars). **The full control reference is the pad's `USER_GUIDE.md`**; the short version:

| | |
|---|---|
| Views (bottom row) | VOLUME = STEP, SEND A = ACCENT > RATCHET > PROB, SEND B = MUTE > SOLO, PAN = MOTION > LANES, CONTROL1 = LENGTH > NOTES, CONTROL2 = SONG. **Tap** = go there, **hold** = peek (back when you let go), **tap the active one again** = its sibling (LED steady / slow blink / double blink) |
| Always live | MS1-8 = pattern (queued to the bar end while playing), CLIP = play/stop, LEFT/RIGHT = steps 1-8 / 9-16, UP/DOWN = octave (melodic), encoder 1 = tempo, encoder 2 = swing, **SET** + MS = copy a pattern, SET + CLIP = clear it |
| SETUP page (MODE2) | piano root viewer, the ORC's 25 scales, octave, MIDI channel 1-16, pattern TYPE (drum / melodic) and scale SOURCE (LOCAL / HOST), all in one page |
| 64-step track | hold VOLUME + tap a pad: that track opens as 64 steps across all the pads; LEFT/RIGHT = previous / next track; tap VOLUME = back |

**How it reaches the ORC.** The pad shows up as **two** USB-MIDI ports. Port `:0` (`KIKPAD:KIKPAD IN <n>:0`) carries the music banks: the brain hot-plugs it like any other controller (`hot-plug [usb engine]` in the journal). Port `:1` is bank 2's private wire, claimed by name as the ORC surface (`hot-plug [kikpad    ]`, see below). Each pattern has its own MIDI channel (set on the SETUP page), so a pattern on the **drums** channel (10 by default) plays the 909 drums and a melodic pattern on the poly / mono / chip channel plays those voices, whatever `/matrix` binds to them. Motion lanes send CCs on the same channel. The pad has no DIN output and makes no sound itself; it needs the Pi (or any USB host) to be heard. If the host sends MIDI Start / Stop / Clock the sequencer follows it, otherwise it runs on its own tempo. The ORC's MIDI clock output goes to `CLOCK_OUT_PORTS` (the pisound DIN by default, and off until toggled), so the pad is not a clock destination unless you add it there.

**The ORC's key on the pad (`orc1-kikpad`).** A small service (`orc1/orc1_kikpad_link.py`, `systemd/orc1-kikpad.service`) reads the live root and scale from the web process (`GET /state`, every 3 s) and sends them to the pad on its private host port `:1` (any channel; CC 10 = root 0-11, CC 11 = scale index). Until 2026-10-14 this used MIDI channel 16 on the music port. With the pad's scale SOURCE set to **HOST**, its melodic patterns play in whatever key the ORC is in (the SOURCE pad is orange until the first message arrives). The scale index is a position in the ORC's own `SCALE_ORDER`, and the pad's table is a copy of it, so the two must match (the pad repo has `tools/check_scale_table.py`; the link logs a warning if the ORC's list ever differs). The service sends on change and every ~10 s, runs at the lowest CPU priority (~0.4 % of a core), never opens the pad as an input, and reopens the pad's port by itself whenever the pad reboots or is replugged (an old handle to a rebooted pad drops messages silently). `journalctl -u orc1-kikpad` shows `[kikpad-link]` lines.

**Reflashing the pad from the Pi.** No button combo needed: run `~/tools/kik_reboot.py new` (it sends CC 127 = 90 on the pad's host port `:1`; the `old` argument is for firmware from before 2026-10-14), then **always** follow it with `~/tools/kikpad_flash.sh <firmware.bin>` (a retry loop around `tkg-flash`, built from the bootloader project's CLI source into `~/tools/`, outside the repo; it needs `libudev-dev`). Entered this way the bootloader waits until it is flashed. Build the `.bin` on the PC with the exact command in the pad repo's `CUSTOM.md` (the wrong upload method builds a dead pad). The bootloader runs first on every power-up, so a bad flash can be recovered with the same loop.

**Remapping banks 0 and 1 and the encoders (`/kikpad`).** The maps for bank 0, bank 1 and the encoders are stored in the pad's flash, so they work with no Pi (for example when the pad drives an MPC). The `/kikpad` page (CONTROL group) is only the editor: the brain (`orc1_kikpad_map.py`) sends the pad a SysEx on port `:1`, and the page shows what the pad *replied*, never a copy of its own. Each pad can be off, a note, a CC or a toggle, on its own channel or the bank's, with a colour; each encoder has eight modes of CC + channel.

### Bank 2: the ORC surface (sampler, skipback, visuals)

Bank 2 decides nothing on the pad. Its presses, encoder turns and buttons go to the Pi on the pad's second port, and **the ORC paints the bank back**, so the lights show the ORC's real state. The brain recognises that port **by name**, so no MIDI channel or CC is used up, and the pages live entirely on the Pi: a new page needs no reflash. **LEFT = SAMPLER page, RIGHT = VISUALS page** (the lit one is showing). SET is a shift on both.

**SAMPLER page.** Deck A = rows 1-3, deck B = rows 4-6:

| Row (per deck) | Pads |
|---|---|
| 1st | GRAB the last 1 / 2 / 4 / 8 bars, GRAB the NEXT 1 / 2 / 4 bars, HOLD (grabs while held, snapped to bars) |
| 2nd | LOOP (play/stop), STOP, REVERSE, PLAY ONCE, CUE 1-4 (yellow = set; tap = set or jump; **SET + cue = clear**) |
| 3rd | STUTTER 1, 1/2, 1/4, 1/8 beat (hold it), JUMP to 0, 1/4, 1/2, 3/4 |
| 7 | one pad per **skipback track** (the first 8 armed, by id): green = RAM ring recording, blue = card ring recording, red = not recording. Tap = save its last N to `~/recordings` (blinks while saving, then a green or red flash) |
| 8 | N = 30 s, 1 min, 2 min, 5 min, 10 min, ALL (lit = chosen); last pad = **GRAB ALL** (every track, sample-aligned, one folder) |

MS1 / MS2 = the encoders work on deck A / deck B. Encoders: speed, gain, filter, EQ low, EQ mid, EQ high (that deck), crossfade, sampler master. LOOP/STOP/REVERSE/PLAY/JUMP light up once a deck holds audio; nothing shows whether a deck is playing (the ORC doesn't track that). The encoders keep their own positions: a control moved from the web or the Mixtrack doesn't show on them.

**VISUALS page** (controls the visuals renderer):

| Row | Pads |
|---|---|
| 1-2 | the 16 scenes (white = playing; tap = switch) |
| 3 | CYCLE scenes, then the peephole: off, circle, rounded, hexagon, diamond, triangle, star |
| 4 | background bend: wave, interlace, squeeze, two layers, wave + squeeze; last pad = FIT on/off |
| 5 | background pattern set (built-in, hero, monster 1..) |
| bottom-right | **START** visuals (blinks while starting, green = running); **SET + tap = STOP** |

Encoder banks on MS1-MS7: **MS1 motion** (speed, trail twist, trails, trail zoom, bright, beat, colour, weight), **MS2 audio** (audio amount, attack, release, sensitivity, gate, beat band low / high, background react), **MS3 background** (level, LFO, warp, frequency, speed, scale, palette cycle, pattern), **MS4 peephole + picks** (size, softness, x, y, room, fit, logo, pattern set), **MS5 camera + LFO** (size, angle, LFO rate, LFO sync, size react / LFO, rotation react / LFO), **MS6 movement** (brightness, sway, trails: react / LFO each; encoders 7-8 do nothing), **MS7 background beat band** (own-band switch, low, high, SENS BG; encoders 5-8 do nothing). The encoders start from the renderer's saved positions and follow changes made on `/visuals`. With the visuals stopped only START works (the rest is dim). Rows 6-7 are free for a pad-sized picture of the visuals later.

**If bank 2 stays dark:** `journalctl -u orc1 | grep kikpad` should show `surface on KIKPAD:KIKPAD IN <n>:1`. Leaving bank 2 and coming back makes the ORC repaint it.

---

## Pad maps (drum brains and other pads)

Any class-compliant USB-MIDI drum brain or pad controller just works when plugged in: the brain hot-plugs it within a few seconds (`hot-plug [usb engine]` in the journal), and General MIDI drum notes on channel 10 play the drums module (e.g. the **Simmons SD350**: kick 36, snare 38, toms 48/45/43, hats 46/42/44, crash 49, ride 51). **Pad maps** let you give individual pads a different job, for when the pads end up "in places that are very much not a drumset".

**On `/pads` (CONTROL group):** pick the device under *Add device* and press ADD (this makes a profile, matched by **name**; the text in *Matches* is any part of the device's MIDI name). LEARN turns on: hit a pad and its row appears. Then choose what it does:

| Choice | What happens |
|---|---|
| pass through | the pad plays what it always played (the same as not listing it) |
| ignore | the hit is dropped |
| play note | a note on a **named module** (drums, poly, chip voices, sampler ...), looked up from `/matrix` at the moment you hit, so moving modules around never sends it to the wrong place; an unbound module stays silent. Velocity = as hit, or fixed |
| send CC | (CC inputs, e.g. a hi-hat pedal) the value goes out as a CC of your choice on a named module |
| action | anything the FCB pedal can do: chords I-VII (latch / stab per the `/pedal` setting), RC-505 buttons, BeatBuddy, sequencer play/stop/record, tap tempo, panic, plus skipback **GRAB ALL**, arp, FX and CME preset |

**Pads are triggers.** Drum brains send their note-off on a timer, not when you lift the stick (the SD350: right before the next hit, or ~2.4 s after the last one), so an action fires on the **hit** and the note-off is ignored. A second hit on the same pad within 80 ms is treated as a double-trigger (measured: two note-ons 10 ms apart from one hit) and doesn't fire again, so a toggle like RC-505 rec/play can't cancel itself. Hold-style actions (the BeatBuddy footswitch, page flips) aren't offered. **Min velocity** (per device, and per action pad) stops a ghost tap firing something; a CC action fires when the value rises past its threshold.

Pads you don't list, and devices with no profile, are untouched. The map is saved by the brain in `~/.orc1_padmap.json` and applies instantly (no restart).

---

## Zoom MS-70CDR pedal (the ZOOM FX module)

Plug the pedal into the Pi's USB (it powers from the port). Bind **ZOOM FX** to a channel on `/matrix`, then send CCs to that channel from anything (a Fire knob naming `zoomfx`, a pad map, any controller):

| CC | Does |
|---|---|
| 20 / 30 / 40 / 50 / 70 / 80 | effect slot 1-6 on (64-127) / off (0-63) |
| 21-29, 31-39, 41-49, 51-59, 71-79, 81-89 | slot 1-6, knobs 1-9 (0-127 = that knob's whole range) |
| program change 0-49 | patch 1-50 |
| 90 / 91 | tuner on/off (two tuner modes) |
| 92 | store the current patch (each push) |

The pedal's own knobs keep working, and the ORC keeps track of them. Slots 4-6 only answer on a pedal with the 2-byte firmware patch (stock firmware ignores edits to them). The service is `orc1-zoomfx` (`journalctl -u orc1-zoomfx`); the live patch is in `/dev/shm/orc1_zoomfx.json`.

## Persistent state files

| File | Purpose |
|------|---------|
| `~/orc1_state.json` | Fire view state, track data, BPM, last-used settings |
| `~/fire_knobmap.json` | Encoder CC remaps |
| `~/fire_presets.json` | Pad / fader / knob preset banks (8 slots each) |
| `~/.orc1_bindings.json` | Module Matrix — which channel each voice listens on |
| `~/.orc1_protected_channels.json` | Which MIDI channels are protected from being chosen (`/matrix`); written by the web process |
| `~/.orc1_sampler_deck_src.json` | Which skipback track each sampler deck grabs from |
| `~/.orc1_config_overrides.json` | Whitelisted config overrides set on `/system` |
| `~/.orc1_module_outputs.json` | Per-module output destination (which physical pair each voice sounds out of) |
| `~/.orc1_patch.json` | Saved `/patch` audio wiring (the current audio-routing persistence) |
| `~/.orc1_skipback_tracks.json` | Skipback per-track source/config (incl. `store` ram/card, `persist`) |
| `~/.orc1_skipback_status.json` | Skipback live status (written by the recorder; read-only for everything else) |
| `~/.skipbuf/` (tmpfs) | RAM rings, every track's `ring.pos`, `heads.json` (aligned-grab snapshot). **Wiped on reboot** |
| `/mnt/skipring/` (card partition) | CARD rings + `checkpoint.json` (the positions that survive a reboot) |
| `~/.orc1_chip_params.json` | Chip voice settings, per voice (last value of each CC, incl. the performance CCs) |
| `~/.orc1_gb_wave.json` | GameBoy wave slots 1-16, the current custom wave, and the harmonic knob positions |
| `~/.orc1_visuals` | VISUALS mode choice: line 1 scene mode (`orc`, `orc:<scene>`, `classic`, or an XScreenSaver name), line 2 cycle seconds, line 3 frame rate |
| `~/.orc1_visuals_state.json` | VISUALS knob positions (every knob CC, by name, incl. the background, peephole, camera and movement) + current scene; written by the renderer, read back by `/visuals` |
| `/dev/shm/orc1_playstate.json` (tmpfs) | The live play-state feed for the HUD (transport, step, chord, tracks, held notes, per-module hit counters); written by the brain only when something changes, read by the HUD kiosk and `/api/hud`. Gone on reboot, rebuilt at once |
| `~/.orc1_vistap` | What the VISUALS listen to: line 1 = left JACK port, line 2 = right (blank = mono), or `ring` = skipback's RAM ring. No file = `system:capture_1` + `_2` |
| `~/.orc1_cme.json` | What the brain last read off the CME U6MIDI Pro (preset, names, router, filters/mappers in use); written by the brain, read by `/cme` |
| *(none)* | The SmartPad's patterns, motion, songs and settings live in **the pad's own flash**, not on the Pi; `orc1-kikpad` keeps no state |
| `~/.orc1_padmap.json` | Pad maps (`/pads`): per-device profiles and what each pad does; written by the brain |
| `/tmp/orc1_padmap_hits.json` (tmpfs) | The last 32 pad hits of mapped devices, for `/pads` LEARN; wiped on reboot |
| `~/orc/orc1/orc1_routes.json` | MIDI message routing rules (RoutingEngine) |
| `~/.fx_state.json` | FX chain running state |
| `~/.jackdrc` | JACK startup config (auto-written by `orc1_jackd_start.sh`) |

> **Defunct:** `~/orc1_routing.json` was the old Fire ROUTE-grid audio table; the Fire ROUTE mode was removed 2026-08-06 and audio patching moved to `/patch` → `~/.orc1_patch.json`. The file may still exist but is no longer the live route table. `~/orc1_airwindows_catalog.json` is a stale cache from the pre-rebuild FX system.

**Do not delete the live files.** They're safe to back up or copy; the engine regenerates `.fx_state.json` if missing.

---

## Code layout

```
~/orc/
  orc1/
    orc1.py                 -> the BRAIN: MIDI I/O, voice dispatch, clock, arp, chord engine, cmd socket
    orc1_synth.py           -> shared DSP LIBRARY (PolySynth, MonoSynth, NESEngine, drums, sampler router,
                               PeakLimiter). Imported by the hosts below -- NOT a running service (retired)
    orc1_drum_host.py       -> per-module JACK host: 909 drums        (service orc1-drum,    client ORC1-Drum)
    orc1_poly_host.py       -> per-module JACK host: poly synth        (service orc1-poly,    client ORC1-Poly)
    orc1_chip_host.py       -> per-module JACK host: NES/GB chip       (service orc1-chip,    client ORC1-Chip)
    orc1_sampler_host.py    -> per-module JACK host: sampler decks     (service orc1-sampler, client ORC1-Sampler)
    orc1_mono_host.py       -> per-module JACK host: mono/lead         (service orc1-mono,    client ORC1-Mono)
    orc1_bbkit_host.py      -> per-module JACK host: BeatBuddy kit     (service orc1-bbkit,   client ORC1-BBKit)
    orc1_fire.py            -> Akai Fire controller handler (this is the big one)
    orc1_mixtrack.py        -> Numark Mixtrack Pro 3 handler (decks, pads, LEDs, sampler)
    orc1_surface_link.py    -> paints the ORC surface on a Fire running the custom firmware
    orc1_padmap.py          -> per-device pad maps (/pads)
    orc1_pedal.py / orc1_rc505.py / orc1_beatbuddy.py -> FCB1010 pedal, RC-505 looper control, BeatBuddy
    orc1_cme.py             -> CME U6MIDI Pro patchbay (/cme)
    orc1_zoomfx.py          -> the zoomfx module: Zoom MS-70CDR over CC (service orc1-zoomfx)
    orc1_sampler.py         -> the sampler engine (decks, ring reader) used by the sampler host
    orc1_health.py          -> read-only health check (orc1-health.timer)
    orc1_kikpad.py          -> SmartPad bank 2 = the ORC surface (SAMPLER + VISUALS pages), on the pad's port :1
    orc1_kikpad_link.py     -> sends the ORC's key to the SmartPad (service orc1-kikpad)
    orc1_kikpad_map.py      -> edits the SmartPad's own maps from /kikpad
    orc1_fx_inserts.py      -> current FX: per-source wet/dry inserts manager (see FX_SYSTEM.md)
    orc1_fxmix.py           -> one insert instance (wet/dry+level DSP, audio-rate)
    orc1_fx_rack.py         -> legacy single-chain rack (still backs /fx page + Fire fx view)
    orc1_fx.py / fx_chain_manager.py -> FXManager + low-level jalv process manager
    orc1_seq.py             -> step sequencer logic
    orc1_arp.py             -> arpeggiator
    orc1_routes.py          -> MIDI message routing engine (RoutingEngine)
    orc1_routes.json        -> MIDI routing rules (RoutingEngine profiles)
    orc1_config.py          -> hardware mappings, channel assignments, module-output helpers
    orc1_patterns.py        -> pattern library
    orc1_web.py             -> web interface (Flask)
    orc1_playstate.py       -> the live play-state feed the brain publishes (/dev/shm/orc1_playstate.json) for the HUDs
    templates/hud.html + static/js/hud.js -> the phone / desktop HUD page (/hud)
    orc1_jackd_start.sh     -> JACK SERVER startup (resolves the master card: L-12 -> pisound -> fallback)
    orc1_bridge_start.sh    -> on-demand ALSA<->JACK zita bridge for a second card (orc1-bridge@)
    orc1_jackd_card_start.sh -> a named JACK server for one extra card (orc1-jackd@<card>)
    orc1_assign_module_out.sh -> moves a module host onto a card's server (or back to the default)
    fire_oled.py            -> OLED display driver for the Fire
    samples/                -> default drum samples (.wav)
    skipback/
      skipback_recorder.py  -> main skipback service (JACK client 'skipback-rec'); RAM + CARD rings
      skipback.sh           -> single-track slice/export (streams to ~/recordings)
      skipback_grab_multi.py -> GRAB ALL: several tracks, sample-aligned, one folder
    daw/                    -> the HDMI-monitor kiosks and their tools (all on-demand, none at boot)
      orc_hud.py            -> the HUD renderer (plain 2D pygame, reads the play-state feed)
      hud.sh / hud_session.sh -> start|stop|shot|status for the HUD / its X session script
      orc_visuals.py, visuals.sh, visuals_session.sh -> VISUALS (GPU line scenes) + its controller + X session
      orc_jacktap.c         -> orc1-vistap, the listen-only JACK tap the VISUALS hear audio through
      daw_mode.sh, daw_session.sh, dj_session.sh -> DAW (REAPER) and DJ (Mixxx) modes
      kiosk_clear.sh        -> frees the monitor before any kiosk starts (stops the others, waits for their X session)
      _test_hud.py, _test_kiosk_clear.py, _test_visuals.py -> their regression suites
  orc-control/
    orc1_system.py          -> system-level controls (GPIO, shutdown, etc.)
    gpio_dashboard.py       -> GPIO status utility
    gpio_test.py            -> GPIO test utility
```

---

## Patching / updating

Because ORC-1 has no screen, updates come in over SSH. The current workflow (superseding the old `plink` "pipe a patch script and `str.replace()`" method) is **edit a local mirror, then deploy whole files**:

1. **Pull before you edit** — there is no autosync, so a stale local copy will clobber newer Pi work.
2. **Back up on the Pi** into `~/backups/<label>_<date>/` (its own step, before the transfer).
3. **Upload with `scp`**, verify `stat -c%s` matches on both ends, then `python3 -m py_compile` (and `bash -n` for shell).
4. **Restart the right unit** — `orc1` for the brain/handlers (the voice hosts follow via `PartOf`), `orc1-jackd` for a card switch, `orc1-web` for web files. Not `orc1-synth` (retired).
5. **Verify the rig, not the exit code** — `systemctl is-active`, `journalctl -p err`, xruns, `jack_lsp -c`.



---

## Troubleshooting

**No audio from L-12:**
The Pi probably booted before the Zoom was powered on and fell back to Pisound.
```bash
sudo systemctl restart orc1-jackd
```
Wait ~5 seconds, then verify with `jack_lsp` that the voice-host outputs appear (`ORC1-Drum:out_L`, `ORC1-Poly:out_L`, … — **not** `ORC1-Synth`, which no longer exists).

**VISUALS / DAW / DJ mode won't start (dies within a second):**
X must start on the console that is in front. The units switch to console 7 themselves (`ExecStartPre=chvt 7`, added 2026-10-05: before that, with the tty1 auto-login in front, X got a paused graphics handle and crashed on every start). If it still fails, `~/.local/share/xorg/Xorg.0.log` says why; `sudo chvt 7` by hand and start again.

**Fire pads not responding:**
```bash
sudo systemctl restart orc1
```

**OLED blank:**
Normal when idle for >30s. Turn any knob or press any pad to wake it. If it stays blank after interaction, restart orc1.

**Skipback not recording:**
Check the source connection:
```bash
jack_lsp -c | grep skipback
```
The recorder **owns and re-reconciles** its own input every watchdog pass, so a manual `jack_connect` will just be re-asserted or overridden — change the source on the **`/skipback`** page instead. If it's recording the wrong thing, that's where to fix it.

**Service won't start / Python error:**
```bash
journalctl -fu orc1 --lines=50          # brain
journalctl -fu orc1-poly --lines=50     # or whichever voice host
```
Common causes: a syntax error from a bad deploy (run `python3 -m py_compile` before restarting), a missing dependency, or JACK not up yet (the voice hosts and skipback wait for `orc1-jackd`).
