# Schema

The MVP uses one SQLite migration with these concepts:

- `sources`: source tools such as `discrawl`, `codex`, `hermes`, `notes`, or `gitlog`
- `collections`: bounded containers such as channels, exports, sessions, and repos
- `actors`: humans, assistants, agents, tools, bots, and systems
- `items`: atomic records such as messages, decisions, tool calls, errors, and notes
- `events`: timestamped occurrences tying source, collection, actor, and item together
- `artifacts`: files, URLs, markdown exports, patches, transcripts, and generated output
- `relations`: graph edges between items, including optional qualified target
  columns `target_source_kind` and `target_collection_external_id`
- `imports` and `import_warnings`: import run metadata
- `item_tags`: indexed item tags from adapter records
- `item_metadata`: indexed project, workspace, harness, event type, session, model, and file-path metadata
- `source_scans`: source-file scan manifests for native imports
- `source_scan_runs` / `source_scan_observed`: memory projection scan receipts and manifests
- `item_fts`: SQLite FTS5 index for item and artifact text

Items may carry `tombstoned_at` for soft removal. Live memory cards are the
latest non-tombstoned row per external id within a `brigade-memory` namespace
collection (`collection.external_id = memory-<uuid4>`), ordered by the
monotonic ingest stamp on `items.updated_at`. Legacy pre-namespace rows may
still exist under `memory:cards` and are never rebuilt or tombstoned by a
namespaced crawl.

Raw adapter lines are preserved in `items.raw_json`. Raw source references are stored in `raw_hash`, `raw_path`, and `raw_ordinal`.

The migration lives in `internal/archive/db.go`.

## Provenance Envelope

New MiseLedger `items` rows will carry a `brigade.provenance-envelope.v1` envelope under `metadata_json.provenance`. Slice 1 ships the typed Go mirror, validator, and legacy read synthesis in `internal/provenance/envelope.go`. Persistence, ingest stamping, and consumer enforcement land in later slices.

### Location

- MiseLedger: `items.metadata_json.provenance` (embedded sorted-key JSON, no document newline).
- Go mirror package: `internal/provenance` (`Envelope`, `Validate`, `SynthesizeLegacyProvenance`).

### Field sets

Closed sets enforced by `Validate`:

- `origin`: `operator-input`, `workspace`, `agent-session`, `external-service`, `external-web`, `unknown`.
- `modality`: `human-written`, `model-generated`, `tool-output`, `external-web`, `mixed`, `unknown`.
- `attribution`: `observed`, `declared`, `inferred`.
- `trust.label`: `unknown`, `untrusted`, `reviewed`, `verified`, `quarantined`.
- `trust.injection.status`: `clean`, `flagged`, `pending`, `error`.
- `locator.kind`: `repo-relative`, `uri`.
- `hashes.content_scope`: `item.text.utf8.v1` (evidence items) and `message.text.utf8.v1` (inter-seat messages).

### Exact-byte scopes

`hashes.content` is the bare lowercase 64-char hex SHA-256 of the exact UTF-8 bytes of the persisted item `text` field. No trimming, newline normalization, or Unicode normalization. `hashes.raw`, when present, is the SHA-256 of the exact retained source bytes with `raw_scope = exact_bytes`. When `raw` is absent, `raw`, `raw_algorithm`, and `raw_scope` are all null. Both algorithms must be `sha256`. The envelope `hashes.content` is a separate contract from the legacy SQLite `items.content_hash` dedupe column. Verify each against its own scope and never compare them for equality.

### Nullable fields

The following string fields are nullable (JSON `null` ↔ Go `*string` nil): `collection_id`, `item_id`, `captured_at`, `ingested_at`, `trust.assigned_at`, `hashes.content`, `hashes.raw_algorithm`, `hashes.raw_scope`, and `hashes.raw`. `Validate` distinguishes a null pointer from a present empty string. For non-legacy envelopes, `collection_id`, `item_id`, and `hashes.content` must be present (non-null) and non-empty. For legacy envelopes, all nullable pointers may be null. When `hashes.raw` is null, `raw_algorithm` and `raw_scope` must also be null. When `hashes.raw` is present, it must be a valid 64-char hex digest and both `raw_algorithm` (`sha256`) and `raw_scope` (`exact_bytes`) must be present and non-null.

### Trust policy entitlements and caps

`trust.trust_policy` stores only `schema = brigade.trust-policy.v1` and `schema_version = 1`. Consumers load the shared `src/brigade/fixtures/trust-policy.v1.json` fixture to derive entitlements per label: `unknown` (search, show_metadata, forensic_content_reveal), `untrusted` (search, show, brief_wrapped with caps), `reviewed`/`verified` (search, show, brief, cite, promote), `quarantined` (search_metadata, show_metadata). `untrusted_caps` are `max_items = 2` and `max_fraction = 0.5`.

### Size ceiling

The canonical compact JSON encoding (UTF-8, no whitespace, HTML escaping disabled) must be no greater than 4096 bytes. `Validate` rejects larger envelopes with a `size`/`4096` error. Key order is not part of the contract: the Go validator measures the byte count of its compact non-HTML-escaped encoding, which is independent of object key ordering, matching Python's `ensure_ascii=False` compact byte count.

### Absolute-path ban

`locator.value` must be repo-relative or a non-file URI. `Validate` rejects POSIX absolute paths (`/etc/passwd`), Windows drive paths (`C:\\Users\\foo`), UNC paths (`\\host\share\file`), and `file:` URIs.

### Authority rule

An inbound adapter envelope claiming `trust.label = reviewed` or `verified` must pass `ValidationContext{InboundAdapter: true, AuthorityProof: &AuthorityProof{AssignedBy, Label}}` where `AssignedBy` matches `trust.assigned_by` and `Label` matches `trust.label`. Otherwise the ingester downgrades or rejects the assertion. An inbound adapter envelope is data, not authority.

### Legacy banner

When an item carries no provenance, `SynthesizeLegacyProvenance()` returns a non-null envelope with `origin = unknown`, `modality = unknown`, `attribution = inferred`, `trust.label = unknown`, nil `repository`/`session`/`locator`, null `collection_id`/`item_id`/`captured_at`/`ingested_at`/`trust.assigned_at`, and null `hashes.content`/`raw`/`raw_algorithm`/`raw_scope`. The display string is `UNKNOWN PROVENANCE - legacy item` (`provenance.LegacyDisplay`). A missing envelope is never treated as trusted.
