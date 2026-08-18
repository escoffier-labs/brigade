# Bootstrap Doctor lifecycle lint integration

Date: 2026-08-17
Status: Approved for implementation

## Goal

Expose Bootstrap Doctor's deterministic lifecycle lint through Brigade's managed-tool doctor checks without turning operator-owned OpenClaw state into a Brigade workspace failure.

## Contract

- Run `bootstrap-doctor status --json` and `bootstrap-doctor lint --json` as separate probes.
- Preserve the existing status result.
- Report the lint probe as a separate managed-tool check.
- Treat clean lint output as `OK`.
- Treat lifecycle findings as advisory `WARN`, including Bootstrap Doctor findings whose own severity is `error`.
- Treat a missing lint command, nonzero exit, malformed JSON, or an invalid payload as `WARN` with an upgrade or diagnostic message. These conditions must not raise from Brigade doctor.
- Include warning and error counts plus the first stable finding ID in the lint summary when findings exist.
- Keep invocation deterministic and side-effect free.

## Managed surfaces

The Bootstrap Doctor manifest advertises these surfaces:

- `summary-json`: `bootstrap-doctor status --json`
- `doctor-json`: `bootstrap-doctor lint --json`
- `verify-exit`: `bootstrap-doctor lint --json`

## Tests first

Add focused coverage in `tests/test_managed.py` for:

- clean status plus clean lint;
- lint warnings and errors mapped to Brigade `WARN`;
- malformed or unavailable lint mapped to `WARN`;
- deterministic status-then-lint command order;
- stable finding ID and severity counts in the lint summary;
- updated managed-tool surface metadata.

Observe the focused test fail before changing implementation, then make the smallest change in `src/brigade/managed.py` that satisfies the contract.

## Verification

Run focused managed-tool tests and the repository verification script through `brigade work verify run`. Because this worktree reuses the base repository virtualenv, set `PYTHONPATH` to this worktree's `src` directory so subprocesses import the isolated branch.

## Out of scope

- Mutating or repairing OpenClaw workspaces.
- Converting Bootstrap Doctor lifecycle findings into Brigade `FAIL`.
- Installing or upgrading Bootstrap Doctor automatically.
- Adding model-route policy linting.
