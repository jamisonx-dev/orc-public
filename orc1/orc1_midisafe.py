"""orc1_midisafe: stop mido leaking ALSA sequencer clients (2026-11-09).

THE BUG (measured on the Pi): python-rtmidi frees an ALSA client ONLY on an explicit `.delete()`; garbage
collection never does (a `del` of an unused rtmidi.MidiIn leaves its /dev/snd/seq handle open). mido 1.3.3's rtmidi
backend calls `.delete()` only on the success path:
  - `get_devices()` (behind every mido.get_input_names / get_output_names) makes a MidiIn + a MidiOut, lists, then
    deletes them. When a port vanishes DURING the listing (a brain restart drops ORC1-Synth-Bus, a Fire crash or
    replug, any client coming and going), `get_ports()` raises "getPortName: error looking for port name!" and
    BOTH clients stay open for good.
  - `Input._open` / `Output._open` make the client first, then raise "unknown port" if the port went away: one more.
Linux allows 64 user sequencer clients. On 2026-11-09 ~43 leaked ones filled them in ~5 hours of uptime: after
that NOTHING could open MIDI ("Cannot allocate memory"), so a replugged Fire could not be reopened, the Fire watcher
went blind and a firmware flash could not find the Fire again.

THE FIX: install() swaps in a get_devices() that deletes its clients in a `finally`, and wraps the port classes'
_open so a failed open deletes the client it made. Behaviour is otherwise identical. Every ORC process that uses
mido calls install() once, before its first port call. Processes that drive rtmidi directly (the DAW bridge,
bsplights, visuals, the box conductor) keep ONE client for their whole run and are not affected.
Guard: _test_midisafe.py (run on the Pi: it needs a real ALSA sequencer).
"""

_installed = False


def _delete(rt):
    if rt is None:
        return
    try:
        rt.delete()
    except Exception:
        pass


def install(log=None):
    """Patch mido's rtmidi backend. Idempotent. Returns True when patched (or already patched), False when
    mido / rtmidi are missing or the backend is not the shape this fix was written for (then nothing is changed)."""
    global _installed
    if _installed:
        return True
    try:
        import rtmidi
        import mido.backends.rtmidi as be
    except Exception:
        return False
    needed = ("get_devices", "_get_api_id", "Input", "Output")
    if not all(hasattr(be, n) for n in needed) or not all(hasattr(c, "_open") for c in (be.Input, be.Output)):
        if log:
            log("[midisafe] mido's rtmidi backend is not the expected shape: NOT patched (clients may leak)")
        return False

    def get_devices(api=None, **kwargs):
        rtapi = be._get_api_id(api)
        mi = mo = None
        try:
            mi = rtmidi.MidiIn(rtapi=rtapi)
            mo = rtmidi.MidiOut(rtapi=rtapi)
            input_names = mi.get_ports()
            output_names = mo.get_ports()
        finally:
            _delete(mi)
            _delete(mo)
        devices = {}
        for name in input_names + output_names:
            if name not in devices:
                devices[name] = {"name": name,
                                 "is_input": name in input_names,
                                 "is_output": name in output_names}
        return list(devices.values())

    def safe_open(orig):
        def _open(self, *a, **k):
            try:
                return orig(self, *a, **k)
            except BaseException:
                _delete(getattr(self, "_rt", None))
                self._rt = None
                raise
        _open._orc1_midisafe = True
        return _open

    be.get_devices = get_devices
    for cls in (be.Input, be.Output):
        if not getattr(cls._open, "_orc1_midisafe", False):
            cls._open = safe_open(cls._open)
    _installed = True
    return True
