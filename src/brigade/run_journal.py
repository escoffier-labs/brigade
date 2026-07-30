"""Append-only per-run lifecycle journal kernel (issue #568, slice 1).

Persists ``brigade.run_event.v1`` envelopes as canonical UTF-8 JSON lines at
``<run-dir>/events/lifecycle.jsonl``. Append is a single bounded ``os.write``
to an ``O_APPEND`` descriptor with returned-byte-count verification and
``fsync`` before return. The append API requires ``expected_previous_sequence``
and enforces contiguous sequence, previous-digest chaining, and idempotency by
key + request digest (same key + same digest returns the existing event; same
key + different digest raises a typed conflict without appending). Tail state
is derived fail-closed: every complete line must be a validated envelope whose
raw bytes exactly equal its canonical form, continuing a gap-free,
duplicate-free, digest-linked sequence; any deviation raises a bounded typed
error and no state is derived from it. Run-artifact permissions are private:
the ``events`` and quarantine directories are 0o700 and journal/quarantine
files are 0o600. On POSIX hosts with ``O_NOFOLLOW``, ``O_DIRECTORY``, and
``fchmod``, modes are enforced via ``fchmod`` on no-follow descriptors so a
permissive umask cannot widen them and a pre-placed symlink cannot redirect
reads, writes, or permission correction. On hosts where any of those APIs are
absent (notably Windows), the module still imports and journal operations use
a symlink-rejecting path-mode fallback: the final path component is rejected
via ``lstat`` before open, opened regular files are verified against the path
with ``lstat``/``fstat`` inode identity before mutation, and modes are applied
with ``chmod`` on the verified path when ``fchmod`` is unavailable. Normal
readers report a partial final line without mutating it; a separate
recovery-only API quarantines the incomplete suffix (write-once, collision-safe)
before truncating.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from brigade import run_events
from brigade.run_events import CanonicalizationError, canonical_bytes

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_HAS_O_NOFOLLOW = _O_NOFOLLOW != 0
_HAS_O_DIRECTORY = _O_DIRECTORY != 0
_HAS_FCHMOD = hasattr(os, "fchmod")
# O_NOFOLLOW rejects symlinked targets so a pre-placed symlink cannot redirect
# journal writes or quarantine captures outside the private run-artifact tree.
_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND
if _HAS_O_NOFOLLOW:
    _OPEN_FLAGS |= _O_NOFOLLOW
_READ_CHUNK = 65536
_MAX_QUARANTINE_ATTEMPTS = 16


class RunJournalError(RuntimeError):
    """Base class for run-journal failures. Carries a bounded ``diagnostic``."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class StaleSequenceError(RunJournalError):
    """``expected_previous_sequence`` did not match the journal tail."""


class IdempotencyConflict(RunJournalError):
    """Same idempotency key recurred with a different request digest."""

    def __init__(
        self,
        diagnostic: str,
        *,
        existing_event_id: str,
        request_digest: str,
        existing_request_digest: str,
    ) -> None:
        super().__init__(diagnostic)
        self.existing_event_id = existing_event_id
        self.request_digest = request_digest
        self.existing_request_digest = existing_request_digest


class PartialWriteError(RunJournalError):
    """``os.write`` returned fewer bytes than the canonical line."""


class ChainIntegrityError(RunJournalError):
    """Chain verification detected a gap, duplicate, or digest mismatch."""


class UnknownFieldError(RunJournalError):
    """An envelope or payload carried a key outside the closed allowlist."""


class UnknownEventTypeError(RunJournalError):
    """An envelope carried an event_type outside the registry."""


class SchemaVersionError(RunJournalError):
    """An envelope carried an unknown schema string or version."""


class PartialTailError(RunJournalError):
    """The journal ends in a partial (unterminated) line; recovery is required."""


@dataclass(frozen=True)
class RunEvent:
    """A validated, self-consistent run_event.v1 envelope read from the journal."""

    schema: str
    schema_version: int
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    recorded_at: str
    idempotency_key: str
    request_digest: str
    previous_digest: str | None
    event_digest: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "recorded_at": self.recorded_at,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "previous_digest": self.previous_digest,
            "event_digest": self.event_digest,
            "payload": dict(self.payload),
        }


@dataclass
class JournalReport:
    """Result of a non-mutating journal read."""

    events: list[RunEvent] = field(default_factory=list)
    partial_tail: bytes | None = None
    chain_errors: list[str] = field(default_factory=list)


@dataclass
class RecoveryReport:
    """Result of a recovery-only partial-tail quarantine + truncate.

    ``quarantine_path`` is None when the journal had no partial tail to
    capture (nothing was written).
    """

    partial_bytes: bytes
    quarantine_path: Path | None


def _reject_symlink_final_component(path: Path) -> None:
    """Reject a symlinked final path component via lstat (fallback no-follow guard)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RunJournalError(_bound(f"cannot stat path: {path.name}")) from exc
    if stat.S_ISLNK(st.st_mode):
        raise RunJournalError(_bound(f"refusing symlinked path: {path.name}"))


def _verify_fd_identity(path: Path, fd: int) -> None:
    """Verify an opened descriptor still refers to the same inode as lstat(path)."""
    try:
        st_path = os.lstat(path)
        st_fd = os.fstat(fd)
    except OSError as exc:
        raise RunJournalError(_bound(f"cannot verify opened path: {path.name}")) from exc
    if st_path.st_ino != st_fd.st_ino or st_path.st_dev != st_fd.st_dev:
        raise RunJournalError(_bound(f"opened path identity mismatch: {path.name}"))


def _chmod_fd_or_path(fd: int, path: Path, mode: int) -> None:
    """Apply mode via fchmod when available, else chmod on a verified path."""
    if _HAS_FCHMOD:
        os.fchmod(fd, mode)
        return
    _verify_fd_identity(path, fd)
    os.chmod(path, mode)


def _open_nofollow(path: Path, flags: int, mode: int = 0o666) -> int:
    """Open a path without following a symlinked final component.

    When ``O_NOFOLLOW`` is available, ``os.open`` rejects symlinked targets
    directly (ELOOP or ENOTDIR for ``O_DIRECTORY`` on a symlinked directory).
    Otherwise the final component is rejected via ``lstat`` before open and
    inode identity is verified with ``lstat``/``fstat`` before the descriptor
    is returned.
    """
    wants_directory = bool(flags & _O_DIRECTORY)
    open_flags = flags
    if wants_directory and not _HAS_O_DIRECTORY:
        open_flags &= ~_O_DIRECTORY

    if _HAS_O_NOFOLLOW:
        try:
            return os.open(path, open_flags | _O_NOFOLLOW, mode)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RunJournalError(_bound(f"refusing symlinked path: {path.name}")) from exc
            if exc.errno == errno.ENOTDIR and wants_directory:
                # Linux reports ENOTDIR (not ELOOP) for O_DIRECTORY|O_NOFOLLOW on
                # a symlinked directory; it is the same rejection.
                raise RunJournalError(_bound(f"refusing symlinked path: {path.name}")) from exc
            raise

    _reject_symlink_final_component(path)
    if wants_directory and not _HAS_O_DIRECTORY:
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise RunJournalError(_bound(f"directory path does not exist: {path.name}")) from exc
        except OSError as exc:
            raise RunJournalError(_bound(f"cannot stat path: {path.name}")) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise RunJournalError(_bound(f"path is not a directory: {path.name}"))

    fd = os.open(path, open_flags, mode)
    try:
        _verify_fd_identity(path, fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def _enforce_dir_mode(path: Path) -> None:
    """Enforce 0o700 on a directory without following a symlinked final component.

    With ``O_NOFOLLOW`` and ``fchmod``, mode is corrected on the opened
    directory descriptor. Without those APIs, the symlink guard and inode
    identity check run first, then ``chmod`` is applied on the verified path.
    """
    _reject_symlink_final_component(path)
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise RunJournalError(_bound(f"cannot stat path: {path.name}")) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise RunJournalError(_bound(f"path is not a directory: {path.name}"))
    if not _HAS_O_DIRECTORY:
        if stat.S_IMODE(st.st_mode) != _DIR_MODE:
            os.chmod(path, _DIR_MODE)
        return
    dir_flags = os.O_RDONLY | _O_DIRECTORY
    fd = _open_nofollow(path, dir_flags)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != _DIR_MODE:
            _chmod_fd_or_path(fd, path, _DIR_MODE)
    finally:
        os.close(fd)


def _enforce_file_mode(path: Path) -> None:
    """Enforce 0o600 on a regular file without following a symlinked final component.

    With ``O_NOFOLLOW`` and ``fchmod``, mode is corrected on the opened file
    descriptor. Without those APIs, the symlink guard and inode identity check
    run first, then ``chmod`` is applied on the verified path.
    """
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RunJournalError(_bound(f"journal path is not a regular file: {path.name}"))
        if stat.S_IMODE(info.st_mode) != _FILE_MODE:
            _chmod_fd_or_path(fd, path, _FILE_MODE)
    finally:
        os.close(fd)


def _mkdir_private(path: Path) -> None:
    """Create a directory with mode 0o700 at mkdir time, then enforce it."""
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    _enforce_dir_mode(path)


def _read_bytes_nofollow(path: Path) -> bytes:
    """Read a regular file without following a symlinked final component.

    Uses an ``O_NOFOLLOW`` descriptor when available; otherwise applies the
    ``lstat`` symlink guard and inode identity verification before reading.
    """
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RunJournalError(_bound(f"journal path is not a regular file: {path.name}"))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def ensure_journal(journal_path: Path) -> None:
    """Create the events directory (0o700) and journal file (0o600) if missing.

    Modes are applied at mkdir/open time and re-enforced without following a
    symlinked final component: via ``fchmod`` on no-follow descriptors when
    ``O_NOFOLLOW`` and ``fchmod`` exist, otherwise via ``lstat`` rejection,
    inode identity verification, and path ``chmod``.
    """
    journal_path = Path(journal_path)
    _mkdir_private(journal_path.parent)
    if not journal_path.exists():
        fd = _open_nofollow(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        os.close(fd)
    _enforce_file_mode(journal_path)


class _DuplicateKeyError(ValueError):
    """Internal: a JSON object carried the same key twice."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKeyError(key)
        obj[key] = value
    return obj


def _parse_canonical_line(line: bytes) -> dict[str, Any]:
    """Parse one journal line, failing closed on any deviation.

    The line must be a validated run_event.v1 envelope whose raw bytes exactly
    equal its canonical form. Raises ChainIntegrityError with a bounded
    diagnostic on: invalid UTF-8 or JSON, duplicate JSON keys, non-object JSON,
    uncanonicalizable values (floats, booleans, oversized integers), byte-level
    differences from canonical form (whitespace, key order, ASCII escapes), or
    failed envelope validation (bad fields, recomputed-digest or event_id
    mismatch).
    """
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChainIntegrityError(_bound("journal line is not valid UTF-8")) from exc
    try:
        env = json.loads(text, object_pairs_hook=_object_pairs_no_duplicates)
    except _DuplicateKeyError as exc:
        raise ChainIntegrityError(_bound(f"journal line repeats JSON key {exc.key!r}")) from exc
    except json.JSONDecodeError as exc:
        raise ChainIntegrityError(_bound(f"journal line is not valid JSON: {exc}")) from exc
    except ValueError as exc:
        raise ChainIntegrityError(_bound("journal line integer exceeds parser limits")) from exc
    except RecursionError as exc:
        raise ChainIntegrityError(_bound("journal line is nested too deeply")) from exc
    if not isinstance(env, dict):
        raise ChainIntegrityError(_bound("journal line is not a JSON object"))
    try:
        canonical = canonical_bytes(env)
    except CanonicalizationError as exc:
        raise ChainIntegrityError(_bound(f"journal line is not canonicalizable: {exc}")) from exc
    if canonical != line:
        raise ChainIntegrityError(_bound("journal line bytes differ from canonical form"))
    errors = run_events.validate_event(env)
    if errors:
        raise ChainIntegrityError(_bound("journal line failed validation: " + "; ".join(errors)))
    return env


def _read_tail_state(
    journal_path: Path,
) -> tuple[int, str | None, dict[str, dict[str, Any]], bytes | None]:
    """Return (last_sequence, last_event_digest, idempotency_index, partial_tail).

    Fail closed: every complete line must be a validated canonical envelope
    (see ``_parse_canonical_line``) continuing a strict, gap-free,
    duplicate-free sequence whose previous_digest links to the prior
    event_digest. Any deviation raises ChainIntegrityError and no state is
    derived from it; last_sequence and last_digest come only from the final
    verified line. The idempotency index maps idempotency_key -> validated
    envelope dict, and a repeated key raises. last_sequence is 0 for an
    empty/missing journal. A non-empty trailing segment without a terminating
    newline is returned as ``partial_tail`` so the append path can refuse to
    write over it (appending over a partial tail would glue the new line to
    the partial bytes and corrupt the journal).
    """
    last_sequence = 0
    last_digest: str | None = None
    index: dict[str, dict[str, Any]] = {}
    partial_tail: bytes | None = None
    if os.path.lexists(journal_path) and not journal_path.exists():
        raise RunJournalError(_bound(f"journal path is a dangling symlink: {journal_path.name}"))
    if not journal_path.exists():
        return last_sequence, last_digest, index, partial_tail
    raw = _read_bytes_nofollow(journal_path)
    segments = raw.split(b"\n")
    # A non-empty final segment after the last newline is a partial tail.
    if segments[-1]:
        partial_tail = segments[-1]
    for line in segments[:-1]:
        env = _parse_canonical_line(line)
        sequence = env["sequence"]
        if sequence != last_sequence + 1:
            raise ChainIntegrityError(_bound(f"journal sequence break: expected {last_sequence + 1}, got {sequence}"))
        if sequence > 1 and env["previous_digest"] != last_digest:
            raise ChainIntegrityError(_bound("journal previous_digest does not link to prior event_digest"))
        key = env["idempotency_key"]
        if key in index:
            raise ChainIntegrityError(_bound(f"journal repeats idempotency key {key!r}"))
        index[key] = env
        last_sequence = sequence
        last_digest = env["event_digest"]
    return last_sequence, last_digest, index, partial_tail


def _envelope_to_event(env: dict[str, Any]) -> RunEvent:
    """Materialize a validated envelope dict.

    Callers validate envelopes first; malformed fields here raise a bounded
    typed error instead of KeyError (defense in depth).
    """
    try:
        return RunEvent(
            schema=env["schema"],
            schema_version=env["schema_version"],
            event_id=env["event_id"],
            run_id=env["run_id"],
            sequence=env["sequence"],
            event_type=env["event_type"],
            recorded_at=env["recorded_at"],
            idempotency_key=env["idempotency_key"],
            request_digest=env["request_digest"],
            previous_digest=env["previous_digest"],
            event_digest=env["event_digest"],
            payload=dict(env["payload"]),
        )
    except (KeyError, TypeError) as exc:
        raise ChainIntegrityError(_bound(f"envelope fields are malformed: {exc}")) from exc


def append_event(
    journal_path: Path,
    *,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    expected_previous_sequence: int,
    recorded_at: str | None = None,
) -> RunEvent:
    """Append one event to the journal under the slice-1 contract.

    Idempotency: same key + same request digest returns the existing event with
    no write; same key + different digest raises IdempotencyConflict with no
    write. Concurrency: ``expected_previous_sequence`` must equal the current
    tail sequence (0 for an empty journal) else StaleSequenceError, no write.
    The write is a single bounded ``os.write`` to an ``O_APPEND`` descriptor
    with returned-byte-count verification and ``fsync`` before return.
    """
    journal_path = Path(journal_path)
    ensure_journal(journal_path)

    if recorded_at is None:
        from datetime import datetime, timezone

        recorded_at = run_events.format_recorded_at(datetime.now(timezone.utc))

    rd = run_events.request_digest(event_type=event_type, payload=payload, idempotency_key=idempotency_key)

    last_sequence, last_digest, index, partial_tail = _read_tail_state(journal_path)

    if partial_tail is not None:
        raise PartialTailError(_bound("journal ends in a partial line; run recover_partial_tail before appending"))

    existing = index.get(idempotency_key)
    if existing is not None:
        existing_rd = existing.get("request_digest")
        if not isinstance(existing_rd, str):
            raise ChainIntegrityError(_bound("indexed journal event is missing request_digest"))
        if existing_rd == rd:
            return _envelope_to_event(existing)
        existing_event_id = existing.get("event_id")
        if not isinstance(existing_event_id, str):
            raise ChainIntegrityError(_bound("indexed journal event is missing event_id"))
        raise IdempotencyConflict(
            _bound(f"idempotency key {idempotency_key!r} conflict"),
            existing_event_id=existing_event_id,
            request_digest=rd,
            existing_request_digest=existing_rd,
        )

    if expected_previous_sequence != last_sequence:
        raise StaleSequenceError(
            _bound(f"stale sequence: expected previous {expected_previous_sequence}, actual {last_sequence}")
        )

    sequence = last_sequence + 1
    envelope = run_events.build_event(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        recorded_at=recorded_at,
        previous_digest=last_digest,
        request_digest_value=rd,
    )

    line = canonical_bytes(envelope) + b"\n"
    if len(line) > run_events.MAX_LINE_BYTES:
        raise CanonicalizationError(_bound(f"canonical line exceeds {run_events.MAX_LINE_BYTES} bytes"))

    fd = _open_nofollow(journal_path, _OPEN_FLAGS, _FILE_MODE)
    try:
        _chmod_fd_or_path(fd, journal_path, _FILE_MODE)
        written = os.write(fd, line)
        if written != len(line):
            raise PartialWriteError(_bound(f"partial write: wrote {written} of {len(line)} bytes"))
        os.fsync(fd)
    finally:
        os.close(fd)

    return _envelope_to_event(envelope)


def _iter_lines(raw: bytes) -> Iterator[tuple[bytes | None, bytes | None]]:
    """Yield (complete_line_without_newline, None) for each full line and a final
    (None, partial_tail) if a non-empty trailing segment lacks a newline."""
    segments = raw.split(b"\n")
    if len(segments) <= 1:
        if raw:
            yield None, raw
        return
    for seg in segments[:-1]:
        yield seg, None
    tail = segments[-1]
    if tail:
        yield None, tail


def read_journal(journal_path: Path) -> JournalReport:
    """Read the journal without mutating it.

    Each complete line must be a validated canonical envelope; lines that are
    malformed, carry duplicate JSON keys, or differ byte-wise from canonical
    form are reported as bounded chain_errors. Verified-prefix semantics
    apply: after the first invalid complete line, sequence mismatch, or
    previous-digest mismatch, only the bounded first error is reported and
    no later events are returned. A non-empty trailing segment without a
    terminating newline is reported as ``partial_tail`` verbatim. The file
    is never written.
    """
    journal_path = Path(journal_path)
    report = JournalReport()
    if os.path.lexists(journal_path) and not journal_path.exists():
        raise RunJournalError(_bound(f"journal path is a dangling symlink: {journal_path.name}"))
    if not journal_path.exists():
        return report
    raw = _read_bytes_nofollow(journal_path)

    expected_sequence = 1
    expected_previous: str | None = None
    for complete, partial in _iter_lines(raw):
        if partial is not None:
            report.partial_tail = partial
            continue
        if complete is None:
            continue
        try:
            env = _parse_canonical_line(complete)
        except RunJournalError as exc:
            report.chain_errors.append(exc.diagnostic)
            break
        event = _envelope_to_event(env)
        if event.sequence != expected_sequence:
            report.chain_errors.append(
                _bound(f"sequence gap/duplicate: expected {expected_sequence}, got {event.sequence}")
            )
            break
        if event.sequence == 1:
            if event.previous_digest is not None:
                report.chain_errors.append("sequence 1 previous_digest must be null")
                break
        elif event.previous_digest != expected_previous:
            report.chain_errors.append(_bound("previous_digest does not link to prior event_digest"))
            break
        report.events.append(event)
        expected_sequence = event.sequence + 1
        expected_previous = event.event_digest
    return report


def read_journal_bounded(journal_path: Path) -> JournalReport:
    """Read the journal with byte and event-count bounds (issue #568 slice 5).

    Mirrors ``read_journal`` semantics but refuses journals above
    ``MAX_JOURNAL_BYTES`` (8 MiB) via ``fstat`` before any whole-file
    allocation, reads in bounded chunks, and fails closed at the first
    complete event whose sequence exceeds ``MAX_JOURNAL_EVENTS`` (512).
    A bound excess is reported as ``bound exceeded`` and not parsed further.
    The existing ``read_journal`` stays compatible.
    """
    from brigade.run_checkpoint import MAX_JOURNAL_BYTES, MAX_JOURNAL_EVENTS

    journal_path = Path(journal_path)
    report = JournalReport()
    if os.path.lexists(journal_path) and not journal_path.exists():
        raise RunJournalError(_bound(f"journal path is a dangling symlink: {journal_path.name}"))
    if not journal_path.exists():
        return report

    fd = _open_nofollow(journal_path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if info.st_size > MAX_JOURNAL_BYTES:
            raise RunJournalError(_bound("bound exceeded: journal above MAX_JOURNAL_BYTES"))
        if not stat.S_ISREG(info.st_mode):
            raise RunJournalError(_bound(f"journal path is not a regular file: {journal_path.name}"))

        # Bounded chunk reads: never allocate the whole file at once.
        raw = bytearray()
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_JOURNAL_BYTES:
                raise RunJournalError(_bound("bound exceeded: journal above MAX_JOURNAL_BYTES"))
        raw_bytes = bytes(raw)
    finally:
        os.close(fd)

    expected_sequence = 1
    expected_previous: str | None = None
    complete_count = 0
    for complete, partial in _iter_lines(raw_bytes):
        if partial is not None:
            report.partial_tail = partial
            continue
        if complete is None:
            continue
        complete_count += 1
        if complete_count > MAX_JOURNAL_EVENTS:
            raise RunJournalError(_bound("bound exceeded: journal complete records above MAX_JOURNAL_EVENTS"))
        try:
            env = _parse_canonical_line(complete)
        except RunJournalError as exc:
            report.chain_errors.append(exc.diagnostic)
            break
        event = _envelope_to_event(env)
        if event.sequence > MAX_JOURNAL_EVENTS:
            raise RunJournalError(_bound("bound exceeded: journal event sequence above MAX_JOURNAL_EVENTS"))
        if event.sequence != expected_sequence:
            report.chain_errors.append(
                _bound(f"sequence gap/duplicate: expected {expected_sequence}, got {event.sequence}")
            )
            break
        if event.sequence == 1:
            if event.previous_digest is not None:
                report.chain_errors.append("sequence 1 previous_digest must be null")
                break
        elif event.previous_digest != expected_previous:
            report.chain_errors.append(_bound("previous_digest does not link to prior event_digest"))
            break
        report.events.append(event)
        expected_sequence = event.sequence + 1
        expected_previous = event.event_digest
    return report


def recover_partial_tail(journal_path: Path, quarantine_dir: Path) -> RecoveryReport:
    """Recovery-only: quarantine the partial suffix verbatim, then truncate.

    The incomplete trailing bytes are captured exactly once under
    ``quarantine_dir`` (0o700) in a write-once (O_EXCL) 0o600 file named with
    the complete-line count, the full SHA-256 of the partial bytes, and an
    exclusive numeric suffix on collision, then the journal is truncated to
    the last complete line (fsynced). With no partial tail nothing is written
    and ``quarantine_path`` is None. Normal readers must never call this; it
    is the only API that mutates the journal body.
    """
    journal_path = Path(journal_path)
    quarantine_dir = Path(quarantine_dir)
    if os.path.lexists(journal_path) and not journal_path.exists():
        raise RunJournalError(_bound(f"journal path is a dangling symlink: {journal_path.name}"))
    if not journal_path.exists():
        raise RunJournalError(_bound(f"journal path does not exist: {journal_path.name}"))
    _mkdir_private(quarantine_dir)

    raw = _read_bytes_nofollow(journal_path)
    last_newline = raw.rfind(b"\n")
    if last_newline == -1:
        partial = raw
        complete = b""
    else:
        partial = raw[last_newline + 1 :]
        complete = raw[: last_newline + 1]

    if not partial:
        return RecoveryReport(partial_bytes=b"", quarantine_path=None)

    digest = hashlib.sha256(partial).hexdigest()
    context = complete.count(b"\n")
    stem = f"lifecycle-partial-{context:06d}-{digest}"
    quarantine_path: Path | None = None
    for attempt in range(_MAX_QUARANTINE_ATTEMPTS):
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = quarantine_dir / f"{stem}{suffix}.bin"
        try:
            qfd = _open_nofollow(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        except FileExistsError:
            continue
        try:
            _chmod_fd_or_path(qfd, candidate, _FILE_MODE)
            written = os.write(qfd, partial)
            if written != len(partial):
                raise PartialWriteError(_bound("quarantine write was partial"))
            os.fsync(qfd)
        finally:
            os.close(qfd)
        quarantine_path = candidate
        break
    if quarantine_path is None:
        raise RunJournalError(_bound("no collision-free quarantine name available"))

    jfd = _open_nofollow(journal_path, os.O_RDWR)
    try:
        _chmod_fd_or_path(jfd, journal_path, _FILE_MODE)
        os.ftruncate(jfd, len(complete))
        os.fsync(jfd)
    finally:
        os.close(jfd)

    return RecoveryReport(partial_bytes=partial, quarantine_path=quarantine_path)


def _bound(msg: str) -> str:
    limit = run_events.MAX_DIAGNOSTIC_LEN
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"
