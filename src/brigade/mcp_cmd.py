"""`brigade mcp` - sync one canonical MCP server catalog into each tool's native config.

Canonical source: ``.brigade/mcp.json`` (tracked/shared). Ownership of which server
keys Brigade manages in each provider file is tracked in the gitignored sidecar
``.brigade/mcp/state.json`` - JSON config files cannot carry a managed-header comment
and are co-owned with the user, so Brigade merges by server key and never owns the whole
file. Foreign servers are preserved; a server the user edited becomes a conflict (skipped
unless ``--force``); orphans are removed only with ``--prune`` and only when still pristine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import localio, mcp_adapters, mcp_runtime, projection
from .mcp_adapters import ADAPTERS, MCP_TARGETS, CanonicalServer
from .render import emit as _emit

CANONICAL_REL = ".brigade/mcp.json"
STATE_REL = ".brigade/mcp/state.json"

GITIGNORE_SNIPPET = [
    "# brigade mcp: canonical source is shared, sidecar state is machine-local",
    "!.brigade/",
    ".brigade/*",
    "!.brigade/mcp.json",
    ".brigade/mcp/",
]


# --------------------------------------------------------------------------- #
# Paths + canonical load
# --------------------------------------------------------------------------- #


def canonical_path(target: Path) -> Path:
    return target / CANONICAL_REL


def state_path(target: Path) -> Path:
    return target / STATE_REL


def load_canonical(target: Path) -> tuple[dict[str, CanonicalServer], list[str], list[str]]:
    """Return (servers, errors, warnings). errors is non-empty when the file is unusable."""
    path = canonical_path(target)
    doc = localio.read_json_dict(path)
    if doc is None:
        if path.exists():
            return {}, [f"{path}: not a valid JSON object"], []
        return {}, [f"{path}: not found (run `brigade mcp init`)"], []
    raw_servers = doc.get("servers")
    if not isinstance(raw_servers, dict):
        return {}, [f"{path}: missing a 'servers' object"], []
    servers: dict[str, CanonicalServer] = {}
    warnings: list[str] = []
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            warnings.append(f"{name}: not an object, skipped")
            continue
        server, warns = mcp_adapters.server_from_dict(str(name), raw)
        servers[str(name)] = server
        warnings.extend(warns)
    return servers, [], warnings


def _write_canonical(target: Path, servers: dict[str, CanonicalServer]) -> None:
    payload = {"version": 1, "servers": {name: mcp_adapters.server_to_dict(s) for name, s in sorted(servers.items())}}
    localio.write_json(canonical_path(target), payload)


def _load_state(target: Path) -> dict[str, Any]:
    doc = localio.read_json_dict(state_path(target))
    if doc is None or not isinstance(doc.get("ownership"), dict):
        return {"version": 1, "ownership": {}}
    return doc


def _save_state(target: Path, state: dict[str, Any]) -> None:
    localio.write_json(state_path(target), state)


# --------------------------------------------------------------------------- #
# Active target resolution
# --------------------------------------------------------------------------- #


def _configured_harnesses(target: Path) -> set[str] | None:
    from .config import load_config

    try:
        cfg = load_config(target)
    except (ValueError, json.JSONDecodeError):
        return None
    if cfg is None:
        return None
    return set(cfg.selection.harnesses)


def _scoped_harness(harness: str, *, user_scope: bool) -> str:
    if harness == "cursor" and user_scope:
        return "cursor-user"
    return harness


def active_targets(target: Path, *, harness: str | None, user_scope: bool) -> tuple[list[str], list[str]]:
    """Resolve which adapters this run touches. Returns (harnesses, notes)."""
    notes: list[str] = []
    if harness is not None:
        harness = _scoped_harness(harness, user_scope=user_scope)
        if harness not in ADAPTERS:
            return [], [f"unknown MCP target {harness!r} (known: {', '.join(MCP_TARGETS)})"]
        adapter = ADAPTERS[harness]
        if adapter.user_scope and not user_scope:
            notes.append(f"{harness} is user-scoped; pass --user-scope to write {adapter.path}")
            return [], notes
        return [harness], notes
    configured = _configured_harnesses(target)
    result: list[str] = []
    for name in MCP_TARGETS:
        adapter = ADAPTERS[name]
        if adapter.user_scope and not user_scope:
            continue
        if name == "cursor-user":
            # Cursor's global config is selected only by the explicit
            # --harness cursor --user-scope combination.
            continue
        if name == "vscode":
            # vscode is not a Brigade harness. Only include it when the repo
            # already carries a .vscode/ directory, so a sync never grows one
            # unasked; --harness vscode still forces it.
            if not (target / ".vscode").is_dir():
                notes.append("vscode: skipped (no .vscode/ directory; run --harness vscode to include)")
                continue
        elif configured is not None and not adapter.user_scope and name not in configured:
            continue
        result.append(name)
    return result, notes


# --------------------------------------------------------------------------- #
# Status / plan engine
# --------------------------------------------------------------------------- #


def _server_targets_harness(server: CanonicalServer, harness: str) -> bool:
    if server.targets is None:
        return True
    if harness == "cursor-user":
        return "cursor" in server.targets or "cursor-user" in server.targets
    if harness == "grok-user":
        return "grok" in server.targets or "grok-user" in server.targets
    if harness == "kimi-user":
        return "kimi" in server.targets or "kimi-user" in server.targets
    if harness == "opencode-user":
        return "opencode" in server.targets or "opencode-user" in server.targets
    return harness in server.targets


def _repo_local_path(target: Path, value: str) -> bool:
    path = Path(value).expanduser()
    if not path.is_absolute():
        return False
    resolved = path.resolve()
    try:
        resolved.relative_to(target.resolve())
    except ValueError:
        return False
    return True


def _without_repo_graphtrail_db_pin(target: Path, args: tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--db" and index + 1 < len(args) and _repo_local_path(target, args[index + 1]):
            index += 2
            continue
        if arg.startswith("--db=") and _repo_local_path(target, arg.split("=", 1)[1]):
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    return tuple(cleaned)


def _project_server(target: Path, harness: str, server: CanonicalServer) -> dict[str, Any]:
    adapter = ADAPTERS[harness]
    projected = adapter.to_provider(server)
    command_name = Path(server.command or "").name.lower()
    if (
        harness != "cursor-user"
        or server.is_remote
        or (server.name.lower() != "graphtrail" and "graphtrail" not in command_name)
    ):
        return projected
    args = _without_repo_graphtrail_db_pin(target, server.args)
    if args == server.args:
        return projected
    if args:
        projected["args"] = list(args)
    else:
        projected.pop("args", None)
    return projected


def _plan_for_harness(
    target: Path,
    harness: str,
    servers: dict[str, CanonicalServer],
    state: dict[str, Any],
    *,
    force: bool,
    prune: bool,
    adopt: bool,
    name_filter: str | None = None,
) -> list[dict[str, Any]]:
    adapter = ADAPTERS[harness]
    path = mcp_adapters.resolve_path(adapter, target)
    text = path.read_text() if path.is_file() else None
    live = adapter.read_file(text)
    owned = state.get("ownership", {}).get(harness, {}).get(adapter.path, {})
    # `desired` is the FULL set targeting this harness; orphans compare against it so a
    # --name-scoped run never treats the other managed servers as prunable orphans.
    desired = {
        name: server for name, server in servers.items() if server.enabled and _server_targets_harness(server, harness)
    }
    items: list[dict[str, Any]] = []
    rel = adapter.path

    for name, server in sorted(desired.items()):
        if name_filter is not None and name != name_filter:
            continue
        provider_dict = _project_server(target, harness, server)
        desired_fp = localio.stable_hash(provider_dict)
        canon_fp = localio.stable_hash(mcp_adapters.server_to_dict(server))
        record = owned.get(name)
        if name not in live:
            items.append(_item(harness, rel, name, "missing", "create", canon_fp, desired_fp))
            continue
        live_fp = localio.stable_hash(live[name])
        if record is None:
            if live_fp == desired_fp:
                items.append(_item(harness, rel, name, "current", "skip", canon_fp, desired_fp, reconciled=True))
            elif adopt:
                items.append(_item(harness, rel, name, "adopted", "update", canon_fp, desired_fp))
            else:
                items.append(
                    _item(
                        harness,
                        rel,
                        name,
                        "foreign",
                        "conflict",
                        canon_fp,
                        desired_fp,
                        detail="a server with this name already exists; --adopt to take ownership",
                    )
                )
            continue
        if live_fp != record.get("projected_fingerprint"):
            if force:
                items.append(_item(harness, rel, name, "conflicted", "update", canon_fp, desired_fp))
            else:
                items.append(
                    _item(
                        harness,
                        rel,
                        name,
                        "conflicted",
                        "conflict",
                        canon_fp,
                        desired_fp,
                        detail="server was edited outside Brigade; leaving the live config unchanged",
                    )
                )
        elif live_fp != desired_fp or record.get("canonical_fingerprint") != canon_fp:
            items.append(_item(harness, rel, name, "stale", "update", canon_fp, desired_fp))
        else:
            items.append(_item(harness, rel, name, "current", "skip", canon_fp, desired_fp))

    # Orphans: owned-or-live servers no longer in the desired canonical set.
    for name in sorted(set(owned) | set(live)):
        if name in desired:
            continue
        if name_filter is not None and name != name_filter:
            continue
        record = owned.get(name)
        if record is None:
            continue  # purely foreign, not ours - never touch
        if name not in live:
            continue  # already gone; ownership cleaned on write
        live_fp = localio.stable_hash(live[name])
        if live_fp != record.get("projected_fingerprint"):
            items.append(
                _item(
                    harness,
                    rel,
                    name,
                    "orphan-edited",
                    "skip",
                    None,
                    None,
                    detail="removed from canonical but edited in place; left untouched",
                )
            )
        elif prune:
            items.append(_item(harness, rel, name, "orphan", "remove", None, None))
        else:
            items.append(
                _item(
                    harness, rel, name, "orphan", "skip", None, None, detail="removed from canonical; --prune to delete"
                )
            )
    adapter_scope = "user" if adapter.user_scope else "project"
    for item in items:
        item["scope"] = adapter_scope
        server = servers.get(item["server"])
        item["transport"] = server.transport if server is not None else None
    return items


def _item(harness, file, server, status, action, canon_fp, proj_fp, *, detail="", reconciled=False) -> dict[str, Any]:
    return {
        "harness": harness,
        "file": file,
        "server": server,
        "status": status,
        "action": action,
        "detail": detail,
        "_canon_fp": canon_fp,
        "_proj_fp": proj_fp,
        "_reconciled": reconciled,
    }


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "skip": 0, "conflict": 0, "remove": 0}
    for item in items:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return counts


def _public_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


def _user_stdio_exposures(planned: list[tuple[str, list[dict[str, Any]]]], target: Path) -> list[dict[str, Any]]:
    """Per user-scoped destination: stdio servers this sync writes and the total after.

    Every stdio server in a user-scoped client config is one child process per
    active client session, so writing them user-wide multiplies processes by
    session count. The total counts the destination file's preserved stdio
    servers too (foreign and unmanaged definitions the merge keeps), not just
    planner items, so the acknowledged exposure matches the real post-sync
    config. Project-scoped destinations never gate.
    """
    exposures = []
    for harness, items in planned:
        adapter = ADAPTERS[harness]
        if not adapter.user_scope:
            continue
        writes = [i for i in items if i["action"] in ("create", "update") and i.get("transport") == "stdio"]
        if not writes:
            continue
        removed = {i["server"] for i in items if i["action"] == "remove"}
        stdio_after = {i["server"] for i in items if i.get("transport") == "stdio" and i["action"] != "remove"}
        path = mcp_adapters.resolve_path(adapter, target)
        try:
            live = adapter.read_file(path.read_text() if path.is_file() else None)
        except (OSError, ValueError):
            live = {}
        for name, provider in live.items():
            if name in removed or name in stdio_after:
                continue
            try:
                server, _ = adapter.from_provider(name, provider)
            except (TypeError, ValueError, KeyError):
                continue
            if server.transport == "stdio":
                stdio_after.add(name)
        exposures.append(
            {
                "harness": harness,
                "destination": items[0]["file"] if items else adapter.path,
                "stdio_writes": len(writes),
                "stdio_total": len(stdio_after),
            }
        )
    return exposures


def _stdio_exposure_lines(exposures: list[dict[str, Any]]) -> list[str]:
    lines = []
    for e in exposures:
        lines.append(
            f"user-scope stdio warning: {e['destination']} gains {e['stdio_writes']} stdio MCP server(s) "
            f"({e['stdio_total']} stdio total after sync)"
        )
        lines.append(
            f"  each active {e['harness']} client session starts one child process per configured stdio "
            f"server: {e['stdio_total']} stdio servers x active sessions"
        )
    lines.append(
        "  prefer project-scoped config or a shared HTTP/SSE transport where the client supports it; "
        "user-wide stdio writes need explicit operator acknowledgement after review"
    )
    return lines


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _ensure_gitignore(target: Path) -> bool:
    gi = target / ".gitignore"
    existing = gi.read_text() if gi.is_file() else ""
    if _gitignore_keeps_mcp_catalog_trackable(existing) and _git_keeps_mcp_catalog_trackable(target) is not False:
        return False
    block = "\n".join(GITIGNORE_SNIPPET)
    sep = "" if existing == "" or existing.endswith("\n") else "\n"
    gi.write_text(existing + sep + ("\n" if existing else "") + block + "\n")
    return True


def _gitignore_keeps_mcp_catalog_trackable(existing: str) -> bool:
    lines = existing.splitlines()
    try:
        unignore_parent = lines.index("!.brigade/")
        ignore_children = lines.index(".brigade/*", unignore_parent + 1)
        unignore_catalog = lines.index("!.brigade/mcp.json", ignore_children + 1)
    except ValueError:
        return False
    return ".brigade/mcp/" in lines[unignore_catalog + 1 :]


def _git_keeps_mcp_catalog_trackable(target: Path) -> bool | None:
    try:
        repo = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if repo.returncode != 0 or repo.stdout.strip() != "true":
        return None

    try:
        ignored = subprocess.run(
            ["git", "-C", str(target), "check-ignore", "-q", CANONICAL_REL],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if ignored.returncode == 1:
        return True
    if ignored.returncode == 0:
        return False
    return None


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = canonical_path(target)
    created = not path.exists()
    if path.exists() and not force:
        return _emit(
            {
                "canonical_path": str(path),
                "created": False,
                "error": "already exists; leaving it unchanged",
                "reason": "already_exists",
            },
            json_output,
            [f"error: {path} already exists; leaving it unchanged"],
            3,
        )
    localio.write_json(path, {"version": 1, "servers": {}})
    state_path(target).parent.mkdir(parents=True, exist_ok=True)
    gi_updated = _ensure_gitignore(target) if update_gitignore else False
    payload = {"canonical_path": str(path), "created": created, "gitignore_updated": gi_updated}
    return _emit(payload, json_output, [f"brigade mcp: wrote {path}", f"gitignore_updated: {gi_updated}"], 0)


def _parse_env_args(pairs: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse --env KEY=ref:VAR | KEY=literal:VAL | KEY=VAL (bare = literal)."""
    env: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for pair in pairs:
        if "=" not in pair:
            errors.append(f"invalid --env {pair!r} (expected KEY=ref:VAR or KEY=VALUE)")
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if value.startswith("ref:"):
            env[key] = {"ref": value[4:]}
        elif value.startswith("literal:"):
            env[key] = {"literal": value[8:]}
        else:
            env[key] = {"literal": value}
    return env, errors


def add(
    *,
    target: Path,
    name: str,
    command: str | None = None,
    args: list[str] | None = None,
    env: list[str] | None = None,
    url: str | None = None,
    transport: str = "stdio",
    timeout: int | None = None,
    targets: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    servers, errors, _ = load_canonical(target)
    if errors:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    env_map, env_errors = _parse_env_args(env or [])
    if env_errors:
        return _emit({"errors": env_errors}, json_output, [f"error: {e}" for e in env_errors], 2)
    server = CanonicalServer(
        name=name,
        transport=transport,
        command=command,
        args=tuple(args or []),
        env=env_map,
        url=url,
        timeout=timeout,
        targets=tuple(targets) if targets else None,
    )
    issues = mcp_adapters.validate_server(server)
    blocking = [msg for sev, msg in issues if sev == "error"]
    if blocking:
        return _emit({"errors": blocking}, json_output, [f"error: {m}" for m in blocking], 2)
    servers[name] = server
    _write_canonical(target, servers)
    warnings = [msg for sev, msg in issues if sev == "warn"]
    payload = {"server": name, "written": True, "canonical_path": str(canonical_path(target)), "warnings": warnings}
    lines = [f"brigade mcp: added {name} to {canonical_path(target)}"] + [f"warning: {w}" for w in warnings]
    return _emit(payload, json_output, lines, 0)


def list_servers(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    servers, errors, _ = load_canonical(target)
    if errors:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    rows = []
    for name, s in sorted(servers.items()):
        refs = sorted(k for k, v in {**s.env, **s.headers}.items() if "ref" in v)
        rows.append(
            {
                "name": name,
                "transport": s.transport,
                "endpoint": s.url if s.is_remote else s.command,
                "enabled": s.enabled,
                "targets": list(s.targets) if s.targets is not None else "all",
                "env_refs": refs,
            }
        )
    lines = [f"{r['name']:<20} {r['transport']:<6} {r['endpoint'] or ''}" for r in rows] or ["(no servers)"]
    return _emit({"servers": rows, "count": len(rows)}, json_output, lines, 0)


def plan(
    *,
    target: Path,
    name: str | None = None,
    harness: str | None = None,
    user_scope: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    servers, errors, _ = load_canonical(target)
    if errors:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    harnesses, notes = active_targets(target, harness=harness, user_scope=user_scope)
    state = _load_state(target)
    items: list[dict[str, Any]] = []
    for h in harnesses:
        items.extend(
            _plan_for_harness(target, h, servers, state, force=False, prune=True, adopt=False, name_filter=name)
        )
    counts = _counts(items)
    source_catalog = str(canonical_path(target))
    destination_files = [str(mcp_adapters.resolve_path(ADAPTERS[h], target)) for h in harnesses]
    payload = {
        "target": str(target),
        "source_catalog": source_catalog,
        "destination_files": destination_files,
        "harnesses": harnesses,
        "notes": notes,
        "items": _public_items(items),
        "counts": counts,
    }
    lines = [f"source: {source_catalog}"]
    lines.extend(f"destination: {path}" for path in destination_files)
    lines.extend(f"{i['harness']:<12} {i['server']:<20} {i['status']:<14} -> {i['action']}" for i in items)
    if not items:
        lines.append("(nothing to plan)")
    lines.extend(notes)
    rc = 1 if counts["conflict"] else 0
    return _emit(payload, json_output, lines, rc)


def _config_current_by_name(
    target: Path,
    servers: dict[str, CanonicalServer],
    harnesses: list[str],
    state: dict[str, Any],
    *,
    name_filter: str | None = None,
) -> dict[str, bool]:
    current = {name: False for name in servers}
    for server_name, server in servers.items():
        if name_filter is not None and server_name != name_filter:
            continue
        applicable_statuses: list[str] = []
        for harness in harnesses:
            if not _server_targets_harness(server, harness):
                continue
            for item in _plan_for_harness(
                target,
                harness,
                servers,
                state,
                force=False,
                prune=False,
                adopt=False,
                name_filter=server_name,
            ):
                if item["server"] == server_name:
                    applicable_statuses.append(item["status"])
                    break
        if applicable_statuses and all(status == "current" for status in applicable_statuses):
            current[server_name] = True
    return current


def _verify_timeout_error(timeout: float | None, *, flag: str = "--timeout") -> str | None:
    if timeout is None:
        return None
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        return f"{flag} must be greater than 0 and no more than 300 seconds"
    return None


def _servers_for_verification(
    servers: dict[str, CanonicalServer],
    harnesses: list[str],
    *,
    name_filter: str | None = None,
) -> dict[str, CanonicalServer]:
    selected: dict[str, CanonicalServer] = {}
    for server_name, server in servers.items():
        if name_filter is not None and server_name != name_filter:
            continue
        if not server.enabled:
            continue
        if any(_server_targets_harness(server, harness) for harness in harnesses):
            selected[server_name] = server
    return selected


def verify(
    *,
    target: Path,
    name: str | None = None,
    harness: str | None = None,
    user_scope: bool = False,
    timeout: float | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    timeout_error = _verify_timeout_error(timeout)
    if timeout_error:
        return _emit({"errors": [timeout_error]}, json_output, [f"error: {timeout_error}"], 2)
    servers, errors, _ = load_canonical(target)
    if errors:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    harnesses, notes = active_targets(target, harness=harness, user_scope=user_scope)
    if not harnesses:
        message = notes[0] if notes else "no MCP harnesses selected for verification"
        return _emit({"errors": [message], "notes": notes}, json_output, [f"error: {message}"], 2)
    state = _load_state(target)
    selected = _servers_for_verification(servers, harnesses, name_filter=name)
    if not selected:
        message = f"no servers matched verification filters (name={name!r})"
        return _emit({"errors": [message]}, json_output, [f"error: {message}"], 2)
    config_current = _config_current_by_name(target, servers, harnesses, state, name_filter=name)
    filters = {"name": name, "harness": harness, "user_scope": user_scope}
    payload, rc = mcp_runtime.run_verification(
        target,
        selected,
        config_current_by_name={server_name: config_current.get(server_name, False) for server_name in selected},
        timeout_override=timeout,
        filters=filters,
    )
    if notes:
        payload["notes"] = notes
    lines = [
        f"brigade mcp verify: {target}",
        f"receipt: {payload['receipt_path']}",
    ]
    lines += [
        f"{result['name']:<20} config_current={result['config_current']} runtime_healthy={result['runtime_healthy']}"
        for result in payload["results"]
    ]
    lines += notes
    return _emit(payload, json_output, lines, rc)


@dataclass
class McpSyncPlan:
    target: Path
    harnesses: list[str]
    items: list[dict[str, Any]]
    notes: list[str]
    exposures: list[dict[str, Any]]
    gated: set[str]
    gate_error: str | None
    gate_declined: bool
    projection: Any | None
    source_catalog: str
    destination_files: list[str]
    errors: list[str] = field(default_factory=list)
    native_write_paths: list[Path] = field(default_factory=list)


def _state_bytes(state: dict[str, Any]) -> bytes:
    return (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _content_digest(data: bytes | None) -> str:
    return projection.ABSENT if data is None else hashlib.sha256(data).hexdigest()


def _mutation_for_path(
    *,
    dest: Path,
    display_path: str,
    before: bytes | None,
    after: bytes | None,
) -> Any | None:
    if before == after:
        return None
    if before is None:
        mutation_type = "create"
    elif after is None:
        mutation_type = "remove"
    else:
        mutation_type = "replace"
    return projection.mutation(
        destination=dest,
        display_path=display_path,
        mutation=mutation_type,
        expected_before=_content_digest(before),
        desired_after=_content_digest(after),
        staged_bytes=after,
    )


def _recovery_records(target: Path) -> list[dict[str, Any]]:
    records = []
    for record in projection.unfinished_operations(target):
        if not record.operation_id.startswith("mcp-"):
            continue
        records.append(
            {
                "operation_id": record.operation_id,
                "terminal_state": "recovery-required",
                "status": record.status,
                "recovery_command": f"brigade mcp recover {record.operation_id}",
            }
        )
    return records


def build_sync_plan(
    *,
    target: Path,
    name: str | None = None,
    harness: str | None = None,
    write: bool = False,
    force: bool = False,
    prune: bool = False,
    adopt: bool = False,
    user_scope: bool = False,
    allow_global_stdio: bool = False,
    interactive: bool | None = None,
    json_output: bool = False,
) -> McpSyncPlan:
    target = target.expanduser().resolve()
    servers, errors, _ = load_canonical(target)
    notes: list[str] = []
    if errors:
        return McpSyncPlan(
            target=target,
            harnesses=[],
            items=[],
            notes=[],
            exposures=[],
            gated=set(),
            gate_error=None,
            gate_declined=False,
            projection=None,
            source_catalog=str(canonical_path(target)),
            destination_files=[],
            errors=errors,
        )
    harnesses, notes = active_targets(target, harness=harness, user_scope=user_scope)
    state = copy.deepcopy(_load_state(target))
    planned = [
        (h, _plan_for_harness(target, h, servers, state, force=force, prune=prune, adopt=adopt, name_filter=name))
        for h in harnesses
    ]
    exposures = _user_stdio_exposures(planned, target)
    gated: set[str] = set()
    gate_error: str | None = None
    gate_declined = False
    if exposures:
        warning_lines = _stdio_exposure_lines(exposures)
        notes.extend(warning_lines)
        if write and not allow_global_stdio:
            is_interactive = interactive if interactive is not None else (not json_output and sys.stdin.isatty())
            if not is_interactive:
                gated = {e["harness"] for e in exposures}
                gate_error = (
                    "user-scoped sync would write stdio MCP servers into a user-wide client config; "
                    "use project scope or an HTTP/SSE transport, or acknowledge the user-scope "
                    "stdio risk only after operator review"
                )
            else:
                for line in warning_lines:
                    print(line, file=sys.stderr)
                print("Write stdio servers into the user-wide config? [y/N]: ", end="", file=sys.stderr, flush=True)
                answer = input().strip().lower()
                if answer not in ("y", "yes"):
                    gated = {e["harness"] for e in exposures}
                    gate_declined = True
                    notes.append("user-scoped stdio destinations skipped: not confirmed at the prompt")

    all_items: list[dict[str, Any]] = []
    mutations: list[Any] = []
    native_write_paths: list[Path] = []
    plan_errors: list[str] = []
    desired_state = copy.deepcopy(state)
    for h, items in planned:
        if h in gated:
            for item in items:
                if item["action"] in ("create", "update", "remove"):
                    item["action"] = "skip"
                    suffix = "not written: user-scoped stdio not acknowledged"
                    item["detail"] = f"{item['detail']}; {suffix}" if item["detail"] else suffix
            all_items.extend(items)
            continue
        all_items.extend(items)
        adapter = ADAPTERS[h]
        path = mcp_adapters.resolve_path(adapter, target)
        owner_map = desired_state.get("ownership", {}).get(h, {}).get(adapter.path)
        to_write: dict[str, dict[str, Any]] = {}
        to_remove: set[str] = set()
        changed = False
        for item in items:
            server_name = item["server"]
            action, status = item["action"], item["status"]
            if action in ("create", "update"):
                if owner_map is None:
                    owner_map = desired_state.setdefault("ownership", {}).setdefault(h, {}).setdefault(adapter.path, {})
                to_write[server_name] = _project_server(target, h, servers[server_name])
                owner_map[server_name] = {
                    "canonical_fingerprint": item["_canon_fp"],
                    "projected_fingerprint": item["_proj_fp"],
                }
                changed = True
            elif action == "remove":
                to_remove.add(server_name)
                if owner_map is not None:
                    owner_map.pop(server_name, None)
                changed = True
            elif status == "current":
                if owner_map is None:
                    owner_map = desired_state.setdefault("ownership", {}).setdefault(h, {}).setdefault(adapter.path, {})
                to_write[server_name] = _project_server(target, h, servers[server_name])
                owner_map[server_name] = {
                    "canonical_fingerprint": item["_canon_fp"],
                    "projected_fingerprint": item["_proj_fp"],
                }
        if changed or any(i.get("_reconciled") for i in items):
            existing = path.read_text() if path.is_file() else None
            try:
                new_text = adapter.write_file(existing, to_write, to_remove)
            except ValueError as exc:
                plan_errors.append(f"{path}: {exc}")
                continue
            mutation = _mutation_for_path(
                dest=path,
                display_path=adapter.path,
                before=_file_bytes(path),
                after=new_text.encode("utf-8"),
            )
            if mutation is not None:
                mutations.append(mutation)
                native_write_paths.append(path)

    ownership_path = state_path(target)
    if ownership_path.is_file() or desired_state != state:
        ownership_mutation = _mutation_for_path(
            dest=ownership_path,
            display_path=STATE_REL,
            before=_file_bytes(ownership_path),
            after=_state_bytes(desired_state),
        )
        if ownership_mutation is not None:
            mutations.append(ownership_mutation)

    canon = canonical_path(target)
    source_digest = localio.file_sha256(canon) if canon.is_file() else "missing"
    projection_plan = (
        projection.build_plan(
            operation_id=f"mcp-{uuid.uuid4().hex}",
            projector="mcp",
            source_fingerprint=source_digest,
            mutations=mutations,
            target=target,
        )
        if mutations
        else None
    )
    return McpSyncPlan(
        target=target,
        harnesses=harnesses,
        items=all_items,
        notes=notes,
        exposures=exposures,
        gated=gated,
        gate_error=gate_error,
        gate_declined=gate_declined,
        projection=projection_plan,
        source_catalog=str(canon),
        destination_files=[str(mcp_adapters.resolve_path(ADAPTERS[h], target)) for h in harnesses],
        errors=plan_errors,
        native_write_paths=native_write_paths,
    )


def status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    recovery = _recovery_records(target)
    payload = {
        "target": str(target),
        "recovery": recovery,
        "terminal_state": recovery[0]["terminal_state"] if recovery else "unchanged",
    }
    lines = [f"brigade mcp status: {target}"]
    if recovery:
        for record in recovery:
            lines.append(f"recovery-required: {record.get('operation_id')} ({record.get('recovery_command')})")
    else:
        lines.append("no unfinished projection recovery")
    return _emit(payload, json_output, lines, 0)


def recover_operation(*, target: Path, operation_id: str, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not operation_id.startswith("mcp-"):
        message = "MCP recovery requires an MCP operation id"
        return _emit({"errors": [message]}, json_output, [f"error: {message}"], 2)
    try:
        receipt = projection.recover(operation_id, target=target, force=force)
    except projection.ProjectionError as exc:
        return _emit({"errors": [str(exc)]}, json_output, [f"error: {exc}"], 2)
    payload = receipt.to_dict()
    payload["recovery_command"] = f"brigade mcp recover {operation_id}" if receipt.recovery_command else None
    lines = [f"brigade mcp recover {operation_id}: {receipt.terminal_state}"]
    rc = 0 if receipt.terminal_state == "restored" else 2
    return _emit(payload, json_output, lines, rc)


def sync(
    *,
    target: Path,
    name: str | None = None,
    harness: str | None = None,
    write: bool = False,
    force: bool = False,
    prune: bool = False,
    adopt: bool = False,
    user_scope: bool = False,
    allow_global_stdio: bool = False,
    interactive: bool | None = None,
    verify_runtime: bool = False,
    verify_timeout: float | None = None,
    json_output: bool = False,
    inject_failure: str | None = None,
) -> int:
    target = target.expanduser().resolve()
    if verify_runtime and not write:
        message = "--verify requires --write"
        return _emit({"errors": [message]}, json_output, [f"error: {message}"], 2)
    timeout_error = _verify_timeout_error(verify_timeout, flag="--verify-timeout")
    if timeout_error:
        return _emit({"errors": [timeout_error]}, json_output, [f"error: {timeout_error}"], 2)
    planned = build_sync_plan(
        target=target,
        name=name,
        harness=harness,
        write=write,
        force=force,
        prune=prune,
        adopt=adopt,
        user_scope=user_scope,
        allow_global_stdio=allow_global_stdio,
        interactive=interactive,
        json_output=json_output,
    )
    if planned.errors and not planned.items:
        return _emit({"errors": planned.errors}, json_output, [f"error: {e}" for e in planned.errors], 2)
    if planned.errors:
        return _emit({"errors": planned.errors}, json_output, [f"error: {e}" for e in planned.errors], 2)

    files_written: list[str] = []
    terminal_state = "planned"
    operation_id: str | None = planned.projection.operation_id if planned.projection else None
    projection_view: dict[str, Any] = {
        "operation_id": operation_id,
        "projector": "mcp",
        "terminal_state": terminal_state,
        "destinations": [
            {"display_path": mutation.display_path, "mutation": mutation.mutation}
            for mutation in (planned.projection.mutations if planned.projection else ())
        ],
    }
    if write and planned.projection:
        try:
            receipt = projection.execute(
                planned.projection,
                target=target,
                inject=projection.FailureInjector(boundary=inject_failure),
            )
        except projection.OverlapBlockedError as exc:
            message = str(exc)
            return _emit(
                {
                    "target": str(target),
                    "errors": [message],
                    "operation_id": operation_id,
                    "terminal_state": "recovery-required",
                    "recovery": _recovery_records(target),
                },
                json_output,
                [f"error: {message}"],
                2,
            )
        except projection.DriftError as exc:
            message = str(exc)
            return _emit(
                {"target": str(target), "errors": [message], "operation_id": operation_id, "terminal_state": "planned"},
                json_output,
                [f"error: {message}"],
                2,
            )
        terminal_state = receipt.terminal_state
        operation_id = receipt.operation_id
        projection_view = receipt.to_dict()
        if terminal_state == "committed":
            files_written = [str(path) for path in planned.native_write_paths]
        elif terminal_state in {"restored", "recovery-required"}:
            payload = {
                "target": str(target),
                "source_catalog": planned.source_catalog,
                "destination_files": planned.destination_files,
                "harnesses": planned.harnesses,
                "wrote": False,
                "files_written": [],
                "notes": planned.notes,
                "stdio_exposures": planned.exposures,
                "items": _public_items(planned.items),
                "counts": _counts(planned.items),
                "operation_id": operation_id,
                "terminal_state": terminal_state,
                "projection": projection_view,
                "errors": [f"projection {terminal_state}"],
            }
            return _emit(payload, json_output, [f"error: projection {terminal_state}"], 2)

    if write and planned.projection is None:
        terminal_state = "unchanged"
        operation_id = None
        projection_view["terminal_state"] = terminal_state
        projection_view["operation_id"] = None
    counts = _counts(planned.items)
    payload = {
        "target": str(target),
        "source_catalog": planned.source_catalog,
        "destination_files": planned.destination_files,
        "harnesses": planned.harnesses,
        "wrote": write and terminal_state == "committed",
        "files_written": files_written,
        "notes": planned.notes,
        "stdio_exposures": planned.exposures,
        "items": _public_items(planned.items),
        "counts": counts,
        "operation_id": None if terminal_state == "unchanged" else operation_id,
        "terminal_state": terminal_state if write else "planned",
        "projection": projection_view,
    }
    if write and terminal_state == "unchanged":
        payload["operation_id"] = None
        payload["projection"] = projection_view
    servers, _, _ = load_canonical(target)
    if write and verify_runtime:
        verifiable = [h for h in planned.harnesses if h not in planned.gated]
        selected = _servers_for_verification(servers, verifiable, name_filter=name) if verifiable else {}
        if selected:
            state = _load_state(target)
            config_current = _config_current_by_name(target, servers, verifiable, state, name_filter=name)
            verification_payload, verify_rc = mcp_runtime.run_verification(
                target,
                selected,
                config_current_by_name={
                    server_name: config_current.get(server_name, False) for server_name in selected
                },
                timeout_override=verify_timeout,
                filters={"name": name, "harness": harness, "user_scope": user_scope},
            )
            payload["verification"] = {
                "receipt_path": verification_payload["receipt_path"],
                "results": verification_payload["results"],
            }
        else:
            verify_rc = 0
    else:
        verify_rc = 0

    verb = "wrote" if write else "would"
    lines = [f"brigade mcp sync ({'write' if write else 'dry-run'}): {target}", f"source: {planned.source_catalog}"]
    lines += [f"destination: {path}" for path in planned.destination_files]
    lines += [f"{i['harness']:<12} {i['server']:<20} {i['status']:<14} -> {i['action']}" for i in planned.items]
    lines += [f"{verb}: {f}" for f in files_written]
    if write and verify_runtime and payload.get("verification"):
        lines.append(f"verification receipt: {payload['verification']['receipt_path']}")
    lines += planned.notes
    rc = 1 if counts["conflict"] else 0
    if verify_rc != 0:
        rc = verify_rc
    if planned.gate_error:
        payload["errors"] = [planned.gate_error]
        payload["stdio_gated"] = sorted(planned.gated)
        lines.append(f"error: {planned.gate_error}")
        rc = 2
    elif planned.gate_declined:
        payload["stdio_gated"] = sorted(planned.gated)
        rc = 1
    return _emit(payload, json_output, lines, rc)


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    servers, errors, warnings = load_canonical(target)
    issues: list[dict[str, str]] = [{"severity": "error", "message": m} for m in errors]
    issues += [{"severity": "warn", "message": m} for m in warnings]
    for server in servers.values():
        for severity, message in mcp_adapters.validate_server(server):
            issues.append({"severity": severity, "message": message})
    for record in _recovery_records(target):
        command = record.get("recovery_command") or f"brigade mcp recover {record.get('operation_id')}"
        issues.append(
            {
                "severity": "warn",
                "message": f"projection recovery-required: {record.get('operation_id')}; run `{command}`",
            }
        )
    configured = _configured_harnesses(target)
    unsupported: list[str] = []
    if configured:
        unsupported = sorted(h for h in configured if h not in ADAPTERS)
    payload = {
        "valid": not any(i["severity"] == "error" for i in issues),
        "canonical_path": str(canonical_path(target)),
        "server_count": len(servers),
        "issues": issues,
        "unsupported_harnesses": unsupported,
    }
    lines = [f"brigade mcp doctor: {len(servers)} server(s), {len(issues)} issue(s)"]
    lines += [f"[{i['severity']}] {i['message']}" for i in issues]
    if unsupported:
        lines.append(f"no MCP adapter for configured harness(es): {', '.join(unsupported)}")
    rc = 0 if payload["valid"] else 1
    return _emit(payload, json_output, lines, rc)


def import_servers(
    *,
    target: Path,
    harness: str,
    merge: bool = False,
    user_scope: bool = False,
    keep_secrets: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    harness = _scoped_harness(harness, user_scope=user_scope)
    adapter = ADAPTERS.get(harness)
    if adapter is None:
        return _emit(
            {"errors": [f"unknown MCP target {harness!r}"]}, json_output, [f"error: unknown target {harness!r}"], 2
        )
    if adapter.user_scope and not user_scope:
        return _emit(
            {"errors": [f"{harness} is user-scoped; pass --user-scope"]},
            json_output,
            [f"error: {harness} is user-scoped; pass --user-scope"],
            2,
        )
    path = mcp_adapters.resolve_path(adapter, target)
    if not path.is_file():
        return _emit({"errors": [f"{path}: not found"]}, json_output, [f"error: {path} not found"], 2)
    live = adapter.read_file(path.read_text())
    existing, errors, _ = load_canonical(target)
    if errors and merge:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    discovered: list[str] = []
    secrets_demoted: list[str] = []
    skipped_existing: list[str] = []
    to_add: dict[str, CanonicalServer] = {}
    for srv_name, raw in sorted(live.items()):
        server, demoted = adapter.from_provider(srv_name, raw, keep_secrets=keep_secrets)
        discovered.append(srv_name)
        secrets_demoted.extend(f"{srv_name}.{d}" for d in demoted)
        if srv_name in existing:
            skipped_existing.append(srv_name)
            continue
        to_add[srv_name] = server
    if merge:
        existing.update(to_add)
        _write_canonical(target, existing)
    payload = {
        "harness": harness,
        "merged": merge,
        "discovered": discovered,
        "added": sorted(to_add),
        "skipped_existing": skipped_existing,
        "secrets_demoted": secrets_demoted,
    }
    lines = [f"brigade mcp import {harness}: {len(discovered)} discovered, {len(to_add)} new"]
    lines += [f"+ {n}" for n in sorted(to_add)]
    lines += [f"secret demoted to ref: {s}" for s in secrets_demoted]
    if not merge:
        lines.append("(preview only; pass --merge to write into .brigade/mcp.json)")
    return _emit(payload, json_output, lines, 0)
