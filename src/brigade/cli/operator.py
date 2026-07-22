"""brigade operator command group."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..operator_cmd import CHECKUP_PRESETS, CHECKUP_SURFACE_NAMES


def register(sub: argparse._SubParsersAction) -> None:
    # operator
    p_operator = sub.add_parser("operator", help="Plan and initialize safe local operator config.")
    operator_sub = p_operator.add_subparsers(dest="operator_command", metavar="<operator-command>")
    operator_sub.required = True
    p_operator_guide = operator_sub.add_parser("guide", help="Print the repo-local Brigade operator workflow.")
    p_operator_guide.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="internal-dogfood",
        help="Operator profile to describe.",
    )
    p_operator_guide.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_plan = operator_sub.add_parser("plan", help="Plan local operator config bootstrap.")
    p_operator_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_plan.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="local-operator",
        help="Bootstrap profile to inspect.",
    )
    p_operator_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt = operator_sub.add_parser(
        "adopt", help="Inspect an existing operator workspace before Brigade adoption."
    )
    operator_adopt_sub = p_operator_adopt.add_subparsers(
        dest="operator_adopt_command", metavar="<operator-adopt-command>"
    )
    operator_adopt_sub.required = True
    p_operator_adopt_plan = operator_adopt_sub.add_parser(
        "plan", help="Build a privacy-preserving read-only adoption plan."
    )
    p_operator_adopt_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_adopt_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt_capture = operator_adopt_sub.add_parser(
        "capture", help="Write a redacted local adoption snapshot."
    )
    p_operator_adopt_capture.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_adopt_capture.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_adopt_import = operator_adopt_sub.add_parser(
        "import-issues", help="Import adoption gaps into the work inbox."
    )
    p_operator_adopt_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_adopt_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_adopt_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration = operator_sub.add_parser(
        "migration", help="Summarize operator adoption and external-surface replacement progress."
    )
    operator_migration_sub = p_operator_migration.add_subparsers(
        dest="operator_migration_command", metavar="<operator-migration-command>"
    )
    operator_migration_sub.required = True
    p_operator_migration_status = operator_migration_sub.add_parser(
        "status", help="Show redacted operator migration progress."
    )
    p_operator_migration_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_migration_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_doctor = operator_migration_sub.add_parser(
        "doctor", help="Check whether Brigade can drive operator migration work."
    )
    p_operator_migration_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_migration_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_import = operator_migration_sub.add_parser(
        "import-issues", help="Import operator migration rollup gaps into the work inbox."
    )
    p_operator_migration_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_migration_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_migration_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_migration_consolidate = operator_migration_sub.add_parser(
        "consolidate", help="Dismiss tiny operator-surface-review imports superseded by a migration rollup."
    )
    p_operator_migration_consolidate.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_migration_consolidate.add_argument("--surface", default=None, help="Optional surface id to consolidate.")
    p_operator_migration_consolidate.add_argument(
        "--review-status", default=None, help="Optional review status to consolidate."
    )
    p_operator_migration_consolidate.add_argument(
        "--reason", default="superseded-by-migration-rollup", help="Short safe dismissal reason."
    )
    p_operator_migration_consolidate.add_argument(
        "--dry-run", action="store_true", help="Report without dismissing imports."
    )
    p_operator_migration_consolidate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces = operator_sub.add_parser(
        "surfaces", help="Capture and review redacted external scheduler/process surfaces."
    )
    operator_surfaces_sub = p_operator_surfaces.add_subparsers(
        dest="operator_surfaces_command", metavar="<operator-surfaces-command>"
    )
    operator_surfaces_sub.required = True
    p_operator_surfaces_capture = operator_surfaces_sub.add_parser(
        "capture", help="Write a redacted local operator surface snapshot."
    )
    p_operator_surfaces_capture.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_surfaces_capture.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_list = operator_surfaces_sub.add_parser(
        "list", help="List the latest redacted operator surface records."
    )
    p_operator_surfaces_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_surfaces_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_doctor = operator_surfaces_sub.add_parser(
        "doctor", help="Check redacted operator surface capture freshness."
    )
    p_operator_surfaces_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_surfaces_doctor.add_argument(
        "--surface", default=None, help="Optional surface id to check, such as shell_crontab."
    )
    p_operator_surfaces_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_review = operator_surfaces_sub.add_parser(
        "review", help="Record a redacted operator surface ownership decision."
    )
    p_operator_surfaces_review.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_surfaces_review.add_argument(
        "--surface", required=True, help="Surface id to review, such as shell_crontab."
    )
    p_operator_surfaces_review.add_argument(
        "--status",
        required=True,
        choices=["brigade-runbook-candidate", "external-ok", "needs-owner", "retire-candidate"],
        help="Review decision.",
    )
    p_operator_surfaces_review.add_argument(
        "--all", dest="all_records", action="store_true", help="Review every current record for the surface."
    )
    p_operator_surfaces_review.add_argument(
        "--record",
        dest="record_labels",
        action="append",
        default=[],
        help="Review one redacted record label. Repeat for multiple records.",
    )
    p_operator_surfaces_review.add_argument(
        "--reason", default="operator-review", help="Short safe reason code. Do not include paths or secrets."
    )
    p_operator_surfaces_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_reviews = operator_surfaces_sub.add_parser(
        "reviews", help="Summarize redacted operator surface review state."
    )
    p_operator_surfaces_reviews.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_surfaces_reviews.add_argument("--surface", default=None, help="Optional surface id to list.")
    p_operator_surfaces_reviews.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_surfaces_import = operator_surfaces_sub.add_parser(
        "import-issues", help="Import surface coverage follow-ups into the work inbox."
    )
    p_operator_surfaces_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_surfaces_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_operator_surfaces_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_init = operator_sub.add_parser("init", help="Write missing gitignored local operator config defaults.")
    p_operator_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_operator_init.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="local-operator",
        help="Bootstrap profile to apply.",
    )
    p_operator_init.add_argument("--force", action="store_true", help="Overwrite existing local config files.")
    p_operator_init.add_argument("--dry-run", action="store_true", help="Show planned writes without changing files.")
    p_operator_init.add_argument(
        "--waive-public-release",
        action="store_true",
        help="Write a local waiver for public release readiness when using an internal profile.",
    )
    p_operator_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_status = operator_sub.add_parser("status", help="Show repo and machine wiring for local operator use.")
    p_operator_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_status.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="internal-dogfood",
        help="Operator profile to inspect.",
    )
    p_operator_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_doctor = operator_sub.add_parser("doctor", help="Print a compact local production readiness verdict.")
    p_operator_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_operator_doctor.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="internal-dogfood",
        help="Operator profile to inspect.",
    )
    p_operator_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_checkup = operator_sub.add_parser(
        "checkup", help="Run every read-only first-run doctor at once and roll up the verdict."
    )
    p_operator_checkup.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_operator_checkup.add_argument(
        "--profile",
        choices=["local-operator", "internal-dogfood"],
        default="internal-dogfood",
        help="Operator profile to inspect.",
    )
    p_operator_checkup.add_argument(
        "--surface",
        action="append",
        choices=CHECKUP_SURFACE_NAMES,
        help="Run only this named health surface. Repeat to select more than one.",
    )
    p_operator_checkup.add_argument(
        "--preset",
        choices=tuple(CHECKUP_PRESETS),
        help="Run a named surface preset.",
    )
    p_operator_checkup.add_argument(
        "--list-surfaces",
        action="store_true",
        help="List valid surfaces and presets without running checks.",
    )
    p_operator_checkup.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_verify_harness = operator_sub.add_parser(
        "verify-harness", help="Verify repo-local wiring for one harness."
    )
    p_operator_verify_harness.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
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
            "grok",
            "amp",
            "crush",
            "openclaw",
            "hermes",
        ],
        required=True,
        help="Harness id to verify.",
    )
    p_operator_verify_harness.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_sync_tools = operator_sub.add_parser(
        "sync-tools", help="Project tracked portable tool sources into local harness folders."
    )
    p_operator_sync_tools.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_sync_tools.add_argument(
        "--dry-run", action="store_true", help="Plan projection writes without changing files."
    )
    p_operator_sync_tools.add_argument(
        "--force", action="store_true", help="Overwrite unmanaged or locally edited projection files."
    )
    p_operator_sync_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_sync_mcp = operator_sub.add_parser(
        "sync-mcp", help="Merge the canonical MCP catalog into each tool's native config (dry-run by default)."
    )
    p_operator_sync_mcp.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_sync_mcp.add_argument("--write", action="store_true", help="Write files (otherwise dry-run).")
    p_operator_sync_mcp.add_argument("--force", action="store_true", help="Overwrite servers edited outside Brigade.")
    p_operator_sync_mcp.add_argument("--prune", action="store_true", help="Remove pristine orphans.")
    p_operator_sync_mcp.add_argument(
        "--adopt", action="store_true", help="Take ownership of same-named foreign servers."
    )
    p_operator_sync_mcp.add_argument(
        "--user-scope", action="store_true", help="Include user-scoped targets (e.g. antigravity)."
    )
    p_operator_sync_mcp.add_argument(
        "--allow-global-stdio",
        action="store_true",
        help="Acknowledge writing stdio MCP servers into a user-wide client config.",
    )
    p_operator_sync_mcp.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_quickstart = operator_sub.add_parser(
        "quickstart", help="Prepare a new user workspace with Brigade configs, portable tools, and harness checks."
    )
    p_operator_quickstart.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_quickstart.add_argument(
        "--depth", choices=["repo", "workspace"], default="repo", help="Install depth for Brigade bootstrap files."
    )
    p_operator_quickstart.add_argument("--harnesses", default="codex", help="Comma-separated harness ids, or none.")
    p_operator_quickstart.add_argument("--owner", default=None, help="Override the canonical memory owner.")
    p_operator_quickstart.add_argument(
        "--tool-pack", type=Path, default=None, help="Optional `brigade tools pack build` directory to import."
    )
    p_operator_quickstart.add_argument(
        "--skill-pack", type=Path, default=None, help="Optional `brigade skills pack build` directory to import."
    )
    p_operator_quickstart.add_argument("--dry-run", action="store_true", help="Plan writes without changing files.")
    p_operator_quickstart.add_argument(
        "--force", action="store_true", help="Overwrite existing generated local setup files when supported."
    )
    p_operator_quickstart.add_argument(
        "--full",
        action="store_true",
        help="Repo depth: install the full kit (rules/, hooks/pre-push, INSTALL_FOR_AGENTS.md) and project "
        "the default tool packs into tools/ and scripts/. Default is the minimal footprint.",
    )
    p_operator_quickstart.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator_bootstrap_portable = operator_sub.add_parser(
        "bootstrap-portable", help="Import optional portable packs and sync tools across local harnesses."
    )
    p_operator_bootstrap_portable.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_operator_bootstrap_portable.add_argument(
        "--tool-pack", type=Path, default=None, help="Optional `brigade tools pack build` directory to import first."
    )
    p_operator_bootstrap_portable.add_argument(
        "--skill-pack", type=Path, default=None, help="Optional `brigade skills pack build` directory to import first."
    )
    p_operator_bootstrap_portable.add_argument(
        "--dry-run", action="store_true", help="Plan projection writes without changing files."
    )
    p_operator_bootstrap_portable.add_argument(
        "--force", action="store_true", help="Overwrite conflicting imported entries or projection files."
    )
    p_operator_bootstrap_portable.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_operator.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import operator_cmd

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
        args._brigade_parser.error(f"unknown operator adopt command: {args.operator_adopt_command}")
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
        args._brigade_parser.error(f"unknown operator migration command: {args.operator_migration_command}")
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
        args._brigade_parser.error(f"unknown operator surfaces command: {args.operator_surfaces_command}")
        return 2
    if args.operator_command == "init":
        return operator_cmd.init(
            target=args.target,
            profile=args.profile,
            force=args.force,
            dry_run=args.dry_run,
            waive_public_release=args.waive_public_release,
            json_output=args.json,
        )
    if args.operator_command == "status":
        return operator_cmd.status(target=args.target, profile=args.profile, json_output=args.json)
    if args.operator_command == "doctor":
        return operator_cmd.doctor(target=args.target, profile=args.profile, json_output=args.json)
    if args.operator_command == "checkup":
        kwargs = {"target": args.target, "profile": args.profile, "json_output": args.json}
        if args.surface:
            kwargs["surfaces"] = args.surface
        if args.preset:
            kwargs["preset"] = args.preset
        if args.list_surfaces:
            kwargs["list_surfaces"] = True
        return operator_cmd.checkup(**kwargs)
    if args.operator_command == "verify-harness":
        return operator_cmd.verify_harness(target=args.target, harness=args.harness, json_output=args.json)
    if args.operator_command == "sync-tools":
        return operator_cmd.sync_tools(
            target=args.target, dry_run=args.dry_run, force=args.force, json_output=args.json
        )
    if args.operator_command == "sync-mcp":
        return operator_cmd.sync_mcp(
            target=args.target,
            write=args.write,
            force=args.force,
            prune=args.prune,
            adopt=args.adopt,
            user_scope=args.user_scope,
            allow_global_stdio=args.allow_global_stdio,
            json_output=args.json,
        )
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
            full=args.full,
            json_output=args.json,
        )
    if args.operator_command == "bootstrap-portable":
        return operator_cmd.bootstrap_portable(
            target=args.target,
            tool_pack=args.tool_pack,
            skill_pack=args.skill_pack,
            dry_run=args.dry_run,
            force=args.force,
            json_output=args.json,
        )
    args._brigade_parser.error(f"unknown operator command: {args.operator_command}")
    return 2
