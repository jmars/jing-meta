"""CLI entry point for jing-dreamer."""

import argparse
import os
import sys
from pathlib import Path

from jing_meta import config as _config
from jing_meta.log import get_logger, setup_logging
from .dreamer import run, run_souffle_mode

logger = get_logger(__name__)


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jing-dreamer",
        description="jing-meta knowledge-graph maintenance (Soufflé + semantic + validator)",
    )
    parser.add_argument(
        "--memory-db",
        default=str(_config.memory_db()),
        type=Path,
        help=f"Path to the jing-meta memory store (default: {_config.memory_db()})",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--apply",
        action="store_true",
        help="Apply mutations (default: dry-run)",
    )
    # --dry-run is the default (absence of --apply)

    parser.add_argument(
        "--souffle",
        action="store_true",
        help="Use the Soufflé Datalog pipeline (deterministic tiers + LLM validation)",
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="Disable semantic re-ranking of candidate relations (Soufflé mode only)",
    )
    parser.add_argument(
        "--validator",
        default="local",
        choices=["local", "cloud", "none"],
        help="Relation validator: 'local' (offline rules+Ollama, default), 'cloud' (API LLM), 'none'",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="LLM API base URL (default: GRAPH_GARDENER_API_URL env var)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model name (default: GRAPH_GARDENER_MODEL env var)",
    )
    return parser


def main() -> None:
    setup_logging()
    parser = _cli()
    args = parser.parse_args()

    memory_db = Path(os.path.expanduser(str(args.memory_db)))

    check_path = memory_db
    if not check_path.is_file():
        logger.error("memory store not found or not a regular file: %s", check_path)
        sys.exit(1)

    if not os.access(str(check_path), os.R_OK):
        logger.error("memory store not readable: %s", check_path)
        sys.exit(1)

    if args.souffle:
        sys.exit(run_souffle_mode(
            memory_db,
            apply=args.apply,
            api_url=args.api_url,
            api_key=None,
            model=args.model,
            rerank=not args.no_rerank,
            validator=args.validator,
        ))
    sys.exit(run(
        memory_db,
        apply=args.apply,
        api_url=args.api_url,
        api_key=None,
        model=args.model,
    ))


if __name__ == "__main__":
    main()
