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

## Issue Types

Memory care can emit:

- `stale`
- `expired`
- `undersourced`
- `contradictory`
- `missing-index-link`
- `orphaned-card`
- `oversized-card`
- `missing-frontmatter`
- `missing-reviewed`
- `missing-freshness`

Contradiction detection is deliberately conservative. Brigade only flags explicit duplicate card identities or metadata hints, not LLM-inferred factual conflicts.

`status` and JSON output summarize freshness metadata coverage: reviewed dates present, missing, and stale, freshness dates present, missing, and expired, confidence distribution, and evidence metadata present or missing. These checks only explain review needs. Brigade does not edit memory cards automatically.

`status` also reports a read-only `archive_candidates` view derived from the saved scan payload (no second card-tree walk). A card is a candidate only when it has a parseable `last_reviewed` and `age_days > 2 * stale_after_days` (exactly 2x TTL is not a candidate). Missing, invalid, or future reviewed dates are unassessable and are never inferred from mtime or Git history. Each candidate includes age, last-reviewed, freshness context, and evidence pointers. The view sets `read_only`, `approval_required`, and `would_archive=false`; there is no `--apply` path and status never archives or writes state.

## Card identity

New canonical cards mint one opaque `card-<uuid4>` ID in frontmatter. A valid ID is never replaced on rewrite or reinforcement. Legacy cards without an ID stay readable through a workspace-relative path fallback (`path:memory/cards/<stem>.md`, plus filename stem and `topic` aliases). Care queues, search and recall logs, refresh imports, and retrieval-eval fixtures prefer the explicit ID and keep those legacy keys as aliases. Alias collisions fail the consumer without rewriting card files.

`brigade memory care backfill` (dry-run by default) writes a deterministic mapping receipt under `.brigade/memory-care/backfills/dry-run-mapping.json` when there is at least one candidate. The receipt reports explicit-ID coverage, path fallbacks, malformed IDs, duplicate IDs (including workspaces without `memory/NAMESPACE`), and alias collisions. `old_id` is the consumer-facing identity (explicit ID, topic, stem, or path) so care queues, search logs, and refresh imports can be remapped. It contains relative paths and IDs only: no card bodies and no absolute paths. A zero-candidate dry run writes nothing. Missing IDs stay a doctor warning until audited coverage is 100 percent; backfill mints an ID on complete care cards that only lack one so that end state is reachable. `--apply` is the named reviewed rewrite that may mint IDs on existing cards and writes the same IDs the dry-run receipt previewed; it refuses to write when aliases collide.

## Search recall signal

`brigade memory search` appends a local-only event to `.brigade/memory/search-log.jsonl` on each real query (timestamp, normalized query, top-K card ids; no card body). The log is capped and drops oldest entries. `memory care status` (and `--json`) surfaces a rolling `search_recall` object: `searches`, `followup_rate`, plus the window and K constants. A follow-up within the window whose top-K ids share nothing with the prior search counts as a miss; overlap or no follow-up counts as satisfied. This is second-class evidence (like `brief_hit_rate`): it informs whether the keyword scorer is good enough and feeds failed queries into the offline retrieval eval harness, but it never flips doctor/`valid`. The dashboard list-all query (`:`) is not logged.

## Safe Fix Planning

`brigade memory care backfill` handles the bulk version of the same repair for setups whose cards predate the freshness convention. It finds cards with frontmatter but no reviewed or freshness dates, derives `last_reviewed` from each card file's last git commit date (the last time anyone touched the fact, or file mtime outside git, labeled as the lower-confidence source), and proposes `fresh_until` as the reviewed date plus the configured stale window. It also backfills a missing content `fingerprint` (stable hash of normalized card body) so ingest reinforcement can match known facts, and mints a missing stable ID even when care dates and fingerprint are already present. It is dry-run by default and prints the full plan. `--apply` writes the derived values into card frontmatter, never overwrites an existing value, and records a receipt under `.brigade/memory-care/backfills/`. Backfilled dates are honest rather than flattering: an old card lands as stale immediately, which is the point, since the scan then ranks the refresh queue by real age instead of reporting unknowable gaps.

`brigade memory care plan-fixes` reads the latest scan and builds a planning-only view for low-risk metadata repairs such as missing reviewed dates or missing freshness dates. The command never writes card files. Plan items include candidate metadata fields, source fingerprints, blockers, and the next review command. Reviewed-date plans are blocked until the operator checks current evidence. Freshness-date plans are blocked until the operator chooses an appropriate date or documents why the card should not expire.

Safe fix plans are copied into memory-care imports as metadata so the work inbox and daily brief can show that a local plan exists. The plan remains advisory. Follow-up work still goes through task promotion or Memory Handoff review.

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

- With a nonempty refresh queue and no `--defer`, closeout refuses (exit 1), writes nothing, and reports the unresolved candidate count. JSON mode emits a `status: blocked` payload; human mode prints the error to stderr.
- `--defer` is the explicit escape hatch for unresolved work and requires a nonblank `--reason`; blank reasons are refused so automation cannot silently suppress work.
- An empty queue may close `reviewed`.

Deferred and reviewed closeouts both record the queue's source fingerprints so `memory care status` can tell open from quieted issues.

## Boundary

Memory care does not run a scheduler, mutate canonical memory, perform remote sync, or promote imports automatically. Card edits stay explicit: routine scan and plan commands never write card files. Only `brigade memory care backfill --apply` may add derived frontmatter, with a receipt. Refreshes stay explicit through reviewed work tasks or the existing Memory Handoff flow. Scheduling those care commands is the operator's job: see the [execution model](execution-model.md). Opt-in `brigade care install` can install target-namespaced systemd user timers on Linux or launchd agents on macOS without Brigade owning a daemon.

## Session-start recall (#466 Slice 1)

`brigade memory recall --target <hub-or-mirror> --cwd <session-cwd> --limit 5`
runs the deterministic card search with terms derived from the cwd basename
(split on `-` and `_`). Output is index-level only: title, tags, and card path
(at most 5 matches / 10 lines). Card bodies never appear.

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
