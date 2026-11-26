"""Read workspace verify manifests from their trusted Git base."""

from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from . import localio

MAX_MANIFEST_BYTES = 1024 * 1024
REQUIRED_CHECK_PATH = "verify/required-check.json"
_MANIFEST_DIR = "verify/manifests"
_OBJECT_ID = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")


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


@dataclass(frozen=True)
class WorktreeIdentity:
    head: str
    branch: str
    git_dir: str | None = None
    root: str | None = None


@dataclass(frozen=True)
class VerifyGitFacts:
    """Lazy Git observations owned by one side of a verify command."""

    target: Path
    capture_registry: bool = False
    read_identity: Callable[[Path], WorktreeIdentity | None] | None = None

    @cached_property
    def identity(self) -> WorktreeIdentity | None:
        return self.read_identity(self.target) if self.read_identity is not None else None

    @cached_property
    def tree(self) -> tuple[str | None, str | None]:
        return localio.tree_fingerprint_with_head(
            self.target, observed_head=self.identity.head if self.identity is not None else None
        )

    @cached_property
    def base(self) -> BaseResolution:
        return _BaseResolver(self.target, deadline=None, git_env=None, batch_refs=True).resolve("HEAD")

    @cached_property
    def base_files(self) -> _BaseFiles | None:
        if not self.capture_registry or not (self.target / ".git").exists():
            return None
        return _read_base_files(self.target, self.base, include_policy=True)


class _BaseResolver:
    """Resolve each commit against one local default branch snapshot."""

    def __init__(
        self, target: Path, *, deadline: float | None, git_env: dict[str, str] | None, batch_refs: bool = False
    ) -> None:
        self._git = partial(_git, target, deadline=deadline, git_env=git_env)
        self._batch_refs = batch_refs

    def _origin_head(self) -> BaseResolution | None:
        result = self._git(
            "for-each-ref", "--format=%(refname)%09%(symref)%09%(objecttype)%09%(objectname)", "refs/remotes/origin"
        )
        if result.returncode:
            return None
        refs = {fields[0]: fields[1:] for line in result.stdout.splitlines() if len(fields := line.split(b"\t")) == 4}
        symref = refs.get(b"refs/remotes/origin/HEAD")
        if symref is None:
            return None
        reference, kind, oid = symref
        target = refs.get(reference)
        if (
            not reference.startswith(b"refs/remotes/origin/")
            or target != [b"", kind, oid]
            or kind != b"commit"
            or not _OBJECT_ID.fullmatch(oid)
            or not reference.isascii()
        ):
            # Keep Git's legacy peeling, fallback and broken-ref behavior.
            return None
        return BaseResolution(
            "resolved", "default_branch", reference.decode("ascii"), default_branch_tip=oid.decode("ascii")
        )

    @cached_property
    def _default_branch(self) -> BaseResolution:
        git = self._git
        remotes = git("remote")
        if remotes.returncode != 0:
            return BaseResolution("unresolved_default_branch", message="Git could not list remotes for this target")
        if not remotes.stdout.strip():
            return BaseResolution("resolved", "no_remote")
        if self._batch_refs and (resolved := self._origin_head()) is not None:
            return resolved
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


@dataclass(frozen=True)
class _BaseFiles:
    manifest_ids: frozenset[str]
    policy: bytes | None


def _read_base_files(target: Path, base: BaseResolution, *, include_policy: bool = False) -> _BaseFiles | None:
    """Read advisory IDs and optionally the committed declaration in two Git launches."""
    if base.status != "resolved" or base.commit is None:
        return None
    paths = [_MANIFEST_DIR]
    if include_policy:
        paths.append(REQUIRED_CHECK_PATH)
    listing = _git(target, "ls-tree", "-r", "-t", "-l", "-z", base.commit, "--", *paths)
    if listing.returncode:
        return None
    requests: dict[bytes, int] = {}
    manifests: set[bytes] = set()
    policy_oid = None
    for row in listing.stdout.split(b"\0")[:-1]:
        metadata, _, path = row.partition(b"\t")
        is_policy = include_policy and path == REQUIRED_CHECK_PATH.encode("ascii")
        try:
            name = path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not is_policy and (Path(name).parent.as_posix() != _MANIFEST_DIR or not name.endswith(".json")):
            continue
        fields = metadata.split()
        if len(fields) != 4:
            return None
        mode, kind, oid, size = fields
        if mode not in {b"100644", b"100755"} or kind != b"blob" or not _OBJECT_ID.fullmatch(oid) or not size.isdigit():
            if is_policy:
                return None
            continue
        length = int(size)
        if length > MAX_MANIFEST_BYTES:
            if is_policy:
                return None
            continue
        requests[oid] = length
        if is_policy:
            policy_oid = oid
        else:
            manifests.add(oid)
    if not requests:
        return _BaseFiles(frozenset(), None)
    # Preserve the bounded per-manifest path for unusually large registries.
    if sum(requests.values()) > 64 * MAX_MANIFEST_BYTES:
        return None
    ids: set[str] = set()
    policy = None
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["git", "cat-file", "--batch"],
                cwd=target,
                input=b"".join(oid + b"\n" for oid in requests),
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=30,
                env=trusted_git_env(),
            )
            if result.returncode:
                return None
            output.seek(0)
            for oid, length in requests.items():
                if output.readline(128).split() != [oid, b"blob", str(length).encode("ascii")]:
                    return None
                raw = output.read(length)
                if len(raw) != length or output.read(1) != b"\n":
                    return None
                if oid == policy_oid:
                    policy = raw
                if oid in manifests:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    except RecursionError:
                        return None
                    manifest_id = payload.get("manifest_id") if isinstance(payload, dict) else None
                    if isinstance(manifest_id, str) and manifest_id:
                        ids.add(manifest_id)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _BaseFiles(frozenset(ids), policy)


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
