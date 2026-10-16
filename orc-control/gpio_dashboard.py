#!/usr/bin/env python3
"""
GPIO Test Dashboard
Run: python3 gpio_dashboard.py
Visit: http://pibox:5001

Install deps first:
  pip install flask flask-socketio --break-system-packages
"""

from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
from gpiozero import Button
import threading
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'gpio-test'
socketio = SocketIO(app, cors_allowed_origins="*")

# ── Pin config ─────────────────────────────────────────────────────
BUTTON_PINS = {
    'A': 4,
    'B': 22,
    'C': 27,
}

# ── State ──────────────────────────────────────────────────────────
state = {
    name: {'pin': pin, 'count': 0, 'active': False, 'last': None, 'status': 'idle'}
    for name, pin in BUTTON_PINS.items()
}
event_log = []
buttons = {}
init_errors = {}

def ts():
    return time.strftime('%H:%M:%S')

def log(msg, kind='info'):
    entry = {'time': ts(), 'msg': msg, 'kind': kind}
    event_log.append(entry)
    if len(event_log) > 100:
        event_log.pop(0)
    socketio.emit('log', entry)

def init_buttons():
    global buttons, init_errors
    # Clean up existing
    for btn in buttons.values():
        try: btn.close()
        except: pass
    buttons = {}
    init_errors = {}

    for name, pin in BUTTON_PINS.items():
        state[name]['count'] = 0
        state[name]['active'] = False
        state[name]['status'] = 'idle'
        try:
            btn = Button(pin, pull_up=True, hold_time=1.0)

            def make_handlers(n, p):
                def on_press():
                    state[n]['count'] += 1
                    state[n]['active'] = True
                    state[n]['last'] = ts()
                    state[n]['status'] = 'pressed'
                    log(f"Button {n}  GPIO {p}  press #{state[n]['count']}", 'press')
                    socketio.emit('state', state)

                def on_release():
                    state[n]['active'] = False
                    state[n]['status'] = 'idle'
                    log(f"Button {n} released", 'release')
                    socketio.emit('state', state)

                def on_hold():
                    state[n]['status'] = 'held'
                    log(f"Button {n}  HELD", 'hold')
                    socketio.emit('state', state)

                return on_press, on_release, on_hold

            p, r, h = make_handlers(name, pin)
            btn.when_pressed = p
            btn.when_released = r
            btn.when_held = h
            buttons[name] = btn
            log(f"Button {name} → GPIO {pin} ready", 'ok')

        except Exception as e:
            init_errors[name] = str(e)
            log(f"Button {name} → GPIO {pin} FAILED: {e}", 'error')

    socketio.emit('state', state)
    socketio.emit('init_errors', init_errors)

# ── Routes ─────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/restart', methods=['POST'])
def restart():
    log('--- Restarting GPIO ---', 'system')
    threading.Thread(target=init_buttons, daemon=True).start()
    return 'ok'

@socketio.on('connect')
def on_connect():
    emit('state', state)
    emit('init_errors', init_errors)
    for entry in event_log[-40:]:
        emit('log', entry)
    emit('log', {'time': ts(), 'msg': 'Connected', 'kind': 'system'})

@socketio.on('request_restart')
def on_restart():
    log('--- Restarting GPIO ---', 'system')
    threading.Thread(target=init_buttons, daemon=True).start()

# ── HTML ───────────────────────────────────────────────────────────
HTML = '''<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPIO Test</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d0d; color: #ccc;
    font-family: 'Courier New', monospace;
    padding: 16px; max-width: 600px; margin: 0 auto;
  }
  h1 { color: #fff; font-size: 1.1em; letter-spacing: 0.15em;
       text-transform: uppercase; margin-bottom: 4px; }
  .subtitle { color: #555; font-size: 0.75em; margin-bottom: 20px; }
  .dot { display: inline-block; width: 8px; height: 8px;
         border-radius: 50%; margin-right: 6px; background: #333; }
  .dot.ok    { background: #2a2; }
  .dot.error { background: #a22; }
  .dot.live  { background: #28f; }

  .buttons-grid {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 10px; margin-bottom: 20px;
  }
  .btn-card {
    background: #1a1a1a; border: 1px solid #2a2a2a;
    border-radius: 8px; padding: 14px 10px; text-align: center;
    transition: all 0.1s;
  }
  .btn-card.active {
    background: #1a2a1a; border-color: #3a6a3a;
    box-shadow: 0 0 12px rgba(60,180,60,0.2);
  }
  .btn-card.held {
    background: #2a1a0a; border-color: #8a4a0a;
    box-shadow: 0 0 12px rgba(255,140,0,0.2);
  }
  .btn-card.error {
    background: #1a0a0a; border-color: #4a1a1a;
  }
  .btn-label { font-size: 1.4em; font-weight: bold; color: #fff; }
  .btn-pin   { font-size: 0.7em; color: #555; margin: 2px 0 8px; }
  .btn-indicator {
    width: 28px; height: 28px; border-radius: 50%;
    background: #222; border: 2px solid #333;
    margin: 0 auto 8px;
    transition: all 0.1s;
  }
  .btn-card.active .btn-indicator {
    background: #3c3; border-color: #5e5;
    box-shadow: 0 0 8px rgba(60,200,60,0.6);
  }
  .btn-card.held .btn-indicator {
    background: #f90; border-color: #fb4;
    box-shadow: 0 0 8px rgba(255,160,0,0.6);
  }
  .btn-count { font-size: 1.3em; color: #aaa; }
  .btn-last  { font-size: 0.65em; color: #444; margin-top: 4px;
               min-height: 1em; }
  .btn-err   { font-size: 0.6em; color: #a44; margin-top: 4px; }

  .restart-btn {
    width: 100%; padding: 12px;
    background: #1a1a2a; border: 1px solid #333;
    color: #88f; font-family: inherit; font-size: 0.85em;
    letter-spacing: 0.1em; text-transform: uppercase;
    border-radius: 6px; cursor: pointer;
    margin-bottom: 20px; transition: all 0.15s;
  }
  .restart-btn:hover { background: #22224a; border-color: #55f; }
  .restart-btn:active { background: #33335a; }

  .log-header {
    font-size: 0.7em; color: #444; text-transform: uppercase;
    letter-spacing: 0.12em; margin-bottom: 8px;
  }
  .log {
    background: #111; border: 1px solid #1e1e1e;
    border-radius: 6px; padding: 10px;
    height: 280px; overflow-y: auto;
    font-size: 0.78em; line-height: 1.7;
  }
  .log-entry { display: flex; gap: 10px; }
  .log-time  { color: #444; min-width: 60px; }
  .log-msg   { color: #999; }
  .log-entry.press   .log-msg { color: #4d4; }
  .log-entry.hold    .log-msg { color: #fa4; }
  .log-entry.release .log-msg { color: #778; }
  .log-entry.error   .log-msg { color: #d44; }
  .log-entry.ok      .log-msg { color: #46a; }
  .log-entry.system  .log-msg { color: #666; }

  .status-bar {
    margin-top: 14px; font-size: 0.65em; color: #444;
    display: flex; justify-content: space-between;
  }
  #conn-status { }
  #conn-status.connected { color: #2a6; }
  #conn-status.disconnected { color: #a22; }
</style>
</head>
<body>
<h1><span class="dot" id="conn-dot"></span>GPIO Test Dashboard</h1>
<div class="subtitle">pibox:5001 &mdash; buttons only, no ADC</div>

<div class="buttons-grid" id="buttons-grid"></div>

<button class="restart-btn" onclick="doRestart()">⟳ Restart GPIO</button>

<div class="log-header">Event Log</div>
<div class="log" id="log"></div>

<div class="status-bar">
  <span id="conn-status" class="disconnected">Disconnected</span>
  <span id="event-count">0 events</span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"></script>
<script>
const PINS = ''' + str(BUTTON_PINS).replace("'", '"') + ''';
const socket = io();
let eventCount = 0;
let errors = {};
let currentState = {};

function buildGrid() {
  const grid = document.getElementById('buttons-grid');
  grid.innerHTML = '';
  for (const [name, pin] of Object.entries(PINS)) {
    grid.innerHTML += `
      <div class="btn-card" id="card-${name}">
        <div class="btn-label">BTN ${name}</div>
        <div class="btn-pin">GPIO ${pin}</div>
        <div class="btn-indicator" id="ind-${name}"></div>
        <div class="btn-count" id="count-${name}">0</div>
        <div class="btn-last" id="last-${name}">—</div>
        <div class="btn-err" id="err-${name}"></div>
      </div>`;
  }
}

function updateState(state) {
  currentState = state;
  for (const [name, s] of Object.entries(state)) {
    const card = document.getElementById('card-' + name);
    if (!card) continue;
    card.className = 'btn-card';
    if (errors[name]) {
      card.classList.add('error');
    } else if (s.status === 'held') {
      card.classList.add('held');
    } else if (s.active) {
      card.classList.add('active');
    }
    document.getElementById('count-' + name).textContent = s.count;
    document.getElementById('last-' + name).textContent = s.last || '—';
  }
}

function addLog(entry) {
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = 'log-entry ' + (entry.kind || '');
  div.innerHTML = `<span class="log-time">${entry.time}</span><span class="log-msg">${entry.msg}</span>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  eventCount++;
  document.getElementById('event-count').textContent = eventCount + ' events';
}

function doRestart() {
  socket.emit('request_restart');
}

socket.on('connect', () => {
  document.getElementById('conn-status').textContent = 'Connected';
  document.getElementById('conn-status').className = 'connected';
  document.getElementById('conn-dot').className = 'dot live';
});
socket.on('disconnect', () => {
  document.getElementById('conn-status').textContent = 'Disconnected';
  document.getElementById('conn-status').className = 'disconnected';
  document.getElementById('conn-dot').className = 'dot error';
});
socket.on('state', updateState);
socket.on('log', addLog);
socket.on('init_errors', (errs) => {
  errors = errs;
  for (const [name, msg] of Object.entries(errs)) {
    const el = document.getElementById('err-' + name);
    if (el) el.textContent = 'ERR: ' + msg.substring(0, 30);
  }
});

buildGrid();
</script>
</body>
</html>'''

# ── Start ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("\n  GPIO Test Dashboard")
    print("  ───────────────────")
    init_buttons()
    print(f"\n  Visit: http://pibox:5001\n")
    socketio.run(app, host='0.0.0.0', port=5001, debug=False)
