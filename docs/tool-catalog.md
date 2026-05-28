# Brigade Tool Catalog

`brigade tools` describes local callable tools, slash commands, skills, superpowers, scripts, and MCP configs across agent harnesses. It inspects local files, reports health, can explicitly write reviewed harness projection files, and can explicitly supervise local runtimes for approved script calls. It does not start MCP servers, auto-sync harness configs, fetch schemas, store auth, install schedulers, or auto-start runtimes.

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
brigade tools call queue simplify --args '{"path":"README.md"}'
brigade tools call queue simplify --args-json args.json
brigade tools call list
brigade tools call show <call-id>
brigade tools call approve <call-id>
brigade tools call reject <call-id> --reason "not needed"
brigade tools call hold <call-id> --reason "needs review"
brigade tools call run <call-id>
brigade tools call run --next
brigade tools runtime init
brigade tools runtime list
brigade tools runtime show <runtime-id>
brigade tools runtime status
brigade tools runtime start <runtime-id>
brigade tools runtime stop <runtime-id>
brigade tools runtime restart <runtime-id>
brigade tools runtime doctor
brigade tools policy init
brigade tools policy show
brigade tools policy doctor
brigade tools plan
brigade tools plan simplify
brigade tools apply simplify --dry-run
brigade tools apply simplify
brigade tools apply --all
brigade tools doctor
brigade tools doctor --json
brigade tools import-issues
```

`list`, `show`, and `search` inspect configured entries. `describe` and `contracts` inspect schema-backed call contracts. `call plan` validates arguments and returns a safe call plan without executing anything. `call queue` stores a plan for local review, and `call approve`, `reject`, and `hold` update review status only. `call run` explicitly executes approved script calls and writes local receipts. `runtime` commands explicitly manage local runtimes. `policy` commands inspect host-local execution gates and env label bindings. `plan` previews projection writes without touching files. `apply` is the only command that writes projections, and it requires either one tool id or `--all`. `doctor` reports catalog health issues. `import-issues` writes those issues into the normal work import inbox as `tool-catalog` task imports with stable source fingerprints.

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
runtime_id = "local-helper"
requires_runtime = false
runtime_health_path = ".brigade/tools/runtime/local-helper.json"
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
- `runtime_id`: optional id of a local runtime from `.brigade/tools/runtimes.toml`.
- `requires_runtime`: true when `call run` must refuse execution unless that runtime is running and healthy.
- `runtime_health_path`: optional local runtime health file label or path.
- `supported_harnesses`: configured harnesses that should have projections.
- `projections`: per-harness projection target paths.
- `health_path`: optional local health summary file used for stale-health checks.
- `fingerprint`: optional source fingerprint when the source file is generated elsewhere.

Supported harness labels are local conventions. Brigade recognizes Claude Code, Codex, OpenCode, Hermes, OpenClaw, MCP, and scripts through the labels `claude`, `codex`, `opencode`, `hermes`, `openclaw`, `mcp`, and `scripts`.

## Runtime Supervisor

Runtimes are explicit local processes that approved script calls can depend on. They are configured in a gitignored file:

```text
.brigade/tools/runtimes.toml
```

Create a starter config with:

```bash
brigade tools runtime init
```

Each runtime is a TOML table:

```toml
[[runtime]]
id = "local-helper"
name = "Local Helper"
enabled = true
command = "python3 -m http.server 8765"
cwd = "."
port = 8765
health_command = "python3 --version"
health_path = ".brigade/tools/runtime/local-helper.json"
pid_path = ".brigade/tools/runtime/local-helper.pid"
log_path = ".brigade/tools/runtime/local-helper.log"
timeout = 10
```

Runtime commands are local and explicit:

- `brigade tools runtime list` and `show <runtime-id>` inspect config and current process state.
- `brigade tools runtime status` reports running, stopped, and stale runtime states.
- `brigade tools runtime start <runtime-id>` starts one runtime with `shell=False`, writes PID metadata, and writes stdout/stderr logs under `.brigade/tools/runtime/`.
- `brigade tools runtime stop <runtime-id>` stops only a process with matching Brigade runtime metadata.
- `brigade tools runtime restart <runtime-id>` stops then starts the same runtime.
- `brigade tools runtime doctor` reports stale PID files, port conflicts, missing cwd, high-risk commands, failed health checks, and missing runtime config.

Brigade refuses high-risk runtime command shapes, detects already-running runtimes, detects stale PID files, and warns when configured ports are already in use. `doctor`, `brief`, and `work run` never start runtimes automatically.

When a tool has `requires_runtime = true`, `brigade tools call run` refuses execution unless the configured runtime exists, is running, is managed by Brigade metadata, and passes health checks. Receipts include the runtime id and runtime snapshot used for the run.

## Execution Policy

Execution policy is host-local and gitignored:

```text
.brigade/tools/policy.toml
```

Create a starter policy with:

```bash
brigade tools policy init
```

The policy shape is:

```toml
allowed_families = ["script"]
allowed_effects = ["local-read", "local-write"]
denied_effects = ["remote-mutation", "secret-read"]
required_approval_modes = ["on-request", "always"]
max_timeout = 60
allowed_runtimes = ["local-helper"]
env_bindings = { SAFE_ENV = "SAFE_ENV" }
```

Policy fields:

- `allowed_families`: optional allow-list for tool families that can run.
- `allowed_effects`: optional allow-list for effect labels.
- `denied_effects`: effect labels that always block planning and execution.
- `required_approval_modes`: allowed approval modes for executable calls.
- `max_timeout`: maximum allowed call timeout in seconds.
- `allowed_runtimes`: optional allow-list for runtime ids.
- `env_bindings`: maps safe env labels from tool config to process environment variable names.

When a policy file exists, `brigade tools call plan` and `brigade tools call run` report policy blockers for denied effects, effects outside the allow-list, disallowed families, missing env bindings, missing process env values, timeout caps, disallowed runtimes, and approval-mode mismatches. A missing policy is a health issue for executable contract-backed tools, but it does not break older read-only catalog workflows.

For env bindings, Brigade reads the current process environment at execution time and passes values to the script under the configured label name. It never stores env values in `.brigade/tools/calls.jsonl`, receipts, logs, docs, or work imports. Receipts include the policy decision and env labels used, with values redacted.

`brigade tools policy doctor` checks the policy file directly. `brigade tools doctor`, `brigade work brief`, and `brigade tools import-issues` also surface missing policy, missing env labels, denied effects, timeout caps, approval-mode blockers, and runtime/policy mismatches.

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

## Call Approval Queue

`brigade tools call queue` stores planned calls in:

```text
.brigade/tools/calls.jsonl
```

Each queued call stores:

- status: `pending`, `approved`, `rejected`, or `held`
- redacted args and rendered argument mapping
- contract metadata, approval mode, permissions, and effects
- blockers from call planning
- projection summary
- source and contract fingerprints
- created and reviewed timestamps
- review reason when rejected or held

Equivalent valid plans dedupe while pending or approved. Rejected calls can be requeued only when args or the contract fingerprint changes. Blocked plans require `--include-blocked` to enter the queue, and blocked calls cannot be approved. Approval never executes a tool.

`brigade tools doctor` warns on stale pending approvals, approved calls whose contract or source fingerprint has gone stale, blocked queued calls, and held or rejected calls. `brigade work brief` shows pending approval count and the highest-priority call approval issue. `brigade tools import-issues` routes call approval health problems into `tool-catalog` imports.

## Call Execution Receipts

`brigade tools call run <call-id>` executes exactly one approved call. `brigade tools call run --next` runs the oldest approved call. Execution is intentionally narrow:

- only `script` family calls run
- the call must be `approved`
- blockers must be empty
- approval, args, contract, source, and projection fingerprints must still match
- completed calls cannot be run again
- commands run with `shell=False`

For script calls, Brigade parses the configured `command`, appends rendered `argument_template` values in stable key order, uses the configured `cwd` when present, and applies the configured timeout. It does not resolve auth, fetch secrets, start MCP servers, run OpenAPI or GraphQL calls, or execute unapproved queue entries.

Each run writes a receipt and raw local logs under:

```text
.brigade/tools/runs/
```

Receipts include call id, tool id, status, timestamps, duration, exit code, timeout status, command label, cwd, redacted stdout/stderr summaries, stdout/stderr log paths, contract/source/call/approval fingerprints, approval metadata, permissions, effects, and projection summary. Raw stdout and stderr logs stay local and gitignored.

`brigade tools doctor` warns on failed executions and calls left running too long. `brigade work brief` surfaces the highest-priority execution issue, and `brigade tools import-issues` routes execution health problems into `tool-catalog` imports.

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
- stale or blocked tool call approvals
- approved tool calls with stale source or contract fingerprints
- failed tool call executions
- tool call executions left running too long
- missing, stopped, stale, unhealthy, or unmanaged required runtimes
- runtime config, cwd, command, PID, port, and health-check issues
- missing execution policy for executable contract-backed tools
- policy blockers for effects, timeouts, approval modes, runtimes, families, and env labels
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

Projection apply is local and explicit. Call planning and call approval review are local and non-executing. Tool execution is explicit through `brigade tools call run`, limited initially to approved local `script` entries, and recorded with local receipts. Runtime lifecycle is explicit through `brigade tools runtime`; no command auto-starts runtimes as a side effect. Execution policy is host-local and gitignored. Brigade does not store secrets, start MCP servers, run OpenAPI or GraphQL calls, install schedulers, fetch remote schemas, store auth, send notifications, or mutate remote services.
