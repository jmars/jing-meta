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
LOCAL_LLM_MODEL = os.environ.get("JING_LOCAL_LLM", "qwen2.5:1.5b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# External tools
SOUFFLE = os.environ.get("JING_SOUFFLE", "/usr/local/bin/souffle")


def vector_index_paths(db: Path | None = None):
    """(vectors_path, names_path) for a memory DB's semantic index."""
    db = db or memory_db()
    return (
        db.parent / f"{db.stem}.entity_vectors.npy",
        db.parent / f"{db.stem}.entity_names.json",
    )
