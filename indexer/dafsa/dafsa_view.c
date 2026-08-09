/* dafsa_view.c — Zero-copy view + prefix enumeration */
#include "dafsa_internal.h"
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
    if (version != 3 && version != 4) goto fail;
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
        if (version == 4) {
            /* v4: verify trailing CRC32.  Covered region is [map, q) (q == CSR
             * end).  Stored CRC sits in the final 4 bytes, little-endian. */
            uint32_t stored, calc;
            if (fsize < 32) goto fail;            /* header 28 + CRC 4 */
            if (q + 4 != end) goto fail;          /* no trailing garbage after CRC */
            stored = (uint32_t)map[fsize - 4]
                   | ((uint32_t)map[fsize - 3] << 8)
                   | ((uint32_t)map[fsize - 2] << 16)
                   | ((uint32_t)map[fsize - 1] << 24);
            calc = crc32_compute(map, (size_t)(q - map));
            if (calc != stored) goto fail;
        } else {
            if (q != end) goto fail;      /* v3: CSR must end exactly at EOF */
        }
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
int enum_dfs(const dafsa *d, unsigned int state, unsigned char *buf,
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
        const Edge *e = &trans_arr_c(s)[j];
        buf[depth] = (unsigned char)e->sym;
        if (enum_dfs(d, e->target, buf, depth + 1,
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
        current = trans_arr_c(&d->states[current])[tr].target;
    }

    /* W\0 semantics: a 0x00 edge must exist from the final prefix state */
    tr = trans_find(&d->states[current], 0x00);
    if (tr < 0) return 0;
    current = trans_arr_c(&d->states[current])[tr].target;

    enum_dfs(d, current, buf, 0, cb, user, &count);
    return count;
}

/* ─── Zero-copy view read helpers ──────────────────────────────────────── */

int view_trans_find(const dafsa_view *v, uint32_t s,
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

int view_edge_next(const dafsa_view *v, uint32_t s,
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
int view_enum_dfs(const dafsa_view *v, uint32_t state,
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

