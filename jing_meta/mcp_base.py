"""Shared MCP base for jing-meta's MCP servers.

Provides a common ``JINGMCP`` FastMCP subclass plus small helpers (lazy
singletons, atomic file writes, text content blocks) used by both the search
and memory MCP servers.

Concurrency model
-----------------
Under mcp 1.23 with ``transport="stdio"``, the stdio server runs on a single
anyio event-loop thread.  Sync tool handlers are dispatched **inline** on that
thread (``func_metadata.py:95``: ``return fn(**args)``) — no ThreadPoolExecutor
and no worker threads.  Tool handlers are therefore **serial**: two handlers
can never overlap, and shared module-level state (``lazy_singleton`` caches,
``_conn``, the embedder) is safe without explicit locks as long as all
handlers are synchronous.

If the mcp pin is ever relaxed to ``>= 2.0``, sync dispatch flips to
``anyio.to_thread.run_sync`` (a thread pool), and handlers **can** overlap.
At that point shared mutable state (SQLite connections, caches, DAFSA handles)
will need per-thread isolation or explicit locking.
"""

from __future__ import annotations

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


from .fsutil import _atomic_write, _atomic_write_json  # noqa: E402, F401 — re-export


def text_block(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def text_blocks(*texts: str) -> list[TextContent]:
    return [text_block(t) for t in texts]
