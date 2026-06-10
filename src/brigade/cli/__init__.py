"""brigade command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import __version__
from ..dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from ..work_cmd import TASK_PRIORITIES, TASK_TYPES

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
    chat as _chat_group,
)


def _build_parser() -> argparse.ArgumentParser:
    from .. import learn_cmd, projects_cmd

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

    # memory
    p_memory = sub.add_parser("memory", help="Inspect local memory maintenance workflows.")
    memory_sub = p_memory.add_subparsers(dest="memory_command", metavar="<memory-command>")
    memory_sub.required = True
    p_memory_care = memory_sub.add_parser("care", help="Scan local memory cards for refresh risk.")
    memory_care_sub = p_memory_care.add_subparsers(dest="memory_care_command", metavar="<memory-care-command>")
    memory_care_sub.required = True
    p_memory_care_init = memory_care_sub.add_parser("init", help="Write local memory-care config.")
    p_memory_care_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_memory_care_init.add_argument("--force", action="store_true", help="Overwrite an existing memory-care config.")
    p_memory_care_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_memory_care_scan = memory_care_sub.add_parser("scan", help="Scan local memory cards without editing them.")
    p_memory_care_scan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_memory_care_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_plan_fixes = memory_care_sub.add_parser(
        "plan-fixes", help="Plan safe memory-care metadata fixes without writing files."
    )
    p_memory_care_plan_fixes.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_memory_care_plan_fixes.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_backfill = memory_care_sub.add_parser(
        "backfill", help="Backfill missing reviewed/freshness card metadata from git history (dry-run by default)."
    )
    p_memory_care_backfill.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_memory_care_backfill.add_argument(
        "--apply", action="store_true", help="Write the derived metadata into card frontmatter and record a receipt."
    )
    p_memory_care_backfill.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_status = memory_care_sub.add_parser("status", help="Show local memory-care status.")
    p_memory_care_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_memory_care_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_doctor = memory_care_sub.add_parser("doctor", help="Check local memory-care health.")
    p_memory_care_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_memory_care_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_import = memory_care_sub.add_parser(
        "import-issues", help="Import memory-care issues into the work inbox."
    )
    p_memory_care_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_memory_care_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_memory_care_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_closeout = memory_care_sub.add_parser("closeout", help="Write local memory-care closeout metadata.")
    p_memory_care_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_memory_care_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_memory_care_closeout.add_argument(
        "--defer", action="store_true", help="Mark current queue deferred instead of reviewed."
    )
    p_memory_care_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

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
        help="Verification command. May be repeated.",
    )
    p_work_verify_run.add_argument("--timeout", type=int, default=900, help="Timeout per command in seconds.")
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
    p_work_phases = work_sub.add_parser("phases", help="Plan and inspect auditable phase execution records.")
    phases_sub = p_work_phases.add_subparsers(dest="phases_command", metavar="<phases-command>")
    phases_sub.required = True
    p_work_phases_init = phases_sub.add_parser("init", help="Initialize the local phase execution ledger.")
    p_work_phases_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_plan = phases_sub.add_parser("plan", help="Plan one phase or a range of phases.")
    p_work_phases_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_plan.add_argument(
        "--phase-id", "--phase", dest="phase_id", default=None, help="Phase id to create, such as phase-165."
    )
    p_work_phases_plan.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range to create, such as 165-170."
    )
    p_work_phases_plan.add_argument("--title", default=None, help="Phase title.")
    p_work_phases_plan.add_argument("--goal", dest="source_goal", default=None, help="Source goal text or label.")
    p_work_phases_plan.add_argument("--grouped", action="store_true", help="Declare an explicit grouped phase range.")
    p_work_phases_plan.add_argument("--force", action="store_true", help="Overwrite existing phase records.")
    p_work_phases_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_list = phases_sub.add_parser("list", help="List local phase records.")
    p_work_phases_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_schema = phases_sub.add_parser("schema", help="Show phase ledger JSON contracts.")
    p_work_phases_schema.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_status = phases_sub.add_parser("status", help="Summarize phase ledger range status.")
    p_work_phases_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_status.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_next = phases_sub.add_parser("next", help="Show the next open phase.")
    p_work_phases_next.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_next.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_show = phases_sub.add_parser("show", help="Show one local phase record.")
    p_work_phases_show.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_start = phases_sub.add_parser("start", help="Mark one phase in progress.")
    p_work_phases_start.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_complete = phases_sub.add_parser("complete", help="Attach completion evidence to one phase.")
    p_work_phases_complete.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_complete.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_complete.add_argument(
        "--status",
        choices=["implemented", "verified", "committed", "pushed"],
        default="implemented",
        help="Completion status.",
    )
    p_work_phases_complete.add_argument("--summary", default=None, help="Implementation summary.")
    p_work_phases_complete.add_argument(
        "--file", dest="files_changed", action="append", default=[], help="Changed file. May be repeated."
    )
    p_work_phases_complete.add_argument(
        "--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated."
    )
    p_work_phases_complete.add_argument("--test-result", default=None, help="Test result summary.")
    p_work_phases_complete.add_argument("--commit", dest="commit_hash", default=None, help="Commit hash.")
    p_work_phases_complete.add_argument("--push-ref", default=None, help="Push ref.")
    p_work_phases_complete.add_argument(
        "--deferred-item", action="append", default=[], help="Deferred item. May be repeated."
    )
    p_work_phases_complete.add_argument(
        "--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation."
    )
    p_work_phases_complete.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_defer = phases_sub.add_parser("defer", help="Defer one phase with a reason.")
    p_work_phases_defer.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_defer.add_argument(
        "--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation."
    )
    p_work_phases_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_closeout = phases_sub.add_parser("closeout", help="Review or close out phase records.")
    p_work_phases_closeout.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed", help="Closeout state."
    )
    p_work_phases_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_compare = phases_sub.add_parser("compare", help="Compare phase evidence against current local state.")
    p_work_phases_compare.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_reconcile = phases_sub.add_parser(
        "reconcile", help="Reconcile phase commit and push evidence against local git state."
    )
    p_work_phases_reconcile.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_reconcile.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_privacy = phases_sub.add_parser(
        "privacy", help="Scan phase evidence for protected private/reference values."
    )
    p_work_phases_privacy.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_privacy.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_handoff = phases_sub.add_parser("handoff", help="Draft a Memory Handoff from phase evidence.")
    p_work_phases_handoff.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_handoff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_handoff.add_argument("--lint", action="store_true", help="Run handoff lint before returning.")
    p_work_phases_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_doctor = phases_sub.add_parser("doctor", help="Check phase execution ledger health.")
    p_work_phases_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_doctor.add_argument(
        "--range", dest="phase_range", default=None, help="Required phase range, such as 165-170."
    )
    p_work_phases_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_import = phases_sub.add_parser(
        "import-issues", help="Import phase ledger issues into the work inbox."
    )
    p_work_phases_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_import.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_import.add_argument("--dry-run", action="store_true", help="Report imports without writing them.")
    p_work_phases_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_evidence = phases_sub.add_parser("evidence", help="Attach local evidence metadata to phase records.")
    phases_evidence_sub = p_work_phases_evidence.add_subparsers(
        dest="phases_evidence_command", metavar="<phases-evidence-command>"
    )
    phases_evidence_sub.required = True
    p_work_phases_evidence_add = phases_evidence_sub.add_parser("add", help="Attach local evidence to one phase.")
    p_work_phases_evidence_add.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_evidence_add.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_evidence_add.add_argument(
        "--file", dest="files_changed", action="append", default=[], help="Changed file path. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated."
    )
    p_work_phases_evidence_add.add_argument("--test-result", default=None, help="Verification result summary.")
    p_work_phases_evidence_add.add_argument(
        "--report-id", action="append", default=[], help="Related phase report id. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--handoff", dest="handoff_paths", action="append", default=[], help="Memory Handoff path. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--note", dest="notes", action="append", default=[], help="Evidence note. May be repeated."
    )
    p_work_phases_evidence_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify = phases_sub.add_parser("verify", help="Plan and record phase verification metadata.")
    phases_verify_sub = p_work_phases_verify.add_subparsers(
        dest="phases_verify_command", metavar="<phases-verify-command>"
    )
    phases_verify_sub.required = True
    p_work_phases_verify_plan = phases_verify_sub.add_parser("plan", help="Plan verification for a phase selector.")
    p_work_phases_verify_plan.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_verify_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_verify_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify_record = phases_verify_sub.add_parser("record", help="Record one phase verification result.")
    p_work_phases_verify_record.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_verify_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_verify_record.add_argument(
        "--command", dest="verification_command", required=True, help="Verification command label."
    )
    p_work_phases_verify_record.add_argument(
        "--status", choices=["passed", "failed", "skipped", "deferred"], required=True, help="Verification result."
    )
    p_work_phases_verify_record.add_argument("--summary", default=None, help="Verification result summary.")
    p_work_phases_verify_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions = phases_sub.add_parser("actions", help="Plan and manage local phase ledger action records.")
    phases_actions_sub = p_work_phases_actions.add_subparsers(
        dest="phases_actions_command", metavar="<phases-actions-command>"
    )
    phases_actions_sub.required = True
    p_work_phases_actions_plan = phases_actions_sub.add_parser(
        "plan", help="Preview phase ledger actions from current issues."
    )
    p_work_phases_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_plan.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 201-205."
    )
    p_work_phases_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_build = phases_actions_sub.add_parser(
        "build", help="Build local phase ledger action records."
    )
    p_work_phases_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_build.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 201-205."
    )
    p_work_phases_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_list = phases_actions_sub.add_parser("list", help="List local phase ledger actions.")
    p_work_phases_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_show = phases_actions_sub.add_parser("show", help="Show one phase ledger action.")
    p_work_phases_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_start = phases_actions_sub.add_parser("start", help="Mark one phase ledger action active.")
    p_work_phases_actions_start.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_done = phases_actions_sub.add_parser("done", help="Mark one phase ledger action done.")
    p_work_phases_actions_done.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_done.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_done.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_defer = phases_actions_sub.add_parser("defer", help="Defer one phase ledger action.")
    p_work_phases_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_archive = phases_actions_sub.add_parser("archive", help="Archive phase ledger actions.")
    p_work_phases_actions_archive.add_argument("action_id", nargs="?", default=None, help="Action id or unique prefix.")
    p_work_phases_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_archive.add_argument(
        "--completed", action="store_true", help="Archive done and deferred actions."
    )
    p_work_phases_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_import = phases_actions_sub.add_parser(
        "import-issues", help="Import open phase actions into the work inbox."
    )
    p_work_phases_actions_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_import.add_argument(
        "--dry-run", action="store_true", help="Report imports without writing them."
    )
    p_work_phases_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_goal = phases_sub.add_parser("goal", help="Draft reviewed phase goal prompts.")
    phases_goal_sub = p_work_phases_goal.add_subparsers(dest="phases_goal_command", metavar="<phases-goal-command>")
    phases_goal_sub.required = True
    p_work_phases_goal_scaffold = phases_goal_sub.add_parser(
        "scaffold", help="Draft a local goal prompt from phase ledger state."
    )
    p_work_phases_goal_scaffold.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_goal_scaffold.add_argument(
        "--range", dest="phase_range", required=True, help="Phase range, such as 211-225."
    )
    p_work_phases_goal_scaffold.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report = phases_sub.add_parser("report", help="Build and inspect phase ledger reports.")
    phases_report_sub = p_work_phases_report.add_subparsers(
        dest="phases_report_command", metavar="<phases-report-command>"
    )
    phases_report_sub.required = True
    p_work_phases_report_build = phases_report_sub.add_parser("build", help="Build a local phase ledger report.")
    p_work_phases_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_report_build.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 165-170."
    )
    p_work_phases_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_list = phases_report_sub.add_parser("list", help="List phase ledger reports.")
    p_work_phases_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_show = phases_report_sub.add_parser("show", help="Show one phase ledger report.")
    p_work_phases_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_closeout = phases_report_sub.add_parser("closeout", help="Close out one phase ledger report.")
    p_work_phases_report_closeout.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_report_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "superseded", "archived"],
        default="reviewed",
        help="Report closeout state.",
    )
    p_work_phases_report_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_compare = phases_report_sub.add_parser(
        "compare", help="Compare one phase ledger report against current state."
    )
    p_work_phases_report_compare.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session = phases_sub.add_parser("session", help="Start and review phase execution sessions.")
    phases_session_sub = p_work_phases_session.add_subparsers(
        dest="phases_session_command", metavar="<phases-session-command>"
    )
    phases_session_sub.required = True
    p_work_phases_session_start = phases_session_sub.add_parser("start", help="Start a local phase execution session.")
    p_work_phases_session_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_start.add_argument(
        "--range", dest="phase_range", required=True, help="Phase range, such as 211-225."
    )
    p_work_phases_session_start.add_argument("--goal", dest="source_goal", default=None, help="Source goal text.")
    p_work_phases_session_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_list = phases_session_sub.add_parser("list", help="List phase execution sessions.")
    p_work_phases_session_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_list.add_argument("--limit", type=int, default=20, help="Maximum sessions to list.")
    p_work_phases_session_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_show = phases_session_sub.add_parser("show", help="Show one phase execution session.")
    p_work_phases_session_show.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoint = phases_session_sub.add_parser(
        "checkpoint", help="Record a local phase session checkpoint."
    )
    p_work_phases_session_checkpoint.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoint.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoint.add_argument("--phase-id", default=None, help="Phase id for the checkpoint.")
    p_work_phases_session_checkpoint.add_argument(
        "--status", choices=["noted", "blocked", "recovered"], default="noted", help="Checkpoint state."
    )
    p_work_phases_session_checkpoint.add_argument("--summary", default=None, help="Safe checkpoint summary.")
    p_work_phases_session_checkpoint.add_argument(
        "--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated."
    )
    p_work_phases_session_checkpoint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints = phases_session_sub.add_parser(
        "checkpoints", help="List and inspect phase session checkpoints."
    )
    phases_session_checkpoints_sub = p_work_phases_session_checkpoints.add_subparsers(
        dest="phases_session_checkpoints_command", metavar="<phases-session-checkpoints-command>"
    )
    phases_session_checkpoints_sub.required = True
    p_work_phases_session_checkpoints_list = phases_session_checkpoints_sub.add_parser(
        "list", help="List phase session checkpoints."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--limit", type=int, default=20, help="Maximum checkpoints to list."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_show = phases_session_checkpoints_sub.add_parser(
        "show", help="Show one phase session checkpoint."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_compare = phases_session_checkpoints_sub.add_parser(
        "compare", help="Compare one checkpoint against current session state."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_import = phases_session_checkpoints_sub.add_parser(
        "import-issues", help="Import checkpoint blockers into the work inbox."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "checkpoint_id", nargs="?", default="latest", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--dry-run", action="store_true", help="Preview imports without writing them."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_archive = phases_session_checkpoints_sub.add_parser(
        "archive", help="Archive one phase session checkpoint."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_note = phases_session_sub.add_parser(
        "recovery-note", help="Record a local phase session recovery note."
    )
    p_work_phases_session_recovery_note.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_note.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_recovery_note.add_argument("--phase-id", default=None, help="Phase id for the recovery note.")
    p_work_phases_session_recovery_note.add_argument("--summary", default=None, help="Safe recovery note summary.")
    p_work_phases_session_recovery_note.add_argument(
        "--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated."
    )
    p_work_phases_session_recovery_note.add_argument(
        "--evidence", action="append", default=[], help="Local evidence label or reference. May be repeated."
    )
    p_work_phases_session_recovery_note.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_notes = phases_session_sub.add_parser(
        "recovery-notes", help="List and inspect phase session recovery notes."
    )
    phases_session_recovery_notes_sub = p_work_phases_session_recovery_notes.add_subparsers(
        dest="phases_session_recovery_notes_command", metavar="<phases-session-recovery-notes-command>"
    )
    phases_session_recovery_notes_sub.required = True
    p_work_phases_session_recovery_notes_list = phases_session_recovery_notes_sub.add_parser(
        "list", help="List phase session recovery notes."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--limit", type=int, default=20, help="Maximum recovery notes to list."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_notes_show = phases_session_recovery_notes_sub.add_parser(
        "show", help="Show one phase session recovery note."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "note_id", help="Recovery note id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_notes_closeout = phases_session_recovery_notes_sub.add_parser(
        "closeout", help="Close out one phase session recovery note."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "note_id", help="Recovery note id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "blocked", "archived"],
        default="reviewed",
        help="Recovery note closeout state.",
    )
    p_work_phases_session_recovery_notes_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_risk = phases_session_sub.add_parser("risk", help="Summarize phase session risk.")
    p_work_phases_session_risk.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_risk.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_risk.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_verification = phases_session_sub.add_parser(
        "verification", help="Summarize phase session verification."
    )
    p_work_phases_session_verification.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_verification.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_verification.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_privacy = phases_session_sub.add_parser(
        "privacy", help="Summarize phase session privacy checks."
    )
    p_work_phases_session_privacy.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_privacy.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_handoffs = phases_session_sub.add_parser(
        "handoffs", help="Summarize phase session handoff coverage."
    )
    p_work_phases_session_handoffs.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_handoffs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_handoffs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_next = phases_session_sub.add_parser(
        "next", help="Show the next required phase session step."
    )
    p_work_phases_session_next.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_next.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_protocol = phases_session_sub.add_parser(
        "protocol", help="Show wrapper-safe phase session resume protocol."
    )
    p_work_phases_session_protocol.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_protocol.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_protocol.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_audit = phases_session_sub.add_parser("audit", help="Self-audit AFK phase session evidence.")
    p_work_phases_session_audit.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_audit.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_resume = phases_session_sub.add_parser(
        "resume", help="Record a safe phase session resume recommendation."
    )
    p_work_phases_session_resume.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_resume.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_closeout = phases_session_sub.add_parser(
        "closeout", help="Close out one phase execution session."
    )
    p_work_phases_session_closeout.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "blocked", "archived"],
        default="reviewed",
        help="Session closeout state.",
    )
    p_work_phases_session_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_activity = phases_session_sub.add_parser(
        "activity", help="Show chronological phase session activity."
    )
    p_work_phases_session_activity.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_activity.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_activity.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_progress = phases_session_sub.add_parser(
        "progress", help="Show phase session progress summary."
    )
    p_work_phases_session_progress.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_progress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_progress.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_import = phases_session_sub.add_parser(
        "import-issues", help="Import unresolved phase session blockers into the work inbox."
    )
    p_work_phases_session_import.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_import.add_argument(
        "--dry-run", action="store_true", help="Preview imports without writing them."
    )
    p_work_phases_session_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_gate = phases_session_sub.add_parser(
        "gate", help="Check whether a phase session is safe to claim complete."
    )
    p_work_phases_session_gate.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_gate.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_gate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report = phases_session_sub.add_parser(
        "report", help="Build and inspect phase session reports."
    )
    phases_session_report_sub = p_work_phases_session_report.add_subparsers(
        dest="phases_session_report_command", metavar="<phases-session-report-command>"
    )
    phases_session_report_sub.required = True
    p_work_phases_session_report_build = phases_session_report_sub.add_parser(
        "build", help="Build a local phase session report."
    )
    p_work_phases_session_report_build.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_list = phases_session_report_sub.add_parser("list", help="List phase session reports.")
    p_work_phases_session_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_session_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_show = phases_session_report_sub.add_parser(
        "show", help="Show one phase session report."
    )
    p_work_phases_session_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_session_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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

    _chat_group.register(sub)

    # context
    p_context = sub.add_parser("context", help="Plan and build local context engineering packs.")
    context_sub = p_context.add_subparsers(dest="context_command", metavar="<context-command>")
    context_sub.required = True
    for name in ("plan", "build"):
        p_context_action = context_sub.add_parser(name, help=f"{name.title()} a local context pack.")
        p_context_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace.")
        p_context_action.add_argument("--kind", choices=["task", "repo", "release", "tool-use"], default="repo")
        p_context_action.add_argument("--task-id", default=None, help="Task id for task context packs.")
        p_context_action.add_argument("--tool-id", default=None, help="Tool id for tool-use context packs.")
        p_context_action.add_argument("--release-id", default=None, help="Release candidate or readiness id.")
        p_context_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_list = context_sub.add_parser("list", help="List local context packs.")
    p_context_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_list.add_argument("--limit", type=int, default=20, help="Maximum packs to list.")
    p_context_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_show = context_sub.add_parser("show", help="Show one local context pack.")
    p_context_show.add_argument("pack_id", help="Pack id or unique prefix.")
    p_context_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_archive = context_sub.add_parser("archive", help="Archive one local context pack.")
    p_context_archive.add_argument("pack_id", help="Pack id or unique prefix.")
    p_context_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_context_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_sync = context_sub.add_parser("sync", help="Plan context pack sync into configured harness destinations.")
    p_context_sync.add_argument(
        "sync_command", choices=["plan", "record"], help="Plan or record a read-only sync plan."
    )
    p_context_sync.add_argument("pack_id", nargs="?", default="latest", help="Pack id, unique prefix, or latest.")
    p_context_sync.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_sync.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_doctor = context_sub.add_parser("doctor", help="Check context pack freshness and references.")
    p_context_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_import = context_sub.add_parser("import-issues", help="Import context pack issues into the work inbox.")
    p_context_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_context_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # projects
    p_projects = sub.add_parser("projects", help="Audit local side-project consolidation decisions.")
    projects_sub = p_projects.add_subparsers(dest="projects_command", metavar="<projects-command>")
    projects_sub.required = True
    p_projects_audit = projects_sub.add_parser("audit", help="Audit configured project consolidation records.")
    p_projects_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_import = projects_sub.add_parser(
        "import-issues", help="Import project consolidation issues into the work inbox."
    )
    p_projects_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_projects_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_projects_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout = projects_sub.add_parser(
        "closeout", help="Write a reviewed project migration closeout receipt."
    )
    p_projects_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_projects_closeout.add_argument(
        "--status", choices=sorted(projects_cmd.PROJECT_CLOSEOUT_STATUSES), required=True, help="Closeout status."
    )
    p_projects_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_projects_closeout.add_argument("--project-id", default=None, help="Close out one blocked project.")
    p_projects_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeouts = projects_sub.add_parser("closeouts", help="List project migration closeout receipts.")
    p_projects_closeouts.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout_show = projects_sub.add_parser(
        "closeout-show", help="Show one project migration closeout receipt."
    )
    p_projects_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_projects_closeout_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness = projects_sub.add_parser(
        "readiness", help="Plan and record project migration readiness receipts."
    )
    projects_readiness_sub = p_projects_readiness.add_subparsers(
        dest="projects_readiness_command", metavar="<projects-readiness-command>"
    )
    projects_readiness_sub.required = True
    p_projects_readiness_plan = projects_readiness_sub.add_parser(
        "plan", help="Plan project migration readiness without writing a receipt."
    )
    p_projects_readiness_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_record = projects_readiness_sub.add_parser(
        "record", help="Write a local project migration readiness receipt."
    )
    p_projects_readiness_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_projects_readiness_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_list = projects_readiness_sub.add_parser(
        "list", help="List local project migration readiness receipts."
    )
    p_projects_readiness_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_show = projects_readiness_sub.add_parser(
        "show", help="Show a local project migration readiness receipt."
    )
    p_projects_readiness_show.add_argument("readiness_id", help="Readiness receipt id or latest.")
    p_projects_readiness_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # learn
    p_learn = sub.add_parser("learn", help="Plan local self-learning candidates without mutating memory or source.")
    learn_sub = p_learn.add_subparsers(dest="learn_command", metavar="<learn-command>")
    learn_sub.required = True
    p_learn_plan = learn_sub.add_parser("plan", help="List local learning candidates.")
    p_learn_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_doctor = learn_sub.add_parser("doctor", help="Check local self-learning queue health.")
    p_learn_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_import = learn_sub.add_parser("import-issues", help="Import learning candidates into the work inbox.")
    p_learn_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_learn_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_import_learnings = learn_sub.add_parser(
        "import-learnings", help="Import structured .learnings/ markdown log entries into the work inbox."
    )
    p_learn_import_learnings.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_import_learnings.add_argument(
        "--file", action="append", default=[], help="Override the .learnings file to read. May be repeated."
    )
    p_learn_import_learnings.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_learn_import_learnings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_skill_candidates = learn_sub.add_parser(
        "skill-candidates", help="Find repeatable learning patterns that could become reviewed skills."
    )
    p_learn_skill_candidates.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_skill_candidates.add_argument(
        "--min-count", type=int, default=2, help="Minimum repeated evidence count required."
    )
    p_learn_skill_candidates.add_argument(
        "--source", default=None, help="Only include candidates from one learning source, such as security-scan."
    )
    p_learn_skill_candidates.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_propose_skill = learn_sub.add_parser(
        "propose-skill", help="Write a reviewed skill proposal from a learning skill candidate."
    )
    p_learn_propose_skill.add_argument("candidate_id", help="Learning skill candidate id or unique prefix.")
    p_learn_propose_skill.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_propose_skill.add_argument(
        "--min-count", type=int, default=2, help="Minimum repeated evidence count required."
    )
    p_learn_propose_skill.add_argument(
        "--source", default=None, help="Resolve the candidate within one learning source, such as security-scan."
    )
    p_learn_propose_skill.add_argument(
        "--dry-run", action="store_true", help="Preview generated skill source and inbox proposal without writing."
    )
    p_learn_propose_skill.add_argument(
        "--force", action="store_true", help="Refresh an existing generated skill source."
    )
    p_learn_propose_skill.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeout = learn_sub.add_parser(
        "closeout", help="Close out a learning candidate as accepted, dismissed, archived, or deferred."
    )
    p_learn_closeout.add_argument("candidate_id", help="Learning candidate id.")
    p_learn_closeout.add_argument("--subsystem", default=None, help="Disambiguate by subsystem.")
    p_learn_closeout.add_argument(
        "--status", choices=sorted(learn_cmd.LEARNING_CLOSEOUT_STATUSES), required=True, help="Closeout status."
    )
    p_learn_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_learn_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeouts = learn_sub.add_parser("closeouts", help="List learning closeout receipts.")
    p_learn_closeouts.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeout_show = learn_sub.add_parser("closeout-show", help="Show a learning closeout receipt.")
    p_learn_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_learn_closeout_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay = learn_sub.add_parser("replay", help="Export, inspect, and compare safe learning replay receipts.")
    learn_replay_sub = p_learn_replay.add_subparsers(dest="learn_replay_command", metavar="<learn-replay-command>")
    learn_replay_sub.required = True
    p_learn_replay_export = learn_replay_sub.add_parser(
        "export", help="Export a safe before/after learning replay receipt."
    )
    p_learn_replay_export.add_argument("scenario_id", help="Scenario id.")
    p_learn_replay_export.add_argument("--before-summary", required=True, help="Safe before summary.")
    p_learn_replay_export.add_argument("--after-summary", required=True, help="Safe after summary.")
    p_learn_replay_export.add_argument(
        "--before-count", type=int, default=None, help="Optional before candidate count."
    )
    p_learn_replay_export.add_argument("--after-count", type=int, default=None, help="Optional after candidate count.")
    p_learn_replay_export.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_replay_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_list = learn_replay_sub.add_parser("list", help="List learning replay receipts.")
    p_learn_replay_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_replay_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_show = learn_replay_sub.add_parser("show", help="Show a learning replay receipt.")
    p_learn_replay_show.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_replay_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_compare = learn_replay_sub.add_parser(
        "compare", help="Compare a learning replay before and after state."
    )
    p_learn_replay_compare.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_replay_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # research
    p_research = sub.add_parser("research", help="Run local-first deep research grounded in a trusted local corpus.")
    research_sub = p_research.add_subparsers(dest="research_command", metavar="<research-command>")
    research_sub.required = True
    p_research_run = research_sub.add_parser("run", help="Run a deep research question.")
    p_research_run.add_argument("question", help="Research question.")
    p_research_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to use.")
    p_research_run.add_argument("--corpus", default=None, help="Named corpus from research.toml.")
    p_research_run.add_argument(
        "--source", action="append", default=[], dest="source", help="Glob path of trusted local sources (repeatable)."
    )
    p_research_run.add_argument("--web", action="store_true", help="Enable the opt-in untrusted web tier.")
    p_research_run.add_argument("--rounds", type=int, default=None, help="Max research rounds (max_rounds).")
    p_research_run.add_argument(
        "--max-time", type=int, default=None, dest="max_time", help="Wall-clock budget in seconds (max_time)."
    )
    p_research_run.add_argument("--provider", default=None, help="Web search provider override.")
    p_research_run.add_argument("--category", default=None, help="Optional category label for the run.")
    p_research_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_list = research_sub.add_parser("list", help="List local research runs.")
    p_research_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_show = research_sub.add_parser("show", help="Show one local research run.")
    p_research_show.add_argument("run_id", help="Run id.")
    p_research_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_export = research_sub.add_parser(
        "export-handoff", help="Export a completed research run as a linted Memory Handoff."
    )
    p_research_export.add_argument("run_id", help="Run id.")
    p_research_export.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_export.add_argument(
        "--inbox",
        choices=(
            "codex",
            "claude",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "hermes",
        ),
        default=None,
        help="Writer harness inbox to export into.",
    )
    p_research_export.add_argument(
        "--handoff-inbox", type=Path, default=None, help="Explicit handoff inbox path for a custom writer."
    )
    p_research_export.add_argument(
        "--force", action="store_true", help="Replace an existing exported handoff at the same path."
    )
    p_research_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_cancel = research_sub.add_parser("cancel", help="Cancel a local research run.")
    p_research_cancel.add_argument("run_id", help="Run id.")
    p_research_cancel.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_cancel.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_resume = research_sub.add_parser("resume", help="Resume a local research run from its checkpoint.")
    p_research_resume.add_argument("run_id", help="Run id.")
    p_research_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_resume.add_argument("--rounds", type=int, default=None, help="Max research rounds (max_rounds).")
    p_research_resume.add_argument(
        "--max-time", type=int, default=None, dest="max_time", help="Wall-clock budget in seconds (max_time)."
    )
    p_research_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_open = research_sub.add_parser("open", help="Print the HTML report path for a local research run.")
    p_research_open.add_argument("run_id", help="Run id.")
    p_research_open.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_open.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_sources = research_sub.add_parser("sources", help="Inspect configured research source routes.")
    research_sources_sub = p_research_sources.add_subparsers(
        dest="research_sources_command", metavar="<sources-command>"
    )
    research_sources_sub.required = True
    p_research_sources_list = research_sources_sub.add_parser("list", help="List configured research source routes.")
    p_research_sources_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_research_sources_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_sources_doctor = research_sources_sub.add_parser(
        "doctor", help="Check configured research source routes."
    )
    p_research_sources_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_research_sources_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_handoffs = research_sub.add_parser("handoffs", help="Inspect and route research handoff export health.")
    research_handoffs_sub = p_research_handoffs.add_subparsers(
        dest="research_handoffs_command", metavar="<handoffs-command>"
    )
    research_handoffs_sub.required = True
    p_research_handoffs_doctor = research_handoffs_sub.add_parser(
        "doctor", help="Check research handoff export health."
    )
    p_research_handoffs_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_research_handoffs_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_handoffs_import = research_handoffs_sub.add_parser(
        "import-issues", help="Import research handoff export issues into the work inbox."
    )
    p_research_handoffs_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_research_handoffs_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing.")
    p_research_handoffs_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # center
    p_center = sub.add_parser("center", help="Read local operator-center summaries.")
    center_sub = p_center.add_subparsers(dest="center_command", metavar="<center-command>")
    center_sub.required = True
    for name in ("status", "activity", "reviews", "templates"):
        p_center_action = center_sub.add_parser(name, help=f"Show local operator-center {name}.")
        p_center_action.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_center_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if name in {"activity", "reviews"}:
            p_center_action.add_argument("--limit", type=int, default=50, help="Maximum rows to show.")
    p_center_schema = center_sub.add_parser("schema", help="Show local operator-center JSON schema manifest.")
    p_center_schema.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness = center_sub.add_parser("readiness", help="Plan and close out local operator readiness.")
    center_readiness_sub = p_center_readiness.add_subparsers(
        dest="center_readiness_command", metavar="<center-readiness-command>"
    )
    center_readiness_sub.required = True
    p_center_readiness_plan = center_readiness_sub.add_parser(
        "plan", help="Plan local operator readiness without writing a receipt."
    )
    p_center_readiness_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_closeout = center_readiness_sub.add_parser(
        "closeout", help="Write a local operator readiness closeout."
    )
    p_center_readiness_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_readiness_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed"
    )
    p_center_readiness_closeout.add_argument("--reason", default=None, help="Review or waiver reason.")
    p_center_readiness_closeout.add_argument(
        "--waive", action="append", default=[], help="Readiness finding id to waive. May be repeated."
    )
    p_center_readiness_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_list = center_readiness_sub.add_parser("list", help="List local operator readiness closeouts.")
    p_center_readiness_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_list.add_argument("--limit", type=int, default=20, help="Maximum closeouts to list.")
    p_center_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_show = center_readiness_sub.add_parser(
        "show", help="Show one local operator readiness closeout."
    )
    p_center_readiness_show.add_argument(
        "readiness_id", nargs="?", default="latest", help="Readiness id, unique prefix, or latest."
    )
    p_center_readiness_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_import = center_readiness_sub.add_parser(
        "import-issues", help="Import unresolved readiness issues into the work inbox."
    )
    p_center_readiness_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_readiness_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_readiness_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report = center_sub.add_parser("report", help="Plan, build, and inspect local operator report bundles.")
    center_report_sub = p_center_report.add_subparsers(dest="center_report_command", metavar="<center-report-command>")
    center_report_sub.required = True
    p_center_report_plan = center_report_sub.add_parser("plan", help="Plan a local operator report without writing it.")
    p_center_report_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_build = center_report_sub.add_parser("build", help="Build a local operator report bundle.")
    p_center_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_list = center_report_sub.add_parser("list", help="List local operator report bundles.")
    p_center_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_center_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_show = center_report_sub.add_parser("show", help="Show one local operator report bundle.")
    p_center_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_archive = center_report_sub.add_parser("archive", help="Archive one local operator report bundle.")
    p_center_report_archive.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_review = center_report_sub.add_parser(
        "review", help="Review one local operator report action plan."
    )
    p_center_report_review.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_review.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_compare = center_report_sub.add_parser(
        "compare", help="Compare one operator report against current local state."
    )
    p_center_report_compare.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_diff = center_report_sub.add_parser("diff", help="Diff two local operator reports.")
    p_center_report_diff.add_argument("base_report_id", help="Older report id, unique prefix, or latest.")
    p_center_report_diff.add_argument("compare_report_id", help="Newer report id or unique prefix.")
    p_center_report_diff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect or update."
    )
    p_center_report_diff.add_argument("--record", action="store_true", help="Write a local report diff receipt.")
    p_center_report_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_closeout = center_report_sub.add_parser("closeout", help="Mark one operator report review state.")
    p_center_report_closeout.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed"
    )
    p_center_report_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_center_report_closeout.add_argument(
        "--defer-item", action="append", default=[], help="Deferred report item id. May be repeated."
    )
    p_center_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions = center_sub.add_parser("actions", help="Plan and manage local daily operator actions.")
    center_actions_sub = p_center_actions.add_subparsers(
        dest="center_actions_command", metavar="<center-actions-command>"
    )
    center_actions_sub.required = True
    p_center_actions_plan = center_actions_sub.add_parser("plan", help="Plan daily actions from an operator report.")
    p_center_actions_plan.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_build = center_actions_sub.add_parser(
        "build", help="Build a daily action queue from an operator report."
    )
    p_center_actions_build.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_build.add_argument(
        "--allow-unreviewed", action="store_true", help="Build from an unclosed report."
    )
    p_center_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_list = center_actions_sub.add_parser("list", help="List local daily operator actions.")
    p_center_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_center_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_show = center_actions_sub.add_parser("show", help="Show one local daily operator action.")
    p_center_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_doctor = center_actions_sub.add_parser(
        "doctor", help="Check local daily operator action aging policy."
    )
    p_center_actions_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_import = center_actions_sub.add_parser(
        "import-issues", help="Import stale operator action issues into the work inbox."
    )
    p_center_actions_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_center_actions_state = center_actions_sub.add_parser(name, help=f"Mark one action {name}.")
        p_center_actions_state.add_argument("action_id", help="Action id or unique prefix.")
        p_center_actions_state.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        p_center_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_defer = center_actions_sub.add_parser("defer", help="Defer one local daily operator action.")
    p_center_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_center_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_archive = center_actions_sub.add_parser(
        "archive", help="Archive completed local daily operator actions."
    )
    p_center_actions_archive.add_argument(
        "--completed", action="store_true", required=True, help="Archive completed actions."
    )
    p_center_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # run
    p_run = sub.add_parser("run", help="Run a bounded cross-model orchestration task.")
    p_run.add_argument("task", help="Task for the aboyeur to plan, dispatch, and synthesize.")
    p_run.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under the current directory.",
    )
    p_run.add_argument("--dry-run", action="store_true", help="Print the plan without dispatching workers.")
    p_run.add_argument("--show-plan", action="store_true", help="Print parsed assignments before dispatch.")
    p_run.add_argument("--verbose", action="store_true", help="Print plan, worker status, and synthesis status.")
    p_run.add_argument(
        "--read-only",
        action="store_true",
        help="Tell agents to inspect and recommend only, without modifying files or external state.",
    )
    p_run.add_argument(
        "--inspect",
        action="store_true",
        help="Print a readable artifact summary after the run completes.",
    )
    p_run.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Working directory for agent CLI calls and default run artifacts.",
    )
    p_run.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for run artifacts. Defaults to .brigade/runs/<id> under --cwd.",
    )
    p_run.add_argument("--no-artifacts", action="store_true", help="Do not write run artifacts.")
    p_run.add_argument(
        "--handoff",
        action="store_true",
        help="Write a Memory Handoff for a successful non-dry run.",
    )
    p_run.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to .claude/memory-handoffs under --cwd.",
    )

    # roster
    p_roster = sub.add_parser("roster", help="Create and check aboyeur rosters.")
    roster_sub = p_roster.add_subparsers(dest="roster_command", metavar="<roster-command>")
    roster_sub.required = True
    p_roster_init = roster_sub.add_parser("init", help="Write a starter .brigade/roster.toml.")
    p_roster_init.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_init.add_argument("--force", action="store_true", help="Overwrite an existing roster.")
    p_roster_init.add_argument(
        "--ollama-model",
        default="llama3.3",
        help="Default local researcher model for the starter roster.",
    )
    p_roster_init.add_argument("--max-workers", type=int, default=4)
    p_roster_doctor = roster_sub.add_parser("doctor", help="Validate roster syntax and installed CLIs.")
    p_roster_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_doctor.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under --target.",
    )

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
    if cmd == "context":
        from .. import context_cmd

        if args.context_command == "plan":
            return context_cmd.plan(
                target=args.target,
                kind=args.kind,
                task_id=args.task_id,
                tool_id=args.tool_id,
                release_id=args.release_id,
                json_output=args.json,
            )
        if args.context_command == "build":
            return context_cmd.build(
                target=args.target,
                kind=args.kind,
                task_id=args.task_id,
                tool_id=args.tool_id,
                release_id=args.release_id,
                json_output=args.json,
            )
        if args.context_command == "list":
            return context_cmd.list_packs(target=args.target, limit=args.limit, json_output=args.json)
        if args.context_command == "show":
            return context_cmd.show(target=args.target, pack_id=args.pack_id, json_output=args.json)
        if args.context_command == "archive":
            return context_cmd.archive(target=args.target, pack_id=args.pack_id, json_output=args.json)
        if args.context_command == "sync":
            if args.sync_command == "plan":
                return context_cmd.sync_plan(target=args.target, pack_id=args.pack_id, json_output=args.json)
            if args.sync_command == "record":
                return context_cmd.sync_record(target=args.target, pack_id=args.pack_id, json_output=args.json)
        if args.context_command == "doctor":
            return context_cmd.doctor(target=args.target, json_output=args.json)
        if args.context_command == "import-issues":
            return context_cmd.import_issues(target=args.target, json_output=args.json)
        parser.error(f"unknown context command: {args.context_command}")
        return 2
    if cmd == "projects":
        from .. import projects_cmd

        if args.projects_command == "audit":
            return projects_cmd.audit(target=args.target, json_output=args.json)
        if args.projects_command == "import-issues":
            return projects_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.projects_command == "closeout":
            return projects_cmd.closeout(
                target=args.target,
                status=args.status,
                reason=args.reason,
                project_id=args.project_id,
                json_output=args.json,
            )
        if args.projects_command == "closeouts":
            return projects_cmd.closeouts(target=args.target, json_output=args.json)
        if args.projects_command == "closeout-show":
            return projects_cmd.closeout_show(target=args.target, closeout_id=args.closeout_id, json_output=args.json)
        if args.projects_command == "readiness":
            if args.projects_readiness_command == "plan":
                return projects_cmd.readiness_plan(target=args.target, json_output=args.json)
            if args.projects_readiness_command == "record":
                return projects_cmd.readiness_record(target=args.target, json_output=args.json)
            if args.projects_readiness_command == "list":
                return projects_cmd.readiness_list(target=args.target, json_output=args.json)
            if args.projects_readiness_command == "show":
                return projects_cmd.readiness_show(
                    target=args.target, readiness_id=args.readiness_id, json_output=args.json
                )
            parser.error(f"unknown projects readiness command: {args.projects_readiness_command}")
            return 2
        parser.error(f"unknown projects command: {args.projects_command}")
        return 2
    if cmd == "learn":
        from .. import learn_cmd

        if args.learn_command == "plan":
            return learn_cmd.plan(target=args.target, json_output=args.json)
        if args.learn_command == "doctor":
            return learn_cmd.doctor(target=args.target, json_output=args.json)
        if args.learn_command == "import-issues":
            return learn_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.learn_command == "import-learnings":
            return learn_cmd.import_learnings(
                target=args.target, files=args.file or None, dry_run=args.dry_run, json_output=args.json
            )
        if args.learn_command == "skill-candidates":
            return learn_cmd.skill_candidates(
                target=args.target, min_count=args.min_count, source=args.source, json_output=args.json
            )
        if args.learn_command == "propose-skill":
            return learn_cmd.propose_skill(
                target=args.target,
                candidate_id=args.candidate_id,
                min_count=args.min_count,
                source=args.source,
                dry_run=args.dry_run,
                force=args.force,
                json_output=args.json,
            )
        if args.learn_command == "closeout":
            return learn_cmd.closeout(
                target=args.target,
                candidate_id=args.candidate_id,
                subsystem=args.subsystem,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        if args.learn_command == "closeouts":
            return learn_cmd.closeouts(target=args.target, json_output=args.json)
        if args.learn_command == "closeout-show":
            return learn_cmd.closeout_show(target=args.target, closeout_id=args.closeout_id, json_output=args.json)
        if args.learn_command == "replay":
            if args.learn_replay_command == "export":
                return learn_cmd.replay_export(
                    target=args.target,
                    scenario_id=args.scenario_id,
                    before_summary=args.before_summary,
                    after_summary=args.after_summary,
                    before_count=args.before_count,
                    after_count=args.after_count,
                    json_output=args.json,
                )
            if args.learn_replay_command == "list":
                return learn_cmd.replay_list(target=args.target, json_output=args.json)
            if args.learn_replay_command == "show":
                return learn_cmd.replay_show(target=args.target, replay_id=args.replay_id, json_output=args.json)
            if args.learn_replay_command == "compare":
                return learn_cmd.replay_compare(target=args.target, replay_id=args.replay_id, json_output=args.json)
            parser.error(f"unknown learn replay command: {args.learn_replay_command}")
            return 2
        parser.error(f"unknown learn command: {args.learn_command}")
        return 2
    if cmd == "research":
        from .. import research_cmd

        if args.research_command == "run":
            overrides = {"max_rounds": args.rounds, "max_time": args.max_time}
            return research_cmd.cli_run(
                target=args.target,
                question=args.question,
                corpus=args.corpus,
                sources=list(args.source),
                web=args.web,
                overrides=overrides,
                provider=args.provider,
                json_output=args.json,
            )
        if args.research_command == "list":
            return research_cmd.cli_list(target=args.target, json_output=args.json)
        if args.research_command == "show":
            return research_cmd.cli_show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.research_command == "export-handoff":
            return research_cmd.cli_export_handoff(
                target=args.target,
                run_id=args.run_id,
                inbox=args.inbox,
                handoff_inbox=args.handoff_inbox,
                force=args.force,
                json_output=args.json,
            )
        if args.research_command == "cancel":
            return research_cmd.cli_cancel(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.research_command == "resume":
            overrides = {"max_rounds": args.rounds, "max_time": args.max_time}
            return research_cmd.cli_resume(
                target=args.target, run_id=args.run_id, overrides=overrides, json_output=args.json
            )
        if args.research_command == "open":
            return research_cmd.cli_open(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.research_command == "sources":
            if args.research_sources_command == "list":
                return research_cmd.cli_sources_list(target=args.target, json_output=args.json)
            if args.research_sources_command == "doctor":
                return research_cmd.cli_sources_doctor(target=args.target, json_output=args.json)
            parser.error(f"unknown research sources command: {args.research_sources_command}")
            return 2
        if args.research_command == "handoffs":
            if args.research_handoffs_command == "doctor":
                return research_cmd.cli_handoffs_doctor(target=args.target, json_output=args.json)
            if args.research_handoffs_command == "import-issues":
                return research_cmd.cli_handoffs_import_issues(
                    target=args.target, dry_run=args.dry_run, json_output=args.json
                )
            parser.error(f"unknown research handoffs command: {args.research_handoffs_command}")
            return 2
        parser.error(f"unknown research command: {args.research_command}")
        return 2
    if cmd == "center":
        from .. import center_cmd

        if args.center_command == "status":
            return center_cmd.status(target=args.target, json_output=args.json)
        if args.center_command == "activity":
            return center_cmd.activity(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_command == "reviews":
            return center_cmd.reviews(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_command == "templates":
            return center_cmd.templates(target=args.target, json_output=args.json)
        if args.center_command == "schema":
            return center_cmd.schema(target=args.target, json_output=args.json)
        if args.center_command == "readiness":
            if args.center_readiness_command == "plan":
                return center_cmd.readiness_plan(target=args.target, json_output=args.json)
            if args.center_readiness_command == "closeout":
                return center_cmd.readiness_closeout(
                    target=args.target,
                    status=args.status,
                    reason=args.reason,
                    waive_finding_ids=args.waive,
                    json_output=args.json,
                )
            if args.center_readiness_command == "list":
                return center_cmd.readiness_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.center_readiness_command == "show":
                return center_cmd.readiness_show(
                    target=args.target, readiness_id=args.readiness_id, json_output=args.json
                )
            if args.center_readiness_command == "import-issues":
                return center_cmd.readiness_import_issues(
                    target=args.target, dry_run=args.dry_run, json_output=args.json
                )
            parser.error(f"unknown center readiness command: {args.center_readiness_command}")
            return 2
        if args.center_command == "report":
            if args.center_report_command == "plan":
                return center_cmd.report_plan(target=args.target, json_output=args.json)
            if args.center_report_command == "build":
                return center_cmd.report_build(target=args.target, json_output=args.json)
            if args.center_report_command == "list":
                return center_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.center_report_command == "show":
                return center_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.center_report_command == "archive":
                return center_cmd.report_archive(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.center_report_command == "review":
                return center_cmd.report_review(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.center_report_command == "compare":
                return center_cmd.report_compare(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.center_report_command == "diff":
                return center_cmd.report_diff(
                    target=args.target,
                    base_report_id=args.base_report_id,
                    compare_report_id=args.compare_report_id,
                    record=args.record,
                    json_output=args.json,
                )
            if args.center_report_command == "closeout":
                return center_cmd.report_closeout(
                    target=args.target,
                    report_id=args.report_id,
                    status=args.status,
                    reason=args.reason,
                    deferred_item_ids=args.defer_item,
                    json_output=args.json,
                )
            parser.error(f"unknown center report command: {args.center_report_command}")
            return 2
        if args.center_command == "actions":
            if args.center_actions_command == "plan":
                return center_cmd.actions_plan(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.center_actions_command == "build":
                return center_cmd.actions_build(
                    target=args.target,
                    report_id=args.report_id,
                    allow_unreviewed=args.allow_unreviewed,
                    json_output=args.json,
                )
            if args.center_actions_command == "list":
                return center_cmd.actions_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.center_actions_command == "show":
                return center_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.center_actions_command == "doctor":
                return center_cmd.actions_doctor(target=args.target, json_output=args.json)
            if args.center_actions_command == "import-issues":
                return center_cmd.actions_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            if args.center_actions_command == "start":
                return center_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.center_actions_command == "done":
                return center_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.center_actions_command == "defer":
                return center_cmd.actions_defer(
                    target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
                )
            if args.center_actions_command == "archive":
                return center_cmd.actions_archive_completed(target=args.target, json_output=args.json)
            parser.error(f"unknown center actions command: {args.center_actions_command}")
            return 2
        parser.error(f"unknown center command: {args.center_command}")
        return 2
    if cmd == "memory":
        from .. import memory_cmd

        if args.memory_command == "care":
            if args.memory_care_command == "init":
                return memory_cmd.init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.memory_care_command == "scan":
                return memory_cmd.scan(target=args.target, json_output=args.json)
            if args.memory_care_command == "backfill":
                return memory_cmd.backfill(target=args.target, apply=args.apply, json_output=args.json)
            if args.memory_care_command == "plan-fixes":
                return memory_cmd.plan_fixes(target=args.target, json_output=args.json)
            if args.memory_care_command == "status":
                return memory_cmd.status(target=args.target, json_output=args.json)
            if args.memory_care_command == "doctor":
                return memory_cmd.doctor(target=args.target, json_output=args.json)
            if args.memory_care_command == "import-issues":
                return memory_cmd.import_issues(
                    target=args.target,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.memory_care_command == "closeout":
                return memory_cmd.closeout(
                    target=args.target,
                    reason=args.reason,
                    defer=args.defer,
                    json_output=args.json,
                )
            parser.error(f"unknown memory care command: {args.memory_care_command}")
            return 2
        parser.error(f"unknown memory command: {args.memory_command}")
        return 2
    if cmd == "work":
        from .. import work_cmd

        if args.work_command == "status":
            return work_cmd.status(target=args.target, limit=args.limit)
        if args.work_command == "doctor":
            return work_cmd.doctor(target=args.target)
        if args.work_command == "bootstrap":
            return work_cmd.bootstrap(
                target=args.target,
                artifacts_dir=args.artifacts_dir,
                handoff_inbox=args.handoff_inbox,
                force=args.force,
                handoff=not args.no_handoff,
                inspect=not args.no_inspect,
                native_read_only_sandbox=args.native_read_only_sandbox,
                timeout_seconds=args.timeout_seconds,
                update_gitignore=not args.no_gitignore,
            )
        if args.work_command == "resume":
            return work_cmd.resume(target=args.target)
        if args.work_command == "brief":
            return work_cmd.brief(target=args.target, limit=args.limit, json_output=args.json)
        if args.work_command == "sweep":
            if args.sweep_args:
                if args.sweep_args[0] != "closeout":
                    parser.error("work sweep accepts only `closeout <sweep-id|latest>` as positional arguments")
                    return 2
                if len(args.sweep_args) > 2:
                    parser.error("work sweep closeout accepts at most one sweep id")
                    return 2
                return work_cmd.sweep_closeout(
                    target=args.target,
                    sweep_id=args.sweep_args[1] if len(args.sweep_args) == 2 else "latest",
                    reason=args.reason,
                    deferred_imports=args.defer,
                    defer_all=args.defer_all,
                    json_output=args.json,
                )
            return work_cmd.sweep(
                target=args.target,
                scanner_id=args.scanner,
                all_matching=args.all,
                include_disabled=args.include_disabled,
                force=args.force,
                ingest=not args.no_ingest,
                json_output=args.json,
            )
        if args.work_command == "sweeps":
            return work_cmd.sweeps(target=args.target, limit=args.limit, json_output=args.json)
        if args.work_command == "plans":
            return work_cmd.plans(target=args.target, limit=args.limit, json_output=args.json)
        if args.work_command == "plan-promote":
            return work_cmd.plan_promote(
                target=args.target, task_id=args.task_id, as_kind=args.as_kind, json_output=args.json
            )
        if args.work_command == "plan-proposals":
            return work_cmd.plan_proposals(target=args.target, json_output=args.json)
        if args.work_command == "sweep-show":
            return work_cmd.sweep_show(target=args.target, sweep_id=args.sweep_id, json_output=args.json)
        if args.work_command == "sweep-review":
            return work_cmd.sweep_review(target=args.target, sweep_id=args.sweep_id, json_output=args.json)
        if args.work_command == "verify":
            if args.verify_command == "plan":
                return work_cmd.verify_plan(target=args.target, commands=args.verify_commands, json_output=args.json)
            if args.verify_command == "run":
                return work_cmd.verify_run(
                    target=args.target,
                    commands=args.verify_commands,
                    timeout=args.timeout,
                    json_output=args.json,
                )
            if args.verify_command == "runs":
                return work_cmd.verify_runs(target=args.target, limit=args.limit, json_output=args.json)
            if args.verify_command == "show":
                return work_cmd.verify_show(target=args.target, run_id=args.run_id, json_output=args.json)
            parser.error(f"unknown verify command: {args.verify_command}")
            return 2
        if args.work_command == "closeout":
            return work_cmd.closeout(target=args.target, session_id=args.session_id, json_output=args.json)
        if args.work_command == "acceptance":
            return work_cmd.acceptance(target=args.target, json_output=args.json)
        if args.work_command == "inbox" and getattr(args, "inbox_command", None):
            if args.inbox_command == "doctor":
                return work_cmd.inbox_doctor(target=args.target, json_output=args.json)
            if args.inbox_command == "archive":
                return work_cmd.inbox_archive(target=args.target, json_output=args.json)
            parser.error(f"unknown inbox command: {args.inbox_command}")
            return 2
        if args.work_command == "inbox":
            return work_cmd.inbox(target=args.target, json_output=args.json, limit=args.limit)
        if args.work_command == "backup":
            if args.backup_command == "init":
                return work_cmd.backup_init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.backup_command == "contract":
                return work_cmd.backup_contract(
                    target=args.target,
                    destination_id=args.destination,
                    json_output=args.json,
                )
            if args.backup_command == "status":
                return work_cmd.backup_status(target=args.target, json_output=args.json)
            if args.backup_command == "doctor":
                return work_cmd.backup_doctor(target=args.target, json_output=args.json)
            if args.backup_command == "import-issues":
                return work_cmd.backup_import_issues(target=args.target, json_output=args.json)
            if args.backup_command == "closeout":
                return work_cmd.backup_closeout(
                    target=args.target,
                    reason=args.reason,
                    defer=args.defer,
                    json_output=args.json,
                )
            parser.error(f"unknown backup command: {args.backup_command}")
            return 2
        if args.work_command == "scanners":
            if args.scanners_command == "init":
                return work_cmd.scanners_init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.scanners_command == "list":
                return work_cmd.scanners_list(target=args.target, json_output=args.json)
            if args.scanners_command == "show":
                return work_cmd.scanners_show(target=args.target, scanner_id=args.scanner_id, json_output=args.json)
            if args.scanners_command == "plan":
                return work_cmd.scanners_plan(target=args.target, json_output=args.json)
            if args.scanners_command == "run":
                return work_cmd.scanners_run(
                    target=args.target,
                    scanner_id=args.scanner_id,
                    all_matching=args.all,
                    due=args.due,
                    include_disabled=args.include_disabled,
                    force=args.force,
                    ingest_output=args.ingest_output,
                    json_output=args.json,
                )
            if args.scanners_command == "runs":
                return work_cmd.scanners_runs(target=args.target, limit=args.limit, json_output=args.json)
            if args.scanners_command == "run-show":
                return work_cmd.scanners_run_show(target=args.target, run_id=args.run_id, json_output=args.json)
            if args.scanners_command == "doctor":
                return work_cmd.scanners_doctor(
                    target=args.target,
                    json_output=args.json,
                    import_issues=args.import_issues,
                )
            parser.error(f"unknown scanners command: {args.scanners_command}")
            return 2
        if args.work_command == "review":
            if args.review_command == "init":
                return work_cmd.review_init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.review_command == "plan":
                return work_cmd.review_plan(target=args.target, json_output=args.json)
            if args.review_command == "run":
                return work_cmd.review_run(
                    target=args.target,
                    reviewer_id=args.reviewer_id,
                    all_matching=args.all,
                    include_disabled=args.include_disabled,
                    json_output=args.json,
                )
            if args.review_command == "runs":
                return work_cmd.review_runs(target=args.target, limit=args.limit, json_output=args.json)
            if args.review_command == "show":
                return work_cmd.review_show(target=args.target, run_id=args.run_id, json_output=args.json)
            if args.review_command == "import-findings":
                return work_cmd.review_import_findings(
                    target=args.target,
                    run_id=args.run_id,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.review_command == "findings":
                return work_cmd.review_findings(target=args.target, run_id=args.run_id, json_output=args.json)
            if args.review_command == "finding-show":
                return work_cmd.review_finding_show(
                    target=args.target, finding_id=args.finding_id, json_output=args.json
                )
            if args.review_command == "closeout":
                return work_cmd.review_closeout(target=args.target, run_id=args.run_id, json_output=args.json)
            parser.error(f"unknown review command: {args.review_command}")
            return 2
        if args.work_command == "phases":
            from .. import phases_cmd

            if args.phases_command == "init":
                return phases_cmd.init(target=args.target, json_output=args.json)
            if args.phases_command == "plan":
                return phases_cmd.plan(
                    target=args.target,
                    phase_id=args.phase_id,
                    phase_range=args.phase_range,
                    title=args.title,
                    source_goal=args.source_goal,
                    grouped=args.grouped,
                    force=args.force,
                    json_output=args.json,
                )
            if args.phases_command == "list":
                return phases_cmd.list_phases(target=args.target, json_output=args.json)
            if args.phases_command == "schema":
                return phases_cmd.schema(target=args.target, json_output=args.json)
            if args.phases_command == "status":
                return phases_cmd.status(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_command == "next":
                return phases_cmd.next_phase(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_command == "show":
                return phases_cmd.show(target=args.target, phase_id=args.phase_id, json_output=args.json)
            if args.phases_command == "start":
                return phases_cmd.start(target=args.target, phase_id=args.phase_id, json_output=args.json)
            if args.phases_command == "complete":
                return phases_cmd.complete(
                    target=args.target,
                    phase_id=args.phase_id,
                    status=args.status,
                    summary=args.summary,
                    files_changed=args.files_changed,
                    tests_run=args.tests_run,
                    test_result_summary=args.test_result,
                    commit_hash=args.commit_hash,
                    push_ref=args.push_ref,
                    deferred_items=args.deferred_item,
                    next_phase_recommendation=args.next_phase_recommendation,
                    json_output=args.json,
                )
            if args.phases_command == "defer":
                return phases_cmd.defer(
                    target=args.target,
                    phase_id=args.phase_id,
                    reason=args.reason,
                    next_phase_recommendation=args.next_phase_recommendation,
                    json_output=args.json,
                )
            if args.phases_command == "closeout":
                return phases_cmd.closeout(
                    target=args.target,
                    selector=args.selector,
                    status=args.status,
                    reason=args.reason,
                    json_output=args.json,
                )
            if args.phases_command == "compare":
                return phases_cmd.compare(target=args.target, selector=args.selector, json_output=args.json)
            if args.phases_command == "reconcile":
                return phases_cmd.reconcile(target=args.target, selector=args.selector, json_output=args.json)
            if args.phases_command == "privacy":
                return phases_cmd.privacy(target=args.target, selector=args.selector, json_output=args.json)
            if args.phases_command == "handoff":
                return phases_cmd.handoff(
                    target=args.target, selector=args.selector, lint=args.lint, json_output=args.json
                )
            if args.phases_command == "doctor":
                return phases_cmd.doctor(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_command == "import-issues":
                return phases_cmd.import_issues(
                    target=args.target, phase_range=args.phase_range, dry_run=args.dry_run, json_output=args.json
                )
            if args.phases_command == "evidence":
                if args.phases_evidence_command == "add":
                    return phases_cmd.evidence_add(
                        target=args.target,
                        phase_id=args.phase_id,
                        files_changed=args.files_changed,
                        tests_run=args.tests_run,
                        test_result_summary=args.test_result,
                        report_ids=args.report_id,
                        handoff_paths=args.handoff_paths,
                        notes=args.notes,
                        json_output=args.json,
                    )
                parser.error(f"unknown phases evidence command: {args.phases_evidence_command}")
                return 2
            if args.phases_command == "verify":
                if args.phases_verify_command == "plan":
                    return phases_cmd.verify_plan(target=args.target, selector=args.selector, json_output=args.json)
                if args.phases_verify_command == "record":
                    return phases_cmd.verify_record(
                        target=args.target,
                        phase_id=args.phase_id,
                        command=args.verification_command,
                        status=args.status,
                        summary=args.summary,
                        json_output=args.json,
                    )
                parser.error(f"unknown phases verify command: {args.phases_verify_command}")
                return 2
            if args.phases_command == "actions":
                if args.phases_actions_command == "plan":
                    return phases_cmd.actions_plan(
                        target=args.target, phase_range=args.phase_range, json_output=args.json
                    )
                if args.phases_actions_command == "build":
                    return phases_cmd.actions_build(
                        target=args.target, phase_range=args.phase_range, json_output=args.json
                    )
                if args.phases_actions_command == "list":
                    return phases_cmd.actions_list(target=args.target, json_output=args.json)
                if args.phases_actions_command == "show":
                    return phases_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.phases_actions_command == "start":
                    return phases_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.phases_actions_command == "done":
                    return phases_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.phases_actions_command == "defer":
                    return phases_cmd.actions_defer(
                        target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
                    )
                if args.phases_actions_command == "archive":
                    return phases_cmd.actions_archive(
                        target=args.target, action_id=args.action_id, completed=args.completed, json_output=args.json
                    )
                if args.phases_actions_command == "import-issues":
                    return phases_cmd.actions_import_issues(
                        target=args.target, dry_run=args.dry_run, json_output=args.json
                    )
                parser.error(f"unknown phases actions command: {args.phases_actions_command}")
                return 2
            if args.phases_command == "goal":
                if args.phases_goal_command == "scaffold":
                    return phases_cmd.goal_scaffold(
                        target=args.target, phase_range=args.phase_range, json_output=args.json
                    )
                parser.error(f"unknown phases goal command: {args.phases_goal_command}")
                return 2
            if args.phases_command == "report":
                if args.phases_report_command == "build":
                    return phases_cmd.report_build(
                        target=args.target, phase_range=args.phase_range, json_output=args.json
                    )
                if args.phases_report_command == "list":
                    return phases_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_report_command == "show":
                    return phases_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
                if args.phases_report_command == "closeout":
                    return phases_cmd.report_closeout(
                        target=args.target,
                        report_id=args.report_id,
                        status=args.status,
                        reason=args.reason,
                        json_output=args.json,
                    )
                if args.phases_report_command == "compare":
                    return phases_cmd.report_compare(
                        target=args.target, report_id=args.report_id, json_output=args.json
                    )
                parser.error(f"unknown phases report command: {args.phases_report_command}")
                return 2
            if args.phases_command == "session":
                if args.phases_session_command == "start":
                    return phases_cmd.session_start(
                        target=args.target,
                        phase_range=args.phase_range,
                        source_goal=args.source_goal,
                        json_output=args.json,
                    )
                if args.phases_session_command == "list":
                    return phases_cmd.session_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_session_command == "show":
                    return phases_cmd.session_show(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "checkpoint":
                    return phases_cmd.session_checkpoint(
                        target=args.target,
                        session_id=args.session_id,
                        phase_id=args.phase_id,
                        status=args.status,
                        summary=args.summary,
                        notes=args.notes,
                        json_output=args.json,
                    )
                if args.phases_session_command == "checkpoints":
                    if args.phases_session_checkpoints_command == "list":
                        return phases_cmd.session_checkpoint_list(
                            target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json
                        )
                    if args.phases_session_checkpoints_command == "show":
                        return phases_cmd.session_checkpoint_show(
                            target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                        )
                    if args.phases_session_checkpoints_command == "compare":
                        return phases_cmd.session_checkpoint_compare(
                            target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                        )
                    if args.phases_session_checkpoints_command == "import-issues":
                        return phases_cmd.session_checkpoint_import_issues(
                            target=args.target,
                            checkpoint_id=args.checkpoint_id,
                            dry_run=args.dry_run,
                            json_output=args.json,
                        )
                    if args.phases_session_checkpoints_command == "archive":
                        return phases_cmd.session_checkpoint_archive(
                            target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                        )
                    parser.error(
                        f"unknown phases session checkpoints command: {args.phases_session_checkpoints_command}"
                    )
                if args.phases_session_command == "recovery-note":
                    return phases_cmd.session_recovery_note(
                        target=args.target,
                        session_id=args.session_id,
                        phase_id=args.phase_id,
                        summary=args.summary,
                        notes=args.notes,
                        evidence=args.evidence,
                        json_output=args.json,
                    )
                if args.phases_session_command == "recovery-notes":
                    if args.phases_session_recovery_notes_command == "list":
                        return phases_cmd.session_recovery_note_list(
                            target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json
                        )
                    if args.phases_session_recovery_notes_command == "show":
                        return phases_cmd.session_recovery_note_show(
                            target=args.target, note_id=args.note_id, json_output=args.json
                        )
                    if args.phases_session_recovery_notes_command == "closeout":
                        return phases_cmd.session_recovery_note_closeout(
                            target=args.target,
                            note_id=args.note_id,
                            status=args.status,
                            reason=args.reason,
                            json_output=args.json,
                        )
                    parser.error(
                        f"unknown phases session recovery notes command: {args.phases_session_recovery_notes_command}"
                    )
                if args.phases_session_command == "risk":
                    return phases_cmd.session_risk(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "verification":
                    return phases_cmd.session_verification(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "privacy":
                    return phases_cmd.session_privacy(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "handoffs":
                    return phases_cmd.session_handoffs(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "next":
                    return phases_cmd.session_next(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "protocol":
                    return phases_cmd.session_protocol(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "audit":
                    return phases_cmd.session_audit(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "resume":
                    return phases_cmd.session_resume(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "closeout":
                    return phases_cmd.session_closeout(
                        target=args.target,
                        session_id=args.session_id,
                        status=args.status,
                        reason=args.reason,
                        json_output=args.json,
                    )
                if args.phases_session_command == "activity":
                    return phases_cmd.session_activity(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "progress":
                    return phases_cmd.session_progress(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "import-issues":
                    return phases_cmd.session_import_issues(
                        target=args.target, session_id=args.session_id, dry_run=args.dry_run, json_output=args.json
                    )
                if args.phases_session_command == "gate":
                    return phases_cmd.session_gate(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_command == "report":
                    if args.phases_session_report_command == "build":
                        return phases_cmd.session_report_build(
                            target=args.target, session_id=args.session_id, json_output=args.json
                        )
                    if args.phases_session_report_command == "list":
                        return phases_cmd.session_report_list(
                            target=args.target, limit=args.limit, json_output=args.json
                        )
                    if args.phases_session_report_command == "show":
                        return phases_cmd.session_report_show(
                            target=args.target, report_id=args.report_id, json_output=args.json
                        )
                    parser.error(f"unknown phases session report command: {args.phases_session_report_command}")
                    return 2
                parser.error(f"unknown phases session command: {args.phases_session_command}")
                return 2
            parser.error(f"unknown phases command: {args.phases_command}")
            return 2
        if args.work_command == "next":
            return work_cmd.next(target=args.target, json_output=args.json)
        if args.work_command == "tasks":
            return work_cmd.tasks(target=args.target, all_tasks=args.all, json_output=args.json)
        if args.work_command == "task":
            if args.task_command == "add":
                text = " ".join(args.text) if args.text else None
                return work_cmd.task_add(
                    target=args.target,
                    text=text,
                    from_next=args.from_next,
                    from_issue=args.from_issue,
                    task_type=args.type,
                    priority=args.priority,
                    acceptance=args.acceptance,
                    template=args.template,
                )
            if args.task_command == "show":
                return work_cmd.task_show(target=args.target, task_id=args.task_id)
            if args.task_command == "plan":
                return work_cmd.task_plan(
                    target=args.target,
                    task_id=args.task_id,
                    json_output=args.json,
                    write=args.write,
                    title=args.title,
                    assumptions=args.assumptions,
                    risks=args.risks,
                    sources=args.sources,
                    next_command=args.next_command,
                    accept=args.accept,
                    kind="meta" if args.meta else "plan",
                    steps=args.step,
                    from_research=args.from_research,
                )
            if args.task_command == "done":
                return work_cmd.task_done(target=args.target, task_id=args.task_id)
            parser.error(f"unknown task command: {args.task_command}")
            return 2
        if args.work_command == "import":
            if args.import_command == "add":
                return work_cmd.import_add(
                    target=args.target,
                    text=" ".join(args.text),
                    kind=args.kind,
                    source=args.source,
                    metadata=args.metadata,
                )
            if args.import_command == "context":
                if not args.text and args.from_file is None:
                    parser.error("work import context requires text or --from-file")
                return work_cmd.import_context(
                    target=args.target,
                    text=" ".join(args.text) if args.text else "",
                    source=args.source,
                    context_kind=args.context_kind,
                    from_file=args.from_file,
                    max_chars=args.max_chars,
                    json_output=args.json,
                )
            if args.import_command == "list":
                return work_cmd.import_list(
                    target=args.target,
                    all_imports=args.all,
                    json_output=args.json,
                    limit=args.limit,
                    source=args.source,
                    kind=args.kind,
                    metadata=args.metadata,
                )
            if args.import_command == "validate":
                return work_cmd.import_validate(input_path=args.input_path, json_output=args.json)
            if args.import_command == "ingest":
                return work_cmd.import_ingest(
                    target=args.target,
                    input_path=args.input_path,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "issue-repairs":
                return work_cmd.import_issue_repairs(
                    target=args.target,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "plan":
                return work_cmd.import_plan(target=args.target, import_id=args.import_id, json_output=args.json)
            if args.import_command == "plan-handoff":
                return work_cmd.import_plan_handoff(target=args.target, import_id=args.import_id, json_output=args.json)
            if args.import_command == "memory-care":
                return work_cmd.import_memory_care(
                    target=args.target,
                    queue=args.queue,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "memory-refresh":
                return work_cmd.import_memory_refresh(
                    target=args.target,
                    queue=args.queue,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "chat-sweep":
                return work_cmd.import_chat_sweep(
                    target=args.target,
                    input_path=args.input_path,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "content-guard":
                return work_cmd.import_content_guard(
                    target=args.target,
                    scan_target=args.scan_target,
                    policy=args.policy,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            if args.import_command == "triage":
                return work_cmd.import_triage(
                    target=args.target,
                    json_output=args.json,
                    limit=args.limit,
                    source=args.source,
                    kind=args.kind,
                    metadata=args.metadata,
                )
            if args.import_command == "provenance":
                return work_cmd.import_provenance(target=args.target, json_output=args.json)
            if args.import_command == "show":
                return work_cmd.import_show(target=args.target, import_id=args.import_id)
            if args.import_command == "promote":
                return work_cmd.import_promote(
                    target=args.target,
                    import_id=args.import_id,
                    all_matching=args.all,
                    kind=args.kind,
                    source=args.source,
                    metadata=args.metadata,
                    run_after=args.run,
                )
            if args.import_command == "promote-handoff":
                return work_cmd.import_promote_handoff(
                    target=args.target,
                    import_id=args.import_id,
                    run_after=args.run,
                    json_output=args.json,
                )
            if args.import_command == "dismiss":
                return work_cmd.import_dismiss(
                    target=args.target,
                    import_id=args.import_id,
                    reason=args.reason,
                    all_matching=args.all,
                    kind=args.kind,
                    source=args.source,
                    metadata=args.metadata,
                )
            parser.error(f"unknown import command: {args.import_command}")
            return 2
        if args.work_command == "list":
            return work_cmd.list_sessions(target=args.target, limit=args.limit)
        if args.work_command == "latest":
            return work_cmd.latest(target=args.target)
        if args.work_command == "show":
            return work_cmd.show(target=args.target, session=args.session)
        if args.work_command == "recap":
            return work_cmd.recap(target=args.target, limit=args.limit, since=args.since)
        if args.work_command == "run":
            task = " ".join(args.task) if args.task else None
            return work_cmd.run(
                task,
                target=args.target,
                title=args.title,
                output_dir=args.output_dir,
                handoff=not args.no_handoff,
                handoff_inbox=args.handoff_inbox,
                dogfood_handoff=args.dogfood_handoff,
                inspect=not args.no_inspect,
                native_read_only_sandbox=args.native_read_only_sandbox,
                timeout_seconds=args.timeout_seconds,
                recap_limit=args.recap_limit,
                queue_next=args.queue_next,
            )
        if args.work_command == "start":
            title = " ".join(args.title) if args.title else None
            return work_cmd.start(target=args.target, title=title, force=args.force)
        if args.work_command == "note":
            return work_cmd.note(target=args.target, text=" ".join(args.text))
        if args.work_command == "end":
            return work_cmd.end(
                target=args.target,
                note=args.note,
                handoff=args.handoff,
                handoff_inbox=args.handoff_inbox,
            )
        parser.error(f"unknown work command: {args.work_command}")
        return 2
    if cmd == "run":
        from .. import aboyeur as aboyeur_mod
        from .. import roster as roster_mod

        run_cwd = args.cwd.expanduser().resolve()
        if not run_cwd.is_dir():
            print(f"error: --cwd is not a directory: {run_cwd}", file=sys.stderr)
            return 2
        if args.handoff and args.dry_run:
            print("error: --handoff cannot be used with --dry-run", file=sys.stderr)
            return 2
        if args.inspect and args.no_artifacts:
            print("error: --inspect cannot be used with --no-artifacts", file=sys.stderr)
            return 2
        roster_path = args.roster or (run_cwd / ".brigade" / "roster.toml")
        try:
            loaded_roster = roster_mod.load_roster(roster_path)
        except FileNotFoundError:
            print(
                f"error: roster not found: {roster_path}. Create .brigade/roster.toml or pass --roster.",
                file=sys.stderr,
            )
            return 2
        except ValueError as exc:
            print(f"error: invalid roster: {exc}", file=sys.stderr)
            return 2
        output_dir = None
        if not args.no_artifacts:
            output_dir = args.output_dir or aboyeur_mod.make_run_dir(run_cwd / ".brigade" / "runs")
        handoff_inbox = None
        if args.handoff:
            handoff_inbox = args.handoff_inbox or (run_cwd / ".claude" / "memory-handoffs")
        rc = aboyeur_mod.run(
            args.task,
            loaded_roster,
            dry_run=args.dry_run,
            show_plan=args.show_plan,
            verbose=args.verbose,
            cwd=run_cwd,
            output_dir=output_dir,
            handoff_inbox=handoff_inbox,
            read_only=args.read_only,
        )
        if output_dir is not None:
            print(f"artifacts: {output_dir}", file=sys.stderr)
            if args.inspect:
                from .. import runs_cmd

                runs_cmd.show(output_dir)
        return rc
    if cmd == "roster":
        from .. import roster_cmd

        if args.roster_command == "init":
            return roster_cmd.init(
                target=args.target,
                force=args.force,
                ollama_model=args.ollama_model,
                max_workers=args.max_workers,
            )
        if args.roster_command == "doctor":
            return roster_cmd.doctor(target=args.target, roster_path=args.roster)
        parser.error(f"unknown roster command: {args.roster_command}")
        return 2
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
