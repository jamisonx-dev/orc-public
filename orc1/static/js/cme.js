// /cme -- the CME U6MIDI Pro. GET /api/cme = what the BRAIN last read off the box
// (~/.orc1_cme.json); POST /api/cme {what: preset|route|refresh} queues a CME_*
// command. The brain writes, reads back and republishes; this page just polls, so
// it always shows what the box says, never what we hoped it says.
(function () {
  "use strict";
  var st = null, busy = null, busySince = 0, busyJob = null, jobSeq = 0, lastT = 0, flPort = 0;
  var TYPE_LABEL = { note_off: "NOTE OFF", note_on: "NOTE ON", polytouch: "POLY AT",
    program_change: "PROG CHG", control_change: "CC", aftertouch: "CH AT", pitchwheel: "BEND",
    sysex: "SYSEX", quarter_frame: "MTC", songpos: "SONG POS", song_select: "SONG SEL",
    tune_request: "TUNE", clock: "CLOCK", start: "START", "continue": "CONT", stop: "STOP",
    active_sensing: "ACT SENSE", reset: "RESET" };

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
    var f = $("cme-flash");
    f.textContent = msg; f.className = err ? "err" : "";
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, err ? 7000 : 2000);
  }

  // Can the page EDIT? Only with a COMPLETE read of the box. A partial read
  // (connected, but a section failed) publishes no config: an editor built from
  // the missing data would write it (an empty router row = wipe that row).
  function full() { return !!(st && st.connected && !st.partial && typeof st.preset === "number"); }

  // every POST carries a job id; the brain echoes it in last.job, so "done" is
  // OUR click finishing, not a hot-plug / pedal job that landed first
  function post(body, label) {
    busy = label; busySince = Date.now();
    busyJob = "w" + Date.now().toString(36) + "-" + (++jobSeq);
    body.job = busyJob;
    render();
    return fetch("/api/cme", { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (!r.ok) { busy = null; flash(r.error || "ORC not reachable: not sent", true); render(); }
        setTimeout(poll, 250);
      })
      .catch(function () { busy = null; flash("error sending", true); render(); });
  }

  function render() {
    if (!st) return;
    var on = !!st.connected, ok = full();
    $("cme-dot").className = "cme-dot" + (ok ? " on" : on ? " part" : "");
    $("cme-name").textContent = st.device_name || "CME U6MIDI Pro";
    $("cme-conn").textContent = st.missing ? "the ORC hasn't read the box yet"
      : ok ? "connected · preset " + st.preset
      : on ? "CONNECTED, BUT THE READ FAILED" + (st.error ? " (" + st.error + ")" : "")
           + ": editing is off until RE-READ succeeds"
      : "not connected" + (st.error ? " (" + st.error + ")" : "");
    $("cme-conn").className = on && !ok ? "cme-bad" : "";

    var names = st.preset_names || ["Preset 1", "Preset 2", "Preset 3", "Preset 4"];
    var pr = $("cme-presets");
    pr.innerHTML = "";
    names.forEach(function (nm, i) {
      var n = i + 1;
      var b = el("button", { "class": "btn" + (st.preset === n ? " on" : ""), type: "button",
        on: { click: function () { if (st.preset !== n) post({ what: "preset", preset: n }, "preset"); } } },
        [el("span", { "class": "n", text: String(n) }), el("span", { "class": "nm", text: nm })]);
      b.disabled = !on || !!busy;
      pr.appendChild(b);
    });

    $("cme-router-preset").textContent = ok ? "PRESET " + st.preset + " (" + (names[st.preset - 1] || "") + ")" : "THIS PRESET";
    var g = $("cme-grid");
    g.innerHTML = "";
    var outs = st.outputs || [], ins = st.inputs || [];
    // a click sends the WHOLE row (cur +/- one cell): only with every row read
    var routerOk = ok && Array.isArray(st.router) && st.router.length === ins.length;
    var head1 = el("tr", {}, [el("th", {}), el("th", { "class": "grp", colspan: "3", text: "TO USB (the Pi)" }),
      el("th", { "class": "grp sep", colspan: "3", text: "TO DIN SOCKETS" })]);
    var head2 = el("tr", {}, [el("th", {})].concat(outs.map(function (o, j) {
      return el("th", { "class": j === 3 ? "sep" : "", text: o.replace(" Out ", " ") });
    })));
    g.appendChild(head1); g.appendChild(head2);
    ins.forEach(function (inName, i) {
      var cur = routerOk && Array.isArray(st.router[i]) ? st.router[i] : null;
      var tr = el("tr", {}, [el("th", { "class": "row", text: inName })]);
      outs.forEach(function (o, j) {
        var lit = !!cur && cur.indexOf(j) >= 0;
        var b = el("button", { "class": "cme-cell" + (lit ? " on" : "") + (busy === "route" + i ? " busy" : ""),
          type: "button", title: inName + " -> " + o,
          on: { click: function () {
            if (!cur || !full()) return;
            var next = lit ? cur.filter(function (x) { return x !== j; }) : cur.concat([j]);
            post({ what: "route", input: i, outputs: next, preset: st.preset }, "route" + i);
          } } }, [document.createTextNode(lit ? "●" : "")]);
        b.disabled = !cur || !!busy;
        tr.appendChild(el("td", { "class": j === 3 ? "sep" : "" }, [b]));
      });
      g.appendChild(tr);
    });

    renderFilters(ok, names);

    renderMappers(ok, names);
    renderCurves();
  }

  // ── mappers: a plain-words list, and an editor that is built ONLY when a mapper
  // is opened or after a save lands (the 1.5 s poll must never wipe an edit).
  var mpSel = null, mpEdit = null, mpPreset = null, mpBase = null;
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function mappersOk() { return full() && Array.isArray(st.mappers) && st.mappers.length === 16; }

  function renderMappers(on, names) {
    $("cme-mp-preset").textContent = on ? "PRESET " + st.preset + " (" + (names[st.preset - 1] || "") + ")" : "THIS PRESET";
    var list = $("cme-mlist");
    list.innerHTML = "";
    if (mpPreset !== null && mpPreset !== st.preset) closeEditor();   // preset switched under us
    if (mpSel !== null && !mappersOk()) closeEditor();               // the data it edits is gone
    mpPreset = st.preset;
    if (!mappersOk()) return;
    st.mappers.forEach(function (m, i) {
      var cls = "cme-mrow" + (m["default"] ? " unused" : "") + (!m.inputs.length ? " off" : "") + (i === mpSel ? " sel" : "");
      var b = el("button", { "class": cls, type: "button",
        on: { click: function () { openEditor(i); } } },
        [el("span", { "class": "n", text: String(i + 1) }), el("span", { "class": "t", text: m.text })]);
      b.disabled = !on || !!busy;
      list.appendChild(b);
    });
  }

  function closeEditor() { mpSel = null; mpEdit = null; $("cme-med").hidden = true; $("cme-med").innerHTML = ""; }

  function openEditor(i) {
    var m = mappersOk() && st.mappers[i];
    if (!m) return;
    mpSel = i;
    mpBase = st.preset;        // the preset this edit is computed from (sent with SAVE)
    mpEdit = clone(m);
    if (m["default"]) {        // a fresh mapper: start from something the app would write
      mpEdit.inputs = [0];
      mpEdit.src.type = 2; mpEdit.dst.type = 0;
    }
    if (mpEdit.src.type === 7 && !mpEdit.transpose) mpEdit.transpose = { octave: 0, semitone: 0 };
    buildEditor();
    render();
  }

  function sel(opts, val, onchange) {
    var s = el("select", {});
    opts.forEach(function (o) {
      var op = el("option", { value: String(o[0]) }, [document.createTextNode(o[1])]);
      if (String(o[0]) === String(val)) op.selected = true;
      s.appendChild(op);
    });
    s.addEventListener("change", function () { onchange(s.value); });
    return s;
  }
  function num(val, lo, hi, onchange) {
    var n = el("input", { type: "number", min: String(lo), max: String(hi), value: String(val) });
    n.addEventListener("change", function () { onchange(parseInt(n.value, 10)); });
    return n;
  }
  function box(label, val, onchange, disabled) {
    var c = el("input", { type: "checkbox" });
    c.checked = !!val; c.disabled = !!disabled;
    c.addEventListener("change", function () { onchange(c.checked); });
    return el("label", {}, [c, document.createTextNode(label)]);
  }
  function rng(lbl, r, onchange) {
    return el("span", {}, [document.createTextNode(lbl + " "),
      num(r[0], 0, 127, function (v) { r[0] = v; onchange(); }), document.createTextNode(" – "),
      num(r[1], 0, 127, function (v) { r[1] = v; onchange(); })]);
  }
  function range(a, b) { var o = []; for (var i = a; i <= b; i++) o.push(i); return o; }

  function buildEditor() {
    var m = mpEdit, med = $("cme-med");
    med.hidden = false; med.innerHTML = "";
    var rebuild = function () { buildEditor(); };
    var st_ = m.src.type, mode = st_ === 7 ? "transpose" : st_ === 8 ? "global" : "normal";
    var vn = (st.value_names || {})[String(st_)] || ["Value 1", "Value 2"];
    var dvn = (st.value_names || {})[String(m.dst.type)] || ["Value 1", "Value 2"];
    med.appendChild(el("div", { "class": "card-label", text: "EDITING MAPPER " + (mpSel + 1) }));

    med.appendChild(el("h4", { text: "INPUTS (none = mapper switched off)" }));
    var ins = el("div", { "class": "cme-frow" });
    (st.inputs || []).forEach(function (name, i) {
      ins.appendChild(box(name, m.inputs.indexOf(i) >= 0, function (on) {
        m.inputs = on ? m.inputs.concat([i]).sort() : m.inputs.filter(function (x) { return x !== i; });
      }));
    });
    med.appendChild(ins);

    med.appendChild(el("h4", { text: "SOURCE: WHAT TO CATCH" }));
    var srow = el("div", { "class": "cme-frow" });
    srow.appendChild(sel((st.src_types || []).map(function (t, i) { return [i, t]; }), st_, function (v) {
      m.src.type = parseInt(v, 10);
      if (m.src.type === 7 && !m.transpose) m.transpose = { octave: 0, semitone: 0 };
      if (m.src.type === 7) m.follow.v1 = false;
      if (m.src.type === 8) { m.follow.ch = false; m.follow.v1 = false; }
      if (m.src.type >= 7) m.dst.type = 0;
      rebuild();
    }));
    var chs = range(1, 16).map(function (c) { return [c, "ch " + c]; });
    srow.appendChild(el("span", {}, [document.createTextNode("channels "),
      sel(chs, m.src.ch_min, function (v) { m.src.ch_min = parseInt(v, 10); }), document.createTextNode(" – "),
      sel(chs, m.src.ch_max, function (v) { m.src.ch_max = parseInt(v, 10); })]));
    med.appendChild(srow);
    var svr = el("div", { "class": "cme-frow" });
    if (vn[0]) svr.appendChild(rng(vn[0], m.src.v1, function () {}));
    if (vn[1]) svr.appendChild(rng(vn[1], m.src.v2, function () {}));
    med.appendChild(svr);

    med.appendChild(el("h4", { text: "TARGET: WHAT IT BECOMES" }));
    var trow = el("div", { "class": "cme-frow" });
    if (mode === "normal") {
      trow.appendChild(sel((st.dst_types || []).map(function (t, i) { return [i, t]; }), m.dst.type, function (v) {
        m.dst.type = parseInt(v, 10); rebuild();
      }));
      trow.appendChild(box("Keep original", m.keep_original, function (on) { m.keep_original = on; }));
    } else if (mode === "transpose") {
      if (m.transpose.octave < -4 || m.transpose.octave > 4) {
        trow.appendChild(el("span", { "class": "cme-warn", text: "stored octave " + m.transpose.octave + " is outside ±4 (left over): pick one" }));
        m.transpose.octave = 0;
      }
      trow.appendChild(el("span", {}, [document.createTextNode("octave "),
        sel(range(-4, 4).map(function (v) { return [v, (v > 0 ? "+" : "") + v]; }), m.transpose.octave, function (v) { m.transpose.octave = parseInt(v, 10); })]));
      if (m.transpose.semitone < -48 || m.transpose.semitone > 48) {
        trow.appendChild(el("span", { "class": "cme-warn", text: "stored semitone " + m.transpose.semitone + " is outside ±48 (left over): pick one" }));
        m.transpose.semitone = 0;
      }
      trow.appendChild(el("span", {}, [document.createTextNode("semitone "),
        sel(range(-48, 48).map(function (v) { return [v, (v > 0 ? "+" : "") + v]; }), m.transpose.semitone, function (v) { m.transpose.semitone = parseInt(v, 10); })]));
      trow.appendChild(el("span", {}, [document.createTextNode("random skip "),
        sel(range(0, 100).map(function (v) { return [v, v ? v + "%" : "none"]; }), m.skip, function (v) { m.skip = parseInt(v, 10); })]));
    } else {
      trow.appendChild(el("span", { text: "every message moves to the target channel" }));
    }
    med.appendChild(trow);

    var crow = el("div", { "class": "cme-frow" });
    var chFollow = mode === "normal" && m.follow.ch;
    if (mode === "normal") crow.appendChild(box("channel follows the input", m.follow.ch, function (on) { m.follow.ch = on; if (on) m.transpose_ch = false; rebuild(); }));
    crow.appendChild(el("span", {}, [document.createTextNode("target channel "),
      (function () { var s = sel(chs, m.dst.ch, function (v) { m.dst.ch = parseInt(v, 10); }); s.disabled = chFollow; return s; })()]));
    if (mode === "normal") crow.appendChild(box("Transpose channel", m.transpose_ch, function (on) { m.transpose_ch = on; if (on) m.follow.ch = false; rebuild(); }));
    med.appendChild(crow);

    [["v1", 0, "use_other", "Use input value 2"], ["v2", 1, "use_other", "Use input value 1"]].forEach(function (d) {
      var k = d[0], label = (mode === "normal" ? dvn : vn)[d[1]];
      if (!label || mode === "global") return;
      if (mode === "transpose" && k === "v1") return;       // that slot holds the transpose
      var r = el("div", { "class": "cme-frow" });
      r.appendChild(el("b", { text: label + ":" }));
      r.appendChild(box("follows the input", m.follow[k], function (on) { m.follow[k] = on; rebuild(); }));
      if (!m.follow[k]) r.appendChild(rng("range", m.dst[k], function () {}));
      r.appendChild(box("Invert", m[k].invert, function (on) { m[k].invert = on; }));
      // curve field: bit 0 = on, bits 1-2 = User 1-4 (measured); acts only with Follow off
      var cv = (m[k].curve & 1) ? ((m[k].curve >> 1) & 3) + 1 : 0;
      r.appendChild(el("span", {}, [document.createTextNode("curve "),
        sel([[0, "off"], [1, "User 1"], [2, "User 2"], [3, "User 3"], [4, "User 4"]], cv, function (v) {
          v = parseInt(v, 10); m[k].curve = v ? 1 | ((v - 1) << 1) : 0; rebuild(); })]));
      if (cv && m.follow[k]) r.appendChild(el("span", { "class": "cme-warn", text: "curves only act with Follow off" }));
      r.appendChild(box(d[3], m[k].use_other, function (on) { m[k].use_other = on; rebuild(); }));
      if (m[k].use_other) r.appendChild(box("Compress/Expand", m[k].compress, function (on) { m[k].compress = on; }));
      med.appendChild(r);
    });

    var act = el("div", { "class": "cme-frow", style: "margin-top:10px" });
    var writeOk = function () { return mappersOk() && !busy && mpBase === st.preset; };
    var save = el("button", { "class": "btn accent", type: "button",
      on: { click: function () { if (writeOk()) post({ what: "mapper", index: mpSel, mapper: mpEdit, preset: mpBase }, "mapper"); } } },
      [document.createTextNode("SAVE TO BOX")]);
    var clr = el("button", { "class": "btn", type: "button",
      on: { click: function () { if (writeOk()) post({ what: "mapper", index: mpSel, mapper: "clear", preset: mpBase }, "mapper"); } } },
      [document.createTextNode("CLEAR (UNUSED)")]);
    save.disabled = clr.disabled = !writeOk();
    act.appendChild(save);
    act.appendChild(clr);
    act.appendChild(el("button", { "class": "btn", type: "button",
      on: { click: function () { openEditor(mpSel); } } }, [document.createTextNode("UNDO EDITS")]));
    act.appendChild(el("button", { "class": "btn", type: "button",
      on: { click: function () { closeEditor(); render(); } } }, [document.createTextNode("CLOSE")]));
    med.appendChild(act);
  }

  function renderCurves() {
    var box_ = $("cme-curves"), cs = st.curves || [];
    if (box_.dataset.sig === JSON.stringify(cs)) return;      // unchanged: leave the SVGs be
    box_.dataset.sig = JSON.stringify(cs);
    box_.innerHTML = "";
    cs.forEach(function (c, i) {
      var pts = c.map(function (v, x) { return x + "," + (127 - v); }).join(" ");
      var fig = document.createElement("figure");
      fig.innerHTML = '<svg viewBox="-2 -2 131 131" preserveAspectRatio="none"><polyline points="0,127 127,0" fill="none" stroke="currentColor" stroke-opacity=".2" stroke-width="1"/><polyline points="' + pts + '" fill="none" stroke="var(--accent)" stroke-width="2.5"/></svg>';
      fig.appendChild(document.createTextNode("Curve " + (i + 1)));
      box_.appendChild(fig);
    });
  }

  function renderFilters(on, names) {
    $("cme-fl-preset").textContent = on ? "PRESET " + st.preset + " (" + (names[st.preset - 1] || "") + ")" : "THIS PRESET";
    var ports = st.filter_ports || [], meas = st.filter_measured || [], fl = st.filters || [];
    var pr = $("cme-fl-ports"), ed = $("cme-fl-edit");
    pr.innerHTML = ""; ed.innerHTML = "";
    ports.forEach(function (p, i) {
      var f = fl[i] || { channels: [], types: [] };
      var kids = [document.createTextNode(p + (meas.indexOf(i) < 0 ? " *" : ""))];
      if (f.channels.length || f.types.length) kids.push(el("span", { "class": "dot" }));
      pr.appendChild(el("button", { "class": "btn" + (i === flPort ? " on" : ""), type: "button",
        on: { click: function () { flPort = i; render(); } } }, kids));
    });
    // a click sends the port's WHOLE filter: only with every port read in full
    if (!on || fl.length !== ports.length || !fl[flPort] || !Array.isArray(fl[flPort].channels)
        || !Array.isArray(fl[flPort].types)) return;
    var f = fl[flPort];
    function send(chs, tys) {
      if (!full()) return;
      post({ what: "filter", port: flPort, channels: chs, types: tys, preset: st.preset }, "filter");
    }
    function toggle(list, v) {
      return list.indexOf(v) >= 0 ? list.filter(function (x) { return x !== v; }) : list.concat([v]);
    }
    ed.appendChild(el("div", { "class": "cme-fl-label", text: "CHANNELS" }));
    var ch = el("div", { "class": "cme-toggles" });
    for (var c = 1; c <= 16; c++) (function (c) {
      var b = el("button", { "class": "cme-tg" + (f.channels.indexOf(c) < 0 ? " pass" : ""), type: "button",
        title: (f.channels.indexOf(c) < 0 ? "passes" : "BLOCKED") + ": click to flip",
        on: { click: function () { send(toggle(f.channels, c), f.types); } } }, [document.createTextNode(String(c))]);
      b.disabled = !!busy;
      ch.appendChild(b);
    })(c);
    ed.appendChild(ch);
    ed.appendChild(el("div", { "class": "cme-fl-label", text: "MESSAGE TYPES" }));
    var ty = el("div", { "class": "cme-toggles" });
    (st.filter_types || []).forEach(function (t) {
      var b = el("button", { "class": "cme-tg" + (f.types.indexOf(t) < 0 ? " pass" : ""), type: "button",
        title: (f.types.indexOf(t) < 0 ? "passes" : "BLOCKED") + ": click to flip",
        on: { click: function () { send(f.channels, toggle(f.types, t)); } } }, [document.createTextNode(TYPE_LABEL[t] || t)]);
      b.disabled = !!busy;
      ty.appendChild(b);
    });
    ed.appendChild(ty);
    var clr = el("button", { "class": "btn", type: "button", style: "margin-top:8px",
      on: { click: function () { send([], []); } } }, [document.createTextNode("PASS EVERYTHING")]);
    clr.disabled = !!busy || (!f.channels.length && !f.types.length);
    ed.appendChild(clr);
  }

  function poll() {
    fetch("/api/cme").then(function (r) { return r.json(); }).then(function (s) {
      st = s;
      var t = (s.last && s.last.t) || 0;
      // done = the brain finished OUR job (last.job echoes the id we sent). An older
      // brain that echoes no job at all falls back to "anything newer than the click".
      var mine = s.last && (s.last.job === busyJob
        || (s.last.job === undefined && t > lastT && t * 1000 >= busySince - 500));
      if (busy && mine) {
        busy = null; busyJob = null;
        if (s.last.ok) flash("done: read back from the box");
        else flash(s.last.error || "failed", true);
        if (mpSel !== null && /^mapper/.test(s.last.what || "")) {
          st = s; openEditor(mpSel);          // show what the BOX now holds, not the form
        }
      }
      if (busy && Date.now() - busySince > 6000) { busy = null; flash("no answer from the ORC", true); }
      lastT = Math.max(lastT, t);
      render();
    }).catch(function () {});
  }

  $("cme-refresh").addEventListener("click", function () { post({ what: "refresh" }, "refresh"); });
  fetch("/api/cme").then(function (r) { return r.json(); }).then(function (s) {
    st = s; lastT = (s.last && s.last.t) || 0; render();
  });
  setInterval(function () { poll(); }, 1500);
})();
