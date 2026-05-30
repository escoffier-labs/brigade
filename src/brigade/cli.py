"""brigade command-line entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from .work_cmd import TASK_PRIORITIES, TASK_TYPES
from .prompt import prompt_for_selection  # imported here so tests can monkeypatch cli.prompt_for_selection


def _build_parser() -> argparse.ArgumentParser:
    from . import learn_cmd, projects_cmd, repos_cmd

    parser = argparse.ArgumentParser(
        prog="brigade",
        description="Brigade: run your agent brigade. Operator-system CLI for agent workspaces.",
    )
    parser.add_argument(
        "--version", action="version", version=f"brigade {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # init
    p_init = sub.add_parser("init", help="Materialize a selection into a target directory.")
    p_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Where to install.")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files.")
    p_init.add_argument(
        "--allow-home",
        action="store_true",
        help="Override the safety guard that refuses to install directly into $HOME.",
    )
    p_init.add_argument(
        "--no-gitignore",
        dest="update_gitignore",
        action="store_false",
        default=True,
        help="Do not create or update the target's .gitignore.",
    )
    p_init.add_argument("--dry-run", action="store_true", help="Show what would happen.")
    p_init.add_argument(
        "--depth",
        choices=["repo", "workspace"],
        default=None,
        help="Install depth: 'repo' (minimal) or 'workspace' (full home). "
             "Omit for an interactive prompt.",
    )
    p_init.add_argument(
        "--harnesses",
        default=None,
        help="Comma-separated harness ids: claude, codex, openclaw, hermes. "
             "Pass 'none' for a generic install with no harness-specific files.",
    )
    p_init.add_argument(
        "--owner",
        default=None,
        help="Override the canonical memory owner. Must be 'this-repo' or one of --harnesses.",
    )
    p_init.add_argument(
        "--include",
        dest="includes",
        action="append",
        default=[],
        help="Optional add-on (currently: 'publisher'). May be repeated.",
    )

    # doctor
    p_doctor = sub.add_parser("doctor", help="Verify a target workspace.")
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--harness",
        choices=["generic", "openclaw", "hermes"],
        default="generic",
    )

    # status
    p_status = sub.add_parser("status", help="Show which stations are present and healthy.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."))

    # add
    p_add = sub.add_parser("add", help="Install and wire a station's managed tools.")
    p_add.add_argument("station", help="Station to add tools for (e.g. memory, guard, tokens).")
    p_add.add_argument("--target", "-t", type=Path, default=Path("."))

    # dogfood
    p_dogfood = sub.add_parser("dogfood", help="Run a safe Codex-only Brigade dogfood review.")
    p_dogfood.add_argument(
        "dogfood_args",
        nargs="*",
        help="Dogfood task, or `init` to write local dogfood defaults.",
    )
    p_dogfood.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_dogfood.add_argument("--output-dir", type=Path, default=None, help="Directory for run artifacts.")
    p_dogfood.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to .codex/memory-handoffs under the effective target.",
    )
    p_dogfood.add_argument("--force", action="store_true", help="Overwrite an existing dogfood config during init.")
    p_dogfood.add_argument("--no-handoff", action="store_true", help="Do not write a Memory Handoff.")
    p_dogfood.add_argument("--no-inspect", action="store_true", help="Do not print the artifact summary afterward.")
    p_dogfood.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox instead of dogfood's default trusted-workspace danger-full-access setting.",
    )
    p_dogfood.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout.")

    # release
    p_release = sub.add_parser("release", help="Inspect local release readiness.")
    release_sub = p_release.add_subparsers(dest="release_command", metavar="<release-command>")
    release_sub.required = True
    p_release_plan = release_sub.add_parser("plan", help="Plan release readiness without writing a receipt.")
    p_release_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_plan.add_argument("--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks.")
    p_release_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_doctor = release_sub.add_parser("doctor", help="Run local release readiness checks without writing a receipt.")
    p_release_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_doctor.add_argument("--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks.")
    p_release_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_run = release_sub.add_parser("run", help="Run local release readiness checks and write a receipt.")
    p_release_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_run.add_argument("--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks.")
    p_release_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_runs = release_sub.add_parser("runs", help="List local release readiness receipts.")
    p_release_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_release_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_show = release_sub.add_parser("show", help="Show one local release readiness receipt.")
    p_release_show.add_argument("run_id", help="Run id, unique prefix, or latest.")
    p_release_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate = release_sub.add_parser("candidate", help="Build and inspect local release candidate bundles.")
    release_candidate_sub = p_release_candidate.add_subparsers(dest="release_candidate_command", metavar="<candidate-command>")
    release_candidate_sub.required = True
    p_release_candidate_plan = release_candidate_sub.add_parser("plan", help="Plan a release candidate bundle without writing it.")
    p_release_candidate_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_candidate_plan.add_argument("--base-ref", default="origin/main", help="Base ref for changed files and release notes.")
    p_release_candidate_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_build = release_candidate_sub.add_parser("build", help="Build a local release candidate bundle.")
    p_release_candidate_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_candidate_build.add_argument("--base-ref", default="origin/main", help="Base ref for changed files and release notes.")
    p_release_candidate_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_list = release_candidate_sub.add_parser("list", help="List local release candidate bundles.")
    p_release_candidate_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_candidate_list.add_argument("--limit", type=int, default=20, help="Maximum candidates to list.")
    p_release_candidate_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_show = release_candidate_sub.add_parser("show", help="Show one local release candidate bundle.")
    p_release_candidate_show.add_argument("candidate_id", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_candidate_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_archive = release_candidate_sub.add_parser("archive", help="Archive one local release candidate bundle.")
    p_release_candidate_archive.add_argument("candidate_id", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_candidate_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_compare = release_candidate_sub.add_parser("compare", help="Compare a candidate against current local state.")
    p_release_candidate_compare.add_argument("candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_candidate_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_closeout = release_candidate_sub.add_parser("closeout", help="Mark a local release candidate review state.")
    p_release_candidate_closeout.add_argument("candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_candidate_closeout.add_argument("--status", choices=["draft", "reviewed", "superseded", "archived"], default="reviewed")
    p_release_candidate_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_release_candidate_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # roadmap
    p_roadmap = sub.add_parser("roadmap", help="Inspect roadmap completion state.")
    roadmap_sub = p_roadmap.add_subparsers(dest="roadmap_command", metavar="<roadmap-command>")
    roadmap_sub.required = True
    p_roadmap_audit = roadmap_sub.add_parser("audit", help="Audit ROADMAP.md and documented command coverage.")
    p_roadmap_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_audit.add_argument("--import-issues", action="store_true", help="Import roadmap audit issues into the work inbox.")
    p_roadmap_patterns = roadmap_sub.add_parser("patterns", help="Show neutral inspiration pattern coverage.")
    p_roadmap_patterns.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_patterns.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_commands = roadmap_sub.add_parser("commands", help="Show parser-derived command documentation coverage.")
    p_roadmap_commands.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_commands.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

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
    p_repos_import = repos_sub.add_parser("import-issues", help="Import repo fleet health issues into the work inbox.")
    p_repos_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_import.add_argument("--dry-run", action="store_true", help="Show counts without writing imports.")
    p_repos_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report = repos_sub.add_parser("report", help="Plan, build, and inspect local repo fleet reports.")
    repos_report_sub = p_repos_report.add_subparsers(dest="repos_report_command", metavar="<repos-report-command>")
    repos_report_sub.required = True
    for name in ("plan", "build"):
        p_repos_report_cmd = repos_report_sub.add_parser(name, help=f"{name.title()} a repo fleet report.")
        p_repos_report_cmd.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_repos_report_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_list = repos_report_sub.add_parser("list", help="List local repo fleet reports.")
    p_repos_report_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_repos_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_show = repos_report_sub.add_parser("show", help="Show one local repo fleet report.")
    p_repos_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_repos_report_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_archive = repos_report_sub.add_parser("archive", help="Archive one local repo fleet report.")
    p_repos_report_archive.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_repos_report_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_report_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_report_closeout = repos_report_sub.add_parser("closeout", help="Mark one local repo fleet report reviewed.")
    p_repos_report_closeout.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_repos_report_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_report_closeout.add_argument("--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed")
    p_repos_report_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions = repos_sub.add_parser("actions", help="Plan and manage local repo fleet actions.")
    repos_actions_sub = p_repos_actions.add_subparsers(dest="repos_actions_command", metavar="<repos-actions-command>")
    repos_actions_sub.required = True
    p_repos_actions_plan = repos_actions_sub.add_parser("plan", help="Plan fleet actions from a report.")
    p_repos_actions_plan.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_repos_actions_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_build = repos_actions_sub.add_parser("build", help="Build fleet actions from a report.")
    p_repos_actions_build.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_repos_actions_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_build.add_argument("--allow-unreviewed", action="store_true", help="Build from an unclosed report.")
    p_repos_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_list = repos_actions_sub.add_parser("list", help="List local repo fleet actions.")
    p_repos_actions_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_repos_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_show = repos_actions_sub.add_parser("show", help="Show one local repo fleet action.")
    p_repos_actions_show.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_repos_actions_state = repos_actions_sub.add_parser(name, help=f"Mark one fleet action {name}.")
        p_repos_actions_state.add_argument("action_id", help="Fleet action id or unique prefix.")
        p_repos_actions_state.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
        p_repos_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_defer = repos_actions_sub.add_parser("defer", help="Defer one local repo fleet action.")
    p_repos_actions_defer.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_repos_actions_defer.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_archive = repos_actions_sub.add_parser("archive", help="Archive completed local repo fleet actions.")
    p_repos_actions_archive.add_argument("--completed", action="store_true", required=True, help="Archive completed actions.")
    p_repos_actions_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_dispatch = repos_actions_sub.add_parser("dispatch", help="Dispatch reviewed fleet actions into target repo work imports.")
    p_repos_actions_dispatch.add_argument("dispatch_args", nargs="*", help="Use `plan <action-id>` or `apply <action-id>`. Omit with --all-reviewed.")
    p_repos_actions_dispatch.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_dispatch.add_argument("--all-reviewed", action="store_true", help="Dispatch all reviewed pending or active fleet actions.")
    p_repos_actions_dispatch.add_argument("--include-deferred", action="store_true", help="Allow dispatching deferred actions.")
    p_repos_actions_dispatch.add_argument("--dry-run", action="store_true", help="Plan without writing target imports or action metadata.")
    p_repos_actions_dispatch.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_reconcile = repos_actions_sub.add_parser("reconcile", help="Reconcile fleet actions against target repo evidence.")
    p_repos_actions_reconcile.add_argument("action_id", nargs="?", default=None, help="Fleet action id or unique prefix. Defaults to all actions.")
    p_repos_actions_reconcile.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_actions_context = repos_actions_sub.add_parser("context", help="Plan or build a target repo context pack for one fleet action.")
    p_repos_actions_context.add_argument("context_command", choices=["plan", "build"], help="Plan or build the context pack.")
    p_repos_actions_context.add_argument("action_id", help="Fleet action id or unique prefix.")
    p_repos_actions_context.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_actions_context.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep = repos_sub.add_parser("sweep", help="Plan, run, and close out explicit repo fleet evidence sweeps.")
    repos_sweep_sub = p_repos_sweep.add_subparsers(dest="repos_sweep_command", metavar="<repos-sweep-command>")
    repos_sweep_sub.required = True
    for name in ("plan", "run"):
        p_repos_sweep_cmd = repos_sweep_sub.add_parser(name, help=f"{name.title()} a repo fleet evidence sweep.")
        p_repos_sweep_cmd.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_repos_sweep_cmd.add_argument("--repo", dest="repo_ids", action="append", default=[], help="Repo id to include. May be repeated.")
        p_repos_sweep_cmd.add_argument("--all", dest="all_repos", action="store_true", help="Include all enabled repos.")
        p_repos_sweep_cmd.add_argument("--stale-only", action="store_true", help="Only include repos without a successful sweep.")
        p_repos_sweep_cmd.add_argument("--include-disabled", action="store_true", help="Allow disabled configured repos.")
        p_repos_sweep_cmd.add_argument("--force", action="store_true", help="Force a refresh even when evidence is fresh.")
        p_repos_sweep_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_runs = repos_sweep_sub.add_parser("runs", help="List repo fleet sweep receipts.")
    p_repos_sweep_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_sweep_runs.add_argument("--limit", type=int, default=20, help="Maximum sweeps to list.")
    p_repos_sweep_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_show = repos_sweep_sub.add_parser("show", help="Show one repo fleet sweep receipt.")
    p_repos_sweep_show.add_argument("sweep_id", help="Sweep id, unique prefix, or latest.")
    p_repos_sweep_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_sweep_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_sweep_closeout = repos_sweep_sub.add_parser("closeout", help="Close out one repo fleet sweep review.")
    p_repos_sweep_closeout.add_argument("sweep_id", nargs="?", default="latest", help="Sweep id, unique prefix, or latest.")
    p_repos_sweep_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_sweep_closeout.add_argument("--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed")
    p_repos_sweep_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_sweep_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release = repos_sub.add_parser("release", help="Plan and close out local repo fleet release trains.")
    repos_release_sub = p_repos_release.add_subparsers(dest="repos_release_command", metavar="<repos-release-command>")
    repos_release_sub.required = True
    for name in ("plan", "build"):
        p_repos_release_cmd = repos_release_sub.add_parser(name, help=f"{name.title()} a repo fleet release train.")
        p_repos_release_cmd.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_repos_release_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_list = repos_release_sub.add_parser("list", help="List repo fleet release trains.")
    p_repos_release_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_list.add_argument("--limit", type=int, default=20, help="Maximum trains to list.")
    p_repos_release_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("show", "compare", "archive"):
        p_repos_release_item = repos_release_sub.add_parser(name, help=f"{name.title()} a repo fleet release train.")
        p_repos_release_item.add_argument("train_id", help="Train id, unique prefix, or latest.")
        p_repos_release_item.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_repos_release_item.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("reconcile", "summary", "report", "checklist", "ready", "activity", "manifest", "audit"):
        p_repos_release_review = repos_release_sub.add_parser(name, help=f"{name.title()} one repo fleet release train.")
        p_repos_release_review.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
        p_repos_release_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_repos_release_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_hygiene = repos_release_sub.add_parser("hygiene", help="Check fleet release train hygiene.")
    p_repos_release_hygiene.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_hygiene.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_import = repos_release_sub.add_parser("import-issues", help="Import fleet release train issues into the local work inbox.")
    p_repos_release_import.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_import.add_argument("--dry-run", action="store_true", help="Validate without writing imports.")
    p_repos_release_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_closeout = repos_release_sub.add_parser("closeout", help="Close out one repo fleet release train.")
    p_repos_release_closeout.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_closeout.add_argument("--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed")
    p_repos_release_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_repos_release_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions = repos_release_sub.add_parser("actions", help="Plan and manage fleet release train actions.")
    repos_release_actions_sub = p_repos_release_actions.add_subparsers(dest="repos_release_actions_command", metavar="<repos-release-actions-command>")
    repos_release_actions_sub.required = True
    p_repos_release_actions_plan = repos_release_actions_sub.add_parser("plan", help="Plan actions from one fleet release train.")
    p_repos_release_actions_plan.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_actions_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_build = repos_release_actions_sub.add_parser("build", help="Build actions from one fleet release train.")
    p_repos_release_actions_build.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_actions_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_actions_build.add_argument("--allow-unreviewed", action="store_true", help="Build from an unclosed release train.")
    p_repos_release_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_list = repos_release_actions_sub.add_parser("list", help="List fleet release train actions.")
    p_repos_release_actions_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_repos_release_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_show = repos_release_actions_sub.add_parser("show", help="Show one fleet release train action.")
    p_repos_release_actions_show.add_argument("action_id", help="Release action id or unique prefix.")
    p_repos_release_actions_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_repos_release_actions_state = repos_release_actions_sub.add_parser(name, help=f"Mark one fleet release action {name}.")
        p_repos_release_actions_state.add_argument("action_id", help="Release action id or unique prefix.")
        p_repos_release_actions_state.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
        p_repos_release_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_defer = repos_release_actions_sub.add_parser("defer", help="Defer one fleet release train action.")
    p_repos_release_actions_defer.add_argument("action_id", help="Release action id or unique prefix.")
    p_repos_release_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_repos_release_actions_defer.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_actions_archive = repos_release_actions_sub.add_parser("archive", help="Archive completed fleet release actions.")
    p_repos_release_actions_archive.add_argument("--completed", action="store_true", required=True, help="Archive completed actions.")
    p_repos_release_actions_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence = repos_release_sub.add_parser("evidence", help="Record manual fleet release evidence.")
    repos_release_evidence_sub = p_repos_release_evidence.add_subparsers(dest="repos_release_evidence_command", metavar="<repos-release-evidence-command>")
    repos_release_evidence_sub.required = True
    p_repos_release_evidence_plan = repos_release_evidence_sub.add_parser("plan", help="Plan manual evidence records for a fleet release train.")
    p_repos_release_evidence_plan.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_evidence_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_evidence_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_record = repos_release_evidence_sub.add_parser("record", help="Record one manual fleet release evidence item.")
    p_repos_release_evidence_record.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_evidence_record.add_argument("--repo", dest="repo_id", required=True, help="Repo id from the train.")
    p_repos_release_evidence_record.add_argument("--step", required=True, choices=sorted(repos_cmd.RELEASE_EVIDENCE_STEPS), help="Manual release evidence step.")
    p_repos_release_evidence_record.add_argument("--status", required=True, choices=sorted(repos_cmd.RELEASE_EVIDENCE_STATUSES), help="Evidence status.")
    p_repos_release_evidence_record.add_argument("--summary", default=None, help="Safe summary.")
    p_repos_release_evidence_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_evidence_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_list = repos_release_evidence_sub.add_parser("list", help="List manual fleet release evidence records.")
    p_repos_release_evidence_list.add_argument("train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest.")
    p_repos_release_evidence_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_evidence_list.add_argument("--limit", type=int, default=50, help="Maximum records to list.")
    p_repos_release_evidence_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_evidence_show = repos_release_evidence_sub.add_parser("show", help="Show one manual fleet release evidence record.")
    p_repos_release_evidence_show.add_argument("evidence_id", help="Evidence id or unique prefix.")
    p_repos_release_evidence_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_evidence_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers = repos_release_sub.add_parser("waivers", help="Record and inspect fleet release waivers.")
    repos_release_waivers_sub = p_repos_release_waivers.add_subparsers(dest="repos_release_waivers_command", metavar="<repos-release-waivers-command>")
    repos_release_waivers_sub.required = True
    p_repos_release_waivers_record = repos_release_waivers_sub.add_parser("record", help="Record one active fleet release waiver.")
    p_repos_release_waivers_record.add_argument("train_id", nargs="?", default="latest", help="Train id, unique prefix, or latest.")
    p_repos_release_waivers_record.add_argument("--scope", required=True, choices=sorted(repos_cmd.RELEASE_WAIVER_SCOPES), help="Waiver scope.")
    p_repos_release_waivers_record.add_argument("--repo", dest="repo_id", default=None, help="Optional repo id from the train.")
    p_repos_release_waivers_record.add_argument("--reason", required=True, help="Safe waiver reason.")
    p_repos_release_waivers_record.add_argument("--expires-at", default=None, help="Optional ISO timestamp when the waiver should expire.")
    p_repos_release_waivers_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_waivers_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_list = repos_release_waivers_sub.add_parser("list", help="List fleet release waivers.")
    p_repos_release_waivers_list.add_argument("train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest.")
    p_repos_release_waivers_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_waivers_list.add_argument("--limit", type=int, default=50, help="Maximum waivers to list.")
    p_repos_release_waivers_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_show = repos_release_waivers_sub.add_parser("show", help="Show one fleet release waiver.")
    p_repos_release_waivers_show.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_waivers_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_revoke = repos_release_waivers_sub.add_parser("revoke", help="Revoke one fleet release waiver.")
    p_repos_release_waivers_revoke.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_revoke.add_argument("--reason", required=True, help="Safe revocation reason.")
    p_repos_release_waivers_revoke.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_waivers_revoke.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_renew = repos_release_waivers_sub.add_parser("renew", help="Renew one fleet release waiver.")
    p_repos_release_waivers_renew.add_argument("waiver_id", help="Waiver id or unique prefix.")
    p_repos_release_waivers_renew.add_argument("--reason", required=True, help="Safe renewal reason.")
    p_repos_release_waivers_renew.add_argument("--expires-at", default=None, help="Optional ISO timestamp when the waiver should expire.")
    p_repos_release_waivers_renew.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_waivers_renew.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_doctor = repos_release_waivers_sub.add_parser("doctor", help="Check fleet release waiver health.")
    p_repos_release_waivers_doctor.add_argument("train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest.")
    p_repos_release_waivers_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_release_waivers_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_import = repos_release_waivers_sub.add_parser("import-issues", help="Import fleet release waiver issues into the local work inbox.")
    p_repos_release_waivers_import.add_argument("train_id", nargs="?", default=None, help="Optional train id, unique prefix, or latest.")
    p_repos_release_waivers_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_waivers_import.add_argument("--dry-run", action="store_true", help="Validate without writing imports.")
    p_repos_release_waivers_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # handoff
    p_handoff = sub.add_parser("handoff", help="Inspect memory handoff inbox health.")
    handoff_sub = p_handoff.add_subparsers(dest="handoff_command", metavar="<handoff-command>")
    handoff_sub.required = True
    p_handoff_doctor = handoff_sub.add_parser("doctor", help="Check handoff inboxes against local source config.")
    p_handoff_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_doctor.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_lint = handoff_sub.add_parser("lint", help="Validate pending or explicit memory handoff files.")
    p_handoff_lint.add_argument("paths", nargs="*", type=Path, help="Handoff files to validate. Defaults to pending inbox files.")
    p_handoff_lint.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_lint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_list = handoff_sub.add_parser("list", help="List local Memory Handoff drafts.")
    p_handoff_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_list.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_list.add_argument("--limit", type=int, default=20, help="Maximum drafts to show.")
    p_handoff_show = handoff_sub.add_parser("show", help="Show one local Memory Handoff draft.")
    p_handoff_show.add_argument("draft_id", help="Draft id, filename, path, or unique prefix.")
    p_handoff_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_show.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_archive = handoff_sub.add_parser("archive", help="Archive reviewed local Memory Handoff drafts.")
    p_handoff_archive.add_argument("draft_id", nargs="?", help="Draft id, filename, path, or unique prefix.")
    p_handoff_archive.add_argument("--all-reviewed", action="store_true", help="Archive all lint-valid drafts.")
    p_handoff_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_archive.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_archive.add_argument("--reason", default=None, help="Review reason to store in archive metadata.")
    p_handoff_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_closeout = handoff_sub.add_parser("closeout", help="Write local handoff draft closeout metadata.")
    p_handoff_closeout.add_argument("draft_id", nargs="?", help="Draft id, filename, path, or unique prefix. Defaults to all pending drafts.")
    p_handoff_closeout.add_argument("--all", action="store_true", help="Close out all non-archived drafts.")
    p_handoff_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_closeout.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_handoff_closeout.add_argument("--defer", action="store_true", help="Mark selected drafts deferred instead of reviewed.")
    p_handoff_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_runs = handoff_sub.add_parser("runs", help="List local handoff ingestion receipts.")
    p_handoff_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to show.")
    p_handoff_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_run_show = handoff_sub.add_parser("run-show", help="Show one local handoff ingestion receipt.")
    p_handoff_run_show.add_argument("run_id", help="Run id or unique prefix.")
    p_handoff_run_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_run_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_reconcile = handoff_sub.add_parser("reconcile", help="Normalize the configured handoff ingestor latest-run log.")
    p_handoff_reconcile.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_reconcile.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_issues = handoff_sub.add_parser("issues", help="Group actionable handoff ingest issues.")
    p_handoff_issues.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_issues.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_issues.add_argument("--limit", type=int, default=20, help="Maximum issue rows to print.")
    p_handoff_issues.add_argument("--category", action="append", default=[], help="Limit to one issue category. May be repeated.")
    p_handoff_import_issues = handoff_sub.add_parser("import-issues", help="Import handoff ingest issues into the work inbox.")
    p_handoff_import_issues.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_import_issues.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_import_issues.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_handoff_import_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_import_issues.add_argument("--category", action="append", default=[], help="Import only one issue category. May be repeated.")
    p_handoff_sync_issues = handoff_sub.add_parser("sync-issues", help="Import current handoff issues and close stale local handoff work.")
    p_handoff_sync_issues.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_sync_issues.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_sync_issues.add_argument("--dry-run", action="store_true", help="Report without writing imports or closing stale items.")
    p_handoff_sync_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_sync_issues.add_argument("--category", action="append", default=[], help="Sync only one issue category. May be repeated.")
    p_handoff_sync_issues.add_argument("--no-close-stale", action="store_true", help="Do not dismiss stale imports or close stale tasks.")

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
    p_memory_care_scan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_memory_care_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_status = memory_care_sub.add_parser("status", help="Show local memory-care status.")
    p_memory_care_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_memory_care_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_doctor = memory_care_sub.add_parser("doctor", help="Check local memory-care health.")
    p_memory_care_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_memory_care_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_import = memory_care_sub.add_parser("import-issues", help="Import memory-care issues into the work inbox.")
    p_memory_care_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_memory_care_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_memory_care_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_care_closeout = memory_care_sub.add_parser("closeout", help="Write local memory-care closeout metadata.")
    p_memory_care_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_memory_care_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_memory_care_closeout.add_argument("--defer", action="store_true", help="Mark current queue deferred instead of reviewed.")
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
    p_work_bootstrap.add_argument("--no-inspect", action="store_true", help="Do not inspect dogfood artifacts by default.")
    p_work_bootstrap.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox for dogfood runs.",
    )
    p_work_bootstrap.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout.")
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
    p_work_sweep.add_argument("--force", action="store_true", help="Run even when another scanner receipt is marked running.")
    p_work_sweep.add_argument("--no-ingest", action="store_true", help="Do not ingest configured scanner import output.")
    p_work_sweep.add_argument("--reason", default=None, help="Review closeout reason when using `closeout`.")
    p_work_sweep.add_argument("--defer", action="append", default=[], help="Defer one pending import during sweep closeout. May be repeated.")
    p_work_sweep.add_argument("--defer-all", action="store_true", help="Defer every pending import during sweep closeout.")
    p_work_sweep.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep.add_argument("sweep_args", nargs="*", help="Use `closeout <sweep-id|latest>` to mark a sweep reviewed.")
    p_work_sweeps = work_sub.add_parser("sweeps", help="List scanner sweep reports.")
    p_work_sweeps.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_sweeps.add_argument("--limit", type=int, default=20, help="Maximum sweeps to list.")
    p_work_sweeps.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep_show = work_sub.add_parser("sweep-show", help="Show one scanner sweep report.")
    p_work_sweep_show.add_argument("sweep_id", help="Sweep id or unique prefix.")
    p_work_sweep_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_sweep_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_sweep_review = work_sub.add_parser("sweep-review", help="Review imports created by one scanner sweep.")
    p_work_sweep_review.add_argument("sweep_id", help="Sweep id, unique prefix, or latest.")
    p_work_sweep_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_sweep_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify = work_sub.add_parser("verify", help="Plan and run local work verification.")
    verify_sub = p_work_verify.add_subparsers(dest="verify_command", metavar="<verify-command>")
    verify_sub.required = True
    p_work_verify_plan = verify_sub.add_parser("plan", help="Plan local verification without running commands.")
    p_work_verify_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_verify_plan.add_argument("--command", dest="verify_commands", action="append", default=None, help="Verification command. May be repeated.")
    p_work_verify_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_run = verify_sub.add_parser("run", help="Run local verification commands and write a receipt.")
    p_work_verify_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_verify_run.add_argument("--command", dest="verify_commands", action="append", default=None, help="Verification command. May be repeated.")
    p_work_verify_run.add_argument("--timeout", type=int, default=900, help="Timeout per command in seconds.")
    p_work_verify_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_runs = verify_sub.add_parser("runs", help="List local work verification receipts.")
    p_work_verify_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_verify_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_verify_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_verify_show = verify_sub.add_parser("show", help="Show one local work verification receipt.")
    p_work_verify_show.add_argument("run_id", help="Run id, unique prefix, or latest.")
    p_work_verify_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
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
    p_work_inbox_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_inbox_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_inbox_archive = inbox_sub.add_parser("archive", help="Archive old closed scanner inbox imports.")
    p_work_inbox_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_inbox_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup = work_sub.add_parser("backup", help="Inspect local backup health summaries.")
    backup_sub = p_work_backup.add_subparsers(dest="backup_command", metavar="<backup-command>")
    backup_sub.required = True
    p_work_backup_init = backup_sub.add_parser("init", help="Write a local backup health config.")
    p_work_backup_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_backup_init.add_argument("--force", action="store_true", help="Overwrite an existing backup config.")
    p_work_backup_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_backup_status = backup_sub.add_parser("status", help="Show local backup health status.")
    p_work_backup_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_backup_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_doctor = backup_sub.add_parser("doctor", help="Check local backup health summaries.")
    p_work_backup_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_backup_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_import = backup_sub.add_parser("import-issues", help="Import backup health issues into the work inbox.")
    p_work_backup_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_backup_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_backup_closeout = backup_sub.add_parser("closeout", help="Write local backup health closeout metadata.")
    p_work_backup_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_backup_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_work_backup_closeout.add_argument("--defer", action="store_true", help="Mark current backup issues deferred instead of reviewed.")
    p_work_backup_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners = work_sub.add_parser("scanners", help="Inspect local scanner registry and schedule plans.")
    scanners_sub = p_work_scanners.add_subparsers(dest="scanners_command", metavar="<scanners-command>")
    scanners_sub.required = True
    p_work_scanners_init = scanners_sub.add_parser("init", help="Write a local scanner registry config.")
    p_work_scanners_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_scanners_init.add_argument("--force", action="store_true", help="Overwrite an existing scanner config.")
    p_work_scanners_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_scanners_list = scanners_sub.add_parser("list", help="List configured local scanners.")
    p_work_scanners_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_show = scanners_sub.add_parser("show", help="Show one configured scanner.")
    p_work_scanners_show.add_argument("scanner_id", help="Scanner id.")
    p_work_scanners_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_plan = scanners_sub.add_parser("plan", help="Plan scanner run windows without executing scanners.")
    p_work_scanners_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_run = scanners_sub.add_parser("run", help="Run configured local scanners explicitly.")
    p_work_scanners_run.add_argument("scanner_id", nargs="?", default=None, help="Scanner id to run.")
    p_work_scanners_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_scanners_run.add_argument("--all", action="store_true", help="Run all configured scanners.")
    p_work_scanners_run.add_argument("--due", action="store_true", help="Run due scanners only.")
    p_work_scanners_run.add_argument("--include-disabled", action="store_true", help="Allow disabled scanners to run.")
    p_work_scanners_run.add_argument("--force", action="store_true", help="Run even when another scanner receipt is marked running.")
    p_work_scanners_run.add_argument("--ingest-output", action="store_true", help="Validate and ingest configured JSONL output after successful runs.")
    p_work_scanners_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_runs = scanners_sub.add_parser("runs", help="List local scanner run receipts.")
    p_work_scanners_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_scanners_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_run_show = scanners_sub.add_parser("run-show", help="Show one scanner run receipt.")
    p_work_scanners_run_show.add_argument("run_id", help="Run id or unique prefix.")
    p_work_scanners_run_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_run_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_doctor = scanners_sub.add_parser("doctor", help="Check scanner registry health.")
    p_work_scanners_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_scanners_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_scanners_doctor.add_argument("--import-issues", action="store_true", help="Import scanner health issues into the work inbox.")
    p_work_review = work_sub.add_parser("review", help="Run explicit local code review producers.")
    review_sub = p_work_review.add_subparsers(dest="review_command", metavar="<review-command>")
    review_sub.required = True
    p_work_review_init = review_sub.add_parser("init", help="Write local code review producer config.")
    p_work_review_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_init.add_argument("--force", action="store_true", help="Overwrite an existing review config.")
    p_work_review_init.add_argument("--no-gitignore", action="store_true", help="Do not update the target .gitignore.")
    p_work_review_plan = review_sub.add_parser("plan", help="Plan configured code review producers without running them.")
    p_work_review_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_review_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_run = review_sub.add_parser("run", help="Run configured local code review producers explicitly.")
    p_work_review_run.add_argument("reviewer_id", nargs="?", default=None, help="Reviewer id to run.")
    p_work_review_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_run.add_argument("--all", action="store_true", help="Run all configured reviewers.")
    p_work_review_run.add_argument("--include-disabled", action="store_true", help="Allow disabled reviewers to run.")
    p_work_review_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_runs = review_sub.add_parser("runs", help="List local code review run receipts.")
    p_work_review_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_review_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")
    p_work_review_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_show = review_sub.add_parser("show", help="Show one code review run receipt.")
    p_work_review_show.add_argument("run_id", help="Run id or unique prefix.")
    p_work_review_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_review_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_import = review_sub.add_parser("import-findings", help="Import normalized review findings into the work inbox.")
    p_work_review_import.add_argument("run_id", help="Run id or unique prefix.")
    p_work_review_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_review_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_findings = review_sub.add_parser("findings", help="List imported code review findings.")
    p_work_review_findings.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_review_findings.add_argument("--run-id", default=None, help="Limit findings to one review run id.")
    p_work_review_findings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_finding_show = review_sub.add_parser("finding-show", help="Show one imported code review finding.")
    p_work_review_finding_show.add_argument("finding_id", help="Finding id, import id, or unique prefix.")
    p_work_review_finding_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_review_finding_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_review_closeout = review_sub.add_parser("closeout", help="Summarize one code review run's resolution state.")
    p_work_review_closeout.add_argument("run_id", help="Run id, unique prefix, or latest.")
    p_work_review_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_review_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_import_add.add_argument("--source", default="manual", help="Import source such as slack, discord, or memory-care.")
    p_work_import_add.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="Metadata as key=value. May be repeated.",
    )
    p_work_import_list = import_sub.add_parser("list", help="List local work imports.")
    p_work_import_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
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
    p_work_import_list.add_argument("--metadata", action="append", default=[], help="Filter by metadata key=value. May be repeated.")
    p_work_import_validate = import_sub.add_parser("validate", help="Validate a work import JSONL file.")
    p_work_import_validate.add_argument("input_path", type=Path, help="JSONL file to validate.")
    p_work_import_validate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_ingest = import_sub.add_parser("ingest", help="Validate and append a work import JSONL file.")
    p_work_import_ingest.add_argument("input_path", type=Path, help="JSONL file to ingest.")
    p_work_import_ingest.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_ingest.add_argument("--dry-run", action="store_true", help="Validate and report without writing imports.")
    p_work_import_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_plan = import_sub.add_parser("plan", help="Preview the task or action a work import would create.")
    p_work_import_plan.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_import_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_plan_handoff = import_sub.add_parser("plan-handoff", help="Preview the Memory Handoff a work import would create.")
    p_work_import_plan_handoff.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_plan_handoff.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_import_plan_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_memory_care = import_sub.add_parser("memory-care", help="Import memory-care refresh queue entries.")
    p_work_import_memory_care.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_memory_care.add_argument(
        "--queue",
        type=Path,
        default=None,
        help="Refresh queue JSON. Defaults to memory/cards/decay/refresh-queue.json under target.",
    )
    p_work_import_memory_care.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_memory_care.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_memory_refresh = import_sub.add_parser("memory-refresh", help="Import memory refresh candidates.")
    p_work_import_memory_refresh.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_memory_refresh.add_argument(
        "--queue",
        type=Path,
        default=None,
        help="Refresh queue JSON. Defaults to memory/cards/decay/refresh-queue.json under target.",
    )
    p_work_import_memory_refresh.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_memory_refresh.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_chat_sweep = import_sub.add_parser("chat-sweep", help="Import chat memory sweep issues.")
    p_work_import_chat_sweep.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_chat_sweep.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        default=None,
        help="Chat memory sweep JSON. Defaults to .brigade/chat-memory-sweeps/latest.json under target.",
    )
    p_work_import_chat_sweep.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_chat_sweep.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_triage = import_sub.add_parser("triage", help="Group pending imports by source and kind.")
    p_work_import_triage.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_import_triage.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_triage.add_argument("--limit", type=int, default=50, help="Maximum imports per group to show.")
    p_work_import_triage.add_argument("--source", default=None, help="Filter by import source.")
    p_work_import_triage.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Filter by import kind.",
    )
    p_work_import_triage.add_argument("--metadata", action="append", default=[], help="Filter by metadata key=value. May be repeated.")
    p_work_import_provenance = import_sub.add_parser("provenance", help="Audit producer import provenance fields.")
    p_work_import_provenance.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_import_provenance.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_show = import_sub.add_parser("show", help="Show one work import.")
    p_work_import_show.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_import_promote = import_sub.add_parser("promote", help="Promote one work import into the task ledger.")
    p_work_import_promote.add_argument("import_id", nargs="?", help="Import id or unique prefix.")
    p_work_import_promote.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_promote.add_argument("--all", action="store_true", help="Promote all pending imports matching filters.")
    p_work_import_promote.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Limit --all promotion to one kind.",
    )
    p_work_import_promote.add_argument("--source", default=None, help="Limit --all promotion to one source.")
    p_work_import_promote.add_argument("--metadata", action="append", default=[], help="Limit --all promotion by metadata key=value. May be repeated.")
    p_work_import_promote.add_argument("--run", action="store_true", help="Promote one task import and immediately run it.")
    p_work_import_promote_handoff = import_sub.add_parser("promote-handoff", help="Promote one reviewed work import into a Memory Handoff draft.")
    p_work_import_promote_handoff.add_argument("import_id", help="Import id or unique prefix.")
    p_work_import_promote_handoff.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_promote_handoff.add_argument("--run", action="store_true", help="For task imports, use the existing promote-and-run path.")
    p_work_import_promote_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_import_dismiss = import_sub.add_parser("dismiss", help="Dismiss one pending work import.")
    p_work_import_dismiss.add_argument("import_id", nargs="?", help="Import id or unique prefix.")
    p_work_import_dismiss.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_dismiss.add_argument("--all", action="store_true", help="Dismiss all pending imports matching filters.")
    p_work_import_dismiss.add_argument(
        "--kind",
        choices=["task", "finding", "decision", "preference", "incident", "link", "command"],
        default=None,
        help="Limit --all dismissal to one kind.",
    )
    p_work_import_dismiss.add_argument("--source", default=None, help="Limit --all dismissal to one source.")
    p_work_import_dismiss.add_argument("--metadata", action="append", default=[], help="Limit --all dismissal by metadata key=value. May be repeated.")
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
    p_work_run.add_argument("--recap-limit", type=int, default=1, help="Maximum sessions to include in the final recap.")
    p_work_run.add_argument("--queue-next", action="store_true", help="Queue the extracted next step after a successful run.")
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

    # chat
    p_chat = sub.add_parser("chat", help="Inspect and import local chat surface exports.")
    chat_sub = p_chat.add_subparsers(dest="chat_command", metavar="<chat-command>")
    chat_sub.required = True
    p_chat_surfaces = chat_sub.add_parser("surfaces", help="Manage local chat surface export config.")
    surfaces_sub = p_chat_surfaces.add_subparsers(dest="surfaces_command", metavar="<surfaces-command>")
    surfaces_sub.required = True
    p_chat_surfaces_init = surfaces_sub.add_parser("init", help="Write a starter .brigade/chat-surfaces.toml.")
    p_chat_surfaces_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_chat_surfaces_init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    p_chat_surfaces_init.add_argument("--no-gitignore", action="store_true", help="Do not update managed .gitignore.")
    p_chat_surfaces_list = surfaces_sub.add_parser("list", help="List configured chat surfaces.")
    p_chat_surfaces_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_chat_surfaces_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_surfaces_show = surfaces_sub.add_parser("show", help="Show one chat surface.")
    p_chat_surfaces_show.add_argument("surface_id", help="Surface id.")
    p_chat_surfaces_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_chat_surfaces_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_surfaces_doctor = surfaces_sub.add_parser("doctor", help="Check chat surface config health.")
    p_chat_surfaces_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_chat_surfaces_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep = chat_sub.add_parser("sweep", help="Validate, ingest, or import local chat sweep exports.")
    sweep_sub = p_chat_sweep.add_subparsers(dest="sweep_command", metavar="<sweep-command>")
    sweep_sub.required = True
    p_chat_sweep_validate = sweep_sub.add_parser("validate", help="Validate a chat export finding file.")
    p_chat_sweep_validate.add_argument("input_path", type=Path, help="Chat export JSON or JSONL file.")
    p_chat_sweep_validate.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace context.")
    p_chat_sweep_validate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep_ingest = sweep_sub.add_parser("ingest", help="Normalize one configured chat surface export.")
    p_chat_sweep_ingest.add_argument("surface_id", help="Surface id.")
    p_chat_sweep_ingest.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_chat_sweep_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep_import = sweep_sub.add_parser("import-issues", help="Import normalized chat sweep issues.")
    p_chat_sweep_import.add_argument("surface_id", help="Surface id.")
    p_chat_sweep_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_chat_sweep_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

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
    p_context_sync.add_argument("sync_command", choices=["plan", "record"], help="Plan or record a read-only sync plan.")
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
    p_projects_import = projects_sub.add_parser("import-issues", help="Import project consolidation issues into the work inbox.")
    p_projects_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_projects_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_projects_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout = projects_sub.add_parser("closeout", help="Write a reviewed project migration closeout receipt.")
    p_projects_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_projects_closeout.add_argument("--status", choices=sorted(projects_cmd.PROJECT_CLOSEOUT_STATUSES), required=True, help="Closeout status.")
    p_projects_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_projects_closeout.add_argument("--project-id", default=None, help="Close out one blocked project.")
    p_projects_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeouts = projects_sub.add_parser("closeouts", help="List project migration closeout receipts.")
    p_projects_closeouts.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout_show = projects_sub.add_parser("closeout-show", help="Show one project migration closeout receipt.")
    p_projects_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_projects_closeout_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness = projects_sub.add_parser("readiness", help="Plan and record project migration readiness receipts.")
    projects_readiness_sub = p_projects_readiness.add_subparsers(dest="projects_readiness_command", metavar="<projects-readiness-command>")
    projects_readiness_sub.required = True
    p_projects_readiness_plan = projects_readiness_sub.add_parser("plan", help="Plan project migration readiness without writing a receipt.")
    p_projects_readiness_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_record = projects_readiness_sub.add_parser("record", help="Write a local project migration readiness receipt.")
    p_projects_readiness_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_projects_readiness_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_list = projects_readiness_sub.add_parser("list", help="List local project migration readiness receipts.")
    p_projects_readiness_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_show = projects_readiness_sub.add_parser("show", help="Show a local project migration readiness receipt.")
    p_projects_readiness_show.add_argument("readiness_id", help="Readiness receipt id or latest.")
    p_projects_readiness_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
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
    p_learn_closeout = learn_sub.add_parser("closeout", help="Close out a learning candidate as accepted, dismissed, archived, or deferred.")
    p_learn_closeout.add_argument("candidate_id", help="Learning candidate id.")
    p_learn_closeout.add_argument("--subsystem", default=None, help="Disambiguate by subsystem.")
    p_learn_closeout.add_argument("--status", choices=sorted(learn_cmd.LEARNING_CLOSEOUT_STATUSES), required=True, help="Closeout status.")
    p_learn_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_learn_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeouts = learn_sub.add_parser("closeouts", help="List learning closeout receipts.")
    p_learn_closeouts.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeout_show = learn_sub.add_parser("closeout-show", help="Show a learning closeout receipt.")
    p_learn_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_learn_closeout_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay = learn_sub.add_parser("replay", help="Export, inspect, and compare safe learning replay receipts.")
    learn_replay_sub = p_learn_replay.add_subparsers(dest="learn_replay_command", metavar="<learn-replay-command>")
    learn_replay_sub.required = True
    p_learn_replay_export = learn_replay_sub.add_parser("export", help="Export a safe before/after learning replay receipt.")
    p_learn_replay_export.add_argument("scenario_id", help="Scenario id.")
    p_learn_replay_export.add_argument("--before-summary", required=True, help="Safe before summary.")
    p_learn_replay_export.add_argument("--after-summary", required=True, help="Safe after summary.")
    p_learn_replay_export.add_argument("--before-count", type=int, default=None, help="Optional before candidate count.")
    p_learn_replay_export.add_argument("--after-count", type=int, default=None, help="Optional after candidate count.")
    p_learn_replay_export.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_replay_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_list = learn_replay_sub.add_parser("list", help="List learning replay receipts.")
    p_learn_replay_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_replay_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_show = learn_replay_sub.add_parser("show", help="Show a learning replay receipt.")
    p_learn_replay_show.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_replay_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_compare = learn_replay_sub.add_parser("compare", help="Compare a learning replay before and after state.")
    p_learn_replay_compare.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_replay_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # center
    p_center = sub.add_parser("center", help="Read local operator-center summaries.")
    center_sub = p_center.add_subparsers(dest="center_command", metavar="<center-command>")
    center_sub.required = True
    for name in ("status", "activity", "reviews", "templates"):
        p_center_action = center_sub.add_parser(name, help=f"Show local operator-center {name}.")
        p_center_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_center_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if name in {"activity", "reviews"}:
            p_center_action.add_argument("--limit", type=int, default=50, help="Maximum rows to show.")
    p_center_report = center_sub.add_parser("report", help="Plan, build, and inspect local operator report bundles.")
    center_report_sub = p_center_report.add_subparsers(dest="center_report_command", metavar="<center-report-command>")
    center_report_sub.required = True
    p_center_report_plan = center_report_sub.add_parser("plan", help="Plan a local operator report without writing it.")
    p_center_report_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_report_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_build = center_report_sub.add_parser("build", help="Build a local operator report bundle.")
    p_center_report_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_list = center_report_sub.add_parser("list", help="List local operator report bundles.")
    p_center_report_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_center_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_show = center_report_sub.add_parser("show", help="Show one local operator report bundle.")
    p_center_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_archive = center_report_sub.add_parser("archive", help="Archive one local operator report bundle.")
    p_center_report_archive.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_report_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_review = center_report_sub.add_parser("review", help="Review one local operator report action plan.")
    p_center_report_review.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_center_report_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_report_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_compare = center_report_sub.add_parser("compare", help="Compare one operator report against current local state.")
    p_center_report_compare.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_center_report_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_closeout = center_report_sub.add_parser("closeout", help="Mark one operator report review state.")
    p_center_report_closeout.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_center_report_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_report_closeout.add_argument("--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed")
    p_center_report_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_center_report_closeout.add_argument("--defer-item", action="append", default=[], help="Deferred report item id. May be repeated.")
    p_center_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions = center_sub.add_parser("actions", help="Plan and manage local daily operator actions.")
    center_actions_sub = p_center_actions.add_subparsers(dest="center_actions_command", metavar="<center-actions-command>")
    center_actions_sub.required = True
    p_center_actions_plan = center_actions_sub.add_parser("plan", help="Plan daily actions from an operator report.")
    p_center_actions_plan.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_center_actions_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_build = center_actions_sub.add_parser("build", help="Build a daily action queue from an operator report.")
    p_center_actions_build.add_argument("report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest.")
    p_center_actions_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_actions_build.add_argument("--allow-unreviewed", action="store_true", help="Build from an unclosed report.")
    p_center_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_list = center_actions_sub.add_parser("list", help="List local daily operator actions.")
    p_center_actions_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_center_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_show = center_actions_sub.add_parser("show", help="Show one local daily operator action.")
    p_center_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_center_actions_state = center_actions_sub.add_parser(name, help=f"Mark one action {name}.")
        p_center_actions_state.add_argument("action_id", help="Action id or unique prefix.")
        p_center_actions_state.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
        p_center_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_defer = center_actions_sub.add_parser("defer", help="Defer one local daily operator action.")
    p_center_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_center_actions_defer.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_archive = center_actions_sub.add_parser("archive", help="Archive completed local daily operator actions.")
    p_center_actions_archive.add_argument("--completed", action="store_true", required=True, help="Archive completed actions.")
    p_center_actions_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
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
    p_security_fix = security_sub.add_parser("fix", help="Apply safe local security hygiene fixes.")
    p_security_fix.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_fix.add_argument("--dry-run", action="store_true", help="Show changes without writing files.")
    p_security_review = security_sub.add_parser("review", help="Review the latest local security evidence bundle.")
    p_security_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_review.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_findings = security_sub.add_parser("findings", help="List local security findings.")
    p_security_findings.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_findings.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_findings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_sarif = security_sub.add_parser("sarif", help="Write SARIF for an existing security report.")
    p_security_sarif.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_sarif.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_sarif.add_argument("--output-path", type=Path, default=None, help="SARIF output path. Defaults to security-report.sarif in the bundle.")
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
    p_security_enrich.add_argument("--provider", choices=["local", "misp"], default=None, help="Override configured provider.")
    p_security_enrich.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_suppress = security_sub.add_parser("suppress", help="Suppress a reviewed security finding.")
    p_security_suppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to suppress.")
    p_security_suppress.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_suppress.add_argument("--reason", required=True, help="Required suppression reason.")
    p_security_unsuppress = security_sub.add_parser("unsuppress", help="Remove a security finding suppression.")
    p_security_unsuppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to unsuppress.")
    p_security_unsuppress.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_closeout = security_sub.add_parser("closeout", help="Write local security review closeout metadata.")
    p_security_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_closeout.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_security_closeout.add_argument("--accept-risk", action="store_true", help="Mark open findings as locally accepted risk.")
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
    p_tools_call_approve = tools_call_sub.add_parser("approve", help="Approve one queued portable tool call without executing it.")
    p_tools_call_approve.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_approve.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_approve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_reject = tools_call_sub.add_parser("reject", help="Reject one queued portable tool call.")
    p_tools_call_reject.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_reject.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_reject.add_argument("--reason", required=True, help="Review reason.")
    p_tools_call_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_hold = tools_call_sub.add_parser("hold", help="Hold one queued portable tool call.")
    p_tools_call_hold.add_argument("call_id", help="Call id or unique prefix.")
    p_tools_call_hold.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_call_hold.add_argument("--reason", required=True, help="Review reason.")
    p_tools_call_hold.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_call_run = tools_call_sub.add_parser("run", help="Run one approved portable tool call and write a local receipt.")
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
    p_tools_run_latest = tools_run_sub.add_parser("latest", help="Show the latest local portable tool execution receipt.")
    p_tools_run_latest.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_run_latest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_run_replay = tools_run_sub.add_parser("replay", help="Queue a reviewed replay candidate from one run receipt.")
    p_tools_run_replay.add_argument("run_id", help="Run id or unique prefix.")
    p_tools_run_replay.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_run_replay.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint = tools_sub.add_parser("checkpoint", help="Review and resume portable tool execution checkpoints.")
    tools_checkpoint_sub = p_tools_checkpoint.add_subparsers(dest="tools_checkpoint_command", metavar="<tools-checkpoint-command>")
    tools_checkpoint_sub.required = True
    p_tools_checkpoint_list = tools_checkpoint_sub.add_parser("list", help="List local portable tool checkpoints.")
    p_tools_checkpoint_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_checkpoint_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_show = tools_checkpoint_sub.add_parser("show", help="Show one local portable tool checkpoint.")
    p_tools_checkpoint_show.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_checkpoint_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_approve = tools_checkpoint_sub.add_parser("approve", help="Approve one checkpoint for explicit resume.")
    p_tools_checkpoint_approve.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_approve.add_argument("--choice", required=True, help="Allowed resume choice.")
    p_tools_checkpoint_approve.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_checkpoint_approve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_reject = tools_checkpoint_sub.add_parser("reject", help="Reject one checkpoint.")
    p_tools_checkpoint_reject.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_reject.add_argument("--reason", required=True, help="Review reason.")
    p_tools_checkpoint_reject.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_checkpoint_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_checkpoint_resume = tools_checkpoint_sub.add_parser("resume", help="Resume one approved checkpoint.")
    p_tools_checkpoint_resume.add_argument("checkpoint_id", help="Checkpoint id or unique prefix.")
    p_tools_checkpoint_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_checkpoint_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime = tools_sub.add_parser("runtime", help="Manage explicit local portable tool runtimes.")
    tools_runtime_sub = p_tools_runtime.add_subparsers(dest="tools_runtime_command", metavar="<tools-runtime-command>")
    tools_runtime_sub.required = True
    p_tools_runtime_init = tools_runtime_sub.add_parser("init", help="Write a local portable tool runtime config.")
    p_tools_runtime_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_runtime_init.add_argument("--force", action="store_true", help="Overwrite existing runtime config.")
    p_tools_runtime_list = tools_runtime_sub.add_parser("list", help="List configured portable tool runtimes.")
    p_tools_runtime_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_runtime_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_show = tools_runtime_sub.add_parser("show", help="Show one portable tool runtime.")
    p_tools_runtime_show.add_argument("runtime_id", help="Runtime id.")
    p_tools_runtime_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_runtime_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_status = tools_runtime_sub.add_parser("status", help="Show portable tool runtime process status.")
    p_tools_runtime_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_runtime_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for runtime_command in ("start", "stop", "restart"):
        p_runtime_action = tools_runtime_sub.add_parser(runtime_command, help=f"{runtime_command.title()} one portable tool runtime.")
        p_runtime_action.add_argument("runtime_id", help="Runtime id.")
        p_runtime_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
        p_runtime_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_runtime_doctor = tools_runtime_sub.add_parser("doctor", help="Check portable tool runtime health.")
    p_tools_runtime_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_runtime_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_policy = tools_sub.add_parser("policy", help="Inspect host-local portable tool execution policy.")
    tools_policy_sub = p_tools_policy.add_subparsers(dest="tools_policy_command", metavar="<tools-policy-command>")
    tools_policy_sub.required = True
    p_tools_policy_init = tools_policy_sub.add_parser("init", help="Write a local portable tool execution policy.")
    p_tools_policy_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_policy_init.add_argument("--force", action="store_true", help="Overwrite existing policy config.")
    p_tools_policy_show = tools_policy_sub.add_parser("show", help="Show local portable tool execution policy.")
    p_tools_policy_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_policy_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_policy_doctor = tools_policy_sub.add_parser("doctor", help="Check portable tool execution policy health.")
    p_tools_policy_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_policy_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_parity = tools_sub.add_parser("parity", help="Inspect and close out portable tool projection parity.")
    tools_parity_sub = p_tools_parity.add_subparsers(dest="tools_parity_command", metavar="<tools-parity-command>")
    tools_parity_sub.required = True
    p_tools_parity_status = tools_parity_sub.add_parser("status", help="Show projection parity closeout state.")
    p_tools_parity_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_tools_parity_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools_parity_closeout = tools_parity_sub.add_parser("closeout", help="Close out current projection parity issues.")
    p_tools_parity_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_parity_closeout.add_argument("--reason", default="", help="Review or defer reason.")
    p_tools_parity_closeout.add_argument("--defer", action="store_true", help="Mark parity issues deferred instead of reviewed.")
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
    p_tools_pack_archive = tools_pack_sub.add_parser("archive", help="Archive one local portable tool pack.")
    p_tools_pack_archive.add_argument("pack_id", help="Pack id or unique prefix.")
    p_tools_pack_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
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
    p_tools_sync_apply.add_argument("--dry-run", action="store_true", default=True, help="Plan writes without changing files.")
    p_tools_sync_apply.add_argument("--write", dest="dry_run", action="store_false", help="Write reviewed add-only projections.")
    p_tools_sync_apply.add_argument("--force", action="store_true", help="Allow intentional overwrites through managed apply.")
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
    p_tools_apply.add_argument("--force", action="store_true", help="Overwrite unmanaged or locally edited projection files.")
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
    p_recon.add_argument("--prune", action="store_true",
                         help="Remove files for harnesses no longer selected.")

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    if cmd == "init":
        # New v0.3.0 path: --depth/--harnesses build a Selection directly.
        if getattr(args, "depth", None) is not None or getattr(args, "harnesses", None) is not None:
            from .selection import Selection, KNOWN_HARNESSES, resolve_owner
            from .install import install_selection

            depth = args.depth or "repo"
            if args.harnesses is None or args.harnesses == "":
                harnesses = ["claude"]
            elif args.harnesses == "none":
                harnesses = []
            else:
                harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
            for h in harnesses:
                if h not in KNOWN_HARNESSES:
                    print(f"error: unknown harness {h!r} (valid: {KNOWN_HARNESSES})", file=sys.stderr)
                    return 2
            try:
                owner = resolve_owner(harnesses, override=args.owner)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=list(args.includes))
            return install_selection(
                target=args.target,
                selection=sel,
                force=getattr(args, "force", False),
                dry_run=getattr(args, "dry_run", False),
                allow_home=getattr(args, "allow_home", False),
            )

        # No selection flags: interactive prompt.
        from .prompt import NonInteractiveError
        from .install import install_selection
        try:
            sel = prompt_for_selection()
        except NonInteractiveError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return install_selection(
            target=args.target,
            selection=sel,
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            allow_home=getattr(args, "allow_home", False),
        )
    if cmd == "doctor":
        from . import doctor as doctor_mod

        return doctor_mod.run(target=args.target, harness=args.harness)
    if cmd == "status":
        from . import status as status_mod

        return status_mod.run(target=args.target)
    if cmd == "add":
        from . import add as add_mod

        return add_mod.run(target=args.target, station=args.station)
    if cmd == "dogfood":
        from . import dogfood_cmd

        dogfood_args = list(args.dogfood_args)
        if dogfood_args and dogfood_args[0] == "init":
            if len(dogfood_args) > 1:
                print("error: dogfood init does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.init(
                target=args.target,
                artifacts_dir=args.output_dir,
                handoff_inbox=args.handoff_inbox,
                force=args.force,
                handoff=not args.no_handoff,
                inspect=not args.no_inspect,
                native_read_only_sandbox=args.native_read_only_sandbox,
                timeout_seconds=args.timeout_seconds,
            )
        if dogfood_args and dogfood_args[0] == "status":
            if len(dogfood_args) > 1:
                print("error: dogfood status does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.status(target=args.target)
        if dogfood_args and dogfood_args[0] == "latest":
            if len(dogfood_args) > 1:
                print("error: dogfood latest does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.latest(target=args.target)
        if dogfood_args and dogfood_args[0] == "next":
            if len(dogfood_args) > 1:
                print("error: dogfood next does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.next_step(target=args.target)
        task = " ".join(dogfood_args) if dogfood_args else None
        return dogfood_cmd.run(
            task,
            target=args.target,
            output_dir=args.output_dir,
            handoff=not args.no_handoff,
            handoff_inbox=args.handoff_inbox,
            inspect=not args.no_inspect,
            native_read_only_sandbox=args.native_read_only_sandbox,
            timeout_seconds=args.timeout_seconds,
        )
    if cmd == "release":
        from . import release_cmd

        if args.release_command == "plan":
            return release_cmd.plan(target=args.target, base_ref=args.base_ref, json_output=args.json)
        if args.release_command == "doctor":
            return release_cmd.doctor(target=args.target, base_ref=args.base_ref, json_output=args.json)
        if args.release_command == "run":
            return release_cmd.run(target=args.target, base_ref=args.base_ref, json_output=args.json)
        if args.release_command == "runs":
            return release_cmd.runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.release_command == "show":
            return release_cmd.show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.release_command == "candidate":
            if args.release_candidate_command == "plan":
                return release_cmd.candidate_plan(target=args.target, base_ref=args.base_ref, json_output=args.json)
            if args.release_candidate_command == "build":
                return release_cmd.candidate_build(target=args.target, base_ref=args.base_ref, json_output=args.json)
            if args.release_candidate_command == "list":
                return release_cmd.candidate_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.release_candidate_command == "show":
                return release_cmd.candidate_show(target=args.target, candidate_id=args.candidate_id, json_output=args.json)
            if args.release_candidate_command == "archive":
                return release_cmd.candidate_archive(target=args.target, candidate_id=args.candidate_id, json_output=args.json)
            if args.release_candidate_command == "compare":
                return release_cmd.candidate_compare(target=args.target, candidate_id=args.candidate_id, json_output=args.json)
            if args.release_candidate_command == "closeout":
                return release_cmd.candidate_closeout(
                    target=args.target,
                    candidate_id=args.candidate_id,
                    status=args.status,
                    reason=args.reason,
                    json_output=args.json,
                )
            parser.error(f"unknown release candidate command: {args.release_candidate_command}")
            return 2
        parser.error(f"unknown release command: {args.release_command}")
        return 2
    if cmd == "roadmap":
        from . import roadmap_cmd

        if args.roadmap_command == "audit":
            return roadmap_cmd.audit(target=args.target, json_output=args.json, import_issues=args.import_issues)
        if args.roadmap_command == "patterns":
            return roadmap_cmd.patterns(target=args.target, json_output=args.json)
        if args.roadmap_command == "commands":
            return roadmap_cmd.commands(target=args.target, json_output=args.json)
        parser.error(f"unknown roadmap command: {args.roadmap_command}")
        return 2
    if cmd == "repos":
        from . import repos_cmd

        if args.repos_command == "init":
            return repos_cmd.init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
                json_output=args.json,
            )
        if args.repos_command == "list":
            return repos_cmd.list_repos(target=args.target, json_output=args.json)
        if args.repos_command == "show":
            return repos_cmd.show(target=args.target, repo_id=args.repo_id, json_output=args.json)
        if args.repos_command == "scan":
            return repos_cmd.scan(target=args.target, json_output=args.json)
        if args.repos_command == "doctor":
            return repos_cmd.doctor(target=args.target, json_output=args.json)
        if args.repos_command == "import-issues":
            return repos_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
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
                return repos_cmd.report_closeout(target=args.target, report_id=args.report_id, status=args.status, reason=args.reason, json_output=args.json)
            parser.error(f"unknown repos report command: {args.repos_report_command}")
            return 2
        if args.repos_command == "actions":
            if args.repos_actions_command == "plan":
                return repos_cmd.actions_plan(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.repos_actions_command == "build":
                return repos_cmd.actions_build(target=args.target, report_id=args.report_id, allow_unreviewed=args.allow_unreviewed, json_output=args.json)
            if args.repos_actions_command == "list":
                return repos_cmd.actions_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.repos_actions_command == "show":
                return repos_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.repos_actions_command == "start":
                return repos_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.repos_actions_command == "done":
                return repos_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.repos_actions_command == "defer":
                return repos_cmd.actions_defer(target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json)
            if args.repos_actions_command == "archive":
                return repos_cmd.actions_archive_completed(target=args.target, json_output=args.json)
            if args.repos_actions_command == "dispatch":
                dispatch_args = list(args.dispatch_args or [])
                dispatch_mode = "apply"
                action_id = None
                if dispatch_args and dispatch_args[0] in {"plan", "apply"}:
                    dispatch_mode = dispatch_args.pop(0)
                if dispatch_args:
                    action_id = dispatch_args.pop(0)
                if dispatch_args:
                    parser.error("too many repos actions dispatch arguments")
                if dispatch_mode == "plan":
                    return repos_cmd.actions_dispatch_plan(
                        target=args.target,
                        action_id=action_id,
                        all_reviewed=args.all_reviewed,
                        include_deferred=args.include_deferred,
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
                    return repos_cmd.actions_context_plan(target=args.target, action_id=args.action_id, json_output=args.json)
                return repos_cmd.actions_context_build(target=args.target, action_id=args.action_id, json_output=args.json)
            parser.error(f"unknown repos actions command: {args.repos_actions_command}")
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
                return repos_cmd.sweep_closeout(target=args.target, sweep_id=args.sweep_id, status=args.status, reason=args.reason, json_output=args.json)
            parser.error(f"unknown repos sweep command: {args.repos_sweep_command}")
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
                return repos_cmd.release_closeout(target=args.target, train_id=args.train_id, status=args.status, reason=args.reason, json_output=args.json)
            if args.repos_release_command == "archive":
                return repos_cmd.release_archive(target=args.target, train_id=args.train_id, json_output=args.json)
            if args.repos_release_command == "reconcile":
                return repos_cmd.release_reconcile(target=args.target, train_id=args.train_id, json_output=args.json)
            if args.repos_release_command == "summary":
                return repos_cmd.release_summary(target=args.target, train_id=args.train_id, json_output=args.json)
            if args.repos_release_command == "report":
                return repos_cmd.release_report(target=args.target, train_id=args.train_id, json_output=args.json)
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
                return repos_cmd.release_import_issues(target=args.target, train_id=args.train_id, dry_run=args.dry_run, json_output=args.json)
            if args.repos_release_command == "actions":
                if args.repos_release_actions_command == "plan":
                    return repos_cmd.release_actions_plan(target=args.target, train_id=args.train_id, json_output=args.json)
                if args.repos_release_actions_command == "build":
                    return repos_cmd.release_actions_build(target=args.target, train_id=args.train_id, allow_unreviewed=args.allow_unreviewed, json_output=args.json)
                if args.repos_release_actions_command == "list":
                    return repos_cmd.release_actions_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.repos_release_actions_command == "show":
                    return repos_cmd.release_actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.repos_release_actions_command == "start":
                    return repos_cmd.release_actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.repos_release_actions_command == "done":
                    return repos_cmd.release_actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
                if args.repos_release_actions_command == "defer":
                    return repos_cmd.release_actions_defer(target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json)
                if args.repos_release_actions_command == "archive":
                    return repos_cmd.release_actions_archive_completed(target=args.target, json_output=args.json)
                parser.error(f"unknown repos release actions command: {args.repos_release_actions_command}")
                return 2
            if args.repos_release_command == "evidence":
                if args.repos_release_evidence_command == "plan":
                    return repos_cmd.release_evidence_plan(target=args.target, train_id=args.train_id, json_output=args.json)
                if args.repos_release_evidence_command == "record":
                    return repos_cmd.release_evidence_record(target=args.target, train_id=args.train_id, repo_id=args.repo_id, step=args.step, status=args.status, summary=args.summary, json_output=args.json)
                if args.repos_release_evidence_command == "list":
                    return repos_cmd.release_evidence_list(target=args.target, train_id=args.train_id, limit=args.limit, json_output=args.json)
                if args.repos_release_evidence_command == "show":
                    return repos_cmd.release_evidence_show(target=args.target, evidence_id=args.evidence_id, json_output=args.json)
                parser.error(f"unknown repos release evidence command: {args.repos_release_evidence_command}")
                return 2
            if args.repos_release_command == "waivers":
                if args.repos_release_waivers_command == "record":
                    return repos_cmd.release_waiver_record(target=args.target, train_id=args.train_id, scope=args.scope, repo_id=args.repo_id, reason=args.reason, expires_at=args.expires_at, json_output=args.json)
                if args.repos_release_waivers_command == "list":
                    return repos_cmd.release_waiver_list(target=args.target, train_id=args.train_id, limit=args.limit, json_output=args.json)
                if args.repos_release_waivers_command == "show":
                    return repos_cmd.release_waiver_show(target=args.target, waiver_id=args.waiver_id, json_output=args.json)
                if args.repos_release_waivers_command == "revoke":
                    return repos_cmd.release_waiver_revoke(target=args.target, waiver_id=args.waiver_id, reason=args.reason, json_output=args.json)
                if args.repos_release_waivers_command == "renew":
                    return repos_cmd.release_waiver_renew(target=args.target, waiver_id=args.waiver_id, reason=args.reason, expires_at=args.expires_at, json_output=args.json)
                if args.repos_release_waivers_command == "doctor":
                    return repos_cmd.release_waiver_doctor(target=args.target, train_id=args.train_id, json_output=args.json)
                if args.repos_release_waivers_command == "import-issues":
                    return repos_cmd.release_waiver_import_issues(target=args.target, train_id=args.train_id, dry_run=args.dry_run, json_output=args.json)
                parser.error(f"unknown repos release waivers command: {args.repos_release_waivers_command}")
                return 2
            parser.error(f"unknown repos release command: {args.repos_release_command}")
            return 2
        parser.error(f"unknown repos command: {args.repos_command}")
        return 2
    if cmd == "handoff":
        from . import handoff_cmd

        if args.handoff_command == "doctor":
            return handoff_cmd.doctor(target=args.target, sources=args.sources, json_output=args.json)
        if args.handoff_command == "lint":
            return handoff_cmd.lint(target=args.target, paths=args.paths, json_output=args.json)
        if args.handoff_command == "list":
            return handoff_cmd.list_drafts(
                target=args.target,
                sources=args.sources,
                json_output=args.json,
                limit=args.limit,
            )
        if args.handoff_command == "show":
            return handoff_cmd.show_draft(
                target=args.target,
                draft_id=args.draft_id,
                sources=args.sources,
                json_output=args.json,
            )
        if args.handoff_command == "archive":
            return handoff_cmd.archive_draft(
                target=args.target,
                draft_id=args.draft_id,
                all_reviewed=args.all_reviewed,
                reason=args.reason,
                sources=args.sources,
                json_output=args.json,
            )
        if args.handoff_command == "closeout":
            return handoff_cmd.closeout(
                target=args.target,
                draft_id=args.draft_id,
                all_pending=args.all,
                reason=args.reason,
                defer=args.defer,
                sources=args.sources,
                json_output=args.json,
            )
        if args.handoff_command == "runs":
            return handoff_cmd.runs(target=args.target, json_output=args.json, limit=args.limit)
        if args.handoff_command == "run-show":
            return handoff_cmd.run_show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.handoff_command == "reconcile":
            return handoff_cmd.reconcile(target=args.target, sources=args.sources, json_output=args.json)
        if args.handoff_command == "issues":
            return handoff_cmd.issues(
                target=args.target,
                sources=args.sources,
                json_output=args.json,
                limit=args.limit,
                categories=args.category,
            )
        if args.handoff_command == "import-issues":
            return handoff_cmd.import_issues(
                target=args.target,
                sources=args.sources,
                dry_run=args.dry_run,
                json_output=args.json,
                categories=args.category,
            )
        if args.handoff_command == "sync-issues":
            return handoff_cmd.sync_issues(
                target=args.target,
                sources=args.sources,
                dry_run=args.dry_run,
                json_output=args.json,
                categories=args.category,
                close_stale=not args.no_close_stale,
            )
        parser.error(f"unknown handoff command: {args.handoff_command}")
        return 2
    if cmd == "chat":
        from . import chat_cmd

        if args.chat_command == "surfaces":
            if args.surfaces_command == "init":
                return chat_cmd.surfaces_init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.surfaces_command == "list":
                return chat_cmd.surfaces_list(target=args.target, json_output=args.json)
            if args.surfaces_command == "show":
                return chat_cmd.surfaces_show(target=args.target, surface_id=args.surface_id, json_output=args.json)
            if args.surfaces_command == "doctor":
                return chat_cmd.surfaces_doctor(target=args.target, json_output=args.json)
            parser.error(f"unknown chat surfaces command: {args.surfaces_command}")
            return 2
        if args.chat_command == "sweep":
            if args.sweep_command == "validate":
                return chat_cmd.sweep_validate(target=args.target, input_path=args.input_path, json_output=args.json)
            if args.sweep_command == "ingest":
                return chat_cmd.sweep_ingest(target=args.target, surface_id=args.surface_id, json_output=args.json)
            if args.sweep_command == "import-issues":
                return chat_cmd.sweep_import_issues(target=args.target, surface_id=args.surface_id, json_output=args.json)
            parser.error(f"unknown chat sweep command: {args.sweep_command}")
            return 2
        parser.error(f"unknown chat command: {args.chat_command}")
        return 2
    if cmd == "context":
        from . import context_cmd

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
        from . import projects_cmd

        if args.projects_command == "audit":
            return projects_cmd.audit(target=args.target, json_output=args.json)
        if args.projects_command == "import-issues":
            return projects_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.projects_command == "closeout":
            return projects_cmd.closeout(target=args.target, status=args.status, reason=args.reason, project_id=args.project_id, json_output=args.json)
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
                return projects_cmd.readiness_show(target=args.target, readiness_id=args.readiness_id, json_output=args.json)
            parser.error(f"unknown projects readiness command: {args.projects_readiness_command}")
            return 2
        parser.error(f"unknown projects command: {args.projects_command}")
        return 2
    if cmd == "learn":
        from . import learn_cmd

        if args.learn_command == "plan":
            return learn_cmd.plan(target=args.target, json_output=args.json)
        if args.learn_command == "doctor":
            return learn_cmd.doctor(target=args.target, json_output=args.json)
        if args.learn_command == "import-issues":
            return learn_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.learn_command == "closeout":
            return learn_cmd.closeout(target=args.target, candidate_id=args.candidate_id, subsystem=args.subsystem, status=args.status, reason=args.reason, json_output=args.json)
        if args.learn_command == "closeouts":
            return learn_cmd.closeouts(target=args.target, json_output=args.json)
        if args.learn_command == "closeout-show":
            return learn_cmd.closeout_show(target=args.target, closeout_id=args.closeout_id, json_output=args.json)
        if args.learn_command == "replay":
            if args.learn_replay_command == "export":
                return learn_cmd.replay_export(target=args.target, scenario_id=args.scenario_id, before_summary=args.before_summary, after_summary=args.after_summary, before_count=args.before_count, after_count=args.after_count, json_output=args.json)
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
    if cmd == "center":
        from . import center_cmd

        if args.center_command == "status":
            return center_cmd.status(target=args.target, json_output=args.json)
        if args.center_command == "activity":
            return center_cmd.activity(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_command == "reviews":
            return center_cmd.reviews(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_command == "templates":
            return center_cmd.templates(target=args.target, json_output=args.json)
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
            if args.center_actions_command == "start":
                return center_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.center_actions_command == "done":
                return center_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.center_actions_command == "defer":
                return center_cmd.actions_defer(target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json)
            if args.center_actions_command == "archive":
                return center_cmd.actions_archive_completed(target=args.target, json_output=args.json)
            parser.error(f"unknown center actions command: {args.center_actions_command}")
            return 2
        parser.error(f"unknown center command: {args.center_command}")
        return 2
    if cmd == "memory":
        from . import memory_cmd

        if args.memory_command == "care":
            if args.memory_care_command == "init":
                return memory_cmd.init(
                    target=args.target,
                    force=args.force,
                    update_gitignore=not args.no_gitignore,
                )
            if args.memory_care_command == "scan":
                return memory_cmd.scan(target=args.target, json_output=args.json)
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
        from . import work_cmd

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
                return work_cmd.review_finding_show(target=args.target, finding_id=args.finding_id, json_output=args.json)
            if args.review_command == "closeout":
                return work_cmd.review_closeout(target=args.target, run_id=args.run_id, json_output=args.json)
            parser.error(f"unknown review command: {args.review_command}")
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
                return work_cmd.task_plan(target=args.target, task_id=args.task_id, json_output=args.json)
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
        from . import aboyeur as aboyeur_mod
        from . import roster as roster_mod

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
                from . import runs_cmd

                runs_cmd.show(output_dir)
        return rc
    if cmd == "roster":
        from . import roster_cmd

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
        from . import runs_cmd

        if args.runs_command == "list":
            return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit)
        if args.runs_command == "latest":
            return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir)
        if args.runs_command == "show":
            return runs_cmd.show(args.run_dir)
        parser.error(f"unknown runs command: {args.runs_command}")
        return 2
    if cmd == "scrub":
        from . import scrub as scrub_mod

        return scrub_mod.run(target=args.target, policy=args.policy, dry_run=args.dry_run)
    if cmd == "security":
        from . import security_cmd

        if args.security_command == "init":
            return security_cmd.init(target=args.target, force=args.force)
        if args.security_command == "config":
            return security_cmd.show_config(target=args.target, json_output=args.json)
        if args.security_command == "doctor":
            return security_cmd.doctor(target=args.target, json_output=args.json)
        if args.security_command == "fix":
            return security_cmd.fix(target=args.target, dry_run=args.dry_run)
        if args.security_command == "review":
            return security_cmd.review(target=args.target, output_dir=args.output_dir, json_output=args.json)
        if args.security_command == "findings":
            return security_cmd.findings(target=args.target, output_dir=args.output_dir, json_output=args.json)
        if args.security_command == "sarif":
            return security_cmd.sarif(target=args.target, output_dir=args.output_dir, output_path=args.output_path, json_output=args.json)
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
        from . import tools_cmd

        if args.tools_command == "init":
            return tools_cmd.init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
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
                return tools_cmd.call_reject(target=args.target, call_id=args.call_id, reason=args.reason, json_output=args.json)
            if args.tools_call_command == "hold":
                return tools_cmd.call_hold(target=args.target, call_id=args.call_id, reason=args.reason, json_output=args.json)
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
                return tools_cmd.checkpoint_show(target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json)
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
                return tools_cmd.checkpoint_resume(target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json)
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
                return tools_cmd.parity_closeout(target=args.target, reason=args.reason, defer=args.defer, json_output=args.json)
            parser.error(f"unknown tools parity command: {args.tools_parity_command}")
            return 2
        if args.tools_command == "pack":
            if args.tools_pack_command == "build":
                return tools_cmd.pack_build(target=args.target, json_output=args.json)
            if args.tools_pack_command == "list":
                return tools_cmd.pack_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.tools_pack_command == "show":
                return tools_cmd.pack_show(target=args.target, pack_id=args.pack_id, json_output=args.json)
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
        from . import handoff as handoff_mod

        return handoff_mod.run(target=args.target)
    if cmd == "ingest":
        from . import ingest as ingest_mod

        return ingest_mod.run(
            target=args.target,
            dry_run=args.dry_run,
            promote_cards=args.promote_cards,
            route_documents=args.route_documents,
        )
    if cmd == "openclaw-fragments":
        from . import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="openclaw")
    if cmd == "hermes-fragments":
        from . import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="hermes")
    if cmd == "reconfigure":
        from .config import load_config
        from .reconfigure import reconfigure as _reconfigure
        from .selection import Selection, KNOWN_HARNESSES, resolve_owner

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
