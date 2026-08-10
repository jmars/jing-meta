# Makefile for the Carrasco & Forcada incremental minimal DAFSA

OBJS = dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o dafsa_crc32.o dafsa_wal.o dafsa_build.o
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

# ─── DAFSA CLI daemon (libc-agnostic static binary) ───────────────────

# Default daemon target: Cosmopolitan build, assimilated to a native ELF.
# cosmocc produces an APE polyglot (dafsa-cli) + native ELFs (.com.dbg =
# x86-64, .aarch64.elf). `dafsa-cli --assimilate` rewrites the APE in place
# into a plain statically-linked x86-64 ELF with no PT_INTERP / DT_NEEDED,
# so it runs under ANY Linux libc (host glibc CPython AND sandbox musl
# CPython) with no APE loader required. --help terminates the program after
# assimilating (otherwise the daemon would block reading stdin).
#
# cosmocc builds a FAT x86_64+aarch64 APE, so every .o it links needs an
# .aarch64/ counterpart.  The 7 core .o files have prebuilt .aarch64/
# counterparts; dafsa_build.c is passed as a SOURCE (not .o) so cosmocc
# compiles both arch objects itself — no .aarch64/dafsa_build.o needed.
daemon: daemon-cosmo

daemon-musl: dafsa_cli.c $(OBJS)
	$(CC) -static -O2 -Wall -Wextra -Werror -I. -o dafsa-cli dafsa_cli.c $(OBJS)
	@if ! file dafsa-cli | grep -q 'ELF .*statically linked'; then \
		echo "FATAL: dafsa-cli is not a statically-linked ELF"; \
		file dafsa-cli; exit 1; \
	fi
	@if readelf -d dafsa-cli 2>/dev/null | grep -q NEEDED; then \
		echo "FATAL: dafsa-cli has dynamic NEEDED entries"; exit 1; \
	fi

# The 7 core objects have prebuilt .aarch64/ counterparts; dafsa_build.c is a
# source so cosmocc compiles both archs itself (avoids the aarch64 dance).
CORE_OBJS = dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o dafsa_crc32.o dafsa_wal.o

daemon-cosmo: dafsa_cli.c dafsa_build.c $(CORE_OBJS)
	cosmocc -O2 -Wall -Wextra -Werror -I. -o dafsa-cli dafsa_cli.c dafsa_build.c $(CORE_OBJS)
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
