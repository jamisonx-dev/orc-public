/* ORC-1 · control.js — Phase 8 CTRL page
   Knob map editor, chord row routing, controller status, voice map. */

'use strict';

// HTML-escape. Declared at FILE scope, not inside an IIFE: it is used by both
// the CTRL module and the UCS module below, and living inside CTRL's closure
// meant every call from UCS threw "ReferenceError: _esc is not defined".
// That broke loadPorts() -- caught by its own try/catch and logged to a console
// nobody was reading -- so the PORT dropdown in the universal pad editor has
// been EMPTY since this page was built (2026-07-13). Found by finally getting
// a browser onto the page, 2026-08-01.
function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const CTRL = (() => {

  // ── State ──────────────────────────────────────────────────
  let _views   = [];
  let _layers  = ['base', 'shift', 'alt'];
  let _targets = [];   // [{label, action}]
  let _map     = {};   // {view: {layer: {knob_idx: [...action]}}}
  let _ports   = [];
  let _curView  = 'seq';
  let _curLayer = 'base';
  // Universal per-PAGE (per-bank) knob overrides (Fire reorg P2). Only consulted
  // when _curView === 'universal'; other views use the global _map.
  let _uniBank  = 0;
  let _uniBanks = 1;
  let _uniKnobs = {};   // {layer: {idx: [action]}} for the current _uniBank
  let _uniFaders = {};  // {row: {cc,channel,port,value}} for the current _uniBank (P3)
  let _ccPorts  = ['synth', 'pisound'];   // ports for the universal CC editor
  // Module keys (from /api/knobmap). On the internal "synth" port a knob can
  // NAME a module instead of a channel -- it then follows the module wherever
  // /matrix puts it (2026-09-24). A number still means exactly that channel.
  let _modules  = [];

  // <option>s for a channel picker: modules first on the synth port, then 1-16.
  function _chOpts(port, cur) {
    const mods = port === 'synth' ? _modules : [];
    return mods.map(m => {
      const live = ORC.boundCh(m);
      const tag  = live ? ` (now ch${live})` : (live === 0 ? ' (not bound)' : '');
      return `<option value="${_esc(m)}"${cur === m ? ' selected' : ''}>${_esc(m)}${tag}</option>`;
    }).join('') + Array.from({length: 16}, (_, i) => i + 1).map(n =>
      `<option value="${n}"${cur === n ? ' selected' : ''}>ch ${n}</option>`).join('');
  }
  // A picker's value -> the action's channel field (int, or a module key).
  function _chVal(v) { return /^\d+$/.test(String(v)) ? parseInt(v) : String(v); }
  // Label text for an action's channel field.
  function _chTxt(ch) {
    if (typeof ch === 'string') {
      const live = ORC.boundCh(ch);
      return live ? `${ch} (ch${live})` : (live === 0 ? `${ch} (not bound)` : ch);
    }
    return `ch${ch}`;
  }

  // ── Init ───────────────────────────────────────────────────
  async function init() {
    await loadKnobmap();
    await loadChordRows();
    ORC.subscribe(_onState);
  }

  // ── KNOB MAP ───────────────────────────────────────────────

  async function loadKnobmap() {
    try {
      const r  = await fetch('/api/knobmap');
      const d  = await r.json();
      _views   = d.views   || [];
      _layers  = d.layers  || ['base', 'shift', 'alt'];
      _targets = d.targets || [];
      _modules = d.modules || [];
      _map     = d.map     || {};

      // Populate view selector (once)
      const sel = document.getElementById('km-view');
      if (sel && sel.options.length === 0) {
        _views.forEach(v => {
          const o = document.createElement('option');
          o.value = v; o.textContent = v;
          sel.appendChild(o);
        });
        // Try to keep the current view
        sel.value = _curView;
      } else if (sel) {
        _curView = sel.value || _curView;
      }

      if (_curView === 'universal') {
        _uniBank = 0; await loadUniKnobs(); await loadUniFaders();
      }
      _renderKnobGrid();

      // Populate CC-out port dropdown from live port list (once)
      const portSel = document.getElementById('km-cc-port');
      if (portSel && portSel.options.length <= 2) {
        try {
          const pr = await fetch('/api/midi_ports');
          const pd = await pr.json();
          const ports = pd.ports || pd;
          if (Array.isArray(ports) && ports.length > 0) {
            portSel.innerHTML = '';
            ports.forEach(p => {
              const o = document.createElement('option');
              o.value = p.key || p; o.textContent = p.label || p.key || p;
              portSel.appendChild(o);
            });
            // Keep the universal per-knob CC editor's port list in step.
            _ccPorts = ports.map(p => p.key || p);
          }
        } catch(_) {}
      }
      _fillCcFormCh();
      if (portSel) portSel.onchange = _fillCcFormCh;
    } catch (e) {
      console.error('[knobmap load]', e);
    }
  }

  function _fillCcFormCh() {
    const ps = document.getElementById('km-cc-port');
    const cs = document.getElementById('km-cc-ch');
    if (!ps || !cs) return;
    const want = cs.value ? _chVal(cs.value) : 1;
    cs.innerHTML = _chOpts(ps.value, want);
  }

  function setLayer(layer) {
    _curLayer = layer;
    ['base','shift','alt'].forEach(l => {
      const t = document.getElementById('km-tab-' + l);
      if (t) t.className = 'btn' + (l === layer ? ' accent' : '');
    });
    _renderKnobGrid();
  }

  // ── Universal per-page knobs (Fire reorg P2) ───────────────
  async function loadUniKnobs() {
    try {
      const r = await fetch('/api/universal_knobs?bank=' + _uniBank);
      const d = await r.json();
      _uniKnobs = d.knobs || {};
      _uniBanks = d.banks || 1;
      _renderUniBankSel();
    } catch (e) { console.error('[uniknobs load]', e); _uniKnobs = {}; }
  }

  function _renderUniBankSel() {
    const sel = document.getElementById('km-bank');
    if (!sel) return;
    sel.innerHTML = '';
    for (let b = 0; b < _uniBanks; b++) {
      const o = document.createElement('option');
      o.value = b; o.textContent = 'PAGE ' + (b + 1);
      sel.appendChild(o);
    }
    sel.value = _uniBank;
  }

  async function onKmBankChange() {
    const sel = document.getElementById('km-bank');
    _uniBank = parseInt((sel && sel.value) || 0) || 0;
    await loadUniKnobs();
    await loadUniFaders();
    _renderKnobGrid();
  }

  async function loadUniFaders() {
    try {
      const r = await fetch('/api/universal_faders?bank=' + _uniBank);
      const d = await r.json();
      _uniFaders = d.faders || {};
    } catch (e) { console.error('[unifaders load]', e); _uniFaders = {}; }
  }

  function _renderUniFaderRows() {
    const host = document.getElementById('uk-fader-rows');
    if (!host) return;
    const portOpts = _ccPorts.map(p =>
      `<option value="${_esc(p)}">${_esc(p)}</option>`).join('');
    host.innerHTML = [0,1,2,3].map(row => {
      const f    = _uniFaders[String(row)] || _uniFaders[row] || null;
      const on   = !!f;
      const ch   = on ? f.channel : 1;
      const cc   = on ? f.cc : 7;
      const stat = on ? `▬ CC${f.cc} · ch${f.channel} · ${f.port}` : 'per-pad';
      return `<div style="display:flex;flex-wrap:wrap;gap:3px;align-items:center;margin:2px 0">
        <span style="font-size:9px;color:var(--label);width:44px">ROW ${row}</span>
        <select id="uf-port-${row}" style="background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px">${portOpts}</select>
        <span style="font-size:9px;color:var(--label)">CH</span>
        <input type="number" id="uf-ch-${row}" min="1" max="16" value="${ch}" style="width:34px;background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px">
        <span style="font-size:9px;color:var(--label)">CC</span>
        <input type="number" id="uf-cc-${row}" min="0" max="127" value="${cc}" style="width:40px;background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px">
        <button class="btn accent" onclick="CTRL.setUniFader(${row})">SET</button>
        <button class="btn" onclick="CTRL.clearUniFader(${row})" title="row → per-pad">×</button>
        <span style="font-size:9px;color:var(--muted)">${_esc(stat)}</span>
      </div>`;
    }).join('');
    [0,1,2,3].forEach(row => {
      const f = _uniFaders[String(row)] || _uniFaders[row] || null;
      if (f) { const ps = document.getElementById('uf-port-' + row);
               if (ps) ps.value = f.port; }
    });
  }

  async function setUniFader(row) {
    const port = document.getElementById('uf-port-' + row)?.value || 'synth';
    const ch   = parseInt(document.getElementById('uf-ch-' + row)?.value ?? 1) || 1;
    const cc   = parseInt(document.getElementById('uf-cc-' + row)?.value ?? 7) || 0;
    try {
      await fetch('/api/universal_faders', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({bank: _uniBank, row, cc, channel: ch, port})
      });
      _uniFaders[String(row)] = {cc, channel: ch, port,
                                 value: (_uniFaders[String(row)]||{}).value || 0};
      _renderUniFaderRows();
    } catch(e) { console.error('[unifader set]', e); }
  }

  async function clearUniFader(row) {
    try {
      await fetch('/api/universal_faders', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({bank: _uniBank, row, clear: true})
      });
      delete _uniFaders[String(row)];
      delete _uniFaders[row];
      _renderUniFaderRows();
    } catch(e) { console.error('[unifader clear]', e); }
  }

  function _renderKnobGrid() {
    const grid = document.getElementById('km-grid');
    if (!grid) return;

    const sel = document.getElementById('km-view');
    _curView  = sel ? sel.value : _curView;

    const isUni = (_curView === 'universal');
    // The PAGE selector + the CC-only editor only apply to the universal surface.
    const bw = document.getElementById('km-bank-wrap');
    if (bw) bw.style.display = isUni ? '' : 'none';
    // Hide the shared CC-OUT builder for universal (each knob has its own inline
    // CC editor there); it stays for the internal views.
    const ccPanel = document.getElementById('km-cc-panel');
    if (ccPanel) ccPanel.style.display = isUni ? 'none' : '';
    const faderPanel = document.getElementById('uk-fader-panel');
    if (faderPanel) faderPanel.style.display = isUni ? '' : 'none';

    if (isUni) {
      _renderUniKnobGrid(grid, _uniKnobs[_curLayer] || {});
      _renderUniFaderRows();
      return;
    }

    // ── Internal views: the named-target dropdown (unchanged) ──
    const layerMap = (_map[_curView] || {})[_curLayer] || {};
    const optHtml = _targets.map(t =>
      `<option value="${_esc(JSON.stringify(t.action))}">${_esc(t.label)}</option>`
    ).join('');

    grid.innerHTML = [0,1,2,3].map(k => {
      return `<div class="km-knob">
        <div class="km-knob-label">KNOB ${k+1}</div>
        <select class="km-sel" id="km-sel-${k}" onchange="CTRL.assignKnob(${k})">
          <option value="">(unassigned)</option>
          ${optHtml}
        </select>
        <div class="km-cur" id="km-cur-${k}"></div>
      </div>`;
    }).join('');

    // Set selected values after render
    [0,1,2,3].forEach(k => {
      const cur = layerMap[String(k)] || layerMap[k] || null;
      const sel2 = document.getElementById('km-sel-' + k);
      if (sel2 && cur) {
        const curStr = JSON.stringify(cur);
        for (const opt of sel2.options) {
          try {
            if (opt.value && JSON.stringify(JSON.parse(opt.value)) === curStr) {
              sel2.value = opt.value;
              break;
            }
          } catch(e) {}
        }
      }
      const lbl = _targetLabel(cur);
      const curEl = document.getElementById('km-cur-' + k);
      if (curEl) curEl.textContent = cur ? (lbl ? '→ ' + lbl : '') : '';
    });
  }

  // On the CC surface, label a knob by WHAT IT SENDS, never by a cryptic preset
  // name (the confusing "synflt"/"dincut" labels have no place here).
  function _ccLabel(a) {
    if (!a) return '(baseline)';
    if (a[0] === 'cc_out' && a.length === 4) return `CC${a[3]} · ${_chTxt(a[2])} · ${a[1]}`;
    if (a[0] === 'pitch_out') return `PITCH · ${_chTxt(a[2])} · ${a[1]}`;
    if (a[0] === 'none') return 'off';
    return a[0] || '?';
  }

  // The universal surface is a CC-send surface: each knob is just PORT/CH/CC.
  // No named internal targets -- to drive an internal ORC param, send its CC
  // to its channel here (poly cutoff = CC74 ch8 synth, etc.).
  function _renderUniKnobGrid(grid, layerMap) {
    const portOpts = _ccPorts.map(p =>
      `<option value="${_esc(p)}">${_esc(p)}</option>`).join('');
    grid.innerHTML = [0,1,2,3].map(k => {
      const cur  = layerMap[String(k)] || layerMap[k] || null;
      const isCC = cur && cur[0] === 'cc_out';
      const ch   = isCC ? cur[2] : 1;
      const cc   = isCC ? cur[3] : 74;
      const txt  = _ccLabel(cur);
      return `<div class="km-knob">
        <div class="km-knob-label">KNOB ${k+1}</div>
        <div class="uk-cc" style="display:flex;flex-wrap:wrap;gap:3px;align-items:center;margin:3px 0">
          <select id="uk-port-${k}" style="background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px">${portOpts}</select>
          <span style="font-size:9px;color:var(--label)">CH</span>
          <select id="uk-ch-${k}" data-cur="${_esc(String(ch))}" style="max-width:150px;background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px"></select>
          <span style="font-size:9px;color:var(--label)">CC</span>
          <input type="number" id="uk-cc-${k}" min="0" max="127" value="${cc}" style="width:40px;background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:2px 3px;border-radius:2px">
          <button class="btn accent" onclick="CTRL.setUniKnobCC(${k})">SET</button>
          <button class="btn" onclick="CTRL.clearUniKnob(${k})" title="clear → baseline">×</button>
        </div>
        <div class="km-cur" id="km-cur-${k}">${_esc(txt)}</div>
      </div>`;
    }).join('');
    // Restore the port selection per knob (value must be set after the option
    // list exists).
    [0,1,2,3].forEach(k => {
      const cur = layerMap[String(k)] || layerMap[k] || null;
      const ps = document.getElementById('uk-port-' + k);
      if (cur && cur[0] === 'cc_out' && ps) ps.value = cur[1];
      const cs = document.getElementById('uk-ch-' + k);
      if (ps && cs) {
        const fill = () => {
          const want = cs.value ? _chVal(cs.value) : _chVal(cs.dataset.cur || 1);
          cs.innerHTML = _chOpts(ps.value, want);
        };
        fill();
        ps.onchange = fill;           // modules exist only on the synth port
      }
    });
  }

  async function _postUniKnob(knob, action) {
    await fetch('/api/universal_knobs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bank: _uniBank, layer: _curLayer, knob,
                            knob_action: action})
    });
    _uniKnobs[_curLayer] = _uniKnobs[_curLayer] || {};
    if (action) _uniKnobs[_curLayer][String(knob)] = action;
    else        delete _uniKnobs[_curLayer][String(knob)];
    const curEl = document.getElementById('km-cur-' + knob);
    if (curEl) curEl.textContent = _ccLabel(action);
  }

  async function setUniKnobCC(knob) {
    const port = document.getElementById('uk-port-' + knob)?.value || 'synth';
    const ch   = _chVal(document.getElementById('uk-ch-' + knob)?.value ?? 1);
    const cc   = parseInt(document.getElementById('uk-cc-' + knob)?.value ?? 74) || 0;
    try { await _postUniKnob(knob, ['cc_out', port, ch, cc]); }
    catch(e) { console.error('[uniknob cc]', e); }
  }

  async function clearUniKnob(knob) {
    try { await _postUniKnob(knob, null); }
    catch(e) { console.error('[uniknob clear]', e); }
  }

  async function assignKnob(knob) {
    const sel = document.getElementById('km-sel-' + knob);
    if (!sel) return;
    let target = null;
    if (sel.value) { try { target = JSON.parse(sel.value); } catch(e) { return; } }

    // Universal: write a per-PAGE override. Empty selection clears it (the knob
    // falls back to the baseline dialmap).
    if (_curView === 'universal') {
      try {
        await fetch('/api/universal_knobs', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({bank: _uniBank, layer: _curLayer, knob,
                                knob_action: target})
        });
        _uniKnobs[_curLayer] = _uniKnobs[_curLayer] || {};
        if (target) _uniKnobs[_curLayer][String(knob)] = target;
        else        delete _uniKnobs[_curLayer][String(knob)];
        const curEl = document.getElementById('km-cur-' + knob);
        if (curEl) curEl.textContent = target ? '→ ' + _targetLabel(target)
                                              : '(baseline)';
      } catch(e) { console.error('[uniknob set]', e); }
      return;
    }

    if (!sel.value) return;   // global map: "(unassigned)" is a no-op
    try {
      await fetch('/api/knobmap', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({view: _curView, layer: _curLayer, knob, target})
      });
      // Update local cache and re-render label
      _map[_curView] = _map[_curView] || {};
      _map[_curView][_curLayer] = _map[_curView][_curLayer] || {};
      _map[_curView][_curLayer][String(knob)] = target;
      const curEl = document.getElementById('km-cur-' + knob);
      if (curEl) curEl.textContent = '→ ' + _targetLabel(target);
    } catch(e) { console.error('[knobmap set]', e); }
  }

  function _targetLabel(action) {
    if (!action) return '';
    const key = JSON.stringify(action);
    for (const t of _targets) {
      if (JSON.stringify(t.action) === key) return t.label;
    }
    // cc_out: ["cc_out", port, ch, cc]  (ch = 1-16 or a module key)
    if (action[0] === 'cc_out' && action.length === 4) {
      return `CC${action[3]} ${_chTxt(action[2])} (${action[1]})`;
    }
    return action[0] || '?';
  }

  async function assignCCOut() {
    const knob = parseInt(document.getElementById('km-cc-knob')?.value ?? 0);
    const port = document.getElementById('km-cc-port')?.value || 'synth';
    const ch   = _chVal(document.getElementById('km-cc-ch')?.value ?? 1);
    const cc   = parseInt(document.getElementById('km-cc-num')?.value ?? 74);
    const stEl = document.getElementById('km-cc-status');
    if (stEl) stEl.textContent = 'saving…';
    const target = ['cc_out', port, ch, cc];
    try {
      if (_curView === 'universal') {
        await fetch('/api/universal_knobs', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({bank: _uniBank, layer: _curLayer, knob,
                                knob_action: target})
        });
        _uniKnobs[_curLayer] = _uniKnobs[_curLayer] || {};
        _uniKnobs[_curLayer][String(knob)] = target;
      } else {
        await fetch('/api/knobmap', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({view: _curView, layer: _curLayer, knob, target})
        });
        _map[_curView] = _map[_curView] || {};
        _map[_curView][_curLayer] = _map[_curView][_curLayer] || {};
        _map[_curView][_curLayer][String(knob)] = target;
      }
      _renderKnobGrid();
      if (stEl) stEl.textContent = `✓ knob${knob+1} → CC${cc} ${_chTxt(ch)} (${port})`;
      setTimeout(() => { if (stEl) stEl.textContent = ''; }, 3000);
    } catch(e) {
      if (stEl) stEl.textContent = 'error';
      console.error('[km cc_out]', e);
    }
  }

  // ── CHORD ROWS ─────────────────────────────────────────────

  async function loadChordRows() {
    try {
      const r = await fetch('/api/chord_rows');
      const d = await r.json();
      _ports = d.ports || ['synth', 'pisound'];
      if (d.modules) _modules = d.modules;
      _crBank = d.bank || 0;
      _renderChordRows(Array.isArray(d.rows) ? d.rows : []);
    } catch(e) { console.error('[chord_rows load]', e); }
  }

  let _crBank = 0, _crCount = 0;
  function _renderChordRows(rows) {
    const el = document.getElementById('chord-rows-ui');
    if (!el) return;
    _crCount = rows.length;

    const portOpts = _ports.map(p =>
      `<option value="${_esc(p)}">${_esc(p)}</option>`).join('');

    el.innerHTML = rows.map((r, i) => {
      const isD = r.type === 'direct';
      const isO = r.type === 'off';
      return `<div class="cr-row" id="cr-row-${i}">
        <span class="cr-idx">ROW ${i}</span>
        <select class="cr-type" id="cr-type-${i}" onchange="CTRL.onRowTypeChange(${i})">
          <option value="chord"  ${r.type==='chord'  ?'selected':''}>chord engine</option>
          <option value="direct" ${r.type==='direct' ?'selected':''}>direct</option>
          <option value="off"    ${r.type==='off'    ?'selected':''}>off</option>
        </select>
        <span class="cr-direct-fields" id="cr-direct-${i}" style="display:${isD?'contents':'none'}">
          <select class="cr-port" id="cr-port-${i}">
            ${portOpts}
          </select>
          <label class="cr-lbl">CH</label>
          <select class="cr-ch" id="cr-ch-${i}" data-cur="${_esc(String(r.channel ?? 1))}" style="max-width:150px"></select>
        </span>
        <label class="cr-lbl">OCT</label>
        <input class="cr-oct" id="cr-oct-${i}" type="number" min="-4" max="4"
               value="${r.octave||0}" style="width:3em">
      </div>`;
    }).join('');

    // Set port dropdown values after render; the channel picker offers the
    // MODULES on the synth port (a row that names one follows it on /matrix).
    rows.forEach((r, i) => {
      const portSel = document.getElementById('cr-port-' + i);
      if (portSel && r.port) portSel.value = r.port;
      const chSel = document.getElementById('cr-ch-' + i);
      if (portSel && chSel) {
        const fill = () => {
          const want = chSel.value ? _chVal(chSel.value) : _chVal(chSel.dataset.cur || 1);
          chSel.innerHTML = _chOpts(portSel.value, want);
        };
        fill();
        portSel.onchange = fill;
      }
    });
  }

  function onRowTypeChange(i) {
    const typeSel = document.getElementById('cr-type-' + i);
    const dirFields = document.getElementById('cr-direct-' + i);
    if (!typeSel || !dirFields) return;
    dirFields.style.display = typeSel.value === 'direct' ? 'contents' : 'none';
  }

  async function saveChordRows() {
    const rows = Array.from({length: _crCount || 4}, (_, i) => i).map(i => {
      const type   = document.getElementById('cr-type-' + i)?.value || 'chord';
      const port   = document.getElementById('cr-port-' + i)?.value || 'synth';
      const ch     = _chVal(document.getElementById('cr-ch-' + i)?.value || '1');
      const octave = parseInt(document.getElementById('cr-oct-' + i)?.value || '0');
      if (type === 'direct') return {type, port, channel: ch, octave};
      if (type === 'off')    return {type, port: null, channel: null, octave};
      return {type: 'chord', port: null, channel: null, octave};
    });
    try {
      const r = await fetch('/api/chord_rows', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rows, bank: _crBank})
      });
      const d = await r.json();
      if (d.ok) _flash('chord-rows-card');
    } catch(e) { console.error('[chord_rows save]', e); }
  }

  // ── CONTROLLER STATUS (via state poll) ────────────────────

  function _onState(s) {
    _renderDevices(s.midi_devices || []);
    _renderVoiceMap(s.voice_map || {});
  }

  function _renderDevices(devices) {
    const el = document.getElementById('ctrl-devices');
    if (!el) return;
    if (!devices.length) { el.textContent = 'No controllers connected.'; return; }
    el.innerHTML = devices.map(d =>
      `<div class="ctrl-dev">● ${_esc(d)}</div>`
    ).join('');
  }

  // ── VOICE MAP ──────────────────────────────────────────────

  function _renderVoiceMap(vm) {
    const el = document.getElementById('vm-display');
    if (!el) return;
    const entries = Object.entries(vm);
    if (!entries.length) {
      el.textContent = 'none set: using the preset routing';
      return;
    }
    el.innerHTML = entries.map(([slot, dest]) => {
      const destStr = dest
        ? `${dest.port || '?'} ch${dest.channel || '?'}`
        : '<span class="vm-silent">SILENT</span>';
      return `<div class="vm-row"><span class="vm-slot">${_esc(slot)}</span>${destStr}</div>`;
    }).join('');
  }

  function clearVoiceMap() {
    cmd('CLEAR_VOICE_MAP');
  }

  // ── Helpers ────────────────────────────────────────────────

  function _flash(cardId) {
    const el = document.getElementById(cardId);
    if (!el) return;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }

  // ── Public API ─────────────────────────────────────────────
  return { init, loadKnobmap, setLayer, assignKnob, assignCCOut,
           onKmBankChange, setUniKnobCC, clearUniKnob,
           setUniFader, clearUniFader,
           onRowTypeChange, saveChordRows, clearVoiceMap };

})();

document.addEventListener('DOMContentLoaded', CTRL.init);

/* ════════════════════════════════════════════════════════════════
   UCS — Universal Control Surface pad editor
   ════════════════════════════════════════════════════════════════ */
const UCS = (() => {

  // State
  let _pads  = {};          // {"r,c": assignment}
  let _layer = 'base';      // base | shift | alt
  let _sel   = null;        // "r,c" of selected pad, or null
  let _ports = [];          // [{key, label, static}] from /api/midi_ports
  let _bank  = 0;           // which pad bank is shown (Fire pages with GRID <>)
  let _banks = 1;           // how many banks the box reports

  // ── Init ────────────────────────────────────────────────────
  async function init() {
    await loadPorts();
    await load();
    _renderGrid();
  }

  // ── Port list ───────────────────────────────────────────────
  async function loadPorts(portList) {
    try {
      if (!portList) {
        const r = await fetch('/api/midi_ports');
        const d = await r.json();
        portList = d.ports || [];
      }
      _ports = portList;
      const sel = document.getElementById('ucs-port');
      if (!sel) return;
      const prev = sel.value;
      sel.innerHTML = _ports.map(p =>
        `<option value="${_esc(p.key)}">${_esc(p.label)}${p.static ? '' : ' ✦'}</option>`
      ).join('');
      // Restore previous selection if still valid
      if (prev && _ports.some(p => p.key === prev)) sel.value = prev;
    } catch(e) { console.error('[ucs ports load]', e); }
  }

  async function scanPorts() {
    const btn = document.getElementById('ucs-scan-btn');
    if (btn) { btn.textContent = 'SCANNING…'; btn.disabled = true; }
    try {
      const r = await fetch('/api/midi_ports/scan', { method: 'POST' });
      const d = await r.json();
      await loadPorts(d.ports || []);
    } catch(e) { console.error('[ucs scan ports]', e); }
    finally {
      if (btn) { btn.textContent = 'SCAN PORTS'; btn.disabled = false; }
    }
  }

  async function load() {
    try {
      const r = await fetch('/api/universal_pads?bank=' + _bank);
      const d = await r.json();
      _pads   = d.pads || {};
      _banks  = Math.max(1, d.banks || 1);
      _renderBankTabs();
      _renderGrid();
    } catch(e) { console.error('[ucs load]', e); }
  }

  // ── Pad banks ───────────────────────────────────────────────
  // The Fire pages between banks with GRID <>. Bank 0 is what this page
  // edited before banks existed, so it stays the default and an old client
  // that never sends `bank` keeps hitting exactly the pads it always did.
  function _renderBankTabs() {
    const box = document.getElementById('ucs-banks');
    if (!box) return;
    let h = '';
    for (let b = 0; b < _banks; b++) {
      h += `<button class="btn${b === _bank ? ' accent' : ''}" `
         + `onclick="UCS.setBank(${b})">${b + 1}</button>`;
    }
    box.innerHTML = h;
  }

  async function setBank(b) {
    if (b === _bank) return;
    _bank = b;
    closeEditor();          // the editor is bound to a pad in the old bank
    await load();
  }

  // ── Layer tabs ──────────────────────────────────────────────
  function setLayer(layer) {
    _layer = layer;
    ['base','shift','alt'].forEach(l => {
      const t = document.getElementById('ucs-tab-' + l);
      if (t) t.className = 'btn' + (l === layer ? ' accent' : '');
    });
    _renderGrid();
  }

  // ── Grid rendering ──────────────────────────────────────────
  function _layerKey(key) {
    // base  → key itself
    // shift → key's shift_action (if any)
    // alt   → key's alt_action   (if any)
    const base = _pads[key];
    if (!base) return null;
    if (_layer === 'base')  return base;
    if (_layer === 'shift') return base.shift_action || null;
    if (_layer === 'alt')   return base.alt_action   || null;
    return null;
  }

  function _renderGrid() {
    const grid = document.getElementById('ucs-grid');
    if (!grid) return;
    let h = '';
    for (let row = 0; row < 4; row++) {
      h += '<div class="ucs-row">';
      for (let col = 0; col < 16; col++) {
        const key    = `${row},${col}`;
        const assign = _layerKey(key);
        const sel    = _sel === key ? ' sel' : '';
        const hasBase = !!_pads[key];
        const cls    = assign ? ' assigned' : (hasBase && _layer !== 'base' ? ' has-base' : '');
        const label  = assign
          ? `<span class="ucs-pad-type">${assign.type.toUpperCase()[0]}</span><span class="ucs-pad-num">${assign.number}</span>`
          : `<span class="ucs-pad-rc">${col+1}</span>`;
        h += `<div class="ucs-pad${cls}${sel}" onclick="UCS.select(${row},${col})">${label}</div>`;
      }
      h += `<span class="ucs-row-label">R${row}</span></div>`;
    }
    grid.innerHTML = h;
  }

  // ── Pad selection & editor ───────────────────────────────────
  function select(row, col) {
    const key = `${row},${col}`;
    _sel = key;
    _renderGrid();
    _openEditor(row, col);
  }

  function _openEditor(row, col) {
    const key    = `${row},${col}`;
    const base   = _pads[key] || {};
    let   assign = base;
    if (_layer === 'shift') assign = base.shift_action || {};
    if (_layer === 'alt')   assign = base.alt_action   || {};

    const el = id => document.getElementById(id);
    el('ucs-ed-title').textContent = `PAD (${row},${col}) · ${_layer.toUpperCase()}`;
    el('ucs-number').value  = assign.number  !== undefined ? assign.number  : 60;
    el('ucs-value').value   = assign.value   !== undefined ? assign.value   : 100;
    el('ucs-channel').value = assign.channel !== undefined ? assign.channel : 1;
    el('ucs-port').value    = assign.port    || (_ports[0]?.key || 'pisound');
    el('ucs-latch').checked = !!assign.latch;
    _setTypeBtn(assign.type || 'note');
    el('ucs-editor').style.display = '';
  }

  function closeEditor() {
    _sel = null;
    document.getElementById('ucs-editor').style.display = 'none';
    _renderGrid();
  }

  function setType(t) { _setTypeBtn(t); }

  function _setTypeBtn(t) {
    ['note','cc','pc'].forEach(x => {
      const b = document.getElementById('ucs-type-' + x);
      if (b) b.className = 'btn' + (x === t ? ' accent' : '');
    });
    const vr = document.getElementById('ucs-value-row');
    if (vr) vr.style.display = (t === 'pc') ? 'none' : '';
  }

  function _curType() {
    if (document.getElementById('ucs-type-pc')?.className.includes('accent')) return 'pc';
    return document.getElementById('ucs-type-cc')?.className.includes('accent') ? 'cc' : 'note';
  }

  function preview() { /* live feedback could go here */ }

  async function savePad() {
    if (!_sel) return;
    const [row, col] = _sel.split(',').map(Number);
    const id = x => document.getElementById(x);
    const assignment = {
      type:    _curType(),
      number:  parseInt(id('ucs-number').value),
      value:   parseInt(id('ucs-value').value),
      channel: parseInt(id('ucs-channel').value),
      port:    id('ucs-port').value,
      latch:   id('ucs-latch').checked,
    };

    const payload = { row, col, bank: _bank };
    if (_layer === 'base') {
      Object.assign(payload, assignment);
    } else {
      // shift/alt: send as sub-action — must have a base first
      if (!_pads[_sel]) {
        alert('Assign base layer first before adding SHIFT/ALT sub-actions.');
        return;
      }
      payload[_layer + '_action'] = assignment;
      // Merge existing base into payload so engine keeps it
      const base = _pads[_sel];
      Object.assign(payload, {
        type: base.type, number: base.number, value: base.value,
        channel: base.channel, port: base.port, latch: base.latch || false,
      });
    }

    try {
      await fetch('/api/universal_pads', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      // Update local cache
      if (_layer === 'base') {
        _pads[_sel] = { ...assignment, _latched_on: false };
      } else {
        _pads[_sel] = _pads[_sel] || {};
        _pads[_sel][_layer + '_action'] = assignment;
      }
      _renderGrid();
      _flash('universal-card');
    } catch(e) { console.error('[ucs save]', e); }
  }

  async function clearPad() {
    if (!_sel) return;
    const [row, col] = _sel.split(',').map(Number);
    try {
      await fetch('/api/universal_pads/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({row, col, bank: _bank})
      });
      delete _pads[_sel];
      closeEditor();
    } catch(e) { console.error('[ucs clear pad]', e); }
  }

  async function clearAll() {
    if (!confirm('Clear every pad in BANK ' + (_bank + 1) + '?')) return;
    try {
      await fetch('/api/universal_pads/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({all: true, bank: _bank})
      });
      _pads = {};
      closeEditor();
      _renderGrid();
    } catch(e) { console.error('[ucs clear all]', e); }
  }

  function _flash(cardId) {
    const el = document.getElementById(cardId);
    if (!el) return;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }

  return { init, load, loadPorts, scanPorts, setLayer, setBank, select,
           setType, preview, savePad, clearPad, clearAll, closeEditor };
})();

document.addEventListener('DOMContentLoaded', UCS.init);
