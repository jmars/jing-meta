"""Tests for output-size bounding in the memory server (memory/server.py).

Verifies that the read tools bound the amount of observation text returned so
a single call cannot blow out the LLM context window: per-entity observation
COUNT caps (``max_obs_per_entity``) and per-observation CHARACTER caps
(``max_obs_chars``, default 500, higher for open_nodes).
"""

import sqlite3

import pytest

from jing_meta.schema import SCHEMA_DDL
from memory import server


def _blocks_text(blocks) -> str:
    return "\n".join(b.text for b in blocks)


def _add_entity(conn, name: str, obs: list[str]) -> None:
    now = server._now()
    conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at) "
        "VALUES (?, 't', ?, ?)",
        (name, now, now),
    )
    eid = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()[0]
    for content in obs:
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (?, ?, ?)",
            (eid, content, now),
        )


@pytest.fixture
def _db(monkeypatch, tmp_path):
    """Point the memory server at a fresh synthetic graph in a tmp dir."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_DDL)
    _add_entity(conn, "x", ["obs " + str(i) for i in range(30)])
    _add_entity(conn, "long", ["A" * 3000])
    _add_entity(conn, "short", ["tiny observation"])
    _add_entity(conn, "a", ["a obs " + str(i) for i in range(20)])
    _add_entity(conn, "b", ["b obs " + str(i) for i in range(20)])
    conn.execute(
        "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) "
        "VALUES ('a', 'b', 'relates_to', ?)",
        (server._now(),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "DB_PATH", db)
    server._conn = None  # force a fresh connection to the temp DB
    return db


class TestBudget:
    def test_open_nodes_max_obs_per_entity(self, _db):
        blocks = server.open_nodes(["x"], max_obs_per_entity=5)
        out = _blocks_text(blocks)
        assert out.count("obs ") == 5
        # Default per-observation cap still applies (500) — no truncation here.
        assert "…" not in out

    def _long_obs(self, blocks) -> str:
        """Return the rendered observation content for the 'long' entity."""
        texts = [b.text for b in blocks]
        idx = texts.index("Name: long")
        obs_line = next(t for t in texts[idx + 1:] if t.startswith("Observation: "))
        return obs_line.split("Observation: ", 1)[1]

    def test_obs_truncation(self, _db):
        blocks = server.search_nodes("A", include_semantic=False)
        assert "long" in _blocks_text(blocks)
        rendered = self._long_obs(blocks)
        assert len(rendered) <= 501  # 500 chars + the ellipsis
        assert rendered.endswith("…")

    def test_open_nodes_higher_char_limit(self, _db):
        blocks = server.open_nodes(["long"])
        rendered = self._long_obs(blocks)
        # open_nodes bounds at 2000 chars, not the default 500.
        assert len(rendered) == 2001  # 2000 chars + the ellipsis
        assert rendered.endswith("…")

    def test_traverse_max_obs_per_entity(self, _db):
        blocks = server.traverse("a", depth=1, max_obs_per_entity=3)
        out = _blocks_text(blocks)
        assert out.count("a obs ") == 3
        assert out.count("b obs ") == 3

    def test_short_obs_not_truncated(self, _db):
        blocks = server.open_nodes(["short"])
        obs_line = next(b.text for b in blocks if b.text.startswith("Observation: "))
        assert obs_line == "Observation: tiny observation"
        assert "…" not in obs_line

    def test_open_nodes_max_obs_chars_none_full_content(self, _db):
        # Passing max_obs_chars=None returns the observation untruncated.
        blocks = server.open_nodes(["long"], max_obs_chars=None)
        rendered = self._long_obs(blocks)
        assert rendered == "A" * 3000
        assert "…" not in rendered

    def test_truncated_obs_surfaces_length(self, _db):
        # Truncated observations are preceded by a metadata block with the true
        # length and a hint to fetch the full text.
        blocks = server.open_nodes(["long"])
        texts = [b.text for b in blocks]
        type_idx = texts.index("Type: t")
        meta = texts[type_idx + 1]
        assert meta.startswith("[Observation: 3000 chars total, truncated to 2000")
        assert "max_obs_chars=None" in meta
        assert "…" in texts[type_idx + 2]

    def test_short_obs_no_length_meta(self, _db):
        # Observations under the limit are not prefixed by a length metadata block.
        blocks = server.open_nodes(["short"])
        texts = [b.text for b in blocks]
        type_idx = texts.index("Type: t")
        assert texts[type_idx + 1] == "Observation: tiny observation"
        assert not texts[type_idx + 1].startswith("[Observation: ")

    def test_search_nodes_default_cap_unchanged(self, _db):
        # search_nodes still uses the default per-observation char cap (500).
        blocks = server.search_nodes("A", include_semantic=False)
        assert self._long_obs(blocks).endswith("…")
