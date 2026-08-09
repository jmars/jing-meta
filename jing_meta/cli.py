"""jing-meta CLI — top-level command dispatcher."""

import argparse
import sys

from jing_meta.log import setup_logging


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="jing-meta", description=(
        "Unified self-hosted knowledge system: indexer, search, memory, dreamer."
    ))
    parser.add_argument(
        "command",
        choices=["indexer", "search", "memory", "dreamer", "archiver", "verify"],
        help="Subsystem to run (each also has its own CLI, e.g. jing-indexer)",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "indexer":
        from indexer.__main__ import main as indexer_main
        return indexer_main(args.args)
    elif args.command == "search":
        # MCP server — blocks on stdio transport; no extra args.
        from search.server import main as search_main
        search_main()
        return 0
    elif args.command == "memory":
        # MCP server — blocks on stdio transport; no extra args.
        from memory.__main__ import main as memory_main
        memory_main()
        return 0
    elif args.command == "dreamer":
        from dreamer.__main__ import main as dreamer_main
        dreamer_main()
        return 0
    elif args.command == "archiver":
        from memory.archiver import main as archiver_main
        return archiver_main(args.args)
    elif args.command == "verify":
        return _verify()
    else:
        parser.print_help()
        return 1


def _verify() -> int:
    """Import-check every subsystem to confirm the repo is coherent."""
    import importlib

    ok = True
    for mod in ["jing_meta.config", "indexer", "search", "memory", "dreamer", "memory.archiver"]:
        try:
            importlib.import_module(mod)
            print(f"  ✓ {mod}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {mod}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
