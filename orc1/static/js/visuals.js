/* visuals.js — the /visuals page (HDMI line scenes; daw/orc_visuals.py).
   Show controls -> /api/visuals (visuals.sh). Every slider -> SYNTH_CC on the
   channel the "visuals" module is bound to (the same path a Fire knob or any
   controller takes); the renderer saves the positions, which /api/visuals
   reads back. The spectrum -> /api/visuals/audio/stream (SSE, ~25/s; polls
   /api/visuals/audio if the stream is refused); dragging on it sets the beat
   band (CC 32/33). SOURCE -> /api/visuals/source (which JACK ports the
   orc1-vistap tap listens to).
   Reorganised 2026-10-12 with owner: one KNOB table drives every slider,
   its label, hint and readout, and the MIDI list (the suite checks it against
   the renderer's KNOBS). */
'use strict';

// ── readouts: the renderer's own maths (orc_visuals.py), in real units ───────
const R = {
  x: (v, k) => '×' + (k ** ((v - 64) / 64)).toFixed(2),
  speed: v => '×' + (2 ** ((v - 64) / 32)).toFixed(2),
  size: v => '×' + (3 ** ((v - 64) / 64)).toFixed(2),
  angle: v => Math.round(180 * (v - 64) / 64) + '°',
  attack: v => v <= 0 ? 'instant' : Math.round(16 * (250 / 16) ** ((v - 1) / 126)) + ' ms',
  release: v => (0.08 * (1.5 / 0.08) ** (v / 127)).toFixed(2) + ' s',
  sens: v => '≥ ' + (3.0 - 1.7 * v / 127).toFixed(1) + '×',
  gate: v => v <= 0 ? 'off' : Math.round(-90 + 50 * Math.min(127, v) / 127) + ' dB',
  audio: v => v <= 0 ? 'off' : '×' + Math.min(2, (v / 64) ** 1.5).toFixed(2),
  hz: v => { const f = 0.02 * 200 ** (v / 127); return (f < 1 ? f.toFixed(2) : f.toFixed(1)) + ' Hz'; },
  bglfo: v => v <= 0 ? 'off' : (0.02 * 50 ** (v / 127)).toFixed(2) + ' Hz',
  pct: v => Math.round(v / 1.27) + '%',
  off: v => v <= 0 ? 'off' : Math.round(v / 1.27) + '%',
  mid: v => (v === 64 ? '0' : (v > 64 ? '+' : '') + Math.round((v - 64) / 0.64)),
};

// The knob table: key -> [cc, label, card group, hint, readout]. Group null =
// set by a picker / the spectrum, not a slider. MUST equal the renderer's
// KNOBS (_test_visuals checks every cc + key).
const KNOB = {
  audio:       [28, 'AUDIO', 'aud', 'how much the audio moves the picture', R.audio],
  sens:        [31, 'SENS', 'aud', 'a beat = the band jumps to this many times its last second', R.sens],
  sens2:       [61, 'SENS BG', 'aud', "the background band's own SENS (OWN BAND only)", R.sens],
  attack:      [29, 'ATTACK', 'aud', 'how fast a beat swells in (it always reaches full size)', R.attack],
  release:     [30, 'RELEASE', 'aud', 'how long a beat takes to fade', R.release],
  gate:        [54, 'GATE', 'aud', 'anything quieter is silence (hiss can\'t fire beats)', R.gate],
  band_lo:     [32, 'BEAT BAND LOW', null, '', null],
  band_hi:     [33, 'BEAT BAND HIGH', null, '', null],
  band2_lo:    [59, 'BACKGROUND BEAT BAND LOW', null, '', null],
  band2_hi:    [60, 'BACKGROUND BEAT BAND HIGH', null, '', null],
  band2_on:    [62, 'BACKGROUND OWN BAND', null, '', null],
  size:        [55, 'SIZE', 'lines', 'the whole picture, bigger or smaller', R.size],
  angle:       [56, 'ANGLE', 'lines', 'turn the whole picture', R.angle],
  speed:       [21, 'SPEED', 'lines', 'how fast the scene moves', R.speed],
  beat:        [26, 'BEAT', 'lines', 'the scene\'s own built-in kick reaction', R.mid],
  bright:      [25, 'BRIGHT', 'lines', '', R.mid],
  weight:      [53, 'WEIGHT', 'lines', 'line thickness; high = the overloaded glow', R.mid],
  colour:      [27, 'COLOUR', 'lines', 'shift the palette round the wheel', R.mid],
  trails:      [23, 'TRAILS', 'trails', 'how long the ghosts of past frames last', R.mid],
  zoom:        [24, 'TRAIL ZOOM', 'trails', 'ghosts get sucked in (left) or rush out (right)', R.mid],
  twist:       [22, 'TRAIL TWIST', 'trails', 'ghosts spin one way or the other', R.mid],
  lfo_rate:    [57, 'RATE', 'lfo', 'FREE only', R.hz],
  lfo_sync:    [58, 'MOVEMENT LFO SYNC', null, '', null],
  size_react:  [70, 'SIZE', 'mod', '', R.off], size_lfo:  [71, 'SIZE', 'mod', '', R.off],
  spin_react:  [72, 'ROTATION', 'mod', '', R.off], spin_lfo:  [73, 'ROTATION', 'mod', '', R.off],
  glow_react:  [74, 'BRIGHTNESS', 'mod', '', R.off], glow_lfo:  [75, 'BRIGHTNESS', 'mod', '', R.off],
  sway_react:  [76, 'SWAY', 'mod', '', R.off], sway_lfo:  [77, 'SWAY', 'mod', '', R.off],
  trail_react: [78, 'TRAILS', 'mod', '', R.off], trail_lfo: [79, 'TRAILS', 'mod', '', R.off],
  bg_pattern:  [34, 'BACKGROUND PATTERN', null, '', null],
  bg_set:      [50, 'BACKGROUND PATTERN SET', null, '', null],
  bg_level:    [36, 'LEVEL', 'bg-show', 'how much shows with nothing playing (0 = black)', R.off],
  bg_react:    [37, 'REACT', 'bg-show', 'how much beats, notes and loudness bring it in', R.mid],
  bg_lfo:      [38, 'LFO', 'bg-show', 'a slow breath on top', R.bglfo],
  bg_mode:     [35, 'BACKGROUND BEND MODE', null, '', null],
  bg_warp:     [39, 'WARP', 'bg-bend', 'how far the bands slide', R.pct],
  bg_freq:     [40, 'FREQ', 'bg-bend', 'how many waves down the screen', R.pct],
  bg_speed:    [41, 'WAVE SPEED', 'bg-bend', 'how fast the waves travel', R.pct],
  bg_scale:    [42, 'SCALE', 'bg-look', 'tile size', R.mid],
  bg_cycle:    [43, 'HUE CYCLE', 'bg-look', 'how fast its colours turn (0 = still)', R.off],
  mask_shape:  [44, 'PEEPHOLE SHAPE', null, '', null],
  mask_size:   [45, 'SIZE', 'mask', 'middle = about the screen height', R.mid],
  mask_soft:   [46, 'SOFT', 'mask', 'hard edge .. soft vignette', R.pct],
  mask_x:      [47, 'X', 'mask', '', R.mid],
  mask_y:      [48, 'Y', 'mask', '', R.mid],
  mask_fit:    [49, 'FIT', 'mask-fit', 'shrink the scene to sit inside the shape (0 = just cut it off)', R.pct],
  mask_room:   [51, 'ROOM', 'mask-fit', 'with FIT: space left round the scene for its trails to curl', R.pct],
  logo:        [52, 'LOGO', null, '', null],
};
const SCENE_CC = 20;
const GROUP_EL = { aud: 'aud-knobs', lines: 'lines-knobs', trails: 'trails-knobs', lfo: 'lfo-knobs',
                   'bg-show': 'bg-show-knobs', 'bg-bend': 'bg-bend-knobs', 'bg-look': 'bg-look-knobs',
                   mask: 'mask-knobs', 'mask-fit': 'mask-fit-knobs' };
const MOD_TARGETS = ['size', 'spin', 'glow', 'sway', 'trail'];
// the beat band(s): 1 = the shapes' (and the background's, unless it has its
// OWN BAND), 2 = the background's own. Dragging the spectrum edits dragBand.
const BAND = { 1: { lo: [32, 'band_lo'], hi: [33, 'band_hi'], sens: [31, 'sens'] },
               2: { lo: [59, 'band2_lo'], hi: [60, 'band2_hi'], sens: [61, 'sens2'] } };
let dragBand = 1;
const own2 = () => (knobs.band2_on ?? 0) >= 64;
// where a knob rests (= the renderer's KNOB_DEFAULT; everything else 64)
const DEFAULTS = { attack: 12, band_lo: 8, band_hi: 38, band2_lo: 8, band2_hi: 38, band2_on: 0, bg_pattern: 0, bg_mode: 0, bg_level: 0, bg_lfo: 0,
                   mask_shape: 0, mask_soft: 24, mask_fit: 127, bg_set: 0, mask_room: 56, logo: 0, gate: 56,
                   lfo_sync: 0, size_react: 0, size_lfo: 0, spin_react: 0, spin_lfo: 0, glow_react: 0,
                   glow_lfo: 0, sway_react: 0, sway_lfo: 0, trail_react: 0, trail_lfo: 0 };
// BACKGROUND + PEEPHOLE + LFO SYNC button lists. MUST match orc_visuals.py
// BG_PATTERNS / BG_MODES / MASK_SHAPES / LFO_SYNC (the renderer splits 0..127
// into equal ranges; a button sends its range's middle). The suite checks.
const BG_PATTERNS = ['checker', 'stripes', 'dots', 'truchet', 'waves', 'diamonds',
                     'rings', 'plaid', 'bricks', 'zigzag'];
const BG_MODES = ['wave', 'interlace', 'squeeze', 'two layers', 'wave + squeeze'];
const MASK_SHAPES = ['off', 'circle', 'rounded', 'hexagon', 'diamond', 'triangle', 'star'];
const LFO_SYNC = ['free', '4 bars', '2 bars', '1 bar', '1/2 bar', '1/4 bar'];
const PICKS = [[35, 'bg_mode', BG_MODES, 'bg-modes'], [44, 'mask_shape', MASK_SHAPES, 'mask-shapes'],
               [58, 'lfo_sync', LFO_SYNC, 'lfo-sync']];
// pattern SETS (built-in / hero / monster 1..) come from /api/visuals/patterns;
// SET = CC 50, PATTERN = CC 34 over the chosen set's names
let patSets = null, patShown = null;
const bucketOf = (v, n) => Math.min(n - 1, Math.floor(v * n / 128));
// the middle of the knob values bucketOf maps to i. NOT floor((i+.5)*128/n):
// past 66 names a range holds only 1-2 values and that rounds into range i-1,
// so 30 hero / monster 3 tiles could not be picked (2026-10-12)
const bucketMid = (i, n) => {
  const lo = Math.ceil(i * 128 / n), hi = Math.ceil((i + 1) * 128 / n) - 1;
  return Math.min(127, Math.floor((lo + hi) / 2));
};
// text from the server / JACK (errors, port names) going into innerHTML
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ccToHz = v => 20 * Math.pow(800, v / 127);
const hzToX = f => Math.log(Math.max(20, f) / 20) / Math.log(800);   // 0..1 across the spectrum
const fmtHz = f => f >= 1000 ? (f / 1000).toFixed(f >= 10000 ? 0 : 1) + 'k' : Math.round(f) + '';

let visCh = 0, touched = 0, built = false;
const knobs = {};                                   // name -> 0..127 (last known)
const pend = {};
const defOf = key => DEFAULTS[key] ?? 64;

function sliderRow(key) {
  const [cc, label, , hint] = KNOB[key], def = defOf(key);
  return `<div class="vrow" id="vr-${key}">
      <span class="vl">${label}</span>
      <input type="range" min="0" max="127" value="${def}" id="vk-${key}"
             oninput="sendKnob(${cc}, '${key}', this.value)"
             ondblclick="this.value=${def}; sendKnob(${cc}, '${key}', ${def})">
      <span class="vv" id="vv-${key}"></span>
    </div>${hint ? `<div class="vhint" id="vh-${key}">${hint}</div>` : ''}`;
}

function modGrid() {
  const rows = MOD_TARGETS.map(t => {
    const r = t + '_react', l = t + '_lfo';
    return `<span class="vl">${KNOB[r][1]}</span>
      <input type="range" min="0" max="127" value="0" id="vk-${r}" title="${KNOB[r][1]} REACT"
             oninput="sendKnob(${KNOB[r][0]}, '${r}', this.value)" ondblclick="this.value=0; sendKnob(${KNOB[r][0]}, '${r}', 0)">
      <input type="range" min="0" max="127" value="0" id="vk-${l}" title="${KNOB[l][1]} LFO"
             oninput="sendKnob(${KNOB[l][0]}, '${l}', this.value)" ondblclick="this.value=0; sendKnob(${KNOB[l][0]}, '${l}', 0)">`;
  }).join('');
  return `<span></span><span class="mh">REACT</span><span class="mh">LFO</span>${rows}`;
}

function build(d) {
  document.getElementById('vis-scenes').innerHTML =
      `<button class="flag" data-vis="orc" onclick="visSetCycle()">CYCLE</button>`
    + d.scenes.map(s => `<button class="flag" data-vis="orc:${s}"
          onclick="visCmd({action:'set', name:'${s}'})">${s.toUpperCase()}</button>`).join('')
    + `<button class="flag" data-vis="classic" style="opacity:.7"
          onclick="visCmd({action:'set', name:'classic', secs:+document.getElementById('vis-secs').value})">CLASSIC</button>`;
  for (const [g, el] of Object.entries(GROUP_EL))
    document.getElementById(el).innerHTML =
      Object.keys(KNOB).filter(k => KNOB[k][2] === g).map(sliderRow).join('');
  document.getElementById('mod-grid').innerHTML = modGrid();
  patLoad();
  for (const [cc, key, names, el] of PICKS)
    document.getElementById(el).innerHTML = names.map((n, i) =>
      `<button class="flag" data-i="${i}" onclick="pick(${cc}, '${key}', ${i}, ${names.length})">${n.toUpperCase()}</button>`).join('');
  document.getElementById('midi-list').innerHTML = midiList();
  built = true;
  showAll();
}

function midiList() {
  // a card prefix only where the bare label would be ambiguous (SIZE, LFO, SPEED ...)
  const names = { lfo: 'movement lfo', mod: 'movement', 'bg-show': 'background',
                  'bg-bend': 'background', 'bg-look': 'background', mask: 'peephole', 'mask-fit': 'peephole' };
  const rows = [[SCENE_CC, 'scene (0 = cycle)']].concat(Object.entries(KNOB).map(([k, [cc, label, g]]) => {
    const extra = g !== 'mod' ? '' : k.endsWith('_react') ? ' react' : ' lfo';
    return [cc, `${(g && names[g] ? names[g] + ' ' : '')}${label.toLowerCase()}${extra}`];
  }));
  rows.sort((a, b) => a[0] - b[0]);
  return rows.map(([cc, t]) => `<b>${cc}</b> ${t}`).join(' · ');
}

// throttled (not debounced): the picture follows a drag live, ~25 sends/s max
function sendKnob(cc, key, value) {
  touched = Date.now();
  knobs[key] = +value;
  showKnob(key);
  if (key === 'trails' || key === 'lfo_sync') showDims();
  if (key === 'band2_on') showBands();
  if (!visCh) return;
  const p = pend[cc];
  pend[cc] = { value: +value, timer: p && p.timer };
  if (!p || !p.timer) {
    pend[cc].timer = setTimeout(() => {
      const q = pend[cc]; pend[cc] = null;
      cmd('SYNTH_CC', { ch: visCh, cc, value: q.value });
    }, 40);
  }
}

function showKnob(key) {
  const el = document.getElementById('vv-' + key), f = KNOB[key] && KNOB[key][4];
  if (el && f) el.textContent = f(knobs[key] ?? defOf(key));
}

// TRAIL ZOOM / TWIST only act on the trails: dim them while TRAILS is low.
// RATE only matters when the LFO runs FREE.
function showDims() {
  const lowTrails = (knobs.trails ?? 64) < 40;
  for (const k of ['zoom', 'twist']) {
    for (const id of ['vr-', 'vh-']) {
      const el = document.getElementById(id + k);
      if (el) el.classList.toggle('vdim', lowTrails);
    }
  }
  const tn = document.getElementById('vh-trails');
  if (tn) tn.innerHTML = lowTrails
    ? '<span style="color:var(--warn,#fbbf24)">low: TRAIL ZOOM / TRAIL TWIST have little to move (turn TRAILS up to see them)</span>'
    : KNOB.trails[3];
  const synced = bucketOf(knobs.lfo_sync ?? 0, LFO_SYNC.length) > 0;
  const rr = document.getElementById('vr-lfo_rate'), rh = document.getElementById('vh-lfo_rate');
  if (rr) rr.classList.toggle('vdim', synced);
  if (rh) rh.textContent = synced ? 'not used while SYNC is on' : 'how fast the LFO swings';
}

function showAll() {
  for (const k of Object.keys(KNOB)) showKnob(k);
  showDims();
}

function pick(cc, key, i, n) {
  sendKnob(cc, key, bucketMid(i, n));
  showPicks();
}

function pickSet(i) {
  sendKnob(50, 'bg_set', bucketMid(i, patSets.length));
  sendKnob(34, 'bg_pattern', bucketMid(0, patSets[i].names.length));
  showPatterns();
}

async function patLoad() {
  try {
    const p = await fetch('/api/visuals/patterns').then(r => r.json());
    if (!p.ok) return;
    patSets = p.sets;
    document.getElementById('bg-sets').innerHTML = patSets.map((s, i) =>
      `<button class="flag" data-i="${i}" onclick="pickSet(${i})">${esc(s.name.toUpperCase())} (${s.names.length})</button>`).join('');
    patShown = null;                                 // rebuild the gallery (names may have changed)
    showPatterns();
  } catch (e) { /* keep the last */ }
}

// the chosen set's patterns: text buttons for the built-ins, a gallery of
// tiles (each shown repeating) for the file sets; CUSTOM adds its upload box
function showPatterns() {
  if (!patSets) return;
  const si = bucketOf(knobs.bg_set ?? 0, patSets.length), set = patSets[si];
  const box = document.getElementById('bg-patterns');
  const custom = set.folder === 'custom';
  if (patShown !== si) {
    patShown = si;
    box.innerHTML = set.names.length ? set.names.map((n, i) => set.folder
      ? `<button class="pthumb" data-i="${i}" title="${esc(n)}" onclick="pick(34, 'bg_pattern', ${i}, ${set.names.length})"
           style="background-image:url('/api/visuals/pattern/${set.folder}/${encodeURIComponent(n)}.png')"></button>`
      : `<button class="flag" data-i="${i}" onclick="pick(34, 'bg_pattern', ${i}, ${set.names.length})">${esc(n.toUpperCase())}</button>`).join('')
      : `<span style="font-size:10px;color:var(--muted)">no patterns of your own yet: upload one below</span>`;
    box.querySelectorAll('button').forEach(b => { b.disabled = !visCh; });
  }
  const cur = set.names.length ? bucketOf(knobs.bg_pattern ?? 0, set.names.length) : -1;
  box.querySelectorAll('[data-i]').forEach(b => b.classList.toggle('on', +b.dataset.i === cur));
  document.querySelectorAll('#bg-sets .flag').forEach(b => b.classList.toggle('on', +b.dataset.i === si));
  const nm = document.getElementById('bg-pattern-name');
  if (nm) nm.textContent = set.names[cur] || '';
  document.getElementById('pat-custom').style.display = custom ? '' : 'none';
  document.getElementById('pat-del').style.display = custom && cur >= 0 ? '' : 'none';
}

async function patUpload() {
  const f = document.getElementById('pat-file').files[0], m = document.getElementById('pat-msg');
  if (!f) { m.textContent = 'pick an SVG first'; return; }
  const fd = new FormData(); fd.append('file', f);
  m.textContent = 'checking...';
  try {
    const d = await fetch('/api/visuals/patterns', { method: 'POST', body: fd }).then(r => r.json());
    if (!d.ok) { m.innerHTML = `<span style="color:var(--danger)">${esc(d.error || 'failed')}</span>`; return; }
    m.innerHTML = `added <b>${esc(d.name)}</b>` + (d.warning ? ` <span style="color:var(--warn,#fbbf24)">· ${esc(d.warning)}</span>` : '');
    await patLoad();
    const set = patSets[patSets.length - 1], i = set.names.indexOf(d.name);
    if (i >= 0 && visCh) pick(34, 'bg_pattern', i, set.names.length);   // show it right away
  } catch (e) { m.textContent = 'no answer from the Pi'; }
}

async function patDelete() {
  const si = bucketOf(knobs.bg_set ?? 0, patSets.length), set = patSets[si];
  const name = set.names[bucketOf(knobs.bg_pattern ?? 0, set.names.length)];
  if (set.folder !== 'custom' || !name || !confirm(`Delete your pattern "${name}"?`)) return;
  const m = document.getElementById('pat-msg');
  try {
    const d = await fetch(`/api/visuals/pattern/custom/${encodeURIComponent(name)}.png`, { method: 'DELETE' }).then(r => r.json());
    m.innerHTML = d.ok ? `deleted ${esc(name)}` : `<span style="color:var(--danger)">${esc(d.error || 'failed')}</span>`;
  } catch (e) { m.textContent = 'no answer from the Pi'; }
  await patLoad();
}

function showPicks() {
  showPatterns();
  if (typeof logoShow === 'function' && logoNames.length) logoShow();
  for (const [, key, names, el] of PICKS) {
    const cur = bucketOf(knobs[key] ?? defOf(key), names.length);
    document.querySelectorAll(`#${el} .flag`).forEach(b => b.classList.toggle('on', +b.dataset.i === cur));
  }
  showDims();
}

function visCentre(boxId) {
  document.querySelectorAll(`#${boxId} input[type=range]`).forEach(el => {
    const key = el.id.slice(3);
    el.value = 64;
    sendKnob(KNOB[key][0], key, 64);
  });
}

let logoAutoOpened = false;

async function visLoad() {
  const st = document.getElementById('vis-state');
  try {
    const d = await fetch('/api/visuals').then(r => r.json());
    if (!d.ok) throw new Error(d.error || '?');
    if (!built) build(d);
    const what = d.mode === 'classic' ? 'classic' : d.mode.startsWith('orc')
      ? (d.cycling === false && d.scene ? d.scene : 'cycling') : d.mode;
    st.innerHTML = d.active ? `<span style="color:var(--ok,#4ade80)">● ${what}</span>`
                            : '<span style="color:var(--muted)">off</span>';
    document.getElementById('vis-start').disabled = d.active;
    document.getElementById('vis-stop').disabled = !d.active;
    document.querySelectorAll('#vis-scenes .flag').forEach(b =>
      b.classList.toggle('on', b.dataset.vis === d.mode));
    // the logo picker opens itself when a scene that uses it is held (once)
    if (!logoAutoOpened && /^orc:(logo|shape4d)$/.test(d.mode)) {
      document.getElementById('logo-box').open = true;
      logoAutoOpened = true;
    }
    for (const [id, v] of [['vis-secs', d.secs], ['vis-fps', d.fps]]) {
      const el = document.getElementById(id);
      if (document.activeElement !== el) {
        el.value = String(v);
        if (!el.value) el.value = id === 'vis-fps' ? '30' : '60';   // a held scene saves 0 s
      }
    }
    visCh = d.channel || 0;
    if (Date.now() - touched > 3000) {
      for (const key of Object.keys(KNOB)) {
        const v = d.knobs && d.knobs[key];
        if (Number.isInteger(v)) {
          knobs[key] = v;
          const el = document.getElementById('vk-' + key);
          if (el) el.value = v;
        }
      }
      showAll();
    }
    showPicks();
    showBands();
    document.querySelectorAll('.card input[type=range]').forEach(i => { i.disabled = !visCh; });
    // picker buttons too: unbound, a click would light up, send nothing and snap back
    document.querySelectorAll('#bg-sets button, #bg-patterns button, #bg-modes button, #mask-shapes button, '
                              + '#lfo-sync button, #logo-list button').forEach(b => { b.disabled = !visCh; });
    const b = document.getElementById('vis-bind');
    const sig = visCh + '|' + d.free.join(',');      // redraw only on change, or
    if (b.dataset.sig !== sig) {                     // the picker resets under you
      b.dataset.sig = sig;
      b.innerHTML = visCh
        ? `knobs on MIDI ch ${visCh} (move it on <a href="/matrix">/matrix</a>)`
        : `<span style="color:var(--warn)">the sliders need a MIDI channel:</span>
           <select id="vis-ch">${d.free.map(c => `<option value="${c}">ch ${c}</option>`).join('')}</select>
           <button class="flag" onclick="visBind()">BIND</button>`;
    }
  } catch (e) {
    st.innerHTML = '<span style="color:var(--danger)">status unavailable</span>';
  }
}

async function visCmd(body) {
  const m = document.getElementById('vis-msg');
  m.textContent = '';
  try {
    const d = await fetch('/api/visuals', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
    if (!d.ok) m.innerHTML = `<span style="color:var(--danger)">${esc(d.error || 'failed')}</span>`;
  } catch (e) {
    m.innerHTML = '<span style="color:var(--danger)">no answer from the ORC</span>';
  }
  setTimeout(visLoad, body.action === 'start' ? 3000 : 600);
}

function visSetCycle() {
  visCmd({ action: 'set', name: 'orc', secs: +document.getElementById('vis-secs').value });
}

async function visBind() {
  await cmd('MODULE_BIND', { module: 'visuals', channel: +document.getElementById('vis-ch').value });
  setTimeout(visLoad, 800);
}

// ── spectrum + beat band ─────────────────────────────────────────────────────
// Frames arrive over SSE (~25/s); the bars are drawn every animation frame,
// jumping UP to a new value and falling smoothly, like a hardware analyser
// (2026-10-12: it was polled 10x/s on a timer of its own and each bar stepped,
// so it looked choppy). Staleness is judged by when frames ARRIVE here.
const cv = document.getElementById('aud-spec');
const cx = cv.getContext('2d');
let spec = null, specRuns = null, bandTrue = null, band2True = null, drag = null, beatAt = -1e9, beat2At = -1e9;
const BAND2_COLOUR = '#22d3ee';
let disp = null, lastRecv = -1e9, lastDraw = performance.now(), audFrame = null;
const FALL = 1.6;                                   // bar fall speed: full height per 1/1.6 s
const TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 16000];

function css(name, fb) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb;
}

function drawTicks() {
  document.getElementById('aud-ticks').innerHTML = TICKS.map((f, i) => {
    const x = hzToX(f) * 100, tf = i === 0 ? 'translateX(0)' : i === TICKS.length - 1 ? 'translateX(-100%)' : '';
    return `<span style="left:${x}%;${tf ? 'transform:' + tf : ''}">${fmtHz(f)}${i === 0 ? ' Hz' : ''}</span>`;
  }).join('');
}

function draw() {
  const now = performance.now(), dt = Math.min(0.1, (now - lastDraw) / 1000);
  lastDraw = now;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(cv.clientWidth * dpr), h = Math.round(170 * dpr);
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  cx.clearRect(0, 0, w, h);
  const acc = css('--accent', '#ff5cc8');
  const two = own2();
  // band 1 (shapes) in the accent colour, band 2 (background, OWN BAND only)
  // in cyan; the band you are dragging is drawn stronger
  const bands = [[1, acc, (now - beatAt) < 150, bandTrue]].concat(
    two ? [[2, BAND2_COLOUR, (now - beat2At) < 150, band2True]] : []);
  const geo = {};
  for (const [b, col, beating] of bands) {
    const lo = bandKnob(b, 'lo'), hi = bandKnob(b, 'hi');
    const bx0 = Math.min(lo, hi) / 127 * w, bx1 = Math.max(lo, hi) / 127 * w;
    geo[b] = [bx0, bx1];
    const strong = !two || b === dragBand;
    cx.globalAlpha = (beating ? 0.45 : 0.2) * (strong ? 1 : 0.5);   // flares on its beat
    cx.fillStyle = col;
    cx.fillRect(bx0, 0, Math.max(2, bx1 - bx0), h);
    cx.globalAlpha = strong ? 1 : 0.5;
    cx.fillRect(bx0 - 1, 0, 2, h);
    cx.fillRect(bx1 - 1, 0, 2, h);
    cx.globalAlpha = 1;
  }
  const [x0, x1] = geo[1];
  if (spec) {
    const n = spec.length;
    if (!disp || disp.length !== n) disp = new Array(n).fill(0);
    const runs = specRuns && specRuns.reduce((a, b) => a + b, 0) === n ? specRuns : new Array(n).fill(1);
    let i = 0;
    for (const len of runs) {
      const db = spec[i];
      const v = db === null ? 0 : Math.max(0, Math.min(1, (db + 72) / 66));   // -72 .. -6 dB (vs the auto gain)
      disp[i] = v >= disp[i] ? v : Math.max(v, disp[i] - FALL * dt);
      const bx = i / n * w, bw = len / n * w, bh = disp[i] * h;
      const mid = (i + len / 2) / n * w;
      const in2 = geo[2] && mid >= geo[2][0] && mid <= geo[2][1];
      cx.fillStyle = mid >= x0 && mid <= x1 ? '#ffffff' : in2 ? BAND2_COLOUR : 'rgba(170,190,255,.55)';
      cx.fillRect(bx + 1, h - bh, Math.max(1, bw - 2), bh);
      i += len;
    }
  }
  for (const [, col, , tr] of bands) {              // what each detector really hears
    if (!tr) continue;
    const t0 = hzToX(tr[0]) * w, t1 = hzToX(tr[1]) * w;
    cx.save();
    cx.setLineDash([4 * dpr, 4 * dpr]);
    cx.strokeStyle = col;
    cx.lineWidth = 1.5 * dpr;
    cx.strokeRect(t0, 2, Math.max(2, t1 - t0), h - 4);
    cx.restore();
  }
  const txt = (b, tr) => {
    const lo = bandKnob(b, 'lo'), hi = bandKnob(b, 'hi');
    return `${fmtHz(ccToHz(Math.min(lo, hi)))}–${fmtHz(ccToHz(Math.max(lo, hi)))} Hz`
      + (tr ? ` (hears ~${fmtHz(tr[0])}–${fmtHz(tr[1])})` : '');
  };
  document.getElementById('aud-band').textContent = two
    ? `shapes ${txt(1, bandTrue)} · background ${txt(2, band2True)}`
    : `beat band ${txt(1, bandTrue)}`;
  document.getElementById('aud-beat').style.background = bands[0][2] ? 'var(--accent,#ff5cc8)' : 'var(--dim)';
  const d2 = document.getElementById('aud-beat2');
  d2.style.display = two ? '' : 'none';
  d2.style.background = two && bands[1][2] ? BAND2_COLOUR : 'var(--dim)';
}

function animate() {
  if (!document.hidden) {
    if (performance.now() - lastRecv > 2000) {           // no live frames (stopped, or never came)
      spec = null; bandTrue = null; band2True = null;
      const lat = document.getElementById('aud-latency'), msg = 'the visuals are not running (press START above)';
      if (lat.textContent !== msg) lat.textContent = msg;
    }
    draw();
  }
  requestAnimationFrame(animate);
}

// The stream's FIRST frame is just whatever file is on disk, possibly left
// over from a renderer that has since stopped (its "t" is the Pi's clock,
// not ours, so it can't be aged here): a frame only counts once the renderer
// has written a NEWER one. The poll path is aged by the server instead.
let streamT = null;
function onStreamFrame(d) {
  const prev = streamT;
  streamT = d.t;
  if (prev !== null && d.t !== prev) onAudio(d);
}

function onAudio(d) {
  lastRecv = performance.now();
  spec = d.spec;
  specRuns = d.spec_runs || null;
  bandTrue = d.band_true || null;
  band2True = d.band2_on ? (d.band2_true || null) : null;
  if (d.beat_age < 0.15) beatAt = lastRecv - d.beat_age * 1000;
  if (typeof d.beat2_age === 'number' && d.beat2_age < 0.15) beat2At = lastRecv - d.beat2_age * 1000;
  audFrame = d;
  const hitEl = document.getElementById('mv-hit'), lfoEl = document.getElementById('mv-lfo');
  if (hitEl && typeof d.hit === 'number') hitEl.style.width = Math.min(100, d.hit * 80) + '%';
  if (lfoEl && typeof d.lfo === 'number') lfoEl.style.left = `calc(${(0.5 + 0.5 * Math.sin(2 * Math.PI * d.lfo)) * 100}% - 2px)`;
  const note = document.getElementById('lfo-note');
  if (note) {
    const synced = bucketOf(knobs.lfo_sync ?? 0, LFO_SYNC.length) > 0;
    note.textContent = !synced ? '' : d.synced ? `locked to the bar · ${d.bpm} BPM`
      : `sequencer stopped: running at ${d.bpm ?? '?'} BPM, it locks to the bar when the sequencer plays`;
  }
  const lat = document.getElementById('aud-latency');
  const frame = 1000 / d.fps;
  const tail = frame / 2 + frame + 17;
  if (d.source === 'tap') {
    const est = Math.round(d.age_ms + d.win_ms / 2 + tail);
    lat.innerHTML = `listening to <b>${esc((d.tap_ports || []).join(' + '))}</b> · ${d.fps} fps ·
      about <b>${est} ms</b> from sound to picture, plus your monitor's own delay`;
  } else {
    const est = Math.round(d.cadence_ms / 2 + d.win_ms / 2 + tail);
    lat.innerHTML = `listening to skipback ring <b>${esc(d.ring || '?')}</b>${d.source ? '' : ' (nothing yet)'}
      · ${d.fps} fps · about <b>${est} ms</b> from sound to picture, plus your monitor's own delay`;
  }
}

// The stream, closed while the tab is hidden (a locked phone otherwise keeps a
// slot, or a half-open one, for minutes: 4 slots, then 429s) and reopened when
// it shows. When the browser gives up on it (a 429 or any non-200 = CLOSED,
// never retried) the page polls instead, and tries the stream again later.
// (2026-10-12 review: after a few lock/wake cycles the spectrum froze and the
// page wrongly said the visuals were not running.)
let es = null, polling = false;
function audStream() {
  if (es || polling || document.hidden) return;
  if (!window.EventSource) { audPoll(); return; }
  streamT = null;
  es = new EventSource('/api/visuals/audio/stream');
  es.onmessage = ev => { try { onStreamFrame(JSON.parse(ev.data)); } catch (e) { /* skip a bad frame */ } };
  es.onerror = () => {
    if (es && es.readyState === EventSource.CLOSED) {   // the browser won't retry: poll
      es = null;
      audPoll();
    }                                                   // else it reconnects by itself
  };
}

function audStreamClose() {
  if (es) { es.close(); es = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) audStreamClose(); else audStream();
});

async function audPoll() {
  if (polling) return;
  polling = true;
  const until = performance.now() + 30000;             // then give the stream another go
  while (performance.now() < until) {
    if (!document.hidden) {
      try {
        const d = await fetch('/api/visuals/audio').then(r => r.json());
        if (d.ok && d.age < 2) onAudio(d);
      } catch (e) { /* keep the last frame */ }
    }
    await new Promise(r => setTimeout(r, 100));
  }
  polling = false;
  audStream();
}

function ccAt(ev) {
  const r = cv.getBoundingClientRect();
  return Math.max(0, Math.min(127, Math.round((ev.clientX - r.left) / r.width * 127)));
}

cv.addEventListener('pointerdown', ev => {
  if (!visCh) return;
  const c = ccAt(ev);
  const lo = bandKnob(dragBand, 'lo'), hi = bandKnob(dragBand, 'hi');
  const near = 4;                                   // grab an edge within ~4 steps
  if (Math.abs(c - lo) <= near) drag = 'lo';
  else if (Math.abs(c - hi) <= near) drag = 'hi';
  else { drag = 'hi'; setBand('lo', c); }           // start a fresh band here
  setBand(drag, c);
  cv.setPointerCapture(ev.pointerId);
});
cv.addEventListener('pointermove', ev => { if (drag) setBand(drag, ccAt(ev)); });
cv.addEventListener('pointerup', () => {
  drag = null;
  const lo = bandKnob(dragBand, 'lo'), hi = bandKnob(dragBand, 'hi');   // keep lo below hi on the wire
  if (lo > hi) { setBand('lo', hi); setBand('hi', lo); }
});

function bandKnob(b, edge) {
  const key = BAND[b][edge][1];
  return knobs[key] ?? defOf(key);
}

function setBand(which, c) {
  const [cc, key] = BAND[dragBand][which];
  sendKnob(cc, key, c);
}

// SHAPES / BACKGROUND: which band a drag edits. Picking BACKGROUND turns on
// its OWN BAND, starting as a copy of the shapes' band + SENS (so nothing
// changes until you move it); SAME AS SHAPES turns it back off.
function pickDragBand(b) {
  if (b === 2 && !own2()) {
    for (const part of ['lo', 'hi', 'sens']) {
      const [cc, key] = BAND[2][part], from = BAND[1][part][1];
      const v = knobs[from] ?? defOf(from);
      const el = document.getElementById('vk-' + key);
      if (el) el.value = v;
      sendKnob(cc, key, v);
    }
    sendKnob(62, 'band2_on', 127);
  }
  dragBand = b;
  showBands();
}

function bandSame() {
  sendKnob(62, 'band2_on', 0);
  dragBand = 1;
  showBands();
}

function showBands() {
  const two = own2();
  if (!two) dragBand = 1;
  document.querySelectorAll('#band-pick .flag').forEach(f => f.classList.toggle('on', +f.dataset.b === dragBand));
  const same = document.getElementById('band-same');
  if (same) same.style.display = two ? '' : 'none';
  for (const id of ['vr-sens2', 'vh-sens2']) {
    const el = document.getElementById(id);
    if (el) el.style.display = two ? '' : 'none';
  }
  document.querySelectorAll('#band-pick .flag, #band-same').forEach(f => { f.disabled = !visCh; });
}

// ── SOURCE: which JACK ports the tap listens to ──────────────────────────────
// Stereo pairs are guessed from the names (..._1/_2 capture pairs, ..._L/_R);
// anything left over is offered alone (mono).
function srcPairs(ports) {
  const set = new Set(ports), used = new Set(), out = [];
  for (const p of ports) {
    if (used.has(p)) continue;
    let m = p.match(/^(.*[^0-9])(\d+)$/), q = null;
    if (m && +m[2] % 2 === 1) q = m[1] + (+m[2] + 1);
    if (!q && /_L$/.test(p)) q = p.replace(/_L$/, '_R');
    if (q && set.has(q) && !used.has(q)) {
      used.add(p); used.add(q);
      const short = q.startsWith(p.slice(0, p.indexOf(':') + 1)) ? q.slice(q.indexOf(':') + 1) : q;
      out.push({ l: p, r: q, label: `${p} + ${short}` });
    }
  }
  for (const p of ports) if (!used.has(p)) out.push({ l: p, r: '', label: `${p} (mono)` });
  return out;
}

async function srcLoad() {
  const sel = document.getElementById('aud-src');
  if (document.activeElement === sel) return;
  try {
    const d = await fetch('/api/visuals/source').then(r => r.json());
    if (!d.ok) throw new Error(d.error);
    const opts = [{ v: 'ring', label: 'skipback ring (master only)' }]
      .concat(srcPairs(d.sources).map(o => ({ v: o.l + '|' + o.r, label: o.label })));
    const cur = d.mode === 'ring' ? 'ring' : d.left + '|' + (d.right === d.left ? '' : d.right);
    if (!opts.some(o => o.v === cur) && cur !== 'ring')
      opts.push({ v: cur, label: `${cur.replace('|', ' + ')} (not in the graph now)` });
    const sig = opts.map(o => o.v).join(',');
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.innerHTML = opts.map(o => `<option value="${esc(o.v)}"></option>`).join('');
      [...sel.options].forEach((el, i) => { el.textContent = opts[i].label; });
    }
    sel.value = cur;
  } catch (e) { /* keep the last list */ }
}

async function srcSet() {
  const v = document.getElementById('aud-src').value, m = document.getElementById('aud-src-msg');
  const [l, r] = v.split('|');
  const body = v === 'ring' ? { mode: 'ring' } : { left: l, right: r };
  try {
    const d = await fetch('/api/visuals/source', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
    m.innerHTML = d.ok ? '' : `<span style="color:var(--danger)">${esc(d.error || 'failed')}</span>`;
  } catch (e) {
    m.innerHTML = '<span style="color:var(--danger)">no answer from the ORC</span>';
  }
  srcLoad();
}

// ── LOGO (CC 52): "chord shape" first, then each logo ───────────────────────
let logoNames = [], logoUser = [], logoShown = null;

async function logoLoad() {
  try {
    const d = await fetch('/api/visuals/logos').then(r => r.json());
    if (!d.ok) return;
    const sig = d.names.join('|');
    logoNames = d.names; logoUser = d.user;
    const box = document.getElementById('logo-list');
    if (box.dataset.sig !== sig) {
      box.dataset.sig = sig;
      box.innerHTML = ['CHORD SHAPE', ...logoNames.map(n => n.toUpperCase())].map((n, i) =>
        `<button class="flag" data-i="${i}" onclick="logoPick(${i})"></button>`).join('');
      [...box.children].forEach((b, i) => { b.textContent = i ? logoNames[i - 1].toUpperCase() : 'CHORD SHAPE'; });
      logoShown = null;
    }
    logoShow();
  } catch (e) { /* keep the last */ }
}

function logoPick(i) {
  sendKnob(52, 'logo', bucketMid(i, logoNames.length + 1));
  logoShow();
}

function logoShow() {
  const i = bucketOf(knobs.logo ?? 0, logoNames.length + 1);
  document.querySelectorAll('#logo-list .flag').forEach(b => b.classList.toggle('on', +b.dataset.i === i));
  const name = i ? logoNames[i - 1] : '';
  document.getElementById('logo-name').textContent = name || 'chord shape';
  document.getElementById('logo-del').style.display = logoUser.includes(name) ? '' : 'none';
  if (logoShown === name) return;
  const cv = document.getElementById('logo-preview');
  if (!cv.clientWidth) return;                      // folded away: draw when opened
  logoShown = name;
  const g = cv.getContext('2d');
  cv.width = cv.clientWidth;
  g.fillStyle = '#000'; g.fillRect(0, 0, cv.width, cv.height);
  if (!name) return;
  fetch('/api/visuals/logo/' + encodeURIComponent(name)).then(r => r.json()).then(d => {
    if (!d.ok || logoShown !== name) return;
    const s = cv.height * 0.45, cx = cv.width / 2, cy = cv.height / 2;
    g.strokeStyle = css('--accent', '#ff5cc8'); g.lineWidth = 1.5;
    for (const p of d.polys) {
      g.beginPath();
      p.forEach(([x, y], k) => k ? g.lineTo(cx + x * s, cy + y * s) : g.moveTo(cx + x * s, cy + y * s));
      g.stroke();
    }
  });
}

async function logoUpload() {
  const f = document.getElementById('logo-file').files[0], m = document.getElementById('logo-msg');
  if (!f) { m.textContent = 'pick an SVG first'; return; }
  const fd = new FormData(); fd.append('file', f);
  try {
    const d = await fetch('/api/visuals/logos', { method: 'POST', body: fd }).then(r => r.json());
    m.innerHTML = d.ok ? `added <b>${d.name}</b> (${d.lines} lines)`
                       : `<span style="color:var(--danger)">${esc(d.error || 'failed')}</span>`;
  } catch (e) { m.textContent = 'no answer from the Pi'; }
  logoLoad();
}

async function logoDelete() {
  const i = bucketOf(knobs.logo ?? 0, logoNames.length + 1), name = logoNames[i - 1];
  if (!name || !confirm(`Delete the logo "${name}"?`)) return;
  await fetch('/api/visuals/logo/' + encodeURIComponent(name), { method: 'DELETE' });
  logoPick(0);
  logoLoad();
}

document.getElementById('logo-box').addEventListener('toggle', () => { logoShown = null; logoShow(); });
drawTicks();
visLoad();
setInterval(visLoad, 2000);
logoLoad();
setInterval(logoLoad, 5000);
srcLoad();
setInterval(srcLoad, 5000);
audStream();
requestAnimationFrame(animate);
