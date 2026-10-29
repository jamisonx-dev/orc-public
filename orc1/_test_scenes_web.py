#!/usr/bin/env python3
"""
SCENES web side + the PLAY / CHORD split (2026-10-26).

Covers:
  - orc1_web._cmd_request: the full-reply reader for the brain's command socket
    (reads to the newline across chunks, deadline, size cap, not-JSON, brain
    down) and that the old 64-byte _send_cmd_socket still works.
  - /api/scenes* + /api/settings/read: each relays exactly the right SCENE_*
    action to the brain, validates slot / timing / ids first, and turns a dead
    or silent brain into a clear error.
  - /api/scenes/settings: only sceneable entries; the web process NEVER builds
    the registry (no cache -> "settings list loading").
  - the pages: PLAY = tempo + scenes + MORE, CHORD exists and is in the nav and
    orc1_health's WEB_PAGES, skipback status on /skipback, RESTART ORC on
    /system, and every action PLAY used to send is still sent from somewhere.

Safe anywhere: HOME is a temp dir BEFORE orc1_web is imported, so the command
socket path is a fake brain inside it; nothing reaches the live brain. Needs
AF_UNIX (run on the Pi / Linux). Flask test client only.

Mutation-prove: make _cmd_request stop at the first recv -> "long reply";
drop the slot check in api_scene_recall -> "slot 0 refused"; let the web build
the registry (set_build_allowed(True)) -> "web never builds".
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EXPECTED = 60
_p = _f = 0


def check(cond, label, extra=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {label} {extra}")


# ── sandbox first: HOME (and so ~/.orc1_cmd.sock) is a temp dir ──────────────
SANDBOX = tempfile.mkdtemp(prefix="orc_scenes_web_")
os.environ["HOME"] = SANDBOX

import orc1_web as W            # noqa: E402  (side-effect free at import)

check(W.CMD_SOCK.startswith(SANDBOX), "command socket path is inside the sandbox", W.CMD_SOCK)


# ── a fake brain on the sandbox socket ───────────────────────────────────────
class FakeBrain:
    def __init__(self, path):
        self.path = path
        self.got = []
        self.reply = lambda payload: (json.dumps({"ok": True}) + "\n").encode()
        self.chunk = 0          # 0 = send in one go, else bytes per send
        self.delay = 0.0        # before replying
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(3.0)
                buf = b""
                while True:
                    c = conn.recv(4096)
                    if not c:
                        break
                    buf += c
                line = buf.split(b"\n", 1)[0]
                payload = json.loads(line.decode()) if line.strip() else None
                self.got.append(payload)
                if self.delay:
                    time.sleep(self.delay)
                out = self.reply(payload)
                if self.chunk:
                    for i in range(0, len(out), self.chunk):
                        conn.sendall(out[i:i + self.chunk])
                        time.sleep(0.001)
                else:
                    conn.sendall(out)
            except Exception:
                pass
            finally:
                conn.close()

    def stop(self):
        self.srv.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


# ── brain down ───────────────────────────────────────────────────────────────
try:
    W._cmd_request({"action": "SCENE_LIST"}, timeout=1.0)
    check(False, "brain down raises")
except (FileNotFoundError, ConnectionRefusedError):
    check(True, "brain down raises")
cl = W.app.test_client()
r = cl.get("/api/scenes")
check(r.status_code == 503 and r.get_json() == {"ok": False, "error": "the ORC is not running"},
      "brain down -> 503 'the ORC is not running'", r.get_data(as_text=True)[:200])

B = FakeBrain(W.CMD_SOCK)

# ── the full-reply reader ────────────────────────────────────────────────────
big = {"ok": True, "slots": {str(n): {"name": f"s{n}", "timing": "NEXT_BAR",
                                      "values": {f"x.{i}": i for i in range(200)}}
                             for n in range(1, 61)}, "status": {"pending": None}}
big_line = (json.dumps(big, separators=(",", ":")) + "\n").encode()
check(len(big_line) > 100_000, "test reply is big (>100 KB)", len(big_line))
B.reply = lambda p: big_line
B.chunk = 7000
try:
    got = W._cmd_request({"action": "SCENE_LIST"})
except Exception as e:
    got = repr(e)
check(got == big, "long reply in many chunks is read whole")
check(B.got[-1] == {"action": "SCENE_LIST"}, "the command arrives as one JSON line", B.got[-1])

B.chunk = 0
B.reply = lambda p: b'{"ok":true,"a":1}\n{"ok":false}\n'
check(W._cmd_request({"action": "X"}) == {"ok": True, "a": 1}, "only the first line is the reply")

B.reply = lambda p: b'{"ok":true,"no_newline":1}'
check(W._cmd_request({"action": "X"}) == {"ok": True, "no_newline": 1},
      "a reply closed without a newline is still read")

B.reply = lambda p: b"not json\n"
try:
    W._cmd_request({"action": "X"})
    check(False, "not-JSON reply raises CmdReplyError")
except W.CmdReplyError:
    check(True, "not-JSON reply raises CmdReplyError")

B.reply = lambda p: b"\n"
try:
    W._cmd_request({"action": "X"})
    check(False, "empty reply raises CmdReplyError")
except W.CmdReplyError:
    check(True, "empty reply raises CmdReplyError")

B.reply = lambda p: big_line
try:
    W._cmd_request({"action": "X"}, max_bytes=50_000)
    check(False, "a reply over the cap raises")
except W.CmdReplyError as e:
    check("too long" in str(e), "a reply over the cap raises", str(e))

B.reply = lambda p: b'{"ok":true}\n'
B.delay = 1.2
t0 = time.monotonic()
try:
    W._cmd_request({"action": "X"}, timeout=0.5)
    check(False, "a slow brain times out")
except W.CmdReplyError as e:
    check("in time" in str(e) and time.monotonic() - t0 < 1.1, "a slow brain times out (deadline kept)", str(e))
r = None
time.sleep(0.9)                     # let the slow connection finish
B.delay = 0.0

B.reply = lambda p: b'{"ok":true}\n'
check(W._send_cmd_socket({"action": "PANIC"}) is True, "old 64-byte ack path still works")
B.reply = lambda p: b'{"ok":false}\n'
check(W._send_cmd_socket({"action": "PANIC"}) is False, "old ack path still reads a refusal")
check(W.write_cmd("TAP_TEMPO") is False and B.got[-1] == {"action": "TAP_TEMPO"},
      "write_cmd still goes through the socket", B.got[-1])

# ── /api/scenes routes relay the right action ────────────────────────────────
ECHO = lambda p: (json.dumps({"ok": True, "echo": p}) + "\n").encode()   # noqa: E731
B.reply = ECHO


def last():
    return B.got[-1]


r = cl.get("/api/scenes")
check(r.status_code == 200 and last() == {"action": "SCENE_LIST"}, "GET /api/scenes -> SCENE_LIST")

r = cl.post("/api/scenes/5/capture", json={"ids": ["tempo.bpm", "poly.cc74"], "merge": True})
check(r.get_json().get("ok") and last() == {"action": "SCENE_CAPTURE", "slot": 5,
                                            "ids": ["tempo.bpm", "poly.cc74"], "merge": True},
      "capture relays slot, ids, merge", last())
n0 = len(B.got)
r = cl.post("/api/scenes/5/capture", json={"ids": []})
check(r.status_code == 400 and len(B.got) == n0, "capture with nothing ticked refused before the brain")
r = cl.post("/api/scenes/5/capture", json={"ids": "tempo.bpm"})
check(r.status_code == 400 and len(B.got) == n0, "capture ids must be a list")

r = cl.post("/api/scenes/7/recall", json={})
check(last() == {"action": "SCENE_RECALL", "slot": 7}, "recall without timing = the scene's own", last())
r = cl.post("/api/scenes/7/recall", json={"timing": "BARS_4"})
check(last() == {"action": "SCENE_RECALL", "slot": 7, "timing": "BARS_4"}, "recall with a timing override")
n0 = len(B.got)
r = cl.post("/api/scenes/7/recall", json={"timing": "SOON"})
check(r.status_code == 400 and len(B.got) == n0, "unknown timing refused")
r = cl.post("/api/scenes/0/recall", json={})
check(r.status_code == 400 and len(B.got) == n0, "slot 0 refused")
r = cl.post("/api/scenes/129/recall", json={})
check(r.status_code == 400 and len(B.got) == n0, "slot 129 refused")
r = cl.post("/api/scenes/128/recall", json={})
check(r.status_code == 200 and last()["slot"] == 128, "slot 128 allowed from the web")

r = cl.post("/api/scenes/3", json={"name": "V" * 60, "timing": "IMMEDIATE",
                                   "values": {"tempo.bpm": 120}, "merge": True, "remove": ["a.b"]})
check(last() == {"action": "SCENE_SET", "slot": 3, "name": "V" * 40, "timing": "IMMEDIATE",
                 "values": {"tempo.bpm": 120}, "merge": True, "remove": ["a.b"]},
      "SET relays name (cut to 40), timing, values, merge, remove", last())
r = cl.post("/api/scenes/3", json={"values": {"x": None}})
check(last() == {"action": "SCENE_SET", "slot": 3, "values": {"x": None}},
      "SET: a ticked-but-unknown value (null) passes through, no merge key", last())
r = cl.post("/api/scenes/3", json={"name": ""})
check(last() == {"action": "SCENE_SET", "slot": 3, "name": ""}, "SET can clear the name")
n0 = len(B.got)
check(cl.post("/api/scenes/3", json={"values": [1]}).status_code == 400 and len(B.got) == n0,
      "SET values must be an object")
check(cl.post("/api/scenes/3", json={"remove": "x"}).status_code == 400 and len(B.got) == n0,
      "SET remove must be a list")
check(cl.post("/api/scenes/3", json={"timing": "LATER"}).status_code == 400 and len(B.got) == n0,
      "SET unknown timing refused")

r = cl.post("/api/scenes/9/clear")
check(last() == {"action": "SCENE_CLEAR", "slot": 9}, "clear relays SCENE_CLEAR")
check(cl.get("/api/scenes/9/clear").status_code == 405, "clear is POST only")

r = cl.post("/api/settings/read", json={"ids": ["tempo.bpm"]})
check(last() == {"action": "SETTINGS_READ", "ids": ["tempo.bpm"]}, "settings/read relays SETTINGS_READ")
n0 = len(B.got)
check(cl.post("/api/settings/read", json={}).status_code == 400 and len(B.got) == n0,
      "settings/read needs a list")

B.reply = lambda p: b'{"ok":false,"error":"settings list loading"}\n'
r = cl.post("/api/scenes/4/capture", json={"ids": ["tempo.bpm"]})
check(r.status_code == 200 and r.get_json() == {"ok": False, "error": "settings list loading"},
      "the brain's refusal is passed through unchanged")
B.reply = lambda p: b"garbage\n"
r = cl.get("/api/scenes")
check(r.status_code == 504 and r.get_json()["ok"] is False, "a garbled brain reply -> 504 with ok:false")

# ── the settings list: web never builds it ───────────────────────────────────
R = W._settings_reg
check(R._BUILD_ALLOWED is False, "web process switched registry building off")
check(not os.path.exists(os.path.expanduser(R.CACHE_FILE)), "sandbox has no registry cache")
r = cl.get("/api/scenes/settings")
j = r.get_json()
check(j == {"ok": False, "loading": True, "error": "settings list loading"},
      "no cache -> 'settings list loading' (never built here)", j)
check(R._CACHE is None, "web never builds the registry")

_real_registry = R.registry
FAKE = [
    {"id": "tempo.bpm", "thing": "Tempo", "label": "Tempo (BPM)", "type": "float", "range": [20, 300],
     "choices": None, "default": None, "set": {}, "read": {"kind": "state"}, "sceneable": True, "order": 20},
    {"id": "gear.mpc_prog", "thing": "Outside gear > MPC", "label": "MPC program", "type": "int",
     "range": [0, 127], "choices": None, "default": None, "set": {}, "read": None, "sceneable": True},
    {"id": "wire.x", "thing": "Wiring", "label": "A wire", "type": "str", "range": None,
     "choices": None, "default": None, "set": {}, "read": {"kind": "json"}, "sceneable": False},
]
R.registry = lambda refresh=False: list(FAKE)
j = cl.get("/api/scenes/settings").get_json()
ids = [e["id"] for e in j.get("settings", [])]
check(j.get("ok") and ids == ["tempo.bpm", "gear.mpc_prog"], "only sceneable settings listed, in order", ids)
by = {e["id"]: e for e in j.get("settings", [])}
check(by.get("tempo.bpm", {}).get("readable") is True and by.get("gear.mpc_prog", {}).get("readable") is False,
      "readable = the ORC can read it back")
check(by.get("gear.mpc_prog", {}).get("thing") == "Outside gear > MPC" and by["tempo.bpm"]["range"] == [20, 300],
      "thing / range carried for the editor")
check("set" not in by.get("tempo.bpm", {}) and "read" not in by.get("tempo.bpm", {}),
      "internals (set / read) not sent to the page")
R.registry = _real_registry

# ── pages ────────────────────────────────────────────────────────────────────
B.reply = lambda p: b'{"ok":true}\n'
for path, needles in [
        ("/", ['id="scenes-card"', 'id="sc-editor"', 'id="more-fold"', "PANIC", "/static/js/scenes.js",
               "/static/js/main.js", "/static/css/scenes.css", 'id="levels-body"']),
        ("/chord", ["/static/js/chord.js", 'id="content"']),
        ("/skipback", ['id="skipback-compact-card"', "/static/js/sb_status.js"]),
        ("/system", ['id="orc-restart"', "orcRestart()"])]:
    r = cl.get(path)
    body = r.get_data(as_text=True)
    check(r.status_code == 200 and all(n in body for n in needles), f"page {path} has its parts",
          [n for n in needles if n not in body])
home = cl.get("/").get_data(as_text=True)
check("sys-compact-card" not in home and "skipback-compact-card" not in home and "system.js" not in home,
      "PLAY no longer carries PI HEALTH / SKIPBACK cards")
check('class="nav-cat solo" href="/chord"' in home, "CHORD is in the nav")
hsrc = open(os.path.join(HERE, "orc1_health.py"), encoding="utf-8").read()
check('"/chord"' in hsrc.split("WEB_PAGES", 1)[1].split("]", 1)[0], "orc1_health WEB_PAGES lists /chord")
for f in ("js/scenes.js", "js/chord.js", "js/sb_status.js", "js/main.js", "css/scenes.css"):
    r = cl.get("/static/" + f)
    check(r.status_code == 200 and len(r.data) > 500, f"static {f} served")
    r.close()

# every action the old PLAY page sent is still sent from its new home
def src(rel):
    return open(os.path.join(HERE, rel), encoding="utf-8").read()


NEW = src("static/js/main.js") + src("static/js/chord.js") + src("static/js/system.js") + src("templates/index.html")
OLD_ACTIONS = ["SET_CLOCK_SRC", "TOGGLE_CLOCK_OUT", "TAP_TEMPO", "SET_BPM", "MIDI_RESCAN", "TOGGLE_DIN_ROUTING",
               "TOGGLE_SYNTH_MIRROR_POLY", "TOGGLE_SYNTH_MIRROR_CHIP", "ROOT_SET", "SCALE_SET", "TOGGLE_ROOT_MODE",
               "TOGGLE_KEY_LOCK", "CYCLE_SCALE_MODE", "TOGGLE_QUALITY_MODE", "TOGGLE_VL", "TOGGLE_MIRROR",
               "TOGGLE_ARP_PT", "TOGGLE_ARP", "TOGGLE_ARP_HOLD", "PATTERN_PREV", "PATTERN_NEXT", "ARP_RATE_PREV",
               "ARP_RATE_NEXT", "ARP_GATE_DELTA", "ARP_SWING_DELTA", "ARP_HUMANIZE_DELTA", "PANIC", "RESTART_ORC",
               "SYNTH_CC"]
missing = [a for a in OLD_ACTIONS if ("'" + a + "'") not in NEW]
check(not missing, "every old PLAY action still has a control", missing)
chord = src("static/js/chord.js")
check(all(a in chord for a in ("TOGGLE_SYNTH_MIRROR_POLY", "ROOT_SET", "TOGGLE_ARP", "CYCLE_SCALE_MODE")),
      "chord engine, arp and CHORDS -> POLY / CHIP are on CHORD")
check("ROOT_SET" not in src("static/js/main.js") and "TOGGLE_ARP'" not in src("static/js/main.js"),
      "PLAY no longer carries the chord engine / arp")

B.stop()
total = _p + _f
print(f"_test_scenes_web: {_p} passed, {_f} failed")
if total != EXPECTED:
    print(f"COUNT DRIFT: ran {total} checks, EXPECTED = {EXPECTED}")
    sys.exit(1)
sys.exit(1 if _f else 0)
