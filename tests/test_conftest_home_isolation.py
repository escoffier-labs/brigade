import os
from pathlib import Path


def test_home_isolation_points_at_pytest_temp(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    home = Path.home().resolve()
    expanded = Path(os.path.expanduser("~")).resolve()
    assert home.is_relative_to(base)
    assert expanded.is_relative_to(base)
    assert os.environ["BRIGADE_HOME"] == str(Path.home() / ".brigade")


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
