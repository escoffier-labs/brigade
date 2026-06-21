# Brigade Tool Catalog

`brigade tools` describes local callable tools, slash commands, skills, superpowers, scripts, and MCP configs across agent harnesses. It inspects local files, reports health, can explicitly write reviewed harness projection files, and can explicitly supervise local runtimes for approved script calls. It does not start MCP servers, fetch schemas, store auth, install schedulers, or auto-start runtimes. (Runtime MCP server configs are a separate concern, managed explicitly by `brigade mcp` - see [mcp-sync.md](mcp-sync.md).)

![Tool catalog separates discovery, projection, and execution](assets/tool-catalog-flow.svg)

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
brigade tools defaults
brigade tools defaults --json
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
brigade tools run list
brigade tools run show <run-id>
brigade tools run latest
brigade tools run replay <run-id>
brigade tools checkpoint list
brigade tools checkpoint show <checkpoint-id>
brigade tools checkpoint approve <checkpoint-id> --choice continue
brigade tools checkpoint reject <checkpoint-id> --reason "not safe"
brigade tools checkpoint resume <checkpoint-id>
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
brigade tools parity status
brigade tools parity closeout
brigade tools pack build
brigade tools pack import <pack-dir>
brigade tools pack list
brigade tools pack show latest
brigade tools plan
brigade tools plan simplify
brigade tools apply simplify --dry-run
brigade tools apply simplify
brigade tools apply --all
brigade tools doctor
brigade tools doctor --json
brigade tools import-issues
```

`defaults` merges the current Brigade built-in portable tools into the local catalog without deleting custom entries. `list`, `show`, and `search` inspect configured entries. `describe` and `contracts` inspect schema-backed call contracts. `call plan` validates arguments and returns a safe call plan without executing anything. `call queue` stores a plan for local review, and `call approve`, `reject`, and `hold` update review status only. `call run` explicitly executes approved script calls and writes local receipts. `run list`, `show`, and `latest` inspect receipts, and `run replay` queues a pending replay candidate without executing it. `checkpoint` commands review and explicitly resume local pause records. `runtime` commands explicitly manage local runtimes. `policy` commands inspect host-local execution gates and env label bindings. `parity status` and `parity closeout` review projection parity without writing projections. `plan` previews projection writes without touching files. `apply` is the only command that writes projections, and it requires either one tool id or `--all`. `doctor` reports catalog health issues. `import-issues` writes those issues into the normal work import inbox as `tool-catalog` task imports with stable source fingerprints.

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
supported_harnesses = ["claude", "codex", "opencode", "antigravity", "pi", "cursor"]
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

Supported harness labels are local conventions. Brigade recognizes Claude Code, Codex, OpenCode, Antigravity, Pi, Cursor, Hermes, OpenClaw, MCP, and scripts through the labels `claude`, `codex`, `opencode`, `antigravity`, `pi`, `cursor`, `hermes`, `openclaw`, `mcp`, and `scripts`.

## Built-In Defaults

`brigade tools defaults --target <repo>` merges the built-in `simplify`, `superpowers`, `frontend`, and `antislop` entries into `.brigade/tools.toml`. Existing custom entries are preserved. If a custom entry reuses a built-in id while pointing at a different `source_path`, Brigade reports a conflict instead of overwriting it; pass `--force` only when replacing that entry with the built-in definition is intentional.

`brigade operator sync-tools --target <repo>` runs this defaults merge before projection apply, so a repo with an older local catalog can pick up newly shipped built-ins on another machine. Generated projections stay local and gitignored; reviewed source files such as `tools/*.md` and the local catalog are the portable source of truth.

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

`brigade tools parity status` summarizes active projection parity issues, reviewed or deferred quieted issues, changed issue count, and the latest parity closeout. `brigade tools parity closeout` stores a local receipt under `.brigade/tools/parity-closeouts/` with safe counts, review status, reason, and parity fingerprints. A parity closeout can quiet unchanged missing, stale, unmanaged, conflicted, or parity-gap projection issues in `tools doctor`, `work brief`, and `tools import-issues`, but changed projection fingerprints resurface as active issues.

Managed projection files start with a Brigade metadata header containing:

- logical tool id
- source family
- harness
- source fingerprint
- projection fingerprint
- generated-at timestamp

For `slash-command`, `skill`, and `superpower` entries, Brigade writes the source content behind that metadata header. For `script` entries, Brigade writes a safe reference projection with the command label and source excerpt. For `mcp` entries in the tool catalog, Brigade writes a documentation stub only. Runtime MCP server configs are written by the separate `brigade mcp` subsystem (see [mcp-sync.md](mcp-sync.md)), not by tool projection.

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

For MCP calls, Brigade requires an approved `mcp` family call with `runtime_id`, `mcp_tool_name`, `input_schema_path`, timeout, permissions, effects, and approval mode. The configured runtime must already be running, managed by Brigade metadata, healthy, and allowed by policy. Brigade then runs the configured local stdio command with `shell=False`, sends JSON-RPC `initialize`, `tools/list`, and `tools/call`, validates that the requested MCP tool is listed, and records the redacted request and response in the receipt. It does not connect to remote MCP servers or start runtimes automatically.

Each run writes a receipt and raw local logs under:

```text
.brigade/tools/runs/
```

Receipts include call id, tool id, family, status, timestamps, duration, exit code, timeout status, command label, cwd, redacted args and rendered arguments, redacted stdout/stderr summaries, stdout/stderr log paths, contract/source/call/approval fingerprints, approval metadata, permissions, effects, runtime snapshot, policy decision, env labels used, and projection summary. MCP receipts also include server id, tool name, request id, redacted request payload, redacted response summary, and MCP response count. Raw stdout and stderr logs stay local and gitignored.

## Run History And Replay

Run history commands read only from `.brigade/tools/runs/`:

- `brigade tools run list` summarizes receipts by status.
- `brigade tools run show <run-id>` shows one receipt by id or unique prefix.
- `brigade tools run latest` shows the newest receipt.
- `brigade tools run replay <run-id>` creates a new pending call from the receipt's redacted args after revalidating the current tool contract, source fingerprint, projection summary, runtime requirement, and policy decision.

Replay never executes the command directly and never marks the replay call approved. Operators or wrappers must review and approve the new call through the normal approval queue, then run it with `brigade tools call run`. Replay uses only the redacted receipt args, so secret env values and secret-looking argument values are not recovered from logs or receipts.

`brigade tools doctor` warns on failed or timed-out runs, malformed receipts, missing stdout/stderr log files, and replay candidates blocked by stale tool, runtime, or policy state. `brigade work brief` surfaces the highest-priority run-history issue, and `brigade tools import-issues` routes run-history problems into `tool-catalog` imports.

## Execution Checkpoints

Approved script tools can request an operator checkpoint by writing a JSON file under the directory in `BRIGADE_TOOL_CHECKPOINT_DIR`. Brigade also passes `BRIGADE_TOOL_CALL_ID` and `BRIGADE_TOOL_RUN_ID` to the script so checkpoint writers can include the current call and run id.

Checkpoint JSON supports:

- `id`, optional; Brigade generates one when absent
- `reason`
- `requested_action`
- `prompt` or `operator_prompt`
- `context`, redacted before storage
- `choices` or `allowed_resume_choices`
- `created_at`, optional
- `expires_at`, optional

When a run writes a checkpoint, Brigade normalizes the file into `.brigade/tools/checkpoints/`, marks the call `waiting`, and writes a waiting receipt linked to the checkpoint. `brigade tools checkpoint approve <checkpoint-id> --choice <choice>` marks the checkpoint approved and moves the call to `resume-pending`. `brigade tools checkpoint resume <checkpoint-id>` then revalidates the call, contract, source, projection, runtime, and policy state before executing. Resume receipts include the original call id, original run id, checkpoint id, resume run id, selected choice, and checkpoint approval metadata.

Checkpoint approval does not execute anything. Checkpoint resume never runs automatically from `doctor`, `brief`, `work run`, or `call run`. Brigade redacts checkpoint context and does not store process env values in checkpoints, receipts, logs, imports, or docs.

`brigade tools doctor` warns on stale, expired, rejected, blocked, and failed checkpoints. `brigade work brief` surfaces the highest-priority checkpoint issue, and `brigade tools import-issues` routes checkpoint problems into `tool-catalog` imports.

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
- failed or timed-out run receipts
- failed MCP executions, malformed JSON-RPC responses, and MCP tool-list mismatches
- malformed run receipts and missing run log files
- run replay candidates blocked by current tool, runtime, or policy state
- stale, expired, rejected, blocked, or failed checkpoints
- missing, stopped, stale, unhealthy, or unmanaged required runtimes
- runtime config, cwd, command, PID, port, and health-check issues
- missing execution policy for executable contract-backed tools
- policy blockers for effects, timeouts, approval modes, runtimes, families, and env labels
- MCP config issues in local JSON files with `mcpServers`

MCP discovery is structural. Brigade summarizes server count and server ids, checks for missing commands and timeout metadata, and flags broad shell-like command shapes. MCP execution is explicit, local-runtime-only, and limited to approved calls through `brigade tools call run`.

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

Projection apply is local and explicit. Call planning, call approval review, run history inspection, and checkpoint review are local and non-executing. Tool execution is explicit through `brigade tools call run`, limited to approved local `script` entries and approved local `mcp` entries with already-running managed runtimes, and recorded with local receipts. Replay creates a pending call and never bypasses approval, runtime, or policy gates. Checkpoint resume is explicit and never automatic. Runtime lifecycle is explicit through `brigade tools runtime`; no command auto-starts runtimes as a side effect. Execution policy is host-local and gitignored. Brigade does not store secrets, connect to remote MCP servers, run OpenAPI or GraphQL calls, install schedulers, fetch remote schemas, store auth, send notifications, or mutate remote services.

## Tool Packs And Sync Plans

`brigade tools pack build` writes a local portable tool pack under `.brigade/tools/packs/`. Packs summarize configured tools, projection status, contracts, policy, runtime state, call approvals, run history, replay candidates, checkpoints, and current catalog issues. They also include an importable `portable-tools.toml` plus repo-relative source files when available. `pack list`, `pack show`, and `pack archive` inspect or move those local packs. `pack import <pack-dir>` merges packed catalog entries into another repo and copies packed source files, but it does not apply harness projections.

`brigade tools sync plan` is a wrapper-facing view of the existing projection plan. `brigade tools sync apply` is dry-run by default, and with no arguments it previews all configured tools. Use `--write --all` or `--write <tool-id>` for an explicit write, and use `--force` only when intentionally using the underlying managed projection overwrite behavior. Sync does not delete projections and does not run automatically from doctor, brief, work run, or release commands.

Release readiness and release candidate evidence treat tool packs and sync plans as review evidence. The evidence records the latest pack, whether the pack is stale or missing, the parity closeout state, sync-plan blocker counts, approval queue counts, run history counts, and checkpoint counts. Release commands never apply projections or run tool calls.
