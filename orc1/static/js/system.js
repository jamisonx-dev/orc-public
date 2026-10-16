/* system.js — ORC-1 system health helpers
   Used by system.html (full page) and main.js (compact card).
   Depends on nothing — pure functions + self-contained poll. */

'use strict';

// ── DAW / DJ mode (REAPER or Mixxx on HDMI; /api/daw) ───────────────────────

async function dawLoad() {
  const st = document.getElementById('daw-state');
  if (!st) return;
  try {
    const d = await fetch('/api/daw').then(r => r.json());
    if (!d.ok) throw new Error(d.error || '?');
    st.innerHTML = (d.active
        ? `<span style="color:var(--ok,#4ade80)">● ${d.app === 'mixxx' ? 'DJ (MIXXX)' : 'DAW (REAPER)'}</span>`
        : '<span style="color:var(--muted)">off</span>')
      + (d.busy ? ` <span style="color:var(--warn)">· busy (${d.state === 'deactivating'
            ? 'stopping, engines coming back' : d.state === 'activating' ? 'starting'
            : 'working'}…)</span>` : '')
      + (d.engines_off ? ' <span style="color:var(--danger)">· engines OFF</span>' : '')
      + (d.hdmi ? '' : ' <span style="color:var(--danger)">· no HDMI monitor</span>');
    // busy = a start/stop/restore is still running (engines going down one at
    // a time, the app's start hooks, or the ExecStopPost restore): a click
    // then would race it, so every button waits.
    dawButtons(d.active || d.busy, !d.active || d.busy);
  } catch (e) {
    st.innerHTML = '<span style="color:var(--danger)">status unavailable</span>';
  }
}

function dawButtons(goOff, stopOff) {
  document.querySelectorAll('#daw-card .daw-go').forEach(b => { b.disabled = goOff; });
  document.getElementById('daw-stop').disabled = stopOff;
}

async function dawStart(app, enginesOff) {
  dawButtons(true, true);             // until the next poll reports busy/active
  const m = document.getElementById('daw-msg');
  const name = app === 'mixxx' ? 'Mixxx' : 'REAPER';
  m.textContent = enginesOff ? `stopping the engines one at a time, then starting ${name}…`
                             : `starting ${name}…`;
  await fetch('/api/daw', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ action: 'start', app, engines_off: enginesOff }) });
  setTimeout(() => { m.textContent = ''; dawLoad(); }, enginesOff ? 8000 : 3000);
}

async function dawStop() {
  dawButtons(true, true);
  const m = document.getElementById('daw-msg');
  m.textContent = 'asking it to quit (the engines restart if they were stopped)…';
  await fetch('/api/daw', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ action: 'stop' }) });
  setTimeout(async () => {
    await dawLoad();
    const d = await fetch('/api/daw').then(r => r.json()).catch(() => ({}));
    m.innerHTML = d.active && !d.busy           // busy = it is closing, engines returning
      ? '<span style="color:var(--danger)">still open: it may be asking a question on the monitor</span>'
      : '';
  }, 17000);
}

if (document.getElementById('daw-card')) { dawLoad(); setInterval(dawLoad, 5000); }

// ── VISUALS (compact: status + start/stop; the rest is on /visuals) ──────────
async function visLoad() {
  const st = document.getElementById('vis-state');
  try {
    const d = await fetch('/api/visuals').then(r => r.json());
    if (!d.ok) throw new Error(d.error || '?');
    st.innerHTML = d.active ? `<span style="color:var(--ok,#4ade80)">● on</span>`
                            : '<span style="color:var(--muted)">off</span>';
    document.getElementById('vis-start').disabled = d.active;
    document.getElementById('vis-stop').disabled = !d.active;
  } catch (e) {
    st.innerHTML = '<span style="color:var(--danger)">status unavailable</span>';
  }
}

async function visQuick(action) {
  await fetch('/api/visuals', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ action }) }).catch(() => {});
  setTimeout(visLoad, action === 'start' ? 3000 : 800);
}

if (document.getElementById('vis-card')) { visLoad(); setInterval(visLoad, 5000); }

// ── HUD kiosk (start/stop the live play-state screen on the monitor) ─────────
async function hudLoad() {
  const st = document.getElementById('hud-state');
  try {
    const d = await fetch('/api/hud_kiosk').then(r => r.json());
    if (!d.ok) throw new Error(d.error || '?');
    // a live feed older than a few seconds means the BRAIN is down, not the screen
    const feed = (d.feed_age_s === null || d.feed_age_s > 5)
      ? ' <span style="color:var(--danger)">· brain not publishing</span>' : '';
    st.innerHTML = (d.active ? `<span style="color:var(--ok,#4ade80)">● on</span>`
                             : '<span style="color:var(--muted)">off</span>') + feed;
    document.getElementById('hud-start').disabled = d.active;
    document.getElementById('hud-stop').disabled = !d.active;
  } catch (e) {
    st.innerHTML = '<span style="color:var(--danger)">status unavailable</span>';
  }
}

async function hudQuick(action) {
  await fetch('/api/hud_kiosk', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ action }) }).catch(() => {});
  setTimeout(hudLoad, action === 'start' ? 3000 : 800);
}

if (document.getElementById('hud-card')) { hudLoad(); setInterval(hudLoad, 5000); }

// ── Rendering helpers ─────────────────────────────────────────────────────────

function _sysBarCls(pct, warn, crit) {
  if (pct >= crit)  return 'var(--danger)';
  if (pct >= warn)  return 'var(--warn)';
  return 'var(--live)';
}

function _sysGauge(label, valHtml, pct, warn, crit) {
  const col = _sysBarCls(pct, warn, crit);
  return `
    <div style="margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px">
        <span style="font-size:9px;color:var(--label);font-family:var(--fh);letter-spacing:.12em">${label}</span>
        <span style="font-size:12px;color:var(--bright)">${valHtml}</span>
      </div>
      <div style="height:3px;background:var(--dim);border-radius:2px;overflow:hidden">
        <div style="height:100%;width:${pct}%;background:${col};border-radius:2px;transition:width .6s,background .6s"></div>
      </div>
    </div>`;
}

function _sysTempHtml(d) {
  const col = d.temp_c >= 80 ? 'var(--danger)' : d.temp_c >= 65 ? 'var(--warn)' : 'var(--bright)';
  const powerHtml = d.throttle_ok
    ? `<span style="font-size:10px;color:var(--live)">✓ POWER NOMINAL</span>`
    : (d.throttle_flags || []).map(f =>
        `<div style="font-size:9px;color:var(--warn)">⚠ ${f}</div>`).join('');
  return `
    <div style="display:flex;align-items:center;gap:16px">
      <div>
        <div style="font-family:var(--fh);font-size:48px;font-weight:900;line-height:1;color:${col};letter-spacing:-.02em;transition:color .4s">${d.temp_c.toFixed(1)}</div>
        <div style="font-size:8px;color:var(--label);font-family:var(--fh);letter-spacing:.15em;margin-top:4px">°C · CPU TEMP</div>
      </div>
      <div style="flex:1">${powerHtml}</div>
    </div>`;
}

function _sysServicesHtml(services) {
  // up: true = RUNNING, "daw" = stopped on purpose by DAW/DJ mode, else DOWN
  return Object.entries(services || {}).map(([name, up]) => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:11px;color:var(--body)">${name}</span>
      <span style="font-size:8px;font-family:var(--fh);font-weight:700;letter-spacing:.1em;padding:2px 7px;border-radius:2px;
        ${up === 'daw' ? 'color:var(--muted);border:1px solid var(--border)'
          : up ? 'color:var(--live);background:rgba(34,224,151,.08);border:1px solid rgba(34,224,151,.3)'
             : 'color:var(--danger);background:rgba(224,90,0,.08);border:1px solid rgba(224,90,0,.3)'}
      ">${up === 'daw' ? 'OFF (DAW/DJ)' : up ? 'RUNNING' : 'DOWN'}</span>
    </div>`).join('');
}

function _sysRecsHtml(recs) {
  if (!recs || !recs.length)
    return '<div style="font-size:10px;color:var(--muted);font-style:italic">no recordings yet</div>';
  return recs.map(r => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid var(--border);font-size:10px">
      <span style="color:var(--body);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:75%">${r.name}</span>
      <span style="color:var(--muted);flex-shrink:0">${r.size_kb}KB</span>
    </div>`).join('');
}

// ── Full-page render (system.html) ────────────────────────────────────────────

function _sysRenderFull(d) {
  const tempEl = document.getElementById('sys-temp');
  const gaugesEl = document.getElementById('sys-gauges');
  const uptimeEl = document.getElementById('sys-uptime');
  const svcsEl = document.getElementById('sys-services');
  const recsEl = document.getElementById('sys-recs');

  if (tempEl)   tempEl.innerHTML   = _sysTempHtml(d);
  if (uptimeEl) uptimeEl.textContent = 'UP ' + d.uptime;
  if (gaugesEl) gaugesEl.innerHTML =
    _sysGauge('CPU', d.cpu_pct + '%', d.cpu_pct, 70, 90) +
    _sysGauge('RAM', d.mem_used_mb + ' MB / ' + d.mem_total_mb + ' MB', d.mem_pct, 75, 90) +
    _sysGauge('DISK', d.disk_used_gb + ' GB / ' + d.disk_total_gb + ' GB', d.disk_pct, 70, 85);
  if (svcsEl)   svcsEl.innerHTML   = _sysServicesHtml(d.services);
  if (recsEl)   recsEl.innerHTML   = _sysRecsHtml(d.recordings);
}

// ── Compact card render (main PLAY page) ──────────────────────────────────────

function _sysRenderCompact(d, el) {
  const col = d.temp_c >= 80 ? 'var(--danger)' : d.temp_c >= 65 ? 'var(--warn)' : 'var(--muted)';
  const svcs = Object.values(d.services || {});
  const allUp = svcs.every(Boolean);
  const downNames = Object.entries(d.services || {}).filter(([,v]) => !v).map(([k]) => k);
  el.innerHTML = `
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
      <span style="font-family:var(--fh);font-size:28px;font-weight:900;color:${col};line-height:1">${d.temp_c.toFixed(1)}<span style="font-size:10px;font-weight:400;color:var(--label)"> °C</span></span>
      <span style="font-size:13px;color:var(--bright)">${d.cpu_pct}% <span style="font-size:9px;color:var(--label)">CPU</span></span>
      <span style="font-size:13px;color:var(--bright)">${d.mem_used_mb}MB <span style="font-size:9px;color:var(--label)">RAM</span></span>
      <span style="font-size:11px;color:var(--accent)">UP ${d.uptime}</span>
    </div>
    <div style="height:3px;background:var(--dim);border-radius:2px;overflow:hidden;margin-bottom:8px">
      <div style="height:100%;width:${d.cpu_pct}%;background:${_sysBarCls(d.cpu_pct,70,90)};border-radius:2px;transition:width .6s"></div>
    </div>
    <div style="display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:9px;${allUp ? 'color:var(--live)' : 'color:var(--danger)'}">
        ${allUp ? '✓ ALL SERVICES RUNNING' : '⚠ DOWN: ' + downNames.join(', ')}
      </span>
      <a href="/system" style="font-size:9px;font-family:var(--fh);color:var(--accent);text-decoration:none;letter-spacing:.08em">SYSTEM →</a>
    </div>`;
}

// ── Poll loop (runs on system.html) ───────────────────────────────────────────

async function _sysPoll() {
  try {
    const r = await fetch('/api/system/stats?' + Date.now());
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (!d.error) _sysRenderFull(d);
  } catch (e) {
    const el = document.getElementById('sys-gauges');
    if (el) el.innerHTML = '<span style="font-size:10px;color:var(--muted)">system service unavailable</span>';
  }
  setTimeout(_sysPoll, 3000);
}

// Auto-start if on the system page
if (document.getElementById('sys-gauges')) _sysPoll();

// ── AUDIO OUT card (system.html) ──────────────────────────────────────────────
async function loadAudio(){
  const el=document.getElementById('audio-list');
  if(!el)return;
  try{
    const d=await fetch('/api/audio/devices').then(r=>r.json());
    if(!d.devices.length){el.innerHTML='<span style="font-size:10px;color:var(--muted)">no playback cards found</span>';return;}
    el.innerHTML=d.devices.map(c=>{
      const active=c.card===d.active;
      const pending=c.card===d.chosen&&!active;
      return `<button class="flag ${active?'on':''}${pending?' warn':''}" title="${c.name}"
        onclick="setAudio('${c.card}')">${c.card}${active?' ✓':pending?' …':''}</button>`;
    }).join('');
  }catch(e){el.innerHTML='<span style="font-size:10px;color:var(--muted)">audio api unavailable</span>';}
}
async function setAudio(card){
  if(!confirm('Switch audio output to '+card+'?\nThe audio engines will restart (~10s of silence).'))return;
  try{
    await fetch('/api/audio/device',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({card})});
    document.getElementById('audio-list').innerHTML=
      '<span style="font-size:10px;color:var(--warn)">restarting audio on '+card+'…</span>';
    setTimeout(loadAudio,12000);
  }catch(e){loadAudio();}
}

// ── BLUETOOTH AUDIO card (system.html) ────────────────────────────────────────
let _btOn=false;
async function loadBt(){
  const b=document.getElementById('bt-toggle'), st=document.getElementById('bt-status');
  if(!b)return;
  try{
    const d=await fetch('/api/bt/status?'+Date.now()).then(r=>r.json());
    _btOn=d.on;
    b.textContent=d.on?'● ON':'○ OFF';
    b.className='flag'+(d.on?' on':'');
    st.textContent=d.on
      ? (d.connected?('connected: '+(d.device||'phone')):'discoverable as “pibox”')
      : 'off';
  }catch(e){ if(st)st.textContent='api unavailable'; }
}
async function btToggle(){
  const action=_btOn?'off':'on';
  const st=document.getElementById('bt-status');
  if(st)st.textContent=(action==='on'?'turning on…':'turning off…');
  try{ await fetch('/api/bt/'+action,{method:'POST'}); }catch(e){}
  loadBt();
}

// Auto-start audio + BT cards only when present (system page)
if (document.getElementById('audio-list')) loadAudio();
if (document.getElementById('bt-toggle')) { loadBt(); setInterval(loadBt,5000); }

// ── Config override card ──────────────────────────────────────────────────────

function _cfgSection(key) {
  const p = key.split('.')[0];
  if (p === 'SYNTH')       return 'SYNTH';
  if (p === 'SAMPLER')     return 'SAMPLER';
  if (p === 'AUTOSAMPLER') return 'AUTOSAMPLER';
  if (p.startsWith('ARP')) return 'ARP';
  return p;
}

function _cfgInputHtml(item) {
  const val = item.current !== null && item.current !== undefined ? item.current : item.default;
  const k   = item.key.replace(/\./g, '__');  // safe DOM id fragment
  if (item.type === 'bool') {
    return `<button class="flag${val ? ' active' : ''}" id="cfg-btn-${k}"
      onclick="ORC._cfgSet('${item.key}',${!val})"
      style="min-width:42px;font-size:9px">${val ? 'ON' : 'OFF'}</button>`;
  }
  if (item.type === 'str') {
    // Plain text input. Without this, str rows fell through to the numeric
    // branch below and rendered <input type="number">, which cannot hold a
    // comma-separated value like "drum,poly,chip,sampler".
    return `<input id="cfg-inp-${k}" type="text" value="${val == null ? '' : val}"
      onchange="ORC._cfgSet('${item.key}', this.value)"
      style="background:var(--field,#1a1c21);color:var(--bright);border:1px solid var(--border);
             padding:3px 5px;font-size:10px;border-radius:2px;width:180px">`;
  }
  if (item.type === 'choice') {
    const opts = (item.choices || []).map(c =>
      `<option value="${c}"${val === c ? ' selected' : ''}>${c}</option>`).join('');
    return `<select id="cfg-inp-${k}" onchange="ORC._cfgSet('${item.key}',this.value)"
      style="background:var(--field,#1a1c21);color:var(--bright);border:1px solid var(--border);
             padding:3px 5px;font-size:10px;border-radius:2px">${opts}</select>`;
  }
  // float or int
  const step = item.type === 'int' ? '1' : '0.01';
  const mn   = item.min !== null && item.min !== undefined ? `min="${item.min}"` : '';
  const mx   = item.max !== null && item.max !== undefined ? `max="${item.max}"` : '';
  return `<input type="number" id="cfg-inp-${k}" value="${val}" step="${step}" ${mn} ${mx}
    onblur="ORC._cfgSet('${item.key}',${item.type==='int'}?parseInt(this.value):parseFloat(this.value))"
    style="width:72px;background:var(--field,#1a1c21);color:var(--bright);
           border:1px solid var(--border);padding:3px 5px;font-size:10px;
           border-radius:2px;text-align:right">`;
}

function _cfgRender(el, schema) {
  // Group by section preserving order
  const order = [], seen = {}, groups = {};
  for (const item of schema) {
    const sec = _cfgSection(item.key);
    if (!seen[sec]) { seen[sec] = true; order.push(sec); groups[sec] = []; }
    groups[sec].push(item);
  }

  let html = '';
  for (const sec of order) {
    html += `<div style="margin:10px 0 4px;font-size:8px;font-family:var(--fh);
      letter-spacing:.15em;color:var(--label);border-bottom:1px solid var(--border);
      padding-bottom:3px">${sec}</div>`;
    for (const item of groups[sec]) {
      const isOvr  = item.overridden;
      const k      = item.key.replace(/\./g, '__');
      const shortK = item.key.split('.').pop();
      const rstBtn = isOvr
        ? `<button class="flag warn" onclick="ORC._cfgReset('${item.key}')"
             style="font-size:8px;padding:2px 6px;flex-shrink:0">RESET</button>`
        : '';
      const defNote = isOvr
        ? `<span style="font-size:8px;color:var(--muted);white-space:nowrap">&nbsp;(def: ${item.default})</span>`
        : '';
      html += `
        <div id="cfg-row-${k}" style="padding:5px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:6px;justify-content:space-between;flex-wrap:wrap">
            <span style="font-size:10px;color:${isOvr?'var(--accent)':'var(--body)'};flex:1;min-width:60px">${shortK}</span>
            <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
              ${_cfgInputHtml(item)}${defNote}${rstBtn}
            </div>
          </div>
          <div style="font-size:8px;color:var(--muted);margin-top:2px;line-height:1.4">${item.desc}</div>
          <div id="cfg-msg-${k}" style="margin-top:3px;min-height:14px"></div>
        </div>`;
    }
  }
  el.innerHTML = html;
}

async function _cfgLoad() {
  const el = document.getElementById('config-body');
  if (!el) return;
  try {
    const r = await fetch('/api/config?' + Date.now());
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    _cfgRender(el, d.schema);
  } catch (e) {
    el.innerHTML = '<span style="font-size:10px;color:var(--muted)">config API unavailable</span>';
  }
}

async function _cfgSet(key, value) {
  const k    = key.replace(/\./g, '__');
  const msgEl = document.getElementById('cfg-msg-' + k);
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key, value})
    });
    const d = await r.json();
    if (d.ok) {
      const svc = d.restart;
      if (msgEl) {
        if (svc && svc !== 'none') {
          msgEl.innerHTML = `<span class="flag warn" style="font-size:8px;cursor:pointer"
            onclick="ORC._cfgRestart('${svc}')">⚠ RESTART ${svc.toUpperCase()} TO APPLY →</span>`;
        } else {
          msgEl.innerHTML = '<span style="font-size:8px;color:var(--live)">✓ saved</span>';
          setTimeout(() => { if (msgEl) msgEl.innerHTML = ''; }, 2000);
        }
      }
      setTimeout(_cfgLoad, 400);
    } else {
      if (msgEl) msgEl.innerHTML =
        `<span style="font-size:8px;color:var(--danger)">${d.error || 'error'}</span>`;
    }
  } catch (e) {
    if (msgEl) msgEl.innerHTML = '<span style="font-size:8px;color:var(--danger)">network error</span>';
  }
}

async function _cfgReset(key) {
  const k    = key.replace(/\./g, '__');
  const msgEl = document.getElementById('cfg-msg-' + k);
  try {
    const r = await fetch('/api/config/' + encodeURIComponent(key), {method: 'DELETE'});
    const d = await r.json();
    if (d.ok) {
      if (msgEl) { msgEl.innerHTML = '<span style="font-size:8px;color:var(--live)">✓ reset to default</span>'; }
      setTimeout(_cfgLoad, 400);
    }
  } catch (e) {}
}

async function _cfgRestart(svc) {
  // Find all restart chips for this service and dim them
  document.querySelectorAll(`[onclick*="_cfgRestart('${svc}')"]`).forEach(b => {
    b.textContent = 'RESTARTING…'; b.style.pointerEvents = 'none';
  });
  try {
    const d = await fetch('/api/restart/' + svc, {method: 'POST'}).then(r => r.json());
    if (d && d.ok === false) {
      // e.g. "synth" while DAW/DJ mode has the engines off: say why, keep it
      document.querySelectorAll(`[onclick*="_cfgRestart('${svc}')"]`).forEach(b => {
        b.textContent = '⚠ ' + (d.error || 'restart refused');
        b.style.pointerEvents = '';
      });
      return;
    }
    // If restarting 'web', page will go away — nothing to do.
    // For others, reload the card after a few seconds.
    if (svc !== 'web') setTimeout(_cfgLoad, 3500);
  } catch (e) {
    setTimeout(_cfgLoad, 3500);
  }
}

// ---------------------------------------------------------------------------
// HEALTH CHECK card
// ---------------------------------------------------------------------------
// Renders the latest report from orc1-health.timer. Two rules drive this:
//
//  1. ALWAYS show the report's age. A card that presented a stale report as
//     current would be precisely the comfortable lie the health check exists
//     to eliminate -- so if the timer dies, this card says so loudly rather
//     than continuing to show the last good news.
//  2. Never invent a verdict. If there is no report, it says there is no
//     report; it does not say "healthy".

function _healthAge(sec) {
  if (sec < 90)   return Math.round(sec) + 's ago';
  if (sec < 5400) return Math.round(sec / 60) + 'm ago';
  return (sec / 3600).toFixed(1) + 'h ago';
}

async function loadHealth() {
  const el = document.getElementById('health-body');
  if (!el) return;
  let d;
  try {
    const r = await fetch('/api/health');
    d = await r.json();
  } catch (e) {
    el.innerHTML = '<span style="font-size:10px;color:var(--muted)">could not reach the web API</span>';
    return;
  }
  if (!d.ok) {
    // Escaped like every other report-derived string. Not exploitable today
    // (error is always locally generated), but the discipline should not
    // depend on that staying true.
    el.innerHTML = '<span style="font-size:10px;color:var(--muted)">' +
      _esc(d.error || 'no report') + '</span>';
    return;
  }

  const c = d.counts || {};
  const dead = c.dead || 0, warn = c.warn || 0, idle = c.idle || 0, ok = c.ok || 0;
  const age = d.age || 0;
  // A report older than ~40 min means the 15-minute timer has missed at least
  // two runs -- treat the checker itself as suspect and say so.
  const stale = age > 2400;

  let h = '<div class="flags" style="align-items:center;gap:6px;margin-bottom:8px">';
  h += '<span class="flag ' + (dead ? 'warn' : '') + '" style="pointer-events:none">' +
       dead + ' DEAD</span>';
  h += '<span class="flag" style="pointer-events:none">' + ok + ' passing</span>';
  if (warn) h += '<span class="flag" style="pointer-events:none">' + warn + ' warning</span>';
  if (idle) h += '<span class="flag" style="pointer-events:none">' + idle + ' accepted</span>';
  h += '<span style="font-size:9px;color:' + (stale ? 'var(--danger)' : 'var(--muted)') +
       '">' + _healthAge(age) + (stale ? ' — TIMER MAY BE DEAD' : '') + '</span>';
  h += '</div>';

  const bad = (d.findings || []).filter(f => f.level === 'dead');
  if (!bad.length) {
    h += '<div style="font-size:10px;color:var(--live)">nothing dead in the last check</div>';
  } else {
    h += '<div style="font-size:10px;line-height:1.5">';
    bad.slice(0, 12).forEach(f => {
      h += '<div style="margin-bottom:3px"><span style="color:var(--danger)">■</span> ' +
           _esc(f.msg) + '</div>';
    });
    if (bad.length > 12) {
      h += '<div style="color:var(--muted)">… and ' + (bad.length - 12) + ' more</div>';
    }
    h += '</div>';
  }
  el.innerHTML = h;
}

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
}

async function healthRun() {
  const el = document.getElementById('health-body');

  // Remember the report we already had, so we can tell a genuinely fresh
  // result from the old one being re-rendered. Without this, a trigger that
  // silently failed would look identical to one that worked.
  let before = 0;
  try { before = (await (await fetch('/api/health')).json()).ts || 0; } catch (e) {}

  let started = false, err = '';
  try {
    const r = await fetch('/api/health/run', {method: 'POST'});
    const d = await r.json();
    started = !!d.ok;
    if (!d.ok) err = d.error || ('HTTP ' + r.status);
  } catch (e) { err = 'could not reach the web API'; }

  if (!started) {
    if (el) el.innerHTML = '<span style="font-size:10px;color:var(--danger)">' +
      _esc('could not start a check: ' + err) + '</span>';
    setTimeout(loadHealth, 2500);
    return;
  }

  if (el) el.innerHTML = '<span style="font-size:10px;color:var(--muted)">running… (~15s)</span>';
  // Poll for a report NEWER than the one we started with, rather than
  // guessing a fixed delay. If it never arrives, say so instead of quietly
  // showing the stale report as though the run had succeeded.
  let tries = 0;
  const poll = setInterval(async () => {
    tries++;
    let ts = 0;
    try { ts = (await (await fetch('/api/health')).json()).ts || 0; } catch (e) {}
    if (ts > before) { clearInterval(poll); loadHealth(); return; }
    if (tries > 15) {
      clearInterval(poll);
      await loadHealth();
      if (el) el.insertAdjacentHTML('beforeend',
        '<div style="font-size:9px;color:var(--danger);margin-top:6px">' +
        'the check was started but produced no new report — see ' +
        '<code>journalctl -u orc1-health</code></div>');
    }
  }, 3000);
}

// Expose for onclick handlers in generated HTML
if (typeof ORC === 'undefined') window.ORC = {};
ORC._cfgSet     = _cfgSet;
ORC._cfgReset   = _cfgReset;
ORC._cfgRestart = _cfgRestart;
ORC.loadHealth  = loadHealth;
ORC.healthRun   = healthRun;

// Auto-start config card if on the system page
if (document.getElementById('config-body')) _cfgLoad();

// Auto-start the health card if present. Refreshed every 60 s -- the report
// itself only changes every 15 min, but the AGE display must keep moving so a
// dead timer becomes visible without a reload.
if (document.getElementById('health-body')) {
  loadHealth();
  setInterval(loadHealth, 60000);
}
