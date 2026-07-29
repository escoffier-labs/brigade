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

from brigade import run_events, run_journal, runguard
from brigade.run_events import _HEX64, _bound

CHECKPOINT_EVENT_TYPE = "run.snapshot.checkpointed"
CHECKPOINT_MEDIA_TYPE = "application/vnd.brigade.run+json"
CHECKPOINT_PRIVACY_CLASS = "private"
CHECKPOINT_DIR_NAME = "recovery-checkpoints"
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_EVENTS = 512

_CHECKPOINT_PAYLOAD_KEYS = frozenset(
    {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type"}
)
_CHECKPOINT_IDEMPOTENCY_PREFIX = "checkpoint"


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
    if keys != _CHECKPOINT_PAYLOAD_KEYS:
        missing = sorted(_CHECKPOINT_PAYLOAD_KEYS - keys)
        extra = sorted(keys - _CHECKPOINT_PAYLOAD_KEYS)
        raise CheckpointError(
            _bound(f"payload keys mismatch: missing={missing}, extra={extra}"),
            category="payload-keys",
        )

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
    elif paired not in _mapped_lifecycle_event_types():
        raise CheckpointError(
            _bound("paired_event_type is not a mapped lifecycle status event"),
            category="paired-event-type",
        )


def _writer_canonical_bytes(obj: Any) -> bytes:
    """Replicate ``aboyeur._write_json`` canonical encoding for writer-byte equality."""
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


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
        tmp_path.unlink(missing_ok=True)
        unlinked = True
    except OSError:
        if primary is None:
            cleanup_err = CheckpointError(_bound("checkpoint temp cleanup failed"), category="unlink")
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


def _checkpoint_payload(run_json_bytes: bytes, *, paired_event_type: str | None) -> dict[str, Any]:
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    return {
        "path": f"events/{CHECKPOINT_DIR_NAME}/{sha}.json",
        "sha256": sha,
        "media_type": CHECKPOINT_MEDIA_TYPE,
        "byte_size": len(run_json_bytes),
        "privacy_class": CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": paired_event_type,
    }


def _checkpoint_idempotency_key(sha: str, *, paired_event_type: str | None) -> str:
    paired = paired_event_type if paired_event_type is not None else "none"
    key = f"{_CHECKPOINT_IDEMPOTENCY_PREFIX}:{sha}:{paired}"
    if len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN:
        return key
    # Bound the paired-event-type tail so the key stays within the envelope
    # limit regardless of event_type length; the sha and prefix are fixed.
    budget = run_events.MAX_IDEMPOTENCY_KEY_LEN - len(_CHECKPOINT_IDEMPOTENCY_PREFIX) - 1 - len(sha) - 1
    if budget < 0:
        # Pathological: prefix+sha already overflow the bound. Fall back to a
        # digest of the paired type so the key is still unique and bounded.
        paired_digest = hashlib.sha256(paired.encode("utf-8")).hexdigest()[:16]
        return f"{_CHECKPOINT_IDEMPOTENCY_PREFIX}:{sha}:{paired_digest}"
    return f"{_CHECKPOINT_IDEMPOTENCY_PREFIX}:{sha}:{paired[:budget]}"


def write_checkpoint(
    run_dir: Path,
    run_json_bytes: bytes,
    *,
    workspace: Path | None = None,
    paired_event_type: str | None,
) -> "run_journal.RunEvent | None":
    """Publish a crash-safe recovery checkpoint and append the checkpoint event.

    Called from ``aboyeur._write_json`` BEFORE ``record_lifecycle_transition``
    and the ``run.json`` atomic replacement. Idempotently activates the
    lifecycle journal (``run_lifecycle.prepare_lifecycle_journal``), publishes
    ``run_json_bytes`` to the content-addressed checkpoint file
    (``publish_checkpoint_file``), and appends one
    ``run.snapshot.checkpointed`` event to the lifecycle journal with
    idempotency key ``checkpoint:<sha256>:<paired-event-type-or-none>``.

    No-op (no file, no event) when the journal is not active for the run.
    When the journal is active but the caller does not hold the matching active
    run lock, the coordinator fails closed with a bounded
    ``LifecycleJournalError`` BEFORE any publish or append: a lock-less writer
    on an active journal must never advance the journal or the snapshot, for
    any status (mapped or unmapped). Raises ``CheckpointError`` on any bounded
    checkpoint publish failure; the failure surfaces BEFORE the lifecycle
    status append and BEFORE the ``run.json`` replacement. Raises
    ``LifecycleJournalError`` on a bounded journal read failure.

    ``run_lifecycle`` is imported lazily inside this function so the lifecycle
    layer may depend on this checkpoint substrate without a circular import.
    """
    from brigade import run_lifecycle  # lazy: avoid circular import

    run_dir = Path(run_dir).expanduser().resolve()
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
    run_json_bytes = bytes(run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    payload = _checkpoint_payload(run_json_bytes, paired_event_type=paired_event_type)
    # Crash-safe publish FIRST. A CheckpointError here fails before the
    # lifecycle status append and before run.json replacement.
    publish_checkpoint_file(run_dir, run_json_bytes)
    try:
        report = run_journal.read_journal(journal_path)
        if report.partial_tail is not None or report.chain_errors:
            raise run_journal.ChainIntegrityError(run_events._bound(run_lifecycle._CHAIN_CATEGORY))
        idempotency_key = _checkpoint_idempotency_key(sha, paired_event_type=paired_event_type)
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
