"""Read workspace verify manifests from their trusted Git base."""

from __future__ import annotations
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BaseResolution:
    status: Literal["resolved", "unresolved_default_branch", "no_merge_base"]
    source: Literal["default_branch", "no_remote"] | None = None
    reference: str | None = None
    commit: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class BaseManifestPayload:
    payload: dict[str, Any]
    commit: str
    payload_sha256: str
    worktree_modified: bool
    worktree_missing: bool
    base_source: str
    base_reference: str
    blob_oid: str


class ManifestGitError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def trusted_git_env() -> dict[str, str]:
    """Drop inherited Git overrides for local repository inspection."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=target,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env=trusted_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        # No git, or a hung git, means no trusted base. Callers treat a non-zero code as that.
        return subprocess.CompletedProcess(command, 127, b"", b"")


def resolve_base(target: Path) -> BaseResolution:
    remotes = _git(target, "remote")
    if remotes.returncode == 0 and not remotes.stdout.strip():
        head = _git(target, "rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode != 0:
            return BaseResolution("no_merge_base", message="verify manifest base has no commit at HEAD")
        return BaseResolution("resolved", "no_remote", "HEAD", head.stdout.decode("ascii").strip())
    candidates: list[str] = []
    symbolic = _git(target, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if symbolic.returncode == 0:
        full = symbolic.stdout.decode("ascii", "ignore").strip()
        if full.startswith("refs/remotes/origin/"):
            candidates.append(full)
    candidates.extend(["refs/remotes/origin/main", "refs/remotes/origin/master"])
    reference = next(
        (item for item in candidates if _git(target, "rev-parse", "--verify", f"{item}^{{commit}}").returncode == 0),
        None,
    )
    if reference is None:
        return BaseResolution(
            "unresolved_default_branch",
            message="verify manifest default branch is unresolved; run git fetch origin <branch> or git remote set-head origin -a",
        )
    merged = _git(target, "merge-base", "HEAD", reference)
    if merged.returncode != 0 or not merged.stdout.strip():
        return BaseResolution(
            "no_merge_base",
            reference=reference,
            message=f"verify manifest default branch has no merge base with HEAD: {reference}",
        )
    return BaseResolution("resolved", "default_branch", reference, merged.stdout.decode("ascii").strip())


def _require_base(base: BaseResolution) -> tuple[str, str, str]:
    if base.status != "resolved" or not base.commit or not base.source or not base.reference:
        raise ManifestGitError(base.status, base.message or "verify manifest base is unresolved")
    return base.commit, base.source, base.reference


def list_base_manifests(target: Path, base: BaseResolution) -> list[Path]:
    commit, _, _ = _require_base(base)
    result = _git(target, "ls-tree", "-r", "-z", commit, "--", "verify/manifests")
    if result.returncode != 0:
        raise ManifestGitError("manifest_base_unreadable", "verify manifest base tree is unreadable")
    paths: list[Path] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        _, raw_path = record.split(b"\t", 1)
        try:
            name = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError:
            # A file name that is not UTF-8 cannot be a manifest. Skip it and keep the rest.
            continue
        if Path(name).parent.as_posix() == "verify/manifests" and name.endswith(".json"):
            paths.append(target / name)
    return sorted(paths)


def load_base_manifest(target: Path, path: Path, base: BaseResolution | None = None) -> BaseManifestPayload:
    base = base or resolve_base(target)
    commit, source, reference = _require_base(base)
    try:
        relative = path.relative_to(target).as_posix()
    except ValueError as exc:
        raise ManifestGitError("manifest_path_invalid", "verify manifest path is outside the repository") from exc
    entry = _git(target, "ls-tree", commit, "--", relative)
    if entry.returncode != 0 or not entry.stdout.strip():
        raise ManifestGitError(
            "manifest_not_on_default_branch",
            f"verify manifest is not on the default branch yet: {relative}; a new manifest can be used once it is on the default branch",
        )
    mode, kind, oid = entry.stdout.split(b"\t", 1)[0].decode("ascii").split()
    if mode not in {"100644", "100755"} or kind != "blob":
        raise ManifestGitError("manifest_not_regular_file", f"verify manifest is not a regular file: {relative}")
    sized = _git(target, "cat-file", "-s", oid)
    size = int(sized.stdout) if sized.returncode == 0 else MAX_MANIFEST_BYTES + 1
    if size > MAX_MANIFEST_BYTES:
        raise ManifestGitError(
            "manifest_too_large", f"verify manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit: {relative}"
        )
    blob = _git(target, "cat-file", "blob", oid)
    if blob.returncode != 0:
        raise ManifestGitError("manifest_blob_unreadable", f"verify manifest blob is unreadable: {relative}")
    raw = blob.stdout
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestGitError(
            "manifest_invalid_utf8_json", f"verify manifest is not valid UTF-8 JSON: {relative}"
        ) from exc
    if not isinstance(payload, dict):
        raise ManifestGitError("manifest_invalid_json", f"verify manifest JSON must be an object: {relative}")
    missing = not path.exists() and not path.is_symlink()
    modified = (
        True
        if missing or path.is_symlink()
        else _git(target, "diff", "--quiet", commit, "--", relative).returncode != 0
    )
    return BaseManifestPayload(
        payload, commit, f"sha256:{hashlib.sha256(raw).hexdigest()}", modified, missing, source, reference, oid
    )
