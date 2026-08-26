# CI Test Sharding Implementation Plan

**Goal:** Reduce Python CI wall time by running four deterministic pytest shards per supported Python version while preserving coverage and protected branch checks.

**Architecture:** A pytest launcher partitions collected node IDs with a stable SHA-256 hash and deselects items outside the requested shard. GitHub Actions fans this across Python 3.10, 3.11, and 3.12, combines four Python 3.12 coverage artifacts, then emits the three existing protected check names from compatibility gate jobs. Top-level concurrency cancels superseded runs, and setup-python caches pip downloads.

**Key tech:** Python 3.10+, pytest plugin hooks, GitHub Actions matrix jobs, pytest-cov data files, actionlint, Brigade receipts.

Execute the checkboxes in order. Keep the first focused run red, then commit the completed slice.

## File map

- `.gitignore`: allow the repository-owned CI launcher under `scripts/`.
- `scripts/ci_pytest_shard.py`: validate shard coordinates, partition collected items, and invoke pytest.
- `tests/test_ci_pytest_shard.py`: stable assignment, CLI forwarding, and empty-shard tests.
- `tests/test_ci_workflow.py`: workflow topology and protected-check contract.
- `.github/workflows/ci.yml`: shard matrix, coverage combination, caching, cancellation, and compatibility gates.
- `docs/superpowers/plans/2026-08-25-ci-test-sharding.md`: live execution checklist.

### Task 1: Implement deterministic pytest sharding

**Files:**
- Modify: `.gitignore`
- Create: `scripts/ci_pytest_shard.py`
- Create: `tests/test_ci_pytest_shard.py`

- [x] Write `tests/test_ci_pytest_shard.py` with four tests:

```python
from types import SimpleNamespace

import pytest

from scripts import ci_pytest_shard


def test_shard_assignment_is_stable_disjoint_and_exhaustive():
    nodeids = [f"tests/test_{number}.py::test_{number}" for number in range(200)]
    first = [[nodeid for nodeid in nodeids if ci_pytest_shard.shard_for_nodeid(nodeid, 4) == index] for index in range(4)]
    second = [[nodeid for nodeid in nodeids if ci_pytest_shard.shard_for_nodeid(nodeid, 4) == index] for index in range(4)]
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
```

- [x] Run RED: `brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_ci_pytest_shard.py"]' --capture brigade-work --no-reuse`.

Expected: collection fails because `scripts/ci_pytest_shard.py` does not exist.

- [x] Add `!/scripts/ci_pytest_shard.py` to `.gitignore`. Create `scripts/ci_pytest_shard.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence

import pytest


def shard_for_nodeid(nodeid: str, count: int) -> int:
    if count <= 0:
        raise ValueError("shard index requires a positive shard count")
    return int(hashlib.sha256(nodeid.encode("utf-8")).hexdigest(), 16) % count


class ShardPlugin:
    def __init__(self, *, index: int, count: int) -> None:
        if count <= 0 or index < 0 or index >= count:
            raise ValueError(f"shard index must satisfy 0 <= index < count (got index={index}, count={count})")
        self.index = index
        self.count = count
        self.collected = 0
        self.selected = 0

    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.collected = len(items)
        selected = [item for item in items if shard_for_nodeid(item.nodeid, self.count) == self.index]
        deselected = [item for item in items if shard_for_nodeid(item.nodeid, self.count) != self.index]
        self.selected = len(selected)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"CI shard {self.index + 1}/{self.count}: selected {self.selected} of {self.collected} tests")

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.collected > 0 and self.selected == 0 and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
            session.exitstatus = pytest.ExitCode.OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--" in arguments:
        separator = arguments.index("--")
        launcher_args = arguments[:separator]
        pytest_args = arguments[separator + 1:]
    else:
        launcher_args = arguments
        pytest_args = []
    parser = argparse.ArgumentParser(description="Run one deterministic pytest shard")
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parsed = parser.parse_args(launcher_args)
    try:
        plugin = ShardPlugin(index=parsed.shard_index, count=parsed.shard_count)
    except ValueError as exc:
        parser.error(str(exc))
    return int(pytest.main(pytest_args, plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] Run GREEN: `brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_ci_pytest_shard.py"]' --capture brigade-work --no-reuse`.

Expected: all launcher tests pass.

### Task 2: Replace the serial CI test matrix

**Files:**
- Modify: `tests/test_ci_workflow.py`
- Modify: `.github/workflows/ci.yml`

- [x] Add workflow tests that assert the exact design contract:

```python
def test_ci_workflow_cancels_superseded_runs_for_the_same_pr_or_ref():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    header = text[: text.index("jobs:")]
    assert "group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}" in header
    assert "cancel-in-progress: true" in header


def test_ci_workflow_runs_four_shards_on_every_supported_python():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    shards = _workflow_job_section(text, "test-shards")
    assert 'name: test shard (${{ matrix.python }}, ${{ matrix.shard }})' in shards
    assert 'python: ["3.10", "3.11", "3.12"]' in shards
    assert "shard: [0, 1, 2, 3]" in shards
    assert "fail-fast: false" in shards
    assert "python scripts/ci_pytest_shard.py" in shards
    assert '--shard-index "${{ matrix.shard }}" --shard-count 4' in shards


def test_ci_workflow_combines_python312_coverage_and_preserves_required_checks():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    shards = _workflow_job_section(text, "test-shards")
    coverage = _workflow_job_section(text, "coverage")
    protected = _workflow_job_section(text, "test")
    assert "COVERAGE_FILE: .coverage.${{ matrix.python }}.${{ matrix.shard }}" in shards
    assert "--cov=brigade --cov-report=" in shards
    assert "uses: actions/upload-artifact@v4" in shards
    assert "include-hidden-files: true" in shards
    assert "uses: actions/download-artifact@v5" in coverage
    assert "pattern: coverage-*" in coverage
    assert "python -m coverage combine .coverage-data" in coverage
    assert "python -m coverage report --fail-under=78" in coverage
    assert 'name: test (${{ matrix.python }})' in protected
    assert "needs: [test-shards, coverage]" in protected
    assert 'python: ["3.10", "3.11", "3.12"]' in protected
    assert "if: ${{ always() }}" in protected
    assert "SHARDS_RESULT: ${{ needs.test-shards.result }}" in protected
    assert "COVERAGE_RESULT: ${{ needs.coverage.result }}" in protected


def test_ci_workflow_caches_pip_for_jobs_that_install_dev_extras():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    for job_name in ("lint", "test-shards", "coverage", "component-manifest-provenance", "repo-metadata"):
        section = _workflow_job_section(text, job_name)
        assert "cache: pip" in section
        assert "cache-dependency-path: pyproject.toml" in section
```

- [x] Run RED: `brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_ci_workflow.py"]' --capture brigade-work --no-reuse`.

Expected: the four new workflow tests fail against the serial job.

- [x] Add top-level workflow concurrency exactly as specified in `docs/superpowers/specs/2026-08-25-ci-test-sharding-design.md`.
- [x] Add setup-python pip caching to `lint`, `component-manifest-provenance`, and `repo-metadata`.
- [x] Replace `test` with `test-shards`: a false-fail-fast matrix over Python `["3.10", "3.11", "3.12"]` and shard `[0, 1, 2, 3]`; install dev extras; invoke the launcher; set `COVERAGE_FILE=.coverage.<python>.<shard>`; add coverage flags only on Python 3.12; upload each Python 3.12 data file with `actions/upload-artifact@v4`, hidden files enabled, and missing files treated as errors.
- [x] Add `coverage`, dependent on `test-shards`: checkout, Python 3.12 with pip cache, install dev extras, download `coverage-*` via `actions/download-artifact@v5` into `.coverage-data` with merged artifacts, run `python -m coverage combine .coverage-data`, then `python -m coverage report --fail-under=78`.
- [x] Add `test`, named `test (${{ matrix.python }})`, as a three-version compatibility matrix. Use `needs: [test-shards, coverage]` and `if: ${{ always() }}`. Its only step must exit 1 unless both `needs.test-shards.result` and `needs.coverage.result` equal `success`.

- [x] Run GREEN: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_ci_pytest_shard.py","tests/test_ci_workflow.py"]' --capture brigade-work --no-reuse`.

Expected: repository checks and both focused test files pass.

- [x] Run `brigade work verify run --target . --argv-json '["actionlint",".github/workflows/ci.yml"]' --capture brigade-work --no-reuse`.

Expected: exit 0 with no diagnostics.

- [x] Run each shard from 0 through 3 against the two focused test files through separate Brigade verification commands. Expected: all exit 0, and their selected counts sum to the unsharded collected count.

- [x] Tick every checkbox, run `git diff --check`, inspect the diff, and commit with `git commit -m "ci: shard Python test matrix"`.
