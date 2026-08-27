#!/usr/bin/env python3
"""Refuse new or raised LEGACY_OVERSIZED entries versus a git merge base.

The on-disk mapping in tests/test_module_size_ratchet.py is not a baseline by
itself: a PR can add a key or raise a cap in the same change and the in-repo
tests still pass. This script compares the current mapping to the file at
``--base-ref`` (default ``origin/main``).
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CURRENT_FILE = REPO_ROOT / "tests" / "test_module_size_ratchet.py"
BASELINE_RELATIVE_PATH = "tests/test_module_size_ratchet.py"


def _is_legacy_target(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "LEGACY_OVERSIZED"


def parse_legacy_oversized(source: str) -> dict[str, int]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(_is_legacy_target(target) for target in node.targets):
            mapping = ast.literal_eval(node.value)
            return {str(key): int(value) for key, value in mapping.items()}
        if isinstance(node, ast.AnnAssign) and _is_legacy_target(node.target) and node.value is not None:
            mapping = ast.literal_eval(node.value)
            return {str(key): int(value) for key, value in mapping.items()}
    raise ValueError("LEGACY_OVERSIZED assignment not found")


def _git_toplevel(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return REPO_ROOT
    return Path(result.stdout.strip())


def _missing_at_ref(stderr: str) -> bool:
    lowered = stderr.lower()
    return "does not exist" in lowered or "exists on disk, but not in" in lowered


def read_baseline(base_ref: str, repo_root: Path) -> tuple[dict[str, int], bool]:
    result = subprocess.run(
        ["git", "show", f"{base_ref}:{BASELINE_RELATIVE_PATH}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if _missing_at_ref(result.stderr):
            return {}, True
        sys.stderr.write(result.stderr)
        raise SystemExit(1)
    return parse_legacy_oversized(result.stdout), False


def compare(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    violations: list[str] = []
    for key in sorted(current):
        if key not in baseline:
            violations.append(f"{key}: added allowlist entry")
            continue
        if current[key] > baseline[key]:
            violations.append(f"{key}: cap raised from {baseline[key]} to {current[key]}")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare LEGACY_OVERSIZED against the merge-base mapping.")
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--current-file", default=str(DEFAULT_CURRENT_FILE))
    args = parser.parse_args(argv)

    current_path = Path(args.current_file)
    current = parse_legacy_oversized(current_path.read_text(encoding="utf-8"))
    repo_root = _git_toplevel(current_path.resolve().parent)
    baseline, missing = read_baseline(args.base_ref, repo_root)
    if missing:
        print(f"size ratchet baseline: {BASELINE_RELATIVE_PATH} missing at {args.base_ref}; treating baseline as empty")
        print(f"size ratchet baseline: ok ({len(current)} entries)")
        return 0

    violations = compare(current, baseline)
    if violations:
        for line in violations:
            print(line)
        return 1
    print(f"size ratchet baseline: ok ({len(current)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
