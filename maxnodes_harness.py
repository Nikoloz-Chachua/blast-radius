"""Scaled replica of tests/test_parser.py::TestParser::test_max_nodes. Every column of the test's
giant query is the same `x=x=...` chain, so the max_nodes errors fire after the same first columns
whatever the width. The replica makes the test's own assertions on fewer columns and prints their
outcomes plus its run time (a big slowdown could make the real 28.8 s test time out)."""
import json, sys, time


def outcomes():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    def is_select(sql, **kw):
        try:
            return isinstance(parse_one(sql, **kw), exp.Select)
        except Exception as e:
            return "raised:" + type(e).__name__

    def raises(sql, needle, **kw):
        try:
            parse_one(sql, **kw)
            return "no error"
        except ParseError as e:
            return needle in str(e)
        except Exception as e:
            return "raised:" + type(e).__name__

    out = [is_select("SELECT 1", max_nodes=1_000_000)]
    cols = [f"col{i}" for i in range(500)]
    out.append(is_select(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000))
    expr = "x" + "=x" * 100
    sql = f"SELECT {','.join(expr for _ in range(600))}"  # >50,000 nodes: errors fire as in the test
    out.append(raises(sql, "Maximum number of AST nodes", max_nodes=20_000))
    out.append(raises(sql, "(50000)", max_nodes=50_000))
    out.append(is_select(f"SELECT {','.join(expr for _ in range(40))}", max_nodes=1_000_000))
    out.append(raises("SELECT " + ",".join(f"x={i}" for i in range(100)), "", max_nodes=5))
    out.append(is_select("SELECT 1, 2, 3"))
    return out


if __name__ == "__main__":
    t = time.process_time()
    res = outcomes()
    print(json.dumps({"maxnodes": res, "cpu": round(time.process_time() - t, 3)}))
