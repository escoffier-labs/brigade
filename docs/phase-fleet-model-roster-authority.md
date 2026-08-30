# Fleet Model Roster Authority

Status: proposed contract for operator review.

Date: 2026-08-30.

Consumers: `brigade run`, `t3-fleet`, Fleet Hub operators.

## Incident and required outcome

`t3-fleet submit` omitted an explicit model selection and inherited the T3
project default `openai/gpt-5.4`. The `gpt-5.4` and `gpt-5.5` families are
permanently retired.
T3 project defaults are observations for drift reporting. They never authorize a
dispatch.

Fleet Hub owns one versioned roster. Every new Brigade or T3 dispatch resolves a
logical seat through that roster before creating a thread, worktree, run, or
provider task. The resolution records the exact provider, model, reasoning,
consumer binding, and roster revision. A configured fleet fails closed when it
cannot obtain a valid current roster or a valid last-known-good copy.

## Decisions

1. **Fleet Hub is the only mutable roster authority.** Basis:
   `stated-constraint`. Brigade host rosters and T3 project defaults are checked
   against it. They do not override it.
2. **T3 consumes a Brigade CLI admission contract.** Basis:
   `evidence+judgment`. Brigade already owns Hub authentication and model-policy
   transport. Keeping HTTP and cache validation inside Brigade prevents a second
   implementation in `t3-fleet`.
3. **A model seat contains exact provider, model, and reasoning values plus
   consumer bindings.** Basis: `stated-constraint`. A provider-only result such
   as `OpenAI` cannot pass admission or the canary.
4. **The last-known-good roster is authenticated by the Hub with the enrolled
   node token.** Basis: `evidence+judgment`. The Hub receives the raw bearer while
   authenticating the request even though it persists only a SHA-256 digest. It
   can MAC the node-audience response without a new dependency or another shared
   secret. Admin-token roster reads are inspectable but not cacheable.
5. **The LKG maximum age is 900 seconds.** Basis: `judgment`. This permits a
   short Hub restart without allowing an old subscription or retired-model rule
   to survive for hours. The first implementation keeps this fixed.
6. **The `gpt-5.4` and `gpt-5.5` families have defense-in-depth denial.** Basis:
   `stated-constraint`. They are permanent Hub retirements and immutable local
   emergency denials in Brigade. `t3-fleet` may retain the same narrow emergency
   check, but it must not duplicate general roster resolution.
7. **Hub admission and audit are one online transaction.** Basis: `judgment`.
   An accepted online resolution writes its audit row before returning success.
   LKG admission writes the same safe record to an owner-only local spool for
   later flush.

## Alternatives considered

### Fetch and resolve the roster inside every consumer

This gives each consumer direct access to the document. It also duplicates HTTP
authentication, cache validation, retired-family matching, revision rollback
checks, and error classification. A drift fix would need coordinated releases in
Brigade and `t3-fleet`.

### Keep the guard inside `t3-fleet`

A local `gpt-5.4` and `gpt-5.5` regex stops this incident but leaves the T3 project
default in control of every other model change. It provides no fleet revision,
subscription policy, Hub audit, or Brigade parity.

### Add a separate Hub signing key and asymmetric signatures

An asymmetric signature would let any consumer verify a document using a public
key. Brigade has zero runtime dependencies, and Python 3.10 does not provide an
Ed25519 signing API in the standard library. This can be added later if a
reviewed dependency or an operating-system signing service becomes acceptable.
The Hub-generated node-token MAC authenticates the first implementation's cache
and binds it to the enrolled machine.

## Hub storage schema

The migration uses the next available Fleet Hub schema version. It must not
claim a fixed number until other pending Hub migrations have landed.

### `model_roster_meta`

One row:

| Column | Type | Rule |
| --- | --- | --- |
| `singleton` | INTEGER | Primary key, always `1` |
| `revision` | INTEGER | Starts at `1`, increases in the same transaction as every roster mutation |
| `updated_at` | TEXT | UTC timestamp |
| `updated_by` | TEXT | Sanitized operator identity, never a token |

### `model_policy`

The existing seat-keyed table gains these required fields:

| Column | Type | Rule |
| --- | --- | --- |
| `seat` | TEXT | Existing primary key |
| `provider` | TEXT | Canonical provider slug |
| `model` | TEXT | Exact provider model ID |
| `reasoning` | TEXT | Exact reasoning value, including `none` when the adapter has no reasoning control |
| `enabled` | INTEGER | Existing boolean admission switch |
| `limit_count` | INTEGER | Existing concurrent seat limit |
| `brigade_cli` | TEXT | Exact Brigade harness binding or empty when unsupported |
| `t3_instance_id` | TEXT | Exact T3 model instance ID or empty when unsupported |
| `t3_service_tier` | TEXT | Exact optional T3 service tier or empty |
| `notes` | TEXT | Existing bounded operator note |
| `updated_at` | TEXT | UTC timestamp |

An enabled seat must have non-empty provider, model, and reasoning values. A
consumer can use a seat only when that consumer's binding is non-empty.

### `model_consumer_defaults`

| Column | Type | Rule |
| --- | --- | --- |
| `consumer` | TEXT | Primary key, initially `brigade-run` and `t3-fleet` |
| `seat` | TEXT | Foreign key to `model_policy.seat` |
| `updated_at` | TEXT | UTC timestamp |

An omitted `t3-fleet --seat` resolves this Hub value. An explicit seat is a
per-dispatch override that still requires an enabled compatible roster row.

### `retired_models`

| Column | Type | Rule |
| --- | --- | --- |
| `provider` | TEXT | Canonical provider slug |
| `family` | TEXT | Canonical family root, such as `gpt-5.4` |
| `match_kind` | TEXT | `family-prefix` in schema v1 |
| `permanent` | INTEGER | Permanent rows cannot be removed or disabled |
| `reason_code` | TEXT | Bounded safe reason |
| `created_at` | TEXT | UTC timestamp |

Primary key: `(provider, family)`. The initial migration inserts permanent
`openai/gpt-5.4` and `openai/gpt-5.5` rows.

`family-prefix` matches the exact family or a suffix separated by `-`, `/`, or
`:` after provider alias normalization. It does not match `gpt-5.40`.

### `model_admission_audit`

| Column | Type | Rule |
| --- | --- | --- |
| `node_id` | TEXT | Derived from the caller token |
| `request_id` | TEXT | Caller-generated idempotency key |
| `phase` | TEXT | `controller`, `target`, or `brigade-run` |
| `consumer` | TEXT | `t3-fleet` or `brigade-run` |
| `source` | TEXT | `hub` or `lkg` |
| `roster_revision` | INTEGER | Exact accepted revision |
| `roster_digest` | TEXT | SHA-256 of the canonical roster body |
| `seat` | TEXT | Resolved seat, nullable when resolution failed before a seat existed |
| `provider` | TEXT | Resolved provider, nullable before model resolution |
| `model` | TEXT | Resolved exact model ID, nullable before model resolution |
| `reasoning` | TEXT | Resolved exact reasoning value, nullable before model resolution |
| `consumer_binding` | TEXT | Sanitized instance or CLI ID, nullable before binding resolution |
| `decision` | TEXT | `admitted` or a bounded denial code |
| `request_digest` | TEXT | Digest of the canonical admission request for replay fencing |
| `expires_at` | TEXT | Freshness bound returned with this decision |
| `created_at` | TEXT | UTC timestamp |

Primary key: `(node_id, request_id, phase)`. Exact replay returns the prior safe
result. A reused key with a different request digest returns conflict. The table
does not store prompts, task text, worktree paths, tokens, or provider output.

## Roster response contract

`GET /models` remains authenticated by the existing admin or node bearer. The
response becomes additive, so old Brigade clients can continue reading
`models`. New consumers validate the complete envelope:

```json
{
  "schema": "brigade.fleet_model_roster.v1",
  "revision": 42,
  "revision_updated_at": "2026-08-30T13:52:00Z",
  "issued_at": "2026-08-30T14:00:00Z",
  "expires_at": "2026-08-30T14:15:00Z",
  "audience_node_id": "node-a",
  "document_sha256": "sha256:0123456789abcdef",
  "mac": {
    "algorithm": "hmac-sha256-node-bearer-v1",
    "value": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "seats": [
    {
      "seat": "cursor_grok",
      "enabled": true,
      "provider": "cursor",
      "model": "cursor-grok-4.6-high-fast",
      "reasoning": "high",
      "limit": 8,
      "bindings": {
        "brigade": {"cli": "cursor-agent"},
        "t3_fleet": {"instance_id": "cursor", "service_tier": null}
      }
    }
  ],
  "consumer_defaults": {
    "brigade-run": "chef",
    "t3-fleet": "cursor_grok"
  },
  "retired_models": [
    {
      "provider": "openai",
      "family": "gpt-5.4",
      "match_kind": "family-prefix",
      "permanent": true,
      "reason_code": "permanently-retired"
    },
    {
      "provider": "openai",
      "family": "gpt-5.5",
      "match_kind": "family-prefix",
      "permanent": true,
      "reason_code": "permanently-retired"
    }
  ],
  "models": []
}
```

The revision-stable document SHA-256 covers canonical compact JSON containing
`schema`, `revision`, `revision_updated_at`, `seats`, `consumer_defaults`, and
`retired_models`. It excludes per-response freshness and audience fields,
`document_sha256`, `mac`, and the legacy `models` projection. The digest therefore
stays identical across reads and nodes until a roster mutation increments the
revision.

`issued_at` is the Hub response time and `expires_at` is exactly 900 seconds
later. They are not part of `document_sha256`. The node-response MAC covers the
complete cacheable envelope containing the stable roster body,
`audience_node_id`, `issued_at`, `expires_at`, and `document_sha256`. Object keys
are sorted and ASCII-safe JSON uses no insignificant whitespace.

For a node-authenticated response, the Hub computes:

```text
HMAC-SHA256(
  key = raw node bearer presented on this request,
  message = "brigade.fleet-model-roster.lkg.v1\0" + canonical_cache_envelope_json
)
```

The Hub returns the 64-character lowercase hexadecimal value and never persists
the raw token or HMAC key. The client verifies it with `hmac.compare_digest`.
Admin-token reads omit `audience_node_id` and `mac` and cannot seed an LKG.

Every accepted mutation increments `revision` in the same `BEGIN IMMEDIATE`
transaction. Admin mutations require `expected_revision`. A stale revision
returns HTTP 409 with `roster_revision_conflict`. Permanent retirements cannot be
deleted, disabled, or narrowed.

## Admission API

Online resolution uses `POST /models` with a node token:

```json
{
  "action": "admit",
  "schema": "brigade.model_admission_request.v1",
  "consumer": "t3-fleet",
  "seat": null,
  "request_id": "c833a6f6-02fd-4eb2-92cb-d44d3cd29b66",
  "phase": "controller",
  "expect_revision": null,
  "expect_digest": null
}
```

The Hub derives `node_id` from the bearer. It selects the explicit seat or the
consumer default, rejects disabled or incompatible rows, applies the permanent
retirement check, records the audit row, then returns:

```json
{
  "schema": "brigade.model_admission.v1",
  "state": "authoritative",
  "source": "hub",
  "roster_revision": 42,
  "roster_digest": "sha256:0123456789abcdef",
  "seat": "cursor_grok",
  "provider": "cursor",
  "model": "cursor-grok-4.6-high-fast",
  "reasoning": "high",
  "binding": {
    "instance_id": "cursor",
    "service_tier": null
  },
  "expires_at": "2026-08-30T14:15:00Z"
}
```

The target repeats admission with the same `request_id`, `phase=target`,
`expect_revision`, and `expect_digest`. Any revision or digest change between
controller and target returns conflict before target receipt, thread, or
worktree creation.

## Brigade CLI contract

`t3-fleet` consumes the CLI, not Hub HTTP:

```bash
brigade fleet models admit \
  --consumer t3-fleet \
  --request-id c833a6f6-02fd-4eb2-92cb-d44d3cd29b66 \
  --phase controller \
  --json
```

Optional arguments:

- `--seat SEAT` selects an enabled compatible seat instead of the Hub consumer
  default.
- `--expect-revision N` and `--expect-digest SHA256` bind target-side replay.
- `--no-lkg` disables cache fallback for a canary or security-sensitive call.

Exit codes:

| Code | Meaning |
| ---: | --- |
| 0 | Exact admission succeeded |
| 1 | Hub, authentication, cache, or cache-integrity failure |
| 2 | Invalid CLI or unsupported response schema |
| 3 | Policy denial, retired model, disabled seat, or consumer-binding drift |
| 4 | Revision, digest, or idempotency conflict |

The JSON schema is stable across Hub and LKG success. Errors use a bounded code
such as `auth-failed`, `hub-unavailable`, `lkg-expired`, `lkg-mac-invalid`,
`revision-rollback`, `retired-model`, `seat-disabled`, or `binding-missing`.
Diagnostics never include tokens, raw response bodies, project paths, or task
text.

## LKG cache contract

The Brigade client writes the cache only after a valid authenticated Hub
response. The cache lives below the existing owner-only Brigade fleet state
directory. The directory is mode 0700 and the regular file is mode 0600. Reads
and atomic replacements follow Brigade's descriptor-safe no-symlink patterns.
The maximum encoded size is 1 MiB.

The cache record contains the complete node-audience roster envelope, its Hub
MAC, `cached_at`, and the highest accepted revision. The client never replaces
the Hub MAC with a self-issued value.

LKG is permitted only after a transport error, timeout, or HTTP 5xx. It must be
no older than 900 seconds and must pass file identity, owner, mode, size, Hub MAC,
schema, audience, digest, expiry, permanent-retirement, and non-decreasing
revision checks.

LKG is forbidden after HTTP 401 or 403, HTTP 409, a malformed authoritative
response, an audience or digest mismatch, Hub MAC failure, node-token rotation, a
future timestamp beyond bounded clock skew, or revision rollback. Existing
in-flight work is unaffected. New work fails closed.

## `t3-fleet` adapter contract

Controller order:

1. Parse CLI arguments without creating state.
2. Generate `request_id`.
3. Call `brigade fleet models admit --phase controller --json`.
4. Apply the returned `binding.instance_id`, `model`,
   `options.reasoningEffort`, and optional `service_tier` to the T3 request.
5. Reject any mismatch or absent exact field.
6. Write the controller journal with the admission fields.
7. Contact the target.

Target order:

1. Validate the envelope and exact field set.
2. Repeat Brigade admission with expected revision and digest.
3. Compare every returned value with the controller envelope.
4. Only then write the target receipt, create a T3 thread, or create a worktree.

The existing `project.defaultModelSelection` can be collected for doctor output.
It is never copied into a dispatch request. A caller-supplied raw
`--model-instance` or `--model` is rejected once Hub authority is configured.
Per-dispatch selection uses `--seat`.

Both controller and target receipts add:

```json
{
  "model_admission": {
    "schema": "brigade.model_admission.v1",
    "source": "hub",
    "roster_revision": 42,
    "roster_digest": "sha256:0123456789abcdef",
    "seat": "cursor_grok",
    "provider": "cursor",
    "model": "cursor-grok-4.6-high-fast",
    "reasoning": "high",
    "provider_instance_id": "cursor"
  }
}
```

The target does not implement Hub HTTP, cache logic, family matching, or roster
parsing. Those remain Brigade responsibilities.

## Brigade run integration

`brigade run` replaces the current unversioned `/models` snapshot with the same
admission resolver. It supplies the requested worker or orchestrator seat and
persists the admission object in `model-policy.json`, `run.json`, and safe fleet
audit. Existing model leases continue to enforce concurrent seat limits after
admission. Admission selects what may run. A lease controls how many may run.

An unconfigured standalone Brigade target keeps its current local behavior. A
configured fleet never falls back to an unversioned local roster.

## Doctor and reconciliation

Brigade adds:

```bash
brigade fleet models doctor --consumer t3-fleet --json
brigade fleet models reconcile --consumer t3-fleet --json
```

The Brigade result reports Hub reachability, roster revision and digest, cache
age and validity, selected consumer default, exact provider/model/reasoning,
binding presence, retired-model status, local Brigade roster drift, and last
admission spool state.

`t3-fleet doctor --json` calls the Brigade doctor and adds T3 observations:

- project default instance/model
- resolved Hub instance/model/reasoning
- model instance present or missing on controller and target
- `project-default-drift`, `instance-missing`, `reasoning-drift`,
  `roster-revision-drift`, and `retired-model-configured` findings

Doctor is read-only. Reconcile reports exact commands or config fields that need
change. It does not edit T3 project defaults or Hub policy.

## First canary acceptance

The first T3 canary runs with `--no-lkg` and a harmless read-only prompt. Its
machine-readable output must contain:

```json
{
  "admission": "pass",
  "roster_revision": 42,
  "provider": "cursor",
  "model": "cursor-grok-4.6-high-fast",
  "reasoning": "high",
  "provider_instance_id": "cursor",
  "observed_model": "cursor-grok-4.6-high-fast",
  "exact_match": true
}
```

`observed_model` must come from the accepted T3 request or a structured T3
response that preserves the exact model ID. A provider family label such as
`OpenAI` cannot satisfy the canary. If T3 exposes no exact accepted model, the
canary fails until that evidence path exists.

## Acceptance tests

- Any `gpt-5.4` or `gpt-5.5` spelling, provider prefix, or separated family suffix
  is denied before thread, worktree, run, receipt, or provider mutation.
- `gpt-5.40` is not caught by the family matcher.
- Every seat mutation, consumer-default mutation, and retirement increments one
  revision atomically.
- A stale `expected_revision` makes no mutation and returns conflict.
- Online admission records one idempotent audit row before success.
- A controller-to-target revision or digest change stops before target mutation.
- A T3 project default mismatch is visible in doctor and never changes the
  admitted model.
- A valid LKG works only after transport or 5xx failure and only for 900 seconds.
- Auth rejection, malformed Hub data, token rotation, cache tampering, future
  timestamp, expiry, and revision rollback all fail closed without mutation.
- Cache and audit spool paths reject symlinks, special files, unsafe modes,
  oversized data, and replacement races.
- Brigade and `t3-fleet` produce byte-compatible admission objects for the same
  seat and revision.
- The canary records exact provider, model, reasoning, and instance ID.
- No response, audit row, receipt, cache error, or Fleet Hub event contains a
  bearer token, prompt, task text, model output, provider response body, or
  private path.

## Rollout boundary

1. Land and deploy the Hub schema, versioned response, admission resolver,
   cache, audit, doctor, and Brigade run integration.
2. Seed the permanent retired rows and current fleet seats, consumer defaults,
   reasoning values, and consumer bindings.
3. Run Brigade doctor on Rocinante, Shadowfax, and Gandalf.
4. Rebase the pending `t3-fleet` guard branch onto the final CLI contract.
5. Run target-side no-mutation denial tests, then the exact-model canary.
6. Keep the T3 project default visible as drift. Do not use it as rollback.

## Non-goals

- Fleet Hub does not store provider credentials or subscription tokens.
- T3 does not gain direct Fleet Hub credentials beyond the enrolled host's
  existing Brigade configuration.
- This slice does not change cloud-worker capacity leases, Grok Bot queue
  authority, or model subscription accounting.
- This slice does not cut a Brigade release.

## Open questions

None block the first implementation. An asymmetric Hub signature and adjustable
LKG age remain later choices if the zero-dependency or 900-second decisions
change.
