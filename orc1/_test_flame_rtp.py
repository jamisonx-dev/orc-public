"""Guard for orc1_flame_rtp (2026-11-13): rtpMIDI "FLAME rtp" <-> the Fire's DAW port, both ways.

Pure: feeds aconnect -l listings (shaped like the Pi's real one, 2026-11-13) to plan(); touches no port or file.
Run: python3 _test_flame_rtp.py
"""
import os, sys

EXPECTED = 13
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_config as cfg
import orc1_flame_rtp as fr

passed = failed = 0


def check(name, ok, info=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {info}")


FIRE = """client 36: 'FLAME' [type=kernel,card=5]
    0 'FLAME seq       '
\tConnecting To: 149:0, 137:0
\tConnected From: 131:0[real:0], 147:0[real:0]
    1 'FLAME DAW       '
"""
RTP = """client 138: 'rtpmidid' [type=user,pid=1183]
    0 'Network         '
    1 'owner (2)     '
\tConnecting To: 155:0
    2 'ORC DAW         '
    3 'FLAME rtp       '
"""
OTHER = """client 155: 'RtMidiIn Client' [type=user,pid=858]
    0 'RtMidi input    '
\tConnected From: 138:1
"""


def main():
    check("both there, unconnected: both directions", fr.plan(FIRE + RTP + OTHER) == [("138:3", "36:1"), ("36:1", "138:3")],
          str(fr.plan(FIRE + RTP + OTHER)))
    check("no Fire: nothing", fr.plan(RTP + OTHER) == [])
    check("no FLAME rtp session: nothing", fr.plan(FIRE + RTP.replace("FLAME rtp", "Other one") + OTHER) == [])
    wired = (FIRE.replace("    1 'FLAME DAW       '\n", "    1 'FLAME DAW       '\n\tConnecting To: 138:3\n")
             + RTP.replace("    3 'FLAME rtp       '\n", "    3 'FLAME rtp       '\n\tConnecting To: 36:1[real:0]\n"))
    check("already wired both ways: nothing", fr.plan(wired + OTHER) == [], str(fr.plan(wired + OTHER)))
    half = FIRE + RTP.replace("    3 'FLAME rtp       '\n", "    3 'FLAME rtp       '\n\tConnecting To: 36:1\n")
    check("one way wired: only the missing one", fr.plan(half) == [("36:1", "138:3")], str(fr.plan(half)))
    check("the Fire's seq port is never the DAW port", fr.find(fr.parse(FIRE + RTP))[1] == "36:1")
    check("ORC DAW (the Pi's own bridge) is never taken", fr.find(fr.parse(FIRE + RTP.replace("FLAME rtp", "xx")))[0] is None)
    p0 = "client 138: 'rtpmidid' [type=user,pid=1183]\n    0 'FLAME rtp'\n"
    check("rtpmidid's port 0 is never the session", fr.find(fr.parse(FIRE + p0))[0] is None)
    fake = "client 200: 'RtMidiOut Client' [type=user]\n    1 'FLAME rtp'\n"
    check("only rtpmidid's port counts as the session", fr.find(fr.parse(FIRE + fake))[0] is None)
    rtpfire = "client 138: 'rtpmidid' [type=user,pid=1183]\n    4 'FLAME DAW x'\n"
    check("an rtpmidid port is never taken for the Fire", fr.find(fr.parse(rtpfire))[1] is None)
    # names: the brain ignores the session, and no Fire matcher claims it
    check("brain skips FLAME rtp", any(s in "flame rtp" for s in cfg.DAWSURF_BRAIN_SKIP) and "flame rtp" in cfg.DAWSURF_BRAIN_SKIP)
    n = cfg.FLAME_RTP_PORT_NAMES[0]
    check("no Fire matcher claims the session name",
          not cfg.is_fire_port(n) and not fr._has(n, cfg.FIRE_DAW_PORT_NAMES) and not fr._has(n, cfg.FIRE_SEQ_PORT_NAMES))
    check("the Pi's DAW bridge does not claim it", not fr._has(n, cfg.DAWSURF_PORT_NAMES))

    total = passed + failed
    print(f"_test_flame_rtp: {passed} passed, {failed} failed (expected {EXPECTED})")
    if total != EXPECTED:
        print(f"  COUNT DRIFT: ran {total}, expected {EXPECTED}")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
