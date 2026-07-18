"""brigade repos command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    from .. import repos_cmd

    # repos
    p_repos = sub.add_parser("repos", help="Inspect local repository fleet readiness.")
    repos_sub = p_repos.add_subparsers(dest="repos_command", metavar="<repos-command>")
    repos_sub.required = True
    p_repos_init = repos_sub.add_parser("init", help="Write local repo fleet config.")
    p_repos_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_init.add_argument("--force", action="store_true", help="Overwrite existing config.")
    p_repos_init.add_argument("--no-gitignore", action="store_true", help="Do not update .gitignore.")
    p_repos_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_list = repos_sub.add_parser("list", help="List configured fleet repos.")
    p_repos_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_show = repos_sub.add_parser("show", help="Show one configured fleet repo.")
    p_repos_show.add_argument("repo_id", help="Repo id.")
    p_repos_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_scan = repos_sub.add_parser("scan", help="Scan local repo fleet readiness.")
    p_repos_scan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_doctor = repos_sub.add_parser("doctor", help="Report repo fleet health.")
    p_repos_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_doctor.add_argument(
        "--deep",
        action="store_true",
        help="Run the operator checkup (every first-run doctor) in each enabled repo and aggregate.",
    )
    p_repos_import = repos_sub.add_parser("import-issues", help="Import repo fleet health issues into the work inbox.")
    p_repos_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_import.add_argument("--dry-run", action="store_true", help="Show counts without writing imports.")
    p_repos_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_first_run = repos_sub.add_parser("first-run", help="Plan the first repo-fleet production evidence run.")
    repos_first_run_sub = p_repos_first_run.add_subparsers(
        dest="repos_first_run_command", metavar="<repos-first-run-command>"
    )
    repos_first_run_sub.required = True
    p_repos_first_run_plan = repos_first_run_sub.add_parser(
        "plan", help="Show the manual first-run repo-fleet sequence."
    )
    p_repos_first_run_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_first_run_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_ingest = repos_sub.add_parser("ingest", help="Ingest every fleet repo's handoffs into the canonical owner.")
    p_repos_ingest.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Canonical memory owner (where the fleet config lives)."
    )
    p_repos_ingest.add_argument("--apply", action="store_true", help="Write changes. Default is a dry run.")
    p_repos_ingest.add_argument("--no-promote-cards", action="store_true", help="Do not auto-promote cards.")
    p_repos_ingest.add_argument("--no-route-documents", action="store_true", help="Do not auto-route documents.")
    p_repos_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_rearm = repos_sub.add_parser("rearm", help="Plan or apply Brigade dogfood arming across fleet repos.")
    p_repos_rearm.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Canonical memory owner (where the fleet config lives)."
    )
    p_repos_rearm.add_argument("--apply", action="store_true", help="Write changes. Default is a dry run.")
    p_repos_rearm.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_health_commands = repos_sub.add_parser(
        "health-commands", help="Inspect configured optional repo health commands."
    )
    p_repos_health_commands.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_health_commands.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_discover = repos_sub.add_parser("discover", help="Plan safe repo discovery under configured roots.")
    repos_discover_sub = p_repos_discover.add_subparsers(
        dest="repos_discover_command", metavar="<repos-discover-command>"
    )
    repos_discover_sub.required = True
    p_repos_discover_plan = repos_discover_sub.add_parser("plan", help="Dry-run discovery under configured roots.")
    p_repos_discover_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_discover_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_adoption = repos_sub.add_parser(
        "adoption", help="Compare harness wiring with observed Brigade work-loop use."
    )
    p_repos_adoption.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo fleet workspace to inspect."
    )
    p_repos_adoption.add_argument(
        "--harness", dest="harnesses", action="append", default=[], help="Harness to inspect. May be repeated."
    )
    p_repos_adoption.add_argument("--days", type=int, default=7, help="Recent session window in days.")
    p_repos_adoption.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    repos_adoption_sub = p_repos_adoption.add_subparsers(dest="adoption_action", metavar="<repos-adoption-command>")
    p_repos_adoption_repair = repos_adoption_sub.add_parser(
        "repair", help="Plan repairs for matching noncompliant rows."
    )
    p_repos_adoption_repair.add_argument(
        "--target", "-t", type=Path, default=argparse.SUPPRESS, help="Repo fleet workspace to inspect."
    )
    p_repos_adoption_repair.add_argument(
        "--harness",
        dest="harnesses",
        action="append",
        default=argparse.SUPPRESS,
        help="Harness to inspect. May be repeated.",
    )
    p_repos_adoption_repair.add_argument(
        "--days", type=int, default=argparse.SUPPRESS, help="Recent session window in days."
    )
    p_repos_adoption_repair.add_argument(
        "--state",
        choices=("unwired", "partial", "advisory-only", "enforced-idle", "active", "bypassed", "stale"),
        default=None,
        help="Repair only rows in this state.",
    )
    p_repos_adoption_repair.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Print machine-readable JSON."
    )
    p_repos_report = repos_sub.add_parser("report", help="Plan, build, and inspect local repo fleet reports.")
    repos_report_sub = p_repos_report.add_subparsers(dest="repos_report_command", metavar="<repos-report-command>")
    repos_report_sub.required = True
    for name in ("plan", "build"):
        p_repos_report_cmd = repos_report_sub.add_parser(name, help=f"{name.title()} a repo fleet report.")
        p_repos_report_cmd.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_repos_report_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_list = repos_report_sub.add_parser("list", help="List local repo fleet reports.")
    p_repos_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_repos_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_show = repos_report_sub.add_parser("show", help="Show one local repo fleet report.")
    p_repos_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_repos_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_archive = repos_report_sub.add_parser("archive", help="Archive one local repo fleet report.")
    p_repos_report_archive.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_repos_report_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_report_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_closeout = repos_report_sub.add_parser("closeout", help="Mark one local repo fleet report reviewed.")
    p_repos_report_closeout.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_repos_report_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_report_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed"
    )
    p_repos_report_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions = repos_sub.add_parser("actions", help="Plan and manage local repo fleet actions.")
    repos_actions_sub = p_repos_actions.add_subparsers(dest="repos_actions_command", metavar="<repos-actions-command>")
    repos_actions_sub.required = True
    p_repos_actions_plan = repos_actions_sub.add_parser("plan", help="Plan fleet actions from a report.")
    p_repos_actions_plan.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_repos_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_build = repos_actions_sub.add_parser("build", help="Build fleet actions from a report.")
    p_repos_actions_build.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_repos_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_actions_build.add_argument("--allow-unreviewed", action="store_true", help="Build from an unclosed report.")
    p_repos_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_list = repos_actions_sub.add_parser("list", help="List local repo fleet actions.")
    p_repos_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_repos_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_show = repos_actions_sub.add_parser("show", help="Show one local repo fleet action.")
    p_repos_actions_show.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_repos_actions_state = repos_actions_sub.add_parser(name, help=f"Mark one fleet action {name}.")
        p_repos_actions_state.add_argument("action_id", help="Fleet action id or unique prefix.")
        p_repos_actions_state.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        p_repos_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_defer = repos_actions_sub.add_parser("defer", help="Defer one local repo fleet action.")
    p_repos_actions_defer.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_repos_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_archive = repos_actions_sub.add_parser(
        "archive", help="Archive completed local repo fleet actions."
    )
    p_repos_actions_archive.add_argument(
        "--completed", action="store_true", required=True, help="Archive completed actions."
    )
    p_repos_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_dispatch = repos_actions_sub.add_parser(
        "dispatch", help="Dispatch reviewed fleet actions into target repo work imports."
    )
    p_repos_actions_dispatch.add_argument(
        "dispatch_args",
        nargs="*",
        help="Use `plan <action-id>`, `apply <action-id>`, or `report <action-id>`. Omit with --all-reviewed.",
    )
    p_repos_actions_dispatch.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_actions_dispatch.add_argument(
        "--all-reviewed", action="store_true", help="Dispatch all reviewed pending or active fleet actions."
    )
    p_repos_actions_dispatch.add_argument(
        "--all", dest="all_actions", action="store_true", help="Include all fleet actions for dispatch reports."
    )
    p_repos_actions_dispatch.add_argument(
        "--include-deferred", action="store_true", help="Allow dispatching deferred actions."
    )
    p_repos_actions_dispatch.add_argument(
        "--dry-run", action="store_true", help="Plan without writing target imports or action metadata."
    )
    p_repos_actions_dispatch.add_argument(
        "--record", action="store_true", help="Record a local dispatch report receipt."
    )
    p_repos_actions_dispatch.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_reconcile = repos_actions_sub.add_parser(
        "reconcile", help="Reconcile fleet actions against target repo evidence."
    )
    p_repos_actions_reconcile.add_argument(
        "action_id", nargs="?", default=None, help="Fleet action id or unique prefix. Defaults to all actions."
    )
    p_repos_actions_reconcile.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_actions_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_context = repos_actions_sub.add_parser(
        "context", help="Plan or build a target repo context pack for one fleet action."
    )
    p_repos_actions_context.add_argument(
        "context_command", choices=["plan", "build"], help="Plan or build the context pack."
    )
    p_repos_actions_context.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_context.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_actions_context.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep = repos_sub.add_parser("sweep", help="Plan, run, and close out explicit repo fleet evidence sweeps.")
    repos_sweep_sub = p_repos_sweep.add_subparsers(dest="repos_sweep_command", metavar="<repos-sweep-command>")
    repos_sweep_sub.required = True
    for name in ("plan", "run"):
        p_repos_sweep_cmd = repos_sweep_sub.add_parser(name, help=f"{name.title()} a repo fleet evidence sweep.")
        p_repos_sweep_cmd.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_repos_sweep_cmd.add_argument(
            "--repo", dest="repo_ids", action="append", default=[], help="Repo id to include. May be repeated."
        )
        p_repos_sweep_cmd.add_argument(
            "--all", dest="all_repos", action="store_true", help="Include all enabled repos."
        )
        p_repos_sweep_cmd.add_argument(
            "--stale-only", action="store_true", help="Only include repos without a successful sweep."
        )
        p_repos_sweep_cmd.add_argument(
            "--include-disabled", action="store_true", help="Allow disabled configured repos."
        )
        p_repos_sweep_cmd.add_argument(
            "--force", action="store_true", help="Force a refresh even when evidence is fresh."
        )
        p_repos_sweep_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_runs = repos_sweep_sub.add_parser("runs", help="List repo fleet sweep receipts.")
    p_repos_sweep_runs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_sweep_runs.add_argument("--limit", type=int, default=20, help="Maximum sweeps to list.")
    p_repos_sweep_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_show = repos_sweep_sub.add_parser("show", help="Show one repo fleet sweep receipt.")
    p_repos_sweep_show.add_argument("sweep_id", help="Sweep id, unique prefix, or latest.")
    p_repos_sweep_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_sweep_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_closeout = repos_sweep_sub.add_parser("closeout", help="Close out one repo fleet sweep review.")
    p_repos_sweep_closeout.add_argument(
        "sweep_id", nargs="?", default="latest", help="Sweep id, unique prefix, or latest."
    )
    p_repos_sweep_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_sweep_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed"
    )
    p_repos_sweep_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_sweep_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release = repos_sub.add_parser("release", help="Plan and close out local repo fleet release trains.")
    repos_release_sub = p_repos_release.add_subparsers(dest="repos_release_command", metavar="<repos-release-command>")
    repos_release_sub.required = True
    for name in ("plan", "build"):
        p_repos_release_cmd = repos_release_sub.add_parser(name, help=f"{name.title()} a repo fleet release train.")
        p_repos_release_cmd.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_repos_release_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_list = repos_release_sub.add_parser("list", help="List repo fleet release trains.")
    p_repos_release_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_list.add_argument("--limit", type=int, default=20, help="Maximum trains to list.")
    p_repos_release_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("show", "compare", "archive"):
        p_repos_release_item = repos_release_sub.add_parser(name, help=f"{name.title()} a repo fleet release train.")
        p_repos_release_item.add_argument("train_id", help="Train id, unique prefix, or latest.")
        p_repos_release_item.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_repos_release_item.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("reconcile", "summary", "report", "matrix", "checklist", "ready", "activity", "manifest", "audit"):
        p_repos_release_review = repos_release_sub.add_parser(
            name, help=f"{name.title()} one repo fleet release train."
        )
        p_repos_release_review.add_argument(
            "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
        )
        p_repos_release_review.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_repos_release_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_hygiene = repos_release_sub.add_parser("hygiene", help="Check fleet release train hygiene.")
    p_repos_release_hygiene.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_hygiene.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_import = repos_release_sub.add_parser(
        "import-issues", help="Import fleet release train issues into the local work inbox."
    )
    p_repos_release_import.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_import.add_argument("--dry-run", action="store_true", help="Validate without writing imports.")
    p_repos_release_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_closeout = repos_release_sub.add_parser("closeout", help="Close out one repo fleet release train.")
    p_repos_release_closeout.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed"
    )
    p_repos_release_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_release_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions = repos_release_sub.add_parser(
        "actions", help="Plan and manage fleet release train actions."
    )
    repos_release_actions_sub = p_repos_release_actions.add_subparsers(
        dest="repos_release_actions_command", metavar="<repos-release-actions-command>"
    )
    repos_release_actions_sub.required = True
    p_repos_release_actions_plan = repos_release_actions_sub.add_parser(
        "plan", help="Plan actions from one fleet release train."
    )
    p_repos_release_actions_plan.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_build = repos_release_actions_sub.add_parser(
        "build", help="Build actions from one fleet release train."
    )
    p_repos_release_actions_build.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_actions_build.add_argument(
        "--allow-unreviewed", action="store_true", help="Build from an unclosed release train."
    )
    p_repos_release_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_list = repos_release_actions_sub.add_parser(
        "list", help="List fleet release train actions."
    )
    p_repos_release_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_repos_release_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_show = repos_release_actions_sub.add_parser(
        "show", help="Show one fleet release train action."
    )
    p_repos_release_actions_show.add_argument("action_id", help="Release action id or unique prefix.")
    p_repos_release_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_repos_release_actions_state = repos_release_actions_sub.add_parser(
            name, help=f"Mark one fleet release action {name}."
        )
        p_repos_release_actions_state.add_argument("action_id", help="Release action id or unique prefix.")
        p_repos_release_actions_state.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        p_repos_release_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_defer = repos_release_actions_sub.add_parser(
        "defer", help="Defer one fleet release train action."
    )
    p_repos_release_actions_defer.add_argument("action_id", help="Release action id or unique prefix.")
    p_repos_release_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_repos_release_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_archive = repos_release_actions_sub.add_parser(
        "archive", help="Archive completed fleet release actions."
    )
    p_repos_release_actions_archive.add_argument(
        "--completed", action="store_true", required=True, help="Archive completed actions."
    )
    p_repos_release_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence = repos_release_sub.add_parser("evidence", help="Record manual fleet release evidence.")
    repos_release_evidence_sub = p_repos_release_evidence.add_subparsers(
        dest="repos_release_evidence_command", metavar="<repos-release-evidence-command>"
    )
    repos_release_evidence_sub.required = True
    p_repos_release_evidence_plan = repos_release_evidence_sub.add_parser(
        "plan", help="Plan manual evidence records for a fleet release train."
    )
    p_repos_release_evidence_plan.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_evidence_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_evidence_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_record = repos_release_evidence_sub.add_parser(
        "record", help="Record one manual fleet release evidence item."
    )
    p_repos_release_evidence_record.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_evidence_record.add_argument(
        "--repo", dest="repo_id", required=True, help="Repo id from the train."
    )
    p_repos_release_evidence_record.add_argument(
        "--step", required=True, choices=sorted(repos_cmd.RELEASE_EVIDENCE_STEPS), help="Manual release evidence step."
    )
    p_repos_release_evidence_record.add_argument(
        "--status", required=True, choices=sorted(repos_cmd.RELEASE_EVIDENCE_STATUSES), help="Evidence status."
    )
    p_repos_release_evidence_record.add_argument("--summary", default=None, help="Safe summary.")
    p_repos_release_evidence_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_evidence_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_list = repos_release_evidence_sub.add_parser(
        "list", help="List manual fleet release evidence records."
    )
    p_repos_release_evidence_list.add_argument(
        "train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest."
    )
    p_repos_release_evidence_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_evidence_list.add_argument("--limit", type=int, default=50, help="Maximum records to list.")
    p_repos_release_evidence_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_show = repos_release_evidence_sub.add_parser(
        "show", help="Show one manual fleet release evidence record."
    )
    p_repos_release_evidence_show.add_argument("evidence_id", help="Evidence id or unique prefix.")
    p_repos_release_evidence_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_evidence_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers = repos_release_sub.add_parser("waivers", help="Record and inspect fleet release waivers.")
    repos_release_waivers_sub = p_repos_release_waivers.add_subparsers(
        dest="repos_release_waivers_command", metavar="<repos-release-waivers-command>"
    )
    repos_release_waivers_sub.required = True
    p_repos_release_waivers_record = repos_release_waivers_sub.add_parser(
        "record", help="Record one active fleet release waiver."
    )
    p_repos_release_waivers_record.add_argument(
        "train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest."
    )
    p_repos_release_waivers_record.add_argument(
        "--scope", required=True, choices=sorted(repos_cmd.RELEASE_WAIVER_SCOPES), help="Waiver scope."
    )
    p_repos_release_waivers_record.add_argument(
        "--repo", dest="repo_id", default=None, help="Optional repo id from the train."
    )
    p_repos_release_waivers_record.add_argument("--reason", required=True, help="Safe waiver reason.")
    p_repos_release_waivers_record.add_argument(
        "--expires-at", default=None, help="Optional ISO timestamp when the waiver should expire."
    )
    p_repos_release_waivers_record.add_argument("--owner-label", default=None, help="Safe review owner label.")
    p_repos_release_waivers_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_waivers_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_list = repos_release_waivers_sub.add_parser("list", help="List fleet release waivers.")
    p_repos_release_waivers_list.add_argument(
        "train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest."
    )
    p_repos_release_waivers_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_waivers_list.add_argument("--limit", type=int, default=50, help="Maximum waivers to list.")
    p_repos_release_waivers_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_show = repos_release_waivers_sub.add_parser("show", help="Show one fleet release waiver.")
    p_repos_release_waivers_show.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_waivers_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_revoke = repos_release_waivers_sub.add_parser(
        "revoke", help="Revoke one fleet release waiver."
    )
    p_repos_release_waivers_revoke.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_revoke.add_argument("--reason", required=True, help="Safe revocation reason.")
    p_repos_release_waivers_revoke.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_waivers_revoke.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_renew = repos_release_waivers_sub.add_parser(
        "renew", help="Renew one fleet release waiver."
    )
    p_repos_release_waivers_renew.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_renew.add_argument("--reason", required=True, help="Safe renewal reason.")
    p_repos_release_waivers_renew.add_argument(
        "--expires-at", default=None, help="Optional ISO timestamp when the waiver should expire."
    )
    p_repos_release_waivers_renew.add_argument("--owner-label", default=None, help="Safe review owner label.")
    p_repos_release_waivers_renew.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_waivers_renew.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_templates = repos_release_waivers_sub.add_parser(
        "templates", help="List fleet release waiver policy templates."
    )
    p_repos_release_waivers_templates.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_friction = repos_sub.add_parser(
        "friction", help="Scan and review friction across the configured repo fleet."
    )
    repos_friction_sub = p_repos_friction.add_subparsers(
        dest="repos_friction_command", metavar="<repos-friction-command>"
    )
    repos_friction_sub.required = True
    p_repos_friction_scan = repos_friction_sub.add_parser("scan", help="Scan friction across enabled fleet repos.")
    p_repos_friction_scan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_friction_scan.add_argument("--days", type=int, default=30, help="Lookback window in days.")
    p_repos_friction_scan.add_argument(
        "--include-agent-logs",
        action="store_true",
        help="Also scan global agent logs once from the operator workspace.",
    )
    p_repos_friction_scan.add_argument("--max-files", type=int, default=5000, help="Maximum source files per repo.")
    p_repos_friction_scan.add_argument(
        "--max-candidates", type=int, default=200, help="Maximum candidates to record per repo."
    )
    p_repos_friction_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_friction_show = repos_friction_sub.add_parser("show", help="Show the latest repo fleet friction report.")
    p_repos_friction_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_friction_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_doctor = repos_release_waivers_sub.add_parser(
        "doctor", help="Check fleet release waiver health."
    )
    p_repos_release_waivers_doctor.add_argument(
        "train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest."
    )
    p_repos_release_waivers_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_repos_release_waivers_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_import = repos_release_waivers_sub.add_parser(
        "import-issues", help="Import fleet release waiver issues into the local work inbox."
    )
    p_repos_release_waivers_import.add_argument(
        "train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest."
    )
    p_repos_release_waivers_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_repos_release_waivers_import.add_argument(
        "--dry-run", action="store_true", help="Validate without writing imports."
    )
    p_repos_release_waivers_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import repos_cmd

    if args.repos_command == "init":
        return repos_cmd.fleet.init(
            target=args.target,
            force=args.force,
            update_gitignore=not args.no_gitignore,
            json_output=args.json,
        )
    if args.repos_command == "list":
        return repos_cmd.fleet.list_repos(target=args.target, json_output=args.json)
    if args.repos_command == "show":
        return repos_cmd.fleet.show(target=args.target, repo_id=args.repo_id, json_output=args.json)
    if args.repos_command == "scan":
        return repos_cmd.fleet.scan(target=args.target, json_output=args.json)
    if args.repos_command == "doctor":
        return repos_cmd.fleet.doctor(target=args.target, json_output=args.json, deep=args.deep)
    if args.repos_command == "import-issues":
        return repos_cmd.sweeps.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
    if args.repos_command == "first-run":
        if args.repos_first_run_command == "plan":
            return repos_cmd.fleet_health.first_run_plan(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown repos first-run command: {args.repos_first_run_command}")
        return 2
    if args.repos_command == "ingest":
        return repos_cmd.ingest_fleet(
            target=args.target,
            apply=args.apply,
            promote_cards=not args.no_promote_cards,
            route_documents=not args.no_route_documents,
            json_output=args.json,
        )
    if args.repos_command == "rearm":
        return repos_cmd.fleet.rearm(target=args.target, apply=args.apply, json_output=args.json)
    if args.repos_command == "health-commands":
        return repos_cmd.fleet_health.health_commands(target=args.target, json_output=args.json)
    if args.repos_command == "discover":
        if args.repos_discover_command == "plan":
            return repos_cmd.fleet.discover_plan(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown repos discover command: {args.repos_discover_command}")
        return 2
    if args.repos_command == "adoption":
        harnesses = args.harnesses or None
        if args.adoption_action == "repair":
            return repos_cmd.adoption_repair(
                target=args.target,
                harnesses=harnesses,
                days=args.days,
                state=args.state,
                json_output=args.json,
            )
        return repos_cmd.adoption_report(
            target=args.target,
            harnesses=harnesses,
            days=args.days,
            json_output=args.json,
        )
    if args.repos_command == "report":
        if args.repos_report_command == "plan":
            return repos_cmd.report_plan(target=args.target, json_output=args.json)
        if args.repos_report_command == "build":
            return repos_cmd.report_build(target=args.target, json_output=args.json)
        if args.repos_report_command == "list":
            return repos_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.repos_report_command == "show":
            return repos_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.repos_report_command == "archive":
            return repos_cmd.report_archive(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.repos_report_command == "closeout":
            return repos_cmd.report_closeout(
                target=args.target,
                report_id=args.report_id,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown repos report command: {args.repos_report_command}")
        return 2
    if args.repos_command == "actions":
        if args.repos_actions_command == "plan":
            return repos_cmd.actions_plan(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.repos_actions_command == "build":
            return repos_cmd.actions_build(
                target=args.target,
                report_id=args.report_id,
                allow_unreviewed=args.allow_unreviewed,
                json_output=args.json,
            )
        if args.repos_actions_command == "list":
            return repos_cmd.actions_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.repos_actions_command == "show":
            return repos_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.repos_actions_command == "start":
            return repos_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.repos_actions_command == "done":
            return repos_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.repos_actions_command == "defer":
            return repos_cmd.actions_defer(
                target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
            )
        if args.repos_actions_command == "archive":
            return repos_cmd.actions_archive_completed(target=args.target, json_output=args.json)
        if args.repos_actions_command == "dispatch":
            dispatch_args = list(args.dispatch_args or [])
            dispatch_mode = "apply"
            action_id = None
            if dispatch_args and dispatch_args[0] in {"plan", "apply", "report"}:
                dispatch_mode = dispatch_args.pop(0)
            if dispatch_args:
                action_id = dispatch_args.pop(0)
            if dispatch_args:
                args._brigade_parser.error("too many repos actions dispatch arguments")
            if dispatch_mode == "plan":
                return repos_cmd.actions_dispatch_plan(
                    target=args.target,
                    action_id=action_id,
                    all_reviewed=args.all_reviewed,
                    include_deferred=args.include_deferred,
                    json_output=args.json,
                )
            if dispatch_mode == "report":
                return repos_cmd.actions_dispatch_report(
                    target=args.target,
                    action_id=action_id,
                    all_actions=args.all_actions,
                    record=args.record,
                    json_output=args.json,
                )
            return repos_cmd.actions_dispatch_apply(
                target=args.target,
                action_id=action_id,
                all_reviewed=args.all_reviewed,
                include_deferred=args.include_deferred,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.repos_actions_command == "reconcile":
            return repos_cmd.actions_reconcile(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.repos_actions_command == "context":
            if args.context_command == "plan":
                return repos_cmd.actions_context_plan(
                    target=args.target, action_id=args.action_id, json_output=args.json
                )
            return repos_cmd.actions_context_build(target=args.target, action_id=args.action_id, json_output=args.json)
        args._brigade_parser.error(f"unknown repos actions command: {args.repos_actions_command}")
        return 2
    if args.repos_command == "sweep":
        if args.repos_sweep_command == "plan":
            return repos_cmd.sweep_plan(
                target=args.target,
                repo_ids=args.repo_ids,
                all_repos=args.all_repos,
                stale_only=args.stale_only,
                include_disabled=args.include_disabled,
                force=args.force,
                json_output=args.json,
            )
        if args.repos_sweep_command == "run":
            return repos_cmd.sweep_run(
                target=args.target,
                repo_ids=args.repo_ids,
                all_repos=args.all_repos,
                stale_only=args.stale_only,
                include_disabled=args.include_disabled,
                force=args.force,
                json_output=args.json,
            )
        if args.repos_sweep_command == "runs":
            return repos_cmd.sweep_runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.repos_sweep_command == "show":
            return repos_cmd.sweep_show(target=args.target, sweep_id=args.sweep_id, json_output=args.json)
        if args.repos_sweep_command == "closeout":
            return repos_cmd.sweep_closeout(
                target=args.target,
                sweep_id=args.sweep_id,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown repos sweep command: {args.repos_sweep_command}")
        return 2
    if args.repos_command == "release":
        if args.repos_release_command == "plan":
            return repos_cmd.release_plan(target=args.target, json_output=args.json)
        if args.repos_release_command == "build":
            return repos_cmd.release_build(target=args.target, json_output=args.json)
        if args.repos_release_command == "list":
            return repos_cmd.release_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.repos_release_command == "show":
            return repos_cmd.release_show(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "compare":
            return repos_cmd.release_compare(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "closeout":
            return repos_cmd.release_closeout(
                target=args.target,
                train_id=args.train_id,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        if args.repos_release_command == "archive":
            return repos_cmd.release_archive(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "reconcile":
            return repos_cmd.release_reconcile(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "summary":
            return repos_cmd.release_summary(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "report":
            return repos_cmd.release_report(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "matrix":
            return repos_cmd.release_matrix(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "checklist":
            return repos_cmd.release_checklist(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "ready":
            return repos_cmd.release_ready(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "activity":
            return repos_cmd.release_activity(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "manifest":
            return repos_cmd.release_manifest(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "audit":
            return repos_cmd.release_audit(target=args.target, train_id=args.train_id, json_output=args.json)
        if args.repos_release_command == "hygiene":
            return repos_cmd.release_hygiene(target=args.target, json_output=args.json)
        if args.repos_release_command == "import-issues":
            return repos_cmd.release_import_issues(
                target=args.target, train_id=args.train_id, dry_run=args.dry_run, json_output=args.json
            )
        if args.repos_release_command == "actions":
            if args.repos_release_actions_command == "plan":
                return repos_cmd.release_actions_plan(target=args.target, train_id=args.train_id, json_output=args.json)
            if args.repos_release_actions_command == "build":
                return repos_cmd.release_actions_build(
                    target=args.target,
                    train_id=args.train_id,
                    allow_unreviewed=args.allow_unreviewed,
                    json_output=args.json,
                )
            if args.repos_release_actions_command == "list":
                return repos_cmd.release_actions_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.repos_release_actions_command == "show":
                return repos_cmd.release_actions_show(
                    target=args.target, action_id=args.action_id, json_output=args.json
                )
            if args.repos_release_actions_command == "start":
                return repos_cmd.release_actions_start(
                    target=args.target, action_id=args.action_id, json_output=args.json
                )
            if args.repos_release_actions_command == "done":
                return repos_cmd.release_actions_done(
                    target=args.target, action_id=args.action_id, json_output=args.json
                )
            if args.repos_release_actions_command == "defer":
                return repos_cmd.release_actions_defer(
                    target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
                )
            if args.repos_release_actions_command == "archive":
                return repos_cmd.release_actions_archive_completed(target=args.target, json_output=args.json)
            args._brigade_parser.error(f"unknown repos release actions command: {args.repos_release_actions_command}")
            return 2
        if args.repos_release_command == "evidence":
            if args.repos_release_evidence_command == "plan":
                return repos_cmd.release_evidence_plan(
                    target=args.target, train_id=args.train_id, json_output=args.json
                )
            if args.repos_release_evidence_command == "record":
                return repos_cmd.release_evidence_record(
                    target=args.target,
                    train_id=args.train_id,
                    repo_id=args.repo_id,
                    step=args.step,
                    status=args.status,
                    summary=args.summary,
                    json_output=args.json,
                )
            if args.repos_release_evidence_command == "list":
                return repos_cmd.release_evidence_list(
                    target=args.target, train_id=args.train_id, limit=args.limit, json_output=args.json
                )
            if args.repos_release_evidence_command == "show":
                return repos_cmd.release_evidence_show(
                    target=args.target, evidence_id=args.evidence_id, json_output=args.json
                )
            args._brigade_parser.error(f"unknown repos release evidence command: {args.repos_release_evidence_command}")
            return 2
        if args.repos_release_command == "waivers":
            if args.repos_release_waivers_command == "record":
                return repos_cmd.release_waiver_record(
                    target=args.target,
                    train_id=args.train_id,
                    scope=args.scope,
                    repo_id=args.repo_id,
                    reason=args.reason,
                    expires_at=args.expires_at,
                    owner_label=args.owner_label,
                    json_output=args.json,
                )
            if args.repos_release_waivers_command == "list":
                return repos_cmd.release_waiver_list(
                    target=args.target, train_id=args.train_id, limit=args.limit, json_output=args.json
                )
            if args.repos_release_waivers_command == "show":
                return repos_cmd.release_waiver_show(
                    target=args.target, waiver_id=args.waiver_id, json_output=args.json
                )
            if args.repos_release_waivers_command == "revoke":
                return repos_cmd.release_waiver_revoke(
                    target=args.target, waiver_id=args.waiver_id, reason=args.reason, json_output=args.json
                )
            if args.repos_release_waivers_command == "renew":
                return repos_cmd.release_waiver_renew(
                    target=args.target,
                    waiver_id=args.waiver_id,
                    reason=args.reason,
                    expires_at=args.expires_at,
                    owner_label=args.owner_label,
                    json_output=args.json,
                )
            if args.repos_release_waivers_command == "doctor":
                return repos_cmd.release_waiver_doctor(
                    target=args.target, train_id=args.train_id, json_output=args.json
                )
            if args.repos_release_waivers_command == "import-issues":
                return repos_cmd.release_waiver_import_issues(
                    target=args.target, train_id=args.train_id, dry_run=args.dry_run, json_output=args.json
                )
            if args.repos_release_waivers_command == "templates":
                return repos_cmd.release_waiver_templates(json_output=args.json)
            args._brigade_parser.error(f"unknown repos release waivers command: {args.repos_release_waivers_command}")
            return 2
        args._brigade_parser.error(f"unknown repos release command: {args.repos_release_command}")
        return 2
    if args.repos_command == "friction":
        if args.repos_friction_command == "scan":
            return repos_cmd.friction_scan(
                target=args.target,
                days=args.days,
                include_agent_logs=args.include_agent_logs,
                max_files=args.max_files,
                max_candidates=args.max_candidates,
                json_output=args.json,
            )
        if args.repos_friction_command == "show":
            return repos_cmd.friction_show(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown repos friction command: {args.repos_friction_command}")
        return 2
    args._brigade_parser.error(f"unknown repos command: {args.repos_command}")
    return 2
