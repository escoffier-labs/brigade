"""Windows-capable import-inbox parent open and containment (#1022)."""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_ledger_has_no_bare_windows_open_flags() -> None:
    """Linux-visible guard: bare os.O_NOFOLLOW/O_DIRECTORY crash on Windows."""
    source = Path(ledger.__file__).read_text()
    for lineno, line in enumerate(source.splitlines(), 1):
        if "os.O_NOFOLLOW" in line and "getattr" not in line:
            raise AssertionError(f"bare os.O_NOFOLLOW at {lineno}: {line}")
        if "os.O_DIRECTORY" in line and "getattr" not in line:
            raise AssertionError(f"bare os.O_DIRECTORY at {lineno}: {line}")


@pytest.mark.skipif(sys.platform != "win32", reason="executes the Windows import proof and authority path")
def test_windows_import_proof_and_authority_store_round_trip(tmp_path: Path) -> None:
    """Run the full import write on Windows; do not grep a CI script."""
    record = ledger._sanitize_untrusted_import_record(
        {"text": "windows proof path", "kind": "task", "source": "manual", "metadata": {}},
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
    assert ledger._has_persisted_import_proof(imported[0], target=tmp_path)
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert ledger._read_imports(tmp_path)


def test_append_import_records_succeeds_on_current_platform(tmp_path: Path) -> None:
    record = ledger._sanitize_untrusted_import_record(
        {"text": "windows inbox regression", "kind": "task", "source": "manual", "metadata": {}},
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
    inbox = helpers._imports_path(tmp_path)
    assert inbox.is_file()
    assert not inbox.is_symlink()
    assert "windows inbox regression" in inbox.read_text()
    assert ledger._read_imports(tmp_path)
