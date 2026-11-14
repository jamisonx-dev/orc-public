"""mxvbox conductor (Pi, python3; service orc1-boxvis): drives the MXV box's orcflash
player (clips + patterns + hit effects) from the ORC's beat.

    python3 conductor.py [offset_ms]      offset < 0 = AFTER the step (-15 felt right)

- Beat: reads /dev/shm/orc1_playstate.json and keeps its OWN steady step clock, phase-
  locked to it (deriving "the next step" from each feed update skipped steps), and sends
  each 16th step's picture once, in order.
- Hits: real drum note-ons on the brain's bus (ORC1-Synth-Bus) for the channels bound to
  drums / bbkit punch through as fading effects (READ as an observer: information). Follows the bus across
  brain restarts. Knobs + clip cues + the visuals' LISTEN knobs come from the ORC's own ports ORC-mxvbox /
  ORC-visuals (2026-11-09: never the synth bus, which is for synthesis only).
- Knobs: CCs + notes on the channel /matrix binds the module "mxvbox" to, found BY NAME
  live from the feed's bindings (mxvbox_knobs.py lists them). Saved to ~/.orc1_mxvbox.json.
- The box: UDP to 10.55.0.6:9000, one command per line (orcflash.py lists them).
- Clips: the BOX owns them (its card + its own RAM slots, orcflash.py). The conductor only
  says "p next" / "p cue N" / "x next" and asks the box every 10 s what it has (for the
  BOX tab, and so clip bars become pattern bars while the box has nothing loaded).

Import-safe: nothing runs until main().
"""
import json, os, random, re, socket, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mxvbox_knobs as K                      # noqa: E402

FEED = "/dev/shm/orc1_playstate.json"
# Two ways to the box: the eth0 cable (10.55.0.6, ~1 ms) whenever that link is up, else the
# home Wi-Fi as mxvbox.local (~6 ms each way through the router, measured 2026-10-29). The
# box only obeys addresses it trusts: 10.55.0.1, plus the Pi Wi-Fi address this conductor
# writes to its /dev/shm/orc/pi_addrs over ssh (key-only) on every status call.
BOX_CABLE, BOX_NAME, BOX_PORT = "10.55.0.6", "mxvbox.local", 9000
CARRIER = "/sys/class/net/eth0/carrier"
link = {"via": "cable", "addr": BOX_CABLE, "wifi": None, "wifi_t": 0.0, "pi_wifi": None}
SAVE = os.path.expanduser("~/.orc1_mxvbox.json")
MODULE = "mxvbox"
PALETTES = [("ff0040", "000000", "ffd000"), ("00e5ff", "1a0033", "ff00aa"),
            ("ff6a00", "001aff", "ffffff"), ("39ff14", "000000", "ff0000"),
            ("ffffff", "000000", "ff00ff")]
BOX_STATUS = "/dev/shm/orc1_mxvbox_status.json"      # for the web's BOX tab
STATUS_S = 10.0
BOX_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "mxvbox"]
STATUS_GRACE = 6         # status calls that may fail in a row before the box counts as gone (~60 s)


def box_ssh(addr):
    """ssh to the box at the address the link uses, not by name: mxvbox.local (mDNS) failed
    on and off for ~27 min on 2026-11-02, the BOX tab said 'disconnected' and the conductor,
    seeing 0 clips, played patterns only, while UDP to the known address worked all along."""
    if addr and re.match(r"^[0-9.]+$", addr):
        return BOX_SSH[:-1] + ["-o", "HostName=%s" % addr, "mxvbox"]
    return list(BOX_SSH)


def keep_wifi(prev, resolved):
    """The box's Wi-Fi address after a lookup: the new one, or (a lookup failed) the last good one."""
    return resolved or prev


def cable_up(path=CARRIER):
    try:
        with open(path) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def pick_link(cable, wifi_addr):
    """-> (via, addr): the cable whenever its link is up, else the box's Wi-Fi address
    (None = no way to the box right now)."""
    if cable:
        return "cable", BOX_CABLE
    return ("wifi", wifi_addr) if wifi_addr else ("none", None)


def resolve_wifi():
    """The box's Wi-Fi address + the Pi's own address towards it (mDNS, can take ~1 s)."""
    try:
        addr = socket.getaddrinfo(BOX_NAME, BOX_PORT, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((addr, BOX_PORT))            # UDP connect sends nothing; it picks the route
            mine = s.getsockname()[0]
        finally:
            s.close()
        return addr, (None if mine.startswith("10.55.0.") else mine)
    except (OSError, IndexError):
        return None, None


link_changed = threading.Event()


def link_thread():
    """Once a second: cable or Wi-Fi? A change is picked up within a second; the Wi-Fi
    address is looked up again every 30 s (and at once when the cable goes). A failed lookup
    keeps the last good address (mDNS drops out; the box's address rarely changes)."""
    while True:
        cable = cable_up()
        if (link["wifi"] is None and not cable) or time.time() - link["wifi_t"] > 30:
            addr, pi = resolve_wifi()
            link["wifi"] = keep_wifi(link["wifi"], addr)
            link["pi_wifi"] = pi or link["pi_wifi"]
            link["wifi_t"] = time.time()
        via, addr = pick_link(cable, link["wifi"])
        if via != link["via"] or addr != link["addr"]:
            print("box link: %s %s" % (via, addr or "-"), flush=True)
            link["via"], link["addr"] = via, addr
            link_changed.set()                    # tell the box our Wi-Fi address right now
        time.sleep(1.0)


def box_status_thread():
    """Every STATUS_S: the box's own status (what its card + RAM hold). Clip bars only
    happen while it has a clip loaded; the BOX tab reads BOX_STATUS. The same call tells
    the box which Pi Wi-Fi address to obey (the ssh alias follows the cable the same way)."""
    import subprocess
    fails = 0
    while True:
        st = {"box": False, "library": 0, "slots": [], "loading": None}
        pi = link["pi_wifi"]
        # pi_seen: "the Pi just called", for the box's wifikeep (it follows the Pi's hotspot
        # when this goes stale, 2026-10-31); touched on every call, cable or Wi-Fi
        tell = "touch /dev/shm/orc/pi_seen; "
        if pi and re.match(r"^[0-9.]+$", pi):
            tell += "echo %s > /dev/shm/orc/pi_addrs; " % pi
        try:
            r = subprocess.run(box_ssh(link["addr"]) + [tell + "cat /dev/shm/orc/status.json; echo; cat /storage/orc/lib/index.json;"
                                          " echo; cat /storage/orc/lib/pools.json 2>/dev/null || echo '[]';"
                                          " echo; cat /storage/orc/lib/flags.json 2>/dev/null || echo '{}'"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                parts = [p for p in r.stdout.split("\n") if p.strip()]
                st.update(json.loads(parts[0]))
                st["clips"] = json.loads(parts[1]) if len(parts) > 1 else []   # the box's library
                st["pool_lists"] = json.loads(parts[2]) if len(parts) > 2 else []   # [{name, ids}]
                st["flags"] = json.loads(parts[3]) if len(parts) > 3 else {}        # {fav: [ids], never: [ids]}
                st["box"] = True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        fails = 0 if st["box"] else fails + 1
        if st["box"] or fails >= STATUS_GRACE:      # one missed call doesn't stop the clip bars
            state["box_clips"] = len(st.get("slots") or [])
        st["link"], st["addr"] = link["via"], link["addr"]
        try:
            with open(BOX_STATUS + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(BOX_STATUS + ".tmp", BOX_STATUS)
        except OSError:
            pass
        link_changed.wait(STATUS_S)
        link_changed.clear()


KICKS, SNARES = {35, 36}, {38, 40, 37, 39}     # GM kick / snare, rim, clap
DRUM_MODULES = ("drums", "bbkit")
# (the raster bars, box command r, were never used -- owner: "looks like windows 98 got a bit too
# full of itself" -- and left the box 2026-11-02: "drop the raster")

knob = K.defaults()
state = {"bps": 115 / 60.0, "drum_chs": set(), "my_ch": None, "cue": None, "clip": None,
         "box_clips": 0, "pool": None, "pool_t": 0.0, "scene_bars": 0, "vis_ch": None, "rot_step": None,
         "rot": {"looks": (), "kick": None, "snare": None, "p": {}},   # ROTATE's pick (ROT_NONE)
         "dirty": 0.0, "show": True, "alpha": None, "alpha_t": 0.0, "looks": None, "looks_t": 0.0, "looks_calc": 0.0}
lock = threading.Lock()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send(cmd):
    addr = link["addr"]
    if addr is None:
        return                  # no way to the box right now
    try:
        sock.sendto(cmd.encode(), (addr, BOX_PORT))
    except OSError:
        pass                    # box off / cable out: UDP just drops, nothing to retry


# ── knobs ─────────────────────────────────────────────────────────────────────
def load_knobs(path=SAVE):
    try:
        with open(path) as f:
            saved = json.load(f)
        moved = False
        for old, new in K.MOVED_CCS.items():         # a knob that moved CC (ROTATE, 2026-11-03): its value
            if str(old) in saved and str(new) not in saved:     # comes along once, then is saved there
                saved[str(new)] = saved[str(old)]
                moved = True
        for k, v in saved.items():
            if int(k) in K.KNOBS:
                knob[int(k)] = max(0, min(127, int(v)))
        if moved:
            save_knobs(path)
            print("knobs: moved %s to their new CCs" % sorted(K.MOVED_CCS), flush=True)
    except (OSError, ValueError):
        pass


def save_knobs(path=SAVE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({str(k): v for k, v in sorted(knob.items())}, f)
    os.replace(tmp, path)


def fade_s():
    return 0.05 + knob[20] / 127.0 * 1.45


def hit_size():
    return knob[28] / 64.0


# the hit reactions, all drawn + faded by the BOX ('h KIND AMT FADE_MS', one message a hit): their
# peak at HIT SIZE 64. blur in levels 0-4, rgb / shudder / warp in picture px, zoom 12 % closer,
# hue degrees, invert / grey 0-1 (capped at 1: HIT SIZE above 64 can't go past "all")
HIT_PEAK = {"blur": 4.0, "rgb": 12.0, "zoom": 0.12, "shudder": 3.0, "hue": 90.0,
            "invert": 1.0, "grey": 1.0, "warp": 10.0}
HIT_CAP = {"blur": 4.0, "invert": 1.0, "grey": 1.0}
KALEIDO_HIT = 6                                   # mirrors (a snap: HIT SIZE does not scale it)


def hit_style(drum):
    """kick/snare -> the reaction's name, or None when that drum's style is off. With ROTATE HITS on,
    the rotation's pick for that drum instead."""
    if knob.get(K.ROTATE_HITS_CC, 0) >= 64 and state["rot"].get(drum):
        return state["rot"][drum]
    style = K.REACTIONS[K.reaction(knob[21 if drum == "kick" else 22])]
    return None if style == "off" else style


# ── ROTATE: the box picks which looks play, bar by bar (2026-11-02) ──────────────────
ROT_BARS = (0, 1, 2, 4, 8)                        # ROTATE, per fifth (0 = off)
ROT_LOOKS = ("kaleido", "rgb", "hue", "invert", "wobble", "mask")   # allowed by K.ROTATE_ALLOW_CCS
ROT_NONE = {"looks": (), "kick": None, "snare": None, "p": {}}


def rotate_tick(bar_n):
    """On a bar's downbeat: a new pick every ROTATE bars, from the allowed looks, never the same set
    twice running (when there is anything else to pick). Seeded by the bar, so it can be tested."""
    every = ROT_BARS[K.fifth(knob.get(61, 0))]
    if not every:
        state["rot"], state["rot_step"] = ROT_NONE, None
        return
    step = bar_n // every
    if step == state["rot_step"]:
        return
    state["rot_step"] = step
    rng = random.Random(step * 7919 + 13)
    allowed = [l for l, cc in zip(ROT_LOOKS, K.ROTATE_ALLOW_CCS) if knob.get(cc, 127) >= 64]
    n = min(len(allowed), 1 + K.pick(knob.get(62, 0), 3))
    prev = set(state["rot"]["looks"])
    fresh = [l for l in allowed if l not in prev]
    looks = rng.sample(fresh, min(n, len(fresh)))
    if len(looks) < n:                            # (few allowed: top up from the last pick)
        looks += rng.sample([l for l in allowed if l not in looks], n - len(looks))
    hits = [r for r in K.REACTIONS if r != "off"]
    state["rot"] = {"looks": tuple(sorted(looks)), "kick": rng.choice(hits), "snare": rng.choice(hits),
                    "p": {"kaleido": rng.choice((3, 4, 6, 8)), "rgb": rng.choice((4.0, 8.0, 16.0)),
                          "hue": rng.choice((60.0, 120.0, 180.0, 240.0, 300.0)),
                          "mask": (rng.randint(1, 5), rng.choice((0, 0, 2, 3)))}}


FADE_S = 0.8                                      # SHOW moves fade over this long (the box's chip does the fade)


def alpha_of(v):
    """SHOW 0-127 -> the box's screen brightness 0-255."""
    return int(round(max(0, min(127, v)) / 127.0 * 255))


def alpha_cmd(now):
    """The box command that brings its brightness to SHOW, or None when it is already told. A change
    fades over FADE_S; the first send and the 10 s refresh (a box restart starts at full brightness)
    are instant."""
    a = alpha_of(knob[7])
    if a == state["alpha"] and now - state["alpha_t"] <= 10.0:
        return None
    secs = FADE_S if state["alpha"] is not None and a != state["alpha"] else 0.0
    state["alpha"], state["alpha_t"] = a, now
    return "a %d %.2f" % (a, secs)


def lines_cmd():
    """The LINES look as box commands: the GPU's thin lines (n), black or made of the blurred picture.
    The parked 2nd-layer lines (t) are always switched off. (Black used to go out as 't' + 'n 0',
    and with the layer parked the box's fallback set the thin lines that the 'n 0' then cleared:
    black lines drew nothing, 2026-11-01 .. 11-02.)"""
    i = K.fifth(knob[23])
    blur = knob[24] >= 64
    look = (K.LINE_LOOKS_BLUR if blur else K.LINE_LOOKS)[i]
    return "t 0\n" + ("n %d %d %s" % (look + ("blur" if blur else "000000",)) if look else "n 0")


def clips_of_4():
    return int(round(knob[25] / 127.0 * 4))


def pool_cmd(now):
    """'g N' when the POOL knob moved, and again every 5 s (UDP can drop; the box may
    have restarted and forgotten), else None."""
    n = (knob.get(30, 0), knob.get(60, 0))           # the clips' pool, the scenes' (SCENE POOL, 0 = the same)
    if n == state["pool"] and now - state["pool_t"] < 5.0:
        return None
    state["pool"], state["pool_t"] = n, now
    return "g %d\ng2 %d" % n


def looks_text():
    """The GPU looks (KALEIDO .. CROSSFADE knobs) as the box's K / R / C / X commands. SPIN and
    HUE CYCLE are "turns per bar", so they follow the tempo."""
    per_bar = state["bps"] / 4.0                      # bars per second
    k = K.KALEIDO_MIRRORS[K.fifth(knob[31])]
    rot, rp = state["rot"]["looks"], state["rot"]["p"]    # ROTATE: only where your own knob is off
    if not k and "kaleido" in rot:
        k = rp["kaleido"]
    rgb = 32 * (knob[33] / 127.0) ** 2                    # curved: fine low, wild high
    if not knob[33] and "rgb" in rot:
        rgb = rp["rgb"]
    hue = knob[34] / 128.0 * 360 + (rp["hue"] if not knob[34] and "hue" in rot else 0.0)
    inv = knob[36] / 127.0 if knob[36] or "invert" not in rot else 1.0
    return "\n".join((
        "K %d %.4f" % (k, knob[32] / 127.0 * per_bar),
        "R %.2f" % rgb,
        "C %.1f %.3f %.3f %.4f" % (hue, knob[35] / 64.0, inv, knob[37] / 127.0 * per_bar),
        "X %.2f" % (knob[38] / 127.0 * 2),
        mask_text(per_bar),
        frame_text(),
    ))


def frame_text():
    """The FRAME knobs as the box's F command (F 0 = off). Your picture: SIZE 64 = the screen."""
    shape = K.pick(knob.get(51, 0), len(K.FRAME_SHAPES))
    if not shape:
        return "F 0"
    size = knob.get(52, 80) / (64.0 if K.FRAME_SHAPES[shape] == "picture" else 127.0)
    return "F %d %.3f %.3f %.3f %.3f %d %d" % (shape, size, knob.get(53, 64) / 127.0, knob.get(54, 64) / 127.0,
                                            knob.get(55, 12) / 127.0, 1 if knob.get(56, 0) >= 64 else 0,
                                            K.pick(knob.get(57, 0), 4) + 1)


def mask_text(per_bar):
    """The MASK knobs as the box's M command (M 0 = off). MOVE is turns per bar, the PULSE breathes
    once a beat (both follow the tempo)."""
    shape = K.pick(knob.get(45, 0), len(K.MASK_SHAPES))
    if not shape and "mask" in state["rot"]["looks"]:     # ROTATE's mask (your own MASK off)
        shape, fill = state["rot"]["p"]["mask"]
        return "M %d 0.450 %d %.4f 0.000 %.4f" % (shape, fill, 0.25 * per_bar, state["bps"])
    if not shape:
        return "M 0"
    return "M %d %.3f %d %.4f %.3f %.4f" % (shape, knob.get(46, 64) / 127.0, K.pick(knob.get(47, 0), len(K.MASK_FILLS)),
                                         knob.get(48, 0) / 127.0 * per_bar, knob.get(49, 0) / 127.0, state["bps"])


LOOKS_DT = 0.05                                   # a knob reaches the box within 50 ms


def looks_cmd(now):
    """The looks' commands when a knob (or the tempo) changed them, and again every 5 s (UDP can
    drop; a restarted box starts with every look off), else None."""
    if now - state["looks_calc"] < LOOKS_DT:
        return None                               # (built every 4 ms loop before: ~250 a second)
    state["looks_calc"] = now
    txt = looks_text()
    if txt == state["looks"] and now - state["looks_t"] < 5.0:
        return None
    state["looks"], state["looks_t"] = txt, now
    return txt


def scene_cmd(bar=0):
    """'S NAME SPEED DETAIL VIDEO' for the SCENE knob's scene (SPEED in cycles/s: 64 = one a bar at
    this tempo), or None when SCENE is off (the pattern bars show patterns). SCENE CYCLE moves on to
    the next scene every N bars, starting from the chosen one (owner 2026-11-02: "cycle"). bar =
    how many SCENE bars have shown (not the song's bar: scenes only fill the pattern bars, and
    counting every bar skipped half the scenes at 'every 2 bars')."""
    name = K.scene_of(knob[39])
    if not name:
        return None
    every = K.SCENE_CYCLE_BARS[K.fifth(knob[43])]
    if every:
        names = K.SCENE_NAMES[1:]
        name = names[(names.index(name) + bar // every) % len(names)]
    return "S %s %.4f %.3f %.3f %.3f" % (name, knob[40] / 64.0 * state["bps"] / 4.0, knob[41] / 127.0,
                                         knob[42] / 127.0, knob.get(44, 0) / 127.0)


def change_every():
    return (1, 2, 4, 8)[min(3, knob[29] // 32)]


# ── pictures ──────────────────────────────────────────────────────────────────
def seq_hit(tracks, prefix, step):
    for t in tracks:
        if t.get("name", "").lower().startswith(prefix) and not t.get("mute") and t.get("steps"):
            s = t["steps"]
            if s[step % len(s)] == "1":
                return True
    return False


def clip_bar(bar):
    return (bar % 4) < clips_of_4()             # CLIPS of every 4 bars are clips


def bar_fx(bar, clip=None):
    """The row effects for a bar, sent at its downbeat (wobble timed to the beat)."""
    bps = state["bps"]
    wob = knob[26] / 64.0
    if not wob and "wobble" in state["rot"]["looks"]:     # ROTATE's wobble (your own WOBBLE off)
        wob = 0.8
    out = ["w 0 0 0", "x -1", lines_cmd(), "l 0"]       # l 0: clear the OLD picture-row lines
    if clip_bar(bar) if clip is None else clip:
        if bar % 3 == 1 and wob > 0:
            out[0] = "w %d 1.5 %.3f" % (round(10 * wob), bps)                   # gentle ripple
        elif bar % 3 == 2 and knob[27] > 0:
            out[1] = "x next %.2f" % (knob[27] / 127.0)                         # weave another clip
    elif wob > 0:
        out[0] = "w %d 0.5 %.3f" % (round(20 * wob), bps / 2)                  # slow heavy bend
    return out


def picture(step, tracks):
    """The command(s) for this 16th step, or None when nothing should change."""
    every = change_every()
    bar_n, in_bar = divmod(step, 16)
    if in_bar == 0:
        rotate_tick(bar_n)                      # ROTATE: a new pick of looks, every N bars
    bar = bar_n // every                        # CHANGE EVERY: hold a picture for N bars
    first = in_bar == 0 and bar_n % every == 0
    if first:
        # decided ONCE per (held) bar: a clip if the bar is a clip bar and something has
        # arrived in a box slot, else a pattern (the box starts empty, or rebooted)
        if clip_bar(bar) and state["box_clips"] > 0:
            if state["cue"] is not None:        # a note on our channel: library clip N
                play, state["cue"] = "p cue %d" % state["cue"], None
            else:
                play = "p next"                 # the box picks (least shown of what it has)
            state["clip"] = "box"
            return "\n".join(["%s %d" % (play, (bar * 37) % 150)] + bar_fx(bar, clip=True))
        sc = scene_cmd(state["scene_bars"])     # (counts scene bars, for SCENE CYCLE)
        if sc:                                  # SCENE chosen: the pattern bars show it instead
            state["scene_bars"] += 1
            state["clip"] = "scene"             # (nothing more to send mid-bar: the box animates it)
            return "\n".join([sc] + bar_fx(bar, clip=False))
        state["clip"] = None
        return "\n".join([pattern_cmd(step, tracks, bar)] + bar_fx(bar, clip=False))
    if state["clip"] is not None:               # a clip plays: nothing to send mid-bar
        return None
    return pattern_cmd(step, tracks, bar)


def pattern_cmd(step, tracks, bar):
    in_bar = step % 16
    beat = in_bar // 4
    a, b, c = PALETTES[bar % len(PALETTES)]
    if seq_hit(tracks, "snare", step):
        a, b = b, a
    kind = (bar // 4) % 5                       # pattern bars cycle the pattern types
    if kind == 0:
        return "v %d %s %s" % (2 + beat * 2, a, b)
    if kind == 1:
        return "h %d %s %s %s" % (3 + beat, a, b, c)
    if kind == 2:
        return "k %d %s %s" % ((4, 8, 16, 8)[beat], a, b)
    if kind == 3:
        return "s %.3f %s %s" % ((in_bar + 1) / 16.0, a, b)      # a wipe across the bar
    return "o %s %s %.2f" % (a, b, (0.9, 0.6, 0.35, 0.15)[beat])


# ── hits ──────────────────────────────────────────────────────────────────────
def num(v):
    """4.0 -> "4", 2.35 -> "2.35": the shortest way to write an amount."""
    return ("%.3f" % v).rstrip("0").rstrip(".")


def hit_cmd(style):
    """The one message for a hit of reaction `style`: the box holds it, then eases it out over FADE."""
    if style == "kaleido":
        amt = KALEIDO_HIT
    else:
        amt = HIT_PEAK[style] * hit_size()
        amt = min(HIT_CAP.get(style, amt), amt)
    return "h %s %s %d" % (style, num(amt), round(fade_s() * 1000))


def fire_hit(drum, now):
    style = hit_style(drum)
    if not state["show"]:
        return
    if style is not None:
        send(hit_cmd(style))                      # the hit itself goes out at once; the box fades it
    if drum == "kick" and knob.get(50, 0) and (K.pick(knob.get(45, 0), len(K.MASK_SHAPES))
                                               or "mask" in state["rot"]["looks"]):
        send(mask_hit_cmd())                      # MASK HIT: the kick throws the hole open (your mask
                                                  # or ROTATE's: it only followed yours, 2026-11-03 review)


def mask_hit_cmd():
    return "h mask %s %d" % (num(0.6 * knob.get(50, 0) / 127.0), round(fade_s() * 1000))

def on_midi(event, _data=None):
    msg = event[0]
    if len(msg) < 3:
        return
    kind, ch = msg[0] & 0xF0, (msg[0] & 0x0F) + 1
    with lock:
        if ch == state["my_ch"]:                   # our own module channel: knobs + cues
            if kind == 0xB0 and msg[1] in K.KNOBS:
                knob[msg[1]] = msg[2]
                state["dirty"] = time.time()
            elif kind == 0x90 and msg[2] > 0:
                state["cue"] = msg[1]
            return
        if kind == 0xB0 and ch == state["vis_ch"] and msg[1] in LISTEN_CCS:
            vis[msg[1]] = msg[2]                   # the LISTEN tab's band / SENS / GATE, live
            return
        if kind == 0x90 and msg[2] > 0 and ch in state["drum_chs"]:
            if msg[1] in KICKS:
                fire_hit("kick", time.time())
            elif msg[1] in SNARES:
                fire_hit("snare", time.time())


# ── LISTEN: the box reacts to what the visuals' LISTEN tab hears (2026-11-02) ─────────
# owner: "the big thing it will be missing ... the ability to respond to what the listen tab on the
# visualizer section is triggering". The same detector (orc1/daw/vis_listen.py) with the same knobs
# (seeded from the renderer's state file, then followed live on the visuals module's channel), so it
# hears exactly what LISTEN hears, also while the visuals kiosk is off. It reads orc1-vistap's audio
# (started when needed) or, failing that, the skipback master ring (read-only).
LISTEN_CCS = (31, 32, 33, 54, 59, 60, 61, 62)
LISTEN_DT = 0.012                                 # s between looks (the tap's audio comes in 21 ms blocks)
vis = {}                                          # the LISTEN knobs {cc: value}


def listen_style(cc):
    style = K.REACTIONS[K.reaction(knob.get(cc, 109))]
    return None if style == "off" else style


def listen_hits(b1, b2, band2):
    """The commands for one LISTEN look: band 1's beat -> LISTEN STYLE; the background's beat (band 2
    when LISTEN gives it its own band, else band 1's) -> LISTEN 2 STYLE."""
    out = []
    s1, s2 = listen_style(58), listen_style(59)
    if b1 and s1:
        out.append(hit_cmd(s1))
    if (b2 if band2 else b1) and s2:
        out.append(hit_cmd(s2))
    return out


def listen_thread(make_tap=None, sleep=time.sleep):
    try:
        if make_tap is None:
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "orc1", "daw")
            if d not in sys.path:
                sys.path.insert(0, d)
            import vis_listen as VL
            vis.update(VL.listen_knobs())
            make_tap, apply_k = VL.AudioTap, VL.apply_knobs
        else:
            make_tap, apply_k = make_tap
    except Exception as e:                        # no numpy / no detector: LISTEN just does nothing
        print("LISTEN off (%s)" % e, flush=True)
        return
    tap, tap_start, err_t = make_tap(), 0.0, 0.0
    while True:
        if not (listen_style(58) or listen_style(59)) or not state["show"]:
            sleep(0.25)                           # nothing listens: cost nothing
            continue
        try:
            with lock:
                k = dict(vis)
            apply_k(tap, k)
            now = time.monotonic()
            b1, b2, _lvl = tap.update(now)
            if tap.source != "tap" and now - tap_start > 30.0:
                tap_start = now                   # (orc1-boxvis Wants= the tap since 2026-11-03: a backstop)
                try:
                    import subprocess
                    subprocess.Popen(["sudo", "-n", "systemctl", "start", "orc1-vistap"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            for c in listen_hits(b1, b2, tap.band2_on):
                send(c)
        except Exception as e:                    # one bad read must not end LISTEN until a restart
            if time.monotonic() - err_t > 10:     # (it did: the thread just died, 2026-11-03 review)
                err_t = time.monotonic()
                print("LISTEN error (carrying on): %r" % e, flush=True)
            sleep(0.5)
            continue
        sleep(LISTEN_DT)


# ── the bus (follows brain restarts) ──────────────────────────────────────────
MIDI_CLIENT = "orc-boxvis-in"          # our client on the synth bus: drum hits (observed, information only)
# 2026-11-09: mxvbox and visuals are not synths, so their knobs come from the ORC (the brain) on its OWN ports,
# never the synth bus (owner: "don't ever move anything that doesn't need an internal synthesis onto the synth").
# Exact port names (= orc1_config.orc_port_name / is_orc_port, copied: this conductor stands alone). Our
# client names ("orc-boxvis-...") never match them.
ORC_PORT = "ORC-mxvbox"               # our knobs + clip cues
ORC_VIS_PORT = "ORC-visuals"          # the visuals module's LISTEN knobs (band / SENS / GATE) we follow
CTL_CLIENT = "orc-boxvis-ctl"
VIS_CLIENT = "orc-boxvis-vis"


def is_bus_port(pname):
    return "orc1-synth-bus" in pname.lower()


def port_part(alsa_name):
    """'RtMidiOut Client:ORC-mxvbox 140:0' -> 'ORC-mxvbox' (= orc1_config.port_part)."""
    m = re.match(r"^[^:]*:(.*?)(?: \d+:\d+)?$", alsa_name or "")
    return m.group(1) if m else ""


def seq_listening(text, me=MIDI_CLIENT, is_src=is_bus_port):
    """/proc/asound/seq/clients -> True when a port of client `me` is Connected From the
    port passing is_src (default ORC1-Synth-Bus: the brain's CURRENT bus). Same logic as
    orc1/daw/orc_visuals.py seq_listening (that renderer learnt it the hard way)."""
    bus, mine, cname, sub = set(), [], None, False
    for ln in text.splitlines():
        m = re.match(r'Client\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m:
            cid, cname = m.group(1), m.group(2)
            continue
        m = re.match(r'\s+Port\s+(\d+)\s*:\s*"([^"]*)"', ln)
        if m and cname is not None:
            if is_src(m.group(2).strip()):
                bus.add("%s:%s" % (cid, m.group(1)))
            sub = cname == me
            continue
        m = re.match(r"\s+Connected From:\s*(.*)", ln)
        if m and sub:
            mine += [a.split("[")[0].strip() for a in m.group(1).split(",")]
    return any(a in bus for a in mine)


def bus_thread(check_s=1.0, client=MIDI_CLIENT, find=None, is_src=is_bus_port):
    """Every check_s: still Connected From the current bus? If not, reopen -- and set the
    callback again after EVERY open (python-rtmidi's close_port() drops it: the visuals
    went deaf at their first reconnect, 2026-10-24). Proven 2026-10-29: 131:0 -> 135:0."""
    import rtmidi
    rt = rtmidi.MidiIn(name=client)
    find = find or (lambda ports: next((p for p in ports if is_bus_port(p)), None))
    rt.ignore_types(sysex=True, timing=True, active_sense=True)
    name = None
    while True:
        try:
            ports = rt.get_ports()
        except Exception:
            ports = []
        now = find(ports)
        try:
            with open("/proc/asound/seq/clients") as f:
                text = f.read()
        except OSError:
            text = None
        fine = name is not None and now == name and (text is None or seq_listening(text, client, is_src))
        if name is not None and not fine:
            print("%s: %s lost (now %s), reconnecting" % (client, name, now), flush=True)
            try:
                rt.close_port()
            except Exception:
                pass
            name = None
        if name is None and now:
            try:
                rt.open_port(ports.index(now))
                try:
                    rt.cancel_callback()
                except Exception:
                    pass
                rt.set_callback(on_midi)
                name = now
                print("%s listening on %s" % (client, now), flush=True)
            except Exception as e:
                print("bus error %s, retrying" % e, flush=True)
        time.sleep(check_s)


# ── main loop ─────────────────────────────────────────────────────────────────
_c = {"mt": None, "d": None}


def feed():
    try:
        mt = os.stat(FEED).st_mtime_ns
        if mt != _c["mt"]:
            with open(FEED) as f:
                _c["d"] = json.load(f)
            _c["mt"] = mt
    except Exception:
        _c["d"] = None
    return _c["d"]


BLACK = "\n".join(("y 0", "x -1", "w 0 0 0", "c 000000"))
STALE_S = 3.0          # the brain rewrites the feed at least once a second: older = the brain is gone
FREE_BPM = 120.0       # FREE-RUN before the ORC has ever played: this tempo


def is_running(d, now):
    """Does the feed say the ORC is playing, and is the feed fresh? (A brain that dies while playing
    leaves `running: true` behind: until 2026-11-03 the conductor kept stepping on that frozen grid.)"""
    tr = (d or {}).get("transport", {})
    if not tr.get("running") or tr.get("step", -1) < 0:
        return False
    return now - float(d.get("t") or 0) <= STALE_S


def when_stopped():
    """black / hold / free (WHEN STOPPED, CC 70)."""
    return K.WHEN_STOPPED[K.pick(knob.get(70, 0), len(K.WHEN_STOPPED))]


def lock_grid(origin, step_s, o, s):
    """The step grid (origin = wall time of step 0) after one feed update that puts it
    at o. Start, a tempo change or a jump > 40 ms re-locks; small drift is followed
    smoothly (1/10 per update), so one late feed update cannot shove the picture."""
    if origin is None or step_s != s or abs(o - origin) > 0.040:
        return o, s
    return origin + (o - origin) * 0.1, step_s


def next_due(origin, step_s, lead, sent, now):
    """(the next step not yet sent, the wall time it is due). Never re-sends or goes back
    (sent + 1), and after a stall jumps to the current step instead of bursting the
    backlog. lead > 0 sends early, < 0 late."""
    k = max(sent + 1, int((now - origin + lead) / step_s))
    return k, origin + k * step_s - lead


def main(argv):
    lead = float(argv[1]) / 1000 if len(argv) > 1 else 0.012
    load_knobs()
    threading.Thread(target=bus_thread, daemon=True).start()                     # synth bus: drum hits
    for _client, _port in ((CTL_CLIENT, ORC_PORT), (VIS_CLIENT, ORC_VIS_PORT)):  # the ORC: knobs
        threading.Thread(target=bus_thread, daemon=True, kwargs={
            "client": _client, "is_src": (lambda p, _w=_port: p == _w),
            "find": (lambda ports, _w=_port: next((x for x in ports if port_part(x) == _w), None))}).start()
    threading.Thread(target=link_thread, daemon=True).start()
    threading.Thread(target=box_status_thread, daemon=True).start()
    threading.Thread(target=listen_thread, daemon=True).start()
    print("conductor -> box (cable %s or %s):%d, %d ms offset; waiting for PLAY"
          % (BOX_CABLE, BOX_NAME, BOX_PORT, -lead * 1000), flush=True)
    origin = step_s = None
    sent, was_running, last_cmd, free = -1, False, None, False
    while True:
        d = feed()
        b = (d or {}).get("bindings", {})
        with lock:
            state["drum_chs"] = {b[m] for m in DRUM_MODULES if isinstance(b.get(m), int)}
            state["my_ch"] = b.get(MODULE) if isinstance(b.get(MODULE), int) else None
            state["vis_ch"] = b.get("visuals") if isinstance(b.get("visuals"), int) else None
            if state["dirty"] and time.time() - state["dirty"] > 2.0:
                state["dirty"] = 0.0
                try:
                    save_knobs()
                except OSError as e:
                    print("could not save knobs:", e, flush=True)
            pc = pool_cmd(time.time())
            if pc:
                send(pc)
            lc = looks_cmd(time.time())
            if lc:
                send(lc)
            show = knob[7] > 0
            ac = alpha_cmd(time.time())
            if ac:
                send(ac)
            if show != state["show"]:
                state["show"] = show
                sent = -1                           # back on: the next step repaints (off just fades)
        tr = (d or {}).get("transport", {})
        if not is_running(d, time.time()) or not state["show"]:
            mode = when_stopped()
            if was_running:
                if state["show"] and mode == "black":   # the transport stopped: black. (SHOW off fades instead)
                    send(BLACK)
                was_running = False
            if state["show"] and mode == "free":
                # FREE-RUN: keep the last grid (or FREE_BPM) going on our own clock; no drum tracks
                if not free or step_s is None or origin is None:
                    step_s = step_s or 60.0 / FREE_BPM / 4
                    origin = origin if origin is not None else time.time() - (sent + 1) * step_s
                    free = True
                now = time.time()
                k, due = next_due(origin, step_s, lead, sent, now)
                if now >= due:
                    with lock:
                        cmd = picture(k, [])
                        if cmd:
                            send(cmd)
                    sent = k
                else:
                    time.sleep(min(0.02, due - now))
                continue
            free = False
            origin, sent = None, -1
            time.sleep(0.02)
            continue
        if free:                                    # the ORC plays again: its grid takes over at once
            free, origin, sent = False, None, -1
        was_running = True
        tracks = d.get("tracks", [])
        s = tr["step_ms"] / 1000.0
        state["bps"] = float(tr.get("bpm") or 115.0) / 60.0
        step_wall = float(d.get("t") or 0) - float(tr.get("age_ms") or 0) / 1000.0
        origin, step_s = lock_grid(origin, step_s, step_wall - tr["step"] * s, s)
        now = time.time()
        k, due = next_due(origin, step_s, lead, sent, now)
        if now >= due:
            with lock:
                cmd = picture(k, tracks)
                if cmd:
                    send(cmd)
            sent = k
            if cmd and k % 64 == 0 and cmd != last_cmd:      # a line every 4 bars, not every beat
                print("%.3f step %d %s" % (now, k, cmd.split("\n")[0]), flush=True)
                last_cmd = cmd
        else:
            time.sleep(min(0.004, due - now))


if __name__ == "__main__":
    main(sys.argv)
