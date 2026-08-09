"""Semantic search tier — finds conceptually-related entities via embeddings.

Complements the Soufflé lexical tier. Soufflé catches pairs that share words;
this catches pairs that are *semantically* related even when they share no
significant tokens (e.g. "GC heap corruption" ~ "memory allocator bug"). Runs
fully offline via a small ONNX model (fastembed + onnxruntime), so nothing
leaves the machine.

This is a DISCOVERY aid, not a decision maker. Candidates it produces must be
validated by the LLM before being applied — embeddings are probabilistic, so a
high similarity threshold is required and every pair is treated as fuzzy.
"""

import os
from pathlib import Path

# Model choice: BAAI/bge-small-en-v1.5 (384-dim, small, offline, good quality).
EMBED_MODEL = os.environ.get("SEMANTIC_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
# Min cosine similarity to consider two entities related. bge-small produces
# dense, high-similarity vectors for true relations; use a high bar since these
# feed the LLM and we want precision over recall.
SIM_THRESHOLD = float(os.environ.get("SEMANTIC_SIM_THRESHOLD", "0.55"))
MAX_PAIRS = int(os.environ.get("SEMANTIC_MAX_PAIRS", "40"))

_embedder = None


def _get_embedder():
    """Lazily init the ONNX embedder (downloads model on first use)."""
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def free_embedder() -> None:
    """Release the ONNX embedder to free RAM (e.g. before loading a local LLM).

    The embedder is large (~1GB). In memory-constrained environments, call this
    after semantic reranking and before a local LLM (Ollama) is loaded, so the
    two models don't collide and OOM.
    """
    global _embedder
    _embedder = None
    import gc

    gc.collect()


def _entity_text(e: dict) -> str:
    """Build the text we embed for an entity: name + top observations."""
    obs = e.get("observations", [])
    return e.get("name", "") + ". " + " ".join(str(o) for o in obs[:4])


def cosine(a, b) -> float:
    """Cosine similarity between two vectors."""
    import numpy as np

    a = np.asarray(a, dtype="float32")
    b = np.asarray(b, dtype="float32")
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def embed_entities(graph: dict) -> list[tuple[dict, list[float]]]:
    """Return [(entity, vector)] for all entities (batched embedding)."""
    entities = graph["entities"]
    texts = [_entity_text(e) for e in entities]
    emb = _get_embedder()
    vecs = list(emb.embed(texts, batch_size=64))
    return list(zip(entities, vecs))


def semantic_candidates(graph: dict) -> list[dict]:
    """Find semantically-similar entity pairs (lexically-independent).

    Returns ranked list of {"from","to","similarity"}, excluding:
      - already-related pairs (either direction)
      - identical entities
    Capped at MAX_PAIRS, highest similarity first.
    """
    import numpy as np

    existing = set()
    for r in graph.get("relations", []):
        existing.add((r.get("from"), r.get("to")))
        existing.add((r.get("to"), r.get("from")))

    pairs = embed_entities(graph)
    names = [e["name"] for e, _ in pairs]
    mat = np.array([v for _, v in pairs], dtype="float32")
    # normalize rows
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1
    mat = mat / norms
    sim = mat @ mat.T

    results = []
    n = len(names)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i][j])
            if s < SIM_THRESHOLD:
                continue
            if (names[i], names[j]) in existing:
                continue
            results.append({"from": names[i], "to": names[j], "similarity": round(s, 3)})

    results.sort(key=lambda r: -r["similarity"])
    return results[:MAX_PAIRS]
