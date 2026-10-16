// ORC-1 DRUMS page — 909 drum voices (CH 6)
'use strict';

// ── Data-driven layout ─────────────────────────────────────────────────────────
// The voice list + per-voice CC map come from /api/drum_layout (computed in
// orc1_config.DRUM_CC). Each voice is packed to exactly the params it uses, so
// the CC for a param is simply: voice.start + voice.params.indexOf(param).
// Redesign 2026-07-21: the HP band is gone and the sample slots moved to the
// sampler section (/sampler), so this page is drums-only now.

let DRUM_CH = 10;   // default; the live /matrix binding wins (ORC.boundCh, each poll)
let VOICES  = [];            // [{key,name,note,start,params}], from the endpoint

// param name -> slider label
const PLAB = {
  level: 'LEVEL', tune: 'TUNE', attack: 'ATTACK', decay: 'DECAY',
  filter_freq: 'FREQ', filter_res: 'RES',
};

let _synthCC = {};
// Persisted values from ~/.orc1_drum_params.json, converted to {ch: {cc: val}}
// so the state-poll handler can use them as a fallback when synth_cc is empty
// (e.g. after a restart that loaded params from the file rather than via a web CC).
let _savedParamsCC = {};

function _ccFor(voice, param) {
  const i = voice.params.indexOf(param);
  return (i < 0) ? -1 : voice.start + i;
}
function _ccVal(ch, cc) {
  const c = _synthCC[String(ch)];
  if (c && c[String(cc)] !== undefined) return c[String(cc)];
  const s = _savedParamsCC[String(ch)];
  return (s && s[String(cc)] !== undefined) ? s[String(cc)] : 64;
}

// ── Trigger ───────────────────────────────────────────────────────────────────
function triggerDrum(note) {
  cmd('SYNTH_NOTE', {ch: DRUM_CH, note, vel: 100, ms: 100});
}

// ── Slider ────────────────────────────────────────────────────────────────────
function onCC(ch, cc, value, valId) {
  if (valId) {
    const el = document.getElementById(valId);
    if (el) el.textContent = value;
  }
  cmd('SYNTH_CC', {ch, cc, value});
}

// ── Build panels ──────────────────────────────────────────────────────────────
function buildPanels() {
  const container = document.getElementById('drum-panels');
  if (!container) return;
  container.innerHTML = '';

  VOICES.forEach(voice => {
    const det = document.createElement('details');
    det.className = 'voice-panel';

    // One slider per param the voice actually exposes (toms have no filter,
    // clap has FREQ but no RES, cowbell now has both — all data-driven).
    const rows = voice.params.map((param, pi) => {
      const cc  = voice.start + pi;
      const id  = `d-${voice.key}-${param}`;
      const vid = `v-${voice.key}-${param}`;
      const lab = PLAB[param] || param.toUpperCase();
      return `<div class="prow">
        <span class="plab">${lab}</span>
        <input type="range" min="0" max="127" value="64" id="${id}"
               oninput="onCC(DRUM_CH,${cc},+this.value,'${vid}')">
        <span class="pval" id="${vid}">64</span>
      </div>`;
    }).join('');

    det.innerHTML = `
      <summary class="voice-hdr">
        <span class="voice-name">${voice.name}</span>
        <button class="trig-btn" onclick="event.stopPropagation();triggerDrum(${voice.note})">▶</button>
      </summary>
      <div class="voice-body">
        <div class="param-list" style="margin-top:10px">
          ${rows}
        </div>
      </div>`;

    container.appendChild(det);
  });
}

// ── State poll integration ─────────────────────────────────────────────────────
ORC.subscribe(function(state) {
  _followBinding(state);
  _synthCC = state.synth_cc || {};
  VOICES.forEach(voice => {
    voice.params.forEach((param, pi) => {
      const cc  = voice.start + pi;
      const id  = `d-${voice.key}-${param}`;
      const vid = `v-${voice.key}-${param}`;
      const el  = document.getElementById(id);
      if (!el || el === document.activeElement) return;
      const v = _ccVal(DRUM_CH, cc);
      el.value = v;
      const vd = document.getElementById(vid);
      if (vd) vd.textContent = v;
    });
  });
});

// ── Load persisted drum params → populate _savedParamsCC + seed sliders ────────
async function loadSavedParams() {
  try {
    const r = await fetch('/api/drum_params');
    if (!r.ok) return;
    const data = await r.json();
    const mapping = {};
    VOICES.forEach(voice => {
      const vd = data[voice.key];
      if (!vd) return;
      voice.params.forEach((param, pi) => {
        if (vd[param] !== undefined) mapping[voice.start + pi] = vd[param];
      });
    });
    _savedParamsCC = {[String(DRUM_CH)]: mapping};
    // Seed sliders immediately so they show real values before the first poll.
    VOICES.forEach(voice => {
      const vd = data[voice.key];
      if (!vd) return;
      voice.params.forEach((param, pi) => {
        if (vd[param] === undefined) return;
        const id  = `d-${voice.key}-${param}`;
        const vid = `v-${voice.key}-${param}`;
        const el  = document.getElementById(id);
        if (!el || el === document.activeElement) return;
        el.value = vd[param];
        const vdEl = document.getElementById(vid);
        if (vdEl) vdEl.textContent = vd[param];
      });
    });
  } catch(e) {}
}

// ── Load the data-driven layout, then build ─────────────────────────────────────
async function loadLayout() {
  try {
    const r = await fetch('/api/drum_layout');
    if (!r.ok) return;
    const d = await r.json();
    if (d.ch && ORC.boundCh('drums') === undefined) DRUM_CH = d.ch;
    VOICES = d.voices || [];
    buildPanels();
    loadSavedParams();  // seed sliders from persisted values once panels exist
  } catch (e) {}
}

// ── Channel = the live /matrix binding (2026-09-24) ──────────────────────────
// Was the static cfg SYNTH.CH_DRUM; the drum module can be moved on /matrix.
function _followBinding(state) {
  const ch = ORC.boundCh('drums', state);
  if (ch === undefined) return;
  if (ch !== DRUM_CH) {
    const saved = _savedParamsCC[String(DRUM_CH)];
    DRUM_CH = ch;
    _savedParamsCC = saved ? {[String(ch)]: saved} : {};
  }
  const tag = document.getElementById('ch-tag-drums');
  if (tag) tag.textContent = ch ? 'CH ' + ch : 'NOT BOUND';
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadLayout();

