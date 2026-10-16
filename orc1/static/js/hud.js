/* ORC-1 · hud.js -- the phone / desktop HUD: what is playing RIGHT NOW.

   Source: GET /api/hud/stream (Server-Sent Events), the brain's play-state feed
   (orc1_playstate.py) relayed by the web process. Falls back to polling GET /api/hud
   when the stream is refused or dies. Same model as the monitor kiosk (daw/orc_hud.py):
   step -> BAR/BEAT/STEP, beat lamps from the step's age, per-module light that jumps
   on a hit-counter change and fades.

   Everything from the feed is drawn with textContent or canvas fillText, never innerHTML:
   track names and chord names are user data. */
'use strict';

const HUD = (() => {
  const TRACK_COLORS = ['#eb5050', '#f09632', '#ebd23c', '#6ed75a', '#3ccdbe', '#4696f0', '#966ef0', '#e15fc8'];
  const SHORT = { chip_p1: 'P1', chip_p2: 'P2', chip_tri: 'TRI', chip_noise: 'NOISE', chip_noise2: 'NOISE2',
                  chip_pcm: 'PCM', gb_wave: 'WAVE', poly: 'POLY', mono: 'MONO', drums: 'DRUMS',
                  bbkit: 'BBKIT', sampler: 'SMPL', visuals: 'VIS' };
  const STALE_MS = 3000;           // the brain heartbeats every 1 s

  // ---- model (pure) -------------------------------------------------------------
  function barPos(step) {
    if (step === null || step === undefined || step < 0) return null;
    return [Math.floor(step / 16) + 1, Math.floor((step % 16) / 4) + 1, (step % 4) + 1];
  }

  function beatFlash(tr, sinceS, tau) {
    tau = tau || 0.12;
    if (!tr || !tr.running || !(tr.step >= 0)) return 0;
    const into = (tr.step % 4) * ((tr.step_ms || 125) / 1000) + sinceS;
    return Math.exp(-Math.max(0, into) / tau);
  }

  class Pulses {
    constructor(tau) { this.tau = tau || 0.22; this.last = {}; this.level = {}; this.t = null; }
    update(mods, nowS) {
      const dt = this.t === null ? 0 : Math.max(0, nowS - this.t);
      this.t = nowS;
      const k = Math.exp(-dt / this.tau);
      for (const n of Object.keys(this.level)) {
        this.level[n] *= k;
        if (this.level[n] < 0.01) delete this.level[n];
      }
      for (const [n, v] of Object.entries(mods || {})) {
        const hits = v.hits || 0, prev = this.last[n];
        this.last[n] = hits;
        if (prev === undefined) continue;                 // first sight is history, not a hit
        if (hits !== prev) {                              // !== so a brain restart (count resets) flashes too
          const vel = Math.min(127, Math.max(1, (v.vel | 0) || 100));
          this.level[n] = Math.max(this.level[n] || 0, 0.35 + 0.65 * vel / 127);
        }
      }
    }
    get(n) { return this.level[n] || 0; }
    active() { return Object.keys(this.level).length > 0; }
  }

  function moduleOrder(bindings) {
    return Object.entries(bindings || {}).map(([m, c]) => [m, parseInt(c, 10)])
      .filter(x => !isNaN(x[1])).sort((a, b) => a[1] - b[1] || (a[0] < b[0] ? -1 : 1));
  }

  // ---- view ---------------------------------------------------------------------
  const $ = id => document.getElementById(id);
  const pulses = new Pulses();
  let S = null, recvMs = 0, lastMsgMs = 0, everMsg = false, raf = 0, es = null, poll = 0, retry = 0;
  const textCache = {};
  const modEls = {};

  function setText(id, txt) {
    if (textCache[id] === txt) return;
    textCache[id] = txt;
    const el = $(id);
    if (el) el.textContent = txt;
  }

  function paintText() {
    const tr = S.transport || {}, ch = S.chord || {};
    setText('hud-bpm', tr.bpm ? tr.bpm.toFixed(1) : '---');
    const clk = { internal: 'INT', external: 'EXT', drift: 'DRIFT' }[tr.clock] || '--';
    setText('hud-clock', 'BPM  ' + clk);
    setText('hud-chord', ch.name || '--');
    setText('hud-key', [ch.key, ch.scale, ch.preset, ch.octave !== undefined ? 'oct ' + ch.octave : '']
      .filter(x => x !== undefined && x !== '').join('   '));
    const badge = $('hud-badge');
    const mode = tr.rec ? 'rec' : (tr.running ? 'play' : '');
    setText('hud-badge', tr.rec ? 'REC' : (tr.running ? 'PLAY' : 'STOP'));
    if (badge.dataset.mode !== mode) { badge.dataset.mode = mode; badge.className = 'hud-badge ' + mode; }
    const bp = tr.running ? barPos(tr.step) : null;
    setText('hud-pos', bp ? 'BAR ' + String(bp[0]).padStart(3, '0') + '  BEAT ' + bp[1] + '  STEP ' + bp[2]
                          : 'BAR ---  BEAT -  STEP -');
    const held = S.held || [];
    setText('hud-held', held.length ? held.slice(0, 8).join('  ') : '-');
    const arp = S.arp || {};
    setText('hud-arp', arp.on ? 'ARP ' + (arp.pattern || '') + ' ' + (arp.rate || '') : 'ARP OFF');
    $('hud-arp').className = 'hud-flag' + (arp.on ? ' on' : '');
    const bb = S.bb || {};
    setText('hud-bb', bb.part ? 'BB ' + (bb.playing ? 'PLAY' : 'STOP') + ' P' + bb.part : '');
    $('hud-bb').className = 'hud-flag' + (bb.playing ? ' on' : '');
  }

  function paintModules() {
    const box = $('hud-mods');
    const order = moduleOrder(S.bindings);
    const key = order.map(o => o.join(':')).join(',');
    if (box.dataset.key === key) return;                  // rebuild only when the bindings change
    box.dataset.key = key;
    box.textContent = '';
    for (const k of Object.keys(modEls)) delete modEls[k];
    for (const [name, chn] of order) {
      const d = document.createElement('div');
      d.className = 'hud-mod';
      const b = document.createElement('b');
      b.textContent = SHORT[name] || name.toUpperCase().slice(0, 7);
      const i = document.createElement('i');
      i.textContent = 'ch ' + chn;
      d.append(b, i);
      box.append(d);
      modEls[name] = d;
    }
  }

  function paintGrid() {
    const cv = $('hud-grid'), tracks = S.tracks || [];
    const W = cv.clientWidth || 300;
    const rows = Math.max(1, tracks.length);
    const rowH = Math.max(12, Math.min(26, Math.floor(300 / rows)));
    const H = rows * rowH, dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.height = H + 'px';
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const labelW = Math.min(64, Math.round(W * 0.2));
    const running = !!(S.transport && S.transport.running);
    const maxlen = Math.max(1, ...tracks.map(t => t.len || 16));
    const cw = (W - labelW) / maxlen;
    g.font = Math.max(9, Math.min(12, rowH - 6)) + 'px "Share Tech Mono", monospace';
    g.textBaseline = 'middle';
    tracks.forEach((t, ri) => {
      const col = TRACK_COLORS[ri % TRACK_COLORS.length], y = ri * rowH;
      g.fillStyle = t.mute ? '#6c7480' : col;
      g.globalAlpha = t.mute ? 0.7 : 0.9;
      g.fillText((t.name || 'trk').slice(0, 7), 0, y + rowH / 2);
      g.globalAlpha = 1;
      const steps = t.steps || '';
      for (let i = 0; i < (t.len || 16); i++) {
        const on = steps[i] === '1', head = running && i === t.pos;
        const x = labelW + i * cw, w = Math.max(2, cw - 2), h = rowH - 3;
        if (on) { g.fillStyle = col; g.globalAlpha = t.mute ? 0.28 : 1; }
        else { g.fillStyle = '#ffffff'; g.globalAlpha = (Math.floor(i / 4) % 2 === 0) ? 0.1 : 0.05; }
        g.fillRect(x, y + 1, w, h);
        if (head) { g.globalAlpha = 0.6; g.fillStyle = '#ffffff'; g.fillRect(x, y + 1, w, h); }
      }
      g.globalAlpha = 1;
    });
  }

  function paintAnim(nowMs) {
    const tr = (S && S.transport) || {};
    const since = (nowMs - recvMs) / 1000 + (tr.age_ms || 0) / 1000;
    const flash = beatFlash(tr, since);
    const cur = tr.running && tr.step >= 0 ? Math.floor((tr.step % 16) / 4) : -1;
    document.querySelectorAll('.hud-lamp').forEach((el, i) => {
      el.style.opacity = i === cur ? (0.25 + 0.75 * flash).toFixed(2) : '0.12';
    });
    for (const [name, el] of Object.entries(modEls)) {
      const lv = pulses.get(name);
      el.style.background = lv > 0.02 ? 'rgba(70,195,240,' + (0.12 + 0.75 * lv).toFixed(2) + ')' : '';
      el.style.color = lv > 0.02 ? '#fff' : '';
      el.style.borderColor = lv > 0.05 ? 'rgba(70,195,240,' + lv.toFixed(2) + ')' : 'transparent';
    }
  }

  function frame(nowMs) {
    raf = 0;
    if (!S) return;
    pulses.update(null, nowMs / 1000);
    paintAnim(nowMs);
    if ((S.transport && S.transport.running) || pulses.active()) raf = requestAnimationFrame(frame);
  }

  function kick() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(frame);
  }

  // ---- data in ---------------------------------------------------------------------
  function apply(s, nowMs) {
    if (!s || typeof s !== 'object') return;
    S = s; recvMs = nowMs; lastMsgMs = nowMs; everMsg = true;
    pulses.update(s.mods, nowMs / 1000);
    paintText(); paintModules(); paintGrid(); paintAnim(nowMs);
    setConn(true);
    kick();
  }

  function setConn(live) {
    const el = $('hud-conn'), root = $('hud-root');
    root.classList.toggle('stale', !live);
    el.classList.toggle('live', live);
    el.textContent = live ? 'LIVE'
      : (everMsg ? 'BRAIN NOT UPDATING' : 'WAITING FOR THE ORC…');
  }

  function checkStale() {
    const now = performance.now();
    setConn(everMsg && now - lastMsgMs < STALE_MS);
  }

  function startStream() {
    stopPoll();
    try { es && es.close(); } catch (e) { /* ignore */ }
    es = new EventSource('/api/hud/stream');
    es.onmessage = e => { try { apply(JSON.parse(e.data), performance.now()); } catch (err) { /* bad frame */ } };
    es.onerror = () => {
      // The browser retries a dropped stream by itself; a REFUSED one (too many HUD
      // streams = 429) is closed for good, so fall back to polling and try again later.
      if (es && es.readyState === 2) { startPoll(); retry = setTimeout(startStream, 15000); }
    };
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      try {
        const d = await fetch('/api/hud').then(r => r.json());
        if (d && d.ok && d.age < 3) apply(d, performance.now());
      } catch (e) { /* the stale check reports it */ }
    }, 500);
  }

  function stopPoll() { if (poll) { clearInterval(poll); poll = 0; } if (retry) { clearTimeout(retry); retry = 0; } }

  function init() {
    setConn(false);
    startStream();
    setInterval(checkStale, 500);
    window.addEventListener('resize', () => { if (S) paintGrid(); });
    // a hidden tab (phone locked / another app) should not hold a stream or burn battery
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { try { es && es.close(); } catch (e) { /* ignore */ } stopPoll(); }
      else { startStream(); kick(); }
    });
  }

  return { barPos, beatFlash, Pulses, moduleOrder, apply, init, _state: () => S };
})();

HUD.init();
