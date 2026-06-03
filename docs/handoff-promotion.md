# Reviewed Handoff Promotion

`brigade work import promote-handoff` turns a reviewed durable scanner import into a local Memory Handoff draft. It is for non-task imports that should become durable knowledge after review, not for work execution.

## Commands

```bash
brigade work import plan-handoff <import-id>
brigade work import plan-handoff <import-id> --json
brigade work import promote-handoff <import-id>
brigade work import promote-handoff <import-id> --json
brigade handoff list
brigade handoff show <handoff-id-or-path>
brigade handoff archive <handoff-id-or-path>
brigade handoff archive --all-reviewed
brigade handoff runs
brigade handoff run-show <run-id>
brigade handoff reconcile
```

`plan-handoff` previews the handoff target, type, local inbox, provenance, and blockers. `promote-handoff` writes the draft, runs handoff lint, and only then marks the import promoted.

## Supported Imports

Handoff promotion is for durable non-task import kinds:

- `decision`
- `preference`
- `link`
- `command`
- `finding`
- `incident`

Task imports still use `brigade work import promote` and `brigade work import promote --run`.

## Target Documents

Brigade picks a no-card handoff target from kind and metadata:

- preferences go to `USER.md`
- commands and operational notes go to `TOOLS.md`
- incidents and failures go to `.learnings/ERRORS.md`
- feature requests go to `.learnings/FEATURE_REQUESTS.md`
- workflow rules go to `rules/scanner-imports.md`
- other durable lessons go to `.learnings/LEARNINGS.md`

Scanner metadata can set `handoff_target_document` to a valid document target when the default is too broad.

## Privacy Boundary

Handoff promotion writes only a local draft under the configured handoff inbox, usually `.codex/memory-handoffs/`, `.claude/memory-handoffs/`, `.opencode/memory-handoffs/`, or `.hermes/memory-handoffs/`. It does not edit `MEMORY.md`, memory cards, or canonical memory, and it does not run the ingestor.

Raw private chat fields are rejected by default, including `raw_text`, `raw_messages`, `messages`, `message_text`, `quotes`, and `transcript`. Unsafe URLs, tokens, host-private paths, user ids, channel ids, hostnames, and secret-looking values are redacted before the draft is written.

Promoted imports preserve the local handoff path, target document, promotion timestamp, and source fingerprint for review.

## Draft Queue Review

`brigade handoff list` discovers pending drafts from `.claude/memory-handoffs/`, `.codex/memory-handoffs/`, `.opencode/memory-handoffs/`, `.hermes/memory-handoffs/`, and inboxes declared in `.brigade/handoff-sources.json`. Each draft summary includes:

- filename id and path
- created and modified timestamps
- lint status and recommended memory action
- target card or target document
- source import id and source fingerprint when present
- scanner provenance such as scanner id, run id, and sweep id
- stale age and source coverage status

Before ingesting into canonical memory, run:

```bash
brigade handoff lint --content-guard --guard-policy personal --target .
```

The intended memory-owner rule is: Brigade lint passes, Content Guard passes when configured, and the operator has reviewed the note. Only then should OpenClaw, Hermes, or another owner ingest the handoff into durable memory.

`brigade handoff show <handoff-id-or-path>` prints the same detail for one draft. `brigade handoff archive <handoff-id-or-path>` moves one draft into `.brigade/handoffs/archive/` and appends a closeout record under `.brigade/handoffs/archive.jsonl`. `brigade handoff archive --all-reviewed` archives lint-valid drafts only, leaving invalid drafts in place for repair.

The draft queue is review visibility only. Brigade does not run the canonical ingestor, route the draft into canonical memory, edit `MEMORY.md`, or mutate memory cards.

## Ingestion Receipt Visibility

Canonical memory ingestion stays outside Brigade, but Brigade can read local receipt summaries that the ingestor or wrapper leaves behind under `.brigade/handoffs/ingest-runs/`. A receipt records the run id, timestamps, source root, inbox paths, processed handoff paths, promoted card targets, routed document targets, skipped paths, failed paths, warning count, safe summary, and local log path.

`brigade handoff runs` lists those receipts. `brigade handoff run-show <run-id>` shows the full normalized receipt and outcome summary. `brigade handoff list` and `brigade handoff show` include the latest known ingestion status for each draft when a receipt references it.

`brigade handoff reconcile` reads the configured `ingestor.last_run_log`, parses recognizable processed, promoted, routed, skipped, failed, malformed, unreachable-source, warning, and no-reply lines, then writes a normalized receipt locally. Reconcile may update `.brigade/handoffs/archive.jsonl` with known ingestion outcome fields while preserving manual archive reasons.

Normalized receipts preserve warning events, malformed handoff paths, unreachable source labels, and no-reply state so operators can see when a clean-looking scheduler run still hid skipped, failed, or unreachable work.

Source coverage issues for uncovered writer inboxes include stable source keys and fingerprints. If an operator dismisses an unchanged uncovered-inbox import, Brigade does not recreate it; when the pending inbox coverage state changes, the source fingerprint changes and the repair import can resurface.

This is observability only. Brigade does not run the ingestor, edit canonical memory, promote cards, route documents, or summarize draft content.
