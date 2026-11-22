"""Scaled replica of tests/test_parser.py::TestParser::test_max_nodes. Every column of the test's
giant query is the same `x=x=...` chain, so the max_nodes errors fire after the same first columns
whatever the width; the replica runs the same calls on fewer columns and prints a digest."""
import hashlib, json, sys


def digest():
    from sqlglot import parse_one
    from sqlglot.errors import ParseError

    def run(sql, **kw):
        try:
            e = parse_one(sql, **kw)
            return ("ok", type(e).__name__, e.sql(), sum(1 for _ in e.walk()))
        except ParseError as ex:
            return ("parse_error", str(ex))
        except Exception as ex:  # anything else changes the test's outcome
            return ("exc", type(ex).__name__, str(ex))

    out = [run("SELECT 1", max_nodes=1_000_000)]
    cols = [f"col{i}" for i in range(500)]
    out.append(run(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000))
    expr = "x" + "=x" * 100
    sql = f"SELECT {','.join(expr for _ in range(300))}"
    out.append(run(sql, max_nodes=50_000))
    out.append(run(sql, max_nodes=20_000))
    out.append(run(f"SELECT {','.join(expr for _ in range(40))}", max_nodes=1_000_000))
    out.append(run("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5))
    out.append(run("SELECT 1, 2, 3"))
    return hashlib.sha256(repr(out).encode()).hexdigest()[:16]


if __name__ == "__main__":
    print(json.dumps({"maxnodes": digest()}))
