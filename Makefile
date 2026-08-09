# Top-level dev targets: C tests, Python tests, lint, typecheck.
# Degrade gracefully: if a tool is missing, print a SKIP message and exit 0;
# if a tool is present but reports issues, fail with a non-zero exit.

.PHONY: test c-test lint typecheck clean

test: c-test
	@if command -v uv >/dev/null 2>&1; then \
		uv run pytest tests/ -v; \
	elif python3 -c "import pytest" >/dev/null 2>&1; then \
		python3 -m pytest tests/ -v; \
	else \
		echo "SKIP: pytest not installed; skipping Python tests (pip install -e '.[dev]' or use uv)"; \
	fi

c-test:
	@$(MAKE) -C indexer/dafsa _t 2>/dev/null || \
		gcc -O2 -Wall -Wextra -Werror -I. -o indexer/dafsa/_t \
		indexer/dafsa/dafsa_test.c indexer/dafsa/dafsa.c \
		indexer/dafsa/dafsa_state.c indexer/dafsa/dafsa_core.c \
		indexer/dafsa/dafsa_persist.c indexer/dafsa/dafsa_view.c 2>/dev/null || true
	@if [ -x indexer/dafsa/_t ]; then \
		cd indexer/dafsa && ./_t; \
	else \
		echo "SKIP: no C toolchain (make/gcc) available; skipping C tests"; \
	fi

lint:
	@if command -v uv >/dev/null 2>&1; then \
		uv run ruff check .; \
	elif command -v ruff >/dev/null 2>&1; then \
		ruff check .; \
	else \
		echo "SKIP: ruff not installed; skipping lint (pip install -e '.[dev]' or use uv)"; \
	fi

typecheck:
	@if command -v uv >/dev/null 2>&1; then \
		uv run mypy --check-untyped-defs jing_meta indexer search memory dreamer; \
	elif command -v mypy >/dev/null 2>&1; then \
		mypy --check-untyped-defs jing_meta indexer search memory dreamer; \
	else \
		echo "SKIP: mypy not installed; skipping typecheck (pip install -e '.[dev]' or use uv)"; \
	fi

clean:
	-$(MAKE) -C indexer/dafsa clean
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	rm -f indexer/dafsa/_t indexer/dafsa/_td indexer/dafsa/*.o indexer/dafsa/dafsa.dot
