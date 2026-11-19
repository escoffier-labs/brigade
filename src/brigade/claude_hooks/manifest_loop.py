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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .. import localio, verify_manifest

_GIT_TIMEOUT_SECONDS = 10.0
_PORCELAIN_PREFIX_WIDTH = 3  # "XY " before each path


@dataclass(frozen=True)
class CoveredManifest:
    manifest_id: str
    subject_path: str


def _normalize(path: str) -> str:
    """Repo-relative, forward-slash form so Windows and POSIX paths compare equal."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def changed_paths(target: Path) -> list[str] | None:
    """Repo-relative paths that differ from HEAD, plus untracked files, in one git call.

    Returns ``None`` on any git failure so callers can fail open.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    paths: list[str] = []
    for entry in result.stdout.split("\0"):
        if len(entry) > _PORCELAIN_PREFIX_WIDTH:
            paths.append(_normalize(entry[_PORCELAIN_PREFIX_WIDTH:]))
    return paths


def covered_manifests(target: Path) -> list[CoveredManifest]:
    """Tracked manifests whose exact ``subject_path`` changed in ``target``'s working tree.

    Scope globs do not count: only a change to the subject the manifest scores
    asks for its verify. Returns ``[]`` on any error.
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
            if subject and subject in changed:
                covered.append(CoveredManifest(manifest.manifest_id, subject))
        return covered
    except Exception:  # noqa: BLE001 - advisory probe; fail open to the unchanged hook behavior
        return []


def verify_command(target: Path, manifest_id: str) -> str:
    """The manifest verify, without the session-env prefix the runtime adds."""
    return (
        f"brigade work verify run --target {shlex.quote(str(target))} "
        f"--manifest {shlex.quote(manifest_id)} --capture auto"
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


def unverified_manifests(target: Path, since: object) -> list[CoveredManifest]:
    """Covered manifests whose subject was written since ``since`` with no later completed receipt.

    The receipt must carry the manifest's id (``verify_manifest_id``), be
    ``completed`` like the generic Stop gate requires, and have started at or
    after the subject's last write. ``since`` is the session start, so a subject
    that was already dirty before the session is not this session's to verify.
    """
    started = localio.parse_iso_datetime(since)
    if started is None:
        return []
    try:
        target = target.expanduser().resolve()
        gaps: list[CoveredManifest] = []
        for covered in covered_manifests(target):
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
