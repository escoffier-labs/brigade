from __future__ import annotations

import json
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from .. import center_cmd, doctor as core_doctor, handoff_cmd, security_cmd, skills_cmd, tools_cmd
from ..install import install_selection
from ..selection import KNOWN_HARNESSES, WRITER_INBOXES, Selection, resolve_owner
from .guide import _steps, _validate_profile, plan_payload
from .health import doctor as operator_doctor, verify_harness


def init(
    *,
    target: Path,
    profile: str = "local-operator",
    handoff_inboxes: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    waive_public_release: bool = False,
    default_tools: bool = True,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        profile = _validate_profile(profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if dry_run:
        payload = plan_payload(target, profile=profile, handoff_inboxes=handoff_inboxes)
        payload["dry_run"] = True
        payload["waive_public_release"] = waive_public_release
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"operator bootstrap dry-run: {target}")
        print(f"profile: {payload['profile']}")
        for row in payload["steps"]:
            print(f"[{row['action']}] {row['id']}: {row['path']}")
        return 0

    results: list[dict[str, Any]] = []
    for step in _steps(target, profile=profile, handoff_inboxes=handoff_inboxes, default_tools=default_tools):
        path = step["path"]
        if path.exists() and not force and step["id"] != "handoff-sources":
            results.append({"id": step["id"], "path": str(path), "status": "skipped", "reason": "already exists"})
            continue
        kwargs = dict(step["kwargs"])
        kwargs.update({"target": target, "force": force})
        if step["id"] == "handoff-sources":
            kwargs["json_output"] = True
        output = StringIO()
        with redirect_stdout(output):
            rc = step["command"](**kwargs)
        output_text = output.getvalue().strip()
        result = {
            "id": step["id"],
            "path": str(path),
            "status": "written" if rc == 0 else "error",
            "return_code": rc,
            "output": output_text.splitlines(),
        }
        if step["id"] == "handoff-sources" and rc == 0:
            try:
                source_result = json.loads(output_text)
            except json.JSONDecodeError:
                source_result = None
            if isinstance(source_result, dict) and source_result.get("written") is False:
                result["status"] = "skipped"
                result["reason"] = "source coverage already current"
        results.append(result)
    post_actions = _post_init_actions(target, profile=profile, waive_public_release=waive_public_release)
    payload = {
        "target": str(target),
        "profile": profile,
        "results": results,
        "post_actions": post_actions,
        "written_count": sum(1 for row in results if row["status"] == "written"),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if _bootstrap_ok(results, post_actions) else 1
    print(f"operator bootstrap: {target}")
    print(f"profile: {profile}")
    for row in results:
        print(f"[{row['status']}] {row['id']}: {row['path']}")
    for row in post_actions:
        print(f"[{row['status']}] {row['id']}: {row.get('detail') or row.get('path') or ''}")
    return 0 if _bootstrap_ok(results, post_actions) else 1


def _bootstrap_ok(results: list[dict[str, Any]], post_actions: list[dict[str, Any]]) -> bool:
    return all(row.get("return_code", 0) == 0 for row in results if row["status"] != "skipped") and all(
        row.get("return_code", 0) == 0 for row in post_actions if row.get("status") != "skipped"
    )


def _post_init_actions(target: Path, *, profile: str, waive_public_release: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    actions.append(_ensure_initial_handoff_ingest_log(target))
    if profile == "internal-dogfood":
        output = StringIO()
        with redirect_stdout(output):
            rc = security_cmd.scan(
                target=target, output_dir=target / ".brigade" / "security" / "latest", json_output=False
            )
        actions.append(
            {
                "id": "security-scan",
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "path": str(target / ".brigade" / "security" / "latest"),
                "output": output.getvalue().strip().splitlines(),
            }
        )
    if waive_public_release:
        actions.append(_waive_public_release_readiness(target))
    return actions


def _ensure_initial_handoff_ingest_log(target: Path) -> dict[str, Any]:
    path = target / ".brigade" / "handoff-ingest" / "latest.log"
    if path.exists():
        return {"id": "handoff-ingest-log", "status": "skipped", "path": str(path), "reason": "already exists"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bootstrap: no handoff ingest runs yet\n")
    except OSError as exc:
        return {"id": "handoff-ingest-log", "status": "error", "path": str(path), "return_code": 1, "detail": str(exc)}
    return {"id": "handoff-ingest-log", "status": "written", "path": str(path), "return_code": 0}


def _waive_public_release_readiness(target: Path) -> dict[str, Any]:
    payload = center_cmd._readiness_payload(target)
    finding = next(
        (item for item in payload.get("findings", []) if item.get("name") == "missing_release_readiness"), None
    )
    if not isinstance(finding, dict):
        return {
            "id": "public-release-readiness-waiver",
            "status": "skipped",
            "reason": "missing_release_readiness not present",
        }
    output = StringIO()
    with redirect_stdout(output):
        rc = center_cmd.readiness_closeout(
            target=target,
            status="reviewed",
            reason="internal dogfood bootstrap: public release readiness is out of scope for local production use",
            waive_finding_ids=[str(finding["finding_id"])],
            json_output=False,
        )
    # readiness_closeout returns 1 when unrelated blockers remain, even though
    # the requested waiver was written. Keep bootstrap success tied to the write.
    return {
        "id": "public-release-readiness-waiver",
        "status": "written" if rc in {0, 1} else "error",
        "return_code": 0 if rc in {0, 1} else rc,
        "readiness_return_code": rc,
        "finding_id": finding.get("finding_id"),
        "output": output.getvalue().strip().splitlines(),
    }


def sync_tools(*, target: Path, dry_run: bool = False, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    defaults_output = StringIO()
    with redirect_stdout(defaults_output):
        defaults_rc = tools_cmd.defaults(
            target=target,
            dry_run=dry_run,
            force=force,
            update_gitignore=True,
            json_output=True,
        )
    try:
        defaults_payload = json.loads(defaults_output.getvalue() or "{}")
    except json.JSONDecodeError:
        defaults_payload = {
            "valid": False,
            "errors": ["tools defaults returned invalid JSON"],
            "output": defaults_output.getvalue().strip().splitlines(),
        }
        defaults_rc = 1
    if defaults_rc != 0:
        payload = {
            "target": str(target),
            "dry_run": dry_run,
            "force": force,
            "defaults": defaults_payload,
            "apply": {"applied_count": 0, "skipped_count": 0, "conflict_count": 0},
            "tool_health": {
                "valid": False,
                "tool_count": None,
                "issue_count": None,
                "top_issue": None,
                "sync_plan": None,
            },
            "projection_paths": [],
            "status": "warn",
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        print(f"operator sync-tools: {target}")
        print("defaults: failed")
        for error in defaults_payload.get("errors") or []:
            print(f"error: {error}")
        for conflict in defaults_payload.get("conflicts") or []:
            if isinstance(conflict, dict):
                print(f"- conflict: {conflict.get('tool_id')} {conflict.get('detail')}")
        return 1
    output = StringIO()
    with redirect_stdout(output):
        rc = tools_cmd.apply(target=target, all_tools=True, dry_run=dry_run, force=force, json_output=True)
    try:
        apply_payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        apply_payload = {
            "valid": False,
            "errors": ["tools apply returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    tool_health = tools_cmd.health(target)
    ok = rc == 0 and (dry_run or int(tool_health.get("issue_count") or 0) == 0)
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "defaults": defaults_payload,
        "apply": apply_payload,
        "tool_health": {
            "valid": tool_health.get("valid"),
            "tool_count": tool_health.get("tool_count"),
            "issue_count": tool_health.get("issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "sync_plan": tool_health.get("sync_plan"),
        },
        "projection_paths": [
            item.get("projection_path")
            for item in (apply_payload.get("applied") or []) + (apply_payload.get("skipped") or [])
            if isinstance(item, dict) and item.get("projection_path")
        ],
        "status": "ok" if ok else "warn",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "ok" else 1
    print(f"operator sync-tools: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    print(f"defaults_added: {len(defaults_payload.get('added') or [])}")
    print(f"defaults_updated: {len(defaults_payload.get('updated') or [])}")
    print(f"applied: {apply_payload.get('applied_count', 0)}")
    print(f"skipped: {apply_payload.get('skipped_count', 0)}")
    print(f"conflicts: {apply_payload.get('conflict_count', 0)}")
    print(f"tool_issues: {tool_health.get('issue_count')}")
    for item in apply_payload.get("applied") or []:
        if isinstance(item, dict):
            verb = "would_write" if dry_run else "wrote"
            print(f"- {verb}: {item.get('tool_id')} {item.get('harness')} {item.get('projection_path')}")
    for item in apply_payload.get("conflicts") or []:
        if isinstance(item, dict):
            print(f"- conflict: {item.get('tool_id')} {item.get('harness')} {item.get('detail')}")
    top = tool_health.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('tool_id')}/{top.get('issue_type')}: {top.get('detail')}")
    return 0 if payload["status"] == "ok" else 1


def sync_mcp(
    *,
    target: Path,
    write: bool = False,
    force: bool = False,
    prune: bool = False,
    adopt: bool = False,
    user_scope: bool = False,
    json_output: bool = False,
) -> int:
    """Validate the canonical MCP catalog, then sync it into each tool's config.

    Three phases: doctor (validate the catalog) -> sync (dry-run unless --write) ->
    summary. Kept separate from sync-tools because it writes shared config files the
    user co-owns, so it deserves its own auditable receipt. Dry-run by default.
    """
    from .. import mcp_cmd

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    doctor_rc, doctor_payload = _capture_json_call(mcp_cmd.doctor, target=target)
    if doctor_rc != 0:
        payload = {
            "target": str(target),
            "write": write,
            "doctor": doctor_payload,
            "sync": None,
            "status": "warn",
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"operator sync-mcp: {target}")
            print("doctor: failed")
            for issue in doctor_payload.get("issues") or []:
                if isinstance(issue, dict) and issue.get("severity") == "error":
                    print(f"error: {issue.get('message')}")
        return 1
    sync_rc, sync_payload = _capture_json_call(
        mcp_cmd.sync, target=target, write=write, force=force, prune=prune, adopt=adopt, user_scope=user_scope
    )
    counts = sync_payload.get("counts") or {}
    ok = sync_rc == 0
    payload = {
        "target": str(target),
        "write": write,
        "doctor": {"valid": doctor_payload.get("valid"), "server_count": doctor_payload.get("server_count")},
        "sync": sync_payload,
        "status": "ok" if ok else "warn",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    print(f"operator sync-mcp: {target}")
    print(f"write: {write}")
    print(f"servers: {doctor_payload.get('server_count')}")
    print(f"created: {counts.get('create', 0)}")
    print(f"updated: {counts.get('update', 0)}")
    print(f"conflicts: {counts.get('conflict', 0)}")
    print(f"removed: {counts.get('remove', 0)}")
    for path in sync_payload.get("files_written") or []:
        print(f"- wrote: {path}")
    return 0 if ok else 1


def _capture_json_call(func, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs, json_output=True)
    try:
        payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "errors": [f"{getattr(func, '__name__', 'command')} returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    return rc, payload


def _capture_text_call(func, **kwargs: Any) -> tuple[int, list[str]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs)
    return rc, output.getvalue().strip().splitlines()


def _parse_harnesses(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return ["codex"]
    if value.strip() == "none":
        return []
    harnesses = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in harnesses if item not in KNOWN_HARNESSES]
    if unknown:
        raise ValueError(f"unknown harness: {', '.join(unknown)}")
    return list(dict.fromkeys(harnesses))


def quickstart(
    *,
    target: Path,
    depth: str = "repo",
    harnesses: str | None = "codex",
    owner: str | None = None,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    full: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if depth not in {"repo", "workspace"}:
        print("error: --depth must be repo or workspace", file=sys.stderr)
        return 2
    includes = ["repo-extras"] if full and depth == "repo" else []
    try:
        selected_harnesses = _parse_harnesses(harnesses)
        memory_owner = resolve_owner(selected_harnesses, override=owner)
        selection = Selection(depth=depth, harnesses=selected_harnesses, owner=memory_owner, includes=includes)
        selection.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run_portable_defaults = full or depth == "workspace"
    steps: list[dict[str, Any]] = []
    install_rc, install_output = _capture_text_call(
        install_selection,
        target=target,
        selection=selection,
        force=force,
        dry_run=dry_run,
        allow_home=False,
    )
    install_status = "planned" if dry_run and install_rc == 0 else "ok" if install_rc == 0 else "error"
    steps.append({"id": "brigade-init", "status": install_status, "return_code": install_rc, "output": install_output})
    if install_rc != 0:
        payload = {
            "target": str(target),
            "depth": depth,
            "harnesses": selected_harnesses,
            "owner": memory_owner,
            "owner_override": owner is not None,
            "dry_run": dry_run,
            "force": force,
            "steps": steps,
            "status": "blocked",
            "next_commands": [
                f"brigade init --target {target} --depth {depth} --harnesses {','.join(selected_harnesses) or 'none'} --force"
            ],
            "local_only_notes": _quickstart_local_notes(),
        }
        payload["issue_report"] = _quickstart_issue_report(payload)
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        _print_quickstart(payload)
        return 1

    selected_inboxes = [WRITER_INBOXES[harness] for harness in selected_harnesses if harness in WRITER_INBOXES]
    init_rc, init_payload = _capture_json_call(
        init,
        target=target,
        profile="local-operator",
        handoff_inboxes=selected_inboxes,
        force=force,
        dry_run=dry_run,
        default_tools=run_portable_defaults,
    )
    init_status = "planned" if dry_run and init_rc == 0 else "ok" if init_rc == 0 else "error"
    steps.append({"id": "operator-init", "status": init_status, "return_code": init_rc, "payload": init_payload})

    run_portable = full or depth == "workspace" or tool_pack is not None or skill_pack is not None
    if run_portable:
        portable_rc, portable_payload = _capture_json_call(
            bootstrap_portable,
            target=target,
            tool_pack=tool_pack,
            skill_pack=skill_pack,
            dry_run=dry_run,
            force=force,
        )
        portable_status = "planned" if dry_run and portable_rc == 0 else "ok" if portable_rc == 0 else "error"
        steps.append(
            {
                "id": "portable-bootstrap",
                "status": portable_status,
                "return_code": portable_rc,
                "payload": portable_payload,
            }
        )
    else:
        portable_rc = 0
        steps.append(
            {
                "id": "portable-bootstrap",
                "status": "skipped",
                "return_code": 0,
                "reason": "minimal install; pass --full to project the default tool packs",
            }
        )

    steps.append(_quickstart_mcp_onramp(target, dry_run=dry_run, force=force))
    steps.append(_quickstart_dogfood(target, dry_run=dry_run, force=force))

    if dry_run:
        for harness in selected_harnesses:
            if harness in WRITER_INBOXES:
                steps.append(
                    {
                        "id": f"verify-{harness}",
                        "status": "planned",
                        "return_code": 0,
                        "next_command": f"brigade operator verify-harness --harness {harness} --target {target}",
                    }
                )
    else:
        for harness in selected_harnesses:
            if harness not in WRITER_INBOXES:
                steps.append(
                    {
                        "id": f"verify-{harness}",
                        "status": "skipped",
                        "reason": "no Brigade handoff writer inbox for this harness",
                    }
                )
                continue
            verify_rc, verify_payload = _capture_json_call(verify_harness, target=target, harness=harness)
            step = {
                "id": f"verify-{harness}",
                "status": "ok" if verify_rc == 0 else "warn",
                "return_code": verify_rc,
                "payload": verify_payload,
            }
            advisories = int(verify_payload.get("warning_count") or 0) if isinstance(verify_payload, dict) else 0
            if verify_rc == 0 and advisories:
                step["advisory_count"] = advisories
                step["advisory_next_command"] = f"brigade operator verify-harness --harness {harness} --target ."
            steps.append(step)

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") not in {"skipped", "planned"})
    if dry_run:
        ok = install_rc == 0 and init_rc == 0 and portable_rc == 0
    payload = {
        "target": str(target),
        "depth": depth,
        "harnesses": selected_harnesses,
        "owner": memory_owner,
        "owner_override": owner is not None,
        "dry_run": dry_run,
        "force": force,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": _quickstart_next_commands(selected_harnesses, dry_run=dry_run),
        "local_only_notes": _quickstart_local_notes(),
    }
    payload["issue_report"] = _quickstart_issue_report(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    _print_quickstart(payload)
    return 0 if ok else 1


def _quickstart_mcp_onramp(target: Path, *, dry_run: bool, force: bool) -> dict[str, Any]:
    """Scaffold the canonical MCP catalog and preview the projection plan.

    The README leads with MCP/tool sync, so quickstart must put that feature on
    the golden path. This writes only `.brigade/mcp.json` (local owned state) and
    runs `mcp plan` as a dry-run summary. It never writes harness MCP configs,
    matching the dry-run-by-default, no-auto-write contract for shared config.
    """
    from .. import mcp_cmd

    if dry_run:
        catalog = target / ".brigade" / "mcp.json"
        return {
            "id": "mcp-init",
            "status": "planned",
            "return_code": 0,
            "planned_writes": [
                {"path": str(catalog), "action": "skip" if catalog.exists() else "write"},
            ],
            "next_command": "brigade mcp init --target .",
        }
    init_rc, init_payload = _capture_json_call(mcp_cmd.init, target=target, force=force)
    # init returns 3 when the catalog already exists; that is a benign skip here.
    if init_rc == 3:
        step: dict[str, Any] = {"id": "mcp-init", "status": "skipped", "return_code": 0, "payload": init_payload}
    else:
        step = {
            "id": "mcp-init",
            "status": "ok" if init_rc == 0 else "error",
            "return_code": init_rc,
            "payload": init_payload,
        }
    if init_rc in {0, 3}:
        _, plan_payload = _capture_json_call(mcp_cmd.plan, target=target)
        if isinstance(plan_payload, dict):
            step["plan"] = {"counts": plan_payload.get("counts"), "server_count": len(plan_payload.get("items") or [])}
    return step


def _quickstart_dogfood(target: Path, *, dry_run: bool, force: bool) -> dict[str, Any]:
    """Arm the work/dogfood loop so a new repo captures runs from day one.

    Without a dogfood config the `work` station is wired but dormant: `work
    status` reports `dogfood: not ready` and no runs are ever captured, which is
    the single most common reason a freshly set-up repo never feeds its outcome
    ledger. This writes only `.brigade/dogfood.toml` (local owned state) and
    starts no runs, matching the no-auto-run contract of the other steps.
    """
    from .. import dogfood_cmd

    if dry_run:
        return {
            "id": "dogfood-init",
            "status": "planned",
            "return_code": 0,
            "next_command": "brigade dogfood init --target .",
        }
    if dogfood_cmd.config_path(target).exists() and not force:
        # A dogfood config already exists; keep it rather than clobber local edits.
        return {"id": "dogfood-init", "status": "skipped", "return_code": 0}
    init_rc, init_output = _capture_text_call(dogfood_cmd.init, target=target, force=force)
    return {
        "id": "dogfood-init",
        "status": "ok" if init_rc == 0 else "error",
        "return_code": init_rc,
        "output": init_output,
    }


def _quickstart_next_commands(harnesses: list[str], *, dry_run: bool) -> list[str]:
    if dry_run:
        return ["rerun without --dry-run after reviewing planned writes"]
    commands = [
        "brigade operator doctor --target . --profile local-operator",
        "brigade tools list --target .",
        "brigade skills doctor --target .",
        "brigade mcp init --target .",
        "brigade mcp sync --write --target .",
        "brigade security scan --target . --output-dir .brigade/security/latest",
    ]
    commands.extend(
        f"brigade operator verify-harness --target . --harness {harness}"
        for harness in harnesses
        if harness in WRITER_INBOXES
    )
    return commands


def _quickstart_local_notes() -> list[str]:
    return [
        ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
        "Generated harness projections and handoff inboxes are local ignored state.",
        "Brigade does not start daemons, activate hooks (the pre-push hook file ships inactive), publish, push, tag, or mutate remotes from quickstart.",
    ]


def _quickstart_issue_report(payload: dict[str, Any]) -> dict[str, Any]:
    from .. import __version__

    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    step_summaries = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        item = {
            "id": step.get("id"),
            "status": step.get("status"),
            "return_code": step.get("return_code"),
        }
        payload_obj = step.get("payload")
        if isinstance(payload_obj, dict):
            item["payload_status"] = payload_obj.get("status") or payload_obj.get("ready")
            top_issue = payload_obj.get("top_issue")
            if isinstance(top_issue, dict):
                item["top_issue"] = {
                    "name": top_issue.get("name"),
                    "detail": top_issue.get("detail"),
                }
        step_summaries.append(item)
    return {
        "brigade_version": __version__,
        "status": payload.get("status"),
        "depth": payload.get("depth"),
        "harnesses": payload.get("harnesses"),
        "owner": payload.get("owner"),
        "dry_run": payload.get("dry_run"),
        "force": payload.get("force"),
        "steps": step_summaries,
        "next_commands": payload.get("next_commands") or [],
        "github_issue_url": "https://github.com/escoffier-labs/brigade/issues/new/choose",
        "privacy_note": "Review before sharing. Do not paste tokens, private hostnames, or unredacted absolute paths.",
    }


def _planned_write_lines(step: dict[str, Any]) -> list[str]:
    """File-by-file preview lines for one dry-run quickstart step."""
    step_id = step.get("id")
    if step_id == "brigade-init":
        return [line.strip() for line in step.get("output") or [] if line.startswith(("  dir", "  file"))]
    if step.get("planned_writes"):
        return [f"{row.get('action', 'write'):<5} {row.get('path')}" for row in step["planned_writes"]]
    payload = step.get("payload")
    if not isinstance(payload, dict):
        return []
    if step_id == "operator-init":
        return [f"{row.get('action', 'write'):<5} {row.get('path')}" for row in payload.get("steps") or []]
    if step_id == "portable-bootstrap":
        lines: list[str] = []
        for sub in payload.get("steps") or []:
            sub_payload = sub.get("payload") if isinstance(sub, dict) else None
            if not isinstance(sub_payload, dict):
                continue
            for path in sub_payload.get("projection_paths") or []:
                lines.append(f"write {path}")
        return lines
    return []


def _print_quickstart(payload: dict[str, Any]) -> None:
    print(f"operator quickstart: {payload['target']}")
    print(f"depth: {payload['depth']}")
    print(f"harnesses: {','.join(payload['harnesses']) or 'none'}")
    owner_note = "" if payload.get("owner_override") else " (auto-selected; override with --owner)"
    print(f"owner: {payload['owner']}{owner_note}")
    print(f"dry_run: {payload['dry_run']}")
    for step in payload["steps"]:
        advisory = (
            f" ({step['advisory_count']} advisory, see {step['advisory_next_command']})"
            if step.get("advisory_count")
            else ""
        )
        print(f"[{step.get('status')}] {step.get('id')}{advisory}")
        if payload.get("dry_run"):
            for line in _planned_write_lines(step):
                print(f"  {line}")
    print(f"status: {payload['status']}")
    print("next:")
    for command in payload["next_commands"]:
        print(f"- {command}")
    print("issues: https://github.com/escoffier-labs/brigade/issues/new/choose")


def bootstrap_portable(
    *,
    target: Path,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    steps: list[dict[str, Any]] = []
    if tool_pack is not None:
        if dry_run:
            steps.append({"id": "tools-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(tool_pack)})
        else:
            rc, payload = _capture_json_call(tools_cmd.pack_import, target=target, pack=tool_pack, force=force)
            steps.append(
                {
                    "id": "tools-pack-import",
                    "status": "ok" if rc == 0 else "error",
                    "return_code": rc,
                    "pack": str(tool_pack),
                    "payload": payload,
                }
            )
    if skill_pack is not None:
        if dry_run:
            steps.append(
                {"id": "skills-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(skill_pack)}
            )
        else:
            rc, payload = _capture_json_call(skills_cmd.pack_import, target=target, pack=skill_pack, force=force)
            steps.append(
                {
                    "id": "skills-pack-import",
                    "status": "ok" if rc == 0 else "error",
                    "return_code": rc,
                    "pack": str(skill_pack),
                    "payload": payload,
                }
            )

    sync_rc, sync_payload = _capture_json_call(sync_tools, target=target, dry_run=dry_run, force=force)
    sync_status = "ok" if sync_rc == 0 else "error"
    if dry_run and isinstance(sync_payload.get("defaults"), dict) and sync_payload["defaults"].get("valid"):
        sync_rc = 0
        sync_status = "planned"
    steps.append({"id": "operator-sync-tools", "status": sync_status, "return_code": sync_rc, "payload": sync_payload})
    if not dry_run:
        tools_rc, tools_payload = _capture_json_call(tools_cmd.doctor, target=target)
        steps.append(
            {
                "id": "tools-doctor",
                "status": "ok" if tools_rc == 0 else "error",
                "return_code": tools_rc,
                "payload": tools_payload,
            }
        )
        skills_rc, skills_payload = _capture_json_call(skills_cmd.doctor, target=target)
        steps.append(
            {
                "id": "skills-doctor",
                "status": "ok" if skills_rc == 0 else "error",
                "return_code": skills_rc,
                "payload": skills_payload,
            }
        )

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") != "skipped")
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "tool_pack": str(tool_pack) if tool_pack is not None else None,
        "skill_pack": str(skill_pack) if skill_pack is not None else None,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": [
            "brigade tools list --target .",
            "brigade skills doctor --target .",
            "brigade security scan --target . --output-dir .brigade/security/latest",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    print(f"operator bootstrap-portable: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    for step in steps:
        print(f"[{step['status']}] {step['id']}")
    print(f"status: {payload['status']}")
    if ok:
        print("next:")
        for command in payload["next_commands"]:
            print(f"- {command}")
    return 0 if ok else 1


def _surface_issue_count(payload: dict[str, Any]) -> int | None:
    for key in ("blocking_issue_count", "issue_count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("failed"), int):
        return summary["failed"]
    return None


CHECKUP_DEFAULT_SURFACES = ("doctor", "operator", "handoff", "tools", "skills", "security")
CHECKUP_EVIDENCE_SURFACES = ("work", "graph", "ledger")
CHECKUP_SURFACE_NAMES = (*CHECKUP_DEFAULT_SURFACES, *CHECKUP_EVIDENCE_SURFACES)
CHECKUP_PRESETS = {"evidence-loop": CHECKUP_EVIDENCE_SURFACES}


def checkup_catalog_payload() -> dict[str, Any]:
    return {
        "surface_names": list(CHECKUP_SURFACE_NAMES),
        "default_surfaces": list(CHECKUP_DEFAULT_SURFACES),
        "presets": {name: list(values) for name, values in CHECKUP_PRESETS.items()},
    }


def _resolve_checkup_surfaces(surfaces: list[str] | None, preset: str | None) -> tuple[list[str], list[str], bool]:
    if surfaces and preset:
        raise ValueError("--surface and --preset cannot be combined")
    if preset is not None:
        if preset not in CHECKUP_PRESETS:
            raise ValueError(f"unknown checkup preset: {preset}")
        selected = list(CHECKUP_PRESETS[preset])
        skipped = [name for name in CHECKUP_SURFACE_NAMES if name not in selected]
        return selected, skipped, True
    if surfaces:
        selected = list(dict.fromkeys(surfaces))
        unknown = [name for name in selected if name not in CHECKUP_SURFACE_NAMES]
        if unknown:
            raise ValueError(f"unknown checkup surface: {', '.join(unknown)}")
        skipped = [name for name in CHECKUP_SURFACE_NAMES if name not in selected]
        return selected, skipped, True
    return list(CHECKUP_DEFAULT_SURFACES), list(CHECKUP_EVIDENCE_SURFACES), False


def _print_checkup_surface(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(str(payload.get("summary") or payload.get("status") or "checkup surface unavailable"))


def _checkup_work(*, target: Path, json_output: bool = False) -> int:
    """Check work receipt integrity and whether outcome capture is keeping up."""
    from .. import outcome_cmd, receipts_cmd
    from ..work_cmd import verification

    receipt_health = receipts_cmd.verify_payload(target)
    artifacts = [
        row
        for row in receipt_health.get("artifacts", [])
        if isinstance(row, dict) and str(row.get("artifact_type") or "").startswith("work-verify-")
    ]
    integrity_failures = [
        row
        for row in artifacts
        if row.get("status") in {receipts_cmd.MISMATCH, receipts_cmd.MISSING, receipts_cmd.SIGNATURE_MISMATCH}
    ]
    receipts = verification._verify_receipts(target)
    latest = receipts[0] if receipts else None
    outcomes = outcome_cmd.health(target)
    outcome_issue_count = int(outcomes.get("issue_count") or 0)
    missing_receipt_issue = int(latest is None and outcome_issue_count == 0)
    issue_count = len(integrity_failures) + outcome_issue_count + missing_receipt_issue
    ready = issue_count == 0
    payload = {
        "status": "ok" if ready else "warn",
        "ready": ready,
        "summary": (
            "work receipts and outcome capture are healthy"
            if ready
            else f"work evidence loop has {issue_count} issue(s)"
        ),
        "issue_count": issue_count,
        "receipt_count": len(receipts),
        "latest_receipt": (
            {
                "run_id": latest.get("run_id"),
                "status": latest.get("status"),
                "started_at": latest.get("started_at"),
            }
            if latest
            else None
        ),
        "receipt_integrity": {
            "artifact_count": len(artifacts),
            "failure_count": len(integrity_failures),
        },
        "outcome_capture": outcomes,
        "next_command": (
            "brigade receipts verify --target ."
            if integrity_failures
            else "brigade work verify run --target . --command '<check>' --capture brigade-work"
            if latest is None
            else "brigade outcome capture <skill-or-card-id> --run-id latest"
            if outcome_issue_count
            else None
        ),
    }
    _print_checkup_surface(payload, json_output=json_output)
    return 0 if ready else 1


def _checkup_graph(*, target: Path, json_output: bool = False) -> int:
    """Check live GraphTrail health and the latest work receipt's graph delta."""
    from .. import search_cmd
    from ..work_cmd import verification

    station = search_cmd.status_payload(target)
    tools = station.get("tools") if isinstance(station.get("tools"), dict) else {}
    graphtrail = tools.get("graphtrail") if isinstance(tools.get("graphtrail"), dict) else {}
    latest = verification._latest_verify_receipt(target)
    delta = latest.get("code_graph_delta") if isinstance(latest, dict) else None
    delta = delta if isinstance(delta, dict) else {}
    graph_ready = (
        graphtrail.get("installed") is True
        and graphtrail.get("db_present") is True
        and graphtrail.get("health") == "ok"
    )
    delta_ready = delta.get("status") == "ok" and delta.get("stale_graph_used") is not True
    issue_count = int(not graph_ready) + int(not delta_ready)
    ready = issue_count == 0
    payload = {
        "status": "ok" if ready else "warn",
        "ready": ready,
        "summary": (
            "GraphTrail and the latest work receipt delta are healthy"
            if ready
            else f"GraphTrail evidence has {issue_count} issue(s)"
        ),
        "issue_count": issue_count,
        "graphtrail": {
            "installed": graphtrail.get("installed"),
            "db_present": graphtrail.get("db_present"),
            "health": graphtrail.get("health"),
            "summary": graphtrail.get("summary"),
        },
        "latest_receipt": latest.get("run_id") if isinstance(latest, dict) else None,
        "code_graph_delta": delta or None,
        "next_command": (
            "brigade search doctor --target ."
            if not graph_ready
            else "brigade work verify run --target . --command '<check>' --capture brigade-work"
        )
        if not ready
        else None,
    }
    _print_checkup_surface(payload, json_output=json_output)
    return 0 if ready else 1


def _checkup_ledger(*, target: Path, json_output: bool = False) -> int:
    """Check MiseLedger status and unimported Brigade work receipt evidence."""
    from .. import evidence_cmd, receipts_cmd
    from ..work_cmd import verification

    station = evidence_cmd.status_payload(target, include_doctor=False)
    receipts = verification._verify_receipts(target)
    cursor_hashes = receipts_cmd._read_miseledger_cursor_hashes(target)
    receipt_hashes: set[str] = set()
    for receipt in receipts:
        receipt_dir = receipt.get("path")
        path = Path(receipt_dir) / "receipt.json" if isinstance(receipt_dir, str) else target / "missing-receipt.json"
        digest, _source = receipts_cmd._receipt_hash(receipt, path)
        receipt_hashes.add(digest)
    pending_hashes = receipt_hashes - cursor_hashes
    station_ready = station.get("installed") is True and station.get("health") == "ok"
    import_ready = not receipts or (station.get("export_cursor_present") is True and not pending_hashes)
    issue_count = int(not station_ready) + int(not import_ready)
    ready = issue_count == 0
    payload = {
        "status": "ok" if ready else "warn",
        "ready": ready,
        "summary": (
            "MiseLedger is healthy and all work receipts are imported"
            if ready
            else f"MiseLedger import health has {issue_count} issue(s)"
        ),
        "issue_count": issue_count,
        "miseledger": {
            "installed": station.get("installed"),
            "health": station.get("health"),
            "summary": station.get("summary"),
            "export_cursor_present": station.get("export_cursor_present"),
        },
        "work_receipt_count": len(receipts),
        "pending_work_receipt_count": len(pending_hashes),
        "next_command": (
            "brigade evidence doctor --target ."
            if not station_ready
            else "brigade receipts export miseledger --target . --new-only --import"
        )
        if not ready
        else None,
    }
    _print_checkup_surface(payload, json_output=json_output)
    return 0 if ready else 1


def _loop_stations_payload(target: Path) -> dict[str, Any]:
    """Report GraphTrail / MiseLedger / context-eval loop health (informational).

    Optional stations do not block checkup readiness. The point is one glance:
    graph ok, ledger ok, last evidence brief hit rate.
    """
    from .. import context_cmd, evidence_brief

    graph_bin = context_cmd._graphtrail_bin()
    graph_db = target / ".graphtrail" / "graphtrail.db"
    if graph_bin and graph_db.is_file():
        graph = {
            "ok": True,
            "status": "ok",
            "detail": "graphtrail on PATH and .graphtrail/graphtrail.db present",
        }
    elif graph_bin:
        graph = {
            "ok": False,
            "status": "missing-db",
            "detail": "graphtrail on PATH; run `graphtrail sync` to build .graphtrail/graphtrail.db",
        }
    else:
        graph = {
            "ok": False,
            "status": "missing-bin",
            "detail": "graphtrail not on PATH (run `brigade setup`; Cargo is one-release compatibility only)",
        }

    ledger_bin = evidence_brief._miseledger_bin()
    if ledger_bin:
        ledger = {
            "ok": True,
            "status": "ok",
            "detail": "miseledger on PATH",
        }
    else:
        ledger = {
            "ok": False,
            "status": "missing-bin",
            "detail": "miseledger not on PATH (run `brigade setup`; go install is one-release compatibility only)",
        }

    rates: list[float] = []
    runs_root = target / ".brigade" / "runs"
    if runs_root.is_dir():
        run_rows: list[tuple[str, float]] = []
        for child in runs_root.iterdir():
            if not child.is_dir():
                continue
            run_json = child / "run.json"
            if not run_json.is_file():
                continue
            try:
                payload = json.loads(run_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            context_eval = payload.get("context_eval")
            if not isinstance(context_eval, dict):
                continue
            rate = context_eval.get("brief_hit_rate")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                continue
            stamp = str(payload.get("started_at") or child.name)
            run_rows.append((stamp, float(rate)))
        run_rows.sort(key=lambda item: item[0], reverse=True)
        rates = [rate for _, rate in run_rows]

    last_rate = rates[0] if rates else None
    mean_rate = round(sum(rates) / len(rates), 3) if rates else None
    return {
        "graph": graph,
        "ledger": ledger,
        "context_eval": {
            "last_brief_hit_rate": last_rate,
            "mean_brief_hit_rate": mean_rate,
            "sample_count": len(rates),
            "detail": (
                f"last={last_rate:.3f} mean={mean_rate:.3f} (n={len(rates)})"
                if rates and last_rate is not None and mean_rate is not None
                else "no context_eval on recent run receipts"
            ),
            "ok": bool(rates),
            "status": "ok" if rates else "none",
        },
    }


def checkup_payload(
    target: Path,
    *,
    profile: str = "internal-dogfood",
    surfaces: list[str] | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """Run every read-only first-run doctor once and roll the verdicts up.

    The first-10-minutes path has an operator run several separate doctors by
    hand. checkup runs them in one pass, captures each one's JSON, and reports a
    single ready/blocking verdict. Each surface's exit code is the source of
    truth for readiness; the issue count is informational. Nothing here writes
    files (security scan and verify-harness are deliberately excluded).

    Also reports optional GraphTrail/MiseLedger loop health (graph / ledger /
    last evidence brief hit rate). Those stations never block readiness.
    """
    target = target.expanduser().resolve()
    selected, skipped, scoped = _resolve_checkup_surfaces(surfaces, preset)
    spec = [
        ("doctor", "brigade doctor --target .", core_doctor.run, {"target": target}),
        ("operator", "brigade operator doctor --target .", operator_doctor, {"target": target, "profile": profile}),
        ("handoff", "brigade handoff doctor --target .", handoff_cmd.doctor, {"target": target}),
        ("tools", "brigade tools doctor --target .", tools_cmd.doctor, {"target": target}),
        ("skills", "brigade skills doctor --target .", skills_cmd.doctor, {"target": target}),
        ("security", "brigade security doctor --target .", security_cmd.doctor, {"target": target}),
        ("work", "brigade operator checkup --target . --surface work", _checkup_work, {"target": target}),
        ("graph", "brigade operator checkup --target . --surface graph", _checkup_graph, {"target": target}),
        ("ledger", "brigade operator checkup --target . --surface ledger", _checkup_ledger, {"target": target}),
    ]
    spec_by_name = {row[0]: row for row in spec}
    surface_results: list[dict[str, Any]] = []
    blocking = 0
    for selected_name in selected:
        if selected_name not in spec_by_name:
            raise ValueError(f"checkup surface is not implemented: {selected_name}")
        name, command, func, kwargs = spec_by_name[selected_name]
        started = time.perf_counter()
        rc, payload = _capture_json_call(func, **kwargs)
        elapsed = round(time.perf_counter() - started, 3)
        surface_ready = rc == 0
        if not surface_ready:
            blocking += 1
        result: dict[str, Any] = {
            "name": name,
            "command": command,
            "ready": surface_ready,
            "exit_code": rc,
            "issue_count": _surface_issue_count(payload),
            "elapsed_seconds": elapsed,
        }
        if name in CHECKUP_EVIDENCE_SURFACES:
            result["details"] = payload
        surface_results.append(result)
    selected_ready = blocking == 0
    next_command = next((surface["command"] for surface in surface_results if not surface["ready"]), None)
    return {
        "target": str(target),
        "profile": profile,
        "ready": selected_ready,
        "selected_ready": selected_ready,
        "overall_ready": None if scoped else selected_ready,
        "selected_surfaces": selected,
        "skipped_surfaces": skipped,
        "blocking_surface_count": blocking,
        "surfaces": surface_results,
        "next_command": next_command,
        "loop": {} if scoped else _loop_stations_payload(target),
    }


def checkup(
    *,
    target: Path,
    profile: str = "internal-dogfood",
    surfaces: list[str] | None = None,
    preset: str | None = None,
    list_surfaces: bool = False,
    json_output: bool = False,
) -> int:
    if list_surfaces and (surfaces or preset):
        print("error: --list-surfaces cannot be combined with --surface or --preset", file=sys.stderr)
        return 2
    if list_surfaces:
        payload = checkup_catalog_payload()
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("operator checkup surfaces:")
            for name in payload["surface_names"]:
                print(f"- {name}")
            print("presets:")
            for name, values in payload["presets"].items():
                print(f"- {name}: {', '.join(values)}")
        return 0
    try:
        payload = checkup_payload(target, profile=profile, surfaces=surfaces, preset=preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1

    print(f"operator checkup: {payload['target']}")
    print(f"profile: {payload['profile']}")
    for surface in payload["surfaces"]:
        mark = "ok" if surface["ready"] else "fail"
        count = surface["issue_count"]
        suffix = f" ({count} issue{'s' if count != 1 else ''})" if isinstance(count, int) and count else ""
        print(f"  [{mark}] {surface['name']}{suffix}")
    loop = payload.get("loop") if isinstance(payload.get("loop"), dict) else {}
    if loop:
        print("loop:")
        for key in ("graph", "ledger", "context_eval"):
            row = loop.get(key)
            if not isinstance(row, dict):
                continue
            mark = "ok" if row.get("ok") else "warn"
            detail = row.get("detail") or row.get("status") or ""
            label = "brief_hit_rate" if key == "context_eval" else key
            print(f"  [{mark}] {label}: {detail}")
    if payload["overall_ready"] is None:
        print(f"selected_ready: {'yes' if payload['selected_ready'] else 'no'}")
        print("overall_ready: not evaluated")
    else:
        print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"blocking_surfaces: {payload['blocking_surface_count']}")
    if payload["next_command"]:
        print(f"next: {payload['next_command']}")
    return 0 if payload["ready"] else 1
