# Local Operator Center

`brigade center` is a read-only CLI view over local Brigade state. It is meant for wrappers and future UI experiments that need one stable JSON surface without starting a server, database, daemon, scheduler, or sync engine.

`brigade daily` is the agent-facing driver on top of the same local evidence. The center commands answer "what exists"; the daily commands answer "what is the best next safe thing to do, what evidence supports it, and how do I close it out?"

Commands:

```bash
brigade center status
brigade center status --json
brigade center activity
brigade center activity --json
brigade center reviews
brigade center reviews --json
brigade center templates
brigade center templates --json
brigade center schema
brigade center schema --json
brigade center report plan
brigade center report build
brigade center report list
brigade center report show <report-id>
brigade center report archive <report-id>
brigade center report review <report-id>
brigade center report compare <report-id>
brigade center report diff <base-report-id> <compare-report-id>
brigade center report diff <base-report-id> <compare-report-id> --record
brigade center report closeout <report-id>
brigade center actions plan <report-id>
brigade center actions build <report-id>
brigade center actions list
brigade center actions show <action-id>
brigade center actions doctor
brigade center actions import-issues
brigade center actions start <action-id>
brigade center actions done <action-id>
brigade center actions defer <action-id> --reason "not today"
brigade center actions archive --completed
brigade center readiness plan
brigade center readiness closeout
brigade center readiness list
brigade center readiness show <readiness-id>
brigade center readiness import-issues
brigade daily init
brigade daily status
brigade daily plan
brigade daily review
brigade daily run
brigade daily closeout
brigade daily history
brigade daily show <run-id>
brigade daily doctor
brigade daily schema
brigade daily resume
brigade daily repair
brigade daily unblock
brigade daily protocol
brigade daily telemetry
brigade daily telemetry doctor
brigade daily hardening plan
brigade daily hardening audit
brigade daily hardening import-issues
brigade daily hardening closeout
brigade daily approvals list
brigade daily approvals show <approval-id>
brigade daily approvals approve <approval-id>
brigade daily approvals reject <approval-id> --reason "not now"
brigade daily approvals hold <approval-id> --reason "needs review"
brigade daily approvals compare <approval-id>
brigade daily approvals archive --consumed
brigade work phases init
brigade work phases plan
brigade work phases list
brigade work phases schema
brigade work phases status
brigade work phases next
brigade work phases show <phase-id>
brigade work phases start <phase-id>
brigade work phases complete <phase-id>
brigade work phases defer <phase-id> --reason "not in this tranche"
brigade work phases closeout <phase-id|range|latest>
brigade work phases compare <phase-id|range|latest>
brigade work phases doctor
brigade work phases import-issues
brigade work phases actions plan
brigade work phases actions build
brigade work phases actions list
brigade work phases actions import-issues
brigade work phases report build
brigade work phases report list
brigade work phases report show <report-id>
brigade work phases report closeout <report-id|latest>
brigade work phases report compare <report-id|latest>
brigade work phases session start --range <range>
brigade work phases session list
brigade work phases session show <session-id|latest>
brigade work phases session next <session-id|latest>
brigade work phases session resume <session-id|latest>
brigade work phases session closeout <session-id|latest>
```

`status` summarizes active work, pending tasks, pending imports, scanner sweep health, review health, handoff drafts, tool catalog health, learning candidates, context packs, release readiness, release candidates, repo fleet, roadmap health, project consolidation, and security health.

`activity` reads local receipts and pack metadata across work sessions, scanner runs, scanner sweeps, review runs, context packs, release readiness receipts, release candidates, and install smoke receipts.

`reviews` returns one pending local review queue across work imports, learning candidates, project consolidation issues, and context pack health. Each row includes:

- owning subsystem
- local id
- status
- priority or severity when known
- safe summary
- suggested next command

`schema` exports a read-only manifest for wrapper-facing JSON contracts. It covers `status`, `activity`, `reviews`, `templates`, report evidence, report review action plans, daily action queues, and operator readiness closeouts. The manifest names stable top-level fields, item fields, action fields, and source commands without reading or writing local receipts.

`templates` lists local workflow templates for context packs, tool packs, project audits, release candidates, and review closeouts.

Every center row uses the same wrapper-facing fields: `subsystem`, `local_id`, `status`, `priority`, `severity`, `safe_summary`, `created_at`, `updated_at`, `receipt_path`, `path`, and `suggested_next_command`.

## Daily Driver

`brigade daily status` summarizes the current local operating state from work, imports, center reviews, action queues, readiness, handoffs, memory-care, security, tools, release receipts, and operator reports. It also returns the next recommended command.

`brigade daily init` writes conservative local defaults to `.brigade/daily.toml`. The config can prefer task, inbox, or readiness modes and can disable context pack builds, operator report builds, readiness imports, import promotion, or work runs.

`brigade daily plan` ranks local candidate actions by urgency, safety, acceptance coverage, provenance, and expected usefulness. It prefers pending accepted tasks, then reviewed imports with acceptance criteria, reviewed center actions, readiness blockers that can become imports, and stale handoff, memory, or security issues. It chooses one recommended action and writes no state unless `--record` is passed. The local preferred mode can move inbox or readiness items upward without bypassing risk, approval, or remote-mutation guards. JSON output includes selection reasons, rejection reasons, safety blockers, approval blockers, stale evidence blockers, and quality blockers for wrappers.

`brigade daily review` previews the selected action with selected adapter, safe evidence references, acceptance criteria when available, risk, config blockers, approval boundary, likely next command, and context pack planning.

`brigade daily run` executes exactly one bounded local step. It can run a pending task, promote an approved import, start a reviewed center action, build an operator report, build a safe context pack, or import readiness issues. It refuses approval-required actions unless `--approved` or `--approval <approval-id>` is passed, refuses disabled adapters, and records receipts under `.brigade/daily/runs/`. Each run receipt includes a normalized adapter result with commands invoked, receipts created, blockers, warnings, evidence references, and the next recommended command.

Approval-required daily actions can be paused as local requests under `.brigade/daily/approvals/`. `brigade daily approvals list/show/approve/reject/hold/compare/archive` reviews, compares, or archives those requests without executing anything. `brigade daily run --approval <approval-id>` consumes one approved, unconsumed request only after revalidating the current config, source evidence, and source fingerprint.

`brigade daily closeout` updates the latest daily receipt as reviewed, deferred, blocked, or archived. It can also write a Memory Handoff draft for durable knowledge, but it never edits canonical memory.

`brigade daily resume`, `brigade daily repair`, and `brigade daily unblock` recover from blocked, failed, stale, or approval-waiting runs by suggesting the next safe command or writing local repair, approval, or import metadata. They do not run arbitrary suggested commands.

`brigade daily hardening plan`, `audit`, `import-issues`, and `closeout` track the phase 115-164 production-hardening tranche. The audit checks daily reliability, center contracts, inbox evidence quality, repo-fleet daily-use state, and self-dogfood release evidence with phase metadata. Import routing creates reviewed `source: daily-hardening` work imports, and release readiness carries compact hardening summaries. Closeout writes only local hardening receipts.

`brigade daily history` and `brigade daily show <run-id|latest>` inspect local plan and run receipts. `brigade daily doctor` reports missing or invalid config, stale plans, stale unclosed or blocked runs, parse errors, missing source evidence, approval issues, telemetry parse errors, and unsafe config. `brigade daily schema` prints wrapper-facing JSON contracts. `brigade daily protocol` documents the JSON-first agent loop, and `brigade daily telemetry` summarizes local-only dogfood metrics.

Daily commands are the intended wrapper path for an agent:

```bash
brigade daily status --json
brigade daily plan --json
brigade daily review --json
brigade daily run --json
brigade daily closeout --json
```

The daily driver never executes arbitrary suggested commands, starts scanners or reviewers, runs tools, runs fleet sweeps, mutates remotes, pushes, tags, publishes, uploads analytics, or edits canonical memory.

## Phase Execution Ledger

`brigade work phases` records long unattended work as local phase evidence under `.brigade/work/phases/`. The ledger exists so an agent-facing run cannot silently compress dozens of phases into a vague summary and later claim completion.

Each phase record stores the stated goal, status, implementation summary, changed files, tests run, test result summary, commit hash, push ref, deferrals, blockers, and next phase recommendation. A phase range must be declared up front with individual records or an explicit grouped record. `brigade work phases doctor` warns on missing records, stale in-progress phases, blocked phases without next steps, complete phases without tests or changed files, committed phases without hashes, pushed phases without push refs, stale completed phases without review closeout, and range records that were compressed without explicit grouping. `brigade work phases status` and `next` make a range easy to resume, `closeout` writes local reviewed, deferred, blocked, or archived closeout metadata, `compare` checks whether record evidence still matches local HEAD, files, reports, and doctor counts, `actions` builds metadata-only local action records from ledger issues, `report build` writes local Markdown and JSON evidence, `report closeout` writes report-bundle review metadata, `report compare` checks saved report freshness, and `import-issues` routes ledger problems into the work inbox without automatic promotion.

Phase execution sessions group a declared AFK range into one local record under `.brigade/work/phases/sessions/`. `session start` records the range, current phase, status summary, commit and test counts, report references, closeout state, and next command. `session next` classifies the safest next step across missing records, pending phases, stale in-progress work, unverified phases, missing commit or push evidence, unreviewed pushed phases, and session closeout. `session resume` records that recommendation as local metadata without executing the suggested command. `session list`, `show`, and `closeout` only read or update local session metadata.

The phase ledger is surfaced in `brigade daily status`, `brigade daily doctor`, `brigade work brief`, `brigade work doctor`, and `brigade center status`. Future AFK multi-phase work is not complete unless the ledger shows evidence or explicit deferrals.

Phase health includes open phase action counts and the top open phase action. `brigade work brief` and `brigade center status` expose those counts so the daily loop can see whether ledger issues already have local follow-up actions.

Open phase actions can be routed into the normal scanner-ready inbox with `brigade work phases actions import-issues`. The command writes only local imports, preserves phase action provenance, and keeps promotion explicit.

The daily driver also treats phase-ledger actions and unresolved phase issues as candidate work. It ranks them below accepted tasks and high-quality imports unless the issue blocks AFK or release completion. `brigade daily run` may start one local phase action or build one phase report as its single bounded step, but it does not execute the suggested repair command.

Release gates include phase-ledger evidence. `brigade release doctor` warns on unresolved closeouts, missing or stale phase reports, report compare issues, and pushed phases without current review closeouts. Release candidate bundles record the latest phase closeout, report references, and latest report compare summary, and candidate compare warns when newer phase evidence appears after the bundle was built.

## Operator Reports

`brigade center report build` writes a local bundle under:

```text
.brigade/center/reports/
```

Each bundle contains:

- `OPERATOR_REPORT.md`, a daily review summary with review queue, activity, and suggested next commands.
- `OPERATOR_REPORT.html`, a dependency-free escaped static rendering of the Markdown report.
- `CENTER_EVIDENCE.json`, stable JSON evidence for wrappers.

`brigade center report plan` previews the same evidence without writing. `list`, `show`, and `archive` inspect or move local bundles. Report health warns when the latest bundle is unclosed, stale, references missing receipts, was built from an older git HEAD, or newer center activity exists. `brigade work brief`, `brigade work doctor`, `brigade release doctor`, release candidate evidence, and release candidate compare surface those report health checks.

`brigade center report review <report-id|latest>` groups actionable report items into:

- urgent blockers
- pending work imports
- code review findings
- handoff drafts
- scanner sweep issues
- tool approvals, checkpoints, and runs
- backup, security, and memory-care issues
- release readiness and candidate issues
- project and learning candidates

`brigade center report compare <report-id|latest>` checks the report against current local state. It warns when HEAD changed, referenced receipts are missing, newer activity exists, newer release/readiness/verification/review/sweep/security receipts exist, or the review queue changed.

`brigade center report diff <base-report-id> <compare-report-id>` compares two completed report bundles. It reports new items, resolved items, changed items, new blockers, and stale receipt references. Passing `--record` writes a local diff receipt under:

```text
.brigade/center/report-diffs/
```

Diff receipts appear in `brigade center activity` and feed operator report health. `brigade work doctor` and `brigade release doctor` surface missing, stale, or issue-bearing report diffs, but no report diff command promotes, dismisses, runs, or fixes anything.

`brigade center report closeout <report-id|latest>` writes `CLOSEOUT.json` in the report bundle. Closeout states are `reviewed`, `deferred`, `superseded`, and `archived`. Closeout stores a reviewed timestamp, reason, unresolved item count, deferred item ids, and report fingerprint.

## Daily Action Queue

`brigade center actions plan <report-id|latest>` converts a report review plan into stable action records without writing them. `brigade center actions build <report-id|latest>` writes the queue under:

```text
.brigade/center/actions/
```

`build` requires the report to be closed out as `reviewed` or `deferred` unless `--allow-unreviewed` is passed. Repeated builds dedupe by report fingerprint and source item id, including actions already archived from the same report. High-priority items that appear in both urgent and subsystem groups become one action owned by the urgent group.

Each action stores:

- action id
- source report id
- source group
- source subsystem and local id
- status: `pending`, `active`, `done`, `deferred`, or `archived`
- priority or severity when known
- safe summary
- suggested command
- created, updated, and reviewed timestamps
- source fingerprint

`start`, `done`, and `defer` only update local action metadata. `archive --completed` archives completed actions and leaves pending, active, and deferred actions in the queue. Center status, center reviews, work brief, work doctor, and release doctor surface open action queue health.

`brigade center actions doctor` applies local aging policy to the action queue. Defaults warn on pending actions older than 24 hours, active actions older than 8 hours, deferred actions older than 72 hours, and completed actions older than 24 hours that have not been archived. `brigade center actions import-issues` routes those stale action issues into the work import inbox as `source: center-action-policy` without running suggested commands.

`brigade center readiness plan` is the final local operator readiness view. It aggregates roadmap audit, docs command inventory, center review queue, release readiness, repo fleet, security, memory-care, backup, tool catalog, context, projects, and learning health into blockers, warnings, waivers, and a manual-only publish checklist. `brigade center readiness closeout` writes a local receipt and `MANUAL_PUBLISH_CHECKLIST.md` under `.brigade/center/readiness/`. `--waive <finding-id>` records an explicit local waiver for one finding, and `brigade center readiness import-issues` turns unresolved findings into `source: center-readiness` work imports. Readiness never executes checklist commands.

## Repo Fleet Rollups

`brigade repos sweep plan/run/runs/show/closeout` explicitly refreshes safe local evidence across configured repos. A sweep runs only configured foreground local read/report commands inside each enabled repo, records per-command status and safe stdout/stderr summaries, stores raw logs only in gitignored local files, and writes one receipt under `.brigade/repos/sweeps/`. Sweep receipts use safe repo ids, labels, command labels, status counts, receipt labels, and local log labels.

`brigade repos report plan/build/list/show/archive/closeout` builds a local fleet rollup from configured `.brigade/repos.toml` entries. Fleet reports live under `.brigade/repos/reports/` and use safe repo ids, labels, counts, statuses, fingerprints, and receipt labels only. They do not copy exact private repo names, owner names, org names, local paths, raw logs, raw scanner output, or raw evidence into public artifacts.

`brigade repos actions plan/build/list/show/start/done/defer/archive` turns a reviewed or deferred fleet report into a local fleet action queue under `.brigade/repos/actions/`. Fleet actions are metadata records only. They point to the safe repo label, source subsystem, source local id, safe summary, and suggested command, but do not execute the command.

`brigade repos actions dispatch plan/apply`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build` connect those local fleet actions to each target repo's existing work import and context pack paths. Dispatch writes only gitignored target repo Brigade state, never promotes the import, never runs work, and never executes suggested commands. Reconciliation reads target repo imports, tasks, closeouts, release receipts, and operator reports, then updates local fleet action metadata.

`brigade repos release plan/build/list/show/compare/closeout/archive` creates local fleet release train bundles under `.brigade/repos/releases/`. Release trains aggregate per-repo readiness, candidates, fleet action reconciliation, verification, review, security, operator reports, and dirty-state counts into a manual-only publish plan. They classify repos as ready, blocked, needing review or dispatch, in progress, stale, missing a release candidate, or deferred. Compare and closeout keep the train reviewable without pushing, tagging, publishing, or mutating remotes.

`brigade repos release actions` turns reviewed release trains into local per-repo action queues, and `brigade repos release evidence` records manual publish evidence such as verification, release doctor, candidate compare, tag, push, and release outcomes. `brigade repos release reconcile` resolves actions against those evidence records, and `brigade repos release summary` reports unresolved, missing, blocked, skipped, deferred, and completed evidence. These records are local metadata only.

`brigade repos release report/checklist/hygiene/import-issues/ready/activity/manifest/audit` provides the closeout layer around those trains: review report files, manual evidence checklists, hygiene warnings, local task imports for unresolved evidence, chronological activity, bundle manifests, bundle audits, and a final waiver-aware ready gate for manual publishing. `brigade repos release waivers` records explicit local waivers for reviewed release-train risk without hiding the underlying counts, and its templates, doctor, and import path surface expired, stale, missing-expiry, missing-owner, weak-reason, invalid-scope, repo-drift, or train-changed waivers.

Center status, center reviews, work brief, work doctor, release doctor, and release evidence include fleet sweep, fleet report, fleet action queue, dispatch, reconciliation, fleet release train action, and manual evidence health.

The operator center never invokes scanners, tools, reviewers, handoff ingestion, release publishing, git commands that mutate state, or remote APIs. Only `center report build`, `center report archive`, `center report diff --record`, `center actions import-issues`, `center actions build/start/done/defer/archive`, and `center readiness closeout/import-issues` write local gitignored center files or work imports. Daily commands add local plan and run receipts under `.brigade/daily/` and can call only the bounded safe flows documented above.
