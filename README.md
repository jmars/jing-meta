# jing-meta

A unified, fully self-hosted knowledge system. One coherent repo where the
DAFSA indexer, cross-domain search, memory graph, and auto-dreamer share
conventions (storage paths, the memory DB, the embedding model) instead of
being disconnected repos.

Everything runs **offline**: compiled Datalog (Soufflé) for exact structural
work, ONNX embeddings for semantic ranking, and a small local LLM (Ollama) for
judgment. No cloud, no external services.

## Components

| Package | What it is | Entry point |
|---|---|---|
| `indexer` | DAFSA-based full-text indexer (Python frontend to the C DAFSA core) | `jing-indexer` |
| `search` | Cross-domain search over indexes (config-driven) | `jing-search` |
| `memory` | The SQLite memory graph + offline semantic lookup | `jing-memory` |
| `archiver` | Moves old observations out of the live memory graph into searchable archives | `jing-archiver` |
| `dreamer` | Knowledge-graph maintenance (Soufflé + semantic rerank + local validator) | `jing-dreamer` |
| `jing_meta` | Shared core: config, storage paths, model registry, embed helper | `jing-meta` |

## Layout

```
jing_meta/        shared config + embed + CLI
indexer/          Python DAFSA frontend (ctypes) + build/search
  dafsa/          C DAFSA core (dafsa.c/h) + libdafsa.so
search/           config-driven cross-domain search over indexes
memory/           SQLite memory graph + semantic index + archiver
dreamer/         Soufflé ruleset + semantic rerank + local validator
```

## Install

```bash
# build the C DAFSA shared lib once
cd indexer/dafsa && gcc -shared -fPIC -O2 -o libdafsa.so dafsa.c && cd ../..

# install (uv)
uv sync
# or run directly with PYTHONPATH=. 
export PYTHONPATH="$PWD"
```

## Quick start

```bash
# Build a DAFSA index over jsonl files
jing-indexer build --dir ~/some/data --pattern "*.jsonl" --extractor jsonl --output /tmp/idx

# Search it
jing-indexer query --index /tmp/idx --query "dafsa"

# Garden the memory graph (offline; dry-run)
jing-dreamer --memory-db ~/.jing/memory.db --souffle --validator local

# Archive observations older than 90 days (dry-run; add --apply to move)
jing-archiver --days 90

# Verify all subsystems import
jing-meta verify
```

## Data locations (shared via `jing_meta.config`)

| Data | Path | Override |
|---|---|---|
| Root | `~/.jing` | `JING_HOME` |
| Memory DB | `~/.jing/memory.db` | `MEMORY_DB_PATH` |
| Search indexes | `~/.jing/indexes` | `JING_INDEX_ROOT` |
| Memory archives | `~/.jing/archives/memory` | `MEMORY_ARCHIVE_DIR` |

## The pipeline (dreamer)

```
memory.db
  → Soufflé (lexical token-overlap, exact, sub-second)   → type renames + duplicates
  → semantic rerank (ONNX bge-small, offline)            → top-N conceptual candidates
  → rules + local LLM (Ollama qwen2.5:1.5b)              → validate + name relations
  → apply (backed up, additive-only)
```

All offline. `validator` can be `local` (default), `cloud`, or `none`.
