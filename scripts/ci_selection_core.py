"""Shared primitives for CI test selectors.

`windows_affected_tests.py` (Windows PR lane) and `ci_test_selection.py` (Linux
shadow selection) both map changed source paths to tests by static import
matching. This module holds the parts they share so there is one definition of
"this test imports that module". Behavior here is pinned by
`tests/test_windows_affected_tests.py`; change it only with the Windows lane in
mind.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
from typing import Iterable


def module_name(path: str) -> str | None:
    if path == "src/brigade/__init__.py" or not path.startswith("src/brigade/") or not path.endswith(".py"):
        return None
    parts = list(PurePosixPath(path).with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_changed(source: str, modules: set[str]) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Let pytest expose broken test syntax rather than silently omit it.
        return True
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            # A named root import depends on that name, not all root siblings.
            # Explicit `import brigade` and root wildcard imports still match parents.
            if node.module != "brigade" or any(alias.name == "*" for alias in node.names):
                imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return any(
        imported == module or module.startswith(imported + ".") or imported.startswith(module + ".")
        for imported in imports
        for module in modules
    )


def discover_tests(repo: Path, patterns: Iterable[str] = ("test_*.py",)) -> dict[str, Path]:
    """Map `tests/`-relative POSIX names to files, for every pattern given."""
    tests = repo / "tests"
    found: dict[str, Path] = {}
    for pattern in patterns:
        found.update({p.relative_to(tests).as_posix(): p for p in tests.rglob(pattern) if p.is_file()})
    return found
