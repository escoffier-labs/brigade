"""Focused coverage for promoted descriptor-relative primitives."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from brigade import dirfd
from brigade.work_cmd.ledger import authority_store


def _posix_dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@pytest.mark.skipif(not dirfd.posix_available(), reason="POSIX dir_fd plus O_NOFOLLOW required")
def test_open_directory_nofollow_refuses_final_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    fd = dirfd.open_directory_nofollow(real)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)

    with pytest.raises(OSError):
        dirfd.open_directory_nofollow(link)


@pytest.mark.skipif(not dirfd.posix_available(), reason="POSIX dir_fd plus O_NOFOLLOW required")
def test_child_directory_and_file_round_trip(tmp_path: Path) -> None:
    parent = dirfd.open_directory_nofollow(tmp_path)
    child = -1
    created = -1
    try:
        dirfd.mkdir_child(parent, "events")
        child = dirfd.open_child_directory(parent, "events")
        created = dirfd.open_child_file(
            child,
            "lifecycle.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.write(created, b"event\n")
        os.close(created)
        created = -1
        named = dirfd.stat_child(child, "lifecycle.jsonl")
        assert stat.S_ISREG(named.st_mode)
        assert named.st_size == 6
        dirfd.replace_children(child, "lifecycle.jsonl", "renamed.jsonl")
        dirfd.fsync_directory(child)
        assert (tmp_path / "events" / "renamed.jsonl").read_bytes() == b"event\n"
        dirfd.unlink_child(child, "renamed.jsonl")
        with pytest.raises(FileNotFoundError):
            dirfd.stat_child(child, "renamed.jsonl")
    finally:
        if created != -1:
            os.close(created)
        if child != -1:
            os.close(child)
        os.close(parent)


def test_validate_component_rejects_empty_dot_dotdot_separators_and_nul() -> None:
    assert dirfd.validate_component("run.json") == "run.json"
    assert dirfd.validate_component("recovery-checkpoints") == "recovery-checkpoints"
    with pytest.raises(OSError, match="empty"):
        dirfd.validate_component("")
    with pytest.raises(OSError, match="contained"):
        dirfd.validate_component(".")
    with pytest.raises(OSError, match="contained"):
        dirfd.validate_component("..")
    with pytest.raises(OSError, match="single contained name"):
        dirfd.validate_component("events/lifecycle.jsonl")
    with pytest.raises(OSError, match="single contained name"):
        dirfd.validate_component("events\\lifecycle.jsonl")
    with pytest.raises(OSError, match="single contained name"):
        dirfd.validate_component("a\x00b")


def test_dirfd_unavailable_message_matches_authority_store() -> None:
    error = dirfd.unavailable("import inbox operations")
    assert str(error) == "descriptor-relative import inbox operations are unavailable"
    assert str(authority_store._dirfd_unavailable("import inbox operations")) == str(error)


def test_authority_store_aliases_honor_monkeypatched_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authority_store, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(authority_store, "_nt_dirfd_available", lambda: False)
    with pytest.raises(OSError, match="descriptor-relative directory operations are unavailable"):
        authority_store._open_directory_nofollow(tmp_path)
    with pytest.raises(OSError, match="descriptor-relative import inbox operations are unavailable"):
        authority_store._dirfd_open_file(3, "inbox.jsonl", os.O_RDONLY)


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="stand-in NT helpers need POSIX dir_fd")
def test_authority_store_uses_nt_branch_when_posix_is_patched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    def fake_nt(path: Path) -> int:
        opened.append(os.fspath(path))
        return os.open(path, _posix_dir_flags())

    monkeypatch.setattr(authority_store, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(authority_store, "_nt_dirfd_available", lambda: True)
    monkeypatch.setattr(dirfd, "_nt_open_directory", fake_nt)
    descriptor = authority_store._open_directory_nofollow(tmp_path)
    try:
        assert opened == [os.fspath(tmp_path)]
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="stand-in NT helpers need POSIX dir_fd")
def test_dirfd_public_api_delegates_to_nt_when_posix_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brigade.work_cmd import nt_dirfd

    calls: list[tuple[str, object]] = []

    def open_root(path: Path | str, *, writable: bool = True) -> int:
        del writable
        calls.append(("root", os.fspath(path)))
        return os.open(path, _posix_dir_flags())

    def open_child(parent: int, name: str, *, writable: bool = True) -> int:
        del writable
        calls.append(("child", name))
        return os.open(name, _posix_dir_flags(), dir_fd=parent)

    def mkdir_child(parent: int, name: str) -> None:
        calls.append(("mkdir", name))
        os.mkdir(name, 0o700, dir_fd=parent)

    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: True)
    monkeypatch.setattr(nt_dirfd, "open_root_directory", open_root)
    monkeypatch.setattr(nt_dirfd, "open_child_directory", open_child)
    monkeypatch.setattr(nt_dirfd, "mkdir_child", mkdir_child)

    parent = dirfd.open_directory_nofollow(tmp_path)
    child = -1
    try:
        dirfd.mkdir_child(parent, "events")
        child = dirfd.open_child_directory(parent, "events")
        assert ("root", os.fspath(tmp_path)) in calls
        assert ("mkdir", "events") in calls
        assert ("child", "events") in calls
    finally:
        if child != -1:
            os.close(child)
        os.close(parent)


def test_authority_store_availability_aliases_match_dirfd() -> None:
    assert authority_store._posix_dirfd_available() is dirfd.posix_available()
    assert authority_store._nt_dirfd_available() is (sys.platform == "win32")
    assert authority_store._dirfd_available() is dirfd.available()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory rename interleaving")
def test_child_names_enumerates_the_held_directory_after_ancestor_swap(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    directory = parent / "entries"
    directory.mkdir(parents=True)
    (directory / "original").touch()
    outside = tmp_path / "outside"
    (outside / "entries").mkdir(parents=True)
    (outside / "entries/forged").touch()
    descriptor = dirfd.open_directory_nofollow(directory)
    try:
        parent.rename(tmp_path / "moved")
        parent.symlink_to(outside, target_is_directory=True)
        assert dirfd.child_names(descriptor, 2) == ["original"]
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("malformed", [None, "size", "offset", "length", "utf16", "status", "unavailable"])
def test_nt_directory_enumeration_is_bounded_and_rejects_malformed_replies(monkeypatch, malformed):
    import ctypes
    import struct
    from types import SimpleNamespace

    from brigade.work_cmd import nt_dirfd

    names = iter([".", "..", "original", "not-read"])
    calls = []

    def query(handle, event, apc, context, block, buffer, capacity, info_class, single, pattern, restart):
        calls.append((handle, capacity, info_class, single, restart))
        if malformed == "status":
            return 0x80000005  # STATUS_BUFFER_OVERFLOW is never a complete entry.
        raw = next(names).encode("utf-16-le")
        offset = 1 if malformed == "offset" else 0
        length = 3 if malformed == "length" else len(raw)
        if malformed == "utf16":
            raw = b"\x00\xd8"  # unpaired surrogate
            length = len(raw)
        record = struct.pack("<III", offset, 0, length) + raw
        ctypes.memmove(buffer, record, len(record))
        iosb = ctypes.cast(block, ctypes.POINTER(nt_dirfd._IO_STATUS_BLOCK)).contents
        iosb.Information = capacity + 1 if malformed == "size" else len(record)
        return 0

    api = SimpleNamespace(
        IO_STATUS_BLOCK=nt_dirfd._IO_STATUS_BLOCK, NtQueryDirectoryFile=None if malformed == "unavailable" else query
    )
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: api)
    monkeypatch.setattr(nt_dirfd, "_handle_from_fd", lambda fd: 99)
    if malformed:
        with pytest.raises(OSError):
            nt_dirfd.child_names(3, 1)
    else:
        assert nt_dirfd.child_names(3, 1) == ["original"]
        assert len(calls) == 3
        assert calls[0] == (99, 65548, 12, True, True)
        assert all(call[-1] is False for call in calls[1:])


def test_child_names_never_falls_back_to_a_pathname(monkeypatch):
    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: False)
    with pytest.raises(OSError, match="directory enumeration.*unavailable"):
        dirfd.child_names(3, 1)


@pytest.fixture
def nt_metadata_kernel(monkeypatch):
    """Exercise real NT helpers against a kernel that enforces object kinds."""
    import ctypes
    from types import SimpleNamespace

    from brigade.work_cmd import nt_dirfd

    entries = {
        "": nt_dirfd._FILE_ATTRIBUTE_DIRECTORY,
        ".brigade": nt_dirfd._FILE_ATTRIBUTE_DIRECTORY,
        "runs": nt_dirfd._FILE_ATTRIBUTE_DIRECTORY,
        "receipt.json": nt_dirfd._FILE_ATTRIBUTE_NORMAL,
        "link": nt_dirfd._FILE_ATTRIBUTE_REPARSE_POINT,
        "junction": nt_dirfd._FILE_ATTRIBUTE_DIRECTORY | nt_dirfd._FILE_ATTRIBUTE_REPARSE_POINT,
    }
    handles = {}
    live = set()
    closed = []
    opens = []
    failures = set()
    real_fstat, real_close = os.fstat, os.close

    def allocate(name):
        handle = 10000 + len(handles)
        handles[handle] = name
        live.add(handle)
        return handle

    def create(output, access, object_attributes, iosb, size, attributes, share, disposition, options, ea, ea_size):
        obj = ctypes.cast(object_attributes, ctypes.POINTER(nt_dirfd._OBJECT_ATTRIBUTES)).contents
        name = obj.ObjectName.contents.Buffer
        assert obj.RootDirectory in live
        opens.append((obj.RootDirectory, name, access, disposition, options, share))
        if name not in entries:
            return nt_dirfd._STATUS_OBJECT_NAME_NOT_FOUND
        if "open" in failures:
            return nt_dirfd._STATUS_ACCESS_DENIED
        is_directory = bool(entries[name] & nt_dirfd._FILE_ATTRIBUTE_DIRECTORY)
        if is_directory and options & nt_dirfd._FILE_NON_DIRECTORY_FILE:
            return nt_dirfd._STATUS_FILE_IS_A_DIRECTORY
        if not is_directory and options & nt_dirfd._FILE_DIRECTORY_FILE:
            return nt_dirfd._STATUS_NOT_A_DIRECTORY
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = (
            0 if "invalid" in failures else allocate(name)
        )
        return 0

    def get_info(handle, output):
        if "info" in failures:
            return False
        info = ctypes.cast(output, ctypes.POINTER(nt_dirfd._BY_HANDLE_FILE_INFORMATION)).contents
        info.dwFileAttributes = entries[handles[handle]]
        return True

    def close(handle):
        if handle not in handles:
            return real_close(handle)
        closed.append(handle)
        live.remove(handle)

    def convert(handle, flags):
        if "convert" in failures:
            raise OSError("CRT conversion failed")
        return handle

    def fstat(handle):
        if handle not in handles:
            return real_fstat(handle)
        if "fstat" in failures:
            raise OSError("metadata failed")
        mode = stat.S_IFDIR if entries[handles[handle]] & nt_dirfd._FILE_ATTRIBUTE_DIRECTORY else stat.S_IFREG
        return os.stat_result((mode | 0o600, handle, 7, 1, 0, 0, 12, 0, 0, 0))

    api = nt_dirfd._bind_api_namespace()
    api.NtCreateFile = create
    api.CreateFileW = lambda *args: allocate("")
    api.GetFileInformationByHandle = get_info
    api.CloseHandle = close
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: api)
    monkeypatch.setattr(nt_dirfd, "_win_error", lambda: OSError("metadata query failed"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=convert, get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: True)
    return SimpleNamespace(opens=opens, live=live, closed=closed, failures=failures)


def test_assessment_reader_descends_directories_with_nt_metadata(tmp_path, nt_metadata_kernel):
    from brigade.control_crosswalk import _AssessmentReader

    kernel = nt_metadata_kernel
    reader = _AssessmentReader(tmp_path)
    try:
        descriptor = reader.directory(tmp_path / ".brigade/runs")
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        assert reader.directory(tmp_path / ".brigade/runs") == descriptor
        assert [call[1] for call in kernel.opens] == [".brigade", ".brigade", "runs", "runs"]
    finally:
        reader.close()
    assert not kernel.live
    assert len(kernel.closed) == len(set(kernel.closed)) == 5


@pytest.mark.parametrize("name,is_directory", [("receipt.json", False), ("runs", True)])
def test_nt_stat_child_reads_only_metadata_and_closes(tmp_path, nt_metadata_kernel, name, is_directory):
    from brigade.work_cmd import nt_dirfd

    kernel = nt_metadata_kernel
    parent = nt_dirfd.open_root_directory(tmp_path, writable=False)
    try:
        result = dirfd.stat_child(parent, name)
        assert stat.S_ISDIR(result.st_mode) is is_directory
        assert result.st_dev == 7
        assert result.st_size == 12
        root, component, access, disposition, options, share = kernel.opens[-1]
        assert root == parent
        assert component == name
        assert access == nt_dirfd._FILE_READ_ATTRIBUTES | nt_dirfd._SYNCHRONIZE
        assert disposition == nt_dirfd._FILE_OPEN
        assert options == nt_dirfd._FILE_OPEN_REPARSE_POINT | nt_dirfd._FILE_SYNCHRONOUS_IO_NONALERT
        assert share == nt_dirfd._SHARE_ALL
        assert kernel.live == {parent}
    finally:
        os.close(parent)
    assert len(kernel.closed) == len(set(kernel.closed)) == 2


@pytest.mark.parametrize(
    "name,failure,error,message,child_closed",
    [
        ("link", None, OSError, "reparse point", True),
        ("junction", None, OSError, "reparse point", True),
        ("missing", None, FileNotFoundError, "does not exist", False),
        ("receipt.json", "open", PermissionError, "access denied", False),
        ("receipt.json", "invalid", OSError, "invalid handle", False),
        ("receipt.json", "info", OSError, "metadata query failed", True),
        ("receipt.json", "convert", OSError, "CRT conversion failed", True),
        ("receipt.json", "fstat", OSError, "metadata failed", True),
        ("../receipt.json", None, OSError, "single contained name", False),
    ],
)
def test_nt_stat_child_refuses_unsafe_or_failed_metadata_without_leaks(
    tmp_path, nt_metadata_kernel, name, failure, error, message, child_closed
):
    from brigade.work_cmd import nt_dirfd

    kernel = nt_metadata_kernel
    parent = nt_dirfd.open_root_directory(tmp_path, writable=False)
    if failure:
        kernel.failures.add(failure)
    try:
        with pytest.raises(error, match=message):
            dirfd.stat_child(parent, name)
        assert kernel.live == {parent}
        assert len(kernel.closed) == int(child_closed)
    finally:
        os.close(parent)
    assert len(kernel.closed) == len(set(kernel.closed))
