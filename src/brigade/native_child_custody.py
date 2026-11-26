"""Owner-only run custody for governed native children.

Custody is descriptor-bound. The run directory is bound before any asset read,
each asset is created with exclusive no-follow creation in owner-only mode, and
publish replay plus readback require a single-link regular file with mode 0600.
Windows binds a protected owner-only DACL at creation through
``work_cmd.nt_private`` and verifies it on the held handle, because NTFS has no
POSIX mode bits. Statements about administrators, backup and restore rights stay
outside this discretionary access boundary.

This module is host neutral. A host adapter supplies the asset names and bytes
and decides which refusals it surfaces under its own platform gate.
"""

from __future__ import annotations

import ctypes
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import dirfd, run_dirfd, run_journal, runguard
from . import run_task_journal as journal
from .native_child_host import NativeChildCustodyError as EnrollmentError


@contextmanager
def owned_run(workspace: Path, run_dir: Path, *, wait_seconds: float = 0) -> Iterator[None]:
    """Hold the workspace run lock for exactly one lifecycle transaction.

    Ownership is per invocation. No token is persisted and no long-lived owner
    process is imitated. Between verbs the run has no live lock holder, which
    is the normal externally controlled state rather than a stale run.
    """
    try:
        with runguard.run_lock(workspace, run_dir=run_dir, wait_seconds=wait_seconds, stale_action="claim"):
            if not runguard.is_active_run_owner(workspace, run_dir):
                raise EnrollmentError("run-owner", "run ownership was not established")
            yield
    except runguard.RunLockError as exc:
        raise EnrollmentError("run-busy", "another lifecycle holds the workspace run lock") from exc
    except runguard.RunGuardError as exc:
        raise EnrollmentError("run-lock", "the workspace run lock is unavailable") from exc


def nt_private(os_name: str | None = None) -> Any | None:
    """The Windows owner-only helper when this platform can actually bind it.

    ``os_name`` defaults to the running platform. A host adapter passes its own
    reading so a test can stand in for another platform.
    """
    if (os.name if os_name is None else os_name) != "nt" or not dirfd.nt_available():
        return None
    try:
        from .work_cmd import nt_private

        # Bind both lazy API namespaces now, so a missing DLL or symbol refuses
        # before any lock or run state is written.
        nt_private._security_api()
        nt_private.nt._require_api()
    except (ImportError, OSError, AttributeError):
        return None
    return nt_private


def _windows_private_helper() -> Any:
    helper = nt_private()
    if helper is None:
        raise EnrollmentError(
            "unsupported-platform",
            "native enrollment needs owner-only file custody that this platform does not provide",
        )
    return helper


def _create_windows_run_dir(run_dir: Path) -> None:
    """Create or reopen the run directory under a protected owner-only DACL."""
    helper = _windows_private_helper()
    try:
        parent = dirfd.open_directory_nofollow(run_dir.parent)
    except OSError:
        raise EnrollmentError("run-dir", "the run directory is not bindable") from None
    try:
        try:
            descriptor = helper.open_child_directory(parent, run_dir.name, create=True)
        except FileExistsError:
            # An existing object is inspected, never repaired. Verification is
            # part of the reopen, so an inherited or widened DACL refuses here.
            try:
                descriptor = helper.open_child_directory(parent, run_dir.name)
            except OSError:
                raise EnrollmentError("run-dir", "the run directory custody is not owner-only") from None
        except OSError:
            raise EnrollmentError("run-dir", "the run directory could not be created owner-only") from None
        try:
            try:
                helper.verify_fd(descriptor, directory=True)
            except OSError:
                raise EnrollmentError("run-dir", "the run directory custody is not owner-only") from None
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise EnrollmentError("run-dir", "the run directory is not a directory")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    run_journal._fsync_directory(run_dir.parent)


def create_run_dir(run_dir: Path) -> None:
    """Create the run directory owner-only, refusing a symlink at the name."""
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _create_windows_run_dir(run_dir)
        return
    run_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        descriptor = dirfd.open_directory_nofollow(run_dir)
    except OSError:
        raise EnrollmentError("run-dir", "the run directory is not bindable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise EnrollmentError("run-dir", "the run directory is not a directory")
        try:
            os.fchmod(descriptor, 0o700)
        except (AttributeError, OSError):
            # Windows has no POSIX mode bits. Creation mode stays owner-only.
            pass
    finally:
        os.close(descriptor)
    run_journal._fsync_directory(run_dir.parent)


def supported_platform(os_name: str | None = None) -> bool:
    """Owner-only custody needs POSIX modes or the Windows NT private helper."""
    return not ((os.name if os_name is None else os_name) == "nt" and nt_private(os_name) is None)


def _private_asset(info: os.stat_result) -> bool:
    """Owner-only single-link regular file: the custody shape we can trust."""
    if os.name == "nt":
        # NTFS has no POSIX mode bits. The owner-only DACL is verified on the
        # held handle; this shape check still refuses links and non-files.
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600


def _require_private_asset(info: os.stat_result) -> None:
    if not _private_asset(info):
        raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file")


def _discard_held_asset(descriptor: int, name: str) -> None:
    """Mark the opened asset for deletion without re-resolving its name.

    The handle carries DELETE, so the mark applies to the exact object that was
    verified. A swapped directory entry cannot redirect it. When the mark fails,
    the empty object stays for inspection, which is the fail-closed alternative
    to an identity-free unlink.
    """
    try:
        from .work_cmd import nt_dirfd

        api = nt_dirfd._require_api()
        disposition = api.FILE_DISPOSITION_INFORMATION()
        disposition.DeleteFile = True
        nt_dirfd._nt_set_info(
            api,
            nt_dirfd._handle_from_fd(descriptor),
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
            nt_dirfd._FileDispositionInformation,
            name=name,
        )
    except (OSError, AttributeError):
        return


def _create_windows_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> None:
    """Create one asset, verify its DACL, then write and flush the bytes."""
    helper = _windows_private_helper()
    try:
        descriptor = helper.open_file(bound.dir_fd(), name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        raise EnrollmentError("asset-conflict", "delegation input asset already exists") from None
    except PermissionError:
        # open_file verified the new object, refused it and closed its handle.
        # Without a held identity the object is left for inspection, not unlinked.
        raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
    except OSError:
        raise EnrollmentError("asset-write", "delegation input asset could not be created") from None
    complete = False
    try:
        try:
            helper.verify_fd(descriptor, directory=False)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        if not _private_asset(os.fstat(descriptor)):
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established")
        try:
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EnrollmentError("asset-write", "delegation input asset could not be written") from None
        complete = True
    finally:
        if not complete:
            _discard_held_asset(descriptor, name)
        os.close(descriptor)
    dirfd.fsync_directory(bound.dir_fd())


def _create_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> None:
    """Publish one asset with exclusive no-follow creation and owner-only custody."""
    if os.name == "nt":
        _create_windows_asset(bound, name, content)
        return
    try:
        descriptor = bound.open_file((), name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise EnrollmentError("asset-conflict", "delegation input asset already exists") from None
    except OSError:
        raise EnrollmentError("asset-write", "delegation input asset could not be created") from None
    info: os.stat_result | None = None
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        try:
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EnrollmentError("asset-write", "delegation input asset could not be written") from None
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if info is None or not _private_asset(info):
        try:
            dirfd.unlink_child(bound.dir_fd(), name)
        except OSError:
            pass
        raise EnrollmentError("asset-custody", "delegation input asset custody was not established")
    dirfd.fsync_directory(bound.dir_fd())


def write_asset(run_dir: Path, name: str, content: bytes) -> None:
    """Publish one owner-only asset atomically, refusing to replace a file."""
    if not journal.native_asset_path(name):
        raise EnrollmentError("asset-name", "delegation input asset name is invalid")
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            _create_asset(bound, name, content)
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during asset publish")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None


def _read_windows_asset(bound: run_dirfd.BoundRunDir, name: str, *, missing_ok: bool) -> bytes | None:
    """Open the asset once, verify custody on that handle, then read it."""
    helper = _windows_private_helper()
    try:
        descriptor = helper.open_file(bound.dir_fd(), name, os.O_RDONLY)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EnrollmentError("asset-missing", "retained delegation input is missing") from None
    except OSError:
        raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file") from None
    try:
        try:
            info = os.fstat(descriptor)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        if not _private_asset(info):
            raise EnrollmentError("asset-custody", "delegation input asset is not a private regular file")
        try:
            helper.verify_fd(descriptor, directory=False)
        except OSError:
            raise EnrollmentError("asset-custody", "delegation input asset custody was not established") from None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > run_dirfd.MAX_READ_BYTES:
                raise EnrollmentError("asset-custody", "delegation input asset exceeds the read limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _existing_asset(bound: run_dirfd.BoundRunDir, name: str, content: bytes) -> bool:
    """True when an already published asset matches; refuses unsafe custody."""
    if os.name == "nt":
        existing = _read_windows_asset(bound, name, missing_ok=True)
        if existing is None:
            return False
        if existing != content:
            raise EnrollmentError("asset-conflict", "retained delegation input already differs")
        return True
    try:
        info = bound.stat((), name)
    except FileNotFoundError:
        return False
    _require_private_asset(info)
    existing = bound.read_bytes(name)
    if existing is None:
        raise EnrollmentError("asset-missing", "retained delegation input disappeared")
    if existing != content:
        raise EnrollmentError("asset-conflict", "retained delegation input already differs")
    return True


def publish_assets(run_dir: Path, assets: Sequence[tuple[str, bytes]]) -> None:
    """Publish each named asset, accepting an identical replay and refusing a differing one."""
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            for name, content in assets:
                if not _existing_asset(bound, name, content):
                    _create_asset(bound, name, content)
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during enrollment")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None


def read_asset(run_dir: Path, name: str) -> bytes:
    """Read one retained asset back under the same custody checks it was written with."""
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            if os.name == "nt":
                read = _read_windows_asset(bound, name, missing_ok=False)
                if read is None:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing")
                raw = read
            else:
                try:
                    info = bound.stat((), name)
                except FileNotFoundError:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing") from None
                _require_private_asset(info)
                existing = bound.read_bytes(name)
                if existing is None:
                    raise EnrollmentError("asset-missing", "retained delegation input is missing")
                raw = existing
            if not bound.still_bound():
                raise EnrollmentError("asset-custody", "run directory changed during readback")
    except OSError:
        raise EnrollmentError("asset-custody", "run directory could not be bound") from None
    return raw
