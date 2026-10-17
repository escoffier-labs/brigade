"""Local export of minimized derivatives from portable evidence packages."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    attestation_input,
    attestation_receipt,
    dirfd,
    evidence_package,
    evidence_package_snapshot,
    evidence_redaction,
    localio,
)

SCHEMA = "brigade.evidence_package_derivative.v1"
SCHEMA_VERSION = 1
POLICY_VERSION = "brigade.evidence-derivative.minimum.v1"
_RECEIPT_FIELDS = frozenset(
    {
        "run_id",
        "producer_run_id",
        "schema_version",
        "status",
        "started_at",
        "completed_at",
        "baseline_commit",
        "tree_fingerprint",
        "changes_patch_sha256",
        "exit_code",
    }
)
_SUPPORTED = frozenset({"manifest.json", *(name for name, _, _ in evidence_package.PACKAGE_FILES)})
_MAX_SOURCE_INVENTORY_ENTRIES = 4096
_RECEIPT_STATUSES = frozenset({"running", "completed", "failed", "rejected", "canceled"})


class DerivativeExportError(ValueError):
    """The source cannot safely produce a minimized derivative."""


@dataclass(frozen=True)
class DerivativeReport:
    removed_fields: int
    redaction_occurrences: int
    excluded_artifacts: int
    derivative_sha256: str


def _file_observation(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _source_inventory(root_fd: int) -> tuple[str, ...]:
    try:
        names = dirfd.child_names(root_fd, _MAX_SOURCE_INVENTORY_ENTRIES + 1)
    except (OSError, NotImplementedError) as exc:
        raise DerivativeExportError("source package inventory could not be read safely") from exc
    if len(names) > _MAX_SOURCE_INVENTORY_ENTRIES:
        raise DerivativeExportError("source package inventory exceeds entry limit")
    return tuple(sorted(names))


def _verify_source_inventory(
    source: Path,
    names: tuple[str, ...],
    observation: tuple[int, int, int, int, int],
    file_observations: dict[str, tuple[int, int, int, int, int]],
) -> None:
    descriptor = -1
    try:
        descriptor = dirfd.open_directory_nofollow(source, writable=False)
        before = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode) or _file_observation(before) != observation:
            raise DerivativeExportError("source package inventory changed while reading")
        if _source_inventory(descriptor) != names:
            raise DerivativeExportError("source package inventory changed while reading")
        for name, expected in file_observations.items():
            current = dirfd.stat_child(descriptor, name)
            if not stat.S_ISREG(current.st_mode) or _file_observation(current) != expected:
                raise DerivativeExportError(f"source artifact changed while reading: {name}")
        if _file_observation(os.fstat(descriptor)) != observation:
            raise DerivativeExportError("source package inventory changed while reading")
    except DerivativeExportError:
        raise
    except (OSError, NotImplementedError, AttributeError) as exc:
        raise DerivativeExportError("source package inventory changed while reading") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read(root_fd: int, name: str, *, limit: int) -> tuple[bytes, tuple[int, int, int, int, int]]:
    descriptor = -1
    try:
        named = dirfd.stat_child(root_fd, name)
        if not stat.S_ISREG(named.st_mode):
            raise DerivativeExportError(f"source artifact is not a regular file: {name}")
        if named.st_size < 0 or named.st_size > limit:
            raise DerivativeExportError(f"source artifact exceeds size limit: {name}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        descriptor = dirfd.open_child_file(root_fd, name, flags, share_write=False)
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise DerivativeExportError(f"source artifact is not a regular file: {name}")
        if _file_observation(initial) != _file_observation(named):
            raise DerivativeExportError(f"source artifact changed while reading: {name}")
        if initial.st_size < 0 or initial.st_size > limit:
            raise DerivativeExportError(f"source artifact exceeds size limit: {name}")

        chunks: list[bytes] = []
        count = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise DerivativeExportError(f"source artifact exceeds size limit: {name}")
            chunks.append(chunk)

        final = os.fstat(descriptor)
        current = dirfd.stat_child(root_fd, name)
        if (
            count != initial.st_size
            or _file_observation(final) != _file_observation(initial)
            or _file_observation(current) != _file_observation(initial)
        ):
            raise DerivativeExportError(f"source artifact changed while reading: {name}")
        return b"".join(chunks), _file_observation(initial)
    except DerivativeExportError:
        raise
    except (OSError, NotImplementedError, AttributeError) as exc:
        raise DerivativeExportError(f"source artifact could not be read safely: {name}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _json_object(data: bytes, name: str, *, max_bytes: int = attestation_input.MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        value = attestation_input.strict_json_loads(data, max_bytes=max_bytes)
    except attestation_input.AttestationInputError as exc:
        raise DerivativeExportError(f"source {name} is malformed") from exc
    if not isinstance(value, dict):
        raise DerivativeExportError(f"source {name} is not an object")
    return value


def _safe_text(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise DerivativeExportError("allowlisted metadata has an invalid type")
    verdict = evidence_redaction.apply_origin_redaction(value, origin="unknown")
    if verdict.status == "error":
        raise DerivativeExportError("metadata redaction failed")
    return verdict.persisted_text, verdict.count


def _minimize_receipt(receipt: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    kept: dict[str, Any] = {}
    redaction_occurrences = 0
    for key in sorted(_RECEIPT_FIELDS & receipt.keys()):
        value = receipt[key]
        if value is None or isinstance(value, (int, bool)):
            kept[key] = value
        elif isinstance(value, str):
            kept[key], count = _safe_text(value)
            redaction_occurrences += count
        else:
            raise DerivativeExportError(f"allowlisted receipt field has an invalid type: {key}")
    return kept, len(set(receipt) - _RECEIPT_FIELDS), redaction_occurrences


def _validate_receipt_field_types(receipt: dict[str, Any]) -> None:
    status = receipt.get("status")
    if not isinstance(status, str) or status not in _RECEIPT_STATUSES:
        raise DerivativeExportError("source receipt field is invalid: status")
    for key in ("started_at", "completed_at"):
        value = receipt.get(key)
        if (key == "started_at" and (not isinstance(value, str) or not value.strip())) or (
            key == "completed_at" and value is not None and (not isinstance(value, str) or not value.strip())
        ):
            raise DerivativeExportError(f"source receipt field is invalid: {key}")
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DerivativeExportError(f"source receipt field is invalid: {key}") from exc
            if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                raise DerivativeExportError(f"source receipt field is invalid: {key}")
    exit_code = receipt.get("exit_code")
    if exit_code is not None and type(exit_code) is not int:
        raise DerivativeExportError("source receipt field is invalid: exit_code")


def _validate_manifest_entries(manifest: dict[str, Any], raw: dict[str, bytes]) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise DerivativeExportError("source manifest entries are malformed")
    declared: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256", "bytes", "media_type", "kind"}
            or not isinstance(entry.get("path"), str)
        ):
            raise DerivativeExportError("source manifest entry is malformed")
        name = entry["path"]
        if name not in _SUPPORTED or name == "manifest.json":
            raise DerivativeExportError("source manifest declares an unsupported artifact class")
        data = raw.get(name)
        if (
            name in declared
            or data is None
            or type(entry.get("bytes")) is not int
            or entry["bytes"] != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise DerivativeExportError(f"source artifact digest mismatch: {name}")
        declared.add(name)
    if declared != set(raw) - {"manifest.json"} or manifest.get("entries_sha256") != localio.canonical_json_digest(
        entries
    ):
        raise DerivativeExportError("source package contents do not match its manifest")


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Compare directory identity, not path text, so case-folding and bind mounts can't hide nesting."""
    try:
        target = os.stat(directory)
        identity = (target.st_dev, target.st_ino)
        for candidate in (path, *path.parents):
            try:
                info = os.stat(candidate)
            except FileNotFoundError:
                continue  # Ancestors created later by the exporter cannot be the source.
            if (info.st_dev, info.st_ino) == identity:
                return True
    except OSError as exc:
        raise DerivativeExportError("derivative paths could not be resolved safely") from exc
    return False


def export_derivative(
    *,
    source: Path,
    out: Path,
    recipient: str,
    authority: str,
    lawful_basis: str,
    necessity: str,
) -> DerivativeReport:
    """Create one local, integrity-only derivative without changing its source."""
    try:
        source = source.expanduser()
        out = out.expanduser()
        if source.is_symlink() or not source.is_dir():
            raise DerivativeExportError("source package is not a directory")
        if out.is_symlink():
            raise DerivativeExportError("derivative output cannot be a symlink")
        source = source.resolve()
        out = out.parent.resolve() / out.name
        if out.exists():
            raise DerivativeExportError("derivative output already exists")
    except (OSError, RuntimeError, ValueError) as exc:
        raise DerivativeExportError("derivative paths could not be resolved safely") from exc
    if _is_within_directory(out.parent, source):
        raise DerivativeExportError("derivative output cannot be inside its source package")

    root_fd = -1
    try:
        root_fd = dirfd.open_directory_nofollow(source, writable=False)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise DerivativeExportError("source package is not a directory")
        root_observation = _file_observation(root_info)
        names = _source_inventory(root_fd)
        if "manifest.json" not in names or "receipt.json" not in names:
            raise DerivativeExportError("source is not a portable evidence package")
        known_names = [name for name in names if name in _SUPPORTED]
        raw: dict[str, bytes] = {}
        file_observations: dict[str, tuple[int, int, int, int, int]] = {}
        for name in known_names:
            limit = (
                evidence_package_snapshot._MANIFEST_MAX_BYTES
                if name == "manifest.json"
                else attestation_input.MAX_JSON_BYTES
                if name == "receipt.json"
                else evidence_package.MAX_ENTRY_BYTES
            )
            raw[name], file_observations[name] = _read(root_fd, name, limit=limit)
        if _source_inventory(root_fd) != names or _file_observation(os.fstat(root_fd)) != root_observation:
            raise DerivativeExportError("source package inventory changed while reading")
    except DerivativeExportError:
        raise
    except OSError as exc:
        raise DerivativeExportError("source package could not be opened safely") from exc
    finally:
        if root_fd != -1:
            os.close(root_fd)
    manifest = _json_object(raw["manifest.json"], "manifest", max_bytes=evidence_package_snapshot._MANIFEST_MAX_BYTES)
    if manifest.get("schema") != evidence_package.SCHEMA:
        raise DerivativeExportError("unsupported source package schema")
    try:
        _, source_metadata, _ = evidence_package_snapshot._validate_manifest(manifest)
    except evidence_package_snapshot.PackageSnapshotError as exc:
        raise DerivativeExportError("source manifest does not satisfy the portable package schema") from exc
    _validate_manifest_entries(manifest, raw)
    receipt = _json_object(raw["receipt.json"], "receipt")
    try:
        receipt_snapshot = attestation_receipt.snapshot_receipt(receipt)
    except attestation_receipt.ReceiptDigestMismatchError as exc:
        raise DerivativeExportError("source receipt digest does not match receipt content") from exc
    except attestation_receipt.ReceiptDigestError as exc:
        raise DerivativeExportError("source receipt digest is invalid") from exc
    if source_metadata["receipt_sha256"] != receipt_snapshot.digest:
        raise DerivativeExportError("source manifest receipt digest does not match receipt")
    try:
        evidence_package_snapshot._validate_receipt(raw["receipt.json"], source_metadata)
    except evidence_package_snapshot.PackageSnapshotError as exc:
        message = (
            "source receipt does not satisfy the portable package schema"
            if exc.code == "malformed"
            else "source manifest identities do not match receipt"
        )
        raise DerivativeExportError(message) from exc
    _validate_receipt_field_types(receipt)
    patch_data = raw.get("changes.patch")
    patch_digest = hashlib.sha256(patch_data).hexdigest() if patch_data is not None else None
    receipt_patch_digest = receipt.get("changes_patch_sha256")
    manifest_patch_digest = source_metadata.get("changes_patch_sha256")
    if patch_data is None:
        if receipt_patch_digest != manifest_patch_digest:
            raise DerivativeExportError("source patch digest does not match receipt and manifest")
    elif receipt_patch_digest != patch_digest or manifest_patch_digest != patch_digest:
        raise DerivativeExportError("source patch digest does not match receipt and manifest")
    minimized, removed, redaction_occurrences = _minimize_receipt(receipt)
    excluded = len(names) - len(known_names)
    safe_authorization: dict[str, Any] = {}
    for key, value in (
        ("recipient", recipient),
        ("authority", authority),
        ("lawful_basis", lawful_basis),
        ("necessity", necessity),
    ):
        if not isinstance(value, str):
            raise DerivativeExportError("allowlisted metadata has an invalid type")
        if not value.strip():
            raise DerivativeExportError(f"authorization metadata cannot be blank: {key}")
        safe_authorization[key], redacted = _safe_text(value)
        redaction_occurrences += redacted
    safe_authorization.update(
        {
            "organization_supplied": True,
        }
    )

    source_artifacts = [
        {"path": name, "sha256": hashlib.sha256(raw[name]).hexdigest(), "bytes": len(raw[name])} for name in known_names
    ]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "classification": "minimized-derivative",
        "created_at": localio.utc_now_iso_z(),
        "authorization": safe_authorization,
        "source_artifacts": source_artifacts,
        "evidence": {"receipt_metadata": minimized},
        "transformation": {
            "policy_version": POLICY_VERSION,
            "removed_fields": removed,
            "redaction_occurrences": redaction_occurrences,
            "excluded_artifacts": excluded,
            "unknown_artifact_policy": "excluded",
        },
        "decisions": {
            "export": "authorized-local-copy",
            "source_quarantine": "unchanged",
            "source_cleanup": "not-authorized",
            "source_hold": "unchanged",
        },
        "verification": {
            "source_signed_objects": "not-included",
            "source_verification": "verify-originals-separately-by-recorded-digest",
            "derivative": "integrity-only",
        },
        "limitations": ["not-original-signed-object", "no-recipient-authorization-validation", "local-export-only"],
    }
    if patch_data is None and manifest_patch_digest is not None:
        payload["limitations"].append("source-patch-omitted")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    derivative_sha256 = hashlib.sha256(encoded).hexdigest()
    parent_fd = output_fd = temp_fd = -1
    temp_name = f".derivative.json.tmp-{secrets.token_hex(4)}"
    parent_identity: tuple[int, int] | None = None
    output_identity: tuple[int, int] | None = None
    temp_identity: tuple[int, int] | None = None
    created_output = False
    published = False
    try:
        parent_fd = dirfd.open_directory_chain(out.parent, create=True)
        parent_info = os.fstat(parent_fd)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _verify_source_inventory(source, names, root_observation, file_observations)
        current_parent_fd = dirfd.open_directory_chain(out.parent)
        try:
            current_parent = os.fstat(current_parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise DerivativeExportError("derivative output parent changed during publication")
        finally:
            os.close(current_parent_fd)
        dirfd.mkdir_child(parent_fd, out.name)
        created_output = True
        created_entry = dirfd.stat_child(parent_fd, out.name)
        output_identity = (created_entry.st_dev, created_entry.st_ino)
        output_fd = dirfd.open_child_directory(parent_fd, out.name)
        output_info = os.fstat(output_fd)
        if (output_info.st_dev, output_info.st_ino) != output_identity:
            raise DerivativeExportError("derivative output directory changed during publication")
        output_entry = dirfd.stat_child(parent_fd, out.name)
        if (output_entry.st_dev, output_entry.st_ino) != output_identity:
            raise DerivativeExportError("derivative output directory changed during publication")
        temp_fd = dirfd.open_child_file(
            output_fd,
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
            share_write=False,
        )
        temp_info = os.fstat(temp_fd)
        temp_identity = (temp_info.st_dev, temp_info.st_ino)
        if not stat.S_ISREG(temp_info.st_mode):
            raise DerivativeExportError("derivative temporary file changed during publication")
        view = memoryview(encoded)
        while view:
            view = view[os.write(temp_fd, view) :]
        os.fsync(temp_fd)
        current_parent_fd = dirfd.open_directory_chain(out.parent)
        try:
            current_parent = os.fstat(current_parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise DerivativeExportError("derivative output parent changed during publication")
        finally:
            os.close(current_parent_fd)
        output_entry = dirfd.stat_child(parent_fd, out.name)
        if (output_entry.st_dev, output_entry.st_ino) != output_identity:
            raise DerivativeExportError("derivative output directory changed during publication")
        temp_entry = dirfd.stat_child(output_fd, temp_name)
        if not stat.S_ISREG(temp_entry.st_mode) or (temp_entry.st_dev, temp_entry.st_ino) != temp_identity:
            raise DerivativeExportError("derivative temporary file changed during publication")
        if dirfd.posix_available():
            dirfd.link_child(output_fd, temp_name, "derivative.json")
            published = True
            dirfd.unlink_child(output_fd, temp_name)
        else:
            dirfd.replace_children(output_fd, temp_name, "derivative.json", replace=False)
            published = True
        final_entry = dirfd.stat_child(output_fd, "derivative.json")
        if not stat.S_ISREG(final_entry.st_mode) or (final_entry.st_dev, final_entry.st_ino) != temp_identity:
            raise DerivativeExportError("derivative output file changed during publication")
        current_parent_fd = dirfd.open_directory_chain(out.parent)
        try:
            current_parent = os.fstat(current_parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise DerivativeExportError("derivative output parent changed during publication")
        finally:
            os.close(current_parent_fd)
        output_entry = dirfd.stat_child(parent_fd, out.name)
        if (output_entry.st_dev, output_entry.st_ino) != output_identity:
            raise DerivativeExportError("derivative output directory changed during publication")
        dirfd.fsync_directory(output_fd)
        # Windows cannot deny write sharing while our write handle is open.
        # Reopen relative to the held directory and revalidate identity and bytes.
        os.close(temp_fd)
        temp_fd = -1
        published_fd = dirfd.open_child_file(
            output_fd,
            "derivative.json",
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0),
            share_write=False,
        )
        with os.fdopen(published_fd, "rb") as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != temp_identity:
                raise DerivativeExportError("derivative output file changed during publication")
            content = reader.read(len(encoded) + 1)
            after = os.fstat(reader.fileno())
            final_entry = dirfd.stat_child(output_fd, "derivative.json")
            if (
                _file_observation(before) != _file_observation(after)
                or _file_observation(before) != _file_observation(final_entry)
                or hashlib.sha256(content).hexdigest() != derivative_sha256
            ):
                raise DerivativeExportError("derivative output file changed during publication")
            try:
                output_names = dirfd.child_names(output_fd, 2)
            except (OSError, NotImplementedError) as exc:
                raise DerivativeExportError("derivative output inventory could not be read safely") from exc
            if output_names != ["derivative.json"]:
                raise DerivativeExportError("derivative output directory contains unexpected entries")
    except BaseException as exc:
        if temp_fd != -1:
            os.close(temp_fd)
            temp_fd = -1
        if output_fd != -1:
            for name, identity in (
                (temp_name, temp_identity),
                ("derivative.json", temp_identity if published else None),
            ):
                if identity is not None:
                    try:
                        entry = dirfd.stat_child(output_fd, name)
                        if (entry.st_dev, entry.st_ino) == identity:
                            dirfd.unlink_child(output_fd, name)
                    except OSError:
                        pass
            os.close(output_fd)
            output_fd = -1
        if created_output and parent_fd != -1 and output_identity is not None:
            try:
                entry = dirfd.stat_child(parent_fd, out.name)
                if (entry.st_dev, entry.st_ino) == output_identity:
                    dirfd.rmdir_child(parent_fd, out.name)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise DerivativeExportError("derivative output could not be published") from exc
        raise
    finally:
        for descriptor in (temp_fd, output_fd, parent_fd):
            if descriptor != -1:
                os.close(descriptor)
    return DerivativeReport(removed, redaction_occurrences, excluded, derivative_sha256)
