# Repo Fleet

`brigade repos` inspects configured local repositories from gitignored `.brigade/repos.toml`. It is a local operator workflow for safe labels, readiness checks, report bundles, and reviewed fleet actions.

Commands:

```bash
brigade repos init
brigade repos list
brigade repos show <repo-id>
brigade repos scan
brigade repos doctor
brigade repos import-issues
brigade repos discover plan
brigade repos health-commands
brigade repos sweep plan
brigade repos sweep run
brigade repos sweep runs
brigade repos sweep show <sweep-id>
brigade repos sweep closeout <sweep-id|latest>
brigade repos report plan
brigade repos report build
brigade repos report list
brigade repos report show <report-id>
brigade repos report archive <report-id>
brigade repos report closeout <report-id>
brigade repos actions plan <report-id>
brigade repos actions build <report-id>
brigade repos actions list
brigade repos actions show <fleet-action-id>
brigade repos actions start <fleet-action-id>
brigade repos actions done <fleet-action-id>
brigade repos actions defer <fleet-action-id> --reason "not today"
brigade repos actions archive --completed
brigade repos actions dispatch plan <fleet-action-id>
brigade repos actions dispatch apply <fleet-action-id>
brigade repos actions dispatch report <fleet-action-id>
brigade repos actions dispatch --all-reviewed
brigade repos actions reconcile [fleet-action-id]
brigade repos actions context plan <fleet-action-id>
brigade repos actions context build <fleet-action-id>
brigade repos release plan
brigade repos release build
brigade repos release list
brigade repos release show <train-id>
brigade repos release compare <train-id|latest>
brigade repos release reconcile <train-id|latest>
brigade repos release summary <train-id|latest>
brigade repos release report <train-id|latest>
brigade repos release checklist <train-id|latest>
brigade repos release hygiene
brigade repos release import-issues <train-id|latest>
brigade repos release ready <train-id|latest>
brigade repos release activity <train-id|latest>
brigade repos release manifest <train-id|latest>
brigade repos release audit <train-id|latest>
brigade repos release closeout <train-id|latest>
brigade repos release archive <train-id>
brigade repos release actions plan <train-id|latest>
brigade repos release actions build <train-id|latest>
brigade repos release actions list
brigade repos release actions show <release-action-id>
brigade repos release actions start <release-action-id>
brigade repos release actions done <release-action-id>
brigade repos release actions defer <release-action-id> --reason "not today"
brigade repos release actions archive --completed
brigade repos release evidence plan <train-id|latest>
brigade repos release evidence record <train-id|latest> --repo <repo-id> --step verification --status completed
brigade repos release evidence list
brigade repos release evidence show <evidence-id>
brigade repos release waivers record <train-id|latest> --scope missing-evidence --reason "reviewed"
brigade repos release waivers list
brigade repos release waivers show <waiver-id>
brigade repos release waivers revoke <waiver-id> --reason "risk changed"
brigade repos release waivers renew <waiver-id> --reason "reviewed again" --expires-at <timestamp>
brigade repos release waivers doctor <train-id|latest>
brigade repos release waivers import-issues <train-id|latest>
```

Fleet reports are written under:

```text
.brigade/repos/reports/
```

Fleet action queues are written under:

```text
.brigade/repos/actions/
```

Fleet sweep receipts are written under:

```text
.brigade/repos/sweeps/
```

Fleet release train bundles are written under:

```text
.brigade/repos/releases/
```

Fleet reports include safe repo ids, safe labels, status counts, blocker and warning counts, top pending action summaries, receipt labels, and suggested next commands. Fleet actions store local metadata only: repo id, safe label, source subsystem, source local id, status, priority or severity, safe summary, suggested command, timestamps, and source fingerprint.

`brigade repos discover plan` is a dry-run discovery command for explicit `[[discovery_root]]` entries in `.brigade/repos.toml`. It scans only configured roots, applies `include`, `exclude`, `max_depth`, and `enabled`, reports safe root-local candidate labels, and never clones, writes repo config, runs git commands, or scans arbitrary home directories by default. Absolute paths and discovered private directory names are not copied into the JSON output.

`brigade repos sweep plan` shows which configured repos would be refreshed and which read-only local Brigade commands would run. `brigade repos sweep run` executes the configured foreground refresh for selected repos, records per-repo command summaries, and stores raw stdout and stderr only in gitignored local logs. Receipt JSON uses repo ids, safe labels, command labels, status counts, fingerprints, and local log labels. It does not store exact repo paths.

Repos may define optional read-only health commands in gitignored config:

```toml
[[repo.health_command]]
label = "local-health"
command = "python3 -m brigade work brief --json"
timeout = 120
```

Health commands are parsed into argv and run without a shell. High-risk shell, remote-copy, and metacharacter-heavy command shapes are rejected before a sweep runs.

`brigade repos health-commands` is the read-only registry view for optional health commands. It lists safe repo ids, command labels, timeouts, latest sweep receipt status, stale receipt warnings, failed command receipt warnings, and local log labels. It redacts argv details and does not read raw logs. Fleet reports include the same registry summary so stale or failed optional health commands are visible before release coordination.

Sweep filters include `--repo <repo-id>`, `--all`, `--stale-only`, `--include-disabled`, and `--force`. `brigade repos sweep closeout` marks a sweep as `reviewed`, `deferred`, `superseded`, or `archived` after the operator has inspected the refreshed evidence. Fleet reports, center status, center reviews, work brief, work doctor, and release doctor surface stale, failed, or unclosed fleet sweeps.

`brigade repos actions build` requires the source fleet report to be closed out as `reviewed` or `deferred` unless `--allow-unreviewed` is passed. Repeated builds dedupe by repo id, report fingerprint, and source item fingerprint, including archived actions from the same report.

`brigade repos actions dispatch` bridges fleet-level review into the target repo's local work loop. `dispatch plan` previews the task import that would be written. `dispatch apply` writes a `source: repo-fleet` task import into the target repo's existing `.brigade/work/imports/inbox.jsonl`, with acceptance criteria and fleet provenance. `dispatch --all-reviewed` applies the same path to reviewed pending or active actions. `--dry-run` writes nothing.

Dispatch is idempotent by fleet action id and source fingerprint. Repeated dispatch of the same action skips equivalent pending or promoted imports. Dismissed target imports stay dismissed until the source fingerprint changes. When the fingerprint changes, Brigade creates a new target import and marks prior dispatch imports superseded.

`brigade repos actions dispatch report <fleet-action-id>` explains dispatch history, target import status, dismissed target imports, superseded imports, changed fingerprints, and broken references. `--record` writes a local report receipt under `.brigade/repos/actions/dispatch-reports/`. The report is read-only with respect to target repos and never promotes imports or runs suggested commands.

`brigade repos actions context plan/build` creates an action-scoped context pack in the target repo under `.brigade/context/packs/`. These packs include safe action summary, acceptance criteria, guidance presence, local receipt labels, and explicit private-evidence exclusions. They do not copy raw guidance contents, raw logs, raw scanner output, private paths, exact private repo names, owner names, org names, hostnames, or secrets.

`brigade repos actions reconcile` reads target repo work imports, promoted tasks, completed tasks, closeouts, release readiness receipts, and operator reports, then updates local fleet action metadata. Reconciliation states include `dispatched`, `in-progress`, `completed`, `dismissed`, `superseded`, `stale`, and `broken-reference`. Completed target tasks mark the fleet action done. Repo fleet health also warns when safe target evidence changes after dispatch. No suggested command is executed.

`brigade repos release plan/build/list/show/compare/closeout/archive` coordinates release readiness across configured repos without publishing anything. A release train collects safe per-repo evidence from fleet sweeps, fleet reports, fleet action reconciliation, target repo operator reports, work closeouts, verification receipts, review closeouts, security closeouts, release readiness receipts, release candidates, dirty tracked counts, and ahead or behind labels when available.

Each repo is classified as `ready`, `blocked`, `needs-review`, `needs-dispatch`, `in-progress`, `stale-evidence`, `no-release-candidate`, or `deferred`. `build` writes `FLEET_RELEASE_TRAIN.md`, `FLEET_RELEASE_EVIDENCE.json`, and `MANUAL_PUBLISH_PLAN.md`. The publish plan contains placeholders and manual-only checklist steps for verification, release doctor, candidate compare, tags, pushes, and release creation. Brigade does not execute any publish step.

`brigade repos release compare <train-id|latest>` checks whether captured repo HEAD labels changed, newer release readiness or candidate receipts exist, fleet action reconciliation changed, referenced safe receipt ids disappeared, or unresolved fleet action state changed. `closeout` records `reviewed`, `deferred`, `superseded`, or `archived` status. Repo doctor, center status, center reviews, work brief, work doctor, and release doctor surface blocked, stale, or unclosed release train state.

`brigade repos release actions plan/build/list/show/start/done/defer/archive` turns a reviewed or deferred release train into a local action queue under `.brigade/repos/releases/actions.json`. Actions are created for repos that are blocked, need review, need dispatch, are in progress, have stale evidence, lack a release candidate, or are deferred. They are metadata records only and never execute suggested commands.

`brigade repos release evidence plan/record/list/show` records manual publish evidence under `.brigade/repos/releases/evidence.jsonl`. Evidence steps are `verification`, `release-doctor`, `candidate-compare`, `tag`, `push`, `release`, and `other`. Statuses are `completed`, `skipped`, `blocked`, and `deferred`. These records describe what the operator did manually; Brigade does not run verification, tag, push, or release commands.

`brigade repos release reconcile <train-id|latest>` compares release-train actions with manual evidence records. An action is marked done only when the repo has required evidence for verification, release doctor, candidate compare, tag, push, and release, and none of those records are blocked. Completed, skipped, and deferred evidence all count as reviewed operator outcomes. Missing or blocked evidence keeps the action open. `brigade repos release summary <train-id|latest>` reports per-repo evidence status, unresolved action counts, and suggested next commands. Release train closeout includes summary counts when available.

`brigade repos release report <train-id|latest>` writes `RELEASE_TRAIN_REPORT.md` and `RELEASE_TRAIN_REPORT.json` into the train bundle. `checklist` prints the required evidence rows for each repo. `hygiene` reports unclosed, stale, or missing-report trains. `import-issues` routes missing or blocked release evidence into the local work inbox as `source: repo-fleet-release`. `activity` shows a chronological local ledger for train creation, closeout, actions, evidence, waivers, reports, and manifests. `manifest` writes `RELEASE_TRAIN_MANIFEST.json` with bundle file labels and fingerprints. `audit` checks for missing bundle files, stale manifests, open actions, and unresolved release evidence.

`brigade repos release ready <train-id|latest>` is a local manual-publish gate that fails when the train has blocked repos, unresolved train actions, missing evidence, or blocked evidence. `brigade repos release waivers` can record explicit local waivers for `blocked-repo`, `unresolved-action`, `missing-evidence`, and `blocked-evidence` scopes. Active waivers are included in ready output and can allow the ready gate to pass without hiding the underlying counts. Expired waivers do not satisfy the ready gate.

Waivers can include `--expires-at <timestamp>`, be renewed with `renew`, and be revoked when risk changes. `waivers doctor` reports expired waivers, waivers with no expiry, stale reviews, and waivers tied to older train fingerprints. `waivers import-issues` routes waiver follow-up into the local work inbox as `source: repo-fleet-release-waiver` with stable fingerprints and dismissed-until-changed behavior. Waivers are local metadata only.

Privacy boundaries:

- No cloning.
- No remote mutation.
- No push, tag, release, PR, visibility, transfer, or archive mutation.
- No automatic action execution.
- No automatic promotion or dismissal.
- No automatic target task promotion, work run, or code fix.
- No exact private repo names, owner names, org names, hostnames, local paths, raw logs, scanner output, private config contents, secrets, or raw evidence in public files.
- Gitignored local config may store local paths and safe labels.
