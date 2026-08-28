"""Stage 1 compatibility seam for the task and import ledger."""
# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, cast
from uuid import uuid4

from .. import constants, edges as edges_mod, helpers, inbox_lock
from ..inbox_lock import verify_canonical_write_locks
from ... import component_paths, evidence_redaction, provenance, runguard, trust_gate
from ...untrusted import scan_handoff_injection_heuristics

from . import authority_store, import_model

_AUTHORITY_SNAPSHOT_READ_CHUNK_BYTES = 1024 * 1024


def _record_verifier_owned_directory(
    target: Path, *, components: tuple[str, ...], directory: int, workspace: dict[str, int] | None = None
) -> None:
    """Record a child directory created by a verifier-owned producer."""
    authority_store._record_external_directory_authority(
        target,
        components,
        directory,
        workspace=authority_store._external_workspace_directory_identity(target) if workspace is None else workspace,
    )


def _validate_record_bound_directory(record: Mapping[str, Any], *, components: tuple[str, ...], directory: int) -> None:
    """Validate a directory binding against an already-read verified record."""
    directories = record.get("directories")
    if not isinstance(directories, dict) or directories.get(
        authority_store._directory_authority_scope(components)
    ) != authority_store._directory_identity(directory):
        raise OSError("external directory authority record does not match directory")


def _validate_verifier_owned_directory(
    target: Path,
    *,
    components: tuple[str, ...],
    directory: int,
    workspace: dict[str, int] | None = None,
    record: Mapping[str, Any] | None = None,
) -> None:
    """Require that a directory still matches its verifier-owned external record.

    ``record`` consumes an already-read verified snapshot; the authority
    store is never reopened on that path.
    """
    if record is not None:
        _validate_record_bound_directory(record, components=components, directory=directory)
        return
    authority_store._validate_external_directory_authority(
        target,
        components,
        directory,
        workspace=authority_store._external_workspace_directory_identity(target) if workspace is None else workspace,
    )


def _file_identity(descriptor: int, data: bytes) -> dict[str, Any]:
    """Return verifier-owned file identity that cannot be rebuilt from row fields."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("file authority is not a single-link regular file")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _external_file_authorities(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        key: dict(value) if isinstance(value, dict) else value for key, value in files.items() if isinstance(key, str)
    }


def _snapshot_external_file_authorities(target: Path) -> dict[str, Any] | None:
    """Copy the current file-authority map, or None when no workspace record exists.

    A missing record is None. An unreadable or malformed record raises OSError so
    callers fail closed instead of treating a read failure as an empty map.
    """
    _path, payload = authority_store._read_external_directory_authority(target)
    if payload is None:
        return None
    return _external_file_authorities(payload)


def _restore_external_file_authorities(
    target: Path, files: dict[str, Any] | None, *, owned: Sequence[str] | None = None
) -> None:
    """Roll the file-authority map back to a snapshot for the scopes the caller owns.

    Restore is a merge for every key the caller did not touch, so a concurrent
    writer's binding survives a rollback it had nothing to do with. For the
    ``owned`` scopes - the ones the rolled-back operation may have added - it is
    a true restore: a scope absent from the snapshot is deleted rather than left
    behind pointing at a file the rollback just unlinked.

    ``files`` of ``None`` means the snapshot saw no record at all; owned scopes
    are still removed. An unreadable current record raises so rollback cannot
    silently wipe the store.
    """
    owned_scopes = tuple(owned or ())
    path, payload = authority_store._read_external_directory_authority(target)
    if payload is None:
        if files is None:
            return
        raise OSError("external file authority record could not be restored")
    if files is None and not owned_scopes:
        return
    merged = _external_file_authorities(payload)
    original = dict(merged)
    for scope in owned_scopes:
        merged.pop(scope, None)
    merged.update(files or {})
    if merged == original:
        return
    if merged:
        payload["files"] = merged
    else:
        payload.pop("files", None)
    authority_store._write_external_directory_authority(path, payload, workspace=target)


def _record_verifier_owned_file(
    target: Path, *, components: tuple[str, ...], descriptor: int, data: bytes, workspace: dict[str, int] | None = None
) -> None:
    """Record durable file identity after the verifier has published the bytes."""
    bound_workspace = authority_store._external_workspace_directory_identity(target) if workspace is None else workspace
    authority_store._require_workspace_directory_identity(target, bound_workspace)
    with inbox_lock.inbox_writer_lock(target):
        path, existing = authority_store._read_external_directory_authority(target)
        resolved = str(target.expanduser().resolve())
        authority_store._require_workspace_directory_identity(target, bound_workspace)
        if existing is None:
            raise OSError("external directory authority record is missing")
        if (
            existing.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
            or existing.get("target") != resolved
            or existing.get("workspace") != bound_workspace
            or not isinstance(existing.get("directories"), dict)
        ):
            raise OSError("external directory authority record is malformed")
        files = existing.setdefault("files", {})
        if not isinstance(files, dict):
            raise OSError("external file authority record is malformed")
        scope = authority_store._directory_authority_scope(components)
        identity = _file_identity(descriptor, data)
        if files.get(scope) == identity:
            return
        files[scope] = identity
        authority_store._write_external_directory_authority(path, existing, workspace=target)
        if _file_identity(descriptor, data) != identity:
            raise OSError("file identity changed while recording authority")


def _validate_verifier_owned_file(
    target: Path,
    *,
    components: tuple[str, ...],
    descriptor: int,
    data: bytes,
    workspace: dict[str, int] | None = None,
    record: Mapping[str, Any] | None = None,
) -> None:
    """Require that a file still matches its verifier-owned identity and content.

    ``record`` consumes an already-read verified snapshot; the authority
    store is never reopened on that path.
    """
    if record is not None:
        files = record.get("files")
        scope = authority_store._directory_authority_scope(components)
        if not isinstance(files, dict) or files.get(scope) != _file_identity(descriptor, data):
            raise OSError("external file authority record does not match file")
        return
    bound_workspace = authority_store._external_workspace_directory_identity(target) if workspace is None else workspace
    authority_store._require_workspace_directory_identity(target, bound_workspace)
    _path, payload = authority_store._read_external_directory_authority(target)
    files = payload.get("files") if isinstance(payload, dict) else None
    if (
        payload is None
        or payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
        or payload.get("target") != str(target.expanduser().resolve())
        or payload.get("workspace") != bound_workspace
        or not isinstance(payload.get("directories"), dict)
        or not isinstance(files, dict)
        or files.get(authority_store._directory_authority_scope(components)) != _file_identity(descriptor, data)
    ):
        raise OSError("external file authority record does not match file")


def _write_compatibility_directory_anchor(anchor_parent: int, directory: int, *, anchor_name: str) -> None:
    """Retain the old marker for released workspaces. It is never authority."""
    identity = authority_store._directory_identity(directory)
    payload = json.dumps(
        {"schema_version": import_model._DIRECTORY_AUTHORITY_SCHEMA_VERSION, **identity},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        descriptor = authority_store._dirfd_open_file(
            anchor_parent,
            anchor_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        return
    try:
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        authority_store._dirfd_fsync(anchor_parent)
    finally:
        os.close(descriptor)


def _adopt_preexisting_scanner_run_directories(target: Path, root: int, *, workspace: dict[str, int]) -> None:
    """Capture released scanner run directories by identity, never by receipt bytes."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for run_id in os.listdir(root):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", run_id):
            continue
        descriptor = -1
        try:
            descriptor = os.open(run_id, flags, dir_fd=root)
            authority_store._record_external_directory_authority(
                target,
                (".brigade", "scanners", "runs", run_id),
                descriptor,
                workspace=workspace,
            )
        except OSError:
            continue
        finally:
            if descriptor != -1:
                os.close(descriptor)


def _open_legacy_scanner_runs_directory(target: Path) -> int:
    """Adopt a released scanner-runs root without deriving trust from receipts."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(target.expanduser().resolve(), flags)
    workspace = authority_store._directory_identity(descriptor)
    anchor_parent = -1
    try:
        brigade = os.open(".brigade", flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = brigade
        anchor_parent = os.dup(descriptor)
        scanners = os.open("scanners", flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = scanners
        runs = os.open("runs", flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = runs

        _path, payload = authority_store._read_external_directory_authority(target)
        scope = authority_store._directory_authority_scope((".brigade", "scanners", "runs"))
        if payload is not None:
            directories = payload.get("directories")
            if (
                payload.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
                or payload.get("target") != str(target.expanduser().resolve())
                or payload.get("workspace") != workspace
                or not isinstance(directories, dict)
            ):
                raise OSError("external directory authority record is malformed")
            if scope in directories:
                raise OSError("external directory authority record does not match directory")

        authority_store._record_external_directory_authority(
            target,
            (".brigade", "scanners", "runs"),
            descriptor,
            workspace=workspace,
        )
        _adopt_preexisting_scanner_run_directories(target, descriptor, workspace=workspace)
        _write_compatibility_directory_anchor(anchor_parent, descriptor, anchor_name=".runs.authority.json")
        os.close(anchor_parent)
        anchor_parent = -1
        result = descriptor
        descriptor = -1
        return result
    finally:
        if anchor_parent != -1:
            os.close(anchor_parent)
        if descriptor != -1:
            os.close(descriptor)


def _open_verifier_owned_directory(
    target: Path,
    *,
    components: tuple[str, ...],
    anchor_name: str,
    create: bool,
    record: Mapping[str, Any] | None = None,
) -> int:
    """Open an externally bound directory through no-follow descriptors.

    ``record`` validates every binding against one already-read verified
    snapshot instead of the live store: no validation re-read, no recording,
    and no reanchoring happens on that path.
    """
    if not authority_store._dirfd_available():
        raise OSError("descriptor-relative directory authority operations are unavailable")
    descriptor = authority_store._open_directory_nofollow(target.expanduser().resolve())
    workspace = authority_store._directory_identity(descriptor)
    anchor_parent = -1
    parent = -1
    try:
        for component in components[:-2]:
            try:
                child = authority_store._dirfd_open_dir(descriptor, component)
            except FileNotFoundError:
                if not create:
                    raise
                authority_store._dirfd_mkdir(descriptor, component)
                child = authority_store._dirfd_open_dir(descriptor, component)
            os.close(descriptor)
            descriptor = child
        anchor_parent = descriptor
        descriptor = -1
        parent_name = components[-2]
        try:
            parent = authority_store._dirfd_open_dir(anchor_parent, parent_name)
        except FileNotFoundError:
            if not create:
                raise
            authority_store._dirfd_mkdir(anchor_parent, parent_name)
            parent = authority_store._dirfd_open_dir(anchor_parent, parent_name)
        name = components[-1]
        created = False
        try:
            child = authority_store._dirfd_open_dir(parent, name)
        except FileNotFoundError:
            if not create:
                raise
            authority_store._dirfd_mkdir(parent, name)
            child = authority_store._dirfd_open_dir(parent, name)
            created = True
        try:
            try:
                if record is None:
                    authority_store._validate_external_directory_authority(
                        target, components, child, workspace=workspace
                    )
                else:
                    _validate_record_bound_directory(record, components=components, directory=child)
            except OSError:
                if record is not None:
                    raise
                if created:
                    authority_store._record_external_directory_authority(target, components, child, workspace=workspace)
                elif authority_store._reanchor_external_directory_authority(
                    target, components, child, workspace=workspace
                ):
                    authority_store._validate_external_directory_authority(
                        target, components, child, workspace=workspace
                    )
                else:
                    # A directory that already existed and is not bound to this
                    # workspace record is never adopted, including on the create
                    # path: a pre-created directory may be an attacker's inode.
                    raise
            _write_compatibility_directory_anchor(anchor_parent, child, anchor_name=anchor_name)
            os.close(parent)
            parent = -1
            os.close(anchor_parent)
            anchor_parent = -1
            return child
        except BaseException:
            os.close(child)
            raise
    except BaseException:
        if parent != -1:
            os.close(parent)
        if anchor_parent != -1:
            os.close(anchor_parent)
        if descriptor != -1:
            os.close(descriptor)
        raise


def _open_import_proof_directory(target: Path, *, create: bool, record: Mapping[str, Any] | None = None) -> int:
    """Open the verifier-owned proof directory through no-follow descriptors."""
    return _open_verifier_owned_directory(
        target,
        components=(".brigade", "work", "imports", "proofs"),
        anchor_name=".proofs.authority.json",
        create=create,
        record=record,
    )


def _validate_import_proof_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("import proof is not a single-link regular file")


def _validate_import_proof_name_matches_descriptor(parent: int, name: str, descriptor: int) -> None:
    """Require the published sidecar name to still identify its held descriptor."""
    _validate_import_proof_descriptor(descriptor)
    named = authority_store._dirfd_stat(parent, name)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (named.st_dev, named.st_ino, named.st_mode, named.st_nlink)
        != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink)
    ):
        raise OSError("import proof name no longer matches its held descriptor")


def _persisted_import_proof_scopes(items: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return the file-authority scopes a proof publication for ``items`` would own."""
    scopes: list[str] = []
    for item in items:
        name = import_model._import_proof_name(item.get("id"))
        if name is None:
            continue
        scopes.append(authority_store._directory_authority_scope((".brigade", "work", "imports", "proofs", name)))
    return tuple(scopes)


def _persisted_import_proof_payload(item: dict[str, Any], *, operation_id: str) -> dict[str, Any] | None:
    item_id = item.get("id")
    source = item.get("source")
    if not isinstance(item_id, str) or not item_id or not isinstance(source, str) or not source:
        return None
    return {
        "schema_version": import_model._IMPORT_PROOF_SCHEMA_VERSION,
        "item_id": item_id,
        "importer_source": source,
        "content_hash": import_model._locally_stamped_import_content_hash(item),
        "operation_id": operation_id,
    }


def _write_persisted_import_proofs(target: Path, items: list[dict[str, Any]], *, operation_id: str) -> None:
    """Record verifier-owned proof only after the corresponding inbox write succeeds."""
    parent = _open_import_proof_directory(target, create=True)
    created: list[str] = []
    published: list[tuple[str, bytes]] = []
    prior_files = _snapshot_external_file_authorities(target)
    try:
        for item in items:
            payload = _persisted_import_proof_payload(item, operation_id=operation_id)
            name = import_model._import_proof_name(item.get("id"))
            if payload is None or name is None:
                raise OSError("cannot persist import proof for malformed local item")
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            descriptor = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            created.append(name)
            try:
                with os.fdopen(os.dup(descriptor), "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                _validate_import_proof_descriptor(descriptor)
                _validate_import_proof_name_matches_descriptor(parent, name, descriptor)
            finally:
                os.close(descriptor)
            published.append((name, data))
        authority_store._dirfd_fsync(parent)
        for name, data in published:
            descriptor = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                _validate_import_proof_name_matches_descriptor(parent, name, descriptor)
                _record_verifier_owned_file(
                    target,
                    components=(".brigade", "work", "imports", "proofs", name),
                    descriptor=descriptor,
                    data=data,
                )
            finally:
                os.close(descriptor)
    except BaseException:
        for name in created:
            try:
                authority_store._dirfd_unlink(parent, name)
            except FileNotFoundError:
                pass
        authority_store._dirfd_fsync(parent)
        try:
            _restore_external_file_authorities(
                target,
                prior_files,
                owned=[
                    authority_store._directory_authority_scope((".brigade", "work", "imports", "proofs", name))
                    for name in created
                ],
            )
        except OSError as exc:
            raise OSError("import proof binding rollback could not restore its retained snapshot") from exc
        raise
    finally:
        os.close(parent)


def _remove_persisted_import_proofs(target: Path, items: list[dict[str, Any]]) -> None:
    """Remove sidecars created for a failed import publication transaction."""
    try:
        parent = _open_import_proof_directory(target, create=False)
    except OSError:
        return
    try:
        for item in items:
            name = import_model._import_proof_name(item.get("id"))
            if name is None:
                continue
            try:
                authority_store._dirfd_unlink(parent, name)
            except FileNotFoundError:
                pass
        authority_store._dirfd_fsync(parent)
    finally:
        os.close(parent)


def _has_persisted_import_proof(
    item: dict[str, Any], *, target: Path | None = None, record: Mapping[str, Any] | None = None
) -> bool:
    """Verify the external local-import proof without trusting row-controlled paths.

    ``record`` validates the proof-directory and sidecar bindings against one
    already-read verified snapshot; the authority store is never reopened.
    """
    if target is None:
        return False
    name = import_model._import_proof_name(item.get("id"))
    if name is None:
        return False
    try:
        parent = _open_import_proof_directory(target, create=False, record=record)
    except OSError:
        return False
    try:
        try:
            descriptor = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            return False
        try:
            _validate_import_proof_descriptor(descriptor)
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_mode, before.st_nlink) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_nlink,
            ):
                return False
            _validate_import_proof_name_matches_descriptor(parent, name, descriptor)
            _validate_verifier_owned_file(
                target,
                components=(".brigade", "work", "imports", "proofs", name),
                descriptor=descriptor,
                data=data,
                record=record,
            )
            payload = json.loads(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    if not isinstance(payload, dict):
        return False
    operation_id = payload.get("operation_id")
    expected = _persisted_import_proof_payload(item, operation_id=operation_id if isinstance(operation_id, str) else "")
    return (
        expected is not None
        and isinstance(operation_id, str)
        and import_model._IMPORT_PROOF_OPERATION_PATTERN.fullmatch(operation_id) is not None
        and payload == expected
    )


def _import_content_identity(item: dict[str, Any]) -> tuple[str, str] | None:
    """Return the immutable-content identity used to migrate sanitized imports."""
    kind = item.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    return kind, import_model._untrusted_import_canonical_hash(item)


def _has_local_import_envelope(item: dict[str, Any], *, importer_source: str) -> bool:
    """Validate the immutable provenance envelope emitted by the local inbox writer."""
    source = item.get("source")
    text = item.get("text")
    metadata = item.get("metadata")
    if source != importer_source or not isinstance(text, str) or not isinstance(metadata, dict):
        return False
    envelope = metadata.get("provenance")
    if provenance.validate_envelope(envelope):
        return False
    envelope_source = envelope.get("source") if isinstance(envelope, Mapping) else None
    envelope_trust = envelope.get("trust") if isinstance(envelope, Mapping) else None
    envelope_hashes = envelope.get("hashes") if isinstance(envelope, Mapping) else None
    return (
        isinstance(envelope_source, Mapping)
        and envelope_source.get("system") == "work-inbox"
        and envelope_source.get("kind") == importer_source
        and envelope_source.get("producer") == "ledger._make_import"
        and isinstance(envelope_trust, Mapping)
        and envelope_trust.get("assigned_by") == "ingest:ledger._make_import"
        and isinstance(envelope_hashes, Mapping)
        and envelope_hashes.get("content") == provenance.content_sha256(text)
    )


def _read_local_scanner_receipt(
    target: Path, scanner_run_id: str, *, record: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Read one local scanner receipt through no-follow descriptor traversal.

    ``record`` validates every binding against one already-read verified
    snapshot instead of reopening the authority store.
    """
    if (
        os.name != "posix"
        or not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", scanner_run_id)
    ):
        return None
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    root = -1
    current = -1
    receipt_descriptor = -1
    try:
        root = _open_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs"),
            anchor_name=".runs.authority.json",
            create=False,
            record=record,
        )
        current = os.open(scanner_run_id, directory_flags, dir_fd=root)
        opened_run = os.fstat(current)
        named_run = os.stat(scanner_run_id, dir_fd=root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_run.st_mode)
            or not stat.S_ISDIR(named_run.st_mode)
            or (opened_run.st_dev, opened_run.st_ino) != (named_run.st_dev, named_run.st_ino)
        ):
            return None
        _validate_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs", scanner_run_id),
            directory=current,
            record=record,
        )
        receipt_descriptor = os.open(
            "receipt.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current,
        )
        before = os.fstat(receipt_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        chunks: list[bytes] = []
        while chunk := os.read(receipt_descriptor, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(receipt_descriptor)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_nlink) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
        ):
            return None
        named_run = os.stat(scanner_run_id, dir_fd=root, follow_symlinks=False)
        if not stat.S_ISDIR(named_run.st_mode) or (opened_run.st_dev, opened_run.st_ino) != (
            named_run.st_dev,
            named_run.st_ino,
        ):
            return None
        _validate_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs", scanner_run_id),
            directory=current,
            record=record,
        )
        data = b"".join(chunks)
        _validate_verifier_owned_file(
            target,
            components=(".brigade", "scanners", "runs", scanner_run_id, "receipt.json"),
            descriptor=receipt_descriptor,
            data=data,
            record=record,
        )
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("run_id") != scanner_run_id:
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if receipt_descriptor != -1:
            os.close(receipt_descriptor)
        if current != -1 and current != root:
            os.close(current)
        if root != -1:
            os.close(root)


def _has_locally_stamped_import_proof(
    item: dict[str, Any], *, target: Path | None = None, record: Mapping[str, Any] | None = None
) -> bool:
    """Return whether a verifier-owned scanner receipt binds this exact row.

    ``record`` validates the receipt bindings against one already-read
    verified snapshot; the authority store is never reopened.
    """
    if target is None:
        return False
    source = item.get("source")
    metadata = item.get("metadata")
    if not isinstance(source, str) or not source or not isinstance(metadata, dict):
        return False
    scanner_run_id = metadata.get("scanner_run_id")
    scanner_id = metadata.get("scanner_id")
    if not isinstance(scanner_run_id, str) or not scanner_run_id or not isinstance(scanner_id, str) or not scanner_id:
        return False
    if not _has_local_import_envelope(item, importer_source=source):
        return False
    receipt = _read_local_scanner_receipt(target, scanner_run_id, record=record)
    if (
        receipt is None
        or receipt.get("run_id") != scanner_run_id
        or receipt.get("status") != "completed"
        or receipt.get("exit_code") != 0
    ):
        return False
    proof = receipt.get("self_import_proofs")
    if not isinstance(proof, dict) or proof.get("scanner_id") != scanner_id or proof.get("source") != source:
        return False
    scanner = proof.get("scanner")
    if not isinstance(scanner, dict):
        return False
    # Public SCANNER_DEFAULTS equality is not producer authentication. The
    # receipt must already be verifier-owned (HMAC store) and structurally
    # consistent with this row.
    if (
        receipt.get("scanner_id") != scanner_id
        or receipt.get("source") != source
        or receipt.get("command") != scanner.get("command")
        or scanner.get("id") != scanner_id
        or scanner.get("source") != source
    ):
        return False
    content_hash = import_model._locally_stamped_import_content_hash(item)
    imports = proof.get("imports")
    return isinstance(imports, list) and any(
        isinstance(entry, dict) and entry.get("id") == item.get("id") and entry.get("content_hash") == content_hash
        for entry in imports
    )


def _read_verified_authority_snapshot(target: Path | None) -> dict[str, Any] | None:
    """Read the authority store once and return its record only from a signed payload.

    Signedness and the verified record come from a single read of the same
    bytes: the raw payload must carry an HMAC envelope and that exact
    envelope must verify. A concurrent same-uid writer cannot swap bytes
    between a verified read and an envelope_version re-check.
    """
    if target is None:
        return None
    path = authority_store._directory_authority_store_path(target)
    try:
        descriptor = authority_store._open_file_nofollow(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _AUTHORITY_SNAPSHOT_READ_CHUNK_BYTES):
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("envelope_version") != 1
        or not isinstance(payload.get("record"), dict)
    ):
        return None
    try:
        record = authority_store._unwrap_authority_envelope(path, payload, workspace=target)
    except OSError:
        return None
    return record if isinstance(record, dict) else None


def _acquire_authenticated_authority_snapshot(target: Path | None) -> dict[str, Any] | None:
    """Reanchor relocated directories once, then read exactly one signed record.

    The authority store may legitimately be missing right after a workspace
    rename until a bound directory open copies the previous workspace's
    record, so the reanchoring opens run before this single verified read.
    Callers must pass the returned record into every downstream validator:
    reopening the store mid-validation lets a concurrent same-uid writer
    combine evidence that never existed in one authenticated snapshot.
    """
    if target is None:
        return None
    try:
        workspace = authority_store._workspace_directory_identity(target)
    except OSError:
        return None
    for components, anchor_name in (
        ((".brigade", "scanners", "runs"), ".runs.authority.json"),
        ((".brigade", "work", "imports", "proofs"), ".proofs.authority.json"),
    ):
        try:
            descriptor = _open_verifier_owned_directory(
                target, components=components, anchor_name=anchor_name, create=False
            )
        except OSError:
            continue
        os.close(descriptor)
    record = _read_verified_authority_snapshot(target)
    if record is None:
        return None
    if (
        record.get("schema_version") != import_model._EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION
        or record.get("target") != str(target.expanduser().resolve())
        or record.get("workspace") != workspace
        or not isinstance(record.get("directories"), dict)
    ):
        return None
    return record


def _authority_store_binding_is_verifier_signed(target: Path | None) -> bool:
    """Return whether the external authority record is a verifier-signed HMAC envelope.

    Receipt and proof file identities recorded in an unsigned store are
    scanner-reproducible bytes that a same-uid attacker can re-bind after the
    run exits. Legacy migration may only trust bindings whose authority record
    itself carries a verified HMAC envelope.
    """
    return _read_verified_authority_snapshot(target) is not None


_APPEND_SNAPSHOT_ABSENT = cast("Mapping[str, Any]", object())
"""Sentinel for append-path callers whose single snapshot acquisition already
ran and legitimately found no signed record. Passing it instead of ``None``
tells :func:`_authenticated_legacy_import_proof` never to re-acquire, so one
append consumes exactly one acquisition (#881 round 4)."""


def _authenticated_legacy_import_proof(
    item: dict[str, Any], *, target: Path | None = None, record: Mapping[str, Any] | None = None
) -> bool:
    """Shared authenticated-legacy predicate: receipt, sidecar, and signed store.

    Every legacy identity path must require a verifier-owned scanner receipt,
    a persisted proof sidecar, and a signed authority record from one
    verified snapshot before trusting scanner-reproducible bindings. With
    ``record`` unset the single snapshot is acquired here after one
    reanchor pass; callers holding a snapshot must pass it back so the
    receipt, directory, and sidecar validators all consume that same record
    instead of independently reopening the store. Append-path callers pass
    :data:`_APPEND_SNAPSHOT_ABSENT` once their own acquisition has run and
    legitimately returned nothing — treating that as unset here would reopen
    the authority state mid-append.
    """
    if record is _APPEND_SNAPSHOT_ABSENT:
        record = None
    elif record is None:
        record = _acquire_authenticated_authority_snapshot(target)
    if target is None or record is None:
        return False
    if not _has_locally_stamped_import_proof(item, target=target, record=record):
        return False
    return _has_persisted_import_proof(item, target=target, record=record)


_UNSIGNED_DEDUPE_DOWNGRADE_WARNED = False


def _unsigned_dedupe_proof_for_non_isolated_workspace(target: Path | None) -> bool:
    """Return whether canonical dedupe may fall back to the unsigned proof path.

    Without ``authority_store.isolation = "external-key"`` no verifier-signed
    store can exist for this workspace, and a same-uid writer can already
    rewrite the import inbox directly (issue #1093 tracks that boundary), so
    canonical dedupe suppression degrades to the pre-#881 evidence grade:
    the persisted sidecar validated against the workspace's own authority
    record — receipt bytes are not revalidated on this fallback path. The
    first selection per process prints one warning naming that downgrade.

    The repo-writable selector alone is not trusted (#881): the fallback is
    also refused when the workspace configuration exists but cannot be parsed
    (invalid input normalizes to ``off``), and whenever the user-level
    isolation posture marker for this target is not positively confirmed
    absent (#881 round 4). Marker reads are tri-state: only one positively
    read healthy marker store that names no governing marker admits the
    fallback; an unreadable or malformed marker store reports unknown, which
    is treated like present. Markers bind the stable workspace root identity,
    so a renamed workspace stays governed and an unrelated workspace at the
    recycled path is not blocked by a stale path-keyed marker. Only the
    explicit, audited ``brigade security authority downgrade`` command clears
    that marker.
    """
    global _UNSIGNED_DEDUPE_DOWNGRADE_WARNED
    if target is None:
        return False
    from ... import authority_marker
    from ...security_cmd.config import authority_store_isolation_state
    from ...security_cmd.models import AUTHORITY_STORE_ISOLATION_OFF

    try:
        mode, healthy = authority_store_isolation_state(target)
    except OSError:
        # Observation failed closed (posture persistence refused); never
        # degrade to the unsigned grade over a broken posture channel.
        return False
    if mode != AUTHORITY_STORE_ISOLATION_OFF or not healthy:
        return False
    try:
        live_identity = authority_store._workspace_directory_identity(target)
    except OSError:
        return False
    status = authority_marker.isolation_marker_status(target, workspace_identity=live_identity)
    if status != authority_marker.MARKER_STATUS_CONFIRMED_ABSENT:
        return False
    if not _UNSIGNED_DEDUPE_DOWNGRADE_WARNED:
        _UNSIGNED_DEDUPE_DOWNGRADE_WARNED = True
        print(
            'warning: authority_store.isolation is not "external-key"; canonical import'
            " dedupe suppression is downgraded to unsigned persisted-proof evidence"
            " (persisted sidecar) in this workspace",
            file=sys.stderr,
        )
    return True
