/*
 * _bench_states.c — Q1 state-count / headroom measurement harness
 *
 * Generates N synthetic keys with heavy English-like prefix sharing and
 * inserts them into a fresh DAFSA, then reports dafsa_stats and an estimate
 * of resident RAM + headroom against DAFSA_MAX_STATES_HARD.
 *
 * Key format EXACTLY matches the real indexer's _composite_key
 * (indexer/__init__.py:162):
 *
 *     word + "\0" + file_idx:u32BE + entry_idx:u32BE
 *
 * word  = stem (fixed pool of ~100 common stems) + 0..8 random lowercase
 *         letters.  Total key ≈ 12-24 bytes.
 *
 * Build (sandbox multi-TU form, NOT make):
 *   cd indexer/dafsa
 *   gcc -O2 -Wall -Wextra -Werror -I. -o _bench_states _bench_states.c \
 *       dafsa.c dafsa_state.c dafsa_core.c dafsa_persist.c dafsa_view.c
 *   ./_bench_states [-n KEYS] [-k FILES] [-p STEMS]
 */
#include "dafsa.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* ─── Tunables (overridable via argv) ─────────────────────────────────── */
#define DEF_KEYS    300000
#define DEF_FILES   1000
#define DEF_STEMS   100
#define PROGRESS_EVERY  50000

/* Deterministic LCG (matches the style of the existing test harness). */
static uint32_t g_rng = 0x5EED5EEDu;
static uint32_t rng_next(void)
{
    g_rng = g_rng * 1664525u + 1013904223u;
    return g_rng;
}

/* Common English-ish stems (real stems drive heavy prefix sharing). */
static const char *STEMS[] = {
    "the","and","ing","that","with","from","this","have","will","they",
    "an","in","on","at","to","for","of","by","be","as","is","it","or","so",
    "comp","re","info","pre","sub","inter","pro","trans","dis","over","under",
    "con","de","ex","im","in","non","un","anti","auto","bi","co","counter",
    "extra","fore","hyper","il","ir","mal","micro","mid","mini","mis","mono",
    "neo","out","pan","para","post","quasi","retro","semi","super","sur",
    "tri","ultra","up","vice","act","app","art","back","ball","base","bell",
    "book","cast","cent","cord","date","duct","duct","face","fer","fix",
    "form","grade","graph","gress","ject","later","log","mand","merge","nav",
    "pact","pand","port","press","quest","rupt","serve","sign","sist","spect",
    "struct","tain","tract","vent","ver","vert","vid","voc","vol"
};

#define NSTEMS_MAX (int)(sizeof(STEMS) / sizeof(STEMS[0]))

/* ─── Dedup set over composite keys ───────────────────────────────────── */
/* Open-addressing hash table; a bucket is empty iff key==NULL. */
static struct {
    unsigned char *key;
    uint8_t        len;
    uint64_t       hash;
} *dedup;
static size_t dedup_cap;   /* power of two */
static size_t dedup_used;

static uint64_t fnv1a(const unsigned char *k, size_t n)
{
    uint64_t h = 14695981039346656037ULL;
    size_t i;
    for (i = 0; i < n; i++) {
        h ^= k[i];
        h *= 1099511628211ULL;
    }
    return h;
}

static void dedup_init(size_t nkeys)
{
    size_t cap = 4096;
    while (cap < nkeys * 2) cap <<= 1;
    dedup_cap = cap;
    dedup = calloc(dedup_cap, sizeof(*dedup));
    if (!dedup) {
        fprintf(stderr, "dedup: OOM\n");
        exit(2);
    }
}

/* Returns 1 if key is already present, else inserts and returns 0. */
static int dedup_insert(const unsigned char *k, size_t n)
{
    uint64_t h = fnv1a(k, n);
    size_t i = (size_t)h & (dedup_cap - 1);
    for (;;) {
        if (!dedup[i].key) {
            unsigned char *copy = malloc(n ? n : 1);
            if (!copy) { fprintf(stderr, "dedup: OOM copy\n"); exit(2); }
            memcpy(copy, k, n);
            dedup[i].key = copy;
            dedup[i].len = (uint8_t)n;
            dedup[i].hash = h;
            dedup_used++;
            return 0;
        }
        if (dedup[i].hash == h && dedup[i].len == n &&
            memcmp(dedup[i].key, k, n) == 0)
            return 1;
        i = (i + 1) & (dedup_cap - 1);
    }
}

/* ─── Key builder ─────────────────────────────────────────────────────── */
#define MAXKEY 64   /* stem(~9) + suffix(8) + \0 + 4 + 4 */

/* Build composite key: word + "\0" + file:u32BE + entry:u32BE. */
static size_t build_key(unsigned char *buf, const char *stem,
                        unsigned char *suffix, size_t suflen,
                        uint32_t file, uint32_t entry)
{
    size_t p = 0;
    size_t sl = strlen(stem);
    memcpy(buf + p, stem, sl); p += sl;
    memcpy(buf + p, suffix, suflen); p += suflen;
    buf[p++] = 0;
    buf[p++] = (unsigned char)(file >> 24);
    buf[p++] = (unsigned char)(file >> 16);
    buf[p++] = (unsigned char)(file >> 8);
    buf[p++] = (unsigned char)(file);
    buf[p++] = (unsigned char)(entry >> 24);
    buf[p++] = (unsigned char)(entry >> 16);
    buf[p++] = (unsigned char)(entry >> 8);
    buf[p++] = (unsigned char)(entry);
    return p;
}

static const char lc[] = "abcdefghijklmnopqrstuvwxyz";

int main(int argc, char **argv)
{
    long nkeys = DEF_KEYS;
    long nfiles = DEF_FILES;
    int nstems = DEF_STEMS;
    int i;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-n") == 0 && i + 1 < argc) {
            nkeys = atol(argv[++i]);
        } else if (strcmp(argv[i], "-k") == 0 && i + 1 < argc) {
            nfiles = atol(argv[++i]);
        } else if (strcmp(argv[i], "-p") == 0 && i + 1 < argc) {
            nstems = atoi(argv[++i]);
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            printf("usage: %s [-n KEYS] [-k FILES] [-p STEMS]\n", argv[0]);
            return 0;
        } else {
            fprintf(stderr, "unknown arg: %s\n", argv[i]);
            return 2;
        }
    }
    if (nkeys < 1) nkeys = 1;
    if (nfiles < 1) nfiles = 1;
    if (nstems < 1) nstems = 1;
    if (nstems > NSTEMS_MAX) nstems = NSTEMS_MAX;

    printf("bench: N=%ld keys, %ld files, %d stems, rng seed=0x5EED5EED\n",
           nkeys, nfiles, nstems);

    dedup_init((size_t)nkeys);

    dafsa *d = dafsa_create();
    if (!d) { fprintf(stderr, "dafsa_create OOM\n"); return 2; }

    /* per-file entry counter */
    uint32_t *file_cnt = calloc((size_t)nfiles, sizeof(uint32_t));
    if (!file_cnt) { fprintf(stderr, "OOM file_cnt\n"); return 2; }

    unsigned char buf[MAXKEY];
    long added = 0, dup = 0;
    long k;
    for (k = 0; k < nkeys; k++) {
        int si = (int)(rng_next() % (uint32_t)nstems);
        const char *stem = STEMS[si];
        unsigned char suffix[8];
        size_t suflen = (size_t)(rng_next() % 9);   /* 0..8 letters */
        size_t j;
        for (j = 0; j < suflen; j++)
            suffix[j] = (unsigned char)lc[rng_next() % 26];

        uint32_t file = (uint32_t)(k % nfiles);
        uint32_t entry = file_cnt[file]++;

        size_t len = build_key(buf, stem, suffix, suflen, file, entry);

        if (dedup_insert(buf, len)) {
            dup++;
            continue;
        }

        int rc = dafsa_add_n(d, buf, len);
        if (rc < 0) {
            fprintf(stderr, "dafsa_add_n error at key %ld\n", k);
            return 2;
        }
        added += rc;

        if ((k + 1) % PROGRESS_EVERY == 0)
            printf("  %ld keys inserted (added=%ld, dup=%ld)\n",
                   k + 1, added, dup);
    }

    printf("  total: %ld keys, %ld added (dedup %ld skipped)\n",
           nkeys, added, dup);

    /* ─── Verification pass: regenerate the same deterministic keys and
     * confirm each one is found (storage correctness). ────────────────── */
    printf("\n=== verification (lookup of all generated keys) ===\n");
    {
        long hits = 0;
        memset(file_cnt, 0, (size_t)nfiles * sizeof(uint32_t)); /* reset entry counters */
        g_rng = 0x5EED5EEDu;   /* reset RNG to regenerate identical keys */
        for (k = 0; k < nkeys; k++) {
            int si = (int)(rng_next() % (uint32_t)nstems);
            const char *stem = STEMS[si];
            unsigned char suffix[8];
            size_t suflen = (size_t)(rng_next() % 9);
            size_t j;
            for (j = 0; j < suflen; j++)
                suffix[j] = (unsigned char)lc[rng_next() % 26];
            uint32_t file = (uint32_t)(k % nfiles);
            uint32_t entry = file_cnt[file]++;   /* counters were just reset above */
            size_t len = build_key(buf, stem, suffix, suflen, file, entry);
            if (dafsa_lookup_n(d, buf, len) == 1) hits++;
        }
        printf("  lookups found: %ld / %ld keys\n", hits, nkeys);
        if (hits != nkeys) {
            fprintf(stderr, "  !! MISMATCH — DAFSA storage incorrect\n");
            return 2;
        }
    }

    dafsa_stats_out st;
    dafsa_stats(d, &st);

    printf("\n=== dafsa_stats ===\n");
    printf("  n_states_total:     %u\n", st.n_states_total);
    printf("  n_states_reachable: %u\n", st.n_states_reachable);
    printf("  n_final:            %u\n", st.n_final);
    printf("  n_trans:            %u\n", st.n_trans);
    printf("  register_probes:    %llu\n", (unsigned long long)st.register_probes);

    printf("\n=== estimated resident RAM ===\n");
    {
        uint64_t states_ram = (uint64_t)st.n_states_total * 64;
        uint64_t inodes_ram = (uint64_t)st.n_trans * 12;      /* Inode = 12B padded */
        uint64_t reg_ram    = (uint64_t)st.n_states_reachable * 16; /* ~reg_cap×16 */
        uint64_t scratch    = 2u << 20;                       /* ~2 MB scratch + misc */
        uint64_t total      = states_ram + inodes_ram + reg_ram + scratch;

        printf("  states (64B/state):        %10.2f MiB (%llu B)\n",
               states_ram / (1024.0 * 1024.0), (unsigned long long)states_ram);
        printf("  inodes (12B/trans):        %10.2f MiB (%llu B)\n",
               inodes_ram / (1024.0 * 1024.0), (unsigned long long)inodes_ram);
        printf("  register (~16B/slot):      %10.2f MiB (%llu B)\n",
               reg_ram / (1024.0 * 1024.0), (unsigned long long)reg_ram);
        printf("  scratch + misc (~2MiB):    %10.2f MiB\n",
               scratch / (1024.0 * 1024.0));
        printf("  [TransHeaps: states with >4 edges are not exposed by the\n"
               "   public stats API; on this prefix-heavy corpus they are a small\n"
               "   delta relative to the 64B/state floor and are excluded below.]\n");
        printf("  ESTIMATED LOWER BOUND (excl. TransHeaps): %.2f MiB\n",
               total / (1024.0 * 1024.0));
        printf("  (+ TransHeaps: unmeasured, est. <5 MiB on this corpus)\n");
    }

    printf("\n=== headroom vs DAFSA_MAX_STATES_HARD (100,000,000) ===\n");
    {
        long long cap = 100000000LL;
        long long used = st.n_states_total;
        double pct = (double)(cap - used) / (double)cap * 100.0;
        double gb = (double)(cap - used) * 64.0 / 1e9;
        printf("  states used:  %lld\n", used);
        printf("  headroom:     %.4f%% (%.2f GB at 64B/state)\n", pct, gb);
        if (used > cap)
            printf("  !! OVER HARD CAP !! — dense-trans memory not viable\n");
    }

    dafsa_free(d);
    free(file_cnt);
    {
        size_t b;
        for (b = 0; b < dedup_cap; b++) free(dedup[b].key);
    }
    free(dedup);
    return 0;
}
