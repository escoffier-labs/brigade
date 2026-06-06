# Hermes Adapter

Hermes is an agent-session source family for Logspine.

Logspine natively supports local Hermes `session_*.json` snapshots and trajectory JSONL under `~/.hermes/sessions`:

```bash
spine adapter hermes ~/.hermes/sessions --out -
spine import hermes ~/.hermes/sessions --json
```

StationTrail can still export the same records through the shared adapter contract:

```bash
stationtrail hermes ~/.hermes/sessions --out - | spine import adapter -
spine import stationtrail hermes ~/.hermes/sessions --json
stationtrail all --out - --redact paths,secrets | spine import adapter -
```

Hermes `state.db` remains an observed storage surface, but Logspine does not parse it directly. Native support is intentionally limited to readable snapshot and trajectory files with stable JSON shapes.

Expected adapter export shape:

```json
{
  "schema": "logspine.adapter.v1",
  "source": {
    "kind": "hermes",
    "name": "Hermes Sessions"
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
      "model": "<model>",
      "platform": "<platform>",
      "file_path": "<source-file>",
      "ordinal": 1
    }
  },
  "actor": {
    "external_id": "hermes:assistant:assistant",
    "type": "assistant",
    "name": "assistant"
  },
  "artifacts": [],
  "links": [],
  "relations": [],
  "raw": {
    "format": "json",
    "hash": "sha256:<raw-message-hash>",
    "path": "<source-json-or-jsonl-path>",
    "ordinal": 1
  }
}
```

Direct `state.db` adapter criteria, if this is ever needed:

- Provide redacted Hermes SQLite samples.
- Identify timestamp, session ID, actor role, event type, message/tool/artifact fields, and raw source references.
- Justify coupling Logspine to that SQLite storage surface.
