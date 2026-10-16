#!/usr/bin/env python3
"""daw_close.py -- ask the DAW/DJ-mode app to quit, like clicking its close box.

    DISPLAY=:0 daw_close.py [CLASS]   exit 0 = asked, 1 = no such window
                                      CLASS = WM_CLASS, any case (default
                                      REAPER; DJ mode passes mixxx)

Sends WM_DELETE_WINDOW to the app's main window(s). REAPER then quits the
normal way (Mixxx likewise; it asks first if a deck is playing): it asks on the monitor about unsaved edits
and closes its JACK client cleanly. Killing it instead (systemctl stop ->
SIGTERM) left jackd saying "client = REAPER was not finished" = an xrun on
the whole graph (measured 2026-09-29), which skipback counts.

Pure ctypes on libX11 (no xdotool/wmctrl on the Pi).
"""
import ctypes
import ctypes.util
import sys

X = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
Window = ctypes.c_ulong
Atom = ctypes.c_ulong


class XClassHint(ctypes.Structure):
    # c_void_p, not c_char_p: XFree needs X's own pointers back (c_char_p
    # hands Python a copy, and freeing that aborts the process)
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", Window), ("message_type", Atom),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("xclient", XClientMessageEvent), ("pad", ctypes.c_long * 24)]


X.XOpenDisplay.restype = ctypes.c_void_p
X.XOpenDisplay.argtypes = [ctypes.c_char_p]
X.XDefaultRootWindow.restype = Window
X.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
X.XInternAtom.restype = Atom
X.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
X.XQueryTree.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(Window),
                         ctypes.POINTER(Window),
                         ctypes.POINTER(ctypes.POINTER(Window)),
                         ctypes.POINTER(ctypes.c_uint)]
X.XGetClassHint.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(XClassHint)]
X.XSendEvent.argtypes = [ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_long,
                         ctypes.POINTER(XEvent)]
X.XFree.argtypes = [ctypes.c_void_p]
X.XFlush.argtypes = [ctypes.c_void_p]
X.XCloseDisplay.argtypes = [ctypes.c_void_p]

CLIENT_MESSAGE = 33


def children(d, w):
    root, parent = Window(), Window()
    kids, n = ctypes.POINTER(Window)(), ctypes.c_uint()
    if not X.XQueryTree(d, w, ctypes.byref(root), ctypes.byref(parent),
                        ctypes.byref(kids), ctypes.byref(n)):
        return []
    out = [kids[i] for i in range(n.value)]
    if kids:
        X.XFree(kids)
    return out


def app_windows(d, root, want):
    """Top-level-ish windows whose WM_CLASS class is `want`, any case
    (depth <= 2: under openbox a client window sits inside a frame)."""
    found = []
    for w in children(d, root):
        for c in [w] + children(d, w):
            h = XClassHint()
            if X.XGetClassHint(d, c, ctypes.byref(h)):
                cls = (ctypes.string_at(h.res_class) if h.res_class
                       else b"").decode(errors="replace")
                if h.res_name:
                    X.XFree(h.res_name)
                if h.res_class:
                    X.XFree(h.res_class)
                if cls.upper() == want.upper():
                    found.append(c)
    return found


def main():
    d = X.XOpenDisplay(None)
    if not d:
        print("daw_close: cannot open the X display (is DAW mode running?)",
              file=sys.stderr)
        return 1
    try:
        root = X.XDefaultRootWindow(d)
        want = sys.argv[1] if len(sys.argv) > 1 else "REAPER"
        wins = app_windows(d, root, want)
        if not wins:
            print(f"daw_close: no {want} window found", file=sys.stderr)
            return 1
        protos = X.XInternAtom(d, b"WM_PROTOCOLS", 0)
        delete = X.XInternAtom(d, b"WM_DELETE_WINDOW", 0)
        for w in wins:
            ev = XEvent()
            ev.xclient.type = CLIENT_MESSAGE
            ev.xclient.window = w
            ev.xclient.message_type = protos
            ev.xclient.format = 32
            ev.xclient.data[0] = delete
            ev.xclient.data[1] = 0              # CurrentTime
            X.XSendEvent(d, w, 0, 0, ctypes.byref(ev))
        X.XFlush(d)
        print(f"daw_close: asked {len(wins)} {want} window(s) to close")
        return 0
    finally:
        X.XCloseDisplay(d)


if __name__ == "__main__":
    sys.exit(main())
