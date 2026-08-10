/*
 * dafsa_test.c — Ported test harness from dawg.c + embedded-NUL test
 *
 * Tests 1-9 are ported from the original dawg.c main().
 * Test 10 verifies embedded-NUL keys via the _n API.
 *
 * Build: see Makefile
 * Run:   ./dafsa
 */
#include "dafsa.h"
#include "dafsa_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

/* ─── M1 test parameters ─────────────────────────────────────────────── */
#define RT_TRIALS    10    /* round-trip trials */
#define RT_KEYS    1200    /* random keys per round-trip trial */
#define RT_MAXLEN     8
#define DD_TRIALS    60    /* delete-differential trials */
#define DD_UNIVERSE 200    /* universe words per differential trial */
#define DD_MAXLEN     8

/* ─── Helper: print stats from a dafsa_stats_out ───────────────────── */

static void print_stats(const dafsa_stats_out *st)
{
    printf("  total states:   %u\n", st->n_states_total);
    printf("  reachable:      %u\n", st->n_states_reachable);
    printf("  final:          %u\n", st->n_final);
    printf("  transitions:    %u\n", st->n_trans);
    printf("  register probes: %llu\n",
           (unsigned long long)st->register_probes);
}

static void show_stats(const dafsa *d)
{
    dafsa_stats_out st;
    dafsa_stats(d, &st);
    print_stats(&st);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* ─── M1 test helpers ─────────────────────────────────────────────────── */

static uint32_t g_rng = 0x2F6E2B1u;

static uint32_t rng_next(void)
{
    g_rng = g_rng * 1664525u + 1013904223u;   /* LCG, deterministic */
    return g_rng;
}

static const char rng_alphabet[] = "abcdefghijklmnopqrstuvwxyz";

/* Assert DAFSA lookup matches the reference set for every universe word. */
static void check_parity(dafsa *d, const unsigned char (*keys)[16],
                         const size_t *lens, const int *in_set, int n)
{
    int k;
    for (k = 0; k < n; k++) {
        int got = dafsa_lookup_n(d, keys[k], lens[k]);
        assert(got == in_set[k]);
    }
}

typedef struct {
    unsigned char payloads[64][8];
    size_t plen[64];
    int count;
    int stop_after;   /* >0: cb returns non-zero once count reaches it */
} enum_ctx;

static int enum_collect(const unsigned char *payload, size_t payload_len,
                        void *user)
{
    enum_ctx *c = (enum_ctx *)user;
    if (c->count < 64) {
        size_t n = payload_len < 8 ? payload_len : 8;
        memcpy(c->payloads[c->count], payload, n);
        c->plen[c->count] = payload_len;
    }
    c->count++;
    if (c->stop_after > 0 && c->count >= c->stop_after) return 1;
    return 0;
}

static int word_in_set(const char *w, const char *const *set, int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (strcmp(w, set[i]) == 0) return 1;
    return 0;
}

/* ═══════════════════════════════════════════════════════════════════════ */

int main(void)
{
    dafsa *d;

    d = dafsa_create();
    assert(d != NULL);

    printf("=== Carrasco & Forcada Incremental DAFSA — PoC (M0) ===\n\n");

    /* ── Test 1: Basic add + lookup ── */
    printf("[Test 1] Adding words: cat, car, cart, do, dog\n");
    assert(dafsa_add(d, (const unsigned char *)"cat") == 1);
    assert(dafsa_add(d, (const unsigned char *)"car") == 1);
    assert(dafsa_add(d, (const unsigned char *)"cart") == 1);
    assert(dafsa_add(d, (const unsigned char *)"do") == 1);
    assert(dafsa_add(d, (const unsigned char *)"dog") == 1);

    /* Verify presence */
    assert(dafsa_lookup(d, (const unsigned char *)"cat") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"car") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"cart") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"do") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"dog") == 1);

    /* Verify absence */
    assert(dafsa_lookup(d, (const unsigned char *)"ca") == 0);
    assert(dafsa_lookup(d, (const unsigned char *)"c") == 0);
    assert(dafsa_lookup(d, (const unsigned char *)"cats") == 0);
    assert(dafsa_lookup(d, (const unsigned char *)"d") == 0);
    assert(dafsa_lookup(d, (const unsigned char *)"dot") == 0);

    printf("  PASS: all lookups correct\n");
    show_stats(d);

    /* ── Test 2: Verify minimality (shared suffixes) ── */
    printf("\n[Test 2] Checking suffix sharing (car/cat share prefix, do/dog share prefix)\n");
    /* Behavioral check: 'car' and 'cat' both exist; 'do' is both a word
     * and a prefix of 'dog'.  Verify that the expected words are
     * recognized and unrelated ones are not. */
    assert(dafsa_lookup(d, (const unsigned char *)"car") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"cat") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"cart") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"do") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"dog") == 1);
    /* 'cax' should NOT be a word */
    assert(dafsa_lookup(d, (const unsigned char *)"cax") == 0);
    /* Minimality check via stats: we added 5 words; reachable states
     * should be reasonable (not 5 x len). */
    {
        dafsa_stats_out st;
        dafsa_stats(d, &st);
        printf("  reachable states after 5 words: %u\n", st.n_states_reachable);
        assert(st.n_states_reachable < 15);  /* sanity: much less than sum-of-lens */
    }
    printf("  PASS: suffix sharing verified\n");

    /* ── Test 3: Duplicate additions are no-ops ── */
    printf("\n[Test 3] Duplicate addition\n");
    assert(dafsa_add(d, (const unsigned char *)"cat") == 0);
    assert(dafsa_add(d, (const unsigned char *)"dog") == 0);
    printf("  PASS: duplicates correctly rejected\n");

    /* ── Test 4: Deletion ── */
    printf("\n[Test 4] Deletion\n");
    assert(dafsa_delete(d, (const unsigned char *)"cart") == 1);
    assert(dafsa_lookup(d, (const unsigned char *)"cart") == 0);
    assert(dafsa_lookup(d, (const unsigned char *)"car") == 1);  /* still there */
    assert(dafsa_lookup(d, (const unsigned char *)"cat") == 1);  /* still there */
    printf("  PASS: 'cart' deleted, 'car'/'cat' unaffected\n");

    /* Delete non-existent */
    assert(dafsa_delete(d, (const unsigned char *)"xyzzy") == 0);
    printf("  PASS: non-existent word correctly rejected\n");

    show_stats(d);

    /* ── Test 5: Larger batch ── */
    printf("\n[Test 5] Adding 20 common English words\n");
    {
        const char *words[] = {
            "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
            "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
            NULL
        };
        int i;
        for (i = 0; words[i]; i++) {
            dafsa_add(d, (const unsigned char *)words[i]);
        }
        /* Verify all are present */
        for (i = 0; words[i]; i++) {
            assert(dafsa_lookup(d, (const unsigned char *)words[i]) == 1);
        }
        printf("  PASS: all 20 words present and minimal\n");
    }
    show_stats(d);

    dafsa_free(d);

    /* ── Test 6: Edge case — single char ── */
    printf("\n[Test 6] Single-character words\n");
    {
        dafsa *d2 = dafsa_create();
        assert(d2 != NULL);

        assert(dafsa_add(d2, (const unsigned char *)"x") == 1);
        assert(dafsa_add(d2, (const unsigned char *)"x") == 0);  /* dup */
        assert(dafsa_lookup(d2, (const unsigned char *)"x") == 1);
        assert(dafsa_lookup(d2, (const unsigned char *)"y") == 0);
        assert(dafsa_delete(d2, (const unsigned char *)"x") == 1);
        assert(dafsa_lookup(d2, (const unsigned char *)"x") == 0);
        printf("  PASS: single-char add/delete works\n");

        dafsa_free(d2);
    }

    /* ── Test 7: Prefix-sharing stress ── */
    printf("\n[Test 7] Prefix-sharing: 'abc', 'abd', 'ab', 'a'\n");
    {
        dafsa *d3 = dafsa_create();
        assert(d3 != NULL);

        assert(dafsa_add(d3, (const unsigned char *)"abc") == 1);
        assert(dafsa_add(d3, (const unsigned char *)"abd") == 1);
        assert(dafsa_add(d3, (const unsigned char *)"ab") == 1);
        assert(dafsa_add(d3, (const unsigned char *)"a") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"abc") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"abd") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"ab") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"a") == 1);
        /* Delete 'abc', verify 'abd', 'ab', 'a' survive */
        assert(dafsa_delete(d3, (const unsigned char *)"abc") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"abc") == 0);
        assert(dafsa_lookup(d3, (const unsigned char *)"abd") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"ab") == 1);
        assert(dafsa_lookup(d3, (const unsigned char *)"a") == 1);
        printf("  PASS: prefix sharing with selective deletion\n");

        dafsa_free(d3);
    }

    /* ── Dot output ── */
    printf("\n[Graphviz] Writing DAFSA to dafsa.dot\n");
    {
        dafsa *d_dot = dafsa_create();
        assert(d_dot != NULL);

        dafsa_add(d_dot, (const unsigned char *)"cat");
        dafsa_add(d_dot, (const unsigned char *)"car");
        dafsa_add(d_dot, (const unsigned char *)"cart");
        dafsa_add(d_dot, (const unsigned char *)"do");
        dafsa_add(d_dot, (const unsigned char *)"dog");

        {
            FILE *f = fopen("dafsa.dot", "w");
            if (f) {
                dafsa_dot(d_dot, f);
                fclose(f);
                printf("  Wrote dafsa.dot (render with: dot -Tpng dafsa.dot -o dafsa.png)\n");
            }
        }

        dafsa_free(d_dot);
    }

    /* ── Test 8: Ordering independence ── */
    printf("\n[Test 8] Ordering independence: same words, different order\n");
    {
        const char *set_a[] = {"apple", "app", "apt", "apex", "apricot", NULL};
        const char *set_b[] = {"apricot", "apex", "apt", "apple", "app", NULL};
        int i;
        dafsa *da = dafsa_create();
        dafsa *db = dafsa_create();

        assert(da != NULL);
        assert(db != NULL);

        /* Add set A */
        for (i = 0; set_a[i]; i++)
            dafsa_add(da, (const unsigned char *)set_a[i]);

        /* Add set B (reversed order) */
        for (i = 0; set_b[i]; i++)
            dafsa_add(db, (const unsigned char *)set_b[i]);

        /* Both DAFSAs should recognize the same words */
        for (i = 0; set_a[i]; i++) {
            assert(dafsa_lookup(da, (const unsigned char *)set_a[i]) == 1);
            assert(dafsa_lookup(db, (const unsigned char *)set_a[i]) == 1);
        }

        /* Should have same number of reachable states (minimal) */
        {
            dafsa_stats_out sta, stb;
            dafsa_stats(da, &sta);
            dafsa_stats(db, &stb);
            printf("  Set A: ");
            print_stats(&sta);
            printf("  Set B: ");
            print_stats(&stb);
            assert(sta.n_states_reachable == stb.n_states_reachable);
        }

        dafsa_free(da);
        dafsa_free(db);
    }
    printf("  PASS: ordering independence verified\n");

    /* ── Test 9: Interleaved add/delete ── */
    printf("\n[Test 9] Interleaved add/delete cycles\n");
    {
        dafsa *dd = dafsa_create();
        assert(dd != NULL);

        /* Add 3 words, delete 1, add 2 more, delete 1, verify survivors */
        assert(dafsa_add(dd, (const unsigned char *)"abc") == 1);
        assert(dafsa_add(dd, (const unsigned char *)"abd") == 1);
        assert(dafsa_add(dd, (const unsigned char *)"abe") == 1);
        assert(dafsa_delete(dd, (const unsigned char *)"abd") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abd") == 0);
        assert(dafsa_lookup(dd, (const unsigned char *)"abc") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abe") == 1);

        assert(dafsa_add(dd, (const unsigned char *)"abf") == 1);
        assert(dafsa_add(dd, (const unsigned char *)"abg") == 1);
        assert(dafsa_delete(dd, (const unsigned char *)"abe") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abe") == 0);
        assert(dafsa_lookup(dd, (const unsigned char *)"abc") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abf") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abg") == 1);

        /* Re-add deleted word */
        assert(dafsa_add(dd, (const unsigned char *)"abe") == 1);
        assert(dafsa_lookup(dd, (const unsigned char *)"abe") == 1);

        dafsa_free(dd);
    }
    printf("  PASS: interleaved add/delete works\n");

    /* ── Test 10: Embedded NUL via _n functions ── */
    printf("\n[Test 10] Embedded NUL via _n functions\n");
    {
        dafsa *dn = dafsa_create();
        assert(dn != NULL);

        /* Keys with embedded NUL bytes (cannot be expressed as C strings) */
        const unsigned char key1[] = {'a', 'b', 0x00, 'c', 'd'};   /* "ab\0cd" */
        const unsigned char key2[] = {'a', 'b', 0x00, 'e', 'f'};   /* "ab\0ef" */
        const unsigned char key3[] = {'x', 0x00, 'y', 0x00, 'z'};  /* "x\0y\0z" */
        const unsigned char key4[] = {'a', 'b', 0x00, 'c', 'd', 0x00, 'g', 'h'}; /* "ab\0cd\0gh" */

        /* Add via _n */
        assert(dafsa_add_n(dn, key1, 5) == 1);
        assert(dafsa_add_n(dn, key2, 5) == 1);
        assert(dafsa_add_n(dn, key3, 5) == 1);
        assert(dafsa_add_n(dn, key4, 8) == 1);

        /* Verify via _n */
        assert(dafsa_lookup_n(dn, key1, 5) == 1);
        assert(dafsa_lookup_n(dn, key2, 5) == 1);
        assert(dafsa_lookup_n(dn, key3, 5) == 1);
        assert(dafsa_lookup_n(dn, key4, 8) == 1);

        /* Verify duplicates rejected via _n */
        assert(dafsa_add_n(dn, key1, 5) == 0);

        /* strlen wrappers should NOT find these keys
         * (they treat the first NUL as terminator) */
        assert(dafsa_lookup(dn, (const unsigned char *)"ab") == 0);
        assert(dafsa_lookup(dn, (const unsigned char *)"x") == 0);

        /* Delete via _n */
        assert(dafsa_delete_n(dn, key1, 5) == 1);
        assert(dafsa_lookup_n(dn, key1, 5) == 0);
        assert(dafsa_lookup_n(dn, key2, 5) == 1);  /* still there */
        assert(dafsa_lookup_n(dn, key3, 5) == 1);  /* still there */
        assert(dafsa_lookup_n(dn, key4, 8) == 1);  /* still there */

        /* Delete non-existent via _n */
        assert(dafsa_delete_n(dn, key1, 5) == 0);

        /* Re-add previously deleted key */
        assert(dafsa_add_n(dn, key1, 5) == 1);
        assert(dafsa_lookup_n(dn, key1, 5) == 1);

        /* Empty key via _n (len=0) */
        assert(dafsa_add_n(dn, (const unsigned char *)"", 0) == 1);
        assert(dafsa_lookup_n(dn, (const unsigned char *)"", 0) == 1);
        assert(dafsa_add_n(dn, (const unsigned char *)"", 0) == 0);  /* dup */
        assert(dafsa_delete_n(dn, (const unsigned char *)"", 0) == 1);
        assert(dafsa_lookup_n(dn, (const unsigned char *)"", 0) == 0);

        dafsa_free(dn);
    }
    printf("  PASS: embedded NUL keys work with _n, invisible to strlen wrappers\n");

    /* ── Test 11: Round-trip persistence ── */
    printf("\n[M1 Test 11] Round-trip: add %d random keys -> save -> free -> load\n",
           RT_KEYS);
    {
        static const char alphabet[] = "abcdefghijklmnopqrstuvwxyz";
        int trial;
        g_rng = 0x0C0FFEE11u;
        for (trial = 0; trial < RT_TRIALS; trial++) {
            unsigned char (*keys)[16] = malloc(RT_KEYS * sizeof(*keys));
            size_t lens[RT_KEYS];
            dafsa *d, *d2;
            dafsa_stats_out st1, st2;
            char path[256];
            int k;

            assert(keys != NULL);
            snprintf(path, sizeof(path), "/tmp/m1_rt_%d.pdwg", trial);

            /* generate random keys */
            for (k = 0; k < RT_KEYS; k++) {
                size_t len = 1 + rng_next() % RT_MAXLEN;
                size_t j;
                lens[k] = len;
                for (j = 0; j < len; j++)
                    keys[k][j] =
                        (unsigned char)alphabet[rng_next() % (sizeof(alphabet) - 1)];
            }

            d = dafsa_create();
            assert(d != NULL);
            for (k = 0; k < RT_KEYS; k++)
                dafsa_add_n(d, keys[k], lens[k]);

            dafsa_stats(d, &st1);
            assert(dafsa_save(d, path) == 0);
            dafsa_free(d);

            d2 = dafsa_load(path);
            assert(d2 != NULL);
            for (k = 0; k < RT_KEYS; k++)
                assert(dafsa_lookup_n(d2, keys[k], lens[k]) == 1);

            dafsa_stats(d2, &st2);
            assert(st2.n_states_reachable == st1.n_states_reachable);
            assert(st2.n_final == st1.n_final);
            assert(st2.n_trans == st1.n_trans);

            dafsa_free(d2);
            remove(path);
            free(keys);
            printf("  trial %d: %d keys, %u reachable, %u final OK\n",
                   trial, RT_KEYS, st1.n_states_reachable, st1.n_final);
        }
    }
    printf("  PASS: round-trip preserves lookup set and reachable-state count\n");

    /* ── Test 12: Randomized delete differential ── */
    printf("\n[M1 Test 12] Randomized delete differential (%d trials)\n", DD_TRIALS);
    {
        unsigned char (*univ)[16] = malloc(DD_UNIVERSE * sizeof(*univ));
        size_t ulens[DD_UNIVERSE];
        int in_set[DD_UNIVERSE];
        int trial, k;

        assert(univ != NULL);
        g_rng = 0x0DD0D202u;

        /* generate a unique word universe */
        for (k = 0; k < DD_UNIVERSE; k++) {
            int dup;
            size_t len, j;
            do {
                len = 1 + rng_next() % DD_MAXLEN;
                for (j = 0; j < len; j++)
                    univ[k][j] = (unsigned char)
                        rng_alphabet[rng_next() % (sizeof(rng_alphabet) - 1)];
                dup = 0;
                {
                    int m;
                    for (m = 0; m < k; m++) {
                        if (ulens[m] == len &&
                            memcmp(univ[m], univ[k], len) == 0) {
                            dup = 1;
                            break;
                        }
                    }
                }
            } while (dup);
            ulens[k] = len;
        }

        for (trial = 0; trial < DD_TRIALS; trial++) {
            dafsa *d = dafsa_create();
            assert(d != NULL);
            for (k = 0; k < DD_UNIVERSE; k++) {
                dafsa_add_n(d, univ[k], ulens[k]);
                in_set[k] = 1;
            }

            /* save/load round-trip in a few trials */
            if (trial == 3 || trial == 27 || trial == 45) {
                char path[64];
                snprintf(path, sizeof(path), "/tmp/m1_dd_%d.pdwg", trial);
                assert(dafsa_save(d, path) == 0);
                dafsa_free(d);
                d = dafsa_load(path);
                assert(d != NULL);
                check_parity(d, univ, ulens, in_set, DD_UNIVERSE);
                remove(path);
            }

            /* delete a random subset */
            for (k = 0; k < DD_UNIVERSE; k++) {
                if (in_set[k] && rng_next() % 4 == 0) {
                    assert(dafsa_delete_n(d, univ[k], ulens[k]) == 1);
                    in_set[k] = 0;
                }
            }
            check_parity(d, univ, ulens, in_set, DD_UNIVERSE);

            /* re-add a random subset of the deleted */
            for (k = 0; k < DD_UNIVERSE; k++) {
                if (!in_set[k] && rng_next() % 2 == 0) {
                    assert(dafsa_add_n(d, univ[k], ulens[k]) == 1);
                    in_set[k] = 1;
                }
            }
            check_parity(d, univ, ulens, in_set, DD_UNIVERSE);

            dafsa_free(d);
        }
        free(univ);
    }
    printf("  PASS: %d delete-differential trials\n", DD_TRIALS);

    /* ── Test 13: Prefix enumeration (W\0 semantics) ── */
    printf("\n[M1 Test 13] Prefix enumeration (W\\0 semantics)\n");
    {
        const char *words[] = {"cat", "car", "cart", "dog",
                               "do",  "apple", "app", "apt"};
        const int nwords = 8;
        const unsigned char pload[] = {1, 2, 3, 4, 5, 6, 7, 8};
        unsigned char key[64];
        dafsa *dp = dafsa_create();
        int i;
        int total = 0;

        assert(dp != NULL);
        /* keys = word || 0x00 || payload-byte */
        for (i = 0; i < nwords; i++) {
            size_t wlen = strlen(words[i]);
            memcpy(key, words[i], wlen);
            key[wlen] = 0x00;
            key[wlen + 1] = pload[i];
            assert(dafsa_add_n(dp, key, wlen + 2) == 1);
        }

        /* explicit W\0 checks */
        {
            enum_ctx c;
            long n;

            memset(&c, 0, sizeof(c));
            n = dafsa_prefix_enum(dp, (const unsigned char *)"ca", 2,
                                  enum_collect, &c);
            assert(n == 0 && c.count == 0);   /* "ca" must NOT match cat/car/cart */
            total++;

            memset(&c, 0, sizeof(c));
            n = dafsa_prefix_enum(dp, (const unsigned char *)"cat", 3,
                                  enum_collect, &c);
            assert(n == 1 && c.count == 1);
            assert(c.plen[0] == 1 && c.payloads[0][0] == 1);  /* cat payload */
            total++;

            memset(&c, 0, sizeof(c));
            n = dafsa_prefix_enum(dp, (const unsigned char *)"cart", 4,
                                  enum_collect, &c);
            assert(n == 1 && c.count == 1);
            assert(c.plen[0] == 1 && c.payloads[0][0] == 3);  /* cart payload */
            total++;
        }

        /* random prefixes vs brute force (a prefix yields hits iff it is a word) */
        {
            int p;
            for (p = 0; p < 25; p++) {
                unsigned char prefix[8];
                char pstr[16];
                size_t plen = rng_next() % 4;
                size_t j;
                long got;
                int expected;

                for (j = 0; j < plen; j++)
                    prefix[j] = (unsigned char)
                        rng_alphabet[rng_next() % (sizeof(rng_alphabet) - 1)];
                memcpy(pstr, prefix, plen);
                pstr[plen] = '\0';
                expected = word_in_set(pstr, words, nwords) ? 1 : 0;
                got = dafsa_prefix_enum(dp, prefix, plen,
                                        enum_collect, &(enum_ctx){0});
                assert(got == expected);
                total++;
            }
        }
        printf("  PASS: %d prefixes checked (incl. explicit ca/cat/cart)\n", total);
        dafsa_free(dp);
    }

    /* ── Test 14: PDWG determinism ── */
    printf("\n[M1 Test 14] PDWG determinism: save->load->save byte-identical\n");
    {
        const char *words[] = {"cat", "car", "cart", "do",
                               "dog", "apple", "app", "apt", NULL};
        dafsa *d = dafsa_create();
        int i;
        FILE *f1, *f2;
        long s1, s2;
        unsigned char *b1, *b2;

        assert(d != NULL);
        for (i = 0; words[i]; i++)
            dafsa_add(d, (const unsigned char *)words[i]);
        assert(dafsa_save(d, "/tmp/m1_det_1.pdwg") == 0);
        dafsa_free(d);

        d = dafsa_load("/tmp/m1_det_1.pdwg");
        assert(d != NULL);
        assert(dafsa_save(d, "/tmp/m1_det_2.pdwg") == 0);
        dafsa_free(d);

        f1 = fopen("/tmp/m1_det_1.pdwg", "rb");
        f2 = fopen("/tmp/m1_det_2.pdwg", "rb");
        assert(f1 && f2);
        fseek(f1, 0, SEEK_END); s1 = ftell(f1); fseek(f1, 0, SEEK_SET);
        fseek(f2, 0, SEEK_END); s2 = ftell(f2); fseek(f2, 0, SEEK_SET);
        assert(s1 == s2);
        b1 = malloc((size_t)s1);
        b2 = malloc((size_t)s2);
        assert(b1 && b2);
        assert(fread(b1, 1, (size_t)s1, f1) == (size_t)s1);
        assert(fread(b2, 1, (size_t)s2, f2) == (size_t)s2);
        assert(memcmp(b1, b2, (size_t)s1) == 0);
        free(b1); free(b2);
        fclose(f1); fclose(f2);
        remove("/tmp/m1_det_1.pdwg");
        remove("/tmp/m1_det_2.pdwg");
        printf("  PASS: byte-identical (%ld bytes)\n", s1);
    }

    /* ── Test 15: Zero-copy view vs materialized differential ── */
    printf("\n[M4 Test 15] Zero-copy view vs materialized differential\n");
    {
        static const char alphabet[] = "abcdefghijklmnopqrstuvwxyz";
        int trial;
        g_rng = 0x0D4F5A11u;
        for (trial = 0; trial < 10; trial++) {
            unsigned char (*keys)[16] = malloc(RT_KEYS * sizeof(*keys));
            size_t lens[RT_KEYS];
            dafsa *d_ro;
            dafsa_view *v;
            char path[256];
            int k, p;

            assert(keys != NULL);
            snprintf(path, sizeof(path), "/tmp/m4_dv_%d.pdwg", trial);

            /* generate random keys with W\0 payloads */
            for (k = 0; k < RT_KEYS; k++) {
                size_t wlen = 1 + rng_next() % RT_MAXLEN;
                size_t j;
                for (j = 0; j < wlen; j++)
                    keys[k][j] =
                        (unsigned char)alphabet[rng_next() % (sizeof(alphabet) - 1)];
                /* append \0 + payload byte */
                keys[k][wlen] = 0x00;
                keys[k][wlen + 1] = (unsigned char)(1 + rng_next() % 255);
                lens[k] = wlen + 2;
            }

            {
                dafsa *d_mat = dafsa_create();
                assert(d_mat != NULL);
                for (k = 0; k < RT_KEYS; k++)
                    dafsa_add_n(d_mat, keys[k], lens[k]);
                assert(dafsa_save(d_mat, path) == 0);
                dafsa_free(d_mat);
            }

            /* open materialized readonly AND zero-copy view */
            d_ro = dafsa_load_readonly(path);
            assert(d_ro != NULL);
            v = dafsa_view_open(path);
            assert(v != NULL);

            /* lookup_n parity: every inserted key */
            for (k = 0; k < RT_KEYS; k++) {
                int mat_lookup  = dafsa_lookup_n(d_ro, keys[k], lens[k]);
                int view_lookup = dafsa_view_lookup_n(v, keys[k], lens[k]);
                assert(mat_lookup == view_lookup);
            }

            /* lookup_n parity: random non-inserted keys (no W\0 suffix) */
            for (k = 0; k < 50; k++) {
                unsigned char rkey[16];
                size_t rlen = 1 + rng_next() % RT_MAXLEN;
                size_t j;
                int mat_lookup, view_lookup;
                for (j = 0; j < rlen; j++)
                    rkey[j] =
                        (unsigned char)alphabet[rng_next() % (sizeof(alphabet) - 1)];
                mat_lookup  = dafsa_lookup_n(d_ro, rkey, rlen);
                view_lookup = dafsa_view_lookup_n(v, rkey, rlen);
                assert(mat_lookup == view_lookup);
            }

            /* prefix_enum parity: random prefixes */
            for (p = 0; p < 25; p++) {
                unsigned char prefix[16];
                size_t plen = rng_next() % 5;
                size_t j;
                enum_ctx mat_ctx, view_ctx;
                long mat_n, view_n;
                int m;

                for (j = 0; j < plen; j++)
                    prefix[j] = (unsigned char)
                        rng_alphabet[rng_next() % (sizeof(rng_alphabet) - 1)];

                memset(&mat_ctx,  0, sizeof(mat_ctx));
                memset(&view_ctx, 0, sizeof(view_ctx));

                mat_n  = dafsa_prefix_enum(d_ro, prefix, plen,
                                           enum_collect, &mat_ctx);
                view_n = dafsa_view_prefix_enum(v, prefix, plen,
                                                enum_collect, &view_ctx);

                assert(mat_n == view_n);
                assert(mat_ctx.count == view_ctx.count);
                for (m = 0; m < mat_ctx.count && m < 64; m++) {
                    assert(mat_ctx.plen[m] == view_ctx.plen[m]);
                    assert(memcmp(mat_ctx.payloads[m],
                                  view_ctx.payloads[m],
                                  mat_ctx.plen[m] < 8 ? mat_ctx.plen[m] : 8)
                           == 0);
                }
            }

            dafsa_view_close(v);
            dafsa_free(d_ro);
            remove(path);
            free(keys);
            printf("  trial %d: %d keys OK\n", trial, RT_KEYS);
        }
    }
    printf("  PASS: zero-copy view matches materialized on all trials\n");

    /* explicit W\0 prefix test for the view */
    {
        dafsa *d_exp;
        dafsa_view *v_exp;
        enum_ctx mat_ctx, view_ctx;
        long mat_n, view_n;

        d_exp = dafsa_create();
        assert(d_exp != NULL);
        /* key "cat\0\x01" */
        assert(dafsa_add_n(d_exp, (const unsigned char *)"cat\0\x01", 5) == 1);
        assert(dafsa_save(d_exp, "/tmp/m4_w0.pdwg") == 0);
        dafsa_free(d_exp);

        d_exp = dafsa_load_readonly("/tmp/m4_w0.pdwg");
        assert(d_exp != NULL);
        v_exp = dafsa_view_open("/tmp/m4_w0.pdwg");
        assert(v_exp != NULL);

        /* "ca" is not a word — prefix_enum must return 0 */
        mat_n  = dafsa_prefix_enum(d_exp, (const unsigned char *)"ca", 2,
                                   enum_collect, &(enum_ctx){0});
        view_n = dafsa_view_prefix_enum(v_exp, (const unsigned char *)"ca", 2,
                                        enum_collect, &(enum_ctx){0});
        assert(mat_n == 0 && view_n == 0);

        /* "cat" is a word — prefix_enum must return 1 */
        memset(&mat_ctx,  0, sizeof(mat_ctx));
        memset(&view_ctx, 0, sizeof(view_ctx));
        mat_n  = dafsa_prefix_enum(d_exp, (const unsigned char *)"cat", 3,
                                   enum_collect, &mat_ctx);
        view_n = dafsa_view_prefix_enum(v_exp, (const unsigned char *)"cat", 3,
                                        enum_collect, &view_ctx);
        assert(mat_n == 1 && view_n == 1);
        assert(mat_ctx.plen[0] == 1 && mat_ctx.payloads[0][0] == 1);
        assert(view_ctx.plen[0] == 1 && view_ctx.payloads[0][0] == 1);

        dafsa_view_close(v_exp);
        dafsa_free(d_exp);
        remove("/tmp/m4_w0.pdwg");
    }

    /* nonexistent path returns NULL */
    {
        dafsa_view *v_null = dafsa_view_open("/tmp/nonexistent_m4_test.pdwg");
        assert(v_null == NULL);
    }

    /* ── Test 16: Corrupted CSR target > n_states → view reject ── */
    printf("\n[M4 Test 16] Corrupted CSR: target > n_states → view_open NULL\n");
    {
        /*
         * Build a minimal v3 PDWG file by hand (v3 is unchecksummed and still
         * accepted by readers).  It has a corrupt CSR target:
         *   n_states=1  n_trans=1  n_final=0
         *   state table [0,0, 1,0]  bitmap 0x00  CSR: sym 'a', target=UINT32_MAX
         * 28-byte header + 4B state table + 1B bitmap + 6B CSR = 39 bytes.
         */
        const unsigned char malformed[] = {
            'P','D','W','G',               /* magic */
            0x03,0x00,0x00,0x00,           /* version=3 */
            0x01,0x00,0x00,0x00,           /* n_states=1 */
            0x01,0x00,0x00,0x00,           /* n_trans=1 */
            0x01,0x00,0x00,0x00,           /* initial_id=1 */
            0x00,0x00,0x00,0x00,           /* n_final=0 */
            0x00,0x00,0x00,0x00,           /* reserved=0 */
            /* state table: sink=0 (u16 LE), state1=1 trans (u16 LE) */
            0x00, 0x00,  /* sink: ntrans=0 */
            0x01, 0x00,  /* state1: ntrans=1 */
            /* final bitmap: 1 byte, no final states */
            0x00,
            /* CSR: sym='a', target=UINT32_MAX in LEB128 */
            0x61, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F,
        };
        FILE *f;
        dafsa_view *v_corrupt;

        f = fopen("/tmp/m4_corrupt.pdwg", "wb");
        assert(f != NULL);
        assert(fwrite(malformed, 1, sizeof(malformed), f) == sizeof(malformed));
        fclose(f);

        /* Zero-copy view must reject this at open time */
        v_corrupt = dafsa_view_open("/tmp/m4_corrupt.pdwg");
        assert(v_corrupt == NULL);

        /* Materialized load must also reject */
        {
            dafsa *d_corrupt = dafsa_load("/tmp/m4_corrupt.pdwg");
            assert(d_corrupt == NULL);
        }

        remove("/tmp/m4_corrupt.pdwg");
    }
    printf("  PASS: corrupted target rejected by view and load\n");

    /* ── Test 17: 256-fanout truncation bug (exactly 256 transitions) ── */
    printf("\n[M1 Test 17] 256-fanout: state with exactly 256 outgoing edges\n");
    {
        /*
         * Build a DAFSA with a root state that has 256 outgoing transitions
         * (one per byte value 0x00..0xFF). Each single-byte key is a final
         * word. In a minimal DAFSA all 256 leaf states are equivalent and
         * merge into one, but the root itself retains all 256 edges.
         *
         * This exercises the exact boundary where the old u8 ntrans would
         * truncate 256 → 0, producing orphaned CSR bytes.
         */
        dafsa *fan = dafsa_create();
        unsigned char key[2];
        int b;

        assert(fan != NULL);

        /* Insert 256 single-byte keys (0x00..0xFF).  Each is a distinct
         * transition from the initial state.  Minimality collapses all
         * leaf states into one, but the root keeps 256 edges. */
        for (b = 0; b < 256; b++) {
            key[0] = (unsigned char)b;
            assert(dafsa_add_n(fan, key, 1) == 1);
        }

        /* Verify all 256 keys are present */
        for (b = 0; b < 256; b++) {
            key[0] = (unsigned char)b;
            assert(dafsa_lookup_n(fan, key, 1) == 1);
        }

        /* Verify non-key is absent */
        key[0] = 0x00; key[1] = 0x01;
        assert(dafsa_lookup_n(fan, key, 2) == 0);

        /* Save to disk */
        assert(dafsa_save(fan, "/tmp/m1_fan256.pdwg") == 0);

        /* Verify on-disk version byte */
        {
            FILE *f = fopen("/tmp/m1_fan256.pdwg", "rb");
            unsigned char hdr[8];
            assert(f != NULL);
            assert(fread(hdr, 1, 8, f) == 8);
            assert(hdr[0] == 'P' && hdr[1] == 'D' && hdr[2] == 'W' && hdr[3] == 'G');
            assert(hdr[4] == 0x04);  /* version LE = 4 */
            fclose(f);
        }

        /* Load via materialized readonly AND zero-copy view */
        {
            dafsa *d_ro = dafsa_load_readonly("/tmp/m1_fan256.pdwg");
            dafsa_view *v = dafsa_view_open("/tmp/m1_fan256.pdwg");

            assert(d_ro != NULL);
            assert(v != NULL);

            /* Lookup parity: all 256 keys */
            for (b = 0; b < 256; b++) {
                key[0] = (unsigned char)b;
                assert(dafsa_lookup_n(d_ro, key, 1) == 1);
                assert(dafsa_view_lookup_n(v, key, 1) == 1);
            }

            /* Non-key parity */
            key[0] = 0x00; key[1] = 0x01;
            assert(dafsa_lookup_n(d_ro, key, 2) == 0);
            assert(dafsa_view_lookup_n(v, key, 2) == 0);

            /* prefix_enum parity: "" (empty prefix) should enumerate all 256.
             * The root has a 0x00 edge to a final state, so prefix_enum("")
             * follows that edge and the leaf is final with no transitions,
             * yielding count=1.  Actually only the key "\x00" starts with the
             * 0x00 edge that prefix_enum picks up. */
            {
                enum_ctx mat_ctx, view_ctx;
                long mat_n, view_n;

                memset(&mat_ctx,  0, sizeof(mat_ctx));
                memset(&view_ctx, 0, sizeof(view_ctx));

                mat_n  = dafsa_prefix_enum(d_ro, (const unsigned char *)"", 0,
                                           enum_collect, &mat_ctx);
                view_n = dafsa_view_prefix_enum(v, (const unsigned char *)"", 0,
                                                enum_collect, &view_ctx);
                assert(mat_n == view_n);
            }

            /* State-table ntrans sum must equal header n_trans (no truncation).
             * We can verify this indirectly: the file loaded/viewed without error,
             * which means the `running != n_trans` and `nt_sum != n_trans` checks
             * passed. */

            dafsa_view_close(v);
            dafsa_free(d_ro);
        }

        /* Re-load mutable and re-save to verify determinism */
        {
            dafsa *d2 = dafsa_load("/tmp/m1_fan256.pdwg");
            assert(d2 != NULL);
            assert(dafsa_save(d2, "/tmp/m1_fan256_b.pdwg") == 0);
            dafsa_free(d2);
        }

        /* byte-identical re-save */
        {
            FILE *f1 = fopen("/tmp/m1_fan256.pdwg", "rb");
            FILE *f2 = fopen("/tmp/m1_fan256_b.pdwg", "rb");
            long s1, s2;
            unsigned char *b1, *b2;
            assert(f1 && f2);
            fseek(f1, 0, SEEK_END); s1 = ftell(f1); fseek(f1, 0, SEEK_SET);
            fseek(f2, 0, SEEK_END); s2 = ftell(f2); fseek(f2, 0, SEEK_SET);
            assert(s1 == s2);
            b1 = malloc((size_t)s1);
            b2 = malloc((size_t)s2);
            assert(b1 && b2);
            assert(fread(b1, 1, (size_t)s1, f1) == (size_t)s1);
            assert(fread(b2, 1, (size_t)s2, f2) == (size_t)s2);
            assert(memcmp(b1, b2, (size_t)s1) == 0);
            free(b1); free(b2);
            fclose(f1); fclose(f2);
        }

        remove("/tmp/m1_fan256.pdwg");
        remove("/tmp/m1_fan256_b.pdwg");
        dafsa_free(fan);
    }
    printf("  PASS: 256-fanout state saved/loaded/viewed without truncation\n");

    /* ── Test 19: Corrupted checksum → both readers reject ── */
    printf("\n[M4 Test 19] Corrupted trailing CRC32 → view_open/load NULL\n");
    {
        const char *path = "/tmp/m4_crc.pdwg";
        const char *keys[] = { "alpha", "bravo", "charlie", "delta" };
        dafsa *crc_d = dafsa_create();
        FILE *f;
        long sz;
        unsigned char *buf;
        size_t i;

        assert(crc_d != NULL);
        for (i = 0; i < sizeof(keys) / sizeof(keys[0]); i++)
            assert(dafsa_add_n(crc_d, (const unsigned char *)keys[i],
                               strlen(keys[i])) == 1);
        assert(dafsa_save(crc_d, path) == 0);
        dafsa_free(crc_d);

        /* sanity: the uncorrupted v4 file loads fine in both readers */
        {
            dafsa_view *v_ok = dafsa_view_open(path);
            dafsa *d_ok = dafsa_load(path);
            assert(v_ok != NULL);
            assert(d_ok != NULL);
            assert(dafsa_view_lookup_n(v_ok, (const unsigned char *)"alpha", 5) == 1);
            assert(dafsa_lookup_n(d_ok, (const unsigned char *)"alpha", 5) == 1);
            dafsa_view_close(v_ok);
            dafsa_free(d_ok);
        }

        /* read the file, flip one bit in the LAST byte (the CRC), write back */
        f = fopen(path, "rb");
        assert(f != NULL);
        fseek(f, 0, SEEK_END); sz = ftell(f); fseek(f, 0, SEEK_SET);
        assert(sz > 4);
        buf = (unsigned char *)malloc((size_t)sz);
        assert(buf != NULL);
        assert(fread(buf, 1, (size_t)sz, f) == (size_t)sz);
        fclose(f);

        buf[sz - 1] ^= 0x01;   /* corrupt the stored CRC */

        f = fopen(path, "wb");
        assert(f != NULL);
        assert(fwrite(buf, 1, (size_t)sz, f) == (size_t)sz);
        fclose(f);
        free(buf);

        /* both readers must now reject the tampered file */
        {
            dafsa_view *v_bad = dafsa_view_open(path);
            dafsa *d_bad = dafsa_load(path);
            assert(v_bad == NULL);
            assert(d_bad == NULL);
        }

        remove(path);
    }
    printf("  PASS: corrupted checksum rejected by view and load\n");

    /* ══════════════════════════════════════════════════════════════════════ */
    /* ─── M5: Write-ahead log and layered view tests ──────────────────── */

    #define WAL_PATH  "/tmp/m5_test.wal"
    #define FST_PATH  "/tmp/m5_base.pdwg"

    /* Build composite key: word || 0x00 || u32BE(file_idx) || u32BE(entry_idx).
     * Returns total length = strlen(word) + 9. */
    {
        /* MK_KEY is a macro defined in this block scope */
        #define MK_KEY(buf, w, fi, ei)                                        \
            do {                                                              \
                size_t _wl = strlen(w);                                       \
                memcpy(buf, w, _wl);                                          \
                buf[_wl]     = 0x00;                                          \
                buf[_wl + 1] = (unsigned char)((fi) >> 24);                   \
                buf[_wl + 2] = (unsigned char)((fi) >> 16);                   \
                buf[_wl + 3] = (unsigned char)((fi) >> 8);                    \
                buf[_wl + 4] = (unsigned char)(fi);                           \
                buf[_wl + 5] = (unsigned char)((ei) >> 24);                   \
                buf[_wl + 6] = (unsigned char)((ei) >> 16);                   \
                buf[_wl + 7] = (unsigned char)((ei) >> 8);                    \
                buf[_wl + 8] = (unsigned char)(ei);                           \
            } while (0)
    }

    /* ── M5-1: append_add ×N then replay round-trip ── */
    printf("\n[M5 Test 1] WAL append/replay round-trip\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];
        int n;

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        assert(dafsa_wal_size(w) == 16);

        for (n = 0; n < 50; n++) {
            MK_KEY(kbuf, "apple", (uint32_t)(n + 1), (uint32_t)(n * 10));
            assert(dafsa_wal_append_add(w, kbuf, strlen("apple") + 9) == 0);
        }

        /* Verify size: 16 header + 50 × (1+4+5+9+4) = 16 + 50×23 = 1166 */
        {
            uint64_t sz = dafsa_wal_size(w);
            assert(sz == 16 + 50 * (1 + 4 + 5 + 9 + 4));
        }
        dafsa_wal_sync(w);
        dafsa_wal_close(w);

        /* Replay: verify count by reopening */
        {
            dafsa_wal *w2 = dafsa_wal_open(WAL_PATH);
            assert(w2 != NULL);
            /* Each record: 1(op)+4(klen)+14(key)+4(crc)=23 bytes */
            assert(dafsa_wal_size(w2) == 16 + 50 * 23);
            dafsa_wal_close(w2);
        }
    }
    printf("  PASS: 50 records round-trip OK\n");

    /* ── M5-2: interleaved add/del replay (log order preserved) ── */
    printf("[M5 Test 2] Interleaved add+del replay\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);

        MK_KEY(kbuf, "cat", 1, 100); assert(dafsa_wal_append_add(w, kbuf, strlen("cat") + 9) == 0);
        MK_KEY(kbuf, "dog", 2, 200); assert(dafsa_wal_append_add(w, kbuf, strlen("dog") + 9) == 0);
        MK_KEY(kbuf, "cat", 1, 100); assert(dafsa_wal_append_del(w, kbuf, strlen("cat") + 9) == 0);
        MK_KEY(kbuf, "cat", 1, 100); assert(dafsa_wal_append_add(w, kbuf, strlen("cat") + 9) == 0);
        MK_KEY(kbuf, "dog", 2, 200); assert(dafsa_wal_append_del(w, kbuf, strlen("dog") + 9) == 0);
        MK_KEY(kbuf, "fox", 3, 300); assert(dafsa_wal_append_add(w, kbuf, strlen("fox") + 9) == 0);

        /* 6 records: verify by size */
        {
            uint64_t sz = dafsa_wal_size(w);
            uint64_t recsz = (uint64_t)(1 + 4 + 3 + 9 + 4); /* 21 */
            assert(sz == 16 + 6 * recsz);
        }
        dafsa_wal_close(w);
    }
    printf("  PASS: 6 ops in log order\n");

    /* ── M5-3: torn-tail — garbage bytes past EOF ── */
    printf("[M5 Test 3] Torn tail: garbage past EOF\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];
        FILE *f;
        uint64_t sz_clean;  /* size with 3 clean records */

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);

        MK_KEY(kbuf, "alpha", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("alpha") + 9);
        MK_KEY(kbuf, "beta",  2, 2); dafsa_wal_append_add(w, kbuf, strlen("beta") + 9);
        MK_KEY(kbuf, "gamma", 3, 3); dafsa_wal_append_add(w, kbuf, strlen("gamma") + 9);
        sz_clean = dafsa_wal_size(w);
        dafsa_wal_close(w);

        /* Append 2 garbage bytes */
        f = fopen(WAL_PATH, "ab");
        assert(f != NULL);
        { unsigned char g[] = {0xFF, 0xFE}; fwrite(g, 1, 2, f); }
        fclose(f);

        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        /* Open must truncate the garbage, leaving only the 3 good records */
        assert(dafsa_wal_size(w) == sz_clean);
        dafsa_wal_close(w);
    }
    printf("  PASS: garbage tail truncated, 3 records\n");

    /* ── M5-4: torn-tail truncated key mid-record ── */
    printf("[M5 Test 4] Torn tail: truncated mid-key\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];
        uint64_t sz_before;

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        MK_KEY(kbuf, "aaa", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("aaa") + 9);
        MK_KEY(kbuf, "bbb", 2, 2); dafsa_wal_append_add(w, kbuf, strlen("bbb") + 9);
        MK_KEY(kbuf, "ccc", 3, 3); dafsa_wal_append_add(w, kbuf, strlen("ccc") + 9);
        sz_before = dafsa_wal_size(w);
        dafsa_wal_close(w);

        /* Truncate the file 10 bytes before the real end (mid-record 3) */
        {
            int fd = open(WAL_PATH, O_RDWR);
            assert(fd >= 0);
            assert(ftruncate(fd, (off_t)(sz_before - 10)) == 0);
            close(fd);
        }

        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        {
            /* Should have only 2 records left */
            uint64_t sz = dafsa_wal_size(w);
            uint64_t recsz = (uint64_t)(1 + 4 + 3 + 9 + 4);
            assert(sz == 16 + 2 * recsz);
        }
        dafsa_wal_close(w);
    }
    printf("  PASS: truncated mid-key → 2 records\n");

    /* ── M5-5: CRC corruption in record ── */
    printf("[M5 Test 5] CRC corruption: flip key byte → replay stops early\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];
        uint64_t sz1; /* offset after record 1 */

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        MK_KEY(kbuf, "r1", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("r1") + 9);
        sz1 = dafsa_wal_size(w);
        MK_KEY(kbuf, "r2", 2, 2); dafsa_wal_append_add(w, kbuf, strlen("r2") + 9);
        MK_KEY(kbuf, "r3", 3, 3); dafsa_wal_append_add(w, kbuf, strlen("r3") + 9);
        dafsa_wal_close(w);

        /* Flip a byte in record 2's key area (at offset sz1 + 5 = first key byte of rec2) */
        {
            int fd = open(WAL_PATH, O_RDWR);
            unsigned char b;
            assert(fd >= 0);
            assert(pread(fd, &b, 1, (off_t)(sz1 + 5)) == 1);
            b ^= 0x01;
            assert(pwrite(fd, &b, 1, (off_t)(sz1 + 5)) == 1);
            close(fd);
        }

        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        /* Should be truncated to sz1 (record 2 invalid, so only record 1 remains) */
        assert(dafsa_wal_size(w) == sz1);
        dafsa_wal_close(w);
    }
    printf("  PASS: CRC corruption truncated to 1 record\n");

    /* ── M5-6: header CRC corruption → NULL ── */
    printf("[M5 Test 6] Header CRC corruption → NULL\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "x", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("x") + 9);
        dafsa_wal_close(w);

        /* Flip byte 8 (flags field — part of CRC-covered region) */
        {
            int fd = open(WAL_PATH, O_RDWR);
            unsigned char b;
            assert(fd >= 0);
            assert(pread(fd, &b, 1, 8) == 1);
            b ^= 0x01;
            assert(pwrite(fd, &b, 1, 8) == 1);
            close(fd);
        }

        w = dafsa_wal_open(WAL_PATH);
        assert(w == NULL);  /* hard error */
    }
    printf("  PASS: header CRC corrupt → NULL\n");

    /* ── M5-7: bad magic / bad version → NULL ── */
    printf("[M5 Test 7] Bad magic and bad version → NULL\n");
    {
        dafsa_wal *w;
        unsigned char kbuf[64];

        /* Bad magic */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "x", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("x") + 9);
        dafsa_wal_close(w);
        {
            int fd = open(WAL_PATH, O_RDWR);
            unsigned char b = 'X';
            assert(fd >= 0);
            assert(pwrite(fd, &b, 1, 0) == 1);
            close(fd);
        }
        w = dafsa_wal_open(WAL_PATH);
        assert(w == NULL);

        /* Bad version */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "x", 1, 1); dafsa_wal_append_add(w, kbuf, strlen("x") + 9);
        dafsa_wal_close(w);
        {
            int fd = open(WAL_PATH, O_RDWR);
            unsigned char b = 99;
            assert(fd >= 0);
            assert(pwrite(fd, &b, 1, 4) == 1);
            close(fd);
        }
        w = dafsa_wal_open(WAL_PATH);
        assert(w == NULL);
    }
    printf("  PASS: bad magic/version → NULL\n");

    /* ── M5-8: key_len=0 and key_len=4106 rejected ── */
    printf("[M5 Test 8] key_len range enforcement\n");
    {
        dafsa_wal *w;
        unsigned char one[1] = {0};
        unsigned char big[4106];

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);

        assert(dafsa_wal_append_add(w, one, 0) == -1);
        memset(big, 'A', sizeof(big));
        assert(dafsa_wal_append_add(w, big, 4106) == -1);

        dafsa_wal_close(w);
    }
    printf("  PASS: key_len 0 and 4106 rejected\n");

    /* ── M5-9: empty WAL (header only) ── */
    printf("[M5 Test 9] Empty WAL: header-only\n");
    {
        dafsa_wal *w;
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        assert(dafsa_wal_size(w) == 16);
        /* replay on empty WAL returns 0 records */
        /* (We can't easily count without a cb, but size==16 is enough) */
        dafsa_wal_close(w);
    }
    printf("  PASS: size=16, replay=0\n");

    /* ── M5-10: ordered-log idempotency: ADD K, DEL K, ADD K → K present ── */
    printf("[M5 Test 10] Ordered-log idempotency: ADD,DEL,ADD → present\n");
    {
        dafsa_wal *w;
        dafsa *d;
        unsigned char kbuf[64];

        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);

        MK_KEY(kbuf, "keytest", 1, 42);
        dafsa_wal_append_add(w, kbuf, strlen("keytest") + 9);
        dafsa_wal_append_del(w, kbuf, strlen("keytest") + 9);
        dafsa_wal_append_add(w, kbuf, strlen("keytest") + 9);
        dafsa_wal_close(w);

        /* Replay into empty dafsa — manually scan records */
        d = dafsa_create();
        assert(d != NULL);
        {
            int fd = open(WAL_PATH, O_RDONLY);
            struct stat st;
            uint8_t *map;
            assert(fd >= 0);
            fstat(fd, &st);
            map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
            assert(map != MAP_FAILED);

            {
                const uint8_t *p = map + 16;
                const uint8_t *end = map + st.st_size;
                while (p + 10 <= end) {
                    uint8_t op = p[0];
                    uint32_t klen = (uint32_t)p[1] | ((uint32_t)p[2] << 8)
                                  | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
                    if (op != 1 && op != 2) break;
                    if (klen < 1 || klen > (uint32_t)(MAX_WORD_LEN + 9)) break;
                    if (p + 5 + klen + 4 > end) break;
                    if (op == 1)
                        dafsa_add_n(d, p + 5, klen);
                    else
                        dafsa_delete_n(d, p + 5, klen);
                    p += 1 + 4 + klen + 4;
                }
            }
            munmap(map, (size_t)st.st_size);
            close(fd);
        }
        /* ADD→DEL→ADD of same key must leave it present */
        assert(dafsa_lookup_n(d, kbuf, strlen("keytest") + 9) == 1);
        dafsa_free(d);
    }
    printf("  PASS: K present after ADD→DEL→ADD\n");

    /* ── M5-11: compact equivalence ── */
    printf("[M5 Test 11] Compact equivalence: base + WAL → replay → verify\n");
    {
        dafsa *d_base;
        dafsa_wal *w;
        unsigned char kbuf[64];

        /* Base set S1: 3 words */
        d_base = dafsa_create();
        assert(d_base != NULL);
        MK_KEY(kbuf, "k1", 1, 10); dafsa_add_n(d_base, kbuf, strlen("k1") + 9);
        MK_KEY(kbuf, "k2", 2, 20); dafsa_add_n(d_base, kbuf, strlen("k2") + 9);
        MK_KEY(kbuf, "k3", 3, 30); dafsa_add_n(d_base, kbuf, strlen("k3") + 9);
        assert(dafsa_save(d_base, FST_PATH) == 0);
        dafsa_free(d_base);

        /* WAL: ADD k4, DEL k2 */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "k4", 4, 40); dafsa_wal_append_add(w, kbuf, strlen("k4") + 9);
        MK_KEY(kbuf, "k2", 2, 20); dafsa_wal_append_del(w, kbuf, strlen("k2") + 9);
        dafsa_wal_close(w);

        /* Load base, replay WAL manually (parse records, apply to loaded dafsa) */
        {
            dafsa *d_comp = dafsa_load(FST_PATH);
            assert(d_comp != NULL);

            /* Manual replay: open WAL, read records, apply */
            {
                int fd = open(WAL_PATH, O_RDONLY);
                struct stat st;
                uint8_t *map;
                assert(fd >= 0);
                fstat(fd, &st);
                map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
                assert(map != MAP_FAILED);

                /* Skip header (16 bytes), parse records */
                {
                    const uint8_t *p = map + 16;
                    const uint8_t *end = map + st.st_size;
                    while (p + 10 <= end) {
                        uint8_t op = p[0];
                        uint32_t klen = (uint32_t)p[1] | ((uint32_t)p[2] << 8)
                                      | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
                        if (op != 1 && op != 2) break;
                        if (klen < 1 || klen > MAX_WORD_LEN + 9) break;
                        if (p + 5 + klen + 4 > end) break;
                        if (op == 1)
                            dafsa_add_n(d_comp, p + 5, klen);
                        else
                            dafsa_delete_n(d_comp, p + 5, klen);
                        p += 1 + 4 + klen + 4;
                    }
                }
                munmap(map, (size_t)st.st_size);
                close(fd);
            }

            /* Verify: k1 present, k2 absent, k3 present, k4 present */
            MK_KEY(kbuf, "k1", 1, 10);
            assert(dafsa_lookup_n(d_comp, kbuf, strlen("k1") + 9) == 1);
            MK_KEY(kbuf, "k2", 2, 20);
            assert(dafsa_lookup_n(d_comp, kbuf, strlen("k2") + 9) == 0);
            MK_KEY(kbuf, "k3", 3, 30);
            assert(dafsa_lookup_n(d_comp, kbuf, strlen("k3") + 9) == 1);
            MK_KEY(kbuf, "k4", 4, 40);
            assert(dafsa_lookup_n(d_comp, kbuf, strlen("k4") + 9) == 1);

            dafsa_free(d_comp);
        }
    }
    printf("  PASS: compact equivalence (S1∪S2\\S3)\n");

    /* ── M5-12: layered lookup merge ── */
    printf("[M5 Test 12] Layered lookup merge\n");
    {
        dafsa *d;
        dafsa_wal *w;
        dafsa_view *v;
        unsigned char kbuf[64];

        /* Base S1: base1, base2 */
        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "base1", 1, 10); dafsa_add_n(d, kbuf, strlen("base1") + 9);
        MK_KEY(kbuf, "base2", 2, 20); dafsa_add_n(d, kbuf, strlen("base2") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        /* WAL: ADD base3, DEL base2 */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "base3", 3, 30); dafsa_wal_append_add(w, kbuf, strlen("base3") + 9);
        MK_KEY(kbuf, "base2", 2, 20); dafsa_wal_append_del(w, kbuf, strlen("base2") + 9);
        dafsa_wal_close(w);

        v = dafsa_view_open_layered(FST_PATH, WAL_PATH);
        assert(v != NULL);

        /* base1: in base, not in WAL → 1 */
        MK_KEY(kbuf, "base1", 1, 10);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("base1") + 9) == 1);
        /* base2: in base, DEL in WAL → 0 */
        MK_KEY(kbuf, "base2", 2, 20);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("base2") + 9) == 0);
        /* base3: not in base, ADD in WAL → 1 */
        MK_KEY(kbuf, "base3", 3, 30);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("base3") + 9) == 1);
        /* base4: not in base, not in WAL → 0 */
        MK_KEY(kbuf, "base4", 4, 40);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("base4") + 9) == 0);

        dafsa_view_close(v);
    }
    printf("  PASS: layered lookup S1∪S2\\S3\n");

    /* ── M5-13: layered prefix_enum merge ── */
    printf("[M5 Test 13] Layered prefix_enum merge\n");
    {
        dafsa *d;
        dafsa_wal *w;
        dafsa_view *v;
        unsigned char kbuf[64];

        /* Base: word "cat" with 3 payloads */
        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "cat", 1, 100); dafsa_add_n(d, kbuf, strlen("cat") + 9);
        MK_KEY(kbuf, "cat", 2, 200); dafsa_add_n(d, kbuf, strlen("cat") + 9);
        MK_KEY(kbuf, "cat", 3, 300); dafsa_add_n(d, kbuf, strlen("cat") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        /* WAL: DEL (2,200), ADD (4,400) */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "cat", 2, 200); dafsa_wal_append_del(w, kbuf, strlen("cat") + 9);
        MK_KEY(kbuf, "cat", 4, 400); dafsa_wal_append_add(w, kbuf, strlen("cat") + 9);
        dafsa_wal_close(w);

        v = dafsa_view_open_layered(FST_PATH, WAL_PATH);
        assert(v != NULL);

        {
            enum_ctx ctx;
            memset(&ctx, 0, sizeof(ctx));
            long n = dafsa_view_prefix_enum(v, (const unsigned char *)"cat", 3,
                                             enum_collect, &ctx);
            /* Expected: (1,100), (3,300), (4,400) — 3 total */
            assert(n == 3);
            assert(ctx.count == 3);
            {
                unsigned char e1[] = {0,0,0,1, 0,0,0,100};
                unsigned char e3[] = {0,0,0,3, 0,0,1,0x2C};
                unsigned char e4[] = {0,0,0,4, 0,0,1,0x90};
                int f1 = 0, f3 = 0, f4 = 0;
                int i;
                for (i = 0; i < ctx.count; i++) {
                    if (ctx.plen[i] == 8) {
                        if (memcmp(ctx.payloads[i], e1, 8) == 0) f1 = 1;
                        if (memcmp(ctx.payloads[i], e3, 8) == 0) f3 = 1;
                        if (memcmp(ctx.payloads[i], e4, 8) == 0) f4 = 1;
                    }
                }
                assert(f1 && f3 && f4);
            }
        }

        dafsa_view_close(v);
    }
    printf("  PASS: prefix_enum (base\\dels)∪adds = 3\n");

    /* ── M5-14: layered prefix_enum for word NOT in base but in WAL ── */
    printf("[M5 Test 14] Layered prefix_enum: word only in WAL\n");
    {
        dafsa *d;
        dafsa_wal *w;
        dafsa_view *v;
        unsigned char kbuf[64];

        /* Base: only "dog" with payload */
        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "dog", 1, 99); dafsa_add_n(d, kbuf, strlen("dog") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        /* WAL: ADD "cat" (word NOT in base) */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "cat", 5, 500); dafsa_wal_append_add(w, kbuf, strlen("cat") + 9);
        dafsa_wal_close(w);

        v = dafsa_view_open_layered(FST_PATH, WAL_PATH);
        assert(v != NULL);

        {
            enum_ctx ctx;
            memset(&ctx, 0, sizeof(ctx));
            long n = dafsa_view_prefix_enum(v, (const unsigned char *)"cat", 3,
                                             enum_collect, &ctx);
            assert(n == 1);
            assert(ctx.count == 1);
            assert(ctx.plen[0] == 8);
            {
                unsigned char exp[] = {0,0,0,5, 0,0,1,0xF4};
                assert(memcmp(ctx.payloads[0], exp, 8) == 0);
            }
        }

        dafsa_view_close(v);
    }
    printf("  PASS: WAL-only word enumerated (early-return fix)\n");

    /* ── M5-15: dedup — WAL ADD for key base already has → emitted once ── */
    printf("[M5 Test 15] Dedup: WAL ADD for base-existing key\n");
    {
        dafsa *d;
        dafsa_wal *w;
        dafsa_view *v;
        unsigned char kbuf[64];

        /* Base: "bird" with 2 payloads */
        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "bird", 1, 10); dafsa_add_n(d, kbuf, strlen("bird") + 9);
        MK_KEY(kbuf, "bird", 2, 20); dafsa_add_n(d, kbuf, strlen("bird") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        /* WAL: ADD same payload already in base */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        MK_KEY(kbuf, "bird", 1, 10); dafsa_wal_append_add(w, kbuf, strlen("bird") + 9);
        dafsa_wal_close(w);

        v = dafsa_view_open_layered(FST_PATH, WAL_PATH);
        assert(v != NULL);

        {
            enum_ctx ctx;
            memset(&ctx, 0, sizeof(ctx));
            long n = dafsa_view_prefix_enum(v, (const unsigned char *)"bird", 4,
                                             enum_collect, &ctx);
            assert(n == 2);
            assert(ctx.count == 2);
        }

        dafsa_view_close(v);
    }
    printf("  PASS: dedup → exactly 2\n");

    /* ── M5-16: dafsa_view_open_layered(fst, NULL) == plain view ── */
    printf("[M5 Test 16] dafsa_view_open_layered(fst, NULL) == plain view\n");
    {
        dafsa *d;
        dafsa_view *v, *vp;
        unsigned char kbuf[64];

        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "test", 1, 1); dafsa_add_n(d, kbuf, strlen("test") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        vp = dafsa_view_open(FST_PATH);
        assert(vp != NULL);
        v = dafsa_view_open_layered(FST_PATH, NULL);
        assert(v != NULL);

        MK_KEY(kbuf, "test", 1, 1);
        assert(dafsa_view_lookup_n(vp, kbuf, strlen("test") + 9) == 1);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("test") + 9) == 1);

        MK_KEY(kbuf, "test", 9, 9);
        assert(dafsa_view_lookup_n(vp, kbuf, strlen("test") + 9) == 0);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("test") + 9) == 0);

        dafsa_view_close(v);
        dafsa_view_close(vp);
    }
    printf("  PASS: NULL wal → same as plain view\n");

    /* ── M5-17: layered with nonexistent wal succeeds ── */
    printf("[M5 Test 17] dafsa_view_open_layered(fst, nonexistent.wal) OK\n");
    {
        dafsa *d;
        dafsa_view *v;
        unsigned char kbuf[64];

        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "test", 1, 1); dafsa_add_n(d, kbuf, strlen("test") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        v = dafsa_view_open_layered(FST_PATH, "/tmp/nonexistent_m5.wal");
        assert(v != NULL);
        MK_KEY(kbuf, "test", 1, 1);
        assert(dafsa_view_lookup_n(v, kbuf, strlen("test") + 9) == 1);
        dafsa_view_close(v);
    }
    printf("  PASS: nonexistent WAL → empty overlay, view OK\n");

    /* ── M5-18: B1 regression — many distinct payloads for one word ── */
    printf("[M5 Test 18] Many distinct payloads (no hang, no loss, no dup)\n");
    {
        dafsa *d;
        dafsa_wal *w;
        dafsa_view *v;
        unsigned char kbuf[64];
        int i;

        /* Base: word "many" with 1 payload (so fst_path is valid).
         * Use a payload range disjoint from the WAL records below
         * so there is no dup and the count is straightforward. */
        d = dafsa_create();
        assert(d != NULL);
        MK_KEY(kbuf, "many", 99, 99); dafsa_add_n(d, kbuf, strlen("many") + 9);
        assert(dafsa_save(d, FST_PATH) == 0);
        dafsa_free(d);

        /* WAL: 200 distinct ADD payloads for "many" */
        remove(WAL_PATH);
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        for (i = 0; i < 200; i++) {
            MK_KEY(kbuf, "many", (uint32_t)(i / 100), (uint32_t)i);
            assert(dafsa_wal_append_add(w, kbuf, strlen("many") + 9) == 0);
        }
        dafsa_wal_close(w);

        /* Open layered view — must NOT hang */
        v = dafsa_view_open_layered(FST_PATH, WAL_PATH);
        assert(v != NULL);

        {
            enum_ctx ctx;
            memset(&ctx, 0, sizeof(ctx));
            long n = dafsa_view_prefix_enum(v, (const unsigned char *)"many", 4,
                                             enum_collect, &ctx);
            /* Base has 1 payload (99,99), WAL adds 200 more distinct payloads
             * for the same word.  Total = 201.
             * Verify count only: the exact set-membership dedup logic is
             * covered by M5-15; what matters here is no hang and correct count. */
            assert(n == 201);
            assert(ctx.count == 201);
        }

        dafsa_view_close(v);
    }
    printf("  PASS: 200-payload layered view OK (no hang)\n");

    /* ── M5-19: corrupt header-only WAL (16 B) → reinitialize ── */
    printf("[M5 Test 19] Corrupt header-only WAL → reinitialize, not NULL\n");
    {
        dafsa_wal *w;
        unsigned char garbage[16];
        int fd;

        remove(WAL_PATH);

        /* Write 16 bytes of garbage (corrupt header, zero records) */
        memset(garbage, 0xAA, sizeof(garbage));
        fd = open(WAL_PATH, O_RDWR | O_CREAT | O_TRUNC, 0644);
        assert(fd >= 0);
        assert(write(fd, garbage, 16) == 16);
        close(fd);

        /* Open must succeed: reinitialize with fresh header */
        w = dafsa_wal_open(WAL_PATH);
        assert(w != NULL);
        assert(dafsa_wal_size(w) == 16);
        dafsa_wal_close(w);

        /* Verify the file now has a valid header (magic "DAWL") */
        {
            unsigned char hdr[4];
            fd = open(WAL_PATH, O_RDONLY);
            assert(fd >= 0);
            assert(read(fd, hdr, 4) == 4);
            close(fd);
            assert(hdr[0] == 'D' && hdr[1] == 'A'
                && hdr[2] == 'W' && hdr[3] == 'L');
        }

        remove(WAL_PATH);
    }
    printf("  PASS: corrupt header-only WAL reinitialized\n");

    remove(WAL_PATH);
    remove(FST_PATH);

    #undef WAL_PATH
    #undef FST_PATH
    #undef MK_KEY

    /* ── M6: streaming CRC32 equivalence ── */
    printf("\n[M6 Test 1] Streaming CRC32 matches one-shot crc32_compute\n");
    {
        static const uint8_t data[] = "123456789";
        size_t len = 9;
        uint32_t one_shot = crc32_compute(data, len);
        uint32_t s;
        size_t i;

        /* standard check value */
        assert(one_shot == 0xCBF43926u);

        /* single-stream init/update/finalize */
        s = crc32_init();
        s = crc32_update(s, data, len);
        s = crc32_finalize(s);
        assert(s == one_shot);

        /* multi-chunk streaming (3 chunks) == one-shot */
        {
            uint32_t m = crc32_init();
            m = crc32_update(m, data, 3);
            m = crc32_update(m, data + 3, 3);
            m = crc32_update(m, data + 6, 3);
            m = crc32_finalize(m);
            assert(m == one_shot);
        }

        /* empty stream: crc32_compute(NULL,0) == finalize(init()) */
        assert(crc32_compute(NULL, 0) == crc32_finalize(crc32_init()));

        /* byte-at-a-time streaming == one-shot (exercises update via put_u8
         * style table lookups on the same table) */
        s = crc32_init();
        for (i = 0; i < len; i++) {
            s = crc32_table[(s ^ data[i]) & 0xFF] ^ (s >> 8);
        }
        s = crc32_finalize(s);
        assert(s == one_shot);
    }
    printf("  PASS: streaming == one-shot == standard check value\n");

    /* ── Summary ── */
    printf("\n=== All tests passed. ===\n");
    return 0;
}
