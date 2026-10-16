// /pedal -- FCB1010 config. GET /api/pedal gives the config (brain-validated shape,
// JSON string keys), the dropdown choices and the port list. Every change POSTs the
// WHOLE config back; the server + brain validate it with orc1_pedal.validate_config
// and the brain applies it live + saves ~/.orc1_pedal.json.
(function () {
  "use strict";
  var conf = null, choices = [], prChoices = [], ports = [], modes = {}, CTRL = 13;
  var tab = { ef: 0, notes: 0 };
  var saveTimer = null;

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
  function flash(msg, err) {
    var f = $("pd-flash"); if (!f) return;
    f.textContent = msg; f.className = err ? "err" : "";
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, err ? 6000 : 1500);
  }

  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      fetch("/api/pedal", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ config: conf }) })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.errors && r.errors.length) flash("refused: " + r.errors.join("; "), true);
          else flash(r.ok ? "saved" : "ORC not reachable: not applied", !r.ok);
          $("pd-src").textContent = "saved config";
        })
        .catch(function () { flash("error saving", true); });
    }, 250);
  }

  // a <select> of switch actions; keeps an unknown cmd: value as its own option
  function actionSelect(list, value, onChange, blankLabel) {
    var s = el("select", { on: { change: function () {
      var v = s.value;
      if (v === "__custom") {
        var name = (window.prompt("ORC command name (e.g. SEQ_PLAY):") || "").trim().toUpperCase();
        if (!name) { s.value = value || ""; return; }
        v = "cmd:" + name;
      }
      onChange(v);
    } } });
    var seen = false;
    list.forEach(function (c) {
      var o = el("option", { value: c[0], text: c[0] === "" && blankLabel ? blankLabel : c[1] });
      if (c[0] === (value || "")) { o.selected = true; seen = true; }
      s.appendChild(o);
    });
    if (!seen && value) s.appendChild(el("option", { value: value, text: value, selected: "selected" }));
    s.appendChild(el("option", { value: "__custom", text: "other ORC command…" }));
    if (!seen && value) s.value = value;
    return s;
  }

  // ---- EF / SP: pages ------------------------------------------------------
  function renderMode(mode) {
    var m = conf.map[mode], pages = m.pages, tabs = $("pd-" + mode + "-tabs");
    if (tab[mode] >= pages.length) tab[mode] = pages.length - 1;
    tabs.innerHTML = "";
    pages.forEach(function (_, i) {
      tabs.appendChild(el("button", { "class": "btn" + (i === tab[mode] ? " on" : ""),
        text: "PAGE " + (i + 1), on: { click: function () { tab[mode] = i; renderMode(mode); } } }));
    });
    tabs.appendChild(el("button", { "class": "btn", text: "+ PAGE", on: { click: function () {
      pages.push({}); tab[mode] = pages.length - 1; renderMode(mode); save(); } } }));
    if (pages.length > 1) tabs.appendChild(el("button", { "class": "btn warn",
      text: "− PAGE " + (tab[mode] + 1), on: { click: function () {
        if (!window.confirm("Delete page " + (tab[mode] + 1) + "?")) return;
        pages.splice(tab[mode], 1); renderMode(mode); save(); } } }));

    var t = $("pd-" + mode); t.innerHTML = "";
    t.appendChild(el("tr", {}, [el("th", { text: "SW" }), el("th", { text: "EVERY PAGE" }),
                                el("th", { text: "PAGE " + (tab[mode] + 1) + " (overrides)" })]));
    var all = m.all || (m.all = {}), page = pages[tab[mode]];
    for (var n = 1; n <= 10; n++) {
      if (modes[mode].indexOf(n) < 0) {
        t.appendChild(el("tr", { "class": "dim" }, [el("td", { "class": "sw", text: n }),
          el("td", { colspan: "2", text: "relay jack: sends no MIDI" })]));
        continue;
      }
      (function (n) {
        var k = String(n);
        var cAll = actionSelect(choices, all[k], function (v) {
          if (v) all[k] = v; else delete all[k]; renderMode(mode); save(); });
        var inheritLbl = all[k] ? "— same as every page —" : "— nothing —";
        var cPage = actionSelect(choices, page[k], function (v) {
          if (v) page[k] = v; else delete page[k]; renderMode(mode); save(); }, inheritLbl);
        t.appendChild(el("tr", {}, [el("td", { "class": "sw", text: n }),
                                     el("td", {}, [cAll]), el("td", {}, [cPage])]));
      })(n);
    }
  }

  // ---- Pr: dialled channel -> action ----------------------------------------
  function renderPr() {
    var pr = conf.map.pr || (conf.map.pr = {}), t = $("pd-pr"); t.innerHTML = "";
    t.appendChild(el("tr", {}, [el("th", { text: "CH" }), el("th", { text: "THE NUMBER…" })]));
    var list = prChoices.concat([["fwd", "forward to a MIDI port"]]);
    for (var ch = 1; ch <= 16; ch++) {
      (function (ch) {
        var k = String(ch), v = pr[k] || "", isFwd = v.indexOf("fwd:") === 0;
        var sel = el("select", { on: { change: function () {
          if (sel.value === "fwd") pr[k] = "fwd:" + (ports[0] || "pisound") + ":1";
          else if (sel.value) pr[k] = sel.value; else delete pr[k];
          renderPr(); save(); } } });
        list.forEach(function (c) {
          var o = el("option", { value: c[0], text: c[1] });
          if (c[0] === (isFwd ? "fwd" : v)) o.selected = true;
          sel.appendChild(o);
        });
        var cell = el("td", {}, [sel]);
        if (isFwd) {
          var parts = v.split(":"), pSel = el("select", {}), cIn;
          ports.forEach(function (p) {
            var o = el("option", { value: p, text: p }); if (p === parts[1]) o.selected = true;
            pSel.appendChild(o); });
          cIn = el("input", { "class": "num", type: "number", min: "1", max: "16", value: parts[2] });
          function upd() {
            var c = parseInt(cIn.value, 10);
            if (!(c >= 1 && c <= 16) || c === CTRL) { flash("channel 1-16, not " + CTRL, true); return; }
            pr[k] = "fwd:" + pSel.value + ":" + c; save();
          }
          pSel.addEventListener("change", upd); cIn.addEventListener("change", upd);
          cell.appendChild(el("div", { "class": "pd-fwd" },
            [el("span", { text: "port" }), pSel, el("span", { text: "on ch" }), cIn]));
        }
        t.appendChild(el("tr", {}, [el("td", { "class": "sw", text: ch }), cell]));
      })(ch);
    }
  }

  // ---- expression pedals ------------------------------------------------------
  function renderExp() {
    var box = $("pd-exp"); box.innerHTML = "";
    ["A", "B"].forEach(function (name) {
      var t = conf.exp[name];
      var pSel = el("select", {});
      [["", "off"], ["synth", "ORC synth modules"]].concat(ports.map(function (p) { return [p, p]; }))
        .forEach(function (c) {
          var o = el("option", { value: c[0], text: c[1] });
          if (c[0] === (t ? t.port : "")) o.selected = true;
          pSel.appendChild(o); });
      var chIn = el("input", { "class": "num", type: "number", min: "1", max: "16",
                               value: t ? t.ch : 1 });
      var ccIn = el("input", { "class": "num", type: "number", min: "0", max: "127",
                               value: t ? t.cc : (name === "A" ? 11 : 1) });
      function upd() {
        if (!pSel.value) { conf.exp[name] = null; renderExp(); save(); return; }
        var ch = parseInt(chIn.value, 10), cc = parseInt(ccIn.value, 10);
        if (!(ch >= 1 && ch <= 16) || ch === CTRL) { flash("channel 1-16, not " + CTRL, true); return; }
        if (!(cc >= 0 && cc <= 127)) { flash("CC 0-127", true); return; }
        conf.exp[name] = { port: pSel.value, ch: ch, cc: cc }; renderExp(); save();
      }
      [pSel, chIn, ccIn].forEach(function (x) { x.addEventListener("change", upd); });
      var on = !!t;
      box.appendChild(el("div", { "class": "pd-row" }, [
        el("span", { "class": "lbl", text: "Pedal " + name + (name === "A" ? " (left)" : " (right)") }),
        pSel,
        el("span", { text: "ch", style: on ? "" : "opacity:.4" }), chIn,
        el("span", { text: "CC", style: on ? "" : "opacity:.4" }), ccIn]));
    });
  }

  function renderChords() {
    $("pd-style").value = conf.chord_style;
    $("pd-beats").value = conf.stab_beats;
    $("pd-oct").value = conf.chord_octave;
  }

  function renderAll() {
    renderMode("ef"); renderMode("notes"); renderPr(); renderExp(); renderChords();
  }

  function load() {
    fetch("/api/pedal").then(function (r) { return r.json(); }).then(function (d) {
      conf = d.config; choices = d.switch_choices; prChoices = d.pr_choices;
      ports = d.ports; modes = d.modes; CTRL = d.ctrl_ch;
      $("pd-src").textContent = d.saved ? "saved config" : "defaults (nothing saved yet)";
      if (d.errors && d.errors.length) flash("saved file had problems: " + d.errors.join("; "), true);
      renderAll();
    }).catch(function () { flash("could not load /api/pedal", true); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("pd-style").addEventListener("change", function () { conf.chord_style = this.value; save(); });
    $("pd-beats").addEventListener("change", function () {
      conf.stab_beats = parseFloat(this.value) || 1; save(); });
    $("pd-oct").addEventListener("change", function () {
      conf.chord_octave = parseInt(this.value, 10) || 4; save(); });
    load();
  });
})();
