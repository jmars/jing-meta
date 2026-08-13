"""Tests for jing_meta.hooks (Vibe maintenance hook glue).

Covers the digest helper, state persistence, and graph-count logic in
``jing_meta.hooks.common``. The hook entry points are thin wrappers around the
engines; we test the shared helpers here to avoid needing a live memory DB or
running the dreamer.
"""

import sqlite3

from jing_meta.hooks import common
from jing_meta.schema import SCHEMA_DDL


def test_digest_append_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    common.digest_append({"system": "graph-gardener", "action": "maintenance", "detail": "3 mutations"})
    common.digest_append({"system": "memory-archiver", "action": "archive", "detail": "archived 2 observations"})
    records = common.digest_read()
    assert len(records) == 2
    # Newest first
    assert records[0]["system"] == "memory-archiver"
    assert records[1]["detail"] == "3 mutations"
    # Each record got a timestamp
    assert "ts" in records[0]


def test_digest_caps_at_max_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    for i in range(common._MAX_DIGEST_ENTRIES + 50):
        common.digest_append({"system": "test", "action": "x", "detail": str(i)})
    records = common.digest_read(limit=common._MAX_DIGEST_ENTRIES + 100)
    assert len(records) == common._MAX_DIGEST_ENTRIES
    # The oldest entries were trimmed
    details = {r["detail"] for r in records}
    assert "0" not in details


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    common.save_state("test-hook-last", {"last_run": 123, "entities": 5, "obs": 9})
    loaded = common.load_state("test-hook-last")
    assert loaded == {"last_run": 123, "entities": 5, "obs": 9}


def test_state_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    assert common.load_state("does-not-exist") == {}


def test_graph_counts(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_DDL)
    conn.execute("INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?,?,?,?)",
                 ("e1", "thing", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?,?,?,?)",
                 ("e2", "thing", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO observations (entity_id, content, created_at) VALUES (1, 'obs1', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO observations (entity_id, content, created_at) VALUES (1, 'obs2', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    assert common.graph_counts(db) == (2, 2)


def test_graph_counts_missing_db(tmp_path):
    assert common.graph_counts(tmp_path / "nope.db") is None


def test_memory_db_and_archive_dir_respect_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("MEMORY_ARCHIVE_DIR", str(tmp_path / "archives"))
    assert common.memory_db() == tmp_path / "custom.db"
    assert common.archive_dir() == tmp_path / "archives"


# ---------------------------------------------------------------------------
# Gardener hook run-id persistence
# ---------------------------------------------------------------------------


def test_new_run_id_is_runstore_style(tmp_path, monkeypatch):
    """_new_run_id returns a collision-safe id in the RunStore format."""
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    from jing_meta.hooks import gardener

    rid = gardener._new_run_id()
    # YYYYMMDDTHHMMSSZ (RunStore.new_run_id), possibly with a -N suffix.
    import re
    assert re.fullmatch(r"\d{8}T\d{6}Z(-\d+)?", rid)


def test_record_digest_includes_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    from jing_meta.hooks import gardener

    gardener._record_digest("TOTAL mutations: 4\n  add_relations: 2", run_id="20260813T000000Z")
    recs = common.digest_read()
    assert recs[0]["system"] == "graph-gardener"
    assert "run_id=20260813T000000Z" in recs[0]["detail"]
    assert "4 mutations" in recs[0]["detail"]


def test_maintenance_bg_forwards_run_id(monkeypatch):
    """_run_maintenance_bg passes run_id through to _run_dreamer."""
    from jing_meta.hooks import gardener

    captured = {}
    def fake_run_dreamer(run_id=None):
        captured["run_id"] = run_id
        return 0, "TOTAL mutations: 0\n"
    monkeypatch.setattr(gardener, "_run_dreamer", fake_run_dreamer)
    monkeypatch.setattr(gardener, "_record_digest", lambda out, run_id=None: None)
    monkeypatch.setattr(gardener, "_rebuild_semantic_index", lambda: None)

    gardener._run_maintenance_bg(run_id="20260813T000000Z")
    assert captured["run_id"] == "20260813T000000Z"
    # Without a run_id, it forwards None (in-memory only).
    gardener._run_maintenance_bg()
    assert captured["run_id"] is None


def test_cloud_config_defaults_to_deepinfra_cloud(monkeypatch):
    """The gardener defaults to the DeepInfra cloud validator + Flex tier."""
    from jing_meta import config as _config
    from jing_meta.hooks import gardener

    for var in ("GRAPH_GARDENER_VALIDATOR", "GRAPH_GARDENER_API_URL",
                "GRAPH_GARDENER_API_KEY", "GRAPH_GARDENER_MODEL", "DEEPINFRA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "di-key")

    validator, api_url, api_key, model = gardener._cloud_config()
    assert validator == "cloud"
    assert api_url == _config.CLOUD_LLM_URL
    assert api_key == "di-key"
    assert model == _config.CLOUD_LLM_MODEL


def test_cloud_config_env_overrides(monkeypatch):
    """Explicit GRAPH_GARDENER_* env vars override the cloud defaults."""
    from jing_meta.hooks import gardener

    monkeypatch.setenv("GRAPH_GARDENER_VALIDATOR", "local")
    monkeypatch.setenv("GRAPH_GARDENER_API_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")
    monkeypatch.setenv("GRAPH_GARDENER_MODEL", "qwen3.5:2b")

    validator, api_url, api_key, model = gardener._cloud_config()
    assert validator == "local"
    assert api_url == "http://localhost:11434/v1"
    assert api_key == "k"
    assert model == "qwen3.5:2b"
