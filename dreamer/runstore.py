"""Run store — manages per-run directories under ``<memory_dir>/dreamer/<run_id>/``.

Each run directory contains:
  - ``graph.db.bak``   — byte-identical snapshot of the source DB at run start
  - ``manifest.json``   — RunManifest metadata
  - ``01_discover.json``… — one JSON file per completed stage
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jing_meta.fsutil import _atomic_write_json
from jing_meta.log import get_logger

from .contracts import (
    Mode,
    RunContext,
    RunManifest,
    Stage,
    StageResult,
    stage_filename,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class RunStore:
    """Manages dreamer run directories and their disk artifacts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- helpers ----

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _exists(self, run_id: str) -> bool:
        return self.run_dir(run_id).is_dir()

    # ---- run lifecycle ----

    def new_run_id(self) -> str:
        """Generate a unique run ID: ``YYYYMMDDTHHMMSSZ``, with ``-2``, ``-3``… suffixes on collision."""
        base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = base
        n = 2
        while self._exists(candidate):
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def create_run(
        self,
        run_id: str,
        mode: Mode,
        source_db: Path,
        max_entities: int | None = None,
    ) -> RunContext:
        """Create a new run directory, snapshot the source DB, and write the manifest.

        The DB is copied byte-for-byte — it is never opened. Returns a RunContext
        ready to feed into the stage functions.
        """
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)

        snapshot_db = run_dir / "graph.db.bak"
        # Byte-identical copy — never open the DB.
        shutil.copyfile(source_db, snapshot_db)

        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = RunManifest(
            run_id=run_id,
            mode=mode,
            source_db=str(source_db),
            created_at=created_at,
            max_entities=max_entities,
        )
        from .contracts import to_jsonable
        _atomic_write_json(run_dir / "manifest.json", to_jsonable(manifest))

        ctx = RunContext(
            run_id=run_id,
            mode=mode,
            source_db=source_db,
            snapshot_db=snapshot_db,
            run_dir=run_dir,
            store=self,
            apply=False,
            max_entities=max_entities,
            run_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        return ctx

    # ---- read ----

    def load_manifest(self, run_id: str) -> RunManifest | None:
        """Load the RunManifest for *run_id*, or None if missing."""
        import json

        path = self.run_dir(run_id) / "manifest.json"
        if not path.is_file():
            return None
        from .contracts import run_manifest_from_dict

        return run_manifest_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load_stage(self, run_id: str, stage: Stage) -> StageResult | None:
        """Load a persisted StageResult, or None if the file doesn't exist."""
        import json

        path = self.run_dir(run_id) / stage_filename(stage)
        if not path.is_file():
            return None
        from .contracts import stage_result_from_dict

        return stage_result_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load_run(self, run_id: str) -> tuple[RunManifest, dict[Stage, StageResult]]:
        """Load the manifest and all persisted stage results for a run.

        Reconciles ``manifest.completed`` against files present on disk — warns
        via the logger on mismatch (file without manifest entry or vice versa).
        """
        manifest = self.load_manifest(run_id)
        if manifest is None:
            raise FileNotFoundError(f"run {run_id!r} not found at {self.run_dir(run_id)}")

        stages: dict[Stage, StageResult] = {}
        for stage in Stage:
            result = self.load_stage(run_id, stage)
            if result is not None:
                stages[stage] = result

        # Reconcile manifest.completed vs disk
        manifest_stages: set[Stage] = set(manifest.completed.keys())
        disk_stages: set[Stage] = set(stages.keys())
        extra_disk = disk_stages - manifest_stages
        extra_manifest = manifest_stages - disk_stages
        if extra_disk:
            logger.warning(
                "run %s: stage files on disk but not in manifest: %s",
                run_id, sorted(s.value for s in extra_disk),
            )
        if extra_manifest:
            logger.warning(
                "run %s: manifest records completed stages with no file: %s",
                run_id, sorted(s.value for s in extra_manifest),
            )

        return manifest, stages

    def list_runs(self) -> list[str]:
        """Return run IDs sorted newest-first (descending by name)."""
        if not self.root.is_dir():
            return []
        ids = [d.name for d in self.root.iterdir() if d.is_dir()]
        ids.sort(reverse=True)
        return ids

    # ---- write ----

    def write_stage(self, ctx: RunContext, result: StageResult) -> None:
        """Persist *result* atomically and update the manifest's completed map."""
        from .contracts import RunManifest, to_jsonable

        run_dir = self.run_dir(ctx.run_id)
        stage_file = run_dir / stage_filename(result.stage)

        _atomic_write_json(stage_file, to_jsonable(result))

        # Update manifest
        manifest_path = run_dir / "manifest.json"
        manifest = self.load_manifest(ctx.run_id)
        if manifest is None:
            manifest = RunManifest(
                run_id=ctx.run_id,
                mode=ctx.mode,
                source_db=str(ctx.source_db),
                created_at=ctx.run_date or "",
                max_entities=ctx.max_entities,
            )
        manifest.completed[result.stage] = result.created_at
        _atomic_write_json(manifest_path, to_jsonable(manifest))

    @staticmethod
    def reconstitute_ctx(
        store: RunStore,
        manifest: RunManifest,
        *,
        apply: bool = False,
        api_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        rerank: bool = True,
        validator: Any = "local",
        max_entities: int | None = None,
    ) -> RunContext:
        """Build a RunContext from an existing manifest (for replay)."""
        from datetime import datetime, timezone

        run_dir = store.run_dir(manifest.run_id)
        return RunContext(
            run_id=manifest.run_id,
            mode=manifest.mode,
            source_db=Path(manifest.source_db),
            snapshot_db=run_dir / "graph.db.bak",
            run_dir=run_dir,
            store=store,
            apply=apply,
            max_entities=max_entities or manifest.max_entities,
            api_url=api_url,
            api_key=api_key,
            model=model,
            rerank=rerank,
            validator=validator,
            run_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
