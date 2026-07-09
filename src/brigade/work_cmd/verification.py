"""Verify, acceptance, and closeout operations."""

from __future__ import annotations
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4
from .. import graphtrail_delta, localio, receipt_signing
from . import constants, helpers, ledger as ledger_mod
from . import reviews as reviews_mod
from . import scanners as scanners_mod


def _default_verify_commands(target: Path) -> list[str]:
    if (target / "pyproject.toml").is_file() and (target / "tests").is_dir():
        if (target / "src").is_dir():
            return ["PYTHONPATH=src python3 -m pytest -q"]
        return ["python3 -m pytest -q"]
    if (target / "pytest.ini").is_file() or (target / "tests").is_dir():
        return ["python3 -m pytest -q"]
    if (target / "package.json").is_file():
        return ["npm test"]
    return []


def _receipt_git_snapshot(target: Path) -> dict[str, Any] | None:
    try:
        head = helpers._git_value(target, "rev-parse", "HEAD")
        branch = helpers._git_value(target, "rev-parse", "--abbrev-ref", "HEAD")
        status = helpers._git(target, "status", "--porcelain")
    except OSError:
        return None
    if head is None or branch is None or status.returncode != 0:
        return None
    return {"head": head, "branch": branch, "dirty_files": len(status.stdout.splitlines())}


def _verify_parse_command(command: str) -> tuple[list[str] | None, dict[str, str], str | None]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return None, {}, f"invalid command: {exc}"
    if not parts:
        return None, {}, "empty command"
    env: dict[str, str] = {}
    argv = list(parts)
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        key, value = argv.pop(0).split("=", 1)
        env[key] = value
    if not argv:
        return None, env, "command contains only environment assignments"
    executable = Path(argv[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, env, f"high-risk verification command: {executable}"
    if any(constants.SCANNER_SHELL_META_RE.search(part) for part in argv):
        return None, env, (
            "high-risk verification command contains shell metacharacters "
            "(use --argv-json to pass a pre-parsed argv list instead)"
        )
    if "/" in argv[0]:
        if not Path(argv[0]).expanduser().exists():
            return None, env, f"verification command is not resolvable: {argv[0]}"
    elif shutil.which(argv[0]) is None:
        return None, env, f"verification command is not resolvable: {argv[0]}"
    return argv, env, None


def _verify_parse_argv(argv: list[str]) -> tuple[list[str] | None, dict[str, str], str | None]:
    """Resolve a pre-parsed verification argv (e.g. from --argv-json).

    The argv arrived pre-split (no shlex/shell parsing happens on it), so the
    shell-metacharacter heuristic in ``_verify_parse_command`` does not apply:
    there is no shell involved and no ambiguity for it to guard against.
    """
    if not argv:
        return None, {}, "empty command"
    executable = Path(argv[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, {}, f"high-risk verification command: {executable}"
    if "/" in argv[0]:
        if not Path(argv[0]).expanduser().exists():
            return None, {}, f"verification command is not resolvable: {argv[0]}"
    elif shutil.which(argv[0]) is None:
        return None, {}, f"verification command is not resolvable: {argv[0]}"
    return list(argv), {}, None


VERIFY_RUNS_KEEP = 50


def _prune_verify_runs(target: Path, keep: int = VERIFY_RUNS_KEEP) -> int:
    """Cap retained verify-run directories so receipts + raw logs don't grow without bound.

    Run dirs are timestamp-prefixed (sortable by name); the newest ``keep`` are
    retained and older ones removed. Best-effort: a removal error never aborts a
    verify run.
    """
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return 0
    run_dirs = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda p: p.name, reverse=True)
    removed = 0
    for stale in run_dirs[keep:]:
        try:
            shutil.rmtree(stale)
            removed += 1
        except OSError:
            continue
    return removed


def _latest_verify_receipt(target: Path) -> dict[str, Any] | None:
    receipts = _verify_receipts(target)
    return receipts[0] if receipts else None


def _verify_read_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(receipt.parent))
    return data


def _verify_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_verify_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _resolve_verify_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _verify_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "verification run not found: latest")
    matches = [run for run in receipts if str(run.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"verification run not found: {run_id}"
    if len(matches) > 1:
        return None, f"verification run id is ambiguous: {run_id}"
    return matches[0], None


def _verification_task_from_session(payload: dict[str, Any]) -> dict[str, Any] | None:
    task = payload.get("task")
    return task if isinstance(task, dict) else None


def _verification_evidence_payload(target: Path, session: tuple[Path, dict[str, Any]] | None = None) -> dict[str, Any]:
    from .. import handoff_cmd

    target = target.expanduser().resolve()
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    latest_session = session or (sessions[0] if sessions else None)
    session_info = helpers._session_info(latest_session[0], latest_session[1]) if latest_session else None
    task = _verification_task_from_session(latest_session[1]) if latest_session else None
    latest_verify = _latest_verify_receipt(target)
    sweep_health = scanners_mod._scanner_sweep_health(target)
    review_health = reviews_mod._review_health(target)
    handoff_drafts = handoff_cmd.draft_queue_payload(target)
    return {
        "target": str(target),
        "session": session_info,
        "task": task,
        "task_acceptance": task.get("acceptance")
        if isinstance(task, dict) and isinstance(task.get("acceptance"), list)
        else [],
        "latest_verify": latest_verify,
        "scanner_sweep": {
            "latest": sweep_health.get("latest"),
            "issue_count": sweep_health.get("review", {}).get("issue_count")
            if isinstance(sweep_health.get("review"), dict)
            else 0,
            "top_issue": sweep_health.get("review", {}).get("top_issue")
            if isinstance(sweep_health.get("review"), dict)
            else None,
            "due_count": sweep_health.get("due_count"),
        },
        "code_review": {
            "latest_run": review_health.get("latest_run"),
            "latest_unclosed_run": review_health.get("latest_unclosed_run"),
            "unresolved_finding_count": review_health.get("unresolved_finding_count"),
            "top_unresolved_finding": review_health.get("top_unresolved_finding"),
        },
        "handoff_drafts": {
            "counts": handoff_drafts.get("counts"),
            "issue_count": handoff_drafts.get("issue_count"),
            "top_issue": handoff_drafts.get("top_issue"),
            "latest_ingest_run": handoff_drafts.get("latest_ingest_run"),
        },
    }


def _verify_plan_payload(target: Path, commands: list[str] | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    planned_commands = commands if commands is not None else _default_verify_commands(target)
    evidence = _verification_evidence_payload(target)
    blockers: list[str] = []
    if not planned_commands:
        blockers.append("no verification commands found; pass --command")
    for command in planned_commands:
        _, _, error = _verify_parse_command(command)
        if error:
            blockers.append(f"{command}: {error}")
    return {
        "target": str(target),
        "verify_runs_root": str(helpers._verify_runs_root(target)),
        "commands": planned_commands,
        "blockers": blockers,
        "evidence": evidence,
        "suggested_command": "brigade work verify run"
        if planned_commands
        else 'brigade work verify run --command "..."',
    }


def _write_verify_markdown(run_dir: Path, receipt: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Verification",
        "",
        f"- Run: `{receipt.get('run_id')}`",
        f"- Status: {receipt.get('status')}",
        f"- Target: `{receipt.get('target')}`",
        f"- Started: {receipt.get('started_at')}",
        f"- Completed: {receipt.get('completed_at')}",
        "",
        "## Commands",
        "",
    ]
    for command in receipt.get("commands", []):
        if not isinstance(command, dict):
            continue
        lines.append(f"- `{command.get('command')}`: exit={command.get('exit_code')} status={command.get('status')}")
    lines.extend(["", "## Evidence", ""])
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    session = evidence.get("session") if isinstance(evidence.get("session"), dict) else None
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    if session:
        lines.append(f"- Session: `{session.get('id')}` status={session.get('status')}")
    if latest_verify:
        lines.append(f"- Previous verification: `{latest_verify.get('run_id')}` status={latest_verify.get('status')}")
    delta = receipt.get("code_graph_delta")
    if isinstance(delta, dict):
        lines.append(f"- Code graph delta: {delta.get('summary') or 'code graph delta unavailable'}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _run_verify_commands(target: Path, commands: list[str | list[str]], timeout: int) -> tuple[dict[str, Any], int]:
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-work-verify-{uuid4().hex[:6]}"
    run_dir = helpers._verify_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    graph_delta_before = graphtrail_delta.capture_before(target, run_dir)
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "target": str(target),
        "status": "running",
        "started_at": started.isoformat(),
        "timeout": timeout,
        "path": str(run_dir),
        "evidence": _verification_evidence_payload(target),
        "commands": [],
    }
    rc = 0
    for index, command in enumerate(commands, start=1):
        if isinstance(command, list):
            argv, env_assignments, error = _verify_parse_argv(command)
            display_command = shlex.join(command)
        else:
            argv, env_assignments, error = _verify_parse_command(command)
            display_command = command
        command_result: dict[str, Any] = {
            "command": display_command,
            "env": sorted(env_assignments),
            "started_at": helpers._now().isoformat(),
        }
        stdout_path = run_dir / f"command-{index}-stdout.log"
        stderr_path = run_dir / f"command-{index}-stderr.log"
        if error or argv is None:
            # The command never ran - Brigade's own parser refused it (shell
            # metacharacters, a high-risk executable, or an unresolvable binary).
            # Mark it 'rejected', not 'failed': a malformed command is invalid
            # input, not a verified regression, so `outcome capture` must read it
            # as neutral (0), never -1.
            command_result.update(
                {
                    "status": "rejected",
                    "exit_code": 2,
                    "stderr_summary": error,
                    "stdout_summary": "",
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
            stdout_path.write_text("")
            stderr_path.write_text(str(error or "invalid command") + "\n")
            if rc == 0:
                rc = 2
            receipt["commands"].append(command_result)
            continue
        run_env = os.environ.copy()
        run_env.update(env_assignments)
        command_started = helpers._now()
        try:
            completed = subprocess.run(
                argv,
                cwd=target,
                env=run_env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            command_completed = helpers._now()
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            stdout_path.write_text(stdout)
            stderr_path.write_text(stderr)
            status = "completed" if completed.returncode == 0 else "failed"
            if completed.returncode != 0 and rc == 0:
                rc = completed.returncode
            command_result.update(
                {
                    "status": status,
                    "exit_code": completed.returncode,
                    "completed_at": command_completed.isoformat(),
                    "duration_seconds": (command_completed - command_started).total_seconds(),
                    "argv": argv,
                    "stdout_summary": scanners_mod._scanner_run_summary(stdout),
                    "stderr_summary": scanners_mod._scanner_run_summary(stderr),
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
        except subprocess.TimeoutExpired as exc:
            command_completed = helpers._now()
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout_path.write_text(stdout)
            stderr_path.write_text(stderr)
            command_result.update(
                {
                    "status": "timed_out",
                    "exit_code": None,
                    "completed_at": command_completed.isoformat(),
                    "duration_seconds": (command_completed - command_started).total_seconds(),
                    "argv": argv,
                    "stdout_summary": scanners_mod._scanner_run_summary(stdout),
                    "stderr_summary": scanners_mod._scanner_run_summary(stderr),
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
            rc = 124
        receipt["commands"].append(command_result)
    receipt["code_graph_delta"] = graphtrail_delta.capture_after_and_diff(target, run_dir, graph_delta_before)
    completed_at = helpers._now()
    receipt["completed_at"] = completed_at.isoformat()
    receipt["duration_seconds"] = (completed_at - started).total_seconds()
    command_statuses = [c.get("status") for c in receipt["commands"] if isinstance(c, dict)]
    if rc == 0:
        receipt["status"] = "completed"
    elif any(status in ("failed", "timed_out") for status in command_statuses):
        # At least one command actually ran and failed/timed out: a real, verified
        # regression. Otherwise the only non-zero outcome was a parser rejection,
        # which is invalid input (neutral), not a regression.
        receipt["status"] = "failed"
    else:
        receipt["status"] = "rejected"
    git = _receipt_git_snapshot(target)
    if git is not None:
        receipt["git"] = git
    log_digests: dict[str, str] = {}
    for command in receipt["commands"]:
        if not isinstance(command, dict):
            continue
        for key in ("stdout_log_path", "stderr_log_path"):
            value = command.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            try:
                log_name = str(path.relative_to(run_dir))
            except ValueError:
                log_name = path.name
            log_digests[log_name] = localio.file_sha256(path)
    delta = receipt.get("code_graph_delta")
    if isinstance(delta, dict):
        sidecar_value = delta.get("sidecar_path")
        if isinstance(sidecar_value, str) and sidecar_value:
            sidecar_path = Path(sidecar_value)
            if sidecar_path.is_file():
                try:
                    log_name = str(sidecar_path.relative_to(run_dir))
                except ValueError:
                    log_name = sidecar_path.name
                log_digests[log_name] = localio.file_sha256(sidecar_path)
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": dict(sorted(log_digests.items())),
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    signing_key = receipt_signing.load_key(target)
    if signing_key is not None:
        key, key_id = signing_key
        receipt["digests"]["signature"] = receipt_signing.sign(receipt["digests"]["receipt_sha256"], key)
        receipt["digests"]["key_id"] = key_id
    helpers._write_json(run_dir / "receipt.json", receipt)
    _write_verify_markdown(run_dir, receipt)
    _prune_verify_runs(target)
    return receipt, rc


def _resolve_closeout_session(target: Path, session_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    if session_id == "latest":
        return (sessions[0][0], sessions[0][1], None) if sessions else (None, None, "work session not found: latest")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path, payload in sessions:
        payload_id = str(payload.get("id") or path.name)
        if (
            payload_id == session_id
            or path.name == session_id
            or payload_id.startswith(session_id)
            or path.name.startswith(session_id)
        ):
            matches.append((path, payload))
    if not matches:
        path = helpers._resolve_session(target, session_id)
        payload = helpers._read_session(path)
        if payload is not None:
            return path, payload, None
        return None, None, f"work session not found: {session_id}"
    if len(matches) > 1:
        return None, None, f"work session id is ambiguous: {session_id}"
    return matches[0][0], matches[0][1], None


def _work_closeout_path(target: Path, closeout_id: str) -> Path:
    return helpers._work_closeouts_root(target) / closeout_id / "closeout.json"


def _latest_work_closeout_payload(target: Path) -> dict[str, Any] | None:
    root = helpers._work_closeouts_root(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for child in root.iterdir():
        payload = helpers._read_json(child / "closeout.json") if child.is_dir() else None
        if isinstance(payload, dict):
            payload.setdefault("path", str(child / "closeout.json"))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _write_work_closeout_markdown(path: Path, closeout: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Closeout",
        "",
        f"- Closeout: `{closeout.get('closeout_id')}`",
        f"- Status: {closeout.get('status')}",
        f"- Ready: {closeout.get('ready')}",
        f"- Session: `{closeout.get('session', {}).get('id') if isinstance(closeout.get('session'), dict) else ''}`",
        f"- Verification: `{closeout.get('verification', {}).get('run_id') if isinstance(closeout.get('verification'), dict) else ''}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = closeout.get("blockers") if isinstance(closeout.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Evidence", ""])
    for key in ("task", "scanner_sweep", "code_review", "handoff_drafts"):
        value = closeout.get(key)
        lines.append(f"- {key}: `{json.dumps(value, sort_keys=True, default=str)[:500]}`")
    path.with_name("closeout.md").write_text("\n".join(lines) + "\n")


def _work_closeout_payload(target: Path, session_id: str, *, write: bool = False) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    session_path, session_payload, error = _resolve_closeout_session(target, session_id)
    if session_path is None or session_payload is None:
        print(f"error: {error}", file=sys.stderr)
        return None, 1 if error and "not found" in error else 2
    evidence = _verification_evidence_payload(target, (session_path, session_payload))
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    task = evidence.get("task") if isinstance(evidence.get("task"), dict) else None
    task_acceptance = evidence.get("task_acceptance") if isinstance(evidence.get("task_acceptance"), list) else []
    scanner_sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    code_review = evidence.get("code_review") if isinstance(evidence.get("code_review"), dict) else {}
    handoff_drafts = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    blockers: list[str] = []
    if session_payload.get("status") != "ended":
        blockers.append(f"work session is not ended: {session_payload.get('status')}")
    if latest_verify is None:
        blockers.append("no verification receipt found")
    elif latest_verify.get("status") != "completed":
        blockers.append(
            f"latest verification did not complete: {latest_verify.get('run_id')} [{latest_verify.get('status')}]"
        )
    if task is not None and not task_acceptance:
        blockers.append(f"task has no acceptance criteria: {task.get('id')}")
    latest_sweep = scanner_sweep.get("latest") if isinstance(scanner_sweep.get("latest"), dict) else None
    if latest_sweep and latest_sweep.get("status") == "failed":
        blockers.append(f"latest scanner sweep failed: {latest_sweep.get('sweep_id')}")
    if int(scanner_sweep.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved review issue(s): {scanner_sweep.get('issue_count')}")
    if code_review.get("latest_unclosed_run"):
        run = code_review["latest_unclosed_run"]
        if isinstance(run, dict):
            blockers.append(f"review run is not closed out: {run.get('run_id')}")
    if int(code_review.get("unresolved_finding_count") or 0) > 0:
        blockers.append(f"code review has unresolved finding(s): {code_review.get('unresolved_finding_count')}")
    if int(handoff_drafts.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoff_drafts.get('issue_count')}")
    now = helpers._now()
    closeout_id = f"{now.strftime('%Y%m%d-%H%M%S')}-work-closeout-{uuid4().hex[:6]}"
    closeout = {
        "closeout_id": closeout_id,
        "target": str(target),
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "created_at": now.isoformat(),
        "session": helpers._session_info(session_path, session_payload),
        "session_path": str(session_path),
        "task": ledger_mod._task_summary(task) if task else None,
        "acceptance_criteria": task_acceptance,
        "verification": {
            "run_id": latest_verify.get("run_id"),
            "status": latest_verify.get("status"),
            "path": latest_verify.get("path"),
            "command_count": len(latest_verify.get("commands") or []),
        }
        if latest_verify
        else None,
        "scanner_sweep": scanner_sweep,
        "code_review": code_review,
        "handoff_drafts": handoff_drafts,
        "blockers": blockers,
    }
    if write:
        path = _work_closeout_path(target, closeout_id)
        helpers._write_json(path, closeout)
        _write_work_closeout_markdown(path, closeout)
        session_payload["closeout"] = {
            "closeout_id": closeout_id,
            "status": closeout["status"],
            "ready": closeout["ready"],
            "path": str(path),
            "created_at": closeout["created_at"],
        }
        helpers._write_json(session_path / "session.json", session_payload)
        closeout["path"] = str(path)
    return closeout, 0 if closeout["ready"] else 1


def verify_plan(
    *,
    target: Path,
    commands: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _verify_plan_payload(target, commands)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not payload["blockers"] else 1
    print(f"work verify plan: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    print(f"commands: {len(commands)}")
    for command in commands:
        print(f"- {command}")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    print(f"run: {payload['suggested_command']}")
    return 0 if not blockers else 1


def verify_run(
    *,
    target: Path,
    commands: list[str | list[str]] | None = None,
    timeout: int = 900,
    json_output: bool = False,
    capture: str | None = None,
    capture_kind: str = "skill",
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if timeout < 1:
        print("error: --timeout must be a positive integer", file=sys.stderr)
        return 2
    planned = commands if commands is not None else _default_verify_commands(target)
    if not planned:
        print("error: no verification commands found; pass --command", file=sys.stderr)
        return 2
    receipt, rc = _run_verify_commands(target, planned, timeout)
    if json_output:
        if capture:
            # Record the outcome in the same command (closes the loop without a
            # second manual step) while keeping stdout valid JSON.
            import contextlib
            import io

            from .. import outcome_cmd

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                outcome_cmd.capture(
                    target=target,
                    artifact_id=capture,
                    artifact_kind=capture_kind,
                    run_id=receipt["run_id"],
                    json_output=False,
                )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return rc
    print(f"work verify run: {target}")
    print(f"run: {receipt['run_id']}")
    print(f"status: {receipt['status']}")
    print(f"commands: {len(receipt['commands'])}")
    for command in receipt["commands"]:
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
    print(f"receipt: {Path(str(receipt['path'])) / 'receipt.json'}")
    if capture:
        from .. import outcome_cmd

        outcome_cmd.capture(
            target=target,
            artifact_id=capture,
            artifact_kind=capture_kind,
            run_id=receipt["run_id"],
            json_output=False,
        )
    return rc


def verify_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    runs = _verify_receipts(target)[:limit]
    payload = {"target": str(target), "verify_runs_root": str(helpers._verify_runs_root(target)), "runs": runs}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work verify runs: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    if not runs:
        print("runs: none")
        return 0
    for run in runs:
        print(
            f"- {run.get('run_id')} [{run.get('status')}] commands={len(run.get('commands') or [])} {run.get('started_at')}"
        )
    return 0


def verify_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    run, error = _resolve_verify_receipt(target, run_id)
    if run is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(run, indent=2, sort_keys=True))
        return 0
    print(f"work verify run: {run.get('run_id')}")
    print(f"status: {run.get('status')}")
    print(f"target: {run.get('target')}")
    print(f"started: {run.get('started_at')}")
    print(f"completed: {run.get('completed_at')}")
    for command in run.get("commands", []):
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
            if command.get("stdout_summary"):
                print(f"  stdout: {helpers._short(str(command.get('stdout_summary')), 140)}")
            if command.get("stderr_summary"):
                print(f"  stderr: {helpers._short(str(command.get('stderr_summary')), 140)}")
    return 0


def closeout(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _work_closeout_payload(target, session_id, write=True)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work closeout: {payload['closeout_id']}")
    print(f"status: {payload['status']}")
    print(f"ready: {payload['ready']}")
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    print(f"session: {session.get('id')}")
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else None
    if verification:
        print(f"verification: {verification.get('run_id')} [{verification.get('status')}]")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    if payload.get("path"):
        print(f"receipt: {payload['path']}")
    return rc


def _acceptance_payload(target: Path) -> dict[str, Any]:
    tasks = [task for task in ledger_mod._read_task_ledger(target).get("tasks", []) if isinstance(task, dict)]
    pending = [task for task in tasks if task.get("status", "pending") == "pending"]
    done = [task for task in tasks if task.get("status") == "done"]
    pending_with_acceptance = [task for task in pending if ledger_mod._task_acceptance(task)]
    pending_missing = [task for task in pending if not ledger_mod._task_acceptance(task)]
    done_with_completion = [task for task in done if task.get("completion")]
    done_missing_completion = [task for task in done if not task.get("completion")]
    done_missing_completed_acceptance = [
        task
        for task in done
        if ledger_mod._task_acceptance(task) and not ledger_mod._normalize_acceptance(task.get("completed_acceptance"))
    ]
    review_payload = reviews_mod._review_findings_payload(target)
    review_groups = review_payload.get("groups") if isinstance(review_payload.get("groups"), dict) else {}
    review_outcomes = review_groups.get("by_resolution") if isinstance(review_groups.get("by_resolution"), dict) else {}
    latest_closeout = _latest_work_closeout_payload(target)
    closeout_summary = None
    if latest_closeout is not None:
        closeout_summary = {
            "closeout_id": latest_closeout.get("closeout_id"),
            "status": latest_closeout.get("status"),
            "ready": latest_closeout.get("ready"),
            "path": latest_closeout.get("path"),
            "acceptance_count": len(latest_closeout.get("acceptance_criteria") or []),
            "blocker_count": len(latest_closeout.get("blockers") or []),
        }
    issues: list[dict[str, Any]] = []
    if pending_missing:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_pending_missing",
                "detail": f"{len(pending_missing)} pending task(s) missing acceptance",
            }
        )
    if done_missing_completion:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completion",
                "detail": f"{len(done_missing_completion)} done task(s) missing completion evidence",
            }
        )
    if done_missing_completed_acceptance:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completed_acceptance",
                "detail": f"{len(done_missing_completed_acceptance)} done task(s) missing completion-time acceptance evidence",
            }
        )
    if int(review_payload.get("unresolved_count") or 0) > 0:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_review_findings_unresolved",
                "detail": f"{review_payload.get('unresolved_count')} review finding(s) unresolved",
            }
        )
    if done and latest_closeout is None:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_missing",
                "detail": "completed tasks exist but no work closeout receipt was found",
            }
        )
    elif latest_closeout is not None and not latest_closeout.get("ready"):
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_blocked",
                "detail": f"latest work closeout is not ready: {latest_closeout.get('closeout_id')}",
            }
        )
    return {
        "target": str(target),
        "task_count": len(tasks),
        "pending_count": len(pending),
        "done_count": len(done),
        "pending_with_acceptance": [task.get("id") for task in pending_with_acceptance],
        "pending_missing_acceptance": [task.get("id") for task in pending_missing],
        "done_with_completion": [task.get("id") for task in done_with_completion],
        "done_missing_completion": [task.get("id") for task in done_missing_completion],
        "done_missing_completed_acceptance": [task.get("id") for task in done_missing_completed_acceptance],
        "review_findings": {
            "count": review_payload.get("count"),
            "unresolved_count": review_payload.get("unresolved_count"),
            "outcomes": dict(sorted(review_outcomes.items())),
            "top_unresolved": review_payload.get("top_unresolved"),
        },
        "review_finding_pending_count": int(review_outcomes.get("pending") or 0),
        "latest_work_closeout": closeout_summary,
        "coverage": {
            "pending_with_acceptance": len(pending_with_acceptance),
            "pending_missing_acceptance": len(pending_missing),
            "done_with_completion": len(done_with_completion),
            "done_missing_completion": len(done_missing_completion),
            "done_with_completed_acceptance": len(done) - len(done_missing_completed_acceptance),
            "done_missing_completed_acceptance": len(done_missing_completed_acceptance),
            "review_findings_resolved": int(review_payload.get("count") or 0)
            - int(review_payload.get("unresolved_count") or 0),
            "review_findings_unresolved": int(review_payload.get("unresolved_count") or 0),
            "work_closeout_ready": 1 if latest_closeout is not None and latest_closeout.get("ready") else 0,
            "work_closeout_missing": 1 if latest_closeout is None else 0,
        },
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def acceptance(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _acceptance_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work acceptance: {target}")
    print(f"tasks: {payload['task_count']}")
    print(f"pending_missing_acceptance: {len(payload['pending_missing_acceptance'])}")
    print(f"done_missing_completion: {len(payload['done_missing_completion'])}")
    print(f"done_missing_completed_acceptance: {len(payload['done_missing_completed_acceptance'])}")
    print(f"review_findings_pending: {payload['review_finding_pending_count']}")
    review_findings = payload.get("review_findings") if isinstance(payload.get("review_findings"), dict) else {}
    print(f"review_findings_unresolved: {review_findings.get('unresolved_count', 0)}")
    closeout = payload.get("latest_work_closeout") if isinstance(payload.get("latest_work_closeout"), dict) else None
    print(f"work_closeout: {closeout.get('closeout_id') if closeout else 'none'}")
    return 0
