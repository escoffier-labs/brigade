from __future__ import annotations

import json
import sys
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
    for step in _steps(target, profile=profile, handoff_inboxes=handoff_inboxes):
        path = step["path"]
        if path.exists() and not force:
            results.append({"id": step["id"], "path": str(path), "status": "skipped", "reason": "already exists"})
            continue
        kwargs = dict(step["kwargs"])
        kwargs.update({"target": target, "force": force})
        output = StringIO()
        with redirect_stdout(output):
            rc = step["command"](**kwargs)
        results.append(
            {
                "id": step["id"],
                "path": str(path),
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "output": output.getvalue().strip().splitlines(),
            }
        )
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
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if depth not in {"repo", "workspace"}:
        print("error: --depth must be repo or workspace", file=sys.stderr)
        return 2
    try:
        selected_harnesses = _parse_harnesses(harnesses)
        memory_owner = resolve_owner(selected_harnesses, override=owner)
        selection = Selection(depth=depth, harnesses=selected_harnesses, owner=memory_owner, includes=[])
        selection.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

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
        init, target=target, profile="local-operator", handoff_inboxes=selected_inboxes, force=force, dry_run=dry_run
    )
    init_status = "planned" if dry_run and init_rc == 0 else "ok" if init_rc == 0 else "error"
    steps.append({"id": "operator-init", "status": init_status, "return_code": init_rc, "payload": init_payload})

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
        {"id": "portable-bootstrap", "status": portable_status, "return_code": portable_rc, "payload": portable_payload}
    )

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


def _quickstart_next_commands(harnesses: list[str], *, dry_run: bool) -> list[str]:
    if dry_run:
        return ["rerun without --dry-run after reviewing planned writes"]
    commands = [
        "brigade operator doctor --target . --profile local-operator",
        "brigade tools list --target .",
        "brigade skills doctor --target .",
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


def checkup_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    """Run every read-only first-run doctor once and roll the verdicts up.

    The first-10-minutes path has an operator run several separate doctors by
    hand. checkup runs them in one pass, captures each one's JSON, and reports a
    single ready/blocking verdict. Each surface's exit code is the source of
    truth for readiness; the issue count is informational. Nothing here writes
    files (security scan and verify-harness are deliberately excluded).
    """
    target = target.expanduser().resolve()
    spec = [
        ("doctor", "brigade doctor --target .", core_doctor.run, {"target": target}),
        ("operator", "brigade operator doctor --target .", operator_doctor, {"target": target, "profile": profile}),
        ("handoff", "brigade handoff doctor --target .", handoff_cmd.doctor, {"target": target}),
        ("tools", "brigade tools doctor --target .", tools_cmd.doctor, {"target": target}),
        ("skills", "brigade skills doctor --target .", skills_cmd.doctor, {"target": target}),
        ("security", "brigade security doctor --target .", security_cmd.doctor, {"target": target}),
    ]
    surfaces: list[dict[str, Any]] = []
    blocking = 0
    for name, command, func, kwargs in spec:
        rc, payload = _capture_json_call(func, **kwargs)
        surface_ready = rc == 0
        if not surface_ready:
            blocking += 1
        surfaces.append(
            {
                "name": name,
                "command": command,
                "ready": surface_ready,
                "exit_code": rc,
                "issue_count": _surface_issue_count(payload),
            }
        )
    ready = blocking == 0
    next_command = next((surface["command"] for surface in surfaces if not surface["ready"]), None)
    return {
        "target": str(target),
        "profile": profile,
        "ready": ready,
        "blocking_surface_count": blocking,
        "surfaces": surfaces,
        "next_command": next_command,
    }


def checkup(*, target: Path, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    payload = checkup_payload(target, profile=profile)
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
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"blocking_surfaces: {payload['blocking_surface_count']}")
    if payload["next_command"]:
        print(f"next: {payload['next_command']}")
    return 0 if payload["ready"] else 1
