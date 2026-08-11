"""Tests for the search index cache (search/indexer.py `_get_cached_index`).

Requires the C DAFSA core (libdafsa.so) and pytest. Run from the repo root:
    python -m pytest tests/ -v
"""

from pathlib import Path

import json
import pytest

# Skip cleanly when the C shared library is not built (e.g. CI without a toolchain).
try:
    from indexer.dafsa import _get_lib
    _get_lib()  # raises RuntimeError if libdafsa.so is missing or ABI-mismatched
except (RuntimeError, AttributeError, OSError) as e:
    pytest.skip(f"libdafsa.so not available: {e}", allow_module_level=True)


@pytest.fixture
def data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.jsonl").write_text("alpha banana", encoding="utf-8")
    return data


def _build(data: Path, out: Path) -> None:
    from indexer import build
    build(data, "*.jsonl", "jsonl", out)


def test_search_fst_cache_hit_and_miss(tmp_path, data_dir):
    from indexer import update
    from search.config import DomainConfig
    from search.indexer import (
        _get_cached_index,
        _index_cache,
        _index_cache_lock,
        search_fst,
    )

    out = tmp_path / "out"
    _build(data_dir, out)

    # isolate from other tests / prior cache state
    with _index_cache_lock:
        _index_cache.clear()

    cfg = DomainConfig(name="test", dir=str(data_dir), fst_index_dir=str(out), extractor="jsonl")

    r1 = search_fst(cfg, "alpha")
    assert r1, "indexed word should be searchable"

    # Same fst mtime + wal size -> cache hit, same object reused, cache stays 1.
    idx1 = _get_cached_index(out)
    search_fst(cfg, "alpha")
    idx2 = _get_cached_index(out)
    assert idx1 is idx2, "unchanged index must be reused from cache"
    with _index_cache_lock:
        assert len(_index_cache) == 1

    # Modify the file and update -> WAL size changes -> cache miss, old evicted.
    (data_dir / "a.jsonl").write_text("alpha grape", encoding="utf-8")
    res = update(data_dir, "*.jsonl", "jsonl", out)
    assert res["updated"] == 1

    idx3 = _get_cached_index(out)
    assert idx3 is not idx1, "changed index must be reopened (cache miss)"
    with _index_cache_lock:
        assert len(_index_cache) == 1, "stale entry for the dir must be evicted"

    # New content searchable through the fresh cached index.
    r3 = search_fst(cfg, "grape")
    assert r3
