// /kikpad -- the KIKPAD's OWN pad + encoder maps (stored on the pad). GET /api/kikpad = what the
// BRAIN last read back from the pad (/tmp/orc1_kikpad_map.json); POST /api/kikpad {op: pad|encoder|
// bank_channel|reset|read} queues a KIKPAD_MAP_* command. The brain sends the SysEx and republishes
// what the PAD replied, so this page always shows the pad's truth, never what we hoped.
(function () {
  "use strict";
  var st = null, bank = 0, sel = null, mode = 0, pending = null, pendingLabel = "", jobSeq = 0;
  var NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  var TYPE_LABEL = { off: "nothing", note: "note", cc: "CC (127 / 0)", toggle: "toggle CC" };

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "on") Object.keys(attrs.on).forEach(function (ev) { e.addEventListener(ev, attrs.on[ev]); });
      else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  function noteName(n) { return NOTES[n % 12] + (Math.floor(n / 12) - 1); }   // 60 = C4 (48 = C3, as the pad's comments)
  function flash(msg, err) {
    var f = $("kp-flash");
    f.textContent = msg; f.className = err ? "err" : "";
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, err ? 8000 : 2500);
  }
  function ready() { return !!(st && st.connected && st.supported); }
  function chOptions(selEl, withBank, value) {
    selEl.innerHTML = "";
    if (withBank) selEl.appendChild(el("option", { value: "bank", text: "bank channel" }));
    for (var c = 1; c <= 16; c++) selEl.appendChild(el("option", { value: String(c), text: "ch " + c }));
    selEl.value = String(value);
  }

  function post(body, label) {
    pending = "w" + Date.now().toString(36) + "-" + (++jobSeq);
    pendingLabel = label;
    body.job = pending;
    render();
    return fetch("/api/kikpad", { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return [r, j]; }); })
      .then(function (rj) {
        if (!rj[0].ok || !rj[1].ok) { pending = null; flash(rj[1].error || "the brain did not take it", true); render(); }
        else poll();
      })
      .catch(function () { pending = null; flash("no answer from the web server", true); render(); });
  }

  // ---- drawing ----------------------------------------------------------------
  function renderStatus() {
    var dot = $("kp-dot"), conn = $("kp-conn"), saved = $("kp-saved");
    dot.className = "kp-dot";
    saved.textContent = "";
    if (!st || st.missing) { conn.textContent = "the brain has not seen the pad yet (plug it in)"; return; }
    if (!st.connected) { conn.textContent = "not connected"; return; }
    if (st.supported === false) {
      dot.className = "kp-dot part";
      conn.textContent = "connected, but its firmware has no map editor (flash kikpad_remap.bin)";
      return;
    }
    if (st.busy || st.supported === null) { dot.className = "kp-dot part"; conn.textContent = pending ? pendingLabel + "…" : "reading the pad…"; }
    else { dot.className = "kp-dot on"; conn.textContent = "connected"; }
    if (st.dirty === true) saved.textContent = "saving on the pad…";
    else if (st.dirty === false) saved.textContent = "✓ saved on the pad";
    if (st.error && !st.busy) { conn.textContent += " · " + st.error; }
  }

  function padLabel(p) {
    if (!p) return ["?", ""];
    var top = p.type === "note" ? noteName(p.number) : p.type === "cc" ? "CC" + p.number
            : p.type === "toggle" ? "TG" + p.number : "";
    return [top, p.channel === "bank" ? "" : String(p.channel)];
  }

  function renderBanks() {
    var tabs = $("kp-banks"); tabs.innerHTML = "";
    (st && st.bank_names || ["Bank 0", "Bank 1"]).forEach(function (name, b) {
      tabs.appendChild(el("button", { "class": "btn" + (b === bank ? " on" : ""), type: "button", text: name,
        on: { click: function () { bank = b; sel = null; render(); } } }));
    });
    var bch = $("kp-bankch"), m = st && st.map && st.map.banks[bank];
    chOptions(bch, false, m && m.channel ? m.channel : 1);
    bch.disabled = !ready() || !!pending;
    bch.onchange = function () { post({ op: "bank_channel", bank: bank, channel: +bch.value }, "setting the bank channel"); };
    $("kp-bankch-note").textContent = bank === 1 ? "(its MS1-8 buttons still switch it live; this is the channel it starts on)" : "";
  }

  function renderGrid() {
    var g = $("kp-grid"); g.innerHTML = "";
    var pads = st && st.map && st.map.banks[bank] ? st.map.banks[bank].pads : [];
    for (var i = 0; i < 64; i++) {
      var p = pads[i], lab = padLabel(p);
      var b = el("button", { "class": "kp-pad" + (!p || p.type === "off" ? " off" : "") + (i === sel ? " sel" : ""),
                             type: "button", title: "pad " + (i + 1) + " (row " + (Math.floor(i / 8) + 1) + ", column " + (i % 8 + 1) + ")" },
                 [el("span", { text: lab[0] }), lab[1] ? el("span", { "class": "ch", text: "ch " + lab[1] }) : null]);
      if (p && p.type !== "off") b.style.background = p.rgb;
      b.disabled = !ready();
      (function (idx) { b.addEventListener("click", function () { sel = idx; render(); }); })(i);
      g.appendChild(b);
    }
  }

  function renderEditor() {
    var box = $("kp-edit");
    var p = sel !== null && st && st.map && st.map.banks[bank] ? st.map.banks[bank].pads[sel] : null;
    if (!p || !ready()) { box.hidden = true; return; }
    box.hidden = false; box.innerHTML = "";
    var dis = !!pending;
    var t = el("select", {}), n = el("input", { type: "number", min: "0", max: "127", value: String(p.number) });
    (st.types || ["off", "note", "cc", "toggle"]).forEach(function (k) { t.appendChild(el("option", { value: k, text: TYPE_LABEL[k] || k })); });
    t.value = p.type;
    var c = el("select", {}); chOptions(c, true, p.channel);
    var nn = el("span", { "class": "kp-name", text: p.type === "note" ? noteName(p.number) : "" });
    t.disabled = n.disabled = c.disabled = dis;
    function send(colour) {
      var num = Math.max(0, Math.min(127, parseInt(n.value, 10) || 0));
      post({ op: "pad", bank: bank, pad: sel, type: t.value, number: num,
             channel: c.value === "bank" ? "bank" : +c.value, colour: colour === undefined ? p.colour : colour },
           "setting pad " + (sel + 1));
    }
    t.onchange = function () { send(); };
    c.onchange = function () { send(); };
    n.onchange = function () { send(); };
    n.oninput = function () { var v = parseInt(n.value, 10); nn.textContent = t.value === "note" && v >= 0 && v <= 127 ? noteName(v) : ""; };
    box.appendChild(el("h4", { text: "BANK " + bank + " · PAD " + (sel + 1) + " (ROW " + (Math.floor(sel / 8) + 1) + ", COLUMN " + (sel % 8 + 1) + ")" }));
    box.appendChild(el("div", { "class": "kp-row" }, [
      el("label", {}, [document.createTextNode("Sends"), t]),
      el("label", {}, [document.createTextNode("Number"), n, nn]),
      el("label", {}, [document.createTextNode("Channel"), c])]));
    var pal = el("div", { "class": "kp-pal" });
    (st.palette || []).forEach(function (rgb, k) {
      var sw = el("button", { "class": "kp-sw" + (k === p.colour ? " sel" : ""), type: "button", title: "colour " + k });
      sw.style.background = rgb; sw.disabled = dis;
      sw.addEventListener("click", function () { send(k); });
      pal.appendChild(sw);
    });
    box.appendChild(el("div", { "class": "kp-name", text: "Colour (as the pad shows it; a toggle that is on shows white)" }));
    box.appendChild(pal);
  }

  function renderEncoders() {
    var tabs = $("kp-modes"); tabs.innerHTML = "";
    (st && st.modes || []).forEach(function (name, m) {
      tabs.appendChild(el("button", { "class": "btn" + (m === mode ? " on" : ""), type: "button", text: name,
        on: { click: function () { mode = m; render(); } } }));
    });
    var tb = $("kp-enc"); tb.innerHTML = "";
    tb.appendChild(el("tr", {}, [el("th", { text: "Encoder" }), el("th", { text: "CC" }), el("th", { text: "Channel" })]));
    var encs = st && st.map && st.map.encoders ? st.map.encoders[mode] : [];
    for (var e = 0; e < 8; e++) {
      var q = encs[e];
      var cc = el("input", { type: "number", min: "0", max: "127", value: q ? String(q.cc) : "" });
      var ch = el("select", {}); chOptions(ch, false, q ? q.channel : 1);
      cc.disabled = ch.disabled = !ready() || !q || !!pending;
      (function (idx, ccEl, chEl) {
        function send() {
          post({ op: "encoder", mode: mode, encoder: idx, cc: Math.max(0, Math.min(127, parseInt(ccEl.value, 10) || 0)),
                 channel: +chEl.value }, "setting encoder " + (idx + 1));
        }
        ccEl.onchange = send; chEl.onchange = send;
      })(e, cc, ch);
      tb.appendChild(el("tr", {}, [el("td", { text: String(e + 1) }), el("td", {}, [cc]), el("td", {}, [ch])]));
    }
  }

  function render() {
    renderStatus();
    renderBanks();
    renderGrid();
    renderEditor();
    renderEncoders();
    $("kp-refresh").disabled = !(st && st.connected) || !!pending;
    Array.prototype.forEach.call(document.querySelectorAll("[data-reset]"), function (b) { b.disabled = !ready() || !!pending; });
  }

  // ---- polling ------------------------------------------------------------------
  var timer = null;
  function poll() {
    clearTimeout(timer);
    fetch("/api/kikpad").then(function (r) { return r.json(); }).then(function (j) {
      st = j;
      if (pending && st.last && st.last.job === pending && !st.busy) {
        if (st.last.ok) flash("✓ the pad took it"); else flash(st.last.error || "the pad did not answer", true);
        pending = null;
      }
      render();
    }).catch(function () {}).then(function () {
      timer = setTimeout(poll, pending || (st && (st.busy || st.dirty)) ? 400 : 1500);
    });
  }

  $("kp-refresh").addEventListener("click", function () { post({ op: "read" }, "reading the pad"); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-reset]"), function (b) {
    b.addEventListener("click", function () {
      if (!confirm(b.textContent + "? The pad goes back to its factory map for this.")) return;
      post({ op: "reset", what: b.getAttribute("data-reset") }, b.textContent.toLowerCase());
    });
  });
  render();
  poll();
})();
