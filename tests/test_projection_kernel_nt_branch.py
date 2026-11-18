"""The projection kernel's Windows branch, run on Linux through a handle-API stand-in.

CI's Windows shard is the real check; these tests keep the branch honest between
runs. ``dirfd`` is switched to its Windows backend and pointed at a stand-in for
``work_cmd.nt_dirfd`` built on POSIX descriptors, so the kernel takes the same
code path it takes on Windows: handle-relative rename-aside, digest of the moved
object, publish or rename back. Without a held parent the mutation would run on
pathnames and a change made after the final check would be committed.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from brigade import dirfd as dirfd_mod
from brigade.projection import kernel as projection
from tests.projection_fixture import mixed_workspace


class _Refused(OSError):
    """Stand-in for ReparsePointRefusedError."""


def _fake_nt(calls: list[tuple[str, ...]]) -> SimpleNamespace:
    def open_root_directory(path: Path | str, *, writable: bool = True) -> int:
        calls.append(("open_root_directory", str(writable)))
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def open_child_directory(parent: int, name: str, *, writable: bool = True) -> int:
        calls.append(("open_child_directory", name, str(writable)))
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)

    def mkdir_child(parent: int, name: str) -> None:
        calls.append(("mkdir_child", name))
        os.mkdir(name, dir_fd=parent)

    def reopen_directory_writable(parent: int, *, modify: bool = False) -> int:
        calls.append(("reopen_directory_writable", str(modify)))
        return os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)

    def open_file(parent: int, name: str, flags: int, mode: int = 0o600, *, share_write: bool = True) -> int:
        calls.append(("open_file", name, str(share_write)))
        info = None
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise _Refused("path component is not a regular file")
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=parent)

    def replace_children(
        parent: int,
        source: str,
        destination: str,
        *,
        replace: bool = True,
        destination_parent: int | None = None,
        directory: bool = False,
    ) -> None:
        calls.append(("replace_children", source, destination, str(replace)))
        target_parent = parent if destination_parent is None else destination_parent
        if replace:
            os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=target_parent)
            return
        os.link(source, destination, src_dir_fd=parent, dst_dir_fd=target_parent, follow_symlinks=False)
        os.unlink(source, dir_fd=parent)

    def unlink_child(parent: int, name: str, *, directory: bool = False, allow_reparse: bool = False) -> None:
        calls.append(("unlink_child", name))
        os.unlink(name, dir_fd=parent)

    def stat_child(parent: int, name: str, *, allow_reparse: bool = False) -> os.stat_result:
        calls.append(("stat_child", name))
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    return SimpleNamespace(
        ReparsePointRefusedError=_Refused,
        available=lambda: True,
        open_root_directory=open_root_directory,
        open_child_directory=open_child_directory,
        mkdir_child=mkdir_child,
        reopen_directory_writable=reopen_directory_writable,
        open_file=open_file,
        replace_children=replace_children,
        unlink_child=unlink_child,
        stat_child=stat_child,
    )


@pytest.fixture
def nt_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Switch ``dirfd`` to the Windows backend, served by the stand-in; return the operations it saw."""
    calls: list[tuple[str, ...]] = []
    fake = _fake_nt(calls)
    monkeypatch.setattr(dirfd_mod, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd_mod, "nt_available", lambda: True)
    monkeypatch.setattr(dirfd_mod, "_nt_dirfd", lambda: fake)
    return calls


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quarantine_renames(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [call for call in calls if call[0] == "replace_children" and call[2].endswith(".quarantine")]


def test_windows_branch_holds_the_parent_and_quarantines_before_it_mutates(
    tmp_path: Path, nt_calls: list[tuple[str, ...]]
) -> None:
    workspace, plan = mixed_workspace(tmp_path)

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "committed"
    assert (workspace / "b-replace.txt").read_bytes() == b"new\n"
    assert not (workspace / "c-remove.txt").exists()
    assert list(workspace.rglob("*.quarantine")) == []
    moved = {call[1] for call in _quarantine_renames(nt_calls)}
    assert moved == {"b-replace.txt", "c-remove.txt"}
    # The held parent is writable (renames and creates need it) and reached through handle operations only.
    assert ("reopen_directory_writable", "True") in nt_calls
    # The moved copy is read with concurrent writers denied, so the digest is of one stable object.
    assert any(call[0] == "open_file" and call[1].endswith(".quarantine") and call[2] == "False" for call in nt_calls)


@pytest.mark.parametrize("victim", ["b-replace.txt", "c-remove.txt"])
def test_windows_branch_catches_an_edit_after_the_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nt_calls: list[tuple[str, ...]], victim: str
) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    real_check = projection._assert_unchanged_since_plan

    def check_then_edit(spec: projection.MutationSpec) -> None:
        real_check(spec)
        if spec.destination.name == victim:
            spec.destination.write_bytes(b"operator edit after the check\n")

    monkeypatch.setattr(projection, "_assert_unchanged_since_plan", check_then_edit)

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "restored"
    assert (workspace / victim).read_bytes() == b"operator edit after the check\n"
    assert not (workspace / "a-create.txt").exists()
    assert list(workspace.rglob("*.quarantine")) == []
    # The victim was moved aside through the handle before anything was published over it.
    assert victim in [call[1] for call in _quarantine_renames(nt_calls)]


def test_windows_branch_catches_a_write_into_the_quarantined_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nt_calls: list[tuple[str, ...]]
) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    real_rename = projection._quarantine_rename

    def rename_then_write_to_the_moved_file(parent_fd: int, name: str, quarantine_name: str) -> None:
        real_rename(parent_fd, name, quarantine_name)
        if name == "c-remove.txt":
            (workspace / quarantine_name).write_bytes(b"late edit through the moved file\n")

    monkeypatch.setattr(projection, "_quarantine_rename", rename_then_write_to_the_moved_file)

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "restored"
    assert (workspace / "c-remove.txt").read_bytes() == b"late edit through the moved file\n"
    assert list(workspace.rglob("*.quarantine")) == []


def test_windows_branch_fails_closed_when_the_rename_aside_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nt_calls: list[tuple[str, ...]]
) -> None:
    """An open handle without delete sharing refuses the rename; nothing is then published or removed."""
    workspace, plan = mixed_workspace(tmp_path)
    nt = dirfd_mod._nt_dirfd()
    real_replace = nt.replace_children

    def refuse_quarantine_renames(parent: int, source: str, destination: str, **kwargs: Any) -> None:
        if destination.endswith(".quarantine"):
            raise PermissionError("sharing violation")
        real_replace(parent, source, destination, **kwargs)

    monkeypatch.setattr(nt, "replace_children", refuse_quarantine_renames)

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "restored"
    assert (workspace / "b-replace.txt").read_bytes() == b"old\n"
    assert (workspace / "c-remove.txt").read_bytes() == b"gone-soon\n"
    assert not (workspace / "a-create.txt").exists()
    assert not any(call[0] == "unlink_child" and call[1] == "c-remove.txt" for call in nt_calls)


def test_windows_branch_restores_when_a_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nt_calls: list[tuple[str, ...]]
) -> None:
    workspace, plan = mixed_workspace(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", boom)

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "restored"
    assert (workspace / "b-replace.txt").read_bytes() == b"old\n"
    assert (workspace / "c-remove.txt").read_bytes() == b"gone-soon\n"
    assert list(workspace.rglob("*.quarantine")) == []


def test_windows_branch_hands_a_writer_the_destination_path(tmp_path: Path, nt_calls: list[tuple[str, ...]]) -> None:
    """No pathname opens through a Windows handle, so a custom writer receives the plain destination."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "merged.txt").write_bytes(b"old\n")
    seen: list[Path] = []

    def writer(path: Path, data: bytes) -> None:
        seen.append(path)
        path.write_bytes(data)

    plan = projection.build_plan(
        operation_id="nt-writer",
        projector="nt-branch",
        source_fingerprint=_digest(b"source"),
        mutations=[
            projection.mutation(
                destination=workspace / "merged.txt",
                mutation="replace",
                expected_before=_digest(b"old\n"),
                desired_after=_digest(b"merged\n"),
                staged_bytes=b"merged\n",
                writer=writer,
            )
        ],
        target=workspace,
    )

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "committed"
    assert seen == [workspace.resolve() / "merged.txt"]
    assert (workspace / "merged.txt").read_bytes() == b"merged\n"
    # A merge writer manages its own bytes in the live file, so there is nothing to move aside.
    assert _quarantine_renames(nt_calls) == []


def test_windows_branch_creates_missing_parents_through_the_held_handle(
    tmp_path: Path, nt_calls: list[tuple[str, ...]]
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plan = projection.build_plan(
        operation_id="nt-nested",
        projector="nt-branch",
        source_fingerprint=_digest(b"source"),
        mutations=[
            projection.mutation(
                destination=workspace / "deep" / "er" / "file.txt",
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(b"x\n"),
                staged_bytes=b"x\n",
            )
        ],
        target=workspace,
    )

    receipt = projection.execute(plan, target=workspace)

    assert receipt.terminal_state == "committed"
    assert (workspace / "deep" / "er" / "file.txt").read_bytes() == b"x\n"
    assert [call[1] for call in nt_calls if call[0] == "mkdir_child"] == ["deep", "er"]
