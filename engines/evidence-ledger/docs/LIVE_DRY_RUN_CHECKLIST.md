# Live Dry-Run Checklist

Use this checklist before importing private local session logs. The commands report roots, counts, structural status, and warnings without printing transcript text.

## StationTrail

```bash
stationtrail discover --json
stationtrail doctor --json
stationtrail doctor --live --json
stationtrail codex ~/.codex/sessions --dry-run --json
stationtrail claude ~/.claude/projects --dry-run --json
stationtrail openclaw ~/.openclaw/agents --dry-run --json
stationtrail hermes ~/.hermes/sessions --dry-run --json
```

For OpenCode, use an explicit sanitized export path or session ID:

```bash
stationtrail opencode <export-json|dir|session-id> --dry-run --json
```

## MiseLedger Native Scanners

```bash
miseledger sources discover --json
miseledger import codex ~/.codex/sessions --dry-run --json
miseledger import claude ~/.claude/projects --dry-run --json
miseledger import openclaw ~/.openclaw/agents --dry-run --json
miseledger import hermes ~/.hermes/sessions --dry-run --json
miseledger import discovered --dry-run --json
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
miseledger import discovered --json
miseledger stats --json
miseledger relations backfill --json
miseledger evidence "known safe fixture phrase" --json
```

Use `miseledger scans list --json` to confirm what files were seen. Use `miseledger scans changed --json` before scheduled or repeated imports.

