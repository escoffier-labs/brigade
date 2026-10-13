"""Native skills state containment and functional Windows acceptance (#1213)."""

from __future__ import annotations

import json
import ctypes
import io
import os
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from brigade import dirfd, outcome_discovery, skills_cmd
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
@pytest.mark.parametrize("restricted", ["workspace", "root", "tree"])
def test_native_skills_read_commands_on_read_only_state(tmp_path, capsys, restricted):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)["proposal_id"]
    assert skills_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)["pack_id"]
    state = tmp_path / ".brigade"
    paths = [tmp_path] if restricted == "workspace" else [state]
    if restricted == "tree":
        paths = sorted(path for path in state.rglob("*") if path.is_dir())
    before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(_read_only_directory_acl(path))
        with pytest.raises(PermissionError):
            fd = dirfd.open_directory_nofollow(paths[0], writable=True)
            os.close(fd)
        assert "security-review" in outcome_discovery._skill_names_at(tmp_path)
        assert skills_cmd.search(target=tmp_path, query="security-review", json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 1
        assert skills_cmd.history(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 1
        assert skills_cmd.fleet_status(target=tmp_path, json_output=True) == 0
        fleet = json.loads(capsys.readouterr().out)
        copy = next(row for row in fleet["copies"] if row["skill_id"] == "security-review" and row["harness"] == "mcp")
        assert copy["status"] == "current"
        assert copy["drift"]["receipt_known"] is True
        assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["proposal_count"] == 1
        assert skills_cmd.inbox_show(target=tmp_path, proposal_id=proposal, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["skill_id"] == "security-review"
        assert skills_cmd.inbox_diff(target=tmp_path, proposal_id=proposal, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["skill_id"] == "security-review"
        assert skills_cmd.pack_list(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["pack_count"] == 1
        assert skills_cmd.pack_show(target=tmp_path, pack_id=pack, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["pack"]["pack_id"] == pack
        assert skills_cmd.lint(target=tmp_path, skill="registry:security-review", json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["valid"] is True
        assert skills_cmd.import_skill(target=tmp_path, source=source, force=True, json_output=True) == 2
        capsys.readouterr()
    assert {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()} == before
    assert skills_cmd.import_skill(target=tmp_path, source=source, force=True, json_output=True) == 0


@native_windows
def test_native_skills_read_commands_do_not_create_missing_state(tmp_path, capsys):
    with _read_only_directory_acl(tmp_path):
        assert skills_cmd.search(target=tmp_path, query="security-review", json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 0
        assert skills_cmd.history(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 0
        assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["proposal_count"] == 0
        assert skills_cmd.pack_list(target=tmp_path, json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["pack_count"] == 0
    assert not (tmp_path / ".brigade").exists()


@native_windows
@pytest.mark.parametrize("operation", ["import", "install"])
@pytest.mark.parametrize("parent_relative", [False, True], ids=["child", "parent"])
def test_native_skills_relative_source_import_and_install(tmp_path, monkeypatch, capsys, operation, parent_relative):
    source = _write_skill(tmp_path / "source")
    payload = b"relative source\x00\xff\n"
    (source / "scripts").mkdir()
    (source / "scripts" / "relative.txt").write_bytes(payload)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace if parent_relative else tmp_path)
    requested = Path(".." if parent_relative else ".") / source.relative_to(tmp_path)
    if operation == "import":
        result = skills_cmd.import_skill(target=workspace, source=requested, json_output=True)
        destination = workspace / ".brigade" / "skills" / "registry" / "security-review"
    else:
        result = skills_cmd.install(workspace=workspace, skill=str(requested), harness="mcp", json_output=True)
        destination = workspace / ".brigade" / "skills" / "mcp-resources" / "security-review"
    capsys.readouterr()
    assert result == 0
    assert (destination / "scripts" / "relative.txt").read_bytes() == payload


@native_windows
@pytest.mark.parametrize("reparse", [False, True], ids=["plain", "junction"])
@pytest.mark.parametrize("parent_relative", [False, True], ids=["child", "parent"])
def test_native_relative_source_collection_keeps_reparse_refusal(tmp_path, monkeypatch, reparse, parent_relative):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"relative source\x00\xff\n"
    (source / "data.txt").write_bytes(payload)
    selected = source
    if reparse:
        selected = tmp_path / "linked-source"
        with _reparse_replacement_setup(selected):
            _link_directory(selected, source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace if parent_relative else tmp_path)
    requested = Path(".." if parent_relative else ".") / selected.relative_to(tmp_path)
    if reparse:
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._collect_source_tree(requested)
    else:
        assert skills_cmd._collect_source_tree(requested) == ([], {("data.txt",): payload})


@native_windows
def test_native_skills_root_creation_refuses_swap_and_closes_handles(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = dirfd.mkdir_child
    original_open = dirfd.open_child_directory
    original_workspace_open = dirfd.open_directory_nofollow
    opened = []

    def open_workspace(*args, **kwargs):
        fd = original_workspace_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def open_child(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def swap_during_creation(parent, name, mode=0o700):
        with _reparse_replacement_setup(workspace):
            workspace.rename(tmp_path / "held-workspace")
            _link_directory(workspace, outside)
        return original_mkdir(parent, name, mode)

    monkeypatch.setattr(dirfd, "open_directory_nofollow", open_workspace)
    monkeypatch.setattr(dirfd, "open_child_directory", open_child)
    monkeypatch.setattr(dirfd, "mkdir_child", swap_during_creation)
    with pytest.raises(skills_cmd.SkillsStatePathError, match="workspace changed"):
        skills_cmd._open_state_root_descriptor(workspace)
    assert not (outside / ".brigade").exists()
    assert (tmp_path / "held-workspace" / ".brigade").is_dir()
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@native_windows
def test_native_skills_write_refuses_hardlink_and_closes_file(tmp_path, monkeypatch):
    state = tmp_path / ".brigade" / "skills"
    state.mkdir(parents=True)
    victim = tmp_path / "outside-victim.txt"
    victim.write_bytes(b"outside victim\n")
    os.link(victim, state / "receipt.json")
    original_open = dirfd.open_child_file
    opened = []

    def open_file(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    with skills_cmd._held_state_root(tmp_path) as anchor:
        monkeypatch.setattr(dirfd, "open_child_file", open_file)
        try:
            with pytest.raises(skills_cmd.SkillsStatePathError, match="single-link regular file"):
                skills_cmd._write_state_file(anchor, "skills", "receipt.json", data=b"replacement\n")
            assert victim.read_bytes() == b"outside victim\n"
            assert len(opened) == 1
            with pytest.raises(OSError):
                os.fstat(opened[0])
        finally:
            for fd in opened:
                try:
                    os.close(fd)
                except OSError:
                    pass


@native_windows
def test_native_skills_missing_read_chain_closes_handles(tmp_path, monkeypatch):
    (tmp_path / ".brigade" / "skills").mkdir(parents=True)
    original_open = dirfd.open_child_directory
    opened = []

    def open_child(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    with skills_cmd._held_state_root(tmp_path, writable=False) as anchor:
        monkeypatch.setattr(dirfd, "open_child_directory", open_child)
        assert skills_cmd._plain_subdirs_under_anchor(anchor, "skills", "missing") is None
        assert skills_cmd._read_state_file_bytes(anchor, "skills", "missing", "data.json") is None
        assert skills_cmd._state_file_exists(anchor, "skills", "missing", "data.json") is False
        assert skills_cmd._state_entry_kind(anchor, "skills", "missing", "entry") == "missing"
        assert len(opened) == 4
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)


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
@pytest.mark.parametrize(
    "nested, filename", [("nested", "資料.txt"), ("nested-😀", "helper-🚀.txt")], ids=["bmp", "nonbmp"]
)
def test_native_skills_import_install_history_inbox_pack_lifecycle(tmp_path, capsys, nested, filename):
    source = _write_skill(tmp_path / "source")
    (source / "scripts" / nested).mkdir(parents=True)
    (source / "scripts" / nested / filename).write_bytes(b"script data\n")
    expected_scripts = {("scripts", nested, filename): b"script data\n"}
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.lint(target=tmp_path, skill="registry:security-review", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    with skills_cmd._held_state_root(tmp_path, writable=False) as anchor:
        for location in ("registry", "mcp-resources"):
            dirs, files = skills_cmd._read_tree_from_anchor(anchor, "skills", location, "security-review")
            assert ("scripts", nested) in dirs
            assert {parts: data for parts, data in files.items() if parts[0] == "scripts"} == expected_scripts
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
    with skills_cmd._held_state_root(tmp_path, writable=False) as anchor:
        _dirs, files = skills_cmd._read_tree_from_anchor(anchor, "skills", "registry", "security-review")
        assert {parts: data for parts, data in files.items() if parts[0] == "scripts"} == expected_scripts
    assert skills_cmd.uninstall(workspace=tmp_path, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    assert not (tmp_path / ".brigade" / "skills" / "mcp-resources" / "security-review").exists()


@native_windows
@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_native_dirfd_nonbmp_child_lookup_and_enumeration(tmp_path, directory):
    name = "child-😀"
    child = tmp_path / name
    if directory:
        child.mkdir()
        (child / "data.txt").write_bytes(b"child data\n")
    else:
        child.write_bytes(b"child data\n")
    with ExitStack() as stack:
        parent = dirfd.open_directory_nofollow(tmp_path, writable=False)
        stack.callback(os.close, parent)
        assert dirfd.child_names(parent, 10) == [name]
        metadata = dirfd.stat_child(parent, name)
        assert stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if directory:
            held = dirfd.open_child_directory(parent, name, writable=False)
            stack.callback(os.close, held)
            assert dirfd.child_names(held, 10) == ["data.txt"]
            fd = dirfd.open_child_file(held, "data.txt", os.O_RDONLY | os.O_BINARY)
        else:
            fd = dirfd.open_child_file(parent, name, os.O_RDONLY | os.O_BINARY)
        stack.callback(os.close, fd)
        assert os.read(fd, 100) == b"child data\n"
    for closed in (parent, fd):
        with pytest.raises(OSError):
            os.fstat(closed)


@native_windows
def test_native_dirfd_rename_to_and_from_nonbmp_destination(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"rename data\n")
    destination = "renamed-😀🚀.txt"
    with ExitStack() as stack:
        parent = dirfd.open_directory_nofollow(tmp_path)
        stack.callback(os.close, parent)
        for source, target in (("source.txt", destination), (destination, "restored.txt")):
            dirfd.replace_children(parent, source, target)
            assert dirfd.child_names(parent, 10) == [target]
            assert not (tmp_path / source).exists()
            assert (tmp_path / target).read_bytes() == b"rename data\n"
            fd = dirfd.open_child_file(parent, target, os.O_RDONLY | os.O_BINARY)
            try:
                assert os.read(fd, 100) == b"rename data\n"
            finally:
                os.close(fd)
    with pytest.raises(OSError):
        os.fstat(parent)


@native_windows
def test_native_dirfd_rename_to_and_from_lone_surrogate_destination(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"rename\x00data\xff\n")
    destination = "renamed-\ud800.txt"
    with ExitStack() as stack:
        parent = dirfd.open_directory_nofollow(tmp_path)
        stack.callback(os.close, parent)
        for source, target in (("source.txt", destination), (destination, "restored.txt")):
            dirfd.replace_children(parent, source, target)
            assert not (tmp_path / source).exists()
            assert (tmp_path / target).read_bytes() == b"rename\x00data\xff\n"
            fd = dirfd.open_child_file(parent, target, os.O_RDONLY | os.O_BINARY)
            try:
                assert os.read(fd, 100) == b"rename\x00data\xff\n"
            finally:
                os.close(fd)
            with pytest.raises(OSError):
                os.fstat(fd)
    with pytest.raises(OSError):
        os.fstat(parent)


@native_windows
def test_native_dirfd_lone_surrogate_child_lookup(tmp_path):
    name = "child-\ud800.txt"
    (tmp_path / name).write_bytes(b"child data\n")
    parent = dirfd.open_directory_nofollow(tmp_path, writable=False)
    try:
        fd = dirfd.open_child_file(parent, name, os.O_RDONLY | os.O_BINARY)
        try:
            assert os.read(fd, 100) == b"child data\n"
        finally:
            os.close(fd)
    finally:
        os.close(parent)


@contextmanager
def _fleet_state_path_probes(monkeypatch, state):
    """Observe every pathname probe, including no-follow metadata reads."""
    probes = []

    def observe(operation, original):
        def call(path, *args, **kwargs):
            if not isinstance(path, int):
                candidate = Path(os.fsdecode(path))
                if candidate.is_relative_to(state):
                    probes.append((operation, str(candidate)))
            return original(path, *args, **kwargs)

        return call

    backend = (dirfd.posix_available(), dirfd.available())
    with monkeypatch.context() as patch:
        patch.setattr(io, "open", observe("open", io.open))
        wrappers = {}
        for operation in ("stat", "lstat", "scandir", "listdir", "access"):
            original = getattr(os, operation)
            wrappers[original] = observe(operation, original)
            patch.setattr(os, operation, wrappers[original])
        # Wrappers inherit the originals' capabilities so backend selection is unchanged.
        for capability in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks", "supports_effective_ids"):
            members = getattr(os, capability)
            inherited = {wrapper for original, wrapper in wrappers.items() if original in members}
            patch.setattr(os, capability, members | inherited)
        for operation in ("exists", "isdir", "isfile", "islink", "isjunction"):
            if hasattr(os.path, operation):
                patch.setattr(os.path, operation, observe(operation, getattr(os.path, operation)))
        assert (dirfd.posix_available(), dirfd.available()) == backend, "path probes must not change the backend"
        yield probes


@native_windows
@pytest.mark.parametrize(
    ("harness", "junction"),
    [("mcp", "root"), ("mcp", "parent"), ("mcp", "adapter"), ("custom", "parent"), ("custom", "adapter")],
)
def test_native_fleet_discovery_never_walks_state_junction(tmp_path, monkeypatch, capsys, harness, junction):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".brigade"
    relative = ("skills", "mcp-resources") if harness == "mcp" else ("projections", "custom")
    if harness == "custom":
        config = state / "skills" / "adapters.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"adapters": {"custom": {"install_path": ".brigade/projections/custom/{skill_id}"}}})
        )
    outside = tmp_path / "outside"
    remaining = relative if junction == "root" else relative[1:] if junction == "parent" else ()
    foreign = outside.joinpath(*remaining, "outside-fleet-only", "SKILL.md")
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"outside fleet content\n")
    link = state if junction == "root" else state / relative[0] if junction == "parent" else state.joinpath(*relative)
    link.parent.mkdir(parents=True, exist_ok=True)
    with _reparse_replacement_setup(link):
        _link_directory(link, outside)
    assert link.resolve() == outside.resolve(), "junction must redirect before fleet discovery"
    assert link.joinpath(*remaining, "outside-fleet-only", "SKILL.md").read_bytes() == b"outside fleet content\n"
    with _read_only_directory_acl(workspace), _fleet_state_path_probes(monkeypatch, state) as probes:
        assert skills_cmd.fleet_status(target=workspace, json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        keys = skills_cmd._fleet_copy_keys(workspace)
    assert payload["copies"] == []
    assert keys == []
    assert not probes, f"fleet state pathname metadata lookup through a live junction: {probes}"
    assert foreign.read_bytes() == b"outside fleet content\n"


@native_windows
@pytest.mark.parametrize("copy_kind", ["missing", "bundled", "registry-without-receipt"])
def test_native_fleet_discovery_uses_read_only_state_anchor(tmp_path, monkeypatch, capsys, copy_kind):
    state = tmp_path / ".brigade"
    present = copy_kind != "missing"
    skill_id = "security-review" if copy_kind == "registry-without-receipt" else "brigade-work"
    if copy_kind == "registry-without-receipt":
        source = _write_skill(tmp_path / "source")
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        capsys.readouterr()
    if present:
        assert skills_cmd.install(workspace=tmp_path, skill=skill_id, harness="mcp", json_output=True) == 0
        capsys.readouterr()
    if copy_kind == "registry-without-receipt":
        (state / "skills" / "installs" / f"{skill_id}-mcp.json").unlink()
    paths = [tmp_path]
    if present:
        paths += [state, *sorted(path for path in state.rglob("*") if path.is_dir())]
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(_read_only_directory_acl(path))
        with _fleet_state_path_probes(monkeypatch, state) as probes:
            assert skills_cmd.fleet_status(target=tmp_path, json_output=True) == 0
            payload = json.loads(capsys.readouterr().out)
    assert [(row["skill_id"], row["harness"], row["status"]) for row in payload["copies"]] == (
        [(skill_id, "mcp", "unknown" if copy_kind == "registry-without-receipt" else "current")] if present else []
    )
    assert not probes, f"fleet inspection must use held descriptors: {probes}"
    assert state.exists() is present


@native_windows
@pytest.mark.parametrize("selector", ["bare", "registry", "raw-path"])
def test_native_compatibility_reads_registry_and_state_copies_through_anchor(tmp_path, monkeypatch, capsys, selector):
    state = tmp_path / ".brigade"
    skill_id = "security-review"
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "source"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{skill_id}", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    requested = {
        "bare": skill_id,
        "registry": f"registry:{skill_id}",
        "raw-path": str(state / "skills" / "registry" / skill_id),
    }[selector]
    with ExitStack() as stack:
        for path in [tmp_path, state, *sorted(path for path in state.rglob("*") if path.is_dir())]:
            stack.enter_context(_read_only_directory_acl(path))
        with _fleet_state_path_probes(monkeypatch, state) as probes:
            assert skills_cmd.compatibility(target=tmp_path, skill=requested, json_output=True) == 0
            payload = json.loads(capsys.readouterr().out)
    mcp = next(row for row in payload["adapters"] if row["id"] == "mcp")
    assert payload["source"]["identity"] == f"registry://skills/{skill_id}"
    assert mcp["installed"] is True
    assert mcp["current_render_fingerprint"] == mcp["installed_render_fingerprint"]
    assert mcp["drift"]["overall"] == "current"
    assert not probes, f"compatibility must read state through held descriptors: {probes}"


@contextmanager
def _outcome_state_path_probes(monkeypatch, state):
    """Observe real pathname metadata calls, leaving descriptor calls alone."""
    probes = []

    def observe(operation, original):
        def call(path, *args, **kwargs):
            if not isinstance(path, int) and kwargs.get("follow_symlinks", True):
                candidate = Path(os.fsdecode(path))
                if candidate.is_relative_to(state):
                    probes.append((operation, str(candidate)))
            return original(path, *args, **kwargs)

        return call

    with monkeypatch.context() as patch:
        for operation in ("stat", "scandir", "access"):
            patch.setattr(os, operation, observe(operation, getattr(os, operation)))
        # Native Python also uses direct Windows metadata APIs for these helpers.
        for operation in ("exists", "isdir"):
            patch.setattr(os.path, operation, observe(operation, getattr(os.path, operation)))
        yield probes


@native_windows
@pytest.mark.parametrize("junction", ["root", "skills"])
@pytest.mark.parametrize("operation", ["names", "artifact"])
def test_native_outcome_discovery_never_walks_state_junction(tmp_path, monkeypatch, junction, operation):
    workspace = tmp_path / "workspace"
    harness = workspace / ".codex" / "skills" / "local-skill" / "SKILL.md"
    harness.parent.mkdir(parents=True)
    harness.write_bytes(b"local harness skill\n")
    state = workspace / ".brigade"
    outside = tmp_path / "outside"
    outside_skills = outside / "skills" if junction == "root" else outside
    foreign = outside_skills / "outside-only" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"outside content\n")
    link = state if junction == "root" else state / "skills"
    link.parent.mkdir(exist_ok=True)
    with _reparse_replacement_setup(link):
        _link_directory(link, outside)
    assert link.resolve() == outside.resolve(), "junction must redirect before discovery starts"
    with _read_only_directory_acl(workspace), _outcome_state_path_probes(monkeypatch, state) as probes:
        if operation == "names":
            assert outcome_discovery._skill_names_at(workspace) == {"local-skill"}
        else:
            assert outcome_discovery._artifact_content_path_at(workspace, "outside-only") is None
            assert outcome_discovery._artifact_content_path_at(workspace, "local-skill") == harness
    assert not probes, f"state pathname metadata lookup through a live junction: {probes}"
    assert foreign.read_bytes() == b"outside content\n"


@native_windows
@pytest.mark.parametrize("present", [False, True], ids=["missing", "registry"])
def test_native_outcome_discovery_uses_read_only_state_anchor(tmp_path, monkeypatch, present):
    state = tmp_path / ".brigade"
    skill = state / "skills" / "registry" / "registry-only" / "SKILL.md"
    if present:
        skill.parent.mkdir(parents=True)
        skill.write_bytes(b"registry skill\n")
    with _read_only_directory_acl(tmp_path), _outcome_state_path_probes(monkeypatch, state) as probes:
        assert outcome_discovery._skill_names_at(tmp_path) == ({"registry-only"} if present else set())
        assert outcome_discovery._artifact_content_path_at(tmp_path, "registry-only") == (skill if present else None)
    assert not probes, f"state inspection must use the held descriptor: {probes}"
    assert state.exists() is present


@native_windows
@pytest.mark.parametrize("destination", ["workspace-docs", "outside"])
def test_native_compatibility_reports_junctioned_install_copy_as_not_installed(
    tmp_path, monkeypatch, capsys, destination
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".brigade"
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(tmp_path / "source"), json_output=True) == 0
    capsys.readouterr()
    redirected = workspace / "docs" if destination == "workspace-docs" else tmp_path / "outside"
    redirected.mkdir()
    (redirected / "SKILL.md").write_bytes(b"redirected copy\n")
    link = state / "skills" / "mcp-resources" / "security-review"
    link.parent.mkdir(parents=True)
    with _reparse_replacement_setup(link):
        _link_directory(link, redirected)
    assert link.resolve() == redirected.resolve(), "junction must redirect before compatibility"
    with _read_only_directory_acl(workspace), _fleet_state_path_probes(monkeypatch, state) as probes:
        assert skills_cmd.compatibility(target=workspace, skill="registry:security-review", json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        result, is_error = skills_cmd._mcp_tool_call(
            workspace, "get_skill_compatibility", {"skill_id": "security-review"}
        )
    assert not probes, f"compatibility must not probe a refused install copy by pathname: {probes}"
    assert payload["source"]["identity"] == "registry://skills/security-review"
    for rows in (payload["adapters"], result["adapters"]):
        mcp = next(row for row in rows if row["id"] == "mcp")
        assert mcp["installed"] is False
        assert mcp["drift"] is None
        assert "installed copy refused by skills state anchoring" in mcp["blockers"]
    assert is_error is False
    assert (redirected / "SKILL.md").read_bytes() == b"redirected copy\n"


def test_bare_id_lint_never_rereads_registry_entry_by_pathname_after_path_disappears(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".brigade"
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(tmp_path / "source", "race-skill")) == 0
    cwd = tmp_path / "cwd"
    _write_skill(cwd, "race-skill")
    monkeypatch.chdir(cwd)
    real_load_skill = registry._load_skill
    kinds = []

    def load_then_rename(target, skill_or_path):
        loaded = real_load_skill(target, skill_or_path)
        kinds.append(loaded[2]["kind"])
        if len(kinds) == 1:
            (cwd / "race-skill").rename(cwd / "moved")
        return loaded

    monkeypatch.setattr(registry, "_load_skill", load_then_rename)
    with _fleet_state_path_probes(monkeypatch, state) as probes:
        payload = skills_cmd._lint_payload(workspace, "race-skill")
    assert kinds[:2] == ["path", "registry"], "the second classification must see the registry entry"
    assert not probes, f"registry entry must be linted from the anchored snapshot: {probes}"
    assert payload["valid"] is True, payload["errors"]
    assert payload["source"]["identity"] == "registry://skills/race-skill"
    assert payload["skill_dir"] == str(state / "skills" / "registry" / "race-skill")
