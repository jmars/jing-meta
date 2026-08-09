#!/usr/bin/env bash
#
# diff_vs_live.sh — parity check between the live Rust fst-indexer and the
# Python jing-meta DAFSA indexer.
#
# Builds the same corpus with both indexers, runs a common query set against
# both, normalizes the JSON hits to a metadata-free shape, and diffs them.
# Prints PASS/FAIL per query; exits non-zero if any query mismatches.
#
# NOTE: this script drives real binaries (fork/exec of installed tools, cargo
# not needed) and is intended to run OUTSIDE the sandbox.
#
# Environment:
#   FST_INDEXER    path to the Rust fst-indexer binary (default: ~/.local/bin/fst-indexer)
#   JING_INDEXER   command to run the Python indexer (default: python3 -m indexer)
#   CORPUS_DIR     directory containing the source corpus (REQUIRED)
#   PATTERN        glob for corpus files   (default: **/*.jsonl)
#   EXTRACTOR      extractor type          (default: jsonl)
#
# Usage:
#   CORPUS_DIR=/path/to/corpus ./diff_vs_live.sh [queries.txt]
#   - queries.txt: one query per line.  If omitted, a default set of 20
#     common words is generated.
set -euo pipefail

FST_INDEXER="${FST_INDEXER:-/home/arch/.local/bin/fst-indexer}"
# NOTE: default uses `python3 -m indexer` (the real jing-meta entry point
# declared in pyproject.toml's `jing-indexer` script / the `indexer` package),
# NOT `jing_meta.indexer` which does not exist. Override via JING_INDEXER for
# another environment.
JING_INDEXER="${JING_INDEXER:-python3 -m indexer}"
CORPUS_DIR="${CORPUS_DIR:-}"
PATTERN="${PATTERN:-**/*.jsonl}"
EXTRACTOR="${EXTRACTOR:-jsonl}"

RUST_OUT=/tmp/diff_rust_out
PY_OUT=/tmp/diff_python_out
MAX=${MAX:-20}

if [ -z "$CORPUS_DIR" ]; then
    echo "ERROR: CORPUS_DIR must be set" >&2
    echo "  CORPUS_DIR=/path/to/corpus $0 [queries.txt]" >&2
    exit 2
fi

if [ ! -x "$FST_INDEXER" ]; then
    echo "ERROR: FST_INDEXER not executable: $FST_INDEXER" >&2
    exit 2
fi

echo "==> fst-indexer:   $FST_INDEXER"
echo "==> jing indexer:  $JING_INDEXER"
echo "==> corpus:        $CORPUS_DIR (pattern '$PATTERN', extractor '$EXTRACTOR')"

# ── Build both fresh ─────────────────────────────────────────────────────
rm -rf "$RUST_OUT" "$PY_OUT"
mkdir -p "$RUST_OUT" "$PY_OUT"

echo "==> building Rust index (fresh)..."
"$FST_INDEXER" build --dir "$CORPUS_DIR" --pattern "$PATTERN" \
    --extractor "$EXTRACTOR" --output "$RUST_OUT/index.fst"

echo "==> building Python index (fresh)..."
$JING_INDEXER build --dir "$CORPUS_DIR" --pattern "$PATTERN" \
    --extractor "$EXTRACTOR" --output "$PY_OUT/index.fst"

# ── Query set ────────────────────────────────────────────────────────────
QUERIES_FILE="${1:-}"
if [ -n "$QUERIES_FILE" ]; then
    mapfile -t QUERIES < <(sed '/^[[:space:]]*$/d' "$QUERIES_FILE")
    echo "==> ${#QUERIES[@]} queries loaded from $QUERIES_FILE"
else
    QUERIES=(the and that with from have will this they what where when who \
             which there their about would could should because really other \
             information)
    echo "==> using default query set (${#QUERIES[@]} words)"
fi
if [ "${#QUERIES[@]}" -eq 0 ]; then
    echo "ERROR: empty query set" >&2
    exit 2
fi

# ── Run + diff ───────────────────────────────────────────────────────────
fails=0
total=0
for q in "${QUERIES[@]}"; do
    total=$((total + 1))

    rust_norm=$(mktemp)
    py_norm=$(mktemp)

    # Rust: search (default --max $MAX); Python: query then slice to $MAX in
    # the normalizer so both compare the same top-$MAX set.
    "$FST_INDEXER" search -i "$RUST_OUT" "$q" --max "$MAX" 2>/dev/null \
        | MAX="$MAX" python3 -c '
import json, sys
d = json.load(sys.stdin)
rows = d.get("results", d.get("hits", []))
out = sorted([int(r["file_idx"]), int(r["entry_idx"])] for r in rows[:int(__import__("os").environ["MAX"])])
print(json.dumps(out, sort_keys=True))
' > "$rust_norm"

    $JING_INDEXER query --index "$PY_OUT" --query "$q" 2>/dev/null \
        | MAX="$MAX" python3 -c '
import json, sys
d = json.load(sys.stdin)
rows = d.get("results", d.get("hits", []))
out = sorted([int(r["file_idx"]), int(r["entry_idx"])] for r in rows[:int(__import__("os").environ["MAX"])])
print(json.dumps(out, sort_keys=True))
' > "$py_norm"

    if diff -q "$rust_norm" "$py_norm" >/dev/null 2>&1; then
        echo "PASS  \"$q\""
    else
        echo "FAIL  \"$q\""
        echo "   rust:   $(cat "$rust_norm")"
        echo "   python: $(cat "$py_norm")"
        fails=$((fails + 1))
    fi

    rm -f "$rust_norm" "$py_norm"
done

echo
echo "==> $((total - fails))/$total queries matched"
if [ "$fails" -gt 0 ]; then
    echo "DIFF_VS_LIVE FAILED: $fails query(s) mismatched" >&2
    exit 1
fi
echo "DIFF_VS_LIVE PASSED"
exit 0
