/*
 * dafsa_build.c — One-shot JSONL index build for dafsa-cli
 *
 * Replaces the per-key Python roundtrip in rebuilds with a one-shot C
 * subcommand.  The ENTIRE build runs in C: file walk, JSONL content
 * extraction, ASCII tokenization, composite-key build+dedup+sort,
 * DAFSA build+save, sidecar + manifest writing.
 *
 * Invoked from dafsa_cli.c main() as: build_main(argc-1, argv+1)
 * when argv[1] == "build".
 *
 * Byte-for-byte output compatible with Python _build_locked for the
 * JSONL extractor on ASCII corpora.
 */

#include "dafsa.h"
#include "dafsa_internal.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <fnmatch.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

/* ─── Constants ─────────────────────────────────────────────────────── */

#define MAX_PATH        4096
#define MAX_LINE        65536
#define MAX_TOKEN_LEN   100
#define MIN_TOKEN_LEN   2
#define PATH_BUF_SZ      (MAX_PATH + 32)  /* room for "/%d.keys" suffix */

/* ─── Dynamic string array ──────────────────────────────────────────── */

typedef struct {
    char   **items;
    size_t   len;
    size_t   cap;
} strvec;

static void strvec_init(strvec *v) { v->items = NULL; v->len = 0; v->cap = 0; }

static int strvec_push(strvec *v, char *s)
{
    if (v->len >= v->cap) {
        size_t nc = v->cap ? v->cap * 2 : 64;
        char **p = (char **)realloc(v->items, nc * sizeof(char *));
        if (!p) return -1;
        v->items = p;
        v->cap = nc;
    }
    v->items[v->len++] = s;
    return 0;
}

static void strvec_free(strvec *v)
{
    for (size_t i = 0; i < v->len; i++) free(v->items[i]);
    free(v->items);
}

/* ─── Sidecar pair: (entry_idx, word_bytes) ─────────────────────────── */

typedef struct {
    int             entry_idx;
    unsigned char  *word;
    size_t          word_len;
} pairrec;

typedef struct {
    pairrec *items;
    size_t   len;
    size_t   cap;
} pairvec;

static void pairvec_init(pairvec *v) { v->items = NULL; v->len = 0; v->cap = 0; }

static int pairvec_push(pairvec *v, int entry_idx,
                        const unsigned char *word, size_t word_len)
{
    if (v->len >= v->cap) {
        size_t nc = v->cap ? v->cap * 2 : 256;
        pairrec *p = (pairrec *)realloc(v->items, nc * sizeof(pairrec));
        if (!p) return -1;
        v->items = p;
        v->cap = nc;
    }
    unsigned char *copy = (unsigned char *)malloc(word_len);
    if (!copy) return -1;
    memcpy(copy, word, word_len);
    v->items[v->len].entry_idx = entry_idx;
    v->items[v->len].word      = copy;
    v->items[v->len].word_len  = word_len;
    v->len++;
    return 0;
}

static void pairvec_free(pairvec *v)
{
    for (size_t i = 0; i < v->len; i++) free(v->items[i].word);
    free(v->items);
}

/* ─── Per-file dedup hash set: (entry_idx, word) ──────────────────────
 * Replaces the O(N²) linear pairvec scan with O(1) amortized membership.
 * Open addressing, FNV-1a, power-of-two capacity.  Matches Python's
 * `seen: set[tuple[int, bytes]]` semantics (entry_idx is part of the key). */

typedef struct {
    int             entry_idx;
    unsigned char  *word;
    size_t          word_len;
    int             used;
} dedup_slot;

typedef struct {
    dedup_slot *slots;
    size_t      cap;    /* power of two */
    size_t      count;
} dedupset;

static void dedupset_init(dedupset *s) { s->slots = NULL; s->cap = 0; s->count = 0; }

static uint64_t fnv1a_hash(const unsigned char *data, size_t len)
{
    uint64_t h = 14695981039346656037ULL;
    for (size_t i = 0; i < len; i++) { h ^= data[i]; h *= 1099511628211ULL; }
    return h;
}

static int dedupset_grow(dedupset *s)
{
    size_t ncap = s->cap ? s->cap * 2 : 1024;
    dedup_slot *ns = (dedup_slot *)calloc(ncap, sizeof(dedup_slot));
    if (!ns) return -1;
    for (size_t i = 0; i < s->cap; i++) {
        if (!s->slots[i].used) continue;
        uint64_t h = fnv1a_hash((const unsigned char *)&s->slots[i].entry_idx,
                                sizeof(s->slots[i].entry_idx));
        h ^= fnv1a_hash(s->slots[i].word, s->slots[i].word_len);
        size_t idx = (size_t)(h & (uint64_t)(ncap - 1));
        while (ns[idx].used) idx = (idx + 1) & (ncap - 1);
        ns[idx] = s->slots[i];
    }
    free(s->slots);
    s->slots = ns;
    s->cap = ncap;
    return 0;
}

static int dedupset_contains(const dedupset *s, int entry_idx,
                             const unsigned char *word, size_t wlen)
{
    if (!s->slots) return 0;
    uint64_t h = fnv1a_hash((const unsigned char *)&entry_idx, sizeof(entry_idx));
    h ^= fnv1a_hash(word, wlen);
    size_t idx = (size_t)(h & (uint64_t)(s->cap - 1));
    while (s->slots[idx].used) {
        if (s->slots[idx].entry_idx == entry_idx &&
            s->slots[idx].word_len == wlen &&
            memcmp(s->slots[idx].word, word, wlen) == 0)
            return 1;
        idx = (idx + 1) & (s->cap - 1);
    }
    return 0;
}

static int dedupset_insert(dedupset *s, int entry_idx,
                           const unsigned char *word, size_t wlen)
{
    if (s->cap == 0 || s->count * 2 >= s->cap) {
        if (dedupset_grow(s) != 0) return -1;
    }
    uint64_t h = fnv1a_hash((const unsigned char *)&entry_idx, sizeof(entry_idx));
    h ^= fnv1a_hash(word, wlen);
    size_t idx = (size_t)(h & (uint64_t)(s->cap - 1));
    while (s->slots[idx].used) idx = (idx + 1) & (s->cap - 1);
    unsigned char *copy = (unsigned char *)malloc(wlen ? wlen : 1);
    if (!copy) return -1;
    if (wlen) memcpy(copy, word, wlen);
    s->slots[idx].entry_idx = entry_idx;
    s->slots[idx].word      = copy;
    s->slots[idx].word_len  = wlen;
    s->slots[idx].used      = 1;
    s->count++;
    return 0;
}

static void dedupset_free(dedupset *s)
{
    if (s->slots) {
        for (size_t i = 0; i < s->cap; i++)
            if (s->slots[i].used) free(s->slots[i].word);
        free(s->slots);
    }
    s->slots = NULL; s->cap = 0; s->count = 0;
}

/* ─── Composite-key array ───────────────────────────────────────────── */

typedef struct {
    unsigned char *data;
    size_t         len;
} keyrec;

typedef struct {
    keyrec *items;
    size_t  len;
    size_t  cap;
} keyvec;

static void keyvec_init(keyvec *v) { v->items = NULL; v->len = 0; v->cap = 0; }

static int keyvec_push(keyvec *v, const unsigned char *data, size_t len)
{
    if (v->len >= v->cap) {
        size_t nc = v->cap ? v->cap * 2 : 65536;
        keyrec *p = (keyrec *)realloc(v->items, nc * sizeof(keyrec));
        if (!p) return -1;
        v->items = p;
        v->cap = nc;
    }
    unsigned char *copy = (unsigned char *)malloc(len);
    if (!copy) return -1;
    memcpy(copy, data, len);
    v->items[v->len].data = copy;
    v->items[v->len].len  = len;
    v->len++;
    return 0;
}

static void keyvec_free(keyvec *v)
{
    for (size_t i = 0; i < v->len; i++) free(v->items[i].data);
    free(v->items);
}

/* ─── Usage ─────────────────────────────────────────────────────────── */

static void usage(void)
{
    fprintf(stderr,
            "Usage: dafsa-cli build --dir <dir> --pattern <glob> "
            "--output <out> [--tokenizer ascii]\n");
}

/* ─── PurePosixPath component-wise comparator ───────────────────────── */
/*
 * Python sorted(list[Path]) compares component-by-component (split on
 * '/'), shorter-component-list first.  strcmp diverges because '.' (0x2E)
 * < '/' (0x2F), so e.g. strcmp("a.txt","a/b.txt") < 0 but Python's
 * path sort puts "a/b.txt" first (shorter component list in the first
 * differing position).  File order drives manifest order, file_idx,
 * composite keys, and .fst bytes — must match EXACTLY.
 */

static int path_sort_cmp(const void *a, const void *b)
{
    const char *pa = *(const char *const *)a;
    const char *pb = *(const char *const *)b;

    for (;;) {
        const char *ca = pa;
        const char *cb = pb;
        while (*pa && *pa != '/') pa++;
        while (*pb && *pb != '/') pb++;
        size_t lena = (size_t)(pa - ca);
        size_t lenb = (size_t)(pb - cb);

        int r = memcmp(ca, cb, lena < lenb ? lena : lenb);
        if (r != 0) return r;
        if (lena != lenb) return lena < lenb ? -1 : 1;

        int a_end = (*pa == '\0');
        int b_end = (*pb == '\0');
        if (a_end && b_end) return 0;
        if (a_end) return -1;  /* shorter component list first */
        if (b_end) return 1;
        pa++; pb++;  /* skip '/' */
    }
}

/* ─── Composite-key comparator (byte-wise memcmp) ───────────────────── */

static int key_cmp(const void *a, const void *b)
{
    const keyrec *ka = (const keyrec *)a;
    const keyrec *kb = (const keyrec *)b;
    size_t minlen = ka->len < kb->len ? ka->len : kb->len;
    int r = memcmp(ka->data, kb->data, minlen);
    if (r != 0) return r;
    if (ka->len < kb->len) return -1;
    if (ka->len > kb->len) return 1;
    return 0;
}

/* ─── UTF-8 encoder (single code point → 1–4 bytes) ───────────────── */

static int encode_utf8(uint32_t cp, unsigned char *out)
{
    if (cp < 0x80) { out[0] = (unsigned char)cp; return 1; }
    if (cp < 0x800) {
        out[0] = (unsigned char)(0xC0 | (cp >> 6));
        out[1] = (unsigned char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (unsigned char)(0xE0 | (cp >> 12));
        out[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        out[2] = (unsigned char)(0x80 | (cp & 0x3F));
        return 3;
    }
    out[0] = (unsigned char)(0xF0 | (cp >> 18));
    out[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
    out[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
    out[3] = (unsigned char)(0x80 | (cp & 0x3F));
    return 4;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* ─── Extract "content" string field ────────────────────────────────── */
/*
 * Scans the JSON object for "content": then a JSON string.  Decodes
 * escapes fully: \" \\ \/ \b \f \n \r \t and \uXXXX → UTF-8 (surrogate
 * pairs handled).  Returns malloc'd NUL-terminated content, or NULL if
 * the field is absent / not a string / decode fails.  On NULL the caller
 * uses the raw line as the entry text.
 */

static char *extract_content_field(const char *line)
{
    const char *p = line;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    if (*p != '{') return NULL;
    p++;

    for (;;) {
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
        if (*p == '}') return NULL;
        if (*p != '"') return NULL;
        p++;

        /* Read key */
        char key[128];
        size_t kp = 0;
        while (*p && *p != '"') {
            if (*p == '\\') {
                p++;
                if (!*p) return NULL;
                if (kp < sizeof(key) - 1) key[kp++] = *p;
            } else {
                if (kp < sizeof(key) - 1) key[kp++] = *p;
            }
            p++;
        }
        if (*p != '"') return NULL;
        p++;
        key[kp] = '\0';

        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
        if (*p != ':') return NULL;
        p++;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;

        if (strcmp(key, "content") == 0) {
            if (*p != '"') return NULL;  /* not a string → raw line */
            p++;

            size_t outcap = 4096;
            char  *out = (char *)malloc(outcap);
            if (!out) return NULL;
            size_t op = 0;

            while (*p && *p != '"') {
                if (*p == '\\') {
                    p++;
                    switch (*p) {
                    case '"':  case '\\': case '/':
                        if (op + 1 >= outcap) {
                            size_t nc = outcap * 2;
                            char *t = (char *)realloc(out, nc);
                            if (!t) { free(out); return NULL; }
                            out = t; outcap = nc;
                        }
                        out[op++] = *p;
                        break;
                    case 'b': if (op < outcap) out[op++] = '\b'; break;
                    case 'f': if (op < outcap) out[op++] = '\f'; break;
                    case 'n': if (op < outcap) out[op++] = '\n'; break;
                    case 'r': if (op < outcap) out[op++] = '\r'; break;
                    case 't': if (op < outcap) out[op++] = '\t'; break;
                    case 'u': {
                        p++;
                        int d0 = hexval(p[0]), d1 = hexval(p[1]);
                        int d2 = hexval(p[2]), d3 = hexval(p[3]);
                        if (d0 < 0 || d1 < 0 || d2 < 0 || d3 < 0) {
                            free(out); return NULL;
                        }
                        uint32_t cp = (uint32_t)((d0 << 12) | (d1 << 8) |
                                                  (d2 << 4) | d3);
                        p += 3;
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            if (p[1] != '\\' || p[2] != 'u')
                                { free(out); return NULL; }
                            d0 = hexval(p[3]); d1 = hexval(p[4]);
                            d2 = hexval(p[5]); d3 = hexval(p[6]);
                            if (d0 < 0 || d1 < 0 || d2 < 0 || d3 < 0)
                                { free(out); return NULL; }
                            uint32_t lo = (uint32_t)((d0 << 12) | (d1 << 8) |
                                                      (d2 << 4) | d3);
                            if (lo < 0xDC00 || lo > 0xDFFF)
                                { free(out); return NULL; }
                            cp = 0x10000 + ((cp - 0xD800) << 10) +
                                 (lo - 0xDC00);
                            p += 6;
                        }
                        if (op + 4 >= outcap) {
                            size_t nc = outcap * 2;
                            char *t = (char *)realloc(out, nc);
                            if (!t) { free(out); return NULL; }
                            out = t; outcap = nc;
                        }
                        unsigned char u8[4];
                        int n = encode_utf8(cp, u8);
                        memcpy(out + op, u8, (size_t)n);
                        op += (size_t)n;
                        break;
                    }
                    default: free(out); return NULL;
                    }
                } else {
                    if (op + 1 >= outcap) {
                        size_t nc = outcap * 2;
                        char *t = (char *)realloc(out, nc);
                        if (!t) { free(out); return NULL; }
                        out = t; outcap = nc;
                    }
                    out[op++] = *p;
                }
                p++;
            }
            if (*p != '"') { free(out); return NULL; }
            out[op] = '\0';
            if (op == 0) { free(out); return NULL; }  /* empty → raw line */
            return out;
        }

        /* Skip non-"content" value */
        if (*p == '"') {
            p++; while (*p && *p != '"') { if (*p == '\\') p++; if (*p) p++; }
            if (*p == '"') p++;
        } else if (*p == '-' || (*p >= '0' && *p <= '9')) {
            if (*p == '-') p++;
            while (*p >= '0' && *p <= '9') p++;
            if (*p == '.') { p++; while (*p >= '0' && *p <= '9') p++; }
            if (*p == 'e' || *p == 'E') {
                p++; if (*p == '+' || *p == '-') p++;
                while (*p >= '0' && *p <= '9') p++;
            }
        } else if (strncmp(p, "true", 4) == 0)  { p += 4; }
        else if (strncmp(p, "false", 5) == 0) { p += 5; }
        else if (strncmp(p, "null", 4) == 0)  { p += 4; }
        else if (*p == '{' || *p == '[') {
            int depth = 1;
            char open = *p, close = (*p == '{') ? '}' : ']';
            p++;
            while (*p && depth > 0) {
                if (*p == '"') {
                    p++; while (*p && *p != '"') { if (*p == '\\') p++; if (*p) p++; }
                    if (*p) p++;
                } else if (*p == open) { depth++; p++; }
                else if (*p == close) { depth--; p++; }
                else p++;
            }
        } else return NULL;

        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
        if (*p == '}') return NULL;
        if (*p == ',') { p++; continue; }
        return NULL;
    }
}

/* ─── ASCII tokenizer ───────────────────────────────────────────────── */
/*
 * Matches Python tokenize (indexer/__init__.py:43):
 *   Split on runs of non-word chars where word char = [A-Za-z0-9_-].
 *   All bytes >= 0x80 are separators (v1 ASCII only).
 *   ASCII-lowercase [A-Z]→[a-z].
 *   Keep tokens of length 2..100 inclusive.
 */

static int is_word_char(unsigned char c)
{
    if (c >= 0x80) return 0;
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
           (c >= '0' && c <= '9') || c == '_' || c == '-';
}

static int tokenize_text(const char *text, strvec *tokens)
{
    const unsigned char *p = (const unsigned char *)text;
    int added = 0;

    while (*p) {
        while (*p && !is_word_char(*p)) p++;
        if (!*p) break;
        const unsigned char *start = p;
        while (*p && is_word_char(*p)) p++;
        size_t tlen = (size_t)(p - start);
        if (tlen >= MIN_TOKEN_LEN && tlen <= MAX_TOKEN_LEN) {
            char *tok = (char *)malloc(tlen + 1);
            if (!tok) return -1;
            for (size_t i = 0; i < tlen; i++) {
                unsigned char c = start[i];
                tok[i] = (char)((c >= 'A' && c <= 'Z') ? c + ('a' - 'A') : c);
            }
            tok[tlen] = '\0';
            if (strvec_push(tokens, tok) != 0) { free(tok); return -1; }
            added++;
        }
    }
    return added;
}

/* ─── Date extraction from filename ─────────────────────────────────── */

static void copy_date(const char *basename, char *out /* at least 11 bytes */)
{
    for (const char *p = basename; *p; p++) {
        if (p[0] >= '0' && p[0] <= '9' &&
            p[1] >= '0' && p[1] <= '9' &&
            p[2] >= '0' && p[2] <= '9' &&
            p[3] >= '0' && p[3] <= '9' &&
            p[4] == '-' &&
            p[5] >= '0' && p[5] <= '9' &&
            p[6] >= '0' && p[6] <= '9' &&
            p[7] == '-' &&
            p[8] >= '0' && p[8] <= '9' &&
            p[9] >= '0' && p[9] <= '9') {
            memcpy(out, p, 10);
            out[10] = '\0';
            return;
        }
    }
    out[0] = '?'; out[1] = '\0';
}

/* ─── Atomic write helpers ──────────────────────────────────────────── */

static int fsync_parent_dir(const char *path)
{
    char dir[MAX_PATH];
    size_t len = strlen(path);
    if (len >= sizeof(dir)) return -1;
    memcpy(dir, path, len + 1);
    char *slash = strrchr(dir, '/');
    if (!slash) return -1;
    if (slash == dir) dir[1] = '\0';
    else *slash = '\0';
    int fd = open(dir, O_RDONLY);
    if (fd < 0) return -1;
    int rc = fsync(fd);
    close(fd);
    return rc;
}

static int atomic_write_bytes(const char *path, const void *data, size_t len)
{
    char tmp[MAX_PATH];
    int n = snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof(tmp)) return -1;

    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;

    ssize_t w = write(fd, data, len);
    if (w < 0 || (size_t)w != len) { close(fd); unlink(tmp); return -1; }
    if (fsync(fd) != 0) { close(fd); unlink(tmp); return -1; }
    close(fd);

    if (rename(tmp, path) != 0) { unlink(tmp); return -1; }
    fsync_parent_dir(path);
    return 0;
}

/* ─── Sidecar write ─────────────────────────────────────────────────── */
/*
 * v1 format: SIDE(4) + version u32LE(4) + n_records u32LE(4) +
 *   records(entry_idx u32LE | word_len u32LE | word) +
 *   crc32 u32LE(4) over header+records.
 */

static int write_sidecar(const char *slots_dir, int file_idx,
                         const pairrec *pairs, size_t npairs)
{
    char path[PATH_BUF_SZ];
    snprintf(path, sizeof(path), "%s/%d.keys", slots_dir, file_idx);

    size_t body_len = 12;
    for (size_t i = 0; i < npairs; i++)
        body_len += 8 + pairs[i].word_len;

    unsigned char *buf = (unsigned char *)malloc(body_len + 4);
    if (!buf) return -1;

    memcpy(buf, "SIDE", 4);
    buf[4] = 1; buf[5] = 0; buf[6] = 0; buf[7] = 0;
    buf[8]  = (unsigned char)(npairs & 0xFF);
    buf[9]  = (unsigned char)((npairs >> 8) & 0xFF);
    buf[10] = (unsigned char)((npairs >> 16) & 0xFF);
    buf[11] = (unsigned char)((npairs >> 24) & 0xFF);

    size_t off = 12;
    for (size_t i = 0; i < npairs; i++) {
        uint32_t ei = (uint32_t)pairs[i].entry_idx;
        uint32_t wl = (uint32_t)pairs[i].word_len;
        buf[off + 0] = (unsigned char)(ei & 0xFF);
        buf[off + 1] = (unsigned char)((ei >> 8) & 0xFF);
        buf[off + 2] = (unsigned char)((ei >> 16) & 0xFF);
        buf[off + 3] = (unsigned char)((ei >> 24) & 0xFF);
        buf[off + 4] = (unsigned char)(wl & 0xFF);
        buf[off + 5] = (unsigned char)((wl >> 8) & 0xFF);
        buf[off + 6] = (unsigned char)((wl >> 16) & 0xFF);
        buf[off + 7] = (unsigned char)((wl >> 24) & 0xFF);
        off += 8;
        memcpy(buf + off, pairs[i].word, wl);
        off += wl;
    }

    uint32_t crc = crc32_compute(buf, body_len);
    buf[off + 0] = (unsigned char)(crc & 0xFF);
    buf[off + 1] = (unsigned char)((crc >> 8) & 0xFF);
    buf[off + 2] = (unsigned char)((crc >> 16) & 0xFF);
    buf[off + 3] = (unsigned char)((crc >> 24) & 0xFF);

    int rc = atomic_write_bytes(path, buf, body_len + 4);
    free(buf);
    return rc;
}

/* ─── Manifest write ────────────────────────────────────────────────── */
/*
 * Produces byte-exact json.dumps({"files":[...], "commit_seq":0}, indent=2)
 * with DEFAULT separators (", ", ": "), ensure_ascii=True, NO trailing
 * newline.  Field order: filename, title, date, source, mtime, size,
 * tombstoned.
 */

struct file_entry {
    char     filename[MAX_PATH];
    char     title[MAX_PATH];
    char     date[32];
    char     source[32];
    int64_t  mtime;    /* nanoseconds */
    int64_t  size;
};

/* Append string literal to dynamic buffer, growing as needed */
static int buf_append(char **buf, size_t *pos, size_t *cap, const char *s, size_t slen)
{
    if (*pos + slen >= *cap) {
        size_t nc = *cap ? *cap * 2 : 65536;
        if (nc < *pos + slen + 1) nc = *pos + slen + 4096;
        char *t = (char *)realloc(*buf, nc);
        if (!t) return -1;
        *buf = t;
        *cap = nc;
    }
    memcpy(*buf + *pos, s, slen);
    *pos += slen;
    return 0;
}

/* Append a JSON-string-escaped copy of s to buf */
static int buf_append_json_string(char **buf, size_t *pos, size_t *cap,
                                  const char *s)
{
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            char esc[2] = {'\\', (char)c};
            if (buf_append(buf, pos, cap, esc, 2) != 0) return -1;
        } else if (c < 0x20) {
            char hex[7];
            snprintf(hex, sizeof(hex), "\\u%04x", c);
            if (buf_append(buf, pos, cap, hex, 6) != 0) return -1;
        } else if (c >= 0x80) {
            char hex[7];
            snprintf(hex, sizeof(hex), "\\u%04x", c);
            if (buf_append(buf, pos, cap, hex, 6) != 0) return -1;
        } else {
            if (buf_append(buf, pos, cap, s, 1) != 0) return -1;
        }
    }
    return 0;
}

/* Append int64 decimal */
static int buf_append_int64(char **buf, size_t *pos, size_t *cap, int64_t v)
{
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%lld", (long long)v);
    if (n < 0) return -1;
    return buf_append(buf, pos, cap, tmp, (size_t)n);
}

static int write_manifest(const char *manifest_path,
                          const struct file_entry *entries,
                          size_t n_files)
{
    char  *buf = (char *)malloc(65536);
    if (!buf) return -1;
    size_t pos = 0;
    size_t cap = 65536;

#define A(s) do { if (buf_append(&buf, &pos, &cap, (s), strlen(s)) != 0) { free(buf); return -1; } } while (0)
#define AJ(s) do { \
        A("\""); \
        if (buf_append_json_string(&buf, &pos, &cap, (s)) != 0) { free(buf); return -1; } \
        A("\""); \
    } while (0)
#define AI(v) do { if (buf_append_int64(&buf, &pos, &cap, (v)) != 0) { free(buf); return -1; } } while (0)

    A("{\n");
    A("  \"files\": [\n");

    for (size_t fi = 0; fi < n_files; fi++) {
        const struct file_entry *e = &entries[fi];

        A("    {\n");
        A("      \"filename\": "); AJ(e->filename); A(",\n");
        A("      \"title\": ");    AJ(e->title);    A(",\n");
        A("      \"date\": ");     AJ(e->date);     A(",\n");
        A("      \"source\": ");   AJ(e->source);   A(",\n");
        A("      \"mtime\": ");    AI(e->mtime);    A(",\n");
        A("      \"size\": ");     AI(e->size);     A(",\n");
        A("      \"tombstoned\": false\n");
        A("    }");
        if (fi + 1 < n_files) A(",");
        A("\n");
    }

    A("  ],\n");
    A("  \"commit_seq\": 0\n");
    A("}");

#undef A
#undef AJ
#undef AI

    int rc = atomic_write_bytes(manifest_path, buf, pos);
    free(buf);
    return rc;
}

/* ─── Recursive file walk ───────────────────────────────────────────── */
/*
 * Mirrors Python collect_files: recursive opendir/readdir walk, prune
 * symlinked dirs, fnmatch(3) on basename against pattern.  Collects paths
 * sorted with PurePosixPath component-wise comparator (path_sort_cmp).
 */

static int collect_files(const char *root, const char *pattern, strvec *out,
                         const char *rel_prefix)
{
    DIR *dp = opendir(root);
    if (!dp) return -1;

    struct dirent *de;
    while ((de = readdir(dp)) != NULL) {
        if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0)
            continue;

        char full[MAX_PATH];
        snprintf(full, sizeof(full), "%s/%s", root, de->d_name);

        struct stat st;
        if (lstat(full, &st) != 0) continue;

        /* Prune ALL symlinks (matches Python os.walk which skips symlinked
         * dirs via dirs[:]=[d for d if not is_symlink] and never collects a
         * symlink as a file).  Under lstat a symlink-to-dir has S_IFLNK, not
         * S_IFDIR, so this must be checked before the S_ISDIR branch. */
        if (S_ISLNK(st.st_mode)) continue;

        if (S_ISDIR(st.st_mode)) {
            /* Build sub_rel for this directory */
            char sub_rel[MAX_PATH];
            if (rel_prefix && rel_prefix[0])
                snprintf(sub_rel, sizeof(sub_rel), "%s/%s", rel_prefix, de->d_name);
            else
                snprintf(sub_rel, sizeof(sub_rel), "%s", de->d_name);

            char subdir[MAX_PATH];
            snprintf(subdir, sizeof(subdir), "%s/%s", root, de->d_name);
            if (collect_files(subdir, pattern, out, sub_rel) != 0) {
                closedir(dp);
                return -1;
            }
        } else if (S_ISREG(st.st_mode)) {
            if (fnmatch(pattern, de->d_name, 0) == 0) {
                char *rel;
                if (rel_prefix && rel_prefix[0]) {
                    size_t n = strlen(rel_prefix) + 1 + strlen(de->d_name);
                    rel = (char *)malloc(n + 1);
                    if (!rel) { closedir(dp); return -1; }
                    snprintf(rel, n + 1, "%s/%s", rel_prefix, de->d_name);
                } else {
                    rel = strdup(de->d_name);
                    if (!rel) { closedir(dp); return -1; }
                }
                if (strvec_push(out, rel) != 0) {
                    free(rel);
                    closedir(dp);
                    return -1;
                }
            }
        }
    }
    closedir(dp);
    return 0;
}

/* ─── Build composite key ───────────────────────────────────────────── */
/*
 * key = word + \0 + file_idx(u32BE) + entry_idx(u32BE)
 */

static size_t make_composite_key(unsigned char *buf, size_t buf_sz,
                                 const char *word,
                                 int file_idx, int entry_idx)
{
    size_t wlen = strlen(word);
    if (wlen + 1 + 4 + 4 > buf_sz) return 0;
    memcpy(buf, word, wlen);
    buf[wlen] = '\0';
    uint32_t fi = (uint32_t)file_idx;
    uint32_t ei = (uint32_t)entry_idx;
    buf[wlen + 1] = (unsigned char)(fi >> 24);
    buf[wlen + 2] = (unsigned char)(fi >> 16);
    buf[wlen + 3] = (unsigned char)(fi >> 8);
    buf[wlen + 4] = (unsigned char)(fi);
    buf[wlen + 5] = (unsigned char)(ei >> 24);
    buf[wlen + 6] = (unsigned char)(ei >> 16);
    buf[wlen + 7] = (unsigned char)(ei >> 8);
    buf[wlen + 8] = (unsigned char)(ei);
    return wlen + 9;
}

/* ─── main dispatch ─────────────────────────────────────────────────── */

int build_main(int argc, char **argv)
{
    const char *dir_arg     = NULL;
    const char *pattern_arg = NULL;
    const char *output_arg  = NULL;
    const char *tokenizer   = "ascii";

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--dir") == 0 && i + 1 < argc)
            dir_arg = argv[++i];
        else if (strcmp(argv[i], "--pattern") == 0 && i + 1 < argc)
            pattern_arg = argv[++i];
        else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc)
            output_arg = argv[++i];
        else if (strcmp(argv[i], "--tokenizer") == 0 && i + 1 < argc)
            tokenizer = argv[++i];
        else if (strcmp(argv[i], "--help") == 0) {
            usage();
            return 0;
        } else {
            fprintf(stderr, "dafsa-cli build: unknown option: %s\n", argv[i]);
            usage();
            return 1;
        }
    }

    if (!dir_arg || !pattern_arg || !output_arg) {
        fprintf(stderr, "dafsa-cli build: missing required argument\n");
        usage();
        return 1;
    }
    if (strcmp(tokenizer, "ascii") != 0) {
        fprintf(stderr, "dafsa-cli build: only --tokenizer ascii is supported\n");
        return 1;
    }

    /* Validate dir */
    struct stat dir_st;
    if (stat(dir_arg, &dir_st) != 0 || !S_ISDIR(dir_st.st_mode)) {
        fprintf(stderr, "dafsa-cli build: --dir %s is not a directory\n",
                dir_arg);
        return 1;
    }

    int ret = 1;  /* assume failure */
    int lock_fd = -1;  /* invalid fd until lock acquired */

    /* All variables declared and initialized up-front so the cleanup
     * path is safe regardless of where we goto done. */
    strvec files;
    strvec_init(&files);
    size_t n_files = 0;
    struct file_entry *manifest_entries = NULL;
    pairvec *file_pairs = NULL;
    keyvec keys;
    keyvec_init(&keys);
    int total_entries = 0;
    char slots_dir[PATH_BUF_SZ] = {0};

    /* Create output dir and slots subdir */
    if (mkdir(output_arg, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "dafsa-cli build: cannot create output dir %s: %s\n",
                output_arg, strerror(errno));
        goto done;
    }
    snprintf(slots_dir, sizeof(slots_dir), "%s/slots", output_arg);
    if (mkdir(slots_dir, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "dafsa-cli build: cannot create slots dir: %s\n",
                strerror(errno));
        goto done;
    }

    /* Lock */
    char lock_path[MAX_PATH];
    snprintf(lock_path, sizeof(lock_path), "%s/index.lock", output_arg);
    lock_fd = open(lock_path, O_RDWR | O_CREAT, 0644);
    if (lock_fd < 0) {
        fprintf(stderr, "dafsa-cli build: cannot open lock file: %s\n",
                strerror(errno));
        ret = 1;
        /* No lock acquired — no unlock needed. */
        goto done_no_lock;
    }
    if (flock(lock_fd, LOCK_EX) != 0) {
        fprintf(stderr, "dafsa-cli build: cannot acquire lock: %s\n",
                strerror(errno));
        close(lock_fd);
        ret = 1;
        goto done_no_lock;
    }

    /* ── File walk ──────────────────────────────────────────────────── */
    if (collect_files(dir_arg, pattern_arg, &files, "") != 0) {
        fprintf(stderr, "dafsa-cli build: file walk failed\n");
        goto done;
    }

    if (files.len > 0)
        qsort(files.items, files.len, sizeof(char *), path_sort_cmp);

    n_files = files.len;
    total_entries = 0;

    if (n_files > 0) {
        manifest_entries = (struct file_entry *)calloc(n_files,
                                                        sizeof(struct file_entry));
        file_pairs = (pairvec *)calloc(n_files, sizeof(pairvec));
        if (!manifest_entries || !file_pairs) {
            fprintf(stderr, "dafsa-cli build: out of memory\n");
            free(manifest_entries);
            free(file_pairs);
            manifest_entries = NULL;
            file_pairs = NULL;
            goto done;
        }
        for (size_t i = 0; i < n_files; i++)
            pairvec_init(&file_pairs[i]);
    }

    /* ── Per-file processing ────────────────────────────────────────── */

    for (size_t file_idx = 0; file_idx < n_files; file_idx++) {
        const char *rel = files.items[file_idx];

        char full_path[MAX_PATH];
        snprintf(full_path, sizeof(full_path), "%s/%s", dir_arg, rel);

        struct stat st;
        if (stat(full_path, &st) != 0) {
            fprintf(stderr, "dafsa-cli build: cannot stat %s: %s\n",
                    full_path, strerror(errno));
            goto done;
        }

        int64_t mtime_ns = (int64_t)st.st_mtim.tv_sec * 1000000000LL +
                           (int64_t)st.st_mtim.tv_nsec;

        const char *base = strrchr(rel, '/');
        base = base ? base + 1 : rel;
        char date_buf[32];
        copy_date(base, date_buf);

        snprintf(manifest_entries[file_idx].filename, MAX_PATH, "%s", rel);
        snprintf(manifest_entries[file_idx].title, MAX_PATH, "%s", rel);
        snprintf(manifest_entries[file_idx].date, sizeof(date_buf), "%s", date_buf);
        snprintf(manifest_entries[file_idx].source, 32, "jsonl");
        manifest_entries[file_idx].mtime = mtime_ns;
        manifest_entries[file_idx].size  = (int64_t)st.st_size;

        /* Read file content */
        FILE *fp = fopen(full_path, "rb");
        if (!fp) {
            fprintf(stderr, "dafsa-cli build: cannot open %s: %s\n",
                    full_path, strerror(errno));
            goto done;
        }

        size_t content_cap = (size_t)st.st_size + 1;
        if (content_cap < 4096) content_cap = 4096;
        char *content = (char *)malloc(content_cap);
        if (!content) { fclose(fp); goto done; }
        size_t content_len = fread(content, 1, content_cap - 1, fp);
        fclose(fp);
        content[content_len] = '\0';

        /* Process lines: split, extract, tokenize */
        const char *p = content;
        const char *end = content + content_len;
        int entry_idx = 0;
        dedupset dset;
        dedupset_init(&dset);
        pairvec *pv = &file_pairs[file_idx];

        while (p < end) {
            /* Find end of line */
            const char *line_start = p;
            while (p < end && *p != '\n' && *p != '\r') p++;
            size_t linelen = (size_t)(p - line_start);
            if (p < end && *p == '\r') p++;
            if (p < end && *p == '\n') p++;

            /* Skip blank lines */
            int blank = 1;
            for (size_t i = 0; i < linelen; i++) {
                if (line_start[i] != ' ' && line_start[i] != '\t' &&
                    line_start[i] != '\n' && line_start[i] != '\r') {
                    blank = 0; break;
                }
            }
            if (blank) continue;

            /* NUL-terminated copy */
            char *line_copy = (char *)malloc(linelen + 1);
            if (!line_copy) { free(content); goto done; }
            memcpy(line_copy, line_start, linelen);
            line_copy[linelen] = '\0';

            /* Extract content field */
            char *extracted = extract_content_field(line_copy);
            char *entry_text = extracted ? extracted : line_copy;

            /* Tokenize */
            strvec tokens;
            strvec_init(&tokens);
            int nt = tokenize_text(entry_text, &tokens);

            for (int ti = 0; ti < nt; ti++) {
                const char *word = tokens.items[ti];
                size_t wlen = strlen(word);

                /* Dedup within this file (O(1) hash-set membership, matching
                 * Python's set[tuple[int,bytes]]). */
                if (dedupset_contains(&dset, entry_idx,
                                      (const unsigned char *)word, wlen))
                    continue;

                if (dedupset_insert(&dset, entry_idx,
                                    (const unsigned char *)word, wlen) != 0) {
                    strvec_free(&tokens);
                    free(line_copy);
                    free(extracted);
                    free(content);
                    dedupset_free(&dset);
                    goto done;
                }

                if (pairvec_push(pv, entry_idx,
                                 (const unsigned char *)word, wlen) != 0) {
                    strvec_free(&tokens);
                    free(line_copy);
                    free(extracted);
                    free(content);
                    dedupset_free(&dset);
                    goto done;
                }

                /* Composite key */
                unsigned char ckey[MAX_LINE];
                size_t cklen = make_composite_key(ckey, sizeof(ckey),
                                                  word, (int)file_idx, entry_idx);
                if (cklen == 0 || keyvec_push(&keys, ckey, cklen) != 0) {
                    strvec_free(&tokens);
                    free(line_copy);
                    free(extracted);
                    free(content);
                    dedupset_free(&dset);
                    goto done;
                }
            }

            strvec_free(&tokens);
            free(line_copy);   /* always free; extracted may alias it */
            free(extracted);
            entry_idx++;
            total_entries++;
        }
        free(content);
        dedupset_free(&dset);
    }

    /* ── Build DAFSA ────────────────────────────────────────────────── */
    size_t n_unique_keys = 0;

    if (keys.len > 0) {
        qsort(keys.items, keys.len, sizeof(keyrec), key_cmp);

        /* Count unique */
        n_unique_keys = 1;
        for (size_t i = 1; i < keys.len; i++)
            if (key_cmp(&keys.items[i - 1], &keys.items[i]) != 0)
                n_unique_keys++;

        /* Build */
        dafsa *d = dafsa_create();
        if (!d) { fprintf(stderr, "dafsa_create OOM\n"); goto done; }

        if (dafsa_add_n(d, keys.items[0].data, keys.items[0].len) < 0) {
            fprintf(stderr, "dafsa_add_n failed\n");
            dafsa_free(d);
            goto done;
        }

        for (size_t i = 1; i < keys.len; i++) {
            if (key_cmp(&keys.items[i - 1], &keys.items[i]) == 0)
                continue;
            if (dafsa_add_n(d, keys.items[i].data, keys.items[i].len) < 0) {
                fprintf(stderr, "dafsa_add_n failed\n");
                dafsa_free(d);
                goto done;
            }
        }

        char fst_path[MAX_PATH];
        snprintf(fst_path, sizeof(fst_path), "%s/index.fst", output_arg);
        int save_rc = dafsa_save(d, fst_path);
        dafsa_free(d);
        if (save_rc != 0) { fprintf(stderr, "dafsa_save failed\n"); goto done; }
    } else {
        /* Empty index */
        dafsa *d = dafsa_create();
        if (!d) { fprintf(stderr, "dafsa_create OOM\n"); goto done; }
        char fst_path[MAX_PATH];
        snprintf(fst_path, sizeof(fst_path), "%s/index.fst", output_arg);
        int save_rc = dafsa_save(d, fst_path);
        dafsa_free(d);
        if (save_rc != 0) { fprintf(stderr, "dafsa_save failed\n"); goto done; }
    }

    /* Remove index.wal */
    {
        char wal_path[MAX_PATH];
        snprintf(wal_path, sizeof(wal_path), "%s/index.wal", output_arg);
        unlink(wal_path);
    }

    /* ── Write sidecars ──────────────────────────────────────────────── */
    for (size_t fi = 0; fi < n_files; fi++) {
        if (write_sidecar(slots_dir, (int)fi,
                          file_pairs[fi].items,
                          file_pairs[fi].len) != 0) {
            fprintf(stderr, "dafsa-cli build: write_sidecar failed for slot %zu\n", fi);
            goto done;
        }
    }

    /* ── Write manifest ──────────────────────────────────────────────── */
    {
        char mp[MAX_PATH];
        snprintf(mp, sizeof(mp), "%s/manifest.json", output_arg);
        if (write_manifest(mp, manifest_entries, n_files) != 0) {
            fprintf(stderr, "dafsa-cli build: write_manifest failed\n");
            goto done;
        }
    }

    /* ── Log ─────────────────────────────────────────────────────────── */
    {
        char fst_path[MAX_PATH];
        snprintf(fst_path, sizeof(fst_path), "%s/index.fst", output_arg);
        struct stat fst_st;
        int64_t fs = 0;
        if (stat(fst_path, &fst_st) == 0) fs = (int64_t)fst_st.st_size;

        char mp[MAX_PATH];
        snprintf(mp, sizeof(mp), "%s/manifest.json", output_arg);
        struct stat ms_st;
        int64_t ms = 0;
        if (stat(mp, &ms_st) == 0) ms = (int64_t)ms_st.st_size;

        fprintf(stderr,
                "Done. FST: %.2f MB, Manifest: %.1f KB | "
                "%zu unique keys from %d entries across %zu files\n",
                fs / 1048576.0, ms / 1024.0,
                n_unique_keys, total_entries, n_files);
    }

    ret = 0;

done:
    if (file_pairs) {
        for (size_t i = 0; i < n_files; i++)
            pairvec_free(&file_pairs[i]);
        free(file_pairs);
    }
    free(manifest_entries);
    keyvec_free(&keys);
    strvec_free(&files);

    if (lock_fd >= 0) {
        flock(lock_fd, LOCK_UN);
        close(lock_fd);
    }
done_no_lock:
    return ret;
}
