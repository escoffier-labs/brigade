"""brigade receipts command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_receipts = sub.add_parser("receipts", help="Verify local Brigade receipt digests.")
    receipts_sub = p_receipts.add_subparsers(dest="receipts_command", metavar="<receipts-command>")
    receipts_sub.required = True

    p_verify = receipts_sub.add_parser("verify", help="Verify receipt and outcome digest chains.")
    p_verify.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_verify.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_verify.set_defaults(func=dispatch)

    p_keygen = receipts_sub.add_parser("keygen", help="Generate a local receipt signing key.")
    p_keygen.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_keygen.add_argument("--force", action="store_true", help="Overwrite an existing receipt signing key.")
    p_keygen.set_defaults(func=dispatch)

    p_export = receipts_sub.add_parser("export", help="Export receipts for external ingest.")
    export_sub = p_export.add_subparsers(dest="receipts_export_command", metavar="<export-target>")
    export_sub.required = True
    p_miseledger = export_sub.add_parser("miseledger", help="Export receipts as miseledger.adapter.v1 JSONL.")
    p_miseledger.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_miseledger.add_argument("--out", default="-", help="Output path, or '-' for stdout.")
    p_miseledger.add_argument("--limit", type=int, default=0, help="Maximum records to export; 0 means all.")
    p_miseledger.add_argument("--new-only", action="store_true", help="Export only receipt items not in the cursor.")
    p_miseledger.add_argument(
        "--import", dest="import_miseledger", action="store_true", help="Import the JSONL with miseledger."
    )
    p_miseledger.add_argument(
        "--json", action="store_true", help="Print a brigade.miseledger_export_result.v1 summary to stdout."
    )
    p_miseledger.add_argument(
        "--fleet",
        action="store_true",
        help="Export from enabled [[repo]] entries in the target fleet config instead of the target itself.",
    )
    p_miseledger.set_defaults(func=dispatch)
    p_attestation = export_sub.add_parser(
        "attestation", help="Export verify receipt as an SSH-signed in-toto attestation."
    )
    p_attestation.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_attestation.add_argument("--run-id", metavar="<id|latest>", required=True, help="Verify run id or 'latest'.")
    p_attestation.add_argument("--out", metavar="PATH|-", default=None, help="Output path, or '-' for stdout.")
    p_attestation.add_argument("--key", metavar="PATH", type=Path, default=None, help="Path to SSH private key.")
    p_attestation.add_argument("--force", action="store_true", help="Overwrite an existing attestation file.")
    p_attestation.set_defaults(func=dispatch)
    for projection in ("otel-genai", "openinference"):
        parser = export_sub.add_parser(projection, help=f"Export privacy-safe {projection} span JSONL.")
        parser.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        parser.add_argument("--out", default="-", help="Output path, or '-' for stdout.")
        parser.set_defaults(func=dispatch)

    p_verify_att = receipts_sub.add_parser(
        "verify-attestation", help="Verify an SSH-signed in-toto attestation envelope."
    )
    p_verify_att.add_argument(
        "attestation_file", metavar="<attestation.json>", type=Path, help="Path to attestation JSON file."
    )
    p_verify_att.add_argument(
        "--target", "-t", type=Path, default=None, help="Repo or workspace to cross-check receipt against."
    )
    p_verify_att.add_argument(
        "--allowed-signers", metavar="PATH", type=Path, default=None, help="Path to OpenSSH allowed_signers file."
    )
    p_verify_att.add_argument("--principal", metavar="NAME", default=None, help="Expected signer principal name.")
    p_verify_att.add_argument(
        "--revoked-keys", metavar="PATH", type=Path, default=None, help="Path to OpenSSH key revocation list (KRL)."
    )
    p_verify_att.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_verify_att.set_defaults(func=dispatch)

    p_att_keygen = receipts_sub.add_parser("attestation-keygen", help="Generate an Ed25519 attestation signing key.")
    p_att_keygen.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_att_keygen.add_argument("--principal", metavar="NAME", default=None, help="Signer principal name.")
    p_att_keygen.add_argument("--force", action="store_true", help="Overwrite an existing attestation signing key.")
    p_att_keygen.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import receipts_cmd

    if args.receipts_command == "verify":
        from .. import approval

        return approval.verify_receipts_with_approvals(target=args.target, json_output=args.json)
    if args.receipts_command == "keygen":
        return receipts_cmd.keygen(target=args.target, force=args.force)
    if args.receipts_command == "export" and args.receipts_export_command == "attestation":
        from .. import attestation_cmd

        return attestation_cmd.export_attestation(
            target=args.target,
            run_id=args.run_id,
            out=args.out,
            key=args.key,
            force=args.force,
        )
    if args.receipts_command == "export" and args.receipts_export_command == "miseledger":
        return receipts_cmd.export_miseledger(
            target=args.target,
            out=args.out,
            limit=args.limit,
            new_only=args.new_only,
            import_miseledger=args.import_miseledger,
            json_output=args.json,
            fleet=args.fleet,
        )
    if args.receipts_command == "export" and args.receipts_export_command in {"otel-genai", "openinference"}:
        from .. import telemetry_export

        return telemetry_export.export(
            target=args.target,
            projection=args.receipts_export_command,
            out=args.out,
        )
    if args.receipts_command == "verify-attestation":
        from .. import attestation_cmd

        return attestation_cmd.verify_attestation(
            attestation_path=args.attestation_file,
            target=args.target,
            allowed_signers=args.allowed_signers,
            principal=args.principal,
            krl_path=args.revoked_keys,
            json_output=args.json,
        )
    if args.receipts_command == "attestation-keygen":
        from .. import attestation_cmd

        return attestation_cmd.attestation_keygen(
            target=args.target,
            principal=args.principal,
            force=args.force,
        )
    args._brigade_parser.error(f"unknown receipts command: {args.receipts_command}")
    return 2
