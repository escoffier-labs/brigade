"""brigade command-line entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from .work_cmd import TASK_PRIORITIES, TASK_TYPES
from .prompt import prompt_for_selection  # imported here so tests can monkeypatch cli.prompt_for_selection


# Top-level help groups. Every sub.add_parser command must appear in exactly
# one group; tests/test_cli_help.py enforces full coverage.
COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("Core memory loop",
     ["init", "handoff", "handoff-template", "ingest", "memory", "doctor", "status"]),
    ("Daily operator loop",
     ["operator", "daily", "work", "center", "runbook", "budgets", "notifications"]),
    ("Stations and tools",
     ["add", "skills", "tools", "pantry", "roster", "run", "runs", "dogfood"]),
    ("Review, security, and research",
     ["security", "scrub", "untrusted", "research", "learn", "chat", "context", "projects"]),
    ("Wiring and advanced",
     ["release", "roadmap", "repos", "reconfigure", "openclaw-fragments", "hermes-fragments"]),
]

_START_HERE = """Brigade: run your agent brigade. Operator-system CLI for agent workspaces.

Start here:
  brigade operator quickstart --target <repo> --harnesses codex   set up a repo or workspace
  brigade operator doctor --target <repo>                         check the wiring
  brigade handoff draft --target <repo> ...                       record a durable note
  brigade ingest --target <workspace>                             route handoffs into memory"""


class _TopLevelHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Suppress argparse's flat subcommand dump on the top-level parser.

    The grouped epilog lists every command instead. Subparsers do not inherit
    this formatter, so `brigade <command> --help` keeps the normal listing.
    """

    def _iter_indented_subactions(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return iter(())
        return super()._iter_indented_subactions(action)


def _grouped_epilog(sub: argparse._SubParsersAction) -> str:
    helps = {action.dest: (action.help or "") for action in sub._choices_actions}
    lines = ["commands:"]
    for title, names in COMMAND_GROUPS:
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(f"  {name:<22}{helps.get(name, '')}" for name in names)
    lines.append("")
    lines.append("Run 'brigade <command> --help' for details on any command.")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    from . import learn_cmd, projects_cmd, release_cmd, repos_cmd

    parser = argparse.ArgumentParser(
        prog="brigade",
        description=_START_HERE,
        formatter_class=_TopLevelHelpFormatter,
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
        help="Comma-separated harness ids: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, openclaw, hermes. "
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

    # daily
    p_daily = sub.add_parser("daily", help="Run the personal daily operator loop.")
    daily_sub = p_daily.add_subparsers(dest="daily_command", metavar="<daily-command>")
    daily_sub.required = True
    for name in ("status", "review", "schema", "doctor"):
        p_daily_action = daily_sub.add_parser(name, help=f"Show daily {name}.")
        p_daily_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_daily_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_init = daily_sub.add_parser("init", help="Write local daily driver defaults.")
    p_daily_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_init.add_argument("--force", action="store_true", help="Overwrite an existing daily config.")
    p_daily_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_plan = daily_sub.add_parser("plan", help="Create the ranked daily plan.")
    p_daily_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_plan.add_argument("--record", action="store_true", help="Write a local daily plan receipt.")
    p_daily_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_run = daily_sub.add_parser("run", help="Run one bounded safe daily action.")
    p_daily_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_run.add_argument("--approved", action="store_true", help="Allow the selected action when it requires explicit approval.")
    p_daily_run.add_argument("--approval", default=None, help="Run using an approved daily approval request.")
    p_daily_run.add_argument("--plan-id", default=None, help="Run from a recorded daily plan id or latest.")
    p_daily_run.add_argument("--replan", action="store_true", help="Ignore a stale or supplied plan and choose a fresh action.")
    p_daily_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name, help_text in (
        ("resume", "Resume or explain recovery for the latest daily run."),
        ("repair", "Inspect daily driver state and write local repair metadata."),
        ("unblock", "Create local unblock metadata, imports, or approval requests."),
        ("protocol", "Print the wrapper-facing daily agent protocol."),
    ):
        p_daily_extra = daily_sub.add_parser(name, help=help_text)
        p_daily_extra.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        if name == "unblock":
            p_daily_extra.add_argument("--dry-run", action="store_true", help="Preview unblock writes.")
        p_daily_extra.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_telemetry = daily_sub.add_parser("telemetry", help="Summarize local daily driver telemetry.")
    p_daily_telemetry.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_telemetry.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    telemetry_sub = p_daily_telemetry.add_subparsers(dest="daily_telemetry_command", metavar="<telemetry-command>")
    p_daily_telemetry_doctor = telemetry_sub.add_parser("doctor", help="Check daily telemetry health.")
    p_daily_telemetry_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_telemetry_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening = daily_sub.add_parser("hardening", help="Plan and audit daily production hardening.")
    hardening_sub = p_daily_hardening.add_subparsers(dest="daily_hardening_command", metavar="<hardening-command>")
    hardening_sub.required = True
    for name in ("plan", "audit"):
        p_daily_hardening_action = hardening_sub.add_parser(name, help=f"Run daily hardening {name}.")
        p_daily_hardening_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_daily_hardening_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening_import = hardening_sub.add_parser("import-issues", help="Route hardening findings into the work inbox.")
    p_daily_hardening_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_hardening_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing.")
    p_daily_hardening_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening_closeout = hardening_sub.add_parser("closeout", help="Write a local hardening closeout receipt.")
    p_daily_hardening_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_hardening_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed")
    p_daily_hardening_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_daily_hardening_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals = daily_sub.add_parser("approvals", help="Review daily approval requests.")
    approvals_sub = p_daily_approvals.add_subparsers(dest="daily_approval_command", metavar="<approval-command>")
    approvals_sub.required = True
    p_daily_approvals_list = approvals_sub.add_parser("list", help="List daily approval requests.")
    p_daily_approvals_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_approvals_list.add_argument("--limit", type=int, default=50, help="Maximum approvals to show.")
    p_daily_approvals_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_show = approvals_sub.add_parser("show", help="Show a daily approval request.")
    p_daily_approvals_show.add_argument("approval_id")
    p_daily_approvals_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_approvals_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("approve", "reject", "hold"):
        p_daily_approval_review = approvals_sub.add_parser(name, help=f"{name.title()} a daily approval request.")
        p_daily_approval_review.add_argument("approval_id")
        p_daily_approval_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
        if name in {"reject", "hold"}:
            p_daily_approval_review.add_argument("--reason", required=True, help="Review reason.")
        else:
            p_daily_approval_review.add_argument("--reason", default=None, help="Optional review reason.")
        p_daily_approval_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_compare = approvals_sub.add_parser("compare", help="Compare a daily approval request with current evidence.")
    p_daily_approvals_compare.add_argument("approval_id")
    p_daily_approvals_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_approvals_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_archive = approvals_sub.add_parser("archive", help="Archive closed daily approval requests.")
    p_daily_approvals_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_approvals_archive.add_argument("--consumed", action="store_true", help="Archive consumed, rejected, or superseded approvals.")
    p_daily_approvals_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_history = daily_sub.add_parser("history", help="List local daily receipts.")
    p_daily_history.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_history.add_argument("--limit", type=int, default=20, help="Maximum receipts to show.")
    p_daily_history.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_show = daily_sub.add_parser("show", help="Show a daily run receipt.")
    p_daily_show.add_argument("run_id", nargs="?", default="latest", help="Run id or latest.")
    p_daily_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_closeout = daily_sub.add_parser("closeout", help="Close out the latest daily run.")
    p_daily_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed")
    p_daily_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_daily_closeout.add_argument("--handoff", action="store_true", help="Write and lint a Memory Handoff draft.")
    p_daily_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # add
    p_add = sub.add_parser("add", help="Install and wire a station's managed tools.")
    p_add.add_argument("station", help="Station to add tools for (e.g. memory, guard, tokens).")
    p_add.add_argument("--target", "-t", type=Path, default=Path("."))

    # pantry
    p_pantry = sub.add_parser("pantry", help="Inspect and plan agentpantry session-auth sync.")
    pantry_sub = p_pantry.add_subparsers(dest="pantry_command", metavar="<pantry-command>")
    pantry_sub.required = True
    p_pantry_status = pantry_sub.add_parser("status", help="Show agentpantry status and advisory health.")
    p_pantry_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_pantry_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry_setup = pantry_sub.add_parser("setup", help="Plan source or sink setup without applying it.")
    pantry_setup_sub = p_pantry_setup.add_subparsers(dest="pantry_setup_command", metavar="<setup-command>")
    pantry_setup_sub.required = True
    p_pantry_setup_plan = pantry_setup_sub.add_parser("plan", help="Plan agentpantry source or sink setup.")
    p_pantry_setup_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update when --write is used.")
    p_pantry_setup_plan.add_argument("--role", choices=["source", "sink"], required=True, help="agentpantry role to plan.")
    p_pantry_setup_plan.add_argument("--peer", default="127.0.0.1:8787", help="Peer/bind address to document in the plan.")
    p_pantry_setup_plan.add_argument("--config-path", default="~/.config/agentpantry/config.toml", help="agentpantry config path to document.")
    p_pantry_setup_plan.add_argument("--key-path", default="~/.config/agentpantry/psk.key", help="agentpantry PSK path to document.")
    p_pantry_setup_plan.add_argument("--write", action="store_true", help="Write a local reviewed plan under .brigade/pantry/plans/.")
    p_pantry_setup_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry_service = pantry_sub.add_parser("service", help="Plan service setup without starting services.")
    pantry_service_sub = p_pantry_service.add_subparsers(dest="pantry_service_command", metavar="<service-command>")
    pantry_service_sub.required = True
    p_pantry_service_plan = pantry_service_sub.add_parser("plan", help="Plan agentpantry service installation.")
    p_pantry_service_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update when --write is used.")
    p_pantry_service_plan.add_argument("--role", choices=["source", "sink"], required=True, help="agentpantry role to document.")
    p_pantry_service_plan.add_argument("--config-path", default="~/.config/agentpantry/config.toml", help="agentpantry config path to document.")
    p_pantry_service_plan.add_argument("--write", action="store_true", help="Write a local reviewed plan under .brigade/pantry/plans/.")
    p_pantry_service_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # notifications
    p_notifications = sub.add_parser("notifications", help="Inspect and plan operator notification wiring.")
    notifications_sub = p_notifications.add_subparsers(dest="notifications_command", metavar="<notifications-command>")
    notifications_sub.required = True
    p_notifications_status = notifications_sub.add_parser("status", help="Show agent-notify status.")
    p_notifications_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_notifications_status.add_argument("--profile", default=None, help="agent-notify profile to inspect.")
    p_notifications_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_notifications_setup = notifications_sub.add_parser("setup", help="Plan notification setup without applying it.")
    notifications_setup_sub = p_notifications_setup.add_subparsers(dest="notifications_setup_command", metavar="<setup-command>")
    notifications_setup_sub.required = True
    p_notifications_setup_plan = notifications_setup_sub.add_parser("plan", help="Print reviewed hook snippets and setup commands.")
    p_notifications_setup_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_notifications_setup_plan.add_argument("--profile", default="operator", help="agent-notify profile to use in snippets.")
    p_notifications_setup_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_notifications_event = notifications_sub.add_parser("event", help="Plan or record explicit operator notification events.")
    notifications_event_sub = p_notifications_event.add_subparsers(dest="notifications_event_command", metavar="<event-command>")
    notifications_event_sub.required = True
    notification_event_types = ("ci-green", "ci-failed", "handoff-waiting", "handoff-ingested", "release-ready", "operator-alert")
    for event_command in ("plan", "record"):
        p_event = notifications_event_sub.add_parser(event_command, help=f"{event_command.title()} an explicit notification event.")
        p_event.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_event.add_argument("--type", choices=notification_event_types, required=True, help="Notification event type.")
        p_event.add_argument("--title", required=True, help="Safe notification title.")
        p_event.add_argument("--message", required=True, help="Safe notification message.")
        p_event.add_argument("--level", choices=["info", "success", "warning", "error"], default="info", help="Safe event level.")
        p_event.add_argument("--profile", default=None, help="agent-notify profile to use.")
        p_event.add_argument("--source", default=None, help="Safe source label, such as ci or handoff.")
        if event_command == "record":
            p_event.add_argument("--send", action="store_true", help="Also invoke agent-notify explicitly.")
        p_event.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # budgets
    p_budgets = sub.add_parser("budgets", help="Inspect Brigade's canonical operator budgets.")
    budgets_sub = p_budgets.add_subparsers(dest="budgets_command", metavar="<budgets-command>")
    budgets_sub.required = True
    p_budgets_show = budgets_sub.add_parser("show", help="Show canonical size and staleness budgets.")
    p_budgets_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_budgets_check = budgets_sub.add_parser("check", help="Check local bootstrap files against budgets.")
    p_budgets_check.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_budgets_check.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # untrusted
    p_untrusted = sub.add_parser("untrusted", help="Wrap and scan untrusted context.")
    untrusted_sub = p_untrusted.add_subparsers(dest="untrusted_command", metavar="<untrusted-command>")
    untrusted_sub.required = True
    p_untrusted_scan = untrusted_sub.add_parser("scan", help="Scan text for prompt-injection-style instructions.")
    p_untrusted_scan.add_argument("text", nargs="*", help="Text to scan.")
    p_untrusted_scan.add_argument("--from-file", type=Path, default=None, help="Read text from a file.")
    p_untrusted_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_untrusted_wrap = untrusted_sub.add_parser("wrap", help="Frame external content as untrusted data.")
    p_untrusted_wrap.add_argument("text", nargs="*", help="Text to wrap.")
    p_untrusted_wrap.add_argument("--from-file", type=Path, default=None, help="Read text from a file.")
    p_untrusted_wrap.add_argument("--source-kind", choices=["web", "tool-output", "retrieved-doc", "memory", "skill", "handoff"], required=True)
    p_untrusted_wrap.add_argument("--goal", default=None, help="Trusted goal to include outside the untrusted block.")
    p_untrusted_wrap.add_argument("--max-chars", type=int, default=None, help="Explicitly truncate content before wrapping.")
    p_untrusted_wrap.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # skills
    p_skills = sub.add_parser("skills", help="Manage reviewed cross-harness skill packs.")
    skills_sub = p_skills.add_subparsers(dest="skills_command", metavar="<skills-command>")
    skills_sub.required = True
    p_skills_search = skills_sub.add_parser("search", help="Search the local skill registry.")
    p_skills_search.add_argument("query", help="Search query.")
    p_skills_search.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_import = skills_sub.add_parser("import", help="Import a skill pack into the local registry.")
    p_skills_import.add_argument("source", type=Path, help="SKILL.md file or directory containing SKILL.md.")
    p_skills_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to update.")
    p_skills_import.add_argument("--id", dest="skill_id", default=None, help="Override imported skill id.")
    p_skills_import.add_argument("--force", action="store_true", help="Overwrite an existing registry entry.")
    p_skills_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_lint = skills_sub.add_parser("lint", help="Lint a registry skill or skill directory.")
    p_skills_lint.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_lint.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_lint.add_argument("--harness", default=None, help="Optional harness adapter to validate rendered output.")
    p_skills_lint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_doctor = skills_sub.add_parser("doctor", help="Check reviewed skill registry health.")
    p_skills_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_import_issues = skills_sub.add_parser("import-issues", help="Import skill registry issues into the work inbox.")
    p_skills_import_issues.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_import_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_install = skills_sub.add_parser("install", help="Install a reviewed skill into one or all harnesses.")
    p_skills_install.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_install.add_argument("--workspace", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_install.add_argument("--target", dest="install_target", required=True, help="Harness target or all.")
    p_skills_install.add_argument("--force", action="store_true", help="Overwrite an existing installed skill.")
    p_skills_install.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_compat = skills_sub.add_parser("compatibility", help="Show skill compatibility across harness adapters.")
    p_skills_compat.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_compat.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_compat.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_history = skills_sub.add_parser("history", help="List reviewed skill install history.")
    p_skills_history.add_argument("skill", nargs="?", default=None, help="Optional skill id.")
    p_skills_history.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_history.add_argument("--harness", default=None, help="Optional harness target filter.")
    p_skills_history.add_argument("--limit", type=int, default=20, help="Maximum history rows to show.")
    p_skills_history.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_diff = skills_sub.add_parser("diff", help="Diff an installed skill against the current rendered registry version.")
    p_skills_diff.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_diff.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_diff.add_argument("--harness", required=True, help="Harness target to compare.")
    p_skills_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_rollback = skills_sub.add_parser("rollback", help="Rollback one installed skill target to the latest snapshot.")
    p_skills_rollback.add_argument("skill", help="Skill id.")
    p_skills_rollback.add_argument("--workspace", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_rollback.add_argument("--target", dest="install_target", required=True, help="Harness target to rollback.")
    p_skills_rollback.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_serve = skills_sub.add_parser("serve-mcp", help="Report the local MCP skills resource contract.")
    p_skills_serve.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_serve.add_argument("--stdio", action="store_true", help="Serve the read-only skills MCP adapter over stdio.")
    p_skills_serve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_publish = skills_sub.add_parser("publish", help="Create a reviewed skill publish proposal.")
    p_skills_publish.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_publish.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_publish.add_argument("--scope", choices=["local", "workspace", "team", "public"], required=True)
    p_skills_publish.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack = skills_sub.add_parser("pack", help="Build, import, and inspect local portable skill packs.")
    skills_pack_sub = p_skills_pack.add_subparsers(dest="skills_pack_command", metavar="<skills-pack-command>")
    skills_pack_sub.required = True
    p_skills_pack_build = skills_pack_sub.add_parser("build", help="Build a local portable skill pack.")
    p_skills_pack_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to pack.")
    p_skills_pack_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_list = skills_pack_sub.add_parser("list", help="List local portable skill packs.")
    p_skills_pack_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_pack_list.add_argument("--limit", type=int, default=20, help="Maximum packs to show.")
    p_skills_pack_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_show = skills_pack_sub.add_parser("show", help="Show one local portable skill pack.")
    p_skills_pack_show.add_argument("pack_id", help="Pack id, id prefix, latest, or path.")
    p_skills_pack_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_pack_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_import = skills_pack_sub.add_parser("import", help="Import skills from a portable skill pack.")
    p_skills_pack_import.add_argument("pack", type=Path, help="Skill pack directory containing skill-pack.json.")
    p_skills_pack_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to update.")
    p_skills_pack_import.add_argument("--force", action="store_true", help="Overwrite existing registry skills.")
    p_skills_pack_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_archive = skills_pack_sub.add_parser("archive", help="Archive one local portable skill pack.")
    p_skills_pack_archive.add_argument("pack_id", help="Pack id, id prefix, or latest.")
    p_skills_pack_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to update.")
    p_skills_pack_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox = skills_sub.add_parser("inbox", help="Review agent-proposed skill packs before import.")
    skills_inbox_sub = p_skills_inbox.add_subparsers(dest="skills_inbox_command", metavar="<skills-inbox-command>")
    skills_inbox_sub.required = True
    p_skills_inbox_add = skills_inbox_sub.add_parser("add", help="Add a proposed skill pack to the review inbox.")
    p_skills_inbox_add.add_argument("source", type=Path, help="SKILL.md file or directory containing SKILL.md.")
    p_skills_inbox_add.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to update.")
    p_skills_inbox_add.add_argument("--id", dest="skill_id", default=None, help="Override proposed skill id.")
    p_skills_inbox_add.add_argument("--summary", default=None, help="Short review summary.")
    p_skills_inbox_add.add_argument("--force", action="store_true", help="Overwrite an existing generated proposal id.")
    p_skills_inbox_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_list = skills_inbox_sub.add_parser("list", help="List pending and reviewed skill proposals.")
    p_skills_inbox_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to inspect.")
    p_skills_inbox_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_show = skills_inbox_sub.add_parser("show", help="Show one skill proposal.")
    p_skills_inbox_show.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to inspect.")
    p_skills_inbox_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_diff = skills_inbox_sub.add_parser("diff", help="Diff one skill proposal against the current registry entry.")
    p_skills_inbox_diff.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_diff.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to inspect.")
    p_skills_inbox_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_accept = skills_inbox_sub.add_parser("accept", help="Accept one skill proposal into the registry.")
    p_skills_inbox_accept.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_accept.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to update.")
    p_skills_inbox_accept.add_argument("--force", action="store_true", help="Overwrite an existing registry skill.")
    p_skills_inbox_accept.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_reject = skills_inbox_sub.add_parser("reject", help="Reject one skill proposal.")
    p_skills_inbox_reject.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_reject.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to update.")
    p_skills_inbox_reject.add_argument("--reason", required=True, help="Review reason.")
    p_skills_inbox_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters = skills_sub.add_parser("adapters", help="Inspect skill harness adapters.")
    skills_adapters_sub = p_skills_adapters.add_subparsers(dest="skills_adapters_command", metavar="<skills-adapters-command>")
    skills_adapters_sub.required = True
    p_skills_adapters_init = skills_adapters_sub.add_parser("init", help="Write local skill adapter overlay config.")
    p_skills_adapters_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_adapters_init.add_argument("--force", action="store_true", help="Overwrite an existing adapter config.")
    p_skills_adapters_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters_list = skills_adapters_sub.add_parser("list", help="List skill harness adapters.")
    p_skills_adapters_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to inspect.")
    p_skills_adapters_list.add_argument("--include-planned", action="store_true", help="Include planned future adapter targets.")
    p_skills_adapters_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters_show = skills_adapters_sub.add_parser("show", help="Show one skill harness adapter.")
    p_skills_adapters_show.add_argument("adapter_id", help="Adapter id.")
    p_skills_adapters_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to inspect.")
    p_skills_adapters_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # operator
    p_operator = sub.add_parser("operator", help="Plan and initialize safe local operator config.")
    operator_sub = p_operator.add_subparsers(dest="operator_command", metavar="<operator-command>")
    operator_sub.required = True
    p_operator_guide = operator_sub.add_parser("guide", help="Print the repo-local Brigade operator workflow.")
    p_operator_guide.add_argument("--profile", choices=["local-operator", "internal-dogfood"], default="internal-dogfood", help="Operator profile to describe.")
    p_operator_guide.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_plan = operator_sub.add_parser("plan", help="Plan local operator config bootstrap.")
    p_operator_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_plan.add_argument("--profile", choices=["local-operator", "internal-dogfood"], default="local-operator", help="Bootstrap profile to inspect.")
    p_operator_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt = operator_sub.add_parser("adopt", help="Inspect an existing operator workspace before Brigade adoption.")
    operator_adopt_sub = p_operator_adopt.add_subparsers(dest="operator_adopt_command", metavar="<operator-adopt-command>")
    operator_adopt_sub.required = True
    p_operator_adopt_plan = operator_adopt_sub.add_parser("plan", help="Build a privacy-preserving read-only adoption plan.")
    p_operator_adopt_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_adopt_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt_capture = operator_adopt_sub.add_parser("capture", help="Write a redacted local adoption snapshot.")
    p_operator_adopt_capture.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_adopt_capture.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt_import = operator_adopt_sub.add_parser("import-issues", help="Import adoption gaps into the work inbox.")
    p_operator_adopt_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_adopt_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_adopt_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration = operator_sub.add_parser("migration", help="Summarize operator adoption and external-surface replacement progress.")
    operator_migration_sub = p_operator_migration.add_subparsers(dest="operator_migration_command", metavar="<operator-migration-command>")
    operator_migration_sub.required = True
    p_operator_migration_status = operator_migration_sub.add_parser("status", help="Show redacted operator migration progress.")
    p_operator_migration_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_migration_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_doctor = operator_migration_sub.add_parser("doctor", help="Check whether Brigade can drive operator migration work.")
    p_operator_migration_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_migration_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_import = operator_migration_sub.add_parser("import-issues", help="Import operator migration rollup gaps into the work inbox.")
    p_operator_migration_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_migration_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_migration_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_consolidate = operator_migration_sub.add_parser("consolidate", help="Dismiss tiny operator-surface-review imports superseded by a migration rollup.")
    p_operator_migration_consolidate.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_migration_consolidate.add_argument("--surface", default=None, help="Optional surface id to consolidate.")
    p_operator_migration_consolidate.add_argument("--review-status", default=None, help="Optional review status to consolidate.")
    p_operator_migration_consolidate.add_argument("--reason", default="superseded-by-migration-rollup", help="Short safe dismissal reason.")
    p_operator_migration_consolidate.add_argument("--dry-run", action="store_true", help="Report without dismissing imports.")
    p_operator_migration_consolidate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces = operator_sub.add_parser("surfaces", help="Capture and review redacted external scheduler/process surfaces.")
    operator_surfaces_sub = p_operator_surfaces.add_subparsers(dest="operator_surfaces_command", metavar="<operator-surfaces-command>")
    operator_surfaces_sub.required = True
    p_operator_surfaces_capture = operator_surfaces_sub.add_parser("capture", help="Write a redacted local operator surface snapshot.")
    p_operator_surfaces_capture.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_surfaces_capture.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_list = operator_surfaces_sub.add_parser("list", help="List the latest redacted operator surface records.")
    p_operator_surfaces_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_surfaces_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_doctor = operator_surfaces_sub.add_parser("doctor", help="Check redacted operator surface capture freshness.")
    p_operator_surfaces_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_surfaces_doctor.add_argument("--surface", default=None, help="Optional surface id to check, such as shell_crontab.")
    p_operator_surfaces_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_review = operator_surfaces_sub.add_parser("review", help="Record a redacted operator surface ownership decision.")
    p_operator_surfaces_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_surfaces_review.add_argument("--surface", required=True, help="Surface id to review, such as shell_crontab.")
    p_operator_surfaces_review.add_argument("--status", required=True, choices=["brigade-runbook-candidate", "external-ok", "needs-owner", "retire-candidate"], help="Review decision.")
    p_operator_surfaces_review.add_argument("--all", dest="all_records", action="store_true", help="Review every current record for the surface.")
    p_operator_surfaces_review.add_argument("--record", dest="record_labels", action="append", default=[], help="Review one redacted record label. Repeat for multiple records.")
    p_operator_surfaces_review.add_argument("--reason", default="operator-review", help="Short safe reason code. Do not include paths or secrets.")
    p_operator_surfaces_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_reviews = operator_surfaces_sub.add_parser("reviews", help="Summarize redacted operator surface review state.")
    p_operator_surfaces_reviews.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_surfaces_reviews.add_argument("--surface", default=None, help="Optional surface id to list.")
    p_operator_surfaces_reviews.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_import = operator_surfaces_sub.add_parser("import-issues", help="Import surface coverage follow-ups into the work inbox.")
    p_operator_surfaces_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_surfaces_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_surfaces_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_init = operator_sub.add_parser("init", help="Write missing gitignored local operator config defaults.")
    p_operator_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_init.add_argument("--profile", choices=["local-operator", "internal-dogfood"], default="local-operator", help="Bootstrap profile to apply.")
    p_operator_init.add_argument("--force", action="store_true", help="Overwrite existing local config files.")
    p_operator_init.add_argument("--dry-run", action="store_true", help="Show planned writes without changing files.")
    p_operator_init.add_argument("--waive-public-release", action="store_true", help="Write a local waiver for public release readiness when using an internal profile.")
    p_operator_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_status = operator_sub.add_parser("status", help="Show repo and machine wiring for local operator use.")
    p_operator_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_status.add_argument("--profile", choices=["local-operator", "internal-dogfood"], default="internal-dogfood", help="Operator profile to inspect.")
    p_operator_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_doctor = operator_sub.add_parser("doctor", help="Print a compact local production readiness verdict.")
    p_operator_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_doctor.add_argument("--profile", choices=["local-operator", "internal-dogfood"], default="internal-dogfood", help="Operator profile to inspect.")
    p_operator_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_verify_harness = operator_sub.add_parser("verify-harness", help="Verify repo-local wiring for one harness.")
    p_operator_verify_harness.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_verify_harness.add_argument(
        "--harness",
        choices=[
            "claude",
            "codex",
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
            "openclaw",
            "hermes",
        ],
        required=True,
        help="Harness id to verify.",
    )
    p_operator_verify_harness.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_sync_tools = operator_sub.add_parser("sync-tools", help="Project tracked portable tool sources into local harness folders.")
    p_operator_sync_tools.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_sync_tools.add_argument("--dry-run", action="store_true", help="Plan projection writes without changing files.")
    p_operator_sync_tools.add_argument("--force", action="store_true", help="Overwrite unmanaged or locally edited projection files.")
    p_operator_sync_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_quickstart = operator_sub.add_parser("quickstart", help="Prepare a new user workspace with Brigade configs, portable tools, and harness checks.")
    p_operator_quickstart.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_quickstart.add_argument("--depth", choices=["repo", "workspace"], default="repo", help="Install depth for Brigade bootstrap files.")
    p_operator_quickstart.add_argument("--harnesses", default="codex", help="Comma-separated harness ids, or none.")
    p_operator_quickstart.add_argument("--owner", default=None, help="Override the canonical memory owner.")
    p_operator_quickstart.add_argument("--tool-pack", type=Path, default=None, help="Optional `brigade tools pack build` directory to import.")
    p_operator_quickstart.add_argument("--skill-pack", type=Path, default=None, help="Optional `brigade skills pack build` directory to import.")
    p_operator_quickstart.add_argument("--dry-run", action="store_true", help="Plan writes without changing files.")
    p_operator_quickstart.add_argument("--force", action="store_true", help="Overwrite existing generated local setup files when supported.")
    p_operator_quickstart.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_bootstrap_portable = operator_sub.add_parser("bootstrap-portable", help="Import optional portable packs and sync tools across local harnesses.")
    p_operator_bootstrap_portable.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_bootstrap_portable.add_argument("--tool-pack", type=Path, default=None, help="Optional `brigade tools pack build` directory to import first.")
    p_operator_bootstrap_portable.add_argument("--skill-pack", type=Path, default=None, help="Optional `brigade skills pack build` directory to import first.")
    p_operator_bootstrap_portable.add_argument("--dry-run", action="store_true", help="Plan projection writes without changing files.")
    p_operator_bootstrap_portable.add_argument("--force", action="store_true", help="Overwrite conflicting imported entries or projection files.")
    p_operator_bootstrap_portable.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # runbook
    p_runbook = sub.add_parser("runbook", help="Plan, run, resume, and close out explicit local runbooks.")
    runbook_sub = p_runbook.add_subparsers(dest="runbook_command", metavar="<runbook-command>")
    runbook_sub.required = True
    p_runbook_plan = runbook_sub.add_parser("plan", help="Inspect a runbook without executing it.")
    p_runbook_plan.add_argument("runbook", type=Path, help="Runbook JSON file.")
    p_runbook_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace command target.")
    p_runbook_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_run = runbook_sub.add_parser("run", help="Run a reviewed runbook and write a receipt.")
    p_runbook_run.add_argument("runbook", nargs="?", type=Path, help="Runbook JSON file.")
    p_runbook_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace command target.")
    p_runbook_run.add_argument("--approved", action="store_true", help="Approve this explicit runbook execution.")
    p_runbook_run.add_argument("--dry-run", action="store_true", help="Validate and show steps without executing.")
    p_runbook_run.add_argument("--resume", dest="resume_run_id", default=None, help="Retry from the first failed step of a previous run.")
    p_runbook_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_resume = runbook_sub.add_parser("resume", help="Show resume information for a runbook run.")
    p_runbook_resume.add_argument("run_id", nargs="?", default="latest", help="Run id, unique prefix, or latest.")
    p_runbook_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_runbook_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_closeout = runbook_sub.add_parser("closeout", help="Mark a runbook run reviewed or deferred.")
    p_runbook_closeout.add_argument("run_id", nargs="?", default="latest", help="Run id, unique prefix, or latest.")
    p_runbook_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_runbook_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed")
    p_runbook_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_runbook_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # dogfood
    p_dogfood = sub.add_parser("dogfood", help="Run a safe Brigade dogfood review with a configured agent CLI.")
    p_dogfood.add_argument(
        "dogfood_args",
        nargs="*",
        help="Dogfood task, or `init` to write local dogfood defaults.",
    )
    p_dogfood.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_dogfood.add_argument("--output-dir", type=Path, default=None, help="Directory for run artifacts.")
    p_dogfood.add_argument(
        "--agent-cli",
        default=None,
        help="Agent CLI for dogfood runs: codex, claude, opencode, antigravity, pi, cursor, or ollama:<model>.",
    )
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
        help="Use Codex's native read-only sandbox instead of the dogfood trusted-workspace default.",
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
    p_release_schema = release_sub.add_parser("schema", help="Show local release evidence schema manifest.")
    p_release_schema.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_ci = release_sub.add_parser("ci", help="Inspect local CI platform deprecation evidence.")
    release_ci_sub = p_release_ci.add_subparsers(dest="release_ci_command", metavar="<release-ci-command>")
    release_ci_sub.required = True
    p_release_ci_doctor = release_ci_sub.add_parser("doctor", help="Check local GitHub Actions platform deprecation evidence.")
    p_release_ci_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_ci_doctor.add_argument("--summary-path", type=Path, default=None, help="Optional local GitHub Actions summary or log file.")
    p_release_ci_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_ci_import = release_ci_sub.add_parser("import-issues", help="Import CI platform deprecation findings into the local work inbox.")
    p_release_ci_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_ci_import.add_argument("--summary-path", type=Path, default=None, help="Optional local GitHub Actions summary or log file.")
    p_release_ci_import.add_argument("--dry-run", action="store_true", help="Validate without writing imports.")
    p_release_ci_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke = release_sub.add_parser("smoke", help="Record and inspect local install smoke matrix receipts.")
    release_smoke_sub = p_release_smoke.add_subparsers(dest="release_smoke_command", metavar="<release-smoke-command>")
    release_smoke_sub.required = True
    p_release_smoke_plan = release_smoke_sub.add_parser("plan", help="Show the supported install smoke matrix.")
    p_release_smoke_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_smoke_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_record = release_smoke_sub.add_parser("record", help="Record one install smoke result.")
    p_release_smoke_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_smoke_record.add_argument("--depth", choices=["repo", "workspace"], default="repo", help="Install depth.")
    p_release_smoke_record.add_argument("--harnesses", default="none", help="Comma-separated harnesses or none.")
    p_release_smoke_record.add_argument("--status", choices=sorted(release_cmd.INSTALL_SMOKE_STATUSES), default="passed", help="Smoke result status.")
    p_release_smoke_record.add_argument("--command-label", default=None, help="Safe command label.")
    p_release_smoke_record.add_argument("--summary", default=None, help="Safe result summary.")
    p_release_smoke_record.add_argument("--receipt-json", type=Path, default=None, help="Parse an existing local smoke receipt JSON file.")
    p_release_smoke_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_list = release_smoke_sub.add_parser("list", help="List install smoke receipts.")
    p_release_smoke_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_smoke_list.add_argument("--limit", type=int, default=20, help="Maximum receipts to list.")
    p_release_smoke_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_show = release_smoke_sub.add_parser("show", help="Show one install smoke receipt.")
    p_release_smoke_show.add_argument("receipt_id", help="Receipt id, unique prefix, or latest.")
    p_release_smoke_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_smoke_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_doctor = release_smoke_sub.add_parser("doctor", help="Check install smoke matrix health.")
    p_release_smoke_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_smoke_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_release_candidate_audit = release_candidate_sub.add_parser("audit", help="Audit one local release candidate bundle.")
    p_release_candidate_audit.add_argument("candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_candidate_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_import = release_candidate_sub.add_parser("import-issues", help="Import release candidate audit issues.")
    p_release_candidate_import.add_argument("candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_candidate_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_release_candidate_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_roadmap_archive = roadmap_sub.add_parser("archive", help="Show archived roadmap items that left the active queue.")
    p_roadmap_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_commands = roadmap_sub.add_parser("commands", help="Show parser-derived command documentation coverage.")
    p_roadmap_commands.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_commands.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_commands.add_argument("--write", action="store_true", help="Write docs/command-inventory.md from the CLI parser.")
    p_roadmap_commands.add_argument("--check", action="store_true", help="Fail when docs/command-inventory.md is missing or stale.")

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
    p_repos_first_run = repos_sub.add_parser("first-run", help="Plan the first repo-fleet production evidence run.")
    repos_first_run_sub = p_repos_first_run.add_subparsers(dest="repos_first_run_command", metavar="<repos-first-run-command>")
    repos_first_run_sub.required = True
    p_repos_first_run_plan = repos_first_run_sub.add_parser("plan", help="Show the manual first-run repo-fleet sequence.")
    p_repos_first_run_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_first_run_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_ingest = repos_sub.add_parser("ingest", help="Ingest every fleet repo's handoffs into the canonical owner.")
    p_repos_ingest.add_argument("--target", "-t", type=Path, default=Path("."), help="Canonical memory owner (where the fleet config lives).")
    p_repos_ingest.add_argument("--apply", action="store_true", help="Write changes. Default is a dry run.")
    p_repos_ingest.add_argument("--no-promote-cards", action="store_true", help="Do not auto-promote cards.")
    p_repos_ingest.add_argument("--no-route-documents", action="store_true", help="Do not auto-route documents.")
    p_repos_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_health_commands = repos_sub.add_parser("health-commands", help="Inspect configured optional repo health commands.")
    p_repos_health_commands.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_health_commands.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_discover = repos_sub.add_parser("discover", help="Plan safe repo discovery under configured roots.")
    repos_discover_sub = p_repos_discover.add_subparsers(dest="repos_discover_command", metavar="<repos-discover-command>")
    repos_discover_sub.required = True
    p_repos_discover_plan = repos_discover_sub.add_parser("plan", help="Dry-run discovery under configured roots.")
    p_repos_discover_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_repos_discover_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_repos_actions_dispatch.add_argument("dispatch_args", nargs="*", help="Use `plan <action-id>`, `apply <action-id>`, or `report <action-id>`. Omit with --all-reviewed.")
    p_repos_actions_dispatch.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_actions_dispatch.add_argument("--all-reviewed", action="store_true", help="Dispatch all reviewed pending or active fleet actions.")
    p_repos_actions_dispatch.add_argument("--all", dest="all_actions", action="store_true", help="Include all fleet actions for dispatch reports.")
    p_repos_actions_dispatch.add_argument("--include-deferred", action="store_true", help="Allow dispatching deferred actions.")
    p_repos_actions_dispatch.add_argument("--dry-run", action="store_true", help="Plan without writing target imports or action metadata.")
    p_repos_actions_dispatch.add_argument("--record", action="store_true", help="Record a local dispatch report receipt.")
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
    for name in ("reconcile", "summary", "report", "matrix", "checklist", "ready", "activity", "manifest", "audit"):
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
    p_repos_release_waivers_record.add_argument("--owner-label", default=None, help="Safe review owner label.")
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
    p_repos_release_waivers_renew.add_argument("--owner-label", default=None, help="Safe review owner label.")
    p_repos_release_waivers_renew.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_repos_release_waivers_renew.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repos_release_waivers_templates = repos_release_waivers_sub.add_parser("templates", help="List fleet release waiver policy templates.")
    p_repos_release_waivers_templates.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_handoff_sources = handoff_sub.add_parser("sources", help="Manage local handoff source coverage.")
    handoff_sources_sub = p_handoff_sources.add_subparsers(dest="handoff_sources_command", metavar="<handoff-sources-command>")
    handoff_sources_sub.required = True
    p_handoff_sources_init = handoff_sources_sub.add_parser("init", help="Write local handoff source coverage for Claude, Codex, OpenCode, and Hermes.")
    p_handoff_sources_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_sources_init.add_argument("--force", action="store_true", help="Overwrite an existing source config.")
    p_handoff_sources_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_doctor = handoff_sub.add_parser("doctor", help="Check handoff inboxes against local source config.")
    p_handoff_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_doctor.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_lint = handoff_sub.add_parser("lint", help="Validate pending or explicit memory handoff files.")
    p_handoff_lint.add_argument("paths", nargs="*", type=Path, help="Handoff files to validate. Defaults to pending inbox files.")
    p_handoff_lint.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_lint.add_argument("--content-guard", action="store_true", help="Also scan handoff files with content-guard.")
    p_handoff_lint.add_argument("--guard-policy", default="personal", help="Content Guard policy name or path for --content-guard.")
    p_handoff_lint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_draft = handoff_sub.add_parser("draft", help="Write a linted Memory Handoff draft in Brigade style.")
    p_handoff_draft.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_draft.add_argument(
        "--inbox",
        default="codex",
        help="Writer inbox path or alias: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, hermes.",
    )
    p_handoff_draft.add_argument("--type", default="workflow", help="Handoff type, such as workflow, decision, setup, or bugfix.")
    p_handoff_draft.add_argument("--title", required=True, help="Short handoff title.")
    p_handoff_draft.add_argument("--summary", required=True, help="Short handoff summary.")
    p_handoff_draft.add_argument("--fact", action="append", default=[], help="Durable fact bullet. May be repeated.")
    p_handoff_draft.add_argument("--evidence", action="append", default=[], help="Evidence bullet. May be repeated.")
    p_handoff_draft.add_argument("--action", choices=["create-card", "update-card", "no-card"], default="no-card", help="Recommended memory action.")
    p_handoff_draft.add_argument("--target-card", default=None, help="Target card filename for card handoffs.")
    p_handoff_draft.add_argument("--target-document", default=".learnings/LEARNINGS.md", help="Target document for no-card handoffs.")
    p_handoff_draft.add_argument("--content", default=None, help="Suggested card or document content.")
    p_handoff_draft.add_argument("--content-file", type=Path, default=None, help="Read suggested content from a file.")
    p_handoff_draft.add_argument("--id", dest="draft_id", default=None, help="Stable id slug to use in the filename.")
    p_handoff_draft.add_argument("--force", action="store_true", help="Overwrite an existing generated draft path.")
    p_handoff_draft.add_argument("--guard", action="store_true", help="Scan the generated draft with content-guard before returning success.")
    p_handoff_draft.add_argument("--guard-policy", default="personal", help="Content Guard policy name or path for --guard.")
    p_handoff_draft.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_handoff_receipt = handoff_sub.add_parser("receipt", help="Plan or record external handoff ingestion receipts.")
    handoff_receipt_sub = p_handoff_receipt.add_subparsers(dest="handoff_receipt_command", metavar="<handoff-receipt-command>")
    handoff_receipt_sub.required = True
    p_handoff_receipt_plan = handoff_receipt_sub.add_parser("plan", help="Preview a normalized external handoff ingest receipt.")
    p_handoff_receipt_plan.add_argument("draft_ids", nargs="*", help="Draft ids, filenames, paths, or unique prefixes.")
    p_handoff_receipt_plan.add_argument("--all-reviewed", action="store_true", help="Include all lint-valid reviewed drafts.")
    p_handoff_receipt_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_receipt_plan.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_receipt_plan.add_argument("--status", choices=["ingested", "skipped", "failed"], default="ingested", help="Outcome to record for selected drafts.")
    p_handoff_receipt_plan.add_argument("--owner", default="external", help="Safe memory owner label, such as openclaw or hermes.")
    p_handoff_receipt_plan.add_argument("--run-id", default=None, help="Explicit receipt run id.")
    p_handoff_receipt_plan.add_argument("--safe-summary", default=None, help="Safe receipt summary.")
    p_handoff_receipt_plan.add_argument("--log-path", default=None, help="Optional local log path label.")
    p_handoff_receipt_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_receipt_record = handoff_receipt_sub.add_parser("record", help="Write a normalized external handoff ingest receipt.")
    p_handoff_receipt_record.add_argument("draft_ids", nargs="*", help="Draft ids, filenames, paths, or unique prefixes.")
    p_handoff_receipt_record.add_argument("--all-reviewed", action="store_true", help="Include all lint-valid reviewed drafts.")
    p_handoff_receipt_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_receipt_record.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_receipt_record.add_argument("--status", choices=["ingested", "skipped", "failed"], default="ingested", help="Outcome to record for selected drafts.")
    p_handoff_receipt_record.add_argument("--owner", default="external", help="Safe memory owner label, such as openclaw or hermes.")
    p_handoff_receipt_record.add_argument("--run-id", default=None, help="Explicit receipt run id.")
    p_handoff_receipt_record.add_argument("--safe-summary", default=None, help="Safe receipt summary.")
    p_handoff_receipt_record.add_argument("--log-path", default=None, help="Optional local log path label.")
    p_handoff_receipt_record.add_argument("--force", action="store_true", help="Overwrite an existing receipt with the same run id.")
    p_handoff_receipt_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_memory_care_plan_fixes = memory_care_sub.add_parser("plan-fixes", help="Plan safe memory-care metadata fixes without writing files.")
    p_memory_care_plan_fixes.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_memory_care_plan_fixes.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_plan_promote.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_plan_promote.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_plan_proposals = work_sub.add_parser("plan-proposals", help="List local draft plan proposals.")
    p_work_plan_proposals.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_plan_proposals.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_backup_contract = backup_sub.add_parser("contract", help="Show the backup summary producer JSON contract.")
    p_work_backup_contract.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_backup_contract.add_argument("--destination", help="Limit the contract to one destination id.")
    p_work_backup_contract.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_phases = work_sub.add_parser("phases", help="Plan and inspect auditable phase execution records.")
    phases_sub = p_work_phases.add_subparsers(dest="phases_command", metavar="<phases-command>")
    phases_sub.required = True
    p_work_phases_init = phases_sub.add_parser("init", help="Initialize the local phase execution ledger.")
    p_work_phases_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_plan = phases_sub.add_parser("plan", help="Plan one phase or a range of phases.")
    p_work_phases_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_plan.add_argument("--phase-id", "--phase", dest="phase_id", default=None, help="Phase id to create, such as phase-165.")
    p_work_phases_plan.add_argument("--range", dest="phase_range", default=None, help="Phase range to create, such as 165-170.")
    p_work_phases_plan.add_argument("--title", default=None, help="Phase title.")
    p_work_phases_plan.add_argument("--goal", dest="source_goal", default=None, help="Source goal text or label.")
    p_work_phases_plan.add_argument("--grouped", action="store_true", help="Declare an explicit grouped phase range.")
    p_work_phases_plan.add_argument("--force", action="store_true", help="Overwrite existing phase records.")
    p_work_phases_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_list = phases_sub.add_parser("list", help="List local phase records.")
    p_work_phases_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_schema = phases_sub.add_parser("schema", help="Show phase ledger JSON contracts.")
    p_work_phases_schema.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_status = phases_sub.add_parser("status", help="Summarize phase ledger range status.")
    p_work_phases_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_status.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_next = phases_sub.add_parser("next", help="Show the next open phase.")
    p_work_phases_next.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_next.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_show = phases_sub.add_parser("show", help="Show one local phase record.")
    p_work_phases_show.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_start = phases_sub.add_parser("start", help="Mark one phase in progress.")
    p_work_phases_start.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_start.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_complete = phases_sub.add_parser("complete", help="Attach completion evidence to one phase.")
    p_work_phases_complete.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_complete.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_complete.add_argument("--status", choices=["implemented", "verified", "committed", "pushed"], default="implemented", help="Completion status.")
    p_work_phases_complete.add_argument("--summary", default=None, help="Implementation summary.")
    p_work_phases_complete.add_argument("--file", dest="files_changed", action="append", default=[], help="Changed file. May be repeated.")
    p_work_phases_complete.add_argument("--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated.")
    p_work_phases_complete.add_argument("--test-result", default=None, help="Test result summary.")
    p_work_phases_complete.add_argument("--commit", dest="commit_hash", default=None, help="Commit hash.")
    p_work_phases_complete.add_argument("--push-ref", default=None, help="Push ref.")
    p_work_phases_complete.add_argument("--deferred-item", action="append", default=[], help="Deferred item. May be repeated.")
    p_work_phases_complete.add_argument("--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation.")
    p_work_phases_complete.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_defer = phases_sub.add_parser("defer", help="Defer one phase with a reason.")
    p_work_phases_defer.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_defer.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_defer.add_argument("--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation.")
    p_work_phases_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_closeout = phases_sub.add_parser("closeout", help="Review or close out phase records.")
    p_work_phases_closeout.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed", help="Closeout state.")
    p_work_phases_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_compare = phases_sub.add_parser("compare", help="Compare phase evidence against current local state.")
    p_work_phases_compare.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_reconcile = phases_sub.add_parser("reconcile", help="Reconcile phase commit and push evidence against local git state.")
    p_work_phases_reconcile.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_reconcile.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_privacy = phases_sub.add_parser("privacy", help="Scan phase evidence for protected private/reference values.")
    p_work_phases_privacy.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_privacy.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_handoff = phases_sub.add_parser("handoff", help="Draft a Memory Handoff from phase evidence.")
    p_work_phases_handoff.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_handoff.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_handoff.add_argument("--lint", action="store_true", help="Run handoff lint before returning.")
    p_work_phases_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_doctor = phases_sub.add_parser("doctor", help="Check phase execution ledger health.")
    p_work_phases_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_doctor.add_argument("--range", dest="phase_range", default=None, help="Required phase range, such as 165-170.")
    p_work_phases_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_import = phases_sub.add_parser("import-issues", help="Import phase ledger issues into the work inbox.")
    p_work_phases_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_import.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_import.add_argument("--dry-run", action="store_true", help="Report imports without writing them.")
    p_work_phases_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_evidence = phases_sub.add_parser("evidence", help="Attach local evidence metadata to phase records.")
    phases_evidence_sub = p_work_phases_evidence.add_subparsers(dest="phases_evidence_command", metavar="<phases-evidence-command>")
    phases_evidence_sub.required = True
    p_work_phases_evidence_add = phases_evidence_sub.add_parser("add", help="Attach local evidence to one phase.")
    p_work_phases_evidence_add.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_evidence_add.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_evidence_add.add_argument("--file", dest="files_changed", action="append", default=[], help="Changed file path. May be repeated.")
    p_work_phases_evidence_add.add_argument("--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated.")
    p_work_phases_evidence_add.add_argument("--test-result", default=None, help="Verification result summary.")
    p_work_phases_evidence_add.add_argument("--report-id", action="append", default=[], help="Related phase report id. May be repeated.")
    p_work_phases_evidence_add.add_argument("--handoff", dest="handoff_paths", action="append", default=[], help="Memory Handoff path. May be repeated.")
    p_work_phases_evidence_add.add_argument("--note", dest="notes", action="append", default=[], help="Evidence note. May be repeated.")
    p_work_phases_evidence_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify = phases_sub.add_parser("verify", help="Plan and record phase verification metadata.")
    phases_verify_sub = p_work_phases_verify.add_subparsers(dest="phases_verify_command", metavar="<phases-verify-command>")
    phases_verify_sub.required = True
    p_work_phases_verify_plan = phases_verify_sub.add_parser("plan", help="Plan verification for a phase selector.")
    p_work_phases_verify_plan.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_verify_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_verify_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify_record = phases_verify_sub.add_parser("record", help="Record one phase verification result.")
    p_work_phases_verify_record.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_verify_record.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_verify_record.add_argument("--command", dest="verification_command", required=True, help="Verification command label.")
    p_work_phases_verify_record.add_argument("--status", choices=["passed", "failed", "skipped", "deferred"], required=True, help="Verification result.")
    p_work_phases_verify_record.add_argument("--summary", default=None, help="Verification result summary.")
    p_work_phases_verify_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions = phases_sub.add_parser("actions", help="Plan and manage local phase ledger action records.")
    phases_actions_sub = p_work_phases_actions.add_subparsers(dest="phases_actions_command", metavar="<phases-actions-command>")
    phases_actions_sub.required = True
    p_work_phases_actions_plan = phases_actions_sub.add_parser("plan", help="Preview phase ledger actions from current issues.")
    p_work_phases_actions_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_actions_plan.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 201-205.")
    p_work_phases_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_build = phases_actions_sub.add_parser("build", help="Build local phase ledger action records.")
    p_work_phases_actions_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_build.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 201-205.")
    p_work_phases_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_list = phases_actions_sub.add_parser("list", help="List local phase ledger actions.")
    p_work_phases_actions_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_show = phases_actions_sub.add_parser("show", help="Show one phase ledger action.")
    p_work_phases_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_start = phases_actions_sub.add_parser("start", help="Mark one phase ledger action active.")
    p_work_phases_actions_start.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_start.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_done = phases_actions_sub.add_parser("done", help="Mark one phase ledger action done.")
    p_work_phases_actions_done.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_done.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_done.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_defer = phases_actions_sub.add_parser("defer", help="Defer one phase ledger action.")
    p_work_phases_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_defer.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_archive = phases_actions_sub.add_parser("archive", help="Archive phase ledger actions.")
    p_work_phases_actions_archive.add_argument("action_id", nargs="?", default=None, help="Action id or unique prefix.")
    p_work_phases_actions_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_archive.add_argument("--completed", action="store_true", help="Archive done and deferred actions.")
    p_work_phases_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_import = phases_actions_sub.add_parser("import-issues", help="Import open phase actions into the work inbox.")
    p_work_phases_actions_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_actions_import.add_argument("--dry-run", action="store_true", help="Report imports without writing them.")
    p_work_phases_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_goal = phases_sub.add_parser("goal", help="Draft reviewed phase goal prompts.")
    phases_goal_sub = p_work_phases_goal.add_subparsers(dest="phases_goal_command", metavar="<phases-goal-command>")
    phases_goal_sub.required = True
    p_work_phases_goal_scaffold = phases_goal_sub.add_parser("scaffold", help="Draft a local goal prompt from phase ledger state.")
    p_work_phases_goal_scaffold.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_goal_scaffold.add_argument("--range", dest="phase_range", required=True, help="Phase range, such as 211-225.")
    p_work_phases_goal_scaffold.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report = phases_sub.add_parser("report", help="Build and inspect phase ledger reports.")
    phases_report_sub = p_work_phases_report.add_subparsers(dest="phases_report_command", metavar="<phases-report-command>")
    phases_report_sub.required = True
    p_work_phases_report_build = phases_report_sub.add_parser("build", help="Build a local phase ledger report.")
    p_work_phases_report_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_report_build.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_list = phases_report_sub.add_parser("list", help="List phase ledger reports.")
    p_work_phases_report_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_show = phases_report_sub.add_parser("show", help="Show one phase ledger report.")
    p_work_phases_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_closeout = phases_report_sub.add_parser("closeout", help="Close out one phase ledger report.")
    p_work_phases_report_closeout.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_report_closeout.add_argument("--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed", help="Report closeout state.")
    p_work_phases_report_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_compare = phases_report_sub.add_parser("compare", help="Compare one phase ledger report against current state.")
    p_work_phases_report_compare.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session = phases_sub.add_parser("session", help="Start and review phase execution sessions.")
    phases_session_sub = p_work_phases_session.add_subparsers(dest="phases_session_command", metavar="<phases-session-command>")
    phases_session_sub.required = True
    p_work_phases_session_start = phases_session_sub.add_parser("start", help="Start a local phase execution session.")
    p_work_phases_session_start.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_start.add_argument("--range", dest="phase_range", required=True, help="Phase range, such as 211-225.")
    p_work_phases_session_start.add_argument("--goal", dest="source_goal", default=None, help="Source goal text.")
    p_work_phases_session_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_list = phases_session_sub.add_parser("list", help="List phase execution sessions.")
    p_work_phases_session_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_list.add_argument("--limit", type=int, default=20, help="Maximum sessions to list.")
    p_work_phases_session_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_show = phases_session_sub.add_parser("show", help="Show one phase execution session.")
    p_work_phases_session_show.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoint = phases_session_sub.add_parser("checkpoint", help="Record a local phase session checkpoint.")
    p_work_phases_session_checkpoint.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_checkpoint.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_checkpoint.add_argument("--phase-id", default=None, help="Phase id for the checkpoint.")
    p_work_phases_session_checkpoint.add_argument("--status", choices=["noted", "blocked", "recovered"], default="noted", help="Checkpoint state.")
    p_work_phases_session_checkpoint.add_argument("--summary", default=None, help="Safe checkpoint summary.")
    p_work_phases_session_checkpoint.add_argument("--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated.")
    p_work_phases_session_checkpoint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints = phases_session_sub.add_parser("checkpoints", help="List and inspect phase session checkpoints.")
    phases_session_checkpoints_sub = p_work_phases_session_checkpoints.add_subparsers(dest="phases_session_checkpoints_command", metavar="<phases-session-checkpoints-command>")
    phases_session_checkpoints_sub.required = True
    p_work_phases_session_checkpoints_list = phases_session_checkpoints_sub.add_parser("list", help="List phase session checkpoints.")
    p_work_phases_session_checkpoints_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_checkpoints_list.add_argument("--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest.")
    p_work_phases_session_checkpoints_list.add_argument("--limit", type=int, default=20, help="Maximum checkpoints to list.")
    p_work_phases_session_checkpoints_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints_show = phases_session_checkpoints_sub.add_parser("show", help="Show one phase session checkpoint.")
    p_work_phases_session_checkpoints_show.add_argument("checkpoint_id", help="Checkpoint id, unique prefix, or latest.")
    p_work_phases_session_checkpoints_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_checkpoints_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints_compare = phases_session_checkpoints_sub.add_parser("compare", help="Compare one checkpoint against current session state.")
    p_work_phases_session_checkpoints_compare.add_argument("checkpoint_id", help="Checkpoint id, unique prefix, or latest.")
    p_work_phases_session_checkpoints_compare.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_checkpoints_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints_import = phases_session_checkpoints_sub.add_parser("import-issues", help="Import checkpoint blockers into the work inbox.")
    p_work_phases_session_checkpoints_import.add_argument("checkpoint_id", nargs="?", default="latest", help="Checkpoint id, unique prefix, or latest.")
    p_work_phases_session_checkpoints_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_checkpoints_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing them.")
    p_work_phases_session_checkpoints_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints_archive = phases_session_checkpoints_sub.add_parser("archive", help="Archive one phase session checkpoint.")
    p_work_phases_session_checkpoints_archive.add_argument("checkpoint_id", help="Checkpoint id, unique prefix, or latest.")
    p_work_phases_session_checkpoints_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_checkpoints_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_note = phases_session_sub.add_parser("recovery-note", help="Record a local phase session recovery note.")
    p_work_phases_session_recovery_note.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_recovery_note.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_recovery_note.add_argument("--phase-id", default=None, help="Phase id for the recovery note.")
    p_work_phases_session_recovery_note.add_argument("--summary", default=None, help="Safe recovery note summary.")
    p_work_phases_session_recovery_note.add_argument("--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated.")
    p_work_phases_session_recovery_note.add_argument("--evidence", action="append", default=[], help="Local evidence label or reference. May be repeated.")
    p_work_phases_session_recovery_note.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_notes = phases_session_sub.add_parser("recovery-notes", help="List and inspect phase session recovery notes.")
    phases_session_recovery_notes_sub = p_work_phases_session_recovery_notes.add_subparsers(dest="phases_session_recovery_notes_command", metavar="<phases-session-recovery-notes-command>")
    phases_session_recovery_notes_sub.required = True
    p_work_phases_session_recovery_notes_list = phases_session_recovery_notes_sub.add_parser("list", help="List phase session recovery notes.")
    p_work_phases_session_recovery_notes_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_recovery_notes_list.add_argument("--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest.")
    p_work_phases_session_recovery_notes_list.add_argument("--limit", type=int, default=20, help="Maximum recovery notes to list.")
    p_work_phases_session_recovery_notes_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_notes_show = phases_session_recovery_notes_sub.add_parser("show", help="Show one phase session recovery note.")
    p_work_phases_session_recovery_notes_show.add_argument("note_id", help="Recovery note id, unique prefix, or latest.")
    p_work_phases_session_recovery_notes_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_recovery_notes_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_notes_closeout = phases_session_recovery_notes_sub.add_parser("closeout", help="Close out one phase session recovery note.")
    p_work_phases_session_recovery_notes_closeout.add_argument("note_id", help="Recovery note id, unique prefix, or latest.")
    p_work_phases_session_recovery_notes_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_recovery_notes_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed", help="Recovery note closeout state.")
    p_work_phases_session_recovery_notes_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_recovery_notes_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_risk = phases_session_sub.add_parser("risk", help="Summarize phase session risk.")
    p_work_phases_session_risk.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_risk.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_risk.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_verification = phases_session_sub.add_parser("verification", help="Summarize phase session verification.")
    p_work_phases_session_verification.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_verification.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_verification.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_privacy = phases_session_sub.add_parser("privacy", help="Summarize phase session privacy checks.")
    p_work_phases_session_privacy.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_privacy.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_handoffs = phases_session_sub.add_parser("handoffs", help="Summarize phase session handoff coverage.")
    p_work_phases_session_handoffs.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_handoffs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_handoffs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_next = phases_session_sub.add_parser("next", help="Show the next required phase session step.")
    p_work_phases_session_next.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_next.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_protocol = phases_session_sub.add_parser("protocol", help="Show wrapper-safe phase session resume protocol.")
    p_work_phases_session_protocol.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_protocol.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_protocol.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_audit = phases_session_sub.add_parser("audit", help="Self-audit AFK phase session evidence.")
    p_work_phases_session_audit.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_resume = phases_session_sub.add_parser("resume", help="Record a safe phase session resume recommendation.")
    p_work_phases_session_resume.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_closeout = phases_session_sub.add_parser("closeout", help="Close out one phase execution session.")
    p_work_phases_session_closeout.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed", help="Session closeout state.")
    p_work_phases_session_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_activity = phases_session_sub.add_parser("activity", help="Show chronological phase session activity.")
    p_work_phases_session_activity.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_activity.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_activity.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_progress = phases_session_sub.add_parser("progress", help="Show phase session progress summary.")
    p_work_phases_session_progress.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_progress.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_progress.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_import = phases_session_sub.add_parser("import-issues", help="Import unresolved phase session blockers into the work inbox.")
    p_work_phases_session_import.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing them.")
    p_work_phases_session_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_gate = phases_session_sub.add_parser("gate", help="Check whether a phase session is safe to claim complete.")
    p_work_phases_session_gate.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_gate.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_gate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report = phases_session_sub.add_parser("report", help="Build and inspect phase session reports.")
    phases_session_report_sub = p_work_phases_session_report.add_subparsers(dest="phases_session_report_command", metavar="<phases-session-report-command>")
    phases_session_report_sub.required = True
    p_work_phases_session_report_build = phases_session_report_sub.add_parser("build", help="Build a local phase session report.")
    p_work_phases_session_report_build.add_argument("session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest.")
    p_work_phases_session_report_build.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_session_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_list = phases_session_report_sub.add_parser("list", help="List phase session reports.")
    p_work_phases_session_report_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_work_phases_session_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_session_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_show = phases_session_report_sub.add_parser("show", help="Show one phase session report.")
    p_work_phases_session_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_session_report_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
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
    p_work_task_plan.add_argument("--write", action="store_true", help="Write or update the plan artifact (plan.md + JSON receipt).")
    p_work_task_plan.add_argument("--assumption", dest="assumptions", action="append", default=[], help="Planning assumption. May be repeated.")
    p_work_task_plan.add_argument("--risk", dest="risks", action="append", default=[], help="Planning risk. May be repeated.")
    p_work_task_plan.add_argument("--source", dest="sources", action="append", default=[], help="Source context ref, link, or note. May be repeated.")
    p_work_task_plan.add_argument("--next-command", dest="next_command", default=None, help="Next safe command to record in the plan.")
    p_work_task_plan.add_argument("--title", default=None, help="Plan title (defaults to the task text).")
    p_work_task_plan.add_argument("--accept", action="store_true", help="Mark the plan artifact accepted.")
    p_work_task_plan.add_argument("--meta", action="store_true", help="Write the meta-plan (plan-for-the-plan) artifact.")
    p_work_task_plan.add_argument("--step", dest="step", action="append", default=[], help="Planning step. May be repeated.")
    p_work_task_plan.add_argument("--from-research", dest="from_research", metavar="RUN_ID", default=None, help="Attach a completed research run report as quarantined (untrusted-web) plan evidence.")
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
    p_work_import_context = import_sub.add_parser("context", help="Inbox raw external context as untrusted data.")
    p_work_import_context.add_argument("text", nargs="*", help="Raw context text.")
    p_work_import_context.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_context.add_argument("--source", default="manual", help="Where the context came from.")
    p_work_import_context.add_argument(
        "--kind",
        choices=["link", "transcript", "error", "issue", "note"],
        default="note",
        dest="context_kind",
        help="Context kind.",
    )
    p_work_import_context.add_argument("--from-file", type=Path, default=None, help="Read context body from a file.")
    p_work_import_context.add_argument("--max-chars", type=int, default=20000, help="Maximum characters of body to fence.")
    p_work_import_context.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_import_issue_repairs = import_sub.add_parser("issue-repairs", help="Import repair tasks for stale issue-backed local tasks.")
    p_work_import_issue_repairs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_issue_repairs.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_issue_repairs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_work_import_content_guard = import_sub.add_parser("content-guard", help="Import Content Guard scan findings as reviewed work imports.")
    p_work_import_content_guard.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_import_content_guard.add_argument("--scan-target", type=Path, default=None, help="Path to scan. Defaults to --target.")
    p_work_import_content_guard.add_argument("--policy", default="public-repo", help="Content Guard policy name or path.")
    p_work_import_content_guard.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_work_import_content_guard.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_learn_skill_candidates = learn_sub.add_parser("skill-candidates", help="Find repeatable learning patterns that could become reviewed skills.")
    p_learn_skill_candidates.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_skill_candidates.add_argument("--min-count", type=int, default=2, help="Minimum repeated evidence count required.")
    p_learn_skill_candidates.add_argument("--source", default=None, help="Only include candidates from one learning source, such as security-scan.")
    p_learn_skill_candidates.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_propose_skill = learn_sub.add_parser("propose-skill", help="Write a reviewed skill proposal from a learning skill candidate.")
    p_learn_propose_skill.add_argument("candidate_id", help="Learning skill candidate id or unique prefix.")
    p_learn_propose_skill.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_propose_skill.add_argument("--min-count", type=int, default=2, help="Minimum repeated evidence count required.")
    p_learn_propose_skill.add_argument("--source", default=None, help="Resolve the candidate within one learning source, such as security-scan.")
    p_learn_propose_skill.add_argument("--dry-run", action="store_true", help="Preview generated skill source and inbox proposal without writing.")
    p_learn_propose_skill.add_argument("--force", action="store_true", help="Refresh an existing generated skill source.")
    p_learn_propose_skill.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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

    # research
    p_research = sub.add_parser("research", help="Run local-first deep research grounded in a trusted local corpus.")
    research_sub = p_research.add_subparsers(dest="research_command", metavar="<research-command>")
    research_sub.required = True
    p_research_run = research_sub.add_parser("run", help="Run a deep research question.")
    p_research_run.add_argument("question", help="Research question.")
    p_research_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to use.")
    p_research_run.add_argument("--corpus", default=None, help="Named corpus from research.toml.")
    p_research_run.add_argument("--source", action="append", default=[], dest="source", help="Glob path of trusted local sources (repeatable).")
    p_research_run.add_argument("--web", action="store_true", help="Enable the opt-in untrusted web tier.")
    p_research_run.add_argument("--rounds", type=int, default=None, help="Max research rounds (max_rounds).")
    p_research_run.add_argument("--max-time", type=int, default=None, dest="max_time", help="Wall-clock budget in seconds (max_time).")
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
    p_research_export = research_sub.add_parser("export-handoff", help="Export a completed research run as a linted Memory Handoff.")
    p_research_export.add_argument("run_id", help="Run id.")
    p_research_export.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_export.add_argument(
        "--inbox",
        choices=("codex", "claude", "opencode", "antigravity", "pi", "cursor", "aider", "goose", "continue", "copilot", "qwen", "kimi", "adal", "openhands", "hermes"),
        default=None,
        help="Writer harness inbox to export into.",
    )
    p_research_export.add_argument("--handoff-inbox", type=Path, default=None, help="Explicit handoff inbox path for a custom writer.")
    p_research_export.add_argument("--force", action="store_true", help="Replace an existing exported handoff at the same path.")
    p_research_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_cancel = research_sub.add_parser("cancel", help="Cancel a local research run.")
    p_research_cancel.add_argument("run_id", help="Run id.")
    p_research_cancel.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_cancel.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_resume = research_sub.add_parser("resume", help="Resume a local research run from its checkpoint.")
    p_research_resume.add_argument("run_id", help="Run id.")
    p_research_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_resume.add_argument("--rounds", type=int, default=None, help="Max research rounds (max_rounds).")
    p_research_resume.add_argument("--max-time", type=int, default=None, dest="max_time", help="Wall-clock budget in seconds (max_time).")
    p_research_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_open = research_sub.add_parser("open", help="Print the HTML report path for a local research run.")
    p_research_open.add_argument("run_id", help="Run id.")
    p_research_open.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_open.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_sources = research_sub.add_parser("sources", help="Inspect configured research source routes.")
    research_sources_sub = p_research_sources.add_subparsers(dest="research_sources_command", metavar="<sources-command>")
    research_sources_sub.required = True
    p_research_sources_list = research_sources_sub.add_parser("list", help="List configured research source routes.")
    p_research_sources_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_sources_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_sources_doctor = research_sources_sub.add_parser("doctor", help="Check configured research source routes.")
    p_research_sources_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_sources_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_handoffs = research_sub.add_parser("handoffs", help="Inspect and route research handoff export health.")
    research_handoffs_sub = p_research_handoffs.add_subparsers(dest="research_handoffs_command", metavar="<handoffs-command>")
    research_handoffs_sub.required = True
    p_research_handoffs_doctor = research_handoffs_sub.add_parser("doctor", help="Check research handoff export health.")
    p_research_handoffs_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_research_handoffs_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_research_handoffs_import = research_handoffs_sub.add_parser("import-issues", help="Import research handoff export issues into the work inbox.")
    p_research_handoffs_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_research_handoffs_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing.")
    p_research_handoffs_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

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
    p_center_schema = center_sub.add_parser("schema", help="Show local operator-center JSON schema manifest.")
    p_center_schema.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness = center_sub.add_parser("readiness", help="Plan and close out local operator readiness.")
    center_readiness_sub = p_center_readiness.add_subparsers(dest="center_readiness_command", metavar="<center-readiness-command>")
    center_readiness_sub.required = True
    p_center_readiness_plan = center_readiness_sub.add_parser("plan", help="Plan local operator readiness without writing a receipt.")
    p_center_readiness_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_closeout = center_readiness_sub.add_parser("closeout", help="Write a local operator readiness closeout.")
    p_center_readiness_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_readiness_closeout.add_argument("--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed")
    p_center_readiness_closeout.add_argument("--reason", default=None, help="Review or waiver reason.")
    p_center_readiness_closeout.add_argument("--waive", action="append", default=[], help="Readiness finding id to waive. May be repeated.")
    p_center_readiness_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_list = center_readiness_sub.add_parser("list", help="List local operator readiness closeouts.")
    p_center_readiness_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_readiness_list.add_argument("--limit", type=int, default=20, help="Maximum closeouts to list.")
    p_center_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_show = center_readiness_sub.add_parser("show", help="Show one local operator readiness closeout.")
    p_center_readiness_show.add_argument("readiness_id", nargs="?", default="latest", help="Readiness id, unique prefix, or latest.")
    p_center_readiness_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_readiness_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_import = center_readiness_sub.add_parser("import-issues", help="Import unresolved readiness issues into the work inbox.")
    p_center_readiness_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_readiness_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_readiness_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_center_report_diff = center_report_sub.add_parser("diff", help="Diff two local operator reports.")
    p_center_report_diff.add_argument("base_report_id", help="Older report id, unique prefix, or latest.")
    p_center_report_diff.add_argument("compare_report_id", help="Newer report id or unique prefix.")
    p_center_report_diff.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect or update.")
    p_center_report_diff.add_argument("--record", action="store_true", help="Write a local report diff receipt.")
    p_center_report_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_center_actions_doctor = center_actions_sub.add_parser("doctor", help="Check local daily operator action aging policy.")
    p_center_actions_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_actions_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_import = center_actions_sub.add_parser("import-issues", help="Import stale operator action issues into the work inbox.")
    p_center_actions_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_center_actions_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_security_template_audit = security_sub.add_parser("template-audit", help="Audit public templates and docs for private values.")
    p_security_template_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_security_template_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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
    p_tools_defaults = tools_sub.add_parser("defaults", help="Merge Brigade built-in portable tools into the local catalog.")
    p_tools_defaults.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_defaults.add_argument("--dry-run", action="store_true", help="Report catalog changes without writing.")
    p_tools_defaults.add_argument("--force", action="store_true", help="Replace conflicting built-in ids with Brigade defaults.")
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
    p_tools_pack_import = tools_pack_sub.add_parser("import", help="Import catalog entries and source files from a portable tool pack.")
    p_tools_pack_import.add_argument("pack", type=Path, help="Tool pack directory containing tool-pack.json.")
    p_tools_pack_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_tools_pack_import.add_argument("--force", action="store_true", help="Overwrite existing tool ids and source files.")
    p_tools_pack_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
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

    parser.epilog = _grouped_epilog(sub)
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
    if cmd == "daily":
        from . import daily_cmd

        if args.daily_command == "init":
            return daily_cmd.init(target=args.target, force=args.force, json_output=args.json)
        if args.daily_command == "status":
            return daily_cmd.status(target=args.target, json_output=args.json)
        if args.daily_command == "plan":
            return daily_cmd.plan(target=args.target, record=args.record, json_output=args.json)
        if args.daily_command == "review":
            return daily_cmd.review(target=args.target, json_output=args.json)
        if args.daily_command == "schema":
            return daily_cmd.schema(target=args.target, json_output=args.json)
        if args.daily_command == "protocol":
            return daily_cmd.protocol(target=args.target, json_output=args.json)
        if args.daily_command == "resume":
            return daily_cmd.resume(target=args.target, json_output=args.json)
        if args.daily_command == "repair":
            return daily_cmd.repair(target=args.target, json_output=args.json)
        if args.daily_command == "unblock":
            return daily_cmd.unblock(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.daily_command == "telemetry":
            if getattr(args, "daily_telemetry_command", None) == "doctor":
                return daily_cmd.telemetry_doctor(target=args.target, json_output=args.json)
            return daily_cmd.telemetry(target=args.target, json_output=args.json)
        if args.daily_command == "hardening":
            if args.daily_hardening_command == "plan":
                return daily_cmd.hardening_plan(target=args.target, json_output=args.json)
            if args.daily_hardening_command == "audit":
                return daily_cmd.hardening_audit(target=args.target, json_output=args.json)
            if args.daily_hardening_command == "import-issues":
                return daily_cmd.hardening_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            if args.daily_hardening_command == "closeout":
                return daily_cmd.hardening_closeout(target=args.target, status=args.status, reason=args.reason, json_output=args.json)
            parser.error(f"unknown daily hardening command: {args.daily_hardening_command}")
            return 2
        if args.daily_command == "history":
            return daily_cmd.history(target=args.target, limit=args.limit, json_output=args.json)
        if args.daily_command == "show":
            return daily_cmd.show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.daily_command == "doctor":
            return daily_cmd.doctor(target=args.target, json_output=args.json)
        if args.daily_command == "approvals":
            if args.daily_approval_command == "list":
                return daily_cmd.approvals_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.daily_approval_command == "show":
                return daily_cmd.approvals_show(target=args.target, approval_id=args.approval_id, json_output=args.json)
            if args.daily_approval_command == "approve":
                return daily_cmd.approvals_approve(target=args.target, approval_id=args.approval_id, json_output=args.json)
            if args.daily_approval_command == "reject":
                return daily_cmd.approvals_reject(target=args.target, approval_id=args.approval_id, reason=args.reason, json_output=args.json)
            if args.daily_approval_command == "hold":
                return daily_cmd.approvals_hold(target=args.target, approval_id=args.approval_id, reason=args.reason, json_output=args.json)
            if args.daily_approval_command == "compare":
                return daily_cmd.approvals_compare(target=args.target, approval_id=args.approval_id, json_output=args.json)
            if args.daily_approval_command == "archive":
                return daily_cmd.approvals_archive(target=args.target, consumed=args.consumed, json_output=args.json)
            parser.error(f"unknown daily approvals command: {args.daily_approval_command}")
            return 2
        if args.daily_command == "run":
            return daily_cmd.run(target=args.target, approved=args.approved, approval_id=args.approval, plan_id=args.plan_id, replan=args.replan, json_output=args.json)
        if args.daily_command == "closeout":
            return daily_cmd.closeout(target=args.target, status=args.status, reason=args.reason, handoff=args.handoff, json_output=args.json)
        parser.error(f"unknown daily command: {args.daily_command}")
        return 2
    if cmd == "add":
        from . import add as add_mod

        return add_mod.run(target=args.target, station=args.station)
    if cmd == "pantry":
        from . import pantry_cmd

        if args.pantry_command == "status":
            return pantry_cmd.status(target=args.target, json_output=args.json)
        if args.pantry_command == "setup":
            if args.pantry_setup_command == "plan":
                return pantry_cmd.setup_plan(
                    target=args.target,
                    role=args.role,
                    peer=args.peer,
                    config_path=args.config_path,
                    key_path=args.key_path,
                    write=args.write,
                    json_output=args.json,
                )
            parser.error(f"unknown pantry setup command: {args.pantry_setup_command}")
            return 2
        if args.pantry_command == "service":
            if args.pantry_service_command == "plan":
                return pantry_cmd.service_plan(
                    target=args.target,
                    role=args.role,
                    config_path=args.config_path,
                    write=args.write,
                    json_output=args.json,
                )
            parser.error(f"unknown pantry service command: {args.pantry_service_command}")
            return 2
        parser.error(f"unknown pantry command: {args.pantry_command}")
        return 2
    if cmd == "notifications":
        from . import notifications_cmd

        if args.notifications_command == "status":
            return notifications_cmd.status(target=args.target, profile=args.profile, json_output=args.json)
        if args.notifications_command == "setup":
            if args.notifications_setup_command == "plan":
                return notifications_cmd.setup_plan(target=args.target, profile=args.profile, json_output=args.json)
            parser.error(f"unknown notifications setup command: {args.notifications_setup_command}")
            return 2
        if args.notifications_command == "event":
            if args.notifications_event_command == "plan":
                return notifications_cmd.event_plan(
                    target=args.target,
                    event_type=args.type,
                    title=args.title,
                    message=args.message,
                    level=args.level,
                    profile=args.profile,
                    source=args.source,
                    json_output=args.json,
                )
            if args.notifications_event_command == "record":
                return notifications_cmd.event_record(
                    target=args.target,
                    event_type=args.type,
                    title=args.title,
                    message=args.message,
                    level=args.level,
                    profile=args.profile,
                    source=args.source,
                    send=args.send,
                    json_output=args.json,
                )
            parser.error(f"unknown notifications event command: {args.notifications_event_command}")
            return 2
        parser.error(f"unknown notifications command: {args.notifications_command}")
        return 2
    if cmd == "budgets":
        from . import budgets_cmd

        if args.budgets_command == "show":
            return budgets_cmd.show(json_output=args.json)
        if args.budgets_command == "check":
            return budgets_cmd.check(target=args.target, json_output=args.json)
        parser.error(f"unknown budgets command: {args.budgets_command}")
        return 2
    if cmd == "untrusted":
        from . import untrusted_cmd

        if args.untrusted_command == "scan":
            return untrusted_cmd.scan(text=args.text, from_file=args.from_file, json_output=args.json)
        if args.untrusted_command == "wrap":
            return untrusted_cmd.wrap(
                text=args.text,
                from_file=args.from_file,
                source_kind=args.source_kind,
                goal=args.goal,
                max_chars=args.max_chars,
                json_output=args.json,
            )
        parser.error(f"unknown untrusted command: {args.untrusted_command}")
        return 2
    if cmd == "skills":
        from . import skills_cmd

        if args.skills_command == "search":
            return skills_cmd.search(target=args.target, query=args.query, json_output=args.json)
        if args.skills_command == "import":
            return skills_cmd.import_skill(
                target=args.target,
                source=args.source,
                skill_id=args.skill_id,
                force=args.force,
                json_output=args.json,
            )
        if args.skills_command == "lint":
            return skills_cmd.lint(target=args.target, skill=args.skill, harness=args.harness, json_output=args.json)
        if args.skills_command == "doctor":
            return skills_cmd.doctor(target=args.target, json_output=args.json)
        if args.skills_command == "import-issues":
            return skills_cmd.import_issues(target=args.target, json_output=args.json)
        if args.skills_command == "install":
            return skills_cmd.install(
                workspace=args.workspace,
                skill=args.skill,
                harness=args.install_target,
                force=args.force,
                json_output=args.json,
            )
        if args.skills_command == "compatibility":
            return skills_cmd.compatibility(target=args.target, skill=args.skill, json_output=args.json)
        if args.skills_command == "history":
            return skills_cmd.history(
                target=args.target,
                skill=args.skill,
                harness=args.harness,
                limit=args.limit,
                json_output=args.json,
            )
        if args.skills_command == "diff":
            return skills_cmd.diff(target=args.target, skill=args.skill, harness=args.harness, json_output=args.json)
        if args.skills_command == "rollback":
            return skills_cmd.rollback(workspace=args.workspace, skill=args.skill, harness=args.install_target, json_output=args.json)
        if args.skills_command == "serve-mcp":
            return skills_cmd.serve_mcp(target=args.target, json_output=args.json, stdio=args.stdio)
        if args.skills_command == "publish":
            return skills_cmd.publish(target=args.target, skill=args.skill, scope=args.scope, json_output=args.json)
        if args.skills_command == "pack":
            if args.skills_pack_command == "build":
                return skills_cmd.pack_build(target=args.target, json_output=args.json)
            if args.skills_pack_command == "list":
                return skills_cmd.pack_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.skills_pack_command == "show":
                return skills_cmd.pack_show(target=args.target, pack_id=args.pack_id, json_output=args.json)
            if args.skills_pack_command == "import":
                return skills_cmd.pack_import(target=args.target, pack=args.pack, force=args.force, json_output=args.json)
            if args.skills_pack_command == "archive":
                return skills_cmd.pack_archive(target=args.target, pack_id=args.pack_id, json_output=args.json)
            parser.error(f"unknown skills pack command: {args.skills_pack_command}")
            return 2
        if args.skills_command == "inbox":
            if args.skills_inbox_command == "add":
                return skills_cmd.inbox_add(
                    target=args.target,
                    source=args.source,
                    skill_id=args.skill_id,
                    summary=args.summary,
                    force=args.force,
                    json_output=args.json,
                )
            if args.skills_inbox_command == "list":
                return skills_cmd.inbox_list(target=args.target, json_output=args.json)
            if args.skills_inbox_command == "show":
                return skills_cmd.inbox_show(target=args.target, proposal_id=args.proposal_id, json_output=args.json)
            if args.skills_inbox_command == "diff":
                return skills_cmd.inbox_diff(target=args.target, proposal_id=args.proposal_id, json_output=args.json)
            if args.skills_inbox_command == "accept":
                return skills_cmd.inbox_accept(target=args.target, proposal_id=args.proposal_id, force=args.force, json_output=args.json)
            if args.skills_inbox_command == "reject":
                return skills_cmd.inbox_reject(target=args.target, proposal_id=args.proposal_id, reason=args.reason, json_output=args.json)
            parser.error(f"unknown skills inbox command: {args.skills_inbox_command}")
            return 2
        if args.skills_command == "adapters":
            if args.skills_adapters_command == "init":
                return skills_cmd.adapters_init(target=args.target, force=args.force, json_output=args.json)
            if args.skills_adapters_command == "list":
                return skills_cmd.adapters_list(target=args.target, include_planned=args.include_planned, json_output=args.json)
            if args.skills_adapters_command == "show":
                return skills_cmd.adapters_show(target=args.target, adapter_id=args.adapter_id, json_output=args.json)
            parser.error(f"unknown skills adapters command: {args.skills_adapters_command}")
            return 2
        parser.error(f"unknown skills command: {args.skills_command}")
        return 2
    if cmd == "operator":
        from . import operator_cmd

        if args.operator_command == "guide":
            return operator_cmd.guide(profile=args.profile, json_output=args.json)
        if args.operator_command == "plan":
            return operator_cmd.plan(target=args.target, profile=args.profile, json_output=args.json)
        if args.operator_command == "adopt":
            if args.operator_adopt_command == "plan":
                return operator_cmd.adoption_plan(target=args.target, json_output=args.json)
            if args.operator_adopt_command == "capture":
                return operator_cmd.adoption_capture(target=args.target, json_output=args.json)
            if args.operator_adopt_command == "import-issues":
                return operator_cmd.adoption_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            parser.error(f"unknown operator adopt command: {args.operator_adopt_command}")
            return 2
        if args.operator_command == "migration":
            if args.operator_migration_command == "status":
                return operator_cmd.migration_status(target=args.target, json_output=args.json)
            if args.operator_migration_command == "doctor":
                return operator_cmd.migration_doctor(target=args.target, json_output=args.json)
            if args.operator_migration_command == "import-issues":
                return operator_cmd.migration_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            if args.operator_migration_command == "consolidate":
                return operator_cmd.migration_consolidate(
                    target=args.target,
                    surface=args.surface,
                    review_status=args.review_status,
                    reason=args.reason,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
            parser.error(f"unknown operator migration command: {args.operator_migration_command}")
            return 2
        if args.operator_command == "surfaces":
            if args.operator_surfaces_command == "capture":
                return operator_cmd.surfaces_capture(target=args.target, json_output=args.json)
            if args.operator_surfaces_command == "list":
                return operator_cmd.surfaces_list(target=args.target, json_output=args.json)
            if args.operator_surfaces_command == "doctor":
                return operator_cmd.surfaces_doctor(target=args.target, surface=args.surface, json_output=args.json)
            if args.operator_surfaces_command == "review":
                return operator_cmd.surfaces_review(
                    target=args.target,
                    surface=args.surface,
                    status=args.status,
                    all_records=args.all_records,
                    record_labels=args.record_labels,
                    reason=args.reason,
                    json_output=args.json,
                )
            if args.operator_surfaces_command == "reviews":
                return operator_cmd.surfaces_reviews(target=args.target, surface=args.surface, json_output=args.json)
            if args.operator_surfaces_command == "import-issues":
                return operator_cmd.surfaces_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            parser.error(f"unknown operator surfaces command: {args.operator_surfaces_command}")
            return 2
        if args.operator_command == "init":
            return operator_cmd.init(target=args.target, profile=args.profile, force=args.force, dry_run=args.dry_run, waive_public_release=args.waive_public_release, json_output=args.json)
        if args.operator_command == "status":
            return operator_cmd.status(target=args.target, profile=args.profile, json_output=args.json)
        if args.operator_command == "doctor":
            return operator_cmd.doctor(target=args.target, profile=args.profile, json_output=args.json)
        if args.operator_command == "verify-harness":
            return operator_cmd.verify_harness(target=args.target, harness=args.harness, json_output=args.json)
        if args.operator_command == "sync-tools":
            return operator_cmd.sync_tools(target=args.target, dry_run=args.dry_run, force=args.force, json_output=args.json)
        if args.operator_command == "quickstart":
            return operator_cmd.quickstart(
                target=args.target,
                depth=args.depth,
                harnesses=args.harnesses,
                owner=args.owner,
                tool_pack=args.tool_pack,
                skill_pack=args.skill_pack,
                dry_run=args.dry_run,
                force=args.force,
                json_output=args.json,
            )
        if args.operator_command == "bootstrap-portable":
            return operator_cmd.bootstrap_portable(target=args.target, tool_pack=args.tool_pack, skill_pack=args.skill_pack, dry_run=args.dry_run, force=args.force, json_output=args.json)
        parser.error(f"unknown operator command: {args.operator_command}")
        return 2
    if cmd == "runbook":
        from . import runbook_cmd

        if args.runbook_command == "plan":
            return runbook_cmd.plan(target=args.target, runbook=args.runbook, json_output=args.json)
        if args.runbook_command == "run":
            if args.resume_run_id:
                return runbook_cmd.retry(target=args.target, run_id=args.resume_run_id, approved=args.approved, dry_run=args.dry_run, json_output=args.json)
            if args.runbook is None:
                parser.error("runbook run requires a runbook path unless --resume is used")
                return 2
            return runbook_cmd.run(target=args.target, runbook=args.runbook, approved=args.approved, dry_run=args.dry_run, json_output=args.json)
        if args.runbook_command == "resume":
            return runbook_cmd.resume(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.runbook_command == "closeout":
            return runbook_cmd.closeout(target=args.target, run_id=args.run_id, status=args.status, reason=args.reason, json_output=args.json)
        parser.error(f"unknown runbook command: {args.runbook_command}")
        return 2
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
                agent_cli=args.agent_cli or dogfood_cmd.DEFAULT_AGENT_CLI,
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
            agent_cli=args.agent_cli,
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
        if args.release_command == "schema":
            return release_cmd.schema(target=args.target, json_output=args.json)
        if args.release_command == "ci":
            if args.release_ci_command == "doctor":
                return release_cmd.ci_doctor(target=args.target, summary_path=args.summary_path, json_output=args.json)
            if args.release_ci_command == "import-issues":
                return release_cmd.ci_import_issues(target=args.target, summary_path=args.summary_path, dry_run=args.dry_run, json_output=args.json)
            parser.error(f"unknown release ci command: {args.release_ci_command}")
            return 2
        if args.release_command == "smoke":
            if args.release_smoke_command == "plan":
                return release_cmd.install_smoke_plan(target=args.target, json_output=args.json)
            if args.release_smoke_command == "record":
                return release_cmd.install_smoke_record(target=args.target, depth=args.depth, harnesses=args.harnesses, status=args.status, command_label=args.command_label, summary=args.summary, receipt_json=args.receipt_json, json_output=args.json)
            if args.release_smoke_command == "list":
                return release_cmd.install_smoke_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.release_smoke_command == "show":
                return release_cmd.install_smoke_show(target=args.target, receipt_id=args.receipt_id, json_output=args.json)
            if args.release_smoke_command == "doctor":
                return release_cmd.install_smoke_doctor(target=args.target, json_output=args.json)
            parser.error(f"unknown release smoke command: {args.release_smoke_command}")
            return 2
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
            if args.release_candidate_command == "audit":
                return release_cmd.candidate_audit(target=args.target, candidate_id=args.candidate_id, json_output=args.json)
            if args.release_candidate_command == "import-issues":
                return release_cmd.candidate_import_issues(
                    target=args.target,
                    candidate_id=args.candidate_id,
                    dry_run=args.dry_run,
                    json_output=args.json,
                )
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
        if args.roadmap_command == "archive":
            return roadmap_cmd.archive(target=args.target, json_output=args.json)
        if args.roadmap_command == "commands":
            return roadmap_cmd.commands(target=args.target, json_output=args.json, write_inventory=args.write, check_inventory=args.check)
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
        if args.repos_command == "first-run":
            if args.repos_first_run_command == "plan":
                return repos_cmd.first_run_plan(target=args.target, json_output=args.json)
            parser.error(f"unknown repos first-run command: {args.repos_first_run_command}")
            return 2
        if args.repos_command == "ingest":
            return repos_cmd.ingest_fleet(
                target=args.target,
                apply=args.apply,
                promote_cards=not args.no_promote_cards,
                route_documents=not args.no_route_documents,
                json_output=args.json,
            )
        if args.repos_command == "health-commands":
            return repos_cmd.health_commands(target=args.target, json_output=args.json)
        if args.repos_command == "discover":
            if args.repos_discover_command == "plan":
                return repos_cmd.discover_plan(target=args.target, json_output=args.json)
            parser.error(f"unknown repos discover command: {args.repos_discover_command}")
            return 2
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
                if dispatch_args and dispatch_args[0] in {"plan", "apply", "report"}:
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
                    return repos_cmd.release_waiver_record(target=args.target, train_id=args.train_id, scope=args.scope, repo_id=args.repo_id, reason=args.reason, expires_at=args.expires_at, owner_label=args.owner_label, json_output=args.json)
                if args.repos_release_waivers_command == "list":
                    return repos_cmd.release_waiver_list(target=args.target, train_id=args.train_id, limit=args.limit, json_output=args.json)
                if args.repos_release_waivers_command == "show":
                    return repos_cmd.release_waiver_show(target=args.target, waiver_id=args.waiver_id, json_output=args.json)
                if args.repos_release_waivers_command == "revoke":
                    return repos_cmd.release_waiver_revoke(target=args.target, waiver_id=args.waiver_id, reason=args.reason, json_output=args.json)
                if args.repos_release_waivers_command == "renew":
                    return repos_cmd.release_waiver_renew(target=args.target, waiver_id=args.waiver_id, reason=args.reason, expires_at=args.expires_at, owner_label=args.owner_label, json_output=args.json)
                if args.repos_release_waivers_command == "doctor":
                    return repos_cmd.release_waiver_doctor(target=args.target, train_id=args.train_id, json_output=args.json)
                if args.repos_release_waivers_command == "import-issues":
                    return repos_cmd.release_waiver_import_issues(target=args.target, train_id=args.train_id, dry_run=args.dry_run, json_output=args.json)
                if args.repos_release_waivers_command == "templates":
                    return repos_cmd.release_waiver_templates(json_output=args.json)
                parser.error(f"unknown repos release waivers command: {args.repos_release_waivers_command}")
                return 2
            parser.error(f"unknown repos release command: {args.repos_release_command}")
            return 2
        parser.error(f"unknown repos command: {args.repos_command}")
        return 2
    if cmd == "handoff":
        from . import handoff_cmd

        if args.handoff_command == "sources":
            if args.handoff_sources_command == "init":
                return handoff_cmd.sources_init(target=args.target, force=args.force, json_output=args.json)
            parser.error(f"unknown handoff sources command: {args.handoff_sources_command}")
            return 2
        if args.handoff_command == "doctor":
            return handoff_cmd.doctor(target=args.target, sources=args.sources, json_output=args.json)
        if args.handoff_command == "lint":
            return handoff_cmd.lint(target=args.target, paths=args.paths, content_guard=args.content_guard, guard_policy=args.guard_policy, json_output=args.json)
        if args.handoff_command == "draft":
            return handoff_cmd.draft(
                target=args.target,
                title=args.title,
                summary=args.summary,
                content=args.content,
                content_file=args.content_file,
                handoff_type=args.type,
                action=args.action,
                target_card=args.target_card,
                target_document=args.target_document,
                fact=args.fact,
                evidence=args.evidence,
                inbox=args.inbox,
                draft_id=args.draft_id,
                force=args.force,
                guard=args.guard,
                guard_policy=args.guard_policy,
                json_output=args.json,
            )
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
        if args.handoff_command == "receipt":
            if args.handoff_receipt_command == "plan":
                return handoff_cmd.receipt_plan(
                    target=args.target,
                    draft_ids=args.draft_ids,
                    all_reviewed=args.all_reviewed,
                    sources=args.sources,
                    status=args.status,
                    owner=args.owner,
                    run_id=args.run_id,
                    safe_summary=args.safe_summary,
                    log_path=args.log_path,
                    json_output=args.json,
                )
            if args.handoff_receipt_command == "record":
                return handoff_cmd.receipt_record(
                    target=args.target,
                    draft_ids=args.draft_ids,
                    all_reviewed=args.all_reviewed,
                    sources=args.sources,
                    status=args.status,
                    owner=args.owner,
                    run_id=args.run_id,
                    safe_summary=args.safe_summary,
                    log_path=args.log_path,
                    force=args.force,
                    json_output=args.json,
                )
            parser.error(f"unknown handoff receipt command: {args.handoff_receipt_command}")
            return 2
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
        if args.learn_command == "skill-candidates":
            return learn_cmd.skill_candidates(target=args.target, min_count=args.min_count, source=args.source, json_output=args.json)
        if args.learn_command == "propose-skill":
            return learn_cmd.propose_skill(target=args.target, candidate_id=args.candidate_id, min_count=args.min_count, source=args.source, dry_run=args.dry_run, force=args.force, json_output=args.json)
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
    if cmd == "research":
        from . import research_cmd

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
            return research_cmd.cli_resume(target=args.target, run_id=args.run_id, overrides=overrides, json_output=args.json)
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
                return research_cmd.cli_handoffs_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            parser.error(f"unknown research handoffs command: {args.research_handoffs_command}")
            return 2
        parser.error(f"unknown research command: {args.research_command}")
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
                return center_cmd.readiness_show(target=args.target, readiness_id=args.readiness_id, json_output=args.json)
            if args.center_readiness_command == "import-issues":
                return center_cmd.readiness_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
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
                return work_cmd.review_finding_show(target=args.target, finding_id=args.finding_id, json_output=args.json)
            if args.review_command == "closeout":
                return work_cmd.review_closeout(target=args.target, run_id=args.run_id, json_output=args.json)
            parser.error(f"unknown review command: {args.review_command}")
            return 2
        if args.work_command == "phases":
            from . import phases_cmd

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
                return phases_cmd.closeout(target=args.target, selector=args.selector, status=args.status, reason=args.reason, json_output=args.json)
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
                return phases_cmd.import_issues(target=args.target, phase_range=args.phase_range, dry_run=args.dry_run, json_output=args.json)
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
                    return phases_cmd.verify_record(target=args.target, phase_id=args.phase_id, command=args.verification_command, status=args.status, summary=args.summary, json_output=args.json)
                parser.error(f"unknown phases verify command: {args.phases_verify_command}")
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
                    return phases_cmd.actions_defer(target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json)
                if args.phases_actions_command == "archive":
                    return phases_cmd.actions_archive(target=args.target, action_id=args.action_id, completed=args.completed, json_output=args.json)
                if args.phases_actions_command == "import-issues":
                    return phases_cmd.actions_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
                parser.error(f"unknown phases actions command: {args.phases_actions_command}")
                return 2
            if args.phases_command == "goal":
                if args.phases_goal_command == "scaffold":
                    return phases_cmd.goal_scaffold(target=args.target, phase_range=args.phase_range, json_output=args.json)
                parser.error(f"unknown phases goal command: {args.phases_goal_command}")
                return 2
            if args.phases_command == "report":
                if args.phases_report_command == "build":
                    return phases_cmd.report_build(target=args.target, phase_range=args.phase_range, json_output=args.json)
                if args.phases_report_command == "list":
                    return phases_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_report_command == "show":
                    return phases_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
                if args.phases_report_command == "closeout":
                    return phases_cmd.report_closeout(target=args.target, report_id=args.report_id, status=args.status, reason=args.reason, json_output=args.json)
                if args.phases_report_command == "compare":
                    return phases_cmd.report_compare(target=args.target, report_id=args.report_id, json_output=args.json)
                parser.error(f"unknown phases report command: {args.phases_report_command}")
                return 2
            if args.phases_command == "session":
                if args.phases_session_command == "start":
                    return phases_cmd.session_start(target=args.target, phase_range=args.phase_range, source_goal=args.source_goal, json_output=args.json)
                if args.phases_session_command == "list":
                    return phases_cmd.session_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_session_command == "show":
                    return phases_cmd.session_show(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "checkpoint":
                    return phases_cmd.session_checkpoint(target=args.target, session_id=args.session_id, phase_id=args.phase_id, status=args.status, summary=args.summary, notes=args.notes, json_output=args.json)
                if args.phases_session_command == "checkpoints":
                    if args.phases_session_checkpoints_command == "list":
                        return phases_cmd.session_checkpoint_list(target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json)
                    if args.phases_session_checkpoints_command == "show":
                        return phases_cmd.session_checkpoint_show(target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json)
                    if args.phases_session_checkpoints_command == "compare":
                        return phases_cmd.session_checkpoint_compare(target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json)
                    if args.phases_session_checkpoints_command == "import-issues":
                        return phases_cmd.session_checkpoint_import_issues(target=args.target, checkpoint_id=args.checkpoint_id, dry_run=args.dry_run, json_output=args.json)
                    if args.phases_session_checkpoints_command == "archive":
                        return phases_cmd.session_checkpoint_archive(target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json)
                    parser.error(f"unknown phases session checkpoints command: {args.phases_session_checkpoints_command}")
                if args.phases_session_command == "recovery-note":
                    return phases_cmd.session_recovery_note(target=args.target, session_id=args.session_id, phase_id=args.phase_id, summary=args.summary, notes=args.notes, evidence=args.evidence, json_output=args.json)
                if args.phases_session_command == "recovery-notes":
                    if args.phases_session_recovery_notes_command == "list":
                        return phases_cmd.session_recovery_note_list(target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json)
                    if args.phases_session_recovery_notes_command == "show":
                        return phases_cmd.session_recovery_note_show(target=args.target, note_id=args.note_id, json_output=args.json)
                    if args.phases_session_recovery_notes_command == "closeout":
                        return phases_cmd.session_recovery_note_closeout(target=args.target, note_id=args.note_id, status=args.status, reason=args.reason, json_output=args.json)
                    parser.error(f"unknown phases session recovery notes command: {args.phases_session_recovery_notes_command}")
                if args.phases_session_command == "risk":
                    return phases_cmd.session_risk(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "verification":
                    return phases_cmd.session_verification(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "privacy":
                    return phases_cmd.session_privacy(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "handoffs":
                    return phases_cmd.session_handoffs(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "next":
                    return phases_cmd.session_next(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "protocol":
                    return phases_cmd.session_protocol(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "audit":
                    return phases_cmd.session_audit(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "resume":
                    return phases_cmd.session_resume(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "closeout":
                    return phases_cmd.session_closeout(target=args.target, session_id=args.session_id, status=args.status, reason=args.reason, json_output=args.json)
                if args.phases_session_command == "activity":
                    return phases_cmd.session_activity(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "progress":
                    return phases_cmd.session_progress(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "import-issues":
                    return phases_cmd.session_import_issues(target=args.target, session_id=args.session_id, dry_run=args.dry_run, json_output=args.json)
                if args.phases_session_command == "gate":
                    return phases_cmd.session_gate(target=args.target, session_id=args.session_id, json_output=args.json)
                if args.phases_session_command == "report":
                    if args.phases_session_report_command == "build":
                        return phases_cmd.session_report_build(target=args.target, session_id=args.session_id, json_output=args.json)
                    if args.phases_session_report_command == "list":
                        return phases_cmd.session_report_list(target=args.target, limit=args.limit, json_output=args.json)
                    if args.phases_session_report_command == "show":
                        return phases_cmd.session_report_show(target=args.target, report_id=args.report_id, json_output=args.json)
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
        if args.security_command == "template-audit":
            return security_cmd.template_audit(target=args.target, json_output=args.json)
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
            if args.tools_pack_command == "import":
                return tools_cmd.pack_import(target=args.target, pack=args.pack, force=args.force, json_output=args.json)
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
