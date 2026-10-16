// ORC-1 MONO page — layered monophonic synth (ORC1-Mono host).
'use strict';

// The mono host is bound to a channel in the module matrix (~/.orc1_bindings.json:
// "3":"mono"), NOT via a cfg.SYNTH channel key, so there is no /api/config entry
// to resolve it from. Keep this in step with the live binding if mono is rebound.
let MONO_CH = 3;   // default; the live /matrix binding wins (ORC.boundCh, each poll)

// CC map (from MonoSynth.set_cc in orc1_synth.py):
//   1 L0 wave · 5 glide · 8/9/10/11 ADSR · 12/14/15 ring depth/ratio/carrier
//  13 level · 19 L0 gain · 20-24 L1 on/semi/detune/wave/gain
//  25-29 L2 on/semi/detune/wave/gain · 70/71/74 filter mode/res/cutoff · 75 octave
// These defaults are cosmetic initial slider positions (the engine owns the real
// boot defaults); overridden by synth_cc and by the saved-params file below.
const MONO_DEF = {
  1:48, 5:0, 8:4, 9:40, 10:89, 11:30, 12:0, 13:64, 14:64, 15:0, 19:64,
  20:0, 21:64, 22:64, 23:48, 24:64, 25:0, 26:64, 27:64, 28:48, 29:64,
  70:21, 71:0, 74:100, 75:64,
};

let _synthCC = {};
// Persisted mono params from ~/.orc1_mono_params.json (flat {cc: val}). Fallback
// layer BETWEEN synth_cc (web-set CCs only) and MONO_DEF — after a restart the
// host loads its patch from that FILE, not a web CC, so synth_cc is empty and the
// page would show boot defaults. Same fix as /synth, /chip, /drums. Level CC13 is
// not in the file (owned by the bus-levels system), so it falls back to MONO_DEF.
let _savedParamsCC = {};

function _ccVal(cc) {
  const c = _synthCC[String(MONO_CH)];
  if (c && c[String(cc)] !== undefined) return c[String(cc)];
  const s = _savedParamsCC[String(MONO_CH)];
  if (s && s[String(cc)] !== undefined) return s[String(cc)];
  return MONO_DEF[cc];
}

// ── waveform (0-31 sine, 32-63 saw, 64-95 square, 96-127 triangle) ────────────
const _WAVE_CC_CLASS = {1: 'L0', 23: 'L1', 28: 'L2'};
function _wvSnap(v) { return v < 32 ? 16 : v < 64 ? 48 : v < 96 ? 80 : 112; }

function setWave(cc, val) {
  const cls = _WAVE_CC_CLASS[cc];
  document.querySelectorAll('.wave-btn.' + cls).forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: MONO_CH, cc, value: val});
}

// ── layer on/off (L1 = CC20, L2 = CC25; value >= 64 is ON) ────────────────────
function toggleLayer(layer) {
  const cc = layer === 1 ? 20 : 25;
  const on = _ccVal(cc) >= 64;
  const val = on ? 0 : 127;
  cmd('SYNTH_CC', {ch: MONO_CH, cc, value: val});
  // optimistic: reflect immediately (poll will confirm)
  if (!_synthCC[String(MONO_CH)]) _synthCC[String(MONO_CH)] = {};
  _synthCC[String(MONO_CH)][String(cc)] = val;
  _paintLayerTog(layer, val >= 64);
}

function _paintLayerTog(layer, on) {
  const b = document.getElementById('tog-l' + layer);
  if (!b) return;
  b.textContent = on ? 'ON' : 'OFF';
  b.classList.toggle('active', on);
}

// ── filter mode / octave ──────────────────────────────────────────────────────
function _fmSnap(v) { return v < 43 ? 21 : v < 85 ? 64 : 106; }
function setFilterMode(val) {
  document.querySelectorAll('.fmode-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: MONO_CH, cc: 70, value: val});
}
function _octSnap(v) { return v < 16 ? 0 : v < 48 ? 32 : v < 80 ? 64 : v < 112 ? 96 : 127; }
function setOctave(val) {
  document.querySelectorAll('.oct-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: MONO_CH, cc: 75, value: val});
}

// ── sliders ───────────────────────────────────────────────────────────────────
function onCC(ch, cc, value, valId) {
  if (valId) document.getElementById(valId).textContent = value;
  cmd('SYNTH_CC', {ch, cc, value});
}
// SEMI: value 0-127 -> round(v/127*48)-24 semitones (matches set_cc)
function _semiOf(v) { return Math.round(v / 127 * 48) - 24; }
function onSemi(cc, value, valId) {
  const s = _semiOf(value);
  if (valId) document.getElementById(valId).textContent = (s > 0 ? '+' : '') + s;
  cmd('SYNTH_CC', {ch: MONO_CH, cc, value});
}
// DETUNE: value 0-127 -> round(v/127*100 - 50) cents (matches set_cc)
function _centsOf(v) { return Math.round(v / 127 * 100 - 50); }
function onCents(cc, value, valId) {
  const c = _centsOf(value);
  if (valId) document.getElementById(valId).textContent = (c > 0 ? '+' : '') + c + '¢';
  cmd('SYNTH_CC', {ch: MONO_CH, cc, value});
}

// ── audition keyboard (one octave, C4..C5) ────────────────────────────────────
function audition(note) { cmd('SYNTH_NOTE', {ch: MONO_CH, note, vel: 100, ms: 600}); }
(function buildKbd() {
  const kbd = document.getElementById('kbd');
  if (!kbd) return;
  const names = ['C', 'D', 'E', 'F', 'G', 'A', 'B', 'C'];
  const notes = [60, 62, 64, 65, 67, 69, 71, 72];
  notes.forEach((n, i) => {
    const b = document.createElement('button');
    b.className = 'btn';
    b.textContent = names[i];
    b.onclick = () => audition(n);
    kbd.appendChild(b);
  });
})();

// ── render from polled state (focus-guarded) ──────────────────────────────────
const _SLIDERS = [
  {cc:5,  id:'sl-gld', vid:'v-gld'},
  {cc:8,  id:'sl-atk', vid:'v-atk'},
  {cc:9,  id:'sl-dec', vid:'v-dec'},
  {cc:10, id:'sl-sus', vid:'v-sus'},
  {cc:11, id:'sl-rel', vid:'v-rel'},
  {cc:12, id:'sl-rmd', vid:'v-rmd'},
  {cc:14, id:'sl-rmr', vid:'v-rmr'},
  {cc:15, id:'sl-rmh', vid:'v-rmh'},
  {cc:13, id:'sl-lvl', vid:'v-lvl'},
  {cc:74, id:'sl-cut', vid:'v-cut'},
  {cc:71, id:'sl-res', vid:'v-res'},
  {cc:19, id:'sl-l0g', vid:'v-l0g'},
  {cc:24, id:'sl-l1g', vid:'v-l1g'},
  {cc:29, id:'sl-l2g', vid:'v-l2g'},
];
// semitone / cents sliders need a formatted label
const _SEMI = [{cc:21, id:'sl-l1s', vid:'v-l1s'}, {cc:26, id:'sl-l2s', vid:'v-l2s'}];
const _CENTS = [{cc:22, id:'sl-l1d', vid:'v-l1d'}, {cc:27, id:'sl-l2d', vid:'v-l2d'}];

// Channel = the live /matrix binding (2026-09-24), like /synth /chip /drums.
function _followBinding(state) {
  const ch = ORC.boundCh('mono', state);
  if (ch === undefined) return;
  if (ch !== MONO_CH) {
    const saved = _savedParamsCC[String(MONO_CH)];
    MONO_CH = ch;
    _savedParamsCC = saved ? {[String(ch)]: saved} : {};
  }
  const tag = document.getElementById('ch-tag-mono');
  if (tag) tag.textContent = ch ? 'CH ' + ch : 'NOT BOUND';
}

function _render(state) {
  _followBinding(state);
  _synthCC = state.synth_cc || {};

  _SLIDERS.forEach(({cc, id, vid}) => {
    const el = document.getElementById(id);
    if (!el || el === document.activeElement) return;
    const v = _ccVal(cc);
    el.value = v;
    if (vid) document.getElementById(vid).textContent = v;
  });
  _SEMI.forEach(({cc, id, vid}) => {
    const el = document.getElementById(id);
    if (!el || el === document.activeElement) return;
    const v = _ccVal(cc); el.value = v;
    const s = _semiOf(v);
    document.getElementById(vid).textContent = (s > 0 ? '+' : '') + s;
  });
  _CENTS.forEach(({cc, id, vid}) => {
    const el = document.getElementById(id);
    if (!el || el === document.activeElement) return;
    const v = _ccVal(cc); el.value = v;
    const c = _centsOf(v);
    document.getElementById(vid).textContent = (c > 0 ? '+' : '') + c + '¢';
  });

  // waveform button groups
  [[1,'L0'],[23,'L1'],[28,'L2']].forEach(([cc, cls]) => {
    const wvS = _wvSnap(_ccVal(cc));
    document.querySelectorAll('.wave-btn.' + cls).forEach(b =>
      b.classList.toggle('active', +b.dataset.val === wvS));
  });
  // layer on/off
  _paintLayerTog(1, _ccVal(20) >= 64);
  _paintLayerTog(2, _ccVal(25) >= 64);
  // filter mode + octave
  const fmS = _fmSnap(_ccVal(70));
  document.querySelectorAll('.fmode-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === fmS));
  const octS = _octSnap(_ccVal(75));
  document.querySelectorAll('.oct-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === octS));
}

async function loadSavedParams() {
  try {
    const r = await fetch('/api/mono_params');
    if (!r.ok) return;
    const data = await r.json();          // flat {cc: val}
    if (data && typeof data === 'object')
      _savedParamsCC = {[String(MONO_CH)]: data};
    const st = ORC.last();
    if (st) _render(st);
  } catch (e) {}
}
loadSavedParams();
ORC.subscribe(_render);
