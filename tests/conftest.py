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
    """Default to the bare-host baseline: no managed tool resolves.

    Managed-tool resolution goes through ``component_bins.resolve``, which
    reaches outside the test sandbox: the developer's real installed.json,
    legacy install dirs, and real PATH binaries. Left live, the suite behaves
    differently on a wired dev host than on CI, and can even execute real
    engine binaries against real archives. This fixture pins the bare-host
    condition while keeping the two sandboxed channels tests legitimately use:
    an explicit env override (GRAPHTRAIL_BIN, MISELEDGER_BIN, ...) resolves
    with full semantics, and a test-modified PATH resolves via plain
    ``shutil.which``. Tests that need a specific binary re-patch
    ``component_bins.resolve`` in their own body.

    `tests/test_proc.py` validates `proc.which` against real binaries, the
    opt-in adapter write probes must detect their real CLIs, and
    `tests/test_component_bins.py` tests the real resolution order. Those
    modules opt out.
    """
    if request.module.__name__.rsplit(".", 1)[-1] in {"test_proc", "test_agent_write_probes", "test_component_bins"}:
        return
    import os
    import shutil

    from brigade import component_bins, managed

    real_resolve = component_bins.resolve
    baseline_path = os.environ.get("PATH", "")
    baseline_entries = set(filter(None, baseline_path.split(os.pathsep)))

    def bare_host_resolve(name, *, env=None):
        environment = env if env is not None else os.environ
        if environment.get(component_bins.ENV_OVERRIDES.get(name, "")):
            return real_resolve(name, env=env)
        current_path = os.environ.get("PATH", "")
        if current_path != baseline_path:
            # Search only the entries the test itself introduced, so a test
            # that prepends a temp dir to the host PATH still cannot resolve
            # real host binaries.
            test_path = os.pathsep.join(
                entry for entry in current_path.split(os.pathsep) if entry and entry not in baseline_entries
            )
            return shutil.which(name, path=test_path) if test_path else None
        return None

    monkeypatch.setattr(component_bins, "resolve", bare_host_resolve)
    monkeypatch.setattr(managed.proc, "which", lambda cmd: None)
