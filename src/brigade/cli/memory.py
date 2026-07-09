"""brigade memory command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
    p_memory_search = memory_sub.add_parser("search", help="Keyword-search local memory cards.")
    p_memory_search.add_argument("query", help="Search terms (matched against title, tags, summary, and body).")
    p_memory_search.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to search.")
    p_memory_search.add_argument("--limit", type=int, default=20, help="Maximum matches to show.")
    p_memory_search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_memory_serve_mcp = memory_sub.add_parser(
        "serve-mcp", help="Expose memory cards over a read-only MCP stdio server (card:// scheme)."
    )
    p_memory_serve_mcp.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace whose cards to serve."
    )
    p_memory_serve_mcp.add_argument(
        "--stdio", action="store_true", help="Run the JSON-RPC stdio server (otherwise print the contract)."
    )
    p_memory_serve_mcp.add_argument("--json", action="store_true", help="Print the contract as JSON.")

    # Embedded memory-doctor verbs (status / lint / compact / init-git).
    def _add_md_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--target", "-t", type=Path, default=None, help="Repo/workspace (uses memory/ + handoff inboxes)."
        )
        p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md).")
        p.add_argument("--handoffs-dir", default=None, help="Handoffs dir.")
        p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md line threshold.")
        p.add_argument("--max-bytes", type=int, default=None, help="MEMORY.md byte threshold.")

    p_memory_status = memory_sub.add_parser(
        "status", help="Read-only memory health summary (cards, index size, handoffs, dead links)."
    )
    _add_md_common(p_memory_status)
    p_memory_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_memory_lint = memory_sub.add_parser("lint", help="Find dead [[wiki-links]] in memory cards; exit 1 if any.")
    _add_md_common(p_memory_lint)

    p_memory_compact = memory_sub.add_parser(
        "compact", help="Flatten or tighten oversized MEMORY.md entries into topic cards (dry-run by default)."
    )
    _add_md_common(p_memory_compact)
    p_memory_compact.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    p_memory_compact.add_argument(
        "--commit", action="store_true", help="Commit after --apply when the memory dir is a git repo."
    )
    p_memory_compact.add_argument(
        "--no-commit", action="store_true", help="Never commit even if MEMORY_DOCTOR_COMMIT=1."
    )
    p_memory_compact.add_argument("--commit-author", default=None, help='Commit author "Name <email>".')

    p_memory_init_git = memory_sub.add_parser(
        "init-git", help="Initialize the memory dir as a git repo with one initial commit."
    )
    _add_md_common(p_memory_init_git)

    p_memory.set_defaults(func=dispatch)


def dispatch(args) -> int:
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
        args._brigade_parser.error(f"unknown memory care command: {args.memory_care_command}")
        return 2
    if args.memory_command == "search":
        return memory_cmd.search(target=args.target, query=args.query, limit=args.limit, json_output=args.json)
    if args.memory_command == "serve-mcp":
        return memory_cmd.serve_mcp(target=args.target, stdio=args.stdio, json_output=args.json)

    from .. import memory_doctor_cmd

    if args.memory_command == "status":
        return memory_doctor_cmd.status(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
            target=args.target,
            json_output=args.json,
        )
    if args.memory_command == "lint":
        return memory_doctor_cmd.lint(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
            target=args.target,
        )
    if args.memory_command == "compact":
        return memory_doctor_cmd.compact(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
            target=args.target,
            apply=args.apply,
            commit=args.commit,
            no_commit=args.no_commit,
            commit_author=args.commit_author,
        )
    if args.memory_command == "init-git":
        return memory_doctor_cmd.init_git(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
            target=args.target,
        )
    args._brigade_parser.error(f"unknown memory command: {args.memory_command}")
    return 2
