# Verification Gate Runtime Design

## Problem

`AGENTS.md` requires `./scripts/verify` before every completed code change and calls it the fast completion gate. That script now runs the full serial coverage suite. Recent receipts show 37 to 55 minute runtimes, concurrent agents launch separate copies, and a timed-out run often triggers another attempt with a larger timeout.

## Decision

Use 2 verification tiers:

1. Development slices run `./scripts/verify-focused <pytest-selector>...` through `brigade work verify run`. The script keeps the repository-wide lint, format, type, version, and snapshot checks, then runs only the named pytest selectors.
2. Pull requests, merges, releases, and verification-infrastructure changes run `./scripts/verify` once through Brigade with `--timeout 3600`. CI keeps its existing full coverage gate.

The documented full command is `brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work`. `--timeout 3600` is the Brigade verifier bound for one known-long integration run. The verifier default is 900 seconds; receipt `20260825-224906-work-verify-2ea8a3` used that default and timed out at 900.019 seconds while pytest was still active. The last completed full command took 1390.94 seconds. A verifier timeout means the command exceeded `--timeout`. Status 75 is lock contention, not a verifier timeout, and must not be retried with a larger timeout.

The full script acquires a nonblocking lock in the shared Git directory through a dependency-free Python 3 POSIX helper (`scripts/verify_lock.py`) that uses `fcntl.flock`. A second local full gate exits with status 75 and tells the caller to use the focused gate or wait for the active run. This protects linked worktrees without adding a runtime dependency or changing CI coverage, and it does not depend on util-linux `flock`.

## Files

- `scripts/verify-focused`: focused development entrypoint.
- `scripts/verify_lock.py`: portable POSIX lock helper. Opens the lock file 0600, takes `LOCK_EX|LOCK_NB`, then execs `scripts/verify` with the descriptor held.
- `scripts/verify`: invoke the helper when `BRIGADE_VERIFY_LOCK_HELD` is absent. Full checks remain unchanged.
- `tests/test_verify_script.py`: executable tests for selector forwarding, missing selectors, lock contention, flock-shim independence, and the documented full-command `--timeout 3600` contract.
- `AGENTS.md`: focused development contract and full integration contract, including `--timeout 3600` on the documented full Brigade command.

## Failure behavior

- `verify-focused` with no selector exits 2 before running any tool.
- A focused tool or selected test failure is returned unchanged by `set -euo pipefail`.
- A concurrent full gate exits 75 before lint or tests start. That status is lock contention. It is not a verifier timeout and must not be retried with a larger `--timeout`.
- A Brigade verifier timeout means `./scripts/verify` exceeded the documented `--timeout 3600` bound. That is a command-duration failure, not lock contention. Do not treat it as status 75.
- `BRIGADE_VERIFY_LOCK_PATH` may point tests or isolated tooling at a separate lock file. Normal runs default to `<git-common-dir>/brigade-full-verify.lock`.
- `BRIGADE_VERIFY_LOCK_PYTHON` selects the interpreter for the lock helper. When unset, `scripts/verify` uses the configured `.venv` Python.
- After the helper holds the lock it sets `BRIGADE_VERIFY_LOCK_HELD=1` and execs `scripts/verify` so the lock remains held for the complete gate.

## Verification

Run `tests/test_verify_script.py` through Brigade first. Then run the focused script against `tests/test_verify_script.py` through Brigade. Do not launch the full suite while unrelated Brigade worktrees are active.
