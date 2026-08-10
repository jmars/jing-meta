/*
 * dafsa_cli.c — JSON-lines stdio daemon for the Carrasco & Forcada DAFSA
 *
 * Protocol: one JSON object per line on stdin (Python → daemon) and stdout
 * (daemon → Python).  All C diagnostics go to stderr; stdout is a pure
 * protocol channel.  The C core (dafsa*.c) is already stdout-clean per
 * contract — the daemon never routes dafsa_dot to stdout.
 *
 * Build as a static musl binary (runs under host glibc AND musl sandbox):
 *   gcc -static -O2 -Wall -Wextra -Werror -I. -o dafsa-cli dafsa_cli.c \
 *       dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o \
 *       dafsa_crc32.o dafsa_wal.o
 *
 * If the hand-rolled JSON tokenizer ever becomes a maintenance burden,
 * swap in cJSON (single .c/.h drop-in, MIT license) for the parse side;
 * snprintf emission is already trivial and unlikely to need replacement.
 */

#include "dafsa.h"
#include "dafsa_build.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ─── Constants ─────────────────────────────────────────────────────── */

#define MAX_HANDLES   4096
#define MAX_LINE      65536
#define MAX_KEY_BYTES 4096
#define MAX_PAYLOADS  65536
#define MAX_BATCH_KEYS 4096   /* per batch_add request */

/* ─── Handle table ──────────────────────────────────────────────────── */

typedef enum { H_DAFSA, H_VIEW, H_WAL } hkind;

typedef struct {
    hkind kind;
    void *ptr;
} hslot;

static hslot g_tab[MAX_HANDLES];

static uint32_t h_alloc(hkind kind, void *ptr)
{
    for (uint32_t i = 1; i < MAX_HANDLES; i++) {
        if (g_tab[i].ptr == NULL) {
            g_tab[i].kind = kind;
            g_tab[i].ptr  = ptr;
            return i;
        }
    }
    return 0; /* table full */
}

static hslot *h_get(uint32_t id)
{
    if (id == 0 || id >= MAX_HANDLES || g_tab[id].ptr == NULL)
        return NULL;
    return &g_tab[id];
}

static void h_release(uint32_t id)
{
    if (id == 0 || id >= MAX_HANDLES) return;
    hslot *s = &g_tab[id];
    if (s->ptr == NULL) return;
    switch (s->kind) {
    case H_DAFSA: dafsa_free((dafsa *)s->ptr); break;
    case H_VIEW:  dafsa_view_close((dafsa_view *)s->ptr); break;
    case H_WAL:   dafsa_wal_close((dafsa_wal *)s->ptr); break;
    }
    s->ptr = NULL;
}

/* ─── Base64 codec ──────────────────────────────────────────────────── */

static const char g_b64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* Decode base64 src → dst.  Returns decoded length, or -1 on error. */
static int b64_decode(const char *src, unsigned char *dst, size_t dst_len)
{
    size_t slen = strlen(src);
    if (slen % 4 != 0) return -1;
    size_t olen = 0;
    for (size_t i = 0; i < slen; i += 4) {
        uint32_t v = 0;
        int      pads = 0;
        for (int j = 0; j < 4; j++) {
            char c = src[i + j];
            if (c == '=') { pads++; v <<= 6; continue; }
            const char *p = strchr(g_b64, c);
            if (!p) return -1;
            v = (v << 6) | (uint32_t)(p - g_b64);
        }
        int out_bytes = 3 - pads;
        if (olen + (size_t)out_bytes > dst_len) return -1;
        for (int j = 0; j < out_bytes; j++) {
            dst[olen++] = (unsigned char)((v >> ((2 - j) * 8)) & 0xFF);
        }
    }
    return (int)olen;
}

/* Encode src → dst (NUL-terminated).  Returns 0 on success, -1 if dst
   too small (needs at most 4 * ceil(srclen/3) + 1 bytes). */
static int b64_encode(const unsigned char *src, size_t srclen,
                      char *dst, size_t dst_len)
{
    size_t opos = 0;
    for (size_t i = 0; i < srclen; i += 3) {
        uint32_t v = 0;
        int      bytes = (int)(srclen - i) < 3 ? (int)(srclen - i) : 3;
        for (int j = 0; j < 3; j++) {
            v <<= 8;
            if (i + (size_t)j < srclen) v |= src[i + j];
        }
        if (opos + 4 >= dst_len) return -1;
        dst[opos++] = g_b64[(v >> 18) & 0x3F];
        dst[opos++] = g_b64[(v >> 12) & 0x3F];
        dst[opos++] = (bytes >= 2) ? g_b64[(v >> 6) & 0x3F] : '=';
        dst[opos++] = (bytes == 3) ? g_b64[v & 0x3F]        : '=';
    }
    dst[opos] = '\0';
    return 0;
}

/* ─── JSON tokenizer (hand-rolled, trusted single client) ───────────── */

typedef struct {
    uint32_t id;
    char     op[32];
    uint32_t h;
    uint32_t wal_h;
    uint32_t dafsa_h;
    char     key_b64[MAX_KEY_BYTES * 2 + 4];
    char     prefix_b64[MAX_KEY_BYTES * 2 + 4];
    char     path[4096];
    char     wal_path[4096];
    int      readonly;
    char     mode[8];
    /* batch keys (JSON array of base64 strings) — heap-allocated per key so
     * the struct stays small and the per-request memset is cheap.  Freed in
     * main() after each request. */
    char    *keys[MAX_BATCH_KEYS];
    uint32_t keys_count;
    /* field presence flags */
    int      has_h, has_key, has_prefix, has_path, has_wal_path;
    int      has_readonly, has_mode, has_wal_h, has_dafsa_h, has_keys;
} request;

static char skip_ws(const char **p)
{
    while (**p == ' ' || **p == '\t' || **p == '\n' || **p == '\r') (*p)++;
    return **p;
}

/* Parse a JSON string.  *p must point at the opening '"'.
 * Handles \\, \", \n, \t, \r, \/ escapes.  Returns 0 on success. */
static int parse_json_str(const char **p, char *buf, size_t bufsz)
{
    if (**p != '"') return -1;
    (*p)++;
    size_t pos = 0;
    while (**p && **p != '"') {
        if (**p == '\\') {
            (*p)++;
            switch (**p) {
            case '"':  if (pos < bufsz-1) buf[pos++] = '"';  break;
            case '\\': if (pos < bufsz-1) buf[pos++] = '\\'; break;
            case 'n':  if (pos < bufsz-1) buf[pos++] = '\n'; break;
            case 't':  if (pos < bufsz-1) buf[pos++] = '\t'; break;
            case 'r':  if (pos < bufsz-1) buf[pos++] = '\r'; break;
            case '/':  if (pos < bufsz-1) buf[pos++] = '/';  break;
            default:   return -1;
            }
        } else {
            if (pos < bufsz - 1) buf[pos++] = **p;
        }
        (*p)++;
    }
    if (**p != '"') return -1;
    (*p)++;
    buf[pos] = '\0';
    return 0;
}

/* Parse a JSON array of strings into keys[] (heap-allocated).  *p must point
 * at '['.  Fills req->keys_count and returns 0 on success.  Caller frees each
 * req->keys[i] after handling the request. */
static int parse_json_str_array(const char **p, request *req)
{
    if (**p != '[') return -1;
    (*p)++;
    req->keys_count = 0;
    skip_ws(p);
    if (**p == ']') { (*p)++; return 0; } /* empty array */
    for (;;) {
        if (**p != '"') return -1;
        if (req->keys_count >= MAX_BATCH_KEYS) return -1;
        char scratch[MAX_KEY_BYTES * 2 + 4];
        if (parse_json_str(p, scratch, sizeof(scratch)) != 0)
            return -1;
        char *copy = strdup(scratch);
        if (copy == NULL) return -1;
        req->keys[req->keys_count++] = copy;
        skip_ws(p);
        if (**p == ']') { (*p)++; return 0; }
        if (**p != ',') return -1;
        (*p)++;
        skip_ws(p);
    }
}

/* Parse a JSON unsigned integer.  *p must point at the first digit. */
static int parse_json_uint(const char **p, uint32_t *val)
{
    if (**p < '0' || **p > '9') return -1;
    uint32_t v = 0;
    while (**p >= '0' && **p <= '9') {
        uint32_t nv = v * 10 + (uint32_t)(**p - '0');
        if (nv < v) return -1;
        v = nv;
        (*p)++;
    }
    *val = v;
    return 0;
}

/* Parse a JSON boolean.  Returns 0/1, or -1 on error. */
static int parse_json_bool(const char **p)
{
    if (strncmp(*p, "true", 4) == 0) { *p += 4; return 1; }
    if (strncmp(*p, "false", 5) == 0) { *p += 5; return 0; }
    return -1;
}

/* Skip a JSON value (used for unknown keys). */
static void skip_json_value(const char **p)
{
    char c = skip_ws(p);
    if (c == '"') {
        char dummy[2];
        parse_json_str(p, dummy, sizeof(dummy));
    } else if (c == '-' || (c >= '0' && c <= '9')) {
        if (c == '-') (*p)++;
        uint32_t dummy;
        parse_json_uint(p, &dummy);
    } else if (c == 't' || c == 'f') {
        parse_json_bool(p);
    } else if (c == 'n') {
        if (strncmp(*p, "null", 4) == 0) *p += 4;
    }
}

/* Lightweight extractor for the request's "id" field, used to reply with a
 * correct id even when the full parse fails (so Python doesn't mistake a
 * parse error for a crash/restart).  Returns 0 if not found. */
static uint32_t extract_request_id(const char *line)
{
    const char *p = line;
    while (*p) {
        if (skip_ws(&p) != '"') { p++; continue; }
        char key[32];
        if (parse_json_str(&p, key, sizeof(key)) != 0) return 0;
        if (skip_ws(&p) != ':') { p++; continue; }
        p++;
        skip_ws(&p);
        if (strcmp(key, "id") == 0) {
            uint32_t v;
            if (parse_json_uint(&p, &v) == 0) return v;
            return 0;
        }
        skip_json_value(&p);
    }
    return 0;
}

static int parse_request(const char *line, request *req)
{
    memset(req, 0, sizeof(*req));
    const char *p = line;

    if (skip_ws(&p) != '{') return -1;
    p++;

    for (;;) {
        char c = skip_ws(&p);
        if (c == '}') { p++; break; }
        if (c != '"') return -1;

        char key[32];
        if (parse_json_str(&p, key, sizeof(key)) != 0) return -1;

        if (skip_ws(&p) != ':') return -1;
        p++;

        c = skip_ws(&p);

        if (strcmp(key, "id") == 0) {
            if (parse_json_uint(&p, &req->id) != 0) return -1;
        } else if (strcmp(key, "op") == 0) {
            if (parse_json_str(&p, req->op, sizeof(req->op)) != 0) return -1;
        } else if (strcmp(key, "h") == 0) {
            if (parse_json_uint(&p, &req->h) != 0) return -1;
            req->has_h = 1;
        } else if (strcmp(key, "wal") == 0) {
            if (parse_json_uint(&p, &req->wal_h) != 0) return -1;
            req->has_wal_h = 1;
        } else if (strcmp(key, "dafsa") == 0) {
            if (parse_json_uint(&p, &req->dafsa_h) != 0) return -1;
            req->has_dafsa_h = 1;
        } else if (strcmp(key, "key") == 0) {
            if (parse_json_str(&p, req->key_b64, sizeof(req->key_b64)) != 0)
                return -1;
            req->has_key = 1;
        } else if (strcmp(key, "prefix") == 0) {
            if (parse_json_str(&p, req->prefix_b64, sizeof(req->prefix_b64)) != 0)
                return -1;
            req->has_prefix = 1;
        } else if (strcmp(key, "keys") == 0) {
            if (parse_json_str_array(&p, req) != 0) return -1;
            req->has_keys = 1;
        } else if (strcmp(key, "path") == 0) {
            if (parse_json_str(&p, req->path, sizeof(req->path)) != 0)
                return -1;
            req->has_path = 1;
        } else if (strcmp(key, "wal_path") == 0) {
            if (parse_json_str(&p, req->wal_path, sizeof(req->wal_path)) != 0)
                return -1;
            req->has_wal_path = 1;
        } else if (strcmp(key, "readonly") == 0) {
            int v = parse_json_bool(&p);
            if (v < 0) return -1;
            req->readonly = v;
            req->has_readonly = 1;
        } else if (strcmp(key, "mode") == 0) {
            if (parse_json_str(&p, req->mode, sizeof(req->mode)) != 0)
                return -1;
            req->has_mode = 1;
        } else {
            skip_json_value(&p);
        }

        c = skip_ws(&p);
        if (c == '}') { p++; break; }
        if (c != ',') return -1;
        p++;
    }

    return 0;
}

/* ─── Prefix-enum payload collector (daemon-local, single-threaded) ──── */

#define MAX_PAYLOAD_LEN 256

static unsigned char g_payloads[MAX_PAYLOADS * MAX_PAYLOAD_LEN];
static uint32_t       g_payload_lens[MAX_PAYLOADS];
static size_t         g_payload_count;

static int enum_collector(const unsigned char *payload, size_t payload_len,
                          void *user)
{
    (void)user;
    if (payload_len > MAX_PAYLOAD_LEN) return 0;
    if (g_payload_count >= MAX_PAYLOADS) return 1; /* stop */
    memcpy(&g_payloads[g_payload_count * MAX_PAYLOAD_LEN], payload,
           payload_len);
    g_payload_lens[g_payload_count] = (uint32_t)payload_len;
    g_payload_count++;
    return 0;
}

/* ─── WAL replay closure ────────────────────────────────────────────── */

typedef struct {
    dafsa *target;
    int    err;
    long   count;
} wal_replay_ctx;

static int wal_replay_cb(uint8_t op, const unsigned char *key,
                         uint32_t key_len, void *user)
{
    wal_replay_ctx *ctx = (wal_replay_ctx *)user;
    int rc;
    if (op == DAFSA_WAL_OP_ADD) {
        rc = dafsa_add_n(ctx->target, key, key_len);
    } else if (op == DAFSA_WAL_OP_DEL) {
        rc = dafsa_delete_n(ctx->target, key, key_len);
    } else {
        ctx->err = -1;
        return -1;
    }
    if (rc < 0) {
        ctx->err = -1;
        return -1;
    }
    ctx->count++;
    return 0;
}

/* ─── JSON reply helpers (all flush immediately) ────────────────────── */

static void reply_ok(uint32_t id)
{
    printf("{\"id\":%u,\"ok\":true}\n", id);
    fflush(stdout);
}

static void reply_ok_h(uint32_t id, uint32_t h)
{
    printf("{\"id\":%u,\"ok\":true,\"h\":%u}\n", id, h);
    fflush(stdout);
}

static void reply_ok_rc(uint32_t id, int rc)
{
    printf("{\"id\":%u,\"ok\":true,\"rc\":%d}\n", id, rc);
    fflush(stdout);
}

static void reply_ok_size(uint32_t id, uint64_t size)
{
    printf("{\"id\":%u,\"ok\":true,\"size\":%llu}\n",
           id, (unsigned long long)size);
    fflush(stdout);
}

static void reply_ok_count(uint32_t id, long count)
{
    printf("{\"id\":%u,\"ok\":true,\"count\":%ld}\n", id, count);
    fflush(stdout);
}

static void reply_ok_handshake(uint32_t id, uint32_t abi)
{
    printf("{\"id\":%u,\"ok\":true,\"abi\":%u,\"proto\":1}\n", id, abi);
    fflush(stdout);
}

static void reply_ok_prefix_enum(uint32_t id, long n)
{
    /* The C core (enum_dfs) increments the count BEFORE invoking the
     * collector callback, so `n` can exceed the number of payloads actually
     * collected (enum_collector skips >MAX_PAYLOAD_LEN entries and stops at
     * MAX_PAYLOADS — both after count was already bumped).  Clamp to what we
     * actually hold so the reply loop never reads past g_payload_lens[] /
     * g_payloads[] (which would be an out-of-bounds read). */
    if (n > (long)g_payload_count) n = (long)g_payload_count;
    printf("{\"id\":%u,\"ok\":true,\"n\":%ld,\"payloads\":[", id, n);
    char b64[((MAX_PAYLOAD_LEN + 2) / 3) * 4 + 4];
    for (long i = 0; i < n; i++) {
        if (i > 0) putchar(',');
        b64_encode(&g_payloads[i * MAX_PAYLOAD_LEN],
                   g_payload_lens[i], b64, sizeof(b64));
        printf("\"%s\"", b64);
    }
    printf("]}\n");
    fflush(stdout);
}

static void reply_ok_stats(uint32_t id, const dafsa_stats_out *st)
{
    printf("{\"id\":%u,\"ok\":true,\"stats\":{"
           "\"n_states_total\":%u,"
           "\"n_states_reachable\":%u,"
           "\"n_final\":%u,"
           "\"n_trans\":%u,"
           "\"register_probes\":%llu"
           "}}\n",
           id,
           st->n_states_total,
           st->n_states_reachable,
           st->n_final,
           st->n_trans,
           (unsigned long long)st->register_probes);
    fflush(stdout);
}

static void reply_err(uint32_t id, const char *err, const char *code)
{
    printf("{\"id\":%u,\"ok\":false,\"err\":\"%s\",\"code\":\"%s\"}\n",
           id, err, code);
    fflush(stdout);
}

/* ─── Dispatch ──────────────────────────────────────────────────────── */

/* Returns 0 normally, 1 to signal the caller to exit (shutdown). */
static int handle_request(const request *req)
{
    uint32_t     id = req->id;
    const char  *op = req->op;
    hslot       *s;
    int          rc;
    unsigned char key_buf[MAX_KEY_BYTES];

    /* ── handshake ── */
    if (strcmp(op, "handshake") == 0) {
        reply_ok_handshake(id, dafsa_abi_version());
        return 0;
    }

    /* ── shutdown ── */
    if (strcmp(op, "shutdown") == 0) {
        reply_ok(id);
        return 1;
    }

    /* ── create ── */
    if (strcmp(op, "create") == 0) {
        dafsa *d = dafsa_create();
        if (!d) { reply_err(id, "dafsa_create returned NULL", "EOOM"); return 0; }
        uint32_t h = h_alloc(H_DAFSA, d);
        if (!h) { dafsa_free(d); reply_err(id, "handle table full", "EFULL"); return 0; }
        reply_ok_h(id, h);
        return 0;
    }

    /* ── load ── */
    if (strcmp(op, "load") == 0) {
        if (!req->has_path) {
            reply_err(id, "missing path", "EBADREQ"); return 0;
        }
        if (req->readonly) {
            if (req->has_wal_path && req->wal_path[0]) {
                dafsa_view *v = dafsa_view_open_layered(req->path,
                                                         req->wal_path);
                if (!v) {
                    reply_err(id, "could not open layered DAFSA view",
                              "ELOAD");
                    return 0;
                }
                uint32_t h = h_alloc(H_VIEW, v);
                if (!h) {
                    dafsa_view_close(v);
                    reply_err(id, "handle table full", "EFULL");
                    return 0;
                }
                reply_ok_h(id, h);
            } else {
                dafsa_view *v = dafsa_view_open(req->path);
                if (!v) {
                    reply_err(id, "could not open DAFSA view", "ELOAD");
                    return 0;
                }
                uint32_t h = h_alloc(H_VIEW, v);
                if (!h) {
                    dafsa_view_close(v);
                    reply_err(id, "handle table full", "EFULL");
                    return 0;
                }
                reply_ok_h(id, h);
            }
        } else {
            dafsa *d = dafsa_load(req->path);
            if (!d) {
                reply_err(id, "could not load DAFSA", "ELOAD");
                return 0;
            }
            uint32_t h = h_alloc(H_DAFSA, d);
            if (!h) {
                dafsa_free(d);
                reply_err(id, "handle table full", "EFULL");
                return 0;
            }
            reply_ok_h(id, h);
        }
        return 0;
    }

    /* ── free ── */
    if (strcmp(op, "free") == 0) {
        if (!req->has_h || req->h == 0) {
            reply_err(id, "invalid handle", "EBADH"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        h_release(req->h);
        reply_ok(id);
        return 0;
    }

    /* ── add ── */
    if (strcmp(op, "add") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (s->kind != H_DAFSA) {
            reply_err(id, "Cannot mutate a read-only DafsaView", "EBADH");
            return 0;
        }
        if (!req->has_key) {
            reply_err(id, "missing key", "EBADREQ"); return 0;
        }
        int key_len = b64_decode(req->key_b64, key_buf, sizeof(key_buf));
        if (key_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        rc = dafsa_add_n((dafsa *)s->ptr, key_buf, (size_t)key_len);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── batch_add ── */
    if (strcmp(op, "batch_add") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (s->kind != H_DAFSA) {
            reply_err(id, "Cannot mutate a read-only DafsaView", "EBADH");
            return 0;
        }
        if (!req->has_keys) {
            reply_err(id, "missing keys", "EBADREQ"); return 0;
        }
        uint32_t added = 0, dups = 0, errors = 0;
        for (uint32_t i = 0; i < req->keys_count; i++) {
            int klen = b64_decode(req->keys[i], key_buf,
                                  sizeof(key_buf));
            if (klen < 0) { errors++; continue; }
            int r = dafsa_add_n((dafsa *)s->ptr, key_buf, (size_t)klen);
            if (r < 0)      errors++;
            else if (r == 1) added++;
            else            dups++;
        }
        printf("{\"id\":%u,\"ok\":true,\"added\":%u,\"dups\":%u,"
               "\"errors\":%u}\n", id, added, dups, errors);
        fflush(stdout);
        return 0;
    }

    /* ── delete ── */
    if (strcmp(op, "delete") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (s->kind != H_DAFSA) {
            reply_err(id, "Cannot mutate a read-only DafsaView", "EBADH");
            return 0;
        }
        if (!req->has_key) {
            reply_err(id, "missing key", "EBADREQ"); return 0;
        }
        int key_len = b64_decode(req->key_b64, key_buf, sizeof(key_buf));
        if (key_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        rc = dafsa_delete_n((dafsa *)s->ptr, key_buf, (size_t)key_len);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── lookup ── */
    if (strcmp(op, "lookup") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (!req->has_key) {
            reply_err(id, "missing key", "EBADREQ"); return 0;
        }
        int key_len = b64_decode(req->key_b64, key_buf, sizeof(key_buf));
        if (key_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        if (s->kind == H_VIEW)
            rc = dafsa_view_lookup_n((dafsa_view *)s->ptr, key_buf,
                                     (size_t)key_len);
        else
            rc = dafsa_lookup_n((dafsa *)s->ptr, key_buf, (size_t)key_len);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── save ── */
    if (strcmp(op, "save") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (s->kind != H_DAFSA) {
            reply_err(id, "Cannot save a read-only DafsaView", "EBADH");
            return 0;
        }
        if (!req->has_path) {
            reply_err(id, "missing path", "EBADREQ"); return 0;
        }
        rc = dafsa_save((dafsa *)s->ptr, req->path);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── prefix_enum ── */
    if (strcmp(op, "prefix_enum") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (!req->has_prefix) {
            reply_err(id, "missing prefix", "EBADREQ"); return 0;
        }
        unsigned char pfx_buf[MAX_KEY_BYTES];
        int pfx_len = b64_decode(req->prefix_b64, pfx_buf, sizeof(pfx_buf));
        if (pfx_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        g_payload_count = 0;
        long n;
        if (s->kind == H_VIEW)
            n = dafsa_view_prefix_enum((dafsa_view *)s->ptr,
                                       pfx_buf, (size_t)pfx_len,
                                       enum_collector, NULL);
        else
            n = dafsa_prefix_enum((dafsa *)s->ptr, pfx_buf, (size_t)pfx_len,
                                  enum_collector, NULL);
        if (n < 0) {
            reply_err(id, "prefix_enum failed", "EENUM"); return 0;
        }
        reply_ok_prefix_enum(id, n);
        return 0;
    }

    /* ── stats ── */
    if (strcmp(op, "stats") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s) { reply_err(id, "invalid handle", "EBADH"); return 0; }
        if (s->kind != H_DAFSA) {
            reply_err(id, "stats not available on a DafsaView", "EBADH");
            return 0;
        }
        dafsa_stats_out st;
        dafsa_stats((dafsa *)s->ptr, &st);
        reply_ok_stats(id, &st);
        return 0;
    }

    /* ── wal_open ── */
    if (strcmp(op, "wal_open") == 0) {
        if (!req->has_path) {
            reply_err(id, "missing path", "EBADREQ"); return 0;
        }
        dafsa_wal *w = NULL;
        if (req->has_mode) {
            if (strcmp(req->mode, "rw") == 0)
                w = dafsa_wal_open_rw(req->path);
            else if (strcmp(req->mode, "ro") == 0)
                w = dafsa_wal_open_ro(req->path);
            else
                w = dafsa_wal_open(req->path);
        } else {
            w = dafsa_wal_open(req->path);
        }
        if (!w) {
            reply_err(id, "dafsa_wal_open returned NULL", "EOPEN");
            return 0;
        }
        uint32_t h = h_alloc(H_WAL, w);
        if (!h) {
            dafsa_wal_close(w);
            reply_err(id, "handle table full", "EFULL");
            return 0;
        }
        reply_ok_h(id, h);
        return 0;
    }

    /* ── wal_append_add ── */
    if (strcmp(op, "wal_append_add") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s || s->kind != H_WAL) {
            reply_err(id, "invalid WAL handle", "EBADH"); return 0;
        }
        if (!req->has_key) {
            reply_err(id, "missing key", "EBADREQ"); return 0;
        }
        int key_len = b64_decode(req->key_b64, key_buf, sizeof(key_buf));
        if (key_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        rc = dafsa_wal_append_add((dafsa_wal *)s->ptr, key_buf,
                                  (uint32_t)key_len);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── wal_append_del ── */
    if (strcmp(op, "wal_append_del") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s || s->kind != H_WAL) {
            reply_err(id, "invalid WAL handle", "EBADH"); return 0;
        }
        if (!req->has_key) {
            reply_err(id, "missing key", "EBADREQ"); return 0;
        }
        int key_len = b64_decode(req->key_b64, key_buf, sizeof(key_buf));
        if (key_len < 0) {
            reply_err(id, "base64 decode failed", "EBADREQ"); return 0;
        }
        rc = dafsa_wal_append_del((dafsa_wal *)s->ptr, key_buf,
                                  (uint32_t)key_len);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── wal_sync ── */
    if (strcmp(op, "wal_sync") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s || s->kind != H_WAL) {
            reply_err(id, "invalid WAL handle", "EBADH"); return 0;
        }
        rc = dafsa_wal_sync((dafsa_wal *)s->ptr);
        reply_ok_rc(id, rc);
        return 0;
    }

    /* ── wal_size ── */
    if (strcmp(op, "wal_size") == 0) {
        if (!req->has_h) {
            reply_err(id, "missing h", "EBADREQ"); return 0;
        }
        s = h_get(req->h);
        if (!s || s->kind != H_WAL) {
            reply_err(id, "invalid WAL handle", "EBADH"); return 0;
        }
        uint64_t sz = dafsa_wal_size((dafsa_wal *)s->ptr);
        reply_ok_size(id, sz);
        return 0;
    }

    /* ── wal_replay ── */
    if (strcmp(op, "wal_replay") == 0) {
        if (!req->has_wal_h || !req->has_dafsa_h) {
            reply_err(id, "missing wal or dafsa handle", "EBADREQ");
            return 0;
        }
        hslot *ws = h_get(req->wal_h);
        hslot *ds = h_get(req->dafsa_h);
        if (!ws || ws->kind != H_WAL) {
            reply_err(id, "invalid WAL handle", "EBADH"); return 0;
        }
        if (!ds || ds->kind != H_DAFSA) {
            reply_err(id, "invalid DAFSA handle", "EBADH"); return 0;
        }
        wal_replay_ctx ctx = {(dafsa *)ds->ptr, 0, 0};
        rc = dafsa_wal_replay((dafsa_wal *)ws->ptr, wal_replay_cb, &ctx);
        if (rc != 0 || ctx.err != 0) {
            reply_err(id, "wal_replay failed", "EREPLAY");
            return 0;
        }
        reply_ok_count(id, ctx.count);
        return 0;
    }

    /* ── unknown op ── */
    reply_err(id, "unknown operation", "EBADOP");
    return 0;
}

/* ─── Debug abort handler (activated by env var for crash-isolation tests) ── */

static int handle_debug_abort(const request *req)
{
    const char *env = getenv("JING_DAFSA_DAEMON_DEBUG_ABORT");
    if (!env || strcmp(env, "1") != 0) return 0;
    if (strcmp(req->op, "debug_abort") != 0) return 0;
    /* Simulate a crash: reply ok then abort(), so the client gets a clean
       reply for this op and a dead daemon for the next one. */
    reply_ok(req->id);
    fflush(stdout);
    abort();
    return 1; /* unreachable */
}

/* Free heap-allocated batch keys after handling a request. */
static void request_free_keys(request *req)
{
    for (uint32_t i = 0; i < req->keys_count; i++) {
        free(req->keys[i]);
        req->keys[i] = NULL;
    }
    req->keys_count = 0;
}

/* ─── main ──────────────────────────────────────────────────────────── */

int main(int argc, char **argv)
{
    /* ── build subcommand: one-shot C-only index build ──────────────── */
    if (argc >= 2 && strcmp(argv[1], "build") == 0)
        return build_main(argc - 1, argv + 1);

    /* ── Stdio daemon (default) ──────────────────────────────────────── */
    /* Line-buffer stdout so Python readline() never deadlocks. */
    setvbuf(stdout, NULL, _IOLBF, 0);

    char  *line   = NULL;
    size_t linesz = 0;

    /* The request struct carries a batch-key buffer sized for the worst-case
     * line (MAX_LINE).  Allocate it on the heap, not the stack, so it cannot
     * overflow the daemon's stack. */
    request *req = (request *)malloc(sizeof(*req));
    if (req == NULL) {
        fprintf(stderr, "dafsa-cli: out of memory allocating request\n");
        return 1;
    }

    while (1) {
        errno = 0;
        ssize_t n = getline(&line, &linesz, stdin);
        if (n < 0) {
            /* EOF or read error — exit quietly. */
            break;
        }

        /* Strip trailing newline (getline includes it). */
        if (n > 0 && line[n - 1] == '\n') line[n - 1] = '\0';

        if (parse_request(line, req) != 0) {
            /* Free any partially-parsed batch keys (parse_json_str_array
             * strdups as it goes; a mid-array failure leaks them otherwise). */
            request_free_keys(req);
            /* Reply with the request's real id (if extractable) so Python
             * doesn't mistake a parse error for a daemon crash/restart. */
            reply_err(extract_request_id(line), "JSON parse error", "EPARSE");
            continue;
        }

        if (handle_debug_abort(req)) break; /* aborted */

        if (handle_request(req) != 0)
            break; /* shutdown */

        request_free_keys(req);
    }

    free(req);
    free(line);
    return 0;
}
