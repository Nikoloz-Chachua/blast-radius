"""Run tests/test_generator.py::TestGenerator::test_generator_memory's loop for the dialects whose
generator (or dialect) classes come from the given modules. Prints {"ok": bool, "dialects": n}."""
import gc, json, sys, tracemalloc


def main():
    mods = set(json.loads(sys.argv[1]))
    limit = int(sys.argv[2])
    from sqlglot.dialects import Dialects
    from sqlglot.dialects.dialect import Dialect

    todo = []
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        classes = list(type(dialect).__mro__) + list(dialect.generator_class.__mro__)
        if any(c.__module__ in mods for c in classes):
            todo.append(dialect)
    if len(todo) > limit:
        print(json.dumps({"ok": False, "dialects": len(todo), "skipped": True}))
        return
    for dialect in todo:
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if not after - before < 1024:
            print(json.dumps({"ok": False, "dialects": len(todo)}))
            return
    print(json.dumps({"ok": True, "dialects": len(todo)}))


if __name__ == "__main__":
    main()
