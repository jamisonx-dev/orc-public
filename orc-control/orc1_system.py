#!/usr/bin/env python3
"""
ORC-1 System Dashboard  v1
Pi health monitor — CPU, memory, temperature, disk, uptime, power.
Reboot and shutdown controls with confirmation.
Runs at http://pibox:5002

INSTALL:
  sudo cp orc1-system.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable orc1-system
  sudo systemctl start orc1-system

NOTE: Reboot and shutdown require passwordless sudo for the service user.
Add to /etc/sudoers (sudo visudo):
  pibox ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown

AUTH: /system/reboot and /system/shutdown require a shared-secret token,
sent as "Authorization: Bearer <token>" or JSON body {"token": "..."}.
Token source: env ORC1_SYSTEM_TOKEN, else ~/.orc1_system_token, else a
nothing else: there is NO built-in default, so with neither set, reboot
and shutdown are refused until you create a token. The dashboard UI
prompts for the token once and stores it in localStorage.
"""

import json
import os
import subprocess
import time
from flask import Flask, Response, request

app = Flask(__name__)

# ── Auth token for reboot/shutdown ───────────────────────────────────────────
# Resolution order: env var ORC1_SYSTEM_TOKEN → ~/.orc1_system_token file.
# There is no default. To enable reboot/shutdown, create the token file:
#   echo "my-secret" > ~/.orc1_system_token && chmod 600 ~/.orc1_system_token

# No default token on purpose: with none configured, SYSTEM_TOKEN is None
# and reboot/shutdown are refused. Create ~/.orc1_system_token to enable.

def _load_token():
    tok = os.environ.get("ORC1_SYSTEM_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(os.path.expanduser("~/.orc1_system_token")) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except Exception:
        pass
    return None   # nothing configured -> reboot/shutdown stay locked

SYSTEM_TOKEN = _load_token()

def _authorized(req):
    """Accept token via 'Authorization: Bearer <tok>' header or JSON body {"token": ...}."""
    if not SYSTEM_TOKEN:
        return False
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip() == SYSTEM_TOKEN:
        return True
    try:
        data = req.get_json(force=True, silent=True) or {}
        if data.get("token") == SYSTEM_TOKEN:
            return True
    except Exception:
        pass
    return False

# ── System data collection ────────────────────────────────────────────────────

def _read(path, default="0"):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default

def _run(cmd, default=""):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL,
                                       timeout=2).decode().strip()
    except Exception:
        return default

# CPU usage — non-blocking /proc/stat delta sampling.
# Each call diffs against the sample taken on the previous call, so steady
# polling (the dashboard polls every 3s) gets an accurate reading with no
# sleeping at all. The very first call takes one quick 0.1s two-point
# sample. Result is cached for 2s to absorb poll bursts.
_cpu_sample = {"idle": None, "total": None, "pct": 0, "ts": 0.0}

def _read_proc_stat():
    with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:]))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    return idle, sum(vals)

def _cpu_percent():
    now = time.time()
    if _cpu_sample["idle"] is not None and now - _cpu_sample["ts"] < 2.0:
        return _cpu_sample["pct"]
    try:
        prev_idle, prev_total = _cpu_sample["idle"], _cpu_sample["total"]
        idle, total = _read_proc_stat()
        if prev_idle is None or total <= prev_total:
            prev_idle, prev_total = idle, total
            time.sleep(0.1)
            idle, total = _read_proc_stat()
        dt  = total - prev_total
        pct = int(round(100.0 * (1.0 - (idle - prev_idle) / dt))) if dt > 0 else 0
        _cpu_sample.update(idle=idle, total=total,
                           pct=max(0, min(100, pct)), ts=now)
    except Exception:
        pass
    return _cpu_sample["pct"]

def get_stats():
    # ── Temperature ───────────────────────────────────────────────────────────
    raw_temp = _read("/sys/class/thermal/thermal_zone0/temp", "0")
    temp_c   = int(raw_temp) / 1000.0

    # ── CPU usage ─────────────────────────────────────────────────────────────
    cpu_pct = _cpu_percent()

    # ── Memory ────────────────────────────────────────────────────────────────
    meminfo = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                meminfo[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass
    mem_total = meminfo.get("MemTotal", 0)
    mem_avail = meminfo.get("MemAvailable", 0)
    mem_used  = mem_total - mem_avail
    mem_pct   = int(mem_used / mem_total * 100) if mem_total else 0

    # ── Disk ──────────────────────────────────────────────────────────────────
    try:
        st       = os.statvfs("/")
        disk_tot = st.f_blocks * st.f_frsize
        disk_avl = st.f_bavail * st.f_frsize
        disk_use = disk_tot - disk_avl
        disk_pct = int(disk_use / disk_tot * 100)
    except Exception:
        disk_tot = disk_avl = disk_use = disk_pct = 0

    # ── Uptime ────────────────────────────────────────────────────────────────
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days    = int(secs // 86400)
        hours   = int((secs % 86400) // 3600)
        minutes = int((secs % 3600) // 60)
        uptime  = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    except Exception:
        uptime = "—"

    # ── Throttle / power flags (Pi-specific) ──────────────────────────────────
    throttle_raw = _run("vcgencmd get_throttled 2>/dev/null", "throttled=0x0")
    try:
        throttle_val = int(throttle_raw.split("=")[1], 16)
    except Exception:
        throttle_val = 0

    flags = []
    if throttle_val & 0x1:   flags.append("UNDERVOLT NOW")
    if throttle_val & 0x2:   flags.append("FREQ CAP NOW")
    if throttle_val & 0x4:   flags.append("THROTTLED NOW")
    if throttle_val & 0x10000: flags.append("UNDERVOLT OCCURRED")
    if throttle_val & 0x20000: flags.append("FREQ CAP OCCURRED")
    if throttle_val & 0x40000: flags.append("THROTTLED OCCURRED")
    throttle_ok = len(flags) == 0

    # ── Services ──────────────────────────────────────────────────────────────
    services = {}
    # skipback-bridge retired at the multitrack Phase-1 cutover (2026-07)  --
    # the recorder behind skipback-record subsumes it. Monitoring a retired
    # unit here is the 07-17 phantom-DOWN bug class; don't re-add it.
    # orc1-jackd added 2026-07-29: it is the SOLE jackd owner, so it is the
    # single point of total-audio failure. Without it here the page could show
    # every service green while there was no sound at all.
    # orc1-synth was RETIRED 2026-09-24 (the six module hosts replaced it);
    # it stayed in this list and showed DOWN forever. The hosts are listed
    # instead. While DAW/DJ mode has them stopped on purpose (daw_mode.sh
    # engines-off marker) they report "daw", which the page shows as OFF
    # (DAW/DJ), not DOWN -- truthy, so the compact card doesn't count it down.
    daw_off = os.path.exists("/run/orc1-daw-engines-off")
    hosts = ["orc1-drum", "orc1-sampler", "orc1-poly", "orc1-chip",
             "orc1-mono", "orc1-bbkit"]
    for name in ["orc1", "orc1-jackd"] + hosts + ["orc1-web",
                                                  "skipback-record"]:
        # `|| true`: is-active exits non-zero for a stopped unit, and _run
        # discards the output on a non-zero exit -- we need the word
        # ("inactive" = stopped cleanly, vs "failed")
        out = _run(f"systemctl is-active {name} || true")
        up = out == "active"
        services[name] = ("daw" if (not up and daw_off and name in hosts
                                    and out == "inactive") else up)

    # ── Recent recordings ─────────────────────────────────────────────────────
    # Two bugs fixed 2026-07-29:
    #  1. This filtered ".mp3" only, but skipback grabs have defaulted to WAV
    #     since 2026-07-17 (sox just wraps the raw ring PCM in a header --
    #     effectively free, where lame competed with the RT audio engine). So
    #     every grab made since then was invisible here and the card showed
    #     only stale mp3s. Extension tuple mirrors orc1_web.py's _AUDIO_EXTS,
    #     matched case-insensitively.
    #  2. It sorted FILENAMES reverse=True, not mtime. Byte-wise
    #     "MAIN_60s..." > "MAIN_300s...", so "recent" was misordered even
    #     within a single track name. Sort by st_mtime instead.
    recs = []
    rec_dir = os.path.expanduser("~/recordings")
    _exts = (".wav", ".mp3", ".flac", ".ogg", ".aif", ".aiff")
    try:
        cands = []
        for fn in os.listdir(rec_dir):
            if not fn.lower().endswith(_exts):
                continue
            fp = os.path.join(rec_dir, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue        # vanished between listdir and stat
            cands.append((st.st_mtime, fn, st.st_size))
        cands.sort(reverse=True)                     # newest first, by mtime
        for _mt, fn, size in cands[:5]:
            recs.append({"name": fn, "size_kb": size // 1024})
    except Exception:
        pass

    return {
        "timestamp":   time.time(),
        "temp_c":      round(temp_c, 1),
        "cpu_pct":     cpu_pct,
        "mem_total_mb": mem_total // 1024,
        "mem_used_mb":  mem_used  // 1024,
        "mem_pct":     mem_pct,
        "disk_total_gb": round(disk_tot / 1e9, 1),
        "disk_used_gb":  round(disk_use / 1e9, 1),
        "disk_pct":    disk_pct,
        "uptime":      uptime,
        "throttle_ok": throttle_ok,
        "throttle_flags": flags,
        "services":    services,
        "recordings":  recs,
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0a0a">
<title>ORC-1 SYSTEM</title>
<style>
/*__ORC_FONTS__*/
:root {
  --bg:#0a0a0a; --panel:#111; --border:#1e1e1e; --dim:#2a2a2a;
  --muted:#444; --label:#556; --body:#8a9aaa; --bright:#c8dae8;
  --accent:#00c8a0; --accent2:#e05a00; --warn:#e0a000;
  --on:#00e87a; --off:#1a1a1a; --red:#e03030;
  --font-seg:'Share Tech Mono',monospace;
  --font-head:'Orbitron',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--body);font-family:var(--font-seg);
  font-size:14px;min-height:100dvh;overflow-x:hidden}
body::after{content:'';position:fixed;inset:0;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,
  rgba(0,0,0,0.07) 2px,rgba(0,0,0,0.07) 4px);
  pointer-events:none;z-index:1000}

.shell{max-width:480px;margin:0 auto;padding:10px 10px 40px;
  display:flex;flex-direction:column;gap:8px}

.hdr{display:flex;align-items:center;justify-content:space-between;
  padding:8px 10px 6px;border-bottom:1px solid var(--border);margin-bottom:2px}
.logo{font-family:var(--font-head);font-weight:900;font-size:17px;
  letter-spacing:.15em;color:var(--bright)}
.logo span{color:var(--accent)}
.hdr-right{display:flex;align-items:center;gap:8px}
.age{font-size:9px;color:var(--muted);letter-spacing:.05em}
.dot{width:8px;height:8px;border-radius:50%;background:var(--off);
  transition:background .4s,box-shadow .4s}
.dot.live{background:var(--on);box-shadow:0 0 8px rgba(0,232,122,.4)}
.dot.stale{background:var(--warn)}
.dot.dead{background:var(--red)}

.card{background:var(--panel);border:1px solid var(--border);
  border-radius:3px;padding:9px 11px}
.card-label{font-family:var(--font-head);font-size:8px;font-weight:700;
  letter-spacing:.25em;color:var(--label);margin-bottom:8px}
.card,.card{transition:border-color .15s}
.flash{border-color:rgba(0,200,160,.35) !important}

/* ── Gauge bars ── */
.gauge-row{display:flex;flex-direction:column;gap:6px}
.gauge{margin-bottom:2px}
.gauge-header{display:flex;justify-content:space-between;
  align-items:baseline;margin-bottom:4px}
.gauge-name{font-size:9px;color:var(--label);letter-spacing:.15em;
  font-family:var(--font-head)}
.gauge-val{font-size:13px;color:var(--bright);font-family:var(--font-seg)}
.gauge-val .unit{font-size:9px;color:var(--muted);margin-left:2px}
.bar-track{height:4px;background:var(--dim);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .6s ease,background .6s}
.bar-ok{background:var(--accent)}
.bar-warn{background:var(--warn)}
.bar-crit{background:var(--red)}

/* ── Temp display ── */
.temp-card{display:flex;align-items:center;gap:16px}
.temp-big{font-family:var(--font-head);font-size:48px;font-weight:900;
  line-height:1;letter-spacing:-.02em;color:var(--bright);
  transition:color .4s}
.temp-big.warm{color:var(--warn)}
.temp-big.hot{color:var(--red)}
.temp-unit{font-size:18px;color:var(--muted);font-family:var(--font-head)}
.temp-sub{font-size:9px;color:var(--label);letter-spacing:.1em;
  font-family:var(--font-head);margin-top:4px}
.throttle-ok{font-size:10px;color:var(--on)}
.throttle-warn{font-size:9px;color:var(--warn);line-height:1.6}

/* ── Services ── */
.svc-grid{display:flex;flex-direction:column;gap:4px}
.svc-row{display:flex;justify-content:space-between;align-items:center;
  padding:4px 0;border-bottom:1px solid var(--border)}
.svc-row:last-child{border-bottom:none}
.svc-name{font-size:11px;color:var(--body);font-family:var(--font-seg)}
.svc-pill{font-size:8px;font-family:var(--font-head);font-weight:700;
  letter-spacing:.1em;padding:2px 7px;border-radius:2px}
.svc-pill.up{color:var(--on);background:rgba(0,232,122,.08);
  border:1px solid rgba(0,232,122,.3)}
.svc-pill.down{color:var(--red);background:rgba(224,48,48,.08);
  border:1px solid rgba(224,48,48,.3)}

/* ── Recordings ── */
.rec-list{display:flex;flex-direction:column;gap:3px}
.rec-row{display:flex;justify-content:space-between;align-items:center;
  padding:3px 0;border-bottom:1px solid var(--border);font-size:10px}
.rec-row:last-child{border-bottom:none}
.rec-name{color:var(--body);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:75%}
.rec-size{color:var(--muted);flex-shrink:0}
.rec-empty{font-size:10px;color:var(--muted);font-style:italic}

/* ── Control buttons ── */
.controls{display:flex;gap:6px}
.ctrl-btn{flex:1;font-family:var(--font-head);font-size:10px;font-weight:700;
  letter-spacing:.08em;padding:10px 8px;border-radius:3px;
  border:1px solid var(--dim);color:var(--muted);background:var(--off);
  cursor:pointer;-webkit-tap-highlight-color:transparent;
  transition:all .15s;user-select:none;-webkit-user-select:none}
.ctrl-btn:active{transform:scale(.95)}
.btn-reboot{color:var(--warn);border-color:rgba(224,160,0,.4);
  background:rgba(224,160,0,.06)}
.btn-reboot:active{background:rgba(224,160,0,.15)}
.btn-shutdown{color:var(--red);border-color:rgba(224,48,48,.4);
  background:rgba(224,48,48,.06)}
.btn-shutdown:active{background:rgba(224,48,48,.15)}

/* ── Confirm overlay ── */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);
  z-index:500;align-items:center;justify-content:center}
.overlay.show{display:flex}
.confirm-box{background:var(--panel);border:1px solid var(--dim);
  border-radius:4px;padding:24px 20px;max-width:300px;width:90%;
  text-align:center}
.confirm-title{font-family:var(--font-head);font-size:13px;font-weight:700;
  letter-spacing:.1em;color:var(--bright);margin-bottom:8px}
.confirm-msg{font-size:11px;color:var(--body);margin-bottom:20px;
  line-height:1.6}
.confirm-btns{display:flex;gap:8px}
.confirm-btn{flex:1;font-family:var(--font-head);font-size:10px;font-weight:700;
  letter-spacing:.08em;padding:10px;border-radius:3px;cursor:pointer;
  -webkit-tap-highlight-color:transparent;transition:all .15s;
  user-select:none;-webkit-user-select:none}
.confirm-cancel{color:var(--muted);background:var(--off);border:1px solid var(--dim)}
.confirm-cancel:active{color:var(--bright)}
.confirm-go.reboot{color:var(--warn);background:rgba(224,160,0,.1);
  border:1px solid rgba(224,160,0,.4)}
.confirm-go.shutdown{color:var(--red);background:rgba(224,48,48,.1);
  border:1px solid rgba(224,48,48,.4)}
.confirm-go:active{filter:brightness(1.4)}

/* ── Uptime chip ── */
.uptime-chip{font-size:11px;color:var(--accent);font-family:var(--font-seg)}

.footer{text-align:center;font-size:8px;color:var(--dim);
  letter-spacing:.12em;margin-top:8px;font-family:var(--font-head)}
</style>
</head>
<body>
<div class="shell">
  <div class="hdr">
    <div class="logo">ORC<span>-1</span> &nbsp;SYS</div>
    <div class="hdr-right">
      <span class="age" id="age">—</span>
      <div class="dot" id="dot"></div>
    </div>
  </div>
  <div id="content"></div>
  <div class="footer">RASPBERRY PI 4B — PISOUND — MEMPHIS TN</div>
</div>

<!-- Confirm overlay -->
<div class="overlay" id="overlay">
  <div class="confirm-box">
    <div class="confirm-title" id="conf-title">CONFIRM</div>
    <div class="confirm-msg" id="conf-msg"></div>
    <div class="confirm-btns">
      <button class="confirm-btn confirm-cancel" onclick="closeConfirm()">CANCEL</button>
      <button class="confirm-btn confirm-go" id="conf-go" onclick="doAction()">CONFIRM</button>
    </div>
  </div>
</div>

<script>
const REFRESH_MS = 3000;
let pendingAction = null;

function barClass(pct, warnAt=70, critAt=90) {
  if (pct >= critAt) return 'bar-crit';
  if (pct >= warnAt) return 'bar-warn';
  return 'bar-ok';
}

function gauge(name, valHtml, pct, warnAt=70, critAt=90) {
  const cls = barClass(pct, warnAt, critAt);
  return `
  <div class="gauge">
    <div class="gauge-header">
      <span class="gauge-name">${name}</span>
      <span class="gauge-val">${valHtml}</span>
    </div>
    <div class="bar-track">
      <div class="bar-fill ${cls}" style="width:${pct}%"></div>
    </div>
  </div>`;
}

function render(d) {
  const tempClass = d.temp_c >= 80 ? 'hot' : d.temp_c >= 65 ? 'warm' : '';
  const tempCol   = d.temp_c >= 80 ? 'var(--red)' : d.temp_c >= 65 ? 'var(--warn)' : 'var(--bright)';

  const throttleHtml = d.throttle_ok
    ? `<div class="throttle-ok">✓ POWER NOMINAL</div>`
    : d.throttle_flags.map(f => `<div class="throttle-warn">⚠ ${f}</div>`).join('');

  const svcs = d.services || {};
  const svcHtml = Object.entries(svcs).map(([name, up]) => `
    <div class="svc-row">
      <span class="svc-name">${name}</span>
      <span class="svc-pill ${up ? 'up' : 'down'}">${up ? 'RUNNING' : 'DOWN'}</span>
    </div>`).join('');

  const recs = d.recordings || [];
  const recHtml = recs.length
    ? recs.map(r => `
      <div class="rec-row">
        <span class="rec-name">${r.name}</span>
        <span class="rec-size">${r.size_kb}KB</span>
      </div>`).join('')
    : `<div class="rec-empty">no recordings yet</div>`;

  return `
  <div class="card">
    <div class="card-label">TEMPERATURE &nbsp;&amp;&nbsp; POWER</div>
    <div class="temp-card">
      <div>
        <div class="temp-big ${tempClass}" style="color:${tempCol}">${d.temp_c.toFixed(1)}</div>
        <div class="temp-sub">°C &nbsp; CPU TEMP</div>
      </div>
      <div style="flex:1">${throttleHtml}</div>
    </div>
  </div>

  <div class="card">
    <div class="card-label" style="display:flex;justify-content:space-between">
      <span>RESOURCES</span>
      <span class="uptime-chip">UP ${d.uptime}</span>
    </div>
    <div class="gauge-row">
      ${gauge('CPU', `${d.cpu_pct}<span class="unit">%</span>`, d.cpu_pct, 70, 90)}
      ${gauge('RAM',
        `${d.mem_used_mb}<span class="unit">MB / ${d.mem_total_mb}MB</span>`,
        d.mem_pct, 75, 90)}
      ${gauge('DISK',
        `${d.disk_used_gb}<span class="unit">GB / ${d.disk_total_gb}GB</span>`,
        d.disk_pct, 70, 85)}
    </div>
  </div>

  <div class="card">
    <div class="card-label">SERVICES</div>
    <div class="svc-grid">${svcHtml}</div>
  </div>

  <div class="card">
    <div class="card-label">RECENT RECORDINGS</div>
    <div class="rec-list">${recHtml}</div>
  </div>

  <div class="card">
    <div class="card-label">SYSTEM CONTROLS</div>
    <div class="controls">
      <button class="ctrl-btn btn-reboot"
        onclick="askConfirm('reboot')">⟳ &nbsp;REBOOT</button>
      <button class="ctrl-btn btn-shutdown"
        onclick="askConfirm('shutdown')">⏻ &nbsp;SHUTDOWN</button>
    </div>
  </div>`;
}

function askConfirm(action) {
  pendingAction = action;
  const isReboot = action === 'reboot';
  document.getElementById('conf-title').textContent = isReboot ? 'REBOOT?' : 'SHUTDOWN?';
  document.getElementById('conf-msg').textContent = isReboot
    ? 'Pi will restart. All services will stop and resume automatically.'
    : 'Pi will power down. Manual power cycle required to restart.';
  const go = document.getElementById('conf-go');
  go.textContent = isReboot ? 'REBOOT' : 'SHUTDOWN';
  go.className = `confirm-btn confirm-go ${action}`;
  document.getElementById('overlay').classList.add('show');
}

function closeConfirm() {
  pendingAction = null;
  document.getElementById('overlay').classList.remove('show');
}

async function doAction() {
  if (!pendingAction) return;
  const action = pendingAction;
  closeConfirm();
  let token = localStorage.getItem('orc1_token') || '';
  if (!token) {
    token = (prompt('System token:') || '').trim();
    if (!token) return;
    localStorage.setItem('orc1_token', token);
  }
  try {
    const res = await fetch('/system/' + action, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (res.status === 401) {
      localStorage.removeItem('orc1_token');
      alert('Invalid token — try again.');
    }
  } catch(e) {}
}

function flashCards() {
  document.querySelectorAll('.card').forEach(el => {
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 200);
  });
}

async function poll() {
  try {
    const res  = await fetch('/stats?' + Date.now());
    const data = await res.json();
    const age  = Date.now()/1000 - (data.timestamp||0);
    const dot  = document.getElementById('dot');
    const ageEl= document.getElementById('age');
    dot.className = age < 6 ? 'dot live' : age < 30 ? 'dot stale' : 'dot dead';
    ageEl.textContent = age < 4 ? 'LIVE' : Math.round(age) + 's';
    document.getElementById('content').innerHTML = render(data);
    flashCards();
  } catch(e) {
    document.getElementById('dot').className = 'dot dead';
    document.getElementById('age').textContent = 'OFFLINE';
  }
  setTimeout(poll, REFRESH_MS);
}

poll();
</script>
</body>
</html>"""



# Fonts are SELF-HOSTED (2026-09-23): the remote Google @import blocked render and
# hung this page on an offline rig. This app has no static route, so the two
# SIL-OFL fonts from orc1/static/fonts are embedded as data URIs once at startup
# (~34 KB). Missing files -> the marker is dropped and the CSS falls back to
# monospace, never an error.
def _embedded_fonts_css():
    import base64
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "orc1", "static", "fonts")
    faces = (("Orbitron", "400 900", "orbitron-latin.woff2"),
             ("Share Tech Mono", "400", "sharetechmono-latin.woff2"))
    css = []
    for fam, wt, fn in faces:
        try:
            with open(os.path.join(d, fn), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            continue
        css.append("@font-face{font-family:'%s';font-style:normal;font-weight:%s;"
                   "font-display:swap;src:url(data:font/woff2;base64,%s) "
                   "format('woff2')}" % (fam, wt, b64))
    return "".join(css)


PAGE = PAGE.replace("/*__ORC_FONTS__*/", _embedded_fonts_css())

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return Response(PAGE, mimetype='text/html')

@app.route('/stats')
def stats():
    return Response(json.dumps(get_stats()), mimetype='application/json')

@app.route('/system/reboot', methods=['POST'])
def reboot():
    if not _authorized(request):
        return Response('{"ok":false,"error":"unauthorized"}', status=401,
                        mimetype='application/json')
    subprocess.Popen(['sudo', 'reboot'])
    return Response('{"ok":true}', mimetype='application/json')

@app.route('/system/shutdown', methods=['POST'])
def shutdown():
    if not _authorized(request):
        return Response('{"ok":false,"error":"unauthorized"}', status=401,
                        mimetype='application/json')
    subprocess.Popen(['sudo', 'shutdown', '-h', 'now'])
    return Response('{"ok":true}', mimetype='application/json')


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5002
    print(f"ORC-1 System Dashboard → http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
