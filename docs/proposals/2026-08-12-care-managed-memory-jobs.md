# Care-managed maintainer memory jobs

Status: proposed  
Issue: #762  
Depends on: the target-scoped `brigade care` redo from #759

## Summary

Move the maintainer's five recurring memory jobs (handoff ingest, care scan,
refresh, evidence crawl, and closeout) behind `brigade care` registrations.
This is a scheduler ownership change, not a new background service and not
permission for unattended agents to make judgment-heavy memory edits. The
operator's scheduler remains the trigger. Brigade owns only the entries it
registered, their target binding, and their observable receipts.

The migration is deliberately entry-by-entry. Each job can be enabled,
observed, and rolled back without replacing the other four. A registration is
identified by both its logical job id and the identity of its target. Two
checkouts registered by the same user therefore never share a systemd unit,
launchd label, status row, or uninstall action on the namespaced backends.
The optional crontab backend remains a single global `# BEGIN BRIGADE CARE`
managed block (not target-namespaced); `#759` keeps it for compatibility but
omits it from the default CLI `--backend` choices (`auto`/`systemd`/`launchd`).
Windows reports managed scheduling as unsupported under `auto` rather than
minting Task Scheduler names.

## Goals and non-goals

### Goals

- Inventory the recurring jobs that currently exist as maintainer practice.
- Express every job with one care-entry contract across scheduler backends.
- Preserve the review boundaries of the underlying Brigade commands.
- Migrate without double firing and with a per-job rollback path.
- Apply #759's target-scoping rule to install, status, repair, and uninstall.

### Non-goals

- Implement the entries or change any command named in this proposal.
- Add a daemon, global queue, runtime dependency, or remote scheduler.
- Invent automatic memory-card rewriting. Refresh remains a reviewed workflow.
- Replace runbook receipts, memory-care queues, evidence-engine state, or
  closeout receipts with scheduler state.
- Adopt unrelated nightly operations or outcome-ratchet recipes.

## Conceptual inventory

These are separate jobs even when an operator currently chains some of them.
Keeping them separate makes ownership, failure, cadence, and rollback visible.

| Job id | Purpose and current command boundary | Proposed default | Writes / authority boundary |
|---|---|---|---|
| `handoff-ingest` | Validate pending handoffs, then route eligible material through `brigade handoff lint --strict --target .` and `brigade ingest --promote-cards --route-documents --target .`. | Every 30 minutes | May promote only through the existing conservative ingester. A lint failure stops ingest. |
| `care-scan` | Recompute decay candidates with `brigade memory care scan --target .`, then publish review items with `brigade memory care import-issues --target .`. | Daily at 06:15 local time | Writes the refresh queue and work imports. It does not edit cards. |
| `memory-refresh` | Drain reviewed refresh work using the card-refresh workflow/runbook, grounded in the care queue and source-of-truth files. | Daily at 07:00, after scan | May change only reviewed, allowlisted cards through an approved runbook. If no approved refresh runbook is configured, the entry is not installable. |
| `evidence-crawl` | Refresh local evidence-memory inputs using an operator-approved evidence-crawl runbook whose steps correspond to the reviewed `brigade evidence crawl plan --target .`. | Daily at 08:00, after refresh | Optional native evidence engine reads local sources. No upload, daemon, or implicit execution of a plan is introduced. Missing engine or approval is a visible non-success receipt. |
| `memory-closeout` | Record review completion with `brigade memory care closeout --target .`. The runbook must first verify that the applicable scan/refresh/evidence receipts are present and successful or explicitly deferred. | Daily at 09:00, last | Writes closeout metadata only. It must not convert a partial or failed chain into `reviewed`. Deferral remains explicit. |

The times are defaults, not correctness dependencies. Ordering is enforced by
receipt prerequisites as well as staggered schedules, because persistent
timers, sleep/wake, and manual runs can violate wall-clock ordering.

## Care-managed entry schema

The implementation should extend the #759 care-entry model rather than create
a maintainer-only scheduler format. The following is the logical schema. a
backend renderer may derive cron lines or unit files, but must not discard any
field needed by `care status`.

```json
{
  "schema": {"name": "brigade-care-entry", "version": 1},
  "job_id": "care-scan",
  "target": {
    "canonical_path": "/absolute/resolved/workspace",
    "identity": "<the #759 target identity>",
    "namespace": "<filesystem-safe rendering of that identity>"
  },
  "schedule": {
    "cron": "15 6 * * *",
    "on_calendar": "*-*-* 06:15:00",
    "persistent": true
  },
  "description": "Brigade memory care scan",
  "execution": {
    "kind": "runbook",
    "runbook_path": ".brigade/memory-care/runbooks/care-scan.json",
    "runbook_id": "care-scan",
    "approved": true,
    "working_directory": "/absolute/resolved/workspace",
    "argv": ["brigade", "runbook", "run", "--approved", ".brigade/memory-care/runbooks/care-scan.json", "--target", "."]
  },
  "prerequisites": [],
  "ownership": {
    "managed_by": "brigade-care",
    "definition_hash": "sha256:<canonical-entry-hash>"
  },
  "rollback": {"legacy_job_ref": "<operator-supplied inventory reference>"}
}
```

### Field rules

- `job_id` is stable within a target and is one of the five ids above.
- `target.canonical_path` is the absolute, symlink-resolved directory captured
  at registration. Execution uses that directory and still passes `--target
  .`. It never depends on the scheduler's ambient working directory.
- `target.identity` and `target.namespace` **reuse the exact identity algorithm
  selected by #759** (`sha256` of the resolved absolute path, truncated to 16
  hex chars). #759 does not ship separate collision-handling beyond that
  identity string; #762 must not invent a second path hash or a parallel
  collision table. Moving or replacing a target requires an explicit
  re-registration. A coincidentally similar basename is not the same target.
- The registry key is `(target.identity, job_id)`. Namespaced backends
  (systemd service/timer basename, launchd label) plus status lookup, repair,
  and uninstall selectors include `target.namespace`. The optional crontab
  backend still uses one global managed marker and is not the preferred path
  for multi-target installs. Windows has no managed registration under `auto`.
- `schedule` contains equivalent backend forms. Local timezone semantics match
  `brigade care`. `persistent` requests catch-up where the backend supports it.
- `execution.kind` is `runbook` for all five jobs. Multi-command shell strings
  are not registration data. Runbooks preserve command allowlists, pins,
  step-level failure, and the existing receipt trail.
- `runbook_path` is target-relative, must resolve beneath the registered target,
  and must have the expected `runbook_id`. Refresh and evidence runbooks are
  operator-approved inputs, not silently generated authority grants.
- `prerequisites` is a list of `{job_id, acceptable_statuses,
  max_age_seconds}` records. It gates execution from target-local receipts, not
  merely from scheduler success. A missing, stale, wrong-target, or failed
  prerequisite produces a skipped/blocked receipt and no mutation.
- `definition_hash` covers the canonical entry including target identity,
  schedule, execution, and prerequisites. It provides the existing care drift
  detection. It is not a content or security signature.
- `legacy_job_ref` records enough operator-local information to restore the old
  trigger during the migration window. It must not contain secrets and is not
  executed by Brigade.

### Job mappings

| Job id | Runbook | Prerequisites | Expected terminal evidence |
|---|---|---|---|
| `handoff-ingest` | Shipped `ingest-sweep` sequence, renamed or aliased to the stable job id | None | Runbook receipt with lint and ingest steps |
| `care-scan` | Shipped scan/import sequence | None | Runbook receipt plus current refresh queue/import result |
| `memory-refresh` | Operator-approved card-refresh runbook | Successful `care-scan` receipt from the same target, normally within 24 hours | Runbook receipt listing reviewed work without copying card bodies into scheduler state |
| `evidence-crawl` | Operator-approved local evidence-crawl runbook | Successful or explicitly no-work `memory-refresh` receipt from the same target, normally within 24 hours | Runbook receipt and evidence-engine status/scan identity |
| `memory-closeout` | Shipped closeout guard plus closeout command | Same-target scan, refresh, and crawl receipts for the closeout window. Explicitly deferred optional stages are acceptable | Memory-care closeout receipt linked by ids/digests to the checked receipts |

Handoff ingest is independent of the daily chain: a bad handoff must not block
care scanning. Closeout covers the daily memory-maintenance chain, not every
half-hour ingest fire. A future combined report may summarize ingest receipts,
but that does not create a prerequisite edge.

## Target-scoped registration

Care state is user-scoped at the scheduler but target-scoped in ownership. For
targets `A` and `B`, installing `care-scan` creates two registrations:

```text
(identity(A), care-scan) -> brigade-<namespace(A)>-care-scan
(identity(B), care-scan) -> brigade-<namespace(B)>-care-scan
```

The concrete prefix follows #759 (`brigade-care-<identity>-<entry>` for
systemd; `dev.brigade.care.<identity>.<entry>` for launchd). The important
invariant is that the namespace appears anywhere those backends require
uniqueness. A non-namespaced `brigade-care-scan.timer` is insufficient.
Crontab remains an optional compatibility backend with one global
`# BEGIN BRIGADE CARE` block; do not rely on it for multi-target isolation.

`care install --target A` may create or update only entries in `A`'s
namespace. `status`, drift repair, adoption, and `uninstall` apply the same
selector. They must neither report another target's receipts as current nor
remove another target's entries. Uninstalling the final job for `A` may remove
`A`'s empty managed container, but never the container for `B` or unowned
scheduler text. Human edits retain #759's preserve-unless-explicitly-adopted
behavior.

Receipts and runbooks remain under each target's `.brigade/` tree. Scheduler
metadata may repeat only safe identifiers and paths required to launch the
job. Memory contents, evidence text, handoff bodies, and credentials never
enter the registry.

## Migration plan

Before step 1, inventory every legacy trigger (cron, user timer, Task Scheduler,
CI schedule, or maintainer wrapper), capture its cadence and enabled state in
`legacy_job_ref`, and establish one rollback command owned by the operator.
For each step use the same cutover protocol:

1. Install the new entry disabled or in dry-run form and inspect its resolved
   target, namespace, runbook hash/pins, schedule, and prerequisite edges.
2. Fire the underlying runbook once with
   `brigade runbook run --approved <runbook> --target .` (care CLI today only
   exposes install/status/uninstall — it does not run jobs), then confirm the
   target-local runbook/domain receipt and `care status` agree.
3. Disable the corresponding legacy trigger. Do not leave old and new triggers
   enabled together for a trial period.
4. Enable the care entry and observe at least one scheduled success/no-work
   result before advancing.

Migrate in this order:

1. **Handoff ingest.** It has no dependency on the daily chain and exercises
   target scoping plus frequent scheduling with the already shipped sequence.
2. **Care scan.** Establishes the queue and receipt that downstream jobs use.
3. **Refresh.** Add only after the approved refresh runbook demonstrably binds
   its edits to reviewed queue items.
4. **Evidence crawl.** Add after refresh receipts can gate it. Absence of the
   optional evidence engine remains explicit rather than silently successful.
5. **Closeout.** Migrate last, after it can prove that it is closing the
   care-managed chain rather than merely running at the end of the hour.

The migration is complete only when the legacy inventory has no enabled
duplicate, all five status rows show the expected target identity, and a full
daily chain produces linked receipts without ambient-path dependence.

## Rollback by job

Rollback is targeted, preserves receipts and queues for diagnosis, and never
uses broad deletion. For every job: disable its care entry first, verify it is
absent/disabled for the intended target namespace, restore the recorded legacy
trigger, and then observe one legacy fire before removing only that job's care
registration.

| Job | Rollback-specific action | State intentionally retained |
|---|---|---|
| `handoff-ingest` | Restore the old lint-then-ingest trigger. Check for a run already in progress before firing it. | Handoff archive/promotions and ingest receipts. Never re-ingest by deleting receipts. |
| `care-scan` | Restore the old scan/import trigger. Downstream care entries stay disabled until a fresh scan receipt exists. | Refresh queue and work-import dedupe state. |
| `memory-refresh` | Disable refresh and restore only the reviewed legacy refresh job. If a partial run occurred, require human review before retry. | Card edits, diffs, work items, and partial runbook receipt. Never reverse edits automatically. |
| `evidence-crawl` | Disable the care entry and restore the legacy local crawl. Do not wipe or rebuild the ledger as scheduler rollback. | Evidence database, cursors, scan ids, and failure receipt. |
| `memory-closeout` | Disable the entry and restore the old closeout trigger only after checking whether a closeout already exists for the receipt set. | Existing closeout receipts. Append a correction/defer record rather than rewriting history. |

If the defect is shared infrastructure (wrong target identity, namespace
collision, or cross-target uninstall), stop all newly managed jobs for the
affected target and roll back in reverse migration order. Jobs for other
target identities remain untouched.

## Status, failure, and safety semantics

- Scheduler presence is not job health. `care status` reports registration
  state, target identity, definition drift, runbook presence/approval, latest
  same-target receipt, and prerequisite freshness separately.
- A blocked prerequisite is not success and is not an excuse to run later
  mutating steps. It should be visible as `blocked`/`skipped`, with a safe
  reason and next command.
- Runbook step failure stops subsequent steps. Closeout cannot label a failed
  chain reviewed. An operator may explicitly defer it with the existing
  closeout semantics.
- Concurrent fires for the same `(target.identity, job_id)` require the normal
  runbook single-run protection or a care-level non-overlap guard. A second
  fire records or reports `already-running`. It does not start another writer.
- Registration never implies `--apply`, broad filesystem authority, network
  upload, or model-seat availability. Any such capability remains an explicit,
  reviewed runbook decision.

## Implementation acceptance plan

Implementation must be test-first, but this design-only change adds no runtime
test. The implementation slice should begin with failing focused tests for:

1. Two targets registering the same five job ids without marker/unit/task-name
   collisions.
2. Install, status, repair/adopt, and uninstall selecting only one target
   namespace.
3. Schema validation, canonical target binding, target-relative runbook
   containment, and definition-hash drift.
4. Exact command/runbook mapping for all five entries.
5. Same-target prerequisite success, missing/stale/wrong-target/failed receipt
   blocking, and no closeout after a partial chain.
6. Non-overlap behavior and receipt reporting.
7. Per-job migration/rollback leaving unrelated jobs and unowned scheduler
   content intact. And
8. Backend parity for the #759-supported managed backends (systemd and
   launchd under `auto`, plus optional crontab compatibility), with Windows
   remaining explicitly unsupported for managed scheduling under `auto`.

Focused care/runbook/memory/evidence tests and Ruff must pass before the full
`./scripts/verify` gate. Manual acceptance uses two temporary Brigade targets.
it must never install against the maintainer's real workspace while testing.

## Open implementation decisions

- The exact CLI spelling for selecting individual entries (for example,
  repeated `--entry` flags versus a profile) should follow the revised #759
  interface, while retaining the per-job ownership and rollback invariants.
- The approved refresh and evidence-crawl runbook bodies need separate review.
  this proposal fixes their scheduler contract, not their mutation policy.
- Backend-specific representation of a disabled staged entry may vary. The
  observable requirement is that it cannot fire before cutover and that status
  distinguishes staged from enabled.
