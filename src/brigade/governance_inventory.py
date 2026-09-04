"""Privacy-bounded export of Brigade's configured workspace inventory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import fleet_model_roster, mcp_cmd, roster, tools_cmd


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


def _safe_regular(path: Path) -> tuple[bool, str | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, "missing"
    except OSError:
        return False, "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return False, "symlink"
    if not stat.S_ISREG(info.st_mode):
        return False, "not-regular"
    if info.st_size > MAX_INPUT_BYTES:
        return False, "oversized"
    return True, None


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    safe, reason = _safe_regular(path)
    if not safe:
        return None, reason
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "not-object")


def _read_mcp_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read canonical MCP JSON while rejecting duplicate object keys."""

    safe, reason = _safe_regular(path)
    if not safe:
        return None, reason

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except ValueError as exc:
        return None, "duplicate-key" if str(exc) == "duplicate-key" else "malformed"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    model_prefixes = (
        ("gpt-", "openai"),
        ("o1", "openai"),
        ("claude-", "anthropic"),
        ("gemini-", "google"),
    )
    for prefix, provider in model_prefixes:
        if model.lower().startswith(prefix):
            return provider, None
    return "unknown", _unknown("provider is not configured")


def _agent_registry(target: Path) -> dict[str, Any]:
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    try:
        resolution = roster.resolve_roster(target)
        safe, reason = _safe_regular(resolution.path)
        if not safe:
            return {
                "schema": AGENT_SCHEMA,
                "items": [],
                "errors": [f"roster: {reason}"],
                "unknown": {},
            }
        loaded = roster.load_roster(resolution.path, resolution=resolution)
    except FileNotFoundError:
        return {
            "schema": AGENT_SCHEMA,
            "items": [],
            "errors": [],
            "unknown": {"ownership": _unknown("roster is not configured")},
        }
    except (OSError, ValueError):
        return {
            "schema": AGENT_SCHEMA,
            "items": [],
            "errors": ["roster: unreadable-or-invalid"],
            "unknown": {},
        }
    for name, agent in sorted(loaded.agents.items()):
        provider, provider_unknown = _provider_for(agent.cli, agent.model)
        unknown = {
            "lifecycle": _unknown("lifecycle is not configured"),
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
        "source": resolution.source,
        "unknown": {},
    }


def _model_registry(agents: dict[str, Any]) -> dict[str, Any]:
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
    }


def _tool_registry(target: Path) -> dict[str, Any]:
    safe, reason = _safe_regular(tools_cmd.paths.config_path(target))
    if not safe and reason != "missing":
        return {
            "schema": TOOL_SCHEMA,
            "items": [],
            "errors": [f"tool-catalog: {reason}"],
            "unknown": {},
        }
    entries, reader_errors = tools_cmd.config._load_config(target)
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
    if parsed.query or parsed.fragment or ".." in parsed.path.split("/"):
        return None
    authority = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path or "/"
    if len(path) > 512:
        return None
    return f"{parsed.scheme}://{authority}{path}"


def _mcp_registry(target: Path) -> dict[str, Any]:
    path = mcp_cmd.canonical_path(target)
    raw, reason = _read_mcp_object(path)
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
    parsed, load_errors, _warnings = mcp_cmd.load_canonical(target)
    if load_errors:
        return {
            "schema": MCP_SCHEMA,
            "items": [],
            "errors": ["mcp-catalog: unreadable-or-invalid"],
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
        server = parsed.get(name)
        if server is None:
            errors.append(f"{name}: malformed")
            continue
        if transport == "stdio":
            if not server.command:
                errors.append(f"{name}: missing command")
                continue
            items.append(
                {
                    "component_type": "local-mcp-server",
                    "enabled": server.enabled,
                    "name": name,
                    "transport": transport,
                }
            )
            continue
        endpoint = _safe_endpoint(server.url or "")
        if endpoint is None:
            errors.append(f"{name}: unsafe endpoint")
            continue
        items.append(
            {
                "component_type": "remote-mcp-service",
                "enabled": server.enabled,
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


def _observed_runs(target: Path, since: datetime | None, until: datetime | None) -> dict[str, Any]:
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
    observations: dict[tuple[str, str, str], list[datetime]] = defaultdict(list)
    errors: list[str] = []
    for index, run_dir in enumerate(sorted(root.iterdir(), key=lambda path: path.name)):
        if index >= MAX_RUN_FILES:
            errors.append("run-receipts: limit-exceeded")
            break
        if run_dir.is_symlink() or not run_dir.is_dir():
            errors.append("run-receipts: unsafe-entry")
            continue
        raw, reason = _read_json_object(run_dir / "run.json")
        if raw is None:
            if reason != "missing":
                errors.append(f"run-receipts: {reason}")
            continue
        started = raw.get("started_at")
        try:
            observed_at = _parse_time(started, "run started_at")
        except ValueError:
            errors.append("run-receipts: invalid-timestamp")
            continue
        if observed_at is None:
            continue
        if (since is not None and observed_at < since) or (until is not None and observed_at > until):
            continue
        workers = raw.get("workers")
        if not isinstance(workers, list):
            continue
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            seat = worker.get("worker")
            model = worker.get("effective_model") or worker.get("requested_model")
            if not isinstance(seat, str) or not seat or not isinstance(model, str) or not model:
                continue
            provider, _unknown_provider = _provider_for(None, model)
            observations[(seat, provider, model)].append(observed_at)
    items = [
        {
            "first_seen": _iso(min(times)),
            "last_seen": _iso(max(times)),
            "model": model,
            "provider": provider,
            "run_count": len(times),
            "seat": seat,
        }
        for (seat, provider, model), times in sorted(observations.items())
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
    agents = _agent_registry(target)
    inventory = {
        "generated_at": _iso(clock),
        "observed_runs": _observed_runs(target, parsed_since, parsed_until),
        "registries": {
            "agents": agents,
            "mcp_servers": _mcp_registry(target),
            "model_providers": _model_registry(agents),
            "tools": _tool_registry(target),
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
        if destination.exists():
            os.rmdir(destination)
        os.replace(temporary, destination)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return sorted(payloads)
