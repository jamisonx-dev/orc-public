# mxvbox: the MXV S805 box as a beat-locked visuals screen

> Deploy the box with `bash mxvbox/tools/box_deploy.sh test|install` from `orc/`.

An old Android TV box (Amlogic S805) running LibreELEC from its INTERNAL flash (Android
wiped 2026-10-29), reached over a private cable to the Pi's eth0 OR the home Wi-Fi,
drawing bold low-res pictures locked to the ORC's beat. Built for
projecting during a performance.

## How it fits together

```
 ORC brain ──/dev/shm/orc1_playstate.json──┐         ┌─ the box (10.55.0.6 or mxvbox.local) ─┐
 (beat, bindings)                          ├─> conductor ──UDP 9000──> orcflash.py ──> fb0 ──> HDMI
 ORC1-Synth-Bus ── drum hits (read only) ──┤
 ORC-mxvbox / ORC-visuals ── knobs, cues ──┘   (Pi, orc1-boxvis)    (one command per line)    720p
```

- **Why not Kodi:** every clip change in Kodi is a decoder flush + refill: 0.5-1 s, never
  the same twice, plus a black flash. The framebuffer path has no decoder: paint the hidden
  page (~5 ms at 320x180), flip (waits one refresh), so a change lands within a frame.
- **Pi cost:** the conductor, ~3-4 % of one core. No display stack on the Pi.

## Cable or Wi-Fi

- The conductor checks eth0's carrier once a second: cable up = `10.55.0.6` (~1 ms),
  cable down = the box's Wi-Fi as `mxvbox.local` (mDNS; ~8 ms round trip through the
  router, measured). The Pi's ssh alias `mxvbox` follows the same rule (`~/.ssh/config`
  `Match ... exec` on the carrier, `HostKeyAlias mxvbox` so one saved key fits both).
- The box obeys UDP from `10.55.0.1` plus the Pi's Wi-Fi address, which the conductor
  writes to the box's `/dev/shm/orc/pi_addrs` over ssh on every status call.
- **Why the box runs from internal flash, no SD card:** on the S805 the SD card and the
  Wi-Fi chip share one SDIO controller. ANY card read (even 300 KB/s) stalled the Wi-Fi
  for 1-6 s (pings 1-6 s vs 10 ms idle). Reads from the internal NAND do not. Keep the SD
  card as a backup only (it holds the setup as of the 2026-10-29 switch; put it back in
  and the box boots from it again, card first).
- `box/wifikeep.sh` (service `wifikeep`): the box's RTL8723BS often misses the network in
  a scan, so after a power cut it never rejoined by itself; this retries every 10 s.
  `box/boxwifi.sh`: asks for a Wi-Fi password and saves it as a connman `.config`
  (run it in a terminal: `ssh -t pibox@pibox.local "ssh -t mxvbox /storage/orc/boxwifi.sh"`;
  the password shows while typed, the box has no `stty`).
- On the home network the box offers only key-only ssh, UDP 9000 (Pi only) and mDNS;
  `pulseaudio` and `rpcbind` are masked in `/storage/.config/system.d`.

## box/ (Python 2.7 on LibreELEC; installed in /storage/orc/)

- `orcflash.py`: the player. Service `orcflash.service` in `/storage/.config/system.d`
  (Kodi masked there, so this owns the screen from boot). UDP 9000 from the Pi only. Its
  command list is at the top of the file: patterns (c v h k s o), clips (p, f), overlays
  (b border, m band: colour / mirror / flip / blur), blur (y), interlace (z),
  weave (x), blurred spaced lines (l ... blur), wobble (w). (The CPU's raster bars, r, and its
  coloured l lines were removed 2026-11-02 with the compressed clips: owner, "drop the
  raster and that other".) Applies every queued
  command in order and draws once, so it can never fall behind.
- `gpu.py` (2026-11-02): the box's Mali-450 GPU (OpenGL ES 2.0 through ctypes) puts each
  composed picture on the screen: one texture upload + one draw onto fb0 (EGL fbdev window,
  640x360 surface, swap synced to the refresh), the chip scales it up. The thin lines (`n`, and
  `t` when the 2nd layer is off) are drawn by the GPU from a row mask built on the CPU (the
  Mali's 16-bit fragment maths put mod()-computed lines on the wrong rows). The fade (`a`)
  still goes through the chip. If EGL won't start, or `/storage/orc/gpu_off` exists, the
  player draws into the framebuffer the old way (same looks). Wobble went from ~10 % late
  frames to 0 per 10 s. BLUR (`y`, the kick hit) is made by the GPU too, live, for ANY picture
  at any strength (`y 2.35`): a dual-Kawase blur
  (shrink in halves to 20x11, grow back), ~6 ms; the conductor fades a blur hit in hundredths, so it
  glides back to sharp. The other blur looks use it too (interlace `z`, blur borders / bands
  `b`/`m blur`, blurred lines `l`/`n .. blur`): orcflash's `blur_mask()` says which pixels show
  the blurred picture, the GPU swaps them in. There are NO stored blur copies any more (removed
  2026-11-02 at owner's say-so, 1.9 GB freed on the box); the no-GPU fallback simply has no
  blur (a blur kick draws a mirror border there).
  THE GPU LOOKS (2026-11-02), knobs CC 31-38 on mxvbox's channel and the BOX tab's KALEIDOSCOPE /
  COLOUR / CLIP CHANGES groups, defaults off / as filmed: kaleidoscope (3/4/6/8 mirrors, SPIN up to
  a turn a bar), RGB split (up to 6 px), hue / saturation / invert / hue cycle, and a crossfade on
  clip changes (up to 2 s; the old clip keeps playing out while it dissolves). Box commands
  K / R / C / X (orcflash's GFX); the conductor sends them when a knob moves and every 5 s. The
  wobble (w) is drawn by the GPU too now: free and smooth (no 1 px steps). They bend where the
  picture is READ, so hits, blur and the crossfade bend with them; the thin lines stay on the
  screen's own rows. Worst case (everything + blur + lines + crossfade) ~21-25 ms a frame.
  HIT REACTIONS (2026-11-02): KICK / SNARE STYLE (CC 21 / 22) pick one of ten, in tenths of the
  knob (mxvbox_knobs.REACTIONS, a dropdown on the BOX tab): blur, RGB split, invert flash, zoom
  punch, warp, shudder, grey, hue jump, off, kaleidoscope snap. ALL are GPU reactions: each hit is
  ONE message, `h KIND AMT FADE_MS`, and the box holds + fades it itself (orcflash HITS), on top of
  the standing looks; HIT SIZE scales them, FADE sets how long. The CPU-drawn mirror border /
  flipped band / deep colour (and the b / m / f commands) were removed 2026-11-02 ("those cpu ones
  can go"); invert / warp / grey took their slots, so saved values of the others kept their meaning.
  The box ignores a stray b / m / f (an old conductor), never letting it stop a clip.
  SCENES (2026-11-02): pictures the GPU DRAWS (no clip, no art), shown in the PATTERN bars: SCENE
  (CC 39: patterns / parallax / floor / tunnel / rings / spiral / op-art), SCENE SPEED (40, 64 = a
  cycle a bar, tempo-locked), SCENE DETAIL (41). Drawn at 320x180 into the picture slot, so every
  look, blur and GPU hit works on them. Box command `S NAME SPEED DETAIL` (gpu.FS_SCENE,
  scene_phases). Phases are wrapped on the CPU and every pattern repeats once a phase (seamless).
  Stripe speed is capped at 2 cycles/s and the contrast is moderate (photosensitivity). The CPU-drawn
  reactions (mirror / flip / deep colour) do not show on a scene.
  SCENE VIDEO (CC 42, 2026-11-02): 0 = drawn colours .. 127 = the scene filled with YOUR clips: three
  clips picked from RAM when a scene starts (kept while it is re-sent, never evicted while it runs):
  parallax sky / far ridge / hills / perspective ground, floor + tunnel tiles alternating two clips,
  rings / spiral / op-art made of two clips. Tiles mirror at their edges (no seams).
  PACING (2026-11-02): the video scenes stuttered at ~20 fps with frames 33 / 50 ms apart, though the
  GPU work is only ~6 ms (measured off screen): the loop timed frames from the last draw's START. Now
  each frame starts PACE_S (17.7 ms) after the last swap (swaps return at a refresh), aiming at every
  2nd refresh; measured ~35 fps, 0-3 late per 10 s. Scene clip frames upload only when they change,
  into the one of two textures the GPU is not reading. Swap interval 2 was tried: this driver waits 2
  refreshes from the call, so 26 fps (gpu.SWAP_INTERVAL = 1; /storage/orc/swap_interval overrides).
  SOFT EDGES (2026-11-02, the staircase on moving edges): every scene edge (stripes, rings, checkers,
  ridges) fades across one pixel (fwidth, GL_OES_standard_derivatives), and the screen pass reads the
  picture SMOOTHLY when it is a scene or a look bends it (kaleidoscope, wobble / warp, zoom, shake, RGB
  split); an unbent clip keeps its chunky pixels. Scenes stay 320x180: at 640x360 they cost 11-20 ms.
  SCENE CYCLE (CC 43): the next scene every 1 / 2 / 4 / 8 bars (each picks new clips).
  RAM: SLOT_BUDGET 520 MB (was 620: with the slots full + a GPU test the box FROZE, no swap);
  `_test_gpu.py` refuses to run under 180 MB free (the 3.10 kernel has no MemAvailable: it is
  worked out as free + buffers + cache - shmem).
  LOADER PACING (2026-11-02, after owner's "small freeze"s): it was rolling the whole library
  through RAM, a 40 MB copy every ~2 s, and the late frames clustered there. Now a clip stays at
  least 60 s (SLOT_MIN_S) and a rolling swap happens at most every 8 s (ROLL_GAP_S); picking a pool
  still swaps out clips outside it at once. Every late frame now logs one line in the box journal
  (`late frame N ms ...: copying / hits / crossfade / scene / blur`): look there after a snag.
  The WEAVE (x) is a GPU pass now (gpu.FS_WEAVE, rows from orcflash.weave_rows = exactly mix_rows'
  choice): in Python it cost 25-55 ms a frame live and every remaining late frame had a weave running.
  BOX ADDRESS: mxvbox.local (mDNS) drops out for minutes at a time; the conductor keeps the last
  good address, and its status call, the web (pools / delete) and the inbox all ssh to that address
  (`-o HostName=`), never the name. (A failed status call used to read as 0 clips: patterns only.)
- `_test_orcflash.py`: 57 checks, run ON THE BOX (`python /storage/orc/_test_orcflash.py`).
- `_test_gpu.py`: 118 checks, ON THE BOX (needs its GPU): draws OFF SCREEN and compares every
  pixel with the framebuffer path's own output (`expand`), plus the GPU / fallback choice.
  6 mutations red.
- `restart.sh`, `fbgrab.py` (prints the on-screen page as raw BGRA: how looks get checked),
  `autopower.sh` (the u-boot `det_pwdn` edit; did not stop the cold-boot standby).
- Its clip LIBRARY is on its own storage (internal flash): `/storage/orc/lib/<id>/clip.etc`
  (since 2026-11-02: COMPRESSED, ETC1 = the Mali's own texture format, 640x360 at 15 fps: 4x the
  old picture in half its RAM, ~20 MB a 12 s clip; the GPU decodes it for free) or the old
  `clip.raw` (320x180 BGRA; still plays, and is all there is without the GPU) + `meta.json`.
  The encoder is `etcpak` (pip, on the Pi), handed the BGRA bytes as they are, so the GPU sees a
  compressed frame exactly like a raw one; the CPU never touches a frame's pixels. The whole
  library was re-made from the originals in `~/box_inbox/done` on 2026-11-02. Listed by the box in
  `lib/index.json`. It fills RAM slots (`/dev/shm/orc`, 520 MB: ~25 compressed clips) from there with a
  nice/ionice-idle copy process and picks clips itself (`p next` / `p cue N` / `x next`).
  A clip stays in RAM at least `SLOT_MIN_S` (60 s) before it can be swapped out: without
  that the library rolled through RAM nonstop (a clip every 2-7 s).
  `slotpolicy.py` = the slot rules (Python 2 + 3, `_test_slotpolicy.py` 19).
- **POOLS** (2026-10-29, owner: hand-picked "pools of similar things" picked live, plus
  a randomized everything list; no songs): `lib/pools.json` = `[{name, ids}]`, written by
  the BOX tab (`/api/mxvbox/pools` over ssh). The POOL knob (CC 30) picks one: 0 =
  everything, N = pool N (`g N` to the box, resent every 5 s). The box then loads and
  shows only that pool's clips; a clip outside it is swapped out at once (shown or not).
  A pool whose clips are all gone, or a number that doesn't exist, = everything.
- After a power cut the box waits for its power button (below the bootloader). Software
  reboots come back by themselves.

## pi/ (python3; service orc1-boxvis)

- `conductor.py`: its own step clock phase-locked to the feed; each 16th step's picture
  sent once, in order; drum note-ons (channels of `drums` / `bbkit`, by name) fire the
  hits; follows the bus across brain restarts.
- `mxvbox_knobs.py`: the module's knobs (CC 7 + 20-30; note = cue a clip). Pure data,
  read by the conductor, the /cc page and the /visuals **BOX** tab. Saved in
  `~/.orc1_mxvbox.json`.
- Clips: the BOX owns them (its storage + its own RAM slots); the conductor only says `p next`,
  `p cue N`, `x next` and reads the box's status every 10 s (BOX tab; clip bars become
  pattern bars while the box has nothing loaded).
- `boxguard.nft` (service `orc1-boxguard`): the box may get DHCP + ping and open NOTHING
  on the Pi, on the cable (eth0) and on the Wi-Fi (matched by its Wi-Fi card's MAC; mDNS
  answers allowed so the Pi can find it).
- `_test_conductor.py` (50, incl. a laggy-feed step-clock simulation and cable/Wi-Fi
  choice); Pi or PC.

## The box inbox (drag and drop)

`\\pibox\boxinbox` on the home network (a Samba share on the Pi, `~/box_inbox`): drop
videos in it.
`pi/inbox.py` (service `orc1-boxinbox`, idle CPU + IO priority, `MemoryMax=400M`) waits
until a file stops growing, converts it with the tool's own code (needs `ffmpeg` on the
Pi), sends it to the box's library and moves the original to `done\` (or `failed\` with a
`.why.txt`); `STATUS.txt` says what it is doing. Box off = files wait. Tests:
`pi/_test_inbox.py` (18). On the home Wi-Fi the box answers only key-only ssh, UDP 9000
(Pi only) and mDNS.

## tools/ (PC)

- `Box clips.bat` / `make_clips.py`: drag videos on -> ADDED to the Pi's library (fills
  the frame; one clip must fit a box slot: ~50 s). `--list` shows the library, `--replace` empties it first.
  It makes the compressed kind (clip.etc) when `etcpak` is installed (`pip install etcpak`;
  NOT on the PC yet, 2026-11-02), else the old 320x180 kind, and says so. The Samba inbox
  (`\\pibox\boxinbox`, `pi/inbox.py`) uses the same converter on the Pi, which has etcpak:
  dropping videos there gives the sharp kind. `_test_make_clips.py` (6, run on the Pi) checks it.
- `box_deploy.sh test|install` (run in `orc/` from Git Bash): stages `box/*.py` on the box, runs all three
  box suites there; `install` then backs up, installs and restarts orcflash, only if every suite passed.
- `box_mutate.py`: mutation-tests box code on the box, in a copy of the staged files (usage in its header).

## Controls

Bind the module `mxvbox` to any channel in /matrix (15 for now). Everything is a CC on that
channel (see `/cc` or `mxvbox_knobs.py`), so a Fire knob, a pad, a scene or the /visuals
BOX tab can drive it.
