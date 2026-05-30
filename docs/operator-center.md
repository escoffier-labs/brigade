# Local Operator Center

`brigade center` is a read-only CLI view over local Brigade state. It is meant for wrappers and future UI experiments that need one stable JSON surface without starting a server, database, daemon, scheduler, or sync engine.

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
brigade center actions start <action-id>
brigade center actions done <action-id>
brigade center actions defer <action-id> --reason "not today"
brigade center actions archive --completed
```

`status` summarizes active work, pending tasks, pending imports, scanner sweep health, review health, handoff drafts, tool catalog health, learning candidates, context packs, release readiness, release candidates, repo fleet, roadmap health, project consolidation, and security health.

`activity` reads local receipts and pack metadata across work sessions, scanner runs, scanner sweeps, review runs, context packs, release readiness receipts, and release candidates.

`reviews` returns one pending local review queue across work imports, learning candidates, project consolidation issues, and context pack health. Each row includes:

- owning subsystem
- local id
- status
- priority or severity when known
- safe summary
- suggested next command

`schema` exports a read-only manifest for wrapper-facing JSON contracts. It covers `status`, `activity`, `reviews`, `templates`, report evidence, report review action plans, and daily action queues. The manifest names stable top-level fields, item fields, action fields, and source commands without reading or writing local receipts.

`templates` lists local workflow templates for context packs, tool packs, project audits, release candidates, and review closeouts.

Every center row uses the same wrapper-facing fields: `subsystem`, `local_id`, `status`, `priority`, `severity`, `safe_summary`, `created_at`, `updated_at`, `receipt_path`, `path`, and `suggested_next_command`.

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

## Repo Fleet Rollups

`brigade repos sweep plan/run/runs/show/closeout` explicitly refreshes safe local evidence across configured repos. A sweep runs only configured foreground local read/report commands inside each enabled repo, records per-command status and safe stdout/stderr summaries, stores raw logs only in gitignored local files, and writes one receipt under `.brigade/repos/sweeps/`. Sweep receipts use safe repo ids, labels, command labels, status counts, receipt labels, and local log labels.

`brigade repos report plan/build/list/show/archive/closeout` builds a local fleet rollup from configured `.brigade/repos.toml` entries. Fleet reports live under `.brigade/repos/reports/` and use safe repo ids, labels, counts, statuses, fingerprints, and receipt labels only. They do not copy exact private repo names, owner names, org names, local paths, raw logs, raw scanner output, or raw evidence into public artifacts.

`brigade repos actions plan/build/list/show/start/done/defer/archive` turns a reviewed or deferred fleet report into a local fleet action queue under `.brigade/repos/actions/`. Fleet actions are metadata records only. They point to the safe repo label, source subsystem, source local id, safe summary, and suggested command, but do not execute the command.

`brigade repos actions dispatch plan/apply`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build` connect those local fleet actions to each target repo's existing work import and context pack paths. Dispatch writes only gitignored target repo Brigade state, never promotes the import, never runs work, and never executes suggested commands. Reconciliation reads target repo imports, tasks, closeouts, release receipts, and operator reports, then updates local fleet action metadata.

`brigade repos release plan/build/list/show/compare/closeout/archive` creates local fleet release train bundles under `.brigade/repos/releases/`. Release trains aggregate per-repo readiness, candidates, fleet action reconciliation, verification, review, security, operator reports, and dirty-state counts into a manual-only publish plan. They classify repos as ready, blocked, needing review or dispatch, in progress, stale, missing a release candidate, or deferred. Compare and closeout keep the train reviewable without pushing, tagging, publishing, or mutating remotes.

`brigade repos release actions` turns reviewed release trains into local per-repo action queues, and `brigade repos release evidence` records manual publish evidence such as verification, release doctor, candidate compare, tag, push, and release outcomes. `brigade repos release reconcile` resolves actions against those evidence records, and `brigade repos release summary` reports unresolved, missing, blocked, skipped, deferred, and completed evidence. These records are local metadata only.

`brigade repos release report/checklist/hygiene/import-issues/ready/activity/manifest/audit` provides the closeout layer around those trains: review report files, manual evidence checklists, hygiene warnings, local task imports for unresolved evidence, chronological activity, bundle manifests, bundle audits, and a final waiver-aware ready gate for manual publishing. `brigade repos release waivers` records explicit local waivers for reviewed release-train risk without hiding the underlying counts, and its doctor/import path surfaces expired, stale, missing-expiry, or train-changed waivers.

Center status, center reviews, work brief, work doctor, release doctor, and release evidence include fleet sweep, fleet report, fleet action queue, dispatch, reconciliation, fleet release train action, and manual evidence health.

The operator center never invokes scanners, tools, reviewers, handoff ingestion, release publishing, git commands that mutate state, or remote APIs. Only `center report build`, `center report archive`, `center report diff --record`, and `center actions build/start/done/defer/archive` write local gitignored center files.
