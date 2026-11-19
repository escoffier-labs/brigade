"""Release a Windows run lock while a waiter holds its metadata open (issue #2747).

NTFS refuses to rename a directory while any handle to a file inside it is open,
even a handle opened with ``FILE_SHARE_DELETE``, and waiters poll ``pid`` and
``owner.json`` inside the lock directory. A contended release therefore removes
those names with POSIX-semantics delete before the directory moves.

Most tests here drive the Windows branch from a fake kernel32. A fake handle is
not a real Windows handle, so those tests run off Windows only (`requires_fake_kernel32`);
on Windows the real kernel32 is never replaced. The tests without that marker
use the production readers and run everywhere, and the last one needs a real
NTFS volume and is skipped elsewhere. If a Windows run reports skips in this
file, every `requires_fake_kernel32` test and the POSIX test will be among them.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from brigade import nt_process, runguard, runguard_gate, runguard_release, run_lane_state

FILE_DISPOSITION_INFO_EX = 21
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_SHARING_VIOLATION = 32
_SHARE_DELETE = nt_process.FILE_SHARE_READ | nt_process.FILE_SHARE_WRITE | nt_process.FILE_SHARE_DELETE

requires_fake_kernel32 = pytest.mark.skipif(
    os.name == "nt",
    reason="a fake kernel32 cannot hand real handles to a real Windows release",
)


class _Entry:
    """Win32 callable that also accepts the ``argtypes``/``restype`` assignments."""

    def __init__(self, func: Any) -> None:
        self._func = func
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self._func(*args)


class FakeKernel32:
    """Kernel32 stand-in that unlinks the name it is asked to dispose of."""

    def __init__(self, *, posix_delete: bool = True, deny_opens: int = 0) -> None:
        self.posix_delete = posix_delete
        # Calls to CreateFileW that answer ERROR_ACCESS_DENIED before it works, the
        # way a name whose delete is pending behaves.
        self.deny_opens = deny_opens
        self.next_handle = 0x100
        self.paths: dict[int, Path] = {}
        self.create_calls: list[tuple[Any, ...]] = []
        self.disposition_calls: list[tuple[int, int, int, int]] = []
        self.close_calls: list[int] = []
        self.CreateFileW = _Entry(self._create_file)  # noqa: N815 - Win32 name
        self.SetFileInformationByHandle = _Entry(self._set_information)  # noqa: N815 - Win32 name
        self.CloseHandle = _Entry(self._close_handle)  # noqa: N815 - Win32 name

    def _create_file(self, name: str, access: int, share: int, *rest: Any) -> int:
        self.create_calls.append((name, access, share, *rest))
        if self.deny_opens > 0:
            self.deny_opens -= 1
            return 0
        handle = self.next_handle
        self.next_handle += 1
        self.paths[handle] = Path(name[len("\\\\?\\") :] if name.startswith("\\\\?\\") else name)
        return handle

    def _set_information(self, handle: int, information_class: int, info: Any, size: int) -> int:
        disposition = info._obj
        self.disposition_calls.append((handle, information_class, disposition.Flags, size))
        if not self.posix_delete:
            return 0
        os.unlink(self.paths[handle])
        return 1

    def _close_handle(self, handle: int) -> int:
        self.close_calls.append(handle)
        return 1


class _FakeClock:
    """Retry clock that advances only when the release sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fake_kernel32(monkeypatch: pytest.MonkeyPatch, fake: FakeKernel32) -> None:
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)


def _windows_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runguard_release, "_WINDOWS", True)


def _fake_kernel32_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Windows opens real handles; only another host needs the stand-in."""
    if os.name != "nt":
        _fake_kernel32(monkeypatch, FakeKernel32())


class _NtOs:
    """``os`` stand-in that reports nt without switching pathlib to Windows paths."""

    name = "nt"

    def __getattr__(self, item: str) -> Any:
        return getattr(os, item)


def _spy_rename(monkeypatch: pytest.MonkeyPatch, watched: Path) -> list[list[str]]:
    """Names still inside ``watched`` at every rename of it."""
    seen: list[list[str]] = []
    original = Path.rename

    def spy(self: Path, target: Path) -> Path:
        if self == watched:
            seen.append(sorted(entry.name for entry in self.iterdir()))
        return original(self, target)

    monkeypatch.setattr(Path, "rename", spy)
    return seen


def _deny_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(self: Path, target: Path) -> Path:
        error = PermissionError(13, "sharing violation")
        error.winerror = ERROR_SHARING_VIOLATION
        raise error

    monkeypatch.setattr(Path, "rename", denied)


def _release_with_fake_clock(
    monkeypatch: pytest.MonkeyPatch, lock_path: Path, ownership: Any, clock: _FakeClock
) -> None:
    """Run the release through ``runguard._release_lock`` with a clock the test drives."""
    monkeypatch.setattr(runguard.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runguard.time, "sleep", clock.sleep)
    runguard._release_lock(lock_path, ownership)


def _dead_owner_lock(lock_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Publish a lock, then make its recorded owner pid read as dead."""
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    owner = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
    owner["pid"] = 99999999
    (lock_path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (lock_path / "pid").write_text("99999999\n", encoding="utf-8")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: pid != 99999999)
    return ownership


@requires_fake_kernel32
def test_release_deletes_the_metadata_names_before_it_renames_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The names a waiter holds open are gone, so the directory rename cannot lose to a reader."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    fake = FakeKernel32()
    _fake_kernel32(monkeypatch, fake)
    _windows_release(monkeypatch)
    seen = _spy_rename(monkeypatch, lock_path)

    runguard._release_lock(lock_path, ownership)

    assert seen == [[]], "the release must unlink pid and owner.json before renaming the directory"
    assert [call[1] for call in fake.create_calls] == [nt_process.DELETE, nt_process.DELETE]
    assert [call[2] for call in fake.create_calls] == [_SHARE_DELETE, _SHARE_DELETE], (
        "a reader without FILE_SHARE_DELETE would keep the name occupied"
    )
    assert [call[4] for call in fake.create_calls] == [nt_process.OPEN_EXISTING, nt_process.OPEN_EXISTING]
    assert [call[5] for call in fake.create_calls] == [
        nt_process.FILE_FLAG_OPEN_REPARSE_POINT,
        nt_process.FILE_FLAG_OPEN_REPARSE_POINT,
    ]
    assert list(fake.paths.values()) == [lock_path / "pid", lock_path / "owner.json"]
    expected_flags = nt_process.FILE_DISPOSITION_FLAG_DELETE | nt_process.FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
    assert [call[1:] for call in fake.disposition_calls] == [
        (FILE_DISPOSITION_INFO_EX, expected_flags, ctypes.sizeof(nt_process._FileDispositionInfoEx))
    ] * 2
    assert fake.close_calls == list(fake.paths), "every opened handle is closed exactly once"
    assert not lock_path.exists()


@requires_fake_kernel32
def test_release_leaves_a_lock_it_no_longer_owns_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "run.lock"
    runguard._publish_lock(lock_path, run_dir=None)
    fake = FakeKernel32()
    _fake_kernel32(monkeypatch, fake)
    _windows_release(monkeypatch)

    runguard._release_lock(lock_path, runguard._LockOwnership("someone-else", 0, 0))

    assert fake.create_calls == []
    assert sorted(entry.name for entry in lock_path.iterdir()) == ["owner.json", "pid"]


@requires_fake_kernel32
def test_release_falls_back_to_the_rename_when_posix_delete_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older Windows and non-NTFS volumes keep the pre-existing rename-only release."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    fake = FakeKernel32(posix_delete=False)
    _fake_kernel32(monkeypatch, fake)
    _windows_release(monkeypatch)
    monkeypatch.setattr(nt_process, "_last_error", lambda: ERROR_INVALID_PARAMETER)
    seen = _spy_rename(monkeypatch, lock_path)

    runguard._release_lock(lock_path, ownership)

    assert seen == [["owner.json", "pid"]], "the fallback must not have removed any metadata"
    assert len(fake.create_calls) == 1, "one unsupported answer is enough to stop deleting"
    assert fake.close_calls == [0x100]
    assert not lock_path.exists()


@requires_fake_kernel32
def test_delete_closes_the_handle_and_reports_a_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "owner.json"
    target.write_text("{}", encoding="utf-8")
    fake = FakeKernel32(posix_delete=False)
    _fake_kernel32(monkeypatch, fake)
    monkeypatch.setattr(nt_process, "_last_error", lambda: ERROR_SHARING_VIOLATION)

    with pytest.raises(PermissionError) as raised:
        nt_process.delete_file_sharing_readers(target)

    assert raised.value.winerror == ERROR_SHARING_VIOLATION
    assert fake.close_calls == [0x100]
    assert target.exists()


@requires_fake_kernel32
def test_release_restores_the_metadata_it_removed_when_it_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that is still published must still describe its live owner."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    published = {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")}
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    _deny_rename(monkeypatch)
    clock = _FakeClock()

    with pytest.raises(PermissionError):
        _release_with_fake_clock(monkeypatch, lock_path, ownership, clock)

    assert len(clock.sleeps) > 1, "a persistent denial must be retried, then reported"
    assert {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")} == published
    assert clock.now <= 1.0, "the release budget must not grow when POSIX delete is available"


@requires_fake_kernel32
def test_release_backoff_starts_near_a_millisecond_and_grows_to_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release retry that sleeps 25 ms at first spends the budget on a handful of attempts."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    _deny_rename(monkeypatch)
    clock = _FakeClock()

    with pytest.raises(PermissionError):
        _release_with_fake_clock(monkeypatch, lock_path, ownership, clock)

    assert clock.sleeps[0] == pytest.approx(0.001)
    assert clock.sleeps[0] < 0.025, "the first retry must not wait a fixed 25 ms"
    assert clock.sleeps[:-1] == sorted(clock.sleeps[:-1]), "the backoff must grow"
    assert len(clock.sleeps) > 3
    assert clock.now <= 1.0


@requires_fake_kernel32
def test_release_leaves_a_successor_published_during_a_retry_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry must not touch a lock another owner published while the release slept."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    fake = FakeKernel32()
    _fake_kernel32(monkeypatch, fake)
    _windows_release(monkeypatch)
    original_rename = Path.rename
    servant: list[object] = []
    attempts = 0

    def deny_once(self: Path, target: Path) -> Path:
        if self == lock_path and attempts == 0:
            error = PermissionError(13, "sharing violation")
            error.winerror = ERROR_SHARING_VIOLATION
            raise error
        return original_rename(self, target)

    def publish_successor(seconds: float) -> None:
        nonlocal attempts
        attempts += 1
        original_rename(lock_path, tmp_path / "old.lock")
        servant.append(runguard._publish_lock(lock_path, run_dir=None))

    clock = _FakeClock()
    clock.sleep = publish_successor  # type: ignore[method-assign]
    monkeypatch.setattr(Path, "rename", deny_once)

    _release_with_fake_clock(monkeypatch, lock_path, ownership, clock)

    assert attempts == 1
    assert fake.disposition_calls, "the first attempt did withhold the metadata"
    assert len(fake.disposition_calls) == 2, "the retry must not delete the successor's metadata"
    assert runguard._read_lock_owner(lock_path)["owner_token"] == servant[0].owner_token
    assert sorted(entry.name for entry in lock_path.iterdir()) == ["owner.json", "pid"]


@requires_fake_kernel32
def test_release_does_not_retry_a_permission_error_the_platform_did_not_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a Windows sharing denial is worth retrying; anything else is reported at once."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    published = {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")}
    attempts = 0

    def denied(self: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "Permission denied")

    clock = _FakeClock()
    monkeypatch.setattr(Path, "rename", denied)

    with pytest.raises(PermissionError, match="Permission denied"):
        _release_with_fake_clock(monkeypatch, lock_path, ownership, clock)

    assert attempts == 1
    assert clock.sleeps == []
    assert {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")} == published


@pytest.mark.parametrize("owner_bytes", [None, b"not json"], ids=["missing", "corrupt"])
def test_release_removes_a_lock_whose_owner_metadata_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_bytes: bytes | None
) -> None:
    """A lost or corrupt owner.json leaves the published directory as the identity."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    if owner_bytes is None:
        (lock_path / "owner.json").unlink()
    else:
        (lock_path / "owner.json").write_bytes(owner_bytes)
    _fake_kernel32_off_windows(monkeypatch)
    _windows_release(monkeypatch)

    runguard._release_lock(lock_path, ownership)

    assert not lock_path.exists()


def test_release_leaves_a_reserved_worktree_lease_directory_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease dir must be empty after a run so its owner can remove it."""
    lease = tmp_path / ".run-locks" / "repo-run"
    lease.mkdir(parents=True)
    lock_path = lease / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    _fake_kernel32_off_windows(monkeypatch)
    _windows_release(monkeypatch)

    runguard._release_lock(lock_path, ownership)

    assert not lock_path.exists()
    assert sorted(entry.name for entry in lease.iterdir()) == []


def test_a_reserved_worktree_lock_keeps_its_gate_out_of_the_lease_directory(tmp_path: Path) -> None:
    gate = runguard_gate._recovery_gate_path(tmp_path / ".run-locks" / "repo-run" / "run.lock")
    assert gate == tmp_path / ".run-locks" / ".repo-run.run.lock.recovery-gate"
    assert runguard_gate._recovery_gate_path(tmp_path / "run.lock") == tmp_path / ".run.lock.recovery-gate"


def test_the_lease_gate_still_excludes_a_second_recoverer(tmp_path: Path) -> None:
    lock_path = tmp_path / ".run-locks" / "repo-run" / "run.lock"
    lock_path.parent.mkdir(parents=True)

    with runguard._stale_recovery_gate(lock_path) as first:
        assert first
        with runguard._stale_recovery_gate(lock_path) as second:
            assert second is False
    with runguard._stale_recovery_gate(lock_path) as again:
        assert again


def test_claim_stale_lock_reads_the_owner_metadata_once_per_stale_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge and the re-judge each need one read, not one per question they ask."""
    lock_path = tmp_path / "tasks.json.lock"
    _dead_owner_lock(lock_path, monkeypatch)
    reads: list[Path] = []
    original = runguard._read_lock_owner

    def counting(path: Path) -> dict[str, object] | None:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(runguard, "_read_lock_owner", counting)

    claimed = runguard._claim_stale_lock(lock_path)

    assert claimed is not None
    assert len(reads) == 4, "one owner.json read per snapshot, plus one for the claimed lock's owner"


@pytest.mark.skipif(os.name == "nt", reason="POSIX release path; the Windows release is covered above")
def test_posix_release_never_deletes_metadata_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX renames a directory with readers inside it, so its release must not change."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    monkeypatch.setattr(
        nt_process,
        "delete_file_sharing_readers",
        lambda path: pytest.fail("a POSIX release must rename the directory as it always did"),
    )
    seen = _spy_rename(monkeypatch, lock_path)

    runguard._release_lock(lock_path, ownership)

    assert seen == [["owner.json", "pid"]]
    assert not lock_path.exists()


def test_run_lane_state_reads_run_lock_owner_through_the_shared_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "owner.json"
    target.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    opened: list[Path] = []
    original = nt_process.open_shared_delete

    def spy(path: Path | str) -> int:
        opened.append(Path(path))
        return original(path)

    monkeypatch.setattr(run_lane_state.nt_process, "open_shared_delete", spy)
    monkeypatch.setattr(run_lane_state, "os", _NtOs())

    payload, _digest = run_lane_state._read_json(target)

    assert payload == {"pid": 4242}
    assert opened == [target], "a plain os.open here would block the holder's release on Windows"


def test_owner_metadata_is_read_through_the_shared_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain read of owner.json refuses the holder's POSIX-semantics delete."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    reads: list[Path] = []
    original = nt_process.read_text_sharing_delete

    def spy(path: Path | str) -> str:
        reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(nt_process, "read_text_sharing_delete", spy)

    assert runguard._read_lock_owner(lock_path)["owner_token"] == ownership.owner_token
    assert reads == [lock_path / "owner.json"]


@requires_fake_kernel32
def test_release_reads_the_metadata_it_may_restore_through_the_shared_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    reads: list[Path] = []
    original = nt_process.read_bytes_sharing_delete

    def spy(path: Path | str) -> bytes:
        reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(nt_process, "read_bytes_sharing_delete", spy)

    runguard._release_lock(lock_path, ownership)

    assert reads == [lock_path / "pid", lock_path / "owner.json"]
    assert not lock_path.exists()


@requires_fake_kernel32
def test_a_stale_claimer_cannot_take_the_lock_the_release_is_emptying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the unlinks and the rename the lock directory is empty and reads stale."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    original_rename = Path.rename
    verdicts: list[object] = []

    def claim_between_withhold_and_rename(self: Path, target: Path) -> Path:
        if self == lock_path and not verdicts:
            verdicts.append(runguard._claim_stale_lock(lock_path))
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", claim_between_withhold_and_rename)

    runguard._release_lock(lock_path, ownership)

    assert verdicts == [runguard._RecoveryGate.BUSY], "the recovery gate must cover the empty directory"
    assert not lock_path.exists()


@requires_fake_kernel32
def test_release_restores_its_metadata_before_a_waiter_can_judge_the_lock_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry releases the gate, so the metadata must be back before the next waiter looks."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    published = {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")}
    _fake_kernel32(monkeypatch, FakeKernel32())
    _windows_release(monkeypatch)
    _deny_rename(monkeypatch)
    clock = _FakeClock()
    verdicts: list[object] = []
    sleeping = clock.sleep

    def claim_while_the_release_sleeps(seconds: float) -> None:
        verdicts.append(runguard._claim_stale_lock(lock_path))
        sleeping(seconds)

    clock.sleep = claim_while_the_release_sleeps  # type: ignore[method-assign]

    with pytest.raises(PermissionError):
        _release_with_fake_clock(monkeypatch, lock_path, ownership, clock)

    assert verdicts, "the release must have retried"
    assert verdicts == [None] * len(verdicts), "a waiter must find the restored live owner, not a dead lock"
    assert {name: (lock_path / name).read_bytes() for name in ("pid", "owner.json")} == published


@requires_fake_kernel32
def test_release_stops_at_a_replacement_owner_that_reuses_the_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NTFS hands a released directory's inode straight on, so the token is the identity."""
    lock_path = tmp_path / "run.lock"
    published = runguard._publish_lock(lock_path, run_dir=None)
    info = lock_path.stat()
    stale_lease = runguard._LockOwnership("stale-lease", info.st_dev, info.st_ino)
    fake = FakeKernel32()
    _fake_kernel32(monkeypatch, fake)
    _windows_release(monkeypatch)

    runguard._release_lock(lock_path, stale_lease)

    assert fake.create_calls == [], "the release must not delete another owner's metadata"
    assert fake.disposition_calls == []
    assert sorted(entry.name for entry in lock_path.iterdir()) == ["owner.json", "pid"]
    assert runguard._read_lock_owner(lock_path)["owner_token"] == published.owner_token


@requires_fake_kernel32
def test_a_reader_retries_a_name_whose_delete_is_still_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """STATUS_DELETE_PENDING answers ERROR_ACCESS_DENIED for a moment, then the open works."""
    target = tmp_path / "owner.json"
    target.write_text('{"pid": 1}', encoding="utf-8")
    fake = FakeKernel32(deny_opens=2)
    _fake_kernel32(monkeypatch, fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "_last_error", lambda: ERROR_ACCESS_DENIED)
    monkeypatch.setattr(nt_process, "DELETE_PENDING_RETRY_SECONDS", 0.0)

    assert nt_process.read_text_sharing_delete(target) == '{"pid": 1}'
    assert len(fake.create_calls) == 3
    assert fake.close_calls == [0x100], "only the handle that opened is closed"


@requires_fake_kernel32
def test_a_reader_reports_a_pending_delete_as_a_missing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "owner.json"
    target.write_text("{}", encoding="utf-8")
    fake = FakeKernel32(deny_opens=99)
    _fake_kernel32(monkeypatch, fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "_last_error", lambda: ERROR_ACCESS_DENIED)
    monkeypatch.setattr(nt_process, "DELETE_PENDING_RETRY_SECONDS", 0.0)

    with pytest.raises(FileNotFoundError):
        nt_process.read_text_sharing_delete(target)

    assert len(fake.create_calls) == nt_process.DELETE_PENDING_RETRY_ATTEMPTS


@requires_fake_kernel32
def test_lock_metadata_reads_do_not_fail_hard_while_a_release_deletes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers like reconcile_run_lock must see a lock going away, not a refusal."""
    lock_path = tmp_path / "run.lock"
    runguard._publish_lock(lock_path, run_dir=None)
    fake = FakeKernel32(deny_opens=999)
    _fake_kernel32(monkeypatch, fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "_last_error", lambda: ERROR_ACCESS_DENIED)
    monkeypatch.setattr(nt_process, "DELETE_PENDING_RETRY_SECONDS", 0.0)

    assert runguard._read_lock_owner(lock_path) is None
    assert runguard._lock_snapshot(lock_path) is not None
    assert runguard._lock_is_stale(lock_path) is True


# This is the acceptance test for issue #2747 on Windows. It must run there: if a
# Windows affected-tests run reports a skip in this file, it is the POSIX test above.
@pytest.mark.skipif(os.name != "nt", reason="needs NTFS POSIX-semantics delete and real share-delete readers")
def test_release_succeeds_while_waiters_poll_the_lock(tmp_path: Path) -> None:
    """The Windows failure from issue #2747, with the release running under live readers."""
    lock_path = tmp_path / "run.lock"
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    failures: list[BaseException] = []
    stop = threading.Event()

    def waiter() -> None:
        # The reader path every waiter runs: _lock_snapshot and read_lock_owner,
        # both of which open pid and owner.json through the share-delete open.
        while not stop.is_set():
            try:
                runguard._lock_snapshot(lock_path)
                runguard.read_lock_owner(lock_path)
            except BaseException as exc:  # noqa: BLE001 - any failure is the finding
                failures.append(exc)
                return

    held = [nt_process.open_shared_delete(lock_path / name) for name in ("pid", "owner.json")]
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    try:
        assert [len(os.read(reader, 1)) for reader in held] == [1, 1]
        runguard._release_lock(lock_path, ownership)
    finally:
        stop.set()
        thread.join(timeout=10)
        for reader in held:
            os.close(reader)

    assert failures == []
    assert not thread.is_alive()
    assert not lock_path.exists(), "a release under readers must not strand the lock"
    held_lock = runguard._acquire_lock(lock_path)
    assert lock_path.is_dir()
    runguard._release_lock(lock_path, held_lock)
    assert not lock_path.exists()
