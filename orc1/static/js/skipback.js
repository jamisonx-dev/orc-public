/* skipback.js — multitrack SKIPBACK recorder page (Phase 2)
 *
 * Talks to the recorder's web control surface:
 *   GET  /api/skipback/tracks            — config merged with live status + RAM
 *   POST /api/skipback/tracks            — {action:"save",tracks:[...]} | {action:"reset"}
 *   POST /api/skipback/tracks/<id>/grab  — per-track grab
 *   GET/POST /api/skipback/joins         — GRAB ALL stereo joins [[L, R], ...]
 *
 * The recorder (skipback-rec) is the authority; this page edits a LOCAL model
 * and applies it with SAVE (whole-array). While there are unsaved edits the 4s
 * poll refreshes only the live status + RAM meter, never the fields you're
 * editing. Self-rescheduling setTimeout (no stacking setInterval).
 */
(function () {
  const $ = (id) => document.getElementById(id);

  let MODEL  = [];      // editable track config (array)
  let STATUS = {};      // id -> live status entry (recording/resolved/fill/...)
  let OPTS   = { sources: [], raw_sources: [], zoom_present: false,
                 ram_budget_mb: 900, ram_free_mb: null, ram_used_mb: 0,
                 ram_free_min: 400 };
  let dirty  = false;
  let loaded = false;

  const RATE = 48000;
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function ringMb(t) {
    return (t.length_secs * RATE * (t.stereo ? 2 : 1) * 2) / 1048576;
  }
  function armedSumMb() {                         // RAM rings only
    return MODEL.filter(t => t.armed && t.store !== 'card').reduce((a, t) => a + ringMb(t), 0);
  }
  function cardSumMb() {
    return MODEL.filter(t => t.armed && t.store === 'card').reduce((a, t) => a + ringMb(t), 0);
  }
  const clone = (x) => JSON.parse(JSON.stringify(x));
  function editingField() {
    // Only text/number inputs (and selects mid-pick) must not be rebuilt from
    // under the user; buttons/toggles are safe to re-render even when focused.
    const c = $('sb-tracks'), ae = document.activeElement;
    if (!c || !ae || !c.contains(ae)) return false;
    return ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT';
  }

  /* ── poll ────────────────────────────────────────────────────────────── */
  async function poll() {
    try {
      const d = await fetch('/api/skipback/tracks').then(r => r.json());
      OPTS = {
        sources:      d.sources || [],
        raw_sources:  d.raw_sources || [],
        zoom_present: !!d.zoom_present,
        ram_budget_mb: d.ram_budget_mb || 900,
        ram_free_mb:  d.ram_free_mb,
        ram_used_mb:  d.ram_used_mb,
        ram_free_min: d.ram_free_min || 400,
        card_mounted: !!d.card_mounted,
        card_cap_mb:  d.card_cap_mb || 0,
      };
      STATUS = {};
      (d.tracks || []).forEach(t => { STATUS[t.id] = t; });
      // Adopt the server config only when the user has no pending edits and
      // isn't typing into a field — otherwise just refresh the live bits.
      if ((!dirty && !editingField()) || !loaded) {
        MODEL = (d.tracks || []).map(t => ({
          id: t.id, name: t.name, stereo: !!t.stereo,
          length_secs: t.length_secs, armed: !!t.armed,
          store: t.store === 'card' ? 'card' : 'ram',
          persist: t.persist !== false,
          source: t.source && t.source.mode
            ? { mode: t.source.mode, left: t.source.left || '', right: t.source.right || '' }
            : { mode: 'auto', left: '', right: '' },
        }));
        loaded = true;
        render();
      } else {
        updateStatusSpans();
      }
      ramMeter();
      quickOpts();
    } catch (e) {
      $('sb-msg').innerHTML = '<span style="color:var(--danger)">error loading skipback state</span>';
    }
    setTimeout(poll, 4000);
  }

  /* ── RAM meter ───────────────────────────────────────────────────────── */
  function ramMeter() {
    const used = armedSumMb();                 // from the edited model
    const budget = OPTS.ram_budget_mb;
    const free = OPTS.ram_free_mb;
    const over = used > budget ||
      (free != null && (free - used) < OPTS.ram_free_min);   // rough projection
    const bar = $('sb-ram-bar');
    if (bar) {
      bar.style.width = Math.min(100, (used / budget) * 100) + '%';
      bar.style.background = over ? 'var(--danger)' : 'var(--accent)';
    }
    const txt = $('sb-ram-txt');
    if (txt) {
      txt.innerHTML = `RAM ${Math.round(used)} / ${budget} MB`
        + (free != null ? ` · ${Math.round(free)} MB free` : '');
      txt.style.color = over ? 'var(--danger)' : 'var(--muted)';
    }
    const cu = cardSumMb(), cap = OPTS.card_cap_mb;
    const cOver = cu > 0 && (!OPTS.card_mounted || cu > cap);
    const cb = $('sb-card-bar');
    if (cb) {
      cb.style.width = cap ? Math.min(100, (cu / cap) * 100) + '%' : '0';
      cb.style.background = cOver ? 'var(--danger)' : 'var(--accent)';
    }
    const ct = $('sb-card-txt');
    if (ct) {
      ct.textContent = OPTS.card_mounted
        ? `${Math.round(cu)} / ${cap} MB`
        : 'card partition NOT mounted';
      ct.style.color = (cOver || !OPTS.card_mounted) ? 'var(--danger)' : 'var(--muted)';
    }
  }

  /* ── render all track rows ───────────────────────────────────────────── */
  function render() {
    const c = $('sb-tracks');
    if (!c) return;
    const ae = document.activeElement;                 // focus guard: only
    if (ae && c.contains(ae) && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return;
    if (!MODEL.length) {
      c.innerHTML = '<div class="card"><span style="font-size:11px;color:var(--muted)">no tracks — add one, or RESET TO DEFAULT</span></div>';
      return;
    }
    c.innerHTML = MODEL.map((t, i) => rowHtml(t, i)).join('');
    renderAll();
  }

  /* ── QUICK ADD: one pick = a named, wired track (stereo pair or mono) ── */
  // "system:capture_9" -> "In 9"; anything else keeps its port name.
  const portLabel = (p) => {
    const m = /^system:capture_(\d+)$/.exec(p);
    return m ? 'In ' + m[1] : p;
  };
  function quickChoices() {
    const stereo = [], seen = new Set();
    OPTS.sources.forEach(s => {
      if (!s.right) return;
      stereo.push({ label: s.label, left: s.left, right: s.right });
      seen.add(s.left + '|' + s.right);
    });
    // every odd/even pair of the card's inputs (1/2, 3/4 ... 13/14), so a
    // pair the named list doesn't cover is still one pick away
    const caps = OPTS.raw_sources.filter(p => /^system:capture_\d+$/.test(p));
    const num = (p) => parseInt(p.split('_').pop(), 10);
    caps.forEach(l => {
      const n = num(l);
      if (n % 2 !== 1) return;
      const r = 'system:capture_' + (n + 1);
      if (!caps.includes(r) || seen.has(l + '|' + r)) return;
      stereo.push({ label: `In ${n}/${n + 1}`, left: l, right: r });
    });
    const mono = OPTS.raw_sources.map(p => ({ label: portLabel(p), left: p }));
    return { stereo, mono };
  }
  let QUICK = { stereo: [], mono: [] };
  function quickOpts() {
    const el = $('sb-quick');
    if (!el || document.activeElement === el) return;
    QUICK = quickChoices();
    el.innerHTML = '<option value="">+ QUICK ADD…</option>'
      + '<optgroup label="STEREO PAIR (one stereo track)">'
      + QUICK.stereo.map((c, k) => `<option value="S${k}">${esc(c.label)}</option>`).join('')
      + '</optgroup><optgroup label="MONO CHANNEL">'
      + QUICK.mono.map((c, k) => `<option value="M${k}">${esc(c.label)}</option>`).join('')
      + '</optgroup>';
  }
  window.sbQuick = (v) => {
    const el = $('sb-quick');
    if (el) el.value = '';
    if (!v) return;
    const c = (v[0] === 'S' ? QUICK.stereo : QUICK.mono)[parseInt(v.slice(1), 10)];
    if (!c) return;
    const stereo = v[0] === 'S';
    MODEL.push({ id: null, name: c.label, stereo, length_secs: 900,
                 armed: true, store: 'ram', persist: true,
                 source: { mode: 'manual', left: c.left, right: stereo ? c.right : '' } });
    setDirty(); render(); ramMeter();
    msg(`added "${esc(c.label)}" (${stereo ? 'stereo' : 'mono'}) — press SAVE to start recording`, true);
  };

  /* ── GRAB ALL stereo JOINS (saved on the Pi, the FCB grab uses them too) ── */
  let JOINS = [];
  async function loadJoins() {
    try {
      const r = await fetch('/api/skipback/joins').then(x => x.json());
      if (r.ok) { JOINS = r.joins; renderJoins(); }
    } catch (e) { /* the card just shows none */ }
  }
  async function saveJoins(next) {
    const el = $('sb-join-msg');
    try {
      const r = await fetch('/api/skipback/joins', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ joins: next }),
      }).then(x => x.json());
      if (!r.ok) { if (el) el.innerHTML = `<span style="color:var(--danger)">${esc(r.error || 'refused')}</span>`; return; }
      JOINS = r.joins;
      if (el) el.textContent = '';
      renderJoins();
    } catch (e) {
      if (el) el.innerHTML = '<span style="color:var(--danger)">save failed</span>';
    }
  }
  const trackName = (id) => {
    const t = MODEL.find(x => x.id === id);
    return t ? `t${id} ${t.name}` : `t${id} (gone)`;
  };
  function renderJoins() {
    const el = $('sb-joins');
    if (!el) return;
    const chips = JOINS.map((j, k) => {
      const bad = j.some(id => { const t = MODEL.find(x => x.id === id); return !t || t.stereo; });
      return `<button class="flag" onclick="sbUnjoin(${k})" title="remove this join"
        style="${bad ? 'color:var(--danger)' : ''}">L ${esc(trackName(j[0]))} + R ${esc(trackName(j[1]))} ✕</button>`;
    }).join('');
    const used = new Set(JOINS.flat());
    const mono = MODEL.filter(t => t.id != null && !t.stereo && !used.has(t.id));
    const opts = mono.map(t => `<option value="${t.id}">t${t.id} ${esc(t.name)}</option>`).join('');
    el.innerHTML = (chips || '<span style="font-size:11px;color:var(--muted)">none — every track gets its own file</span>')
      + (mono.length >= 2 ? `<div style="display:flex;gap:4px;margin-top:4px;flex-wrap:wrap;align-items:center">
          <span style="font-size:10px;color:var(--muted)">L</span><select id="sb-join-l">${opts}</select>
          <span style="font-size:10px;color:var(--muted)">R</span><select id="sb-join-r">${opts}</select>
          <button class="flag" onclick="sbJoin()">+ JOIN</button></div>` : '');
    const r = $('sb-join-r');
    if (r && r.options.length > 1) r.selectedIndex = 1;
  }
  window.sbJoin = () => {
    const l = parseInt(($('sb-join-l') || {}).value, 10);
    const r = parseInt(($('sb-join-r') || {}).value, 10);
    const el = $('sb-join-msg');
    if (isNaN(l) || isNaN(r) || l === r) { if (el) el.textContent = 'pick two different tracks'; return; }
    saveJoins(JOINS.concat([[l, r]]));
  };
  window.sbUnjoin = (k) => saveJoins(JOINS.filter((_, i) => i !== k));

  /* ── GRAB ALL card: which armed tracks are in ────────────────────────── */
  const ALL_OUT = new Set();                          // ids left OUT (default: all in)
  function renderAll() {
    const el = $('sb-all-tracks');
    if (!el) return;
    const armed = MODEL.filter(t => t.id != null && t.armed);
    el.innerHTML = armed.length ? armed.map(t => {
      const on = !ALL_OUT.has(t.id);
      return `<button class="flag ${on ? 'on' : ''}" onclick="sbAllToggle(${t.id})"
        style="${on ? 'background:var(--accent);color:#0a0b0d' : ''}">t${t.id} ${esc(t.name)}</button>`;
    }).join('') : '<span style="font-size:11px;color:var(--muted)">no armed tracks</span>';
    renderJoins();
  }
  window.sbAllToggle = (id) => {
    if (ALL_OUT.has(id)) ALL_OUT.delete(id); else ALL_OUT.add(id);
    renderAll();
  };
  window.sbGrabAll = async (secs) => {
    const el = $('sb-all-msg');
    const ids = MODEL.filter(t => t.id != null && t.armed && !ALL_OUT.has(t.id)).map(t => t.id);
    if (!ids.length) { if (el) el.textContent = 'no tracks chosen'; return; }
    if (el) el.textContent = `grabbing ${secs ? 'last ' + secs + 's' : 'everything'} from ${ids.length} track(s)…`;
    try {
      const r = await fetch('/api/skipback/grab_all', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seconds: secs, tracks: ids }),
      }).then(x => x.json());
      if (!el) return;
      el.innerHTML = r.ok
        ? `✓ saved folder <b>${esc(r.folder)}</b> (${r.tracks.length} tracks in ${r.files} files, ${Math.round(r.seconds)}s)` +
          (r.aligned ? '' : `<br><span style="color:var(--danger)">⚠ ${esc(r.warning)}</span>`) +
          ((r.joins_skipped || []).length ? `<br><span style="color:var(--danger)">⚠ not joined: ${esc(r.joins_skipped.join('; '))}</span>` : '') +
          ((r.not_recording || []).length ? `<br><span style="color:var(--danger)">⚠ left out (not recording): ${esc(r.not_recording.map(t => 't' + t).join(', '))}</span>` : '')
        : r.pending ? `⏳ ${esc(r.error)}`
        : `<span style="color:var(--danger)">grab failed: ${esc(r.error || '?')}</span>`;
    } catch (e) {
      if (el) el.innerHTML = '<span style="color:var(--danger)">grab failed</span>';
    }
  };

  function rowHtml(t, i) {
    const st = t.id != null ? STATUS[t.id] : null;
    const isAuto = (t.source.mode || 'auto') !== 'manual';
    const idBadge = t.id != null ? `t${t.id}` : 'NEW';

    // status line
    let statusInner = statusHtml(t, st);

    // named source dropdown (manual only)
    const namedOpts = ['<option value="">— named source —</option>']
      .concat(OPTS.sources.map(s => {
        const v = esc(s.left + '|' + (s.right || ''));
        const sel = (s.left === t.source.left &&
                     (s.right || '') === (t.source.right || '')) ? ' selected' : '';
        return `<option value="${v}"${sel}>${esc(s.label)}</option>`;
      })).join('');
    const rawOpt = (cur) => ['<option value="">—</option>']
      .concat(OPTS.raw_sources.map(p =>
        `<option value="${esc(p)}"${p === cur ? ' selected' : ''}>${esc(p)}</option>`)).join('');

    const manualBlock = isAuto ? '' : `
      <div style="margin-top:6px">
        <select onchange="sbNamed(${i}, this.value)" style="width:100%;margin-bottom:4px">${namedOpts}</select>
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <select onchange="sbRawL(${i}, this.value)" style="flex:1;min-width:110px">${rawOpt(t.source.left)}</select>
          ${t.stereo ? `<select onchange="sbRawR(${i}, this.value)" style="flex:1;min-width:110px">${rawOpt(t.source.right)}</select>` : ''}
        </div>
      </div>`;

    const grabRow = (t.id != null && t.armed) ? `
      <div class="flags" style="margin-top:6px">
        ${[[5,'5s'],[30,'30s'],[60,'60s'],[300,'5m'],[600,'10m']].map(([s,l]) =>
          `<button class="flag" onclick="sbGrab(${t.id},${s})">${l}</button>`).join('')}
      </div>
      <div id="sb-grab-${t.id}" style="margin-top:4px;font-size:11px;color:var(--accent);min-height:14px"></div>` : '';

    return `
    <div class="card">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-family:var(--fh,var(--fs));font-size:10px;color:var(--label);min-width:26px">${idBadge}</span>
        <input value="${esc(t.name)}" oninput="sbName(${i}, this.value)"
               style="flex:1;min-width:60px" placeholder="name">
        <button class="flag ${t.armed ? 'on' : ''}" onclick="sbArm(${i})"
                style="${t.armed ? 'background:var(--ok,#4ade80);color:#0a0b0d' : ''}">${t.armed ? 'ARMED' : 'DISARMED'}</button>
        <button class="flag" onclick="sbRemove(${i})" title="remove track">✕</button>
      </div>

      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px">
        <button class="flag ${isAuto ? 'on' : ''}" onclick="sbMode(${i},'auto')"
                style="${isAuto ? 'background:var(--accent);color:#0a0b0d' : ''}">AUTO</button>
        <button class="flag ${!isAuto ? 'on' : ''}" onclick="sbMode(${i},'manual')"
                style="${!isAuto ? 'background:var(--accent);color:#0a0b0d' : ''}">MANUAL</button>
        <span style="width:8px"></span>
        <button class="flag" onclick="sbStereo(${i})">${t.stereo ? 'STEREO' : 'MONO'}</button>
        <button class="flag ${t.store === 'card' ? 'on' : ''}" onclick="sbStore(${i})"
                title="where this track's ring lives: RAM (cleared on reboot) or the CARD partition"
                style="${t.store === 'card' ? 'background:var(--accent);color:#0a0b0d' : ''}">${t.store === 'card' ? 'CARD' : 'RAM'}</button>
        ${t.store === 'card' ? `<button class="flag" onclick="sbPersist(${i})"
                title="PERSIST: history survives a reboot. FRESH: starts empty each boot"
                >${t.persist ? 'PERSIST' : 'FRESH'}</button>` : ''}
        <label style="font-size:11px;color:var(--muted)">length
          <input type="number" min="5" max="7200" value="${t.length_secs}"
                 onchange="sbLen(${i}, this.value)"
                 style="width:64px">s</label>
        <span id="sb-mb-${i}" style="font-size:10px;color:var(--muted)">${ringMb(t).toFixed(0)} MB</span>
      </div>

      ${manualBlock}

      <div id="sb-st-${i}" style="margin-top:6px;font-size:11px">${statusInner}</div>
      ${grabRow}
    </div>`;
  }

  function statusHtml(t, st) {
    if (t.id == null) return '<span style="color:var(--muted)">unsaved — press SAVE to start recording</span>';
    if (!t.armed)     return '<span style="color:var(--muted)">disarmed (ring freed)</span>';
    if (!st)          return '<span style="color:var(--muted)">…</span>';
    if (st.card_blocked) return `<span style="color:var(--danger)">● CARD — not armed: ${esc(st.card_blocked)}</span>`;
    if (st.ram_blocked) return '<span style="color:var(--danger)">● RAM-BLOCKED — not armed (over the RAM budget; try CARD)</span>';
    const rec = st.recording && st.connected;
    const dot = rec ? '<span style="color:var(--ok,#4ade80)">● RECORDING</span>'
                    : '<span style="color:var(--danger)">● not recording</span>';
    const src = (st.resolved && st.resolved.length) ? st.resolved.join(' + ') : '—';
    const fill = st.fill_secs ? ` · ${Math.round(st.fill_secs)}s buffered` : '';
    const res = st.resumed_from === 'checkpoint' ? ' <span style="color:var(--muted)">· history resumed after reboot</span>' : '';
    const gaps = st.gaps ? ` <span style="color:var(--danger)">· ⚠ ${st.gaps} dropped period(s)</span>` : '';
    return `${dot} <span style="color:var(--muted)">${esc(src)}${fill}</span>${res}${gaps}`;
  }

  function updateStatusSpans() {
    MODEL.forEach((t, i) => {
      const el = $('sb-st-' + i);
      if (el) el.innerHTML = statusHtml(t, t.id != null ? STATUS[t.id] : null);
    });
  }

  /* ── edit handlers ───────────────────────────────────────────────────── */
  function setDirty() {
    dirty = true;
    const b = $('sb-save-btn');
    if (b) { b.textContent = 'SAVE *'; b.style.background = 'var(--accent)'; b.style.color = '#0a0b0d'; }
  }
  function clearDirty() {
    dirty = false;
    const b = $('sb-save-btn');
    if (b) { b.textContent = 'SAVE'; b.style.background = ''; b.style.color = ''; }
  }

  // text inputs: update model WITHOUT re-render (keep focus)
  window.sbName = (i, v) => { MODEL[i].name = v; setDirty(); };
  window.sbLen = (i, v) => {
    let n = parseInt(v, 10); if (isNaN(n)) n = 5;
    n = Math.max(5, Math.min(7200, n));
    MODEL[i].length_secs = n; setDirty();
    const mb = $('sb-mb-' + i); if (mb) mb.textContent = ringMb(MODEL[i]).toFixed(0) + ' MB';
    ramMeter();
  };
  // structural edits: mutate + re-render
  window.sbStereo = (i) => { MODEL[i].stereo = !MODEL[i].stereo; setDirty(); render(); ramMeter(); };
  window.sbStore = (i) => {
    const t = MODEL[i], to = t.store === 'card' ? 'ram' : 'card';
    if (t.id != null && !confirm(`Move t${t.id} to ${to.toUpperCase()}? This rebuilds its ring, so the audio it holds now is lost.`)) return;
    t.store = to; setDirty(); render(); ramMeter();
  };
  window.sbPersist = (i) => { MODEL[i].persist = !MODEL[i].persist; setDirty(); render(); };
  window.sbArm = (i) => { MODEL[i].armed = !MODEL[i].armed; setDirty(); render(); ramMeter(); };
  window.sbMode = (i, m) => { MODEL[i].source.mode = m; setDirty(); render(); };
  window.sbNamed = (i, v) => {
    if (!v) return;
    const [l, r] = v.split('|');
    MODEL[i].source.left = l || ''; MODEL[i].source.right = r || '';
    setDirty(); render();
  };
  window.sbRawL = (i, v) => { MODEL[i].source.left = v; setDirty(); };
  window.sbRawR = (i, v) => { MODEL[i].source.right = v; setDirty(); };
  window.sbRemove = (i) => { MODEL.splice(i, 1); setDirty(); render(); ramMeter(); };
  window.sbAddTrack = () => {
    // default source: the card's first stereo input pair (the old default,
    // ORC1-Synth, was retired 2026-09-24 and recorded nothing)
    const raw = OPTS.raw_sources || [];
    const src = raw.length >= 2
      ? { mode: 'manual', left: raw[0], right: raw[1] }
      : { mode: 'auto', left: '', right: '' };
    MODEL.push({ id: null, name: '', stereo: true, length_secs: 900,
                 armed: true, store: 'ram', persist: true, source: src });
    setDirty(); render(); ramMeter();
  };

  function msg(html, ok) {
    const el = $('sb-msg');
    if (el) { el.innerHTML = html; el.style.color = ok ? 'var(--ok,#4ade80)' : 'var(--danger)'; }
  }

  window.sbSave = async () => {
    msg('saving…', true);
    try {
      const r = await fetch('/api/skipback/tracks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'save', tracks: MODEL }),
      }).then(x => x.json());
      if (!r.ok) { msg('refused: ' + (r.error || '?'), false); return; }
      MODEL = (r.tracks || []).map(clone);
      clearDirty(); render(); ramMeter();
      msg('saved — recorder applies within ~5s', true);
    } catch (e) { msg('save failed: ' + e, false); }
  };

  window.sbReset = async () => {
    if (!confirm('Reset skipback to its default (one MAIN track, AUTO source, 20 min, RAM)? This replaces the current tracks and their recorded audio.')) return;
    msg('resetting…', true);
    try {
      const r = await fetch('/api/skipback/tracks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'reset' }),
      }).then(x => x.json());
      if (!r.ok) { msg('reset refused: ' + (r.error || '?'), false); return; }
      MODEL = (r.tracks || []).map(clone);
      clearDirty(); render(); ramMeter();
      msg('reset — recorder applies within ~5s', true);
    } catch (e) { msg('reset failed: ' + e, false); }
  };

  window.sbGrab = async (id, secs) => {
    const el = $('sb-grab-' + id);
    if (el) el.textContent = `grabbing last ${secs}s…`;
    try {
      const r = await fetch(`/api/skipback/tracks/${id}/grab`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seconds: secs }),
      }).then(x => x.json());
      if (el) el.innerHTML = r.ok
        ? `✓ saved <b>${esc(r.file)}</b> to ~/recordings`
        : r.pending ? `⏳ ${esc(r.error)}`
        : `<span style="color:var(--danger)">grab failed: ${esc(r.error || '?')}</span>`;
    } catch (e) {
      if (el) el.innerHTML = '<span style="color:var(--danger)">grab failed</span>';
    }
  };

  poll();
  loadJoins();
})();
