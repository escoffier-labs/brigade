"""brigade skills command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
    p_skills_lint.add_argument(
        "--mode", choices=["strict", "lenient"], default="strict", help="Agent Skills validation mode."
    )
    p_skills_lint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_doctor = skills_sub.add_parser("doctor", help="Check reviewed skill registry health.")
    p_skills_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_import_issues = skills_sub.add_parser(
        "import-issues", help="Import skill registry issues into the work inbox."
    )
    p_skills_import_issues.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect."
    )
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
    p_skills_diff = skills_sub.add_parser(
        "diff", help="Diff an installed skill against the current rendered registry version."
    )
    p_skills_diff.add_argument("skill", help="Skill id, path, or directory.")
    p_skills_diff.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_diff.add_argument("--harness", required=True, help="Harness target to compare.")
    p_skills_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_uninstall = skills_sub.add_parser("uninstall", help="Remove an installed skill from one or all harnesses.")
    p_skills_uninstall.add_argument("skill", help="Installed skill id.")
    p_skills_uninstall.add_argument("--workspace", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_uninstall.add_argument("--target", dest="install_target", required=True, help="Harness target or all.")
    p_skills_uninstall.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_rollback = skills_sub.add_parser(
        "rollback", help="Rollback one installed skill target to the latest snapshot."
    )
    p_skills_rollback.add_argument("skill", help="Skill id.")
    p_skills_rollback.add_argument("--workspace", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_rollback.add_argument("--target", dest="install_target", required=True, help="Harness target to rollback.")
    p_skills_rollback.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_serve = skills_sub.add_parser("serve-mcp", help="Report the local MCP skills resource contract.")
    p_skills_serve.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect.")
    p_skills_serve.add_argument(
        "--stdio", action="store_true", help="Serve the read-only skills MCP adapter over stdio."
    )
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
    p_skills_pack_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect."
    )
    p_skills_pack_list.add_argument("--limit", type=int, default=20, help="Maximum packs to show.")
    p_skills_pack_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_show = skills_pack_sub.add_parser("show", help="Show one local portable skill pack.")
    p_skills_pack_show.add_argument("pack_id", help="Pack id, id prefix, latest, or path.")
    p_skills_pack_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to inspect."
    )
    p_skills_pack_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_import = skills_pack_sub.add_parser("import", help="Import skills from a portable skill pack.")
    p_skills_pack_import.add_argument("pack", type=Path, help="Skill pack directory containing skill-pack.json.")
    p_skills_pack_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to update."
    )
    p_skills_pack_import.add_argument("--force", action="store_true", help="Overwrite existing registry skills.")
    p_skills_pack_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_pack_archive = skills_pack_sub.add_parser("archive", help="Archive one local portable skill pack.")
    p_skills_pack_archive.add_argument("pack_id", help="Pack id, id prefix, or latest.")
    p_skills_pack_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to update."
    )
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
    p_skills_inbox_diff = skills_inbox_sub.add_parser(
        "diff", help="Diff one skill proposal against the current registry entry."
    )
    p_skills_inbox_diff.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_diff.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace inbox to inspect.")
    p_skills_inbox_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_accept = skills_inbox_sub.add_parser("accept", help="Accept one skill proposal into the registry.")
    p_skills_inbox_accept.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_accept.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace registry to update."
    )
    p_skills_inbox_accept.add_argument("--force", action="store_true", help="Overwrite an existing registry skill.")
    p_skills_inbox_accept.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_inbox_reject = skills_inbox_sub.add_parser("reject", help="Reject one skill proposal.")
    p_skills_inbox_reject.add_argument("proposal_id", help="Proposal id or unique prefix.")
    p_skills_inbox_reject.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Workspace inbox to update."
    )
    p_skills_inbox_reject.add_argument("--reason", required=True, help="Review reason.")
    p_skills_inbox_reject.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters = skills_sub.add_parser("adapters", help="Inspect skill harness adapters.")
    skills_adapters_sub = p_skills_adapters.add_subparsers(
        dest="skills_adapters_command", metavar="<skills-adapters-command>"
    )
    skills_adapters_sub.required = True
    p_skills_adapters_init = skills_adapters_sub.add_parser("init", help="Write local skill adapter overlay config.")
    p_skills_adapters_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to update.")
    p_skills_adapters_init.add_argument("--force", action="store_true", help="Overwrite an existing adapter config.")
    p_skills_adapters_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters_list = skills_adapters_sub.add_parser("list", help="List skill harness adapters.")
    p_skills_adapters_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to inspect.")
    p_skills_adapters_list.add_argument(
        "--include-planned", action="store_true", help="Include planned future adapter targets."
    )
    p_skills_adapters_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills_adapters_show = skills_adapters_sub.add_parser("show", help="Show one skill harness adapter.")
    p_skills_adapters_show.add_argument("adapter_id", help="Adapter id.")
    p_skills_adapters_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to inspect.")
    p_skills_adapters_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_skills.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import skills_cmd

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
        return skills_cmd.lint(
            target=args.target, skill=args.skill, harness=args.harness, mode=args.mode, json_output=args.json
        )
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
    if args.skills_command == "uninstall":
        return skills_cmd.uninstall(
            workspace=args.workspace,
            skill=args.skill,
            harness=args.install_target,
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
        return skills_cmd.rollback(
            workspace=args.workspace, skill=args.skill, harness=args.install_target, json_output=args.json
        )
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
        args._brigade_parser.error(f"unknown skills pack command: {args.skills_pack_command}")
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
            return skills_cmd.inbox_accept(
                target=args.target, proposal_id=args.proposal_id, force=args.force, json_output=args.json
            )
        if args.skills_inbox_command == "reject":
            return skills_cmd.inbox_reject(
                target=args.target, proposal_id=args.proposal_id, reason=args.reason, json_output=args.json
            )
        args._brigade_parser.error(f"unknown skills inbox command: {args.skills_inbox_command}")
        return 2
    if args.skills_command == "adapters":
        if args.skills_adapters_command == "init":
            return skills_cmd.adapters_init(target=args.target, force=args.force, json_output=args.json)
        if args.skills_adapters_command == "list":
            return skills_cmd.adapters_list(
                target=args.target, include_planned=args.include_planned, json_output=args.json
            )
        if args.skills_adapters_command == "show":
            return skills_cmd.adapters_show(target=args.target, adapter_id=args.adapter_id, json_output=args.json)
        args._brigade_parser.error(f"unknown skills adapters command: {args.skills_adapters_command}")
        return 2
    args._brigade_parser.error(f"unknown skills command: {args.skills_command}")
    return 2
