"""Evidence-package verification adapter for control-readiness assessments."""

from __future__ import annotations

from pathlib import Path

from . import control_readiness, evidence_package, evidence_package_snapshot


def check_entry_name(name: str, *, strict_package: bool) -> None:
    """Reject unsafe paths and names outside the strict package descriptor."""
    if not evidence_package._is_safe_entry_path(name):
        raise evidence_package_snapshot.PackageSnapshotError("unsafe")
    if strict_package and name not in evidence_package.PACKAGE_ENTRY_NAMES:
        raise evidence_package_snapshot.PackageSnapshotError("malformed")


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
