# Phase Execution Ledger

Brigade uses the phase execution ledger to make long unattended work auditable. A multi-phase goal is not complete because an operator says it is complete. It is complete only when the ledger contains a record for each phase, with evidence or an explicit deferral.

The ledger is local and gitignored:

```text
.brigade/work/phases/
```

Public docs describe the contract. Local records hold the run-specific evidence.

## Commands

```bash
brigade work phases init
brigade work phases plan --phase-id phase-165 --title "Auditable AFK ledger" --goal "phase 165"
brigade work phases plan --range 165-170 --grouped --title "Grouped hardening" --goal "phase range"
brigade work phases list
brigade work phases schema
brigade work phases status --range 165-170
brigade work phases next --range 165-170
brigade work phases show <phase-id>
brigade work phases start <phase-id>
brigade work phases complete <phase-id> --summary "..." --file src/file.py --test "pytest ..." --commit <hash> --push-ref main
brigade work phases defer <phase-id> --reason "..."
brigade work phases closeout <phase-id|range|latest> --status reviewed --reason "checked evidence"
brigade work phases doctor --range 165-170
brigade work phases import-issues --range 165-170
brigade work phases report build --range 165-170
brigade work phases report list
brigade work phases report show latest
```

Every command supports stable JSON output with `--json`.

## Record Contract

Each phase record includes:

- `phase_id`
- `title`
- `source_goal`
- `status`
- `started_at`
- `completed_at`
- `implementation_summary`
- `files_changed`
- `tests_run`
- `test_result_summary`
- `commit_hash`
- `push_ref`
- `deferred_items`
- `blocker_reason`
- `next_phase_recommendation`

Allowed statuses:

- `pending`
- `in-progress`
- `implemented`
- `verified`
- `committed`
- `pushed`
- `deferred`
- `blocked`

## No Silent Compression

Grouped work is allowed, but it must be declared before work starts.

If a goal covers phases 200 through 210, the operator must either:

- create one record for each phase, or
- create an explicit grouped range with `brigade work phases plan --range 200-210 --grouped`.

Completing a range requires every phase in that range to be implemented or explicitly deferred. A broad summary record that claims a range without an explicit grouping record is a ledger issue.

## Completion Rule

A phase is not complete unless the ledger has evidence:

- changed files, or a deferral reason
- tests run, or a clear deferred verification reason
- commit hash when marked `committed`
- push ref when marked `pushed`

Deferral is acceptable. Silent compression is not.

## Doctor Checks

`brigade work phases doctor` reports:

- complete phases without tests
- complete phases without changed files or deferral evidence
- committed phases without commit hashes
- pushed phases without push refs
- missing records for a requested range
- range records compressed without explicit grouping
- stale in-progress phases
- blocked phases without a next recommendation
- stale completed phases that have not been reviewed

The phase ledger is surfaced in `brigade daily status`, `brigade daily doctor`, `brigade work brief`, `brigade work doctor`, and `brigade center status`.

## Closeouts, Reports, And Imports

`brigade work phases closeout <phase-id|range|latest>` writes a local review record under:

```text
.brigade/work/phases/closeouts/
```

Closeouts can be `reviewed`, `deferred`, `blocked`, or `archived`. Each record stores the affected phase ids, unresolved issue count, deferred phase ids, reason, review timestamp, and source fingerprint. Doctor uses those fingerprints to warn when completed phase evidence becomes stale or unreviewed again.

`brigade work phases report build` writes a local bundle under:

```text
.brigade/work/phases/reports/
```

Each report includes `PHASE_REPORT.md` and `PHASE_EVIDENCE.json` with range status, doctor checks, record summaries, and suggested next commands.

`brigade work phases import-issues` routes unresolved ledger issues into the scanner-ready work inbox as `source: phase-ledger` task imports. Imports dedupe by a stable source fingerprint and keep promotion explicit.

Release readiness and release candidate evidence include a compact phase-ledger summary so publish review can see whether long unattended work still has open ledger issues.
