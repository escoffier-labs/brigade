"""Entries may be removed, never added; to un-ignore a module delete its pattern and fix what mypy reports."""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "mypy_override_baseline.txt"
SRC_BRIGADE = REPO_ROOT / "src" / "brigade"


def _ignored_module_patterns() -> list[str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        data = tomllib.loads(text)
        patterns: list[str] = []
        for override in data.get("tool", {}).get("mypy", {}).get("overrides", []):
            if override.get("ignore_errors") is not True:
                continue
            module = override.get("module", [])
            if isinstance(module, str):
                patterns.append(module)
            else:
                patterns.extend(str(item) for item in module)
        return patterns
    return _ignored_module_patterns_from_text(text)


def _ignored_module_patterns_from_text(text: str) -> list[str]:
    """Python 3.10 path: tomllib is 3.11+ and toml_compat cannot read multiline arrays."""
    patterns: list[str] = []
    blocks = text.split("[[tool.mypy.overrides]]")[1:]
    for block in blocks:
        nxt = block.find("\n[")
        body = block if nxt < 0 else block[:nxt]
        if "ignore_errors = true" not in body:
            continue
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if stripped.startswith('"') and stripped.endswith('"'):
                patterns.append(stripped[1:-1])
    return patterns


def _baseline_patterns() -> set[str]:
    return {line for line in BASELINE.read_text(encoding="utf-8").splitlines() if line}


def _brigade_modules_and_packages() -> set[str]:
    names: set[str] = set()
    for path in SRC_BRIGADE.rglob("*"):
        if path.name.startswith(".") or path.name == "__pycache__":
            continue
        if path.is_file() and path.suffix != ".py":
            continue
        relative = path.relative_to(SRC_BRIGADE.parent)
        if path.is_file():
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            names.add(".".join(parts))
            continue
        names.add(".".join(relative.parts))
    return names


def test_mypy_override_patterns_only_shrink() -> None:
    current = set(_ignored_module_patterns())
    extra = sorted(current - _baseline_patterns())
    assert not extra, "mypy ignore_errors patterns may be removed, never added: " + ", ".join(extra)


def test_mypy_override_patterns_still_match_src() -> None:
    names = _brigade_modules_and_packages()
    stale = [
        pattern for pattern in _ignored_module_patterns() if not any(fnmatch.fnmatch(name, pattern) for name in names)
    ]
    assert not stale, "stale mypy ignore_errors patterns (no src/brigade module or package): " + ", ".join(stale)
