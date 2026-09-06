import os
from pathlib import Path


def test_home_isolation_points_at_pytest_temp(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    home = Path.home().resolve()
    expanded = Path(os.path.expanduser("~")).resolve()
    assert home.is_relative_to(base)
    assert expanded.is_relative_to(base)
    assert os.environ["BRIGADE_HOME"] == str(Path.home() / ".brigade")
