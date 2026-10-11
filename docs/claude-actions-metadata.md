# Claude Actions metadata snapshots

`brigade.claude_actions_metadata.collect_metadata` reads bounded job and artifact
metadata for one independently confirmed Actions run attempt. This passive slice
contributes to issue #1602. It does not complete the full issue.

```python
collect_metadata(
    transport=None,
    *,
    run: ActionRunEvidence,
    current_head_sha: str,
    observed_at: str,
    limits: claude_actions_import.Limits | None = None,
    clock=time.monotonic,
    runner_kinds: Mapping[int, str] | None = None,
)
```

The caller supplies `ActionRunEvidence` with an independently confirmed
`ConfirmedWorkflow`, including its exact definition commit SHA. Construction
validates syntax. The caller remains responsible for confirming that the
definition applies to that run attempt. Workflow, job and artifact names cannot
establish Claude applicability. Required identities, current comparison SHA,
observation time, limits and runner attestations are validated before any GET.

## Transport and budgets

The default `FixtureTransport` is offline. Missing fixtures are unavailable.
An injected `GetTransport` may read only the fixed paginated paths:

- `/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs`
- `/repos/{repository}/actions/runs/{run_id}/artifacts`

Each request receives `page`, `per_page` and a response byte cap. The transport
must enforce authorization, blocking timeouts, bounded reads, no redirects and
no identity fallback. Provider links never control a request. The collector
cannot interrupt a blocking GET. Its elapsed checks reject late responses.

The collector reuses `claude_actions_import.Limits` and `_Reader`, including
response, JSON depth, node, integer, duplicate-key and byte safeguards.
An out-of-range JSON integer anywhere in a page rejects the whole page as
`malformed-payload` before object quarantine.
Page budgets apply separately to each endpoint. Request, object, byte and elapsed
budgets apply across both endpoints. A denied endpoint does not prevent the
other endpoint from being read within the remaining shared budgets.

## Attempt jobs and run artifacts

Jobs use identity
`github-actions-job:{repository}:{run_id}:{attempt}:{job_id}`. Each provider job
must match the supplied run ID and any supplied attempt. The attempt endpoint
supplies the attempt when the body omits it. If valid known job and supplied run
heads differ, the job is quarantined. Jobs retain the caller's exact definition
revision, normalized provider timestamps and the collection observation time.
Head relation is recomputed against the supplied current comparison SHA.
Execution status and conclusion describe execution only. Job metadata cannot
prove which step ran Claude, review completion, findings or merge readiness.

Artifacts use identity `github-actions-artifact:{repository}:{artifact_id}`.
Their provider `workflow_run.id` must match the supplied run ID. If both provider
and supplied run heads normalize to valid SHAs, they must match or the artifact
is quarantined as `object-identity-mismatch`. Unknown heads remain unknown.
Artifacts are run scoped: `run_attempt` and `definition_revision` stay `None`, and
`attempt_association` stays `unknown`. Their own provider run head is compared
independently to the current comparison SHA. The queried attempt never supplies
artifact provenance.

Only actual boolean expiry values survive. `availability` is `expired` for
`True`, otherwise `present`, describing an observed metadata record.
`content_availability` and `deletion_state` remain `unknown`. Bounded nonnegative
size and normalized creation, update and expiry times survive. No archive URL
or artifact content is read or exported.

The scope follows GitHub's
[attempt jobs endpoint](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
and [run artifacts endpoint](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts).
The parent retrieved these documents through PageForge at 2026-10-11T02:37Z.
No publication date was supplied by those pages. The artifacts endpoint provides
run metadata without a run attempt binding.

GitHub's [rerun documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
states that reruns retain the original `GITHUB_SHA` and `GITHUB_REF`, supporting
the run identity check while leaving artifact attempt association unknown.
The parent retrieved this document through PageForge at 2026-10-11T02:49:28Z.
The page supplied no publication date.

## Runner provenance and redaction

Optional `runner_kinds` maps bounded provider job IDs to `github-hosted`,
`self-hosted` or `unknown`. Known supplied kinds carry
`runner_authority=caller-confirmed-job-runner`. All other hosting authority is
`unknown`. Provider labels, runner names and runner group IDs provide no hosting
authority. Every `local_node_id` stays `None`.

The output omits names, labels, step and script values, environment, logs,
branch text and runner details. Optional malformed statuses, timestamps and
URLs become `unknown` or `None`. Job API links must match the exact repository
and job ID. Job HTML links accept both `/{repository}/runs/{run_id}/jobs/{job_id}`
and `/{repository}/actions/runs/{run_id}/job/{job_id}`. Artifact API links must
match the exact object metadata path. Links require HTTPS, the expected GitHub
host, and no query, fragment, credentials or port. Download and log paths are
excluded. Error bodies, exception messages and raw payloads are never exported.

## Completeness and snapshot identity

The JSON-compatible result includes `jobs`, `artifacts`, endpoint states,
`sources`, `usage`, `limits`, `incomplete_reasons`, `quarantined`,
`duplicate_objects` and `current_head_job_identities`. Records carry authority,
scope, source URL, revision and observation time separately. Current head job
identities establish only the SHA comparison.

Traversal validates total counts, list and object types, page cardinality,
stable totals, unique IDs and repeated pages. Raw JSON equality ignores key
order and preserves boolean versus integer distinctions. Identical objects
deduplicate within this snapshot. Conflicting objects quarantine their entire
provider identity, with no last writer replacement. Invalid scope bindings use
`object-identity-mismatch`. Conflict reasons use `conflicting-identity`.
Already accepted valid observations survive partial failures.

Complete nonempty endpoints are `confirmed`. A complete empty requested
snapshot is `absent-in-complete-scope`. Partial, malformed or budget limited
endpoints are `unknown`. HTTP denial, missing resources and rate limiting map
to `access-denied`, `unavailable` and `rate-limited`. A 404 cannot prove deletion.
Any incomplete endpoint makes global `complete` false. Global `absence`,
`review_state`, `findings_state`, `merge_readiness` and `deletion_state` remain
`unknown`, including after successful empty snapshots.

This API has no persistence, cross-refresh ordering, live discovery, CLI wiring,
capacity accounting or FleetHub writes.
