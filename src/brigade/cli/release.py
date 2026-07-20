"""brigade release command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    from .. import release_cmd

    # release
    p_release = sub.add_parser("release", help="Inspect local release readiness.")
    release_sub = p_release.add_subparsers(dest="release_command", metavar="<release-command>")
    release_sub.required = True
    p_release_plan = release_sub.add_parser("plan", help="Plan release readiness without writing a receipt.")
    p_release_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_plan.add_argument(
        "--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks."
    )
    p_release_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_doctor = release_sub.add_parser(
        "doctor", help="Run local release readiness checks without writing a receipt."
    )
    p_release_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_release_doctor.add_argument(
        "--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks."
    )
    p_release_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_run = release_sub.add_parser("run", help="Run local release readiness checks and write a receipt.")
    p_release_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_release_run.add_argument(
        "--base-ref", default="origin/main", help="Base ref for introduced-content and docs checks."
    )
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
    p_release_version_sync = release_sub.add_parser(
        "version-sync", help="Check or fix in-tree version stamps against the source of truth."
    )
    p_release_version_sync.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    vs_mode = p_release_version_sync.add_mutually_exclusive_group()
    vs_mode.add_argument("--check", action="store_true", help="Verify stamps match the source (default).")
    vs_mode.add_argument("--write", action="store_true", help="Rewrite stamps to the source version.")
    p_release_version_sync.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_ci = release_sub.add_parser("ci", help="Inspect local CI platform deprecation evidence.")
    release_ci_sub = p_release_ci.add_subparsers(dest="release_ci_command", metavar="<release-ci-command>")
    release_ci_sub.required = True
    p_release_ci_doctor = release_ci_sub.add_parser(
        "doctor", help="Check local GitHub Actions platform deprecation evidence."
    )
    p_release_ci_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_ci_doctor.add_argument(
        "--summary-path", type=Path, default=None, help="Optional local GitHub Actions summary or log file."
    )
    p_release_ci_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_ci_import = release_ci_sub.add_parser(
        "import-issues", help="Import CI platform deprecation findings into the local work inbox."
    )
    p_release_ci_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_ci_import.add_argument(
        "--summary-path", type=Path, default=None, help="Optional local GitHub Actions summary or log file."
    )
    p_release_ci_import.add_argument("--dry-run", action="store_true", help="Validate without writing imports.")
    p_release_ci_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke = release_sub.add_parser("smoke", help="Record and inspect local install smoke matrix receipts.")
    release_smoke_sub = p_release_smoke.add_subparsers(dest="release_smoke_command", metavar="<release-smoke-command>")
    release_smoke_sub.required = True
    p_release_smoke_plan = release_smoke_sub.add_parser("plan", help="Show the supported install smoke matrix.")
    p_release_smoke_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_smoke_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_record = release_smoke_sub.add_parser("record", help="Record one install smoke result.")
    p_release_smoke_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_smoke_record.add_argument("--depth", choices=["repo", "workspace"], default="repo", help="Install depth.")
    p_release_smoke_record.add_argument("--harnesses", default="none", help="Comma-separated harnesses or none.")
    p_release_smoke_record.add_argument(
        "--status", choices=sorted(release_cmd.INSTALL_SMOKE_STATUSES), default="passed", help="Smoke result status."
    )
    p_release_smoke_record.add_argument("--command-label", default=None, help="Safe command label.")
    p_release_smoke_record.add_argument("--summary", default=None, help="Safe result summary.")
    p_release_smoke_record.add_argument(
        "--receipt-json", type=Path, default=None, help="Parse an existing local smoke receipt JSON file."
    )
    p_release_smoke_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_list = release_smoke_sub.add_parser("list", help="List install smoke receipts.")
    p_release_smoke_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_smoke_list.add_argument("--limit", type=int, default=20, help="Maximum receipts to list.")
    p_release_smoke_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_show = release_smoke_sub.add_parser("show", help="Show one install smoke receipt.")
    p_release_smoke_show.add_argument("receipt_id", help="Receipt id, unique prefix, or latest.")
    p_release_smoke_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_smoke_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_smoke_doctor = release_smoke_sub.add_parser("doctor", help="Check install smoke matrix health.")
    p_release_smoke_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_smoke_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate = release_sub.add_parser("candidate", help="Build and inspect local release candidate bundles.")
    release_candidate_sub = p_release_candidate.add_subparsers(
        dest="release_candidate_command", metavar="<candidate-command>"
    )
    release_candidate_sub.required = True
    p_release_candidate_plan = release_candidate_sub.add_parser(
        "plan", help="Plan a release candidate bundle without writing it."
    )
    p_release_candidate_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_candidate_plan.add_argument(
        "--base-ref", default="origin/main", help="Base ref for changed files and release notes."
    )
    p_release_candidate_plan.add_argument(
        "--guard-policy",
        help="Bundled guard policy name or path used to sanitize commit messages (default: public-repo).",
    )
    p_release_candidate_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_build = release_candidate_sub.add_parser(
        "build", help="Build a local release candidate bundle."
    )
    p_release_candidate_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_candidate_build.add_argument(
        "--base-ref", default="origin/main", help="Base ref for changed files and release notes."
    )
    p_release_candidate_build.add_argument(
        "--guard-policy",
        help="Bundled guard policy name or path used to sanitize commit messages (default: public-repo).",
    )
    p_release_candidate_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_list = release_candidate_sub.add_parser("list", help="List local release candidate bundles.")
    p_release_candidate_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_candidate_list.add_argument("--limit", type=int, default=20, help="Maximum candidates to list.")
    p_release_candidate_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_show = release_candidate_sub.add_parser("show", help="Show one local release candidate bundle.")
    p_release_candidate_show.add_argument("candidate_id", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_candidate_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_archive = release_candidate_sub.add_parser(
        "archive", help="Archive one local release candidate bundle."
    )
    p_release_candidate_archive.add_argument("candidate_id", help="Candidate id, unique prefix, or latest.")
    p_release_candidate_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_candidate_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_audit = release_candidate_sub.add_parser(
        "audit", help="Audit one local release candidate bundle."
    )
    p_release_candidate_audit.add_argument(
        "candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest."
    )
    p_release_candidate_audit.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_candidate_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_import = release_candidate_sub.add_parser(
        "import-issues", help="Import release candidate audit issues."
    )
    p_release_candidate_import.add_argument(
        "candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest."
    )
    p_release_candidate_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_candidate_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_release_candidate_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_compare = release_candidate_sub.add_parser(
        "compare", help="Compare a candidate against current local state."
    )
    p_release_candidate_compare.add_argument(
        "candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest."
    )
    p_release_candidate_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_release_candidate_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release_candidate_closeout = release_candidate_sub.add_parser(
        "closeout", help="Mark a local release candidate review state."
    )
    p_release_candidate_closeout.add_argument(
        "candidate_id", nargs="?", default="latest", help="Candidate id, unique prefix, or latest."
    )
    p_release_candidate_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_release_candidate_closeout.add_argument(
        "--status", choices=["draft", "reviewed", "superseded", "archived"], default="reviewed"
    )
    p_release_candidate_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_release_candidate_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_release.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import release_cmd

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
    if args.release_command == "version-sync":
        from .. import release_version_sync

        return release_version_sync.version_sync(target=args.target, write=args.write, json_output=args.json)
    if args.release_command == "ci":
        if args.release_ci_command == "doctor":
            return release_cmd.ci_doctor(target=args.target, summary_path=args.summary_path, json_output=args.json)
        if args.release_ci_command == "import-issues":
            return release_cmd.ci_import_issues(
                target=args.target, summary_path=args.summary_path, dry_run=args.dry_run, json_output=args.json
            )
        args._brigade_parser.error(f"unknown release ci command: {args.release_ci_command}")
        return 2
    if args.release_command == "smoke":
        if args.release_smoke_command == "plan":
            return release_cmd.install_smoke_plan(target=args.target, json_output=args.json)
        if args.release_smoke_command == "record":
            return release_cmd.install_smoke_record(
                target=args.target,
                depth=args.depth,
                harnesses=args.harnesses,
                status=args.status,
                command_label=args.command_label,
                summary=args.summary,
                receipt_json=args.receipt_json,
                json_output=args.json,
            )
        if args.release_smoke_command == "list":
            return release_cmd.install_smoke_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.release_smoke_command == "show":
            return release_cmd.install_smoke_show(target=args.target, receipt_id=args.receipt_id, json_output=args.json)
        if args.release_smoke_command == "doctor":
            return release_cmd.install_smoke_doctor(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown release smoke command: {args.release_smoke_command}")
        return 2
    if args.release_command == "candidate":
        if args.release_candidate_command == "plan":
            return release_cmd.candidate_plan(
                target=args.target,
                base_ref=args.base_ref,
                guard_policy=args.guard_policy,
                json_output=args.json,
            )
        if args.release_candidate_command == "build":
            return release_cmd.candidate_build(
                target=args.target,
                base_ref=args.base_ref,
                guard_policy=args.guard_policy,
                json_output=args.json,
            )
        if args.release_candidate_command == "list":
            return release_cmd.candidate_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.release_candidate_command == "show":
            return release_cmd.candidate_show(target=args.target, candidate_id=args.candidate_id, json_output=args.json)
        if args.release_candidate_command == "archive":
            return release_cmd.candidate_archive(
                target=args.target, candidate_id=args.candidate_id, json_output=args.json
            )
        if args.release_candidate_command == "audit":
            return release_cmd.candidate_audit(
                target=args.target, candidate_id=args.candidate_id, json_output=args.json
            )
        if args.release_candidate_command == "import-issues":
            return release_cmd.candidate_import_issues(
                target=args.target,
                candidate_id=args.candidate_id,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.release_candidate_command == "compare":
            return release_cmd.candidate_compare(
                target=args.target, candidate_id=args.candidate_id, json_output=args.json
            )
        if args.release_candidate_command == "closeout":
            return release_cmd.candidate_closeout(
                target=args.target,
                candidate_id=args.candidate_id,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown release candidate command: {args.release_candidate_command}")
        return 2
    args._brigade_parser.error(f"unknown release command: {args.release_command}")
    return 2
