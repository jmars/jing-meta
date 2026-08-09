"""jing-indexer CLI — build and search DAFSA indexes."""

import argparse
import json
import sys
from pathlib import Path

from . import build, open_index


def _build(args) -> int:
    build(Path(args.dir), args.pattern, args.extractor, Path(args.output))
    return 0


def _query(args) -> int:
    with open_index(Path(args.index)) as idx:
        hits = idx.search(args.query, any_word=args.any)
        results = []
        for h in hits:
            results.append({
                "file_idx": h.file_idx,
                "entry_idx": h.entry_idx,
                "file": idx.file_name(h.file_idx),
            })
        print(json.dumps({"query": args.query, "hits": results}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jing-indexer", description=(
        "DAFSA-based full-text indexer (Python frontend to the C DAFSA core)."
    ))
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build an index")
    b.add_argument("--dir", required=True)
    b.add_argument("--pattern", required=True)
    b.add_argument("--extractor", choices=["jsonl", "txt", "transcript"], required=True)
    b.add_argument("--output", required=True)
    b.set_defaults(fn=_build)

    q = sub.add_parser("query", help="search an index")
    q.add_argument("--index", required=True)
    q.add_argument("--query", required=True)
    q.add_argument("--any", action="store_true", help="OR-mode (union) instead of AND")
    q.set_defaults(fn=_query)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
