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
brigade work phases compare <phase-id|range|latest>
brigade work phases reconcile <phase-id|range|latest>
brigade work phases privacy <phase-id|range|latest>
brigade work phases handoff <phase-id|range|latest> --lint
brigade work phases doctor --range 165-170
brigade work phases import-issues --range 165-170
brigade work phases evidence add <phase-id> --file src/file.py --test "pytest ..."
brigade work phases verify plan <phase-id|range|latest>
brigade work phases verify record <phase-id> --command "pytest ..." --status passed
brigade work phases actions plan
brigade work phases actions build
brigade work phases actions list
brigade work phases actions show <action-id>
brigade work phases actions start <action-id>
brigade work phases actions done <action-id>
brigade work phases actions defer <action-id> --reason "..."
brigade work phases actions archive --completed
brigade work phases actions import-issues
brigade work phases goal scaffold --range 211-225
brigade work phases report build --range 165-170
brigade work phases report list
brigade work phases report show latest
brigade work phases report closeout latest --status reviewed --reason "checked report"
brigade work phases report compare latest
brigade work phases session start --range 211-225 --goal "AFK tranche"
brigade work phases session list
brigade work phases session show latest
brigade work phases session next latest
brigade work phases session resume latest
brigade work phases session closeout latest --status reviewed --reason "checked session"
brigade work phases session activity latest
brigade work phases session progress latest
brigade work phases session import-issues latest
brigade work phases session gate latest
brigade work phases session report build latest
brigade work phases session report list
brigade work phases session report show latest
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

`brigade work phases compare <phase-id|range|latest>` is a read-only freshness check for phase evidence. It reports changed HEAD labels, missing referenced files, missing commit hashes, missing push refs, newer phase reports, newer test evidence, and changed doctor issue counts when a record carries a baseline.

`brigade work phases actions plan` previews local action records from phase doctor issues and closeout blockers. `actions build` writes deduped metadata-only actions under `.brigade/work/phases/actions/`. `start`, `done`, `defer`, and `archive` only update local action metadata. They never execute suggested commands.

`brigade work phases actions import-issues` routes open phase action records into the work inbox as `source: phase-ledger-action` task imports. Imports preserve the action id, phase id, issue type, safe summary, suggested command, and source fingerprint, then dedupe through the normal import path.

`brigade work phases goal scaffold --range <range>` writes a local editable `/goal` draft under `.brigade/work/phases/goals/`. The draft is generated from ledger records, matching session state, unresolved blockers, and roadmap references. It deliberately omits raw logs, private paths, private repo names, and private evidence.

`brigade work phases report build` writes a local bundle under:

```text
.brigade/work/phases/reports/
```

Each report includes `PHASE_REPORT.md` and `PHASE_EVIDENCE.json` with range status, doctor checks, record summaries, and suggested next commands.

`brigade work phases report closeout <report-id|latest>` writes `CLOSEOUT.json` into the local report bundle with `reviewed`, `deferred`, `superseded`, or `archived` status. Report closeouts are local review metadata only.

`brigade work phases report compare <report-id|latest>` checks a report bundle against current phase status counts, doctor issue count, HEAD label when captured, closeout state, and newer phase record changes.

`brigade work phases session start --range <range>` creates a local AFK execution session under `.brigade/work/phases/sessions/`. A session records the requested phase range, source goal, current phase, phase status summary, commit and test counts, report references, closeout state, and next recommended command. `session list`, `session show`, and `session closeout` inspect or review that local metadata without executing work. `session next` and `session resume` classify the safest next step, such as a missing phase record, pending phase, stale in-progress phase, unverified phase, missing commit or push evidence, unreviewed pushed phase, or session closeout. `resume` writes only local resume metadata and never executes the suggested command.

`brigade work phases session checkpoint <session-id|latest>` records a local checkpoint under `.brigade/work/phases/session-checkpoints/` and attaches a compact reference to the session. A checkpoint stores the session id, phase range, phase id when known, status, safe summary, safe notes, current next-step recommendation, suggested next command, and source fingerprint. It is recovery metadata only. It does not execute the suggested command.

`brigade work phases session checkpoints list`, `show`, and `compare` inspect those recovery points with text and JSON output. `list --session <session-id|latest>` narrows the view to one AFK session, `show latest` returns the newest checkpoint, and `compare <checkpoint-id|latest>` checks whether the saved next-step state still matches the current session state without executing its suggested next command.

`brigade work phases session checkpoints import-issues <checkpoint-id|latest>` routes blocked or stale checkpoint issues into deduped `source: phase-session-checkpoint` work imports. The records preserve checkpoint id, session id, phase id, issue type, source fingerprint, safe summary, and suggested next command. It never promotes or runs the imported work.

`brigade work phases session next` and `brigade work phases session resume` include the latest checkpoint summary and checkpoint issue count when a session has recovery metadata. This keeps wrapper-facing resume decisions checkpoint-aware without requiring an extra command before every AFK resume.

`brigade work phases session recovery-note <session-id|latest>` records safe AFK resume context under `.brigade/work/phases/session-recovery-notes/`. Recovery notes store a summary, optional safe notes, optional evidence labels, the current next-step snapshot, and a source fingerprint. `brigade work phases session recovery-notes list/show` inspect those records, and `brigade work phases session recovery-notes closeout <note-id|latest>` marks a note reviewed, deferred, blocked, or archived with local metadata. Session activity includes `session-recovery-note` events. Recovery notes do not change phase status, execute commands, or promote work.

`brigade daily plan` treats unresolved checkpoint state as a `phase-session-checkpoint` candidate. The candidate points at `brigade work phases session checkpoints import-issues` so the agent can route checkpoint drift into reviewed work instead of silently continuing from stale recovery metadata.

`brigade daily run` may also select `write-phase-session-checkpoint` for an active phase session and write exactly one local checkpoint receipt. This is still metadata-only and does not run phase work, tests, git commands, scanners, or remote operations.

`brigade work phases session risk <session-id|latest>` summarizes the current AFK risk level from the session next step, checkpoint issues, open recovery notes, and phase-ledger doctor issues. The command is read-only and gives wrappers one compact risk record before deciding whether to resume, checkpoint, import issues, or close out.

`brigade work phases session verification <session-id|latest>` summarizes verification entries across the session range. It counts expected, passed, failed, skipped, and deferred verification, lists phases with missing or failed verification, and suggests the next `brigade work phases verify plan` command without running tests.

`brigade work phases session privacy <session-id|latest>` summarizes privacy-check state across the session range. It counts clean, blocked, and missing privacy checks, lists phases that still need review, and suggests the next `brigade work phases privacy` command without running a scan.

`brigade work phases session handoffs <session-id|latest>` summarizes handoff coverage across the session range. It counts linted, drafted, failed, deferred, and missing handoffs, lists phases that still need a draft or lint repair, and suggests the next `brigade work phases handoff` command without writing a new handoff.

`brigade work phases session activity <session-id|latest>` produces a chronological read-only activity ledger from phase starts, completions, tests, commits, reports, compare summaries, actions, imports, closeouts, handoff drafts, and session resume events.

`brigade work phases session progress <session-id|latest>` summarizes percent complete, status counts, blockers, current phase, next command, test coverage, commit and push coverage, and estimated remaining local steps. It is read-only.

`brigade work phases session import-issues <session-id|latest>` routes unresolved session blockers into the existing work inbox as `source: phase-session` task imports. Imports dedupe by session id, phase id, issue type, and source fingerprint, and dismissed imports stay quiet until the source blocker changes.

`brigade work phases session gate <session-id|latest>` reports whether a session is safe to claim complete. The gate requires every phase to be implemented or deferred, tests recorded, commit and push evidence recorded, a clean privacy check, a linted handoff or handoff deferral, phase and session reports, clean report compare checks, and reviewed session closeout. Release doctor and release candidate evidence include the latest gate summary.

`brigade work phases session report build <session-id|latest>` writes a local bundle under `.brigade/work/phases/session-reports/` with `SESSION_REPORT.md` and `SESSION_EVIDENCE.json`. The bundle includes phase records, doctor issues, report compare summary, phase actions, phase-related imports, commits, push refs, test counts, blockers, and suggested next commands.

`brigade work phases import-issues` routes unresolved ledger issues into the scanner-ready work inbox as `source: phase-ledger` task imports. Imports dedupe by a stable source fingerprint and keep promotion explicit.

`brigade work phases evidence add <phase-id>` appends local evidence metadata to a phase record. Evidence attachments can include changed files, test commands, test result summaries, report ids, handoff paths, and notes. Doctor warns when attached local file or handoff references are missing.

`brigade work phases verify plan <phase-id|range|latest>` shows expected verification commands from the selected phase records and current recorded outcomes. `brigade work phases verify record <phase-id>` records an operator-supplied verification result as local metadata. It never runs the command.

`brigade work phases reconcile <phase-id|range|latest>` compares phase commit and push metadata against local git state. It reports missing commits, commits not on a local branch, pushed phases without push refs, and dirty worktree state after claimed completion. It never mutates git.

`brigade work phases privacy <phase-id|range|latest>` scans referenced public files and phase summaries for protected private or reference values, then records a compact clean or blocked summary in each selected phase record. Findings report pattern categories and source labels, not the matched private value.

`brigade work phases handoff <phase-id|range|latest>` drafts a standard no-card Memory Handoff into the local handoff inbox from selected phase evidence. `--lint` validates the draft before returning. The command records the handoff path and lint summary on each selected phase record as local evidence, but it does not edit canonical memory.

Release readiness and release candidate evidence include a compact phase-ledger summary so publish review can see whether long unattended work still has open ledger issues.
Release doctor and release candidate compare also warn when pushed phases are unreviewed, phase reports are missing or stale, closeouts have unresolved issues, report compare has open issues, or newer phase closeout/report evidence exists after a candidate was built.
Those release and operator surfaces also include latest phase session and session report references, warning when active sessions are stale, unreported, or changed after a candidate was built.
