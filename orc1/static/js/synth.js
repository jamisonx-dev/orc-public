// ORC-1 SYNTH page — poly voice (CH 8)
'use strict';

// ── defaults (CC values, used when engine hasn't sent anything yet) ────────────
// Poly synth defaults: waveform=saw(48), atk≈5ms, dec≈200ms, sus=70%, rel≈300ms,
// level=50%, cutoff=open, res=0, mode=LP, octave=0
let POLY_CH = 0;   // set from the live /matrix binding (ORC.boundCh) on the first poll
const POLY_DEF = {
  1: 48,   // waveform: saw (32-63 range)
  8: 6,    // attack: ~5ms
  9: 64,   // decay:  ~200ms
  10: 89,  // sustain: 70%
  11: 78,  // release: ~300ms
  13: 64,  // level: 50%
  74: 64,  // cutoff: mid
  71: 0,   // resonance: off
  70: 21,  // filter mode: LP (0-42)
  75: 64,  // octave: 0
};

let _synthCC = {};
// Persisted poly params from ~/.orc1_poly_params.json (flat {cc: val}), keyed
// here by POLY_CH. A fallback layer BETWEEN synth_cc (web-set CCs only) and
// POLY_DEF: after a restart the synth loads its patch from that FILE, not via a
// web CC, so synth_cc is empty and the page would show boot defaults instead of
// the real saved state. Same fix as /drums (2026-08-13). Master CC13 is not in
// the file (owned by the bus-levels system), so it still falls back to POLY_DEF.
let _savedParamsCC = {};

function _ccVal(ch, cc) {
  const c = _synthCC[String(ch)];
  if (c && c[String(cc)] !== undefined) return c[String(cc)];
  const s = _savedParamsCC[String(ch)];
  if (s && s[String(cc)] !== undefined) return s[String(cc)];
  return POLY_DEF[cc];
}

// ── Waveform ─────────────────────────────────────────────────────────────────
// Poly: 0-31=sine, 32-63=saw, 64-95=square, 96-127=triangle
function setWaveform(val) {
  document.querySelectorAll('.wave-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: POLY_CH, cc: 1, value: val});
}

function _wvSnap(v) {
  return v < 32 ? 16 : v < 64 ? 48 : v < 96 ? 80 : 112;
}

// ── Filter mode ───────────────────────────────────────────────────────────────
// _cc_to_mode: 0-42=LP, 43-84=HP, 85-127=BP; midpoints 21/64/106
function setFilterMode(val) {
  document.querySelectorAll('.fmode-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: POLY_CH, cc: 70, value: val});
}

function _fmSnap(v) {
  return v < 43 ? 21 : v < 85 ? 64 : 106;
}

// ── Octave ────────────────────────────────────────────────────────────────────
// _cc_to_octave: 0=−2, ~32=−1, ~64=0, ~96=+1, 127=+2
function setOctave(val) {
  document.querySelectorAll('.oct-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === val));
  cmd('SYNTH_CC', {ch: POLY_CH, cc: 75, value: val});
}

function _octSnap(v) {
  return v < 16 ? 0 : v < 48 ? 32 : v < 80 ? 64 : v < 112 ? 96 : 127;
}

// ── Generic slider → SYNTH_CC ─────────────────────────────────────────────────
function onCC(ch, cc, value, valId) {
  if (valId) document.getElementById(valId).textContent = value;
  cmd('SYNTH_CC', {ch, cc, value});
}

// ── Audition ──────────────────────────────────────────────────────────────────
function audition(ch, note) {
  cmd('SYNTH_NOTE', {ch, note, vel: 100, ms: 500});
}

// ── State poll integration ─────────────────────────────────────────────────────
const _SLIDERS = [
  {cc:8,  id:'sl-atk', vid:'v-atk'},
  {cc:9,  id:'sl-dec', vid:'v-dec'},
  {cc:10, id:'sl-sus', vid:'v-sus'},
  {cc:11, id:'sl-rel', vid:'v-rel'},
  {cc:13, id:'sl-lvl', vid:'v-lvl'},
  {cc:74, id:'sl-cut', vid:'v-cut'},
  {cc:71, id:'sl-res', vid:'v-res'},
];

// ── MIDI channel pickers ──────────────────────────────────────────────────────
// ── Channel = the live /matrix binding (2026-09-24) ──────────────────────────
// Was the static cfg SYNTH.CH_POLY (8), which went stale when poly moved on
// /matrix: the page then drove chip P2 (bound to 8) instead of the poly.
function _followBinding(state) {
  const ch = ORC.boundCh('poly', state);
  if (ch === undefined) return;
  if (ch !== POLY_CH) {
    const saved = _savedParamsCC[String(POLY_CH)];
    POLY_CH = ch;
    _savedParamsCC = saved ? {[String(ch)]: saved} : {};
  }
  const tag = document.getElementById('ch-tag-poly');
  if (tag) tag.textContent = ch ? 'CH ' + ch : 'NOT BOUND';
}
async function _loadVoiceCh() { _followBinding(ORC.last()); }

// Load persisted params AFTER the channel config resolves (POLY_CH may be
// remapped), then re-render the last polled state so saved values show without
// waiting a poll cycle.
async function loadSavedParams() {
  try {
    const r = await fetch('/api/poly_params');
    if (!r.ok) return;
    const data = await r.json();          // flat {cc: val}
    if (data && typeof data === 'object')
      _savedParamsCC = {[String(POLY_CH)]: data};
    const st = ORC.last();
    if (st) _render(st);
  } catch (e) {}
}
_loadVoiceCh().then(loadSavedParams);

function _render(state) {
  _followBinding(state);
  _synthCC = state.synth_cc || {};

  // Update sliders — skip if focused (focus guard)
  _SLIDERS.forEach(({cc, id, vid}) => {
    const el = document.getElementById(id);
    if (!el || el === document.activeElement) return;
    const v = _ccVal(POLY_CH, cc);
    el.value = v;
    if (vid) document.getElementById(vid).textContent = v;
  });

  // Waveform buttons
  const wv = _ccVal(POLY_CH, 1);
  const wvS = _wvSnap(wv);
  document.querySelectorAll('.wave-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === wvS));

  // Filter mode buttons
  const fm = _ccVal(POLY_CH, 70);
  const fmS = _fmSnap(fm);
  document.querySelectorAll('.fmode-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === fmS));

  // Octave buttons
  const oct = _ccVal(POLY_CH, 75);
  const octS = _octSnap(oct);
  document.querySelectorAll('.oct-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.val === octS));
}
ORC.subscribe(_render);
