"""brigade security command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
    p_security_template_audit = security_sub.add_parser(
        "template-audit", help="Audit public templates and docs for private values."
    )
    p_security_template_audit.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_security_template_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_fix = security_sub.add_parser("fix", help="Apply safe local security hygiene fixes.")
    p_security_fix.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_security_fix.add_argument("--dry-run", action="store_true", help="Show changes without writing files.")
    p_security_review = security_sub.add_parser("review", help="Review the latest local security evidence bundle.")
    p_security_review.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_review.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_findings = security_sub.add_parser("findings", help="List local security findings.")
    p_security_findings.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review."
    )
    p_security_findings.add_argument(
        "--output-dir", type=Path, default=None, help="Security evidence bundle directory."
    )
    p_security_findings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_diff = security_sub.add_parser(
        "diff", help="Compare two security reports (new/resolved/persisting findings)."
    )
    p_security_diff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for config and the default --against."
    )
    p_security_diff.add_argument(
        "--base", dest="base_dir", type=Path, required=True, help="Baseline security evidence bundle directory."
    )
    p_security_diff.add_argument(
        "--against",
        dest="against_dir",
        type=Path,
        default=None,
        help="Bundle to compare against. Defaults to .brigade/security/latest.",
    )
    p_security_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_sarif = security_sub.add_parser("sarif", help="Write SARIF for an existing security report.")
    p_security_sarif.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to review.")
    p_security_sarif.add_argument("--output-dir", type=Path, default=None, help="Security evidence bundle directory.")
    p_security_sarif.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help="SARIF output path. Defaults to security-report.sarif in the bundle.",
    )
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
    p_security_enrich.add_argument(
        "--provider", choices=["local", "misp"], default=None, help="Override configured provider."
    )
    p_security_enrich.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_security_suppress = security_sub.add_parser("suppress", help="Suppress a reviewed security finding.")
    p_security_suppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to suppress.")
    p_security_suppress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_suppress.add_argument("--reason", required=True, help="Required suppression reason.")
    p_security_unsuppress = security_sub.add_parser("unsuppress", help="Remove a security finding suppression.")
    p_security_unsuppress.add_argument("fingerprint", help="Finding id, id prefix, or fingerprint to unsuppress.")
    p_security_unsuppress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_closeout = security_sub.add_parser("closeout", help="Write local security review closeout metadata.")
    p_security_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_security_closeout.add_argument(
        "--output-dir", type=Path, default=None, help="Security evidence bundle directory."
    )
    p_security_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_security_closeout.add_argument(
        "--accept-risk", action="store_true", help="Mark open findings as locally accepted risk."
    )
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
    p_security.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import security_cmd

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
    if args.security_command == "diff":
        return security_cmd.diff(
            target=args.target,
            base_dir=args.base_dir,
            against_dir=args.against_dir,
            json_output=args.json,
        )
    if args.security_command == "sarif":
        return security_cmd.sarif(
            target=args.target, output_dir=args.output_dir, output_path=args.output_path, json_output=args.json
        )
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
    args._brigade_parser.error(f"unknown security command: {args.security_command}")
    return 2
