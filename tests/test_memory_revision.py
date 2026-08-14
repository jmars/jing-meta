"""Tests for per-entity optimistic concurrency (revision) in memory/server.py.

Covers the ``revision`` column migration, its surfacing on reads
(``open_nodes`` / ``recent``), and compare-and-swap enforcement on the write
tools (``add_observations``, ``delete_observations``, ``delete_entities``),
plus backward compatibility for callers that do not pass an ``expectedRev``.
"""

import sqlite3

import pytest

from jing_meta.schema import SCHEMA_DDL, ensure_schema
from memory import server


def _blocks_text(blocks) -> str:
    return "\n".join(b.text for b in blocks)


def _rev(db, name):
    """Return the stored revision of *name*, or None if it doesn't exist."""
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT revision FROM entities WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _obs(db, name):
    """Return the observation contents of *name*, in insertion order."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT content FROM observations WHERE entity_id = "
            "(SELECT id FROM entities WHERE name = ?) ORDER BY id",
            (name,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _entity_exists(db, name):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT 1 FROM entities WHERE name = ?", (name,)
        ).fetchone() is not None
    finally:
        conn.close()


@pytest.fixture
def _db(monkeypatch, tmp_path):
    """Point the memory server at a fresh, empty, current-schema DB in a tmp dir."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", db)
    server._conn = None  # force a fresh connection to the temp DB
    return db


def _insert_entity(db, name, rev=0, obs=()):
    """Insert an entity (and optional observations) with a specific revision."""
    conn = sqlite3.connect(str(db))
    now = server._now()
    conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at, revision) "
        "VALUES (?, 't', ?, ?, ?)",
        (name, now, now, rev),
    )
    eid = conn.execute(
        "SELECT id FROM entities WHERE name = ?", (name,)
    ).fetchone()[0]
    for content in obs:
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (eid, content, now),
        )
    conn.commit()
    conn.close()
    return eid


class TestMigration:
    def test_migration_backfills_revision_zero(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        # Old schema: no revision column.
        conn.execute(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT UNIQUE, entity_type TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('x', 't', 'now', 'now')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
        assert "revision" in cols
        assert conn.execute(
            "SELECT revision FROM entities WHERE name = 'x'"
        ).fetchone()[0] == 0
        conn.close()

    def test_ensure_schema_is_idempotent(self, tmp_path):
        db = tmp_path / "db.sqlite"
        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        ensure_schema(conn)  # second call must not error or duplicate the column
        cols = [r[1] for r in conn.execute("PRAGMA table_info(entities)")]
        assert cols.count("revision") == 1
        conn.close()


class TestRevisionSurfacing:
    def test_open_nodes_surfaces_revision(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["hello"])
        out = _blocks_text(server.open_nodes(["x"]))
        assert "Revision: 0" in out

    def test_recent_surfaces_revision(self, _db):
        _insert_entity(_db, "x", rev=3, obs=["hello"])
        out = _blocks_text(server.recent(hours=168))
        assert "Revision: 3" in out

    def test_open_nodes_no_revision_for_missing(self, _db):
        # A missing entity produces no Revision block at all.
        out = _blocks_text(server.open_nodes(["nope"]))
        assert "Revision:" not in out


class TestAddObservationsCAS:
    def test_matching_expected_rev_succeeds_and_bumps(self, _db):
        _insert_entity(_db, "x", rev=0)
        msg = server.add_observations(
            [{"entityName": "x", "contents": ["new obs"], "expectedRev": 0}]
        )
        assert "Added 1 observations" in msg
        assert "CONFLICT" not in msg
        assert _rev(_db, "x") == 1
        assert _obs(_db, "x") == ["new obs"]

    def test_stale_expected_rev_conflicts(self, _db):
        _insert_entity(_db, "x", rev=0)
        # Advance the revision to 1 via an unguarded write.
        server.add_observations([{"entityName": "x", "contents": ["first"]}])
        assert _rev(_db, "x") == 1

        msg = server.add_observations(
            [{"entityName": "x", "contents": ["second"], "expectedRev": 0}]
        )
        assert "CONFLICT: entity 'x' revision changed (expected 0, current 1)" in msg
        # Observation NOT inserted and revision unchanged.
        assert "second" not in _obs(_db, "x")
        assert _rev(_db, "x") == 1

    def test_without_expected_rev_still_works_and_bumps(self, _db):
        _insert_entity(_db, "x", rev=0)
        msg = server.add_observations([{"entityName": "x", "contents": ["a", "b"]}])
        assert "Added 2 observations" in msg
        assert _rev(_db, "x") == 1

    def test_batch_partial_commit_on_conflict(self, _db):
        _insert_entity(_db, "a", rev=0)
        _insert_entity(_db, "b", rev=0)
        # Advance 'a' to revision 1, leave 'b' at 0.
        server.add_observations([{"entityName": "a", "contents": ["prior"]}])

        msg = server.add_observations([
            {"entityName": "a", "contents": ["stale"], "expectedRev": 0},  # conflicts
            {"entityName": "b", "contents": ["fresh"], "expectedRev": 0},  # applies
        ])
        assert "CONFLICT" in msg
        assert "Added 1 observations" in msg
        assert "stale" not in _obs(_db, "a")
        assert _obs(_db, "b") == ["fresh"]
        assert _rev(_db, "b") == 1

    def test_add_observations_dedupes_existing_content(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["dup"])
        msg = server.add_observations([{"entityName": "x", "contents": ["dup"]}])
        assert "Added 0 observations" in msg
        assert _rev(_db, "x") == 0
        assert _obs(_db, "x") == ["dup"]

    def test_add_observations_dedup_mixed(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["a"])
        msg = server.add_observations([{"entityName": "x", "contents": ["a", "b"]}])
        assert "Added 1 observations" in msg
        assert _rev(_db, "x") == 1
        assert _obs(_db, "x") == ["a", "b"]

    def test_add_observations_dedup_matching_expectedRev_no_bump(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["a"])
        msg = server.add_observations(
            [{"entityName": "x", "contents": ["a"], "expectedRev": 0}]
        )
        assert "CONFLICT" not in msg
        assert "Added 0 observations" in msg
        assert _rev(_db, "x") == 0

    def test_add_observations_dedup_stale_expectedRev_is_noop(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["a"])
        # Advance the revision to 1 via an unguarded write.
        server.add_observations([{"entityName": "x", "contents": ["b"]}])
        assert _rev(_db, "x") == 1

        # Re-adding 'a' with a stale expectedRev inserts nothing => idempotent no-op.
        msg = server.add_observations(
            [{"entityName": "x", "contents": ["a"], "expectedRev": 0}]
        )
        assert "CONFLICT" not in msg
        assert "Added 0 observations" in msg
        assert _rev(_db, "x") == 1


class TestDeleteEntitiesCAS:
    def test_matching_rev_deletes(self, _db):
        _insert_entity(_db, "x", rev=0)
        msg = server.delete_entities(["x"], expectedRevs={"x": 0})
        assert "Deleted 1 entities" in msg
        assert not _entity_exists(_db, "x")

    def test_stale_rev_conflicts_and_keeps_entity(self, _db):
        _insert_entity(_db, "x", rev=0)
        server.add_observations([{"entityName": "x", "contents": ["bump"]}])
        assert _rev(_db, "x") == 1

        msg = server.delete_entities(["x"], expectedRevs={"x": 0})
        assert "CONFLICT: entity 'x' revision changed (expected 0, current 1)" in msg
        assert _entity_exists(_db, "x")
        assert _rev(_db, "x") == 1

    def test_not_found_with_expected_rev_is_silent(self, _db):
        # A missing name with an expectedRev is not-found, not a conflict.
        msg = server.delete_entities(["ghost"], expectedRevs={"ghost": 0})
        assert msg == "Deleted 0 entities."

    def test_unconditional_delete_unchanged(self, _db):
        _insert_entity(_db, "x", rev=5)
        msg = server.delete_entities(["x"])
        assert "Deleted 1 entities" in msg
        assert not _entity_exists(_db, "x")


class TestDeleteObservationsCAS:
    def test_matching_rev_deletes(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["to delete"])
        msg = server.delete_observations(
            [{"entityName": "x", "observations": ["to delete"], "expectedRev": 0}]
        )
        assert "Deleted 1 observations" in msg
        assert _obs(_db, "x") == []
        assert _rev(_db, "x") == 1

    def test_stale_rev_conflicts_and_keeps_obs(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["keep me"])
        server.add_observations([{"entityName": "x", "contents": ["bump"]}])
        assert _rev(_db, "x") == 1

        msg = server.delete_observations(
            [{"entityName": "x", "observations": ["keep me"], "expectedRev": 0}]
        )
        assert "CONFLICT: entity 'x' revision changed (expected 0, current 1)" in msg
        assert "keep me" in _obs(_db, "x")
        assert _rev(_db, "x") == 1

    def test_without_expected_rev_still_works(self, _db):
        _insert_entity(_db, "x", rev=0, obs=["drop"])
        msg = server.delete_observations(
            [{"entityName": "x", "observations": ["drop"]}]
        )
        assert "Deleted 1 observations" in msg
        assert _obs(_db, "x") == []
        assert _rev(_db, "x") == 1


class TestCreateBackwardCompat:
    def test_create_entities_duplicate_still_errors(self, _db):
        msg1 = server.create_entities(
            [{"name": "x", "entityType": "t", "observations": ["o"]}]
        )
        assert "Created 1 entities" in msg1
        msg2 = server.create_entities(
            [{"name": "x", "entityType": "t", "observations": ["o2"]}]
        )
        assert "Entity already exists: x" in msg2

    def test_create_relations_duplicate_still_errors(self, _db):
        server.create_entities(
            [{"name": "a", "entityType": "t", "observations": []},
             {"name": "b", "entityType": "t", "observations": []}]
        )
        msg1 = server.create_relations([{"from": "a", "to": "b", "relationType": "r"}])
        assert "Created 1 relations" in msg1
        msg2 = server.create_relations([{"from": "a", "to": "b", "relationType": "r"}])
        assert "Relation already exists: a -> b (r)" in msg2

    def test_new_entity_revision_zero_then_one(self, _db):
        server.create_entities(
            [{"name": "x", "entityType": "t", "observations": []}]
        )
        assert _rev(_db, "x") == 0
        server.add_observations([{"entityName": "x", "contents": ["first"]}])
        assert _rev(_db, "x") == 1


class TestObservationUniqueness:
    """DB-enforced uniqueness on ``observations(entity_id, content)``."""

    def test_fresh_db_unique(self, tmp_path):
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_DDL)
        conn.commit()
        conn.close()
        eid = _insert_entity(db, "e", obs=["x"])

        conn = sqlite3.connect(str(db))
        cur = conn.execute(
            "INSERT OR IGNORE INTO observations (entity_id, content, created_at) "
            "VALUES (?, 'x', ?)",
            (eid, server._now()),
        )
        assert cur.rowcount == 0
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE entity_id = ? AND content = 'x'",
            (eid,),
        ).fetchone()[0]
        assert n == 1
        conn.close()

    def test_migration_dedupes_duplicates(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        # Pre-migration schema: entities + observations with NO unique index.
        conn.execute(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT UNIQUE, entity_type TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "entity_id INTEGER, content TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('e', 't', 'now', 'now')"
        )
        # Two duplicate (entity_id, content) rows (ids 1 and 2) plus one distinct.
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'dup', 'now')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'dup', 'now')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'keep', 'now')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, content FROM observations WHERE entity_id = 1 ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == [1, 3]  # MIN(id) 'dup' + distinct 'keep' survive
        assert [r[1] for r in rows] == ["dup", "keep"]
        index_names = [r[1] for r in conn.execute("PRAGMA index_list(observations)")]
        assert "idx_obs_unique" in index_names
        conn.close()

    def test_migration_idempotent(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT UNIQUE, entity_type TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "entity_id INTEGER, content TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('e', 't', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'dup', 'now')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'dup', 'now')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        ensure_schema(conn)  # second call must not error
        index_names = [r[1] for r in conn.execute("PRAGMA index_list(observations)")]
        assert index_names.count("idx_obs_unique") == 1
        n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert n == 1  # one of the two dups removed; unchanged after 2nd call
        conn.close()

    def test_insert_or_ignore_rowcount_zero(self, tmp_path):
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_DDL)
        conn.commit()
        conn.close()
        eid = _insert_entity(db, "e", obs=["x"])

        conn = sqlite3.connect(str(db))
        cur = conn.execute(
            "INSERT OR IGNORE INTO observations (entity_id, content, created_at) "
            "VALUES (?, 'x', ?)",
            (eid, server._now()),
        )
        assert cur.rowcount == 0
        conn.close()
