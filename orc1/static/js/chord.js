/* CHORD page (2026-10-26): the chord engine, the arpeggiator and
   CHORDS -> POLY / CHIP, moved off PLAY unchanged (same actions, same state
   fields). cmd() / flag() / nudgeRow() come from shared.js. */
const REFRESH_MS=1500;
/* Fallback only. The live list comes from the engine (state.scales) so this
   picker can't drift as scales are added -- see scaleList() below. */
const SCALE_NAMES=["MAJ","MIN","HMIN","MMIN","DOR","PHR","LYD","MIX","LOC","CHROM"];
function scaleList(d){
  return (d && Array.isArray(d.scales) && d.scales.length) ? d.scales : SCALE_NAMES;
}

let _rootOpen=false;
let _scaleOpen=false;
let _lastData=null;

function render(d){
  const arp=d.arp||{};
  const root=d.root??0;
  const curScale=d.scale||'CHROM';
  const extsHtml=(d.chord_exts||[]).map(e=>`<span class="ext-pill">${e}</span>`).join('');

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

  /* ── key lock: pulls an off-key press into the key (O3, 2026-07-29). */
  const klLabel=d.key_lock?'KEY LOCK ✓':'KEY LOCK';
  const klTip='Moves a chord played outside the key onto the nearest in-key note';
  /* O4: SCALE SNAP is a five-way mode (off/filter/up/down/nearest), so the
     button cycles and names the mode it is in. Falls back to the legacy
     boolean if the engine predates scale_mode. */
  const snapMode=d.scale_mode||(d.scale_snap?'nearest':'off');
  const snapLabel=snapMode==='off'?'SNAP OFF':'SNAP '+snapMode.toUpperCase();
  const snapTips={
    off:'Off - every note plays as pressed',
    filter:'Off-scale notes are SILENCED rather than moved',
    up:'Off-scale notes round UP to the next note in the scale',
    down:'Off-scale notes round DOWN to the previous note in the scale',
    nearest:'Off-scale notes round to whichever scale note is closer'};
  const snapTip='Fire poly/chip keyboards. '+(snapTips[snapMode]||'')+' (click to cycle)';

  /* O4: where the chord quality comes from. DIAT = the scale degree; FIXED =
     the quality pads are a persistent setting. base_override is a MOMENTARY
     override inside DIAT only. */
  const qMode=d.quality_mode||'DIAT';
  const qLabel=qMode==='FIXED'?'QUALITY FIXED':'QUALITY DIAT';
  const qTip=qMode==='FIXED'
    ?'The MAJ/MIN/SUS/DIM pads are a persistent setting. Click for DIAT.'
    :'Chord quality comes from the scale degree; a quality pad overrides it until the next key press. Click to latch FIXED.';

  /* Chord-quality badge: chord_base_eff (what actually sounds), NOT
     chord_base (the last explicit override, never reset). Can be any shape the
     scale produced (AUG, maj7, m7b5, 6no5) or a roman numeral. */
  const effBase=d.chord_base_eff||d.chord_base;
  const baseBadge=(effBase&&effBase!=='MAJ'&&effBase!=='--')
    ?`<div class="chord-base" title="${qMode==='FIXED'?'FIXED mode - the quality pads are a persistent setting':d.base_override?'Manual override (until next key press)':'From the scale degree'}">${effBase}${(qMode!=='FIXED'&&d.base_override)?'*':''}</div>`
    :'';

  /* Key + degree, so the page says WHY a chord is the quality it is. Hidden on
     degree I of the key, where it would just restate the chord root. */
  const degStr=(d.chord_degree!=null)
    ?['I','II','III','IV','V','VI','VII','VIII'][d.chord_degree]||'' : '';
  const keyBadge=(degStr&&d.chord_root_name&&d.chord_root_name!==d.root_name)
    ?`<div class="chord-base" title="Degree ${degStr} of ${d.root_name} ${d.scale||''}">${d.root_name}:${degStr}</div>`
    :'';

  /* ── ARP controls ───────────────────────────────── */
  const arpSection=arp&&Object.keys(arp).length?`
  <div class="card yellow">
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

  /* ── chord engine card ───────────────────────────── */
  const chordCard=`
  <div class="card yellow">
    <div class="card-label">CHORD ENGINE</div>
    <div class="chord-row">
      <div class="chord-left">
        <div class="chord-main">
          <!-- The chord's OWN root, not the key (root_name is the KEY tonic).
               Tapping opens the key picker (that is what ROOT_SET sets). -->
          <div class="chord-root" onclick="_rootOpen=!_rootOpen;rerender()"
               title="Chord root. Tap to change the KEY (currently ${d.root_name||'?'}).">${d.chord_root_name||d.root_name||'?'}</div>
          <div class="chord-info">
            ${baseBadge}${keyBadge}
            <div class="chord-exts">${extsHtml}</div>
          </div>
        </div>
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
      </div>
    </div>
    <div class="chord-sub">
      <div class="sub-item" style="width:100%">
        <div class="sub-lbl">SEND CHORDS TO</div>
        <div class="flags">
          ${flag('CHORDS → POLY','TOGGLE_SYNTH_MIRROR_POLY',d.synth_mirror_poly)}
          ${flag('CHORDS → CHIP','TOGGLE_SYNTH_MIRROR_CHIP',d.synth_mirror_chip)}
        </div>
      </div>
    </div>
  </div>`;

  return chordCard+arpSection;
}

function rerender(){if(_lastData)document.getElementById('content').innerHTML=render(_lastData);}

async function poll(){
  try{
    const res=await fetch('/state?'+Date.now());
    const d=await res.json();
    const age=Date.now()/1000-(d.timestamp||0);
    document.getElementById('dot').className='dot '+(age<10?'live':age<60?'stale':'dead');
    document.getElementById('age').textContent=age<2?'LIVE':age<60?Math.round(age)+'s ago':Math.round(age/60)+'m ago';
    _lastData=d;
    document.getElementById('content').innerHTML=render(d);
  }catch(e){
    document.getElementById('dot').className='dot dead';
    document.getElementById('age').textContent='NO DATA';
  }
  setTimeout(poll,REFRESH_MS);
}
poll();
