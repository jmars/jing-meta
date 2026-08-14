import sqlite3
from pathlib import Path

from dreamer.dreamer import load_graph_sqlite, save_graph_sqlite
from jing_meta.schema import SCHEMA_DDL
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
        conn.commit()
        conn.close()
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
        conn.commit()
        conn.close()
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
        import dreamer.local_validator as lv
        from dreamer.local_validator import validate_and_name
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
        import dreamer.local_validator as lv
        from dreamer.local_validator import validate_and_name
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


class TestValidateCloudChunking:
    """validate_cloud splits candidates into chunks and merges results."""

    def test_empty_returns_empty(self):
        from dreamer.dreamer import validate_cloud
        assert validate_cloud([], api_url=None, api_key=None, model=None) == []

    def test_chunks_and_merges(self, monkeypatch):
        from dreamer import llm as llm_mod
        from dreamer.dreamer import validate_cloud

        calls = {"n": 0, "cand_counts": []}

        def fake_call(system_prompt, user_prompt, max_tokens, api_url, api_key, model):
            calls["n"] += 1
            # Count candidates from the numbered lines in the prompt.
            count = sum(1 for line in user_prompt.splitlines()
                        if len(line) > 4 and line.lstrip()[0].isdigit() and ". \"" in line)
            calls["cand_counts"].append(count)
            relations = [
                {"from": f"A{j}", "to": f"B{j}", "relationType": "related_to"}
                for j in range(count)
            ]
            return {"add_relations": relations}, {"model": "m"}

        monkeypatch.setattr(llm_mod, "call", fake_call)

        cands = [{"from": f"E{i}", "to": f"F{i}", "shared": 2} for i in range(45)]
        result = validate_cloud(cands, api_url=None, api_key=None, model=None, chunk_size=20)

        # 45 candidates / 20 per chunk = 3 chunks (20, 20, 5).
        assert calls["n"] == 3
        assert calls["cand_counts"] == [20, 20, 5]
        assert len(result) == 45

    def test_failed_chunk_skipped_rest_contribute(self, monkeypatch):
        from dreamer import llm as llm_mod
        from dreamer.dreamer import validate_cloud

        calls = {"n": 0}

        def fake_call(system_prompt, user_prompt, max_tokens, api_url, api_key, model):
            calls["n"] += 1
            if calls["n"] == 2:  # fail the middle chunk
                return None, None
            return {"add_relations": [{"from": "x", "to": "y", "relationType": "r"}]}, {}

        monkeypatch.setattr(llm_mod, "call", fake_call)

        cands = [{"from": "a", "to": "b", "shared": 1}] * 30
        result = validate_cloud(cands, api_url=None, api_key=None, model=None, chunk_size=10)

        # 3 chunks of 10; middle (chunk 2) fails -> skipped; chunks 1 & 3 contribute.
        assert calls["n"] == 3
        assert len(result) == 2  # one relation from each of the 2 successful chunks


class TestRevisionBumps:
    """save_graph_sqlite bumps the per-entity ``revision`` on mutations."""

    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_DDL)
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('Alpha', 'plan', '2025-01-01', '2025-01-02')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'obs 1', '2025-01-01')"
        )
        conn.commit()
        conn.close()

    def _rev(self, path: Path, name: str) -> int:
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute(
                "SELECT revision FROM entities WHERE name = ?", (name,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_no_change_does_not_bump(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        graph = load_graph_sqlite(db)
        save_graph_sqlite(graph, db)  # identical graph -> no revision change
        assert self._rev(db, "Alpha") == 0

    def test_type_change_bumps_revision(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        graph = load_graph_sqlite(db)
        graph["entities"][0]["entityType"] = "implementation"
        save_graph_sqlite(graph, db)
        assert self._rev(db, "Alpha") == 1

    def test_obs_add_bumps_revision(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        graph = load_graph_sqlite(db)
        graph["entities"][0]["observations"].append("obs 2")
        save_graph_sqlite(graph, db)
        assert self._rev(db, "Alpha") == 1

    def test_obs_delete_bumps_revision(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        graph = load_graph_sqlite(db)
        graph["entities"][0]["observations"] = []  # drop "obs 1"
        save_graph_sqlite(graph, db)
        assert self._rev(db, "Alpha") == 1


class TestSaveGraphCAS:
    """``save_graph_sqlite(observed=...)`` only deletes/overwrites state it observed."""

    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_DDL)
        conn.commit()
        conn.close()

    def _insert_entity(self, path, name, etype="plan", obs=(), rev=0):
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at, revision) "
            "VALUES (?, ?, '2025-01-01', '2025-01-01', ?)",
            (name, etype, rev),
        )
        eid = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (name,)
        ).fetchone()[0]
        for content in obs:
            conn.execute(
                "INSERT INTO observations (entity_id, content, created_at) "
                "VALUES (?, ?, '2025-01-01')",
                (eid, content),
            )
        conn.commit()
        conn.close()
        return eid

    def _insert_relation(self, path, from_e, to_e, rtype="references"):
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) "
            "VALUES (?, ?, ?, '2025-01-01')",
            (from_e, to_e, rtype),
        )
        conn.commit()
        conn.close()

    def _rev(self, path, name):
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute(
                "SELECT revision FROM entities WHERE name = ?", (name,)
            ).fetchone()[0]
        finally:
            conn.close()

    def _type(self, path, name):
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute(
                "SELECT entity_type FROM entities WHERE name = ?", (name,)
            ).fetchone()[0]
        finally:
            conn.close()

    def _obs(self, path, name):
        conn = sqlite3.connect(str(path))
        try:
            return [r[0] for r in conn.execute(
                "SELECT content FROM observations WHERE entity_id = "
                "(SELECT id FROM entities WHERE name = ?) ORDER BY id",
                (name,),
            ).fetchall()]
        finally:
            conn.close()

    def _rels(self, path):
        conn = sqlite3.connect(str(path))
        try:
            return set(conn.execute(
                "SELECT from_entity, to_entity, relation_type FROM relations"
            ).fetchall())
        finally:
            conn.close()

    def test_observed_does_not_delete_concurrent_observation(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        self._insert_entity(db, "Alpha", "plan", obs=["fact-A"])
        observed = load_graph_sqlite(db)

        # A concurrent writer adds fact-B after the dreamer loaded its snapshot.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES "
            "((SELECT id FROM entities WHERE name = 'Alpha'), 'fact-B', '2025-01-02')"
        )
        conn.commit()
        conn.close()

        desired = {
            "entities": [{"name": "Alpha", "entityType": "plan", "observations": ["fact-A"]}],
            "relations": [],
            "other": [],
        }
        save_graph_sqlite(desired, db, observed=observed)

        assert set(self._obs(db, "Alpha")) == {"fact-A", "fact-B"}

    def test_observed_does_not_overwrite_concurrent_type_change(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        self._insert_entity(db, "Alpha", "plan")
        observed = load_graph_sqlite(db)

        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE entities SET entity_type = 'implementation' WHERE name = 'Alpha'")
        conn.commit()
        conn.close()

        desired = {
            "entities": [{"name": "Alpha", "entityType": "plan", "observations": []}],
            "relations": [],
            "other": [],
        }
        save_graph_sqlite(desired, db, observed=observed)

        assert self._type(db, "Alpha") == "implementation"
        assert self._rev(db, "Alpha") == 0

    def test_observed_still_overwrites_type_when_unchanged(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        self._insert_entity(db, "Alpha", "plan")
        observed = load_graph_sqlite(db)

        desired = {
            "entities": [{"name": "Alpha", "entityType": "implementation", "observations": []}],
            "relations": [],
            "other": [],
        }
        save_graph_sqlite(desired, db, observed=observed)

        assert self._type(db, "Alpha") == "implementation"
        assert self._rev(db, "Alpha") == 1

    def test_observed_still_deletes_observed_unwanted_obs(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        self._insert_entity(db, "Alpha", "plan", obs=["a", "b"])
        observed = load_graph_sqlite(db)

        desired = {
            "entities": [{"name": "Alpha", "entityType": "plan", "observations": ["a"]}],
            "relations": [],
            "other": [],
        }
        save_graph_sqlite(desired, db, observed=observed)

        assert self._obs(db, "Alpha") == ["a"]
        assert self._rev(db, "Alpha") == 1

    def test_observed_preserves_concurrent_relation(self, tmp_path):
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        self._insert_entity(db, "Alpha", "plan")
        self._insert_entity(db, "Beta", "implementation")
        self._insert_relation(db, "Alpha", "Beta", "implements")
        observed = load_graph_sqlite(db)

        # A concurrent writer adds a second relation.
        self._insert_relation(db, "Alpha", "Beta", "uses")

        desired = {
            "entities": observed["entities"],
            "relations": [{"from": "Alpha", "to": "Beta", "relationType": "implements"}],
            "other": [],
        }
        save_graph_sqlite(desired, db, observed=observed)

        rels = self._rels(db)
        assert ("Alpha", "Beta", "implements") in rels
        assert ("Alpha", "Beta", "uses") in rels


class TestSaveGraphObservationUniqueness:
    """``save_graph_sqlite`` must not error when the DB already holds an observation."""

    def test_no_error_when_observation_already_present(self, tmp_path):
        db = tmp_path / "db.sqlite"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_DDL)
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) "
            "VALUES ('Alpha', 'plan', '2025-01-01', '2025-01-02')"
        )
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (1, 'obs 1', '2025-01-01')"
        )
        conn.commit()
        conn.close()

        graph = {
            "entities": [
                {"name": "Alpha", "entityType": "plan", "observations": ["obs 1"]}
            ],
            "relations": [],
            "other": [],
        }
        # Must not raise IntegrityError even though the unique index is present.
        save_graph_sqlite(graph, db)

        conn = sqlite3.connect(str(db))
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE entity_id = 1 AND content = 'obs 1'"
        ).fetchone()[0]
        conn.close()
        assert n == 1
