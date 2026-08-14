"""Memory Archiver — move old observations out of the live memory graph.

A first-class jing-meta component: keeps the live memory graph lean by archiving
observations older than ``days`` (default 90) into a JSONL directory that the
DAFSA indexer indexes as a searchable archive. The live graph keeps recent,
emergent context; the archive keeps everything older, full-text searchable on
demand.

Design / safety guarantees (non-negotiable, ported from the standalone
``vibe-memory-archiver.py`` and preserved):
- **Archive first, delete second.** Observations are written to the archive
  file (atomically), flushed, and ONLY THEN removed from the graph. If the
  archive write fails, nothing is deleted. No data loss.
- **Observations only.** Never touches entities or relations — archiving an
  observation keeps its entity (with newer observations) and all its relations
  intact. This preserves the graph's connectivity.
- **Idempotent / dry-run by default.** Default is a dry-run that prints what
  would be archived. Pass ``--apply`` to actually move data.
- **Backup before apply.** A timestamped backup of the memory DB is created
  before any deletion.
- **Reversible in principle.** Every archived line carries the full original
  observation text + created_at + entity, so it can be restored if ever needed.

The archive JSONL format is ``{"entity":..., "content":..., "created_at":...}``
per line — compatible with ``indexer.EXTRACTORS["jsonl"]`` (reads "content").
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jing_meta import config as _config
from jing_meta.fsutil import _atomic_write
from jing_meta.log import get_logger, setup_logging
from jing_meta.schema import ensure_schema

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_micro() -> str:
    """Timestamp with microsecond precision (collision-resistant for backups)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")


def _parse_age(days: int) -> str:
    """Return an ISO-8601 timestamp `days` in the past (UTC)."""
    ts = datetime.now(timezone.utc) - timedelta(days=days)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_old_observations(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    """Return old observations as list of {id, entity, content, created_at}."""
    rows = conn.execute(
        """
        SELECT o.id AS id, o.entity_id AS entity_id, e.name AS entity,
               o.content AS content, o.created_at AS created_at
        FROM observations o
        JOIN entities e ON e.id = o.entity_id
        WHERE datetime(o.created_at) < datetime(?)
        ORDER BY o.created_at ASC
        """,
        (cutoff,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "entity_id": r["entity_id"],
            "entity": r["entity"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _write_archive(archive_path: Path, obs: list[dict]) -> None:
    """Write observations to a JSONL file atomically (tmp + replace + fsync)."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        json.dumps(
            {"entity": o["entity"], "content": o["content"],
             "created_at": o["created_at"]},
            ensure_ascii=False,
        )
        for o in obs
    ).encode("utf-8")
    if payload:
        payload += b"\n"
    _atomic_write(archive_path, payload)


def _report(msg: str, quiet: bool = False) -> None:
    """Emit a progress message.

    ``archive()`` is shared by the CLI (which wants stdout for human display)
    and the Vibe post_agent hook (which must keep stdout empty/JSON so the hook
    protocol doesn't reject it as an "invalid response"). When ``quiet`` is set
    we route to the stderr logger instead of stdout.
    """
    if quiet:
        logger.info("%s", msg)
    else:
        print(msg)


def _delete_observations(conn: sqlite3.Connection, obs: list[dict]) -> int:
    """Delete the given observations by primary key (id).

    Deleting by ``id`` (rather than an (entity, content) match) guarantees we
    only ever remove exactly the rows that were selected and archived. The
    ``idx_obs_unique`` UNIQUE(entity_id, content) constraint means duplicate
    content can no longer exist, so delete-by-id is belt-and-suspenders — but it
    remains the safest form, since it can never remove a *different* row that
    happens to share content.

    After deleting, bumps the ``revision`` of each distinct affected entity so a
    concurrent writer holding a stale snapshot of that entity sees a conflict
    instead of silently clobbering the post-archive state.
    """
    cur = conn.executemany("DELETE FROM observations WHERE id = ?", [(o["id"],) for o in obs])
    for entity_id in {o["entity_id"] for o in obs}:
        conn.execute(
            "UPDATE entities SET revision = revision + 1 WHERE id = ?",
            (entity_id,),
        )
    return cur.rowcount


def _backup_db(db: Path) -> Path:
    """Create a timestamped backup of the memory DB. Returns the backup path."""
    backup = Path(str(db) + f".bak.prearchive.{_now_micro()}")
    shutil.copyfile(db, backup)
    return backup


def _rebuild_index(archive_dir: Path) -> bool:
    """Build a DAFSA index over the archive dir (best effort, in-process).

    Returns True if the index was built successfully, False otherwise.
    """
    from indexer import build as indexer_build

    try:
        indexer_build(
            dir=archive_dir,
            pattern="*.jsonl",
            extractor="jsonl",
            output=archive_dir / "index",
        )
        return True
    except Exception as e:  # noqa: BLE001 - best effort, never fatal
        logger.warning("index rebuild failed: %s", e)
        return False


def archive(
    memory_db: Path | None = None,
    archive_dir: Path | None = None,
    days: int = 90,
    apply: bool = False,
    rebuild_index: bool = True,
    quiet: bool = False,
) -> dict:
    """Archive observations older than `days` from the memory graph.

    Returns a summary dict with keys: archived, total_before, archive_path,
    backup_path, deleted, index_rebuilt.
    """
    memory_db = Path(os.path.expanduser(str(memory_db or _config.memory_db())))
    archive_dir = Path(os.path.expanduser(str(archive_dir or _config.archive_dir())))

    if days < 0:
        raise ValueError("--days must be >= 0")
    if not memory_db.is_file():
        raise FileNotFoundError(
            f"memory store not found or not a regular file: {memory_db}"
        )

    cutoff = _parse_age(days)

    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # The memory server / dreamer can write to this same DB in a separate
    # process; wait for a competing writer instead of failing with
    # "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        old = _select_old_observations(conn, cutoff)
    finally:
        conn.close()

    count_conn = sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)
    # Read-only, but still wait briefly in case a writer holds a lock.
    count_conn.execute("PRAGMA busy_timeout=5000")
    total_obs = count_conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    count_conn.close()

    _report(f"Observations older than {days}d (before {cutoff}): {len(old)} of {total_obs}", quiet)
    if not old:
        _report("Nothing to archive.", quiet)
        return {"archived": 0, "total_before": total_obs, "archive_path": "", "backup_path": "", "deleted": 0, "index_rebuilt": False}

    if not apply:
        _report("Dry run — no changes. Use --apply to archive.", quiet)
        for o in old[:10]:
            _report(f"  [{o['entity']}] {o['content'][:80]}", quiet)
        if len(old) > 10:
            _report(f"  ... and {len(old)-10} more", quiet)
        return {"archived": len(old), "total_before": total_obs, "archive_path": "", "backup_path": "", "deleted": 0, "index_rebuilt": False}

    # ---- Apply path ----
    archive_path = archive_dir / f"archive-{_now().replace(':','')}.jsonl"

    # 1. Write archive first (must succeed before any deletion)
    _write_archive(archive_path, old)
    _report(f"Archived {len(old)} observations to {archive_path}", quiet)

    # 2. Backup the DB before deleting
    backup = _backup_db(memory_db)
    _report(f"Backup: {backup}", quiet)

    # 3. Delete from graph (single transaction — all-or-nothing)
    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # The memory server / dreamer can write to this same DB in a separate
    # process; wait for a competing writer instead of failing with
    # "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    # Ensure the DB is on the current schema (adds `revision` on older DBs)
    # before we bump revisions on the affected entities.
    ensure_schema(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = _delete_observations(conn, old)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _report(f"Removed {deleted} observations from the live graph.", quiet)

    # 4. Rebuild search index (best effort)
    index_rebuilt = False
    if rebuild_index:
        index_rebuilt = _rebuild_index(archive_dir)

    return {
        "archived": len(old),
        "total_before": total_obs,
        "archive_path": str(archive_path),
        "backup_path": str(backup),
        "deleted": deleted,
        "index_rebuilt": index_rebuilt,
    }


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jing-archiver",
        description="jing-meta memory archiver (archive old observations out of the live graph)",
    )
    parser.add_argument(
        "--memory-db",
        default=str(_config.memory_db()),
        type=Path,
        help=f"Path to the jing-meta memory store (default: {_config.memory_db()})",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(_config.archive_dir()),
        type=Path,
        help=f"Directory to write JSONL archives (default: {_config.archive_dir()})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Archive observations older than N days (default 90)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move observations (default: dry-run)",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip the DAFSA index rebuild after archiving",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = _cli()
    args = parser.parse_args(argv)

    try:
        archive(
            memory_db=args.memory_db,
            archive_dir=args.archive_dir,
            days=args.days,
            apply=args.apply,
            rebuild_index=not args.no_index,
        )
    except (ValueError, FileNotFoundError) as e:
        logger.error("%s", e)
        return 1
    except OSError as e:
        logger.error("filesystem error: %s", e)
        return 1
    except sqlite3.Error as e:
        logger.error("database error: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
