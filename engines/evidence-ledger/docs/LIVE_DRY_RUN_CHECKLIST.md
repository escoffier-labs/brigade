# Live Dry-Run Checklist

Use this checklist before importing private local session logs. The commands report roots, counts, structural status, and warnings without printing transcript text.

## Built-In Session Crawl

```bash
brigade-evidence sources discover --json
brigade-evidence crawl sessions --dry-run --json
brigade-evidence crawl cursor --dry-run --json
```

For one source root, use explicit native dry-runs:

```bash
brigade-evidence import codex ~/.codex/sessions --dry-run --json
brigade-evidence import claude ~/.claude/projects --dry-run --json
brigade-evidence import openclaw ~/.openclaw/agents --dry-run --json
brigade-evidence import opencode ~/.local/share/opencode --dry-run --json
brigade-evidence import hermes ~/.hermes/sessions --dry-run --json
brigade-evidence import grok ~/.grok/sessions --dry-run --json
```

## brigade-evidence Native Scanners

```bash
brigade-evidence sources discover --json
brigade-evidence import codex ~/.codex/sessions --dry-run --json
brigade-evidence import claude ~/.claude/projects --dry-run --json
brigade-evidence import openclaw ~/.openclaw/agents --dry-run --json
brigade-evidence import hermes ~/.hermes/sessions --dry-run --json
brigade-evidence import cursor ~/.config/Cursor/User --dry-run --json
brigade-evidence import grok ~/.grok/sessions --dry-run --json
brigade-evidence import discovered --dry-run --json
```

Expected output:

- candidate roots and file counts
- generated record counts
- warnings for malformed or unsupported records
- scan file metadata such as path, size, mtime, content hash, record count, and warning count

Do not paste private transcript content into issues or docs. If parser work needs samples, create redacted fixtures with representative structure and synthetic text.

## External Crawler Dry-Runs

Use narrow external dry-runs before importing provider data:

```bash
brigade-evidence crawl github --repo escoffier-labs/brigade-evidence --numbers 34 --limit 1 --dry-run --json
brigade-evidence crawl gmail --account "$GMAIL_ACCOUNT" --query "subject:brigade-evidence" --limit 1 --metadata-only --dry-run --json
scripts/smoke_gmail_metadata.sh
```

Gmail account selection stays explicit. `scripts/smoke_gmail_metadata.sh` reads the first configured Gog account and runs a metadata-only dry-run with `MISELEDGER_GMAIL_SMOKE_QUERY`, defaulting to `subject:brigade-evidence`. It does not import mail.

## Safe Import

After dry-runs look sane:

```bash
brigade-evidence import discovered --json
brigade-evidence stats --json
brigade-evidence relations backfill --json
brigade-evidence evidence "known safe fixture phrase" --json
```

Use `brigade-evidence scans list --json` to confirm what files were seen. Use `brigade-evidence scans changed --json` before scheduled or repeated imports.
