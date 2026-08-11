# Issue #846 R2 — residual work-store characterization

Refs #846. Slice R2 only: residual no-listener cells on top of the accepted R1
harness from PR #849. This document does not approve a backend, add a runtime
dependency, start a listener, migrate source of truth, or close #846.

GraphTrail impact lookup was skipped for this slice: `brigade code doctor`
reported GraphTrail not installed (`error: graphtrail is not installed; run
\`brigade setup\``).

## Contract

Normative inputs:

- GitHub issue #846
- The single 0.27.0 scoping comment on that issue
- Accepted R1 artifacts from PR #849

Normative behavioral anchors:

- #737 graph / ready-set semantics
- #738 claim CAS (one winner, exit `13`, same-actor rejection, guards,
  empty-filter fail-closed)
- #770 BUILD native; Beads excluded

## R2 ownership

Extend only:

- this report
- synthetic fixtures (`docs/research/fixtures/work-store-r1/` reused;
  `docs/research/fixtures/work-store-r2/residual_markers.json` added)
- `scripts/measure_work_store.py`
- `tests/test_work_store_measurement.py`
- measurement JSON `docs/measurements/issue-846-work-store-r2.json`

Production work-store code, engine memory projection, #843/#850, #844, #845,
#495, and #498 are out of scope.

## Residual cells covered (no network listener)

| Cell | JSON ledger | SQLite WAL | Dolt shapes |
|---|---|---|---|
| Two-claimer race (0 + 13) | measured | measured | blocked |
| Same-actor rejection + idempotent retry | measured | measured | blocked |
| Guard mismatch + empty-filter | measured (`if_actor` + `if_status` match/mismatch) | unavailable (adapter has no release filters) | blocked |
| Restart recovery | measured (fresh subprocess read) | measured (fresh subprocess export) | blocked |
| Schema future/downgrade observation | measured (coerces to current; does not reject) | measured (meta preserves write; export coerces) | blocked |
| Branch/config backup + restore digests | measured (branch unsupported; config+claim+task digests) | measured | blocked |
| Install footprint | measured | measured | blocked |
| Cold-start timing | measured (parent `subprocess.run` wall; inner probe op labeled separately) | measured (parent `subprocess.run` wall; inner probe op labeled separately) | blocked |
| Backup timing | measured (`backup_time_ns`) | measured | blocked |
| Resource measurements (disk + RSS) | measured (`rss_scope=subprocess_child`) | measured (`rss_scope=subprocess_child`) | blocked |
| Metrics state (observed, not hard-coded) | measured | measured | blocked |
| Deleted secret / history | measured (live/backup + synthetic Git/ignore-path) | measured (live/backup; Git history unavailable — no native VCS) | blocked |
| Server auth / TLS / permissions | blocked (needs listener + approval) | blocked | blocked |

Hard-coded `metrics_state` / `secret_history_handling` gate booleans from R1
are replaced with observed harness cell results. Numbers are never fabricated:
every residual cell is `measured`, `blocked`, or `unavailable`.

Restart cells use a fresh subprocess that receives only the store path and
emits machine-readable JSON. Cold-start timing is the parent wall clock around
`subprocess.run` (`cold_start_timing_scope=parent_subprocess_wall`); the probe
also reports `inner_operation_ns` for the in-process read/export after import.
JSON and SQLite RSS samples use the same `popen_sample_child` protocol
(`rss_scope=subprocess_child`). JSON secret-history proves ignore-path
exclusion and synthetic tracked Git retention of a deleted marker; SQLite marks
the Git-history subcell `unavailable` and does not claim it in the pass.

SQLite metrics scans the touched store directory and restores
`DISABLE_TELEMETRY` / `BRIGADE_ANONYMOUS_METRICS` after the cell. JSON guard
coverage includes `if_status` match and mismatch with exit/reason assertions.

## Sendback repair (comment 5247390894)

- Secret history: JSON measures synthetic Git + ignore-path; SQLite marks
  `git_history` unavailable (live/backup still measured).
- Cold-start: parent times `subprocess.run`; inner operation labeled separately.
- RSS: both shapes sample a short-lived child via one Popen protocol and label
  `rss_scope` / `rss_protocol` explicitly.

## Decision record (unchanged from R1)

1. Keep the machine-local JSON ledger.
2. Current need remains sequential portable sync plus reviewable
   branch-and-merge proposals for one operator.
3. Concurrent cross-machine claims are not a current requirement.
4. Dolt remains a characterization candidate (not banned, not selected).
5. No backend, daemon, listener, Beads path, or `brigade.workstore.v1`.

Observed schema note (characterization only): the current ledger path coerces
`version` to `TASK_LEDGER_VERSION` via `ensure_ledger_edges` rather than
rejecting future or downgraded versions. R2 records that observation; it does
not change production schema policy.

## Verification

Baseline anchors:

```bash
brigade work verify run --target . --command \
  ".venv/bin/python -m pytest -q tests/test_work_cmd_edges.py tests/test_work_cmd_claim.py" \
  --capture brigade-work
```

Focused R2 harness + anchors:

```bash
brigade work verify run --target . --command \
  ".venv/bin/python -m pytest -q tests/test_work_store_measurement.py tests/test_work_cmd_edges.py tests/test_work_cmd_claim.py" \
  --capture brigade-work
```

Relevant full gate:

```bash
./scripts/verify
```

Cite exact Brigade receipt IDs in the draft PR body.
