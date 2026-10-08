"""Print the definitions and calls that Python's own ast finds under a root.

Usage: python3 -I python_ast_recall.py ROOT
Output: JSON with three lists of [path, line, name]:
  defs            every function, async function and class definition, at the
                  first line of its span: the first decorator when it has one,
                  matching the graph's decorated-definition spans
  function_calls  calls lexically inside a function body
  scope_calls     module-level and class-body calls (the graph owns these
                  through the module pseudo-symbol)
Calls whose name is a Python builtin are dropped, as the extractor drops them.
"""

import ast
import builtins
import json
import os
import sys

BUILTINS = set(dir(builtins))
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    # The extractor drops a fixed list of builtin names (extractors/python.rs
    # PY_SKIP) by name alone, so a member call such as `event.set()` is dropped
    # too. Treating every builtin-named call as out of scope matches the
    # "non-builtin calls" baseline in issue #1649.
    return None if name in BUILTINS else name


def scan(rel, source, out):
    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            handle(child, in_function)

    def handle(node, in_function):
        if isinstance(node, FUNCTIONS + (ast.ClassDef,)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out["defs"].append([rel, start, node.name])
            # Decorators, bases and defaults evaluate in the enclosing scope.
            for decorator in node.decorator_list:
                handle(decorator, in_function)
            if isinstance(node, ast.ClassDef):
                for base in node.bases + node.keywords:
                    handle(base, in_function)
                body_in_function = in_function
            else:
                handle(node.args, in_function)
                body_in_function = True
            for statement in node.body:
                handle(statement, body_in_function)
            return
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name is not None:
                key = "function_calls" if in_function else "scope_calls"
                out[key].append([rel, node.lineno, name])
        walk(node, in_function)

    walk(ast.parse(source), False)


def main(root):
    out = {"defs": [], "function_calls": [], "scope_calls": []}
    for directory, names, files in os.walk(root):
        names.sort()
        for file in sorted(files):
            if file.endswith(".py"):
                path = os.path.join(directory, file)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                with open(path, encoding="utf-8") as handle:
                    scan(rel, handle.read(), out)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(sys.argv[1])
