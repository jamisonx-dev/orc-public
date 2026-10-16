/* ORC-1 · shared.js — Phase 1 web reorg
   cmd(), poll loop with subscriber registry, focusGuard helper.
   Loaded by base.html before per-page scripts. */

'use strict';

/* ─── CMD ─────────────────────────────────────────────────── */
async function cmd(action, extra) {
  try {
    await fetch('/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ action }, extra || {}))
    });
  } catch (e) {}
}

/* ─── POLL LOOP ───────────────────────────────────────────── */
const ORC = (() => {
  const _subs = [];
  let _last = null;
  let _timer = null;
  const REFRESH_MS = 1500;

  const dotEl  = () => document.getElementById('dot');
  const ageEl  = () => document.getElementById('age');

  function _updateStatus(ts) {
    const dot = dotEl();
    const age = ageEl();
    if (!dot) return;
    const now = Date.now() / 1000;
    const diff = now - ts;
    dot.className = 'dot ' + (diff < 4 ? 'live' : diff < 12 ? 'stale' : 'dead');
    if (age) age.textContent = diff < 4 ? Math.round(diff) + 's ago' :
                               diff < 60 ? Math.round(diff) + 's ago' :
                               Math.round(diff / 60) + 'm ago';
  }

  async function _tick() {
    try {
      const r = await fetch('/state');
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      _last = d;
      if (d._ts) _updateStatus(d._ts);
      for (const fn of _subs) { try { fn(d); } catch (e) { console.error('[orc poll sub]', e); } }
    } catch (e) {
      const dot = dotEl();
      if (dot) dot.className = 'dot dead';
    }
  }

  function start() {
    _tick();
    _timer = setInterval(_tick, REFRESH_MS);
  }

  function subscribe(fn) {
    _subs.push(fn);
    if (_last) { try { fn(_last); } catch (e) {} }
  }

  function unsubscribe(fn) {
    const i = _subs.indexOf(fn);
    if (i !== -1) _subs.splice(i, 1);
  }

  function last() { return _last; }

  // A module's MIDI channel from the brain's LIVE /matrix bindings
  // (state.bindings = {module: ch}). undefined = no bindings in this state yet
  // (keep whatever the page had); 0 = the module is not bound (the brain drops
  // channel 0, so a control on an unbound module does nothing rather than
  // driving whatever else sits on its old channel).
  // 2026-09-24: the voice pages used the static cfg CH_* keys, which went stale
  // once modules moved on /matrix -- /synth and /chip P2 were driving EACH
  // OTHER's synth (poly bound to 6, chip_p2 to 8; config still said 8 / 6).
  function boundCh(key, state) {
    const b = (state || _last || {}).bindings;
    if (!b || typeof b !== 'object') return undefined;
    const ch = +b[key];
    return (ch >= 1 && ch <= 16) ? ch : 0;
  }

  return { start, subscribe, unsubscribe, last, boundCh };
})();

/* ─── FOCUS GUARD ─────────────────────────────────────────── */
/* Wraps a render function so it won't rebuild innerHTML of a container
   if any of its inputs/selects/textareas currently have focus.
   Per the seq dropdown/focus-loss fix — mandatory for all editable cards. */
function focusGuard(containerOrId, renderFn) {
  return function guardedRender(data) {
    const el = typeof containerOrId === 'string'
      ? document.getElementById(containerOrId)
      : containerOrId;
    if (el && el.contains(document.activeElement)) return;
    renderFn(data);
  };
}

/* ─── SHARED HELPERS ──────────────────────────────────────── */

/* flag() — render a toggle button (returns HTML string, legacy compat) */
function flag(label, action, on, style) {
  return `<button class="flag ${on ? (style || 'on') : ''}" onclick="cmd('${action}')">${label}</button>`;
}

/* nudgeRow() — render a label + ±step row (returns HTML string) */
function nudgeRow(key, val, action, step) {
  const d = typeof val === 'number' ? val.toFixed(2) : (val || '—');
  return `<div class="arp-row">
    <span class="arp-key">${key}</span>
    <div class="arp-ctrl">
      <button class="nudge" onclick="cmd('${action}',{value:${-step}})">${-step}</button>
      <span class="arp-val">${d}</span>
      <button class="nudge" onclick="cmd('${action}',{value:${step}})">+${step}</button>
    </div></div>`;
}

/* segBtn() — render a segmented button group (returns HTML string) */
function segBtn(options, current, onClickTpl) {
  return options.map(o => {
    const active = o === current;
    const cls = active ? 'btn accent' : 'btn';
    const onclick = onClickTpl.replace('__V__', o);
    return `<button class="${cls}" onclick="${onclick}">${o}</button>`;
  }).join('');
}

/* ─── GROUPED RAIL NAV + ACTIVE STATE ─────────────────────── */
document.addEventListener('DOMContentLoaded', function () {
  const nav   = document.querySelector('.nav-bar');
  const shell = document.querySelector('.shell');

  // Reserve exactly the nav's rendered height so no content hides behind it.
  // The height changes when a group opens/closes, so re-measure on every change
  // (the ResizeObserver below also catches wraps and safe-area shifts).
  function fitNav() {
    if (nav && shell) shell.style.paddingBottom = (nav.offsetHeight + 8) + 'px';
  }

  if (nav) {
    const path = window.location.pathname;
    const cats = nav.querySelectorAll('.nav-cat');
    const subs = nav.querySelectorAll('.nav-sub');

    // Mark the active page link, and remember which group holds it.
    let hereGroup = null;
    nav.querySelectorAll('a.nav-item, a.nav-cat').forEach(function (a) {
      const href = a.getAttribute('href');
      const match = href === '/' ? path === '/'
                                 : (path === href || path.startsWith(href + '/'));
      if (match) {
        a.classList.add('active');
        const sub = a.closest('.nav-sub');
        if (sub) hereGroup = sub.id;
      }
    });

    // Open exactly one group (or none when id is null).
    function setGroup(id) {
      subs.forEach(function (s) { s.classList.toggle('open', id !== null && s.id === id); });
      cats.forEach(function (c) { c.classList.toggle('open', id !== null && c.dataset.group === id); });
      fitNav();
    }

    cats.forEach(function (c) {
      if (!c.dataset.group) return;                        // PLAY / SYS are plain links
      if (c.dataset.group === hereGroup) c.classList.add('here');
      c.addEventListener('click', function () {
        setGroup(c.classList.contains('open') ? null : c.dataset.group);
      });
    });

    if (hereGroup) setGroup(hereGroup);                    // auto-open the current page's group
  }

  fitNav();
  window.addEventListener('resize', fitNav);
  if (window.ResizeObserver && nav) { try { new ResizeObserver(fitNav).observe(nav); } catch (e) {} }

  ORC.start();
});
