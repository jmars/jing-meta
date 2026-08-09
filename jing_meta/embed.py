"""Shared embedding helper — one model loader for the whole system.

The indexer/dreamer/memory all use this so the ONNX model is loaded once and
memory stays bounded (call `free()` to release before loading a local LLM).
"""

import os
import threading

import numpy as np

from . import config

_embedder = None
_lock = threading.Lock()


def _get_embedder():
    """Lazily init and return the ONNX embedder singleton (downloads on first use)."""
    global _embedder
    with _lock:
        if _embedder is None:
            from fastembed import TextEmbedding

            _embedder = TextEmbedding(model_name=config.EMBED_MODEL)
    return _embedder


def embed(texts, batch_size: int = 64) -> list[np.ndarray]:
    """Embed a list of strings -> list of float32 vectors. Lazy model init."""
    return [np.asarray(v, dtype="float32") for v in _get_embedder().embed(texts, batch_size=batch_size)]


def _entity_text(e: dict) -> str:
    """Build the text we embed for an entity: name + top observations."""
    obs = e.get("observations", []) or []
    return e.get("name", "") + ". " + " ".join(str(o) for o in obs[:4])


def embed_one(text: str) -> np.ndarray:
    """Embed a single string -> float32 vector."""
    return embed([text])[0]


def cosine(a, b) -> float:
    """Cosine similarity between two vectors."""
    a = np.asarray(a, dtype="float32")
    b = np.asarray(b, dtype="float32")
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def free() -> None:
    """Release the embedder to free RAM (call before loading a local LLM)."""
    global _embedder
    with _lock:
        _embedder = None
    import gc

    gc.collect()
