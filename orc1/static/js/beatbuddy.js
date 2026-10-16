// BeatBuddy control page. Talks to the mind (orc1_beatbuddy) via the cmd socket
// through /api/beatbuddy/cmd, and polls /api/beatbuddy for songs / kits / status.
// Songs are joined to bb_catalog.json metadata (genre / name / bpm / ts / kit),
// so the browser groups by genre and filters by search text + tempo.
(function () {
  "use strict";
  var curSong = null, curKit = null, songs = [], folders = [], kits = [];
  var filt = { genre: "", q: "", bpmMin: "", bpmMax: "" };
  var clockBpm = 120, clockMode = "internal";

  function flash(msg) {
    var f = document.getElementById("bb-flash");
    if (!f) return;
    f.textContent = msg;
    setTimeout(function () { if (f.textContent === msg) f.textContent = ""; }, 1400);
  }

  function post(body) {
    return fetch("/api/beatbuddy/cmd", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  var BB = {
    cmd: function (action, extra) {
      post(Object.assign({ action: action }, extra || {}))
        .then(function (r) { flash(r.ok ? action.replace("BB_", "") : "failed"); })
        .catch(function () { flash("error"); });
    },
    loadSong: function (file) {
      curSong = file;
      renderSongs();
      this.cmd("BB_SONG_SELECT", { song: file });
    },
    selectKit: function (idx) {
      curKit = idx;
      renderKits();
      this.cmd("BB_KIT_SELECT", { program: idx });
    },
    bpm: function (delta) {
      if (clockMode === "external") { flash("clock is external"); return; }
      var v = Math.max(20, Math.min(300, Math.round(clockBpm) + delta));
      clockBpm = v;
      var el = document.getElementById("bb-clock-bpm"); if (el) el.textContent = v;
      fetch("/cmd", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "SET_BPM", value: v }) })
        .then(function () { flash("bpm " + v); }).catch(function () { flash("error"); });
    },
    matchBpm: function () { this.cmd("BB_MATCH_BPM"); },
    tempoMode: function (followClock) {
      this.cmd("BB_TEMPO_MODE", { mode: followClock ? "clock" : "song" });
    },
    // OUTPUT (2026-09-23): where the hits go -- BBKit, the ORC 909, or any MIDI
    // out (MPC via the pisound DIN, etc.) on a chosen channel.
    setOutput: function (dest) {
      showPortRow(false);
      this.cmd("BB_OUTPUT", { dest: dest });
    },
    showPort: function () { showPortRow(true); },
    applyPort: function () {
      var p = document.getElementById("bb-out-portsel");
      var c = document.getElementById("bb-out-ch");
      if (!p || !p.value) { flash("no port"); return; }
      this.cmd("BB_OUTPUT", { dest: "port", port: p.value, channel: parseInt(c.value, 10) });
    },
    refresh: function () { poll(); }
  };

  var outCur = null, portRowOpen = false;
  function showPortRow(on) {
    portRowOpen = on;
    var row = document.getElementById("bb-out-port");
    if (row) row.style.display = on ? "flex" : "none";
  }
  function loadPorts() {
    var ch = document.getElementById("bb-out-ch");
    if (ch && !ch.options.length) {
      for (var i = 1; i <= 16; i++) {
        var o = document.createElement("option"); o.value = i; o.textContent = "ch " + i;
        ch.appendChild(o);
      }
      ch.value = 10;
    }
    fetch("/api/midi_ports").then(function (r) { return r.json(); }).then(function (d) {
      var sel = document.getElementById("bb-out-portsel");
      if (!sel) return;
      sel.innerHTML = "";
      (d.ports || []).filter(function (p) { return p.key !== "synth"; }).forEach(function (p) {
        var o = document.createElement("option"); o.value = p.key; o.textContent = p.label;
        sel.appendChild(o);
      });
      if (outCur && outCur.port) sel.value = outCur.port;
    }).catch(function () {});
  }
  function renderOutput(o) {
    if (!o || !o.dest) return;
    outCur = o;
    document.querySelectorAll("#bb-out [data-dest]").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-dest") === o.dest);
    });
    var note = document.getElementById("bb-out-note");
    if (note) note.textContent =
      o.dest === "bbkit" ? "sampled BeatBuddy kit (ORC1-BBKit)" :
      o.dest === "909"   ? "ORC 909 drums" :
                           "MIDI out: " + o.port + " ch " + o.channel + " (notes sent unchanged)";
    if (o.dest === "port" && !portRowOpen) {
      var sel = document.getElementById("bb-out-portsel");
      var ch = document.getElementById("bb-out-ch");
      if (sel && document.activeElement !== sel) sel.value = o.port;
      if (ch && document.activeElement !== ch) ch.value = o.channel;
    }
  }
  window.BB = BB;

  function passes(s) {
    if (filt.genre && s.folder !== filt.genre) return false;
    if (filt.q) {
      var q = filt.q.toLowerCase();
      if ((s.name || "").toLowerCase().indexOf(q) < 0 &&
          (s.folder || "").toLowerCase().indexOf(q) < 0) return false;
    }
    if (filt.bpmMin && (s.bpm == null || s.bpm < +filt.bpmMin)) return false;
    if (filt.bpmMax && (s.bpm == null || s.bpm > +filt.bpmMax)) return false;
    return true;
  }

  function renderGenreSelect() {
    var sel = document.getElementById("bb-genre");
    if (!sel || sel.dataset.built === "1" + folders.length) return;
    sel.dataset.built = "1" + folders.length;
    sel.innerHTML = "";
    var all = document.createElement("option");
    all.value = ""; all.textContent = "All genres (" + songs.length + ")";
    sel.appendChild(all);
    folders.forEach(function (f) {
      var n = songs.filter(function (s) { return s.folder === f; }).length;
      var o = document.createElement("option");
      o.value = f; o.textContent = f + " (" + n + ")";
      sel.appendChild(o);
    });
    sel.value = filt.genre;
  }

  function renderSongs() {
    var host = document.getElementById("bb-songs");
    var empty = document.getElementById("bb-songs-empty");
    var count = document.getElementById("bb-count");
    if (!host) return;
    host.innerHTML = "";
    var shown = songs.filter(passes);
    if (empty) empty.hidden = songs.length > 0;
    if (count) count.textContent = shown.length + " / " + songs.length;
    var lastFolder = null;
    shown.forEach(function (s) {
      if (s.folder !== lastFolder) {
        lastFolder = s.folder;
        var h = document.createElement("div");
        h.className = "bb-genre-hd";
        h.textContent = s.folder;
        host.appendChild(h);
      }
      var b = document.createElement("button");
      b.className = "btn bb-song" + (s.file === curSong ? " cur" : "");
      b.title = s.file + (s.kit ? "  · kit: " + s.kit : "");
      var meta = [];
      if (s.bpm) meta.push(s.bpm + " bpm");
      if (s.ts) meta.push(s.ts);
      if (s.nParts) meta.push(s.nParts + (s.nParts === 1 ? " part" : " parts"));
      if (s.kit) meta.push("♫ " + s.kit);
      b.innerHTML = "<span class='bb-song-nm'></span><span class='bb-sub'></span>";
      b.querySelector(".bb-song-nm").textContent = s.name;
      b.querySelector(".bb-sub").textContent = meta.join(" · ");
      b.onclick = function () { BB.loadSong(s.file); };
      host.appendChild(b);
    });
  }

  function renderKits() {
    var host = document.getElementById("bb-kits");
    if (!host) return;
    host.innerHTML = "";
    kits.forEach(function (name, i) {
      var b = document.createElement("button");
      b.className = "btn bb-kit" + (i === curKit ? " cur" : "");
      b.textContent = name;
      b.onclick = function () { BB.selectKit(i); };
      host.appendChild(b);
    });
  }

  function setStatus(st) {
    var lamp = document.getElementById("bb-lamp");
    var playing = !!(st && st.playing);
    if (lamp) {
      lamp.className = "bb-lamp " + (playing ? "playing" : "stopped");
      lamp.textContent = (playing ? "▶ " : "■ ") + ((st && st.status) || "—");
    }
    var song = (st && st.song) || "";
    var nm = song;
    var match = songs.filter(function (s) { return s.file === song; })[0];
    if (match) nm = match.name;
    document.getElementById("bb-song").textContent = nm ? nm.replace(/\.BBS$/i, "") : "—";
    document.getElementById("bb-part").textContent = (st && st.part) || "—";
    document.getElementById("bb-bpm").textContent = (st && st.bpm) || "—";
    // tempo card
    if (st && st.clock_bpm != null) clockBpm = st.clock_bpm;
    if (st && st.clock_mode) clockMode = st.clock_mode;
    var cb = document.getElementById("bb-clock-bpm");
    if (cb) cb.textContent = (st && st.clock_bpm != null) ? st.clock_bpm : "—";
    var cm = document.getElementById("bb-clock-mode");
    if (cm) cm.textContent = clockMode === "external" ? "· external"
                            : clockMode === "drift" ? "· drift" : "";
    var sb = document.getElementById("bb-song-bpm2");
    if (sb) sb.textContent = (st && st.bpm) || "—";
    var fl = document.getElementById("bb-follow");
    if (fl && document.activeElement !== fl) fl.checked = (st && st.tempo_mode === "clock");
    var ext = (clockMode === "external");
    ["bb-match"].forEach(function (id) { var e = document.getElementById(id); if (e) e.disabled = ext; });
    if (st && st.song && st.song !== curSong) { curSong = st.song; renderSongs(); }
    renderOutput(st && st.output);
  }

  function bindFilters() {
    var g = document.getElementById("bb-genre");
    var q = document.getElementById("bb-search");
    var lo = document.getElementById("bb-bpm-min");
    var hi = document.getElementById("bb-bpm-max");
    if (g) g.onchange = function () { filt.genre = g.value; renderSongs(); };
    if (q) q.oninput = function () { filt.q = q.value; renderSongs(); };
    if (lo) lo.oninput = function () { filt.bpmMin = lo.value; renderSongs(); };
    if (hi) hi.oninput = function () { filt.bpmMax = hi.value; renderSongs(); };
    var clr = document.getElementById("bb-clear");
    if (clr) clr.onclick = function () {
      filt = { genre: "", q: "", bpmMin: "", bpmMax: "" };
      if (g) g.value = ""; if (q) q.value = ""; if (lo) lo.value = ""; if (hi) hi.value = "";
      renderSongs();
    };
  }

  function poll() {
    fetch("/api/beatbuddy").then(function (r) { return r.json(); }).then(function (d) {
      var changed = false;
      if (JSON.stringify(d.songs) !== JSON.stringify(songs)) { songs = d.songs || []; changed = true; }
      if (JSON.stringify(d.folders) !== JSON.stringify(folders)) { folders = d.folders || []; changed = true; }
      if (JSON.stringify(d.kits) !== JSON.stringify(kits)) { kits = d.kits || []; renderKits(); }
      if (changed) { renderGenreSelect(); renderSongs(); }
      setStatus(d.state || {});
    }).catch(function () {});
  }

  bindFilters();
  loadPorts();
  poll();
  setInterval(poll, 800);
})();
