/* ORC-1 · matrix.js — Module Matrix page (slice 4).
   Wires the approved mockup to the live engine:
     bus + rows  <- /state {bindings, join, reserved, synth_cc}
     assign flow -> MODULE_BIND {module, channel}   (arm client-side, commit = the cmd)
     take out    -> MODULE_UNBIND {module}
     P1/P2 join  -> MODULE_JOIN {mode|detune|spread}
     level       -> SYNTH_CC / SAMPLER_MASTER_GAIN  (live, ephemeral)
     params ▸    -> /static/cc_map.json (living CC list)
   The channel map + join are truth on the engine; this page reflects and drives them. */
'use strict';

const RESLAB = { 12: 'CHORD', 13: 'CTRL', 14: 'MPC', 15: 'MELODY' };
const JOIN_MODES = ['none', 'unison', 'detune', 'fifth', 'third', 'octave', 'octave_dn'];
const JOIN_LABEL = { none: '—', unison: 'UNISON', detune: 'DETUNE', fifth: '5TH',
                     third: '3RD', octave: 'OCT', octave_dn: 'OCT−' };
const DEFAULT_DETUNE = 7;   // sent when first entering DETUNE mode so it isn't unison

// Display order + identity. `lvl` describes how this module's level reaches the synth.
const MODULES = [
  { key: 'drums',      name: 'DRUMS',    fam: '909 kit',       code: 'DRM',  color: '#e0a000' },
  { key: 'poly',       name: 'POLY',     fam: 'poly synth',      code: 'POLY', color: '#4a90e2' },
  { key: 'chip_p1',    name: 'CHIP P1',  fam: 'NES pulse 1',   code: 'CP1',  color: '#9060e0' },
  { key: 'chip_p2',    name: 'CHIP P2',  fam: 'NES pulse 2',   code: 'CP2',  color: '#9060e0' },
  { key: 'chip_tri',   name: 'CHIP TRI', fam: 'NES triangle',  code: 'TRI',  color: '#9060e0' },
  { key: 'chip_noise', name: 'CHIP NS',  fam: 'NES noise',     code: 'NOI',  color: '#9060e0' },
  { key: 'chip_noise2', name: 'CHIP NS2', fam: 'NES noise 2',  code: 'NO2',  color: '#9060e0' },
  { key: 'gb_wave',    name: 'GB WAVE',  fam: 'Game Boy wave', code: 'GBW',  color: '#22e097' },
  { key: 'chip_pcm',   name: 'CHIP PCM', fam: 'NES sample',    code: 'PCM',  color: '#9060e0' },
  { key: 'sampler',    name: 'SAMPLER',  fam: 'skipback deck', code: 'SMP',  color: '#00c8a0' },
  { key: 'mono',       name: 'MONO',     fam: 'mono synth',    code: 'MONO', color: '#e06040' },
  { key: 'bbkit',      name: 'BB KIT',   fam: 'BeatBuddy kit', code: 'BBK',  color: '#e0a000' },
  { key: 'visuals',    name: 'VISUALS',  fam: 'HDMI scenes',   code: 'VIS',  color: '#ff5cc8' },
  { key: 'zoomfx',     name: 'ZOOM FX',  fam: 'MS-70CDR pedal', code: 'ZFX', color: '#c0c0c0' },
];
const NO_TAKEOUT = new Set(['sampler']);

/* level → the right CC/cmd for a module on its live channel (see orc1_synth.py:
   CC13 = each voice's own master; gb_wave has none, uses CC7; drums = the drum-BUS
   master CC10 on ch13; sampler = its own master gain facade). */
function levelDescriptor(m, ch) {
  if (m.key === 'drums')   return { action: 'SYNTH_CC', extra: v => ({ ch: 13,  cc: 10, value: v }), read: () => readCC(13, 10) };
  // bbkit's level is its BUS master on the control channel (CC12, orc1_bbkit_host)
  if (m.key === 'bbkit')   return { action: 'SYNTH_CC', extra: v => ({ ch: 13,  cc: 12, value: v }), read: () => readCC(13, 12) };
  if (m.key === 'sampler') return { action: 'SAMPLER_MASTER_GAIN', extra: v => ({ value: v }), read: () => null };
  // visuals makes no sound: its LVL is the scenes' BRIGHTNESS knob (CC 25)
  if (m.key === 'visuals') return { action: 'SYNTH_CC', extra: v => ({ ch, cc: 25, value: v }), read: () => readCC(ch, 25) };
  // the Zoom pedal has no master level: CC7 is sent and orc1_zoomfx.py ignores it
  if (m.key === 'zoomfx')  return { action: 'SYNTH_CC', extra: v => ({ ch, cc: 7, value: v }), read: () => readCC(ch, 7) };
  if (m.key === 'gb_wave') return { action: 'SYNTH_CC', extra: v => ({ ch, cc: 7,  value: v }), read: () => readCC(ch, 7) };
  return { action: 'SYNTH_CC', extra: v => ({ ch, cc: 13, value: v }), read: () => readCC(ch, 13) };
}

let live = { bindings: {}, join: { mode: 'none', detune: 0, spread: false }, reserved: [12, 13, 14], cc: {} };
let armed = null;            // module key pending a channel pick, or null
let ccMap = null;            // /static/cc_map.json modules block
let openParams = null;       // module key whose params drawer is open
const lvlVal = {};           // client-authoritative fader positions {key: 0-127}
let holdUntil = 0;           // suppress poll overwrite briefly after an optimistic action
const _lastSent = {};        // per-key throttle timestamps

const $ = id => document.getElementById(id);
function readCC(ch, cc) {
  const c = live.cc[String(ch)];
  const v = c && c[String(cc)];
  return (typeof v === 'number') ? v : null;
}

const joined = () => live.join.mode !== 'none';
// module -> channel, from live bindings (a module absent = out of the fold)
function channelOf(key) { return live.bindings[key]; }
// channel -> module currently on it
function occByChannel() {
  const o = {};
  for (const [k, ch] of Object.entries(live.bindings)) o[ch] = k;
  return o;
}
function isReserved(c) { return live.reserved.includes(c); }

/* ─── render ──────────────────────────────────────────────── */
function renderBus() {
  const bus = $('bus'); if (!bus) return;
  const occ = occByChannel();
  let html = '';
  let free = 0;
  for (let c = 1; c <= 16; c++) {
    let cls = 'empty', tag = '—';
    if (isReserved(c)) { cls = 'reserved'; tag = RESLAB[c] || 'RSV'; }
    else if (occ[c]) {
      cls = 'occupied';
      const mk = occ[c];
      tag = (MODULES.find(m => m.key === mk) || {}).code || mk;
      if (mk === 'chip_p1' && joined()) tag = 'CP1⛓';
    } else free++;
    const pickable = armed && !isReserved(c) && !occ[c];
    if (pickable) cls += ' pickable';
    html += `<div class="cell ${cls}" data-ch="${c}">
      <span class="ch">${String(c).padStart(2, '0')}</span><span class="tag">${tag}</span></div>`;
  }
  bus.innerHTML = html;
  if (armed) bus.querySelectorAll('.cell.pickable').forEach(el =>
    el.onclick = () => commit(+el.dataset.ch));
  const bound = Object.keys(occ).length;
  $('bus-count').textContent = `${bound} bound · ${free} free · ${live.reserved.length} reserved`;
}

function paramsDrawer(key) {
  if (openParams !== key) return '';
  const list = ccMap && ccMap[key];
  if (!list) return `<div class="pdrawer"><span class="pnote">no CC list for this module</span></div>`;
  if (!list.length) return `<div class="pdrawer"><span class="pnote">controlled from its own page — no CC list</span></div>`;
  const rows = list.map(p => {
    let d = (p.desc || p.code || '').trim();
    if (d.length > 88) d = d.slice(0, 86) + '…';
    const cond = p.cond ? ` <span style="color:var(--footer)">(${escapeHtml(String(p.cond))})</span>` : '';
    return `<div class="prow"><span class="pcc">CC ${escapeHtml(String(p.cc))}</span><span class="pdesc">${escapeHtml(d)}${cond}</span></div>`;
  }).join('');
  return `<div class="pdrawer">${rows}</div>`;
}
function escapeHtml(s) { return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function renderMods() {
  const host = $('mods'); if (!host) return;
  // focus guard: don't rebuild while a fader in here is being dragged
  if (host.contains(document.activeElement) &&
      document.activeElement.classList.contains('fader')) return;
  const jn = joined();
  let html = '';
  MODULES.forEach(m => {
    const ch = channelOf(m.key);
    const on = ch !== undefined;
    const isP1 = m.key === 'chip_p1';
    const isP2joined = m.key === 'chip_p2' && jn;

    // level: client value, seeded from last-known CC when we have it
    if (lvlVal[m.key] === undefined) {
      const r = on ? levelDescriptor(m, ch).read() : null;
      lvlVal[m.key] = (r === null) ? (m.key === 'visuals' ? 64 : 96) : r;
    }
    const lv = lvlVal[m.key];

    const chSlot = `<div class="ch-chip" data-arm="${m.key}"><span class="lab">CH</span>
        <span class="num">${on ? String(ch).padStart(2, '0') : '—'}</span></div>`;

    let meta = '';
    if (isP1) meta += `<span class="join-chip${jn ? '' : ' off'}" data-join="1" title="cycle P1+P2 coupling">
        <span class="lab">JOIN P2</span> <b>${JOIN_LABEL[live.join.mode]}</b></span>`;
    if (isP2joined) meta += `<span class="join-chip" data-unjoin="1" title="release the join">
        <span class="lab">⛓ P1</span> <b>${JOIN_LABEL[live.join.mode]}${live.join.mode === 'detune'
          ? ' ' + (live.join.detune >= 0 ? '+' : '') + live.join.detune + 'ct' : ''}</b></span>`;
    meta += `<a class="mtag patch" href="/patch">outputs <b>/patch</b></a>`;
    meta += `<span class="params${openParams === m.key ? ' open' : ''}" data-params="${m.key}">params ▸</span>`;

    const pwrLocked = NO_TAKEOUT.has(m.key);
    html += `<div class="mod${on ? '' : ' off'}${armed === m.key ? ' armed' : ''}${isP2joined ? ' joined' : ''}${openParams === m.key ? ' pop' : ''}" data-key="${m.key}">
      <button class="pwr${pwrLocked ? ' locked' : ''}" data-pwr="${m.key}" title="${pwrLocked ? 'always on' : 'bring in / take out'}">⏻</button>
      <div class="id"><i class="dot" style="background:${m.color};box-shadow:0 0 7px ${m.color}"></i>
        <div><div class="mname">${m.name}</div><div class="mfam">${m.fam}</div></div></div>
      ${chSlot}
      <div class="lvl"><span class="lvl-lab">LVL</span>
        <input class="fader" type="range" min="0" max="127" value="${lv}" data-lvl="${m.key}" style="--fill:${Math.round(lv / 127 * 100)}%">
        <span class="val">${lv}</span></div>
      <div class="meta">${meta}</div>
      ${paramsDrawer(m.key)}
    </div>`;
  });
  host.innerHTML = html;
  wireMods(host);
  const inFold = MODULES.filter(m => channelOf(m.key) !== undefined).length;
  $('mod-count').textContent = `${inFold}/${MODULES.length} bound`;
}

function wireMods(host) {
  host.querySelectorAll('[data-pwr]').forEach(el => el.onclick = () => togglePower(el.dataset.pwr));
  host.querySelectorAll('[data-arm]').forEach(el => el.onclick = () => arm(el.dataset.arm));
  host.querySelectorAll('[data-join]').forEach(el => el.onclick = cycleJoin);
  host.querySelectorAll('[data-unjoin]').forEach(el => el.onclick = () => setJoin('none'));
  host.querySelectorAll('[data-params]').forEach(el => el.onclick = () => {
    openParams = (openParams === el.dataset.params) ? null : el.dataset.params;
    renderMods();
  });
  host.querySelectorAll('[data-lvl]').forEach(f => {
    const key = f.dataset.lvl;
    const val = f.parentElement.querySelector('.val');
    f.oninput = () => {
      const v = +f.value;
      lvlVal[key] = v;
      f.style.setProperty('--fill', Math.round(v / 127 * 100) + '%');
      val.textContent = v;
      sendLevel(key, v, false);
    };
    f.onchange = () => sendLevel(key, +f.value, true);
  });
}

function render() {
  const mtx = $('mtx');
  if (mtx) mtx.classList.toggle('arming', !!armed);
  if (armed) $('arm-name').textContent = (MODULES.find(m => m.key === armed) || {}).name || '—';
  renderBus();
  renderMods();
  renderProt();
}

/* ─── actions ─────────────────────────────────────────────── */
function markHold() { holdUntil = Date.now() + 1200; }

function arm(key) {
  if (channelOf(key) === undefined) { // out of the fold: bringing it in also picks a channel
    // fall through — arming an unbound module lets the user drop it on a free cell
  }
  armed = (armed === key) ? null : key;
  render();
}
function cancel() { armed = null; render(); }

function commit(channel) {
  if (!armed) return;
  const key = armed;
  cmd('MODULE_BIND', { module: key, channel });
  live.bindings[key] = channel;     // optimistic
  armed = null; markHold();
  render();
  const cell = $('bus').querySelector(`.cell[data-ch="${channel}"]`);
  if (cell) cell.classList.add('flash');
}

function togglePower(key) {
  if (NO_TAKEOUT.has(key)) return;
  if (channelOf(key) !== undefined) {          // in the fold → take out
    cmd('MODULE_UNBIND', { module: key });
    delete live.bindings[key];                 // optimistic
    if (armed === key) armed = null;
    markHold(); render();
  } else {                                     // out → bring in: arm to pick a channel
    arm(key);
  }
}

function cycleJoin() {
  const i = JOIN_MODES.indexOf(live.join.mode);
  setJoin(JOIN_MODES[(i + 1) % JOIN_MODES.length]);
}
function setJoin(mode) {
  const extra = { mode };
  // entering DETUNE with no detune set yet → give it a musical default
  if (mode === 'detune' && !live.join.detune) { extra.detune = DEFAULT_DETUNE; live.join.detune = DEFAULT_DETUNE; }
  cmd('MODULE_JOIN', extra);
  live.join.mode = mode;            // optimistic
  markHold(); render();
}

function sendLevel(key, v, isFinal) {
  const m = MODULES.find(x => x.key === key); if (!m) return;
  const ch = channelOf(key);
  if (ch === undefined && key !== 'drums' && key !== 'sampler' && key !== 'bbkit') return; // out of fold, no channel
  const d = levelDescriptor(m, ch);
  const now = Date.now();
  if (!isFinal && now - (_lastSent[key] || 0) < 45) return;  // throttle drag spam
  _lastSent[key] = now;
  cmd(d.action, d.extra(v));
  if (isFinal) markHold();   // let synth_cc catch up before the poll reconciles this fader
}

/* Follow a level changed elsewhere (another web client, another page) — synth_cc
   in /state is the last value forwarded via SYNTH_CC. Skips any fader being
   dragged, and the whole pass is gated by holdUntil after a local change so a
   just-set value never snaps back to a stale poll. Note: a Fire knob sends the
   CC straight to the bus (not via SYNTH_CC), so it won't appear here — same
   limitation as every other page. */
function reconcileLevels() {
  const dragging = document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.lvl : null;
  MODULES.forEach(m => {
    if (m.key === dragging) return;
    const ch = channelOf(m.key);
    const rb = levelDescriptor(m, ch).read();
    if (rb !== null && lvlVal[m.key] !== rb) lvlVal[m.key] = rb;
  });
}

/* ─── poll ────────────────────────────────────────────────── */
function onState(d) {
  if (Date.now() < holdUntil) return;          // let an optimistic action settle
  if (d.bindings) live.bindings = Object.assign({}, d.bindings);
  // Any module the engine knows that this list lacks still gets a row (so it
  // can always be moved / taken out); _test_matrix_modules keeps the list whole.
  if (Array.isArray(d.module_keys)) d.module_keys.forEach(k => {
    if (!MODULES.some(m => m.key === k))
      MODULES.push({ key: k, name: k.toUpperCase(), fam: 'module', code: k.slice(0, 4).toUpperCase(), color: '#888888' });
  });
  if (d.join) live.join = Object.assign({ mode: 'none', detune: 0, spread: false }, d.join);
  if (Array.isArray(d.reserved) && !prot) live.reserved = d.reserved.slice();   // /api/protected_channels wins once loaded
  if (d.synth_cc) live.cc = d.synth_cc;
  reconcileLevels();
  render();
}

/* ─── protected channels (2026-10-14): /api/protected_channels, the one live list every picker refuses ─── */
let prot = null;             // {"1": {on, name, locked}, ...}
function applyProt(j) {
  if (!j || !j.channels) return;
  prot = j.channels;
  live.reserved = Object.keys(prot).filter(c => prot[c].on).map(Number);
  for (const c of Object.keys(prot)) if (prot[c].on && prot[c].name) RESLAB[c] = prot[c].name.toUpperCase().slice(0, 6);
}
function renderProt() {
  const host = $('prot'); if (!host || !prot) return;
  const occ = occByChannel();
  host.textContent = '';
  let n = 0;
  for (let c = 1; c <= 16; c++) {
    const e = prot[String(c)] || { on: false, name: '' };
    if (e.on) n++;
    const cell = document.createElement('div');
    cell.className = 'cell ' + (e.on ? 'reserved' : 'empty') + (e.locked ? '' : ' pickable');
    const a = document.createElement('span'); a.className = 'ch'; a.textContent = String(c).padStart(2, '0');
    const b = document.createElement('span'); b.className = 'tag';
    b.textContent = e.on ? ((e.name || 'PROT').toUpperCase().slice(0, 6) + (e.locked ? ' 🔒' : '')) : (occ[c] ? 'in use' : '—');
    cell.append(a, b);
    cell.title = e.locked ? 'always protected' : (e.on ? 'tap to unprotect' : 'tap to protect');
    if (!e.locked) cell.onclick = () => toggleProt(c, !e.on, e.name);
    host.append(cell);
  }
  $('prot-count').textContent = `${n} protected`;
}
async function toggleProt(c, on, name) {
  const body = { channel: c, on };
  if (on && !name) {
    const nm = prompt(`Name for ch ${c} (what it's kept for, optional):`, '');
    if (nm === null) return;
    body.name = nm;
  }
  try {
    const r = await fetch('/api/protected_channels', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                      body: JSON.stringify(body) });
    const j = await r.json();
    $('prot-msg').textContent = j.ok ? '' : (j.error || 'refused');
    if (j.ok) applyProt(j);
  } catch (e) { $('prot-msg').textContent = 'error: ' + e; }
  render();
}
async function loadProt() {
  try { const r = await fetch('/api/protected_channels'); if (r.ok) applyProt(await r.json()); } catch (e) {}
  render();
}

/* ─── boot ────────────────────────────────────────────────── */
(async function () {
  try {
    const r = await fetch('/static/cc_map.json');
    if (r.ok) { const j = await r.json(); ccMap = j.modules || null; }
  } catch (e) {}
  const c = $('cancel'); if (c) c.onclick = cancel;
  render();
  loadProt();
  ORC.subscribe(onState);
})();
