const REFRESH_MS=1500;
const ROOT_NAMES=["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
/* Fallback only. The live list comes from the engine (state.scales) so this
   picker can't drift as scales are added -- see scaleList() below. */
const SCALE_NAMES=["MAJ","MIN","HMIN","MMIN","DOR","PHR","LYD","MIX","LOC","CHROM"];
function scaleList(d){
  return (d && Array.isArray(d.scales) && d.scales.length) ? d.scales : SCALE_NAMES;
}

let _bpm=120.0;
let _rootOpen=false;
let _scaleOpen=false;
let _lastData=null;
let _restarting=false;

async function cmd(action,extra){
  try{await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({action},extra||{}))});}catch(e){}
}

function flag(label,action,on,style){
  return `<button class="flag ${on?(style||'on'):''}" onclick="cmd('${action}')">${label}</button>`;
}

function nudgeRow(key,val,action,step){
  const d=typeof val==='number'?val.toFixed(2):(val||'—');
  return `<div class="arp-row">
    <span class="arp-key">${key}</span>
    <div class="arp-ctrl">
      <button class="nudge" onclick="cmd('${action}',{value:${-step}})">${-step}</button>
      <span class="arp-val">${d}</span>
      <button class="nudge" onclick="cmd('${action}',{value:${step}})">+${step}</button>
    </div></div>`;
}

function render(d){
  const arp=d.arp||{};
  const root=d.root??0;
  const curScale=d.scale||'CHROM';
  const extsHtml=(d.chord_exts||[]).map(e=>`<span class="ext-pill">${e}</span>`).join('');

  /* ── BPM (white) ─────────────────────────────────── */
  /* Clock IN: internal (Pi makes its own clock) vs external (follow incoming
     MIDI clock). DRIFT can be set from the Fire but isn't offered here, so the
     web toggle only ever flips INT<->EXT. */
  const ext=arp.clock_mode==='external';
  const drift=arp.clock_mode==='drift';
  const inLabel=drift?'IN: DRIFT ⚠':ext?'IN: EXT':'IN: INT';
  const inNext=ext?'internal':'external';   /* DRIFT also flips to EXT */
  const bpmStr=typeof arp.bpm==='number'?arp.bpm.toFixed(1):'—';
  const bpmSection=`
  <div class="bpm-section">
    <div class="bpm-top">
      <span class="bpm-num" id="bpm-display" title="tap to type a tempo" onclick="setBpmManual()">${bpmStr}</span>
      <span class="bpm-unit">BPM</span>
      <div class="bpm-flags">
        <button class="flag ${(ext||drift)?'warn':''}" onclick="cmd('SET_CLOCK_SRC',{mode:'${inNext}'})">${inLabel}</button>
        ${flag(d.clock_out?'CLK OUT ✓':'CLK OUT','TOGGLE_CLOCK_OUT',d.clock_out)}
      </div>
    </div>
    <div class="bpm-btns">
      <button class="bpm-btn" onclick="adjBpm(-5)">−5</button>
      <button class="bpm-btn" onclick="adjBpm(-1)">−1</button>
      <button class="bpm-btn tap-btn" onclick="cmd('TAP_TEMPO')">TAP</button>
      <button class="bpm-btn" onclick="adjBpm(1)">+1</button>
      <button class="bpm-btn" onclick="adjBpm(5)">+5</button>
    </div>
  </div>`;

  /* ── MIDI (blue) ──────────────────────────────────── */
  const devs=d.midi_devices||[];
  const devHtml=devs.length
    ?'<ul class="dev-list">'+devs.map(n=>`<li>${n}</li>`).join('')+'</ul>'
    :'<div class="dev-none">No external devices</div>';
  const dinMode=d.din_routing==='engine';
  /* ── pan helper: -1..1 → display label ───── */

  const midiCard=`
  <div class="card blue">
    <div class="card-label"><span>MIDI</span>
      <button class="rescan-btn" onclick="cmd('MIDI_RESCAN')">RESCAN</button>
    </div>
    ${devHtml}
    <div class="flags">
      ${flag(dinMode?'DIN: ENGINE':'DIN: THRU','TOGGLE_DIN_ROUTING',dinMode)}
      ${flag('CHORDS → POLY','TOGGLE_SYNTH_MIRROR_POLY',d.synth_mirror_poly)}
      ${flag('CHORDS → CHIP','TOGGLE_SYNTH_MIRROR_CHIP',d.synth_mirror_chip)}
    </div>
  </div>`;

  /* ── root picker  (piano-keyboard layout: white keys / black keys) ──── */
  const rootPicker=_rootOpen?`
    <div class="picker" style="display:block;padding:6px 8px">
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:3px">
        ${[{n:'C',i:0},{n:'D',i:2},{n:'E',i:4},{n:'F',i:5},{n:'G',i:7},{n:'A',i:9},{n:'B',i:11}]
          .map(({n,i})=>`<button class="pk-btn ${i===root?'sel':''}"
            onclick="_rootOpen=false;cmd('ROOT_SET',{value:${i}});rerender()">${n}</button>`).join('')}
      </div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:3px">
        ${[{n:'C#',i:1},{n:'E♭',i:3},{n:'F#',i:6},{n:'A♭',i:8},{n:'B♭',i:10}]
          .map(({n,i})=>`<button class="pk-btn ${i===root?'sel':''}"
            onclick="_rootOpen=false;cmd('ROOT_SET',{value:${i}});rerender()">${n}</button>`).join('')}
      </div>
    </div>`:'';

  /* ── scale picker ───────────────────────────────── */
  const scalePicker=_scaleOpen?`
    <div class="picker scale-picker">
      ${scaleList(d).map(s=>`<button class="pk-btn ${s===curScale?'sel':''}"
        onclick="_scaleOpen=false;cmd('SCALE_SET',{value:'${s}'});rerender()">${s}</button>`).join('')}
    </div>`:'';

  /* ── key lock ──────────────────────────────────────────────────────
     REPURPOSED 2026-07-29 (O3).  It used to stop the keyboard overwriting the
     root; the root/chord_root split means the keyboard can no longer touch the
     key at all, so that job no longer exists.  It now pulls an off-key press
     into the key, which is what it was accidentally approximating.
     (Was "DIAT LOCK" before 2026-07-28.) */
  const klLabel=d.key_lock?'KEY LOCK ✓':'KEY LOCK';
  const klTip='Pulls a chord played outside the key onto the nearest note that is in it, so every pad gives an in-key chord';
  /* O4: SCALE SNAP became a five-way mode (off/filter/up/down/nearest), so the
     button cycles instead of toggling and names the mode it is in. Falls back
     to the legacy boolean if the engine predates scale_mode. */
  const snapMode=d.scale_mode||(d.scale_snap?'nearest':'off');
  const snapLabel=snapMode==='off'?'SNAP OFF':'SNAP '+snapMode.toUpperCase();
  const snapTips={
    off:'Off - every note plays as pressed',
    filter:'Off-scale notes are SILENCED rather than moved',
    up:'Off-scale notes round UP to the next note in the scale',
    down:'Off-scale notes round DOWN to the previous note in the scale',
    nearest:'Off-scale notes round to whichever scale note is closer'};
  const snapTip='Fire poly/chip keyboards. '+(snapTips[snapMode]||'')+' (click to cycle)';

  /* O4: which source the chord quality is coming from, and a control to latch
     it. DIAT = from the scale degree; FIXED = the quality pads are a persistent
     setting. base_override is a MOMENTARY override inside DIAT only. */
  const qMode=d.quality_mode||'DIAT';
  const qLabel=qMode==='FIXED'?'QUALITY FIXED':'QUALITY DIAT';
  const qTip=qMode==='FIXED'
    ?'The MAJ/MIN/SUS/DIM pads are a persistent setting. Click for DIAT.'
    :'Chord quality comes from the scale degree; a quality pad overrides it until the next key press. Click to latch FIXED.';
  const chPicker='';

  /* Chord-quality badge. Must read chord_base_eff (what actually sounds), NOT
     chord_base -- chord_base only holds the last explicit override and is never
     reset, so showing it would keep displaying a stale quality long after the
     override lapsed. Marked when the quality is a manual override rather than
     the scale's own degree.
     Since O3 chord_base_eff can be any shape the scale produced (AUG, maj7,
     m7b5, 6no5) or a roman numeral when the stack has no common name -- so the
     old `!== 'MAJ'` suppression is kept only to avoid shouting on a plain major,
     and the badge is no longer assumed to be one of four names. */
  const effBase=d.chord_base_eff||d.chord_base;
  const baseBadge=(effBase&&effBase!=='MAJ'&&effBase!=='--')
    ?`<div class="chord-base" title="${qMode==='FIXED'?'FIXED mode - the quality pads are a persistent setting':d.base_override?'Manual override (until next key press)':'From the scale degree'}">${effBase}${(qMode!=='FIXED'&&d.base_override)?'*':''}</div>`
    :'';

  /* Key + degree, so the page can say WHY a chord is the quality it is -- the
     Fire OLED got this and the web did not. Hidden on degree I of the key,
     where it would just restate the chord root. */
  const degStr=(d.chord_degree!=null)
    ?['I','II','III','IV','V','VI','VII','VIII'][d.chord_degree]||'' : '';
  const keyBadge=(degStr&&d.chord_root_name&&d.chord_root_name!==d.root_name)
    ?`<div class="chord-base" title="Degree ${degStr} of ${d.root_name} ${d.scale||''}">${d.root_name}:${degStr}</div>`
    :'';

  /* ── ARP controls ───────────────────────────────── */
  const arpSection=arp&&Object.keys(arp).length?`
    <div class="arp-sep">
      <div class="arp-head">
        <span class="arp-title">ARPEGGIATOR</span>
        ${flag(arp.enabled?'ON':'OFF','TOGGLE_ARP',arp.enabled)}
      </div>
      <div class="flags" style="margin-bottom:6px">
        ${flag(d.arp_hold?'HOLD ✓':'HOLD','TOGGLE_ARP_HOLD',d.arp_hold,'warn')}
      </div>
      <div class="arp-params">
        <div class="arp-row">
          <span class="arp-key">PATTERN</span>
          <div class="arp-ctrl">
            <button class="nudge" onclick="cmd('PATTERN_PREV')">◀</button>
            <span class="arp-val" style="min-width:64px">${arp.pattern||'—'}</span>
            <button class="nudge" onclick="cmd('PATTERN_NEXT')">▶</button>
          </div>
        </div>
        <div class="arp-row">
          <span class="arp-key">RATE</span>
          <div class="arp-ctrl">
            <button class="nudge" onclick="cmd('ARP_RATE_PREV')">◀</button>
            <span class="arp-val" style="min-width:44px">${arp.rate||'—'}</span>
            <button class="nudge" onclick="cmd('ARP_RATE_NEXT')">▶</button>
          </div>
        </div>
        ${nudgeRow('GATE',arp.gate_trim??arp.gate,'ARP_GATE_DELTA',0.05)}
        ${nudgeRow('SWING',arp.swing,'ARP_SWING_DELTA',0.05)}
        ${nudgeRow('HUMANIZE',arp.humanize,'ARP_HUMANIZE_DELTA',0.05)}
      </div>
    </div>`:'';

  /* ── chord + arp card (yellow) ───────────────────── */
  const chordCard=`
  <div class="card yellow">
    <div class="chord-row">
      <div class="chord-left">
        <div class="chord-main">
          <!-- The chord's OWN root, not the key.  root_name became the KEY
               TONIC in the 2026-07-29 split, so showing it here read "G MIN"
               while a D minor was sounding.  Tapping still opens the key
               picker (that is what ROOT_SET sets), hence the title. -->
          <div class="chord-root" onclick="_rootOpen=!_rootOpen;rerender()"
               title="Chord root. Tap to change the KEY (currently ${d.root_name||'?'}).">${d.chord_root_name||d.root_name||'?'}</div>
          <div class="chord-info">
            ${baseBadge}${keyBadge}
            <div class="chord-exts">${extsHtml}</div>
          </div>
        </div>
      </div>
      <div class="chord-actions">
        <button class="panic-btn" onclick="cmd('PANIC')" title="Stops the sequencer, arp, and any latched sampler loop, and flushes hanging notes">⚡ PANIC / STOP ALL</button>
        <button class="restart-btn" onclick="doRestart()">↺ RESTART</button>
      </div>
    </div>
    ${rootPicker}
    <div class="chord-sub">
      <div class="sub-item">
        <div class="sub-lbl">SCALE</div>
        <button class="clickable-val" onclick="_scaleOpen=!_scaleOpen;rerender()">${curScale}</button>
      </div>
      <div class="sub-item"><div class="sub-lbl">VOICING</div><div class="sub-val">${d.voicing||'—'}</div></div>
    </div>
    ${scalePicker}
    <div style="margin-top:8px">
      <div class="flags">
        ${flag(d.root_mode?'STAGE ROOT':'AUTO CHORD','TOGGLE_ROOT_MODE',true,d.root_mode?'warn':'accent')}
        <button class="flag ${d.key_lock?'on':''}" title="${klTip}" onclick="cmd('TOGGLE_KEY_LOCK')">${klLabel}</button>
        <button class="flag ${snapMode!=='off'?'on':''}" title="${snapTip}" onclick="cmd('CYCLE_SCALE_MODE')">${snapLabel}</button>
        <button class="flag ${qMode==='FIXED'?'on':''}" title="${qTip}" onclick="cmd('TOGGLE_QUALITY_MODE')">${qLabel}</button>
        ${flag('VL','TOGGLE_VL',d.vl_enabled)}
        ${flag('MIRROR','TOGGLE_MIRROR',d.mirror_input_ch)}
        ${flag(d.arp_passthrough?'NOTE THRU':'CHORD ENG','TOGGLE_ARP_PT',d.arp_passthrough,'warn')}
      </div>
      ${chPicker}
    </div>
    ${arpSection}
  </div>`;

  if(_restarting) return `<div class="offline-card"><div class="offline-icon">↺</div><div class="offline-msg">RESTARTING ORC-1…</div></div>`;
  return bpmSection+midiCard+chordCard;
}

function rerender(){if(_lastData)document.getElementById('content').innerHTML=render(_lastData);}

function doRestart(){
  if(!confirm('Restart all ORC-1 services?'))return;
  _restarting=true;rerender();
  cmd('RESTART_ORC');
  setTimeout(()=>{_restarting=false;},9000);
}

function _sendBpm(v){
  _bpm=v;
  const el=document.getElementById('bpm-display');
  if(el)el.textContent=v.toFixed(1);
  fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'SET_BPM',value:v})}).catch(()=>{});
}
function adjBpm(d){
  /* Snap to a whole number first, then step -- otherwise a fractional BPM
     (from TAP or external clock) leaves the decimal digit stuck forever. */
  _sendBpm(Math.max(20,Math.min(300,Math.round(_bpm)+d)));
}
function setBpmManual(){
  const cur=Math.round(_bpm)||120;
  const s=prompt('Set tempo (BPM, 20-300):',cur);
  if(s===null)return;               /* cancelled */
  const v=parseFloat(s);
  if(!isFinite(v))return;           /* junk -- leave tempo unchanged */
  _sendBpm(Math.max(20,Math.min(300,Math.round(v))));
}


/* ── PI HEALTH compact card ─────────────────────────────────────────────── */
async function _sysCompactPoll() {
  const el = document.getElementById('sys-compact-body');
  if (!el) return;
  try {
    const d = await fetch('/api/system/stats?' + Date.now()).then(r => r.json());
    if (!d.error) _sysRenderCompact(d, el);
  } catch (e) {
    el.innerHTML = '<span style="font-size:10px;color:var(--muted)">system service unavailable</span>';
  }
  setTimeout(_sysCompactPoll, 5000);
}
_sysCompactPoll();

/* ── SKIPBACK compact card (B1 — top-level status on PLAY page) ─────────── */
async function _skipbackCompactPoll() {
  const el = document.getElementById('skipback-compact-body');
  if (!el) return;
  try {
    const [sys, cfg] = await Promise.all([
      fetch('/api/system/stats?' + Date.now()).then(r => r.json()).catch(() => ({})),
      fetch('/api/skipback/config').then(r => r.json()).catch(() => ({}))
    ]);
    const svcs = sys.services || {};
    const recUp = svcs['skipback-record'];
    /* skipback-bridge retired at the multitrack cutover: undefined in stats
       = not monitored anymore (recorder subsumes it); only an explicit
       false (pre-cutover rollback state) still counts as down. */
    const brgUp = svcs['skipback-bridge'];
    const active = recUp && brgUp !== false;
    const srcLabel = (cfg.resolved && cfg.resolved.label) || '—';
    const dot    = active ? '<span style="color:var(--live)">●</span>'
                          : '<span style="color:var(--danger)">●</span>';
    const status = active ? 'RECORDING' : '<span style="color:var(--danger)">DOWN</span>';
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:11px">${dot}&nbsp;${status}&nbsp;&nbsp;<span style="color:var(--label);font-size:9px">SRC</span>&nbsp;<span style="color:var(--bright)">${srcLabel}</span></span>
        <a href="/sampler" style="font-size:9px;font-family:var(--fh);color:var(--accent);text-decoration:none;letter-spacing:.08em">SAMPLER →</a>
      </div>`;
  } catch(e) {
    el.innerHTML = '<span style="font-size:10px;color:var(--muted)">—</span>';
  }
  setTimeout(_skipbackCompactPoll, 6000);
}
_skipbackCompactPoll();

/* ── LEVELS card: master + per-engine volume ───────────────────────────────
   Four sliders that send the CH_CTRL volume CCs to the synth bus via SYNTH_CC
   — the SAME CCs the Fire's MASTER/POLY/CHIP/DRUM faders send, so the two are
   just two front-ends onto one control. Boot positions match orc1_config's
   SYNTH_VOL (= the engine's startup gains), so nothing changes until moved.
   Since 2026-07-29 the synth PERSISTS these four levels across a restart
   (P11), so /api/bus_levels is asked first and wins over the boot value — a
   slider that opened at its boot guess while the engine sat somewhere else
   would jump the bus the moment you touched it, which is the whole class of
   silent gain change P11 exists to stop.
   Note: live read-back still only tracks web-initiated changes
   (state.synth_cc); a move made on the Fire won't push its value back here
   (the Fire sends straight to the bus, not via SYNTH_CC) — it shows up on the
   next page load, via the persisted levels. */
const LEVELS_CH = 13;
const LEVELS = [
  {label:'MASTER', cc:7,  boot:76,  key:'master'},
  {label:'POLY',   cc:8,  boot:64,  key:'poly'},
  {label:'CHIP',   cc:9,  boot:44,  key:'chip'},
  {label:'DRUM',   cc:10, boot:64,  key:'drum'},
];
function _levelsBuild(){
  const el=document.getElementById('levels-body');
  if(!el) return;
  el.innerHTML='<div style="display:grid;grid-template-columns:auto 1fr auto;gap:6px 8px;align-items:center;font-size:11px">'+
    LEVELS.map(l=>`<span style="opacity:.7">${l.label}</span>`+
      `<input type="range" min="0" max="127" value="${l.boot}" id="lvl-${l.cc}" style="width:100%" oninput="_levelsSend(${l.cc},this.value)">`+
      `<span id="lvlv-${l.cc}" style="min-width:26px;text-align:right;color:var(--bright)">${l.boot}</span>`
    ).join('')+'</div>';
  _levelsSeed();
}
/* Seed from the synth's persisted levels. Read-only — never sends a CC, so
   opening the page can't itself change a gain. */
async function _levelsSeed(){
  try{
    const saved=await fetch('/api/bus_levels?'+Date.now()).then(r=>r.json());
    for(const l of LEVELS){
      const v=saved[l.key];
      if(v===undefined) continue;
      const inp=document.getElementById('lvl-'+l.cc);
      if(inp && document.activeElement!==inp){
        inp.value=v;
        const lab=document.getElementById('lvlv-'+l.cc); if(lab)lab.textContent=v;
      }
    }
  }catch(e){}
}
function _levelsSend(cc,val){
  const v=document.getElementById('lvlv-'+cc); if(v)v.textContent=val;
  cmd('SYNTH_CC',{ch:LEVELS_CH,cc:cc,value:parseInt(val,10)});
}
/* Read-back now comes from the synth's persisted levels rather than
   state.synth_cc. synth_cc only ever held web-initiated moves, so a level set
   on the Fire left the slider showing an older web value indefinitely — and
   touching it then jumped the bus. The persisted file is written by the synth
   itself within ~1.5s of ANY move (web, Fire, or knob), so it's the one source
   that tells the truth about all of them. A bus never moved is absent from the
   file and correctly keeps its boot position. */
async function _levelsPoll(){
  try{
    const saved=await fetch('/api/bus_levels?'+Date.now()).then(r=>r.json());
    for(const l of LEVELS){
      const val=saved[l.key];
      if(val===undefined) continue;
      const inp=document.getElementById('lvl-'+l.cc);
      if(inp && document.activeElement!==inp && String(inp.value)!==String(val)){
        inp.value=val;
        const v=document.getElementById('lvlv-'+l.cc); if(v)v.textContent=val;
      }
    }
  }catch(e){}
  setTimeout(_levelsPoll,2500);
}
_levelsBuild(); _levelsPoll();


async function poll(){
  if(_restarting){setTimeout(poll,REFRESH_MS);return;}
  try{
    const res=await fetch('/state?'+Date.now());
    const d=await res.json();
    const age=Date.now()/1000-(d.timestamp||0);
    document.getElementById('dot').className='dot '+(age<10?'live':age<60?'stale':'dead');
    document.getElementById('age').textContent=age<2?'LIVE':age<60?Math.round(age)+'s ago':Math.round(age/60)+'m ago';
    if(d.arp&&typeof d.arp.bpm==='number')_bpm=d.arp.bpm;
    _lastData=d;
    document.getElementById('content').innerHTML=render(d);
  }catch(e){
    document.getElementById('dot').className='dot dead';
    document.getElementById('age').textContent='NO DATA';
  }
  setTimeout(poll,REFRESH_MS);
}
poll();
