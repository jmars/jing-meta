# jing-meta Architecture Roadmap

_Architectural review and prioritized improvement roadmap._
_Source: advisor review of the full repo (~9,500 LOC), 2026-08-09._

## Status — all roadmap items complete (2026-08-09)

All 20 roadmap items (Tiers 1–3) and the resolved "Other findings" are implemented
and committed. Each item below carries its completion commit. This roadmap is
maintained as the historical record of the improvement program; it is not an open
todo list. Remaining known gaps are only the "Other findings" still listed as open
at the bottom.

| Item | Status | Commit |
|------|--------|--------|
| Tier 1: 1–7 | ✅ Done | `d847796` (Tier 1 architecture-roadmap improvements) |
| 8 SQLite N+1 | ✅ Done | `964c9fe` |
| 9 CI/tooling | ✅ Done | `4baf8be` |
| 10 structured logging | ✅ Done | `4080553` |
| 11 config validation | ✅ Done | `14a24eb` |
| 12 consolidate extractors | ✅ Done | `088faf6` |
| 13 validator batching | ✅ Done | `964c9fe` |
| 14 pipeline testability | ✅ Done | `964c9fe` |
| 15 AND/OR FST | ✅ Done | `a7a405b` |
| Other findings | ✅ Done | `b95c10a` + `97c1852` |
| 16 shared MCP base | ✅ Done | `0940aca` |
| 17 concurrency docs | ✅ Done | `b72c057` |
| 18 PDWG checksum | ✅ Done | `aa4c3cf` |
| 19 dreamer stages | ✅ Done | `9bdef50` |
| 20 sidecar format | ✅ Done | `5f9b59e` |

## Headline findings (read these first)

1. **The "single embedding loader" claim is false.** There are **three** independent
   `fastembed.TextEmbedding` loaders with **two different env-var names**
   (`JING_EMBED_MODEL` in `jing_meta/embed.py`; `SEMANTIC_EMBED_MODEL` in both
   `memory/semantic_index.py:25` and `dreamer/semantic_search.py:18`).
   `jing_meta/embed.py` is loaded by **nothing** in the repo today. `cosine()`
   and `_entity_text()` are duplicated byte-for-byte across modules. The README's
   "share… the embedding model" promise is unmet.

2. **The zero-copy `dafsa_view` exists in C but is NOT wired to Python.**
   `indexer/dafsa/__init__.py:78` routes `readonly=True` to `dafsa_load_readonly`
   (state-materializing). `dafsa_view_open`/`dafsa_view_prefix_enum`/
   `dafsa_view_close` are **not declared in the ctypes layer at all**. ROADMAP M4
   claims this is done — doc/code divergence.

3. **The local validator silently fabricates relations when Ollama is down.**
   `dreamer/local_validator.py:152-153` falls through to `rel = "related_to"`
   whenever `_local_llm_relation` returns `None` (timeout, refused, not running).
   Every weak candidate that survived the `sim < 0.45` filter becomes a permanent
   `related_to` edge with zero validation. This pollutes the graph under exactly
   the failure mode the offline design is supposed to tolerate.

4. **`--no-rerank` neuters validation.** `validate_and_name` reads
   `c.get("similarity", 0.0)` (`local_validator.py:135`), but `build_shortlist`
   only attaches `similarity` *after* `rerank_with_semantic` runs. With
   `--no-rerank`, every candidate has similarity 0.0 and is dropped by the
   `sim < 0.45` guard unless `_shared_token_relation` rescues it. The flag is
   documented as a perf toggle; it actually changes correctness.

5. **Python `_atomic_write` is weaker than the C `dafsa_save` it sits next to.**
   C does `fsync(file)` → `rename` → `fsync_dir_of(path)`
   (`dafsa_persist.c:198-205`). Python does `fsync(file)` → `os.replace`
   (`indexer/__init__.py:142-155`) with **no directory fsync**. The manifest is
   less durable than the DAFSA.

---

## Tier 1 — Quick wins (hours each, low risk, high value)

1. **Fix `validate_and_name` to not fabricate `related_to`** when the local LLM
   fails. Skip the candidate instead. (`dreamer/local_validator.py:152`)
   — *correctness, must-fix.*

2. **Fix `--no-rerank` ↔ validator coupling.** Always populate a similarity
   signal (lexical fallback) before validation.
   (`dreamer/souffle_pipeline.py:301`, `dreamer/local_validator.py:135`)
   — *correctness.*

3. **Wire `dafsa_view_*` into the ctypes wrapper** and route read-only
   `Dafsa.load` through `dafsa_view_open`. Update the ROADMAP to match.
   (`indexer/dafsa/__init__.py`) — *perf + doc/code consistency.*

4. **Add directory fsync to Python `_atomic_write`** so the manifest is as
   durable as the C DAFSA. (`indexer/__init__.py:142`) — *durability.*

5. **Complete the ctypes ABI declarations** for `dafsa_view_*` and `dafsa_stats`
   (or remove `dafsa_stats` from the Python surface). Add a `dafsa_abi_version()`
   probe. (`indexer/dafsa/__init__.py`) — *safety.*

6. **Lazy-load `libdafsa.so`** (module-level `_get_lib()` instead of
   `_lib = _load()`). `pytest.importorskip` in tests.
   (`indexer/dafsa/__init__.py`, `tests/test_indexer.py`) — *operability.*

7. **Unify the embedder.** Delete `_get_embedder` in
   `memory/semantic_index.py` and `dreamer/semantic_search.py`; both call
   `jing_meta.embed`. Standardize on `JING_EMBED_MODEL`; delete
   `SEMANTIC_EMBED_MODEL`. Collapse the duplicate `cosine` and `_entity_text`.
   — *removes ~150 LOC, three drift surfaces.*

## Tier 2 — Medium (1–3 days each)

8. **Fix the dreamer SQLite N+1 and O(N×M).** Single JOIN in
   `load_graph_sqlite`; `id→info` map in `save_graph_sqlite`.
   (`dreamer/dreamer.py:43-103`)

9. **Add `ruff` + `mypy --check-untyped-defs` + a GitHub Actions workflow**
   running ruff, mypy, pytest, and the C test binary (`make _t`). Add a `make
   test` target and a `tests/test_c_core.py` that runs it (skipped if no gcc).

10. **Introduce structured logging** across all four subsystems via `logging`,
    gated by `JING_LOG_LEVEL`. Replace every `print(..., file=sys.stderr)`.

11. **Add config validation** in `DomainConfig.__post_init__` / `load_config`:
    enum-check `type`, `extractor`; compile-check globs. Fail loud at config
    load, not silently at query time.

12. **Consolidate extractors.** `search/extractors.py` and
    `indexer/__init__.py` should share one source of truth, or the difference
    (capping, tool_calls handling) should be explicit and documented. Same for
    `_DOMAIN_EXTRACTOR_MAP` in `server.py` — derive from `indexer.EXTRACTORS`.

13. **Batch the local validator** through `build_validation_prompt` (already used
    by the cloud path) and route through `dreamer/llm.call` against Ollama's
    `/v1` endpoint with relaxed key handling. — *perf + retries + JSON.*

14. **Make the dreamer pipeline testable.** Add a `validator` callable seam;
    write end-to-end tests with a stub LLM and a tiny synthetic graph.

15. **Expose AND/OR through the FST path** (don't fall back to slow scan for AND
    queries), or document the limitation in the `search` tool docstring.

## Tier 3 — Architectural (weeks; do after Tier 1–2)

16. **Extract a shared MCP base** (`jing_meta/mcp_base.py`): config singleton,
    atomic-write helper, logging, common return-type conventions. Both servers
    inherit. — *sets up #17.*

17. **Decide and document the MCP concurrency model.** If single-threaded stdio
    forever: document at the top of both servers. If multi-threaded ever:
    per-thread SQLite connections, embedder lock already in `jing_meta.embed`
    (after #7), per-request Dafsa handles (or RW-lock around a shared one, since
    `dafsa_stats` writes the handle — see #2f).

18. **Add a checksum to the PDWG format.** Either a trailing CRC32 on v3, or a v4
    with CRC. Catches silent bit-flip corruption of long-lived indexes. Update
    both `dafsa_save` and `dafsa_view_open`/`dafsa_load_impl` to verify.

19. **Separate the dreamer into explicit, replayable stages** with typed data
    contracts (dataclasses or pydantic): `discover → rank → validate → apply`.
    Each stage has a disk format (so a run can be replayed from any stage) and
    unit tests. The current `run_pipeline` mixes ranking, validation, and LLM
    dispatch in one function.

20. **Type the sidecar format** (version header + checksum), or fold per-file
    keys into a small SQLite DB alongside the manifest. The current
    `<slots>/<i>.keys` LE format is unversioned and unchecksummed; a single torn
    write is silently truncated by `_read_sidecar`
    (`indexer/__init__.py:186-190`).

---

## Other findings (not yet sized / background)

- **`dafsa_load_impl` aborts on OOM** (`dafsa_persist.c:409-412`). A corrupt
  `index.fst` with one state claiming huge `ntrans` kills the process. Per-state
  `ntrans` is uncapped; only total `n_states` has a hard cap.
- **`dafsa_stats` no longer casts away `const`** (lazy-stats cache removed in
  97c1852).  The function computes fresh on every call (one BFS) and is now
  const-correct — safe for concurrent reads on a shared read-only DAFSA handle.
- **Implicit schema contract**: `dreamer/dreamer.py:load_graph_sqlite` +
  `save_graph_sqlite` talk directly to the memory DB tables; no shared schema
  definition between dreamer and `memory/server.py:_init_schema`.
- **Token regex/stopword drift**: `dreamer.py:_STOPWORDS` and
  `souffle_pipeline.py:STOP` differ; Soufflé token table and Python candidate
  generator agree on overlap counts only by accident.
- **`_local_llm_relation` parses badly**: `resp.split()[0]` on
  `"The relation is implements"` → `"the"`.
- **No `BEGIN IMMEDIATE`** in `save_graph_sqlite` → `database is locked` under
  concurrent WAL.
- **`apply_mutations` archive step** uses `datetime.now()` per-observation
  instead of the run timestamp.
- **Dead/duplicated surface**: `DomainConfig.fst_binary`, unused `import
  subprocess` in `search/indexer.py`, `_DOMAIN_EXTRACTOR_MAP` paralleling
  `indexer.EXTRACTORS`, and divergent `extract_*` functions between
  `indexer/__init__.py` and `search/extractors.py`.
- **No type checking / linting / CI.** Only `pytest>=8` under `dev`.
- **`tests/test_indexer.py` imports `indexer` unconditionally** (line 12). If
  `libdafsa.so` isn't built, the whole test session aborts on collection.
- **Test coverage is one subsystem**: 12 cases for `indexer.update`; zero for
  search/memory MCP tools, dreamer pipeline, config, extractors, renderers,
  local_validator, souffle_pipeline.
- **No structured logging** anywhere (everything `print(..., file=sys.stderr)`).
- **Config validation absent**: no enum-check of `type`/`extractor`, no
  compile-check of globs; a typo in `config.toml` silently yields empty results.
- **`fastembed` first-use download is invisible** (100MB–1GB, no progress/log).
- **Inconsistent return contracts**: memory read tools return `list[TextContent]`,
  memory write tools return `str`, all search tools return `str`.
- **FST-backed search hardcodes `any_word=True`** (`search/indexer.py:120`), so
  AND queries silently fall back to the slow line scan.
- **Slow-path regex has no timeout** and the guard misses patterns like
  `(a|a)*$`; a hostile regex can hang the server.

---

## Should vs nice-to-have

- **Should ship (correctness/operability):** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11.
  These are either bugs (#1, #2), doc/code divergences (#3), safety/durability
  gaps (#4, #5, #6), or the unification the project explicitly promises (#7, #8).
  Most are <1 day.
- **Should ship (maturity):** 12, 13, 14, 15. Pay-off grows with the codebase
  size; cheaper to do now than later.
- **Nice-to-have (architectural):** 16–20. Defer until there's a concrete driver
  (multi-threaded server, real corruption incident, dreamer complexity pain).

## Not deeply audited (flagged for honesty)

- `dafsa_core.c:80+` (confluence_path, replace_or_register, clone_state,
  add_n/delete_n) — algorithmic correctness relies on the randomized
  delete-differential test in `dafsa_test.c`, which exists but isn't run by CI.
- The Soufflé `.dl` ruleset (`dreamer/souffle/garden.dl`).
- Transcript parser edge cases (`search/transcript.py`).
