/* sampler.js — Phase 6 — ORC-1 SAMPLER page
   Depends on cmd() and ORC (shared.js) loaded first by base.html.
   No local cmd() override — shared.js handles it. */

'use strict';

// Capture-row tag + status styling (kept here so this stays a one-file feature).
document.head.insertAdjacentHTML('beforeend', `<style>
  .cap-tag{font-size:8px;color:var(--muted);font-family:var(--fh);
    letter-spacing:.08em;align-self:center;min-width:40px;white-space:nowrap}
  .cap-status{font-size:9px;color:var(--accent);font-family:var(--fh);
    align-self:center;margin-left:2px;white-space:nowrap}
</style>`);

// ── DECK GENERATION ───────────────────────────────────────────────────────────
// Each deck gets: grab controls · transport · waveform ·
//                nudge · speed/vol/EQ/filter sliders
const decksEl = document.getElementById('decks');
['A', 'B'].forEach(d => {
  decksEl.insertAdjacentHTML('beforeend', `
    <div class="card amber" id="deck-${d}">
      <div class="card-label">
        DECK ${d}
      </div>

      <div class="flags" style="margin-bottom:6px;align-items:center">
        <span class="cap-tag" title="grabs the audio that already played, ending now">⟲ PAST</span>
        <select id="s${d}" style="background:var(--dim);border:1px solid var(--border);color:var(--bright);font-family:var(--fs);font-size:10px;padding:4px 6px;border-radius:2px">
          <option>4</option><option selected>8</option><option>12</option><option>16</option><option>25</option><option>60</option>
        </select>
        <button class="flag teal"
          onclick="cmd('SAMPLER_CAPTURE',{deck:'${d}',seconds:+document.getElementById('s${d}').value})">GRAB SECS</button>
        ${[1,2,4,8].map(b =>
          `<button class="flag warn"
            onclick="cmd('SAMPLER_CAPTURE_BARS',{deck:'${d}',bars:${b}})"
            >${b} BAR${b>1?'S':''}</button>`).join('')}
      </div>

      <div class="flags" style="margin-bottom:8px;align-items:center">
        <span class="cap-tag" title="press = downbeat; records the NEXT bars, in time">⟳ AHEAD</span>
        ${[1,2,4,8].map(b =>
          `<button class="flag warn"
            onclick="fwdFixed('${d}',${b})"
            >${b} BAR${b>1?'S':''}</button>`).join('')}
        <button class="flag on" id="fwdhold-${d}"
          title="hold to record ahead; release to stop (snaps to whole bars)">● HOLD</button>
        <button class="flag" title="cancel an armed forward capture"
          onclick="cmd('SAMPLER_FWD_CANCEL',{deck:'${d}'});fwdStatus('${d}','cancelled',900)">✕</button>
        <span id="fwdst-${d}" class="cap-status"></span>
      </div>

      <div class="flags" style="margin-bottom:8px">
        <button class="flag accent" title="Play the capture once, then stop"
          onclick="cmd('SAMPLER_AUDITION',{deck:'${d}'})">▶ PLAY</button>
        <button class="flag on" title="Latch a repeating loop (press again / STOP to end)"
          onclick="cmd('SAMPLER_LOOP',{deck:'${d}'})">LOOP ⟳</button>
        <button class="flag" onclick="cmd('SAMPLER_REVERSE',{deck:'${d}'})">REV ◀◀</button>
        <button class="flag" onclick="cmd('SAMPLER_STOP',{deck:'${d}'})">STOP</button>
        <button class="flag purple" onclick="cmd('SAMPLER_EXPORT',{deck:'${d}'})">SAVE → SAMPLES</button>
      </div>

      <div class="flags" style="margin-bottom:8px;align-items:center">
        <span class="cap-tag" title="temporarily shrink the loop to N bars from its start; FULL restores the whole capture">LOOP LEN</span>
        ${[1,2,4,8].map(b =>
          `<button class="flag" onclick="whittle('${d}',${b})">${b} BAR${b>1?'S':''}</button>`).join('')}
        <button class="flag accent" onclick="whittleFull('${d}')">FULL</button>
      </div>

      <div class="flags" style="margin-bottom:8px;align-items:center">
        <span class="cap-tag" title="hold to stutter a subdivision at the playhead; release returns to the loop">BEAT RPT</span>
        <button class="flag warn br-btn" data-deck="${d}" data-beats="1"    title="hold: 1/4-note stutter">1/4</button>
        <button class="flag warn br-btn" data-deck="${d}" data-beats="0.5"  title="hold: 1/8-note stutter">1/8</button>
        <button class="flag warn br-btn" data-deck="${d}" data-beats="0.25" title="hold: 1/16-note stutter">1/16</button>
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
        <span style="font-size:8px;color:var(--label);font-family:var(--fh);letter-spacing:.1em">WAVEFORM</span>
        <span id="wave-len-${d}" style="font-size:8px;color:var(--muted);font-family:var(--fh)">—</span>
      </div>
      <canvas id="wave-${d}" width="560" height="48"
        style="width:100%;height:48px;display:block;background:rgba(0,0,0,.3);border:1px solid var(--border);border-radius:2px;margin-bottom:8px"></canvas>

      <div class="flags" style="margin-bottom:10px">
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',beats:-1})">◀ BEAT</button>
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',ms:-100})">−100ms</button>
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',ms:-10})">−10ms</button>
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',ms:10})">+10ms</button>
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',ms:100})">+100ms</button>
        <button class="flag" onclick="cmd('SAMPLER_NUDGE',{deck:'${d}',beats:1})">BEAT ▶</button>
      </div>

      <div style="display:grid;grid-template-columns:52px 1fr;gap:5px 10px;align-items:center;font-size:9px;color:var(--label);font-family:var(--fh);letter-spacing:.06em">
        <span>SPD</span>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="spd-${d}" type="range" min="0" max="127" value="64" style="flex:1"
            oninput="cmd('SAMPLER_SPEED',{deck:'${d}',value:+this.value})">
          <button class="flag" style="flex:none;font-size:8px;padding:2px 6px"
            title="reset speed to normal (1×)" onclick="samplerSpeedReset('${d}')">1×</button>
        </div>
        <span>VOL</span>
        <input type="range" min="0" max="127" value="96" oninput="cmd('SAMPLER_GAIN',{deck:'${d}',value:+this.value})">
        <span>EQ HI</span>
        <input type="range" min="0" max="127" value="64" title="+/−15dB shelf · 64=flat"
          oninput="cmd('SAMPLER_EQ',{deck:'${d}',band:'hi',value:+this.value})">
        <span>EQ MID</span>
        <input type="range" min="0" max="127" value="64" title="+/−15dB peak · 64=flat"
          oninput="cmd('SAMPLER_EQ',{deck:'${d}',band:'mid',value:+this.value})">
        <span>EQ LOW</span>
        <input type="range" min="0" max="127" value="64" title="+/−15dB shelf · 64=flat"
          oninput="cmd('SAMPLER_EQ',{deck:'${d}',band:'low',value:+this.value})">
        <span>FILTER</span>
        <input type="range" min="0" max="127" value="64" title="64=bypass · &lt;64 LP · &gt;64 HP"
          oninput="cmd('SAMPLER_FILTER',{deck:'${d}',value:+this.value})">
      </div>
    </div>`);
});

// Reset a deck's playback speed to normal (64 = 1×) and snap the slider back.
// The decks have no "back to normal" any other way — the fader has no detent.
function samplerSpeedReset(d) {
  const el = document.getElementById('spd-' + d);
  if (el) el.value = 64;
  cmd('SAMPLER_SPEED', { deck: d, value: 64 });
}

// ── FORWARD CAPTURE (press = downbeat; the sample lands after N bars) ──────────
// Web parity for the Mixtrack forward-grab. The engine schedules a grab of the
// window [press, press+length] using the LIVE tempo — the ORC's own bar phase is
// never used; this click IS the downbeat. HOLD records until release, snapped to
// whole bars engine-side. (The web adds a little click→socket latency the
// controller doesn't, but the model is identical.) SAMPLER_FWD_* is handled by
// the engine cmd dispatcher, so this is a front-end-only feature.
let _samplerBpm = 120;                        // kept fresh by the shared state poll
const _BEATS_BAR = 4;                         // matches engine _SMP_BEATS_BAR default
function _barSecs() {
  return _BEATS_BAR * 60 / Math.max(20, Math.min(300, _samplerBpm));
}

const _fwdTimer = { A: null, B: null };       // per-deck status/countdown timer
function fwdStatus(d, txt, holdMs) {
  const el = document.getElementById('fwdst-' + d);
  if (!el) return;
  el.textContent = txt;
  clearTimeout(_fwdTimer[d]);
  if (holdMs) _fwdTimer[d] = setTimeout(() => { el.textContent = ''; }, holdMs);
}

// Fixed forward grab: fire it, then run a client-side countdown so the player
// sees when it lands (both clocks started at ~the same instant — close enough
// for a readout; the actual capture is scheduled on the engine).
function fwdFixed(d, bars) {
  cmd('SAMPLER_FWD_FIXED', { deck: d, bars });
  const total = bars * _barSecs(), t0 = Date.now();
  const barsTxt = bars + ' bar' + (bars > 1 ? 's' : '');
  clearTimeout(_fwdTimer[d]);
  (function tick() {
    const el = document.getElementById('fwdst-' + d);
    if (!el) return;
    const left = total - (Date.now() - t0) / 1000;
    if (left <= 0) { fwdStatus(d, '✓ captured ' + barsTxt, 1400); return; }
    el.textContent = '⟳ rec ' + barsTxt + ' — ' + left.toFixed(1) + 's';
    _fwdTimer[d] = setTimeout(tick, 100);
  })();
}

// HOLD-to-record: pointerdown marks the downbeat, release finalizes the length.
['A', 'B'].forEach(d => {
  const btn = document.getElementById('fwdhold-' + d);
  if (!btn) return;
  let holdT0 = 0, holdIv = null;
  const start = ev => {
    ev.preventDefault();
    if (holdT0) return;
    holdT0 = Date.now();
    cmd('SAMPLER_FWD_HOLD_START', { deck: d });
    btn.style.boxShadow = '0 0 0 2px var(--accent)';
    clearTimeout(_fwdTimer[d]);
    holdIv = setInterval(() => {
      fwdStatus(d, '● recording — ' + ((Date.now() - holdT0) / 1000).toFixed(1) + 's');
    }, 100);
  };
  const end = ev => {
    if (!holdT0) return;
    if (ev) ev.preventDefault();
    holdT0 = 0;
    clearInterval(holdIv);
    cmd('SAMPLER_FWD_HOLD_END', { deck: d });
    btn.style.boxShadow = '';
    fwdStatus(d, '✓ captured (snapped to bars)', 1500);
  };
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', end);
  btn.addEventListener('pointerleave', end);
  btn.addEventListener('pointercancel', end);
});

// ── MOMENTARY BEAT-REPEAT (stutter) ───────────────────────────────────────────
// Hold a subdivision button to squeeze the play/loop region to a tight loop at
// the live playhead; release restores the region. The loop LENGTH is derived
// from the live tempo engine-side (SAMPLER_BR_ON/OFF are handled by the cmd
// dispatcher), so this is a front-end-only feature: pointerdown = on,
// pointerup/leave/cancel = off. `beats`: 1 = 1/4 note, 0.5 = 1/8, 0.25 = 1/16.
document.querySelectorAll('.br-btn').forEach(btn => {
  const d = btn.dataset.deck, beats = parseFloat(btn.dataset.beats);
  let on = false;
  const start = ev => {
    ev.preventDefault();
    if (on) return;
    on = true;
    cmd('SAMPLER_BR_ON', { deck: d, beats });
    btn.style.boxShadow = '0 0 0 2px var(--accent)';
  };
  const end = ev => {
    if (!on) return;
    if (ev) ev.preventDefault();
    on = false;
    cmd('SAMPLER_BR_OFF', { deck: d });
    btn.style.boxShadow = '';
  };
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', end);
  btn.addEventListener('pointerleave', end);
  btn.addEventListener('pointercancel', end);
});

// ── NON-DESTRUCTIVE WHITTLE (2026-08-18) ──────────────────────────────────────
// Shrink the play/loop region to N bars from its current START, reversibly.
// Reuses SAMPLER_BOUNDS (the same region the drag handles set) — the captured
// buffer is never modified, so FULL brings the whole thing back. Bar length is
// derived from the live tempo (_samplerBpm) and the capture length (view JSON).
function whittle(deck, bars) {
  const v = _lastView[deck], L = v && v.length_s;
  if (!L) return;                                  // no capture yet / unknown length
  const start = _region[deck].start || 0;
  const frac  = Math.min(1 - start, (bars * _barSecs()) / L);
  const end   = Math.min(1, start + frac);
  _region[deck] = { start, end };
  drawWave(deck, v);                               // reflect the new region visually
  cmd('SAMPLER_BOUNDS', { deck, start, end });
}
function whittleFull(deck) {
  _region[deck] = { start: 0, end: 1 };
  if (_lastView[deck]) drawWave(deck, _lastView[deck]);
  cmd('SAMPLER_BOUNDS', { deck, start: 0, end: 1 });
}

// ── FILE DECKS ────────────────────────────────────────────────────────────────
// Browse ~/recordings + samples/, load any audio file onto a deck (cued at 0).
// Press LOOP or GRAB to start; bar-grab its playback = live resampling.
decksEl.insertAdjacentHTML('beforeend', `
  <div class="card blue">
    <div class="card-label">
      <span>FILE DECKS</span>
      <button class="rescan-btn" onclick="loadFileList()">↻ REFRESH</button>
    </div>
    <select id="file-list" class="orc-select" style="width:100%;margin-bottom:8px"></select>
    <div class="flags">
      <button class="flag accent" onclick="loadFileTo('A')">LOAD → A</button>
      <button class="flag accent" onclick="loadFileTo('B')">LOAD → B</button>
      <span id="file-msg" style="font-size:9px;color:var(--muted);align-self:center"></span>
    </div>
    <div id="mt-browse" style="font-size:9px;color:var(--muted);font-family:var(--fh);margin-top:6px;letter-spacing:.06em"></div>
  </div>`);

async function loadFileList() {
  try {
    const r = await fetch('/api/sampler/files?' + Date.now());
    const d = await r.json();
    const sel = document.getElementById('file-list');
    sel.innerHTML = (d.files || []).map(f =>
      `<option value="${f.path}">${f.name} (${f.mb}MB)</option>`).join('')
      || '<option value="">no files found</option>';
  } catch (e) {}
}

async function loadFileTo(deck) {
  const sel = document.getElementById('file-list');
  if (!sel.value) return;
  await cmd('SAMPLER_LOAD_FILE', { deck, path: sel.value });
  const m = document.getElementById('file-msg');
  m.textContent = 'loading → ' + deck + '… (waveform appears when decoded)';
  setTimeout(() => { m.textContent = ''; }, 6000);
}

loadFileList();

// ── WAVEFORM VIEWS + START/END REGION (trim = loop) ───────────────────────────
// The synth writes ~/.orc1_sampler_view_<deck>.json on every capture/nudge,
// including the current start/end fractions. We poll it (not /state) at 1s.
// The two draggable handles set ONE play/loop region: PLAY/one-shot runs
// start→end, LOOP wraps start→end. Dragging sends SAMPLER_BOUNDS (which drives
// the same CH_SAMPLER CCs a mapped controller would).
const _waveTs   = { A: 0, B: 0 };
const _lastView = { A: null, B: null };            // last payload, for redraws mid-drag
const _region   = { A: { start: 0, end: 1 }, B: { start: 0, end: 1 } };
const _drag     = { A: null, B: null };            // 'start' | 'end' while dragging
let   _boundsSendT = 0;

function drawWave(deck, view) {
  const cv = document.getElementById('wave-' + deck);
  if (!cv || !view || !view.peaks) return;
  const ctx = cv.getContext('2d'), w = cv.width, h = cv.height, n = view.peaks.length;
  const reg = _region[deck];
  const s = Math.max(0, Math.min(1, reg.start)) * w;
  const e = Math.max(0, Math.min(1, reg.end))   * w;
  ctx.clearRect(0, 0, w, h);
  // waveform
  const col = getComputedStyle(document.body).getPropertyValue('--warn').trim() || '#e0a000';
  ctx.strokeStyle = col;
  ctx.lineWidth = Math.max(1, w / n - 1);
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = (i + 0.5) * w / n;
    const lo = Math.max(-1, Math.min(1, view.peaks[i][0]));
    const hi = Math.max(-1, Math.min(1, view.peaks[i][1]));
    ctx.moveTo(x, h / 2 - hi * (h / 2 - 1));
    ctx.lineTo(x, h / 2 - lo * (h / 2 - 1) + 0.5);
  }
  ctx.stroke();
  // dim the trimmed-off parts (outside [start,end]) OVER the waveform
  ctx.fillStyle = 'rgba(0,0,0,.5)';
  if (s > 0) ctx.fillRect(0, 0, s, h);
  if (e < w) ctx.fillRect(e, 0, w - e, h);
  // the two region handles
  const acc = getComputedStyle(document.body).getPropertyValue('--accent').trim() || '#3fb6ff';
  ctx.strokeStyle = acc; ctx.fillStyle = acc; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(s, 0); ctx.lineTo(s, h); ctx.stroke();
  ctx.fillRect(s, 0, 4, 7);                       // start tab (top, rightward)
  ctx.beginPath(); ctx.moveTo(e, 0); ctx.lineTo(e, h); ctx.stroke();
  ctx.fillRect(e - 4, h - 7, 4, 7);               // end tab (bottom, leftward)
  const lbl = document.getElementById('wave-len-' + deck);
  if (lbl) {
    const L = view.length_s || 0;
    lbl.textContent = L.toFixed(2) + 's  ·  region '
      + (reg.start * L).toFixed(2) + '–' + (reg.end * L).toFixed(2) + 's';
  }
}

// Fraction (0..1) of the canvas width at a pointer x, resolution-independent
// (the canvas is 560px internally but CSS-stretched to 100%).
function _regionFromX(cv, clientX) {
  const r = cv.getBoundingClientRect();
  return Math.max(0, Math.min(1, (clientX - r.left) / r.width));
}

function _sendBounds(deck, final) {
  const now = Date.now();
  if (!final && now - _boundsSendT < 60) return;   // throttle the drag stream
  _boundsSendT = now;
  const reg = _region[deck];
  cmd('SAMPLER_BOUNDS', { deck, start: reg.start, end: reg.end });
}

function _applyDrag(deck, f) {
  const reg = _region[deck], GAP = 0.01;           // keep a minimal region
  if (_drag[deck] === 'start') reg.start = Math.max(0, Math.min(f, reg.end - GAP));
  else                         reg.end   = Math.min(1, Math.max(f, reg.start + GAP));
  if (_lastView[deck]) drawWave(deck, _lastView[deck]);
  _sendBounds(deck, false);
}

['A', 'B'].forEach(deck => {
  const cv = document.getElementById('wave-' + deck);
  if (!cv) return;
  cv.style.cursor = 'ew-resize';
  cv.style.touchAction = 'none';                   // let pointer drags own the gesture
  cv.addEventListener('pointerdown', ev => {
    const f = _regionFromX(cv, ev.clientX), reg = _region[deck];
    const ds = Math.abs(f - reg.start), de = Math.abs(f - reg.end);
    if (Math.min(ds, de) > 0.06) return;           // click wasn't near a handle
    _drag[deck] = ds <= de ? 'start' : 'end';
    cv.setPointerCapture(ev.pointerId);
    _applyDrag(deck, f);
    ev.preventDefault();
  });
  cv.addEventListener('pointermove', ev => {
    if (_drag[deck]) _applyDrag(deck, _regionFromX(cv, ev.clientX));
  });
  const endDrag = () => {
    if (!_drag[deck]) return;
    _drag[deck] = null;
    _sendBounds(deck, true);                        // always send the final position
  };
  cv.addEventListener('pointerup', endDrag);
  cv.addEventListener('pointercancel', endDrag);
});

async function pollWaves() {
  for (const d of ['A', 'B']) {
    try {
      const r = await fetch('/api/sampler/view/' + d + '?' + Date.now());
      if (r.ok) {
        const v = await r.json();
        _lastView[d] = v;
        if (v.ts !== _waveTs[d]) {
          _waveTs[d] = v.ts;
          // A new capture/nudge/load: adopt the region the engine reports (a
          // fresh GRAB resets it to the whole buffer). Never yank it out from
          // under an in-progress drag.
          if (!_drag[d]) {
            _region[d] = { start: (v.start != null ? v.start : 0),
                           end:   (v.end   != null ? v.end   : 1) };
          }
          drawWave(d, v);
        }
      }
    } catch (e) {}
  }
  setTimeout(pollWaves, 1000);
}
pollWaves();

// ── STATE SUBSCRIBER ──────────────────────────────────────────────────────────
// Piggybacks on the shared ORC poll loop — no duplicate timer.
// Updates the Mixtrack browse-position readout.
ORC.subscribe(function(d) {
  if (d.arp && typeof d.arp.bpm === 'number') _samplerBpm = d.arp.bpm;
  if (d.sampler_browse) {
    const el = document.getElementById('mt-browse');
    if (el) el.textContent = 'ENCODER  A → ' + (d.sampler_browse.A || '—')
                           + '   ·   B → ' + (d.sampler_browse.B || '—');
  }
});

// ── SKIPBACK SOURCE CARD ──────────────────────────────────────────────────────
// "What does the ring buffer (and thus the sampler) hear?"
// Presets: SYNTH DIRECT = ORC1-Synth:out_L/R (mixer-independent, always audible).
//          L12 MASTER   = system:capture_13/14 (hears external instruments too,
//                         but requires L12 master fader up and post-fader).
// Free pickers: any JACK output port pair.
async function loadSkipback() {
  const l = document.getElementById('sb-l'),
        r = document.getElementById('sb-r'),
        st = document.getElementById('sb-status');
  if (!l) return;
  try {
    const d = await fetch('/api/skipback/source').then(x => x.json());
    const opts = sel => d.sources.map(p =>
      `<option${sel.includes(p) ? ' selected' : ''}>${p}</option>`).join('');
    l.innerHTML = opts(d.left);
    r.innerHTML = opts(d.right);
    if (st) st.textContent = 'now: ' + (d.left[0] || '—') + ' + ' + (d.right[0] || '—');
  } catch (e) {
    if (st) st.textContent = 'api unavailable';
  }
}

async function applySkipback(left, right) {
  const l = left  || document.getElementById('sb-l').value;
  const r = right || document.getElementById('sb-r').value;
  try {
    await fetch('/api/skipback/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ left: l, right: r })
    });
  } catch (e) {}
  loadSkipback();
}

function skipbackPreset(which) {
  if (which === 'synth') applySkipback('ORC1-Synth:out_L', 'ORC1-Synth:out_R');
  else                   applySkipback('system:capture_13', 'system:capture_14');
}

loadSkipback();


// ── ONE-SHOT SAMPLE BANK (relocated from the drum channel 2026-07-21) ──────────
// Triggered/sculpted on CH_SAMPLER via the generic SYNTH_NOTE/SYNTH_CC cmds:
// note (note_base+i) triggers slot i; CC ccs[param] sculpts it. Read-back comes
// from state.synth_cc[ch], same as the /drums and /synth pages. No dedicated
// facade needed — the synth's _route_sampler maps these to the smpl_<i> voices.
const OS_PLAB = {level:'LEVEL', tune:'TUNE', attack:'ATTACK', decay:'DECAY',
                 filter_freq:'FREQ', filter_res:'RES'};
let _osCfg = null;          // {ch, params, slots:[{i,note,name,loaded,ccs}]}
let _osSynthCC = {};

function _osVal(cc) {
  const c = _osSynthCC[String(_osCfg ? _osCfg.ch : 7)];
  return (c && c[String(cc)] !== undefined) ? c[String(cc)] : 64;
}
// _osCfg.ch = the drums module's LIVE channel; null = drums unbound on /matrix
// -> send nothing (it used to be the shipped CH_DRUM, 2026-10-01).
function osTrigger(note) {
  if (_osCfg && _osCfg.ch) cmd('SYNTH_NOTE', {ch: _osCfg.ch, note, vel: 110, ms: 120});
}
function osCC(cc, value, valId) {
  if (valId) { const el = document.getElementById(valId); if (el) el.textContent = value; }
  if (_osCfg && _osCfg.ch) cmd('SYNTH_CC', {ch: _osCfg.ch, cc, value});
}

async function loadOneshot() {
  const host = document.getElementById('oneshot-slots');
  if (!host) return;
  try {
    _osCfg = await fetch('/api/oneshot').then(r => r.json());
  } catch (e) { return; }
  const chSpan = document.getElementById('os-ch');
  if (chSpan) chSpan.textContent = _osCfg.ch || 'NOT BOUND';
  const noteSpan = document.getElementById('os-note');
  if (noteSpan && _osCfg.note_base != null) noteSpan.textContent = _osCfg.note_base;
  host.innerHTML = _osCfg.slots.map(s => {
    const label = s.loaded ? s.name : 'empty';
    const rows = _osCfg.params.map(p => {
      const cc  = s.ccs[p];
      const id  = `os-${s.i}-${p}`, vid = `osv-${s.i}-${p}`;
      const lab = OS_PLAB[p] || p.toUpperCase();
      return `<div class="prow">
        <span class="plab">${lab}</span>
        <input type="range" min="0" max="127" value="64" id="${id}"
               oninput="osCC(${cc},+this.value,'${vid}')">
        <span class="pval" id="${vid}">64</span>
      </div>`;
    }).join('');
    return `<details class="voice-panel">
      <summary class="voice-hdr">
        <span class="voice-name">SLOT ${s.i}</span>
        <span class="ch-tag" style="background:var(--amber-bg);border-color:var(--amber);color:var(--amber)">${label}</span>
        <button class="trig-btn" onclick="event.stopPropagation();osTrigger(${s.note})">▶</button>
      </summary>
      <div class="voice-body"><div class="param-list" style="margin-top:10px">${rows}</div></div>
    </details>`;
  }).join('');
}

ORC.subscribe(function(state) {
  _osSynthCC = state.synth_cc || {};
  if (!_osCfg) return;
  _osCfg.slots.forEach(s => {
    _osCfg.params.forEach(p => {
      const cc = s.ccs[p];
      const el = document.getElementById(`os-${s.i}-${p}`);
      if (!el || el === document.activeElement) return;
      const v = _osVal(cc);
      el.value = v;
      const vd = document.getElementById(`osv-${s.i}-${p}`);
      if (vd) vd.textContent = v;
    });
  });
});

loadOneshot();


// ── PER-DECK GRAB SOURCE (2026-09-15) ─────────────────────────────────────────
// Each deck grabs from its OWN skipback ring, so you can sample two live streams
// at once. Deck A defaults to MAIN (track 0), deck B to track 1. This picks WHICH
// ring each deck grabs; what a ring RECORDS is set on /skipback. Repoint goes over
// the cmd socket as SAMPLER_SET_SOURCE, mirroring every other sampler control.
let _deckSrcCache = null;
// RINGS card (2026-09-23, replaces CAPTURE SOURCE): every ARMED skipback ring up
// front on this page -- point a deck at it, or save its last N seconds to a file
// (same /api/skipback/tracks/<id>/grab the /skipback page uses). Mirrors the
// Mixtrack: FX1/2/3 = the 1st/2nd/3rd armed ring per deck, SHIFT+FX saves. Kept
// at the TOP of the page; re-polled so a Mixtrack pick shows here too.
decksEl.insertAdjacentHTML('afterbegin', `
  <div class="card">
    <div class="card-label"><span>RINGS · GRAB FROM / SAVE</span>
      <a href="/skipback" style="font-size:9px;color:var(--accent);text-decoration:none">add / edit rings ›</a>
    </div>
    <div style="font-size:9px;color:var(--muted);margin-bottom:6px">
      <b>DECK A / B</b>: that deck grabs from this ring (lit = current).
      <b>SAVE</b>: write the ring's last N seconds to ~/recordings.
      Mixtrack: FX1-3 pick the ring for a deck; SHIFT+FX saves it.
    </div>
    <div id="deck-src-rows" style="display:flex;flex-direction:column;gap:10px"></div>
  </div>`);

function _deckSrcInfo(tid) {
  const t = _deckSrcCache && (_deckSrcCache.tracks || []).find(x => x.id === tid);
  if (!t) return 'track ' + tid;
  return (t.source ? 'records ' + t.source : 'no source assigned')
         + (t.recording ? '' : ' · NOT recording');
}
function _esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function _deckSrcRender() {
  const rows = document.getElementById('deck-src-rows');
  if (!rows || !_deckSrcCache) return;
  const cur = _deckSrcCache.decks || { A: 0, B: 1 };
  // same order as the Mixtrack's FX1-3 (orc1.skipback_rings): RAM rings first
  // -- a grab from a CARD ring can land early when the SD card stalls -- then
  // CARD, each by id
  const rings = (_deckSrcCache.tracks || []).filter(t => t.armed)
                  .sort((a, b) => ((a.store === 'card') - (b.store === 'card')) || (a.id - b.id));
  if (!rings.length) {
    rows.innerHTML = '<div style="font-size:10px;color:var(--muted)">no armed rings — add one on the skipback page</div>';
    return;
  }
  rows.innerHTML = rings.map((t, i) => `
    <div>
      <div class="card-label" style="margin:0">
        <span>${i < 3 ? 'FX' + (i + 1) + ' · ' : ''}${_esc(t.name)}
          <span style="color:var(--muted)">(${t.stereo ? 'stereo' : 'mono'}, ${t.store === 'card' ? 'CARD: grabs may land early' : 'RAM'})</span></span>
        <span style="font-size:9px;color:${t.recording ? 'var(--muted)' : 'var(--danger)'}">
          ${_esc(_deckSrcInfo(t.id))}</span>
      </div>
      <div class="flags" style="margin-top:3px">
        ${['A', 'B'].map(d => `<button class="flag ${cur[d] === t.id ? 'on' : ''}"
           onclick="deckSrcSet('${d}', ${t.id})">DECK ${d}</button>`).join('')}
        <span style="width:8px"></span>
        ${[[30, '30s'], [60, '60s'], [300, '5m'], [600, '10m']].map(([s, l]) =>
          `<button class="flag" onclick="ringSave(${t.id}, ${s})">SAVE ${l}</button>`).join('')}
      </div>
      <div id="ring-save-${t.id}" style="font-size:9px;color:var(--accent);min-height:11px"></div>
    </div>`).join('');
}
function deckSrcSet(deck, track) {
  const tid = parseInt(track, 10);
  if (isNaN(tid)) return;
  cmd('SAMPLER_SET_SOURCE', { deck: deck, track: tid });
  if (_deckSrcCache && _deckSrcCache.decks) _deckSrcCache.decks[deck] = tid;
  _deckSrcRender();
}
async function ringSave(tid, secs) {
  const el = document.getElementById('ring-save-' + tid);
  if (el) { el.style.color = 'var(--accent)'; el.textContent = 'saving last ' + secs + 's…'; }
  try {
    const r = await fetch('/api/skipback/tracks/' + tid + '/grab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: secs }) });
    const d = await r.json();
    if (!el) return;
    if (d.ok) { el.textContent = 'saved ' + d.file; }
    else { el.style.color = 'var(--danger)'; el.textContent = 'save failed: ' + (d.error || r.status); }
  } catch (e) {
    if (el) { el.style.color = 'var(--danger)'; el.textContent = 'save failed: ' + e; }
  }
}
async function _deckSrcLoad() {
  try {
    _deckSrcCache = await fetch('/api/sampler/deck_source?' + Date.now()).then(r => r.json());
    _deckSrcRender();
  } catch (e) {}
}
_deckSrcLoad();
setInterval(_deckSrcLoad, 5000);     // follow picks made on the Mixtrack

// ── PRESS-ANCHORED CAPTURE TRIM (2026-08-18) ──────────────────────────────────
// The bar-grab now ends at YOUR press (the downbeat), not the ORC grid. This
// trims where that end lands: 0 = the newest recorded audio, negative reaches
// back. Source-dependent (L12 vs synth-direct differ), so tune by ear until a
// fresh grab loops in the pocket. Persisted synth-side (SAMPLER_PRESS_TRIM).
let _pressTrimMs = 0;
let _onsetSnap = true;
let _capAnchor = 'press';
decksEl.insertAdjacentHTML('beforeend', `
  <div class="card">
    <div class="card-label"><span>CAPTURE TIMING</span>
      <span id="ptrim-src" style="font-size:9px;color:var(--muted)"></span>
    </div>
    <div style="font-size:9px;color:var(--muted);margin-bottom:6px">
      <b>ONSET SNAP</b> moves the loop start onto the nearest downbeat transient.
      <b>PRESS TRIM</b> offsets where a grab ends (0 = newest audio, negative
      reaches back); it is kept per source. <b>ANCHOR</b>: PRESS ends the grab at
      your press; GRID ends it on the ORC barline (suits material clocked to the
      ORC, e.g. BeatBuddy).
    </div>
    <div class="flags" style="margin-bottom:6px">
      <button class="flag" id="onset-btn" onclick="onsetToggle()">ONSET SNAP</button>
      <button class="flag" id="anchor-press" onclick="setAnchor('press')">ANCHOR: PRESS</button>
      <button class="flag" id="anchor-grid" onclick="setAnchor('grid')">ANCHOR: GRID</button>
    </div>
    <div class="card-label" style="margin-top:2px"><span>PRESS TRIM</span>
      <span id="ptrim-val" style="font-family:var(--fh);color:var(--accent)">0 ms</span>
    </div>
    <div class="flags">
      <button class="flag" onclick="pressTrim(-10)">−10 ms</button>
      <button class="flag" onclick="pressTrim(-1)">−1 ms</button>
      <button class="flag" onclick="pressTrim(1)">+1 ms</button>
      <button class="flag" onclick="pressTrim(10)">+10 ms</button>
      <button class="flag accent" onclick="pressTrimSet(0)">0 ms · NEWEST</button>
    </div>
  </div>`);

function _prettySource(k) {
  if (!k || k === '__default__') return 'source: default';
  return 'source: ' + k.replace(/,/g, ' + ');
}
function _pressTrimShow() {
  const el = document.getElementById('ptrim-val');
  if (el) el.textContent = _pressTrimMs + ' ms';
}
function pressTrimSet(ms) {
  _pressTrimMs = Math.max(-127, Math.min(0, Math.round(ms)));
  _pressTrimShow();
  cmd('SAMPLER_PRESS_TRIM', { ms: _pressTrimMs });
}
function pressTrim(delta) { pressTrimSet(_pressTrimMs + delta); }

function _onsetShow() {
  const b = document.getElementById('onset-btn');
  if (b) { b.textContent = 'ONSET SNAP: ' + (_onsetSnap ? 'ON' : 'OFF');
           b.classList.toggle('accent', _onsetSnap); }
}
function onsetToggle() {
  _onsetSnap = !_onsetSnap;
  _onsetShow();
  cmd('SAMPLER_ONSET_SNAP', { on: _onsetSnap });
}
function _anchorShow() {
  const p = document.getElementById('anchor-press');
  const g = document.getElementById('anchor-grid');
  if (p) p.classList.toggle('accent', _capAnchor === 'press');
  if (g) g.classList.toggle('accent', _capAnchor === 'grid');
}
function setAnchor(mode) {
  _capAnchor = (mode === 'grid') ? 'grid' : 'press';
  _anchorShow();
  cmd('SAMPLER_ANCHOR', { mode: _capAnchor });
}

(async () => {
  try {
    const d = await fetch('/api/sampler/press_trim?' + Date.now()).then(r => r.json());
    if (typeof d.ms === 'number') {
      _pressTrimMs = Math.max(-127, Math.min(0, Math.round(d.ms)));
      _pressTrimShow();
    }
    if (typeof d.onset_snap === 'boolean') _onsetSnap = d.onset_snap;
    const s = document.getElementById('ptrim-src');
    if (s) s.textContent = _prettySource(d.source);
  } catch (e) {}
  _onsetShow();
  _anchorShow();
})();
