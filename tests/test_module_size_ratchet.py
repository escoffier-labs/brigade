"""Entries may be lowered or removed, never added or raised. A new module over 2000 lines must be split before merge.

CI enforces the shrink-only rule against the merge base with scripts/check_size_ratchet_baseline.py.
"""

from __future__ import annotations

from pathlib import Path

CEILING = 2000
REPO_ROOT = Path(__file__).resolve().parents[1]

LEGACY_OVERSIZED: dict[str, int] = {
    "src/brigade/work_cmd/ledger.py": 6850,
    "src/brigade/aboyeur.py": 5362,
    "src/brigade/skills_cmd.py": 5305,
    "src/brigade/runs_cmd.py": 3584,
    "src/brigade/claude_hooks/runtime.py": 3389,
    "src/brigade/work_cmd/scanners.py": 3332,
    "src/brigade/research_cmd.py": 3088,
    "src/brigade/outcome_cmd.py": 2903,
    "src/brigade/run_redaction.py": 2802,
    "src/brigade/harness_profile_cmd.py": 2570,
    "src/brigade/work_cmd/verification.py": 2380,
    "src/brigade/memory_cmd.py": 2189,
    "src/brigade/care_cmd.py": 2084,
    "src/brigade/receipts_cmd.py": 2055,
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _production_module_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    for path in (REPO_ROOT / "src" / "brigade").rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        sizes[relative.as_posix()] = _line_count(path)
    return sizes


def test_modules_outside_allowlist_stay_under_ceiling() -> None:
    oversized = {
        rel: count
        for rel, count in _production_module_sizes().items()
        if rel not in LEGACY_OVERSIZED and count > CEILING
    }
    assert not oversized, (
        "modules over the 2000-line ceiling that are not in LEGACY_OVERSIZED: "
        + ", ".join(f"{rel} ({count})" for rel, count in sorted(oversized.items()))
        + "; split them before merge instead of adding allowlist entries"
    )


def test_allowlisted_modules_never_grow() -> None:
    grown = []
    for rel, cap in LEGACY_OVERSIZED.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        count = _line_count(path)
        if count > cap:
            grown.append(f"{rel} grew from {cap} to {count}")
    assert not grown, "allowlisted modules must not grow: " + "; ".join(grown)


def test_allowlist_only_shrinks() -> None:
    sizes = _production_module_sizes()
    stale = []
    for rel, cap in LEGACY_OVERSIZED.items():
        count = sizes.get(rel)
        if count is None:
            stale.append(f"{rel} is not a scanned src/brigade module; delete its LEGACY_OVERSIZED entry (cap {cap})")
            continue
        if count <= CEILING:
            stale.append(f"{rel} is {count} lines (<= {CEILING}); delete its LEGACY_OVERSIZED entry")
    assert not stale, "allowlist entries that no longer belong: " + "; ".join(stale)
