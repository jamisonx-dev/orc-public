// ORC-1 CHIP page — NES + GB voices
'use strict';

// ── Voice channel variables (updated from /api/config on load) ────────────────
let CH_CHIP     = 9;   // NES Pulse 1
let CH_CHIP_P2  = 6;   // NES Pulse 2 (took ch6 when drums moved to GM ch10)
let CH_CHIP_TRI = 11;  // NES Triangle
let CH_CHIP_NS  = 4;   // NES Noise
let CH_GB_WAVE  = 5;   // GB Wave
let CH_CHIP_PCM = 16;  // NES PCM

// ── Defaults (keyed by voice name; rebuilt into _DEF by channel on load) ─────
// CC 1 (duty/waveform) midpoints: 16=12.5%, 48=25%, 80=50%, 112=75%
// CC 3 (noise mode): 32=musical(long), 96=metallic(short)
// NES couple modes (7): none/unison/detune/fifth/third/octave/octave_dn
//   CC 72 range 0-127 across 7 modes: midpoints 9,27,45,63,81,99,118
// GB expansion CCs (2026-07-20): 77=vibrato depth, 78=vibrato rate,
//   79/80/81 = sweep time/shift/direction (P1 only; stored as band midpoints)
const _DEF_VOICES = {
  p1:   {1:80, 8:0, 9:64, 10:89, 11:50, 13:89, 74:64, 71:0, 70:21, 75:64,
         77:0, 78:64, 79:8, 80:8, 81:0},
  p2:   {1:80, 8:0, 9:64, 10:89, 11:50, 13:89, 74:64, 71:0, 70:21, 75:64,
         77:0, 78:64},
  tri:  {13:89, 74:64, 71:0, 70:21, 75:64},
  ns:   {3:32, 8:0, 9:64, 10:0, 11:40, 13:89},
  // GB wave: 8/9/10/11 = A/D/S/R (2026-08-04 — this voice now has its own
  // envelope, incl. the release that replaced the clicking instant cutoff).
  // 22 = volume mode (0 continuous / 127 authentic 4-step), 24 = retrigger
  // mode (0 phase-continuous / 127 authentic phase reset). Both default to
  // the smooth, non-clicking side.
  // ⚠ These must match GameboyWaveVoice.__init__, or the page misreports the
  // voice until a CC is sent (and the first touch of a slider is a jump).
  // Times go through _cc_to_time(v, lo, hi) = lo*(hi/lo)**(v/127), so:
  //   CC8  11 -> 2ms attack   CC9  63 -> 120ms decay   CC11 68 -> 150ms release
  //   CC10 102 -> sustain 0.80   CC7 127 -> gain 1.0 (continuous by default)
  gb:   {1:8, 7:127, 8:11, 9:63, 10:102, 11:68, 22:0, 24:0, 77:0, 78:64},
  link: {72:9, 73:64, 76:0},  // P1+P2 link, sent on CH_CHIP
};
// Per-voice performance mods (2026-09-24, NESEngine _ChipMod): these are the
// engine's own defaults (glide/arp/slide/tremolo/retrigger off, pan centre;
// arp 15 steps/s = 62, slide time 0.1 s = 55, tremolo 6 Hz = 86).
const _MOD_DEF = {5:0, 86:64, 87:0, 88:0, 89:62, 90:64, 110:55, 111:0, 112:86, 113:0};
for (const v of ['p1', 'p2', 'tri', 'ns', 'gb']) Object.assign(_DEF_VOICES[v], _MOD_DEF);
_DEF_VOICES.ns[114] = 0;   // noise key split off
// Rebuilt by _rebuildDef() so _ccVal lookups still work after channel changes
let _DEF = {};
function _rebuildDef() {
  _DEF = {};
  _DEF[CH_CHIP]     = _DEF_VOICES.p1;
  _DEF[CH_CHIP_P2]  = _DEF_VOICES.p2;
  _DEF[CH_CHIP_TRI] = _DEF_VOICES.tri;
  _DEF[CH_CHIP_NS]  = _DEF_VOICES.ns;
  _DEF[CH_GB_WAVE]  = _DEF_VOICES.gb;
}
_rebuildDef();

// Map voice name → current MIDI channel
function _voiceCh(voice) {
  return {p1: CH_CHIP, p2: CH_CHIP_P2, tri: CH_CHIP_TRI,
          ns: CH_CHIP_NS, gb: CH_GB_WAVE}[voice] || CH_CHIP;
}

let _synthCC = {};
let _spreadOn = false;

// Persisted chip params from ~/.orc1_chip_params.json, remapped to {ch: {cc}}.
// A fallback layer BETWEEN synth_cc (web-set CCs only) and _DEF: after a restart
// the synth loads its patch from that FILE, not via a web CC, so synth_cc is
// empty and the page would show boot defaults instead of the real saved state.
// The file nests by the SYNTH voice name; map those to the JS voice tags (the
// coupling CCs 72/73/76 are ephemeral and never in the file -> they fall back to
// _DEF, correct). Same fix as /drums (2026-08-13).
const _SYNTH_VOICE = {pulse1: 'p1', pulse2: 'p2', triangle: 'tri',
                      noise: 'ns', wave: 'gb'};
let _savedParamsCC = {};
let _savedRaw = {};          // /api/chip_params as fetched (voice-keyed)
let _gbWaveCC = {};          // harmonic knob positions from /api/gb_wave, as {cc: val}
// Rebuild _savedParamsCC (channel-keyed) from the voice-keyed file data; rerun
// whenever the bindings move a voice to another channel.
function _remapSaved() {
  const mapped = {};
  for (const [sv, ccs] of Object.entries(_savedRaw)) {
    const tag = _SYNTH_VOICE[sv];
    if (!tag || !ccs || typeof ccs !== 'object') continue;
    const ch = String(_voiceCh(tag));
    mapped[ch] = Object.assign(mapped[ch] || {}, ccs);
  }
  const gch = String(CH_GB_WAVE);
  mapped[gch] = Object.assign(mapped[gch] || {}, _gbWaveCC);
  _savedParamsCC = mapped;
}

function _ccVal(ch, cc) {
  const c = _synthCC[String(ch)];
  if (c && c[String(cc)] !== undefined) return c[String(cc)];
  const s = _savedParamsCC[String(ch)];
  if (s && s[String(cc)] !== undefined) return s[String(cc)];
  return (_DEF[ch] || {})[cc];
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function onCC(ch, cc, value, valId) {
  if (valId) document.getElementById(valId).textContent = value;
  cmd('SYNTH_CC', {ch, cc, value});
}

function audition(ch, note) {
  cmd('SYNTH_NOTE', {ch, note, vel: 100, ms: 500});
}

// ── Duty cycle (P1/P2 CC 1) ───────────────────────────────────────────────────
// Buttons use data-voice="p1"/"p2" so selection works after channel remaps
function setDuty(voice, val) {
  document.querySelectorAll(`.duty-btn[data-voice="${voice}"]`).forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: _voiceCh(voice), cc: 1, value: val});
}
function _dutySnap(v) { return v < 32 ? 16 : v < 64 ? 48 : v < 96 ? 80 : 112; }

// ── Filter mode (shared helper) ───────────────────────────────────────────────
// Buttons use data-voice so selection works after channel remaps
function setFilterMode(voice, val) {
  document.querySelectorAll(`.fmode-btn[data-voice="${voice}"]`).forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: _voiceCh(voice), cc: 70, value: val});
}
function _fmSnap(v) { return v < 43 ? 21 : v < 85 ? 64 : 106; }

// ── Noise mode (CC 3) ─────────────────────────────────────────────────────────
function setNoiseMode(val) {
  document.querySelectorAll('.noise-mode-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: CH_CHIP_NS, cc: 3, value: val});
}
function _noiseModeSnap(v) { return v < 64 ? 32 : 96; }

// ── GB Wave bank (CC 1, range 0-15 per bank → midpoints 8,24,40,56,72,88,104,120) ──
function setGBWave(val) {
  document.querySelectorAll('.bank-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 1, value: val});
  // Picking a preset reverts the synth out of custom Wave-RAM mode (CC1 does
  // that synth-side) — mirror it here so the UI can't show CUSTOM as active.
  if (typeof _setWaveCustomUI === 'function') _setWaveCustomUI(false);
}
// ── GB Wave hardware-accuracy switches (2026-08-04) ───────────────────────────
// CC 22: volume quantised to the DMG's 4 levels. CC 24: authentic phase reset
// on every trigger. Both are the ORIGINAL clicking behaviour, kept as opt-in.
function gbVolQuant(on) {
  _gbSwitchUI('gb-volq-btn', 'gb-volc-btn', on);
  cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 22, value: on ? 127 : 0});
}
function gbHardRetrig(on) {
  _gbSwitchUI('gb-hardrt-btn', 'gb-softrt-btn', on);
  cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 24, value: on ? 127 : 0});
}
function _gbSwitchUI(onId, offId, on) {
  const a = document.getElementById(onId), b = document.getElementById(offId);
  if (a) a.classList.toggle('active', !!on);
  if (b) b.classList.toggle('active', !on);
}

function _gbBankSnap(v) {
  // 8 banks of 16 values each; snap to midpoint
  const idx = Math.min(7, Math.floor(v / 16));
  return idx * 16 + 8;
}

// ── P1+P2 couple mode (CC 72 on CH_CHIP) ─────────────────────────────────────
// 7 modes evenly across 0-127: idx = min(6, value*7//128)
// Midpoints: none=9, unison=27, detune=45, fifth=63, third=81, octave=99, octave_dn=118
const COUPLE_VALS = [9, 27, 45, 63, 81, 99, 118];
function setCouple(val) {
  document.querySelectorAll('.couple-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: CH_CHIP, cc: 72, value: val});
}
function _coupleSnap(v) {
  const idx = Math.min(6, Math.floor(v * 7 / 128));
  return COUPLE_VALS[idx];
}

// ── Stereo spread (CC 76, 0-63=off, 64-127=on) ────────────────────────────────
function toggleSpread() {
  _spreadOn = !_spreadOn;
  const btn = document.getElementById('spread-btn');
  if (btn) {
    btn.textContent = _spreadOn ? 'ON' : 'OFF';
    btn.classList.toggle('active', _spreadOn);
  }
  cmd('SYNTH_CC', {ch: CH_CHIP, cc: 76, value: _spreadOn ? 96 : 32});
}

// ── Pulse anti-alias (CC 85, >=64 = on) ───────────────────────────────────────
// Opt-in PolyBLEP on the pulse osc. Engine-wide over BOTH pulses, so it is sent
// on CH_CHIP and NESEngine flips pulse1+pulse2 together. Default OFF (raw grit);
// persists with the patch (unlike spread/couple, CC 85 is a saved chip param).
let _aaOn = false;
function toggleAA() {
  _aaOn = !_aaOn;
  const btn = document.getElementById('aa-btn');
  if (btn) {
    btn.textContent = _aaOn ? 'ON' : 'OFF';
    btn.classList.toggle('active', _aaOn);
  }
  cmd('SYNTH_CC', {ch: CH_CHIP, cc: 85, value: _aaOn ? 127 : 0});
}

// ══════════════════════════════════════════════════════════════════════════════
// GB CHIP EXPANSION (2026-07-20): pitch bend, sweep, custom Wave RAM
// ══════════════════════════════════════════════════════════════════════════════

// ── Pitch bend — momentary, spring-to-center ─────────────────────────────────
// Uses the NEW 'SYNTH_PITCH' cmd verb: {ch: 1-indexed channel, pitch: -8192..8191},
// 0 = center (no bend).  While dragging we send at most one message per ~30ms
// (trailing edge — the LAST position always gets sent), so a fast wiggle can't
// flood the cmd socket.  On release the slider snaps back to center and we
// always send pitch 0, so a note can never be left stuck bent.
let BEND_RANGE = 12;  // ± semitones — mirrors SYNTH.CHIP_BEND_RANGE (loaded from /api/config)

const _bendState = {};  // per-voice: {pending: last dragged pitch, timer: throttle handle}

function _bendReadout(voice, pitch) {
  // Show the bend in semitones (pitch/8192 × BEND_RANGE), e.g. "+3.2st"
  const el = document.getElementById(voice + '-v-bend');
  if (!el) return;
  const semis = pitch / 8192 * BEND_RANGE;
  el.textContent = (semis >= 0 ? '+' : '') + semis.toFixed(1) + 'st';
}

function bendInput(voice, pitch) {
  _bendReadout(voice, pitch);
  const st = _bendState[voice] || (_bendState[voice] = {pending: 0, timer: null});
  st.pending = pitch;
  if (st.timer === null) {
    st.timer = setTimeout(() => {
      st.timer = null;
      cmd('SYNTH_PITCH', {ch: _voiceCh(voice), pitch: st.pending});
    }, 30);
  }
}

function bendRelease(voice) {
  // Fires on pointerup/change (normal release) AND pointercancel/mouseleave
  // (safety — e.g. finger slides off the slider), so center is guaranteed.
  const st = _bendState[voice];
  const el = document.getElementById(voice + '-bend');
  // Only send the center message if we were actually bent (mouseleave fires
  // on every hover-out, bent or not — don't spam pitch 0 for those).
  const wasBent = (st && (st.timer !== null || st.pending !== 0)) || (el && +el.value !== 0);
  if (st && st.timer !== null) { clearTimeout(st.timer); st.timer = null; }
  if (st) st.pending = 0;
  if (el) el.value = 0;              // spring back to center
  _bendReadout(voice, 0);
  if (wasBent) cmd('SYNTH_PITCH', {ch: _voiceCh(voice), pitch: 0});
}

// ── Pulse 1 hardware sweep (CC79 time, CC80 shift, CC81 direction) ───────────
// The HTML sliders run 0–7 (the real GB register range).  Encode to the CC's
// band MIDPOINT: cc = v*16 + 8.  Synth-side decode is floor(cc*8/128) =
// floor(cc/16), and floor((v*16+8)/16) = v — exact roundtrip for all v in 0..7.
function setSweep(cc, v7, vid) {
  if (vid) {
    // TIME shows "OFF" at 0 (0 = sweep disabled); SHIFT just shows the number
    document.getElementById(vid).textContent = (cc === 79 && v7 === 0) ? 'OFF' : v7;
  }
  cmd('SYNTH_CC', {ch: _voiceCh('p1'), cc: cc, value: v7 * 16 + 8});
}

// Direction toggle: UP sends CC81=0 (0–63 = up), DOWN sends 127 (64–127 = down)
let _sweepDown = false;
function _renderSweepDir() {
  const btn = document.getElementById('sweep-dir-btn');
  if (btn) {
    btn.textContent = _sweepDown ? 'DOWN ↓' : 'UP ↑';
    btn.classList.toggle('active', _sweepDown);
  }
}
function toggleSweepDir() {
  _sweepDown = !_sweepDown;
  _renderSweepDir();
  cmd('SYNTH_CC', {ch: _voiceCh('p1'), cc: 81, value: _sweepDown ? 127 : 0});
}

// ── Custom Wave RAM draw grid (GB CH3: 32 steps × 4-bit) ─────────────────────
// Drawing writes into the local _waveRam array; changed steps are QUEUED and
// flushed every ~50ms so a drag doesn't flood the socket.  Each step goes out
// as a sequential CC pair on the wave channel:
//   CC20 = index*4  (synth decodes value*32//128 = value//4 → (index*4)//4 = index,
//                    exact for all 0..31 — index*4 tops out at 124 < 128)
//   CC21 = value*8  (synth decodes value*16//128 = value//8 → (value*8)//8 = value,
//                    exact for all 0..15 — value*8 tops out at 120 < 128)
// The pair is awaited in order so the index latch (CC20) always lands before
// its value (CC21).
const WAVE_STEPS = 32, WAVE_LEVELS = 16;
// Start with a triangle shape so ACTIVATE makes sound even before you draw
let _waveRam = Array.from({length: WAVE_STEPS}, (_, i) => (i < 16 ? i : 31 - i));
let _waveDirty = new Map();      // index → value, latest wins during a drag
let _waveFlushTimer = null;
let _waveFlushing = false;       // serializes flushes (a flush awaits its sends)
let _waveTablePushed = false;    // full table sent at least once this page load?
let _waveCustomActive = false;

function _waveQueue(idx, val) {
  _waveDirty.set(idx, val);
  if (_waveFlushTimer === null) _waveFlushTimer = setTimeout(_waveFlush, 50);
}

async function _waveFlush() {
  _waveFlushTimer = null;
  if (_waveFlushing) {          // a previous flush is still sending — retry soon
    _waveFlushTimer = setTimeout(_waveFlush, 50);
    return;
  }
  _waveFlushing = true;
  const batch = _waveDirty;
  _waveDirty = new Map();       // new draws during the flush go into a fresh batch
  try {
    for (const [idx, val] of batch) {
      await cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 20, value: idx * 4});  // latch index
      await cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 21, value: val * 8});  // write value
    }
  } finally {
    _waveFlushing = false;
  }
  if (_waveDirty.size && _waveFlushTimer === null)
    _waveFlushTimer = setTimeout(_waveFlush, 50);
}

function _waveDraw() {
  const cv = document.getElementById('wave-ram-canvas');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  // Pull page colors from the shared.css tokens (with safe fallbacks)
  const cs = getComputedStyle(document.documentElement);
  const accent = (cs.getPropertyValue('--accent') || '').trim() || '#4a90e2';
  const dim    = (cs.getPropertyValue('--dim')    || '').trim() || '#2a2f34';
  ctx.clearRect(0, 0, W, H);
  const cw = W / WAVE_STEPS, chh = H / WAVE_LEVELS;
  // faint grid
  ctx.strokeStyle = dim;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < WAVE_STEPS; i++) { ctx.moveTo(i * cw, 0); ctx.lineTo(i * cw, H); }
  ctx.moveTo(0, H / 2); ctx.lineTo(W, H / 2);   // center line
  ctx.stroke();
  // bars: each step drawn as a filled column up to its 0-15 level
  ctx.fillStyle = accent;
  for (let i = 0; i < WAVE_STEPS; i++) {
    const lvl = _waveRam[i];
    const barH = (lvl + 1) * chh;               // level 0 still shows a sliver
    ctx.globalAlpha = _waveCustomActive ? 1.0 : 0.55;  // dim when preset mode
    ctx.fillRect(i * cw + 1, H - barH, cw - 2, barH);
  }
  ctx.globalAlpha = 1.0;
}

function _waveInit() {
  const cv = document.getElementById('wave-ram-canvas');
  if (!cv) return;
  // Match the canvas's internal resolution to its CSS size so cells are crisp
  const resize = () => {
    cv.width = cv.clientWidth || 320;
    cv.height = 128;
    _waveDraw();
  };
  resize();
  window.addEventListener('resize', resize);

  let drawing = false;
  const plot = (ev) => {
    const r = cv.getBoundingClientRect();
    const col = Math.max(0, Math.min(WAVE_STEPS - 1,
      Math.floor((ev.clientX - r.left) / r.width * WAVE_STEPS)));
    const lvl = Math.max(0, Math.min(WAVE_LEVELS - 1,
      WAVE_LEVELS - 1 - Math.floor((ev.clientY - r.top) / r.height * WAVE_LEVELS)));
    if (_waveRam[col] !== lvl) {
      _waveRam[col] = lvl;
      _waveQueue(col, lvl);
      _waveDraw();
    }
  };
  cv.addEventListener('pointerdown', ev => {
    drawing = true;
    try { cv.setPointerCapture(ev.pointerId); } catch (e) {}
    plot(ev);
    ev.preventDefault();
  });
  cv.addEventListener('pointermove', ev => { if (drawing) plot(ev); });
  cv.addEventListener('pointerup',     () => { drawing = false; });
  cv.addEventListener('pointercancel', () => { drawing = false; });
}

function _setWaveCustomUI(on) {
  _waveCustomActive = on;
  const a = document.getElementById('wave-custom-btn');
  const p = document.getElementById('wave-preset-btn');
  if (a) a.classList.toggle('active', on);
  if (p) p.classList.toggle('active', !on);
  // Preset bank buttons dim while the custom table is live
  document.querySelectorAll('.bank-btn').forEach(b => { b.style.opacity = on ? 0.45 : 1; });
  _waveDraw();
}

async function waveCustomOn() {
  // First activation this page load: push the ENTIRE 32-step table so the
  // synth's custom buffer exactly matches what's drawn (it may hold stale
  // data from a previous session).  After that, only drawn changes are sent.
  if (!_waveTablePushed) {
    _waveTablePushed = true;
    for (let i = 0; i < WAVE_STEPS; i++) _waveDirty.set(i, _waveRam[i]);
  }
  // Make sure every queued step has landed BEFORE activating
  if (_waveFlushTimer !== null) { clearTimeout(_waveFlushTimer); _waveFlushTimer = null; }
  await _waveFlush();
  while (_waveFlushing || _waveDirty.size) { await new Promise(r => setTimeout(r, 25)); }
  await cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 23, value: 127});
  _setWaveCustomUI(true);
}

function waveCustomOff() {
  cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: 23, value: 0});
  _setWaveCustomUI(false);
}

_waveInit();

// ── Per-voice performance mods + GB wave shaping (2026-09-24) ─────────────────
// Every chip voice has tracker-style mods on its OWN channel (NESEngine
// _ChipMod): glide, pan, arp/trill, pitch slide, tremolo, retrigger. The rows
// are built here into each card's .mods-slot; the voice's channel is resolved
// at send time (_voiceCh), so a /matrix move needs no rebuild.
const _msTxt = s => s < 1 ? Math.round(s * 1000) + 'ms' : s.toFixed(2) + 's';
const _MOD_ROWS = [
  {cc: 5,   key: 'gld', lab: 'GLIDE',   tip: 'Portamento: 0 = off, else 5 ms – 2 s from the previous note',
   fmt: v => v ? _msTxt(0.005 * 400 ** (v / 127)) : 'OFF'},
  {cc: 86,  key: 'pan', lab: 'PAN',     tip: 'Pan: centre = 64 (both sides full level)',
   fmt: v => v === 64 ? 'C' : v < 64 ? 'L' + Math.round((64 - v) / 64 * 100) : 'R' + Math.round((v - 64) / 63 * 100)},
  {cc: 87,  key: 'ara', lab: 'ARP A',   tip: 'Arp interval A, 0–24 semitones (A alone = trill)',
   fmt: v => { const s = Math.round(v * 24 / 127); return s ? '+' + s + 'st' : 'OFF'; }},
  {cc: 88,  key: 'arb', lab: 'ARP B',   tip: 'Arp interval B, 0–24 semitones',
   fmt: v => { const s = Math.round(v * 24 / 127); return s ? '+' + s + 'st' : 'OFF'; }},
  {cc: 89,  key: 'ars', lab: 'ARP SPD', tip: 'Arp / trill speed, 4–60 steps per second',
   fmt: v => (4 * 15 ** (v / 127)).toFixed(1) + '/s'},
  {cc: 90,  key: 'sld', lab: 'SLIDE',   tip: 'Pitch slide from each note start: 64 = none, 0/127 = -/+24 st',
   fmt: v => { const s = Math.round((v - 64) / 64 * 24); return s ? (s > 0 ? '+' : '') + s + 'st' : 'OFF'; }},
  {cc: 110, key: 'slt', lab: 'SLIDE T', tip: 'Time to reach the full slide, 10 ms – 2 s',
   fmt: v => _msTxt(0.01 * 200 ** (v / 127))},
  {cc: 111, key: 'trd', lab: 'TREMOLO', tip: 'Tremolo depth: 0 = off',
   fmt: v => v ? Math.round(v / 127 * 100) + '%' : 'OFF'},
  {cc: 112, key: 'trr', lab: 'TREM RT', tip: 'Tremolo rate, 0.5–20 Hz',
   fmt: v => (0.5 * 40 ** (v / 127)).toFixed(1) + 'Hz'},
  {cc: 113, key: 'rtg', lab: 'RETRIG',  tip: 'Re-strike the held note 1–40 times a second (0 = off)',
   fmt: v => v ? (40 ** ((v - 1) / 126)).toFixed(1) + '/s' : 'OFF'},
];
const _MOD_VOICES = ['p1', 'p2', 'tri', 'ns', 'gb'];

function modCC(voice, cc, value, vid, fmt) {
  const el = document.getElementById(vid);
  if (el) el.textContent = fmt ? fmt(value) : value;
  cmd('SYNTH_CC', {ch: _voiceCh(voice), cc, value});
}

function _buildModRows() {
  document.querySelectorAll('.mods-slot').forEach(slot => {
    const v = slot.dataset.voice;
    const rows = _MOD_ROWS.map((r, i) => {
      const def = _MOD_DEF[r.cc];
      return `<div class="prow">
        <span class="plab" title="${r.tip}">${r.lab}</span>
        <input type="range" min="0" max="127" value="${def}" id="${v}-m-${r.key}"
               oninput="modCC('${v}',${r.cc},+this.value,'${v}-mv-${r.key}',_MOD_ROWS[${i}].fmt)">
        <span class="pval" id="${v}-mv-${r.key}">${r.fmt(def)}</span>
      </div>`;
    }).join('');
    slot.innerHTML = `<details class="voice-panel" style="margin-top:12px">
        <summary class="voice-hdr"><span class="voice-name">PERFORMANCE MODS</span></summary>
        <div class="param-list" style="margin-top:8px">${rows}</div>
        <div style="font-size:9px;color:var(--label);margin-top:4px">
          CC 5 glide · 86 pan · 87/88 arp · 89 arp speed · 90/110 slide / slide time · 111/112 tremolo / rate · 113 retrigger.
          Sent on this voice's channel, so any controller can drive them.
        </div>
      </details>`;
  });
}

function _modSlides() {
  const out = [];
  for (const v of _MOD_VOICES)
    for (const r of _MOD_ROWS)
      out.push({ch: _voiceCh(v), cc: r.cc, id: `${v}-m-${r.key}`, vid: `${v}-mv-${r.key}`, fmt: r.fmt});
  return out;
}

// Noise key split (CC 114): readout as a note name.
const _NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
function _splitFmt(v) { return v ? _NOTE_NAMES[v % 12] + (Math.floor(v / 12) - 1) + '+' : 'OFF'; }
function _splitLabel(v) {
  const el = document.getElementById('ns-v-split');
  if (el) el.textContent = _splitFmt(v);
}

// ── GB wave SHAPE: 8 harmonic knobs (CC 102-109) ─────────────────────────────
// The chip host rebuilds the 32-step table from the knobs (sum of sines,
// normalised to 0-15) and plays it as the custom table. The page mirrors the
// same maths (orc1_synth._gb_harmonic_table) so the canvas shows the new
// shape at once.
const _GB_HARM_CC0 = 102, _GB_HARM_N = 8, _GB_STORE_CC = 115, _GB_N_SLOTS = 16;
let _harm = new Array(_GB_HARM_N).fill(0);

function _harmTable(levels) {
  const s = new Array(WAVE_STEPS).fill(0);
  levels.forEach((lv, k) => {
    if (!lv) return;
    for (let i = 0; i < WAVE_STEPS; i++) s[i] += (lv / 127) * Math.sin(2 * Math.PI * (k + 1) * i / WAVE_STEPS);
  });
  const lo = Math.min(...s), hi = Math.max(...s);
  if (hi - lo < 1e-9) return new Array(WAVE_STEPS).fill(8);
  // numpy rounds half to even; match it so the canvas equals the synth table
  const rnd = x => { const f = Math.floor(x), d = x - f; return d > 0.5 || (d === 0.5 && f % 2) ? f + 1 : f; };
  return s.map(x => rnd((x - lo) / (hi - lo) * 15));
}

function _buildHarmRows() {
  const host = document.getElementById('gb-harm');
  if (!host) return;
  host.innerHTML = Array.from({length: _GB_HARM_N}, (_, k) => `<div class="prow">
      <span class="plab" title="Level of harmonic ${k + 1} (CC ${_GB_HARM_CC0 + k})">H${k + 1}</span>
      <input type="range" min="0" max="127" value="0" id="gb-h${k}" oninput="setHarm(${k},+this.value)">
      <span class="pval" id="gb-hv${k}">0</span>
    </div>`).join('');
}

function _harmSlides() {
  return Array.from({length: _GB_HARM_N}, (_, k) =>
    ({ch: CH_GB_WAVE, cc: _GB_HARM_CC0 + k, id: `gb-h${k}`, vid: `gb-hv${k}`}));
}

function setHarm(k, value) {
  _harm[k] = value;
  _gbWaveCC[_GB_HARM_CC0 + k] = value;
  onCC(CH_GB_WAVE, _GB_HARM_CC0 + k, value, `gb-hv${k}`);
  // Mirror the synth: the shaped table is now the custom table, playing.
  _waveRam = _harmTable(_harm);
  _waveTablePushed = true;           // the synth already holds exactly this
  _setWaveCustomUI(true);
}

// ── GB wave SLOTS: PC 0-15 recall; STORE (CC 115 held) + PC = save ───────────
let _slots = new Array(_GB_N_SLOTS).fill(null);
let _storeArmed = false;

function _renderSlots() {
  const host = document.getElementById('gb-slots');
  if (!host) return;
  host.innerHTML = _slots.map((t, i) =>
    `<button class="bank-btn" style="${t || _storeArmed ? '' : 'opacity:.35'}" onclick="gbSlot(${i})"
       title="Slot ${i + 1}${t ? (_storeArmed ? ' — tap to OVERWRITE' : ' — tap to recall') : (_storeArmed ? ' — tap to save here' : ' (empty)')}">${i + 1}</button>`).join('');
  const b = document.getElementById('gb-store-btn');
  if (b) b.classList.toggle('active', _storeArmed);
}

function gbStoreArm() {
  _storeArmed = !_storeArmed;
  _renderSlots();
}

async function gbSlot(i) {
  if (_storeArmed) {
    // STORE is a momentary CC on the synth: hold it (127) across the PC.
    await cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: _GB_STORE_CC, value: 127});
    await cmd('SYNTH_PC', {ch: CH_GB_WAVE, program: i});
    await cmd('SYNTH_CC', {ch: CH_GB_WAVE, cc: _GB_STORE_CC, value: 0});
    _slots[i] = _waveRam.slice();
    _storeArmed = false;
    _renderSlots();
    return;
  }
  if (!_slots[i]) return;            // the synth ignores an empty slot too
  await cmd('SYNTH_PC', {ch: CH_GB_WAVE, program: i});
  _waveRam = _slots[i].slice();
  _waveTablePushed = true;
  _setWaveCustomUI(true);
}

// The chip host's wave state (~/.orc1_gb_wave.json): slots, the custom table
// and the harmonic knob positions -- none of which are in the per-CC patch file.
async function loadGbWave() {
  try {
    const r = await fetch('/api/gb_wave');
    if (!r.ok) return;
    const d = await r.json();
    const okTable = t => Array.isArray(t) && t.length === WAVE_STEPS;
    if (Array.isArray(d.slots))
      _slots = Array.from({length: _GB_N_SLOTS}, (_, i) => okTable(d.slots[i]) ? d.slots[i].slice() : null);
    if (Array.isArray(d.harm) && d.harm.length === _GB_HARM_N) {
      _harm = d.harm.map(v => +v || 0);
      _gbWaveCC = {};
      _harm.forEach((v, k) => { _gbWaveCC[_GB_HARM_CC0 + k] = v; });
      _remapSaved();
    }
    if (okTable(d.custom)) {
      _waveRam = d.custom.slice();
      _waveTablePushed = true;       // the canvas now equals the synth's custom table
    }
    if ('use_custom' in d) _setWaveCustomUI(!!d.use_custom);
    else _waveDraw();
    _renderSlots();
    const st = ORC.last();
    if (st) _render(st);
  } catch (e) {}
}

_buildModRows();
_buildHarmRows();
_renderSlots();

// ── Poll integration ──────────────────────────────────────────────────────────
// Sliders that just track a single (ch, cc) — rebuilt when channels change
function _buildSlides() {
  return [
    // P1
    {ch:CH_CHIP,    cc:8,  id:'p1-atk', vid:'p1-v-atk'},
    {ch:CH_CHIP,    cc:9,  id:'p1-dec', vid:'p1-v-dec'},
    {ch:CH_CHIP,    cc:10, id:'p1-sus', vid:'p1-v-sus'},
    {ch:CH_CHIP,    cc:11, id:'p1-rel', vid:'p1-v-rel'},
    {ch:CH_CHIP,    cc:13, id:'p1-lvl', vid:'p1-v-lvl'},
    {ch:CH_CHIP,    cc:74, id:'p1-cut', vid:'p1-v-cut'},
    {ch:CH_CHIP,    cc:71, id:'p1-res', vid:'p1-v-res'},
    {ch:CH_CHIP,    cc:75, id:'p1-oct', vid:'p1-v-oct'},
    // P2
    {ch:CH_CHIP_P2, cc:8,  id:'p2-atk', vid:'p2-v-atk'},
    {ch:CH_CHIP_P2, cc:9,  id:'p2-dec', vid:'p2-v-dec'},
    {ch:CH_CHIP_P2, cc:10, id:'p2-sus', vid:'p2-v-sus'},
    {ch:CH_CHIP_P2, cc:11, id:'p2-rel', vid:'p2-v-rel'},
    {ch:CH_CHIP_P2, cc:13, id:'p2-lvl', vid:'p2-v-lvl'},
    {ch:CH_CHIP_P2, cc:74, id:'p2-cut', vid:'p2-v-cut'},
    {ch:CH_CHIP_P2, cc:71, id:'p2-res', vid:'p2-v-res'},
    {ch:CH_CHIP_P2, cc:75, id:'p2-oct', vid:'p2-v-oct'},
    // TRI
    {ch:CH_CHIP_TRI, cc:13, id:'tri-lvl', vid:'tri-v-lvl'},
    {ch:CH_CHIP_TRI, cc:74, id:'tri-cut', vid:'tri-v-cut'},
    {ch:CH_CHIP_TRI, cc:71, id:'tri-res', vid:'tri-v-res'},
    {ch:CH_CHIP_TRI, cc:75, id:'tri-oct', vid:'tri-v-oct'},
    // NOISE
    {ch:CH_CHIP_NS, cc:8,  id:'ns-atk', vid:'ns-v-atk'},
    {ch:CH_CHIP_NS, cc:9,  id:'ns-dec', vid:'ns-v-dec'},
    {ch:CH_CHIP_NS, cc:10, id:'ns-sus', vid:'ns-v-sus'},
    {ch:CH_CHIP_NS, cc:11, id:'ns-rel', vid:'ns-v-rel'},
    {ch:CH_CHIP_NS, cc:13, id:'ns-lvl', vid:'ns-v-lvl'},
    // GB
    {ch:CH_GB_WAVE, cc:7,  id:'gb-vol', vid:'gb-v-vol'},
    {ch:CH_GB_WAVE, cc:8,  id:'gb-atk', vid:'gb-v-atk'},
    {ch:CH_GB_WAVE, cc:9,  id:'gb-dec', vid:'gb-v-dec'},
    {ch:CH_GB_WAVE, cc:10, id:'gb-sus', vid:'gb-v-sus'},
    {ch:CH_GB_WAVE, cc:11, id:'gb-rel', vid:'gb-v-rel'},
    // P1+P2 link (sent on CH_CHIP)
    {ch:CH_CHIP, cc:73, id:'sl-detune', vid:'v-detune'},
    // Vibrato depth/rate (GB expansion) — plain 0-127 CCs, one pair per voice.
    // (BEND sliders are deliberately NOT here: they're momentary and always
    //  spring back to center, so there's no state to sync from the poll.)
    {ch:CH_CHIP,    cc:77, id:'p1-vibd', vid:'p1-v-vibd'},
    {ch:CH_CHIP,    cc:78, id:'p1-vibr', vid:'p1-v-vibr'},
    {ch:CH_CHIP_P2, cc:77, id:'p2-vibd', vid:'p2-v-vibd'},
    {ch:CH_CHIP_P2, cc:78, id:'p2-vibr', vid:'p2-v-vibr'},
    {ch:CH_GB_WAVE, cc:77, id:'gb-vibd', vid:'gb-v-vibd'},
    {ch:CH_GB_WAVE, cc:78, id:'gb-vibr', vid:'gb-v-vibr'},
    {ch:CH_CHIP_NS, cc:114, id:'ns-split', vid:'ns-v-split', fmt:_splitFmt},
  ].concat(_modSlides(), _harmSlides());
}
let _SLIDES = _buildSlides();

function _render(state) {
  _followBindings(state);
  _synthCC = state.synth_cc || {};
  // Check bus status once on first poll (synth must be running for jack_lsp to work)

  // Sliders
  _SLIDES.forEach(({ch, cc, id, vid, fmt}) => {
    const el = document.getElementById(id);
    if (!el || el === document.activeElement) return;
    const v = _ccVal(ch, cc);
    if (v === undefined) return;
    el.value = v;
    const vd = vid && document.getElementById(vid);
    if (vd) vd.textContent = fmt ? fmt(v) : v;
  });

  // Duty (P1/P2) — select by data-voice, not data-ch
  [['p1', CH_CHIP], ['p2', CH_CHIP_P2]].forEach(([voice, ch]) => {
    const v = _ccVal(ch, 1);
    if (v !== undefined) {
      const s = _dutySnap(v);
      document.querySelectorAll(`.duty-btn[data-voice="${voice}"]`).forEach(b =>
        b.classList.toggle('active', +b.dataset.val === s));
    }
  });

  // Noise mode
  const nv = _ccVal(CH_CHIP_NS, 3);
  if (nv !== undefined) {
    const s = _noiseModeSnap(nv);
    document.querySelectorAll('.noise-mode-btn').forEach(b =>
      b.classList.toggle('active', +b.dataset.val === s));
  }

  // GB Wave bank
  const gv = _ccVal(CH_GB_WAVE, 1);
  if (gv !== undefined) {
    const s = _gbBankSnap(gv);
    document.querySelectorAll('.bank-btn').forEach(b =>
      b.classList.toggle('active', +b.dataset.val === s));
  }

  // GB hardware-accuracy switches (CC 22 volume mode, CC 24 retrigger mode)
  const qv = _ccVal(CH_GB_WAVE, 22);
  if (qv !== undefined) _gbSwitchUI('gb-volq-btn', 'gb-volc-btn', qv >= 64);
  const rv = _ccVal(CH_GB_WAVE, 24);
  if (rv !== undefined) _gbSwitchUI('gb-hardrt-btn', 'gb-softrt-btn', rv >= 64);

  // Couple mode
  const cv = _ccVal(CH_CHIP, 72);
  if (cv !== undefined) {
    const s = _coupleSnap(cv);
    document.querySelectorAll('.couple-btn').forEach(b =>
      b.classList.toggle('active', +b.dataset.val === s));
  }

  // Stereo spread
  const sv = _ccVal(CH_CHIP, 76);
  if (sv !== undefined) {
    _spreadOn = sv >= 64;
    const btn = document.getElementById('spread-btn');
    if (btn) {
      btn.textContent = _spreadOn ? 'ON' : 'OFF';
      btn.classList.toggle('active', _spreadOn);
    }
  }

  // Pulse anti-alias (persisted, so it restores from the saved chip params).
  const av = _ccVal(CH_CHIP, 85);
  _aaOn = (av !== undefined) && av >= 64;
  {
    const btn = document.getElementById('aa-btn');
    if (btn) {
      btn.textContent = _aaOn ? 'ON' : 'OFF';
      btn.classList.toggle('active', _aaOn);
    }
  }

  // Sweep TIME/SHIFT (GB expansion): CC stores a 0-127 band midpoint, the
  // slider shows the hardware 0-7 value — decode with floor(cc/16).
  [['sw-time', 79, 'sw-v-time', true], ['sw-shift', 80, 'sw-v-shift', false]]
    .forEach(([id, cc, vid, isTime]) => {
      const el = document.getElementById(id);
      if (!el || el === document.activeElement) return;
      const v = _ccVal(CH_CHIP, cc);
      if (v === undefined) return;
      const v7 = Math.min(7, Math.floor(v / 16));
      el.value = v7;
      const lab = document.getElementById(vid);
      if (lab) lab.textContent = (isTime && v7 === 0) ? 'OFF' : v7;
    });

  // Sweep direction button (CC81: 0-63 = up, 64-127 = down)
  const swd = _ccVal(CH_CHIP, 81);
  if (swd !== undefined) {
    _sweepDown = swd >= 64;
    _renderSweepDir();
  }
  // NOTE: custom-wave mode (CC23) is deliberately NOT synced from the poll —
  // picking a preset reverts the synth via CC1 without touching the stored
  // CC23 value, so the stale 127 would wrongly flip the UI back to CUSTOM.
  // The buttons + setGBWave() keep the local UI state correct instead.

}
ORC.subscribe(_render);

// ── MIDI channel pickers ──────────────────────────────────────────────────────
// ── Channels = the live /matrix bindings (2026-09-24) ────────────────────────
// Was the static cfg SYNTH.CH_* keys, which went stale once modules moved on
// /matrix: the P2 card drove the POLY synth (poly bound to 6, cfg P2 = 6).
const _MOD_OF = {p1: 'chip_p1', p2: 'chip_p2', tri: 'chip_tri', ns: 'chip_noise',
                 gb: 'gb_wave', pcm: 'chip_pcm'};
const _TAG_OF = {p1: 'ch-tag-chip', p2: 'ch-tag-chip-p2', tri: 'ch-tag-chip-tri',
                 ns: 'ch-tag-chip-ns', gb: 'ch-tag-chip-gb'};
function _followBindings(state) {
  const got = {};
  for (const [v, m] of Object.entries(_MOD_OF)) {
    const ch = ORC.boundCh(m, state);
    if (ch === undefined) return;          // no bindings in this state yet
    got[v] = ch;
  }
  const changed = got.p1 !== CH_CHIP || got.p2 !== CH_CHIP_P2 || got.tri !== CH_CHIP_TRI ||
                  got.ns !== CH_CHIP_NS || got.gb !== CH_GB_WAVE || got.pcm !== CH_CHIP_PCM;
  CH_CHIP = got.p1; CH_CHIP_P2 = got.p2; CH_CHIP_TRI = got.tri;
  CH_CHIP_NS = got.ns; CH_GB_WAVE = got.gb; CH_CHIP_PCM = got.pcm;
  for (const [v, id] of Object.entries(_TAG_OF)) {
    const tag = document.getElementById(id);
    if (tag) tag.textContent = got[v] ? 'CH ' + got[v] : 'NOT BOUND';
  }
  if (changed) {
    _rebuildDef();
    _SLIDES = _buildSlides();
    _remapSaved();
  }
}

// Bend range for the BEND readout (± semitones, SYNTH.CHIP_BEND_RANGE).
async function _loadVoiceCh() {
  try {
    const r = await fetch('/api/config');
    if (!r.ok) return;
    const d = await r.json();
    for (const e of d.schema)
      if (e.key === 'SYNTH.CHIP_BEND_RANGE' && e.current != null) BEND_RANGE = +e.current || 12;
  } catch (e) {}
  _followBindings(ORC.last());
}

// Load persisted params AFTER the channel config resolves (voice channels may
// be remapped and _voiceCh()/_DEF depend on them), then re-render the last
// polled state so saved values show without waiting a poll cycle.
async function loadSavedParams() {
  try {
    const r = await fetch('/api/chip_params');
    if (!r.ok) return;
    _savedRaw = (await r.json()) || {};   // {synthVoiceName: {cc: val}}
    _remapSaved();
    const st = ORC.last();
    if (st) _render(st);
  } catch (e) {}
}
_loadVoiceCh().then(loadSavedParams).then(loadGbWave);
