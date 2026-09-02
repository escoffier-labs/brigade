# Grok Bot operating guide

This guide covers the whole Grok Bot integration: what Brigade owns, which
connector packs exist, how queue actors are authorized, how lifecycle commands
behave, how a finding reaches the owner, and how to configure the Bot so
results arrive through the relay instead of a cloud workspace. For listener
installation and per-role setup commands, see
[Grok Bot MCP listener](grokbot-mcp.md). For the n8n read and action catalog,
see [n8n-operator](grokbot-n8n-operator.md).

## Overview

Grok Bot runs on its own cloud computer. Brigade runs on the operator's host and
owns the queue, the connector packs, and every path that reaches owner memory.
The Bot never holds queue authority. It calls role-scoped MCP listeners and
receives bounded, validated envelopes.

The integration follows four rules.

**One repository.** Every supported component installs from `brigade-cli`. There
is no second repository to clone and no separately packaged sidecar. The legacy
`grokbot-dispatch-mcp` sidecar is retired; see
[Retirement and rollback window](phase-grokbot-one-repository-distribution.md).

**Role-scoped processes.** Each pack runs as its own listener process with its
own bind, its own bearer, and a fixed tool inventory. There is no shared
connector credential and no single process that serves every pack.

**All Bot output is untrusted until owner review.** Every packaged bind is
loopback, and a remote connector needs an explicit public route, Cloudflare
Access in front of it, and a separate origin bearer. Reports and findings land
in the owner's review inbox as drafts with every producer-controlled line
quoted. Nothing the Bot produces edits canonical memory, `MEMORY.md`, or memory
cards.

## Connector packs

The registry is closed. Brigade does not load user-supplied pack manifests.
Pack manifests use schema `brigade.grokbot.connector-pack.v1`; instance config
uses `brigade.grokbot.connector-instance.v1` and lives at
`.brigade/grokbot/packs/<pack-id>.json`. Every packaged pack is version `1.0.0`.

| Pack id | Kind | Default bind | Public route | Tools |
|---|---|---|---|---|
| `operator` | queue-role | `127.0.0.1:8766` | none | `grokbot_queue_cancel`, `grokbot_queue_expire`, `grokbot_queue_list`, `grokbot_queue_report`, `grokbot_queue_status` |
| `repository-scout` | queue-role | `127.0.0.1:8767` | none | `grokbot_queue_ack_cancel`, `grokbot_queue_claim`, `grokbot_queue_complete`, `grokbot_queue_fail`, `grokbot_queue_list`, `grokbot_queue_renew`, `grokbot_queue_start`, `grokbot_queue_status` |
| `implementation-worker` | queue-role | `127.0.0.1:8768` | none | same eight tools as `repository-scout` |
| `cerebro-memory` | connector | `127.0.0.1:8770` | none | `cerebro_health`, `cerebro_proposal_status`, `cerebro_propose`, `cerebro_search`, `cerebro_show` |
| `fleet-steward` | connector | `127.0.0.1:8771` | none | `execute_remediation`, `fleet_overview`, `host_status`, `incident_bundle`, `propose_remediation`, `service_health` |
| `backup-steward` | connector | `127.0.0.1:8772` | none | `backup_execute_action`, `backup_operation_status`, `backup_overview`, `backup_propose_action`, `backup_restore_readiness`, `backup_target_status` |
| `obsidian-operator` | connector | `127.0.0.1:8773` | `/mcp` | `obsidian_action_status`, `obsidian_capabilities`, `obsidian_execute_action`, `obsidian_propose_action`, `obsidian_read`, `obsidian_search` |
| `wazuh-triage` | connector | `127.0.0.1:8774` | none | `wazuh_action_status`, `wazuh_alert_status`, `wazuh_classify`, `wazuh_incident_bundle`, `wazuh_ingest`, `wazuh_propose_remediation` |
| `n8n-operator` | connector | `127.0.0.1:8775` | none | `n8n_action_status`, `n8n_execute_action`, `n8n_execution_bundle`, `n8n_overview`, `n8n_propose_action`, `n8n_workflow_status` |
| `operations-relay` | connector | `127.0.0.1:8777` | none | `automation_finding_status`, `submit_automation_finding` |

`obsidian-operator` is the only pack with a public route. Port `8776` is
unallocated. Registry validation rejects duplicate ids, duplicate ports,
overlapping non-empty public routes, and any non-loopback packaged bind.

Each pack stores its own extra instance keys beyond the shared set (schema,
pack id, pack version, bind, allowed hosts, allowed origins, public route, and
the bearer reference):

- `fleet-steward`, `backup-steward`, `wazuh-triage`: runtime, ledger,
  action-state, and approval-directory references.
- `obsidian-operator`: those, plus staging directory, Excalidraw helper
  executable, upstream URL, and upstream key reference.
- `n8n-operator`: runtime, action-state, and approval-directory references.
- `cerebro-memory`: CLI executable and workdir references.
- `operations-relay`: the owner workspace path only.

## Queue coordination

### Roles and actor kinds

There are two worker roles, `implementation-worker` and `repository-scout`, and
five actor kinds: `feed`, `control`, `operator`, `implementation-worker`, and
`repository-scout`. A role is set on an enrolled actor policy only for the two
worker kinds.

Jobs move through the work states `queued`, `claimed`, and `running`, and the
terminal states `completed`, `failed`, `expired`, and `canceled`. The mutating
actions are `enqueue`, `claim`, `start`, `renew`, `complete`, `fail`, `cancel`,
`expire`, and `ack-cancel`. Of those, `start`, `renew`, `complete`, `fail`, and
`ack-cancel` operate against a live lease.

The hub authorizes each request against the caller's enrolled actor kind:

| Actor kind | Permitted hub actions |
|---|---|
| `feed` | `enqueue`, `list`, `whoami` |
| `control` | `enqueue`, `list`, `whoami` |
| `operator` | `list`, `status`, `whoami`, `cancel`, `expire`, `report-metadata` |
| `implementation-worker` | `list`, `status`, `whoami`, `claim`, `start`, `renew`, `complete`, `fail`, `ack-cancel` |
| `repository-scout` | same nine actions as `implementation-worker` |
| admin token only | `enroll-actor` |

The `list` entry for `feed` and `control` requires the #1343 fix deployed
hub-side. Until that hub is rolled forward, a feed or control actor holds only
`enqueue` and `whoami`, and any list call from those actors is refused. Deploy
the fixed hub before you rely on a feed lane that reads the queue. A worker's
role is fixed by its enrolled policy: a worker that asks for a different role
is refused, and so is a job outside its queue or role.

### Approval labels

Work enters the queue through GitHub labels, one label per role. Labelling an
issue for a scout does not approve a build, and a build never starts from an
issue that was only scout-approved:

| Approval label | Selector | What it produces |
|---|---|---|
| `grokbot-scout-approved` | `brigade run cloud grokbot scout-feed` | one read-only Repository Scout report |
| `grokbot-build-approved` | `brigade run cloud grokbot build-feed` | one implementation-worker draft pull request |

Both labels are named in their own private policy file, so an operator can
rename either one without touching the other. The intended flow is scout first,
then build: when the scout's report snapshot for that issue is still on the
queue target, `build-feed` names it in the worker job so the build starts from
the report instead of re-reading the repository from scratch. Setting
`require_scout_report` to `true` in the build policy makes that ordering
mandatory.

### Leases

A lease runs from 30 seconds to 3600 seconds and defaults to 300 seconds. The
holder renews before expiry and within the job deadline. `expire` never
requeues a job; it finalizes one whose deadline or lease has passed.

### Job deadlines

A job's deadline is `queued_at` plus its `timeout_seconds`. The deadline is the
hub's, not an operator's: nobody has to call `expire` for a job to end.

- Every `list`, `status`, `claim`, `renew`, and other mutating request sweeps
  past-deadline jobs to `expired` before it answers, so a read never reports a
  job that is already over as `queued` or `running`.
- The hub also sweeps on a timer (`start_expiry_sweeper`, every 60 seconds by
  default) for the life of the process, so a queue that nobody polls still
  expires. A job enqueued with `timeout_seconds` 7200 that no worker ever claims
  is `expired` about 7200 seconds later with no request involved.
- Each automatic expiry writes an `expire` row to `grokbot_operations` with an
  `expire:deadline:<job_id>:<revision>` operation id and a NULL `actor_node_id`:
  the deadline asked for it, not an actor. An operator `expire` still writes its
  own operation id, and the two ids can never collide.
- Claiming a job that is already past its deadline is refused with `job-expired`
  rather than a state or revision error, so a Bot can tell "too late" apart from
  "you raced another worker".

The local (no-hub) queue in `grokbot_jobs.py` follows the same rule: `status`
and `get_job` terminalize an elapsed job before projecting it, and a claim past
the deadline expires the job and fails with `job-expired`.

`grokbot_queue_claim` takes `lease_id` as an optional argument. A Bot that has
no lease to supply omits it or sends `null`, and the listener mints a uuid4 hex
lease and returns it in the claim result as `lease_id`. That returned value is
what the worker carries on start, renew, complete, fail, and ack-cancel for that
job. A supplied `lease_id` is echoed back unchanged, and a malformed one is
still refused before any queue mutation.

### Queue tool arguments

`grokbot_queue_list` accepts four optional arguments: `state` (one of the seven
job states), `include_all` (boolean, default `true`; `false` hides terminal
jobs), `limit` (integer 1 to 100, default 100), and `role`. The advertised
`inputSchema` carries exactly those four. An optional argument sent as `null`
means the same as omitting it.

`state` and `include_all` intersect. A terminal `state` with `include_all` set
to `false` can only answer with an empty list, so that pair is refused with
`state <state> requires include_all` rather than returning the silence a Bot
reads as a broken queue.

The role is pinned server-side. `role` is accepted only when it equals this
listener's own role, it never changes the projection, and no argument lets a
worker listener see another role's jobs. A refused list call answers with a
bounded reason naming the accepted keys, states, or role rather than the
generic validation message, and the reason never repeats the value that was
sent.

Each tool call writes one INFO journal line: tool name, the accepted argument
keys that were present, a count of unrecognized keys, the decision (`ok` or
`refused`), and the bounded reason. Values, job payloads, lease values, and
bearer material never appear. Requests refused at the HTTP edge are journaled
the same way.

### Enrollment and credentials

Enrollment is admin-only:

```bash
brigade fleet grokbot enroll-actor
```

`enroll-actor` requires the hub admin token and is refused when a node token
makes the call. It upserts one row per node into the hub's actor policy, keyed
by node id, recording the queue owner node, the queue id, the actor kind, the
optional worker role, and the enabled flag. Every other action requires an
already-enrolled node and runs with the node's own token.

Listeners read their Fleet Hub node token from a file named by a narrowly
scoped environment variable, never from argv and never from an MCP argument:

- `BRIGADE_GROKBOT_<INSTANCE>_HUB_TOKEN_FILE` for one named listener, where
  `<INSTANCE>` is the upper-cased instance with hyphens replaced by
  underscores.
- `BRIGADE_GROKBOT_HUB_TOKEN_FILE` as the generic direct-queue listener token.
- `BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE` for the feed actor, with no fallback.

Each path must be absolute and each file must be mode `0600` and read through a
no-follow descriptor. A listener fails closed when the bound token's actor kind
does not equal the listener's own instance.

### Hub authority is irreversible

Once the local hub-authority marker is written, Brigade treats Fleet Hub as the
sole queue authority. A hub outage or a missing hub URL does not fall back to
local lifecycle. Roll the hub forward to the version you intend to run before
you enroll feed actors or write the authority marker, because you cannot step
back to local authority afterwards.

## Lifecycle

Every pack verb defaults to preview or read-only. Nothing writes without
`--apply`, and service installation writes nothing without `--out`.

| Verb | Default | What `--apply` writes |
|---|---|---|
| `pack list` / `pack show` | read-only | nothing |
| `pack setup` | preview | `.brigade/grokbot/packs/<id>.json` at mode `0600`; queue-role packs also write the legacy role store, with verified rollback of both |
| `pack doctor` | read-only | nothing |
| `pack canary` | read-only, non-mutating | nothing |
| `pack install-service` | renders to stdout unless `--out` | one unit file in the named directory |
| `pack update` | preview | the rewritten instance config |
| `pack remove` | preview | deletes only owned config and unit paths |
| `pack relay-setup` | preview | `.brigade/grokbot/relay.json` |
| `pack relay-doctor` | read-only | nothing |
| `pack relay` | preview | one bounded batch under `--apply --limit N` |
| `pack install-relay-service` | renders to stdout unless `--out` | `brigade-grokbot-findings-relay.service` and `.timer` |

No lifecycle verb starts, stops, reloads, or enables a service, and none stores
a bearer value. Install the rendered unit through the host's normal
service-management process after you review it. `setup` stores only a bearer
reference, either `--bearer-file` or `--bearer-env`. A `--bind` whose port
matches another pack's packaged default or installed port is refused as
`duplicate-port` without printing config contents.

Doctor emits sanitized named checks. Queue roles emit `dependency`, `config`,
`permissions`, `queue`, and `endpoint`. Under hub authority with a configured
feed token, and with the #1343 fix in place, a queue role also emits
`feed-authority` with a status of `ok`, `fail`, or `skipped`; a `skipped`
`feed-authority` check does not fail the command. The `operations-relay` pack
emits `dependency`, `config`, `permissions`, and `endpoint`; it has no `queue`
check.
The legacy `--instance` commands (`setup`, `doctor`, `canary`,
`install-service`) remain supported and interoperate with pack config.

## Finding delivery

The `operations-relay` pack is the supported ingress for a generic Bot result.
It exposes exactly two tools and serves `/health` as
`{"ok": true, "service": "grokbot-operations-relay"}`. Requests are
`Bearer`-authenticated with a constant-time comparison and bounded at 16 KiB.

```
producer
  -> submit_automation_finding      loopback :8777, bearer, 16 KiB request bound
  -> input validation               exact 10 keys, forbidden-key screen
  -> handle = sha256(producer NUL finding_id NUL revision)
  -> outbox record                  .brigade/cloud/grokbot/outbox/<relay-id>.json, 0600, pending
  -> owner draft                    <owner>/memory/handoff-inbox/finding-<handle>.md
  -> delivery marker                .brigade/cloud/grokbot/findings/<handle>.json, 0700 dir / 0600 file
  -> outbox pending -> ready        only after the draft is delivered
  -> Fleet Hub event                report_event; ready -> reported only when it returns True
  -> owner review and normal memory ingest
```

The draft filename is deterministic, and `memory/handoff-inbox` is the owner's
review inbox, not an auto-ingest path.

### `submit_automation_finding`

The input must be an object whose key set equals these ten keys exactly. Extra
keys and missing keys are both rejected.

| Key | Rule |
|---|---|
| `producer` | matches `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |
| `finding_id` | same identifier pattern |
| `revision` | same identifier pattern |
| `observed_at` | empty string for a legacy unknown time, or a timezone-aware ISO timestamp of at most 64 characters |
| `severity` | one of `info`, `low`, `medium`, `warning`, `high`, `critical`, `unknown` |
| `title` | non-blank, at most 200 characters, no NUL |
| `body` | non-blank, no NUL, at most 12,000 UTF-8 bytes |
| `source_ref` | opaque, at most 512 characters; consecutive dots are allowed |
| `source_digest` | `sha256:` plus lowercase hex; it is not required to hash the body |
| `content_digest` | must equal `sha256:` of canonical UTF-8 `title` + NUL + `body` |

The whole request is bounded at 16,384 bytes, so the 12,000-byte body bound
leaves room for the JSON-RPC envelope.

These keys are rejected on intersection, before the exact-set check: `argv`,
`bearer`, `command`, `credential`, `cwd`, `dest`, `destination`, `executable`,
`file`, `http`, `https`, `memory`, `owner`, `password`, `path`, `secret`,
`target`, `token`, `url`.

Idempotency is by `producer` plus `finding_id` plus `revision`. That triple
hashes to the irreversible `handle`, which is the only identifier that appears
in public output. Resubmitting the same revision does not create a second
draft. A later revision produces a replacement draft with a non-colliding name.

The tool returns `handle`, `created`, `known`, `reported`, `pending`, and
`state`.

Only two public error codes exist: `invalid_request` ("Tool input failed
validation") and `unavailable` ("Operations relay is unavailable").

### `automation_finding_status`

The input must be exactly `producer`, `finding_id`, and `revision`, with the
same forbidden-key screen and the same identifier rules. It returns `handle`
and `state`.

`state` is the outbox status when a record exists (`pending`, `ready`, or
`reported`), otherwise `delivered` when the marker file for that handle exists,
otherwise `unknown`. A raised internal findings error also reports `unknown`.

### Owner workspace

`--owner` is required for this pack. The path must be absolute, contain no NUL,
not be a symlink, be a real directory, be mode exactly `0700`, and be owned by
the current uid.

### Redaction boundary

Delivery markers store only schema, the irreversible identity digest, revision,
source digest, content digest, the draft relative path, the draft SHA-256, and
`delivered_at`.

The Fleet event carries `run_id` as the irreversible relay digest, the fixed
labels `findings-relay` and `grokbot`, an allowlisted `finding.<severity>`
state, and stable `ts`, `sequence`, and `digest` values.

Finding `title`, `body`, `source_ref`, raw `producer`, raw `finding_id`, and
any filesystem path, address, command, or credential never reach delivery
markers, the Fleet outbox, Brigade receipts, Fleet Hub events, or CLI output.
Public surfaces carry counts plus the irreversible handle or relay id. On the
queue side the hub independently rejects `instructions`,
`verification_commands`, `ownership_paths`, `base_ref`, `report_text`,
`report`, `spec`, and every credential-shaped key, and hub job projections omit
the idempotency key hash and the lease token digest.

## Operating the Bot

The goal is that a scheduled routine and an interactive session both end with a
finding in the owner's review inbox, with no manual pickup from the Bot's cloud
workspace. Nine configuration rules get you there.

**1. Put the submit step in a Skill, not in the Bot description.** A Skill is
the documented container that carries result validation, a return format, and
approval requirements. A line in the Bot description carries none of those.

**2. Mention the connector in each routine or automation instruction.**
`@`-mentioning the private connector inside the instruction is the documented
mechanism for making the Bot use it on every run. A connector that is merely
enabled on the Bot is not the same thing.

**3. Make the MCP tool description do the enforcement.** Tool descriptions drive
selection, and `tool_choice: required` is not available outside the API. Word
the description so it states that this is the only accepted delivery channel and
that work is not complete until the call returns a handle.

**4. Require the Bot to echo the returned handle.** The conversation is the
surface that holds the final result, so an echoed handle turns "did it submit"
into a one-glance check and gives you the argument for
`automation_finding_status`.

**5. Allow one idempotent retry, then an explicit failure report.** On a tool
failure the agent will otherwise change its approach and write to the workspace
instead. Because submission is idempotent on producer plus finding id plus
revision, a repeat of the identical call is safe. State that the fallback is a
plain failure message, never a workspace file.

**6. Serve over Streamable HTTP.** SSE is supported but breaks behind quick
tunnels. Streamable HTTP is the transport to depend on.

**7. Use a stable public hostname.** Changing the URL forces you to remove and
re-add the connector, which silently breaks every routine that referenced it.

**8. Keep Cloudflare Access in front, with a separate origin bearer.** Publish
only the relay endpoint. Do not publish the fleet hub or any local queue path,
and do not replace Access with an unauthenticated tunnel policy. Whether the
consumer connector dialog accepts a bearer field is not documented; verify it
in-product before you depend on it. The documented header path
(`authorization` / `headers`) exists only in the API `remote_mcp` tool.

**9. Word the tool so it does not read as an approval-gated category.** An
unattended routine that trips an approval on "publishing content" or "production
changes" has no documented resolution path. Describe the call as submitting a
finding for owner review, because that is what it does.

Treat the relay as the system of record. Grok keeps a bounded number of run
records per routine (20 at the time of writing), and a workspace reset can
discard unsaved work, so anything that exists only Bot-side can disappear.

### Example Skill text

```text
Name: Submit operations finding

When: Any scheduled routine or interactive task that produces a result the
owner needs to see.

Steps:
1. Compute content_digest as the SHA-256 of the UTF-8 bytes of the title, one
   NUL byte, then the body. Prefix it with "sha256:".
2. Call submit_automation_finding on the operations relay connector with
   exactly these keys: producer, finding_id, revision, observed_at, severity,
   title, body, source_ref, source_digest, content_digest.
3. Read the "handle" value from the response.

Result validation: the response contains a "handle". If it does not, the
submission did not happen.

Return format: report "submitted <handle>" in the conversation, then the
one-line summary. Never report a result without a handle.

Approval requirements: none. This submits a finding for owner review and
changes nothing in production.

On failure: retry the identical call once. The same producer, finding_id, and
revision is idempotent, so a retry cannot create a duplicate. If the second
call also fails, report "submission failed" plus the error in the
conversation. Do not write the result to a workspace file and do not continue
with an alternative delivery approach.
```

### Example routine instruction

```text
Run the daily check. When you have a result, use @operations-relay and follow
the "Submit operations finding" skill: submit the finding, then echo the
returned handle in this conversation as "submitted <handle>". The submission
is the deliverable. A run without a handle is a failed run.
```

## Troubleshooting

**`scout-feed --apply` fails with an opaque queue error.** Under hub authority,
`brigade run cloud grokbot scout-feed --apply` reads the existing scout jobs
through the hub `list` action before it enqueues, so a credential refusal on
`list` surfaces as a generic queue error rather than an authorization problem.
This is #1343. After the fix the refusal prints in the form
`auth-failed action=list actor=feed`, naming the refused action and the actor
kind. Until it is deployed, check the enrolled actor kind and the token file
named by `BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE` before you look at the queue
itself. The sibling `brigade run cloud grokbot feed --apply` command enqueues
only, never calls `list`, and still reports its failures in the opaque
`queue-error index=N` form, which the #1343 fix does not change.

**Doctor is green while the feed lane is dead.** The queue-role doctor checks
dependency, config, permissions, queue, and endpoint. None of those exercises
the feed actor's hub authority, so a feed lane refused at the hub reports no
failing check and the queue simply stays empty. The #1343 fix adds a
feed-authority doctor check that covers this. Until it is deployed, confirm the
lane by watching for newly enqueued jobs, not by reading a green doctor.

**A Grok run that finished is reported as a failure.** The structured-output
check compares the stop reason against the exact string `EndTurn`, so a CLI
that emits any other casing fails that comparison and the run is reported as an
output-validation failure with the answer discarded. This is #1345, fixed in
#1349, which accepts both `EndTurn` and `end_turn`. Until that fix is deployed,
check the stop reason the CLI actually emitted before you treat a discarded
answer as a model failure.

**A Bot reports that every tool call fails input validation.** Before #1363,
`grokbot_queue_list` advertised no arguments and refused any filter with a
generic `-32602 Invalid request`, and `grokbot_queue_claim` required a
`lease_id` the Bot had no way to obtain, so a first claim failed too. A model
that filters a queue list on its first attempt reads that as a dead adapter and
pauses its routine. After the fix the four list filters are accepted, the claim
mints a lease when one is not supplied, and every refusal names what it would
accept. Check `journalctl` for the per-call line
(`grokbot tool=... decision=refused reason=...`) before reproducing the call by
hand. An empty journal for a reported failure means the request never reached
the listener.

**Doctor returns nonzero right after setup.** The endpoint check cannot connect
until the listener process is running. Start the listener, then run doctor
again.

**Canary fails.** A canary needs a listener that is already running. The
`operations-relay` canary fails with `config` when runtime config cannot load,
`health` when `/health` does not return the expected service payload, `auth`
when an anonymous request is not rejected with 401 or 403,
`inventory-unreachable` when the tool list cannot be read, and
`inventory-mismatch` when the tool list is not exactly
`automation_finding_status` and `submit_automation_finding`.
