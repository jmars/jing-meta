# Makefile for the Carrasco & Forcada incremental minimal DAFSA

OBJS = dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o dafsa_crc32.o dafsa_wal.o
CFLAGS = -O2 -Wall -Wextra -Werror -I. -fPIC

all: _t libdafsa.so

_t: dafsa_test.c $(OBJS)
	$(CC) $(CFLAGS) -o $@ dafsa_test.c $(OBJS)

# Host (glibc) shared lib — links libc.so.6. Build ONLY on the host.
libdafsa.so: $(OBJS)
	$(CC) -shared -fPIC $(CFLAGS) -o $@ $(OBJS)

# Sandbox / musl build — emits a DISTINCT filename so a musl build can never
# clobber the host libdafsa.so (musl libc.so is loader/ABI-incompatible with
# host glibc). In-sandbox tests load libdafsa-musl.so via the loader's
# fallback (see indexer/dafsa/__init__.py _load()).
musl: CFLAGS += -DMUSL_BUILD
musl: libdafsa-musl.so

libdafsa-musl.so: $(OBJS)
	$(CC) -shared -fPIC $(CFLAGS) -o $@ $(OBJS)

debug: CFLAGS += -DDAFSA_DEBUG
debug: _t

asan: CFLAGS += -fsanitize=address,undefined -g
asan: _t

clean:
	rm -f _t _td $(OBJS) dafsa.dot libdafsa.so libdafsa-musl.so

.PHONY: all debug asan musl clean
