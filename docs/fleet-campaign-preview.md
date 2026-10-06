# Offline fleet campaign preview

`brigade fleet campaign preview` prints a read-only JSON membership plan. It
selects repositories through the existing `.brigade/repos.toml` selector and
binds each selected opaque repository ID to an explicit existing task or
reviewed action reference. It reads no Hub state, member task ledgers, provider
credentials, prompts or artifacts. It creates no campaign registry and makes
no claims, imports, enqueues or dispatches.

```bash
brigade fleet campaign preview --target . --campaign-id campaign-a \
  --repos repo-a,repo-b --bindings bindings.json --json
```

`--repos` accepts comma-separated exact configuration IDs. Without it, the
command selects every enabled repository. Disabled IDs and missing IDs refuse
an explicit selection. There are no named repository sets. Bindings must cover
the selected membership exactly, with one binding per repository. Identical
or conflicting duplicate bindings refuse the preview. Repository paths and
labels are excluded from the public envelope.

The bindings file is a JSON list. Each object contains exactly these fields:

```json
[
  {
    "repo_id": "repo-a",
    "authority_kind": "task",
    "authority_id": "task-a",
    "authority_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "spec_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
]
```

Use `authority_kind: "action"` for an existing fleet action ID and its exact
source fingerprint. For tasks, use the explicit repository-local task ID and
the fingerprint of the bound task authority. `spec_fingerprint` binds the
exact requested specification. Obtain these references from their existing
task/action authorities. This command does not resolve, verify, create or
reconcile them. The example digests are placeholders. Supply real SHA256
digests locally without putting task or specification contents in the file.

The membership fingerprint covers sorted repository keys, authority kinds,
exact IDs, authority fingerprints and specification fingerprints. Reordering
changes no fingerprint. Paths, basenames, campaign names and latest runs never
select or bind a member. Keys must be 1 to 128 ASCII letters, digits, dots,
underscores, colons or hyphens, starting with a letter or digit. `latest` is
refused. Keys are intended to be opaque: never encode private text or secrets.
Fingerprints must contain exactly 64 lowercase hexadecimal characters.

Pass `--prior prior-preview.json` to compare an explicitly retained preview.
The prior envelope's own immutable binding fingerprint is checked before
comparison. A different campaign ID or changed membership, authority binding
or specification refuses the comparison. Without a prior envelope,
`prior_comparison` is `not_checked`. Conflict detection covers only the supplied
prior envelope. There is no durable registry or global conflict check.

All members have `task_state: "unknown"` and `launch_safety: "unknown"` and
remain `resume_candidates`. `summary.done` is always false. These candidates
are membership planning output, with no authorization to launch. Terminal
exclusion requires explicit matched authoritative task state, which this
offline slice does not read. An execution success, failure, returned PR,
provider session or supplied terminal report cannot complete a task.

An optional `--observations observations.json` accepts a JSON list of strictly
typed fixture records, each with exactly these fields:

```json
[
  {
    "repo_id": "repo-a",
    "authority_id": "task-a",
    "authority_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "task_status": "done",
    "execution_status": "succeeded",
    "attempt_id": "attempt-a",
    "fresh": true,
    "complete": true,
    "job_key": "job-a",
    "artifact_key": "artifact-a"
  }
]
```

These records have `supplied_unverified` provenance. Task reports and execution
statuses remain separate evidence fields. Allowed task reports are `pending`,
`in_progress`, `blocked`, `deferred`, `done`, `cancelled`, `dismissed` and
`unknown`. Execution statuses are `unknown`, `running`, `succeeded` and `failed`.
`fresh` and `complete` must be booleans. They are fixture assertions, with no
independent freshness verification. Truncated fixtures must set `complete`
false. Job and artifact keys are opaque references or null, including Grok Bot
references. Artifact contents, URLs and prompts are refused.

Identical observations are deduplicated. Multiple attempts never increase
repository membership counts. Missing, stale, incomplete, mismatched and
conflicting evidence stays unknown. Even fresh matching terminal reports
remain unverified and exclude no members. Observations for other repository
keys are unused. The preview exposes no authoritative pending-task, session,
Actions, review or completion rollup.

Membership is bounded to 256 repositories, observation input to 1024 records,
and each supplied JSON file to 256 KiB. Malformed inputs, extra fields and
unsafe references return exit 2 with fixed JSON refusal codes containing no
input values. A valid plan returns exit 0. This describes preview validity,
not task completion or safe launch. JSON is also the default without `--json`.

Issues #1128 and #1121 remain open for persistent Hub grouping, cross-machine
fan-out/resume, duplicate prevention, dashboard integration and Grok Bot
campaign propagation. Exactly-once or two-conductor dispatch is outside this
preview. Future persistence must follow #1576's offline schema25 work and bind
existing Worklore/item authorities, per-member operation keys, response-lost
reconciliation, explicit retries and canonical cross-machine repository
identity with fail-closed claim authority. The legacy late-acquire race in
#1189 and local claim fallback after Hub failures remain launch constraints.
