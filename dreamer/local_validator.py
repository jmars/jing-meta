"""Offline relation validator — deterministic rules first, local LLM fallback.

Fully offline relation validation/naming for the gardening pipeline.

Two tiers, cheapest first:
  1. DETERMINISTIC RULES: infer the relation type from entity type-patterns and
     confidence signals. Precise and free. Handles the easy ~70%.
  2. LOCAL LLM (Ollama): name the remaining ambiguous pairs with a small Q4
     model (qwen2.5:3b by default). Fast (~1s/pair), zero network, private.

The cloud LLM is NOT needed. Everything runs on the machine.
"""

import os
from typing import Optional

from jing_meta import config as _config

# Local LLM config (shared with the rest of jing-meta)
OLLAMA_URL = os.environ.get("OLLAMA_URL", _config.OLLAMA_URL)
LOCAL_MODEL = os.environ.get("LOCAL_LLM_MODEL", _config.LOCAL_LLM_MODEL)

# Confidence signals from the semantic tier (candidate.similarity, 0-1).
# Pairs above this are treated as certain relations (rules suffice).
HIGH_SIM = float(os.environ.get("VALIDATOR_HIGH_SIM", "0.70"))


# ---------------------------------------------------------------------------
# Tier 1: deterministic rules
# ---------------------------------------------------------------------------

# (from_type_pattern, to_type_pattern) -> relation type
# Entity types are lowercase (after Soufflé consolidation).
TYPE_RULES: list[tuple[tuple[str, str], str]] = [
    (("plan", "implementation"), "implements"),
    (("plan", "refactor"), "implements"),
    (("design-decision", "implementation"), "implements"),
    (("bug", "fix"), "fixes"),
    (("bug", "bugfix"), "fixes"),
    (("finding", "fix"), "fixes"),
    (("test", "code-change"), "tests"),
    (("test", "implementation"), "tests"),
    (("implementation", "test"), "tested_by"),
    (("file", "test"), "contains"),
    (("change", "implementation"), "implements"),
    (("milestone", "plan"), "contains"),
]


def _lookup_type(graph: dict, name: str) -> Optional[str]:
    """Return an entity's type by name (lowercased)."""
    for e in graph["entities"]:
        if e["name"] == name:
            return (e.get("entityType") or "unknown").lower()
    return None


def _rule_relation(from_type: Optional[str], to_type: Optional[str]) -> Optional[str]:
    """Try the type-pattern rules for a relation name."""
    if not from_type or not to_type:
        return None
    for (fa, ta), rel in TYPE_RULES:
        if fa == from_type and ta == to_type:
            return rel
    return None


def _shared_token_relation(a: str, b: str, graph: dict) -> Optional[str]:
    """If one entity's name appears in the other's observations, it's related."""
    def name_in_obs(name, other_name):
        for e in graph["entities"]:
            if e["name"] == other_name:
                for o in e.get("observations", []):
                    if name and name.lower() in str(o).lower():
                        return True
        return False

    if name_in_obs(a, b) or name_in_obs(b, a):
        return "references"
    return None


# ---------------------------------------------------------------------------
# Tier 2: local LLM (Ollama)
# ---------------------------------------------------------------------------


def _local_llm_relation(a: str, b: str) -> Optional[str]:
    """Ask the local Ollama model to name the relation. Returns None on failure."""
    import requests

    prompt = (
        "You are a knowledge graph relation validator. Two entities are given. "
        "Decide the relation type connecting them. Choose ONLY from: "
        "implements, part_of, related_to, tested_by, fixes, depends_on, uses, "
        "references, causes, verified_by, contains, supersedes. "
        f"Entities:\nA: {a}\nB: {b}\n"
        "Reply with ONLY the single relation type, lowercase, nothing else."
    )
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LOCAL_MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.1, "num_predict": 20},
            },
            timeout=60,
        )
        resp = (r.json().get("response") or "").strip().lower()
        return resp.split()[0] if resp else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_and_name(
    candidates: list[dict],
    graph: dict,
    *,
    use_local_llm: bool = True,
) -> list[dict]:
    """Validate + name candidate relations.

    Returns a list of {"from", "to", "relationType", "confidence"}.
    High-similarity pairs are accepted; low-similarity pairs are dropped unless
    rules or the LLM clearly relate them.
    """
    out = []
    for c in candidates:
        a, b = c["from"], c["to"]
        sim = c.get("similarity", 0.0)

        # Drop clearly-weak candidates (not confidently related).
        if sim < 0.45 and not _shared_token_relation(a, b, graph):
            continue

        # 1. Rules first (precise, free).
        from_type = _lookup_type(graph, a)
        to_type = _lookup_type(graph, b)
        rel = _rule_relation(from_type, to_type)
        if rel is None:
            rel = _shared_token_relation(a, b, graph)

        # 2. Local LLM for the rest.
        if rel is None and use_local_llm:
            rel = _local_llm_relation(a, b)

        if rel is None:
            rel = "related_to"  # safe default

        out.append({"from": a, "to": b, "relationType": rel, "confidence": round(sim, 3)})
    return out
