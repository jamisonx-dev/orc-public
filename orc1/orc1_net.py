#!/usr/bin/env python3
"""orc1_net.py -- the ORC "rescue" page on :5003: WiFi (scan / join / forget / hotspot) and FILES.

A separate tiny Flask service (orc1-net.service) so it still works when orc1-web is down,
and so the WiFi card on /system can simply embed it. It answers only to trusted sources
(see orc1_nettrust.py: the Pi itself, its own hotspot, and the LAN while joined
to a home network); everyone else gets a 403. Every network change goes through
orc1_netroot.py (sudo), which keeps passwords out of argv and rolls a failed join back.
Files: read-only browse + download of a few whitelisted roots, never dot-files.
"""
import json
import mimetypes
import os
import subprocess
import sys
import time

from flask import Flask, Response, abort, jsonify, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_nettrust as T          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_HELPER = ["sudo", "-n", "/usr/bin/python3", os.path.join(HERE, "orc1_netroot.py")]
NETMODE = os.path.join(HERE, "orc1_netmode.sh")
HOME = "/home/pibox"
FILE_ROOTS = {"recordings": HOME + "/recordings", "orc": HOME + "/orc",
              "backups": HOME + "/backups", "home": HOME}

app = Flask(__name__)


@app.before_request
def gate():
    cls = T.source_class(request.remote_addr or "")
    if cls is None:
        return Response(
            "403: the Pi only accepts this page from its own hotspot (pibox, 172.24.1.1), "
            "or a home network it is joined to.\n", status=403, mimetype="text/plain")
    request.environ["orc.srcclass"] = cls


def root(cmd, payload=None, timeout=90):
    try:
        p = subprocess.run(ROOT_HELPER + [cmd], input=json.dumps(payload or {}), capture_output=True,
                           text=True, timeout=timeout)
        return json.loads(p.stdout or "{}") or {"ok": False, "error": p.stderr[:200]}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def sh(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def status():
    ap = "pb-hotspot" in sh(["nmcli", "-t", "-f", "NAME", "con", "show", "--active"]).splitlines()
    conn = T.active_wifi_connection()
    ips = {}
    for ifn in ("wlan0", "eth0", "bnep0"):
        v = sh(["ip", "-4", "-o", "addr", "show", ifn])
        if v:
            ips[ifn] = v.split()[3].split("/")[0]
    inet = subprocess.run(["ping", "-c1", "-W2", "1.1.1.1"], capture_output=True).returncode == 0
    res = None
    try:
        with open("/run/orc1_net_result.json") as f:
            res = json.load(f)
    except Exception:
        pass
    return {"mode": "hotspot" if ap else "client", "connection": conn, "ips": ips,
            "internet": inet, "result": res, "at_home": T.at_home(),
            "you": request.environ.get("orc.srcclass"), "now": time.time()}


@app.route("/api/status")
def api_status():
    return jsonify(status())


@app.route("/api/known")
def api_known():
    return jsonify(root("known"))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    return jsonify(root("scan", {"rescan": bool((request.get_json(silent=True) or {}).get("rescan"))}))


@app.route("/api/join", methods=["POST"])
def api_join():
    d = request.get_json(silent=True) or {}
    return jsonify(root("join", {k: d.get(k) for k in ("ssid", "psk", "low", "hidden")}))


@app.route("/api/forget", methods=["POST"])
def api_forget():
    d = request.get_json(silent=True) or {}
    return jsonify(root("forget", {"name": d.get("name")}))


@app.route("/api/trust", methods=["POST"])
def api_trust():
    d = request.get_json(silent=True) or {}
    return jsonify(root("trust", {"name": d.get("name"), "on": d.get("on")}))


@app.route("/api/ap_psk", methods=["POST"])
def api_ap_psk():
    d = request.get_json(silent=True) or {}
    return jsonify(root("ap_psk", {"psk": d.get("psk")}))


@app.route("/api/mode", methods=["POST"])
def api_mode():
    m = (request.get_json(silent=True) or {}).get("mode")
    if m not in ("ap", "client"):
        return jsonify({"ok": False, "error": "mode must be ap or client"})
    # detached: switching drops the link the answer would travel on
    subprocess.Popen(["sudo", "-n", "systemd-run", "--no-block", "--collect", NETMODE, m],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True, "started": m})


# ---- files -----------------------------------------------------------------------------

def _resolve(rootkey, rel):
    base = FILE_ROOTS.get(rootkey)
    if not base:
        abort(404)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        abort(403)
    full = os.path.realpath(os.path.join(base, *parts))
    if full != os.path.realpath(base) and not full.startswith(os.path.realpath(base) + os.sep):
        abort(403)
    # a symlink may leave the root only to land somewhere that is not hidden either
    if any(p.startswith(".") for p in os.path.relpath(full, "/").split(os.sep)):
        abort(403)
    return full


@app.route("/api/files")
def api_files():
    rk, rel = request.args.get("root", "recordings"), request.args.get("path", "")
    full = _resolve(rk, rel)
    if not os.path.isdir(full):
        abort(404)
    rows = []
    for n in os.listdir(full):
        if n.startswith("."):
            continue
        try:
            st = os.stat(os.path.join(full, n))
        except OSError:
            continue
        rows.append({"name": n, "dir": os.path.isdir(os.path.join(full, n)), "size": st.st_size,
                     "mtime": st.st_mtime})
    rows.sort(key=lambda r: (not r["dir"], -r["mtime"]) if rk == "recordings" else (not r["dir"], r["name"].lower()))
    return jsonify({"root": rk, "path": rel, "rows": rows[:2000], "roots": list(FILE_ROOTS)})


@app.route("/dl")
def dl():
    full = _resolve(request.args.get("root", ""), request.args.get("path", ""))
    if not os.path.isfile(full):
        abort(404)
    inline = request.args.get("inline") == "1"
    return send_file(full, as_attachment=not inline, conditional=True,
                     mimetype=mimetypes.guess_type(full)[0] or "application/octet-stream")


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORC-1 network + files</title>
<style>
:root{--bg:#0f1115;--fg:#e8e8e8;--dim:#8a8f98;--card:#181b21;--line:#2b303a;--acc:#4fd1c5;--bad:#f56565;--ok:#68d391}
@media (prefers-color-scheme: light){:root{--bg:#f4f5f7;--fg:#1a1d23;--dim:#5b6270;--card:#fff;--line:#d5d9e0;--acc:#0b7d73;--bad:#c53030;--ok:#2f855a}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
body.embed{background:transparent}
.wrap{max-width:720px;margin:0 auto;padding:12px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0}
h2{font-size:12px;letter-spacing:.1em;color:var(--dim);margin:0 0 8px;font-weight:600}
button,input,select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 10px}
button{cursor:pointer}button.pri{background:var(--acc);color:#000;border-color:var(--acc)}
button.bad{color:var(--bad)}input{width:100%;box-sizing:border-box;margin:4px 0}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:6px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.dim{color:var(--dim);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}
a{color:var(--acc)}.sp{display:flex;gap:8px;flex-wrap:wrap}
</style></head><body><div class="wrap">
<div id="hdr"><b>ORC-1</b> network + files <a href="/" style="float:right" id="full" hidden>open full page</a></div>

<div class="card" id="st"><h2>LINK</h2><div id="stbody" class="dim">loading...</div>
 <div class="sp" style="margin-top:8px"><button onclick="mode('ap')">START HOTSPOT</button><button onclick="mode('client')">JOIN BEST KNOWN WIFI</button></div>
 <div class="dim" style="margin-top:6px">Either switch drops this page for ~20 s: reconnect your phone to the new network and reload.</div></div>

<div class="card"><h2>JOIN A WIFI</h2>
 <div id="scanmsg" class="dim"></div><div id="nets"></div>
 <button onclick="scan(true)">RESCAN</button>
 <div style="margin-top:8px"><input id="ssid" placeholder="network name (SSID)" autocapitalize="off" autocomplete="off">
 <input id="psk" type="password" placeholder="password (typed here, never sent anywhere else)" autocomplete="new-password">
 <label class="dim"><input type="checkbox" id="low" style="width:auto"> low priority (use for a phone hotspot)</label>
 <label class="dim"><input type="checkbox" id="hid" style="width:auto"> hidden network</label>
 <div><button class="pri" onclick="join()">JOIN</button></div>
 <div id="joinmsg" class="dim" style="margin-top:6px"></div></div></div>

<div class="card"><h2>SAVED NETWORKS</h2><div id="known" class="dim">loading...</div>
 <div class="dim">"Home" = the LAN is trusted for settings and files while joined to it. Leave it off for anything that is not yours.</div></div>

<div class="card"><h2>HOTSPOT PASSWORD</h2><input id="appsk" type="password" placeholder="new hotspot password (8-63 characters)" autocomplete="new-password">
 <button onclick="appsk()">CHANGE</button> <span id="apmsg" class="dim"></span></div>

<div class="card"><h2>FILES</h2><div class="sp" id="roots"></div><div id="path" class="dim" style="margin:6px 0"></div><div id="files"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const embed=location.search.includes('embed=1');
if(embed){document.body.classList.add('embed');$('hdr').hidden=true;}
async function J(u,b){const r=await fetch(u,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function status(){try{const s=await J('/api/status');
 const ips=Object.entries(s.ips).map(([k,v])=>k+' '+v).join(' · ');
 let h='<b>'+(s.mode==='hotspot'?'HOTSPOT "pibox" (172.24.1.1)':'WiFi: '+esc(s.connection||'(none)'))+'</b> · internet <span class="'+(s.internet?'ok':'bad')+'">'+(s.internet?'yes':'no')+'</span><br><span class="dim">'+esc(ips)+'<br>you are on: '+s.you+'</span>';
 if(s.result&&Date.now()/1000-s.result.ts<900)h+='<br><span class="'+(s.result.ok?'ok':'bad')+'">last join: '+esc(s.result.message)+'</span>';
 $('stbody').innerHTML=h;}catch(e){$('stbody').textContent='status unavailable (changing network?)';}}
async function scan(r){$('scanmsg').textContent=r?'scanning...':'';const d=await J('/api/scan',{rescan:r});
 $('scanmsg').textContent=d.cached?'(hotspot is up: showing the list from before it started; rescan briefly drops the hotspot)':'';
 $('nets').innerHTML=(d.nets||[]).map(n=>'<div class="row"><span>'+esc(n.ssid)+(n.inuse?' ✓':'')+' <span class="dim">'+n.signal+'% '+esc(n.sec)+'</span></span><button onclick="pick(this.dataset.s)" data-s="'+esc(n.ssid)+'">PICK</button></div>').join('')||'<div class="dim">none found</div>';}
function pick(s){$('ssid').value=s;$('psk').focus();}
async function join(){const ssid=$('ssid').value.trim(),psk=$('psk').value;if(!ssid)return;
 if(!confirm('Join "'+ssid+'"? If this page goes away, reconnect your phone to the new network (or back to the pibox hotspot if it fails).'))return;
 const d=await J('/api/join',{ssid,psk,low:$('low').checked,hidden:$('hid').checked});
 $('psk').value='';
 $('joinmsg').innerHTML=d.ok?'<span class="ok">joining... this can take up to a minute. If the page drops, reconnect to the right network and reload. A failed join rolls back by itself.</span>':'<span class="bad">'+esc(d.error)+'</span>';
 if(d.ok){let n=0;const t=setInterval(()=>{status();known();if(++n>30)clearInterval(t);},3000);}}
async function known(){const d=await J('/api/known');
 $('known').innerHTML=(d.nets||[]).map(n=>'<div class="row"><span>'+esc(n.name)+' <span class="dim">'+n.kind+'</span></span><span>'+
  (n.kind==='hotspot'?'':'<label class="dim"><input type="checkbox" style="width:auto" '+(n.trusted?'checked ':'')+'onchange="trust(this.dataset.n,this.checked)" data-n="'+esc(n.name)+'"> home</label> <button class="bad" onclick="forget(this.dataset.n)" data-n="'+esc(n.name)+'">FORGET</button>')+'</span></div>').join('')||'none';}
async function forget(n){if(!confirm('Forget "'+n+'"? Its password is deleted from the Pi.'))return;const d=await J('/api/forget',{name:n});if(!d.ok)alert(d.error);known();}
async function trust(n,on){await J('/api/trust',{name:n,on});status();}
async function mode(m){if(!confirm(m==='ap'?'Start the hotspot? You will be disconnected from the current WiFi.':'Leave the hotspot and join the best known WiFi?'))return;
 await J('/api/mode',{mode:m}).catch(()=>{});$('stbody').textContent='switching...';}
async function appsk(){const p=$('appsk').value;const d=await J('/api/ap_psk',{psk:p});$('appsk').value='';
 $('apmsg').innerHTML=d.ok?'<span class="ok">saved'+(d.active?' (takes effect the next time the hotspot starts)':'')+'</span>':'<span class="bad">'+esc(d.error)+'</span>';}
let root='recordings',cur='';
async function files(){const d=await J('/api/files?root='+encodeURIComponent(root)+'&path='+encodeURIComponent(cur));
 $('roots').innerHTML=d.roots.map(r=>'<button '+(r===root?'class="pri" ':'')+'onclick="root=\''+r+'\';cur=\'\';files()">'+r+'</button>').join('');
 $('path').textContent='/'+root+'/'+cur;
 let h=cur?'<div class="row"><a href="#" onclick="cur=cur.split(\'/\').slice(0,-1).join(\'/\');files();return false">.. (up)</a></div>':'';
 h+=d.rows.map(r=>{const p=(cur?cur+'/':'')+r.name;
  return '<div class="row"><span>'+(r.dir?'<a href="#" onclick="cur=this.dataset.p;files();return false" data-p="'+esc(p)+'">'+esc(r.name)+'/</a>':'<a href="/dl?root='+root+'&path='+encodeURIComponent(p)+'">'+esc(r.name)+'</a>')+'</span><span class="dim">'+(r.dir?'':(r.size/1048576).toFixed(1)+' MB · ')+new Date(r.mtime*1000).toLocaleString()+'</span></div>';}).join('');
 $('files').innerHTML=h||'<div class="dim">empty</div>';}
status();known();scan(false);files();setInterval(status,8000);
if(embed)$('full').hidden=true;else $('full').hidden=false;
</script></body></html>"""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5003
    print("ORC-1 network page -> http://0.0.0.0:%d" % port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
