/* RECORDING NOW card at the top of /skipback (moved from PLAY 2026-10-26).
   Reads the recorder's own per-track status (/api/skipback/tracks), not the
   systemd state: a running service is not proof of recording. "recording"
   there = ring open AND drains landing; status_age = seconds since the
   recorder last reported (every ~5 s while alive). */
'use strict';

async function _skipbackCompactPoll() {
  const el = document.getElementById('skipback-compact-body');
  if (!el) return;
  const row = (dotColor, text, extra) => `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span style="font-size:11px"><span style="color:${dotColor}">●</span>&nbsp;${text}${extra || ''}</span>
      </div>`;
  try {
    const d = await fetch('/api/skipback/tracks?' + Date.now()).then(r => r.json());
    const armed = (d.tracks || []).filter(t => t.armed);
    let html;
    if (d.status_age == null || d.status_age > 15) {
      html = row('var(--danger)', '<span style="color:var(--danger)">RECORDER NOT REPORTING</span>');
    } else if (!armed.length) {
      html = row('var(--warn)', '<span style="color:var(--warn)">NO TRACKS ARMED</span>');
    } else {
      html = armed.map(t => {
        const ok = t.recording && t.connected;
        const state = ok ? 'RECORDING' : '<span style="color:var(--danger)">NOT RECORDING</span>';
        const notes = [];
        if (t.fallback) notes.push('pisound inputs (source missing)');
        if (t.gaps) notes.push(`${t.gaps} gap${t.gaps === 1 ? '' : 's'}`);
        const extra = `&nbsp;&nbsp;<span style="color:var(--bright)">${t.name}</span>`
          + `&nbsp;<span style="color:var(--label);font-size:9px">${t.length_secs} s</span>`
          + (notes.length ? `&nbsp;<span style="color:var(--warn);font-size:9px">${notes.join(' · ')}</span>` : '');
        return row(ok ? 'var(--live)' : 'var(--danger)', state, extra);
      }).join('');
    }
    el.innerHTML = `<div style="display:flex;flex-direction:column;gap:4px">${html}</div>`;
  } catch (e) {
    el.innerHTML = row('var(--danger)', '<span style="color:var(--danger)">No response from the ORC web server</span>');
  }
  setTimeout(_skipbackCompactPoll, 6000);
}
_skipbackCompactPoll();
