import os
from pathlib import Path

import pytest

from tests.support import PRIVATE_DIRECTORY_MODE, assert_private_mode


@pytest.fixture(scope="module")
def seen_isolation_directories():
    return set()


@pytest.mark.parametrize("_case", range(2))
def test_home_isolation_points_at_pytest_temp(tmp_path_factory, tmp_path, seen_isolation_directories, _case):
    base = tmp_path_factory.getbasetemp().resolve()
    home = Path.home().resolve()
    expanded = Path(os.path.expanduser("~")).resolve()
    assert expanded == home
    assert os.environ["BRIGADE_HOME"] == str(Path.home() / ".brigade")
    assert os.environ["BRIGADE_USER_DIR"] == os.environ["BRIGADE_HOME"]

    directories = {
        home,
        *(Path(os.environ[key]).resolve() for key in ("HERMES_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME")),
    }
    assert len(directories) == 4
    parents = {path.parent for path in directories}
    assert len(parents) == 1
    parent = parents.pop()
    assert parent.parent == base
    assert Path(os.environ["LOCALAPPDATA"]).resolve() in directories
    assert str(base) in os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep)
    for path in directories:
        assert path.is_dir()
        assert not path.is_relative_to(tmp_path)
        assert not tmp_path.is_relative_to(path)
        assert path not in seen_isolation_directories
    seen_isolation_directories.update(directories)
    if os.name != "nt":
        for path in (*directories, parent, home / ".brigade"):
            assert_private_mode(path, PRIVATE_DIRECTORY_MODE)


def test_no_direct_setenv_home_outside_helper():
    root = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name in {"_home.py", "test_conftest_home_isolation.py"}:
            continue
        text = path.read_text()
        if 'setenv("HOME"' in text or "setenv('HOME'" in text:
            offenders.append(path.name)
    assert not offenders, f"use tests._home.set_home instead of setenv HOME in: {offenders}"
