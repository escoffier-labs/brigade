# CI board

`/deck/ci` shows cached default-branch heads, the latest default-branch workflow
run, required checks for each exact SHA, and open PR heads and rollups. It uses
the existing Deck operator authentication. It cannot rerun workflows, comment,
dispatch, or change GitHub state. GitHub remains authoritative for merging.

## Populate from a node

Configure the existing fleet Hub URL and this machine's enrolled node token.
CI observation writes never fall back to the admin token.

```sh
brigade fleet ci reconcile --target /path/to/workspace --json
```

The repository set combines only the current Hub policy inventory with enabled
`.brigade/repos.toml` entries. Local entries use the existing origin identity
resolver. Stored historical CI rows do not select repositories. Unknown visibility uses native `gh`, including the first metadata
read. Confirmed public repositories use Octopool without local fallback.

A node bearer receives only confirmed public Hub repositories. Private and
unknown names are operator-only. A node can reconcile its own private local
inventory without receiving another machine's private inventory. For a first
policy discovery or private policy-only repository, explicitly use the existing
configured admin bearer for the read plan:

```sh
brigade fleet ci reconcile --target /path/to/workspace --operator-plan --json
```

A metadata failure demotes a previously public repository to unknown visibility and removes it from ordinary node plans. Recover it through local inventory or run `reconcile --operator-plan` on the trusted operator node.

Private refresh requests also require `--operator-plan`, including private repositories in local inventory. Node work plans return refresh keys only for confirmed public policy repositories. No private-name hashes are exposed to node readers.

All resulting observation POSTs still use the node token. Do not distribute an
admin token merely to run CI collection. Operator-authorized discovery belongs
on a trusted operator node.

Reconcile runs on demand, skips repositories attempted within the last hour,
and caches successfully observed required-check sets for 24 hours. Failed required-check reads retry on the next hourly-due attempt, subject to the ledger. A matching refresh is consumed after any successfully persisted attempt, including an incomplete attempt. Its reason remains visible. There is no new daemon or
scheduler. Repeatedly invoke the command from an existing authorized care flow
if continuous collection is needed. The local cache defaults to
`BRIGADE_HOME/ci-node.db` (the usual Brigade home when unset). `--state` selects
another private cache. Keep that directory outside version control.

All GitHub reads use `GitHubReader` and the adjacent shared
`github-read-budget.db` ledger. The native reader's ETags and response bodies persist across CLI invocations in the private node database, bounded to 128 entries per repository and 16 MiB of response bodies across the shared cache. The database is restricted to its owner. Public Octopool reads retain the relay's cache behavior. Admin refresh does not bypass that ledger,
upstream backoff, or the daily required-check cache. One repository repairs at
most 21 heads, two pages of 100 records per collection, 1,000 normalized events,
and one latest workflow. Reaching a bound leaves the affected scope unknown.
403, 404, 429, malformed responses, missing permissions, and incomplete pages
also remain unknown. Required checks combine classic branch protection and
effective branch rules. A successful branch response with explicit `protected: false`, together with a successful empty effective-rules response, establishes an empty required set. Classic protection 404 alone never establishes that fact. A protected branch with inaccessible classic checks stays unknown. GitHub documents that branch protection includes both classic protections and rulesets in its [branch API](https://docs.github.com/en/rest/branches/branches#list-branches), while the [protection API](https://docs.github.com/en/rest/branches/branch-protection#get-branch-protection) requires Administration read permission. These primary docs were retrieved on 2026-10-22, not treated as publication dates. Empty, successfully observed required sets display
`none-required`, not `passing`.

## Pull relay events

The relay from issue #1874 is not deployed. This interface is fixture-tested.
Provision its read-only bearer outside the repository, then set
`BRIGADE_CI_RELAY_URL` to its HTTPS origin and `BRIGADE_CI_RELAY_TOKEN` to that
bearer in the collector environment.

```sh
brigade fleet ci pull --limit 100 --json
```

Each invocation requests one `GET /v1/events?after=<cursor>&limit=<n>` page,
with a limit of 1 through 100. The compatibility fixture is generated from
relay PR #1940 head `2e87f6d030e4382c4d0e44265b6102b031f54ad4`, using
its actual `normalizeEvent` function and the `listEvents` response shape:

```json
{"events": [{"cursor": 1, "delivery_id": "example-delivery", "repository": "example/project", "event": "workflow_run", "action": "completed", "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "status": "completed", "conclusion": "success", "run_id": "11", "run_attempt": 2, "pr_number": null, "provider_created_at": "2026-10-21T12:00:00Z", "provider_updated_at": "2026-10-21T12:01:00Z", "received_at": "2026-10-21T12:01:01Z"}], "next_cursor": 1}
```

The adapter maps `repository` to the Hub repository field and provider timestamps
to evidence timestamps. It converts bounded positive decimal IDs within SQLite's
signed integer range, maps the shared relay `run_id` to workflow/check/suite
identity, and uses `pr_number` for PR identity. A null attempt becomes 1.
Delivery IDs, arbitrary payload bodies, logs, actor data, and extra fields are
not forwarded. Page cursors must increase, and `next_cursor` must equal the last
record cursor (or the input cursor on an empty page). The local cursor advances numerically and cannot rewind after overlapping pulls. The collector supplies an opaque relay-origin key and each record cursor as transport receipt metadata. The Hub retains per-repository relay watermarks in the same transaction as observations. Replaying an acknowledged page does not renew observation time, last-event time, or invalidation. These receipt keys are not provider object identities. Invalidation barriers retain at most 1,000 head entries per repository; excess entries coalesce to a repository-wide unknown barrier at the latest receipt time. Legacy batches without receipt metadata remain accepted and conservatively replayable. After an intentional relay cursor epoch reset, coordinate operator maintenance across collectors: pause pulls, clear that relay's local `ci_cursor` entries and the corresponding Hub `ci_relay_watermarks` entries, then reconcile before resuming pulls. This maintenance resets both sides of the cursor namespace.

PR #1940 does not provide check context names or app IDs. Those events cannot
establish required or PR check rollups. The current Worker also emits no provider
ID for status events, and normal check-run webhooks may lack the provider
creation/update timestamps it selects. No ID or provider time is invented.
A scoped unusable event becomes a durable invalidation. Normal hourly-due reconciliation repairs it; relay activity does not create an urgent refresh request.
A nameless lifecycle observation preserves any name and app identity already established for that provider object and attempt. An object without established identity cannot pass a rollup. The affected scope remains unknown until reconciliation.
An unsupported 64-hex SHA also invalidates scope rather than being represented as
a supported evidence SHA.

The Hub atomically persists valid observations, marks affected check scope
unknown, and preserves the invalidation for hourly repair. Only after the Hub acknowledges both the event and
invalidation counts does the node persist `next_cursor`. Thus a malformed
status record cannot block every subsequent page or retain old green checks.
A successfully persisted pull exits 0, including when its `incomplete` count reports repair-needed observations. Transport or acknowledgement failures exit 2. Reconcile retains exit 1 for incomplete collection.
A missing/invalid repository cannot be safely scoped, so that page is refused
without cursor advancement. Relay or operator correction is required in that
case. Reordered/mismatched cursors are also refused.

Required and PR rollups therefore depend on bounded reconciliation. Workflow
and PR identities can update from usable relay events. A relay workflow can
enter the default panel only for the exact current default SHA. Other workflows
remain in history with unknown branch attribution. Only a known default-branch
run on an older head is labelled `stale-head`.

The scalar repository, SHA, object ID, and timestamp validators reuse existing
evidence code. The managed Claude review normalizer is not called for generic CI.

## Refresh and read API

- `GET /ci`: cached facts. Admin sees all repositories. An enrolled node sees
  public repositories only. Anonymous requests are refused.
- `GET /ci/work`: inventory, pending admin refresh keys, and a Hub `as_of` repair-start marker. Admin sees the full
  inventory and all refresh keys. Nodes see confirmed public policy names and
  only those repositories' refresh keys. Private refresh discovery is operator-only.
- `POST /ci`: bounded event/repair batches, enrolled node token required.
- `POST /ci/refresh`: `{ "repo": "example/project" }`, admin bearer required.
  Returns 202 and queues local database work. A subsequent node reconciliation
  consumes the matching request after any successfully persisted attempt. A newer refresh
  request cannot be erased by acknowledgement of an older one.

Only admin refresh requests bypass the hourly due bound. Relay invalidations mark scope unknown without adding a refresh request.

Before each repository's first provider read, the node computes `repair_started_at` from the Hub work-plan `as_of` plus nonnegative elapsed monotonic time since receiving the plan. Long inventory sweeps therefore use a distinct start marker for each repository without using the node wall clock. The Hub preserves any invalidation received at or after that marker, even if a repair POST arrives later. A later repair can clear it. Once a stored snapshot has a marker, an older, equal, or missing incoming marker leaves that snapshot, its barriers, and refresh acknowledgement unchanged. The batch still persists provider objects and acknowledges repository counts. Legacy snapshots remain accepted when no stored marker is known.

The Hub makes no outbound calls, including refresh. Browser sessions read the
board through existing Deck authentication. The board contains no mutation
form. Refresh uses the admin bearer API rather than browser cookies.

Facts carry source, observation time, and freshness. Head and rollup freshness
expires after one hour. A known default-branch workflow completion for an older head remains marked
`stale-head`. Neutral and skipped checks do not pass. Last event received and
last reconciled are separate, so relay delivery can be distinguished from
periodic repair. Reconciliation alone can populate the board after dropped
webhook delivery.

## Storage and trust

The Hub accepts metadata attestations from enrolled, trusted collector nodes.
Repository visibility is such an attestation, not independently verified by the
Hub. A malicious enrolled node could misclassify a name it already knows.
Operators should enroll collectors that are trusted to handle their repository
inventory. Missing or failed metadata classification becomes `unknown`, which
is hidden from node reads. A successful private classification stops further
Octopool calls for that repair.

Hub receipt order separates authoritative snapshots from prior check deliveries,
so node clock skew cannot revive a removed successful check. Observation times
still show the collecting node's time and can indicate unknown freshness when
its clock is ahead. Workflow state cannot regress from running to queued at an
equal provider timestamp. `startup_failure` is retained as a known failed conclusion by the CI board without changing shared evidence normalizer APIs.

On ingestion the Hub expires provider rows older than 30 days when their SHA is
outside current default/PR heads. Each repository retains at most 100 workflow
attempts and 1,000 check/PR observations. Caps prune unreferenced heads before current default/PR heads. Workflow history pruning does not erase check completeness. If the check cap must trim current-head observations, only those affected heads become unknown; untouched heads retain their completeness. Each view reads at most 200 repositories,
20 workflow attempts per repository, and 1,001 check rows for at most 21 current
heads. Exceeding a read bound leaves scope unknown. Rows outside the selected
inventory are history, not automatic reconciliation targets.
