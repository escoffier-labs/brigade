# Bounded Actions read importer, issue #1602

This slice adds a pure reader in `src/brigade/claude_actions_import.py`,
owner-boundary regressions in `tests/test_claude_actions_import.py`, and this
acceptance record. Issue #1602 remains open.

## Settled recipe

1. Reproduce the missing importer with the confirmation-boundary test before
   implementation. Existing normalizer tests cannot exercise GET boundaries.
2. Reuse `GetTransport` and `GetResponse` from `claude_review_import`. Default to
   an empty offline fixture transport, which reports unavailable resources.
3. Validate a canonical repository, numeric workflow ID, comparison head,
   observation time, limits, and bounded per-run-attempt confirmations before IO.
   Accept no definitions, YAML, arbitrary paths or URLs. Metadata, names and run
   SHAs cannot establish definition applicability.
4. Read workflow metadata and paginated workflow runs through fixed repository
   GET paths. Fetch older attempts from the documented numeric attempt path.
   Deduplicate identical identities and quarantine conflicting snapshots.
5. Check shared request/object/byte and per-endpoint page budgets, body size,
   JSON depth/node/integer bounds, and elapsed time before and after each read.
   Preserve denial, unavailable, malformed and truncation reasons separately.
6. Normalize only explicitly confirmed attempts with `normalize_run`. Preserve
   immutable definition revision, provider timestamps, source links and
   observation time. Keep current and stale heads separate. Export no raw names,
   titles, branch text, actors, provider errors or response bodies.
7. Run the required four-module focused gate through Brigade, capture its actual
   outcome, and hand the uncommitted diff and limitations to root.

The tests protect protocol, authority, resource and redaction contracts at the
public collector boundary. A reader that trusted names, ignored supplied byte
caps, defaulted attempts or overwrote a current head with a delayed stale record
would fail them. The injected transport is a production boundary, not a testing
seam. No existing tests or source files change.

Code graph sync and exact-path affected queries were attempted before edits.
The native code graph engine was unavailable. Graph coverage is skipped, not
evidence of no impact. Manual impact: the new reader imports
`claude_actions_evidence` and the transport types from `claude_review_import`.
The focused gate includes their tests and `claude_actions_diagnostics` tests.

## Remaining full acceptance

Root owns integration, changelog, review, full verification, Windows and CI
qualification, PR and merge. This candidate does not qualify those gates.
Supported authenticated live transports, independently resolved immutable
workflow definitions and applicability, YAML discovery, job/artifact metadata,
runner classification, persistence, cross-observation refresh ordering, CLI,
JSON command and FleetHub readouts remain open. Logs, transcripts and artifact
downloads remain excluded. Provider branch text is deliberately withheld.

The injected transport must enforce an actual blocking-call timeout and avoid
redirects, identity fallback and alternate routes. Elapsed checks can reject a
late response but cannot preempt a blocked call. There are no background threads
or daemons. The reader never reads credential files, probes secret availability,
installs apps, dispatches/reruns, posts comments or executes provider work.

Successful metadata is only a metadata observation. A confirmed attempt is an
execution observation, with unknown review/findings/merge readiness and hosting.
Neither establishes app access/installation, auth readiness, provider sessions,
health or capacity/admission. Complete means only the bounded requested snapshot
was read consistently, never complete absence of Claude configuration/execution.

Protocol shapes follow the supported GitHub REST endpoints for
[workflow metadata](https://docs.github.com/en/rest/actions/workflows#get-a-workflow),
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)
and [run attempts](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt).
Public documentation was checked during root integration; all collection tests
use offline fixtures.
