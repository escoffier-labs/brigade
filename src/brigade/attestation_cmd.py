"""CLI handlers for receipts attestation commands."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from . import attestation, cosign_attestation
from .work_cmd import verification as verify_mod


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def export_attestation(
    *,
    target: Path,
    run_id: str,
    out: str | None = None,
    key: Path | None = None,
    profile: str = "sshsig",
    force: bool = False,
) -> int:
    if profile not in {"sshsig", "cosign"}:
        print(
            f"error: unsupported attestation profile '{profile}' (supported profiles: 'sshsig', 'cosign')",
            file=sys.stderr,
        )
        return 1
    if run_id != "latest" and (not _RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}):
        print(
            "error: run id must be 'latest' or contain only letters, digits, dot, underscore, or hyphen",
            file=sys.stderr,
        )
        return 1
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    receipt_data: dict[str, Any] | None = None
    direct_receipt = target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json"
    if run_id != "latest" and direct_receipt.is_file():
        try:
            data = json.loads(direct_receipt.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("path", str(direct_receipt.parent))
                receipt_data = data
        except Exception:
            pass

    if receipt_data is None:
        resolved, error = verify_mod._resolve_verify_receipt(target, run_id)
        if resolved is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        receipt_data = resolved

    if profile == "cosign":
        key_path = cosign_attestation.resolve_cosign_key_path(target, key_file=key)
        if not key_path.is_file():
            print(
                f"error: cosign private key not found: {key_path} (use --key to select a cosign private key)",
                file=sys.stderr,
            )
            return 1
        default_name = "attestation.sigstore.json"
        try:
            artifact = cosign_attestation.export_attestation(receipt_data, key_path=key_path)
        except attestation.AttestationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: failed to export attestation: {exc}", file=sys.stderr)
            return 1
    elif profile == "sshsig":
        key_path = attestation.resolve_signing_key_path(target, key_file=key)
        if not key_path.is_file():
            print(f"error: signing key not found: {key_path}", file=sys.stderr)
            return 1
        default_name = "attestation.json"
        try:
            artifact = attestation.export_attestation(receipt_data, key_path=key_path)
        except attestation.AttestationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: failed to export attestation: {exc}", file=sys.stderr)
            return 1
    else:
        raise AssertionError(f"unhandled attestation profile: {profile}")

    if out == "-":
        print(json.dumps(artifact, indent=2, sort_keys=True))
        return 0

    if out is not None:
        out_path = Path(out).expanduser().resolve()
    else:
        run_dir_str = receipt_data.get("path")
        if run_dir_str:
            run_dir = Path(run_dir_str)
        else:
            resolved_run_id = str(receipt_data.get("run_id") or run_id)
            run_dir = target / ".brigade" / "work" / "verify-runs" / resolved_run_id
        out_path = run_dir / default_name

    try:
        attestation.write_attestation_file(artifact, out_path, force=force)
    except FileExistsError as exc:
        print(f"error: {exc} (use --force to overwrite)", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: failed to write attestation: {exc}", file=sys.stderr)
        return 1

    return 0


def verify_attestation(
    *,
    attestation_path: Path,
    target: Path | None = None,
    allowed_signers: Path | None = None,
    principal: str | None = None,
    krl_path: Path | None = None,
    json_output: bool = False,
) -> int:
    effective_krl = krl_path
    if effective_krl is None and target is not None:
        default_krl = attestation.default_revoked_keys_path(target)
        if default_krl.is_file():
            effective_krl = default_krl

    result = attestation.verify_attestation(
        attestation_path,
        allowed_signers_path=allowed_signers,
        principal=principal,
        target=target,
        krl_path=effective_krl,
    )
    if json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.status)
    return 0 if result.status == attestation.STATUS_SIGNED_OK else 1


def attestation_keygen(
    *,
    target: Path,
    principal: str | None = None,
    force: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        key_path, signers_path = attestation.keygen(target, principal=principal, force=force)
    except FileExistsError:
        key_path = attestation.default_key_path(target)
        print(f"error: attestation signing key already exists: {key_path}", file=sys.stderr)
        print("hint: leaving the existing key unchanged; pass --force to rotate", file=sys.stderr)
        return 1
    except attestation.AttestationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"attestation signing key: {key_path}")
    print(f"allowed signers: {signers_path}")
    print("reminder: keep the private key gitignored; Brigade's .brigade/ ignore convention covers it.")
    return 0
