#!/usr/bin/env python3
"""FX ride-live probe: for one LV2 plugin, sweep every input control while a
steady tone runs through it (via the REAL jalv+FIFO path the ORC uses) and score
each control for glitchiness (transient spikes in the output during the sweep).

Usage:  python3 fxprobe.py <uri-or-shortname> [more ...]
Calibration: galactic's 'bigness' (size) is known to glitch; expect a high score
there and low scores on brightness/detune.
"""
import sys, os, re, time, subprocess, threading, signal
import numpy as np
import jack

TONE_HZ = 220.0
TONE_A  = 0.30

def lv2ls():
    return subprocess.run(["lv2ls"], capture_output=True, text=True).stdout.split()

def resolve_uri(name):
    if "://" in name:
        return name
    for u in lv2ls():
        if u.rsplit("/", 1)[-1] == name or u.rsplit("#", 1)[-1] == name:
            return u
    return None

def parse_ports(uri):
    """-> (audio_in[symbols], audio_out[symbols], controls[{symbol,min,max,default}])."""
    txt = subprocess.run(["lv2info", uri], capture_output=True, text=True).stdout
    blocks = re.split(r"\n\s*Port \d+:", txt)
    ain, aout, ctrls = [], [], []
    for b in blocks:
        sym = re.search(r"Symbol:\s*(\S+)", b)
        if not sym:
            continue
        sym = sym.group(1)
        is_audio = "AudioPort" in b
        is_ctrl  = "ControlPort" in b
        is_in    = "InputPort" in b
        is_out   = "OutputPort" in b
        if is_audio and is_in:  ain.append(sym)
        elif is_audio and is_out: aout.append(sym)
        elif is_ctrl and is_in:
            mn = re.search(r"Minimum:\s*([-\d.eE]+)", b)
            mx = re.search(r"Maximum:\s*([-\d.eE]+)", b)
            df = re.search(r"Default:\s*([-\d.eE]+)", b)
            ctrls.append({"symbol": sym,
                          "min": float(mn.group(1)) if mn else 0.0,
                          "max": float(mx.group(1)) if mx else 1.0,
                          "default": float(df.group(1)) if df else 0.0})
    return ain, aout, ctrls

def spawn_jalv(client, uri):
    fifo = f"/tmp/jalv-{client}.fifo"
    cmd = (f'FIFO="{fifo}"; mkfifo "$FIFO" 2>/dev/null; exec 3<>"$FIFO"; '
           f'exec jalv -n {client} "{uri}" < "$FIFO" 2>/dev/null')
    p = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return p, fifo

def wait_ports(client, timeout=12):
    t = time.time()
    while time.time() - t < timeout:
        outs = subprocess.run(["jack_lsp"], capture_output=True, text=True).stdout
        if any(l.startswith(client + ":") for l in outs.splitlines()):
            time.sleep(0.5)
            return True
        time.sleep(0.3)
    return False

def probe(name):
    uri = resolve_uri(name)
    if not uri:
        print(f"{name}: URI not found"); return
    ain, aout, ctrls = parse_ports(uri)
    if len(ain) < 1 or len(aout) < 1:
        print(f"{name}: not a stereo/mono audio effect ({len(ain)} in {len(aout)} out) -- skip")
        return
    client = "fxp"
    subprocess.run(["bash", "-c", f'rm -f /tmp/jalv-{client}.fifo'], capture_output=True)
    proc, fifo = spawn_jalv(client, uri)
    if not wait_ports(client):
        print(f"{name}: jalv ports never appeared");
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception: pass
        return

    jc = jack.Client("tone")
    oL = jc.outports.register("oL"); oR = jc.outports.register("oR")
    iL = jc.inports.register("iL");  iR = jc.inports.register("iR")
    phase = {"p": 0.0}
    cap = {"on": False, "buf": []}
    lock = threading.Lock()
    @jc.set_process_callback
    def process(n):
        t = (phase["p"] + np.arange(1, n+1)) / jc.samplerate
        sig = (TONE_A * np.sin(2*np.pi*TONE_HZ*t)).astype(np.float32)
        phase["p"] = (phase["p"] + n) % jc.samplerate
        oL.get_array()[:] = sig; oR.get_array()[:] = sig
        if cap["on"]:
            with lock:
                cap["buf"].append(iL.get_array().copy())

    with jc:
        # wire tone -> plugin -> capture
        try:
            jc.connect(f"tone:oL", f"{client}:{ain[0]}")
            jc.connect(f"tone:oR", f"{client}:{ain[1 if len(ain)>1 else 0]}")
            jc.connect(f"{client}:{aout[0]}", "tone:iL")
            jc.connect(f"{client}:{aout[0]}", "tone:iR")
        except Exception as e:
            print(f"{name}: connect failed: {e}")
        time.sleep(0.4)
        ff = os.open(fifo, os.O_WRONLY)
        def setc(sym, val): os.write(ff, f"{sym} = {val}\n".encode())

        def capture(secs):
            with lock: cap["buf"] = []
            cap["on"] = True; time.sleep(secs); cap["on"] = False
            with lock:
                return np.concatenate(cap["buf"]) if cap["buf"] else np.zeros(1, np.float32)

        # baseline: defaults held
        for c in ctrls: setc(c["symbol"], c["default"])
        time.sleep(0.3)
        base = capture(0.5)
        base_pk = float(np.max(np.abs(np.diff(base)))) if base.size > 2 else 1e-9
        base_pk = max(base_pk, 1e-6)

        print(f"\n=== {name}  ({uri.rsplit('/',1)[-1]})  in={ain} out={aout} ===")
        print(f"    baseline peak|diff| = {base_pk:.5f}   ({len(ctrls)} controls)")
        SKIP = {"on", "bypass", "enabled", "active"}
        results = []
        for c in ctrls:
            sym, lo, hi = c["symbol"], c["min"], c["max"]
            if sym.lower() in SKIP or hi <= lo or "meter" in sym.lower():
                continue
            # sweep lo->hi over ~1.4s in steps, capturing
            with lock: cap["buf"] = []
            cap["on"] = True
            steps = 70
            for i in range(steps+1):
                setc(sym, lo + (hi-lo)*i/steps)
                time.sleep(0.02)
            time.sleep(0.05); cap["on"] = False
            with lock:
                sw = np.concatenate(cap["buf"]) if cap["buf"] else np.zeros(1, np.float32)
            setc(sym, c["default"])
            if sw.size < 3:
                continue
            d = np.abs(np.diff(sw))
            pk = float(np.max(d))
            med = float(np.median(d)) or 1e-9
            # glitch score: transient spike vs baseline, AND peak-vs-own-median
            score = pk / base_pk
            spike = pk / med
            results.append((sym, score, spike, pk))
        results.sort(key=lambda r: -r[1])
        for sym, score, spike, pk in results:
            flag = "GLITCHY" if (score > 4 and spike > 8) else ("watch" if score > 2.5 else "smooth")
            print(f"    {sym:16s} score={score:6.1f}  peak/med={spike:6.1f}  pk={pk:.4f}   {flag}")
        os.close(ff)

    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception: pass
    subprocess.run(["bash", "-c", f'rm -f {fifo}'], capture_output=True)
    time.sleep(0.5)

if __name__ == "__main__":
    for name in sys.argv[1:]:
        try:
            probe(name)
        except Exception as e:
            print(f"{name}: ERROR {e}")
