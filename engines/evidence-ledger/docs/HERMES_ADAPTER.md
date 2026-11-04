# Hermes Adapter

Hermes is an agent-session source family for brigade-evidence.

brigade-evidence natively supports local Hermes `session_*.json` snapshots and trajectory JSONL under `~/.hermes/sessions`:

```bash
brigade-evidence adapter hermes ~/.hermes/sessions --out -
brigade-evidence import hermes ~/.hermes/sessions --json
```

Archived StationTrail exports used the same shared adapter contract. If you already have one of those JSONL files, import it through the adapter path:

```bash
brigade-evidence import adapter hermes.adapter.jsonl --source hermes --json
```

Hermes `state.db` remains an observed storage surface, but brigade-evidence does not parse it directly. Native support is intentionally limited to readable snapshot and trajectory files with stable JSON shapes.

Expected adapter export shape:

```json
{
  "schema": "miseledger.adapter.v1",
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
- Justify coupling brigade-evidence to that SQLite storage surface.
