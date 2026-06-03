# Schema

The MVP uses one SQLite migration with these concepts:

- `sources`: source tools such as `discrawl`, `codex`, or `aicrawl`
- `collections`: bounded containers such as channels, exports, sessions, and repos
- `actors`: humans, assistants, agents, tools, bots, and systems
- `items`: atomic records such as messages, decisions, tool calls, errors, and notes
- `events`: timestamped occurrences tying source, collection, actor, and item together
- `artifacts`: files, URLs, markdown exports, patches, transcripts, and generated output
- `relations`: graph edges between items
- `imports` and `import_warnings`: import run metadata
- `item_tags`: indexed item tags from adapter records
- `item_metadata`: indexed project, workspace, harness, event type, session, model, and file-path metadata
- `source_scans`: source-file scan manifests for native imports
- `item_fts`: SQLite FTS5 index for item and artifact text

Raw adapter lines are preserved in `items.raw_json`. Raw source references are stored in `raw_hash`, `raw_path`, and `raw_ordinal`.

The migration lives in `internal/archive/db.go`.
