# Roadmap: Replace the Rust `fst` backend with the Carrasco–Forcada C DAFSA (`dafsa`)

**Status:** Approved plan (2026-08-06); **canonical roadmap updated 2026-08-09** for the jing-meta Python frontend. The C core (M0/M1) and the M4 optional `DafsaView` are **done**; the `build`/`search` frontend is **done in Python**. The **main remaining item is M3 — incremental `update`** (sidecars, tombstones, stable `file_idx`).
**Naming (D10, decided 2026-08-06):** the DAFSA core / PoC is named **`dafsa`** (Deterministic Acyclic Finite State Automaton). The deployed binary **stays `fst-indexer`** for drop-in compatibility (`web-archive-mcp/server.py:602` hardcodes it; config points at it).
**Scope:** Replaces the `fst` crate search backend in the palimpsest toolkit (`unified-history-mcp`, via `fst-indexer`) with the Carrasco–Forcada incremental minimal acyclic DFA in `dafsa.c`. **This repo (`jing-meta`) is the canonical home of the dafsa core**; the frontend here is **Python `ctypes` (no Rust toolchain)**. The Rust and standalone-C frontends below are retained as historical reference (D13).

---

## Part 1 — Session decision log

Every decision made during this planning session, recorded for reference.

### D1. Approach: hybrid, not standalone C binary
Keep the `fst-indexer` Rust binary (same name, same CLI, same JSON, same `manifest.json` *shape*). Drop the `fst` crate. Compile the C DAFSA core in via the `cc` crate + FFI. Tokenization, globbing, `jsonl`/`txt`/`transcript` extractors, and manifest writing **stay in Rust, byte-for-byte unchanged**.

- **Rationale:** re-porting JSON parsing, JSON string escaping, globbing, and transcript parsing to C is the highest-parity-risk work and buys nothing the user asked for. The only thing a standalone C `dafsa-indexer` (M4, deferred) buys is shedding the Rust toolchain, which is not a requirement.

### D2. Incremental strategy: TRUE INCREMENTAL
User explicitly chose **True incremental (B)** over (A) event-driven full rebuild and (C) hybrid append + nightly compaction:
- Persistent read-write DAFSA (load → mutate → save).
- Append-only file slots + tombstones for a **stable `file_idx`**.
- Per-file key sidecar so `update` can `dafsa_delete` old keys and `dafsa_add` new ones.
- New `update` subcommand; **drop the hourly reindex timer**; keep a cheap nightly `build` as compaction/GC.
- `dafsa_delete_n` is load-bearing → randomized differential tests mandatory.

### D3. Length-delimited key API (NUL-in-keys)
The composite key `{word}\0{file_idx:u32BE}{entry_idx:u32BE}` embeds a NUL separator, but the existing `dafsa.c` API is `strlen`-based. **Every key API must be length-delimited** (`dafsa_add_n`/`dafsa_lookup_n`/`dafsa_delete_n`); the old `strlen` wrappers are kept as thin shims.

### D4. On-disk format is ours to replace
`index.fst` contents are ours to define (only `fst-indexer` reads it), but the **filename must stay `index.fst`** (consumer checks `idx_dir/"index.fst"` at `indexer.py:131`). The `manifest.json` contract is shared with Python and must keep its shape.

### D5. Stable `file_idx` via append-only slots + tombstones
- `file_idx` = slot index in `files[]`. **Never renumbered by `update`.**
- New file → append slot (`slot = files.len()`). Removed file → keep slot, tombstone the entry, delete its keys. Changed file → same slot, delete-old/add-new.
- Python still does `manifest[file_idx]`; `resolve_file_idx` just tolerates tombstoned slots.
- Only `build` (compaction) renumbers, producing a dense manifest.

### D6. Prefix semantics = `W\0`, not `W`
DAFSA walks `word`, **requires a `\0` edge next**, then enumerates payloads. Query `"ca"` must NOT return `"cat"` hits. Mandatory explicit test.

### D7. `MAX_STATES=100000` is too small
Convert the fixed arrays to heap `malloc`/`realloc` with doubling grow. Remove `MAX_STATES`/`REGISTER_SZ` caps. **Measure real-corpus reachable-state count before M2 gate** (Q1).

### D8. Deployment/ownership decisions
- `fst-indexer` stays a normal native Rust binary (NOT cosmocc/APE). `build.rs` uses system `cc`.
- Vendored C lives at `fst-indexer/c/dafsa.{c,h}` (copy of the PoC source); PoC `Makefile` gets a `sync` target.
- `web-archive-mcp` `rebuild()` and `dns-whois-mcp` `storage.py` are **unchanged**.
- Cosmocc is only used for the standalone PoC test binary.

### D9. Phasing (M0–M4) — see Part 3.

### D11. Standardize on `dafsa` prefix; remove legacy `dawg.c`
- The M0 refactor produced `dafsa.h`/`dafsa.c`/`dafsa_test.c` with an opaque `dafsa` type and a `dafsa_`-prefixed API.
- On 2026-08-06 the old `dawg.c` was deleted from the working tree and the repo standardized on `dafsa` (commit 27d6d62); build artifacts (`dafsa.aarch64.elf`, `dafsa.com.dbg`, `dawg.com.dbg`) were removed.
- `dawg.c` remains recoverable in git history (commit ccba17b).

### D12. C frontend replaces Rust CLI (2026-08-08)
- **Reverses D1/D8's "hybrid, not standalone" stance.** A pure-C `fst-indexer` CLI now lives in this repo as `fst-indexer.c`, built with cosmocc as a single portable APE binary alongside `dafsa.c` + vendored `cJSON.c` (for `manifest.json` read, jsonl `content` extraction, and search-result JSON).
- CLI and JSON output are **byte-compatible** with the Rust reference (`fst-indexer` in `palimpsest/fst-indexer`): same `build`/`search` args, same `{"query":...,"total_matches":N,"results":[...]}` stdout, same `manifest.json` structure, and the same v3 `index.fst` (verified: C- and Rust-built `index.fst` are byte-identical — matching md5; either binary can read the other's index).
- Ported to C: recursive glob file collection (fnmatch), txt/jsonl/transcript extractors, Tactiq parser, ASCII tokenizer, key = `{word}\0{file_idx:u32BE}{entry_idx:u32BE}`, sorted+deduped DAFSA insert, zero-copy `dafsa_view` search, AND/OR hit-set logic.
- **Documented divergences:** tokenization is ASCII-only (Rust uses Unicode alphanumeric/lowercase; multibyte UTF-8 chars become delimiters); `manifest.json` pretty-print uses TABS (cJSON) vs serde_json spaces (structure identical).
- This is the "standalone C binary" the roadmap deferred — now the frontend. The Rust CLI in `palimpsest/fst-indexer` is retained (unchanged) as reference; a future step can drop it.

### D13. jing-meta canonical home + Python frontend (2026-08-09)
- **Replaces D1/D8/D12 as the deployed approach.** `jing-meta` is now the canonical repo for the dafsa core; `carrasco-forcada-poc/` and `palimpsest/fst-indexer` are retained as historical R&D/testbed.
- The frontend here is **Python `ctypes`** over the C DAFSA core (`indexer/dafsa/__init__.py`), dropping the Rust dependency entirely — no Rust toolchain required.
- `indexer/__init__.py` + `indexer/__main__.py` replicate the Rust `fst-indexer` behavior **byte-for-byte** (same tokenizer/extractors/key format/manifest shape); verified hit-set parity on both Rust-built and Python-built indexes. `libdafsa.so` is built with `gcc -shared -fPIC -O2`.
- **Only `build` and `query` (search) are implemented in Python so far.** M3 (`update`, sidecars, tombstones, stable `file_idx`) is the main remaining milestone.

---

## Part 2 — Detailed file-by-file plan

## 0. Guiding decisions (locked)

- **Hybrid**: keep `fst-indexer` Rust binary, drop `fst` crate, compile C DAFSA via `cc` crate + FFI.
- **Tokenization/globbing/extractors/manifest-writing stay in Rust**, byte-for-byte unchanged.
- **True incremental**: persistent RW DAFSA, append-only slots + tombstones, per-file sidecar, `update` subcommand, drop hourly timer, keep nightly `build` compaction.
- **C source is portable C99**, compiled two ways: (a) system `cc` via `cc` crate for Rust binary; (b) `cosmocc` for standalone PoC test binary. **cosmocc is NOT used by `build.rs`.**
- `dafsa_delete_n` is load-bearing → randomized differential tests mandatory.

---

## 1. New DAFSA C API (`dafsa.h`)

Refactor to an **opaque handle** (`dafsa *`), heap-allocated/growable arrays, length-delimited key ops, persistence, and prefix enumeration. The PoC `main()` test harness moves to `dafsa_test.c`.

### 1.1 Header `dafsa.h`

```c
#ifndef DAFSA_H
#define DAFSA_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct dafsa dafsa;   /* opaque */

/* --- lifecycle --- */
dafsa *dafsa_create(void);                 /* empty DAFSA; NULL on OOM */
void   dafsa_free(dafsa *d);              /* NULL-safe */

/* --- persistence (read-write, mutable load) --- */
dafsa *dafsa_load(const char *path);       /* materialize on-disk form into a mutable DAFSA; NULL on error */
int   dafsa_save(const dafsa *d, const char *path); /* atomic: path.tmp, fsync, rename. 0=ok, -1=err */

/* --- length-delimited key ops (keys MAY contain NUL) --- */
int dafsa_add_n    (dafsa *d, const unsigned char *key, size_t len); /* 1 added, 0 dup, -1 err */
int dafsa_lookup_n (const dafsa *d, const unsigned char *key, size_t len); /* 1/0 */
int dafsa_delete_n (dafsa *d, const unsigned char *key, size_t len); /* 1 deleted, 0 absent, -1 err */

/* --- NUL-terminated convenience (legacy/PoC) --- */
int dafsa_add    (dafsa *d, const unsigned char *word);
int dafsa_lookup (const dafsa *d, const unsigned char *word);
int dafsa_delete (dafsa *d, const unsigned char *word);

/* --- prefix enumeration (W\0 semantics) ---
 * Enumerate keys of form: prefix || 0x00 || payload.
 * Walks prefix, requires a 0x00 edge next, then DFS, calling cb(payload, len)
 * for each final state reached. Returns count or -1. Stops early if cb != 0. */
typedef int (*dafsa_enum_cb)(const unsigned char *payload, size_t payload_len, void *user);
long dafsa_prefix_enum(const dafsa *d, const unsigned char *prefix, size_t prefix_len,
                      dafsa_enum_cb cb, void *user);

/* --- stats --- */
typedef struct {
    uint32_t n_states_total;     /* live + orphans (excludes sink 0) */
    uint32_t n_states_reachable; /* BFS from initial */
    uint32_t n_final;
    uint32_t n_trans;
    uint64_t register_probes;
} dafsa_stats_out;
void dafsa_stats(const dafsa *d, dafsa_stats_out *out);

/* --- debug --- */
void dafsa_dot(const dafsa *d, FILE *f);

#ifdef __cplusplus
}
#endif
#endif
```

### 1.2 Internal struct changes (in `dafsa.c`)

```c
struct dafsa {
    uint32_t nstates;          /* state 0 = sink sentinel */
    uint32_t initial;          /* == 1 after create/load */
    State   *states;            /* heap, capacity states_cap */
    size_t   states_cap;
    Inode   *inodes;            /* heap, capacity inodes_cap (index 0 = sentinel) */
    size_t   inodes_cap;
    uint32_t inodes_used;
    uint64_t *reg_keys;         /* open-addressing; capacity reg_cap */
    uint32_t *reg_vals;
    size_t   reg_cap;           /* grown; load factor kept < 0.7 */
    uint64_t reg_probes;
};
```

- `State` keeps the dense `Edge trans[ALPHABET_SZ]` in-memory (the incremental algorithm relies on sorted-array binary search + `memmove` insert). `ALPHABET_SZ` stays 256. State ≈ 2 KiB; with heap growth we can reach millions of states.
- `state_new` doubles `states` (initial cap e.g. 4096); `inode_alloc` grows `inodes`; `reg_insert`/`reg_lookup` grow + rehash when load factor > 0.7.
- Remove `MAX_STATES`, `REGISTER_SZ`; introduce `DAFSA_MAX_STATES_HARD = 100_000_000` as sanity abort.
- **Pointer-safety audit (load-bearing):** grep for every `State *`/`Inode *` held across a call that may realloc (`state_new`, `inode_alloc`, `reg_insert`-grow); convert dangling pointers to index-based re-fetch.

### 1.3 On-disk serialization layout (`index.fst`)

Magic `"PDWG"`, **version 3** (the only supported format; older v1/v2 files are rejected), all integers little-endian. Compact form: BFS-renumber reachable states to 1..N (initial → 1), **drop orphans** (refcount 0, unreachable). Orphans never serialized; load materializes only reachable states, so on-disk is always minimal (compaction on every save). The zero-copy `dafsa_view` reads the same layout directly off the mmap without materializing `State[]`/`Edge[]`.

```
HEADER (28 bytes)
  u8  magic[4]      = "PDWG"
  u32 version       = 3
  u32 n_states      ; live reachable states (ids 1..n_states); sink 0 not counted
  u32 n_trans       ; total outgoing transitions over live states
  u32 initial_id    ; == 1
  u32 n_final
  u32 reserved      = 0

STATE TABLE  (n_states+1) x 2 bytes, index 0 = sink (0), 1..n_states live:
  u16 ntrans        ; per-state transition count. u16 (not u8) so a state with
                    ; 256 outgoing edges (full byte alphabet) doesn't truncate.

FINAL BITMAP  ceil((n_states+1)/8) bytes, bit i set iff state i is final (bit 0 always 0).

CSR TRANSITIONS  grouped by state in state-table order, sorted by sym asc; edges are
variable-width (no per-edge padding):
  u8  sym
  uvarint target_id   ; LEB128 remapped live id (1..n_states), or 0 = sink
```

> **Format history:** v1 (8-byte state table rows `u32 trans_offset` + `u32 ntrans`, 5-byte CSR edges) and v2 (`u8 ntrans` + LEB128 CSR targets) are **removed**. v3 uses a `u16` per-state `ntrans` (fixes a save bug where a 256-edge state truncated to 0, leaving orphaned CSR bytes) and enforces that the CSR ends exactly at EOF. Loaders require `version == 3`; older files fail with a clean error. Verified byte-identical round-trip (Test 14) and 256-fanout round-trip (Test 17).

**Save algorithm:**
1. BFS from `initial`, collect reachable states in BFS order, build `old→new` id map (new ids 1..N, initial→1). Sink 0 → 0.
2. `n_trans` = Σ ntrans over reachable.
3. Write header, state table (entry 0 = 0, then each `u16 ntrans`), final bitmap (`bit[new_id] = old.is_final`), CSR (`sym, map[target]` per state in sorted order, targets LEB128-encoded).
4. Atomic: write `path.tmp`, `fsync`, `rename(path.tmp, path)`.

**Load algorithm (materialized `dafsa_load` / `dafsa_load_readonly`):**
1. Read & validate header (magic, `version == 3`), bounds-check every read.
2. `dafsa_create()`; grow `states` to `n_states+1`; set `nstates`, `initial`.
3. Read state table (skip entry 0); store `u16 ntrans` per id.
4. Read final bitmap → set `is_final`.
5. Read CSR → populate `states[i].trans[]` via direct copy (already sorted, no `trans_add`); set `ntrans`; `sig = 0`. Validate each LEB128 target ≤ `n_states`; verify CSR ends exactly at EOF.
6. If `mutable`: rebuild inodes (refcount + in_head) and register (sig → state).
7. Return handle.

**Zero-copy `dafsa_view_open`:** mmaps the file, validates the header, builds a single `state_off[n_states+2]` u64 byte-offset index into the CSR via one LEB128-skip pass, and validates every transition target ≤ `n_states` plus the `CSR ends at EOF` invariant. Search (lookup/prefix_enum) then reads `sym`/`target` directly off the mmap with no `State[]`/`Edge[]` materialization.

### 1.4 `dafsa_prefix_enum`

```
walk prefix from initial; if any transition missing → return 0
find a 0x00 edge from final prefix state; if none → return 0
DFS from that state, accumulating payload bytes; at each final state call cb(payload, len)
  (payload = bytes gathered after the 0x00 edge); if cb returns non-zero → stop, return count
return number of keys enumerated
```

Bounded by max key length (4096). For `fst-indexer` payload is always 8 bytes → DFS depth ≤ 8.

---

## 2. Sidecar format (per-file key store)

- **Location:** `<index_dir>/slots/<file_idx>.keys` — one per non-tombstoned slot. Absence == tombstone.
- **Purpose:** lets `update` reconstruct exact keys for a file so it can `dafsa_delete_n` them before re-adding. DAFSA key = `word || 0x00 || file_idx_be(4) || entry_idx_be(4)`. `file_idx` is the slot (known from path), NOT stored per-record.
- **Record layout** (LE, length-prefixed words):
```
; repeated until EOF:
  u32 entry_idx
  u32 word_len
  u8  word[word_len]
```
- **Dedup:** within one file, dedup `(entry_idx, word)` via a `HashSet<(u32, Vec<u8>)>` before writing sidecar and before `dafsa_add_n`. (`dafsa_add_n` is idempotent, but sidecar must be deduped to avoid redundant `dafsa_delete_n` and inflated size.)
- **Consistency invariant:** `slots/<file_idx>.keys` reflects the keys currently in the DAFSA for `file_idx`.
- **Crash-safety ordering:** DAFSA is saved *before* sidecars are overwritten, so a crash leaves (new DAFSA + old sidecar) → next `update` reads old sidecar, `dafsa_delete_n` of absent keys (no-op), re-extracts, re-adds → converges with no ghosts.

---

## 3. Manifest + file-slot management

### 3.1 `manifest.json` structure (backward-compatible shape)

```rust
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct FileEntry {
    pub filename: String,   // path relative to --dir (unchanged meaning)
    pub title: String,
    pub date: String,
    pub source: String,
    pub mtime: u64,         // last-indexed mtime, nanoseconds since epoch
    pub size: u64,          // last-indexed size in bytes
    pub tombstoned: bool,   // true ⇒ file removed; slot preserved
}
```

Tombstoned slot: `{"filename":"","title":"","date":"","source":"","mtime":0,"size":0,"tombstoned":true}`. Top-level shape unchanged: `{"files":[...]}`.

### 3.2 Slot rules
- `file_idx` is the slot index = position in `files[]`. **Never renumbered by `update`.**
- New file → `slot = files.len()` (append). Removed file → keep slot, tombstone entry, delete keys. Changed file → same slot, delete-old/add-new, update entry. Unchanged (`mtime` && `size` match) → skip.
- Compaction (`build`) → dense manifest, renumbers `file_idx` 0..N-1 by `collect_files` sort. **Only `build` renumbers.**

### 3.3 Python reads
`_load_manifest` unchanged. `resolve_file_idx` becomes tombstone-aware:
```python
def resolve_file_idx(index_dir, file_idx):
    files = _load_manifest(index_dir)
    if files is None or file_idx < 0 or file_idx >= len(files):
        return None
    fe = files[file_idx]
    if fe.get("tombstoned") or not fe.get("filename"):
        return None
    return fe["filename"]
```
> **M3 update (D13/Option C):** `filename` is the path relative to `--dir` and is the **stable match key** for `update`. `_fix_dirs_manifest` was **deleted** — it mutated the canonical field, destroying the match key on the next update. The server derives the session-id via `Path(fname).parent.name` at read time (see §7.1).

---

## 4. `update` subcommand

### 4.1 CLI
```
fst-indexer update -i <index_dir> --dir <root> --pattern <glob> --extractor <jsonl|txt|transcript>
```
Exit 0 on success, non-zero on error. Stdout JSON:
```json
{"command":"update","index_dir":"<path>","unchanged":N,"updated":N,"added":N,"removed":N,"total_slots":N,"dafsa_states_reachable":N}
```

### 4.2 Algorithm
```
update(index_dir, dir, pattern, extractor):
  if not exists(index_dir/index.fst):
      return build(dir, pattern, extractor, index_dir)   # fresh, dense (first run)
  d = dafsa_load(index_dir/index.fst)
  manifest = load index_dir/manifest.json
  live = { manifest[i].filename : i for i if not manifest[i].tombstoned }
  disk = collect_files(dir, pattern)

  for f in disk:
      rel = f.strip_prefix(dir)
      stat = f.metadata()
      if rel in live:
          slot = live[rel]; prev = manifest[slot]
          if prev.mtime == stat.mtime and prev.size == stat.size: continue  # unchanged
          # CHANGED: delete old keys (from sidecar), re-extract, add new keys, rewrite sidecar+entry
          for (ei, w) in read slots/<slot>.keys:
              dafsa_delete_n(d, w\0 + slot_be + ei_be)
          (fe, entries) = extract(extractor, f, rel)
          pairs = dedup_per_entry(entries)
          for (ei, w) in pairs: dafsa_add_n(d, w\0 + slot_be + ei_be)
          manifest[slot] = fe with mtime/size/tombstoned=false
      else:
          slot = len(manifest)   # NEW FILE → append
          (fe, entries) = extract(extractor, f, rel); pairs = dedup_per_entry(entries)
          for (ei, w) in pairs: dafsa_add_n(d, w\0 + slot_be + ei_be)
          manifest.push(fe with mtime/size/tombstoned=false)

  disk_rel = set(rel for f in disk)
  for i in 0..len(manifest):
      if not manifest[i].tombstoned and manifest[i].filename not in disk_rel:
          # TOMBSTONE: delete keys, remove sidecar, set tombstone
          for (ei, w) in read slots/<i>.keys: dafsa_delete_n(d, w\0 + i_be + ei_be)
          manifest[i] = tombstone entry

  # Commit ORDER: (a) dafsa_save → (b) sidecars → (c) manifest
  dafsa_save(d, index_dir/index.fst.tmp) → fsync → rename
  write sidecars for changed/new; unlink sidecars for tombstoned
  write index_dir/manifest.json.tmp → fsync → rename
```

### 4.3 Crash-safety ordering
DAFSA save → sidecar writes → manifest write. A crash between (a)-(b) leaves new DAFSA + old sidecar → self-heals on next `update` (no-op deletes, converge). Crash between (b)-(c) leaves new DAFSA + new sidecars + old manifest → only risk is a transient duplicate slot for a newly-added file, healed by nightly `build`. Concurrent readers (search) read only `index.fst` + `manifest.json`, never the sidecar, so they're safe throughout.

**Known limitation:** no WAL; full crash-consistency provided by nightly `build` compaction. (Stronger guarantee → add a `generation` file written last; deferred to post-M4, open question Q5.)

### 4.3a Crash-consistency analysis (M4)
**`dafsa_save` atomicity (verified empirically by `indexer/dafsa/_crash_test.c`).** `dafsa_save` (`dafsa_persist.c`) writes `PATH.tmp`, `fflush`+`fsync`s it, `rename`s it onto `PATH`, then `fsync`s the containing directory. The rename is the single atomic point of visibility, so a reader/loader at `PATH` sees **either the complete old index or the complete new index — never a partial write**. Crash windows and their observable effect:

| # | Window | On-disk state after kill -9 | Recovery |
|---|--------|------------------------------|----------|
| 1 | writing `PATH.tmp` | `PATH` = old (valid); stray `PATH.tmp` left behind | `.tmp` ignored; next save overwrites / cleanup removes it |
| 2 | `fflush` + `fsync(PATH.tmp)` | `PATH` = old (valid); new data fully buffered but not renamed | same as #1 |
| 3 | `rename(PATH.tmp → PATH)` | `PATH` = complete new index (rename is atomic) | none needed |
| 4 | `fsync_dir_of(PATH)` | `PATH` = new index, but the rename may not survive a power loss (only a process kill) | nightly `build` / compaction rebuilds |

**Test harness (`_crash_test.c`, M4):** builds a 200-original-key index, then runs 100 trials where a child loads the index, adds 100 distinguishable new keys (leading `0xFF`), re-saves atomically, and the parent `SIGKILL`s it after a random 500–50,000 µs delay (landing in one of the four windows above). After each kill it validates: `dafsa_load` must succeed, all 200 originals must be present, and the count of new keys must be **exactly 0 or exactly 100** (never between). **Result: 100/100 trials passed — new=0 on 12 trials, new=100 on 88 trials, 0 partials.** Built clean with `-Werror`; confirms `dafsa_save` is atomic and the commit path contains no observable partial-index window.

**Python `update()` 3-phase self-healing (M3, §4.2/§4.3).** `update()` commits in three ordered phases: **(a)** `dafsa_save` of the new index, **(b)** per-file sidecar writes, **(c)** atomic manifest write. Crash between (a)-(b): new DAFSA + old sidecars → next `update` re-reads old sidecars, performs no-op deletes of the (already-absent) old keys and re-adds the new ones → converges. Crash between (b)-(c): new DAFSA + new sidecars + old manifest → the only risk is a transient duplicate slot for a newly-appended file, healed by the nightly `build` compaction. Concurrent readers (`search`) read only `index.fst` + `manifest.json`, never the sidecar, so they are safe throughout. Combined with the `dafsa_save` atomicity above, the index at `PATH`/`index.fst` is never left partially-written by a crash.

### 4.4 Error handling
- Missing `index.fst` → fall back to `build` (first run). Corrupt `index.fst` → hard error (stderr + non-zero exit), recovered by compaction/manual `build`.
- `dafsa_delete_n` returning 0 (key absent) → warning, not fatal (orphan healing).
- Per-file extraction errors mirror `build`'s behavior (skip file's entries, emit empty slot).
- Atomic writes: `.tmp` + `fsync` + `rename` for every file.

---

## 5. `build` subcommand changes

`build` becomes initial + nightly compaction (dense, no tombstones, renumbers).

- Drop `BTreeSet<Vec<u8>>` (keys). DAFSA is order-independent (PoC Test 8) and `dafsa_add_n` idempotent → no pre-sort/dedup of composite keys. Per-file `(entry_idx, word)` dedup via `HashSet` replaces it.
- Drop `fst::SetBuilder`/`write_fst` → `dafsa_save`.
- Add sidecar writing (`output/slots/<idx>.keys`).
- `FileEntry` gains `mtime`/`size`/`tombstoned` (build always writes `tombstoned:false`).
- Atomic `.tmp`+`fsync`+`rename` for `index.fst` and `manifest.json`.
- `report_size` reports DAFSA size + manifest size + sidecar total.

`Index::open` calls `Dafsa::load` instead of `fst::Set::new`. `Index::search` uses `dafsa_prefix_enum` per query word, collecting payloads into `HashSet`, then the **existing** AND/OR intersect-union / sort-by-(file_idx DESC, entry_idx ASC) / truncate logic is kept **verbatim**.

---

## 6. Rust FFI layer

> **⚠ HISTORICAL (superseded by D13).** This section describes the Rust `ffi.rs`/`build.rs`/`cc`-crate layer from the pre-jing-meta plan. It was implemented and verified (M2), but the jing-meta frontend is now **Python `ctypes`** (`indexer/dafsa/__init__.py`), so this Rust layer is retained only as reference in `palimpsest/fst-indexer`. The Python wrapper is far thinner than the Rust FFI; no `build.rs`, no `cc` crate, no `#[repr(C)]` structs required.

### 6.1 `build.rs`
```rust
fn main() {
    cc::Build::new()
        .file("c/dafsa.c")
        .include("c")
        .flag_if_supported("-std=c99")
        .warnings(true)
        .extra_warnings(true)
        .compile("dafsa");
    println!("cargo:rerun-if-changed=c/dafsa.c");
    println!("cargo:rerun-if-changed=c/dafsa.h");
}
```
The `cc` crate probes `$CC`, then `cc`, `gcc`, `clang`. It must NOT pick up cosmocc (cosmocc emits APE objects that won't link into a native Rust binary). Ensure a real `gcc`/`clang` is installed; set `CC=gcc` if needed. **Verify in M2 (Q2).**

### 6.2 `Cargo.toml`
- Remove `fst = "0.4"`. Add `cc = "1"` (build-dependency). Keep `clap`, `serde`, `serde_json`, `anyhow`, `glob`, dev `tempfile`. Add `libc` only if needed for fsync (prefer `File::sync_all()`).

### 6.3 `src/ffi.rs` (new)
`extern "C"` block declaring all `dafsa_*` functions (see §1.1) + a safe `Dafsa` wrapper:
- `Dafsa::create()`, `Dafsa::load(path)`, `Dafsa::save(path)`, `Dafsa::add_n(&[u8])`, `Dafsa::delete_n(&[u8])`, `Dafsa::prefix_enum<F: FnMut(&[u8])>(&[u8])` (uses a stack trampoline passing the closure via `user` pointer), `Dafsa::stats()`, `Drop` frees.
- `#[repr(C)] struct DafsaStatsOut { ... }`.
- Not `Send`/`Sync` (CLI is single-threaded).

### 6.4 `Index::search` using `prefix_enum`
```rust
for word in &query_words {
    let mut hits: HitSet = HitSet::new();
    self.dafsa.prefix_enum(word.as_bytes(), |payload| {
        if payload.len() == 8 {
            let fi = u32::from_be_bytes(payload[0..4].try_into().unwrap());
            let ei = u32::from_be_bytes(payload[4..8].try_into().unwrap());
            hits.insert((fi, ei));
        }
    })?;
    if hits.is_empty() && !opts.any { return Ok(Vec::new()); }
    if !hits.is_empty() { word_sets.push(hits); }
}
// ... identical AND/OR intersect/union + sort + truncate ...
```
`prefix_enum("ca")` returns no payloads → `query "ca"` yields no `cat` hits (parity with `fst` startswith-break). **Add an explicit test for this.**

### 6.5 `DafsaView` (read-only, optional — defer to M4)
A `DafsaView` that loads only the compact CSR + final bitmap (no inode/register rebuild) for faster `Index::open`/search and lower memory. Not required for correctness.

---

## 7. Python side changes

### 7.1 `unified-history-mcp/src/unified_history_mcp/indexer.py`
- `_load_manifest` (line 39): unchanged.
- `resolve_file_idx` (line 153): tombstone-aware (returns None if `tombstoned` or no `filename`).
- **`resolve_file_idx` (line ~134):** tombstone-aware (returns None if `tombstoned` or no `filename`). **Unchanged** in shape — returns the relative-path `filename`.
- `build_index` (line 55): unchanged (still calls `build` — initial + compaction).
- **New `update_index(cfg, index_dir=None)`** mirroring `build_index` but calling in-process `indexer.update` (no subprocess), then returns a summary message. **`_fix_dirs_manifest` was DELETED (M3/Option C)** — no dirs-domain manifest rewriting; `filename` stays the relative path.
- `search_fst` (line 118): unchanged (results may include tombstoned `file_idx` → `resolve_file_idx` returns None → caller filters).
- **`server.py` hit resolution (`r_domain == "sessions"`):** derives `sess_id = Path(fname).parent.name` from the relative-path `filename`, filters cwd via `dir/sess_id`, sets `file_path = dir/fname` (points directly at `messages.jsonl`), and returns `file_id = sess_id` for sessions. This replaces the old `dir/fname` + `_fix_dirs_manifest` flow.

### 7.2 `palimpsest-setup/scripts/palimpsest-reindex.py` (UNVERIFIED — open first, Q4)
- Per-domain call switches `build_index` → `update_index` (hourly).
- Add nightly compaction path (`build_index`), either separate `palimpsest-compact.py` or a `--compact` flag, scheduled `OnCalendar=03:00`.
- Drop the single hourly-build timer; install both unit pairs (hourly update + daily compact). Update `setup.sh` accordingly.

### 7.3 `web-archive-mcp/src/web_archive_mcp/server.py` (585-628)
**Unchanged.** `rebuild()` hardcodes `"fst-indexer"` (not `cfg.fst_binary`) and calls `build` = compaction — correct for web-archive's append-only JSONL model.

### 7.4 `dns-whois-mcp/src/dns_whois_mcp/storage.py` (line 130)
**Unchanged.** Writes `content` field for the jsonl extractor (parity-preserved).

---

## 8. File-by-file inventory

### 8.1 `carrasco-forcada-poc/` (R&D testbed, cosmocc-built)
| File | Action | What changes |
|---|---|---|
| `dafsa.h` | CREATE | Public API header per §1.1. |
| `dafsa.c` | MODIFY (large) | Opaque `struct dafsa`; heap arrays + grow; `_n` ops; load/save/prefix_enum/stats; BFS-renumber save; load materialize; remove `main()` (moves to `dafsa_test.c`). |
| `dafsa_test.c` | CREATE | Old `main()` (tests 1-9) + round-trip, prefix W\0 semantics, embedded-NUL, randomized delete differential, enumeration correctness. |
| `Makefile` | MODIFY | Build `dafsa.c`+`dafsa_test.c` with cosmocc; `test`; add `sync` target copying `dafsa.{c,h}` → `fst-indexer/c/`. |

### 8.2 `palimpsest/fst-indexer/`  [⚠ HISTORICAL — superseded by D13]
> Retained as reference only. The canonical jing-meta frontend is Python (see 8.6); the Rust binary still works and is the live `/home/arch/.local/bin/fst-indexer`, but no new work targets it.

| File | Action | What changes |
|---|---|---|
| `Cargo.toml` | MODIFY | Remove `fst`, add `cc` build-dep. |
| `build.rs` | CREATE | `cc` crate compiles `c/dafsa.c`. |
| `c/dafsa.c`, `c/dafsa.h` | CREATE (vendored) | Byte-identical to PoC source, synced via `make sync`. |
| `src/ffi.rs` | CREATE | `extern "C"` + safe `Dafsa` wrapper. |
| `src/lib.rs` | MODIFY (large) | Drop fst; `Index { dafsa, manifest }`; `FileEntry` +3 fields; rewrite `build`/`open`/`search`; new `update`; sidecar/atomic-write/composite_key helpers. |
| `src/main.rs` | MODIFY | Add `Update` CLI variant. |

### 8.3 `palimpsest/unified-history-mcp/src/unified_history_mcp/`
| File | Action | What changes |
|---|---|---|
| `indexer.py` | MODIFY | Tombstone-aware `resolve_file_idx`; `update_index`; **`_fix_dirs_manifest` DELETED** (Option C). |

### 8.4 `palimpsest-setup/` (UNVERIFIED — Q4)
| File | Action | What changes |
|---|---|---|
| `scripts/palimpsest-reindex.py` | MODIFY | `build_index` → `update_index`; add compaction mode. |
| `scripts/palimpsest-compact.py` | CREATE | Calls `build_index` nightly. |
| systemd units | MODIFY | Hourly `update` + daily `build` (03:00). |
| `setup.sh` | MODIFY | Install both unit pairs; drop single hourly-build timer. |

### 8.5 `web-archive-mcp/`, `dns-whois-mcp/`
| File | Action |
|---|---|
| `web-archive-mcp/.../server.py` (`rebuild` at 585-628) | Unchanged (build = compaction). |
| `dns-whois-mcp/.../storage.py` (line 130) | Unchanged. |

### 8.6 `jing-meta/` (CANONICAL — D13)
| File | Action | What changes |
|---|---|---|
| `indexer/dafsa/dafsa.c`, `dafsa.h` | COPIED | Byte-identical C core from `carrasco-forcada-poc`; canonical copy lives here. |
| `indexer/dafsa/__init__.py` | DONE | Python `ctypes` `Dafsa` wrapper (create/load/save/add/lookup/delete/prefix_enum). |
| `indexer/dafsa/libdafsa.so` | BUILT | `gcc -shared -fPIC -O2 -o libdafsa.so dafsa.c`. |
| `indexer/__init__.py` | DONE | `build` + `Index.search` (AND/OR), byte-for-byte parity with Rust. |
| `indexer/__main__.py` | DONE | CLI: `build`, `query`. **TODO:** add `update` (M3). |
| `indexer/dafsa/dafsa_test.c`, `Makefile` | MISSING | Not vendored here yet; live in `carrasco-forcada-poc` (M4: vendor + `tests/`). |
| `jing_meta/search` integration | TODO | `update_index` wiring (M3). |

---

## 9. Testing strategy

### 9.1 PoC C tests (`dafsa_test.c`, `make test` with cosmocc)
- Existing 9 tests ported to the opaque API.
- Round-trip: add N words → save → free → load → verify lookups + reachable counts.
- Prefix semantics: `dafsa_prefix_enum("cat")` returns cat payloads; `("ca")` and `("c")` return **zero**; `("cart")` returns cart payloads.
- Embedded NUL via `_n`.
- **Randomized delete differential** (load-bearing): for T trials, random universe of K random words; build via `dafsa_add`; maintain a parallel `HashSet`; delete random subset; assert DAFSA lookup == reference for every word; re-add random subset; assert again. Seeded for reproducibility.
- Enumeration correctness: for random prefixes, assert `prefix_enum` returns exactly the matching `prefix\0*` keys.

### 9.2 Cargo tests (`fst-indexer`, `cargo test`)
- **Existing `lib.rs` tests kept green** (parity gate).
- `prefix_enum` parity: 2-file txt corpus with "cat","car","cart"; assert `search("ca")` = 0, `search("cat")` = cat hits.
- `update == build` convergence: (a) build; (b) update with no changes → identical search results (+ reachable-state counts); (c) modify file → search reflects new content, drops old; (d) delete file → tombstoned, no hits; (e) add file → appended slot, searchable.
- Tombstone stability: delete + re-add same filename → slot preserved (only `build` renumbers).
- Sidecar idempotency: `update` twice with no changes → zero mutations.
- Atomic write: no `.tmp` files remain.

### 9.3 Differential vs. live `fst-indexer` (`/home/arch/.local/bin/fst-indexer`)
**Cannot run in sandbox** (live binary blocked). Provide `carrasco-forcada-poc/scripts/diff_vs_live.sh` to run outside the sandbox: build both binaries on a synthetic `/tmp` corpus, run Q queries (AND/OR, varied `--max`), diff normalized JSON. Zero mismatches = parity.

### 9.4 Real-corpus differential (outside sandbox)
Point both binaries at the real sessions / web-archive dirs (per `~/.config/unified-history-mcp/config.toml`), build both, run the same battery, diff.

### 9.5 `MAX_STATES` measurement (required, Q1)
After M2, run `target/release/fst-indexer build` on the real corpora and read `dafsa_states_reachable` from summary JSON. Confirm comfortably below `DAFSA_MAX_STATES_HARD`. If > ~5M states, revisit the dense `trans[256]` (2 KiB × 10M ≈ 20 GiB RAM).

**MEASURED (2026-08-09, `_bench_states.c`):** synthetic prefix-heavy corpus of **300,000** composite keys (`word\0file:u32BE entry:u32BE`, 1000 files, 100 shared stems, deterministic seed `0x5EED5EED`). Result: `n_states_total` **1,391,245**, `n_states_reachable` **1,391,239**, `n_trans` **1,691,003**, `n_final` 1 (all composite-key terminal paths converge to one shared final state — inherent to the `\0`-delimited scheme, not an error; verified 300,000/300,000 lookups hit). Estimated resident RAM ≈ **127.5 MiB** (states 64B×n ≈ 84.9 MiB + inodes 12B×n_trans ≈ 19.4 MiB + register ≈ 21.2 MiB + scratch ≈ 2 MiB). Headroom vs `DAFSA_MAX_STATES_HARD` (100,000,000): **98.61%**, i.e. **~6.31 GB at 64 B/state** — far above the ~5M-state / 10 GiB dense-`trans[256]` risk line, so the sparse-heap + inline-≤4 edge design (Phase 3) is comfortably viable. Even a 10× larger corpus (~14M states) stays well under the hard cap.

---

## 10. Phasing (M0..M4)

### M0 — C core refactor (heap + opaque + `_n` APIs)
**Files:** `dafsa.h` (new), `dafsa.c` (refactor), `dafsa_test.c` (new — old tests ported), `Makefile`.
**Steps:** extract header; opaque struct; heap arrays + grow; `_n` ops (strlen wrappers delegate); `create`/`free`; move `main()` to `dafsa_test.c`; port 9 tests; pointer-dangling audit.
**Done when:** `make test` (cosmocc) passes 9 existing tests + new `_n` embedded-NUL test. save/load/prefix_enum are stubs returning -1.

**STATUS: DONE (2026-08-06).** All 10 tests pass (verified via native ELF `dafsa.com.dbg` in-sandbox; APE `./dafsa` cannot fork under sandbox pledge but `.com.dbg` runs). `-Werror` clean. Reviewer found no blockers; pointer safety exemplary (all re-fetched by index across realloc). Applied reviewer SF3/SF4 NULL-safety guards to `dafsa_add_n`/`delete_n`/`lookup_n`. SF1 (register stale entries from merges) is pre-existing in original `dawg.c`, cleaned by M1's register-rebuild-on-load + nightly `build` — deferred, not a blocker. Files: `dafsa.h` (2916B), `dafsa.c` (31047B), `dafsa_test.c` (13464B), `Makefile` (324B). Note: `dawg.c` was later removed (2026-08-06) and the repo standardized on `dafsa`; it remains in git history (ccba17b) for reference. (Note: actual filenames use `dafsa` prefix, not the `dawg` prefix used in the original M0 spec.)

### M1 — C persistence + prefix enumeration
**Files:** `dafsa.c` (save/load/prefix_enum/stats), `dafsa_test.c` (round-trip, prefix, delete-differential).
**Steps:** `dafsa_save` (BFS-renumber, header, state table, final bitmap, CSR, atomic write); `dafsa_load` (materialize, rebuild inodes, rebuild register); `dafsa_prefix_enum`; `dafsa_stats(out)`; tests.
**Done when:** `make test` green; round-trip preserves lookup set across save/load for ≥10 trials of ≥1000 random keys; delete differential passes ≥50 trials; `prefix_enum` matches brute-force for ≥20 prefixes.

**STATUS: DONE (2026-08-07).** Implemented `dafsa_save`/`dafsa_load`/`dafsa_prefix_enum`; `make test` passes all 14 tests (10 M0 + 4 M1: round-trip ×10 trials ×1200 keys, delete differential ×60 trials ×200 words, prefix enum ×28 prefixes incl. explicit `ca`→0, PDWG byte-identical determinism). On-disk header is **28 bytes** (see §1.3). **Pre-existing incremental bugs fixed** (exposed by the mandatory delete differential — this was the flagged `dafsa_delete_n` risk): (1) `delete_n` Phase 2 now clones **ascending** (was descending, corrupting shared sub-automata) and uses updated `path[di-1]`; (2) `add_n` now clones shared ancestors on re-add (was only cloning the terminal); (3) register staleness (pre-existing SF1) fixed via `reg_rebuild()` after confluence in both `add_n` and `delete_n`. **Known cost:** `reg_rebuild()` is O(nstates) per op — see Q12. **Note:** the `reg_rebuild()` step referenced here was **later removed** in favour of incremental register maintenance (stale-entry validation in `replace_or_register`) — see Q12 (RESOLVED); this M1 note describes an earlier draft. **Phase 2 perf/refactor DONE (2026-08-09):** (a) `DAFSA_DEBUG` invariant checker (`dafsa_check_invariants`); (b) dropped dead `chars` param from `confluence_path`; (c) heap-allocated per-handle path scratch (`d->spath/schars/sparents` + `dafsa_ensure_scratch`, `malloc+memcpy+free` to avoid partial-OOM danglers); (d) 32-bit fold in `sig_compute` (signature formula changed — reload mutable handles after deploy); (e) orphan state **free-list** (`free_head` via `State.sig`, `state_new` recycles, `state_free` + `state_detach_from_children` removes phantom inodes) — caps orphan accumulation ~800× (churn test: nstates_total 182k→~220). Reviewer: no blockers; fixed S1 (hard-limit moved after free-list pop), S2 (phantom-inode detach), S3 (`reg_lookup_no_count` in checker). All 17 tests pass with `-O2 -Wall -Wextra -Werror` and `-DDAFSA_DEBUG`; 60k-op churn differential passes with per-op invariant check. `dafsa_test.c` vendored into this repo (was only in carrasco-forcada-poc). **Phase 3 DONE (2026-08-09):** (A1) inline ≤4 transitions — `State` is now 64 B / one cache line (refcount@0, is_final@4, ntrans@8, in_head@12, sig@16, `TransHeap* trans_heap`@24 NULL⇒inline, `Edge trans[4]`@32); every transition access routes through `static inline trans_arr`/`trans_arr_c`; `trans_reserve` promotes inline→heap (malloc+memcpy of the ≤4 edges into a `TransHeap` FAM) or reallocs, `trans_add` re-fetches the array after reserve. Kills the second dependent cache load for the ~95% of states with ≤4 edges. (B3) dropped redundant `State.id`. (R3) lazy `dafsa_stats` (stats_reachable/final/trans + stats_valid, invalidated in add_n/delete_n/load_impl). (R1) split the 2085-line monolith into `dafsa.c`/`dafsa_state.c`/`dafsa_core.c`/`dafsa_persist.c`/`dafsa_view.c` + `dafsa_internal.h` + `Makefile` (multi-TU, `debug`/`asan`/`clean`). On-disk PDWG format and `dafsa_view` mmap path untouched. Reviewer: SAFE TO SHIP, no blockers; applied should-fixes (dropped redundant `dafsa_load_impl` forward-decl; added `n_states > DAFSA_MAX_STATES_HARD` cap in load) + nits. All 17 tests pass in both modes; adversarial A1 stress (inline↔heap churn, clone-of-heap-state, 256-fanout round-trip) and 60k-op churn differential pass with per-op invariant check. Build in this repo now uses `gcc -I. -o _t dafsa_test.c dafsa.c dafsa_state.c dafsa_core.c dafsa_persist.c dafsa_view.c` (or `make _t`; sandbox `make` needs `CC` that can read system headers).

### M2 — Rust FFI + `build`/`search` on DAFSA, drop `fst` crate
**Files:** `Cargo.toml`, `build.rs` (new), `c/dafsa.{c,h}` (vendored via `make sync`), `src/ffi.rs` (new), `src/lib.rs` (rewrite build/open/search, drop fst), `src/main.rs` (unchanged CLI).
**Steps:** `make sync`; add `cc` build-dep + build.rs; write `ffi.rs`; rewrite `build`/`open`/`search`; `cargo test`; verify native `cc` (Q2); run differential script on `/tmp` corpus.
**Done when:** `cargo test` green (parity gate); native binary builds; differential vs live `fst-indexer` on `/tmp` = 0 mismatches across ≥100 queries.

**STATUS: SUPERSEDED by D13.** The Rust FFI + `build`/`search`-on-DAFSA was implemented (22 tests green, clippy clean, byte-identical `index.fst`) — but the jing-meta pivot (D13) **replaces the Rust frontend with Python `ctypes`**. The functional equivalent (build/search on DAFSA) is **done in Python** (`indexer/__init__.py` + `__main__.py`), verified byte-for-byte. The Rust code remains in `palimpsest/fst-indexer` as reference only.

### M3 — `update` subcommand + sidecar + manifest tombstones + Python  [**MAIN REMAINING MILESTONE**]
**Files:** Rust plan: `src/lib.rs` (`Index::update`, sidecar/atomic/composite helpers, `FileEntry` +3 fields), `src/main.rs` (`Update` variant), `indexer.py` (tombstone-aware + `update_index`), `palimpsest-setup/` (reindex→update, nightly compact) — Q4 caveat. **In jing-meta (D13): implement in Python** — extend `indexer/__init__.py` `FileEntry` dataclass with `mtime`/`size`/`tombstoned`; add `slots/<file_idx>.keys` sidecar read/write + composite-key + atomic-write helpers; add `Index::update` (§4.2/§4.3); wire an `update` subcommand in `indexer/__main__.py`; add `update_index` in `jing_meta/search`-side integration.
**Steps:** extend `FileEntry`; sidecar read/write + `composite_key` + `atomic_write`; `Index::update` (§4.2/§4.3); `Update` CLI; tests (update==build, modify/delete/add, tombstone stability, sidecar idempotency); Python `update_index`; systemd timers.
**Done when:** `update` then `search` matches fresh `build`'s search for same corpus; modify/delete/add update correctly with stable `file_idx`; Python `update_index` works end-to-end on a test domain; nightly compaction timer installed.

**STATUS: DONE (2026-08-09).** Implemented in Python: `update(dir, pattern, extractor, output)` in `indexer/__init__.py`, `update` subcommand in `indexer/__main__.py`, `update_index` + tombstone-aware `resolve_file_idx` in `search/indexer.py`, `update_index` import in `search/server.py`. `build` now emits per-file sidecars (`slots/<i>.keys`), records `mtime`/`size`/`tombstoned`, and writes the manifest atomically. **Design decision (Option C, per advisor):** the manifest `filename` is the path **relative to `--dir`** everywhere (ROADMAP §3.1) and is the stable match key for `update`; `_fix_dirs_manifest` was **deleted** (it mutated the canonical field in place, destroying the match key — verified idempotent `update` on a `dirs` corpus returned `added:2,removed:2` before the fix). The server derives the session-id via `Path(fname).parent.name` at read time in the `r_domain == "sessions"` branch (`server.py`), and sets `file_id = sess_id` for sessions. Deleting `_fix_dirs_manifest` also removed the only non-atomic manifest write in the pipeline (it did a bare `write_text` after `update`'s atomic commit). **Q12 (register-maintenance cost) is RESOLVED — the C core already uses incremental register maintenance (no `reg_rebuild`); see Q12 entry for the empirical verification.** Tests: `tests/test_indexer.py` (12 cases incl. nested-dir + dirs-idempotency + tombstone-aware `resolve_file_idx`). NOTE: pre-fix basename-stored sessions indexes need a one-time `build` rebuild.

### M4 — Hardening, real-corpus, headroom, optional `DafsaView`
**Files:** `scripts/diff_vs_live.sh`, `fst-indexer` (optional `DafsaView`), docs/READMEs.
**Steps:** real-corpus differential; `MAX_STATES` measurement; crash-consistency review (simulate kill -9 between commit phases); (optional) `DafsaView` read-only fast path; (optional) switch web-archive `rebuild()` to `update`; update README/ROADMAP in both repos.
**Done when:** real-corpus differential clean; state-count recorded and within headroom; crash review documented; READMEs updated.

**STATUS: DONE (2026-08-09).** All four hardening items committed and verified:
1. **Q1 state-count/headroom measurement** — `indexer/dafsa/_bench_states.c` (synthetic 300k-key, prefix-heavy corpus, deterministic seed). Measured `n_states_reachable` **1,391,239**, `n_trans` **1,691,003**, ≈ **127.5 MiB** RAM, headroom **98.61%** (~6.31 GB at 64B/state) — see §9.5. 300k/300k lookups verified.
2. **Crash-consistency (kill -9) review** — `indexer/dafsa/_crash_test.c`: 100 trials, **0 bad** (new=0 on 12, new=100 on 88) proving `dafsa_save` atomicity; documented in §4.3a.
3. **Real-corpus differential** — `scripts/diff_vs_live.sh` (Rust `fst-indexer` vs Python `jing-meta` indexer; runs outside sandbox; `chmod +x`).
4. **READMEs/ROADMAP updated** — this status, §4.3a, and §9.5.

✅ The optional `DafsaView` zero-copy mmap fast path is done (in `dafsa_view.c`; Python `Index` opens read-only via `Dafsa.load(..., readonly=True)` → uses `dafsa_view_open`). Both C harnesses (`_bench_states.c`, `_crash_test.c`) build clean with `gcc -O2 -Wall -Wextra -Werror` and pass. Historical note preserved: the earlier PARTIAL status was superseded by this DONE entry.

---

## 11. Risks & open questions

- **Q1 (CRITICAL, measurement required):** reachable-state count on real corpus vs dense `trans[256]` ~2 KiB/state RAM. Measure at M2 gate; if >5M states (~10 GiB), branch M2.5 to convert `trans[]` to heap sparse. On-disk CSR is already sparse, so disk is fine regardless.
- **Q2 (build-blocking):** `cc` crate must pick native `gcc`/`clang`, not cosmocc (sandbox aliases all to cosmocc, which emits APE objects that won't link into a native Rust binary). Verify at start of M2; set `CC=gcc` if needed.
- **Q3:** orphan accumulation within a single `update` process (never serialized; fresh process per update; nightly build resets). Acceptable for hourly cadence.
- **Q12 (2026-08-07, performance) — RESOLVED 2026-08-09:** the original concern was that `reg_rebuild()` was called after every confluence in `add_n`/`delete_n`, making each op O(nstates) and a full build O(N²). **The committed `dafsa.c` already implements option (a)** — `reg_rebuild` no longer exists; `replace_or_register` maintains the register incrementally by validating each register hit at lookup time: a hit is only a valid merge target if the named state is still live (`refcount != 0 || == initial`) **and** still carries the exact signature (lines 614-634). Verified empirically (gcc `-O2`, sandbox): (1) `replace_or_register`/`confluence_path`/`state_new` counters stay **constant** for a fixed 20k add-delta against growing bases 50k→400k (~179k sig_compute & reg_lookup calls, ~95k state_new, ~0 reg_grow) → per-op cost is O(1)-amortized, no O(nstates) step; (2) all 17 real C tests pass (round-trip, delete-differential ×60, prefix enum, PDWG determinism, zero-copy view, 256-fanout); (3) realistic prefix-sharing corpus (300k words): build 1.38s, incremental add 9.6µs/op, delete 14.4µs/op. The only wall-clock superlinearity observed is **cache/memory latency** on a multi-megastate sparse array (~100MB), which is inherent to a large automaton, not an algorithmic O(nstates)/op — so no code change is needed. (Historical note: the M1 STATUS below describing `reg_rebuild()` reflects an earlier draft; the refactor to incremental maintenance superseded it.)
- **Q4 (RESOLVED 2026-08-06):** `palimpsest-setup/scripts/palimpsest-reindex.py` and `setup.sh` **verified** via `bash` (the sandbox could not reach them, but the read-only bash tool could). Exact facts:
  - `palimpsest-reindex.py` (29 lines): imports `load_config` + `build_index`; loops `cfg.domains.items()`, calls `build_index(dc)` at line 20, returns 0 unless `"not found"` in msg. **Matches the roadmap's §7.2 assumption exactly.**
  - `setup.sh` (181 lines): step 3 builds `fst-indexer` via `cargo build --release` + `install` to `~/.local/bin/fst-indexer` (lines 44-46). Step 8 writes the systemd units via heredocs — `palimpsest-reindex.{service,timer}` at lines 104-119 (hourly `OnCalendar=*:0`), `summarize` (121-136), `gardener` nightly 02:00 (138-153), `subagent-meta` nightly 03:00 (155-170); `enable --now` all four at 172-175. Scripts installed via `install -m 0755` at 82-89.
  - **Live state confirmed:** `systemctl --user list-timers 'palimpsest-*'` shows all 4 timers active; `palimpsest-reindex.timer` fires hourly (`*:0`, last ran 17:00:03 UTC). Live unit files match the heredocs (ExecStart=`$VPY ~/.local/bin/palimpsest-reindex.py`).
  - **`config.py` verified** (unified-history-mcp): `DomainConfig.effective_index_dir` property (lines 47-51) returns `fst_index_dir` if set else `dir`; `update_index` should use it (same as `build_index` does). `load_config` reads `~/.config/unified-history-mcp/config.toml` by default.
  - **Implementation implication for §7.2/§8.4:** the plan's edits are directly applicable to these real files. Add a `--compact` flag or a separate `palimpsest-compact.py` (calls `build_index`), switch the hourly `palimpsest-reindex.py` to `update_index`, and add a new `palimpsest-compact.{service,timer}` heredoc pair (`OnCalendar=03:00`) in `setup.sh`'s step 8. No unknown structure blocks the work.
- **Q5:** no WAL; crash-consistency via nightly `build`. Narrow sidecar-rename↔manifest-rename window can create a transient duplicate slot, healed by compaction. Defer stronger guarantee (a `generation` file) to post-M4 unless the real corpus shows problems.
- **Q6:** prefix-semantics regression risk (high-impact, low-visibility). Mandatory explicit `"ca"`-vs-`"cat"` test.
- **Q7 (RESOLVED 2026-08-09):** `dirs`-domain manifest handling. `_fix_dirs_manifest` was **deleted** in M3 (Option C) — it mutated the canonical `filename` (match key) in place, breaking `update` idempotency. The server now derives the session-id via `Path(fname).parent.name` at read time. `resolve_file_idx` is tombstone-aware (returns None for tombstoned/empty `filename`).
- **Q8:** serde field additions safe (Python ignores unknown keys; only Rust writes manifest).
- **Q9:** FFI not thread-safe (single-threaded CLI fine).
- **Q10:** `MAX_WORD_LEN` stack arrays fine (max composite key ≈ 109 bytes, well under 4096).
- **Q11:** vendored C drift (`fst-indexer/c/dafsa.c` vs PoC). Add a `cargo test` diff/hash check, or accept copy+sync model.

---

## Key file locations (verified this session)
- **`dafsa` (CANONICAL) — `indexer/dafsa/`**: `dafsa.c` + `dafsa.h` (opaque C core), `__init__.py` (Python `ctypes` `Dafsa` wrapper), `libdafsa.so` (built `gcc -shared -fPIC -O2`). Frontend: `indexer/__init__.py` (`build`, `Index.search`), `indexer/__main__.py` (`build`/`query` CLI).
- `dafsa.c` (historical) — `/home/arch/projects/carrasco-forcada-poc/dafsa.c` (opaque `dafsa` type, `dafsa_`-prefixed API; build with `dafsa.h` + `dafsa_test.c`, Makefile: cosmocc `-Wall -Wextra -Werror -O2`).
- `fst-indexer` — `/home/arch/projects/palimpsest/fst-indexer/` (`Cargo.toml`: fst=0.4, clap4, serde, serde_json, anyhow, glob, dev tempfile; `main.rs` Build/Search CLI; `lib.rs`: build :81, open :147, search :167, payload decode :188, extract_jsonl :258, extract_txt :303, extract_transcript :333, parse_transcript :381, write_fst :453, write_manifest :465, collect_files :484, date_from_path :515, tokenize :543, tests :552).
- `indexer.py` (historical palimpsest) — `/home/arch/projects/palimpsest/unified-history-mcp/src/unified_history_mcp/indexer.py` (build_index :55, search_fst :118, _load_manifest :39, resolve_file_idx :153, _iter_domain_files :14). In jing-meta the equivalent is `search/indexer.py` (`_fix_dirs_manifest` deleted in M3).
- `web-archive-mcp` — `/home/arch/projects/palimpsest/web-archive-mcp/src/web_archive_mcp/server.py:585-628` (`rebuild()` hardcodes `"fst-indexer"` at :602).
- `dns-whois-mcp` — `/home/arch/projects/palimpsest/dns-whois-mcp/src/dns_whois_mcp/storage.py:130` (writes `content` field).
- Live binary — `/home/arch/.local/bin/fst-indexer` (sandbox-blocked; differential must run outside sandbox).
- `palimpsest-setup/scripts/` — NOT found in accessible tree (Q4 — verify before editing).
