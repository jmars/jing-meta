"""DAFSA indexer adapter — uses the in-process Python DAFSA indexer.

Replaces shelling out to the `fst-indexer` binary. Since jing-meta ships the
Python DAFSA indexer in the `indexer` package, build/search happen in-process
with no external binary dependency. The on-disk format is identical.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from .config import DomainConfig


def _iter_domain_files(cfg: DomainConfig) -> list[Path]:
    """Return domain files/dirs, newest first."""
    root = cfg.dir
    if not root.is_dir():
        return []

    if cfg.type == "dirs":
        import fnmatch

        items = [p for p in root.iterdir() if p.is_dir()]
        items = [p for p in items if fnmatch.fnmatch(p.name, cfg.pattern)]
    else:
        items = []
        for p in root.rglob(cfg.pattern):
            if p.is_dir():
                continue
            if cfg.extensions and p.suffix.lower() not in cfg.extensions:
                continue
            items.append(p)

    return sorted(
        items, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True
    )


def _load_manifest(index_dir: Path) -> Optional[list[dict]]:
    """Load the Rust indexer's manifest.json to resolve file_idx -> filename.

    The Rust binary writes manifest.json in the same order it assigns file_idx,
    so files[N] in the manifest corresponds to file_idx=N in search results.
    """
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        return data.get("files", [])
    except (json.JSONDecodeError, OSError):
        return None


def build_index(cfg: DomainConfig, index_dir: Optional[str] = None) -> tuple[bool, str]:
    """Build a DAFSA index for a domain (in-process, Python indexer)."""
    from indexer import build as dafsa_build

    out_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir
    files = _iter_domain_files(cfg)
    if not files:
        return False, f"No files found for domain '{cfg.name}' in {cfg.dir}"

    # For "dirs" domains the content lives in per-dir files (e.g. messages.jsonl);
    # fst_pattern overrides the dir-glob so we index those files.
    fst_pattern = cfg.fst_pattern or cfg.pattern

    try:
        dafsa_build(cfg.dir.resolve(), fst_pattern, cfg.extractor, out_dir)
        return True, f"Index built for '{cfg.name}' ({len(files)} files) at {out_dir}"
    except Exception as e:  # noqa: BLE001
        return False, f"Index build error for '{cfg.name}': {e}"


def update_index(cfg: DomainConfig, index_dir: Optional[str] = None) -> tuple[bool, str]:
    """Incrementally update a DAFSA index for a domain (in-process, Python)."""
    from indexer import update as dafsa_update

    out_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir
    # For "dirs" domains the content lives in per-dir files (e.g. messages.jsonl);
    # fst_pattern overrides the dir-glob so we index those files.
    fst_pattern = cfg.fst_pattern or cfg.pattern

    try:
        result = dafsa_update(cfg.dir.resolve(), fst_pattern, cfg.extractor, out_dir)
        msg = (
            f"Index updated for '{cfg.name}': "
            f"unchanged={result['unchanged']}, updated={result['updated']}, "
            f"added={result['added']}, removed={result['removed']} "
            f"at {out_dir}"
        )
        return True, msg
    except Exception as e:  # noqa: BLE001
        return False, f"Index update error for '{cfg.name}': {e}"


def search_fst(
    cfg: DomainConfig,
    query: str,
    max_results: int = 100,
    index_dir: Optional[str] = None,
) -> Optional[list[dict]]:
    """Search via the DAFSA index (in-process). Returns list of result dicts.

    Each result: {"file_idx", "entry_idx", "_domain", "date"}.
    Returns None on failure.
    """
    from indexer import open_index as dafsa_open

    idx_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir
    idx_file = idx_dir / "index.fst"

    if not idx_file.exists():
        return None

    try:
        with dafsa_open(idx_dir) as idx:
            hits = idx.search(query, any_word=True)
            results = []
            for h in hits:
                results.append({
                    "file_idx": h.file_idx,
                    "entry_idx": h.entry_idx,
                    "_domain": cfg.name,
                    "date": idx.files[h.file_idx].date if h.file_idx < len(idx.files) else "?",
                })
            return results
    except Exception:  # noqa: BLE001
        return None


def resolve_file_idx(index_dir: Path, file_idx: int) -> Optional[str]:
    """Resolve a file_idx to an actual filename using the Rust manifest.json."""
    files = _load_manifest(index_dir)
    if files is None or file_idx < 0 or file_idx >= len(files):
        return None
    fe = files[file_idx]
    if fe.get("tombstoned") or not fe.get("filename"):
        return None
    return fe["filename"]
