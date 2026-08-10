"""Python stdio-daemon frontend to the C DAFSA core.

Spawns a long-lived ``dafsa-cli`` subprocess (a Cosmopolitan build, assimilated
into a native statically-linked ELF) and talks JSON-lines over stdio.  The
daemon is libc-agnostic — it runs under both host glibc CPython and the musl
sandbox CPython with no dynamic linker dependency and no APE loader.

Exposes the same public API as the old ctypes backend: ``Dafsa`` (with
create/load/free/add/delete/lookup/save/prefix_enum/stats/context-manager) and
``DafsaWal`` (open/append_add/append_del/sync/size/replay_into/close/
context-manager).  The API is byte-for-byte identical; consumers
(``indexer/__init__.py``, ``search/indexer.py``) need no changes.

A C crash kills only the daemon, not the Python MCP server.  The next request
detects the dead process and spawns a fresh one transparently.

Build the daemon once with:
    make -C indexer/dafsa daemon
"""

import atexit
import base64
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

# Matches MAX_WORD_LEN in dafsa.c. Keys longer than this are rejected by the C
# core (returns -1); guard here too so we never build an oversized buffer.
_MAX_WORD_LEN = 4096

# Must match DAFSA_ABI_VERSION in dafsa.h.
_ABI_VERSION = 1


# ── Exceptions ─────────────────────────────────────────────────────────────

class DafsaDaemonCrashed(RuntimeError):
    """Raised when the daemon subprocess dies mid-request."""


def _raise_dafsa_error(err: str, code: str) -> None:
    """Map a daemon error code to the right Python exception type."""
    if code in ("ELOAD",):
        raise FileNotFoundError(err)
    if code in ("EOOM", "EFULL"):
        raise MemoryError(err)
    if code in ("EOPEN",):
        raise OSError(err)
    # EBADH, EBADOP, EBADREQ, EPARSE, EENUM, EREPLAY → RuntimeError
    raise RuntimeError(err)


# ── Daemon singleton ───────────────────────────────────────────────────────

class _Daemon:
    """Long-lived subprocess wrapper around ``dafsa-cli``.

    Singleton — one daemon per process.  Lazily spawned on first request.
    All requests are serialised through a threading.Lock; the daemon is
    single-threaded internally.
    """

    _instance: "_Daemon | None" = None
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get(cls) -> "_Daemon":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._req_lock: threading.Lock = threading.Lock()
        self._next_id: int = 0
        self._spawn()
        atexit.register(self.shutdown)

    # ── binary discovery ──────────────────────────────────────────────

    @staticmethod
    def _find_binary() -> str:
        """Resolve the daemon binary path.

        Order: $JING_DAFSA_CLI → indexer/dafsa/dafsa-cli (in-tree) →
        shutil.which("dafsa-cli").
        """
        env = os.environ.get("JING_DAFSA_CLI")
        if env and Path(env).is_file():
            return env

        in_tree = Path(__file__).parent / "dafsa-cli"
        if in_tree.is_file():
            return str(in_tree)

        found = shutil.which("dafsa-cli")
        if found:
            return found

        raise RuntimeError(
            "dafsa-cli daemon binary not found. Build it with: "
            "make -C indexer/dafsa daemon  (or set $JING_DAFSA_CLI). "
            "See AGENTS.md."
        )

    # ── spawn / shutdown ──────────────────────────────────────────────

    def _spawn(self) -> None:
        """Launch the daemon subprocess and perform the handshake."""
        path = self._find_binary()
        self._proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Handshake: verify ABI + protocol version.
        reply = self._send_recv("handshake")
        if reply.get("abi") != _ABI_VERSION:
            self._kill()
            raise RuntimeError(
                f"dafsa-cli ABI mismatch: expected {_ABI_VERSION}, "
                f"got {reply.get('abi')}; rebuild with: "
                "make -C indexer/dafsa daemon"
            )
        if reply.get("proto") != 1:
            self._kill()
            raise RuntimeError(
                f"dafsa-cli protocol version mismatch: expected 1, "
                f"got {reply.get('proto')}"
            )

    def _kill(self) -> None:
        """Forcibly terminate the daemon (best-effort)."""
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.stdout.close()
                self._proc.stderr.close()
            except OSError:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                except OSError:
                    pass
            self._proc = None

    def shutdown(self) -> None:
        """Graceful shutdown: send ``shutdown`` op, wait, force-kill backstop."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(
                json.dumps({"id": 0xFFFFFFFF, "op": "shutdown"}) + "\n"
            )
            self._proc.stdin.flush()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()
        self._proc = None

    # ── request / response ────────────────────────────────────────────

    def _send_recv(self, op: str, **kw) -> dict:
        """Send a request and read the reply (called under ``_req_lock``).

        Does NOT raise on ok:false — the caller interprets the reply.
        """
        assert self._proc is not None
        self._next_id += 1
        req_id = self._next_id & 0xFFFFFFFF
        payload = {"id": req_id, "op": op}
        payload.update(kw)
        line = json.dumps(payload, separators=(",", ":"),
                          ensure_ascii=False)

        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._proc = None
            raise DafsaDaemonCrashed(
                f"Daemon crashed before sending request: {e}"
            ) from e

        try:
            resp_line = self._proc.stdout.readline()
        except Exception as e:
            self._proc = None
            raise DafsaDaemonCrashed(
                f"Daemon crashed during read: {e}"
            ) from e

        if not resp_line:
            self._proc = None
            raise DafsaDaemonCrashed("Daemon closed stdout unexpectedly")

        try:
            reply = json.loads(resp_line)
        except json.JSONDecodeError:
            self._proc = None
            raise DafsaDaemonCrashed(
                f"Daemon sent invalid JSON: {resp_line[:200]}"
            ) from None

        if reply.get("id") != req_id:
            self._proc = None
            raise DafsaDaemonCrashed(
                f"Daemon reply id mismatch: expected {req_id}, "
                f"got {reply.get('id')}"
            )

        return reply

    def request(self, op: str, **kw) -> dict:
        """Send a request, read the reply, and raise on error.

        Returns the parsed reply dict.  Raises ``DafsaDaemonCrashed`` if
        the daemon dies; raises the appropriate Python exception (OSError,
        FileNotFoundError, MemoryError, RuntimeError) on ok:false.

        On crash, the daemon is marked dead; the *next* request spawns a
        fresh one automatically.
        """
        with self._req_lock:
            # Auto-restart: if the daemon died, spawn a fresh one.
            if self._proc is None or self._proc.poll() is not None:
                if self._proc is not None and self._proc.poll() is not None:
                    # Consume the return code so it doesn't zombie.
                    pass
                self._spawn()

            reply = self._send_recv(op, **kw)
            if not reply.get("ok"):
                _raise_dafsa_error(
                    reply.get("err", "unknown error"),
                    reply.get("code", ""),
                )
            return reply


# ── Backward-compat shim for test skip probes ──────────────────────────────

def _get_lib() -> _Daemon:
    """Spawn and handshake the daemon (raises RuntimeError if the binary is
    missing or the ABI mismatches).  Kept for backward compatibility so the
    existing test skip probes (``_get_lib()`` in ``test_indexer.py``,
    ``test_search_indexer.py``, ``test_wal.py``) work unchanged.
    """
    return _Daemon.get()


# ── Helpers ────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    """Base64-encode *data* (standard alphabet)."""
    return base64.b64encode(data).decode("ascii")


# ── Dafsa ──────────────────────────────────────────────────────────────────

class Dafsa:
    """Python wrapper around an opaque C DAFSA handle (held by the daemon).

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
        reply = _Daemon.get().request("create")
        h = reply["h"]
        if not h:
            raise MemoryError("dafsa_create returned NULL (OOM)")
        return cls(h)

    @classmethod
    def load(cls, path: str, readonly: bool = False, wal_path: str | None = None) -> "Dafsa":
        """Load a DAFSA from *path*.

        When *readonly* is ``True`` (the default for ``Index``), opens a
        zero-copy mmap view via ``dafsa_view_open`` — search-only, much
        lighter than materializing the full state table.

        When *wal_path* is given alongside ``readonly=True``, opens a layered
        view via ``dafsa_view_open_layered``, merging the WAL overlay (adds
        and deletes) on top of the base view at the C level.
        """
        kw = {"path": str(path)}
        if readonly:
            kw["readonly"] = True
            if wal_path:
                kw["wal_path"] = str(wal_path)
        try:
            reply = _Daemon.get().request("load", **kw)
        except FileNotFoundError:
            if wal_path:
                raise FileNotFoundError(
                    f"could not open layered DAFSA view from {path} + {wal_path}"
                )
            elif readonly:
                raise FileNotFoundError(f"could not open DAFSA view from {path}")
            else:
                raise FileNotFoundError(f"could not load DAFSA from {path}")
        return cls(reply["h"], is_view=readonly)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def free(self) -> None:
        """Release the handle.  NULL-safe.  Uses the correct close for the
        handle type (``dafsa_view_close`` for views, ``dafsa_free`` otherwise)."""
        if self._h:
            _Daemon.get().request("free", h=self._h)
            self._h = 0

    # ── Mutation (mutable handles only) ─────────────────────────────────

    def add(self, key: bytes) -> bool:
        """Add *key* to the DAFSA.  Returns ``True`` if added, ``False`` if
        already present.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot mutate a read-only DafsaView")
        if len(key) > _MAX_WORD_LEN:
            return False
        return bool(_Daemon.get().request("add", h=self._h, key=_b64(key))["rc"])

    def add_many(self, keys: list[bytes]) -> int:
        """Add *keys* to the DAFSA in batched daemon RPCs.

        Returns the number of keys newly added (duplicates are counted but not
        returned).  Raises RuntimeError on a view handle.  Keys are sent in
        chunks that fit within the daemon's per-request limits (MAX_BATCH_KEYS
        and the line buffer), amortizing the IPC round-trip — much faster than
        calling :meth:`add` in a loop for bulk index builds.
        """
        if self._is_view:
            raise RuntimeError("Cannot mutate a read-only DafsaView")
        items = [k for k in keys if len(k) <= _MAX_WORD_LEN]
        if not items:
            return 0
        _daemon = _Daemon.get()
        total_added = 0
        # Chunk to stay under the daemon's per-request batch cap and line cap.
        chunk = 2048
        for start in range(0, len(items), chunk):
            part = items[start:start + chunk]
            reply = _daemon.request(
                "batch_add", h=self._h, keys=[_b64(k) for k in part]
            )
            total_added += int(reply.get("added", 0))
        return total_added

    def delete(self, key: bytes) -> bool:
        """Delete *key* from the DAFSA.  Returns ``True`` if deleted,
        ``False`` if absent.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot mutate a read-only DafsaView")
        if len(key) > _MAX_WORD_LEN:
            return False
        return bool(_Daemon.get().request("delete", h=self._h, key=_b64(key))["rc"])

    def save(self, path: str) -> None:
        """Serialise the DAFSA to *path*.  Raises RuntimeError on a view handle."""
        if self._is_view:
            raise RuntimeError("Cannot save a read-only DafsaView")
        reply = _Daemon.get().request("save", h=self._h, path=str(path))
        if reply["rc"] != 0:
            raise OSError(f"dafsa_save failed for {path}")

    # ── Search ──────────────────────────────────────────────────────────

    def lookup(self, key: bytes) -> bool:
        """Return ``True`` if *key* is present in the DAFSA."""
        if len(key) > _MAX_WORD_LEN:
            return False
        return bool(_Daemon.get().request("lookup", h=self._h, key=_b64(key))["rc"])

    def prefix_enum(self, prefix: bytes) -> list[bytes]:
        """Enumerate all payloads whose key starts with *prefix* (W\\0 semantics)."""
        reply = _Daemon.get().request(
            "prefix_enum", h=self._h, prefix=_b64(prefix)
        )
        n = reply.get("n", 0)
        if n < 0:
            raise RuntimeError(f"prefix_enum failed (code {n})")
        return [base64.b64decode(p) for p in reply.get("payloads", [])]

    # ── Statistics ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return a dict of DAFSA statistics (mutable handles only).

        Raises RuntimeError on a view handle (the view has no register or
        materialized state table to inspect).
        """
        if self._is_view:
            raise RuntimeError("stats not available on a DafsaView")
        return _Daemon.get().request("stats", h=self._h)["stats"]

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.free()
        return False


# ── DafsaWal ───────────────────────────────────────────────────────────────

class DafsaWal:
    """Thin wrapper around the C write-ahead log (WAL) handle (held by the daemon).

    Mirrors the style of ``Dafsa``: holds an opaque daemon handle id, provides
    factory/close/context-manager, and delegates to the daemon via JSON-lines.
    """

    def __init__(self, handle: int):
        self._h = handle

    @classmethod
    def open(cls, path: str) -> "DafsaWal":
        """Open or create the WAL at *path* (append-only log)."""
        reply = _Daemon.get().request("wal_open", path=str(path))
        return cls(reply["h"])

    def append_add(self, key: bytes) -> bool:
        """Append an ADD record. Returns True on success."""
        return _Daemon.get().request(
            "wal_append_add", h=self._h, key=_b64(key)
        )["rc"] == 0

    def append_del(self, key: bytes) -> bool:
        """Append a DEL record. Returns True on success."""
        return _Daemon.get().request(
            "wal_append_del", h=self._h, key=_b64(key)
        )["rc"] == 0

    def sync(self) -> None:
        """fflush + fsync the WAL file."""
        if _Daemon.get().request("wal_sync", h=self._h)["rc"] != 0:
            raise OSError("dafsa_wal_sync failed")

    def size(self) -> int:
        """Return the current WAL file size in bytes."""
        return _Daemon.get().request("wal_size", h=self._h)["size"]

    def replay_into(self, dafsa: "Dafsa") -> int:
        """Replay all WAL records into *dafsa* (must be mutable).

        Returns the number of records replayed, or -1 on error.
        Per-record errors (add/delete returning <0) abort replay immediately.
        Idempotent: already-present (add→0) or already-absent (delete→0) are
        silently skipped and do not count as failures.
        """
        try:
            reply = _Daemon.get().request(
                "wal_replay", wal=self._h, dafsa=dafsa._h
            )
            return reply["count"]
        except RuntimeError:
            return -1

    def close(self) -> None:
        """Close the WAL handle. NULL-safe."""
        if self._h:
            _Daemon.get().request("free", h=self._h)
            self._h = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
