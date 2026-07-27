# Proposal: ProvenanceEnvelope on every evidence item

Status: draft, executor-ready. Not implemented.

Issue: [#505](https://github.com/escoffier-labs/brigade/issues/505).

## BLUF

Attach one **`ProvenanceEnvelope`** to every evidence item at ingest and every
inter-seat message at its send boundary. Consumers derive entitlements from a shared
**`trust_policy`** and enforce trust, integrity, and brief caps on read or receive.
Schema evolution follows merged PR
[#562](https://github.com/escoffier-labs/brigade/pull/562); exact-byte hashing follows
merged PR [#563](https://github.com/escoffier-labs/brigade/pull/563).

**Ingestion:** eight architectural boundaries converge on MiseLedger `items`, Python
`.brigade/work/imports.jsonl`, or research checkpoints. Central fan-in: Go `importAdapterReaderProgress` to
`upsertRecord`; Python `ledger._append_import_records` (41 direct GraphTrail callers);
`receipts_cmd._miseledger_item` dispatches to `_verify_miseledger_item` or
`_run_miseledger_item` for receipt indexing ([#552](https://github.com/escoffier-labs/brigade/pull/552)).
Research findings persist through `research.registry.save_checkpoint`. Inter-seat
messages converge on `build_plan_prompt`, `_worker_prompt`, and `build_synth_prompt`
before transport dispatch.

**Consumption:** eleven architectural boundaries read evidence or inter-seat messages. Central fan-out: Go
`showItem`, `evidenceBundle`, and search/explain; Python `evidence_brief` bundle fetch
and render; research report/handoff renderers; `context_cmd._context_payload` for
context packs; and `parse_plan`, worker dispatch, and synthesis for inter-seat receive
gates.

## Problem

Brigade evidence today carries partial provenance in three incompatible shapes:

1. **MiseLedger** (`engines/evidence-ledger/`): `adapter.Record` plus SQLite columns
   `content_hash`, `raw_hash`, `source_kind`, and indexed metadata keys. Imports are
   always treated as untrusted at export (`untrusted_context: true` in
   `internal/app/app.go` and `internal/app/mcp.go`).
2. **Work imports** (`.brigade/work/imports.jsonl`): `source`, `kind`, and ad hoc
   `metadata` with `source_fingerprint`, scanner fields, and injection flags on
   `kind=context` only (`work_cmd/imports.py`).
3. **Research findings** (`research/types.py`): `Finding.trust` in
   `{"local","web","cli","browser"}` with no content hash or ingest path.

Nothing unifies `{source, modality, trust, content_hash}` across Go archive rows, Python
inbox records, brief injection, and promotion gates. Operators cannot answer who may act
on this item or which bytes were hashed without path archaeology. Missing envelopes are
sometimes treated as safe by omission.

Inter-seat payloads have the same gap. Planner output becomes worker assignments, worker
output becomes synthesis input, and prior-stage results can enter later worker prompts
without a boundary record that says model-generated and untrusted.

The two July incidents exercise different failure modes. [#555](https://github.com/escoffier-labs/brigade/issues/555)
showed that content can reach an injection-sensitive consumer without a classification
or scan verdict. [#546](https://github.com/escoffier-labs/brigade/issues/546) showed that
a mechanically valid receipt can still carry a meaningless signal. The envelope makes
both states visible: injection state gates content use, while source, origin, and session
attribution remain available to selection and scoring even when integrity is verified.

## Goal

Stamp a versioned **`ProvenanceEnvelope`** at ingest on every new item and at send time
on every inter-seat message. During migration, synthesize a non-null envelope on read
for legacy rows. Consumers derive entitlements from `trust_policy` and enforce label
rules, integrity checks, and brief caps. Hash checks prove integrity of persisted or
in-flight bytes, not factual truth.

## Non-goals

- Origin-scoped redaction before persistence ([#498](https://github.com/escoffier-labs/brigade/issues/498)).
- Selection transparency in briefs ([#495](https://github.com/escoffier-labs/brigade/issues/495)).
- Causal lineage graph across plan/run/verify/outcome/handoff ([#493](https://github.com/escoffier-labs/brigade/issues/493)).
- Context-pack freshness snapshots ([#492](https://github.com/escoffier-labs/brigade/issues/492)).
- Per-seat tool CandidateSetGate ([#504](https://github.com/escoffier-labs/brigade/issues/504)).
- Replacing `untrusted.wrap_untrusted` or content-guard egress scans.
- Remote provenance registries or cross-machine trust federation.
- Retaining complete planner, worker, or synthesis prompt bodies when Brigade does not
  retain them today. Message receipts store the envelope and routing metadata, not the
  message text.

## Schema: `brigade.provenance-envelope.v1`

Top-level object. Stored inside MiseLedger `items.metadata_json.provenance`, Python
work-import `metadata.provenance`, research
`checkpoint.json.findings[].provenance`, or a message-envelope receipt beside the run.
Exported research handoff/work-import rows copy the finding envelope into
`metadata.provenance`. Target serialized size is at most 1 KiB; validator hard ceiling
is 4 KiB.

```json
{
  "schema": "brigade.provenance-envelope.v1",
  "schema_version": 1,
  "source": {
    "system": "receipts",
    "kind": "verify-receipt",
    "producer": "receipts_cmd._verify_miseledger_item"
  },
  "origin": "agent-session",
  "repository": {
    "id": "escoffier-labs/brigade",
    "revision": "abc123def456"
  },
  "session": {
    "id": "20260726-213137-work-verify-042161",
    "harness": "claude"
  },
  "collection_id": "brigade_work_verify_runs",
  "item_id": "verify-runs/example/receipt.json",
  "locator": {
    "kind": "repo-relative",
    "value": ".brigade/work/verify-runs/example/receipt.json"
  },
  "attribution": "observed",
  "modality": "tool-output",
  "trust": {
    "label": "untrusted",
    "assigned_by": "ingest:receipts_cmd.index_miseledger_receipts",
    "assigned_at": "2026-07-26T21:31:37.123456+00:00",
    "trust_policy": {
      "schema": "brigade.trust-policy.v1",
      "schema_version": 1
    },
    "injection": {
      "status": "clean",
      "count": 0,
      "rules": []
    }
  },
  "hashes": {
    "content_algorithm": "sha256",
    "content_scope": "item.text.utf8.v1",
    "content": "abc123...",
    "raw_algorithm": "sha256",
    "raw_scope": "exact_bytes",
    "raw": "def456..."
  },
  "captured_at": "2026-07-26T21:31:37+00:00",
  "ingested_at": "2026-07-26T21:31:38+00:00"
}
```

### Field rules

| Field | Rule |
| --- | --- |
| `schema` | Constant `brigade.provenance-envelope.v1`. |
| `schema_version` | Integer. Additive optional fields only within a version (PR #562). |
| `source.system` | Producer system: `miseledger`, `work-inbox`, `research`, `receipts`, etc. |
| `source.kind` | Producer channel within the system (adapter import, native crawl, handoff sync). |
| `source.producer` | Repo-relative symbol that wrote the envelope (for example `ingest.upsertRecord`, `ledger._make_import`). |
| `origin` | **#498 policy key.** Closed enum (below). Not a nested object. |
| `repository.id` | Stable repo identifier (for example GitHub `owner/name`); not an absolute path. |
| `repository.revision` | Commit SHA or equivalent revision when known; else `null`. |
| `session.id` | Harness session, work session, or receipt `run_id` when present. |
| `session.harness` | Harness name when known (`claude`, `cursor`, `codex`, etc.). |
| `collection_id` | Adapter collection or inbox producer scope. |
| `item_id` | Stable external evidence id or message id from producer. |
| `locator.kind` | `repo-relative` or `uri`. |
| `locator.value` | Repo-relative path or URI. Never an absolute host filesystem path. |
| `attribution` | Closed enum: `observed`, `declared`, `inferred`. |
| `modality` | Closed set (below). |
| `trust.label` | Closed set (below). |
| `trust.assigned_by` | `ingest:<symbol>`, `scanner:<symbol>`, `verifier:<symbol>`, or `operator:<command>`. |
| `trust.assigned_at` | UTC ISO-8601 from `localio.utc_now_iso()` or Go RFC3339Nano. |
| `trust.trust_policy` | Reference to shared policy schema/version; entitlements are derived at consumers, not stored per envelope. |
| `trust.injection` | `{status, count, rules}` where status is `clean`, `flagged`, `pending`, or `error`. |
| `hashes.content_scope` | `item.text.utf8.v1` for evidence or `message.text.utf8.v1` for inter-seat messages. |
| `hashes.raw_scope` | Always `exact_bytes` when `raw` is present. |
| `hashes.content` / `hashes.raw` | Bare 64-character lowercase hexadecimal SHA-256 digest, matching PR #563. |
| `captured_at` | Producer timestamp when known; else ingest time. |
| `ingested_at` | Wall time envelope was written. |

Null display: JSON `null` for unknown optional fields. CLI and briefs render unknown
origin fields as omitted lines, not the string `"null"`. Legacy synthesized envelopes
display **`UNKNOWN PROVENANCE - legacy item`**.

Serialization follows PR #562: integer `schema_version`, additive optional fields
within v1, readers ignore unknown keys, UTF-8, and sorted object keys. JSON documents
end with one newline; JSONL writes one sorted object per line. Embedded SQLite
`metadata_json` uses the same sorted-key encoding without a document newline.

### Origin closed set (#498 policy key)

| Value | Meaning |
| --- | --- |
| `operator-input` | Typed or pasted operator content |
| `workspace` | Repo-local files and workspace artifacts |
| `agent-session` | Harness or agent session output |
| `external-service` | API or CLI from outside the repo |
| `external-web` | Web fetch or browser capture |
| `unknown` | Legacy or insufficient context |

Redaction policy ([#498](https://github.com/escoffier-labs/brigade/issues/498)) keys off
this field. Envelopes never store absolute repo roots.

### Modality closed set

| Value | Typical surface |
| --- | --- |
| `human-written` | Operator notes, handoff text, declared human input |
| `model-generated` | LLM completions and summaries |
| `tool-output` | Tool calls, scanner output, verify receipts, command records |
| `external-web` | Research web or browser findings |
| `mixed` | Combined human and model or tool content |
| `unknown` | Legacy backfill or insufficient classification |

New modalities require a `schema_version` bump and a row in `docs/import-schema.md` or
`engines/evidence-ledger/docs/SCHEMA.md`.

### Trust labels, authority, and policy

| Label | Meaning | Default consumer behavior |
| --- | --- | --- |
| `unknown` | Legacy or unclassified | Search and forensic show only; excluded from briefs and context by default |
| `untrusted` | Imported external text; digest may be present | Wrapped in briefs; capped at 2 items and 50% of brief bytes |
| `reviewed` | Explicit operator review or verifier-authored review receipt | May be briefed, cited, and promoted with label |
| `verified` | Mechanical binding or integrity proof passed | May be briefed, cited, and promoted with label |
| `quarantined` | Pending/failed injection scan, injection signal, or integrity downgrade | Metadata-only search and show; excluded from brief, context, cite, and promote |

**Authority**

- An inbound adapter envelope is data, not authority. The central ingester validates
  identity fields and downgrades any unproved `reviewed` or `verified` assertion.
- Only a registered path named in `trust.assigned_by` may set the initial label.
- **`verified` means mechanical binding and integrity** (receipt digest match, unchanged
  `content` hash, signed receipt chain). It does **not** mean factual truth.
- A clean injection scan does **not** upgrade trust.
- Injection hit downgrades to `quarantined`.
- Upgrades require unchanged `hashes.content`, an append-only evidence event, and an
  explicit operator action or verifier-authored receipt. An ingest or indexing path
  cannot upgrade the item it just wrote.
- Downgrades append an event; rows are never deleted.

Initial label assignment is closed:

| Label | Who may assign it |
| --- | --- |
| `unknown` | Legacy read synthesizer or an ingester that cannot classify a required field |
| `untrusted` | Normal ingesters after a clean scan or for non-injectable tool metadata |
| `reviewed` | Explicit operator review or a registered review verifier, with an event |
| `verified` | Registered verifier after checking the named mechanical binding, with an event |
| `quarantined` | Ingester for a pending scan; scanner on a hit/error; integrity checker on mismatch |

A clean scan may release a pending procedural quarantine to `untrusted`, with an event
and unchanged content hash. This is not an upgrade to `reviewed` or `verified`.

Concrete upgrade paths:

- `brigade evidence trust review <item-ref> --content-hash <digest>` is an explicit
  operator attestation. It may move unchanged content from `untrusted` to `reviewed`
  and appends a transition event.
- `brigade receipts verify` may move an indexed verify-receipt item to `verified` only
  through the proof checks below.
- No command may promote `unknown` or `quarantined` content without first resolving its
  missing classification, scan, or integrity failure.

**`trust_policy` (not per-envelope entitlements)**

Store `trust.trust_policy.schema` and `trust.trust_policy.schema_version` only.
Consumers load `brigade.trust-policy.v1` fixtures and derive entitlements:

| Label | Derived entitlements |
| --- | --- |
| `unknown` | `search`, `show_metadata`, `forensic_content_reveal` |
| `untrusted` | `search`, `show`, `brief_wrapped` (caps apply) |
| `reviewed` | `search`, `show`, `brief`, `cite`, `promote` |
| `verified` | `search`, `show`, `brief`, `cite`, `promote` |
| `quarantined` | `search_metadata`, `show_metadata` |

Existing human framing is preserved: `untrusted.wrap_untrusted` still wraps brief text;
the envelope adds machine-readable trust beside human-readable fences.

### Exact hashed bytes (PR #563 alignment)

| Hash | Input | Representation |
| --- | --- | --- |
| Envelope `hashes.content` | SHA-256 of the **exact UTF-8 bytes** of the persisted item `text` field | Bare 64-character lowercase hex, scope `item.text.utf8.v1` |
| Message envelope `hashes.content` | SHA-256 of the **exact UTF-8 bytes** sent across the seat boundary | Bare 64-character lowercase hex, scope `message.text.utf8.v1` |
| Envelope `hashes.raw` (when present) | SHA-256 of exact retained source bytes (JSONL line, file slice) | Bare 64-character lowercase hex, scope `exact_bytes` |

Rules:

- No trimming, newline normalization, or Unicode normalization for envelope `content`.
- Every v1 ingester materializes one persisted `text` field before hashing. Work imports
  and MiseLedger already have it. Research findings add a persisted `text` projection
  used by report and handoff renderers while retaining `title`, `summary`, and `evidence`
  for compatibility.
- A research finding `text` projection is exactly
  `{title}\n{summary}\n{evidence}` encoded as UTF-8. The writer adds only those two
  newline separators, does no trimming or normalization, and treats missing fields as
  empty strings.
- **Artifacts are not included** in item `content` hash. Each artifact keeps its own
  `content_hash` in MiseLedger `artifacts`.
- **No canonical JSON** as the envelope content-hash convention.
- PR #563 hashes exact retained `changes.patch` bytes and writes a bare lowercase digest.
  The envelope uses the same algorithm, exact-byte rule, and representation. Its byte
  scope is persisted item text rather than patch bytes and is named explicitly.
- **Migration compatibility:** MiseLedger SQLite `items.content_hash` today uses
  `textnorm.Normalize(strings.TrimSpace(item.text + "\n" + summary))` for identity and
  dedupe. Artifact bodies are excluded. Keep that column and algorithm separate during
  migration. Do not rename SQLite `items.content_hash`; it keeps the existing normalized
  dedupe algorithm. New envelope `hashes.content` is a separate contract and indexed
  projection that follows PR #563 exact-byte rules on item text only. Tamper checks
  compare envelope `content` to recomputed exact UTF-8 bytes.

**Hot-path cost:** one streaming SHA-256 over item text bytes per new item, O(text
bytes).

## Read verification and tamper response

Recompute `hashes.content` (and `raw` when materialized) on search, show, evidence
bundle, MCP, brief, and context surfaces for selected or materialized items. If a
surface emits an artifact body, verify its existing artifact `content_hash` too. Cap
verification to the existing 200-result search limit.

| Surface | Mismatch behavior |
| --- | --- |
| Search | Suppress mismatched snippet; attach `integrity_mismatch: true` on the hit |
| Show (direct) | Return typed `integrity_mismatch`; hide body unless the operator passes `--forensic-content` |
| Evidence bundle / MCP / HTTP | Return metadata and mismatch only; no forensic content reveal in v1 |
| Brief / context | Omit item; increment `integrity_omitted` counter |
| All | Append one downgrade event per item/hash/mismatch to the owning store; never delete the row |

Hash proves **integrity only**, not authenticity or factual correctness. The envelope
hash and legacy SQLite `content_hash` have different byte scopes. Verify each against its
own contract and never compare them for equality.

### Forensic content reveal

Add `--forensic-content` to direct `miseledger show` and
`brigade evidence show`. It reveals a legacy `unknown` body with the unknown-provenance
banner, or an integrity-mismatched body after printing
`integrity_mismatch: true`; it never changes trust. It does not reveal content whose
injection status is `flagged`, `pending`, or `error`. MCP, HTTP, evidence bundles,
briefs, and context packs remain metadata-only on mismatch in v1, so an agent cannot
opt itself into unsafe content. Integrity-mismatch reveal is a direct operator
diagnostic exception, not a general `quarantined` entitlement.

## Inter-seat message boundary

Every call that crosses from one Brigade seat to another creates an envelope over the
exact outbound string and verifies it again immediately before the receiving parser or
model sees it. Message bodies remain in memory unless an existing receipt already keeps
them. Append `message-envelopes.jsonl` beside `run.json` with `message_id`, `phase`,
`from_seat`, `to_seat`, and the envelope, but no message body.

Closed `source.kind` values are `plan-request`, `plan-result`, `worker-request`,
`worker-result`, `synthesis-request`, and `synthesis-result`. All model and tool output
starts `untrusted`, as issue #505 requires. The message policy is channel-specific:

- Brigade-composed request prompts may cross only from an allowlisted producer symbol
  after every embedded evidence item passed its own trust gate.
- Planner results enter only `parse_plan` and its closed JSON contract.
- Worker and prior-stage results enter later prompts only through
  `wrap_untrusted`, with bounded bytes and their envelope label visible.
- Synthesis results may enter receipts and the final response, but never become trusted
  evidence merely because synthesis completed.
- A pending, error, flagged, quarantined, unknown, or hash-mismatched message is not
  delivered. Its receipt records metadata and the rejection reason.

Legacy run receipts did not retain enough message bytes to reconstruct hashes. On read,
show `UNKNOWN PROVENANCE - legacy message`, `trust.label=unknown`, and a null content
hash. Never replay or inject such a legacy message.

## Backfill and legacy read synthesis

1. **Resumable batch backfill:** `miseledger doctor provenance backfill` walks `items`
   in batches, writing inferred envelopes from `source_kind`, locator, and legacy hashes.
2. **Work inbox:** `brigade work import provenance --backfill` stamps rows missing
   `metadata.provenance` in `work_cmd/imports.py`.
3. **Research:** `brigade research provenance backfill` adds inferred envelopes and the
   exact `text` projection to legacy checkpoint findings without changing legacy
   `Finding.trust`.
4. **On-read synthesis:** When `provenance` is absent, synthesize a non-null envelope
   with `origin=unknown`, `modality=unknown`, `trust.label=unknown`, `attribution=inferred`,
   and display **`UNKNOWN PROVENANCE - legacy item`**. Never treat a missing envelope as
   trusted.
5. **Verified assignment:** Backfill may set `verified` only after actual receipt
   verification by `brigade receipts verify` or an equivalent verifier-authored receipt,
   never from field presence or indexing alone.
6. **Idempotent:** Second pass makes no changes when envelope content hash matches.
7. **Legacy messages:** Do not invent a digest from partial run receipts. Synthesize
   metadata-only `unknown` envelopes and keep the message ineligible for replay.

## Cost and lazy work

| Work | Bound |
| --- | --- |
| Ingest/send hash | One streaming SHA-256 per item or message, O(content bytes) |
| Envelope size | Target <= 1 KiB; validator rejects > 4 KiB |
| Locator / ids | Enforced max lengths in validator |
| Source defaults | Batch-level defaults in ingest paths to avoid per-row bloat |
| Read verification | Capped by existing 200-result search limit |
| Backfill | Lazy, resumable, operator-initiated |
| Injection scan | Lazy storage is allowed only when `trust.injection.status="pending"` |
| New writes | A content-emitting consumer must resolve `pending` synchronously or omit the item |
| Message receipt | One envelope plus bounded routing metadata; no new prompt-body retention |

## Ingestion map (eight boundaries)

Architectural ingress boundaries. All MiseLedger paths converge through
`importAdapterReaderProgress` to `upsertRecord` (`internal/ingest/importer.go`).

| # | Boundary | Representative paths | Converges to |
| --- | --- | --- | --- |
| 1 | MiseLedger adapter, native, crawler, watch | `cmdImportAdapter`, `cmdImportNative`, `cmdCrawl*`, `cmdImportDiscovered`, harness `*.Generate` sources | `upsertRecord` |
| 2 | Generic and manual Python work imports | `ledger._append_import_records` (41 direct GraphTrail callers), `imports.import_add` | `.brigade/work/imports.jsonl` via `_make_import` |
| 3 | Handoff issue ingest and sync | `handoff_cmd.issue_ops.sync_issues`, `issue_ops.import_issues` | work inbox |
| 4 | Chat-memory and memory-refresh sweeps | `imports.import_chat_sweep`, `services._chat_sweep_records`, `chat_cmd.sweep_import_issues`, memory-refresh producers | work inbox |
| 5 | Scanner, content-guard, and review imports | `scanners._scanners_run_payload`, `scanners._scanner_stamp_new_imports`, `work_cmd/reviews.py` paths | work inbox |
| 6 | Research findings and exported research handoffs | `research.extract`, `research.registry.save_checkpoint`, `research_cmd.run`, `research_cmd.export_handoff` | research checkpoint, then handoff/work inbox |
| 7 | Receipt indexing ([#552](https://github.com/escoffier-labs/brigade/pull/552)) | `receipts_cmd.index_miseledger_receipts`, `receipts_cmd._collect_export_receipts`, `verification._attach_miseledger_indexing`; `_miseledger_item` to `_verify_miseledger_item` / `_run_miseledger_item` | MiseLedger adapter import |
| 8 | Inter-seat message emission | `build_plan_prompt`, planner result capture, `_worker_prompt`, `WorkerResult`, `build_synth_prompt`, synthesis result capture | transient boundary plus `message-envelopes.jsonl` |

The Python work-inbox boundary covers all 15 current
`PROVENANCE_AUDIT_SOURCES`: `backup-health`, `chat-memory-sweep`, `code-review`,
`context-pack`, `handoff-ingest`, `learning-loop`, `memory-care`,
`memory-refresh`, `project-consolidation`, `repo-fleet`, `repo-fleet-release`,
`roadmap-audit`, `scanner-health`, `security-scan`, and `tool-catalog`. New callers of
`_append_import_records` inherit the central stamp, while producer-specific tests assert
correct origin and modality rather than only envelope presence.

## Consumption map (eleven boundaries)

Architectural surfaces that must honor envelope trust, derived entitlements, and
integrity checks.

| # | Boundary | Representative symbols | Notes |
| --- | --- | --- | --- |
| 1 | Search and explain | `cmdSearch`, `cmdExplain`, `evidence_cmd.run_engine("search")`, `handleSearch` | Suppress mismatched snippets |
| 2 | Show item | `showItem`, `evidence_cmd.run_engine("show")`, `handleItem`, `mcpShow` | Hide body on mismatch without forensic flag |
| 3 | Evidence bundles, cache, Markdown | `evidenceBundle`, `cmdEvidence`, `writeEvidenceMarkdown`, `evidence_brief.fetch_evidence_bundle`, `evidence_brief.render_evidence_bundle` | Per-item integrity in bundle |
| 4 | Sessions, collections, browser | `cmdSessions`, `listSessions`, `searchSessions`, `handleSessions`, `handleSessionItems` | Session-scoped reads |
| 5 | SQL and export | `receipts_cmd.export_miseledger`, `repos_cmd.adoption._miseledger_hashes`, `operator_cmd.lifecycle._checkup_ledger` | Export carries envelope metadata |
| 6 | Aboyeur run briefs | `aboyeur.run`, `aboyeur._prepend_optional_briefs`, `aboyeur.build_plan_prompt`, `aboyeur._worker_prompt` | Trust caps and omit rules |
| 7 | `import_context_from_miseledger` notes | `imports.import_context_from_miseledger`, `imports._append_active_context_note`, `cli/work/dispatching.py` | Context note provenance |
| 8 | Work import triage, show, promotion, handoff | `work_cmd.import_provenance`, `services._import_provenance_payload`, `memory_cmd._miseledger_evidence_id`, handoff promote paths | Promotion gates |
| 9 | Research reports and handoff exports | `research.report.render_markdown`, `research.report.render_html`, `research.handoff.render_handoff`, `research_cmd.export_handoff` | Replace legacy `Finding.trust` display with envelope origin/modality/trust |
| 10 | Context packs, center, repo action packs | `context_cmd._context_payload`, `context_cmd.build`, `tools_cmd.packs.pack_build`, `work_cmd.session.briefing.brief`, `center_cmd` action surfaces | Forward-compatible with [#492](https://github.com/escoffier-labs/brigade/issues/492) |
| 11 | Inter-seat receive gates | `parse_plan`, `run_transport.dispatch`, `run_transport._dag_dispatch`, prior-stage prompt composition, `build_synth_prompt`, synthesis receipt writer | Verify hash, channel, trust, scan state, schema, and caps before delivery |

## #555 injection quarantine

Coordinate with [#555](https://github.com/escoffier-labs/brigade/issues/555) without
merging egress and injection verdicts.

At ingest, any path that runs `untrusted.scan_untrusted` and finds a hit sets
`trust.label=quarantined` and `trust.injection.status=flagged`. Extend scans to
`ledger._make_import` for injectable external text. A Go adapter without a matching
scanner sets `trust.label=quarantined` and `trust.injection.status=pending`; it does not
claim a clean verdict.

Map `scan_untrusted()` to `status=flagged` when `count > 0`, otherwise `clean`.
Populate `rules` from the matching rule ids. Do not retain excerpts in the envelope.

If `trust.injection.status=pending`, every content-emitting consumer runs the scan before
rendering. A clean result releases the item only to `untrusted`. A hit quarantines the
item. An unavailable or incomplete scan records `status=error` and is fail-closed:
briefs, context packs, MCP content responses, promotion, and citations omit the body.
Search may return metadata only. New ingest paths cannot bypass this gate by leaving the
field pending.

`handoff lint --content-guard` remains a separate axis; envelope quarantine does not
change lint exit codes.

## Auditable trust transitions

Trust transitions use the store that owns the item:

- MiseLedger adds an append-only `provenance_events` table keyed by item id.
- Python work inbox items append to `.brigade/work/provenance-events.jsonl`.
- Research findings append to the run's `provenance-events.jsonl`.

`item_ref` has three closed forms: `miseledger:item:<id>`,
`work-import:<import_id>`, or `research-finding:<run_id>:<index>`.

```json
{
  "schema": "brigade.provenance-event.v1",
  "schema_version": 1,
  "at": "2026-07-26T22:00:00+00:00",
  "item_ref": "miseledger:item:abc123",
  "from_label": "untrusted",
  "to_label": "verified",
  "envelope_content_hash": "abc123...",
  "content_scope": "item.text.utf8.v1",
  "operator_command": "brigade receipts verify",
  "evidence": { "receipt_sha256": "..." }
}
```

`envelope_content_hash` is the bare 64-character lowercase digest and must match the
embedded envelope at transition time. Update the embedded envelope `trust` fields in
place. Events are mandatory for upgrades and downgrades.

### Trust upgrade through receipt verification

`index_miseledger_receipts` always ingests receipt items as `untrusted`; indexing alone
cannot verify them. After existing `brigade receipts verify` checks pass:

1. Resolve each indexed verify-receipt item through its stable external id or export
   cursor.
2. Recompute its envelope content hash and stop on mismatch.
3. Require a complete receipt v2 `baseline_commit`, `tree_fingerprint`, and
   `changes_patch_sha256` tuple, plus an on-disk `changes.patch` whose exact-byte digest
   matches `changes_patch_sha256`.
4. Move that receipt item to `verified` and append one provenance event naming the
   verifier receipt digest and envelope content hash.
5. When the item is already `verified` with the same hash, make the operation an
   idempotent no-op.

This verifies the indexed receipt item and its patch binding. It does not verify claims
inside arbitrary evidence mentioned by the receipt.

## Related issue interfaces (one line each)

| Issue | Interface |
| --- | --- |
| [#498](https://github.com/escoffier-labs/brigade/issues/498) | Redaction runs before persistence and consumes envelope `origin`; #498 may add optional verdict/count fields within v1, while #505 defines no redaction policy and stores no raw secrets. |
| [#495](https://github.com/escoffier-labs/brigade/issues/495) | Brief selection consumes `trust.label`; #495 owns the selected/omitted explanation and rule identifiers. |
| [#493](https://github.com/escoffier-labs/brigade/issues/493) | Lineage edges reference `item_ref` and envelope `session.id`; envelope does not embed the graph. |
| [#492](https://github.com/escoffier-labs/brigade/issues/492) | Pack freshness owns generator and dependency snapshots; copied evidence keeps its per-item envelope. |
| [#504](https://github.com/escoffier-labs/brigade/issues/504) | CandidateSetGate may consume trust labels and records envelope item refs in run receipts; it does not assign trust. |

## Reconciliation with #474 and #502

Proposal PR [#575](https://github.com/escoffier-labs/brigade/pull/575) for
[#474](https://github.com/escoffier-labs/brigade/issues/474) and proposal PR
[#570](https://github.com/escoffier-labs/brigade/pull/570) for
[#502](https://github.com/escoffier-labs/brigade/issues/502) were reviewed before
finalizing this contract.

| Issue | Relationship |
| --- | --- |
| [#474](https://github.com/escoffier-labs/brigade/issues/474) | `brigade.worker_failure.v1`, seat health, retry, and outcome domain remain receipt fields; an indexed receipt's envelope carries source, modality, item integrity, and `session.id`, not failure meaning or seat health state. |
| [#502](https://github.com/escoffier-labs/brigade/issues/502) | Scorecards continue to require verifier-authored `subject_binding`, `check_role`, patch identity, and #474 taxonomy. Its skill `content_fingerprint` has a different object/scope; when it binds the same item text it references the envelope digest rather than rehashing it. Envelope `trust.label=verified` can admit an indexed receipt to consumers but cannot make it scoreable. |

When #502 `subject_binding` binds the same exact bytes as an evidence item's persisted
`text`, its `content_fingerprint` is the bare envelope `hashes.content` value without a
`sha256:` prefix. Receipt patch identity continues to use bare
`changes_patch_sha256` per #563. A different object or byte scope keeps its own named
digest and cannot be compared for equality.

No overlap sufficient to merge issues; implement #505 first as the shared metadata layer.

## Implementation slices

Six ordered sub-issues. Schema lands first, item ingestion follows, then message and
consumer enforcement.

### Slice 1 (S): Shared schema, policy fixtures, and validators

**Scope:** Add `src/brigade/provenance.py` (envelope builder, validator, read synthesis).
Add `internal/provenance/envelope.go` mirror. Add
`src/brigade/fixtures/trust-policy.v1.json` and envelope golden fixtures. Document in
`docs/import-schema.md` and `engines/evidence-ledger/docs/SCHEMA.md`.

**Tests:** `tests/test_provenance_envelope.py` (round-trip, closed sets, size ceiling,
legacy synthesis displays `UNKNOWN PROVENANCE - legacy item`);
`engines/evidence-ledger/internal/provenance/envelope_test.go` (Go parity on
`item.text.utf8.v1` hash).

**Acceptance**

- [ ] Python and Go produce identical `hashes.content` for shared fixtures.
- [ ] Validator rejects envelopes > 4 KiB and absolute-path locators.
- [ ] `./scripts/verify` and `go test ./internal/provenance/...` pass.

### Slice 2 (M): Go persistence, ingest, and backfill

**Scope:** Stamp envelope in `ingest.upsertRecord` (`internal/ingest/importer.go`).
Preserve legacy `content_hash` column for dedupe. Add
`miseledger doctor provenance backfill` in `internal/app/app.go`. On-read synthesis in
`showItem` when metadata lacks `provenance`. Persist the full envelope JSON and indexed
scalar projections for `origin`, `modality`, `trust.label`, content hash scope, and
content hash digest. Add the append-only MiseLedger `provenance_events` table.

**Tests:** `engines/evidence-ledger/internal/ingest/importer_test.go`,
`engines/evidence-ledger/internal/app/app_test.go` (backfill idempotency, synthesis).

**Acceptance**

- [ ] Fresh `miseledger import adapter` rows include `metadata_json.provenance`.
- [ ] Search and SQL can filter indexed `origin`, `modality`, and `trust.label` without
      parsing nested JSON.
- [ ] Legacy row show returns synthesized `unknown` envelope, not implicit trust.
- [ ] MiseLedger trust transitions append immutable event rows.
- [ ] `scripts/smoke_archive.sh` passes.

### Slice 3 (M): Python fan-in and receipt export

**Scope:** Stamp envelope in `ledger._make_import`, `imports.import_context`,
`scanners._scanner_stamp_new_imports`, `handoff_cmd.issue_ops`, chat and memory sweep
paths, `research.registry.save_checkpoint`, research report/handoff renderers, and
`receipts_cmd.index_miseledger_receipts`. Extend `import_provenance` in
`work_cmd/imports.py`. Preserve legacy `Finding.trust` as a source-tier compatibility
field; it cannot assign envelope trust.

**Tests:** `tests/test_work_cmd_imports.py` (`test_work_import_provenance_audits_cross_producer_contract`),
`tests/test_receipts_cmd.py` (receipt indexing envelope),
`tests/test_work_cmd_verification.py` (verify receipt trust).

**Acceptance**

- [ ] New imports from `PROVENANCE_AUDIT_SOURCES` in `work_cmd/constants.py` include envelopes.
- [ ] Research findings persist a hashed `text` projection and map legacy
      `local|web|cli|browser` to origin/modality, never directly to trust.
- [ ] Injection fixture sets `quarantined`.
- [ ] `brigade work import provenance` reports `missing_envelope` for legacy rows.

### Slice 4 (M): Inter-seat message envelopes and receive gates

**Scope:** Stamp and verify messages around `build_plan_prompt`, planner result parsing,
`_worker_prompt`, `run_transport.dispatch`, `run_transport._dag_dispatch`, prior-stage result composition,
`build_synth_prompt`, and synthesis result capture. Append metadata-only
`message-envelopes.jsonl`; do not add prompt-body retention.

**Tests:** `tests/test_aboyeur.py`, `tests/test_run_transport.py`,
`tests/test_run_receipts.py` (all six message kinds, exact-byte mismatch, planner schema
gate, prior-result wrapping, quarantine rejection, legacy unknown display).

**Acceptance**

- [ ] Every planner, worker, and synthesis send/receive boundary carries one envelope
      with `message.text.utf8.v1`.
- [ ] Model and tool outputs start `untrusted`; no consumer silently upgrades them.
- [ ] Planner JSON enters only `parse_plan`; worker/prior-stage text is wrapped and
      capped before prompt composition.
- [ ] Quarantined, unknown, pending/error scan, and hash-mismatched messages are not
      delivered.
- [ ] Run receipts contain envelope and routing metadata but no newly retained message
      body.

### Slice 5 (M): Public read surfaces and tamper verification

**Scope:** Integrity recompute in `showItem`, `evidenceBundle`, `cmdSearch`,
`internal/app/server.go`, `internal/app/mcp.go`, and `evidence_brief.fetch_evidence_bundle`.
Append downgrade events to the owning store. Surface `integrity_mismatch` in JSON. Add
the operator-only `--forensic-content` flag to direct MiseLedger and Brigade show
commands; MCP, HTTP, bundles, briefs, and context remain metadata-only on mismatch.

**Tests:** `engines/evidence-ledger/internal/app/app_test.go` (search suppress, show
forensic), `tests/test_evidence_cmd.py`, `tests/test_evidence_runtime.py`,
`scripts/smoke_mcp.sh`, `scripts/smoke_http.sh`.

**Acceptance**

- [ ] Tampered fixture: search suppresses snippet; show hides body without
      `--forensic-content`.
- [ ] Direct `--forensic-content` reveals the body with a mismatch warning and does not
      change trust; MCP and HTTP cannot reveal it.
- [ ] Downgrade event appended; row not deleted.
- [ ] MCP `show_item` returns envelope and integrity fields.

### Slice 6 (M): Brief, context, promotion enforcement and trust-transition audit

**Scope:** Enforce derived entitlements in `evidence_brief.render_evidence_bundle`,
`aboyeur._prepend_optional_briefs`, `context_cmd._context_payload`,
`memory_cmd._miseledger_evidence_id`, promote paths, and
`.brigade/work/provenance-events.jsonl` writer hooked from `receipts_cmd.verify`. Resolve
pending injection scans before any content leaves the trust gate.

**Tests:** `tests/test_aboyeur.py` (untrusted caps: 2 items, 50% bytes),
`tests/test_context_cmd.py` (unknown omitted), `tests/test_receipts_cmd.py` (upgrade
event, no automation self-upgrade).

**Acceptance**

- [ ] `unknown` and `quarantined` omitted from default brief and context.
- [ ] `untrusted` wrapped and capped.
- [ ] Pending injection scan failure emits metadata only and cannot leak item content.
- [ ] Successful verify with a complete matching v2 patch tuple appends one upgrade
      event with unchanged `envelope_content_hash`; indexing alone stays `untrusted`.
- [ ] Duplicate verify is a no-op event.

## Verification

Executor runs after all slices through Brigade:

```bash
brigade work verify run --target . --command "./scripts/verify" --capture brigade-work
brigade work verify run --target engines/evidence-ledger --command "go vet ./..." --capture brigade-work
brigade work verify run --target engines/evidence-ledger --command "go test ./..." --capture brigade-work
brigade work verify run --target engines/evidence-ledger --command "scripts/smoke_archive.sh" --capture brigade-work
```

Report receipt ids and paste failures verbatim per `AGENTS.md`.
