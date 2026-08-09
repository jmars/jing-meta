"""DAFSA-based full-text indexer (Python frontend to the C DAFSA core).

Replicates the Rust `fst-indexer` behavior byte-for-byte (same tokenizer,
extractors, key format, and manifest shape) but with Python as the frontend to
the C DAFSA core via ctypes — no Rust toolchain required.

Key format (matches Rust exactly):
    {word}\0{file_idx:u32BE}{entry_idx:u32BE}
Stored in the DAFSA; `prefix_enum(word)` returns the 8-byte payload
(file_idx + entry_idx) for every entry containing that word.
"""

import fnmatch
import json
import os
import re
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

from jing_meta.log import get_logger
from jing_meta.mcp_base import _atomic_write, _atomic_write_json

from .dafsa import Dafsa

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tokenizer (matches Rust tokenize: split on non-alnum/non-_-/non--, lowercase,
# keep tokens of length 2..=100). Rust char::is_alphanumeric is Unicode-aware,
# so we use \w (Unicode word chars) minus the _ and - exceptions to mirror it.
# ---------------------------------------------------------------------------
_TOKEN_SPLIT = re.compile(r"[^\w\-]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    # \w includes underscore; Rust keeps '_' too. Rust keeps '-' as a token
    # char; our split on [^\w\-] preserves both. Good.
    parts = _TOKEN_SPLIT.split(text)
    out = []
    for s in parts:
        low = s.lower()
        if 2 <= len(low) <= 100:
            out.append(low)
    return out


# ---------------------------------------------------------------------------
# Extractors (match Rust extract_*)
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def date_from_path(path: Path) -> str:
    m = _DATE_RE.search(path.name)
    return m.group(0) if m else "?"


def extract_jsonl(path: Path, filename: str) -> tuple[tuple[str, str, str, str], list[str]]:
    date = date_from_path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        logger.warning("Skipping non-UTF-8 file %s: decode error", path)
        return (filename, filename, date, "jsonl"), []
    entries = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            v = json.loads(line)
            text = v.get("content") if isinstance(v, dict) else None
            entries.append(str(text) if text else line)
        except json.JSONDecodeError:
            entries.append(line)
    return (filename, filename, date, "jsonl"), entries


def extract_txt(path: Path, filename: str) -> tuple[tuple[str, str, str, str], list[str]]:
    date = date_from_path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        logger.warning("Skipping non-UTF-8 file %s: decode error", path)
        return (filename, filename, date, "txt"), []
    return (filename, filename, date, "txt"), [line for line in content.splitlines() if line.strip()]


def extract_transcript(path: Path, filename: str) -> tuple[tuple[str, str, str, str], list[str]]:
    date = date_from_path(path)
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            # Tactiq-style: "Speaker: text" — index the whole line (simplified)
            entries.append(s)
    except (UnicodeDecodeError, OSError):
        logger.warning("Skipping non-UTF-8 file %s: decode error", path)
        return (filename, filename, date, "transcript"), []
    return (filename, filename, date, "transcript"), entries


EXTRACTORS = {
    "jsonl": extract_jsonl,
    "txt": extract_txt,
    "transcript": extract_transcript,
}

# Canonical registry of DAFSA-capable extractors.  The search config's
# _VALID_EXTRACTORS is a superset that additionally allows "notification"
# for domains that don't use a DAFSA index (e.g. the notifications domain
# read via server-side bespoke functions).  search/indexer.py gracefully
# skips domains whose extractor is not in this set.


# ---------------------------------------------------------------------------
# Collect files (recursive, sorted, skip symlinked dirs)
# ---------------------------------------------------------------------------
def collect_files(dir: Path, pattern: str) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(dir):
        # prune symlinked dirs (os.walk yields root as str; wrap in Path)
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
        for name in names:
            if fnmatch.fnmatch(name, pattern):
                files.append(root_path / name)
    files.sort()
    return files


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
@dataclass
class FileEntry:
    filename: str
    title: str
    date: str
    source: str
    mtime: int = 0
    size: int = 0
    tombstoned: bool = False


# ---------------------------------------------------------------------------
# Atomic I/O and sidecar helpers
# ---------------------------------------------------------------------------

# Sidecar format (v1): a versioned, CRC32-checksummed record stream.
#     offset  size  field
#     0       4     magic     b"SIDE"
#     4       4     version   u32LE = 1
#     8       4     n_records u32LE
#     12      8*N   records   (entry_idx u32LE | word_len u32LE | word raw bytes)
#     12+8*N  4     crc32     u32LE over bytes 0 .. (12+8*N)-1
# CRC32 = stdlib zlib.crc32 (IEEE 802.3: init 0xFFFFFFFF, final XOR 0xFFFFFFFF),
# byte-compatible with the C crc32_compute used by PDWG v4.
#
# Legacy sniff: files NOT starting with b"SIDE" are treated as the old
# unversioned/unchecksummed LE stream, parsed with the same loop below.


class SidecarCorruptError(Exception):
    """Sidecar file is corrupt or has been tampered with."""


def _composite_key(word: bytes, file_idx: int, entry_idx: int) -> bytes:
    """{word}\0{file_idx:u32BE}{entry_idx:u32BE} — matches build's key bytes."""
    return word + b"\0" + file_idx.to_bytes(4, "big") + entry_idx.to_bytes(4, "big")


def _read_sidecar(slots_dir: Path, file_idx: int) -> list[tuple[int, bytes]]:
    """Parse <slots>/<file_idx>.keys and return (entry_idx, word) pairs.

    Reads the versioned v1 format (magic b"SIDE", version=1, CRC32 checksummed),
    falling back to the legacy unversioned LE stream when the file does not start
    with b"SIDE". Returns [] if the slot file is missing (tombstone).

    Raises SidecarCorruptError if the file is truncated, tampered with, or has
    an unknown version.
    """
    path = slots_dir / f"{file_idx}.keys"
    if not path.exists():
        return []
    data = path.read_bytes()

    # Legacy path: no magic header — old unversioned LE record stream.
    if data[:4] != b"SIDE":
        pairs: list[tuple[int, bytes]] = []
        i = 0
        n = len(data)
        while i < n:
            if i + 8 > n:  # not enough bytes for the header
                break
            entry_idx = int.from_bytes(data[i:i + 4], "little")
            word_len = int.from_bytes(data[i + 4:i + 8], "little")
            i += 8
            if i + word_len > n:  # partial final record
                break
            pairs.append((entry_idx, data[i:i + word_len]))
            i += word_len
        # Intentional behavior change: a trailing partial record is corruption
        # now (old silent-truncation tolerance removed).
        if i != n:
            raise SidecarCorruptError("trailing garbage in legacy sidecar")
        return pairs

    # New-format path: magic b"SIDE", version=1, CRC32 over header+records.
    if len(data) < 16:
        raise SidecarCorruptError("too short for header+CRC")
    version = int.from_bytes(data[4:8], "little")
    if version != 1:
        raise SidecarCorruptError(f"unknown version {version}")
    n_records = int.from_bytes(data[8:12], "little")
    MAX_RECORDS = 10_000_000
    if n_records > MAX_RECORDS:
        raise SidecarCorruptError("record count too large")
    expected = 12 + 8 * n_records + 4
    if len(data) < expected:
        raise SidecarCorruptError("truncated record")

    pairs = []
    i = 12
    for _ in range(n_records):
        if i + 8 > len(data):
            raise SidecarCorruptError("truncated record")
        entry_idx = int.from_bytes(data[i:i + 4], "little")
        word_len = int.from_bytes(data[i + 4:i + 8], "little")
        i += 8
        if i + word_len > len(data):
            raise SidecarCorruptError("truncated record")
        pairs.append((entry_idx, data[i:i + word_len]))
        i += word_len
    if len(pairs) != n_records:
        raise SidecarCorruptError("record count mismatch")
    if zlib.crc32(data[:-4]) != int.from_bytes(data[-4:], "little"):
        raise SidecarCorruptError("CRC mismatch")
    return pairs


def _write_sidecar(slots_dir: Path, file_idx: int, pairs: list[tuple[int, bytes]]) -> None:
    """Serialize v1 sidecar (magic b"SIDE", version=1, CRC32) to <file_idx>.keys."""
    slots_dir.mkdir(parents=True, exist_ok=True)
    header = bytearray(b"SIDE")
    header += (1).to_bytes(4, "little")
    header += len(pairs).to_bytes(4, "little")
    records = bytearray()
    for entry_idx, word in pairs:
        records += entry_idx.to_bytes(4, "little")
        records += len(word).to_bytes(4, "little")
        records += word
    body = bytes(header) + bytes(records)
    body += zlib.crc32(body).to_bytes(4, "little")
    _atomic_write(slots_dir / f"{file_idx}.keys", body)


def _dedup_pairs(extracted: list[str]) -> list[tuple[int, bytes]]:
    """Flatten entries to deduped (entry_idx, word_bytes) pairs, in order."""
    pairs: list[tuple[int, bytes]] = []
    seen: set[tuple[int, bytes]] = set()
    for entry_idx, text in enumerate(extracted):
        for word in tokenize(text):
            pair = (entry_idx, word.encode())
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def _manifest_files(output: Path) -> list[dict]:
    manifest_path = output / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data.get("files", [])
    except (OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(dir: Path, pattern: str, extractor: str, output: Path) -> None:
    """Build a DAFSA index over files in *dir* matching *pattern*."""
    fn = EXTRACTORS.get(extractor)
    if fn is None:
        raise ValueError(f"unknown extractor {extractor!r}; choices: {list(EXTRACTORS)}")

    files = collect_files(dir, pattern)
    entries: list[FileEntry] = []
    keys: set[bytes] = set()
    total_entries = 0
    file_pairs: list[list[tuple[int, bytes]]] = []

    for file_idx, filepath in enumerate(files):
        stat = filepath.stat()
        # Manifest filename = path relative to *dir* (matches update's match key
        # and the search server's `cfg.dir / fname` resolution for nested files).
        rel = str(filepath.relative_to(dir))
        meta, extracted = fn(filepath, rel)
        entries.append(
            FileEntry(*meta, mtime=stat.st_mtime_ns, size=stat.st_size, tombstoned=False)
        )
        pairs = _dedup_pairs(extracted)
        file_pairs.append(pairs)
        total_entries += len(extracted)
        for entry_idx, word in pairs:
            keys.add(_composite_key(word, file_idx, entry_idx))

    # Build DAFSA in sorted key order (required for minimality).
    with Dafsa.create() as d:
        for key in sorted(keys):
            d.add(key)

        output.mkdir(parents=True, exist_ok=True)
        fst_path = output / "index.fst"
        d.save(str(fst_path))

    slots_dir = output / "slots"
    for file_idx, pairs in enumerate(file_pairs):
        _write_sidecar(slots_dir, file_idx, pairs)

    manifest_path = output / "manifest.json"
    _atomic_write_json(manifest_path, {"files": [asdict(e) for e in entries]})

    fs = fst_path.stat().st_size if fst_path.exists() else 0
    ms = manifest_path.stat().st_size if manifest_path.exists() else 0
    logger.warning("Done. FST: %.2f MB, Manifest: %.1f KB | %d unique keys from %d entries across %d files", fs / 1_048_576, ms / 1024, len(keys), total_entries, len(files))


# ---------------------------------------------------------------------------
# Incremental update
# ---------------------------------------------------------------------------
def update(dir: Path, pattern: str, extractor: str, output: Path) -> dict:
    """Incrementally update an existing index, or build it on first run.

    Returns a summary dict with unchanged/updated/added/removed counts.
    """
    fn = EXTRACTORS.get(extractor)
    if fn is None:
        raise ValueError(f"unknown extractor {extractor!r}; choices: {list(EXTRACTORS)}")

    output = Path(output)
    fst_path = output / "index.fst"
    manifest_path = output / "manifest.json"
    slots_dir = output / "slots"

    # First run — no index yet.
    if not fst_path.exists():
        build(dir, pattern, extractor, output)
        return {
            "command": "update",
            "index_dir": str(output),
            "unchanged": 0,
            "updated": 0,
            "added": 0,
            "removed": 0,
            "total_slots": len(_manifest_files(output)),
            "first_run": True,
        }

    d = None
    try:
        d = Dafsa.load(str(fst_path), readonly=False)

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = data.get("files", [])
        live = {
            fe["filename"]: i
            for i, fe in enumerate(files)
            if not fe.get("tombstoned")
        }

        disk_files = collect_files(dir, pattern)
        disk_rel = {str(filepath.relative_to(dir)) for filepath in disk_files}

        unchanged = updated = added = removed = 0
        pending_sidecars: dict[int, list[tuple[int, bytes]]] = {}
        pending_tombstones: set[int] = set()

        # Phase 1: accumulate changes (no writes yet).
        for filepath in disk_files:
            rel = str(filepath.relative_to(dir))
            stat = filepath.stat()

            if rel in live:
                slot = live[rel]
                prev = files[slot]
                if prev.get("mtime") == stat.st_mtime_ns and prev.get("size") == stat.st_size:
                    unchanged += 1
                    continue
                # CHANGED: drop old keys, extract and re-add.
                try:
                    old_pairs = _read_sidecar(slots_dir, slot)
                except SidecarCorruptError as e:
                    logger.warning(
                        "update: corrupt sidecar for slot %d (%s); skipping key deletion — stale keys may remain",
                        slot,
                        e,
                    )
                    continue
                for ei, w in old_pairs:
                    if not d.delete(_composite_key(w, slot, ei)):
                        logger.warning("update: missing key for %s (orphan), healing", rel)
                meta, extracted = fn(filepath, rel)
                pairs = _dedup_pairs(extracted)
                for ei, w in pairs:
                    d.add(_composite_key(w, slot, ei))
                files[slot] = {
                    "filename": rel,
                    "title": meta[1],
                    "date": meta[2],
                    "source": meta[3],
                    "mtime": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "tombstoned": False,
                }
                pending_sidecars[slot] = pairs
                updated += 1
            else:
                # NEW: append at len(files) — stable file_idx, never renumber.
                slot = len(files)
                meta, extracted = fn(filepath, rel)
                pairs = _dedup_pairs(extracted)
                for ei, w in pairs:
                    d.add(_composite_key(w, slot, ei))
                files.append({
                    "filename": rel,
                    "title": meta[1],
                    "date": meta[2],
                    "source": meta[3],
                    "mtime": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "tombstoned": False,
                })
                pending_sidecars[slot] = pairs
                added += 1

        # Tombstone pass: live slots whose file is gone from disk.
        for i, fe in enumerate(files):
            if not fe.get("tombstoned") and fe["filename"] not in disk_rel:
                try:
                    old_pairs = _read_sidecar(slots_dir, i)
                except SidecarCorruptError as e:
                    logger.warning(
                        "update: corrupt sidecar for slot %d (%s); skipping key deletion — stale keys may remain",
                        i,
                        e,
                    )
                    continue
                for ei, w in old_pairs:
                    if not d.delete(_composite_key(w, i, ei)):
                        logger.warning("update: missing key for slot %d (orphan), healing", i)
                files[i] = {
                    "filename": "",
                    "title": "",
                    "date": "",
                    "source": "",
                    "mtime": 0,
                    "size": 0,
                    "tombstoned": True,
                }
                pending_tombstones.add(i)
                removed += 1

        # Phase 2: commit — DAFSA first, then sidecars, then manifest.
        d.save(str(fst_path))
        for slot, pairs in pending_sidecars.items():
            _write_sidecar(slots_dir, slot, pairs)
        for slot in pending_tombstones:
            sp = slots_dir / f"{slot}.keys"
            try:
                sp.unlink(missing_ok=True)
            except OSError:
                pass
        _atomic_write_json(manifest_path, {"files": files})

        return {
            "command": "update",
            "index_dir": str(output),
            "unchanged": unchanged,
            "updated": updated,
            "added": added,
            "removed": removed,
            "total_slots": len(files),
        }
    finally:
        if d is not None:
            d.free()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@dataclass
class Hit:
    file_idx: int
    entry_idx: int


def open_index(index_dir: Path) -> "Index":
    return Index(index_dir)


class Index:
    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        self.fst_path = self.index_dir / "index.fst"
        self.manifest_path = self.index_dir / "manifest.json"
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"No manifest.json found in {self.index_dir}")
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.files = [FileEntry(**f) for f in self.manifest.get("files", [])]
        self._view: Dafsa | None = Dafsa.load(str(self.fst_path), readonly=True)

    def close(self) -> None:
        if hasattr(self, "_view") and self._view:
            self._view.free()
            self._view = None

    def search(self, query: str, any_word: bool = False) -> list[Hit]:
        query_words = tokenize(query)
        if not query_words:
            return []
        view = self._view
        assert view is not None, "Index has been closed"
        word_sets: list[set[tuple[int, int]]] = []
        for word in query_words:
            hits: set[tuple[int, int]] = set()
            for payload in view.prefix_enum(word.encode()):
                if len(payload) == 8:
                    fi = int.from_bytes(payload[0:4], "big")
                    ei = int.from_bytes(payload[4:8], "big")
                    hits.add((fi, ei))
            if not hits and not any_word:
                return []
            if hits:
                word_sets.append(hits)
        if not word_sets:
            return []
        if any_word:
            all_hits: set[tuple[int, int]] = set()
            for ws in word_sets:
                all_hits |= ws
            return [Hit(f, e) for f, e in all_hits]
        inter: set[tuple[int, int]] = word_sets[0]
        for ws in word_sets[1:]:
            inter &= ws
        return [Hit(f, e) for f, e in inter]

    def file_name(self, file_idx: int) -> str:
        return self.files[file_idx].filename if 0 <= file_idx < len(self.files) else "?"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
