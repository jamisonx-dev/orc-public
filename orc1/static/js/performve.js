// Perform-VE control surface. Write-only: the box does not report state over MIDI,
// so controls SEND CC/PC (FINDINGS.md §3c "performve tools") and do not read back.
(function () {
  "use strict";
  const flash = document.getElementById("pve-flash");
  let flashT = null;
  function note(msg, err) {
    flash.textContent = msg; flash.className = err ? "err" : "";
    clearTimeout(flashT); flashT = setTimeout(() => { flash.textContent = ""; }, 1800);
  }
  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { note(j.error || "send failed", true); return false; }
      return true;
    } catch (e) { note("brain unreachable", true); return false; }
  }
  const cc = (n, v) => post("/api/performve_cc", { cc: n, value: v });
  const pc = (p) => post("/api/performve_pc", { preset: p });

  // ── value lists for the style selectors ───────────────────────────────────
  const XFX   = ["Stutter", "Mono Chopper", "Stereo Chopper", "Ring Mod",
                 "Stereo Flanger", "Mono Flanger", "SideChain Pump"];
  const DBL   = ["Unison", "Octave Down", "Octave Up", "Oct Up/Down"];
  const FILT  = ["LPF/HPF", "Radio", "Megaphone", "Guitar Amp", "Distortion"];
  const MORPH = ["Natural Shift", "Warp Shift", "Vocoder 1", "Vocoder 2", "Vocoder 3",
                 "Vocoder 4", "Vocoder 5", "Vocoder 6", "Vocoder 7", "Vocoder 8", "Vocoder 9"];
  const ECHO  = ["Div 0", "Div 1", "Div 2", "Div 3", "Div 4", "Div 5", "Div 6", "Div 7",
                 "Div 8", "Div 9", "Div 10", "Div 11", "Slapback"];
  const REV   = ["Style 1", "Style 2", "Style 3", "Style 4"];
  const KEY   = ["NaturalPlay", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B", "Chromatic"];
  const SMODE = ["Re-Trigger", "Legato", "Loop Re-Trig", "Loop Legato"];

  // ── effect blocks: enable CC (or null), then controls ─────────────────────
  // control kinds: {sel:cc, opts:[...]}  {sld:cc, max, fmt?}
  const BLOCKS = [
    { nm: "HARDTUNE", en: 53, ctl: [
      { lb: "Key / scale", sel: 19, opts: KEY },
      { lb: "Amount", sld: 20 } ] },
    { nm: "DOUBLE", en: 51, ctl: [
      { lb: "Style", sel: 17, opts: DBL },
      { lb: "Level", sld: 45 } ] },
    { nm: "MORPH", en: 52, ctl: [
      { lb: "Style", sel: 24, opts: MORPH },
      { lb: "Mode (0-12 poly rel / 13-25 mono porta)", sld: 23, max: 25 },
      { lb: "Shift (semitones)", sld: 43, max: 72, fmt: v => (Math.round(v) - 36) + " st" },
      { lb: "Gender", sld: 44 } ] },
    { nm: "XFX", en: 54, ctl: [
      { lb: "Style", sel: 16, opts: XFX },
      { lb: "Mod 1", sld: 21 },
      { lb: "Mod 2", sld: 22 } ] },
    { nm: "ECHO / DELAY", en: 55, ctl: [
      { lb: "Delay Div", sel: 27, opts: ECHO },
      { lb: "Delay amount", sld: 46 },
      { lb: "Reverb style", sel: 28, opts: REV },
      { lb: "Reverb amount", sld: 47 } ] },
    { nm: "FILTER", en: 56, ctl: [
      { lb: "Style", sel: 18, opts: FILT },
      { lb: "Filter Mod", sld: 48 } ] },
    { nm: "SAMPLE", en: 80, ctl: [
      { lb: "Mode", sel: 25, opts: SMODE } ] },
    { nm: "VOICE / MIX", en: null, ctl: [
      { lb: "Lead level", sld: 41 },
      { lb: "MIDI / harmony level", sld: 42 },
      { lb: "Vibrato (mod wheel)", sld: 1 },
      { lb: "Notes voice smoothing", sld: 26 },
      { lb: "Envelope attack", sld: 73 },
      { lb: "Envelope release", sld: 72 } ] },
  ];

  const TRIGGERS = [
    { lb: "Kick", cc: 81 }, { lb: "Snare", cc: 82 }, { lb: "Hi-Hat", cc: 83 },
    { lb: "Sample REC", cc: 58 }, { lb: "Sample PLAY", cc: 59 }, { lb: "Sustain", cc: 64 },
  ];

  // ── audio insert (route a module through the box) ─────────────────────────
  const insSel = document.getElementById("pve-ins-src");
  const insBtn = document.getElementById("pve-ins-on");
  const insNote = document.getElementById("pve-ins-note");
  let insOn = false, insSrc = null;
  function paintIns() {
    insBtn.classList.toggle("on", insOn);
    insBtn.textContent = insOn ? "ON" : "OFF";
    if (insSrc) insSel.value = insSrc;
  }
  async function loadIns() {
    try {
      const r = await fetch("/api/performve_insert"); const j = await r.json();
      insSel.innerHTML = "";
      (j.sources || []).forEach(s => { const o = document.createElement("option");
        o.value = s; o.textContent = s.replace("ORC1-", ""); insSel.appendChild(o); });
      insOn = !!j.on; insSrc = j.source || (j.sources && j.sources[0]) || null;
      insNote.textContent = j.bridge_up ? "" : "audio bridge starts when switched on";
      insBtn.disabled = false;
      paintIns();
    } catch (e) { insNote.textContent = "unavailable"; }
  }
  async function sendIns() {
    const body = { on: insOn, source: insSel.value || null };
    if (insOn) note("starting bridge…");
    try {
      const r = await fetch("/api/performve_insert", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok || !j.ok) { note(j.error || "insert failed", true); insOn = !insOn; paintIns(); return; }
      insOn = !!j.on; insSrc = j.source; paintIns();
      note(insOn ? ("insert: " + (insSrc || "").replace("ORC1-", "")) : "insert off");
    } catch (e) { note("brain unreachable", true); insOn = !insOn; paintIns(); }
  }
  insBtn.onclick = () => { insOn = !insOn; paintIns(); sendIns(); };
  insSel.onchange = () => { if (insOn) sendIns(); };
  loadIns();

  // ── presets ───────────────────────────────────────────────────────────────
  const pres = document.getElementById("pve-presets");
  for (let i = 0; i < 3; i++) {
    const b = document.createElement("button");
    b.className = "btn"; b.type = "button"; b.textContent = "PRESET " + (i + 1);
    b.onclick = () => { pc(i); note("preset " + (i + 1)); };
    pres.appendChild(b);
  }

  // ── effect blocks ──────────────────────────────────────────────────────────
  const host = document.getElementById("pve-blocks");
  for (const blk of BLOCKS) {
    const el = document.createElement("div"); el.className = "pve-blk";
    const hd = document.createElement("div"); hd.className = "pve-blk-hd";
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = blk.nm;
    hd.appendChild(nm);
    if (blk.en != null) {
      const en = document.createElement("button");
      en.className = "pve-en"; en.type = "button"; en.textContent = "OFF";
      let on = false;
      en.onclick = () => {
        on = !on; en.classList.toggle("on", on); en.textContent = on ? "ON" : "OFF";
        cc(blk.en, on ? 127 : 0);
      };
      hd.appendChild(en);
    }
    el.appendChild(hd);
    for (const c of blk.ctl) {
      const w = document.createElement("div"); w.className = "pve-ctl";
      const lb = document.createElement("label"); lb.textContent = c.lb; w.appendChild(lb);
      if (c.sel) {
        const s = document.createElement("select");
        c.opts.forEach((o, i) => { const op = document.createElement("option");
          op.value = i; op.textContent = i + " · " + o; s.appendChild(op); });
        s.onchange = () => cc(c.sel, +s.value);
        w.appendChild(s);
      } else {
        const row = document.createElement("div"); row.className = "row";
        const r = document.createElement("input"); r.type = "range";
        r.min = 0; r.max = c.max || 127; r.value = 0;
        const v = document.createElement("span"); v.className = "val";
        const fmt = c.fmt || (x => x);
        v.textContent = fmt(0);
        let t = null;
        r.oninput = () => {
          v.textContent = fmt(+r.value);
          clearTimeout(t); t = setTimeout(() => cc(c.sld, +r.value), 40);
        };
        row.appendChild(r); row.appendChild(v); w.appendChild(row);
      }
      el.appendChild(w);
    }
    host.appendChild(el);
  }

  // ── triggers ────────────────────────────────────────────────────────────────
  const trig = document.getElementById("pve-trig");
  for (const t of TRIGGERS) {
    const b = document.createElement("button");
    b.className = "btn"; b.type = "button"; b.textContent = t.lb;
    b.onclick = () => { cc(t.cc, 127); note(t.lb + " 127"); };
    trig.appendChild(b);
  }
})();
