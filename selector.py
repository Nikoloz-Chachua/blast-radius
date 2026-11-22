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


def units(tree):
    """Map qualified name -> (kind, node, dump, parent_class_name)."""
    result = {}

    def visit(body, prefix, cls):
        others = []
        for idx, node in enumerate(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                if name in result:
                    name = f"{name}#{idx}"
                result[name] = ("func", node, ast.dump(node), cls)
            elif isinstance(node, ast.ClassDef):
                name = f"{prefix}{node.name}"
                if name in result:
                    name = f"{name}#{idx}"
                header = ast.dump(
                    ast.ClassDef(
                        name=node.name,
                        bases=node.bases,
                        keywords=node.keywords,
                        body=[],
                        decorator_list=node.decorator_list,
                        type_params=getattr(node, "type_params", []),
                    )
                )
                result[name] = ("class", node, header, cls)
                visit(node.body, name + ".", name)
            else:
                others.append(node)
        key = f"{prefix}<body>"
        result[key] = ("body", others, "|".join(ast.dump(n) for n in others), cls)

    visit(tree.body, "", None)
    return result


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
        self._py_files = None
        self.import_time_files = {}
        self._overlay = None
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

    def class_scope(self, rel, old_units, cls):
        if cls is None:
            return self.file_scope(rel)
        node = old_units[cls][1]
        s, e = node_range(node)
        return self.cov.range(rel, s, e)

    def file_scope(self, rel):
        out = self.cov.whole_file(rel)
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
            if parent is not func:
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
        ou, nu = units(canon(old_tree)), units(canon(new_tree))
        touched = changed_old_lines(hunks)
        uns = self.cov.unstable.get(rel, set())
        import_time = bool(touched & uns) or set(ou) != set(nu) or any(
            k in ("body", "class") and nu.get(n) is not None and nu[n][2] != d
            for n, (k, _, d, _) in ou.items())
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
                whole = self.cov.range(rel, *rng)
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
                # lines that run once per process (imports, caches): coverage only credits the
                # first test, so treat the edit as affecting every user of this module/class
                uns = self.cov.unstable.get(rel, set())
                lines_here = {l for l in touched if rng[0] <= l <= rng[1]}
                if uns and (lines_here & uns or (got is not None and want and set(want) & uns)):
                    picked |= whole | self.file_scope(rel)
                    if cls:
                        picked |= self.class_users(rel, cls.split(".")[0]) or set()
                    picked |= self.setup_class_expand(picked, rel)
                if other is None:
                    # removed method: anything using its class may now dispatch differently
                    picked |= self.class_scope(rel, ou, cls)
                    if cls:
                        picked |= self.class_users(rel, cls) or set()
            elif kind == "class":
                picked |= self.class_scope(rel, ou, name)
                names_needing_refs.add(name.split(".")[-1])
                picked |= self.class_users(rel, name) or set()
            else:  # body statements of a class or module
                if cls is None:
                    picked |= self.file_scope(rel)
                    names_needing_refs |= self.changed_names(node, other[1] if other else [])
                else:
                    picked |= self.class_attr_change(rel, ou, cls, node, other[1] if other else [], touched)
        for name, (kind, node, dump, cls) in nu.items():
            if name in ou:
                continue
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

    def cmp_refine(self, rel, old_text, new_text, name, got):
        """Drop tests that evaluated the swapped comparison and never saw old/new disagree."""
        try:
            ro, rn = units(ast.parse(old_text)), units(ast.parse(new_text))
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
        new_d = {ast.dump(n) for n in new_nodes}
        changed = [n for n in old_nodes if ast.dump(n) not in new_d]
        # 1. edits that sit entirely inside lambda bodies run at call time: line coverage is exact
        lam_lines = set()
        all_in_lambda = bool(changed)
        for st in changed:
            lines = {l for l in touched if st.lineno <= l <= st.end_lineno}
            lams = [n for n in ast.walk(st) if isinstance(n, ast.Lambda)]
            for l in lines:
                inside = [lm for lm in lams if lm.body.lineno <= l <= lm.body.end_lineno]
                if not inside:
                    all_in_lambda = False
                else:
                    lm = min(inside, key=lambda x: x.end_lineno - x.lineno)
                    lam_lines.update(range(lm.body.lineno, lm.body.end_lineno + 1))
            if not lines:
                all_in_lambda = False
        if len(new_nodes) != len(old_nodes):
            all_in_lambda = False
        if all_in_lambda and lam_lines:
            per = self.cov.file(rel)
            got = set()
            for l in lam_lines:
                got.update(per.get(l, ()))
            return got
        # 2. data read elsewhere: every test that created this class (or a subclass)
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
        "bad = []\n"
        "try:\n"
        "    import sqlglot\n"
        "except BaseException as e:\n"
        "    print(json.dumps(['sqlglot'])); sys.exit(0)\n"
        "for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):\n"
        "    if info.name == 'sqlglot.__main__': continue\n"
        "    try: importlib.import_module(info.name)\n"
        "    except BaseException: bad.append(info.name)\n"
        "try:\n"
        "    from sqlglot.dialects.dialect import Dialect\n"
        "    for k, c in sorted(Dialect.classes.items()):\n"
        "        try:\n"
        "            d = c(); d.tokenizer; d.parser(); d.generator()\n"
        "        except BaseException: bad.append('dialect:' + k)\n"
        "except BaseException: bad.append('dialects')\n"
        "print(json.dumps(sorted(bad)))\n"
    )

    def _run_check(self):
        import subprocess
        env = dict(os.environ, PYTHONPATH=self._overlay, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", self.CHECK], cwd=self._overlay, env=env,
                           capture_output=True, text=True, timeout=60)
        lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        if r.returncode != 0 or not lines:
            return None
        return set(json.loads(lines[-1]))

    def import_ok(self, files):
        """Apply the new texts to a scratch copy of sqlglot; import every module and build every
        dialect. Anything that breaks compared with the unpatched copy means a broken import."""
        import shutil, tempfile
        try:
            if self._overlay is None:
                self._overlay = tempfile.mkdtemp(prefix="br_")
                shutil.copytree(self.src / "sqlglot", os.path.join(self._overlay, "sqlglot"))
                self._baseline = self._run_check()
                log(f"import check baseline: {self._baseline}")
            if self._baseline is None:
                return False
            for rel, text in files.items():
                with open(os.path.join(self._overlay, rel), "w") as fh:
                    fh.write(text)
            got = self._run_check()
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
        picked = self._select(patch_text)
        if self.import_time_files and not self.import_ok(self.import_time_files):
            log("import fails after this patch: selecting everything")
            return set(self.all)
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
                picked |= self.python_change(rel, old_text, new_text, f["hunks"])
            elif rel.startswith("tests/fixtures/") or rel.startswith("tests/"):
                got = set(self.cov.fix.get(rel, ()))
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
