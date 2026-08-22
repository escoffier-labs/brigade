"""Cross-repo campaign lens over per-repo work graphs (#814).

A campaign is a named config listing member targets plus optional cross-repo
``blocks`` edges. Per-repo ledgers stay authoritative; the campaign file is a
query-time lens. Aggregated ready views use repo-qualified task ids
(``{member_id}:{local_task_id}``). Removing the campaign file changes nothing
in member repos.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import edges as edges_mod
from . import helpers
from . import partition as partition_mod

CAMPAIGN_VERSION = 1
QUALIFIER_SEP = ":"
# Issue #814 authorizes optional cross-repo blocks edges only. Local
# parent-child / discovered-from types have no defined cross-repo campaign
# readiness semantics, so campaign configs reject them.
CAMPAIGN_EDGE_TYPES = ("blocks",)
REASON_INVALID_CAMPAIGN = "invalid_campaign"
REASON_MISSING_CAMPAIGN = "missing_campaign"
REASON_MISSING_MEMBER = "missing_member"
REASON_UNREADABLE_MEMBER = "unreadable_member"
REASON_DANGLING_EDGE = "dangling_campaign_edge"


class CampaignError(ValueError):
    """Validation or load failure for a campaign config."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        payload = {"error": str(self), "reason": self.reason, **self.details}
        return payload


def campaigns_dir(workspace: Path) -> Path:
    return workspace / ".brigade" / "campaigns"


def resolve_campaign_path(workspace: Path, campaign: str) -> Path:
    """Resolve a campaign name or path under ``workspace``.

    Bare names map to ``.brigade/campaigns/<name>.json``. Absolute paths, paths
    containing a separator, or values ending in ``.json`` are treated as file
    paths (relative paths resolve against ``workspace``).
    """
    workspace = workspace.expanduser().resolve()
    value = campaign.strip()
    if not value:
        raise CampaignError(
            "campaign name is required",
            reason=REASON_INVALID_CAMPAIGN,
            details={"campaign": campaign},
        )
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    if "/" in value or "\\" in value or value.endswith(".json"):
        return (workspace / raw).resolve()
    return (campaigns_dir(workspace) / f"{value}.json").resolve()


def qualify_task_id(member_id: str, local_id: str) -> str:
    return f"{member_id}{QUALIFIER_SEP}{local_id}"


def split_qualified_id(qualified: str) -> tuple[str, str] | None:
    if QUALIFIER_SEP not in qualified:
        return None
    member_id, local_id = qualified.split(QUALIFIER_SEP, 1)
    if not member_id.strip() or not local_id.strip():
        return None
    return member_id.strip(), local_id.strip()


def _string_field(value: object, *, field: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(
            f"{label}: {field} must be a non-empty string",
            reason=REASON_INVALID_CAMPAIGN,
            details={"field": field},
        )
    return value.strip()


def _normalize_member(raw: object, *, index: int) -> dict[str, str]:
    label = f"members[{index}]"
    if not isinstance(raw, dict):
        raise CampaignError(
            f"{label}: expected object",
            reason=REASON_INVALID_CAMPAIGN,
            details={"index": index},
        )
    member_id = _string_field(raw.get("id"), field="id", label=label)
    if QUALIFIER_SEP in member_id:
        raise CampaignError(
            f"{label}: id cannot contain {QUALIFIER_SEP!r}",
            reason=REASON_INVALID_CAMPAIGN,
            details={"id": member_id},
        )
    path = _string_field(raw.get("path"), field="path", label=label)
    member: dict[str, str] = {"id": member_id, "path": path}
    label_value = raw.get("label")
    if isinstance(label_value, str) and label_value.strip():
        member["label"] = label_value.strip()
    return member


def _normalize_campaign_edge(raw: object, *, index: int, member_ids: set[str]) -> dict[str, str]:
    label = f"edges[{index}]"
    if not isinstance(raw, dict):
        raise CampaignError(
            f"{label}: expected object",
            reason=REASON_INVALID_CAMPAIGN,
            details={"index": index},
        )
    edge_type = raw.get("type", "blocks")
    if not isinstance(edge_type, str) or edge_type.strip() not in CAMPAIGN_EDGE_TYPES:
        raise CampaignError(
            f"{label}: type must be one of: {', '.join(CAMPAIGN_EDGE_TYPES)} "
            "(cross-repo campaign edges are blocks-only)",
            reason=REASON_INVALID_CAMPAIGN,
            details={"type": edge_type, "allowed": list(CAMPAIGN_EDGE_TYPES)},
        )
    source = _string_field(raw.get("source"), field="source", label=label)
    target = _string_field(raw.get("target"), field="target", label=label)
    for endpoint_name, endpoint in (("source", source), ("target", target)):
        parts = split_qualified_id(endpoint)
        if parts is None:
            raise CampaignError(
                f"{label}: {endpoint_name} must be repo-qualified as member_id:task_id",
                reason=REASON_INVALID_CAMPAIGN,
                details={endpoint_name: endpoint},
            )
        member_id, _local = parts
        if member_id not in member_ids:
            raise CampaignError(
                f"{label}: {endpoint_name} references unknown member {member_id!r}",
                reason=REASON_INVALID_CAMPAIGN,
                details={endpoint_name: endpoint, "member_id": member_id},
            )
    return {"source": source, "target": target, "type": edge_type.strip()}


def normalize_campaign(raw: object, *, name_hint: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignError(
            "campaign config must be a JSON object",
            reason=REASON_INVALID_CAMPAIGN,
        )
    version = raw.get("version", CAMPAIGN_VERSION)
    if version != CAMPAIGN_VERSION:
        raise CampaignError(
            f"unsupported campaign version: {version!r} (expected {CAMPAIGN_VERSION})",
            reason=REASON_INVALID_CAMPAIGN,
            details={"version": version},
        )
    name = raw.get("name")
    if name is None and name_hint:
        resolved_name = name_hint
    else:
        resolved_name = _string_field(name if name is not None else name_hint, field="name", label="campaign")

    raw_members = raw.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise CampaignError(
            "campaign members must be a non-empty list",
            reason=REASON_INVALID_CAMPAIGN,
        )
    members: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_members):
        member = _normalize_member(item, index=index)
        if member["id"] in seen_ids:
            raise CampaignError(
                f"duplicate campaign member id: {member['id']}",
                reason=REASON_INVALID_CAMPAIGN,
                details={"id": member["id"]},
            )
        seen_ids.add(member["id"])
        members.append(member)

    raw_edges = raw.get("edges") or []
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise CampaignError(
            "campaign edges must be a list when present",
            reason=REASON_INVALID_CAMPAIGN,
        )
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw_edges):
        edge = _normalize_campaign_edge(item, index=index, member_ids=seen_ids)
        key = (edge["source"], edge["target"], edge["type"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(edge)

    return {
        "version": CAMPAIGN_VERSION,
        "name": resolved_name,
        "members": members,
        "edges": edges,
    }


def load_campaign(workspace: Path, campaign: str) -> tuple[dict[str, Any], Path]:
    """Load and normalize a campaign config. Returns ``(config, path)``."""
    path = resolve_campaign_path(workspace, campaign)
    if not path.is_file():
        raise CampaignError(
            f"campaign not found: {path}",
            reason=REASON_MISSING_CAMPAIGN,
            details={"campaign_path": str(path)},
            exit_code=1,
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(
            f"could not read campaign: {exc}",
            reason=REASON_INVALID_CAMPAIGN,
            details={"campaign_path": str(path)},
        ) from exc
    name_hint = path.stem if path.suffix == ".json" else path.name
    config = normalize_campaign(raw, name_hint=name_hint)
    return config, path


def _member_target(workspace: Path, member: dict[str, str]) -> Path:
    path = Path(member["path"]).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _read_member_ledger(member_target: Path) -> dict[str, Any]:
    """Read a member ledger, distinguishing absent from unreadable.

    Missing ``tasks.json`` is a legitimate empty member ledger. Corrupt or
    unreadable content fails closed so campaign ready cannot omit that member
    while still claiming an authoritative aggregate.
    """
    path = helpers._tasks_path(member_target)
    if not path.exists():
        return {"version": edges_mod.TASK_LEDGER_VERSION, "tasks": [], "edges": []}
    try:
        raw_text = path.read_text()
        payload = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(
            f"campaign member ledger unreadable: {path}: {exc}",
            reason=REASON_UNREADABLE_MEMBER,
            details={"tasks_path": str(path), "target": str(member_target)},
            exit_code=1,
        ) from exc
    if not isinstance(payload, dict):
        raise CampaignError(
            f"campaign member ledger must be a JSON object: {path}",
            reason=REASON_UNREADABLE_MEMBER,
            details={"tasks_path": str(path), "target": str(member_target)},
            exit_code=1,
        )
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        payload["tasks"] = []
    edges_mod.ensure_ledger_edges(payload)
    return payload


def _qualify_task(task: dict[str, Any], *, member_id: str, member_target: Path) -> dict[str, Any]:
    local_id = task.get("id")
    if not isinstance(local_id, str) or not local_id.strip():
        return dict(task)
    qualified = dict(task)
    qualified["id"] = qualify_task_id(member_id, local_id.strip())
    qualified["repo"] = member_id
    qualified["local_id"] = local_id.strip()
    qualified["target"] = str(member_target)
    return qualified


def _qualify_edge(edge: dict[str, Any], *, member_id: str) -> dict[str, Any]:
    return {
        "source": qualify_task_id(member_id, str(edge["source"])),
        "target": qualify_task_id(member_id, str(edge["target"])),
        "type": edge["type"],
        **({"id": edge["id"]} if isinstance(edge.get("id"), str) else {}),
        **({"created_at": edge["created_at"]} if isinstance(edge.get("created_at"), str) else {}),
        "repo": member_id,
    }


def _annotate_ready_item(item: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(item)
    parts = split_qualified_id(str(item.get("id") or ""))
    if parts is not None:
        annotated["repo"] = parts[0]
        annotated["local_id"] = parts[1]
    return annotated


def build_synthetic_ledger(
    workspace: Path,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a qualified synthetic ledger from member repos + campaign edges.

    Returns ``(ledger, members_info, errors)``. Missing or unreadable members are
    reported in ``errors`` and skipped from the synthetic ledger; callers must
    fail closed when ``errors`` is non-empty before treating the aggregate as
    authoritative readiness.
    """
    workspace = workspace.expanduser().resolve()
    tasks: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    members_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for member in config["members"]:
        member_id = member["id"]
        member_target = _member_target(workspace, member)
        info: dict[str, Any] = {
            "id": member_id,
            "path": member["path"],
            "target": str(member_target),
            "ok": False,
        }
        if isinstance(member.get("label"), str):
            info["label"] = member["label"]
        if not member_target.is_dir():
            error = {
                "reason": REASON_MISSING_MEMBER,
                "member_id": member_id,
                "path": member["path"],
                "target": str(member_target),
                "error": f"campaign member path is not a directory: {member_target}",
            }
            errors.append(error)
            info["error"] = error["error"]
            members_info.append(info)
            continue
        try:
            ledger = _read_member_ledger(member_target)
        except CampaignError as exc:
            error = {
                "reason": exc.reason,
                "member_id": member_id,
                "path": member["path"],
                "target": str(member_target),
                "error": str(exc),
                **exc.details,
            }
            errors.append(error)
            info["error"] = error["error"]
            members_info.append(info)
            continue
        local_edges = edges_mod.ensure_ledger_edges(ledger)
        member_tasks = [task for task in ledger.get("tasks") or [] if isinstance(task, dict)]
        for task in member_tasks:
            tasks.append(_qualify_task(task, member_id=member_id, member_target=member_target))
        for edge in local_edges:
            edges.append(_qualify_edge(edge, member_id=member_id))
        info["ok"] = True
        info["task_count"] = len(member_tasks)
        info["edge_count"] = len(local_edges)
        info["tasks_path"] = str(helpers._tasks_path(member_target))
        members_info.append(info)

    for edge in config.get("edges") or []:
        edges.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "type": edge["type"],
                "campaign": True,
            }
        )

    synthetic = {
        "version": edges_mod.TASK_LEDGER_VERSION,
        "tasks": tasks,
        "edges": edges_mod.normalize_edges(edges),
    }
    return synthetic, members_info, errors


def _validate_campaign_edge_endpoints(
    config: dict[str, Any],
    synthetic: dict[str, Any],
) -> None:
    """Fail closed when campaign edges name tasks absent from loaded members.

    Per-repo orphan blockers do not gate readiness; campaign edges must not
    inherit that silence across repos.
    """
    task_ids = {
        str(task["id"])
        for task in synthetic.get("tasks") or []
        if isinstance(task, dict) and isinstance(task.get("id"), str) and task["id"].strip()
    }
    dangling: list[dict[str, str]] = []
    for index, edge in enumerate(config.get("edges") or []):
        for endpoint_name in ("source", "target"):
            endpoint = str(edge.get(endpoint_name) or "")
            if endpoint not in task_ids:
                dangling.append(
                    {
                        "index": str(index),
                        "endpoint": endpoint_name,
                        "id": endpoint,
                        "type": str(edge.get("type") or "blocks"),
                    }
                )
    if dangling:
        raise CampaignError(
            "campaign edge endpoints must name existing member tasks",
            reason=REASON_DANGLING_EDGE,
            details={"dangling_endpoints": dangling},
            exit_code=1,
        )


def campaign_readiness_payload(
    workspace: Path,
    campaign: str,
    *,
    explain: bool = False,
    parallel_safe: bool = False,
    impact_runner: Any | None = None,
) -> dict[str, Any]:
    """Aggregated ready-view JSON for a campaign (shaped like per-repo ready)."""
    workspace = workspace.expanduser().resolve()
    config, campaign_path = load_campaign(workspace, campaign)
    synthetic, members_info, errors = build_synthetic_ledger(workspace, config)
    if errors:
        # Partial aggregates that omit a configured member are not authoritative.
        raise CampaignError(
            "campaign members missing or unreadable",
            reason=REASON_MISSING_MEMBER
            if any(err.get("reason") == REASON_MISSING_MEMBER for err in errors)
            else REASON_UNREADABLE_MEMBER,
            details={"members": members_info, "member_errors": errors, "errors": errors},
            exit_code=1,
        )
    _validate_campaign_edge_endpoints(config, synthetic)

    resolution = edges_mod.resolve_readiness(synthetic)
    payload = resolution.as_dict(explain=explain)
    payload["ready"] = [_annotate_ready_item(item) for item in payload["ready"]]
    if explain and isinstance(payload.get("blocked"), list):
        payload["blocked"] = [_annotate_ready_item(item) for item in payload["blocked"]]

    pending_count = sum(
        1
        for task in synthetic.get("tasks") or []
        if isinstance(task, dict)
        and task.get("status", "pending") == "pending"
        and isinstance(task.get("text"), str)
        and task["text"].strip()
    )
    payload.update(
        {
            "target": str(workspace),
            "campaign": config["name"],
            "campaign_path": str(campaign_path),
            "campaign_version": config["version"],
            "members": members_info,
            "member_count": len(members_info),
            "campaign_edges": list(config.get("edges") or []),
            "edges": synthetic["edges"],
            "pending_count": pending_count,
            "member_errors": errors,
            "member_error_count": len(errors),
        }
    )
    if parallel_safe:
        tasks_by_id = {
            str(task["id"]): task
            for task in synthetic.get("tasks") or []
            if isinstance(task, dict) and isinstance(task.get("id"), str)
        }
        partition_payload = partition_mod.partition_campaign_ready(
            payload["ready"],
            members=members_info,
            tasks_by_id=tasks_by_id,
            impact_runner=impact_runner,
        )
        payload["parallel_safe"] = True
        payload.update(partition_payload)
    return payload
