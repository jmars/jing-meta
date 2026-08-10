/*
 * dafsa_build.h — One-shot JSONL index build subcommand for dafsa-cli
 *
 * Replaces the per-key Python roundtrip in rebuilds with a one-shot C
 * subcommand: file walk, JSONL extraction, ASCII tokenization,
 * composite-key build+dedup+sort, DAFSA build+save, sidecar + manifest
 * writing — byte-for-byte compatible with the Python _build_locked path
 * (JSONL extractor, ASCII corpora).
 *
 * Public entry point:
 *   int build_main(int argc, char **argv);
 *
 * Called from dafsa_cli.c main() when argv[1] == "build".
 */

#ifndef DAFSA_BUILD_H
#define DAFSA_BUILD_H

int build_main(int argc, char **argv);

#endif /* DAFSA_BUILD_H */
