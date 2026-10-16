'use strict';
/* ORC-1 · beatstep.js — BeatStep Pro global-settings config via amidi.
 *
 * Data-driven: every control on this page is generated from
 * /static/bsp_params.json, which is the decoded MCC "global parameters"
 * dictionary (paramId 65). Each param becomes a SysEx write:
 *
 *     F0 00 20 6B 7F 42 02 00 41 <globalParamId> <value> F7
 *
 * The backend (/api/beatstep/send) already emits exactly this; we only pass
 * the param byte (= globalParamId, hex) and the value byte (hex). So adding a
 * param here is pure data — no protocol code changes.
 *
 * The BSP is WRITE-ONLY over this path (it does not report its current values
 * back), so highlighted/selected state reflects what YOU set this session,
 * seeded from the device factory defaults. */

const BSP = (() => {
  let _ft = null;
  // live "what we've set this session" per globalParamId (decimal) → value.
  // Seeded from defaults; used for optimistic highlighting AND condition gating.
  const state = {};

  function _hx(n) { return (n & 0xFF).toString(16).padStart(2, '0').toUpperCase(); }

  function _noteName(n) {
    const N = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
    return N[((n % 12) + 12) % 12] + (Math.floor(n / 12) - 1); // C4 = 60
  }

  function _flash(ok, msg) {
    const el = document.getElementById('bsp-flash');
    if (!el) return;
    el.textContent = ok ? ('✓ ' + (msg || 'sent')) : ('✗ ' + (msg || 'error'));
    el.className = 'bsp-flash ' + (ok ? 'ok' : 'err');
    clearTimeout(_ft);
    _ft = setTimeout(() => { el.textContent = ''; el.className = 'bsp-flash'; }, 2200);
  }

  // Low-level: send explicit hex paramId + param(itemId/globalParamId) + value.
  async function sendRaw(paramIdHex, paramHex, valueHex, label) {
    try {
      const r = await fetch('/api/beatstep/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paramId: paramIdHex, param: paramHex, value: valueHex })
      });
      const d = await r.json();
      d.ok ? _flash(true, label || (paramHex + ' → ' + valueHex)) : _flash(false, d.error);
      return d.ok;
    } catch (e) { _flash(false, e.message); return false; }
  }

  // Global-bank convenience (paramId 41): send param + value.
  function sendHex(paramHex, valueHex, label) { return sendRaw('41', paramHex, valueHex, label); }

  // High-level: set a global param (decimal gp) to a decimal value.
  // Records into state, re-applies conditional visibility, sends.
  async function setParam(gp, value, label) {
    state[gp] = value;
    _applyConditions();
    return sendHex(_hx(gp), _hx(value), label);
  }

  // ── condition gating ────────────────────────────────────────────
  // A param row carries data-cond-gp / data-cond-vals; it is shown only when
  // state[condGp] is one of condVals. Gating params on this device: 39 (Drum
  // Map: 0=Custom), 70/72 (Seq1/2 Pitch: 0=V/Oct, 1=Hz/Volt).
  function _applyConditions() {
    document.querySelectorAll('[data-cond-gp]').forEach(row => {
      const cg = parseInt(row.getAttribute('data-cond-gp'));
      const vals = row.getAttribute('data-cond-vals').split(',').map(Number);
      row.style.display = vals.includes(state[cg]) ? '' : 'none';
    });
  }

  // ── control renderers (return HTML string; wiring via delegated handlers) ──

  function _segLabel(p, o) { return o.label; }

  function _renderControl(p) {
    const gp = p.gp;
    if (p.kind === 'toggle') {
      // ON=127 / OFF=0
      return `<div class="seg" data-gp="${gp}" data-ctl="toggle">
        <button class="btn" data-v="127">ON</button>
        <button class="btn" data-v="0">OFF</button></div>`;
    }
    if (p.kind === 'list') {
      const opts = p.options;
      if (opts.length <= 4) {
        return `<div class="seg" data-gp="${gp}" data-ctl="seg">` +
          opts.map(o => `<button class="btn" data-v="${o.v}">${_segLabel(p,o)}</button>`).join('') +
          `</div>`;
      }
      return `<select class="bsp-sel" data-gp="${gp}" data-ctl="select">` +
        opts.map(o => `<option value="${o.v}">${o.label}</option>`).join('') +
        `</select>`;
    }
    if (p.kind === 'channel') {
      let html = '';
      for (let i = 1; i <= 16; i++) html += `<option value="${i - 1}">CH ${i}</option>`;
      if (p.drum) html += `<option value="66">DRUM CH</option>`;   // 0x42
      if (p.user) html += `<option value="65">USER</option>`;      // 0x41
      if (p.all)  html += `<option value="126">ALL</option>`;      // 0x7E
      return `<select class="bsp-sel" data-gp="${gp}" data-ctl="select">${html}</select>`;
    }
    if (p.kind === 'note') {
      const dv = p.default;
      return `<div class="bsp-num-row">
        <input type="number" class="bsp-num" data-gp="${gp}" data-ctl="note"
               min="0" max="127" value="${dv}">
        <span class="bsp-note-lbl" data-note-for="${gp}">${_noteName(dv)}</span>
        <button class="btn accent" data-set="${gp}">SET</button></div>`;
    }
    if (p.kind === 'range') {
      const dv = p.default, mn = p.min ?? 0, mx = p.max ?? 127;
      return `<div class="bsp-num-row">
        <input type="number" class="bsp-num" data-gp="${gp}" data-ctl="num"
               min="${mn}" max="${mx}" value="${dv}">
        <span style="font-size:10px;color:var(--muted)">${mn}–${mx}</span>
        <button class="btn accent" data-set="${gp}">SET</button></div>`;
    }
    return `<span class="bsp-note">(unsupported)</span>`;
  }

  function _renderParamRow(p) {
    const cond = p.cond
      ? ` data-cond-gp="${p.cond.gp}" data-cond-vals="${p.cond.vals.join(',')}"`
      : '';
    return `<div class="bsp-row"${cond}>
      <span class="bsp-lbl">${p.name}</span>
      <div class="bsp-right">${_renderControl(p)}</div></div>`;
  }

  function _renderSection(sec) {
    return `<section class="card">
      <div class="card-label">${sec.group}</div>
      ${sec.params.map(_renderParamRow).join('')}
    </section>`;
  }

  // ── highlight helpers ───────────────────────────────────────────
  function _syncSegHighlight(gp) {
    document.querySelectorAll(`.seg[data-gp="${gp}"] button`).forEach(b => {
      b.classList.toggle('on', parseInt(b.getAttribute('data-v')) === state[gp]);
    });
  }
  function _syncAllHighlights() {
    document.querySelectorAll('.seg[data-gp]').forEach(seg => {
      _syncSegHighlight(parseInt(seg.getAttribute('data-gp')));
    });
    document.querySelectorAll('select[data-gp]').forEach(sel => {
      const gp = parseInt(sel.getAttribute('data-gp'));
      if (state[gp] === undefined) return;
      const want = String(state[gp]);
      sel.value = want;
      if (sel.value !== want) {   // device value not among the options — surface it
        const o = document.createElement('option');
        o.value = want; o.textContent = `(${want})`;
        sel.appendChild(o); sel.value = want;
      }
    });
  }

  // ── delegated event wiring ──────────────────────────────────────
  function _wire(root) {
    // seg / toggle buttons
    root.addEventListener('click', ev => {
      const btn = ev.target.closest('.seg button');
      if (btn) {
        const seg = btn.closest('.seg');
        const gp = parseInt(seg.getAttribute('data-gp'));
        const v  = parseInt(btn.getAttribute('data-v'));
        setParam(gp, v, `${_findName(gp)} → ${btn.textContent.trim()}`);
        _syncSegHighlight(gp);
        return;
      }
      const setBtn = ev.target.closest('button[data-set]');
      if (setBtn) {
        const gp = parseInt(setBtn.getAttribute('data-set'));
        const inp = root.querySelector(`input[data-gp="${gp}"]`);
        if (inp) setParam(gp, parseInt(inp.value) || 0, `${_findName(gp)} → ${inp.value}`);
        return;
      }
    });
    // selects (channel + long lists) auto-send on change
    root.addEventListener('change', ev => {
      const sel = ev.target.closest('select[data-gp]');
      if (sel) {
        const gp = parseInt(sel.getAttribute('data-gp'));
        const opt = sel.options[sel.selectedIndex];
        setParam(gp, parseInt(sel.value), `${_findName(gp)} → ${opt.textContent.trim()}`);
      }
    });
    // live note-name label as you type
    root.addEventListener('input', ev => {
      const inp = ev.target.closest('input[data-ctl="note"]');
      if (inp) {
        const gp = inp.getAttribute('data-gp');
        const lbl = root.querySelector(`[data-note-for="${gp}"]`);
        const n = parseInt(inp.value);
        if (lbl && n >= 0 && n <= 127) lbl.textContent = _noteName(n);
      }
    });
  }

  let _byGp = {};
  function _findName(gp) { return (_byGp[gp] || {}).name || ('param ' + gp); }

  // ── read-back: fetch live values from the device and apply to the UI ──
  async function readValues(pairs) {
    const r = await fetch('/api/beatstep/read', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pairs })
    });
    return r.json();  // {ok, values:{"PPII":int}, missing:[]}
  }

  function _applyValue(gp, v) {
    state[gp] = v;
    // number inputs
    const inp = document.querySelector(`#bsp-body input[data-gp="${gp}"]`);
    if (inp) {
      inp.value = v;
      const lbl = document.querySelector(`#bsp-body [data-note-for="${gp}"]`);
      if (lbl && v >= 0 && v <= 127) lbl.textContent = _noteName(v);
    }
  }

  async function refresh() {
    const btn = document.getElementById('bsp-refresh');
    if (btn) { btn.disabled = true; btn.textContent = '⟳ READING…'; }
    try {
      const pairs = Object.keys(_byGp).map(gp => ['41', _hx(parseInt(gp))]);
      const d = await readValues(pairs);
      if (d.ok) {
        Object.entries(d.values).forEach(([k, v]) => _applyValue(parseInt(k.slice(2), 16), v));
        _syncAllHighlights();
        _applyConditions();
        _flash(true, `read ${Object.keys(d.values).length} values` +
                     (d.missing && d.missing.length ? ` (${d.missing.length} missed)` : ''));
        // also refresh the Tier-2 CONTROL ASSIGN + Tier-3 SEQUENCER editors
        // (they read separately)
        if (typeof BSP2 !== 'undefined' && BSP2.refresh) { try { await BSP2.refresh(); } catch (e) {} }
        if (typeof BSP3 !== 'undefined' && BSP3.refresh) { try { await BSP3.refresh(); } catch (e) {} }
      } else {
        _flash(false, d.error || 'read failed');
      }
    } catch (e) { _flash(false, e.message); }
    finally { if (btn) { btn.disabled = false; btn.textContent = '⟳ REFRESH FROM DEVICE'; } }
  }

  // ── init ────────────────────────────────────────────────────────
  async function init() {
    let table;
    try {
      const r = await fetch('/static/bsp_params.json');
      table = await r.json();
    } catch (e) {
      document.getElementById('bsp-body').innerHTML =
        `<section class="card"><div class="card-label">ERROR</div>
         <p class="bsp-note">Could not load bsp_params.json: ${e.message}</p></section>`;
      return;
    }
    // seed defaults + name index
    table.sections.forEach(sec => sec.params.forEach(p => {
      _byGp[p.gp] = p;
      if (p.default !== undefined) state[p.gp] = p.default;
    }));
    const body = document.getElementById('bsp-body');
    body.innerHTML = table.sections.map(_renderSection).join('');
    _wire(body);
    _syncAllHighlights();
    _applyConditions();
    pollConn();
    // read the device's live values into the UI (non-blocking; no-op if absent)
    refresh().catch(() => {});
  }

  // ── connection poll ─────────────────────────────────────────────
  async function pollConn() {
    try {
      const r = await fetch('/api/beatstep/status');
      const d = await r.json();
      const el = document.getElementById('bsp-conn');
      if (el) {
        el.textContent = d.connected ? `● CONNECTED  ${d.port}` : '○ NOT FOUND — plug BeatStep Pro into the Pi';
        el.className = 'bsp-conn ' + (d.connected ? 'live' : 'dead');
      }
    } catch (e) {}
    setTimeout(pollConn, 6000);
  }

  return { sendHex, sendRaw, setParam, init, refresh, readValues,
           hx: _hx, noteName: _noteName };
})();

// ════════════════════════════════════════════════════════════════════════════
// Tier 2 — per-control ControllerMap assignment (knobs / pads / steps)
// Each control has a Mode (paramId 1); the visible sub-fields depend on that
// Mode (and sometimes an Option, paramId 6). A field edit sends:
//     paramId=<field paramId>, param=<control itemId>, value
// The device is write-only, so the editor reflects what you set this session,
// seeded from a sensible default Mode per family.
// ════════════════════════════════════════════════════════════════════════════
const BSP2 = (() => {
  let TABLE = null;
  let curType = 'knob';
  let curItem = null;                 // itemId (int) of the selected control
  // per-control remembered field values, keyed cstate[type][itemId][paramId] = value
  const cstate = {};
  const lastItem = {};                // remember selected control per family

  const hx = (n) => BSP.hx(n);

  function _st(type, item) {
    cstate[type] = cstate[type] || {};
    if (!cstate[type][item]) {
      // seed Mode to the family default; Option to 0
      cstate[type][item] = { 1: TABLE.types[type].defaultMode, 6: 0 };
    }
    return cstate[type][item];
  }

  // is a field visible for the current control state? (all conds must hold)
  function _visible(field, s) {
    if (!field.conds) return true;
    return field.conds.every(c => c.vals.includes(s[c.p]));
  }

  function _fieldControl(field, s) {
    const pid = field.phex;                         // hex paramId for the send
    const cur = s[field.paramId];
    if (field.kind === 'list' || field.kind === 'mmc') {
      const opts = field.options || [];
      return `<select class="bsp-sel" data-fp="${pid}" data-pidn="${field.paramId}" data-ctl="csel">` +
        opts.map(o => `<option value="${o.v}" ${o.v === cur ? 'selected' : ''}>${o.label}</option>`).join('') +
        `</select>`;
    }
    if (field.kind === 'channel') {
      let h = '';
      for (let i = 1; i <= 16; i++) h += `<option value="${i - 1}" ${(i - 1) === cur ? 'selected' : ''}>CH ${i}</option>`;
      if (field.drum) h += `<option value="66" ${cur === 66 ? 'selected' : ''}>DRUM CH</option>`;
      if (field.user) h += `<option value="65" ${cur === 65 ? 'selected' : ''}>USER</option>`;
      if (field.all)  h += `<option value="126" ${cur === 126 ? 'selected' : ''}>ALL</option>`;
      // fallback so a device value with no matching option is shown, not hidden as CH 1
      if (cur !== undefined && ![...Array(16).keys()].includes(cur) && ![66,65,126].includes(cur))
        h += `<option value="${cur}" selected>(${cur})</option>`;
      return `<select class="bsp-sel" data-fp="${pid}" data-pidn="${field.paramId}" data-ctl="csel">${h}</select>`;
    }
    if (field.kind === 'note') {
      const dv = (cur !== undefined) ? cur : (field.default ?? 60);
      return `<div class="bsp-num-row">
        <input type="number" class="bsp-num" data-fp="${pid}" data-pidn="${field.paramId}" data-ctl="cnote"
               min="0" max="127" value="${dv}">
        <span class="bsp-note-lbl" data-cnote-for="${pid}">${BSP.noteName(dv)}</span>
        <button class="btn accent" data-cset="${pid}" data-pidn="${field.paramId}">SET</button></div>`;
    }
    // range / cc
    const mn = field.min ?? 0, mx = field.max ?? 127;
    const dv = (cur !== undefined) ? cur : (field.default ?? 0);
    return `<div class="bsp-num-row">
      <input type="number" class="bsp-num" data-fp="${pid}" data-pidn="${field.paramId}" data-ctl="cnum"
             min="${mn}" max="${mx}" value="${dv}">
      <span style="font-size:10px;color:var(--muted)">${mn}–${mx}</span>
      <button class="btn accent" data-cset="${pid}" data-pidn="${field.paramId}">SET</button></div>`;
  }

  function _renderEditor() {
    const t = TABLE.types[curType];
    const s = _st(curType, curItem);
    const rows = t.fields.filter(f => f.paramId !== 1)     // Mode gets its own header row
                         .filter(f => _visible(f, s))
                         .map(f => `<div class="bsp-row"><span class="bsp-lbl">${f.name}</span>
                                    <div class="bsp-right">${_fieldControl(f, s)}</div></div>`)
                         .join('');
    const modeOpts = t.modeOptions.map(o =>
      `<option value="${o.v}" ${o.v === s[1] ? 'selected' : ''}>${o.label}</option>`).join('');
    const ctrlName = (TABLE.controls[curType].find(c => c.itemId === curItem) || {}).name || '';
    document.getElementById('bsp2-editor').innerHTML = `
      <div class="bsp-row"><span class="bsp-lbl">MODE — ${ctrlName}</span>
        <div class="bsp-right"><select class="bsp-sel" id="bsp2-mode">${modeOpts}</select></div></div>
      ${rows || '<p class="bsp-note">This mode has no extra parameters.</p>'}`;
  }

  function _renderControlPicker() {
    const list = TABLE.controls[curType];
    curItem = lastItem[curType] || list[0].itemId;   // remember per-family selection
    const opts = list.map(c =>
      `<option value="${c.itemId}" ${c.itemId === curItem ? 'selected' : ''}>${c.name}</option>`).join('');
    document.getElementById('bsp2-control').innerHTML = opts;
    _renderEditor();
    _readControl();
  }

  // Read this control's live values back from the device: every paramId that
  // any of its fields uses (Mode=1, plus 2..8). The reply for (paramId,item)
  // reflects whatever that paramId currently holds — i.e. the active mode's
  // field — so we can seed the whole editor from reality.
  async function _readControl() {
    if (!BSP.readValues) return;
    const item = curItem, type = curType;
    const pids = [...new Set(TABLE.types[type].fields.map(f => f.paramId).concat([1]))];
    const pairs = pids.map(p => [BSP.hx(p), BSP.hx(item)]);
    let d;
    try { d = await BSP.readValues(pairs); } catch (e) { return; }
    // ignore if the user moved on to another control meanwhile
    if (!d || !d.ok || curItem !== item || curType !== type) return;
    const s = _st(type, item);
    Object.entries(d.values).forEach(([k, v]) => { s[parseInt(k.slice(0, 2), 16)] = v; });
    _renderEditor();
  }

  // public refresh: re-read the currently-shown control from the device
  function refresh() { return _readControl(); }

  function _send(fpHex, pidNum, value, label) {
    const s = _st(curType, curItem);
    s[pidNum] = value;
    BSP.sendRaw(fpHex, hx(curItem), hx(value), label);
    // Mode/Option changes alter which fields show; the newly-visible fields
    // reuse shared paramIds (3/4/5), so re-read from the device to show their
    // real values instead of the previous field's leftover.
    if (pidNum === 1 || pidNum === 6) { _renderEditor(); _readControl(); }
  }

  function _wire(root) {
    root.addEventListener('change', ev => {
      // family tabs handled by buttons; here: control picker, mode, field selects
      const ctl = ev.target;
      if (ctl.id === 'bsp2-control') { curItem = parseInt(ctl.value); lastItem[curType] = curItem; _renderEditor(); _readControl(); return; }
      if (ctl.id === 'bsp2-mode') {
        _send('01', 1, parseInt(ctl.value), `${_curName()} Mode → ${ctl.options[ctl.selectedIndex].textContent}`);
        return;
      }
      const sel = ctl.closest && ctl.closest('select[data-ctl="csel"]');
      if (sel) {
        _send(sel.getAttribute('data-fp'), parseInt(sel.getAttribute('data-pidn')),
              parseInt(sel.value), `${_curName()} → ${sel.options[sel.selectedIndex].textContent.trim()}`);
      }
    });
    root.addEventListener('click', ev => {
      const fam = ev.target.closest('button[data-fam]');
      if (fam) {
        curType = fam.getAttribute('data-fam');
        root.querySelectorAll('button[data-fam]').forEach(b =>
          b.classList.toggle('on', b === fam));
        _renderControlPicker();
        return;
      }
      const setb = ev.target.closest('button[data-cset]');
      if (setb) {
        const fp = setb.getAttribute('data-cset');
        const inp = document.querySelector(`#bsp2-editor input[data-fp="${fp}"]`);
        if (inp) _send(fp, parseInt(setb.getAttribute('data-pidn')),
                       parseInt(inp.value) || 0, `${_curName()} → ${inp.value}`);
      }
    });
    root.addEventListener('input', ev => {
      const inp = ev.target.closest('input[data-ctl="cnote"]');
      if (inp) {
        const fp = inp.getAttribute('data-fp');
        const lbl = document.querySelector(`#bsp2-editor [data-cnote-for="${fp}"]`);
        const n = parseInt(inp.value);
        if (lbl && n >= 0 && n <= 127) lbl.textContent = BSP.noteName(n);
      }
    });
  }

  function _curName() {
    return (TABLE.controls[curType].find(c => c.itemId === curItem) || {}).name || '';
  }

  async function init() {
    const mount = document.getElementById('bsp2');
    if (!mount) return;
    try {
      TABLE = await (await fetch('/static/bsp_controls.json')).json();
    } catch (e) {
      mount.innerHTML = `<p class="bsp-note">Could not load bsp_controls.json: ${e.message}</p>`;
      return;
    }
    mount.innerHTML = `
      <div class="bsp-row"><span class="bsp-lbl">CONTROL</span>
        <div class="bsp-right">
          <div class="seg" style="margin-right:8px">
            <button class="btn on" data-fam="knob">KNOBS</button>
            <button class="btn" data-fam="pad">PADS</button>
            <button class="btn" data-fam="step">STEPS</button>
          </div>
          <select class="bsp-sel" id="bsp2-control"></select>
        </div></div>
      <div id="bsp2-editor"></div>`;
    _wire(mount);
    _renderControlPicker();
  }

  return { init, refresh };
})();

// ════════════════════════════════════════════════════════════════════════════
// Tier 3 — sequencer / project scalar settings (Scale, Transpose, Direction,
// Time Division, Length, Swing, Randomness, Probability, Triplet, Polyrhythm).
// Each "unit" (Project / Seq 1 / Seq 2 / Drum) is one card; a control edit sends
//     paramId=<field paramId>, param=<unit itemId>, value
// via the SAME uniform frame as Tiers 1-2. A write edits that sequencer's
// CURRENTLY SELECTED pattern (verified: the device ignores pattern/step address
// bytes, so per-step data is out of scope — see ORC1_BSP_Tier3_Discovery.md).
// Unlike the global bank, these DO read back, so the editor shows live values.
// ════════════════════════════════════════════════════════════════════════════
const BSP3 = (() => {
  let TABLE = null;
  const cstate = {};                     // cstate[unitKey][paramId] = value
  const hx = (n) => BSP.hx(n);

  function _st(key) { cstate[key] = cstate[key] || {}; return cstate[key]; }
  function _unit(key) { return TABLE.units.find(u => u.key === key); }
  function _nm(key) { const u = _unit(key); return u ? u.name : key; }

  function _control(unit, p) {
    const s = _st(unit.key), cur = s[p.paramId], pid = p.phex, u = unit.key;
    if (p.kind === 'list') {
      const opts = p.options || [];
      if (opts.length <= 4) {
        return `<div class="seg" data-u="${u}" data-fp="${pid}" data-pidn="${p.paramId}" data-ctl="cseg">` +
          opts.map(o => `<button class="btn" data-v="${o.v}">${o.label}</button>`).join('') + `</div>`;
      }
      return `<select class="bsp-sel" data-u="${u}" data-fp="${pid}" data-pidn="${p.paramId}" data-ctl="csel">` +
        opts.map(o => `<option value="${o.v}" ${o.v === cur ? 'selected' : ''}>${o.label}</option>`).join('') +
        `</select>`;
    }
    // range
    const mn = p.min ?? 0, mx = p.max ?? 127, dv = (cur !== undefined) ? cur : (p.default ?? mn);
    return `<div class="bsp-num-row">
      <input type="number" class="bsp-num" data-u="${u}" data-fp="${pid}" data-pidn="${p.paramId}" data-ctl="cnum"
             min="${mn}" max="${mx}" value="${dv}">
      <span style="font-size:10px;color:var(--muted)">${mn}–${mx}</span>
      <button class="btn accent" data-cset="${pid}" data-u="${u}" data-pidn="${p.paramId}">SET</button></div>`;
  }

  function _renderUnit(unit) {
    return `<section class="card"><div class="card-label">${unit.name}</div>` +
      unit.params.map(p => `<div class="bsp-row"><span class="bsp-lbl">${p.name}</span>
        <div class="bsp-right">${_control(unit, p)}</div></div>`).join('') +
      `</section>`;
  }

  function _syncUnit(key) {
    const s = _st(key);
    document.querySelectorAll(`#bsp3 .seg[data-u="${key}"]`).forEach(seg => {
      const pidn = parseInt(seg.getAttribute('data-pidn'));
      seg.querySelectorAll('button').forEach(b =>
        b.classList.toggle('on', parseInt(b.getAttribute('data-v')) === s[pidn]));
    });
    document.querySelectorAll(`#bsp3 select[data-u="${key}"]`).forEach(sel => {
      const pidn = parseInt(sel.getAttribute('data-pidn'));
      if (s[pidn] === undefined) return;
      const want = String(s[pidn]);
      sel.value = want;
      if (sel.value !== want) {   // device value not among options — surface it
        const o = document.createElement('option');
        o.value = want; o.textContent = `(${want})`;
        sel.appendChild(o); sel.value = want;
      }
    });
    document.querySelectorAll(`#bsp3 input[data-u="${key}"]`).forEach(inp => {
      const pidn = parseInt(inp.getAttribute('data-pidn'));
      if (s[pidn] !== undefined && document.activeElement !== inp) inp.value = s[pidn];
    });
  }

  function _send(key, fpHex, pidNum, value, label) {
    const u = _unit(key);
    if (!u) return;
    _st(key)[pidNum] = value;
    BSP.sendRaw(fpHex, u.ihex, hx(value), label);
    _syncUnit(key);
  }

  function _wire(root) {
    root.addEventListener('click', ev => {
      const b = ev.target.closest('.seg button');
      if (b) {
        const seg = b.closest('.seg'), key = seg.getAttribute('data-u');
        _send(key, seg.getAttribute('data-fp'), parseInt(seg.getAttribute('data-pidn')),
              parseInt(b.getAttribute('data-v')), `${_nm(key)} ${b.textContent.trim()}`);
        return;
      }
      const setb = ev.target.closest('button[data-cset]');
      if (setb) {
        const key = setb.getAttribute('data-u'), fp = setb.getAttribute('data-cset');
        const inp = root.querySelector(`input[data-u="${key}"][data-fp="${fp}"]`);
        if (inp) _send(key, fp, parseInt(setb.getAttribute('data-pidn')),
                       parseInt(inp.value) || 0, `${_nm(key)} → ${inp.value}`);
      }
    });
    root.addEventListener('change', ev => {
      const sel = ev.target.closest('select[data-ctl="csel"]');
      if (sel) {
        const key = sel.getAttribute('data-u');
        _send(key, sel.getAttribute('data-fp'), parseInt(sel.getAttribute('data-pidn')),
              parseInt(sel.value), `${_nm(key)} → ${sel.options[sel.selectedIndex].textContent.trim()}`);
      }
    });
  }

  // Read every unit's params back from the device (they DO report values).
  async function refresh() {
    if (!TABLE || !BSP.readValues) return;
    const pairs = [];
    TABLE.units.forEach(u => u.params.forEach(p => pairs.push([p.phex, u.ihex])));
    let d;
    try { d = await BSP.readValues(pairs); } catch (e) { return; }
    if (!d || !d.ok) return;
    Object.entries(d.values).forEach(([k, v]) => {
      const pid = parseInt(k.slice(0, 2), 16), item = parseInt(k.slice(2), 16);
      const u = TABLE.units.find(u => u.item === item);
      if (u) _st(u.key)[pid] = v;
    });
    TABLE.units.forEach(u => _syncUnit(u.key));
  }

  async function init() {
    const mount = document.getElementById('bsp3');
    if (!mount) return;
    try {
      TABLE = await (await fetch('/static/bsp_seq.json')).json();
    } catch (e) {
      mount.innerHTML = `<p class="bsp-note">Could not load bsp_seq.json: ${e.message}</p>`;
      return;
    }
    // seed defaults
    TABLE.units.forEach(u => u.params.forEach(p => {
      if (p.default !== undefined) _st(u.key)[p.paramId] = p.default;
    }));
    mount.innerHTML = TABLE.units.map(_renderUnit).join('');
    _wire(mount);
    TABLE.units.forEach(u => _syncUnit(u.key));
    refresh().catch(() => {});
  }

  return { init, refresh };
})();

document.addEventListener('DOMContentLoaded', () => { BSP.init(); BSP2.init(); BSP3.init(); });
