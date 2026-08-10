"""Catch class attributes that shadow a builtin used in an annotation.

`def list(...)` inside a class makes a later `-> list[X]` annotation resolve to
the method, which fails at class-creation time with "'function' object is not
subscriptable". compileall does not catch it; this does.
"""
import ast
import builtins
import os
import sys

BUILTINS = set(dir(builtins))
problems = []

for dirpath, _dirs, files in os.walk("lox"):
    if "__pycache__" in dirpath:
        continue
    for name in sorted(f for f in files if f.endswith(".py")):
        path = os.path.join(dirpath, name)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            deferred = any(
                isinstance(n, ast.ImportFrom) and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
                for n in ast.walk(tree)
            )
            names = {
                b.name for b in node.body
                if isinstance(b, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            } & BUILTINS
            if not names:
                continue
            used = set()
            for b in node.body:
                for sub in ast.walk(b):
                    if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name):
                        used.add(sub.value.id)
            clash = names & used
            if clash and not deferred:
                problems.append(f"{path}:{node.lineno} class {node.name} defines {sorted(clash)} "
                                f"which shadows the builtin used in an annotation")
            elif names:
                problems.append(f"NOTE {path}:{node.lineno} class {node.name} shadows builtin(s) "
                                f"{sorted(names)} (safe here, but fragile)")

errors = [p for p in problems if not p.startswith("NOTE")]
for p in problems:
    print(p)
print(f"\n{len(errors)} error(s), {len(problems) - len(errors)} note(s)")
sys.exit(1 if errors else 0)
