"""Dreamer stage functions — discover, rank, validate, apply.

Each function takes a ``RunContext`` and an optional prior ``StageResult`` and
returns a ``StageResult``.  When ``ctx.store`` is None the stage runs purely
in-memory (no disk persistence).  This is the path ``souffle_pipeline.run_pipeline``
uses to keep its no-disk contract.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jing_meta.log import get_logger

from .contracts import (
    Candidate,
    Mode,
    MutationPlan,
    RankedCandidate,
    RunContext,
    Stage,
    StageResult,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist(ctx: RunContext, result: StageResult) -> StageResult:
    """Persist *result* to disk if a store is available, else return as-is."""
    if ctx.store is not None:
        ctx.store.write_stage(ctx, result)
    return result


# ---------------------------------------------------------------------------
# Stage: discover
# ---------------------------------------------------------------------------


def discover(ctx: RunContext, prior: StageResult | None = None) -> StageResult:
    """Find candidate relations from the snapshot graph.

    Soufflé mode: loads snapshot, exports facts, runs Datalog, parses results.
    LLM mode: loads snapshot, caps, chunks, runs token-overlap suggestions.
    """
    from .dreamer import _cap_graph, _chunk_graph, load_graph_sqlite, suggest_relations

    graph = load_graph_sqlite(ctx.snapshot_db)
    n_entities = len(graph["entities"])
    n_relations = len(graph["relations"])
    print(f"Loaded (SQLite): {n_entities} entities, {n_relations} relations")

    if ctx.max_entities and ctx.max_entities > 0 and n_entities > ctx.max_entities:
        graph = _cap_graph(graph, ctx.max_entities)
        print(f"Capped to {ctx.max_entities} most-recently-updated entities")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if ctx.mode == Mode.SOUFFLE:
        import tempfile

        from .souffle_pipeline import (
            build_shortlist,
            export_facts,
            parse_results,
            run_souffle,
        )

        id2name = {i: e["name"] for i, e in enumerate(graph["entities"])}

        with tempfile.TemporaryDirectory(prefix="souffle-garden-") as td:
            base = Path(td)
            facts = base / "facts"
            out = base / "out"
            facts.mkdir()
            out.mkdir()
            export_facts(graph, facts)
            run_souffle(facts, out)
            results = parse_results(out, id2name)
            certain, llm_candidates = build_shortlist(results, id2name)

        candidates: list[Candidate] = []
        for c in llm_candidates:
            candidates.append(Candidate(
                from_=c["from"],
                to=c["to"],
                shared=c.get("shared"),
            ))

        payload: dict[str, Any] = {
            "certain_mutations": certain,
            "chunks": [{
                "entities": n_entities,
                "candidates": candidates,
            }],
            "n_entities": n_entities,
            "n_relations": n_relations,
        }
        return _persist(ctx, StageResult(
            stage=Stage.DISCOVER,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))

    else:  # Mode.LLM
        chunks = _chunk_graph(graph, chunk_size=ctx.chunk_size)
        payload_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            raw = suggest_relations(chunk)
            chunk_candidates = [
                Candidate(
                    from_=c["from"],
                    to=c["to"],
                    signals=c.get("signals"),
                )
                for c in raw
            ]
            payload_chunks.append({
                "entities": [e["name"] for e in chunk["entities"]],
                "candidates": chunk_candidates,
            })

        payload = {
            "chunks": payload_chunks,
            "n_entities": n_entities,
            "n_relations": n_relations,
        }
        return _persist(ctx, StageResult(
            stage=Stage.DISCOVER,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))


# ---------------------------------------------------------------------------
# Stage: rank
# ---------------------------------------------------------------------------


def rank(ctx: RunContext, prior: StageResult | None = None) -> StageResult:
    """Score/rank candidate relations.

    Soufflé mode: re-rank via semantic similarity or apply lexical fallback.
    LLM mode: passthrough (candidates already ranked by ``suggest_relations``).
    """
    if prior is None:
        raise ValueError("rank stage requires a prior discover result")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    discover_payload = prior.payload

    if ctx.mode == Mode.SOUFFLE:
        from .dreamer import load_graph_sqlite
        from .souffle_pipeline import rerank_with_semantic

        graph = load_graph_sqlite(ctx.snapshot_db)

        certain = discover_payload.get("certain_mutations", {})
        raw_candidates = discover_payload["chunks"][0]["candidates"]

        # Reconstruct the dict form that rerank_with_semantic expects
        cand_dicts = [
            {"from": c.from_, "to": c.to, "shared": c.shared or 0}
            for c in raw_candidates
        ]

        if ctx.rerank and cand_dicts:
            reranked = rerank_with_semantic(cand_dicts, graph)
            # Free the ONNX embedder before loading local LLM
            try:
                from jing_meta import embed as _embed
                _embed.free()
            except Exception:
                pass
        elif cand_dicts:
            # Lexical fallback (no semantic rerank)
            reranked = []
            for c in cand_dicts:
                shared = c.get("shared", 0)
                c_copy = dict(c)
                c_copy["similarity"] = round(min(shared / 10, 1.0), 3)
                reranked.append(c_copy)
        else:
            reranked = []

        ranked: list[RankedCandidate] = [
            RankedCandidate(
                from_=c["from"],
                to=c["to"],
                score=c.get("similarity", c.get("shared", 0) / 10),
                shared=c.get("shared", 0),
                similarity=c.get("similarity", 0.0),
            )
            for c in reranked
        ]

        payload: dict[str, Any] = {
            "candidates": ranked,
            "certain_mutations": certain,
        }
        return _persist(ctx, StageResult(
            stage=Stage.RANK,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))

    else:  # Mode.LLM — passthrough
        chunks = discover_payload.get("chunks", [])
        all_ranked: list[RankedCandidate] = []
        for chunk in chunks:
            for c in chunk.get("candidates", []):
                all_ranked.append(RankedCandidate(
                    from_=c.from_,
                    to=c.to,
                    score=float(len(c.signals or [])),
                    signals=c.signals or [],
                ))

        payload = {
            "candidates": all_ranked,
            "chunks": chunks,  # preserve chunk info for validate
        }
        return _persist(ctx, StageResult(
            stage=Stage.RANK,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))


# ---------------------------------------------------------------------------
# Stage: validate
# ---------------------------------------------------------------------------


def validate(ctx: RunContext, prior: StageResult | None = None) -> StageResult | None:
    """Validate/named candidate relations and produce a MutationPlan.

    LLM mode: the chunked build_prompt → call_llm → merge loop (one validate stage).
    If any LLM call returns None the run is aborted (returns None) — partial
    results are NOT applied.

    Soufflé mode: validator dispatch (callable, local, or cloud).
    """
    if prior is None:
        raise ValueError("validate stage requires a prior rank result")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if ctx.mode == Mode.LLM:
        from .dreamer import (
            _chunk_graph as chunk_graph,
        )
        from .dreamer import (
            build_prompt,
            call_llm,
            load_graph_sqlite,
            validate_plan,
        )

        graph = load_graph_sqlite(ctx.snapshot_db)
        discover_chunks = prior.payload.get("chunks", [])

        # Re-chunk the graph the same way discover did
        graph_chunks = chunk_graph(graph, chunk_size=ctx.chunk_size)

        merged: dict[str, Any] = {"mutations": {
            "archive_observations": [], "rename_types": [], "merge_entities": [],
            "add_entities": [], "add_relations": [],
        }}
        summaries: list[str] = []

        for ci, chunk_graph_item in enumerate(graph_chunks):
            chunk_info = discover_chunks[ci] if ci < len(discover_chunks) else {}
            cand_dicts = [
                {"from": c.from_, "to": c.to, "signals": c.signals}
                for c in (chunk_info.get("candidates") or [])
            ]

            prompt = build_prompt(chunk_graph_item, candidates=cand_dicts)
            print(
                f"  chunk {ci + 1}/{len(graph_chunks)}: {len(chunk_graph_item['entities'])} entities, "
                f"{len(chunk_graph_item['relations'])} relations, "
                f"{len(cand_dicts)} candidate relations, prompt {len(prompt)} chars"
            )
            if len(prompt) > 80_000:
                logger.warning("chunk prompt is %d chars — may exceed LLM context window", len(prompt))

            plan_dict, metadata = call_llm(
                prompt, api_url=ctx.api_url, api_key=ctx.api_key, model=ctx.model,
            )
            if plan_dict is None:
                logger.error("LLM call failed on chunk %d — aborting run.", ci + 1)
                return None

            if metadata:
                print(
                    f"    {metadata.get('model', '?')} | in {metadata.get('tokens_in', '?')} | "
                    f"out {metadata.get('tokens_out', '?')}"
                )

            chunk_muts = plan_dict.get("mutations", {})
            if isinstance(chunk_muts, dict):
                for key in merged["mutations"]:
                    merged["mutations"][key].extend(chunk_muts.get(key, []) or [])
            if plan_dict.get("summary"):
                summaries.append(str(plan_dict["summary"]))

        # Validate the merged plan
        warnings = validate_plan(merged)
        for w in warnings:
            logger.warning("%s", w)

        plan = MutationPlan.from_legacy_dict(merged)
        plan.summary = " ".join(summaries) if summaries else ""

        payload = {"plan": plan.to_legacy_dict()}
        return _persist(ctx, StageResult(
            stage=Stage.VALIDATE,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))

    else:  # Mode.SOUFFLE
        from . import llm
        from .dreamer import build_validation_prompt, load_graph_sqlite

        graph = load_graph_sqlite(ctx.snapshot_db)
        ranked = prior.payload.get("candidates", [])
        certain = prior.payload.get("certain_mutations", {})

        # Build candidate dicts for the validator
        cand_dicts = [
            {"from": c.from_, "to": c.to, "shared": c.shared, "similarity": c.similarity}
            for c in ranked
        ]

        added_relations: list[dict] = []
        if cand_dicts:
            validator = ctx.validator
            if callable(validator):
                added = validator(cand_dicts, graph)
                added_relations = [
                    {"from": r["from"], "to": r["to"], "relationType": r["relationType"]}
                    for r in added
                ]
            elif validator == "local":
                from .local_validator import validate_and_name
                added = validate_and_name(cand_dicts, graph, use_local_llm=True)
                added_relations = [
                    {"from": r["from"], "to": r["to"], "relationType": r["relationType"]}
                    for r in added
                ]
            elif validator == "cloud":
                prompt = build_validation_prompt(cand_dicts)
                llm_result, _meta = llm.call(
                    system_prompt="You are a knowledge graph relation validator.",
                    user_prompt=prompt,
                    max_tokens=2000,
                    api_url=ctx.api_url, api_key=ctx.api_key, model=ctx.model,
                )
                if llm_result and isinstance(llm_result, dict):
                    added = llm_result.get("add_relations", [])
                    if isinstance(added, list):
                        added_relations = added

        plan = MutationPlan(
            rename_types=certain.get("rename_types", []),
            merge_entities=certain.get("merge_entities", []),
            add_relations=added_relations,
        )

        # Compute stats
        plan.stats = {
            "candidates_found": len(ranked),
            "llm_candidates_shown": len(cand_dicts),
            "certain_merges": len(certain.get("merge_entities", [])),
            "certain_renames": len(certain.get("rename_types", [])),
        }

        payload = {"plan": plan.to_legacy_dict()}
        return _persist(ctx, StageResult(
            stage=Stage.VALIDATE,
            run_id=ctx.run_id,
            mode=ctx.mode,
            created_at=now,
            payload=payload,
        ))


# ---------------------------------------------------------------------------
# Stage: apply
# ---------------------------------------------------------------------------


def apply(ctx: RunContext, prior: StageResult | None = None) -> StageResult:
    """Apply the MutationPlan to the source DB (or dry-run)."""
    if prior is None:
        raise ValueError("apply stage requires a prior validate result")

    from .dreamer import apply_mutations, load_graph_sqlite, save_graph_sqlite

    plan_dict = prior.payload.get("plan", {})
    plan = MutationPlan.from_legacy_dict(plan_dict)

    # Load the LIVE graph (not the snapshot — we're applying to the real DB)
    graph = load_graph_sqlite(ctx.source_db)
    n_entities_before = len(graph["entities"])
    n_relations_before = len(graph["relations"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup_path = ""

    if ctx.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = str(ctx.source_db.with_suffix(".db.bak." + timestamp))
        shutil.copy2(ctx.source_db, backup_path)
        print(f"Backup saved to: {backup_path}")

        graph = apply_mutations(graph, plan.to_legacy_dict(), run_date=ctx.run_date)
        save_graph_sqlite(graph, ctx.source_db)

    n_entities_after = len(graph["entities"])
    n_relations_after = len(graph["relations"])

    payload: dict[str, Any] = {
        "applied": ctx.apply,
        "backup_path": backup_path,
        "n_entities_before": n_entities_before,
        "n_entities_after": n_entities_after,
        "n_relations_before": n_relations_before,
        "n_relations_after": n_relations_after,
    }
    return _persist(ctx, StageResult(
        stage=Stage.APPLY,
        run_id=ctx.run_id,
        mode=ctx.mode,
        created_at=now,
        payload=payload,
    ))
