/*
 * _crash_test.c — kill -9 atomicity regression test for dafsa_save
 *
 * Verifies that dafsa_save is atomic: a child that loads the index, adds 100
 * distinguishable "new" keys, and re-saves is SIGKILL'd at a random point in
 * the write.  After the kill the on-disk index must contain either the full
 * old set (0 new keys) or the full new set (100 new keys) — never a partial
 * count in between.
 *
 * dafsa_save commit sequence and the crash windows we exercise (see
 * dafsa_persist.c::dafsa_save):
 *   1. write  PATH.tmp   (tmp write — killed here leaves old PATH + stray .tmp)
 *   2. fflush + fsync(PATH.tmp)   (data made durable, before rename)
 *   3. rename(PATH.tmp -> PATH)   (atomic point of visibility)
 *   4. fsync_dir_of(PATH)         (rename made durable)
 * In every window the old PATH remains valid (0 new) or the rename already
 * committed (100 new); a partial write is never visible at PATH.
 *
 * Build (sandbox multi-TU form):
 *   cd indexer/dafsa
 *   gcc -O2 -Wall -Wextra -Werror -I. -o _crash_test _crash_test.c \
 *       dafsa.c dafsa_state.c dafsa_core.c dafsa_persist.c dafsa_view.c
 *   ./_crash_test
 */
#include "dafsa.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>

#define N_ORIG      200    /* original keys (len 3-10, a-z) */
#define N_NEW       100    /* distinguishable new keys (leading 0xFF) */
#define TRIALS      100
#define PATH        "_crash_idx.pdwg"
#define TMPPATH     "_crash_idx.pdwg.tmp"

/* Deterministic LCG. */
static uint32_t g_rng = 0xC0FFEE11u;
static uint32_t rng_next(void)
{
    g_rng = g_rng * 1664525u + 1013904223u;
    return g_rng;
}

static const char lc[] = "abcdefghijklmnopqrstuvwxyz";

/* Build an original key: len 3..10 lowercase a-z. */
static size_t mk_orig(unsigned char *buf)
{
    size_t len = 3 + (size_t)(rng_next() % 8);   /* 3..10 */
    size_t i;
    for (i = 0; i < len; i++)
        buf[i] = (unsigned char)lc[rng_next() % 26];
    return len;
}

/* Build a distinguishable new key: 0xFF + 3..8 lowercase. */
static size_t mk_new(unsigned char *buf)
{
    size_t len = 1 + 3 + (size_t)(rng_next() % 6);   /* 4..9 bytes total */
    size_t i;
    buf[0] = 0xFF;
    for (i = 1; i < len; i++)
        buf[i] = (unsigned char)lc[rng_next() % 26];
    return len;
}

int main(void)
{
    dafsa *d;
    unsigned char orig[N_ORIG][12];
    size_t orig_len[N_ORIG];
    unsigned char newk[N_NEW][12];
    size_t newk_len[N_NEW];
    int trial;
    int n_zero = 0, n_hundred = 0, n_bad = 0;

    /* 1. Create the original set and persist it. */
    d = dafsa_create();
    if (!d) { fprintf(stderr, "create OOM\n"); return 2; }
    {
        int i;
        for (i = 0; i < N_ORIG; i++) {
            orig_len[i] = mk_orig(orig[i]);
            if (dafsa_add_n(d, orig[i], orig_len[i]) < 0) {
                fprintf(stderr, "add orig failed\n");
                return 2;
            }
        }
    }
    if (dafsa_save(d, PATH) != 0) {
        fprintf(stderr, "initial save failed\n");
        return 2;
    }

    /* Build the distinguishable new-key set (once). */
    {
        int i;
        for (i = 0; i < N_NEW; i++)
            newk_len[i] = mk_new(newk[i]);
    }

    /* 2. Load and verify all originals present. */
    {
        dafsa *d2 = dafsa_load(PATH);
        int i, missing = 0;
        if (!d2) { fprintf(stderr, "load after initial save failed\n"); return 2; }
        for (i = 0; i < N_ORIG; i++)
            if (dafsa_lookup_n(d2, orig[i], orig_len[i]) != 1) missing++;
        printf("pre-trial: load OK, %d/%d originals present\n",
               N_ORIG - missing, N_ORIG);
        dafsa_free(d2);
    }

    /* 3. Trials: fork a child that re-saves with +100 new keys; kill it. */
    printf("running %d kill -9 trials...\n", TRIALS);
    for (trial = 0; trial < TRIALS; trial++) {
        pid_t pid = fork();
        if (pid < 0) { fprintf(stderr, "fork failed at trial %d\n", trial); return 2; }

        if (pid == 0) {
            /* CHILD: load current index, add 100 new keys, atomic save, exit. */
            dafsa *dc = dafsa_load(PATH);
            int i;
            if (!dc) _exit(0);   /* nothing to do; parent will still pass */
            for (i = 0; i < N_NEW; i++)
                dafsa_add_n(dc, newk[i], newk_len[i]);
            {
                int rc = dafsa_save(dc, PATH);
                dafsa_free(dc);
                _exit(rc == 0 ? 0 : 1);
            }
        }

        /* PARENT: kill the child after a random short delay (500-50000 us). */
        usleep(500 + (rng_next() % 49501));
        kill(pid, SIGKILL);
        {
            int status;
            waitpid(pid, &status, 0);
            /* A child that exited with code 1 failed its own save before being
             * killed; that is a harness fault, not a valid trial outcome. */
            if (WIFEXITED(status) && WEXITSTATUS(status) == 1) {
                fprintf(stderr, "child failed to save (trial %d)\n", trial);
                n_bad++;
            }
        }

        /* Validate: index must load; all originals present; new count 0 or 100. */
        {
            dafsa *dv = dafsa_load(PATH);
            int i, orig_ok = 0, newcnt = 0;
            int valid = 1;
            if (!dv) {
                fprintf(stderr, "trial %d: dafsa_load returned NULL\n", trial);
                n_bad++;
                goto reset_trial;
            }
            for (i = 0; i < N_ORIG; i++)
                if (dafsa_lookup_n(dv, orig[i], orig_len[i]) == 1) orig_ok++;
            for (i = 0; i < N_NEW; i++)
                if (dafsa_lookup_n(dv, newk[i], newk_len[i]) == 1) newcnt++;

            if (orig_ok != N_ORIG) {
                fprintf(stderr, "trial %d: lost %d originals\n",
                        trial, N_ORIG - orig_ok);
                valid = 0;
            }
            if (newcnt != 0 && newcnt != N_NEW) {
                fprintf(stderr, "trial %d: PARTIAL save — %d new keys (must be 0 or %d)\n",
                        trial, newcnt, N_NEW);
                valid = 0;
            }
            dafsa_free(dv);

            if (!valid) {
                n_bad++;
            } else if (newcnt == 0) {
                n_zero++;
            } else {
                n_hundred++;
            }
        }

    reset_trial:
        /* Reset for next trial: re-persist the original set, clear any
         * stray .tmp left by a child killed mid-write. */
        if (dafsa_save(d, PATH) != 0) {
            fprintf(stderr, "trial %d: reset save failed\n", trial);
            dafsa_free(d);
            return 2;
        }
        remove(TMPPATH);
    }

    dafsa_free(d);

    printf("trial distribution: new=0 on %d trials, new=%d on %d trials, bad=%d\n",
           n_zero, N_NEW, n_hundred, n_bad);
    if (n_bad != 0) {
        fprintf(stderr, "CRASH TEST FAILED (%d bad trials)\n", n_bad);
        remove(PATH);
        remove(TMPPATH);
        return 1;
    }
    printf("CRASH TEST PASSED (%d trials, atomic save invariant held)\n", TRIALS);

    remove(PATH);
    remove(TMPPATH);
    return 0;
}
