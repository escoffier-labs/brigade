"""brigade handoff command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # handoff
    p_handoff = sub.add_parser("handoff", help="Inspect memory handoff inbox health.")
    handoff_sub = p_handoff.add_subparsers(dest="handoff_command", metavar="<handoff-command>")
    handoff_sub.required = True
    p_handoff_sources = handoff_sub.add_parser("sources", help="Manage local handoff source coverage.")
    handoff_sources_sub = p_handoff_sources.add_subparsers(
        dest="handoff_sources_command", metavar="<handoff-sources-command>"
    )
    handoff_sources_sub.required = True
    p_handoff_sources_init = handoff_sources_sub.add_parser(
        "init", help="Write local handoff source coverage for Claude, Codex, OpenCode, and Hermes."
    )
    p_handoff_sources_init.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_handoff_sources_init.add_argument("--force", action="store_true", help="Overwrite an existing source config.")
    p_handoff_sources_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_doctor = handoff_sub.add_parser("doctor", help="Check handoff inboxes against local source config.")
    p_handoff_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_doctor.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_migrate = handoff_sub.add_parser(
        "migrate", help="Convert near-miss homegrown handoff notes into the Brigade template (dry-run by default)."
    )
    p_handoff_migrate.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_migrate.add_argument("--inbox", default=None, help="Limit to one writer inbox (harness id or path).")
    p_handoff_migrate.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite convertible notes, preserving originals under migrated-originals/.",
    )
    p_handoff_migrate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_lint = handoff_sub.add_parser("lint", help="Validate pending or explicit memory handoff files.")
    p_handoff_lint.add_argument(
        "paths", nargs="*", type=Path, help="Handoff files to validate. Defaults to pending inbox files."
    )
    p_handoff_lint.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_lint.add_argument(
        "--content-guard",
        action="store_true",
        help="Run content-guard leak scan plus handoff injection heuristics (secrets/PII and instruction-shaped payloads).",
    )
    p_handoff_lint.add_argument(
        "--guard-policy", default="personal", help="Content Guard policy name or path for --content-guard."
    )
    p_handoff_lint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_draft = handoff_sub.add_parser("draft", help="Write a linted Memory Handoff draft in Brigade style.")
    p_handoff_draft.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_draft.add_argument(
        "--inbox",
        default="codex",
        help="Writer inbox path or alias: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, grok, amp, crush, hermes.",
    )
    p_handoff_draft.add_argument(
        "--type", default="workflow", help="Handoff type, such as workflow, decision, setup, or bugfix."
    )
    p_handoff_draft.add_argument("--title", required=True, help="Short handoff title.")
    p_handoff_draft.add_argument("--summary", required=True, help="Short handoff summary.")
    p_handoff_draft.add_argument("--fact", action="append", default=[], help="Durable fact bullet. May be repeated.")
    p_handoff_draft.add_argument("--evidence", action="append", default=[], help="Evidence bullet. May be repeated.")
    p_handoff_draft.add_argument(
        "--action",
        choices=["create-card", "update-card", "no-card"],
        default="no-card",
        help="Recommended memory action.",
    )
    p_handoff_draft.add_argument("--target-card", default=None, help="Target card filename for card handoffs.")
    p_handoff_draft.add_argument(
        "--target-document", default=".learnings/LEARNINGS.md", help="Target document for no-card handoffs."
    )
    p_handoff_draft.add_argument(
        "--content",
        default=None,
        help="Suggested card or document content. One of --content/--content-file is required.",
    )
    p_handoff_draft.add_argument(
        "--content-file",
        type=Path,
        default=None,
        help="Read suggested content from a file. One of --content/--content-file is required.",
    )
    p_handoff_draft.add_argument("--id", dest="draft_id", default=None, help="Stable id slug to use in the filename.")
    p_handoff_draft.add_argument("--force", action="store_true", help="Overwrite an existing generated draft path.")
    p_handoff_draft.add_argument(
        "--guard", action="store_true", help="Scan the generated draft with content-guard before returning success."
    )
    p_handoff_draft.add_argument(
        "--guard-policy", default="personal", help="Content Guard policy name or path for --guard."
    )
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
    p_handoff_closeout.add_argument(
        "draft_id", nargs="?", help="Draft id, filename, path, or unique prefix. Defaults to all pending drafts."
    )
    p_handoff_closeout.add_argument("--all", action="store_true", help="Close out all non-archived drafts.")
    p_handoff_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_handoff_closeout.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_handoff_closeout.add_argument(
        "--defer", action="store_true", help="Mark selected drafts deferred instead of reviewed."
    )
    p_handoff_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_runs = handoff_sub.add_parser("runs", help="List local handoff ingestion receipts.")
    p_handoff_runs.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to show.")
    p_handoff_runs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_run_show = handoff_sub.add_parser("run-show", help="Show one local handoff ingestion receipt.")
    p_handoff_run_show.add_argument("run_id", help="Run id or unique prefix.")
    p_handoff_run_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_handoff_run_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_receipt = handoff_sub.add_parser("receipt", help="Plan or record external handoff ingestion receipts.")
    handoff_receipt_sub = p_handoff_receipt.add_subparsers(
        dest="handoff_receipt_command", metavar="<handoff-receipt-command>"
    )
    handoff_receipt_sub.required = True
    p_handoff_receipt_plan = handoff_receipt_sub.add_parser(
        "plan", help="Preview a normalized external handoff ingest receipt."
    )
    p_handoff_receipt_plan.add_argument("draft_ids", nargs="*", help="Draft ids, filenames, paths, or unique prefixes.")
    p_handoff_receipt_plan.add_argument(
        "--all-reviewed", action="store_true", help="Include all lint-valid reviewed drafts."
    )
    p_handoff_receipt_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_handoff_receipt_plan.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_receipt_plan.add_argument(
        "--status",
        choices=["ingested", "skipped", "failed"],
        default="ingested",
        help="Outcome to record for selected drafts.",
    )
    p_handoff_receipt_plan.add_argument(
        "--owner", default="external", help="Safe memory owner label, such as openclaw or hermes."
    )
    p_handoff_receipt_plan.add_argument("--run-id", default=None, help="Explicit receipt run id.")
    p_handoff_receipt_plan.add_argument("--safe-summary", default=None, help="Safe receipt summary.")
    p_handoff_receipt_plan.add_argument("--log-path", default=None, help="Optional local log path label.")
    p_handoff_receipt_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_receipt_record = handoff_receipt_sub.add_parser(
        "record", help="Write a normalized external handoff ingest receipt."
    )
    p_handoff_receipt_record.add_argument(
        "draft_ids", nargs="*", help="Draft ids, filenames, paths, or unique prefixes."
    )
    p_handoff_receipt_record.add_argument(
        "--all-reviewed", action="store_true", help="Include all lint-valid reviewed drafts."
    )
    p_handoff_receipt_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_handoff_receipt_record.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_receipt_record.add_argument(
        "--status",
        choices=["ingested", "skipped", "failed"],
        default="ingested",
        help="Outcome to record for selected drafts.",
    )
    p_handoff_receipt_record.add_argument(
        "--owner", default="external", help="Safe memory owner label, such as openclaw or hermes."
    )
    p_handoff_receipt_record.add_argument("--run-id", default=None, help="Explicit receipt run id.")
    p_handoff_receipt_record.add_argument("--safe-summary", default=None, help="Safe receipt summary.")
    p_handoff_receipt_record.add_argument("--log-path", default=None, help="Optional local log path label.")
    p_handoff_receipt_record.add_argument(
        "--force", action="store_true", help="Overwrite an existing receipt with the same run id."
    )
    p_handoff_receipt_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_reconcile = handoff_sub.add_parser(
        "reconcile", help="Normalize the configured handoff ingestor latest-run log."
    )
    p_handoff_reconcile.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_handoff_reconcile.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_issues = handoff_sub.add_parser("issues", help="Group actionable handoff ingest issues.")
    p_handoff_issues.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_handoff_issues.add_argument("--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json.")
    p_handoff_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_issues.add_argument("--limit", type=int, default=20, help="Maximum issue rows to print.")
    p_handoff_issues.add_argument(
        "--category", action="append", default=[], help="Limit to one issue category. May be repeated."
    )
    p_handoff_import_issues = handoff_sub.add_parser(
        "import-issues", help="Import handoff ingest issues into the work inbox."
    )
    p_handoff_import_issues.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_handoff_import_issues.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_import_issues.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_handoff_import_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_import_issues.add_argument(
        "--category", action="append", default=[], help="Import only one issue category. May be repeated."
    )
    p_handoff_sync_issues = handoff_sub.add_parser(
        "sync-issues", help="Import current handoff issues and close stale local handoff work."
    )
    p_handoff_sync_issues.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_handoff_sync_issues.add_argument(
        "--sources", type=Path, default=None, help="Override .brigade/handoff-sources.json."
    )
    p_handoff_sync_issues.add_argument(
        "--dry-run", action="store_true", help="Report without writing imports or closing stale items."
    )
    p_handoff_sync_issues.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_handoff_sync_issues.add_argument(
        "--category", action="append", default=[], help="Sync only one issue category. May be repeated."
    )
    p_handoff_sync_issues.add_argument(
        "--no-close-stale", action="store_true", help="Do not dismiss stale imports or close stale tasks."
    )
    p_handoff.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import handoff_cmd

    if args.handoff_command == "sources":
        if args.handoff_sources_command == "init":
            return handoff_cmd.sources_init(target=args.target, force=args.force, json_output=args.json)
        args._brigade_parser.error(f"unknown handoff sources command: {args.handoff_sources_command}")
        return 2
    if args.handoff_command == "doctor":
        return handoff_cmd.doctor(target=args.target, sources=args.sources, json_output=args.json)
    if args.handoff_command == "migrate":
        return handoff_cmd.migrate(target=args.target, inbox=args.inbox, apply=args.apply, json_output=args.json)
    if args.handoff_command == "lint":
        return handoff_cmd.lint(
            target=args.target,
            paths=args.paths,
            content_guard=args.content_guard,
            guard_policy=args.guard_policy,
            json_output=args.json,
        )
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
        args._brigade_parser.error(f"unknown handoff receipt command: {args.handoff_receipt_command}")
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
    args._brigade_parser.error(f"unknown handoff command: {args.handoff_command}")
    return 2
