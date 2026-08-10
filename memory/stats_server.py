"""Memory Stats MCP Server — read-only stats and discovery for the knowledge graph.

Read-only companion to ``memory/server.py``: exposes high-level overview and
per-entity discovery tools over the same SQLite memory graph. Unlike the
standalone ``memory-stats-mcp`` this port is **SQLite-only** — jing-meta has no
JSONL memory store, so a missing/unreadable DB returns a clear error string
rather than a fabricated empty result.

Tools:
  graph_stats    — high-level overview with temporal data
  entity_summary — full details for a specific entity

Concurrency
-----------
Runs over stdio on a single event-loop thread (see ``jing_meta.mcp_base`` for
the full model). Every tool call opens a **fresh read-only SQLite connection**
(``file:...?mode=ro``) and closes it before returning, so no shared connection
state is ever held and there is nothing to lock. This mirrors the stateless,
single-threaded expectations of the memory server.

Reads the same database as ``memory/server.py`` (``MEMORY_DB_PATH`` /
``jing_meta.config.memory_db()``).
"""

import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from mcp.types import TextContent

from jing_meta import config as _jing_config
from jing_meta.mcp_base import JINGMCP, text_block, text_blocks

logger = logging.getLogger("memory-stats")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", str(_jing_config.memory_db())))

mcp = JINGMCP(
    "memory-stats",
    instructions="Read-only stats and discovery for the memory knowledge graph",
)


# ---------------------------------------------------------------------------
# Data access — read-only SQLite, fresh connection per call
# ---------------------------------------------------------------------------

def _has_sqlite() -> bool:
    return DB_PATH.is_file()


def _open_sqlite_ro() -> sqlite3.Connection | None:
    """Open a read-only SQLite connection. Returns None on failure."""
    try:
        uri = "file:" + quote(str(DB_PATH)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.warning("Cannot open SQLite read-only: %s", e)
        return None


def _query_sqlite(query: str, params: tuple = ()) -> list:
    """Execute a SQLite query on a fresh read-only connection.

    Returns the result rows. On open failure (missing/unreadable DB) or a
    transient query error it returns ``[]``; callers distinguish a missing DB
    from an empty graph via ``_has_sqlite()`` / ``_open_sqlite_ro()``.
    """
    conn = _open_sqlite_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return rows
    except sqlite3.OperationalError as e:
        logger.warning("SQLite query failed (transient): %s", e)
        try:
            conn.close()
        except Exception:
            pass
        return []
    except Exception as e:  # noqa: BLE001 — report and degrade, never crash
        logger.error("SQLite query error: %s", e)
        try:
            conn.close()
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def graph_stats() -> list[TextContent]:
    """Show a high-level overview of the knowledge graph.

    Returns entity type counts, relation type counts, recently added entities,
    total observation count, and temporal data (oldest/newest entities, 24h
    activity). Use this before searching to understand what domains and topics
    exist in the graph — no keyword guessing needed.
    """
    if not _has_sqlite():
        return [text_block(f"Knowledge graph database not found at {DB_PATH}")]

    def q(sql: str, params: tuple = ()) -> list:
        return _query_sqlite(sql, params)

    rows = q("SELECT COUNT(*) as n FROM entities")
    entity_count = rows[0]["n"] if rows else 0

    rows = q("SELECT COUNT(*) as n FROM relations")
    rel_count = rows[0]["n"] if rows else 0

    rows = q("SELECT COUNT(*) as n FROM observations")
    obs_count = rows[0]["n"] if rows else 0

    type_rows = q("SELECT entity_type, COUNT(*) as n FROM entities GROUP BY entity_type ORDER BY n DESC")
    type_counts = [(r["entity_type"], r["n"]) for r in type_rows]

    rel_type_rows = q("SELECT relation_type, COUNT(*) as n FROM relations GROUP BY relation_type ORDER BY n DESC")
    rel_counts = [(r["relation_type"], r["n"]) for r in rel_type_rows]

    recent_rows = q("SELECT name, entity_type, updated_at FROM entities ORDER BY updated_at DESC LIMIT 10")

    oldest = q("SELECT name, entity_type, created_at FROM entities ORDER BY created_at ASC LIMIT 1")
    newest = q("SELECT name, entity_type, created_at FROM entities ORDER BY created_at DESC LIMIT 1")
    recent_24h = q("SELECT COUNT(*) as n FROM entities WHERE datetime(updated_at) >= datetime('now', '-24 hours')")

    output = [
        "Knowledge Graph Overview:",
        f"  Entities: {entity_count}, Relations: {rel_count}, Observations: {obs_count}",
        "",
        f"  Entity Types ({len(type_counts)}):",
    ]
    for et, count in type_counts:
        output.append(f"    {et}: {count}")

    if rel_counts:
        output.append("")
        output.append(f"  Relation Types ({len(rel_counts)}):")
        for rt, count in rel_counts:
            output.append(f"    {rt}: {count}")

    if recent_rows:
        output.append("")
        output.append("  Recent Entities:")
        for r in recent_rows[:10]:
            ts = r["updated_at"] or "?"
            output.append(f"    [{r['entity_type']}] {r['name']} ({ts})")

    if oldest and newest and recent_24h:
        output.append("")
        output.append("  Temporal:")
        output.append(f"    Oldest: {oldest[0]['name']} ({oldest[0]['created_at']})")
        output.append(f"    Newest: {newest[0]['name']} ({newest[0]['created_at']})")
        output.append(f"    24h activity: {recent_24h[0]['n']} entities updated")

    return text_blocks(*output)


@mcp.tool()
def entity_summary(name: str) -> list[TextContent]:
    """Get full details for a specific entity by name.

    Returns entity type, all observations with timestamps, related entities
    (inbound and outbound relations), and creation/update timestamps.
    """
    if not _has_sqlite():
        return [text_block(f"Knowledge graph database not found at {DB_PATH}")]

    rows = _query_sqlite(
        "SELECT id, name, entity_type, created_at, updated_at FROM entities WHERE name = ?",
        (name,),
    )
    if not rows:
        return [text_block(f"Entity not found: {name}")]
    return _entity_summary_sqlite(rows[0])


def _entity_summary_sqlite(e) -> list[TextContent]:
    entity_id = e["id"]
    obs_rows = _query_sqlite(
        "SELECT content, created_at FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    )
    out_rel = _query_sqlite(
        "SELECT to_entity, relation_type FROM relations WHERE from_entity = ?",
        (e["name"],),
    )
    in_rel = _query_sqlite(
        "SELECT from_entity, relation_type FROM relations WHERE to_entity = ?",
        (e["name"],),
    )

    output = [
        f"Entity: {e['name']}",
        f"  Type: {e['entity_type']}",
        f"  Created: {e['created_at']}",
        f"  Updated: {e['updated_at']}",
        "",
        f"  Observations ({len(obs_rows)}):",
    ]
    for i, o in enumerate(obs_rows, 1):
        ts = o["created_at"] or "?"
        content = o["content"]
        if len(content) > 120:
            content = content[:117] + "..."
        output.append(f"    {i}. [{ts}] {content}")

    if out_rel:
        output.append("")
        output.append(f"  Outgoing relations ({len(out_rel)}):")
        for r in out_rel:
            output.append(f"    → {r['to_entity']} ({r['relation_type']})")

    if in_rel:
        output.append("")
        output.append(f"  Incoming relations ({len(in_rel)}):")
        for r in in_rel:
            output.append(f"    {r['from_entity']} → ({r['relation_type']})")

    return text_blocks(*output)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run_stdio()
