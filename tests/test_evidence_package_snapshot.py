"""Strict, descriptor-anchored portable evidence package snapshots."""

from __future__ import annotations

import errno
import hashlib
import itertools
import json
import os
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from brigade import attestation_input, evidence_package, localio
from brigade.evidence_package_snapshot import PackageSnapshotError, snapshot_package


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp_receipt(receipt: dict[str, object]) -> None:
    receipt["digests"] = {
        "algorithm": "sha256",
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }


def _write_package(tmp_path: Path, *, files: dict[str, bytes] | None = None) -> Path:
    directory = tmp_path / f"package-{sum(1 for _ in tmp_path.iterdir())}"
    directory.mkdir()
    supplied = files if files is not None else {"changes.patch": b"diff --git a/a b/a\n"}
    receipt: dict[str, object] = {
        "schema_version": 2,
        "run_id": "20260908-120000-work-verify-package",
        "producer_run_id": "20260908-110000-work-run-package",
        "tree_fingerprint": "1" * 40,
        "baseline_commit": "2" * 40,
        "changes_patch_sha256": _sha256(supplied["changes.patch"]) if "changes.patch" in supplied else None,
    }
    _stamp_receipt(receipt)
    receipt_bytes = json.dumps(receipt, sort_keys=True).encode("utf-8")
    contents = {"receipt.json": receipt_bytes, **supplied}
    allowed = {name: (kind, media) for name, kind, media in evidence_package.PACKAGE_FILES}
    entries = [
        {
            "path": name,
            "sha256": _sha256(data),
            "bytes": len(data),
            "media_type": allowed[name][1],
            "kind": allowed[name][0],
        }
        for name, data in sorted(contents.items())
    ]
    manifest = {
        "schema": evidence_package.SCHEMA,
        "schema_version": 1,
        "created_at": "2026-09-08T12:00:00.123456Z",
        "source": {
            "verify_run_id": receipt["run_id"],
            "producer_run_id": receipt["producer_run_id"],
            "tree_fingerprint": receipt["tree_fingerprint"],
            "baseline_commit": receipt["baseline_commit"],
            "changes_patch_sha256": receipt["changes_patch_sha256"],
            "receipt_sha256": receipt["digests"]["receipt_sha256"],  # type: ignore[index]
        },
        "entries": entries,
        "entries_sha256": localio.canonical_json_digest(entries),
        "limitations": ["receipt-contains-local-paths", "integrity-only"],
    }
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    (directory / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return directory


def _manifest(directory: Path) -> dict[str, object]:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(directory: Path, manifest: dict[str, object]) -> None:
    (directory / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _assert_refused(directory: Path, code: str) -> None:
    with pytest.raises(PackageSnapshotError) as raised:
        snapshot_package(directory)
    assert raised.value.code == code


def _restamp_package_receipt(directory: Path, receipt: dict[str, object]) -> None:
    _stamp_receipt(receipt)
    receipt_bytes = json.dumps(receipt, sort_keys=True).encode("utf-8")
    (directory / "receipt.json").write_bytes(receipt_bytes)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    source = manifest["source"]
    assert isinstance(entries, list) and isinstance(source, dict)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["bytes"] = len(receipt_bytes)
    receipt_entry["sha256"] = _sha256(receipt_bytes)
    source["receipt_sha256"] = receipt["digests"]["receipt_sha256"]  # type: ignore[index]
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)


def _exported_package(tmp_path: Path, optional: dict[str, bytes]) -> Path:
    run_id = "20260908-120000-work-verify-export"
    run_directory = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    run_directory.mkdir(parents=True)
    receipt: dict[str, object] = {
        "schema_version": 2,
        "run_id": run_id,
        "producer_run_id": "20260908-110000-work-run-export",
        "tree_fingerprint": "1" * 40,
        "baseline_commit": "2" * 40,
        "changes_patch_sha256": _sha256(optional["changes.patch"]) if "changes.patch" in optional else None,
    }
    _stamp_receipt(receipt)
    (run_directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    for name, contents in optional.items():
        (run_directory / name).write_bytes(contents)
    exported = tmp_path / "exported"
    assert evidence_package.export_package(target=tmp_path, run_id=run_id, out_str=str(exported)) == 0
    return exported


def test_strict_export_round_trip_and_immutable_output(tmp_path: Path) -> None:
    directory = _write_package(
        tmp_path,
        files={
            "changes.patch": b"patch\n",
            "attestation.json": b'{"opaque": true}',
            "attestation.sigstore.json": b"not validated here",
            "summary.md": b"# summary\n",
        },
    )

    snapshot = snapshot_package(directory)

    assert snapshot.manifest_sha256 == _sha256(snapshot.manifest_bytes)
    assert [entry.path for entry in snapshot.entries] == sorted(entry.path for entry in snapshot.entries)
    assert snapshot.source.verify_run_id == "20260908-120000-work-verify-package"
    with pytest.raises(AttributeError):
        snapshot.source.verify_run_id = "changed"  # type: ignore[misc]
    assert isinstance(snapshot.entries, tuple)


@pytest.mark.parametrize(
    "included",
    [
        combination
        for count in range(5)
        for combination in itertools.combinations(
            ("changes.patch", "attestation.json", "attestation.sigstore.json", "summary.md"), count
        )
    ],
)
def test_exported_packages_round_trip_for_every_optional_combination(tmp_path: Path, included: tuple[str, ...]) -> None:
    optional = {
        "changes.patch": b"patch\n",
        "attestation.json": b'{"opaque": true}',
        "attestation.sigstore.json": b"unvalidated bundle",
        "summary.md": b"# summary\n",
    }
    directory = _exported_package(tmp_path, {name: optional[name] for name in included})

    snapshot = snapshot_package(directory)

    assert {entry.path for entry in snapshot.entries} == {"receipt.json", *included}
    assert {path.name for path in directory.iterdir()} == {"manifest.json", "receipt.json", *included}
    assert not hasattr(snapshot.source, "signature_valid")


def test_manifest_identity_hashes_exact_captured_bytes(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    first = snapshot_package(directory)
    manifest = _manifest(directory)
    (directory / "manifest.json").write_bytes(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
    second = snapshot_package(directory)

    assert first.manifest_sha256 != second.manifest_sha256
    assert first.manifest_bytes != second.manifest_bytes


@pytest.mark.parametrize(
    ("created_at", "accepted"),
    [
        ("2026-09-08T12:00:00Z", True),
        ("2026-09-08T12:00:00.1Z", True),
        ("2026-02-30T12:00:00Z", False),
        ("2026-09-08T12:00:00+00:00", False),
        ("2026-09-08T12:00:00.1234567Z", False),
    ],
)
def test_created_at_requires_valid_utc_precision(tmp_path: Path, created_at: str, accepted: bool) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    manifest["created_at"] = created_at
    _write_manifest(directory, manifest)

    if accepted:
        assert snapshot_package(directory).source.verify_run_id
    else:
        _assert_refused(directory, "malformed")


def test_strict_manifest_shapes_and_entry_allowlist(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    manifest["extra"] = None
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")

    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["media_type"] = "text/plain"
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")


@pytest.mark.parametrize(
    "field", ["verify_run_id", "producer_run_id", "tree_fingerprint", "baseline_commit", "changes_patch_sha256"]
)
def test_source_must_equal_the_stored_receipt(tmp_path: Path, field: str) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    source = manifest["source"]
    assert isinstance(source, dict)
    source[field] = {
        "tree_fingerprint": "3" * 40,
        "baseline_commit": "4" * 40,
        "changes_patch_sha256": "3" * 64,
    }.get(field, "different-run-id")
    _write_manifest(directory, manifest)
    _assert_refused(directory, "mismatch")


def test_stored_receipt_digest_and_patch_presence_are_required(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    receipt.pop("digests")
    receipt_bytes = json.dumps(receipt, sort_keys=True).encode("utf-8")
    (directory / "receipt.json").write_bytes(receipt_bytes)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    source = manifest["source"]
    assert isinstance(entries, list) and isinstance(source, dict)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["bytes"] = len(receipt_bytes)
    receipt_entry["sha256"] = _sha256(receipt_bytes)
    source["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")
    assert evidence_package.verify_package(directory=directory, json_output=False) == 0

    directory = _write_package(tmp_path, files={})
    manifest = _manifest(directory)
    source = manifest["source"]
    assert isinstance(source, dict)
    source["changes_patch_sha256"] = "3" * 64
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")


def test_patch_digest_must_match_source_receipt_entry_and_actual_bytes(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    receipt["changes_patch_sha256"] = "3" * 64
    _stamp_receipt(receipt)
    receipt_bytes = json.dumps(receipt, sort_keys=True).encode("utf-8")
    (directory / "receipt.json").write_bytes(receipt_bytes)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    source = manifest["source"]
    assert isinstance(entries, list) and isinstance(source, dict)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["bytes"] = len(receipt_bytes)
    receipt_entry["sha256"] = _sha256(receipt_bytes)
    source["changes_patch_sha256"] = receipt["changes_patch_sha256"]
    source["receipt_sha256"] = receipt["digests"]["receipt_sha256"]
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)

    _assert_refused(directory, "mismatch")


def test_receipt_digest_equality_and_nullable_receipt_fields(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    source = manifest["source"]
    assert isinstance(source, dict)
    source["receipt_sha256"] = "f" * 64
    _write_manifest(directory, manifest)
    _assert_refused(directory, "mismatch")

    directory = _write_package(tmp_path, files={})
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    for key in ("producer_run_id", "tree_fingerprint", "baseline_commit", "changes_patch_sha256"):
        receipt.pop(key)
    _restamp_package_receipt(directory, receipt)
    manifest = _manifest(directory)
    source = manifest["source"]
    assert isinstance(source, dict)
    source.update(
        {
            "producer_run_id": None,
            "tree_fingerprint": None,
            "baseline_commit": None,
            "changes_patch_sha256": None,
        }
    )
    _write_manifest(directory, manifest)
    snapshot = snapshot_package(directory)
    assert snapshot.source.producer_run_id is None
    assert snapshot.source.tree_fingerprint is None
    assert snapshot.source.baseline_commit is None
    assert snapshot.source.changes_patch_sha256 is None


def test_successful_repeated_snapshots_close_each_root_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    opened: list[int] = []
    closed: list[int] = []
    real_open = package_snapshot.dirfd.open_directory_nofollow
    real_close = os.close

    def record_open(path: Path) -> int:
        descriptor = real_open(path)
        opened.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(package_snapshot.dirfd, "open_directory_nofollow", record_open)
    monkeypatch.setattr(os, "close", record_close)
    assert snapshot_package(directory).entries
    assert snapshot_package(directory).entries
    assert opened == [descriptor for descriptor in opened if descriptor in closed]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError(errno.EIO, "read failure"), id="eio"),
        pytest.param(OSError(errno.ENOSYS, "unsupported"), id="enosys"),
        pytest.param(NotImplementedError("unsupported"), id="not-implemented"),
    ],
)
def test_every_opened_descriptor_is_closed_on_success_and_child_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    active: set[int] = set()
    real_open = os.open
    real_close = os.close
    real_read = os.read
    child_descriptors: set[int] = set()

    def track(descriptor: int) -> int:
        active.add(descriptor)
        return descriptor

    monkeypatch.setattr(
        package_snapshot.dirfd,
        "open_directory_nofollow",
        lambda path: track(real_open(path, package_snapshot.dirfd.root_directory_flags())),
    )

    def open_child(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        descriptor = track(real_open(name, flags, dir_fd=parent))
        child_descriptors.add(descriptor)
        return descriptor

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", open_child)
    monkeypatch.setattr(
        package_snapshot.os, "open", lambda name, flags, dir_fd: track(real_open(name, flags, dir_fd=dir_fd))
    )
    monkeypatch.setattr(
        package_snapshot.os, "close", lambda descriptor: (active.discard(descriptor), real_close(descriptor))[1]
    )
    monkeypatch.setattr(package_snapshot, "_capabilities_available", lambda: True)

    assert snapshot_package(directory).entries
    assert not active
    assert snapshot_package(directory).entries
    assert not active

    failed = False

    def fail_after_child_open(descriptor: int, count: int) -> bytes:
        nonlocal failed
        if descriptor in child_descriptors and not failed:
            failed = True
            raise failure
        return real_read(descriptor, count)

    monkeypatch.setattr(package_snapshot.os, "read", fail_after_child_open)
    _assert_refused(directory, "unavailable")
    assert failed
    assert not active


def test_declared_companion_growth_uses_only_one_eof_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _write_package(tmp_path, files={"changes.patch": b"small"})
    import brigade.evidence_package_snapshot as package_snapshot

    patch_descriptor = -1
    requested: list[int] = []
    returned: list[int] = []
    grew = False
    real_open = package_snapshot.dirfd.open_child_file
    real_read = os.read

    def record_patch_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        nonlocal patch_descriptor
        descriptor = real_open(parent, name, flags, mode)
        if name == "changes.patch":
            patch_descriptor = descriptor
        return descriptor

    def grow_on_patch_read(descriptor: int, count: int) -> bytes:
        nonlocal grew
        if descriptor == patch_descriptor:
            requested.append(count)
            if not grew:
                grew = True
                with (directory / "changes.patch").open("ab") as handle:
                    handle.write(b"expanded")
        data = real_read(descriptor, count)
        if descriptor == patch_descriptor:
            returned.append(len(data))
        return data

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", record_patch_open)
    monkeypatch.setattr(os, "read", grow_on_patch_read)
    _assert_refused(directory, "mismatch")
    assert requested
    assert max(requested) <= len(b"small") + 1
    assert sum(returned) <= len(b"small") + 1


def test_package_children_must_be_the_listed_regular_files(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    (directory / "extra.txt").write_text("extra", encoding="utf-8")
    _assert_refused(directory, "unsafe")

    directory = _write_package(tmp_path)
    (directory / "receipt.json").unlink()
    (directory / "receipt.json").symlink_to(directory / "changes.patch")
    _assert_refused(directory, "unsafe")

    directory = _write_package(tmp_path)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(directory, target_is_directory=True)
    _assert_refused(root_link, "unsafe")

    directory = _write_package(tmp_path)
    (directory / "changes.patch").unlink()
    os.mkfifo(directory / "changes.patch")
    _assert_refused(directory, "unsafe")


def test_duplicate_json_and_digest_or_size_mismatches_are_refused(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    (directory / "manifest.json").write_bytes(b'{"schema":"a","schema":"b"}')
    _assert_refused(directory, "malformed")

    directory = _write_package(tmp_path)
    (directory / "changes.patch").write_bytes(b"changed")
    _assert_refused(directory, "mismatch")


def test_missing_fd_enumeration_and_windows_are_explicitly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    monkeypatch.setattr(os, "scandir", os.scandir)
    monkeypatch.setattr(os, "supports_fd", set())
    _assert_refused(directory, "unavailable")

    import brigade.evidence_package_snapshot as package_snapshot

    monkeypatch.setattr(package_snapshot.os, "name", "nt", raising=False)
    _assert_refused(directory, "unavailable")


@pytest.mark.parametrize("missing,windows", [(True, False), (False, True)])
def test_capability_refusals_happen_before_root_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool, windows: bool
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    monkeypatch.setattr(
        package_snapshot.dirfd,
        "open_directory_nofollow",
        lambda path: (_ for _ in ()).throw(AssertionError("root open should not occur")),
    )
    if missing:
        monkeypatch.setattr(package_snapshot.os, "supports_fd", set())
    if windows:
        monkeypatch.setattr(package_snapshot.os, "name", "nt", raising=False)
    _assert_refused(directory, "unavailable")


def test_final_scan_is_fresh_and_anchored_to_the_held_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    calls = 0
    real_scan = package_snapshot._scan_names

    def add_unexpected_child(root_fd: int) -> frozenset[str]:
        nonlocal calls
        calls += 1
        names = real_scan(root_fd)
        if calls == 1:
            (directory / "unexpected-child").write_bytes(b"x")
        return names

    monkeypatch.setattr(package_snapshot, "_scan_names", add_unexpected_child)
    with pytest.raises(PackageSnapshotError) as raised:
        snapshot_package(directory)
    assert raised.value.code in {"changed", "unsafe"}
    assert (directory / "unexpected-child").exists()
    assert calls == 2


def test_scan_refuses_repeated_names_without_consuming_an_unbounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    class RepeatedEntries:
        count = 0

        def __iter__(self) -> "RepeatedEntries":
            return self

        def __next__(self) -> object:
            self.count += 1
            if self.count > len(package_snapshot._PACKAGE_NAMES):
                raise AssertionError("scan consumed the unbounded tail")
            return type("Entry", (), {"name": "manifest.json"})()

    class ScanContext:
        def __enter__(self) -> RepeatedEntries:
            return RepeatedEntries()

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
            return False

    root_fd = package_snapshot.dirfd.open_directory_nofollow(directory)
    monkeypatch.setattr(package_snapshot.os, "scandir", lambda fd: ScanContext())
    try:
        with pytest.raises(PackageSnapshotError) as raised:
            package_snapshot._scan_names(root_fd)
    finally:
        os.close(root_fd)
    assert raised.value.code == "unsafe"


@pytest.mark.parametrize("as_symlink", [False, True])
def test_root_replacement_after_open_cannot_redirect_child_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_symlink: bool
) -> None:
    directory = _write_package(tmp_path)
    (tmp_path / "replacement-parent").mkdir()
    replacement = _write_package(tmp_path / "replacement-parent", files={"changes.patch": b"replacement bytes\n"})
    import brigade.evidence_package_snapshot as package_snapshot

    replacement_inodes = {
        (replacement / name).stat().st_ino for name in ("manifest.json", "receipt.json", "changes.patch")
    }
    opened_inodes: list[int] = []
    real_open = package_snapshot.dirfd.open_directory_nofollow
    real_child_open = package_snapshot.dirfd.open_child_file

    def open_and_replace(path: Path) -> int:
        descriptor = real_open(path)
        moved = tmp_path / "original-held"
        directory.rename(moved)
        if as_symlink:
            directory.symlink_to(replacement, target_is_directory=True)
        else:
            replacement.rename(directory)
        return descriptor

    def record_child_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        descriptor = real_child_open(parent, name, flags, mode)
        opened_inodes.append(os.fstat(descriptor).st_ino)
        return descriptor

    monkeypatch.setattr(package_snapshot.dirfd, "open_directory_nofollow", open_and_replace)
    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", record_child_open)
    with pytest.raises(PackageSnapshotError) as raised:
        snapshot_package(directory)
    assert raised.value.code == "changed"
    assert opened_inodes
    assert not set(opened_inodes) & replacement_inodes


def test_bounds_and_descriptor_cleanup_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    entries[1]["bytes"] = evidence_package.MAX_ENTRY_BYTES + 1
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")

    directory = _write_package(tmp_path)
    closed: list[int] = []
    real_close = os.close

    def record_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", record_close)
    (directory / "extra").write_text("x", encoding="utf-8")
    _assert_refused(directory, "unsafe")
    assert closed


def test_manifest_and_receipt_input_limits(tmp_path: Path) -> None:
    directory = _write_package(tmp_path)
    (directory / "manifest.json").write_bytes(b" " * (64 * 1024 + 1))
    _assert_refused(directory, "malformed")

    directory = _write_package(tmp_path)
    (directory / "receipt.json").write_bytes(b" " * (attestation_input.MAX_JSON_BYTES + 1))
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["bytes"] = attestation_input.MAX_JSON_BYTES + 1
    _write_manifest(directory, manifest)
    _assert_refused(directory, "malformed")


def test_declared_receipt_limit_is_malformed_before_receipt_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    receipt_entry = next(entry for entry in entries if entry["path"] == "receipt.json")
    receipt_entry["bytes"] = attestation_input.MAX_JSON_BYTES + 1
    receipt_entry["sha256"] = _sha256((directory / "receipt.json").read_bytes())
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)

    import brigade.evidence_package_snapshot as package_snapshot

    real_open = package_snapshot.dirfd.open_child_file

    def refuse_receipt_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        if name == "receipt.json":
            raise AssertionError("oversized declared receipt was opened")
        return real_open(parent, name, flags, mode)

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", refuse_receipt_open)
    _assert_refused(directory, "malformed")


def test_oversized_sparse_companion_is_refused_before_whole_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_package(tmp_path)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    patch = next(entry for entry in entries if entry["path"] == "changes.patch")
    patch["bytes"] = evidence_package.MAX_ENTRY_BYTES + 1
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)

    import brigade.evidence_package_snapshot as package_snapshot

    real_open = package_snapshot.dirfd.open_child_file

    def refuse_patch_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        if name == "changes.patch":
            raise AssertionError("oversized child was opened")
        return real_open(parent, name, flags, mode)

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", refuse_patch_open)
    _assert_refused(directory, "malformed")

    directory = _write_package(tmp_path)
    patch_path = directory / "changes.patch"
    with patch_path.open("r+b") as handle:
        handle.truncate(evidence_package.MAX_ENTRY_BYTES + 1)
    manifest = _manifest(directory)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    manifest["entries_sha256"] = localio.canonical_json_digest(entries)
    _write_manifest(directory, manifest)

    patch_descriptor = -1
    real_read = os.read

    def record_patch_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        nonlocal patch_descriptor
        descriptor = real_open(parent, name, flags, mode)
        if name == "changes.patch":
            patch_descriptor = descriptor
        return descriptor

    def refuse_oversized_patch_read(descriptor: int, count: int) -> bytes:
        if descriptor == patch_descriptor:
            raise AssertionError("oversized child bytes were read")
        return real_read(descriptor, count)

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", record_patch_open)
    monkeypatch.setattr(package_snapshot.os, "read", refuse_oversized_patch_read)
    _assert_refused(directory, "mismatch")
    assert patch_descriptor != -1


def test_opaque_companion_streaming_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contents = b"x" * (2 * 64 * 1024 + 17)
    directory = _write_package(tmp_path, files={"summary.md": contents})
    import brigade.evidence_package_snapshot as package_snapshot

    summary_fd = -1
    requests: list[int] = []
    returned: list[int] = []
    real_open = package_snapshot.dirfd.open_child_file
    real_read = os.read

    def record_summary_open(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        nonlocal summary_fd
        descriptor = real_open(parent, name, flags, mode)
        if name == "summary.md":
            summary_fd = descriptor
        return descriptor

    def record_summary_read(descriptor: int, count: int) -> bytes:
        data = real_read(descriptor, count)
        if descriptor == summary_fd:
            requests.append(count)
            returned.append(len(data))
        return data

    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", record_summary_open)
    monkeypatch.setattr(package_snapshot.os, "read", record_summary_read)
    assert snapshot_package(directory).entries
    assert requests and max(requests) <= 64 * 1024
    assert sum(returned) == len(contents)


def test_snapshot_retains_only_immutable_compact_values(tmp_path: Path) -> None:
    snapshot = snapshot_package(_write_package(tmp_path))

    def has_mutable_or_path(value: object) -> bool:
        if isinstance(value, (dict, list, Path)):
            return True
        if is_dataclass(value):
            return any(has_mutable_or_path(getattr(value, field.name)) for field in fields(value))
        if isinstance(value, tuple):
            return any(has_mutable_or_path(item) for item in value)
        return False

    assert not has_mutable_or_path(snapshot)
    with pytest.raises(AttributeError):
        snapshot.entries[0].path = "changed"  # type: ignore[misc]


def test_embedded_nul_and_runtime_unsupported_primitives_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brigade.evidence_package_snapshot as package_snapshot

    opened = False

    def fail_if_opened(path: Path) -> int:
        nonlocal opened
        opened = True
        raise AssertionError("embedded NUL reached root open")

    monkeypatch.setattr(package_snapshot.dirfd, "open_directory_nofollow", fail_if_opened)
    _assert_refused(Path("embedded\x00nul"), "unsafe")
    assert not opened

    directory = _write_package(tmp_path)

    def unavailable_root(path: Path) -> int:
        raise NotImplementedError

    monkeypatch.setattr(package_snapshot.dirfd, "open_directory_nofollow", unavailable_root)
    _assert_refused(directory, "unavailable")

    monkeypatch.setattr(
        package_snapshot.dirfd,
        "open_directory_nofollow",
        lambda path: (_ for _ in ()).throw(OSError(errno.ENOSYS, "unsupported")),
    )
    _assert_refused(directory, "unavailable")


@pytest.mark.parametrize(
    "operation",
    [
        "scan-open",
        "scan-iteration",
        "child-open",
        "child-read",
        "child-stat",
        "final-child-stat",
        "final-root-stat",
    ],
)
@pytest.mark.parametrize("failure_kind", ["not-implemented", "unsupported-oserror"])
def test_runtime_unsupported_primitives_after_capability_admission_close_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure_kind: str
) -> None:
    directory = _write_package(tmp_path)
    import brigade.evidence_package_snapshot as package_snapshot

    active: set[int] = set()
    child_descriptors: set[int] = set()
    reached = False
    manifest_stats = 0
    real_open = os.open
    real_scandir = os.scandir
    real_stat = os.stat
    real_read = os.read
    real_close = os.close
    real_root_open = package_snapshot.dirfd.open_directory_nofollow
    real_child_open = package_snapshot.dirfd.open_child_file

    def unsupported() -> None:
        nonlocal reached
        reached = True
        if failure_kind == "not-implemented":
            raise NotImplementedError("unsupported")
        raise OSError(errno.ENOSYS, "unsupported")

    def track(descriptor: int) -> int:
        active.add(descriptor)
        return descriptor

    def open_root(path: Path) -> int:
        return track(real_root_open(path))

    def open_child(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
        if operation == "child-open":
            unsupported()
        descriptor = track(real_child_open(parent, name, flags, mode))
        child_descriptors.add(descriptor)
        return descriptor

    def open_scan(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if operation == "scan-open" and path == "." and dir_fd is not None:
            unsupported()
        return track(real_open(path, flags, mode, dir_fd=dir_fd))

    def scan(descriptor: int) -> os.ScandirIterator[str]:
        if operation == "scan-iteration":
            unsupported()
        return real_scandir(descriptor)

    def stat_path(path: str | Path, *, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal manifest_stats
        if operation == "child-stat" and path == "manifest.json" and dir_fd is not None:
            unsupported()
        if operation == "final-child-stat" and path == "manifest.json" and dir_fd is not None:
            manifest_stats += 1
            if manifest_stats == 2:
                unsupported()
        if operation == "final-root-stat" and path == directory and dir_fd is None:
            unsupported()
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def read_child(descriptor: int, count: int) -> bytes:
        if operation == "child-read" and descriptor in child_descriptors:
            unsupported()
        return real_read(descriptor, count)

    def close(descriptor: int) -> None:
        active.discard(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(package_snapshot, "_capabilities_available", lambda: True)
    monkeypatch.setattr(package_snapshot.dirfd, "open_directory_nofollow", open_root)
    monkeypatch.setattr(package_snapshot.dirfd, "open_child_file", open_child)
    monkeypatch.setattr(package_snapshot.os, "open", open_scan)
    monkeypatch.setattr(package_snapshot.os, "scandir", scan)
    monkeypatch.setattr(package_snapshot.os, "stat", stat_path)
    monkeypatch.setattr(package_snapshot.os, "read", read_child)
    monkeypatch.setattr(package_snapshot.os, "close", close)
    supports_dir_fd = set(package_snapshot.os.supports_dir_fd)
    supports_dir_fd.update({open_scan, stat_path})
    monkeypatch.setattr(package_snapshot.os, "supports_dir_fd", supports_dir_fd)
    supports_follow_symlinks = set(package_snapshot.os.supports_follow_symlinks)
    supports_follow_symlinks.add(stat_path)
    monkeypatch.setattr(package_snapshot.os, "supports_follow_symlinks", supports_follow_symlinks)
    assert package_snapshot._capabilities_available()

    _assert_refused(directory, "unavailable")

    assert reached
    assert not active
