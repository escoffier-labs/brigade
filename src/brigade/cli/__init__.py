"""brigade command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import __version__

# imported here so tests can monkeypatch cli.prompt_for_selection
from ..prompt import prompt_for_selection as prompt_for_selection

# Shared parser helpers live in cli._common. COMMAND_GROUPS is re-exported here
# so it stays on the public surface (tests reference cli.COMMAND_GROUPS).
from ._common import (
    COMMAND_GROUPS as COMMAND_GROUPS,
    _START_HERE,
    _TopLevelHelpFormatter,
    _grouped_epilog,
)

# Migrated command-group modules (batch 1). Each exposes register(sub) and
# dispatch(args). They are registered in _build_parser in the same order the
# inline parser blocks were originally added.
from . import (
    init as _init_group,
    doctor as _doctor_group,
    status as _status_group,
    daily as _daily_group,
    add as _add_group,
    pantry as _pantry_group,
    notifications as _notifications_group,
    budgets as _budgets_group,
    untrusted as _untrusted_group,
    skills as _skills_group,
    operator as _operator_group,
    runbook as _runbook_group,
    dogfood as _dogfood_group,
    release as _release_group,
    roadmap as _roadmap_group,
    repos as _repos_group,
    handoff as _handoff_group,
    memory as _memory_group,
    work as _work_group,
    chat as _chat_group,
    context as _context_group,
    projects as _projects_group,
    learn as _learn_group,
    research as _research_group,
    center as _center_group,
    run as _run_group,
    roster as _roster_group,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade",
        description=_START_HERE,
        formatter_class=_TopLevelHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"brigade {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    _init_group.register(sub)
    _doctor_group.register(sub)
    _status_group.register(sub)
    _daily_group.register(sub)
    _add_group.register(sub)
    _pantry_group.register(sub)
    _notifications_group.register(sub)
    _budgets_group.register(sub)
    _untrusted_group.register(sub)

    _skills_group.register(sub)
    _operator_group.register(sub)
    _runbook_group.register(sub)
    _dogfood_group.register(sub)
    _release_group.register(sub)
    _roadmap_group.register(sub)
    _repos_group.register(sub)
    _handoff_group.register(sub)

    _memory_group.register(sub)
    _work_group.register(sub)

    _chat_group.register(sub)

    _context_group.register(sub)
    _projects_group.register(sub)
    _learn_group.register(sub)
    _research_group.register(sub)
    _center_group.register(sub)
    _run_group.register(sub)
    _roster_group.register(sub)

    # runs
    p_runs = sub.add_parser("runs", help="Inspect Brigade run artifacts.")
    runs_sub = p_runs.add_subparsers(dest="runs_command", metavar="<runs-command>")
    runs_sub.required = True
    p_runs_list = runs_sub.add_parser("list", help="List recent Brigade run directories.")
    p_runs_list.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be listed.",
    )
    p_runs_list.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_list.add_argument("--limit", type=int, default=10, help="Maximum number of runs to show.")
    p_runs_latest = runs_sub.add_parser("latest", help="Show the most recent Brigade run.")
    p_runs_latest.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be inspected.",
    )
    p_runs_latest.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_show = runs_sub.add_parser("show", help="Show a readable summary of one run directory.")
    p_runs_show.add_argument("run_dir", type=Path, help="Path to a Brigade run artifact directory.")

    # scrub
    p_scrub = sub.add_parser("scrub", help="Run content-guard against a target.")
    p_scrub.add_argument("--target", "-t", type=Path, default=Path("."))
    p_scrub.add_argument(
        "--policy",
        default="public-repo",
        help="Policy file name (looks under .brigade/policies, then content-guard/policies) or path.",
    )
    p_scrub.add_argument("--dry-run", action="store_true")

    # security
    p_security = sub.add_parser("security", help="Scan agent workspace security posture.")
    security_sub = p_security.add_subparsers(dest="security_command", metavar="<security-command>")
    security_sub.required = True
    p_security_init = security_sub.add_parser("init", help="Write local security scan defaults.")
    p_security_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to configure.")
    p_security_init.add_argument("--force", action="store_true", help="Overwrite an existing security config.")
    p_security_config = security_sub.add_parser("config", help="Show local security scan config.")
    p_security_config.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_security_config.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_doctor = security_sub.add_parser("doctor", help="Check local security scanner health.")
    p_security_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_security_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_template_audit = security_sub.add_parser(
        "template-audit", help="Audit public templates and docs for private values."
    )
    p_security_template_audit.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_security_template_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_fix = security_sub.add_parser("fix", help="Apply safe local security hygiene fixes.")
    p_security_fix.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_fix.add_argument("--dry-run", action="store_true", help="Show changes without writing files.")
    p_security_review = security_sub.add_parser("review", help="Review the latest local security evidence bundle.")
    p_security_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_review.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_findings = security_sub.add_parser("findings", help="List local security findings.")
    p_security_findings.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review."
    )
    p_security_findings.add_argument(
        "--output-dir", type=Path, default=None, help="Security evidence bundle directory."
    )
    p_security_findings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_sarif = security_sub.add_parser("sarif", help="Write SARIF for an existing security report.")
    p_security_sarif.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_sarif.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_sarif.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help="SARIF output path. Defaults to security-report.sarif in the bundle.",
    )
    p_security_sarif.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_show = security_sub.add_parser("show", help="Show one local security finding.")
    p_security_show.add_argument("finding_id", help="Finding id, id prefix, or fingerprint.")
    p_security_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_show.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_enrich = security_sub.add_parser("enrich", help="Enrich an existing security report.")
    p_security_enrich.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to enrich.")
    p_security_enrich.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Security evidence bundle directory. Defaults to .brigade/security/latest.",
    )
    p_security_enrich.add_argument(
        "--report",
        dest="report_path",
        type=Path,
        default=None,
        help="Explicit security-report.json path. Defaults to --output-dir/security-report.json.",
    )
    p_security_enrich.add_argument(
        "--provider", choices=["local", "misp"], default=None, help="Override configured provider."
    )
    p_security_enrich.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_suppress = security_sub.add_parser("suppress", help="Suppress a reviewed security finding.")
    p_security_suppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to suppress.")
    p_security_suppress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_suppress.add_argument("--reason", required=True, help="Required suppression reason.")
    p_security_unsuppress = security_sub.add_parser("unsuppress", help="Remove a security finding suppression.")
    p_security_unsuppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to unsuppress.")
    p_security_unsuppress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_closeout = security_sub.add_parser("closeout", help="Write local security review closeout metadata.")
    p_security_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_closeout.add_argument(
        "--output-dir", type=Path, default=None, help="Security evidence bundle directory."
    )
    p_security_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_security_closeout.add_argument(
        "--accept-risk", action="store_true", help="Mark open findings as locally accepted risk."
    )
    p_security_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_scan = security_sub.add_parser("scan", help="Run a read-only agent workspace security scan.")
    p_security_scan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to scan.")
    p_security_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_scan.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write redacted security report artifacts to this directory.",
    )
    p_security_scan.add_argument(
        "--policy",
        choices=["personal", "public-repo", "ci", "strict"],
        default=None,
        help="Policy preset. Defaults to .brigade/security.toml or personal.",
    )
    p_security_scan.add_argument(
        "--fail-on",
        choices=["none", "low", "medium", "high", "critical"],
        default=None,
        help="Return nonzero when a finding at or above this severity exists.",
    )
    p_security_scan.add_argument(
        "--include-templates",
        dest="include_templates",
        action="store_true",
        default=None,
        help="Include public template files in scanner findings.",
    )
    p_security_scan.add_argument(
        "--no-include-templates",
        dest="include_templates",
        action="store_false",
        help="Exclude public template files from scanner findings.",
    )
    p_security_scan.add_argument(
        "--import-findings",
        action="store_true",
        help="Append findings to the local Brigade work import inbox.",
    )

    # tools
    p_tools = sub.add_parser("tools", help="Inspect local portable tool and skill catalog.")
    tools_sub = p_tools.add_subparsers(dest="tools_command", metavar="<tools-command>")
    tools_sub.required = True
    p_tools_init = tools_sub.add_parser("init", help="Write local tool catalog defaults.")
    p_tools_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_init.add_argument("--force", action="store_true", help="Overwrite an existing tools config.")
    p_tools_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_tools_defaults = tools_sub.add_parser(
        "defaults", help="Merge Brigade built-in portable tools into the local catalog."
    )
    p_tools_defaults.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_defaults.add_argument("--dry-run", action="store_true", help="Report catalog changes without writing.")
    p_tools_defaults.add_argument(
        "--force", action="store_true", help="Replace conflicting built-in ids with Brigade defaults."
    )
    p_tools_defaults.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_tools_defaults.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_list = tools_sub.add_parser("list", help="List portable tool catalog entries.")
    p_tools_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_show = tools_sub.add_parser("show", help="Show one portable tool catalog entry.")
    p_tools_show.add_argument("tool_id", help="Logical tool id.")
    p_tools_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_describe = tools_sub.add_parser("describe", help="Describe one portable tool contract.")
    p_tools_describe.add_argument("tool_id", help="Logical tool id.")
    p_tools_describe.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_describe.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_contracts = tools_sub.add_parser("contracts", help="List portable tool contracts.")
    p_tools_contracts.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_contracts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_search = tools_sub.add_parser("search", help="Search portable tool catalog entries.")
    p_tools_search.add_argument("query", help="Search query.")
    p_tools_search.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call = tools_sub.add_parser("call", help="Plan portable tool calls without executing them.")
    tools_call_sub = p_tools_call.add_subparsers(dest="tools_call_command", metavar="<tools-call-command>")
    tools_call_sub.required = True
    p_tools_call_plan = tools_call_sub.add_parser("plan", help="Plan one portable tool call without executing it.")
    p_tools_call_plan.add_argument("tool_id", help="Logical tool id.")
    p_tools_call_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_call_plan.add_argument("--args", dest="args", default=None, help="Inline JSON object arguments.")
    p_tools_call_plan.add_argument("--args-json", type=Path, default=None, help="Path to a JSON object argument file.")
    p_tools_call_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_queue = tools_call_sub.add_parser("queue", help="Queue one planned portable tool call for review.")
    p_tools_call_queue.add_argument("tool_id", help="Logical tool id.")
    p_tools_call_queue.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_queue.add_argument("--args", dest="args", default=None, help="Inline JSON object arguments.")
    p_tools_call_queue.add_argument("--args-json", type=Path, default=None, help="Path to a JSON object argument file.")
    p_tools_call_queue.add_argument("--include-blocked", action="store_true", help="Queue plans that have blockers.")
    p_tools_call_queue.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_list = tools_call_sub.add_parser("list", help="List queued portable tool calls.")
    p_tools_call_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_call_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_show = tools_call_sub.add_parser("show", help="Show one queued portable tool call.")
    p_tools_call_show.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_call_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_approve = tools_call_sub.add_parser(
        "approve", help="Approve one queued portable tool call without executing it."
    )
    p_tools_call_approve.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_approve.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_call_approve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_reject = tools_call_sub.add_parser("reject", help="Reject one queued portable tool call.")
    p_tools_call_reject.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_reject.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_call_reject.add_argument("--reason", required=True, help="Review reason.")
    p_tools_call_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_hold = tools_call_sub.add_parser("hold", help="Hold one queued portable tool call.")
    p_tools_call_hold.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_hold.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_hold.add_argument("--reason", required=True, help="Review reason.")
    p_tools_call_hold.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_run = tools_call_sub.add_parser(
        "run", help="Run one approved portable tool call and write a local receipt."
    )
    p_tools_call_run.add_argument("call_id", nargs="?", help="Call id or unique prefix.")
    p_tools_call_run.add_argument("--next", action="store_true", help="Run the oldest approved portable tool call.")
    p_tools_call_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_run = tools_sub.add_parser("run", help="Inspect portable tool execution history and replay plans.")
    tools_run_sub = p_tools_run.add_subparsers(dest="tools_run_command", metavar="<tools-run-command>")
    tools_run_sub.required = True
    p_tools_run_list = tools_run_sub.add_parser("list", help="List local portable tool execution receipts.")
    p_tools_run_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_run_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_run_show = tools_run_sub.add_parser("show", help="Show one local portable tool execution receipt.")
    p_tools_run_show.add_argument("run_id", help="Run id or unique prefix.")
    p_tools_run_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_run_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_run_latest = tools_run_sub.add_parser(
        "latest", help="Show the latest local portable tool execution receipt."
    )
    p_tools_run_latest.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_run_latest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_run_replay = tools_run_sub.add_parser(
        "replay", help="Queue a reviewed replay candidate from one run receipt."
    )
    p_tools_run_replay.add_argument("run_id", help="Run id or unique prefix.")
    p_tools_run_replay.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_run_replay.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint = tools_sub.add_parser(
        "checkpoint", help="Review and resume portable tool execution checkpoints."
    )
    tools_checkpoint_sub = p_tools_checkpoint.add_subparsers(
        dest="tools_checkpoint_command", metavar="<tools-checkpoint-command>"
    )
    tools_checkpoint_sub.required = True
    p_tools_checkpoint_list = tools_checkpoint_sub.add_parser("list", help="List local portable tool checkpoints.")
    p_tools_checkpoint_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_checkpoint_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_show = tools_checkpoint_sub.add_parser("show", help="Show one local portable tool checkpoint.")
    p_tools_checkpoint_show.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_checkpoint_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_approve = tools_checkpoint_sub.add_parser(
        "approve", help="Approve one checkpoint for explicit resume."
    )
    p_tools_checkpoint_approve.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_approve.add_argument("--choice", required=True, help="Allowed resume choice.")
    p_tools_checkpoint_approve.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_checkpoint_approve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_reject = tools_checkpoint_sub.add_parser("reject", help="Reject one checkpoint.")
    p_tools_checkpoint_reject.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_reject.add_argument("--reason", required=True, help="Review reason.")
    p_tools_checkpoint_reject.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_checkpoint_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_resume = tools_checkpoint_sub.add_parser("resume", help="Resume one approved checkpoint.")
    p_tools_checkpoint_resume.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_resume.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_checkpoint_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime = tools_sub.add_parser("runtime", help="Manage explicit local portable tool runtimes.")
    tools_runtime_sub = p_tools_runtime.add_subparsers(dest="tools_runtime_command", metavar="<tools-runtime-command>")
    tools_runtime_sub.required = True
    p_tools_runtime_init = tools_runtime_sub.add_parser("init", help="Write a local portable tool runtime config.")
    p_tools_runtime_init.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_runtime_init.add_argument("--force", action="store_true", help="Overwrite existing runtime config.")
    p_tools_runtime_list = tools_runtime_sub.add_parser("list", help="List configured portable tool runtimes.")
    p_tools_runtime_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_runtime_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_show = tools_runtime_sub.add_parser("show", help="Show one portable tool runtime.")
    p_tools_runtime_show.add_argument("runtime_id", help="Runtime id.")
    p_tools_runtime_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_runtime_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_status = tools_runtime_sub.add_parser("status", help="Show portable tool runtime process status.")
    p_tools_runtime_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_runtime_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for runtime_command in ("start", "stop", "restart"):
        p_runtime_action = tools_runtime_sub.add_parser(
            runtime_command, help=f"{runtime_command.title()} one portable tool runtime."
        )
        p_runtime_action.add_argument("runtime_id", help="Runtime id.")
        p_runtime_action.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        p_runtime_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_doctor = tools_runtime_sub.add_parser("doctor", help="Check portable tool runtime health.")
    p_tools_runtime_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_runtime_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_policy = tools_sub.add_parser("policy", help="Inspect host-local portable tool execution policy.")
    tools_policy_sub = p_tools_policy.add_subparsers(dest="tools_policy_command", metavar="<tools-policy-command>")
    tools_policy_sub.required = True
    p_tools_policy_init = tools_policy_sub.add_parser("init", help="Write a local portable tool execution policy.")
    p_tools_policy_init.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_policy_init.add_argument("--force", action="store_true", help="Overwrite existing policy config.")
    p_tools_policy_show = tools_policy_sub.add_parser("show", help="Show local portable tool execution policy.")
    p_tools_policy_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_policy_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_policy_doctor = tools_policy_sub.add_parser("doctor", help="Check portable tool execution policy health.")
    p_tools_policy_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_policy_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_parity = tools_sub.add_parser("parity", help="Inspect and close out portable tool projection parity.")
    tools_parity_sub = p_tools_parity.add_subparsers(dest="tools_parity_command", metavar="<tools-parity-command>")
    tools_parity_sub.required = True
    p_tools_parity_status = tools_parity_sub.add_parser("status", help="Show projection parity closeout state.")
    p_tools_parity_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_tools_parity_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_parity_closeout = tools_parity_sub.add_parser(
        "closeout", help="Close out current projection parity issues."
    )
    p_tools_parity_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_parity_closeout.add_argument("--reason", default="", help="Review or defer reason.")
    p_tools_parity_closeout.add_argument(
        "--defer", action="store_true", help="Mark parity issues deferred instead of reviewed."
    )
    p_tools_parity_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_pack = tools_sub.add_parser("pack", help="Build and inspect local portable tool packs.")
    tools_pack_sub = p_tools_pack.add_subparsers(dest="tools_pack_command", metavar="<tools-pack-command>")
    tools_pack_sub.required = True
    p_tools_pack_build = tools_pack_sub.add_parser("build", help="Build a local portable tool pack.")
    p_tools_pack_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_pack_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_pack_list = tools_pack_sub.add_parser("list", help="List local portable tool packs.")
    p_tools_pack_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_pack_list.add_argument("--limit", type=int, default=20, help="Maximum packs to list.")
    p_tools_pack_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_pack_show = tools_pack_sub.add_parser("show", help="Show one local portable tool pack.")
    p_tools_pack_show.add_argument("pack_id", help="Pack id or unique prefix.")
    p_tools_pack_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_pack_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_pack_import = tools_pack_sub.add_parser(
        "import", help="Import catalog entries and source files from a portable tool pack."
    )
    p_tools_pack_import.add_argument("pack", type=Path, help="Tool pack directory containing tool-pack.json.")
    p_tools_pack_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_pack_import.add_argument(
        "--force", action="store_true", help="Overwrite existing tool ids and source files."
    )
    p_tools_pack_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_pack_archive = tools_pack_sub.add_parser("archive", help="Archive one local portable tool pack.")
    p_tools_pack_archive.add_argument("pack_id", help="Pack id or unique prefix.")
    p_tools_pack_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_tools_pack_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_sync = tools_sub.add_parser("sync", help="Plan and apply reviewed portable tool projection sync.")
    tools_sync_sub = p_tools_sync.add_subparsers(dest="tools_sync_command", metavar="<tools-sync-command>")
    tools_sync_sub.required = True
    p_tools_sync_plan = tools_sync_sub.add_parser("plan", help="Plan reviewed projection sync without writing.")
    p_tools_sync_plan.add_argument("tool_id", nargs="?", help="Optional logical tool id.")
    p_tools_sync_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_sync_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_sync_apply = tools_sync_sub.add_parser("apply", help="Apply reviewed projection sync.")
    p_tools_sync_apply.add_argument("tool_id", nargs="?", help="Optional logical tool id.")
    p_tools_sync_apply.add_argument("--all", action="store_true", help="Apply all configured tool projections.")
    p_tools_sync_apply.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_sync_apply.add_argument(
        "--dry-run", action="store_true", default=True, help="Plan writes without changing files."
    )
    p_tools_sync_apply.add_argument(
        "--write", dest="dry_run", action="store_false", help="Write reviewed add-only projections."
    )
    p_tools_sync_apply.add_argument(
        "--force", action="store_true", help="Allow intentional overwrites through managed apply."
    )
    p_tools_sync_apply.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_plan = tools_sub.add_parser("plan", help="Plan portable tool projection writes.")
    p_tools_plan.add_argument("tool_id", nargs="?", help="Optional logical tool id.")
    p_tools_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_apply = tools_sub.add_parser("apply", help="Explicitly write portable tool projections.")
    p_tools_apply.add_argument("tool_id", nargs="?", help="Logical tool id.")
    p_tools_apply.add_argument("--all", action="store_true", help="Apply all configured tool projections.")
    p_tools_apply.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_apply.add_argument("--dry-run", action="store_true", help="Plan writes without changing files.")
    p_tools_apply.add_argument(
        "--force", action="store_true", help="Overwrite unmanaged or locally edited projection files."
    )
    p_tools_apply.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_doctor = tools_sub.add_parser("doctor", help="Check portable tool catalog health.")
    p_tools_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_import = tools_sub.add_parser("import-issues", help="Import tool catalog issues into the work inbox.")
    p_tools_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # handoff-template
    p_ht = sub.add_parser("handoff-template", help="Print the handoff TEMPLATE.md.")
    p_ht.add_argument(
        "--target",
        "-t",
        type=Path,
        default=None,
        help="Prefer the target's installed TEMPLATE.md when present.",
    )

    # ingest
    p_ing = sub.add_parser("ingest", help="Process writer memory-handoff inboxes into canonical memory.")
    p_ing.add_argument("--target", "-t", type=Path, default=Path("."))
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.add_argument(
        "--promote-cards",
        action="store_true",
        help="Auto-promote create-card / update-card handoffs (default off; opt-in).",
    )
    p_ing.add_argument(
        "--route-documents",
        action="store_true",
        help="Auto-route no-card handoffs to TOOLS.md/USER.md/rules/.learnings (default off; opt-in).",
    )

    # openclaw-fragments
    p_ocf = sub.add_parser("openclaw-fragments", help="Write OpenClaw config fragments for manual review.")
    p_ocf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")

    # hermes-fragments
    p_hf = sub.add_parser("hermes-fragments", help="Write Hermes adapter fragments (experimental).")
    p_hf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")

    # reconfigure
    p_recon = sub.add_parser("reconfigure", help="Adjust an existing install to a new Selection.")
    p_recon.add_argument("--target", "-t", type=Path, default=Path("."))
    p_recon.add_argument("--depth", choices=["repo", "workspace"], default=None)
    p_recon.add_argument("--harnesses", default=None)
    p_recon.add_argument("--owner", default=None)
    p_recon.add_argument("--include", dest="includes", action="append", default=[])
    p_recon.add_argument("--prune", action="store_true", help="Remove files for harnesses no longer selected.")

    parser.epilog = _grouped_epilog(sub)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    # Migrated command groups dispatch via set_defaults(func=...). The parser is
    # attached so dispatch functions can call parser.error for unreachable
    # unknown-subcommand cases (required subparsers normally prevent these).
    args._brigade_parser = parser
    func = getattr(args, "func", None)
    if func is not None:
        return func(args)

    # Legacy if-chain for not-yet-migrated groups.
    if cmd == "runs":
        from .. import runs_cmd

        if args.runs_command == "list":
            return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit)
        if args.runs_command == "latest":
            return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir)
        if args.runs_command == "show":
            return runs_cmd.show(args.run_dir)
        parser.error(f"unknown runs command: {args.runs_command}")
        return 2
    if cmd == "scrub":
        from .. import scrub as scrub_mod

        return scrub_mod.run(target=args.target, policy=args.policy, dry_run=args.dry_run)
    if cmd == "security":
        from .. import security_cmd

        if args.security_command == "init":
            return security_cmd.init(target=args.target, force=args.force)
        if args.security_command == "config":
            return security_cmd.show_config(target=args.target, json_output=args.json)
        if args.security_command == "doctor":
            return security_cmd.doctor(target=args.target, json_output=args.json)
        if args.security_command == "template-audit":
            return security_cmd.template_audit(target=args.target, json_output=args.json)
        if args.security_command == "fix":
            return security_cmd.fix(target=args.target, dry_run=args.dry_run)
        if args.security_command == "review":
            return security_cmd.review(target=args.target, output_dir=args.output_dir, json_output=args.json)
        if args.security_command == "findings":
            return security_cmd.findings(target=args.target, output_dir=args.output_dir, json_output=args.json)
        if args.security_command == "sarif":
            return security_cmd.sarif(
                target=args.target, output_dir=args.output_dir, output_path=args.output_path, json_output=args.json
            )
        if args.security_command == "show":
            return security_cmd.show(
                target=args.target,
                finding_id=args.finding_id,
                output_dir=args.output_dir,
                json_output=args.json,
            )
        if args.security_command == "enrich":
            return security_cmd.enrich(
                target=args.target,
                output_dir=args.output_dir,
                report_path=args.report_path,
                provider=args.provider,
                json_output=args.json,
            )
        if args.security_command == "suppress":
            return security_cmd.suppress(target=args.target, fingerprint=args.fingerprint, reason=args.reason)
        if args.security_command == "unsuppress":
            return security_cmd.unsuppress(target=args.target, fingerprint=args.fingerprint)
        if args.security_command == "closeout":
            return security_cmd.closeout(
                target=args.target,
                output_dir=args.output_dir,
                reason=args.reason,
                accept_risk=args.accept_risk,
                json_output=args.json,
            )
        if args.security_command == "scan":
            return security_cmd.scan(
                target=args.target,
                json_output=args.json,
                policy=args.policy,
                fail_on=args.fail_on,
                include_templates=args.include_templates,
                import_findings=args.import_findings,
                output_dir=args.output_dir,
            )
        parser.error(f"unknown security command: {args.security_command}")
        return 2
    if cmd == "tools":
        from .. import tools_cmd

        if args.tools_command == "init":
            return tools_cmd.init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
            )
        if args.tools_command == "defaults":
            return tools_cmd.defaults(
                target=args.target,
                dry_run=args.dry_run,
                force=args.force,
                update_gitignore=not args.no_gitignore,
                json_output=args.json,
            )
        if args.tools_command == "list":
            return tools_cmd.list_tools(target=args.target, json_output=args.json)
        if args.tools_command == "show":
            return tools_cmd.show(target=args.target, tool_id=args.tool_id, json_output=args.json)
        if args.tools_command == "describe":
            return tools_cmd.describe(target=args.target, tool_id=args.tool_id, json_output=args.json)
        if args.tools_command == "contracts":
            return tools_cmd.contracts(target=args.target, json_output=args.json)
        if args.tools_command == "search":
            return tools_cmd.search(target=args.target, query=args.query, json_output=args.json)
        if args.tools_command == "call":
            if args.tools_call_command == "plan":
                return tools_cmd.call_plan(
                    target=args.target,
                    tool_id=args.tool_id,
                    args=args.args,
                    args_json=args.args_json,
                    json_output=args.json,
                )
            if args.tools_call_command == "queue":
                return tools_cmd.call_queue(
                    target=args.target,
                    tool_id=args.tool_id,
                    args=args.args,
                    args_json=args.args_json,
                    include_blocked=args.include_blocked,
                    json_output=args.json,
                )
            if args.tools_call_command == "list":
                return tools_cmd.call_list(target=args.target, json_output=args.json)
            if args.tools_call_command == "show":
                return tools_cmd.call_show(target=args.target, call_id=args.call_id, json_output=args.json)
            if args.tools_call_command == "approve":
                return tools_cmd.call_approve(target=args.target, call_id=args.call_id, json_output=args.json)
            if args.tools_call_command == "reject":
                return tools_cmd.call_reject(
                    target=args.target, call_id=args.call_id, reason=args.reason, json_output=args.json
                )
            if args.tools_call_command == "hold":
                return tools_cmd.call_hold(
                    target=args.target, call_id=args.call_id, reason=args.reason, json_output=args.json
                )
            if args.tools_call_command == "run":
                return tools_cmd.call_run(
                    target=args.target,
                    call_id=args.call_id,
                    next_call=args.next,
                    json_output=args.json,
                )
            parser.error(f"unknown tools call command: {args.tools_call_command}")
            return 2
        if args.tools_command == "run":
            if args.tools_run_command == "list":
                return tools_cmd.run_list(target=args.target, json_output=args.json)
            if args.tools_run_command == "show":
                return tools_cmd.run_show(target=args.target, run_id=args.run_id, json_output=args.json)
            if args.tools_run_command == "latest":
                return tools_cmd.run_latest(target=args.target, json_output=args.json)
            if args.tools_run_command == "replay":
                return tools_cmd.run_replay(target=args.target, run_id=args.run_id, json_output=args.json)
            parser.error(f"unknown tools run command: {args.tools_run_command}")
            return 2
        if args.tools_command == "checkpoint":
            if args.tools_checkpoint_command == "list":
                return tools_cmd.checkpoint_list(target=args.target, json_output=args.json)
            if args.tools_checkpoint_command == "show":
                return tools_cmd.checkpoint_show(
                    target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                )
            if args.tools_checkpoint_command == "approve":
                return tools_cmd.checkpoint_approve(
                    target=args.target,
                    checkpoint_id=args.checkpoint_id,
                    choice=args.choice,
                    json_output=args.json,
                )
            if args.tools_checkpoint_command == "reject":
                return tools_cmd.checkpoint_reject(
                    target=args.target,
                    checkpoint_id=args.checkpoint_id,
                    reason=args.reason,
                    json_output=args.json,
                )
            if args.tools_checkpoint_command == "resume":
                return tools_cmd.checkpoint_resume(
                    target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                )
            parser.error(f"unknown tools checkpoint command: {args.tools_checkpoint_command}")
            return 2
        if args.tools_command == "runtime":
            if args.tools_runtime_command == "init":
                return tools_cmd.runtime_init(target=args.target, force=args.force)
            if args.tools_runtime_command == "list":
                return tools_cmd.runtime_list(target=args.target, json_output=args.json)
            if args.tools_runtime_command == "show":
                return tools_cmd.runtime_show(target=args.target, runtime_id=args.runtime_id, json_output=args.json)
            if args.tools_runtime_command == "status":
                return tools_cmd.runtime_status(target=args.target, json_output=args.json)
            if args.tools_runtime_command == "start":
                return tools_cmd.runtime_start(target=args.target, runtime_id=args.runtime_id, json_output=args.json)
            if args.tools_runtime_command == "stop":
                return tools_cmd.runtime_stop(target=args.target, runtime_id=args.runtime_id, json_output=args.json)
            if args.tools_runtime_command == "restart":
                return tools_cmd.runtime_restart(target=args.target, runtime_id=args.runtime_id, json_output=args.json)
            if args.tools_runtime_command == "doctor":
                return tools_cmd.runtime_doctor(target=args.target, json_output=args.json)
            parser.error(f"unknown tools runtime command: {args.tools_runtime_command}")
            return 2
        if args.tools_command == "policy":
            if args.tools_policy_command == "init":
                return tools_cmd.policy_init(target=args.target, force=args.force)
            if args.tools_policy_command == "show":
                return tools_cmd.policy_show(target=args.target, json_output=args.json)
            if args.tools_policy_command == "doctor":
                return tools_cmd.policy_doctor(target=args.target, json_output=args.json)
            parser.error(f"unknown tools policy command: {args.tools_policy_command}")
            return 2
        if args.tools_command == "parity":
            if args.tools_parity_command == "status":
                return tools_cmd.parity_status(target=args.target, json_output=args.json)
            if args.tools_parity_command == "closeout":
                return tools_cmd.parity_closeout(
                    target=args.target, reason=args.reason, defer=args.defer, json_output=args.json
                )
            parser.error(f"unknown tools parity command: {args.tools_parity_command}")
            return 2
        if args.tools_command == "pack":
            if args.tools_pack_command == "build":
                return tools_cmd.pack_build(target=args.target, json_output=args.json)
            if args.tools_pack_command == "list":
                return tools_cmd.pack_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.tools_pack_command == "show":
                return tools_cmd.pack_show(target=args.target, pack_id=args.pack_id, json_output=args.json)
            if args.tools_pack_command == "import":
                return tools_cmd.pack_import(
                    target=args.target, pack=args.pack, force=args.force, json_output=args.json
                )
            if args.tools_pack_command == "archive":
                return tools_cmd.pack_archive(target=args.target, pack_id=args.pack_id, json_output=args.json)
            parser.error(f"unknown tools pack command: {args.tools_pack_command}")
            return 2
        if args.tools_command == "sync":
            if args.tools_sync_command == "plan":
                return tools_cmd.sync_plan(target=args.target, tool_id=args.tool_id, json_output=args.json)
            if args.tools_sync_command == "apply":
                return tools_cmd.sync_apply(
                    target=args.target,
                    tool_id=args.tool_id,
                    all_tools=args.all,
                    dry_run=args.dry_run,
                    force=args.force,
                    json_output=args.json,
                )
            parser.error(f"unknown tools sync command: {args.tools_sync_command}")
            return 2
        if args.tools_command == "plan":
            return tools_cmd.plan(target=args.target, tool_id=args.tool_id, json_output=args.json)
        if args.tools_command == "apply":
            return tools_cmd.apply(
                target=args.target,
                tool_id=args.tool_id,
                all_tools=args.all,
                dry_run=args.dry_run,
                force=args.force,
                json_output=args.json,
            )
        if args.tools_command == "doctor":
            return tools_cmd.doctor(target=args.target, json_output=args.json)
        if args.tools_command == "import-issues":
            return tools_cmd.import_issues(target=args.target, json_output=args.json)
        parser.error(f"unknown tools command: {args.tools_command}")
        return 2
    if cmd == "handoff-template":
        from .. import handoff as handoff_mod

        return handoff_mod.run(target=args.target)
    if cmd == "ingest":
        from .. import ingest as ingest_mod

        return ingest_mod.run(
            target=args.target,
            dry_run=args.dry_run,
            promote_cards=args.promote_cards,
            route_documents=args.route_documents,
        )
    if cmd == "openclaw-fragments":
        from .. import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="openclaw")
    if cmd == "hermes-fragments":
        from .. import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="hermes")
    if cmd == "reconfigure":
        from ..config import load_config
        from ..reconfigure import reconfigure as _reconfigure
        from ..selection import Selection, KNOWN_HARNESSES, resolve_owner

        existing = load_config(args.target)
        if existing is None:
            print("error: no .brigade/config.json in target. Run `brigade init` first.", file=sys.stderr)
            return 2

        depth = args.depth or existing.selection.depth
        if args.harnesses is None:
            harnesses = list(existing.selection.harnesses)
        elif args.harnesses == "none":
            harnesses = []
        else:
            harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
        for h in harnesses:
            if h not in KNOWN_HARNESSES:
                print(f"error: unknown harness {h!r}", file=sys.stderr)
                return 2
        owner = resolve_owner(harnesses, override=args.owner)
        includes = list(args.includes) if args.includes else list(existing.selection.includes)
        new_sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=includes)
        return _reconfigure(args.target, new_selection=new_sel, prune=args.prune)

    parser.error(f"unknown command: {cmd}")
    return 2


def main_deprecated(argv=None) -> int:
    print(
        "warning: the 'solo-mise' command is deprecated; use 'brigade' instead.",
        file=sys.stderr,
    )
    return main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
