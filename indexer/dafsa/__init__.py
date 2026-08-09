"""Python ctypes frontend to the C DAFSA core.

Loads the C DAFSA shared library (built from `dafsa.c` in this directory) and
exposes its API to Python: create/load/save, length-delimited add/lookup/delete,
and prefix enumeration (used for search). This replaces the Rust frontend while
keeping the exact same DAFSA on-disk format.

Build the shared lib once with:  gcc -shared -fPIC -O2 -o libdafsa.so dafsa.c
"""

import ctypes
import ctypes.util
import sys
from pathlib import Path

_LIB_PATH = Path(__file__).parent / "libdafsa.so"

# Matches MAX_WORD_LEN in dafsa.c. Keys longer than this are rejected by the C
# core (returns -1); guard here too so we never build an oversized buffer.
_MAX_WORD_LEN = 4096

# Enumerate callback: returns count (or -1 on error); stop by returning 0.
_ENUM_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_void_p)


def _load() -> ctypes.CDLL:
    path = _LIB_PATH if _LIB_PATH.exists() else ctypes.util.find_library("dafsa")
    if not path:
        raise RuntimeError(
            "libdafsa.so not found. Build it in indexer/dafsa with: "
            "gcc -shared -fPIC -O2 -o libdafsa.so dafsa.c"
        )
    lib = ctypes.CDLL(str(path))

    lib.dafsa_create.restype = ctypes.c_void_p
    lib.dafsa_free.argtypes = [ctypes.c_void_p]

    lib.dafsa_load.argtypes = [ctypes.c_char_p]
    lib.dafsa_load.restype = ctypes.c_void_p
    lib.dafsa_load_readonly.argtypes = [ctypes.c_char_p]
    lib.dafsa_load_readonly.restype = ctypes.c_void_p
    lib.dafsa_save.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.dafsa_save.restype = ctypes.c_int

    lib.dafsa_add_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_add_n.restype = ctypes.c_int
    lib.dafsa_lookup_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_lookup_n.restype = ctypes.c_int
    lib.dafsa_delete_n.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.dafsa_delete_n.restype = ctypes.c_int

    lib.dafsa_prefix_enum.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, _ENUM_CB, ctypes.c_void_p,
    ]
    lib.dafsa_prefix_enum.restype = ctypes.c_long

    return lib


_lib = _load()


class Dafsa:
    """Python wrapper around the opaque C DAFSA handle."""

    def __init__(self, handle: int):
        self._h = handle

    @classmethod
    def create(cls) -> "Dafsa":
        h = _lib.dafsa_create()
        if not h:
            raise MemoryError("dafsa_create returned NULL (OOM)")
        return cls(h)

    @classmethod
    def load(cls, path: str, readonly: bool = False) -> "Dafsa":
        fn = _lib.dafsa_load_readonly if readonly else _lib.dafsa_load
        h = fn(str(path).encode())
        if not h:
            raise FileNotFoundError(f"could not load DAFSA from {path}")
        return cls(h)

    def free(self) -> None:
        if self._h:
            _lib.dafsa_free(self._h)
            self._h = 0

    def add(self, key: bytes) -> bool:
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        return bool(_lib.dafsa_add_n(self._h, buf, len(key)))

    def lookup(self, key: bytes) -> bool:
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        return bool(_lib.dafsa_lookup_n(self._h, buf, len(key)))

    def delete(self, key: bytes) -> bool:
        if len(key) > _MAX_WORD_LEN:
            return False
        buf = ctypes.create_string_buffer(key)
        return bool(_lib.dafsa_delete_n(self._h, buf, len(key)))
    def save(self, path: str) -> None:
        if _lib.dafsa_save(self._h, str(path).encode()) != 0:
            raise OSError(f"dafsa_save failed for {path}")

    def prefix_enum(self, prefix: bytes) -> list[bytes]:
        """Enumerate all payloads whose key starts with *prefix*."""
        results: list[bytes] = []

        def cb(payload, payload_len, _user):
            results.append(ctypes.string_at(payload, payload_len))
            return 1  # continue

        cb_fn = _ENUM_CB(cb)
        buf = ctypes.create_string_buffer(prefix)
        _lib.dafsa_prefix_enum(self._h, buf, len(prefix), cb_fn, None)
        return results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.free()
        return False
