#!/usr/bin/env python3
"""Linux PR test selection plan, for SHADOW MODE only.

Given a base/head diff, compute which test files a selective run would execute
and why. CI still runs every test. `ci_shadow_report.py` joins this plan with
the shard junit results to record whether any failing test fell outside it (an
"escape"). Nothing here skips a test or gates a check.

Rules, in order of authority:

* Fail open. Any error, any changed path the selector cannot map, and any
  run-everything trigger makes the plan `mode: full`.
* Incomplete evidence never authorizes a skip. An unmapped path is a reason to
  run everything, not a reason to run nothing.
* Every selected file carries at least one reason.
* Heuristics may only add tests. Nothing removes a test the rules selected.

How a selective plan is built (see docs/adaptive-ci/shadow-selection.md):

1. A changed test file is selected.
2. A changed `src/brigade` or `scripts` module selects every test file whose
   transitive static import closure (src, scripts and test-support modules,
   including lazy in-function imports, `brigade.x` string references and
   conftest imports) reaches it, or whose text names the module path.
3. A changed doc, workflow or changelog fragment selects tests that mention its
   path, its file name or its top-level directory.
4. A fixed set of whole-tree invariant tests is always selected.

Every plan records `selector_digest`, a SHA-256 over this file and `ci_selection_core.py`, so
the evaluator can drop runs made by an earlier version of the selector.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Sequence

# The workflow runs this file directly, while tests import it as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ci_selection_core import discover_tests

SCHEMA_VERSION = 1
# Both pytest default patterns: `tests/repeat_watch_approval_test.py` is collected.
TEST_PATTERNS = ("test_*.py", "*_test.py")

# Any change here can alter how every test runs or how this plan is built.
FULL_PATHS = {
    "pyproject.toml",
    ".github/workflows/ci.yml",
    "scripts/ci_pytest_shard.py",
    "scripts/ci_selection_core.py",
    "scripts/ci_test_selection.py",
    "scripts/ci_shadow_report.py",
    "src/brigade/__init__.py",
}
# Documentation-like areas where a change can only matter to a test that reads it.
TEXT_AREAS = ("docs/", "changelog.d/", ".github/")
# Tests that scan the whole source tree, so no import edge predicts them.
INVARIANT_TESTS = (
    "tests/test_module_size_ratchet.py",
    "tests/test_mypy_override_ratchet.py",
    "tests/test_posix_facade_guard.py",
    "tests/test_phase257_facades.py",
    "tests/test_verify_brigade_skill.py",
    "tests/test_retired_engine_names.py",
)
CLI_SUBPROCESS = re.compile(r"""["']-m["']\s*,\s*["']brigade["']|-m brigade\b|\[\s*["']brigade["']\s*,\s*["'][a-z]""")
# Selection policies, widest last. A `closure:K` reason means the test reaches a changed module
# through K deferred (in-function) imports; a policy keeps closures with K <= limit. Every other
# reason (changed, imports, references, invariant) is kept by every policy. The plan lists the
# widest policy so one run scores all of them.
POLICIES: dict[str, int | None] = {"import-time": 0, "lazy-1": 1, "full-closure": None}
DOTTED = re.compile(r"^brigade(?:\.\w+)+(?::\w+)?$")
# Files whose contents define how a plan is built, including FULL_PATHS, INVARIANT_TESTS and POLICIES above.
SELECTOR_SOURCES = ("ci_test_selection.py", "ci_selection_core.py")


def selector_digest() -> str:
    """SHA-256 over the selector's own source, so plans from different selector versions can be told apart."""
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in SELECTOR_SOURCES:
        digest.update(name.encode() + b"\0")
        # Normalize line endings so a Windows checkout with autocrlf hashes like Linux.
        digest.update((here / name).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def policy_files(plan: dict, policy: str) -> set[str]:
    """Test files a policy would run for this plan. Full mode, or any unknown policy, runs everything."""
    if plan.get("mode") != "selective" or policy not in POLICIES:
        return set(plan.get("selected", {}))
    limit = POLICIES[policy]
    keep = set()
    for name, reasons in plan["selected"].items():
        closures = [int(r.split(":")[1]) for r in reasons if r.startswith("closure:")]
        if len(closures) < len(reasons) or limit is None or min(closures) <= limit:
            keep.add(name)
    return keep


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/") and name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    )


def classify(path: str) -> tuple[str, str]:
    """Return (kind, detail). kind `full` carries the run-everything reason."""
    name = PurePosixPath(path).name
    if path in FULL_PATHS:
        return "full", f"shared-infra:{path}"
    if name == "conftest.py":
        return "full", f"conftest:{path}"
    if path.startswith("tests/"):
        return ("test", "") if is_test_file(path) else ("full", f"test-support:{path}")
    if path.startswith("src/brigade/"):
        return ("module", "") if path.endswith(".py") else ("full", f"src-data:{path}")
    if path.startswith("scripts/"):
        return "script", ""
    if path.startswith(TEXT_AREAS) or ("/" not in path and path.endswith(".md")):
        return "text", ""
    return "full", f"unmapped-path:{path}"


def dotted_name(path: str, root: str, prefix: str) -> tuple[str, bool]:
    """`src/brigade/a/b.py` -> (`brigade.a.b`, False); package `__init__` -> (pkg, True)."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    parts = parts[len(PurePosixPath(root).parts) :] if root else parts
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join([prefix, *parts] if prefix else parts), is_package


class Graph:
    """Static import graph over src, scripts and tests."""

    def __init__(self, repo: Path, test_files: dict[str, Path]) -> None:
        self.repo = repo
        self.paths: dict[str, Path] = {}  # node -> file
        self.is_package: dict[str, bool] = {}
        self.text: dict[str, str] = {}  # test node -> source text
        self.test_nodes: dict[str, str] = {}  # `tests/x.py` -> node
        sources: list[tuple[Path, str, str]] = []
        for base, prefix in (("src/brigade", "brigade"), ("scripts", "scripts")):
            for path in sorted((repo / base).rglob("*.py")):
                sources.append((path, base, prefix))
        for path in sorted((repo / "tests").rglob("*.py")):
            sources.append((path, "tests", "tests"))
        for path, base, prefix in sources:
            rel = path.relative_to(repo).as_posix()
            node, package = dotted_name(rel, base, prefix if prefix != "tests" else "tests")
            if prefix == "brigade" and rel == "src/brigade/__init__.py":
                node, package = "brigade", True
            self.paths[node] = path
            self.is_package[node] = package
            if prefix == "tests":
                self.test_nodes[rel] = node
        self.top: dict[str, set[str]] = defaultdict(set)  # import-time (or test-run-time) edges
        self.lazy: dict[str, set[str]] = defaultdict(set)  # imports deferred into a function body
        self.parse_errors: list[str] = []
        for node, path in self.paths.items():
            self._read(node, path)
        # conftest imports apply to every test at or below its directory.
        for rel, node in self.test_nodes.items():
            for conftest in self._conftests_above(rel):
                self.top[node].add(conftest)

    def _conftests_above(self, rel: str) -> list[str]:
        parts = PurePosixPath(rel).parts[:-1]
        found = []
        for depth in range(1, len(parts) + 1):
            candidate = "/".join([*parts[:depth], "conftest.py"])
            if candidate in self.test_nodes:
                found.append(self.test_nodes[candidate])
        return found

    def resolve(self, name: str, importer: str | None = None) -> set[str]:
        """Existing modules executed by importing `name` (itself plus parent packages)."""
        parts = name.split(".")
        found = {".".join(parts[:i]) for i in range(1, len(parts) + 1)} & self.paths.keys()
        if importer is not None and not found and parts[0] not in {"brigade", "scripts", "tests"}:
            # Plain `import helper`: pytest puts the test file's directory on sys.path.
            package = importer if self.is_package[importer] else importer.rpartition(".")[0]
            for base in (package, "tests"):
                local = f"{base}.{name}" if base else name
                found |= {
                    ".".join(local.split(".")[:i]) for i in range(1, len(local.split(".")) + 1)
                } & self.paths.keys()
        return found

    def _read(self, node: str, path: Path) -> None:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeError) as exc:
            # An unreadable test, src or script file makes the whole plan full. Data files that
            # merely end in .py under tests/ (deliberately broken fixtures) do not.
            if not node.startswith("tests.") or is_test_file(path.relative_to(self.repo).as_posix()):
                self.parse_errors.append(f"{path.name}: {type(exc).__name__}")
            return
        if node.startswith("tests."):
            self.text[node] = source
        package = node if self.is_package[node] else node.rpartition(".")[0]
        strings = {i.value for i in ast.walk(tree) if isinstance(i, ast.Constant) and isinstance(i.value, str)}
        is_test = node.startswith("tests.")
        top: set[str] = set()
        lazy: set[str] = set()

        def add(deps: set[str], deferred: bool) -> None:
            # Test code runs when the test does, so a test's own imports are never deferred.
            (lazy if deferred and not is_test else top).update(deps)

        def visit(item: ast.AST, deferred: bool) -> None:
            if isinstance(item, ast.Import):
                for alias in item.names:
                    add(self.resolve(alias.name, node), deferred)
            elif isinstance(item, ast.ImportFrom):
                if item.level:
                    base = package.split(".") if package else []
                    base = base[: len(base) - (item.level - 1)] if item.level > 1 else base
                    target = ".".join([*base, *(item.module.split(".") if item.module else [])])
                else:
                    target = item.module or ""
                if target:
                    add(self.resolve(target, node), deferred)
                    for alias in item.names:
                        add(self.resolve(f"{target}.{alias.name}", node), deferred)
            elif isinstance(item, ast.Constant) and isinstance(item.value, str) and DOTTED.match(item.value):
                add(self.resolve(item.value.split(":")[0]), deferred)
            elif isinstance(item, ast.Call) and _call_name(item) in {"import_module", "__import__"} and item.args:
                add(self._dynamic(item, package, strings), deferred)
            inner = deferred or isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            for child in ast.iter_child_nodes(item):
                visit(child, inner)

        visit(tree, False)
        if is_test and CLI_SUBPROCESS.search(source):
            # Tests that run the `brigade` console script reach the whole CLI without importing it.
            top.add("brigade.cli")
        top.discard(node)
        lazy -= top | {node}
        self.top[node] = top
        self.lazy[node] = lazy

    def _dynamic(self, call: ast.Call, package: str, strings: set[str]) -> set[str]:
        """Resolve a computed import to the modules this file names, else to everything under its prefix."""
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return self.resolve(arg.value) if not arg.value.startswith(".") else set()
        prefix = "brigade"  # unknown target: any brigade module
        if isinstance(arg, ast.JoinedStr) and arg.values:
            first = arg.values[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
                dots = len(text) - len(text.lstrip("."))
                if dots:
                    base = package.split(".")
                    prefix = ".".join(base[: len(base) - (dots - 1)])
                else:
                    prefix = text.rstrip(".") if text.count(".") else "brigade"
            else:
                prefix = package  # f"{__name__}.x" and similar
        elif len(call.args) > 1 or call.keywords:
            prefix = package  # import_module(name, package)
        # Lazy-export tables name their targets as string constants in the same file.
        named = {
            f"{base}.{text.lstrip('.')}"
            for text in strings
            if text.lstrip(".").isidentifier()
            for base in {prefix, package, package.rpartition(".")[0]}
        } & self.paths.keys()
        if named:
            return named
        return {name for name in self.paths if name == prefix or name.startswith(prefix + ".")}

    def affected(self, changed: set[str]) -> tuple[set[str], dict[str, int]]:
        """Return (direct importers, {importer: fewest deferred imports on any path to a changed module}).

        Reverse breadth-first search over the import graph. A deferred import is one inside a
        function body in src or scripts, so cost 0 means reachable through import-time edges alone.
        """
        reverse: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for node in self.paths:
            for dep in self.top[node]:
                reverse[dep].append((node, 0))
            for dep in self.lazy[node]:
                reverse[dep].append((node, 1))
        direct = {parent for module in changed for parent, _ in reverse[module]}
        best: dict[str, int] = {module: 0 for module in changed}
        queue = list(changed)
        while queue:
            module = queue.pop()
            for parent, cost in reverse[module]:
                used = best[module] + cost
                if used < best.get(parent, 1 << 30):
                    best[parent] = used
                    queue.append(parent)
        return direct, best


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def full_plan(repo: Path, changed: Sequence[str], triggers: list[dict], test_files: dict[str, Path]) -> dict:
    names = sorted(f"tests/{name}" for name in test_files)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "full",
        "changed": sorted(changed),
        "full_triggers": triggers,
        "selected": {name: ["full"] for name in names},
        "test_files_total": len(names),
    }


def select(repo: Path, changed: Sequence[str], graph: Graph | None = None) -> dict:
    """Build the plan. Never raises: any failure is a full-mode plan with the error."""
    changed = sorted(set(changed))
    test_files: dict[str, Path] = {}
    try:
        test_files = discover_tests(repo, TEST_PATTERNS)
        plan = _select(repo, changed, test_files, graph)
    except Exception as exc:  # noqa: BLE001 - fail open on anything at all
        plan = full_plan(repo, changed, [{"path": "*", "reason": f"selector-error:{type(exc).__name__}"}], test_files)
        plan["error"] = f"{type(exc).__name__}: {exc}"
    plan["selector_digest"] = selector_digest()
    return plan


def _select(repo: Path, changed: list[str], test_files: dict[str, Path], graph: Graph | None = None) -> dict:
    triggers: list[dict] = []
    kinds: dict[str, list[str]] = defaultdict(list)
    for path in changed:
        kind, detail = classify(path)
        if kind == "full":
            reason, _, target = detail.partition(":")
            triggers.append({"path": target, "reason": reason})
        elif kind in ("module", "script") and path.endswith(".py") and not (repo / path).exists():
            # The graph is built from the head tree, so a deleted module has no node and an
            # importer of it (`from pkg import gone`) has no edge to follow. Run everything.
            triggers.append({"path": path, "reason": "deleted-module"})
        else:
            kinds[kind].append(path)
    if not changed:
        triggers.append({"path": "*", "reason": "empty-diff"})
    if not test_files:
        triggers.append({"path": "*", "reason": "no-tests-discovered"})
    if triggers:
        return full_plan(repo, changed, triggers, test_files)

    graph = graph or Graph(repo, test_files)
    if graph.parse_errors:
        return full_plan(repo, changed, [{"path": "*", "reason": "unparseable-source"}], test_files)
    names = {f"tests/{name}": path for name, path in test_files.items()}
    reasons: dict[str, list[str]] = defaultdict(list)

    def add(name: str, why: str) -> None:
        if name in names and why not in reasons[name]:
            reasons[name].append(why)

    for path in kinds["test"]:
        add(path, "changed")
    modules: dict[str, str] = {}  # node -> changed path
    for path in kinds["module"] + kinds["script"]:
        if path.endswith(".py"):
            base, prefix = ("src/brigade", "brigade") if path.startswith("src/") else ("scripts", "scripts")
            node, _ = dotted_name(path, base, prefix)
            modules[node] = path
    direct, best = graph.affected(set(modules))
    for rel, node in graph.test_nodes.items():
        if rel in names and node in best:
            add(rel, "imports" if node in direct else f"closure:{best[node]}")
    # Textual references: path mentions catch tests that read or exec a file without importing it.
    for node, path in modules.items():
        tokens = {path.removeprefix("src/"), path}
        for rel, test_node in graph.test_nodes.items():
            text = graph.text.get(test_node, "")
            if rel in names and (node in text or any(token in text for token in tokens)):
                add(rel, f"references:{path}")
    for path in kinds["script"] + kinds["text"]:
        tokens = {path, PurePosixPath(path).name}
        if path.startswith(TEXT_AREAS):
            tokens.add(PurePosixPath(path).parts[0])
        for rel, test_node in graph.test_nodes.items():
            text = graph.text.get(test_node, "")
            if rel in names and any(token in text for token in tokens):
                add(rel, f"references:{path}")
    for invariant in INVARIANT_TESTS:
        add(invariant, "invariant")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "selective",
        "changed": changed,
        "full_triggers": [],
        "selected": {name: sorted(why) for name, why in sorted(reasons.items())},
        "test_files_total": len(names),
    }


def git_changed(repo: Path, base: str, head: str) -> list[str]:
    # --no-renames reports a move as a delete plus an add, so old and new paths both map.
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return sorted(path for path in out.split("\0") if path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        changed = git_changed(args.repo, args.base, args.head)
        plan = select(args.repo, changed)
    except Exception as exc:  # noqa: BLE001 - a plan failure must never fail the job
        plan = {
            "schema_version": SCHEMA_VERSION,
            "mode": "full",
            "changed": [],
            "full_triggers": [{"path": "*", "reason": f"diff-error:{type(exc).__name__}"}],
            "selected": {},
            "test_files_total": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "selector_digest": selector_digest(),
        }
    plan["base"], plan["head"] = args.base, args.head
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"shadow plan: mode={plan['mode']} selected={len(plan['selected'])}/{plan['test_files_total']} "
        f"changed={len(plan['changed'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
