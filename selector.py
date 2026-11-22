"""Blast Radius selector.

For each patch: rebuild the changed files, compare old and new Python ASTs to
find which functions / classes / module statements really changed, and pick
the tests that executed those parts at the pinned commit (per-test coverage
map built from the pinned commit). Comment- or formatting-only edits select
nothing. Fixture files map to the tests that opened them.
"""

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA", "/data"))
SRC = Path(os.environ.get("SQLGLOT", "/opt/sqlglot"))
HERE = Path(__file__).resolve().parent
MAX_WORKERS = 8
WORKER_COST_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- map loading
class CovMap:
    def __init__(self, path: Path):
        raw = json.loads(gzip.decompress(path.read_bytes()))
        self.lines = raw["lines"]
        self.fix = raw["fix"]
        self.cls = raw.get("cls", {})
        self.unstable = {k: set(v) for k, v in raw.get("unstable", {}).items()}
        self.eager = {k: set(v) for k, v in raw.get("eager", {}).items()}
        self.fix_setup = raw.get("fix_setup", {})
        rp = path.parent / "covmap_rt.json.gz"
        self.runtime = CovMap(rp) if rp.exists() and path.name != "covmap_rt.json.gz" else None
        ap = path.parent / "armap.json.gz"
        self.ar = json.loads(gzip.decompress(ap.read_bytes())) if ap.exists() and path.name == "covmap.json.gz" else None
        dp = path.parent / "dkmap.json.gz"
        self.dk = json.loads(gzip.decompress(dp.read_bytes())) if dp.exists() and path.name == "covmap.json.gz" else None
        cp = path.parent / "cmpmap.json.gz"
        self.cmp = json.loads(gzip.decompress(cp.read_bytes())) if cp.exists() else {}
        self._cache = {}

    def file(self, rel):
        if rel not in self._cache:
            entry = self.lines.get(rel)
            if entry is None:
                self._cache[rel] = {}
            else:
                sets = entry["sets"]
                self._cache[rel] = {int(k): sets[v] for k, v in entry["ln"].items()}
        return self._cache[rel]

    def range(self, rel, start, end):
        per = self.file(rel)
        out = set()
        for ln in range(start, end + 1):
            s = per.get(ln)
            if s:
                out.update(s)
        return out

    def whole_file(self, rel):
        out = set()
        for s in self.file(rel).values():
            out.update(s)
        return out


# ---------------------------------------------------------------- diff parsing
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return {path: (old_path, new_path, [hunks])}; hunk = (old_start, lines)."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "header": line}
            files.append(cur)
        elif cur is not None and line.startswith("--- ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif cur is not None and line.startswith("+++ ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif cur is not None and line.startswith("@@"):
            m = HUNK.match(line)
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            o = n = 0
            i += 1
            while i < len(lines) and (o < old_len or n < new_len):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                body.append((tag, l[1:]))
                if tag in (" ", "-"):
                    o += 1
                if tag in (" ", "+"):
                    n += 1
                i += 1
            cur["hunks"].append((old_start, body))
            continue
        i += 1
    for f in files:
        if f["old"] is None and f["new"] is None:
            parts = f["header"].split(" ")
            f["old"] = parts[2][2:]
            f["new"] = parts[3][2:]
    return files


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if old_start == 0:
            start = 0
        out.extend(old[pos:start])
        pos = start
        for tag, content in body:
            if tag == " ":
                out.append(old[pos])
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(content)
    out.extend(old[pos:])
    return "\n".join(out)


def changed_old_lines(hunks):
    """Old-file line numbers touched by the hunks (deleted lines and insertion points)."""
    touched = set()
    for old_start, body in hunks:
        pos = old_start
        for tag, _ in body:
            if tag == " ":
                pos += 1
            elif tag == "-":
                touched.add(pos)
                pos += 1
            elif tag == "+":
                touched.add(max(pos - 1, 1))
                touched.add(pos)
    return touched


# ---------------------------------------------------------------- AST diffing
class _Canon(ast.NodeTransformer):
    """Treat `x is None` / `x == None` (and `is`/`==` against ENUM_LIKE.MEMBER) as the same code."""

    @staticmethod
    def _identity_safe(node):
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        return (isinstance(node, ast.Attribute) and node.attr.isupper() and len(node.attr) > 1
                and isinstance(node.value, (ast.Name, ast.Attribute)))

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if self._identity_safe(right) or self._identity_safe(left):
                if isinstance(op, ast.Eq):
                    op = ast.Is()
                elif isinstance(op, ast.NotEq):
                    op = ast.IsNot()
            ops.append(op)
            left = right
        node.ops = ops
        return node


CMP_NAMES = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge",
             ast.Is: "is", ast.IsNot: "isnot", ast.In: "in", ast.NotIn: "notin"}
CMP_FAMILY = {"eq": ("eq", "ne", "is", "isnot", "lt", "le", "gt", "ge"),
              "ne": ("eq", "ne", "is", "isnot", "lt", "le", "gt", "ge"),
              "is": ("is", "isnot", "eq", "ne"), "isnot": ("is", "isnot", "eq", "ne"),
              "lt": ("lt", "le", "gt", "ge", "eq", "ne"), "le": ("lt", "le", "gt", "ge", "eq", "ne"),
              "gt": ("lt", "le", "gt", "ge", "eq", "ne"), "ge": ("lt", "le", "gt", "ge", "eq", "ne"),
              "in": ("in", "notin"), "notin": ("in", "notin")}


def single_cmp_swap(old_fn, new_fn):
    """If the only difference is one comparison operator, return (lineno, col, old_op, new_op)."""
    a, b = list(ast.walk(old_fn)), list(ast.walk(new_fn))
    if len(a) != len(b):
        return None
    found = None
    for x, y in zip(a, b):
        if type(x) is not type(y):
            if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
                continue  # judged at the Compare node
            return None
        if isinstance(x, ast.Compare):
            if [type(o) for o in x.ops] != [type(o) for o in y.ops]:
                if found is not None or len(x.ops) != 1:
                    return None
                ox, oy = CMP_NAMES.get(type(x.ops[0])), CMP_NAMES.get(type(y.ops[0]))
                if ox is None or oy is None:
                    return None
                found = (x.lineno, x.col_offset, ox, oy)
            continue
        if isinstance(x, ast.cmpop):
            continue
        for field, vx in ast.iter_fields(x):
            if isinstance(vx, ast.AST) or (isinstance(vx, list) and vx and isinstance(vx[0], ast.AST)):
                continue
            if vx != getattr(y, field, None):
                return None
    return found


def canon(tree):
    return _Canon().visit(tree)


class LazyDump:
    """Canonical AST dump of a unit, computed only when needed. Two units whose source lines the
    patch did not touch are equal without dumping (same text, same AST)."""
    __slots__ = ("nodes", "lines", "touched", "_v")

    def __init__(self, nodes, lines, touched):
        self.nodes, self.lines, self.touched, self._v = nodes, lines, touched, None

    @property
    def untouched(self):
        return not (self.lines & self.touched)

    @property
    def value(self):
        if self._v is None:
            import copy
            self._v = "|".join(ast.dump(_Canon().visit(copy.deepcopy(n))) for n in self.nodes)
        return self._v

    def __eq__(self, other):
        if not isinstance(other, LazyDump):
            return NotImplemented
        if self.untouched and other.untouched and len(self.nodes) == len(other.nodes):
            return True
        return self.value == other.value

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    def __hash__(self):
        return 0


def _span(nodes):
    out = set()
    for n in nodes:
        start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        out.update(range(start, n.end_lineno + 1))
    return out


def units(tree, touched=frozenset()):
    """Map qualified name -> (kind, node, dump, parent_class_name); dump is a LazyDump."""
    result = {}

    def visit(body, prefix, cls):
        others = []
        for idx, node in enumerate(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                if name in result:
                    name = f"{name}#{idx}"
                result[name] = ("func", node, LazyDump([node], _span([node]), touched), cls)
            elif isinstance(node, ast.ClassDef):
                name = f"{prefix}{node.name}"
                if name in result:
                    name = f"{name}#{idx}"
                header = ast.ClassDef(
                    name=node.name,
                    bases=node.bases,
                    keywords=node.keywords,
                    body=[],
                    decorator_list=node.decorator_list,
                    type_params=getattr(node, "type_params", []),
                )
                first = node.body[0].lineno if node.body else node.lineno + 1
                hstart = min([node.lineno] + [d.lineno for d in node.decorator_list])
                result[name] = ("class", node, LazyDump([header], set(range(hstart, first)), touched), cls)
                visit(node.body, name + ".", name)
            else:
                others.append(node)
        key = f"{prefix}<body>"
        result[key] = ("body", others, LazyDump(others, _span(others), touched), cls)

    visit(tree.body, "", None)
    return result


def new_touched_lines(hunks):
    """New-file line numbers touched by the hunks (added lines and both sides of deletions)."""
    touched = set()
    offset = 0
    for old_start, body in hunks:
        pos = max(old_start, 1) + offset
        for tag, _ in body:
            if tag == " ":
                pos += 1
            elif tag == "-":
                touched.add(pos)
                touched.add(max(pos - 1, 1))
                offset -= 1
            elif tag == "+":
                touched.add(pos)
                pos += 1
                offset += 1
    return touched


def node_range(node):
    if isinstance(node, list):
        if not node:
            return None
        return min(n.lineno for n in node), max(n.end_lineno for n in node)
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


# tests that run sqlglot in a child process (coverage cannot see inside it)
SUBPROCESS_TESTS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
# fixture files TestExecutor.setUpClass loads (tables + query lists) -> the tests that use them
_EX = "tests/test_executor.py::TestExecutor::"
SETUP_FIXTURE_USERS = {
    "tests/fixtures/optimizer/tpc-h/": [_EX + "test_optimized_tpch", _EX + "test_execute_tpch"],
    "tests/fixtures/optimizer/tpc-ds/": [_EX + "test_execute_tpcds"],
}
# dialect names (module stems under sqlglot/dialects, parsers, generators, tokenizers, typing)
DIALECT_NAMES = {
    "athena", "bigquery", "clickhouse", "databricks", "dax", "doris", "dremio", "drill", "druid", "duckdb", "dune",
    "exasol", "fabric", "hive", "materialize", "mysql", "oracle", "postgres", "presto", "prql", "redshift",
    "risingwave", "singlestore", "snowflake", "solr", "spark", "spark2", "sqlite", "starrocks", "tableau",
    "teradata", "trino", "tsql",
}
# class settings that tokenizer/dialect class-building code compiles into other structures
BAKED_NAMES = {
    "BIT_STRINGS", "BYTE_STRINGS", "BYTE_STRING_ESCAPES", "COMMENTS", "HEREDOC_STRINGS", "HEX_STRINGS",
    "IDENTIFIERS", "IDENTIFIER_ESCAPES", "KEYWORDS", "NATIONAL_RAW_STRINGS", "QUOTES", "RAW_STRINGS",
    "SINGLE_TOKENS", "STRING_ESCAPES", "UNICODE_STRINGS", "NUMERIC_ESCAPES", "TIME_MAPPING", "FORMAT_MAPPING",
    "INVERSE_TIME_MAPPING", "INVERSE_FORMAT_MAPPING", "CREATABLE_KIND_MAPPING", "DATE_PART_MAPPING",
    "ESCAPED_SEQUENCES", "UNESCAPED_SEQUENCES", "TRANSFORMS", "VALID_INTERVAL_UNITS", "SUPPORTED_JSON_PATH_PARTS",
}
# helpers that run while classes are being built at import time (their results are baked in)
IMPORT_MACHINERY = {
    "sqlglot/tokens.py": ("_convert_quotes", "_quotes_to_format"),
    "sqlglot/dialects/dialect.py": ("_with_strict_time_inverse",),
    "sqlglot/helper.py": ("subclasses",),
    "sqlglot/trie.py": ("new_trie",),
}
# builds every dialect's generator 1000x and checks memory: only generator code can break it
# (a dialect that fails to import is caught by the import check, which then selects everything)
GENMEM = "tests/test_generator.py::TestGenerator::test_generator_memory"
GENMEM_FILES = ("sqlglot/generator.py", "sqlglot/generators/", "sqlglot/dialects/dialect.py",
                "sqlglot/dialects/__init__.py", "sqlglot/helper.py")
# derived/class-read names precomputed from the pinned source (same scan as below)
NAMES_FILE = HERE / "names.json"
MAXNODES = "tests/test_parser.py::TestParser::test_max_nodes"
MAXNODES_COV = HERE / "maxnodes_cov.json"
MAXNODES_DIGEST = Path(os.environ.get("MAXNODES_DIGEST", "/app/maxnodes_digest.json"))
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_COV = HERE / "tpcds_cov.json.gz"
TPCDS_DIGESTS = Path(os.environ.get("TPCDS_DIGESTS", "/app/tpcds_digests.json"))
TPCDS_RAW = Path(os.environ.get("TPCDS_RAW", "/app/tpcds_raw.pkl"))
TPCDS_EARLY = Path(os.environ.get("TPCDS_EARLY", "/app/tpcds_early.json"))
TPCDS_MAX_ENTRIES = 30
def is_dialect_module(rel):
    """A dialect-specific module (not the shared base or the executor's Python dialect)."""
    parts = rel.split("/")
    return (len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "tokenizers", "typing")
            and parts[2][:-3] in DIALECT_NAMES and parts[2][:-3] != "python")


TPC_QUERY_FILES = {"tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"}
# test classes that build shared state in setUpClass
# (class -> modules its setUpClass runs: executor tables + pool initializer, MappingSchema)
SETUP_CLASSES = {"tests/test_executor.py::TestExecutor": ("sqlglot/executor/", "sqlglot/schema.py"),
                 "tests/test_optimizer.py::TestOptimizer": ("sqlglot/schema.py",),
                 "tests/test_lineage.py::TestLineage": ("sqlglot/schema.py",)}
# modules imported by a bare `import sqlglot`
EAGER_MODULES = {
    "sqlglot/__init__.py", "sqlglot/_typing.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py",
    "sqlglot/diff.py", "sqlglot/errors.py", "sqlglot/expressions/__init__.py", "sqlglot/expressions/aggregate.py",
    "sqlglot/expressions/array.py", "sqlglot/expressions/builders.py", "sqlglot/expressions/constraints.py",
    "sqlglot/expressions/core.py", "sqlglot/expressions/datatypes.py", "sqlglot/expressions/ddl.py",
    "sqlglot/expressions/dml.py", "sqlglot/expressions/functions.py", "sqlglot/expressions/json.py",
    "sqlglot/expressions/math.py", "sqlglot/expressions/properties.py", "sqlglot/expressions/query.py",
    "sqlglot/expressions/string.py", "sqlglot/expressions/temporal.py", "sqlglot/generator.py",
    "sqlglot/helper.py", "sqlglot/jsonpath.py", "sqlglot/optimizer/__init__.py", "sqlglot/optimizer/scope.py",
    "sqlglot/parser.py", "sqlglot/parsers/__init__.py", "sqlglot/parsers/base.py", "sqlglot/schema.py",
    "sqlglot/time.py", "sqlglot/tokenizer_core.py", "sqlglot/tokens.py", "sqlglot/trie.py", "sqlglot/typing/__init__.py",
}
BASE_STEMS = {"dialect", "__init__", "parser", "generator", "tokens", "tokenizer", "core", "base", "helper"}


def sig_dump(node):
    """Function without its body: name, args, decorators, returns."""
    clone = ast.FunctionDef(name=node.name, args=node.args, body=[], decorator_list=node.decorator_list,
                            returns=node.returns, type_comment=None, type_params=getattr(node, "type_params", []))
    return ast.dump(clone)


IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Selector:
    def __init__(self, tests, cov, src):
        self.tests = tests
        self.all = {t["number"] for t in tests}
        self.cov = cov
        self.src = src
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self._src_cache = {}
        self._runtime = {}
        self._derived = None
        self._class_reads = None
        self._py_files = None
        self.import_time_files = {}
        self._overlay = None
        self._baseline = "unset"
        self._tpcds = None
        self.subprocess_tests = {t["number"] for t in tests if t["id"] in SUBPROCESS_TESTS}
        self.by_class = {}
        for t in tests:
            self.by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
        self.cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
        self.num_of = {t["id"]: t["number"] for t in tests}
        self.tests_by_num = {t["number"]: t for t in tests}

    def read(self, rel):
        if rel not in self._src_cache:
            p = self.src / rel
            self._src_cache[rel] = p.read_text() if p.exists() else None
        return self._src_cache[rel]

    def py_files(self):
        if self._py_files is None:
            self._py_files = [
                str(p.relative_to(self.src))
                for d in ("sqlglot", "tests")
                for p in (self.src / d).rglob("*.py")
            ]
        return self._py_files

    def references(self, names):
        """Tests covering any line (in sqlglot/ or tests/) that mentions one of names."""
        out = set()
        if not names:
            return out
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        for rel in self.py_files():
            text = self.read(rel)
            if text is None or not pat.search(text):
                continue
            per = self.cov.file(rel)
            for i, line in enumerate(text.split("\n"), 1):
                if pat.search(line):
                    s = per.get(i)
                    if s:
                        out.update(s)
                    elif rel.startswith("tests/"):
                        # e.g. a module-level reference in a test file
                        out.update(self.by_file.get(rel, ()))
        return out

    def runtime_lines(self, rel):
        """Lines that run when code is called (function bodies), not when the module is imported
        (def/decorator lines, class bodies, module level): import-time lines are credited to every
        test that imports the module."""
        if rel not in self._runtime:
            text = self.read(rel)
            out = set()
            try:
                for n in ast.walk(ast.parse(text or "")):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        body = n.body if isinstance(n.body, list) else [n.body]
                        out.update(range(body[0].lineno, n.end_lineno + 1))
            except SyntaxError:
                pass
            self._runtime[rel] = out
        return self._runtime[rel]

    def cov_runtime(self, rel, start, end):
        rt = self.runtime_lines(rel)
        per = self.cov.file(rel)
        out = set()
        for ln in range(start, end + 1):
            if ln in rt:
                s = per.get(ln)
                if s:
                    out.update(s)
        return out

    def class_scope(self, rel, old_units, cls):
        if cls is None:
            return self.file_scope(rel)
        node = old_units[cls][1]
        s, e = node_range(node)
        return self.cov_runtime(rel, s, e)

    def file_scope(self, rel):
        text = self.read(rel)
        n = (text or "").count("\n") + 1
        out = self.cov_runtime(rel, 1, n) if rel.startswith("sqlglot/") else self.cov.whole_file(rel)
        if rel.startswith("tests/"):
            out |= self.by_file.get(rel, set())
        return out

    # ---- helpers for precise (statement-level) selection
    def family(self, rel):
        """Tests using a dialect-specific module family, e.g. every sqlglot/*/tsql.py."""
        parts = rel.split("/")
        stem = parts[-1][:-3]
        if len(parts) < 3 or stem in BASE_STEMS:
            return None
        out = set()
        for f in self.cov.lines:
            fp = f.split("/")
            if f.startswith("sqlglot/") and fp[-1] == stem + ".py":
                out |= self.cov.whole_file(f)
        return out

    def stmt_lines(self, func, lines):
        """Coverage lines that decide whether `lines` (old line numbers) matter inside func."""
        want = set()
        body_stmts = []

        def walk(stmts, parent):
            for st in stmts:
                body_stmts.append((st, parent))
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(st, field, None)
                    if sub and isinstance(sub, list):
                        walk([x for x in sub if isinstance(x, ast.stmt)] or [], st)
                        for h in sub:
                            if not isinstance(h, ast.stmt) and hasattr(h, "body"):
                                walk(h.body, st)

        walk(func.body, func)
        for ln in lines:
            best = None
            for st, parent in body_stmts:
                if st.lineno <= ln <= st.end_lineno:
                    if best is None or (st.end_lineno - st.lineno) <= (best[0].end_lineno - best[0].lineno):
                        best = (st, parent)
            if best is None:
                return None  # in the signature/decorators: whole function
            st, parent = best
            first_child = None
            for field in ("body", "handlers", "cases"):
                sub = getattr(st, field, None)
                if sub and isinstance(sub, list):
                    first_child = min(x.lineno for x in sub)
                    break
            if first_child is not None and ln >= first_child:
                # inside a compound statement but not in any child statement
                # (e.g. an else:/except: line): who evaluates the compound header
                want.update(range(st.lineno, first_child))
            elif first_child is not None:
                want.update(range(st.lineno, first_child))
            else:
                want.update(range(st.lineno, st.end_lineno + 1))
            if parent is not func and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pf = None
                for field in ("body", "handlers", "cases"):
                    sub = getattr(parent, field, None)
                    if sub and isinstance(sub, list):
                        pf = min(x.lineno for x in sub)
                        break
                want.update(range(parent.lineno, (pf or parent.lineno + 1)))
        return want

    def python_change(self, rel, old_text, new_text, hunks):
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return set(self.all)
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            # broken module: imports fail all over the suite
            return set(self.all)
        touched = changed_old_lines(hunks)
        ou, nu = units(old_tree, touched), units(new_tree, new_touched_lines(hunks))
        self._raw_units = (ou, nu)
        uns = self.cov.eager.get(rel, set())
        import_time = bool(touched & uns) or set(ou) != set(nu) or any(
            k in ("body", "class") and nu.get(n) is not None and nu[n][2] != d
            for n, (k, _, d, _) in ou.items())
        if import_time and not (touched & uns) and set(ou) == set(nu) and self.constant_only(ou, nu):
            # only literal values changed in class/module statements (no new names, no structure
            # change): evaluating the statement cannot raise; baked settings still get the check
            import_time = False
        if import_time and rel.startswith("sqlglot/"):
            self.import_time_files[rel] = new_text
        if import_time and rel in EAGER_MODULES:
            picked_extra = set(self.subprocess_tests)
        else:
            picked_extra = set()
        picked = set()
        names_needing_refs = set()
        for name, (kind, node, dump, cls) in ou.items():
            other = nu.get(name)
            if other is not None and other[2] == dump:
                continue
            if kind == "func":
                rng = node_range(node)
                whole = self.cov_runtime(rel, *rng)
                got = None
                want = None
                if other is not None and sig_dump(node) == sig_dump(other[1]):
                    lines = {l for l in touched if rng[0] <= l <= rng[1]}
                    want = self.stmt_lines(node, lines) if lines else None
                    if want is not None:
                        got = set()
                        per = self.cov.file(rel)
                        for l in want:
                            got.update(per.get(l, ()))
                if got is not None and other is not None:
                    got = self.cmp_refine(rel, old_text, new_text, name, got)
                picked |= whole if got is None else got
                self.func_picked |= whole if got is None else got
                if other is None:
                    self.nonfunc_change = True
                else:
                    rng_lines = set(range(rng[0], rng[1] + 1))
                    if got is None or want is None:
                        self.changed_lines.setdefault(rel, set()).update(rng_lines)
                    else:
                        self.changed_lines.setdefault(rel, set()).update(want)
                        self.changed_lines[rel].update(l for l in touched if rng[0] <= l <= rng[1])
                # class-building code that runs while modules import (metaclasses, __init_subclass__):
                # its results are baked into classes every test uses
                if self.is_class_machinery(node, cls, ou, rel) and {l for l in touched if rng[0] <= l <= rng[1]} & self.cov.eager.get(rel, set()):
                    picked |= set(self.all)
                if other is None:
                    # removed method: anything using its class may now dispatch differently
                    picked |= self.class_scope(rel, ou, cls)
                    if cls:
                        picked |= self.class_users(rel, cls) or set()
            elif kind == "class":
                self.nonfunc_change = True
                picked |= self.class_scope(rel, ou, name)
                names_needing_refs.add(name.split(".")[-1])
                picked |= self.class_users(rel, name) or set()
            else:  # body statements of a class or module
                self.nonfunc_change = True
                if cls is None:
                    lam = self.lambda_body_tests(rel, node, other[1], touched) if other else None
                    if lam is not None:
                        picked |= lam
                        continue
                    picked |= self.file_scope(rel)
                    names_needing_refs |= self.changed_names(node, other[1] if other else [])
                else:
                    picked |= self.class_attr_change(rel, ou, cls, node, other[1] if other else [], touched)
        for name, (kind, node, dump, cls) in nu.items():
            if name in ou:
                continue
            self.nonfunc_change = True
            # new function / class: its container decides who can reach it
            if cls is not None and cls in ou:
                picked |= self.class_scope(rel, ou, cls)
                names_needing_refs.add(cls.split(".")[-1])
                picked |= self.class_users(rel, cls) or set()
            else:
                picked |= self.file_scope(rel)
        if names_needing_refs:
            picked |= self.references(names_needing_refs)
        return picked

    def class_users(self, rel, cls):
        """Tests that created an instance of this class or a subclass (None if never seen)."""
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        qual = ".".join(part.split("#")[0] for part in cls.split("."))
        got = self.cov.cls.get(f"{mod}:{qual}")
        return set(got) if got is not None else None

    def tpc_queries(self, rel, hunks, got):
        """Parse old/new TPC query files like tests/helpers.load_sql_fixture_pairs does."""
        old = self.read(rel)
        if old is None:
            return got
        new = apply_hunks(old, hunks)

        def pairs(text):
            body = "\n".join(l for l in text.splitlines() if l and not l.startswith("--"))
            st = body.split(";")
            return [(st[i].strip(), st[i + 1].strip()) for i in range(0, len(st), 2) if i + 1 < len(st)]

        po, pn = pairs(old), pairs(new)
        if len(po) != len(pn):
            return got
        src_changed = any(a[0] != b[0] for a, b in zip(po, pn))
        any_changed = src_changed or any(a[1] != b[1] for a, b in zip(po, pn))
        if not any_changed:
            return set()
        if not src_changed:
            # only expected outputs changed: executor runs of the input queries are unaffected
            drop = {self.num_of.get(_EX + "test_execute_tpch"), self.num_of.get(_EX + "test_execute_tpcds")}
            return got - drop
        return got

    def constant_only(self, ou, nu):
        """True when every changed class/module-level statement differs only in literal constants and
        assigns no baked setting; function bodies are not import-time code here."""
        for name, (kind, node, dump, cls) in ou.items():
            other = nu.get(name)
            if other is None or other[2] == dump:
                continue
            if kind == "func":
                continue
            if kind != "body":
                return False
            a_nodes, b_nodes = node, other[1]
            if len(a_nodes) != len(b_nodes):
                return False
            for a, b in zip(a_nodes, b_nodes):
                if ast.dump(a) == ast.dump(b):
                    continue
                for n in ast.walk(a):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and (n.id in BAKED_NAMES):
                        return False
                xa, xb = list(ast.walk(a)), list(ast.walk(b))
                if len(xa) != len(xb):
                    return False
                for x, y in zip(xa, xb):
                    if type(x) is not type(y):
                        return False
                    if isinstance(x, ast.Constant):
                        if type(x.value) is not type(y.value):
                            return False
                        continue
                    for field, vx in ast.iter_fields(x):
                        if isinstance(vx, ast.AST) or (isinstance(vx, list) and vx and isinstance(vx[0], ast.AST)):
                            continue
                        if vx != getattr(y, field, None):
                            return False
        return True

    def is_class_machinery(self, node, cls, ou, rel=None):
        if node.name in ("__init_subclass__", "__set_name__", "__class_getitem__"):
            return True
        if rel is not None and node.name in IMPORT_MACHINERY.get(rel, ()):
            return True
        if cls and cls in ou:
            cnode = ou[cls][1]
            for b in cnode.bases:
                name = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else ""
                if name == "type":
                    return True
        return False

    def genmem_proof(self, files):
        """Run test_generator_memory's own loop, on the patched code, for the dialects whose dialect or
        generator classes come from the changed modules (the others run unchanged code)."""
        import shutil, subprocess, tempfile
        try:
            mods = []
            for rel in files:
                mod = rel[:-3].replace("/", ".")
                mods.append(mod[: -len(".__init__")] if mod.endswith(".__init__") else mod)
            if self._overlay is None:
                self._overlay = tempfile.mkdtemp(prefix="br_")
                shutil.copytree(self.src / "sqlglot", os.path.join(self._overlay, "sqlglot"))
            try:
                for rel, text in self.patched_texts.items():
                    with open(os.path.join(self._overlay, rel), "w") as fh:
                        fh.write(text)
                env = dict(os.environ, PYTHONPATH=self._overlay, PYTHONDONTWRITEBYTECODE="1")
                r = subprocess.run([sys.executable, str(HERE / "genmem_harness.py"), json.dumps(mods), "8"],
                                   cwd=self._overlay, env=env, capture_output=True, text=True, timeout=120)
            finally:
                for rel in self.patched_texts:
                    src = self.src / rel
                    if src.exists():
                        shutil.copy2(src, os.path.join(self._overlay, rel))
            lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
            if r.returncode != 0 or not lines:
                return False
            res = json.loads(lines[-1])
            log(f"generator_memory check: {res}")
            return bool(res.get("ok")) and res.get("dialects", 0) > 0
        except Exception as e:
            log(f"generator_memory check error {e!r}")
            return False

    def maxnodes_proof(self):
        """Run the scaled replica of test_max_nodes on the patched code and compare its digest with
        the unpatched one computed at build time."""
        import shutil, subprocess, tempfile
        try:
            if not MAXNODES_COV.exists() or not MAXNODES_DIGEST.exists():
                return False
            replica = {(r, l) for r, l in json.loads(MAXNODES_COV.read_text())["replica"]}
            for rel, lines in self.changed_lines.items():
                for ln in lines:
                    if self.num_of[MAXNODES] in self.cov.file(rel).get(ln, ()) and (rel, ln) not in replica:
                        return False
            if not self.patched_texts:
                return False
            if self._overlay is None:
                self._overlay = tempfile.mkdtemp(prefix="br_")
                shutil.copytree(self.src / "sqlglot", os.path.join(self._overlay, "sqlglot"))
            try:
                for rel, text in self.patched_texts.items():
                    with open(os.path.join(self._overlay, rel), "w") as fh:
                        fh.write(text)
                env = dict(os.environ, PYTHONPATH=self._overlay, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
                r = subprocess.run([sys.executable, str(HERE / "maxnodes_harness.py")], cwd=self._overlay, env=env,
                                   capture_output=True, text=True, timeout=120)
            finally:
                for rel in self.patched_texts:
                    src = self.src / rel
                    if src.exists():
                        shutil.copy2(src, os.path.join(self._overlay, rel))
            lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
            if r.returncode != 0 or not lines:
                return False
            got, base = json.loads(lines[-1]), json.loads(MAXNODES_DIGEST.read_text())
            # same assertion outcomes, and not markedly slower (the real test runs ~29 s)
            ok = got.get("maxnodes") == base.get("maxnodes") and got.get("cpu", 1e9) <= 2 * base.get("cpu", 0) + 0.3
            log(f"max_nodes proof: {'same' if ok else 'DIFFERENT'}")
            return ok
        except Exception as e:
            log(f"max_nodes proof error {e!r}")
            return False

    def tpcds_proof(self, all_early=False):
        """Re-run the TPC-DS entries that reach the changed lines on the patched code; True when
        every digest matches the unpatched one computed at build time."""
        import shutil, subprocess, tempfile
        try:
            if not TPCDS_COV.exists() or not TPCDS_DIGESTS.exists() or not TPCDS_RAW.exists():
                return False
            if self._tpcds is None:
                raw = json.loads(gzip.decompress(TPCDS_COV.read_bytes()))
                setup, late = {}, {}
                for rel, ln in raw["setup"]:
                    setup.setdefault(rel, set()).add(ln)
                for rel, ln in raw.get("late", []):
                    late.setdefault(rel, set()).add(ln)
                early = json.loads(TPCDS_EARLY.read_text()) if TPCDS_EARLY.exists() else None
                self._tpcds = (setup, late, raw["entries"], json.loads(TPCDS_DIGESTS.read_text()), early)
            setup, late, entries, digests, early_digests = self._tpcds
            if all_early:
                if early_digests is None:
                    return False
                # non-executed entries are only parsed with the default dialect, which a
                # dialect-specific module cannot change
                affected = {int(k) for k in early_digests if k in self._tpcds_executed()}
                mode = "early"
                digests = early_digests
                return self._tpcds_run(mode, affected, digests)
            affected = set()
            for rel, lines in self.changed_lines.items():
                if lines & setup.get(rel, set()):
                    log(f"tpcds proof: changed lines run in setup {sorted(lines & setup.get(rel, set()))[:5]}")
                    return False
                per = entries.get(rel, {})
                for ln in lines:
                    affected.update(per.get(str(ln), ()))
            in_late = any(lines & late.get(rel, set()) for rel, lines in self.changed_lines.items())
            # early-stage proof: the changed lines run only before planning, so equal early outputs
            # mean planning and execution get identical input and run identical code
            mode = "early" if (not in_late and early_digests is not None) else "digest"
            if not affected or (mode == "digest" and len(affected) > TPCDS_MAX_ENTRIES):
                log(f"tpcds proof: {len(affected)} entries affected ({mode})")
                return False
            if mode == "early":
                digests = early_digests
            return self._tpcds_run(mode, affected, digests)
        except Exception as e:
            log(f"tpcds proof error {e!r}")
            return False

    def _tpcds_executed(self):
        if getattr(self, "_tpcds_exec", None) is None:
            text = (self.src / "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql").read_text()
            body = "\n".join(l for l in text.splitlines() if l and not l.startswith("--"))
            st = body.split(";")
            ex = set()
            for idx, i in enumerate(range(0, len(st), 2)):
                if i + 1 < len(st) and "# execute: true" in st[i].strip().split("\n")[0:3].__str__():
                    ex.add(str(idx))
            self._tpcds_exec = ex
        return self._tpcds_exec

    def _tpcds_run(self, mode, affected, digests):
        import shutil, subprocess, tempfile
        try:
            if self._overlay is None:
                self._overlay = tempfile.mkdtemp(prefix="br_")
                shutil.copytree(self.src / "sqlglot", os.path.join(self._overlay, "sqlglot"))
            try:
                for rel, text in self.patched_texts.items():
                    with open(os.path.join(self._overlay, rel), "w") as fh:
                        fh.write(text)
                env = dict(os.environ, PYTHONPATH=self._overlay + os.pathsep + str(self.src),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
                r = subprocess.run([sys.executable, str(HERE / "tpcds_harness.py"), mode, str(TPCDS_RAW),
                                    str(self.src / "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"),
                                    json.dumps(sorted(affected)),
                                    str(TPCDS_EARLY if mode == "early" else TPCDS_DIGESTS)],
                                   cwd=self._overlay, env=env, capture_output=True, text=True, timeout=120)
            finally:
                for rel in self.patched_texts:
                    src = self.src / rel
                    if src.exists():
                        shutil.copy2(src, os.path.join(self._overlay, rel))
            lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
            if r.returncode != 0 or not lines:
                return False
            got = json.loads(lines[-1])
            ok = all(got.get(str(i)) == digests.get(str(i)) for i in affected)
            log(f"tpcds proof ({mode}) on {len(affected)} entries: {'same' if ok else 'DIFFERENT'}")
            return ok
        except Exception as e:
            log(f"tpcds proof error {e!r}")
            return False

    def setup_class_expand(self, picked, rel):
        """Classes with setUpClass build shared state once; credit goes to their first test only.
        Expand only when the changed module is one that setUpClass itself runs."""
        extra = set()
        for n in picked:
            c = self.cls_of[n]
            prefixes = SETUP_CLASSES.get(c)
            if prefixes and any(rel.startswith(p) for p in prefixes):
                extra |= self.by_class.get(c, set())
        return extra

    def _derived_names(self):
        """Class-level names that some class body or module level reads other than through a
        `{**X.NAME, ...}` merge into the same NAME: their values get baked into other attributes."""
        if self._derived is None and NAMES_FILE.exists():
            self._derived = set(json.loads(NAMES_FILE.read_text())["derived"]) | set(BAKED_NAMES)
        if self._derived is None:
            out = set(BAKED_NAMES)
            for rel in self.py_files():
                if not rel.startswith("sqlglot/"):
                    continue
                try:
                    tree = ast.parse(self.read(rel) or "")
                except SyntaxError:
                    continue
                def scan(stmts):
                    for st in stmts:
                        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for d in st.decorator_list:
                                for n in ast.walk(d):
                                    if isinstance(n, ast.Attribute) and n.attr.isupper(): out.add(n.attr)
                            continue
                        if isinstance(st, ast.ClassDef):
                            scan(st.body); continue
                        own = None
                        if isinstance(st, (ast.Assign, ast.AnnAssign)):
                            tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                            own = tg.id if isinstance(tg, ast.Name) else None
                        val = getattr(st, "value", None)
                        # keys-only tries (SHOW_TRIE = new_trie(k.split(" ") for k in SHOW_PARSERS)): an
                        # added/removed key is handled by the added-key rule
                        if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "new_trie":
                            continue
                        for n in ast.walk(st):
                            name = n.attr if isinstance(n, ast.Attribute) else n.id if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) else None
                            # copies into the same name (subclass merges/filters) keep per-key semantics
                            if name and name.isupper() and len(name) > 1 and name != own:
                                out.add(name)
                scan(tree.body)
            self._derived = out
        return self._derived

    @staticmethod
    def _key_reps(node):
        if isinstance(node, ast.Constant):
            return {repr(node.value)}
        if isinstance(node, ast.Attribute):
            base = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id if isinstance(node.value, ast.Name) else None
            reps = {node.attr}
            if base:
                reps.add(f"{base}.{node.attr}")
            return reps
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Tuple) and all(isinstance(e, ast.Constant) for e in node.elts):
            return {repr(tuple(e.value for e in node.elts))}
        return None

    def _class_read_names(self):
        """UPPERCASE names read through a class object (cls.X, klass.X, SomeClass.X, type(self).X)
        anywhere in sqlglot: such reads bypass the instance hook used to record reads."""
        if self._class_reads is None and NAMES_FILE.exists():
            self._class_reads = set(json.loads(NAMES_FILE.read_text())["class_reads"])
        if self._class_reads is None:
            out = set()
            for rel in self.py_files():
                if not rel.startswith("sqlglot/"):
                    continue
                try:
                    tree = ast.parse(self.read(rel) or "")
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Attribute) and n.attr.isupper() and len(n.attr) > 1:
                        v = n.value
                        if isinstance(v, ast.Name) and (v.id in ("cls", "klass", "mcs", "metacls") or v.id[:1].isupper()):
                            out.add(n.attr)
                        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "type":
                            out.add(n.attr)
                        elif isinstance(v, ast.Attribute) and v.attr in ("__class__",) :
                            out.add(n.attr)
                        elif isinstance(v, ast.Attribute) and v.attr[:1].isupper() and not v.attr.isupper():
                            out.add(n.attr)  # module.Class.X
            self._class_reads = out
        return self._class_reads

    def attr_read_tests(self, rel, cls, old_nodes, new_nodes):
        if not self.cov.ar or len(old_nodes) != len(new_nodes):
            return None
        mod = rel[:-3].replace("/", ".")
        qual = ".".join(part.split("#")[0] for part in cls.split("."))
        me = f"{mod}:{qual}"
        out = set()
        changed_any = False
        for a, b in zip(old_nodes, new_nodes):
            if ast.dump(a) == ast.dump(b):
                continue
            changed_any = True
            names = []
            for st in (a, b):
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    names.append(st.targets[0].id)
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    names.append(st.target.id)
                else:
                    return None
            if names[0] != names[1]:
                return None
            name = names[0]
            if not name.isupper() or name in self._derived_names() or name in self._class_read_names():
                return None
            got = self.cov.ar.get(f"{me}|{name}")
            if not got:
                return None  # never read through an instance: something else consumes it
            out.update(got)
        return out if changed_any else None

    def dict_key_tests(self, rel, cls, old_nodes, new_nodes):
        if not self.cov.dk or len(old_nodes) != len(new_nodes):
            return None
        mod = rel[:-3].replace("/", ".")
        qual = ".".join(part.split("#")[0] for part in cls.split("."))
        me = f"{mod}:{qual}"
        owners = [o for o, m in self.cov.dk["mro"].items() if me in m]
        if not owners:
            return None
        out = set()
        changed_any = False
        for a, b in zip(old_nodes, new_nodes):
            if ast.dump(a) == ast.dump(b):
                continue
            changed_any = True
            def parts(st):
                if not isinstance(st, (ast.Assign, ast.AnnAssign)):
                    return None
                tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                if isinstance(st, ast.Assign) and len(st.targets) != 1:
                    return None
                if not isinstance(tg, ast.Name) or not isinstance(st.value, ast.Dict):
                    return None
                return tg.id, st.value
            pa, pb = parts(a), parts(b)
            if pa is None or pb is None or pa[0] != pb[0]:
                return None
            name = pa[0]
            if name in self._derived_names():
                return None
            def entries(d):
                plain, unpack = {}, []
                for k, v in zip(d.keys, d.values):
                    if k is None:
                        unpack.append(ast.dump(v))
                    else:
                        plain[ast.dump(k)] = (k, ast.dump(v))
                return plain, unpack
            (ea, ua), (eb, ub) = entries(pa[1]), entries(pb[1])
            if ua != ub:
                return None
            keys = [kd for kd in set(ea) | set(eb) if ea.get(kd, (None, None))[1] != eb.get(kd, (None, None))[1]]
            added = any(kd not in ea for kd in keys)
            recs = [(self.cov.dk["look"].get(f"{o}|{name}", {}), self.cov.dk["whole"].get(f"{o}|{name}", [])) for o in owners]
            if not any(lk or wh for lk, wh in recs):
                return None  # never read at run time: only through something built at import
            for lk, wh in recs:
                out.update(wh)
                if added:
                    for ts in lk.values():
                        out.update(ts)
                    continue
                for kd in keys:
                    k = (ea.get(kd) or eb.get(kd))[0]
                    reps = self._key_reps(k)
                    if reps is None:
                        return None
                    for r in reps:
                        out.update(lk.get(r, ()))
        return out if changed_any else None

    def cmp_refine(self, rel, old_text, new_text, name, got):
        """Drop tests that evaluated the swapped comparison and never saw old/new disagree."""
        try:
            ro, rn = self._raw_units
            swap = single_cmp_swap(ro[name][1], rn[name][1])
        except Exception:
            return got
        if swap is None:
            return got
        line, col, op_old, op_new = swap
        if line in self.cov.unstable.get(rel, set()):
            return got
        fam = CMP_FAMILY[op_old]
        if op_new not in fam:
            return got
        rec = self.cov.cmp.get(f"{rel}:{line}:{col}")
        if rec is None:
            return got  # never recorded: keep coverage's answer
        i, j = fam.index(op_old), fam.index(op_new)
        seen, differ = set(), set()
        for vec_s, tests in rec.items():
            vec = json.loads(vec_s)
            seen.update(tests)
            if vec[i] != vec[j]:
                differ.update(tests)
        return got - (seen - differ)

    def class_attr_change(self, rel, ou, cls, old_nodes, new_nodes, touched):
        """A class-level statement changed (e.g. a FUNCTIONS dict entry or WEEK_OFFSET)."""
        lam = self.lambda_body_tests(rel, old_nodes, new_nodes, touched)
        if lam is not None:
            return lam
        new_d = {ast.dump(n) for n in new_nodes}
        changed = [n for n in old_nodes if ast.dump(n) not in new_d]
        # 2.-4. below
        return self._class_attr_rest(rel, ou, cls, old_nodes, new_nodes, changed)

    def lambda_body_tests(self, rel, old_nodes, new_nodes, touched):
        """Edits that sit entirely inside lambda bodies run at call time: the tests that ran those
        body lines (runtime coverage) are exact. The statements must be identical once every lambda
        body is blanked out (so keys, arguments and defaults on the same line are unchanged). None
        when that fails, or a changed body also ran while modules imported (its result is baked in)."""
        import copy
        if len(new_nodes) != len(old_nodes) or not old_nodes:
            return None

        def blank(n):
            n2 = copy.deepcopy(n)
            for x in ast.walk(n2):
                if isinstance(x, ast.Lambda):
                    x.body = ast.Constant(value=0)
            return ast.dump(n2)

        lam_lines = set()
        for o, nw in zip(old_nodes, new_nodes):
            if ast.dump(o) == ast.dump(nw):
                continue
            if blank(o) != blank(nw):
                return None
            lo = [x for x in ast.walk(o) if isinstance(x, ast.Lambda)]
            ln = [x for x in ast.walk(nw) if isinstance(x, ast.Lambda)]
            if len(lo) != len(ln):
                return None
            for a, b in zip(lo, ln):
                if ast.dump(a.body) != ast.dump(b.body):
                    lam_lines.update(range(a.body.lineno, a.body.end_lineno + 1))
        if not lam_lines or lam_lines & self.cov.eager.get(rel, set()):
            return None
        per = (self.cov.runtime or self.cov).file(rel)
        got = set()
        for l in lam_lines:
            got.update(per.get(l, ()))
        return got

    def _class_attr_rest(self, rel, ou, cls, old_nodes, new_nodes, changed):
        # 2. a class-level dict literal where only entries changed: the tests that looked up a changed
        #    key (or read the whole dict) at run time
        dk = self.dict_key_tests(rel, cls, old_nodes, new_nodes)
        if dk is not None:
            return dk
        # 3. plain settings (NAME = value) read through instances: the tests whose reads resolved to
        #    this class's definition
        ar = self.attr_read_tests(rel, cls, old_nodes, new_nodes)
        if ar is not None:
            return ar
        # 4. data read elsewhere: every test that created this class (or a subclass)
        inst = self.class_users(rel, cls)
        if inst is not None:
            return inst | self.class_scope(rel, ou, cls)
        # 3. class never instantiated in the tests (mixin, metaclass, helper): be generous
        chain = cls.split(".")
        names = self.changed_names(changed, [])
        return (self.class_scope(rel, ou, chain[0]) | self.references({chain[0]})
                | (self.references(names) if names else set()))

    def changed_names(self, old_nodes, new_nodes):
        old_d = {ast.dump(n) for n in old_nodes}
        new_d = {ast.dump(n) for n in new_nodes}
        names = set()
        for n in list(old_nodes) + list(new_nodes):
            if ast.dump(n) in old_d and ast.dump(n) in new_d:
                continue
            for t in ast.walk(n):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    names.add(t.id)
                elif isinstance(t, ast.alias):
                    names.add((t.asname or t.name).split(".")[0])
        names.discard("__all__")
        return {n for n in names if len(n) > 2}

    def module_importers(self, rel):
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        last = mod.split(".")[-1]
        return self.references({last})

    CHECK = (
        "import importlib, json, pkgutil, sys\n"
        "mods, dialects = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n"
        "bad = []\n"
        "try:\n"
        "    import sqlglot\n"
        "except BaseException as e:\n"
        "    print(json.dumps(['sqlglot'])); sys.exit(0)\n"
        "if mods == 'ALL':\n"
        "    mods = [i.name for i in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.') if i.name != 'sqlglot.__main__']\n"
        "for m in mods:\n"
        "    try: importlib.import_module(m)\n"
        "    except BaseException: bad.append(m)\n"
        "try:\n"
        "    from sqlglot.dialects.dialect import Dialect\n"
        "    items = sorted(Dialect.classes.items()) if dialects == 'ALL' else [(k, Dialect.get_or_raise(k).__class__) for k in dialects]\n"
        "    for k, c in items:\n"
        "        try:\n"
        "            d = c(); d.tokenizer; d.parser(); d.generator()\n"
        "        except BaseException: bad.append('dialect:' + k)\n"
        "except BaseException: bad.append('dialects')\n"
        "print(json.dumps(sorted(bad)))\n"
    )

    def _run_check(self, mods="ALL", dialects="ALL"):
        import subprocess
        env = dict(os.environ, PYTHONPATH=self._overlay, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", self.CHECK, json.dumps(mods), json.dumps(dialects)],
                           cwd=self._overlay, env=env, capture_output=True, text=True, timeout=60)
        lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        if r.returncode != 0 or not lines:
            return None
        return set(json.loads(lines[-1]))

    def _check_scope(self, files):
        """Core (eagerly imported) modules: import everything and build every dialect.
        A lazily imported module: import it and build the dialect it belongs to."""
        if any(rel in EAGER_MODULES or not rel.startswith("sqlglot/") for rel in files):
            return "ALL", "ALL"
        mods, dialects = [], []
        for rel in files:
            mod = rel[:-3].replace("/", ".")
            mods.append(mod[: -len(".__init__")] if mod.endswith(".__init__") else mod)
            stem = rel.rsplit("/", 1)[-1][:-3]
            if stem in DIALECT_NAMES:
                dialects.append(stem)
        return mods, dialects

    def import_ok(self, files):
        """Apply the new texts to a scratch copy of sqlglot; import every module and build every
        dialect. Anything that breaks compared with the unpatched copy means a broken import."""
        import shutil, tempfile
        try:
            if self._overlay is None:
                self._overlay = tempfile.mkdtemp(prefix="br_")
                shutil.copytree(self.src / "sqlglot", os.path.join(self._overlay, "sqlglot"))
            if self._baseline == "unset":
                self._baseline = self._run_check()
                log(f"import check baseline: {self._baseline}")
            if self._baseline is None:
                return False
            for rel, text in files.items():
                with open(os.path.join(self._overlay, rel), "w") as fh:
                    fh.write(text)
            mods, dialects = self._check_scope(files)
            got = self._run_check(mods, dialects)
            log(f"import check on {sorted(files)}: {'ok' if got is not None and got <= self._baseline else 'BROKEN'}")
            return got is not None and got <= self._baseline
        except Exception as e:
            log(f"import check error {e!r}")
            return False
        finally:
            for rel in files:
                src = self.src / rel
                dst = os.path.join(self._overlay, rel) if self._overlay else None
                if dst and src.exists():
                    shutil.copy2(src, dst)

    def select(self, patch_text):
        self.import_time_files = {}
        self.func_picked = set()
        self.changed_lines = {}
        self.nonfunc_change = False
        self.patched_texts = {}
        picked = self._select(patch_text)
        files = [f["old"] or f["new"] for f in parse_patch(patch_text)]
        py = all(f.startswith("sqlglot/") and f.endswith(".py") for f in files)
        if self.import_time_files and not self.import_ok(self.import_time_files):
            rels = list(self.import_time_files)
            if (len(files) == 1 and rels == files and files[0].startswith("sqlglot/") and files[0].endswith(".py")
                    and files[0] not in EAGER_MODULES and not self.cov.eager.get(files[0])):
                # a lazily imported module no longer imports: only the tests that import it (they ran
                # its import-time lines in their own process) can fail, plus child-process tests
                rel = files[0]
                rt = self.runtime_lines(rel)
                importers = set()
                for ln, ts in self.cov.file(rel).items():
                    if ln not in rt:
                        importers.update(ts)
                if importers:
                    log(f"import fails in lazily imported {rel}: {len(importers)} importers")
                    return importers | self.subprocess_tests
            log("import fails after this patch: selecting everything")
            return set(self.all)
        # test_generator_memory: generator construction only (see GENMEM)
        gm = self.num_of.get(GENMEM)
        if gm in picked and gm not in self.func_picked and not any(f.startswith(GENMEM_FILES) for f in files):
            picked.discard(gm)
        if gm in picked and py and self.genmem_proof(files):
            log("generator_memory check passed on the patched code: dropping it")
            picked.discard(gm)
        td = self.num_of.get(TPCDS)
        if td in picked and py:
            if not self.nonfunc_change and not self.import_time_files and self.tpcds_proof():
                log("tpcds proof: unchanged, dropping test_execute_tpcds")
                picked.discard(td)
            elif self.nonfunc_change and all(is_dialect_module(f) for f in files) and self.tpcds_proof(all_early=True):
                # class-level edits in dialect-specific modules: planning/execution never use these
                # classes, so equal early-stage outputs for every entry prove the test unchanged
                log("tpcds proof (dialect class data): unchanged, dropping test_execute_tpcds")
                picked.discard(td)
        mx = self.num_of.get(MAXNODES)
        if mx in picked and py and self.maxnodes_proof():
            log("max_nodes proof: replica unchanged, dropping test_max_nodes")
            picked.discard(mx)
        return picked

    def _select(self, patch_text):
        picked = set()
        for f in parse_patch(patch_text):
            rel = f["old"] or f["new"]
            path_new = f["new"]
            if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                old_text = self.read(f["old"]) if f["old"] else ""
                if old_text is None:
                    old_text = ""
                new_text = apply_hunks(old_text, f["hunks"]) if path_new else ""
                if f["old"] is None:
                    # brand-new module: only reachable through edits elsewhere,
                    # but a new test-dir conftest/helper could matter
                    if rel.startswith("tests/") and rel.endswith("conftest.py"):
                        picked |= self.all
                    continue
                if path_new is None:
                    picked |= self.file_scope(rel) | self.module_importers(rel)
                    continue
                self.patched_texts[rel] = new_text
                picked |= self.python_change(rel, old_text, new_text, f["hunks"])
            elif rel.startswith("tests/fixtures/") or rel.startswith("tests/"):
                got = set(self.cov.fix.get(rel, ()))
                if rel in self.cov.fix_setup and not any(rel.startswith(p) for p in SETUP_FIXTURE_USERS):
                    got |= set(self.cov.fix_setup[rel])
                # files loaded once in TestExecutor.setUpClass: credit went to its first test only
                for prefix, ids in SETUP_FIXTURE_USERS.items():
                    if rel.startswith(prefix):
                        # executor tests credited only because they ran setUpClass first
                        got = {n for n in got if not self.tests_by_num[n]["id"].startswith(_EX)}
                        got |= {self.num_of[i] for i in ids if i in self.num_of}
                if rel in TPC_QUERY_FILES and f["old"] and f["new"]:
                    got = self.tpc_queries(rel, f["hunks"], got)
                if got and not rel.endswith(".sql") and not any(rel.startswith(p) for p in SETUP_FIXTURE_USERS):
                    # other data files: whole classes of the tests that read them
                    for n in list(got):
                        got |= self.by_class.get(self.cls_of[n], set())
                if not got:
                    base = rel.split("/")[-1]
                    stem = base.split(".")[0]
                    got = self.references({stem}) if len(stem) > 2 else set()
                    if not got:
                        got = set(self.all)
                picked |= got
            elif rel.endswith((".md", ".rst", ".txt")) or rel.startswith(("docs/", ".github/")):
                continue
            else:
                picked |= set(self.all)
        return picked


# ---------------------------------------------------------------- splitting
def split(numbers, dur):
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: dur[n], reverse=True)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in items:
            j = loads.index(min(loads))
            ws[j].append(n)
            loads[j] += dur[n]
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    cov = CovMap(HERE / "covmap.json.gz")
    sel = Selector(tests, cov, SRC)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            picked = sel.select(patch.read_text(errors="replace"))
        except Exception as e:  # never crash: fall back to everything
            log(f"{patch.stem}: error {e!r}, running all")
            picked = set(sel.all)
        picked &= sel.all
        workers = split(picked, dur)
        print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
        log(f"{patch.stem}: {len(picked)} tests, {len(workers)} workers")


if __name__ == "__main__":
    main()
