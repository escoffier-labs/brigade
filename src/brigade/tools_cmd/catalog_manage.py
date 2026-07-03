"""Catalog management commands for the tools command family."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import toml_compat as tomllib
from ..config import load_config as load_brigade_config
from ..install import apply_gitignore
from ..render import emit
from ..selection import Selection
from . import config, constants, helpers, paths, projections as projections_mod


def _facade(name: str) -> Any:
    return getattr(sys.modules[__package__], name)


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, default_tools: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = paths.config_path(target)
    if path.exists() and not force:
        print(f"error: tool catalog config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    # A minimal install gets an empty catalog: no default tools and no
    # tracked tools/*.md sources until the user opts in (--full).
    path.write_text(helpers._format_tools_toml() if default_tools else helpers._format_tool_entries([]))
    source_results = helpers._ensure_default_tool_sources(target) if default_tools else []
    print(f"tools_config: {path}")
    print(f"tools: {len(constants.DEFAULT_TOOLS) if default_tools else 0}")
    print(f"sources: {sum(1 for item in source_results if item['action'] == 'create')}")
    if update_gitignore:
        result = apply_gitignore(target, _gitignore_selection(target))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade tools list")
    return 0


def _read_tool_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    if tomllib is None:
        return [], ["tool catalog requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid tool catalog config: {exc}"]
    values = payload.get("tool")
    if values is None:
        return [], []
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        return [], ["tool catalog must contain [[tool]] table entries"]
    return [dict(item) for item in values], []


def _projection_scope(target: Path) -> set[str] | None:
    """Return the projection targets wired for this workspace, or None for all.

    A workspace with a Brigade selection (`.brigade/config.json`) only gets
    default projections for its selected harnesses plus the neutral `scripts`
    folder. Without a selection, defaults stay unscoped.
    """
    try:
        config = load_brigade_config(target)
    except Exception:
        return None
    if config is None:
        return None
    harnesses = [h for h in (config.selection.harnesses or []) if isinstance(h, str) and h.strip()]
    if not harnesses:
        return None
    scope = set(harnesses)
    owner = str(config.selection.owner or "").strip()
    if owner:
        scope.add(owner)
    scope.add("scripts")
    return scope


def _gitignore_selection(target: Path) -> Selection:
    """Use the workspace's configured selection for gitignore rewrites.

    Re-applying the managed gitignore block with a narrower selection than the
    install drops inbox entries for the other selected harnesses.
    """
    try:
        config = load_brigade_config(target)
    except Exception:
        config = None
    if config is not None and config.selection.harnesses:
        return config.selection
    return Selection("repo", ["codex"], "codex")


def _scoped_default_tool(tool: dict[str, Any], scope: set[str] | None) -> dict[str, Any]:
    entry = dict(tool)
    if scope is None:
        return entry
    entry["supported_harnesses"] = [h for h in entry.get("supported_harnesses", []) if h in scope]
    entry["projections"] = {h: p for h, p in (entry.get("projections") or {}).items() if h in scope}
    return entry


def defaults(
    *,
    target: Path,
    dry_run: bool = False,
    force: bool = False,
    update_gitignore: bool = True,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = paths.config_path(target)
    entries, errors = _read_tool_entries(path)
    if errors:
        payload = {
            "target": str(target),
            "config_path": str(path),
            "valid": False,
            "errors": errors,
            "dry_run": dry_run,
            "force": force,
            "created": False,
            "added": [],
            "updated": [],
            "skipped": [],
            "conflicts": [],
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 1
    existing_by_id = {
        str(entry.get("id")): index
        for index, entry in enumerate(entries)
        if isinstance(entry.get("id"), str) and str(entry.get("id")).strip()
    }
    projection_scope = _projection_scope(target)
    defaults_by_id = {str(tool["id"]): _scoped_default_tool(tool, projection_scope) for tool in constants.DEFAULT_TOOLS}
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    conflicts: list[dict[str, str]] = []
    merged = [dict(entry) for entry in entries]
    for tool_id, default_entry in defaults_by_id.items():
        if tool_id not in existing_by_id:
            merged.append(dict(default_entry))
            added.append(tool_id)
            continue
        index = existing_by_id[tool_id]
        existing = merged[index]
        existing_source = str(existing.get("source_path") or "")
        default_source = str(default_entry.get("source_path") or "")
        if force or existing_source == default_source:
            if existing == default_entry:
                skipped.append(tool_id)
            else:
                merged[index] = dict(default_entry)
                updated.append(tool_id)
            continue
        conflicts.append(
            {
                "tool_id": tool_id,
                "existing_source_path": existing_source,
                "default_source_path": default_source,
                "detail": "existing tool id uses a different source_path",
            }
        )
    created = not path.exists()
    if not dry_run and not conflicts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(helpers._format_tool_entries(merged))
    source_results = helpers._ensure_default_tool_sources(target, dry_run=dry_run or bool(conflicts))
    gitignore_result = "skipped"
    if update_gitignore and not dry_run and not conflicts:
        gitignore_result = apply_gitignore(target, _gitignore_selection(target))
    payload = {
        "target": str(target),
        "config_path": str(path),
        "valid": not conflicts,
        "errors": [],
        "dry_run": dry_run,
        "force": force,
        "created": created,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "conflicts": conflicts,
        "sources": source_results,
        "source_created_count": len([item for item in source_results if item.get("action") == "create"]),
        "source_skipped_count": len([item for item in source_results if item.get("action") == "skip"]),
        "tool_count": len(merged),
        "default_count": len(defaults_by_id),
        "projection_scope": sorted(projection_scope) if projection_scope is not None else None,
        "gitignore": gitignore_result,
        "next_command": "brigade tools apply --target . --all",
    }
    text_lines = [
        f"tools defaults: {target}",
        f"config_path: {path}",
        f"dry_run: {dry_run}",
        f"created: {'yes' if created else 'no'}",
        f"added: {len(added)}",
        f"updated: {len(updated)}",
        f"skipped: {len(skipped)}",
        f"conflicts: {len(conflicts)}",
        f"sources_created: {payload['source_created_count']}",
        f"gitignore: {gitignore_result}",
    ]
    for tool_id in added:
        text_lines.append(f"- added: {tool_id}")
    for tool_id in updated:
        text_lines.append(f"- updated: {tool_id}")
    for conflict in conflicts:
        text_lines.append(f"- conflict: {conflict['tool_id']} {conflict['detail']}")
    text_lines.append(f"next_command: {payload['next_command']}")
    return emit(payload, json_output, text_lines, 0 if payload["valid"] else 1)


def list_tools(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _facade("_catalog_payload")(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    if not payload["tools"]:
        print("tools: none")
        return 0
    for tool in payload["tools"]:
        print(
            f"- {tool.get('id')} [{tool.get('family')}] "
            f"harnesses={','.join(tool.get('supported_harnesses', []))} "
            f"tools={tool.get('tool_count')}"
        )
        if tool.get("description"):
            print(f"  {helpers._short(str(tool['description']))}")
    return 0


def show(*, target: Path, tool_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _facade("_catalog_payload")(target)
    tool = None
    for item in payload["tools"]:
        if item.get("id") == tool_id:
            tool = item
            break
    if json_output:
        print(
            json.dumps(
                {"target": str(target), "config_path": payload["config_path"], "tool": tool}, indent=2, sort_keys=True
            )
        )
        return 0 if tool is not None else 1
    if tool is None:
        print(f"error: tool not found: {tool_id}", file=sys.stderr)
        return 1
    print(f"tool: {tool.get('id')}")
    print(f"name: {tool.get('name')}")
    print(f"family: {tool.get('family')}")
    print(f"description: {tool.get('description')}")
    print(f"supported_harnesses: {', '.join(tool.get('supported_harnesses', []))}")
    print(f"tool_count: {tool.get('tool_count')}")
    print(f"schema_available: {tool.get('schema_available')}")
    print(f"auth_label: {tool.get('auth_label') or ''}")
    print("projections:")
    for harness, status in sorted(tool.get("projection_coverage", {}).items()):
        print(f"  {harness}: {status}")
    if tool.get("mcp"):
        print(f"mcp_servers: {tool['mcp'].get('server_count')}")
    return 0


def search(*, target: Path, query: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    needle = query.casefold().strip()
    payload = _facade("_catalog_payload")(target)
    matches = [
        tool
        for tool in payload["tools"]
        if needle
        and needle in " ".join(str(tool.get(key, "")) for key in ("id", "name", "family", "description")).casefold()
    ]
    result = {"target": str(target), "query": query, "matches": matches, "match_count": len(matches)}
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"tool search: {query}")
    print(f"matches: {len(matches)}")
    for tool in matches:
        print(f"- {tool.get('id')} [{tool.get('family')}] {helpers._short(str(tool.get('description', '')))}")
    return 0


def describe(*, target: Path, tool_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _facade("_describe_payload")(target, tool_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] and payload["tool"] is not None else 1
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return 1
    tool = payload["tool"]
    assert isinstance(tool, dict)
    raw_contract = tool.get("contract")
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    print(f"tool: {tool.get('id')}")
    print(f"name: {tool.get('name')}")
    print(f"family: {tool.get('family')}")
    print(f"description: {tool.get('description')}")
    print(f"command: {contract.get('command') or ''}")
    print(f"approval_mode: {contract.get('approval_mode')}")
    print(f"input_schema: {contract.get('input_schema_path') or ''}")
    print(f"output_schema: {contract.get('output_schema_path') or ''}")
    print(f"permissions: {', '.join(contract.get('permissions', []))}")
    print(f"effects: {', '.join(contract.get('effects', []))}")
    print(f"contract_issues: {payload['issue_count']}")
    return 0


def contracts(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = projections_mod._contracts_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools contracts: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    print(f"contracts: {payload['contract_count']}")
    print(f"contract_issues: {payload['issue_count']}")
    for contract in payload["contracts"]:
        status = "ready" if contract.get("has_contract") and contract.get("issue_count") == 0 else "needs-review"
        print(f"- {contract.get('tool_id')} [{contract.get('family')}] {status} issues={contract.get('issue_count')}")
        print(f"  input_schema: {contract.get('input_schema_path') or ''}")
        print(f"  approval_mode: {contract.get('approval_mode')}")
    return 0


def plan(*, target: Path, tool_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = projections_mod._projection_plan_payload(target, tool_id=tool_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools projection plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if tool_id is not None:
        print(f"tool_id: {tool_id}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    projections = payload["projections"]
    print(f"projections: {len(projections)}")
    if payload["counts"]:
        print("counts:")
        for status, count in sorted(payload["counts"].items()):
            print(f"  {status}: {count}")
    for item in projections:
        print(f"- {item.get('tool_id')} {item.get('harness')} {item.get('status')} action={item.get('action')}")
        print(f"  source: {item.get('source_path')}")
        print(f"  target: {item.get('projection_path')}")
        if item.get("expected_fingerprint"):
            print(f"  expected_fingerprint: {item.get('expected_fingerprint')}")
        print(f"  detail: {item.get('detail')}")
    return 0


def apply(
    *,
    target: Path,
    tool_id: str | None = None,
    all_tools: bool = False,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if bool(tool_id) == bool(all_tools):
        print("error: pass exactly one of <tool-id> or --all", file=sys.stderr)
        return 2
    tools, errors = config._load_config(target)
    selected = [tool for tool in tools if tool.get("enabled", True) and (all_tools or tool.get("id") == tool_id)]
    if tool_id is not None and not selected and not errors:
        errors.append(f"tool not found: {tool_id}")
    generated_at = datetime.now(timezone.utc)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for tool in selected:
        for harness in tool.get("supported_harnesses", []):
            item = projections_mod._projection_item(target, tool, harness, generated_at=generated_at, force=force)
            public_item = {key: value for key, value in item.items() if key != "rendered"}
            action = item.get("action")
            if action == "conflict":
                conflicts.append(public_item)
                continue
            if action not in {"create", "update"}:
                skipped.append(public_item)
                continue
            if dry_run:
                applied.append({**public_item, "dry_run": True})
                continue
            projection_path = Path(str(item["projection_path"]))
            projection_path.parent.mkdir(parents=True, exist_ok=True)
            projection_path.write_text(str(item["rendered"]))
            applied.append(public_item)
    payload = {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool_id": tool_id,
        "all": all_tools,
        "dry_run": dry_run,
        "force": force,
        "applied": applied,
        "skipped": skipped,
        "conflicts": conflicts,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "conflict_count": len(conflicts),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors and not conflicts else 1
    print(f"tools projection apply: {target}")
    print(f"config_path: {paths.config_path(target)}")
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    print(f"applied: {len(applied)}")
    print(f"skipped: {len(skipped)}")
    print(f"conflicts: {len(conflicts)}")
    for item in applied:
        verb = "would_write" if dry_run else "wrote"
        print(f"- {verb}: {item.get('tool_id')} {item.get('harness')} {item.get('projection_path')}")
    for item in conflicts:
        print(f"- conflict: {item.get('tool_id')} {item.get('harness')} {item.get('detail')}")
    return 0 if not conflicts else 1
