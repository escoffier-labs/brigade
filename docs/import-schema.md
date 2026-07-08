# Brigade Work Import JSONL Schema

Brigade work imports are the local handoff contract for scanners, wrappers, and chat adapters that discover candidate work. Producers write JSON Lines files, then an operator or wrapper validates and ingests them with `brigade work import`.

Imports are local review items. They do not write canonical memory directly.

## Commands

```bash
brigade work import validate imports.jsonl
brigade work import ingest imports.jsonl
brigade work import memory-care
brigade work import memory-refresh
brigade work import chat-sweep
brigade chat sweep import-issues discord-export
brigade workflow scan --import-candidates
brigade workflow propose-runbook <candidate-id>
brigade work inbox
brigade work inbox doctor
brigade work inbox archive
brigade work import triage
brigade work import provenance
brigade work import plan <import-id>
brigade work import plan-handoff <import-id>
brigade work import promote-handoff <import-id>
brigade work import promote --run <import-id>
brigade work import promote --all --source memory-care --kind task
```

`validate` checks a JSONL file without writing. `ingest` appends valid records into `.brigade/work/imports/inbox.jsonl`, skipping duplicate pending records with the same source, kind, and normalized text. Scanner producers can also provide stable source item keys and fingerprints so repeated ingestion skips equivalent pending or promoted imports, while dismissed imports stay dismissed unless the source item changes materially. `inbox` groups pending imports for daily review. `inbox doctor` reports queue hygiene issues, and `inbox archive` moves old closed imports to `.brigade/work/imports/archive.jsonl` without touching pending imports. `import provenance` audits producer imports for stable source identity, source fingerprints, safe summaries, evidence references, and scanner run provenance. `plan` previews the task or handoff a reviewed import would create. `promote --run` promotes one task import and immediately runs it through the normal work-session loop. `plan-handoff` and `promote-handoff` preview and write reviewed Memory Handoff drafts for durable non-task imports. `memory-care` reads `memory/cards/decay/refresh-queue.json` and converts queued cards into task imports. `memory-refresh` accepts the same queue plus `candidates` or `refresh_candidates` and writes TDD-ready refresh task imports. `chat-sweep` reads `.brigade/chat-memory-sweeps/latest.json` and converts sweep `issues`. Actionable issues become task imports. `brigade chat sweep import-issues <surface-id>` produces that same chat-sweep import shape from configured local chat export fixtures. `workflow scan --import-candidates` mines repeated command sequences from local verify receipts and daily run receipts, then writes reviewed workflow imports. `workflow propose-runbook` writes a workshop `runbook.json` from a chosen sequence candidate using its concrete `example_commands` only. It does not execute the runbook.

## Record Shape

Each non-empty line must be one JSON object:

```json
{"text":"Refresh memory card memory/cards/tools.md: source-of-truth changed","kind":"task","source":"memory-care","metadata":{"card_file":"memory/cards/tools.md","reason":"source-of-truth changed"}}
```

Required fields:

- `text`: non-empty string. The operator-facing candidate work.

Optional fields:

- `kind`: one of `task`, `finding`, `decision`, `preference`, `incident`, `link`, or `command`. Defaults to `task`.
- `source`: non-empty string identifying the producer, such as `memory-care`, `slack`, `discord`, `telegram`, or `repo-scan`. Defaults to `manual`.
- `metadata`: JSON object with producer-specific context.

Task-only optional fields:

- `type`: one of `task`, `feature`, `bug`, `docs`, `security`, `workflow`, `research`, or `chore`.
- `priority`: one of `low`, `normal`, `high`, or `urgent`.
- `template`: one of `vertical-slice`, `bugfix`, `red-green-refactor`, `docs`, or `security-follow-up`.
- `acceptance`: list of non-empty strings. These become local task acceptance criteria when the import is promoted.

Task fields are valid only when `kind` is `task`. When a task import is promoted, Brigade preserves these fields on the local task ledger item and keeps source-specific details in `metadata`.

Durable non-task imports with kind `decision`, `preference`, `link`, `command`, `finding`, or `incident` can be promoted into a local Memory Handoff draft. Promotion writes to the configured handoff inbox, runs handoff lint, then marks the import `promoted` only after the draft is valid. The promoted import stores `handoff_path`, `handoff_target_document`, `promoted_at`, and `handoff_source_fingerprint`. Review those drafts with `brigade handoff list`, `brigade handoff show`, and `brigade handoff archive`; those commands do not run the canonical ingestor or edit memory.

Recommended metadata keys:

- `card_file`: memory card path for memory-care records.
- `card_id`: stable memory card identity.
- `reason`: short reason the item was produced.
- `refresh_reason`: reason a memory card needs review.
- `source_item_key`: stable producer item key used for idempotency.
- `source_fingerprint`: producer item fingerprint used to detect material changes.
- `sweep_path`: local chat memory sweep JSON path.
- `sweep_id`: stable chat sweep id.
- `sweep_issue_id`: stable issue id inside a chat sweep.
- `issue_title`: title from a chat memory sweep issue.
- `issue_source`: original issue source such as cron, delivery, crawler, or bridge.
- `provider`: scanner provider or memory owner that produced the item.
- `workspace`: chat workspace or source workspace.
- `channel`: chat channel or surface name.
- `thread`: thread id, message range, or export locator.
- `message_range`: local message range or export range.
- `confidence`: producer confidence such as `low`, `medium`, or `high`.
- `evidence_summary`: compact evidence summary, not raw private chat text.
- `evidence`: local evidence path, not raw private chat text.
- `handoff_target_document`: optional explicit target document for `promote-handoff`, such as `TOOLS.md`, `USER.md`, `rules/scanner-imports.md`, or `.learnings/LEARNINGS.md`.

Scanner run provenance metadata is added by `brigade work scanners run --ingest-output`, and also attached to new inbox records that a scanner command writes directly during a run when Brigade can match the source:

- `scanner_id`: configured scanner id.
- `scanner_source`: configured scanner source.
- `scanner_run_id`: local scanner run receipt id.
- `scanner_receipt_path`: local receipt path under `.brigade/scanners/runs/`.
- `scanner_output_path_snapshot`: safe snapshot of configured output path after the run.
- `scanner_import_path`: configured JSONL import output path when present.
- `source_fingerprint`: producer fingerprint or Brigade-computed fingerprint for dedupe and dismissed-change checks.

Roadmap and repo-fleet producers use the same import contract:

- `source: roadmap-audit` records roadmap hygiene issues such as stale phase sections or documented command drift. Metadata includes `issue_type`, `safe_summary`, `source_item_key`, and `source_fingerprint`.
- `source: repo-fleet` records local repository readiness gaps from `brigade repos import-issues`. Metadata includes `repo_id`, `issue_type`, `safe_summary`, `source_item_key`, and `source_fingerprint`.
- `source: project-consolidation` records local project decision or readiness gaps from `brigade projects import-issues`. Metadata includes a safe project alias, `issue_type`, `safe_summary`, `source_item_key`, and `source_fingerprint`.
- `source: learning-loop` records bounded local learning candidates from `brigade learn import-issues`. Metadata includes candidate id, source subsystem, safe summary, `source_item_key`, and `source_fingerprint`.
- `source: learnings-import` records entries parsed from structured `.learnings/` markdown logs by `brigade learn import-learnings`. `ERR` entries import as `incident`, `LRN` entries as `finding`, and `FEAT` entries as `feature` tasks. Metadata includes `entry_id`, `entry_prefix`, `source_file`, `area`, redacted `safe_summary` and `safe_detail`, `source_item_key`, and `source_fingerprint`.
- `source: workflow-scan` records repeated command sequences from `brigade workflow scan --import-candidates`. Verify receipts contribute one observation per receipt from the full ordered `commands[].command` list, falling back to `shlex.join(argv)` when `command` is missing. Daily runs contribute one observation per run from the full ordered `commands_invoked` list. The scanner does not create per-command candidates and does not key candidates on failure status. Metadata includes `workflow_id`, `pattern_key`, `suggested_runbook_id`, `sources`, `review_risk`, `source_item_key`, and `source_fingerprint`. `source_item_key` is exactly the candidate id. `source_fingerprint` is the first 16 hex characters of the SHA-256 digest over canonical JSON containing only `id`, `sequence`, and `suggested_runbook_id`.

## Workflow Sequence Scanner Producer

Run:

```bash
brigade workflow scan --days 30 --min-count 2 --min-steps 1 --import-candidates
brigade workflow show
brigade workflow propose-runbook <candidate-id>
```

`workflow scan` writes `.brigade/workflow/latest.json` and `.brigade/workflow/latest.md` unless `--dry-run` is passed. `--json` prints the same scan payload. `--min-count` defaults to 2 and must be at least 1, so singleton sequences are excluded unless the operator opts in with `--min-count 1`. `--max-files` bounds receipt file reads per source.

Each candidate object has this exact schema:

```json
{
  "id": "workflow-<pattern_key>",
  "pattern_key": "<sha256 first 12 hex over joined normalized sequence templates>",
  "sequence": ["python3 -m pytest -q", "python3 -m ruff check ."],
  "example_commands": ["python3 -m pytest -q", "python3 -m ruff check ."],
  "occurrence_count": 2,
  "session_count": 2,
  "sources": ["verify"],
  "ends_in_verify_pass": true,
  "first_seen": "2026-06-11T12:00:00+00:00",
  "last_seen": "2026-06-11T13:00:00+00:00",
  "suggested_runbook_id": "workflow-<pattern_key>",
  "evidence": [],
  "review_risk": "normal",
  "suggested_next_command": "brigade workflow propose-runbook workflow-<pattern_key>"
}
```

`pattern_key` is computed from the normalized sequence templates joined with newline characters. Normalization replaces volatile tokens with fixed placeholders before hashing: run-id shapes become `<run-id>`, UUIDs and bare hex tokens of 12 or more characters become `<hex>`, ISO timestamps and dates become `<date>`, and absolute paths under `/tmp` or the user home become `<path>`. Repo-relative paths are kept as-is. The same workflow observed across runs that differ only by those tokens therefore produces the same `pattern_key`. The candidate `id` and `suggested_runbook_id` are both `workflow-<pattern_key>`. `sequence` holds the templates; `example_commands` holds the concrete commands from the most recent observation. `review_risk` is `high` when any example command matches the advisory destructive deny-list used by runbook policy, otherwise `normal`.

`workflow propose-runbook` resolves an exact candidate id or a unique prefix against `.brigade/workflow/latest.json`. It writes or prints a runbook whose id is `suggested_runbook_id`, whose description names the scan candidate and provenance, whose `approved` field is false, whose `pins` list is empty, whose `allowed_commands` are sorted command names from `example_commands`, and whose steps use concrete runnable commands from `example_commands` with `timeout_seconds` set to 600. The generated payload is validated against `brigade runbook plan` policy before anything is written; when validation fails, the command reports the policy failures, writes nothing, and exits 1.

Repo-fleet imports must use safe labels only. Do not copy full local paths, guidance file contents, private config values, raw logs, scanner output, owner names, exact private repo names, or raw evidence into import text or metadata.

## Privacy Rules

- Keep raw chat exports, message bodies, and third-party personal details out of public docs and public repos.
- Store source locators and summaries in metadata instead of raw message quotes.
- Route durable memory changes through reviewed Memory Handoffs. Do not let scanners edit memory cards directly by default.
- `promote-handoff` rejects raw private chat fields such as `raw_text`, `raw_messages`, `messages`, `message_text`, `quotes`, and `transcript`.
- `promote-handoff` redacts unsafe URLs, tokens, host-private paths, user ids, channel ids, hostnames, and secret-looking values before writing handoff drafts.

## Memory-Care And Memory-Refresh Producers

The memory-care and memory-refresh producers read this refresh queue by default:

```text
memory/cards/decay/refresh-queue.json
```

Minimal queue shape:

```json
{
  "cards": [
    {
      "file": "memory/cards/example.md",
      "reason": "source-of-truth changed"
    }
  ]
}
```

Run:

```bash
brigade work import memory-care
brigade work import memory-refresh
brigade work import triage
```

`memory-care` keeps the legacy source name `memory-care`. `brigade memory care scan` now produces this queue directly from local memory cards, and `brigade memory care import-issues` imports it into the work inbox. `memory-refresh` uses source `memory-refresh` and also accepts `candidates` or `refresh_candidates`.

Memory-refresh candidates can include `id` or `card_id`, `file`, `refresh_reason`, `issue_type`, `safe_summary`, `confidence`, `evidence_references`, `evidence_summary`, `suggested_refresh_action`, `source_item_key`, `source_fingerprint`, `priority`, `template`, and `acceptance`. The producer writes `task` imports preserving card identity, refresh reason, queue path, safe summary, evidence summary, source item key, and source fingerprint metadata.

Memory-care issue types are `stale`, `expired`, `undersourced`, `contradictory`, `missing-index-link`, `orphaned-card`, `oversized-card`, and `missing-frontmatter`. Memory-care imports are review items only. Brigade does not edit memory cards automatically.

## Chat Memory Sweep Producer

The chat-sweep producer reads this summary by default:

```text
.brigade/chat-memory-sweeps/latest.json
```

Minimal sweep shape:

```json
{
  "generated_at": "2026-05-26T22:09:00-04:00",
  "sessions": {
    "listed": 24,
    "reviewed": 10,
    "durable": 1
  },
  "issues": [
    {
      "id": "sweep-issue-1",
      "title": "Cron delivery failure",
      "summary": "Recent message delivery failed.",
      "kind": "incident",
      "source": "cron",
      "severity": "warning",
      "metadata": {
        "surface": "discord",
        "local_locator": "crawler://discord/example"
      }
    }
  ]
}
```

Run:

```bash
brigade work import chat-sweep
brigade work import triage
```

The producer writes imports with source `chat-memory-sweep`, preserving local locators and summary metadata. If an issue has `actionable: true`, `task: true`, or `kind: "task"`, Brigade writes a `task` import with task metadata and acceptance criteria. The JSON output reports `created`, `skipped`, `dismissed`, and `invalid` counts for wrappers.

The producer omits raw private fields such as `raw_text`, `raw_messages`, `messages`, `message_text`, `quotes`, and `transcript`. Use `summary`, `evidence_summary`, and local evidence locators instead of copying private chat bodies into the inbox.

## Chat Surface Export Producer

Local chat surface exports are configured in:

```text
.brigade/chat-surfaces.toml
```

Run:

```bash
brigade chat surfaces init
brigade chat sweep validate .brigade/chat-surfaces/discord-export.json
brigade chat sweep ingest discord-export
brigade chat sweep import-issues discord-export
```

Each export finding must provide `provider`, `surface_id`, `issue_id`, `issue_type`, `priority`, `confidence`, `safe_summary`, `evidence_summary`, `suggested_task_text`, and `acceptance_criteria`. Supported provider families are `discord-export`, `slack-export`, `telegram-export`, `clickclack-export`, and `generic-jsonl`; aliases such as `discord`, `slack-json`, `telegram`, `clickclack`, `generic`, and `jsonl` are normalized to those canonical families.

`ingest` writes normalized sweep JSON under `.brigade/chat-memory-sweeps/`, and `import-issues` routes actionable items through the existing source `chat-memory-sweep` import path. Raw private chat fields such as `raw_text`, `raw_messages`, `message_text`, `messages`, and `transcript` are rejected by default. Use safe summaries, channel labels, message ranges, confidence, and local evidence paths instead.
