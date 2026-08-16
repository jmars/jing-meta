"""Semantic lookup layer for the memory graph.

Complements the lexical `search_nodes` / trigram `search_similar` with
offline, embedding-based semantic retrieval.

Design goals:
- **Offline & private**: embeddings via fastembed + onnxruntime (bge-small),
  no network.
- **Fast query path**: entity vectors are precomputed and cached to a file
  (`<db_dir>/entity_vectors.npy` + `entity_names.txt`). At query time we embed
  only the *query* (one pass) and cosine-compare against the cached index. The
  embedding model is loaded lazily and only if needed.
- **Additive signal**: semantic results are returned as a *supplement* to
  lexical results, never a replacement — exact matches stay first.
"""

import json
import os
from pathlib import Path

import numpy as np

from jing_meta import embed as _embed

_SIM_THRESHOLD = float(os.environ.get("SEMANTIC_LOOKUP_THRESHOLD", "0.50"))
_TOP_N = int(os.environ.get("SEMANTIC_LOOKUP_TOP", "10"))


# ---------------------------------------------------------------------------
# Index build / cache
# ---------------------------------------------------------------------------


def _index_paths(db_path: str) -> tuple[Path, Path]:
    db = Path(db_path)
    return (
        db.parent / f"{db.stem}.entity_vectors.npy",
        db.parent / f"{db.stem}.entity_names.json",
    )


def build_index(conn, db_path: str) -> tuple[Path, Path]:
    """Embed all entities and cache the vector index to disk.

    Returns (vectors_path, names_path). Call this from a maintenance step
    (e.g. after gardening), NOT on the hot query path.
    """
    rows = conn.execute("SELECT name, entity_type FROM entities").fetchall()
    entities = [
        {"name": r["name"], "entityType": r["entity_type"], "observations": []}
        for r in rows
    ]
    # Attach observations for richer embeddings
    for e in entities:
        obs = conn.execute(
            "SELECT content FROM observations WHERE entity_id = "
            "(SELECT id FROM entities WHERE name = ?) LIMIT 4",
            (e["name"],),
        ).fetchall()
        e["observations"] = [o["content"] for o in obs]

    try:
        vecs = np.array(
            [np.array(v, dtype="float32") for v in _embed._get_embedder().embed(
                [_embed._entity_text(e) for e in entities], batch_size=64
            )],
            dtype="float32",
        )
    finally:
        # Release the ONNX embedder after the batch — it costs ~240MB resident
        # and this is a maintenance path, not the hot query path. Reload is
        # cheap (model cached on disk) on the next semantic op.
        _embed.free()
    vpath, npath = _index_paths(db_path)
    np.save(vpath, vecs)
    npath.write_text(
        json.dumps([e["name"] for e in entities], ensure_ascii=False), encoding="utf-8"
    )
    return vpath, npath


def _load_index(db_path: str) -> tuple[np.ndarray, list[str]] | None:
    vpath, npath = _index_paths(db_path)
    if not vpath.exists() or not npath.exists():
        return None
    vecs = np.load(vpath)
    names = json.loads(npath.read_text(encoding="utf-8"))
    return vecs, names


# ---------------------------------------------------------------------------
# Query-time semantic search
# ---------------------------------------------------------------------------


def semantic_search(query: str, db_path: str, top_n: int = _TOP_N) -> list[dict]:
    """Return semantic neighbors of *query* among cached entity vectors.

    Returns [{name, entityType?, similarity}] ranked desc, filtered by
    threshold. Returns [] if no index built or embedder unavailable.
    """
    cached = _load_index(db_path)
    if cached is None:
        return []
    vecs, names = cached

    try:
        qvec = np.array(
            next(_embed._get_embedder().embed([query])), dtype="float32"
        )
    except Exception:
        return []
    finally:
        # Release the ONNX embedder after the single query embed — it costs
        # ~240MB resident and is only needed for this one pass. Holding it for
        # the server's lifetime inflated every jing-memory process that ever ran
        # a semantic search to ~300MB. Reload on next semantic op is cached+cheap.
        _embed.free()

    # cosine similarity against all cached vectors (normalized dot product)
    qnorm = np.linalg.norm(qvec)
    if qnorm == 0:
        return []
    qvec = qvec / qnorm
    vnorms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vnorms[vnorms == 0] = 1
    normed = vecs / vnorms
    sims = normed @ qvec

    idx = np.argsort(-sims)[:top_n]
    results = []
    for i in idx:
        s = float(sims[i])
        if s < _SIM_THRESHOLD:
            continue
        results.append({"name": names[i], "similarity": round(s, 3)})
    return results


def index_age(db_path: str) -> int:
    """Seconds since the vector index was built (or -1 if missing)."""
    import time

    vpath, _ = _index_paths(db_path)
    if not vpath.exists():
        return -1
    return int(time.time() - vpath.stat().st_mtime)
