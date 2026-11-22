# Blast Radius entry

For each patch, `selector.py`:

1. Rebuilds every changed file from the pinned sqlglot source in `src.tar.gz` and the diff.
2. Compares the old and new Python AST, so comment- and formatting-only edits select nothing.
   `x is None` / `x == None` and `is`/`==` against ENUM.MEMBER count as the same code.
3. For each function, class or module statement that really changed, picks the tests that
   executed it at the pinned commit. `covmap.json.gz` comes from running every test in its own
   forked process under a `sys.monitoring` line tracer, so import-time code, caches and
   `setUpClass` are credited to every test that needs them; only lines that run when code is called
   count for whole-function/class fallbacks (def lines and class bodies run on import).
   - changes inside a function use statement-level coverage;
   - a lone comparison-operator swap drops tests that evaluated that comparison and never saw the
     old and new operator disagree (`cmpmap.json.gz`);
   - an edit to entries of a class-level dict literal (FUNCTIONS, *_PARSERS, TYPE_MAPPING, ...) selects
     the tests that looked up a changed key or read the whole dict (`dkmap.json.gz`, recorded with
     wrapping dicts in a fork-per-test run); added keys select every test that used the dict; dicts
     that class-building code compiles into other structures are excluded;
   - a plain class-level setting (`NAME = value`) selects the tests whose reads of NAME through an
     instance resolved to that class's definition (`armap.json.gz`, recorded with a
     `__getattribute__` hook in a fork-per-test run); settings also read through class objects or
     derived into other settings (`names.json`, scanned from the pinned source) are excluded;
   - other class-level settings select every test that created that class (or a subclass);
   - code that builds classes while modules import (metaclass, `__init_subclass__` and their
     helpers) selects every test.
4. If import-time code changed (beyond literal values in non-baked settings), it test-imports a patched scratch copy (every module and dialect for
   core modules, otherwise the changed module and its dialect); if anything breaks, it selects every
   test.
5. Fixture files map to the tests that opened them. The TPC-H/TPC-DS files that
   `TestExecutor.setUpClass` loads map to the executor tests that use them (query inputs vs expected
   outputs are told apart by parsing both versions like the tests do).
6. Expensive tests are dropped only when a check on the patched code shows they behave as before:
   - `test_execute_tpcds` (21.5 s): `tpcds_cov.json.gz` records which lines each TPC-DS entry
     reaches and which run in shared setup or in planning/execution. The selector replays the
     affected entries on the patched code (`tpcds_harness.py`) and compares digests with the
     unpatched ones computed during the Docker build: the early stages only (parse, pickle round trip,
     optimizer output and types, DuckDB transpile) when nothing changed in planning/execution,
     otherwise full execution for up to 30 entries. Class-level edits in dialect-specific modules are
     checked with the early stages of every entry.
   - `test_max_nodes` (28.8 s): every column of its giant query is identical, so a scaled replica
     (`maxnodes_harness.py`) runs the same calls in the same order; dropped when the replica's digest
     is unchanged and the changed lines are all reached by the replica (`maxnodes_cov.json`).
   - `test_generator_memory`: kept only for generator-related changes, and then its own loop runs on
     the patched code for the affected dialects only (`genmem_harness.py`).
7. Splits the chosen tests across 1–8 workers, picking the worker count with the lowest cost.

## Pre-existing code and data
- `src.tar.gz`: sqlglot's `.py` files, test helpers and the TPC-H/TPC-DS fixtures (queries and TPC-DS
  data) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` (MIT License, https://github.com/tobymao/sqlglot).
- All maps were built from the pinned commit only, by running sqlglot's own test suite (fork per
  test, a shared-process coverage run, an instrumented copy for comparison outcomes, and wrapped
  class-level dicts for key lookups). The harnesses reproduce what the corresponding tests do.

License: MIT.
