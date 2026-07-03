"""Tool pack and projection sync commands for the tools command family."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..install import apply_gitignore
from ..render import emit
from . import catalog_health, catalog_manage, constants, helpers, paths, projections


def _packs_root(target: Path) -> Path:
    return target / ".brigade" / "tools" / "packs"


def _packs_archive_root(target: Path) -> Path:
    return target / ".brigade" / "tools" / "packs-archive"


def _tool_pack_payload(target: Path) -> dict[str, Any]:
    catalog = catalog_health._catalog_payload(target)
    projection = projections._projection_plan_payload(target)
    payload = {
        "target": catalog["target"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_count": catalog["tool_count"],
        "tools": catalog["tools"],
        "projection_counts": projection["counts"],
        "projections": projection["projections"],
        "parity": catalog["parity"],
        "policy": catalog["policy"],
        "runtimes": catalog["runtimes"],
        "call_queue": catalog["call_queue"],
        "run_history": catalog["run_history"],
        "checkpoints": catalog["checkpoints"],
        "issues": catalog["issues"],
        "issue_count": catalog["issue_count"],
    }
    payload["evidence_fingerprint"] = _tool_pack_evidence_fingerprint(payload)
    return payload


def _tool_pack_evidence_fingerprint(payload: dict[str, Any]) -> str:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    parity = payload.get("parity") if isinstance(payload.get("parity"), dict) else {}
    latest_closeout = parity.get("latest_closeout") if isinstance(parity.get("latest_closeout"), dict) else {}
    return helpers._stable_hash(
        {
            "tool_ids": [tool.get("id") for tool in tools if isinstance(tool, dict)],
            "tool_source_fingerprints": [tool.get("source_fingerprint") for tool in tools if isinstance(tool, dict)],
            "tool_count": payload.get("tool_count"),
            "projection_counts": payload.get("projection_counts"),
            "issue_count": payload.get("issue_count"),
            "issue_fingerprints": [
                issue.get("parity_fingerprint") or helpers._stable_hash(issue)
                for issue in issues
                if isinstance(issue, dict)
            ],
            "call_queue_counts": (payload.get("call_queue") or {}).get("counts")
            if isinstance(payload.get("call_queue"), dict)
            else {},
            "run_history_counts": (payload.get("run_history") or {}).get("counts")
            if isinstance(payload.get("run_history"), dict)
            else {},
            "checkpoint_counts": (payload.get("checkpoints") or {}).get("counts")
            if isinstance(payload.get("checkpoints"), dict)
            else {},
            "parity_closeout_id": latest_closeout.get("closeout_id"),
            "parity_closeout_status": latest_closeout.get("status"),
            "parity_quieted_count": parity.get("quieted_issue_count"),
            "parity_changed_count": parity.get("changed_issue_count"),
            "policy_issue_count": (payload.get("policy") or {}).get("issue_count")
            if isinstance(payload.get("policy"), dict)
            else None,
            "runtime_issue_count": (payload.get("runtimes") or {}).get("issue_count")
            if isinstance(payload.get("runtimes"), dict)
            else None,
        }
    )


def pack_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pack_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-tool-pack"
    payload = _tool_pack_payload(target)
    payload.update({"pack_id": pack_id, "status": "built"})
    pack_dir = _packs_root(target) / pack_id
    entries, entry_errors = catalog_manage._read_tool_entries(paths.config_path(target))
    source_files: list[dict[str, Any]] = []
    source_root = pack_dir / "source-files"
    for entry in entries:
        source_path = entry.get("source_path")
        if not isinstance(source_path, str) or not source_path.strip():
            continue
        source = helpers._as_path(target, source_path)
        if source is None or not source.is_file():
            source_files.append(
                {"tool_id": entry.get("id"), "source_path": source_path, "packed": False, "reason": "source missing"}
            )
            continue
        rel = Path(source_path)
        if rel.is_absolute() or ".." in rel.parts:
            source_files.append(
                {
                    "tool_id": entry.get("id"),
                    "source_path": source_path,
                    "packed": False,
                    "reason": "source path is not repo-relative",
                }
            )
            continue
        dest = source_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        source_files.append(
            {
                "tool_id": entry.get("id"),
                "source_path": source_path,
                "packed": True,
                "pack_path": str(dest.relative_to(pack_dir)),
            }
        )
    portable_catalog = {"entries": entries, "entry_errors": entry_errors, "source_files": source_files}
    payload["portable_catalog"] = portable_catalog
    if entries:
        (pack_dir / "portable-tools.toml").write_text(helpers._format_tool_entries(entries))
    helpers._write_json(pack_dir / "tool-pack.json", payload)
    (pack_dir / "TOOL_PACK.md").write_text(
        f"# Tool Pack {pack_id}\n\n"
        f"- tools: {payload['tool_count']}\n"
        f"- issues: {payload['issue_count']}\n"
        f"- projections: {len(payload['projections'])}\n"
        f"- import: brigade tools pack import {pack_dir}\n"
    )
    payload["path"] = str(pack_dir)
    text_lines = [
        f"tool_pack: {pack_id}",
        f"path: {pack_dir}",
        f"tools: {payload['tool_count']}",
        f"issues: {payload['issue_count']}",
    ]
    return emit(payload, json_output, text_lines, 0)


def _tool_packs(target: Path) -> list[dict[str, Any]]:
    root = _packs_root(target)
    packs: list[dict[str, Any]] = []
    if root.is_dir():
        for path in root.iterdir():
            payload, error = helpers._read_json(path / "tool-pack.json")
            if error is None and isinstance(payload, dict):
                payload.setdefault("path", str(path))
                packs.append(payload)
    packs.sort(key=lambda item: str(item.get("created_at") or item.get("pack_id") or ""), reverse=True)
    return packs


def _sync_plan_summary(target: Path) -> dict[str, Any]:
    plan = projections._projection_plan_payload(target)
    blockers = [
        item
        for item in plan.get("projections", [])
        if isinstance(item, dict) and item.get("status") in {"conflicted", "unmanaged", "missing_source"}
    ]
    actions = [
        item
        for item in plan.get("projections", [])
        if isinstance(item, dict) and item.get("action") in {"create", "update", "conflict"}
    ]
    return {
        "valid": plan.get("valid"),
        "counts": plan.get("counts", {}),
        "projection_count": len(plan.get("projections", [])),
        "action_count": len(actions),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "dry_run_default": True,
        "add_only": True,
        "delete_supported": False,
    }


def _tool_pack_health(target: Path) -> dict[str, Any]:
    packs = _tool_packs(target)
    latest = packs[0] if packs else None
    current = _tool_pack_payload(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": constants.WARN,
                "name": "tool_pack_missing",
                "issue_type": "pack_missing",
                "detail": "no tool pack has been built",
            }
        )
    else:
        if latest.get("evidence_fingerprint") != current.get("evidence_fingerprint"):
            checks.append(
                {
                    "status": constants.WARN,
                    "name": "tool_pack_stale",
                    "issue_type": "pack_stale",
                    "detail": f"{latest.get('pack_id')} no longer matches current catalog evidence",
                }
            )
        path = latest.get("path")
        if path and not Path(str(path)).exists():
            checks.append(
                {
                    "status": constants.WARN,
                    "name": "tool_pack_missing_path",
                    "issue_type": "pack_missing_path",
                    "detail": str(path),
                }
            )
    return {
        "packs_path": str(_packs_root(target)),
        "pack_count": len(packs),
        "latest": latest,
        "current_fingerprint": current.get("evidence_fingerprint"),
        "issue_count": len(checks),
        "issues": checks,
        "top_issue": checks[0] if checks else None,
    }


def pack_list(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    packs = _tool_packs(target)[:limit]
    payload = {"target": str(target), "packs": packs, "pack_count": len(packs)}
    text_lines = [f"tool packs: {target}"]
    for pack in packs:
        text_lines.append(f"- {pack.get('pack_id')} tools={pack.get('tool_count')} issues={pack.get('issue_count')}")
    return emit(payload, json_output, text_lines, 0)


def _find_tool_pack(target: Path, pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    packs = _tool_packs(target)
    if pack_id == "latest":
        return (packs[0], None) if packs else (None, "tool pack not found: latest")
    matches = [pack for pack in packs if str(pack.get("pack_id") or "").startswith(pack_id)]
    if not matches:
        return None, f"tool pack not found: {pack_id}"
    if len(matches) > 1:
        return None, f"tool pack id is ambiguous: {pack_id}"
    return matches[0], None


def pack_show(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_tool_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "pack": pack}, indent=2, sort_keys=True))
        return 0
    print(f"tool_pack: {pack.get('pack_id')}")
    print(f"tools: {pack.get('tool_count')}")
    print(f"issues: {pack.get('issue_count')}")
    return 0


def pack_archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_tool_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source = Path(str(pack.get("path") or _packs_root(target) / str(pack.get("pack_id"))))
    destination = _packs_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived tool pack already exists: {destination}", file=sys.stderr)
        return 2
    source.rename(destination)
    payload = {
        "target": str(target),
        "pack_id": pack.get("pack_id"),
        "status": "archived",
        "archive_path": str(destination),
    }
    return emit(payload, json_output, [f"archived: {pack.get('pack_id')}", f"path: {destination}"], 0)


def pack_import(*, target: Path, pack: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack = pack.expanduser().resolve()
    manifest, manifest_error = helpers._read_json(pack / "tool-pack.json")
    if manifest_error is not None or not isinstance(manifest, dict):
        print(f"error: not a tool pack: {pack}", file=sys.stderr)
        return 2
    portable_catalog = manifest.get("portable_catalog") if isinstance(manifest.get("portable_catalog"), dict) else {}
    entries = portable_catalog.get("entries") if isinstance(portable_catalog.get("entries"), list) else []
    entries = [dict(entry) for entry in entries if isinstance(entry, dict)]
    if not entries and (pack / "portable-tools.toml").is_file():
        entries, _ = catalog_manage._read_tool_entries(pack / "portable-tools.toml")
    current_entries, errors = catalog_manage._read_tool_entries(paths.config_path(target))
    conflicts: list[dict[str, Any]] = []
    copied_sources: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    existing_by_id = {str(entry.get("id")): entry for entry in current_entries if entry.get("id")}
    for entry in entries:
        tool_id = str(entry.get("id") or "")
        if not tool_id:
            conflicts.append({"tool_id": "", "reason": "packed entry missing id"})
            continue
        existing_entry = existing_by_id.get(tool_id)
        if (
            existing_entry is not None
            and not force
            and helpers._stable_hash(existing_entry) != helpers._stable_hash(entry)
        ):
            conflicts.append(
                {
                    "tool_id": tool_id,
                    "reason": "tool id already exists with different definition",
                    "existing_source_path": existing_entry.get("source_path"),
                }
            )
        source_path = entry.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            rel = Path(source_path)
            if rel.is_absolute() or ".." in rel.parts:
                conflicts.append(
                    {"tool_id": tool_id, "reason": "source path is not repo-relative", "source_path": source_path}
                )
                continue
            packed_source = pack / "source-files" / rel
            target_source = target / rel
            if (
                packed_source.is_file()
                and target_source.is_file()
                and packed_source.read_bytes() != target_source.read_bytes()
                and not force
            ):
                conflicts.append(
                    {
                        "tool_id": tool_id,
                        "reason": "source file already exists with different content",
                        "source_path": source_path,
                    }
                )
    if errors:
        conflicts.append({"tool_id": None, "reason": "existing catalog errors", "errors": errors})
    if conflicts:
        payload = {
            "target": str(target),
            "pack": str(pack),
            "valid": False,
            "imported": [],
            "copied_sources": copied_sources,
            "skipped_existing": skipped_existing,
            "conflicts": conflicts,
        }
        text_lines = [f"conflict: {conflict.get('tool_id')} {conflict.get('reason')}" for conflict in conflicts]
        return emit(payload, json_output, text_lines, 1)
    merged_by_id = {str(entry.get("id")): dict(entry) for entry in current_entries if entry.get("id")}
    imported: list[dict[str, Any]] = []
    for entry in entries:
        tool_id = str(entry.get("id") or "")
        if not tool_id:
            continue
        source_path = entry.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            rel = Path(source_path)
            packed_source = pack / "source-files" / rel
            target_source = target / rel
            if packed_source.is_file():
                target_source.parent.mkdir(parents=True, exist_ok=True)
                if force or not target_source.exists() or packed_source.read_bytes() == target_source.read_bytes():
                    shutil.copy2(packed_source, target_source)
                    copied_sources.append(
                        {"tool_id": tool_id, "source_path": source_path, "target_path": str(target_source)}
                    )
        if tool_id in existing_by_id and not force:
            skipped_existing.append(
                {
                    "tool_id": tool_id,
                    "reason": "identical entry already exists",
                    "source_path": entry.get("source_path"),
                }
            )
            continue
        merged_by_id[tool_id] = dict(entry)
        imported.append({"tool_id": tool_id, "source_path": entry.get("source_path")})
    paths.config_path(target).parent.mkdir(parents=True, exist_ok=True)
    paths.config_path(target).write_text(helpers._format_tool_entries(list(merged_by_id.values())))
    result = apply_gitignore(target, catalog_manage._gitignore_selection(target))
    payload = {
        "target": str(target),
        "pack": str(pack),
        "pack_id": manifest.get("pack_id"),
        "valid": True,
        "imported_count": len(imported),
        "imported": imported,
        "copied_sources": copied_sources,
        "skipped_existing": skipped_existing,
        "skipped_existing_count": len(skipped_existing),
        "gitignore": result,
        "next_command": "brigade tools plan --target .",
    }
    text_lines = [
        f"tool_pack_import: {manifest.get('pack_id') or pack.name}",
        f"imported: {len(imported)}",
        "next_command: brigade tools plan --target .",
    ]
    return emit(payload, json_output, text_lines, 0)


def sync_plan(*, target: Path, tool_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = projections._projection_plan_payload(target, tool_id=tool_id)
    payload.update({"mode": "sync-plan", "dry_run_default": True, "delete_supported": False, "add_only": True})
    text_lines = [
        f"tools sync plan: {target}",
        f"projections: {len(payload['projections'])}",
        "dry_run_default: true",
        "delete_supported: false",
    ]
    return emit(payload, json_output, text_lines, 0 if payload["valid"] else 1)


def sync_apply(
    *,
    target: Path,
    tool_id: str | None = None,
    all_tools: bool = False,
    dry_run: bool = True,
    force: bool = False,
    json_output: bool = False,
) -> int:
    if tool_id is None and not all_tools:
        if not dry_run and force:
            print("error: pass <tool-id> or --all for write sync catalog_manage.apply", file=sys.stderr)
            return 2
        all_tools = True
    if not dry_run and not force:
        # Sync catalog_manage.apply is intentionally conservative: explicit non-dry-run writes require --force.
        dry_run = True
    return catalog_manage.apply(
        target=target,
        tool_id=tool_id,
        all_tools=all_tools,
        dry_run=dry_run,
        force=force,
        json_output=json_output,
    )
