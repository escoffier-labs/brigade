"""brigade research command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
            "grok",
            "amp",
            "crush",
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
    p_research.set_defaults(func=dispatch)


def dispatch(args) -> int:
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
        args._brigade_parser.error(f"unknown research sources command: {args.research_sources_command}")
        return 2
    if args.research_command == "handoffs":
        if args.research_handoffs_command == "doctor":
            return research_cmd.cli_handoffs_doctor(target=args.target, json_output=args.json)
        if args.research_handoffs_command == "import-issues":
            return research_cmd.cli_handoffs_import_issues(
                target=args.target, dry_run=args.dry_run, json_output=args.json
            )
        args._brigade_parser.error(f"unknown research handoffs command: {args.research_handoffs_command}")
        return 2
    args._brigade_parser.error(f"unknown research command: {args.research_command}")
    return 2
