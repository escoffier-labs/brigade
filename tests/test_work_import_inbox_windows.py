"""Windows-capable import-inbox parent open and containment (#1022)."""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import authority_key
from brigade.work_cmd import helpers, ledger
from brigade.work_cmd import nt_dirfd


def _link_directory(link: Path, target: Path) -> None:
    """Create a directory symlink, or a junction on Windows without admin."""
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and link.exists():
            return
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except OSError as exc:
            raise OSError(f"unable to create directory reparse point: {completed.stderr or exc}") from exc
    link.symlink_to(target, target_is_directory=True)


def test_nt_dirfd_available_only_on_windows() -> None:
    assert nt_dirfd.available() is (sys.platform == "win32")
    assert ledger._nt_dirfd_available() is (sys.platform == "win32")


def test_nt_dirfd_api_binder_does_not_nameerror() -> None:
    """Regression: nested class-body assignment raised NameError on Windows CI."""
    api = nt_dirfd._bind_api_namespace()
    assert api.OBJECT_ATTRIBUTES is nt_dirfd._OBJECT_ATTRIBUTES
    obj = api.OBJECT_ATTRIBUTES()
    obj.Length = ctypes.sizeof(api.OBJECT_ATTRIBUTES)
    us, buf = api.make_unicode("imports")
    assert us.Length == len("imports") * 2
    assert buf[0] == "i"
    rename = api.make_rename_class(len("inbox.jsonl"))
    info = rename()
    info.ReplaceIfExists = True
    info.FileName = "inbox.jsonl"
    info.FileNameLength = len("inbox.jsonl") * 2
    assert ctypes.sizeof(info) > 0


def test_nt_dirfd_directory_access_is_traversal_without_delete() -> None:
    """Intermediate/parent directory opens must not request DELETE (Win11 ACCESS_DENIED)."""
    traverse = nt_dirfd._DIRECTORY_TRAVERSE_ACCESS
    modify = nt_dirfd._DIRECTORY_MODIFY_ACCESS
    assert traverse & nt_dirfd._FILE_LIST_DIRECTORY
    assert traverse & nt_dirfd._FILE_TRAVERSE
    assert traverse & nt_dirfd._SYNCHRONIZE
    assert not (traverse & nt_dirfd._DELETE)
    assert not (modify & nt_dirfd._DELETE)
    assert modify & nt_dirfd._FILE_ADD_FILE
    assert modify & nt_dirfd._FILE_ADD_SUBDIRECTORY
    assert modify & nt_dirfd._FILE_DELETE_CHILD
    assert nt_dirfd._directory_access(writable=False) == traverse
    assert nt_dirfd._directory_access(writable=True) == modify
    assert nt_dirfd._SHARE_ALL == (nt_dirfd._FILE_SHARE_READ | nt_dirfd._FILE_SHARE_WRITE | nt_dirfd._FILE_SHARE_DELETE)
    assert nt_dirfd._FILE_OPEN_REPARSE_POINT == 0x00200000
    assert nt_dirfd._FILE_WRITE_ACCESS & nt_dirfd._FILE_WRITE_DATA
    assert nt_dirfd._FILE_READ_ACCESS & nt_dirfd._FILE_READ_DATA
    assert not hasattr(nt_dirfd, "_GENERIC_WRITE")


def test_nt_dirfd_access_denied_names_the_component() -> None:
    with pytest.raises(PermissionError, match="path component access denied: imports"):
        nt_dirfd._raise_ntstatus(nt_dirfd._STATUS_ACCESS_DENIED, name="imports")
    with pytest.raises(PermissionError, match="path component access denied$"):
        nt_dirfd._raise_ntstatus(nt_dirfd._STATUS_ACCESS_DENIED)


def test_nt_dirfd_validate_component_rejects_traversal() -> None:
    nt_dirfd.validate_component("imports")
    nt_dirfd.validate_component("inbox.jsonl")
    with pytest.raises(OSError, match="contained"):
        nt_dirfd.validate_component("..")
    with pytest.raises(OSError, match="contained"):
        nt_dirfd.validate_component(".")
    with pytest.raises(OSError, match="single contained name"):
        nt_dirfd.validate_component("a/b")
    with pytest.raises(OSError, match="single contained name"):
        nt_dirfd.validate_component("a\\b")
    with pytest.raises(OSError, match="empty"):
        nt_dirfd.validate_component("")


@pytest.mark.skipif(sys.platform == "win32", reason="negative path is for hosts without the NT APIs")
def test_nt_dirfd_refuses_to_run_off_windows(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="unavailable"):
        nt_dirfd.open_root_directory(tmp_path)


def test_open_import_inbox_parent_uses_windows_path_when_posix_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a POSIX-only guard must not kill every import writer on Windows."""
    monkeypatch.setattr(ledger, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(ledger, "_nt_dirfd_available", lambda: True)
    opened: dict[str, object] = {}

    def fake_nt(target: Path, *, create: bool) -> tuple[int, str]:
        opened["target"] = target
        opened["create"] = create
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        return os.open(tmp_path, flags), "inbox.jsonl"

    monkeypatch.setattr(ledger, "_open_import_inbox_parent_nt", fake_nt)
    parent, name = ledger._open_import_inbox_parent(tmp_path, create=True)
    try:
        assert name == "inbox.jsonl"
        assert opened == {"target": tmp_path, "create": True}
    finally:
        os.close(parent)


def test_open_import_inbox_parent_succeeds_on_current_platform(tmp_path: Path) -> None:
    parent, name = ledger._open_import_inbox_parent(tmp_path, create=True)
    try:
        assert name == "inbox.jsonl"
        assert parent >= 0
        assert stat.S_ISDIR(os.fstat(parent).st_mode)
        assert (tmp_path / ".brigade" / "work" / "imports").is_dir()
        assert not (tmp_path / ".brigade" / "work" / "imports").is_symlink()
    finally:
        os.close(parent)


def test_open_import_inbox_parent_rejects_symlink_or_junction_component(tmp_path: Path) -> None:
    work = helpers._work_root(tmp_path)
    work.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "imports").mkdir(parents=True)
    _link_directory(work / "imports", outside / "imports")

    with pytest.raises(OSError):
        parent, _name = ledger._open_import_inbox_parent(tmp_path, create=True)
        os.close(parent)
    assert (outside / "imports").is_dir()
    assert list((outside / "imports").iterdir()) == []


def test_open_import_inbox_parent_rejects_inbox_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def escaped(_target: Path) -> Path:
        return tmp_path.parent / "escaped-inbox" / "inbox.jsonl"

    monkeypatch.setattr(ledger.helpers, "_imports_path", escaped)
    with pytest.raises(OSError, match="escapes target"):
        ledger._open_import_inbox_parent(tmp_path, create=True)


def _assert_no_bare_windows_open_flags(source_path: Path) -> None:
    """Linux-visible guard: bare os.O_NOFOLLOW/O_DIRECTORY crash on Windows."""
    source = source_path.read_text()
    for lineno, line in enumerate(source.splitlines(), 1):
        if "os.O_NOFOLLOW" in line and "getattr" not in line:
            raise AssertionError(f"bare os.O_NOFOLLOW at {source_path}:{lineno}: {line}")
        if "os.O_DIRECTORY" in line and "getattr" not in line:
            raise AssertionError(f"bare os.O_DIRECTORY at {source_path}:{lineno}: {line}")


def test_ledger_has_no_bare_windows_open_flags() -> None:
    _assert_no_bare_windows_open_flags(Path(ledger.__file__))
    _assert_no_bare_windows_open_flags(Path(authority_key.__file__))


def _append_manual_import(tmp_path: Path, text: str) -> dict[str, object]:
    record = ledger._sanitize_untrusted_import_record(
        {"text": text, "kind": "task", "source": "manual", "metadata": {}},
        importer_source="manual",
    )
    imported, skipped, dismissed, rejected = ledger._append_import_records(
        tmp_path,
        [record],
        provenance_source="manual",
        migrate_untrusted_identities=True,
    )
    assert imported
    assert skipped == []
    assert dismissed == []
    assert rejected == []
    return imported[0]


def _bind_workspace_authority(tmp_path: Path) -> dict[str, int]:
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    workspace = ledger._workspace_directory_identity(tmp_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root = os.open(tmp_path / ".brigade", flags)
    try:
        ledger._record_external_directory_authority(tmp_path, (".brigade",), root, workspace=workspace)
    finally:
        os.close(root)
    return workspace


@pytest.mark.skipif(sys.platform != "win32", reason="executes the Windows import proof and authority path")
def test_windows_import_proof_and_authority_store_round_trip(tmp_path: Path) -> None:
    """Run the full import write on Windows; do not grep a CI script."""
    imported = _append_manual_import(tmp_path, "windows proof path")
    assert ledger._has_persisted_import_proof(imported, target=tmp_path)
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert ledger._read_imports(tmp_path)


def test_append_import_records_executes_proof_and_authority_store(tmp_path: Path) -> None:
    """Linux CI must execute the import-proof authority write, not only grep a script."""
    imported = _append_manual_import(tmp_path, "windows inbox regression")
    inbox = helpers._imports_path(tmp_path)
    assert inbox.is_file()
    assert not inbox.is_symlink()
    assert "windows inbox regression" in inbox.read_text()
    assert ledger._read_imports(tmp_path)
    assert ledger._has_persisted_import_proof(imported, target=tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload["target"] == str(tmp_path.resolve())
    raw = path.read_text(encoding="utf-8")
    assert "envelope_version" in raw or "schema_version" in raw


def test_authority_store_survives_missing_posix_open_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the HMAC store after deleting os.O_NOFOLLOW/O_DIRECTORY (Windows)."""
    monkeypatch.delattr(ledger.os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(ledger.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(ledger, "_nt_dirfd_available", lambda: True)
    monkeypatch.setattr(authority_key, "_nt_nofollow_available", lambda: True)

    def open_path_file(path: Path | str, flags: int, mode: int = 0o600) -> int:
        create = bool(flags & os.O_CREAT)
        exclusive = bool(flags & os.O_EXCL)
        write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        open_flags = os.O_RDONLY
        if write:
            open_flags = os.O_WRONLY
        if create:
            open_flags |= os.O_CREAT
        if exclusive:
            open_flags |= os.O_EXCL
        return os.open(path, open_flags, mode) if create else os.open(path, open_flags)

    def open_root_directory(path: Path | str) -> int:
        return os.open(path, os.O_RDONLY)

    monkeypatch.setattr(nt_dirfd, "open_path_file", open_path_file)
    monkeypatch.setattr(nt_dirfd, "open_root_directory", open_root_directory)

    _bind_workspace_authority(tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload["target"] == str(tmp_path.resolve())
    assert path.is_file()


def test_authority_store_closes_temp_handle_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows raises WinError 32 if os.replace runs while the temp fd is open."""
    write_fds: list[int] = []

    def wrap_open(real_open):
        def tracking_open(path: Path, flags: int, mode: int = 0o600) -> int:
            descriptor = real_open(path, flags, mode)
            if flags & os.O_CREAT:
                write_fds.append(descriptor)
            return descriptor

        return tracking_open

    real_replace = os.replace

    def refuse_replace_if_temp_open(source: Path | str, destination: Path | str) -> None:
        for descriptor in write_fds:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            raise OSError(32, "file is being used by another process", str(source))
        real_replace(source, destination)

    monkeypatch.setattr(ledger, "_open_file_nofollow", wrap_open(ledger._open_file_nofollow))
    monkeypatch.setattr(authority_key, "_open_file_nofollow", wrap_open(authority_key._open_file_nofollow))
    monkeypatch.setattr(os, "replace", refuse_replace_if_temp_open)

    _bind_workspace_authority(tmp_path)
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None


def test_append_import_records_succeeds_on_current_platform(tmp_path: Path) -> None:
    imported = _append_manual_import(tmp_path, "windows inbox regression")
    inbox = helpers._imports_path(tmp_path)
    assert inbox.is_file()
    assert not inbox.is_symlink()
    assert "windows inbox regression" in inbox.read_text()
    assert ledger._read_imports(tmp_path)
    assert ledger._has_persisted_import_proof(imported, target=tmp_path)
