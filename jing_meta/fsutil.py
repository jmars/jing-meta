"""Shared filesystem utilities — atomic writes.

Extracted from ``jing_meta/mcp_base.py`` so the dreamer (and any other
subsystem) can use durable atomic writes without importing FastMCP.
"""

import json
import os
from pathlib import Path


def _atomic_write(path: Path, content: bytes) -> None:
    """Write *content* to *path* atomically: tmp file + fsync + rename + dir fsync.

    Mirrors the C ``dafsa_save`` durability sequence (fsync file, rename,
    fsync_dir_of).  The directory fsync is best-effort — on platforms where it
    is unsupported (e.g. some network FS) the OSError is caught so the write
    still succeeds, just with weaker durability.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(content)
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Durability: fsync the containing directory so the rename is committed.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # best-effort; some platforms/FS don't support directory fsync
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data) -> None:
    """Atomically write *data* as JSON to *path*."""
    _atomic_write(path, json.dumps(data, indent=2).encode("utf-8"))
