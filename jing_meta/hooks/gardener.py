"""Graph Gardener hook — throttled, auto-applying knowledge-graph maintenance.

Console script: ``jing-gardener-hook``. Wired as a Vibe ``post_agent`` hook so
maintenance runs on its own without the user having to remember to apply.

Design goals (emergent-friendly):

- **Self-throttling**: at most once per ``GRAPH_GARDENER_INTERVAL_MIN``
  (default 180), tracked by a state file under ``$VIBE_HOME/state``.
- **Growth-gated**: only runs when the graph has grown by at least
  ``GRAPH_GARDENER_MIN_GROWTH`` entities+observations (default 25) since the
  last run.
- **Auto-applies by default** (``--apply``) so the user doesn't have to
  remember; set ``GRAPH_GARDENER_DRY_RUN=1`` to force a dry-run. The dreamer is
  additive-only and backs up the DB before every apply.
- **Graceful skip**: exits 0 on any non-fatal condition (throttled, no growth,
  no key); never errors the agent's turn.

Reads the Vibe hook JSON payload on stdin (ignored for cadence) per Vibe's hook
wire protocol.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from jing_meta.hooks import common
from jing_meta.log import get_logger, setup_logging

logger = get_logger(__name__)

DEFAULT_INTERVAL_MIN = 60  # run at most once per hour
DEFAULT_MIN_GROWTH = 25


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cloud_config() -> tuple[str, str | None, str | None, str | None]:
    """Resolve (validator, api_url, api_key, model) for the dreamer's cloud path.

    Defaults to the DeepInfra cloud validator (Mistral-Small-24B, Flex tier),
    reading the DeepInfra API key. Explicit GRAPH_GARDENER_* env vars override
    the defaults; set GRAPH_GARDENER_VALIDATOR=local to fall back to Ollama.
    """
    from jing_meta import config as _config

    validator = os.environ.get("GRAPH_GARDENER_VALIDATOR", "cloud")
    api_url = os.environ.get("GRAPH_GARDENER_API_URL") or _config.CLOUD_LLM_URL
    api_key = (
        os.environ.get("GRAPH_GARDENER_API_KEY")
        or os.environ.get("DEEPINFRA_API_KEY")
    )
    model = os.environ.get("GRAPH_GARDENER_MODEL") or _config.CLOUD_LLM_MODEL
    return validator, api_url, api_key, model


def _new_run_id() -> str:
    """Return a collision-safe run ID for a maintenance pass.

    Delegates to the dreamer's RunStore so the ID matches the ``<memory_dir>/dreamer/<run_id>/``
    layout and is unique even if two passes start in the same second. This is generated in the
    *hook* (parent) process and handed to the detached child so the run is auditable on disk.
    """
    from dreamer.runstore import RunStore
    from jing_meta import config as _config

    store = RunStore(_config.memory_dir() / "dreamer")
    return store.new_run_id()


def _run_dreamer(run_id: str | None = None) -> tuple[int, str]:
    """Run the dreamer in Soufflé mode against the resolved memory DB.

    When *run_id* is given the run is persisted under ``<memory_dir>/dreamer/<run_id>/``
    (snapshot + manifest + per-stage JSON), so a background pass is auditable/replayable
    after the fact. Returns (exit_code, stdout_text). Falls back to LLM-discovery mode
    when ``GRAPH_GARDENER_LLM_MODE=1`` is set.
    """
    from dreamer.dreamer import run_souffle_mode

    db = common.memory_db()
    rerank = True
    validator, api_url, api_key, model = _cloud_config()

    if os.environ.get("GRAPH_GARDENER_LLM_MODE"):
        # Legacy fallback: use the non-Soufflé LLM-discovery pipeline.
        from dreamer.dreamer import run

        code = run(
            db,
            apply=not os.environ.get("GRAPH_GARDENER_DRY_RUN"),
            api_url=api_url,
            api_key=api_key,
            model=model,
            run_id=run_id,
        )
        return code, ""

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_souffle_mode(
            db,
            apply=not os.environ.get("GRAPH_GARDENER_DRY_RUN"),
            api_url=api_url,
            api_key=api_key,
            model=model,
            rerank=rerank,
            validator=validator,
            run_id=run_id,
        )
    return code, buf.getvalue()


def _rebuild_semantic_index() -> None:
    """Rebuild the offline semantic index after the graph changed.

    In-process (no subprocess). Best-effort; skips silently on failure.
    Controlled by ``GRAPH_GARDENER_SEMANTIC_INDEX`` (default 1).
    """
    if os.environ.get("GRAPH_GARDENER_SEMANTIC_INDEX", "1") != "1":
        return
    import sqlite3

    from memory.semantic_index import build_index

    db = common.memory_db()
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            v, n = build_index(conn, str(db))
            logger.info("semantic index rebuilt -> %s", v.name)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - best effort
        logger.warning("semantic index rebuild skipped: %s", e)


def _record_digest(out: str, run_id: str | None = None) -> None:
    """Parse the dreamer's stdout and append a digest record."""
    m_total = re.search(r"TOTAL mutations:\s*(\d+)", out)
    types = {}
    for m in re.finditer(r"^\s+(\w+):\s+(\d+)", out, re.MULTILINE):
        types[m.group(1)] = int(m.group(2))
    saved = re.search(r"Saved:\s*(\d+)\s+entities,\s*(\d+)\s+relations", out)

    detail_parts = []
    if run_id:
        detail_parts.append(f"run_id={run_id}")
    if m_total:
        detail_parts.append(f"{m_total.group(1)} mutations")
    for k, v in types.items():
        detail_parts.append(f"{k}={v}")
    if saved:
        detail_parts.append(f"graph now {saved.group(1)} entities / {saved.group(2)} rels")

    # Surfaces local-validator health (e.g. "Ollama was down for N/M pairs") so
    # a silently-failing validator is observable in the digest. Printed by the
    # dreamer as: "  validator health: batch X/Y ok, N/M pairs returned None (K single-pair fallbacks)"
    m_vh = re.search(
        r"validator health: batch (\d+)/(\d+) ok, (\d+)/(\d+) pairs returned None",
        out,
    )
    if m_vh:
        none, total = int(m_vh.group(3)), int(m_vh.group(4))
        if none > 0:
            detail_parts.append(f"validator: {none}/{total} pairs failed")
        elif total > 0:
            detail_parts.append(f"validator: {total} pairs ok")

    common.digest_append({
        "system": "graph-gardener",
        "action": "maintenance",
        "detail": ", ".join(detail_parts) if detail_parts else "ran",
    })


def main() -> int:
    setup_logging()
    common.drain_stdin()

    key = (
        os.environ.get("GRAPH_GARDENER_API_KEY")
        or os.environ.get("DEEPINFRA_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
    )
    if not key:
        logger.info(
            "no API key (GRAPH_GARDENER_API_KEY, DEEPINFRA_API_KEY, or DEEPSEEK_API_KEY) — skipping."
        )
        return 0

    state_name = "graph-gardener-last"
    state = common.load_state(state_name)
    last_ts = state.get("last_run", 0)
    interval = _env_float("GRAPH_GARDENER_INTERVAL_MIN", DEFAULT_INTERVAL_MIN) * 60
    min_growth = _env_int("GRAPH_GARDENER_MIN_GROWTH", DEFAULT_MIN_GROWTH)

    now = common.monotonic_now()
    if now - last_ts < interval:
        return 0  # throttled

    counts = common.graph_counts()
    if counts is None:
        return 0

    # Growth gate: skip unless the graph has accumulated meaningful new
    # material since the last run.
    last_e, last_o = state.get("entities", 0), state.get("obs", 0)
    added = max(counts[0] - last_e, 0) + max(counts[1] - last_o, 0)
    if added < min_growth:
        common.save_state(state_name, {**state, "last_run": now,
                                       "entities": counts[0], "obs": counts[1]})
        return 0

    common.save_state(state_name, {**state, "last_run": now,
                                   "entities": counts[0], "obs": counts[1]})

    # The maintenance work (Soufflé run + LLM validation + semantic-index
    # rebuild) is slow (~167s) and LLM-heavy. Run it in a DETACHED background
    # process so the hook returns immediately and never blocks the agent's
    # turn. The throttle/growth state is already saved above, so the work is
    # still cadence-gated (exactly one maintenance pass per interval).
    #
    # A run_id is generated HERE (parent) and passed to the child so each pass
    # persists to <memory_dir>/dreamer/<run_id>/ (snapshot + manifest + stage
    # JSON) and can be audited/replayed even though it runs detached.
    run_id = _new_run_id()
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jing_meta.hooks.gardener",
             "--run-maintenance-bg", "--run-id", run_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # pragma: no cover — spawn failure must never break the turn
        logger.warning("failed to spawn background gardener: %s", exc)
    return 0


def _run_maintenance_bg(run_id: str | None = None) -> int:
    """Background entry point: runs the slow maintenance work detached from the
    hook. Called by the detached subprocess (``--run-maintenance-bg``). Fail-open
    end-to-end — never propagates a failure to the (already-returned) hook."""
    try:
        setup_logging()
        code, out = _run_dreamer(run_id=run_id)
        if code != 0:
            logger.warning("dreamer exited with code %d", code)
        first_lines = "\n".join(out.splitlines()[:8])
        logger.info("maintenance run %s:\n%s", run_id, first_lines)
        _record_digest(out, run_id=run_id)
        _rebuild_semantic_index()
    except Exception as exc:  # pragma: no cover — background job must fail open
        logger.warning("background gardener failed: %s", exc)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--run-maintenance-bg":
        # Optional trailing --run-id <id> (generated by the hook parent).
        run_id = None
        if "--run-id" in args:
            i = args.index("--run-id")
            if i + 1 < len(args):
                run_id = args[i + 1]
        sys.exit(_run_maintenance_bg(run_id=run_id))
    sys.exit(main())
