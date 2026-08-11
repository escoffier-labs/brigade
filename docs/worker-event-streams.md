# Worker event streams: field classification and scrubbed projections

Status: implemented for issue #592 (classification/scrubbing + export/archive
boundary).

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
`require_consumer_policy`, `project_worker_streams_for_export`,
`assert_export_tree_has_no_raw_worker_streams`).
`brigade.run_audit.reject_raw_worker_stream_evidence` is the audit/replay gate.
`brigade.work_run_archive.export_run` is the portable archive gate.

## Export / archive boundary

`brigade runs export` never copies raw `events/<worker>.jsonl` into a
`brigade.work-run` archive as public support data. On the export staging tree
only:

1. each raw worker stream is scrubbed with the fail-closed scrubber
2. a distinct sidecar `events/<worker>.scrubbed.json` is written
3. the raw NDJSON is removed from the export copy
4. the manifest classifies the sidecar as
   `role=artifact`, `privacy_class=redacted`,
   `media_type=<scrubbed media type>`,
   `nested_schema=brigade.worker_event_stream_scrubbed.v1`

If any method, field, or item is unknown or unsafely classified, export refuses
with a bounded `export-privacy` diagnostic and leaves the source run unchanged.
`validate-archive` / import also refuse raw worker streams and misclassified
scrubbed sidecars. Local raw streams remain available under
`policy=local-only` for resume/salvage; that policy is never implied by export.

## Raw capture vs scrub matrix

Raw Codex app-server notification capture is intentionally open-ended for
non-delta methods. `codex_appserver` forwards every non-delta notification to
`on_event`, and the worker event writer appends that payload to
`events/<worker>.jsonl`. Delta methods (`item/*/delta`, …) are consumed for
salvage only and are never written.

That recorder path is **not** a closed allowlist. Unknown non-delta methods may
appear in the raw local stream. They remain local/`unclassified` until
classified, and scrub/export consumers reject them fail-closed with a bounded
diagnostic. The field-classification matrix below is the closed scrub/export
contract for known methods and item types only; it must not be read as the set
of methods the recorder is allowed to capture.

## Field-classification matrix

Source of truth: `worker_events.FIELD_CLASSIFICATION_MATRIX` /
`worker_events.classification_matrix()`. The tables below document the closed
scrub/export matrix for known Codex app-server notification shapes.

### Envelope

| Field | Class |
| --- | --- |
| `jsonrpc` | public_metadata |
| `method` | public_metadata |
| `params` | public_metadata (nested matrix below) |

### Methods classified for scrubbed projection

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

## Acceptance mapping (#592)

| Criterion | Enforcement |
| --- | --- |
| Documented scrub matrix covers known Codex app-server methods/item types | this document + `classification_matrix()` |
| Raw capture is open-ended for non-delta methods; matrix is not a recorder allowlist | docs + recorder-to-matrix contract test |
| Unknown types/fields fail closed with bounded diagnostics | `WorkerEventError` / `MAX_DIAGNOSTIC_LEN` |
| Credentials, headers, cookies, env, prompts, private content, home paths, provider error bodies absent | scrub omit + `scrubbed_projection_omits_sensitive_material` + adversarial goldens + archive export regressions |
| Order, stable IDs, safe operation names, schema versions, source digests preserved | scrubbed stream document + golden stream case + export sidecar |
| Distinct raw/scrubbed media types and artifact classes | `media_types()` / `artifact_classes()` + archive manifest classification |
| Audit/replay reject raw unless `local-only` | `load_stream_for_consumer`, `run_audit.reject_raw_worker_stream_evidence` |
| Export never ships raw streams as public support | `project_worker_streams_for_export`, `_classify_path`, `validate_archive` |
| Adversarial goldens for the supported transport | fixture cases for every matrix item type |
| Legacy streams inspectable and unclassified until scrubbed | `inspect_stream_file` (source run unchanged by export) |

## Non-goals

- Legacy backfill of historical streams into scrubbed sidecars on disk
- Treating redaction as a substitute for access control
- Replacing the ProvenanceEnvelope trust contract (#498)
