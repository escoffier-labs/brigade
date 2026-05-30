# Security Scanner

Brigade includes a read-only local security scanner for agent workspaces. It is designed to produce redacted findings that can be reviewed, suppressed, or imported into the local work inbox.

## Local Config

`brigade security init` writes `.brigade/security.toml`. The file is host-local and should stay gitignored.

Supported fields:

- `policy`: `personal`, `public-repo`, or `strict`.
- `scan_profile`: `public-repo`, `internal-workspace`, or `local-only-audit`.
- `fail_on`: `none`, `low`, `medium`, `high`, or `critical`.
- `include_templates`: whether public template files are scanned.
- `enabled_checks`: any of `automation`, `mcp`, `permissions`, `prompt-injection`, `secrets`, and `supply-chain`.
- `include_paths` and `exclude_paths`: relative path prefixes.
- `severity_threshold`: minimum severity retained in reports.
- `output_path`: relative path for the latest local evidence bundle.
- `[suppressions]` and `[suppression_reasons]`: reviewed finding fingerprints and reasons.

Keep tokens, private URLs, hostnames, mount paths, repo paths, and credentials out of this config. Use labels or local paths only when they are safe to expose in local command output.

## Review Flow

```bash
brigade security scan --target .
brigade security findings
brigade security sarif
brigade security show <finding-id>
brigade security suppress <finding-id-or-fingerprint> --reason "reviewed false positive"
brigade security unsuppress <finding-id-or-fingerprint>
brigade security doctor
```

Findings include stable `id`, `fingerprint`, `rule_id`, `severity`, `category`, `path`, `line`, `safe_excerpt`, and `remediation_hint` fields. Secret-looking values are redacted before JSON reports, Markdown reports, SARIF, work imports, docs, or session artifacts are written.

Security scans write `security-report.sarif` next to the JSON and Markdown reports. `brigade security sarif` can regenerate that SARIF file from an existing local evidence bundle without rescanning.

Guardrail surfaces distinguish repo guidance, Claude command files, Codex skills, subagents, and tool wrappers. Public template findings keep `confidence: template`, while active workspace guidance and wrapper files report runtime confidence.

## Inbox Flow

`brigade security scan --import-findings` writes the local evidence bundle and imports unsuppressed findings into the existing work inbox with source `security-scan`.

Imported records preserve safe metadata:

- finding id
- rule id
- severity and category
- path and line
- safe detail
- remediation hint
- local evidence path
- stable source key and fingerprint

Repeated scans dedupe equivalent pending findings. Dismissed imports stay dismissed until the finding materially changes.

## Boundaries

The scanner is local and read-only. It does not call external SaaS scanners, perform network scanning, store secrets, start a daemon, schedule scans, mutate GitHub issues, or remediate findings automatically.
