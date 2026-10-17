# Claude Action run evidence: first slice of #1602

`brigade.claude_actions_evidence` normalizes one offline GitHub Actions run
fixture. It adds no command, importer, storage, Hub schema or runtime dependency.
Issue #1602 remains open.

## Caller contract

`ConfirmedWorkflow(repository, workflow_id, revision)` carries a canonical
`owner/repo`, numeric workflow ID and full definition commit SHA. A future
workflow-discovery caller must confirm the actual Claude Action definition and
establish that this revision applies to the particular run being normalized.
Constructing this record validates syntax only. The normalizer cannot establish
that a workflow invokes `anthropics/claude-code-action` from a name, job label,
action ref or branch name.

`normalize_run(fixture, workflow=confirmation, observed_at=timestamp,
current_head_sha=head, runner_kind=hosting)` returns immutable typed evidence.
The provider fixture must supply matching `repository.full_name` and
`workflow_id`, plus positive integer `id` and `run_attempt`. Required identity
errors reject the record without echoing provider values. Unknown fields are
ignored, including titles, actors, commit messages, logs and transcripts.

The stable identity is `(canonical_repository, run_id, run_attempt)`. Refreshing
the same attempt preserves this identity. Callers must retain separate attempts
and decide how newer observations replace snapshots. This module provides no
history store, freshness ordering or deduplicating importer.

## Evidence semantics and limits

| Field | Meaning and bound |
| --- | --- |
| Workflow identity | Repository syntax bounded to 140 characters, positive ID below 2^63, 40 hexadecimal definition revision |
| Run identity | Positive run ID and attempt below 2^63, never defaulted to attempt 1 |
| Event and branch | Event up to 64 characters and branch up to 255, malformed or control-bearing text omitted |
| Head SHA | 40 hexadecimal characters, normalized to lowercase |
| Status | `queued`, `in_progress`, `completed`, `requested`, `waiting`, `pending`, or `unknown` |
| Conclusion | Explicit supported conclusion only with `completed` status, otherwise `unknown` |
| Provider times | Valid timezone-aware `created_at`, `run_started_at`, `updated_at`, normalized to UTC, otherwise absent |
| Observation time | Required caller timestamp, validated separately from provider times |
| URLs | Optional exact HTTPS GitHub run or attempt links for this repository and object, maximum 2048 characters |
| PR associations | First 20 input rows only, bounded positive numbers, optional head/base SHAs, canonical PR links |

Conclusion preserves `success`, `failure`, `cancelled`, `skipped`, `timed_out`,
`neutral`, `action_required` and `stale`. Missing or invalid status cannot become
completed from a conclusion or timestamp. A queued or running record carrying
a terminal conclusion retains an unknown conclusion.

`head_relation` compares the run SHA with the caller-supplied current comparison
SHA. It returns `current-head`, `stale-head` or `unknown`. PR associations never
promote a mismatched run SHA. A matching run SHA alone does not establish PR
review coverage. Merge commits and `pull_request_target` require caller-level
scope interpretation. `review_state`, `findings_state` and `merge_readiness`
remain `unknown`, including after successful execution.

PR association state describes only the supplied list: `complete` means its
rows were processed within the budget, `partial` marks invalid or duplicate
rows or truncation, and `unknown` means the list was missing or malformed.
An empty list is no proof about repository-wide PR association. The importer
must separately record endpoint scope, pagination and any exclusion settings.

The execution provider is `github-actions`. Optional caller-confirmed
`runner_kind` preserves `github-hosted` or `self-hosted` provenance and defaults
to `unknown`. The module never infers hosting from workflow or runner names and
never assigns an enrolled local node: `local_node_id` stays absent.

Only run and attempt read links are accepted. Other origins, credentials,
ports, query strings, fragments, encoded paths, mismatched IDs and mutation
routes are omitted. Canonical PR links are constructed from validated repository
and PR identities. Nothing follows these links.

## Remaining #1602 acceptance

- Cross-refresh history, refresh ordering and idempotent persistence for run,
  job and artifact observations. Persistence and CLI/FleetHub readouts remain
  open. Keep Actions records separate from managed review, Claude cloud adoption
  and cloud capacity/admission.
- General YAML normalization beyond caller-supplied mappings and provider auth
  readiness require supported evidence. Artifact downloads, logs and transcripts
  remain excluded.

No setup step, app installation, workflow/secret write, paid review, dispatch,
rerun or trigger comment is implemented.

## Sources and verification

Sources retrieved on 2026-10-06. This is a retrieval date, not a publication
date.

[Claude Code GitHub Actions](https://code.claude.com/docs/en/github-actions)
documents the action and setup context. The
[GitHub workflow-runs REST reference](https://docs.github.com/en/rest/actions/workflow-runs)
provides the run/attempt metadata contract. Discovery and importer correctness
remain future verification responsibilities.

Regression tests live in `tests/test_claude_actions_evidence.py`. The focused
development gate is `./scripts/verify-focused tests/test_claude_actions_evidence.py`,
run through Brigade with `--capture brigade-work`. The full gate is required
before publication and merge.
