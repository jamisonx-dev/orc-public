// /pads -- per-device pad maps (orc1_padmap). GET /api/padmap gives the config
// (validated), the action choices, the modules with their LIVE /matrix channel, the
// connected input names and the brain's recent pad hits (learn mode). Every change
// POSTs the WHOLE config; the server + brain validate it with
// orc1_padmap.validate_config and the brain applies it live + saves it.
(function () {
  "use strict";
  var conf = null, actions = [], modules = [], devices = [], colors = [];
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
  function flash(msg, err) {
    var f = $("pm-flash"); f.textContent = msg; f.className = err ? "err" : "";
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, err ? 6000 : 1500);
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
      sw.replaceWith(sw = swatch(rgbOf[e.color])); save(); } } });
    s.appendChild(opt("off", "dark", !e.color));
    colors.forEach(function (c) { s.appendChild(opt(c.name, c.name, c.name === e.color)); });
    box.appendChild(sw); box.appendChild(s);
    return box;
  }
  function keySort(a, b) {
    var pa = a.split(":"), pb = b.split(":");
    return pa[0] === pb[0] ? +pa[1] - +pb[1] : (pa[0] === "note" ? -1 : 1);
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
          if (r.config) { conf = r.config; }
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

  // ── one pad row ───────────────────────────────────────────────────────────
  function moduleSelect(val, onch) {
    var s = el("select", { on: { change: function () { onch(s.value); } } });
    modules.forEach(function (m) {
      s.appendChild(opt(m.key, m.label + (m.ch ? "  (ch " + m.ch + ")" : "  (not bound)"),
                        m.key === val));
    });
    return s;
  }
  function numInput(val, lo, hi, onch, title) {
    var i = el("input", { class: "num", type: "number", min: lo, max: hi, value: val,
      title: title || "", on: { change: function () {
        var v = parseInt(i.value, 10);
        if (isNaN(v)) return; v = Math.max(lo, Math.min(hi, v)); i.value = v; onch(v); } } });
    return i;
  }

  function whatCell(key, e) {
    var isNote = key.indexOf("note:") === 0, box = el("div", { class: "pm-what" });
    var dos = isNote ? [["pass", "pass through"], ["ignore", "ignore"], ["note", "play note"],
                        ["action", "action"]]
                     : [["pass", "pass through"], ["ignore", "ignore"], ["cc", "send CC"],
                        ["action", "action"]];
    var doSel = el("select", { on: { change: function () {
      var d = doSel.value, n = +key.split(":")[1];
      var keep = e.label ? { label: e.label } : {};
      if (d === "note") Object.assign(keep, { do: "note", module: firstMod("drums"), note: n, vel: "same" });
      else if (d === "cc") Object.assign(keep, { do: "cc", module: firstMod("poly"), cc: n });
      else if (d === "action") Object.assign(keep, { do: "action", action: actions[0][0] });
      else keep.do = d;
      pads()[key] = keep; save(); render();
    } } });
    dos.forEach(function (d) { doSel.appendChild(opt(d[0], d[1], d[0] === (e.do || "pass"))); });
    box.appendChild(doSel);
    if (e.do === "note") {
      box.appendChild(el("span", { text: "on" }));
      box.appendChild(moduleSelect(e.module, function (v) { e.module = v; save(); }));
      box.appendChild(el("span", { text: "note" }));
      box.appendChild(numInput(e.note, 0, 127, function (v) { e.note = v; save(); }));
      var vs = el("select", { title: "velocity", on: { change: function () {
        e.vel = vs.value === "same" ? "same" : 100; save(); render(); } } });
      vs.appendChild(opt("same", "vel: as hit", e.vel === "same"));
      vs.appendChild(opt("fixed", "vel: fixed", e.vel !== "same"));
      box.appendChild(vs);
      if (e.vel !== "same")
        box.appendChild(numInput(e.vel, 1, 127, function (v) { e.vel = v; save(); }));
    } else if (e.do === "cc") {
      box.appendChild(el("span", { text: "to" }));
      box.appendChild(moduleSelect(e.module, function (v) { e.module = v; save(); }));
      box.appendChild(el("span", { text: "CC" }));
      box.appendChild(numInput(e.cc, 0, 127, function (v) { e.cc = v; save(); }));
    } else if (e.do === "action") {
      var as = el("select", { on: { change: function () { e.action = as.value; save(); } } });
      actions.forEach(function (a) { as.appendChild(opt(a[0], a[1], a[0] === e.action)); });
      box.appendChild(as);
      box.appendChild(el("span", { text: isNote ? "min vel" : "fires at" }));
      box.appendChild(numInput(e.min_vel || (isNote ? devConf().min_vel : 64), 1, 127,
        function (v) { e.min_vel = v; save(); },
        isNote ? "softer hits than this don't fire" : "fires when the value rises past this"));
    }
    return box;
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
      var lab = el("input", { type: "text", value: e.label || "", placeholder: kn.sub,
        style: "width:92px", maxlength: "24", on: { change: function () {
          if (lab.value.trim()) e.label = lab.value.trim(); else delete e.label; save(); } } });
      tb.appendChild(el("tr", { "data-key": k }, [
        el("td", { class: "key" }, [document.createTextNode(kn.main + " "),
                                    el("small", { text: kn.sub }),
                                    devConf().surface ? colorCell(e) : null]),   // in the PAD cell: a 5th column overflowed the 484 px page
        el("td", { class: "lab" }, [lab]),
        el("td", {}, [whatCell(k, e)]),
        el("td", {}, [el("button", { class: "btn", type: "button", text: "✕",
          title: "remove (the pad passes through again)", on: { click: function () {
            delete pads()[k]; save(); render(); } } })])
      ]));
    });
  }

  function render() {
    renderTabs();
    var has = cur && conf.devices[cur];
    $("pm-dev").hidden = !has;
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
    var added = false;
    fresh.forEach(function (h) {
      if (h.device !== cur) return;
      $("pm-last").textContent = "last: " + keyName(h.key).main + " @ " + h.value;
      if (learn && !pads()[h.key]) { pads()[h.key] = { do: "pass", label: "" }; added = true; }
      var row = document.querySelector('#pm-rows tr[data-key="' + h.key + '"]');
      if (row) { row.className = "hit"; setTimeout(function () { row.className = ""; }, 150); }
    });
    if (added) {
      // a "pass" entry with no label isn't kept by the validator, so learned pads
      // get a placeholder label until you choose what they do
      Object.keys(pads()).forEach(function (k) {
        var e = pads()[k]; if (e.do === "pass" && !e.label) e.label = keyName(k).sub || k;
      });
      save(); render();
      fresh.forEach(function (h) {
        var row = document.querySelector('#pm-rows tr[data-key="' + h.key + '"]');
        if (row) { row.className = "hit"; setTimeout(function () { row.className = ""; }, 150); }
      });
    }
  }

  function load(first) {
    return fetch("/api/padmap").then(function (r) { return r.json(); }).then(function (j) {
      actions = j.actions || []; modules = j.modules || []; devices = j.devices || []; colors = j.colors || [];
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
