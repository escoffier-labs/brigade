# Hermes Adapter

Hermes should be treated as an agent-session source family for Logspine, but native import is currently blocked on observed session samples.

Local inspection found a `.hermes` directory with config and plugin files, but no readable Hermes JSONL session logs. Because Logspine tests must use fixtures and not private live transcripts, the native Hermes parser should wait for a redacted sample.

Expected adapter export shape:

```json
{
  "schema": "logspine.adapter.v1",
  "source": {
    "kind": "hermes",
    "name": "Hermes",
    "version": "unknown"
  },
  "collection": {
    "external_id": "hermes:session:<session-id>",
    "kind": "agent_session",
    "name": "<session-id>"
  },
  "item": {
    "external_id": "hermes:<deterministic-id>",
    "kind": "message",
    "created_at": "2026-06-03T00:00:00Z",
    "text": "searchable event text",
    "tags": ["agent-session", "hermes"],
    "metadata": {
      "harness": "hermes",
      "event_type": "<event-type>",
      "session_id": "<session-id>",
      "run_id": "<run-id>",
      "workspace_dir": "<workspace>"
    }
  },
  "actor": {
    "external_id": "hermes:assistant",
    "type": "assistant",
    "name": "assistant"
  },
  "artifacts": [],
  "links": [],
  "relations": [],
  "raw": {
    "format": "json",
    "hash": "sha256:<raw-line-hash>",
    "path": "<source-jsonl-path>",
    "ordinal": 1
  }
}
```

Native adapter unblock criteria:

- Provide a redacted Hermes session JSONL fixture.
- Identify timestamp, session ID, actor role, event type, message/tool/artifact fields, and raw source references.
- Confirm whether Hermes logs are JSONL, JSON arrays, SQLite, or another local format.
