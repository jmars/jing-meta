"""DAFSA indexer adapter — uses the in-process Python DAFSA indexer.

Replaces shelling out to the `fst-indexer` binary. Since jing-meta ships the
Python DAFSA indexer in the `indexer` package, build/search happen in-process
with no external binary dependency. The on-disk format is identical.
"""

import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from indexer import EXTRACTORS as DAFSA_EXTRACTORS

from .config import DomainConfig

if TYPE_CHECKING:
    from indexer import Index

# ---------------------------------------------------------------------------
# Refcounted open-Index cache.
#
# Cache of open Index objects keyed by (dir, commit_seq). The commit_seq is
# a monotonically-increasing integer in manifest.json, bumped on every
# update()/compact()/build() commit — a stronger consistency signal than
# (fst_mtime_ns, wal_size).  A reader briefly takes a shared flock on
# index.lock for the stat+open window, then re-checks commit_seq after opening
# to catch the narrow window where a writer committed mid-snapshot.
#
# Each cached value is a `_CacheEntry` wrapping an Index (which holds an mmap'd
# DAFSA view + optional WAL overlay). Two invariants keep long-running servers
# from leaking mmaps and prevent use-after-free under a thread pool:
#
#   * Refcount: every lookup increments `_CacheEntry.refcount`; the caller
#     releases it with `_release_entry` when done. An entry is only closed
#     (view freed) once its refcount is 0, so an in-flight reader never
#     observes a freed view — the `assert view is not None` path in the
#     indexer is unreachable for any reader that holds a reference.
#
#   * Bounded + TTL eviction: entries idle past `_CACHE_TTL_SECONDS` or beyond
#     `_CACHE_MAX_ENTRIES` (LRU) are retired and closed. Retirement removes an
#     entry from the dict immediately (bounded cache size), but if a reader
#     still references it the actual close is deferred until its refcount
#     drops to 0.
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 300.0  # close idle indexes after this many idle seconds
_CACHE_MAX_ENTRIES = 32  # LRU cap on simultaneously-open indexes

_index_cache: dict[tuple, "_CacheEntry"] = {}
# Entries retired from the dict but still referenced by an in-flight reader.
# Swept (closed) once their refcount drops to 0.
_retired: list["_CacheEntry"] = []
_index_cache_lock = threading.Lock()


class _CacheEntry:
    """Refcounted wrapper around an open Index.

    `index` may be None after the entry has been closed. `refcount` counts
    in-flight readers; `retired` marks an entry removed from `_index_cache`
    that is awaiting final close. `last_used` is a monotonic clock timestamp
    for LRU/TTL eviction.

    All other attribute access delegates to the wrapped Index so callers can
    treat an entry like the Index itself (`.search`, `.files`, ...).
    """

    __slots__ = ("index", "refcount", "last_used", "retired")

    def __init__(self, index: "Index", now: float):
        self.index = index
        self.refcount = 1
        self.last_used = now
        self.retired = False

    def __getattr__(self, name):
        # __getattr__ is only called for attributes not found via __slots__,
        # so this cleanly delegates search/files/etc. to the wrapped Index.
        return getattr(self.index, name)


def _retire(entry: "_CacheEntry", now: float) -> None:
    """Retire *entry* (already removed from `_index_cache`).

    Close it immediately if nothing references it; otherwise defer the close
    to `_release_entry`/`_sweep_retired` once its refcount drops to 0.
    """
    entry.retired = True
    if entry.refcount == 0:
        try:
            entry.close()
        except Exception:  # noqa: BLE001
            pass
    else:
        _retired.append(entry)


def _close_entry(entry: "_CacheEntry") -> None:
    try:
        entry.close()
    except Exception:  # noqa: BLE001
        pass


def _sweep_retired() -> None:
    """Close retired entries whose refcount has dropped to 0. Caller holds lock."""
    keep: list["_CacheEntry"] = []
    for entry in _retired:
        if entry.refcount == 0:
            _close_entry(entry)
        else:
            keep.append(entry)
    _retired[:] = keep


def _evict_lru(now: float) -> None:
    """Apply the TTL and size bound (LRU). Caller holds the lock.

    Only evicts entries with no in-flight readers; a referenced (active)
    entry is left for `_sweep_retired`/`_release_entry` to close later.
    """
    if len(_index_cache) > _CACHE_MAX_ENTRIES:
        excess = len(_index_cache) - _CACHE_MAX_ENTRIES
        candidates = sorted(
            ((k, e) for k, e in _index_cache.items() if e.refcount == 0),
            key=lambda kv: kv[1].last_used,
        )
        for k, e in candidates[:excess]:
            del _index_cache[k]
            _retire(e, now)
    for k in [
        k
        for k, e in _index_cache.items()
        if e.refcount == 0 and (now - e.last_used) > _CACHE_TTL_SECONDS
    ]:
        e = _index_cache.pop(k)
        _retire(e, now)


def _release_entry(entry: "_CacheEntry") -> None:
    """Release a reference previously taken by `_get_cached_index`.

    If the entry was retired while referenced, this is what finally closes it
    (freeing its mmap) once the last reader is done.
    """
    with _index_cache_lock:
        if entry.refcount <= 0:
            return  # already fully released — never go negative
        entry.refcount -= 1
        if entry.retired and entry.refcount == 0:
            _close_entry(entry)
            try:
                _retired.remove(entry)
            except ValueError:
                pass


def _read_commit_seq(idx_dir: Path) -> int:
    """Return the commit_seq from the manifest, or -1 if absent/unreadable."""
    from indexer import _read_commit_seq as _rcs
    return _rcs(idx_dir)


@contextlib.contextmanager
def _index_lock_shared(idx_dir: Path):
    """Acquire a shared flock on index.lock for consistent snapshot reads.

    Does NOT create the lock file — that is the writer's job.  If the lock
    file is absent, there are no concurrent writers and we proceed unlocked.
    """
    lock_path = idx_dir / "index.lock"
    if not lock_path.exists():
        yield
        return
    fd = os.open(str(lock_path), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _get_cached_index(idx_dir: Path) -> "_CacheEntry":
    """Return a refcounted cached Index wrapper for *idx_dir*.

    The cache key is (dir, commit_seq).  A brief shared flock on index.lock
    ensures we read a consistent snapshot (manifest + WAL + FST).  After
    opening the layered view we re-check commit_seq to catch the narrow window
    where a writer committed between our stat and open.

    The returned entry holds a reference; callers MUST pair this with
    `_release_entry(entry)` when they are done using the Index.
    """
    from indexer import Index

    # Bounded retry on writer contention: if a writer commits faster than we
    # can open the index, bail out rather than spinning indefinitely.
    max_retries = 5
    for _ in range(max_retries):
        # Acquire a shared lock for the stat+open window — keeps the writer
        # (which holds LOCK_EX) from committing mid-snapshot.
        with _index_lock_shared(idx_dir):
            seq = _read_commit_seq(idx_dir)
            key = (str(idx_dir), seq)
            now = time.monotonic()

            with _index_cache_lock:
                _sweep_retired()
                cached = _index_cache.get(key)
                if cached is not None:
                    cached.refcount += 1
                    cached.last_used = now
                    _evict_lru(now)
                    return cached
                # Retire stale entries for this dir (different key). These are
                # removed from the dict immediately so the cache can't pile up
                # old generations; the actual close is deferred if a reader
                # still references one.
                for k, entry in list(_index_cache.items()):
                    if k[0] == str(idx_dir) and k != key:
                        del _index_cache[k]
                        _retire(entry, now)

            entry = _CacheEntry(Index(idx_dir), now)

            # Re-check commit_seq after opening. If a writer committed during
            # our open, seq2 will differ from seq and we retry.
            seq2 = _read_commit_seq(idx_dir)
            if seq2 != seq:
                entry.close()
                time.sleep(0.01)  # brief backoff before retrying
                continue  # retry with the new seq

            with _index_cache_lock:
                _index_cache[key] = entry
                _evict_lru(now)
            return entry

    # Too much writer churn to get a consistent snapshot.
    raise RuntimeError(f"index at {idx_dir} is changing too quickly to open a consistent view")


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
        entry = _get_cached_index(idx_dir)
        try:
            hits = entry.search(query, any_word=any_word)
            results = []
            for h in hits:
                results.append(
                    {
                        "file_idx": h.file_idx,
                        "entry_idx": h.entry_idx,
                        "_domain": cfg.name,
                        "date": entry.files[h.file_idx].date
                        if h.file_idx < len(entry.files)
                        else "?",
                    }
                )
            return results
        finally:
            _release_entry(entry)
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
