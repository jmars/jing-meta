"""Shared knowledge-graph SQLite schema.

Single source of truth for table/column definitions used by both
``memory/server.py`` (schema initialization) and ``dreamer/dreamer.py``
(SQLite read/write).  Any column addition, rename, or type change MUST
be made here first, then reflected in both consumers.

Stale-observation archiving — canonical strategy (decision record)
=================================================================
Archiving stale observations is the ARCHIVER's job: ``memory/archiver.py``
DELETES observations older than the cutoff and writes them to a JSONL
archive (searchable via the DAFSA indexer).  The dreamer must NOT also tag
observations with an ``[archived: ...]`` prefix — that would leave the stale
text in the live graph instead of removing it.  This constant documents the
forbidden prefix; do not use it to mark observations in the live graph.
"""

# Prefix the dreamer is forbidden to apply when it "archives" observations.
# Real archiving = delete from the live graph + write to JSONL (archiver.py).
ARCHIVED_OBSERVATION_PREFIX = "[archived: "

SCHEMA_DDL = """\
    CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_entity TEXT NOT NULL,
        to_entity TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(from_entity, to_entity, relation_type)
    );
    CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_unique ON observations(entity_id, content);
    CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_entity);
    CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_entity);
    CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
"""


def ensure_schema(conn) -> None:
    """Create tables if missing and migrate pre-revision / pre-unique databases in place.

    ``CREATE TABLE IF NOT EXISTS`` will NOT alter an existing table, so two
    migrations run explicitly on pre-existing DBs:
      1. observation dedup — an older DB may hold duplicate (entity_id, content)
         rows from before the unique constraint existed.  Those would make the
         unique index below fail, so delete the later duplicates first, keeping
         the earliest (lowest id) row per group.
      2. ``revision`` column backfill (unchanged).

    Idempotent: the dedup no-ops once clean, ``CREATE UNIQUE INDEX IF NOT
    EXISTS`` no-ops once present, and the revision ALTER no-ops once the column
    exists.  Safe to call from every writer (memory/server.py, dreamer/dreamer.py,
    memory/archiver.py) that may open a DB written by an older build.
    """
    # Dedup BEFORE the unique index is created (executescript below).  Only
    # meaningful if the observations table already exists (pre-existing DB).
    has_obs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
    ).fetchone() is not None
    if has_obs:
        conn.execute(
            "DELETE FROM observations WHERE id NOT IN "
            "(SELECT MIN(id) FROM observations GROUP BY entity_id, content)"
        )
        conn.commit()

    conn.executescript(SCHEMA_DDL)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
    if "revision" not in cols:
        conn.execute(
            "ALTER TABLE entities ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
