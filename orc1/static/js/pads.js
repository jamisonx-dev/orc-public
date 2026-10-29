// /pads -- per-device pad maps (orc1_padmap). GET /api/padmap gives the config
// (validated), the action choices, the modules with their LIVE /matrix channel, the
// connected input names and the brain's recent pad hits (learn mode). Every change
// POSTs the WHOLE config; the server + brain validate it with
// orc1_padmap.validate_config and the brain applies it live + saves it.
(function () {
  "use strict";
  var conf = null, actions = [], modules = [], devices = [], colors = [], sbTracks = [], ports = [];
  var cur = null, learn = false, lastSeq = null, saveTimer = null;

  // GM drum names, so "note 38" reads as "snare"
  var GM = {35:"kick 2",36:"kick",37:"side stick",38:"snare",39:"clap",40:"snare 2",
    41:"low floor tom",42:"closed hat",43:"floor tom",44:"pedal hat",45:"low tom",
    46:"open hat",47:"low-mid tom",48:"hi-mid tom",49:"crash",50:"high tom",51:"ride",
    52:"china",53:"ride bell",54:"tambourine",55:"splash",56:"cowbell",57:"crash 2",
    59:"ride 2"};

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "on") Object.keys(attrs.on).forEach(function (ev) {
        e.addEventListener(ev, attrs.on[ev]); });
      else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  function opt(v, t, sel) {
    var o = el("option", { value: v, text: t }); if (sel) o.selected = true; return o;
  }
  var keepUntil = 0;                 // an "added pad N" message isn't covered by the routine "saved"
  function flash(msg, err, ms) {
    if (!err && msg === "saved" && Date.now() < keepUntil) return;
    var f = $("pm-flash"); f.textContent = msg; f.className = err ? "err" : "";
    if (ms) keepUntil = Date.now() + ms;
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, ms || (err ? 6000 : 1500));
  }
  function keyName(k) {
    var p = k.split(":"), n = +p[1];
    if (p[0] === "note" && cur && conf.devices[cur] && conf.devices[cur].surface && n < 64)
      return { main: "pad " + n, sub: "row " + (Math.floor(n / 16) + 1) + " · col " + (n % 16 + 1) };   // a surface: note = pad
    return p[0] === "note" ? { main: "note " + n, sub: GM[n] || "" }
                           : { main: "CC " + n, sub: "" };
  }
  // a colour swatch the page draws from the LED levels (0-7) the firmware will use
  function swatch(rgb) {
    var s = el("span", { class: "pm-sw" });
    if (rgb) s.style.background = "rgb(" + rgb.map(function (v) { return Math.round(v * 255 / 7); }).join(",") + ")";
    return s;
  }
  function colorCell(e) {
    var box = el("div", { class: "pm-col" }), rgbOf = {};
    colors.forEach(function (c) { rgbOf[c.name] = c.rgb; });
    var sw = swatch(rgbOf[e.color]);
    var s = el("select", { title: "the pad's colour on the surface", on: { change: function () {
      if (s.value === "off") delete e.color; else e.color = s.value;
      sw.replaceWith(sw = swatch(rgbOf[e.color])); save(); renderGrid(); } } });
    s.appendChild(opt("off", "dark", !e.color));
    colors.forEach(function (c) { s.appendChild(opt(c.name, c.name, c.name === e.color)); });
    box.appendChild(sw); box.appendChild(s);
    return box;
  }
  function keySort(a, b) {
    var pa = a.split(":"), pb = b.split(":");
    return pa[0] === pb[0] ? +pa[1] - +pb[1] : (pa[0] === "note" ? -1 : 1);
  }

  // key-order-free JSON, so the server's (Python) copy compares equal to ours when it is
  function canon(v) {
    if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
    if (v && typeof v === "object")
      return "{" + Object.keys(v).sort().map(function (k) { return JSON.stringify(k) + ":" + canon(v[k]); }).join(",") + "}";
    return JSON.stringify(v);
  }

  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      fetch("/api/padmap", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify({ config: conf }) })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.errors && r.errors.length) flash("refused: " + r.errors.join("; "), true);
          else flash(r.ok ? "saved" : "ORC not reachable: not applied", !r.ok);
          // Take the server's copy ONLY when it differs (it refused or cleaned an entry), and
          // redraw then. Swapping `conf` for an equal copy without a redraw left every box on
          // the page editing the OLD objects: the next change in that pad was saved without it
          // and lost (2026-10-19, found while checking a pad's "To" -> output port).
          if (r.config && canon(r.config) !== canon(conf)) { conf = r.config; render(); }
          $("pm-src").textContent = "saved config";
        })
        .catch(function () { flash("error saving", true); });
    }, 250);
  }

  // ── device tabs + add ─────────────────────────────────────────────────────
  function renderTabs() {
    var t = $("pm-tabs"); t.innerHTML = "";
    var names = Object.keys(conf.devices);
    if (!names.length) t.appendChild(el("span", { class: "pm-help", style: "margin:0",
      text: "No device profiles yet. Add the device whose pads you want to map." }));
    names.forEach(function (n) {
      t.appendChild(el("button", { class: "btn" + (n === cur ? " on" : ""), type: "button",
        text: n, on: { click: function () { cur = n; render(); } } }));
    });
    var a = $("pm-adddev"); a.innerHTML = "";
    var seen = {};
    devices.forEach(function (d) {
      var base = String(d).split(":")[0].trim();
      if (!base || seen[base]) return; seen[base] = 1;
      a.appendChild(opt(base, base));
    });
    if (!a.options.length) a.appendChild(opt("", "(no MIDI inputs open)"));
  }

  // ── one pad: a header + one LABELLED line per setting, the label first ───
  // (2026-10-19, owner: "the formatting is confusing" -- the old row was a pile of
  // unlabelled dropdowns with "on" / "note" trailing AFTER their box and wrapping.)
  function line(label, kids, hint) {
    var d = el("div", { class: "pm-line" }, [el("span", { class: "fl", text: label })].concat(kids));
    if (hint) d.appendChild(el("small", { class: "pm-hint", text: hint }));
    return d;
  }
  function numInput(val, lo, hi, onch, title) {
    var i = el("input", { class: "num", type: "number", min: lo, max: hi, value: val,
      title: title || "", on: { change: function () {
        var v = parseInt(i.value, 10);
        if (isNaN(v)) return; v = Math.max(lo, Math.min(hi, v)); i.value = v; onch(v); } } });
    return i;
  }
  function pick(pairs, val, onch, title) {
    var s = el("select", { title: title || "", on: { change: function () { onch(s.value); } } });
    pairs.forEach(function (p) { s.appendChild(opt(p[0], p[1], p[0] === val)); });
    return s;
  }

  // where a note / CC goes: a NAMED ORC module (resolved live from /matrix), or an
  // OUTPUT PORT on its own channel (external gear has real channels)
  function destLines(e) {
    var s = el("select", { title: "where it goes", on: { change: function () {
      var v = s.value;
      if (v.indexOf("p:") === 0) { e.port = v.slice(2); if (!e.ch) e.ch = 1; delete e.module; }
      else { e.module = v.slice(2); delete e.port; delete e.ch; }
      save(); render(); } } });
    var g1 = el("optgroup", { label: "ORC modules" });
    modules.forEach(function (m) {
      g1.appendChild(opt("m:" + m.key, m.label + (m.ch ? "  (ch " + m.ch + ")" : "  (not bound)"),
                         !e.port && m.key === e.module));
    });
    s.appendChild(g1);
    if (ports.length) {
      var g2 = el("optgroup", { label: "Output ports" });
      ports.forEach(function (pk) { g2.appendChild(opt("p:" + pk, pk, e.port === pk)); });
      s.appendChild(g2);
    }
    var out = [line("To", [s])];
    if (e.port)
      out.push(line("Channel", [numInput(e.ch || 1, 1, 16, function (v) {
        if (v === 13) { flash("ch 13 is the ORC's control channel", true); render(); return; }
        e.ch = v; save(); })]));
    return out;
  }

  function whatCell(key, e) {
    var isNote = key.indexOf("note:") === 0, box = el("div", { class: "pm-what" });
    var surface = !!devConf().surface;
    var dos = isNote ? [["pass", "pass through"], ["ignore", "ignore"], ["note", "play a note"],
                        ["cc", "send a CC"], ["action", "action"], ["grab", "skipback grab"]]
                     : [["pass", "pass through"], ["ignore", "ignore"], ["cc", "send a CC"],
                        ["action", "action"]];
    var doSel = pick(dos, e.do || "pass", function (d) {
      var n = +key.split(":")[1];
      var keep = e.label ? { label: e.label } : {};
      if (d === "note") Object.assign(keep, { do: "note", module: firstMod("drums"), note: n, vel: "same" });
      else if (d === "cc") Object.assign(keep, isNote
        ? { do: "cc", module: firstMod("poly"), cc: 20, value: 127, mode: surface ? "momentary" : "press" }
        : { do: "cc", module: firstMod("poly"), cc: n });
      else if (d === "action") Object.assign(keep, { do: "action", action: actions[0][0] });
      else if (d === "grab") Object.assign(keep, { do: "grab", scope: "all", tracks: "armed",
                                                   len: "bars", bars: 4 });
      else keep.do = d;
      if (e.color) keep.color = e.color;          // a surface pad keeps its colour
      pads()[key] = keep; save(); render();
    }, "what this pad does");
    var lines = [line("Does", [doSel])];

    if (e.do === "note") {
      lines = lines.concat(destLines(e));
      lines.push(line("Note", [numInput(e.note, 0, 127, function (v) { e.note = v; save(); })]));
      var vk = [pick([["same", "as hit"], ["fixed", "fixed"]], e.vel === "same" ? "same" : "fixed",
                     function (v) { e.vel = v === "same" ? "same" : 100; save(); render(); })];
      if (e.vel !== "same") vk.push(numInput(e.vel, 1, 127, function (v) { e.vel = v; save(); }));
      lines.push(line("Velocity", vk));
    } else if (e.do === "cc") {
      lines = lines.concat(destLines(e));
      lines.push(line("CC number", [numInput(e.cc, 0, 127, function (v) { e.cc = v; save(); })]));
      if (!isNote) {
        lines.push(line("Value", [], "passes the knob's own value through"));
      } else {
        var vv = [pick([["vel", "hit velocity"], ["fixed", "fixed"]], e.value === "vel" ? "vel" : "fixed",
                       function (v) { e.value = v === "vel" ? "vel" : 127; save(); render(); })];
        if (e.value !== "vel") vv.push(numInput(e.value, 0, 127, function (v) { e.value = v; save(); }));
        lines.push(line("Value", vv));
        var modes = [["press", "press: send once"], ["toggle", "toggle: value / 0 each press"]];
        if (surface) modes.splice(1, 0, ["momentary", "momentary: value, 0 on release"]);
        lines.push(line("Behaviour", [pick(modes, e.mode || "press", function (v) { e.mode = v; save(); })]));
      }
    } else if (e.do === "action") {
      var as = pick(actions.map(function (a) { return [a[0], a[1]]; }), e.action,
                    function (v) { e.action = v; save(); render(); });
      lines.push(line("Action", [as]));
      lines.push(line(isNote ? "Min velocity" : "Fires at", [numInput(
        e.min_vel || (isNote ? devConf().min_vel : 64), 1, 127, function (v) { e.min_vel = v; save(); },
        isNote ? "softer hits than this don't fire" : "fires when the value rises past this")]));
      if (e.action === "cmd:TAP_TEMPO" && surface)
        lines.push(line("", [], "blinks the ORC clock's beat (beat 1 white; grey = following an outside clock)"));
    } else if (e.do === "grab") {
      lines = lines.concat(grabLines(e, surface));
    }
    lines.forEach(function (l) { box.appendChild(l); });
    return box;
  }

  // ── a skipback GRAB pad: what, how long, which tracks ──────────────────────
  function mmss(s) { return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }
  function grabLines(e, surface) {
    var out = [];
    var what = [["all", "all tracks (aligned)"]].concat(sbTracks.map(function (t) {
      return ["t:" + t.id, "one track: " + t.name + (t.armed ? "" : " (off)")]; }));
    out.push(line("Grab", [pick(what, e.scope === "track" ? "t:" + e.track : "all", function (v) {
      if (v === "all") { e.scope = "all"; e.tracks = "armed"; delete e.track; }
      else { e.scope = "track"; e.track = +v.slice(2); delete e.tracks; }
      save(); render(); }, "what to grab")]));

    var lens = [["bars", "bars"], ["time", "time (m:ss)"], ["all", "everything"]];
    if (surface) lens.splice(1, 0, ["hold", "while held"]);   // only the Fire sends a real release
    var lk = [pick(lens, e.len, function (v) {
      e.len = v; delete e.bars; delete e.secs; delete e.snap;
      if (e.len === "bars") e.bars = 4;
      else if (e.len === "time") e.secs = 60;
      else if (e.len === "hold") e.snap = true;
      save(); render(); }, "how long")];
    var hint = "";
    if (e.len === "bars") {
      lk.push(numInput(e.bars, 1, 256, function (v) { e.bars = v; save(); },
                       "bars at the ORC's live tempo, ending at your press"));
      hint = "at the live tempo, ending at your press";
    } else if (e.len === "time") {
      var t = el("input", { type: "text", value: mmss(e.secs || 60), style: "width:64px",
        title: "minutes:seconds, ending at your press (max 120:00)", on: { change: function () {
          var p = t.value.trim().split(":"), s = p.length > 1 ? (+p[0]) * 60 + (+p[1]) : +p[0];
          if (!isFinite(s) || s < 1) { t.value = mmss(e.secs || 60); return; }
          e.secs = Math.min(7200, Math.round(s)); t.value = mmss(e.secs); save(); } } });
      lk.push(t);
      hint = "minutes:seconds, ending at your press";
    } else if (e.len === "hold") {
      var sn = el("input", { type: "checkbox", on: { change: function () { e.snap = sn.checked; save(); } } });
      sn.checked = e.snap !== false;
      lk.push(el("label", { title: "round the hold to whole bars (from the press)" },
                 [sn, document.createTextNode(" snap to bars")]));
      hint = "press to start, release to grab";
    }
    out.push(line("Length", lk, hint));

    if (e.scope !== "track") {
      var tk = [pick([["armed", "armed when pressed"], ["fixed", "fixed tracks"]],
                     e.tracks === "armed" ? "armed" : "fixed", function (v) {
        e.tracks = v === "armed" ? "armed"
                 : sbTracks.filter(function (x) { return x.armed; }).map(function (x) { return x.id; });
        save(); render(); }, "which tracks")];
      if (e.tracks !== "armed") {
        sbTracks.forEach(function (x) {
          var cb = el("input", { type: "checkbox", on: { change: function () {
            var s = (e.tracks || []).filter(function (i) { return i !== x.id; });
            if (cb.checked) s.push(x.id);
            e.tracks = s.sort(function (a, b) { return a - b; });
            if (!e.tracks.length) e.tracks = "armed";
            save(); render(); } } });
          cb.checked = (e.tracks || []).indexOf(x.id) >= 0;
          tk.push(el("label", {}, [cb, document.createTextNode(" " + x.name)]));
        });
      }
      out.push(line("Tracks", tk));
    }
    return out;
  }
  function firstMod(pref) {
    for (var i = 0; i < modules.length; i++) if (modules[i].key === pref) return pref;
    return modules.length ? modules[0].key : pref;
  }

  function devConf() { return conf.devices[cur]; }
  function pads() { return devConf().pads; }

  function renderRows() {
    var tb = $("pm-rows"); tb.innerHTML = "";
    var keys = Object.keys(pads()).sort(keySort);
    $("pm-empty").hidden = keys.length > 0;
    keys.forEach(function (k) {
      var e = pads()[k], kn = keyName(k);
      var lab = el("input", { type: "text", value: e.label || "", placeholder: "name",
        class: "pm-name", maxlength: "24", title: "a name for this pad", on: { change: function () {
          if (lab.value.trim()) e.label = lab.value.trim(); else delete e.label; save(); renderGrid(); } } });
      var head = el("div", { class: "pm-head" }, [
        el("span", { class: "pm-key", text: kn.main }),
        kn.sub ? el("small", { class: "pm-sub", text: kn.sub }) : null,
        devConf().surface ? colorCell(e) : null,
        lab,
        el("button", { class: "btn pm-x", type: "button", text: "✕",
          title: devConf().surface ? "remove (the pad goes silent)" : "remove (the pad plays as normal again)",
          on: { click: function () {
            delete pads()[k]; save(); render(); } } })]);
      tb.appendChild(el("tr", { "data-key": k }, [
        el("td", { class: "pm-pad" }, [head, whatCell(k, e)])]));
    });
  }

  // ── adding pads: the Fire grid (click) and LEARN (hit) both land here ───────
  function addPad(key, how) {
    var isNew = !pads()[key];
    if (isNew) pads()[key] = { do: "pass", label: keyName(key).sub || key };
    if (isNew) { save(); render(); }
    var kn = keyName(key);
    flash((isNew ? "added " : "") + kn.main + (kn.sub ? " (" + kn.sub + ")" : "") +
          (isNew ? (how === "learn" ? ": choose what it does below" : ": choose what it does") : ""),
          false, isNew ? 4000 : 0);
    focusRow(key);
  }
  function focusRow(key) {
    var row = document.querySelector('#pm-rows tr[data-key="' + key + '"]');
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.add("new");
    setTimeout(function () { row.classList.remove("new"); }, 2500);
  }
  function renderGrid() {
    var wrap = $("pm-gridwrap"), g = $("pm-grid");
    var on = !!(cur && conf.devices[cur] && conf.devices[cur].surface);
    wrap.hidden = !on;
    if (!on) return;
    var rgbOf = {}; colors.forEach(function (c) { rgbOf[c.name] = c.rgb; });
    g.innerHTML = "";
    for (var n = 0; n < 64; n++) (function (n) {
      var key = "note:" + n, e = pads()[key], rgb = e && rgbOf[e.color];
      var b = el("button", { type: "button", "aria-label": "pad " + n,
        title: "pad " + n + " (row " + (Math.floor(n / 16) + 1) + " · col " + (n % 16 + 1) + ")" +
               (e ? ": " + (e.label || e.do) : ": not in the list, click to add"),
        on: { click: function () { addPad(key, "grid"); } } });
      if (rgb) { b.style.background = "rgb(" + rgb.map(function (v) { return Math.round(v * 255 / 7); }).join(",") + ")";
                 b.classList.add("lit"); }
      if (e) b.classList.add("mapped");
      g.appendChild(b);
    })(n);
  }

  function render() {
    renderTabs();
    var has = cur && conf.devices[cur];
    $("pm-dev").hidden = !has;
    renderGrid();
    if (!has) return;
    $("pm-match").value = devConf().port_match;
    $("pm-minvel").value = devConf().min_vel;
    $("pm-surface").checked = !!devConf().surface;
    $("pm-surfch-wrap").hidden = !devConf().surface;      // just SHOW ON FIRE: a surface takes no channel (fw v0.21)
    $("pm-learn").className = "btn" + (learn ? " on" : "");
    renderRows();
  }

  // ── hits: learn + flash the row that was hit ──────────────────────────────
  function onHits(hits) {
    if (lastSeq === null) {           // first poll: only remember where we are
      lastSeq = hits.length ? hits[hits.length - 1].seq : 0; return;
    }
    var fresh = hits.filter(function (h) { return h.seq > lastSeq; });
    if (hits.length && hits[hits.length - 1].seq < lastSeq) fresh = hits;  // brain restarted
    if (hits.length) lastSeq = hits[hits.length - 1].seq;
    var learned = [];
    fresh.forEach(function (h) {
      if (h.device !== cur) return;
      $("pm-last").textContent = "last: " + keyName(h.key).main + " @ " + h.value;
      // a "pass" entry with no label isn't kept by the validator, so a learned pad
      // gets its position as a placeholder label until you choose what it does
      if (learn && !pads()[h.key]) {
        pads()[h.key] = { do: "pass", label: keyName(h.key).sub || h.key };
        learned.push(h.key);
      }
      var row = document.querySelector('#pm-rows tr[data-key="' + h.key + '"]');
      if (row) { row.classList.add("hit"); setTimeout(function () { row.classList.remove("hit"); }, 150); }
    });
    if (learned.length) {
      // it used to land silently at the bottom of the list, dark and "pass through" (owner: "nothing")
      save(); render();
      var last = learned[learned.length - 1], kn = keyName(last);
      flash("added " + learned.map(function (k) { return keyName(k).main; }).join(", ") +
            (kn.sub ? " (" + kn.sub + ")" : "") + ": choose what it does below", false, 4000);
      focusRow(last);
    }
  }

  function load(first) {
    return fetch("/api/padmap").then(function (r) { return r.json(); }).then(function (j) {
      actions = j.actions || []; modules = j.modules || []; devices = j.devices || []; colors = j.colors || [];
      sbTracks = j.sb_tracks || [];
      ports = j.ports || [];
      if (first) {
        conf = j.config;
        cur = Object.keys(conf.devices)[0] || null;
        $("pm-src").textContent = j.saved ? "saved config" : "no pad maps yet";
        if (j.errors && j.errors.length) flash("saved file had problems: " + j.errors.join("; "), true);
        render();
      } else {
        renderTabs();
      }
      onHits(j.hits || []);
    }).catch(function () { $("pm-src").textContent = "server not reachable"; });
  }

  $("pm-add").addEventListener("click", function () {
    var n = $("pm-adddev").value;
    if (!n) return;
    if (!conf.devices[n]) conf.devices[n] = { port_match: n, min_vel: 1, pads: {} };
    cur = n; learn = true; save(); render();
    flash("added: LEARN is on, hit a pad");
  });
  $("pm-del").addEventListener("click", function () {
    if (!cur || !confirm('Remove the pad map for "' + cur + '"? Its pads pass through again.')) return;
    delete conf.devices[cur]; cur = Object.keys(conf.devices)[0] || null; save(); render();
  });
  $("pm-learn").addEventListener("click", function () { learn = !learn; render(); });
  $("pm-match").addEventListener("change", function () {
    var v = $("pm-match").value.trim(); if (!v) return render();
    devConf().port_match = v; save();
  });
  $("pm-surface").addEventListener("change", function () {
    if ($("pm-surface").checked) { devConf().surface = true; }
    else { delete devConf().surface; delete devConf().surface_ch; }
    save(); render();
  });
  $("pm-show").addEventListener("click", function () {
    fetch("/api/padmap/show", { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ device: cur }) })
      .then(function (r) { return r.json(); })
      .then(function (r) { flash(r.ok ? "sent to the Fire" : "ORC not reachable", !r.ok); })
      .catch(function () { flash("error", true); });
  });
  $("pm-minvel").addEventListener("change", function () {
    var v = Math.max(1, Math.min(127, parseInt($("pm-minvel").value, 10) || 1));
    devConf().min_vel = v; $("pm-minvel").value = v; save();
  });

  load(true);
  setInterval(function () { if (conf) load(false); }, 400);
})();
