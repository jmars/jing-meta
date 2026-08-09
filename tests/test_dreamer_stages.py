"""Tests for dreamer stages pipeline (roadmap #19).

Covers: contracts round-trips, RunStore lifecycle, per-stage logic,
replay, and backward compatibility seams.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from dreamer.dreamer import (
    apply_mutations,
    load_graph_sqlite,
    replay_run,
    run,
    run_souffle_mode,
)
from dreamer.souffle_pipeline import (
    run_pipeline,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tiny_graph() -> dict:
    return {
        "entities": [
            {"name": "Alpha", "entityType": "plan", "observations": ["requires implementation Beta"]},
            {"name": "Beta", "entityType": "implementation", "observations": ["implements Alpha"]},
            {"name": "Gamma", "entityType": "bug", "observations": ["caused by Beta"]},
            {"name": "Delta", "entityType": "fix", "observations": ["fixes Gamma"]},
        ],
        "relations": [
            {"from": "Alpha", "to": "Beta", "relationType": "references"},
        ],
    }


def _tmp_db(tmp_path: Path, name: str = "test.db") -> Path:
    """Create a minimal SQLite memory DB for tests."""
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
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
    for e in _tiny_graph()["entities"]:
        conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?,?,?,?)",
            (e["name"], e["entityType"], "2025-01-01", "2025-01-02"),
        )
    for r in _tiny_graph()["relations"]:
        conn.execute(
            "INSERT INTO relations (from_entity, to_entity, relation_type, created_at) VALUES (?,?,?,?)",
            (r["from"], r["to"], r["relationType"], "2025-01-01"),
        )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# TestContracts
# ---------------------------------------------------------------------------


class TestContracts:
    """Round-trip, rename, and legacy conversion tests for dataclasses."""

    def test_candidate_round_trip_all_shapes(self):
        from dreamer.contracts import Candidate, to_jsonable

        # Full shape
        c = Candidate(from_="A", to="B", signals=["tok1"], shared=3, similarity=0.8)
        d = to_jsonable(c)
        assert d["from"] == "A"
        assert d["to"] == "B"
        assert "from_" not in d
        assert d["signals"] == ["tok1"]
        assert d["shared"] == 3
        assert d["similarity"] == 0.8

        # Minimal shape
        c2 = Candidate(from_="X", to="Y")
        d2 = to_jsonable(c2)
        assert d2["from"] == "X"
        assert d2["to"] == "Y"
        assert d2["signals"] is None

        # None signals/shared/similarity
        c3 = Candidate(from_="P", to="Q", signals=None, shared=None, similarity=None)
        d3 = to_jsonable(c3)
        assert d3["signals"] is None

    def test_from_rename_is_explicit(self):
        """The from_ field must serialize as 'from' and deserialize back."""
        from dreamer.contracts import RankedCandidate, to_jsonable

        rc = RankedCandidate(from_="A", to="B", score=0.9, shared=5, similarity=0.8)
        d = to_jsonable(rc)
        assert d["from"] == "A"
        assert "from_" not in d

        # Round-trip through JSON
        raw = json.dumps(d)
        loaded = json.loads(raw)
        # The deserialisation layer handles the rename
        from dreamer.contracts import _rename_key
        _rename_key(loaded, "from", "from_")
        rc2 = RankedCandidate(**{k: v for k, v in loaded.items() if k in {"from_", "to", "score", "signals", "shared", "similarity"}})
        assert rc2.from_ == "A"

    def test_ranked_defaults(self):
        from dreamer.contracts import RankedCandidate
        rc = RankedCandidate(from_="A", to="B", score=0.5)
        assert rc.signals == []
        assert rc.shared == 0
        assert rc.similarity == 0.0

    def test_mutation_plan_legacy_round_trip_identity(self):
        from dreamer.contracts import MutationPlan

        original = {
            "mutations": {
                "archive_observations": [{"entity": "E", "observation_index": 0, "reason": "stale"}],
                "rename_types": [{"entity": "E", "new_type": "note"}],
                "merge_entities": [{"keep": "A", "remove": "B", "reason": "dup"}],
                "add_entities": [{"name": "N", "entityType": "summary", "observations": ["obs"]}],
                "add_relations": [{"from": "A", "to": "B", "relationType": "related_to"}],
            },
            "summary": "test plan",
            "_stats": {"candidates": 5},
        }
        plan = MutationPlan.from_legacy_dict(original)
        assert plan.archive_observations == [{"entity": "E", "observation_index": 0, "reason": "stale"}]
        assert plan.summary == "test plan"
        assert plan.stats == {"candidates": 5}

        # Round-trip back
        d = plan.to_legacy_dict()
        assert d["mutations"]["archive_observations"] == original["mutations"]["archive_observations"]
        assert d["summary"] == "test plan"
        assert d["_stats"] == {"candidates": 5}

    def test_stage_result_version_field(self):
        from dreamer.contracts import Mode, Stage, StageResult, to_jsonable

        sr = StageResult(
            stage=Stage.DISCOVER, run_id="20250101T000000Z",
            mode=Mode.LLM, created_at="now", payload={"key": "val"},
        )
        d = to_jsonable(sr)
        assert d["version"] == 1
        assert d["stage"] == "discover"
        assert d["mode"] == "llm"

    def test_stage_filename(self):
        from dreamer.contracts import Stage, stage_filename
        assert stage_filename(Stage.DISCOVER) == "01_discover.json"
        assert stage_filename(Stage.RANK) == "02_rank.json"
        assert stage_filename(Stage.VALIDATE) == "03_validate.json"
        assert stage_filename(Stage.APPLY) == "04_apply.json"

    def test_stages_after(self):
        from dreamer.contracts import Stage, stages_after
        assert stages_after(Stage.DISCOVER) == (Stage.DISCOVER, Stage.RANK, Stage.VALIDATE, Stage.APPLY)
        assert stages_after(Stage.VALIDATE) == (Stage.VALIDATE, Stage.APPLY)
        assert stages_after(Stage.APPLY) == (Stage.APPLY,)


# ---------------------------------------------------------------------------
# TestRunStore
# ---------------------------------------------------------------------------


class TestRunStore:
    """RunStore lifecycle: create, write stages, load, list."""

    def test_new_run_id_collision_suffix(self, tmp_path, monkeypatch):
        from dreamer.contracts import Mode
        from dreamer.runstore import RunStore

        store = RunStore(tmp_path)
        db_path = _tmp_db(tmp_path, "collision.db")

        # Create a run with a name that would collide
        rid = store.new_run_id()
        store.create_run(rid, Mode.LLM, db_path)

        # Force a collision: create a dir with the next candidate
        (tmp_path / f"{rid}-2").mkdir()
        rid2 = store.new_run_id()
        assert rid2 != rid
        assert rid2.endswith("-3")  # -2 existed, so -3

    def test_create_run_snapshots_db(self, tmp_path):
        from dreamer.contracts import Mode
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.LLM, db_path)

        snapshot = ctx.snapshot_db
        assert snapshot.is_file()
        # Byte-identical copy
        assert snapshot.read_bytes() == db_path.read_bytes()

    def test_write_stage_atomic_cleans_tmp(self, tmp_path):
        from dreamer.contracts import Mode, Stage, StageResult
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.LLM, db_path)

        sr = StageResult(
            stage=Stage.DISCOVER, run_id=rid,
            mode=Mode.LLM, created_at="now", payload={"k": "v"},
        )
        store.write_stage(ctx, sr)

        # Check file exists
        stage_path = store.run_dir(rid) / "01_discover.json"
        assert stage_path.is_file()
        content = json.loads(stage_path.read_text())
        assert content["stage"] == "discover"
        assert content["payload"] == {"k": "v"}

        # No tmp file left behind
        tmps = list(store.run_dir(rid).glob("*.tmp"))
        assert len(tmps) == 0

    def test_write_stage_updates_manifest(self, tmp_path):
        from dreamer.contracts import Mode, Stage, StageResult
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.LLM, db_path)

        sr = StageResult(
            stage=Stage.DISCOVER, run_id=rid,
            mode=Mode.LLM, created_at="now",
        )
        store.write_stage(ctx, sr)

        manifest = store.load_manifest(rid)
        assert manifest is not None
        assert Stage.DISCOVER in manifest.completed

    def test_load_run_returns_all_stages(self, tmp_path):
        from dreamer.contracts import Mode, Stage, StageResult
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.LLM, db_path)

        for stage in (Stage.DISCOVER, Stage.RANK, Stage.VALIDATE):
            store.write_stage(ctx, StageResult(
                stage=stage, run_id=rid, mode=Mode.LLM, created_at="t",
            ))

        manifest, stages = store.load_run(rid)
        assert Stage.DISCOVER in stages
        assert Stage.RANK in stages
        assert Stage.VALIDATE in stages
        assert Stage.APPLY not in stages

    def test_list_runs_sorted_desc(self, tmp_path):
        from dreamer.contracts import Mode
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")

        store.create_run("run-a", Mode.LLM, db_path)
        store.create_run("run-b", Mode.LLM, db_path)

        runs = store.list_runs()
        assert runs == ["run-b", "run-a"]


# ---------------------------------------------------------------------------
# TestDiscoverStage
# ---------------------------------------------------------------------------


class TestDiscoverStage:
    """Discover stage: Soufflé and LLM modes."""

    def test_souffle_writes_certain_and_candidates(self, tmp_path, monkeypatch):
        from dreamer.contracts import Mode, RunContext
        from dreamer.stages import discover

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.SOUFFLE,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
        )

        # Monkeypatch souffle pipeline helpers that discover calls internally
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda results, id2name: (
                {"merge_entities": [], "rename_types": [{"entity": "A", "new_type": "note"}]},
                [{"from": "A", "to": "B", "shared": 5}],
            ),
        )
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [], "duplicates": [], "renames": [], "stale": []},
        )

        result = discover(ctx)
        assert result.stage.value == "discover"
        payload = result.payload
        assert "certain_mutations" in payload
        assert len(payload["chunks"]) == 1
        assert len(payload["chunks"][0]["candidates"]) == 1
        c = payload["chunks"][0]["candidates"][0]
        assert c.from_ == "A"

    def test_llm_writes_per_chunk_candidates(self, tmp_path, monkeypatch):
        from dreamer.contracts import Mode, RunContext
        from dreamer.stages import discover

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
            chunk_size=2,  # small to get multiple chunks
        )

        result = discover(ctx)
        assert result.stage.value == "discover"
        chunks = result.payload["chunks"]
        assert len(chunks) >= 1
        # Each chunk has an "entities" list and "candidates" list
        for ch in chunks:
            assert isinstance(ch["entities"], list)
            assert isinstance(ch["candidates"], list)


# ---------------------------------------------------------------------------
# TestRankStage
# ---------------------------------------------------------------------------


class TestRankStage:
    """Rank stage: Soufflé rerank / lexical fallback, LLM passthrough."""

    def test_souffle_rerank_with_stubbed_semantic(self, tmp_path, monkeypatch):
        from dreamer.contracts import Candidate, Mode, RunContext, Stage, StageResult
        from dreamer.stages import rank

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.SOUFFLE,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False, rerank=True,
        )

        prior = StageResult(
            stage=Stage.DISCOVER, run_id="test", mode=Mode.SOUFFLE,
            created_at="now",
            payload={
                "certain_mutations": {"merge_entities": [], "rename_types": []},
                "chunks": [{
                    "entities": 4,
                    "candidates": [Candidate(from_="A", to="B", shared=5)],
                }],
            },
        )

        # Stub rerank_with_semantic to return re-ranked list
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.rerank_with_semantic",
            lambda cands, graph: [{"from": "A", "to": "B", "shared": 5, "similarity": 0.85}],
        )
        # Stub embed.free()
        monkeypatch.setattr("jing_meta.embed", lambda: None, raising=False)
        try:
            monkeypatch.setattr("jing_meta.embed.free", lambda: None)
        except Exception:
            pass

        result = rank(ctx, prior)
        assert result.stage.value == "rank"
        ranked = result.payload["candidates"]
        assert len(ranked) == 1
        rc = ranked[0]
        assert rc.from_ == "A"
        assert rc.similarity == 0.85

    def test_souffle_lexical_fallback_when_rerank_false(self, tmp_path):
        from dreamer.contracts import Candidate, Mode, RunContext, Stage, StageResult
        from dreamer.stages import rank

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.SOUFFLE,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False, rerank=False,
        )

        prior = StageResult(
            stage=Stage.DISCOVER, run_id="test", mode=Mode.SOUFFLE,
            created_at="now",
            payload={
                "certain_mutations": {},
                "chunks": [{
                    "entities": 4,
                    "candidates": [Candidate(from_="A", to="B", shared=10)],
                }],
            },
        )

        result = rank(ctx, prior)
        ranked = result.payload["candidates"]
        assert ranked[0].similarity == 1.0  # min(10/10, 1.0)

    def test_llm_passthrough(self, tmp_path):
        from dreamer.contracts import Candidate, Mode, RunContext, Stage, StageResult
        from dreamer.stages import rank

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
        )

        prior = StageResult(
            stage=Stage.DISCOVER, run_id="test", mode=Mode.LLM,
            created_at="now",
            payload={
                "chunks": [{
                    "entities": ["Alpha", "Beta"],
                    "candidates": [Candidate(from_="Alpha", to="Beta", signals=["tok1", "tok2"])],
                }],
            },
        )

        result = rank(ctx, prior)
        ranked = result.payload["candidates"]
        assert len(ranked) == 1
        assert ranked[0].score == 2.0  # len(signals)


# ---------------------------------------------------------------------------
# TestValidateStage
# ---------------------------------------------------------------------------


class TestValidateStage:
    """Validate stage: Soufflé validator dispatch, LLM per-chunk merge."""

    def test_souffle_callable_seam_assembles_plan(self, tmp_path, monkeypatch):
        from dreamer.contracts import (
            Mode,
            RankedCandidate,
            RunContext,
            Stage,
            StageResult,
        )
        from dreamer.stages import validate

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.SOUFFLE,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
            validator=lambda cands, graph: [
                {"from": "A", "to": "B", "relationType": "stub_rel"},
            ],
        )

        prior = StageResult(
            stage=Stage.RANK, run_id="test", mode=Mode.SOUFFLE,
            created_at="now",
            payload={
                "candidates": [
                    RankedCandidate(from_="A", to="B", score=0.9, shared=5, similarity=0.8),
                ],
                "certain_mutations": {
                    "rename_types": [{"entity": "X", "new_type": "note"}],
                    "merge_entities": [],
                },
            },
        )

        result = validate(ctx, prior)
        plan_dict = result.payload["plan"]
        muts = plan_dict["mutations"]
        assert len(muts["add_relations"]) == 1
        assert muts["add_relations"][0]["relationType"] == "stub_rel"
        assert len(muts["rename_types"]) == 1

    def test_validate_plan_warnings_logged_not_fatal(self, tmp_path, monkeypatch):
        """Warnings from validate_plan should be logged but not crash."""
        from dreamer.contracts import (
            Mode,
            RunContext,
            Stage,
            StageResult,
        )
        from dreamer.stages import validate

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.SOUFFLE,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
            validator=lambda cands, graph: [],
        )

        prior = StageResult(
            stage=Stage.RANK, run_id="test", mode=Mode.SOUFFLE,
            created_at="now",
            payload={
                "candidates": [],
                "certain_mutations": {"rename_types": [], "merge_entities": []},
            },
        )

        result = validate(ctx, prior)
        assert result is not None
        plan_dict = result.payload["plan"]
        assert "mutations" in plan_dict

    def test_llm_per_chunk_merge(self, tmp_path, monkeypatch):
        from dreamer.contracts import Candidate, Mode, RunContext, Stage, StageResult
        from dreamer.stages import validate

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
            chunk_size=2,
        )

        prior = StageResult(
            stage=Stage.RANK, run_id="test", mode=Mode.LLM,
            created_at="now",
            payload={
                "chunks": [{
                    "entities": ["Alpha", "Beta"],
                    "candidates": [Candidate(from_="Alpha", to="Beta", signals=["tok"])],
                }],
            },
        )

        # Stub call_llm to return a plan per chunk. validate() imports these
        # from .dreamer *inside the function body*, so patch the dreamer module
        # (dreamer.dreamer), not dreamer.stages.
        call_count = [0]

        def stub_call_llm(prompt, **kw):
            call_count[0] += 1
            return {
                "mutations": {
                    "add_relations": [{"from": "Alpha", "to": "Beta", "relationType": "uses"}],
                    "archive_observations": [], "rename_types": [], "merge_entities": [],
                    "add_entities": [],
                },
                "summary": "linked",
            }, {"model": "stub"}

        monkeypatch.setattr("dreamer.dreamer.call_llm", stub_call_llm)

        # Stub validate_plan to avoid checking real mutations
        monkeypatch.setattr("dreamer.dreamer.validate_plan", lambda plan: [])

        result = validate(ctx, prior)
        assert call_count[0] >= 1
        plan_dict = result.payload["plan"]
        assert len(plan_dict["mutations"]["add_relations"]) >= 1


# ---------------------------------------------------------------------------
# TestApplyStage
# ---------------------------------------------------------------------------


class TestApplyStage:
    """Apply stage: dry-run vs real write."""

    def test_dry_run_no_op_ack(self, tmp_path):
        from dreamer.contracts import Mode, MutationPlan, RunContext, Stage, StageResult
        from dreamer.stages import apply

        db_path = _tmp_db(tmp_path, "source.db")
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=False,
        )

        plan = MutationPlan()
        prior = StageResult(
            stage=Stage.VALIDATE, run_id="test", mode=Mode.LLM,
            created_at="now",
            payload={"plan": plan.to_legacy_dict()},
        )

        result = apply(ctx, prior)
        assert result.payload["applied"] is False
        assert result.payload["backup_path"] == ""

    def test_real_write_backup_and_save(self, tmp_path):
        from dreamer.contracts import Mode, MutationPlan, RunContext, Stage, StageResult
        from dreamer.stages import apply

        db_path = _tmp_db(tmp_path, "source.db")
        run_date = "2025-06-15"
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=True,
            run_date=run_date,
        )

        plan = MutationPlan(
            add_relations=[{"from": "Alpha", "to": "Gamma", "relationType": "causes"}],
        )
        prior = StageResult(
            stage=Stage.VALIDATE, run_id="test", mode=Mode.LLM,
            created_at="now",
            payload={"plan": plan.to_legacy_dict()},
        )

        result = apply(ctx, prior)
        assert result.payload["applied"] is True
        assert result.payload["backup_path"] != ""

        # Verify the relation was added
        graph = load_graph_sqlite(db_path)
        rels = {(r["from"], r["to"], r["relationType"]) for r in graph["relations"]}
        assert ("Alpha", "Gamma", "causes") in rels

    def test_run_date_threaded_into_archive_tag(self, tmp_path):
        from dreamer.contracts import Mode, MutationPlan, RunContext, Stage, StageResult
        from dreamer.stages import apply

        db_path = _tmp_db(tmp_path, "source.db")
        # Add an entity with an observation
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO entities VALUES (10,'ObsEntity','note','2025-01-01','2025-01-01')")
        conn.execute("INSERT INTO observations VALUES (10,10,'old content','2025-01-01')")
        conn.commit()
        conn.close()

        run_date = "2025-12-25"
        ctx = RunContext(
            run_id="test", mode=Mode.LLM,
            source_db=db_path, snapshot_db=db_path,
            run_dir=tmp_path, store=None, apply=True,
            run_date=run_date,
        )

        plan = MutationPlan(
            archive_observations=[{"entity": "ObsEntity", "observation_index": 0, "reason": "stale"}],
        )
        prior = StageResult(
            stage=Stage.VALIDATE, run_id="test", mode=Mode.LLM,
            created_at="now",
            payload={"plan": plan.to_legacy_dict()},
        )

        apply(ctx, prior)
        graph = load_graph_sqlite(db_path)
        obs_entity = next(e for e in graph["entities"] if e["name"] == "ObsEntity")
        assert "[archived: 2025-12-25 stale]" in obs_entity["observations"][0]


# ---------------------------------------------------------------------------
# TestReplay
# ---------------------------------------------------------------------------


class TestReplay:
    """Replay: from_validate reproduces plan; from_rank re-runs validate; apply idempotent."""

    def test_from_validate_reproduces_plan_with_stubbed_llm(self, tmp_path, monkeypatch):
        """Replaying from validate with a stubbed LLM should reproduce the plan."""
        from dreamer.contracts import Mode
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.SOUFFLE, db_path)

        from dreamer.stages import discover, rank

        # Manually create discover and rank stages
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [], "duplicates": [], "renames": [], "stale": []},
        )
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda r, i: (
                {"merge_entities": [], "rename_types": []},
                [{"from": "A", "to": "B", "shared": 5}],
            ),
        )

        d_result = discover(ctx)
        r_result = rank(ctx, d_result)

        # Now replay from validate
        from dreamer.stages import validate

        ctx2 = RunStore.reconstitute_ctx(store, store.load_manifest(rid), apply=False)
        ctx2.validator = lambda cands, graph: [
            {"from": "A", "to": "B", "relationType": "replayed_rel"},
        ]

        v_result = validate(ctx2, r_result)
        plan_dict = v_result.payload["plan"]
        assert plan_dict["mutations"]["add_relations"][0]["relationType"] == "replayed_rel"

    def test_replay_run_unknown_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            replay_run("nonexistent-run-id")

    def test_replay_apply_dry_run_idempotent(self, tmp_path):
        """Replaying apply as dry-run should not change the DB."""
        from dreamer.contracts import Mode
        from dreamer.runstore import RunStore

        db_path = _tmp_db(tmp_path, "source.db")
        store = RunStore(tmp_path / "dreamer")
        rid = store.new_run_id()
        ctx = store.create_run(rid, Mode.LLM, db_path)

        from dreamer.contracts import MutationPlan, Stage, StageResult

        # Write dummy discover/rank/validate
        for stage, payload in [
            (Stage.DISCOVER, {"chunks": [], "n_entities": 0, "n_relations": 0}),
            (Stage.RANK, {"candidates": [], "chunks": []}),
            (Stage.VALIDATE, {"plan": MutationPlan().to_legacy_dict()}),
        ]:
            store.write_stage(ctx, StageResult(
                stage=stage, run_id=rid, mode=Mode.LLM, created_at="t", payload=payload,
            ))

        # Replay apply dry-run (inject the tmp store so the run is found)
        result = replay_run(rid, from_stage="apply", store=store)
        assert result == 0


# ---------------------------------------------------------------------------
# TestBackwardCompat
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Existing public signatures still work with no new kwargs."""

    def test_run_no_new_kwargs_works(self, tmp_path, monkeypatch):
        """run(db, apply=False) without run_id/from_stage should work."""
        db_path = _tmp_db(tmp_path, "source.db")

        # Stub call_llm to return a minimal valid plan
        monkeypatch.setattr(
            "dreamer.dreamer.call_llm",
            lambda prompt, **kw: (
                {"mutations": {"add_relations": [], "archive_observations": [],
                               "rename_types": [], "merge_entities": [], "add_entities": []},
                 "summary": "clean"},
                {"model": "stub"},
            ),
        )
        # Stub validate_plan
        monkeypatch.setattr("dreamer.dreamer.validate_plan", lambda plan: [])

        rc = run(db_path, apply=False)
        assert rc == 0

    def test_run_pipeline_returns_legacy_shape(self, monkeypatch):
        """run_pipeline returns the same shape as before."""
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [], "duplicates": [], "renames": [], "stale": []},
        )
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda r, i: (
                {"merge_entities": [], "rename_types": []},
                [],
            ),
        )

        plan = run_pipeline(_tiny_graph(), validator="none", rerank=False)
        assert "mutations" in plan
        assert "_stats" in plan
        assert "add_relations" in plan["mutations"]
        assert "rename_types" in plan["mutations"]
        assert "merge_entities" in plan["mutations"]

    def test_callable_validator_seam_still_works(self, monkeypatch):
        """The monkeypatched test from test_dreamer.py still passes."""
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

        plan = run_pipeline(_tiny_graph(), validator=stub_validator, rerank=False)
        assert len(called) == 1
        assert called[0][0][0]["from"] == "A"
        assert plan["mutations"]["add_relations"][0]["relationType"] == "stub_rel"

    def test_run_souffle_mode_default_args(self, tmp_path, monkeypatch):
        """run_souffle_mode with just (db, apply=False) should work."""
        db_path = _tmp_db(tmp_path, "source.db")
        monkeypatch.setattr("dreamer.souffle_pipeline.run_souffle", lambda *a, **kw: None)
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.parse_results",
            lambda *a, **kw: {"candidates": [], "duplicates": [], "renames": [], "stale": []},
        )
        monkeypatch.setattr(
            "dreamer.souffle_pipeline.build_shortlist",
            lambda r, i: ({"merge_entities": [], "rename_types": []}, []),
        )

        rc = run_souffle_mode(db_path, apply=False, validator="none", rerank=False)
        assert rc == 0

    def test_apply_mutations_no_run_date_kwarg(self):
        """apply_mutations without run_date uses current date."""
        graph = {
            "entities": [{"name": "E", "entityType": "note", "observations": ["old"]}],
            "relations": [],
        }
        plan = {
            "mutations": {
                "archive_observations": [{"entity": "E", "observation_index": 0, "reason": "x"}],
                "rename_types": [], "merge_entities": [], "add_entities": [], "add_relations": [],
            },
        }
        result = apply_mutations(graph, plan)
        # Should have archived tag with today's date
        obs = result["entities"][0]["observations"][0]
        assert "[archived:" in obs

    def test_apply_mutations_with_run_date_kwarg(self):
        """apply_mutations with run_date uses the supplied date."""
        graph = {
            "entities": [{"name": "E", "entityType": "note", "observations": ["old"]}],
            "relations": [],
        }
        plan = {
            "mutations": {
                "archive_observations": [{"entity": "E", "observation_index": 0, "reason": "x"}],
                "rename_types": [], "merge_entities": [], "add_entities": [], "add_relations": [],
            },
        }
        result = apply_mutations(graph, plan, run_date="2025-07-04")
        obs = result["entities"][0]["observations"][0]
        assert "[archived: 2025-07-04" in obs
