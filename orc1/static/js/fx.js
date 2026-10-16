// ORC-1 FX page — chain list + FX I/O card
'use strict';

// ── FX I/O card ──────────────────────────────────────────────────────────────

let _io = null;
let _ioDirty = false;

async function loadIO() {
  try {
    const r = await fetch('/api/fx/io');
    _io = await r.json();
    renderIO();
  } catch (e) {
    document.getElementById('io-status').textContent = 'error loading I/O';
  }
}

function renderIO() {
  if (!_io) return;
  document.getElementById('io-sl').value = _io.send_l   || '';
  document.getElementById('io-sr').value = _io.send_r   || '';
  document.getElementById('io-rl').value = _io.return_l || '';
  document.getElementById('io-rr').value = _io.return_r || '';

  // Highlight active preset button
  document.getElementById('io-hw-btn').classList.toggle('active', _io.mode === 'hardware');
  document.getElementById('io-int-btn').classList.toggle('active', _io.mode === 'internal');

  // INTERNAL source picker (by module NAME; the ports above are resolved live)
  const sel = document.getElementById('io-mod');
  if (_io.modules && sel.options.length <= 1) {
    for (const m of _io.modules) {
      const o = document.createElement('option');
      o.value = m.key; o.textContent = m.label;
      sel.appendChild(o);
    }
  }
  sel.value = _io.module || '';

  // Port availability warning
  const av  = _io.available || {};
  const bad = Object.keys(av).filter(k => !av[k]);
  const warn = document.getElementById('io-warn');
  const modLabel = (_io.modules || []).find(m => m.key === _io.module)?.label || _io.module;
  if (_io.mode === 'internal' && !_io.module) {
    warn.textContent = 'INTERNAL has no source module — pick one above';
    warn.style.display = '';
  } else if (_io.mode === 'internal' && _io.return_none) {
    warn.textContent = `${modLabel}'s output is set to off on /patch — the rack has nowhere to return`;
    warn.style.display = '';
  } else if (bad.length) {
    const names = { send_l:'SEND L', send_r:'SEND R', return_l:'RETURN L', return_r:'RETURN R' };
    warn.textContent = `Port(s) not found in JACK: ${bad.map(k => names[k] || k).join(', ')}`;
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
  clearDirty();
}

function markDirty() {
  _ioDirty = true;
  document.getElementById('io-apply-btn').style.display = '';
}

function clearDirty() {
  // Every render calls this (incl. the state poll's re-GET), so only wipe the
  // status line when there was an edit to discard -- not a fresh confirmation.
  if (_ioDirty) document.getElementById('io-status').textContent = '';
  _ioDirty = false;
  document.getElementById('io-apply-btn').style.display = 'none';
}

function _postIO(body) {
  return fetch('/api/fx/io', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  })
  .then(r => r.json())
  .then(d => {
    const st = document.getElementById('io-status');
    if (!d.ok) { st.textContent = d.error || 'error'; return; }
    // Re-GET so the port-availability check runs against the new ports.
    loadIO().then(() => {
      const m = (_io.modules || []).find(x => x.key === _io.module);
      st.textContent = _io.mode === 'internal'
        ? `INTERNAL — ${m ? m.label : 'custom ports'} routed through the FX rack`
        : 'HARDWARE INSERT — physical cable loop';
    });
  })
  .catch(() => { document.getElementById('io-status').textContent = 'error'; });
}

function setPreset(preset) {
  const body = {preset};
  const pick = document.getElementById('io-mod').value;
  if (preset === 'internal' && pick) body.module = pick;
  _postIO(body);
}

function setModule(key) {
  if (key) _postIO({module: key});
}

async function applyIO() {
  const body = {
    mode:     (_io && _io.mode) || 'hardware',
    send_l:   document.getElementById('io-sl').value.trim(),
    send_r:   document.getElementById('io-sr').value.trim(),
    return_l: document.getElementById('io-rl').value.trim(),
    return_r: document.getElementById('io-rr').value.trim(),
  };
  try {
    const r = await fetch('/api/fx/io', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) { _io = d; renderIO(); }
    else document.getElementById('io-status').textContent = d.error || 'error';
  } catch(e) {
    document.getElementById('io-status').textContent = 'error';
  }
}

// ── FX RACK (3-slot series stack) ─────────────────────────────────────────────
//
// Three slots.  Each slot picks one plugin from the installed palette; enabled
// slots are wired in series by the brain (SEND → 1 → 2 → 3 → OUT).  There is no
// 4th slot — the cap is the whole point (the Pi can't run out of DSP).
//
// This page only reads /api/fx/rack + /api/fx/palette and sends RACK_* commands;
// the brain owns the audio graph.

let _palette = [];      // [{key,label}] for the pickers
let _favorites = [];    // curated keeper keys (shown first, as ★ Favourites)
let _categories = {};   // {category: [keys]} for grouped optgroups
let _rack    = null;    // {cap, slots:[{idx,plugin,label,enabled,running,controls}]}
let _pickerOpen = false; // suppress re-render while a <select> is focused
let _paramTouchedAt = 0; // ms of the last slider move — suppress the poll's DOM
                         // rebuild for a grace window so a live drag isn't yanked
                         // out from under the finger and the value can't flicker
                         // back to the pre-move disk snapshot during the brain's
                         // ~1s persist debounce.
const _PARAM_GRACE_MS = 2500;

// Map a slider's raw value (already in the control's own min..max range) through.
function onRackParam(idx, symbol, value, el) {
  _paramTouchedAt = Date.now();
  const lbl = document.getElementById(`rpv-${idx}-${symbol}`);
  if (lbl) lbl.textContent = (+value).toFixed(2);
  cmd('RACK_PARAM', { idx, symbol, value: +value });
}

function rackAssign(idx, plugin) {
  cmd('RACK_ASSIGN', { idx, plugin: plugin || null });
  setTimeout(loadRack, 2600);   // jalv takes a couple seconds to come up
}
function rackToggle(idx)      { cmd('RACK_TOGGLE', { idx });        setTimeout(loadRack, 2600); }
function rackClear(idx)       { cmd('RACK_CLEAR',  { idx });        setTimeout(loadRack, 800); }
function rackMove(idx, delta) { cmd('RACK_MOVE',   { idx, delta }); setTimeout(loadRack, 2600); }

function pluginOptions(selectedKey) {
  // Grouped picker: ★ Favourites (the curated keepers) first, then each category,
  // then anything uncategorised -- so the 292-plugin palette is browsable without
  // a flat wall of names. Falls back to a flat list if the API sent no grouping.
  const label = {};
  _palette.forEach(p => { label[p.key] = p.label; });
  const opt = k => {
    const sel = k === selectedKey ? ' selected' : '';
    return `<option value="${k}"${sel}>${label[k] || k}</option>`;
  };
  const grp = (name, keys) => {
    const ks = keys.filter(k => label[k]);
    if (!ks.length) return '';
    return `<optgroup label="${name}">` + ks.map(opt).join('') + `</optgroup>`;
  };
  let html = `<option value="">— empty —</option>`;
  if (_favorites.length || Object.keys(_categories).length) {
    html += grp('★ Favourites', _favorites);
    const inFav = new Set(_favorites);
    const shown = new Set(_favorites);
    Object.keys(_categories).forEach(cat => {
      const rest = _categories[cat].filter(k => !inFav.has(k));
      rest.forEach(k => shown.add(k));
      html += grp(cat, rest);
    });
    // Anything the categories map missed.
    const leftover = _palette.map(p => p.key).filter(k => !shown.has(k));
    html += grp('Other', leftover);
  } else {
    _palette.forEach(p => { html += opt(p.key); });
  }
  return html;
}

function renderFlow() {
  const el = document.getElementById('rack-flow');
  if (!_rack) { el.textContent = ''; return; }
  const active = _rack.slots.filter(s => s.enabled && s.plugin);
  const parts = ['SEND'];
  active.forEach(s => parts.push((s.label || s.plugin) + (s.running ? '' : ' …')));
  parts.push('OUT');
  el.textContent = active.length ? parts.join('  →  ') : 'SEND  →  OUT   (no effects on — dry through)';
}

function renderRack() {
  const el = document.getElementById('rack-slots');
  if (!_rack) { el.innerHTML = '<div style="color:var(--muted);font-size:10px;padding:8px">Loading…</div>'; return; }
  renderFlow();

  el.innerHTML = '';
  _rack.slots.forEach(s => {
    const on      = s.enabled && s.plugin;
    const running = s.running;
    const canEnable = !!s.plugin;

    const controls = (s.controls || []).map(c => {
      const step = ((c.max - c.min) / 200) || 0.005;
      return `
      <div class="slider-row" style="margin-top:4px">
        <div class="slider-header">
          <span class="slider-label">${(c.label || c.symbol).toUpperCase()}</span>
          <span class="slider-value" id="rpv-${s.idx}-${c.symbol}">${(+c.value).toFixed(2)}</span>
        </div>
        <input type="range" min="${c.min}" max="${c.max}" step="${step}" value="${c.value}"
               oninput="onRackParam(${s.idx},'${c.symbol}',+this.value,this)"
               ${running ? '' : 'disabled'}>
      </div>`;
    }).join('');

    const card = document.createElement('div');
    card.className = `card fx-chain ${running ? 'fx-running' : ''}`;
    card.id = `rack-slot-${s.idx}`;
    card.innerHTML = `
      <div class="chain-header">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="chain-slot">SLOT ${s.idx + 1}</span>
          ${running ? '<span class="run-dot" title="running"></span>' : ''}
        </div>
        <div style="display:flex;gap:4px">
          <button class="btn" onclick="rackMove(${s.idx},-1)" title="earlier in chain" ${s.idx === 0 ? 'disabled' : ''}>▲</button>
          <button class="btn" onclick="rackMove(${s.idx},1)"  title="later in chain"  ${s.idx === _rack.slots.length - 1 ? 'disabled' : ''}>▼</button>
          <button class="btn ${on ? 'teal' : ''}" onclick="rackToggle(${s.idx})" ${canEnable ? '' : 'disabled'}>
            ${on ? 'ON' : 'OFF'}
          </button>
        </div>
      </div>
      <div class="btn-row" style="margin:6px 0">
        <select class="port-inp" style="flex:1" onchange="rackAssign(${s.idx}, this.value)"
                onfocus="_pickerOpen=true" onblur="_pickerOpen=false">
          ${pluginOptions(s.plugin)}
        </select>
        ${s.plugin ? `<button class="btn" onclick="rackClear(${s.idx})" title="empty slot">✕</button>` : ''}
      </div>
      ${controls}
    `;
    el.appendChild(card);
  });
}

async function loadPalette() {
  try {
    const r = await fetch('/api/fx/palette');
    const d = await r.json();
    _palette = d.plugins || [];
    _favorites = d.favorites || [];
    _categories = d.categories || {};
  } catch (e) {}
}

async function loadRack() {
  try {
    const r = await fetch('/api/fx/rack');
    _rack = await r.json();
    // Don't rebuild the slider DOM while the user is mid-drag (or just finished)
    // or while the plugin picker is open — either would clobber live input.
    const recentlyTouched = (Date.now() - _paramTouchedAt) < _PARAM_GRACE_MS;
    if (!_pickerOpen && !recentlyTouched) renderRack();
  } catch (e) {}
}

// ── Poll integration ──────────────────────────────────────────────────────────

ORC.subscribe(function(state) {
  // Follow fx_io changes made elsewhere (Fire, another tab). The state carries
  // the RAW file; the ports are resolved live by /api/fx/io, so re-GET that
  // rather than merging the file's stored ports over the resolved ones.
  if (state.fx_io && _io && !_ioDirty &&
      (state.fx_io.mode !== _io.mode ||
       (state.fx_io.module || '') !== (_io.module || ''))) {
    loadIO();
  }
});

// Poll the rack a bit for running-state changes (e.g. Fire-driven edits).
setInterval(loadRack, 4000);

// ── Init ──────────────────────────────────────────────────────────────────────

loadIO();
loadPalette().then(loadRack);
