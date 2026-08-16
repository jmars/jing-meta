"""Index-hook — trigger incremental DAFSA index updates after each agent turn.

Console script: ``jing-index-hook``. Wired as a Vibe ``post_agent`` hook so the
DAFSA search index stays current without a polling daemon.

Design:
- No throttle: ``update_index()`` is cheap (WAL append; empty-diff fast path for
  unchanged domains). The sessions domain changes most turns, so throttling
  would defeat the purpose.
- Compaction deferred to background: when the WAL crosses 25% of the base FST,
  folding it into a fresh snapshot can take ~a minute on large indices — far
  over the hook's default 60s timeout. The hook therefore keeps the cheap WAL
  append inline but spawns the compaction in a DETACHED background process
  (``--compact-bg``), returning immediately. Same pattern as the gardener hook.
- Self-compacting: the detached compactor folds the WAL when it exceeds 25% of
  the base FST, so the WAL cannot accumulate unbounded work.
- Opt-out: set ``JING_INDEX_HOOK_ENABLED=0`` (or "false"/"no") to disable.
- Graceful: exits 0 on all non-fatal paths; never errors the agent's turn.
"""
from __future__ import annotations

import os
import subprocess
import sys

from indexer import EXTRACTORS as DAFSA_EXTRACTORS
from jing_meta.hooks import common
from jing_meta.log import get_logger, setup_logging
from search.config import Config, load_config
from search.indexer import needs_compact, update_index

logger = get_logger(__name__)


def run_updates(cfg: Config, defer_compact: bool = False) -> list[tuple[str, bool, str]]:
    """Run update_index for every DAFSA-capable domain in *cfg*.

    Returns a list of (domain_name, ok, message) tuples. Non-DAFSA domains
    (e.g. extractor="notification") are skipped. When *defer_compact* is True,
    the WAL fold is deferred (fast); the caller should later run the detached
    compactor via ``spawn_background_compact`` for domains that need it.
    """
    results = []
    for name, domain_cfg in cfg.domains.items():
        if domain_cfg.extractor not in DAFSA_EXTRACTORS:
            logger.debug("Skipping non-DAFSA domain %r (extractor=%r)", name, domain_cfg.extractor)
            continue
        if defer_compact:
            ok, msg = update_index(domain_cfg, defer_compact=True)
        else:
            ok, msg = update_index(domain_cfg)
        results.append((name, ok, msg))
    return results


def spawn_background_compact(domain_names: list[str]) -> None:
    """Spawn a DETACHED background process to fold the WAL for *domain_names*.

    Compaction can take ~a minute on large indices, so it must never block the
    agent's turn or hit the hook timeout. This mirrors the gardener hook's
    detached-maintenance pattern. Best-effort: a failed spawn is logged, not
    raised.
    """
    if not domain_names:
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jing_meta.hooks.indexhook", "--compact-bg", *domain_names],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # pragma: no cover — spawn failure must never break the turn
        logger.warning("failed to spawn background index compaction: %s", exc)


def _compact_bg(domain_names: list[str]) -> int:
    """Background entry point: fold the WAL for the named domains, detached from
    the hook. Called by the detached subprocess (``--compact-bg <name>...``).
    Fail-open end-to-end — never propagates a failure to the (already-returned)
    hook. Also re-checks needs_compact so an already-folded domain is a no-op.
    """
    try:
        from indexer import compact as dafsa_compact
    except Exception as exc:  # pragma: no cover
        logger.warning("background compact import failed: %s", exc)
        return 0
    try:
        cfg = load_config()
    except Exception as exc:  # pragma: no cover
        logger.warning("background compact: config load failed: %s", exc)
        return 0
    for name in domain_names:
        domain_cfg = cfg.domains.get(name)
        if domain_cfg is None:
            logger.warning("background compact: unknown domain %r", name)
            continue
        if not needs_compact(domain_cfg):
            logger.debug("background compact: %r WAL below threshold; skipping", name)
            continue
        try:
            dafsa_compact(domain_cfg.effective_index_dir)
            logger.info("compaction folded for %r", name)
            common.digest_append(
                {"system": "indexhook", "action": "compacted", "domain": name}
            )
        except Exception as exc:  # pragma: no cover — background job must fail open
            logger.warning("background compact failed for %r: %s", name, exc)
            common.digest_append(
                {"system": "indexhook", "action": "compact_failed", "domain": name,
                 "detail": str(exc)}
            )
    return 0


def main() -> int:
    setup_logging()
    common.drain_stdin()

    if os.environ.get("JING_INDEX_HOOK_ENABLED", "1").lower() in ("0", "false", "no"):
        logger.debug("jing-index-hook disabled via JING_INDEX_HOOK_ENABLED")
        return 0

    cfg = load_config()
    if not cfg.domains:
        return 0

    # Keep the cheap WAL append inline, but defer the heavy WAL fold so the hook
    # stays well under the 60s timeout even on large indices.
    results = run_updates(cfg, defer_compact=True)
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

    # Spawn the detached compactor for domains whose WAL crossed the threshold.
    pending = [name for name, domain_cfg in cfg.domains.items()
               if domain_cfg.extractor in DAFSA_EXTRACTORS and needs_compact(domain_cfg)]
    if pending:
        logger.info("deferring compaction for %r to background process", pending)
        spawn_background_compact(pending)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--compact-bg":
        sys.exit(_compact_bg(args[1:]))
    sys.exit(main())
