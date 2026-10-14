"""Evidence-package verification adapter for control-readiness assessments."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from . import (
    _control_crosswalk_io,
    attestation_input,
    attestation_receipt,
    control_readiness,
    evidence_package,
    evidence_package_snapshot,
)


def linked_package(ctx: _control_crosswalk_io._ReadContext, artifact: Path) -> bool:
    """Consume link refusals represented by a package-specific rejection."""
    manifest = artifact / "manifest.json"
    linked = _control_crosswalk_io._is_symlink(ctx, artifact) or _control_crosswalk_io._is_symlink(ctx, manifest)
    if linked:
        for path in (artifact, manifest):
            if ctx.reader.errors.get(path) == "symlink_refused":
                ctx.reader.errors.pop(path)
    return linked


def check_entry_name(name: str, *, strict_package: bool) -> None:
    """Reject unsafe paths and names outside the strict package descriptor."""
    if not name or name in {".", ".."} or "/" in name or any(part == ".." for part in name.split("/")):
        raise evidence_package_snapshot.PackageSnapshotError("unsafe")
    if strict_package and name not in evidence_package.PACKAGE_ENTRY_NAMES:
        raise evidence_package_snapshot.PackageSnapshotError("malformed")
    if not strict_package and not evidence_package._is_safe_entry_path(name):
        raise evidence_package_snapshot.PackageSnapshotError("unsafe")


def stage_source_package(
    reader: _control_crosswalk_io._AssessmentReader,
    artifact: Path,
    read_manifest: Callable[[], dict[str, object] | None],
    *,
    strict_package: bool,
    reserve_bytes: int,
) -> Path:
    """Stage a package and reject source changes during the bounded copy."""
    package_fd = reader.directory(artifact)
    initial_directory_metadata = _control_crosswalk_io._package_entry_metadata(os.fstat(package_fd))
    assert initial_directory_metadata is not None
    max_children = len(evidence_package.PACKAGE_FILES) + 2
    actual_names = set(_control_crosswalk_io.dirfd.child_names(package_fd, max_children))
    manifest_path = artifact / "manifest.json"
    manifest_metadata = reader.read_metadata.get(manifest_path)
    if manifest_metadata is None:
        manifest_metadata = _control_crosswalk_io._package_entry_metadata(reader.info(manifest_path))
    source_metadata = {"manifest.json": manifest_metadata}
    manifest = read_manifest()
    if manifest is None:
        raise _control_crosswalk_io._ReadRefusal("discovery_unreadable")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise evidence_package_snapshot.PackageSnapshotError("malformed")
    entry_names: set[str] = set()
    entry_sizes: dict[str, object] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise evidence_package_snapshot.PackageSnapshotError("malformed")
        name = entry["path"]
        check_entry_name(name, strict_package=strict_package)
        if strict_package:
            declared_size = entry.get("bytes")
            limit = attestation_input.MAX_JSON_BYTES if name == "receipt.json" else evidence_package.MAX_ENTRY_BYTES
            if type(declared_size) is not int or not 0 <= declared_size <= limit:
                raise evidence_package_snapshot.PackageSnapshotError("malformed")
        entry_sizes[name] = entry.get("bytes")
        entry_names.add(name)
    if len(actual_names) > len(evidence_package.PACKAGE_FILES) + 1 or actual_names - {
        "manifest.json",
        *entry_names,
    }:
        raise evidence_package_snapshot.PackageSnapshotError("unsafe")
    reader.copy(artifact / "manifest.json", reserve_bytes=reserve_bytes, hold_open=strict_package)
    for name in sorted(entry_names, key=lambda item: item != "receipt.json"):
        entry_path = artifact / name
        limit = attestation_input.MAX_JSON_BYTES if name == "receipt.json" else evidence_package.MAX_ENTRY_BYTES
        try:
            entry_info = reader.info(entry_path)
        except FileNotFoundError:
            if strict_package:
                code = "malformed" if name == "receipt.json" else "mismatch"
                raise evidence_package_snapshot.PackageSnapshotError(code) from None
            entry_info = None
        except OSError as exc:
            if _control_crosswalk_io._is_unsafe_link_refusal(exc):
                raise evidence_package_snapshot.PackageSnapshotError("unsafe") from exc
            raise
        if entry_info is not None and (stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISREG(entry_info.st_mode)):
            raise evidence_package_snapshot.PackageSnapshotError("unsafe")
        if strict_package and entry_info is not None and entry_info.st_size != entry_sizes[name]:
            raise evidence_package_snapshot.PackageSnapshotError("mismatch")
        source_metadata[name] = _control_crosswalk_io._package_entry_metadata(entry_info)
        try:
            reader.copy(entry_path, limit=limit, reserve_bytes=reserve_bytes, hold_open=strict_package)
        except _control_crosswalk_io._ReadRefusal as exc:
            if exc.reason == "read_limit_exceeded":
                raise evidence_package_snapshot.PackageSnapshotError("read_limit_exceeded") from exc
            if _control_crosswalk_io._is_unsafe_link_refusal(exc):
                raise evidence_package_snapshot.PackageSnapshotError("unsafe") from exc
            raise
        except OSError as exc:
            if _control_crosswalk_io._is_unsafe_link_refusal(exc):
                raise evidence_package_snapshot.PackageSnapshotError("unsafe") from exc
            try:
                entry_info = reader.info(entry_path)
            except FileNotFoundError:
                if strict_package:
                    code = "malformed" if name == "receipt.json" else "mismatch"
                    raise evidence_package_snapshot.PackageSnapshotError(code) from None
                raise exc from None
            except OSError as recheck_error:
                if _control_crosswalk_io._is_unsafe_link_refusal(recheck_error):
                    raise evidence_package_snapshot.PackageSnapshotError("unsafe") from recheck_error
                raise exc from None
            if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISREG(entry_info.st_mode):
                raise evidence_package_snapshot.PackageSnapshotError("unsafe") from exc
            raise
        if strict_package and reader.read_metadata.get(entry_path) != source_metadata[name]:
            raise evidence_package_snapshot.PackageSnapshotError("mismatch")
    source_issue = _control_crosswalk_io.package_source_issue(
        reader,
        artifact,
        package_fd,
        initial_directory_metadata,
        actual_names,
        source_metadata,
        max_children=max_children,
    )
    if source_issue is not None:
        raise evidence_package_snapshot.PackageSnapshotError(source_issue)
    return reader.snapshot_target / artifact.relative_to(reader.target)


def receipt_binding_candidates(staged_package: Path) -> dict[str, object]:
    """Return run identities only from a receipt with a valid stored digest."""
    try:
        receipt = attestation_input.read_json_object(staged_package / "receipt.json")
        snapshot = attestation_receipt.snapshot_stored_receipt(receipt)
    except (OSError, attestation_input.AttestationInputError, attestation_receipt.ReceiptDigestError):
        return {}
    return {
        "receipt.producer_run_id": snapshot.receipt.get("producer_run_id"),
        "receipt.run_id": snapshot.receipt.get("run_id"),
    }


def binding_candidates(
    obs: control_readiness.ArtifactObservation,
    source: object,
    receipt: dict[str, object],
    *,
    strict_package: bool,
) -> dict[str, object]:
    if strict_package and obs.proposed in {"invalid", "rejected", "unavailable"}:
        return receipt
    candidates = (
        {
            "source.producer_run_id": source.get("producer_run_id"),
            "source.verify_run_id": source.get("verify_run_id"),
        }
        if isinstance(source, dict)
        else {}
    )
    candidates.update(receipt)
    return candidates


def unreadable_manifest_observation(
    reader: _control_crosswalk_io._AssessmentReader, relpath: str, path: Path
) -> control_readiness.ArtifactObservation:
    reason = reader.errors.get(path)
    if reason is None:
        return control_readiness.ArtifactObservation(
            relpath, "discovered", "invalid", control_readiness.dims(), ["invalid_json"]
        )
    if reason == "symlink_refused":
        unsafe = True
    else:
        try:
            info = reader.info(path)
        except OSError:
            info = None
        unsafe = info is not None and not stat.S_ISREG(info.st_mode)
    if unsafe:
        return control_readiness.ArtifactObservation(
            relpath,
            "claim_validated",
            "rejected",
            control_readiness.dims(integrity=("failed", "entry_path_unsafe"), subject="unknown", population="passed"),
            ["entry_path_unsafe"],
        )
    return _unavailable(relpath, reason)


def needs_staged_receipt(obs: control_readiness.ArtifactObservation) -> bool:
    return (
        "package_snapshot_mismatch" in obs.reasons
        or (obs.proposed == "invalid" and "schema_missing" in obs.reasons)
        or (obs.proposed == "rejected" and "entry_path_unsafe" in obs.reasons)
        or obs.proposed == "unavailable"
    )


def trusted_timestamp_manifest(
    manifest: dict[str, object] | None,
    obs: control_readiness.ArtifactObservation,
    *,
    strict_package: bool,
) -> dict[str, object] | None:
    """Discard manifest timestamps until strict-package validation succeeds."""
    return None if strict_package and obs.proposed in {"invalid", "rejected", "unavailable"} else manifest


def _unavailable(relpath: str, reason: str) -> control_readiness.ArtifactObservation:
    return control_readiness.ArtifactObservation(
        relpath,
        "discovered",
        "unavailable",
        control_readiness.dims(
            integrity=("unavailable", reason),
            signature=("unavailable", reason),
            authorization=("unavailable", reason),
            subject="unknown",
            population="not_applicable",
        ),
        [reason],
    )


def verify_evidence_package(_target: Path, package_dir: Path) -> control_readiness.ArtifactObservation:
    """Validate package bytes and return the bounded readiness observation."""
    try:
        snapshot = evidence_package_snapshot.snapshot_package(package_dir)
    except evidence_package_snapshot.PackageSnapshotError as exc:
        if exc.code == "unavailable":
            return _unavailable(package_dir.name, "verifier_tool_unavailable")
        if exc.code == "changed":
            return _unavailable(package_dir.name, "verifier_error")
        if exc.code == "unsafe":
            return control_readiness.ArtifactObservation(
                package_dir.name,
                "claim_validated",
                "rejected",
                control_readiness.dims(
                    integrity=("failed", "entry_path_unsafe"), subject="unknown", population="passed"
                ),
                ["entry_path_unsafe"],
            )
        if exc.code == "read_limit_exceeded":
            return _unavailable(package_dir.name, exc.code)
        if exc.code == "malformed":
            return control_readiness.ArtifactObservation(
                package_dir.name, "discovered", "invalid", control_readiness.dims(), ["schema_missing"]
            )
        if exc.code == "mismatch":
            return control_readiness.ArtifactObservation(
                package_dir.name,
                "claim_validated",
                "rejected",
                control_readiness.dims(
                    integrity=("failed", "package_snapshot_mismatch"), subject="unknown", population="passed"
                ),
                ["package_snapshot_mismatch"],
            )
        return _unavailable(package_dir.name, "verifier_error")
    return control_readiness.ArtifactObservation(
        package_dir.name,
        "claim_validated",
        "validated",
        control_readiness.dims(integrity="passed", subject="passed", population="passed"),
        source_digest_sha256=snapshot.manifest_sha256,
    )
