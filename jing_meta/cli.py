"""jing-meta CLI — top-level command dispatcher."""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="jing-meta", description=(
        "Unified self-hosted knowledge system: indexer, search, memory, dreamer."
    ))
    parser.add_argument(
        "command",
        choices=["indexer", "search", "memory", "dreamer", "verify"],
        help="Subsystem to run (each also has its own CLI, e.g. jing-indexer)",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "indexer":
        from indexer.__main__ import main as m
        return m(args.args)
    elif args.command == "search":
        # MCP server — blocks on stdio transport; no extra args.
        from search.server import main as m
        m()
        return 0
    elif args.command == "memory":
        from memory.__main__ import main as m
        return m(args.args)
    elif args.command == "dreamer":
        from dreamer.__main__ import main as m
        return m(args.args)
    elif args.command == "verify":
        return _verify()
    else:
        parser.print_help()
        return 1


def _verify() -> int:
    """Import-check every subsystem to confirm the repo is coherent."""
    import importlib

    ok = True
    for mod in ["jing_meta.config", "indexer", "search", "memory", "dreamer"]:
        try:
            importlib.import_module(mod)
            print(f"  ✓ {mod}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {mod}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
