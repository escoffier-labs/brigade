"""Recovery checkpoint substrate for the run lifecycle (issue #568 slice 5, Task 1).

A checkpoint is a write-ahead record of the exact ``run.json`` bytes the legacy
writer is about to commit, content-addressed by their SHA-256 and stored at
``<run-dir>/events/recovery-checkpoints/<sha256>.json``. One
``run.snapshot.checkpointed`` event per write is appended to the existing
``events/lifecycle.jsonl`` journal under the closed ``brigade.run_event.v1``
envelope. This module owns the crash-safe publish helper, the payload-first
``validate_checkpoint`` with open-fd hardening, and the bounded journal-reader
bounds. Task 2 wires the fail-closed hook into ``aboyeur._write_json``.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from brigade import run_events, run_journal, runguard
from brigade.run_events import _HEX64, _bound

CHECKPOINT_EVENT_TYPE = "run.snapshot.checkpointed"
CHECKPOINT_MEDIA_TYPE = "application/vnd.brigade.run+json"
CHECKPOINT_PRIVACY_CLASS = "private"
CHECKPOINT_DIR_NAME = "recovery-checkpoints"
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_EVENTS = 512

_CHECKPOINT_PAYLOAD_REQUIRED_KEYS = frozenset(
    {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type"}
)
_CHECKPOINT_PAYLOAD_OPTIONAL_KEYS = frozenset({"body_kind", "pairing_key"})
# Backwards-compatible alias kept for any external reader of the closed key set;
# the validation logic now treats body_kind as optional.
_CHECKPOINT_PAYLOAD_KEYS = _CHECKPOINT_PAYLOAD_REQUIRED_KEYS | _CHECKPOINT_PAYLOAD_OPTIONAL_KEYS
_CHECKPOINT_IDEMPOTENCY_PREFIX = "checkpoint"

# Journal-derived metadata fields the projector owns over the run.json
# contract (see run_projector.DERIVED_FIELDS). A base-stripped checkpoint
# excludes exactly these four fields so the content-addressed snapshot is
# stable across projector re-derivations while the durable request fields
# are preserved.
_JOURNAL_METADATA_FIELDS = frozenset(
    {
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
)

# Durable request fields that must be present in a base-stripped checkpoint:
# the lifecycle journal request and the journal-authority request. These are
# the two preserved request signals a base-stripped snapshot must carry.
_DURABLE_REQUEST_FIELDS = frozenset({"lifecycle_journal_requested", "run_journal_authority_requested"})

_BODY_KIND_BASE_STRIPPED = "base-stripped"
_DISPATCH_FACT_EVENT_TYPES = frozenset(
    {
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
    }
)
_CHECKPOINT_IDEMPOTENCY_BASE_STRIPPED_PREFIX = f"{_CHECKPOINT_IDEMPOTENCY_PREFIX}:base-stripped"


class CheckpointError(RuntimeError):
    """Bounded checkpoint failure carrying a category diagnostic.

    The diagnostic is bounded to ``run_events.MAX_DIAGNOSTIC_LEN`` and carries
    only a category, never a raw path or payload value.
    """

    def __init__(self, diagnostic: str, *, category: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic
        self.category = category


def checkpoint_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "events" / CHECKPOINT_DIR_NAME


def checkpoint_path(run_dir: Path, sha256: str) -> Path:
    return checkpoint_dir(run_dir) / f"{sha256}.json"


def _mapped_lifecycle_event_types() -> set[str]:
    from brigade import run_lifecycle

    return set(run_lifecycle.STATUS_EVENT_TYPE.values())


def _validate_payload(payload: Any) -> None:
    """Full payload validation before any path access. Raises CheckpointError."""
    if not isinstance(payload, Mapping):
        raise CheckpointError(_bound("checkpoint payload must be an object"), category="payload-shape")

    for key in payload.keys():
        if not isinstance(key, str):
            raise CheckpointError(_bound("payload keys must be strings"), category="payload-keys")

    keys = set(payload.keys())
    missing = _CHECKPOINT_PAYLOAD_REQUIRED_KEYS - keys
    extra = keys - _CHECKPOINT_PAYLOAD_KEYS
    if missing or extra:
        missing_sorted = sorted(missing)
        extra_sorted = sorted(extra)
        raise CheckpointError(
            _bound(f"payload keys mismatch: missing={missing_sorted}, extra={extra_sorted}"),
            category="payload-keys",
        )

    # body_kind is optional: absent means legacy-full. An explicitly present
    # value (including null) must be exactly "base-stripped"; anything else
    # fails closed with category body-kind so unknown body kinds never reach
    # the fd path. A present null is not base-stripped, so it fails too.
    if "body_kind" in payload:
        body_kind = payload["body_kind"]
        if not isinstance(body_kind, str) or body_kind != _BODY_KIND_BASE_STRIPPED:
            raise CheckpointError(_bound("body_kind must be base-stripped"), category="body-kind")

    if "pairing_key" in payload:
        pairing_key = payload["pairing_key"]
        if not isinstance(pairing_key, str) or not _HEX64.match(pairing_key):
            raise CheckpointError(_bound("pairing_key must be 64-char lowercase hex"), category="pairing-key")

    if payload["media_type"] != CHECKPOINT_MEDIA_TYPE:
        raise CheckpointError(_bound("media_type mismatch"), category="media-type")

    if payload["privacy_class"] != CHECKPOINT_PRIVACY_CLASS:
        raise CheckpointError(_bound("privacy_class mismatch"), category="privacy-class")

    sha = payload["sha256"]
    if not isinstance(sha, str) or not _HEX64.match(sha):
        raise CheckpointError(_bound("sha256 must be 64-char lowercase hex"), category="sha256")

    byte_size = payload["byte_size"]
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        raise CheckpointError(_bound("byte_size must be an integer"), category="byte-size")
    if byte_size < 0 or byte_size > MAX_CHECKPOINT_BYTES:
        raise CheckpointError(_bound("byte_size out of range"), category="byte-size")

    declared_path = payload["path"]
    if not isinstance(declared_path, str):
        raise CheckpointError(_bound("path must be a string"), category="path")
    expected_path = f"events/{CHECKPOINT_DIR_NAME}/{sha}.json"
    if declared_path != expected_path:
        raise CheckpointError(_bound("path does not match declared sha256"), category="path")
    if declared_path.startswith("/"):
        raise CheckpointError(_bound("path must be relative"), category="path")
    if "\\" in declared_path:
        raise CheckpointError(_bound("path must not contain backslashes"), category="path")
    if ".." in declared_path.split("/"):
        raise CheckpointError(_bound("path must not contain '..' components"), category="path")
    stem = declared_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem != sha:
        raise CheckpointError(_bound("path stem must equal sha256"), category="path")

    paired = payload["paired_event_type"]
    if paired is None:
        pass
    elif not isinstance(paired, str):
        raise CheckpointError(_bound("paired_event_type must be null or a string"), category="paired-event-type")
    elif paired not in run_events.EVENT_TYPES:
        raise CheckpointError(_bound("paired_event_type not in registry"), category="paired-event-type")
    elif paired not in _mapped_lifecycle_event_types() and paired not in _DISPATCH_FACT_EVENT_TYPES:
        raise CheckpointError(
            _bound("paired_event_type is not a mapped lifecycle or dispatch fact event"),
            category="paired-event-type",
        )


def _writer_canonical_bytes(obj: Any) -> bytes:
    """Replicate ``aboyeur._write_json`` canonical encoding for writer-byte equality."""
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _strip_journal_metadata_from_base(base_bytes: bytes) -> bytes:
    """Strip the four journal-derived metadata fields from a run.json base.

    Returns the writer-canonical encoding of the base object with
    ``projector_version``, ``journal_present``, ``journal_last_sequence``,
    and ``journal_last_event_digest`` removed. Status and every other
    preserved field are retained. The result is writer-canonical so the
    operation is idempotent (a second strip removes nothing and re-emits
    the same canonical bytes). Non-UTF8, non-JSON, and non-object inputs
    fail closed with bounded categories ``utf8`` / ``json-object``.
    """
    try:
        text = base_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckpointError(_bound("base is not valid UTF-8"), category="utf8") from exc
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CheckpointError(_bound("base is not valid JSON"), category="json-object") from exc
    except RecursionError as exc:
        raise CheckpointError(_bound("base JSON nesting too deep"), category="json-object") from exc
    if not isinstance(obj, dict):
        raise CheckpointError(_bound("base is not a JSON object"), category="json-object")
    stripped = {k: v for k, v in obj.items() if k not in _JOURNAL_METADATA_FIELDS}
    try:
        return _writer_canonical_bytes(stripped)
    except RecursionError as exc:
        raise CheckpointError(_bound("base canonical re-encode nesting too deep"), category="json-object") from exc


def _validate_base_stripped_request_rules(obj: Mapping[str, Any]) -> None:
    """Enforce the base-stripped durable-request / metadata-exclusion rules.

    A base-stripped checkpoint must carry both durable request fields
    (``lifecycle_journal_requested`` and ``run_journal_authority_requested``)
    set to the bool ``True`` (not merely present: a missing key, a False
    value, or a wrong-typed value all fail closed) and must exclude every
    journal-derived metadata field. Validation runs on the stripped result:
    the write path passes the parsed stripped object (after
    ``_strip_journal_metadata_from_base``) and the validate path passes the
    stored stripped bytes. Canonical metadata-bearing input is accepted on
    the write path and stripped, so only the derived stored base must
    exclude the journal metadata fields. Any violation fails closed with
    category ``base-stripped-requests``.
    """
    for field in _DURABLE_REQUEST_FIELDS:
        if obj.get(field) is not True:
            raise CheckpointError(
                _bound(f"base-stripped base missing durable request field: {field}"),
                category="base-stripped-requests",
            )
    present_metadata = sorted(set(obj.keys()) & _JOURNAL_METADATA_FIELDS)
    if present_metadata:
        raise CheckpointError(
            _bound(f"base-stripped base must exclude journal metadata fields: {present_metadata}"),
            category="base-stripped-requests",
        )


def _read_bounded(fd: int, expected_size: int) -> bytes:
    """Read exactly ``expected_size`` bytes from ``fd``, then one extra byte.

    Rejects growth after ``fstat`` (the extra byte is non-empty) and short
    reads (file shrank or returned fewer than ``expected_size`` bytes). Never
    accumulates beyond ``MAX_CHECKPOINT_BYTES``. Raises ``CheckpointError``
    with category ``size-mismatch`` on any bound violation, or ``read`` on a
    raw ``OSError`` from ``os.read``.
    """
    if expected_size < 0 or expected_size > MAX_CHECKPOINT_BYTES:
        raise CheckpointError(_bound("checkpoint size out of range"), category="byte-size")
    chunks: list[bytes] = []
    remaining = expected_size
    try:
        while remaining > 0:
            chunk = os.read(fd, min(run_journal._READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise CheckpointError(_bound("checkpoint read failed"), category="read") from exc
    data = b"".join(chunks)
    if len(data) != expected_size:
        raise CheckpointError(_bound("checkpoint size mismatch"), category="size-mismatch")
    try:
        extra = os.read(fd, 1)
    except OSError as exc:
        raise CheckpointError(_bound("checkpoint read failed"), category="read") from exc
    if extra:
        raise CheckpointError(_bound("checkpoint grew after fstat"), category="size-mismatch")
    return data


def _close_guarded(fd: int, primary: CheckpointError | None, *, category: str = "close") -> CheckpointError | None:
    """Close ``fd``, translating an ``OSError`` into a bounded ``CheckpointError``.

    Returns the error to raise (or ``None``). A close failure never masks a
    prior ``primary`` failure: when ``primary`` is set it is returned
    unchanged and the close error is suppressed; when ``primary`` is ``None``
    a new bounded ``CheckpointError`` is returned. The fd is best-effort closed
    even on a first close failure.
    """
    close_err: CheckpointError | None = None
    try:
        os.close(fd)
    except OSError:
        if primary is None:
            close_err = CheckpointError(_bound("checkpoint close failed"), category=category)
    return primary if primary is not None else close_err


def validate_checkpoint(run_dir: Path, event: Any) -> bytes:
    """Validate a checkpoint payload, then open and verify the referenced file.

    Accepts either a payload Mapping or a ``run_journal.RunEvent`` whose
    ``payload`` is the checkpoint payload. Returns the verified checkpoint
    bytes. Raises ``CheckpointError`` on any validation failure. Raw
    ``OSError`` from ``run_journal._open_nofollow`` (including missing file
    and permission denial), ``fstat``, bounded ``os.read``, and ``os.close``
    is translated into a bounded categorized ``CheckpointError``; an earlier
    validation failure is preserved over a close failure.
    """
    run_dir = Path(run_dir)
    payload: Mapping[str, Any]
    if isinstance(event, run_journal.RunEvent):
        payload = event.payload
    elif isinstance(event, Mapping):
        payload = event
    else:
        raise CheckpointError(_bound("event must be a payload object or RunEvent"), category="payload-shape")

    _validate_payload(payload)

    sha = payload["sha256"]
    byte_size = payload["byte_size"]
    final_path = checkpoint_path(run_dir, sha)

    try:
        fd = run_journal._open_nofollow(final_path, os.O_RDONLY)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(_bound(f"cannot open checkpoint: {exc.diagnostic}"), category="open-fd") from exc
    except OSError as exc:
        raise CheckpointError(_bound("cannot open checkpoint"), category="open-fd") from exc
    primary: CheckpointError | None = None
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise CheckpointError(_bound("checkpoint fstat failed"), category="fstat") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CheckpointError(_bound("checkpoint path is not a regular file"), category="not-regular")
        if info.st_nlink != 1:
            raise CheckpointError(_bound("checkpoint link count is not one"), category="link-count")
        if info.st_size != byte_size:
            raise CheckpointError(_bound("checkpoint size mismatch"), category="size-mismatch")
        data = _read_bounded(fd, byte_size)
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != sha:
            raise CheckpointError(_bound("checkpoint digest mismatch"), category="digest-mismatch")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointError(_bound("checkpoint is not valid UTF-8"), category="utf8") from exc
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CheckpointError(_bound("checkpoint is not valid JSON"), category="json-object") from exc
        except RecursionError as exc:
            raise CheckpointError(_bound("checkpoint JSON nesting too deep"), category="json-object") from exc
        if not isinstance(obj, dict):
            raise CheckpointError(_bound("checkpoint is not a JSON object"), category="json-object")
        try:
            canonical = _writer_canonical_bytes(obj)
        except RecursionError as exc:
            raise CheckpointError(
                _bound("checkpoint canonical re-encode nesting too deep"), category="writer-bytes"
            ) from exc
        if canonical != data:
            raise CheckpointError(_bound("checkpoint bytes differ from writer canonical form"), category="writer-bytes")
        # When the payload declares body_kind base-stripped, the stored bytes
        # are the stripped base: they must still carry both durable request
        # fields and must exclude every journal-derived metadata field. This
        # is the recovery-side integrity check that the snapshot was legitimately
        # stripped (metadata removed, requests preserved).
        if payload.get("body_kind") == _BODY_KIND_BASE_STRIPPED:
            _validate_base_stripped_request_rules(obj)
        return data
    except CheckpointError as exc:
        primary = exc
        raise
    finally:
        close_err = _close_guarded(fd, primary)
        if close_err is not None and primary is None:
            raise close_err


def _verify_collision(final_path: Path, expected_bytes: bytes, expected_sha: str) -> None:
    """Open an existing final path no-follow and require byte+digest equality.

    Raises ``CheckpointError`` with category ``collision-unsafe`` for a
    symlink, non-regular inode, link count above one, or a raw ``OSError``
    from ``run_journal._open_nofollow`` (including missing file and
    permission denial), or ``collision-mismatch`` for byte or digest
    inequality. Raw ``OSError`` from ``fstat``, bounded ``os.read``, and
    ``os.close`` is translated into a bounded categorized
    ``CheckpointError``; an earlier collision failure is preserved over a
    close failure.
    """
    try:
        fd = run_journal._open_nofollow(final_path, os.O_RDONLY)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(_bound(f"collision-unsafe: {exc.diagnostic}"), category="collision-unsafe") from exc
    except OSError as exc:
        raise CheckpointError(_bound("collision-unsafe: open failed"), category="collision-unsafe") from exc
    primary: CheckpointError | None = None
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise CheckpointError(_bound("collision-unsafe: fstat failed"), category="fstat") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CheckpointError(_bound("collision-unsafe: not a regular file"), category="collision-unsafe")
        if info.st_nlink != 1:
            raise CheckpointError(_bound("collision-unsafe: link count above one"), category="collision-unsafe")
        if info.st_size != len(expected_bytes):
            raise CheckpointError(_bound("collision-mismatch: bytes differ"), category="collision-mismatch")
        data = _read_bounded(fd, len(expected_bytes))
        if data != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha:
            raise CheckpointError(_bound("collision-mismatch: bytes differ"), category="collision-mismatch")
    except CheckpointError as exc:
        primary = exc
        raise
    finally:
        close_err = _close_guarded(fd, primary)
        if close_err is not None and primary is None:
            raise close_err


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def _fsync_directory(path: Path) -> None:
    """Open ``path`` no-follow as a directory, fsync the descriptor, then close.

    The directory is opened via ``run_journal._open_nofollow`` with
    ``O_RDONLY | O_DIRECTORY`` so a raced-in symlink or non-directory inode
    is rejected before any fsync occurs (never fsync an attacker-selected
    target). The opened descriptor is ``fstat``-ed and required to be a
    directory, then ``fsync``-ed. Raises ``CheckpointError`` with category
    ``dir-fsync`` on any ``OSError`` from open, fstat, fsync, or close; a
    close failure is suppressed when an earlier open/fstat/fsync failure
    already raised (bounded error-precedence).
    """
    if not _supports_directory_fsync():
        return
    dir_flags = os.O_RDONLY | run_journal._O_DIRECTORY
    try:
        fd = run_journal._open_nofollow(path, dir_flags)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(
            _bound(f"checkpoint directory fsync failed: {exc.diagnostic}"), category="dir-fsync"
        ) from exc
    except OSError as exc:
        raise CheckpointError(_bound("checkpoint directory fsync failed"), category="dir-fsync") from exc
    primary: CheckpointError | None = None
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise CheckpointError(_bound("checkpoint directory fsync failed"), category="dir-fsync") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise CheckpointError(_bound("checkpoint directory fsync failed"), category="dir-fsync")
        try:
            os.fsync(fd)
        except OSError as exc:
            raise CheckpointError(_bound("checkpoint directory fsync failed"), category="dir-fsync") from exc
    except CheckpointError as exc:
        primary = exc
        raise
    finally:
        close_err = _close_guarded(fd, primary, category="dir-fsync")
        if close_err is not None and primary is None:
            raise close_err


def _cleanup_temp(tmp_path: Path, cp_dir: Path, primary: CheckpointError | None) -> CheckpointError | None:
    """Unlink the temp and fsync the checkpoint directory after a temp deletion.

    Used on the success, collision, and error paths so every successful temp
    deletion is followed by a checkpoint-directory fsync on POSIX. Returns the
    error to raise (or ``primary`` unchanged). A cleanup unlink or cleanup
    fsync failure never replaces a prior ``primary`` safety failure: when
    ``primary`` is set it is returned unchanged and cleanup errors are
    suppressed (the directory fsync still runs best-effort so the unlink is
    durable). When ``primary`` is ``None``, a cleanup-only failure returns a
    new bounded ``CheckpointError`` (``unlink`` or ``dir-fsync``).
    """
    cleanup_err: CheckpointError | None = None
    unlinked = False
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        # Preserve Path.unlink(missing_ok=True) semantics while using the
        # same os.unlink boundary on every supported Python version.
        unlinked = True
    except OSError:
        if primary is None:
            cleanup_err = CheckpointError(_bound("checkpoint temp cleanup failed"), category="unlink")
    else:
        unlinked = True
    if unlinked:
        try:
            _fsync_directory(cp_dir)
        except CheckpointError as exc:
            if primary is None and cleanup_err is None:
                cleanup_err = exc
    return primary if primary is not None else cleanup_err


def _abort_with_cleanup(
    tmp_path: Path, cp_dir: Path, primary: CheckpointError, *, cause: BaseException | None = None
) -> None:
    """Run temp cleanup, then raise ``primary`` (or a cleanup-only error)."""
    cleanup_err = _cleanup_temp(tmp_path, cp_dir, primary)
    if cleanup_err is not None and primary is None:
        raise cleanup_err from None
    if cause is not None:
        raise primary from cause
    raise primary


def publish_checkpoint_file(run_dir: Path, run_json_bytes: bytes) -> Path:
    """Crash-safe publish of ``run_json_bytes`` to the content-addressed checkpoint path.

    Creates the 0o700 ``recovery-checkpoints`` directory (no-follow mkdir),
    writes to a 0o600 same-directory temp, fsyncs, publishes by atomic
    no-replace ``os.link``, applies POSIX-guarded directory fsync, and unlinks
    the temp followed by another directory fsync. On ``EEXIST`` the existing
    final file is opened through the same no-follow regular single-link fd
    hardening and required to be byte and digest equal (a safe matching
    collision is a no-op that still unlinks the temp and fsyncs the
    directory); an unsafe inode or mismatched collision raises
    ``CheckpointError``. Every successful temp deletion (new-publish, EEXIST
    collision, and all error paths) is followed by a checkpoint-directory
    fsync on POSIX. The mkstemp fd is always closed, even when chmod fails; a
    close failure is translated to a bounded ``CheckpointError`` only when no
    earlier failure exists and never masks chmod/write/fsync failures.
    """
    run_dir = Path(run_dir)
    if not isinstance(run_json_bytes, (bytes, bytearray)):
        raise CheckpointError(_bound("run_json_bytes must be bytes"), category="payload-shape")
    run_json_bytes = bytes(run_json_bytes)
    byte_size = len(run_json_bytes)
    if byte_size > MAX_CHECKPOINT_BYTES:
        raise CheckpointError(_bound("checkpoint bytes exceed MAX_CHECKPOINT_BYTES"), category="byte-size")
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    cp_dir = checkpoint_dir(run_dir)
    try:
        run_journal._mkdir_private(cp_dir)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(
            _bound(f"cannot create checkpoint directory: {exc.diagnostic}"), category="mkdir"
        ) from exc
    except OSError as exc:
        raise CheckpointError(_bound("cannot create checkpoint directory"), category="mkdir") from exc
    final_path = checkpoint_path(run_dir, sha)

    try:
        fd, tmp_name = tempfile.mkstemp(dir=cp_dir, prefix=".checkpoint.", suffix=".tmp")
    except OSError as exc:
        raise CheckpointError(_bound("cannot create checkpoint temp"), category="temp-create") from exc
    tmp_path = Path(tmp_name)
    primary: CheckpointError | None = None
    try:
        try:
            run_journal._chmod_fd_or_path(fd, tmp_path, run_journal._FILE_MODE)
        except run_journal.RunJournalError as exc:
            raise CheckpointError(_bound("checkpoint temp chmod failed"), category="chmod") from exc
        except OSError as exc:
            raise CheckpointError(_bound("checkpoint temp chmod failed"), category="chmod") from exc
        try:
            written = os.write(fd, run_json_bytes)
            if written != len(run_json_bytes):
                raise CheckpointError(_bound("checkpoint temp write was partial"), category="io")
            os.fsync(fd)
        except CheckpointError:
            raise
        except OSError as exc:
            raise CheckpointError(_bound("checkpoint temp write failed"), category="io") from exc
    except CheckpointError as exc:
        primary = exc
    finally:
        close_err = _close_guarded(fd, primary)
        if close_err is not None and primary is None:
            primary = close_err

    if primary is not None:
        _abort_with_cleanup(tmp_path, cp_dir, primary)

    try:
        os.link(tmp_path, final_path)
    except FileExistsError:
        try:
            _verify_collision(final_path, run_json_bytes, sha)
        except CheckpointError as exc:
            _abort_with_cleanup(tmp_path, cp_dir, exc)
        cleanup_err = _cleanup_temp(tmp_path, cp_dir, None)
        if cleanup_err is not None:
            raise cleanup_err from None
        return final_path
    except OSError as exc:
        primary = CheckpointError(_bound("checkpoint link failed"), category="link")
        _abort_with_cleanup(tmp_path, cp_dir, primary, cause=exc)

    try:
        _fsync_directory(cp_dir)
    except CheckpointError as exc:
        _abort_with_cleanup(tmp_path, cp_dir, exc)

    cleanup_err = _cleanup_temp(tmp_path, cp_dir, None)
    if cleanup_err is not None:
        raise cleanup_err from None

    return final_path


# -- Checkpoint coordinator (issue #568 slice 5, Task 2) ----------------------


def _checkpoint_payload(
    run_json_bytes: bytes,
    *,
    paired_event_type: str | None,
    body_kind: str | None = None,
    pairing_key: str | None = None,
) -> dict[str, Any]:
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    payload: dict[str, Any] = {
        "path": f"events/{CHECKPOINT_DIR_NAME}/{sha}.json",
        "sha256": sha,
        "media_type": CHECKPOINT_MEDIA_TYPE,
        "byte_size": len(run_json_bytes),
        "privacy_class": CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": paired_event_type,
    }
    if body_kind is not None:
        payload["body_kind"] = body_kind
    if pairing_key is not None:
        payload["pairing_key"] = pairing_key
    return payload


def dispatch_pairing_key(event_type: str, seat: str, attempt: int) -> str:
    """Return the stable dispatch checkpoint identity for one worker action."""
    return hashlib.sha256(
        run_events.canonical_bytes({"event_type": event_type, "seat": seat, "attempt": attempt})
    ).hexdigest()


def _checkpoint_idempotency_key(
    sha: str,
    *,
    paired_event_type: str | None,
    body_kind: str | None = None,
    pairing_key: str | None = None,
) -> str:
    paired = paired_event_type if paired_event_type is not None else "none"
    if body_kind == _BODY_KIND_BASE_STRIPPED:
        prefix = _CHECKPOINT_IDEMPOTENCY_BASE_STRIPPED_PREFIX
    else:
        prefix = _CHECKPOINT_IDEMPOTENCY_PREFIX
    if pairing_key is None:
        key = f"{prefix}:{sha}:{paired}"
        if len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN:
            return key
        budget = run_events.MAX_IDEMPOTENCY_KEY_LEN - len(prefix) - 1 - len(sha) - 1
        if budget < 0:
            paired_digest = hashlib.sha256(paired.encode("utf-8")).hexdigest()[:16]
            return f"{prefix}:{sha}:{paired_digest}"
        return f"{prefix}:{sha}:{paired[:budget]}"

    key = f"{prefix}:{sha}:{paired}:{pairing_key}"
    if len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN:
        return key
    # Bound the pairing identity so the key stays within the envelope limit
    # regardless of event type length; the prefix and snapshot digest are fixed.
    budget = run_events.MAX_IDEMPOTENCY_KEY_LEN - len(prefix) - 1 - len(sha) - 1 - len(paired) - 1
    if budget < 0:
        # Pathological: prefix+sha already overflow the bound. Fall back to a
        # digest of the paired type so the key is still unique and bounded.
        identity_digest = hashlib.sha256(pairing_key.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{sha}:{identity_digest}"
    return f"{prefix}:{sha}:{paired}:{pairing_key[:budget]}"


def write_checkpoint(
    run_dir: Path,
    run_json_bytes: bytes,
    *,
    workspace: Path | None = None,
    paired_event_type: str | None,
    body_kind: str | None = None,
    pairing_key: str | None = None,
) -> "run_journal.RunEvent | None":
    """Publish a crash-safe recovery checkpoint and append the checkpoint event.

    Called from ``aboyeur._write_json`` BEFORE ``record_lifecycle_transition``
    and the ``run.json`` atomic replacement. Idempotently activates the
    lifecycle journal (``run_lifecycle.prepare_lifecycle_journal``), publishes
    the checkpoint bytes to the content-addressed checkpoint file
    (``publish_checkpoint_file``), and appends one
    ``run.snapshot.checkpointed`` event to the lifecycle journal.

    ``body_kind`` selects the checkpoint body authority. The default
    (``None``) is the slice-5 legacy-full form: the SHA, payload, and
    published file are over the raw ``run_json_bytes`` and the event
    payload omits ``body_kind``; the idempotency key remains exactly
    ``checkpoint:<sha256>:<paired-event-type-or-none>``. When
    ``body_kind`` is ``"base-stripped"`` the durable request rules are
    enforced on the parsed base (both durable request fields present and
    every journal-derived metadata field absent), the four metadata
    fields are stripped (``_strip_journal_metadata_from_base``), and the
    SHA, payload, published file, and event payload ``body_kind`` are
    derived from the stripped bytes; the idempotency key becomes
    ``checkpoint:base-stripped:<sha256>:<paired-event-type-or-none>``.

    No-op (no file, no event) when the journal is not active for the run.
    When the journal is active but the caller does not hold the matching active
    run lock, the coordinator fails closed with a bounded
    ``LifecycleJournalError`` BEFORE any publish or append: a lock-less writer
    on an active journal must never advance the journal or the snapshot, for
    any status (mapped or unmapped). Raises ``CheckpointError`` on any bounded
    checkpoint publish failure (including an unknown ``body_kind`` value or a
    base-stripped request/metadata rule violation); the failure surfaces BEFORE
    the lifecycle status append and BEFORE the ``run.json`` replacement. Raises
    ``LifecycleJournalError`` on a bounded journal read failure.

    ``run_lifecycle`` is imported lazily inside this function so the lifecycle
    layer may depend on this checkpoint substrate without a circular import.

    Validation-before-activation ordering: ``body_kind`` validation and, for
    ``base-stripped``, all decoding, canonical-byte validation, stripping,
    and durable-request validation run BEFORE
    ``prepare_lifecycle_journal``. A rejected candidate therefore raises
    before the lifecycle journal is activated, so an enrolled-but-inactive
    run under the lock never gains an ``events/`` directory or
    ``lifecycle.jsonl`` from a rejected write (the no-mutation precondition).
    After successful preprocessing, the existing prepare, journal-active, and
    owner checks run unchanged, and the crash-safe publish still precedes the
    journal append. Canonical metadata-bearing input is accepted and stripped;
    only the derived stored base must exclude the journal metadata fields.
    """
    from brigade import run_lifecycle  # lazy: avoid circular import

    run_dir = Path(run_dir).expanduser().resolve()
    run_json_bytes = bytes(run_json_bytes)
    if pairing_key is not None and (not isinstance(pairing_key, str) or not _HEX64.match(pairing_key)):
        raise CheckpointError(_bound("pairing_key must be 64-char lowercase hex"), category="pairing-key")
    # Validate body_kind and, for base-stripped, strip the journal-derived
    # metadata fields BEFORE SHA/payload/publish AND before
    # prepare_lifecycle_journal, so the content-addressed snapshot is stable
    # across projector re-derivations AND a rejected candidate never
    # activates the journal. The base-stripped invariants (both durable
    # request fields present, no journal metadata) are enforced on the
    # stripped result so the write path and the validate path agree: a base
    # carrying metadata fields is stripped (not rejected), and a base missing
    # a durable request field fails closed.
    if body_kind is not None and body_kind != _BODY_KIND_BASE_STRIPPED:
        raise CheckpointError(_bound("body_kind must be base-stripped"), category="body-kind")
    if body_kind == _BODY_KIND_BASE_STRIPPED:
        # Reject non-house-canonical incoming bytes BEFORE stripping, SHA,
        # payload, publish, or append. The standalone stripping helper may
        # canonicalize valid noncanonical JSON, but the write path must not:
        # a base-stripped snapshot is content-addressed over the stripped
        # bytes, so the incoming base must already be the aboyeur writer
        # canonical encoding.
        try:
            incoming_text = run_json_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointError(_bound("base is not valid UTF-8"), category="utf8") from exc
        try:
            incoming_obj = json.loads(incoming_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CheckpointError(_bound("base is not valid JSON"), category="json-object") from exc
        except RecursionError as exc:
            raise CheckpointError(_bound("base JSON nesting too deep"), category="json-object") from exc
        if not isinstance(incoming_obj, dict):
            raise CheckpointError(_bound("base is not a JSON object"), category="json-object")
        try:
            if _writer_canonical_bytes(incoming_obj) != run_json_bytes:
                raise CheckpointError(_bound("base bytes differ from writer canonical form"), category="writer-bytes")
        except RecursionError as exc:
            raise CheckpointError(_bound("base canonical re-encode nesting too deep"), category="writer-bytes") from exc
        publish_bytes = _strip_journal_metadata_from_base(run_json_bytes)
        try:
            stripped_obj = json.loads(publish_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CheckpointError(_bound("stripped base is not valid JSON"), category="json-object") from exc
        except RecursionError as exc:
            raise CheckpointError(_bound("stripped base JSON nesting too deep"), category="json-object") from exc
        if not isinstance(stripped_obj, dict):
            raise CheckpointError(_bound("stripped base is not a JSON object"), category="json-object")
        _validate_base_stripped_request_rules(stripped_obj)
    else:
        publish_bytes = run_json_bytes
    run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
    journal_path = run_lifecycle._journal_path(run_dir)
    if not journal_path.is_file():
        return None
    # Journal active: every publish/append must come from the process holding
    # the matching active run lock. A lock-less writer fails closed before any
    # checkpoint/status append and before run.json changes, for every status.
    if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("lifecycle journal append requires the active run lock for this run")
        )
    sha = hashlib.sha256(publish_bytes).hexdigest()
    payload = _checkpoint_payload(
        publish_bytes,
        paired_event_type=paired_event_type,
        body_kind=body_kind,
        pairing_key=pairing_key,
    )
    # Crash-safe publish FIRST. A CheckpointError here fails before the
    # lifecycle status append and before run.json replacement.
    publish_checkpoint_file(run_dir, publish_bytes)
    try:
        report = run_journal.read_journal(journal_path)
        if report.partial_tail is not None or report.chain_errors:
            raise run_journal.ChainIntegrityError(run_events._bound(run_lifecycle._CHAIN_CATEGORY))
        idempotency_key = _checkpoint_idempotency_key(
            sha,
            paired_event_type=paired_event_type,
            body_kind=body_kind,
            pairing_key=pairing_key,
        )
        return run_journal.append_event(
            journal_path,
            run_id=run_lifecycle._run_id_from_dir(run_dir),
            event_type=CHECKPOINT_EVENT_TYPE,
            payload=payload,
            idempotency_key=idempotency_key,
            expected_previous_sequence=report.events[-1].sequence if report.events else 0,
        )
    except CheckpointError:
        raise
    except run_journal.RunJournalError as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    except run_events.CanonicalizationError as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    except OSError as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc


# -- Checkpoint-backed recovery (issue #568 slice 5, Task 5) -------------------


def latest_checkpoint_event(events: list["run_journal.RunEvent"]) -> "run_journal.RunEvent | None":
    """Select the highest-sequence ``run.snapshot.checkpointed`` event.

    Returns ``None`` when ``events`` is empty or contains no checkpoint
    events. Otherwise returns the checkpoint event with the largest
    ``sequence`` value. Sequence uniqueness is enforced by the journal
    reader, so a tie is not a concern here.
    """
    latest: run_journal.RunEvent | None = None
    for event in events:
        if event.event_type != CHECKPOINT_EVENT_TYPE:
            continue
        if latest is None or event.sequence > latest.sequence:
            latest = event
    return latest


def _restore_run_json_from_checkpoint(
    run_dir: Path,
    checkpoint_bytes: bytes,
    *,
    run_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Restore ``run.json`` from verified checkpoint bytes when needed.

    Restores only when ``run_meta`` is ``None`` (run.json missing or
    unparseable). A present-but-corrupt run.json is preserved by renaming
    it to ``run.json.corrupt-<uuid>`` before the atomic replacement; a
    rename ``OSError`` is translated to a bounded ``CheckpointError``
    with category ``preserve-corrupt`` so the corrupt original is never
    silently overwritten. The restore uses ``localio.write_text_atomic``
    so a reader never observes a half-written file. Returns the parsed
    checkpoint mapping. When ``run_meta`` is already a dict the run.json
    is left untouched and the parsed checkpoint mapping is returned.
    """
    from brigade import localio  # lazy: keep the import local for stdlib-only surface

    try:
        repaired = json.loads(checkpoint_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CheckpointError(_bound("checkpoint bytes are not valid JSON"), category="json-object") from exc
    if not isinstance(repaired, dict):
        raise CheckpointError(_bound("checkpoint is not a JSON object"), category="json-object")
    if run_meta is not None:
        return repaired
    run_json = run_dir / "run.json"
    if run_json.is_file():
        # Preserve the corrupt original before replacing it.
        backup = run_json.with_name(f"run.json.corrupt-{uuid4().hex}")
        try:
            run_json.rename(backup)
        except OSError as exc:
            raise CheckpointError(_bound("could not preserve corrupt run.json"), category="preserve-corrupt") from exc
    try:
        localio.write_text_atomic(run_json, checkpoint_bytes.decode("utf-8"))
    except OSError as exc:
        raise CheckpointError(_bound("could not restore run.json from checkpoint"), category="restore-write") from exc
    return repaired


def _parse_checkpoint_object(checkpoint_bytes: bytes) -> dict[str, Any]:
    """Parse verified checkpoint bytes into a dict for coverage derivation.

    ``validate_checkpoint`` already proved the bytes are valid UTF-8 JSON
    equal to the writer canonical encoding, so this parse cannot fail on
    well-formed input. A defensive failure (e.g. concurrent in-place
    mutation between validate and parse on a path that bypassed the
    single-link fd check) surfaces a bounded ``CheckpointError`` rather
    than a raw ``ValueError``. Returns the parsed mapping.
    """
    try:
        obj = json.loads(checkpoint_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CheckpointError(_bound("checkpoint bytes are not valid JSON"), category="json-object") from exc
    if not isinstance(obj, dict):
        raise CheckpointError(_bound("checkpoint is not a JSON object"), category="json-object")
    return obj


def _paired_event_derived_status(event: "run_journal.RunEvent") -> str | None:
    """Derive the run.json status a paired lifecycle status event carries.

    Reuses the canonical ``run_projector`` event-to-status tables
    (``EVENT_STATUS`` and ``_PAYLOAD_STATUS_RULES``) with no duplicate
    mapping. Returns the derived status, or ``None`` when the event has no
    derivable status (an unmapped type or a payload-driven row whose
    payload status is missing/disallowed). A ``None`` result means the
    paired event cannot match any checkpoint status, so the caller treats
    it as an uncovered tail.
    """
    from brigade import run_projector  # lazy: avoid import cycle

    event_type = event.event_type
    if event_type == "run.dispatch.completed" and not run_projector._has_dispatch_identity(event.payload):
        return "result-processing"
    if event_type in run_projector.EVENT_STATUS:
        return run_projector.EVENT_STATUS[event_type]
    rule = run_projector._PAYLOAD_STATUS_RULES.get(event_type)
    if rule is None:
        return None
    allowed, derived = rule
    payload_status = event.payload.get("status") if isinstance(event.payload, Mapping) else None
    if payload_status not in allowed:
        return None
    return derived if derived is not None else payload_status


def _verify_coverage(
    events: list["run_journal.RunEvent"],
    latest: "run_journal.RunEvent",
    checkpoint_obj: Mapping[str, Any],
) -> None:
    """Verify the latest checkpoint covers the journal tail (issue #568 slice 5).

    The latest checkpoint event at sequence N covers either itself as the
    tail, or exactly one immediately following event at N+1 whose
    ``event_type`` equals the checkpoint's ``paired_event_type`` and whose
    derived status equals the status in the validated checkpoint bytes.
    When ``paired_event_type`` is null, the checkpoint covers only itself
    as the tail. Anything else fails uncovered-tail. ``checkpoint_obj`` is
    the parsed validated checkpoint bytes; its ``status`` is the authoritative
    status the paired event must derive to.
    """
    paired_event_type = latest.payload.get("paired_event_type")
    pairing_key = latest.payload.get("pairing_key")
    tail = events[-1]
    if latest.sequence == tail.sequence:
        # A dispatch pairing key promises a specific identity-bearing fact.
        # A checkpoint at tail means that fact never committed, so recovery
        # must surface the incomplete pair instead of treating the checkpoint
        # as covered.
        if pairing_key is not None:
            raise CheckpointError(
                _bound("dispatch checkpoint pair is incomplete"),
                category="incomplete-pair",
            )
        # Legacy lifecycle checkpoints remain recoverable at tail after a
        # crash before their paired status append.
        return
    if paired_event_type is None:
        # Null pairing covers only checkpoint-at-tail; any following event
        # is uncovered.
        raise CheckpointError(_bound("journal tail is not covered by the latest checkpoint"), category="uncovered-tail")
    # Exactly one immediately following event must exist at N+1.
    following = [e for e in events if e.sequence == latest.sequence + 1]
    if len(following) != 1 or following[0].sequence != tail.sequence:
        # Two or more events after the checkpoint (or a non-contiguous tail).
        raise CheckpointError(_bound("journal tail is not covered by the latest checkpoint"), category="uncovered-tail")
    paired = following[0]
    if paired.event_type != paired_event_type:
        raise CheckpointError(_bound("journal tail is not covered by the latest checkpoint"), category="uncovered-tail")
    if pairing_key is not None:
        seat = paired.payload.get("seat")
        attempt = paired.payload.get("attempt")
        if (
            paired.event_type not in _DISPATCH_FACT_EVENT_TYPES
            or not isinstance(seat, str)
            or not seat
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise CheckpointError(_bound("checkpoint pairing identity is invalid"), category="pairing")
        if pairing_key != dispatch_pairing_key(paired.event_type, seat, attempt):
            raise CheckpointError(_bound("checkpoint pairing identity does not match event"), category="pairing")
        # Identity-bearing dispatch facts are status-neutral. Their paired
        # checkpoint preserves the aggregate status already in run.json, so
        # no event-derived status comparison applies.
        return
    derived = _paired_event_derived_status(paired)
    checkpoint_status = checkpoint_obj.get("status")
    if not isinstance(checkpoint_status, str) or derived != checkpoint_status:
        raise CheckpointError(_bound("journal tail is not covered by the latest checkpoint"), category="uncovered-tail")


def recover_from_checkpoint(
    run_dir: Path,
    run_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate the lifecycle journal and restore run.json from the latest checkpoint.

    Reads the journal with byte/event bounds (``read_journal_bounded``),
    quarantining a partial tail via ``recover_partial_tail`` and re-reading
    before any derivation. Enforces a gap-free, contiguous sequence
    starting at 1 with correct previous-digest chaining (reported by the
    bounded reader as ``chain_errors``), a single journal ``run_id``
    equal to the run directory name across every envelope, highest
    checkpoint validity (``validate_checkpoint``), event/checkpoint/
    directory ``run_id`` equality, and checkpoint coverage of the journal
    tail. The checkpoint bytes are validated before their status is used.
    The latest checkpoint event at sequence N covers itself as the tail,
    or exactly one immediately following event at N+1 whose ``event_type``
    equals the checkpoint's ``paired_event_type`` and whose derived status
    equals the status in the validated checkpoint bytes. A null
    ``paired_event_type`` covers only checkpoint-at-tail. Anything else
    fails uncovered-tail.

    Restore-byte selection (issue #568 slice 6): when the latest checkpoint
    payload carries ``body_kind`` ``"base-stripped"`` and the validated
    checkpoint base carries ``run_journal_authority_requested`` set to True,
    the canonical stripped base is parsed and projected with
    ``run_projector.project_run_snapshot(base, events, journal_present=True)``;
    the projector bytes are the intermediate recovery receipt. A
    ``run_projector.ProjectionError`` is wrapped as a bounded
    ``CheckpointError`` with category ``projection`` (the original preserved
    as the cause); recovery never falls back to an earlier checkpoint or to
    a legacy-full restore. When ``body_kind`` is absent the legacy-full
    restore uses the verified checkpoint bytes verbatim.

    Restores ``run.json`` only when it is missing or unparseable
    (``run_meta is None``); a corrupt original is preserved by rename and
    the restore uses ``localio.write_text_atomic``. Returns the repaired
    validated mapping parsed from the selected restore bytes. Raises
    ``CheckpointError`` (bounded, categorized) on any validation or
    restoration failure; nothing is restored on failure.
    """
    from brigade import localio  # noqa: F401  (kept for symmetry with the restore helper)
    from brigade import run_lifecycle  # lazy: avoid circular import

    try:
        run_dir = Path(run_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise CheckpointError(_bound("could not resolve run directory"), category="path-resolve") from exc
    run_id = run_dir.name
    journal_path = run_lifecycle._journal_path(run_dir)
    if not journal_path.is_file():
        raise CheckpointError(_bound("lifecycle journal not found"), category="no-journal")

    try:
        report = run_journal.read_journal_bounded(journal_path)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(_bound(f"could not read journal: {exc.diagnostic}"), category="journal-read") from exc
    except OSError as exc:
        raise CheckpointError(_bound("could not read journal"), category="journal-read") from exc
    if report.partial_tail is not None:
        quarantine_dir = run_dir / "events" / "quarantine"
        try:
            run_journal.recover_partial_tail(journal_path, quarantine_dir)
        except run_journal.RunJournalError as exc:
            raise CheckpointError(
                _bound(f"could not quarantine partial tail: {exc.diagnostic}"), category="partial-tail"
            ) from exc
        except OSError as exc:
            raise CheckpointError(_bound("could not quarantine partial tail"), category="partial-tail") from exc
        try:
            report = run_journal.read_journal_bounded(journal_path)
        except run_journal.RunJournalError as exc:
            raise CheckpointError(
                _bound(f"could not reread journal: {exc.diagnostic}"), category="journal-read"
            ) from exc
        except OSError as exc:
            raise CheckpointError(_bound("could not reread journal"), category="journal-read") from exc
        if report.partial_tail is not None:
            raise CheckpointError(_bound("partial tail persisted after quarantine"), category="partial-tail")
    if report.chain_errors:
        raise CheckpointError(_bound("journal chain is not derivable"), category="chain")
    if not report.events:
        raise CheckpointError(_bound("journal has no events"), category="empty-journal")

    expected_sequence = 1
    expected_previous: str | None = None
    for event in report.events:
        if event.sequence != expected_sequence:
            raise CheckpointError(_bound("journal sequence break"), category="chain")
        if event.sequence == 1:
            if event.previous_digest is not None:
                raise CheckpointError(_bound("sequence 1 previous_digest must be null"), category="chain")
        elif event.previous_digest != expected_previous:
            raise CheckpointError(_bound("journal previous_digest does not link"), category="chain")
        if event.run_id != run_id:
            raise CheckpointError(_bound("journal run_id does not match run directory"), category="run-id-mismatch")
        expected_sequence = event.sequence + 1
        expected_previous = event.event_digest

    latest = latest_checkpoint_event(report.events)
    if latest is None:
        raise CheckpointError(_bound("no checkpoint event in journal"), category="no-checkpoint")
    if latest.run_id != run_id:
        raise CheckpointError(_bound("checkpoint run_id does not match run directory"), category="run-id-mismatch")

    # Validate the checkpoint bytes BEFORE using their status. The coverage
    # check reads the status from the checkpointed run.json bytes, so the
    # bytes must be verified first. Restoration stays validation-before-
    # mutation: every validation (payload, fd, chain, coverage) completes
    # before the only mutation (the restore write).
    checkpoint_bytes = validate_checkpoint(run_dir, latest)
    checkpoint_obj = _parse_checkpoint_object(checkpoint_bytes)
    _verify_coverage(report.events, latest, checkpoint_obj)
    restore_bytes = checkpoint_bytes
    if (
        latest.payload.get("body_kind") == _BODY_KIND_BASE_STRIPPED
        and checkpoint_obj.get("run_journal_authority_requested") is True
    ):
        # Authority path (issue #568 slice 6): the validated checkpoint body is
        # the canonical stripped base and the base carries the journal-authority
        # request, so the recovery receipt is the projector's re-derivation of
        # the snapshot over the verified event sequence -- never the stripped
        # bytes verbatim. A projection failure fails closed as a bounded
        # CheckpointError: no fallback to an earlier checkpoint or to a
        # legacy-full restore.
        from brigade import run_projector  # lazy: avoid import cycle

        try:
            projection = run_projector.project_run_snapshot(checkpoint_obj, report.events, journal_present=True)
            restore_bytes = projection.to_bytes()
        except run_projector.ProjectionError as exc:
            raise CheckpointError(_bound("projection failed"), category="projection") from exc
    return _restore_run_json_from_checkpoint(run_dir, restore_bytes, run_meta=run_meta)
