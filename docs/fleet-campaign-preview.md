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
select or bind a member. Keys must be 1 to 128 ASCII letters, digits or
the punctuation `._:-`, starting with a letter or digit. `latest` is
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
preview. The current Hub code declares schema version 25 in
`src/brigade/fleet_hub.py`. Issue #1576 covers hosted-worker concurrency policy.
Future persistence must bind existing Worklore/item authorities, per-member
operation keys, response-lost reconciliation, explicit retries and canonical
cross-machine repository
identity with fail-closed claim authority. The legacy late-acquire race in
#1189 and local claim fallback after Hub failures remain launch constraints.

## Exact local action inspection

`brigade fleet campaign inspect` reads existing local action/import/task
lineage. It uses the same enabled `repos.toml` selection as preview. It never
initializes a workspace, contacts the Hub, creates imports, changes claims or
dispatches work. Preview behavior and its resume candidates remain unchanged.

```bash
brigade fleet campaign inspect --target . --campaign-id campaign-a \
  --repos repo-a --references references.json --json
```

The references file contains one exact action reference per selected member:

```json
[
  {
    "repo_id": "repo-a",
    "action_id": "action-a",
    "source_fingerprint": "aaaaaaaaaaaaaaaa"
  }
]
```

References accept exactly these three fields. Fingerprints follow the existing
storage grammar: 16 or 64 lowercase hexadecimal characters. Preview bindings
still require 64 characters. Input is limited to 256 references and 256 KiB.
Each local ledger snapshot is limited to 4 MiB, including growth during reads.
Reads retain file descriptors anchored at the configured workspace or
repository root. Opens are nonblocking on POSIX. Child directories and files
refuse symlinks and Windows
reparse points. Only regular files are read. Descriptor and recorded-name
checks detect replacement, growth and observed generation changes. Parsing
uses those same bounded bytes without compatibility readers or temporary
snapshot files. Opaque keys use preview's key
grammar and must never encode private contents or secrets.

Inspection requires an exact action ID and repository ID, its explicit stored
fingerprint and a recorded review timestamp. The dispatch fingerprint must
match before inspection follows its exact `target_import_id`. That import
must have matching repo-fleet action, repository and fingerprint metadata. A
promoted import leads only to its recorded exact task ID. The task must have
source `import:repo-fleet` and exact `import_id`, `import_source`, `import_kind`,
`fleet_action_id`, `repo_id` and `source_fingerprint` metadata linking it back
to that promoted import. An unrelated task or changed provenance reports
`task_mismatch`, with no local task observation.

If recorded, the action's `target_import_id` and `target_task_id` and the
dispatch's `target_task_id` must agree with the chain. Contradictions report
`lineage_mismatch`. Action status must be a known non-archived action state.
Missing or junk states report `action_status_invalid`. Archived or superseded
actions report `action_superseded`. Recorded resolution states may be
`dispatched`, `in-progress` or `completed`, and dispatch status may be
`dispatched`. Negative states (`superseded`, `dismissed`, `stale`,
`broken-reference`) and unknown recorded states report `lineage_mismatch`.
These states confer no launch or completion authority.

Prefixes, newer imports, synthesized fingerprints and task searches never
repair missing lineage. Missing, duplicate, corrupt, superseded, mismatched or
changed lineage stays unknown with a fixed reason code. There is no archive
fallback: an archived import absent from the current import ledger reports
`import_missing`. Pending imports report `import_not_promoted`, dismissed
imports report `import_dismissed`, and unknown or missing import states report
`import_status_invalid`. None yield a task observation.
Stable malformed or unsafe ledgers report `ledger_refused`. Observed generation
changes report `lineage_changed`.

The JSON envelope contains `kind: "fleet-campaign-inspect"`, `read_only: true`,
the campaign ID and members with opaque repository/action IDs,
`source_fingerprint`, `action_binding`, `reason` and `local_task_status`.
`action_binding: "matched"` means the recorded local chain reached an exact
task with a recognized current status. It does not verify the requested
campaign specification. `specification_verification` and `launch_safety`
always remain `unknown`. The summary counts `inspected`, `matched` and
`unknown` members and always reports `done: false`, even for local `done`
tasks. No members are excluded from preview's resume candidates.

Exact import and task IDs remain internal to lineage matching and provenance
checks. The envelope omits `import_id` and `task_id` on every path because
production-generated IDs can contain title slugs.

Paths, summaries, task text, prompts, artifacts, credentials and raw exceptions
are excluded. Valid inspection returns exit 0 even when members are unknown.
Malformed inputs or repository selection return exit 2 with fixed JSON
refusal codes. JSON is the default without `--json`. Reads detect observed
generation changes but do not provide an atomic cross-ledger snapshot or
cross-machine freshness guarantees. Hub observations and campaign completion
or launch authority remain outside this slice.
