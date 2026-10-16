let PORTS = ['pisound','rtp'];   // populated dynamically from /api/midi_ports on load
const TYPES = ['any','note','cc','pc'];
const TRANSFORMS = ['','bank_align','transpose'];

let _state = {version:1, devices:{}};
let _active = new Set();

function toast(msg, err=false){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (err?' err':'');
  setTimeout(()=>el.className='toast', 2500);
}

// ── Chord engine output ───────────────────────────────────────────────────────
// Short labels for the well-known external presets shown as quick-select buttons.
const PRESET_LABELS = {
  'MONO_CLEAN': 'CLEAN',
  'MONO_BASS':  'BASS SAME CH',
  'MONO_BASS2': 'BASS→CH2',
  'BASS_SPLIT': 'BASS SPLIT',
  'LEAD_OUT':   'LEAD OUT',
  'FULL_SPLIT': 'FULL SPLIT',
  'MPC_MAC':    'MPC+MAC',
  'USER_1':'USER 1','USER_2':'USER 2','USER_3':'USER 3','USER_4':'USER 4',
};

let _engineState = {};

function renderChordOut(s){
  _engineState = s;
  const presets = s.presets_external || [];
  const cur     = s.preset || '';
  const co      = s.chord_out || {};

  // Preset buttons
  const pEl = document.getElementById('chord-presets');
  if(pEl) pEl.innerHTML = presets.map(p=>`
    <button class="btn${p===cur?' accent':''}" style="font-size:8px;padding:4px 7px"
      onclick="setPreset('${p}')">${PRESET_LABELS[p]||p}</button>`).join('');

  // Port selector
  const portSel = document.getElementById('co-port');
  if(portSel) portSel.innerHTML = '<option value="">preset routing</option>'
    + PORTS.map(k=>`<option value="${k}"${co.port===k?' selected':''}>${k}</option>`).join('');

  // Channel selector
  const chSel = document.getElementById('co-ch');
  if(chSel) chSel.value = co.channel || '';

  // Status
  const st = document.getElementById('co-status');
  if(st){
    const presetDesc = cur ? (PRESET_LABELS[cur]||cur) : '—';
    if(co.port) st.textContent = `→ ${co.port} ch${co.channel||'(preset)'}  |  preset: ${presetDesc}`;
    else        st.textContent = `preset: ${presetDesc}`;
  }
}

async function setPreset(name){
  try{
    await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'SET_PRESET',preset:name})});
    setTimeout(load, 300);   // reload to show updated preset highlight
  } catch(e){ toast('error: '+e, true); }
}

async function applyChordOut(){
  const port = document.getElementById('co-port').value || null;
  const ch   = parseInt(document.getElementById('co-ch').value) || null;
  if(!port){ clearChordOut(); return; }
  try{
    await fetch('/api/chord_out',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({port, channel:ch})});
    setTimeout(load, 200);
  } catch(e){ toast('error: '+e, true); }
}

async function clearChordOut(){
  try{ await fetch('/api/chord_out',{method:'DELETE'}); setTimeout(load, 200); }
  catch(e){ toast('error: '+e, true); }
}

// ── Perform-VE (HardTune NaturalPlay key feed) ────────────────────────────────
let _pveFeed = false;

function renderPerformve(s){
  _pveFeed = !!s.performve_feed;
  const ps = s.performve_src || {};
  const fb = document.getElementById('pve-feed-btn');
  if(fb){ fb.textContent = 'FOLLOW: ' + (_pveFeed?'ON':'off'); fb.className = 'btn' + (_pveFeed?' accent':''); }
  const fs = document.getElementById('pve-feed-status');
  if(fs) fs.textContent = _pveFeed ? 'chord engine → box ch1' : 'chord-engine feed off';
  const sp = document.getElementById('pve-src-port');
  if(sp) sp.value = ps.port || '';
  const sc = document.getElementById('pve-src-ch');
  if(sc) sc.value = ps.channel || '';
  const ss = document.getElementById('pve-src-status');
  if(ss) ss.textContent = ps.port ? `claiming ${ps.port} ch${ps.channel||'(any)'} → box (consumed)` : 'no external source';
}

async function togglePerformveFeed(){
  try{
    await fetch('/api/performve_feed',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({on: !_pveFeed})});
    setTimeout(load, 200);
  } catch(e){ toast('error: '+e, true); }
}

async function applyPerformveSrc(){
  const port = document.getElementById('pve-src-port').value || null;
  const ch   = parseInt(document.getElementById('pve-src-ch').value) || null;
  try{
    await fetch('/api/performve_src',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({port, channel:ch})});
    setTimeout(load, 200);
  } catch(e){ toast('error: '+e, true); }
}

// ── Melody snap output ────────────────────────────────────────────────────────

function renderMelodyOut(s){
  const mo = s.melody_out || {};
  const portSel = document.getElementById('mo-port');
  if(portSel) portSel.innerHTML = '<option value="">pisound (default)</option>'
    + PORTS.map(k=>`<option value="${k}"${mo.port===k?' selected':''}>${k}</option>`).join('');
  const chSel = document.getElementById('mo-ch');
  if(chSel) chSel.value = mo.channel || '';
  const st = document.getElementById('mo-status');
  if(st) st.textContent = mo.port ? `→ ${mo.port} ch${mo.channel||15}` : 'pisound ch15 (default)';
}

async function applyMelodyOut(){
  const port = document.getElementById('mo-port').value || null;
  const ch   = parseInt(document.getElementById('mo-ch').value) || null;
  const action = port ? 'SET_MELODY_OUT' : 'CLEAR_MELODY_OUT';
  try{
    await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action, port, channel:ch})});
    setTimeout(load, 200);
  } catch(e){ toast('error: '+e, true); }
}

async function clearMelodyOut(){
  try{
    await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'CLEAR_MELODY_OUT'})});
    setTimeout(load, 200);
  } catch(e){ toast('error: '+e, true); }
}

// ── Voice map ─────────────────────────────────────────────────────────────────

const VOICE_SLOTS = [
  { key: 'CHORD_ALL',  label: 'ALL'  },
  { key: 'CHORD_ROOT', label: 'ROOT' },
  { key: 'CHORD_3RD',  label: '3RD'  },
  { key: 'CHORD_5TH',  label: '5TH'  },
  { key: 'CHORD_7TH',  label: '7TH'  },
  { key: 'CHORD_9TH',  label: '9TH'  },
  { key: 'BASS',       label: 'BASS' },
  { key: 'LEAD',       label: 'LEAD' },
  { key: 'ARP_DEST',   label: 'ARP'  },
];

let _voiceMap  = {};  // current live overrides (from engine state)
let _presetMap = {};  // current preset's slot definitions (for hint column)
let _vmBindings = {}; // live /matrix bindings {module: ch}
let _vmModules  = []; // every module the engine knows (state.module_keys)

// Channel picker for one voice-map row. Modules first -- on the SYNTH port only
// (a slot that names a module follows it on /matrix, 2026-09-24) -- then plain
// channels 1-16. EVERY module is listed, unbound ones marked: listing only the
// bound ones made a saved entry naming an unbound module lose its value, and
// APPLY then saved channel:null (2026-10-01). A saved value not in the list
// (e.g. a module name on a non-synth port) is kept as its own option.
function _chOpts(port, cur) {
  const mods = port === 'synth' ? _vmModules : [];
  let html = '<option value="">—</option>';
  if (mods.length) html += '<optgroup label="synth module">' + mods.map(m => {
    const ch = _vmBindings[m];
    return `<option value="${m}">${m} (${ch ? 'ch' + ch : 'unbound'})</option>`;
  }).join('') + '</optgroup>';
  html += [...Array(16)].map((_,i)=>`<option value="${i+1}">${i+1}</option>`).join('');
  const c = (cur === undefined || cur === null) ? '' : String(cur);
  if (c && !/^\d+$/.test(c) && !mods.includes(c))
    html += `<option value="${c}">${c} (not on this port)</option>`;
  return html;
}

function _presetSlotHint(key) {
  const slot = _presetMap[key];
  if (!slot) return '—';
  if (typeof slot.channel === 'string') {       // a synth slot that NAMES its module
    const live = ORC.boundCh(slot.channel);
    return `${slot.port} ${slot.channel} (${live ? 'ch' + live : 'not bound'})`;
  }
  return `${slot.port} ch${slot.channel}`;
}

function _portOpts(cur) {
  // Voice-map only. 'synth' (the internal bus) belongs here even though PORTS
  // leaves it out: the NES/POLY presets route to it, and without the option
  // "load preset" blanked those slots' port and APPLY silenced them (2026-09-28).
  return ['preset','off','synth',...PORTS.filter(p => p !== 'synth')]
    .map(p => `<option value="${p}"${cur===p?' selected':''}>${p}</option>`)
    .join('');
}

function renderVoiceMap(s) {
  _voiceMap  = s.voice_map  || {};
  _presetMap = s.preset_map || {};
  const grid = document.getElementById('vm-grid');
  if (!grid) return;

  _vmBindings = s.bindings || {};
  _vmModules  = Array.isArray(s.module_keys) && s.module_keys.length
    ? s.module_keys.slice() : Object.keys(_vmBindings).sort();

  grid.innerHTML = `<thead><tr>
    <th style="text-align:left;color:var(--label);padding:3px 8px 3px 0;font-weight:normal;white-space:nowrap">SLOT</th>
    <th style="text-align:left;color:var(--label);padding:3px 8px 3px 0;font-weight:normal">PORT</th>
    <th style="text-align:left;color:var(--label);padding:3px 8px 3px 0;font-weight:normal">CH</th>
    <th style="text-align:left;color:var(--label);padding:3px 0;font-weight:normal;font-size:9px">PRESET ROUTING</th>
  </tr></thead><tbody>`
  + VOICE_SLOTS.map(({key, label}) => {
    const entry   = _voiceMap[key];
    const isOff   = entry === null;
    const isSet   = entry !== undefined && entry !== null;
    const curPort = isOff ? 'off' : (isSet ? (entry.port || 'preset') : 'preset');
    const dimRow  = curPort === 'preset' ? 'opacity:0.5' : '';
    const chDim   = (curPort === 'preset' || curPort === 'off') ? 'opacity:0.35;pointer-events:none' : '';
    return `<tr id="vmr-${key}" style="${dimRow}">
      <td style="padding:3px 8px 3px 0;color:var(--bright);white-space:nowrap">${label}</td>
      <td style="padding:3px 8px 3px 0">
        <select id="vm-port-${key}"
          style="background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 4px;border-radius:2px"
          onchange="vmPortChange('${key}')">
          ${_portOpts(curPort)}
        </select>
      </td>
      <td style="padding:3px 8px 3px 0">
        <select id="vm-ch-${key}"
          style="background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 4px;border-radius:2px;${chDim}">
          ${_chOpts(curPort, isSet ? entry.channel : '')}
        </select>
      </td>
      <td style="padding:3px 0;color:var(--muted);font-size:9px">${_presetSlotHint(key)}</td>
    </tr>`;
  }).join('')
  + '</tbody>';

  // Set channel select values after innerHTML (so <option> elements exist)
  VOICE_SLOTS.forEach(({key}) => {
    const entry = _voiceMap[key];
    if (entry && entry !== null) {
      const chEl = document.getElementById('vm-ch-' + key);
      if (chEl) chEl.value = entry.channel || '';
    }
  });

  // Status line
  const nActive = Object.values(_voiceMap).filter(v => v !== null).length;
  const nSilent = Object.values(_voiceMap).filter(v => v === null).length;
  const st = document.getElementById('vm-status');
  if (st) st.textContent = (nActive + nSilent) > 0
    ? `${nActive} override(s), ${nSilent} silence(s) active`
    : 'following preset';
}

function vmPortChange(key) {
  const portEl = document.getElementById('vm-port-' + key);
  const chEl   = document.getElementById('vm-ch-'   + key);
  const row    = document.getElementById('vmr-'     + key);
  const p = portEl ? portEl.value : 'preset';
  const dim  = p === 'preset' || p === 'off';
  if (chEl) {
    // Module names belong to the synth port only: rebuild the picker for the
    // new port, keeping the pick when it is still offered there.
    const keep = chEl.value;
    chEl.innerHTML = _chOpts(p, /^\d+$/.test(keep) ? keep : '');
    if ([...chEl.options].some(o => o.value === keep)) chEl.value = keep;
    chEl.style.opacity       = dim ? '0.35' : '';
    chEl.style.pointerEvents = dim ? 'none'  : '';
  }
  if (row) row.style.opacity = p === 'preset' ? '0.5' : '';
}

function _buildVoiceMapPayload() {
  const vm = {};
  VOICE_SLOTS.forEach(({key}) => {
    const portEl = document.getElementById('vm-port-' + key);
    const chEl   = document.getElementById('vm-ch-'   + key);
    if (!portEl) return;
    const p = portEl.value;
    if (p === 'preset') return;          // absent = follow preset
    if (p === 'off')    { vm[key] = null; return; }
    const cv = chEl ? chEl.value : '';
    vm[key] = { port: p, channel: /^\d+$/.test(cv) ? parseInt(cv) : (cv || null) };
  });
  return vm;
}

async function applyVoiceMap() {
  const vm = _buildVoiceMapPayload();
  try {
    await fetch('/cmd', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ action: 'SET_VOICE_MAP', voice_map: vm }) });
    setTimeout(load, 300);
  } catch(e) { toast('error: ' + e, true); }
}

async function clearVoiceMap() {
  try {
    await fetch('/cmd', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ action: 'CLEAR_VOICE_MAP' }) });
    setTimeout(load, 300);
  } catch(e) { toast('error: ' + e, true); }
}

function loadPresetIntoVoiceMap() {
  // Populate the editor rows from the active preset's slot definitions.
  // The user still needs to click APPLY to activate them as overrides.
  VOICE_SLOTS.forEach(({key}) => {
    const portEl = document.getElementById('vm-port-' + key);
    const chEl   = document.getElementById('vm-ch-'   + key);
    if (!portEl) return;
    const slot = _presetMap[key];
    portEl.value = !slot ? 'off' : (slot.port || 'preset');
    vmPortChange(key);          // FIRST: builds the picker for the new port
    if (slot && chEl) chEl.value = slot.channel || '';
  });
  toast('preset loaded into editor — click APPLY to activate');
}

// ── Save voice_map as a named User Preset ─────────────────────────────────────
// Merges the current editor state (voice_map) on top of the active preset and
// saves the result as the chosen USER_N slot — live and persistent (no restart).

async function saveAsPreset() {
  const name = document.getElementById('vm-save-slot')?.value;
  if (!name) return;
  const stEl  = document.getElementById('vm-save-status');
  if (stEl) stEl.textContent = 'saving…';
  try {
    // 1. Apply the current editor state as a live voice_map first
    const vm = _buildVoiceMapPayload();
    await fetch('/cmd', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ action: 'SET_VOICE_MAP', voice_map: vm }) });
    // 2. Save as preset — engine merges voice_map into preset, clears voice_map
    await fetch('/cmd', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ action: 'SAVE_AS_PRESET', preset: name }) });
    if (stEl) stEl.textContent = `✓ saved as ${name} (active + persistent)`;
    setTimeout(() => { if (stEl) stEl.textContent = ''; }, 4000);
    setTimeout(load, 350);
    toast(`routing saved as ${name}`);
  } catch(e) {
    if (stEl) stEl.textContent = 'error — see console';
    toast('save failed: ' + e, true);
  }
}

// ── Main load ─────────────────────────────────────────────────────────────────

// ── Voice MIDI thru ───────────────────────────────────────────────────────────

let _vtData = {thru:{}, voices:[], ports:[]};

function renderVoiceThru(d){
  _vtData = d;
  const el = document.getElementById('vt-grid');
  if(!el) return;
  const ports = d.ports.length ? d.ports : PORTS;
  const portOpts = ports.map(k=>`<option value="${k}">${k}</option>`).join('');
  const chOpts = [...Array(16)].map((_,i)=>`<option value="${i+1}">${i+1}</option>`).join('');

  const hdrStyle = 'padding:4px 6px;font-family:var(--fh);font-size:8px;letter-spacing:.1em;color:var(--label);border-bottom:1px solid var(--border)';
  let html = `<thead><tr>
    <th style="${hdrStyle}">VOICE</th>
    <th style="${hdrStyle}">IN CH</th>
    <th style="${hdrStyle}">→ PORT</th>
    <th style="${hdrStyle}">→ CH</th>
    <th style="${hdrStyle}"></th>
  </tr></thead><tbody>`;

  for(const v of d.voices){
    const k   = String(v.ch);
    const cur = d.thru[k] || {};
    const active = !!cur.port;
    const rowStyle = `border-bottom:1px solid var(--border);${active?'background:rgba(var(--accent-rgb,80,140,255),.07)':''}`;
    const selStyle = 'background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 4px;border-radius:2px';
    const portSel  = `<select id="vt-port-${k}" style="${selStyle}"><option value="">off</option>${portOpts}</select>`;
    const chSel    = `<select id="vt-ch-${k}"   style="${selStyle}">${chOpts}</select>`;
    html += `<tr style="${rowStyle}">
      <td style="padding:4px 6px;color:${active?'var(--accent)':'var(--body)'}">${v.name}</td>
      <td style="padding:4px 6px;color:var(--muted)">${v.ch}</td>
      <td style="padding:4px 6px">${portSel}</td>
      <td style="padding:4px 6px">${chSel}</td>
      <td style="padding:4px 6px">
        <button class="btn${active?' accent':''}" style="font-size:8px;padding:2px 7px"
          onclick="applyVoiceThru(${v.ch})">SET</button>
        ${active?`<button class="btn" style="font-size:8px;padding:2px 7px;margin-left:3px"
          onclick="clearVoiceThru(${v.ch})">✕</button>`:''}
      </td>
    </tr>`;
  }
  html += '</tbody>';
  el.innerHTML = html;

  // Restore current selections after rendering
  for(const v of d.voices){
    const k = String(v.ch);
    const cur = d.thru[k] || {};
    const portEl = document.getElementById(`vt-port-${k}`);
    const chEl   = document.getElementById(`vt-ch-${k}`);
    if(portEl && cur.port) portEl.value = cur.port;
    if(chEl   && cur.channel) chEl.value = String(cur.channel);
  }
}

async function applyVoiceThru(srcCh){
  const portEl = document.getElementById(`vt-port-${srcCh}`);
  const chEl   = document.getElementById(`vt-ch-${srcCh}`);
  const port   = portEl ? portEl.value : '';
  const ch     = chEl   ? parseInt(chEl.value) : 0;
  if(!port){ await clearVoiceThru(srcCh); return; }
  try{
    await fetch('/api/voice_thru',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src_ch:srcCh, port, channel:ch})});
    setTimeout(loadVoiceThru, 200);
  } catch(e){ toast('MIDI thru error: '+e, true); }
}

async function clearVoiceThru(srcCh){
  try{
    await fetch(`/api/voice_thru/${srcCh}`,{method:'DELETE'});
    setTimeout(loadVoiceThru, 200);
  } catch(e){ toast('MIDI thru error: '+e, true); }
}

async function loadVoiceThru(){
  try{
    const d = await fetch('/api/voice_thru').then(r=>r.json());
    renderVoiceThru(d);
  } catch(e){ /* non-fatal */ }
}

async function load(){
  try{
    // Fetch midi ports to keep the PORTS selector list in sync with reality
    const [r, s, portsData, vtData] = await Promise.all([
      fetch('/api/routes').then(r=>r.json()),
      fetch('/state?'+Date.now()).then(r=>r.json()).catch(()=>({})),
      fetch('/api/midi_ports').then(r=>r.json()).catch(()=>null),
      fetch('/api/voice_thru').then(r=>r.json()).catch(()=>null),
    ]);
    if (portsData && portsData.ports) {
      // Use port keys (exclude 'synth' which is always internal)
      PORTS = portsData.ports
        .filter(p => p.key !== 'synth')
        .map(p => p.key);
      if (!PORTS.length) PORTS = ['pisound'];
    }
    _state = r;
    _active = new Set(s.active_routes || []);
    renderChordOut(s);
    renderVoiceMap(s);
    renderMelodyOut(s);
    renderPerformve(s);
    if(vtData) renderVoiceThru(vtData);
    render();
  } catch(e){ toast('load failed: '+e, true); }
}

// CH_CTRL(13) is hard-guarded in the brain (a pre-rule can never claim it — the
// ORC's control inbox). 12/14/15 are default conveniences a pre-rule CAN claim,
// but doing so preempts the engine, so warn.
const _PRE_WARN = {13:'CH13 is the ORC control inbox — RESERVED, this pre-rule will NOT claim it',
                   14:'CH14 is the MPC control passthrough — a pre-claim overrides it',
                   12:'CH12 is chord-input — a pre-claim overrides the chord engine',
                   15:'CH15 is melody-snap — a pre-claim overrides it'};
// Mirrors orc1_routes.is_rule(): comments and parked "_disabled" examples are not
// rules, so they are neither listed here nor matched by the engine.
const _RULE_KEYS = ['in_port','in_ch','in_type','match','out_port','out_ch','transform','action','stage'];
function isRule(r){
  return !!r && typeof r==='object' && !('_disabled' in r) && _RULE_KEYS.some(k=>k in r);
}

function ruleLabel(r){
  const ch = r.in_ch==='any'?'any':'CH'+r.in_ch;
  const ty = r.in_type||'any';
  const match = r.match ? r.match[0]+'–'+r.match[1] : 'all';
  const out = (r.out_port||'pisound')+(r.out_ch&&r.out_ch!=='same'?' CH'+r.out_ch:'');
  const tf = r.transform ? ' <span class="hi">'+r.transform+'</span>' : '';
  const action = r.action==='consume'?'<span style="color:var(--accent2)">consume</span>':'→ <span class="port">'+out+'</span>';
  const stage = r.stage==='pre' ? '<span style="color:var(--accent2);font-weight:bold">PRE </span>' : '';
  const src = (r.in_port && r.in_port!=='any')
    ? '<span class="hi">'+(r.in_port==='self' ? 'from this device' : r.in_port)+'</span> ' : '';
  const wtxt = r.stage!=='pre' ? '' :
    (r.in_ch==='any' || r.in_ch===undefined)
      ? 'ANY channel — this pre-rule claims EVERY channel from its source (except CH13): chord input, drums, synths'+(r.action==='consume'?' — and CONSUME silences them all':'')
      : (_PRE_WARN[r.in_ch] || '');
  const warn = wtxt ? ` <span style="color:#e66;cursor:help" title="${wtxt}">⚠</span>` : '';
  return `${stage}${src}<span class="hi">${ch}</span> ${ty} [${match}] ${action}${tf}${warn}`;
}

function toggleRule(devKey, rIdx){
  const rules = _state.devices[devKey].rules;
  rules[rIdx].enabled = !rules[rIdx].enabled;
  renderDevice(devKey);
}

function deleteRule(devKey, rIdx){
  _state.devices[devKey].rules.splice(rIdx, 1);
  renderDevice(devKey);
}

function toggleDev(devKey){
  const body = document.getElementById('db-'+devKey);
  const icon = document.getElementById('di-'+devKey);
  body.classList.toggle('open');
  icon.classList.toggle('open');
}

function renderDevice(devKey){
  const dev = _state.devices[devKey];
  const isActive = _active.has(devKey);
  const el = document.getElementById('dev-'+devKey);
  if(!el) return render();
  // Keep each rule's index in the FULL list: toggleRule/deleteRule act on
  // dev.rules[i], and hidden placeholders would otherwise shift every index.
  const rules = (dev.rules||[]).map((r,i)=>({r,i})).filter(({r})=>isRule(r));
  el.innerHTML = `
    <div class="card${isActive?' active-dev':''}" id="dev-${devKey}">
      <div class="card-head" onclick="toggleDev('${devKey}')">
        <div class="dev-dot${isActive?' live':''}"></div>
        <div class="dev-name">${devKey}</div>
        <div class="dev-match">${dev.always ? '<span style="color:var(--accent2)">ALWAYS ON</span>' : (dev.port_match||devKey)}</div>
        <div class="dev-toggle" id="di-${devKey}">▶</div>
      </div>
      <div class="dev-body open" id="db-${devKey}">
        <div class="rules-list">
          ${rules.length===0?'<div class="no-rules">no rules — add one below</div>':
            rules.map(({r,i})=>`
              <div class="rule-row">
                <div class="rule-en${r.enabled!==false?' on':''}" onclick="toggleRule('${devKey}',${i})" title="${r.enabled!==false?'enabled':'disabled'}"></div>
                <div class="rule-text">${ruleLabel(r)}</div>
                <button class="rule-del" onclick="deleteRule('${devKey}',${i})" title="delete">✕</button>
              </div>`).join('')}
        </div>
        <button class="add-rule-btn" onclick="openAddRule('${devKey}')">+ ADD RULE</button>
        <div class="rule-form" id="rf-${devKey}">
          ${ruleForm(devKey)}
        </div>
        <div class="dev-footer">
          <button class="btn danger" onclick="deleteDev('${devKey}')">DELETE DEVICE</button>
        </div>
      </div>
    </div>`;
}

function ruleForm(devKey){
  const dev = _state.devices[devKey] || {};
  // "this device only" = in_port "self": the rule hears ONLY the device this
  // profile belongs to. An always-on profile has no device, so it's not offered.
  const selfOpt = dev.always ? '' :
    `<option value="self">this device only (${dev.port_match||devKey})</option>`;
  return `
    <div class="rf-lbl">IN CHANNEL</div>
    <select class="rf-sel" id="rf-ch-${devKey}">
      <option value="any">any</option>
      ${[...Array(16)].map((_,i)=>`<option value="${i+1}">${i+1}</option>`).join('')}
    </select>
    <div class="rf-grid" style="margin-top:6px">
      <div>
        <div class="rf-lbl">SOURCE (in port)</div>
        <select class="rf-sel" id="rf-src-${devKey}">
          <option value="any">any input</option>
          ${selfOpt}
          <option value="usb">any usb</option>
          <option value="din">din</option>
        </select>
      </div>
      <div>
        <div class="rf-lbl">STAGE</div>
        <select class="rf-sel" id="rf-st-${devKey}">
          <option value="post">post (default)</option>
          <option value="pre">pre — claim any channel</option>
        </select>
      </div>
      <div>
        <div class="rf-lbl">TYPE</div>
        <select class="rf-sel" id="rf-ty-${devKey}">
          ${TYPES.map(t=>`<option>${t}</option>`).join('')}
        </select>
      </div>
      <div>
        <div class="rf-lbl">RANGE (min–max)</div>
        <div class="rf-row">
          <input class="rf-inp" id="rf-lo-${devKey}" type="number" min="0" max="127" placeholder="0" style="width:48px">
          <span>–</span>
          <input class="rf-inp" id="rf-hi-${devKey}" type="number" min="0" max="127" placeholder="127" style="width:48px">
        </div>
      </div>
      <div>
        <div class="rf-lbl">OUT PORT</div>
        <select class="rf-sel" id="rf-op-${devKey}">
          ${PORTS.map(p=>`<option>${p}</option>`).join('')}
        </select>
      </div>
      <div>
        <div class="rf-lbl">OUT CHANNEL</div>
        <select class="rf-sel" id="rf-oc-${devKey}">
          <option value="same">same</option>
          ${[...Array(16)].map((_,i)=>`<option value="${i+1}">${i+1}</option>`).join('')}
        </select>
      </div>
      <div>
        <div class="rf-lbl">TRANSFORM</div>
        <select class="rf-sel" id="rf-tf-${devKey}">
          <option value="">none</option>
          <option value="bank_align">bank_align</option>
          <option value="transpose">transpose</option>
        </select>
      </div>
      <div>
        <div class="rf-lbl">ACTION</div>
        <select class="rf-sel" id="rf-ac-${devKey}">
          <option value="forward">forward</option>
          <option value="consume">consume</option>
        </select>
      </div>
    </div>
    <div class="rf-btns">
      <button class="btn accent" onclick="saveRule('${devKey}')">ADD</button>
      <button class="btn" onclick="closeAddRule('${devKey}')">CANCEL</button>
    </div>`;
}

function openAddRule(devKey){
  document.getElementById('rf-'+devKey).classList.add('open');
}
function closeAddRule(devKey){
  document.getElementById('rf-'+devKey).classList.remove('open');
}

function saveRule(devKey){
  const g = id => document.getElementById(id+'-'+devKey);
  const lo = parseInt(g('rf-lo').value||0);
  const hi = parseInt(g('rf-hi').value||127);
  const rule = {
    in_ch:  g('rf-ch').value==='any'?'any':parseInt(g('rf-ch').value),
    in_type: g('rf-ty').value,
    match:  [lo, hi],
    out_port: g('rf-op').value,
    out_ch: g('rf-oc').value==='same'?'same':parseInt(g('rf-oc').value),
    action: g('rf-ac').value,
    enabled: true
  };
  const tf = g('rf-tf').value;
  if(tf) rule.transform = tf;
  const src = g('rf-src').value;
  if(src && src!=='any') rule.in_port = src;   // scope to a source (self/usb/din)
  if(g('rf-st').value==='pre') rule.stage = 'pre';   // claim BEFORE channel dispatch
  if(rule.in_type==='any') delete rule.in_type;
  if(rule.match[0]===0 && rule.match[1]===127) delete rule.match;
  _state.devices[devKey].rules = _state.devices[devKey].rules||[];
  _state.devices[devKey].rules.push(rule);
  renderDevice(devKey);
  closeAddRule(devKey);
}

function deleteDev(devKey){
  if(!confirm('Delete device profile "'+devKey+'"?')) return;
  delete _state.devices[devKey];
  render();
}

function addDevice(){
  const name  = document.getElementById('nd-name').value.trim();
  const match = document.getElementById('nd-match').value.trim();
  const desc  = document.getElementById('nd-desc').value.trim();
  const always = document.getElementById('nd-always').checked;
  if(!name){ toast('profile name required', true); return; }
  if(_state.devices[name]){ toast('profile already exists', true); return; }
  _state.devices[name] = always
    ? { description: desc, port_match: '(always on)', always: true, rules: [] }
    : { description: desc, port_match: match||name, rules: [] };
  document.getElementById('nd-name').value='';
  document.getElementById('nd-match').value='';
  document.getElementById('nd-desc').value='';
  document.getElementById('nd-always').checked=false;
  render();
  toast('added — press SAVE & RELOAD to apply');
}

function render(){
  const devEl = document.getElementById('devices');
  const keys  = Object.keys(_state.devices||{});
  if(keys.length===0){
    devEl.innerHTML = '<div style="font-size:9px;color:var(--muted);font-style:italic;padding:6px 0">no device profiles — add one below</div>';
  } else {
    devEl.innerHTML = keys.map(k=>`<div id="dev-${k}"></div>`).join('');
    keys.forEach(k=>renderDevice(k));
  }
  // Static rules (read-only display)
  const sr = _state.static_rules||[];
  document.getElementById('static-rules').innerHTML = sr.length===0?
    '<div class="no-rules">none defined</div>':
    sr.map(r=>`<div class="rule-row"><div class="rule-text">${ruleLabel(r)}</div></div>`).join('');
}

async function saveAndReload(){
  try{
    const res = await fetch('/api/routes',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(_state)
    });
    const d = await res.json();
    if(d.ok) toast('saved & reloaded');
    else toast('save failed', true);
  } catch(e){ toast('error: '+e, true); }
}

async function reloadFromDisk(){
  await load();
  toast('reloaded from disk');
}

load();
