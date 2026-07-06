# Retention Policy

MiseLedger can prune item-level evidence through an explicit policy pass:

```bash
miseledger prune --policy default --dry-run --json
miseledger prune --policy default --apply --export exports/pruned-2026-07-06.jsonl.gz --json
miseledger prune policy --policy retention.json --dry-run --json
```

Dry run is the default. A destructive run must include both `--apply` and `--export <path>`. Matched records are written to compressed adapter JSONL before any rows are deleted, so the slice can be restored later with `miseledger import adapter <file>`.

## Default Policy

The default policy targets old operational noise:

| Tier | Match | Action |
| --- | --- | --- |
| `default-operational-noise` | `tool_call`, `command`, `progress`, `status`, `event`, and `queue-operation` items older than 90 days | Export, then delete |

Messages, decisions, notes, issues, and pull requests are not matched by the default policy.

## Custom Policy

Custom policies are JSON files:

```json
{
  "name": "local-retention",
  "tiers": [
    {
      "name": "old-command-output",
      "source_kinds": ["codex", "claude"],
      "item_kinds": ["command", "tool_call"],
      "older_than_days": 120,
      "action": "delete"
    }
  ]
}
```

Fields:

- `name`: policy name reported in JSON output.
- `tiers[].name`: tier name reported in JSON output.
- `tiers[].source_kinds`: optional list of source kinds to match.
- `tiers[].item_kinds`: required list of item kinds to match.
- `tiers[].older_than_days`: required positive age threshold.
- `tiers[].action`: must be `delete`.

## Delete Semantics

Policy pruning deletes only matched items and dependent rows. It does not remove sources, collections, actors, imports, import warnings, or source scan manifests.

For each matched item, MiseLedger:

- exports the original adapter JSON line to gzip-compressed JSONL;
- deletes item tags, item metadata, events, artifacts, and FTS rows;
- deletes relations where the pruned item is the source;
- tombstones relations where the pruned item is the target by setting `target_item_id` to null while preserving `target_external_id`;
- deletes the item row;
- optimizes FTS and checkpoints the WAL.

Keeping scan manifests prevents routine re-crawls from resurrecting intentionally pruned source files.

## Restoring A Slice

```bash
miseledger import adapter exports/pruned-2026-07-06.jsonl.gz --json
```

Restored rows use the same adapter identity rules as any other import. If the original source files are still present, keep the scan manifests intact so normal crawls do not re-add pruned rows by accident.
