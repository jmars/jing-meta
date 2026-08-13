"""Soufflé pipeline — deterministic maintenance tiers via Datalog + LLM validation.

Replaces the LLM's discovery work (expensive, flaky) with a compiled Datalog
ruleset (Soufflé) that computes the deterministic tiers in sub-second:

  consolidate_types   -> provable one-off -> canonical renames
  detect_duplicates   -> provable normalized-name collisions
  candidate_relations -> token-overlap pairs, ranked by shared-token count

The deterministic results that need NO judgment (type renames, exact duplicates)
are applied directly. Only the fuzzy part — which candidate relations are *really*
related and what to name them — is sent to the LLM, on a capped, ranked shortlist.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from jing_meta import config as _config
from jing_meta.text import STOPWORDS

SOUFFLE = os.environ.get("SOUFFLE", _config.SOUFFLE)
GARDEN_DL = Path(__file__).parent / "souffle" / "garden.dl"

# Cap on candidate relations sent to the LLM for validation (highest shared-token first).
MAX_LLM_CANDIDATES = int(os.environ.get("SOUFFLE_MAX_CANDIDATES", "80"))
MIN_SHARED = int(os.environ.get("SOUFFLE_MIN_SHARED", "2"))
STALE_CUTOFF = os.environ.get("SOUFFLE_STALE_CUTOFF", "2026-05-10T00:00:00Z")


class SouffleError(Exception):
    pass


def _read_csv(path: Path, cols: int) -> list[list[str]]:
    """Read a Soufflé output CSV (tab-delimited, no header)."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= cols:
            rows.append(parts[:cols])
    return rows


def export_facts(graph: dict, facts_dir: Path) -> dict[str, int]:
    """Write graph facts to *facts_dir*. Returns {rel: count}."""
    id_by_name = {}
    with open(facts_dir / "entity.csv", "w", encoding="utf-8") as f:
        for i, e in enumerate(graph["entities"]):
            name = e["name"].replace("\n", " ")
            etype = (e.get("entityType") or "unknown").replace("\n", " ")
            id_by_name[name] = i
            f.write(f"{i}\t{name}\t{etype}\n")
    with open(facts_dir / "obs.csv", "w", encoding="utf-8") as f:
        for e in graph["entities"]:
            eid = id_by_name.get(e["name"])
            if eid is None:
                continue
            for o in e.get("observations", []):
                content = str(o).replace("\n", " ").replace("\t", " ")
                f.write(f"{eid}\t{content}\n")
    with open(facts_dir / "relation.csv", "w", encoding="utf-8") as f:
        for r in graph["relations"]:
            fid = id_by_name.get(r.get("from"))
            tid = id_by_name.get(r.get("to"))
            if fid is None or tid is None:
                continue
            f.write(f"{fid}\t{tid}\t{r.get('relationType', 'references')}\n")

    # canonical type mapping (from the packaged ruleset dir)
    canonical_src = Path(__file__).parent / "souffle" / "canonical_type.csv"
    if canonical_src.exists():
        (facts_dir / "canonical_type.csv").write_text(canonical_src.read_text(), encoding="utf-8")

    with open(facts_dir / "norm_name.csv", "w", encoding="utf-8") as f:
        import re
        for e in graph["entities"]:
            norm = re.sub(r"[^a-z0-9]", "", e["name"].lower())
            if len(norm) > 3:
                f.write(f"{id_by_name[e['name']]}\t{norm}\n")
    # tokens for candidate generation
    import re as _re
    with open(facts_dir / "token.csv", "w", encoding="utf-8") as f:
        for e in graph["entities"]:
            eid = id_by_name[e["name"]]
            text = e["name"] + " " + " ".join(str(o) for o in e.get("observations", [])[:3])
            for t in set(_re.findall(r"[a-z0-9][a-z0-9_-]*", text.lower())):
                if t not in STOPWORDS and len(t) > 2:
                    f.write(f"{eid}\t{t}\n")
    counts = {
        "entity": len(graph["entities"]),
        "obs": sum(len(e.get("observations", [])) for e in graph["entities"]),
        "relation": len(graph["relations"]),
    }
    return counts


def run_souffle(facts_dir: Path, output_dir: Path) -> None:
    """Run the Soufflé ruleset over the facts. Raises on failure."""
    if not GARDEN_DL.exists():
        raise SouffleError(f"ruleset not found: {GARDEN_DL}")
    if not Path(SOUFFLE).exists():
        raise SouffleError(f"souffle binary not found at {SOUFFLE}")
    output_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [SOUFFLE, str(GARDEN_DL), "-F", str(facts_dir), "-D", str(output_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise SouffleError(f"souffle failed: {proc.stderr.strip()}")


def parse_results(output_dir: Path, id2name: dict[int, str]) -> dict:
    """Parse Soufflé outputs into structured results with real names."""
    def name(pair):
        return (id2name.get(int(pair[0]), "?"), id2name.get(int(pair[1]), "?"))

    candidates = []
    for row in _read_csv(output_dir / "candidate_relation.csv", 3):
        a, b, shared = row[0], row[1], int(row[2])
        candidates.append({"from": name(row)[0], "to": name(row)[1], "shared": shared})

    duplicates = []
    for row in _read_csv(output_dir / "duplicate_pair.csv", 2):
        a, b = name(row)
        duplicates.append({"a": a, "b": b})

    renames = []
    for row in _read_csv(output_dir / "type_rename.csv", 3):
        # columns: eid, from, to
        renames.append({"name": id2name.get(int(row[0]), "?"), "from": row[1], "to": row[2]})

    stale = []
    for row in _read_csv(output_dir / "stale_obs.csv", 2):
        stale.append({"eid": int(row[0])})

    # rank candidates by shared-token count desc, drop already-connected, cap
    candidates.sort(key=lambda c: -c["shared"])
    return {
        "candidates": candidates,
        "duplicates": duplicates,
        "renames": renames,
        "stale": stale,
    }


def build_shortlist(results: dict, id2name: dict[int, str]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Split results into (certain, llm_needed).

    certain = duplicates + renames (provable, no LLM).
    llm_needed = top-N ranked candidates (fuzzy, needs validation/naming).
    """
    certain = {
        "merge_entities": [
            {"keep": d["a"], "remove": d["b"], "reason": "duplicate name (normalized collision)"}
            for d in results["duplicates"]
        ],
        "rename_types": [
            {"entity": r["name"], "new_type": r["to"]} for r in results["renames"]
        ],
    }
    llm_candidates = [
        {"from": c["from"], "to": c["to"], "shared": c["shared"]}
        for c in results["candidates"]
        if c["shared"] >= MIN_SHARED
    ][:MAX_LLM_CANDIDATES]
    return certain, llm_candidates


def rerank_with_semantic(
    candidates: list[dict],
    graph: dict,
    *,
    semantic_weight: float = 0.6,
) -> list[dict]:
    """Re-rank Soufflé candidates using semantic similarity.

    Soufflé ranks by shared-token *count* (lexical), which over-weights pairs
    that merely share many generic domain tokens. Semantic search re-ranks by
    *meaningful* similarity. A pair's final score is a blend:

        score = semantic_weight * semantic_sim + (1-semantic_weight) * norm_shared

    where norm_shared = min(shared / 10, 1) (saturates, so 10+ shared tokens
    is 'fully lexically strong'). Pairs are re-ordered by score, then capped at
    MAX_LLM_CANDIDATES. Requires the semantic tier (fastembed); if it's
    unavailable, returns the original ranking unchanged.
    """
    try:
        from .semantic_search import semantic_candidates
    except Exception:
        return candidates[:MAX_LLM_CANDIDATES]

    # Build a lookup of semantic similarity for the candidate pairs.
    try:
        sems = semantic_candidates(graph)
        sim_lookup = {
            tuple(sorted([s["from"], s["to"]])): s["similarity"]
            for s in sems
        }
    except Exception:
        return candidates[:MAX_LLM_CANDIDATES]

    scored = []
    for c in candidates:
        key = tuple(sorted([c["from"], c["to"]]))
        sim = sim_lookup.get(key)
        if sim is None:
            # Not in the semantic top-N; give it a low semantic score so
            # lexically-strong-but-semantically-weak pairs sink.
            sim = 0.0
        norm_shared = min(c.get("shared", 0) / 10, 1.0)
        score = semantic_weight * sim + (1 - semantic_weight) * norm_shared
        scored.append({**c, "similarity": round(sim, 3), "_score": round(score, 4)})

    scored.sort(key=lambda x: -x["_score"])
    for c in scored:
        c.pop("_score", None)
    return scored[:MAX_LLM_CANDIDATES]


def run_pipeline(
    graph: dict,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    validate_relations: bool = True,
    rerank: bool = True,
    validator: str = "local",
) -> dict:
    """Run the full Soufflé pipeline against a graph, returning a mutation plan.

    Steps:
      1. Export the graph to Soufflé facts (temp dir).
      2. Run the compiled Datalog ruleset.
      3. Parse results; split into (certain, llm_needed).
         - certain = type renames + exact duplicates (no LLM, provable).
         - llm_needed = capped, ranked candidate relations.
      4. (Optional) Send the candidate shortlist to the LLM to validate + name.
      5. Return a mutation plan in the same shape `apply_mutations` expects:
         {"mutations": {"rename_types": [...], "merge_entities": [...],
                        "add_relations": [...]}}

    ``validator`` may be a string ("local"/"cloud"/"none") or a callable
    ``(candidates, graph) -> list[dict]`` returning validated relations with
    keys from/to/relationType (used as a test seam).

    Delegates to the stage functions (discover → rank → validate) over an
    in-memory RunContext (no disk I/O). The existing helper functions
    (run_souffle, parse_results, build_shortlist) are called by the stage
    code via the same module path, so monkeypatching those names in tests
    continues to work.
    """
    from pathlib import Path

    # Helpers are called directly (not wrapped via stage functions) to preserve
    # the test-seam monkeypatch paths at the original module level.

    # --- Phase 1: What was "discover" in the original ---
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

    # --- Phase 2: Ranking ---
    if rerank and llm_candidates:
        llm_candidates = rerank_with_semantic(llm_candidates, graph)
        try:
            from jing_meta import embed as _embed
            _embed.free()
        except Exception:
            pass
    elif llm_candidates:
        # --no-rerank is a PERFORMANCE toggle, not a correctness/precision
        # toggle. It should NOT change which candidates survive the validator's
        # sim gate (0.45 in local_validator) purely because of ranking choice.
        # The semantic path scores candidates ~0.5-1.0 for true relations, so
        # use a lexical similarity that isn't systematically lower. Calibrated
        # so MIN_SHARED (2) shared tokens => 2/3 ~= 0.67 > 0.45, meaning the
        # same candidates that a semantic rerank would keep also clear the gate
        # here. (Do not use /10: that would drop 2-4 shared-token candidates
        # that the semantic ranker would keep.)
        for c in llm_candidates:
            shared = c.get("shared", 0)
            c["similarity"] = round(min(shared / 3, 1.0), 3)

    # --- Phase 3: Validate ---
    plan: dict[str, Any] = {"mutations": {
        "rename_types": certain["rename_types"],
        "merge_entities": certain["merge_entities"],
        "add_relations": [],
        "archive_observations": [],
        "add_entities": [],
    }}

    if llm_candidates:
        if callable(validator):
            added = validator(llm_candidates, graph)
            plan["mutations"]["add_relations"] = [
                {"from": r["from"], "to": r["to"], "relationType": r["relationType"]}
                for r in added
            ]
        elif validator == "local":
            from .local_validator import validate_and_name
            health: dict[str, Any] = {}
            added = validate_and_name(llm_candidates, graph, use_local_llm=True, health=health)
            plan["mutations"]["add_relations"] = [
                {"from": r["from"], "to": r["to"], "relationType": r["relationType"]}
                for r in added
            ]
            if health:
                plan["_validator_health"] = dict(health)
        elif validator == "cloud" and validate_relations:
            from .dreamer import validate_cloud

            plan["mutations"]["add_relations"] = validate_cloud(
                llm_candidates,
                api_url=api_url,
                api_key=api_key,
                model=model,
            )

    plan["_stats"] = {
        "candidates_found": len(results["candidates"]),
        "llm_candidates_shown": len(llm_candidates),
        "certain_merges": len(certain["merge_entities"]),
        "certain_renames": len(certain["rename_types"]),
    }
    if plan.get("_validator_health"):
        plan["_stats"]["validator_health"] = plan.pop("_validator_health")
    return plan


