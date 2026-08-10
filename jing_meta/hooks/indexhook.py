"""Index-hook — trigger incremental DAFSA index updates after each agent turn.

Console script: ``jing-index-hook``. Wired as a Vibe ``post_agent`` hook so the
DAFSA search index stays current without a polling daemon.

Design:
- No throttle: ``update_index()`` is cheap (WAL append; empty-diff fast path for
  unchanged domains). The sessions domain changes most turns, so throttling
  would defeat the purpose.
- Self-compacting: ``indexer.update()`` compacts when the WAL exceeds 25% of the
  base FST, so the hook cannot accumulate unbounded work.
- Opt-out: set ``JING_INDEX_HOOK_ENABLED=0`` (or "false"/"no") to disable.
- Graceful: exits 0 on all non-fatal paths; never errors the agent's turn.
"""
from __future__ import annotations

import os
import sys

from indexer import EXTRACTORS as DAFSA_EXTRACTORS
from jing_meta.hooks import common
from jing_meta.log import get_logger, setup_logging
from search.config import Config, load_config
from search.indexer import update_index

logger = get_logger(__name__)


def run_updates(cfg: Config) -> list[tuple[str, bool, str]]:
    """Run update_index for every DAFSA-capable domain in *cfg*.

    Returns a list of (domain_name, ok, message) tuples. Non-DAFSA domains
    (e.g. extractor="notification") are skipped.
    """
    results = []
    for name, domain_cfg in cfg.domains.items():
        if domain_cfg.extractor not in DAFSA_EXTRACTORS:
            logger.debug("Skipping non-DAFSA domain %r (extractor=%r)", name, domain_cfg.extractor)
            continue
        ok, msg = update_index(domain_cfg)
        results.append((name, ok, msg))
    return results


def main() -> int:
    setup_logging()
    common.drain_stdin()

    if os.environ.get("JING_INDEX_HOOK_ENABLED", "1").lower() in ("0", "false", "no"):
        logger.debug("jing-index-hook disabled via JING_INDEX_HOOK_ENABLED")
        return 0

    cfg = load_config()
    if not cfg.domains:
        return 0

    results = run_updates(cfg)
    for name, ok, msg in results:
        if ok:
            logger.info("index updated for %r: %s", name, msg)
        else:
            logger.warning("index update failed for %r: %s", name, msg)
            # Route failures into the maintenance digest so they surface in the
            # `jing-maintenance-digest` CLI instead of vanishing into the log.
            common.digest_append(
                {"system": "indexhook", "action": "update_failed", "domain": name, "detail": msg}
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
