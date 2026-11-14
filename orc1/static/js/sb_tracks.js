/* sb_tracks.js — the skipback TRACK card pieces shared by /skipback and
   /sampler (2026-11-12, owner: "it's all really confusing").

   One "Records" picker in plain names (GET /api/skipback/tracks -> choices),
   applied AT ONCE through POST /api/skipback/tracks/<id>/source. A pair =
   stereo track, a single input = mono track: "what i pick, always" (a width
   change empties that track's ring; skipback is ephemeral until grabbed, so
   it is not asked about). Tracks are shown by NAME, never as t1 / t2.
   Each page draws its own card around these helpers. */
'use strict';

window.SBT = (() => {
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const name = (t) => (t && (t.display_name || t.name)) || 'track';

  // <option>s for a track's Records picker; the current pick is selected,
  // and a pick whose port is not in the graph right now is still shown.
  function options(t, choices) {
    const groups = [];
    (choices || []).forEach(c => {
      let g = groups.find(x => x.name === c.group);
      if (!g) groups.push(g = { name: c.group, items: [] });
      g.items.push(c);
    });
    const cur = t.source_key || 'auto';
    const missing = !t.source_present && cur !== 'auto'
      ? `<option value="${esc(cur)}" selected>${esc(t.source_label)} (not connected now)</option>` : '';
    return missing + groups.map(g => {
      const opts = g.items.map(c =>
        `<option value="${esc(c.key)}"${c.key === cur ? ' selected' : ''}>${esc(c.label)}</option>`).join('');
      return g.name === 'AUTO' ? opts : `<optgroup label="${esc(g.name)}">${opts}</optgroup>`;
    }).join('');
  }

  // ● RECORDING / ● NOT RECORDING / ○ OFF
  function state(t) {
    if (!t.armed) return '<span style="color:var(--muted)">○ OFF</span>';
    if (t.card_blocked) return `<span style="color:var(--danger)">● NOT RECORDING: ${esc(t.card_blocked)}</span>`;
    if (t.ram_blocked) return '<span style="color:var(--danger)">● NOT RECORDING: over the RAM budget</span>';
    return (t.recording && t.connected)
      ? '<span style="color:var(--ok,#4ade80)">● RECORDING</span>'
      : '<span style="color:var(--danger)">● NOT RECORDING</span>';
  }

  // The line under the picker: only when it says something you need.
  function nowLine(t) {
    if (!t.armed || !t.now_label) return '';
    if (t.source_key === 'auto')
      return `<span style="color:var(--muted)">now: ${esc(t.now_label)}</span>`;
    if (t.now_differs)
      return `<span style="color:var(--warn,#f5a524)">now getting ${esc(t.now_label)} — ${esc(t.source_label)} is not connected</span>`;
    return '';
  }

  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return await r.json();
    } catch (e) { return { ok: false, error: String(e) }; }
  }
  const setSource = (tid, key) => post(`/api/skipback/tracks/${tid}/source`,
    typeof key === 'string' ? { key } : key);
  const arm = (tid, on) => post(`/api/skipback/tracks/${tid}/arm`, { armed: !!on });

  return { esc, name, options, state, nowLine, setSource, arm };
})();
