/* PLAY page: the tempo card + the MORE fold-out (MIDI devices, DIN, MIRROR,
   NOTE THRU, LEVELS). SCENES live in scenes.js; the chord engine, the arp and
   CHORDS -> POLY / CHIP moved to the CHORD page (chord.js) on 2026-10-26.
   cmd() / flag() come from shared.js. */
const REFRESH_MS=1500;

let _bpm=120.0;
let _lastData=null;

function renderTempo(d){
  const arp=d.arp||{};
  /* Clock IN: internal (Pi makes its own clock) vs external (follow incoming
     MIDI clock). DRIFT can be set from the Fire but isn't offered here, so the
     web toggle only ever flips INT<->EXT. */
  const ext=arp.clock_mode==='external';
  const drift=arp.clock_mode==='drift';
  const inLabel=drift?'IN: DRIFT ⚠':ext?'IN: EXT':'IN: INT';
  const inNext=ext?'internal':'external';   /* DRIFT also flips to EXT */
  const bpmStr=typeof arp.bpm==='number'?arp.bpm.toFixed(1):'—';
  return `
  <div class="bpm-section">
    <div class="bpm-top">
      <span class="bpm-num" id="bpm-display" title="Tap to type a tempo" onclick="setBpmManual()">${bpmStr}</span>
      <span class="bpm-unit">BPM</span>
      <div class="bpm-flags">
        <button class="flag ${(ext||drift)?'warn':''}" title="Make the clock here, or follow incoming MIDI clock" onclick="cmd('SET_CLOCK_SRC',{mode:'${inNext}'})">${inLabel}</button>
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
}

/* MORE fold-out: the MIDI card (devices, RESCAN, DIN routing) plus MIRROR and
   NOTE THRU, which used to sit in the chord card. Same actions as before. */
function renderMore(d){
  const devs=d.midi_devices||[];
  const devHtml=devs.length
    ?'<ul class="dev-list">'+devs.map(n=>`<li>${n}</li>`).join('')+'</ul>'
    :'<div class="dev-none">No external devices</div>';
  const dinMode=d.din_routing==='engine';
  return `
  <div class="card blue">
    <div class="card-label"><span>MIDI</span>
      <button class="rescan-btn" onclick="cmd('MIDI_RESCAN')">RESCAN</button>
    </div>
    ${devHtml}
    <div class="flags">
      ${flag(dinMode?'DIN: ENGINE':'DIN: THRU','TOGGLE_DIN_ROUTING',dinMode)}
      ${flag('MIRROR','TOGGLE_MIRROR',d.mirror_input_ch)}
      ${flag(d.arp_passthrough?'NOTE THRU':'CHORD ENG','TOGGLE_ARP_PT',d.arp_passthrough,'warn')}
    </div>
  </div>`;
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

/* ── LEVELS card: master + per-engine volume ───────────────────────────────
   Four sliders that send the CH_CTRL volume CCs to the synth bus via SYNTH_CC
   — the SAME CCs the Fire's MASTER/POLY/CHIP/DRUM faders send, so the two are
   just two front-ends onto one control. Boot positions match orc1_config's
   SYNTH_VOL (= the engine's startup gains), so nothing changes until moved.
   The hosts PERSIST these levels across a restart, so /api/bus_levels is asked
   first and wins over the boot value — a slider that opened at its boot guess
   while the engine sat somewhere else would jump the bus the moment you
   touched it. The persisted file is written by the hosts within ~1.5 s of ANY
   move (web, Fire or knob), so polling it tracks Fire moves too. */
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
}
function _levelsSend(cc,val){
  const v=document.getElementById('lvlv-'+cc); if(v)v.textContent=val;
  cmd('SYNTH_CC',{ch:LEVELS_CH,cc:cc,value:parseInt(val,10)});
}
/* Read-only — never sends a CC, so opening the page can't change a gain. */
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
  try{
    const res=await fetch('/state?'+Date.now());
    const d=await res.json();
    const age=Date.now()/1000-(d.timestamp||0);
    document.getElementById('dot').className='dot '+(age<10?'live':age<60?'stale':'dead');
    document.getElementById('age').textContent=age<2?'LIVE':age<60?Math.round(age)+'s ago':Math.round(age/60)+'m ago';
    if(d.arp&&typeof d.arp.bpm==='number')_bpm=d.arp.bpm;
    _lastData=d;
    document.getElementById('content').innerHTML=renderTempo(d);
    const more=document.getElementById('more-midi');
    if(more)more.innerHTML=renderMore(d);
  }catch(e){
    document.getElementById('dot').className='dot dead';
    document.getElementById('age').textContent='NO DATA';
  }
  setTimeout(poll,REFRESH_MS);
}
poll();
