"""brigade work command group."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ... import extras as _extras_mod
from ...dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from ...work_cmd import TASK_PRIORITIES, TASK_TYPES
from .. import extras as _extras_cli


def register(sub: argparse._SubParsersAction) -> None:
    # work
    p_work = sub.add_parser("work", help="Inspect and manage a daily Brigade work session.")
    work_sub = p_work.add_subparsers(dest="work_command", metavar="<work-command>")
    work_sub.required = True
    p_work_status = work_sub.add_parser("status", help="Show current repo and dogfood work state.")
    p_work_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_status.add_argument("--limit", type=int, default=12, help="Maximum dirty file entries to show.")
    p_work_doctor = work_sub.add_parser("doctor", help="Check whether the daily work loop is ready.")
    p_work_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_bootstrap = work_sub.add_parser("bootstrap", help="Initialize and verify the daily work loop.")
    p_work_bootstrap.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to prepare.")
    p_work_bootstrap.add_argument("--artifacts-dir", type=Path, default=None, help="Directory for dogfood artifacts.")
    p_work_bootstrap.add_argument("--handoff-inbox", type=Path, default=None, help="Memory Handoff inbox.")
    p_work_bootstrap.add_argument("--force", action="store_true", help="Overwrite an existing dogfood config.")
    p_work_bootstrap.add_argument("--no-handoff", action="store_true", help="Disable work handoff defaults.")
    p_work_bootstrap.add_argument(
        "--no-inspect", action="store_true", help="Do not inspect dogfood artifacts by default."
    )
    p_work_bootstrap.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox for dogfood runs.",
    )
    p_work_bootstrap.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout."
    )
    p_work_bootstrap.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_resume = work_sub.add_parser("resume", help="Show the current work handoff point and next command.")
    p_work_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_brief = work_sub.add_parser("brief", help="Show the daily work brief and suggested next command.")
    p_work_brief.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_brief.add_argument("--limit", type=int, default=3, help="Maximum recent sessions to include.")
    p_work_brief.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep = work_sub.add_parser("sweep", help="Run an explicit daily scanner sweep.")
    p_work_sweep.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_sweep.add_argument("--scanner", default=None, help="Run one scanner id instead of due scanners.")
    p_work_sweep.add_argument("--all", action="store_true", help="Run all configured scanners.")
    p_work_sweep.add_argument("--include-disabled", action="store_true", help="Allow disabled scanners to run.")
    p_work_sweep.add_argument(
        "--force", action="store_true", help="Run even when another scanner receipt is marked running."
    )
    p_work_sweep.add_argument(
        "--no-ingest", action="store_true", help="Do not ingest configured scanner import output."
    )
    p_work_sweep.add_argument("--reason", default=None, help="Review closeout reason when using `closeout`.")
    p_work_sweep.add_argument(
        "--defer", action="append", default=[], help="Defer one pending import during sweep closeout. May be repeated."
    )
    p_work_sweep.add_argument(
        "--defer-all", action="store_true", help="Defer every pending import during sweep closeout."
    )
    p_work_sweep.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep.add_argument(
        "sweep_args", nargs="*", help="Use `closeout <sweep-id|latest>` to mark a sweep reviewed."
    )
    p_work_sweeps = work_sub.add_parser("sweeps", help="List scanner sweep reports.")
    p_work_sweeps.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_sweeps.add_argument("--limit", type=int, default=20, help="Maximum sweeps to list.")
    p_work_sweeps.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_plans = work_sub.add_parser("plans", help="List task plan artifacts.")
    p_work_plans.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_plans.add_argument("--limit", type=int, default=20, help="Maximum plan artifacts to list.")
    p_work_plans.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_plan_promote = work_sub.add_parser(
        "plan-promote",
        help="Promote an accepted plan to a local DRAFT proposal (never installs).",
    )
    p_work_plan_promote.add_argument("task_id", help="Task id or unique prefix.")
    p_work_plan_promote.add_argument(
        "--as",
        dest="as_kind",
        choices=["template", "rule", "skill"],
        required=True,
        help="Draft proposal kind to generate.",
    )
    p_work_plan_promote.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_plan_promote.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_plan_proposals = work_sub.add_parser("plan-proposals", help="List local draft plan proposals.")
    p_work_plan_proposals.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_plan_proposals.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep_show = work_sub.add_parser("sweep-show", help="Show one scanner sweep report.")
    p_work_sweep_show.add_argument("sweep_id", help="Sweep id or unique prefix.")
    p_work_sweep_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_sweep_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep_review = work_sub.add_parser("sweep-review", help="Review imports created by one scanner sweep.")
    p_work_sweep_review.add_argument("sweep_id", help="Sweep id, unique prefix, or latest.")
    p_work_sweep_review.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_sweep_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify = work_sub.add_parser("verify", help="Plan and run local work verification.")
    verify_sub = p_work_verify.add_subparsers(dest="verify_command", metavar="<verify-command>")
    verify_sub.required = True
    p_work_verify_plan = verify_sub.add_parser("plan", help="Plan local verification without running commands.")
    p_work_verify_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_verify_plan.add_argument(
        "--command",
        dest="verify_commands",
        action="append",
        default=None,
        help="Verification command. May be repeated.",
    )
    p_work_verify_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_run = verify_sub.add_parser("run", help="Run local verification commands and write a receipt.")
    p_work_verify_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_verify_run.add_argument(
        "--command",
        dest="verify_commands",
        action="append",
        default=None,
        help="Verification command. May be repeated. Mutually exclusive with --argv-json.",
    )
    p_work_verify_run.add_argument(
        "--argv-json",
        dest="verify_argv_json",
        default=None,
        metavar="JSON",
        help=(
            "JSON array of argv strings for one verification command, executed directly "
            "with shell=False (bypasses the shell-metacharacter check, for commands whose "
            "arguments need punctuation like ';' or quotes). Mutually exclusive with --command."
        ),
    )
    p_work_verify_run.add_argument("--timeout", type=int, default=900, help="Timeout per command in seconds.")
    p_work_verify_run.add_argument(
        "--capture",
        default=None,
        metavar="ARTIFACT_ID",
        help="Capture this run's outcome for ARTIFACT_ID in the same step (closes the loop; no separate capture).",
    )
    p_work_verify_run.add_argument(
        "--capture-kind", default="skill", choices=["skill", "card"], help="Artifact kind for --capture."
    )
    p_work_verify_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_runs = verify_sub.add_parser("runs", help="List local work verification receipts.")
    p_work_verify_runs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_verify_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_verify_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_show = verify_sub.add_parser("show", help="Show one local work verification receipt.")
    p_work_verify_show.add_argument("run_id", help="Run id, unique prefix, or latest.")
    p_work_verify_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_verify_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_closeout = work_sub.add_parser("closeout", help="Write a local work closeout receipt.")
    p_work_closeout.add_argument("session_id", help="Work session id, unique prefix, or latest.")
    p_work_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_acceptance = work_sub.add_parser("acceptance", help="Summarize task acceptance coverage.")
    p_work_acceptance.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_acceptance.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_inbox = work_sub.add_parser("inbox", help="Review scanner-ready work imports.")
    p_work_inbox.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_inbox.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_inbox.add_argument("--limit", type=int, default=20, help="Maximum imports to show.")
    inbox_sub = p_work_inbox.add_subparsers(dest="inbox_command", metavar="<inbox-command>")
    p_work_inbox_doctor = inbox_sub.add_parser("doctor", help="Check scanner inbox hygiene.")
    p_work_inbox_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_inbox_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_inbox_archive = inbox_sub.add_parser("archive", help="Archive old closed scanner inbox imports.")
    p_work_inbox_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_inbox_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup = work_sub.add_parser("backup", help="Inspect local backup health summaries.")
    backup_sub = p_work_backup.add_subparsers(dest="backup_command", metavar="<backup-command>")
    backup_sub.required = True
    p_work_backup_init = backup_sub.add_parser("init", help="Write a local backup health config.")
    p_work_backup_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_backup_init.add_argument("--force", action="store_true", help="Overwrite an existing backup config.")
    p_work_backup_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_backup_contract = backup_sub.add_parser("contract", help="Show the backup summary producer JSON contract.")
    p_work_backup_contract.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_backup_contract.add_argument("--destination", help="Limit the contract to one destination id.")
    p_work_backup_contract.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_status = backup_sub.add_parser("status", help="Show local backup health status.")
    p_work_backup_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_backup_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_doctor = backup_sub.add_parser("doctor", help="Check local backup health summaries.")
    p_work_backup_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_backup_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_import = backup_sub.add_parser(
        "import-issues", help="Import backup health issues into the work inbox."
    )
    p_work_backup_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_backup_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_closeout = backup_sub.add_parser("closeout", help="Write local backup health closeout metadata.")
    p_work_backup_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_backup_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_work_backup_closeout.add_argument(
        "--defer", action="store_true", help="Mark current backup issues deferred instead of reviewed."
    )
    p_work_backup_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners = work_sub.add_parser("scanners", help="Inspect local scanner registry and schedule plans.")
    scanners_sub = p_work_scanners.add_subparsers(dest="scanners_command", metavar="<scanners-command>")
    scanners_sub.required = True
    p_work_scanners_init = scanners_sub.add_parser("init", help="Write a local scanner registry config.")
    p_work_scanners_init.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_scanners_init.add_argument("--force", action="store_true", help="Overwrite an existing scanner config.")
    p_work_scanners_init.add_argument(
        "--no-gitignore", action="store_true", help="Do not update the target .gitignore."
    )
    p_work_scanners_list = scanners_sub.add_parser("list", help="List configured local scanners.")
    p_work_scanners_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_show = scanners_sub.add_parser("show", help="Show one configured scanner.")
    p_work_scanners_show.add_argument("scanner_id", help="Scanner id.")
    p_work_scanners_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_plan = scanners_sub.add_parser("plan", help="Plan scanner run windows without executing scanners.")
    p_work_scanners_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_run = scanners_sub.add_parser("run", help="Run configured local scanners explicitly.")
    p_work_scanners_run.add_argument("scanner_id", nargs="?", default=None, help="Scanner id to run.")
    p_work_scanners_run.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_scanners_run.add_argument("--all", action="store_true", help="Run all configured scanners.")
    p_work_scanners_run.add_argument("--due", action="store_true", help="Run due scanners only.")
    p_work_scanners_run.add_argument("--include-disabled", action="store_true", help="Allow disabled scanners to run.")
    p_work_scanners_run.add_argument(
        "--force", action="store_true", help="Run even when another scanner receipt is marked running."
    )
    p_work_scanners_run.add_argument(
        "--ingest-output",
        action="store_true",
        help="Validate and ingest configured JSONL output after successful runs.",
    )
    p_work_scanners_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_runs = scanners_sub.add_parser("runs", help="List local scanner run receipts.")
    p_work_scanners_runs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_scanners_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_run_show = scanners_sub.add_parser("run-show", help="Show one scanner run receipt.")
    p_work_scanners_run_show.add_argument("run_id", help="Run id or unique prefix.")
    p_work_scanners_run_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_run_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_doctor = scanners_sub.add_parser("doctor", help="Check scanner registry health.")
    p_work_scanners_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_scanners_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_doctor.add_argument(
        "--import-issues", action="store_true", help="Import scanner health issues into the work inbox."
    )
    p_work_review = work_sub.add_parser("review", help="Run explicit local code review producers.")
    review_sub = p_work_review.add_subparsers(dest="review_command", metavar="<review-command>")
    review_sub.required = True
    p_work_review_init = review_sub.add_parser("init", help="Write local code review producer config.")
    p_work_review_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_init.add_argument("--force", action="store_true", help="Overwrite an existing review config.")
    p_work_review_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_review_plan = review_sub.add_parser(
        "plan", help="Plan configured code review producers without running them."
    )
    p_work_review_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_review_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_run = review_sub.add_parser("run", help="Run configured local code review producers explicitly.")
    p_work_review_run.add_argument("reviewer_id", nargs="?", default=None, help="Reviewer id to run.")
    p_work_review_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_run.add_argument("--all", action="store_true", help="Run all configured reviewers.")
    p_work_review_run.add_argument("--include-disabled", action="store_true", help="Allow disabled reviewers to run.")
    p_work_review_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_runs = review_sub.add_parser("runs", help="List local code review run receipts.")
    p_work_review_runs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_review_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_review_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_show = review_sub.add_parser("show", help="Show one code review run receipt.")
    p_work_review_show.add_argument("run_id", help="Run id or unique prefix.")
    p_work_review_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_review_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_import = review_sub.add_parser(
        "import-findings", help="Import normalized review findings into the work inbox."
    )
    p_work_review_import.add_argument("run_id", help="Run id or unique prefix.")
    p_work_review_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_review_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_review_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_findings = review_sub.add_parser("findings", help="List imported code review findings.")
    p_work_review_findings.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_review_findings.add_argument("--run-id", default=None, help="Limit findings to one review run id.")
    p_work_review_findings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_finding_show = review_sub.add_parser("finding-show", help="Show one imported code review finding.")
    p_work_review_finding_show.add_argument("finding_id", help="Finding id, import id, or unique prefix.")
    p_work_review_finding_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_review_finding_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_closeout = review_sub.add_parser("closeout", help="Summarize one code review run's resolution state.")
    p_work_review_closeout.add_argument("run_id", help="Run id, unique prefix, or latest.")
    p_work_review_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_review_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    if _extras_mod.enabled():
        _register_phases(work_sub)
    else:
        # The phase-execution ledger is operator-suite surface; it gates
        # like a top-level extras command.
        _extras_cli.register_stub(work_sub, "phases", invocation="work phases")
    p_work_next = work_sub.add_parser("next", help="Show the next daily work task and suggested command.")
    p_work_next.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_tasks = work_sub.add_parser("tasks", help="List pending work tasks.")
    p_work_tasks.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_tasks.add_argument("--all", action="store_true", help="Include completed tasks.")
    p_work_tasks.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_task = work_sub.add_parser("task", help="Add, show, or complete one work task.")
    task_sub = p_work_task.add_subparsers(dest="task_command", metavar="<task-command>")
    task_sub.required = True
    p_work_task_add = task_sub.add_parser("add", help="Add a pending work task.")
    p_work_task_add.add_argument("text", nargs="*", help="Task text.")
    p_work_task_add.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_task_add.add_argument("--from-next", action="store_true", help="Add the latest extracted dogfood next step.")
    p_work_task_add.add_argument("--from-issue", default=None, help="Import a GitHub issue by URL or number using gh.")
    p_work_task_add.add_argument("--type", choices=TASK_TYPES, default="task", help="Task type.")
    p_work_task_add.add_argument("--priority", choices=TASK_PRIORITIES, default="normal", help="Task priority.")
    p_work_task_add.add_argument(
        "--acceptance",
        action="append",
        default=[],
        help="Acceptance criterion. Repeat for multiple criteria.",
    )
    p_work_task_add.add_argument(
        "--template",
        choices=["vertical-slice", "bugfix", "red-green-refactor", "docs", "security-follow-up"],
        default=None,
        help="Add template acceptance criteria and planning guidance.",
    )
    p_work_task_show = task_sub.add_parser("show", help="Show one work task.")
    p_work_task_show.add_argument("task_id", help="Task id or unique prefix.")
    p_work_task_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_task_plan = task_sub.add_parser("plan", help="Show task acceptance criteria and run plan.")
    p_work_task_plan.add_argument("task_id", help="Task id or unique prefix.")
    p_work_task_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_task_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_task_plan.add_argument(
        "--write", action="store_true", help="Write or update the plan artifact (plan.md + JSON receipt)."
    )
    p_work_task_plan.add_argument(
        "--assumption", dest="assumptions", action="append", default=[], help="Planning assumption. May be repeated."
    )
    p_work_task_plan.add_argument(
        "--risk", dest="risks", action="append", default=[], help="Planning risk. May be repeated."
    )
    p_work_task_plan.add_argument(
        "--source",
        dest="sources",
        action="append",
        default=[],
        help="Source context ref, link, or note. May be repeated.",
    )
    p_work_task_plan.add_argument(
        "--next-command", dest="next_command", default=None, help="Next safe command to record in the plan."
    )
    p_work_task_plan.add_argument("--title", default=None, help="Plan title (defaults to the task text).")
    p_work_task_plan.add_argument("--accept", action="store_true", help="Mark the plan artifact accepted.")
    p_work_task_plan.add_argument(
        "--meta", action="store_true", help="Write the meta-plan (plan-for-the-plan) artifact."
    )
    p_work_task_plan.add_argument(
        "--step", dest="step", action="append", default=[], help="Planning step. May be repeated."
    )
    p_work_task_plan.add_argument(
        "--from-research",
        dest="from_research",
        metavar="RUN_ID",
        default=None,
        help="Attach a completed research run report as quarantined (untrusted-web) plan evidence.",
    )
    p_work_task_done = task_sub.add_parser("done", help="Mark one work task done.")
    p_work_task_done.add_argument("task_id", help="Task id or unique prefix.")
    p_work_task_done.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import = work_sub.add_parser("import", help="Add, list, show, or promote scanner-ready work imports.")
    import_sub = p_work_import.add_subparsers(dest="import_command", metavar="<import-command>")
    import_sub.required = True
    p_work_import_add = import_sub.add_parser("add", help="Add a local work import.")
    p_work_import_add.add_argument("text", nargs="+", help="Import text.")
    p_work_import_add.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_add.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default="task",
        help="Import kind.",
    )
    p_work_import_add.add_argument(
        "--source", default="manual", help="Import source such as slack, discord, or memory-care."
    )
    p_work_import_add.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="Metadata as key=value. May be repeated.",
    )
    p_work_import_context = import_sub.add_parser("context", help="Inbox raw external context as untrusted data.")
    p_work_import_context.add_argument("text", nargs="*", help="Raw context text.")
    p_work_import_context.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_context.add_argument("--source", default="manual", help="Where the context came from.")
    p_work_import_context.add_argument(
        "--kind",
        choices=["link", "transcript", "error", "issue", "note"],
        default="note",
        dest="context_kind",
        help="Context kind.",
    )
    p_work_import_context.add_argument("--from-file", type=Path, default=None, help="Read context body from a file.")
    p_work_import_context.add_argument(
        "--from-miseledger",
        default=None,
        metavar="QUERY",
        help="Fetch a Brigade-source evidence bundle from MiseLedger and print it as untrusted context.",
    )
    p_work_import_context.add_argument("--limit", type=int, default=5, help="Maximum MiseLedger results to fetch.")
    p_work_import_context.add_argument(
        "--max-chars", type=int, default=20000, help="Maximum characters of body to fence."
    )
    p_work_import_context.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_list = import_sub.add_parser("list", help="List local work imports.")
    p_work_import_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_list.add_argument("--all", action="store_true", help="Include promoted imports.")
    p_work_import_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_list.add_argument("--limit", type=int, default=20, help="Maximum imports to show.")
    p_work_import_list.add_argument("--source", default=None, help="Filter by import source.")
    p_work_import_list.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Filter by import kind.",
    )
    p_work_import_list.add_argument(
        "--metadata", action="append", default=[], help="Filter by metadata key=value. May be repeated."
    )
    p_work_import_validate = import_sub.add_parser("validate", help="Validate a work import JSONL file.")
    p_work_import_validate.add_argument("input_path", type=Path, help="JSONL file to validate.")
    p_work_import_validate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_ingest = import_sub.add_parser("ingest", help="Validate and append a work import JSONL file.")
    p_work_import_ingest.add_argument("input_path", type=Path, help="JSONL file to ingest.")
    p_work_import_ingest.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_ingest.add_argument(
        "--dry-run", action="store_true", help="Validate and report without writing imports."
    )
    p_work_import_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_issue_repairs = import_sub.add_parser(
        "issue-repairs", help="Import repair tasks for stale issue-backed local tasks."
    )
    p_work_import_issue_repairs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_issue_repairs.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_issue_repairs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_plan = import_sub.add_parser("plan", help="Preview the task or action a work import would create.")
    p_work_import_plan.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_plan_handoff = import_sub.add_parser(
        "plan-handoff", help="Preview the Memory Handoff a work import would create."
    )
    p_work_import_plan_handoff.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_plan_handoff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_plan_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_memory_care = import_sub.add_parser("memory-care", help="Import memory-care refresh queue entries.")
    p_work_import_memory_care.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_memory_care.add_argument(
        "--queue",
        type=Path,
        default=None,
        help="Refresh queue JSON. Defaults to memory/cards/decay/refresh-queue.json under target.",
    )
    p_work_import_memory_care.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_memory_care.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_memory_refresh = import_sub.add_parser("memory-refresh", help="Import memory refresh candidates.")
    p_work_import_memory_refresh.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_memory_refresh.add_argument(
        "--queue",
        type=Path,
        default=None,
        help="Refresh queue JSON. Defaults to memory/cards/decay/refresh-queue.json under target.",
    )
    p_work_import_memory_refresh.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_memory_refresh.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_chat_sweep = import_sub.add_parser("chat-sweep", help="Import chat memory sweep issues.")
    p_work_import_chat_sweep.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_chat_sweep.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        default=None,
        help="Chat memory sweep JSON. Defaults to .brigade/chat-memory-sweeps/latest.json under target.",
    )
    p_work_import_chat_sweep.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_chat_sweep.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_content_guard = import_sub.add_parser(
        "content-guard", help="Import Content Guard scan findings as reviewed work imports."
    )
    p_work_import_content_guard.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_content_guard.add_argument(
        "--scan-target", type=Path, default=None, help="Path to scan. Defaults to --target."
    )
    p_work_import_content_guard.add_argument(
        "--policy", default="public-repo", help="Content Guard policy name or path."
    )
    p_work_import_content_guard.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_content_guard.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_triage = import_sub.add_parser("triage", help="Group pending imports by source and kind.")
    p_work_import_triage.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_triage.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_triage.add_argument("--limit", type=int, default=50, help="Maximum imports per group to show.")
    p_work_import_triage.add_argument("--source", default=None, help="Filter by import source.")
    p_work_import_triage.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Filter by import kind.",
    )
    p_work_import_triage.add_argument(
        "--metadata", action="append", default=[], help="Filter by metadata key=value. May be repeated."
    )
    p_work_import_provenance = import_sub.add_parser("provenance", help="Audit producer import provenance fields.")
    p_work_import_provenance.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_provenance.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_show = import_sub.add_parser("show", help="Show one work import.")
    p_work_import_show.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_import_promote = import_sub.add_parser("promote", help="Promote one work import into the task ledger.")
    p_work_import_promote.add_argument("import_id", nargs="?", help="Import id or unique prefix.")
    p_work_import_promote.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_promote.add_argument(
        "--all", action="store_true", help="Promote all pending imports matching filters."
    )
    p_work_import_promote.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Limit --all promotion to one kind.",
    )
    p_work_import_promote.add_argument("--source", default=None, help="Limit --all promotion to one source.")
    p_work_import_promote.add_argument(
        "--metadata", action="append", default=[], help="Limit --all promotion by metadata key=value. May be repeated."
    )
    p_work_import_promote.add_argument(
        "--run", action="store_true", help="Promote one task import and immediately run it."
    )
    p_work_import_promote_handoff = import_sub.add_parser(
        "promote-handoff", help="Promote one reviewed work import into a Memory Handoff draft."
    )
    p_work_import_promote_handoff.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_promote_handoff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_promote_handoff.add_argument(
        "--run", action="store_true", help="For task imports, use the existing promote-and-run path."
    )
    p_work_import_promote_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_dismiss = import_sub.add_parser("dismiss", help="Dismiss one pending work import.")
    p_work_import_dismiss.add_argument("import_id", nargs="?", help="Import id or unique prefix.")
    p_work_import_dismiss.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_import_dismiss.add_argument(
        "--all", action="store_true", help="Dismiss all pending imports matching filters."
    )
    p_work_import_dismiss.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Limit --all dismissal to one kind.",
    )
    p_work_import_dismiss.add_argument("--source", default=None, help="Limit --all dismissal to one source.")
    p_work_import_dismiss.add_argument(
        "--metadata", action="append", default=[], help="Limit --all dismissal by metadata key=value. May be repeated."
    )
    p_work_import_dismiss.add_argument("--reason", default=None, help="Optional dismiss reason.")
    p_work_list = work_sub.add_parser("list", help="List recent Brigade work sessions.")
    p_work_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_list.add_argument("--limit", type=int, default=10, help="Maximum sessions to show.")
    p_work_latest = work_sub.add_parser("latest", help="Show the latest Brigade work session.")
    p_work_latest.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_show = work_sub.add_parser("show", help="Show one Brigade work session.")
    p_work_show.add_argument("session", help="Session id or path.")
    p_work_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_recap = work_sub.add_parser("recap", help="Summarize recent Brigade work sessions.")
    p_work_recap.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_recap.add_argument("--limit", type=int, default=5, help="Maximum sessions to include.")
    p_work_recap.add_argument("--since", default=None, help="Only include sessions since YYYY-MM-DD.")
    p_work_run = work_sub.add_parser("run", help="Start a work session, run dogfood, end it, and recap.")
    p_work_run.add_argument("task", nargs="*", help="Dogfood task. Defaults to the standard next-slice review.")
    p_work_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace for the session.")
    p_work_run.add_argument("--title", default=None, help="Work session title. Defaults to the task text.")
    p_work_run.add_argument("--output-dir", type=Path, default=None, help="Directory for dogfood run artifacts.")
    p_work_run.add_argument("--handoff-inbox", type=Path, default=None, help="Memory Handoff inbox.")
    p_work_run.add_argument("--no-handoff", action="store_true", help="Do not write a work-session Memory Handoff.")
    p_work_run.add_argument(
        "--dogfood-handoff",
        action="store_true",
        help="Also let the underlying dogfood run write its own Memory Handoff.",
    )
    p_work_run.add_argument("--no-inspect", action="store_true", help="Do not print the dogfood artifact summary.")
    p_work_run.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox for the underlying dogfood run.",
    )
    p_work_run.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout.")
    p_work_run.add_argument(
        "--recap-limit", type=int, default=1, help="Maximum sessions to include in the final recap."
    )
    p_work_run.add_argument(
        "--queue-next", action="store_true", help="Queue the extracted next step after a successful run."
    )
    p_work_start = work_sub.add_parser("start", help="Start a local Brigade work session.")
    p_work_start.add_argument("title", nargs="*", help="Optional session title.")
    p_work_start.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace for the session.")
    p_work_start.add_argument("--force", action="store_true", help="Replace an existing active session pointer.")
    p_work_note = work_sub.add_parser("note", help="Append a note to the active Brigade work session.")
    p_work_note.add_argument("text", nargs="+", help="Note text.")
    p_work_note.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace for the session.")
    p_work_end = work_sub.add_parser("end", help="End the active local Brigade work session.")
    p_work_end.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace for the session.")
    p_work_end.add_argument("--note", default=None, help="Optional closing note.")
    p_work_end.add_argument("--handoff", action="store_true", help="Write a Memory Handoff for the ended session.")
    p_work_end.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to configured dogfood inbox or .codex/memory-handoffs.",
    )
    p_work.set_defaults(func=dispatch)
