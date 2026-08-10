"""Memory MCP Server — persistent knowledge graph with SQLite backend.

Replaces the off-the-shelf mcp-server-memory (npm) with a Python implementation
that adds timestamps, graph traversal, fuzzy search, and temporal queries.

Storage:
  SQLite  at MEMORY_DB_PATH / jing_meta.config.memory_db() — primary store
"""

# Concurrency: under mcp 1.23 (stdio transport), all tool handlers run
# serially on the main event-loop thread.  See jing_meta/mcp_base.py for the
# full model and the implications of upgrading to mcp ≥2.

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.types import TextContent

from jing_meta import config as _jing_config
from jing_meta.mcp_base import JINGMCP
from jing_meta.schema import SCHEMA_DDL

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", str(_jing_config.memory_db())))

mcp = JINGMCP(
    "memory",
    instructions="Persistent knowledge graph with graph traversal, fuzzy search, and temporal queries",
)

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()

# SQL LIKE wildcards that must be escaped in user-supplied tokens
_LIKE_ESCAPE = re.compile(r'([%_\\])')


def _escape_like(token: str) -> str:
    return _LIKE_ESCAPE.sub(r'\\\1', token)


def _get_conn() -> sqlite3.Connection:
    """Get or create the SQLite connection with WAL mode."""
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _conn = sqlite3.connect(str(DB_PATH))
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA foreign_keys=ON")
                # The dreamer, archiver, and stats hooks can open this same
                # memory DB concurrently in separate processes, so a writer
                # must wait for a competing writer's lock rather than failing
                # immediately with "database is locked".
                _conn.execute("PRAGMA busy_timeout=5000")
                _conn.row_factory = sqlite3.Row
                _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist.

    Takes the connection explicitly so it never re-enters ``_get_conn``
    (which would otherwise recurse through the same ``_conn_lock``).
    """
    conn.executescript(SCHEMA_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fuzzy search helpers
# ---------------------------------------------------------------------------

def _trigram_similarity(a: str, b: str) -> float:
    """Simple trigram similarity between two strings (0.0 to 1.0)."""
    a = a.lower()
    b = b.lower()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) < 3 or len(b) < 3:
        return 0.8 if (a in b or b in a) else 0.0

    def trigrams(s: str) -> set[str]:
        return {s[i:i + 3] for i in range(len(s) - 2)}

    ta = trigrams(a)
    tb = trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_entity_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def _get_obs_by_entity_id(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    return [r["content"] for r in rows]


def _truncate_obs(obs: str, max_chars: int) -> str:
    """Truncate *obs* to *max_chars*, appending '…' when shortened."""
    if len(obs) > max_chars:
        return obs[:max_chars] + "…"
    return obs


# ---------------------------------------------------------------------------
# Content-block renderers
#
# Return-contract split across the MCP servers:
#   * memory/server.py READ tools (search_nodes, open_nodes, read_graph,
#     traverse, recent, search_similar, rebuild_semantic_index) return their
#     results as a list of TextContent blocks rather than a single
#     JSON-encoded string. Each observation is emitted as its own block
#     ("Observation: …"), matching the reference mcp-server-memory behaviour,
#     so agents receive discrete observations instead of one opaque string.
#   * memory/server.py WRITE tools (create_entities, create_relations,
#     add_observations, delete_*) return a plain str (a one-line status/confirmation).
#   * search/server.py tools return a plain str (rendered text), NOT TextContent.
# ---------------------------------------------------------------------------

def _entity_blocks(
    name: str,
    entity_type: str,
    observations: list[str],
    created_at: str | None = None,
    updated_at: str | None = None,
    max_obs_chars: int | None = 500,
) -> list[TextContent]:
    blocks = [
        TextContent(type="text", text=f"Name: {name}"),
        TextContent(type="text", text=f"Type: {entity_type}"),
    ]
    for obs in observations:
        text = obs if max_obs_chars is None else _truncate_obs(obs, max_obs_chars)
        blocks.append(TextContent(type="text", text=f"Observation: {text}"))
    if created_at:
        blocks.append(TextContent(type="text", text=f"Created: {created_at}"))
    if updated_at:
        blocks.append(TextContent(type="text", text=f"Updated: {updated_at}"))
    return blocks


def _relation_blocks(relations: list[dict]) -> list[TextContent]:
    blocks: list[TextContent] = []
    for r in relations:
        blocks.append(
            TextContent(
                type="text",
                text=f"Relation: {r['from']} -> {r['to']} ({r['relationType']})",
            )
        )
        if r.get("created_at"):
            blocks.append(
                TextContent(type="text", text=f"Relation created: {r['created_at']}")
            )
    return blocks


def _join_entities(entities: list[dict], max_obs_chars: int | None = 500) -> list[TextContent]:
    """Render a list of entity dicts, inserting a separator between entities.

    Each entity dict may carry optional created_at/updated_at keys (e.g. from
    ``recent``/``open_nodes``); they are rendered when present.
    """
    blocks: list[TextContent] = []
    for i, e in enumerate(entities):
        if i:
            blocks.append(TextContent(type="text", text="---"))
        blocks.extend(
            _entity_blocks(
                e["name"],
                e["entityType"],
                e["observations"],
                created_at=e.get("created_at"),
                updated_at=e.get("updated_at"),
                max_obs_chars=max_obs_chars,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Tools — CRUD (backward compatible)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_entities(entities: list[dict]) -> str:
    """Create multiple new entities in the knowledge graph.

    Each entity must have: name (str), entityType (str), observations (list[str]).
    """
    conn = _get_conn()
    created = 0
    errors: list[str] = []
    now = _now()

    for e in entities:
        name = e.get("name")
        etype = e.get("entityType", "unknown")
        observations = e.get("observations", [])

        if not name or not isinstance(name, str):
            errors.append("Missing or invalid 'name' in entity")
            continue

        try:
            cur = conn.execute(
                "INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, etype, now, now),
            )
            entity_id = cur.lastrowid
            for obs in observations:
                if isinstance(obs, str):
                    conn.execute(
                        "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
                        (entity_id, obs, now),
                    )
            created += 1
        except sqlite3.IntegrityError:
            errors.append(f"Entity already exists: {name}")

    conn.commit()
    msg = f"Created {created} entities."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"
    return msg


@mcp.tool()
def create_relations(relations: list[dict]) -> str:
    """Create multiple new relations between entities in the knowledge graph.

    Each relation must have: from (str), to (str), relationType (str).
    """
    conn = _get_conn()
    created = 0
    errors: list[str] = []
    now = _now()

    for r in relations:
        from_e = r.get("from")
        to_e = r.get("to")
        rtype = r.get("relationType")

        if not from_e or not to_e:
            errors.append("Missing 'from' or 'to' in relation")
            continue
        if not rtype:
            errors.append(f"Missing 'relationType' for {from_e} -> {to_e}")
            continue

        try:
            conn.execute(
                "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (from_e, to_e, rtype, now),
            )
            created += 1
        except sqlite3.IntegrityError:
            errors.append(f"Relation already exists: {from_e} -> {to_e} ({rtype})")

    conn.commit()
    msg = f"Created {created} relations."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"
    return msg


@mcp.tool()
def add_observations(observations: list[dict]) -> str:
    """Add new observations to existing entities in the knowledge graph.

    Each item must have: entityName (str), contents (list[str]).
    """
    conn = _get_conn()
    added = 0
    errors: list[str] = []
    now = _now()

    for item in observations:
        entity_name = item.get("entityName")
        contents = item.get("contents", [])

        if not entity_name:
            errors.append("Missing 'entityName'")
            continue

        entity_id = _get_entity_id(conn, entity_name)
        if entity_id is None:
            errors.append(f"Entity not found: {entity_name}")
            continue

        for content in contents:
            if isinstance(content, str):
                conn.execute(
                    "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
                    (entity_id, content, now),
                )
                added += 1

        conn.execute("UPDATE entities SET updated_at = ? WHERE id = ?", (now, entity_id))

    conn.commit()

    msg = f"Added {added} observations."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"
    return msg


@mcp.tool()
def delete_entities(entityNames: list[str]) -> str:
    """Delete multiple entities and their associated relations from the knowledge graph."""
    conn = _get_conn()
    deleted = 0

    for name in entityNames:
        # Clean up dangling relations first (relations use soft FKs by name)
        conn.execute("DELETE FROM relations WHERE from_entity = ? OR to_entity = ?", (name, name))
        cursor = conn.execute("DELETE FROM entities WHERE name = ?", (name,))
        deleted += cursor.rowcount

    conn.commit()
    return f"Deleted {deleted} entities."


@mcp.tool()
def delete_observations(deletions: list[dict]) -> str:
    """Delete specific observations from entities in the knowledge graph.

    Each item must have: entityName (str), observations (list[str] — exact content match).
    """
    conn = _get_conn()
    deleted = 0
    errors: list[str] = []

    for item in deletions:
        entity_name = item.get("entityName")
        obs_to_delete = item.get("observations", [])

        if not entity_name:
            errors.append("Missing 'entityName'")
            continue

        entity_id = _get_entity_id(conn, entity_name)
        if entity_id is None:
            errors.append(f"Entity not found: {entity_name}")
            continue

        for obs_content in obs_to_delete:
            cursor = conn.execute(
                "DELETE FROM observations WHERE entity_id = ? AND content = ?",
                (entity_id, obs_content),
            )
            deleted += cursor.rowcount

    conn.commit()
    msg = f"Deleted {deleted} observations."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"
    return msg


@mcp.tool()
def delete_relations(relations: list[dict]) -> str:
    """Delete multiple relations from the knowledge graph.

    Each relation must have: from (str), to (str), relationType (str). If relationType
    is omitted, all relations between from and to are deleted.
    """
    conn = _get_conn()
    deleted = 0

    for r in relations:
        from_e = r.get("from")
        to_e = r.get("to")
        rtype = r.get("relationType")

        if rtype:
            cursor = conn.execute(
                "DELETE FROM relations WHERE from_entity = ? AND to_entity = ? AND relation_type = ?",
                (from_e, to_e, rtype),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM relations WHERE from_entity = ? AND to_entity = ?",
                (from_e, to_e),
            )
        deleted += cursor.rowcount

    conn.commit()
    return f"Deleted {deleted} relations."


# ---------------------------------------------------------------------------
# Tools — Query (improved)
# ---------------------------------------------------------------------------

def _score_entity(
    name: str,
    entity_type: str,
    token_hits: dict[str, set[str]],
) -> int:
    """Relevance score for a matched entity from per-token hit locations.

    Name matches are weighted highest (+10 per token), then type (+5 per token),
    then observation content (+1 per token). ``token_hits`` maps each token to
    the set of fields (``"name"``, ``"type"``, ``"obs"``) it matched in.
    """
    score = 0
    for hits in token_hits.values():
        if "name" in hits:
            score += 10
        if "type" in hits:
            score += 5
        if "obs" in hits:
            score += 1
    return score


@mcp.tool()
def search_nodes(
    query: str,
    limit: int = 20,
    max_obs_per_entity: int = 5,
    include_semantic: bool = True,
) -> list[TextContent]:
    """Search for nodes in the knowledge graph.

    Case-insensitive, AND-joined token match across entity names, types, and
    observation content (all tokens must match somewhere). Results are
    relevance-ranked (name > type > observation) and bounded by ``limit``,
    with at most ``max_obs_per_entity`` observations returned per entity.

    When ``include_semantic`` is True (default) and an offline embedding index
    exists (built by ``rebuild_semantic_index``), a clearly-labelled block of
    semantically-similar entities is appended after the lexical hits — these
    are conceptually-related entities that may share no words with the query.

    Args:
        query:              Space-separated search tokens (all must match).
        limit:              Max number of entities to return (default 20).
        max_obs_per_entity: Max observations to attach per entity (default 5).
        include_semantic:   Append semantically-similar entities (default True).
    """
    conn = _get_conn()
    tokens = query.lower().split()
    limit = max(1, limit)
    max_obs_per_entity = max(1, max_obs_per_entity)

    if not tokens:
        return [TextContent(type="text", text="No entities found.")]

    conditions = []
    params: list[str] = []
    for token in tokens:
        escaped = _escape_like(token)
        like = f"%{escaped}%"
        conditions.append(
            "(LOWER(e.name) LIKE ? ESCAPE '\\' OR LOWER(e.entity_type) LIKE ? ESCAPE '\\' "
            "OR e.id IN (SELECT entity_id FROM observations WHERE LOWER(content) LIKE ? ESCAPE '\\'))"
        )
        params.extend([like, like, like])

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT DISTINCT e.id, e.name, e.entity_type FROM entities e WHERE {where} ORDER BY e.name",
        params,
    ).fetchall()

    if not rows:
        return [TextContent(type="text", text="No entities found.")]

    # Fetch observations for ALL matched entities in a single query.
    obs_by_id: dict[int, list[str]] = {r["id"]: [] for r in rows}
    id_placeholders = ",".join("?" * len(rows))
    obs_rows = conn.execute(
        f"SELECT entity_id, content FROM observations "
        f"WHERE entity_id IN ({id_placeholders}) ORDER BY entity_id, id",
        [r["id"] for r in rows],
    ).fetchall()
    for o in obs_rows:
        obs_by_id.setdefault(o["entity_id"], []).append(o["content"])

    # Relevance-score each matched entity, tracking which tokens hit name/type/obs.
    scored_entities = []
    for row in rows:
        name = row["name"]
        etype = row["entity_type"]
        obs_list = obs_by_id.get(row["id"], [])
        name_l = name.lower()
        type_l = etype.lower()
        obs_l = [o.lower() for o in obs_list]
        token_hits: dict[str, set[str]] = {}
        for token in tokens:
            hits: set[str] = set()
            if token in name_l:
                hits.add("name")
            if token in type_l:
                hits.add("type")
            if any(token in o for o in obs_l):
                hits.add("obs")
            token_hits[token] = hits
        score = _score_entity(name, etype, token_hits)
        scored_entities.append((score, name, etype, row["id"], obs_list))

    scored_entities.sort(key=lambda x: (-x[0], x[1]))
    scored_entities = scored_entities[:limit]

    result_entities = []
    result_relations = []
    names_list = []
    for _score, name, etype, _eid, obs_list in scored_entities:
        result_entities.append({
            "name": name,
            "entityType": etype,
            "observations": obs_list[:max_obs_per_entity],
        })
        names_list.append(name)

    # Include relations between matched entities
    if names_list:
        placeholders = ",".join("?" * len(names_list))
        rel_rows = conn.execute(
            f"SELECT from_entity, to_entity, relation_type FROM relations "
            f"WHERE from_entity IN ({placeholders}) OR to_entity IN ({placeholders})",
            names_list + names_list,
        ).fetchall()

        for rel in rel_rows:
            result_relations.append({
                "from": rel["from_entity"],
                "to": rel["to_entity"],
                "relationType": rel["relation_type"],
            })

    blocks: list[TextContent] = _join_entities(result_entities)
    blocks.extend(_relation_blocks(result_relations))

    # Append semantically-similar entities (default-on, clearly labelled) so the
    # agent gets conceptual neighbors the lexical search would miss.
    if include_semantic:
        try:
            from . import semantic_index

            semantic_hits = semantic_index.semantic_search(query, str(DB_PATH), top_n=10)
            if semantic_hits:
                # Avoid duplicating entities already shown lexically.
                shown_names = {e["name"] for e in result_entities}
                new_hits = [h for h in semantic_hits if h["name"] not in shown_names]
                if new_hits:
                    blocks.append(TextContent(type="text", text="\n[Semantically similar to query:]"))
                    blocks.extend(
                        TextContent(type="text", text=f"  {h['name']} (sim={h['similarity']})")
                        for h in new_hits
                    )
        except Exception:
            pass  # semantic layer is best-effort; never break lexical search

    return blocks


@mcp.tool()
def search_semantic(
    query: str,
    top_n: int = 10,
    threshold: float = 0.5,
) -> list[TextContent]:
    """Semantic search over the memory graph (offline, embedding-based).

    Complements the lexical ``search_nodes``. Finds entities that are
    *conceptually* related to the query even when they share no words — e.g.
    query "heap corruption" can surface an entity about "GC nursery bug".
    Fully offline (fastembed + onnxruntime, bge-small); no network.

    Requires the entity vector index to exist (built by ``rebuild_semantic_index``,
    typically run after gardening). Returns semantically-close entity names with
    similarity scores.

    Args:
        query:     Free-text query (embedded, not token-matched).
        top_n:     Max semantic neighbors to return (default 10).
        threshold: Min cosine similarity 0-1 (default 0.5).
    """
    from . import semantic_index

    results = semantic_index.semantic_search(query, str(DB_PATH), top_n=top_n)
    if not results:
        age = semantic_index.index_age(str(DB_PATH))
        if age < 0:
            return [TextContent(type="text", text=(
                "No semantic index found. Run rebuild_semantic_index first "
                "(or after gardening) to enable semantic search."))]
        return [TextContent(type="text", text="No semantically-similar entities above threshold.")]

    blocks = [TextContent(type="text", text=f"Semantically similar to '{query}':")]
    blocks.extend(
        TextContent(type="text", text=f"  {r['name']} (sim={r['similarity']})")
        for r in results
    )
    return blocks


@mcp.tool()
def rebuild_semantic_index() -> list[TextContent]:
    """Build (or rebuild) the offline embedding index for semantic search.

    Embeds every entity's name + observations via a local ONNX model
    (bge-small-en-v1.5) and caches the vectors to disk. Run this after
    gardening or after significant graph changes so ``search_semantic`` stays
    current. Fully offline.

    Returns the index path and entity count.
    """
    from . import semantic_index

    conn = _get_conn()
    vpath, npath = semantic_index.build_index(conn, str(DB_PATH))
    n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    return [TextContent(type="text", text=(
        f"Semantic index rebuilt: {n} entities -> {vpath.name} "
        f"(names: {npath.name})"))]



@mcp.tool()
def open_nodes(names: list[str], max_obs_per_entity: int = 20) -> list[TextContent]:
    """Open specific nodes in the knowledge graph by their names.

    Returns full entity details including observations and timestamps
    as discrete content blocks. Observations are truncated to
    ``max_obs_per_entity`` per entity and each observation is bounded to 2000
    characters, so a single call cannot blow out the context window.

    Args:
        names:              Names of entities to open.
        max_obs_per_entity: Max observations to render per entity (default 20).
    """
    conn = _get_conn()
    blocks: list[TextContent] = []

    for name in names:
        row = conn.execute(
            "SELECT id, name, entity_type, created_at, updated_at FROM entities WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            continue

        blocks.extend(
            _entity_blocks(
                row["name"],
                row["entity_type"],
                _get_obs_by_entity_id(conn, row["id"])[:max_obs_per_entity],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                max_obs_chars=2000,
            )
        )

    if not blocks:
        return [TextContent(type="text", text="No entities found.")]
    return blocks


@mcp.tool()
def read_graph() -> list[TextContent]:
    """Read the entire knowledge graph.

    Returns all entities with observations and all relations as discrete
    content blocks.
    Note: loads the full graph into memory — may be large.
    """
    conn = _get_conn()
    entities = []
    relations = []

    for row in conn.execute("SELECT id, name, entity_type FROM entities ORDER BY name"):
        entities.append({
            "name": row["name"],
            "entityType": row["entity_type"],
            "observations": _get_obs_by_entity_id(conn, row["id"]),
        })

    for row in conn.execute("SELECT from_entity, to_entity, relation_type FROM relations ORDER BY id"):
        relations.append({
            "from": row["from_entity"],
            "to": row["to_entity"],
            "relationType": row["relation_type"],
        })

    blocks: list[TextContent] = _join_entities(entities)
    blocks.extend(_relation_blocks(relations))
    if not blocks:
        return [TextContent(type="text", text="Knowledge graph is empty.")]
    return blocks


# ---------------------------------------------------------------------------
# Tools — New (graph traversal, temporal, fuzzy)
# ---------------------------------------------------------------------------

@mcp.tool()
def traverse(start_node: str, depth: int = 1, max_obs_per_entity: int = 5) -> list[TextContent]:
    """Traverse the graph from a starting node, returning all entities within N hops.

    Args:
        start_node:         Entity name to start from
        depth:              Number of hops to traverse (default 1, max 3)
        max_obs_per_entity: Max observations to render per entity (default 5).
    """
    conn = _get_conn()

    row = conn.execute("SELECT name FROM entities WHERE name = ?", (start_node,)).fetchone()
    if not row:
        return [TextContent(type="text", text=f"Entity not found: {start_node}")]

    depth = max(1, min(depth, 3))
    visited: set[str] = {start_node}
    frontier = {start_node}
    seen_relations: set[tuple[str, str, str]] = set()
    all_relations: list[dict] = []

    for _ in range(depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        placeholders = ",".join("?" * len(frontier))
        frontier_list = list(frontier)

        # Outgoing
        rels = conn.execute(
            f"SELECT from_entity, to_entity, relation_type FROM relations WHERE from_entity IN ({placeholders})",
            frontier_list,
        ).fetchall()
        for r in rels:
            key = (r["from_entity"], r["to_entity"], r["relation_type"])
            if key not in seen_relations:
                seen_relations.add(key)
                all_relations.append({
                    "from": r["from_entity"],
                    "to": r["to_entity"],
                    "relationType": r["relation_type"],
                })
            if r["to_entity"] not in visited:
                visited.add(r["to_entity"])
                next_frontier.add(r["to_entity"])

        # Incoming
        rels = conn.execute(
            f"SELECT from_entity, to_entity, relation_type FROM relations WHERE to_entity IN ({placeholders})",
            frontier_list,
        ).fetchall()
        for r in rels:
            key = (r["from_entity"], r["to_entity"], r["relation_type"])
            if key not in seen_relations:
                seen_relations.add(key)
                all_relations.append({
                    "from": r["from_entity"],
                    "to": r["to_entity"],
                    "relationType": r["relation_type"],
                })
            if r["from_entity"] not in visited:
                visited.add(r["from_entity"])
                next_frontier.add(r["from_entity"])

        frontier = next_frontier

    entities = []
    for name in sorted(visited):
        erow = conn.execute(
            "SELECT id, name, entity_type FROM entities WHERE name = ?", (name,),
        ).fetchone()
        if not erow:
            continue
        entities.append({
            "name": erow["name"],
            "entityType": erow["entity_type"],
            "observations": _get_obs_by_entity_id(conn, erow["id"])[:max_obs_per_entity],
        })

    blocks: list[TextContent] = _join_entities(entities)
    blocks.extend(_relation_blocks(all_relations))
    blocks.append(TextContent(type="text", text=f"Hops: {depth}, Nodes found: {len(entities)}"))
    return blocks


@mcp.tool()
def recent(hours: int = 24) -> list[TextContent]:
    """Return entities, relations, and observations created or updated in the last N hours.

    Time-scoped on observations: only observations whose own ``created_at`` falls
    within the window are returned for a recently-updated entity — not the entity's
    full observation history. This keeps the result proportional to recent activity
    rather than to entity size.

    Args:
        hours: Look-back window in hours (default 24, max 720)
    """
    hours = max(1, min(hours, 720))
    conn = _get_conn()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use SQLite datetime() for format-agnostic comparison
    entities = []
    rows = conn.execute(
        "SELECT id, name, entity_type, created_at, updated_at FROM entities "
        "WHERE datetime(updated_at) >= datetime(?) ORDER BY updated_at DESC",
        (cutoff_iso,),
    ).fetchall()

    # Fetch, in a single query, only the observations created within the window
    # for all recently-updated entities (time-scoped, not full history).
    recent_obs_by_entity: dict[int, list[str]] = {}
    if rows:
        id_placeholders = ",".join("?" * len(rows))
        obs_rows = conn.execute(
            f"SELECT entity_id, content FROM observations "
            f"WHERE entity_id IN ({id_placeholders}) "
            f"AND datetime(created_at) >= datetime(?) ORDER BY entity_id, id",
            [*(r["id"] for r in rows), cutoff_iso],
        ).fetchall()
        for o in obs_rows:
            recent_obs_by_entity.setdefault(o["entity_id"], []).append(o["content"])

    for row in rows:
        entities.append({
            "name": row["name"],
            "entityType": row["entity_type"],
            "observations": recent_obs_by_entity.get(row["id"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

    relations = []
    for row in conn.execute(
        "SELECT from_entity, to_entity, relation_type, created_at FROM relations "
        "WHERE datetime(created_at) >= datetime(?) ORDER BY created_at DESC",
        (cutoff_iso,),
    ):
        relations.append({
            "from": row["from_entity"],
            "to": row["to_entity"],
            "relationType": row["relation_type"],
            "created_at": row["created_at"],
        })

    blocks: list[TextContent] = _join_entities(entities)
    blocks.extend(_relation_blocks(relations))
    blocks.append(TextContent(type="text", text=f"Window: {hours}h since {cutoff_iso}"))
    return blocks


@mcp.tool()
def search_similar(name: str, threshold: float = 0.3) -> list[TextContent]:
    """Fuzzy search for entity names using trigram similarity.

    Args:
        name:      Name to search for (fuzzy matched)
        threshold: Minimum similarity score 0.0–1.0 (default 0.3)
    """
    threshold = max(0.0, min(threshold, 1.0))
    conn = _get_conn()
    rows = conn.execute("SELECT name, entity_type FROM entities").fetchall()

    scored = []
    for row in rows:
        score = _trigram_similarity(name, row["name"])
        if score >= threshold:
            scored.append((row["name"], row["entity_type"], score))

    scored.sort(key=lambda x: -x[2])

    if not scored:
        return [TextContent(type="text", text=f"No similar entities found for '{name}'.")]

    blocks = [
        TextContent(type="text", text=f"Similar to '{name}' (threshold={threshold}):")
    ]
    blocks.extend(
        TextContent(
            type="text",
            text=f"Match: {s[0]} ({s[1]}) score={round(s[2], 3)}",
        )
        for s in scored[:20]
    )
    return blocks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server."""
    mcp.run_stdio(pre_warm=_get_conn)
