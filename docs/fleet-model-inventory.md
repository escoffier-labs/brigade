# Fleet Model Inventory

The `brigade.fleet_model_inventory` module provides server-owned persistent
model inventory tracking for the Brigade Fleet control plane. It maintains
source provenance, tracks coverage freshness across providers, harnesses, and
accounts, validates CLI and manual evidence, and classifies model identities
without heuristic string guessing.

## Core Concepts

### Access Tier Isolation

Model availability exists across three distinct layers that must not be conflated:

1. **CLI Local Access (`cli_local`)**: Proves an installed tool executable is
   logged in and capable of executing dispatches locally.
2. **Cloud Model Catalogue (`cloud_catalog`)**: Represents models listed in a
   remote vendor API or documentation catalogue. A catalogue listing does not
   guarantee local CLI execution.
3. **Subscription Access (`subscription`)**: Represents accounts or quotas
   entitled to run a specific model tier. A public model list does not prove
   active subscription entitlement.

The inventory store tracks evidence origin explicitly so that public catalogues
or remote rosters cannot masquerade as verified local execution paths.

### Freshness and Expiry

Every observation carries two UTC ISO 8601 timestamps:
- `captured_at`: The exact time the probe or observation completed.
- `expires_at`: The validity ceiling for the observation.

At query time (`snapshot`), freshness is calculated against `now`:
- `now < expires_at`: The coverage remains eligible for evaluation (`fresh`).
- `now >= expires_at`: The exact boundary transitions the coverage to `stale`
  with reason `inventory-expired`.

Clock skew is guarded at ingestion: `expires_at` must be at or after `captured_at`.

### Complete vs Partial Inventory Semantics

Inventories report one of two scopes:
- **`complete`**: The observation enumerates the full set of models available for
  the provider coverage. Models omitted from a healthy, fresh, complete snapshot
  are classified as `missing`.
- **`partial`**: The observation enumerates only a subset (such as a specific
  family or experimental lane). Unlisted models cannot be proven absent, so they
  are classified as `unavailable` with reason
  `uninventoried-in-partial-snapshot`.

### Failure Supersession and Ordering

Projections per `(provider, harness, account_id)` follow strict precedence:
- **New failure supersedes old good**: When a newer observation reports status
  `error`, the projection immediately transitions to `unavailable` with the safe
  error reason. Old successful models are not retained past a newer failure.
- **Older snapshots cannot win**: An observation with `captured_at` earlier than
  the projection's current `captured_at` is stored in raw observations but ignored
  by the projection.
- **Same-instant conflicts**: If two different observations share the exact same
  `captured_at` timestamp, the projection status transitions to `unknown` with
  reason `same-instant-conflict`.
- **Idempotency**: Re-ingesting an identical observation is idempotent and yields
  no projection or ledger mutations.

### Explicit Operator Aliases

Different harnesses and seats may refer to models by canonical names (such as
`gemini-3.8-flash`) while the provider native CLI exposes an effort-suffixed
identifier (such as `gemini-3.8-flash-low`).

The inventory component prohibits heuristic string guessing, regex trimming, or
fuzzy matching. All translations between canonical seat names and native model
identifiers must be registered through explicit operator alias mappings scoped
to a provider (and optionally a harness). A mapping for one provider never
blesses another provider's missing canonical model, and stored aliases never
select a different account or harness native identity.

## Database Schema

`ensure_schema(conn)` registers three additive SQLite tables:

```sql
CREATE TABLE IF NOT EXISTS fleet_model_inventory_observations (
    observation_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    provider TEXT NOT NULL,
    harness TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    models_json TEXT NOT NULL,
    error_reason TEXT,
    evidence_type TEXT NOT NULL DEFAULT 'cli_probe',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_model_inventory_projections (
    provider TEXT NOT NULL,
    harness TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    observation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    models_json TEXT NOT NULL,
    error_reason TEXT,
    evidence_type TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, harness, account_id)
);

CREATE TABLE IF NOT EXISTS fleet_model_inventory_aliases (
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    native_model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, canonical_model)
);
```

### Observation ID Hash

Each observation ID is a SHA-256 digest of canonical JSON containing all
measurement fields:
- `schema`: `"brigade.fleet_model_inventory.v1"`
- `source`: Caller-verified trusted source string
- `provider`: Provider identifier
- `harness`: Harness identifier (or empty string)
- `account_id`: Account identifier (or empty string)
- `scope`: `"complete"` or `"partial"`
- `status`: `"ok"` or `"error"`
- `captured_at`: ISO 8601 string
- `expires_at`: ISO 8601 string
- `models`: Canonical sorted model records
- `error_reason`: Sanitized safe error code
- `evidence_type`: Observation type (`cli_probe` or `manual_browser`)

If an incoming observation generates an existing ID with differing content, an
`InventoryCollisionError` is raised.

## Probe Helpers and Parsers

### Process Isolation Rules

1. **Executable Allowlist**: Only the exact read-only argv tuples in
   `{("agy", "models"), ("opencode", "models"), ("cursor-agent", "models"), ("grok", "models")}`
   may be spawned. Other commands are rejected with exit code 126.
2. **No Shell Execution**: Live probes call `brigade.proc.run(..., supervise_group=True)`,
   which launches without a shell and reaps the owned process group on timeout,
   overflow, or incomplete descendants.
3. **No Token Configuration**: The probe runner does not parse token configuration
   files or export authorization secrets.
4. **Bounded Output and Timeout**: `proc.run` retains at most 1 MiB
   (`proc.MAX_CAPTURE_BYTES`). Inventory then accepts at most 64 KiB of stdout.
   Any `output_limit_exceeded`, `stream_limit_exceeded`, `incomplete_process_group`,
   `decode_failed`, or oversized accepted stdout is treated as unavailable. Timeout
   is bounded between 1.0 and 60.0 seconds (default 15.0 seconds).
5. **No Raw Stderr in Errors**: Diagnostic messages are sanitized into safe symbolic
   codes (e.g., `probe-exited-1`, `probe-timed-out`, `executable-not-found`,
   `output-too-large`, `probe-failed`) rather than recording raw process streams.

### Supported Formats

- **`agy models`**: Parses tab-separated rows of `model_id\tdisplay_name`.
- **`opencode models`**: Parses linewise exact `provider/model` identifiers.
- **`cursor-agent models`**: Parses block output containing `Available models`
  header, `<model_id> - <description>` rows, and `Tip: use --model <id>` footer.
- **`grok models`**: Parses block output containing `Available models:` header
  and `* <model_id>` rows.

If command output deviates from the expected structure, parsers raise
`InventoryValidationError` and probe helpers return status `error` with reason
`unsupported-output-shape`. Unrecognized outputs never yield empty-success.

### Unverified Harnesses

Harnesses lacking a verified live inventory command (`claudecode`, `codex`,
`jules`, `grokbot`) immediately return an error payload with reason
`unsupported-harness-no-verified-live-model-list`. They are never probed via
speculative commands.

### Manual Browser Observations

Expiring operator evidence can be recorded via `build_manual_browser_payload`.
The observation carries `source: "manual:browser"`, `evidence_type: "manual_browser"`,
and explicit operator attribution. It is validated identically to CLI payloads
and must define an expiration timestamp.

## Module Integration API

### `ensure_schema(conn: sqlite3.Connection) -> None`
Creates additive tables and indexes. Safe to execute repeatedly.

### `ingest(conn, payload, *, trusted_source, allowed_sources=None) -> dict[str, Any]`
Validates payload schema, checks `trusted_source` against the allowlist and
payload claim, validates safety bounds (payload size <= 256 KiB, finite numbers,
no credential-bearing URLs), computes observation hash, stores raw observation,
and updates projection.

### `snapshot(conn, *, now=None, provider=None, harness=None, account_id=None) -> dict[str, Any]`
Computes the current projection state evaluated at `now`. Determines exact
`fresh`, `stale`, `unavailable`, and `unknown` states per coverage.

### `classify_exact_identity(source, *, provider, model, now=None, aliases=None, harness=None, account_id=None) -> dict[str, Any]`
Evaluates a single seat model against inventory. Resolves operator aliases,
checks coverage status, and classifies the model as `available`, `retired`,
`policy-blocked`, `missing`, or `unavailable`. Freshness is always reevaluated
at `now` from `captured_at`/`expires_at`. A flat `{canonical: native}` alias map
is accepted only because this call already selected one provider.

### `to_fleet_policy_inventory(conn_or_snapshot, *, now=None, aliases=None, bindings=None) -> dict[str, Any]`
Transforms the inventory snapshot into the dictionary consumed by
`fleet_policy.validate_inventory(document, inventory)`:
```python
{
    "provider-name": {
        "state": "fresh",
        "available": ["model-1", "model-2"],
        "retired": ["model-old"],
        "blocked": ["model-blocked"]
    }
}
```
Accepts an explicit `bindings` map `{"provider": {"harness": ..., "account_id": ...}}`
to resolve multi-coverage scopes without favorable guessing.
Query-time `now` reevaluates `captured_at`/`expires_at` even when a mapping snapshot
still carries `state: "fresh"`. Missing, malformed, naive, or future timestamps do
not admit. Aliases must be provider-scoped (`{"google": {"seat": "native"}}`); a
flat `{canonical: native}` map is not applied across providers. Stored aliases are
keyed `(provider, canonical)` and never select a different account or harness.
If coverage is stale, partial, or failed, `state` is set to `unavailable` with an
explanatory reason, preventing `validate_inventory` from incorrectly treating
uninventoried seats as missing.

JSON trees accepted by ingest are bounded to depth 8 and 8192 nodes. Validation
errors are generic and omit arbitrary map keys and values.

## Authoritative publisher configuration

Hub ingest does not accept a client-claimed source. Policy documents declare
the allowed publishers under `routing.inventory_collectors`, a bounded mapping
from collector key to exactly:

```json
{
  "node_id": "11111111-1111-4111-8111-111111111111",
  "source": "cli:agy",
  "provider": "google",
  "harness": "agy",
  "account_id": "acct-generic"
}
```

Unknown fields are rejected. The map is optional and defaults to `{}` so older
documents still parse. An empty map denies ingest: there is no implicit
publisher.

Node ingest requires the authenticated node and the exact configured
`(node_id, provider, harness, account_id)` tuple. `trusted_source` is taken
from that server record. The client `source` is never trusted and cannot pick
a more favorable scope. `manual:browser` is admin/operator-only and must carry
operator provenance through `build_manual_browser_payload`. A node token cannot
use the admin path as a fallback.

`brigade fleet policy inventory ingest --file PATH --json` and
`brigade fleet policy inventory status --json` talk to the existing `/policy`
action and `GET /policy/inventory`.
`brigade fleet policy inventory probe --harness H --provider P --account-id A`
delegates to `probe_cli_inventory` and writes a local file or stdout. It does
not publish unless `--publish` is set. The Hub never probes a provider while
rendering.
