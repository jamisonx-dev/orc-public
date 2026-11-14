#!/usr/bin/env python3
"""
The REAPER OSC dialect (orc1_dawsurface_osc): the stdlib OSC 1.0 codec, the adapter against canned
REAPER OSC traffic (tracks by absolute number, the list end, 0 dB learning, pages), and the pattern
file orc1/daw/ORC.ReaperOSC (every address the adapter speaks is in it, its bank size = cfg's).

    python3 _test_dawsurface_osc.py [-v]        (PC: py _test_dawsurface_osc.py)

Pure: no socket, no port, no file written. HOME + USERPROFILE point at a temp dir before importing.
On the Pi it also checks the pattern file's action names against REAPER's own Default.ReaperOSC.
"""
import os
import re
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 65


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


SANDBOX = tempfile.mkdtemp(prefix="dawosc_test_")
os.environ["HOME"] = SANDBOX
os.environ["USERPROFILE"] = SANDBOX
sys.path.insert(0, HERE)
try:
    import orc1_config as cfg                       # noqa: E402
    import orc1_dawsurface as D                     # noqa: E402
    import orc1_dawsurface_osc as O                 # noqa: E402

    # ---- 1. the codec -------------------------------------------------------------------------
    check("exact bytes: /play, no arguments", O.osc_message("/play") == b"/play\0\0\0,\0\0\0")
    check("exact bytes: /track/1/volume 0.5 (big-endian float32)",
          O.osc_message("/track/1/volume", (0.5,)) == b"/track/1/volume\0,f\0\0" + struct.pack(">f", 0.5))
    check("exact bytes: /action 41743 (int32)", O.osc_message("/action", (41743,)) ==
          b"/action\0,i\0\0" + struct.pack(">i", 41743))
    pad_ok = True
    for n in range(0, 9):                                # strings of every length pad to 4 with >= 1 NUL
        s = "x" * n
        b = O.osc_message("/a", (s,))
        body = b[8:]                                     # "/a\0\0" + ",s\0\0"
        pad_ok = pad_ok and len(body) % 4 == 0 and body[:n] == s.encode() and body[n] == 0 \
            and len(body) == (n // 4 + 1) * 4 and O.osc_decode(b) == [("/a", [s])]
    check("strings of length 0-8: NUL-terminated, padded to 4, round trip", pad_ok)
    for addr in ("/x", "/xy", "/xyz", "/wxyz"):
        check("address %r pads right and round-trips" % addr,
              len(O.osc_message(addr)) % 4 == 0 and O.osc_decode(O.osc_message(addr)) == [(addr, [])])
    args = (1, -2, 0.25, "Bass", True, False, None, b"\x01\x02\x03", ("d", 1.5), ("h", 2 ** 40))
    back = O.osc_decode(O.osc_message("/mix", args))[0][1]
    check("mixed args round trip (i, f, s, T, F, N, b, d, h)",
          back == [1, -2, 0.25, "Bass", True, False, None, b"\x01\x02\x03", 1.5, 2 ** 40], back)
    check("bool goes as T/F, not as an int", O.osc_message("/b", (True,))[4:8] == b",T\0\0")
    check("an int / float are kept apart", O.osc_message("/v", (1,))[4:8] == b",i\0\0"
          and O.osc_message("/v", (1.0,))[4:8] == b",f\0\0")
    check("a forced type: ('f', 1) sends a float", O.osc_decode(O.osc_message("/v", (("f", 1),)))[0][1] == [1.0])
    check("utf-8 names survive", O.osc_decode(O.osc_message("/track/1/name", ("Bäss",)))[0][1] == ["Bäss"])
    m1, m2, m3 = O.osc_message("/a", (1,)), O.osc_message("/b", ("x",)), O.osc_message("/c")
    nested = O.osc_bundle([m1, O.osc_bundle([m2, m3])])
    check("bundles (nested) flatten in order", O.osc_decode(nested) == [("/a", [1]), ("/b", ["x"]), ("/c", [])])
    check("a bundle header: '#bundle', timetag 1 = immediately",
          nested[:8] == b"#bundle\0" and struct.unpack(">Q", nested[8:16])[0] == 1)
    check("an old-style message with no type tags = no arguments", O.osc_decode(b"/old\0\0\0\0") == [("/old", [])])
    for bad, why in ((b"/a\0\0,i\0\0\0\0", "int cut short"), (b"/a", "unterminated address"),
                     (b"zz\0\0", "no leading /"), (b"/a\0\0x\0\0\0", "type tags without ','"),
                     (b"/a\0\0,q\0\0", "unknown type tag"),
                     (b"#bundle\0" + b"\0" * 8 + struct.pack(">i", 99) + b"/a\0\0", "element past the end")):
        check("malformed: %s -> OscError" % why, raises(lambda: O.osc_decode(bad), O.OscError))
    check("can't send an unknown Python type", raises(lambda: O.osc_message("/a", (object(),)), O.OscError))
    check("an address must start with /", raises(lambda: O.osc_message("a"), O.OscError))

    # ---- 2. the adapter: canned REAPER traffic -------------------------------------------------
    A = O.OscAdapter(512)
    S = D.DawSurface(A)
    evs = []
    S.subscribe(evs.append)

    def rx(addr, *args):
        return S.receive((addr, args))

    check("512 strips + master, the host doesn't bank (surface_banking)",
          len(S.strips) == 512 and S.master is not None and A.surface_banking)
    for t in range(1, 6):
        rx("/track/%d/number/str" % t, str(t))
        rx("/track/%d/name" % t, ["Kick", "Snare", "", "Bass", "Keys"][t - 1])
    rx("/track/300/name", "")
    check("tracks by ABSOLUTE number: /track/4/name -> strip 3", S.strips[3].name == "Bass")
    check("an unnamed track still exists (its number came)", A.exists[2] and S.strips[2].name == "")
    check("track count = 1 + the highest slot holding a track", A.track_count() == 5)
    rx("/track/6/number/str", "")
    check("an empty number past the end = no track (the list end stays 5)", A.track_count() == 5 and not A.exists[5])
    rx("/track/5/number/str", "")
    check("a deleted last track: its number goes empty -> 4", A.track_count() == 4)
    rx("/track/400/name", "Strings")
    check("a name with no number = a track there (strip 399)", A.exists[399] and A.track_count() == 400)
    rx("/track/400/name", "")
    check("...its name going empty = gone again", A.track_count() == 4)
    rx("/track/2/volume", 0.25)
    rx("/track/2/pan", 0.75)
    rx("/track/2/mute", 1.0)
    rx("/track/2/solo", 1)
    rx("/track/2/recarm", 0.0)
    rx("/track/2/select", 1.0)
    rx("/track/2/vu", 0.5)
    s2 = S.strips[1]
    check("volume / pan / mute / solo / rec / select / vu land on strip 1",
          (s2.fader, s2.pan, s2.mute, s2.solo, s2.rec, s2.select, s2.meter)
          == (0.25, 0.75, True, True, False, True, 0.5), s2.as_dict())
    rx("/master/volume", 0.6)
    rx("/master/vu", 0.1)
    check("master volume + vu", S.master.fader == 0.6 and S.master.meter == 0.1)
    rx("/play", 1.0)
    rx("/repeat", 1.0)
    rx("/rewind", 0.0)
    check("transport feedback: play, repeat = loop, rewind",
          S.transport["play"] is True and S.transport["loop"] is True and S.transport["rewind"] is False)
    n = len(evs)
    for junk in (("/track/513/name", ("x",)), ("/track/0/name", ("x",)), ("/tempo/raw", (120.0,)),
                 ("/track/1/volume", ("loud",)), (5, ()), ()):
        S.receive(junk)
    check("ignored: track 513 of 512, track 0, unknown address, a volume without a number, junk",
          len(evs) == n and S.strips[0].name == "Kick", evs[n:])

    # 0 dB learning
    check("0 dB unknown before REAPER reports one", A.unity01 is None)
    rx("/track/3/volume", 0.71598)
    rx("/track/3/volume/db", 0.0)
    check("a (volume, volume/db = 0.0) pair teaches 0 dB's position", A.unity01 == 0.71598)
    rx("/track/3/volume/db", 2.5)
    rx("/track/3/volume", 0.8)
    check("a pair at +2.5 dB teaches nothing", A.unity01 == 0.71598)
    rx("/track/4/volume", 0.5)
    rx("/track/1/volume/db", 0.0)
    check("halves from DIFFERENT tracks never pair", A.unity01 == 0.71598)
    rx("/track/1/volume/db", 0.0)
    rx("/track/1/volume", 0.7)
    check("either order pairs (db first, then volume)", A.unity01 == 0.7)

    # ---- 3. gestures -> REAPER ------------------------------------------------------------------
    S.send = None
    check("fader -> /track/N/volume (normalized float)", S.set_fader(2, 0.5) == [(0, ("/track/3/volume", (0.5,)))])
    check("fader in dB -> /track/N/volume/db (the model keeps the position)",
          S.set_fader_db(2, 0.0, 0.7) == [(0, ("/track/3/volume/db", (0.0,)))] and S.strips[2].fader == 0.7)
    check("master: no dB pattern -> the position instead",
          S.set_fader_db(D.MASTER, 0.0, 0.7) == [(0, ("/master/volume", (0.7,)))])
    check("touch -> /track/N/volume/touch 1 / 0", S.touch(0, True) == [(0, ("/track/1/volume/touch", (1,)))]
          and S.touch(0, False) == [(0, ("/track/1/volume/touch", (0,)))] and S.touch(D.MASTER) == [])
    check("a mute press = ONE toggle (the release sends nothing)", S.press("mute", 0) == [(0, ("/track/1/mute/toggle", ()))])
    check("rec -> recarm/toggle", S.press("rec", 4) == [(0, ("/track/5/recarm/toggle", ()))])
    check("select toggles REAPER's known state (strip 1 is selected -> 0)",
          S.press("select", 1) == [(0, ("/track/2/select", (0,)))] and S.press("select", 0) == [(0, ("/track/1/select", (1,)))])
    check("play / stop / record / loop: one trigger per press",
          [S.press(n)[0][1][0] for n in ("play", "stop", "record", "loop")] == ["/play", "/stop", "/record", "/repeat"])
    check("rewind / fast_fwd held: 1 on press, 0 on release",
          S.button("rewind", None, True) == [(0, ("/rewind", (1,)))] and S.button("fast_fwd", None, False) == [(0, ("/forward", (0,)))])
    check("bank / channel send NOTHING (the bridge's views do the banking)",
          S.bank(1) == [] and S.channel(-1) == [])
    r = S.turn(1, 2)
    check("a V-pot turn steps the pan from REAPER's last (0.75 + 2 %)",
          len(r) == 1 and r[0][1][0] == "/track/2/pan" and abs(r[0][1][1][0] - 0.77) < 1e-9, r)
    check("an absolute pan -> /track/N/pan", S.set_pan(3, 0.25) == [(0, ("/track/4/pan", (0.25,)))]
          and S.strips[3].pan == 0.25)
    check("jog -> /scrub", S.jog(-3) == [(0, ("/scrub", (-3.0,)))])
    hello = A.hello()
    check("hello = /device/track/count 512, bank 1, action 41743 (Refresh all surfaces)",
          hello == [(0, ("/device/track/count", (512,))), (0, ("/device/track/bank/select", (1,))),
                    (0, ("/action", (41743,)))])
    check("every message the adapter makes encodes", all(O.encode(m) for _p, m in hello))
    check("strip past the last refused", raises(lambda: S.set_fader(512, 0.1), IndexError))
    msgs = A.set_page(2)
    check("page 3 = REAPER bank 3 + a refresh, the page's knowledge forgotten",
          msgs == [(0, ("/device/track/bank/select", (3,))), (0, ("/action", (41743,)))]
          and A.track_count() == 0 and A.track_number(0) == 1025 and A.unity01 == 0.7)
    rx("/track/512/number/str", "1536")
    check("a full page (its last strip holds a track) says there may be more", A.page_full())

    # ---- 4. the pattern file ------------------------------------------------------------------------
    pf = os.path.join(HERE, "daw", "ORC.ReaperOSC")
    with open(pf) as fh:
        lines = [ln.split("#")[0].split() for ln in fh]
    lines = [ln for ln in lines if ln]
    settings = {ln[0]: ln[1] for ln in lines if len(ln) == 2 and "/" not in ln[1]}
    patterns = {}
    for ln in lines:
        for p in ln[1:]:
            if "/" in p:
                patterns[p[1:]] = (p[0], ln[0])
    check("pattern file: DEVICE_TRACK_COUNT = cfg.DAWSURF_OSC_TRACKS",
          int(settings.get("DEVICE_TRACK_COUNT", -1)) == cfg.DAWSURF_OSC_TRACKS, settings)
    check("pattern file: the bank follows the DEVICE (only the ORC moves it)",
          settings.get("DEVICE_TRACK_BANK_FOLLOWS") == "DEVICE")
    # every address the adapter sends or reads, with @ for the track number
    used = {"/track/@/name": "s", "/track/@/number/str": "s", "/track/@/volume": "n", "/track/@/volume/db": "f",
            "/track/@/volume/touch": "b", "/track/@/pan": "n", "/track/@/mute": "b", "/track/@/solo": "b",
            "/track/@/recarm": "b", "/track/@/select": "b", "/track/@/vu": "n", "/track/@/mute/toggle": "t",
            "/track/@/solo/toggle": "t", "/track/@/recarm/toggle": "t", "/master/volume": "n", "/master/vu": "n",
            "/master/pan": "n", "/play": "t", "/stop": "t", "/record": "t", "/repeat": "t", "/rewind": "b",
            "/forward": "b", "/scrub": "r", "/action": "i", "/device/track/count": "i",
            "/device/track/bank/select": "i"}
    missing = sorted(a for a, t in used.items() if patterns.get(a, (None,))[0] != t)
    check("pattern file: every address the adapter speaks is there with its flag", not missing, missing)
    re_ok = all(O._TRACK.match(a.replace("@", "7")) for a in patterns if a.startswith("/track/@/")
                and not a.endswith(("/toggle", "/touch")))
    check("the adapter decodes every track feedback pattern the file asks REAPER for", re_ok)
    # REAPER 7.81's action names (Default.ReaperOSC, read on the Pi 2026-11-09): a typo'd name = ignored by REAPER
    reaper_names = {"DEVICE_TRACK_COUNT", "DEVICE_SEND_COUNT", "DEVICE_RECEIVE_COUNT", "DEVICE_FX_COUNT",
                    "DEVICE_FX_PARAM_COUNT", "DEVICE_FX_INST_PARAM_COUNT", "DEVICE_MARKER_COUNT",
                    "DEVICE_REGION_COUNT", "REAPER_TRACK_FOLLOWS", "DEVICE_TRACK_FOLLOWS",
                    "DEVICE_TRACK_BANK_FOLLOWS", "DEVICE_FX_FOLLOWS", "DEVICE_EQ", "DEVICE_ROTARY_CENTER",
                    "RECORD", "STOP", "PLAY", "PAUSE", "REPEAT", "REWIND", "FORWARD", "SCRUB", "MASTER_VOLUME",
                    "MASTER_PAN", "MASTER_VU", "TRACK_NAME", "TRACK_NUMBER", "TRACK_MUTE", "TRACK_SOLO",
                    "TRACK_REC_ARM", "TRACK_SELECT", "TRACK_VOLUME", "TRACK_VOLUME_TOUCH", "TRACK_PAN",
                    "TRACK_VU", "ACTION", "DEVICE_TRACK_BANK_SELECT"}
    default = "/opt/REAPER/Plugins/Default.ReaperOSC"
    if os.path.exists(default):
        with open(default) as fh:
            reaper_names = {ln.split()[0] for ln in fh if ln.strip() and not ln.startswith("#")}
        src = "REAPER's own Default.ReaperOSC"
    else:
        src = "the names copied from REAPER 7.81"
    unknown = sorted({ln[0] for ln in lines} - reaper_names)
    check("pattern file: every action name is one of %s" % src, not unknown, unknown)
    core = open(os.path.join(HERE, "orc1_dawsurface_osc.py")).read()
    check("the dialect opens no socket and starts no thread", "socket" not in re.sub(r'"""[\s\S]*?"""', "", core)
          and "threading" not in core)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

total = PASS + FAIL
print("%d passed, %d failed" % (PASS, FAIL))
if total != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (total, EXPECTED))
    sys.exit(1)
sys.exit(1 if FAIL else 0)
