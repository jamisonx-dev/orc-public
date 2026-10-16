#!/usr/bin/env python3
"""
ORC-1 Web UI  v3
Read-only status + flag toggle controls.
Runs at http://<pi-ip>:5000

Commands are sent to orc1.py over a Unix domain socket
(~/.orc1_cmd.sock) as JSON lines — no commands lost, no polling latency.
If the socket is unavailable (older engine), falls back to the legacy
atomic file handshake: write ~/orc1_cmd.json, engine polls every 100ms.

v3 (2026-07-04): the inline HTML/CSS/JS monolith was extracted into
templates/ and static/ — one template + css + js per page:

  /         templates/index.html    static/css/main.css    static/js/main.js
  /seq      templates/seq.html      static/css/seq.css     static/js/seq.js
  /routes   templates/routes.html   static/css/routes.css  static/js/routes.js
  /sampler  templates/sampler.html  static/css/sampler.css static/js/sampler.js

Edit those files to customize the UI; this file is only plumbing
(state read, command socket, JSON APIs).
"""

import glob, json, os, socket, threading, time
from flask import Flask, Response, request, render_template, jsonify

app      = Flask(__name__)
# Pi-local app: don't let browsers cache static css/js, so UI edits show
# up on the next reload instead of after a cache timeout.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

STATE    = os.path.expanduser("~/orc1_state.json")
CMD      = os.path.expanduser("~/orc1_cmd.json")
CMD_SOCK = os.path.expanduser("~/.orc1_cmd.sock")
ROUTES   = os.path.expanduser("~/orc1_routes.json")

def read_state():
    try:
        with open(STATE) as f: return json.load(f)
    except: return None

def _send_cmd_socket(payload):
    """Send one JSON-line command over the engine's Unix socket.
    Returns True on success, raises on any failure."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(CMD_SOCK)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        try:
            s.shutdown(socket.SHUT_WR)
            resp = s.recv(64)
        except Exception:
            resp = b""
        # Engine acks with {"ok":true}\n; an empty resp still means the
        # line was delivered (engine may have closed early).
        return (not resp) or (b'"ok":true' in resp)
    finally:
        s.close()

def write_cmd(action, extra=None):
    payload = {"action": action}
    if extra: payload.update(extra)
    # Preferred: Unix socket to the engine (no lost commands)
    try:
        return _send_cmd_socket(payload)
    except (FileNotFoundError, ConnectionRefusedError):
        # Nobody is listening = the brain is DOWN (stopped / restarting).
        # Report failure, and do NOT leave the command in the fallback file:
        # the brain reads that file when it next starts, so a click made while
        # it was down used to fire seconds later, out of context, after the
        # page had already been told "ok" (2026-09-24).
        return False
    except Exception:
        pass   # alive but slow (timeout etc.): the polled file still gets it
    # Fallback: legacy atomic file handshake (engine polls every 100ms)
    try:
        tmp = CMD + ".tmp"
        with open(tmp, "w") as f: json.dump(payload, f)
        os.replace(tmp, CMD)
        return True
    except: return False

# ── Pages (templates/ + static/) ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/sampler')
def sampler_page():
    return render_template('sampler.html')

@app.route('/routes')
def routes_page():
    return render_template('routes.html')

@app.route('/matrix')
def matrix_page():
    return render_template('matrix.html')


# ── PROTECTED CHANNELS (2026-10-14): one live setting (orc1_config.protected_config) that every channel
# picker in the ORC refuses; edited on /matrix. The web process is the file's only writer.
def _protect_error(ch):
    import orc1_config as _cfg
    return _cfg.protect_error(ch)


def _protected_refusal(ch):
    return Response(json.dumps({"ok": False, "error": _protect_error(ch)}), status=400,
                    mimetype='application/json')


def _protected_view():
    import orc1_config as _cfg
    conf = _cfg.protected_config()
    return {"channels": {str(c): {"on": conf.get(c, {}).get("on", False),
                                  "name": conf.get(c, {}).get("name", ""),
                                  "locked": c in _cfg.PROTECTED_LOCKED} for c in range(1, 17)}}


@app.route('/api/protected_channels', methods=['GET'])
def api_protected_get():
    return jsonify(_protected_view())


@app.route('/api/protected_channels', methods=['POST'])
def api_protected_set():
    """{channel, on, name?}: switch one channel's protection. Refused for a LOCKED channel (13), and for
    switching ON a channel a module is bound to (the next restart would drop that binding: move it first)."""
    import orc1_config as _cfg
    data = request.get_json(force=True, silent=True) or {}
    try:
        ch = int(data.get("channel"))
        if not 1 <= ch <= 16:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "channel 1-16"}), 400
    on = data.get("on") is True
    conf = {c: dict(e) for c, e in _cfg.protected_config().items()}
    if ch in _cfg.PROTECTED_LOCKED and not on:
        return jsonify({"ok": False, "error": f"ch {ch} is the ORC's control channel: always protected"}), 400
    if on:
        try:
            with open(os.path.expanduser(_cfg.BINDINGS_FILE)) as f:
                holder = (json.load(f).get("bindings") or {}).get(str(ch))
        except (OSError, ValueError):
            holder = None
        if holder:
            return jsonify({"ok": False, "error": f"{holder} is bound to ch {ch}: move it on /matrix first"}), 400
    e = conf.setdefault(ch, {"on": False, "name": ""})
    e["on"] = on
    if "name" in data:
        e["name"] = str(data.get("name") or "").strip()[:24]
    out = {"channels": {str(c): v for c, v in sorted(conf.items())}}
    p = os.path.expanduser(_cfg.PROTECTED_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, p)
    return jsonify(dict(_protected_view(), ok=True))

@app.route('/seq')
def seq_page():
    return render_template('seq.html')

# ── State + command APIs ──────────────────────────────────────────────────────

@app.route('/fx')
def fx_page():
    return render_template('fx.html')

@app.route('/synth')
def synth_page():
    return render_template('synth.html')

@app.route('/chip')
def chip_page():
    return render_template('chip.html')

@app.route('/drums')
def drums_page():
    return render_template('drums.html')

@app.route('/mono')
def mono_page():
    return render_template('mono.html')

@app.route('/beatstep')
def beatstep_page():
    return render_template('beatstep.html')

# ---- RC-505 looper control -------------------------------------------------
RC505_CONFIG = os.path.expanduser("~/rc505_tools/rc505_config.json")

@app.route('/rc505')
def rc505_page():
    return render_template('rc505.html')

def _rc505_read_map():
    """{cc:int -> target:str} from the authored config, or {}."""
    try:
        cfg = json.load(open(RC505_CONFIG))
        out = {}
        for a in cfg.get("assigns", {}).get("assigns", []):
            src = a.get("src")
            if isinstance(src, dict) and "cc" in src and a.get("tgt"):
                out[int(src["cc"])] = a["tgt"]
        return out
    except Exception:
        return {}

@app.route('/api/rc505')
def api_rc505():
    import orc1_rc505 as R
    cc_to_target = _rc505_read_map()
    target_to_cc = {t: cc for cc, t in cc_to_target.items()}
    try:
        import mido
        present = any("RC-505 MIDI 1" in n for n in mido.get_output_names())
    except Exception:
        present = None
    actions = [{**item, "cc": target_to_cc.get(item["target"])} for item in R.all_targets()]
    return jsonify({"present": present,
                    "mapped":   [a for a in actions if a["cc"] is not None],
                    "unmapped": [a for a in actions if a["cc"] is None]})

@app.route('/api/rc505/cmd', methods=['POST'])
def api_rc505_cmd():
    d = request.get_json(force=True) or {}
    act = d.get("action", "")
    if not act.startswith("RC505_"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    ok = write_cmd(act, {k: v for k, v in d.items() if k != "action"})
    return jsonify({"ok": bool(ok)})

@app.route('/api/rc505/map', methods=['GET', 'POST'])
def api_rc505_map():
    import orc1_rc505 as R
    if request.method == 'GET':
        return jsonify(_rc505_read_map())
    body = request.get_json(force=True) or {}
    m = body.get("map", {})
    valid = set(R.TARGET_NAMES)
    assigns, slot = [], 1
    for cc_s, tgt in sorted(m.items(), key=lambda kv: int(kv[0])):
        cc = int(cc_s)
        if not (1 <= cc <= 31 or 64 <= cc <= 95) or tgt not in valid or slot > 16:
            continue
        entry = {"slot": slot, "src": {"cc": cc}, "tgt": tgt}
        if R.is_continuous(tgt):
            entry["min"], entry["max"] = 0, 100
        assigns.append(entry); slot += 1
    try:
        cfg = json.load(open(RC505_CONFIG))
    except Exception:
        cfg = {}
    cfg.setdefault("assigns", {})
    cfg["assigns"].update({"mem_ids": "all", "clear_others": True, "assigns": assigns})
    tmp = RC505_CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, RC505_CONFIG)
    write_cmd("RC505_RELOAD_MAP")
    return jsonify({"ok": True, "assigns": len(assigns),
                    "note": "Saved + live handler reloaded. To apply on the UNIT: storage mode -> bash ~/rc505_tools/rc505_write.sh -> power-cycle."})

@app.route('/api/bus_levels')
def api_bus_levels():
    """The persisted bus levels (master / poly / chip / drum / ...), which the
    module hosts write within ~1.5 s of any move. The play page's LEVELS card
    seeds and follows its sliders from this."""
    try:
        with open(os.path.expanduser("~/.orc1_bus_levels.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = {k: v for k, v in (data.items() if isinstance(data, dict) else [])
           if isinstance(v, (int, float)) and not isinstance(v, bool)
           and 0 <= v <= 127}
    return Response(json.dumps(out), mimetype='application/json')

@app.route('/beatbuddy')
def beatbuddy_page():
    return render_template('beatbuddy.html')


# ── /pedal: FCB1010 config. The BRAIN owns ~/.orc1_pedal.json (applies + saves on
# PEDAL_CONFIG_SET); this page reads that file and validates with the SAME
# orc1_pedal.validate_config the brain runs, so the two can't disagree.
_PEDAL_CONF = os.path.expanduser("~/.orc1_pedal.json")


@app.route('/pedal')
def pedal_page():
    return render_template('pedal.html')


def _pedal_ports():
    import orc1_config as _c
    return sorted(getattr(_c, "PORTS", {}).keys())


@app.route('/api/pedal', methods=['GET'])
def api_pedal_get():
    import orc1_pedal as _p
    try:
        with open(_PEDAL_CONF) as f:
            raw = json.load(f)
        saved = True
    except Exception:
        raw, saved = {}, False
    conf, errs = _p.validate_config(raw, ports=set(_pedal_ports()))
    return jsonify({
        "config": conf, "saved": saved, "errors": errs,
        "switch_choices": _p.SWITCH_CHOICES, "pr_choices": _p.PR_CHOICES,
        "ports": _pedal_ports(),
        "modes": {m: list(r) for m, r in _p.MODES.items()},
        "ctrl_ch": _p.CTRL_CH,
    })


@app.route('/api/pedal', methods=['POST'])
def api_pedal_set():
    import orc1_pedal as _p
    data = request.get_json(force=True, silent=True) or {}
    conf, errs = _p.validate_config(data.get("config"), ports=set(_pedal_ports()))
    try:
        with open(_PEDAL_CONF) as f:
            prev, _e = _p.validate_config(json.load(f), ports=set(_pedal_ports()))
    except Exception:
        prev = None
    errs += _p.refuse_protected(conf, prev, _protect_error)
    ok = write_cmd("PEDAL_CONFIG_SET", {"config": conf})
    return jsonify({"ok": bool(ok), "config": conf, "errors": errs})

# ── /pads: per-device pad maps (orc1_padmap). The BRAIN owns ~/.orc1_padmap.json
# (applies + saves on PADMAP_SET); this page validates with the SAME
# orc1_padmap.validate_config. Learn mode reads the brain's tmpfs hit log.
_PADMAP_CONF = os.path.expanduser("~/.orc1_padmap.json")
_PADMAP_HITS = "/tmp/orc1_padmap_hits.json"


def _padmap_modules():
    import orc1_config as _c
    return list(getattr(_c, "MODULE_KEYS", []))


@app.route('/pads')
def pads_page():
    return render_template('pads.html')


@app.route('/api/padmap', methods=['GET'])
def api_padmap_get():
    import orc1_padmap as _pm
    try:
        with open(_PADMAP_CONF) as f:
            raw, saved = json.load(f), True
    except Exception:
        raw, saved = {}, False
    conf, errs = _pm.validate_config(raw, _padmap_modules())
    st = read_state() or {}
    bound = _live_bindings(st)
    try:
        with open(_PADMAP_HITS) as f:
            hits = json.load(f).get("hits", [])
    except Exception:
        hits = []
    return jsonify({
        "config": conf, "saved": saved, "errors": errs,
        "actions": _pm.ACTION_CHOICES,
        "colors": [{"name": n, "rgb": list(rgb)} for n, rgb in _pm.COLORS],   # surface pad colours (LED levels 0-7)
        "modules": [{"key": m, "label": _VOICE_LABELS.get(m, m), "ch": bound.get(m)}
                    for m in _padmap_modules()],
        "devices": st.get("midi_devices", []) if isinstance(st, dict) else [],
        "hits": hits,
    })


@app.route('/api/padmap', methods=['POST'])
def api_padmap_set():
    import orc1_padmap as _pm
    data = request.get_json(force=True, silent=True) or {}
    conf, errs = _pm.validate_config(data.get("config"), _padmap_modules())
    ok = write_cmd("PADMAP_SET", {"config": conf})
    return jsonify({"ok": bool(ok), "config": conf, "errors": errs})


@app.route('/api/padmap/show', methods=['POST'])
def api_padmap_show():
    """SHOW ON FIRE: put a surface device back in surface mode (the player left it with a mode button)."""
    data = request.get_json(force=True, silent=True) or {}
    dev = str(data.get("device") or "")[:40]
    return jsonify({"ok": bool(write_cmd("SURFACE_SHOW", {"device": dev}))})


# ── /cme: the CME U6MIDI Pro MIDI router. The BRAIN owns the box (orc1_cme.CmeLink,
# the only process that speaks its SysEx) and publishes what it reads to
# ~/.orc1_cme.json; this page reads that file and sends CME_* commands.
_CME_STATE = os.path.expanduser("~/.orc1_cme.json")


@app.route('/cme')
def cme_page():
    return render_template('cme.html')

@app.route('/performve')
def performve_page():
    return render_template('performve.html')


@app.route('/api/cme', methods=['GET'])
def api_cme_get():
    import orc1_cme as _c
    try:
        with open(_CME_STATE) as f:
            st = json.load(f)
    except Exception:
        st = {"connected": False, "missing": True}
    st.pop("raw", None)
    st["inputs"], st["outputs"] = _c.ROUTER_INPUTS, _c.ROUTER_OUTPUTS
    st["filter_ports"], st["filter_measured"] = _c.FILTER_PORTS, list(_c.FILTER_MEASURED)
    st["filter_types"] = [n for n, _b, _bit in _c.FILTER_TYPES]
    st["src_types"], st["dst_types"] = _c.SRC_TYPES, _c.DST_TYPES
    st["value_names"] = {str(k): v for k, v in _c.VALUE_NAMES.items()}
    return jsonify(st)


@app.route('/api/cme', methods=['POST'])
def api_cme_set():
    # The body -> command mapping (and every validation) is orc1_cme.web_command,
    # so _test_cme.py tests exactly what this endpoint sends. A malformed body is
    # ALWAYS a 400 (a non-dict mapper used to raise AttributeError = a 500).
    import orc1_cme as _c
    try:
        action, payload = _c.web_command(request.get_json(force=True, silent=True) or {})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    ok = write_cmd(action, payload)
    return jsonify({"ok": bool(ok)})


# ── /kikpad: the KIKPAD's OWN pad + encoder maps. They live ON THE PAD (its firmware stores
# them, so it plays the same into any host); the BRAIN is the only process that speaks the
# editor SysEx (orc1_kikpad_map.KikpadMapLink) and publishes what the pad replied to
# /tmp/orc1_kikpad_map.json. This page reads that file and sends KIKPAD_MAP_* commands.
@app.route('/kikpad')
def kikpad_page():
    return render_template('kikpad.html')


@app.route('/api/kikpad', methods=['GET'])
def api_kikpad_get():
    import orc1_kikpad_map as _km
    try:
        with open(_km.STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {"connected": False, "missing": True, "map": _km.empty_map(),
              "modes": list(_km.MODE_NAMES), "bank_names": list(_km.BANK_NAMES)}
    st["types"] = list(_km.TYPES)
    st["palette"] = [_km.colour_rgb(c) for c in range(64)]
    return jsonify(st)


@app.route('/api/kikpad', methods=['POST'])
def api_kikpad_set():
    import orc1_kikpad_map as _km
    try:
        action, payload = _km.web_command(request.get_json(force=True, silent=True) or {})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": bool(write_cmd(action, payload))})


_BB_SONGS_DIR = os.path.expanduser("~/.orc1_bbsongs")
_BB_KITS_DIR  = os.path.expanduser("~/orc/orc1/kits")
import glob as _glob
_bb_cat_cache = {"mtime": 0, "by_file": {}, "folder_order": []}


def _bb_catalog_files():
    """Every *catalog*.json in the songs dir -- bb_catalog.json (factory) plus
    slate_catalog.json / any custom catalog. Merged into one browse index."""
    return sorted(_glob.glob(os.path.join(_BB_SONGS_DIR, "*catalog*.json")))


def _bb_load_catalog():
    """Load + merge the catalog file(s) (built by tools/bb_catalog.py and
    slate2song.py), cached by newest mtime. Returns {FILE_UPPER: [entry,...]}
    keyed by staged filename plus an ordered folder list. One physical song file
    can appear under >1 genre."""
    files = _bb_catalog_files()
    m = max((os.path.getmtime(f) for f in files), default=0)
    if m != _bb_cat_cache["mtime"]:
        by_file, order, seen = {}, [], set()
        for path in files:
            try:
                with open(path, encoding="utf-8") as f:
                    cat = json.load(f)
                for fo in cat.get("folders", []):
                    if fo["name"] not in seen:
                        seen.add(fo["name"]); order.append(fo["name"])
                for s in cat.get("songs", []):
                    by_file.setdefault(s["file"].upper(), []).append(s)
            except Exception:
                pass
        _bb_cat_cache.update(mtime=m, by_file=by_file, folder_order=order)
    return _bb_cat_cache["by_file"], _bb_cat_cache["folder_order"]


@app.route('/api/beatbuddy')
def api_beatbuddy():
    """Enriched song list (each staged .BBS joined to its catalog metadata:
    genre folder, name, bpm, time-sig, parts, wanted kit), the kit list (folders,
    index = PC number), and the live mind status."""
    try:
        staged = [f for f in os.listdir(_BB_SONGS_DIR)
                  if f.lower().endswith(".bbs")
                  or (f.lower().endswith(".json") and "catalog" not in f.lower())]
    except Exception:
        staged = []
    by_file, folder_order = _bb_load_catalog()
    songs = []
    for f in staged:
        entries = by_file.get(f.upper())
        if entries:
            for e in entries:
                songs.append({"file": f, "name": e.get("name") or f,
                              "folder": e.get("folder") or "Other",
                              "bpm": e.get("bpm"), "ts": e.get("ts"),
                              "nParts": e.get("nParts"), "kit": e.get("kit") or "",
                              "hasIntro": e.get("hasIntro"), "hasOutro": e.get("hasOutro"),
                              "nFills": e.get("nFills")})
        else:
            songs.append({"file": f, "name": f[:-4], "folder": "Other",
                          "bpm": None, "ts": None, "nParts": None, "kit": ""})
    songs.sort(key=lambda s: (s["folder"].lower(), (s["name"] or "").lower()))
    # ordered list of folders actually present, catalog order first
    present = {s["folder"] for s in songs}
    folders = [f for f in folder_order if f in present] + \
              sorted(f for f in present if f not in folder_order)
    try:
        kits = sorted(d for d in os.listdir(_BB_KITS_DIR)
                      if os.path.isdir(os.path.join(_BB_KITS_DIR, d)))
    except Exception:
        kits = []
    st = read_state() or {}
    return jsonify({"songs": songs, "folders": folders, "kits": kits,
                    "state": st.get("beatbuddy", {})})

@app.route('/api/beatbuddy/cmd', methods=['POST'])
def api_beatbuddy_cmd():
    d = request.get_json(force=True) or {}
    act = d.get("action", "")
    if not act.startswith("BB_"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    ok = write_cmd(act, {k: v for k, v in d.items() if k != "action"})
    return jsonify({"ok": bool(ok)})

@app.route('/cc')
def cc_map_page():
    # The CC map is a standalone generated page (gen_cc_map.py -> static/
    # cc_map.html), not a template. Serve it at a clean URL so it is reachable
    # from the nav rather than only via /static/cc_map.html. Regenerate on the
    # Pi after touching any set_cc handler: `python3 gen_cc_map.py`.
    # 2026-10-07: nobody did, for two weeks, while the page said "read out of the
    # running code". Now it rebuilds itself (niced, ~3 s) when anything it reads is
    # newer than the page; a failed rebuild serves the old page.
    _cc_map_refresh()
    return app.send_static_file('cc_map.html')


@app.route('/screens')
def screen_bench_page():
    # ORC Screen Bench: pixel editor for the HUD's 1.8" ST7735S TFTs and 0.96"
    # SSD1306 OLEDs. A standalone, self-contained page (fonts + JSZip embedded, no
    # internet). Built on the PC by "screen tools/build.ps1" from its source there;
    # edit the source and rebuild, never this file. Static: no brain or JACK access.
    return app.send_static_file('screen_bench.html')


_CC_MAP_LOCK = threading.Lock()


def _cc_map_stale():
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        built = os.path.getmtime(os.path.join(here, "static", "cc_map.html"))
    except OSError:
        return True
    srcs = (glob.glob(os.path.join(here, "*.py"))
            + glob.glob(os.path.join(here, "static", "js", "*.js"))
            + glob.glob(os.path.join(here, "templates", "*.html")))
    return any(os.path.getmtime(p) > built for p in srcs
               if not os.path.basename(p).startswith("_test"))


def _cc_map_refresh():
    if not _cc_map_stale():
        return
    with _CC_MAP_LOCK:                       # one rebuild at a time
        if not _cc_map_stale():
            return
        import subprocess, sys
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            subprocess.run(["nice", "-n", "15", sys.executable, "gen_cc_map.py"],
                           cwd=here, capture_output=True, timeout=60)
        except Exception as e:
            print(f"  ! /cc rebuild failed: {e}", flush=True)

@app.route('/state')
def state():
    d = read_state()
    if d is None:
        return Response('{"error":"no state"}', status=503, mimetype='application/json')
    return Response(json.dumps(d), mimetype='application/json')

@app.route('/cmd', methods=['POST'])
def command():
    try:
        data   = request.get_json(force=True) or {}
        action = data.get('action','')
        if not action:
            return Response('{"ok":false}', mimetype='application/json')
        extra  = {k: v for k, v in data.items() if k != 'action'}
        ok = write_cmd(action, extra)
        return Response(json.dumps({"ok": ok}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok":False,"error":str(e)}),
                        status=500, mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# BEATSTEP PRO API — send SysEx config messages via amidi
# ══════════════════════════════════════════════════════════════════════════════
# GET  /api/beatstep/status   → {connected, port}
# POST /api/beatstep/send     {param:"0B", value:"01"} → {ok, sysex}
#
# SysEx format: F0 00 20 6B 7F 42 02 00 41 [PARAM] [VALUE] F7
# See: https://github.com/Pl0p/Beatstep-pro-Sysex

def _bsp_port():
    """Return the amidi port string for the BeatStep Pro USB port, or None."""
    import subprocess, re
    try:
        out = subprocess.run(["amidi", "-l"], capture_output=True, text=True, timeout=4).stdout
        for line in out.splitlines():
            if "BeatStep Pro Arturia Be" in line:
                m = re.search(r'(hw:\d+,\d+,\d+)', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None

@app.route('/api/beatstep/status')
def api_beatstep_status():
    port = _bsp_port()
    return Response(json.dumps({"connected": port is not None, "port": port}),
                    mimetype='application/json')

@app.route('/api/beatstep/send', methods=['POST'])
def api_beatstep_send():
    import subprocess, re
    data  = request.get_json(force=True) or {}
    # paramId selects the addressing bank: 41 = global params (Tier 1, the
    # default so all existing callers keep working); 01-08 = per-control
    # ControllerMap fields (Tier 2), where `param` is the control's itemId.
    paramId = str(data.get("paramId", "41")).strip().upper()
    param   = str(data.get("param", "")).strip().upper()
    value   = str(data.get("value", "")).strip().upper()
    # Validate: all three must be exactly 2 hex digits
    if (not re.match(r'^[0-9A-F]{2}$', paramId) or
            not re.match(r'^[0-9A-F]{2}$', param) or
            not re.match(r'^[0-9A-F]{2}$', value)):
        return Response(json.dumps({"ok": False,
                                    "error": "paramId, param and value must each be 2 hex digits"}),
                        status=400, mimetype='application/json')
    port = _bsp_port()
    if not port:
        return Response(json.dumps({"ok": False,
                                    "error": "BeatStep Pro not found — check USB connection"}),
                        status=503, mimetype='application/json')
    sysex = f"F0 00 20 6B 7F 42 02 00 {paramId} {param} {value} F7"
    try:
        r = subprocess.run(["amidi", "-p", port, "-S", sysex],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip() or "amidi error"
            return Response(json.dumps({"ok": False, "error": err}),
                            status=500, mimetype='application/json')
        return Response(json.dumps({"ok": True, "sysex": sysex, "port": port}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# Serialize BSP reads: the device drops overlapping request bursts and replies
# are broadcast on a shared port, so two concurrent readers (laptop + phone)
# corrupt each other. Flask is threaded, so a process-wide lock queues reads.
_BSP_READ_LOCK = threading.Lock()

@app.route('/api/beatstep/read', methods=['POST'])
def api_beatstep_read():
    """Read LIVE parameter values back from the BSP. Body: {"pairs": [[paramIdHex,
    itemHex], ...]}. Delegates to bsp_readback.py (rtmidi capture + amidi send).
    Returns {ok, values:{"PPII":int}, missing:[...]}. See bsp_readback.py for the
    request/reply protocol."""
    import subprocess, re
    data  = request.get_json(force=True) or {}
    pairs = data.get("pairs", [])
    # validate: list of [2hex, 2hex]
    if (not isinstance(pairs, list) or
            any(not (isinstance(p, list) and len(p) == 2 and
                     re.match(r'^[0-9A-Fa-f]{2}$', str(p[0])) and
                     re.match(r'^[0-9A-Fa-f]{2}$', str(p[1]))) for p in pairs)):
        return Response(json.dumps({"ok": False, "error": "pairs must be a list of [hex,hex]"}),
                        status=400, mimetype='application/json')
    if not pairs:
        return Response(json.dumps({"ok": True, "values": {}, "missing": []}),
                        mimetype='application/json')
    if not _bsp_port():
        return Response(json.dumps({"ok": False,
                                    "error": "BeatStep Pro not found — check USB connection"}),
                        status=503, mimetype='application/json')
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsp_readback.py")
    # Queue behind any in-flight read (bounded wait) so hardware access is
    # one-at-a-time. Subprocess timeout (35s) sits comfortably above the
    # reader's true worst case (chunked send + a 6s wall-clock retry budget).
    if not _BSP_READ_LOCK.acquire(timeout=40):
        return Response(json.dumps({"ok": False, "error": "reader busy"}),
                        status=503, mimetype='application/json')
    try:
        r = subprocess.run(["python3", helper, json.dumps(pairs)],
                           capture_output=True, text=True, timeout=35)
        out = (r.stdout or "").strip()
        if not out:
            return Response(json.dumps({"ok": False,
                                        "error": (r.stderr or "no output from reader").strip()[:300]}),
                            status=500, mimetype='application/json')
        return Response(out, mimetype='application/json')
    except subprocess.TimeoutExpired:
        return Response(json.dumps({"ok": False, "error": "read timed out"}),
                        status=504, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')
    finally:
        _BSP_READ_LOCK.release()

# ══════════════════════════════════════════════════════════════════════════════
# AUDIO OUTPUT API — pick which sound card JACK runs on (interface-agnostic)
# ══════════════════════════════════════════════════════════════════════════════
# GET  /api/audio/devices -> live card list + current/chosen card
# POST /api/audio/device  {"card": "M3"} -> persist choice, restart orc1-jackd
# The choice lives in ~/.orc1_audio.json (runtime state, not repo);
# orc1_jackd_start.sh reads it on every jackd start. That file holds TWO keys:
#   "card"   -- chosen soundcard short name (this endpoint owns it)
#   "period" -- optional JACK buffer-period override (no web owner; hand-set)
# Any writer here must MERGE so it cannot destroy the other key.

AUDIO_CHOICE = os.path.expanduser("~/.orc1_audio.json")
JACKDRC      = os.path.expanduser("~/.jackdrc")

def _list_audio_cards():
    """Parse `aplay -l` into [{card, name}] (playback-capable cards only)."""
    import subprocess, re
    cards, seen = [], set()
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True,
                             text=True, timeout=5).stdout
        for m in re.finditer(r"^card \d+: (\S+) \[([^\]]*)\]", out, re.M):
            short, desc = m.group(1), m.group(2)
            if short in seen or short == "Loopback":   # Loopback = skipback plumbing
                continue
            seen.add(short)
            cards.append({"card": short, "name": desc})
    except Exception:
        pass
    return cards

def _active_jack_card():
    try:
        with open(JACKDRC) as f:
            import re
            m = re.search(r"-dhw:(\S+?)(?:\s|$)", f.read().replace(" -P", " "))
            return m.group(1) if m else None
    except Exception:
        return None

@app.route('/api/audio/devices')
def api_audio_devices():
    chosen = None
    try:
        with open(AUDIO_CHOICE) as f:
            chosen = json.load(f).get("card")
    except Exception:
        pass
    return Response(json.dumps({
        "devices": _list_audio_cards(),
        "active":  _active_jack_card(),
        "chosen":  chosen,
    }), mimetype='application/json')

@app.route('/api/audio/device', methods=['POST'])
def api_audio_device():
    try:
        import subprocess
        data = request.get_json(force=True) or {}
        card = str(data.get("card", "")).strip()
        valid = {c["card"] for c in _list_audio_cards()}
        if not card or card not in valid:
            return Response('{"ok":false,"error":"unknown card"}',
                            status=400, mimetype='application/json')
        # MERGE, don't overwrite: this file also carries a "period" key that
        # orc1_jackd_start.sh reads as the JACK buffer-period override. A blind
        # json.dump({"card": ...}) silently destroyed it, dropping the L12 back
        # to period 512 and causing xruns for months. Preserve every other key.
        try:
            with open(AUDIO_CHOICE) as f:
                choice = json.load(f)
            if not isinstance(choice, dict):
                choice = {}
        except Exception:
            choice = {}
        choice["card"] = card
        tmp = AUDIO_CHOICE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(choice, f)
        os.replace(tmp, AUDIO_CHOICE)
        # orc1-jackd owns jackd (2026-07-14) and resolves the card in
        # orc1_jackd_start.sh, so the CARD switch must restart THAT unit -- the
        # synth's BindsTo=orc1-jackd cascades a synth restart automatically.
        # Restarting orc1-synth alone never rebinds jackd to the new card.
        subprocess.Popen(["sudo", "systemctl", "restart", "orc1-jackd"])
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# SKIPBACK SOURCE API — assign what the ring buffer (and thus the sampler) hears
# ══════════════════════════════════════════════════════════════════════════════
# The skipback RECORDER (JACK client 'skipback-rec', skipback_recorder.py)
# records whatever is connected to its track-0 input ports. These endpoints
# rewire that LIVE via jack_connect/jack_disconnect and persist the choice to
# ~/.orc1_skipback_src.json, which the recorder re-reads on a ~5s watchdog
# (_refresh_src_override) -- so the persisted write lands even if the live
# rewire below is a no-op.
#
# 2026-07-29: these were 'skipback-bridge:playback_1/2' -- the pre-cutover
# bridge client, retired/masked on 2026-07-19. That client does not exist, so
# every jack_connect here failed ("not a valid port"), was swallowed by
# capture_output=True, and the endpoint still returned ok -- making the source
# card read "not connected" forever and the live rewire a silent no-op.

SKIPBACK_SRC  = os.path.expanduser("~/.orc1_skipback_src.json")
def _sb_track0_ports():
    """Live JACK input ports for skipback track 0, resolved AT CALL TIME.

    The recorder names ports by channel count (Track.port_names): a stereo
    track registers t<id>_L / t<id>_R, a MONO track registers only t<id>_M.
    Track 0's stereo flag is user-editable on /skipback, so hardcoding the
    stereo pair would reproduce the exact bug this module was just fixed for:
    a port name that does not exist makes every jack_connect fail silently
    (capture_output=True swallows it) and the source picker read
    "not connected" forever.

    Returns a tuple of 1 (mono) or 2 (stereo) real port names.
    """
    stereo = True
    try:
        for t in _sb_load_tracks_file():
            if isinstance(t, dict) and t.get("id") == 0:
                stereo = bool(t.get("stereo", True))
                break
    except Exception:
        pass                                   # default to the stereo pair
    if stereo:
        return ("skipback-rec:t0_L", "skipback-rec:t0_R")
    return ("skipback-rec:t0_M",)

_jack_last_error = [None]


def _jack(args):
    """Run a jack CLI tool. "" if it cannot run at all.

    Returning empty rather than raising is what makes the callers' carefully
    written "jackd is not reachable" paths REACHABLE. Before this, a wedged
    jackd -- a documented state on this rig, where audio keeps playing but
    jack_lsp hangs -- raised TimeoutExpired out of the request handler and
    Flask answered with a 500 HTML page, so the honest 503 and the "unknown"
    state designed for exactly that condition could never be shown.
    """
    import subprocess
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=5).stdout
    except Exception as e:
        # Deduped: /patch polls several endpoints on a timer, so an
        # unreachable jackd would otherwise write thousands of identical
        # journal lines an hour to an SD card.
        msg = "%s: %s" % (args[0] if args else "?", e)
        if _jack_last_error[0] != msg:
            print("[jack] %s" % msg, flush=True)
            _jack_last_error[0] = msg
        return ""

def _jack_sources():
    """All JACK AUDIO OUTPUT ports (things the ring could listen to). -t adds
    each port's type line after its properties ("32 bit float mono audio" /
    "8 bit raw midi"): a MIDI port is no source for an audio input."""
    out, ports = _jack(["jack_lsp", "-p", "-t"]), []
    cur, is_out = None, False
    for line in out.splitlines():
        if not line.startswith(("\t", " ")):
            cur, is_out = line.strip(), False
        elif "properties:" in line:
            is_out = "output" in line
        elif cur and is_out and line.strip().endswith("audio"):
            ports.append(cur)
            cur = None
    return ports

def _skipback_current(ports=None):
    """Current connections of track 0's input port(s)."""
    ports = ports if ports is not None else _sb_track0_ports()
    out = _jack(["jack_lsp", "-c"])
    conns, cur = {p: [] for p in ports}, None
    for line in out.splitlines():
        if not line.startswith(("\t", " ")):
            cur = line.strip()
        elif cur in conns:
            conns[cur].append(line.strip())
    return conns

@app.route('/api/skipback/source')
def api_skipback_get():
    ports = _sb_track0_ports()
    cur = _skipback_current(ports)
    return Response(json.dumps({
        "sources": _jack_sources(),
        "left":    cur[ports[0]],
        # Mono track 0 has no right port -- report empty rather than IndexError.
        "right":   cur[ports[1]] if len(ports) > 1 else [],
    }), mimetype='application/json')

@app.route('/api/skipback/source', methods=['POST'])
def api_skipback_post():
    try:
        import subprocess
        data  = request.get_json(force=True) or {}
        left  = str(data.get("left", "")).strip()
        right = str(data.get("right", "")).strip()
        ports = _sb_track0_ports()
        valid = set(_jack_sources())
        # A mono track 0 has one input, so only 'left' is required/used.
        if left not in valid or (len(ports) > 1 and right not in valid):
            return Response('{"ok":false,"error":"unknown port"}',
                            status=400, mimetype='application/json')
        # live rewire: drop existing, connect chosen
        cur = _skipback_current(ports)
        for sb_port, new_src in zip(ports, (left, right)):
            for old in cur[sb_port]:
                subprocess.run(["jack_disconnect", old, sb_port],
                               capture_output=True, timeout=5)
            subprocess.run(["jack_connect", new_src, sb_port],
                           capture_output=True, timeout=5)
        tmp = SKIPBACK_SRC + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"left": left, "right": right}, f)
        os.replace(tmp, SKIPBACK_SRC)
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# SAMPLER VIEW API — waveform peaks of each deck's current capture
# ══════════════════════════════════════════════════════════════════════════════
# The synth process writes ~/.orc1_sampler_view_<deck>.json (240 min/max
# pairs, atomic) on every capture / nudge re-slice; this just serves it so
# the sampler cards can DRAW what got grabbed instead of leaving captures
# a guessing game. 404 = nothing captured yet.

# File decks: list loadable audio files from the sampler's allowed roots
# (recordings + samples — same roots the engine validates loads against;
# read from orc1_config so the two can't drift). Loading goes through the
# normal /cmd path as SAMPLER_LOAD_FILE {deck, path}.
try:
    import orc1_config as _cfg
    _FILE_ROOTS = _cfg.SAMPLER.get("FILE_ROOTS",
                                   [os.path.expanduser("~/recordings")])
except Exception:
    _FILE_ROOTS = [os.path.expanduser("~/recordings")]
_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".aif", ".aiff")

@app.route('/api/sampler/files')
def api_sampler_files():
    files = []
    for root in _FILE_ROOTS:
        root = os.path.expanduser(root)
        try:
            for name in os.listdir(root):
                if not name.lower().endswith(_AUDIO_EXTS):
                    continue
                p = os.path.join(root, name)
                try:
                    st = os.stat(p)
                    files.append({"name": name, "path": p,
                                  "mb": round(st.st_size / 1048576, 1),
                                  "mtime": int(st.st_mtime)})
                except OSError:
                    pass
        except OSError:
            pass
    files.sort(key=lambda f: -f["mtime"])
    return Response(json.dumps({"files": files}), mimetype='application/json')

@app.route('/api/sampler/view/<deck>')
def api_sampler_view(deck):
    deck = deck.upper()
    if deck not in ("A", "B"):
        return Response('{"error":"bad deck"}', status=400,
                        mimetype='application/json')
    path = os.path.expanduser(f"~/.orc1_sampler_view_{deck}.json")
    try:
        with open(path) as f:
            return Response(f.read(), mimetype='application/json')
    except Exception:
        return Response('{"error":"no capture"}', status=404,
                        mimetype='application/json')

def _sampler_current_source_key():
    """Track 0's live capture source key (its `resolved` ports), mirroring the
    synth's SamplerManager._current_source_key. '__default__' if unresolvable."""
    try:
        with open(os.path.expanduser("~/.orc1_skipback_status.json")) as f:
            st = json.load(f)
        for t in st.get("tracks", []):
            if int(t.get("id", -1)) == 0:
                res = [str(p) for p in (t.get("resolved") or []) if p]
                if res:
                    return ",".join(sorted(res))
                break
    except Exception:
        pass
    return "__default__"


@app.route('/api/sampler/press_trim')
def api_sampler_press_trim():
    """Capture-timing state for the /sampler page: the PRESS trim (ms, <=0) for
    the CURRENT capture source, the onset-snap flag, and the source key. Reads
    the synth's persisted per-source file (2026-09-15 format), with a fallback
    to the legacy flat {"press_latency_ms": x}."""
    path = os.path.expanduser("~/.orc1_sampler_press_trim.json")
    ms, onset = 0.0, True
    key = _sampler_current_source_key()
    try:
        with open(path) as f:
            data = json.load(f)
        by = data.get("by_source")
        if isinstance(by, dict):
            if key in by:
                ms = float(by[key])
            elif "__default__" in by:
                ms = float(by["__default__"])
            onset = bool(data.get("onset_snap", True))
        elif "press_latency_ms" in data:          # legacy flat
            ms = float(data["press_latency_ms"])
    except Exception:
        pass
    return Response(json.dumps({"ms": ms, "onset_snap": onset, "source": key}),
                    mimetype='application/json')


@app.route('/api/sampler/deck_source')
def api_sampler_deck_source():
    """Per-deck GRAB source for the /sampler page: which skipback track each deck
    grabs from, plus the list of skipback tracks (id, name, mono/stereo, live
    record-source) so the selector can label each ring and show what it is
    actually recording. The sampler host writes ~/.orc1_sampler_deck_src.json on
    every change; a missing file => the boot defaults (deck A->t0, deck B->t1).
    Repointing a deck goes over the cmd socket as SAMPLER_SET_SOURCE (like every
    other sampler control); repointing what a ring RECORDS lives on /skipback."""
    cur = {"A": 0, "B": 1}
    try:
        with open(os.path.expanduser("~/.orc1_sampler_deck_src.json")) as f:
            d = json.load(f)
        for deck in ("A", "B"):
            if deck in d:
                cur[deck] = int(d[deck])
    except Exception:
        pass
    by_id, _ = _sb_read_status()
    tracks = []
    for t in _sb_load_tracks_file():
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        st = by_id.get(tid, {})
        res = [str(p) for p in (st.get("resolved") or []) if p]
        tracks.append({
            "id":        tid,
            "name":      t.get("name") or ("TRACK %s" % tid),
            "stereo":    bool(t.get("stereo", True)),
            "armed":     bool(t.get("armed", True)),
            "store":     "card" if t.get("store") == "card" else "ram",
            "recording": bool(st.get("recording")),
            "source":    " + ".join(res) if res else "",
        })
    return Response(json.dumps({"decks": cur, "tracks": tracks}),
                    mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# AUTOSAMPLER API — MIDI sweep → capture → MPC-ready WAVs + .PGM
# ══════════════════════════════════════════════════════════════════════════════
# Job runs inside the engine (orc1_autosampler.py); start/cancel go over the
# cmd socket, progress is read from ~/.orc1_autosample_job.json (atomic,
# written by the job thread after every step — same pattern as sampler views).

AUTOSAMPLE_JOB = os.path.expanduser("~/.orc1_autosample_job.json")

@app.route('/api/autosample/status')
def api_autosample_status():
    try:
        with open(AUTOSAMPLE_JOB) as f:
            return Response(f.read(), mimetype='application/json')
    except Exception:
        return Response('{"running":false,"phase":"idle"}',
                        mimetype='application/json')

@app.route('/api/autosample/options')
def api_autosample_options():
    """MIDI destinations + JACK capture sources + config defaults."""
    try:
        import orc1_config as _c
        ports = ["synth"] + [k for k in _c.PORTS.keys() if k != "synth"]
        defaults = {k.lower(): v for k, v in
                    getattr(_c, "AUTOSAMPLER", {}).items()}
        # The default voice is the poly module: its live /matrix channel and
        # its own output pair.
        poly = _live_bindings(read_state() or {}).get("poly")
        if poly:
            defaults["channel"] = poly
        jn = getattr(_c, "MODULE_JACK_NAMES", {}).get("poly")
        if jn:
            defaults["capture_l"], defaults["capture_r"] = jn + ":out_L", jn + ":out_R"
    except Exception:
        ports, defaults = ["synth"], {}
    return Response(json.dumps({
        "ports": ports,
        "sources": _jack_sources(),      # reuse the skipback helper
        "defaults": defaults,
    }), mimetype='application/json')

@app.route('/api/autosample/start', methods=['POST'])
def api_autosample_start():
    try:
        params = request.get_json(force=True) or {}
        ok = write_cmd("AUTOSAMPLE_START", {"params": params})
        return Response(json.dumps({"ok": ok}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

@app.route('/api/autosample/cancel', methods=['POST'])
def api_autosample_cancel():
    ok = write_cmd("AUTOSAMPLE_CANCEL")
    return Response(json.dumps({"ok": ok}), mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# CHORD OUTPUT API — runtime port/channel override for the chord engine
# ══════════════════════════════════════════════════════════════════════════════
# GET  /api/chord_out -> {port, channel, ports:[key,...]}
# POST /api/chord_out {port, channel} -> set override; omit channel to keep preset ch
# DELETE /api/chord_out -> clear override (back to preset routing)

CHORD_OUT = os.path.expanduser("~/.orc1_chord_out.json")

@app.route('/api/chord_out', methods=['GET'])
def api_chord_out_get():
    # Current override comes from the live state (most up to date)
    st = read_state() or {}
    co = st.get("chord_out", {"port": None, "channel": None})
    # Available port keys from config, excluding internal synth
    try:
        import orc1_config as _cfg
        ports = [k for k in _cfg.PORTS.keys() if k != "synth"]
    except Exception:
        ports = []
    return Response(json.dumps({
        "port":    co.get("port"),
        "channel": co.get("channel"),
        "ports":   ports,
    }), mimetype='application/json')

@app.route('/api/chord_out', methods=['POST'])
def api_chord_out_set():
    try:
        data    = request.get_json(force=True) or {}
        port    = str(data.get("port", "")).strip() or None
        channel = data.get("channel")
        if channel is not None:
            channel = int(channel)
            if not (1 <= channel <= 16):
                return Response('{"ok":false,"error":"channel 1-16"}',
                                status=400, mimetype='application/json')
            if _protect_error(channel):
                return _protected_refusal(channel)
        write_cmd("SET_CHORD_OUT", {"port": port, "channel": channel})
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

@app.route('/api/chord_out', methods=['DELETE'])
def api_chord_out_clear():
    write_cmd("CLEAR_CHORD_OUT")
    return Response('{"ok":true}', mimetype='application/json')


# GET  /api/performve_feed  -> {on: bool}
# POST /api/performve_feed  {on: bool} -> ADDITIVE Perform-VE follow (mirror the
#   sounding chord to the 'performve' port on ch1; touches no other routing).
@app.route('/api/performve_feed', methods=['GET'])
def api_performve_feed_get():
    st = read_state() or {}
    return Response(json.dumps({"on": bool(st.get("performve_feed", False))}),
                    mimetype='application/json')

@app.route('/api/performve_feed', methods=['POST'])
def api_performve_feed_set():
    try:
        data = request.get_json(force=True) or {}
        write_cmd("SET_PERFORMVE_FEED", {"on": bool(data.get("on"))})
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# GET  /api/performve_src  -> {port, channel, sources:[...]}
# POST /api/performve_src  {port: null|"usb"|"din"|"both", channel: null|1-16}
#   -> claim an external chord stream for the box, CONSUMED (option A).
@app.route('/api/performve_src', methods=['GET'])
def api_performve_src_get():
    st = read_state() or {}
    ps = st.get("performve_src", {"port": None, "channel": None})
    return Response(json.dumps({
        "port":    ps.get("port"),
        "channel": ps.get("channel"),
        "sources": ["usb", "din", "both"],
    }), mimetype='application/json')

@app.route('/api/performve_src', methods=['POST'])
def api_performve_src_set():
    try:
        data = request.get_json(force=True) or {}
        port = data.get("port") or None
        if port not in (None, "usb", "din", "both"):
            return Response('{"ok":false,"error":"port must be usb|din|both|null"}',
                            status=400, mimetype='application/json')
        channel = data.get("channel")
        if channel is not None:
            channel = int(channel)
            if not (1 <= channel <= 16):
                return Response('{"ok":false,"error":"channel 1-16"}',
                                status=400, mimetype='application/json')
        write_cmd("SET_PERFORMVE_SRC", {"port": port, "channel": channel})
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# POST /api/performve_cc  {cc: 0-127, value: 0-127}
#   Fire one Control Change at the Perform-VE (ch1). The box is driven entirely by
#   CCs (FINDINGS.md §3c) and does not report state back, so this is write-only.
@app.route('/api/performve_cc', methods=['POST'])
def api_performve_cc():
    try:
        data = request.get_json(force=True) or {}
        cc = int(data.get("cc")); value = int(data.get("value"))
        if not (0 <= cc <= 127 and 0 <= value <= 127):
            return Response('{"ok":false,"error":"cc/value 0-127"}',
                            status=400, mimetype='application/json')
        ok = write_cmd("SET_PERFORMVE_CC", {"cc": cc, "value": value})
        return Response(json.dumps({"ok": bool(ok)}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# POST /api/performve_pc  {preset: 0-127}  (PC 0/1/2 = presets 1/2/3)
@app.route('/api/performve_pc', methods=['POST'])
def api_performve_pc():
    try:
        data = request.get_json(force=True) or {}
        preset = int(data.get("preset"))
        if not (0 <= preset <= 127):
            return Response('{"ok":false,"error":"preset 0-127"}',
                            status=400, mimetype='application/json')
        ok = write_cmd("SET_PERFORMVE_PC", {"preset": preset})
        return Response(json.dumps({"ok": bool(ok)}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ── Perform-VE audio INSERT (convenience toggle) ──────────────────────────────
# Routes one ORC module THROUGH the Perform-VE over USB, cable-free:
#   <module>:out_L/out_R  ->  performve-out:playback_3  (= USB-in ch3 "Dry vocal")
#   performve-in:capture_1/2 (= USB-out processed Mix)  ->  system:playback_1/2 (main)
# ADDITIVE send/return (never removes the module's own output -> no double-ownership;
# for a true dry-replace insert, mute the module's direct out on /patch). Wires are
# owned here and are NOT /patch-saved, so the /patch reconciler leaves them alone.
# The /patch page does the same thing by hand (PERFORMVE OUT 3 / PERFORMVE IN 1/2)
# and persists it through its own saved-connections reconciler.
_PVE_SEND = "performve-out:playback_3"
_PVE_RET  = [("performve-in:capture_1", "system:playback_1"),
             ("performve-in:capture_2", "system:playback_2")]
_PVE_INSERT_FILE = os.path.expanduser("~/.orc1_performve_insert.json")

def _pve_modules():
    """Module JACK clients that expose out_L/out_R right now (live, never a table)."""
    mods = set()
    for line in _jack(["jack_lsp"]).splitlines():
        line = line.strip()
        if line.startswith("ORC1-") and (line.endswith(":out_L") or line.endswith(":out_R")):
            mods.add(line.split(":", 1)[0])
    return sorted(mods)

def _pve_bridge_up():
    return any(l.strip() == _PVE_SEND for l in _jack(["jack_lsp"]).splitlines())

def _pve_bridge_set(up):
    """Start (and wait for) or stop the Perform-VE JACK bridges on demand. Starting
    performve-in pulls performve-out (Requires=). Passwordless sudo, as /patch CARDS."""
    import subprocess
    try:
        if up:
            if _pve_bridge_up():
                return True
            subprocess.run(["sudo", "systemctl", "start", "performve-in"],
                           capture_output=True, text=True, timeout=25)
            for _ in range(24):          # bridges take a moment to clock up
                time.sleep(0.5)
                if _pve_bridge_up():
                    return True
            return _pve_bridge_up()
        subprocess.run(["sudo", "systemctl", "stop", "performve-in", "performve-out"],
                       capture_output=True, text=True, timeout=25)
        return True
    except Exception:
        return _pve_bridge_up()

def _pve_other_connections():
    """True if any Perform-VE bridge port is connected to something OTHER than this
    insert's own wires (e.g. a /patch wire). Used to avoid stopping a bridge someone
    else is using when the insert is turned off."""
    out, cur = _jack(["jack_lsp", "-c"]), None
    ours = {_PVE_SEND} | {s for s, _ in _PVE_RET} | {d for _, d in _PVE_RET} \
         | {m + ":out_L" for m in _pve_modules()} | {m + ":out_R" for m in _pve_modules()}
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = line.strip()
        elif cur and ("performve-in:" in cur or "performve-out:" in cur):
            if line.strip() not in ours:
                return True
    return False

def _pve_insert_load():
    try:
        d = json.load(open(_PVE_INSERT_FILE))
        return {"on": bool(d.get("on")), "source": d.get("source") or None}
    except Exception:
        return {"on": False, "source": None}

def _pve_insert_save(st):
    try:
        tmp = _PVE_INSERT_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(st, f)
        os.replace(tmp, _PVE_INSERT_FILE)
    except Exception:
        pass

def _pve_insert_apply(on, source):
    """Make the live JACK graph match (on, source). Idempotent: first tear down ALL
    insert wires (send from every module + the two returns), then wire the chosen
    source if on. Best-effort -- _jack swallows failures when the bridge is down."""
    for m in _pve_modules():                       # drop any module -> send wire
        _jack(["jack_disconnect", m + ":out_L", _PVE_SEND])
        _jack(["jack_disconnect", m + ":out_R", _PVE_SEND])
    for src, dst in _PVE_RET:
        _jack(["jack_disconnect", src, dst])
    if on and source and source in _pve_modules() and _pve_bridge_up():
        _jack(["jack_connect", source + ":out_L", _PVE_SEND])
        _jack(["jack_connect", source + ":out_R", _PVE_SEND])
        for src, dst in _PVE_RET:
            _jack(["jack_connect", src, dst])
        return True
    return False

@app.route('/api/performve_insert', methods=['GET'])
def api_performve_insert_get():
    st = _pve_insert_load()
    return Response(json.dumps({
        "on": st["on"], "source": st["source"],
        "sources": _pve_modules(),
        "bridge_up": _pve_bridge_up(),
    }), mimetype='application/json')

@app.route('/api/performve_insert', methods=['POST'])
def api_performve_insert_set():
    try:
        data = request.get_json(force=True) or {}
        on = bool(data.get("on"))
        source = data.get("source") or None
        if on:
            if not _pve_bridge_set(True):
                return Response(json.dumps({"ok": False,
                    "error": "could not start the Perform-VE JACK bridge"}),
                    status=503, mimetype='application/json')
            applied = _pve_insert_apply(True, source)
            _pve_insert_save({"on": applied, "source": source})
            return Response(json.dumps({"ok": True, "on": applied, "source": source}),
                            mimetype='application/json')
        # off: drop our wires, then stop the bridge IFF nobody else is using it
        # (so an insert OFF never kills a /patch wire through the same box).
        _pve_insert_apply(False, source)
        if not _pve_other_connections():
            _pve_bridge_set(False)
        _pve_insert_save({"on": False, "source": source})
        return Response(json.dumps({"ok": True, "on": False, "source": source}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

def _pve_insert_restore():
    """Re-apply the saved insert on web start: start the bridge on demand, then wire."""
    try:
        st = _pve_insert_load()
        if st["on"] and _pve_bridge_set(True):
            _pve_insert_apply(True, st["source"])
    except Exception:
        pass


# GET  /api/voice_thru  -> {thru:{ch:{"port","channel"},...}, voices:[{name,ch},...], ports:[...]}
# POST /api/voice_thru  {src_ch, port, channel} -> set one mapping
# DELETE /api/voice_thru/<src_ch> -> clear one channel (or all if src_ch == "0")
_VOICE_LABELS = {"drums": "drums", "poly": "poly", "chip_p1": "chip P1",
                 "chip_p2": "chip P2", "chip_tri": "chip tri",
                 "chip_noise": "chip ns", "chip_noise2": "chip ns2",
                 "gb_wave": "gb wave", "chip_pcm": "pcm", "sampler": "sampler",
                 "mono": "mono", "bbkit": "bbkit", "visuals": "visuals",
                 "zoomfx": "zoom fx"}


def _live_bindings(st=None):
    """{module: channel} for every BOUND module: the brain's live table (in
    its state file), else its persisted ~/.orc1_bindings.json. NEVER the
    web's cfg SYNTH.CH_* -- those are only the first-boot seed (session 39:
    pages reading them drove whatever module now sat on that channel)."""
    b = (st or {}).get("bindings") if isinstance(st, dict) else None
    if isinstance(b, dict) and b:
        try:
            return {str(m): int(c) for m, c in b.items() if c}
        except (TypeError, ValueError):
            pass
    try:
        import orc1_config as _cfg
        with open(os.path.expanduser(getattr(_cfg, "BINDINGS_FILE",
                                             "~/.orc1_bindings.json"))) as f:
            return {str(m): int(c) for c, m in
                    json.load(f).get("bindings", {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


@app.route('/api/voice_thru', methods=['GET'])
def api_voice_thru_get():
    st = read_state() or {}
    try:
        import orc1_config as _cfg
        # Every BOUND module, labelled from the live /matrix bindings (this
        # listed the shipped cfg channels: "poly ch 8" was really chip P2 on
        # the rig, and mono/bbkit were missing; 2026-10-01).
        order = list(getattr(_cfg, "MODULE_KEYS", []))
        bound = _live_bindings(st)
        voices = [{"name": _VOICE_LABELS.get(m, m), "module": m, "ch": ch}
                  for m, ch in sorted(bound.items(),
                                      key=lambda mc: (mc[1], order.index(mc[0])
                                                      if mc[0] in order else 99))]
        ports = [k for k in _cfg.PORTS.keys() if k != "synth"]
    except Exception:
        voices, ports = [], []
    return Response(json.dumps({
        "thru":   st.get("voice_thru", {}),
        "voices": voices,
        "ports":  ports,
    }), mimetype='application/json')

@app.route('/api/voice_thru', methods=['POST'])
def api_voice_thru_set():
    try:
        data   = request.get_json(force=True) or {}
        src_ch = int(data.get("src_ch", 0))
        port   = str(data.get("port", "")).strip() or None
        ch_out = data.get("channel")
        if not (1 <= src_ch <= 16):
            return Response('{"ok":false,"error":"src_ch 1-16"}',
                            status=400, mimetype='application/json')
        if not port:
            return Response('{"ok":false,"error":"port required"}',
                            status=400, mimetype='application/json')
        if ch_out is None or not (1 <= int(ch_out) <= 16):
            return Response('{"ok":false,"error":"channel 1-16"}',
                            status=400, mimetype='application/json')
        if _protect_error(ch_out):
            return _protected_refusal(ch_out)
        write_cmd("SET_VOICE_THRU", {"src_ch": src_ch, "port": port,
                                      "channel": int(ch_out)})
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

@app.route('/api/voice_thru/<int:src_ch>', methods=['DELETE'])
def api_voice_thru_clear(src_ch):
    write_cmd("CLEAR_VOICE_THRU", {"src_ch": src_ch})
    return Response('{"ok":true}', mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# ROUTING API
# ══════════════════════════════════════════════════════════════════════════════

def _routes_new_protected(old, new):
    """'' or why a NEW / changed rule's out_ch is refused (PROTECTED CHANNELS). Rules already saved are
    left alone, so protecting a channel never blocks saving the rest of the page."""
    def rules(d):
        out = set()
        for key, dev in ((d or {}).get("devices") or {}).items():
            for r in (dev or {}).get("rules") or []:
                out.add((key, json.dumps(r, sort_keys=True)))
        return out
    for key, rs in rules(new) - rules(old):
        r = json.loads(rs)
        oc = r.get("out_ch", "same")
        if oc not in (None, "same") and _protect_error(oc):
            return f"{key}: {_protect_error(oc)}"
    return ""


def _read_routes():
    try:
        with open(ROUTES) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1, "devices": {}}
    except Exception as e:
        return {"error": str(e)}

def _write_routes(data):
    tmp = ROUTES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ROUTES)

@app.route('/api/routes', methods=['GET'])
def api_routes_get():
    return Response(json.dumps(_read_routes()), mimetype='application/json')

@app.route('/api/routes', methods=['POST'])
def api_routes_post():
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict) or 'devices' not in data:
            return Response('{"ok":false,"error":"invalid"}', status=400, mimetype='application/json')
        bad = _routes_new_protected(_read_routes(), data)
        if bad:
            return Response(json.dumps({"ok": False, "error": bad}), status=400,
                            mimetype='application/json')
        _write_routes(data)
        write_cmd('RELOAD_ROUTES')
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# ══════════════════════════════════════════════════════════════════════════════
# SEQUENCER API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/seq', methods=['POST'])
def api_seq():
    """Dispatch a SEQ_* command to the engine via the cmd socket."""
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "")
        if not action.startswith("SEQ_"):
            return Response('{"ok":false,"error":"not a seq action"}',
                            status=400, mimetype='application/json')
        write_cmd(action)
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# BLUETOOTH AUDIO API — on-demand A2DP sink (phone -> JACK -> L12 main out)
# ══════════════════════════════════════════════════════════════════════════════
# Flips the on-demand helper scripts installed on the Pi:
#   bt-audio-on  = unblock/power radio, start auto-accept pairing agent,
#                  become discoverable as "pibox"
#   bt-audio-off = stop advertising + power the radio down
# Audio itself bridges to the L12 via bluealsa -> orc_bt (see /etc/asound.conf)
# and orc-bt-jack.service. This only toggles availability; the engine/synth
# are never touched.

def _bt(args):
    import subprocess
    return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout

def _bt_status():
    powered = discoverable = False
    device = None
    try:
        show = _bt(["bluetoothctl", "show"])
        powered      = "Powered: yes"      in show
        discoverable = "Discoverable: yes" in show
    except Exception:
        pass
    try:
        con = _bt(["bluetoothctl", "devices", "Connected"]).strip()
        if con:
            # line looks like: "Device AA:BB:CC:DD:EE:FF My Phone"
            parts = con.splitlines()[0].split(" ", 2)
            device = parts[2] if len(parts) > 2 else parts[-1]
    except Exception:
        pass
    return {"on": powered and discoverable,
            "connected": bool(device), "device": device}

@app.route('/api/bt/status')
def api_bt_status():
    return Response(json.dumps(_bt_status()), mimetype='application/json')

@app.route('/api/bt/<action>', methods=['POST'])
def api_bt_toggle(action):
    if action not in ("on", "off"):
        return Response('{"ok":false,"error":"on|off"}', status=400,
                        mimetype='application/json')
    try:
        import subprocess
        subprocess.run(["/usr/local/bin/bt-audio-" + action],
                       capture_output=True, text=True, timeout=20)
        return Response(json.dumps({"ok": True, **_bt_status()}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ── Bluetooth OUTPUT routing ──────────────────────────────────────────────────
# The phone's bluealsa-aplay client renames itself on every pause/resume, so a
# routing pinned to its name never sticks and it cannot live in the patch grid.
# The persistent bt-route.service watches for the client and wires it to the
# output saved here; this endpoint reads/writes that saved choice and, for an
# instant response, snaps any live stream over right now.
# NOTE: keep _BT_DEST / _BT_OUT_FILE in sync with orc1_bt_route.py (DEST/PREF_FILE).
_BT_OUT_FILE = os.path.expanduser("~/.orc1_bt_out.json")
_BT_DEST = {
    "out-1-2":     ("system:playback_1",      "system:playback_2"),
    "out-3-4":     ("system:playback_3",      "system:playback_4"),
    "pisound-out": ("pisound-out:playback_1", "pisound-out:playback_2"),
    "l12":         ("l12-out:playback_1",     "l12-out:playback_2"),
}
_BT_DEST_LABEL = {
    "out-1-2":     "OUT-A · main (follows card)",
    "out-3-4":     "OUT-B",
    "pisound-out": "PS-O · pisound",
    "l12":         "L-12 (only when jackd fell back to pisound)",
}
_BT_DEFAULT_OUT = "out-1-2"

def _bt_out_read():
    try:
        with open(_BT_OUT_FILE) as f:
            v = json.load(f).get("out", _BT_DEFAULT_OUT)
    except Exception:
        v = _BT_DEFAULT_OUT
    return v if v in _BT_DEST else _BT_DEFAULT_OUT

def _bt_out_write(did):
    tmp = _BT_OUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"out": did}, f)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, _BT_OUT_FILE)

def _bt_live_ports():
    """Current bluealsa-aplay output ports, if the phone is streaming (else [])."""
    return sorted(p for p in _jack_ports()
                  if p.startswith("bluealsa-aplay.") and ":out_" in p)

def _bt_port_conns(p):
    """Real connections of a bluealsa out port (drop the echoed self/sibling)."""
    return [c for c in _jack(["jack_lsp", "-c", p]).split()
            if c != p and not c.startswith("bluealsa-aplay.")]

def _bt_snap_live(did):
    """Move any live bluealsa stream to `did` NOW -- connect-first-then-disconnect,
    and never touch a wire whose target port is absent. Same safety contract as the
    connector: a bad target can never silence bluetooth."""
    ports_now = _jack_ports()
    lport, rport = _BT_DEST[did]
    for p in _bt_live_ports():
        try:
            idx = int(p.rsplit("out_", 1)[1])
        except Exception:
            continue
        target = lport if (idx % 2 == 0) else rport
        if target not in ports_now:
            continue                                 # missing target -> leave alone
        cur = _bt_port_conns(p)
        if target not in cur:
            _jack(["jack_connect", p, target])
            if target not in _bt_port_conns(p):
                continue                             # connect didn't take -> leave alone
        for c in _bt_port_conns(p):
            if c != target:
                _jack(["jack_disconnect", p, c])

@app.route('/api/bt/output', methods=['GET', 'POST'])
def api_bt_output():
    if request.method == 'POST':
        did = (request.get_json(silent=True) or {}).get("out", "")
        if did not in _BT_DEST:
            return Response(json.dumps({"ok": False, "error": "unknown output"}),
                            status=400, mimetype='application/json')
        try:
            _bt_out_write(did)
            _bt_snap_live(did)
        except Exception as e:
            return Response(json.dumps({"ok": False, "error": str(e)}),
                            status=500, mimetype='application/json')
    ports = _jack_ports()
    opts  = [{"id": k, "label": _BT_DEST_LABEL.get(k, k),
              "available": _BT_DEST[k][0] in ports} for k in _BT_DEST]
    return Response(json.dumps({"ok": True, "out": _bt_out_read(), "options": opts,
                                "streaming": bool(_bt_live_ports())}),
                    mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# PHASE-2 APIs — presets, FX chains, sample slots
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/presets')
def api_presets():
    """List all preset names + which one is currently active.
    Returns: {presets: [name,...], active: name|null}
    """
    try:
        import orc1_config as cfg
        names  = list(cfg.PRESETS.keys())
        state  = read_state()
        active = state.get("preset") if state else None
        return Response(json.dumps({"presets": names, "active": active}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, mimetype='application/json')

_FX_CHAINS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fx_chains.json")

@app.route('/api/fx')
def api_fx():
    """All FX chains with metadata + live running state.
    Returns: {chains: [{name, description, running, params:[{symbol,label,min,max,default},...]},...]}
    Mixtrack slot binding: deck1 → chains[0..2], deck2 → chains[3..5] (fx_chains.json order).
    """
    try:
        with open(_FX_CHAINS_JSON) as f:
            raw = json.load(f)
        state   = read_state() or {}
        fx_live = state.get("fx", {})   # {name: {running, pids, description}}
        chains  = []
        for name, chain_def in raw.items():
            params = []
            for plugin in chain_def.get("plugins", []):
                for p in plugin.get("params", []):
                    params.append({
                        "symbol":  p["symbol"],
                        "label":   p.get("label", p["symbol"]),
                        "min":     p.get("min", 0.0),
                        "max":     p.get("max", 1.0),
                        "default": p.get("default", 0.0),
                    })
            live    = fx_live.get(name, {})
            chains.append({
                "name":        name,
                "description": chain_def.get("description", ""),
                "running":     bool(live.get("running", False)),
                "params":      params,
            })
        return Response(json.dumps({"chains": chains}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, mimetype='application/json')

_SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

@app.route('/api/samples')
def api_samples():
    """List wav files in the samples/ directory.
    Returns: {samples: [{name, loaded},...]}
    The synth loads the first 4 alphabetically into smpl_0..smpl_3.
    'loaded' marks those slots so the UI can flag which files are active.
    """
    try:
        os.makedirs(_SAMPLES_DIR, exist_ok=True)
        wavs    = sorted(glob.glob(os.path.join(_SAMPLES_DIR, "*.wav")))
        loaded  = set(os.path.basename(w) for w in wavs[:4])
        samples = [{"name": os.path.basename(w),
                    "loaded": os.path.basename(w) in loaded}
                   for w in wavs]
        return Response(json.dumps({"samples": samples}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, mimetype='application/json')


@app.route('/api/drum_layout')
def api_drum_layout():
    """Data-driven drum CC layout for the /drums page — the single source of
    truth. Returns {base, ch, voices:[{key,name,note,start,params}]} computed in
    orc1_config.DRUM_CC (each voice packed to exactly its params; the HP band and
    the in-drum sample slots were removed 2026-07-21, samples relocated to the
    sampler section). CC for a param = voice.start + params.index(param)."""
    try:
        import orc1_config as _cfg
        dcc    = getattr(_cfg, "DRUM_CC", {})
        layout = dcc.get("LAYOUT", {})
        edit   = dcc.get("EDIT_VOICES", list(layout.keys()))
        base   = int(dcc.get("BASE", 20))
        ch     = int(_cfg.SYNTH.get("CH_DRUM", 10))
        # Primary GM trigger note per voice (the ▶ button / display convenience).
        notes  = {"bass_drum": 36, "snare": 38, "low_tom": 41, "mid_tom": 43,
                  "hi_tom": 50, "rimshot": 37, "clap": 39, "closed_hat": 42,
                  "open_hat": 46, "cymbal": 49, "ride": 51, "ride_bell": 53,
                  "splash": 55, "himid_tom": 48, "cowbell": 56}
        voices = []
        for key in edit:
            d = layout.get(key)
            if not d:
                continue
            voices.append({"key": key, "name": key.replace("_", " ").upper(),
                           "note": notes.get(key, 36),
                           "start": int(d["start"]), "params": list(d["params"])})
        return Response(json.dumps({"base": base, "ch": ch, "voices": voices}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, mimetype='application/json')


@app.route('/api/drum_params')
def api_drum_params():
    """Return the persisted drum parameter CC values so the /drums page can
    initialise sliders to the actual synth state rather than defaulting to 64.
    Reads ~/.orc1_drum_params.json directly (the same file the synth persist
    thread writes); returns {} for a missing/corrupt file."""
    import os, json as _json
    path = os.path.expanduser("~/.orc1_drum_params.json")
    try:
        with open(path) as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return Response(_json.dumps(data), mimetype='application/json')


def _read_params_file(path):
    """Read a persisted synth-param JSON file, returning {} for a
    missing/corrupt/non-dict file. Shared by the poly + chip endpoints below."""
    import os, json as _json
    try:
        with open(os.path.expanduser(path)) as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.route('/api/poly_params')
def api_poly_params():
    """Persisted POLY voice params so the /synth page can initialise sliders to
    the actual synth state after a restart -- when the synth loaded the patch
    from ~/.orc1_poly_params.json (the patch-persistence file) rather than via a
    web CC, synth_cc is empty and the page would otherwise show boot defaults.
    Flat {cc: val}; the /drums equivalent, one voice over. {} if unwritten."""
    import json as _json
    return Response(_json.dumps(_read_params_file("~/.orc1_poly_params.json")),
                    mimetype='application/json')


@app.route('/api/mono_params')
def api_mono_params():
    """Persisted MONO params for /mono, same rationale as /api/poly_params.
    Flat {cc: val} from ~/.orc1_mono_params.json. {} if unwritten (mono keeps
    its code defaults). Level CC13 is absent (owned by the bus-levels system)."""
    import json as _json
    return Response(_json.dumps(_read_params_file("~/.orc1_mono_params.json")),
                    mimetype='application/json')


@app.route('/api/chip_params')
def api_chip_params():
    """Persisted CHIP voice params for /chip, same rationale as /api/poly_params.
    Nested by synth voice name {pulse1|pulse2|triangle|noise|pcm|wave: {cc: val}}
    -- chip.js maps those names to the JS voice tags / channels."""
    import json as _json
    return Response(_json.dumps(_read_params_file("~/.orc1_chip_params.json")),
                    mimetype='application/json')


@app.route('/api/gb_wave')
def api_gb_wave():
    """The GB wave voice's table state for /chip (2026-09-24): 16 slots, the
    custom table, use_custom, preset, the 8 harmonic knob positions. Owned and
    written by the chip host (~/.orc1_gb_wave.json); read-only here. With no
    file yet, "slots" is filled with the voice's own defaults (slots 1-8 = the
    8 presets, read from orc1_synth.py by AST so the page can never drift from
    the engine, and without importing numpy into this process)."""
    import json as _json
    data = _read_params_file("~/.orc1_gb_wave.json")
    if "slots" not in data:
        presets = _gb_wave_presets()
        if presets:
            data["slots"] = presets + [None] * (16 - len(presets))
    return Response(_json.dumps(data), mimetype='application/json')


_GB_PRESETS_CACHE = None


def _gb_wave_presets():
    """orc1_synth._GB_WAVE_PRESETS (list of 32-step tables), parsed once."""
    global _GB_PRESETS_CACHE
    if _GB_PRESETS_CACHE is None:
        import ast, os
        try:
            src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "orc1_synth.py"), encoding="utf-8").read()
            _GB_PRESETS_CACHE = []
            for node in ast.parse(src).body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and getattr(node.targets[0], "id", "") == "_GB_WAVE_PRESETS"):
                    _GB_PRESETS_CACHE = [list(t) for t in ast.literal_eval(node.value)]
                    break
        except Exception:
            _GB_PRESETS_CACHE = []
    return _GB_PRESETS_CACHE


@app.route('/api/oneshot')
def api_oneshot():
    """One-shot sample bank for the /sampler page. MOVED BACK to CH_DRUM
    2026-09-02 (audio host split): the smpl_0..N slots are drum voices again, so
    their trigger notes are on CH_DRUM (ONESHOT_NOTE_BASE+i) and their sculpt CCs
    come from the drum CC map (cfg.DRUM_CC LAYOUT[smpl_i], which packs them after
    cowbell at CC 79+). The UI drives them via the generic SYNTH_NOTE/SYNTH_CC
    cmds on this channel; read-back is state.synth_cc[CH_DRUM]. The synth loads
    the first N wavs alphabetically (cymbal.wav excluded — the drum Cymbal
    voice), same as _load_samples."""
    try:
        import orc1_config as _cfg
        S   = getattr(_cfg, "SAMPLER", {})
        SY  = getattr(_cfg, "SYNTH", {})
        DCC = getattr(_cfg, "DRUM_CC", {})
        layout = DCC.get("LAYOUT", {}) or {}
        # The drums module's LIVE channel (/matrix), not the shipped CH_DRUM;
        # None = drums unbound -> the page sends nothing (2026-10-01).
        ch     = _live_bindings(read_state()).get("drums")
        nslots = int(S.get("ONESHOT_SLOTS", 4))
        nbase  = int(S.get("ONESHOT_NOTE_BASE", 60))
        params = list(S.get("ONESHOT_PARAMS",
                     ["level", "tune", "attack", "decay", "filter_freq", "filter_res"]))
        os.makedirs(_SAMPLES_DIR, exist_ok=True)
        wavs = [w for w in sorted(glob.glob(os.path.join(_SAMPLES_DIR, "*.wav")))
                if os.path.basename(w).lower() != "cymbal.wav"][:nslots]
        slots = []
        for i in range(nslots):
            vlay    = layout.get("smpl_%d" % i, {})
            vparams = list(vlay.get("params", params))
            start   = vlay.get("start")
            # CCs come straight from the packed drum layout; if the voice isn't
            # in the layout (config without the smpl_* voices) the slot reports
            # no CCs rather than inventing colliding numbers.
            ccs = ({p: start + j for j, p in enumerate(vparams)}
                   if start is not None else {})
            name = os.path.basename(wavs[i]) if i < len(wavs) else None
            slots.append({"i": i, "note": nbase + i, "name": name,
                          "loaded": name is not None, "ccs": ccs})
        cc_base = layout.get("smpl_0", {}).get("start")
        return Response(json.dumps({"ch": ch, "note_base": nbase, "cc_base": cc_base,
                                    "params": params, "slots": slots}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, mimetype='application/json')


_FX_IO_PORT_KEYS = ("send_l", "send_r", "return_l", "return_r")

# INTERNAL mode names a MODULE (2026-09-28): send = its outs, return = wherever
# /patch module outputs send it, both resolved live by orc1_config.fx_io_resolve.
# It used to be a fixed ORC1-Synth:out_L/R -> system:playback_1/2, a client the
# audio-host split retired on 09-24, so INTERNAL fed the rack from nothing.

def _fx_io_read():
    import orc1_config as _cfg
    return _cfg.load_fx_io(_mo_phys_ports)

def _jack_ports():
    """Return set of all JACK port names currently registered."""
    try:
        import subprocess as _sp
        env = {"JACK_DEFAULT_SERVER": "default", "PATH": "/usr/bin:/bin:/usr/local/bin"}
        r = _sp.run(["jack_lsp"], capture_output=True, text=True, timeout=3, env=env)
        return set(r.stdout.split())
    except Exception:
        return set()

@app.route('/api/fx/io')
def api_fx_io_get():
    """GET current FX I/O mode + ports + whether each port exists in JACK."""
    io    = _fx_io_read()
    ports = _jack_ports()
    io["available"] = {
        "send_l":   io["send_l"]   in ports,
        "send_r":   io["send_r"]   in ports,
        "return_l": io["return_l"] in ports,
        "return_r": io["return_r"] in ports,
    }
    io["all_ports"] = sorted(ports)
    import orc1_config as _cfg
    io["modules"] = [{"key": k, "label": _cfg.MODULE_LABELS.get(k, k),
                      "client": _cfg.MODULE_JACK_NAMES[k]}
                     for k in _cfg.MODULE_OUT_KEYS if k in _cfg.MODULE_JACK_NAMES]
    return Response(json.dumps(io), mimetype='application/json')

@app.route('/api/fx/io', methods=['POST'])
def api_fx_io_post():
    """POST {preset?, module?, mode?, send_l?, send_r?, return_l?, return_r?}
    → apply + restart chains. Rules: orc1_config.fx_io_apply."""
    try:
        import orc1_config as _cfg
        data = request.get_json(force=True) or {}
        current = _fx_io_read()
        new, err = _cfg.fx_io_apply(current, data)
        if err:
            return Response(json.dumps({"ok": False, "error": err}),
                            status=400, mimetype='application/json')
        new = _cfg.fx_io_resolve(new, _mo_phys_ports)
        payload = {k: new[k] for k in ("mode", "module") + _FX_IO_PORT_KEYS if k in new}
        # Write via engine cmd so it also stops chains + clears cache. The
        # brain writes the file, so "not sent" must not read as "ok".
        if not write_cmd("SET_FX_IO", payload):
            return Response(json.dumps({"ok": False, "error": "brain not running"}),
                            status=503, mimetype='application/json')
        return Response(json.dumps({"ok": True, **new}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# FX RACK — 3-slot series stack (new-style FX)
# ══════════════════════════════════════════════════════════════════════════════
# The rack itself (jalv processes) is owned by the orc1 brain.  This web process
# only READS state (desired config + palette + the brain's status file) and
# sends mutations over the cmd socket via RACK_* actions.  We instantiate a
# RackManager here purely for its file-reading helpers — it never spawns jalv in
# the web process (apply() is never called here).
try:
    from orc1_fx_rack import RackManager as _RackManager, RACK_STATUS_FILE as _RACK_STATUS_FILE
    _rack_ro = _RackManager()          # read-only view for the web process
except Exception:
    _rack_ro = None
    _RACK_STATUS_FILE = os.path.expanduser("~/.orc1_fx_rack_status.json")

@app.route('/api/fx/rack')
def api_fx_rack():
    """Return the rack view: 3 slots with plugin/enabled/controls/values, plus
    the live running flag per slot (overlaid from the brain's status file)."""
    if not _rack_ro:
        return Response(json.dumps({"cap": 3, "slots": []}),
                        mimetype='application/json')
    view = _rack_ro.status()           # desired + palette + values (running=False)
    # Overlay the real running set written by the brain on its last rebuild.
    try:
        with open(_RACK_STATUS_FILE) as f:
            st = json.load(f)
        run = {s["idx"]: s.get("running", False) for s in st.get("slots", [])}
        for s in view["slots"]:
            s["running"] = run.get(s["idx"], False)
    except Exception:
        pass
    return Response(json.dumps(view), mimetype='application/json')

@app.route('/api/fx/palette')
def api_fx_palette():
    """Return the installed-plugin palette as a sorted [{key,label}] picker list,
    plus `favorites` (the curated keeper keys) and `categories` ({cat: [keys]}) so
    the picker can group -- ★ Favourites first, then by category -- instead of a
    flat 292-item list."""
    items = _rack_ro.palette_list() if _rack_ro else []
    favs  = _rack_ro.favorites() if _rack_ro else []
    try:
        cats = _rack_ro.categories() if _rack_ro else {}
    except Exception:
        cats = {}
    return Response(json.dumps({"plugins": items, "favorites": favs,
                               "categories": cats}), mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM HEALTH — proxy to orc1-system.service at :5002
# ══════════════════════════════════════════════════════════════════════════════
# The system dashboard runs as a separate Flask app on :5002 so it survives
# an engine crash. This proxy lets the main UI fetch stats same-origin
# (no CORS headaches) and fills in the /system nav tab.
# GET  /api/system/stats  → proxied from localhost:5002/stats
# GET  /system            → the system health page (extends base.html)

_SYSTEM_URL = "http://localhost:5002/stats"

@app.route('/system')
def system_page():
    return render_template('system.html')

@app.route('/api/system/stats')
def api_system_stats():
    try:
        import urllib.request
        with urllib.request.urlopen(_SYSTEM_URL, timeout=2) as r:
            data = r.read()
        return Response(data, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": "system service unavailable", "detail": str(e)}),
            status=503, mimetype='application/json')


# ─── UNIVERSAL PAD API ───────────────────────────────────────────────────────
# GET  /api/universal_pads  → {pads: {"r,c": {type,number,value,channel,port,latch,...}}}
# POST /api/universal_pads  {row,col,...assignment}  → UNIVERSAL_SET_PAD cmd
# POST /api/universal_pads/clear {row,col} or {all:true}  → UNIVERSAL_CLEAR_PAD cmd
_FIRE_STATE_FILE = os.path.expanduser('~/orc1_state.json')

@app.route('/api/universal_pads')
def api_universal_pads_get():
    """Pads for one bank. ?bank=N (default 0, the bank this page has always
    edited). Returns `banks` too so the UI knows how many exist without a
    second round trip."""
    # Bank COUNT comes from config, not from what is already on disk. Reading
    # it off the state file meant that on a box where the Fire had never
    # connected -- a supported case; the pad editor works with no Fire present
    # -- only one tab ever rendered, so banks 2..N could never be selected in
    # order to create them. Chicken and egg with no way in.
    nbanks = 4
    try:
        import orc1_config as _c
        nbanks = max(1, min(9, int(_c.FIRE.get('UNIVERSAL_BANKS', 4))))
    except Exception:
        pass
    pads = {}
    try:
        bank = int(request.args.get('bank', 0))
    except (TypeError, ValueError):
        bank = 0
    try:
        with open(_FIRE_STATE_FILE) as fh:
            d = json.load(fh)
        raw = d.get('fire_universal_banks')
        if isinstance(raw, dict) and raw:
            # A box configured for MORE banks than config says (a state file
            # from elsewhere) still gets its tabs, so nothing becomes hidden.
            nbanks = max(nbanks, max((int(k) for k in raw if str(k).isdigit()),
                                     default=0) + 1)
            pads = raw.get(str(bank), {})
        else:
            # Pre-bank state file: one grid, and it is bank 0.
            pads = d.get('fire_universal_pads', {}) if bank == 0 else {}
    except Exception:
        pass
    if not isinstance(pads, dict):
        pads = {}
    return Response(json.dumps({'pads': pads, 'bank': bank, 'banks': nbanks}),
                    mimetype='application/json')


@app.route('/api/universal_pads', methods=['POST'])
def api_universal_pads_set():
    try:
        data = request.get_json(force=True)
        write_cmd('UNIVERSAL_SET_PAD', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


@app.route('/api/universal_pads/clear', methods=['POST'])
def api_universal_pads_clear():
    try:
        data = request.get_json(force=True)
        write_cmd('UNIVERSAL_CLEAR_PAD', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


# ─── CONTROL PAGE ────────────────────────────────────────────────────────────

@app.route('/control')
def control_page():
    return render_template('control.html')


# GET  /api/knobmap
#   Returns {views, layers, targets, map}
#   map = {view: {layer: {knob_idx: [action_tuple]}}}
#   targets = [{label, action}]  from FIRE["KNOB_TARGETS"]
# POST /api/knobmap  {view, layer, knob, target:[...]}  → send KNOBMAP_SET cmd
_KNOBMAP_FILE  = os.path.expanduser('~/fire_knobmap.json')
_CHORD_ROWS_FILE = os.path.expanduser('~/.orc1_chord_rows.json')

@app.route('/api/knobmap')
def api_knobmap_get():
    import orc1_config as _cfg
    from orc1_fire import VIEWS as FIRE_VIEWS
    targets = [{'label': lbl, 'action': list(act)}
               for lbl, act in _cfg.FIRE.get('KNOB_TARGETS', [])]
    layers  = ['base', 'shift', 'alt']
    views   = list(FIRE_VIEWS)
    kmap    = {}
    try:
        with open(_KNOBMAP_FILE) as fh:
            kmap = json.load(fh)
    except Exception:
        pass
    # modules: a synth-bus send may NAME a module instead of a channel
    # (resolved live from the /matrix bindings, 2026-09-24).
    return Response(json.dumps({
        'views': views, 'layers': layers,
        'targets': targets, 'map': kmap,
        'modules': list(getattr(_cfg, 'MODULE_KEYS', []))
    }), mimetype='application/json')


@app.route('/api/knobmap', methods=['POST'])
def api_knobmap_set():
    try:
        data = request.get_json(force=True)
        write_cmd('KNOBMAP_SET', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


# GET  /api/universal_knobs?bank=N  → {knobs:{layer:{idx:[action]}}, bank, banks}
#   Per-PAGE (per-bank) knob overrides for the universal surface (Fire reorg P2).
#   Reads the live state file; the target list is the same as /api/knobmap.
# POST /api/universal_knobs  {bank, layer, knob, knob_action}  → UNIVERSAL_SET_KNOB
#   knob_action = an action list (e.g. ["cc_out","synth",8,74]) or null to clear.
@app.route('/api/universal_knobs')
def api_universal_knobs_get():
    nbanks = 4
    try:
        import orc1_config as _c
        nbanks = max(1, min(9, int(_c.FIRE.get('UNIVERSAL_BANKS', 4))))
    except Exception:
        pass
    try:
        bank = int(request.args.get('bank', 0))
    except (TypeError, ValueError):
        bank = 0
    knobs = {}
    try:
        with open(_FIRE_STATE_FILE) as fh:
            d = json.load(fh)
        allk = d.get('fire_universal_knobs')
        if isinstance(allk, dict) and allk:
            nbanks = max(nbanks, max((int(k) for k in allk if str(k).isdigit()),
                                     default=0) + 1)
            knobs = allk.get(str(bank), {})
    except Exception:
        pass
    if not isinstance(knobs, dict):
        knobs = {}
    return Response(json.dumps({'knobs': knobs, 'bank': bank, 'banks': nbanks}),
                    mimetype='application/json')


@app.route('/api/universal_knobs', methods=['POST'])
def api_universal_knobs_set():
    try:
        data = request.get_json(force=True)
        write_cmd('UNIVERSAL_SET_KNOB', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


# GET  /api/universal_faders?bank=N  → {faders:{row:{cc,channel,port,value}}, bank, banks}
#   Per-PAGE fader ROWS on the universal surface (Fire reorg P3).
# POST /api/universal_faders  {bank, row, cc, channel, port}  → UNIVERSAL_SET_FADER
#   or {bank, row, clear:true} to remove the fader on that row.
@app.route('/api/universal_faders')
def api_universal_faders_get():
    nbanks = 4
    try:
        import orc1_config as _c
        nbanks = max(1, min(9, int(_c.FIRE.get('UNIVERSAL_BANKS', 4))))
    except Exception:
        pass
    try:
        bank = int(request.args.get('bank', 0))
    except (TypeError, ValueError):
        bank = 0
    faders = {}
    try:
        with open(_FIRE_STATE_FILE) as fh:
            d = json.load(fh)
        allf = d.get('fire_universal_faders')
        if isinstance(allf, dict) and allf:
            nbanks = max(nbanks, max((int(k) for k in allf if str(k).isdigit()),
                                     default=0) + 1)
            faders = allf.get(str(bank), {})
    except Exception:
        pass
    if not isinstance(faders, dict):
        faders = {}
    return Response(json.dumps({'faders': faders, 'bank': bank, 'banks': nbanks}),
                    mimetype='application/json')


@app.route('/api/universal_faders', methods=['POST'])
def api_universal_faders_set():
    try:
        data = request.get_json(force=True)
        write_cmd('UNIVERSAL_SET_FADER', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


# GET  /api/chord_rows  → current row config (file, else config default)
# POST /api/chord_rows  {rows:[...]}  → send CHORD_ROWS_SET cmd
@app.route('/api/chord_rows')
def api_chord_rows_get():
    """The ACTIVE bank's rows (the file is banked since v2: {v, active,
    banks:[[row...]...]}; this returned the whole dict and the /control card
    broke on it -- "rows.map is not a function", 2026-09-24)."""
    import orc1_config as _cfg
    rows, bank, nbanks = list(_cfg.FIRE.get('CHORD_ROW_MAP', [])), 0, 1
    try:
        with open(_CHORD_ROWS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get('banks'), list) and data['banks']:
            nbanks = len(data['banks'])
            bank = max(0, min(nbanks - 1, int(data.get('active', 0) or 0)))
            rows = data['banks'][bank]
        elif isinstance(data, list):
            rows = data
    except Exception:
        pass
    ports = list(_cfg.PORTS.keys()) + ['synth']
    return Response(json.dumps({'rows': rows, 'bank': bank, 'banks': nbanks,
                                'ports': ports,
                                'modules': list(getattr(_cfg, 'MODULE_KEYS', []))}),
                    mimetype='application/json')


@app.route('/api/chord_rows', methods=['POST'])
def api_chord_rows_set():
    try:
        data = request.get_json(force=True)
        write_cmd('CHORD_ROWS_SET', data)
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}),
                        status=500, mimetype='application/json')


# ─── MIDI PORTS API ──────────────────────────────────────────────────────────
# GET  /api/midi_ports         → {ports:[{key,label,static},...]}
#   static=True  = defined in orc1_config.PORTS (always available after startup)
#   static=False = opened on-demand by MIDI_SCAN (disappears on engine restart)
# POST /api/midi_ports/scan    → send MIDI_SCAN cmd, wait, return updated list

def _build_port_list(state=None):
    """Combined list of configured + dynamically-scanned MIDI output ports."""
    try:
        import orc1_config as _cfg
        ports = [{"key": k, "label": f"{k}  ({v})", "static": True}
                 for k, v in _cfg.PORTS.items()]
    except Exception:
        ports = []
    ports.append({"key": "synth", "label": "synth  (internal bus)", "static": True})
    if state is None:
        state = read_state() or {}
    for k, v in (state.get("dynamic_ports") or {}).items():
        ports.append({"key": k, "label": f"{k}  — {v}", "static": False})
    return ports

@app.route('/api/midi_ports')
def api_midi_ports_get():
    return Response(json.dumps({"ports": _build_port_list()}),
                    mimetype='application/json')

@app.route('/api/midi_ports/scan', methods=['POST'])
def api_midi_ports_scan():
    write_cmd('MIDI_SCAN')
    time.sleep(0.8)   # let engine open ports + atomically write state
    st = read_state() or {}
    return Response(json.dumps({"ok": True, "ports": _build_port_list(st)}),
                    mimetype='application/json')


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG OVERRIDES API  (Phase 9, 2026-07-13)
# ══════════════════════════════════════════════════════════════════════════════
# GET    /api/config          → schema list with current values + override flags
# POST   /api/config          {key, value} → validate + persist to overrides file
# DELETE /api/config/<key>    → remove key from overrides (reset to config default)
#
# Only schema-listed keys are editable.  The overrides file is
# ~/.orc1_config_overrides.json — orc1_config.py applies it at import time.
# Restarting the appropriate service picks up any change (restart hint is returned).

_CONFIG_OVERRIDES_FILE = os.path.expanduser("~/.orc1_config_overrides.json")

# Schema rows: (key, type, min, max, choices, description, restart_svc, default)
# type: "float" | "int" | "bool" | "choice"
# restart_svc: "none" | "orc1" | "synth" | "web" | "skipback"
_CONFIG_SCHEMA = [
    # ── SYNTH ──
    ("SYNTH.MASTER_LEVEL",         "float",  0.0,  1.5,  None,
     "Master bus BOOT level (0=silent, 0.6=nominal, 1.5=hot). Only applies "
     "while the master fader has never been moved — once it has, the saved "
     "level in ~/.orc1_bus_levels.json wins and this has no effect.",
     "synth", 0.60),
    ("SYNTH.SUB_HPF_HZ",           "float",  10.0, 200.0, None,
     "Sub oscillator HPF cutoff Hz — filters infrasonic content", "synth", 40.0),
    ("SYNTH.SEQ_RECORD_CH",        "int",    0,    16,   None,
     "MIDI channel for live sequencer recording (0 = disabled)", "orc1", 0),
    ("SYNTH.CHIP_BEND_RANGE",      "int",    1,    24,   None,
     "Chip pitchwheel bend range in semitones (±) — pulse 1/2 + GB wave", "synth", 12),
    # ── SYNTH VOICE CHANNELS ──
    # Token is "orc1", NOT "synth" (corrected 2026-07-29). The BRAIN reads
    # every one of these too -- orc1.py:3772-3774 builds _SYNTH_CHS (its
    # routing set) from the whole CH_* group, and :466 reads CH_SAMPLER.
    # Restarting only the synth left the brain still encoding on the OLD
    # channel, so the voice you just remapped went SILENT. "orc1" is the
    # correct token because orc1-synth is PartOf=orc1.service, so an orc1
    # restart cascades a synth restart -- one token covers both readers.
    # Avoid 10 (always-passthrough), 12 (chord in), 13 (ctrl), 14 (passthrough), 15 (melody).
    ("SYNTH.CH_DRUM",     "int", 1, 16, None,
     "Drum voice MIDI-in channel (default 10 = GM drum channel)", "orc1", 10),
    ("SYNTH.CH_POLY",     "int", 1, 16, None,
     "Poly synth MIDI-in channel (default 8; CH 7 legacy alias also works)", "orc1", 8),
    ("SYNTH.CH_CHIP",     "int", 1, 16, None,
     "NES Pulse 1 MIDI-in channel (default 9)", "orc1", 9),
    ("SYNTH.CH_CHIP_P2",  "int", 1, 16, None,
     "NES Pulse 2 MIDI-in channel (default 6)", "orc1", 6),
    ("SYNTH.CH_CHIP_TRI", "int", 1, 16, None,
     "NES Triangle MIDI-in channel (default 11)", "orc1", 11),
    ("SYNTH.CH_CHIP_NS",  "int", 1, 16, None,
     "NES Noise MIDI-in channel (default 4)", "orc1", 4),
    ("SYNTH.CH_GB_WAVE",  "int", 1, 16, None,
     "Game Boy Wave MIDI-in channel (default 5)", "orc1", 5),
    ("SYNTH.CH_CHIP_PCM", "int", 1, 16, None,
     "NES PCM MIDI-in channel (default 16)", "orc1", 16),
    ("SAMPLER.CH_SAMPLER","int", 1, 16, None,
     "Sampler MIDI-in channel (default 7)", "orc1", 7),
    # ── SAMPLER ──
    # Tokens corrected 2026-07-29: these were ALL "none" (= no restart
    # needed), but every one is read ONCE at process start -- either into
    # SamplerManager.__init__ (orc1_sampler.py, lives in the synth) or into a
    # module-level _SMP_* constant in orc1.py (the brain). "none" meant the
    # card let you change a value and told you it had taken effect while the
    # running engine kept using the old one. Dual readers get "orc1", which
    # cascades the synth restart via PartOf=.
    ("SAMPLER.DEFAULT_CAPTURE_SECONDS", "int", 1,  60,   None,
     "Default grab duration (seconds) in non-bar mode", "orc1", 8),
    ("SAMPLER.MAX_CAPTURE_SECONDS",     "int", 5,  300,  None,
     "Maximum capture length cap", "synth", 60),
    # Range was 0..500 with default 42 while the real config value is -99 --
    # the calibrated value was UNREACHABLE from this card, and anything you
    # could reach shifted every capture window by 100-600ms. Negative values
    # are meaningful here (the ring lags heard audio), so the range must span
    # them.
    ("SAMPLER.CAPTURE_LATENCY_MS",      "int", -500, 500, None,
     "Ring lag vs heard audio in ms (negative = ring lags) — re-measure if the capture source or JACK period changes",
     "orc1", -99),
    ("SAMPLER.SEAM_FADE_MS",            "int", 0,  50,   None,
     "Loop-seam declick crossfade length (ms)", "synth", 15),
    ("SAMPLER.QUANTIZE_SNAP",        "float", 0.0, 0.5,  None,
     "Bar-quantize snap: pressed within this fraction of a bar before the next barline waits for it",
     "orc1", 0.15),
    ("SAMPLER.QUANTIZE_MARGIN_MS",   "int",  100, 2000,  None,
     "Min ms after barline before quantized grab fires", "orc1", 350),
    ("SAMPLER.BEND_FACTOR",          "float", 1.0, 1.25, None,
     "Beatmatch pitch-bend factor (1.04 = ±4%)", "synth", 1.04),
    ("SAMPLER.SPEED_RANGE_OCTAVES",  "float", 0.05, 3.0, None,
     "Pitch fader range — center=1×, ends=±N octaves (0.12 ≈ ±8%)", "synth", 1.0),
    ("SAMPLER.CAPTURE_TARGET_PEAK",  "float", 0.3, 1.0,  None,
     "Normalize each capture to this peak (0.9 = –0.9 dBFS)", "synth", 0.9),
    # ── AUTOSAMPLER ──
    ("AUTOSAMPLER.HOLD_S",      "float", 0.5, 10.0, None,
     "Note hold duration per pitch during a sweep", "none", 2.0),
    ("AUTOSAMPLER.TAIL_S",      "float", 0.5, 5.0,  None,
     "Record time after note-off — captures release/reverb tail", "none", 1.5),
    ("AUTOSAMPLER.GAP_S",       "float", 0.0, 2.0,  None,
     "Silence between sweep notes", "none", 0.3),
    ("AUTOSAMPLER.NOTE_STEP",   "int",   1,   12,   None,
     "Semitones between sweep notes (3 = every minor 3rd)", "none", 3),
    ("AUTOSAMPLER.TARGET_PEAK", "float", 0.5, 1.0,  None,
     "Per-kit normalization target peak", "none", 0.9),
    ("AUTOSAMPLER.STEREO",      "bool",  None, None, None,
     "Autosampler sweep: record stereo WAVs (doubles MPC RAM usage). Skipback ring is always stereo regardless of this setting.", "none", False),
    # ── ARP ──
    ("ARP_DEFAULT_BPM",   "float", 40.0, 280.0, None,
     "Default BPM on startup", "orc1", 120.0),
    ("ARP_DEFAULT_GATE",  "float", 0.0,  2.0,   None,
     "Default arp gate (1.0=full, 0.5=staccato, 2.0=legato)", "orc1", 1.0),
    ("ARP_DEFAULT_SWING", "float", 0.0,  0.5,   None,
     "Default arp swing amount", "orc1", 0.0),
]

def _cfg_schema_map():
    return {s[0]: {"type": s[1], "min": s[2], "max": s[3], "choices": s[4],
                   "desc": s[5], "restart": s[6], "default": s[7]}
            for s in _CONFIG_SCHEMA}

def _cfg_overrides_read():
    try:
        with open(_CONFIG_OVERRIDES_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _cfg_overrides_write(d):
    tmp = _CONFIG_OVERRIDES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, _CONFIG_OVERRIDES_FILE)

def _cfg_current_value(key):
    """Read live value from the already-imported (overrides applied) config."""
    try:
        import orc1_config as _cfg
        parts = key.split(".")
        obj = getattr(_cfg, parts[0], None)
        for p in parts[1:]:
            obj = obj[p]
        return obj
    except Exception:
        return None

def _cfg_validate(value, schema_entry):
    t = schema_entry["type"]
    try:
        if t == "float":
            v = float(value)
            mn, mx = schema_entry["min"], schema_entry["max"]
            if mn is not None and v < mn: return None, f"must be ≥ {mn}"
            if mx is not None and v > mx: return None, f"must be ≤ {mx}"
            return v, None
        elif t == "int":
            v = int(value)
            mn, mx = schema_entry["min"], schema_entry["max"]
            if mn is not None and v < mn: return None, f"must be ≥ {mn}"
            if mx is not None and v > mx: return None, f"must be ≤ {mx}"
            return v, None
        elif t == "bool":
            if isinstance(value, bool): return value, None
            if str(value).lower() in ("true", "1", "yes"): return True, None
            if str(value).lower() in ("false", "0", "no"): return False, None
            return None, "must be true or false"
        elif t == "choice":
            choices = schema_entry["choices"] or []
            if value not in choices: return None, f"must be one of {choices}"
            return value, None
        elif t == "str":
            # Added 2026-07-30. "str" rows have been in the schema for a long
            # time (JACK_PAIR2_BUSES, later MAIN_MIX_BUSES -- both since removed) but there was no
            # branch for them here, so every save returned "unknown type" --
            # i.e. those keys were never actually editable from the page that
            # advertised them. Trimmed and length-capped; the consuming code
            # parses/validates its own contents.
            v = str(value).strip()
            if len(v) > 200: return None, "too long (max 200 chars)"
            return v, None
    except Exception as e:
        return None, str(e)
    return None, "unknown type"

@app.route('/api/config')
def api_config_get():
    schema = _cfg_schema_map()
    overrides = _cfg_overrides_read()
    result = []
    for s in _CONFIG_SCHEMA:
        key = s[0]
        entry = dict(schema[key])
        entry["key"] = key
        entry["current"] = _cfg_current_value(key)
        entry["overridden"] = key in overrides
        result.append(entry)
    return Response(json.dumps({"schema": result, "overrides": overrides}),
                    mimetype='application/json')

@app.route('/api/config', methods=['POST'])
def api_config_set():
    try:
        data  = request.get_json(force=True) or {}
        key   = str(data.get("key", "")).strip()
        schema = _cfg_schema_map()
        if key not in schema:
            return Response(json.dumps({"ok": False, "error": "unknown key"}),
                            status=400, mimetype='application/json')
        value, err = _cfg_validate(data.get("value"), schema[key])
        if err:
            return Response(json.dumps({"ok": False, "error": err}),
                            status=400, mimetype='application/json')
        ovr = _cfg_overrides_read()
        ovr[key] = value
        _cfg_overrides_write(ovr)
        return Response(json.dumps({"ok": True, "restart": schema[key]["restart"]}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

@app.route('/api/config/<path:key>', methods=['DELETE'])
def api_config_reset(key):
    try:
        schema = _cfg_schema_map()
        if key not in schema:
            return Response(json.dumps({"ok": False, "error": "unknown key"}),
                            status=400, mimetype='application/json')
        ovr = _cfg_overrides_read()
        if key in ovr:
            del ovr[key]
            _cfg_overrides_write(ovr)
        return Response(json.dumps({"ok": True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

# ── Service restart shortcut ──────────────────────────────────────────────────
# POST /api/restart/<service>  → sudo systemctl restart <mapped service>
# Used by the config card restart chips. Restarting "web" kills the current
# connection — the browser will reconnect on reload.
#
# SKIPBACK COMES FIRST (2026-10-01): "synth" and "orc1" never go through ONE
# `systemctl restart`. Naming all six hosts in one call (or restarting orc1,
# which they are PartOf) STOPS them at the same instant -- 70-stagger only
# spaces out starts -- and that pile-up was measured to xrun skipback (~40 ms
# gap). They run the one-at-a-time scripts instead (_RESTART_SCRIPTS). While
# DAW/DJ engines-off mode has the hosts stopped, a "synth" restart is refused
# (the page says why) and the gentle restart leaves them stopped.
_HOSTS_SH = os.path.expanduser("~/orc/orc1/orc1_hosts.sh")
_GENTLE_SH = os.path.expanduser("~/orc/orc1/orc1_gentle_restart.sh")
_DAW_OFF_MARK = "/run/orc1-daw-engines-off"      # daw/daw_mode.sh
_RESTART_SCRIPTS = {
    "synth": [_HOSTS_SH, "restart"],
    "orc1":  [_GENTLE_SH],
}
_RESTART_MAP = {
    "orc1":     "orc1",
    # The JACK owner (2026-07-14). Restarting it is the ONLY way to clear a
    # wedged jackd or rebind the soundcard; orc1-synth no longer does either.
    # BindsTo=orc1-jackd cascades a synth restart automatically.
    "jackd":    "orc1-jackd",
    # "synth" = the six per-module audio hosts that replaced the retired
    # orc1-synth (2026-09-24: this still named orc1-synth, so every /system
    # setting marked "restart synth to apply" -- e.g. CHIP_BEND_RANGE -- was a
    # silent no-op). Restarted through _RESTART_SCRIPTS (one at a time).
    "synth":    ["orc1-drum", "orc1-sampler", "orc1-poly", "orc1-chip",
                 "orc1-mono", "orc1-bbkit"],
    "web":      "orc1-web",
    "skipback": "skipback-record",
}

@app.route('/api/restart/<service>', methods=['POST'])
def api_restart(service):
    if service not in _RESTART_MAP:
        return Response(json.dumps({"ok": False, "error": "unknown service"}),
                        status=400, mimetype='application/json')
    try:
        import subprocess
        units = _RESTART_MAP[service]
        units = units if isinstance(units, list) else [units]
        if service == "synth" and os.path.exists(_DAW_OFF_MARK):
            return Response(json.dumps({
                "ok": False, "service": units, "engines_off": True,
                "error": "engines are OFF for DAW/DJ mode -- they come back "
                         "(with the new settings) when it ends"}),
                mimetype='application/json')
        if service in _RESTART_SCRIPTS:
            # background + own session: the sequence takes ~5-15 s
            subprocess.Popen(["sudo"] + _RESTART_SCRIPTS[service],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return Response(json.dumps({"ok": True, "service": units,
                                        "sequenced": True}),
                            mimetype='application/json')
        subprocess.Popen(["sudo", "systemctl", "restart"] + units)
        return Response(json.dumps({"ok": True, "service": units}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')



# ==============================================================================
# SKIPBACK - standalone recorder page + config API
# ==============================================================================
# /skipback is skipback's OWN page. Skipback is a standalone stereo ring
# recorder; the sampler is just one read-only consumer of the ring.
#   mode "auto"   -> follow the rig: Zoom L12 master when the L12 is the active
#                    card (system:capture_13/14 present), else pisound in, else
#                    synth direct.
#   mode "manual" -> an explicit JACK source pair (incl. any Zoom track pair).
# The RECORDER (skipback_recorder.py) is the authority and re-resolves every
# ~5s via _refresh_src_override; these endpoints also live-rewire for instant
# feedback and persist the choice to ~/.orc1_skipback_src.json.
# (Was "the bridge (skipback_bridge.sh)" -- retired in the 2026-07-19 cutover.)

@app.route('/skipback')
def skipback_page():
    return render_template('skipback.html')

def _zoom_present():
    s = set(_jack_sources())
    return "system:capture_13" in s and "system:capture_14" in s

def _resolve_auto(srcs=None):
    """(left, right, label) for auto mode given available JACK sources."""
    s = set(srcs if srcs is not None else _jack_sources())
    if "system:capture_13" in s and "system:capture_14" in s:
        return ("system:capture_13", "system:capture_14", "ZOOM MASTER")
    if "pisound-in:capture_1" in s and "pisound-in:capture_2" in s:
        return ("pisound-in:capture_1", "pisound-in:capture_2", "PISOUND IN")
    # Same last resort as the recorder's AUTO_PAIRS (the retired ORC1-Synth
    # "synth direct" pair was dropped there 2026-09-15; the label followed
    # 2026-09-28).
    return ("system:capture_1", "system:capture_2", "CARD INPUT 1-2")

def _labeled_sources(srcs=None):
    """Friendly named source pairs that currently exist in the JACK graph."""
    s = set(srcs if srcs is not None else _jack_sources())
    out = []
    def add(label, l, r):
        if l in s and r in s:
            out.append({"label": label, "left": l, "right": r})
    add("Zoom Master", "system:capture_13", "system:capture_14")
    for i in range(1, 13, 2):
        add("Zoom Track %d-%d" % (i, i + 1),
            "system:capture_%d" % i, "system:capture_%d" % (i + 1))
    add("Pisound In", "pisound-in:capture_1", "pisound-in:capture_2")
    # Each ORC module's own output (2026-09-28; replaces the retired single
    # "Synth Direct" ORC1-Synth pair). Only listed when the module is up.
    try:
        import orc1_config as _c
        mods = list(_c.MODULE_JACK_NAMES.items())
        labels = getattr(_c, "MODULE_LABELS", {})
    except Exception:
        mods, labels = [], {}
    for key, client in mods:
        add("Module: %s" % labels.get(key, key), "%s:out_L" % client,
            "%s:out_R" % client)
    return out

def _skipback_mode():
    try:
        with open(SKIPBACK_SRC) as f:
            d = json.load(f)
        if d.get("mode"):
            return d.get("mode"), d.get("left", ""), d.get("right", "")
        if d.get("left") and d.get("right"):
            return "manual", d["left"], d["right"]
    except Exception:
        pass
    return "auto", "", ""

@app.route('/api/skipback/config')
def api_skipback_config_get():
    srcs = _jack_sources()
    mode, mleft, mright = _skipback_mode()
    ports = _sb_track0_ports()
    cur = _skipback_current(ports)
    conn_l = cur[ports[0]][0] if cur[ports[0]] else ""
    # Mono track 0 registers a single input port -- no right channel to report.
    conn_r = (cur[ports[1]][0] if len(ports) > 1 and cur[ports[1]] else "")
    if mode == "manual" and mleft and mright:
        want_l, want_r = mleft, mright
        label = mleft
        for ls in _labeled_sources(srcs):
            if ls["left"] == mleft and ls["right"] == mright:
                label = ls["label"]
                break
    else:
        want_l, want_r, label = _resolve_auto(srcs)
    return Response(json.dumps({
        "mode":         mode,
        "zoom_present": _zoom_present(),
        "resolved":     {"left": want_l, "right": want_r, "label": label},
        "connected":    {"left": conn_l, "right": conn_r},
        "sources":      _labeled_sources(srcs),
        "raw_sources":  srcs,
    }), mimetype='application/json')

@app.route('/api/skipback/config', methods=['POST'])
def api_skipback_config_post():
    try:
        import subprocess
        data = request.get_json(force=True) or {}
        mode = str(data.get("mode", "auto")).strip()
        srcs = set(_jack_sources())
        if mode == "manual":
            left  = str(data.get("left", "")).strip()
            right = str(data.get("right", "")).strip()
            if not left or not right:
                return Response('{"ok":false,"error":"manual mode needs left+right"}',
                                status=400, mimetype='application/json')
            payload = {"mode": "manual", "left": left, "right": right}
            want_l, want_r = left, right
        else:
            payload = {"mode": "auto"}
            want_l, want_r, _ = _resolve_auto(srcs)
        tmp = SKIPBACK_SRC + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, SKIPBACK_SRC)
        ports = _sb_track0_ports()
        # Mono track 0 only consumes want_l, so don't gate the rewire on want_r.
        if want_l in srcs and (len(ports) == 1 or want_r in srcs):
            cur = _skipback_current(ports)
            for sb_port, new_src in zip(ports, (want_l, want_r)):
                for old in cur[sb_port]:
                    if old != new_src:
                        subprocess.run(["jack_disconnect", old, sb_port],
                                       capture_output=True, timeout=5)
                subprocess.run(["jack_connect", new_src, sb_port],
                               capture_output=True, timeout=5)
        return Response(json.dumps({"ok": True, "mode": mode,
                                    "left": want_l, "right": want_r}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')

@app.route('/api/skipback/grab', methods=['POST'])
def api_skipback_grab():
    try:
        import subprocess
        data = request.get_json(force=True) or {}
        secs = int(data.get("seconds", 60))
        secs = max(1, min(1860, secs))
        script = os.path.expanduser("~/orc/orc1/skipback/skipback.sh")
        r = _sb_run_grab(["bash", script, str(secs)], 180)
        if r is None:
            return _sb_pending()
        rc, out, err = r
        out = (out or "").strip()
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        if rc != 0 or not saved:
            return Response(json.dumps({"ok": False,
                            "error": (err or out or "grab failed").strip()}),
                            status=500, mimetype='application/json')
        return Response(json.dumps({"ok": True, "file": os.path.basename(saved),
                                    "seconds": secs}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ==============================================================================
# SKIPBACK — multitrack tracks API (Phase 2)
# ==============================================================================
# The recorder (skipback_recorder.py, JACK client 'skipback-rec') owns ALL
# recording. These endpoints are just the web control surface over its two
# files (spec: _planning/ORC1_Multitrack_Skipback_Spec.md, contracts C2/C3/C4/C6):
#   ~/.orc1_skipback_tracks.json  — config: WE write it (atomically); the
#                                   recorder reads + reconciles it within ~5s.
#   ~/.orc1_skipback_status.json  — live status: the recorder writes it, WE
#                                   only read it (same pattern as bus status).
# The legacy /api/skipback/{config,source,grab} endpoints above stay as track-0
# shims (C5) — main.js's PLAY card + sampler.js still call them.

SKIPBACK_TRACKS  = os.path.expanduser("~/.orc1_skipback_tracks.json")
SKIPBACK_STATUS  = os.path.expanduser("~/.orc1_skipback_status.json")
SKIPBUF_ROOT     = os.path.expanduser("~/.skipbuf")
SKIPBACK_SH      = os.path.expanduser("~/orc/orc1/skipback/skipback.sh")


def _sb_press_env():
    """Env for a grab subprocess carrying the moment the request arrived
    (SKIPBACK_PRESS_TS). The grab ends at the press even if the recorder's
    card writer is stalled; stamping it here, not when bash gets going, keeps
    a slow spawn under load from moving the cut (2026-09-29)."""
    return dict(os.environ, SKIPBACK_PRESS_TS=repr(time.time()))


_SB_GRAB_ALL_LOCK = threading.Lock()     # one web GRAB ALL at a time


def _sb_run_grab(cmd, timeout, release=None):
    """Run a grab script. Returns (returncode, stdout, stderr), or None when
    it is still writing after `timeout` s. Review 2026-10-01: this used to be
    subprocess.run(timeout=...), which KILLS the script -- its cleanup never
    ran, so a slow (big CARD-ring) grab left a hidden .part of up to GBs in
    ~/recordings. Now a slow grab is left to finish in the background (it
    renames itself into place when done); `release` (a held Lock) is released
    only when the script really exits."""
    import subprocess
    p = subprocess.Popen(cmd, env=_sb_press_env(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        def reap():
            try:
                p.communicate()
            finally:
                if release is not None:
                    release.release()
        threading.Thread(target=reap, daemon=True).start()
        return None
    if release is not None:
        release.release()
    return p.returncode, out, err


def _sb_pending():
    return Response(json.dumps({
        "ok": False, "pending": True,
        "error": "still writing (a big grab); it finishes in the background "
                 "and appears in ~/recordings when done"}),
        status=202, mimetype='application/json')
SB_RATE          = 48000          # rig is pinned at 48 kHz
SB_RAM_BUDGET_MB = 900            # C4: armed-ring sum must stay <= this
SB_RAM_FREE_MIN  = 400            # C4: projected free RAM (MB) must stay >= this
SB_MAX_TRACKS    = 16             # sanity cap (ids run 0-63; UI never needs 16)
SB_CARD_ROOT     = "/mnt/skipring"  # store:"card" rings (= recorder DISK_ROOT)
SB_CARD_RESERVE  = 64 * 1024 * 1024 # = recorder DISK_RESERVE

def _sb_card_info():
    """(mounted, capacity_bytes) of the card ring partition -- same maths as
    the recorder's _disk_mounted/_disk_capacity so both guards agree."""
    try:
        if not os.path.ismount(SB_CARD_ROOT):
            return False, 0
        st = os.statvfs(SB_CARD_ROOT)
        return True, max(0, st.f_blocks * st.f_frsize - SB_CARD_RESERVE)
    except OSError:
        return False, 0

def _sb_err(msg, status, **extra):
    body = {"ok": False, "error": msg}
    body.update(extra)
    return Response(json.dumps(body), status=status, mimetype='application/json')

def _sb_ring_bytes(stereo, length_secs):
    """Full ring size for a track (S16_LE @48k). Matches the recorder's
    Recorder._ring_bytes exactly so both sides of the guard agree."""
    return int(length_secs) * SB_RATE * (2 if stereo else 1) * 2

def _sb_mem_available_mb():
    """MemAvailable in MB; None when unreadable (non-Linux dev box) so the
    caller falls back to a budget-only guard, exactly like the recorder."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None

def _sb_track_dir(t):
    """Resolved ring directory for a track dict: its explicit 'dir' if set,
    else the recorder's t<id> default. Single place so the web can never
    disagree with the recorder about where a track's ring lives."""
    d = t.get("dir") if isinstance(t, dict) else None
    if isinstance(d, str) and d:
        return d
    return "t%d" % int(t["id"])

def _sb_resident_bytes(t):
    """tmpfs pages already resident for a track's ring.raw (st_blocks*512).
    Lets the guard exclude an already-armed ring's pages so re-saving an
    unchanged config is never falsely refused (mirrors the recorder).

    Takes the track DICT, not just the id (2026-07-29): this hardcoded
    't%d' % tid, so for a track with a hand-set 'dir' it always stat'd a
    path that does not exist, returned 0, and counted the whole ring as
    NEW growth -- inflating the projection and falsely refusing a save that
    merely re-armed an already-resident ring."""
    try:
        return os.stat(os.path.join(SKIPBUF_ROOT, _sb_track_dir(t),
                                    "ring.raw")).st_blocks * 512
    except (OSError, KeyError, TypeError, ValueError):
        return 0

def _sb_load_tracks_file():
    """Raw track list from the tracks JSON. [] if missing/unreadable — the
    recorder is the authority; the web only mirrors what's on disk."""
    try:
        with open(SKIPBACK_TRACKS) as f:
            d = json.load(f)
        t = d.get("tracks")
        return t if isinstance(t, list) else []
    except Exception:
        return []

def _sb_read_status():
    """(by_id, globals) from the recorder's status JSON. ({}, {}) if absent."""
    try:
        with open(SKIPBACK_STATUS) as f:
            s = json.load(f)
    except Exception:
        return {}, {}
    by_id = {e["id"]: e for e in s.get("tracks", [])
             if isinstance(e, dict) and "id" in e}
    return by_id, s

def _sb_write_tracks(tracks):
    """Atomic tracks-JSON write (tmp + os.replace) — the recorder may stat it
    at any moment, so it must never see a half-written file."""
    tmp = SKIPBACK_TRACKS + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"tracks": tracks}, f, indent=2)
    os.replace(tmp, SKIPBACK_TRACKS)

def _sb_validate_track(t, seen_ids):
    """Web-side validation, kept in lock-step with the recorder's _valid_track
    so the web never writes a config the recorder would silently skip. Returns
    (clean_track, None) or (None, human_reason). id may be None / negative =>
    'allocate a fresh id' (a brand-new row from the UI)."""
    if not isinstance(t, dict):
        return None, "not an object"
    tid = t.get("id")
    if tid is None or (isinstance(tid, int) and not isinstance(tid, bool) and tid < 0):
        tid = None
    elif not isinstance(tid, int) or isinstance(tid, bool) or not 0 <= tid <= 63:
        return None, "id must be an integer 0-63"
    elif tid in seen_ids:
        return None, "duplicate id %s" % tid
    stereo = t.get("stereo", True)
    if not isinstance(stereo, bool):
        return None, "stereo must be true/false"
    length = t.get("length_secs")
    if (not isinstance(length, (int, float)) or isinstance(length, bool)
            or not 5 <= length <= 7200):
        return None, "length_secs must be 5-7200"
    armed = t.get("armed", True)
    if not isinstance(armed, bool):
        return None, "armed must be true/false"
    name = t.get("name")
    name = name.strip() if isinstance(name, str) else ""
    src = t.get("source") or {}
    if not isinstance(src, dict):
        return None, "source must be an object"
    left = str(src.get("left") or "").strip()
    right = str(src.get("right") or "").strip()
    mode = src.get("mode") or ("manual" if left else "auto")
    if mode not in ("auto", "manual"):
        return None, "source mode must be auto/manual"
    clean_src = {"mode": mode}
    if mode == "manual":
        if not left:
            return None, "manual source needs a left port"
        if stereo and not right:
            return None, "stereo manual source needs a right port"
        clean_src["left"] = left
        if stereo:
            clean_src["right"] = right
    store = t.get("store", "ram")
    if store not in ("ram", "card"):
        return None, "store must be ram or card"
    persist = t.get("persist", True)
    if not isinstance(persist, bool):
        return None, "persist must be true/false"
    out = {"id": tid, "name": name, "stereo": stereo,
           "length_secs": int(length), "armed": armed, "source": clean_src,
           "store": store, "persist": persist}
    # Optional per-track ring directory. Same rules as the recorder's
    # _valid_track (plain name, never a path that could escape the root).
    # Preserved rather than dropped because 'dir' is in the recorder's
    # _structural_change() set: resetting it to the t<id> default makes the
    # recorder tear the track down with delete_files=True, DESTROYING that
    # track's recorded ring. Only set when explicitly supplied; the POST
    # handler carries an existing dir forward when the UI omits it.
    if "dir" in t and t["dir"] is not None:
        d = t["dir"]
        if (not isinstance(d, str) or not d or "/" in d or "\\" in d
                or d in (".", "..") or d.startswith(".")):
            return None, "bad dir %r (plain directory name only)" % (d,)
        # Track 0's dir is PINNED to 't0' (2026-07-29). Not a resolve: the
        # recorder's _legacy_symlinks refuses to point the legacy top-level
        # paths at anything but t0 for track 0, and the SAMPLER reads those
        # legacy paths (SAMPLER.RING_PATH) -- so a track 0 on some other dir
        # would leave the live capture path dead while every grab silently
        # read the wrong file. Web-validator-only BY DESIGN: the recorder's
        # own _valid_track must keep accepting a free dir, or its documented
        # proto config (dir: "proto_t0") would break.
        # tid may still be None here (a brand-new row); the POST handler
        # re-checks after id allocation, since _next_id() can hand out 0.
        if tid == 0 and d != "t0":
            return None, ("track 0's ring dir is pinned to 't0' (the sampler "
                          "and the legacy symlinks both read it); got %r" % (d,))
        out["dir"] = d
    return out, None

def _sb_ram_guard(tracks):
    """Contract C4 (web side — the first line of defense; the recorder
    re-checks because the file is hand-editable). Returns (ok, message,
    used_mb). Refuse if the armed-ring sum exceeds the budget OR projected
    free RAM would drop below the floor. Projected free excludes pages already
    resident (growth-only), matching Recorder._apply_ram_guard so the two
    sides never disagree about the same config."""
    mem = _sb_mem_available_mb()
    total = growth = card = 0
    for t in tracks:
        if not t.get("armed"):
            continue
        b = _sb_ring_bytes(t["stereo"], t["length_secs"])
        if t.get("store") == "card":            # card rings cost no RAM
            card += b
            continue
        total += b
        growth += max(0, b - _sb_resident_bytes(t))
    if card:
        mounted, cap = _sb_card_info()
        if not mounted:
            return (False, "CARD tracks need the card partition %s, which isn't "
                    "mounted." % SB_CARD_ROOT, total / 1048576.0)
        if card > cap:
            return (False, "CARD tracks need %d MB but the card partition holds "
                    "%d MB. Shorten or disarm a CARD track."
                    % (round(card / 1048576.0), round(cap / 1048576.0)),
                    total / 1048576.0)
    used_mb = total / 1048576.0
    if used_mb > SB_RAM_BUDGET_MB:
        return (False,
                "This config needs %d MB of ring RAM but the budget is %d MB. "
                "Disarm a track or shorten a buffer."
                % (round(used_mb), SB_RAM_BUDGET_MB), used_mb)
    if mem is not None:
        proj = mem - growth / 1048576.0
        if proj < SB_RAM_FREE_MIN:
            return (False,
                    "This config would leave only %d MB free (need >= %d MB). "
                    "Disarm a track or shorten a buffer."
                    % (round(proj), SB_RAM_FREE_MIN), used_mb)
    return True, "", used_mb

def _sb_default_tracks():
    """'Reset to sensible': ONE stereo track, t0 MAIN auto (resolves to the
    Zoom master when the L12 is up, else pisound in), 20 min, RAM. The old
    second default, t1 SYNTH on ORC1-Synth:out_L/R, recorded NOTHING after
    2026-09-24 (that client was retired; there is no single synth bus since
    the audio-host split), so it was dropped (2026-09-28). The L12 channels
    and module outputs are one-tap additions from the source picker."""
    return [
        {"id": 0, "name": "MAIN", "source": {"mode": "auto"},
         "stereo": True, "length_secs": 1200, "armed": True,
         "store": "ram", "persist": True},
    ]

@app.route('/api/skipback/tracks')
def api_skipback_tracks_get():
    """C6: per-track config merged with live recorder status + RAM globals +
    the (reused) source-picker option lists."""
    cfg = _sb_load_tracks_file()
    by_id, glob = _sb_read_status()
    srcs = _jack_sources()
    out, armed_sum = [], 0
    for t in cfg:
        stereo = bool(t.get("stereo", True))
        length = int(t.get("length_secs", 0) or 0)
        if t.get("armed") and t.get("store") != "card":
            armed_sum += _sb_ring_bytes(stereo, length)
        st = by_id.get(t.get("id"), {})
        out.append({
            "id":          t.get("id"),
            "name":        t.get("name", ""),
            "source":      t.get("source", {"mode": "auto"}),
            "stereo":      stereo,
            "length_secs": length,
            "armed":       bool(t.get("armed", True)),
            # Echoed so a hand-set ring dir can round-trip through the editor.
            # Absent => the recorder's t<id> default. See _sb_validate_track.
            **({"dir": t["dir"]} if t.get("dir") else {}),
            "ring_mb":     round(_sb_ring_bytes(stereo, length) / 1048576.0, 1),
            "recording":   bool(st.get("recording")),
            "resolved":    st.get("resolved", []),
            "connected":   bool(st.get("connected")),
            "fill_secs":   st.get("fill_secs", 0),
            "ram_blocked": bool(st.get("ram_blocked")),
            "drain_err":   st.get("drain_err"),
            # dropped periods since arming (breaks sample alignment before it)
            "gaps":        int(st.get("gaps", 0) or 0),
            "store":       t.get("store", "ram"),
            "persist":     bool(t.get("persist", True)),
            "card_blocked": st.get("card_blocked"),
            "resumed_from": st.get("resumed_from"),
        })
    mem = _sb_mem_available_mb()
    used_mb = glob.get("ram_used_mb")           # recorder's own figure wins
    if used_mb is None:
        used_mb = round(armed_sum / 1048576.0, 1)
    return Response(json.dumps({
        "tracks":        out,
        "ram_used_mb":   used_mb,
        "ram_budget_mb": SB_RAM_BUDGET_MB,
        "ram_free_mb":   round(mem, 1) if mem is not None else None,
        "ram_free_min":  SB_RAM_FREE_MIN,
        "card_mounted":  _sb_card_info()[0],
        "card_cap_mb":   round(_sb_card_info()[1] / 1048576.0),
        "card_used_mb":  round(sum(_sb_ring_bytes(bool(t.get("stereo", True)),
                                                  int(t.get("length_secs", 0) or 0))
                                   for t in cfg if t.get("armed")
                                   and t.get("store") == "card") / 1048576.0),
        "sources":       _labeled_sources(srcs),
        "raw_sources":   srcs,
        "zoom_present":  _zoom_present(),
        "status_ts":     glob.get("ts"),
    }), mimetype='application/json')

@app.route('/api/skipback/tracks', methods=['POST'])
def api_skipback_tracks_post():
    """C6: add / edit / remove tracks (whole-array save) or reset-to-defaults.
      {"action":"save","tracks":[...]}  — validate + RAM guard + atomic write
      {"action":"reset"}                — smart defaults for the current rig
    On success the legacy ~/.orc1_skipback_src.json shim is removed so the
    tracks file becomes authoritative for track 0's source (the recorder's
    _refresh_src_override note: 'Phase 2's track editor must update or delete
    src.json ... then this shim can retire')."""
    try:
        data = request.get_json(force=True) or {}
        action = str(data.get("action", "save"))
        if action == "reset":
            tracks = _sb_default_tracks()
        else:
            raw = data.get("tracks")
            if not isinstance(raw, list):
                return _sb_err("'tracks' must be a list", 400)
            if not raw:
                return _sb_err("at least one track is required", 400)
            if len(raw) > SB_MAX_TRACKS:
                return _sb_err("too many tracks (max %d)" % SB_MAX_TRACKS, 400)
            clean, seen = [], set()
            for i, t in enumerate(raw):
                c, why = _sb_validate_track(t, seen)
                if c is None:
                    return _sb_err("track %d: %s" % (i + 1, why), 400)
                if c["id"] is not None:
                    seen.add(c["id"])
                # did the caller actually SEND these? (the validator defaults
                # them, and a silent store flip rebuilds + wipes the ring)
                c["_sent"] = {k for k in ("store", "persist")
                              if isinstance(t, dict) and k in t}
                clean.append(c)
            # allocate ids + names for brand-new rows (id was None)
            def _next_id():
                n = 0
                while n in seen:
                    n += 1
                seen.add(n)
                return n
            for c in clean:
                # Remember which rows arrived WITHOUT an id. A freshly
                # allocated id may recycle one just freed by a deletion in the
                # same save, and such a row must NOT inherit the deleted
                # track's ring dir (see the carry-forward block below).
                c["_new_row"] = c["id"] is None
                if c["id"] is None:
                    c["id"] = _next_id()
                if not c["name"]:
                    c["name"] = "MAIN" if c["id"] == 0 else "T%d" % c["id"]
            # Carry a hand-set 'dir' forward when the UI omits it (it is not an
            # editable field, so a plain save would otherwise reset it to the
            # t<id> default -- a structural change that makes the recorder
            # delete that track's ring files). Done after id allocation so the
            # match is against the FINAL id. 'reset' deliberately skips this:
            # it is an explicit start-over.
            # Skipped for brand-new rows: _next_id() reuses the lowest free id,
            # so a row added in the same save that deleted track N is allocated
            # id N and would otherwise inherit the DELETED track's dir --
            # silently adopting its ring, or (if other fields differ) tripping
            # _structural_change and deleting it after all.
            prev_by_id = {e["id"]: e for e in _sb_load_tracks_file()
                          if isinstance(e, dict) and "id" in e}
            for c in clean:
                if "dir" not in c and not c["_new_row"]:
                    prev = prev_by_id.get(c["id"])
                    if isinstance(prev, dict) and prev.get("dir"):
                        c["dir"] = prev["dir"]
                # Same carry-forward for store/persist (2026-09-28): a store
                # change is structural (the recorder rebuilds the ring and
                # deletes the old one), so an omitted field must never
                # quietly revert a CARD track to RAM.
                if not c["_new_row"]:
                    prev = prev_by_id.get(c["id"])
                    if isinstance(prev, dict):
                        for k in ("store", "persist"):
                            if k not in c["_sent"] and k in prev:
                                c[k] = prev[k]
            for c in clean:
                c.pop("_new_row", None)      # internal marker, never persisted
                c.pop("_sent", None)
            # ── Post-allocation dir checks (2026-07-29) ──────────────────────
            # Both MUST run here, not in _sb_validate_track: ids are only final
            # after _next_id(), and dirs are only final after the carry-forward
            # above.
            #  1. Re-apply the track-0 pin. A brand-new row arrives with
            #     id: None, and _next_id() reuses the lowest free id -- so a row
            #     the validator saw as "not track 0" can BECOME track 0.
            #  2. Uniqueness must compare RESOLVED dirs. Two tracks sharing one
            #     directory would have two recorder writers on one ring file at
            #     different moduli. Comparing only explicit dirs misses the
            #     collision that matters: dir:"t1" on track 0 vs track 1's
            #     IMPLICIT t1 default.
            seen_dirs = {}
            for i, c in enumerate(clean):
                if c["id"] == 0 and c.get("dir", "t0") != "t0":
                    return _sb_err(
                        "track %d: track 0's ring dir is pinned to 't0' (the "
                        "sampler and the legacy symlinks both read it); got %r"
                        % (i + 1, c.get("dir")), 400)
                rd = _sb_track_dir(c)
                if rd in seen_dirs:
                    return _sb_err(
                        "track %d: ring dir %r already used by track id %s "
                        "(two tracks cannot share one ring file)"
                        % (i + 1, rd, seen_dirs[rd]), 400)
                seen_dirs[rd] = c["id"]
            tracks = clean
        ok, msg, used_mb = _sb_ram_guard(tracks)
        if not ok:
            return _sb_err(msg, 400, ram_used_mb=round(used_mb, 1),
                           ram_budget_mb=SB_RAM_BUDGET_MB)
        _sb_write_tracks(tracks)
        try:
            os.remove(SKIPBACK_SRC)             # retire the C5 track-0 shim
        except OSError:
            pass
        return Response(json.dumps({"ok": True, "tracks": tracks,
                                    "ram_used_mb": round(used_mb, 1)}),
                        mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)

@app.route('/api/skipback/tracks/<int:tid>/grab', methods=['POST'])
def api_skipback_track_grab(tid):
    """C6: grab the last N seconds from ONE track's ring -> ~/recordings.
    Delegates to skipback.sh with the track id; the script clamps secs to that
    ring's length and reads its channel count from the tracks JSON."""
    try:
        import subprocess
        data = request.get_json(force=True) or {}
        secs = max(1, min(7200, int(data.get("seconds", 60))))
        r = _sb_run_grab(["bash", SKIPBACK_SH, str(secs), "wav", str(tid)], 240)
        if r is None:
            return _sb_pending()
        rc, out, err = r
        out = (out or "").strip()
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        if rc != 0 or not saved:
            return _sb_err((err or out or "grab failed").strip(), 500)
        return Response(json.dumps({"ok": True, "file": os.path.basename(saved),
                                    "seconds": secs, "track": tid}),
                        mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


SKIPBACK_MULTI = os.path.expanduser("~/orc/orc1/skipback/skipback_grab_multi.py")
# Stereo JOINS for GRAB ALL: pairs of MONO tracks written as one stereo WAV.
# The web is the only writer; the grab script reads it via --joins-file (the
# brain's SKIPBACK_SAVE_ALL passes the same file, so the FCB honours it too).
SKIPBACK_JOINS = os.path.expanduser("~/.orc1_skipback_joins.json")


def _sb_valid_joins(items):
    """[[L, R], ...] -> cleaned list, or raises ValueError. Track ids 0-63,
    L != R, a track in at most one join."""
    if not isinstance(items, list):
        raise ValueError("joins must be a list of [L, R] pairs")
    out, used = [], set()
    for j in items:
        if (not isinstance(j, list) or len(j) != 2 or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 63
                for x in j)):
            raise ValueError(f"bad join {j!r} (want [L, R] track ids 0-63)")
        if j[0] == j[1]:
            raise ValueError(f"t{j[0]} can't join itself")
        if j[0] in used or j[1] in used:
            raise ValueError(f"a track can be in only one join ({j!r})")
        used.update(j)
        out.append([j[0], j[1]])
    return out


@app.route('/api/skipback/joins', methods=['GET', 'POST'])
def api_skipback_joins():
    """GET -> {"ok": true, "joins": [[L, R], ...]}; POST {"joins": [...]}
    replaces the list (atomic write)."""
    try:
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            joins = _sb_valid_joins(data.get("joins"))
            tmp = SKIPBACK_JOINS + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"joins": joins}, f)
            os.replace(tmp, SKIPBACK_JOINS)
        else:
            try:
                with open(SKIPBACK_JOINS) as f:
                    joins = _sb_valid_joins((json.load(f) or {}).get("joins", []))
            except FileNotFoundError:
                joins = []
        return Response(json.dumps({"ok": True, "joins": joins}),
                        mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 400)


@app.route('/api/skipback/grab_all', methods=['POST'])
def api_skipback_grab_all():
    """Sample-aligned grab of several tracks into ONE folder in ~/recordings
    (skipback_grab_multi.py). Body: {"seconds": N (0 = all held in common),
    "tracks": [ids] (omitted/empty = every recording track)}."""
    try:
        import subprocess
        data = request.get_json(force=True) or {}
        secs = max(0, min(7200, int(data.get("seconds", 0))))
        tracks = [int(t) for t in (data.get("tracks") or [])]
        cmd = ["nice", "-n", "15", "ionice", "-c3", "python3", SKIPBACK_MULTI,
               str(secs), "--joins-file", SKIPBACK_JOINS]
        if tracks:
            cmd += ["--tracks", ",".join(str(t) for t in tracks)]
        if not _SB_GRAB_ALL_LOCK.acquire(blocking=False):
            return _sb_err("a GRAB ALL is already writing -- wait for it", 409)
        try:
            r = _sb_run_grab(cmd, 600, release=_SB_GRAB_ALL_LOCK)
        except Exception:
            _SB_GRAB_ALL_LOCK.release()
            raise
        if r is None:
            return _sb_pending()
        rc, out, err = r
        out = (out or "").strip()
        saved = out.split("Saved:", 1)[1].strip() if "Saved:" in out else ""
        if rc != 0 or not saved:
            return _sb_err((err or out or "grab failed").strip(), 500)
        man = {}
        try:
            with open(os.path.join(saved, "manifest.json")) as f:
                man = json.load(f)
        except Exception:
            pass
        return Response(json.dumps({
            "ok": True, "folder": os.path.basename(saved),
            "seconds": man.get("seconds"), "aligned": man.get("aligned", True),
            "warning": man.get("warning", man.get("note", "")),
            "not_recording": man.get("not_recording", []),
            "files": len(man.get("files", man.get("tracks", []))),
            "joins_skipped": man.get("joins_skipped", []),
            "tracks": [t.get("id") for t in man.get("tracks", [])]}),
            mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


# ==============================================================================
# HEALTH  - the read-only diagnostic's latest report
# ==============================================================================
# orc1-health.timer runs orc1_health.py every 15 min and writes its JSON report
# to ~/.orc1_health.json. This endpoint only READS that file -- it never runs
# the check inline, because the check samples each skipback ring for 3 s and
# would block a web worker for ten seconds or more.
#
# The `age` field matters as much as the findings: a report that has stopped
# being refreshed is itself a fault (the timer died), and a card that showed
# stale results as if they were current would be exactly the kind of
# comfortable lie this whole tool exists to eliminate. The UI shows the age.

# ==============================================================================
# DAW / DJ MODE  - REAPER (orc1-daw) or Mixxx + the Mixtrack (orc1-dj),
#                  fullscreen on the HDMI monitor
# ==============================================================================
# daw/daw_mode.sh owns the sequencing: engines-off stops the six module hosts
# ONE AT A TIME (skipback comes first) and orc1-daw's ExecStopPost brings them
# back however the DAW ends. The web only asks it.

DAW_MODE = os.path.expanduser("~/orc/orc1/daw/daw_mode.sh")


@app.route('/api/daw', methods=['GET', 'POST'])
def api_daw():
    """GET -> {"ok", "active", "app": "reaper"|"mixxx"|null, "state" (the
    unit's ActiveState), "busy" (a start/stop/restore is still running: the
    page keeps every button disabled), "engines_off", "mixtrack_to_mixxx",
    "hdmi"}.
    POST {"action": "start", "app": "reaper"|"mixxx", "engines_off": bool
    (default true: Mixxx + the engines swapped the Pi, measured)}
    | {"action": "stop"}.
    The script runs in the background (engines-off takes a few seconds);
    the page polls GET for the result."""
    import subprocess
    try:
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            act = data.get("action")
            if act == "start":
                cmd = ["sudo", DAW_MODE, "start"]
                if data.get("app") == "mixxx":
                    cmd.append("dj")
                if not data.get("engines_off", True):
                    cmd.append("engines-on")
            elif act == "stop":
                cmd = ["sudo", DAW_MODE, "stop"]
            else:
                return _sb_err("action must be start or stop", 400)
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return Response(json.dumps({"ok": True}), mimetype='application/json')
        r = subprocess.run([DAW_MODE, "status"], capture_output=True, text=True,
                           timeout=5)
        st = json.loads(r.stdout)
        st["ok"] = True
        return Response(json.dumps(st), mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


# ==============================================================================
# VISUALS MODE - line-drawing scenes on the HDMI monitor (orc1-visuals)
# ==============================================================================
# daw/visuals.sh owns start/stop/scene choice. The KNOBS are not here: the page
# sends them as SYNTH_CC on the channel the "visuals" module is bound to (the
# same path a Fire knob or any controller takes), and the renderer writes its
# knob positions back to ~/.orc1_visuals_state.json for the page to show.

VISUALS_SH = os.path.expanduser("~/orc/orc1/daw/visuals.sh")
_VIS_STATE = os.path.expanduser("~/.orc1_visuals_state.json")
_VIS_CONF = os.path.expanduser("~/.orc1_visuals")
_VIS_SCENES = ["tunnel", "mystify", "tube", "harmonograph", "warp", "hypercube",
               "scope", "ridgeline", "rose", "lorenz", "chladni",
               "knot", "polytope", "hopf", "shape4d", "logo"]


@app.route('/api/visuals', methods=['GET', 'POST'])
def api_visuals():
    """GET -> {"ok", "active", "mode", "secs", "scene", "cycling", "knobs",
    "scenes", "channel" (visuals' bound channel, 0 = unbound), "free"
    (channels it could bind to)}.
    POST {"action": "start"|"stop"|"next"}
    | {"action": "set", "name": "orc"|"classic"|<scene>|<xscreensaver name>,
       "secs": N}."""
    import re
    import subprocess
    import orc1_config as _cfg
    try:
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            act = data.get("action")
            if act in ("start", "stop"):
                cmd = ["sudo", VISUALS_SH, act]
            elif act == "next":
                cmd = [VISUALS_SH, "next"]
            elif act == "fps":
                fps = int(data.get("fps", 0))
                if fps not in (30, 45, 60):
                    return _sb_err("fps must be 30, 45 or 60", 400)
                cmd = [VISUALS_SH, "fps", str(fps)]
            elif act == "set":
                name = str(data.get("name", ""))
                if not re.fullmatch(r"[a-z0-9]+", name):
                    return _sb_err("bad name", 400)
                cmd = [VISUALS_SH, "set", name]
                if name in ("orc", "classic"):
                    cmd.append(str(max(5, min(3600, int(data.get("secs", 60))))))
            else:
                return _sb_err("action must be start, stop, next or set", 400)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return _sb_err((r.stderr or r.stdout).strip() or "failed", 400)
            return Response(json.dumps({"ok": True}), mimetype='application/json')

        r = subprocess.run([VISUALS_SH, "status"], capture_output=True, text=True,
                           timeout=5)
        st = json.loads(r.stdout)
        st["ok"] = True
        st["scenes"] = _VIS_SCENES
        try:
            with open(_VIS_CONF) as f:
                lines = f.read().split("\n")
            st["secs"] = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 60
            st["fps"] = int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 30
        except OSError:
            st["secs"], st["fps"] = 60, 30
        try:
            with open(_VIS_STATE) as f:
                vs = json.load(f)
            st.update(scene=vs.get("scene"), cycling=vs.get("cycling"),
                      knobs=vs.get("knobs", {}))
        except (OSError, ValueError):
            st.update(scene=None, cycling=None, knobs={})
        bound = {}
        try:
            with open(os.path.expanduser(_cfg.BINDINGS_FILE)) as f:
                bound = {int(c): m for c, m in
                         json.load(f).get("bindings", {}).items()}
        except (OSError, ValueError):
            pass
        st["channel"] = next((c for c, m in bound.items() if m == "visuals"), 0)
        reserved = _cfg.protected_channels()
        st["free"] = [c for c in range(1, 17)
                      if c not in reserved and c not in bound]
        return Response(json.dumps(st), mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


@app.route('/visuals')
def visuals_page():
    return render_template('visuals.html')


# -- HUD KIOSK: the live play-state screen on the HDMI monitor (orc1-hud) -----------
# daw/hud.sh owns start/stop; the renderer only reads the brain's tmpfs feed
# (see /api/hud above). It shares the monitor with VISUALS / DAW / DJ: starting
# any of those replaces it.
HUD_SH = os.path.expanduser("~/orc/orc1/daw/hud.sh")


@app.route('/api/hud_kiosk', methods=['GET', 'POST'])
def api_hud_kiosk():
    """GET -> {"ok", "active", "feed_age_s"}.  POST {"action": "start"|"stop"}."""
    import subprocess
    try:
        if request.method == 'POST':
            act = (request.get_json(force=True) or {}).get("action")
            if act not in ("start", "stop"):
                return _sb_err("action must be start or stop", 400)
            r = subprocess.run(["sudo", HUD_SH, act], capture_output=True, text=True,
                               timeout=15)
            if r.returncode != 0:
                return _sb_err((r.stderr or r.stdout).strip() or "failed", 400)
            return Response(json.dumps({"ok": True, "note": (r.stdout or "").strip()}),
                            mimetype='application/json')
        r = subprocess.run([HUD_SH, "status"], capture_output=True, text=True, timeout=5)
        st = json.loads(r.stdout)
        st["ok"] = True
        return Response(json.dumps(st), mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


_VIS_TAP_SRC = os.path.expanduser("~/.orc1_vistap")


def _vis_patterns():
    import sys as _sys
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daw")
    if d not in _sys.path:
        _sys.path.insert(0, d)
    import vis_patterns
    return vis_patterns


@app.route('/api/visuals/patterns', methods=['GET', 'POST'])
def api_visuals_patterns():
    """The background's pattern SETS in knob order (daw/vis_patterns.py):
    [{"name", "folder" (null = built-in), "names": [...]}]. SET = CC 50,
    PATTERN = CC 34 (bucketed over the set's names). The last set is
    "custom" = your uploads (~/orc1_patterns).
    POST multipart "file" = an SVG tile for CUSTOM (2026-10-12): checked
    (512 KB max, safe SVG, renders, actually shows: the ORC reads the RED
    channel) by vis_patterns.save_upload -> {"name", "warning"}; refused
    with the reason otherwise. Same name = replaces your earlier one."""
    try:
        vp = _vis_patterns()
        if request.method == 'POST':
            # judge the size BEFORE request.files spools the body to /tmp (RAM)
            if request.content_length is None:
                return _sb_err("upload needs a Content-Length", 411)
            if request.content_length > vp.MAX_UPLOAD + 64 * 1024:
                return _sb_err("too big (512 KB max)", 413)
            up = request.files.get("file")
            if up is None:
                return _sb_err("no file", 400)
            data = up.read(vp.MAX_UPLOAD + 1)
            try:
                text = data.decode("utf-8")
                name, warn = vp.save_upload(up.filename, text)
            except UnicodeDecodeError:
                return _sb_err("not a text SVG file", 400)
            except ValueError as e:
                return _sb_err(str(e), 400)
            return Response(json.dumps({"ok": True, "name": name, "warning": warn}),
                            mimetype='application/json')
        sets = [{"name": n, "folder": f, "names": names}
                for n, f, names in vp.pattern_sets(vp.BUILTIN)]
        return Response(json.dumps({"ok": True, "sets": sets}), mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


def _vis_logos():
    _vis_patterns()                      # puts daw/ on sys.path
    import vis_logos
    return vis_logos


@app.route('/api/visuals/logos', methods=['GET', 'POST'])
def api_visuals_logos():
    """Logos / line drawings (daw/vis_logos.py). GET -> {"names" (LOGO knob
    order after "chord"), "user" (the ones you uploaded: deletable)}.
    POST multipart "file" = an SVG line drawing -> ~/orc1_logos/<name>.svg,
    accepted only if it is <= 2 MB and parses into lines (and not into more
    than vis_logos.MAX_RAW_POINTS points). A name equal to a shipped logo's
    REPLACES it on the LOGO knob (delete yours and the shipped one is back)."""
    import re as _re
    try:
        vl = _vis_logos()
        if request.method == 'POST':
            # Judge the size BEFORE touching request.files: parsing the form
            # spools the WHOLE body to /tmp first, and /tmp is RAM (tmpfs).
            # 64 KB of slack covers the multipart boundary + part headers. No
            # length (a chunked upload) can't be judged up front: refused.
            if request.content_length is None:
                return _sb_err("upload needs a Content-Length", 411)
            if request.content_length > vl.MAX_BYTES + 64 * 1024:
                return _sb_err("too big (2 MB max)", 413)
            up = request.files.get("file")
            if up is None:
                return _sb_err("no file", 400)
            data = up.read(vl.MAX_BYTES + 1)
            if len(data) > vl.MAX_BYTES:
                return _sb_err("too big (2 MB max)", 400)
            try:
                text = data.decode("utf-8")
                polys = vl.parse_svg(text)
            except (UnicodeDecodeError, ValueError, SyntaxError) as e:
                return _sb_err("not a usable SVG line drawing: %s" % e, 400)
            base = os.path.splitext(os.path.basename(up.filename or "logo"))[0]
            name = _re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40] or "logo"
            os.makedirs(vl.user_dir(), exist_ok=True)
            tmp = os.path.join(vl.user_dir(), name + ".svg.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(vl.user_dir(), name + ".svg"))
            return Response(json.dumps({"ok": True, "name": name, "lines": len(polys)}),
                            mimetype='application/json')
        lst = vl.logo_list()
        user = [n for n, p in lst if os.path.dirname(p) == vl.user_dir()]
        return Response(json.dumps({"ok": True, "names": [n for n, _ in lst], "user": user}),
                        mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


@app.route('/api/visuals/logo/<name>', methods=['GET', 'DELETE'])
def api_visuals_logo(name):
    """GET -> the logo's parsed lines as JSON for the page's preview (drawn
    from these, never by sending the raw SVG to the browser: an SVG can
    carry script). DELETE -> remove one of YOUR uploads (not a shipped one)."""
    try:
        vl = _vis_logos()
        paths = dict(vl.logo_list())
        if name not in paths:
            return _sb_err("no such logo", 404)
        if request.method == 'DELETE':
            if os.path.dirname(paths[name]) != vl.user_dir():
                return _sb_err("only your uploaded logos can be deleted", 403)
            os.remove(paths[name])
            return Response(json.dumps({"ok": True}), mimetype='application/json')
        polys = vl.load(paths[name])
        polys = vl.normalize(polys, max_points=1500)
        return Response(json.dumps({"ok": True, "polys": [[[round(x, 4), round(y, 4)] for x, y in p]
                                                          for p in polys]}),
                        mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


@app.route('/api/visuals/pattern/<folder>/<slug>.png', methods=['GET', 'DELETE'])
def api_visuals_pattern_png(folder, slug):
    """One file tile as a PNG (rasterised on first use, then cached), for
    the /visuals gallery. Only the pattern folders and plain slugs.
    DELETE removes one of YOUR tiles (custom only)."""
    import re as _re
    from flask import send_file
    if folder not in ("hero", "monster", "custom") or not _re.fullmatch(r"[a-z0-9-]{1,80}", slug):
        return _sb_err("no such pattern", 404)
    try:
        vp = _vis_patterns()
        if request.method == 'DELETE':
            if folder != vp.CUSTOM:
                return _sb_err("only your custom patterns can be deleted", 403)
            if not vp.delete_upload(slug):
                return _sb_err("no such pattern", 404)
            return Response(json.dumps({"ok": True}), mimetype='application/json')
        if slug not in vp._slugs(folder):
            return _sb_err("no such pattern", 404)
        resp = send_file(vp.tile_png(folder, slug), mimetype="image/png")
        # yours can be replaced under the same name: never let a browser keep an old one
        resp.headers["Cache-Control"] = "no-cache" if folder == vp.CUSTOM else "max-age=86400"
        return resp
    except Exception as e:
        return _sb_err(str(e), 500)


@app.route('/api/visuals/source', methods=['GET', 'POST'])
def api_visuals_source():
    """What the visuals LISTEN to. orc1-vistap (daw/orc_jacktap.c, a C
    listen-only JACK client) reads ~/.orc1_vistap -- line 1 = left port,
    line 2 = right (blank = mono) or "ring" = skipback's RAM ring as before
    -- and re-reads it within 0.5 s, so a change needs no restart.
    GET -> {"ok", "mode": "ports"|"ring", "left", "right", "sources": every
    JACK output port}. POST {"mode": "ring"} | {"left": port, "right": port}."""
    try:
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            if data.get("mode") == "ring":
                text = "ring\n"
            else:
                srcs = set(_jack_sources())
                left = str(data.get("left") or "")
                right = str(data.get("right") or "")
                if left not in srcs or (right and right not in srcs):
                    return _sb_err("not a JACK output port right now", 400)
                text = "%s\n%s\n" % (left, right if right != left else "")
            tmp = _VIS_TAP_SRC + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, _VIS_TAP_SRC)
            return Response(json.dumps({"ok": True}), mimetype='application/json')
        try:
            with open(_VIS_TAP_SRC) as f:
                lines = [ln.strip() for ln in f.read().splitlines()]
        except OSError:
            lines = ["system:capture_1", "system:capture_2"]   # the tap's default
        left = lines[0] if lines else ""
        right = lines[1] if len(lines) > 1 else ""
        st = {"ok": True, "sources": _jack_sources()}
        if left in ("", "ring"):
            st.update(mode="ring", left=None, right=None)
        else:
            st.update(mode="ports", left=left, right=right or left)
        return Response(json.dumps(st), mimetype='application/json')
    except Exception as e:
        return _sb_err(str(e), 500)


@app.route('/api/visuals/audio')
def api_visuals_audio():
    """What the renderer hears, for the /visuals spectrum: written ~10x/s to
    tmpfs by daw/orc_visuals.py (publish_audio). "age" = how old it is, so
    the page can say the renderer isn't running instead of freezing."""
    try:
        with open("/tmp/orc1_visuals_audio.json") as f:
            d = json.load(f)
        d["ok"] = True
        d["age"] = max(0.0, time.time() - float(d.get("t") or 0))
        return Response(json.dumps(d), mimetype='application/json')
    except (OSError, ValueError):
        return Response(json.dumps({"ok": False, "error": "renderer not running"}),
                        mimetype='application/json')


_VIS_AUDIO_STREAMS_MAX = 4
_vis_audio_streams = 0
_vis_audio_lock = threading.Lock()


@app.route('/api/visuals/audio/stream')
def api_visuals_audio_stream():
    """The same file as /api/visuals/audio, as Server-Sent Events: one frame
    each time the renderer rewrites it (~25/s). One connection instead of a
    poll per frame (each poll is an access-log line on the SD card). The page
    judges staleness by its OWN receive time (phone and Pi clocks differ)."""
    import orc1_playstate as ps
    global _vis_audio_streams
    with _vis_audio_lock:
        if _vis_audio_streams >= _VIS_AUDIO_STREAMS_MAX:
            return Response("too many spectrum streams", status=429)
        _vis_audio_streams += 1

    def gen():
        global _vis_audio_streams
        try:
            yield from ps.sse_events(path="/tmp/orc1_visuals_audio.json", poll=0.02,
                                     keepalive=5.0)
        finally:
            with _vis_audio_lock:
                _vis_audio_streams -= 1
    return Response(gen(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -- LIVE PLAY-STATE (the HUD) ----------------------------------------------------
# The brain publishes /dev/shm/orc1_playstate.json (orc1_playstate.py) whenever
# something changes. These routes only READ that file -- a slow or stuck browser
# can never slow the brain. "age" is how long since the brain last wrote, so a
# dead brain reads as stale instead of looking live.
_HUD_STREAMS_MAX = 4            # one tab per screen is plenty; strays must not pile up
_hud_streams = 0
_hud_lock = threading.Lock()


@app.route('/hud')
def hud_page():
    """The phone / desktop HUD: the live play-state over /api/hud/stream."""
    return render_template('hud.html')


@app.route('/api/hud')
def api_hud():
    import orc1_playstate as ps
    d, age = ps.read()
    if d is None:
        return Response(json.dumps({"ok": False, "error": "brain not publishing"}),
                        mimetype='application/json')
    d["ok"] = True
    d["age"] = age
    return Response(json.dumps(d), mimetype='application/json')


@app.route('/api/hud/stream')
def api_hud_stream():
    """Server-Sent Events: one frame per change, a keepalive every 5 s."""
    import orc1_playstate as ps
    global _hud_streams
    with _hud_lock:
        if _hud_streams >= _HUD_STREAMS_MAX:
            return Response("too many HUD streams", status=429)
        _hud_streams += 1

    def gen():
        global _hud_streams
        try:
            yield from ps.sse_events(keepalive=5.0)
        finally:
            with _hud_lock:
                _hud_streams -= 1
    return Response(gen(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_HEALTH_JSON = os.path.expanduser("~/.orc1_health.json")
_health_run_last = 0.0          # debounce floor for POST /api/health/run


@app.route('/api/health')
def api_health():
    try:
        with open(_HEALTH_JSON) as f:
            rep = json.load(f)
        if not isinstance(rep, dict):
            raise ValueError("report is not a JSON object")
        # Inside the guard on purpose: a non-numeric ts (hand-edited file)
        # would otherwise raise here and return Flask's generic 500 instead of
        # this endpoint's own {"ok": false, ...} shape, which the card knows
        # how to display honestly.
        rep["ok"] = True
        rep["age"] = max(0.0, time.time() - float(rep.get("ts") or 0))
    except FileNotFoundError:
        return Response(json.dumps({
            "ok": False,
            "error": "no report yet -- is orc1-health.timer enabled?"}),
            status=404, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')
    return Response(json.dumps(rep), mimetype='application/json')


@app.route('/api/health/run', methods=['POST'])
def api_health_run():
    """Trigger a fresh check in the background.

    Fire-and-forget via systemd rather than running it here: the check takes
    ~10 s, which would hold a web worker, and letting systemd own it means the
    manual path and the timer path run the identical unit with the identical
    user and environment -- so a result you triggered by hand can never differ
    from the scheduled one.

    Debounced: the page is unauthenticated on the LAN, and without this a
    client re-POSTing as each ~10 s run finished could keep the check running
    back to back forever. That would not touch the RT audio threads (this runs
    Nice=10 / IO idle) but it would contend with orc1 and this web app, which
    are ordinary priority. systemd merges concurrent start jobs, so the risk
    is sustained load rather than parallel processes -- a time floor is enough.
    """
    global _health_run_last
    now = time.time()
    if now - _health_run_last < 30:
        return Response(json.dumps({
            "ok": False, "throttled": True,
            "error": "a check ran less than 30 s ago"}),
            status=429, mimetype='application/json')
    try:
        import subprocess
        subprocess.Popen(["sudo", "systemctl", "start", "orc1-health.service"])
        _health_run_last = now
        return Response(json.dumps({"ok": True, "started": True}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ==============================================================================
# PATCHBAY  - /patch : send any input anywhere, as mono or as a stereo pair
# ==============================================================================
# Built entirely from the LIVE JACK graph on every load. Nothing here is a
# stored table of what the rig contains, because every previous attempt at
# routing in this project failed the same way: a table written months ago
# stopped matching the hardware and the UI kept cheerfully offering
# destinations that no longer existed.
#
# NAMING RULE, from owner: ports are labelled by POSITION, never by the gear
# plugged into them -- "IN 5/6", not "EPM8". What is patched into the mixer
# changes with what he is doing; the socket number does not. An earlier tool
# got this exactly backwards and was thrown away for it.
#
# MONO vs STEREO is the PLAYER'S choice per send, not something inferred:
# channels 1-8 of an L-12 are mono strips, 9/10 and 11/12 are stereo pairs,
# and any of them may be used either way depending on what is plugged in.
# Mismatches need no processing -- JACK sums multiple connections into one
# input port, so a stereo source into a mono track sums, and a mono source
# into a stereo destination feeds both sides. Both are plain wires.

def _patch_ports():
    """Every audio port right now, with direction. [] if jackd is unreachable."""
    out, cur, res = _jack(["jack_lsp", "-t", "-p"]), None, []
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = {"name": line.strip(), "dir": None, "audio": False}
            res.append(cur)
        elif cur is not None:
            s = line.strip().lower()
            if "audio" in s:
                cur["audio"] = True
            elif s.startswith("properties:"):
                if "output" in s:
                    cur["dir"] = "out"
                elif "input" in s:
                    cur["dir"] = "in"
    return [p for p in res if p["audio"] and p["dir"]]


def _patch_conns(ports=None):
    """[[src_out, dst_in], ...] -- each connection once, in signal direction.

    `ports` may be passed in by a caller that has already enumerated them, so
    the reconciler below does not run jack_lsp twice per pass; omitted, it
    enumerates them itself exactly as before.
    """
    out, cur = _jack(["jack_lsp", "-c"]), None
    dirs = {p["name"]: p["dir"]
            for p in (ports if ports is not None else _patch_ports())}
    seen = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = line.strip()
        elif cur:
            other = line.strip()
            # jack_lsp -c lists connections from BOTH ends; keep the one
            # oriented output -> input so the UI never shows a wire twice.
            if dirs.get(cur) == "out" and dirs.get(other) == "in":
                seen.add((cur, other))
            elif dirs.get(cur) == "in" and dirs.get(other) == "out":
                seen.add((other, cur))
    return sorted(seen)


def _patch_card():
    """The interface jackd is actually on, for labelling. Not hardcoded."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=4).stdout
        for l in out.splitlines():
            if "jackd" in l and "-d" in l and "grep" not in l:
                import re as _re
                m = _re.search(r"-d\s*hw:(\S+)", l)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "SYSTEM"


def _patch_label(name, card, track_names):
    """Position-based label for a port. Never the name of attached gear."""
    client, _, short = name.partition(":")
    if client == "system":
        if short.startswith("capture_"):
            return "%s IN %s" % (card, short.split("_")[-1])
        if short.startswith("playback_"):
            return "%s OUT %s" % (card, short.split("_")[-1])
    if client == "skipback-rec":
        # t<id>_L / _R / _M -- label with the track's own name, which the
        # player set, plus the channel so a pair is obvious.
        body = short[1:]
        tid = body.split("_")[0]
        ch = body.split("_")[-1]
        nm = track_names.get(tid, "TRACK %s" % tid)
        return "SKIPBACK %s %s" % (nm, ch)
    # A bridged card's client is "<card>-in" / "<card>-out" and its ports are
    # capture_N / playback_N. Label those by POSITION like the jackd card --
    # "M3 OUT 1", not "PLAYBACK 1" -- so a bridged interface reads the same way
    # as the built-in one and nothing is named after the gear plugged into it.
    for suffix, word in (("-in", "IN"), ("-out", "OUT")):
        if client.endswith(suffix) and "_" in short:
            n = short.rsplit("_", 1)[-1]
            if n.isdigit():
                return "%s %s %s" % (client[:-len(suffix)].upper(), word, n)
    return short.replace("_", " ").upper()


@app.route('/patch')
def patch_page():
    return render_template('patch.html')


@app.route('/api/patch')
def api_patch():
    ports = _patch_ports()
    if not ports:
        return Response(json.dumps({
            "ok": False,
            "error": "jackd is not reachable -- no audio graph to show"}),
            status=503, mimetype='application/json')
    card = _patch_card()
    track_names = {}
    try:
        cfg = json.load(open(os.path.expanduser(
            "~/.orc1_skipback_tracks.json")))
        for t in (cfg.get("tracks") or []):
            if isinstance(t, dict):
                track_names[str(t.get("id"))] = t.get("name") or ""
    except Exception:
        pass

    def _row(p):
        client = p["name"].split(":", 1)[0]
        return {"name": p["name"], "client": client, "dir": p["dir"],
                "label": _patch_label(p["name"], card, track_names)}

    return Response(json.dumps({
        "ok": True, "card": card,
        "sources": [_row(p) for p in ports if p["dir"] == "out"],
        "destinations": [_row(p) for p in ports if p["dir"] == "in"],
        "connections": _patch_conns(ports),
        # So the CURRENT PATCH list can say which wires will come back after a
        # power cycle and which are only live until the next restart.
        "saved": _patch_saved_pairs(),
    }), mimetype='application/json')


def _module_owned_wires(conns, phys=None):
    """{(src, dst): module label} for each module's CONFIGURED output wires --
    the pair /api/module_outputs owns (resolve_module_out of its saved dest).
    Extra fan-out wires a player added from a module out are NOT included:
    those are the player's own, freely cut. Asks jackd for the physical ports
    only when a module out is actually in `conns`."""
    import orc1_config as _cfg
    outs = {}
    for key in _cfg.MODULE_OUT_KEYS:
        jk = _cfg.MODULE_JACK_NAMES.get(key)
        if jk:
            outs[key] = (jk + ":out_L", jk + ":out_R")
    srcs = {s for (s, _d) in conns}
    if not any(p in srcs for pair in outs.values() for p in pair):
        return {}
    phys = phys if phys is not None else _mo_phys_ports()
    owned = {}
    for key, (ol, orr) in outs.items():
        pair = _cfg.resolve_module_out(_cfg.load_module_out(key), phys)
        if pair:
            label = _cfg.MODULE_LABELS.get(key, key)
            owned[(ol, pair[0])] = label
            owned[(orr, pair[1])] = label
    return owned


def _patch_protected(conns=None, phys=None):
    """{(src, dst): reason} for wires a misclick must not silently remove.

    NOT a denylist -- everything here can still be torn down deliberately, by
    sending force=true. It exists because these are not recoverable from where
    the player is standing:

      * A module's CONFIGURED output (drum/poly/chip/... -> its pair from
        MODULE OUTPUTS). The host only connects it at startup, so cutting it
        silences the module with no visible cause -- and re-picking the same
        pair in the MODULE OUTPUTS dropdown fires no change event. (Until
        2026-09-23 this guarded the retired ORC1-Synth, so after the audio host
        split it protected nothing.)
      * The skipback recorder's source wiring is the always-on safety net this
        whole machine exists for. Cutting it stops the ring recording while
        every status field still reads RECORDING.

    Resolved LIVE from the graph rather than hardcoded, so it stays correct on
    a different card or a re-sourced track.
    """
    conns = conns if conns is not None else _patch_conns()
    owned = _module_owned_wires(conns, phys)
    out = {}
    for src, dst in conns:
        if (src, dst) in owned:
            out[(src, dst)] = ("this is the %s module's output -- change it "
                               "on MODULE OUTPUTS; cut here, the module goes "
                               "silent until it restarts" % owned[(src, dst)])
        elif dst.startswith("skipback-rec:"):
            out[(src, dst)] = ("this feeds a skipback track -- removing it "
                               "stops that ring recording")
    return out


@app.route('/api/patch/protected')
def api_patch_protected():
    return Response(json.dumps({
        "ok": True,
        "protected": [[a, b, why] for (a, b), why in
                      _patch_protected().items()]}),
        mimetype='application/json')


@app.route('/api/patch', methods=['POST'])
def api_patch_set():
    """Connect or disconnect a source selection to a destination selection.

    Fans mono<->stereo deliberately: 1->2 feeds both sides, 2->1 sums into the
    one input, N->N is pairwise. Every jack_connect/disconnect result is
    CHECKED and reported -- the defining bug of this project is a routing
    control that reports success while wiring nothing, which is what happens
    when subprocess output is discarded.
    """
    import subprocess
    try:
        d = request.get_json(force=True) or {}
        op = d.get("op")
        src = [s for s in (d.get("src") or []) if isinstance(s, str)]
        dst = [s for s in (d.get("dst") or []) if isinstance(s, str)]
        if op not in ("connect", "disconnect") or not src or not dst:
            return Response(json.dumps({"ok": False,
                                        "error": "need op, src, dst"}),
                            status=400, mimetype='application/json')

        live = {p["name"]: p["dir"] for p in _patch_ports()}
        bad = [p for p in src + dst if p not in live]
        if bad:
            return Response(json.dumps({
                "ok": False,
                "error": "these ports do not exist: " + ", ".join(bad)}),
                status=400, mimetype='application/json')
        if any(live[p] != "out" for p in src) or any(live[p] != "in" for p in dst):
            return Response(json.dumps({
                "ok": False,
                "error": "sources must be outputs and destinations inputs"}),
                status=400, mimetype='application/json')

        if len(src) == 1:
            pairs = [(src[0], x) for x in dst]
        elif len(dst) == 1:
            pairs = [(x, dst[0]) for x in src]
        elif len(src) == len(dst):
            pairs = list(zip(src, dst))
        else:
            # zip() would silently truncate to the shorter list and apply a
            # PARTIAL patch while reporting success. Refuse instead.
            return Response(json.dumps({
                "ok": False,
                "error": "cannot pair %d source(s) with %d destination(s) -- "
                         "use 1, or matching counts" % (len(src), len(dst))}),
                status=400, mimetype='application/json')

        # Guard the two wires that cannot be restored from the instrument.
        # Refusing is recoverable; a silent teardown of the main output is not.
        if op == "disconnect" and not d.get("force"):
            prot = _patch_protected()
            hits = [{"src": a, "dst": b, "why": prot[(a, b)]}
                    for a, b in pairs if (a, b) in prot]
            if hits:
                return Response(json.dumps({
                    "ok": False, "protected": hits,
                    "error": hits[0]["why"]}),
                    status=409, mimetype='application/json')

        tool = "jack_connect" if op == "connect" else "jack_disconnect"
        done, failed = [], []
        # THE REWIRE AND THE BOOKKEEPING MUST BE ONE ATOMIC STEP, or a
        # disconnect races the reconciler and comes straight back on:
        #
        #   this handler   jack_disconnect(W)          -- W leaves the graph
        #   reconciler     reads the saved file        -- W is still in it
        #   reconciler     reads the graph             -- W is not in it
        #   reconciler     jack_connect(W)             -- W IS BACK
        #   this handler   _patch_saved_remove(W)      -- too late
        #
        # leaving W live in the graph, absent from the saved patch, and nothing
        # that will ever take it away again. The window is the reconciler's own
        # jack_lsp calls -- ~130ms of every 10s cycle, not microseconds -- and
        # on a stereo pair the visible result is one channel restored and one
        # not, which reads as "the patchbay is flaky". Holding the lock across
        # both makes the reconciler wait its turn and then see the truth.
        # _patch_lock is an RLock precisely so the nested acquire in
        # _patch_saved_add/_remove is fine.
        with _patch_lock:
            for a, b in pairs:
                err = _patch_run(tool, a, b)
                if err is None:
                    done.append([a, b])
                else:
                    failed.append({"src": a, "dst": b, "error": err})

            # Record the PLAYER'S INTENT for every wire that actually took.
            # Only `done` -- recording a pair that failed to connect would put
            # a wire in the saved patch that has never carried audio, which is
            # the exact class of lie the rest of this endpoint is written to
            # avoid.
            warn = None
            if done:
                try:
                    if op == "connect":
                        _patch_saved_add(done)
                    else:
                        _patch_saved_remove(done)
                except Exception as e:
                    # The audio is ALREADY rewired at this point. Letting this
                    # reach the outer handler would return a 500 saying the
                    # whole operation failed, while the wire is live -- the
                    # usual lie of this project, merely inverted. Report the
                    # wire truthfully and the bookkeeping failure separately.
                    warn = ("the wire is live but could not be saved: %s" % e)
                    print("[patch] %s" % warn, flush=True)
            saved_now = _patch_saved_pairs()

        body = {"ok": not failed, "op": op, "done": done, "failed": failed,
                "saved": saved_now, "connections": _patch_conns()}
        if warn:
            body["warning"] = warn
        return Response(json.dumps(body), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ------------------------------------------------------ MODULE OUTPUTS ---
# Per-module output routing (2026-09-15). Model lives in orc1_config.py. This
# OWNS each module's out_L/out_R destination: it rewires the wires LIVE and
# persists the choice, and the hosts re-apply it at their own startup. It is a
# DIFFERENT owner from the /patch saved list (which never records these), so the
# /patch reconciler leaves them alone. Fixes the "routed the synth elsewhere but
# it still comes through the mains" bug: the hosts used to force-connect to 1/2
# unconditionally, with nothing that could make a module exclusive to 3/4.

_mo_lock = threading.RLock()


def _mo_phys_ports():
    """Physical playback (speaker) ports in JACK order -- the SAME order the
    hosts get from S.phys_playback_ports(), so both agree what 'out-3-4' means.
    [] if jackd is unreachable."""
    out, cur, res = _jack(["jack_lsp", "-t", "-p"]), None, []
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = {"name": line.strip(), "audio": False,
                   "inp": False, "phys": False}
            res.append(cur)
        elif cur is not None:
            s = line.strip().lower()
            if "audio" in s:
                cur["audio"] = True
            if s.startswith("properties:"):
                if "input" in s:
                    cur["inp"] = True
                if "physical" in s:
                    cur["phys"] = True
    return [p["name"] for p in res if p["audio"] and p["inp"] and p["phys"]]


def _mo_pairs(phys):
    """Selectable destinations: consecutive physical pairs (1-2, 3-4, ...) plus
    'none'. id matches orc1_config.resolve_module_out ('out-<a>-<b>')."""
    pairs = []
    for i in range(0, len(phys) - 1, 2):
        a, b = i + 1, i + 2
        pairs.append({"id": "out-%d-%d" % (a, b), "label": "%d-%d" % (a, b),
                      "ports": [phys[i], phys[i + 1]]})
    pairs.append({"id": "none", "label": "off", "ports": []})
    return pairs


def _module_current_card(key):
    """The card a module is routed to right now, read from its systemd drop-in
    (Environment=JACK_SERVER=<card>), or 'main' on the default server. Read-only,
    no sudo -- the drop-in is world-readable."""
    conf = "/etc/systemd/system/orc1-%s.service.d/50-jack-server.conf" % key
    try:
        with open(conf) as f:
            for line in f:
                line = line.strip()
                if line.startswith("Environment=JACK_SERVER="):
                    return line.split("=", 2)[2].strip() or "main"
    except Exception:
        pass
    return "main"


def _assignable_cards():
    """Card choices for the per-module CARD selector: 'main' + every non-minor
    OUTPUT card that is not the default-server's own card (routing a module to
    that card IS 'main')."""
    out = ["main"]
    try:
        main_card = _patch_card()
        for c in _alsa_cards():
            if c["name"] in _CARD_MINOR or not c.get("has_play"):
                continue
            if c["name"] == main_card:
                continue
            out.append(c["name"])
    except Exception:
        pass
    return out


def _mo_state():
    import orc1_config as _cfg
    phys = _mo_phys_ports()
    conns = _patch_conns()
    mods = []
    for key in _cfg.MODULE_OUT_KEYS:
        jk = _cfg.MODULE_JACK_NAMES.get(key, "")
        dest = _cfg.load_module_out(key)               # None = default pair
        resolved = _cfg.resolve_module_out(dest, phys)
        live = sorted({d for (s, d) in conns if s.startswith(jk + ":")})
        mods.append({
            "key": key,
            "label": _cfg.MODULE_LABELS.get(key, key),
            "jack": jk,
            "dest": dest or "",                        # "" = default (1st pair)
            "resolved": list(resolved) if resolved else [],
            "live": live,
            "card": _module_current_card(key),         # 'main' or a card name
            "card_assignable": key in _CARD_ASSIGNABLE,
        })
    return {"phys": phys, "pairs": _mo_pairs(phys), "modules": mods,
            "cards": _assignable_cards()}


def _mo_write(key, dest):
    """Atomic RMW of ~/.orc1_module_outputs.json: keep other modules' choices,
    set this one. Empty dest removes the key (= revert to the default pair)."""
    import orc1_config as _cfg
    path = _cfg.MODULE_OUT_FILE
    try:
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    if dest:
        d[key] = dest
    else:
        d.pop(key, None)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@app.route('/api/module_outputs')
def api_module_outputs():
    try:
        return Response(json.dumps(_mo_state()), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


@app.route('/api/module_outputs', methods=['POST'])
def api_module_outputs_set():
    """Set one module's output destination: rewire it LIVE and persist.

    Body: {"module": "<key>", "dest": "out-3-4" | "none" | "" }
    "" / "main" / "default" -> revert to the default first physical pair.
    """
    try:
        import orc1_config as _cfg
        d = request.get_json(force=True) or {}
        key = d.get("module")
        dest = d.get("dest") or ""
        if key not in _cfg.MODULE_OUT_KEYS:
            return Response(json.dumps({"ok": False,
                            "error": "unknown module %r" % key}),
                            status=400, mimetype='application/json')
        if dest in ("main", "default"):
            dest = ""                                  # stored absence = default
        if dest and dest != "none" and not dest.startswith("out-"):
            return Response(json.dumps({"ok": False,
                            "error": "bad dest %r" % dest}),
                            status=400, mimetype='application/json')
        phys = _mo_phys_ports()
        if not phys:
            return Response(json.dumps({"ok": False,
                            "error": "jackd not reachable"}),
                            status=503, mimetype='application/json')
        jk = _cfg.MODULE_JACK_NAMES[key]
        out_l, out_r = jk + ":out_L", jk + ":out_R"
        pair = _cfg.resolve_module_out(dest if dest else None, phys)
        done, failed = [], []
        with _mo_lock:
            # 1) tear down every current wire FROM this module's outs (this
            #    endpoint owns the module's single output destination).
            for (s, dst) in _patch_conns():
                if s in (out_l, out_r):
                    _patch_run("jack_disconnect", s, dst)
            # 2) connect to the resolved pair (unless 'none').
            if pair:
                for src, dst in ((out_l, pair[0]), (out_r, pair[1])):
                    err = _patch_run("jack_connect", src, dst)
                    (done if err is None else failed).append(
                        [src, dst] if err is None
                        else {"src": src, "dst": dst, "error": err})
            # 3) persist the intent (the hosts re-apply it at startup).
            _mo_write(key, dest)
        return Response(json.dumps({"ok": not failed, "module": key,
                        "dest": dest, "done": done, "failed": failed,
                        "state": _mo_state()}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# --------------------------------------------------- PER-CARD MODULE OUT ---
# Per-host-jackd model (2026-09-22): route a module to a CARD's own jackd
# (jackd -n <card>), or back to the default (main) server. This is a DIFFERENT
# axis from /api/module_outputs above (which picks the output PAIR within the
# default graph): here the module's whole host is rebound to another card's
# server. The heavy lifting -- stop any bridge on the card (own-jackd wins),
# start orc1-jackd@<card>, write the module's JACK_SERVER drop-in, daemon-reload,
# restart the host, auto-stop unused servers -- is a root helper the web calls
# via sudo (passwordless sudo is available on this box). The host then attaches
# to that server and self-connects to its physical outs; nothing is wired here.
_ASSIGN_HELPER = "/home/pibox/orc/orc1/orc1_assign_module_out.sh"
# Modules whose host adapts to the server's sample rate (bbkit is still 48k-only).
_CARD_ASSIGNABLE = {"poly", "chip", "drum", "mono", "sampler"}


@app.route('/api/module_card', methods=['POST'])
def api_module_card_set():
    """Body: {"module": "poly", "card": "M3" | "main"}. Route a module to a
    card's own jackd, or revert to the default server."""
    import subprocess
    try:
        d = request.get_json(force=True) or {}
        module = str(d.get("module") or "")
        card = str(d.get("card") or "main")
        if module not in _CARD_ASSIGNABLE:
            return Response(json.dumps({
                "ok": False, "error": "module not card-assignable: %r" % module}),
                status=400, mimetype='application/json')
        if card not in ("main", "default"):
            # never pass an arbitrary string to a root helper -- match a real card.
            names = {c["name"] for c in _alsa_cards()}
            if card not in names:
                return Response(json.dumps({
                    "ok": False, "error": "no such card: %s" % card}),
                    status=400, mimetype='application/json')
        r = subprocess.run(["sudo", _ASSIGN_HELPER, module, card],
                           capture_output=True, text=True, timeout=45)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            return Response(json.dumps({
                "ok": False, "module": module, "card": card,
                "error": (r.stderr or out or "assign failed").strip()[:400]}),
                status=500, mimetype='application/json')
        return Response(json.dumps({"ok": True, "module": module, "card": card,
                                    "log": out[-400:]}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ----------------------------------------------------------- SAVED PATCH ---
# /patch was a LIVE patchbay only. Every wire made here died at the next power
# cycle, because nothing wrote any of them down -- and the page still looked
# persistent, because the wires that DID come back came back from somewhere
# else entirely: the synth's own auto-connect, and the Fire's route restore
# from ~/orc1_routing.json. So the honest description of the old behaviour was
# "the Fire grid is the persistent patchbay and this one is not", which is not
# what anybody reading this page would have guessed.
#
# THE ONE RULE THAT MATTERS: never prune the saved list from an empty or
# unreadable graph. jackd being down looks precisely like "every port you saved
# has gone away", and acting on that reading would delete the patch at exactly
# the moment the rig is broken. That is the phantom-reading failure this
# project has now hit three times, so it is a guard, not a comment: every prune
# path below is gated on having actually enumerated a non-empty graph.
#
# THE SECOND RULE, WHICH COST A REDESIGN: THIS SUBSYSTEM MUST NEVER FIGHT
# ANOTHER ONE. The first version re-asserted every saved wire on every pass,
# forever. It was reviewed as working, and it was -- at the cost of silently
# undoing the teardowns of every OTHER surface that wires this graph:
#
#   * The Fire ROUTE grid runs jack_disconnect when you turn a pad off. The pad
#     goes dark, ~/orc1_routing.json is updated, the OLED agrees -- and this
#     loop reconnected the same ports within ten seconds. Audio kept flowing
#     with every indicator saying it should not. That is this project's
#     defining bug, merely inverted: a control that reports success while the
#     graph disagrees.
#   * /skipback's source change disconnects the old source and connects the
#     new. This loop re-added the old one, so the ring recorded BOTH, summed,
#     while the page reported only the new -- on the always-on safety net.
#
# So a wire that goes away while BOTH ITS PORTS ARE STILL THERE was cut on
# purpose by somebody, and is left alone. The distinction that matters is not
# who cut it -- which is unknowable from here -- but whether the PORTS
# survived:
#
#   ports vanished and came back  -> a restart. Nobody chose that. Restore it.
#   ports never went away         -> a deliberate cut. Hand off. Do not fight.
#
# On the one ambiguous case -- a client that restarts entirely between two
# passes, so the gap is never observed -- it does NEITHER. It does not
# re-assert (which would fight) and it does not delete (which would lose your
# patch). It marks the wire CUT, shows it on the page, and waits for you to
# press LOAD. Choosing "tell the player" over guessing is the whole design.
#
# (A port UUID would have distinguished the two exactly. It does not: jackd
# reuses the same uuid for a restarted client's port -- measured on this rig
# with jack_metro, same uuid 4294967333 across a stop/start, not assumed.)

PATCH_FILE = os.path.expanduser("~/.orc1_patch.json")

# How long a saved wire's port may be MISSING before the entry is dropped.
# owner's call: a saved wire referencing something that is no longer there
# should go away rather than linger forever.
#
# This was 5 minutes and that was wrong. Of the wires on this rig, five point
# at system:playback_3/4 (which exist only on the L-12) and two at pisound-*
# (a hat that gets unplugged). Switching cards from the CARDS section of this
# very page, or pulling the hat, would have permanently deleted a third of the
# patch after five minutes -- while the same routes in ~/orc1_routing.json
# survive indefinitely. A day means "this hardware is actually gone", not "you
# are mid-soundcheck". It is measured in WALL CLOCK and stored in the file, so
# it is real elapsed absence rather than orc1-web uptime.
PATCH_STALE_SECS = 86400

# THIS INTERVAL IS A CORRECTNESS BOUND, NOT A TASTE SETTING, AND 10s WAS WRONG.
#
# The restore rule reads a restart as "the ports went away and came back". If
# the poll never SEES the gap, a restart is indistinguishable from somebody
# cutting the wire, and the wire is left cut instead of being put back -- which
# is the feature's headline claim silently not happening.
#
# Measured on this rig: `systemctl restart orc1` takes ORC1-Synth's ports out
# of the graph for ~4.2s (sampled at 5Hz across a real restart). At a 10s poll
# that gap was MISSED and the saved wire was wrongly marked cut -- observed,
# not theorised. A poll period shorter than the gap observes it by pigeonhole,
# so 2s catches any absence of ~2.2s or longer with better than 2x margin.
#
# Cost, measured rather than guessed: jack_lsp is 28.5ms of CPU per call and a
# pass makes two, so 2s = ~2.7% of ONE core = ~0.7% of this 4-core Pi, and only
# while something is actually saved. For scale, the alsa bridges are 12.3% EACH.
#
# RESIDUAL, stated honestly: a client that restarts in under ~2.2s is still
# ambiguous. It is marked `cut` and shown on the page rather than guessed at,
# and LOAD puts it back.
PATCH_RECONCILE_SECS = 2

_patch_lock = threading.RLock()       # this process is the only writer
_patch_seen_up = set()                # wires observed connected this process
_patch_last_error = {}                # (src,dst) -> last error, to not spam
_patch_last_pass = [None]             # monotonic time of the last completed pass


def _patch_run(tool, a, b):
    """Run jack_connect/jack_disconnect. None on success, else the error text.

    "already connected" / "already disconnected" ARE the desired end state, so
    they count as success -- matching jack's own wording, which was verified by
    running the tool rather than guessed at. Reconcile idempotency rests on
    this clause: without it every pass would report a failure for wires that
    are already exactly as they should be.
    """
    import subprocess
    try:
        r = subprocess.run([tool, a, b], capture_output=True, timeout=8)
    except Exception as e:
        return str(e)
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if r.returncode == 0 or "already" in err.lower():
        return None
    return err or "rc=%d" % r.returncode


def _patch_entry(src, dst, cut=False, missing_since=None):
    return {"src": src, "dst": dst, "cut": bool(cut),
            "missing_since": missing_since}


def _patch_saved_load():
    """The saved wires as a list of entries. [] when there is no file yet.

    Accepts the original v1 shape (a list of [src,dst] pairs) as well as v2
    entries, so a file written before the cut/missing_since fields existed
    still loads instead of being read as "you have no saved patch".

    A file that will not parse is MOVED ASIDE rather than silently treated as
    empty, so the next write cannot quietly cement the loss and so the UI has
    something true to report.
    """
    try:
        with open(PATCH_FILE) as f:
            d = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        bad = PATCH_FILE + ".bad"
        try:
            os.replace(PATCH_FILE, bad)
            print("[patch] saved patch was unreadable (%s) -- moved to %s"
                  % (e, bad), flush=True)
        except Exception:
            print("[patch] saved patch is unreadable (%s)" % e, flush=True)
        return []
    rows, seen = [], set()
    for c in (d.get("connections") if isinstance(d, dict) else None) or []:
        # Anything malformed is dropped rather than carried, so a bad entry
        # cannot reach jack_connect as a half-name.
        if isinstance(c, dict):
            src, dst, cut = c.get("src"), c.get("dst"), c.get("cut")
            ms = c.get("missing_since")
            ms = ms if isinstance(ms, (int, float)) else None
        elif isinstance(c, (list, tuple)) and len(c) == 2:
            src, dst, cut, ms = c[0], c[1], False, None
        else:
            continue
        if not (isinstance(src, str) and isinstance(dst, str)
                and ":" in src and ":" in dst):
            continue
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        rows.append(_patch_entry(src, dst, cut, ms))
    return rows


def _patch_saved_write(rows):
    """Replace the saved list. Atomic -- this file is written on every edit.

    A plain open(path,"w") truncates and then streams; an interruption there
    leaves an unparseable file, which _patch_saved_load() reads as "no saved
    patch" and the next write cements. That is exactly what bit
    _save_routing_file() one file over. The directory is fsynced as well as
    the file, so a hard power cut cannot leave the rename un-durable.
    """
    seen, out = set(), []
    for c in rows:
        e = c if isinstance(c, dict) else _patch_entry(c[0], c[1])
        if (e["src"], e["dst"]) in seen:
            continue
        seen.add((e["src"], e["dst"]))
        out.append(_patch_entry(e["src"], e["dst"], e.get("cut"),
                                e.get("missing_since")))
    tmp = PATCH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 2, "connections": out}, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATCH_FILE)
    try:
        dirfd = os.open(os.path.dirname(PATCH_FILE) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        pass                       # best effort; the file itself is intact
    return out


def _patch_saved_pairs(rows=None):
    """Just the [src,dst] pairs -- what the page needs to mark saved wires."""
    rows = _patch_saved_load() if rows is None else rows
    return [[e["src"], e["dst"]] for e in rows]


def _patch_saved_add(pairs):
    """Record intent. An existing entry is re-armed rather than duplicated."""
    with _patch_lock:
        rows = _patch_saved_load()
        have = {(e["src"], e["dst"]): e for e in rows}
        for p in pairs:
            t = (p[0], p[1])
            if t in have:
                # Re-making a wire you had cut means you want it again.
                have[t]["cut"] = False
                have[t]["missing_since"] = None
            else:
                rows.append(_patch_entry(p[0], p[1]))
        return _patch_saved_write(rows)


def _patch_saved_remove(pairs):
    with _patch_lock:
        gone = {(p[0], p[1]) for p in pairs}
        for t in gone:
            _patch_seen_up.discard(t)
            _patch_last_error.pop(t, None)
        return _patch_saved_write([e for e in _patch_saved_load()
                                   if (e["src"], e["dst"]) not in gone])


def _patch_reconcile(prune=True, connect=True, force=False):
    """Re-establish saved wires that a RESTART took away.

    Not wires that somebody deliberately cut -- see the second rule at the top
    of this section. `force` is the explicit LOAD button: it clears the cut
    marks first, because pressing LOAD is the player saying "yes, re-apply it".

    Returns a report naming every wire in each outcome, because "it restored
    your patch" with no list is indistinguishable from "it did nothing".
    """
    with _patch_lock:
        rep = {"ok": True, "done": [], "failed": [], "pending": [],
               "dropped": [], "cut": [], "saved": []}
        saved = _patch_saved_load()
        rep["saved"] = _patch_saved_pairs(saved)
        if not saved:
            return rep                      # nothing saved: never touch jackd

        ports = _patch_ports()
        if not ports:
            # See the FIRST rule above. No graph means no information, not an
            # empty graph -- so no prune, no connect, and say so out loud.
            rep["ok"] = False
            rep["error"] = ("jackd is not reachable -- the saved patch was "
                            "left untouched")
            rep["pending"] = _patch_saved_pairs(saved)
            return rep

        live = {p["name"] for p in ports}
        conns = {tuple(c) for c in _patch_conns(ports)}
        now = time.time()
        keep, changed = [], False
        for e in saved:
            t = (e["src"], e["dst"])
            if force and e["cut"]:
                e["cut"] = False
                changed = True
            if t[0] in live and t[1] in live:
                if e["missing_since"] is not None:
                    e["missing_since"] = None
                    changed = True
                keep.append(e)
                if t in conns:
                    _patch_seen_up.add(t)
                    if e["cut"]:
                        e["cut"] = False    # it is up again; the cut is over
                        changed = True
                    continue
                if t in _patch_seen_up:
                    # We watched this wire go from up to down while both its
                    # ports stayed put. Nothing but a deliberate disconnect
                    # does that. Hand off rather than fight.
                    _patch_seen_up.discard(t)
                    if not e["cut"]:
                        e["cut"] = True
                        changed = True
                        print("[patch] %s -> %s was disconnected elsewhere -- "
                              "leaving it cut, not re-asserting it"
                              % (t[0], t[1]), flush=True)
                    rep["cut"].append(list(t))
                    continue
                if e["cut"]:
                    rep["cut"].append(list(t))
                    continue            # already handed off; do not re-fight
                if not connect:
                    rep["pending"].append(list(t))
                    continue
                err = _patch_run("jack_connect", t[0], t[1])
                if err is None:
                    rep["done"].append(list(t))
                    _patch_seen_up.add(t)
                    _patch_last_error.pop(t, None)
                else:
                    rep["failed"].append({"src": t[0], "dst": t[1],
                                          "error": err})
                    if _patch_last_error.get(t) != err:
                        print("[patch] could not restore %s -> %s: %s"
                              % (t[0], t[1], err), flush=True)
                    _patch_last_error[t] = err
                continue
            # A port is absent. The ports going away is what tells us a
            # restart happened, so forget that we ever saw this wire up --
            # that is what makes it eligible for restoring when they return.
            _patch_seen_up.discard(t)
            if e["missing_since"] is None:
                e["missing_since"] = now
                changed = True
            elif e["missing_since"] > now:
                # This Pi has no RTC and NTP corrects the clock after boot, so
                # a stamp from the future is a clock jump, not a paradox.
                e["missing_since"] = now
                changed = True
            age = now - e["missing_since"]
            if prune and age >= PATCH_STALE_SECS:
                rep["dropped"].append(list(t))
                _patch_last_error.pop(t, None)
                changed = True
                print("[patch] dropped saved wire %s -> %s: port absent for "
                      "%dh" % (t[0], t[1], int(age / 3600)), flush=True)
            else:
                keep.append(e)
                rep["pending"].append(list(t))
        # Only write when something actually changed. An idle pass on a
        # healthy rig must not rewrite this file every ten seconds -- that is
        # gratuitous SD-card wear on a headless instrument.
        if changed:
            _patch_saved_write(keep)
        rep["saved"] = _patch_saved_pairs(keep)
        rep["ok"] = not rep["failed"]
        return rep


def _patch_reconcile_thread():
    """Keep the saved patch true, in the background.

    This is the half that makes the feature real rather than merely written
    down. A cold boot brings jackd, the synth and the bridges up over several
    seconds, so a one-shot replay at web start would fire into a half-empty
    graph and silently restore half the patch; and restarting orc1-synth
    destroys every one of its connections at any time (the Fire's route
    restore has the same problem and only runs on an orc1 restart). Looping
    covers both without either one being a special case.

    Costs nothing when nothing is saved: _patch_reconcile() returns before it
    runs jack_lsp at all if the file is empty, which is the shipped default.
    """
    last_err = [None]
    while True:
        try:
            _patch_reconcile()
            _patch_last_pass[0] = time.monotonic()
            last_err[0] = None
        except Exception as e:
            # Deduped: an unreachable jack_lsp would otherwise be 8,640
            # identical journal lines a day, forever, on an SD card.
            if last_err[0] != str(e):
                print("[patch] reconcile pass failed: %s" % e, flush=True)
                last_err[0] = str(e)
        # Outside the try on purpose: a raising pass must still sleep, or a
        # persistent failure becomes a hot loop.
        time.sleep(PATCH_RECONCILE_SECS)


@app.route('/api/patch/saved')
def api_patch_saved():
    """The saved patch, and which of it is currently live. Read-only."""
    saved = _patch_saved_load()
    ports = _patch_ports()
    live = {p["name"] for p in ports}
    conns = {tuple(c) for c in _patch_conns(ports)} if ports else set()
    rows = []
    for e in saved:
        t = (e["src"], e["dst"])
        if not ports:
            # "unknown" is not "missing": with no graph we cannot tell, and
            # saying "missing" here would be inventing a fact about hardware.
            state, why = "unknown", ""
        elif t in conns:
            state, why = "live", ""
        elif t[0] not in live or t[1] not in live:
            state, why = "missing", ""
        elif e["cut"]:
            state, why = "cut", ""
        elif t in _patch_last_error:
            # A restore that fails every pass must not render as a friendly
            # "waiting" forever -- that is a control claiming it is about to
            # work while it never will.
            state, why = "failing", _patch_last_error[t]
        else:
            state, why = "waiting", ""
        rows.append({"src": t[0], "dst": t[1], "state": state, "error": why})
    age = (None if _patch_last_pass[0] is None
           else round(time.monotonic() - _patch_last_pass[0], 1))
    return Response(json.dumps({
        "ok": True, "connections": rows,
        "stale_secs": PATCH_STALE_SECS,
        "reconcile_secs": PATCH_RECONCILE_SECS,
        # If the background thread ever dies, everything else here keeps
        # rendering perfectly -- which is the phantom-UP shape this project
        # has hit three times. Publish its heartbeat so it is checkable.
        "last_pass_age": age}), mimetype='application/json')


@app.route('/api/patch/saved', methods=['POST'])
def api_patch_saved_set():
    """save = snapshot the live graph; load = apply the file; reset = forget."""
    try:
        d = request.get_json(force=True) or {}
        op = d.get("op")
        if op == "save":
            with _patch_lock:
                ports = _patch_ports()
                if not ports:
                    # Snapshotting an unreachable graph would save an empty
                    # patch over a good one and call it success.
                    return Response(json.dumps({
                        "ok": False,
                        "error": "jackd is not reachable -- nothing to save, "
                                 "and the existing saved patch was left "
                                 "alone"}),
                        status=503, mimetype='application/json')
                rows = _patch_saved_write(
                    [_patch_entry(c[0], c[1]) for c in _patch_conns(ports)])
                _patch_seen_up.clear()
                _patch_last_error.clear()
            return Response(json.dumps({"ok": True, "op": op,
                                        "count": len(rows),
                                        "connections": _patch_saved_pairs(rows)}),
                            mimetype='application/json')
        if op == "load":
            rep = _patch_reconcile(prune=True, connect=True, force=True)
            rep["op"] = op
            return Response(json.dumps(rep), mimetype='application/json',
                            status=200 if rep.get("ok") else 409)
        if op == "reset":
            with _patch_lock:
                _patch_saved_write([])
                _patch_seen_up.clear()
                _patch_last_error.clear()
            # The live graph is deliberately NOT touched: RESET forgets the
            # saved patch, it does not unplug the rig.
            return Response(json.dumps({"ok": True, "op": op, "count": 0,
                                        "connections": []}),
                            mimetype='application/json')
        if op == "forget":
            a, b = d.get("src"), d.get("dst")
            if not isinstance(a, str) or not isinstance(b, str):
                return Response(json.dumps({"ok": False,
                                            "error": "need src and dst"}),
                                status=400, mimetype='application/json')
            with _patch_lock:
                rows = _patch_saved_remove([[a, b]])
            # Deliberately does NOT disconnect: this is how you stop a wire
            # coming back WITHOUT silencing it in the middle of playing.
            return Response(json.dumps({"ok": True, "op": op,
                                        "count": len(rows)}),
                            mimetype='application/json')
        return Response(json.dumps({
            "ok": False,
            "error": "op must be save, load, reset or forget"}),
            status=400, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


# ------------------------------------------------------------------ CARDS ---
# jackd owns exactly ONE card. Everything else physically plugged into the Pi
# is invisible to JACK -- and therefore to the patchbay, to skipback, to all of
# it -- until a bridge process pulls it in. This box has ten ALSA cards and
# JACK could see one and a half of them, which is why /patch looked like a
# fixed menu: it was faithfully showing a graph that was itself a small subset
# of the hardware.
#
# Cards that already have PURPOSE-BUILT units keep them. The Perform-VE pair in
# particular has a load-bearing ordering requirement -- the device emits
# digital silence on capture unless a playback stream is already open, and
# alsa_in does NOT error in that state -- so performve-in waits for
# performve-out's ports. A generic bridge would lose that and record silence.
_CARD_UNITS = {
    "pisound":   {"in": "pisound-in",   "out": "pisound-out"},
    "PerformVE": {"in": "performve-in", "out": "performve-out"},
}

# Not worth offering as audio I/O on this box. Listed anyway (never hidden --
# hiding is how a tool starts deciding for you), just flagged so the useful
# ones are obvious.
_CARD_MINOR = {"vc4hdmi0", "vc4hdmi1", "Loopback", "Headphones"}


def _alsa_cards():
    """Every ALSA card present, with measured channel counts where readable."""
    import re as _re
    cards = []
    try:
        txt = open("/proc/asound/cards").read()
    except Exception:
        return cards
    for m in _re.finditer(r"^\s*(\d+)\s+\[([^\]]+)\]", txt, _re.M):
        idx, name = int(m.group(1)), m.group(2).strip()
        cards.append({"index": idx, "name": name,
                      "play_ch": None, "cap_ch": None,
                      "has_play": False, "has_cap": False})
    have_play, have_cap = set(), set()
    for tool, sink in (("aplay", have_play), ("arecord", have_cap)):
        try:
            out = _jack([tool, "-l"])
            for l in out.splitlines():
                mm = _re.match(r"^card (\d+):", l)
                if mm:
                    sink.add(int(mm.group(1)))
        except Exception:
            pass
    for c in cards:
        c["has_play"] = c["index"] in have_play
        c["has_cap"] = c["index"] in have_cap
        # Channel counts come from the DEVICE where it exposes them. Where it
        # does not (non-USB cards have no stream0) the count is reported as
        # null and the UI says "unknown" rather than inventing a number.
        try:
            st = open("/proc/asound/card%d/stream0" % c["index"]).read()
        except Exception:
            continue
        sect = None
        for line in st.splitlines():
            s = line.strip()
            if s.startswith("Playback:"):
                sect = "play_ch"
            elif s.startswith("Capture:"):
                sect = "cap_ch"
            elif s.endswith(":") and " " not in s and sect:
                # Any OTHER bare section header ends the one we were reading.
                # Without this, a third header would leave `sect` pointing at
                # the previous section and mis-attribute its Channels: lines.
                sect = None
            elif "Channels:" in s and sect:
                try:
                    n = int(s.split(":")[1].strip())
                    if n > (c[sect] or 0):
                        c[sect] = n
                except Exception:
                    pass
    return cards


def _bridge_unit(card, direction):
    ded = _CARD_UNITS.get(card, {}).get(direction)
    return ded or ("orc1-bridge@%s.%s" % (card, direction))


def _proc_cpu():
    """{jack client name: cpu%} for running bridge processes."""
    out, res = _jack(["ps", "-eo", "pcpu,args"]), {}
    for l in out.splitlines():
        if "alsa_in" not in l and "alsa_out" not in l:
            continue
        parts = l.split()
        try:
            cpu = float(parts[0])
        except Exception:
            continue
        if "-j" in parts:
            try:
                res[parts[parts.index("-j") + 1]] = cpu
            except Exception:
                pass
    return res


@app.route('/api/patch/cards')
def api_patch_cards():
    graph_clients = {p["name"].split(":", 1)[0] for p in _patch_ports()}
    cpu = _proc_cpu()
    jack_card = _patch_card()
    rows = []
    for c in _alsa_cards():
        entry = {"name": c["name"], "index": c["index"],
                 "minor": c["name"] in _CARD_MINOR,
                 "is_jackd": c["name"] == jack_card, "dirs": {}}
        for direction, present, ch in (("in", c["has_cap"], c["cap_ch"]),
                                       ("out", c["has_play"], c["play_ch"])):
            if not present:
                continue
            client = "%s-%s" % (c["name"].lower(), direction)
            if c["name"] == jack_card:
                # jackd's own card appears as "system", not as a bridge.
                entry["dirs"][direction] = {
                    "client": "system", "in_jack": "system" in graph_clients,
                    "channels": ch, "unit": None, "cpu": None, "owned": True}
                continue
            unit = _bridge_unit(c["name"], direction)
            ded = _CARD_UNITS.get(c["name"], {}).get(direction)
            cname = ded if ded else client
            entry["dirs"][direction] = {
                "client": cname, "in_jack": cname in graph_clients,
                "channels": ch, "unit": unit, "cpu": cpu.get(cname),
                "owned": False}
        if entry["dirs"]:
            rows.append(entry)
    return Response(json.dumps({"ok": True, "jackd_card": jack_card,
                                "cards": rows}),
                    mimetype='application/json')


@app.route('/api/patch/cards', methods=['POST'])
def api_patch_cards_set():
    """Bring a card into the JACK graph, or drop it."""
    import subprocess
    try:
        d = request.get_json(force=True) or {}
        card = str(d.get("card") or "")
        direction = str(d.get("dir") or "")
        op = str(d.get("op") or "")
        if direction not in ("in", "out") or op not in ("start", "stop"):
            return Response(json.dumps({"ok": False, "error": "bad request"}),
                            status=400, mimetype='application/json')
        # Only cards that actually exist, matched by name against
        # /proc/asound/cards -- never a string straight from the request.
        names = {c["name"] for c in _alsa_cards()}
        if card not in names:
            return Response(json.dumps({
                "ok": False, "error": "no such card: %s" % card}),
                status=400, mimetype='application/json')
        unit = _bridge_unit(card, direction)
        r = subprocess.run(["sudo", "systemctl", op, unit],
                           capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            return Response(json.dumps({
                "ok": False, "unit": unit,
                "error": (r.stderr or r.stdout or "").strip()[:300]}),
                status=500, mimetype='application/json')

        # systemctl exiting 0 is NOT evidence the card reached the graph.
        # pisound-out, for one, deliberately idles (`sleep infinity`) when
        # jackd already owns hw:pisound -- so `start` succeeds, the unit reads
        # active, and nothing is bridged. Reporting that as "brought in" is
        # exactly the reports-success-while-wiring-nothing failure this whole
        # page exists to end. Verify against the live graph instead.
        import time as _t
        ded = _CARD_UNITS.get(card, {}).get(direction)
        cname = ded if ded else "%s-%s" % (card.lower(), direction)
        present = False
        for _ in range(12):                       # bridges take a moment
            _t.sleep(0.5)
            present = cname in {p["name"].split(":", 1)[0]
                                for p in _patch_ports()}
            if present == (op == "start"):
                break
        if op == "start" and not present:
            return Response(json.dumps({
                "ok": False, "unit": unit, "client": cname,
                "error": "the unit started but '%s' never appeared in the "
                         "JACK graph -- check journalctl -u %s"
                         % (cname, unit)}),
                status=500, mimetype='application/json')
        return Response(json.dumps({"ok": True, "unit": unit, "op": op,
                                    "client": cname, "in_jack": present}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype='application/json')


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"ORC-1 web UI → http://0.0.0.0:{port}")
    # Started here rather than at import so that anything importing this module
    # for testing does not silently acquire a thread that rewires live audio.
    threading.Thread(target=_patch_reconcile_thread, daemon=True,
                     name="patch-reconcile").start()
    # Re-apply a saved Perform-VE insert once the bridge is up (best-effort; the
    # bridge units start independently, so wait briefly rather than race them).
    def _pve_restore_thread():
        if not _pve_insert_load()["on"]:
            return
        time.sleep(8)                # let jackd settle before starting a bridge
        _pve_insert_restore()        # starts the bridge on demand, then wires
    threading.Thread(target=_pve_restore_thread, daemon=True,
                     name="pve-insert-restore").start()
    app.run(host='0.0.0.0', port=port, debug=False)
