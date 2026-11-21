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
            # broken module: everything that uses or imports it
            return self.file_scope(rel) | self.module_importers(rel)
        ou, nu = units(canon(old_tree)), units(canon(new_tree))
        touched = changed_old_lines(hunks)
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
                if other is not None and sig_dump(node) == sig_dump(other[1]):
                    lines = {l for l in touched if rng[0] <= l <= rng[1]}
                    want = self.stmt_lines(node, lines) if lines else None
                    if want is not None:
                        got = set()
                        per = self.cov.file(rel)
                        for l in want:
                            got.update(per.get(l, ()))
                picked |= whole if got is None else got
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

    def select(self, patch_text):
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
