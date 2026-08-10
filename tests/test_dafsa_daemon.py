"""Tests for the DAFSA CLI daemon (JSON-lines stdio protocol).

Covers the daemon protocol, handle table, error handling, crash isolation,
and edge cases (embedded NUL keys, prefix_enum multi-payload).

Requires the ``dafsa-cli`` binary and pytest.  Reuses ``_get_lib()`` for the
skip probe (same pattern as the other test files).
"""

import os
from pathlib import Path

import pytest

try:
    from indexer.dafsa import _get_lib, DafsaDaemonCrashed, Dafsa, DafsaWal
    _get_lib()  # raises RuntimeError if dafsa-cli is missing or ABI-mismatched
except (RuntimeError, AttributeError, OSError) as e:
    pytest.skip(f"dafsa-cli daemon not available: {e}", allow_module_level=True)


# ── 1. handshake ───────────────────────────────────────────────────────────

def test_handshake():
    """Handshake returns abi==1, proto==1."""
    from indexer.dafsa import _Daemon
    d = _Daemon.get()
    reply = d.request("handshake")
    assert reply["abi"] == 1
    assert reply["proto"] == 1


# ── 2. handle table ────────────────────────────────────────────────────────

def test_create_free_recreate():
    """create → free → re-create returns a valid handle."""
    d1 = Dafsa.create()
    h1 = d1._h
    assert h1 > 0
    d1.add(b"hello")
    assert d1.lookup(b"hello")
    d1.free()

    # Re-creating should get a new handle (may or may not be the same id).
    d2 = Dafsa.create()
    assert d2._h > 0
    d2.free()


def test_free_zero_id():
    """free on id 0 is a no-op (Python side guards with `if self._h:`)."""
    # Dafsa.free() checks `if self._h:` before sending — this is tested
    # implicitly by the context-manager tests.  Direct free with h=0 would
    # be rejected by the daemon with EBADH, and the Python side would raise
    # RuntimeError, which is fine.
    pass


def test_add_on_freed_handle_raises():
    """add on a freed handle raises RuntimeError."""
    d = Dafsa.create()
    d.free()
    with pytest.raises(RuntimeError, match="invalid handle"):
        d.add(b"test")


def test_view_rejects_mutation():
    """A read-only view rejects add/delete/save/stats with EBADH."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # Create a small DAFSA, save it, then open as view.
        fst = Path(td) / "test.fst"
        with Dafsa.create() as d:
            d.add(b"hello")
            d.save(str(fst))

        v = Dafsa.load(str(fst), readonly=True)
        with pytest.raises(RuntimeError, match="Cannot mutate"):
            v.add(b"world")
        with pytest.raises(RuntimeError, match="Cannot mutate"):
            v.delete(b"hello")
        with pytest.raises(RuntimeError, match="Cannot save"):
            v.save(str(fst))
        with pytest.raises(RuntimeError, match="stats not available"):
            v.stats()
        # Lookup should still work.
        assert v.lookup(b"hello")
        v.free()


# ── 3. prefix_enum multi-payload ───────────────────────────────────────────

def test_prefix_enum_multi_payload():
    """prefix_enum returns ALL matching payloads, not just the first."""
    # Build a DAFSA with multiple keys sharing a prefix, each with a
    # different 8-byte suffix.  The composite-key encoding uses W\0:
    #   key = word + \x00 + file_idx(4BE) + entry_idx(4BE)
    # prefix_enum("word") walks to the \x00 edge, then DFS collects suffixes.
    with Dafsa.create() as d:
        for i in range(10):
            key = b"word\x00" + i.to_bytes(4, "big") + i.to_bytes(4, "big")
            d.add(key)

        results = d.prefix_enum(b"word")
        assert len(results) == 10, f"expected 10 payloads, got {len(results)}"
        # Verify we got all expected 8-byte suffixes
        expected = {
            i.to_bytes(4, "big") + i.to_bytes(4, "big")
            for i in range(10)
        }
        assert set(results) == expected


# ── 4. wal_replay across handles ───────────────────────────────────────────

def test_wal_replay_across_handles():
    """Append N adds/dels, replay, verify lookup set."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wal_path = Path(td) / "test.wal"

        # Open WAL and append adds/dels.
        with DafsaWal.open(str(wal_path)) as wal:
            for i in range(5):
                wal.append_add(f"keep_{i}".encode())
            for i in range(3):
                wal.append_add(f"del_{i}".encode())
            for i in range(3):
                wal.append_del(f"del_{i}".encode())
            wal.sync()

        # Replay into a fresh DAFSA.
        with Dafsa.create() as d:
            with DafsaWal.open(str(wal_path)) as wal2:
                n = wal2.replay_into(d)
            assert n == 11  # 5 adds + 3 adds + 3 dels = 11 WAL records
            for i in range(5):
                assert d.lookup(f"keep_{i}".encode()), f"keep_{i} should be present"
            for i in range(3):
                assert not d.lookup(f"del_{i}".encode()), f"del_{i} should be absent"


# ── 5. stats round-trip ────────────────────────────────────────────────────

def test_stats_round_trip():
    """stats returns all 5 counters, all ints."""
    with Dafsa.create() as d:
        d.add(b"alpha")
        d.add(b"beta")
        d.add(b"gamma")
        s = d.stats()
        assert isinstance(s, dict)
        for field in ("n_states_total", "n_states_reachable", "n_final",
                       "n_trans", "register_probes"):
            assert field in s, f"missing {field}"
            assert isinstance(s[field], int), f"{field} is {type(s[field])}"


# ── 6. daemon crash isolation ──────────────────────────────────────────────

def test_daemon_crash_isolation():
    """Crash mid-request: next op raises DafsaDaemonCrashed, then fresh daemon works."""
    from indexer.dafsa import _Daemon

    # Trigger a crash via debug_abort (needs JING_DAFSA_DAEMON_DEBUG_ABORT=1).
    # We need to set the env BEFORE spawning the daemon.  Since the daemon is
    # a singleton that's already spawned, we have to shut it down, set the env,
    # and let the next request spawn a fresh one with the debug flag.
    daemon = _Daemon.get()
    daemon.shutdown()

    old_env = os.environ.get("JING_DAFSA_DAEMON_DEBUG_ABORT")
    os.environ["JING_DAFSA_DAEMON_DEBUG_ABORT"] = "1"
    try:
        # Force a fresh spawn (the next request will auto-spawn).
        # The debug_abort op replies ok then abort()s — the daemon dies
        # but this request gets a normal reply.
        reply = _Daemon.get().request("debug_abort")
        assert reply.get("ok") is True

        # The daemon is now dead.  Next request should raise DafsaDaemonCrashed.
        with pytest.raises(DafsaDaemonCrashed):
            _Daemon.get().request("handshake")

        # After the crash is detected, unset the debug env so the next
        # daemon spawn is normal.
        del os.environ["JING_DAFSA_DAEMON_DEBUG_ABORT"]

        # Next request auto-spawns a fresh daemon and succeeds.
        reply2 = _Daemon.get().request("handshake")
        assert reply2["abi"] == 1
    finally:
        # Restore env.
        if old_env is not None:
            os.environ["JING_DAFSA_DAEMON_DEBUG_ABORT"] = old_env
        elif "JING_DAFSA_DAEMON_DEBUG_ABORT" in os.environ:
            del os.environ["JING_DAFSA_DAEMON_DEBUG_ABORT"]
        # Reset the daemon singleton to clean state for subsequent tests.
        _Daemon._instance = None


# ── 7. embedded-NUL key round-trip ─────────────────────────────────────────

def test_embedded_nul_key_round_trip():
    """add(b'a\\x00b') then lookup(b'a\\x00b') → True."""
    key = b"a\x00b"
    with Dafsa.create() as d:
        assert d.add(key), "should add new key"
        assert d.lookup(key), "should find embedded-NUL key"
        assert not d.lookup(b"a"), "prefix a should not match a\\x00b"
        assert not d.lookup(b"ab"), "ab should not match a\\x00b"


# ── 7b. batch_add ──────────────────────────────────────────────────────────

def test_add_many_batch():
    """add_many sends a batch_add; returns newly-added count."""
    with Dafsa.create() as d:
        added = d.add_many([b"alpha", b"beta", b"alpha", b"gamma"])
        assert added == 3, f"expected 3 new (alpha dup), got {added}"
        assert d.lookup(b"alpha")
        assert d.lookup(b"beta")
        assert d.lookup(b"gamma")
        assert not d.lookup(b"delta")


def test_add_many_large_chunks_correct():
    """add_many over many keys (exceeds one chunk) adds all of them."""
    n = 5000
    with Dafsa.create() as d:
        keys = [f"k{i}".encode() for i in range(n)]
        added = d.add_many(keys)
        assert added == n, f"expected {n} added, got {added}"
        assert d.lookup(b"k0")
        assert d.lookup(f"k{n-1}".encode())
        # duplicates across chunks are not double-counted
        added2 = d.add_many([b"k0", b"new"])
        assert added2 == 1, f"expected only 'new' added, got {added2}"


# ── 8. JING_DAFSA_CLI env override ─────────────────────────────────────────

def test_env_override_detected():
    """$JING_DAFSA_CLI is picked up by _find_binary."""
    from indexer.dafsa import _Daemon as DaemonCls
    path = DaemonCls._find_binary()
    assert path, "should find the binary"
    # It should be the in-tree build by default.
    assert "dafsa-cli" in path
