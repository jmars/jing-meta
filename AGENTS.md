# Agent instructions — jing-meta

Conventions and guardrails for AI coding agents working in this repo.

## C DAFSA native library: `libdafsa.so` is HOST-only — never clobber it with a musl build

`indexer/dafsa/libdafsa.so` is the **host glibc build** of the C DAFSA core,
loaded by the Python `ctypes` frontend (`indexer/dafsa/__init__.py` →
`Dafsa.load/create/add/...`) under host CPython. It must **always** be built
with the **host toolchain** (`gcc -shared -fPIC -O2`, i.e. `make libdafsa.so`
or `make all` on the host machine, which links `libc.so.6`).

### Do NOT do this
- **Never run a plain `make` / `make all` inside the shell-sandbox** and let it
  write `libdafsa.so`. The sandbox's `cc`/`gcc` are the **vendored musl cross
  toolchain**; a musl-built `libdafsa.so` is dynamically linked against musl
  `libc.so` and **will not load** under host glibc CPython (`ctypes.CDLL` →
  `dlopen` fails to resolve the `libc.so` dependency). It silently breaks the
  host frontend and any host-side `tests/test_indexer.py` /
  `test_search_indexer.py` / `test_wal.py`.
- **Never commit or keep** a musl-built `.so` as `libdafsa.so`.

### Correct patterns
- **Host build:** run `make -C indexer/dafsa libdafsa.so` (or `make all`) on
  the host, using host `gcc`. Verify it links `libc.so.6`:
  `readelf -d indexer/dafsa/libdafsa.so | grep NEEDED`.
- **Sandbox / musl build (for in-sandbox C testing only):** build to the
  distinct filename `libdafsa-musl.so` via
  `make -C indexer/dafsa musl`. This target emits `libdafsa-musl.so` and never
  touches `libdafsa.so`.
- **Tests should use a different file.** The ctypes loader
  (`_load()` in `indexer/dafsa/__init__.py`) resolves the library in this
  order: `$JING_DAFSA_SO` (explicit override) → `libdafsa.so` → `libdafsa-musl.so`
  → `ctypes.util.find_library("dafsa")`. So:
  - Host tests pick up the canonical `libdafsa.so`.
  - In-sandbox/musl tests pick up `libdafsa-musl.so` automatically, OR set
    `JING_DAFSA_SO=/abs/path/libdafsa-musl.so` to force it.
  - Never point host tests at a musl `.so`.

### Why the filenames differ
Host and sandbox use different C libraries (glibc vs musl) that are ABI- and
loader-incompatible. Keeping a distinct `libdafsa-musl.so` name guarantees a
musl build can never overwrite the host artifact, and each environment loads
the `.so` built for it.

## Git hygiene
- C build artifacts are gitignored (`libdafsa.so`, `libdafsa-musl.so`, `*.o`,
  `_t`, etc.) — build them locally, don't commit them.
- Personal/state data (`*.db`, `.jing/`, `*.npy`) is never committed.
