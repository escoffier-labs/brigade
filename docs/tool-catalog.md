# Brigade Tool Catalog

`brigade tools` describes local callable tools, slash commands, skills, superpowers, scripts, and MCP configs across agent harnesses. It inspects local files, reports health, and can explicitly write reviewed harness projection files. It does not invoke tools, start MCP servers, auto-sync harness configs, fetch schemas, or store auth.

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
brigade tools describe simplify
brigade tools contracts
brigade tools call plan simplify --args '{"path":"README.md"}'
brigade tools call plan simplify --args-json args.json
brigade tools plan
brigade tools plan simplify
brigade tools apply simplify --dry-run
brigade tools apply simplify
brigade tools apply --all
brigade tools doctor
brigade tools doctor --json
brigade tools import-issues
```

`list`, `show`, and `search` inspect configured entries. `describe` and `contracts` inspect schema-backed call contracts. `call plan` validates arguments and returns a safe call plan without executing anything. `plan` previews projection writes without touching files. `apply` is the only command that writes projections, and it requires either one tool id or `--all`. `doctor` reports catalog health issues. `import-issues` writes those issues into the normal work import inbox as `tool-catalog` task imports with stable source fingerprints.

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
input_schema_path = "tools/simplify.input.schema.json"
output_schema_path = "tools/simplify.output.schema.json"
examples_path = "tools/simplify.examples.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
cwd = "."
env_labels = ["SAFE_ENV"]
argument_template = { path = "{path}", mode = "--mode={mode}" }
supported_harnesses = ["claude", "codex", "opencode"]
projections = { claude = ".claude/commands/simplify.md", codex = ".codex/skills/simplify/SKILL.md" }
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
- `input_schema_path`: optional JSON Schema path for call arguments. Required for `call plan`.
- `output_schema_path`: optional JSON Schema path for wrapper output expectations.
- `examples_path`: optional local examples file.
- `permissions`: safe labels for needed capabilities.
- `effects`: safe labels for expected effects such as `local-read`.
- `approval_mode`: `never`, `on-request`, or `always`.
- `cwd`: optional local working directory label or relative path.
- `env_labels`: safe environment labels only, never values.
- `argument_template`: table of call-plan argument names to template strings such as `{path}`.
- `supported_harnesses`: configured harnesses that should have projections.
- `projections`: per-harness projection target paths.
- `health_path`: optional local health summary file used for stale-health checks.
- `fingerprint`: optional source fingerprint when the source file is generated elsewhere.

Supported harness labels are local conventions. Brigade recognizes Claude Code, Codex, OpenCode, Hermes, OpenClaw, MCP, and scripts through the labels `claude`, `codex`, `opencode`, `hermes`, `openclaw`, `mcp`, and `scripts`.

## Projection Planning And Apply

`brigade tools plan` expands configured `supported_harnesses` and `projections` into exact projection actions. Each projection reports:

- logical tool id
- source family
- harness
- source path
- projection path
- source fingerprint
- expected projection fingerprint
- status
- action

Projection statuses are:

- `missing`: target file is absent and can be created
- `current`: managed projection matches the current source
- `stale`: managed projection is safe to update from changed source or changed renderer output
- `unmanaged`: target exists without Brigade projection metadata
- `conflicted`: managed target has local edits
- `missing_source`: source file cannot be read

`brigade tools apply <tool-id>` and `brigade tools apply --all` write only `create` and `update` actions. `--dry-run` reports writes without touching files. `--force` is required to overwrite unmanaged files or managed projections with local edits. `doctor`, `brief`, `work run`, and `import-issues` never apply projections automatically.

Managed projection files start with a Brigade metadata header containing:

- logical tool id
- source family
- harness
- source fingerprint
- projection fingerprint
- generated-at timestamp

For `slash-command`, `skill`, and `superpower` entries, Brigade writes the source content behind that metadata header. For `script` entries, Brigade writes a safe reference projection with the command label and source excerpt. For `mcp` entries, Brigade writes a documentation stub only. It does not write runtime MCP server configs.

## Contracts And Call Planning

Contracts let wrappers understand how a portable tool should be called without invoking it. Brigade supports a practical local subset of JSON Schema:

- root object schemas
- `required`
- scalar types: `string`, `number`, `integer`, `boolean`, and `null`
- arrays with `items`
- enums
- boolean `additionalProperties`

Unsupported schema keywords are reported as contract health issues. `brigade tools call plan` accepts inline JSON with `--args` or a file with `--args-json`, validates it against `input_schema_path`, renders `argument_template`, and returns a redacted plan with:

- tool id, family, command label, cwd, timeout, auth label, and env labels
- rendered argument mapping
- permissions and effects
- approval mode and approval requirement
- blockers for invalid args, missing schemas, missing commands, unsafe labels, and conflicted or unmanaged projections

Call planning is read-only. It does not invoke the command, start daemons, start MCP servers, resolve approvals, fetch remote schemas, or store auth.

## Health Checks

`brigade tools doctor` reports:

- missing source, manifest, schema, projection, or health files
- invalid schema JSON
- invalid or unsupported contract schemas
- missing call contracts for command-backed tools
- missing examples
- bad argument templates
- missing required script or custom commands
- command labels that do not resolve on the current host
- high-risk command shapes such as shell pipes into `sh`, `bash -c`, `sudo`, or `rm -rf`
- parity gaps where a supported harness lacks a projection target
- missing, stale, unmanaged, or locally edited projection files
- stale health files
- unsafe auth or env field names in the local config
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

Projection apply is local and explicit. Call planning is local and read-only. Brigade does not invoke projected tools, install schedulers, start a daemon, fetch remote schemas, store auth, or mutate remote services.
