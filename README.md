# Blast Radius entry

For each patch, `selector.py`:

1. Rebuilds every changed file from the pinned sqlglot source in `src.tar.gz` and the diff.
2. Compares the old and new Python AST, so comment- and formatting-only edits select nothing.
   `x is None` / `x == None` and `is`/`==` against ENUM.MEMBER count as the same code.
3. For each function, class or module statement that really changed, picks the tests that
   executed it at the pinned commit (`covmap.json.gz`, per-test line coverage):
   - changes inside a function use statement-level coverage;
   - a lone comparison-operator swap drops tests that evaluated that comparison and never saw the
     old and new operator disagree (`cmpmap.json.gz`);
   - class-level settings select every test that created that class (or a subclass);
   - lines that run only once per process (imports, caches; found by comparing coverage from two
     test orders) select every user of the module, plus whole `setUpClass` test classes.
4. If import-time code changed, it imports every sqlglot module and builds every dialect from a
   patched scratch copy; if anything new breaks, it selects every test.
5. Fixture files map to the tests that opened them (data files: their whole test class).
6. Splits the chosen tests across 1–8 workers, picking the worker count with the lowest cost.

## Pre-existing code and data
- `src.tar.gz`: sqlglot's `.py` files at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` (MIT License, https://github.com/tobymao/sqlglot).
- `covmap.json.gz`, `cmpmap.json.gz`: built from the pinned commit only, by running sqlglot's own
  test suite under coverage (two test orders) and under an instrumented copy that records
  comparison outcomes per test.

License: MIT.
