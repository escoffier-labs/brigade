"""brigade work command group."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from ..work_cmd import TASK_PRIORITIES, TASK_TYPES


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
        help="Verification command. May be repeated.",
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
    p_work.set_defaults(func=dispatch)


def dispatch(args) -> int:
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
                args._brigade_parser.error(
                    "work sweep accepts only `closeout <sweep-id|latest>` as positional arguments"
                )
                return 2
            if len(args.sweep_args) > 2:
                args._brigade_parser.error("work sweep closeout accepts at most one sweep id")
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
                capture=args.capture,
                capture_kind=args.capture_kind,
            )
        if args.verify_command == "runs":
            return work_cmd.verify_runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.verify_command == "show":
            return work_cmd.verify_show(target=args.target, run_id=args.run_id, json_output=args.json)
        args._brigade_parser.error(f"unknown verify command: {args.verify_command}")
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
        args._brigade_parser.error(f"unknown inbox command: {args.inbox_command}")
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
        args._brigade_parser.error(f"unknown backup command: {args.backup_command}")
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
        args._brigade_parser.error(f"unknown scanners command: {args.scanners_command}")
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
            return work_cmd.review_finding_show(target=args.target, finding_id=args.finding_id, json_output=args.json)
        if args.review_command == "closeout":
            return work_cmd.review_closeout(target=args.target, run_id=args.run_id, json_output=args.json)
        args._brigade_parser.error(f"unknown review command: {args.review_command}")
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
            return phases_cmd.handoff(target=args.target, selector=args.selector, lint=args.lint, json_output=args.json)
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
            args._brigade_parser.error(f"unknown phases evidence command: {args.phases_evidence_command}")
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
            args._brigade_parser.error(f"unknown phases verify command: {args.phases_verify_command}")
            return 2
        if args.phases_command == "actions":
            if args.phases_actions_command == "plan":
                return phases_cmd.actions_plan(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_actions_command == "build":
                return phases_cmd.actions_build(target=args.target, phase_range=args.phase_range, json_output=args.json)
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
                return phases_cmd.actions_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            args._brigade_parser.error(f"unknown phases actions command: {args.phases_actions_command}")
            return 2
        if args.phases_command == "goal":
            if args.phases_goal_command == "scaffold":
                return phases_cmd.goal_scaffold(target=args.target, phase_range=args.phase_range, json_output=args.json)
            args._brigade_parser.error(f"unknown phases goal command: {args.phases_goal_command}")
            return 2
        if args.phases_command == "report":
            if args.phases_report_command == "build":
                return phases_cmd.report_build(target=args.target, phase_range=args.phase_range, json_output=args.json)
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
                return phases_cmd.report_compare(target=args.target, report_id=args.report_id, json_output=args.json)
            args._brigade_parser.error(f"unknown phases report command: {args.phases_report_command}")
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
                return phases_cmd.session_show(target=args.target, session_id=args.session_id, json_output=args.json)
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
                args._brigade_parser.error(
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
                args._brigade_parser.error(
                    f"unknown phases session recovery notes command: {args.phases_session_recovery_notes_command}"
                )
            if args.phases_session_command == "risk":
                return phases_cmd.session_risk(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "verification":
                return phases_cmd.session_verification(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "privacy":
                return phases_cmd.session_privacy(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "handoffs":
                return phases_cmd.session_handoffs(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "next":
                return phases_cmd.session_next(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "protocol":
                return phases_cmd.session_protocol(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "audit":
                return phases_cmd.session_audit(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "resume":
                return phases_cmd.session_resume(target=args.target, session_id=args.session_id, json_output=args.json)
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
                return phases_cmd.session_gate(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "report":
                if args.phases_session_report_command == "build":
                    return phases_cmd.session_report_build(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_report_command == "list":
                    return phases_cmd.session_report_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_session_report_command == "show":
                    return phases_cmd.session_report_show(
                        target=args.target, report_id=args.report_id, json_output=args.json
                    )
                args._brigade_parser.error(
                    f"unknown phases session report command: {args.phases_session_report_command}"
                )
                return 2
            args._brigade_parser.error(f"unknown phases session command: {args.phases_session_command}")
            return 2
        args._brigade_parser.error(f"unknown phases command: {args.phases_command}")
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
        args._brigade_parser.error(f"unknown task command: {args.task_command}")
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
                args._brigade_parser.error("work import context requires text or --from-file")
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
        args._brigade_parser.error(f"unknown import command: {args.import_command}")
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
    args._brigade_parser.error(f"unknown work command: {args.work_command}")
    return 2
