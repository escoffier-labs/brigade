# Verification Gate Runtime Design

## Problem

`AGENTS.md` requires `./scripts/verify` before every completed code change and calls it the fast completion gate. That script now runs the full serial coverage suite. Recent receipts show 37 to 55 minute runtimes, concurrent agents launch separate copies, and a timed-out run often triggers another attempt with a larger timeout.

## Decision

Use 2 verification tiers:

1. Development slices run `./scripts/verify-focused <pytest-selector>...` through `brigade work verify run`. The script keeps the repository-wide lint, format, type, version, and snapshot checks, then runs only the named pytest selectors.
2. Pull requests, merges, releases, and verification-infrastructure changes run `./scripts/verify` once through Brigade. CI keeps its existing full coverage gate.

The full script will acquire a nonblocking lock in the shared Git directory. A second local full gate exits with status 75 and tells the caller to use the focused gate or wait for the active run. This protects linked worktrees without adding a dependency or changing CI coverage.

## Files

- `scripts/verify-focused`: focused development entrypoint.
- `scripts/verify`: fail-fast shared full-gate lock. Full checks remain unchanged.
- `tests/test_verify_script.py`: executable tests for selector forwarding, missing selectors, and lock contention.
- `AGENTS.md`: focused development contract and full integration contract.

## Failure behavior

- `verify-focused` with no selector exits 2 before running any tool.
- A focused tool or selected test failure is returned unchanged by `set -euo pipefail`.
- A concurrent full gate exits 75 before lint or tests start.
- `BRIGADE_VERIFY_LOCK_PATH` may point tests or isolated tooling at a separate lock file. Normal runs default to `<git-common-dir>/brigade-full-verify.lock`.

## Verification

Run `tests/test_verify_script.py` through Brigade first. Then run the focused script against `tests/test_verify_script.py` through Brigade. Do not launch the full suite while unrelated Brigade worktrees are active.
