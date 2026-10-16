# ORC-1 FX system — as built (2026-09-03)

Authoritative reference for the effects system after the audio-host split. Read
this before touching anything under `orc1_fxmix.py`, `orc1_fx_inserts.py`,
`fx_chain_manager.py`, `orc1_fx_rack.py`, `orc1_fx.py`, `gen_fx_palette.py`,
`fxprobe.py`, or the `/fx` page. **The code on the Pi is truth; this documents it.**

---

## 1. The model — arbitrary per-source wet/dry INSERTS, no bridging

Effects are placed as **inserts**. An insert takes **ONE stereo source** — a module
output (`ORC1-Poly:out_L/R`, `ORC1-Chip:out`, `ORC1-Drum`, `ORC1-Sampler`,
`ORC1-Mono`) **or a hardware input** (e.g. `system:capture_1/2` — a pisound fx
loop) — runs it through a jalv fx chain at 100 % wet, blends dry↔wet at **audio
rate**, and sends the result to **ONE stereo destination** (usually
`system:playback_1/2`).

Many inserts run at once. Each is **completely independent** — its own source,
chain, destination, mixer and control. **There is NO summing/bridging between
inserts** (poly's fx never touches chip's audio). This mirrors the module-host
north star: independent units, recognised by config, each able to sit on its own
I/O later. **Do not re-introduce a single global "fx bus" that sums the modules** —
that was built once (commit 28d5b18) and deliberately reverted (92b9b8f) because it
bridged the modules.

### Signal flow of one insert `ORC1-FX-<id>`
```
  src ─┬─────────────────────────────────────────────────────────→ (NOT to dst)
       └─→ FX-<id>:dry
           FX-<id>:send (= dry passthrough) ─→ [ jalv chain, 100% wet ] ─→ FX-<id>:wet
           FX-<id>:out = (dry·(1-mix) + wet·mix) · level  [slewed + limited] ─→ dst
```
The mixer **CUTS the direct `src → dst`** so the insert sits in the path. `mix` and
`level` slew per-sample (~25 ms) so riding them is glitch-free; the out has its own
`PeakLimiter` (a hot reverb build-up hit 2.9 and clipped without it).

### Why audio-rate (the load-bearing finding)
LV2 control ports are read **once per audio block**, so a plugin's own dry/wet or
level control STEPS when you ride it — writing the jalv FIFO faster can't smooth it
(`fxprobe` proved it: even 70 fine steps still clicked). So the host does the blend
itself at sample rate, leaving the plugin at 100 % wet. **Reverb size/decay is
intrinsically un-rideable** (the plugin re-tunes its delay network) — set it while
parked; the wet/dry mix rides fine regardless.

---

## 2. Components / file map

| File | Role |
|---|---|
| `orc1_fxmix.py` | ONE insert **instance**. CLI: `--name --src L,R --dst L,R --mix-cc N --level-cc N`. `FxMixer` = the wet/dry+level DSP (slewed, limited). A reconcile thread owns THIS instance's fixed wiring (src→dry, out→dst, cut src→dst) — runs one pass immediately, then polls every 3 s so it survives a src/dst restart. Reads its mix/level CCs off the shared bus. |
| `orc1_fx_inserts.py` | The **manager**. Reads `~/.orc1_fx_inserts.json`; per insert launches an `ORC1-FX-<id>` mixer + a jalv chain wired `send→p0→…→wet` (via the rack's mono/stereo-tolerant `_wire_bus`). Assigns each insert a CH_CTRL mix/level CC pair = `CC_BASE(40) + 2*slot`. `reap_and_apply()` kills orphans then applies; `_stop_insert` RESTORES `src→dst` on removal. CLI: `apply|stop|status`. |
| `orc1.py` (brain) | Constructs `InsertManager`, runs `reap_and_apply` on start (bg thread). `FXMIX_SET {id, mix, level}` resolves the insert's CCs and emits them; `FX_INSERT_APPLY` re-reads the config + reconciles live. Globals `fx_inserts`, `_FX_INSERTS_AVAILABLE`. |
| `fx_chain_manager.py` | Low-level jalv layer: `spawn_jalv(client, uri)`, `jack_connect`, `load_palette()`. Params to a running plugin go via its FIFO `/tmp/jalv-<client>.fifo` as `symbol = value`. |
| `orc1_fx_rack.py` | The LEGACY single-chain "rack" (`/fx` page, Fire). `RackManager` + banks/categories/favourites. Holds `_wire_bus` (mono/stereo-tolerant) and `_wait_for_ports`, which the insert manager reuses. Still present; the global send/return `fx_io` path (see §6) is legacy. |
| `orc1_fx.py` | `FXManager` = `eng.fx`, wraps the rack for the Fire/web/cmd handler. |
| `gen_fx_palette.py` | Regenerates `fx_palette.json` from installed LV2 (`--all` = every plugin). **Machine-specific / generated** — marked `-diff` in `.gitattributes` so it never bloats a review. |
| `fxprobe.py` | Ride-live tester: hosts a plugin via the real jalv+FIFO path, sweeps each control with a tone running through, scores transients. Calibrated on galactic `bigness` (glitchy). Use to vet a plugin before adding it as a keeper. |
| `fx_ride_map.json` | Measured per-knob "ride live vs set-while-parked" verdict per keeper. |
| `fx_categories.json` / `~/.orc1_fx_favorites.json` | Category map + the 17 curated keeper favourites. |

---

## 3. Config — `~/.orc1_fx_inserts.json` (runtime, per-box)
```json
{"inserts": [
  {"id": "poly",
   "src": ["ORC1-Poly:out_L", "ORC1-Poly:out_R"],
   "dst": ["system:playback_1", "system:playback_2"],
   "chain": ["Reverb"]},
  {"id": "vox",
   "src": ["system:capture_1", "system:capture_2"],
   "dst": ["system:playback_1", "system:playback_2"],
   "chain": ["Saturator", "vibrato"]}
]}
```
- `id` — unique; becomes the JACK client `ORC1-FX-<id>` and the FXMIX_SET address.
- `src`/`dst` — stereo JACK port pairs (any ports; module outs or hardware in/out).
- `chain` — ordered palette plugin KEYS (last URI segment: `Reverb`, `vibrato`,
  `MonoCompressor`, …). Empty = a bare wet/dry gain (dry passthrough, no effect).
- Slot order fixes the CC pair. Apply with `FX_INSERT_APPLY` or a brain restart.

---

## 4. Control
- **Per instance** (the norm): `FXMIX_SET {"id": "poly", "mix": 0.0-1.0, "level": 0.0-1.0}`
  over the cmd socket → the brain resolves that insert's CCs and emits them on
  CH_CTRL; the mixer slews. `mix` 0 = dry (bypass), 1 = full wet. `level` 1.0 ↔ CC64
  = unity (mixer scales `/64` → 0..2×). **Default mix=0** so inserting never
  silences a source.
- **Add/remove/reconfigure live**: edit the config, send `FX_INSERT_APPLY`.
- **CC allocation**: `CC_BASE = 40`; insert slot i → mix `40+2i`, level `41+2i`
  (clear of the module bus-level CCs 7-11, bind 20/21, legacy FXMix 3/4).
- **Legacy** single-mixer control (`FXMIX_SET` with no `id`) uses CC 3/4.

---

## 5. Curated palette (declutter + rideability)
341 LV2 plugins installed: **Airwindows** (177, characterful/redundant), **Calf**
(~40, solid conventional fx), **x42** (~120, mostly meters/midifilter). Curated to
**17 keeper favourites** (Fire "Favorites" bank + web ★ Favourites optgroup); the
full palette stays browsable under categories. `fx_ride_map.json` records which
knobs ride clean (measured): e.g. `capacitor2` lowpass/highpass, `vibrato`
speed/depth, `RotarySpeaker`, `VintageDelay` feedback/time/filter, Calf `Reverb`
decay/damping. The universally glitchy knobs are **dry/wet and level** (which the
insert mixer now owns at audio rate) and **reverb size/decay** (intrinsic).

---

## 6. Two coexisting fx paths (know which is which)
1. **Inserts (this doc, current)** — `orc1_fx_inserts` + `orc1_fxmix`, per-source,
   independent, rideable. The way forward.
2. **Legacy global send/return** — `orc1_fx_rack` + `~/.orc1_fx_io.json`
   (`send`/`return` port pairs) + the `/fx` page. Still functional for a single
   global chain. Don't confuse the two — the inserts do NOT use `fx_io`.
   - **HARDWARE INSERT** = explicit send/return ports (a physical cable loop;
     defaults = the pisound pair).
   - **INTERNAL** (since 2026-09-28) names ONE module: `{"mode": "internal",
     "module": "poly"}`. `orc1_config.fx_io_resolve` turns it into the module's
     `out_L/R` (send) and wherever its /patch module output sends it (return),
     at READ time, so it follows a module-output change. Every reader goes
     through it: `fx_chain_manager._get_fx_ports`, `/api/fx/io`, the health
     check. Typing ports by hand on /fx = custom ports (module cleared).
   - **Dry-cut:** while the rack (or a legacy chain) runs in INTERNAL, the
     source's own `send→return` wires are cut (`FXManager.internal_acquire`)
     and restored EXACTLY as cut (the pairs are remembered). The rack gives
     its token back before every rebuild, so switching the module while it
     runs restores the old source and cuts the new one.
   - Before 09-28 INTERNAL was hardcoded to the retired `ORC1-Synth` and the
     dry-cut named it too, so it sent nothing and cut nothing. Known limit: a
     single module-host restart re-adds its dry wire while the rack is on
     (dry + wet) until the next rack apply.

---

## 7. Gotchas (all learned the hard way / caught by ultrareview)
- **Mono plugins**: an insert chain can contain a 1-in/1-out plugin. Wiring MUST be
  mono/stereo-tolerant (sum both sends into a mono in, fan a mono out to both wet).
  The manager reuses the rack's `_wire_bus` for this — do NOT wire with a naive
  `zip` (it drops a channel; ultrareview caught exactly that).
- **Reconcile timing**: an insert's wiring pass must run IMMEDIATELY on start, not
  after the first `stop_event.wait(3.0)` — otherwise the source is dead (and its
  direct path uncut) for 3 s.
- **Removing an insert must RESTORE `src→dst`**: the killed mixer had cut it, and a
  module host only re-auto-connects at its OWN startup, so a removed insert would
  leave the source silent. `_stop_insert` reconnects it. (On a brain restart the
  hosts restart too and self-heal.)
- **`~/.orc1_bus_levels.json` is MULTI-WRITER** since the split (poly/chip via the
  synth's `_flush_bus_levels`, drum, mono). All writes go through the
  flock-protected `S._bus_level_write` — never RMW that file directly, or a
  concurrent flush loses a key.
- **Never test a persistence helper against its live file**: `S._bus_level_write`
  uses the hardcoded `BUS_LEVELS_FILE`; a concurrency test once clobbered the real
  levels. Point the module's path constant at a temp path first.
- **`fx_palette.json` is generated**; regenerate with `gen_fx_palette.py --all`
  rather than hand-editing; it's `-diff` in `.gitattributes`.

---

## 8. Done / pending
**Done + verified live:** instance-able wet/dry inserts; the manager (arbitrary
inserts, no bridging, mono-safe wiring); brain integration (apply-on-start, reap,
`FXMIX_SET {id}`, `FX_INSERT_APPLY`, `src→dst` restore); the curated palette + ride
map + `fxprobe`; the audio-rate glitch-free mix/level.

**Pending:** a **web/Fire UI** to add/assign inserts (pick source, destination,
chain) and ride each insert's mix/level — today that's config-file + `FX_INSERT_APPLY`
+ `FXMIX_SET {id}`. The `_test_fxmix.py` suite covers the `FxMixer` DSP + per-instance
CC routing; there is no offline suite for the manager's process/JACK wiring (it's
integration-tested live). Memory: `orc1-fx-curation`.
