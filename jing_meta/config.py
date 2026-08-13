"""Shared configuration — one source of truth for storage paths and models.

Every jing-meta component reads paths/models from here, so the whole system
agrees on where data lives and which models to use.
"""

import os
from pathlib import Path

# Root data directory. Override per-install with JING_HOME.
JING_HOME = Path(os.environ.get("JING_HOME", Path.home() / ".jing"))


def memory_db() -> Path:
    """Path to the SQLite memory graph. Override with MEMORY_DB_PATH."""
    return Path(os.environ.get("MEMORY_DB_PATH", JING_HOME / "memory.db"))


def memory_dir() -> Path:
    """Directory for memory-side auxiliary files (archives, semantic index)."""
    d = JING_HOME / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_dir() -> Path:
    """Where the archiver moves old observations. Override with MEMORY_ARCHIVE_DIR."""
    d = Path(os.environ.get("MEMORY_ARCHIVE_DIR", JING_HOME / "archives" / "memory"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_root() -> Path:
    """Where searchable domain indexes live. Override with JING_INDEX_ROOT."""
    d = Path(os.environ.get("JING_INDEX_ROOT", JING_HOME / "indexes"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# Embedding model for semantic search + reranking (offline, ONNX).
EMBED_MODEL = os.environ.get("JING_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# Local LLM for relation validation (Ollama). Override with JING_LOCAL_LLM.
# qwen3.5:2b-q4_k_m (Qwen 3.5, Q4_K_M weights) + q4_0 KV cache (see the
# ollama systemd drop-in) — offline, fast, private.
LOCAL_LLM_MODEL = os.environ.get("JING_LOCAL_LLM", "qwen3.5:2b-q4_k_m")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Cloud LLM (OpenAI-compatible) used by the dreamer's "cloud" validator path and
# any GRAPH_GARDENER_API_URL-less `llm.call`. Override with JING_CLOUD_LLM_URL /
# JING_CLOUD_LLM_MODEL, or at call-time via GRAPH_GARDENER_API_URL /
# GRAPH_GARDENER_MODEL. Defaults to the local DeepInfra cache proxy
# (http://127.0.0.1:8322) so dreamer cloud calls are logged to
# deepinfra-usage.jsonl and share the proxy's prompt cache. The proxy forwards
# the request body verbatim (including service_tier) to api.deepinfra.com and
# injects DEEPINFRA_API_KEY, so no key is needed from the caller. Serving
# Mistral-Small-24B — a cheap, JSON-reliable relation-naming model.
CLOUD_LLM_URL = os.environ.get(
    "JING_CLOUD_LLM_URL", "http://127.0.0.1:8322/v1/openai"
)
CLOUD_LLM_MODEL = os.environ.get(
    "JING_CLOUD_LLM_MODEL", "mistralai/Mistral-Small-24B-Instruct-2501"
)

# DeepInfra service tier for cloud LLM calls. Flex (0.8x base) is for
# non-production / asynchronous work — slower responses and occasional
# unavailability, in exchange for ~20% lower cost. Defaults to "flex" because
# the primary consumer (the detached background graph-gardener) is exactly that
# workload. Override with JING_CLOUD_SERVICE_TIER ("" for Standard, "priority"
# for 1.5x). Only models that advertise the tier honor it.
CLOUD_SERVICE_TIER = os.environ.get("JING_CLOUD_SERVICE_TIER", "flex").strip()

# External tools
SOUFFLE = os.environ.get("JING_SOUFFLE", "/usr/local/bin/souffle")


def vector_index_paths(db: Path | None = None):
    """(vectors_path, names_path) for a memory DB's semantic index."""
    db = db or memory_db()
    return (
        db.parent / f"{db.stem}.entity_vectors.npy",
        db.parent / f"{db.stem}.entity_names.json",
    )
