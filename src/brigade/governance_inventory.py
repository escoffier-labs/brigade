"""Privacy-bounded export of Brigade's configured workspace inventory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import dirfd, fleet_model_admission, fleet_model_roster, localio, mcp_cmd, receipt_schema, roster, tools_cmd


INVENTORY_SCHEMA = "brigade.governance_inventory.v1"
AGENT_SCHEMA = "brigade.governance_inventory.agents.v1"
MODEL_SCHEMA = "brigade.governance_inventory.model_providers.v1"
TOOL_SCHEMA = "brigade.governance_inventory.tools.v1"
MCP_SCHEMA = "brigade.governance_inventory.mcp_servers.v1"
OBSERVED_RUNS_SCHEMA = "brigade.governance_inventory.observed_runs.v1"
MANIFEST_SCHEMA = "brigade.governance_inventory.manifest.v1"
SCOPE_NOTICE = "This export covers Brigade configured workspace scope only, not a company-wide AI inventory."
MAX_INPUT_BYTES = 1024 * 1024
MAX_RUN_FILES = 10_000
MAX_WORKER_ROWS = 10_000


class _InputBudget:
    """One bounded byte allowance for every local file read during an export."""

    def __init__(self, maximum: int | None = None) -> None:
        self.remaining = MAX_INPUT_BYTES if maximum is None else maximum

    def consume(self, count: int) -> bool:
        if count < 0 or count > self.remaining:
            return False
        self.remaining -= count
        return True


def canonical_json(value: object) -> str:
    """Return deterministic JSON for comparison, digests, and stdout."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _unknown(reason: str) -> dict[str, str]:
    return {"reason": reason, "value": "unknown"}


def _parse_time(value: str | datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an RFC 3339 timestamp") from exc
    else:
        raise ValueError(f"{field} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_window(since: str | datetime | None, until: str | datetime | None) -> tuple[datetime | None, datetime | None]:
    parsed_since = _parse_time(since, "--since")
    parsed_until = _parse_time(until, "--until")
    if parsed_since is not None and parsed_until is not None and parsed_since > parsed_until:
        raise ValueError("--since must not be after --until")
    return parsed_since, parsed_until


def _read_regular_bytes(path: Path, budget: _InputBudget) -> tuple[bytes | None, str | None]:
    """Read one bounded regular file without reopening a checked pathname."""

    try:
        parent_fd = dirfd.open_directory_nofollow(path.parent)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = dirfd.open_child_file(parent_fd, path.name, flags | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None, "missing"
        except OSError:
            try:
                entry = dirfd.stat_child(parent_fd, path.name)
            except OSError:
                entry = None
            if entry is not None and stat.S_ISLNK(entry.st_mode):
                return None, "symlink"
            return None, "unreadable"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, "not-regular"
            if info.st_size > budget.remaining:
                return None, "oversized"
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, budget.remaining - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > budget.remaining:
                    return None, "oversized"
                chunks.append(chunk)
            if not budget.consume(size):
                return None, "oversized"
            return b"".join(chunks), None
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _read_json_object(path: Path, budget: _InputBudget) -> tuple[dict[str, Any] | None, str | None]:
    raw, reason = _read_regular_bytes(path, budget)
    if raw is None:
        return None, reason
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "not-object")


def _read_mcp_object(path: Path, budget: _InputBudget) -> tuple[dict[str, Any] | None, str | None]:
    """Read canonical MCP JSON while rejecting duplicate object keys."""

    raw, reason = _read_regular_bytes(path, budget)
    if raw is None:
        return None, reason

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-key")
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise ValueError("non-finite")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_non_finite,
        )
    except ValueError as exc:
        if str(exc) == "duplicate-key":
            return None, "duplicate-key"
        if str(exc) == "non-finite":
            return None, "non-finite"
        return None, "malformed"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "not-object")


def _provider_for(cli: str | None, model: str | None) -> tuple[str, dict[str, str] | None]:
    if not model:
        return "unknown", _unknown("model is not configured")
    lowered = (cli or "").lower()
    known = {
        "claude": "anthropic",
        "codex": "openai",
        "cursor": "cursor",
        "gemini": "google",
        "opencode": "opencode",
        "ollama": "ollama",
        "kimi": "moonshot",
    }
    for prefix, provider in known.items():
        if lowered == prefix or lowered.startswith(f"{prefix}-") or lowered.startswith(f"{prefix}:"):
            return fleet_model_roster.canonicalize_provider(provider), None
    return "unknown", _unknown("provider is not configured")


def _agent_registry(target: Path, budget: _InputBudget) -> dict[str, Any]:
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    source = {"kind": "workspace-roster", "state": "available"}
    path = target / ".brigade" / "roster.toml"
    try:
        raw, reason = _read_regular_bytes(path, budget)
        if reason == "missing":
            return {
                "schema": AGENT_SCHEMA,
                "items": [],
                "errors": [],
                "source": {"kind": "workspace-roster", "state": "unavailable"},
                "unknown": {"ownership": _unknown("workspace roster is not configured")},
            }
        if raw is None:
            return {
                "schema": AGENT_SCHEMA,
                "items": [],
                "errors": [f"roster: {reason}"],
                "source": {"kind": "workspace-roster", "state": "unavailable"},
                "unknown": {},
            }
        # ``load_roster`` is the existing validator. Parse the bytes from the
        # held descriptor rather than reopening the checked pathname.
        resolution = roster.RosterResolution(path=path, source="workspace")
        loaded = roster.load_roster(path, resolution=resolution, text=raw.decode("utf-8"))
    except (OSError, ValueError):
        return {
            "schema": AGENT_SCHEMA,
            "items": [],
            "errors": ["roster: unreadable-or-invalid"],
            "source": {"kind": "workspace-roster", "state": "unavailable"},
            "unknown": {},
        }
    for name, agent in sorted(loaded.agents.items()):
        provider, provider_unknown = _provider_for(agent.cli, agent.model)
        unknown = {
            "environment": _unknown("environment is not configured"),
            "lifecycle": _unknown("lifecycle is not configured"),
            "owner": _unknown("owner is not configured"),
            "privilege": _unknown("privilege is not configured"),
        }
        if agent.purpose is None:
            unknown["purpose"] = _unknown("purpose is not configured")
        if provider_unknown is not None:
            unknown["provider"] = provider_unknown
        item: dict[str, Any] = {
            "cli": agent.cli,
            "configured": True,
            "model": agent.model,
            "name": name,
            "provider": provider,
            "role": agent.role,
            "transport": agent.transport,
            "unknown": unknown,
        }
        if agent.purpose is not None:
            item["purpose"] = agent.purpose
        if agent.reasoning is not None:
            item["reasoning"] = agent.reasoning
        items.append(item)
    return {
        "schema": AGENT_SCHEMA,
        "items": items,
        "errors": errors,
        "source": source,
        "unknown": {},
    }


def _fleet_policy(now: datetime) -> dict[str, Any]:
    unavailable = {
        "admissions": [],
        "denials": [],
        "source": {
            "cached_at": None,
            "kind": "node-local-fleet-model-policy-lkg",
            "revision": None,
            "state": "unavailable",
        },
        "unknown": {"fleet_policy": _unknown("no valid node-local Fleet model-policy LKG is available")},
    }
    try:
        record = fleet_model_admission._load_lkg_record()
        cached_at = fleet_model_admission._parse_iso(record.get("cached_at"))
        envelope = record.get("roster")
        if not isinstance(envelope, dict) or cached_at is None:
            return unavailable
        token = fleet_model_admission._node_token() or fleet_model_admission._client.load_fleet_settings()["token"]
        audience = fleet_model_admission._client.resolve_node_id()
        if not token or fleet_model_admission._validate_envelope(envelope, token=token, audience=audience) is not None:
            return unavailable
        expires_at = fleet_model_admission._parse_iso(envelope.get("expires_at"))
        if (
            expires_at is None
            or expires_at <= now
            or cached_at > now + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS)
            or (now - cached_at).total_seconds() > fleet_model_roster.LKG_TTL_SECONDS
        ):
            return unavailable
        revision = envelope.get("revision")
        if type(revision) is not int:
            return unavailable
        admissions: list[dict[str, str]] = []
        denials: list[dict[str, str]] = []
        for row in envelope["seats"]:
            if not isinstance(row, dict):
                return unavailable
            item = {key: row[key] for key in ("seat", "provider", "model", "reasoning")}
            if row["enabled"]:
                admissions.append(item)
            else:
                denials.append({key: item[key] for key in ("seat", "provider", "model")} | {"reason": "seat-disabled"})
        return {
            "admissions": sorted(admissions, key=lambda item: item["seat"]),
            "denials": sorted(denials, key=lambda item: item["seat"]),
            "source": {
                "cached_at": _iso(cached_at),
                "kind": "node-local-fleet-model-policy-lkg",
                "revision": revision,
                "state": "available",
            },
            "unknown": {},
        }
    except (KeyError, OSError, TypeError, ValueError, fleet_model_admission.FleetClientError):
        return unavailable


def _model_registry(agents: dict[str, Any], now: datetime) -> dict[str, Any]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for agent in agents["items"]:
        model = agent.get("model")
        provider = agent.get("provider")
        if not isinstance(model, str) or not isinstance(provider, str):
            continue
        key = (provider, model)
        entry = entries.setdefault(
            key,
            {
                "admission": "configured-local-roster",
                "model": model,
                "provider": provider,
                "seats": [],
                "unknown": {
                    "provider_retention": _unknown("provider retention is not available from local configuration"),
                    "provenance": _unknown("dated provider provenance is not available from local configuration"),
                },
            },
        )
        entry["seats"].append(agent["name"])
    for entry in entries.values():
        entry["seats"].sort()
    retired = [
        {"family": family, "permanent": True, "provider": provider, "reason": fleet_model_roster.PERMANENT_REASON}
        for provider, family in fleet_model_roster.PERMANENT_RETIRED_FAMILIES
    ]
    return {
        "schema": MODEL_SCHEMA,
        "items": sorted(entries.values(), key=lambda item: (item["provider"], item["model"])),
        "retired_families": retired,
        "errors": [],
        "fleet_policy": _fleet_policy(now),
    }


def _tool_registry(target: Path, budget: _InputBudget) -> dict[str, Any]:
    raw, raw_reason = _read_regular_bytes(tools_cmd.paths.config_path(target), budget)
    if raw is None and raw_reason != "missing":
        return {"schema": TOOL_SCHEMA, "items": [], "errors": [f"tool-catalog: {raw_reason}"], "unknown": {}}
    text = raw.decode("utf-8") if raw is not None else None
    entries, reader_errors = tools_cmd.config._load_config(target, text=text)
    if reader_errors:
        missing_only = len(reader_errors) == 1 and reader_errors[0].startswith("tool catalog config missing:")
        return {
            "schema": TOOL_SCHEMA,
            "items": [],
            "errors": [] if missing_only else ["tool-catalog: unreadable-or-invalid"],
            "unknown": {
                "purpose": _unknown("tool catalog is not configured" if missing_only else "tool catalog is invalid")
            },
        }
    items = []
    for item in entries:
        identifier = item.get("id")
        name = item.get("name")
        family = item.get("family")
        if not all(isinstance(value, str) and value for value in (identifier, name, family)):
            continue
        tool: dict[str, Any] = {
            "capabilities": sorted(set(item.get("capability", []))),
            "component_type": "local-tool",
            "effects": sorted(set(item.get("effects", []))),
            "enabled": bool(item.get("enabled", True)),
            "family": family,
            "id": identifier,
            "name": name,
            "permissions": sorted(set(item.get("permissions", []))),
            "unknown": {"lifecycle": _unknown("lifecycle is not configured")},
        }
        description = item.get("description")
        if isinstance(description, str) and description:
            tool["purpose"] = description
        else:
            tool["unknown"]["purpose"] = _unknown("purpose is not configured")
        items.append(tool)
    return {
        "schema": TOOL_SCHEMA,
        "items": sorted(items, key=lambda item: item["id"]),
        "errors": [],
        "unknown": {},
    }


def _safe_endpoint(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment:
        return None
    authority = parsed.hostname
    if ":" in authority:
        authority = f"[{authority}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _mcp_registry(target: Path, budget: _InputBudget) -> dict[str, Any]:
    path = mcp_cmd.canonical_path(target)
    raw, reason = _read_mcp_object(path, budget)
    if raw is None:
        if reason == "missing":
            return {
                "schema": MCP_SCHEMA,
                "items": [],
                "errors": [],
                "unknown": {"ownership": _unknown("MCP catalog is not configured")},
            }
        return {
            "schema": MCP_SCHEMA,
            "items": [],
            "errors": [f"mcp-catalog: {reason}"],
            "unknown": {},
        }
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        return {
            "schema": MCP_SCHEMA,
            "items": [],
            "errors": ["mcp-catalog: missing-servers-object"],
            "unknown": {},
        }
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, raw_server in sorted(servers.items()):
        if not isinstance(name, str) or not name or not isinstance(raw_server, dict):
            errors.append("mcp-catalog: malformed-server")
            continue
        transport = raw_server.get("transport", "stdio")
        if transport not in {"stdio", "http", "sse"}:
            errors.append(f"{name}: unsupported transport")
            continue
        enabled = raw_server.get("enabled", True)
        if type(enabled) is not bool:
            errors.append(f"{name}: malformed")
            continue
        if transport == "stdio":
            if not isinstance(raw_server.get("command"), str) or not raw_server["command"]:
                errors.append(f"{name}: missing command")
                continue
            items.append(
                {
                    "component_type": "local-mcp-server",
                    "enabled": enabled,
                    "name": name,
                    "transport": transport,
                }
            )
            continue
        raw_url = raw_server.get("url")
        endpoint = _safe_endpoint(raw_url if isinstance(raw_url, str) else "")
        if endpoint is None:
            errors.append(f"{name}: unsafe endpoint")
            continue
        items.append(
            {
                "component_type": "remote-mcp-service",
                "enabled": enabled,
                "endpoint": endpoint,
                "name": name,
                "transport": transport,
            }
        )
    return {
        "schema": MCP_SCHEMA,
        "items": sorted(items, key=lambda item: item["name"]),
        "errors": sorted(errors),
        "unknown": {},
    }


def _valid_observed_worker(worker: object) -> bool:
    if not isinstance(worker, dict):
        return False
    seat = worker.get("worker")
    model = worker.get("effective_model") or worker.get("requested_model")
    return isinstance(seat, str) and 0 < len(seat) <= 256 and isinstance(model, str) and 0 < len(model) <= 256


def _observed_runs(
    target: Path,
    since: datetime | None,
    until: datetime | None,
    configured_agents: dict[str, Any],
    budget: _InputBudget,
) -> dict[str, Any]:
    root = target / ".brigade" / "runs"
    try:
        root_info = root.lstat()
    except OSError:
        root_info = None
    if root_info is None or stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return {
            "schema": OBSERVED_RUNS_SCHEMA,
            "items": [],
            "errors": [],
            "unknown": {"provenance": _unknown("no bounded run receipts are configured")},
        }
    observations: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[str] = []
    worker_rows = 0
    try:
        with os.scandir(root) as scanned:
            entries = sorted(islice(scanned, MAX_RUN_FILES + 1), key=lambda entry: entry.name)
    except OSError:
        return {
            "schema": OBSERVED_RUNS_SCHEMA,
            "items": [],
            "errors": ["run-receipts: unreadable"],
            "unknown": {},
        }
    if len(entries) > MAX_RUN_FILES:
        errors.append("run-receipts: limit-exceeded")
        entries = entries[:MAX_RUN_FILES]
    for entry in entries:
        run_dir = Path(entry.path)
        if run_dir.is_symlink() or not run_dir.is_dir():
            errors.append("run-receipts: unsafe-entry")
            continue
        raw, reason = _read_json_object(run_dir / "run.json", budget)
        if raw is None:
            if reason != "missing":
                errors.append(f"run-receipts: {reason}")
            continue
        if (
            raw.get("schema") != receipt_schema.RUN_RECEIPT_SCHEMA
            or raw.get("schema_version") != receipt_schema.RUN_RECEIPT_SCHEMA_VERSION
        ):
            errors.append("run-receipts: run-unverifiable")
            continue
        started = raw.get("started_at")
        try:
            observed_at = _parse_time(started, "run started_at")
        except ValueError:
            errors.append("run-receipts: invalid-timestamp")
            continue
        if observed_at is None:
            continue
        if (since is not None and observed_at < since) or (until is not None and observed_at >= until):
            continue
        worker_payload, worker_reason = _read_json_object(run_dir / "worker-results.json", budget)
        if worker_payload is None:
            errors.append(f"run-receipts: worker-results-{worker_reason}")
            continue
        workers = worker_payload.get("results")
        if (
            worker_payload.get("schema") != receipt_schema.WORKER_RESULTS_SCHEMA
            or worker_payload.get("schema_version") != receipt_schema.WORKER_RESULTS_SCHEMA_VERSION
            or worker_payload.get("producer_run_id") != entry.name
            or not isinstance(workers, list)
        ):
            errors.append("run-receipts: worker-results-unverifiable")
            continue
        if any(not _valid_observed_worker(worker) for worker in workers):
            errors.append("run-receipts: worker-results-malformed-row")
            continue
        for worker in workers:
            if worker_rows >= MAX_WORKER_ROWS:
                errors.append("run-receipts: worker-row-limit-exceeded")
                break
            worker_rows += 1
            if not isinstance(worker, dict):
                errors.append("run-receipts: worker-results-malformed-row")
                continue
            seat = worker.get("worker")
            model = worker.get("effective_model") or worker.get("requested_model")
            if (
                not isinstance(seat, str)
                or not 0 < len(seat) <= 256
                or not isinstance(model, str)
                or not 0 < len(model) <= 256
            ):
                errors.append("run-receipts: worker-results-malformed-row")
                continue
            configured = configured_agents.get(seat)
            if isinstance(configured, dict) and isinstance(configured.get("provider"), str):
                provider = configured["provider"]
                unknown = configured.get("unknown", {}).get("provider") if provider == "unknown" else None
            else:
                provider = "unknown"
                unknown = _unknown("observed seat is not configured in the workspace roster")
            aggregate = observations.setdefault(
                (seat, provider, model),
                {"first_seen": observed_at, "last_seen": observed_at, "run_count": 0, "unknown": unknown},
            )
            aggregate["first_seen"] = min(aggregate["first_seen"], observed_at)
            aggregate["last_seen"] = max(aggregate["last_seen"], observed_at)
            aggregate["run_count"] += 1
    items = [
        {
            "first_seen": _iso(aggregate["first_seen"]),
            "last_seen": _iso(aggregate["last_seen"]),
            "model": model,
            "provider": provider,
            "run_count": aggregate["run_count"],
            "seat": seat,
            "unknown": {"provider_attribution": aggregate["unknown"]} if aggregate["unknown"] is not None else {},
        }
        for (seat, provider, model), aggregate in sorted(observations.items())
    ]
    return {"schema": OBSERVED_RUNS_SCHEMA, "items": items, "errors": sorted(set(errors)), "unknown": {}}


def build_inventory(
    *,
    target: Path,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a point-in-time configured-scope export without contacting providers."""

    target = target.expanduser()
    if not target.is_dir():
        raise ValueError("--target must be an existing directory")
    parsed_since, parsed_until = _time_window(since, until)
    clock = _parse_time(now or datetime.now(timezone.utc), "clock")
    assert clock is not None
    budget = _InputBudget()
    agents = _agent_registry(target, budget)
    configured_agents = {item["name"]: item for item in agents["items"] if isinstance(item.get("name"), str)}
    inventory = {
        "generated_at": _iso(clock),
        "observed_runs": _observed_runs(target, parsed_since, parsed_until, configured_agents, budget),
        "registries": {
            "agents": agents,
            "mcp_servers": _mcp_registry(target, budget),
            "model_providers": _model_registry(agents, clock),
            "tools": _tool_registry(target, budget),
        },
        "schema": INVENTORY_SCHEMA,
        "scope": {"notice": SCOPE_NOTICE, "type": "brigade-configured-workspace"},
        "time_window": {
            "since": _iso(parsed_since) if parsed_since else None,
            "until": _iso(parsed_until) if parsed_until else None,
        },
    }
    return inventory


def cyclonedx_bom(inventory: dict[str, Any]) -> dict[str, Any]:
    """Transform an inventory into an opt-in CycloneDX 1.7 BOM."""

    components: list[dict[str, Any]] = []
    for tool in inventory["registries"]["tools"]["items"]:
        components.append(
            {
                "bom-ref": f"tool:{tool['id']}",
                "name": tool["name"],
                "type": "application",
                "version": "configured",
            }
        )
    for server in inventory["registries"]["mcp_servers"]["items"]:
        if server["component_type"] == "local-mcp-server":
            components.append(
                {
                    "bom-ref": f"mcp:{server['name']}",
                    "name": server["name"],
                    "type": "application",
                    "version": "configured",
                }
            )
    for model in inventory["registries"]["model_providers"]["items"]:
        component = {
            "bom-ref": f"model:{model['provider']}:{model['model']}",
            "name": model["model"],
            "type": "machine-learning-model",
            "version": "configured",
        }
        if model["provider"] != "unknown":
            component["group"] = model["provider"]
        components.append(component)
    components.sort(key=lambda item: item["bom-ref"])
    services = [
        {
            "bom-ref": f"mcp:{server['name']}",
            "endpoints": [server["endpoint"]],
            "name": server["name"],
        }
        for server in inventory["registries"]["mcp_servers"]["items"]
        if server["component_type"] == "remote-mcp-service"
    ]
    services.sort(key=lambda item: item["bom-ref"])
    refs = [item["bom-ref"] for item in components] + [item["bom-ref"] for item in services]
    serial = uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(inventory))
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "components": components,
        "compositions": [{"aggregate": "incomplete", "assemblies": refs}] if refs else [],
        "dependencies": [{"dependsOn": refs, "ref": "brigade:governance-inventory"}],
        "metadata": {
            "component": {
                "bom-ref": "brigade:governance-inventory",
                "name": "Brigade governance inventory",
                "type": "application",
                "version": "configured",
            },
            "properties": [
                {"name": "brigade:time_window:since", "value": inventory["time_window"]["since"] or ""},
                {"name": "brigade:time_window:until", "value": inventory["time_window"]["until"] or ""},
            ],
            "timestamp": inventory["generated_at"],
        },
        "serialNumber": f"urn:uuid:{serial}",
        "services": services,
        "specVersion": "1.7",
        "version": 1,
    }


def _write_fsync(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_directory(temporary: Path, destination: Path) -> None:
    """Publish without a remove gap where the platform supports replacement."""

    if dirfd.available():
        parent_fd = dirfd.open_directory_nofollow(destination.parent)
        try:
            try:
                existing = dirfd.stat_child(parent_fd, destination.name)
            except FileNotFoundError:
                existing = None
            if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode)):
                raise ValueError("--output-dir must be a non-symlink directory")
            if existing is not None:
                destination_fd = dirfd.open_child_directory(parent_fd, destination.name)
                try:
                    if os.listdir(destination_fd):
                        raise ValueError("--output-dir must be empty")
                finally:
                    os.close(destination_fd)
            dirfd.replace_children(parent_fd, temporary.name, destination.name)
            dirfd.fsync_directory(parent_fd)
            return
        finally:
            os.close(parent_fd)
    try:
        os.replace(temporary, destination)
    except FileExistsError:
        if os.name != "nt" or not destination.is_dir():
            raise
        os.rmdir(destination)
        os.replace(temporary, destination)


def write_artifacts(
    *,
    target: Path,
    output_dir: Path,
    now: datetime | None = None,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    cyclonedx: bool = False,
) -> list[str]:
    """Write detached inventory artifacts atomically, refusing non-empty destinations."""

    destination = output_dir.expanduser()
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("--output-dir must be a non-symlink directory")
        if any(destination.iterdir()):
            raise ValueError("--output-dir must be empty")
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("--output-dir parent must be an existing non-symlink directory")
    inventory = build_inventory(target=target, since=since, until=until, now=now)
    payloads = {"inventory.json": _json_bytes(inventory)}
    if cyclonedx:
        payloads["cyclonedx.json"] = _json_bytes(cyclonedx_bom(inventory))
    manifest_items = [
        {"bytes": len(data), "path": name, "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(payloads.items())
    ]
    payloads["manifest.json"] = _json_bytes(
        {"artifacts": manifest_items, "schema": MANIFEST_SCHEMA, "time_window": inventory["time_window"]}
    )
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    try:
        for name, data in sorted(payloads.items()):
            _write_fsync(temporary / name, data)
        dir_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        _publish_directory(temporary, destination)
        localio._fsync_parent_directory(parent)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return sorted(payloads)
