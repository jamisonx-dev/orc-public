#!/usr/bin/env python3
"""pgmlib_ui — browser UI for MPC1000/JJOS2XL .PGM and .PMD files.

Double-click "MPC Tool.bat" (or run: py pgmlib_ui.py). A local page opens:
pick a folder (/mnt/mpc when the MPC is mounted on the Pi), click a
file, edit pad mode on a visual grid, save. Every save first copies the
original into pgmlib/backups/.

Pi build. Serves on 0.0.0.0:8765 — accessible from the local network.
"""
import json, os, shutil, sys, time, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, 'orc1'))   # Pi: ~/orc/orc1/ (canonical pgmlib location)
sys.path.insert(0, os.path.dirname(_here))          # PC dev: parent of pgmlib/ dir
import pgmlib
from pgmlib import Program, PadMode, PatternBank, pad_label, note_name

PORT = 8765
BACKUPS = os.path.expanduser("~/backups/pgmlib")
DEFAULT_DIRS = ["/mnt/mpc", os.path.expanduser("~/recordings"), os.path.expanduser("~/orc/orc1/samples")]


def backup(path):
    os.makedirs(BACKUPS, exist_ok=True)
    dst = os.path.join(
        BACKUPS, f"{time.strftime('%Y%m%d_%H%M%S')}_{os.path.basename(path)}")
    shutil.copy2(path, dst)
    return dst


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/list":
                d = q.get("dir") or next(
                    (x for x in DEFAULT_DIRS if os.path.isdir(x)), ".")
                if d == "::roots":
                    roots = [r for r in DEFAULT_DIRS if os.path.isdir(r)]
                    self._send(200, {"dir": "::roots", "roots": roots,
                                     "dirs": [], "files": [], "wavs": 0,
                                     "exists": True})
                    return
                d = os.path.abspath(d)
                files, dirs, wavs = [], [], 0
                if os.path.isdir(d):
                    try:
                        entries = sorted(os.listdir(d), key=str.upper)
                    except OSError as e:
                        self._send(200, {"dir": d, "dirs": [], "files": [],
                                         "wavs": 0, "exists": False,
                                         "error": str(e)})
                        return
                    for f in entries:
                        fp = os.path.join(d, f)
                        if os.path.isdir(fp):
                            dirs.append(f)
                        else:
                            ext = os.path.splitext(f)[1].upper()
                            if ext in (".PGM", ".PMD", ".PAT"):
                                files.append({"name": f, "type": ext[1:]})
                            elif ext == ".WAV":
                                wavs += 1
                parent = os.path.dirname(d.rstrip("\\/"))
                self._send(200, {"dir": d, "dirs": dirs, "files": files,
                                 "wavs": wavs, "exists": os.path.isdir(d),
                                 "parent": parent if parent != d else None})
            elif u.path == "/api/pat":
                pb = PatternBank.load(q["path"])
                self._send(200, {"patterns": pb.patterns()})
            elif u.path == "/api/pgm":
                pg = Program.load(q["path"])
                pads = []
                for p in range(64):
                    s = pg.sample(p, 0)
                    pads.append({"note": pg.pad_note[p], "name": s["name"]})
                self._send(200, {"pads": pads,
                                 "inst": bool(pg.inst_flag),
                                 "version": pg.version})
            elif u.path == "/api/inspect":
                obj = pgmlib.open_any(q["path"])
                self._send(200, {"text": obj.inspect()})
            elif u.path == "/api/pmd":
                pm = PadMode.load(q["path"])
                slots = []
                for sl in range(PadMode.SLOTS):
                    pads = [pm.get(sl, p) for p in range(64)]
                    slots.append({"used": pm.slot_used(sl), "pads": pads})
                self._send(200, {"slots": slots})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._json_body()
            if u.path == "/api/pmd/save":
                path = b["path"]
                pm = PadMode.load(path)          # keeps undecoded bytes
                for sl, slot in enumerate(b["slots"]):
                    for p, e in enumerate(slot["pads"]):
                        pm.set(sl, p, type=e["type"], num=e["num"],
                               value=e["value"], flag=e["flag"])
                bk = backup(path)
                pm.save(path)
                self._send(200, {"ok": True, "backup": bk})
            elif u.path == "/api/pat/save":
                path = b["path"]
                if os.path.exists(path):
                    pb = PatternBank.load(path)      # preserves header bytes
                    bk = backup(path)
                else:
                    pb, bk = PatternBank.blank(), None
                pb.set_patterns(b["patterns"])
                pb.save(path)
                self._send(200, {"ok": True, "backup": bk})
            elif u.path == "/api/pgm/save-notes":
                path = b["path"]
                pg = Program.load(path)
                pg.set_pad_notes(b["notes"])
                bk = backup(path)
                pg.save(path)
                self._send(200, {"ok": True, "backup": bk})
            elif u.path == "/api/build":
                folder, kind = b["folder"], b.get("kind", "inst")
                name = pgmlib.sanitize_name(
                    b.get("name") or os.path.basename(folder.rstrip("\\/")))
                wavs = sorted(
                    os.path.join(folder, f) for f in os.listdir(folder)
                    if f.upper().endswith(".WAV"))
                if not wavs:
                    raise ValueError("no WAVs in that folder")
                if kind == "inst":
                    prog, notes, warns = pgmlib.build_inst(wavs)
                    info = (f"{len(notes)} samples, "
                            f"{note_name(notes[0])}..{note_name(notes[-1])}")
                else:
                    prog, warns = pgmlib.build_drum(wavs)
                    info = f"{len(wavs)} pads"
                out = os.path.join(folder, name + ".PGM")
                if os.path.exists(out):
                    backup(out)
                prog.save(out)
                self._send(200, {"ok": True, "out": out, "info": info,
                                 "warnings": warns})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>MPC Tool</title><style>
:root { --bg:#14161a; --panel:#1d2127; --edge:#2c323b; --tx:#d6dae2;
        --dim:#7d8590; --acc:#e8a33d; --cc:#4d9de0; --note:#68b36b;
        --pc:#c76fd1; --off:#262b33; }
* { box-sizing:border-box; font-family:'Segoe UI',system-ui,sans-serif; }
body { margin:0; background:var(--bg); color:var(--tx); display:flex;
       height:100vh; overflow:hidden; }
#side { width:270px; min-width:270px; background:var(--panel);
        border-right:1px solid var(--edge); padding:14px; overflow-y:auto; }
#main { flex:1; padding:18px 22px; overflow-y:auto; }
h1 { font-size:15px; margin:0 0 10px; color:var(--acc); letter-spacing:1px; }
h2 { font-size:13px; margin:16px 0 8px; color:var(--dim);
     text-transform:uppercase; letter-spacing:1px; }
input,select,button { background:var(--off); color:var(--tx);
  border:1px solid var(--edge); border-radius:6px; padding:6px 9px;
  font-size:13px; }
button { cursor:pointer; }
button:hover { border-color:var(--acc); }
button.primary { background:var(--acc); color:#14161a; font-weight:600; }
#dir { width:100%; margin-bottom:8px; }
.file { padding:6px 9px; border-radius:6px; cursor:pointer; font-size:13px;
        display:flex; justify-content:space-between; }
.file:hover { background:var(--off); }
.file.sel { background:var(--off); outline:1px solid var(--acc); }
.file .tag { color:var(--dim); font-size:11px; }
pre { background:var(--panel); border:1px solid var(--edge);
      border-radius:8px; padding:14px; font-size:12.5px; line-height:1.5;
      white-space:pre-wrap; }
#slots { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
#slots button.on { background:var(--acc); color:#14161a; }
.bank { margin-bottom:14px; }
.banklabel { color:var(--dim); font-size:11px; margin-bottom:4px; }
.grid { display:grid; grid-template-columns:repeat(8,1fr); gap:6px;
        max-width:720px; }
.pad { aspect-ratio:1.4; border-radius:8px; border:1px solid var(--edge);
       background:var(--off); cursor:pointer; padding:5px 7px; font-size:11px;
       display:flex; flex-direction:column; justify-content:space-between; }
.pad:hover { border-color:var(--tx); }
.pad.sel { outline:2px solid var(--acc); }
.pad .lbl { color:var(--dim); font-size:10px; }
.pad .what { font-weight:600; font-size:12px; }
.pad.t2 .what { color:var(--cc); } .pad.t0 .what { color:var(--note); }
.pad.t3 .what { color:var(--pc); }
#editor { position:sticky; top:0; background:var(--panel);
  border:1px solid var(--edge); border-radius:10px; padding:14px;
  margin-bottom:16px; display:none; }
#editor .row { display:flex; gap:10px; align-items:center; margin:8px 0;
               flex-wrap:wrap; }
#editor label { color:var(--dim); font-size:12px; min-width:52px; }
#editor input[type=number] { width:72px; }
#status { position:fixed; bottom:14px; right:16px; background:var(--panel);
  border:1px solid var(--acc); border-radius:8px; padding:9px 14px;
  font-size:13px; display:none; max-width:480px; }
.warn { color:var(--acc); font-size:12px; }
.hint { color:var(--dim); font-size:12px; line-height:1.5; }
#buildbox input { width:100%; margin:4px 0; }
table.seq { border-collapse:collapse; margin-top:6px; }
table.seq th { color:var(--dim); font-size:10px; font-weight:400;
               padding:0 0 4px; text-align:left; }
table.seq td.rowhdr { color:var(--dim); font-size:11px; white-space:nowrap;
                      padding-right:10px; }
td.cell { width:26px; height:26px; min-width:26px; border:1px solid var(--edge);
          border-radius:4px; cursor:pointer; background:var(--off);
          text-align:center; color:#14161a; font-size:12px; }
td.cell:hover { border-color:var(--tx); }
td.cell.beat { border-left:2px solid #3a4250; }
td.cell.bar, th.bar { border-left:2px solid var(--dim); }
td.cell.on { background:color-mix(in srgb, var(--acc) calc(var(--v)*100%), var(--off)); }
td.cell.sel { outline:2px solid #fff; }
</style></head><body>
<div id="side">
  <h1>MPC TOOL</h1>
  <input id="dir" placeholder="folder (e.g. /mnt/mpc)">
  <button onclick="loadDir()" style="width:100%">Open folder</button>
  <div id="files"></div>
  <h2>Build a program</h2>
  <div id="buildbox" class="hint">
    From a folder of WAVs.<br>
    <input id="bfolder" placeholder="WAV folder">
    <input id="bname" placeholder="program name (optional)">
    <select id="bkind" style="width:100%">
      <option value="inst">Instrument (multisample)</option>
      <option value="drum">Drum kit (one-shots)</option>
    </select>
    <button onclick="buildProg()" style="width:100%;margin-top:6px">Build .PGM</button>
    <div style="margin-top:6px">Instrument WAVs need note names in the
    filename (Bass-C2.wav) or an embedded root note.</div>
  </div>
</div>
<div id="main">
  <div id="welcome" class="hint">
    <h2>How this works</h2>
    Open the folder on the left (H:\AUTOLOAD appears automatically when the
    MPC is plugged in as USB storage).<br><br>
    <b style="color:var(--tx)">.PMD files</b> open as an editable pad grid —
    click a pad, set what it sends (note / CC / program change), save.
    A timestamped backup of the original is kept in pgmlib\backups\ on every
    save.<br><br>
    <b style="color:var(--tx)">.PGM files</b> open with an editable pad&rarr;note
    grid (this is where note assignments really live — verified on hardware)
    plus a readable inspection below.<br><br>
    <b style="color:var(--tx)">.PAT files</b> (pattern banks) open in a step
    sequencer — pads as rows, 16th-note steps as columns. Click a step to add
    a note, click it again to edit velocity/gate/nudge, save when happy.<br><br>
    Colors: <span style="color:var(--note)">note</span> ·
    <span style="color:var(--cc)">CC</span> ·
    <span style="color:var(--pc)">program change</span>
  </div>
  <div id="content"></div>
</div>
<div id="status"></div>
<script>
let cur = null, pmd = null, slot = 0, selPads = [];

function el(id){ return document.getElementById(id); }
function toast(msg, ms=3500){ const s = el('status');
  s.innerHTML = msg; s.style.display = 'block';
  clearTimeout(s._t); s._t = setTimeout(()=>s.style.display='none', ms); }
async function api(path, body){
  const r = await fetch(path, body ? {method:'POST', body:JSON.stringify(body)} : {});
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j; }

async function loadDir(dir){
  try {
    const d = dir !== undefined ? dir : el('dir').value;
    const j = await api('/api/list' + (d ? '?dir=' + encodeURIComponent(d) : ''));
    if (j.dir !== '::roots') el('dir').value = j.dir;
    if (!j.exists) { toast('Folder not readable — is the MPC plugged in?'); return; }
    let h = '';
    if (j.dir === '::drives'){
      h = (j.roots||[]).map(r =>
        `<div class="file" onclick="loadDir('${r}')">
         <span>&#128196; ${r}</span><span class="tag">root</span></div>`).join('');
    } else {
      h = `<div class="file" onclick="loadDir('::roots')">
           <span>&#128196; roots</span></div>`;
      if (j.parent)
        h += `<div class="file" onclick="loadDir('${j.parent.replace(/\\/g,'\\\\')}')">
              <span>&#8617; up one level</span></div>`;
      h += j.dirs.map(f =>
        `<div class="file" onclick="loadDir(el('dir').value.replace(/[\\\\\/]$/,'') + '\\\\' + '${f.replace(/'/g,"\\'")}')">
         <span>&#128193; ${f}</span></div>`).join('');
      h += j.files.map(f =>
        `<div class="file" onclick="openFile('${f.name.replace(/'/g,"\\'")}',this)">
         <span>${f.name}</span><span class="tag">${f.type}</span></div>`).join('');
      if (!j.files.length && !j.dirs.length)
        h += '<div class="hint" style="margin-top:8px">nothing here</div>';
      if (!el('bfolder').value && j.dir !== '::roots') el('bfolder').value = j.dir;
    }
    el('files').innerHTML = h;
  } catch(e){ toast('Error: ' + e.message); } }

async function openFile(name, div){
  document.querySelectorAll('.file').forEach(x=>x.classList.remove('sel'));
  if (div) div.classList.add('sel');
  cur = el('dir').value.replace(/\/$/,'') + '/' + name;
  el('welcome').style.display = 'none';
  if (name.toUpperCase().endsWith('.PMD')) return openPmd();
  if (name.toUpperCase().endsWith('.PAT')) return openPat();
  return openPgm(name); }

/* ── pattern bank editor ─────────────────────────────────────────────── */
let pat = null, patIdx = 0, patSel = null;   // patSel = index into events

async function openPat(){
  try {
    const j = await api('/api/pat?path=' + encodeURIComponent(cur));
    pat = j.patterns; patIdx = 0; patSel = null;
    renderPat();
  } catch(e){ toast('Error: ' + e.message); } }

function patRows(p){                 // distinct pads, sorted
  return [...new Set(p.events.map(e => e.pad))].sort((a,b)=>a-b); }

function renderPat(){
  const name = cur.split('/').pop();
  let h = `<h2>${name} — pattern bank</h2><div id="slots">`;
  pat.forEach((p, i) => {
    h += `<button class="${i===patIdx?'on':''}"
          onclick="patIdx=${i};patSel=null;renderPat()">${p.name||('#'+i)}</button>`; });
  h += `<button onclick="addPattern()" title="new pattern">+ new</button></div>`;
  if (!pat.length){
    h += '<div class="hint">empty bank — add a pattern</div>';
    el('content').innerHTML = h; return; }
  const p = pat[patIdx], steps = p.bars * 16;
  h += `<div class="row" style="margin-bottom:10px">
    <label class="hint">name</label>
    <input value="${p.name}" size="10" maxlength="8"
      onchange="pat[patIdx].name=this.value;renderPat()">
    <label class="hint">bars</label>
    <select onchange="pat[patIdx].bars=+this.value;renderPat()">
      ${[1,2,3,4,6,8].map(b=>`<option ${b===p.bars?'selected':''}>${b}</option>`).join('')}
    </select>
    <button onclick="dupPattern()">duplicate</button>
    <button onclick="delPattern()">delete pattern</button></div>
    <div id="editor"></div>`;
  const rows = patRows(p);
  h += '<div style="overflow-x:auto"><table class="seq"><tr><th></th>';
  for (let s = 0; s < steps; s++)
    h += `<th class="${s%16===0?'bar':(s%4===0?'beat':'')}">${s%16===0?(s/16+1):''}</th>`;
  h += '</tr>';
  rows.forEach(pad => {
    h += `<tr><td class="rowhdr">${padName(pad)} ${nname(36+pad)}
          <a href="#" onclick="delRow(${pad});return false" title="remove row"
          style="color:var(--dim)">&times;</a></td>`;
    for (let s = 0; s < steps; s++){
      const t0 = s*24, evs = p.events
        .map((e,i)=>({...e, idx:i}))
        .filter(e => e.pad===pad && e.time >= t0 && e.time < t0+24);
      const e = evs[0];
      const on = !!e, sel = on && patSel === e.idx;
      const off = on && e.time !== t0;
      h += `<td class="cell ${s%16===0?'bar':(s%4===0?'beat':'')}
            ${on?'on':''} ${sel?'sel':''}"
            ${on?`style="--v:${(e.vel/127).toFixed(2)}"`:''}
            onclick="cellClick(${pad},${s})">${off?'&#8250;':''}</td>`; }
    h += '</tr>'; });
  h += `</table></div>
    <div class="row" style="margin:8px 0">
      <label class="hint">add row:</label><select id="newrow">`;
  for (let i = 0; i < 64; i++)
    if (!rows.includes(i))
      h += `<option value="${i}">${padName(i)} ${nname(36+i)}</option>`;
  h += `</select><button onclick="addRow()">add pad row</button>
    <button class="primary" onclick="savePat()">SAVE bank</button>
    <span class="hint">&nbsp;backup kept &middot; click a step to add a note,
    click again to edit it</span></div>`;
  el('content').innerHTML = h;
  renderPatEd(); }

function cellClick(pad, s){
  const p = pat[patIdx], t0 = s*24;
  const idx = p.events.findIndex(e => e.pad===pad && e.time >= t0 && e.time < t0+24);
  if (idx < 0){
    p.events.push({time:t0, note:36+pad, vel:127, dur:24, pad});
    patSel = p.events.length - 1;
  } else if (patSel === idx){
    patSel = null;                       // deselect
  } else {
    patSel = idx;                        // select existing for editing
  }
  renderPat(); }

function renderPatEd(){
  const ed = el('editor');
  if (patSel === null || !ed){ if (ed) ed.style.display='none'; return; }
  const e = pat[patIdx].events[patSel];
  if (!e){ patSel = null; ed.style.display='none'; return; }
  ed.style.display = 'block';
  ed.innerHTML = `<b>${padName(e.pad)} @ ${1+Math.floor(e.time/384)}.${1+Math.floor(e.time%384/96)}.${String(e.time%96).padStart(2,'0')}</b>
    <div class="row">
      <label>velocity</label><input type="number" id="pv" min="1" max="127" value="${e.vel}">
      <label>gate (ticks)</label><input type="number" id="pg" min="0" max="3072" value="${e.dur}">
      <label>nudge (ticks)</label><input type="number" id="pt" min="-23" max="23" value="${e.time%24 > 12 ? e.time%24-24 : e.time%24}">
      <label>note</label><input type="number" id="pn" min="0" max="127" value="${e.note}">
    </div>
    <div class="row">
      <button class="primary" onclick="applyPatEd()">Apply</button>
      <button onclick="pat[patIdx].events.splice(patSel,1);patSel=null;renderPat()">Delete note</button>
      <span class="hint">nudge shifts off the 16th grid (&#8250; marker); 24 ticks = one 16th</span>
    </div>`; }

function applyPatEd(){
  const e = pat[patIdx].events[patSel];
  const grid = e.time - (e.time%24 > 12 ? e.time%24-24 : e.time%24);
  e.vel = Math.max(1, Math.min(127, +el('pv').value));
  e.dur = Math.max(0, +el('pg').value);
  e.note = Math.max(0, Math.min(127, +el('pn').value));
  e.time = Math.max(0, grid + (+el('pt').value));
  patSel = null; renderPat(); }

function addRow(){
  const pad = +el('newrow').value;
  pat[patIdx].events.push({time:0, note:36+pad, vel:127, dur:24, pad});
  patSel = pat[patIdx].events.length - 1;
  renderPat(); }

function delRow(pad){
  if (!confirm('Remove all ' + padName(pad) + ' notes from this pattern?')) return;
  pat[patIdx].events = pat[patIdx].events.filter(e => e.pad !== pad);
  patSel = null; renderPat(); }

function addPattern(){
  pat.push({name:'PAT' + (pat.length+1), flag:0, bars:1, events:[]});
  patIdx = pat.length - 1; patSel = null; renderPat(); }

function dupPattern(){
  const p = pat[patIdx];
  pat.splice(patIdx+1, 0, JSON.parse(JSON.stringify(p)));
  pat[patIdx+1].name = (p.name.slice(0,7) + '2');
  patIdx++; patSel = null; renderPat(); }

function delPattern(){
  if (!confirm('Delete pattern "' + pat[patIdx].name + '"?')) return;
  pat.splice(patIdx, 1);
  patIdx = Math.max(0, patIdx-1); patSel = null; renderPat(); }

async function savePat(){
  try {
    const j = await api('/api/pat/save', {path: cur, patterns: pat});
    toast('Saved &check;' + (j.backup ? ' &nbsp;backup: ' +
      j.backup.split('/').pop() : ''), 5000);
  } catch(e){ toast('Save failed: ' + e.message); } }

let pgm = null, selNote = null;

async function openPgm(name){
  try {
    const [j, ins] = await Promise.all([
      api('/api/pgm?path=' + encodeURIComponent(cur)),
      api('/api/inspect?path=' + encodeURIComponent(cur))]);
    pgm = j; selNote = null;
    renderPgm(name, ins.text);
  } catch(e){ toast('Error: ' + e.message); } }

function nname(n){
  const N = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
  return N[n % 12] + (Math.floor(n / 12) - 2); }

function renderPgm(name, inspectText){
  let h = `<h2>${name} — ${pgm.inst ? 'INSTRUMENT' : 'DRUM'} program
           &middot; pad &rarr; MIDI note</h2>
           <div class="hint" style="margin-bottom:10px">Click a pad to change
           which MIDI note triggers it. This is the note info the MPC reads
           (PMD note-type pads just play the pad — the mapping lives here).</div>
           <div id="editor"></div>`;
  for (let b = 0; b < 4; b++){
    h += `<div class="bank"><div class="banklabel">BANK ${'ABCD'[b]}</div>
          <div class="grid">`;
    for (let i = 0; i < 16; i++){
      const p = b*16 + i, e = pgm.pads[p];
      h += `<div class="pad ${e.name ? 't0' : ''} ${selNote === p ? 'sel' : ''}"
            onclick="selNote=${p};renderPgm('${name.replace(/'/g,"\\'")}',window._ins);renderNoteEd()">
            <span class="lbl">${'ABCD'[b]}${String(i+1).padStart(2,'0')}</span>
            <span class="what">${e.note} ${nname(e.note)}</span>
            <span class="lbl">${e.name || ''}</span></div>`; }
    h += '</div></div>'; }
  h += `<div class="row" style="margin:10px 0">
        <button class="primary" onclick="savePgmNotes()">SAVE note map</button>
        <button onclick="chromatic()">Reset chromatic 36-99</button>
        <span class="hint">&nbsp;backup kept automatically</span></div>`;
  h += '<h2>inspection</h2><pre>' + inspectText.replace(/</g,'&lt;') + '</pre>';
  window._ins = inspectText;
  el('content').innerHTML = h;
  renderNoteEd(); }

function renderNoteEd(){
  const ed = el('editor');
  if (selNote === null || !ed){ if (ed) ed.style.display = 'none'; return; }
  ed.style.display = 'block';
  const e = pgm.pads[selNote];
  ed.innerHTML = `<b>Pad ${padName(selNote)}</b>
    ${e.name ? '&nbsp;<span class="hint">(' + e.name + ')</span>' : ''}
    <div class="row"><label>MIDI note</label>
    <input type="number" id="pnote" min="0" max="127" value="${e.note}"
      oninput="el('pnotename').textContent = nname(+this.value)">
    <span class="hint" id="pnotename">${nname(e.note)}</span>
    <button class="primary" onclick="pgm.pads[${selNote}].note = +el('pnote').value;
      selNote = null; renderPgm(cur.split('/').pop(), window._ins)">Apply</button>
    </div>`; }

function chromatic(){
  pgm.pads.forEach((e, p) => e.note = 36 + p);
  renderPgm(cur.split('\\').pop(), window._ins); }

async function savePgmNotes(){
  try {
    const j = await api('/api/pgm/save-notes',
      {path: cur, notes: pgm.pads.map(e => e.note)});
    toast('Saved &check; &nbsp;backup: ' + j.backup.split('\\').pop(), 5000);
  } catch(e){ toast('Save failed: ' + e.message); } }

async function openPmd(){
  try {
    const j = await api('/api/pmd?path=' + encodeURIComponent(cur));
    pmd = j.slots; slot = pmd.findIndex(s=>s.used); if (slot<0) slot = 0;
    selPads = []; renderPmd();
  } catch(e){ toast('Error: ' + e.message); } }

function padDesc(e){
  if (e.type===2) return 'CC ' + e.num + '&rarr;' + e.value;
  if (e.type===3) return 'PC ' + e.num;
  if (e.type===0) return (e.num||e.value||e.flag) ? 'note' : '';
  return 't' + e.type; }

function renderPmd(){
  const name = cur.split('\\').pop();
  let h = '<h2>' + name + ' — pad mode editor</h2><div id="slots">';
  for (let s=0; s<10; s++)
    h += `<button class="${s===slot?'on':''}" onclick="slot=${s};selPads=[];renderPmd()">
          slot ${s}${pmd[s].used?' &bull;':''}</button>`;
  h += '</div><div id="editor"></div>';
  for (let b=0; b<4; b++){
    h += `<div class="bank"><div class="banklabel">BANK ${'ABCD'[b]}
          &nbsp;<a href="#" onclick="selectBank(${b});return false"
          style="color:var(--dim)">select all</a></div><div class="grid">`;
    for (let i=0; i<16; i++){
      const p = b*16+i, e = pmd[slot].pads[p];
      h += `<div class="pad t${e.type} ${selPads.includes(p)?'sel':''}"
            onclick="clickPad(${p},event)">
            <span class="lbl">${'ABCD'[b]}${String(i+1).padStart(2,'0')}
            ${e.flag?'&#9873;':''}</span>
            <span class="what">${padDesc(e)}</span></div>`; }
    h += '</div></div>'; }
  h += `<div class="row" style="margin-top:10px">
        <button class="primary" onclick="savePmd()">SAVE to file</button>
        <span class="hint">&nbsp;backup kept automatically &middot;
        click pads to select (ctrl-click for several), then edit above</span></div>`;
  el('content').innerHTML = h;
  renderEditor(); }

function selectBank(b){
  selPads = Array.from({length:16},(_,i)=>b*16+i); renderPmd(); }

function clickPad(p, ev){
  if (ev.ctrlKey || ev.metaKey){
    selPads = selPads.includes(p) ? selPads.filter(x=>x!==p) : [...selPads,p];
  } else selPads = [p];
  renderPmd(); }

function renderEditor(){
  const ed = el('editor');
  if (!selPads.length){ ed.style.display='none'; return; }
  ed.style.display = 'block';
  const e = pmd[slot].pads[selPads[0]];
  const many = selPads.length > 1;
  ed.innerHTML = `
   <b>${many ? selPads.length + ' pads selected' : 'Pad ' + padName(selPads[0])}</b>
   <div class="row"><label>sends</label>
     <select id="etype">
       <option value="0" ${e.type===0?'selected':''}>note (plays pad — mapping set in PGM)</option>
       <option value="2" ${e.type===2?'selected':''}>CC</option>
       <option value="3" ${e.type===3?'selected':''}>program change</option>
     </select></div>
   <div class="row"><label>number</label>
     <input type="number" id="enum" min="0" max="127" value="${e.num}">
     <label>value</label>
     <input type="number" id="evalue" min="0" max="127" value="${e.value}">
     <label><input type="checkbox" id="eflag" ${e.flag?'checked':''}>
       flag <span class="hint">(meaning TBD — verify on MPC)</span></label></div>
   ${many ? `<div class="row"><label>ramp</label>
     <input type="number" id="rlo" value="0" min="0" max="127">
     <span class="hint">to</span>
     <input type="number" id="rhi" value="127" min="0" max="127">
     <button onclick="applyEdit(true)">Apply with value ramp</button></div>` : ''}
   <div class="row"><button class="primary" onclick="applyEdit(false)">
     Apply${many?' to all selected':''}</button>
     <button onclick="clearPads()">Clear pad${many?'s':''}</button></div>`; }

function padName(p){ return 'ABCD'[p>>4] + String((p&15)+1).padStart(2,'0'); }

function applyEdit(ramp){
  const t = +el('etype').value, n = +el('enum').value,
        v = +el('evalue').value, f = el('eflag').checked ? 1 : 0;
  selPads.sort((a,b)=>a-b).forEach((p,i)=>{
    let val = v;
    if (ramp){
      const lo = +el('rlo').value, hi = +el('rhi').value;
      val = Math.round(lo + (hi-lo) * i / Math.max(selPads.length-1,1)); }
    pmd[slot].pads[p] = {type:t, num:n, value:val, flag:f}; });
  renderPmd(); }

function clearPads(){
  selPads.forEach(p => pmd[slot].pads[p] = {type:0,num:0,value:0,flag:0});
  renderPmd(); }

async function savePmd(){
  try {
    const j = await api('/api/pmd/save', {path:cur, slots:pmd});
    toast('Saved &check; &nbsp;backup: ' + j.backup.split('\\').pop(), 5000);
  } catch(e){ toast('Save failed: ' + e.message); } }

async function buildProg(){
  try {
    const j = await api('/api/build', {folder:el('bfolder').value,
      name:el('bname').value, kind:el('bkind').value});
    let msg = 'Built ' + j.out.split('\\').pop() + ' (' + j.info + ')';
    if (j.warnings.length)
      msg += '<br><span class="warn">' + j.warnings.join('<br>') + '</span>';
    toast(msg, 9000); loadDir();
  } catch(e){ toast('Build failed: ' + e.message); } }

loadDir();
</script></body></html>"""


def main():
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://0.0.0.0:{PORT}/"
    print(f"MPC Tool → http://pibox.local:{PORT}/ (ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
