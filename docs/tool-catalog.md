# Brigade Tool Catalog

`brigade tools` describes local callable tools, slash commands, skills, superpowers, scripts, and MCP configs across agent harnesses. It inspects local files and reports health, but it does not invoke tools, start MCP servers, sync harness configs, write projection files, fetch schemas, or store auth.

The local config is gitignored:

```text
.brigade/tools.toml
```

Create it with:

```bash
brigade tools init
```

## Commands

```bash
brigade tools list
brigade tools list --json
brigade tools show simplify
brigade tools search simplify
brigade tools doctor
brigade tools doctor --json
brigade tools import-issues
```

`list`, `show`, and `search` inspect configured entries. `doctor` reports catalog health issues. `import-issues` writes those issues into the normal work import inbox as `tool-catalog` task imports with stable source fingerprints.

## Config Shape

Each logical tool is a TOML table:

```toml
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
manifest_path = "tools/simplify.manifest.json"
schema_path = "tools/simplify.schema.json"
command = "brigade tools show simplify"
auth_label = "local-user"
timeout = 30
supported_harnesses = ["claude", "codex", "opencode"]
projections = { claude = ".claude/commands/simplify.md", codex = ".codex/skills/simplify/SKILL.md" }
projection_fingerprints = { claude = "abc123" }
health_path = ".brigade/tools/simplify-health.json"
fingerprint = "source-fingerprint"
```

Fields:

- `id`: stable logical tool id.
- `name`: display name.
- `family`: one of `skill`, `slash-command`, `superpower`, `mcp`, `openapi`, `graphql`, `script`, or `custom`.
- `enabled`: true or false.
- `description`: safe short description for humans and wrappers.
- `source_path`: local source file for the portable entry.
- `manifest_path`: optional local manifest path.
- `schema_path`: optional local JSON schema or tool schema path.
- `command`: optional command label. Required for `script` and `custom` entries.
- `auth_label`: safe label only, such as `local-user` or `github-readonly`.
- `timeout`: expected timeout in seconds.
- `supported_harnesses`: configured harnesses that should have projections.
- `projections`: per-harness projection target paths.
- `projection_fingerprints`: optional expected fingerprints for projection drift checks.
- `health_path`: optional local health summary file used for stale-health checks.
- `fingerprint`: optional source fingerprint when the source file is generated elsewhere.

Supported harness labels are local conventions. Brigade recognizes Claude Code, Codex, OpenCode, Hermes, OpenClaw, MCP, and scripts through the labels `claude`, `codex`, `opencode`, `hermes`, `openclaw`, `mcp`, and `scripts`.

## Health Checks

`brigade tools doctor` reports:

- missing source, manifest, schema, projection, or health files
- invalid schema JSON
- missing required script or custom commands
- command labels that do not resolve on the current host
- high-risk command shapes such as shell pipes into `sh`, `bash -c`, `sudo`, or `rm -rf`
- parity gaps where a supported harness lacks a projection target
- stale projection files when stored fingerprints do not match the local file
- stale health files
- unsafe auth field names in the local config
- MCP config issues in local JSON files with `mcpServers`

MCP discovery is structural only. Brigade summarizes server count and server ids, checks for missing commands and timeout metadata, and flags broad shell-like command shapes. It never starts an MCP server.

## Work Inbox Routing

`brigade tools import-issues` creates local work imports with:

- `source = "tool-catalog"`
- logical tool id
- source family
- issue type
- harness and projection target when relevant
- safe issue detail
- stable source item key and fingerprint

Repeated imports dedupe equivalent pending or promoted issues. Dismissed tool-catalog imports stay dismissed until the issue fingerprint changes.

## Privacy Boundary

Keep all catalog state local and gitignored. Do not put tokens, passwords, raw credentials, URLs with embedded secrets, private hostnames, or host-private paths in public templates. Brigade reports unsafe field names without copying their values into command output, work imports, session artifacts, docs, or handoffs.
