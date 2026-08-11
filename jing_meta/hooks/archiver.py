"""Memory Archiver hook — throttled archiving of old observations.

Console script: ``jing-archiver-hook``. Wired as a Vibe ``post_agent`` hook so
old observations get moved out of the live memory graph into the searchable
memory-archive domain automatically. Unlike the graph-gardener this is cheap
(no LLM call), so it runs on a longer, simpler throttle.

Design:
- Self-throttled: at most once per ``MEMORY_ARCHIVE_INTERVAL_HOURS``
  (default 24h), tracked by a state file under ``$VIBE_HOME/state``.
- Growth-aware: only runs when the graph has grown since the last run.
- Auto-applies by default; set ``MEMORY_ARCHIVE_DRY_RUN=1`` to force a dry-run.
- Archive-first-delete-second + DB backup are handled inside
  ``memory.archiver.archive``.
- Graceful skip: exits 0 on any non-fatal condition; never errors the turn.
"""

from __future__ import annotations

import os
import sys

from jing_meta.hooks import common
from jing_meta.log import get_logger, setup_logging

logger = get_logger(__name__)

DEFAULT_INTERVAL_HOURS = 24


def _run_archiver() -> dict:
    """Run the archiver against the resolved memory DB / archive dir.

    Returns the archiver's summary dict. ``apply`` is True unless
    ``MEMORY_ARCHIVE_DRY_RUN`` is set.
    """
    from memory.archiver import archive

    return archive(
        memory_db=common.memory_db(),
        archive_dir=common.archive_dir(),
        days=int(os.environ.get("MEMORY_ARCHIVE_DAYS", "90")),
        apply=not os.environ.get("MEMORY_ARCHIVE_DRY_RUN"),
        rebuild_index=True,
        # A post_agent hook must keep stdout empty (or valid JSON) — the
        # archive() progress messages would otherwise be rejected by Vibe as an
        # "invalid response". Route them to the stderr logger instead.
        quiet=True,
    )


def _record_digest(result: dict) -> None:
    """Record a digest entry from the archiver summary dict."""
    archived = result.get("archived", 0)
    deleted = result.get("deleted", 0)
    detail_parts = []
    if archived:
        detail_parts.append(f"archived {archived} observations")
    if deleted:
        detail_parts.append(f"removed {deleted} from live graph")
    if not detail_parts:
        return  # nothing archived this run — don't clutter the digest

    common.digest_append({
        "system": "memory-archiver",
        "action": "archive",
        "detail": ", ".join(detail_parts),
    })


def main() -> int:
    setup_logging()
    common.drain_stdin()

    state_name = "memory-archiver-last"
    state = common.load_state(state_name)
    last_ts = state.get("last_run", 0)
    interval_h = float(
        os.environ.get("MEMORY_ARCHIVE_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)
    )

    now = common.monotonic_now()
    if now - last_ts < interval_h * 3600:
        return 0  # throttled

    counts = common.graph_counts()
    if counts is None:
        return 0
    if (counts[0], counts[1]) == (state.get("entities"), state.get("obs")):
        # No growth — nothing new to look at.
        common.save_state(state_name, {**state, "last_run": now,
                                       "entities": counts[0], "obs": counts[1]})
        return 0

    common.save_state(state_name, {**state, "last_run": now,
                                   "entities": counts[0], "obs": counts[1]})

    result = _run_archiver()
    logger.info(
        "archived=%s deleted=%s archive_path=%s",
        result.get('archived', 0),
        result.get('deleted', 0),
        result.get('archive_path') or '(dry-run)',
    )

    _record_digest(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
