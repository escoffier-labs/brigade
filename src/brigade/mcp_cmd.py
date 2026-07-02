"""`brigade mcp` - sync one canonical MCP server catalog into each tool's native config.

Canonical source: ``.brigade/mcp.json`` (tracked/shared). Ownership of which server
keys Brigade manages in each provider file is tracked in the gitignored sidecar
``.brigade/mcp/state.json`` - JSON config files cannot carry a managed-header comment
and are co-owned with the user, so Brigade merges by server key and never owns the whole
file. Foreign servers are preserved; a server the user edited becomes a conflict (skipped
unless ``--force``); orphans are removed only with ``--prune`` and only when still pristine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import localio, mcp_adapters
from .mcp_adapters import ADAPTERS, MCP_TARGETS, CanonicalServer

CANONICAL_REL = ".brigade/mcp.json"
STATE_REL = ".brigade/mcp/state.json"

GITIGNORE_SNIPPET = [
    "# brigade mcp: canonical source is shared, sidecar state is machine-local",
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


def active_targets(target: Path, *, harness: str | None, user_scope: bool) -> tuple[list[str], list[str]]:
    """Resolve which adapters this run touches. Returns (harnesses, notes)."""
    notes: list[str] = []
    if harness is not None:
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
    return server.targets is None or harness in server.targets


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
        provider_dict = adapter.to_provider(server)
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
                        detail="server was edited outside Brigade; --force to overwrite",
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


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _emit(payload: dict[str, Any], json_output: bool, text_lines: list[str], rc: int) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in text_lines:
            print(line)
    return rc


def _ensure_gitignore(target: Path) -> bool:
    gi = target / ".gitignore"
    existing = gi.read_text() if gi.is_file() else ""
    if "!.brigade/mcp.json" in existing:
        return False
    block = "\n".join(GITIGNORE_SNIPPET)
    sep = "" if existing == "" or existing.endswith("\n") else "\n"
    gi.write_text(existing + sep + ("\n" if existing else "") + block + "\n")
    return True


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = canonical_path(target)
    created = not path.exists()
    if path.exists() and not force:
        return _emit(
            {"canonical_path": str(path), "created": False, "error": "already exists (use --force)"},
            json_output,
            [f"error: {path} already exists (use --force)"],
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
    payload = {
        "target": str(target),
        "harnesses": harnesses,
        "notes": notes,
        "items": _public_items(items),
        "counts": counts,
    }
    lines = [f"{i['harness']:<12} {i['server']:<20} {i['status']:<14} -> {i['action']}" for i in items] or [
        "(nothing to plan)"
    ]
    lines.extend(notes)
    rc = 1 if counts["conflict"] else 0
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
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    servers, errors, _ = load_canonical(target)
    if errors:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    harnesses, notes = active_targets(target, harness=harness, user_scope=user_scope)
    state = _load_state(target)
    all_items: list[dict[str, Any]] = []
    files_written: list[str] = []

    for h in harnesses:
        items = _plan_for_harness(target, h, servers, state, force=force, prune=prune, adopt=adopt, name_filter=name)
        all_items.extend(items)
        adapter = ADAPTERS[h]
        path = mcp_adapters.resolve_path(adapter, target)
        owner_map = state.setdefault("ownership", {}).setdefault(h, {}).setdefault(adapter.path, {})
        # The write set is built strictly from plan items: create/update/current servers are
        # (re)written; conflict/foreign/orphan-skip servers are omitted so their live value in
        # the file is preserved by the merge. This is what protects a user-edited server.
        to_write: dict[str, dict[str, Any]] = {}
        to_remove: set[str] = set()
        changed = False
        for item in items:
            server_name = item["server"]
            action, status = item["action"], item["status"]
            if action in ("create", "update"):
                to_write[server_name] = adapter.to_provider(servers[server_name])
                owner_map[server_name] = {
                    "canonical_fingerprint": item["_canon_fp"],
                    "projected_fingerprint": item["_proj_fp"],
                }
                changed = True
            elif action == "remove":
                to_remove.add(server_name)
                owner_map.pop(server_name, None)
                changed = True
            elif status == "current":
                # Owned + matching (or reconciled after state loss): re-assert ownership and
                # keep it present in the file. Identical content, so not a "change".
                to_write[server_name] = adapter.to_provider(servers[server_name])
                owner_map[server_name] = {
                    "canonical_fingerprint": item["_canon_fp"],
                    "projected_fingerprint": item["_proj_fp"],
                }
        if write and (changed or any(i.get("_reconciled") for i in items)):
            existing = path.read_text() if path.is_file() else None
            new_text = adapter.write_file(existing, to_write, to_remove)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text)
            files_written.append(str(path))

    if write:
        _save_state(target, state)
    counts = _counts(all_items)
    payload = {
        "target": str(target),
        "harnesses": harnesses,
        "wrote": write,
        "files_written": files_written,
        "notes": notes,
        "items": _public_items(all_items),
        "counts": counts,
    }
    verb = "wrote" if write else "would"
    lines = [f"brigade mcp sync ({'write' if write else 'dry-run'}): {target}"]
    lines += [f"{i['harness']:<12} {i['server']:<20} {i['status']:<14} -> {i['action']}" for i in all_items]
    lines += [f"{verb}: {f}" for f in files_written]
    lines += notes
    rc = 1 if counts["conflict"] else 0
    return _emit(payload, json_output, lines, rc)


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    servers, errors, warnings = load_canonical(target)
    issues: list[dict[str, str]] = [{"severity": "error", "message": m} for m in errors]
    issues += [{"severity": "warn", "message": m} for m in warnings]
    for server in servers.values():
        for severity, message in mcp_adapters.validate_server(server):
            issues.append({"severity": severity, "message": message})
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
