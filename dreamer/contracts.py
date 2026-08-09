"""Typed data contracts for the dreamer stages pipeline.

Provides dataclasses and enums that define the stage protocol, disk format,
and payload shapes.  Pure stdlib — zero new dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Mode(str, Enum):
    LLM = "llm"
    SOUFFLE = "souffle"


class Stage(str, Enum):
    DISCOVER = "discover"
    RANK = "rank"
    VALIDATE = "validate"
    APPLY = "apply"


STAGE_ORDER: tuple[Stage, ...] = (Stage.DISCOVER, Stage.RANK, Stage.VALIDATE, Stage.APPLY)


def stages_after(stage: Stage) -> tuple[Stage, ...]:
    """Return all stages after *stage* (inclusive)."""
    try:
        idx = STAGE_ORDER.index(stage)
    except ValueError:
        return STAGE_ORDER
    return STAGE_ORDER[idx:]


def stage_filename(stage: Stage) -> str:
    """Return the on-disk filename for a stage result, e.g. '01_discover.json'."""
    idx = STAGE_ORDER.index(stage) + 1
    return f"{idx:02d}_{stage.value}.json"


# ---------------------------------------------------------------------------
# Candidate dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A raw candidate relation pair (pre-ranking)."""
    from_: str = field(metadata={"json": "from"})
    to: str
    signals: list[str] | None = None
    shared: int | None = None
    similarity: float | None = None


@dataclass(frozen=True)
class RankedCandidate:
    """A candidate after ranking/scoring."""
    from_: str = field(metadata={"json": "from"})
    to: str
    score: float
    signals: list[str] = field(default_factory=list)
    shared: int = 0
    similarity: float = 0.0


@dataclass(frozen=True)
class ValidatedRelation:
    """A validated relation ready to apply."""
    from_: str = field(metadata={"json": "from"})
    to: str
    relation_type: str = field(metadata={"json": "relationType"})
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# MutationPlan
# ---------------------------------------------------------------------------


@dataclass
class MutationPlan:
    """The mutation plan assembled by validate and consumed by apply."""
    archive_observations: list[dict] = field(default_factory=list)
    rename_types: list[dict] = field(default_factory=list)
    merge_entities: list[dict] = field(default_factory=list)
    add_entities: list[dict] = field(default_factory=list)
    add_relations: list[dict] = field(default_factory=list)
    summary: str = ""
    stats: dict[str, Any] = field(default_factory=dict)

    def to_legacy_dict(self) -> dict:
        """Return the legacy ``{"mutations": {...}, "summary":..., "_stats":...}`` dict."""
        d: dict[str, Any] = {
            "mutations": {
                "archive_observations": list(self.archive_observations),
                "rename_types": list(self.rename_types),
                "merge_entities": list(self.merge_entities),
                "add_entities": list(self.add_entities),
                "add_relations": list(self.add_relations),
            },
        }
        if self.summary:
            d["summary"] = self.summary
        d["_stats"] = dict(self.stats)
        return d

    @classmethod
    def from_legacy_dict(cls, d: dict) -> MutationPlan:
        """Parse a legacy ``{"mutations":{...}}`` dict into a MutationPlan."""
        muts = d.get("mutations", {}) or {}
        return cls(
            archive_observations=list(muts.get("archive_observations", []) or []),
            rename_types=list(muts.get("rename_types", []) or []),
            merge_entities=list(muts.get("merge_entities", []) or []),
            add_entities=list(muts.get("add_entities", []) or []),
            add_relations=list(muts.get("add_relations", []) or []),
            summary=str(d.get("summary", "") or ""),
            stats=dict(d.get("_stats", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Stage result & manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageResult:
    """The output of a single stage, persisted as a JSON file on disk."""
    stage: Stage
    run_id: str
    mode: Mode
    created_at: str
    version: int = 1
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunManifest:
    """Metadata about a single dreamer run, persisted as manifest.json."""
    run_id: str
    mode: Mode
    version: int = 1
    source_db: str = ""
    created_at: str = ""
    max_entities: int | None = None
    completed: dict[Stage, str] = field(default_factory=dict)


@dataclass
class RunContext:
    """Immutable-ish context passed through every stage function."""
    run_id: str
    mode: Mode
    source_db: Path
    snapshot_db: Path
    run_dir: Path
    store: Any  # RunStore | None
    apply: bool
    max_entities: int | None = None
    api_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    rerank: bool = True
    validator: Any = "local"
    chunk_size: int = 100
    run_date: str | None = None


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dataclass_to_dict(obj: Any) -> dict:
    """Convert a dataclass instance to a plain dict, honouring ``metadata={"json":...}`` renames."""
    result: dict[str, Any] = {}
    for f in fields(obj):
        key = f.metadata.get("json", f.name)
        val = getattr(obj, f.name)
        result[key] = to_jsonable(val)
    return result


def to_jsonable(obj: Any) -> Any:
    """Recursively convert *obj* to a JSON-serialisable value.

    - ``Enum`` → ``str.value``
    - ``Path`` → ``str``
    - ``dataclass`` → ``dict`` (field rename via ``metadata={"json":...}``)
    - ``list``/``tuple`` → ``list`` of converted values
    - ``dict`` → ``dict`` with values converted
    - everything else passes through unchanged.
    """
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return _dataclass_to_dict(obj)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            key: str = k.value if isinstance(k, Enum) else str(k)
            result[key] = to_jsonable(v)
        return result
    return obj


# ---------------------------------------------------------------------------
# Deserialisation helpers
# ---------------------------------------------------------------------------


def _rename_key(d: dict, old: str, new: str) -> None:
    """Map a key in a dict from *old* to *new* in place, if present."""
    if old in d and new not in d:
        d[new] = d.pop(old)


def stage_result_from_dict(d: dict) -> StageResult:
    """Parse a dict (from JSON) into a StageResult."""
    _rename_key(d, "from", "from_")
    return StageResult(
        stage=Stage(d["stage"]),
        run_id=d["run_id"],
        mode=Mode(d["mode"]),
        created_at=d["created_at"],
        version=d.get("version", 1),
        payload=d.get("payload", {}),
    )


def run_manifest_from_dict(d: dict) -> RunManifest:
    """Parse a dict (from JSON) into a RunManifest."""
    completed_raw: dict = d.get("completed", {}) or {}
    completed: dict[Stage, str] = {}
    for k, v in completed_raw.items():
        try:
            completed[Stage(k)] = str(v)
        except ValueError:
            completed[Stage(k)] = str(v)  # best-effort for unknown stages
    return RunManifest(
        run_id=d["run_id"],
        mode=Mode(d["mode"]),
        version=d.get("version", 1),
        source_db=str(d.get("source_db", "")),
        created_at=str(d.get("created_at", "")),
        max_entities=d.get("max_entities"),
        completed=completed,
    )


def mutation_plan_from_dict(d: dict) -> MutationPlan:
    """Parse a dict into a MutationPlan, accepting both legacy and dataclass shapes."""
    # Accept both "mutations" (legacy) and the flat keys (dataclass shape).
    if "mutations" in d and isinstance(d["mutations"], dict):
        return MutationPlan.from_legacy_dict(d)
    _rename_key(d, "from", "from_")
    return MutationPlan(
        archive_observations=d.get("archive_observations", []) or [],
        rename_types=d.get("rename_types", []) or [],
        merge_entities=d.get("merge_entities", []) or [],
        add_entities=d.get("add_entities", []) or [],
        add_relations=d.get("add_relations", []) or [],
        summary=d.get("summary", "") or "",
        stats=d.get("stats", {}) or {},
    )
