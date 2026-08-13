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

from jing_meta import embed as _embed

# Min cosine similarity to consider two entities related. bge-small produces
# dense, high-similarity vectors for true relations; use a high bar since these
# feed the LLM and we want precision over recall.
SIM_THRESHOLD = float(os.environ.get("SEMANTIC_SIM_THRESHOLD", "0.55"))
MAX_PAIRS = int(os.environ.get("SEMANTIC_MAX_PAIRS", "80"))


def embed_entities(graph: dict) -> list[tuple[dict, list[float]]]:
    """Return [(entity, vector)] for all entities (batched embedding)."""
    entities = graph["entities"]
    texts = [_embed._entity_text(e) for e in entities]
    emb = _embed._get_embedder()
    vecs = list(emb.embed(texts, batch_size=64))
    return list(zip(entities, vecs, strict=True))


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
