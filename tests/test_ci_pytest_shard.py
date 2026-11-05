import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ci_pytest_shard

ROOT = Path(__file__).resolve().parents[1]


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


def test_main_loads_selector_as_plugin_and_forwards_pytest_arguments(monkeypatch, tmp_path):
    recorded = {}

    def fake_main(args):
        recorded["args"] = args
        return 17

    monkeypatch.setattr(ci_pytest_shard.pytest, "main", fake_main)
    result_json = tmp_path / "shard.json"
    result = ci_pytest_shard.main(
        ["--shard-index", "2", "--shard-count", "4", "--result-json", str(result_json), "--", "-q", "tests/x.py"]
    )
    assert result == 17
    assert recorded["args"] == [
        "-p",
        "ci_pytest_shard",
        "--ci-shard-index=2",
        "--ci-shard-count=4",
        f"--ci-shard-result-json={result_json.resolve()}",
        "-q",
        "tests/x.py",
    ]


@pytest.mark.parametrize("workers", [None, "2"])
def test_shard_runs_exactly_its_selection_serially_and_under_xdist(tmp_path, workers):
    if workers is not None:
        pytest.importorskip("xdist")
    suite = tmp_path / "test_probe.py"
    suite.write_text(
        "import os, pathlib, pytest\n"
        "@pytest.mark.parametrize('case', range(40))\n"
        "def test_case(case, request):\n"
        "    ran = pathlib.Path(os.environ['SHARD_PROBE_DIR'])\n"
        "    (ran / str(case)).write_text(request.node.nodeid)\n"
    )
    ran = tmp_path / "ran"
    ran.mkdir()
    result_json = tmp_path / "shard.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/ci_pytest_shard.py"),
        "--shard-index",
        "1",
        "--shard-count",
        "4",
        "--result-json",
        str(result_json),
        "--",
        "-q",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(tmp_path),
        "-c",
        str(tmp_path / "pytest.ini"),
        str(suite),
    ]
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    if workers is not None:
        command[command.index("--") + 1 : command.index("--") + 1] = ["-n", workers]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(os.environ, SHARD_PROBE_DIR=str(ran)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodeids = [f"test_probe.py::test_case[{case}]" for case in range(40)]
    expected = {nodeid for nodeid in nodeids if ci_pytest_shard.shard_for_nodeid(nodeid, 4) == 1}
    executed = {path.read_text() for path in ran.iterdir()}
    assert executed == expected
    markers = [line for line in result.stdout.splitlines() if line.startswith("CI shard ")]
    assert markers == [f"CI shard 2/4: selected {len(expected)} of 40 tests"]
    summary = result.stdout.splitlines()[-1]
    assert result.stdout.index(markers[0]) < result.stdout.index(summary)
    recorded = json.loads(result_json.read_text())
    assert recorded["collected"] == 40
    assert recorded["selected"] == len(expected)
    assert recorded["counts"]["passed"] == len(expected)


def test_empty_shard_is_successful_after_nonempty_collection():
    plugin = ci_pytest_shard.ShardPlugin(index=0, count=4)
    plugin.collected = 3
    plugin.selected = 0
    session = SimpleNamespace(exitstatus=pytest.ExitCode.NO_TESTS_COLLECTED, config=SimpleNamespace())
    plugin.pytest_sessionfinish(session, pytest.ExitCode.NO_TESTS_COLLECTED)
    assert session.exitstatus == pytest.ExitCode.OK
