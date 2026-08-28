"""Preview-first local delivery of untrusted Grok Bot automation findings.

A producer writes a private ``brigade.grokbot.findings.v1`` manifest. Preview
is the default. Apply writes at most ``limit`` deterministic Memory Handoff
drafts into the canonical owner's review inbox and records delivery markers
under the local Grok Bot queue. Finding title and body never appear in
command output, markers, or other Brigade receipts.

The generic entry shape accepts live fleet and backup values: severities
``info``, ``low``, ``medium``, ``warning``, ``high``, ``critical``, and
``unknown``; ``observed_at`` as an empty string or a timezone-aware ISO
timestamp; and ``source_digest`` as a ``sha256:`` hex digest that is not
required to hash the body. ``content_digest`` is the required integrity
check over canonical UTF-8 title + NUL + body. ``adapt_live_finding``
converts the live sidecar's extra ``trust`` / ``delivery`` labels and raw
revision hex digest into that approved exact-key shape without rewriting
live severity, time, or digest values. Live ``reason`` / ``summary`` values
may reach the live maximum; the adapter derives the generic title with the
same bounded ``proposalTitle`` rule the live relay uses and keeps the full
body. ``convert_live_findings`` is the supported batch conversion path.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from . import grokbot_jobs, grokbot_ops, grokbot_reconcile, handoff_cmd
from .handoff_cmd.models import DEFAULT_DRAFT_DOCUMENT, NO_CARD_ACTION

FINDINGS_SCHEMA = "brigade.grokbot.findings.v1"
LIVE_FINDINGS_SCHEMA = "brigade.grokbot.live-findings.v1"
REQUIRED_MANIFEST_KEYS = frozenset({"schema", "entries"})
REQUIRED_ENTRY_KEYS = frozenset(
    {
        "producer",
        "finding_id",
        "revision",
        "observed_at",
        "severity",
        "title",
        "body",
        "source_ref",
        "source_digest",
        "content_digest",
    }
)
LIVE_ENTRY_KEYS = frozenset(REQUIRED_ENTRY_KEYS - {"content_digest"})
LIVE_OPTIONAL_KEYS = frozenset({"trust", "delivery"})
MARKER_KEYS = frozenset(
    {
        "schema",
        "identity",
        "revision",
        "source_digest",
        "content_digest",
        "draft_path",
        "draft_sha256",
        "delivered_at",
    }
)
IDENTITY_KEYS = frozenset({"producer", "finding_id"})
SEVERITIES = frozenset({"info", "low", "medium", "warning", "high", "critical", "unknown"})
FINDINGS_DIRNAME = "findings"
REVIEW_INBOX = grokbot_reconcile.REVIEW_INBOX
MIN_LIMIT = grokbot_reconcile.MIN_LIMIT
MAX_LIMIT = grokbot_reconcile.MAX_LIMIT
DEFAULT_LIMIT = grokbot_reconcile.DEFAULT_LIMIT
MAX_MANIFEST_BYTES = 786432
MAX_ENTRIES = 50
MAX_TITLE_CHARS = 200
PROPOSAL_TITLE_CHARS = 120
LIVE_MAX_TITLE_BYTES = 16_384
MAX_BODY_BYTES = grokbot_jobs.MAX_REPORT_BYTES
MAX_SOURCE_REF_CHARS = 512
MAX_OBSERVED_AT_CHARS = 64
MARKER_MAX_BYTES = 4096
SECURE_OWNER_WRITE_AVAILABLE = grokbot_reconcile.SECURE_OWNER_WRITE_AVAILABLE


class FindingsError(ValueError):
    """A rejected findings request with a stable machine-readable reason."""

    def __init__(self, reason: str, index: int | None = None):
        self.reason = reason
        self.index = index
        super().__init__(reason)


def draft_filename(producer: str, finding_id: str, revision: str) -> str:
    """Return the deterministic inbox draft name for one identity and revision."""
    return f"finding-{identity_digest(producer, finding_id, revision)}.md"


def marker_filename(producer: str, finding_id: str, revision: str) -> str:
    """Return the deterministic queue marker name for one identity and revision."""
    return f"{identity_digest(producer, finding_id, revision)}.json"


def identity_digest(producer: str, finding_id: str, revision: str) -> str:
    """Return the irreversible digest for one producer, finding, and revision."""
    payload = f"{producer}\0{finding_id}\0{revision}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_digest(title: str, body: str) -> str:
    """Return ``sha256:`` of canonical UTF-8 title + NUL + body."""
    return "sha256:" + hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()


def proposal_title(producer: str, finding_id: str) -> str:
    """Return the live-relay ``proposalTitle`` for one producer and finding."""
    title = f"[UNTRUSTED] {producer} {finding_id}"
    return title if len(title) <= PROPOSAL_TITLE_CHARS else title[:PROPOSAL_TITLE_CHARS]


def adapt_live_finding(entry: object, *, index: int = 0) -> dict[str, str]:
    """Convert a live fleet or backup normalized record into the approved entry.

    Live sidecar records keep extra ``trust`` / ``delivery`` labels, emit
    ``source_digest`` as a raw revision hex digest, and omit ``content_digest``.
    This adapter preserves live severity, observed_at, digest, and body values.
    It prefixes a bare 64-hex digest with ``sha256:``, derives a bounded title
    with the live relay ``proposalTitle`` rule, and adds the required content
    digest. Unexpected keys are rejected.
    """
    if not isinstance(entry, dict):
        raise FindingsError("invalid-entry", index=index)
    extra = set(entry) - LIVE_ENTRY_KEYS - LIVE_OPTIONAL_KEYS
    if extra or LIVE_ENTRY_KEYS - set(entry):
        raise FindingsError("invalid-entry", index=index)
    trust = entry.get("trust")
    delivery = entry.get("delivery")
    if trust is not None and trust != "untrusted":
        raise FindingsError("invalid-entry", index=index)
    if delivery is not None and delivery != "review-only":
        raise FindingsError("invalid-entry", index=index)
    producer = entry["producer"]
    finding_id = entry["finding_id"]
    title = entry["title"]
    body = entry["body"]
    if not isinstance(producer, str) or not isinstance(finding_id, str):
        raise FindingsError("invalid-entry", index=index)
    if not isinstance(title, str) or not isinstance(body, str):
        raise FindingsError("invalid-entry", index=index)
    _validate_live_title(title, index=index)
    derived_title = proposal_title(producer, finding_id)
    source_digest = entry["source_digest"]
    if isinstance(source_digest, str) and grokbot_jobs.LOWER_HEX_64_RE.fullmatch(source_digest):
        source_digest = f"sha256:{source_digest}"
    return _validate_entry(
        {
            "producer": producer,
            "finding_id": finding_id,
            "revision": entry["revision"],
            "observed_at": entry["observed_at"],
            "severity": entry["severity"],
            "title": derived_title,
            "body": body,
            "source_ref": entry["source_ref"],
            "source_digest": source_digest,
            "content_digest": content_digest(derived_title, body),
        },
        index=index,
    )


def convert_live_findings(source: Path, destination: Path) -> dict[str, Any]:
    """Convert live normalized records into a mode-0600 generic manifest."""
    payload = _load_live_manifest(source)
    adapted: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(payload["entries"]):
        item = adapt_live_finding(entry, index=index)
        identity = (item["producer"], item["finding_id"])
        if identity in seen:
            raise FindingsError("duplicate-identity", index=index)
        seen.add(identity)
        adapted.append(item)
    adapted = _sorted_entries(adapted)
    _write_manifest_atomic(destination, {"schema": FINDINGS_SCHEMA, "entries": adapted})
    return {
        "converted": len(adapted),
        "findings": [_handle(item) for item in adapted],
    }


def preview(
    target: Path,
    owner: Path,
    manifest: Path,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Validate a findings manifest and count deliveries without writing."""
    _validate_owner(owner)
    limit = _validate_limit(limit)
    payload = load_manifest(manifest)
    eligible, known = _classify(target, owner, payload["entries"])
    return {
        "eligible": len(eligible),
        "known": len(known),
        "created": 0,
        "limit": limit,
        "findings": [_handle(entry) for entry in eligible[:limit]],
    }


def apply(
    target: Path,
    owner: Path,
    manifest: Path,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write at most ``limit`` owner-review drafts and matching queue markers."""
    owner_path = _validate_owner(owner)
    bound = _validate_limit(limit)
    payload = load_manifest(manifest)
    return _deliver_entries(target, owner_path, payload["entries"], bound, now)


def apply_entries(
    target: Path,
    owner: Path,
    entries: object,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write drafts from in-memory entries after a complete preflight."""
    owner_path, bound, validated = _preflight_entries(owner, entries, limit)
    return _deliver_entries(target, owner_path, validated, bound, now)


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a private findings file after checking ownership, mode, and schema."""
    try:
        payload = json.loads(_read_manifest_snapshot(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FindingsError("malformed-manifest") from exc
    if not isinstance(payload, dict):
        raise FindingsError("malformed-manifest")
    if set(payload) != REQUIRED_MANIFEST_KEYS:
        if payload.get("schema") != FINDINGS_SCHEMA:
            raise FindingsError("invalid-schema")
        raise FindingsError("malformed-manifest")
    if payload["schema"] != FINDINGS_SCHEMA:
        raise FindingsError("invalid-schema")
    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise FindingsError("malformed-manifest")
    seen: set[tuple[str, str]] = set()
    validated: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        item = _validate_entry(entry, index=index)
        identity = (item["producer"], item["finding_id"])
        if identity in seen:
            raise FindingsError("duplicate-identity", index=index)
        seen.add(identity)
        validated.append(item)
    payload["entries"] = _sorted_entries(validated)
    return payload


def _preflight_entries(owner: Path, entries: object, limit: object) -> tuple[Path, int, list[dict[str, str]]]:
    """Validate owner, limit, and every entry before any queue storage access."""
    owner_path = _validate_owner(owner)
    bound = _validate_limit(limit)
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise FindingsError("malformed-manifest")
    seen: set[tuple[str, str]] = set()
    validated: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        item = _validate_entry(entry, index=index)
        identity = (item["producer"], item["finding_id"])
        if identity in seen:
            raise FindingsError("duplicate-identity", index=index)
        seen.add(identity)
        validated.append(item)
    return owner_path, bound, _sorted_entries(validated)


def _deliver_entries(
    target: Path,
    owner_path: Path,
    entries: list[dict[str, str]],
    limit: int,
    now: datetime | None,
) -> dict[str, Any]:
    """Deliver pre-validated entries through the existing atomic writer."""
    if not SECURE_OWNER_WRITE_AVAILABLE:  # pragma: no cover - exercised on Windows.
        raise FindingsError("secure-owner-write-unavailable")
    created: list[dict[str, str]] = []
    recovered = 0
    eligible: list[dict[str, str]] = []
    known: list[dict[str, str]] = []
    try:
        with grokbot_jobs._storage_paths(target) as storage, grokbot_jobs._queue_lock(storage):
            findings_dir, extra_fd = _open_findings(storage)
            try:
                eligible, known, recoverable = _classify_locked(findings_dir, owner_path, entries)
                recoverable_ids = {(item["producer"], item["finding_id"]) for item in recoverable}
                eligible_ids = {(item["producer"], item["finding_id"]) for item in eligible}
                for entry in entries:
                    identity = (entry["producer"], entry["finding_id"])
                    if identity not in recoverable_ids and identity not in eligible_ids:
                        continue
                    if len(created) + recovered >= limit:
                        break
                    handle, status = _deliver_one(findings_dir, owner_path, entry, now)
                    if status == "created":
                        created.append(handle)
                    else:
                        recovered += 1
            finally:
                if extra_fd is not None:
                    os.close(extra_fd)
    except grokbot_jobs.GrokbotJobError as exc:
        raise FindingsError(exc.reason) from exc
    except grokbot_reconcile.ReconcileError as exc:
        raise FindingsError(exc.reason) from exc
    return {
        "eligible": len(eligible),
        "known": len(known),
        "created": len(created),
        "skipped": len(known) + recovered,
        "limit": limit,
        "findings": created,
    }


def _validate_owner(owner: Path) -> Path:
    try:
        return grokbot_reconcile._validate_owner(owner)
    except grokbot_reconcile.ReconcileError as exc:
        raise FindingsError(exc.reason) from exc


def _validate_limit(limit: object) -> int:
    try:
        return grokbot_reconcile._validate_limit(limit)
    except grokbot_reconcile.ReconcileError as exc:
        raise FindingsError(exc.reason) from exc


def _validate_entry(entry: object, *, index: int) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) != REQUIRED_ENTRY_KEYS:
        raise FindingsError("invalid-entry", index=index)
    producer = _bounded_identifier(entry["producer"], reason="invalid-producer", index=index)
    finding_id = _bounded_identifier(entry["finding_id"], reason="invalid-finding-id", index=index)
    revision = _bounded_identifier(entry["revision"], reason="invalid-revision", index=index)
    observed_at = _validate_observed_at(entry["observed_at"], index=index)
    severity = entry["severity"]
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise FindingsError("invalid-severity", index=index)
    title = _bounded_text(entry["title"], reason="invalid-title", maximum=MAX_TITLE_CHARS, index=index)
    body = _bounded_body(entry["body"], index=index)
    source_ref = _validate_source_ref(entry["source_ref"], index=index)
    source_digest = entry["source_digest"]
    if not isinstance(source_digest, str) or not grokbot_jobs.TASK_HASH_RE.fullmatch(source_digest):
        raise FindingsError("invalid-source-digest", index=index)
    digest = entry["content_digest"]
    if not isinstance(digest, str) or not grokbot_jobs.TASK_HASH_RE.fullmatch(digest):
        raise FindingsError("invalid-content-digest", index=index)
    if digest != content_digest(title, body):
        raise FindingsError("digest-mismatch", index=index)
    return {
        "producer": producer,
        "finding_id": finding_id,
        "revision": revision,
        "observed_at": observed_at,
        "severity": severity,
        "title": title,
        "body": body,
        "source_ref": source_ref,
        "source_digest": source_digest,
        "content_digest": digest,
    }


def _bounded_identifier(value: object, *, reason: str, index: int) -> str:
    if not isinstance(value, str) or not grokbot_jobs.OPAQUE_ID_RE.fullmatch(value):
        raise FindingsError(reason, index=index)
    return value


def _bounded_text(value: object, *, reason: str, maximum: int, index: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise FindingsError(reason, index=index)
    return value


def _bounded_body(value: object, *, index: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise FindingsError("invalid-body", index=index)
    if len(value.encode("utf-8")) > MAX_BODY_BYTES:
        raise FindingsError("invalid-body", index=index)
    return value


def _validate_observed_at(value: object, *, index: int) -> str:
    return _validate_aware_timestamp(value, reason="invalid-observed-at", index=index, allow_empty=True)


def _validate_aware_timestamp(
    value: object,
    *,
    reason: str,
    index: int | None = None,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > MAX_OBSERVED_AT_CHARS:
        raise FindingsError(reason, index=index)
    if value == "":
        if allow_empty:
            return value
        raise FindingsError(reason, index=index)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise FindingsError(reason, index=index) from exc
    if parsed.tzinfo is None:
        raise FindingsError(reason, index=index)
    return value


def _validate_live_title(value: str, *, index: int) -> None:
    if "\x00" in value or len(value.encode("utf-8")) > LIVE_MAX_TITLE_BYTES:
        raise FindingsError("invalid-title", index=index)


def _validate_source_ref(value: object, *, index: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_SOURCE_REF_CHARS
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise FindingsError("invalid-source-ref", index=index)
    return value


def _load_live_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_manifest_snapshot(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FindingsError("malformed-manifest") from exc
    if not isinstance(payload, dict):
        raise FindingsError("malformed-manifest")
    if payload.get("schema") != LIVE_FINDINGS_SCHEMA:
        raise FindingsError("invalid-schema")
    if set(payload) != REQUIRED_MANIFEST_KEYS:
        raise FindingsError("malformed-manifest")
    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise FindingsError("malformed-manifest")
    return payload


def _manifest_name(path: Path) -> str:
    name = Path(path).name
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise FindingsError("unsafe-manifest")
    return name


def _open_manifest_parent(path: Path) -> int:
    """Hold ``path.parent`` after refusing every symlink parent component."""
    try:
        return grokbot_ops._open_parent_nofollow(Path(path).expanduser(), create=False)
    except OSError as exc:
        raise FindingsError("unsafe-manifest") from exc


def _open_manifest_file(parent_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
    """Open one child name relative to a no-follow parent descriptor."""
    if os.name == "posix":
        return os.open(name, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=parent_fd)
    from .work_cmd import nt_dirfd  # pragma: no cover - exercised on Windows.

    return nt_dirfd.open_file(parent_fd, name, flags, mode)  # pragma: no cover - exercised on Windows.


def _write_manifest_atomic(path: Path, payload: dict[str, Any]) -> None:
    if not SECURE_OWNER_WRITE_AVAILABLE:  # pragma: no cover - exercised on Windows.
        raise FindingsError("secure-owner-write-unavailable")
    destination = Path(path).expanduser()
    name = _manifest_name(destination)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    parent_fd: int | None = None
    descriptor: int | None = None
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        if destination.is_symlink():
            raise FindingsError("unsafe-manifest")
        parent_fd = _open_manifest_parent(destination)
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise FindingsError("unsafe-manifest")
        descriptor = _open_manifest_file(
            parent_fd,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        temporary_created = True
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FindingsError("unsafe-manifest")
        os.fchmod(descriptor, 0o600)
        grokbot_jobs._write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
    except FindingsError:
        raise
    except OSError as exc:
        raise FindingsError("unsafe-manifest") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if temporary_created and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = exc
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise FindingsError("unsafe-manifest") from cleanup_error


def _read_manifest_snapshot(path: Path) -> bytes:
    destination = Path(path).expanduser()
    name = _manifest_name(destination)
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd = _open_manifest_parent(destination)
        descriptor = _open_manifest_file(parent_fd, name, os.O_RDONLY)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FindingsError("unsafe-manifest")
        owner_uid = getattr(os, "getuid", None)
        if owner_uid is not None and info.st_uid != owner_uid():
            raise FindingsError("unsafe-manifest")
        if (info.st_mode & 0o777) != 0o600:
            raise FindingsError("unsafe-manifest")
        return grokbot_jobs._read_bounded_bytes(descriptor, MAX_MANIFEST_BYTES)
    except grokbot_jobs.GrokbotJobError as exc:
        if exc.reason == "report-too-large":
            raise FindingsError("oversized-manifest") from exc
        raise FindingsError("unsafe-manifest") from exc
    except FindingsError:
        raise
    except OSError as exc:
        raise FindingsError("unsafe-manifest") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise FindingsError("unsafe-manifest") from cleanup_error


def _identity_digest(producer: str, finding_id: str, revision: str) -> str:
    return identity_digest(producer, finding_id, revision)


def _sorted_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(entries, key=lambda item: (item["producer"], item["finding_id"], item["revision"]))


def _handle(entry: dict[str, str]) -> dict[str, str]:
    return {
        "producer": entry["producer"],
        "finding_id": entry["finding_id"],
        "revision": entry["revision"],
    }


def _queue_root(target: Path) -> Path:
    return Path(target).expanduser() / ".brigade" / "cloud" / "grokbot"


def _classify(
    target: Path,
    owner: Path,
    entries: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    directory = _open_findings_readonly(target)
    try:
        eligible, known, _recoverable = _classify_locked(directory, owner, entries)
        return eligible, known
    finally:
        if directory is not None and directory.descriptor is not None:
            os.close(directory.descriptor)


def _classify_locked(
    directory: grokbot_jobs._Directory | None,
    owner: Path,
    entries: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    eligible: list[dict[str, str]] = []
    known: list[dict[str, str]] = []
    recoverable: list[dict[str, str]] = []
    for entry in entries:
        status = _classify_one(directory, owner, entry)
        if status == "eligible":
            eligible.append(entry)
        elif status == "known":
            known.append(entry)
        else:
            recoverable.append(entry)
    return eligible, known, recoverable


def _classify_one(
    directory: grokbot_jobs._Directory | None,
    owner: Path,
    entry: dict[str, str],
) -> str:
    text = _render_handoff(entry)
    name = draft_filename(entry["producer"], entry["finding_id"], entry["revision"])
    expected_path = f"{REVIEW_INBOX}/{name}"
    expected_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    marker = _read_marker(directory, entry) if directory is not None else None
    existing = _existing_handoff(owner, name)
    if marker is not None:
        expected = identity_digest(entry["producer"], entry["finding_id"], entry["revision"])
        marker_identity = marker["identity"]
        if isinstance(marker_identity, dict):
            matches_identity = marker_identity == {
                "producer": entry["producer"],
                "finding_id": entry["finding_id"],
            }
        else:
            matches_identity = marker_identity == expected
        if not matches_identity:
            raise FindingsError("marker-conflict")
        if marker["revision"] != entry["revision"]:
            raise FindingsError("marker-conflict")
        if marker["source_digest"] != entry["source_digest"]:
            raise FindingsError("digest-mismatch")
        if marker["content_digest"] != entry["content_digest"]:
            raise FindingsError("digest-mismatch")
        if marker["draft_path"] != expected_path or marker["draft_sha256"] != expected_digest:
            raise FindingsError("marker-conflict")
        if existing is not None and existing != text:
            raise FindingsError("draft-conflict")
        if existing is None:
            return "recover-draft"
        return "known"
    if existing is not None and existing != text:
        raise FindingsError("draft-conflict")
    if existing is not None:
        return "recover-marker"
    return "eligible"


def _deliver_one(
    directory: grokbot_jobs._Directory,
    owner: Path,
    entry: dict[str, str],
    now: datetime | None,
) -> tuple[dict[str, str], str]:
    text = _render_handoff(entry)
    name = draft_filename(entry["producer"], entry["finding_id"], entry["revision"])
    try:
        status = grokbot_reconcile._write_handoff(owner, Path(REVIEW_INBOX), name, text)
    except grokbot_reconcile.ReconcileError as exc:
        raise FindingsError(exc.reason) from exc
    _write_marker(directory, entry, text, now)
    return _handle(entry), status


def _render_handoff(entry: dict[str, str]) -> str:
    title = f"Untrusted automation finding {entry['producer']}/{entry['finding_id']}"
    summary = (
        "This Memory Handoff carries untrusted automation output from a producer "
        "finding. It is review-only. Route it to canonical-owner review rather "
        "than auto-edit canonical memory, MEMORY.md, or memory cards."
    )
    suggested = (
        "Untrusted automation output. Review-only. Route this finding to "
        "canonical-owner review rather than auto-edit canonical memory.\n\n"
        "Quoted title:\n\n"
        + grokbot_reconcile._quote_report(entry["title"])
        + "\n\nQuoted body:\n\n"
        + grokbot_reconcile._quote_report(entry["body"])
    )
    return handoff_cmd.drafts._render_handoff_draft(
        handoff_type="research",
        title=title,
        summary=summary,
        facts=[
            f"producer: {entry['producer']}",
            f"finding_id: {entry['finding_id']}",
            f"revision: {entry['revision']}",
            f"observed_at: {entry['observed_at']}",
            f"severity: {entry['severity']}",
            f"source_ref: {entry['source_ref']}",
            f"source_digest: {entry['source_digest']}",
            f"content_digest: {entry['content_digest']}",
        ],
        evidence=[
            f"findings manifest delivery for {entry['producer']}/{entry['finding_id']} revision {entry['revision']}"
        ],
        action=NO_CARD_ACTION,
        target_card=None,
        target_document=DEFAULT_DRAFT_DOCUMENT,
        suggested_content=suggested,
    )


def _existing_handoff(owner: Path, name: str) -> str | None:
    if not SECURE_OWNER_WRITE_AVAILABLE:  # pragma: no cover - exercised on Windows.
        return None
    descriptors: list[int] = []
    try:
        current = os.open(owner, grokbot_reconcile._directory_flags())
        descriptors.append(current)
        for part in Path(REVIEW_INBOX).parts:
            try:
                child = os.open(part, grokbot_reconcile._directory_flags(), dir_fd=current)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise FindingsError("unsafe-storage")
            descriptors.append(child)
            current = child
        try:
            return grokbot_reconcile._read_existing_handoff_at(current, name)
        except grokbot_reconcile.ReconcileError as exc:
            raise FindingsError(exc.reason) from exc
    except FindingsError:
        raise
    except OSError as exc:
        raise FindingsError("unsafe-storage") from exc
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        if close_error is not None:
            raise FindingsError("unsafe-storage") from close_error


def _open_queue_child(storage: grokbot_jobs._Storage, name: str) -> tuple[grokbot_jobs._Directory, int | None]:
    """Open or create one private child of the Grok Bot queue root."""
    if storage.root.descriptor is None:  # pragma: no cover - exercised on Windows.
        path = storage.root.path / name
        grokbot_jobs._ensure_directory(path)
        return grokbot_jobs._Directory(path, None), None
    descriptor = grokbot_jobs._open_or_create_directory(storage.root.descriptor, name)
    return grokbot_jobs._Directory(storage.root.path / name, descriptor), descriptor


def _open_findings(storage: grokbot_jobs._Storage) -> tuple[grokbot_jobs._Directory, int | None]:
    return _open_queue_child(storage, FINDINGS_DIRNAME)


def delivery_marker_valid(target: Path, owner: Path, entry: dict[str, str]) -> bool:
    """Return True when the delivery marker exists and matches this entry."""
    directory = _open_findings_readonly(target)
    try:
        if directory is None:
            return False
        return _classify_one(directory, owner, entry) in {"known", "recover-draft"}
    finally:
        if directory is not None and directory.descriptor is not None:
            os.close(directory.descriptor)


def _open_findings_readonly(target: Path) -> grokbot_jobs._Directory | None:
    return _open_queue_child_readonly(target, FINDINGS_DIRNAME)


def _open_queue_child_readonly(target: Path, child: str) -> grokbot_jobs._Directory | None:
    names = (".brigade", "cloud", "grokbot", child)
    if os.name != "posix":  # pragma: no cover - exercised on Windows.
        current = Path(target).expanduser()
        for name in names:
            current = current / name
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise FindingsError("unsafe-storage") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise FindingsError("unsafe-storage")
        return grokbot_jobs._Directory(current, None)
    descriptors: list[int] = []
    try:
        parent_fd = grokbot_jobs._open_directory_path(Path(target).expanduser().absolute())
        descriptors.append(parent_fd)
        for name in names:
            try:
                child_fd = grokbot_jobs._open_existing_directory(parent_fd, name)
            except FileNotFoundError:
                return None
            descriptors.append(child_fd)
            parent_fd = child_fd
        child_descriptor = descriptors.pop()
        return grokbot_jobs._Directory(_queue_root(target) / child, child_descriptor)
    except grokbot_jobs.GrokbotJobError as exc:
        raise FindingsError(exc.reason) from exc
    except OSError as exc:
        raise FindingsError("unsafe-storage") from exc
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        if close_error is not None:
            raise FindingsError("unsafe-storage") from close_error


def _read_marker(directory: grokbot_jobs._Directory, entry: dict[str, str]) -> dict[str, Any] | None:
    name = marker_filename(entry["producer"], entry["finding_id"], entry["revision"])
    try:
        data = grokbot_jobs._read_bytes_file(
            directory,
            name,
            maximum=MARKER_MAX_BYTES,
            missing_reason="marker-missing",
        )
    except grokbot_jobs.GrokbotJobError as exc:
        if exc.reason == "marker-missing":
            return None
        raise FindingsError(exc.reason) from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FindingsError("corrupt-storage") from exc
    if not isinstance(payload, dict) or set(payload) != MARKER_KEYS or payload.get("schema") != FINDINGS_SCHEMA:
        raise FindingsError("corrupt-storage")
    identity = payload.get("identity")
    if isinstance(identity, dict):
        if set(identity) != IDENTITY_KEYS:
            raise FindingsError("corrupt-storage")
        if not isinstance(identity.get("producer"), str) or not isinstance(identity.get("finding_id"), str):
            raise FindingsError("corrupt-storage")
    elif not isinstance(identity, str) or not grokbot_jobs.LOWER_HEX_64_RE.fullmatch(identity):
        raise FindingsError("corrupt-storage")
    if not isinstance(payload.get("revision"), str) or not grokbot_jobs.OPAQUE_ID_RE.fullmatch(payload["revision"]):
        raise FindingsError("corrupt-storage")
    if not isinstance(payload.get("source_digest"), str) or not grokbot_jobs.TASK_HASH_RE.fullmatch(
        payload["source_digest"]
    ):
        raise FindingsError("corrupt-storage")
    if not isinstance(payload.get("content_digest"), str) or not grokbot_jobs.TASK_HASH_RE.fullmatch(
        payload["content_digest"]
    ):
        raise FindingsError("corrupt-storage")
    if not isinstance(payload.get("draft_path"), str) or not isinstance(payload.get("draft_sha256"), str):
        raise FindingsError("corrupt-storage")
    if not grokbot_jobs.LOWER_HEX_64_RE.fullmatch(payload["draft_sha256"]):
        raise FindingsError("corrupt-storage")
    _validate_aware_timestamp(payload.get("delivered_at"), reason="corrupt-storage")
    return payload


def _write_marker(
    directory: grokbot_jobs._Directory,
    entry: dict[str, str],
    text: str,
    now: datetime | None,
) -> None:
    name = marker_filename(entry["producer"], entry["finding_id"], entry["revision"])
    grokbot_jobs._write_json_file(
        directory,
        name,
        {
            "schema": FINDINGS_SCHEMA,
            "identity": identity_digest(entry["producer"], entry["finding_id"], entry["revision"]),
            "revision": entry["revision"],
            "source_digest": entry["source_digest"],
            "content_digest": entry["content_digest"],
            "draft_path": f"{REVIEW_INBOX}/{draft_filename(entry['producer'], entry['finding_id'], entry['revision'])}",
            "draft_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "delivered_at": grokbot_jobs._now_iso(now),
        },
    )
