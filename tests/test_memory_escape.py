"""Tests for the memory server's LIKE-escape helper (memory/server.py).

``_escape_like`` escapes the SQL LIKE wildcards (percent, underscore, and the
backslash escape character itself) in user-supplied search tokens so that a
token can never widen a query into an unintended wildcard match. These tests
pin the exact escaping behaviour for adversarial input and verify end-to-end
that a literal percent/underscore in a query does not behave as a LIKE wildcard.
"""

import sqlite3

import pytest

from jing_meta.schema import SCHEMA_DDL
from memory import server


def _blocks_text(blocks) -> str:
    return "\n".join(b.text for b in blocks)


class TestEscapeLike:
    """Unit tests for ``_escape_like`` over adversarial tokens."""

    def test_percent_is_escaped(self):
        assert server._escape_like("%") == "\\%"

    def test_underscore_is_escaped(self):
        assert server._escape_like("_") == "\\_"

    def test_backslash_is_escaped(self):
        # Backslash is itself the escape character, so it must be doubled.
        assert server._escape_like("\\") == "\\\\"

    def test_embedded_wildcards_escaped(self):
        assert server._escape_like("100%done") == "100\\%done"
        assert server._escape_like("foo_bar") == "foo\\_bar"
        assert server._escape_like("a\\b") == "a\\\\b"

    def test_nul_byte_passthrough(self):
        assert server._escape_like("he\x00llo") == "he\x00llo"

    def test_emoji_passthrough(self):
        assert server._escape_like("emoji 😀") == "emoji 😀"

    def test_quotes_and_semicolon_passthrough(self):
        assert server._escape_like("don't") == "don't"
        assert server._escape_like("a;b") == "a;b"

    def test_brackets_passthrough(self):
        # SQLite LIKE has no bracket syntax; leave them untouched.
        assert server._escape_like("[abc]") == "[abc]"
        assert server._escape_like("x]y") == "x]y"

    def test_empty_and_plain(self):
        assert server._escape_like("") == ""
        assert server._escape_like("plain") == "plain"


class TestSearchNoWildcardInjection:
    """End-to-end: a literal ``%``/``_`` in a query must not act as a wildcard."""

    @pytest.fixture
    def _db(self, monkeypatch, tmp_path):
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_DDL)
        now = server._now()

        def add(name: str, obs: list[str]) -> None:
            conn.execute(
                "INSERT INTO entities (name, entity_type, created_at, updated_at) "
                "VALUES (?, 't', ?, ?)",
                (name, now, now),
            )
            eid = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,)
            ).fetchone()[0]
            for content in obs:
                conn.execute(
                    "INSERT INTO observations (entity_id, content, created_at) "
                    "VALUES (?, ?, ?)",
                    (eid, content, now),
                )

        # An entity whose *literal* text contains LIKE wildcards.
        add("literal_pct", ["task is 100% done"])
        add("literal_us", ["foo_bar syntax"])
        # A neighbouring entity that a wildcard query would wrongly match.
        add("neighbour", ["task is 100 complete"])
        add("foobar", ["plain foobar"])
        conn.commit()
        conn.close()

        monkeypatch.setattr(server, "DB_PATH", db)
        server._conn = None  # force a fresh connection to the temp DB
        return db

    def test_percent_token_matches_only_literal(self, _db):
        blocks = server.search_nodes("100%", include_semantic=False)
        out = _blocks_text(blocks)
        assert "literal_pct" in out
        assert "neighbour" not in out  # 100% must NOT behave as 100 + any suffix

    def test_underscore_token_matches_only_literal(self, _db):
        blocks = server.search_nodes("foo_bar", include_semantic=False)
        out = _blocks_text(blocks)
        assert "literal_us" in out
        assert "foobar" not in out  # foo_bar must NOT match foobar
