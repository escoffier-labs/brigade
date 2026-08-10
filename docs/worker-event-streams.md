# Worker event streams: field classification and scrubbed projections

Status: implemented for issue #592 (first slice).

## Boundary

Brigade's Codex app-server transport records worker notifications under
`events/<worker>.jsonl`. Those files can contain provider notifications, model
text, tool metadata, absolute paths, and transport diagnostics.

This contract:

- classifies each recorded field as `public_metadata`, `private_content`,
  `secret`, or `prohibited`
- keeps raw streams local and access-controlled by default
- produces a bounded scrubbed projection for inspection, replay evidence,
  export, or attachment
- fails closed when an unknown event type or field cannot be classified

Raw streams and scrubbed projections use distinct media types and artifact
classes:

| Kind | Media type | Artifact class |
| --- | --- | --- |
| Raw | `application/vnd.brigade.worker-events.appserver+jsonl` | `worker-event-stream-raw` |
| Scrubbed | `application/vnd.brigade.worker-events.appserver.scrubbed+jsonl` | `worker-event-stream-scrubbed` |
| Legacy / not yet scrubbed | raw media type | `worker-event-stream-unclassified` |

Legacy on-disk NDJSON remains locally inspectable and is marked
`unclassified` until a scrubbed projection exists.

## Consumer policy

| Policy | Consumers | Behavior |
| --- | --- | --- |
| `scrubbed-only` (default) | audit, replay, export | reject raw/unclassified streams |
| `local-only` | resume, local watch/salvage | may read raw streams in place |

Library entry points live in `brigade.worker_events` (`scrub_event`,
`scrub_stream_file`, `inspect_stream_file`, `load_stream_for_consumer`,
`require_consumer_policy`). `brigade.run_audit.reject_raw_worker_stream_evidence`
is the audit/replay gate.

## Field-classification matrix

Source of truth: `worker_events.FIELD_CLASSIFICATION_MATRIX` /
`worker_events.classification_matrix()`. The tables below document that closed
matrix for the Codex app-server notification envelope currently recorded by
Brigade.

### Envelope

| Field | Class |
| --- | --- |
| `jsonrpc` | public_metadata |
| `method` | public_metadata |
| `params` | public_metadata (nested matrix below) |

### Methods recorded by the app-server transport

| Method | Public params | Stripped (private/secret/prohibited) |
| --- | --- | --- |
| `turn/started` | `threadId`, `turn.id`, `turn.status`, scrubbed `turn.items` | `turn.error`, private item payloads |
| `turn/completed` | same as started | provider `turn.error` bodies |
| `item/started` | `threadId`, `turnId`, `item.id`, `item.type`, safe item public fields | prompts, text, commands, cwd, diffs, tool args/results |
| `item/completed` | same as started, plus `completedAtMs` | same as started |
| `item/commandExecution/requestApproval#auto-declined` | `threadId`, `turnId`, `itemId`, `availableDecisions` | `command`, `cwd`, `reason`, headers, cookies, env, credentials, prompts, grant roots |

Only the recorded approval auto-decline method above is accepted. Other
`*#auto-declined` bases fail closed.

Delta methods (`item/*/delta`, …) are never recorded by Brigade and are
intentionally absent. Unknown methods or fields fail scrubbing with a bounded
diagnostic.

### Turn fields

| Field | Class |
| --- | --- |
| `id` | public_metadata |
| `status` | public_metadata |
| `items` | public_metadata (each element via item-type matrix) |
| `error` | prohibited |

### Item types

Every supported item type keeps `id` and `type` as public metadata. Content
fields are private or prohibited. Closed per-type key set:

| Item type | Public fields | Stripped fields |
| --- | --- | --- |
| `userMessage` | `id`, `type`, `clientId` | `content` |
| `agentMessage` | `id`, `type`, `phase` | `text` |
| `plan` | `id`, `type` | `text` |
| `reasoning` | `id`, `type` | `summary`, `content` |
| `commandExecution` | `id`, `type`, `status`, `exitCode`, `durationMs` | `command`, `cwd`, `commandActions`, `aggregatedOutput` |
| `fileChange` | `id`, `type`, `status` | `changes` |
| `mcpToolCall` | `id`, `type`, `server`, `tool`, `status`, `pluginId` | `arguments`, `appContext`, `result`, `error`, `mcpAppResourceUri` |
| `dynamicToolCall` | `id`, `type`, `tool`, `status`, `success`, `durationMs` | `arguments`, `contentItems` |
| `collabToolCall` | `id`, `type`, `tool`, `status`, `senderThreadId`, `receiverThreadId`, `newThreadId`, `agentStatus` | `prompt` |
| `webSearch` | `id`, `type` | `query`, `action` |
| `imageView` | `id`, `type` | `path` |
| `enteredReviewMode` | `id`, `type` | `review` |
| `exitedReviewMode` | `id`, `type` | `review` |
| `contextCompaction` | `id`, `type` | _(none)_ |

### Global secret keys

These keys are secret wherever they appear under `params` (including nested
objects that would otherwise be public): `authorization`, `Authorization`,
`cookie`, `Cookie`, `cookies`, `set-cookie`, `Set-Cookie`, `credentials`,
`apiKey`, `api_key`, `token`, `accessToken`, `refreshToken`, `password`,
`secret`, `env`, `environment`, `headers`.

Absolute home paths travel in `cwd` / `path` / `grantRoot` and are classified
`private_content`. Provider error bodies are `prohibited`.

## Schemas

- Scrubbed event: `brigade.worker_event_scrubbed.v1`
- Scrubbed stream document: `brigade.worker_event_stream_scrubbed.v1`

Golden adversarial fixtures:
`src/brigade/fixtures/worker-events-appserver.v1.golden.json`.

## Acceptance mapping (#592 first slice)

| Criterion | Enforcement |
| --- | --- |
| Documented matrix covers recorded Codex app-server events | this document + `classification_matrix()` |
| Unknown types/fields fail closed with bounded diagnostics | `WorkerEventError` / `MAX_DIAGNOSTIC_LEN` |
| Credentials, headers, cookies, env, prompts, private content, home paths, provider error bodies absent | scrub omit + `scrubbed_projection_omits_sensitive_material` + adversarial goldens |
| Order, stable IDs, safe operation names, schema versions, source digests preserved | scrubbed stream document + golden stream case |
| Distinct raw/scrubbed media types and artifact classes | `media_types()` / `artifact_classes()` |
| Audit/replay reject raw unless `local-only` | `load_stream_for_consumer`, `run_audit.reject_raw_worker_stream_evidence` |
| Adversarial goldens for the supported transport | fixture cases for every matrix item type |
| Legacy streams inspectable and unclassified until scrubbed | `inspect_stream_file` |

## Non-goals (first slice)

- Legacy backfill of historical streams
- Export/archive wiring for scrubbed sidecars
- Treating redaction as a substitute for access control
- Replacing the ProvenanceEnvelope trust contract (#498)
