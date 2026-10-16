# ORC-1 Architecture

A developer-facing map of how ORC-1 is put together **as it exists today**. (User-facing operation lives in `ORC-1_OWNERS_MANUAL.md`.) This describes implemented reality; planned/forward-looking work is intentionally not here.

> **Trust the code, not this file.** Per the project's prime directive: where this doc and the code on the Pi disagree, the code wins — say so and fix the doc. Two things below are easy to get wrong and are flagged inline: the audio engine is **not** one process (§ per-module hosts), and a second soundcard joins the **one** JACK graph via a bridge rather than a second server (§ audio output).

## The shape: cooperating processes

ORC-1 is not one program. It's a small system of cooperating processes, which is why it stays responsive (audio never blocks on control logic) and why each piece restarts independently.

```
  Hardware controllers            ┌──────────────────────┐
  (Akai Fire, Mixtrack Pro 3,     │      orc1.py         │   the BRAIN
   DIN/USB MIDI)  ───────────────▶│  chord engine, arp,  │
                                  │  sequencer, routing, │
   web browser ──▶ orc1_web.py ──▶│  controller handlers,│
        (Flask)   (cmd socket)    │  sampler facade      │
                                  └──────────┬───────────┘
                                             │ virtual MIDI bus "ORC1-Synth-Bus"
                                             │ (ALSA-seq fan-out: one source, many readers)
              ┌───────────────┬──────────────┼──────────────┬───────────────┐
              ▼               ▼              ▼              ▼               ▼
        ┌──────────┐    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
        │orc1-drum │    │orc1-poly │   │orc1-chip │   │orc1-      │   │orc1-mono │  per-module
        │  _host   │    │  _host   │   │  _host   │   │ sampler_h │   │  _host   │  JACK hosts
        └────┬─────┘    └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘  (+ orc1-bbkit)
             └───────────────┴───── one jackd (orc1-jackd, sole server) ──┴─────────┘
                                             │
                                             ▼
                                     audio interface  ◀─── on-demand zita bridges
                                  (card-agnostic: L-12       pull EXTRA cards into
                                   or pisound by default,     this SAME graph, via /patch
                                   + bridged cards)           (NOT separate jackd servers)

   audio in ──▶ skipback_recorder.py ──▶ ~/.skipbuf/t<id>/ring.raw (per-track circular buffers)
```

## Components

### `orc1.py` — the brain (control process)
Owns everything that decides *what should happen*:
- Opens the physical controller inputs (Fire, Mixtrack, DIN, USB), matched by port name.
- Runs the chord engine, arpeggiator, and step sequencer.
- Applies the routing rules in `orc1_routes.py` (+ `orc1_routes.json`), a generic device-adapter layer that translates each controller's quirky protocol into a common vocabulary.
- Dispatches controller messages to the per-controller handlers (`orc1_fire.py`, `orc1_mixtrack.py`).
- Forwards notes/CCs to the module hosts over a **virtual MIDI output** it creates, `ORC1-Synth-Bus`. This is an ALSA-seq source: many client processes can subscribe to it at once and all receive the stream, which is what makes the per-module split (below) cost the brain nothing.
- Runs a Unix-socket command server (`~/.orc1_cmd.sock`) that the web process sends JSON commands to (`_execute_cmd`).
- Exposes the **sampler facade** (`sampler_capture/gate/scrub/speed/gain/crossfade`) — the single, controller-agnostic API any front-end calls; each call emits MIDI on `CH_SAMPLER` (channel 7) to the sampler host.
- Owns the **Module Matrix binding layer** (`_bindings`, `~/.orc1_bindings.json`): each voice ("module") listens on a channel that can be reassigned live from the `/matrix` page rather than being fixed. `cfg.SYNTH["CH_*"]` stays the in-process source of truth (both the module's listen dispatch AND the brain's chord/arp OUTPUT read it, so they can never diverge); a bind mutates it live and tells the host via a `CH_CTRL` arm/sentinel-note handshake, with the map persisted to seed both processes at boot. `MODULE_JOIN` couples chip pulse 2 onto pulse 1 (ephemeral) by re-emitting the engine's own CC72/73/76 on pulse 1's channel.

### The instrument: per-module JACK host processes (the 2026-09 audio-host split)
**There is no single audio process.** `orc1_synth.py` is a **shared DSP library** (not a running service): the hosts import it for `PolySynth`, `MonoSynth`, the NES/Game-Boy `NESEngine`, the 909 drum voices, `SampleVoice`/`SamplerManager`, and `PeakLimiter`. Each module runs in **its own JACK host process** (the audio-host split, 2026-09-02):

| Service | Script | JACK client | Renders |
|---|---|---|---|
| `orc1-drum` | `orc1_drum_host.py` | `ORC1-Drum` | 909 drum voices |
| `orc1-poly` | `orc1_poly_host.py` | `ORC1-Poly` | polyphonic tone engine |
| `orc1-chip` | `orc1_chip_host.py` | `ORC1-Chip` | NES/Game-Boy chip voices (stereo; see below) |
| `orc1-sampler` | `orc1_sampler_host.py` | `ORC1-Sampler` | live-remix sampler decks |
| `orc1-mono` | `orc1_mono_host.py` | `ORC1-Mono` | mono/lead engine |
| `orc1-bbkit` | `orc1_bbkit_host.py` | `ORC1-BBKit` | BeatBuddy-style kit |

Each host subscribes to the brain's `ORC1-Synth-Bus` MIDI output (ALSA-seq fans one source out to many readers, so every host hears the brain for free — no per-module routing in the brain), renders **only its own module**, applies its **own `PeakLimiter`**, and publishes its own `out_L`/`out_R`. `orc1-synth.service` is **retired** (disabled + inactive); `orc1_synth.py` is only ever imported now, never run as a service.

**The chip engine (`NESEngine`, hosted by `orc1-chip`).** Monophonic voices, each on its own module channel: `pulse1`, `pulse2`, `triangle`, `noise`, `noise2` (a second independent noise voice, module `chip_noise2`, added 2026-09-24), the Game Boy `wave` (`GameboyWaveVoice`) and one-shot `pcm`. Every voice carries a `_ChipMod` for per-voice performance CCs (glide, pan, tracker-style arp/trill, pitch slide, tremolo, retrigger); arp steps and retrigger strikes are sample-accurate because the voice is rendered in segments split at each step. The host calls `render_stereo()`: while every voice is centred it is exactly the old mono mix (plus the P1/P2 spread), and once a voice is panned it mixes L/R with a second filter instance kept in step with the first. The wave voice builds its 32×4-bit table from eight harmonic levels and keeps 16 slots (Program Change recall/store). The CC numbers are in the owner's manual ("Chip voices"). Chip settings persist per voice in `~/.orc1_chip_params.json` (last value of each CC); the wave's table state lives separately in `~/.orc1_gb_wave.json`, because replaying single CCs cannot rebuild a table.

### `orc1_web.py` — the web control surface
A separate Flask process. It never touches the engine directly — it sends JSON lines to `orc1.py` over `~/.orc1_cmd.sock` (`write_cmd` → `_send_cmd_socket`). Serves the main page plus the per-subsystem pages (`/synth`, `/chip`, `/drums`, `/seq`, `/sampler`, `/fx`, `/patch`, `/matrix`, `/mpc`, `/control`, `/system`, …). Listens on port 5000. Because it must stay light, it imports `orc1_config` (numpy-free) for shared pure helpers rather than the heavy DSP modules.

### `orc1_sampler.py` — the live-remix sampler (NumPy-only)
Self-contained so it can run standalone (`python3 orc1_sampler.py --selftest N`) to prove the ring tap. `RingBufferReader` takes a read-only snapshot of a skipback ring (same wrap math as `skipback.sh`); `SampleDeck` plays a captured buffer with variable-rate interpolation + click-fades; `SamplerManager` owns the decks + crossfade. It runs inside `orc1-sampler` (`orc1_sampler_host.py`) now, not inside a monolithic synth. **skipback is never modified** — the sampler only reads its rings, and each deck can read a different track's ring (deck A defaults to t0, deck B to t1).

### `skipback/` — retroactive audio capture (independent)
`skipback_recorder.py` is a JACK client (client name `skipback-rec`) that records one or more configurable tracks straight from the JACK graph into per-track circular buffers at `~/.skipbuf/t<id>/ring.raw`; `skipback.sh N` slices the last N seconds to a WAV (or MP3 on request) in `~/recordings`. Track config lives in `~/.orc1_skipback_tracks.json` and the recorder reconciles its own input wiring every watchdog pass (by design — so `/patch` cannot steal skipback's source; move it from the `/skipback` picker instead). Runs as its own systemd service (`skipback-record`), deliberately isolated from the rest of the system.

**Recorder internals (2026-09-28):**
- **Three thread types.**
  - The **JACK callback** only memcpys each port's period into a per-channel `jack.RingBuffer`, then pushes one 8-byte STAMP per track (JACK frame time, nframes) *after* that period's audio. A track's ports and its stamp go in together or not at all.
  - **Two writer threads** (~42 ms passes), one for RAM tracks and one for CARD tracks, each under its own lock (since 2026-09-29). With ONE writer, an SD stall blocked the RAM drains too: measured under a 28 GB copy, the RAM tracks' 2.7 s buffers overflowed (15 gaps each) and their `ring.pos` froze, which also made sampler grabs land early. Now a card stall only delays card tracks (43.7 s cushion); re-measured under the same copy, RAM heads stayed within 50 ms with card writer passes up to 2.3 s, 0 gaps. Each writer drains exactly the stamped periods, converts float32 to S16_LE, and writes the ring. Because it drains only stamped periods, every head sits on a known JACK frame. Each drain publishes the track's head as ONE tuple (`Track.head`), and after each pass a writer publishes `<root>/heads.json` from those tuples (no cross-writer lock): every track's `pos`, head `frame`, `t` (wall time the head was true), `fill_bytes`, `aligned_bytes` (recorded since this process armed the track) and `gaps` (dropped periods), as one atomic snapshot.
  - The **watchdog** (5 s) reconciles config, wiring, guards and status.
  - **Priorities (2026-09-29):**
    - The whole process runs at SCHED_FIFO 1 (`skipback-record.service.d/75-writer-fifo.conf`), and libjack raises the callback thread to FIFO 5.
    - The callback is Python, so it must take the GIL every period. SCHED_OTHER writers preempted by ordinary CPU load while they held it made the callback miss its deadline (measured by ftrace, xruns).
    - At FIFO 1, no ordinary process can preempt a GIL holder.
- **Sample-aligned multitrack grab** (`skipback_grab_multi.py`). It cuts every chosen track at the newest frame all of them reached. A track that drained further is cut that many frames earlier. It then streams the tracks together, oldest first, into one folder of equal-length WAVs + `manifest.json`. The span is limited to `aligned_bytes`, and it keeps 2 s off the write head on a full ring. **Stereo joins** (`--joins L:R` / `--joins-file`, 2026-09-29): two mono tracks are interleaved into one stereo WAV at write time (the rings stay mono). The web (`/api/skipback/joins`, sole writer of `~/.orc1_skipback_joins.json`) and the brain's `SKIPBACK_SAVE_ALL` both pass the file; with no flag nothing is joined, so tests never read the live file.
- **Single-track grab** (`skipback.sh`) streams the span in 4 MB chunks straight into the WAV in `~/recordings`, with periodic `fdatasync` + page-cache drop. Nothing goes to `/tmp` (tmpfs), and there's no full-span read into RAM.
- **Storage per track: `store: "ram" | "card"`.**
  - RAM rings live in tmpfs `~/.skipbuf/t<id>/`.
  - CARD rings live on the `skipring` partition (`/mnt/skipring`, fstab `nofail`) and are `posix_fallocate`d to full size. `ring.pos` stays in tmpfs, and a tmpfs `ring.raw` symlink points at the card file, so every reader is unchanged.
  - A checkpoint thread (1 s) snapshots the heads under the writer lock, then fdatasyncs each card ring *outside* the lock, and only then rewrites one combined `/mnt/skipring/checkpoint.json` (tmp + fsync + rename).
  - Resume order on arm: tmpfs `ring.pos` (a restart within the same boot, exact), then the card checkpoint (after a reboot, when `persist`, or when it's the same boot), then fresh.
  - CARD channels get an 8 MiB (~43.7 s) RT ringbuffer; RAM channels 2 MiB (~10.9 s; it was 512 KiB = 2.7 s until 2026-09-29, when a RAM writer pass hit 2.6 s under memory pressure). The recorder also has a memory reservation (`skipback-record.service.d/70-memory-protect.conf`: MemoryMin 256M, MemoryLow 1G), which needs the memory cgroup (`cgroup_enable=memory`, see INSTALL). That covers the multi-second SD stalls measured on the reference card (up to 10.6 s checkpoint, 5.25 s writer pass, with 0 loss over a 65-min 14-track soak).
- **Guards.**
  - RAM tracks share the 900 MB RAM budget. The free-RAM floor only warns, and never stops a ring (SKIPBACK COMES FIRST).
  - CARD tracks are budgeted against the card partition's size, and refuse to arm while it isn't a mountpoint.
  - Switching RAM↔CARD is a structural change: the ring is rebuilt and the old one deleted.
- Tests: `_test_skipback_grab.py`, `_test_skipback_align.py`, `_test_skipback_card.py`, `_test_skipback_ramguard.py`.

> Superseded 2026-07-19: the older single-track pipeline (`zoom_skipback_bridge.sh` → ALSA Loopback → `skipback_daemon.sh` → `~/.skipbuf/ring.raw`) and its `skipback-bridge`/`zoom-skipback` services are retired. Compatibility symlinks remain at the old ring paths.

### The HDMI monitor: DAW, DJ, VISUALS and HUD modes

The ORC is headless; a monitor on the Pi's HDMI port is used on demand by four **kiosk units that share VT7 / display `:0`** (each `Conflicts=` the others, no `[Install]` = never at boot). Each is a `PAMName=login` session running `xinit` with a session script in `orc1/daw/`:

- **`orc1-daw`** (REAPER) and **`orc1-dj`** (Mixxx + the Mixtrack handover): both are JACK clients. `daw/daw_mode.sh` stops the six module hosts one at a time first (engines-off, the default) and restores them however the app ends.
- **`orc1-hud`** (`hud_session.sh` → `daw/orc_hud.py`, plain 2D pygame, **no browser**: Chromium costs too much RAM on this Pi): the live play-state screen, see [the play-state feed and the HUD](#the-live-play-state-feed-and-the-hud). Not a JACK client; it only reads a tmpfs file.
- **`orc1-visuals`** (`visuals_session.sh` → `daw/orc_visuals.py`, a pygame + moderngl GPU renderer; XScreenSaver modules as "classic"). **The renderer itself is deliberately NOT a JACK client.** It hears the music read-only:
  1. It opens `ORC1-Synth-Bus` like a module host: kicks/snares on the drums/bbkit channels, held notes for colour, and CC 20-33 on the channel of the **`visuals` module** (a `MODULE_KEYS` entry with no host).
  2. **Audio from `orc1-vistap`** (2026-09-30): `daw/orc_jacktap.c`, a listen-only **C** JACK client (input ports `in_L`/`in_R` only). Its RT callback only copies each period into **`/dev/shm/orc1_vistap`**: a 64-byte header (`magic "ORCTAP1"`, rate, channels, capacity, period, `wpos` = frames written, published with release ordering AFTER the samples, `mono` = CLOCK_MONOTONIC of the newest period, `conn`, `alive`, xruns, pid) then a 16384-frame float32 stereo ring. The renderer's `TapShm` copies the newest N frames and re-reads `wpos` to drop a copy the writer lapped. **Why C:** a Python JACK callback needs the GIL every period, and the renderer holds it for numpy/GL work, so it could make the whole graph (skipback included) miss a deadline; the C callback takes no locks, allocates nothing and never touches Python. Its main thread re-reads `~/.orc1_vistap` every 0.5 s and owns exactly its own two wires (re-connecting a source that comes back after a host restart). It exits 1 when JACK goes away and `Restart=on-failure` rejoins (measured ~5 s). The unit is `Wants=` by `orc1-visuals` and `PartOf=` it; `ExecStart` runs `daw/jacktap_build.sh`, which compiles the binary to `~/.cache/orc1/` when the source is newer (gcc + `libjack-jackd2-dev`), then `exec`s it so SIGTERM reaches it and it closes its client cleanly (a SIGKILLed JACK client can xrun the graph).
  3. **Fallback:** when the tap is absent, stale (> 0.5 s), stopped or unconnected (or `~/.orc1_vistap` says `ring`), the newest audio of skipback's RAM master ring (`~/.skipbuf/t<id>/ring.raw` at `ring.pos`, the way the sampler grabs). `AudioTap.source` = `tap` | `ring`.

  It publishes what it hears (incl. `source`, `tap_ports`, `age_ms`) to `/tmp/orc1_visuals_audio.json` for the `/visuals` spectrum; `/api/visuals/source` lists every JACK output port and writes `~/.orc1_vistap`. It runs `SCHED_IDLE`. **Frame pacing:** swaps are locked to the monitor refresh (`SDL_GL_SetSwapInterval` through the system libSDL2 that Debian's pygame links) when the fps divides it (60 → 1, 30 → 2; otherwise sleep pacing), and motion advances in whole refreshes (`frame_dt`): unsynced it ran ~62 fps on a 60 Hz monitor and doubled/skipped a frame every ~0.5 s. Scene changes arrive as SIGUSR2 (re-read `~/.orc1_visuals`) and SIGUSR1 (next scene), so it never restarts to switch.

  **Frame pipeline** (2026-09-30): (1) `FADE_FS` fades + zooms + spins last frame's buffer about `ctr` (the peephole centre, else the screen centre); (2) the scene's `Lines` are optionally FIT (`Fit.plan`: points scaled about the screen centre to the mask's inscribed radius `MASK_INRADIUS`, moved to its centre, BEFORE the glow quads are built so width is kept; the extent tracks up instantly and eases down over 3 s) and drawn additively; (3) the **`Composer`** (2026-09-30 later: split for speed, MEASURED on V3D at 1280x720: a per-pixel star mask cost 8 ms a frame and the full-res background 5.5 ms, so tube+bg+star ran 46 fps; now 60): `MASK_FS` draws the peephole into an R8 texture only when its settings change, `BG_FS` renders the background at 1/`BG_DIV` (2) resolution into an RGB texture only when it shows, and `SHOW_FS` composites (lines + bg lookup, tone map, × mask lookup). The line VBO is orphaned before each write (writing the buffer the GPU still draws from stalled a test frame 13.8 → 31 ms). Tiles come from `daw/vis_patterns.py` (`pattern_sets`: built-in + `patterns/hero` + `patterns/monster` in pages of 128; `tile_png` rasterises an SVG once with rsvg-convert at a whole-number scale of its own size into `~/.cache/orc1/patterns/`; `load_tile`), loaded in the pattern thread (≤16 file tiles cached, flipped to texture orientation); the web serves `/api/visuals/patterns` + `/api/visuals/pattern/<hero|monster>/<slug>.png` for the gallery. What used to be one `SHOW_FS` pass did: the **background** (a grey seamless tile from `make_pattern`, built once for all patterns in a thread at start, read per ~H/224 "scanline" row at `A·sin(F·row + S·t)`: wave / interlace / squeeze / two layers / wave+squeeze; hsv palette from the chord hue, black gaps stay black; scaled by `Presence` = rest level + react × an envelope driven by kick / note / audio beat / loudness + optional LFO; lines punch through), tone-maps, then multiplies by the **peephole** (signed-distance shapes: circle, rounded rect, hexagon, diamond, triangle, star; soft edge) so outside is pure black. Knobs: CC 34-49 on the visuals channel. `_test_visuals.py` compiles and renders the real shaders off-screen on the Pi's GPU (EGL) to check black-at-rest, black gaps, mask corners, each shape's inscribed radius and the trails pivot.

**All three units run `ExecStartPre=-+/usr/bin/chvt 7` (2026-10-05).** Xorg must start on the active console: on an inactive VT logind hands it a *paused* DRM fd, it logs "no primary bus or device found", falls back to fbdev + a modesetting GPU screen and aborts (signal 6). With the tty1 auto-login in front, every start died in 0.5 s until this was added.

`PAMName=login` moves the app into a logind session scope, so each unit's `MemoryHigh`/`MemoryMax` is copied onto that scope by `daw_mode.sh cap-session`, run from the tty-less oneshot `orc1-kiosk-cap@<kiosk>.service` (each kiosk `Wants=` it); otherwise the cap would bind nothing. It must NOT be a kiosk `ExecStartPost`: every Exec line of a kiosk inherits `StandardInput=tty` + `TTYVHangup=yes`, and with `Type=simple` a StartPost forks beside xinit and its `vhangup()` of tty7 can SIGHUP xinit before it starts X (a clean exit to systemd: the unit just ends, monitor dark; kernel-traced 2026-10-12).

**Handing the monitor over (2026-10-08): `daw/kiosk_clear.sh`.** `Conflicts=` alone is a race. The new unit's `xinit` starts in the same second the old one's login session is still closing, joins that dying session and is taken down with it (measured: HUD → VISUALS left nothing running, with systemd logging "Deactivated successfully" and no session scope). So `hud.sh start`, `visuals.sh start` and `daw_mode.sh start` first call `kiosk_clear.sh`, which stops the other kiosks explicitly and waits (≤ 10 s, then carries on with a note) until Xorg **and** every `TTY=tty7` session in `/run/systemd/sessions` is gone. ⚠ It reads **regular files only**: logind keeps `<id>.ref` named pipes in that directory, and `grep` over one blocks forever (the first version hung `hud.sh start` for good). Proven live: HUD ↔ VISUALS in both directions, twice. A DAW/DJ start goes through the same helper but was not exercised live, since that launches REAPER/Mixxx. A normal STOP leaves the unit `failed` (`xinit` exits 1 when its X server is stopped); that is how all four kiosks end, and health ignores it.

### The live play-state feed and the HUD

**What it is for.** `~/orc1_state.json` is a settings snapshot written after a button press, so it says what was last *set*, never what is *playing*. A screen that shows the instrument live needs a different feed.

**The feed (`orc1_playstate.py`, stdlib).** The brain runs one daemon thread that builds a small dict about 30 times a second (`orc1._playstate_snapshot`) and writes it **atomically to tmpfs, `/dev/shm/orc1_playstate.json`, only when something changed** (plus a 1 s heartbeat, so a dead brain shows up as a growing age rather than a frozen-looking frame). Idle cost: one heartbeat write a second, 0% CPU; the worst case measured at ~0.6 ms per tick (under 2% of a core at 30 Hz). Contents: `transport` (running, bpm, clock mode, the global 16th-note `step`, `step_ms`, `age_ms` = how old that step was at publish time, rec), `chord` (name, key, scale, degree, preset, octave), `arp`, `held` (note names), `tracks` (name, length, playhead `pos`, mute, the steps as a 0/1 string), `bindings` (module → channel), `mods` (per bound module: a **hit counter** and the last velocity) and `bb` (BeatBuddy).

- **Read-only on the hot paths.** The snapshot only *reads* attributes the engine already keeps (`seq._global_step`, the arp's bpm, `state["held_notes"]`...). The one addition on a live path is `ActivityTracker.hit()`, two dict stores per synth-bus note-on, called from **both** synth-bus choke points: `send_to("synth", ...)` **and** `send_synth_midi` (which writes straight to the bus; hooking only the first one hid every web, Fire and module note, found live; a source test guards both).
- **Ages and counters, not timestamps.** `age_ms` is excluded from change detection (it changes on every call), and activity is a counter, so a hit shows up as a change and the page needs no clock sync: it extrapolates the playhead from the step counter and its own receive time, and pulses a module when the counter goes up.
- **Readers.** `GET /api/hud` (the JSON, plus `ok` and `age`) and `GET /api/hud/stream` (Server-Sent Events, one frame per change, a keepalive every 5 s, at most 4 streams) in `orc1_web.py`. They read the file and never the brain, so a slow or stuck browser cannot slow the instrument.
- **The phone / desktop page (`/hud`, `templates/hud.html` + `static/js/hud.js`).** An `EventSource` on the stream; the same model as the kiosk (`barPos`, `beatFlash`, `Pulses`: a module lights when its hit counter changes and fades with a ~0.22 s time constant). **Staleness is judged by the page's own receive time**, never by comparing the feed's `t` with the phone's clock (the two clocks differ): no frame for 3 s = "BRAIN NOT UPDATING" and a dimmed body (the brain heartbeats every second, so a live brain can never trip it). If the stream is refused (the 4-stream cap, HTTP 429, which `EventSource` treats as final) it polls `/api/hud` and retries the stream every 15 s; a hidden tab closes the stream. Everything from the feed is drawn with `textContent` or canvas `fillText`, never `innerHTML` (track and chord names are user data; a test guards it). The animation loop runs only while the transport is running or a module light is fading. Verified live at 375 px: the real sequencer and notes drove the transport, beat lamps and module lights; the stale and 429-fallback paths; 0 console errors.

**The HUD kiosk (`daw/orc_hud.py`).** Plain 2D pygame software drawing on the HDMI monitor, a VT7 kiosk like the others (`systemd/orc1-hud.service`, `daw/hud_session.sh`, controlled by `daw/hud.sh start|stop|shot|status` and the HUD card on `/system` via `/api/hud_kiosk`). The model code (`Feed`, `Pulses`, `bar_pos`, `beat_flash`) has no pygame in it and is tested without a display; drawing is split into four regions (header, tracks, modules, footer).

- **Cheap on purpose.** `SCHED_IDLE` (`chrt --idle`), `SDL_AUDIODRIVER=dummy` and `pygame.display.init()` only (`pygame.init()` would open the Pi's headphone card and underrun it every ~30 ms, the 2026-09-29 xrun hunt). Memory cap 120M/180M, measured ~104 MB for the whole session including Xorg.
- **Repaint only what changed.** The first version redrew and flipped the whole frame every tick: 33% of a core while playing (draw 6 ms + flip 6 ms, the X flip costing as much as the drawing). Now the static track grid is cached on a surface and only the playhead cells are drawn over it; each region is repainted, and flipped with `pygame.display.update(rects)`, only when its inputs changed; fades (the beat lamps, the module lights) repaint at most 15 times a second while new *data* (a step, a chord) is drawn the moment it arrives; and the brain's heartbeat does not count as a change. Measured: playing **13.6%** of a core, idle **0.1%**, skipback 0 xruns / 0 gaps throughout.
- **Honest about a dead feed.** No file = "NO SIGNAL FROM THE ORC BRAIN"; a file older than 2.5 s = the last frame dimmed with a "BRAIN NOT UPDATING" banner.
- **Layout scales** with the screen height (designed at 1280×720; with no monitor attached the Pi falls back to 1024×768, so text is fitted to its slot rather than assumed to fit).
- `hud.sh shot` (SIGUSR1) saves the frame the renderer is showing to `/tmp/orc1_hud_shot.png` (an X screen grab can be stale for a GL/SDL window).
- Tests: `_test_playstate.py` (36), `daw/_test_hud.py` (64, real pixel renders to an in-memory surface; Pi only, needs pygame + numpy) and `daw/_test_kiosk_clear.py` (15); every fix mutation-tested.

### Controller handlers
`orc1_fire.py` and `orc1_mixtrack.py` translate a specific controller's input into engine actions **and** drive its LEDs via their own MIDI output (the Fire's pads/OLED; the Mixtrack's RGB button/pad LEDs). Control *assignments* are data in `orc1_config.py` (`MIXTRACK_*` dicts, Fire maps); hardware constants (note/CC numbers) live in the handler.

### The SmartPad: bank-2 surface (`orc1_kikpad.py`) and key link (`orc1-kikpad`)
The Midiplus SmartPad (custom KIKPAD firmware, repo `jamisonx-dev/smartpad-kikpad`) is a self-contained USB-MIDI controller: its sequencer, patterns and storage all live in the pad. It enumerates **two** USB-MIDI cables = two ALSA ports. Port `:0` carries the music banks and is an ordinary hot-plugged USB engine input.

**Port `:1` = the host surface (2026-10-05).** Bank 2 decides nothing in firmware: pads (note 0-63), buttons (note 64-87, MS1 = 64; MODE1 is never sent), encoders (CC 0-7, 65 / 63 = one click right / left) and "bank shown / left" (CC 100 = 1 / 0) go out on **USB cable 1 only**, and the host paints it back on the same cable (note 0-63 velocity = 6-bit rgbRGB colour, note 64-87 = button LEDs, CC 1 / 2 / 4 = clear / fill / buttons off). The firmware buffers the host's picture, so the host can paint while another bank shows; entering bank 2 shows it. The brain claims the port **by name** (`orc1_kikpad.is_surface_port`: prefix `kikpad`, ALSA port number 1) in `open_ports` and in the hot-plug scan **before** the generic USB path, and feeds it to `KikpadSurface.handle()`, never `_route_message`. So no MIDI channel, CC or SysEx is reserved anywhere, and its presses can't play a module or reach the DIN. `_kikpad_open` (re)attaches the paint output on every plug (a replugged pad reuses the name with a new ALSA port, and a stale handle drops silently); an unplug detaches it.

`KikpadSurface` (pure logic; engine + output injected) has two pages, LEFT = SAMPLER, RIGHT = VISUALS. Every action is a **named brain call**, which resolves the module's live `/matrix` binding itself: `sampler_capture_bars`, `sampler_capture_fwd_fixed` / `_hold_start` / `_hold_end`, `sampler_cue` / `_cue_clear`, `sampler_beat_repeat_on` / `_off`, `sampler_seek`, `sampler_speed` / `_gain` / `_filter` / `_eq`, `sampler_crossfade`, `sampler_master_gain`, `skipback_save`, `skipback_save_all`, plus two added for it: **`module_cc(module, cc, value)`** (a CC to whatever channel the module is bound to; unbound = nothing sent) and **`visuals_power(on)`** (`daw/visuals.sh start|stop` on a thread; start refuses while DAW/DJ holds the screen). The VISUALS page sends the renderer's own parameter numbers (`VIS_KNOBS` / `VIS_SCENE_CC` / `VIS_SCENES`, checked against `daw/orc_visuals.py` by the suite). LEDs show only state the Pi can read, polled at 4 Hz under one lock and painted as diffs: the decks' view files (loaded, cues), the skipback tracks file + `heads.json` (a head older than 5 s RAM / 20 s CARD = not recording), the visuals state file (knobs, scene; a knob turned on the pad is kept 3 s, because the renderer saves at most every 2 s) and `/tmp/orc1_visuals_audio.json` freshness (< 2 s = running). Leaving bank 2 mid-press releases held gestures (stutter, hold-grab). **Paint pacing (measured):** the pad took paced 64-pad frames up to 240 fps with 0 overruns, but the ALSA sequencer **drops** (it doesn't block) once the pad's 4 KB rawmidi buffer fills (`seq_midi: MIDI output buffer overrun` in dmesg; an unpaced burst lost messages), so anything that animates the pad must pace frames and send diffs. Tests: `_test_kikpad_surface.py`, `_test_hotplug.py` (the port claim).

**The key link.** The other thing the pad wants from the ORC is the **key**: `orc1/orc1_kikpad_link.py` (unit `orc1-kikpad`, `SCHED_IDLE`, `Nice=10`, `MemoryMax=64M`) polls the web process's `GET /state` every 3 s and writes `root` + `scale` to the pad as ch16 CC10 / CC11 (scale = index into `SCALE_ORDER`, which the firmware's table copies; the link warns if the live list differs). It was made a separate process on purpose: no brain change, no audio-side risk, and it never opens the pad as an input. Two traps shaped it: the pad's ALSA port is destroyed and recreated on every USB reboot, often with the same client number, and a stale `mido` output handle then drops messages **silently** (so the link drops its handle when the pad vanishes from the port list and reopens before every heartbeat); and every poll is a line in the web process's access log, hence the 3 s interval (0.5 s was ~170k journal lines a day on the SD card the skipback rings share). Tests: `_test_kikpad_link.py` (drives the real loop against a fake `mido` and clock).

### The Zoom MultiStomp: the `zoomfx` module (`orc1_zoomfx.py`, unit `orc1-zoomfx`)
A Zoom MS-70CDR (USB-MIDI only, no USB audio) driven like a module: `zoomfx` is a `MODULE_KEYS` entry with **no audio host** (like `visuals`), bound to a channel on `/matrix`. `orc1_zoomfx.py` reads that channel off the `ORC1-Synth-Bus` (ALSA fans the bus out; it re-reads `~/.orc1_bindings.json` every 2 s so a `/matrix` move applies live; unbound = the pedal is left alone) and turns plain CCs into the pedal's SysEx: slot n on/off = CC base, knob k = base + k, bases 20/30/40/50/70/80 (60-69 skipped), PC 0-49 = patch, CC 90/91 = the pedal's two tuner CCs (74/75), CC 92 = store (rising edge). A knob CC is scaled to the effect's real range from `zoomfx_params.json` (read out of the effect files; no table = raw, the pedal clamps). Edits to an empty slot are dropped (never tested on the pedal). Edits are coalesced per knob (newest wins) and spaced 4 ms. It **owns the pedal's port**: the brain lists `"zoom ms"` in `PATCHBAY_PORT_PREFIXES` and never opens it (opened as a generic USB input, the pedal's SysEx went out the pisound DIN via `_pass`). It keeps the pedal in editor mode (`50`), so the pedal reports its own knob turns (`31`) and patch changes (`28`); the live patch goes to `/dev/shm/orc1_zoomfx.json`. A replug is caught by name (the handle is dropped and reopened). Slots 4-6 need the 2-byte firmware patch on owner's pedal (stock firmware ignores edits to them). The full SysEx map, the firmware findings and the patch: `zoom ms70cdr tools/FORMAT.md` (PC-side). Tests: `_test_zoomfx.py`.

### Pad maps (`orc1_padmap.py`, the `/pads` page)
Per-device pad remapping for any generic input (a drum brain such as the Simmons SD350, a pad controller). A profile matches an input **port name** (substring), never a channel. `_route_message` asks `padmap.handle(msg, source_name)` right after the Perform-VE input-source claim and **before** the pre-stage RoutingEngine pass and all channel dispatch; a pad the profile doesn't list (and every device without a profile) falls through unchanged. Per pad (`note:N` / `cc:N`): `pass`, `ignore`, `note` (to a **named module**, resolved through `_module_channel` at hit time; unbound = silent, never ch 13; the note-off goes to the same channel/note the note-on used even if `/matrix` moved in between), `cc` (to a named module), or `action` = the pedal action vocabulary run as a press-only switch (`_padmap_action`: BeatBuddy gestures to `_bb_pedal_action`, everything else to `_pedal_switch_action(a, True, False)`), on its own thread so a chord or RC-505 tap never stalls MIDI input. **Pads are triggers**: drum brains send timer note-offs (measured on the SD350: just before the next hit, or ~2.4 s after), so actions fire on the note-on only, with an 80 ms re-arm per pad against the measured 10 ms double-trigger; CC actions fire on the rising edge through a threshold. The module is pure (no mido, no ports; the brain injects `emit` / `module_ch` / `run_action`). The BRAIN owns `~/.orc1_padmap.json` (`PADMAP_SET`), with one validator (`validate_config`) shared by the web and the brain; LEARN reads the brain's last 32 hits of profiled devices from `/tmp/orc1_padmap_hits.json`. Tests: `_test_padmap.py`.

### Config & support
`orc1_config.py` (all channels, controller maps, `SAMPLER`, `PORTS`, **module-output routing helpers** `MODULE_OUT_KEYS` / `MODULE_JACK_NAMES` / `resolve_module_out` / `load_module_out`), `orc1_arp.py` / `orc1_seq.py` / `orc1_patterns.py` (arp + sequencer), `orc1_fx.py` / `fx_chain_manager.py` (LV2 FX chains), `fire_oled.py` (Fire display).

## Audio output: one JACK graph + on-demand card bridges

> **⚠ Updated 2026-09-28: module OUTPUTS to a second card no longer use a bridge.** Since 2026-09-22 (per-card jackd, `_planning/ORC1_Per_Host_Jackd_Plan.md` phases 1 + 3):
> - Assigning poly/chip/drum/mono/sampler to another card (the CARD picker on `/patch` → `POST /api/module_card`) starts that card's **own** lazy named server, `orc1-jackd@<card>` (`orc1_jackd_card_start.sh`, at the card's native rate), and **stops any bridge on that card** ("own-jackd wins").
> - The module is rebound to the new server via a `JACK_SERVER=<card>` drop-in. The server auto-stops when its last module leaves.
> - `bbkit` is not card-assignable (48 kHz only).
> - Bridges (below) remain for **capture** and other cross-card uses.
> - Consequence: a module on another card's server is **not** in the main graph. In the reference studio its audio returns physically through the mixers into the L-12, which is how skipback records it.
>
> The rest of this section describes the default graph and the bridge mechanism. Treat its "a second card always joins through a bridge" claims as history for module outputs.

**There is exactly one JACK server.** `orc1-jackd` owns it (sole owner since 2026-07-14), binds it to one master card, and runs it at **48000 Hz**. Every module host, `skipback-rec`, and the Bluetooth router are **clients on that one server** — there are no separate jackd instances anywhere on this box.

**The master card is chosen, not fixed.** `orc1_jackd_start.sh` reads `~/.orc1_audio.json` for a chosen card, else prefers `L12`, else `pisound`, else the first real playback card (skipping Loopback/HDMI/Headphones). "Zoom L-12" is just the usual default, not a requirement. Switching cards is `POST /api/audio/device` → `restart orc1-jackd`, which cascades every synth-side client via `BindsTo`.

**A second card joins that SAME graph through a bridge, NOT a second jackd instance** — the single most important thing to get right. Assigning a module's output to another card does **not** spin up a per-device JACK server, and the design does not "avoid bridging" — it *automates* bridging. When a card is pulled into the graph:

- Templated unit **`orc1-bridge@<card>.<dir>`** → `orc1_bridge_start.sh` → **`zita-j2a`** (playback) / **`zita-a2j`** (capture). zita, deliberately, **not** `alsa_in`/`alsa_out`: a second card free-runs on its own clock (no word clock between cards here), so anything crossing to it must be async-resampled, and the `alsa_*` example tools' resampler drifts audibly (garbled sustained notes with **zero** JACK xruns, because the artifact is in the bridge, not the graph). zita's resampler holds the drift cleanly.
- Client name is `<card-lowercase>-<dir>` (e.g. `m3-out`), ports `m3-out:playback_1..N`. These register as **physical** ports in the one jackd, so they appear as ordinary destinations to `/patch` and to module-output routing.
- **Rate is read from the device, never assumed.** If the card lists 48000, zita opens it at 48000 and only clock-syncs (no resampling); otherwise it opens the card's own rate (e.g. the Mixtrack Pro 3 is **44100-only**) and zita resamples 48000↔that. Channel count is read from `/proc/asound/card<N>/stream0`. **Consequence:** a module always renders at the graph's 48 kHz and zita converts at the card, so **the sample rate you cut a WAV at is irrelevant to which card a module can reach** — it only affects playback pitch inside the 48 kHz graph.
- Two cards keep **dedicated** units instead of the generic template (`_CARD_UNITS` in `orc1_web.py`): `pisound` (`pisound-in/out`) and `PerformVE` (`performve-in/out`). The Perform-VE's is load-bearing: the device emits digital silence on capture unless a playback stream is already open, so `performve-in` waits for `performve-out`'s ports — a generic bridge would record silence.
- The template has **no `[Install]` section**: bridges are started on demand only (each costs real CPU — ~12% for an 8-channel device — and adds a clock domain to resample against), via **`POST /api/patch/cards {card, dir, op:start|stop}`** — the CARDS section of `/patch`. It verifies the card actually reached the graph against the live wiring, **not** the `systemctl` exit code (a unit can go `active` while bridging nothing).

Because every bridged card is a client in the **one** graph, audio can be freely mixed and routed **across** cards — there are no separate clock "islands" that have to be kept apart. (A genuine per-device-jackd design would give native rates with no bridge but would fragment the mix into unmixable islands; that is *not* what is built here.)

### Per-module output routing (2026-09-15)

Which physical pair a module sounds out of is persisted per module in **`~/.orc1_module_outputs.json`** and served by **`/api/module_outputs`** (GET/POST). Destinations are **card-agnostic ids**: `out-<a>-<b>` = "the a-th and b-th physical playback ports of whatever is fitted" (bridged-card ports count), `none` = wired nowhere, absent/invalid = the first pair (the historical default, so a rig with no file is byte-identical to old behaviour). `resolve_module_out(dest, phys)` lives in `orc1_config.py` (numpy-free, so the light web process and the heavy hosts share **one** resolver and can't drift). Assigning tears down the module's existing output wires, connects the resolved pair, and persists; the hosts re-apply the saved choice at their own startup. This endpoint **owns** each module's output wires — the `/patch` reconciler leaves wires it never saved alone, so the two don't fight. Keys: `drum, poly, chip, sampler, mono, bbkit`.

### Putting a module out a different card is TWO separate actions (by design)
1. **Bring the card into the graph** — `/patch` CARDS section → `POST /api/patch/cards … op:start` → starts `orc1-bridge@<card>.<dir>` (the zita bridge). Until this is done the card has **no JACK ports at all**, so there is nothing to route to.
2. **Route the module's output to it** — `/patch` module-outputs → `POST /api/module_outputs` → wires `ORC1-<Module>:out_L/R` to that card's pair.

These are two different mechanisms (`/api/patch/cards` vs `/api/module_outputs`), not one gesture. What spins up when you route a module to a fresh card is a **zita bridge into the one jackd**, not a new server.

## Data / control flow examples

- **Play a chord on the Fire:** Fire → `orc1.py` (Fire handler → chord engine → voice-leading) → `ORC1-Synth-Bus` → the relevant module host(s) render → their own JACK outs → (module-output routing) → audio interface.
- **Capture a sample from the web:** browser → `/cmd` → `orc1_web.py` → cmd socket → `orc1.py` `_execute_cmd` → `sampler_capture()` → `CH_SAMPLER` MIDI on `ORC1-Synth-Bus` → `orc1-sampler` host's `SamplerManager.capture()` reads the deck's `~/.skipbuf/t<id>/ring.raw` into a deck → plays out `ORC1-Sampler`.

- **Bluetooth music moves the visuals:** phone → `bluealsa-aplay` (routed by `bt-route`) → L-12 → the L-12 master (`system:capture_13/14` on the reference rig) → `orc1-vistap:in_L/R` → `/dev/shm/orc1_vistap` every JACK period → `orc_visuals.py` reads the newest 21 ms, finds an onset in the beat band → the GPU pulse. Without the tap, the same audio comes via `skipback-record`'s RAM ring instead (a new chunk every ~43 ms).

## Source vs. runtime (why not everything is in the repo)

The repo (`~/orc/`) contains **source**: code, the skipback scripts, default samples, docs. **Runtime/user data lives in `$HOME` by design** and is not part of the repo:

| Runtime data | Location | Why it stays out |
|---|---|---|
| Recordings | `~/recordings/` | user output, per-machine |
| Ring buffers | `~/.skipbuf/t<id>/ring.raw` | per-track working caches (hundreds of MB) |
| Engine/knob/FX/seq state | `~/orc1_state.json`, `~/fire_knobmap.json`, `~/.fx_state.json`, `~/orc1_seq_state.json` | per-user state |
| Voice patches | `~/.orc1_chip_params.json`, `~/.orc1_gb_wave.json` (and the other hosts' `~/.orc1_*_params.json`) | per-user sounds |
| Module output / matrix / patch | `~/.orc1_module_outputs.json`, `~/.orc1_bindings.json`, `~/.orc1_patch.json` | per-rig routing choices |
| Visuals choice + knobs + audio source | `~/.orc1_visuals`, `~/.orc1_visuals_state.json`, `~/.orc1_vistap` | per-user look |
| Visuals tap binary + its shared memory | `~/.cache/orc1/orc_jacktap` (built from `orc1/daw/orc_jacktap.c`), `/dev/shm/orc1_vistap` | derived / runtime only |
| Command socket | `~/.orc1_cmd.sock` | runtime IPC |

All paths are `$HOME`- or `__file__`-relative, so the same code runs for any user on any machine.

## Process management

systemd units, all running as `pibox`:

- **`orc1`** — the brain.
- **`orc1-jackd`** — the sole JACK server.
- **`orc1-drum` / `orc1-poly` / `orc1-chip` / `orc1-sampler` / `orc1-mono` / `orc1-bbkit`** — the six per-module audio hosts (2026-09 split). Each is `PartOf=orc1` (follows its restart — a *MIDI* coupling), `BindsTo=orc1-jackd` (an *audio* coupling — a card switch restarts jackd, which cascades the hosts so they reconnect), and sets `JACK_NO_START_SERVER=1`.
- **`orc1-web`**, **`orc1-system`**, **`skipback-record`**.
- **`orc1-daw` / `orc1-dj` / `orc1-visuals`** — the on-demand HDMI kiosks (REAPER / Mixxx / the line-scene visuals), one at a time on VT7; see "The HDMI monitor" above. **`orc1-vistap`** — the visuals' listen-only C JACK client, started and stopped with `orc1-visuals`.
- **`orc1-bridge@<card>.<dir>`** — on-demand ALSA↔JACK zita bridges (no `[Install]`; started from `/patch`). Plus the dedicated **`pisound-in/out`** and **`performve-in/out`** bridges, and **`bt-route`** (Bluetooth output router).
- **`orc1-synth` is RETIRED** — disabled + inactive; `orc1_synth.py` is now a shared library, not a service (its old process runtime — `SynthEngine`, `_route_midi`, `run_jack`/`main`, the main-mix coupling — was deleted 2026-09-24, so running it directly just imports and exits). Do not read its `inactive` state as a fault.
- `orc1-health` runs from a timer (`orc1-health.timer`), not continuously.

Reference copies of the units live in `orc/systemd/` and are installed to `/etc/systemd/system/`.

**`orc1-jackd` is the sole owner of the JACK server** (since 2026-07-14). It resolves the soundcard and runs `jackd` in the foreground so systemd tracks it directly; it is `PartOf` nothing, so a module-host crash can never take down audio or the skipback ring. Every other JACK client sets `JACK_NO_START_SERVER=1` and simply connects — this is what killed the old cold-boot race where several clients each tried to autostart jackd. Its `ExecStartPre` reaps any stale jackd and clears `/dev/shm/jack*`.
