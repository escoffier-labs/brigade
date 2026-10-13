"""Native skills state containment and functional Windows acceptance (#1213)."""

from __future__ import annotations

import json
import ctypes
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from brigade import dirfd, skills_cmd
from brigade.skills_cmd import registry
from tests.test_skills_cmd import _write_skill
from tests.test_work_import_inbox_windows import _link_directory


native_windows = pytest.mark.skipif(sys.platform != "win32", reason="native Windows handle acceptance")


@contextmanager
def _reparse_replacement_setup(link):
    try:
        yield
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT, (
            "replacement must be a reparse point"
        )
    except OSError as exc:
        pytest.fail(f"reparse replacement setup failed (native link rights required): {exc}")


@contextmanager
def _read_only_directory_acl(path):
    """Deny directory mutation on a temp fixture, restoring its original DACL."""
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.GetFileSecurityW.restype = wintypes.BOOL
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    api.SetFileSecurityW.restype = wintypes.BOOL
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    size = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    assert size.value, "could not size fixture DACL"
    original = ctypes.create_string_buffer(size.value)
    assert api.GetFileSecurityW(str(path), 4, original, size, ctypes.byref(size)), ctypes.WinError(
        ctypes.get_last_error()
    )
    restricted = ctypes.c_void_p()
    # Everyone: deny ADD_FILE, ADD_SUBDIRECTORY, DELETE_CHILD; allow other access.
    assert api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:(D;;0x46;;;WD)(A;;FA;;;WD)", 1, ctypes.byref(restricted), None
    ), ctypes.WinError(ctypes.get_last_error())
    try:
        assert api.SetFileSecurityW(str(path), 4, restricted), ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            assert api.SetFileSecurityW(str(path), 4, original), ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(restricted)


@native_windows
@pytest.mark.parametrize("operation", ["collect", "plain"])
def test_native_skills_read_only_source_acl(tmp_path, operation):
    source = tmp_path / "source"
    child = source / "sub"
    child.mkdir(parents=True)
    (child / "data.txt").write_bytes(b"read-only source\n")
    with _read_only_directory_acl(child):
        with pytest.raises(PermissionError):
            fd = dirfd.open_directory_nofollow(child, writable=True)
            os.close(fd)
        if operation == "collect":
            assert skills_cmd._collect_source_tree(source) == ([("sub",)], {("sub", "data.txt"): b"read-only source\n"})
        else:
            fd = dirfd.open_directory_nofollow(source, writable=False)
            try:
                assert skills_cmd._dir_fd_subtree_plain(fd)
            finally:
                os.close(fd)


@pytest.mark.parametrize("operation", ["read", "tree", "list"])
def test_skills_state_refuses_unavailable_backend(tmp_path, monkeypatch, operation):
    state = tmp_path / ".brigade" / "skills"
    state.mkdir(parents=True)
    (state / "history.jsonl").write_bytes(b"private state\n")
    monkeypatch.setattr(registry, "_HAS_DESCRIPTOR_ANCHOR", False)
    anchor = registry._StateRootAnchor(tmp_path)
    with pytest.raises(skills_cmd.SkillsStatePathError, match="descriptor anchoring"):
        if operation == "read":
            skills_cmd._read_state_file_bytes(anchor, "skills", "history.jsonl")
        elif operation == "tree":
            skills_cmd._read_tree_from_anchor(anchor, "skills")
        else:
            skills_cmd._plain_subdirs_under_anchor(anchor, "skills")


@native_windows
@pytest.mark.parametrize("swap", ["parent", "file", "enumeration"])
def test_native_reparse_acceptance_fails_when_swap_setup_fails(tmp_path, monkeypatch, swap):
    def fail_setup(*args, **kwargs):
        raise OSError("injected reparse setup failure")

    if swap == "file":
        monkeypatch.setattr(Path, "symlink_to", fail_setup)
    else:
        monkeypatch.setitem(globals(), "_link_directory", fail_setup)
    with pytest.raises(pytest.fail.Exception, match="reparse replacement setup failed"):
        if swap == "enumeration":
            test_native_skills_enumeration_refuses_child_junction_swap(tmp_path, monkeypatch)
        else:
            test_native_skills_read_never_follows_reparse_replacement(tmp_path, monkeypatch, swap)


@native_windows
@pytest.mark.parametrize("swap", ["parent", "file"])
def test_native_skills_read_never_follows_reparse_replacement(tmp_path, monkeypatch, swap):
    parent = tmp_path / ".brigade" / "skills"
    parent.mkdir(parents=True)
    victim = parent / "history.jsonl"
    victim.write_bytes(b"held tree\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / victim.name).write_bytes(b"outside secret\n")
    fired = False

    def replace():
        nonlocal fired
        if fired:
            return
        with _reparse_replacement_setup(parent if swap == "parent" else victim):
            if swap == "parent":
                parent.rename(parent.with_name("held-skills"))
                _link_directory(parent, outside)
            else:
                victim.unlink()
                victim.symlink_to(outside / victim.name)
        fired = True

    original_open = dirfd.open_child_file
    original_read = Path.read_bytes

    def open_after_swap(fd, name, flags, mode=0o600):
        if name == victim.name:
            replace()
        return original_open(fd, name, flags, mode)

    def read_after_swap(path):
        if path == victim:
            replace()
        return original_read(path)

    with skills_cmd._held_state_root(tmp_path) as anchor:
        monkeypatch.setattr(dirfd, "open_child_file", open_after_swap)
        monkeypatch.setattr(Path, "read_bytes", read_after_swap)
        try:
            result = skills_cmd._read_state_file_bytes(anchor, "skills", victim.name)
        except skills_cmd.SkillsStatePathError:
            result = None
        assert fired, "replacement must occur at the final read boundary"
        assert result in {None, b"held tree\n"}


@native_windows
def test_native_skills_enumeration_refuses_child_junction_swap(tmp_path, monkeypatch):
    base = tmp_path / ".brigade" / "skills"
    child = base / "entry"
    child.mkdir(parents=True)
    (child / "SKILL.md").write_bytes(b"held tree")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"outside secret")
    original_names = dirfd.child_names
    fired = False

    def names_after_swap(fd, limit):
        nonlocal fired
        names = original_names(fd, limit)
        if "entry" in names and not fired:
            with _reparse_replacement_setup(child):
                child.rename(base / "held-entry")
                _link_directory(child, outside)
            fired = True
        return names

    with skills_cmd._held_state_root(tmp_path) as anchor:
        monkeypatch.setattr(dirfd, "child_names", names_after_swap)
        try:
            _dirs, files = skills_cmd._read_tree_from_anchor(anchor, "skills")
        except skills_cmd.SkillsStatePathError:
            files = {}
        assert fired, "enumeration must use the held directory"
        assert b"outside secret" not in files.values()


@native_windows
def test_native_skills_long_unicode_tree_can_read_replace_append_and_remove(tmp_path):
    workspace = tmp_path.joinpath(*(["unicode-資料-" + "a" * 35] * 4))
    workspace.mkdir(parents=True)
    assert len(str(workspace)) > 260
    with skills_cmd._held_state_root(workspace) as anchor:
        parts = ("skills", "資料", "履歴.jsonl")
        skills_cmd._write_state_file(anchor, *parts, data=b"first\n")
        skills_cmd._write_state_file(anchor, *parts, data=b"replacement\n")
        skills_cmd._append_state_line(anchor, *parts, data=b"append\n")
        assert skills_cmd._read_state_file_bytes(anchor, *parts) == b"replacement\nappend\n"
        assert skills_cmd._plain_subdirs_under_anchor(anchor, "skills") == ["資料"]
        assert skills_cmd._read_tree_from_anchor(anchor, "skills")[1] == {
            ("資料", "履歴.jsonl"): b"replacement\nappend\n"
        }
        skills_cmd._remove_tree_in_anchor(anchor, "skills", "資料")
        assert skills_cmd._state_entry_kind(anchor, "skills", "資料") == "missing"


@native_windows
def test_native_skills_import_install_history_inbox_pack_lifecycle(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    (source / "scripts" / "nested").mkdir(parents=True)
    (source / "scripts" / "nested" / "資料.txt").write_bytes(b"script data\n")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.history(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)["proposal_id"]
    assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["proposal_count"] == 1
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal, force=True, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)["pack_id"]
    assert skills_cmd.pack_archive(target=tmp_path, pack_id=pack, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.pack_show(target=tmp_path, pack_id=pack, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["pack"]["archived"] is True
    archive = tmp_path / ".brigade" / "skills" / "packs-archive" / pack
    assert skills_cmd.pack_import(target=tmp_path, pack=archive, force=True, json_output=True) == 0
    capsys.readouterr()
    assert (
        tmp_path / ".brigade" / "skills" / "registry" / "security-review" / "scripts" / "nested" / "資料.txt"
    ).read_bytes() == b"script data\n"
    assert skills_cmd.uninstall(workspace=tmp_path, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    assert not (tmp_path / ".brigade" / "skills" / "mcp-resources" / "security-review").exists()
