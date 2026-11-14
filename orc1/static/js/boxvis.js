/* ORC-1 · boxvis.js -- the BOX tab on /visuals: the MXV box's beat-locked visuals.
   Every control -> SYNTH_CC on the channel the "mxvbox" module is bound to (found by
   NAME from /api/mxvbox, never a fixed channel), the same path a Fire knob takes. The
   knob table + choice names come from mxvbox/pi/mxvbox_knobs.py via /api/mxvbox; the
   conductor (orc1-boxvis) saves the values, which the page reads back. */
'use strict';

const BOX = { ch: 0, values: {}, knobs: {}, styles: [], lines: [], kaleido: [], scenes: [null], touched: 0, built: false, pend: {},
              pools: [], poolsT: 0, clips: null, playing: null,
              flags: { fav: [], never: [] }, flagsT: 0, queued: null, queuedT: 0 };
// the five-way knobs: a button sends the middle of its fifth (0-25 / 26-50 / ...)
const FIFTH_MID = [12, 38, 63, 89, 115];
const fifth = v => Math.max(0, Math.min(4, Math.floor(v * 5 / 128)));
const STYLE_LABEL = { blur: 'BLUR', rgb: 'RGB SPLIT', invert: 'INVERT FLASH', zoom: 'ZOOM PUNCH', warp: 'WARP',
                      shudder: 'SHUDDER', grey: 'GREY (COLOUR DRAINS)', hue: 'HUE JUMP', off: 'OFF', kaleido: 'KALEIDOSCOPE SNAP' };
// KICK / SNARE STYLE are tenths of 0-127 (mxvbox_knobs.REACTIONS); a choice sends the middle of its tenth
const reactionOf = v => Math.max(0, Math.min(BOX.styles.length - 1, Math.floor(v * BOX.styles.length / 128)));
const reactionMid = i => Math.floor(i * 128 / BOX.styles.length + 6);

function boxSelect(id, cc) {
  document.getElementById(id).innerHTML =
    `<select id="${id}-sel" onchange="boxSend(${cc}, this.value)" style="min-width:14em">` +
    BOX.styles.map((s, i) => `<option value="${reactionMid(i)}">${STYLE_LABEL[s] || s.toUpperCase()}</option>`).join('') +
    '</select>';
}
const lineLabel = l => !l ? 'OFF' : l[0] === 2 ? 'EVERY OTHER ROW' : `${l[1]} ROW${l[1] > 1 ? 'S' : ''} IN ${l[0]}`;
// slider groups: [cc, value -> label]; each group sits under its own heading (box-<group>)
const pct127 = v => Math.round(v / 127 * 100) + ' %';
const SLIDER_GROUPS = {
  // (2026-11-03 regroup: the old AMOUNTS grab-bag split up -- each slider sits with the thing it adjusts)
  'box-pic-sliders': [
    [38, v => v ? (v / 127 * 2).toFixed(2) + ' s' : 'HARD CUT'],      // CROSSFADE
    [27, pct127],                                             // WEAVE
    [26, v => Math.round(v / 64 * 100) + ' %'],               // WOBBLE
  ],
  'box-hit-sliders': [
    [28, v => Math.round(v / 64 * 100) + ' %'],               // HIT SIZE
    [20, v => (0.05 + v / 127 * 1.45).toFixed(2) + ' s'],   // HIT FADE
  ],
  'box-scene-sliders': [
    [40, v => v ? (v / 64).toFixed(2) + ' cycles / bar' : 'STILL'],  // SCENE SPEED
    [41, pct127],                                                     // SCENE DETAIL
    [42, v => v ? Math.round(v / 127 * 100) + ' % video' : 'DRAWN'],  // SCENE VIDEO
    [44, v => v ? Math.round(v / 127 * 100) + ' % clip on top' : 'OFF'],  // SCENE OVERLAY
  ],
  'box-mask-sliders': [
    [46, pct127],                                                     // MASK SIZE
    [48, v => v ? (v / 127).toFixed(2) + ' turns / bar' : 'STILL'],  // MASK MOVE
    [49, v => v ? Math.round(v / 127 * 100) + ' % breathing' : 'STEADY'],   // MASK PULSE
    [50, v => v ? Math.round(v / 127 * 100) + ' % open' : 'OFF'],   // MASK HIT
  ],
  'box-frame-sliders': [
    [52, pct127],                                                     // FRAME SIZE
    [53, pct127],                                                     // FRAME X
    [54, pct127],                                                     // FRAME Y
    [55, v => v ? Math.round(v / 127 * 100) + ' % soft' : 'HARD EDGE'],   // FRAME SOFT
  ],
  'box-kal-sliders': [
    [32, v => v ? (v / 127).toFixed(2) + ' turns / bar' : 'STILL'],   // SPIN
  ],
  'box-colour-sliders': [
    [33, v => (32 * (v / 127) ** 2).toFixed(1) + ' px'],             // RGB SPLIT (curved, to 32 px)
    [34, v => Math.round(v / 128 * 360) + '°'],                       // HUE
    [35, v => Math.round(v / 64 * 100) + ' %'],                       // SATURATION
    [36, pct127],                                                     // INVERT
    [37, v => v ? (v / 127).toFixed(2) + ' turns / bar' : 'STILL'],   // HUE CYCLE
  ],
};
const SLIDERS = Object.values(SLIDER_GROUPS).flat();

function boxSend(cc, value) {
  BOX.touched = Date.now();
  BOX.values[cc] = +value;
  boxShow();
  if (!BOX.ch) return;
  const p = BOX.pend[cc];
  BOX.pend[cc] = { value: +value, timer: p && p.timer };
  if (!p || !p.timer) {                      // throttled: a drag reaches the box live, ~25/s
    BOX.pend[cc].timer = setTimeout(() => {
      const q = BOX.pend[cc]; BOX.pend[cc] = null;
      cmd('SYNTH_CC', { ch: BOX.ch, cc, value: q.value });
    }, 40);
  }
}

function boxPicker(id, cc, labels, values) {
  document.getElementById(id).innerHTML = labels.map((l, i) =>
    `<button class="flag" data-v="${values[i]}" onclick="boxSend(${cc}, ${values[i]})">${l}</button>`).join('');
}

function boxBuild() {
  boxPicker('box-show', 7, ['ON', 'BLACKOUT'], [127, 0]);
  // WHEN STOPPED (CC 70, thirds): what the box does when the ORC stops (or its beat feed goes quiet)
  boxPicker('box-stopped', 70, ['WHEN STOPPED: BLACK', 'HOLD THE PICTURE', 'FREE-RUN (KEEPS CHANGING)'], [21, 64, 107]);
  boxSelect('box-kick', 21);
  boxSelect('box-snare', 22);
  boxSelect('box-listen', 58);
  boxSelect('box-listen2', 59);
  boxPicker('box-lines', 23, BOX.lines.map(lineLabel), FIFTH_MID);
  boxPicker('box-lfill', 24, ['BLACK', 'BLURRED PICTURE'], [0, 100]);
  boxPicker('box-clips', 25, ['PATTERNS ONLY', '1 IN 4', '2 IN 4', '3 IN 4', 'CLIPS ONLY'], [0, 32, 64, 95, 127]);
  boxPicker('box-every', 29, ['EVERY BAR', '2 BARS', '4 BARS', '8 BARS'], [16, 48, 80, 112]);
  boxPicker('box-kal', 31, BOX.kaleido.map(n => n ? `${n} MIRRORS` : 'OFF'), FIFTH_MID);
  boxPicker('box-scene', 39, BOX.scenes.map(n => n ? n.toUpperCase() : 'PATTERNS'),
            BOX.scenes.map((_, i) => Math.floor(i * 128 / BOX.scenes.length + 9)));
  boxPicker('box-scene-cycle', 43, ['NO CYCLE', 'CYCLE EVERY BAR', 'EVERY 2 BARS', 'EVERY 4 BARS', 'EVERY 8 BARS'], FIFTH_MID);
  // MASK: an n-way knob's button sends the middle of its slice
  const mids = n => Array.from({ length: n }, (_, i) => Math.floor((i + 0.5) * 128 / n));
  const maskLabel = n => !n ? 'NO MASK' : n === 'bright' ? 'BRIGHT PARTS' : n === 'dark' ? 'DARK PARTS' : n.toUpperCase();
  boxPicker('box-mask', 45, BOX.maskShapes.map(maskLabel), mids(BOX.maskShapes.length));
  boxPicker('box-mask-fill', 47, BOX.maskFills.map(n => n === 'clip' ? 'ANOTHER CLIP' : n.toUpperCase()), mids(BOX.maskFills.length));
  // FRAME: the projected shape (the light only inside it, or that shape blocked out)
  const frameLabel = n => !n ? 'WHOLE SCREEN' : n === 'rounded' ? 'ROUNDED SQUARE' : n === 'picture' ? 'YOUR PICTURE' : n.toUpperCase();
  boxPicker('box-frame', 51, BOX.frameShapes.map(frameLabel), mids(BOX.frameShapes.length));
  boxPicker('box-frame-inv', 56, ['LIGHT INSIDE (SPOTLIGHT)', 'BLOCK IT OUT'], [20, 100]);
  boxPicker('box-frame-pic', 57, ['PICTURE 1', 'PICTURE 2', 'PICTURE 3', 'PICTURE 4'], mids(4));
  boxFramePics();
  // ROTATE: which looks the box picks, bar by bar
  boxPicker('box-rot', 61, ['NO ROTATION', 'NEW PICK EVERY BAR', 'EVERY 2 BARS', 'EVERY 4 BARS', 'EVERY 8 BARS'], FIFTH_MID);
  boxPicker('box-rot-n', 62, ['1 LOOK AT A TIME', '2 AT A TIME', '3 AT A TIME'], mids(3));
  document.getElementById('box-rot-looks').innerHTML = ROT_LOOKS.map(([cc, n]) =>
    `<button class="flag" data-cc="${cc}" onclick="boxSend(${cc}, (BOX.values[${cc}] ?? 127) >= 64 ? 0 : 127)">${n}</button>`).join('');
  boxPicker('box-rot-hits', 76, ['KICK + SNARE STAY AS SET', 'THEY ROTATE TOO'], [20, 100]);
  for (const [id, group] of Object.entries(SLIDER_GROUPS)) {
    document.getElementById(id).innerHTML = group.map(([cc]) => {
      const k = BOX.knobs[cc] || {};
      return `<div class="vrow"><span class="vl">${k.name || cc}</span>
        <input type="range" min="0" max="127" id="bk-${cc}" value="${k.default ?? 64}"
               oninput="boxSend(${cc}, this.value)" ondblclick="this.value=${k.default ?? 64}; boxSend(${cc}, ${k.default ?? 64})">
        <span class="vv" id="bv-${cc}"></span></div>
        <div class="vhint">${(k.help || '').replace(/</g, '&lt;')}</div>`;
    }).join('');
  }
  document.getElementById('box-midi').innerHTML = Object.values(BOX.knobs)
    .map(k => `<b>${k.cc}</b> ${k.name.toLowerCase()}`).join(' · ') + ' · <b>notes</b> cue clip N';
  BOX.built = true;
}

// (RGB .. MASK were CC 64-68, ROTATE HITS 69, until 2026-11-03: the sustain / pedal CCs)
const ROT_LOOKS = [[63, 'KALEIDO'], [71, 'RGB SPLIT'], [72, 'HUE'], [73, 'INVERT'], [74, 'WOBBLE'], [75, 'MASK']];

// FRAME pictures: 4 slots, each a preview + an upload (white = the projector's light, black = dark)
function boxFramePics() {
  const el = document.getElementById('box-frame-pics');
  if (!el) return;
  el.innerHTML = [1, 2, 3, 4].map(n => `<div class="framepic">
      <div class="vl">PICTURE ${n}</div>
      <img id="bfp-img-${n}" alt="" style="width:160px;height:90px;background:#000;display:block;object-fit:contain"
           src="/api/mxvbox/frame/${n}?t=${Date.now()}" onerror="this.style.opacity=0.25">
      <input type="file" accept="image/*,.svg" onchange="boxFrameUpload(${n}, this)">
      <div class="vhint" id="bfp-msg-${n}"></div></div>`).join('');
}

async function boxFrameUpload(n, input) {
  const f = input.files && input.files[0], msg = document.getElementById('bfp-msg-' + n);
  if (!f) return;
  msg.textContent = 'sending...';
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/api/mxvbox/frame/' + n, { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    msg.textContent = d.ok ? 'on the box: pick YOUR PICTURE + PICTURE ' + n : (d.error || 'failed: ' + r.status);
    const img = document.getElementById('bfp-img-' + n);
    img.style.opacity = 1;
    img.src = '/api/mxvbox/frame/' + n + '?t=' + Date.now();
  } catch (e) {
    msg.textContent = 'could not send: ' + e.message;
  }
  input.value = '';
}

function boxShow() {
  const v = BOX.values;
  const mark = (id, test) => document.querySelectorAll(`#${id} .flag`).forEach(b => b.classList.toggle('on', test(+b.dataset.v)));
  mark('box-show', x => (x > 0) === ((v[7] ?? 127) > 0));
  const third = x => Math.floor(x * 3 / 128);
  mark('box-stopped', x => third(x) === third(v[70] ?? 0));
  // LINES: the blurred fill uses other row patterns (mxvbox_knobs.LINE_LOOKS_BLUR): label what plays
  const looks = (v[24] ?? 0) >= 64 && BOX.linesBlur ? BOX.linesBlur : BOX.lines;
  document.querySelectorAll('#box-lines .flag').forEach((b, i) => { b.textContent = lineLabel(looks[i]); });
  for (const [id, cc] of [['box-kick', 21], ['box-snare', 22], ['box-listen', 58], ['box-listen2', 59]]) {
    const sel = document.getElementById(id + '-sel');
    if (sel && document.activeElement !== sel) sel.value = reactionMid(reactionOf(v[cc] ?? 0));
  }
  mark('box-lines', x => fifth(x) === fifth(v[23] ?? 0));
  mark('box-lfill', x => (x >= 64) === ((v[24] ?? 0) >= 64));
  mark('box-clips', x => Math.round(x / 127 * 4) === Math.round((v[25] ?? 0) / 127 * 4));
  mark('box-every', x => Math.min(3, x >> 5) === Math.min(3, (v[29] ?? 0) >> 5));
  mark('box-kal', x => fifth(x) === fifth(v[31] ?? 0));
  const seventh = x => Math.floor(x * BOX.scenes.length / 128);
  mark('box-scene', x => seventh(x) === seventh(v[39] ?? 0));
  mark('box-scene-cycle', x => fifth(x) === fifth(v[43] ?? 0));
  const nth = n => x => Math.floor(x * n / 128);
  const ms = nth(BOX.maskShapes.length), mf = nth(Math.max(1, BOX.maskFills.length));
  mark('box-mask', x => ms(x) === ms(v[45] ?? 0));
  mark('box-mask-fill', x => mf(x) === mf(v[47] ?? 0));
  const fs = nth(BOX.frameShapes.length), q4 = nth(4);
  mark('box-frame', x => fs(x) === fs(v[51] ?? 0));
  mark('box-frame-inv', x => (x >= 64) === ((v[56] ?? 0) >= 64));
  mark('box-frame-pic', x => q4(x) === q4(v[57] ?? 0));
  const q3 = nth(3);
  mark('box-rot', x => fifth(x) === fifth(v[61] ?? 0));
  mark('box-rot-n', x => q3(x) === q3(v[62] ?? 0));
  mark('box-rot-hits', x => (x >= 64) === ((v[76] ?? 0) >= 64));
  document.querySelectorAll('#box-rot-looks .flag').forEach(b => b.classList.toggle('on', (v[+b.dataset.cc] ?? 127) >= 64));
  boxPoolRow();
  for (const [cc, f] of SLIDERS) {
    const el = document.getElementById('bk-' + cc), lab = document.getElementById('bv-' + cc);
    if (el && v[cc] !== undefined) { if (document.activeElement !== el) el.value = v[cc]; lab.textContent = f(+el.value); }
  }
}

// POOLS (owner 2026-10-29): hand-picked lists of clips, picked live, plus EVERYTHING
// (the whole library shuffled). Stored on the box (lib/pools.json); pool N = CC 30 value N.
const curPool = () => BOX.values[30] ?? 0;
let poolArmed = 0;

function boxPoolRow() {
  const el = document.getElementById('box-pool');
  if (!el) return;
  const cur = curPool();
  el.innerHTML = ['EVERYTHING', ...BOX.pools.map((p, i) => `${i + 1}. ${p.order ? '▶ ' : ''}${p.name}`)].map((n, i) =>
    `<button class="flag${i === cur ? ' on' : ''}" onclick="boxSend(30, ${i}); boxClips()">${escB(n)}</button>`).join('')
    + '<button class="flag" onclick="boxPoolNew()">+ NEW POOL</button>'
    + '<button class="flag" onclick="boxPoolNew(true)">+ NEW PLAYLIST</button>';
  boxPlaylist();
  const sp = document.getElementById('box-scene-pool');         // SCENE POOL (CC 60): the scenes' own clips
  if (sp) {
    const cur2 = Math.min(BOX.pools.length, Math.max(0, BOX.values[60] ?? 0));
    sp.innerHTML = ['SCENES: SAME AS POOL', ...BOX.pools.map(p => 'SCENES: ' + p.name)].map((n, i) =>
      `<button class="flag${i === cur2 ? ' on' : ''}" onclick="boxSend(60, ${i}); boxPoolRow()">${escB(n)}</button>`).join('');
  }
  const p = BOX.pools[cur - 1];
  document.getElementById('box-pool-tools').innerHTML = p ? `
    <span style="font-size:10px;color:var(--muted);align-self:center">${escB(p.name)}: ${p.ids.length} clips${p.order ? ', in this order, looping' : ', shuffled'}</span>
    <button class="flag${p.order ? ' on' : ''}" onclick="boxPoolOrder()">${p.order ? 'PLAYLIST (IN ORDER)' : 'MAKE IT A PLAYLIST'}</button>
    <button class="flag" onclick="boxPoolRename()">RENAME</button>
    <button class="flag" onclick="boxPoolRemove()">${poolArmed === cur ? 'SURE? (later pools move up one)' : 'REMOVE POOL'}</button>` : '';
}

async function boxPoolsSave(msg) {
  BOX.poolsT = Date.now();                    // the box's status lags ~10-15 s: keep ours meanwhile
  const d = await fetch('/api/mxvbox/pools', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pools: BOX.pools }) }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
  if (d.ok) BOX.pools = d.pools;
  document.getElementById('box-state').textContent = d.ok ? msg : `could not save the pools: ${d.error}`;
  boxPoolRow(); boxClips();
}

function boxPoolNew(order = false) {
  const kind = order ? 'playlist' : 'pool';
  const name = prompt(`Name for the new ${kind}`, `${kind} ${BOX.pools.length + 1}`);
  if (name === null) return;
  BOX.pools.push({ name: name.trim() || `${kind} ${BOX.pools.length + 1}`, ids: [], order });
  boxSend(30, BOX.pools.length);
  boxPoolsSave(`made ${kind} ${BOX.pools.length}: now ADD clips to it below` + (order ? ' (they play in the order you add them)' : ''));
}

function boxPoolOrder() {
  const p = BOX.pools[curPool() - 1];
  if (!p) return;
  p.order = !p.order;
  boxPoolsSave(p.order ? `${p.name} is a playlist: its clips play in order, looping` : `${p.name} is a shuffled pool again`);
}

// a playlist's clips, numbered, each with up / down / out (2026-11-02, owner: "numbered playlists")
function boxPlaylist() {
  const el = document.getElementById('box-playlist');
  if (!el) return;
  const p = BOX.pools[curPool() - 1];
  if (!p || !p.order) { el.innerHTML = ''; return; }
  const names = Object.fromEntries((BOX.clips || []).map(c => [c.id, c.name]));
  const pos = BOX.playlistPos || 0;
  el.innerHTML = p.ids.length ? p.ids.map((id, i) => `
    <div style="display:flex;gap:6px;align-items:center;font-size:10px;margin:2px 0">
      <span style="width:22px;text-align:right;color:var(--muted)">${i + 1}.</span>
      <span style="flex:1;font-family:var(--fs)">${escB(names[id] || id)}${pos === i + 1 ? ' · <b>PLAYING</b>' : ''}${names[id] ? '' : ' <span style="color:var(--warn,#fbbf24)">(not on the box)</span>'}</span>
      <button class="flag" style="padding:2px 8px" ${i ? '' : 'disabled'} onclick="boxPlMove(${i}, -1)">↑</button>
      <button class="flag" style="padding:2px 8px" ${i < p.ids.length - 1 ? '' : 'disabled'} onclick="boxPlMove(${i}, 1)">↓</button>
      <button class="flag" style="padding:2px 8px" onclick="boxPlMove(${i}, 0)">✕</button>
    </div>`).join('') : '<span style="color:var(--muted);font-size:10px">empty: ADD clips from the list below, in the order they should play</span>';
}

function boxPlMove(i, d) {
  const p = BOX.pools[curPool() - 1];
  if (!p) return;
  if (d === 0) p.ids.splice(i, 1);
  else [p.ids[i], p.ids[i + d]] = [p.ids[i + d], p.ids[i]];
  boxPoolsSave(`${p.name}: ${p.ids.length} clips, in order`);
}

function boxPoolRename() {
  const p = BOX.pools[curPool() - 1];
  const name = p && prompt('New name', p.name);
  if (!name) return;
  p.name = name.trim();
  boxPoolsSave('renamed');
}

function boxPoolRemove() {
  const cur = curPool();
  if (poolArmed !== cur) { poolArmed = cur; boxPoolRow(); setTimeout(() => { poolArmed = 0; boxPoolRow(); }, 4000); return; }
  poolArmed = 0;
  BOX.pools.splice(cur - 1, 1);
  boxSend(30, 0);
  // SCENE POOL (CC 60) names a pool by number too: the removed one -> the same as POOL, a later
  // one moves down with its pool (it silently pointed at the next pool before, 2026-11-03 review)
  const sp = BOX.values[60] ?? 0;
  if (sp === cur) boxSend(60, 0);
  else if (sp > cur) boxSend(60, sp - 1);
  boxPoolsSave('pool removed: back to EVERYTHING');
}

function boxPoolToggle(id) {
  const p = BOX.pools[curPool() - 1];
  if (!p) return;
  p.ids = p.ids.includes(id) ? p.ids.filter(x => x !== id) : [...p.ids, id];
  boxPoolsSave(`${p.name}: ${p.ids.length} clips`);
}

// The box's clip library, with a delete button each (owner: "allow me to have some way
// of deleting videos from the list"). First click arms it ("SURE?"), the second deletes.
let boxArmed = null, boxArmT = 0;
const escB = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function boxClips(clips = BOX.clips, playing = BOX.playing) {
  BOX.clips = clips; BOX.playing = playing;
  const el = document.getElementById('box-clips-list');
  if (!el) return;
  const pool = BOX.pools[curPool() - 1];
  if (!clips) { el.innerHTML = '<span style="color:var(--muted)">(waiting for the box)</span>'; return; }
  if (boxArmed && Date.now() - boxArmT > 4000) boxArmed = null;
  const html = clips.length ? clips.map(c => `
    <div style="display:flex;gap:8px;align-items:center;font-size:10px;margin:2px 0">
      <span style="flex:1;font-family:var(--fs)">${escB(c.name)} <span style="color:var(--muted)">
        ${(c.frames / 15).toFixed(1)} s${c.id === playing ? ' · ON SCREEN' : ''}</span></span>
      ${pool ? `<button class="flag${pool.ids.includes(c.id) ? ' on' : ''}" style="padding:2px 8px"
         onclick="boxPoolToggle('${escB(c.id)}')">${pool.ids.includes(c.id) ? 'IN POOL' : 'ADD'}</button>` : ''}
      <button class="flag${BOX.flags.fav.includes(c.id) ? ' on' : ''}" style="padding:2px 8px" title="favourite: loaded first, shown about twice as often"
         onclick="boxFlag('fav', '${escB(c.id)}')">★</button>
      <button class="flag${BOX.flags.never.includes(c.id) ? ' on' : ''}" style="padding:2px 8px" title="never play it (it stays in the library)"
         onclick="boxFlag('never', '${escB(c.id)}')">NEVER</button>
      <button class="flag${BOX.queued === c.id ? ' on' : ''}" style="padding:2px 8px" title="the next clip bar plays it"
         onclick="boxNext('${escB(c.id)}')">${BOX.queued === c.id ? 'UP NEXT' : 'PLAY NEXT'}</button>
      <button class="flag" style="padding:2px 8px" onclick="boxDelete('${escB(c.id)}')">${boxArmed === c.id ? 'SURE?' : 'DELETE'}</button>
    </div>`).join('') : '<span style="color:var(--muted)">no clips on the box yet: drag videos into \\\\pibox\\boxinbox</span>';
  // only when something changed: rebuilding it on every 3 s poll could eat a tap on the phone
  if (html !== el._html) { el.innerHTML = html; el._html = html; }
}

// FAVOURITE / NEVER (one or the other) and PLAY NEXT, per clip (2026-11-02)
async function boxFlag(kind, id) {
  const other = kind === 'fav' ? 'never' : 'fav';
  const on = !BOX.flags[kind].includes(id);
  BOX.flags[kind] = on ? [...BOX.flags[kind], id] : BOX.flags[kind].filter(x => x !== id);
  if (on) BOX.flags[other] = BOX.flags[other].filter(x => x !== id);
  BOX.flagsT = Date.now();                    // the box's status lags: keep ours meanwhile
  boxClips();
  const d = await fetch('/api/mxvbox/flags', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(BOX.flags) }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
  if (d.ok) BOX.flags = d.flags;
  document.getElementById('box-state').textContent = d.ok
    ? `${id}: ${on ? (kind === 'fav' ? 'a favourite' : 'never played') : (kind === 'fav' ? 'not a favourite' : 'played again')}`
    : `could not save: ${d.error}`;
  boxClips();
}

async function boxNext(id) {
  const forget = BOX.queued === id;
  const d = await fetch('/api/mxvbox/next', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: forget ? '-' : id }) }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
  if (d.ok) { BOX.queued = forget ? null : id; BOX.queuedT = Date.now(); }
  document.getElementById('box-state').textContent = d.ok ? (forget ? 'nothing queued' : `${id} plays at the next clip bar`)
    : `could not queue: ${d.error}`;
  boxClips();
}

async function boxDelete(id) {
  if (boxArmed !== id) { boxArmed = id; boxArmT = Date.now(); boxPoll(); return; }
  boxArmed = null;
  const d = await fetch('/api/mxvbox/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }) }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
  document.getElementById('box-state').textContent = d.ok ? `deleted ${id} from the box` : `could not delete: ${d.error}`;
}

async function boxPoll() {
  try {
    const d = await fetch('/api/mxvbox').then(r => r.json());
    if (!d.ok) throw new Error(d.error || 'failed');
    BOX.ch = d.channel;
    BOX.styles = d.styles; BOX.lines = d.lines; BOX.linesBlur = d.lines_blur || null; BOX.kaleido = d.kaleido || [0, 3, 4, 6, 8];
    BOX.scenes = d.scenes || [null];
    BOX.maskShapes = d.mask_shapes || [null];
    BOX.maskFills = d.mask_fills || [];
    BOX.frameShapes = d.frame_shapes || [null];
    BOX.knobs = Object.fromEntries(d.knobs.map(k => [k.cc, k]));
    if (!BOX.built) boxBuild();
    if (Date.now() - BOX.touched > 3000)        // the conductor saves ~2 s after a change
      BOX.values = Object.fromEntries(Object.entries(d.values).map(([k, x]) => [+k, x]));
    const bits = [d.box ? 'box online' : 'BOX NOT ANSWERING (off? press its power button)',
                  d.conductor ? 'conductor running' : 'CONDUCTOR STOPPED (orc1-boxvis)',
                  d.channel ? `channel ${d.channel}` : 'mxvbox NOT BOUND: bind it on /matrix'];
    const L = d.library;                          // the rolling clip library (library.py)
    if (L) bits.push(`library ${L.library} clips · ${L.slots.length} on the box`
                     + (L.loading ? ` · arriving: ${L.loading}` : ''));
    if (L && Array.isArray(L.pool_lists) && Date.now() - BOX.poolsT > 20000) BOX.pools = L.pool_lists;
    if (L && L.flags && Date.now() - BOX.flagsT > 20000)
      BOX.flags = { fav: L.flags.fav || [], never: L.flags.never || [] };
    if (L && Date.now() - BOX.queuedT > 15000) BOX.queued = L.queued || null;
    BOX.playlistPos = (L && L.playlist_pos) || 0;
    if (BOX.queued) bits.push(`up next: ${BOX.queued}`);
    boxClips(L && L.clips, L && L.playing);
    const st = document.getElementById('box-state');
    st.textContent = bits.join(' · ');
    st.style.color = (d.box && d.conductor && d.channel) ? 'var(--muted)' : 'var(--warn,#fbbf24)';
    boxShow();
  } catch (e) {
    document.getElementById('box-state').textContent = 'could not read /api/mxvbox: ' + e.message;
  }
}

boxPoll();
setInterval(boxPoll, 3000);
