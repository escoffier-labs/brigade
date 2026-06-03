# Hermes Adapter

Hermes is an agent-session source family for Logspine through the AgentTrail external scanner.

AgentTrail supports local Hermes `session_*.json` snapshots and trajectory JSONL under `~/.hermes/sessions`. Logspine should import those through the shared adapter contract:

```bash
agenttrail hermes ~/.hermes/sessions --out - | spine import adapter -
spine import agenttrail hermes ~/.hermes/sessions --json
agenttrail all --out - --redact paths,secrets | spine import adapter -
```

Hermes `state.db` remains an observed storage surface, but Logspine does not parse it natively. Keep source-specific Hermes parsing in AgentTrail unless there is a strong reason to duplicate it.

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

Native Logspine adapter criteria, if this is ever needed:

- Provide redacted Hermes session fixtures not already covered by AgentTrail.
- Identify timestamp, session ID, actor role, event type, message/tool/artifact fields, and raw source references.
- Justify duplicating AgentTrail behavior inside Logspine.
