# Brigade Simplification Audit Report

Date: 2026-06-04

This pass checked whether Brigade's growing command and tool surface is accidental overlap or a structured operator system. The result is mixed: the public CLI is large, but the command inventory is current and most overlap is consistent lifecycle vocabulary across stations. The main simplification opportunity is implementation reuse, not immediate command removal.

## Checks Run

- `brigade roadmap commands --check --json`
- parser-derived command grouping from `/tmp/brigade-commands.json`
- Python AST duplicate function-name inventory over `src/brigade/*.py`
- focused duplicate helper search for JSON, hashing, slug, clock, config, and receipt helpers
- `brigade security scan --target . --policy strict --fail-on none --json`

## Current Shape

- Public CLI commands: 468
- Command inventory: current
- Command inventory issues: 0
- Strict security findings: 0
- Largest command groups:
  - `work`: 139
  - `repos`: 70
  - `tools`: 46
  - `center`: 29
  - `daily`: 26
  - `release`: 22
  - `skills`: 17
  - `handoff`: 16
  - `security`: 14

## Finding 1: Lifecycle Verbs Are Consistent, Not Random

Commands repeat verbs such as `plan`, `build`, `list`, `show`, `archive`, `closeout`, `compare`, `doctor`, and `import-issues` across stations. This looks large in the inventory, but it is a consistent operator grammar.

Classification: keep public CLI shape.

Rationale: these commands act on different evidence stores and user jobs. Collapsing them into a generic command would make discovery and help text worse unless the underlying receipt types are first unified.

## Finding 2: Action Queues Are The Best First Extraction Target

The strongest implementation duplication is the action queue lifecycle across:

- `center actions`
- `work phases actions`
- `repos actions`
- `repos release actions`

Repeated functions include `actions_plan`, `actions_build`, `actions_list`, `actions_show`, `actions_start`, `actions_done`, `actions_defer`, `_read_actions`, `_find_action`, and `_set_action_status`.

Classification: extract helper.

Recommended slice: create a small internal action-queue helper that owns read/write/find/status transitions and lets each station keep its local payload builder and command names.

Risk: medium. The commands are heavily tested, but their output shapes likely differ in small ways.

## Finding 3: Report Bundle Lifecycles Are The Second Extraction Target

The next duplicated family is report lifecycle code across:

- `center report`
- `work phases report`
- `repos report`
- `repos release`
- `release candidate`

Repeated functions include `report_build`, `report_list`, `report_show`, `report_closeout`, `report_compare`, `_reports_root`, `_resolve_report`, `_report_payload`, and `_write_report_bundle`.

Classification: extract helper.

Recommended slice: create a report-store helper for root paths, list/show resolution, JSON/Markdown bundle writes, archive/closeout metadata, and compare scaffolding.

Risk: medium-high. Report commands are release-facing and should be refactored one station at a time.

## Finding 4: Generic JSON/Time/Hash Helpers Are Duplicated Everywhere

Common helpers are repeated across many modules:

- `_write_json`: 15 modules
- `_read_json`: 13 modules
- `_now`: 15 modules
- `_stable_hash`: 5 modules
- `_slug`: 5 modules
- `_read_jsonl`: 4 definitions

Classification: extract helper, opportunistic.

Recommended slice: add a small `brigade.io` or `brigade.local_json` helper for read/write JSON, JSONL, stable hash, and UTC timestamps. Migrate only modules touched by active work first.

Risk: low if migrated opportunistically, medium if done repo-wide in one pass.

## Finding 5: Command Removal Is Not The Right First Move

No command pair was obviously safe to remove. The suspicious-looking overlaps usually differ by evidence type:

- `doctor` commands check different stations.
- `plan` commands produce different local receipts.
- `closeout` commands close different review loops.
- `import-issues` commands preserve source-specific provenance.

Classification: keep, then document grouping.

Recommended slice: improve docs by grouping commands by lifecycle and station instead of flattening all commands in prose. Keep `docs/command-inventory.md` generated.

## Finding 6: The Code Simplifier Should Not Run Blind

The available `tools/simplify.md` is a cross-harness summarization tool, not a safe source-code rewrite tool. It explicitly says not to edit source files unless asked. A blind simplifier over Brigade would risk flattening command boundaries that are intentional.

Classification: no automated rewrite yet.

Recommended slice: run simplification only on one reviewed target at a time, starting with action queue helpers or JSON helper extraction.

## Candidate Backlog

1. Extract shared local JSON utilities.
   - Type: extract helper
   - Risk: low
   - Verification: focused tests for touched module plus full suite

2. Extract action queue storage and status transitions.
   - Type: extract helper
   - Risk: medium
   - Verification: `center`, `phases`, and `repos` action tests

3. Extract report store primitives.
   - Type: extract helper
   - Risk: medium-high
   - Verification: report, release, center, and phase tests

4. Add a lifecycle-oriented command map to docs.
   - Type: docs only
   - Risk: low
   - Verification: `brigade roadmap commands --check`

5. Add an internal complexity budget check.
   - Type: new audit command or security-style doctor check
   - Risk: low-medium
   - Verification: focused roadmap/doctor tests

## Recommendation

Do not remove public commands yet. Start with internal helper extraction, first JSON utilities, then action queues, then report stores. That should reduce the feeling of overlap while preserving the operator grammar users already rely on.
