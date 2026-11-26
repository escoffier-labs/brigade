"""Codex Cloud adapter: submit a task, poll to a terminal state, return the result.

A roster seat references it as ``cli = "codex-cloud:configured"`` (resolved
from ``CODEX_CLOUD_ENV`` or ``brigade run cloud setup``) or
``cli = "codex-cloud:<env-id>"``. Environment ids come from the Codex Cloud
workspace UI; ``codex cloud list --json`` omits them. The worker's text is the
task's final status plus its unified diff. The diff is NEVER applied locally;
apply it deliberately with ``codex cloud apply <task-id>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import quote
from uuid import uuid4

from . import bug_io, cloud_tracker, dirfd, fleet_client, localio, proc
from .inbox_lock import InboxLockTimeout, held_file_lock
from .isolated_exec import trusted_cwd

SUBMIT_TIMEOUT = 120.0
POLL_TIMEOUT = 60.0
DIFF_TIMEOUT = 120.0
POLL_INTERVAL = 15.0
DIFF_CAP = 20_000
DEFAULT_MAX_ITEMS = 250
DEFAULT_LIST_TIMEOUT = 10.0
LEASE_SYNC_BUDGET_SECONDS = 105.0
MAX_LIST_PAGES = 20
MAX_LIST_LIMIT = 20
LAUNCH_RECONCILE_WINDOW = timedelta(minutes=5)
_LAUNCH_TAG_RE = re.compile(r"\[(brigade-launch-[0-9a-f]{32})\]")
CONFIGURED_SELECTOR = "configured"
DEFAULT_ENV_VAR = "CODEX_CLOUD_ENV"
CONFIG_SCHEMA = "brigade.codex-cloud-config/1"
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LITERAL_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_CONSECUTIVE_HUB_UNAVAILABLE_RENEWS = 3
# Live canary: first status after submit/bind can be unknown nonzero
# propagation; two retries at POLL_INTERVAL covers the observed ~30s recovery.
MAX_TRANSIENT_STATUS_RETRIES = 2
_AUTH_FAILURE_RE = re.compile(
    r"(?:\bnot (?:logged|signed) in\b|\bsign[- ]?in\b|\bauthentication\b|\bunauthorized\b|\b401\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ListTasksResult:
    """Bounded ``codex cloud list --json`` inventory with a safe failure reason."""

    tasks: list[dict[str, Any]]
    ok: bool = True
    reason: str | None = None
    complete: bool = True


class CodexCloudConfigError(ValueError):
    """Raised when a Codex Cloud seat cannot resolve its environment id."""


# Status keywords scanned (word-bounded, case-insensitive) in `codex cloud status`.
TERMINAL_OK = ("ready", "completed", "succeeded", "applied", "finished")
TERMINAL_FAIL = ("failed", "errored", "error", "cancelled", "canceled", "expired")
NONTERMINAL = ("pending", "queued", "running", "planning", "paused", "in_progress", "awaiting_plan_approval")

_ID_PATTERNS = (
    re.compile(r"https?://\S*/tasks?/(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b", re.I),
    re.compile(r"\btask\s+id\s*[:=]\s*(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b", re.I),
    re.compile(r"\b(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b"),
)

_GITHUB_REPO_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)"
)


def _hub_configured() -> bool:
    """Return True when a fleet hub is configured.

    No-hub developer environments keep the existing local-only dispatch path.
    """
    return bool(fleet_client.load_fleet_config().get("hub_url"))


def _require_private_lease_storage() -> None:
    # Lease storage does not go through work_cmd/nt_private.py yet, so on Windows it
    # would inherit the parent ACL. Refuse until it does.
    if sys.platform == "win32":
        raise OSError("secure-owner-store-unavailable")


def require_hub_lease_storage() -> bool:
    """Refuse unsupported Hub storage before admission, provider work, or recovery."""
    configured = _hub_configured()
    if configured:
        _require_private_lease_storage()
    return configured


def _repo_from_cwd(cwd: Path | None) -> str | None:
    """Return an ``owner/repo`` string from the cwd's GitHub origin, if known."""
    try:
        result = proc.run(["git", "remote", "get-url", "origin"], timeout=5.0, cwd=cwd)
    except Exception:  # noqa: BLE001 - repo identification is best-effort
        return None
    if result.code != 0:
        return None
    url = (result.stdout or "").strip()
    if not url:
        return None
    match = _GITHUB_REPO_RE.search(url)
    if match:
        return match.group(1)
    return None


def _ensure_cloud_directory(target: Path) -> None:
    """Create cloud ancestors privately and durably without changing existing modes."""
    with ExitStack() as stack:
        parent = dirfd.open_directory_chain(Path(target).expanduser().resolve())
        stack.callback(os.close, parent)
        for component in (".brigade", "cloud"):
            try:
                dirfd.mkdir_child(parent, component, 0o700)
            except FileExistsError:
                pass
            child = dirfd.open_child_directory(parent, component)
            stack.callback(os.close, child)
            # Retry any earlier directory publication whose fsync failed.
            dirfd.fsync_directory(parent)
            parent = child


def _preflight_registry(register_target: Path | None) -> bool:
    """Confirm registry-directory durability without reading or rewriting its live file."""
    if register_target is None:
        return True
    from . import cloud_tracker

    descriptor: int | None = None
    created_sidecar: Path | None = None
    cleanup_ok = True
    try:
        registry_dir = cloud_tracker.registry_path(Path(register_target)).parent
        _ensure_cloud_directory(Path(register_target))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        for _ in range(4):
            candidate = registry_dir / f".registry-preflight-{uuid4().hex}"
            try:
                descriptor = os.open(os.fspath(candidate), flags, 0o600)
            except FileExistsError:
                continue
            created_sidecar = candidate
            break
        if descriptor is None:
            return False
        written = os.write(descriptor, b"brigade-registry-preflight\n")
        if written != len(b"brigade-registry-preflight\n"):
            return False
        os.fsync(descriptor)
    except Exception:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_ok = False
        if created_sidecar is not None:
            try:
                created_sidecar.unlink()
            except OSError:
                cleanup_ok = False
    return cleanup_ok


def _lease_recovery_path(target: Path, lease_id: str) -> Path:
    digest = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()
    return cloud_tracker.registry_path(target).parent / "codex-leases" / f"{digest}.json"


class WorkspaceNotPrivate(OSError):
    """Recovery storage is writable by another user."""


def _validate_recovery_directory(info: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("unsafe lease recovery directory")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022)):
        raise WorkspaceNotPrivate("lease recovery directory must be owned and private")


@contextmanager
def _lease_recovery_directory(target: Path, *, create: bool) -> Iterator[int]:
    """Hold each no-follow parent; persist every child directory before use."""
    _require_private_lease_storage()
    workspace = Path(target).expanduser().resolve()
    with ExitStack() as stack:
        anchor = dirfd.open_directory_chain(workspace)
        stack.callback(os.close, anchor)
        _validate_recovery_directory(os.fstat(anchor), private=False)
        parent = anchor
        chain = []
        for component in (".brigade", "cloud", "codex-leases"):
            if create:
                try:
                    dirfd.mkdir_child(parent, component, 0o700)
                except FileExistsError:
                    pass
            child = dirfd.open_child_directory(parent, component)
            stack.callback(os.close, child)
            _validate_recovery_directory(os.fstat(child), private=component == "codex-leases")
            if create:
                # Also retries an earlier directory publication whose fsync failed.
                dirfd.fsync_directory(parent)
            chain.append((parent, component, child))
            parent = child
        yield parent
        for ancestor, component, child in chain:
            live = dirfd.stat_child(ancestor, component)
            opened = os.fstat(child)
            _validate_recovery_directory(live, private=component == "codex-leases")
            _validate_recovery_directory(opened, private=component == "codex-leases")
            if (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError("lease recovery directory changed")
        current = dirfd.open_directory_chain(workspace)
        stack.callback(os.close, current)
        before, after = os.fstat(anchor), os.fstat(current)
        _validate_recovery_directory(after, private=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError("lease recovery workspace changed")


def _read_lease_recovery_record(parent: int, name: str, descriptor: int) -> dict[str, Any]:
    before = os.fstat(descriptor)
    bug_io._regular(before, private=True)
    if before.st_size < 0 or before.st_size > 4096:
        raise ValueError("lease recovery record exceeds size bound")
    raw = bytearray()
    while len(raw) <= 4096:
        chunk = os.read(descriptor, 4097 - len(raw))
        if not chunk:
            break
        raw.extend(chunk)
    after = os.fstat(descriptor)
    bug_io._regular(after, private=True)
    live = dirfd.stat_child(parent, name)
    bug_io._regular(live, private=True)
    if (
        len(raw) > 4096
        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or (after.st_dev, after.st_ino) != (live.st_dev, live.st_ino)
    ):
        raise OSError("lease recovery record changed")
    record = json.loads(raw)
    if not isinstance(record, dict) or set(record) != {"schema", "lease_id", "holder"}:
        raise ValueError("invalid lease recovery record")
    return record


def _recovery_file_flags(flags: int) -> int:
    return flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def _persist_lease_holder(target: Path, lease_id: str, holder: str | None) -> None:
    """Publish one private, immutable capability before submitting provider work."""
    _require_private_lease_storage()
    if not isinstance(holder, str) or not holder:
        raise ValueError("cloud lease holder unavailable")
    record = {"schema": "brigade.codex-lease-recovery/1", "lease_id": lease_id, "holder": holder}
    raw = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > 4096:
        raise ValueError("lease recovery record exceeds size bound")
    name = _lease_recovery_path(target, lease_id).name
    with _lease_recovery_directory(target, create=True) as parent:
        created = True
        try:
            descriptor = dirfd.open_child_file(
                parent,
                name,
                _recovery_file_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                0o600,
                share_write=False,
            )
        except FileExistsError:
            created = False
            descriptor = dirfd.open_child_file(
                parent,
                name,
                _recovery_file_flags(os.O_RDWR),
                share_write=False,
            )
        try:
            bug_io._regular(os.fstat(descriptor), private=True)
            if created:
                offset = 0
                while offset < len(raw):
                    written = os.write(descriptor, raw[offset:])
                    if written <= 0:
                        raise OSError("lease recovery record write failed")
                    offset += written
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _read_lease_recovery_record(parent, name, descriptor) != record:
                raise OSError("lease recovery record conflicts with holder")
            # Partial exclusive creations are never accepted as recovery records.
            # No provider work starts unless the complete file and name are durable.
            os.fsync(descriptor)
            dirfd.fsync_directory(parent)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _read_lease_recovery_record(parent, name, descriptor) != record:
                raise OSError("lease recovery record changed during publication")
        finally:
            os.close(descriptor)


def _recover_lease_holder(target: Path, lease_id: str) -> str | None:
    try:
        _require_private_lease_storage()
        name = _lease_recovery_path(target, lease_id).name
        with _lease_recovery_directory(target, create=False) as parent:
            descriptor = dirfd.open_child_file(parent, name, _recovery_file_flags(os.O_RDONLY))
            try:
                record = _read_lease_recovery_record(parent, name, descriptor)
            finally:
                os.close(descriptor)
        if record.get("schema") != "brigade.codex-lease-recovery/1" or record.get("lease_id") != lease_id:
            return None
        holder = record.get("holder")
        return holder if isinstance(holder, str) and holder else None
    except (OSError, ValueError, UnicodeError):
        return None


def _attach_registered_lease_holder(target: Path, task_id: str, holder: str) -> None:
    with cloud_tracker.registry_lock(target):
        registry = cloud_tracker.load_registry(target)
        entry = next(
            (
                row
                for row in registry["entries"]
                if row.get("provider") == "codex-cloud" and row.get("task_id") == task_id
            ),
            None,
        )
        if isinstance(entry, dict) and not entry.get("lease_holder"):
            entry["lease_holder"] = holder
            cloud_tracker.save_registry(target, registry)


def _resolve_launch_branch(target: Path, repo: str, branch: str | None) -> tuple[str | None, str | None]:
    """Resolve a GitHub base branch and distinguish absent refs from failed lookups."""
    url = f"https://github.com/{repo}"
    if not _GITHUB_REPO_RE.fullmatch(url) or any(part in {".", ".."} for part in repo.split("/")):
        return None, "branch-unavailable"
    if branch is not None and (branch in {"", ".", ".."} or branch.startswith("-")):
        return None, "branch-unavailable"

    gh_argv = (
        ["gh", "repo", "view", f"github.com/{repo}", "--json", "defaultBranchRef"]
        if branch is None
        else ["gh", "api", "--hostname", "github.com", f"repos/{repo}/branches/{quote(branch, safe='')}"]
    )
    gh = proc.run(gh_argv, timeout=10.0, cwd=target, env={**os.environ, "GH_PROMPT_DISABLED": "1"})
    gh_reports_no_default = False
    if gh.code == 0:
        try:
            data = json.loads(gh.stdout)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            gh_reports_no_default = branch is None and "defaultBranchRef" in data and data["defaultBranchRef"] is None
            ref = data.get("defaultBranchRef") if branch is None else data
            name = ref.get("name") if isinstance(ref, dict) else None
            if isinstance(name, str) and name and not name.startswith("-") and (branch is None or name == branch):
                return name, None

    # GitHub can return 404 for an inaccessible private repository. Only
    # a successful ref query can confirm that an explicit branch is absent.
    prefix = "refs/heads/"
    argv = (
        ["git", "ls-remote", "--symref", "--", url, "HEAD"]
        if branch is None
        else ["git", "ls-remote", "--refs", "--", url, f"{prefix}{branch}"]
    )
    result = proc.run(argv, timeout=10.0, cwd=target, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.code != 0:
        return None, "branch-unavailable" if gh_reports_no_default else "branch-unverifiable"
    refs = [line.partition("\t") for line in result.stdout.splitlines()]
    if branch is not None:
        if any(ref == f"{prefix}{branch}" for _, _, ref in refs):
            return branch, None
    elif any(ref == "HEAD" and not value.startswith("ref: ") for value, _, ref in refs):
        for value, _, ref in refs:
            if ref == "HEAD" and value.startswith(f"ref: {prefix}"):
                default = value.removeprefix(f"ref: {prefix}")
                if default and not default.startswith("-"):
                    return default, None
    return None, "branch-unavailable"


def _tagged_prompt(prompt: str, tag: str) -> str:
    return f"[{tag}]\nKeep the bracketed launch tag verbatim in the task title.\n\n{prompt}"


def _clean_submit_failure(result: proc.Result) -> bool:
    # The CLI uses exit 1 both before and after the create request. The
    # process wrapper reserves 126 and 127 for exec failures.
    return result.code in (126, 127)


def _submitted_task_id(result: proc.Result) -> str | None:
    if result.code == 0:
        return parse_task_id(result.stdout) or parse_task_id(result.stderr)
    # A failed command can mention unrelated task IDs in its diagnostics.
    # Accept only the standalone submission response when it exits nonzero.
    responses = {
        match[1]
        for stream in (result.stdout, result.stderr)
        for line in stream.splitlines()
        if (
            match := re.fullmatch(
                r"(?:Submitted )?(?:https?://\S+/tasks?/)?(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})", line.strip()
            )
        )
    }
    return next(iter(responses)) if len(responses) == 1 else None


def _finish_launch_intent(target: Path, tag: str, *, task_id: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve one intent, with stable task identity across bind retries."""
    with cloud_tracker.registry_lock(target):
        registry = cloud_tracker.load_registry(target)
        entry = next(
            row for row in registry["entries"] if row.get("provider") == "codex-cloud" and row.get("launch_tag") == tag
        )
        candidate = entry.get("launch_candidate_id")
        if entry["launch_state"] == "tracked" and entry["task_id"] == task_id:
            return "ok", entry
        if entry["launch_state"] != "needs-reconcile" or (candidate is not None and candidate != task_id):
            return "tracking-failed", entry
        if task_id is None:
            entry["launch_state"] = "not-launched"
            entry.pop("lease_holder", None)
        else:
            existing = [
                row
                for row in registry["entries"]
                if row.get("provider") == "codex-cloud" and row.get("task_id") == task_id
            ]
            holder = entry.get("lease_holder")
            if len(existing) > 1 or (
                existing and (existing[0].get("launch_tag") or existing[0].get("lease_holder") not in (holder, None))
            ):
                return "tracking-failed", entry
            if candidate is None:
                entry["launch_candidate_id"] = task_id
                cloud_tracker.save_registry(target, registry)
            lease_id = entry.get("launch_lease_id")
            if lease_id is not None and (
                not isinstance(holder, str)
                or not fleet_client.bind_cloud(
                    lease_id, provider_task_id=task_id, holder=holder, ttl_seconds=fleet_client.CLOUD_BOUND_TTL_SECONDS
                ).granted
            ):
                return "bind-failed", entry
            if existing:
                registered = existing[0]
                registry["entries"].remove(entry)
                registered["launch_tag"] = tag
                if holder:
                    registered["lease_holder"] = holder
                entry = registered
            entry.update(task_id=task_id, launch_state="tracked")
            entry.pop("launch_candidate_id", None)
        entry.pop("launch_lease_id", None)
        cloud_tracker.save_registry(target, registry)
        return "ok", entry


def reconcile_launches(target: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Recover exact matches and retire absent intents after the visibility window."""
    pending = [
        row
        for row in cloud_tracker.load_registry(target)["entries"]
        if row.get("provider") == "codex-cloud" and row.get("launch_state") == "needs-reconcile"
    ]
    observed = now or datetime.now(timezone.utc)
    inventory = ListTasksResult([], ok=False)
    if any(not row.get("launch_candidate_id") for row in pending):
        try:
            inventory = list_tasks(cwd=target, include_launch_tags=True, command_timeout=3)
        except (OSError, RuntimeError):
            pass
    matches: dict[str, set[str]] = {}
    if inventory.ok and inventory.complete:
        for task in inventory.tasks:
            if tag := task.get("launch_tag"):
                matches.setdefault(tag, set()).add(task["id"])

    outcomes = []
    for row in pending:
        tag = row["launch_tag"]
        candidate = row.get("launch_candidate_id")
        task_ids = matches.get(tag, set())
        started = cloud_tracker._parse_time(row.get("dispatched_at"))  # noqa: SLF001 - shared timestamp parser
        expired = started is not None and observed - started >= LAUNCH_RECONCILE_WINDOW
        task_id = candidate or (next(iter(task_ids)) if len(task_ids) == 1 else None)
        reason = "awaiting-match"
        if not inventory.ok or not inventory.complete:
            reason = "inventory-incomplete"
        elif len(task_ids) > 1:
            reason = "ambiguous-match"
        if task_id or (inventory.ok and inventory.complete and not task_ids and expired):
            try:
                reason, row = _finish_launch_intent(target, tag, task_id=task_id)
            except (OSError, RuntimeError, ValueError, StopIteration):
                reason = "tracking-failed"
        outcomes.append(
            {"id": row["id"], "tag": tag, "state": row["launch_state"], "task_id": task_id, "reason": reason}
        )
    return outcomes


def launch_task(prompt: str, *, target: Path, repo: str, label: str, branch: str | None = None) -> dict[str, Any]:
    """Submit one task from stdin and bind its Codex provider lease."""
    from . import cloud_tracker

    try:
        hub_configured = require_hub_lease_storage()
    except OSError:
        return {"ok": False, "reason": "secure-owner-store-unavailable"}
    if not _preflight_registry(target):
        return {"ok": False, "reason": "registry-unwritable"}
    env_id = resolve_environment_id(CONFIGURED_SELECTOR, target=target)
    prompt_hash = cloud_tracker.prompt_hash(prompt)
    canonical_repo = repo.removeprefix("https://github.com/").strip("/").removesuffix(".git")
    base_branch, reason = _resolve_launch_branch(target, canonical_repo, branch)
    if base_branch is None:
        return {"ok": False, "reason": reason}
    lease_id: str | None = None
    holder: str | None = None
    if hub_configured:
        admission = fleet_client.admit_cloud(
            "codex",
            repo=canonical_repo,
            label=cloud_tracker.lease_label("codex", canonical_repo, prompt_hash),
            prompt_hash=prompt_hash,
            ttl_seconds=300,
        )
        lease_id = admission.lease.get("lease_id") if isinstance(admission.lease, dict) else None
        if not admission.granted or not isinstance(lease_id, str):
            return {"ok": False, "reason": admission.reason if not admission.granted else "no-lease"}
        holder = admission.holder
        try:
            _persist_lease_holder(target, lease_id, holder)
        except WorkspaceNotPrivate:
            fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
            return {"ok": False, "reason": "workspace-not-private"}
        except Exception:  # noqa: BLE001 - no provider work exists yet
            fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
            return {"ok": False, "reason": "lease-recovery-unwritable"}

    tag = f"brigade-launch-{uuid4().hex}"
    try:
        cloud_tracker.register(
            target,
            provider="codex-cloud",
            label=label,
            prompt_hash=prompt_hash,
            lease_holder=holder,
            environment_audit=environment_audit_ref(CONFIGURED_SELECTOR, env_id),
            launch_tag=tag,
            launch_lease_id=lease_id,
        )
    except Exception:  # noqa: BLE001 - no provider work exists yet
        if lease_id is not None:
            fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
        return {"ok": False, "reason": "registry-unwritable"}
    try:
        result = proc.run(
            ["codex", "cloud", "exec", "--env", env_id, "--branch", base_branch, "-"],
            timeout=SUBMIT_TIMEOUT,
            cwd=target,
            stdin=_tagged_prompt(prompt, tag).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 - provider acceptance could be unknown
        return {"ok": False, "reason": "uncertain"}
    task_id = _submitted_task_id(result)
    if task_id is None:
        if _clean_submit_failure(result):
            try:
                reason, _ = _finish_launch_intent(target, tag, task_id=None)
            except (OSError, RuntimeError, ValueError, StopIteration):
                return {"ok": False, "reason": "tracking-failed"}
            if reason != "ok":
                return {"ok": False, "reason": reason}
            if lease_id is not None:
                fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
            return {"ok": False, "reason": "submit-failed"}
        return {"ok": False, "reason": "uncertain"}
    try:
        reason, entry = _finish_launch_intent(target, tag, task_id=task_id)
    except Exception:  # noqa: BLE001 - keep known provider work recoverable after persistence failure
        return {"ok": False, "reason": "tracking-failed", "task_id": task_id}
    if reason != "ok":
        return {"ok": False, "reason": reason, "task_id": task_id}
    # A recognized ID proves work exists even when the CLI exits nonzero.
    # Track and bind it, but do not claim the submission completed successfully.
    return {
        "ok": result.code == 0,
        "reason": "ok" if result.code == 0 else "uncertain",
        "task_id": task_id,
        "entry": entry,
    }


def acquire_registered_task_lease(
    target: Path, *, task_id: str, label: str, prompt_hash: str | None, repo: str | None
) -> None:
    """Acquire and bind one lease for a Codex task registered after submission."""
    from . import cloud_tracker

    if not require_hub_lease_storage():
        return
    entries = cloud_tracker.load_registry(target)["entries"]
    current = next(
        (row for row in entries if row.get("provider") == "codex-cloud" and row.get("task_id") == task_id), None
    )
    if isinstance(current, dict) and isinstance(current.get("lease_holder"), str):
        return
    snapshot = fleet_client.fetch_cloud()
    leases = snapshot.get("leases")
    existing = (
        next(
            (row for row in leases if isinstance(row, dict) and row.get("provider_task_id") == task_id),
            None,
        )
        if isinstance(leases, list)
        else None
    )
    if isinstance(existing, dict):
        existing_id = existing.get("lease_id")
        recovered = _recover_lease_holder(target, existing_id) if isinstance(existing_id, str) else None
        if recovered is None:
            # Snapshots deliberately omit holders. Never invent ownership or a duplicate lease.
            raise RuntimeError("cloud lease holder unavailable")
        _attach_registered_lease_holder(target, task_id, recovered)
        return
    digest = prompt_hash or cloud_tracker.prompt_hash(label)
    admission = fleet_client.admit_cloud(
        "codex",
        repo=repo,
        label=cloud_tracker.lease_label("codex", repo, digest),
        prompt_hash=prompt_hash,
        ttl_seconds=300,
    )
    lease_id = admission.lease.get("lease_id") if isinstance(admission.lease, dict) else None
    if not admission.granted or not isinstance(lease_id, str):
        raise RuntimeError("cloud admission denied")
    try:
        _persist_lease_holder(target, lease_id, admission.holder)
    except Exception:  # noqa: BLE001 - this provider task already exists; retain its lease
        raise RuntimeError("cloud lease recovery unavailable") from None
    bound = fleet_client.bind_cloud(
        lease_id, provider_task_id=task_id, holder=admission.holder, ttl_seconds=fleet_client.CLOUD_BOUND_TTL_SECONDS
    )
    if not bound.granted:
        raise RuntimeError("cloud lease bind failed")
    if admission.holder:
        _attach_registered_lease_holder(target, task_id, admission.holder)


def parse_task_id(text: str) -> str | None:
    """Return only a strict Codex Cloud task id; never an arbitrary stdout token."""
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _scan_status(text: str) -> str | None:
    """Return the terminal status keyword from status output.

    The installed CLI prints `[STATUS] <task title>` as the first line, so a
    leading bracket token is authoritative when present. Otherwise scan only
    status-shaped lines (`Status: ...`), so incidental words in a task title
    ("fix failed tests") cannot terminate polling. Whole-text scanning is the
    last resort for unrecognized formats.
    """
    brackets = re.findall(r"^\s*\[([A-Za-z_ -]+)\]", text, re.M)
    if brackets:
        scope = "\n".join(brackets)
    else:
        status_lines = [line for line in text.splitlines() if re.match(r"\s*(task\s+)?(status|state)\b", line, re.I)]
        scope = "\n".join(status_lines) if status_lines else text
    lowered = scope.lower()
    for word in TERMINAL_FAIL + TERMINAL_OK:
        if re.search(rf"\b{word}\b", lowered):
            return word
    return None


def _verified_status(text: str) -> str | None:
    """Return a documented bracket or status-line state, never title prose."""
    bracket = re.search(r"^\s*\[([A-Za-z_ -]+)\]", text, re.M)
    status = re.search(r"^\s*(?:task\s+)?(?:status|state)\s*[:=]\s*([A-Za-z_-]+)", text, re.I | re.M)
    match = bracket or status
    if match is None:
        return None
    token = match.group(1).strip().lower().replace(" ", "_")
    return token if token in TERMINAL_FAIL + TERMINAL_OK + NONTERMINAL else None


class LeaseSyncResult(TypedDict):
    renewed: int
    released: int
    errors: int
    skipped: int
    reason: NotRequired[str]


def sync_leases(target: Path) -> LeaseSyncResult:
    """Renew or release live Codex leases held by this workspace."""
    counts: LeaseSyncResult = {"renewed": 0, "released": 0, "errors": 0, "skipped": 0}
    holders = {
        entry["task_id"]: entry["lease_holder"]
        for entry in cloud_tracker.load_registry(target)["entries"]
        if entry.get("provider") == "codex-cloud"
        and isinstance(entry.get("task_id"), str)
        and isinstance(entry.get("lease_holder"), str)
        and entry["lease_holder"]
    }
    if not holders:
        return counts
    lock_path = cloud_tracker.registry_path(target).with_name("codex-sync.lock")
    try:
        with held_file_lock(lock_path, deadline_seconds=0):
            return _sync_locked_leases(holders, counts)
    except InboxLockTimeout:
        counts["skipped"] = 1
        counts["reason"] = "already-running"
    except OSError:
        counts["errors"] += 1
        counts["reason"] = "lock-unavailable"
    return counts


def _sync_budget_exceeded(counts: LeaseSyncResult) -> LeaseSyncResult:
    counts["errors"] += 1
    counts["reason"] = "budget-exceeded"
    return counts


def _sync_locked_leases(holders: dict[str, str], counts: LeaseSyncResult) -> LeaseSyncResult:
    node_id = fleet_client.resolve_node_id()
    if not fleet_client._node_id_is_claimable(node_id):  # noqa: SLF001 - use the same identity rule as cloud mutations
        counts["errors"] += 1
        counts["reason"] = "no-identity"
        return counts
    deadline = time.monotonic() + LEASE_SYNC_BUDGET_SECONDS
    try:
        codex_leases = [lease for lease in fleet_client.fetch_cloud()["leases"] if lease.get("provider") == "codex"]
    except fleet_client.FleetClientError:
        counts["errors"] += 1
        return counts
    leases = [lease for lease in codex_leases if lease.get("owner_node") == node_id]
    if codex_leases and not leases:
        counts["skipped"] = len(codex_leases)
        counts["reason"] = "no-matching-node"
        return counts
    terminal: list[tuple[str, str, str]] = []
    for lease in leases:
        if lease.get("state") != "bound" or lease.get("released_at") is not None or lease.get("expired"):
            continue
        task_id = lease.get("provider_task_id")
        lease_id = lease.get("lease_id")
        expires_at = cloud_tracker._parse_time(lease.get("expires_at"))  # noqa: SLF001 - shared timestamp parser
        if (
            not isinstance(task_id, str)
            or re.fullmatch(r"task_[A-Za-z0-9][A-Za-z0-9_-]{2,}", task_id) is None
            or task_id not in holders
            or not isinstance(lease_id, str)
            or not lease_id
            or expires_at is None
            or expires_at.timestamp() <= time.time()
        ):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _sync_budget_exceeded(counts)
        try:
            result = proc.run(
                ["codex", "cloud", "status", task_id], timeout=min(DEFAULT_LIST_TIMEOUT, remaining), cwd=trusted_cwd()
            )
        except OSError:
            result = None
        if time.monotonic() >= deadline:
            return _sync_budget_exceeded(counts)
        if (
            result is None
            or result.code != 0
            or result.timed_out
            or _AUTH_FAILURE_RE.search(result.stdout + "\n" + result.stderr)
        ):
            counts["errors"] += 1
            continue
        state = _verified_status(result.stdout)
        if state is None:
            counts["errors"] += 1
        elif state in TERMINAL_OK + TERMINAL_FAIL:
            terminal.append((lease_id, state, holders[task_id]))
        else:
            # The probe may outlast the lease. The hub also checks expiry
            # atomically with renewal to close the remaining race.
            if expires_at.timestamp() > time.time():
                decision = fleet_client.renew_cloud(
                    lease_id, ttl_seconds=fleet_client.CLOUD_BOUND_TTL_SECONDS, holder=holders[task_id], node_id=node_id
                )
                if decision.granted:
                    counts["renewed"] += 1
                else:
                    counts["errors"] += 1

    # A scan that runs out of time leaves capacity fenced. Apply the
    # terminal observations only after the probe loop finishes in budget.
    for lease_id, state, holder in terminal:
        if time.monotonic() >= deadline:
            return _sync_budget_exceeded(counts)
        decision = fleet_client.release_cloud(lease_id, state=state, holder=holder, node_id=node_id)
        if decision.granted:
            counts["released"] += 1
        else:
            counts["errors"] += 1
    return counts


def config_path(target: Path) -> Path:
    return Path(target).expanduser().resolve() / ".brigade" / "cloud" / "codex.json"


def resolve_config_target(start: Path | None) -> Path:
    """Return the Brigade workspace root that owns cloud seat configuration."""
    if start is None:
        return Path(".").expanduser().resolve()
    current = Path(start).expanduser()
    try:
        current = current.resolve()
    except OSError:
        pass
    if current.is_file():
        current = current.parent
    home = Path.home()
    git_root: Path | None = None
    try:
        from . import runguard

        if runguard.is_git_worktree(current):
            git_root = runguard.git_root(current)
    except Exception:  # noqa: BLE001 - repo boundary is best-effort
        git_root = None
    for candidate in (current, *current.parents):
        if config_path(candidate).is_file():
            return candidate
        try:
            if (candidate / ".brigade").is_dir():
                return candidate
        except OSError:
            continue
        if candidate == home or (git_root is not None and candidate == git_root):
            break
    return current


def validate_selector_token(token: str) -> None:
    """Reject codex-cloud roster selectors that can only fail at dispatch."""
    if token is None:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    if token != token.strip():
        raise CodexCloudConfigError("codex-cloud selector must not contain surrounding whitespace")
    if any(ch.isspace() for ch in token):
        raise CodexCloudConfigError("codex-cloud selector must not contain whitespace")
    raw = token.strip()
    if not raw:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    if raw == CONFIGURED_SELECTOR:
        return
    if raw.startswith("$"):
        if not ENV_NAME_RE.fullmatch(raw[1:]):
            raise CodexCloudConfigError("invalid environment variable name")
        return
    if raw.startswith("-"):
        raise CodexCloudConfigError("codex-cloud literal environment id must not look like a CLI flag")
    if not LITERAL_SELECTOR_RE.fullmatch(raw):
        raise CodexCloudConfigError("invalid codex-cloud literal environment id")


def environment_audit_ref(selector_token: str, environment_id: str) -> dict[str, str]:
    """Return the least-sensitive audit payload for a resolved cloud environment.

    Literal roster tokens already declare the id in the reviewed roster, so
    local receipts may store the raw value. ``configured`` and ``$VAR`` selectors
    resolve from ambient process state; those runs store a stable fingerprint so
    operators can correlate evidence without echoing provider ids into hub or
    public output.
    """
    token = (selector_token or "").strip()
    if token == CONFIGURED_SELECTOR or token.startswith("$"):
        # Truncated unsalted digest for local receipt correlation only; not a secret token.
        digest = hashlib.sha256(environment_id.encode("utf-8")).hexdigest()[:12]
        return {"environment_fingerprint": f"sha256:{digest}"}
    return {"environment_id": environment_id}


def save_environment_config(
    target: Path,
    *,
    environment_id: str | None = None,
    environment_id_env: str | None = None,
) -> Path:
    """Write a local Codex Cloud environment reference. Prefer an env-var name."""
    if (environment_id is None) == (environment_id_env is None):
        raise CodexCloudConfigError("provide exactly one of environment_id or environment_id_env")
    if environment_id_env is not None:
        if not ENV_NAME_RE.fullmatch(environment_id_env):
            raise CodexCloudConfigError("invalid environment variable name")
        environment: dict[str, str] = {"kind": "env", "name": environment_id_env}
    else:
        value = (environment_id or "").strip()
        if not value:
            raise CodexCloudConfigError("environment_id is empty")
        environment = {"kind": "value", "id": value}
    path = config_path(target)
    _ensure_cloud_directory(target)
    localio.write_json(path, {"schema": CONFIG_SCHEMA, "environment": environment})
    path.chmod(0o600)
    return path


def load_environment_config(target: Path) -> dict[str, Any] | None:
    path = config_path(resolve_config_target(target))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexCloudConfigError("codex cloud config is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema") != CONFIG_SCHEMA:
        raise CodexCloudConfigError("codex cloud config uses an unsupported schema")
    return data


def _read_env_name(name: str, environ: Mapping[str, str]) -> str:
    if not ENV_NAME_RE.fullmatch(name):
        raise CodexCloudConfigError("invalid environment variable name")
    value = (environ.get(name) or "").strip()
    if not value:
        raise CodexCloudConfigError(f"{name} is unset")
    return value


def _resolve_configured(target: Path, environ: Mapping[str, str]) -> str:
    root = resolve_config_target(target)
    path = config_path(root)
    if path.is_file():
        config = load_environment_config(root)
        spec = config.get("environment") if isinstance(config, dict) else None
        if isinstance(spec, dict):
            kind = spec.get("kind")
            if kind == "env" and isinstance(spec.get("name"), str):
                return _read_env_name(spec["name"], environ)
            if kind == "value" and isinstance(spec.get("id"), str) and spec["id"].strip():
                return spec["id"].strip()
        raise CodexCloudConfigError("configured Codex Cloud environment is unavailable")
    fallback = (environ.get(DEFAULT_ENV_VAR) or "").strip()
    if fallback:
        return fallback
    raise CodexCloudConfigError("configured Codex Cloud environment is unavailable")


def resolve_environment_id(
    token: str,
    *,
    target: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve a roster token to a Codex Cloud environment id.

    Literal tokens pass through. ``$NAME`` reads that process environment
    variable. ``configured`` reads ``.brigade/cloud/codex.json`` or
    ``CODEX_CLOUD_ENV``. The resolved id is never logged here.
    """
    raw = (token or "").strip()
    if not raw:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    mapping = os.environ if environ is None else environ
    root = resolve_config_target(Path(target) if target is not None else None)
    if raw.startswith("$"):
        return _read_env_name(raw[1:], mapping)
    if raw == CONFIGURED_SELECTOR:
        return _resolve_configured(root, mapping)
    return raw


def _environment_seen(env_id: str, tasks: list[dict[str, Any]]) -> str:
    """Return whether inventory rows expose the resolved environment id."""
    if not tasks:
        return "unknown"
    seen_ids = {
        task_id
        for task in tasks
        if isinstance(task, dict) and isinstance(task_id := task.get("environment_id"), str) and task_id.strip()
    }
    if not seen_ids:
        return "unknown"
    return "true" if env_id in seen_ids else "false"


def doctor(
    target: Path,
    *,
    selector: str = CONFIGURED_SELECTOR,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report whether a Codex Cloud seat can resolve an environment id."""
    mapping = os.environ if environ is None else environ
    try:
        env_id = resolve_environment_id(selector, target=target, environ=mapping)
        configured = True
        audit = environment_audit_ref(selector, env_id)
    except CodexCloudConfigError:
        configured = False
        audit = None
    return {
        "ok": configured,
        "environment_configured": configured,
        "provider": "codex-cloud",
        "selector": selector,
        **(audit or {}),
    }


def canary(
    target: Path,
    *,
    selector: str = CONFIGURED_SELECTOR,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bounded inventory check. Never calls ``codex cloud exec`` or ``apply``."""
    mapping = os.environ if environ is None else environ
    try:
        env_id = resolve_environment_id(selector, target=target, environ=mapping)
    except CodexCloudConfigError:
        return {
            "ok": False,
            "environment_configured": False,
            "task_count": 0,
            "apply": False,
            "selector": selector,
            "reason": "environment-unconfigured",
        }
    inventory = list_tasks(cwd=target)
    audit = environment_audit_ref(selector, env_id)
    environment_seen = _environment_seen(env_id, inventory.tasks)
    if not inventory.ok:
        return {
            "ok": False,
            "environment_configured": True,
            "task_count": 0,
            "apply": False,
            "selector": selector,
            "environment_seen": environment_seen,
            "reason": inventory.reason,
            **audit,
        }
    report: dict[str, Any] = {
        "ok": True,
        "environment_configured": True,
        "task_count": len(inventory.tasks),
        "apply": False,
        "selector": selector,
        "environment_seen": environment_seen,
        **audit,
    }
    if environment_seen == "false":
        report["reason"] = "environment-not-seen"
    return report


def _environment_id(task: dict[str, Any]) -> str | None:
    for key in ("environment_id", "environmentId", "env_id", "envId"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    environment = task.get("environment")
    if isinstance(environment, str) and environment.strip():
        return environment.strip()
    if isinstance(environment, dict):
        value = environment.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_list_failure(result: proc.Result) -> str:
    if result.code == 127:
        return "provider-missing"
    if result.code == 126:
        return "provider-unavailable"
    if result.code == 124:
        return "provider-timeout"
    combined = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
    if result.code != 0 and _AUTH_FAILURE_RE.search(combined):
        return "auth-failure"
    return "provider-error"


def list_tasks(
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_pages: int = MAX_LIST_PAGES,
    cwd: Path | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    command_timeout: float = DEFAULT_LIST_TIMEOUT,
    include_launch_tags: bool = False,
) -> ListTasksResult:
    """Run ``codex cloud list --json`` and return a bounded, sanitized task list.

    Only the documented ``{tasks: [...], cursor}`` schema is accepted. Reads use
    the documented 1-20 item limit and cursor pagination, with fixed page and
    item caps; ``max_pages`` lowers the page cap (at least one page is read).
    Task rows retain id, normalized state, environment id, and, when
    requested by reconciliation, a validated launch tag extracted from the title.
    """
    item_cap = max(0, min(max_items, DEFAULT_MAX_ITEMS))
    sanitized: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_task_ids: set[str] = set()
    complete = False
    valid_rows = True
    for _page in range(max(1, min(max_pages, MAX_LIST_PAGES))):
        if len(sanitized) >= item_cap:
            break
        limit = min(MAX_LIST_LIMIT, item_cap - len(sanitized))
        if limit < 1:
            break
        argv = ["codex", "cloud", "list", "--json", "--limit", str(limit)]
        if cursor is not None:
            argv += ["--cursor", cursor]
        if process_registry is None:
            result = proc.run(argv, timeout=command_timeout, cwd=cwd)
        else:
            result = proc.run(argv, timeout=command_timeout, cwd=cwd, process_registry=process_registry)
        if result.code != 0:
            return ListTasksResult([], ok=False, reason=_classify_list_failure(result))
        try:
            data = json.loads((result.stdout or "").strip())
        except json.JSONDecodeError:
            return ListTasksResult([], ok=False, reason="list-json-invalid")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return ListTasksResult([], ok=False, reason="list-json-unsupported")
        for raw in data["tasks"]:
            if not isinstance(raw, dict):
                valid_rows = False
                continue
            task_id = raw.get("id")
            if not isinstance(task_id, str) or not parse_task_id(task_id) == task_id:
                valid_rows = False
                continue
            if task_id in seen_task_ids:
                continue
            task: dict[str, Any] = {
                "id": task_id,
                "state": cloud_tracker.normalize_provider_state(raw.get("status") or raw.get("state")),
            }
            environment_id = _environment_id(raw)
            if environment_id is not None:
                task["environment_id"] = environment_id
            if include_launch_tags:
                title = raw.get("title")
                if not isinstance(title, str) or not title.strip():
                    valid_rows = False
                else:
                    tags = _LAUNCH_TAG_RE.findall(title)
                    if len(tags) == 1:
                        task["launch_tag"] = tags[0]
                    elif tags:
                        valid_rows = False
            sanitized.append(task)
            seen_task_ids.add(task_id)
            if len(sanitized) >= item_cap:
                break
        next_cursor = data.get("cursor")
        if next_cursor in (None, ""):
            complete = valid_rows and len(data["tasks"]) <= limit
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return ListTasksResult(sanitized, complete=complete)


def run_cloud_task(
    prompt: str,
    *,
    env_id: str,
    timeout: float,
    cwd: Path | None = None,
    attempts: int = 1,
    branch: str | None = None,
    poll_interval: float = POLL_INTERVAL,
    sleep=time.sleep,
    clock=time.monotonic,
    process_registry: proc.ProcessRegistry | None = None,
    label: str | None = None,
    register_target: Path | None = None,
    session_id: str | None = None,
    environment_audit: dict[str, str] | None = None,
):
    """Submit, poll, and collect one Codex Cloud task. Mirrors run_agent's contract."""
    from .agents import AgentResult

    def timeout_result(detail: str, *, task_id: str | None = None, text: str = ""):
        return AgentResult(
            text=text[:DIFF_CAP],
            ok=False,
            detail=detail[:200],
            failure_kind="timeout",
            thread_id=task_id,
            status="timeout",
            timed_out=True,
            cloud_environment=environment_audit,
        )

    def fail_result(
        detail: str,
        *,
        task_id: str | None = None,
        status: str = "",
        failure_kind: str = "provider-error",
        timed_out: bool = False,
    ):
        return AgentResult(
            text="",
            ok=False,
            detail=detail[:200],
            failure_phase="dispatch",
            failure_kind=failure_kind,
            thread_id=task_id,
            status=status,
            timed_out=timed_out,
            cloud_environment=environment_audit,
        )

    try:
        hub_configured = _hub_configured()
    except Exception:  # noqa: BLE001 - configuration uncertainty must fail closed
        return fail_result(
            "fleet admission configuration unavailable", status="uncertain", failure_kind="fleet-admission-unavailable"
        )
    if hub_configured:
        try:
            _require_private_lease_storage()
        except OSError:
            return fail_result(
                "secure-owner-store-unavailable", status="denied", failure_kind="secure-owner-store-unavailable"
            )
    lease_id: str | None = None
    lease_id_str = ""
    holder: str | None = None
    prompt_hash = cloud_tracker.prompt_hash(prompt)
    repo: str | None = None
    tracker_label = cloud_tracker.lease_label("codex", None, prompt_hash)
    if hub_configured:
        repo = _repo_from_cwd(cwd)
        tracker_label = cloud_tracker.lease_label("codex", repo, prompt_hash)
        admit = fleet_client.admit_cloud(
            "codex",
            repo=repo,
            label=tracker_label,
            prompt_hash=prompt_hash,
            ttl_seconds=300,
        )
        if not admit.granted:
            failure_kind = "fleet-admission-denied" if admit.reason == "refused" else "fleet-admission-unavailable"
            return fail_result("cloud admission denied", status="denied", failure_kind=failure_kind)
        lease_id = admit.lease.get("lease_id") if isinstance(admit.lease, dict) else None
        holder = admit.holder
        if not isinstance(lease_id, str):
            return fail_result(
                "cloud admission returned no usable lease", status="denied", failure_kind="fleet-admission-denied"
            )
        # Narrow for mypy: the lease id is a string for the rest of the dispatch.
        lease_id_str = lease_id
    registry_root = register_target or cwd
    tag = f"brigade-launch-{uuid4().hex}"
    if registry_root is not None:
        try:
            _ensure_cloud_directory(registry_root)
            cloud_tracker.register(
                registry_root,
                provider="codex-cloud",
                label=tracker_label,
                prompt_hash=prompt_hash,
                session_id=session_id,
                branch=branch,
                environment_audit=environment_audit,
                expected_artifact=({"kind": "branch", "pattern": branch} if branch else {"kind": "diff"}),
                lease_holder=holder,
                launch_tag=tag,
                launch_lease_id=lease_id,
            )
        except Exception:  # noqa: BLE001 - no provider work exists yet
            if lease_id is not None:
                fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
            return fail_result("cloud registry is unwritable", status="denied", failure_kind="registry-unwritable")

    argv = ["codex", "cloud", "exec", "--env", env_id]
    if attempts > 1:
        argv += ["--attempts", str(attempts)]
    if branch:
        argv += ["--branch", branch]
    argv.append(_tagged_prompt(prompt, tag) if registry_root is not None else prompt)

    def run_command(command: list[str], *, command_timeout: float) -> proc.Result:
        if process_registry is None:
            return proc.run(command, timeout=command_timeout, cwd=cwd)
        return proc.run(command, timeout=command_timeout, cwd=cwd, process_registry=process_registry)

    deadline = clock() + timeout
    try:
        submit = run_command(argv, command_timeout=min(timeout, SUBMIT_TIMEOUT))
    except Exception:  # noqa: BLE001 - provider acceptance could be unknown
        return fail_result("cloud submit result was ambiguous", status="uncertain", failure_kind="uncertain")

    task_id = _submitted_task_id(submit)
    clean_failure = task_id is None and _clean_submit_failure(submit)
    if registry_root is not None and (task_id is not None or clean_failure):
        try:
            reason, _ = _finish_launch_intent(registry_root, tag, task_id=task_id)
        except Exception:  # noqa: BLE001 - the intent survives the failed update
            reason = "tracking-failed"
    elif hub_configured and task_id is not None:
        bind = fleet_client.bind_cloud(lease_id_str, provider_task_id=task_id, holder=holder)
        reason = "ok" if bind.granted else "bind-failed"
    else:
        reason = "ok"
    if reason == "bind-failed":
        return fail_result("cloud lease bind failed", task_id=task_id, status=reason, failure_kind="fleet-bind-failed")
    if reason != "ok":
        return fail_result("cloud registry update failed", task_id=task_id, status=reason, failure_kind=reason)

    if clean_failure:
        if lease_id is not None:
            fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
        return fail_result("cloud submit failed", status="submit-failed")
    if submit.code != 0 or task_id is None:
        if hub_configured:
            return fail_result(
                "cloud submit result was ambiguous",
                task_id=task_id,
                status="uncertain",
                failure_kind="uncertain",
                timed_out=submit.code == 124,
            )
        if submit.code == 124:
            return timeout_result("cloud submit timed out", task_id=task_id)
        if submit.code != 0:
            return fail_result("cloud submit failed", task_id=task_id, status="uncertain")
        return fail_result("cloud submit returned no usable task id", status="uncertain")

    def remaining(floor: float = 5.0) -> float:
        return max(floor, deadline - clock())

    status_text = ""
    status_word = None
    consecutive_hub_unavailable_renews = 0
    consecutive_unknown_nonzero = 0
    while True:
        st = run_command(
            ["codex", "cloud", "status", task_id],
            command_timeout=min(POLL_TIMEOUT, remaining()),
        )
        status_text = (st.stdout + "\n" + st.stderr).strip()
        # A documented terminal marker is authoritative even when the CLI
        # exits nonzero (live canary: `[ERROR]` plus exit 1). Known
        # nonterminal markers keep polling. Unknown nonzero output may
        # retry a bounded number of times, then fails closed.
        verified_status = _verified_status(status_text)
        if verified_status in TERMINAL_FAIL + TERMINAL_OK:
            status_word = verified_status
            break
        if st.code == 124:
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud status timed out",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="uncertain",
                    timed_out=True,
                    cloud_environment=environment_audit,
                )
            return timeout_result(
                "cloud status timed out",
                task_id=task_id,
                text="",
            )
        if st.code != 0 and _AUTH_FAILURE_RE.search(status_text):
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud status authentication failed",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="auth-failure",
                    timed_out=False,
                    cloud_environment=environment_audit,
                )
            return fail_result(
                "cloud status authentication failed",
                task_id=task_id,
                status="uncertain",
                failure_kind="auth-failure",
            )
        if st.code != 0 and verified_status not in NONTERMINAL:
            consecutive_unknown_nonzero += 1
            if consecutive_unknown_nonzero > MAX_TRANSIENT_STATUS_RETRIES:
                if hub_configured:
                    return AgentResult(
                        text="",
                        ok=False,
                        detail="cloud status unavailable",
                        thread_id=task_id,
                        status="uncertain",
                        failure_phase="dispatch",
                        failure_kind="uncertain",
                        timed_out=False,
                        cloud_environment=environment_audit,
                    )
                return fail_result(
                    "cloud status unavailable", task_id=task_id, status="uncertain", failure_kind="uncertain"
                )
        else:
            consecutive_unknown_nonzero = 0
        status_word = verified_status if verified_status in TERMINAL_FAIL + TERMINAL_OK else _scan_status(status_text)
        if hub_configured and verified_status not in TERMINAL_FAIL + TERMINAL_OK:
            status_word = None
        if status_word in TERMINAL_FAIL:
            break
        if status_word in TERMINAL_OK:
            break
        if hub_configured:
            renew = fleet_client.renew_cloud(lease_id_str, ttl_seconds=300, holder=holder)
            if not renew.granted:
                if renew.reason in ("refused", "auth-failed"):
                    return AgentResult(
                        text="",
                        ok=False,
                        detail="cloud lease renew refused",
                        thread_id=task_id,
                        status="uncertain",
                        failure_phase="dispatch",
                        failure_kind="fleet-lease-lost",
                        timed_out=False,
                        cloud_environment=environment_audit,
                    )
                if renew.reason == "hub-unavailable":
                    consecutive_hub_unavailable_renews += 1
                    if consecutive_hub_unavailable_renews >= MAX_CONSECUTIVE_HUB_UNAVAILABLE_RENEWS:
                        return AgentResult(
                            text="",
                            ok=False,
                            detail="cloud lease renew unavailable",
                            thread_id=task_id,
                            status="uncertain",
                            failure_phase="dispatch",
                            failure_kind="fleet-lease-lost",
                            timed_out=False,
                            cloud_environment=environment_audit,
                        )
                else:
                    consecutive_hub_unavailable_renews = 0
            else:
                consecutive_hub_unavailable_renews = 0
        if clock() >= deadline:
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud task timed out",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="uncertain",
                    timed_out=True,
                    cloud_environment=environment_audit,
                )
            return timeout_result(
                "cloud task timed out",
                task_id=task_id,
                text="",
            )
        sleep(poll_interval)

    # Verified terminal state: release the lease now.
    if hub_configured:
        fleet_client.release_cloud(lease_id_str, state=status_word or "released", holder=holder)

    if status_word in TERMINAL_FAIL:
        return AgentResult(
            text="",
            ok=False,
            detail=f"cloud task {status_word}"[:200],
            thread_id=task_id,
            status=status_word,
            cloud_environment=environment_audit,
        )

    diff = run_command(
        ["codex", "cloud", "diff", task_id],
        command_timeout=min(DIFF_TIMEOUT, remaining(floor=30.0)),
    )
    parts = [f"codex cloud task {task_id} [{status_word}]", status_text]
    if diff.code == 124:
        return timeout_result(
            "cloud diff timed out",
            task_id=task_id,
            text="",
        )
    if diff.code != 0:
        parts.append("WARNING: cloud diff failed")
    elif diff.stdout.strip():
        parts += [
            f"Unified diff (NOT applied locally; apply with `codex cloud apply {task_id}`):",
            diff.stdout.strip()[:DIFF_CAP],
        ]
    else:
        parts.append("No diff produced (research or no-change task).")
    text = "\n\n".join(p for p in parts if p)
    return AgentResult(
        text=text,
        ok=True,
        thread_id=task_id,
        status=status_word or "",
        cloud_environment=environment_audit,
    )
