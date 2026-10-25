#!/usr/bin/env python3
"""Strict replay-compatible PR test selection. Unknown tests default to B."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Sequence

from scripts.windows_affected_tests import SHARED_PATHS, imports_changed, module_name

MANIFEST = Path(__file__).with_name("pr_test_tiers.json")
SELECTION_INFRASTRUCTURE = {
    "scripts/pr_test_tiers.json",
    "scripts/pr_tier_selection.py",
    "scripts/pr_tier_shadow.py",
}


def select_tests(repo: Path, changed: Sequence[str]) -> dict:
    """Return portable paths, per-file reasons and the inputs needed to replay."""
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    a, c = set(manifest["A"]), set(manifest["C"])
    files = {path.relative_to(repo).as_posix(): path for path in (repo / "tests").rglob("test_*.py") if path.is_file()}
    changed_set = set(changed)
    modules = {module for path in changed_set if (module := module_name(path)) is not None}
    reasons: dict[str, list[str]] = {}
    for name, path in sorted(files.items()):
        why = []
        if name in a:
            why.append("tier-a")
        if name in changed_set:
            why.append("changed")
        if name not in c and modules and imports_changed(path.read_text(encoding="utf-8"), modules):
            why.append("import")
        if why:
            reasons[name] = why
    initial_count = len(reasons)
    shared = bool(changed_set & (SHARED_PATHS | SELECTION_INFRASTRUCTURE)) or any(
        PurePosixPath(path).name == "conftest.py"
        or (path.startswith("tests/") and not (PurePosixPath(path).name.startswith("test_") and path.endswith(".py")))
        or (path.startswith("src/brigade/") and not path.endswith(".py"))
        for path in changed_set
    )
    fallback = "shared" if shared else "over-250" if initial_count > 250 else None
    if fallback:
        for name in sorted(files):
            if name not in c:
                reasons.setdefault(name, []).append("fallback:" + fallback)
    return {
        "selected": sorted(reasons),
        "reasons": reasons,
        "tiers": {name: "A" if name in a else "C" if name in c else "B" for name in sorted(files)},
        "changed": sorted(changed_set),
        "initial_count": initial_count,
        "fallback": fallback,
        "tier_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
