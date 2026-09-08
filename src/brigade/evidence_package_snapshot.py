"""Strict, descriptor-anchored snapshots of portable evidence packages."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from . import approval_v2, attestation_input, attestation_receipt, dirfd, evidence_package, localio

_MANIFEST_MAX_BYTES = 64 * 1024
_BUFFER_BYTES = 64 * 1024
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_MANIFEST_KEYS = frozenset(
    {"schema", "schema_version", "created_at", "source", "entries", "entries_sha256", "limitations"}
)
_SOURCE_KEYS = frozenset(
    {
        "verify_run_id",
        "producer_run_id",
        "tree_fingerprint",
        "baseline_commit",
        "changes_patch_sha256",
        "receipt_sha256",
    }
)
_ENTRY_KEYS = frozenset({"path", "sha256", "bytes", "media_type", "kind"})
_LIMITATIONS = ["receipt-contains-local-paths", "integrity-only"]
_ALLOWED = {name: (kind, media_type) for name, kind, media_type in evidence_package.PACKAGE_FILES}
_PACKAGE_NAMES = frozenset({"manifest.json", *_ALLOWED})


class PackageSnapshotError(ValueError):
    """A strict package snapshot could not be produced safely."""

    def __init__(self, code: str):
        if code not in {"unavailable", "unsafe", "malformed", "mismatch", "changed"}:
            raise ValueError("invalid package snapshot error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PackageSource:
    verify_run_id: str
    receipt_sha256: str
    producer_run_id: str | None
    tree_fingerprint: str | None
    baseline_commit: str | None
    changes_patch_sha256: str | None


@dataclass(frozen=True)
class PackageEntry:
    path: str
    sha256: str
    size_bytes: int
    kind: str
    media_type: str


@dataclass(frozen=True)
class PackageSnapshot:
    manifest_bytes: bytes
    manifest_sha256: str
    source: PackageSource
    entries: tuple[PackageEntry, ...]


@dataclass(frozen=True)
class _Observation:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _error(code: str) -> NoReturn:
    raise PackageSnapshotError(code)


def _unsupported_operation(error: OSError) -> bool:
    return error.errno in {errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _capabilities_available() -> bool:
    return (
        os.name == "posix"
        and sys.platform != "win32"
        and dirfd.posix_available()
        and os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_NONBLOCK", 0))
    )


def _observation(value: os.stat_result) -> _Observation:
    try:
        return _Observation(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    except AttributeError:
        _error("unavailable")


def _same_observation(left: _Observation, right: _Observation) -> bool:
    return left == right


def _scan_names(root_fd: int) -> frozenset[str]:
    scan_fd = -1
    try:
        scan_fd = os.open(".", dirfd.directory_flags(), dir_fd=root_fd)
        names: set[str] = set()
        entry_count = 0
        with os.scandir(scan_fd) as iterator:
            for entry in iterator:
                entry_count += 1
                if entry_count > len(_PACKAGE_NAMES) or entry.name not in _PACKAGE_NAMES or entry.name in names:
                    _error("unsafe")
                names.add(entry.name)
        return frozenset(names)
    except PackageSnapshotError:
        raise
    except (NotImplementedError, OSError):
        _error("unavailable")
    finally:
        if scan_fd != -1:
            try:
                os.close(scan_fd)
            except (NotImplementedError, OSError):
                pass


def _read_child(
    root_fd: int,
    name: str,
    *,
    limit: int,
    declared_size: int | None,
    capture: bool,
) -> tuple[bytes | None, str, _Observation]:
    descriptor = -1
    try:
        named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode):
            _error("unsafe")
        descriptor = dirfd.open_child_file(
            root_fd,
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
        initial = _observation(os.fstat(descriptor))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _error("unsafe")
        if initial != _observation(named):
            _error("changed")
        if initial.size < 0 or initial.size > limit or (declared_size is not None and initial.size != declared_size):
            _error("mismatch" if declared_size is not None else "malformed")

        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture else None
        count = 0
        permitted_size = declared_size if declared_size is not None else limit
        while True:
            chunk = os.read(descriptor, min(_BUFFER_BYTES, permitted_size + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > permitted_size:
                _error("mismatch" if declared_size is not None else "malformed")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        if count != initial.size:
            _error("changed")
        final = _observation(os.fstat(descriptor))
        if not _same_observation(initial, final):
            _error("changed")
        return (b"".join(chunks) if chunks is not None else None, digest.hexdigest(), initial)
    except PackageSnapshotError:
        raise
    except (NotImplementedError, OSError):
        _error("unavailable")
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except (NotImplementedError, OSError):
                pass


def _is_run_id(value: object, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and value not in {"", ".", ".."} and bool(approval_v2._RUN_ID_RE.fullmatch(value))


def _is_git_object(value: object) -> bool:
    return value is None or (isinstance(value, str) and bool(approval_v2._HEX40_OR_64_RE.fullmatch(value)))


def _is_digest(value: object, *, nullable: bool) -> bool:
    return (nullable and value is None) or (
        isinstance(value, str) and bool(attestation_receipt._SHA256_RE.fullmatch(value))
    )


def _validate_created_at(value: object) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _validate_manifest(value: object) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        _error("malformed")
    manifest = cast(dict[str, Any], value)
    if (
        manifest["schema"] != evidence_package.SCHEMA
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        _error("malformed")
    if not _validate_created_at(manifest["created_at"]) or manifest["limitations"] != _LIMITATIONS:
        _error("malformed")
    source_value = manifest["source"]
    entries_value = manifest["entries"]
    if not isinstance(source_value, dict) or set(source_value) != _SOURCE_KEYS or not isinstance(entries_value, list):
        _error("malformed")
    source = cast(dict[str, Any], source_value)
    entries: list[dict[str, Any]] = []
    if not _is_run_id(source["verify_run_id"], nullable=False):
        _error("malformed")
    if (
        not _is_run_id(source["producer_run_id"], nullable=True)
        or not _is_git_object(source["tree_fingerprint"])
        or not _is_git_object(source["baseline_commit"])
    ):
        _error("malformed")
    if not _is_digest(source["changes_patch_sha256"], nullable=True) or not _is_digest(
        source["receipt_sha256"], nullable=False
    ):
        _error("malformed")
    if not 1 <= len(entries_value) <= len(_ALLOWED):
        _error("malformed")
    paths: list[str] = []
    for entry_value in entries_value:
        entry = cast(dict[str, Any], entry_value)
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            _error("malformed")
        path = entry["path"]
        size = entry["bytes"]
        if (
            not isinstance(path, str)
            or path not in _ALLOWED
            or type(size) is not int
            or not 0
            <= size
            <= (attestation_input.MAX_JSON_BYTES if path == "receipt.json" else evidence_package.MAX_ENTRY_BYTES)
        ):
            _error("malformed")
        if not _is_digest(entry["sha256"], nullable=False) or (entry["kind"], entry["media_type"]) != _ALLOWED[path]:
            _error("malformed")
        paths.append(path)
        entries.append(entry)
    if paths != sorted(paths) or len(paths) != len(set(paths)) or "receipt.json" not in paths:
        _error("malformed")
    if not _is_digest(manifest["entries_sha256"], nullable=False) or manifest[
        "entries_sha256"
    ] != localio.canonical_json_digest(entries):
        _error("malformed")
    if (source["changes_patch_sha256"] is None) != ("changes.patch" not in paths):
        _error("malformed")
    return manifest, source, entries


def _final_child_checks(root_fd: int, observed: dict[str, _Observation], expected_names: frozenset[str]) -> None:
    if _scan_names(root_fd) != expected_names:
        _error("changed")
    for name, before in observed.items():
        try:
            after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except NotImplementedError:
            _error("unavailable")
        except OSError as error:
            if _unsupported_operation(error):
                _error("unavailable")
            _error("changed")
        if not stat.S_ISREG(after.st_mode):
            _error("unsafe")
        if not _same_observation(before, _observation(after)):
            _error("changed")


def _validate_receipt(data: bytes, source: dict[str, Any]) -> str:
    try:
        parsed = attestation_input.strict_json_loads(data, max_bytes=attestation_input.MAX_JSON_BYTES)
    except attestation_input.AttestationInputError:
        _error("malformed")
    if (
        not isinstance(parsed, dict)
        or type(parsed.get("schema_version")) is not int
        or parsed["schema_version"] != 2
        or not _is_run_id(parsed.get("run_id"), nullable=False)
    ):
        _error("malformed")
    try:
        receipt_snapshot = attestation_receipt.snapshot_stored_receipt(parsed)
    except attestation_receipt.ReceiptDigestError:
        _error("malformed")
    for key in ("verify_run_id", "producer_run_id", "tree_fingerprint", "baseline_commit", "changes_patch_sha256"):
        receipt_key = "run_id" if key == "verify_run_id" else key
        if source[key] != parsed.get(receipt_key):
            _error("mismatch")
    if source["receipt_sha256"] != receipt_snapshot.digest:
        _error("mismatch")
    return receipt_snapshot.digest


def snapshot_package(directory: Path) -> PackageSnapshot:
    """Return an immutable strict snapshot, or a bounded refusal reason."""
    if not _capabilities_available():
        _error("unavailable")
    if "\x00" in os.fspath(directory):
        _error("unsafe")

    root_fd = -1
    try:
        try:
            root_fd = dirfd.open_directory_nofollow(directory)
        except NotImplementedError:
            _error("unavailable")
        except OSError as error:
            if _unsupported_operation(error):
                _error("unavailable")
            _error("unsafe")
        root = _observation(os.fstat(root_fd))
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            _error("unsafe")
        initial_names = _scan_names(root_fd)
        if "manifest.json" not in initial_names:
            _error("malformed")
        manifest_bytes, manifest_digest, manifest_observation = _read_child(
            root_fd, "manifest.json", limit=_MANIFEST_MAX_BYTES, declared_size=None, capture=True
        )
        assert manifest_bytes is not None
        try:
            manifest_value = attestation_input.strict_json_loads(manifest_bytes, max_bytes=_MANIFEST_MAX_BYTES)
        except attestation_input.AttestationInputError:
            _error("malformed")
        _manifest, source, entries = _validate_manifest(manifest_value)
        expected_names = frozenset({"manifest.json", *(entry["path"] for entry in entries)})
        if initial_names != expected_names:
            _error("unsafe")

        observed = {"manifest.json": manifest_observation}
        entry_values: list[PackageEntry] = []
        patch_digest: str | None = None
        receipt_digest: str | None = None
        for entry in entries:
            name = entry["path"]
            data, digest, observation = _read_child(
                root_fd,
                name,
                limit=attestation_input.MAX_JSON_BYTES if name == "receipt.json" else evidence_package.MAX_ENTRY_BYTES,
                declared_size=entry["bytes"],
                capture=name == "receipt.json",
            )
            if digest != entry["sha256"]:
                _error("mismatch")
            observed[name] = observation
            entry_values.append(PackageEntry(name, digest, entry["bytes"], entry["kind"], entry["media_type"]))
            if name == "changes.patch":
                patch_digest = digest
            if name == "receipt.json":
                assert data is not None
                receipt_digest = _validate_receipt(data, source)
                del data

        assert receipt_digest is not None
        if source["changes_patch_sha256"] is not None and patch_digest != source["changes_patch_sha256"]:
            _error("mismatch")

        _final_child_checks(root_fd, observed, expected_names)
        try:
            current_root = os.stat(directory, follow_symlinks=False)
        except NotImplementedError:
            _error("unavailable")
        except OSError as error:
            if _unsupported_operation(error):
                _error("unavailable")
            _error("changed")
        if not stat.S_ISDIR(current_root.st_mode) or not _same_observation(root, _observation(current_root)):
            _error("changed")
        return PackageSnapshot(
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_digest,
            source=PackageSource(
                verify_run_id=source["verify_run_id"],
                receipt_sha256=source["receipt_sha256"],
                producer_run_id=source["producer_run_id"],
                tree_fingerprint=source["tree_fingerprint"],
                baseline_commit=source["baseline_commit"],
                changes_patch_sha256=source["changes_patch_sha256"],
            ),
            entries=tuple(entry_values),
        )
    except PackageSnapshotError:
        raise
    except (NotImplementedError, OSError):
        _error("unavailable")
    finally:
        if root_fd != -1:
            try:
                os.close(root_fd)
            except (NotImplementedError, OSError):
                pass
