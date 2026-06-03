# aicrawl Adapter

`aicrawl` remains a source family for Logspine, not a dependency.

No local aicrawl JSONL export shape was available beyond the existing `logspine.adapter.v1` fixture in `testdata/adapters/aicrawl.fixture.jsonl`. Native import should wait for a real redacted aicrawl export sample rather than guessing a schema.

Expected adapter export shape:

```json
{
  "schema": "logspine.adapter.v1",
  "source": {
    "kind": "aicrawl",
    "name": "aicrawl",
    "version": "unknown"
  },
  "collection": {
    "external_id": "aicrawl:chat:<chat-id>",
    "kind": "ai_chat_export",
    "name": "<chat title>"
  },
  "item": {
    "external_id": "aicrawl:message:<message-id>",
    "kind": "message",
    "created_at": "2026-06-03T00:00:00Z",
    "text": "chat message text",
    "tags": ["aicrawl"],
    "metadata": {
      "harness": "aicrawl",
      "conversation_id": "<conversation-id>",
      "model": "<model-if-known>"
    }
  },
  "actor": {
    "external_id": "aicrawl:actor:<role>",
    "type": "human",
    "name": "<role>"
  },
  "artifacts": [],
  "links": [],
  "relations": [],
  "raw": {
    "format": "json",
    "hash": "sha256:<raw-line-hash>",
    "path": "<source-export-path>",
    "ordinal": 1
  }
}
```

Native adapter unblock criteria:

- Provide a redacted aicrawl export fixture.
- Identify whether the source is JSONL, JSON, SQLite, or another local archive format.
- Map conversation IDs, message IDs, roles, timestamps, model metadata, attachments, and raw references.
