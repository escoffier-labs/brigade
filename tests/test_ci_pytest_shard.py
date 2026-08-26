from types import SimpleNamespace

import pytest

from scripts import ci_pytest_shard


def test_shard_assignment_is_stable_disjoint_and_exhaustive():
    nodeids = [f"tests/test_{number}.py::test_{number}" for number in range(200)]
    first = [
        [nodeid for nodeid in nodeids if ci_pytest_shard.shard_for_nodeid(nodeid, 4) == index] for index in range(4)
    ]
    second = [
        [nodeid for nodeid in nodeids if ci_pytest_shard.shard_for_nodeid(nodeid, 4) == index] for index in range(4)
    ]
    flattened = [nodeid for shard in first for nodeid in shard]
    assert first == second
    assert sorted(flattened) == sorted(nodeids)
    assert len(flattened) == len(set(flattened))
    assert all(first)


@pytest.mark.parametrize(("index", "count"), [(-1, 4), (4, 4), (0, 0)])
def test_shard_plugin_rejects_invalid_coordinates(index, count):
    with pytest.raises(ValueError, match="shard index"):
        ci_pytest_shard.ShardPlugin(index=index, count=count)


def test_main_forwards_pytest_arguments(monkeypatch):
    recorded = {}

    def fake_main(args, plugins):
        recorded["args"] = args
        recorded["plugin"] = plugins[0]
        return 17

    monkeypatch.setattr(ci_pytest_shard.pytest, "main", fake_main)
    result = ci_pytest_shard.main(["--shard-index", "2", "--shard-count", "4", "--", "-q", "tests/test_ci_workflow.py"])
    assert result == 17
    assert recorded["args"] == ["-q", "tests/test_ci_workflow.py"]
    assert recorded["plugin"].index == 2
    assert recorded["plugin"].count == 4


def test_empty_shard_is_successful_after_nonempty_collection():
    plugin = ci_pytest_shard.ShardPlugin(index=0, count=4)
    plugin.collected = 3
    plugin.selected = 0
    session = SimpleNamespace(exitstatus=pytest.ExitCode.NO_TESTS_COLLECTED)
    plugin.pytest_sessionfinish(session, pytest.ExitCode.NO_TESTS_COLLECTED)
    assert session.exitstatus == pytest.ExitCode.OK
