"""Baseline mode for content-guard.

Baseline mode solves the "first install on a dirty repo" UX problem. The user
runs ``content-guard baseline init <dir>`` to capture all current findings as
accepted. Subsequent ``content-guard scan --baseline <path>`` invocations
filter those known findings out, so the hook only fires on NEW violations.

The baseline file is JSON (not YAML) because the core package is intentionally
dependency-free; see AGENTS.md. The schema is versioned so future format
changes can be detected and rejected explicitly.

The fingerprint is a sha256 hash of ``rule_id + "::" + match`` truncated to
16 hex chars. That gives a stable identifier across runs while ignoring
position (line/column) so refactors that move text around still match. Path
is matched separately so the same offending string in a different file is
treated as a new finding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .engine import scan_text
from .policy import Policy
from .types import Finding


BASELINE_SCHEMA_VERSION = 1
DEFAULT_BASELINE_FILENAME = ".content-guard-baseline.json"

# Mirrors cli.DEFAULT_EXCLUDE_DIR_NAMES. Duplicated here to avoid a circular
# import (cli imports from this module).
_DEFAULT_EXCLUDE_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        ".cache",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "out",
        ".turbo",
        ".parcel-cache",
        "vendor",
        ".claude",
    }
)


@dataclass(frozen=True)
class BaselineEntry:
    """One accepted finding in the baseline file."""

    path: str
    rule_id: str
    match: str
    line: int
    fingerprint: str


@dataclass
class Baseline:
    """Collection of accepted findings plus metadata."""

    entries: list[BaselineEntry] = field(default_factory=list)
    created_at: str = ""
    version: int = BASELINE_SCHEMA_VERSION

    def _index(self) -> dict[tuple[str, str, str], BaselineEntry]:
        """Index entries by (path, rule_id, fingerprint) for fast lookup."""
        return {(e.path, e.rule_id, e.fingerprint): e for e in self.entries}

    def contains(self, path: str, rule_id: str, fingerprint: str) -> bool:
        return (path, rule_id, fingerprint) in self._index()


def fingerprint_for(rule_id: str, match: str) -> str:
    """Stable 16-char hex fingerprint for (rule_id, match) pair.

    Same input always produces same output. Path is intentionally NOT part of
    the fingerprint so the caller can decide whether to match cross-file or
    same-file (we choose same-file at the filter step).
    """
    digest = hashlib.sha256(f"{rule_id}::{match}".encode("utf-8")).hexdigest()
    return digest[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _entry_from_finding(finding: Finding, path: str) -> BaselineEntry:
    return BaselineEntry(
        path=path,
        rule_id=finding.rule_id,
        match=finding.match,
        line=finding.line,
        fingerprint=fingerprint_for(finding.rule_id, finding.match),
    )


def init_baseline(
    target_dir: Path,
    policy: Policy | None = None,
    scope: str = "tree",
) -> Baseline:
    """Scan ``target_dir`` and capture all current findings as a baseline.

    ``scope`` is accepted for future compatibility with the planned
    ``--scope tracked|tree|staged|diff`` flag. Only ``tree`` is implemented
    here; the scope arg is stored implicitly via the entries we capture.
    """
    if scope != "tree":
        raise ValueError(f"baseline scope {scope!r} not yet supported (only 'tree')")

    active_policy = policy or Policy()
    entries: list[BaselineEntry] = []

    target_dir = Path(target_dir)
    if target_dir.is_file():
        files = [target_dir]
    else:
        files = sorted(target_dir.rglob("*.md"))

    for file_path in files:
        if _DEFAULT_EXCLUDE_DIR_NAMES.intersection(file_path.parts):
            continue
        try:
            text = file_path.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        result = scan_text(text, policy=active_policy)
        rel_path = _relative_path(file_path, target_dir)
        for finding in result.findings:
            # Skip findings that are already allowed by inline directives — those
            # don't need a baseline entry, and capturing them adds noise.
            if finding.action == "allow":
                continue
            entries.append(_entry_from_finding(finding, rel_path))

    return Baseline(entries=entries, created_at=_now_iso())


def _relative_path(file_path: Path, target_dir: Path) -> str:
    """Return a path relative to target_dir if possible, otherwise as-is."""
    try:
        if target_dir.is_file():
            return file_path.name
        return str(file_path.relative_to(target_dir))
    except ValueError:
        return str(file_path)


def save_baseline(baseline: Baseline, path: Path) -> None:
    """Write the baseline to ``path`` as JSON."""
    payload = {
        "version": baseline.version or BASELINE_SCHEMA_VERSION,
        "created_at": baseline.created_at or _now_iso(),
        "entries": [asdict(e) for e in baseline.entries],
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_baseline(path: Path) -> Baseline:
    """Read and validate a baseline JSON file at ``path``."""
    raw_text = Path(path).read_text()
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline file is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("baseline root must be a JSON object")

    version = raw.get("version")
    if not isinstance(version, int):
        raise ValueError("baseline 'version' must be an integer")
    if version != BASELINE_SCHEMA_VERSION:
        raise ValueError(f"unsupported baseline version {version!r}; expected {BASELINE_SCHEMA_VERSION}")

    created_at = raw.get("created_at", "")
    if not isinstance(created_at, str):
        raise ValueError("baseline 'created_at' must be a string")

    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("baseline 'entries' must be a list")

    entries: list[BaselineEntry] = []
    for i, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise ValueError(f"baseline entries[{i}] must be an object")
        try:
            entry = BaselineEntry(
                path=str(item["path"]),
                rule_id=str(item["rule_id"]),
                match=str(item["match"]),
                line=int(item["line"]),
                fingerprint=str(item["fingerprint"]),
            )
        except KeyError as exc:
            raise ValueError(f"baseline entries[{i}] missing required field {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"baseline entries[{i}] has invalid value: {exc}") from exc
        entries.append(entry)

    return Baseline(entries=entries, created_at=created_at, version=version)


def filter_findings(
    findings: list[Finding],
    baseline: Baseline,
    file_path: str,
) -> list[Finding]:
    """Return findings NOT present in the baseline for ``file_path``.

    Matches by (path, rule_id, fingerprint). A finding with the same rule_id
    but different match content (different fingerprint) is treated as NEW
    even if it lives in a baselined file.
    """
    index = baseline._index()
    kept: list[Finding] = []
    for finding in findings:
        key = (file_path, finding.rule_id, fingerprint_for(finding.rule_id, finding.match))
        if key in index:
            continue
        kept.append(finding)
    return kept
