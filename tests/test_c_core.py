"""Build and run the vendored C DAFSA test binary.

The binary asserts the C core's invariants directly (no Python involved).
Skipped at module level when no C toolchain (gcc) is available.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DAFSA_DIR = Path(__file__).resolve().parent.parent / "indexer" / "dafsa"
BIN = DAFSA_DIR / "_t"

SOURCES = [
    "dafsa_test.c",
    "dafsa.c",
    "dafsa_state.c",
    "dafsa_core.c",
    "dafsa_persist.c",
    "dafsa_view.c",
    "dafsa_crc32.c",
]

BANNER = "=== All tests passed. ==="

def _gcc_can_compile() -> bool:
    """True if gcc can read system headers and compile a trivial TU.

    In the shell-sandbox, gcc is present but cannot read /usr/include
    (out-of-tree header read denied), so the C core build would always fail —
    skip in that environment rather than error. On real machines this passes.
    """
    if shutil.which("gcc") is None:
        return False
    import tempfile
    probe = tempfile.NamedTemporaryFile("w", suffix=".c", delete=False)
    try:
        probe.write("int main(void){return 0;}\n")
        probe.close()
        res = subprocess.run(
            ["gcc", "-o", os.devnull, probe.name],
            capture_output=True, text=True,
        )
        return res.returncode == 0
    finally:
        import os as _os
        try:
            _os.unlink(probe.name)
        except OSError:
            pass


if not _gcc_can_compile():
    pytest.skip("gcc unavailable or cannot read system headers (e.g. sandbox); skipping C core tests", allow_module_level=True)


def _need_rebuild() -> bool:
    """Rebuild only if any source is newer than the existing binary."""
    if not BIN.exists():
        return True
    bin_mtime = BIN.stat().st_mtime
    return any((DAFSA_DIR / s).stat().st_mtime > bin_mtime for s in SOURCES)


@pytest.fixture(scope="module")
def c_test_result():
    if _need_rebuild():
        cmd = (
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-I.", "-o", str(BIN)]
            + [str(DAFSA_DIR / s) for s in SOURCES]
        )
        compiled = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
        if compiled.returncode != 0:
            print("C test compile FAILED:\n%s" % compiled.stderr, file=sys.stderr)
            raise AssertionError("gcc build of C test failed (see stderr)")

    # Run from the dafsa dir so the binary's own outputs (dafsa.dot) land in
    # the gitignored directory instead of polluting the repo root.
    run = subprocess.run(
        [str(BIN)], cwd=str(DAFSA_DIR), capture_output=True, text=True, timeout=30
    )
    return run


def test_c_core(c_test_result):
    if c_test_result.returncode != 0:
        print("C test run FAILED (rc=%d)\n--- stdout ---\n%s\n--- stderr ---\n%s"
              % (c_test_result.returncode, c_test_result.stdout, c_test_result.stderr),
              file=sys.stderr)
        raise AssertionError("C test binary exited non-zero (see stderr)")

    if BANNER not in c_test_result.stdout:
        print("C test stdout did not contain expected banner %r\n--- stdout ---\n%s"
              % (BANNER, c_test_result.stdout), file=sys.stderr)
        raise AssertionError("C test passed but banner %r not found" % BANNER)
