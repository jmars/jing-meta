"""Byte-parity test: C dafsa-cli build must produce identical output to Python.

Builds a small ASCII JSONL corpus with both the C ``dafsa-cli build``
subcommand and the Python ``_build_locked`` path, then asserts
byte-identical ``index.fst``, sidecars, and ``manifest.json``, and that
search returns identical hits from both indexes.

Skips if the binary lacks the ``build`` subcommand (graceful fallback).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# Skip cleanly when the C shared library is not built.
try:
    from indexer.dafsa import _get_lib
    _get_lib()
except (RuntimeError, AttributeError, OSError) as e:
    pytest.skip(f"libdafsa.so not available: {e}", allow_module_level=True)

from indexer import _build_locked, open_index


def _resolve_cli():
    """Resolve dafsa-cli binary path."""
    import shutil
    env = os.environ.get("JING_DAFSA_CLI")
    if env and Path(env).is_file():
        return env
    in_tree = Path(__file__).parent.parent / "indexer" / "dafsa" / "dafsa-cli"
    if in_tree.is_file():
        return str(in_tree)
    found = shutil.which("dafsa-cli")
    if found:
        return found
    pytest.skip("dafsa-cli binary not found")


def _c_build_has_subcommand(cli):
    """Return True if dafsa-cli supports the ``build`` subcommand."""
    try:
        cp = subprocess.run(
            [cli, "build", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        return cp.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="module")
def cli_binary():
    cli = _resolve_cli()
    if not _c_build_has_subcommand(cli):
        pytest.skip("dafsa-cli lacks build subcommand")
    return cli


def _corpus_dir(tmp_path):
    """Create a small ASCII JSONL corpus with nested dirs and a date-named file."""
    data = tmp_path / "corpus"
    data.mkdir()
    (data / "a.jsonl").write_text('{"content": "alpha beta gamma"}\n', encoding="utf-8")
    (data / "b.jsonl").write_text(
        '{"content": "beta delta epsilon"}\n{"content": "alpha mu"}\n',
        encoding="utf-8",
    )
    sub = data / "sub"
    sub.mkdir()
    (sub / "2024-03-15.jsonl").write_text(
        '{"content": "zeta eta theta"}\n', encoding="utf-8"
    )
    (sub / "nested.jsonl").write_text(
        '{"content": "iota kappa lambda"}\n', encoding="utf-8"
    )
    return data


def _cmpdirs(a: Path, b: Path) -> list[str]:
    """Recursively compare two directories; return list of diffs."""
    diffs = []
    aa = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    bb = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    if [str(x) for x in aa] != [str(x) for x in bb]:
        diffs.append(f"file list differs: {aa} vs {bb}")
        return diffs
    for rel in aa:
        fa = a / rel
        fb = b / rel
        if fa.read_bytes() != fb.read_bytes():
            diffs.append(f"{rel} differs")
    return diffs


def _search_hits(index_dir: Path, query: str):
    with open_index(index_dir) as idx:
        hits = idx.search(query)
        return {(h.file_idx, h.entry_idx, idx.file_name(h.file_idx)) for h in hits}


def test_byte_parity_jsonl(tmp_path, cli_binary):
    """C ``dafsa-cli build`` must produce byte-identical output to Python."""
    corpus = _corpus_dir(tmp_path)
    py_out = tmp_path / "py-out"
    c_out = tmp_path / "c-out"

    # Python build
    with pytest.MonkeyPatch.context() as mp:
        # Force Python path (not C dispatch) for this test
        mp.setattr("indexer._c_build_available", lambda: False)
        from indexer import build as py_build
        py_build(corpus, "*.jsonl", "jsonl", py_out)

    # C build
    cp = subprocess.run(
        [cli_binary, "build",
         "--dir", str(corpus),
         "--pattern", "*.jsonl",
         "--output", str(c_out)],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, f"dafsa-cli build failed: {cp.stderr}"

    # Byte-parity: all files identical
    diffs = _cmpdirs(py_out, c_out)
    assert not diffs, f"Byte-parity diffs: {diffs}"

    # Search parity: identical hits from both indexes
    queries = ["alpha", "beta", "zeta", "iota", "mu", "nonexistent"]
    for q in queries:
        py_hits = _search_hits(py_out, q)
        c_hits = _search_hits(c_out, q)
        assert py_hits == c_hits, f"Search parity failed for '{q}': py={py_hits} c={c_hits}"


def test_manifest_json_structure(tmp_path, cli_binary):
    """Manifest from C build must match Python json.dumps format exactly."""
    corpus = _corpus_dir(tmp_path)
    c_out = tmp_path / "c-out"

    cp = subprocess.run(
        [cli_binary, "build",
         "--dir", str(corpus),
         "--pattern", "*.jsonl",
         "--output", str(c_out)],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0

    man = json.loads((c_out / "manifest.json").read_text(encoding="utf-8"))
    assert "files" in man
    assert man["commit_seq"] == 0
    files = man["files"]
    assert len(files) == 4

    # Check fields present in dataclass order
    expected_keys = ["filename", "title", "date", "source", "mtime", "size", "tombstoned"]
    for fe in files:
        assert list(fe.keys()) == expected_keys, f"Key order mismatch: {list(fe.keys())}"
        assert fe["tombstoned"] is False
        assert fe["source"] == "jsonl"
        assert isinstance(fe["mtime"], int) and fe["mtime"] > 0
        assert isinstance(fe["size"], int) and fe["size"] > 0
        assert fe["filename"] == fe["title"]

    # Date-named file should have extracted date
    dates = {fe["filename"]: fe["date"] for fe in files}
    assert dates["sub/2024-03-15.jsonl"] == "2024-03-15"


def test_empty_corpus(tmp_path, cli_binary):
    """C build on an empty directory should succeed and produce empty FST."""
    empty = tmp_path / "empty"
    empty.mkdir()
    c_out = tmp_path / "c-out"

    cp = subprocess.run(
        [cli_binary, "build",
         "--dir", str(empty),
         "--pattern", "*.jsonl",
         "--output", str(c_out)],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0
    assert (c_out / "index.fst").exists()
    assert (c_out / "manifest.json").exists()

    man = json.loads((c_out / "manifest.json").read_text(encoding="utf-8"))
    assert man["files"] == []
    assert man["commit_seq"] == 0


def test_cmd_missing_args(cli_binary):
    """Missing required args should exit non-zero."""
    cp = subprocess.run(
        [cli_binary, "build", "--dir", "/tmp"],
        capture_output=True, text=True, timeout=5,
    )
    assert cp.returncode != 0


def test_cmd_bad_tokenizer(cli_binary):
    """Unknown tokenizer should be rejected."""
    cp = subprocess.run(
        [cli_binary, "build",
         "--dir", "/tmp", "--pattern", "*", "--output", "/tmp/out",
         "--tokenizer", "unicode"],
        capture_output=True, text=True, timeout=5,
    )
    assert cp.returncode != 0
