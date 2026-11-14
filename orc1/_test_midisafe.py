"""Guard for orc1_midisafe (2026-11-09): mido must not leak ALSA sequencer clients when a port vanishes.

Run ON THE PI (needs a real ALSA sequencer): python3 _test_midisafe.py [-v]
Each scenario runs in a FRESH child process and counts that process's open /dev/snd/seq handles, so nothing here
can be fooled by another process. The first check proves the test can see the bug (the same scenario WITHOUT the
fix must leak), so a broken fix and a blind test cannot both pass. Creates one short-lived virtual port of its own
('midisafe-test'); touches no ORC port or file.
"""
import os, subprocess, sys, json

EXPECTED = 9
HERE = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "-v" in sys.argv


def seq_handles():
    c = 0
    for f in os.listdir("/proc/self/fd"):
        try:
            if os.readlink("/proc/self/fd/" + f) == "/dev/snd/seq":
                c += 1
        except OSError:
            pass
    return c


def _child(scenario, fixed):
    """Runs in the child: returns a dict of measurements."""
    sys.path.insert(0, HERE)
    import gc, rtmidi, mido
    if fixed:
        import orc1_midisafe
        assert orc1_midisafe.install()
        assert orc1_midisafe.install()            # idempotent
    out = {"start": seq_handles()}
    if scenario == "listing_fails":
        real = rtmidi.MidiOut

        class Vanishing(real):                    # what ALSA does when a port goes away mid-listing
            def get_ports(self, *a, **k):
                raise rtmidi.SystemError("MidiOutAlsa::getPortName: error looking for port name!")
        rtmidi.MidiOut = Vanishing
        raised = 0
        for _ in range(3):
            try:
                mido.get_output_names()
            except Exception:
                raised += 1
        rtmidi.MidiOut = real
        out["raised"] = raised
    elif scenario == "open_gone_port":
        for _ in range(3):
            for opener in (mido.open_input, mido.open_output):
                try:
                    opener("midisafe-no-such-port 199:0")
                except Exception:
                    pass
    elif scenario == "normal":
        v = mido.open_output("midisafe-test", virtual=True)
        names = mido.get_input_names()
        mine = next((n for n in names if "midisafe-test" in n), None)
        out["found"] = mine is not None
        if mine:
            p = mido.open_input(mine)
            out["open_while"] = seq_handles()
            p.close()
        v.close()
        for _ in range(5):
            mido.get_input_names(); mido.get_output_names()
    gc.collect()
    out["end"] = seq_handles()
    return out


def run(scenario, fixed):
    r = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--child", scenario, "1" if fixed else "0"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"child {scenario} fixed={fixed} failed: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        if VERBOSE or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")

    bug = run("listing_fails", fixed=False)
    check("the test sees the bug: 3 failed listings WITHOUT the fix leak clients",
          bug["end"] - bug["start"] >= 3, str(bug))

    lf = run("listing_fails", fixed=True)
    check("failed listing still raises (the error is not swallowed)", lf.get("raised") == 3, str(lf))
    check("failed listing leaks nothing with the fix", lf["end"] == lf["start"], str(lf))

    og_bug = run("open_gone_port", fixed=False)
    check("the test sees the open bug: opening a gone port WITHOUT the fix leaks",
          og_bug["end"] - og_bug["start"] >= 3, str(og_bug))
    og = run("open_gone_port", fixed=True)
    check("opening a gone port leaks nothing with the fix", og["end"] == og["start"], str(og))

    nm = run("normal", fixed=True)
    check("normal: a port is found by name", nm.get("found") is True, str(nm))
    check("normal: an open port holds a client", nm.get("open_while", 0) > nm["start"], str(nm))
    check("normal: open/close + 10 listings leave nothing behind", nm["end"] == nm["start"], str(nm))

    # every ORC process that uses mido installs the fix before its first port call
    want = ["orc1.py", "orc1_zoomfx.py", "orc1_kikpad_link.py", "orc1_web.py", "orc1_drum_host.py",
            "orc1_poly_host.py", "orc1_chip_host.py", "orc1_mono_host.py", "orc1_sampler_host.py",
            "orc1_bbkit_host.py"]
    missing = [f for f in want if "orc1_midisafe.install(" not in open(os.path.join(HERE, f), encoding="utf-8").read()]
    check("every mido-using ORC process calls orc1_midisafe.install()", not missing, f"missing: {missing}")

    total = passed + failed
    print(f"_test_midisafe: {passed} passed, {failed} failed (expected {EXPECTED})")
    if total != EXPECTED:
        print(f"  COUNT DRIFT: ran {total}, expected {EXPECTED}")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        print(json.dumps(_child(sys.argv[2], sys.argv[3] == "1")))
        sys.exit(0)
    sys.exit(main())
