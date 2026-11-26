"""Read workspace verify manifests from their trusted Git base."""

from __future__ import annotations
import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal

MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BaseResolution:
    status: Literal["resolved", "unresolved_default_branch", "no_merge_base"]
    source: Literal["default_branch", "no_remote"] | None = None
    reference: str | None = None
    commit: str | None = None
    message: str | None = None
    default_branch_tip: str | None = None
    timed_out: bool = False


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


def _git(
    target: Path, *args: str, deadline: float | None = None, git_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", *args]
    timeout = 30.0 if deadline is None else min(30.0, deadline - monotonic())
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)
    try:
        return subprocess.run(
            command,
            cwd=target,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=trusted_git_env() if git_env is None else git_env,
        )
    except subprocess.TimeoutExpired:
        if deadline is not None:
            raise
    except OSError:
        pass
    # Callers without a deadline treat an unavailable or hung git as a failed probe.
    return subprocess.CompletedProcess(command, 127, b"", b"")


def resolve_base(
    target: Path, *, commit: str = "HEAD", deadline: float | None = None, git_env: dict[str, str] | None = None
) -> BaseResolution:
    """Find the trusted base for HEAD or a supplied commit object ID."""
    return _BaseResolver(target, deadline=deadline, git_env=git_env).resolve(commit)


@dataclass
class _VerifyBase:
    target: Path
    resolution: BaseResolution | None = None


_VERIFY_BASE: ContextVar[_VerifyBase | None] = ContextVar("_VERIFY_BASE", default=None)


@contextmanager
def verify_base_scope(target: Path) -> Iterator[None]:
    """Share successful base resolution between the verdict and its advisory manifest list."""
    token = _VERIFY_BASE.set(_VerifyBase(target))
    try:
        yield
    finally:
        _VERIFY_BASE.reset(token)


def clear_verify_base(target: Path) -> None:
    snapshot = _VERIFY_BASE.get()
    if snapshot is not None and snapshot.target == target:
        snapshot.resolution = None


def resolve_verify_base(target: Path) -> BaseResolution:
    snapshot = _VERIFY_BASE.get()
    if snapshot is None or snapshot.target != target:
        return resolve_base(target)
    if snapshot.resolution is not None:
        return snapshot.resolution
    resolution = resolve_base(target)
    # An unavailable base may be transient. Let the next consumer retry.
    if resolution.status == "resolved":
        snapshot.resolution = resolution
    return resolution


class _BaseResolver:
    """Resolve each commit against one local default branch snapshot."""

    def __init__(self, target: Path, *, deadline: float | None, git_env: dict[str, str] | None) -> None:
        self._git = partial(_git, target, deadline=deadline, git_env=git_env)

    @cached_property
    def _default_branch(self) -> BaseResolution:
        git = self._git
        remotes = git("remote")
        if remotes.returncode != 0:
            return BaseResolution("unresolved_default_branch", message="Git could not list remotes for this target")
        if not remotes.stdout.strip():
            return BaseResolution("resolved", "no_remote")
        candidates: list[str] = []
        symbolic = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if symbolic.returncode == 0:
            full = symbolic.stdout.decode("ascii", "ignore").strip()
            if full.startswith("refs/remotes/origin/"):
                candidates.append(full)
        candidates.extend(["refs/remotes/origin/main", "refs/remotes/origin/master"])
        for reference in dict.fromkeys(candidates):
            tip = git("rev-parse", "--verify", f"{reference}^{{commit}}")
            if tip.returncode == 0 and tip.stdout.strip():
                tip_commit = tip.stdout.decode("ascii").strip()
                return BaseResolution("resolved", "default_branch", reference, default_branch_tip=tip_commit)
        return BaseResolution(
            "unresolved_default_branch",
            message="verify manifest default branch is unresolved; run git fetch origin <branch> or git remote set-head origin -a",
        )

    def resolve(self, commit: str) -> BaseResolution:
        git = self._git
        default = self._default_branch
        if default.source == "no_remote":
            head = git("rev-parse", "--verify", f"{commit}^{{commit}}")
            if head.returncode != 0:
                return BaseResolution("no_merge_base", message=f"verify manifest base has no commit at {commit}")
            return BaseResolution("resolved", "no_remote", commit, head.stdout.decode("ascii").strip())
        reference = default.reference
        tip_commit = default.default_branch_tip
        if reference is None or tip_commit is None:
            return default
        try:
            merged = git("merge-base", commit, tip_commit)
        except subprocess.TimeoutExpired:
            return BaseResolution(
                "no_merge_base",
                reference=reference,
                message=f"Git timed out while finding the merge base for {reference}.",
                timed_out=True,
                default_branch_tip=tip_commit,
            )
        if merged.returncode != 0 or not merged.stdout.strip():
            return BaseResolution(
                "no_merge_base",
                reference=reference,
                message=f"verify manifest default branch has no merge base with {commit}: {reference}",
                default_branch_tip=tip_commit,
            )
        return BaseResolution(
            "resolved",
            "default_branch",
            reference,
            merged.stdout.decode("ascii").strip(),
            default_branch_tip=tip_commit,
        )


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
