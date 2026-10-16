// RC-505 looper control page.
const RC505 = (() => {
  const $ = id => document.getElementById(id);
  let allTargets = [];   // [{name,target,continuous,cc|null}]

  async function api(path, opts) {
    const r = await fetch(path, opts);
    return r.json();
  }
  function flash(msg, el = 'rc-flash') { const e = $(el); if (e) { e.textContent = msg; setTimeout(() => { if (e.textContent === msg) e.textContent = ''; }, 1500); } }

  function cmd(action, extra) {
    return api('/api/rc505/cmd', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ action }, extra || {}))
    });
  }

  // ---- live control ----
  function action(name, value) { cmd('RC505_ACTION', value === undefined ? { name } : { name, value }); }
  function transport(start) { cmd('RC505_TRANSPORT', { start: !!start }); flash(start ? 'start' : 'stop'); }
  function gotoMem() {
    const n = Math.max(1, Math.min(99, parseInt($('rc-mem').value || '1', 10)));
    cmd('RC505_MEMORY', { program: n - 1 }); flash('memory ' + n);
  }

  function renderControls(mapped) {
    const box = $('rc-controls'); box.innerHTML = '';
    if (!mapped.length) { box.innerHTML = '<span class="rc-note">No actions mapped. Add some below.</span>'; return; }
    mapped.forEach(a => {
      const d = document.createElement('div'); d.className = 'rc-ctl';
      if (a.continuous) {
        d.innerHTML = `<span class="cc">CC${a.cc}</span><span class="tgt">${a.target}</span>
          <input type="range" min="0" max="127" value="0">`;
        const s = d.querySelector('input');
        let last = 0;
        s.addEventListener('input', () => {
          const now = performance.now();
          if (now - last > 50) { last = now; action(a.name, +s.value); }
        });
        s.addEventListener('change', () => action(a.name, +s.value));
      } else {
        d.innerHTML = `<span class="cc">CC${a.cc}</span><span class="tgt">${a.target}</span>
          <button class="btn trig">TRIGGER</button>`;
        d.querySelector('button').addEventListener('click', () => { action(a.name); flash(a.target); });
      }
      box.appendChild(d);
    });
  }

  // ---- mapping editor ----
  function countMapped() {
    const inputs = document.querySelectorAll('#rc-maprows input.cc');
    let n = 0; const seen = {}; let dup = false;
    inputs.forEach(i => { const v = i.value.trim(); if (v) { n++; if (seen[v]) dup = true; seen[v] = 1; } });
    const el = $('rc-mapcount'); el.textContent = n + (dup ? ' — DUPLICATE CC' : '');
    el.className = (n > 16 || dup) ? 'warn' : '';
    return { n, dup };
  }

  function renderMap(list) {
    const body = $('rc-maprows'); body.innerHTML = '';
    list.forEach(a => {
      const tr = document.createElement('tr');
      if (a.cc != null) tr.className = 'mapped';
      tr.innerHTML = `<td>${a.target}</td>
        <td><input class="cc" data-target="${a.target}" value="${a.cc != null ? a.cc : ''}"
             placeholder="—" title="CC 1-31 or 64-95"></td>`;
      body.appendChild(tr);
    });
    body.querySelectorAll('input.cc').forEach(i => i.addEventListener('input', countMapped));
    countMapped();
  }

  async function saveMap() {
    const { n, dup } = countMapped();
    if (dup) { flash('fix duplicate CC first', 'rc-savemsg'); return; }
    if (n > 16) { flash('max 16 mapped', 'rc-savemsg'); return; }
    const map = {};
    document.querySelectorAll('#rc-maprows input.cc').forEach(i => {
      const v = i.value.trim();
      if (v) map[v] = i.dataset.target;   // {cc: target}
    });
    const res = await api('/api/rc505/map', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map })
    });
    $('rc-savemsg').textContent = res.ok ? `saved ${res.assigns} assign(s). ${res.note || ''}` : 'save failed';
    refresh();
  }

  async function refresh() {
    let s;
    try { s = await api('/api/rc505'); } catch (e) { $('rc-conn').textContent = '○ error'; return; }
    const c = $('rc-conn');
    if (s.present) { c.textContent = '● connected'; c.className = 'rc-conn live'; }
    else { c.textContent = '○ RC not detected (storage mode or unplugged)'; c.className = 'rc-conn dead'; }
    const mapped = s.mapped || [], unmapped = s.unmapped || [];
    allTargets = mapped.concat(unmapped);
    renderControls(mapped);
    renderMap(allTargets);
  }

  document.addEventListener('DOMContentLoaded', refresh);
  return { refresh, action, transport, gotoMem, saveMap };
})();
