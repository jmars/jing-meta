"""Structured logging setup for jing-meta.

Provides ``setup_logging()`` for entry points and ``get_logger()`` for library
modules.  Log level is controlled by the ``JING_LOG_LEVEL`` env var (default
``"WARNING"``).
"""

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_setup_done = False


def setup_logging() -> None:
    """Configure the root logger for jing-meta.

    Idempotent — safe to call multiple times or from multiple entry points.
    Reads ``JING_LOG_LEVEL`` from the environment (default ``"WARNING"``;
    valid values: CRITICAL/ERROR/WARNING/INFO/DEBUG).  Invalid values fall
    back to WARNING.

    Only entry-point modules should call this (``__main__.py`` files, the
    top-level CLI dispatcher).  Library code must NOT call ``setup_logging()``.
    """
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    level = os.environ.get("JING_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format=_LOG_FORMAT,
        stream=sys.stderr,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a logger for *name* (typically ``__name__``).

    Does NOT call ``setup_logging()`` — entry points must call that first.
    """
    return logging.getLogger(name)
