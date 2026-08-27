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

from . import tasks_plans, authority_store, descriptor_anchors, import_model, locking


def _metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _legacy_import_source_content_identity(
    item: dict[str, Any], *, target: Path | None = None, record: Mapping[str, Any] | None = None
) -> tuple[str, str, str] | None:
    """Return a legacy identity only when local provenance establishes its source."""
    if not descriptor_anchors._authenticated_legacy_import_proof(item, target=target, record=record):
        return None
    source = item["source"]
    content_identity = descriptor_anchors._import_content_identity(item)
    if content_identity is None:
        return None
    kind, content_hash = content_identity
    return source, kind, content_hash


def _open_import_inbox_parent(target: Path, *, create: bool) -> tuple[int, str]:
    """Hold the inbox parent for an import/proof publication transaction."""
    if authority_store._posix_dirfd_available():
        return _open_import_inbox_parent_posix(target, create=create)
    if authority_store._nt_dirfd_available():
        return _open_import_inbox_parent_nt(target, create=create)
    raise OSError("descriptor-relative import inbox operations are unavailable")


def _open_import_inbox_parent_posix(target: Path, *, create: bool) -> tuple[int, str]:
    """POSIX openat walk that rejects every symlinked inbox parent component."""
    target_root = target.expanduser().resolve()
    inbox_path = helpers._imports_path(target_root)
    try:
        relative = inbox_path.relative_to(target_root)
    except ValueError as exc:
        raise OSError("import inbox escapes target") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent = os.open(target_root, flags)
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=parent)
                child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, relative.parts[-1]
    except BaseException:
        os.close(parent)
        raise


def _open_import_inbox_parent_nt(target: Path, *, create: bool) -> tuple[int, str]:
    """Windows handle walk that rejects every reparse-point inbox parent component."""
    from .. import nt_dirfd

    target_root = target.expanduser().resolve()
    inbox_path = helpers._imports_path(target_root)
    try:
        relative = inbox_path.relative_to(target_root)
    except ValueError as exc:
        raise OSError("import inbox escapes target") from exc
    parent = nt_dirfd.open_root_directory(target_root)
    try:
        for component in relative.parts[:-1]:
            nt_dirfd.validate_component(component)
            try:
                child = nt_dirfd.open_child_directory(parent, component)
            except FileNotFoundError:
                if not create:
                    raise
                nt_dirfd.mkdir_child(parent, component)
                child = nt_dirfd.open_child_directory(parent, component)
            os.close(parent)
            parent = child
        return parent, nt_dirfd.validate_component(relative.parts[-1])
    except BaseException:
        os.close(parent)
        raise


def _validate_import_inbox_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("import inbox is not a single-link regular file")


def _validate_import_inbox_name_matches_descriptor(parent: int, name: str, descriptor: int) -> None:
    """Ensure a directory entry still names the retained regular inbox object."""
    if not authority_store._dirfd_available():
        raise OSError("descriptor-relative import inbox validation is unavailable")
    opened = os.fstat(descriptor)
    named = authority_store._dirfd_stat(parent, name)
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (named.st_dev, named.st_ino, named.st_mode, named.st_nlink)
        != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink)
    ):
        raise OSError("import inbox name no longer matches its held descriptor")


def _write_import_inbox_bytes_at(
    parent: int,
    name: str,
    data: bytes,
    *,
    previous_raw: bytes | None = None,
    previous_exists: bool | None = None,
) -> None:
    """Atomically publish import bytes through the transaction's held parent."""
    existing = -1
    descriptor = -1
    temporary_name = f".{name}.{uuid4().hex}.tmp"
    try:
        try:
            existing = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            pass
        else:
            _validate_import_inbox_descriptor(existing)
            _validate_import_inbox_name_matches_descriptor(parent, name, existing)
            if previous_raw is None:
                chunks: list[bytes] = []
                while chunk := os.read(existing, 1024 * 1024):
                    chunks.append(chunk)
                previous_raw = b"".join(chunks)
            if previous_exists is None:
                previous_exists = True
        if previous_exists is None:
            previous_exists = False
        if previous_raw is None:
            previous_raw = b""
        descriptor = authority_store._dirfd_open_file(
            parent,
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        _validate_import_inbox_descriptor(descriptor)
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_import_inbox_descriptor(descriptor)
        authority_store._dirfd_replace(parent, temporary_name, name)
        _validate_import_inbox_name_matches_descriptor(parent, name, descriptor)
        authority_store._dirfd_fsync(parent)
    except BaseException:
        try:
            authority_store._dirfd_unlink(parent, temporary_name)
        except FileNotFoundError:
            pass
        _restore_import_inbox_snapshot(parent, name, previous_raw or b"", bool(previous_exists))
        raise
    finally:
        if existing != -1:
            os.close(existing)
        if descriptor != -1:
            os.close(descriptor)


def _snapshot_import_inbox(target: Path) -> tuple[int, str, bytes, bool]:
    """Capture exact inbox bytes while retaining authority to restore them."""
    parent, name = _open_import_inbox_parent(target, create=True)
    descriptor = -1
    try:
        try:
            descriptor = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return parent, name, b"", False
        _validate_import_inbox_descriptor(descriptor)
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        snapshot_total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            snapshot_total += len(chunk)
            if snapshot_total > locking._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES:
                raise locking.ImportInboxSnapshotLimitExceeded(
                    f"import inbox exceeds {locking._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES} byte snapshot limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("import inbox changed while snapshotting")
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise OSError("import inbox changed while snapshotting")
        return parent, name, raw, True
    except BaseException:
        os.close(parent)
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _restore_import_inbox_snapshot(parent: int, name: str, data: bytes, exists: bool) -> None:
    """Restore an ordinary import transaction through its original parent."""
    if not exists:
        try:
            authority_store._dirfd_unlink(parent, name)
        except FileNotFoundError:
            pass
        authority_store._dirfd_fsync(parent)
        return
    for _attempt in range(3):
        temporary_name = f".{name}.{uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = authority_store._dirfd_open_file(
                parent,
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_import_inbox_descriptor(descriptor)
            _validate_import_inbox_name_matches_descriptor(parent, temporary_name, descriptor)
            authority_store._dirfd_replace(parent, temporary_name, name)
            temporary_name = ""
            _validate_import_inbox_name_matches_descriptor(parent, name, descriptor)
            authority_store._dirfd_fsync(parent)
            return
        except OSError:
            pass
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_name:
                try:
                    authority_store._dirfd_unlink(parent, temporary_name)
                except FileNotFoundError:
                    pass
    descriptor = -1
    try:
        descriptor = authority_store._dirfd_open_file(
            parent,
            name,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        _validate_import_inbox_descriptor(descriptor)
        os.ftruncate(descriptor, 0)
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("import inbox rollback write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        _validate_import_inbox_name_matches_descriptor(parent, name, descriptor)
        authority_store._dirfd_fsync(parent)
        return
    except OSError:
        try:
            authority_store._dirfd_unlink(parent, name)
        except FileNotFoundError:
            pass
        authority_store._dirfd_fsync(parent)
        raise OSError("import inbox rollback could not restore its retained snapshot") from None
    finally:
        if descriptor != -1:
            os.close(descriptor)


# Central envelope stamps land under metadata.provenance after identity is fixed.
# Exclude them from dedupe fingerprints so repeated imports stay idempotent.
_IMPORT_FINGERPRINT_METADATA_SKIP = frozenset(
    {
        "source_fingerprint",
        "sweep_path",
        "queue_path",
        "provenance",
    }
)


# Default origin/modality for work-inbox producers. Authoritative source,
# origin, and modality always come from this mapping (plus the local
# work-inbox producer stamp). Validated inbound envelopes may reuse only
# non-authoritative identity fields. Unknown sources fail closed to
# origin ``unknown``; modality still defaults to tool-output.
_IMPORT_SOURCE_ORIGIN_MODALITY: dict[str, tuple[str, str]] = {
    "manual": ("operator-input", "human-written"),
    "backup-health": ("workspace", "tool-output"),
    "chat-memory-sweep": ("agent-session", "mixed"),
    "code-review": ("workspace", "tool-output"),
    "context-pack": ("workspace", "tool-output"),
    "handoff-ingest": ("agent-session", "mixed"),
    "learning-loop": ("workspace", "tool-output"),
    "memory-care": ("workspace", "tool-output"),
    "memory-refresh": ("workspace", "tool-output"),
    "project-consolidation": ("workspace", "tool-output"),
    "repo-fleet": ("workspace", "tool-output"),
    "repo-fleet-release": ("workspace", "tool-output"),
    "roadmap-audit": ("workspace", "tool-output"),
    "scanner-health": ("workspace", "tool-output"),
    "security-scan": ("workspace", "tool-output"),
    "tool-catalog": ("workspace", "tool-output"),
}


def _import_fingerprint(item: dict[str, Any]) -> str | None:
    metadata = _metadata(item)
    value = metadata.get("source_fingerprint")
    if isinstance(value, str) and value.strip():
        return value.strip()
    source_key = import_model._import_source_key(item)
    if not source_key:
        return None
    return helpers._stable_hash(
        {
            "text": item.get("text"),
            "kind": item.get("kind"),
            "type": item.get("type"),
            "priority": item.get("priority"),
            "template": item.get("template"),
            "acceptance": item.get("acceptance"),
            "metadata": {key: value for key, value in metadata.items() if key not in _IMPORT_FINGERPRINT_METADATA_SKIP},
        }
    )


def _import_origin_modality(source: str, *, kind: str | None = None) -> tuple[str, str]:
    cleaned = source.strip().lower() if isinstance(source, str) else ""
    if kind == "context":
        origin = evidence_redaction.origin_for_external_ingest(source)
    else:
        origin = evidence_redaction.classify_source_origin(source)
    modality = _IMPORT_SOURCE_ORIGIN_MODALITY.get(cleaned, ("workspace", "tool-output"))[1]
    return origin, modality


_IMPORT_IDENTITY_MAX_LEN = 256
_IMPORT_CAPTURED_AT_MAX_LEN = 64


class _ImportProvenanceError(ValueError):
    """Raised when local provenance stamping fails for one import record."""


_IMPORT_PROVENANCE_REJECTION_REASON = "provenance_stamp_failed"


def _bound_import_identity(value: object, *, max_len: int = _IMPORT_IDENTITY_MAX_LEN) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned.encode("utf-8")) > max_len:
        return None
    return cleaned


def _safe_import_identity(value: object, *, max_len: int = _IMPORT_IDENTITY_MAX_LEN) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    cleaned = _bound_import_identity(value, max_len=max_len)
    if cleaned is None or not provenance.is_safe_identity_label(cleaned):
        return None
    return cleaned


def _resolve_import_provenance_source(
    record: dict[str, Any],
    provenance_source: str | None,
) -> str:
    record_source = str(record.get("source") or "manual").strip() or "manual"
    if provenance_source is None:
        return record_source
    return provenance_source.strip() or "learning-loop"


def _repository_id_is_unsafe(value: str) -> bool:
    return not provenance.is_safe_identity_label(value)


def _safe_repository_id(value: object) -> str | None:
    cleaned = _safe_import_identity(value)
    if cleaned is None or _repository_id_is_unsafe(cleaned):
        return None
    return cleaned


def _safe_repository_revision(value: object) -> str | None:
    """Return a bounded revision label, never a platform-rooted locator."""
    return str(value) if provenance.is_safe_repository_revision(value) else None


def _import_repository_fields(metadata: dict[str, Any]) -> tuple[str, str | None]:
    repo = metadata.get("repository")
    if isinstance(repo, dict):
        repo_id = _safe_repository_id(repo.get("id"))
        if repo_id is not None:
            revision = _safe_repository_revision(repo.get("revision"))
            return repo_id, revision
    for key in ("repository_id", "repo_id"):
        repo_id = _safe_repository_id(metadata.get(key))
        if repo_id is not None:
            revision = _safe_repository_revision(metadata.get("repository_revision"))
            return repo_id, revision
    return "unknown", None


def _import_session_fields(metadata: dict[str, Any]) -> tuple[str | None, str | None]:
    session = metadata.get("session")
    if isinstance(session, dict):
        session_id = _safe_import_identity(session.get("id"))
        harness = _safe_import_identity(session.get("harness"))
        return session_id, harness
    session_id = _safe_import_identity(metadata.get("session_id"))
    harness = _safe_import_identity(metadata.get("session_harness"))
    return session_id, harness


def _locator_is_unsafe(kind: object, value: str) -> bool:
    if provenance.is_absolute_locator(value):
        return True
    if kind == "repo-relative" and ".." in value.replace("\\", "/").split("/"):
        return True
    return False


def _safe_locator_fields(
    *,
    locator_kind: object,
    locator_value: object,
    fallback_item_id: str,
) -> tuple[str, str]:
    if (
        locator_kind not in provenance.LOCATOR_KINDS
        or not isinstance(locator_value, str)
        or not locator_value.strip()
        or _locator_is_unsafe(locator_kind, locator_value.strip())
    ):
        return "uri", f"work-import:{fallback_item_id}"
    bounded = _bound_import_identity(locator_value.strip())
    if bounded is None:
        return "uri", f"work-import:{fallback_item_id}"
    return str(locator_kind), bounded


def _sanitize_import_identity_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop locator-shaped identity hints before persisting importer metadata."""
    sanitized = dict(metadata)
    for key in ("repository_id", "repo_id"):
        if key in sanitized and _safe_repository_id(sanitized[key]) is None:
            sanitized.pop(key)
    if "repository_revision" in sanitized and _safe_repository_revision(sanitized["repository_revision"]) is None:
        sanitized.pop("repository_revision")
    repository = sanitized.get("repository")
    if isinstance(repository, dict):
        repository_id = _safe_repository_id(repository.get("id"))
        if repository_id is None:
            sanitized.pop("repository", None)
        else:
            cleaned_repository = dict(repository)
            cleaned_repository["id"] = repository_id
            revision = _safe_repository_revision(cleaned_repository.get("revision"))
            if cleaned_repository.get("revision") is not None and revision is None:
                cleaned_repository.pop("revision", None)
            sanitized["repository"] = cleaned_repository

    for key in (
        "session_id",
        "session_harness",
        "collection_id",
        "item_id",
        "source_item_key",
        "source_item_id",
        "scanner_item_id",
    ):
        if key in sanitized and _safe_import_identity(sanitized[key]) is None:
            sanitized.pop(key)
    session = sanitized.get("session")
    if isinstance(session, dict):
        cleaned_session = dict(session)
        for key in ("id", "harness"):
            if key in cleaned_session and _safe_import_identity(cleaned_session[key]) is None:
                cleaned_session.pop(key)
        if cleaned_session:
            sanitized["session"] = cleaned_session
        else:
            sanitized.pop("session", None)
    return sanitized


def _stamp_inferred_import_provenance(
    *,
    text: str,
    source: str,
    item_id: str,
    metadata: dict[str, Any],
    ingested_at: str,
) -> dict[str, Any]:
    """Backfill an inferred envelope. Never assigns reviewed or verified."""
    origin, modality = _import_origin_modality(source)
    try:
        hits = scan_handoff_injection_heuristics(text)
        warnings = [hit for hit in hits if hit.severity == "warning"]
        if warnings:
            trust_label, injection_status = "quarantined", "flagged"
            injection_count = len(warnings)
            injection_rules = sorted({hit.rule for hit in warnings if isinstance(hit.rule, str) and hit.rule})
        else:
            trust_label, injection_status, injection_count, injection_rules = "unknown", "clean", 0, []
    except Exception:
        trust_label, injection_status, injection_count, injection_rules = "quarantined", "pending", 0, []
    repository_id, repository_revision = _import_repository_fields(metadata)
    session_id, session_harness = _import_session_fields(metadata)
    collection_id = _safe_import_identity(metadata.get("collection_id")) or f"work-inbox:{source}"
    envelope_item_id = (
        _safe_import_identity(import_model._import_source_key({"metadata": metadata, "source": source})) or item_id
    )
    locator_kind, locator_value = _safe_locator_fields(
        locator_kind=metadata.get("locator_kind"),
        locator_value=metadata.get("locator_value"),
        fallback_item_id=item_id,
    )
    captured_at = (
        _bound_import_identity(metadata.get("captured_at"), max_len=_IMPORT_CAPTURED_AT_MAX_LEN) or ingested_at
    )
    return provenance.build_envelope(
        source_system="work-inbox",
        source_kind=source,
        source_producer="work_cmd.imports.import_provenance",
        origin=origin,
        repository_id=repository_id,
        repository_revision=repository_revision,
        session_id=session_id,
        session_harness=session_harness,
        collection_id=collection_id,
        item_id=envelope_item_id,
        locator_kind=locator_kind,
        locator_value=locator_value,
        attribution="inferred",
        modality=modality,
        trust_label=trust_label,
        trust_assigned_by="ingest:work_cmd.imports.import_provenance",
        trust_assigned_at=ingested_at,
        injection_status=injection_status,
        injection_count=injection_count,
        injection_rules=injection_rules,
        text=text,
        raw_bytes=None,
        content_scope="item.text.utf8.v1",
        captured_at=captured_at,
        ingested_at=ingested_at,
    )


def _import_envelope_matches(item: dict[str, Any]) -> bool:
    metadata = _metadata(item)
    env = metadata.get("provenance")
    if not isinstance(env, Mapping):
        return False
    if provenance.validate_envelope(env, inbound_adapter=True):
        return False
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return False
    return hashes.get("content") == provenance.content_sha256(str(item.get("text") or ""))


def _backfill_import_provenance(target: Path) -> dict[str, Any]:
    """Stamp inferred envelopes under the canonical inbox writer exclusion."""
    with locking._canonical_inbox_write(target):
        return _backfill_import_provenance_locked(target)


def _backfill_import_provenance_locked(target: Path) -> dict[str, Any]:
    """Stamp inferred envelopes on inbox rows missing a valid matching envelope."""
    verify_canonical_write_locks(target)
    imports = import_model._read_imports(target)
    now = helpers._now().isoformat()
    updated: list[dict[str, Any]] = []
    stamped = 0
    unchanged = 0
    missing = 0
    inferred = 0
    for item in imports:
        if not isinstance(item, dict):
            continue
        if _import_envelope_matches(item):
            unchanged += 1
            updated.append(item)
            continue
        missing += 1
        metadata = dict(_metadata(item))
        metadata.pop("provenance", None)
        env = _stamp_inferred_import_provenance(
            text=str(item.get("text") or ""),
            source=str(item.get("source") or "manual"),
            item_id=str(item.get("id") or "unknown"),
            metadata=metadata,
            ingested_at=now,
        )
        new_item = dict(item)
        new_meta = dict(metadata)
        new_meta["provenance"] = env
        new_item["metadata"] = new_meta
        updated.append(new_item)
        stamped += 1
        inferred += 1
    if stamped:
        import_model._write_imports(target, updated)
    return {
        "target": str(target),
        "scanned": len(imports),
        "stamped": stamped,
        "unchanged": unchanged,
        "missing_envelope": missing,
        "inferred": inferred,
        "trusted": 0,
    }


def _import_injection_trust(text: str) -> tuple[str, str, int, list[str]]:
    """Map injection scan outcome to trust label and injection fields.

    Hit -> quarantined/flagged; clean -> untrusted/clean; unavailable ->
    quarantined/pending. Does not upgrade trust.
    """
    try:
        hits = scan_handoff_injection_heuristics(text)
        warnings = [hit for hit in hits if hit.severity == "warning"]
    except Exception:
        return "quarantined", "pending", 0, []
    if warnings:
        rules = sorted({hit.rule for hit in warnings if isinstance(hit.rule, str) and hit.rule})
        return "quarantined", "flagged", len(warnings), rules
    return "untrusted", "clean", 0, []


def _reusable_inbound_provenance(inbound: object) -> Mapping[str, Any] | None:
    if not isinstance(inbound, Mapping):
        return None
    if provenance.validate_envelope(inbound):
        return None
    return inbound


def _stamp_import_provenance(
    *,
    text: str,
    source: str,
    item_id: str,
    metadata: dict[str, Any],
    ingested_at: str,
    inbound_provenance: object = None,
    redaction: Mapping[str, Any] | None = None,
    redaction_failed: bool = False,
    kind: str | None = None,
) -> dict[str, Any]:
    """Build a locally assigned work-inbox provenance envelope for one import.

    Authoritative source, origin, and modality always come from the trusted
    producer mapping. A validated inbound envelope may contribute only
    non-authoritative identity fields (locator/repository/session/collection/
    item/attribution/captured_at). Digest and trust are always recomputed.
    """
    reusable = _reusable_inbound_provenance(inbound_provenance)
    trust_label, injection_status, injection_count, injection_rules = _import_injection_trust(text)
    if redaction_failed:
        trust_label = "quarantined"
    origin, modality = _import_origin_modality(source, kind=kind)
    source_system = "work-inbox"
    source_kind = source
    source_producer = "ledger._make_import"

    if reusable is not None:
        repo_obj = reusable["repository"]
        repository_id = _safe_repository_id(repo_obj.get("id")) or "unknown"
        revision = repo_obj.get("revision")
        repository_revision = _safe_repository_revision(revision) if repository_id != "unknown" else None
        session_obj = reusable["session"]
        session_id = _safe_import_identity(session_obj.get("id"))
        session_harness = _safe_import_identity(session_obj.get("harness"))
        collection_id = _safe_import_identity(reusable["collection_id"]) or f"work-inbox:{source}"
        envelope_item_id = _safe_import_identity(reusable["item_id"]) or item_id
        locator_obj = reusable["locator"]
        locator_kind, locator_value = _safe_locator_fields(
            locator_kind=locator_obj.get("kind"),
            locator_value=locator_obj.get("value"),
            fallback_item_id=item_id,
        )
        attribution = str(reusable["attribution"])
        captured_at = _bound_import_identity(reusable.get("captured_at"), max_len=_IMPORT_CAPTURED_AT_MAX_LEN)
        if captured_at is None:
            captured_at = ingested_at
    else:
        repository_id, repository_revision = _import_repository_fields(metadata)
        session_id, session_harness = _import_session_fields(metadata)

        collection_id_candidate = _safe_import_identity(metadata.get("collection_id"))
        collection_id = collection_id_candidate or f"work-inbox:{source}"

        envelope_item_id = import_model._import_source_key({"metadata": metadata, "source": source}) or item_id
        envelope_item_id = _safe_import_identity(envelope_item_id) or item_id
        locator_kind, locator_value = _safe_locator_fields(
            locator_kind=metadata.get("locator_kind"),
            locator_value=metadata.get("locator_value"),
            fallback_item_id=item_id,
        )
        attribution = "observed"
        captured_at = _bound_import_identity(metadata.get("captured_at"), max_len=_IMPORT_CAPTURED_AT_MAX_LEN)
        if captured_at is None:
            captured_at = ingested_at

    try:
        return provenance.build_envelope(
            source_system=source_system,
            source_kind=source_kind,
            source_producer=source_producer,
            origin=origin,
            repository_id=repository_id,
            repository_revision=repository_revision,
            session_id=session_id,
            session_harness=session_harness,
            collection_id=collection_id,
            item_id=envelope_item_id,
            locator_kind=locator_kind,
            locator_value=locator_value,
            attribution=attribution,
            modality=modality,
            trust_label=trust_label,
            trust_assigned_by="ingest:ledger._make_import",
            trust_assigned_at=ingested_at,
            injection_status=injection_status,
            injection_count=injection_count,
            injection_rules=injection_rules,
            text=text,
            raw_bytes=None,
            content_scope="item.text.utf8.v1",
            captured_at=captured_at,
            ingested_at=ingested_at,
            redaction=redaction,
        )
    except ValueError as exc:
        raise _ImportProvenanceError(str(exc)) from exc


def _import_source_identity(item: dict[str, Any]) -> tuple[str, str, str] | None:
    source_key = import_model._import_source_key(item)
    if not source_key:
        return None
    return (
        str(item.get("source") or "manual"),
        str(item.get("kind") or "task"),
        source_key,
    )


def _validate_import_record(
    value: object,
    *,
    label: str,
    allow_non_task_fields: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return None, [f"{label}: expected JSON object"]

    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{label}: text must be a non-empty string")
    kind = value.get("kind", "task")
    if not isinstance(kind, str) or kind not in constants.IMPORT_KINDS:
        errors.append(f"{label}: kind must be one of: {', '.join(constants.IMPORT_KINDS)}")
    source = value.get("source", "manual")
    if not isinstance(source, str) or not source.strip():
        errors.append(f"{label}: source must be a non-empty string")
    metadata = value.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        errors.append(f"{label}: metadata must be an object when present")
    task_type = value.get("type")
    if task_type is not None and (not isinstance(task_type, str) or task_type.strip() not in constants.TASK_TYPES):
        errors.append(f"{label}: type must be one of: {', '.join(constants.TASK_TYPES)}")
    priority = value.get("priority")
    if priority is not None and (not isinstance(priority, str) or priority.strip() not in constants.TASK_PRIORITIES):
        errors.append(f"{label}: priority must be one of: {', '.join(constants.TASK_PRIORITIES)}")
    template = value.get("template")
    if template is not None and (not isinstance(template, str) or template.strip() not in constants.TASK_TEMPLATES):
        errors.append(f"{label}: template must be one of: {', '.join(constants.TASK_TEMPLATES)}")
    acceptance = value.get("acceptance")
    normalized_acceptance: list[str] = []
    if acceptance is not None:
        if not isinstance(acceptance, list):
            errors.append(f"{label}: acceptance must be a list of non-empty strings")
        else:
            seen_acceptance: set[str] = set()
            for index, item in enumerate(acceptance, start=1):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"{label}: acceptance item {index} must be a non-empty string")
                    continue
                rendered = item.strip()
                key = import_model._task_text_key(rendered)
                if key in seen_acceptance:
                    continue
                normalized_acceptance.append(rendered)
                seen_acceptance.add(key)
    task_fields = {
        name
        for name, present in {
            "type": task_type is not None,
            "priority": priority is not None,
            "template": template is not None,
            "acceptance": acceptance is not None,
        }.items()
        if present
    }
    if task_fields and kind != "task" and not allow_non_task_fields:
        errors.append(f"{label}: task fields are only valid when kind is task")

    if errors:
        return None, errors
    assert isinstance(text, str)
    assert isinstance(source, str)
    record: dict[str, Any] = {
        "text": text.strip(),
        "kind": kind,
        "source": source.strip(),
        "metadata": metadata,
    }
    if isinstance(task_type, str) and task_type.strip():
        record["type"] = task_type.strip()
    if isinstance(priority, str) and priority.strip():
        record["priority"] = priority.strip()
    if isinstance(template, str) and template.strip():
        record["template"] = template.strip()
    if acceptance is not None:
        record["acceptance"] = normalized_acceptance
    return record, []


def _parse_import_jsonl(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        label = f"line {line_number}"
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: invalid JSON: {exc.msg}")
            continue
        record, record_errors = _validate_import_record(value, label=label)
        errors.extend(record_errors)
        if record is not None:
            records.append(record)
    return records, errors


def _load_import_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        text = path.read_text()
    except OSError as exc:
        return [], [f"{path}: {exc}"]
    return _parse_import_jsonl(text)


def _append_import_records(
    target: Path,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    provenance_source: str | None = None,
    contain_provenance_errors: bool = False,
    migrate_untrusted_identities: bool = False,
    preserve_existing_raw: Callable[[bytes], None] | None = None,
    restore_existing_raw: Callable[[bytes, bool], None] | None = None,
    existing_imports: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Append records as one locked inbox read-modify-write transaction.

    The canonical writer locks span the read through the publication so a
    concurrent import, promote, dismiss, or scanner run cannot interleave
    (reentrant per process for callers already inside the lock; a marked
    self-importing child takes only the writer lock).
    """
    with locking._canonical_inbox_write(target):
        return _append_import_records_locked(
            target,
            records,
            dry_run=dry_run,
            provenance_source=provenance_source,
            contain_provenance_errors=contain_provenance_errors,
            migrate_untrusted_identities=migrate_untrusted_identities,
            preserve_existing_raw=preserve_existing_raw,
            restore_existing_raw=restore_existing_raw,
            existing_imports=existing_imports,
        )


def _append_import_records_locked(
    target: Path,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    provenance_source: str | None = None,
    contain_provenance_errors: bool = False,
    migrate_untrusted_identities: bool = False,
    preserve_existing_raw: Callable[[bytes], None] | None = None,
    restore_existing_raw: Callable[[bytes, bool], None] | None = None,
    existing_imports: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    imports = existing_imports if existing_imports is not None else import_model._read_imports(target)
    # One authenticated view of authority state for the whole append: the
    # snapshot is acquired once (after a reanchor pass) and consumed by
    # every proof validator below, which never reopen the store (#881).
    authority_record = descriptor_anchors._acquire_authenticated_authority_snapshot(target)
    # Canonical-dedupe posture split (#881 operator decision): suppression
    # consumes the single authenticated snapshot whenever the store verifies
    # as signed; only where no verifier-signed store can exist - isolation is
    # not "external-key" - does it fall back to the pre-#881 unsigned proof
    # path (persisted sidecar plus reproducible receipt), warning once per
    # process about the downgrade. Legacy identity grants below never take
    # that fallback: they stay signed-only.
    unsigned_dedupe_fallback = (
        authority_record is None and descriptor_anchors._unsigned_dedupe_proof_for_non_isolated_workspace(target)
    )
    # Exactly one acquisition per append: downstream legacy validators receive
    # the consumed snapshot, or the explicit absent-sentinel that forbids
    # re-acquisition (#881 round 4).
    snapshot_binding = authority_record if authority_record is not None else descriptor_anchors._APPEND_SNAPSHOT_ABSENT
    existing = {
        import_model._import_record_key(item)
        for item in imports
        if isinstance(item, dict) and item.get("status", "pending") in {"pending", "promoted"}
    }
    existing_by_source: dict[tuple[str, str, str], dict[str, Any]] = {}
    legacy_by_source_content: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in imports:
        if not isinstance(item, dict):
            continue
        identity = _import_source_identity(item)
        if identity is not None:
            existing_by_source[identity] = item
        legacy_identity = _legacy_import_source_content_identity(item, target=target, record=snapshot_binding)
        if legacy_identity is not None and not import_model._has_canonical_untrusted_import_identity(item):
            legacy_by_source_content[legacy_identity] = item
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_dismissed: list[dict[str, Any]] = []
    rejected: list[str] = []
    for record in records:
        key = import_model._import_record_key(record)
        identity = _import_source_identity(record)
        if identity is not None and identity in existing_by_source:
            existing_item = existing_by_source[identity]
            # Dedupe suppression consumes the one signed authority record
            # acquired above when one exists. Without external-key isolation
            # no signed store can exist, so the explicit unsigned downgrade
            # predicate admits the pre-#881 persisted-proof path instead;
            # unsigned sidecar or receipt bindings stay forgeable (#881) and
            # are never trusted for legacy identity grants.
            if authority_record is not None:
                canonical_existing_proof = descriptor_anchors._has_persisted_import_proof(
                    existing_item, target=target, record=authority_record
                )
            elif unsigned_dedupe_fallback:
                canonical_existing_proof = descriptor_anchors._has_persisted_import_proof(existing_item, target=target)
            else:
                canonical_existing_proof = False
            canonical_existing_row = import_model._has_canonical_untrusted_import_identity(existing_item)
            canonical_incoming_row = import_model._has_canonical_untrusted_import_identity(record)
            canonical_migration = migrate_untrusted_identities and canonical_incoming_row and not canonical_existing_row
            existing_migration_proof = (
                authority_record is not None
                and descriptor_anchors._authenticated_legacy_import_proof(
                    existing_item, target=target, record=snapshot_binding
                )
                and descriptor_anchors._import_content_identity(existing_item)
                == descriptor_anchors._import_content_identity(record)
            )
            if canonical_existing_row and canonical_incoming_row and not canonical_existing_proof:
                pass
            elif canonical_migration and not existing_migration_proof:
                pass
            elif existing_item.get("status") == "dismissed":
                if _import_fingerprint(existing_item) == _import_fingerprint(record):
                    skipped_dismissed.append(record)
                    continue
            elif _import_fingerprint(existing_item) == _import_fingerprint(record):
                skipped.append(record)
                continue
        elif (
            key[2]
            and key in existing
            and not (migrate_untrusted_identities and import_model._has_canonical_untrusted_import_identity(record))
        ):
            skipped.append(record)
            continue
        elif migrate_untrusted_identities and import_model._has_canonical_untrusted_import_identity(record):
            content_identity = descriptor_anchors._import_content_identity(record)
            if content_identity is not None:
                legacy_identity = (str(record.get("source")), *content_identity)
                legacy_item = legacy_by_source_content.get(legacy_identity)
                if legacy_item is not None:
                    if legacy_item.get("status") == "dismissed":
                        skipped_dismissed.append(record)
                        continue
                    if legacy_item.get("status") in {"pending", "promoted"}:
                        skipped.append(record)
                        continue
        try:
            item = tasks_plans._make_import(
                str(record["text"]),
                kind=str(record["kind"]),
                source=str(record["source"]),
                metadata=record.get("metadata") if isinstance(record.get("metadata"), dict) else None,
                task_type=record.get("type") if isinstance(record.get("type"), str) else None,
                priority=record.get("priority") if isinstance(record.get("priority"), str) else None,
                acceptance=record.get("acceptance") if isinstance(record.get("acceptance"), list) else None,
                template=record.get("template") if isinstance(record.get("template"), str) else None,
                provenance_source=_resolve_import_provenance_source(record, provenance_source),
            )
        except _ImportProvenanceError:
            if not contain_provenance_errors:
                raise
            rejected.append(_IMPORT_PROVENANCE_REJECTION_REASON)
            continue
        imported.append(item)
        existing.add(key)
        if identity is not None:
            existing_by_source[identity] = item
    if imported and not dry_run:
        verify_canonical_write_locks(target)
        inbox_parent, inbox_name, previous_raw, previous_exists = _snapshot_import_inbox(target)
        published = False
        try:
            if preserve_existing_raw:
                try:
                    preserve_existing_raw(
                        b"".join(json.dumps(item, sort_keys=True).encode("utf-8") + b"\n" for item in imported)
                    )
                except OSError:
                    return [], [], [], ["inbox_persistence_failed"]
                published = True
            else:
                imports.extend(imported)
                published = True
                rendered = "".join(json.dumps(item, sort_keys=True) + "\n" for item in imports).encode("utf-8")
                try:
                    _write_import_inbox_bytes_at(inbox_parent, inbox_name, rendered)
                except BaseException:
                    _restore_import_inbox_snapshot(inbox_parent, inbox_name, previous_raw, previous_exists)
                    raise
            try:
                descriptor_anchors._write_persisted_import_proofs(target, imported, operation_id=uuid4().hex)
            except BaseException:
                try:
                    if published:
                        if restore_existing_raw is not None:
                            restore_existing_raw(previous_raw, previous_exists)
                        else:
                            _restore_import_inbox_snapshot(inbox_parent, inbox_name, previous_raw, previous_exists)
                finally:
                    try:
                        descriptor_anchors._remove_persisted_import_proofs(target, imported)
                    except BaseException:
                        pass
                raise
        finally:
            if inbox_parent != -1:
                os.close(inbox_parent)
    return imported, skipped, skipped_dismissed, rejected
