"""Isolation checks retained for reading retired fleet instances."""

import os

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.retained_paths import validate_disjoint_state_paths


@pytest.mark.parametrize(
    "runtime,ledger",
    [
        ("/var/lib/a", "/var/lib/a/ledger.json"),
        ("/var/lib/../a", "/var/lib/b"),
        ("var/lib/a", "/var/lib/b"),
    ],
    ids=["overlap", "parent-traversal", "relative"],
)
def test_disjoint_paths_reject_overlap_dots_and_relative(runtime, ledger):
    root = "C:" if os.name == "nt" else ""
    runtime = root + runtime if runtime.startswith("/") else runtime
    with pytest.raises(FleetError) as caught:
        validate_disjoint_state_paths(runtime, root + ledger, root + "/var/lib/c", root + "/var/lib/d")
    assert caught.value.code == "invalid_request"
    assert runtime not in str(caught.value)


def test_absolute_reference_accepts_posix_and_drive_roots_and_rejects_unc():
    from brigade.grokbot_fleet.retained_paths import validate_absolute_reference as validate_ref

    # A drive-less root such as "/var/lib/state" carries no drive on Windows and
    # resolves against the current drive, so it can alias C:\var\lib\state.
    if os.name == "nt":
        with pytest.raises(FleetError):
            validate_ref("/var/lib/state")
        for accepted in (r"C:\state\dir", "C:/state/dir"):
            assert isinstance(validate_ref(accepted), str)
    else:
        assert isinstance(validate_ref("/var/lib/state"), str)
        for drive_path in (r"C:\state\dir", "C:/state/dir"):
            with pytest.raises(FleetError):
                validate_ref(drive_path)
    for rejected in (
        r"\\server\share\path",
        r"\\.\pipe\x",
        r"\\?\C:\path",
        "//server/share",
        r"/\server/share",
        r"\/server/share",
        "relative/path",
        "/var/lib/../escape",
        r"C:\state\..\escape",
    ):
        with pytest.raises(FleetError):
            validate_ref(rejected)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (r"C:\a", r"C:\a\b"),
        (r"C:\a\b", r"C:\a"),
        (r"C:\Brigade\State", r"c:\brigade"),
        (r"c:\brigade", r"C:\Brigade\State"),
    ],
    ids=["backslash-parent", "backslash-child", "case-differs-child", "case-differs-parent"],
)
def test_paths_overlap_on_windows_nests_on_backslashes_and_ignores_case(monkeypatch, left, right):
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import retained_paths as runtime_mod

    with monkeypatch.context() as patched:
        patched.setattr(runtime_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        assert runtime_mod.paths_overlap(left, right) is True


def test_paths_overlap_treats_backslash_as_literal_on_posix():
    from brigade.grokbot_fleet.retained_paths import paths_overlap

    if os.name != "posix":
        pytest.skip("backslash is a literal filename character only on POSIX")
    assert paths_overlap("/a/b\\c", "/a/b") is False


def test_paths_overlap_handles_root_operand_and_rejects_trailing_alias(monkeypatch):
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import retained_paths as runtime_mod
    from brigade.grokbot_fleet import retained_paths as lifecycle_mod

    assert runtime_mod.paths_overlap("/", "/var/x") is True
    assert runtime_mod.paths_overlap("/var/x", "/") is True
    with monkeypatch.context() as patched:
        patched.setattr(runtime_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        patched.setattr(lifecycle_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        assert runtime_mod.paths_overlap("C:\\", r"C:\foo") is True
        assert runtime_mod.paths_overlap(r"C:\foo", "C:\\") is True
        assert runtime_mod.paths_overlap(r"C:\a", r"C:\a\b") is True
        assert isinstance(lifecycle_mod.validate_absolute_reference(r"C:\state"), str)
        for rejected in (r"C:\state.", r"C:\state "):
            with pytest.raises(FleetError):
                lifecycle_mod.validate_absolute_reference(rejected)
    root = "C:" if os.name == "nt" else ""
    for rejected in (root + "/var/lib/state.", root + "/var/lib/state "):
        with pytest.raises(FleetError):
            lifecycle_mod.validate_absolute_reference(rejected)
    assert isinstance(lifecycle_mod.validate_absolute_reference(root + "/var/lib/my.dir/state"), str)


def test_disjoint_paths_reject_backslash_nested_drive_paths(monkeypatch):
    import ntpath
    from types import SimpleNamespace
    from brigade.grokbot_fleet import retained_paths

    monkeypatch.setattr(retained_paths, "os", SimpleNamespace(name="nt", path=ntpath))
    with pytest.raises(FleetError):
        validate_disjoint_state_paths(
            r"C:\b\state\runtime.json",
            r"C:\b\other\ledger.json",
            r"C:\b\state",
            r"C:\b\approvals",
        )
