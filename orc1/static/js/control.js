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
  // (The Akai-firmware 'universal' view's per-page knobs + fader rows were edited
  //  here until 2026-10-27; the UNIVERSAL pages now live ON the Fire, see UCS.)
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
      // 'universal' is edited in the FIRE UNIVERSAL PAGES card (stored on the Fire)
      _views   = (d.views  || []).filter(v => v !== 'universal');
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

  function _renderKnobGrid() {
    const grid = document.getElementById('km-grid');
    if (!grid) return;

    const sel = document.getElementById('km-view');
    _curView  = sel ? sel.value : _curView;

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

  async function assignKnob(knob) {
    const sel = document.getElementById('km-sel-' + knob);
    if (!sel) return;
    let target = null;
    if (sel.value) { try { target = JSON.parse(sel.value); } catch(e) { return; } }

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
      await fetch('/api/knobmap', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({view: _curView, layer: _curLayer, knob, target})
      });
      _map[_curView] = _map[_curView] || {};
      _map[_curView][_curLayer] = _map[_curView][_curLayer] || {};
      _map[_curView][_curLayer][String(knob)] = target;
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
           onRowTypeChange, saveChordRows, clearVoiceMap };

})();

document.addEventListener('DOMContentLoaded', CTRL.init);

/* ════════════════════════════════════════════════════════════════
   UCS — FIRE UNIVERSAL PAGES (ORC Fire firmware v0.31+, 2026-10-27)
   The pages are stored ON the Fire. This editor reads a page from the Fire,
   edits it here (a draft), and SEND TO FIRE writes the whole page, after
   which the brain reads it back and compares (orc1_fire_pages.FirePages).
   Same look and flow as the old universal pad editor: page (bank) tabs,
   BASE / SHIFT layer, a 4×16 pad grid, an inline pad editor; plus the
   page's knobs (4 banks × 4). Channels are 1-16 here.
   ════════════════════════════════════════════════════════════════ */
const UCS = (() => {

  const KBANK_NAMES = ['CHANNEL', 'MIXER', 'USER 1', 'USER 2'];   // the Fire's KNOB MODE lights
  const TYPE_HELP = {
    note:       'NOTE: plays the note while held. ON = velocity.',
    note_latch: 'NOTE LATCH: one press starts the note, the next stops it. ON = velocity.',
    cc_hold:    'CC HOLD: sends ON when pressed, OFF when let go.',
    cc_toggle:  'CC TOGGLE: sends ON and OFF on alternate presses.',
    cc_press:   'CC PRESS: sends ON on each press, nothing on release.',
    pc:         'PROG CHANGE: sends program NUMBER.',
  };

  let _meta   = null;      // /api/fire_pages meta (types, colours, sizes)
  let _info   = null;      // last FIRE_PAGES_INFO reply
  let _page   = 0;         // 0-7
  let _layer  = 0;         // 0 BASE, 1 SHIFT
  let _sel    = null;      // pad index 0-63, or null
  let _kbank  = 0;         // knob bank 0-3
  let _draft  = null;      // {name, pads: {"pad,layer": {...}}, knobs: {slot: {...}}}
  let _loaded = false;     // _draft came from the Fire
  let _dirty  = false;
  let _busy   = false;

  const $ = id => document.getElementById(id);

  // ── Init / status ────────────────────────────────────────────
  async function init() {
    await loadInfo();
    if (_info && _info.ok && Number.isInteger(_info.current)) _page = _info.current;
    _renderBankTabs();
    await load();
  }

  async function loadInfo() {
    try {
      const r = await fetch('/api/fire_pages');
      const d = await r.json();
      _meta = d.meta || _meta;
      _info = d;
    } catch (e) {
      _info = {ok: false, error: 'cannot reach the ORC web server'};
      console.error('[fire pages info]', e);
    }
    _fillSelects();
    _renderStatus();
  }

  function _renderStatus() {
    const el = $('fup-status');
    if (!el) return;
    const d = _info || {};
    if (!d.ok) {
      el.className = 'fup-status bad';
      el.textContent = d.error || d.why || 'the Fire did not answer';
      return;
    }
    const cur = Number.isInteger(d.current) ? ` · the Fire is on page ${d.current + 1}` +
                (d.showing ? ' (UNIVERSAL showing)' : '') : '';
    el.className = 'fup-status good';
    el.textContent = `Fire connected · firmware version ${d.version} · storage ${d.used} of ${d.budget} bytes${cur}`;
  }

  function _fillSelects() {
    if (!_meta) return;
    const ts = $('ucs-type');
    if (ts && !ts.options.length)
      ts.innerHTML = _meta.pad_types.map(t => `<option value="${_esc(t.key)}">${_esc(t.label)}</option>`).join('');
    const cs = $('ucs-colour');
    if (cs && !cs.options.length)
      cs.innerHTML = _meta.colours.map((c, i) => `<option value="${i}">${_esc(c.name.toUpperCase())}</option>`).join('');
  }

  function _rgb(i) {
    const c = _meta && _meta.colours[i];
    return c ? `rgb(${c.rgb[0]},${c.rgb[1]},${c.rgb[2]})` : 'transparent';
  }

  // ── Page tabs ────────────────────────────────────────────────
  function _renderBankTabs() {
    const box = $('ucs-banks');
    if (!box) return;
    const n = (_meta && _meta.pages) || 8;
    const pages = (_info && _info.ok && _info.pages) || [];
    let h = '';
    for (let p = 0; p < n; p++) {
      const pi = pages[p] || {};
      const tip = pi.name ? ` title="${_esc(pi.name)}"` : '';
      const used = pi.entries ? ' fup-used' : '';
      h += `<button class="btn${p === _page ? ' accent' : ''}${used}"${tip} onclick="UCS.setBank(${p})">${p + 1}</button>`;
    }
    box.innerHTML = h;
  }

  async function setBank(p) {
    if (p === _page || _busy) return;
    if (_dirty && !confirm(`Page ${_page + 1} has changes not sent to the Fire. Drop them?`)) return;
    _page = p;
    closeEditor();
    _renderBankTabs();
    await load();
  }

  // ── Read a page from the Fire ────────────────────────────────
  function _emptyDraft() { return {name: '', pads: {}, knobs: {}}; }

  function _toDraft(data) {
    const d = _emptyDraft();
    if (!data) return d;
    d.name = data.name || '';
    (data.pads || []).forEach(p => { d.pads[`${p.pad},${p.layer}`] = Object.assign({}, p); });
    (data.knobs || []).forEach(k => { d.knobs[k.slot] = Object.assign({}, k); });
    return d;
  }

  function _fromDraft() {
    return {
      name: _draft.name || '',
      pads: Object.values(_draft.pads),
      knobs: Object.values(_draft.knobs),
    };
  }

  async function load() {
    _result('');
    _draft = _emptyDraft(); _loaded = false; _setDirty(false);
    if (!_info || !_info.ok) { _renderAll(); return; }
    _result(`reading page ${_page + 1} from the Fire…`);
    try {
      const r = await fetch('/api/fire_pages/' + _page);
      const d = await r.json();
      if (!d.ok) { _result('could not read the page: ' + (d.error || 'no answer'), 'bad'); }
      else if (!d.data) { _result('the page on the Fire is not readable here: ' + (d.invalid || '?'), 'bad'); }
      else { _draft = _toDraft(d.data); _loaded = true; _result(''); }
    } catch (e) {
      _result('could not read the page (network)', 'bad');
      console.error('[fire page read]', e);
    }
    _renderAll();
  }

  async function reload() {
    if (_busy) return;
    if (_dirty && !confirm('Drop the changes not sent and read the page from the Fire again?')) return;
    await loadInfo();
    _renderBankTabs();
    await load();
  }

  function _renderAll() {
    const nm = $('fup-name');
    if (nm) nm.value = _draft.name || '';
    _renderGrid();
    _renderKnobs();
    _setDirty(_dirty);
  }

  // ── Layer ────────────────────────────────────────────────────
  function setLayer(layer) {
    _layer = layer === 'shift' ? 1 : 0;
    ['base', 'shift'].forEach((l, i) => {
      const t = $('ucs-tab-' + l);
      if (t) t.className = 'btn' + (i === _layer ? ' accent' : '');
    });
    _renderGrid();
    if (_sel !== null) _openEditor(_sel);
  }

  // ── Grid ─────────────────────────────────────────────────────
  function _short(t) {
    return {note: 'N', note_latch: 'NL', cc_hold: 'CH', cc_toggle: 'CT', cc_press: 'CP', pc: 'PC'}[t] || '?';
  }

  function _renderGrid() {
    const grid = $('ucs-grid');
    if (!grid) return;
    let h = '';
    for (let row = 0; row < 4; row++) {
      h += '<div class="ucs-row">';
      for (let col = 0; col < 16; col++) {
        const pad = row * 16 + col;
        const a = _draft.pads[`${pad},${_layer}`];
        const base = _draft.pads[`${pad},0`];
        const sel = _sel === pad ? ' sel' : '';
        const cls = a ? ' assigned' : (base && _layer ? ' has-base' : '');
        const tint = base ? ` style="--padc:${_rgb(base.colour)}"` : '';
        const label = a
          ? `<span class="ucs-pad-type">${_short(a.type)}</span><span class="ucs-pad-num">${a.number}</span>`
          : `<span class="ucs-pad-rc">${col + 1}</span>`;
        h += `<div class="ucs-pad${cls}${sel}${base ? ' tinted' : ''}"${tint} onclick="UCS.select(${row},${col})" title="pad ${pad} (row ${row + 1}, column ${col + 1})">${label}</div>`;
      }
      h += `<span class="ucs-row-label">R${row + 1}</span></div>`;
    }
    grid.innerHTML = h;
  }

  // ── Pad editor ───────────────────────────────────────────────
  function select(row, col) {
    _sel = row * 16 + col;
    _renderGrid();
    _openEditor(_sel);
  }

  function _openEditor(pad) {
    const a = _draft.pads[`${pad},${_layer}`];
    const base = _draft.pads[`${pad},0`];
    const row = Math.floor(pad / 16), col = pad % 16;
    $('ucs-ed-title').textContent = `PAD R${row + 1} C${col + 1} · ${_layer ? 'SHIFT' : 'BASE'}` + (a ? '' : ' · not set');
    const v = a || {type: 'note', number: 36, on: 100, off: 0, channel: 1,
                    colour: base ? base.colour : 16};
    $('ucs-type').value = v.type;
    $('ucs-number').value = v.number;
    $('ucs-on').value = v.on;
    $('ucs-off').value = v.off;
    $('ucs-channel').value = v.channel;
    $('ucs-colour').value = String(v.colour);
    onType(); onColour();
    $('ucs-editor').style.display = '';
  }

  function onType() {
    const t = $('ucs-type').value;
    const isNote = t === 'note' || t === 'note_latch';
    $('ucs-number-lbl').textContent = isNote ? 'NOTE' : (t === 'pc' ? 'PROGRAM' : 'CC');
    $('ucs-on-lbl').textContent = isNote ? 'VELOCITY' : 'ON';
    const showOn = t !== 'pc', showOff = t === 'cc_hold' || t === 'cc_toggle';
    [['ucs-on-lbl', showOn], ['ucs-on', showOn], ['ucs-off-lbl', showOff], ['ucs-off', showOff]]
      .forEach(([id, on]) => { $(id).style.display = on ? '' : 'none'; });
    const help = $('ucs-type-help');
    if (help) help.textContent = (TYPE_HELP[t] || '') +
      (_layer ? ' The Fire shows the BASE colour.' : '');
  }

  function onColour() {
    const sw = $('ucs-swatch');
    if (sw) sw.style.background = _rgb(parseInt($('ucs-colour').value));
  }

  function closeEditor() {
    _sel = null;
    const ed = $('ucs-editor');
    if (ed) ed.style.display = 'none';
    _renderGrid();
  }

  function _num(id, lo, hi, what) {
    const v = Number($(id).value);
    if (!Number.isInteger(v) || v < lo || v > hi) throw new Error(`${what} must be ${lo}-${hi}`);
    return v;
  }

  function savePad() {
    if (_sel === null) return;
    let p;
    try {
      p = {pad: _sel, layer: _layer, type: $('ucs-type').value,
           number: _num('ucs-number', 0, 127, 'NUMBER'),
           on: $('ucs-on').style.display === 'none' ? 127 : _num('ucs-on', 0, 127, 'ON'),
           off: $('ucs-off').style.display === 'none' ? 0 : _num('ucs-off', 0, 127, 'OFF'),
           channel: _num('ucs-channel', 1, 16, 'CHANNEL'),
           colour: parseInt($('ucs-colour').value)};
    } catch (e) { _result(e.message, 'bad'); return; }
    _draft.pads[`${_sel},${_layer}`] = p;
    _setDirty(true);
    _result('');
    _renderGrid();
    _openEditor(_sel);
    _flash('universal-card');
  }

  function clearPad() {
    if (_sel === null) return;
    delete _draft.pads[`${_sel},${_layer}`];
    _setDirty(true);
    closeEditor();
  }

  function onName() {
    _draft.name = $('fup-name').value;
    _setDirty(true);
  }

  // ── Knobs (4 banks × 4) ──────────────────────────────────────
  function _renderKnobs() {
    const tabs = $('fup-kbanks');
    if (tabs) tabs.innerHTML = KBANK_NAMES.map((n, b) =>
      `<button class="btn${b === _kbank ? ' accent' : ''}" onclick="UCS.setKBank(${b})" title="knob bank ${b + 1}">${n}</button>`).join('');
    const grid = $('fup-kgrid');
    if (!grid) return;
    grid.innerHTML = [0, 1, 2, 3].map(k => {
      const slot = _kbank * 4 + k;
      const kn = _draft.knobs[slot] || null;
      const t = kn ? kn.type : '';
      const opt = (v, l) => `<option value="${v}"${t === v ? ' selected' : ''}>${l}</option>`;
      return `<div class="km-knob">
        <div class="km-knob-label">KNOB ${k + 1}</div>
        <select class="km-sel" id="fk-type-${k}" onchange="UCS.setKnob(${k})">
          ${opt('', '(nothing)')}${opt('cc', 'CC')}${opt('pb', 'PITCH BEND')}
        </select>
        <div class="fk-row"${kn ? '' : ' style="display:none"'}>
          <label class="ucs-lbl">CH</label>
          <input class="ucs-num fk-num" id="fk-ch-${k}" type="number" min="1" max="16" value="${kn ? kn.channel : 1}" onchange="UCS.setKnob(${k})">
          <label class="ucs-lbl"${t === 'pb' ? ' style="display:none"' : ''}>CC</label>
          <input class="ucs-num fk-num" id="fk-cc-${k}" type="number" min="0" max="127" value="${kn ? kn.cc : 74}" onchange="UCS.setKnob(${k})"${t === 'pb' ? ' style="display:none"' : ''}>
          <label class="ucs-lbl">START</label>
          <input class="ucs-num fk-num" id="fk-st-${k}" type="number" min="0" max="127" value="${kn ? kn.start : 64}" onchange="UCS.setKnob(${k})">
        </div>
        <div class="km-cur">${kn ? _esc(kn.type === 'pb' ? `pitch bend · ch${kn.channel}` : `CC${kn.cc} · ch${kn.channel}`) : 'does nothing'}</div>
      </div>`;
    }).join('');
  }

  function setKBank(b) { _kbank = b; _renderKnobs(); }

  function setKnob(k) {
    const slot = _kbank * 4 + k;
    const t = $('fk-type-' + k).value;
    if (!t) { delete _draft.knobs[slot]; }
    else {
      const old = _draft.knobs[slot];
      let ch, cc, st;
      try {
        ch = _num('fk-ch-' + k, 1, 16, 'CH');
        cc = _num('fk-cc-' + k, 0, 127, 'CC');
        st = _num('fk-st-' + k, 0, 127, 'START');
      } catch (e) { _result(e.message, 'bad'); return; }
      if (!old && t === 'pb') st = 64;      // a new pitch bend knob starts centred
      _draft.knobs[slot] = {slot, type: t, channel: ch, cc, start: st};
    }
    _setDirty(true);
    _result('');
    _renderKnobs();
  }

  // ── Send / clear ─────────────────────────────────────────────
  function _setDirty(on) {
    _dirty = !!on;
    const el = $('fup-dirty');
    if (el) el.textContent = _dirty ? '● changes not sent to the Fire yet' : (_loaded ? 'same as on the Fire' : '');
  }

  function _result(msg, kind) {
    const el = $('fup-result');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'fup-result' + (kind ? ' ' + kind : '');
  }

  async function send() {
    if (_busy) return;
    if (!_info || !_info.ok) { _result(_info && _info.error ? _info.error : 'no Fire', 'bad'); return; }
    _busy = true;
    const btn = $('fup-send-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'SENDING…'; }
    _result(`writing page ${_page + 1} to the Fire…`);
    try {
      const r = await fetch('/api/fire_pages/' + _page, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(_fromDraft())});
      const d = await r.json();
      if (d.ok && d.verified) {
        _draft = _toDraft(d.data); _loaded = true; _setDirty(false);
        _renderAll();
        _result(`saved on the Fire: page ${_page + 1}, ${d.len} bytes, read back and checked`, 'good');
        _flash('universal-card');
      } else {
        _result('NOT saved: ' + (d.error || 'no answer'), 'bad');
      }
    } catch (e) {
      _result('NOT saved: cannot reach the ORC', 'bad');
      console.error('[fire page write]', e);
    } finally {
      _busy = false;
      if (btn) { btn.disabled = false; btn.textContent = 'SEND TO FIRE'; }
      await loadInfo();
      _renderBankTabs();
    }
  }

  async function clearAll() {
    if (_busy) return;
    if (!confirm(`Empty page ${_page + 1} ON THE FIRE (every pad and knob)?`)) return;
    _busy = true;
    _result(`clearing page ${_page + 1} on the Fire…`);
    try {
      const r = await fetch(`/api/fire_pages/${_page}/clear`, {method: 'POST'});
      const d = await r.json();
      if (d.ok && d.verified) {
        _draft = _emptyDraft(); _loaded = true; _setDirty(false);
        closeEditor(); _renderAll();
        _result(`page ${_page + 1} is empty on the Fire`, 'good');
      } else {
        _result('NOT cleared: ' + (d.error || 'no answer'), 'bad');
      }
    } catch (e) {
      _result('NOT cleared: cannot reach the ORC', 'bad');
      console.error('[fire page clear]', e);
    } finally {
      _busy = false;
      await loadInfo();
      _renderBankTabs();
    }
  }

  function _flash(cardId) {
    const el = $(cardId);
    if (!el) return;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }

  return { init, reload, setLayer, setBank, select, onType, onColour, onName,
           savePad, clearPad, clearAll, closeEditor, setKBank, setKnob, send };
})();

document.addEventListener('DOMContentLoaded', UCS.init);
