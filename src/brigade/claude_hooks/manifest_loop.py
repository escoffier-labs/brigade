"""Ask for the tracked manifest verify when a change touches a manifest subject (#2589).

An ad-hoc ``--command`` verify never produces a scoreable trial, because only a
tracked manifest builds the subject binding. When the working tree changed the
exact ``subject_path`` of a tracked manifest, the PreToolUse deny names that
manifest's verify next to the agent's own check, and the Stop gate holds closeout
until a completed receipt from that manifest postdates the write. Every probe
here is advisory: any git, filesystem or manifest error reads as "no match" so
the hooks fall back to their pre-existing behavior.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import localio, verify_manifest

# One budget for every git call in a probe. The hook body runs under a 12 s envelope and two hook
# timeouts latch the work-loop hook off for the session, so this stays well below it.
_GIT_TIMEOUT_SECONDS = 2.0
_PORCELAIN_PREFIX_WIDTH = 3  # "XY " before each path


@dataclass(frozen=True)
class CoveredManifest:
    manifest_id: str
    subject_path: str
    # Target-relative path of the manifest file. ``--manifest <id>`` errors when the id is ambiguous,
    # a path always resolves.
    source: str


def _normalize(path: str) -> str:
    """Repo-relative, forward-slash form so Windows and POSIX paths compare equal."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _git_output(target: Path, args: list[str], deadline: float) -> str | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(target), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            # Paths are bytes on POSIX. Keep undecodable names instead of failing the whole probe.
            encoding="utf-8",
            errors="surrogateescape",
            timeout=remaining,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def changed_paths(target: Path) -> list[str] | None:
    """Target-relative paths that differ from HEAD, plus untracked files.

    ``git status`` reports paths relative to the repository root even with ``-C <subdir>``, so
    paths outside ``target`` are dropped and the ``target`` prefix is stripped. Returns ``None``
    on any git failure so callers can fail open.
    """
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    prefix = _git_output(target, ["rev-parse", "--show-prefix"], deadline)
    status = _git_output(
        target, ["status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", "--", "."], deadline
    )
    if prefix is None or status is None:
        return None
    prefix = _normalize(prefix.strip())
    if prefix:
        prefix = prefix.rstrip("/") + "/"
    paths: list[str] = []
    for entry in status.split("\0"):
        if len(entry) <= _PORCELAIN_PREFIX_WIDTH:
            continue
        path = _normalize(entry[_PORCELAIN_PREFIX_WIDTH:])
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix) :]
        paths.append(path)
    return paths


def covered_manifests(target: Path) -> list[CoveredManifest]:
    """Tracked manifests whose exact ``subject_path`` changed in ``target``'s working tree.

    Scope globs do not count: only a change to the subject the manifest scores
    asks for its verify. A deleted subject cannot be scored, so it does not count
    either. Returns ``[]`` on any error.
    """
    try:
        target = target.expanduser().resolve()
        paths = changed_paths(target)
        if not paths:
            return []
        changed = set(paths)
        covered: list[CoveredManifest] = []
        for manifest in verify_manifest.manifests_matching_paths(target, paths):
            subject = _normalize(manifest.subject_path or "")
            if not subject or subject not in changed or not (target / subject).is_file():
                continue
            source = verify_manifest.manifest_source_path(target, manifest) or manifest.manifest_id
            covered.append(CoveredManifest(manifest.manifest_id, subject, source))
        return covered
    except Exception:  # noqa: BLE001 - advisory probe; fail open to the unchanged hook behavior
        return []


def verify_command(target: Path, source: str) -> str:
    """The manifest verify, without the session-env prefix the runtime adds."""
    return (
        f"brigade work verify run --target {shlex.quote(str(target))} --manifest {shlex.quote(source)} --capture auto"
    )


def _manifest_receipts(target: Path, manifest_id: str) -> Iterator[dict[str, Any]]:
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return
    for path in root.glob("*/receipt.json"):
        receipt = localio.read_json_dict(path)
        if isinstance(receipt, dict) and receipt.get("verify_manifest_id") == manifest_id:
            yield receipt


def _receipt_target_is(receipt: dict[str, Any], target: Path) -> bool:
    raw = receipt.get("target")
    if not isinstance(raw, str):
        return False
    try:
        return Path(raw).expanduser().resolve() == target
    except OSError:
        return False


def unverified_manifests(target: Path, since: object, written_paths: Iterable[str]) -> list[CoveredManifest]:
    """Covered manifests whose subject this session wrote since ``since`` with no later completed receipt.

    ``written_paths`` are the target-relative files this session wrote itself. A subject
    that is only dirty, because another session, a formatter or a checkout touched it,
    is not this session's to verify.

    The receipt must carry the manifest's id (``verify_manifest_id``), be
    ``completed`` like the generic Stop gate requires, and have started at or
    after the subject's last write. ``since`` is the session start.
    """
    started = localio.parse_iso_datetime(since)
    written_by_session = {_normalize(path) for path in written_paths}
    if started is None or not written_by_session:
        return []
    try:
        target = target.expanduser().resolve()
        gaps: list[CoveredManifest] = []
        for covered in covered_manifests(target):
            if covered.subject_path not in written_by_session:
                continue
            try:
                written = (target / covered.subject_path).stat().st_mtime
            except OSError:
                continue
            if written < started.timestamp():
                continue
            if not _has_completed_receipt_since(target, covered.manifest_id, written):
                gaps.append(covered)
        return gaps
    except Exception:  # noqa: BLE001 - advisory probe; fail open to the unchanged Stop behavior
        return []


def _has_completed_receipt_since(target: Path, manifest_id: str, written: float) -> bool:
    for receipt in _manifest_receipts(target, manifest_id):
        if receipt.get("status") != "completed" or not _receipt_target_is(receipt, target):
            continue
        receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
        if receipt_started is not None and receipt_started.timestamp() >= written:
            return True
    return False
