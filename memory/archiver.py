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
        SELECT o.id AS id, e.name AS entity, o.content AS content,
               o.created_at AS created_at
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


def _delete_observations(conn: sqlite3.Connection, obs: list[dict]) -> int:
    """Delete the given observations by primary key (id).

    Deleting by ``id`` (rather than an (entity, content) match) guarantees we
    only ever remove exactly the rows that were selected and archived — a newer
    duplicate observation with identical content is never wrongly deleted.
    """
    cur = conn.executemany("DELETE FROM observations WHERE id = ?", [(o["id"],) for o in obs])
    return cur.rowcount


def _backup_db(db: Path) -> Path:
    """Create a timestamped backup of the memory DB. Returns the backup path."""
    backup = Path(str(db) + f".bak.prearchive.{_now_micro()}")
    shutil.copy2(db, backup)
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
    try:
        old = _select_old_observations(conn, cutoff)
    finally:
        conn.close()

    total_obs = (
        sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)
        .execute("SELECT COUNT(*) FROM observations")
        .fetchone()[0]
    )

    print(f"Observations older than {days}d (before {cutoff}): {len(old)} of {total_obs}")
    if not old:
        print("Nothing to archive.")
        return {"archived": 0, "total_before": total_obs, "archive_path": "", "backup_path": "", "deleted": 0, "index_rebuilt": False}

    if not apply:
        print("Dry run — no changes. Use --apply to archive.")
        for o in old[:10]:
            print(f"  [{o['entity']}] {o['content'][:80]}")
        if len(old) > 10:
            print(f"  ... and {len(old)-10} more")
        return {"archived": len(old), "total_before": total_obs, "archive_path": "", "backup_path": "", "deleted": 0, "index_rebuilt": False}

    # ---- Apply path ----
    archive_path = archive_dir / f"archive-{_now().replace(':','')}.jsonl"

    # 1. Write archive first (must succeed before any deletion)
    _write_archive(archive_path, old)
    print(f"Archived {len(old)} observations to {archive_path}")

    # 2. Backup the DB before deleting
    backup = _backup_db(memory_db)
    print(f"Backup: {backup}")

    # 3. Delete from graph (single transaction — all-or-nothing)
    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = _delete_observations(conn, old)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"Removed {deleted} observations from the live graph.")

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
        result = archive(
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
