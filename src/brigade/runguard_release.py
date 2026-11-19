"""Release the run lock, including against a Windows reader that holds it open.

``runguard`` releases a lock by renaming its directory away and then deleting it.
That is atomic and correct on POSIX. On Windows, NTFS refuses the rename while
any handle to a file inside the directory is open, and ``FILE_SHARE_DELETE`` on
the reader's handle does not lift that. Waiters poll ``pid`` and ``owner.json``
inside the lock directory, so a contended release spends its whole retry budget
losing to them, raises ``PermissionError``, and leaves a live owner's lock
published (issue #2747).

So on Windows the release removes those two names first with POSIX-semantics
delete (see :func:`brigade.nt_process.delete_file_sharing_readers`), which
unlinks a name a reader holds open, and only then renames the now-empty
directory. If the volume cannot do that, the release falls back to rename-only
retries, the behaviour every platform had before. A release that gives up puts
back the metadata it removed, so a lock that is still published still describes
its live owner.

Removing the names empties the directory for the instant before the rename, and
an empty lock directory reads as stale to every waiter. So the whole Windows
attempt runs under the stale-recovery gate (``runguard_gate``): no recoverer can
rename that empty directory away or publish a new lock at ``path`` while the
release holds the gate, which is what makes removing and restoring the metadata
safe. The gate is released before the release sleeps and retries.

Split out of ``runguard`` to keep that module under the size ceiling.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from . import nt_process
from .runguard_gate import _recovery_gate_busy_error, _stale_recovery_gate

if TYPE_CHECKING:
    from .runguard import _LockOwnership

LOCK_RELEASE_RETRY_SECONDS = 1.0
LOCK_RELEASE_BACKOFF_SECONDS = 0.001
LOCK_RELEASE_BACKOFF_CAP_SECONDS = 0.05
_SHARING_DENIED = {5, 32}
_LOCK_METADATA_NAMES = ("pid", "owner.json")
# Only Windows has to move the metadata names out of the way first; POSIX renames
# a directory with readers inside it, and that release is unchanged.
_WINDOWS = os.name == "nt"


class _Clock(Protocol):
    """The retry clock, injected so tests drive the budget instead of the wall."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class _GateBusy(Exception):
    """Another process holds the stale-recovery gate, so this attempt did nothing."""


@dataclass(frozen=True)
class _Metadata:
    """Metadata bytes a Windows release removed, to put back if the release gives up."""

    files: tuple[tuple[Path, bytes], ...]


def release_lock(path: Path, ownership: _LockOwnership, *, clock: _Clock) -> None:
    """Remove the lock at ``path`` while it is still ``ownership``'s.

    Retries a sharing denial and a busy recovery gate with a short backoff inside
    ``LOCK_RELEASE_RETRY_SECONDS``. A denial that outlasts the budget raises
    ``PermissionError`` with the lock left as it was found.
    """
    release_path = path.with_name(f".{path.name}.{ownership.owner_token}.release")
    deadline = clock.monotonic() + LOCK_RELEASE_RETRY_SECONDS
    backoff = LOCK_RELEASE_BACKOFF_SECONDS
    while True:
        denial: PermissionError | None = None
        try:
            _release_once(path, release_path, ownership)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _SHARING_DENIED:
                raise
            denial = error
        except _GateBusy:
            pass
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            if denial is not None:
                raise denial
            raise _recovery_gate_busy_error(path)
        clock.sleep(min(backoff, remaining))
        backoff = min(backoff * 2, LOCK_RELEASE_BACKOFF_CAP_SECONDS)


def _release_once(path: Path, release_path: Path, ownership: _LockOwnership) -> None:
    """One release attempt, under the stale-recovery gate on Windows."""
    if not _WINDOWS:
        _release_posix_lock(path, release_path, ownership)
        return
    try:
        path.stat()
    except OSError:
        # The lock or its workspace is gone; do not make the gate file for it.
        return
    with _stale_recovery_gate(path) as gated:
        if not gated:
            raise _GateBusy
        # Judge the lock again inside the gate, on the identity and the owner
        # token: a holder that lost `path` must not unlink the metadata of the
        # lock that replaced it, and a released inode is reused at once.
        if not _still_our_lock(path, ownership):
            return
        withheld = _withhold_metadata(path)
        _rename_and_remove(path, release_path, ownership, withheld=withheld)


def _release_posix_lock(path: Path, release_path: Path, ownership: _LockOwnership) -> None:
    """The release every platform used before issue #2747, byte for byte."""
    try:
        visible = path.stat()
    except OSError:
        return
    if visible.st_dev != ownership.device or visible.st_ino != ownership.inode:
        return
    _rename_and_remove(path, release_path, ownership, withheld=None)


def _still_our_lock(path: Path, ownership: _LockOwnership) -> bool:
    """Whether ``path`` is still the lock this lease published.

    The published directory's device and inode are the release identity, and
    that identity alone releases a lock whose owner metadata is lost or
    corrupt, a contract the release has always had. When the metadata is
    readable, its owner token must also match: NTFS hands a released
    directory's inode straight to the next lock, so the token is the only thing
    that tells a replacement owner apart from ours.
    """
    try:
        visible = path.stat()
    except OSError:
        return False
    if visible.st_dev != ownership.device or visible.st_ino != ownership.inode:
        return False
    try:
        payload = json.loads(nt_process.read_text_sharing_delete(path / "owner.json"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return True
    if not isinstance(payload, dict):
        return True
    token = payload.get("owner_token")
    return not isinstance(token, str) or token == ownership.owner_token


def _withhold_metadata(path: Path) -> _Metadata | None:
    """Delete the names a waiter may hold open, so the directory can leave ``path``.

    ``None`` means this Windows build or filesystem cannot unlink a name a reader
    holds open. Nothing was removed then, and the caller retries the rename.
    """
    deleted: list[tuple[Path, bytes]] = []
    for target, payload in _metadata_bytes(path):
        try:
            if not nt_process.delete_file_sharing_readers(target):
                _put_back(deleted)
                return None
        except FileNotFoundError:
            continue
        except OSError:
            _put_back(deleted)
            raise
        deleted.append((target, payload))
    return _Metadata(files=tuple(deleted))


def _metadata_bytes(path: Path) -> list[tuple[Path, bytes]]:
    """The lock metadata as it is published, or nothing when any of it is unreadable.

    The read shares delete access for the same reason every other reader does: a
    plain open would refuse the unlink this release is about to attempt. A name
    that is already gone is skipped; one that cannot be read could not be
    restored, so nothing may be removed.
    """
    captured: list[tuple[Path, bytes]] = []
    for name in _LOCK_METADATA_NAMES:
        target = path / name
        try:
            captured.append((target, nt_process.read_bytes_sharing_delete(target)))
        except FileNotFoundError:
            continue
        except OSError:
            return []
    return captured


def _rename_and_remove(
    path: Path, release_path: Path, ownership: _LockOwnership, *, withheld: _Metadata | None
) -> None:
    try:
        path.rename(release_path)
    except FileNotFoundError:
        return
    except BaseException:
        _restore_metadata(path, ownership, withheld)
        raise
    _remove_released(path, release_path, ownership)


def _put_back(files: Iterable[tuple[Path, bytes]]) -> None:
    for target, payload in files:
        try:
            target.write_bytes(payload)
        except OSError:
            # The release is already failing; a best-effort restore is all it owes.
            pass


def _restore_metadata(path: Path, ownership: _LockOwnership, withheld: _Metadata | None) -> None:
    """Put withheld metadata back, if ``path`` still holds this lease's directory."""
    if withheld is None or not withheld.files:
        return
    try:
        visible = path.stat()
    except OSError:
        return
    if visible.st_dev != ownership.device or visible.st_ino != ownership.inode:
        return
    _put_back(withheld.files)


def _remove_released(path: Path, release_path: Path, ownership: _LockOwnership) -> None:
    """Delete the renamed lock, or put it back when it was not this lease's directory."""
    try:
        released = release_path.stat()
    except OSError:
        return
    if released.st_dev == ownership.device and released.st_ino == ownership.inode:
        shutil.rmtree(release_path, ignore_errors=True)
        return
    if not path.exists():
        release_path.rename(path)
