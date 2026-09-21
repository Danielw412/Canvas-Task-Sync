"""Hand memory a finished sync no longer needs back to the operating system.

A single run briefly holds a lot at once: the source capture, base64 screenshots, the
Gemini request and response, and every remote task in the configured lists.  CPython
frees those objects as soon as the run ends, but glibc keeps the pages: freed blocks
below the trim threshold stay in the allocator, and each worker thread gets its own
arena that is never returned while the process lives.  The effect is a service that
climbs tens of megabytes per run and then sits at that size doing nothing.

``release_memory`` closes that gap.  It is advisory everywhere it is not supported --
non-glibc Linux, macOS, and Windows simply get the garbage collection -- so callers can
use it unconditionally.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import threading

# malloc_trim walks every arena, so two callers running it at once would duplicate the
# work for no benefit.
_TRIM_LOCK = threading.Lock()


def _load_malloc_trim():
    """Return glibc's malloc_trim, or None where the platform does not provide it."""
    for name in ("libc.so.6", ctypes.util.find_library("c")):
        if not name:
            continue
        try:
            trim = ctypes.CDLL(name).malloc_trim
        except (OSError, AttributeError):
            continue
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    return None


_MALLOC_TRIM = _load_malloc_trim()
TRIM_SUPPORTED = _MALLOC_TRIM is not None


def release_memory() -> bool:
    """Collect garbage, then return freed pages to the OS.

    Returns True when pages were actually handed back.  Callers treat a False as
    "nothing more to do here", never as an error.
    """
    gc.collect()
    if _MALLOC_TRIM is None:
        return False
    with _TRIM_LOCK:
        try:
            return bool(_MALLOC_TRIM(0))
        except OSError:  # pragma: no cover - a hostile libc, not a normal failure
            return False
