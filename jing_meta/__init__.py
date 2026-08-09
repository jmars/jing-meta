"""jing-meta — unified self-hosted knowledge system.

One coherent system where the DAFSA indexer, cross-domain search, memory graph,
and dreamer all share conventions (storage paths, the memory DB, the embedding
model) instead of being disconnected repos.

Subpackages:
  indexer   — DAFSA-based full-text indexer (Python frontend to the C DAFSA core)
  search    — cross-domain search over indexes (formerly unified-history-mcp)
  memory    — the SQLite memory graph + offline semantic lookup (formerly memory-mcp)
  dreamer  — knowledge-graph maintenance (Soufflé + semantic rerank + validator)
"""
