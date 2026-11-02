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


def test_assign_shards_balances_timed_files_longest_first():
    durations = {"tests/a.py": 10.0, "tests/b.py": 6.0, "tests/c.py": 5.0, "tests/d.py": 4.0, "tests/e.py": 1.0}
    nodeids = [f"{name}::test_{case}" for name in durations for case in range(3)]
    owners = dict(zip(nodeids, ci_pytest_shard.assign_shards(nodeids, 2, durations), strict=True))
    by_file = {nodeid.split("::")[0]: owner for nodeid, owner in owners.items()}
    # Longest first onto the lighter shard: a, b, c, d, e -> 0, 1, 1, 0, 1 (14 s vs 12 s).
    assert by_file == {"tests/a.py": 0, "tests/b.py": 1, "tests/c.py": 1, "tests/d.py": 0, "tests/e.py": 1}
    assert all(owners[nodeid] == by_file[nodeid.split("::")[0]] for nodeid in nodeids)


def test_assign_shards_hashes_untimed_tests_and_ignores_input_order():
    durations = {"tests/timed.py": 3.0}
    nodeids = ["tests/timed.py::test_one", *[f"tests/new.py::test_{case}" for case in range(20)]]
    owners = dict(zip(nodeids, ci_pytest_shard.assign_shards(nodeids, 4, durations), strict=True))
    for nodeid in nodeids[1:]:
        assert owners[nodeid] == ci_pytest_shard.shard_for_nodeid(nodeid, 4)
    reordered = list(reversed(nodeids))
    assert dict(zip(reordered, ci_pytest_shard.assign_shards(reordered, 4, durations), strict=True)) == owners


def test_assign_shards_balances_only_files_present_in_the_collection():
    durations = {"tests/big.py": 100.0, "tests/x.py": 2.0, "tests/y.py": 1.0}
    nodeids = ["tests/x.py::test_x", "tests/y.py::test_y"]
    # The absent big file must not occupy a shard and push both small files together.
    assert sorted(ci_pytest_shard.assign_shards(nodeids, 2, durations)) == [0, 1]


def test_load_file_durations_ignores_missing_malformed_and_invalid_entries(tmp_path):
    assert ci_pytest_shard.load_file_durations(None) == {}
    assert ci_pytest_shard.load_file_durations(tmp_path / "absent.json") == {}
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    assert ci_pytest_shard.load_file_durations(malformed) == {}
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        json.dumps(
            {"version": 1, "files": {"tests/a.py": 1.5, "tests/b.py": -1, "tests/c.py": "2", "tests/d.py": True}}
        ),
        encoding="utf-8",
    )
    assert ci_pytest_shard.load_file_durations(mixed) == {"tests/a.py": 1.5}


def test_committed_durations_cover_the_suite():
    durations = ci_pytest_shard.load_file_durations(ci_pytest_shard.DURATIONS_PATH)
    assert durations
    assert all(name.startswith("tests/") and name.endswith(".py") for name in durations)


def test_shard_balances_timed_files_under_xdist(tmp_path):
    pytest.importorskip("xdist")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_heavy", "test_light_a", "test_light_b"):
        (tests / f"{name}.py").write_text(
            "import os, pathlib, pytest\n"
            "@pytest.mark.parametrize('case', range(5))\n"
            "def test_case(case, request):\n"
            "    ran = pathlib.Path(os.environ['SHARD_PROBE_DIR'])\n"
            "    (ran / request.node.nodeid.replace('/', '_')).write_text(request.node.nodeid)\n"
        )
    durations = tmp_path / "durations.json"
    durations.write_text(
        json.dumps(
            {"version": 1, "files": {"tests/test_heavy.py": 9, "tests/test_light_a.py": 5, "tests/test_light_b.py": 4}}
        )
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    executed = []
    for index in range(2):
        ran = tmp_path / f"ran-{index}"
        ran.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/ci_pytest_shard.py"),
                "--shard-index",
                str(index),
                "--shard-count",
                "2",
                "--",
                "-q",
                "-n",
                "2",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(tmp_path),
                "-c",
                str(tmp_path / "pytest.ini"),
                f"--ci-shard-durations={durations}",
                "tests",
            ],
            cwd=tmp_path,
            env=dict(os.environ, SHARD_PROBE_DIR=str(ran)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        executed.append({path.read_text().split("::")[0] for path in ran.iterdir()})
    # 9 s alone against 5 s + 4 s.
    assert executed == [{"tests/test_heavy.py"}, {"tests/test_light_a.py", "tests/test_light_b.py"}]
