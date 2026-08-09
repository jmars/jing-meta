"""Tests for the jing-meta memory archiver (memory/archiver.py).

Covers the safety-critical guarantees: archive-first-delete-second, dry-run
default, backup before apply, observations-only, and the day-cutoff logic.
``indexer.build`` is monkeypatched so tests don't require the C DAFSA lib.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jing_meta.schema import SCHEMA_DDL
from memory import archiver


def _make_test_db(path: Path, n_old: int = 5, n_new: int = 3) -> None:
    """Create a memory.db with old (100d) and new (1d) observations."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_DDL)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(n_old):
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES (?, 'old_entity', ?, ?)",
            (f"old-entity-{i}", old, old),
        )
        eid = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (f"old-entity-{i}",)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (eid, f"old observation {i}", old),
        )
    for i in range(n_new):
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES (?, 'new_entity', ?, ?)",
            (f"new-entity-{i}", new, new),
        )
        eid = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (f"new-entity-{i}",)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (eid, f"new observation {i}", new),
        )
    conn.commit()
    conn.close()


def _count_observations(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        conn.close()


class TestArchive:
    @pytest.fixture(autouse=True)
    def _no_index(self, monkeypatch):
        # Avoid the C DAFSA dependency; index rebuild is best-effort anyway.
        monkeypatch.setattr(archiver, "_rebuild_index", lambda d: True)

    def test_dry_run_no_changes(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=False)
        assert result["archived"] == 5
        assert result["deleted"] == 0
        assert not list(arc.glob("*.jsonl"))
        assert _count_observations(db) == 8

    def test_apply_creates_archive(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        files = list(arc.glob("archive-*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        assert len(lines) == 5
        assert all("content" in o and "entity" in o and "created_at" in o for o in lines)

    def test_apply_deletes_observations(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert result["deleted"] == 5
        assert _count_observations(db) == 3  # only new remain

    def test_backup_created_on_apply(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert result["backup_path"]
        assert Path(result["backup_path"]).exists()

    def test_observations_only(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db, n_old=2, n_new=1)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) "
            "VALUES ('old-entity-0', 'new-entity-0', 'related_to', 'x')"
        )
        conn.commit()
        conn.close()
        archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            n_rels = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        finally:
            conn.close()
        assert n_entities == 3  # untouched
        assert n_rels == 1  # untouched

    def test_cutoff_respects_days(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db, n_old=5, n_new=3)
        archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert _count_observations(db) == 3

    def test_empty_graph(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_DDL)
        conn.commit()
        conn.close()
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert result["archived"] == 0

    def test_no_old_observations(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db, n_old=0, n_new=3)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert result["archived"] == 0

    def test_days_zero_archives_all(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db, n_old=2, n_new=2)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=0, apply=True)
        assert result["archived"] == 4
        assert _count_observations(db) == 0

    def test_duplicate_content_only_old_deleted(self, tmp_path):
        """A newer duplicate observation with identical content must survive."""
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db, n_old=0, n_new=0)
        conn = sqlite3.connect(str(db))
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('dup-entity', 't', ?, ?)", (old, old))
        eid = conn.execute("SELECT id FROM entities WHERE name='dup-entity'").fetchone()[0]
        # Same content, two different timestamps -> two rows, different ids.
        conn.execute("INSERT INTO observations (entity_id, content, created_at) VALUES (?,?,?)",
                     (eid, "shared content", old))
        conn.execute("INSERT INTO observations (entity_id, content, created_at) VALUES (?,?,?)",
                     (eid, "shared content", new))
        conn.commit()
        conn.close()
        archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            remaining = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            kept_created = conn.execute(
                "SELECT created_at FROM observations WHERE content='shared content'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining == 1  # only the old duplicate was archived+deleted
        assert kept_created == new

    def test_archive_write_failure_deletes_nothing(self, tmp_path, monkeypatch):
        """If the archive write fails, no observations are deleted."""
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        monkeypatch.setattr(archiver, "_write_archive",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert _count_observations(db) == 8  # untouched
        assert not list(arc.glob("*.jsonl"))  # no archive file

    def test_index_rebuilt(self, tmp_path, monkeypatch):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        called = {}
        monkeypatch.setattr(archiver, "_rebuild_index",
                            lambda d: called.update(dir=d) or True)
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90, apply=True)
        assert result["index_rebuilt"] is True
        assert "dir" in called

    def test_no_index_flag_skips_build(self, tmp_path, monkeypatch):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        monkeypatch.setattr(archiver, "_rebuild_index",
                            lambda d: (_ for _ in ()).throw(AssertionError("should not call")))
        result = archiver.archive(memory_db=db, archive_dir=arc, days=90,
                                  apply=True, rebuild_index=False)
        assert result["index_rebuilt"] is False

    def test_days_negative_errors(self, tmp_path):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        with pytest.raises(ValueError):
            archiver.archive(memory_db=db, archive_dir=arc, days=-1)

    def test_missing_db_errors(self, tmp_path):
        arc = tmp_path / "archives"
        with pytest.raises(FileNotFoundError):
            archiver.archive(memory_db=tmp_path / "nope.db", archive_dir=arc, days=90)


class TestCLI:
    def test_cli_dry_run(self, tmp_path, capsys):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        rc = archiver.main(["--memory-db", str(db), "--archive-dir", str(arc),
                            "--days", "90"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Dry run" in out

    def test_cli_apply(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "memory.db"
        arc = tmp_path / "archives"
        _make_test_db(db)
        monkeypatch.setattr(archiver, "_rebuild_index", lambda d: True)
        rc = archiver.main(["--memory-db", str(db), "--archive-dir", str(arc),
                            "--days", "90", "--apply"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Archived 5 observations" in out
