# Retention Policy Design

MiseLedger currently keeps normalized evidence items indefinitely. The existing `prune` commands only remove old import metadata and missing scan manifests; they do not delete items, events, artifacts, relations, or FTS rows.

This is intentional until item-level retention has three safety rails:

1. A dry-run report that names exactly which tiers match, how many rows would be affected, and which source kinds and item kinds dominate the total.
2. A cold export step that writes matched rows to compressed adapter JSONL before deletion, so a retained slice can be re-imported later.
3. Relation and scan-manifest rules that avoid accidental resurrection or dangling evidence.

## Default Tier Shape

A future `prune --policy <file|default>` should start with conservative defaults:

| Tier | Match | Default Action |
| --- | --- | --- |
| Fresh evidence | Any item younger than 90 days | Keep |
| Durable decisions | Messages, summaries, decisions, notes, issues, pull requests | Keep longer than operational noise |
| Operational noise | Tool calls, command output, progress events, status events | Eligible after the fresh window |
| Large raw artifacts | Artifact text or payloads above a configured byte threshold | Eligible before the parent item |

Policy files should be explicit about source kind, item kind, age, and action. No policy should infer that a kind is low-value just because it is frequent.

## Required Delete Semantics

Item deletion should happen in one transaction per policy application after the export succeeds. The operation should:

- delete or tombstone relations that reference pruned items, based on policy;
- remove FTS rows for deleted items;
- keep source scan manifests by default, so re-runs do not re-import files that were intentionally pruned;
- run checkpoint and FTS optimization after bulk deletion;
- report the export path, row counts, and reclaimed bytes.

## Open Decisions

- Whether relation rows should be tombstoned by default or dropped with deleted items.
- Whether policy should support summarization as a first-class action, or whether summarization belongs in a separate command that writes new adapter records before prune runs.
- Whether default policy should ever delete message-like evidence, or only operational noise.
