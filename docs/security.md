# Security Scanner

Brigade includes a read-only local security scanner for agent workspaces. It is designed to produce redacted findings that can be reviewed, suppressed, or imported into the local work inbox.

![Security scans become reviewable local work](assets/security-flow.svg)

## Content Guard

Content Guard is Brigade's publish and memory-safety scanner. Brigade shells out to the local scanner instead of importing it as a library, so the boundary stays explicit.

Use it in three places:

- `brigade handoff lint --content-guard --guard-policy personal` checks pending handoffs before memory ingest.
- `brigade handoff draft --guard --guard-policy personal ...` writes a draft and returns failure if Content Guard blocks it.
- `brigade work import content-guard --policy public-repo` runs a scan and turns blocking findings into reviewable work imports.

`brigade operator status` and `brigade operator doctor` report whether Content Guard is installed, the expected policy, the active pre-push hook path, the hook mode, suggested repair commands, and the latest local scan summary when available.

Policy guidance:

- `personal`: local/internal working notes and memory handoffs.
- `public-repo`: code and docs before push.
- `public-content`: stricter checks for blog, social, site copy, and other user-facing content.
- `strict`: high-sensitivity review where false positives are acceptable.

The memory-owner boundary is: ingest only handoffs that pass Brigade lint, Content Guard when configured, and human review. OpenClaw or Hermes should treat raw handoff inbox files as pending review, not permanent memory.

## Local Config

`brigade security init` writes `.brigade/security.toml`. The file is host-local and should stay gitignored.

Supported fields:

- `policy`: `personal`, `public-repo`, `ci`, or `strict`.
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
brigade security template-audit
brigade security show <finding-id>
brigade security suppress <finding-id-or-fingerprint> --reason "reviewed false positive"
brigade security unsuppress <finding-id-or-fingerprint>
brigade security doctor
```

Findings include stable `id`, `fingerprint`, `rule_id`, `severity`, `category`, `path`, `line`, `safe_excerpt`, and `remediation_hint` fields. Secret-looking values are redacted before JSON reports, Markdown reports, SARIF, work imports, docs, or session artifacts are written.

Security scans write `security-report.sarif` next to the JSON and Markdown reports. `brigade security sarif` can regenerate that SARIF file from an existing local evidence bundle without rescanning.

`brigade security template-audit` is a focused public artifact audit. It scans `src/brigade/templates`, `templates`, and `docs` for private paths, private-looking URLs, and secret-looking values, while allowing placeholders, reserved example domains, loopback examples, template variables, and environment labels. The audit is read-only and its summary is included in `brigade security doctor` and release readiness evidence.

Guardrail surfaces distinguish repo guidance, Claude command files, Codex skills, subagents, and tool wrappers. Public template findings keep `confidence: template`, while active workspace guidance and wrapper files report runtime confidence.

Security closeouts include policy-pack evidence: policy name, fail threshold, template inclusion, blocker count, warning count, and whether open findings were accepted as local risk. Release readiness and release candidates include the latest closeout.

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
