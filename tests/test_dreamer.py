import sqlite3
from pathlib import Path

from dreamer.dreamer import load_graph_sqlite, save_graph_sqlite
from jing_meta.text import STOPWORDS


# ---------------------------------------------------------------------------
# Item #4: shared stopwords
# ---------------------------------------------------------------------------

def test_stopwords_is_frozenset():
    assert isinstance(STOPWORDS, frozenset)

def test_stopwords_contains_key_words():
    assert "the" in STOPWORDS and "been" in STOPWORDS and "her" in STOPWORDS

def test_stopwords_excludes_domain_terms():
    assert "knowledge" not in STOPWORDS and "code" not in STOPWORDS


class TestSqliteRoundTrip:
    """Item #8: load→save→load preserves everything (regression for N+1/O(NxM) fix)."""

    def _make_db(self, path: Path) -> dict:
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, entity_type TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER REFERENCES entities(id),
                content TEXT, created_at TEXT
            );
            CREATE TABLE relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity TEXT, to_entity TEXT,
                relation_type TEXT, created_at TEXT
            );
        """)
        conn.execute("INSERT INTO entities VALUES (1,'Alpha','plan','2025-01-01','2025-01-02')")
        conn.execute("INSERT INTO entities VALUES (2,'Beta','implementation','2025-01-01','2025-01-03')")
        conn.execute("INSERT INTO entities VALUES (3,'Gamma','plan','2025-01-04','2025-01-04')")
        conn.execute("INSERT INTO observations VALUES (1,1,'Alpha obs 1','2025-01-01')")
        conn.execute("INSERT INTO observations VALUES (2,1,'Alpha obs 2','2025-01-02')")
        conn.execute("INSERT INTO observations VALUES (3,2,'Beta obs','2025-01-01')")
        conn.execute("INSERT INTO relations VALUES (1,'Alpha','Beta','implements','2025-01-05')")
        conn.commit(); conn.close()
        return {
            "entities": [
                {"name": "Gamma", "entityType": "plan", "observations": []},
                {"name": "Beta", "entityType": "implementation", "observations": ["Beta obs"]},
                {"name": "Alpha", "entityType": "plan", "observations": ["Alpha obs 1", "Alpha obs 2"]},
            ],
            "relations": [{"from": "Alpha", "to": "Beta", "relationType": "implements"}],
            "other": [],
        }

    def test_round_trip_preserves_all(self, tmp_path):
        db = tmp_path / "test.db"
        expected = self._make_db(db)
        loaded = load_graph_sqlite(db)
        assert loaded == expected, f"Load mismatch: {loaded}"
        save_graph_sqlite(loaded, db)
        reloaded = load_graph_sqlite(db)
        assert reloaded == expected, f"Round-trip mismatch: {reloaded}"

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = load_graph_sqlite(tmp_path / "nonexistent.db")
        assert result == {"entities": [], "relations": [], "other": []}

    def test_entity_without_observations_still_appears(self, tmp_path):
        db = tmp_path / "noobs.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, entity_type TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER REFERENCES entities(id), content TEXT, created_at TEXT);
            CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity TEXT, to_entity TEXT, relation_type TEXT, created_at TEXT);
        """)
        conn.execute("INSERT INTO entities VALUES (1,'Solo','note','2025-01-01','2025-01-01')")
        conn.commit(); conn.close()
        result = load_graph_sqlite(db)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "Solo"
        assert result["entities"][0]["observations"] == []


class TestPipelineTestability:
    """Item #14: validator callable seam in run_pipeline."""

    def _tiny_graph(self):
        return {
            "entities": [
                {"name": "A", "entityType": "plan", "observations": []},
                {"name": "B", "entityType": "implementation", "observations": []},
            ],
            "relations": [],
        }

    def test_callable_validator_receives_candidates(self, monkeypatch):
        from dreamer.souffle_pipeline import run_pipeline
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda *a, **kw: ({"merge_entities": [], "rename_types": []},
                              [{"from": "A", "to": "B", "shared": 5}]),
        )
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [{"from": "A", "to": "B", "shared": 5}],
                              "duplicates": [], "renames": [], "stale": []},
        )
        called = []
        def stub_validator(candidates, graph):
            called.append((candidates, graph))
            return [{"from": "A", "to": "B", "relationType": "stub_rel"}]
        plan = run_pipeline(self._tiny_graph(), validator=stub_validator, rerank=False)
        assert len(called) == 1
        assert called[0][0][0]["from"] == "A"
        assert plan["mutations"]["add_relations"][0]["relationType"] == "stub_rel"

    def test_string_validator_still_works(self, monkeypatch):
        from dreamer.souffle_pipeline import run_pipeline
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda *a, **kw: ({"merge_entities": [], "rename_types": []},
                              [{"from": "A", "to": "B", "shared": 5}]),
        )
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [{"from": "A", "to": "B", "shared": 5}],
                              "duplicates": [], "renames": [], "stale": []},
        )
        plan = run_pipeline(self._tiny_graph(), validator="none", rerank=False)
        assert plan["mutations"]["add_relations"] == []


class TestLocalValidatorBatching:
    """Item #13: local validator batches LLM calls."""

    def _tiny_graph(self):
        return {
            "entities": [
                {"name": "plan_alpha", "entityType": "plan", "observations": []},
                {"name": "impl_beta", "entityType": "implementation", "observations": []},
                {"name": "bug_gamma", "entityType": "bug", "observations": []},
                {"name": "fix_delta", "entityType": "fix", "observations": []},
                {"name": "other", "entityType": "note", "observations": []},
            ],
            "relations": [],
        }

    def test_deterministic_rules_avoid_llm_call(self):
        from dreamer.local_validator import validate_and_name
        candidates = [
            {"from": "plan_alpha", "to": "impl_beta", "similarity": 0.8},
            {"from": "bug_gamma", "to": "fix_delta", "similarity": 0.9},
        ]
        call_count = [0]
        def stub_llm(prompt):
            call_count[0] += 1
            return {"add_relations": []}
        result = validate_and_name(candidates, self._tiny_graph(), use_local_llm=True, llm_callable=stub_llm)
        assert len(result) == 2
        assert call_count[0] == 0

    def test_batch_sends_one_prompt_for_multiple_unnamed(self):
        from dreamer.local_validator import validate_and_name
        candidates = [
            {"from": "other", "to": "plan_alpha", "similarity": 0.6},
            {"from": "other", "to": "impl_beta", "similarity": 0.55},
        ]
        call_count = [0]
        prompts_seen = []
        def stub_llm(prompt):
            call_count[0] += 1
            prompts_seen.append(prompt)
            return {"add_relations": [
                {"from": "other", "to": "plan_alpha", "relationType": "references"},
                {"from": "other", "to": "impl_beta", "relationType": "uses"},
            ]}
        result = validate_and_name(candidates, self._tiny_graph(), use_local_llm=True, llm_callable=stub_llm)
        assert call_count[0] == 1
        assert len(result) == 2
        assert {r["relationType"] for r in result} == {"references", "uses"}

    def test_never_fabricates_relation(self):
        from dreamer.local_validator import validate_and_name
        import dreamer.local_validator as lv
        candidates = [{"from": "other", "to": "plan_alpha", "similarity": 0.6}]
        def stub_llm(prompt):
            return {"add_relations": []}
        original = lv._local_llm_relation
        try:
            lv._local_llm_relation = lambda a, b: None  # simulate fallback also failing
            result = validate_and_name(
                candidates, self._tiny_graph(), use_local_llm=True, llm_callable=stub_llm
            )
            assert len(result) == 0
        finally:
            lv._local_llm_relation = original

    def test_llm_failure_falls_back_to_single_pair(self):
        from dreamer.local_validator import validate_and_name
        import dreamer.local_validator as lv
        candidates = [{"from": "other", "to": "plan_alpha", "similarity": 0.8}]
        def stub_llm(prompt):
            return None
        original = lv._local_llm_relation
        try:
            lv._local_llm_relation = lambda a, b: "fallback_rel"
            result = validate_and_name(candidates, self._tiny_graph(), use_local_llm=True, llm_callable=stub_llm)
            assert len(result) == 1
            assert result[0]["relationType"] == "fallback_rel"
        finally:
            lv._local_llm_relation = original

    def test_mixed_deterministic_and_batched(self):
        from dreamer.local_validator import validate_and_name
        candidates = [
            {"from": "plan_alpha", "to": "impl_beta", "similarity": 0.9},
            {"from": "other", "to": "bug_gamma", "similarity": 0.5},
        ]
        def stub_llm(prompt):
            assert "other" in prompt and "bug_gamma" in prompt
            assert "plan_alpha" not in prompt
            return {"add_relations": [{"from": "other", "to": "bug_gamma", "relationType": "relates_to"}]}
        result = validate_and_name(candidates, self._tiny_graph(), use_local_llm=True, llm_callable=stub_llm)
        assert len(result) == 2
        rel_types = {r["relationType"] for r in result}
        assert "implements" in rel_types
        assert "relates_to" in rel_types


class TestParseRelationType:
    """Item #5: robust _local_llm_relation parsing."""

    def test_parse_relation_type_direct(self):
        from dreamer.local_validator import _parse_relation_type
        assert _parse_relation_type("implements") == "implements"
        assert _parse_relation_type("the relation is fixes") == "fixes"
        assert _parse_relation_type("Relation type: uses.") == "uses"
        assert _parse_relation_type("I think it's part_of") == "part_of"
        assert _parse_relation_type("") is None

    def test_parse_relation_type_no_fabrication(self):
        from dreamer.local_validator import _parse_relation_type
        assert _parse_relation_type("the cat sat on the mat") is None
        assert _parse_relation_type("relation: unknown_type") is None
