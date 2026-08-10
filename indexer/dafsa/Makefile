# Makefile for the Carrasco & Forcada incremental minimal DAFSA

OBJS = dafsa.o dafsa_state.o dafsa_core.o dafsa_persist.o dafsa_view.o dafsa_crc32.o dafsa_wal.o
CFLAGS = -O2 -Wall -Wextra -Werror -I. -fPIC

all: _t libdafsa.so

_t: dafsa_test.c $(OBJS)
	$(CC) $(CFLAGS) -o $@ dafsa_test.c $(OBJS)

libdafsa.so: $(OBJS)
	$(CC) -shared -fPIC $(CFLAGS) -o $@ $(OBJS)

debug: CFLAGS += -DDAFSA_DEBUG
debug: _t

asan: CFLAGS += -fsanitize=address,undefined -g
asan: _t

clean:
	rm -f _t _td $(OBJS) dafsa.dot libdafsa.so

.PHONY: all debug asan clean
