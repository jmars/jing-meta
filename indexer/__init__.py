"""DAFSA-based full-text indexer (Python frontend to the C DAFSA core).

Replicates the Rust `fst-indexer` behavior byte-for-byte (same tokenizer,
extractors, key format, and manifest shape) but with Python as the frontend to
the C DAFSA core via ctypes — no Rust toolchain required.

Key format (matches Rust exactly):
    {word}\0{file_idx:u32BE}{entry_idx:u32BE}
Stored in the DAFSA; `prefix_enum(word)` returns the 8-byte payload
(file_idx + entry_idx) for every entry containing that word.
"""

import glob
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from .dafsa import Dafsa


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


def extract_jsonl(path: Path, filename: str):
    date = date_from_path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        print(f"Skipping non-UTF-8 file {path}: decode error", file=sys.stderr)
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


def extract_txt(path: Path, filename: str):
    date = date_from_path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        print(f"Skipping non-UTF-8 file {path}: decode error", file=sys.stderr)
        return (filename, filename, date, "txt"), []
    return (filename, filename, date, "txt"), [l for l in content.splitlines() if l.strip()]


def extract_transcript(path: Path, filename: str):
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
        print(f"Skipping non-UTF-8 file {path}: decode error", file=sys.stderr)
        return (filename, filename, date, "transcript"), []
    return (filename, filename, date, "transcript"), entries


EXTRACTORS = {
    "jsonl": extract_jsonl,
    "txt": extract_txt,
    "transcript": extract_transcript,
}


# ---------------------------------------------------------------------------
# Collect files (recursive, sorted, skip symlinked dirs)
# ---------------------------------------------------------------------------
def collect_files(dir: Path, pattern: str) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(dir):
        # prune symlinked dirs
        dirs[:] = [d for d in dirs if not (root / d).is_symlink()]
        for name in names:
            if glob.fnmatch.fnmatch(name, pattern):
                files.append(Path(root) / name)
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


def build(dir: Path, pattern: str, extractor: str, output: Path) -> None:
    """Build a DAFSA index over files in *dir* matching *pattern*."""
    fn = EXTRACTORS.get(extractor)
    if fn is None:
        raise ValueError(f"unknown extractor {extractor!r}; choices: {list(EXTRACTORS)}")

    files = collect_files(dir, pattern)
    entries: list[FileEntry] = []
    keys: set[bytes] = set()
    total_entries = 0

    for file_idx, filepath in enumerate(files):
        meta, extracted = fn(filepath, filepath.name)
        entries.append(FileEntry(*meta))
        for entry_idx, text in enumerate(extracted):
            total_entries += 1
            for word in tokenize(text):
                k = word.encode() + b"\0" + file_idx.to_bytes(4, "big") + entry_idx.to_bytes(4, "big")
                keys.add(k)

    # Build DAFSA in sorted key order (required for minimality).
    with Dafsa.create() as d:
        for key in sorted(keys):
            d.add(key)

        output.mkdir(parents=True, exist_ok=True)
        fst_path = output / "index.fst"
        d.save(str(fst_path))

    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps({"files": [asdict(e) for e in entries]}, indent=2),
        encoding="utf-8",
    )

    fs = fst_path.stat().st_size if fst_path.exists() else 0
    ms = manifest_path.stat().st_size if manifest_path.exists() else 0
    print(
        f"Done. FST: {fs / 1_048_576:.2f} MB, Manifest: {ms / 1024:.1f} KB | "
        f"{len(keys)} unique keys from {total_entries} entries across {len(files)} files",
        file=sys.stderr,
    )


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
        self._view = Dafsa.load(str(self.fst_path), readonly=True)

    def close(self) -> None:
        if hasattr(self, "_view") and self._view:
            self._view.free()
            self._view = None

    def search(self, query: str, any_word: bool = False) -> list[Hit]:
        query_words = tokenize(query)
        if not query_words:
            return []
        word_sets: list[set[tuple[int, int]]] = []
        for word in query_words:
            hits: set[tuple[int, int]] = set()
            for payload in self._view.prefix_enum(word.encode()):
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
