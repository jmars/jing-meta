"""Shared MCP base for jing-meta's MCP servers.

Provides a common ``JINGMCP`` FastMCP subclass plus small helpers (lazy
singletons, atomic file writes, text content blocks) used by both the search
and memory MCP servers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from jing_meta.log import setup_logging

T = TypeVar("T")


class JINGMCP(FastMCP):
    """FastMCP subclass with jing-meta-idiomatic setup and run."""

    def run_stdio(self, pre_warm: Callable[[], None] | None = None) -> None:
        """Set up logging, optionally pre-warm resources, then run stdio."""
        setup_logging()
        if pre_warm is not None:
            pre_warm()
        self.run(transport="stdio")


def lazy_singleton(factory: Callable[[], T]) -> Callable[[], T]:
    """Return a zero-arg callable that computes *factory()* once, then caches it."""
    _sentinel: object = object()
    _value: T = _sentinel  # type: ignore[assignment]

    def get() -> T:
        nonlocal _value
        if _value is _sentinel:
            _value = factory()
        return _value

    return get


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
    _atomic_write(path, json.dumps(data, indent=2).encode("utf-8"))


def text_block(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def text_blocks(*texts: str) -> list[TextContent]:
    return [text_block(t) for t in texts]
