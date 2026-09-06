"""Portable evidence package export and independent integrity verification."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

from . import attestation_input, attestation_receipt, localio

SCHEMA = "brigade.evidence_package.v1"
SCHEMA_VERSION = 1
EXPORT_SCHEMA = "brigade.evidence_package_export.v1"
VERIFY_SCHEMA = "brigade.evidence_package_verification.v1"

MAX_ENTRY_BYTES = 64 * 1024 * 1024

PACKAGE_FILES = (
    ("receipt.json", "receipt", "application/json"),
    ("changes.patch", "patch", "text/plain"),
    ("attestation.json", "attestation", "application/json"),
    ("attestation.sigstore.json", "cosign-bundle", "application/vnd.dev.sigstore.bundle.v0.3+json"),
    ("summary.md", "summary", "text/plain"),
)


class EvidencePackageError(Exception):
    """Raised when an evidence package operation cannot complete safely."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_runs_root(target: Path) -> Path:
    return target / ".brigade" / "work" / "verify-runs"


def _out_inside_verify_runs(target: Path, out: Path) -> bool:
    """Return True when out resolves inside the target verify-runs root."""
    try:
        out.resolve().relative_to(_verify_runs_root(target).resolve())
    except ValueError:
        return False
    return True


def _staging_suffix() -> str:
    return secrets.token_hex(4)


def _entry_manifest(path: str, data: bytes, kind: str, media_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": _sha256_bytes(data),
        "bytes": len(data),
        "media_type": media_type,
        "kind": kind,
    }


def _read_source_file(path: Path) -> bytes:
    """Read a bounded source file; refuse non-regular files and oversized data."""
    if not path.is_file():
        raise EvidencePackageError(f"required package source file is not a regular file: {path.name}")
    size = path.stat().st_size
    if size > MAX_ENTRY_BYTES:
        raise EvidencePackageError(f"package source file exceeds 64 MiB limit: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvidencePackageError(f"cannot read package source file: {path.name}") from exc


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync of a directory for durable publication."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.resolve(), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_package(
    *,
    target: Path,
    run_id: str,
    out_str: str,
    force: bool = False,
    json_output: bool = False,
) -> int:
    """Export one verify run as a portable evidence package."""
    target = target.expanduser().resolve()
    out_path = Path(out_str).expanduser().resolve()

    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if _out_inside_verify_runs(target, out_path):
        print("error: --out cannot be inside the verify-runs directory", file=sys.stderr)
        return 1

    try:
        selected = attestation_receipt.load_selected_receipt(target, run_id)
    except attestation_receipt.ReceiptDigestError as exc:
        print(f"error: cannot export evidence package: {exc}", file=sys.stderr)
        return 1
    except attestation_receipt.ReceiptSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    receipt = selected.snapshot.receipt
    verify_run_id = selected.directory.name
    producer_run_id = receipt.get("producer_run_id")
    tree_fingerprint = receipt.get("tree_fingerprint")
    baseline_commit = receipt.get("baseline_commit")
    changes_patch_sha256 = receipt.get("changes_patch_sha256")
    receipt_sha256 = selected.snapshot.digest

    entries: list[dict[str, Any]] = []
    copied: list[tuple[str, bytes]] = []

    for filename, kind, media_type in PACKAGE_FILES:
        src = selected.directory / filename
        if not src.is_file():
            if kind == "receipt":
                print(f"error: required receipt file missing: {src}", file=sys.stderr)
                return 1
            continue
        data = _read_source_file(src)
        entries.append(_entry_manifest(filename, data, kind, media_type))
        copied.append((filename, data))

    entries.sort(key=lambda item: item["path"])
    entries_sha256 = localio.canonical_json_digest(entries)

    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": localio.utc_now_iso_z(),
        "source": {
            "verify_run_id": verify_run_id,
            "producer_run_id": producer_run_id,
            "tree_fingerprint": tree_fingerprint,
            "baseline_commit": baseline_commit,
            "changes_patch_sha256": changes_patch_sha256,
            "receipt_sha256": receipt_sha256,
        },
        "entries": entries,
        "entries_sha256": entries_sha256,
        "limitations": ["receipt-contains-local-paths", "integrity-only"],
    }
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_bytes = manifest_json.encode("utf-8")
    manifest_sha256 = _sha256_bytes(manifest_bytes)

    staging = out_path.parent / f"{out_path.name}.staging-{_staging_suffix()}"
    replaced: Path | None = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        print("error: evidence package staging directory already exists", file=sys.stderr)
        return 1

    try:
        for filename, _data in copied:
            localio.write_bytes_atomic(staging / filename, _data)
        localio.write_bytes_atomic(staging / "manifest.json", manifest_bytes)
        _fsync_directory(staging)

        if out_path.exists():
            if not force:
                print(
                    f"error: evidence package output already exists: {out_path} (use --force to replace)",
                    file=sys.stderr,
                )
                shutil.rmtree(staging, ignore_errors=True)
                return 1
            replaced = out_path.parent / f"{out_path.name}.replaced-{_staging_suffix()}"
            out_path.rename(replaced)

        os.replace(staging, out_path)
    except BaseException:
        try:
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            pass
        if replaced is not None and replaced.exists():
            try:
                replaced.rename(out_path)
            except Exception:
                pass
        raise

    if replaced is not None and replaced.exists():
        try:
            shutil.rmtree(replaced, ignore_errors=True)
        except Exception:
            pass

    if json_output:
        print(
            json.dumps(
                {
                    "schema": EXPORT_SCHEMA,
                    "out": out_str,
                    "verify_run_id": verify_run_id,
                    "entries": len(entries),
                    "manifest_sha256": manifest_sha256,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"evidence package exported: {verify_run_id} ({len(entries)} entries)")
    return 0


def _is_safe_entry_path(path: str) -> bool:
    if not path:
        return False
    if path == "..":
        return False
    if "/" in path or os.sep in path:
        return False
    if any(part == ".." for part in path.split("/")):
        return False
    return True


def verify_package(
    *,
    directory: Path,
    json_output: bool = False,
) -> int:
    """Verify content integrity of a portable evidence package."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        print(f"error: --directory is not a directory: {directory}", file=sys.stderr)
        return 1

    manifest_path = directory / "manifest.json"
    try:
        manifest = attestation_input.read_json_object(manifest_path, max_bytes=attestation_input.MAX_JSON_BYTES)
    except attestation_input.AttestationInputError as exc:
        print(f"error: cannot read manifest: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot read manifest: {exc}", file=sys.stderr)
        return 1

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        print("error: manifest entries is not a list", file=sys.stderr)
        return 1

    entry_paths = set()
    per_entry: list[dict[str, Any]] = []
    all_present = True
    entries_sha256_match = False
    receipt_digest_status = "invalid"

    expected_entries_sha256 = localio.canonical_json_digest(entries)
    stored_entries_sha256 = manifest.get("entries_sha256")
    if isinstance(stored_entries_sha256, str) and stored_entries_sha256 == expected_entries_sha256:
        entries_sha256_match = True

    for entry in entries:
        if not isinstance(entry, dict):
            print("error: manifest entry is not an object", file=sys.stderr)
            return 1
        path = entry.get("path")
        if not isinstance(path, str):
            print("error: manifest entry path is not a string", file=sys.stderr)
            return 1
        size = entry.get("bytes")
        if not isinstance(size, int):
            print("error: manifest entry bytes is not an integer", file=sys.stderr)
            return 1
        expected_sha256 = entry.get("sha256")
        if not isinstance(expected_sha256, str):
            print("error: manifest entry sha256 is not a string", file=sys.stderr)
            return 1

        if not _is_safe_entry_path(path):
            print(f"error: manifest entry path is unsafe: {path}", file=sys.stderr)
            return 1
        if size > MAX_ENTRY_BYTES:
            print(f"error: manifest entry exceeds size limit: {path}", file=sys.stderr)
            return 1

        entry_paths.add(path)
        file_path = directory / path
        if not file_path.exists():
            state = "missing"
        elif not file_path.is_file():
            state = "unreadable"
        else:
            try:
                data = attestation_input.read_bounded_file(file_path, max_bytes=size)
            except attestation_input.AttestationInputError as exc:
                if "exceeds byte limit" in str(exc):
                    state = "mismatch"
                else:
                    state = "unreadable"
            except OSError:
                state = "unreadable"
            else:
                if len(data) != size or _sha256_bytes(data) != expected_sha256:
                    state = "mismatch"
                else:
                    state = "present"

        if state != "present":
            all_present = False
        per_entry.append({"path": path, "state": state})

    # Detect extra regular files not named in the manifest.
    extras: list[str] = []
    try:
        for child in directory.iterdir():
            if child.name == "manifest.json" or child.name in entry_paths:
                continue
            try:
                if child.is_file() and not child.is_symlink():
                    extras.append(child.name)
            except OSError:
                pass
    except OSError as exc:
        print(f"error: cannot read package directory: {exc}", file=sys.stderr)
        return 1

    receipt_json = directory / "receipt.json"
    if not receipt_json.is_file():
        receipt_digest_status = "invalid"
    else:
        try:
            receipt_data = attestation_input.read_json_object(receipt_json, max_bytes=attestation_input.MAX_JSON_BYTES)
            snapshot = attestation_receipt.snapshot_receipt(receipt_data)
        except (attestation_input.AttestationInputError, OSError):
            receipt_digest_status = "invalid"
        except attestation_receipt.ReceiptDigestError:
            receipt_digest_status = "mismatch"
        else:
            stored_receipt_sha256 = manifest.get("source", {}).get("receipt_sha256")
            if isinstance(stored_receipt_sha256, str) and stored_receipt_sha256 == snapshot.digest:
                receipt_digest_status = "match"
            else:
                receipt_digest_status = "mismatch"

    ok = all_present and not extras and entries_sha256_match and receipt_digest_status == "match"

    if json_output:
        result = {
            "schema": VERIFY_SCHEMA,
            "manifest_sha256": manifest_sha256,
            "manifest_entries": "match" if entries_sha256_match else "mismatch",
            "receipt_digest": receipt_digest_status,
            "entries": per_entry,
            "extras": extras,
            "ok": ok,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for row in per_entry:
            print(f"{row['path']}: {row['state']}")
        if extras:
            print(f"extra files: {', '.join(extras)}")
        print(f"manifest entries: {'match' if entries_sha256_match else 'mismatch'}")
        print(f"receipt digest: {receipt_digest_status}")
        if not ok:
            print("verification failed")
        else:
            print("verification ok")

    return 0 if ok else 1
