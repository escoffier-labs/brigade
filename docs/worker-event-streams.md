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
`worker_events.classification_matrix()`.

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

### Item types

Every supported item type keeps `id` and `type` as public metadata. Content
fields (`text`, `content`, `command`, `cwd`, `changes`, `arguments`, `result`,
`query`, `path`, `review`, `prompt`, …) are private or prohibited. See
`classification_matrix()["item_types"]` for the closed per-type key set.

## Schemas

- Scrubbed event: `brigade.worker_event_scrubbed.v1`
- Scrubbed stream document: `brigade.worker_event_stream_scrubbed.v1`

Golden adversarial fixtures:
`src/brigade/fixtures/worker-events-appserver.v1.golden.json`.

## Non-goals (first slice)

- Legacy backfill of historical streams
- Export/archive wiring for scrubbed sidecars
- Treating redaction as a substitute for access control
- Replacing the ProvenanceEnvelope trust contract (#498)
