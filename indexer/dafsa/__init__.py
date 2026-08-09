"""Python ctypes frontend to the C DAFSA core.

Loads the C DAFSA shared library (built from the sources in this directory) and
exposes its API to Python: create/load/save, length-delimited add/lookup/delete,
prefix enumeration (used for search), statistics, and ABI version checking.

The library is lazy-loaded on first use so that ``import indexer`` does not fail
when ``libdafsa.so`` is missing — only DAFSA operations do.

Read-only paths (``Dafsa.load(path, readonly=True)``) use the zero-copy
``dafsa_view_open`` mmap view, which is much lighter than materializing the
entire state table.  The view handle is search-only: mutation and persistence
are not available on a view, and ``free()`` calls ``dafsa_view_close``.

Build the shared lib once with:
    make  (see Makefile in this directory)
or:
    gcc -shared -fPIC -O2 -o libdafsa.so dafsa.c dafsa_state.c dafsa_core.c \\
        dafsa_persist.c dafsa_view.c
"""

import ctypes
import ctypes.util
import sys
import threading
from pathlib import Path

_LIB_PATH = Path(__file__).parent / "libdafsa.so"

# Matches MAX_WORD_LEN in dafsa.c. Keys longer than this are rejected by the C
# core (returns -1); guard here too so we never build an oversized buffer.
_MAX_WORD_LEN = 4096

# Must match DAFSA_ABI_VERSION in dafsa.h.  If a stale .so is present, the
# version probe catches the mismatch before any calls are made.
_ABI_VERSION = 1

# Enumerate callback: return 0 to continue, non-zero to stop early.
_ENUM_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_void_p)


class DafsaStatsOut(ctypes.Structure):
    """ctypes mirror of the C dafsa_stats_out struct (dafsa.h:62-68)."""
    _fields_ = [
        ("n_states_total", ctypes.c_uint32),
        ("n_states_reachable", ctypes.c_uint32),
        ("n_final", ctypes.c_uint32),
        ("n_trans", ctypes.c_uint32),
        ("register_probes", ctypes.c_uint64),
    ]


def _load() -> ctypes.CDLL:
    """Find and load libdafsa.so, declare all ctypes signatures, and verify ABI.

    Raises RuntimeError if the library is not found or the ABI version
    mismatches.
    """
    path = _LIB_PATH if _LIB_PATH.exists() else ctypes.util.find_library("dafsa")
    if not path:
        raise RuntimeError(
            "libdafsa.so not found. Build it in indexer/dafsa with: "
            "make  (or see Makefile for the full gcc incantation)"
        )
    lib = ctypes.CDLL(str(path))

    # ── ABI version probe (must happen first) ──
    lib.dafsa_abi_version.argtypes = []
    lib.dafsa_abi_version.restype = ctypes.c_uint32
    abi = lib.dafsa_abi_version()
    if abi != _ABI_VERSION:
        raise RuntimeError(
            f"libdafsa.so ABI mismatch: expected {_ABI_VERSION}, got {abi}; "
            "rebuild it with 'make' in indexer/dafsa"
        )

    # ── Lifecycle ──
    lib.dafsa_create.restype = ctypes.c_void_p
    lib.dafsa_free.argtypes = [ctypes.c_void_p]

    # ── Persistence ──
    lib.dafsa_load.argtypes = [ctypes.c_char_p]
    lib.dafsa_load.restype = ctypes.c_void_p
    lib.dafsa_save.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.dafsa_save.restype = ctypes.c_int

    # ── Key ops (length-delimited) ──
    lib.dafsa_add_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_add_n.restype = ctypes.c_int
    lib.dafsa_lookup_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_lookup_n.restype = ctypes.c_int
    lib.dafsa_delete_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_delete_n.restype = ctypes.c_int

    # ── Prefix enumeration ──
    lib.dafsa_prefix_enum.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, _ENUM_CB, ctypes.c_void_p,
    ]
    lib.dafsa_prefix_enum.restype = ctypes.c_long

    # ── Zero-copy search-only view (M4) ──
    lib.dafsa_view_open.argtypes = [ctypes.c_char_p]
    lib.dafsa_view_open.restype = ctypes.c_void_p
    lib.dafsa_view_close.argtypes = [ctypes.c_void_p]
    lib.dafsa_view_lookup_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_view_lookup_n.restype = ctypes.c_int
    lib.dafsa_view_prefix_enum.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, _ENUM_CB, ctypes.c_void_p,
    ]
    lib.dafsa_view_prefix_enum.restype = ctypes.c_long

    # ── Statistics ──
    lib.dafsa_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(DafsaStatsOut)]
    lib.dafsa_stats.restype = None

    return lib


# Lazy-loaded: _load() is called on first use, not at import time.
# This means ``import indexer`` does not fail when libdafsa.so is missing.
_lib = None
_lib_lock = threading.Lock()


def _get_lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        with _lib_lock:
            if _lib is None:
                _lib = _load()
    return _lib


class Dafsa:
    """Python wrapper around the opaque C DAFSA handle.

    Handles may be either mutable (from ``create()`` or ``load(readonly=False)``)
    or a zero-copy search-only view (from ``load(readonly=True)``).  Views use
    ``dafsa_view_open`` under the hood and cannot be mutated or saved.
    """

    def __init__(self, handle: int, is_view: bool = False):
        self._h = handle
        self._is_view = is_view

    # ── Factory methods ─────────────────────────────────────────────────

    @classmethod
    def create(cls) -> "Dafsa":
        """Create a new empty mutable DAFSA."""
        h = _get_lib().dafsa_create()
        if not h:
            raise MemoryError("dafsa_create returned NULL (OOM)")
        return cls(h)

    @classmethod
    def load(cls, path: str, readonly: bool = False) -> "Dafsa":
        """Load a DAFSA from *path*.

        When *readonly* is ``True`` (the default for ``Index``), opens a
        zero-copy mmap view via ``dafsa_view_open`` — search-only, much
        lighter than materializing the full state table.
        """
        lib = _get_lib()
        if readonly:
            h = lib.dafsa_view_open(str(path).encode())
            if not h:
                raise FileNotFoundError(f"could not open DAFSA view from {path}")
            return cls(h, is_view=True)
        else:
            h = lib.dafsa_load(str(path).encode())
            if not h:
                raise FileNotFoundError(f"could not load DAFSA from {path}")
            return cls(h)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def free(self) -> None:
        """Release the handle.  NULL-safe.  Uses the correct close for the
        handle type (``dafsa_view_close`` for views, ``dafsa_free`` otherwise)."""
        if self._h:
            lib = _get_lib()
            if self._is_view:
                lib.dafsa_view_close(self._h)
            else:
                lib.dafsa_free(self._h)
            self._h = 0

    # ── Mutation (mutable handles only) ─────────────────────────────────

    def add(self, key: bytes) -> bool:
        """Add *key* to the DAFSA.  Returns ``True`` if added, ``False`` if
        already present.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot mutate a read-only DafsaView")
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        return bool(_get_lib().dafsa_add_n(self._h, buf, len(key)))

    def delete(self, key: bytes) -> bool:
        """Delete *key* from the DAFSA.  Returns ``True`` if deleted,
        ``False`` if absent.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot mutate a read-only DafsaView")
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        return bool(_get_lib().dafsa_delete_n(self._h, buf, len(key)))

    def save(self, path: str) -> None:
        """Serialise the DAFSA to *path*.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot save a read-only DafsaView")
        if _get_lib().dafsa_save(self._h, str(path).encode()) != 0:
            raise OSError(f"dafsa_save failed for {path}")

    # ── Search ──────────────────────────────────────────────────────────

    def lookup(self, key: bytes) -> bool:
        """Return ``True`` if *key* is present in the DAFSA."""
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        lib = _get_lib()
        if self._is_view:
            return bool(lib.dafsa_view_lookup_n(self._h, buf, len(key)))
        else:
            return bool(lib.dafsa_lookup_n(self._h, buf, len(key)))

    def prefix_enum(self, prefix: bytes) -> list[bytes]:
        """Enumerate all payloads whose key starts with *prefix* (W\\0 semantics)."""
        results: list[bytes] = []

        def cb(payload, payload_len, _user):
            results.append(ctypes.string_at(payload, payload_len))
            return 0  # 0 = continue enumerating (non-zero would stop early)

        cb_fn = _ENUM_CB(cb)
        buf = ctypes.create_string_buffer(prefix)
        lib = _get_lib()
        if self._is_view:
            lib.dafsa_view_prefix_enum(self._h, buf, len(prefix), cb_fn, None)
        else:
            lib.dafsa_prefix_enum(self._h, buf, len(prefix), cb_fn, None)
        return results

    # ── Statistics ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return a dict of DAFSA statistics (mutable handles only).

        Raises RuntimeError on a view handle (the view has no register or
        materialized state table to inspect).
        """
        if self._is_view:
            raise RuntimeError("stats not available on a DafsaView")
        out = DafsaStatsOut()
        _get_lib().dafsa_stats(self._h, ctypes.byref(out))
        return {
            "n_states_total": out.n_states_total,
            "n_states_reachable": out.n_states_reachable,
            "n_final": out.n_final,
            "n_trans": out.n_trans,
            "register_probes": out.register_probes,
        }

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.free()
        return False
