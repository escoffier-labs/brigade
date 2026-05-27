# Brigade Work Import JSONL Schema

Brigade work imports are the local handoff contract for scanners, wrappers, and chat adapters that discover candidate work. Producers write JSON Lines files, then an operator or wrapper validates and ingests them with `brigade work import`.

Imports are local review items. They do not write canonical memory directly.

## Commands

```bash
brigade work import validate imports.jsonl
brigade work import ingest imports.jsonl
brigade work import memory-care
brigade work import triage
brigade work import promote --all --source memory-care --kind task
```

`validate` checks a JSONL file without writing. `ingest` appends valid records into `.brigade/work/imports/inbox.jsonl`, skipping duplicate pending records with the same source, kind, and normalized text. `memory-care` reads `memory/cards/decay/refresh-queue.json` and converts queued cards into task imports.

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

Recommended metadata keys:

- `card_file`: memory card path for memory-care records.
- `reason`: short reason the item was produced.
- `workspace`: chat workspace or source workspace.
- `channel`: chat channel or surface name.
- `thread`: thread id, message range, or export locator.
- `confidence`: producer confidence such as `low`, `medium`, or `high`.
- `evidence`: local evidence path or compact summary, not raw private chat text.

## Privacy Rules

- Keep raw chat exports, message bodies, and third-party personal details out of public docs and public repos.
- Store source locators and summaries in metadata instead of raw message quotes.
- Route durable memory changes through reviewed Memory Handoffs. Do not let scanners edit memory cards directly by default.

## Memory-Care Producer

The memory-care producer reads this refresh queue by default:

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
brigade work import triage
```

The producer writes `task` imports with source `memory-care`, preserving `card_file`, `reason`, and `queue_path` metadata.
