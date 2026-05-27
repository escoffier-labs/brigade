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
    p_work_task_add.add_argument("--type", choices=TASK_TYPES, default="task", help="Task type.")
    p_work_task_add.add_argument("--priority", choices=TASK_PRIORITIES, default="normal", help="Task priority.")
    p_work_task_add.add_argument(
        "--acceptance",
        action="append",
        default=[],
        help="Acceptance criterion. Repeat for multiple criteria.",
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
    p_security_fix = security_sub.add_parser("fix", help="Apply safe local security hygiene fixes.")
    p_security_fix.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_fix.add_argument("--dry-run", action="store_true", help="Show changes without writing files.")
    p_security_review = security_sub.add_parser("review", help="Review the latest local security evidence bundle.")
    p_security_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_review.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_security_suppress = security_sub.add_parser("suppress", help="Suppress a reviewed security finding fingerprint.")
    p_security_suppress.add_argument("fingerprint", help="Finding fingerprint to suppress.")
    p_security_suppress.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_suppress.add_argument("--reason", required=True, help="Required suppression reason.")
    p_security_unsuppress = security_sub.add_parser("unsuppress", help="Remove a security finding suppression.")
    p_security_unsuppress.add_argument("fingerprint", help="Finding fingerprint to unsuppress.")
    p_security_unsuppress.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
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
        choices=["personal", "public-repo", "strict"],
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
    if cmd == "handoff":
        from . import handoff_cmd

        if args.handoff_command == "doctor":
            return handoff_cmd.doctor(target=args.target, sources=args.sources, json_output=args.json)
        if args.handoff_command == "lint":
            return handoff_cmd.lint(target=args.target, paths=args.paths, json_output=args.json)
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
                    task_type=args.type,
                    priority=args.priority,
                    acceptance=args.acceptance,
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
            if args.import_command == "memory-care":
                return work_cmd.import_memory_care(
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
        if args.security_command == "fix":
            return security_cmd.fix(target=args.target, dry_run=args.dry_run)
        if args.security_command == "review":
            return security_cmd.review(target=args.target, output_dir=args.output_dir, json_output=args.json)
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
