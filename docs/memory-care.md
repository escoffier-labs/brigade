# Memory Care

Brigade memory care is a local, read-only scanner for durable memory cards. It detects cards that need review, writes a refresh queue, and routes review tasks into the existing work inbox.

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

`status` and JSON output summarize freshness metadata coverage: reviewed dates present, missing, and stale, freshness dates present, missing, and expired, confidence distribution, and evidence metadata present or missing. These checks only explain review needs. Brigade does not edit memory cards automatically.

`status` also reports a read-only `archive_candidates` view derived from the saved scan payload (no second card-tree walk). A card is a candidate only when it has a parseable `last_reviewed` and `age_days > 2 * stale_after_days` (exactly 2x TTL is not a candidate). Missing, invalid, or future reviewed dates are unassessable and are never inferred from mtime or Git history. Each candidate includes age, last-reviewed, freshness context, and evidence pointers. The view sets `read_only`, `approval_required`, and `would_archive=false`; there is no `--apply` path and status never archives or writes state.

## Card identity

New canonical cards mint one opaque `card-<uuid4>` ID in frontmatter. A valid ID is never replaced on rewrite or reinforcement. Legacy cards without an ID stay readable through a workspace-relative path fallback (`path:memory/cards/<stem>.md`, plus filename stem and `topic` aliases). Care queues, search and recall logs, refresh imports, and retrieval-eval fixtures prefer the explicit ID and keep those legacy keys as aliases. Alias collisions fail the consumer without rewriting card files.

`brigade memory care backfill` (dry-run by default) writes a deterministic mapping receipt under `.brigade/memory-care/backfills/dry-run-mapping.json` when there is at least one candidate. The receipt reports explicit-ID coverage, path fallbacks, malformed IDs, duplicate IDs (including workspaces without `memory/NAMESPACE`), and alias collisions. `old_id` is the consumer-facing identity (explicit ID, topic, stem, or path) so care queues, search logs, and refresh imports can be remapped. It contains relative paths and IDs only: no card bodies and no absolute paths. A zero-candidate dry run writes nothing. Missing IDs stay a doctor warning until audited coverage is 100 percent; backfill mints an ID on complete care cards that only lack one so that end state is reachable. `--apply` is the named reviewed rewrite that may mint IDs on existing cards and writes the same IDs the dry-run receipt previewed; it refuses to write when aliases collide.

## Search recall signal

`brigade memory search` appends a local-only event to `.brigade/memory/search-log.jsonl` on each real query (timestamp, normalized query, top-K card ids, no card body). The log is capped and drops oldest entries. `memory care status` prints search counts and the follow-up rate. `memory care status --json` surfaces a rolling `search_recall` object: `searches`, `followup_rate`, plus the window and K constants. It also reports `compared_pairs`, `repeat_queries`, `repeat_results`, and `repeat_query_results` over adjacent valid log events within the same window. Repeated queries ignore case and whitespace. Repeated results require equal nonempty card-id sets, regardless of order. The combined count requires both. Empty results, malformed events, backwards timestamps, and pairs outside the window do not count as repeated results. Card aliases are not resolved for these equality counts. Status reads the existing log without running searches or adding events. A follow-up within the window whose top-K ids share nothing with the prior search counts as a miss. Overlap or no follow-up counts as satisfied. This is second-class evidence (like `brief_hit_rate`): it informs whether the keyword scorer is good enough and feeds failed queries into the offline retrieval eval harness, but it never flips doctor/`valid`. The dashboard list-all query (`:`) is not logged.

## Safe Fix Planning

`brigade memory care backfill` handles the bulk version of the same repair for setups whose cards predate the freshness convention. It finds cards with frontmatter but no reviewed or freshness dates, derives `last_reviewed` from each card file's last git commit date (the last time anyone touched the fact, or file mtime outside git, labeled as the lower-confidence source), and proposes `fresh_until` as the reviewed date plus the configured stale window. It also backfills a missing content `fingerprint` (stable hash of normalized card body) so ingest reinforcement can match known facts, and mints a missing stable ID even when care dates and fingerprint are already present. It is dry-run by default and prints the full plan. `--apply` writes the derived values into card frontmatter, never overwrites an existing value, and records a receipt under `.brigade/memory-care/backfills/`. Backfilled dates are honest rather than flattering: an old card lands as stale immediately, which is the point, since the scan then ranks the refresh queue by real age instead of reporting unknowable gaps.

`brigade memory care plan-fixes` reads the latest scan and builds a planning-only view for low-risk metadata repairs such as missing reviewed dates or missing freshness dates. The command never writes card files. Plan items include candidate metadata fields, source fingerprints, blockers, and the next review command. `status` and `plan-fixes` print the top block reasons with counts so a fully blocked plan is diagnosable.

A planned fix is blocked when Brigade can name a metadata repair but will not treat it as ready. `requires-current-evidence-review` is an operator decision: `missing-reviewed` stays blocked until someone checks current evidence and sets `last_reviewed`. `requires-operator-freshness-date` applies only when the card also has no reviewed date, so Brigade cannot derive `fresh_until`. Other blockers (`card-file-missing`, `card-frontmatter-missing`, and similar) mean the card is not readable as a metadata-only repair.

When a card already has `last_reviewed` but no `fresh_until`, that freshness plan is unblocked. Brigade derives `fresh_until` as `last_reviewed + stale_after_days`, the same rule as `backfill`. The plan still writes nothing. Run `brigade memory care backfill` (dry-run, then `--apply` after review) to drain that class. To unblock a reviewed-date plan, review the card against current evidence, or promote the memory-care import and set `last_reviewed` through the normal refresh path.

Safe fix plans are copied into memory-care imports as metadata so the work inbox and daily brief can show that a local plan exists. The plan remains advisory. Follow-up work still goes through task promotion, `backfill --apply` for derived freshness dates, or Memory Handoff review.

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

Memory care does not run a scheduler, mutate canonical memory, perform remote sync, or promote imports automatically. Card edits stay explicit: routine scan and plan commands never write card files. Only `brigade memory care backfill --apply` may add derived frontmatter, with a receipt. Refreshes stay explicit through reviewed work tasks or the existing Memory Handoff flow. Scheduling those care commands is the operator's job: see the [execution model](execution-model.md). Opt-in `brigade care install` can install target-namespaced systemd user timers on Linux or launchd agents on macOS without Brigade owning a daemon.

## Session-start recall (#466 Slice 1)

`brigade memory recall --target <hub-or-mirror> --cwd <session-cwd> --query "token refresh" --limit 5`
runs deterministic card search using caller-supplied task terms. `--query` is
optional, normalized to one printable line, and bounded to 256 characters.
Precedence is a nonempty explicit query, stable repository basename from the
Git common directory at a checkout root, cwd basename split on `-` and `_`,
then `workspace` when no basename exists. At the memory root, callers without
a task query use `workspace` without probing Git. A blank task query falls back
to checkout context. Linked worktrees share repository terms. Subdirectories
and unavailable or untrusted Git context retain the cwd fallback.

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
  session and fails open when the target is missing, empty, or unreadable.

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
