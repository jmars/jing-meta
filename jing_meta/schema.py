"""Shared knowledge-graph SQLite schema.

Single source of truth for table/column definitions used by both
``memory/server.py`` (schema initialization) and ``dreamer/dreamer.py``
(SQLite read/write).  Any column addition, rename, or type change MUST
be made here first, then reflected in both consumers.
"""

SCHEMA_DDL = """\
    CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
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
    CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_entity);
    CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_entity);
    CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
"""
