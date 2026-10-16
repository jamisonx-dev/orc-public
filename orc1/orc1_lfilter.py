"""scipy's C lfilter WITHOUT importing scipy.signal (2026-09-23).

Every audio host filters (drum voices, poly, mono, NES/chip, sampler EQ/sweep) via
`scipy.signal.lfilter`. Importing `scipy.signal` just for that loads ~530 scipy
modules and costs ~48 MB of PRIVATE memory per process -- measured on the Pi --
x6 hosts ~ 250 MB, on a box whose skipback RAM guard sits near a 400 MB floor.

The filter itself is one small compiled function, `scipy/signal/_sigtools.*.so`
`_linear_filter`. This module loads THAT extension file directly (0.1 MB, zero
scipy modules) and calls it exactly as `scipy.signal.lfilter` does for an IIR
filter (len(a) > 1):  `_sigtools._linear_filter(b, a, x, axis, zi)` on
np.atleast_1d(b/a) + np.asarray(x/zi) -- no other preprocessing (read from
scipy 1.17.1's lfilter source). The output is BIT-IDENTICAL to scipy's
(_test_lfilter_lite.py proves it against the real lfilter).

Robust by construction: if the direct load fails (a scipy upgrade renames or
moves the private file, or scipy is absent), `lfilter` falls back to the real
`scipy.signal.lfilter`, and to None if scipy is missing entirely -- the callers
already treat None as "filter bypassed". The FIR case (len(a) == 1), which none
of our biquads use, always goes to the real scipy path so it stays exact.
"""
import numpy as np

_linear_filter = None
_scipy_lfilter = None
SOURCE = "none"            # "direct" | "scipy" | "none" -- for logs / tests


def _load_direct():
    import glob
    import importlib.util
    import os
    spec0 = importlib.util.find_spec("scipy")   # locates, does NOT import scipy
    if spec0 is None or not spec0.origin:
        return None
    hits = glob.glob(os.path.join(os.path.dirname(spec0.origin),
                                  "signal", "_sigtools*.so"))
    hits += glob.glob(os.path.join(os.path.dirname(spec0.origin),
                                   "signal", "_sigtools*.pyd"))
    if not hits:
        return None
    # An extension module must load under its REAL name (PyInit__sigtools).
    spec = importlib.util.spec_from_file_location("_sigtools", hits[0])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._linear_filter


def _full_scipy():
    global _scipy_lfilter
    if _scipy_lfilter is None:
        from scipy.signal import lfilter as _lf
        _scipy_lfilter = _lf
    return _scipy_lfilter


def _direct_lfilter(b, a, x, axis=-1, zi=None):
    b = np.atleast_1d(b)
    a = np.atleast_1d(a)
    if len(a) == 1:                        # FIR: not our case -> exact scipy path
        return _full_scipy()(b, a, x, axis=axis, zi=zi)
    x = np.asarray(x)
    if zi is None:
        return _linear_filter(b, a, x, axis)
    return _linear_filter(b, a, x, axis, np.asarray(zi))


try:
    _linear_filter = _load_direct()
except Exception:
    _linear_filter = None

if _linear_filter is not None:
    lfilter = _direct_lfilter
    SOURCE = "direct"
else:
    try:
        lfilter = _full_scipy()
        SOURCE = "scipy"
    except Exception:
        lfilter = None
        SOURCE = "none"
