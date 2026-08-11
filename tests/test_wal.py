"""Tests for the DAFSA write-ahead log (WAL) incremental-persistence feature.

Requires the C DAFSA core (libdafsa.so) and pytest. Run from the repo root:
    python -m pytest tests/ -v
"""

import json
from pathlib import Path

import pytest

try:
    from indexer.dafsa import _get_lib
    _get_lib()
except (RuntimeError, AttributeError, OSError) as e:
    pytest.skip(f"libdafsa.so not available: {e}", allow_module_level=True)

from indexer import build, compact, open_index, update


def _write_jsonl(directory: Path, name: str, content: str) -> Path:
    p = directory / name
    p.write_text(json.dumps({"content": content}) + "\n", encoding="utf-8")
    return p


def _search(index_dir: Path, query: str):
    with open_index(index_dir) as idx:
        hits = idx.search(query)
        return {
            (h.file_idx, h.entry_idx, idx.file_name(h.file_idx))
            for h in hits
        }


def _search_files(index_dir: Path, query: str) -> set[str]:
    return {f for _, _, f in _search(index_dir, query)}


def _fresh_build(corpus, output) -> Path:
    """Build a fresh index into a temp dir and return the output dir."""
    build(corpus, "*.jsonl", "jsonl", output)
    return output


# ── P1: build + update with one changed file → search matches fresh build ──
def test_wal_update_changed_file_search_matches_fresh_build(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha apple banana")
    _write_jsonl(data, "b.jsonl", "bravo banana cherry")

    out = tmp_path / "out"
    _fresh_build(data, out)

    # Change a.txt: remove apple, add dandelion
    _write_jsonl(data, "a.jsonl", "alpha banana dandelion")
    result = update(data, "*.jsonl", "jsonl", out)

    assert result["updated"] == 1
    assert result["unchanged"] == 1

    # Search correctness: old word gone, new word present
    assert not _search(out, "apple"), "old word 'apple' should be gone"
    assert _search_files(out, "dandelion") == {"a.jsonl"}
    assert _search_files(out, "cherry") == {"b.jsonl"}, "b.txt unaffected"

    # Compare with fresh build (file_idx values may differ, compare by filename)
    fresh = tmp_path / "fresh"
    _fresh_build(data, fresh)
    assert _search_files(out, "banana") == _search_files(fresh, "banana")
    assert _search_files(out, "alpha") == _search_files(fresh, "alpha")
    assert _search_files(out, "cherry") == _search_files(fresh, "cherry")


# ── P2: incrementality proof — base FST mtime unchanged, WAL exists and grew ──
def test_wal_incrementality_proof(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # Use a larger base so the WAL stays under 25 % (compaction would rewrite FST).
    words = " ".join(f"word_{i:03d}" for i in range(100))
    _write_jsonl(data, "a.jsonl", f"alpha beta gamma {words}")
    _write_jsonl(data, "b.jsonl", "delta epsilon zeta eta theta iota kappa")

    out = tmp_path / "out"
    _fresh_build(data, out)

    fst_path = out / "index.fst"
    wal_path = out / "index.wal"

    # After fresh build, WAL should not exist
    assert not wal_path.exists(), "fresh build must remove stale WAL"

    base_mtime = fst_path.stat().st_mtime_ns

    # Small modification — only 2 key changes on a large base
    _write_jsonl(data, "a.jsonl", f"alpha beta zigzag {words}")
    result = update(data, "*.jsonl", "jsonl", out)

    # Base FST mtime must be unchanged (no compaction triggered)
    assert fst_path.stat().st_mtime_ns == base_mtime, "base index.fst mtime must not change"
    assert result.get("index_fst_mtime_unchanged") is True

    # WAL must exist and have content
    assert wal_path.exists(), "index.wal must exist after update"
    wal_size = wal_path.stat().st_size
    assert wal_size > 16, f"WAL must contain records beyond header, got {wal_size} bytes"

    # Search still works through the layered view
    assert _search_files(out, "zigzag") == {"a.jsonl"}
    assert "b.jsonl" in _search_files(out, "delta")
    assert not _search(out, "gamma"), "removed word 'gamma' must not be found"


# ── P3: merge search correctness after several updates ──
def test_wal_merge_search_correctness(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha beta gamma")
    _write_jsonl(data, "b.jsonl", "delta epsilon zeta")
    _write_jsonl(data, "c.jsonl", "eta theta iota")

    out = tmp_path / "out"
    _fresh_build(data, out)

    # Update 1: modify a.txt (remove gamma, add kappa)
    _write_jsonl(data, "a.jsonl", "alpha beta kappa")
    update(data, "*.jsonl", "jsonl", out)

    # Update 2: add a new file
    _write_jsonl(data, "d.jsonl", "lambda mu")
    update(data, "*.jsonl", "jsonl", out)

    # Update 3: delete b.txt
    (data / "b.jsonl").unlink()
    update(data, "*.jsonl", "jsonl", out)

    # Update 4: modify c.txt (remove theta, add nu)
    _write_jsonl(data, "c.jsonl", "eta nu iota")
    update(data, "*.jsonl", "jsonl", out)

    # Search correctness after all updates
    assert _search_files(out, "kappa") == {"a.jsonl"}
    assert not _search(out, "gamma"), "removed word should be gone"
    assert _search_files(out, "lambda") == {"d.jsonl"}
    assert _search_files(out, "mu") == {"d.jsonl"}
    assert not _search(out, "delta"), "deleted file's words should be gone"
    assert not _search(out, "epsilon"), "deleted file's words should be gone"
    assert not _search(out, "zeta"), "deleted file's words should be gone"
    assert _search_files(out, "nu") == {"c.jsonl"}
    assert not _search(out, "theta"), "removed word from c.txt should be gone"
    assert _search_files(out, "eta") == {"c.jsonl"}, "unchanged word still present"

    # Compare with fresh build (file_idx values may differ, compare by filename)
    fresh = tmp_path / "fresh"
    _fresh_build(data, fresh)
    for word in ["alpha", "beta", "kappa", "lambda", "mu", "eta", "nu", "iota"]:
        assert _search_files(out, word) == _search_files(fresh, word), f"mismatch for '{word}'"
    for word in ["gamma", "delta", "epsilon", "zeta", "theta"]:
        assert not _search(out, word), f"'{word}' should not be searchable"


# ── P4: compaction correctness ──
def test_wal_compaction_correctness(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # Use a larger base so the initial update doesn't trigger auto-compaction.
    words = " ".join(f"word_{i:03d}" for i in range(100))
    _write_jsonl(data, "a.jsonl", f"alpha beta gamma {words}")
    _write_jsonl(data, "b.jsonl", "delta epsilon zeta eta theta iota kappa")

    out = tmp_path / "out"
    _fresh_build(data, out)

    # Modify a.txt to create WAL (small delta on large base → no auto-compaction)
    _write_jsonl(data, "a.jsonl", f"alpha beta zigzag {words}")
    update(data, "*.jsonl", "jsonl", out)

    wal_path = out / "index.wal"
    fst_path = out / "index.fst"

    assert wal_path.exists(), "WAL must exist before compaction"
    pre_mtime = fst_path.stat().st_mtime_ns

    # Search state before compaction (compare by filename, file_idx may differ)
    pre_zeta = _search_files(out, "zigzag")
    pre_alpha = _search_files(out, "alpha")
    pre_delta = _search_files(out, "delta")

    # Force compaction
    compact(out)

    # WAL gone, FST rewritten
    assert not wal_path.exists(), "WAL must be removed after compaction"
    assert fst_path.stat().st_mtime_ns != pre_mtime, "FST must be rewritten"

    # Search identical pre/post compaction
    assert _search_files(out, "zigzag") == pre_zeta
    assert _search_files(out, "alpha") == pre_alpha
    assert _search_files(out, "delta") == pre_delta
    assert not _search(out, "gamma"), "removed word still gone after compaction"


# ── P5: crash recovery — torn tail truncation ──
def test_wal_crash_recovery_torn_tail(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # Larger base to avoid auto-compaction on first update.
    words = " ".join(f"word_{i:03d}" for i in range(100))
    _write_jsonl(data, "a.jsonl", f"alpha beta gamma {words}")
    _write_jsonl(data, "b.jsonl", "delta epsilon zeta eta theta iota kappa")

    out = tmp_path / "out"
    _fresh_build(data, out)

    # Do a normal update to create a valid WAL
    _write_jsonl(data, "a.jsonl", f"alpha beta zigzag {words}")
    update(data, "*.jsonl", "jsonl", out)

    # Snapshot search after valid WAL
    valid_zigzag = _search_files(out, "zigzag")
    valid_alpha = _search_files(out, "alpha")
    valid_delta = _search_files(out, "delta")
    assert not _search(out, "gamma")

    # Now append garbage to the WAL (simulating a torn tail)
    wal_path = out / "index.wal"
    garbage = b"\xff\xff\xff\xff" + b"torn" * 10
    with wal_path.open("ab") as f:
        f.write(garbage)

    # Opening the index must not crash — C truncates torn tail
    assert _search_files(out, "zigzag") == valid_zigzag
    assert _search_files(out, "alpha") == valid_alpha
    assert _search_files(out, "delta") == valid_delta
    assert not _search(out, "gamma"), "removed word still gone after torn tail"


# ── P6: repeated updates (5x) → final search matches fresh build ──
def test_wal_repeated_updates(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # Larger base to avoid auto-compaction during the sequence.
    padding = " ".join(f"pad_{i:03d}" for i in range(80))
    files = {
        "a.jsonl": f"alpha beta gamma {padding}",
        "b.jsonl": f"delta epsilon zeta {padding}",
        "c.jsonl": f"eta theta iota {padding}",
        "d.jsonl": f"kappa lambda mu {padding}",
        "e.jsonl": f"nu xi omicron {padding}",
    }
    for name, content in files.items():
        _write_jsonl(data, name, content)

    out = tmp_path / "out"
    _fresh_build(data, out)

    # 5 rounds of modifications (each includes padding to avoid huge diffs)
    # Round 1: modify a.txt (gamma→phi)
    _write_jsonl(data, "a.jsonl", f"alpha beta phi {padding}")
    update(data, "*.jsonl", "jsonl", out)

    # Round 2: add new file f.txt
    _write_jsonl(data, "f.jsonl", f"pi rho sigma {padding}")
    update(data, "*.jsonl", "jsonl", out)

    # Round 3: delete c.txt
    (data / "c.jsonl").unlink()
    update(data, "*.jsonl", "jsonl", out)

    # Round 4: modify b.txt (zeta→omega) and d.txt (lambda→tau)
    _write_jsonl(data, "b.jsonl", f"delta epsilon omega {padding}")
    _write_jsonl(data, "d.jsonl", f"kappa tau mu {padding}")
    update(data, "*.jsonl", "jsonl", out)

    # Round 5: delete e.txt
    (data / "e.jsonl").unlink()
    update(data, "*.jsonl", "jsonl", out)

    # Compare with fresh build (file_idx values may differ, compare by filename)
    fresh = tmp_path / "fresh"
    _fresh_build(data, fresh)
    for word in ["alpha", "beta", "phi", "delta", "epsilon", "omega", "kappa", "tau", "mu",
                 "pi", "rho", "sigma"]:
        assert _search_files(out, word) == _search_files(fresh, word), f"mismatch for '{word}'"
    for word in ["gamma", "zeta", "eta", "theta", "iota", "lambda", "nu", "xi", "omicron"]:
        assert not _search(out, word), f"'{word}' should not be searchable"


# ── P7: empty-diff fast path — no changes → no writes ──
def test_wal_empty_diff_fast_path(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha beta")
    _write_jsonl(data, "b.jsonl", "gamma delta")

    out = tmp_path / "out"
    _fresh_build(data, out)

    fst_path = out / "index.fst"
    wal_path = out / "index.wal"

    base_mtime = fst_path.stat().st_mtime_ns
    wal_exists_before = wal_path.exists()
    wal_size_before = wal_path.stat().st_size if wal_exists_before else 0

    # Update with NO changes
    result = update(data, "*.jsonl", "jsonl", out)

    assert result["unchanged"] == 2
    assert result["updated"] == 0
    assert result["added"] == 0
    assert result["removed"] == 0
    assert result.get("index_fst_mtime_unchanged") is True

    # No writes at all
    assert fst_path.stat().st_mtime_ns == base_mtime, "FST mtime must be unchanged"
    wal_exists_after = wal_path.exists()
    wal_size_after = wal_path.stat().st_size if wal_exists_after else 0
    assert wal_size_after == wal_size_before, "WAL size must be unchanged (no writes)"


# ── P8: new word not in base → search returns the entry ──
def test_wal_new_word_in_added_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha beta")

    out = tmp_path / "out"
    _fresh_build(data, out)

    # Add a file with a word never seen before
    _write_jsonl(data, "b.jsonl", "xylophone zebra")
    update(data, "*.jsonl", "jsonl", out)

    assert _search_files(out, "xylophone") == {"b.jsonl"}
    assert _search_files(out, "zebra") == {"b.jsonl"}
    # Old word from original file must still work
    assert _search_files(out, "alpha") == {"a.jsonl"}, "old word must still be searchable"
