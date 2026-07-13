import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_target(tmp_path):
    """Return a clean temp directory to act as a workspace target."""
    return tmp_path / "ws"


@pytest.fixture(autouse=True)
def _extras_enabled_for_suite(monkeypatch, request):
    """The suite exercises the full command surface, so extras default on.

    The extras gate itself is tested in tests/test_extras_gate.py, which
    opts out by clearing BRIGADE_EXTRAS in its own fixture.
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_extras_gate":
        return
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch):
    """Point HERMES_HOME at a dedicated temp dir (outside the test's tmp_path so
    it never pollutes workspace assertions) so hermes-harness skill installs
    never touch the real ~/.hermes. The dir exists, so the 'Hermes is installed'
    gate passes for tests that exercise hermes installs; tests that need the
    absent-Hermes case re-point HERMES_HOME in their own body."""
    home = tmp_path_factory.mktemp("hermes_home")
    monkeypatch.setenv("HERMES_HOME", str(home))


@pytest.fixture(autouse=True)
def _no_managed_tools_on_path(monkeypatch, request):
    """Default to the bare-host baseline: no managed tool detected on PATH.

    The doctor folds in installed managed tools, but a dev host may have some
    of them globally installed. Neutralize detection so checks assert against
    the documented bare-`$HOME` condition. Tests that exercise installed tools
    re-patch `managed.proc.which` in their own body, which overrides this.

    `tests/test_proc.py` validates `proc.which` against real binaries, and the
    opt-in adapter write probes must detect their real CLIs. Both modules opt
    out because patching `managed.proc.which` patches the shared function.
    """
    if request.module.__name__.rsplit(".", 1)[-1] in {"test_proc", "test_agent_write_probes"}:
        return
    from brigade import managed

    monkeypatch.setattr(managed.proc, "which", lambda cmd: None)
