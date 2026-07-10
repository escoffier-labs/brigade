"""Session lifecycle, run/status/doctor/brief, and task operations."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from ... import dogfood_cmd, localio
from ...install import apply_gitignore
from .. import constants, helpers, ledger as ledger_mod, config as config_mod, services as services_mod
from .. import scanners as scanners_mod, reviews as reviews_mod

from . import lifecycle as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def bootstrap(
    *,
    target: Path,
    artifacts_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    force: bool = False,
    handoff: bool = True,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    update_gitignore: bool = True,
) -> int:
    if timeout_seconds <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    print(f"work bootstrap: {target}")
    if not target.is_dir():
        _print_bootstrap_line(constants.FAIL, "target", f"not a directory: {target}")
        return 2
    _print_bootstrap_line(constants.OK, "target", target)

    failures = 0
    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        _print_bootstrap_line(constants.FAIL, "git", "not a git repository")
    else:
        _print_bootstrap_line(constants.OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    if config.exists() and not force:
        _print_bootstrap_line(constants.OK, "dogfood_config", f"exists at {config}")
    else:
        rc = dogfood_cmd.init(
            target=target,
            artifacts_dir=artifacts_dir,
            handoff_inbox=handoff_inbox,
            force=force,
            handoff=handoff,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            failures += 1
            _print_bootstrap_line(constants.FAIL, "dogfood_config", f"init failed with exit code {rc}")
        else:
            _print_bootstrap_line(constants.OK, "dogfood_config", config)

    try:
        effective_target, effective_artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        effective_target = target
        effective_artifacts_dir = artifacts_dir or (target / ".brigade" / "runs")
        cfg = None
        _print_bootstrap_line(constants.FAIL, "dogfood_paths", exc)
    else:
        _print_bootstrap_line(constants.OK, "dogfood_target", effective_target)
        _print_bootstrap_line(constants.OK, "dogfood_artifacts", effective_artifacts_dir)

    work_root = helpers._work_root(effective_target)
    effective_artifacts_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    _print_bootstrap_line(constants.OK, "artifacts_dir", effective_artifacts_dir)
    _print_bootstrap_line(constants.OK, "work_root", work_root)

    effective_handoff = cfg.handoff if cfg is not None else handoff
    effective_handoff_inbox = (
        cfg.handoff_inbox
        if cfg is not None and cfg.handoff_inbox is not None
        else handoff_inbox.expanduser()
        if handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    if effective_handoff:
        effective_handoff_inbox.mkdir(parents=True, exist_ok=True)
        _print_bootstrap_line(constants.OK, "handoff_inbox", effective_handoff_inbox)
    else:
        _print_bootstrap_line(constants.WARN, "handoff_inbox", "handoff disabled")

    if update_gitignore:
        result = apply_gitignore(effective_target, helpers._work_selection(effective_target, effective_handoff_inbox))
        _print_bootstrap_line(constants.OK, "gitignore", result)
    else:
        _print_bootstrap_line(constants.WARN, "gitignore", "skipped")

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        _print_bootstrap_line(constants.FAIL, "codex", "missing on PATH")
    else:
        _print_bootstrap_line(constants.OK, "codex", codex_path)

    config_ignored = localio.check_git_ignored(effective_target, config)
    artifacts_ignored = localio.check_git_ignored(effective_target, effective_artifacts_dir)
    work_ignored = localio.check_git_ignored(effective_target, work_root)
    handoff_ignored = (
        localio.check_git_ignored(effective_target, effective_handoff_inbox) if effective_handoff else "disabled"
    )
    ignore_values = {
        "config_ignored": config_ignored,
        "artifacts_ignored": artifacts_ignored,
        "work_ignored": work_ignored,
        "handoff_ignored": handoff_ignored,
    }
    for name, value in ignore_values.items():
        level = constants.OK if value in {"yes", "outside-target", "disabled"} else constants.WARN
        _print_bootstrap_line(level, name, value)

    ready = failures == 0
    _print_bootstrap_line(
        constants.OK if ready else constants.FAIL,
        "ready",
        "daily work loop is usable" if ready else f"{failures} blocker{'s' if failures != 1 else ''}",
    )
    print("next_command: brigade work run")
    return 0 if ready else 1


def run(
    task: str | None,
    *,
    target: Path,
    task_id: str | None = None,
    title: str | None = None,
    output_dir: Path | None = None,
    handoff: bool = True,
    handoff_inbox: Path | None = None,
    dogfood_handoff: bool = False,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    recap_limit: int = 1,
    queue_next: bool = False,
) -> int:
    if recap_limit < 1:
        print("error: --recap-limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    resolved = _resolve_next_task(target)
    if task_id is not None:
        if task:
            print("error: pass a task or task_id, not both", file=sys.stderr)
            return 2
        selected_task, _ = ledger_mod._find_task(target, task_id)
        if selected_task is None or selected_task.get("status", "pending") != "pending":
            print(f"error: pending task not found: {task_id}", file=sys.stderr)
            return 1
        resolved = {
            "task": str(selected_task.get("text", "")).strip(),
            "source": "task_ledger",
            "task_id": selected_task.get("id"),
            "ledger_task": selected_task,
            "dogfood": helpers._dogfood_snapshot(target),
        }
    task_text = task or str(resolved["task"])
    consumed_task_id = resolved.get("task_id") if task is None and resolved.get("source") == "task_ledger" else None
    ledger_task = (
        resolved.get("ledger_task") if consumed_task_id and isinstance(resolved.get("ledger_task"), dict) else None
    )
    run_task_text = (
        _render_task_run_prompt(ledger_task)
        if ledger_task is not None and ledger_mod._task_acceptance(ledger_task)
        else task_text
    )
    task_snapshot = ledger_mod._task_snapshot(ledger_task) if ledger_task is not None else None
    session_title = title or task_text
    start_rc = start(target=target, title=session_title, task_snapshot=task_snapshot)
    if start_rc != 0:
        return start_rc
    session_dir = helpers._active_session_dir(target)

    dogfood_rc = 1
    try:
        dogfood_rc = dogfood_cmd.run(
            run_task_text,
            target=target,
            output_dir=output_dir,
            handoff=dogfood_handoff,
            handoff_inbox=handoff_inbox if dogfood_handoff else None,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
    finally:
        note = f"brigade work run completed with dogfood exit code {dogfood_rc}"
        end_rc = end(target=target, note=note, handoff=handoff, handoff_inbox=handoff_inbox)

    if end_rc != 0:
        return end_rc if dogfood_rc == 0 else dogfood_rc
    if dogfood_rc == 0 and isinstance(consumed_task_id, str):
        task, ledger = ledger_mod._find_task(target, consumed_task_id)
        if task is not None:
            now = helpers._now().isoformat()
            task["status"] = "done"
            task["updated_at"] = now
            task["completed_at"] = now
            task["completed_session_title"] = session_title
            if session_dir is not None:
                task["completed_session_path"] = str(session_dir)
            completed_run_path = _latest_completed_run_path(target, output_dir)
            if completed_run_path is not None:
                task["completed_run_path"] = completed_run_path
            task["completed_acceptance"] = ledger_mod._task_acceptance(task)
            ledger_mod._write_task_ledger(target, ledger)
    if dogfood_rc == 0 and queue_next:
        queued_task, created, reason = _queue_latest_next(
            target,
            session_dir=session_dir,
            session_title=session_title,
        )
        if queued_task is None:
            print(f"queued_next: skipped ({reason})")
        else:
            print(f"queued_next: {queued_task.get('id')} ({'created' if created else 'existing'})")
    recap(target=target, limit=recap_limit)
    return dogfood_rc


def status(*, target: Path, limit: int = 12) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work: {target}")
    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        print("git: unavailable")
    else:
        print(f"repo: {repo_root}")
        branch = helpers._git_value(target, "branch", "--show-current")
        if branch is None:
            branch = helpers._git_value(target, "rev-parse", "--short", "HEAD") or "unknown"
            branch = f"detached:{branch}"
        print(f"branch: {branch}")
        status_out = helpers._git_value(target, "status", "--short") or ""
        _print_dirty(status_out.splitlines(), limit=limit)

    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"dogfood: not ready ({exc})")
        return 0

    config = dogfood_cmd.config_path(target)
    codex_path = shutil.which("codex")
    dogfood_ready = config.exists() and codex_path is not None and effective_target.is_dir()
    print(f"dogfood: {'ready' if dogfood_ready else 'not ready'}")
    print(f"dogfood_config: {config if config.exists() else str(config) + ' (missing)'}")
    print(f"dogfood_target: {effective_target}")
    print(f"dogfood_artifacts: {artifacts_dir}")
    print(f"codex: {codex_path or 'missing'}")
    if cfg and cfg.handoff:
        handoff_inbox = cfg.handoff_inbox or dogfood_cmd.default_handoff_inbox(effective_target)
        print(f"handoff_inbox: {handoff_inbox}")

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        print("latest_run: none")
        print("next: none")
        return 0

    latest_path, latest_meta = latest
    print(
        "latest_run: "
        f"{latest_meta.get('started_at', latest_path.name)} "
        f"[{latest_meta.get('status', 'unknown')}] {latest_path}"
    )
    task = helpers._short(str(latest_meta.get("task") or ""))
    if task:
        print(f"latest_task: {task}")
    next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
    print(f"next: {helpers._short(next_step) if next_step else 'none'}")
    print("next_command: brigade dogfood next")
    print("inspect_command: brigade dogfood latest")
    return 0


def doctor(*, target: Path) -> int:
    from .. import (
        center_cmd,
        chat_cmd,
        context_cmd,
        daily_cmd,
        handoff_cmd,
        learn_cmd,
        memory_cmd,
        phases_cmd,
        projects_cmd,
        repos_cmd,
        roadmap_cmd,
        security_cmd,
        tools_cmd,
    )

    target = target.expanduser().resolve()
    failures = 0

    print(f"work doctor: {target}")
    if not target.is_dir():
        helpers._doctor_line(constants.FAIL, "target", f"not a directory: {target}")
        return 2
    helpers._doctor_line(constants.OK, "target", target)

    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        helpers._doctor_line(constants.FAIL, "git", "not a git repository")
    else:
        helpers._doctor_line(constants.OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        helpers._doctor_line(constants.FAIL, "dogfood_config", exc)
        effective_target = target
        artifacts_dir = target / ".brigade" / "runs"
        cfg = None
    else:
        if config.is_file():
            helpers._doctor_line(constants.OK, "dogfood_config", config)
        else:
            failures += 1
            helpers._doctor_line(
                constants.FAIL, "dogfood_config", f"missing, run `brigade dogfood init --target {target}`"
            )
        helpers._doctor_line(constants.OK, "dogfood_target", effective_target)
        helpers._doctor_line(constants.OK, "dogfood_artifacts", artifacts_dir)

    security_config = security_cmd.config_path(effective_target)
    security_config_valid = True
    if security_config.is_file():
        try:
            loaded_security = security_cmd.load_config(effective_target)
        except ValueError as exc:
            security_config_valid = False
            failures += 1
            helpers._doctor_line(constants.FAIL, "security_config", f"invalid {security_config}: {exc}")
        else:
            policy = loaded_security.policy if loaded_security is not None else "personal"
            helpers._doctor_line(constants.OK, "security_config", f"{security_config} (policy={policy})")
            enrichment = security_cmd.enrichment_health(effective_target)
            helpers._doctor_line(
                constants.OK if enrichment.get("configured") else constants.WARN,
                "security_enrichment",
                f"{enrichment.get('provider') or 'none'} ({enrichment.get('status')})",
            )
    else:
        helpers._doctor_line(
            constants.WARN, "security_config", f"missing, run `brigade security init --target {effective_target}`"
        )

    if security_config_valid:
        try:
            suppression_health = security_cmd.suppression_health(effective_target)
        except ValueError as exc:
            failures += 1
            helpers._doctor_line(constants.FAIL, "security_suppressions", f"invalid: {exc}")
        else:
            stale = suppression_health["stale"]
            missing_reasons = suppression_health["missing_reasons"]
            if stale:
                helpers._doctor_line(
                    constants.WARN,
                    "security_stale_suppressions",
                    f"{len(stale)} no longer match current findings: {', '.join(stale[:5])}",
                )
            if missing_reasons:
                helpers._doctor_line(
                    constants.WARN,
                    "security_suppression_reasons",
                    f"{len(missing_reasons)} missing reason: {', '.join(missing_reasons[:5])}",
                )
            if not stale and not missing_reasons:
                helpers._doctor_line(
                    constants.OK, "security_suppressions", f"{suppression_health['suppression_count']} configured"
                )

    security_artifacts = security_cmd.default_artifacts_dir(effective_target)
    security_bundle = security_cmd.inspect_evidence_bundle(security_artifacts)
    if security_bundle.get("ready"):
        helpers._doctor_line(
            constants.OK,
            "security_evidence",
            f"{security_artifacts} "
            f"(generated_at={security_bundle.get('generated_at')}, findings={security_bundle.get('finding_count')})",
        )
    else:
        helpers._doctor_line(
            constants.WARN,
            "security_evidence",
            f"{security_bundle.get('reason')}; run `brigade security scan --target {effective_target} --output-dir {security_artifacts}`",
        )
    security_health = security_cmd.health(effective_target)
    open_finding_check = None
    for check in security_health["checks"]:
        if check.get("name") == "security_open_findings":
            open_finding_check = check
            break
    if open_finding_check is not None:
        helpers._doctor_line(
            str(open_finding_check.get("status")), "security_open_findings", open_finding_check.get("detail")
        )

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        helpers._doctor_line(constants.FAIL, "codex", "missing on PATH")
    else:
        helpers._doctor_line(constants.OK, "codex", codex_path)

    work_root = helpers._work_root(effective_target)
    helpers._doctor_line(constants.OK if work_root.parent.exists() else constants.WARN, "work_root", work_root)
    current = helpers._current_path(effective_target)
    if current.exists():
        active_dir = work_root / current.read_text().strip()
        active_payload = helpers._read_session(active_dir)
        if active_payload is None:
            failures += 1
            helpers._doctor_line(constants.FAIL, "active_session", f"invalid: {active_dir}")
        else:
            helpers._doctor_line(constants.WARN, "active_session", f"active: {active_dir}")
            started = helpers._parse_iso_datetime(active_payload.get("started_at"))
            if started is not None:
                age_hours = (helpers._now() - started).total_seconds() / 3600
                if age_hours > constants.ACTIVE_SESSION_STALE_HOURS:
                    helpers._doctor_line(
                        constants.WARN,
                        "active_session_age",
                        f"open for {age_hours:.1f} hours, close or resume it",
                    )
    else:
        helpers._doctor_line(constants.OK, "active_session", "none")

    pending_tasks = ledger_mod._pending_tasks(effective_target)
    missing_acceptance = [task for task in pending_tasks if not ledger_mod._task_acceptance(task)]
    if missing_acceptance:
        sample = ", ".join(str(task.get("id")) for task in missing_acceptance[:5])
        helpers._doctor_line(
            constants.WARN,
            "task_acceptance",
            f"{len(missing_acceptance)} pending task(s) missing acceptance criteria: {sample}",
        )
    else:
        helpers._doctor_line(
            constants.OK, "task_acceptance", "pending tasks have acceptance criteria or no tasks are pending"
        )

    plan_coverage = ledger_mod._plan_coverage_payload(effective_target)
    if plan_coverage["significant_without_plan"] > 0:
        plan_sample = ", ".join(plan_coverage["task_ids"][:5])
        helpers._doctor_line(
            constants.WARN,
            "plan_coverage",
            f"{plan_coverage['significant_without_plan']} significant pending task(s) without a plan artifact: {plan_sample}",
        )
    else:
        helpers._doctor_line(constants.OK, "plan_coverage", "significant pending tasks have plan artifacts")

    workflow_rules = _workflow_rule_health(effective_target)
    helpers._doctor_line(str(workflow_rules["status"]), str(workflow_rules["name"]), workflow_rules["detail"])

    issue_tasks = [(task, issue) for task in pending_tasks if (issue := ledger_mod._task_issue_metadata(task))]
    if issue_tasks:
        gh_path = shutil.which("gh")
        if gh_path is None:
            sample = ", ".join(str(task.get("id")) for task, _ in issue_tasks[:5])
            helpers._doctor_line(
                constants.WARN,
                "github_issues",
                f"{len(issue_tasks)} issue-backed task(s) cannot be checked because gh is missing: {sample}",
            )
        else:
            closed: list[str] = []
            unchecked: list[str] = []
            for task, issue in issue_tasks:
                issue_ref = ledger_mod._github_issue_ref(issue)
                if issue_ref is None:
                    unchecked.append(str(task.get("id")))
                    continue
                remote_issue, _, error = ledger_mod._read_github_issue(effective_target, issue_ref)
                if remote_issue is None:
                    unchecked.append(f"{task.get('id')} ({error})")
                    continue
                state = str(remote_issue.get("state") or "").lower()
                if state == "closed":
                    closed.append(str(task.get("id")))
            if closed:
                helpers._doctor_line(
                    constants.WARN,
                    "github_issues_closed",
                    f"{len(closed)} remote issue(s) are closed: {', '.join(closed[:5])}",
                )
            if unchecked:
                helpers._doctor_line(
                    constants.WARN,
                    "github_issues_unchecked",
                    f"{len(unchecked)} issue-backed task(s) could not be checked: {', '.join(unchecked[:5])}",
                )
            if not closed and not unchecked:
                helpers._doctor_line(constants.OK, "github_issues", f"{len(issue_tasks)} issue-backed task(s) checked")
    else:
        helpers._doctor_line(constants.OK, "github_issues", "none")

    pending_imports = ledger_mod._pending_imports(effective_target)
    now = helpers._now()
    stale_imports = [
        item
        for item in pending_imports
        if (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (now - created).total_seconds() / 3600 > constants.IMPORT_STALE_HOURS
    ]
    if stale_imports:
        sample = ", ".join(str(item.get("id")) for item in stale_imports[:5])
        helpers._doctor_line(
            constants.WARN,
            "scanner_imports_stale",
            f"{len(stale_imports)} pending import(s) older than {constants.IMPORT_STALE_HOURS}h: {sample}",
        )
    else:
        helpers._doctor_line(constants.OK, "scanner_imports_stale", "none")
    task_imports_missing_acceptance = [
        item for item in pending_imports if item.get("kind") == "task" and not ledger_mod._import_task_acceptance(item)
    ]
    if task_imports_missing_acceptance:
        sample = ", ".join(str(item.get("id")) for item in task_imports_missing_acceptance[:5])
        helpers._doctor_line(
            constants.WARN,
            "scanner_import_acceptance",
            f"{len(task_imports_missing_acceptance)} pending task import(s) missing acceptance criteria: {sample}",
        )
    else:
        helpers._doctor_line(
            constants.OK,
            "scanner_import_acceptance",
            "pending task imports have acceptance criteria or no task imports are pending",
        )
    dismissed_by_source: dict[str, int] = {}
    for item in ledger_mod._read_imports(effective_target):
        if not isinstance(item, dict) or item.get("status") != "dismissed":
            continue
        source = str(item.get("source") or "manual")
        dismissed_by_source[source] = dismissed_by_source.get(source, 0) + 1
    noisy_sources = {
        source: count
        for source, count in dismissed_by_source.items()
        if count >= constants.DISMISSED_SOURCE_WARN_THRESHOLD
    }
    if noisy_sources:
        detail = ", ".join(f"{source}={count}" for source, count in sorted(noisy_sources.items()))
        helpers._doctor_line(
            constants.WARN,
            "scanner_import_noise",
            f"dismissed import threshold {constants.DISMISSED_SOURCE_WARN_THRESHOLD}: {detail}",
        )
    else:
        helpers._doctor_line(constants.OK, "scanner_import_noise", "none")

    inbox_hygiene = services_mod._inbox_hygiene_payload(effective_target)
    for check in inbox_hygiene["checks"]:
        if check.get("status") != constants.OK:
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    scanner_health = scanners_mod._scanner_health(effective_target)
    for check in scanner_health["checks"]:
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    sweep_health = scanners_mod._scanner_sweep_health(effective_target)
    for check in sweep_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    review_health = reviews_mod._review_health(effective_target)
    for check in review_health["checks"]:
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    chat_health = chat_cmd.health(effective_target)
    for check in chat_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    memory_health = memory_cmd.health(effective_target)
    for check in memory_health["checks"]:
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    backup_health = config_mod._backup_health(effective_target)
    for check in backup_health.get("active_checks", backup_health["checks"]):
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    tool_health = tools_cmd.health(effective_target)
    if tool_health["issues"]:
        for issue in tool_health["issues"]:
            if issue.get("status") == constants.FAIL:
                failures += 1
            helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))
    else:
        helpers._doctor_line(constants.OK, "tool_catalog", f"{tool_health['tool_count']} configured")

    roadmap_health = roadmap_cmd.health(effective_target)
    for issue in roadmap_health["checks"]:
        helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))

    repo_health = repos_cmd.health(effective_target)
    for check in repo_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    for bucket in (repo_health.get("report"), repo_health.get("actions")):
        if isinstance(bucket, dict):
            for check in bucket.get("checks", []):
                helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    sweep_bucket = repo_health.get("sweep")
    if isinstance(sweep_bucket, dict):
        for check in sweep_bucket.get("checks", []):
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    release_bucket = repo_health.get("release_train")
    if isinstance(release_bucket, dict):
        for check in release_bucket.get("checks", []):
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    context_health = context_cmd.health(effective_target)
    for issue in context_health.get("issues", []):
        helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))
    if not context_health.get("issues"):
        helpers._doctor_line(constants.OK, "context_packs", f"{context_health.get('pack_count', 0)} local pack(s)")

    projects_health = projects_cmd.health(effective_target)
    for check in projects_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    learning_health = learn_cmd.health(effective_target)
    if learning_health.get("issue_count"):
        top_learning = learning_health.get("top_issue") if isinstance(learning_health.get("top_issue"), dict) else {}
        helpers._doctor_line(
            constants.WARN,
            "learning_candidates",
            top_learning.get("detail") or f"{learning_health.get('candidate_count', 0)} candidate(s)",
        )
    else:
        helpers._doctor_line(constants.OK, "learning_candidates", "none")

    center_report_health = center_cmd.report_health(effective_target)
    for check in center_report_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not center_report_health.get("checks"):
        latest_report = (
            center_report_health.get("latest") if isinstance(center_report_health.get("latest"), dict) else {}
        )
        helpers._doctor_line(constants.OK, "operator_report", latest_report.get("report_id") or "none")

    center_actions_health = center_cmd.actions_health(effective_target)
    for check in center_actions_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not center_actions_health.get("checks"):
        helpers._doctor_line(
            constants.OK, "operator_actions", f"{center_actions_health.get('action_count', 0)} action(s)"
        )

    daily_health = daily_cmd.health(effective_target)
    for check in daily_health.get("checks", []):
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not daily_health.get("issue_count"):
        helpers._doctor_line(constants.OK, "daily_driver", f"{daily_health.get('run_count', 0)} run(s)")

    phase_health = phases_cmd.health(effective_target)
    for check in phase_health.get("checks", []):
        if check.get("status") == constants.FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not phase_health.get("issue_count"):
        helpers._doctor_line(constants.OK, "phase_ledger", f"{phase_health.get('record_count', 0)} record(s)")

    handoff_inbox = (
        cfg.handoff_inbox
        if cfg and cfg.handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    helpers._doctor_line(
        constants.OK if handoff_inbox.parent.exists() else constants.WARN, "handoff_inbox", handoff_inbox
    )

    config_ignored = localio.check_git_ignored(effective_target, config)
    helpers._doctor_line(_doctor_ignore_level(config_ignored), "config_ignored", config_ignored)
    artifacts_ignored = localio.check_git_ignored(effective_target, artifacts_dir)
    helpers._doctor_line(_doctor_ignore_level(artifacts_ignored), "artifacts_ignored", artifacts_ignored)
    security_ignored = localio.check_git_ignored(effective_target, security_artifacts)
    helpers._doctor_line(_doctor_ignore_level(security_ignored), "security_ignored", security_ignored)
    backup_config_ignored = localio.check_git_ignored(effective_target, helpers._backup_config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(backup_config_ignored), "backup_config_ignored", backup_config_ignored)
    scanner_config_ignored = localio.check_git_ignored(effective_target, helpers._scanner_config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(scanner_config_ignored), "scanner_config_ignored", scanner_config_ignored)
    tools_config_ignored = localio.check_git_ignored(effective_target, tools_cmd.config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(tools_config_ignored), "tools_config_ignored", tools_config_ignored)
    work_ignored = localio.check_git_ignored(effective_target, work_root)
    helpers._doctor_line(_doctor_ignore_level(work_ignored), "work_ignored", work_ignored)
    handoff_ignored = localio.check_git_ignored(effective_target, handoff_inbox)
    helpers._doctor_line(_doctor_ignore_level(handoff_ignored), "handoff_ignored", handoff_ignored)

    for status, name, detail in handoff_cmd.doctor_checks(effective_target):
        if status == constants.FAIL:
            failures += 1
        helpers._doctor_line(status, name, detail)

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        helpers._doctor_line(constants.WARN, "latest_run", "none")
    else:
        latest_path, latest_meta = latest
        helpers._doctor_line(
            constants.OK, "latest_run", f"{latest_meta.get('started_at', latest_path.name)} {latest_path}"
        )
        next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
        helpers._doctor_line(
            constants.OK if next_step else constants.WARN,
            "latest_next",
            helpers._short(next_step) if next_step else "none",
        )

    if failures:
        helpers._doctor_line(constants.FAIL, "ready", f"{failures} blocker{'s' if failures != 1 else ''}")
        return 1
    helpers._doctor_line(constants.OK, "ready", "daily work loop is usable")
    return 0
