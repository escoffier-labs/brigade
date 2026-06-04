# Live Dry-Run Checklist

Use this checklist before importing private local session logs. The commands report roots, counts, structural status, and warnings without printing transcript text.

## AgentTrail

```bash
agenttrail discover --json
agenttrail doctor --json
agenttrail doctor --live --json
agenttrail codex ~/.codex/sessions --dry-run --json
agenttrail claude ~/.claude/projects --dry-run --json
agenttrail openclaw ~/.openclaw/agents --dry-run --json
agenttrail hermes ~/.hermes/sessions --dry-run --json
```

For OpenCode, use an explicit sanitized export path or session ID:

```bash
agenttrail opencode <export-json|dir|session-id> --dry-run --json
```

## Logspine Native Scanners

```bash
spine sources discover --json
spine import codex ~/.codex/sessions --dry-run --json
spine import claude ~/.claude/projects --dry-run --json
spine import openclaw ~/.openclaw/agents --dry-run --json
spine import hermes ~/.hermes/sessions --dry-run --json
spine import discovered --dry-run --json
```

Expected output:

- candidate roots and file counts
- generated record counts
- warnings for malformed or unsupported records
- scan file metadata such as path, size, mtime, content hash, record count, and warning count

Do not paste private transcript content into issues or docs. If parser work needs samples, create redacted fixtures with representative structure and synthetic text.

## Safe Import

After dry-runs look sane:

```bash
spine import discovered --json
spine stats --json
spine relations backfill --json
spine evidence "known safe fixture phrase" --json
```

Use `spine scans list --json` to confirm what files were seen. Use `spine scans changed --json` before scheduled or repeated imports.

