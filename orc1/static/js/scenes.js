/* PLAY page: SCENES (2026-10-26).
   The brain owns the scenes (orc1_scenes.py); this page only talks to
   /api/scenes* (orc1_web.py), which relays to the brain's SCENE_* actions.
   - A scene = slot 1-128: a name, a default timing, and only the ticked
     settings, each with a value (null = ticked, value not known yet).
   - Ticking / unticking / editing a value saves at once (SCENE_SET).
   - CAPTURE reads the current value of every ticked setting the ORC can read
     back; values typed for settings it cannot read are kept.
   - The settings list (orc1_settings_registry, sceneable only) is grouped by
     the first part of its "thing"; a group's rows are built only when it is
     opened, so 380 settings cost nothing until looked at. */
'use strict';

const SC = {
  slots: {},          // "<n>" -> {name, timing, values}
  status: null,       // SCENE_LIST status
  listErr: '',
  settings: null,     // [entry] once loaded
  byId: {},
  groups: [],         // [{top, subs: [{thing, ids}], ids}]
  sel: 1,
  override: '',       // recall timing override ('' = the scene's own)
  tickedOnly: false,
  confirmClear: false,
  msg: '', msgCls: '',
  shownSig: '',       // what the editor was last built from
  busy: false,
};
const SC_TIMINGS = ['IMMEDIATE', 'NEXT_BEAT', 'NEXT_BAR', 'BARS_2', 'BARS_4'];
const SC_TLABEL = {IMMEDIATE: 'IMMEDIATE', NEXT_BEAT: 'NEXT BEAT', NEXT_BAR: 'NEXT BAR',
                   BARS_2: 'AFTER 2 BARS', BARS_4: 'AFTER 4 BARS'};
const SC_N = 128;

/* ── small helpers ──────────────────────────────────────────────────────── */
function scEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}
function scLsGet(k, dflt) {
  try { const v = localStorage.getItem(k); return v === null ? dflt : v; } catch (e) { return dflt; }
}
function scLsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function scFoldOpen(name, dflt) { return scLsGet('orc.fold.' + name, dflt ? '1' : '0') === '1'; }

async function scApi(method, url, body) {
  try {
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json'},
                                body: body === undefined ? undefined : JSON.stringify(body)});
    const d = await r.json().catch(() => ({ok: false, error: 'HTTP ' + r.status}));
    return d && typeof d === 'object' ? d : {ok: false, error: 'bad reply'};
  } catch (e) {
    return {ok: false, error: 'No response from the ORC web server'};
  }
}

function scSlot(n) { return SC.slots[String(n)] || null; }
function scValues(n) { const s = scSlot(n); return (s && s.values) || {}; }
function scLabel(id) { const e = SC.byId[id]; return e ? e.label : id; }
function scSlotName(n) { const s = scSlot(n); return s && s.name ? s.name : ''; }
function scSlotTitle(n) { const nm = scSlotName(n); return 'scene ' + n + (nm ? ' "' + nm + '"' : ''); }

/* Remember open/closed for every <details data-fold> on the page. */
function scBindFolds(root) {
  (root || document).querySelectorAll('details[data-fold]').forEach(d => {
    if (d._scBound) return;
    d._scBound = true;
    d.addEventListener('toggle', () => {
      scLsSet('orc.fold.' + d.dataset.fold, d.open ? '1' : '0');
      if (d.open && d.dataset.group !== undefined) scFillGroup(d);
    });
  });
}

/* ── settings list ──────────────────────────────────────────────────────── */
async function scLoadSettings() {
  const d = await scApi('GET', '/api/scenes/settings');
  if (!d.ok) {
    SC.settingsErr = d.loading ? 'Settings list loading…' : (d.error || 'Settings list unavailable');
    scRenderEditor(true);
    setTimeout(scLoadSettings, 3000);
    return;
  }
  SC.settingsErr = '';
  SC.settings = d.settings || [];
  SC.byId = {};
  const groups = [], gi = {};
  for (const e of SC.settings) {
    SC.byId[e.id] = e;
    const parts = String(e.thing || 'Other').split(' > ');
    const top = parts[0];
    if (!(top in gi)) { gi[top] = groups.length; groups.push({top, subs: [], subi: {}, ids: []}); }
    const g = groups[gi[top]];
    if (!(e.thing in g.subi)) { g.subi[e.thing] = g.subs.length; g.subs.push({thing: e.thing, sub: parts.slice(1).join(' > '), ids: []}); }
    g.subs[g.subi[e.thing]].ids.push(e.id);
    g.ids.push(e.id);
  }
  SC.groups = groups;
  scRenderEditor(true);
}

/* ── list + status poll ─────────────────────────────────────────────────── */
let _scTimer = null;
async function scPoll() {
  clearTimeout(_scTimer);
  const d = await scApi('GET', '/api/scenes');
  if (d.ok) {
    SC.slots = d.slots || {};
    SC.status = d.status || null;
    SC.listErr = '';
  } else {
    SC.listErr = d.error || 'Scenes unavailable';
  }
  scRenderStatus();
  scRenderSlots();
  scRenderEditor(false);
  const pending = SC.status && SC.status.pending;
  _scTimer = setTimeout(scPoll, pending ? 1000 : 2500);
}

function scRenderStatus() {
  const src = document.getElementById('sc-src');
  const el = document.getElementById('sc-status');
  if (!el) return;
  if (SC.listErr) {
    src.className = 'sc-chip danger'; src.textContent = 'OFFLINE';
    el.innerHTML = '<span class="sc-bad">' + scEsc(SC.listErr) + '</span>';
    return;
  }
  const st = SC.status || {};
  const bs = st.bar_source;
  src.className = 'sc-chip ' + (bs === 'fire' ? 'live' : bs === 'orc' ? 'accent' : '');
  src.textContent = bs === 'fire' ? 'BAR: FIRE' : bs === 'orc' ? 'BAR: ORC CLOCK' : 'STOPPED';
  src.title = bs === 'stopped' || !bs ? 'Nothing playing: a recall lands at once'
                                      : 'Where NEXT BEAT / NEXT BAR are counted from';
  const lines = [];
  const p = st.pending;
  if (p) {
    let when = SC_TLABEL[p.timing] || p.timing;
    if (p.beats_left) when = 'at the next beat';
    else if (p.bars_left === 1) when = 'at the next bar';
    else if (p.bars_left > 1) when = 'in ' + p.bars_left + ' bars';
    lines.push('<span class="sc-pend">● ' + scEsc(scSlotTitle(p.slot)) + ' waits: lands ' + scEsc(when)
      + (p.bar_source === 'fire' ? ' (Fire bar)' : p.bar_source === 'orc' ? ' (ORC clock)' : '') + '</span>');
  }
  const l = st.last;
  if (l) {
    const ago = l.at ? Math.max(0, Math.round(Date.now() / 1000 - l.at)) : null;
    const agoS = ago === null ? '' : ago < 90 ? ago + ' s ago' : Math.round(ago / 60) + ' min ago';
    const unch = (l.unchanged || []).length, skip = (l.skipped || []).length;
    let s = 'Last: ' + scEsc('scene ' + l.slot + (l.name ? ' "' + l.name + '"' : '')) + (agoS ? ', ' + agoS : '')
      + ' · ' + l.sent + ' sent · ' + unch + ' already set · '
      + (skip ? '<span class="sc-pend">' + skip + ' skipped</span>' : '0 skipped') + ' · '
      + (l.failed ? '<span class="sc-bad">' + l.failed + ' failed</span>' : '0 failed');
    const why = [];
    for (const [id, w] of (l.skipped || [])) why.push('<li>' + scEsc(scLabel(id)) + ': ' + scEsc(w) + '</li>');
    for (const [id, w] of (l.errors || [])) why.push('<li class="sc-bad">' + scEsc(scLabel(id)) + ': ' + scEsc(w) + '</li>');
    if (why.length) s += '<details><summary>WHY</summary><ul>' + why.join('') + '</ul></details>';
    lines.push(s);
  }
  if (!lines.length) lines.push('<span style="color:var(--muted)">No scene recalled yet.</span>');
  el.innerHTML = lines.join('<br>');
}

function scRenderSlots() {
  const pend = SC.status && SC.status.pending ? SC.status.pending.slot : null;
  const filled = Object.keys(SC.slots).map(Number).filter(n => n >= 1 && n <= SC_N).sort((a, b) => a - b);
  const f = document.getElementById('sc-filled');
  if (f) {
    const add = `<button class="sc-slot" type="button" onclick="scNew()"
         title="Opens the first empty slot">+ NEW SCENE</button>`;
    f.innerHTML = (filled.length
      ? filled.map(n => `<button class="sc-slot${n === SC.sel ? ' sel' : ''}${n === pend ? ' pend' : ''}" type="button"
           onclick="scSelect(${n})" title="Scene ${n}"><b>${n}</b>${scEsc(scSlotName(n) || 'UNTITLED')}</button>`).join('')
      : '<span class="sc-none">No scenes yet. Press + NEW SCENE, tick settings, then CAPTURE.</span>') + add;
  }
  const g = document.getElementById('sc-grid');
  if (g) {
    let h = '';
    for (let n = 1; n <= SC_N; n++) {
      const c = 'sc-cell' + (scSlot(n) ? ' filled' : '') + (n === SC.sel ? ' sel' : '') + (n === pend ? ' pend' : '');
      const nm = scSlotName(n);
      h += `<button class="${c}" type="button" onclick="scSelect(${n})" title="${scEsc('Scene ' + n + (nm ? ': ' + nm : scSlot(n) ? '' : ' (empty)'))}">${n}</button>`;
    }
    g.innerHTML = h;
  }
}

/* + NEW SCENE: the lowest empty slot (a specific number, e.g. for CC 42, is under PICK A SLOT NUMBER). */
function scNew() {
  for (let n = 1; n <= SC_N; n++) if (!scSlot(n)) { scSelect(n); return; }
}

function scSelect(n) {
  SC.sel = n;
  SC.opened = true;
  SC.confirmClear = false;
  SC.msg = '';
  scLsSet('orc.scenes.sel', String(n));
  scRenderSlots();
  scRenderEditor(true);
}

/* ── the editor ─────────────────────────────────────────────────────────── */
function scTicked(n) { return Object.keys(scValues(n)); }

function scNeedCount(n) {
  const v = scValues(n);
  return Object.keys(v).filter(id => v[id] === null && SC.byId[id] && !SC.byId[id].readable).length;
}

function scRenderEditor(force) {
  const box = document.getElementById('sc-editor');
  if (!box) return;
  const n = SC.sel;
  const sig = JSON.stringify([n, scSlot(n), !!SC.settings, SC.settingsErr || '', SC.tickedOnly]);
  if (!force && sig === SC.shownSig) return;
  // never rebuild under the user's fingers: retry on the next poll
  if (box.contains(document.activeElement) && document.activeElement.matches('input[type=text],input[type=number],input:not([type]),select')) return;
  SC.shownSig = sig;
  const s = scSlot(n);
  // An empty slot only opens when picked on this visit (+ NEW SCENE or a slot number),
  // so the card starts as just your scenes.
  if (!s && !SC.opened) { box.innerHTML = ''; return; }
  const vals = scValues(n);
  const ticked = Object.keys(vals);
  const need = scNeedCount(n);
  const timing = (s && s.timing) || 'NEXT_BAR';
  const tOpts = (cur, withOwn) => (withOwn ? `<option value=""${cur === '' ? ' selected' : ''}>ITS OWN (${SC_TLABEL[timing]})</option>` : '')
    + SC_TIMINGS.map(t => `<option value="${t}"${t === cur ? ' selected' : ''}>${SC_TLABEL[t]}</option>`).join('');
  const readableTicked = ticked.filter(id => SC.byId[id] && SC.byId[id].readable);
  const title = scSlotTitle(n);

  let h = `<div class="sc-ed">
    <div class="sc-ed-head"><span class="sc-ed-title">SCENE ${n}</span>
      ${s ? '' : '<span class="sc-chip">EMPTY</span>'}</div>
    <div class="sc-row">
      <label class="sc-field"><span>NAME</span>
        <input class="sc-in sc-name" id="sc-name" maxlength="40" value="${scEsc(s ? s.name : '')}" placeholder="untitled"
               onchange="scSetName(this.value)" onkeydown="if(event.key==='Enter')this.blur()"></label>
      <label class="sc-field" title="When a recall lands, unless the trigger says otherwise"><span>TIMING</span>
        <select class="sc-sel" id="sc-timing" onchange="scSetTiming(this.value)">${tOpts(timing, false)}</select></label>
    </div>
    <div class="sc-actions">
      <button class="flag accent" type="button" onclick="scRecall()" ${ticked.length ? '' : 'disabled'}
              title="Sets every ticked setting">▶ RECALL</button>
      <select class="sc-sel" id="sc-override" onchange="SC.override=this.value" title="Timing for this recall">${tOpts(SC.override, true)}</select>
      <button class="flag on" type="button" onclick="scCapture()" ${readableTicked.length ? '' : 'disabled'}
              title="Stores the current value of each ticked setting">● CAPTURE</button>
      <button class="flag danger" type="button" onclick="scAskClear()" ${s ? '' : 'disabled'}
              title="Empties this slot">CLEAR</button>
    </div>`;
  if (SC.confirmClear && s) {
    h += `<div class="sc-confirm">Clear ${scEsc(title)}? Its name and ${ticked.length} setting${ticked.length === 1 ? '' : 's'} are removed. This cannot be undone.
      <div class="flags"><button class="flag danger" type="button" onclick="scClear()">CLEAR SCENE ${n}</button>
      <button class="flag" type="button" onclick="SC.confirmClear=false;scRenderEditor(true)">CANCEL</button></div></div>`;
  }
  h += `<div class="sc-msg ${SC.msgCls}" id="sc-msg">${scEsc(SC.msg)}</div>
    <div class="sc-count"><span>${ticked.length} ticked${need ? ' · <span style="color:var(--warn)">' + need + ' need a value</span>' : ''}</span>
      <button class="flag ${SC.tickedOnly ? 'accent' : ''}" type="button" onclick="scToggleTickedOnly()">TICKED ONLY</button></div>`;

  if (!SC.settings) {
    h += `<div class="sc-msg">${scEsc(SC.settingsErr || 'Loading the settings list…')}</div>`;
  } else {
    for (let gi = 0; gi < SC.groups.length; gi++) {
      const g = SC.groups[gi];
      const nt = g.ids.filter(id => id in vals).length;
      if (SC.tickedOnly && !nt) continue;
      const open = scFoldOpen('scg.' + g.top, false);
      h += `<details class="fold sc-grp" data-fold="scg.${scEsc(g.top)}" data-group="${gi}"${open ? ' open' : ''}>
        <summary>${scEsc(g.top.toUpperCase())}<span class="fold-count">${nt} / ${g.ids.length}</span></summary>
        <div class="sc-grp-body"></div></details>`;
    }
    const stale = ticked.filter(id => !SC.byId[id]);
    if (stale.length) {
      h += `<details class="fold sc-grp" open><summary>NOT IN THIS ORC VERSION<span class="fold-count">${stale.length}</span></summary>
        <div class="sc-grp-body">${stale.map(id => `<div class="prow"><label class="prow-tick">
          <input type="checkbox" checked data-tick="${scEsc(id)}"><span class="prow-lbl">${scEsc(id)}</span></label>
          <span class="prow-val"><span class="prow-dash">${scEsc(JSON.stringify(vals[id]))}</span></span></div>`).join('')}</div></details>`;
    }
  }
  h += '</div>';
  box.innerHTML = h;
  box.querySelectorAll('details.sc-grp[open][data-group]').forEach(scFillGroup);
  scBindFolds(box);
}

function scFillGroup(det) {
  const g = SC.groups[+det.dataset.group];
  if (!g) return;
  const vals = scValues(SC.sel);
  const body = det.querySelector('.sc-grp-body');
  let h = '';
  const allOn = g.ids.every(id => id in vals);
  h += `<div class="sc-sub"><span class="sc-sub-lbl">${g.subs.length > 1 ? 'ALL OF ' + scEsc(g.top.toUpperCase()) : ''}</span>
    <button class="flag sc-tickall" type="button" data-tickall="${+det.dataset.group}" data-sub="-1">${allOn ? 'UNTICK ALL' : 'TICK ALL'}</button></div>`;
  g.subs.forEach((sub, si) => {
    const rows = sub.ids.filter(id => !SC.tickedOnly || id in vals);
    if (!rows.length) return;
    if (g.subs.length > 1) {
      const subOn = sub.ids.every(id => id in vals);
      h += `<div class="sc-sub"><span class="sc-sub-lbl">${scEsc((sub.sub || g.top).toUpperCase())}</span>
        <button class="flag sc-tickall" type="button" data-tickall="${+det.dataset.group}" data-sub="${si}">${subOn ? 'UNTICK ALL' : 'TICK ALL'}</button></div>`;
    }
    h += rows.map(id => scRow(id, vals)).join('');
  });
  body.innerHTML = h;
}

function scRow(id, vals) {
  const e = SC.byId[id];
  const on = id in vals;
  return `<div class="prow${on ? '' : ' off'}"><label class="prow-tick" title="${scEsc(id)}">
      <input type="checkbox" ${on ? 'checked' : ''} data-tick="${scEsc(id)}"><span class="prow-lbl">${scEsc(e.label)}</span></label>
    <span class="prow-val">${on ? scField(e, vals[id]) : '<span class="prow-dash">—</span>'}</span></div>`;
}

function scField(e, v) {
  const need = v === null || v === undefined;
  const ph = e.readable ? 'not captured' : 'type a value';
  const cls = 'sc-in' + (need ? ' need' : '');
  const id = scEsc(e.id);
  const t = e.type;
  if (t === 'enum' || t === 'bool') {
    const ch = t === 'bool' ? [true, false] : (Array.isArray(e.choices) ? e.choices.slice() : []);
    if (!need && !ch.some(c => c === v)) ch.push(v);
    const lab = c => t === 'bool' ? (c ? 'ON' : 'OFF') : String(c);
    return `<select class="sc-sel${need ? ' sc-in need' : ''}" data-val="${id}">`
      + (need ? `<option value="" selected>${ph}</option>` : '')
      + ch.map((c, i) => `<option value="${i}"${!need && c === v ? ' selected' : ''}>${scEsc(lab(c))}</option>`).join('')
      + '</select>';
  }
  if (t === 'cc' || t === 'int' || t === 'float') {
    const r = Array.isArray(e.range) ? e.range : null;
    const mm = r ? ` min="${r[0]}" max="${r[1]}"` : '';
    const step = t === 'float' ? 'any' : '1';
    return `<input class="${cls}" type="number" inputmode="decimal" step="${step}"${mm} data-val="${id}"
      value="${need ? '' : scEsc(v)}" placeholder="${ph}" title="${r ? r[0] + ' to ' + r[1] : ''}">`;
  }
  const txt = need ? '' : (t === 'dict' ? JSON.stringify(v) : String(v));
  return `<input class="${cls}" type="text" data-val="${id}" value="${scEsc(txt)}" placeholder="${ph}"
    title="${t === 'dict' ? 'JSON, as captured' : ''}">`;
}

/* Turn what the field holds into the setting's value. -> [ok, value] */
function scParse(e, el) {
  const raw = el.value;
  if (raw === '') return [true, null];
  const t = e.type;
  if (t === 'enum' || t === 'bool') {
    const ch = t === 'bool' ? [true, false] : (Array.isArray(e.choices) ? e.choices.slice() : []);
    const cur = scValues(SC.sel)[e.id];
    if (cur !== null && cur !== undefined && !ch.some(c => c === cur)) ch.push(cur);
    const i = parseInt(raw, 10);
    return (i >= 0 && i < ch.length) ? [true, ch[i]] : [false, null];
  }
  if (t === 'cc' || t === 'int' || t === 'float') {
    let v = t === 'float' ? parseFloat(raw) : parseInt(raw, 10);
    if (!isFinite(v)) return [false, null];
    if (Array.isArray(e.range)) v = Math.max(e.range[0], Math.min(e.range[1], v));
    return [true, v];
  }
  if (t === 'dict') {
    try { const v = JSON.parse(raw); return (v && typeof v === 'object') ? [true, v] : [false, null]; }
    catch (x) { return [false, null]; }
  }
  return [true, raw];
}

/* ── actions ────────────────────────────────────────────────────────────── */
function scSay(msg, cls) {
  SC.msg = msg || ''; SC.msgCls = cls || '';
  const el = document.getElementById('sc-msg');
  if (el) { el.textContent = SC.msg; el.className = 'sc-msg ' + SC.msgCls; }
}

/* Apply a SCENE_SET reply's slot locally, then redraw. */
function scApplySet(n, d) {
  if (d && d.ok && d.scene) {
    const sc = d.scene;
    if (!Object.keys(sc.values || {}).length && !sc.name && (sc.timing || 'NEXT_BAR') === 'NEXT_BAR') delete SC.slots[String(n)];
    else SC.slots[String(n)] = sc;
  }
  scRenderSlots();
  scRenderEditor(true);
}

async function scSetSlot(body, okMsg) {
  const n = SC.sel;
  const d = await scApi('POST', '/api/scenes/' + n, body);
  if (!d.ok) { scSay('Not saved: ' + (d.error || 'error'), 'bad'); return d; }
  scApplySet(n, d);
  scSay(okMsg || 'Saved.', 'ok');
  return d;
}

function scSetName(v) { scSetSlot({name: v}, 'Name saved.'); }
function scSetTiming(v) { scSetSlot({timing: v}, 'Timing saved: ' + SC_TLABEL[v] + '.'); }
function scToggleTickedOnly() {
  SC.tickedOnly = !SC.tickedOnly;
  scLsSet('orc.scenes.tickedOnly', SC.tickedOnly ? '1' : '0');
  scRenderEditor(true);
}

async function scTick(ids, on) {
  if (!ids.length) return;
  const vals = scValues(SC.sel);
  if (on) {
    const add = {};
    ids.filter(id => !(id in vals)).forEach(id => { add[id] = null; });
    if (!Object.keys(add).length) return;
    await scSetSlot({values: add, merge: true},
      Object.keys(add).length + ' ticked. CAPTURE stores their current values.');
  } else {
    const rm = ids.filter(id => id in vals);
    if (!rm.length) return;
    await scSetSlot({remove: rm}, rm.length + ' unticked.');
  }
}

async function scCapture() {
  const n = SC.sel;
  const before = Object.assign({}, scValues(n));
  const ids = Object.keys(before).filter(id => SC.byId[id] && SC.byId[id].readable);
  if (!ids.length) { scSay('Nothing the ORC can read back is ticked: type those values instead.', 'bad'); return; }
  scSay('Capturing ' + ids.length + ' settings…');
  const d = await scApi('POST', '/api/scenes/' + n + '/capture', {ids, merge: true});
  if (!d.ok) { scSay('Capture failed: ' + (d.error || 'error'), 'bad'); return; }
  // a value typed earlier survives a read that came back empty
  const unread = Object.keys(d.unread || {});
  const keep = {};
  unread.forEach(id => { if (before[id] !== null && before[id] !== undefined) keep[id] = before[id]; });
  if (Object.keys(keep).length) await scApi('POST', '/api/scenes/' + n, {values: keep, merge: true});
  await scPoll();
  let m = 'Captured ' + (ids.length - unread.length) + ' of ' + ids.length + '.';
  if (unread.length) m += ' Could not read: ' + unread.slice(0, 4).map(scLabel).join(', ') + (unread.length > 4 ? ' …' : '')
    + (Object.keys(keep).length ? ' (kept the values they had).' : '.');
  scSay(m, unread.length ? '' : 'ok');
  scRenderEditor(true);
}

async function scRecall() {
  const n = SC.sel;
  const body = SC.override ? {timing: SC.override} : {};
  const d = await scApi('POST', '/api/scenes/' + n + '/recall', body);
  if (!d.ok) { scSay('Recall failed: ' + (d.error || 'error'), 'bad'); return; }
  const src = d.bar_source === 'fire' ? ' (bar from the Fire)' : d.bar_source === 'orc' ? ' (ORC clock)' : '';
  scSay(d.when === 'now' ? 'Scene ' + n + ' recalled.'
                         : 'Scene ' + n + ' lands ' + (SC_TLABEL[d.timing] || d.timing) + src + '.', 'ok');
  setTimeout(scPoll, 300);
}

function scAskClear() { SC.confirmClear = true; scRenderEditor(true); }

async function scClear() {
  const n = SC.sel;
  SC.confirmClear = false;
  const d = await scApi('POST', '/api/scenes/' + n + '/clear', {});
  if (!d.ok) { scSay('Not cleared: ' + (d.error || 'error'), 'bad'); scRenderEditor(true); return; }
  delete SC.slots[String(n)];
  SC.msg = 'Scene ' + n + ' cleared.'; SC.msgCls = 'ok';
  scRenderSlots();
  scRenderEditor(true);
}

/* One delegated listener for ticks, tick-alls and value edits. */
function scBindEditor() {
  const box = document.getElementById('sc-editor');
  if (!box) return;
  box.addEventListener('change', ev => {
    const el = ev.target;
    if (el.dataset.tick !== undefined) { scTick([el.dataset.tick], el.checked); return; }
    if (el.dataset.val !== undefined) {
      const e = SC.byId[el.dataset.val];
      if (!e) return;
      const [ok, v] = scParse(e, el);
      if (!ok) { scSay(e.label + ': not a valid value.', 'bad'); return; }
      const vals = {}; vals[e.id] = v;
      el.blur();
      scSetSlot({values: vals, merge: true}, e.label + ' saved.');
    }
  });
  box.addEventListener('click', ev => {
    const b = ev.target.closest('[data-tickall]');
    if (!b) return;
    const g = SC.groups[+b.dataset.tickall];
    if (!g) return;
    const si = +b.dataset.sub;
    const ids = si < 0 ? g.ids : g.subs[si].ids;
    const vals = scValues(SC.sel);
    scTick(ids, !ids.every(id => id in vals));
  });
  box.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && ev.target.dataset && ev.target.dataset.val !== undefined) ev.target.blur();
  });
}

/* ── start ──────────────────────────────────────────────────────────────── */
(function scInit() {
  if (!document.getElementById('scenes-card')) return;
  const n = parseInt(scLsGet('orc.scenes.sel', '1'), 10);
  SC.sel = (n >= 1 && n <= SC_N) ? n : 1;
  SC.tickedOnly = scLsGet('orc.scenes.tickedOnly', '0') === '1';
  document.querySelectorAll('details[data-fold]').forEach(d => {
    const dflt = false;
    d.open = scFoldOpen(d.dataset.fold, dflt);
  });
  scBindFolds(document);
  scBindEditor();
  scRenderSlots();
  scRenderEditor(true);
  scLoadSettings();
  scPoll();
})();
