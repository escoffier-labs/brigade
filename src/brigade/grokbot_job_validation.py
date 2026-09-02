"""Validation primitives for private Grok Bot queue jobs."""

from __future__ import annotations

import re
from typing import Any


JOB_ID_RE = re.compile(r"^grokbot-[0-9a-f]{24}$")
REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TASK_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GITHUB_PULL_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/pull/[1-9][0-9]*$"
)
LOWER_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_KINDS = frozenset({"draft-pr", "branch", "report"})
# The one queue-side bound on a lease. Callers that offer a configurable or
# caller-requested lease read these instead of restating the numbers.
LEASE_SECONDS_MIN = 30
LEASE_SECONDS_MAX = 3600


class GrokbotJobError(ValueError):
    """A rejected Grok Bot job request with a stable machine-readable reason."""

    def __init__(self, reason: str, *, action: str | None = None):
        self.reason = reason
        self.action = action
        super().__init__(reason)


def bounded_string(value: object, *, reason: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise GrokbotJobError(reason)
    return value


def validate_repository(value: object) -> None:
    repository = bounded_string(value, reason="invalid-repository", maximum=200)
    parts = repository.split("/")
    if len(parts) != 2 or not all(REPOSITORY_PART_RE.fullmatch(part) for part in parts):
        raise GrokbotJobError("invalid-repository")


def validate_base_ref(value: object) -> None:
    ref = bounded_string(value, reason="invalid-base-ref", maximum=128)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or ref.endswith((".", "/"))
        or any(part.endswith(".lock") for part in ref.split("/"))
    ):
        raise GrokbotJobError("invalid-base-ref")


def validate_ownership_paths(value: object) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise GrokbotJobError("invalid-ownership-paths")
    paths: set[str] = set()
    for item in value:
        path = bounded_string(item, reason="invalid-ownership-paths", maximum=512)
        if (
            path in paths
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise GrokbotJobError("invalid-ownership-paths")
        paths.add(path)


def validate_commands(value: object) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise GrokbotJobError("invalid-verification-commands")
    for command in value:
        bounded_string(command, reason="invalid-verification-commands", maximum=1000)


def validate_artifact(value: object, *, role: object) -> None:
    if not isinstance(value, dict):
        raise GrokbotJobError("invalid-artifact")
    if set(value) != {"kind"}:
        raise GrokbotJobError("invalid-artifact")
    kind = value.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise GrokbotJobError("invalid-artifact")
    if role == "implementation-worker" and kind not in {"draft-pr", "branch"}:
        raise GrokbotJobError("invalid-artifact")
    if role == "repository-scout" and kind != "report":
        raise GrokbotJobError("invalid-artifact")


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise GrokbotJobError("invalid-idempotency-key")
    return value


def validate_opaque_id(value: object, reason: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise GrokbotJobError(reason)
    return value


def validate_lease_seconds(value: object) -> int:
    if type(value) is not int or not LEASE_SECONDS_MIN <= value <= LEASE_SECONDS_MAX:
        raise GrokbotJobError("invalid-lease-seconds")
    return value


def validate_job_id(value: object) -> str:
    if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
        raise GrokbotJobError("invalid-job-id")
    return value


def validate_completion_artifact(artifact: object, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise GrokbotJobError("invalid-artifact")
    expected = spec.get("artifact")
    if not isinstance(expected, dict):  # Covered by record validation; retained for type safety.
        raise GrokbotJobError("corrupt-storage")
    kind = expected["kind"]
    if artifact.get("kind") != kind:
        raise GrokbotJobError("artifact-mismatch")
    if kind == "draft-pr":
        if set(artifact) != {"kind", "url", "branch"}:
            raise GrokbotJobError("invalid-artifact")
        url = artifact.get("url")
        if not isinstance(url, str) or not GITHUB_PULL_URL_RE.fullmatch(url):
            raise GrokbotJobError("invalid-artifact")
        validate_repo_relative_branch(artifact.get("branch"))
    elif kind == "branch":
        if set(artifact) != {"kind", "branch", "commit"}:
            raise GrokbotJobError("invalid-artifact")
        validate_repo_relative_branch(artifact.get("branch"))
        commit = artifact.get("commit")
        if not isinstance(commit, str) or not LOWER_HEX_40_RE.fullmatch(commit):
            raise GrokbotJobError("invalid-artifact")
    elif kind == "report":
        if set(artifact) != {"kind", "path", "sha256"}:
            raise GrokbotJobError("invalid-artifact")
        validate_repo_relative_path(artifact.get("path"))
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or not LOWER_HEX_64_RE.fullmatch(digest):
            raise GrokbotJobError("invalid-artifact")
    else:  # validate_spec guarantees this cannot happen for a valid record.
        raise GrokbotJobError("corrupt-storage")
    return artifact


def validate_repo_relative_branch(value: object) -> str:
    branch = bounded_string(value, reason="invalid-artifact", maximum=255)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
        or "//" in branch
        or branch.startswith("-")
        or ".." in branch
        or "@{" in branch
        or branch.endswith((".", "/", ".lock"))
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise GrokbotJobError("invalid-artifact")
    return branch


def validate_repo_relative_path(value: object) -> str:
    path = bounded_string(value, reason="invalid-artifact", maximum=512)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(character) < 32 for character in path)
    ):
        raise GrokbotJobError("invalid-artifact")
    return path
