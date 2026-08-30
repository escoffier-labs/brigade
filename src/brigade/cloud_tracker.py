"""Cloud dispatch registry: register, reconcile, and report without auto-delete (#890)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import localio

REGISTRY_SCHEMA = "brigade.run.cloud.registry.v1"
STATUS_SCHEMA = "brigade.run.cloud.status.v1"
SWEEP_SCHEMA = "brigade.run.cloud.sweep.v1"
SYNC_SCHEMA = "brigade.run.cloud.sync.v1"
MAINTENANCE_SCHEMA = "brigade.run.cloud.maintenance.v1"
DEFAULT_TERMINAL_KEEP = 50
DEFAULT_TERMINAL_MAX_AGE_HOURS = 168
PRESERVED_RETENTION_REASONS = ("active", "ambiguous", "orphaned", "needs-investigation")

DEFAULT_STALE_READY_HOURS = 6
CLOUD_BRANCH_PREFIXES = ("codex/", "cursor/", "grokbot/", "claude/", "jules/")
TRACKER_PROVIDERS = frozenset({"codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules"})

READY_STATES = frozenset({"ready", "completed", "succeeded", "applied"})
FAILED_STATES = frozenset({"failed", "errored", "error", "cancelled", "canceled", "expired"})
FINISHED_STATES = frozenset({"finished", "completed", "succeeded", "applied", "ready"})
PENDING_STATES = frozenset({"pending", "claimed", "running", "queued", "in_progress", "dispatching"})
# Jules non-terminal states hold capacity until the provider explicitly finishes.
JULES_HOLDING_STATES = frozenset(
    {"planning", "awaiting_plan_approval", "awaiting_user_feedback", "paused", "state_unspecified"}
)
ACTIVE_STATES = PENDING_STATES | JULES_HOLDING_STATES | frozenset({"creating", "active"})
TERMINAL_STATES = FINISHED_STATES | FAILED_STATES | frozenset({"interrupted", "timed_out", "timeout"})

CLASSIFICATIONS = (
    "pending",
    "ready-to-land",
    "landed",
    "stale",
    "orphaned",
    "needs-investigation",
)


@dataclass(frozen=True)
class ProviderObservation:
    """Bounded, sanitized health and inventory evidence for one provider."""

    configured: bool
    reachable: bool
    reason: str | None
    tasks: dict[str, dict[str, Any]]


def _root(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "cloud"


def registry_path(target: Path) -> Path:
    return _root(target) / "registry.json"


def prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


LEASE_LABEL_MAX = 120
_LEASE_LABEL_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def lease_label(provider: str, repo: str | None, prompt_hash_value: str | None) -> str:
    """Return a bounded ``provider:owner/repo@<digest>`` lease label.

    Prompt text is never a label. Hosted adapters call this so the hub, the
    registry, and every dashboard row carry a stable identifier derived from the
    provider, the repository, and the prompt hash instead of the prompt itself.
    """
    name = str(provider or "unknown").strip() or "unknown"
    slug = str(repo or "").strip().removeprefix("https://github.com/").rstrip("/")
    if not _LEASE_LABEL_REPO_RE.match(slug):
        slug = "unknown-repo"
    digest = str(prompt_hash_value or "").strip().rpartition(":")[2]
    digest = digest[:12] if re.fullmatch(r"[0-9a-fA-F]+", digest or "") else "nohash"
    return f"{name}:{slug}@{digest}"[:LEASE_LABEL_MAX]


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def empty_registry(*, stale_ready_hours: int = DEFAULT_STALE_READY_HOURS) -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": int(stale_ready_hours),
        "entries": [],
    }


def load_registry(target: Path) -> dict[str, Any]:
    path = registry_path(target)
    if not path.is_file():
        return empty_registry()
    data = localio.read_json_dict(path) or {}
    if data.get("schema") != REGISTRY_SCHEMA:
        # Soft-recover unknown/legacy payloads into the current schema shape.
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        hours = data.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS)
        try:
            stale_ready_hours = int(hours)
        except (TypeError, ValueError):
            stale_ready_hours = DEFAULT_STALE_READY_HOURS
        return {
            "schema": REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": stale_ready_hours,
            "entries": [e for e in entries if isinstance(e, dict)],
        }
    raw_entries = data.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    try:
        stale_ready_hours = int(data.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS))
    except (TypeError, ValueError):
        stale_ready_hours = DEFAULT_STALE_READY_HOURS
    loaded = {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": stale_ready_hours,
        "entries": [e for e in entries if isinstance(e, dict)],
    }
    retention = data.get("retention")
    if isinstance(retention, dict):
        loaded["retention"] = retention
    return loaded


def save_registry(target: Path, registry: dict[str, Any]) -> None:
    path = registry_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": int(registry.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS)),
        "entries": [e for e in (registry.get("entries") or []) if isinstance(e, dict)],
    }
    retention = registry.get("retention")
    if isinstance(retention, dict):
        payload["retention"] = retention
    localio.write_json(path, payload)


def _new_id() -> str:
    return f"cloud-{uuid4().hex[:12]}"


def register(
    target: Path,
    *,
    provider: str,
    task_id: str | None = None,
    label: str,
    prompt_hash: str | None = None,
    session_id: str | None = None,
    expected_artifact: dict[str, Any] | None = None,
    branch: str | None = None,
    dispatched_at: str | None = None,
    source: str = "dispatch",
    environment_audit: dict[str, str] | None = None,
    lease_holder: str | None = None,
) -> dict[str, Any]:
    if provider not in TRACKER_PROVIDERS:
        raise ValueError("provider must be one of: codex-cloud, cursor-cloud, grokbot-cloud, claude-cloud, jules")
    if not label.strip():
        raise ValueError("label must not be empty")
    if source == "dispatch" and not task_id:
        raise ValueError("dispatch registration requires task_id")
    registry = load_registry(target)
    entry = {
        "id": _new_id(),
        "provider": provider,
        "task_id": task_id,
        "label": label.strip(),
        "prompt_hash": prompt_hash,
        "session_id": session_id,
        "expected_artifact": expected_artifact or {"kind": "diff"},
        "branch": branch,
        "dispatched_at": dispatched_at or _now_iso(),
        "adopted_at": None,
        "source": source,
    }
    if environment_audit:
        for key in ("environment_id", "environment_fingerprint"):
            value = environment_audit.get(key)
            if isinstance(value, str) and value:
                entry[key] = value
    if isinstance(lease_holder, str) and lease_holder:
        entry["lease_holder"] = lease_holder
    registry["entries"].append(entry)
    save_registry(target, registry)
    return entry


def adopt(
    target: Path,
    *,
    provider: str,
    task_id: str | None = None,
    branch: str | None = None,
    label: str | None = None,
    prompt_hash: str | None = None,
    session_id: str | None = None,
    expected_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not task_id and not branch:
        raise ValueError("adopt requires --task-id and/or --branch")
    source = "adopt-task" if task_id and not branch else "adopt-branch" if branch and not task_id else "adopt-task"
    if task_id and branch:
        source = "adopt-task"
    resolved_label = (label or task_id or branch or "adopted").strip()
    artifact = expected_artifact
    if artifact is None and branch:
        artifact = {"kind": "branch", "pattern": branch}
    entry = register(
        target,
        provider=provider,
        task_id=task_id,
        label=resolved_label,
        prompt_hash=prompt_hash,
        session_id=session_id,
        expected_artifact=artifact,
        branch=branch,
        source=source,
    )
    # Stamp adopted_at without storing prompt text.
    registry = load_registry(target)
    for item in registry["entries"]:
        if item.get("id") == entry["id"]:
            item["adopted_at"] = _now_iso()
            entry = item
            break
    save_registry(target, registry)
    return entry


def normalize_provider_state(value: object) -> str | None:
    """Public provider-state normalization: lower-case, strip, or None."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return word or None


def _normalize_provider_state(value: object) -> str | None:
    """Internal alias kept for existing call sites."""
    return normalize_provider_state(value)


def is_active_state(state: str | None) -> bool:
    """True for states that consume hosted capacity."""
    return normalize_provider_state(state) in ACTIVE_STATES


def is_terminal_state(state: str | None) -> bool:
    """True for finished/failed states that should not hold capacity."""
    return normalize_provider_state(state) in TERMINAL_STATES


def _hours_since(earlier: datetime | None, now: datetime) -> float | None:
    if earlier is None:
        return None
    return max(0.0, (now - earlier).total_seconds() / 3600.0)


def _branch_names(github: dict[str, Any]) -> set[str]:
    branches = github.get("branches") if isinstance(github, dict) else None
    if not isinstance(branches, list):
        return set()
    names: set[str] = set()
    for item in branches:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
        elif isinstance(item, str):
            names.add(item)
    return names


def _prs_for_branch(github: dict[str, Any], branch: str | None) -> list[dict[str, Any]]:
    if not branch:
        return []
    prs = github.get("prs") if isinstance(github, dict) else None
    if not isinstance(prs, list):
        return []
    matched = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        if pr.get("head") == branch:
            matched.append(pr)
    return matched


def _classify_entry(
    entry: dict[str, Any],
    *,
    provider_tasks: dict[str, Any],
    github: dict[str, Any],
    stale_ready_hours: int,
    now: datetime,
    cursor_wired: bool,
    provider_observations: dict[str, ProviderObservation] | None = None,
) -> dict[str, Any]:
    provider = str(entry.get("provider") or "")
    task_id = entry.get("task_id")
    branch = entry.get("branch") if isinstance(entry.get("branch"), str) else None
    observation = provider_observations.get(provider) if provider_observations else None
    provider_info = observation.tasks.get(str(task_id)) if observation and task_id else None
    if provider_info is None:
        provider_info = provider_tasks.get(str(task_id)) if task_id else None
    provider_state = None
    ready_at = None
    if isinstance(provider_info, dict):
        provider_state = _normalize_provider_state(provider_info.get("state"))
        ready_at = _parse_time(provider_info.get("ready_at"))
    branches = _branch_names(github)
    prs = _prs_for_branch(github, branch)
    merged = any(str(pr.get("state", "")).upper() == "MERGED" for pr in prs)
    open_pr = any(str(pr.get("state", "")).upper() == "OPEN" for pr in prs)
    branch_exists = bool(branch and branch in branches)
    raw_expected = entry.get("expected_artifact")
    expected = raw_expected if isinstance(raw_expected, dict) else {}
    expects_branch = expected.get("kind") in {"branch", "draft-pr"}

    evidence: dict[str, Any] = {
        "registry": {"id": entry.get("id"), "source": entry.get("source"), "dispatched_at": entry.get("dispatched_at")},
        "provider": {
            "wired": observation.configured if observation else provider != "cursor-cloud" or cursor_wired,
            "state": provider_state,
            "task_id": task_id,
        },
        "github": {
            "branch": branch,
            "branch_exists": branch_exists,
            "prs": prs,
        },
    }
    if observation is not None:
        evidence["provider"].update(
            {
                "configured": observation.configured,
                "reachable": observation.reachable,
                "reason": observation.reason,
            }
        )

    classification = "pending"
    if provider == "cursor-cloud" and not (observation.configured if observation else cursor_wired):
        evidence["provider"] = "unwired"
        if branch_exists and (provider_state in FAILED_STATES or not task_id):
            classification = "orphaned"
        elif merged:
            classification = "landed"
        else:
            classification = "pending"
    elif merged:
        classification = "landed"
    elif provider_state in FAILED_STATES and branch_exists:
        classification = "orphaned"
    elif provider_state in READY_STATES or provider_state == "finished":
        # Ready / finished without a merge: landable, stale, or needs investigation.
        if expects_branch and not branch_exists and not open_pr:
            classification = "needs-investigation"
        else:
            ready_mark = ready_at or _parse_time(entry.get("dispatched_at"))
            age_hours = _hours_since(ready_mark, now)
            if age_hours is not None and age_hours >= stale_ready_hours:
                classification = "stale"
            else:
                classification = "ready-to-land"
    elif provider_state in PENDING_STATES or provider_state is None:
        classification = "pending"
    elif provider_state in FAILED_STATES:
        classification = "needs-investigation" if expects_branch and not branch_exists else "pending"
    else:
        classification = "pending"

    return {
        "id": entry.get("id"),
        "provider": provider,
        "task_id": task_id,
        "label": entry.get("label"),
        "branch": branch,
        "classification": classification,
        "provider_state": provider_state,
        "prompt_hash": entry.get("prompt_hash"),
        "session_id": entry.get("session_id"),
        "dispatched_at": entry.get("dispatched_at"),
        "source": entry.get("source"),
        "evidence": evidence,
        "pr": prs[0] if prs else None,
    }


def _orphan_branch_rows(
    *,
    registry_entries: list[dict[str, Any]],
    github: dict[str, Any],
    now: datetime,
    known_branches: set[str] | None = None,
) -> list[dict[str, Any]]:
    registered_branches = {
        e.get("branch") for e in registry_entries if isinstance(e.get("branch"), str) and e.get("branch")
    }
    registered_branches |= {
        e.get("expected_artifact", {}).get("pattern")
        for e in registry_entries
        if isinstance(e.get("expected_artifact"), dict)
        and isinstance(e.get("expected_artifact", {}).get("pattern"), str)
        and "/" in str(e.get("expected_artifact", {}).get("pattern"))
        and "*" not in str(e.get("expected_artifact", {}).get("pattern"))
    }
    if known_branches:
        registered_branches |= known_branches
    rows: list[dict[str, Any]] = []
    for name in sorted(_branch_names(github)):
        if not name.startswith(CLOUD_BRANCH_PREFIXES):
            continue
        if name in registered_branches:
            continue
        provider = _provider_for_branch(name)
        rows.append(
            {
                "id": f"orphan-branch:{name}",
                "provider": provider,
                "task_id": None,
                "label": name,
                "branch": name,
                "classification": "orphaned",
                "provider_state": None,
                "prompt_hash": None,
                "session_id": None,
                "dispatched_at": None,
                "source": "github-branch",
                "evidence": {
                    "registry": None,
                    "provider": {"wired": False, "state": None, "task_id": None},
                    "github": {"branch": name, "branch_exists": True, "prs": _prs_for_branch(github, name)},
                },
                "pr": _prs_for_branch(github, name)[0] if _prs_for_branch(github, name) else None,
                "observed_at": _now_iso(now),
            }
        )
    return rows


def _provider_for_branch(branch: str) -> str:
    if branch.startswith("cursor/"):
        return "cursor-cloud"
    if branch.startswith("grokbot/"):
        return "grokbot-cloud"
    if branch.startswith("claude/"):
        return "claude-cloud"
    if branch.startswith("jules/"):
        return "jules"
    return "codex-cloud"


def _grokbot_rows(
    target: Path, *, github: dict[str, Any], now: datetime, stale_ready_hours: int
) -> list[dict[str, Any]]:
    """Project queue jobs into tracker rows without reading private envelopes."""
    try:
        from . import grokbot_jobs

        queue_rows = grokbot_jobs.tracker_rows(target)
    except Exception:  # noqa: BLE001 - tracker remains available when queue storage is unavailable
        from . import grokbot_jobs

        if grokbot_jobs.hub_authority(target):
            return [_grokbot_hub_unavailable_row()]
        return []

    if any(isinstance(job, dict) and job.get("degraded") for job in queue_rows):
        return [_grokbot_hub_unavailable_row()]

    rows: list[dict[str, Any]] = []
    for job in queue_rows:
        job_id = job.get("job_id")
        if not isinstance(job_id, str):
            continue
        raw_artifact = job.get("artifact")
        artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
        raw_completed = job.get("result_artifact")
        completed: dict[str, Any] = raw_completed if isinstance(raw_completed, dict) else {}
        branch = completed.get("branch") if isinstance(completed.get("branch"), str) else None
        refs = _grokbot_artifact_refs(artifact=artifact, completed=completed, github=github, branch=branch)
        augmented_github = github
        if branch and refs.get("pr_url") and not _prs_for_branch(github, branch):
            raw_prs = github.get("prs")
            prs: list[Any] = list(raw_prs) if isinstance(raw_prs, list) else []
            augmented_github = {
                **github,
                "prs": [
                    *prs,
                    {"head": branch, "state": "OPEN", "url": refs["pr_url"]},
                ],
            }
        classification_row = _classify_entry(
            {
                "id": f"grokbot:{job_id}",
                "provider": "grokbot-cloud",
                "task_id": job_id,
                "label": job.get("label"),
                "branch": branch,
                "dispatched_at": job.get("queued_at"),
                "source": "grokbot-hub" if grokbot_jobs.hub_authority(target) else "grokbot-queue",
                "expected_artifact": artifact,
            },
            provider_tasks={job_id: {"state": job.get("state"), "ready_at": job.get("updated_at")}},
            github=augmented_github,
            stale_ready_hours=stale_ready_hours,
            now=now,
            cursor_wired=False,
        )
        row: dict[str, Any] = {
            "id": f"grokbot:{job_id}",
            "provider": "grokbot-cloud",
            "job_id": job_id,
            "label": job.get("label"),
            "task_hash": job.get("task_hash"),
            "state": job.get("state"),
            "classification": classification_row["classification"],
            "artifact_refs": refs,
            "source": "grokbot-hub" if grokbot_jobs.hub_authority(target) else "grokbot-queue",
        }
        for key in ("created_at", "updated_at", "queued_at", "claimed_at"):
            if isinstance(job.get(key), str):
                row[key] = job[key]
        rows.append(row)
    return rows


def _grokbot_hub_unavailable_row() -> dict[str, Any]:
    return {
        "id": "grokbot:hub-unavailable",
        "provider": "grokbot-cloud",
        "job_id": "grokbot-hub-unavailable",
        "label": "Grok Bot hub unavailable",
        "state": "unavailable",
        "classification": "needs-investigation",
        "degraded": True,
        "artifact_refs": {"kind": "report"},
        "source": "grokbot-hub",
    }


def _grokbot_artifact_refs(
    *, artifact: dict[str, Any], completed: dict[str, Any], github: dict[str, Any], branch: str | None
) -> dict[str, Any]:
    """Keep only artifact identifiers and GitHub PR metadata safe for operators."""
    kind = artifact.get("kind") if isinstance(artifact.get("kind"), str) else None
    refs: dict[str, Any] = {"kind": kind}
    if branch:
        refs["branch"] = branch
    if kind == "draft-pr":
        url = completed.get("url") if isinstance(completed.get("url"), str) else None
        pr = _prs_for_branch(github, branch)[0] if branch and _prs_for_branch(github, branch) else None
        if isinstance(pr, dict):
            if isinstance(pr.get("url"), str):
                url = pr["url"]
            if isinstance(pr.get("isDraft"), bool):
                refs["is_draft"] = pr["isDraft"]
            if isinstance(pr.get("headRefOid"), str):
                refs["head_sha"] = pr["headRefOid"]
        if url:
            refs["pr_url"] = url
    elif kind == "branch" and isinstance(completed.get("commit"), str):
        refs["head_sha"] = completed["commit"]
    elif kind == "report":
        if isinstance(completed.get("sha256"), str):
            refs["sha256"] = completed["sha256"]
        if isinstance(completed.get("private_snapshot_id"), str):
            refs["private_snapshot_id"] = completed["private_snapshot_id"]
        if type(completed.get("size")) is int:
            refs["size"] = completed["size"]
        if isinstance(completed.get("path"), str) and not completed.get("private_snapshot_id"):
            refs["path"] = completed["path"]
    return refs


def status_payload(
    target: Path,
    *,
    now: datetime | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool = False,
    provider_observations: dict[str, ProviderObservation] | None = None,
) -> dict[str, Any]:
    from . import grokbot_jobs

    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    registry = load_registry(target)
    stale_ready_hours = int(registry.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS))
    provider_tasks = provider_tasks if isinstance(provider_tasks, dict) else {}
    github = github if isinstance(github, dict) else {"branches": [], "prs": []}
    jules_wired = jules_cloud_wired()
    provider_observations = provider_observations if isinstance(provider_observations, dict) else None

    entries = [
        _classify_entry(
            entry,
            provider_tasks=provider_tasks,
            github=github,
            stale_ready_hours=stale_ready_hours,
            now=observed,
            cursor_wired=cursor_wired,
            provider_observations=provider_observations,
        )
        for entry in registry["entries"]
    ]
    grokbot_rows = _grokbot_rows(target, github=github, now=observed, stale_ready_hours=stale_ready_hours)
    grokbot_degraded = any(row.get("degraded") for row in grokbot_rows)
    entries.extend(grokbot_rows)
    known_queue_branches = {
        refs["branch"]
        for row in grokbot_rows
        if isinstance((refs := row.get("artifact_refs")), dict) and isinstance(refs.get("branch"), str)
    }
    entries.extend(
        _orphan_branch_rows(
            registry_entries=registry["entries"],
            github=github,
            now=observed,
            known_branches=known_queue_branches,
        )
    )

    sources: dict[str, dict[str, Any]] = {
        "codex-cloud": {"wired": True, "authority": "best-effort"},
        "cursor-cloud": {
            "wired": bool(cursor_wired),
            "authority": "best-effort" if cursor_wired else "unwired",
            "detail": None if cursor_wired else "no API key; cursor cloud left unwired",
        },
        "claude-cloud": {
            "wired": False,
            "authority": "disabled-by-policy",
            "detail": "local/background sessions are not cloud discovery; claude cloud remains untracked/disabled until a structured bindable provider surface exists",
        },
        "jules": {
            "wired": jules_wired,
            "authority": "alpha REST" if jules_wired else "unwired",
            "detail": None if jules_wired else "JULES_API_KEY not set",
        },
        "grokbot-cloud": {
            "wired": True,
            "authority": "hub" if grokbot_jobs.hub_authority(target) else "local-queue",
            **({"degraded": True, "detail": "hub unavailable"} if grokbot_degraded else {}),
        },
        "github": {"wired": True, "authority": "ground-truth"},
    }
    if provider_observations:
        for provider, observation in provider_observations.items():
            source = sources.get(provider)
            if source is None:
                continue
            source.update(
                {
                    "wired": observation.configured,
                    "configured": observation.configured,
                    "reachable": observation.reachable,
                    "reason": observation.reason,
                }
            )
            if provider == "claude-cloud":
                source["detail"] = "disabled-by-policy"
            elif observation.reason is not None:
                source["detail"] = observation.reason
            elif "detail" in source:
                source["detail"] = None

    return {
        "schema": STATUS_SCHEMA,
        "target": str(target.expanduser().resolve()),
        "observed_at": _now_iso(observed),
        "stale_ready_hours": stale_ready_hours,
        "sources": sources,
        "entries": entries,
        "counts": {
            classification: sum(1 for row in entries if row.get("classification") == classification)
            for classification in CLASSIFICATIONS
        },
    }


def sync_payload(
    target: Path,
    *,
    now: datetime | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool = False,
    provider_observations: dict[str, ProviderObservation] | None = None,
    hub_leases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile registry observations with hub leases without secrets.

    Status is read-only; sync is the only mutating observation step and it is
    bounded. Terminal observations release matching hub leases; active observations
    are kept. No Needs You row is invented just because a terminal local registry
    entry has no matching hub lease. No prompt text, bearer, key, or presigned URL
    is recorded.
    """
    from . import fleet_client

    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    status = status_payload(
        target,
        now=observed,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=cursor_wired,
        provider_observations=provider_observations,
    )
    hub_leases = hub_leases if isinstance(hub_leases, list) else []

    active: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    for row in status.get("entries", []):
        if not isinstance(row, dict):
            continue
        state = row.get("provider_state")
        if is_active_state(state):
            active.append(row)
        elif is_terminal_state(state):
            terminal.append(row)

    registry_entries = {entry.get("id"): entry for entry in load_registry(target)["entries"] if isinstance(entry, dict)}
    renewed: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []
    needs_you: list[dict[str, Any]] = []
    for row in active:
        task_id = row.get("task_id")
        if not isinstance(task_id, str):
            continue
        match = next((lease for lease in hub_leases if str(lease.get("provider_task_id")) == task_id), None)
        holder = registry_entries.get(row.get("id"), {}).get("lease_holder")
        if isinstance(match, dict) and match.get("lease_id") and isinstance(holder, str):
            try:
                decision = fleet_client.renew_cloud(str(match["lease_id"]), holder=holder)
                if decision.granted:
                    renewed.append({"id": row.get("id"), "task_id": task_id, "lease_id": match["lease_id"]})
                else:
                    needs_you.append({"id": row.get("id"), "detail": f"hub lease renewal refused: {decision.reason}"})
            except Exception as exc:  # noqa: BLE001 - sync must stay bounded
                needs_you.append({"id": row.get("id"), "detail": f"hub lease renewal failed: {type(exc).__name__}"})
    for row in terminal:
        # Release a matching hub lease when a terminal observation is known and a
        # lease id is present. Failures are recorded, not rethrown; no Needs You row
        # is invented merely because a local registry entry has no matching lease.
        task_id = row.get("task_id")
        if not isinstance(task_id, str):
            continue
        match = next((lease for lease in hub_leases if str(lease.get("provider_task_id")) == task_id), None)
        if isinstance(match, dict) and match.get("lease_id"):
            registry_entry = registry_entries.get(row.get("id"), {})
            holder = registry_entry.get("lease_holder")
            try:
                decision = fleet_client.release_cloud(
                    str(match["lease_id"]),
                    state=str(row.get("classification", "released")),
                    holder=holder if isinstance(holder, str) else None,
                )
                if decision.granted:
                    released.append({"id": row.get("id"), "task_id": task_id, "lease_id": match["lease_id"]})
                else:
                    needs_you.append({"id": row.get("id"), "detail": f"hub lease release refused: {decision.reason}"})
            except Exception as exc:  # noqa: BLE001 - sync must stay bounded
                needs_you.append({"id": row.get("id"), "detail": f"hub lease release failed: {type(exc).__name__}"})

    return {
        "schema": SYNC_SCHEMA,
        "action": "reconcile",
        "target": str(target.expanduser().resolve()),
        "observed_at": status.get("observed_at"),
        "stale_ready_hours": status.get("stale_ready_hours"),
        "sources": status.get("sources"),
        "active": active[:100],
        "renewed": renewed[:100],
        "released": released[:100],
        "needs_you": needs_you[:10],
        "counts": {
            "active": len(active),
            "renewed": len(renewed),
            "released": len(released),
            "needs_you": len(needs_you),
        },
    }


def stale_entries_for_brief(target: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Best-effort stale summary for work brief; fail-open on observe errors."""
    try:
        provider_tasks, github, cursor_wired = observe_providers(target)
        payload = status_payload(
            target,
            now=now,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
        )
    except Exception as exc:  # noqa: BLE001 - brief must stay available
        return {
            "stale_count": 0,
            "stale_entries": [],
            "error": f"{type(exc).__name__}: {exc}",
            "suggested_command": "brigade run cloud status --json",
        }
    stale = [e for e in payload.get("entries", []) if e.get("classification") == "stale"]
    return {
        "stale_count": len(stale),
        "stale_entries": stale[:10],
        "stale_ready_hours": payload.get("stale_ready_hours"),
        "suggested_command": "brigade run cloud status --json",
    }


def sweep(target: Path, *, now: datetime | None = None, status: dict[str, Any] | None = None) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    payload = status if isinstance(status, dict) else status_payload(target, now=observed)
    recoverable: list[dict[str, Any]] = []
    deletable: list[dict[str, Any]] = []
    for row in payload.get("entries", []):
        if not isinstance(row, dict):
            continue
        classification = row.get("classification")
        item = {
            "id": row.get("id"),
            "label": row.get("label"),
            "branch": row.get("branch"),
            "classification": classification,
            "evidence": row.get("evidence"),
        }
        if classification in {"ready-to-land", "stale", "needs-investigation", "pending"}:
            recoverable.append(item)
        elif classification == "orphaned":
            deletable.append(item)
        # landed: neither recoverable work nor deletable junk in the sweep sense
    sweep_id = f"{observed.strftime('%Y%m%d-%H%M%S')}-cloud-sweep-{uuid4().hex[:6]}"
    report = {
        "schema": SWEEP_SCHEMA,
        "sweep_id": sweep_id,
        "target": str(target.expanduser().resolve()),
        "observed_at": _now_iso(observed),
        "action": "report-only",
        "recoverable": recoverable,
        "deletable": deletable,
        "note": "Nothing was deleted. Review deletable branches, then remove them manually or via an authorized step.",
    }
    receipt_dir = _root(target) / "sweeps" / sweep_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    localio.write_json(receipt_dir / "sweep.json", report)
    return report


def default_retention_policy(
    *,
    keep_terminal: int | None = None,
    max_age_hours: int | None = None,
) -> dict[str, Any]:
    """Return the auditable registry retention contract."""
    return {
        "preserve": list(PRESERVED_RETENTION_REASONS),
        "bound": ["landed", "terminal"],
        "keep_terminal": DEFAULT_TERMINAL_KEEP if keep_terminal is None else int(keep_terminal),
        "max_age_hours": DEFAULT_TERMINAL_MAX_AGE_HOURS if max_age_hours is None else int(max_age_hours),
        "sort": ["dispatched_at_desc", "id_desc"],
    }


def _row_is_preserved(row: dict[str, Any]) -> bool:
    """Keep active, ambiguous, orphaned, needs-investigation, and current work."""
    classification = row.get("classification")
    state = row.get("provider_state")
    if classification in {"orphaned", "needs-investigation", "pending", "ready-to-land", "stale"}:
        return True
    if is_active_state(state):
        return True
    if not is_terminal_state(state):
        return True
    return False


def compact_registry(
    target: Path,
    *,
    now: datetime | None = None,
    keep_terminal: int | None = None,
    max_age_hours: int | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool = False,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit maintenance: bound landed/terminal history and write atomically.

    Read-only status never calls this. Active, ambiguous, orphaned, and
    needs-investigation rows are always preserved. Missing timestamps are
    treated as ambiguous and kept.
    """
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    policy = default_retention_policy(keep_terminal=keep_terminal, max_age_hours=max_age_hours)
    if policy["keep_terminal"] < 0:
        raise ValueError("keep_terminal must be >= 0")
    if policy["max_age_hours"] < 1:
        raise ValueError("max_age_hours must be >= 1")

    registry = load_registry(target)
    payload = (
        status
        if isinstance(status, dict)
        else status_payload(
            target,
            now=observed,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
        )
    )
    rows_by_id = {
        row.get("id"): row for row in payload.get("entries", []) if isinstance(row, dict) and row.get("id") is not None
    }

    preserved: list[dict[str, Any]] = []
    eligible: list[tuple[datetime, str, dict[str, Any]]] = []
    for entry in registry["entries"]:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        row = rows_by_id.get(entry_id, {})
        if not isinstance(row, dict) or not row or _row_is_preserved(row):
            preserved.append(entry)
            continue
        dispatched = _parse_time(entry.get("dispatched_at"))
        if dispatched is None:
            preserved.append(entry)
            continue
        eligible.append((dispatched, str(entry_id or ""), entry))

    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)
    kept_terminal: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for dispatched, _entry_id, entry in eligible:
        age = _hours_since(dispatched, observed)
        over_age = age is not None and age >= policy["max_age_hours"]
        over_count = len(kept_terminal) >= policy["keep_terminal"]
        if over_age or over_count:
            dropped.append(entry)
        else:
            kept_terminal.append(entry)

    kept_ids = {item.get("id") for item in (*preserved, *kept_terminal)}
    registry["entries"] = [
        entry for entry in registry["entries"] if isinstance(entry, dict) and entry.get("id") in kept_ids
    ]
    registry["retention"] = {**policy, "compacted_at": _now_iso(observed)}
    save_registry(target, registry)

    maintenance_id = f"{observed.strftime('%Y%m%d-%H%M%S')}-cloud-compact-{uuid4().hex[:6]}"
    report = {
        "schema": MAINTENANCE_SCHEMA,
        "maintenance_id": maintenance_id,
        "action": "compact",
        "target": str(target.expanduser().resolve()),
        "observed_at": _now_iso(observed),
        "policy": policy,
        "kept": len(registry["entries"]),
        "dropped_ids": [entry.get("id") for entry in dropped],
        "counts": {"kept": len(registry["entries"]), "dropped": len(dropped)},
        "atomic": True,
    }
    receipt_dir = _root(target) / "maintenance" / maintenance_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    localio.write_json(receipt_dir / "compact.json", report)
    return report


def _run_text(command: list[str], *, cwd: Path | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _parse_codex_cloud_list(stdout: str) -> dict[str, Any]:
    """Best-effort adapter for `codex cloud list` / status-shaped text."""
    tasks: dict[str, Any] = {}
    # JSON array or object with tasks
    text = stdout.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    tasks[str(item["id"])] = {
                        "state": _normalize_provider_state(item.get("status") or item.get("state")),
                        "ready_at": item.get("ready_at") or item.get("updated_at"),
                    }
            return tasks
        if isinstance(data, dict):
            raw_tasks = data.get("tasks")
            items = raw_tasks if isinstance(raw_tasks, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    tasks[str(item["id"])] = {
                        "state": _normalize_provider_state(item.get("status") or item.get("state")),
                        "ready_at": item.get("ready_at") or item.get("updated_at"),
                    }
            return tasks
    # Line adapter: task_id <id> ... [READY] / Status: ready
    current_id: str | None = None
    for line in text.splitlines():
        id_match = re.search(r"\b(task_[A-Za-z0-9-]{4,})\b", line)
        if id_match:
            current_id = id_match.group(1)
            tasks.setdefault(current_id, {"state": None, "ready_at": None})
        bracket = re.search(r"\[([A-Za-z_ -]+)\]", line)
        status_line = re.search(r"(?i)\bstatus\b\s*[:=]\s*([A-Za-z_ -]+)", line)
        state = None
        if bracket:
            state = _normalize_provider_state(bracket.group(1))
        elif status_line:
            state = _normalize_provider_state(status_line.group(1))
        if state and current_id:
            tasks[current_id]["state"] = state
    return tasks


def _codex_cloud_observation(target: Path) -> ProviderObservation:
    from . import codex_cloud

    try:
        inventory = codex_cloud.list_tasks(cwd=target)
    except Exception:  # noqa: BLE001 - observation must stay bounded
        return ProviderObservation(True, False, "transport-failure", {})
    tasks = {
        row["id"]: {"state": row.get("state"), "ready_at": None}
        for row in inventory.tasks
        if isinstance(row.get("id"), str)
    }
    if not tasks:
        # Preserve the established bounded positive-evidence fallback for
        # registered tasks. Failure or absence never implies a terminal state.
        registry = load_registry(target)
        for entry in registry["entries"]:
            task_id = entry.get("task_id")
            if not isinstance(task_id, str) or entry.get("provider") != "codex-cloud":
                continue
            scode, sout, serr = _run_text(["codex", "cloud", "status", task_id], cwd=target)
            blob = (sout + "\n" + serr).strip()
            if scode != 0 and not blob:
                continue
            state = codex_cloud._scan_status(blob)  # noqa: SLF001 - shared status scanner
            if state is not None:
                tasks[task_id] = {"state": state, "ready_at": None}
    if not inventory.ok:
        reason = inventory.reason or "provider-error"
        if reason in {"provider-missing", "provider-unavailable"}:
            return ProviderObservation(False, False, reason, tasks)
        if reason == "provider-timeout":
            return ProviderObservation(True, False, "transport-failure", tasks)
        if reason == "auth-failure":
            return ProviderObservation(True, False, reason, tasks)
        return ProviderObservation(True, True, reason, tasks)
    return ProviderObservation(True, True, None, tasks)


def observe_codex_cloud_tasks(target: Path) -> dict[str, Any]:
    """Compatibility wrapper returning only Codex Cloud inventory rows."""
    return _codex_cloud_observation(target).tasks


def observe_github(target: Path) -> dict[str, Any]:
    """Ground truth for cloud-shaped branches and PRs via gh."""
    branches: list[dict[str, str]] = []
    code, stdout, _ = _run_text(
        ["gh", "api", "repos/{owner}/{repo}/branches?per_page=100"],
        cwd=target,
    )
    if code == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    name = item["name"]
                    if name.startswith(CLOUD_BRANCH_PREFIXES):
                        branches.append({"name": name})
    # Also accept local refs when gh is unavailable so adopt/sweep still works offline in tests.
    if not branches:
        code, stdout, _ = _run_text(["git", "branch", "-a", "--format=%(refname:short)"], cwd=target)
        if code == 0:
            for line in stdout.splitlines():
                name = line.strip().removeprefix("origin/")
                if name.startswith(CLOUD_BRANCH_PREFIXES):
                    branches.append({"name": name})

    prs: list[dict[str, Any]] = []
    code, stdout, _ = _run_text(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--json",
            "number,state,headRefName,url,isDraft,headRefOid",
            "--limit",
            "100",
        ],
        cwd=target,
    )
    if code == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                head = item.get("headRefName")
                if isinstance(head, str) and head.startswith(CLOUD_BRANCH_PREFIXES):
                    prs.append(
                        {
                            "head": head,
                            "state": item.get("state"),
                            "number": item.get("number"),
                            "url": item.get("url"),
                            "isDraft": item.get("isDraft"),
                            "headRefOid": item.get("headRefOid"),
                        }
                    )
    return {"branches": branches, "prs": prs}


def cursor_cloud_wired() -> bool:
    """Cursor cloud stays unwired until an API key exists; never guess task state."""
    import os

    for key in ("CURSOR_API_KEY", "CURSOR_CLOUD_API_KEY", "BG_AGENT_API_KEY"):
        value = os.environ.get(key, "").strip()
        if value:
            return True
    return False


def _cursor_api_key() -> str | None:
    """Return the first configured Cursor API key, or None."""
    import os

    for key in ("CURSOR_API_KEY", "CURSOR_CLOUD_API_KEY", "BG_AGENT_API_KEY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _provider_error_observation(error: BaseException) -> ProviderObservation:
    reason = getattr(error, "reason", None)
    if reason == "auth-failure":
        return ProviderObservation(True, False, "auth-failure", {})
    if reason == "transport-failure" or isinstance(error, (OSError, TimeoutError)):
        return ProviderObservation(True, False, "transport-failure", {})
    return ProviderObservation(True, True, "provider-error", {})


def _cursor_cloud_observation(target: Path) -> ProviderObservation:
    """Fetch sanitized Cursor Cloud inventory when an API key is present.

    Provider API failures are bounded: the tracker stays available and no
    existing registry entries are erased or admitted.
    """
    api_key = _cursor_api_key()
    if not api_key:
        return ProviderObservation(False, False, "unconfigured", {})
    try:
        from . import cursor_cloud

        agents = cursor_cloud.list_agents(api_key, max_pages=2, max_items=100, include_usage=True)
    except Exception as exc:  # noqa: BLE001 - observation must stay bounded
        return _provider_error_observation(exc)
    tasks: dict[str, Any] = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            continue
        row: dict[str, Any] = {
            "state": cursor_cloud.normalize_state(agent.get("latestRunState")),
            "agent_id": agent_id,
        }
        run_id = agent.get("latestRunId")
        if isinstance(run_id, str) and run_id:
            row["run_id"] = run_id
        duration = agent.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            row["duration_ms"] = duration
        updated = agent.get("updated_at")
        if isinstance(updated, str) and updated:
            row["updated_at"] = updated
        usage = cursor_cloud.sanitize_token_usage(agent.get("usage"))
        if usage:
            row["usage"] = usage
        tasks[agent_id] = row
    return ProviderObservation(True, True, None, tasks)


def observe_cursor_cloud_tasks(target: Path) -> dict[str, Any]:
    """Compatibility wrapper returning only Cursor Cloud inventory rows."""
    return _cursor_cloud_observation(target).tasks


def jules_cloud_wired() -> bool:
    """Jules Cloud is wired when an API key is present."""
    import os

    return bool(os.environ.get("JULES_API_KEY", "").strip())


def _jules_api_key() -> str | None:
    """Return the configured Jules API key, or None."""
    import os

    value = os.environ.get("JULES_API_KEY", "").strip()
    return value or None


def _jules_cloud_observation(target: Path) -> ProviderObservation:
    """Fetch sanitized Jules Cloud inventory when an API key is present.

    Provider API failures are bounded: the tracker stays available and no
    existing registry entries are erased or admitted. Inventory is a positive
    signal only. A failed fetch returns ``{}`` and a truncated page walk simply
    omits ids, and absence of an id is never read as terminal, so unknown or
    truncated inventory can never release capacity.
    """
    api_key = _jules_api_key()
    if not api_key:
        return ProviderObservation(False, False, "unconfigured", {})
    try:
        from . import jules_cloud

        sessions = jules_cloud.list_sessions(api_key, max_pages=2, max_items=100)
    except Exception as exc:  # noqa: BLE001 - observation must stay bounded
        return _provider_error_observation(exc)
    tasks: dict[str, Any] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = session.get("id")
        if not isinstance(session_id, str):
            continue
        tasks[session_id] = {"state": normalize_provider_state(session.get("state"))}
    return ProviderObservation(True, True, None, tasks)


def observe_jules_cloud_tasks(target: Path) -> dict[str, Any]:
    """Compatibility wrapper returning only Jules Cloud inventory rows."""
    return _jules_cloud_observation(target).tasks


def _grokbot_cloud_observation() -> ProviderObservation:
    from . import fleet_client_grokbot

    try:
        decision = fleet_client_grokbot.whoami()
    except Exception:  # noqa: BLE001 - provider health must stay bounded
        return ProviderObservation(True, False, "transport-failure", {})
    if decision.granted:
        return ProviderObservation(True, True, None, {})
    if decision.reason == "no-hub":
        return ProviderObservation(False, False, "unconfigured", {})
    if decision.reason == "no-identity":
        return ProviderObservation(True, False, "actor-not-enrolled", {})
    if decision.reason == "auth-failed":
        return ProviderObservation(True, False, "auth-failure", {})
    if decision.reason == "hub-unavailable":
        return ProviderObservation(True, False, "transport-failure", {})
    return ProviderObservation(True, True, decision.reason, {})


def observe_provider(provider: str, target: Path) -> ProviderObservation:
    """Observe exactly one provider without exposing credentials or bodies."""
    if provider == "codex-cloud":
        return _codex_cloud_observation(target)
    if provider == "cursor-cloud":
        return _cursor_cloud_observation(target)
    if provider == "jules":
        return _jules_cloud_observation(target)
    if provider == "grokbot-cloud":
        return _grokbot_cloud_observation()
    if provider == "claude-cloud":
        return ProviderObservation(False, False, "disabled-by-policy", {})
    return ProviderObservation(False, False, "unsupported-provider", {})


def observe_provider_details(target: Path, **_kwargs: Any) -> tuple[dict[str, ProviderObservation], dict[str, Any]]:
    """Return per-provider health so failed empty inventories stay visible."""
    observations: dict[str, ProviderObservation] = {}
    for provider in TRACKER_PROVIDERS:
        try:
            observations[provider] = observe_provider(provider, target)
        except Exception:  # noqa: BLE001 - observation must stay bounded
            observations[provider] = ProviderObservation(True, False, "transport-failure", {})
    return observations, observe_github(target)


def _legacy_provider_tasks(observations: dict[str, ProviderObservation]) -> dict[str, Any]:
    tasks: dict[str, Any] = {}
    for provider in ("codex-cloud", "cursor-cloud", "jules"):
        tasks.update(observations.get(provider, ProviderObservation(False, False, None, {})).tasks)
    return tasks


def observe_providers(target: Path, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Compatibility wrapper for callers that only accept a flattened inventory."""
    observations, github = observe_provider_details(target)
    return _legacy_provider_tasks(observations), github, observations["cursor-cloud"].configured


def center_activity_records(
    target: Path,
    *,
    now: datetime | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool | None = None,
) -> list[dict[str, Any]]:
    """Map registry status rows into Center agent_activity records on host=cloud."""
    observed = now or datetime.now(timezone.utc)
    if provider_tasks is None or github is None or cursor_wired is None:
        observed_tasks, observed_github, observed_cursor = observe_providers(target)
        provider_tasks = provider_tasks if provider_tasks is not None else observed_tasks
        github = github if github is not None else observed_github
        cursor_wired = cursor_cloud_wired() if cursor_wired is None else cursor_wired
    payload = status_payload(
        target,
        now=observed,
        provider_tasks=provider_tasks or {},
        github=github or {"branches": [], "prs": []},
        cursor_wired=bool(cursor_wired),
    )
    state_map = {
        "pending": "running",
        "ready-to-land": "ready",
        "stale": "stale",
        "landed": "succeeded",
        "orphaned": "failed",
        "needs-investigation": "unknown",
    }
    records: list[dict[str, Any]] = []
    for row in payload.get("entries", []):
        if not isinstance(row, dict):
            continue
        classification = str(row.get("classification") or "pending")
        started = row.get("dispatched_at")
        records.append(
            {
                "activity_id": f"cloud:{row.get('id')}",
                "parent_activity_id": None,
                "provider": str(row.get("provider") or "cloud").replace("-cloud", ""),
                "harness": str(row.get("provider") or "cloud"),
                "kind": "cloud-task",
                "host": "cloud",
                "label": str(row.get("provider") or "cloud"),
                "task_label": str(row.get("label") or row.get("branch") or "Cloud task"),
                "model": None,
                "state": state_map.get(classification, "unknown"),
                "classification": classification,
                "started_at": started,
                "last_updated_at": payload.get("observed_at"),
                "elapsed_seconds": None,
                "source": {
                    "name": "cloud-tracker",
                    "authority": "local-registry",
                    "observed_at": payload.get("observed_at"),
                },
                "links": {
                    "status": "brigade run cloud status --json",
                    "task_id": row.get("task_id") or row.get("job_id"),
                    "branch": row.get("branch")
                    or (
                        (row.get("artifact_refs") or {}).get("branch")
                        if isinstance(row.get("artifact_refs"), dict)
                        else None
                    ),
                },
            }
        )
    return records


def health(
    target: Path,
    *,
    now: datetime | None = None,
    github: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded local queue health summary for operator consumers."""
    snapshot = github if isinstance(github, dict) else observe_github(target)
    payload = status_payload(target, now=now, provider_tasks={}, github=snapshot)
    entries = [row for row in payload.get("entries", []) if row.get("provider") == "grokbot-cloud"]
    classifications = {name: sum(1 for row in entries if row.get("classification") == name) for name in CLASSIFICATIONS}
    attention = classifications["stale"] + classifications["needs-investigation"] + classifications["orphaned"]
    top = next(
        (row for row in entries if row.get("classification") in {"stale", "needs-investigation", "orphaned"}), None
    )
    return {
        "job_count": len(entries),
        "classification_counts": classifications,
        "issue_count": attention,
        "top_issue": (
            {
                "name": "grokbot_queue_attention",
                "detail": f"{top.get('label') or top.get('job_id')}: {top.get('classification')}",
                "job_id": top.get("job_id"),
            }
            if isinstance(top, dict)
            else None
        ),
    }


def set_stale_ready_hours(target: Path, hours: int) -> dict[str, Any]:
    if hours < 1:
        raise ValueError("stale_ready_hours must be >= 1")
    registry = load_registry(target)
    registry["stale_ready_hours"] = int(hours)
    save_registry(target, registry)
    return registry
