# Blast Radius entry

For each patch, `selector.py`:

1. Rebuilds every changed file from the pinned sqlglot source in `src.tar.gz` and the diff.
2. Compares the old and new Python AST, so comment- and formatting-only edits select nothing.
   `x is None` / `x == None` and `is`/`==` against ENUM.MEMBER count as the same code.
3. For each function, class or module statement that really changed, picks the tests that
   executed it at the pinned commit (`covmap.json.gz`, a per-test line coverage map).
   Changes inside a function use statement-level coverage; class-level settings select every test
   that created that class (or a subclass), recorded while building the map.
4. Fixture files (`tests/fixtures/...`) map to the tests that opened them.
5. Splits the chosen tests across 1–8 workers, picking the worker count with the lowest cost.

## Pre-existing code and data
- `src.tar.gz`: sqlglot's `.py` files at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` (MIT License, https://github.com/tobymao/sqlglot).
- `Makefile`, `score.py`, `sample/`: from the official Blast Radius starter kit.
- `covmap.json.gz`: built from the pinned commit only, by running sqlglot's tests with
  `pytest-cov --cov-context=test` plus `tools/fixplug.py` (records fixture files each test opens
  and runs the tests' process pools in-process so coverage sees them, and records which sqlglot
  classes each test instantiates), then `tools/build_map.py`.

## Test locally
```sh
make sample
```

License: MIT.
