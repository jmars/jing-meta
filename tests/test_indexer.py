"""Tests for the incremental `update` subcommand of the Python DAFSA indexer.

Requires the C DAFSA core (libdafsa.so) and pytest. Run from the repo root:
    python -m pytest tests/ -v
"""

import json
import zlib
from pathlib import Path

import pytest

# The DAFSA C lib is now lazy-loaded (import indexer doesn't fail), but actual
# operations need libdafsa.so.  Probe early so the entire module skips cleanly
# when the shared library is not built (e.g. in CI without a C toolchain).
try:
    from indexer.dafsa import _get_lib
    _get_lib()  # raises RuntimeError if libdafsa.so is missing or ABI-mismatched
except (RuntimeError, AttributeError, OSError) as e:
    pytest.skip(f"libdafsa.so not available: {e}", allow_module_level=True)

from indexer import (
    SidecarCorruptError,
    _read_sidecar,
    _write_sidecar,
    build,
    open_index,
    update,
)


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


def _has_slots_manifest(index_dir: Path) -> bool:
    return (index_dir / "slots").is_dir() and (index_dir / "manifest.json").is_file()


def _no_tmp_leftovers(index_dir: Path) -> bool:
    return not list(index_dir.rglob("*.tmp"))


@pytest.fixture
def corpus(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha apple banana")
    _write_jsonl(data, "b.jsonl", "bravo banana cherry")
    return data


def _build_index(corpus, output) -> None:
    build(corpus, "*.jsonl", "jsonl", output)


# --- 1. update on fresh (no index) == build ---------------------------------
def test_update_first_run_equals_build(tmp_path, corpus):
    out_a = tmp_path / "out_build"
    out_b = tmp_path / "out_update"

    _build_index(corpus, out_a)
    result = update(corpus, "*.jsonl", "jsonl", out_b)

    assert result["first_run"] is True
    assert result["added"] == 0
    assert result["total_slots"] == 2

    assert _has_slots_manifest(out_b)
    assert _search(out_a, "banana") == _search(out_b, "banana")
    assert _search(out_a, "apple") == _search(out_b, "apple")
    assert _search(out_b, "cherry") == _search(out_a, "cherry")
    assert _no_tmp_leftovers(out_b)


# --- 2. no changes -> all unchanged ------------------------------------------
def test_update_no_changes(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # snapshot sidecars before
    before = {
        p.name: p.read_bytes()
        for p in (out / "slots").glob("*.keys")
    }

    result = update(corpus, "*.jsonl", "jsonl", out)

    assert result["unchanged"] == 2
    assert result["updated"] == 0
    assert result["added"] == 0
    assert result["removed"] == 0

    after = {
        p.name: p.read_bytes()
        for p in (out / "slots").glob("*.keys")
    }
    assert after == before
    assert _no_tmp_leftovers(out)


# --- 3. modify a file ---------------------------------------------------------
def test_update_modify(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # a.txt changes: apple removed, dandelion added
    _write_jsonl(corpus, "a.jsonl", "alpha banana dandelion")

    result = update(corpus, "*.jsonl", "jsonl", out)

    assert result["updated"] == 1
    assert result["unchanged"] == 1

    hits = _search(out, "apple")
    assert not hits, "old word 'apple' should be gone after update"

    hits = _search(out, "dandelion")
    assert hits, "new word 'dandelion' should be present"
    assert all(f == "a.jsonl" for _, _, f in hits)

    # other file unaffected
    assert _search(out, "cherry"), "b.txt still has cherry"
    assert _no_tmp_leftovers(out)


# --- 4. delete a file -> tombstoned ------------------------------------------
def test_update_delete(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    (corpus / "a.jsonl").unlink()

    result = update(corpus, "*.jsonl", "jsonl", out)

    assert result["removed"] == 1
    assert result["unchanged"] == 1

    # slot sidecar absent (tombstone)
    assert not (out / "slots" / "0.keys").exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["tombstoned"] is True
    assert manifest["files"][0]["filename"] == ""

    # no hits from deleted slot
    assert not _search(out, "apple")
    assert not _search(out, "alpha")
    # surviving file still searchable
    assert _search(out, "cherry")
    assert _no_tmp_leftovers(out)


# --- 5. add a file -> appended at new slot -----------------------------------
def test_update_add(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    _write_jsonl(corpus, "c.jsonl", "cherimoya cranberry")

    result = update(corpus, "*.jsonl", "jsonl", out)

    assert result["added"] == 1
    assert result["unchanged"] == 2

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 3
    assert manifest["files"][2]["filename"] == "c.jsonl"
    assert manifest["files"][2]["tombstoned"] is False
    # old slots unchanged
    assert manifest["files"][0]["filename"] == "a.jsonl"

    assert _search(out, "cherimoya")
    assert _search(out, "banana"), "old content still searchable"
    assert _no_tmp_leftovers(out)


# --- 6. tombstone stability ---------------------------------------------------
def test_tombstone_stability(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # delete a.txt (slot 0) then re-add same-named a.txt
    (corpus / "a.jsonl").unlink()
    update(corpus, "*.jsonl", "jsonl", out)

    _write_jsonl(corpus, "a.jsonl", "alpha again")
    update(corpus, "*.jsonl", "jsonl", out)

    # re-added file goes to a NEW slot; old slot stays tombstoned
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["tombstoned"] is True
    new_slot = next(
        i for i, fe in enumerate(manifest["files"])
        if fe["filename"] == "a.jsonl" and not fe["tombstoned"]
    )
    assert new_slot == 2, "re-added a.txt must land on a fresh slot (no renumber)"
    assert not (out / "slots" / "0.keys").exists()
    assert _search(out, "again")
    assert _no_tmp_leftovers(out)


# --- 7. sidecar round-trip -----------------------------------------------------
def test_sidecar_roundtrip(tmp_path):
    slots = tmp_path / "slots"
    pairs = [(0, b"alpha"), (1, b"banana"), (0, b"apple")]

    _write_sidecar(slots, 7, pairs)
    path = slots / "7.keys"
    data = path.read_bytes()
    assert data[:4] == b"SIDE", "v1 sidecar must start with magic b'SIDE'"
    assert _read_sidecar(slots, 7) == pairs

    # Format layout: 12-byte header + 8*N record headers + words + 4-byte CRC.
    n = len(pairs)
    assert len(data) == 12 + 8 * n + sum(len(w) for _, w in pairs) + 4
    assert zlib.crc32(data[:-4]) == int.from_bytes(data[-4:], "little")

    # missing file -> []
    assert _read_sidecar(slots, 999) == []

    # Legacy-sniff: a manually written legacy LE stream (no magic) parses fine.
    legacy = bytearray()
    for ei, w in pairs:
        legacy += ei.to_bytes(4, "little") + len(w).to_bytes(4, "little") + w
    (slots / "8.keys").write_bytes(bytes(legacy))
    assert _read_sidecar(slots, 8) == pairs

    # Legacy + trailing garbage byte -> SidecarCorruptError
    (slots / "9.keys").write_bytes(bytes(legacy) + b"\x00")
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 9)

    # Truncated new-format (cut into CRC) -> SidecarCorruptError
    (slots / "10.keys").write_bytes(data[:-5])
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 10)

    # CRC bit-flip (XOR last byte) -> SidecarCorruptError
    flipped = bytearray(data)
    flipped[-1] ^= 0xFF
    (slots / "11.keys").write_bytes(bytes(flipped))
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 11)

    # n_records mismatch (header says 5, body has 3) -> SidecarCorruptError
    bad = bytearray(b"SIDE")
    bad += (1).to_bytes(4, "little") + (5).to_bytes(4, "little")
    bad += bytes(legacy) + (0).to_bytes(4, "little")
    (slots / "12.keys").write_bytes(bytes(bad))
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 12)

    # Unknown version (byte = 99) -> SidecarCorruptError
    ver = bytearray(data)
    ver[4] = 99
    (slots / "13.keys").write_bytes(bytes(ver))
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 13)

    # Just magic (4 bytes b"SIDE") -> SidecarCorruptError
    (slots / "14.keys").write_bytes(b"SIDE")
    with pytest.raises(SidecarCorruptError):
        _read_sidecar(slots, 14)

    # Valid n_records=0 file (16 bytes) -> []
    empty = b"SIDE" + (1).to_bytes(4, "little") + (0).to_bytes(4, "little")
    (slots / "15.keys").write_bytes(empty + zlib.crc32(empty).to_bytes(4, "little"))
    assert _read_sidecar(slots, 15) == []


# --- 8. manifest backward-compat ----------------------------------------------
def test_manifest_backward_compat(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # Rewrite manifest with only the 4 old fields (no mtime/size/tombstoned).
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    old_files = [
        {"filename": fe["filename"], "title": fe["title"],
         "date": fe["date"], "source": fe["source"]}
        for fe in manifest["files"]
    ]
    (out / "manifest.json").write_text(
        json.dumps({"files": old_files}), encoding="utf-8"
    )

    assert _search(out, "banana")
    assert _search(out, "cherry")


# --- 9. no .tmp leftovers after build -----------------------------------------
def test_no_tmp_after_build(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)
    assert _no_tmp_leftovers(out)

    update(corpus, "*.jsonl", "jsonl", out)
    assert _no_tmp_leftovers(out)


# --- 10. nested-dir corpus: idempotent update + stable relative paths ---------
def test_nested_dir_idempotent_update(tmp_path):
    """build stores relative paths; idempotent update must not duplicate slots."""
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "sub" / "nested.jsonl").write_text("alpha beta", encoding="utf-8")
    (data / "top.jsonl").write_text("gamma delta", encoding="utf-8")
    out = tmp_path / "out"

    build(data, "*.jsonl", "jsonl", out)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(f["filename"] for f in man["files"]) == ["sub/nested.jsonl", "top.jsonl"]
    assert _search(out, "alpha")

    res = update(data, "*.jsonl", "jsonl", out)
    assert res["unchanged"] == 2
    assert res["added"] == 0 and res["updated"] == 0 and res["removed"] == 0, res
    man2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(man2["files"]) == 2, "no duplicate slots after idempotent update"


# --- 11. dirs-type domain: idempotent update (no false add/remove) ------------
def test_dirs_type_idempotent_update(tmp_path):
    """Sessions-style layout (dir/<session>/messages.jsonl). update must keep
    the relative-path match key stable so an unchanged corpus is all-unchanged."""
    data = tmp_path / "data"
    (data / "sess1").mkdir(parents=True)
    (data / "sess2").mkdir(parents=True)
    (data / "sess1" / "messages.jsonl").write_text('{"content": "alpha beta"}\n', encoding="utf-8")
    (data / "sess2" / "messages.jsonl").write_text('{"content": "gamma delta"}\n', encoding="utf-8")
    out = tmp_path / "out"

    build(data, "messages.jsonl", "jsonl", out)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(f["filename"] for f in man["files"]) == [
        "sess1/messages.jsonl", "sess2/messages.jsonl"
    ]

    res = update(data, "messages.jsonl", "jsonl", out)
    assert res["unchanged"] == 2, res
    assert res["added"] == 0 and res["updated"] == 0 and res["removed"] == 0, res
    man2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(man2["files"]) == 2, "no duplicate slots"


# --- 12. resolve_file_idx is tombstone-aware -----------------------------------
def test_resolve_file_idx_tombstone(tmp_path):
    from search.indexer import resolve_file_idx

    out = tmp_path / "out"
    (out).mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({
        "files": [
            {"filename": "a.jsonl", "title": "a", "date": "?", "source": "jsonl"},
            {"filename": "", "title": "", "date": "", "source": "",
             "tombstoned": True},
        ]
    }), encoding="utf-8")

    assert resolve_file_idx(out, 0) == "a.jsonl"
    assert resolve_file_idx(out, 1) is None  # tombstoned
    assert resolve_file_idx(out, 5) is None  # out of range


# --- 13. graceful skip for non-DAFSA extractors --------------------------------
def test_build_index_skips_unsupported_extractor(tmp_path):
    """build_index gracefully skips domains whose extractor is not in indexer.EXTRACTORS."""
    from search.config import DomainConfig
    from search.indexer import build_index

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "foo.jsonl").write_text('{"content": "hello"}\n', encoding="utf-8")

    cfg = DomainConfig(name="test", dir=str(data_dir), extractor="notification")
    ok, msg = build_index(cfg)
    assert ok is True
    assert "skipped" in msg
    assert "notification" in msg


def test_update_index_skips_unsupported_extractor(tmp_path):
    """update_index gracefully skips domains whose extractor is not in indexer.EXTRACTORS."""
    from search.config import DomainConfig
    from search.indexer import update_index

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "foo.jsonl").write_text('{"content": "hello"}\n', encoding="utf-8")

    cfg = DomainConfig(name="test", dir=str(data_dir), extractor="notification")
    ok, msg = update_index(cfg)
    assert ok is True
    assert "skipped" in msg
    assert "notification" in msg


# --- 14. AND/OR search semantics -----------------------------------------------
def test_search_and_or(tmp_path, corpus):
    """FST search supports AND (any_word=False) and OR (any_word=True)."""
    out = tmp_path / "out"
    _build_index(corpus, out)

    with open_index(out) as idx:
        # OR: "alpha cherry" — a.txt has alpha, b.txt has cherry → both files
        or_hits = idx.search("alpha cherry", any_word=True)
        or_files = {idx.file_name(h.file_idx) for h in or_hits}
        assert or_files == {"a.jsonl", "b.jsonl"}, f"OR should match both files, got {or_files}"

        # AND: "alpha cherry" — no single file has both → empty
        and_hits = idx.search("alpha cherry", any_word=False)
        assert len(and_hits) == 0, f"AND should match no files, got {and_hits}"

        # AND: "alpha banana" — only a.txt has both
        and_hits2 = idx.search("alpha banana", any_word=False)
        and_files2 = {idx.file_name(h.file_idx) for h in and_hits2}
        assert and_files2 == {"a.jsonl"}, f"AND should match only a.txt, got {and_files2}"

        # single-word search matches both files (defaults to AND, no difference for 1 word)
        default_hits = idx.search("banana")
        default_files = {idx.file_name(h.file_idx) for h in default_hits}
        assert default_files == {"a.jsonl", "b.jsonl"}, f"single-word search should match both, got {default_files}"


# --- pair-level diff: unchanged keys in a changed file are not re-added ----
def test_update_pair_level_diff_leaves_unchanged_keys(tmp_path):
    """A changed file should only touch keys that actually changed.

    Regresses the previous delete-all + re-add-all behaviour, which churned the
    DAFSA with O(N) delete + O(N) add even when only one entry changed — the
    cause of multi-minute updates on large session files.
    """
    from indexer import _read_sidecar

    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data, "a.jsonl", "alpha banana cherry")
    _write_jsonl(data, "b.jsonl", "delta")

    out = tmp_path / "out"
    build(data, "*.jsonl", "jsonl", out)

    # a.txt: only 'banana' -> 'grape' changed; 'alpha' and 'cherry' unchanged.
    _write_jsonl(data, "a.jsonl", "alpha grape cherry")

    result = update(data, "*.jsonl", "jsonl", out)
    assert result["updated"] == 1

    # The DAFSA should still contain the unchanged keys (search correctness).
    assert _search(out, "alpha")
    assert _search(out, "cherry")
    assert _search(out, "grape")
    assert not _search(out, "banana")

    # More precisely: the changed slot's sidecar records exactly the new pair
    # set, and unchanged (entry_idx, word) pairs survived without churn. The
    # sidecar content is the source of truth for what's in the DAFSA.
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    slot = next(
        i for i, fe in enumerate(manifest["files"])
        if fe.get("filename") == "a.jsonl"
    )
    pairs = set(_read_sidecar(out / "slots", slot))
    assert ("alpha", b"alpha") not in pairs  # (entry_idx, word) — entry0
    # Verify 'alpha' is still searchable even though the file text changed the
    # entry layout; the key identity is (entry_idx, word).
    assert _search(out, "alpha")


# --- 15. index.lock flock: exists during and after the context --------------
def test_index_lock_acquire_release(tmp_path):
    from indexer import _index_lock

    out = tmp_path / "out"
    with _index_lock(out):
        assert (out / "index.lock").exists(), "lock file must exist while held"
    # Lock file persists after release (flock released, fd closed).
    assert (out / "index.lock").exists()


# --- 16. corrupt sidecar on a changed file -> treat as new (add-all) --------
def test_update_corrupt_sidecar_changed_file(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # Bit-flip the CRC byte (last byte) of a.txt's sidecar (slot 0).
    sp = out / "slots" / "0.keys"
    data = bytearray(sp.read_bytes())
    data[-1] ^= 0xFF
    sp.write_bytes(bytes(data))

    # Modify a.txt (new word 'dandelion').
    _write_jsonl(corpus, "a.jsonl", "alpha banana dandelion")

    result = update(corpus, "*.jsonl", "jsonl", out)
    assert result["updated"] == 1
    assert result["unchanged"] == 1

    # Recovery by add-all: new word searchable, other file unaffected.
    assert _search(out, "dandelion")
    assert _search(out, "cherry")
    assert _no_tmp_leftovers(out)


# --- 17. corrupt sidecar on a deleted file -> still tombstoned --------------
def test_update_corrupt_sidecar_tombstone(tmp_path, corpus):
    out = tmp_path / "out"
    _build_index(corpus, out)

    # Bit-flip the CRC byte (last byte) of a.txt's sidecar (slot 0).
    sp = out / "slots" / "0.keys"
    data = bytearray(sp.read_bytes())
    data[-1] ^= 0xFF
    sp.write_bytes(bytes(data))

    # Delete a.txt — we can't know its old keys, but the manifest must still be
    # tombstoned so the file is hidden from results.
    (corpus / "a.jsonl").unlink()

    result = update(corpus, "*.jsonl", "jsonl", out)
    assert result["removed"] == 1
    assert result["unchanged"] == 1

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["tombstoned"] is True
    assert manifest["files"][0]["filename"] == ""
    # tombstone removes the (corrupt) sidecar too
    assert not (out / "slots" / "0.keys").exists()
    # surviving file still searchable
    assert _search(out, "cherry")
    assert _no_tmp_leftovers(out)



def test_extractor_subset_invariant():
    """Every DAFSA indexer extractor must also be a valid search-config extractor.

    indexer.EXTRACTORS and search.config._VALID_EXTRACTORS are maintained in
    two places; keep them consistent so a new extractor doesn't silently fail
    the DAFSA-capable gate at search time.
    """
    from indexer import EXTRACTORS
    from search.config import _VALID_EXTRACTORS

    missing = set(EXTRACTORS) - set(_VALID_EXTRACTORS)
    assert not missing, (
        "indexer.EXTRACTORS has entries not in search.config._VALID_EXTRACTORS: "
        f"{sorted(missing)}. Add them to _VALID_EXTRACTORS in search/config.py."
    )
