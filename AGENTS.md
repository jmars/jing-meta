# Agent instructions — jing-meta

Conventions and guardrails for AI coding agents working in this repo.

## C DAFSA daemon: `dafsa-cli` — a libc-agnostic native ELF

The DAFSA C core is accessed through a long-lived stdio JSON-lines daemon
(`indexer/dafsa/dafsa-cli`) instead of a ctypes `.so`.  The daemon is built with
**Cosmopolitan (`cosmocc`) and then assimilated into a native statically-linked
x86-64 ELF**, so it runs under **both** the host glibc CPython and the musl
sandbox CPython — it has **no libc dependency and no APE loader at runtime**.

The Python frontend (`indexer/dafsa/__init__.py`) spawns the daemon as a
subprocess and talks JSON-lines over stdio.  A C crash kills only the daemon,
not the Python MCP server.

### Build

```sh
make -C indexer/dafsa daemon        # = daemon-cosmo (default)
```

`daemon-cosmo` runs `cosmocc` to produce the APE polyglot plus two native ELFs
(`dafsa-cli.com.dbg` = x86-64, `dafsa-cli.aarch64.elf`), then runs
`sh ./dafsa-cli --assimilate --help` to rewrite the APE **in place** into a
plain statically-linked x86-64 ELF (`file` shows "statically linked, no section
header"; `readelf -d` shows no `NEEDED`/`PT_INTERP`).  The `--help` arg makes the
daemon exit after assimilating instead of blocking on stdin.  The build asserts
`readelf -d dafsa-cli | grep NEEDED` is empty.

A `daemon-musl` target (static musl via the vendored cross `cc`) is also
available if the cosmo toolchain is unavailable; it produces an equivalent
libc-agnostic static ELF.

The old `libdafsa.so` / `libdafsa-musl.so` shared-library targets are still in
the Makefile because the C test binary (`_t`) and `test_c_core.py` still link
directly against the object files — they are harmless.

### Binary discovery

The Python frontend resolves the daemon binary in this order:

1. `$JING_DAFSA_CLI` — explicit absolute path override
2. `indexer/dafsa/dafsa-cli` — the in-tree build
3. `shutil.which("dafsa-cli")` — on `$PATH`

### libc-agnostic property

Because `dafsa-cli` is a statically-linked native ELF (no `NEEDED`, no
`PT_INTERP`), it works regardless of the host's libc:

- **Host glibc CPython** (`.venv/bin/python`) can spawn it and talk to it over
  pipes — `subprocess.Popen` just needs an executable, not a compatible dynamic
  linker.
- **Sandbox musl CPython** (`python3`) can spawn the same binary natively.

This eliminates the old split between `libdafsa.so` (glibc) and
`libdafsa-musl.so` (musl) — one binary, both environments.

### Do NOT do this

- **Never add a runtime `libc` dependency** to `dafsa-cli`.  The build target
  asserts `readelf -d dafsa-cli | grep NEEDED` is empty.
- **Never commit the binary** or the cosmocc side-artifacts (`dafsa-cli.com.dbg`,
  `dafsa-cli.aarch64.elf`) — they are gitignored.

## Git hygiene
- C build artifacts are gitignored (`libdafsa.so`, `libdafsa-musl.so`,
  `dafsa-cli`, `*.o`, `_t`, etc.) — build them locally, don't commit them.
- Personal/state data (`*.db`, `.jing/`, `*.npy`) is never committed.
