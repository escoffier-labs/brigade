"""Rank candidate verification commands from GraphTrail affected-test impact.

``work verify plan`` can propose pytest (or similar) commands scoped to the
tests GraphTrail attributes to changed files, with hop distance and via-symbols
as evidence. The worker still chooses which command to run.

GraphTrail stays a read-only oracle. When the binary or index is missing, this
module degrades to an empty ranking (never raises) so verify planning keeps
working without the engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .. import component_bins, proc
from . import helpers

DEFAULT_AFFECTED_DEPTH = 3
# Soft cap on how many single-file candidate commands we surface.
CANDIDATE_FILE_CAP = 12

_ATTRIBUTION = (
    "tests statically attributed through incoming call edges; a lower bound, "
    "not coverage. Absence here does not mean untested."
)


def degraded_ranking(*, reason: str, changed_files: Sequence[str] | None = None) -> dict[str, Any]:
    """Empty ranking used when GraphTrail cannot enrich verify planning."""
    return {
        "degraded": True,
        "degraded_reason": reason,
        "changed_files": _unique_strings(changed_files),
        "missing_files": [],
        "depth": DEFAULT_AFFECTED_DEPTH,
        "attribution": _ATTRIBUTION,
        "candidates": [],
        "suggested_command": None,
    }


def collect_changed_files(target: Path, files: Sequence[str] | None = None) -> list[str]:
    """Return repo-relative changed paths: explicit ``files`` or ``git diff --name-only HEAD``."""
    if files is not None:
        return _unique_strings(files)
    target = target.expanduser().resolve()
    result = helpers._git(target, "diff", "--name-only", "HEAD")
    if result.returncode != 0:
        return []
    return _unique_strings(result.stdout.splitlines())


def confidence_for_hops(min_hops: int) -> dict[str, Any]:
    """Map GraphTrail min_hops to a display confidence band and numeric score.

    Lower hop distance means a shorter static call path from the test to a
    changed symbol, so confidence is higher. This is structural evidence, not
    coverage proof.
    """
    hops = max(0, int(min_hops))
    score = round(max(0.1, 1.0 - (0.2 * hops)), 3)
    if hops <= 1:
        band = "high"
    elif hops <= 3:
        band = "medium"
    else:
        band = "low"
    return {"score": score, "band": band, "min_hops": hops}


def rank_verification_candidates(
    target: Path,
    *,
    files: Sequence[str] | None = None,
    depth: int = DEFAULT_AFFECTED_DEPTH,
    affected_runner: Any | None = None,
) -> dict[str, Any]:
    """Rank verification command candidates from GraphTrail ``affected``.

    Degrades to an empty ranking when GraphTrail is unavailable or when there
    are no changed files to attribute. Never raises.
    """
    target = target.expanduser().resolve()
    changed = collect_changed_files(target, files)
    binary = component_bins.resolve("graphtrail")
    db_path = target / ".graphtrail" / "graphtrail.db"
    if binary is None:
        return degraded_ranking(reason="graphtrail binary not found", changed_files=changed)
    if not db_path.is_file():
        return degraded_ranking(reason="graphtrail index not found", changed_files=changed)
    if not changed:
        # Oracle is available but nothing changed — not degraded.
        return {
            "degraded": False,
            "changed_files": [],
            "missing_files": [],
            "depth": depth,
            "attribution": _ATTRIBUTION,
            "candidates": [],
            "suggested_command": None,
            "note": "no changed files to attribute",
        }

    runner = affected_runner or _run_graphtrail_affected
    report = runner(target, binary, db_path, changed, depth)
    if not isinstance(report, dict):
        return degraded_ranking(reason="graphtrail affected returned no usable report", changed_files=changed)

    return _ranking_from_affected_report(target, report, depth=depth)


def _ranking_from_affected_report(target: Path, report: dict[str, Any], *, depth: int) -> dict[str, Any]:
    changed_files = _unique_strings(report.get("changed_files"))
    missing_files = _unique_strings(report.get("missing_files"))
    attribution = report.get("attribution")
    if not isinstance(attribution, str) or not attribution.strip():
        attribution = _ATTRIBUTION
    report_depth = report.get("depth")
    if isinstance(report_depth, int) and report_depth > 0:
        depth = report_depth

    affected_tests = report.get("affected_tests")
    if not isinstance(affected_tests, list):
        affected_tests = []

    base_prefix = _pytest_command_prefix(target)
    candidates: list[dict[str, Any]] = []
    for row in affected_tests:
        if not isinstance(row, dict):
            continue
        file_path = row.get("file_path") or row.get("path")
        if not isinstance(file_path, str) or not file_path.strip():
            continue
        path = file_path.strip().replace("\\", "/")
        min_hops_raw = row.get("min_hops")
        try:
            min_hops = int(min_hops_raw) if min_hops_raw is not None else 0
        except (TypeError, ValueError):
            min_hops = 0
        via = _unique_strings(row.get("via"))
        confidence = confidence_for_hops(min_hops)
        command = f"{base_prefix} {path}".strip() if base_prefix else path
        candidates.append(
            {
                "command": command,
                "test_path": path,
                "confidence": confidence,
                "evidence": {
                    "file_path": path,
                    "min_hops": min_hops,
                    "via": via,
                    "source": "graphtrail.affected",
                },
            }
        )

    candidates.sort(
        key=lambda item: (
            -float(item["confidence"]["score"]),
            int(item["confidence"]["min_hops"]),
            str(item["test_path"]),
        )
    )
    if len(candidates) > CANDIDATE_FILE_CAP:
        candidates = candidates[:CANDIDATE_FILE_CAP]

    suggested_command = None
    if candidates:
        # Prefer a single combined command covering the highest-confidence tests.
        limited = [str(item["test_path"]) for item in candidates]
        joined = " ".join(limited)
        suggested_command = f"{base_prefix} {joined}".strip() if base_prefix else joined

    return {
        "degraded": False,
        "changed_files": changed_files,
        "missing_files": missing_files,
        "depth": depth,
        "attribution": attribution.strip(),
        "candidates": candidates,
        "suggested_command": suggested_command,
        "truncated": bool(report.get("truncated")),
    }


def _pytest_command_prefix(target: Path) -> str:
    """Match ``_default_verify_commands`` so ranked candidates share the same runner."""
    if (target / "pyproject.toml").is_file() and (target / "tests").is_dir():
        if (target / "src").is_dir():
            return "PYTHONPATH=src python3 -m pytest -q"
        return "python3 -m pytest -q"
    if (target / "pytest.ini").is_file() or (target / "tests").is_dir():
        return "python3 -m pytest -q"
    if (target / "package.json").is_file():
        # GraphTrail attributes source/test files; npm projects still get path hints
        # but the default runner stays ``npm test`` at the plan level.
        return "python3 -m pytest -q"
    return "python3 -m pytest -q"


def _run_graphtrail_affected(
    target: Path,
    binary: str,
    db_path: Path,
    files: Sequence[str],
    depth: int,
) -> dict[str, Any] | None:
    argv = [binary, "--db", str(db_path), "affected", *files, "--depth", str(depth), "--json"]
    result = proc.run(argv, timeout=15.0, cwd=target)
    if result.code != 0:
        return None
    data = result.json()
    if isinstance(data, dict):
        return data
    return None


def _unique_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Sequence[object] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip().replace("\\", "/")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
