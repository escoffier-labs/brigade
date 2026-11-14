# Memory Care

Brigade memory care scans durable memory cards and tends them. The scan is local and never edits cards. It detects cards that need review, writes findings and a refresh queue, and routes review tasks into the existing work inbox. Tending then retries pending hub operations, applies an allowlist of derived fixes, archives eligible cards, drains the review inbox and reports instruction budgets. Every automatic card change is recorded with undo. Tending runs when you call `memory care tend`, and a successful CLI ingest runs its inbox stage. It is not on a schedule unless you add one. See [Safe Fix Planning](#safe-fix-planning), [Boundary](#boundary) and [Tending](#tending).

## Commands

```bash
brigade memory care init
brigade memory care scan
brigade memory care plan-fixes
brigade memory care backfill
brigade memory care status
brigade memory care doctor
brigade memory care import-issues
brigade memory care closeout
```

`init` writes `.brigade/memory-care.toml`. The config is host-local and should stay gitignored.

## Deterministic reflection candidates

`brigade memory reflect run --dry-run` derives `stale-newer-evidence`, `duplicate`,
`reinforce`, and `opposite-polarity` candidates without editing cards or calling a
model. It reads the pending-evidence log and canonical cards selected by
`.brigade/memory-care.toml`. `--scope` matches the exact card frontmatter scope,
with missing scope treated as `repo`. `--limit` bounds the retained candidates
from 1 to 100, with a default of 20. Receipts include `total_candidates` and
`truncated`. Priority is opposite-polarity flags, stale evidence, reinforcement,
then duplicates, with stable ordering within each class.

Each candidate cites evidence resolved to an existing file in a receipt or
handoff root, an evidence root, or the local evidence bundle cache. A log row
alone does not resolve its id. Missing pointers, hash-only ids, unrelated
repository files, and external URLs do not count. `proof_count` counts
distinct resolved supporting ids. Receipt prefixes and fragments are removed before
deduplication and storage, and bundle aliases use `miseledger:<bundle>`.
`evidence_sources` records the matching root for
each id, or `evidence-bundle` for local cached evidence bundles. `proof_sources` counts distinct
roots among the supporting ids. These source labels do not establish trust or
independence. Handoff roots include all writer inboxes from the harness registry,
`memory/handoff-inbox`, and local inboxes in `.brigade/handoff-sources.json`.
Only `reinforced` and `outcome` log rows supply new support.
Pending near matches appear in `review_evidence_ids` and never increase proof.
Pending near matches and contrary ids still block when their files are missing.
These pointers appear in `unresolved_evidence_ids` and do not supply proof or
satisfy the requirement for resolved evidence. Candidates with either kind of
pending flag are Tier 2 and have no metadata diff.
Exact reinforcement is already applied by ingest, so its log rows alone do not
produce reinforcement previews. A preview triggered by new outcome evidence
also includes resolved reinforcement ids so its evidence list supports its proof count.
Reflection considers the newest 64 distinct log evidence ids per card by timestamp,
with evidence ids breaking ties. Older ids remain in the log.
`brigade outcome capture <card-stem> --kind card --run-id <id>` appends an
`outcome` support row after durable outcome capture when a tracked verifier
manifest admits the receipt, its subject matches the current contained card's
kind, id, and content fingerprint, and verification and effectiveness both
succeeded. The row contains a target-relative receipt pointer and the original
receipt completion timestamp (or start timestamp when completion is absent).
Capture and reflection leave canonical card bytes unchanged.
Reused receipts, audit-only checks, manual and worker-run signals, skill captures,
failed verification, and checks without successful effectiveness add no support.
Retrying capture can append the same receipt id and timestamp. Reflection counts
that id once, and retry does not refresh the observation. If the optional log
append fails, capture warns after preserving the durable outcome. Retry capture
to supply the missing evidence. Ordinary non-supporting receipts are excluded
silently. Capture checks the safely read receipt against the admitted source,
then rechecks the card snapshot and its linked-card fingerprint after admission.

Reflection revalidates logged `outcome` pointers of the form
`.brigade/work/verify-runs/<id>/receipt.json` against the retained receipt and
the candidate's card snapshot, including linked cards. Changed card bindings,
missing or invalid receipts, and receipt identity or timestamp drift contribute
no proof or consolidation preview. Their historical log rows remain untouched.
An invalid logged pointer cannot regain proof through the card's evidence
metadata. Saved candidates and proposals undergo the same check when proposed
or applied. Existing citations retained by a new proposal are bound by their
current file digests, so an approved consolidation does not make future updates
impossible when its metadata changes the old receipt's card fingerprint. Every
receipt explicitly used as new operation support still requires the live binding,
even if already cited. Previews preserve historical citations, but obsolete verify
bindings do not count as current proof. Previously cited evidence does not block
consolidation as uncited pending evidence. Validation deduplicates repeated ids
and timestamps and stays within the newest 64 distinct log ids plus up to 64
current citations per card.
Generic evidence under other roots, and manually cited files without a matching
verify-outcome log row, retain their existing resolution and reviewed-proposal
semantics. A file's presence alone does not validate logged verify-outcome support.
Staleness uses `last_consolidated`, falling back to `last_reviewed` and
its supported aliases. Date-only cutoffs require evidence on a later UTC date.
Near-match and opposite-polarity flags use the same cutoff, including the care
scanner's automatic reinforcement exception. Rows already cited by the card do
not remain pending flags. Newer support for an id does not revive its older flag.
Exact normalized fingerprints identify duplicates. Opposite-polarity evidence
that remains pending produces a Tier 2 review flag and never decides which claim is correct.
Cross-scope and mixed-trust duplicate groups also receive Tier 2. Trust declarations
include `trust_label` and nested provenance metadata. Block-style metadata lists,
unparsed YAML flow lists, and citations through `sources`, `source`, `refs`, or
`links` also force Tier 2 with no diff because the scalar evidence preview cannot
preserve them.

Runs retain `candidates.jsonl` and `receipt.json` under
`.brigade/memory/reflect/runs/<run-id>/`, including dry runs. Dry runs print
reproducible metadata diffs for evidence, proof count, and consolidation time,
and `would merge` summaries for duplicates. Consolidation time in a preview is
the newest cited evidence timestamp. A stale candidate with contrary evidence
prints a review summary instead of a consolidation diff. No preview is applied.
`memory reflect status` shows the latest receipt, `memory reflect candidates`
lists the latest successful run, and `memory reflect show <candidate-id>` shows
one retained candidate and its preview. After each run publishes its receipt,
retention keeps at most 50 run directories from the past 30 days. The latest
successful run is always kept, reserving one of those 50 slots even when it is
older than 30 days. Unfinished directories older than 30 days are also removed.
Recent unfinished runs and directories changed during cleanup are left alone.
Links and unexpected files are never deleted. A cleanup failure returns `retention_warning`
and leaves the published receipt available. Inspection reads the newest 1024 run
directories. Invalid receipts are skipped and counted
in the status payload's `invalid_runs`. Candidate inspection also skips runs with
unreadable or corrupt candidate files and reports skipped runs in the listing's
`invalid_runs`. Missing receipts indicate unfinished runs
and are not counted as invalid. All accept `--target` and `--json`.
Text previews escape terminal control characters except newlines, carriage returns,
and tabs. Stored candidates retain the original diff text.

A stage failure records a failed receipt and leaves cards untouched. Incomplete
runs never become visible candidates. Unsafe storage is refused when a receipt
cannot be written safely, including when reflect storage overlaps a card root.
Malformed or missing frontmatter, invalid UTF-8, oversized cards, permission-denied
cards, files with multiple hard links, cards with more than 64 evidence references,
and ambiguous card identities are skipped and listed by path with a reason in the
receipt's `skipped_cards`. Cards changed during an exact read still fail the run.
Malformed or excessively nested trust JSON is treated as unknown trust.
Retry after the writer finishes.
Identity collisions include ids and aliases across all valid cards before scope
filtering. Corpus-wide file and byte limits still fail the run.
The evidence log's 4 MiB scan window is informational and recorded as
`scan_window_truncated`. Malformed rows inside that window, unreadable logs,
and logs changed during a scan still fail the run. No log rotation or compaction
command is provided in this slice. To recover from a malformed row, pause writers
and back up `.brigade/memory/reflect/evidence-log.jsonl`. Manually quarantine the
invalid line outside the log, or truncate an incomplete final line. Preserve
valid rows, then rerun reflection before resuming writers. Rows with an unknown
`kind` also require recovery or a Brigade version that supports that kind.
Policy promotion and model-authored claim changes are not available through
reflection. Reviewed proposal creation is available as described below.

## Reviewed consolidation proposals

`brigade memory reflect propose <candidate-id> --reason "Inspect cited support"`
turns a retained reflection candidate into a `brigade.memory-proposal.v2`
proposal. It checks every candidate base hash against its current card. The
proposal records the reflect run id and candidate id. Creation writes a private
proposal and review notification, leaving canonical cards untouched.

The translator proposes evidence additions for supported stale or reinforcement
candidates and exact duplicate merges. It refuses contrary evidence, unresolved
near matches, and candidates that would require invented claim wording. Sources
must share a configured card root and matching declared scope. Existing trust
admission checks still apply. No proposal is automatically reviewed or applied.
Candidate base hashes are rechecked under the proposal lock. Stale-evidence
consolidations update `last_consolidated` from the newest cited pending evidence
timestamp and derive `proof_count` from distinct resolved evidence ids.
Machine reinforcement timestamps do not advance this consolidation timestamp.

Use the existing decision path for both v1 and v2:

```bash
brigade memory proposal show <proposal-id> --json
brigade memory proposal review <proposal-id> --digest <digest> --reason "Inspected every delta"
brigade memory proposal reject <proposal-id> --digest <digest> --reason "Keep the current claims"
brigade memory proposal apply <proposal-id> --digest <digest>
```

Review and rejection are alternative decisions. Rejection is terminal for that
digest. Apply requires an exact accepted digest and rechecks source bytes,
file modes, configuration, indexes, and cited evidence. A failed kernel write
restores every changed card and index. A committed retry checks the retained
receipt and after-state before returning `already-applied`.

To introduce claim anchors, create a reviewed proposal for the selected card:

```bash
brigade memory proposal create --relation consolidate \
  --source memory/cards/cache.md --anchor-blocks \
  --reason "Add stable claim anchors for reviewed deltas"
```

Anchors use Obsidian syntax, for example `Keep the cache warm. ^c-123abc`.
Paragraphs and individual bullets receive anchors. Headings, code fences,
quoted blocks, tables, and `previously` history lines do not. An anchor belongs
to its card and survives claim replacement. Removed ids remain reserved in
`claim_anchor_ids` and are never minted again for that card. Each card permits
at most 4096 reserved ids. Exhaustion refuses the proposal. Vault projection
preserves anchors in the generated note body. Projection remains one-way and
cannot authorize canonical edits.
Evidence-only changes preserve existing trust and provenance. Anchor-only
changes also preserve them and update envelope content hashes for the new bytes.
Claim changes and duplicate merges derive reviewed provenance.

V2 accepts one to eight distinct source cards through repeated `--source`
options. Use `--ops-file <path>` for a bounded JSON list of typed operations.
Every operation includes `type`, `card_path`, `block_id`, and
`expected_block_hash`. The hash is SHA-256 of the exact UTF-8 block, including
its ` ^c-<6hex>` suffix and internal line endings, without a final line ending.
Operations are evaluated in order against the proposed state.

| Type | Additional fields | Result |
| --- | --- | --- |
| `append_evidence` | `evidence_ids` list, optional `last_consolidated` | Add resolved, scan-clean evidence pointers and derive `proof_count`. A supplied timestamp must match the newest cited non-reinforcement evidence-log timestamp for the card. |
| `replace_block` | `text` | Replace one claim, retain its anchor, and append a `previously X, now Y` history line. |
| `remove_block` | None | Remove one claim, reserve its id, and append its previous wording to history. |
| `add_previously` | `text` | Append a supplied previous wording to history. |
| `supersede_card` | `other_card` | Mark the addressed card superseded by another retained source card. |
| `merge_exact_duplicates` | `other_card` | Merge exact claims into the addressed card and remove the other source. |

Replacement and history text must be a single unanchored paragraph or bullet.
Exact duplicate comparison ignores block anchors and normalizes whitespace.
Case and punctuation must match. Duplicate merges retain
aliases, evidence, and anchor reservations. Supported Markdown index references
are rewritten to the survivor. Unsupported or ambiguous removed-card references
are refused.

Unknown fields, unknown types, missing anchors, hash mismatches, unsafe text,
and unresolved op evidence cause that op to be dropped. The proposal reports
only the dropped index and a refusal reason. If all submitted ops are dropped,
no proposal is saved, even if anchoring was requested. Other valid ops can still
be proposed. The `diff` field is a deterministic unified diff of the exact
before and after bytes. The proposal digest binds that preview and its inputs.

## Config

The local config supports:

- `card_roots`: directories containing memory card Markdown files.
- `index_paths`: memory indexes such as `MEMORY.md`.
- `stale_after_days`: review age threshold.
- `expiry_warning_days`: days before `fresh_until` or `expires_at` counts as expired.
- `minimum_confidence`: `unknown`, `low`, `medium`, or `high`.
- `require_evidence`: whether cards without evidence metadata are flagged.
- `include_paths` and `exclude_paths`: relative path prefixes.
- `output_path`: where `scan-latest.json` and `refresh-queue.json` are written.
- `enabled_checks`: enabled issue types.
- `max_card_bytes`: oversized-card threshold.
- `read_only_paths`: glob patterns (relative, no rooted or drive-qualified paths, no `..`) for cards that automation must never edit, such as policy or operator-owned cards. `*` and `?` stay inside one path segment, `**` spans segments, and a pattern naming a directory covers its descendants. Leading `./` and trailing `/` are ignored, so `memory/cards/policy/` and `memory/cards/policy` mean the same thing. `scan`, `plan-fixes`, and `backfill` never raise issues against or write to matching cards, and the cards stay visible to search and recall. Their IDs and aliases still feed duplicate-ID and alias-collision checks, so a normal card that reuses a protected card's identity is flagged on the normal card, `backfill` refuses to apply while such a collision exists, and `backfill` never mints an ID a protected card already holds. The scan reports `read_only_card_count` alongside `card_count`, which still counts every card. `brigade memory proposal create` and `apply` refuse any proposal that touches a matching card, with an error naming the pattern. `create` checks every path the proposal would change, index files such as `MEMORY.md` included. Re-running `apply` on a proposal that already committed returns its receipt even if a matching pattern was added afterwards. `brigade memory care status` and `doctor` list the active patterns. The default is empty. `brigade memory compact` (its flatten and tighten steps) works from the memory directory, not the care config, and does not consult `read_only_paths`.

## Issue Types

Memory care can emit:

- `stale`
- `unconsolidated-evidence`
- `expired`
- `undersourced`
- `contradictory`
- `missing-index-link`
- `orphaned-card`
- `oversized-card`
- `missing-frontmatter`
- `missing-reviewed`
- `missing-freshness`

`unconsolidated-evidence` flags cards with matched evidence newer than `last_consolidated`, falling back to `last_reviewed` when consolidation metadata is absent or invalid. Cards with neither a valid consolidation date nor a valid review date are unassessable. Date-only references compare each log timestamp's UTC calendar date with the stored date. Ingest sets reinforced `last_reviewed` dates using the local calendar, so evidence from the same local day can fall on a later UTC date. Timestamp references compare exact instants. Each finding lists evidence ids in `evidence_references`, uses the existing refresh queue fingerprint and dedupe rules, and is counted by `memory care status`.

Reinforcement records do not appear in `evidence_references`. When a card lacks a valid `last_consolidated` and has a reinforcement dated on or after its review reference, care treats that reference as a possible automatic bump. Date-only review references use the reinforcement's local calendar date for this check. Care retains all non-reinforcement evidence for review because the earlier review date is unknown. Set `last_consolidated` after consolidation to establish an explicit cutoff.

Existing `.brigade/memory-care.toml` files with an explicit `enabled_checks` list must add `"unconsolidated-evidence"` to enable this check. Upgrade every Brigade installation that reads the config before adding it, because older versions reject unknown check names. A missing `enabled_checks` field uses the installed version's defaults. Status prints `unconsolidated-evidence: disabled` when the check is absent from the enabled list, and JSON status includes `enabled_checks`.

Ingest appends body-free near-match, opposite-polarity, and reinforcement records to `.brigade/memory/reflect/evidence-log.jsonl`. Each row contains a timestamp, card identity and path alias, evidence id, match kind, and similarity. Evidence ids for files inside the target are relative paths to the review inbox copy or processed archive. Archives outside the target use `sha256:` ids derived from their absolute paths, without storing those paths. These hashes identify records but cannot be opened as file pointers. Cards without stable ids retain their legacy identity and path. Reading or appending the log never edits cards. Existing exact-match reinforcement still updates card metadata. Rows are limited to 4 KiB. Care reads only the newest 4 MiB of the log, skipping any partial row at the cutoff. Older evidence can fall outside this window. Exceeding the scan limit adds one warning to `evidence_log.warning_count`. Malformed or oversized rows inside the window also add counted warnings. Unsafe or non-regular paths are refused. Log IO follows the proposal storage rules and fails closed when safe directory descriptors are unavailable, including Windows. Care reports this capability limit as `evidence_log.unsupported: true`, without counting it as an invalid line.

Contradiction detection is deliberately conservative. Brigade only flags explicit duplicate card identities or metadata hints, not LLM-inferred factual conflicts.

`status` and JSON output summarize freshness metadata coverage: reviewed dates present, missing, and stale, freshness dates present, missing, and expired, confidence distribution, and evidence metadata present or missing. These checks only explain review needs. `status` and `scan` do not edit memory cards. Automatic edits come only from tending, described below.

`status` reports `archive_candidates` schema 2 from the latest tend result, without walking the card tree. It reports use-and-subject retirement candidates, `approval_required=false` and whether tending is enabled. Status itself never writes or archives cards.

## Card identity

New canonical cards mint one opaque `card-<uuid4>` ID in frontmatter. A valid ID is never replaced on rewrite or reinforcement. Legacy cards without an ID stay readable through a workspace-relative path fallback (`path:memory/cards/<stem>.md`, plus filename stem and `topic` aliases). Care queues, search and recall logs, refresh imports, and retrieval-eval fixtures prefer the explicit ID and keep those legacy keys as aliases. Alias collisions fail the consumer without rewriting card files.

`brigade memory care backfill` (dry-run by default) writes a deterministic mapping receipt under `.brigade/memory-care/backfills/dry-run-mapping.json` when there is at least one candidate. The receipt reports explicit-ID coverage, path fallbacks, malformed IDs, duplicate IDs (including workspaces without `memory/NAMESPACE`), and alias collisions. `old_id` is the consumer-facing identity (explicit ID, topic, stem, or path) so care queues, search logs, and refresh imports can be remapped. It contains relative paths and IDs only: no card bodies and no absolute paths. A zero-candidate dry run writes nothing. Missing IDs stay a doctor warning until audited coverage is 100 percent; backfill mints an ID on complete care cards that only lack one so that end state is reachable. `--apply` is the named reviewed rewrite that may mint IDs on existing cards and writes the same IDs the dry-run receipt previewed; it refuses to write when aliases collide.

## Search ranking

`brigade memory search` ranks cards with plain BM25 (k1=1.2, b=0.75, #2196). Each card is one field: title, tags, summary, and the full card text. Words are lowercased and split on anything that is not a letter, digit, or underscore, so `gate` no longer matches `gateway`. Repeated query words count once. The ranker builds its index in memory on every call and keeps no cache and no index file.

Each match's `score` is a non-negative float rounded to 4 decimals. Higher ranks first, and equal scores sort by card path. Scores compare only within one result list: they depend on the corpus, so a score from one workspace or query says nothing about another. The text output prints the score to 2 decimals. Before #2196, `score` was a small integer (3 per query term in the title, tags, or summary, 1 per term found only in the body); that scorer survives only as the `substring` baseline in the [retrieval eval](../evals/memory-retrieval/README.md). A query with no word characters, such as the dashboard list-all query `:`, keeps the old literal substring match, and every match scores 0.

## Search recall signal

`brigade memory search` appends a local-only event to `.brigade/memory/search-log.jsonl` on each real query (timestamp, normalized query, top-K card ids, no card body). The log is capped and drops oldest entries. `memory care status` prints search counts and the follow-up rate. `memory care status --json` surfaces a rolling `search_recall` object: `searches`, `followup_rate`, plus the window and K constants. It also reports `compared_pairs`, `repeat_queries`, `repeat_results`, and `repeat_query_results` over adjacent valid log events within the same window. Repeated queries ignore case and whitespace. Repeated results require equal nonempty card-id sets, regardless of order. The combined count requires both. Empty results, malformed events, backwards timestamps, and pairs outside the window do not count as repeated results. Card aliases are not resolved for these equality counts. Status reads the existing log without running searches or adding events. A follow-up within the window whose top-K ids share nothing with the prior search counts as a miss. Overlap or no follow-up counts as satisfied. Prompt-driven recall (see below) adds events to the same log without the query text: it records `query_hmac`, an HMAC-SHA256 of the normalized query keyed by a per-host secret in `.brigade/memory/search-log-key` (created on first use, mode 0600, never synced), plus `query_source` and the returned card ids. A plain hash of a short prompt could be reversed by guessing, so the key matters. Only explicit `memory search` calls log raw query text. This is second-class evidence (like `brief_hit_rate`): it informs whether the ranker is good enough and feeds failed queries into the offline retrieval eval harness, but it never flips doctor/`valid`. The dashboard list-all query (`:`) is not logged.

## Safe Fix Planning

`brigade memory care backfill` handles the bulk version of the same repair for setups whose cards predate the freshness convention. It finds cards with frontmatter but no reviewed or freshness dates, derives `last_reviewed` from each card file's last git commit date (the last time anyone touched the fact, or file mtime outside git, labeled as the lower-confidence source), and proposes `fresh_until` as the reviewed date plus the configured stale window. It also backfills a missing content `fingerprint` (stable hash of normalized card body) so ingest reinforcement can match known facts, and mints a missing stable ID even when care dates and fingerprint are already present. It is dry-run by default and prints the full plan. `--apply` writes the derived values into card frontmatter, never overwrites an existing value, and records a receipt under `.brigade/memory-care/backfills/`. Backfilled dates are honest rather than flattering: an old card lands as stale immediately, which is the point, since the scan then ranks the refresh queue by real age instead of reporting unknowable gaps.

`brigade memory care plan-fixes` reads the latest scan and reports proposed repairs without changing cards. Add `--apply` to apply the automatic allowlist. The `fixes` tend stage runs before retirement. It runs only through `memory care tend` or `plan-fixes --apply`. `[tend] enabled = false` disables automatic fixes and retains the reviewed-date evidence-review blocker.

| Fix kind | Disposition | Reason |
|---|---|---|
| `missing-freshness` | Applied | Derives `fresh_until` from the recorded review date plus `stale_after_days`. |
| `missing-reviewed` | Applied | Uses only the card's last commit date, or modification time outside Git. |
| `orphaned-card` | Reported | Indexes are curated bootstrap documents and must not grow with every card. |
| `missing-index-link` | Reported | A dead link needs a separate removal decision. |
| Other kinds | Reported | Evidence, content, ownership and retirement judgments are outside the fix stage. |

A missing, unreadable or future review-date source leaves the card unchanged. The journal reason records `git-last-commit` or `file-mtime`. It never claims a review happened today. Missing freshness remains blocked if there is no usable review date. A later scan can derive freshness after a reviewed-date repair.

Each changed card has one journal decision with its reason and before-bytes. `brigade memory care undo <decision-id>` restores it only if the card still has the expected bytes. Apply compares the card digest with its plan and refuses protected paths, archive pins, malformed frontmatter and unsafe input. The one-line apply summary gives counts and fixed reason words without card text or absolute paths. Hub publication continues through the existing push path.

## Refresh Queue

`brigade memory care scan` writes:

```text
.brigade/memory-care/decay/scan-latest.json
.brigade/memory-care/decay/refresh-queue.json
```

Before 0.9.1, scans wrote to `memory/cards/decay/`. Readers still fall back to that location when the default path has no `scan-latest.json` and the legacy path has existing output, so existing workspaces keep their queue continuity.

Queue entries include card identity, issue type, severity, priority, safe summary, evidence references, suggested refresh action, acceptance criteria, source item key, source fingerprint, and safe fix-plan metadata when available. `brigade memory care import-issues` imports those entries as source `memory-care` task imports with dedupe and dismissed-until-changed behavior.

## Closeout

`brigade memory care closeout` records review completion, but it cannot convert a partial or failed care chain into `reviewed`:

- `--dry-run` (with or without `--json`) prints the refresh queue that a write would mark reviewed or deferred, grouped by `issue_type`, then exits 0 without writing a receipt or changing the queue. A nonempty queue that would be blocked on a real review still previews as `would_block: true`.
- With a nonempty refresh queue and no `--defer`, a write refuses (exit 1), writes nothing, and reports the unresolved candidate count. JSON mode emits a `status: blocked` payload; human mode prints the error to stderr.
- Closeout also refuses (exit 1, nothing written) when the refresh queue file is absent or fails validation; it never treats a missing or malformed queue as empty. `--dry-run` uses the same validation and still refuses those cases.
- `--defer` is the explicit escape hatch for unresolved work and requires a nonblank `--reason`; blank reasons are refused on a write so automation cannot silently suppress work.
- An empty queue may close `reviewed`.

Only deferred closeouts record the queue's source fingerprints, so `memory care status` can tell open from quieted issues. Reviewed closeouts record an empty fingerprint list.

Receipts with malformed fields are ignored rather than honored: only a `reviewed` closeout with zero candidates and no fingerprints, or a `deferred` closeout with well-formed fingerprints and a nonblank string reason, quiets matching queue entries (#1191). When the latest receipt is ignored, status JSON reports why in `latest_closeout_ignored_reason`.

## Boundary

Memory care archives eligible cards automatically and records every change with before-bytes and undo. It never deletes memory. Paths covered by `read_only_paths` and cards with frontmatter `archive: never` are exempt. Content guard, secret and credential scanning, and pre-push checks remain in force. Scheduling care commands is the operator's job: see the [execution model](execution-model.md).

## Tending

`brigade memory care tend` runs five stages in this order: `hub` retries pending hub operations, `fixes` applies the automatic allowlist from [Safe Fix Planning](#safe-fix-planning), `retire` archives eligible cards, `inbox` drains the [review inbox](#review-inbox), and `instructions` reports [instruction budgets](#instruction-budgets) without moving anything. Use `--stage` with one of those names to select a stage, `--dry-run` to preview without writing, and `--json` for structured output. CLI scans do not run automatic care or recover pending decisions. Use explicit `memory care tend` for those operations. `memory care scan --no-tend` remains accepted as a deprecated no-op. Library scans remain read-only.

Workspace `.brigade/memory-care.toml` accepts:

```toml
[tend]
enabled = true
unused_after_days = 120
deadline_days = 14
max_archives_per_run = 20
```

A read ledger records last-read days from search and prompt recall. Retirement requires a ledger at least 120 days old, no read within that window (including available hub read stats), and a past `expires_at` or `expires` date. `fresh_until` alone never means the subject ended. A new or reset ledger archives nothing on use grounds. This stage makes no model calls.

Archive moves `memory/cards/x.md` to `memory/archive/cards/x.md`. Cards outside `memory/` keep their relative path under the archive. Archived cards leave search, recall, scan, proposals, hub push and retrieval evaluation. Writable index files lose only the card's link lines.

`brigade memory care undo <decision-id>` restores the exact bytes and reinserts removed links without replacing unrelated index edits. It refuses when either card path changed after the decision. An occupied original path is never overwritten. The journal lives under `.brigade/memory-care/journal/` with owner-only before-images. In hub sync mode, published cards are retired and restored cards revived. Unavailable hub operations remain pending for retry. Local undo remains available. Hub revision conflicts enter the existing sync conflict path.

To apply an external judgment, use `brigade memory care archive --from-file decisions.jsonl [--limit N]`. Each JSON line contains `card` (a target-relative card path) and a nonblank `reason`. The optional positive limit caps successful archives independently of the steady-state limit. Refused, missing, protected and duplicate cards are reported and skipped. Every accepted row uses the same archive journal and undo.


### Review inbox

Successful non-dry-run CLI ingest drains the review inbox. `ingest --no-tend` skips this
once. `[tend] enabled = false` disables automatic care for the workspace. Ingest now
takes the care lock and finishes or abandons pending care decisions. Library ingest
keeps its existing behavior.

| Disposition | Reason codes | Needs evidence-log proof | Action |
|---|---|---|---|
| Resolve | `document-content-present`, `document-anchor-present` | No. Read the destination to prove representation | Record already-applied and move to `memory/handoff-inbox/resolved/` |
| Retry | `expected-sha-stale`, `expected-sha-mismatch`, `card-publication-changed`, `card-publication-busy`, `document-destination-unwritable`, `bootstrap-budget-exceeded` | Use the fresh outcome's per-code flag | Recheck with the original promotion and routing flags, once per run |
| Retain | Injection, content-scan, contradiction, identity migration, collisions and unsafe destinations | Yes for contradiction candidates, otherwise no | Record a trust or security hold and leave the draft untouched |
| Hold | Proposal notifications, near matches, invalid or missing fields, unapplied knowledge and unknown codes | Yes for near matches, reinforcement records and unknown codes, otherwise no | Leave untouched with a separate hold count and reason |
| Expire | No current ingest codes qualify | No current codes | The deadline mechanism remains available for future codes that prove knowledge is represented elsewhere or carry nothing durable |

An unsuccessful retry stays held when it contains unapplied knowledge. A later pull
request in this plan will add background model judgment for held drafts. Every inspected
draft gets a reason in the body-free report at `.brigade/memory-care/inbox/latest.json`.
Reading that report is optional. Oversized destinations stay held. Invalid UTF-8,
oversized drafts, unsafe paths, tracked review drafts and drafts without an unambiguous
routing timestamp are skipped and reported. Malformed writer folders are inspected only
within the target. Quarantine is never read. External writer roots are reported and
skipped because the local journal cannot undo writes outside its target.

An already-applied draft resolves only after a fresh read proves all proposed content is present in the destination. This proof works on any platform and does not depend on the evidence log. Review holds remain held even when evidence is available.

A draft cited by the evidence log stays at its original path. A journaled row in
`.brigade/memory-care/inbox-ledger.jsonl` removes it from pending work. If the evidence
log cannot be read completely, outcomes needing evidence-log proof are held. Independent resolutions retain potentially cited drafts in place through the ledger. Undo restores
the draft's pending state and any changed card or document. Card replacements require
unchanged destination bytes. Appended blocks require an unchanged exact match. Machine
logs stay append-only: undo appends a digest-addressed retraction, and readers omit
withdrawn rows regardless of row order. Document undo matches its exact block under the
same short lock used by Brigade document appenders, preserving later blocks. External
editors do not take this lock, so their concurrent edits cannot be coordinated. A
document-lock timeout leaves retries waiting. Plain ingest routes its original bytes
into the review inbox.

Retries prepare the journal and save before-images before writing a destination.
Recovery uses the same prepare-then-finish protocol as card archives. Evidence, document
and ledger bytes append after finish, with their path, length and digest recorded in the
decision. Recovery recognizes a published block to avoid duplicate appends. Reprocessing
keeps ingest's destination, identity, expected-SHA and injection checks, and
also honors memory-care read-only paths and archive pins. This drain changes local files
only. Hub handoff rows remain outside this stage, and an unavailable hub cannot cause a
hub-side inbox change.

A handoff aimed at a known instruction file goes to that file only while the append
stays under the file's act-at threshold. Past it, behind a symlink, or when the
`[instructions]` table cannot be read, the content is appended to a private overflow
card, `memory/cards/instructions/<file-key>--overflow-<type>.md`, rolling to `-2`, `-3`
at `max_card_bytes`. Every overflow card is `visibility: private` and `archive: never`.
Each append is an undoable `card-append` decision, so `brigade memory care undo` removes
exactly that block and keeps later ones. A replay finds its block in any earlier card of
that file and type and never writes it twice. Holds from earlier releases that were
refused for size (`bootstrap-budget-exceeded`) drain the same way on the next run.
Excluding a file or setting `enabled = false` does not turn this routing off. A handoff
with an injection signal or an unsafe name is still kept for a person.

The card path is predictable, so the writer trusts a card only when this feature created
it. Before every append to an existing card it requires one frontmatter block with
`visibility: private` (read by the same parser the hub push uses), `archive: never`, the
`moved-instructions` tag and a `source_file`. The `moved-instructions` tag must be an exact member of the `tags` value, not a substring
of it. A card that fails any of these, or that is too large for the duplicate check to
read in full, receives nothing: the handoff is held as `overflow-card-untrusted`, with
the card's path in the reason. That code is a retry code. Each ingest run and each drain
re-evaluates it, so the draft stays held while the card still fails and is delivered
with no person involved once the card is repaired or removed. Ingest and the drain write
`.brigade/memory-care/inbox/blocked-cards.json` (`{"version": 1, "cards": [{"path",
"problem", "held_drafts"}]}`), one row per card that holds drafts, with its
workspace-relative path and the count. The file is rewritten every run and empties when
the card passes. A block
that cannot fit an empty card, frontmatter included, is kept as `overflow-block-too-large`
and never written oversize. A configured `max_card_bytes` above the duplicate scan's
1 MiB read limit is not applied. Retry reads an overflow card with that same limit, so a
card routing may choose is a card a held draft can be delivered to. The journal decision records both the limit used and the
requested one. The undo of the append that created a card needs the card's frontmatter
unchanged, so a later stage that rewrites it (`last_reviewed`, `fresh_until`) makes that
one undo refuse, and later appends are undone as before.

Every one of these rules is enforced on the exact bytes of the opened destination, under
the document lock, at the moment of writing, on every path that writes, including
recovery of a decision that was committed earlier. The writer opens the card read-write
without following links, and refuses to write when the card is not in the state the
decision recorded (created or existing), is not one this feature created, no longer has
the bytes the decision pinned, or would pass `max_card_bytes` with the block. A refused
recovery is recorded on the decision (`append_refused`), which recovery does not retry,
and a handoff that was moved aside for the retry goes back to the review inbox. A direct
append to an instruction file measures the opened file under the lock and goes to the
overflow card in the same run when the block no longer fits, so two runs cannot both
decide that a block fits. The file's `(st_dev, st_ino)` is pinned when the route is
decided and must match at the open, which stops a swapped-in link but still accepts a
file that was already hard linked. Overflow cards must have a single link.

When the care configuration (`.brigade/memory-care.toml`) cannot be parsed, the card's
protected paths cannot be read, so nothing is written and the handoff is held as
`document-destination-unwritable`. That is a retry code: the handoff is retried
automatically on the next drain once the file parses, and it is never lost. While the
file does not parse, even a small append to a file far below its act-at goes to this
hold instead of to the file.

The drain also sweeps redundant before-images of abandoned decisions. A sweep requires
an exact surviving copy at a recorded path, retains unique or unreadable images and
restorable decisions, and records each removed path, size, digest and surviving live
copy without saving another copy. These redundant images are not recoverable through
undo. Orphan directories without readable decision metadata stay in place and are
reported.

### Instruction budgets

Always-loaded instruction files (`AGENTS.md`, `USER.md`, `TOOLS.md`, `MEMORY.md` and the other names an agent harness reads at startup, plus the user-level files Codex and the other harnesses load) are measured against one limit each. This release measures and reports. It moves nothing.

**How the limit is chosen.** The limit is the smallest of Brigade's target budget for the file and every cap a loader enforces on it. The target is the per-name budget, else `default_limit_bytes` (20,000). OpenClaw caps come from its own configuration, per agent, with `USER.md` fixed at 4,000. The Codex cap is `project_doc_max_bytes` (32,768 by default) and applies to the project file Codex selects. A character cap is used as the same number of bytes, so a limit is never looser than the harness. When two caps tie, the loader is named. Paths that reach the same file through a symlink or hard link are one row, named by the real file.

**Thresholds and states.** Act-at is 85% of the limit and trim-to is 70%. A file at or under act-at is `within`. Between act-at and the limit it is `over-act-at`: nothing is cut yet and no alert is raised. Over the limit it is `could-not-trim` with reason `report-only`, because no file can be moved yet. A loader total over its cap is `total-over-cap`.

**Where it shows.**

- `brigade memory care tend --stage instructions --dry-run --json` prints one row per file (display path, loaders, class, bytes, limit and its source, act-at, trim-to, state, and any overflow cards the file does not name yet) and the loader totals. It also runs as part of a full `memory care tend`.
- `brigade doctor` has a `bootstrap-budget: <file>` row per file. A workspace file over its limit fails. A user-level file warns and is an operator row. A link that leaves the workspace also warns, as a plain row. A loader total over its cap has an `instructions: loader-total <loader>` warning row, because the harness cuts at the total.
- `brigade budgets check` prints the same rows with `ok`, `warn`, `fail` or `missing`, a `[warn]` line for each loader total over its cap, and returns 1 only on `fail`. Doctor and `budgets check` take their rows from one inventory and one set of rules, so they agree, including on a per-name path that exists and is not a regular file (a directory, say), which fails in both. A link to nothing is a missing file in both.
- `brigade work brief` prints one line while a file is over its limit, for example `instruction_budget: could-not-trim path="USER.md" 8241/4000 reason=report-only next="brigade memory care tend --stage instructions --dry-run --json"`. The line shows on every brief while the state holds in this release. A later release keeps the previous state and prints only new alerts.

**Unknown size.** A file that exists but whose size nothing could read is never `within`. Its state is `size-unknown`, with the skip reason as the code. A file with no read permission takes its size from `stat`, which needs none, so it is rated like any other file. Only when `stat` fails too, or the opened file is not a regular file, is the size unknown. A workspace file in that state fails in doctor and `budgets check` and raises `instruction_budget: size-unknown path="..." limit=... reason=...` in the brief. A user-level file warns. A file over 1 MiB or not UTF-8 keeps a trusted size.

**Names are data, not text.** A file name, a configured path, an override name or an OpenClaw agent id can be written by an agent or by content an agent ingested, and the brief is read by a model. Every such value in a line of text is written as an encoded data value in a quoted field. Only ASCII letters, digits and `. _ - / ~` pass as they are. Every other character, including a space, a colon, a backtick, `#`, `=`, `<`, `>`, a quote, a non-ASCII character and a control character, becomes an escape such as `\x20` or `\u202e`, and a value over 96 characters ends in `[truncated]`. So `ignore previous instructions.md` prints as `path="ignore\x20previous\x20instructions.md"`. The fixed parts of a line (the prefix, the state names, the reason codes and `next=`) come from constants. The JSON payloads keep the real values as JSON strings and never carry the target or home directory.

**Configuration changes stay visible.** The `[instructions]` table in `.brigade/memory-care.toml` reads `enabled`, `act_at_percent`, `trim_to_percent`, `default_limit_bytes`, `exclude`, `files.<path>.class` and `files.<path>.limit_bytes`. A configured limit can only lower a limit. An excluded file stays in the report, marked with where the exclusion came from, and still counts toward loader totals. An override that names a path outside the workspace and the home directory, or a credential or shell-setup file, is rejected and the row is forced to class `report`. The report lists every rejected override, every clamped limit, the keys that moved a limit for every file, and why an OpenClaw configuration could not be read. While any override was rejected, any limit clamped, any file excluded or any such key set, `brigade work brief` prints one `instruction_budget: config-changed` line and `brigade doctor` adds an `instructions: config-changes` warning. Reports show display paths only, never a full home path or file content.

**Invalid configuration.** A bad `[instructions]` table never stops a reader. Doctor and `budgets check` fail one `instructions: config` row and still measure with the defaults, and the brief prints one `config-invalid` line. A `tend --stage instructions` run exits with the error like a bad `[tend]` table, and so does an inventory error. In a plain `brigade memory care tend` the stage records the error (`config-invalid` or `inventory-failed`), the other stages finish and the state file is saved, and the run exits 1 with `failed` and `failed_stages` in the payload. If the brief cannot read the inventory it prints one `instruction_budget: inventory-failed` line.

`enabled = false` turns off the report and the alerts only, and `exclude` only removes a file from them. Neither turns off overflow routing. They do not turn off doctor: an excluded or disabled over-limit workspace file keeps `brigade doctor` failing, by design, because the loader cuts the file either way. Moving sections of a full file comes in a later release.


## Session-start and prompt recall (#466 Slice 1, #2196)

`brigade memory recall --target <hub-or-mirror> --cwd <session-cwd> --query "token refresh" --limit 5`
runs deterministic card search using caller-supplied task terms. `--query` is
optional, normalized to one printable line, and bounded to 256 characters.
Callers that have the user's prompt pass it as the query. Precedence is the
prompt or a nonempty explicit query, then the stable repository basename from
the Git common directory at a checkout root, then the cwd basename split on `-`
and `_`, then `workspace` when no basename exists. At the memory root, callers
without a query use `workspace` without probing Git. A blank query falls back
to checkout context. Linked worktrees share repository terms. Subdirectories
and unavailable or untrusted Git context retain the cwd fallback.

The payload's `query_source` says where the query came from: `prompt` (the
user's prompt, from the `UserPromptSubmit` hook), `explicit` (`--query`), `cwd`
(a repository or folder basename), or `workspace` (the generic fallback at the
memory root). Folder names make poor queries, so readers should discount `cwd`
and `workspace` results. Before #2196 a repository basename was labeled `repo`.

Prompt text never reaches disk. The prompt travels to the recall child over
stdin. Prompt recall output starts with `memory recall (from your prompt):`
instead of echoing the query, because hook output is persisted under
`.brigade/work/claude-hooks/`. Its search-log event stores only a keyed hash of
the query (see [search recall signal](#search-recall-signal)).

Query context never changes `--target`. Output is index-level only: title,
tags, and card path (at most 5 matches / 10 lines). Card bodies never appear.
Git identity and search run in the existing child process with a five-second
deadline. Earlier target resolution is outside that deadline. Recall exits 0
on missing targets, timeouts, or failures. Queries are explicit caller context,
with no transcript inference.

Repository terms improve query consistency, but keyword search does not filter
individual card repository/task scope or freshness. The [task/worktree fixture
comparison](../evals/memory-retrieval/task-recall/README.md) measures relevance
and exposes those limits through the existing stale/scope checks. Retrieval
relevance and repeat-search counts remain second-class diagnostics, separate
from manifest-backed verification scores.

Machine-local config key: `memory_recall_target` in `.brigade/config.json`.

- Workspace-depth installs may omit the key; recall defaults to the current target.
- Repo-depth installs stay unconfigured until an explicit hub or mirror path is set.
- Claude's managed `SessionStart` hook merges recall beside the work brief once per
  session and fails open when the target is missing, empty, or unreadable. With
  no prompt available it searches folder terms and labels them `cwd`.
- Claude's managed `UserPromptSubmit` hook recalls cards for each prompt, using
  the trimmed prompt capped at 256 characters, with the same five-second
  deadline and 5 match / 10 line caps. It adds one recall subprocess per prompt
  and does nothing unless a recall target is active.

A file that is in the report only because `[instructions.files]` enrolled it is shown in the brief, in doctor and in `budgets check` text by an id such as `configured-file-1a2b3c4d5e6f`. Its name was chosen by whoever wrote the configuration, and a name can read as a sentence even when it holds only path characters. The JSON output carries the same id as `text_name` beside the display path. The brief also prints one `loader-config-unreadable` line when a harness configuration cannot be read, because the limits then fall back to Brigade's own targets and may be looser than the harness's real cap.

### Operator smoke (before merge)

Use a temp target, never the real home directory:

```bash
target="$(mktemp -d)"
hub="$(mktemp -d)"
git init -q -b main "$target"
python -m brigade init --target "$target" --depth repo --harnesses claude
# Point the repo at a local hub/mirror (edit .brigade/config.json):
#   "memory_recall_target": "<hub>"
mkdir -p "$hub/memory/cards"
printf '%s\n' '---' 'title: Smoke Card' 'tags: ["smoke"]' '---' 'fixture body' \
  > "$hub/memory/cards/smoke.md"
python -m brigade memory recall --target "$hub" --cwd "$target/astro-portfolio" --limit 5
# Expect title/tags/path only; no card body.
python -m brigade work hooks install --target "$target"
# In a real Claude Code session on $target (or a nested cwd named like
# astro-portfolio): confirm SessionStart injects the work brief plus recall
# exactly once, then a second SessionStart in the same session injects nothing.
```

For operator-owned cron, systemd, and CI recipes (and `brigade care`), see [scheduled care](scheduled-care.md).

### Reads protect cards

Automatic retirement needs a positive reason, currently an `expires_at` or
`expires` date in the past. Being unread never archives a card by itself.
A recorded read within `unused_after_days` blocks automatic retirement.

Search results, recall at session start and after compaction, explicit card
reads, local connector reads, replica card reads, and local dispatch memory
context count as reads. The Claude Code Read hook also records an opened card
in its owning memory root. Vault search and show count projected notes when
their identity matches a canonical card in the target memory root. Projection
maps, notes without an identifiable card, and projections of another root
cannot be attributed. Card listings and maintenance operations do not count.

Brigade cannot see a file opened by a person or by a tool it does not
instrument. Hub read counts have budgets and can miss reads. Reads therefore
only ever protect a card. Missing, incomplete, or too-young read history blocks
automatic retirement. `memory care archive --from-file` applies an outside
judgment and does not consult reads.
