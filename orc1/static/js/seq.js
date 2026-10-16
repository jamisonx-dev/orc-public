const CHORD_TONES = {'-1':'BASS','0':'ROOT','1':'3rd','2':'5th','3':'7th','4':'9th'};
const MODES = ['note','chord'];
let _s = {};   // last state
let _sel_step = {};  // {tidx: step} for note editing

function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 2000);
}

async function savePattern(){
  await cmd('SEQ_SAVE');
  const el = document.getElementById('save-note');
  const orig = el.textContent;
  el.textContent = '✓ saved';
  el.style.color = 'var(--accent)';
  setTimeout(()=>{ el.textContent = orig; el.style.color = ''; }, 2500);
}

async function cmd(action){
  try{
    await fetch('/api/seq',{method:'POST',headers:{'Content-Type':'application/json'},
                            body:JSON.stringify({action})});
  } catch(e){ toast('Error: '+e); }
}

function setRecTrack(v){ cmd('SEQ_REC_TRACK:'+v); }

// Effective render colour: the stored 0-127 track colour scaled by the track's
// brightness (0-127, default full). Brightness is a cosmetic render multiplier
// applied here (web) and in the Fire's _track_color -- the stored colour stays
// pristine so the picker/hue dial still edit the true hue.
function eff(t){
  const b = (t.bright===undefined?127:t.bright)/127;
  const c = t.color||[0,0,0];
  return [Math.round(c[0]*b), Math.round(c[1]*b), Math.round(c[2]*b)];
}

function rgb(c){ return `rgb(${c[0]*2},${c[1]*2},${c[2]*2})`; }
function dimrgb(c,f=8){
  // Floor each channel so an "empty" step is always at least a visible dark
  // gray, regardless of how dark the track's own color is -- otherwise a
  // dark track's off-steps round to ~black and vanish against the panel bg.
  const r = Math.max(32, Math.round(c[0]*2/f));
  const g = Math.max(32, Math.round(c[1]*2/f));
  const b = Math.max(32, Math.round(c[2]*2/f));
  return `rgb(${r},${g},${b})`;
}

// Stored track colour is on a 0-127 scale (Fire 7-bit pad RGB); the web grid
// doubles it for brightness. The <input type="color"> works in 0-255 hex, so
// map stored->hex by *2 (clamped) and hex->stored by /2 on the way back. This
// keeps the picker WYSIWYG with the step grid.
function colorHex(c){
  const h = n => Math.min(255, (n||0)*2).toString(16).padStart(2,'0');
  c = c || [0,0,0];
  return '#'+h(c[0])+h(c[1])+h(c[2]);
}
function setColor(tidx, hex){
  const r = parseInt(hex.slice(1,3),16) >> 1;
  const g = parseInt(hex.slice(3,5),16) >> 1;
  const b = parseInt(hex.slice(5,7),16) >> 1;
  cmd(`SEQ_SET_PROP:${tidx}:color:${r},${g},${b}`);
}

function noteLabel(t, note){
  if(t.mode==='chord') return CHORD_TONES[String(note)]||('T'+note);
  const names=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
  return names[note%12]+(Math.floor(note/12)-1);
}

function buildStepGrid(t, tidx){
  const len = t.length;
  const cols = Math.min(len, 16);
  const rows = Math.ceil(len / 16);
  const tc = eff(t);   // colour after the track's brightness multiplier
  let html = `<div class="step-grid" style="grid-template-columns:repeat(${cols},1fr)">`;
  for(let i=0;i<len;i++){
    const on  = t.steps[i]==='1';
    const acc = t.accents && t.accents[i]==='1';
    const sno = t.step_notes && t.step_notes[String(i)];
    const isPos = i===t.pos && _s.seq_running;
    const isSel = _sel_step[tidx]===i;
    let bg, border='1px solid rgba(255,255,255,.18)';
    if(isPos)      { bg = on ? rgb(tc) : '#444'; border='1px solid rgba(255,255,255,.5)'; }
    else if(on)    { bg = sno!==undefined ? `rgba(${tc[0]*2+40},${tc[1]*2+40},${tc[2]*2+40},1)` : (acc?`rgba(${Math.min(255,tc[0]*2+80)},${Math.min(255,tc[1]*2+80)},${Math.min(255,tc[2]*2+80)},1)`:rgb(tc)); }
    else           { bg = dimrgb(tc, tidx===(_s.sel_track||0)?5:8); }
    const h = rows>1?'14px':'20px';
    const ovClass = sno!==undefined&&on?' has-override':'';
    const plClass = isPos?' playhead':'';
    const selClass = isSel?' ring':'';
    html += `<div class="step${ovClass}${plClass}" data-tidx="${tidx}" data-step="${i}"
              style="background:${bg};height:${h};border:${border}"
              ontouchstart="event.preventDefault();tapStep(${tidx},${i})"
              onclick="tapStep(${tidx},${i})"></div>`;
  }
  return html + '</div>';
}

function tapStep(tidx, step){
  // Simple press-to-set, press-again-to-erase.
  // If the track's ▸ detail panel is open, the first tap instead selects
  // the step so you can edit its per-step note override in the note row below.
  // Tap the same step again to deselect, or tap another step to move selection.
  //
  // Debounce: 400ms cooldown per (track, step) pair.  Without this, mobile
  // browsers can re-fire events on the freshly rebuilt DOM element while a
  // finger is still on the screen (seq page rebuilds every 200ms while playing).
  const key = `${tidx}:${step}`;
  const now  = Date.now();
  if(window._stepCooldown && window._stepCooldown[key] &&
     now - window._stepCooldown[key] < 400) return;
  (window._stepCooldown = window._stepCooldown||{})[key] = now;

  const t = _s.seq_tracks[tidx];
  if(!t) return;
  if(_expanded.has(tidx)){
    // Detail open: select/deselect step for note editing
    if(_sel_step[tidx] === step){
      _sel_step[tidx] = undefined;
    } else {
      _sel_step[tidx] = step;
    }
    renderTracks();
    return;
  }
  // Detail closed: plain toggle
  cmd(`SEQ_TOGGLE_STEP:${tidx}:${step}`);
}

function buildNoteRow(t, tidx){
  const selStep = _sel_step[tidx];
  if(selStep===undefined) return '';
  const sno = t.step_notes && t.step_notes[String(selStep)];
  const hasOverride = sno !== undefined;
  let inputHtml;
  if(t.mode==='chord'){
    const tones = [[-1,'BASS'],[0,'ROOT'],[1,'3rd'],[2,'5th'],[3,'7th'],[4,'9th']];
    inputHtml = `<select onchange="setStepNote(${tidx},${selStep},this.value)">
      ${tones.map(([v,l])=>`<option value="${v}"${(hasOverride?sno:t.note)==v?' selected':''}>${l}</option>`).join('')}
    </select>`;
  } else {
    const curNote = hasOverride ? sno : t.note;
    inputHtml = `<input type="number" min="0" max="127" value="${curNote}"
      onchange="setStepNote(${tidx},${selStep},this.value)">`;
  }
  return `<div class="note-row">
    <label>STEP ${selStep+1} NOTE</label>
    ${inputHtml}
    <button class="clr" onclick="clearStepNote(${tidx},${selStep})">${hasOverride?'CLR':'DEFAULT'}</button>
  </div>`;
}

function setStepNote(tidx, step, note){
  const t = _s.seq_tracks && _s.seq_tracks[tidx];
  const minNote = (t && t.mode === 'chord') ? -1 : 0;
  cmd(`SEQ_SET_NOTE:${tidx}:${step}:${Math.max(minNote, parseInt(note))}`);
}
function clearStepNote(tidx, step){
  cmd(`SEQ_CLEAR_NOTE:${tidx}:${step}`);
  delete _sel_step[tidx];
}

function buildTrackDetail(t, tidx){
  const modes = MODES.map(m=>`<option value="${m}"${t.mode===m?' selected':''}>${m}</option>`).join('');
  return `<div class="tdetail${_expanded.has(tidx)?' open':''}" id="td-${tidx}">
    <div class="tgrid">
      <div>
        <div class="tlbl">CHANNEL</div>
        <input class="tinp" type="number" min="1" max="16" value="${t.ch}"
               onchange="setProp(${tidx},'ch',this.value)">
      </div>
      <div>
        <div class="tlbl">DEFAULT ${t.mode==='chord'?'TONE':'NOTE'}</div>
        ${t.mode==='chord'
          ? `<select class="tsel" onchange="setProp(${tidx},'note',this.value)">
               ${[[-1,'BASS'],[0,'ROOT'],[1,'3rd'],[2,'5th'],[3,'7th'],[4,'9th']].map(([v,l])=>`<option value="${v}"${t.note==v?' selected':''}>${l}</option>`).join('')}
             </select>`
          : `<input class="tinp" type="number" min="0" max="127" value="${t.note}"
                    onchange="setProp(${tidx},'note',this.value)">`
        }
      </div>
      <div>
        <div class="tlbl">MODE</div>
        <select class="tsel" onchange="setProp(${tidx},'mode',this.value)">${modes}</select>
      </div>
      <div>
        <div class="tlbl">VELOCITY</div>
        <input class="tinp" type="number" min="1" max="127" value="${t.vel}"
               onchange="setProp(${tidx},'vel',this.value)">
      </div>
      <div>
        <div class="tlbl">LENGTH</div>
        <input class="tinp" type="number" min="1" max="64" value="${t.length}"
               onchange="setProp(${tidx},'len',this.value)">
      </div>
      <div>
        <div class="tlbl">NAME</div>
        <input class="tinp" type="text" maxlength="12" value="${t.name}"
               onchange="setProp(${tidx},'name',this.value)">
      </div>
      <div>
        <div class="tlbl">COLOR</div>
        <input class="tcol" type="color" value="${colorHex(t.color)}"
               onchange="setColor(${tidx},this.value)">
      </div>
      <div>
        <div class="tlbl">BRIGHT</div>
        <input class="tbri" type="range" min="0" max="127"
               value="${t.bright===undefined?127:t.bright}"
               oninput="this.nextElementSibling.textContent=Math.round(this.value*100/127)+'%'"
               onchange="setProp(${tidx},'bright',this.value)">
        <span class="tbri-val">${Math.round((t.bright===undefined?127:t.bright)*100/127)}%</span>
      </div>
    </div>
    <div class="tfooter">
      <button class="tbtn-sm accent" onclick="cmd('SEQ_CLEAR_STEPS:${tidx}')">CLR STEPS</button>
      <button class="tbtn-sm danger"  onclick="delTrack(${tidx})">DELETE</button>
    </div>
  </div>`;
}

function setProp(tidx, prop, val){
  cmd(`SEQ_SET_PROP:${tidx}:${prop}:${val}`);
}

function delTrack(tidx){
  if(!confirm('Delete track '+(tidx+1)+'?')) return;
  cmd('SEQ_DEL:'+tidx);
}

let _expanded = new Set();
function toggleExpand(tidx){
  if(_expanded.has(tidx)) _expanded.delete(tidx);
  else _expanded.add(tidx);
  const el = document.getElementById('td-'+tidx);
  const btn = document.getElementById('te-'+tidx);
  if(el){ el.classList.toggle('open', _expanded.has(tidx)); }
  if(btn){ btn.classList.toggle('open', _expanded.has(tidx)); btn.textContent = _expanded.has(tidx)?'▾':'▸'; }
}

function renderTracks(){
  const trks = _s.seq_tracks || [];
  const recTrk = _s.seq_rec_track || 0;
  // Update rec track select
  const sel = document.getElementById('rec-track-sel');
  if(sel){
    // Only rebuild the dropdown options when they've actually changed AND
    // the element doesn't currently have focus (on mobile, focus = native
    // picker is open; rebuilding innerHTML while it's open closes and
    // reopens it on every poll cycle, causing the "many windows" crash).
    const want = trks.map((t,i)=>`<option value="${i}"${i===recTrk?' selected':''}>${i+1}: ${t.name}</option>`).join('');
    if(sel.innerHTML !== want && document.activeElement !== sel) sel.innerHTML = want;
  }
  document.getElementById('rec-status').textContent = _s.seq_record ? '● REC' : '';
  document.getElementById('rec-status').style.color = _s.seq_record ? 'var(--rec)' : '';

  const div = document.getElementById('tracks');
  // Same root cause as the rec-track-sel guard above: this whole container
  // gets replaced from scratch every poll tick (every 200-800ms), which
  // wipes focus, cursor position, and any in-progress edit on every input
  // inside a track's detail panel (CHANNEL/NOTE/MODE/VELOCITY/LENGTH/NAME)
  // or the step-note override row. Skip the rebuild entirely while the user
  // has a field in here focused -- steps/playhead animation pause for that
  // one poll tick, which is a fine trade for not losing what you just typed.
  if (div.contains(document.activeElement)) return;
  div.innerHTML = trks.map((t,tidx)=>{
    const isRec   = _s.seq_record && tidx===recTrk;
    const isMuted = t.mute;
    const chLabel = `CH${t.ch}`;
    return `<div class="tcard${isRec?' is-rec':''}">
      <div class="tcard-head">
        <span class="tnum">${tidx+1}</span>
        <span class="tname">${t.name}</span>
        <span class="tmode">${chLabel} ${t.mode}</span>
        <div class="tmute${isMuted?' muted':''}" onclick="cmd('SEQ_MUTE:${tidx}')"></div>
        <button class="texpand${_expanded.has(tidx)?' open':''}" id="te-${tidx}"
                onclick="toggleExpand(${tidx})">${_expanded.has(tidx)?'▾':'▸'}</button>
      </div>
      ${buildStepGrid(t, tidx)}
      ${buildNoteRow(t, tidx)}
      ${buildTrackDetail(t, tidx)}
    </div>`;
  }).join('');
}

function renderTransport(){
  const running = _s.seq_running;
  const rec     = _s.seq_record;
  document.getElementById('btn-play').className = 'tbtn'+(running?' active-play':'');
  document.getElementById('btn-stop').className = 'tbtn';
  document.getElementById('btn-rec').className  = 'tbtn'+(rec?' active-rec':'');
  const bpm = _s.arp_bpm || _s.bpm;
  if(bpm) document.getElementById('bpm-disp').textContent = Math.round(bpm*10)/10+'bpm';
}

async function poll(){
  try{
    const r = await fetch('/state?'+Date.now());
    _s = await r.json();
    renderTransport();
    renderTracks();
  } catch(e){}
  setTimeout(poll, _s.seq_running ? 200 : 800);
}

poll();
