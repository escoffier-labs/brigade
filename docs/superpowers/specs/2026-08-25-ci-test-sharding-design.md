# CI Test Sharding Design

## Problem

The Python test matrix still runs the complete suite serially on Python 3.10, 3.11, and 3.12. The August 25 Python 3.11 job took 46 minutes 54 seconds for 8,986 tests, up from 17 minutes 24 seconds for 8,148 tests on August 19. Package installation takes seconds, so dependency caching alone cannot recover the lost wall time.

## Decision

Run four deterministic pytest shards on each supported Python version. A repository-owned pytest launcher will assign each collected node ID to exactly one shard with a stable SHA-256 hash. This avoids a new dependency, keeps the normal local pytest path unchanged, and spreads parametrized tests more evenly than file-only partitioning.

The workflow will also:

- enable `actions/setup-python` pip caching for Python jobs that install the development extras;
- cancel an older run when a newer commit starts CI for the same pull request or ref;
- collect coverage independently from the four Python 3.12 shards, combine the data in a separate job, and enforce the existing 78 percent threshold once;
- preserve the protected branch check names `test (3.10)`, `test (3.11)`, and `test (3.12)` with lightweight compatibility gate jobs that require every shard and the combined coverage job to succeed.

## Files

- `.gitignore`: allow the CI shard launcher under the otherwise ignored `scripts/` tree.
- `scripts/ci_pytest_shard.py`: validate shard coordinates, partition collected pytest items, report the selection, and invoke pytest.
- `tests/test_ci_pytest_shard.py`: deterministic, exhaustive, disjoint, and argument-validation tests for the launcher.
- `tests/test_ci_workflow.py`: workflow contract for concurrency, caching, shard count, coverage artifacts, and protected-check compatibility.
- `.github/workflows/ci.yml`: parallel shard matrix, coverage combination, dependency caching, cancellation, and compatibility gates.

## Shard contract

- Shard indexes are zero-based and must satisfy `0 <= index < count` with `count > 0`.
- Assignment is `int(sha256(nodeid).hexdigest(), 16) % count`.
- The launcher passes all arguments after `--` to pytest without reinterpretation.
- Deselected items are reported through pytest's normal deselection hook.
- An empty shard is successful if pytest collection itself succeeds. Four shards against the current suite are nonempty.
- CI uses four shards. Changing the count requires updating the workflow contract test.

## Coverage and failure behavior

- Python 3.10 and 3.11 shards run without coverage.
- Python 3.12 shards write uniquely named coverage data files and upload one artifact per shard.
- The coverage job downloads all four artifacts, runs `coverage combine`, and enforces `coverage report --fail-under=78`.
- A test shard failure fails the shard matrix. The compatibility gates use `always()` and explicitly fail unless both the shard matrix and coverage job succeeded, so required checks never become misleading skipped checks.
- `fail-fast` stays false so one failure does not discard evidence from the remaining shards.

## Expected effect

The Python matrix's wall time should approach the slowest quarter of the suite plus collection, runner queueing, and coverage combination. The first merged run is the measurement point. If one shard is consistently more than 25 percent slower than the median after five runs, replace hash partitioning with a checked-in duration map in a separate change.

## Non-goals

- Do not add `pytest-xdist`, `pytest-split`, or another dependency.
- Do not change local `./scripts/verify` behavior.
- Do not change the 78 percent coverage threshold or supported Python versions.
- Do not update branch protection settings.
- Do not deploy or configure Octopool. That exploration is tracked in a separate Memory Handoff.
