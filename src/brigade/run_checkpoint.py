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

from brigade import run_events, run_journal
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


def validate_checkpoint(run_dir: Path, event: Any) -> bytes:
    """Validate a checkpoint payload, then open and verify the referenced file.

    Accepts either a payload Mapping or a ``run_journal.RunEvent`` whose
    ``payload`` is the checkpoint payload. Returns the verified checkpoint
    bytes. Raises ``CheckpointError`` on any validation failure.
    """
    run_dir = Path(run_dir)
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
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CheckpointError(_bound("checkpoint path is not a regular file"), category="not-regular")
        if info.st_nlink != 1:
            raise CheckpointError(_bound("checkpoint link count is not one"), category="link-count")
        if info.st_size != byte_size:
            raise CheckpointError(_bound("checkpoint size mismatch"), category="size-mismatch")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, run_journal._READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
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
        if not isinstance(obj, dict):
            raise CheckpointError(_bound("checkpoint is not a JSON object"), category="json-object")
        if _writer_canonical_bytes(obj) != data:
            raise CheckpointError(_bound("checkpoint bytes differ from writer canonical form"), category="writer-bytes")
        return data
    finally:
        os.close(fd)


def _verify_collision(final_path: Path, expected_bytes: bytes, expected_sha: str) -> None:
    """Open an existing final path no-follow and require byte+digest equality.

    Raises ``CheckpointError`` with category ``collision-unsafe`` for a
    symlink, non-regular inode, or link count above one, or
    ``collision-mismatch`` for byte or digest inequality.
    """
    try:
        fd = run_journal._open_nofollow(final_path, os.O_RDONLY)
    except run_journal.RunJournalError as exc:
        raise CheckpointError(_bound(f"collision-unsafe: {exc.diagnostic}"), category="collision-unsafe") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CheckpointError(_bound("collision-unsafe: not a regular file"), category="collision-unsafe")
        if info.st_nlink != 1:
            raise CheckpointError(_bound("collision-unsafe: link count above one"), category="collision-unsafe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, run_journal._READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        if data != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha:
            raise CheckpointError(_bound("collision-mismatch: bytes differ"), category="collision-mismatch")
    finally:
        os.close(fd)


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def _fsync_directory(path: Path) -> None:
    if not _supports_directory_fsync():
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_checkpoint_file(run_dir: Path, run_json_bytes: bytes) -> Path:
    """Crash-safe publish of ``run_json_bytes`` to the content-addressed checkpoint path.

    Creates the 0o700 ``recovery-checkpoints`` directory (no-follow mkdir),
    writes to a 0o600 same-directory temp, fsyncs, publishes by atomic
    no-replace ``os.link``, applies POSIX-guarded directory fsync, and unlinks
    the temp. On ``EEXIST`` the existing final file is opened through the same
    no-follow regular single-link fd hardening and required to be byte and
    digest equal (a safe matching collision is a no-op); an unsafe inode or
    mismatched collision raises ``CheckpointError``.
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
    run_journal._mkdir_private(cp_dir)
    final_path = checkpoint_path(run_dir, sha)

    fd, tmp_name = tempfile.mkstemp(dir=cp_dir, prefix=".checkpoint.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, run_journal._FILE_MODE)
        written = os.write(fd, run_json_bytes)
        if written != len(run_json_bytes):
            raise CheckpointError(_bound("checkpoint temp write was partial"), category="io")
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp_path, final_path)
    except FileExistsError:
        _verify_collision(final_path, run_json_bytes, sha)
        tmp_path.unlink(missing_ok=True)
        return final_path
    else:
        _fsync_directory(cp_dir)
        tmp_path.unlink(missing_ok=True)
        return final_path
