"""DAFSA indexer adapter — uses the in-process Python DAFSA indexer.

Replaces shelling out to the `fst-indexer` binary. Since jing-meta ships the
Python DAFSA indexer in the `indexer` package, build/search happen in-process
with no external binary dependency. The on-disk format is identical.
"""

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from indexer import EXTRACTORS as DAFSA_EXTRACTORS

from .config import DomainConfig

if TYPE_CHECKING:
    from indexer import Index

# Cache of open Index objects keyed by (dir, fst_mtime_ns, wal_size). Unchanged
# indexes are reused across search_fst calls; a changed index (update/rebuild/
# compact) evicts the stale entry and opens a fresh one. Guarded by a lock so
# concurrent searches can't race on eviction.
_index_cache: dict[tuple, "Index"] = {}
_index_cache_lock = threading.Lock()


def _get_cached_index(idx_dir: Path) -> "Index":
    """Return a cached Index for *idx_dir*, reopening it when the index changed.

    The cache key is (dir, index.fst mtime_ns, index.wal size). Missing files
    contribute 0, so a brand-new or not-yet-built index is handled uniformly.
    """
    from indexer import Index

    fst = idx_dir / "index.fst"
    wal = idx_dir / "index.wal"
    fst_mtime = fst.stat().st_mtime_ns if fst.exists() else 0
    wal_size = wal.stat().st_size if wal.exists() else 0
    key = (str(idx_dir), fst_mtime, wal_size)

    with _index_cache_lock:
        cached = _index_cache.get(key)
        if cached is not None:
            return cached
        # Evict and close stale entries for this dir (different key).
        for k, idx in list(_index_cache.items()):
            if k[0] == str(idx_dir) and k != key:
                try:
                    idx.close()
                except Exception:  # noqa: BLE001
                    pass
                del _index_cache[k]
        idx = Index(idx_dir)
        _index_cache[key] = idx
        return idx


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
    if cfg.extractor not in DAFSA_EXTRACTORS:
        from jing_meta.log import get_logger
        logger = get_logger(__name__)
        logger.warning(
            "Skipping DAFSA index build for domain %r: extractor %r is not in "
            "indexer.EXTRACTORS (%s)",
            cfg.name, cfg.extractor, ", ".join(sorted(DAFSA_EXTRACTORS)),
        )
        return True, f"skipped (extractor {cfg.extractor!r} not supported by DAFSA indexer)"

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
    if cfg.extractor not in DAFSA_EXTRACTORS:
        from jing_meta.log import get_logger
        logger = get_logger(__name__)
        logger.warning(
            "Skipping DAFSA index update for domain %r: extractor %r is not in "
            "indexer.EXTRACTORS (%s)",
            cfg.name, cfg.extractor, ", ".join(sorted(DAFSA_EXTRACTORS)),
        )
        return True, f"skipped (extractor {cfg.extractor!r} not supported by DAFSA indexer)"

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
    any_word: bool = True,
) -> Optional[list[dict]]:
    """Search via the DAFSA index (in-process). Returns list of result dicts.

    Each result: {"file_idx", "entry_idx", "_domain", "date"}.
    Returns None on failure.
    """
    idx_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir
    idx_file = idx_dir / "index.fst"

    if not idx_file.exists():
        return None

    try:
        idx = _get_cached_index(idx_dir)
        hits = idx.search(query, any_word=any_word)
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
