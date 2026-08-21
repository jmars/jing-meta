# Makefile for the jing-meta DAFSA indexer CLI
# Core engine objects come from the vendor/dafsa submodule (fixpoint-linux/dafsa).
# dafsa_build.o is the LOCAL dafsa_build.c (build_main JSONL CLI driver), NOT the
# submodule's dafsa_build_sorted — keep this explicit rule so the submodule's
# dafsa_build.c is never compiled here.

ENGINE  = vendor/dafsa
CFLAGS  = -O2 -Wall -Wextra -Werror -I. -I$(ENGINE) -fPIC

CORE_OBJS = dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o dafsa_crc32.o dafsa_wal.o
OBJS = $(CORE_OBJS) dafsa_build.o

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

# Compile the 7 core engine objects from the submodule into LOCAL .o files
# (keeps the submodule pristine).
$(CORE_OBJS): %.o: $(ENGINE)/%.c $(ENGINE)/dafsa.h $(ENGINE)/dafsa_internal.h
	$(CC) $(CFLAGS) -c -o $@ $<

# build_main lives in the LOCAL dafsa_build.c (jing's indexer CLI driver).
dafsa_build.o: dafsa_build.c dafsa_build.h $(ENGINE)/dafsa.h $(ENGINE)/dafsa_internal.h
	$(CC) $(CFLAGS) -c -o $@ $<

# ─── DAFSA CLI daemon (libc-agnostic static binary) ───────────────────

daemon: daemon-cosmo

daemon-musl: dafsa_cli.c $(OBJS)
	$(CC) -static -O2 -Wall -Wextra -Werror -I. -I$(ENGINE) -o dafsa-cli dafsa_cli.c $(OBJS)
	@if ! file dafsa-cli | grep -q 'ELF .*statically linked'; then \
		echo "FATAL: dafsa-cli is not a statically-linked ELF"; \
		file dafsa-cli; exit 1; \
	fi
	@if readelf -d dafsa-cli 2>/dev/null | grep -q NEEDED; then \
		echo "FATAL: dafsa-cli has dynamic NEEDED entries"; exit 1; \
	fi

daemon-cosmo: dafsa_cli.c dafsa_build.c $(CORE_OBJS)
	cosmocc -O2 -Wall -Wextra -Werror -I. -I$(ENGINE) -o dafsa-cli dafsa_cli.c dafsa_build.c $(CORE_OBJS)
	sh ./dafsa-cli --assimilate --help < /dev/null >/dev/null 2>&1 || { \
		echo "FATAL: dafsa-cli --assimilate failed"; exit 1; }
	@if ! file dafsa-cli | grep -q 'ELF .*statically linked'; then \
		echo "FATAL: dafsa-cli is not a statically-linked ELF:"; \
		file dafsa-cli; exit 1; \
	fi
	@if readelf -d dafsa-cli 2>/dev/null | grep -q NEEDED; then \
		echo "FATAL: dafsa-cli has dynamic NEEDED entries"; exit 1; \
	fi

debug: CFLAGS += -DDAFSA_DEBUG
debug: _t

asan: CFLAGS += -fsanitize=address,undefined -g
asan: _t

clean:
	rm -f _t _td $(OBJS) dafsa.dot libdafsa.so libdafsa-musl.so dafsa-cli dafsa-cli.com.dbg dafsa-cli.aarch64.elf

.PHONY: all debug asan musl clean daemon daemon-musl daemon-cosmo
