# Roster Reproducibility

## Goal

Record and apply per-seat reasoning settings, report read-only enforcement for
the seats that will actually run, and attribute scorecard observations only to
participating seats using their measured durations.

## Work

- [x] Add a validated optional `reasoning` field to CLI roster seats.
- [x] Apply reasoning to Codex exec and app-server, OpenCode, Pi, and Grok.
- [x] Preserve reasoning in roster snapshots and resume paths.
- [x] Limit direct-worker read-only advisories to the selected worker.
- [x] Base advisory classification on the effective transport and sandbox.
- [x] Record per-seat execution duration in run artifacts.
- [x] Count only actual worker and orchestrator participation in scorecards.
- [x] Run focused tests and `./scripts/verify` through Brigade.
