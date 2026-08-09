/*
 * dafsa.c -- Carrasco & Forcada (2002) Incremental Minimal Acyclic DFA
 *
 * Implements incremental addition and deletion of strings from a minimal
 * deterministic acyclic finite-state automaton (DAFSA) using the
 * clone-on-write + register + confluence algorithm described in:
 *
 *   Carrasco, R.C. & Forcada, M.L. (2002)
 *   "Incremental Construction and Maintenance of Minimal
 *    Acyclic Finite-State Automata"
 *   Computational Linguistics, 28(2), pp. 207-216.
 *
 * Refactored from dawg.c: heap-allocated/growable arrays, opaque handle,
 * length-delimited key API.
 */
#include "dafsa.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <assert.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>

/* ─── Tunables ──────────────────────────────────────────────────────────── */

#define DAFSA_MAX_STATES_HARD  100000000
#define MAX_WORD_LEN           4096
#define ALPHABET_SZ            256
#define FNV_OFFSET             14695981039346656037ULL
#define FNV_PRIME              1099511628211ULL

/* Hint to prefetch a cache line for reading. No-op on compilers without
 * __builtin_prefetch (MSVC etc.). */
#if defined(__GNUC__) || defined(__clang__)
#define DAFSA_PREFETCH(ptr) __builtin_prefetch((const void *)(ptr), 0, 1)
#else
#define DAFSA_PREFETCH(ptr) ((void)0)
#endif

/* ─── Data structures ──────────────────────────────────────────────────── */

typedef struct {
    unsigned char  sym;
    unsigned int   target;         /* state index */
} Edge;

/* Linked-list node for incoming-edges tracking.
 * Stored as a singly-linked list threaded through a flat array so we
 * don't need malloc/free per edge. */
typedef struct inode {
    unsigned int   parent;         /* which state points here */
    unsigned char  sym;            /* via which character */
    unsigned int   next;           /* index of next inode in parent list, 0=end */
} Inode;

typedef struct {
    unsigned int   id;             /* self-index, for sanity */
    unsigned int   refcount;       /* number of incoming transitions */
    unsigned char  is_final;

    /* outbound transitions -- kept sorted by sym, heap-allocated (sparse).
     * `trans` points to an array of capacity `trans_cap`, of which the first
     * `ntrans` entries are live and sorted by sym. Using a heap array instead
     * of a fixed trans[256] keeps memory proportional to actual transitions
     * (sparse), avoiding ~2 KiB per state on large corpora. */
    unsigned int   ntrans;
    Edge          *trans;
    unsigned int   trans_cap;

    /* inbound edges (for merge redirection) */
    unsigned int   in_head;        /* index of first Inode, 0=none */

    uint64_t       sig;            /* cached FNV-1a signature, 0=invalid */
} State;

struct dafsa {
    unsigned int   nstates;        /* state 0 = implicit dead/sink state, unused */
    unsigned int   initial;        /* initial state id */

    /* Heap arrays with doubling growth */
    State         *states;
    size_t         states_cap;     /* capacity (allocated count) */

    Inode         *inodes;
    size_t         inodes_cap;
    unsigned int   inodes_used;    /* index 0 = sentinel */

    /* Register: open-addressing hash table sig -> state_id */
    uint64_t      *reg_keys;
    uint32_t      *reg_vals;
    size_t         reg_cap;        /* capacity (prime) */
    size_t         reg_used;       /* count of occupied slots */
    uint64_t       reg_probes;

    /* Per-handle scratch for add/delete path traversal.
     * NOT reentrant: a single dafsa must not be mutated concurrently. */
    unsigned int  *spath;
    unsigned char *schars;
    unsigned int  *sparents;
    size_t         scratch_cap;    /* entries (all three share this cap) */

    /* Orphan-state free-list. Freed slots are chained via their `sig` field
     * (reused as a `next` pointer).  0 = empty list. */
    unsigned int   free_head;
};

/* Zero-copy search-only view: mmaps the on-disk PDWG v3 file and indexes
 * directly into the CSR — no State[]/Edge[] materialization. */
struct dafsa_view {
    uint8_t       *map;          /* mmap'd file, PROT_READ MAP_PRIVATE */
    size_t         map_len;
    uint32_t       n_states;
    uint32_t       initial;      /* == 1 */
    const uint8_t *final_bits;   /* points into map */
    const uint8_t *csr;          /* points into map: first CSR byte */
    uint64_t      *state_off;    /* n_states+2 byte offsets into csr;
                                  * state s spans [csr+off[s], csr+off[s+1])
                                  * off[0]=0; off[n_states+1]==total CSR bytes */
};

/* ─── Forward declarations ─────────────────────────────────────────────── */

static unsigned int state_new(dafsa *d);
static void         state_detach_from_children(dafsa *d, unsigned int sid);
static int          trans_find(const State *s, unsigned char c);
static int          trans_reserve(State *s, unsigned int need);
static void         trans_add(State *s, unsigned char c, unsigned int tgt);
static void         incoming_add(dafsa *d, unsigned int src, unsigned char c,
                                 unsigned int dst);
static void         incoming_redirect(dafsa *d, unsigned int old_tgt,
                                      unsigned int new_tgt);
static void         incoming_redirect_one(dafsa *d, unsigned int parent,
                                           unsigned char sym,
                                           unsigned int old_tgt,
                                           unsigned int new_tgt);
static uint64_t     sig_compute(const State *s);
static unsigned int reg_lookup(dafsa *d, uint64_t sig);
static void         reg_insert(dafsa *d, uint64_t sig, unsigned int id);
static void         reg_grow(dafsa *d);
static int          replace_or_register(dafsa *d, unsigned int sid,
                                        unsigned int parent);
static unsigned int clone_state(dafsa *d, unsigned int sid);
static int          confluence_path(dafsa *d, unsigned int *path,
                                    unsigned int *parents,
                                    unsigned int len);
static int          trans_find(const State *s, unsigned char c);

#ifdef DAFSA_DEBUG
static void dafsa_check_invariants(const dafsa *d);
#endif

/* ─── Prime helpers (for register growth) ──────────────────────────────── */

static int is_prime(size_t n)
{
    size_t d;
    if (n < 2) return 0;
    if (n % 2 == 0) return n == 2;
    for (d = 3; d * d <= n; d += 2) {
        if (n % d == 0) return 0;
    }
    return 1;
}

static size_t next_prime(size_t n)
{
    if (n < 2) return 2;
    if (n % 2 == 0) n++;
    while (!is_prime(n))
        n += 2;
    return n;
}

/* ─── Lifecycle ────────────────────────────────────────────────────────── */

dafsa *dafsa_create(void)
{
    dafsa *d;

    d = calloc(1, sizeof(*d));
    if (!d) return NULL;

    d->states_cap = 4096;
    d->states = calloc(d->states_cap, sizeof(State));
    if (!d->states) goto fail;

    d->inodes_cap = 4096;
    d->inodes = calloc(d->inodes_cap, sizeof(Inode));
    if (!d->inodes) goto fail;

    d->reg_cap = 4093;   /* prime */
    d->reg_keys = calloc(d->reg_cap, sizeof(uint64_t));
    if (!d->reg_keys) goto fail;
    d->reg_vals = calloc(d->reg_cap, sizeof(uint32_t));
    if (!d->reg_vals) goto fail;

    d->nstates = 1;            /* state 0 is "no state" sentinel */
    d->initial = state_new(d);  /* state 1 is the initial state */
    d->inodes_used = 0;
    d->reg_used = 0;
    d->reg_probes = 0;

    return d;

fail:
    dafsa_free(d);
    return NULL;
}

void dafsa_free(dafsa *d)
{
    if (!d) return;
    free(d->spath);
    free(d->schars);
    free(d->sparents);
    if (d->states) {
        unsigned int i;
        for (i = 0; i < d->nstates; i++)
            free(d->states[i].trans);
    }
    free(d->states);
    free(d->inodes);
    free(d->reg_keys);
    free(d->reg_vals);
    free(d);
}

/* ─── State management ─────────────────────────────────────────────────── */

static unsigned int state_new(dafsa *d)
{
    /* Try the free-list first — recycled slots don't count against the cap. */
    if (d->free_head != 0) {
        unsigned int id = d->free_head;
        /* sig is reused as the next pointer; narrowing from uint64_t to
         * uint32_t is safe because state ids are bounded by 100M. */
        d->free_head = (unsigned int)d->states[id].sig;
        memset(&d->states[id], 0, sizeof(State));
        d->states[id].id = id;
        return id;
    }

    /* Hard limit only fires when we must allocate a genuinely new slot.
     * Free-list exhaustion already handled above. */
    if (d->nstates >= DAFSA_MAX_STATES_HARD) {
        fprintf(stderr, "dafsa: max states exceeded (%u)\n",
                (unsigned)DAFSA_MAX_STATES_HARD);
        abort();
    }

    if (d->nstates >= d->states_cap) {
        size_t new_cap = d->states_cap * 2;
        State *new_states;

        if (new_cap > (size_t)DAFSA_MAX_STATES_HARD + 1)
            new_cap = (size_t)DAFSA_MAX_STATES_HARD + 1;

        new_states = realloc(d->states, new_cap * sizeof(State));
        if (!new_states) {
            fprintf(stderr, "dafsa: OOM growing states\n");
            abort();
        }
        /* Zero-initialize the newly allocated tail */
        memset(new_states + d->states_cap, 0,
               (new_cap - d->states_cap) * sizeof(State));
        d->states = new_states;
        d->states_cap = new_cap;
    }

    {
        unsigned int id = d->nstates++;
        State *s = &d->states[id];
        memset(s, 0, sizeof(*s));
        s->id = id;
        return id;
    }
}

/* Remove phantom inode entries from children's in_head chains.
 * Called by state_free BEFORE it frees the trans[] array.
 *
 * For each outgoing transition (sym, child) of state sid, unlinks the
 * matching inode from child's in_head chain and decrements child's
 * refcount.  Without this, children accumulate garbage inodes whose
 * parent field points at a now-reused slot — inflating refcounts and
 * risking UB if the reused slot is later merged (the assert at ~line 475
 * would fire on a stale parent). */
static void state_detach_from_children(dafsa *d, unsigned int sid)
{
    State *s = &d->states[sid];
    unsigned int j;

    for (j = 0; j < s->ntrans; j++) {
        unsigned char sym   = s->trans[j].sym;
        unsigned int  child = s->trans[j].target;
        unsigned int *prev_ptr;
        unsigned int ni;

        /* In a minimal acyclic DFA, child != sid always (no self-loops).
         * Also child could be the equivalent/new_tgt that triggered this
         * merge — that's fine; this just removes sid's phantom contribution. */
        prev_ptr = &d->states[child].in_head;
        ni = *prev_ptr;
        while (ni != 0) {
            Inode *in = &d->inodes[ni];
            if (in->parent == sid && in->sym == sym) {
                /* unlink from child's chain — at most one match per (child,sym) */
                *prev_ptr = in->next;
                d->states[child].refcount--;
                break;
            }
            prev_ptr = &in->next;
            ni = *prev_ptr;
        }
    }
}

/* Release an orphan state slot to the free-list.  Only call when:
 *   refcount == 0 && id != d->initial
 * Detaches phantom inodes from children, frees the transition array,
 * and chains the slot for reuse. */
static void state_free(dafsa *d, unsigned int id)
{
    State *s = &d->states[id];

    /* Detach phantom inodes from children BEFORE freeing trans[].
     * state_detach_from_children walks s->trans[], so trans must
     * still be intact here. */
    state_detach_from_children(d, id);

    free(s->trans);
    s->trans    = NULL;
    s->ntrans   = 0;
    s->refcount = 0;
    s->is_final = 0;
    s->in_head  = 0;
    /* Chain into free-list via the `sig` field.
     * State ids fit in 32 bits (bounded by DAFSA_MAX_STATES_HARD=100M),
     * so narrowing the uint64_t sig to uint32_t for the next pointer is safe. */
    s->sig       = d->free_head;
    d->free_head = id;
}

/* ─── Inode allocation ─────────────────────────────────────────────────── */

static Inode *inode_alloc(dafsa *d)
{
    if (d->inodes_used + 1 >= d->inodes_cap) {
        size_t new_cap = d->inodes_cap * 2;
        Inode *new_inodes = realloc(d->inodes, new_cap * sizeof(Inode));
        if (!new_inodes) {
            fprintf(stderr, "dafsa: OOM growing inodes\n");
            abort();
        }
        memset(new_inodes + d->inodes_cap, 0,
               (new_cap - d->inodes_cap) * sizeof(Inode));
        d->inodes = new_inodes;
        d->inodes_cap = new_cap;
    }
    return &d->inodes[++d->inodes_used];   /* index 0 = sentinel */
}

/* ─── Scratch arena for add/delete path traversal ──────────────────────── */

/* Ensure the per-handle scratch arrays can hold `len+2` entries.
 * Returns 0 on success, -1 on OOM.  Not reentrant on the same handle. */
static int dafsa_ensure_scratch(dafsa *d, size_t len)
{
    size_t need = len + 2;

    if (d->scratch_cap >= need) return 0;

    {
        unsigned int  *new_path;
        unsigned char *new_chars;
        unsigned int  *new_parents;

        /* Use malloc (not realloc) so that the old pointers remain valid
         * until all three allocations have succeeded.  Then swap. */
        new_path    = (unsigned int *)malloc(need * sizeof(unsigned int));
        new_chars   = (unsigned char *)malloc(need * sizeof(unsigned char));
        new_parents = (unsigned int *)malloc(need * sizeof(unsigned int));
        if (!new_path || !new_chars || !new_parents) {
            free(new_path);
            free(new_chars);
            free(new_parents);
            return -1;
        }
        /* Copy old contents (if any) */
        if (d->spath) {
            size_t old_n = d->scratch_cap;
            memcpy(new_path,    d->spath,    old_n * sizeof(unsigned int));
            memcpy(new_chars,   d->schars,   old_n * sizeof(unsigned char));
            memcpy(new_parents, d->sparents, old_n * sizeof(unsigned int));
        }
        free(d->spath);
        free(d->schars);
        free(d->sparents);
        d->spath    = new_path;
        d->schars   = new_chars;
        d->sparents = new_parents;
        d->scratch_cap = need;
    }
    return 0;
}

/* ─── Transition helpers ───────────────────────────────────────────────── */

/* binary search for transition `c`. Returns index or -1. */
static int trans_find(const State *s, unsigned char c)
{
    unsigned int n = s->ntrans;
    /* Fast path: most DAFSA states have few transitions; a linear scan (with
     * early exit on the sorted-array invariant) beats binary search there. */
    if (n <= 8) {
        unsigned int i;
        for (i = 0; i < n; i++) {
            unsigned char sy = s->trans[i].sym;
            if (sy == c) return (int)i;
            if (sy >  c) return -1;   /* sorted: no later entry can match */
        }
        return -1;
    }
    {
        int lo = 0, hi = (int)n - 1;
        while (lo <= hi) {
            int mid = (lo + hi) / 2;
            if (s->trans[mid].sym == c) return mid;
            if (s->trans[mid].sym <  c) lo = mid + 1;
            else                        hi = mid - 1;
        }
        return -1;
    }
}

/* Ensure the state's transition array has capacity for `need` entries.
 * Grows geometrically (doubling). Returns 0 on success, -1 on OOM. */
static int trans_reserve(State *s, unsigned int need)
{
    unsigned int new_cap;
    Edge *new_trans;

    if (need <= s->trans_cap) return 0;

    new_cap = s->trans_cap ? s->trans_cap : 4;
    while (new_cap < need) new_cap *= 2;
    if (new_cap > ALPHABET_SZ) new_cap = ALPHABET_SZ;
    /* At this point new_cap >= need: `need` is bounded by ALPHABET_SZ (the
     * binary-search invariant guarantees ntrans <= ALPHABET_SZ), and the
     * doubling loop above guarantees new_cap >= need unless capped at
     * ALPHABET_SZ — in which case need also equals ALPHABET_SZ. No dead
     * re-clamp needed. */

    new_trans = (Edge *)realloc(s->trans, new_cap * sizeof(Edge));
    if (!new_trans) return -1;
    s->trans = new_trans;
    s->trans_cap = new_cap;
    return 0;
}

/* insert a transition, maintaining sorted order */
static void trans_add(State *s, unsigned char c, unsigned int tgt)
{
    assert(s->ntrans < ALPHABET_SZ);
    /* Grow on demand (sparse heap array). On OOM, abort like state_new does —
     * the DAFSA cannot continue without the transition table. */
    if (trans_reserve(s, s->ntrans + 1) != 0) {
        fprintf(stderr, "dafsa: OOM growing transitions\n");
        abort();
    }
    {
        int pos = 0;
        while (pos < (int)s->ntrans && s->trans[pos].sym < c)
            pos++;
        if (pos < (int)s->ntrans && s->trans[pos].sym == c) {
            /* update existing */
            s->trans[pos].target = tgt;
            return;
        }
        /* shift right */
        memmove(&s->trans[pos + 1], &s->trans[pos],
                (s->ntrans - (unsigned)pos) * sizeof(Edge));
        s->trans[pos].sym = c;
        s->trans[pos].target = tgt;
        s->ntrans++;
    }
}

/* ─── Incoming-edge tracking ───────────────────────────────────────────── */

static void incoming_add(dafsa *d, unsigned int src, unsigned char c,
                          unsigned int dst)
{
    Inode *in = inode_alloc(d);   /* may realloc d->inodes */
    in->parent = src;
    in->sym = c;
    in->next  = d->states[dst].in_head;   /* re-fetched by index, safe */
    d->states[dst].in_head = d->inodes_used;
    d->states[dst].refcount++;
}

/* Redirect ALL incoming edges that point to old_tgt to point to new_tgt.
 * Also fix up the parents' transition tables.
 *
 * IMPORTANT: This function holds State* and Inode* across its loop body
 * but no realloc-triggering call is made inside the loop, so they remain
 * valid. Callers must re-fetch any State* / Inode* they hold across the
 * call to this function (which they do -- callers use indices). */
static void incoming_redirect(dafsa *d, unsigned int old_tgt,
                               unsigned int new_tgt)
{
    State *old_s = &d->states[old_tgt];  /* fetched once; no realloc in body */
    State *new_s = &d->states[new_tgt];
    unsigned int ni = old_s->in_head;

    while (ni != 0) {
        Inode *in = &d->inodes[ni];
        State  *parent = &d->states[in->parent];
        int pos;

        /* update parent's transition */
        pos = trans_find(parent, in->sym);
        assert(pos >= 0);
        parent->trans[pos].target = new_tgt;
        parent->sig = 0;  /* invalidate, will recompute */

        /* update refcounts */
        old_s->refcount--;
        new_s->refcount++;

        /* move the inode to new_tgt's list */
        {
            unsigned int next = in->next;
            in->next = new_s->in_head;
            new_s->in_head = ni;
            ni = next;
        }
    }
    old_s->in_head = 0;

    /* If this state is now an orphan, free it for reuse */
    if (old_s->refcount == 0 && old_tgt != d->initial)
        state_free(d, old_tgt);
}

/* Redirect a single incoming edge: parent's transition via sym from
 * old_tgt to new_tgt. Updates the transition table, inode list, and
 * refcounts. Used during clone-on-write.
 *
 * Pointer safety: accesses d->states[] and d->inodes[] by index/re-fetch
 * each iteration. No realloc-triggering call in the loop body. */
static void incoming_redirect_one(dafsa *d, unsigned int parent,
                                   unsigned char sym,
                                   unsigned int old_tgt,
                                   unsigned int new_tgt)
{
    int pos;

    /* update parent's transition */
    pos = trans_find(&d->states[parent], sym);
    assert(pos >= 0 && d->states[parent].trans[pos].target == old_tgt);
    d->states[parent].trans[pos].target = new_tgt;
    d->states[parent].sig = 0;

    /* update refcounts */
    d->states[old_tgt].refcount--;
    d->states[new_tgt].refcount++;

    /* move the inode from old_tgt's list to new_tgt's list */
    {
        unsigned int *prev_ptr = &d->states[old_tgt].in_head;
        unsigned int ni = *prev_ptr;

        while (ni != 0) {
            /* Re-fetch inode each iteration (safe -- no realloc in loop) */
            Inode *in = &d->inodes[ni];
            if (in->parent == parent && in->sym == sym) {
                /* unlink from old_tgt */
                *prev_ptr = in->next;
                /* link into new_tgt */
                in->next = d->states[new_tgt].in_head;
                d->states[new_tgt].in_head = ni;

                /* If old_tgt is now an orphan, free it for reuse */
                if (d->states[old_tgt].refcount == 0 && old_tgt != d->initial)
                    state_free(d, old_tgt);

                return;
            }
            prev_ptr = &in->next;
            ni = *prev_ptr;
        }
    }
    /* Not found -- shouldn't happen if bookkeeping is correct */
    assert(0 && "incoming_redirect_one: inode not found");
}

/* ─── Signature computation (FNV-1a) ───────────────────────────────────── */

/* IMPORTANT (2026-08-09): the target-hashing formula changed from a
 * 4-byte little-endian loop to a single 32-bit xor + multiply.
 * Signatures computed before this change are incompatible — any
 * in-flight mutable handle must be reloaded from disk before
 * mutation (the nightly build provides this escape hatch). */
static uint64_t sig_compute(const State *s)
{
    uint64_t h = FNV_OFFSET;

    /* hash the final flag */
    h ^= s->is_final ? 1 : 0;
    h *= FNV_PRIME;

    /* hash each transition: sym + 32-bit target in one step */
    {
        unsigned int i;
        for (i = 0; i < s->ntrans; i++) {
            h ^= s->trans[i].sym;
            h *= FNV_PRIME;
            h ^= s->trans[i].target;    /* xor the full 32-bit target once */
            h *= FNV_PRIME;
        }
    }
    return h;
}

/* ─── Register (equivalence-class map) ────────────────────────────────── */

static unsigned int reg_lookup(dafsa *d, uint64_t sig)
{
    size_t idx;

    if (sig == 0) return 0;  /* 0 = invalid/empty */

    idx = (size_t)(sig % d->reg_cap);
    while (d->reg_keys[idx] != 0) {
        if (d->reg_keys[idx] == sig)
            return d->reg_vals[idx];
        idx = (idx + 1) % d->reg_cap;
        d->reg_probes++;
    }
    return 0;  /* not found */
}

/* Non-counting variant for invariant-checker / read-only paths that
 * must not perturb reg_probes.  Identical to reg_lookup except it
 * does not increment d->reg_probes.  Only used inside DAFSA_DEBUG. */
#ifdef DAFSA_DEBUG
static unsigned int reg_lookup_no_count(const dafsa *d, uint64_t sig)
{
    size_t idx;

    if (sig == 0) return 0;

    idx = (size_t)(sig % d->reg_cap);
    while (d->reg_keys[idx] != 0) {
        if (d->reg_keys[idx] == sig)
            return d->reg_vals[idx];
        idx = (idx + 1) % d->reg_cap;
    }
    return 0;
}
#endif

/* Grow the register: double capacity -> next prime, rehash all entries. */
static void reg_grow(dafsa *d)
{
    size_t new_cap = next_prime(d->reg_cap * 2);
    uint64_t *new_keys;
    uint32_t *new_vals;
    size_t i;

    new_keys = calloc(new_cap, sizeof(uint64_t));
    new_vals = calloc(new_cap, sizeof(uint32_t));
    if (!new_keys || !new_vals) {
        free(new_keys);
        free(new_vals);
        fprintf(stderr, "dafsa: OOM growing register\n");
        abort();
    }

    /* Rehash all existing entries */
    for (i = 0; i < d->reg_cap; i++) {
        uint64_t sig = d->reg_keys[i];
        if (sig == 0) continue;
        {
            size_t idx = (size_t)(sig % new_cap);
            while (new_keys[idx] != 0)
                idx = (idx + 1) % new_cap;
            new_keys[idx] = sig;
            new_vals[idx] = d->reg_vals[i];
        }
    }

    free(d->reg_keys);
    free(d->reg_vals);
    d->reg_keys = new_keys;
    d->reg_vals = new_vals;
    d->reg_cap   = new_cap;
    /* reg_used is unchanged (same number of entries) */
}

static void reg_insert(dafsa *d, uint64_t sig, unsigned int id)
{
    size_t idx;

    if (sig == 0) return;

    /* Grow if load factor would exceed 0.7 after insert */
    if ((d->reg_used + 1) * 10 > d->reg_cap * 7)
        reg_grow(d);

    idx = (size_t)(sig % d->reg_cap);
    while (d->reg_keys[idx] != 0) {
        /* overwrite if re-inserting the same state */
        if (d->reg_keys[idx] == sig) {
            d->reg_vals[idx] = id;
            return;
        }
        idx = (idx + 1) % d->reg_cap;
        d->reg_probes++;
    }
    d->reg_keys[idx] = sig;
    d->reg_vals[idx] = id;
    d->reg_used++;
}

/* ─── Clone-on-write ──────────────────────────────────────────────────── */

/* Clone state `sid`, return the clone's id. The clone gets refcount=0
 * initially, and all its outgoing transitions add incoming edges to their
 * targets (via incoming_add). The caller is responsible for redirecting
 * the appropriate single incoming edge from the parent.
 *
 * Pointer safety: state_new() may realloc d->states, so we fetch src/dst
 * AFTER state_new. incoming_add() may realloc d->inodes, but dst/src point
 * into d->states (separate allocation), so they remain valid. */
static unsigned int clone_state(dafsa *d, unsigned int sid)
{
    unsigned int new_id = state_new(d);   /* MAY REALLOC d->states */

    /* Re-fetch AFTER state_new: the old &d->states[sid] would be stale */
    State *src = &d->states[sid];
    State *dst = &d->states[new_id];

    dst->is_final = src->is_final;
    dst->ntrans = src->ntrans;
    if (src->ntrans > 0) {
        /* Allocate the clone's transition array (sparse). On OOM, abort. */
        if (trans_reserve(dst, src->ntrans) != 0) {
            fprintf(stderr, "dafsa: OOM cloning transitions\n");
            abort();
        }
        memcpy(dst->trans, src->trans, src->ntrans * sizeof(Edge));
    }
    dst->sig = src->sig;

    /* Register incoming edges for all of the clone's outgoing transitions.
     * incoming_add may realloc d->inodes, but dst is in d->states (safe). */
    {
        unsigned int i;
        for (i = 0; i < dst->ntrans; i++) {
            incoming_add(d, new_id, dst->trans[i].sym, dst->trans[i].target);
        }
    }

    return new_id;
}

/* ─── Core: replace_or_register ────────────────────────────────────────── */

/* After modifying state `sid`, call this to either register it (if its
 * signature is unique) or merge it with an existing equivalent state.
 *
 * `parent` is the state that transitions to `sid`.
 * After a merge, parent's signature is invalidated; confluence_path
 * will re-register it.
 *
 * Returns 1 if a merge occurred (the caller may need to rebuild the register),
 * 0 otherwise.
 *
 * Pointer safety: s = &d->states[sid] is fetched once. reg_lookup and
 * reg_insert may realloc reg_keys/reg_vals (separate allocation), so s
 * remains valid. incoming_redirect also does not realloc states/inodes. */
static int replace_or_register(dafsa *d, unsigned int sid,
                               unsigned int parent)
{
    State *s = &d->states[sid];  /* fetched once; states not realloc'd here */
    uint64_t new_sig;
    unsigned int equivalent;

    /* Compute (or recompute) signature */
    new_sig = sig_compute(s);
    s->sig = new_sig;

    equivalent = reg_lookup(d, new_sig);
    /* A register entry is only a valid merge target if the state it names is
     * still live (refcount > 0, or the initial state) AND still carries that
     * exact signature. Otherwise the entry is stale (the state was merged away
     * or its structure changed) and must be ignored — this is what makes
     * incremental register maintenance correct without a full reg_rebuild on
     * every operation. */
    if (equivalent != 0 && equivalent != sid &&
        (d->states[equivalent].refcount != 0 || equivalent == d->initial) &&
        d->states[equivalent].sig == new_sig) {
        /* --- MERGE: sid into equivalent --- */
        incoming_redirect(d, sid, equivalent);

        /* Repoint the register entry: this signature now belongs to the
         * surviving state `equivalent`, not the merged-away `sid`.  Leaving
         * the stale sid entry would make a later state with the same signature
         * merge into a dead id. */
        reg_insert(d, new_sig, equivalent);

        /* parent's transition was updated by incoming_redirect.
         * Now parent's signature is dirty. */
        d->states[parent].sig = 0;

        /* sid was freed by incoming_redirect — nothing more to do here */
        return 1;
    } else {
        /* --- REGISTER: this signature is unique (or its entry was stale) --- */
        reg_insert(d, new_sig, sid);
        return 0;
    }
}

/* ─── Confluence along the path ────────────────────────────────────────── */

/* After adding/deleting a word, the path from root to the final state
 * may contain states that need to be re-registered. Process bottom-up.
 *
 * path[i]     = state id
 * parents[i]  = path[i-1]  (parents[0] is unused)
 * len         = number of states on the path
 *
 * Returns 1 if any state on the path was merged (caller may need to rebuild
 * the register), 0 otherwise.
 *
 * Pointer safety: operates entirely on indices in the path[]/parents[]
 * stack arrays. replace_or_register re-fetches State * internally. */
static int confluence_path(dafsa *d, unsigned int *path,
                           unsigned int *parents,
                           unsigned int len)
{
    int i;
    int merged = 0;
    for (i = (int)len - 1; i >= 1; i--) {
        unsigned int child  = path[i];
        unsigned int parent = parents[i];
        if (replace_or_register(d, child, parent)) merged = 1;
    }
    /* Also register the root if it was modified */
    if (replace_or_register(d, path[0], 0)) merged = 1;
    return merged;
}

/* ─── Add word (length-delimited) ──────────────────────────────────────── */

int dafsa_add_n(dafsa *d, const unsigned char *key, size_t len)
{
    unsigned int path_len;
    unsigned int current;
    unsigned int pos;

    if (len == 0) {
        /* Empty string: the initial state becomes final */
        if (d->states[d->initial].is_final) {
#ifdef DAFSA_DEBUG
            dafsa_check_invariants(d);
#endif
            return 0;
        }
        d->states[d->initial].is_final = 1;
        replace_or_register(d, d->initial, 0);
#ifdef DAFSA_DEBUG
        dafsa_check_invariants(d);
#endif
        return 1;
    }
    if (key == NULL) return -1;   /* defensive: non-empty key must be non-NULL */
    if (len > MAX_WORD_LEN) return -1;   /* hard guard: path arrays are bounded */

    if (dafsa_ensure_scratch(d, len) != 0) return -1;

    /* --- Phase 1: Traverse existing path --- */
    current  = d->initial;
    path_len = 0;

    d->spath[path_len]    = current;
    d->schars[path_len]   = 0;
    d->sparents[path_len] = 0;
    path_len++;

    for (pos = 0; pos < len; pos++) {
        unsigned char c = key[pos];
        int tr = trans_find(&d->states[current], c);
        if (tr < 0) break;  /* divergence point */

        {
            unsigned int next = d->states[current].trans[tr].target;
            /* Prefetch the next iteration's state (and its trans[] array) so
             * the two dependent random loads overlap the memory latency of the
             * current iteration. Safe: read-only hint; bounds checked. */
            if (pos + 1 < len) {
                DAFSA_PREFETCH(&d->states[next]);
                if (d->states[next].trans)
                    DAFSA_PREFETCH(d->states[next].trans);
            }
            current = next;
            d->spath[path_len]    = current;
            d->schars[path_len]   = c;
            d->sparents[path_len] = d->spath[path_len - 1];
            path_len++;
        }
    }

    /* --- Check: word already present? --- */
    if (pos == len && d->states[current].is_final) {
#ifdef DAFSA_DEBUG
        dafsa_check_invariants(d);
#endif
        return 0;  /* already in the DAFSA */
    }

    /* --- Clone-on-write: make the prefix path private (ascending) ---
     * Clone every shared state along the path from root toward the leaf.
     * Each redirect targets the already-private parent path[di-1], so it
     * never affects other words that share a sub-automaton.  This is required
     * when re-adding a word whose (deleted) ghost branch is still shared with
     * another word, as well as when adding a fresh suffix at a divergence. */
    {
        unsigned int di;
        for (di = 1; di < path_len; di++) {
            unsigned int sid = d->spath[di];
            if (d->states[sid].refcount > 1) {
                unsigned int clone = clone_state(d, sid);
                unsigned int parent = d->spath[di - 1];
                unsigned char pc    = d->schars[di];

                /* clone_state may realloc states; re-fetch via indices */
                incoming_redirect_one(d, parent, pc, sid, clone);

                /* Update path (and the parent pointer of the next element) */
                d->spath[di] = clone;
                if (di + 1 < path_len)
                    d->sparents[di + 1] = clone;
            }
        }
        current = d->spath[path_len - 1];
    }

    /* --- Phase 2: Add suffix from the divergence point --- */
    if (pos < len) {
        unsigned int i;
        for (i = pos; i < len; i++) {
            unsigned char c = key[i];
            unsigned int next;

            next = state_new(d);   /* MAY REALLOC states */
            /* re-fetch state via index -- current is an index, safe */
            trans_add(&d->states[current], c, next);
            d->states[current].sig = 0;  /* dirty */

            incoming_add(d, current, c, next);  /* MAY REALLOC inodes */

            d->spath[path_len]    = next;
            d->schars[path_len]   = c;
            d->sparents[path_len] = current;
            path_len++;

            current = next;
        }
    }

    /* --- Phase 3: Mark final and confluence --- */
    d->states[current].is_final = 1;
    d->states[current].sig = 0;  /* dirty */

    /* Stale register entries are validated at lookup in replace_or_register
     * (live state + matching signature), so no full register rebuild is needed
     * here — rebuilding on every add is O(nstates) and makes bulk builds
     * O(N^2). */
    confluence_path(d, d->spath, d->sparents, path_len);

#ifdef DAFSA_DEBUG
    dafsa_check_invariants(d);
#endif
    return 1;
}

/* ─── Delete word (length-delimited) ───────────────────────────────────── */

int dafsa_delete_n(dafsa *d, const unsigned char *key, size_t len)
{
    unsigned int path_len;
    unsigned int current;
    unsigned int i;
    int di;

    if (len == 0) {
        if (!d->states[d->initial].is_final) {
#ifdef DAFSA_DEBUG
            dafsa_check_invariants(d);
#endif
            return 0;
        }
        d->states[d->initial].is_final = 0;
        d->states[d->initial].sig = 0;
        replace_or_register(d, d->initial, 0);
#ifdef DAFSA_DEBUG
        dafsa_check_invariants(d);
#endif
        return 1;
    }
    if (key == NULL) return -1;   /* defensive: non-empty key must be non-NULL */
    if (len > MAX_WORD_LEN) return -1;   /* hard guard: path arrays are bounded */

    if (dafsa_ensure_scratch(d, len) != 0) return -1;

    /* --- Phase 1: Traverse to the final state --- */
    current  = d->initial;
    path_len = 0;

    d->spath[path_len]    = current;
    d->schars[path_len]   = 0;
    d->sparents[path_len] = 0;
    path_len++;

    for (i = 0; i < len; i++) {
        unsigned char c = key[i];
        int tr = trans_find(&d->states[current], c);
        if (tr < 0) {
#ifdef DAFSA_DEBUG
            dafsa_check_invariants(d);
#endif
            return 0;  /* not present */
        }
        {
            unsigned int next = d->states[current].trans[tr].target;
            /* Prefetch the next iteration's state + trans[] (see add_n). */
            if ((unsigned)(i + 1) < len) {
                DAFSA_PREFETCH(&d->states[next]);
                if (d->states[next].trans)
                    DAFSA_PREFETCH(d->states[next].trans);
            }
            current = next;
        }
        d->spath[path_len]    = current;
        d->schars[path_len]   = c;
        d->sparents[path_len] = d->spath[path_len - 1];
        path_len++;
    }

    if (!d->states[current].is_final) {
#ifdef DAFSA_DEBUG
        dafsa_check_invariants(d);
#endif
        return 0;  /* word is a prefix but not a word */
    }

    /* --- Phase 2: Clone-on-write, bottom-up ---
     * Walk the path from the root toward the leaf (ascending).  At each step
     * the parent (path[di-1]) is already private -- either it was cloned in
     * the previous iteration (refcount == 1) or it had refcount == 1 to begin
     * with -- so redirecting its single edge on the path cannot affect any
     * other word.  (A descending walk would redirect the edge of a still-shared
     * parent and corrupt words that share the sub-automaton.) */
    {
        for (di = 1; di < (int)path_len; di++) {
            unsigned int sid = d->spath[di];
            if (d->states[sid].refcount > 1) {
                unsigned int clone = clone_state(d, sid);
                /* parent is the (possibly just-cloned) previous path state;
                 * must re-read path[di-1], NOT the stale parents[] snapshot */
                unsigned int parent = d->spath[di - 1];
                unsigned char pc    = d->schars[di];

                /* clone_state may realloc states; re-fetch via indices */
                incoming_redirect_one(d, parent, pc, sid, clone);

                /* Update path -- and current if this is the final state */
                d->spath[di] = clone;
                if (di < (int)path_len - 1)
                    d->sparents[di + 1] = clone;
                if (di == (int)path_len - 1)
                    current = clone;
            }
        }
    }

    /* --- Phase 3: Unmark final and confluence --- */
    d->states[current].is_final = 0;
    d->states[current].sig = 0;

    /* Stale register entries (from merged-away/dead states) are validated at
     * lookup in replace_or_register, so no full register rebuild is needed. */
    confluence_path(d, d->spath, d->sparents, path_len);

#ifdef DAFSA_DEBUG
    dafsa_check_invariants(d);
#endif
    return 1;
}

/* ─── Lookup (length-delimited) ────────────────────────────────────────── */

int dafsa_lookup_n(const dafsa *d, const unsigned char *key, size_t len)
{
    unsigned int current = d->initial;
    size_t i;

    if (key == NULL && len > 0) return 0;  /* defensive */

    for (i = 0; i < len; i++) {
        int tr = trans_find(&d->states[current], key[i]);
        if (tr < 0) return 0;
        {
            unsigned int next = d->states[current].trans[tr].target;
            /* Prefetch the next iteration's state + trans[] (see add_n). */
            if (i + 1 < len) {
                DAFSA_PREFETCH(&d->states[next]);
                if (d->states[next].trans)
                    DAFSA_PREFETCH(d->states[next].trans);
            }
            current = next;
        }
    }
    return d->states[current].is_final;
}

/* ─── NUL-terminated convenience wrappers ──────────────────────────────── */

int dafsa_add(dafsa *d, const unsigned char *word)
{
    return dafsa_add_n(d, word, strlen((const char *)word));
}

int dafsa_lookup(const dafsa *d, const unsigned char *word)
{
    return dafsa_lookup_n(d, word, strlen((const char *)word));
}

int dafsa_delete(dafsa *d, const unsigned char *word)
{
    return dafsa_delete_n(d, word, strlen((const char *)word));
}

/* ─── Persistence ─────────────────────────────────────────────────────── */

/* On-disk format (ROADMAP 1.3): all integers little-endian, explicit byte
 * writes (State/Edge have padding; never fwrite raw structs).
 *
 * Version 3 (2026-08-09): widens state-table ntrans from u8 to u16 LE,
 * fixing truncation for states with exactly 256 out-edges.
 *
 * Version 2 (compressed; 2026-08-08): drops the per-state offset column and
 * varint-encodes CSR target ids, shrinking large indexes ~40-58% vs v1.
 * Search semantics are unchanged. v1 is no longer read.
 *
 *   HEADER:  magic[4]="PDWG"; u32 version=3; u32 n_states; u32 n_trans;
 *            u32 initial_id=1; u32 n_final; u32 reserved=0
 *   STATE TABLE: (n_states+1) x u16 LE ntrans (0..65535; entry 0 = 0).
 *            Transition offsets are implied (cumulative), not stored.
 *   FINAL BITMAP: ceil((n_states+1)/8) bytes; bit i set iff reachable state i is final
 *   CSR TRANSITIONS: n_trans x (u8 sym; LEB128 u32 target_id), grouped by state
 *            in state-table order, sorted by sym asc.  Sink 0 -> 0, else new id.
 */

static int put_u8(FILE *f, uint8_t v);

static int put_uvarint(FILE *f, uint32_t v)
{
    /* LEB128 */
    do {
        uint8_t byte = (uint8_t)(v & 0x7F);
        v >>= 7;
        if (v != 0) byte |= 0x80;
        if (put_u8(f, byte)) return -1;
    } while (v != 0);
    return 0;
}

static int put_u8(FILE *f, uint8_t v)
{
    return fputc(v, f) == EOF ? -1 : 0;
}

static int put_u16_le(FILE *f, uint16_t v)
{
    if (put_u8(f, (uint8_t)(v & 0xFF)) != 0) return -1;
    if (put_u8(f, (uint8_t)((v >> 8) & 0xFF)) != 0) return -1;
    return 0;
}

static int put_u32_le(FILE *f, uint32_t v)
{
    int i;
    for (i = 0; i < 4; i++) {
        if (put_u8(f, (uint8_t)(v & 0xFF)) != 0) return -1;
        v >>= 8;
    }
    return 0;
}

/* Open the directory containing `path` and fsync it, so a prior rename of a
 * file into it is made durable. Returns 0 on success, -1 on error. */
static int fsync_dir_of(const char *path)
{
    char *dir = NULL;
    const char *slash;
    int fd, ret = -1;

    if (!path || !*path) return -1;
    /* dirname(path) without modifying path: everything up to the last '/'. */
    slash = strrchr(path, '/');
    if (slash == NULL) {
        dir = (char *)malloc(2);
        if (!dir) return -1;
        dir[0] = '.'; dir[1] = '\0';
    } else if (slash == path) {
        dir = (char *)malloc(2);
        if (!dir) return -1;
        dir[0] = '/'; dir[1] = '\0';
    } else {
        size_t n = (size_t)(slash - path);
        dir = (char *)malloc(n + 1);
        if (!dir) return -1;
        memcpy(dir, path, n);
        dir[n] = '\0';
    }

    fd = open(dir, O_RDONLY | O_DIRECTORY);
    if (fd >= 0) {
        ret = fsync(fd);
        close(fd);
    }
    free(dir);
    return ret;
}

/* Save a compact, minimal form: BFS-renumber reachable states 1..N (initial
 * -> 1), drop orphans (refcount 0 / unreachable).  Atomic: write path.tmp,
 * fflush, fsync, fclose, rename.  Returns 0 on success, -1 on any error.
 * `d` is const and is never mutated. */
int dafsa_save(const dafsa *d, const char *path)
{
    FILE *f = NULL;
    char *tmp_path = NULL;
    uint32_t *old_to_new = NULL;
    uint32_t *queue = NULL;
    unsigned char *visited = NULL;
    uint32_t n_reach = 0, n_trans = 0, n_final = 0;
    uint32_t head, tail, i, j;
    size_t path_len;
    int ok = -1;

    if (!d || !path) return -1;

    old_to_new = (uint32_t *)calloc(d->nstates, sizeof(uint32_t));
    queue      = (uint32_t *)malloc(d->nstates * sizeof(uint32_t));
    visited    = (unsigned char *)calloc(d->nstates, 1);
    if (!old_to_new || !queue || !visited) goto out;

    /* BFS from initial, renumber reachable states in BFS order 1..N */
    head = 0; tail = 0;
    queue[tail++] = d->initial;
    visited[d->initial] = 1;
    while (head < tail) {
        uint32_t old = queue[head++];
        const State *s = &d->states[old];
        old_to_new[old] = ++n_reach;
        if (s->is_final) n_final++;
        n_trans += s->ntrans;
        for (j = 0; j < s->ntrans; j++) {
            uint32_t tgt = s->trans[j].target;
            if (!visited[tgt]) {
                visited[tgt] = 1;
                queue[tail++] = tgt;
            }
        }
    }

    /* atomic: write to path.tmp then rename onto path */
    path_len = strlen(path);
    tmp_path = (char *)malloc(path_len + 5);
    if (!tmp_path) goto out;
    snprintf(tmp_path, path_len + 5, "%s.tmp", path);

    f = fopen(tmp_path, "wb");
    if (!f) goto out;
    /* Large buffered writes: saves ~15M fputc syscalls on a multi-megastate
     * index (default stdio buffer is only 4-8 KB). */
    if (setvbuf(f, NULL, _IOFBF, 1u << 20) != 0) goto fail;

    /* header */
    if (put_u8(f, 'P') || put_u8(f, 'D') || put_u8(f, 'W') || put_u8(f, 'G'))
        goto fail;
    if (put_u32_le(f, 3)) goto fail;            /* version */
    if (put_u32_le(f, n_reach)) goto fail;      /* n_states */
    if (put_u32_le(f, n_trans)) goto fail;      /* n_trans */
    if (put_u32_le(f, 1)) goto fail;            /* initial_id */
    if (put_u32_le(f, n_final)) goto fail;      /* n_final */
    if (put_u32_le(f, 0)) goto fail;            /* reserved */

    /* state table: (n_states+1) x u16 LE ntrans (entry 0 = 0). Offsets implied. */
    if (put_u16_le(f, 0)) goto fail;
    for (i = 1; i <= n_reach; i++) {
        const State *s = &d->states[queue[i - 1]];
        if (put_u16_le(f, (uint16_t)s->ntrans)) goto fail;
    }

    /* final bitmap: ceil((n_states+1)/8) bytes; bit 0 always 0 */
    {
        uint32_t nb = (n_reach + 8) / 8;
        for (i = 0; i < nb; i++) {
            uint8_t byte = 0;
            for (j = 0; j < 8; j++) {
                uint32_t idx = i * 8 + j;
                if (idx >= 1 && idx <= n_reach &&
                    d->states[queue[idx - 1]].is_final)
                    byte |= (uint8_t)(1u << j);
            }
            if (put_u8(f, byte)) goto fail;
        }
    }

    /* CSR: transitions grouped by state in state-table order (sym asc) */
    for (i = 1; i <= n_reach; i++) {
        const State *s = &d->states[queue[i - 1]];
        for (j = 0; j < s->ntrans; j++) {
            if (put_u8(f, s->trans[j].sym)) goto fail;
            if (put_uvarint(f, old_to_new[s->trans[j].target])) goto fail;
        }
    }

    if (ferror(f)) goto fail;

    /* atomic commit */
    if (fflush(f) != 0) goto fail;
    if (fsync(fileno(f)) != 0) goto fail;
    if (fclose(f) != 0) { f = NULL; goto fail; }
    f = NULL;
    if (rename(tmp_path, path) != 0) goto fail;
    /* fsync the containing directory so the rename itself is durable; a crash
     * after rename but before this point can otherwise lose the rename even
     * though the file data was fsync'd. */
    if (fsync_dir_of(path) != 0) goto fail;

    ok = 0;
    goto out;

fail:
    if (f) fclose(f);
    if (tmp_path) remove(tmp_path);
    ok = -1;

out:
    free(tmp_path);
    free(old_to_new);
    free(queue);
    free(visited);
    return ok;
}

/* Memory-buffer parse helpers for dafsa_load: parse from an in-memory buffer
 * via a cursor instead of per-byte fgetc, which is dramatically faster on
 * large indexes (millions of transitions).  All return -1 on EOF/overflow. */
static int mb_u8(const uint8_t **p, const uint8_t *end, uint8_t *out)
{
    if (*p >= end) return -1;
    *out = *(*p)++;
    return 0;
}
static int mb_u16(const uint8_t **p, const uint8_t *end, uint16_t *out)
{
    uint8_t lo, hi;
    if (mb_u8(p, end, &lo) || mb_u8(p, end, &hi)) return -1;
    *out = (uint16_t)(lo | ((uint16_t)hi << 8));
    return 0;
}
static int mb_u32(const uint8_t **p, const uint8_t *end, uint32_t *out)
{
    uint32_t v = 0;
    int i;
    if (*p + 4 > end) return -1;
    for (i = 0; i < 4; i++)
        v |= ((uint32_t)(*p)[i]) << (8 * i);
    *p += 4;
    *out = v;
    return 0;
}
static int mb_uvarint(const uint8_t **p, const uint8_t *end, uint32_t *out)
{
    uint32_t v = 0;
    unsigned int shift = 0;
    uint8_t byte;
    for (;;) {
        if (*p >= end) return -1;
        byte = *(*p)++;
        v |= ((uint32_t)(byte & 0x7F)) << shift;
        if (!(byte & 0x80)) break;
        shift += 7;
        if (shift > 28) return -1;   /* overflow / malformed */
    }
    *out = v;
    return 0;
}

static int mb_skipvarint(const uint8_t **p, const uint8_t *end)
{
    for (;;) {
        uint8_t b;
        if (mb_u8(p, end, &b)) return -1;
        if (!(b & 0x80)) return 0;
    }
}

/* Materialize the on-disk compact form back into a fully mutable DAFSA:
 * rebuilds the incoming-edge lists (refcount + in_head) and the register.
 * Returns the handle, or NULL on any error (partial handle freed). */
static dafsa *dafsa_load_impl(const char *path, int mutable);

dafsa *dafsa_load(const char *path)
{
    return dafsa_load_impl(path, 1);
}

/* Fast read-only load: parses the same on-disk form but skips rebuilding the
 * incoming-edge table and the register, which are only needed for mutation
 * (add/delete/merge).  Lookup and prefix_enum read only states[]/trans[]/
 * is_final, so a search-only handle loads dramatically faster on large
 * indexes (sessions: ~360ms -> ~40ms).  The handle MUST NOT be mutated. */
dafsa *dafsa_load_readonly(const char *path)
{
    return dafsa_load_impl(path, 0);
}

static dafsa *dafsa_load_impl(const char *path, int mutable)
{
    int fd = -1;
    dafsa *d = NULL;
    uint8_t *map = NULL;
    uint8_t *final_bits = NULL;
    const uint8_t *p, *end;
    uint32_t version, n_states, n_trans, initial_id, n_final, reserved;
    uint32_t running;
    size_t bitmap_bytes, fsize = 0;
    uint32_t i, j;

    if (!path) return NULL;

    fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    {
        struct stat st;
        if (fstat(fd, &st) != 0) goto fail;
        fsize = (size_t)st.st_size;
    }
    if (fsize == 0) goto fail;
    map = (uint8_t *)mmap(NULL, fsize, PROT_READ, MAP_PRIVATE, fd, 0);
    if (map == MAP_FAILED) { map = NULL; goto fail; }
    close(fd);
    fd = -1;
    p = map;
    end = map + fsize;

    /* header */
    {
        uint8_t magic[4];
        if (mb_u8(&p, end, &magic[0]) || mb_u8(&p, end, &magic[1]) ||
            mb_u8(&p, end, &magic[2]) || mb_u8(&p, end, &magic[3]))
            goto fail;
        if (magic[0] != 'P' || magic[1] != 'D' ||
            magic[2] != 'W' || magic[3] != 'G')
            goto fail;
    }
    if (mb_u32(&p, end, &version) || mb_u32(&p, end, &n_states) ||
        mb_u32(&p, end, &n_trans) || mb_u32(&p, end, &initial_id) ||
        mb_u32(&p, end, &n_final) || mb_u32(&p, end, &reserved))
        goto fail;
    (void)reserved;
    if (version != 3) goto fail;
    if (initial_id != 1) goto fail;
    if (n_states == 0) goto fail;                       /* initial must exist */
    if ((size_t)n_states + 1 > SIZE_MAX / sizeof(State)) goto fail;

    d = dafsa_create();
    if (!d) goto fail;

    /* grow states array to hold n_states+1 entries.
     * Round capacity up to a power of two so the first `state_new` after a
     * load doesn't immediately double-and-realloc the entire array (which, at
     * 2M+ states, copies ~80MB). */
    if ((size_t)n_states + 1 > d->states_cap) {
        size_t need = (size_t)n_states + 1;
        size_t new_cap = d->states_cap;
        State *new_states;
        while (new_cap < need) new_cap *= 2;
        new_states = (State *)realloc(d->states, new_cap * sizeof(State));
        if (!new_states) goto fail;
        memset(new_states + d->states_cap, 0,
               (new_cap - d->states_cap) * sizeof(State));
        d->states = new_states;
        d->states_cap = new_cap;
    }
    d->nstates = n_states + 1;
    d->initial = 1;

    /* zero sink + live states; restore self indices */
    memset(d->states, 0, (size_t)(n_states + 1) * sizeof(State));
    for (i = 0; i <= n_states; i++)
        d->states[i].id = i;

    /* state table: (n_states+1) x u16 LE ntrans (entry 0 = 0). Offsets are
     * implied (cumulative), so we only validate the total. */
    {
        uint16_t sink_nt;
        if (mb_u16(&p, end, &sink_nt)) goto fail;
        if (sink_nt != 0) goto fail;
    }
    for (i = 1; i <= n_states; i++) {
        uint16_t nt;
        if (mb_u16(&p, end, &nt)) goto fail;
        d->states[i].ntrans = nt;
    }
    running = 0;
    for (i = 1; i <= n_states; i++)
        running += d->states[i].ntrans;
    if (running != n_trans) goto fail;

    /* final bitmap */
    bitmap_bytes = (size_t)((n_states + 8) / 8);
    final_bits = (uint8_t *)malloc(bitmap_bytes);
    if (!final_bits) goto fail;
    if (p + bitmap_bytes > end) goto fail;
    memcpy(final_bits, p, bitmap_bytes);
    p += bitmap_bytes;
    {
        uint32_t finals = 0;
        for (i = 1; i <= n_states; i++) {
            if (final_bits[i / 8] & (uint8_t)(1u << (i % 8))) {
                d->states[i].is_final = 1;
                finals++;
            }
        }
        if (finals != n_final) goto fail;
    }

    /* CSR: direct copy into trans[] (already sorted, no trans_add) */
    for (i = 1; i <= n_states; i++) {
        State *s = &d->states[i];
        /* Allocate the sparse transition array for this state. On OOM abort. */
        if (s->ntrans > 0 && trans_reserve(s, s->ntrans) != 0) {
            fprintf(stderr, "dafsa: OOM loading transitions\n");
            abort();
        }
        for (j = 0; j < s->ntrans; j++) {
            uint8_t sym;
            uint32_t target;
            if (mb_u8(&p, end, &sym)) goto fail;
            if (mb_uvarint(&p, end, &target)) goto fail;
            if (target > n_states) goto fail;           /* 0 = sink, else 1..N */
            s->trans[j].sym = sym;
            s->trans[j].target = target;
        }
    }
    if (p != end) goto fail;  /* reject trailing bytes after CSR */

    /* Rebuild incoming edges + register ONLY for a mutable handle.  Search
     * (lookup / prefix_enum) does not need either; skipping them is the whole
     * point of the read-only fast load path. */
    if (mutable) {
        /* rebuild incoming edges: restores refcount + in_head */
        for (i = 1; i <= n_states; i++) {
            State *s = &d->states[i];
            for (j = 0; j < s->ntrans; j++)
                incoming_add(d, i, s->trans[j].sym, s->trans[j].target);
        }

        /* rebuild register: sig_compute + reg_insert per live state */
        for (i = 1; i <= n_states; i++) {
            State *s = &d->states[i];
            uint64_t sig = sig_compute(s);
            s->sig = sig;
            reg_insert(d, sig, i);
        }
    }

    if (map) munmap(map, fsize);
    free(final_bits);
    return d;

fail:
    if (fd >= 0) close(fd);
    if (map) munmap(map, fsize);
    dafsa_free(d);
    free(final_bits);
    return NULL;
}

/* ─── Zero-copy search-only view ───────────────────────────────────────── */

dafsa_view *dafsa_view_open(const char *path)
{
    int fd = -1;
    dafsa_view *v = NULL;
    uint8_t *map = NULL;
    const uint8_t *p, *end, *ntbl;
    uint32_t version, n_states, n_trans, initial_id, n_final, reserved;
    uint64_t *state_off = NULL;
    size_t bitmap_bytes, fsize = 0;
    uint32_t i, j;

    if (!path) return NULL;

    fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    {
        struct stat st;
        if (fstat(fd, &st) != 0) goto fail;
        fsize = (size_t)st.st_size;
    }
    if (fsize < 28) goto fail;                       /* minimum: header only */
    map = (uint8_t *)mmap(NULL, fsize, PROT_READ, MAP_PRIVATE, fd, 0);
    if (map == MAP_FAILED) { map = NULL; goto fail; }
    close(fd);
    fd = -1;
    p   = map;
    end = map + fsize;

    /* header */
    {
        uint8_t magic[4];
        if (mb_u8(&p, end, &magic[0]) || mb_u8(&p, end, &magic[1]) ||
            mb_u8(&p, end, &magic[2]) || mb_u8(&p, end, &magic[3]))
            goto fail;
        if (magic[0] != 'P' || magic[1] != 'D' ||
            magic[2] != 'W' || magic[3] != 'G')
            goto fail;
    }
    if (mb_u32(&p, end, &version) || mb_u32(&p, end, &n_states) ||
        mb_u32(&p, end, &n_trans) || mb_u32(&p, end, &initial_id) ||
        mb_u32(&p, end, &n_final) || mb_u32(&p, end, &reserved))
        goto fail;
    (void)reserved;
    if (version != 3) goto fail;
    if (initial_id != 1) goto fail;
    if (n_states == 0) goto fail;

    /* state table: capture ntbl BEFORE advancing past it */
    ntbl = p;
    {
        uint16_t sink_nt;
        if (mb_u16(&p, end, &sink_nt)) goto fail;
        if (sink_nt != 0) goto fail;
    }
    /* now p points past the sink u16; skip remaining n_states u16 entries */
    if ((size_t)n_states > (size_t)(end - p) / 2) goto fail;
    p += (size_t)n_states * 2;

    /* final bitmap */
    bitmap_bytes = (size_t)((n_states + 8) / 8);
    if (p + bitmap_bytes > end) goto fail;
    {
        /* Validate n_final against popcount of the bitmap */
        const uint8_t *bm = p;
        uint32_t finals = 0;
        for (i = 0; i < bitmap_bytes; i++) {
            uint8_t b = bm[i];
            /* popcount per byte via Brian Kernighan's method */
            while (b) { finals++; b &= (uint8_t)(b - 1); }
        }
        if (finals != n_final) goto fail;
    }
    p += bitmap_bytes;

    /* build state_off: walk the CSR once, reading ntrans from ntbl.
     * Also verify n_trans matches the sum of ntrans values. */
    if ((size_t)n_states + 2 > (size_t)-1 / sizeof(uint64_t)) goto fail;
    state_off = calloc(n_states + 2, sizeof(uint64_t));
    if (!state_off) goto fail;

    {
        const uint8_t *q = p;         /* p now points at first CSR byte */
        uint32_t nt_sum = 0;
        state_off[0] = 0;
        for (i = 1; i <= n_states; i++) {
            /* ntbl points at table start; state i's u16 is at bytes 2*i, 2*i+1 */
            uint16_t nt = (uint16_t)ntbl[(size_t)i * 2]
                        | ((uint16_t)ntbl[(size_t)i * 2 + 1] << 8);
            nt_sum += nt;
            state_off[i] = (uint64_t)(q - p);
            for (j = 0; j < nt; j++) {
                uint32_t tgt;
                if (q >= end) goto fail;
                q++;                                       /* sym byte */
                if (mb_uvarint(&q, end, &tgt)) goto fail;  /* target */
                if (tgt > n_states) goto fail;             /* bounds check */
            }
        }
        state_off[n_states + 1] = (uint64_t)(q - p);
        if (nt_sum != n_trans) goto fail;  /* header n_trans mismatch */
        if (q != end) goto fail;           /* CSR must end exactly at EOF */
    }

    v = calloc(1, sizeof(*v));
    if (!v) goto fail;
    v->map        = map;
    v->map_len    = fsize;
    v->n_states   = n_states;
    v->initial    = initial_id;
    v->final_bits = p - bitmap_bytes;  /* bitmap start */
    v->csr        = p;                 /* first CSR byte */
    v->state_off  = state_off;

    return v;

fail:
    if (fd >= 0) close(fd);
    if (map) munmap(map, fsize);
    free(state_off);
    free(v);
    return NULL;
}

void dafsa_view_close(dafsa_view *v)
{
    if (!v) return;
    if (v->map) munmap(v->map, v->map_len);
    free(v->state_off);
    free(v);
}

/* ─── Prefix enumeration ──────────────────────────────────────────────── */

/* Recursive DFS from `state`, appending transition bytes into buf.  Calls
 * cb at each final state with the accumulated payload (bytes collected after
 * the 0x00 edge).  Returns non-zero to stop early. */
static int enum_dfs(const dafsa *d, unsigned int state, unsigned char *buf,
                    size_t depth, dafsa_enum_cb cb, void *user, long *count)
{
    const State *s = &d->states[state];
    uint32_t j;

    if (s->is_final) {
        (*count)++;
        if (cb(buf, depth, user) != 0) return 1;
    }
    if (depth >= MAX_WORD_LEN) return 0;
    for (j = 0; j < s->ntrans; j++) {
        buf[depth] = (unsigned char)s->trans[j].sym;
        if (enum_dfs(d, s->trans[j].target, buf, depth + 1,
                     cb, user, count) != 0)
            return 1;
    }
    return 0;
}

/* Enumerate keys of form prefix || 0x00 || payload.  Walks the prefix from
 * the initial state, requires a 0x00 edge next (W\0 semantics), then DFS the
 * payload states calling cb(payload, len).  Returns the number of keys
 * enumerated; 0 if the prefix is absent or not a key boundary. */
long dafsa_prefix_enum(const dafsa *d, const unsigned char *prefix,
                       size_t prefix_len, dafsa_enum_cb cb, void *user)
{
    unsigned int current;
    unsigned char buf[MAX_WORD_LEN];
    size_t i;
    int tr;
    long count = 0;

    if (!d || !cb) return -1;
    if (prefix == NULL && prefix_len > 0) return 0;
    if (prefix_len > MAX_WORD_LEN) return 0;

    current = d->initial;

    /* walk the prefix */
    for (i = 0; i < prefix_len; i++) {
        tr = trans_find(&d->states[current], prefix[i]);
        if (tr < 0) return 0;
        current = d->states[current].trans[tr].target;
    }

    /* W\0 semantics: a 0x00 edge must exist from the final prefix state */
    tr = trans_find(&d->states[current], 0x00);
    if (tr < 0) return 0;
    current = d->states[current].trans[tr].target;

    enum_dfs(d, current, buf, 0, cb, user, &count);
    return count;
}

/* ─── Zero-copy view read helpers ──────────────────────────────────────── */

static int view_trans_find(const dafsa_view *v, uint32_t s,
                           unsigned char sym, uint32_t *target_out)
{
    const uint8_t *p   = v->csr + v->state_off[s];
    const uint8_t *end = v->csr + v->state_off[s + 1];
    while (p < end) {
        unsigned char e_sym = *p++;
        if (e_sym == sym) {
            if (mb_uvarint(&p, end, target_out) != 0) return -1;
            if (*target_out > v->n_states) return -1;
            return 0;
        }
        if (mb_skipvarint(&p, end) != 0) return -1;
    }
    return -1;
}

static int view_edge_next(const dafsa_view *v, uint32_t s,
                          const uint8_t **cursor,
                          unsigned char *sym_out, uint32_t *target_out)
{
    const uint8_t *end = v->csr + v->state_off[s + 1];
    if (*cursor >= end) return -1;
    if (mb_u8(cursor, end, sym_out))    return -1;
    if (mb_uvarint(cursor, end, target_out)) return -1;
    if (*target_out > v->n_states) return -1;
    return 0;
}

/* Recursive DFS for the view, mirroring enum_dfs but reading edges via
 * view_edge_next and checking final_bits directly. */
static int view_enum_dfs(const dafsa_view *v, uint32_t state,
                          unsigned char *buf, size_t depth,
                          dafsa_enum_cb cb, void *user, long *count)
{
    const uint8_t *cur;
    unsigned char sym;
    uint32_t tgt;

    if (v->final_bits[state / 8] & (uint8_t)(1u << (state % 8))) {
        (*count)++;
        if (cb(buf, depth, user) != 0) return 1;
    }
    if (depth >= MAX_WORD_LEN) return 0;
    cur = v->csr + v->state_off[state];
    while (view_edge_next(v, state, &cur, &sym, &tgt) == 0) {
        buf[depth] = sym;
        if (view_enum_dfs(v, tgt, buf, depth + 1, cb, user, count) != 0)
            return 1;
    }
    return 0;
}

int dafsa_view_lookup_n(const dafsa_view *v,
                         const unsigned char *key, size_t len)
{
    uint32_t current;
    size_t i;

    if (!v) return 0;
    if (key == NULL && len > 0) return 0;

    current = v->initial;
    for (i = 0; i < len; i++) {
        uint32_t target;
        if (view_trans_find(v, current, key[i], &target) != 0)
            return 0;
        current = target;
    }
    return (v->final_bits[current / 8] &
            (uint8_t)(1u << (current % 8))) ? 1 : 0;
}

long dafsa_view_prefix_enum(const dafsa_view *v,
                             const unsigned char *prefix, size_t prefix_len,
                             dafsa_enum_cb cb, void *user)
{
    uint32_t current;
    unsigned char buf[MAX_WORD_LEN];
    size_t i;
    long count = 0;

    if (!v || !cb) return -1;
    if (prefix == NULL && prefix_len > 0) return 0;
    if (prefix_len > MAX_WORD_LEN) return 0;

    current = v->initial;

    /* walk the prefix */
    for (i = 0; i < prefix_len; i++) {
        uint32_t target;
        if (view_trans_find(v, current, prefix[i], &target) != 0)
            return 0;
        current = target;
    }

    /* W\0 semantics: a 0x00 edge must exist from the final prefix state */
    {
        uint32_t target;
        if (view_trans_find(v, current, 0x00, &target) != 0)
            return 0;
        current = target;
    }

    view_enum_dfs(v, current, buf, 0, cb, user, &count);
    return count;
}

/* ─── Statistics ───────────────────────────────────────────────────────── */

void dafsa_stats(const dafsa *d, dafsa_stats_out *out)
{
    unsigned char *visited;
    unsigned int *queue;
    unsigned int head, tail;
    unsigned int reachable, finals, transitions;

    if (!d || !out) return;

    visited = (unsigned char *)calloc(d->nstates, 1);
    queue   = (unsigned int *)malloc(d->nstates * sizeof(unsigned int));
    if (!visited || !queue) {
        free(visited);
        free(queue);
        /* Degrade gracefully: return zeros */
        memset(out, 0, sizeof(*out));
        return;
    }

    head     = 0;
    tail     = 0;
    reachable = 0;
    finals    = 0;
    transitions = 0;

    queue[tail++] = d->initial;
    visited[d->initial] = 1;

    while (head < tail) {
        unsigned int sid = queue[head++];
        const State *s = &d->states[sid];

        reachable++;
        if (s->is_final) finals++;
        transitions += s->ntrans;

        {
            unsigned int j;
            for (j = 0; j < s->ntrans; j++) {
                unsigned int tgt = s->trans[j].target;
                if (!visited[tgt]) {
                    visited[tgt] = 1;
                    queue[tail++] = tgt;
                }
            }
        }
    }

    out->n_states_total     = d->nstates - 1;  /* exclude sink 0 */
    out->n_states_reachable = reachable;
    out->n_final            = finals;
    out->n_trans            = transitions;
    out->register_probes    = d->reg_probes;

    free(visited);
    free(queue);
}

/* ─── Dot output for visualization ─────────────────────────────────────── */

void dafsa_dot(const dafsa *d, FILE *f)
{
    unsigned int i;

    if (!d || !f) return;

    fprintf(f, "digraph DAFSA {\n");
    fprintf(f, "  rankdir=LR;\n");
    fprintf(f, "  node [shape=circle,fontsize=10];\n");
    /* mark the initial state */
    fprintf(f, "  start [shape=point];\n");
    fprintf(f, "  start -> %u;\n", d->initial);

    for (i = 1; i < d->nstates; i++) {
        const State *s = &d->states[i];
        const char *shape;
        unsigned int j;

        if (s->refcount == 0 && s->id != d->initial) continue; /* skip orphans */

        shape = s->is_final ? "doublecircle" : "circle";
        fprintf(f, "  %u [shape=%s,label=\"%u (rc=%u)\"];\n",
                s->id, shape, s->id, s->refcount);

        for (j = 0; j < s->ntrans; j++) {
            fprintf(f, "  %u -> %u [label=\"%c\"];\n",
                    s->id, s->trans[j].target,
                    s->trans[j].sym >= 32 && s->trans[j].sym < 127
                        ? s->trans[j].sym : '?');
        }
    }
    fprintf(f, "}\n");
}

/* ─── DAFSA_DEBUG invariant checker ─────────────────────────────────────── */

#ifdef DAFSA_DEBUG
static void dafsa_check_invariants(const dafsa *d)
{
    unsigned int i;
    unsigned char *visited;
    unsigned int *queue;
    unsigned int head, tail;

    /* Allocate BFS workspace */
    visited = (unsigned char *)calloc(d->nstates, 1);
    queue   = (unsigned int *)malloc(d->nstates * sizeof(unsigned int));
    if (!visited || !queue) {
        free(visited);
        free(queue);
        return;  /* cannot check; skip gracefully */
    }

    /* BFS from initial to find reachable states */
    head = 0; tail = 0;
    queue[tail++] = d->initial;
    visited[d->initial] = 1;

    while (head < tail) {
        unsigned int sid = queue[head++];
        const State *s = &d->states[sid];

        /* (a) no orphan reachable from initial */
        assert(s->refcount > 0 || sid == d->initial);

        /* (b) ntrans matches the actual trans[] entries — trans[] is
         *     sorted by sym and has no duplicate sym */
        for (i = 0; i < s->ntrans; i++) {
            if (i > 0)
                assert(s->trans[i - 1].sym < s->trans[i].sym);
        }

        /* (c) refcount == number of inodes pointing at this state */
        {
            unsigned int ni = s->in_head;
            unsigned int cnt = 0;
            while (ni != 0) {
                cnt++;
                ni = d->inodes[ni].next;
            }
            assert(cnt == s->refcount);
        }

        /* enqueue children */
        for (i = 0; i < s->ntrans; i++) {
            unsigned int tgt = s->trans[i].target;
            if (!visited[tgt]) {
                visited[tgt] = 1;
                queue[tail++] = tgt;
            }
        }
    }

    /* (d) register: for each reachable state, reg_lookup must return
     *     either this state or a valid equivalent (matching sig, live).
     *     Stale entries pointing to dead states (refcount==0, id!=initial)
     *     are a known benign artefact of clone-on-write — they are
     *     validated away in replace_or_register. */
    for (i = 1; i < d->nstates; i++) {
        const State *s = &d->states[i];
        unsigned int eq;
        if (s->refcount == 0 && i != d->initial) continue; /* dead slot */
        /* Only check states that have a valid signature (transitions
         * or is_final).  Freshly-created states with sig==0 are dirty. */
        if (s->sig == 0) continue;
        eq = reg_lookup_no_count(d, s->sig);  /* does not perturb reg_probes */
        /* eq == 0 means no register entry — that's OK for dirty states
         * whose old sig was evicted.  If non-zero, it must be live. */
        if (eq != 0)
            assert(eq == d->initial || d->states[eq].refcount > 0);
    }

    free(visited);
    free(queue);
}
#endif /* DAFSA_DEBUG */
