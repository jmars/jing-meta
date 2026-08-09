"""Tests for the jing-meta memory stats server (memory/stats_server.py).

Verifies the read-only overview/discovery tools against a synthetic SQLite
graph built in a tmp dir. ``DB_PATH`` is patched per-test (the module reads it
at import time), so the tools are exercised against the temp DB — and against a
nonexistent path for the missing-DB case.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory import stats_server
from jing_meta.schema import SCHEMA_DDL


def _make_test_db(path: Path) -> None:
    """Build a synthetic graph: 4 entities, 4 observations, 2 relations.

    Includes an entity with no relations and one with no observations, so the
    summary/overview code paths for those shapes are exercised.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_DDL)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _add_entity(name: str, etype: str, obs: list[str]) -> None:
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, etype, now, now),
        )
        eid = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()[0]
        for content in obs:
            conn.execute(
                "INSERT INTO observations (entity_id, content, created_at) "
                "VALUES (?, ?, ?)",
                (eid, content, now),
            )

    _add_entity("alpha", "concept", ["alpha obs 1", "alpha obs 2"])
    _add_entity("beta", "person", ["beta obs 1"])
    _add_entity("gamma", "concept", [])  # entity with no observations / relations
    _add_entity("delta", "project", ["delta obs 1"])

    conn.execute(
        "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) "
        "VALUES ('alpha', 'beta', 'relates_to', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) "
        "VALUES ('delta', 'alpha', 'depends_on', ?)",
        (now,),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def _db(monkeypatch, tmp_path):
    """Point the stats server at a fresh synthetic graph in a tmp dir."""
    db = tmp_path / "memory.db"
    _make_test_db(db)
    monkeypatch.setattr(stats_server, "DB_PATH", db)
    return db


class TestGraphStats:
    def test_counts(self, _db):
        out = stats_server.graph_stats()
        assert "Entities: 4, Relations: 2, Observations: 4" in out

    def test_entity_types(self, _db):
        out = stats_server.graph_stats()
        assert "concept: 2" in out
        assert "person: 1" in out
        assert "project: 1" in out

    def test_relation_types(self, _db):
        out = stats_server.graph_stats()
        assert "relates_to: 1" in out
        assert "depends_on: 1" in out

    def test_recent_and_temporal(self, _db):
        out = stats_server.graph_stats()
        assert "[concept] alpha" in out
        assert "Oldest:" in out and "Newest:" in out
        assert "24h activity:" in out

    def test_missing_db_returns_message(self, monkeypatch):
        monkeypatch.setattr(
            stats_server, "DB_PATH", Path("/nonexistent/does-not-exist.db")
        )
        out = stats_server.graph_stats()
        assert "Knowledge graph database not found" in out


class TestEntitySummary:
    def test_present_entity(self, _db):
        out = stats_server.entity_summary("alpha")
        assert "Entity: alpha" in out
        assert "Type: concept" in out
        assert "Observations (2):" in out
        assert "alpha obs 1" in out
        assert "Outgoing relations (1):" in out
        assert "→ beta (relates_to)" in out
        assert "Incoming relations (1):" in out
        assert "delta → (depends_on)" in out

    def test_entity_no_observations(self, _db):
        out = stats_server.entity_summary("gamma")
        assert "Entity: gamma" in out
        assert "Observations (0):" in out

    def test_missing_entity(self, _db):
        assert stats_server.entity_summary("missing") == "Entity not found: missing"

    def test_missing_db_returns_message(self, monkeypatch):
        monkeypatch.setattr(
            stats_server, "DB_PATH", Path("/nonexistent/does-not-exist.db")
        )
        out = stats_server.entity_summary("alpha")
        assert "Knowledge graph database not found" in out
