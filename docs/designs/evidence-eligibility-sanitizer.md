# Evidence Eligibility Sanitizer: a provably-last chokepoint for #1030 / #1031 / #1032

Status: design pass (no code). Supersedes the parked partial sanitizer in PR #1055.
Scope: `engines/evidence-ledger/internal/app/` — `app.go`, `mcp.go`, `integrity.go`
(plus one mandatory delegation edit in `server.go`, see §9).

## 1. Problem

PR #1055 attempted to close three HIGH findings on the evidence facade:

- **#1030** — artifact.text URL-hash swap (artifact body replaced while a
  `kind=url` content hash still verified).
- **#1031** — cached `show` replaying stored bundles without re-running live
  eligibility + integrity checks.
- **#1032** — free-form metadata injection through the search projection
  (`collection.kind` unbounded, etc.).

Five independent probe-executing reviews of that PR each found **one more**
model-facing leak of attacker-writable data for ineligible/quarantined items.
The surface grew instead of converging; the PR was parked.

Root cause: attacker-writable fields are assigned at multiple points, several
**before** the eligibility decision, across four exit paths and the full
DB-sourced projection. Any fix that patches fields one by one loses this race:
the next review finds the next field. The fix must be structural.

Branch naming note: on the current branch the materialization function is
`evidenceBundle` (`app.go:2509`); the parked branch renamed it to
`materializeEvidenceBundle` and added an `item_ids` branch. This document uses
the parked-branch names and flags the rename as part of the plan.

### The four exit paths

| # | Exit path | Current entry points |
|---|---|---|
| E1 | `materializeEvidenceBundle` — `item_ids` branch (new in #1055) | MCP/CLI show regeneration |
| E2 | `materializeEvidenceBundle` — no-`item_ids` (search) branch | `cmdEvidence`, `mcpEvidence` (mcp.go:263), HTTP `handleEvidence` (server.go:294) |
| E3 | `listEvidenceBundles` (`app.go:2673`) | `cmdEvidenceList`, future MCP list |
| E4 | evidence show / MCP `show_evidence_bundle` | `cmdEvidenceShow` → `loadEvidenceBundle` (app.go:2657), `mcpEvidenceShow` (mcp.go:297) |

Every exit also has two renderers: JSON (`writeJSON` / `httpJSON` /
`mcpTextResult`) and Markdown (`writeEvidenceMarkdown`, which prints
`bundle["query"]` verbatim at app.go:2727; the non-JSON list print at
app.go:2503 does the same). Renderers are downstream of the chokepoint, never
a second decision point.

## 2. Goals

1. One sanitizer, applied as the **provably-last transform** to the **complete
   serialized payload** on every exit path. Nothing writes to the response
   after it runs.
2. Future fields are sanitized **by construction**: each exit serializes into a
   neutral structure the walker allowlists at every object level; an unlisted
   key is dropped, not passed through.
3. Ineligible/quarantined items leak nothing attacker-writable — fields are
   **dropped**, never truncated or byte-trimmed.
4. Identity fields are assigned only **after** the eligibility decision.
5. #1063-style full-payload regression test with under-bound needles that
   catches both leakage *and* the truncate-instead-of-drop failure mode.

Non-goals: changing eligibility semantics (`contentEligible`,
integrity.go:228), changing storage formats, reworking the cache layout, or
adding runtime dependencies.

## 3. Data classification

Attacker-writable = anything derived from imported archive content
(items, artifacts, relations, actors, collections, raw/metadata JSONL) or any
free-form request parameter echoed back into a model-facing payload.

- **Attacker-writable, value-bearing:** `external_id`, `snippet`, `text`,
  `summary`, `metadata.*` incl. the reflected provenance envelope attached by
  `attachIntegrityFields` (integrity.go:280 attaches the *raw* stored map),
  `collection.{name,kind}`, `actor.{name,type}`, `artifacts[].{path,url,
  mime_type,text,content_hash}`, `raw_ref.{hash,path}`, `related[].{
  relation_type,target_external_id,target_*}`, `created_at`/`timestamp`,
  bundle `query`, `filters.{source,project,from,to,code_reference.*}`,
  listing `query`, cached `id`/`resource_uri` values read back from disk.
- **Server-generated identity:** item `id` (SQLite rowid-derived), bundle `id`
  (`evidenceBundleID`, 24 lowercase hex of SHA-256, app.go:2604-2619),
  `resource_uri` (`"miseledger://evidence/" + id`), `generated_at`.
  These are safe to emit *as long as they are constructed server-side from
  validated inputs after the gate* — never echoed from cache or filenames
  without validation.
- **Enum-shaped:** `trust_label`, eligibility status/reason, `source_kind`,
  `collection.kind`. Safe only against a closed allowlist.

## 4. Design: the chokepoint

### 4.1 Shape

Each exit builds its response as today, then hands it to exactly one function
as its final act:

```go
// integrity.go (new)
type evidenceEligibility struct {
    Status string // "eligible" | "ineligible"
    Reason string // closed reason-code enum below
}

type evidenceOutbound struct {
    Tree      any                            // response-shaped neutral structure
    Decisions map[string]evidenceEligibility // keyed by server-assigned item id
}

func finalizeEvidenceResponse(out evidenceOutbound) ([]byte, error)
```

Pipeline inside `finalizeEvidenceResponse`, in order:

1. `json.Marshal(out.Tree)` — captures the **complete** payload including any
   field added after this design was written.
2. `json.Unmarshal` into `any` — the neutral structure
   (`map[string]any` / `[]any` / `string` / `float64` / `bool` / `nil`).
3. Recursive walk enforcing allowlists at every object level (§4.2), using
   `out.Decisions[id]` to collapse ineligible items (§4.3).
4. `json.Marshal` of the sanitized tree; these bytes are the only thing exits
   may write.

**Provably-last enforcement:** all four exits end in
`finalizeEvidenceResponse` as their final statement; CLI/HTTP/MCP renderers
become byte sinks:

- CLI: `writeEvidenceJSON(w, b)` / `renderEvidenceMarkdown(w, b)` consume the
  returned bytes; `writeJSON` is no longer called by any evidence exit.
- MCP: new `mcpTextResultBytes(b)` wraps pre-sanitized bytes;
  `mcpTextResult(v any)` is no longer called by evidence tools (it marshals
  internally and would bypass the walk).
- HTTP: `handleEvidence` delegates to the same finalize output before
  `httpJSON`.

Two guards make "nothing after" auditable rather than aspirational:

- A unit test walks every evidence exit function and asserts its returned
  bytes equal `finalizeEvidenceResponse` applied to its tree (byte equality).
- An AST-based test parses `internal/app` and fails if `writeJSON`,
  `httpJSON`, `mcpTextResult`, or `fmt.Fprintf(out, …)` appear inside the
  four exit functions (or their Markdown renderers) outside the sanctioned
  byte-sink helpers.

### 4.2 Allowlists (drop-on-unlisted semantics)

The walker enforces **allowlists at every object level**. Unknown keys are
removed. This is the by-construction property: a future field addition to any
projection is sanitized unless someone deliberately adds it here too.

Bundle root keys:

```
id, resource_uri, generated_at, regenerated_at?, results, grouped_by_source,
integrity_omitted, integrity_mismatches, warnings, untrusted_context,
result_count?            # E3 listings only
```

Dropped at root, permanently: `query`, `filters` (all of it — see surfaces 3–4),
any cached `cache_ref` echo. Callers who need filter provenance get
`regenerated_at` + `result_count`; the request itself is the caller's own
input and needs no reflection.

Eligible-item keys (E1/E2/E4 results):

```
id, external_id, snippet, timestamp, source_kind, kind, score,
collection{external_id, kind, name}, actor{external_id, type, name},
raw_ref{path, hash, ordinal}, artifacts[], related[]?,
provenance, integrity_mismatch, origin, modality, trust_label
```

Typed-field validators run inside the walk (validate-or-drop, never trim):

- `timestamp`/`created_at`: must parse RFC3339(Nano); else dropped.
- `raw_ref.hash`, `content_hash`: must match `^[0-9a-f]{64}$` (optionally
  `sha256:` prefixed); else dropped.
- `source_kind`, `collection.kind`, `relation_type`, `trust_label`: must be in
  the closed enum sets (§6 for kinds); else dropped or replaced with the empty
  value the schema already documents.
- `score`: numeric-or-drop.

Artifact keys: `id, kind, path, url, mime_type, text?, content_hash` — present
only under an eligible parent item; `text` only when the request opted into
`include_artifact_text` (decision recorded at materialization time in the
walker config, not inferred from payload presence).

Related keys: `relation_type, target_external_id, target_item_id,
target_kind, target_created_at` — same typed validators; `target_created_at`
RFC3339-or-drop.

E3 listing keys: `id, resource_uri, generated_at, result_count` — and nothing
else. `query` is gone from listings (also fixes the non-JSON print at
app.go:2503).

Ineligible stub (§4.3): exactly three keys.

### 4.3 Ineligible/quarantined items: drop, and prove identity comes last

For any item whose freshly computed `contentEligible(view)` (integrity.go:228)
is false — parse error, integrity mismatch, legacy-unknown, trust label
`unknown`/`quarantined`, injection status ≠ clean — the walker replaces the
item map with exactly:

```json
{"id": "<24-hex>", "eligibility_status": "ineligible", "reason_code": "<enum>"}
```

Reason-code enum (closed set, derived from `integrityView`):
`parse_error`, `integrity_mismatch`, `legacy_unknown`,
`trust_unknown`, `trust_quarantined`, `injection_not_clean`.
No prose, no expected/actual hash values (those leak plaintext through the
back door), no envelope fragments.

**Drop-not-trim rule:** no walker path ever truncates a value. Historic suites
passed because planted needles exceeded a 64-byte trim and were cut below
detectability; trimming preserves attacker-controlled prefixes and is banned.
Validators either accept the whole value or remove the whole field.

**Identity-after-decision proof.** Materialization is restructured so the item
map literally cannot carry an id before the gate:

```
provisional := buildProvisionalItem(...)   // NO "id" key assigned anywhere
view       := inspectItemIntegrity(...)
if !contentEligible(view) {
    return stub{id: <server id>, reason: reasonCode(view)}   // id minted now, post-decision
}
id := <server-assigned item id>             // first assignment point
item := provisional; item["id"] = id
Decisions[id] = eligible
```

`bundle id` (`evidenceBundleID`) and `resource_uri` are computed over the
post-decision id set, so even aggregate identity reflects gated data. Two
tests lock the invariant: (a) key-set equality — every ineligible stub decodes
to exactly the three keys above; (b) ordering — a fake clock/instrumented
builder asserts `buildProvisionalItem` output contains no `id` key.

### 4.4 Per-exit rewiring

- **E2 search branch:** `evidenceBundle` keeps gathering, but per-item
  mitigation moves out: delete the inline snippet-clear /
  `delete(art, "text")` block (app.go:2564-2571). It becomes the walker's job.
  Final statement: `finalizeEvidenceResponse`.
- **E1 item_ids branch (#1055 rename retained):** same builder, ids supplied
  instead of searched. Converges on the identical provisional→gate→assign
  sequence; shares the walker.
- **E4 show (#1031):** `loadEvidenceBundle` is demoted to an *id-set loader*.
  Show re-opens the DB, re-runs `inspectItemIntegrity` live for every item id
  in the cached bundle via E1, and emits a fresh bundle marked
  `regenerated_at`. Cached `results` bytes are never replayed. Items missing
  from the DB get stubs with reason `source_missing` (added to the enum).
  Bundle-level reflection is limited to the validated `id`/`resource_uri`
  (surface 1).
- **E3 list:** validate filename against `^[0-9a-f]{24}\.json$`; derive `id`
  from the validated match; load only to count results; assign `id` and
  construct `resource_uri` **after** validation (reorder of today's
  app.go:2687-2699). Entries failing validation are skipped silently — they
  are not echoed in any form.

## 5. Re-landing #1055's confirmed-closed sub-fixes inside this pass

All three land as part of the sanitizer pass, not as cherry-picks of the
parked branch (its partial sanitizer is deleted, not reused):

- **#1030 URL-hash swap** — keep/restore in `verifyMaterializedHashes`
  (integrity.go): for `kind=url` artifacts the stored `content_hash` must match
  *either* the normalized body digest *or* the digest of the URL string
  (logic already present at integrity.go:176-185 on this branch; the parked
  diff must be reconciled onto it, and the walker's hash-format validator
  guarantees what reaches the model is a well-formed digest, not attacker
  text). Covered again by the §8 needle suite (needle in artifact.text with a
  swapped-in valid url hash must surface as `integrity_mismatch` → stub).
- **#1032 kind allowlists** — implemented as walker enum rules
  (§4.2): `collection.kind` (and `source_kind`, `relation_type`) must be in
  the closed sets derived from ingest; anything else drops. The projection
  stops being the place where unbounded kinds get policed ad hoc.
- **#1031 live regeneration on cached show** — §4.4 E4. Deleted from the
  parked approach: replaying cached payloads with a partial sanitizer bolted
  on.

Deleted from the parked partial-sanitizer approach: per-field `delete(...)`/
blank-out calls scattered across branches, 64-byte trims of `created_at`/
`timestamp`, pre-gate assignment of `id`/`resource_uri` from `entry.Name()`
(parked integrity.go:606-607 / app.go:2942-2954 equivalents), and the
per-review whack-a-mole pattern itself.

## 6. Leak-surface → closing element

| # | Enumerated surface | Exit paths | Closing design element |
|---|---|---|---|
| 1 | cache-ref `query` and `resource_uri` echoed verbatim in evidence show / MCP `show_evidence_bundle` | E4 | §4.4 E4: cached payload demoted to id-set loader; `resource_uri` reconstructed from validated 24-hex id; `query` absent from root allowlist (§4.2); live regeneration stamps `regenerated_at` |
| 2 | bundle `collection.kind` unbounded | E1, E2, E4 (via results) | §4.2/#1032 walker enum rule: `collection.kind` ∉ closed kind set ⇒ dropped; enforced on every exit because every result passes the same walk |
| 3 | `filters.code_reference.qualified_name` and `.file_path` reflected | E2 (root `filters`) | Root allowlist drops `filters` wholesale (§4.2); matching still consumes parsed `SearchOpts.CodeReference` — input handling unchanged, reflection removed |
| 4 | no-`item_ids` branch `filters.project/from/to`; `listEvidenceBundles` `query` | E2, E3 | Same root-allowlist drop (E2); E3 listing allowlist contains only `{id, resource_uri, generated_at, result_count}` (§4.4 E3) |
| 5 | `listEvidenceBundles` payload `id`/`resource_uri` sourced from `entry.Name()` before the gate (parked integrity.go:606-607, app.go:2942-2954; current app.go:2687-2699) | E3 | §4.4 E3 reorder: regex-validate filename → derive id → assign identity fields strictly after validation; non-conforming entries skipped |
| 6 | `results[].artifacts[].{path,url,mime_type}`, `results[].raw_ref.hash`, `results[].related[].{relation_type,target_external_id}` verbatim on all four exits; `created_at`/`timestamp` 64-byte-trimmed instead of dropped | E1–E4 | §4.3 ineligible ⇒ three-key stub (fields dropped, not trimmed); eligible items keep evidence content but typed fields pass validate-or-drop (§4.2); trims banned by construction and caught by the §8 window test |

## 7. Why this converges

The five reviews diverged because each found a different field. Under this
design every finding reduces to one question: *is the field in a walker
allowlist?* If yes, it was consciously admitted; if no, the walk removed it
on all exits simultaneously, because there is exactly one walk and it runs
last. New fields default to dropped (fail-closed), so the surface shrinks by
default instead of growing.

## 8. Test design: full-payload needle walk (shape of #1063)

One table-driven Go test in `internal/app`:
`TestEvidenceExitsNoUnderboundNeedleAnyPath`.

Fixture: archive seeded with (a) many ineligible/quarantined items covering
every reason code, (b) one fully eligible control item (valid provenance,
clean injection status, hashes matching bodies), (c) a tampered on-disk cache
bundle whose `id`, `resource_uri`, and `query` carry needles (drives E4/E3).

**Needles:** unique ~200-byte markers (180 filler chars + `NEEDLE0001`-style
suffix) planted in *every* writable field: `external_id`, `snippet`, `text`,
`summary`, metadata free fields, reflected provenance strings,
`collection.name/.kind`, `actor.name/.type`, `artifacts[].path/url/mime_type/
text/content_hash`, `raw_ref.hash/path`, `related[].relation_type/
target_external_id/target_kind/target_created_at`, `timestamp`/`created_at`,
bundle `query`, `filters.project/from/to/source`,
`code_reference.qualified_name/file_path`, tampered cache `id`/`resource_uri`.
~200 bytes is deliberately **under** no bound but **over** the historic 64-byte
trim — a trimming implementation leaves a visible prefix fragment.

**Drivers:** all four exits exercised through their public entries — E1
(item_ids materialize), E2 (search materialize via CLI, MCP
`create_evidence_bundle`, HTTP POST `/evidence`), E3 (`evidence list`),
E4 (`evidence show` CLI + MCP `show_evidence_bundle`). Each returns bytes.

**Assertions, per exit:**

1. Decode bytes into `any`; recursively visit every leaf, counting them.
2. Visited-leaf count ≥ fixture baseline (~700+, mirroring the #1063
   707-field walk) — prevents vacuous passes over empty payloads.
3. No string leaf contains any full needle.
4. **Truncation tripwire:** no string leaf shares any ≥32-byte sliding window
   with any needle. A 64-byte trim of a 180-byte needle leaves 32-byte windows
   intact, so window overlap fails the test where full-string containment
   would (exactly the failure mode that let earlier suites pass).
5. Key-set check: every ineligible stub has exactly `{id,
   eligibility_status, reason_code}`; no `filters`/`query` key exists at root;
   listing objects have exactly the four allowed keys.
6. **Positive control:** the eligible item's planted needle IS present at its
   expected paths (`snippet`, `text`) on E1/E2/E4 — proving the walk traverses
   real data and the assertions aren't green because the payload is empty.

Existing narrower tests stay; they assert behavior, not payload completeness,
and now sit underneath this suite.

## 9. Implementation plan (ordered)

All in `engines/evidence-ledger/internal/app/`.

1. **integrity.go** — add `evidenceEligibility`, closed reason-code enum,
   `reasonCode(view)`; add walker tables (§4.2), `walkEvidenceTree`,
   `finalizeEvidenceResponse`; reconcile #1030 dual-hash logic in
   `verifyMaterializedHashes` (already at integrity.go:176-185 here — keep as
   the single source, port any parked test coverage).
2. **app.go** — split `evidenceBundle` into `materializeEvidenceBundle(db,
   opts, itemIDs []string)` (E1+E2 share one body; nil `itemIDs` ⇒ search
   branch) plus `buildProvisionalItem` (no id assignment); move the gate
   before identity (§4.3); delete the inline mitigation at app.go:2564-2571;
   compute `evidenceBundleID`/`resource_uri` post-decision.
3. **app.go** — rewrite `listEvidenceBundles` (regex-gate, post-gate identity,
   four-key summaries, no `query`); fix the non-JSON print at app.go:2503.
4. **app.go** — replace `cmdEvidenceShow`'s `loadEvidenceBundle`-and-echo with
   live regeneration via E1 (§4.4 E4, #1031); convert `cmdEvidence`,
   `cmdEvidenceShow`, `cmdEvidenceList` to byte-sink rendering; rewrite
   `writeEvidenceMarkdown` to consume finalized bytes (kills the app.go:2727
   `query` echo).
5. **mcp.go** — add `mcpTextResultBytes`; route `mcpEvidence` (E2) and
   `mcpEvidenceShow` (E4) through finalize; `mcpEvidenceShow` regenerates live
   like the CLI.
6. **server.go** (mandatory adjacent edit — `/evidence` is exit E2's third
   transport; skipping it would leave an unsanitized exit): `handleEvidence`
   renders the finalize output bytes.
7. **Tests** — §8 needle-walk suite; stub key-set and identity-ordering tests
   (§4.3); AST guard test (§4.1); port/rebase the parked branch's #1030/
   #1031/#1032 tests onto the new seams.
8. Delete any surviving parked-branch partial-sanitizer code if the rebase
   starts from #1055 (per §5 list).

Verification per engine AGENTS.md: `go vet ./...`, `go test ./...`, plus
`scripts/smoke_mcp.sh` and `scripts/smoke_http.sh` (both touch evidence
surfaces), all against temp-HOME isolation only.
