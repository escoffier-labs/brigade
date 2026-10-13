"""Evidence-package verification adapter for control-readiness assessments."""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path

from . import _control_crosswalk_io, attestation_input, control_readiness, evidence_package, evidence_package_snapshot


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
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise evidence_package_snapshot.PackageSnapshotError("malformed")
        name = entry["path"]
        check_entry_name(name, strict_package=strict_package)
        entry_names.add(name)
    if len(actual_names) > len(evidence_package.PACKAGE_FILES) + 1 or actual_names - {
        "manifest.json",
        *entry_names,
    }:
        raise evidence_package_snapshot.PackageSnapshotError("unsafe")
    reader.copy(artifact / "manifest.json", reserve_bytes=reserve_bytes)
    for name in entry_names:
        entry_path = artifact / name
        limit = attestation_input.MAX_JSON_BYTES if name == "receipt.json" else evidence_package.MAX_ENTRY_BYTES
        try:
            entry_info = reader.info(entry_path)
        except FileNotFoundError:
            entry_info = None
        if entry_info is not None and (stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISREG(entry_info.st_mode)):
            raise evidence_package_snapshot.PackageSnapshotError("unsafe")
        source_metadata[name] = _control_crosswalk_io._package_entry_metadata(entry_info)
        try:
            reader.copy(entry_path, limit=limit, reserve_bytes=reserve_bytes)
        except OSError as exc:
            try:
                entry_info = reader.info(entry_path)
            except OSError:
                raise exc from None
            if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISREG(entry_info.st_mode):
                raise evidence_package_snapshot.PackageSnapshotError("unsafe") from exc
            raise
    source_issue = _control_crosswalk_io.package_source_issue(
        reader, artifact, package_fd, actual_names, source_metadata, max_children=max_children
    )
    if source_issue is not None:
        raise evidence_package_snapshot.PackageSnapshotError(source_issue)
    return reader.snapshot_target / artifact.relative_to(reader.target)


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
