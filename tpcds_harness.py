"""Reproduce what tests/test_executor.py::TestExecutor::test_execute_tpcds derives from each TPC-DS
entry, and print a digest per entry. Run against a sqlglot tree given on PYTHONPATH.

  python tpcds_harness.py digest <raw_tables.pkl> <tpc-ds.sql> <indices|all>   -> JSON {index: digest}
  python tpcds_harness.py raw <data_dir> <out.pkl>                               -> plain rows (no sqlglot)

Per entry the test: parses the SQL in the parent (every entry), sends the parsed expression through
a spawn pool (pickle round trip), executes it with sqlglot's Python executor on sqlglot Tables
(execute: true entries only), pickles the result back, and compares it with DuckDB running
transpile(sql, write="duckdb"). The digest covers each of those steps.
"""

import ast
import csv
import gzip
import hashlib
import json
import pickle
import sys


def load_pairs(path):
    with open(path, encoding="utf-8") as f:
        text = "\n".join(l for l in f.read().splitlines() if l and not l.startswith("--"))
    st = text.split(";")
    out = []
    for i in range(0, len(st), 2):
        if i + 1 < len(st):
            sql = st[i].strip()
            meta = {}
            lines = sql.split("\n")
            j = 0
            while lines[j].startswith("#"):
                k, v = lines[j].split(":", maxsplit=1)
                meta[k.lstrip("#").strip()] = v.strip()
                j += 1
            out.append((meta, "\n".join(lines[j:]), st[i + 1].strip()))
    return out


def raw(data_dir, out, schema):
    tables = {}
    for table, columns in schema.items():
        with gzip.open(f"{data_dir}/{table}.csv.gz", "rt", newline="") as fh:
            reader = csv.reader(fh, delimiter="|")
            next(reader)
            rows, ctypes = [], []
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = (columns, rows)
    with open(out, "wb") as fh:
        pickle.dump(tables, fh, protocol=5)


def h(obj):
    return hashlib.sha256(repr(obj).encode()).hexdigest()[:16]


def setup(raw_pkl, sql_path):
    from sqlglot.executor.table import Table

    with open(raw_pkl, "rb") as fh:
        rawt = pickle.load(fh)
    tables = {t: Table(columns=cols, rows=rows) for t, (cols, rows) in rawt.items()}
    tables = pickle.loads(pickle.dumps(tables))  # the pool initializer's spawn pickling
    return tables, load_pairs(sql_path)


def run_entry(i, meta, sql, tables, schema):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute

    try:
        expression = parse_one(sql)
    except Exception as e:
        return "parse:" + type(e).__name__
    try:
        expr2, meta2 = pickle.loads(pickle.dumps((expression, meta)))
        parts = [expr2.sql()]
        if meta2.get("execute"):
            sub = {t.name: tables[t.name] for t in find_tables(expr2)}
            table = execute(expr2, schema=schema, tables=sub)
            table = pickle.loads(pickle.dumps(table))
            parts.append((list(table.columns), [tuple(r) for r in table.rows]))
            parts.append(transpile(sql, write="duckdb")[0])
        return h(parts)
    except Exception as e:
        return "error:" + type(e).__name__ + ":" + h(str(e))


def run_early(i, meta, sql, schema):
    """Everything execute() does before planning, plus the parent's parse and the DuckDB transpile."""
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    try:
        expression = parse_one(sql)
    except Exception as e:
        return "parse:" + type(e).__name__
    try:
        expr2, meta2 = pickle.loads(pickle.dumps((expression, meta)))
        parts = [expr2.sql()]
        if meta2.get("execute"):
            opt = optimize(expr2, ensure_schema(schema), leave_tables_isolated=True, dialect=None)
            types = [(type(n).__name__, n.type.sql() if getattr(n, "type", None) is not None else None)
                     for n in opt.walk()]
            parts += [opt.sql(), opt.sql(pretty=True), repr(opt), types, transpile(sql, write="duckdb")[0]]
        return h(parts)
    except Exception as e:
        return "error:" + type(e).__name__ + ":" + h(str(e))


def main():
    mode = sys.argv[1]
    from tests.helpers import TPCDS_SCHEMA

    if mode == "raw":
        raw(sys.argv[2], sys.argv[3], TPCDS_SCHEMA)
        return
    if mode == "early":
        pairs = load_pairs(sys.argv[3])
        tables = None
    else:
        tables, pairs = setup(sys.argv[2], sys.argv[3])
    want = range(len(pairs)) if sys.argv[4] == "all" else json.loads(sys.argv[4])
    # optional expected digests: stop at the first entry that differs
    expected = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else None
    out = {}
    for i in want:
        meta, sql, _ = pairs[i]
        if mode == "early":
            out[i] = run_early(i, meta, sql, TPCDS_SCHEMA)
        else:
            out[i] = run_entry(i, meta, sql, tables, TPCDS_SCHEMA)
        if expected is not None and out[i] != expected.get(str(i)):
            break
    print(json.dumps(out))


if __name__ == "__main__":
    main()
