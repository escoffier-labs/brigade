# MCP server config sync (`brigade mcp`)

Every agent tool reads its MCP servers from a different file in a different shape. Keeping
the same servers wired across Claude Code, Cursor, Codex, VS Code, OpenCode, Antigravity, and other harnesses
means hand-editing multiple configs. `brigade mcp` keeps one canonical catalog and merges it into
each tool's native config.

If you are comparing approaches before adopting Brigade, start here:
[sync MCP servers across coding agents](https://brigade.tools/compare/sync-mcp-servers-across-coding-agents).

MCP runtime config sync into tool-owned files is bounded
on purpose: it is **explicit** (never runs from `doctor`/`brief`/`work run`), **dry-run by
default**, **merges by server key** (servers you added by hand are never touched), and
**never inlines secrets** (env values are written as `${VAR}` references). Brigade still does
not start MCP servers during plan, doctor, or default sync, and it does not store auth.
Runtime checks happen only through the explicit `mcp verify` command or `sync --write --verify`.

## The canonical catalog: `.brigade/mcp.json`

Tracked and shared (committed to the repo). The ownership sidecar `.brigade/mcp/state.json`
is machine-local and gitignored.

```jsonc
{
  "version": 1,
  "servers": {
    "github": {
      "transport": "stdio",                 // "stdio" | "http" | "sse" (default stdio)
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": { "ref": "GITHUB_TOKEN" } },  // {ref} | {literal} | "bare" (=literal, warned)
      "timeout": 60,
      "enabled": true,
      "targets": null                       // null = all configured tools, or ["claude","cursor"]
    },
    "docs": {
      "transport": "http",
      "url": "https://mcp.example.com/v1",
      "headers": { "Authorization": { "ref": "DOCS_TOKEN" } },
      "timeout": 30
    }
  }
}
```

`env` and `headers` values are references (`{"ref": "VAR"}`) or explicit literals
(`{"literal": "..."}`). A bare string is treated as a literal and flagged by `doctor`. Brigade
does not read the process environment while planning or syncing. Runtime verification resolves
references only in the spawned process or HTTP request, then omits those values from output and
receipts.

## Supported tools

| Tool | File | Shape |
|------|------|-------|
| Claude Code | `.mcp.json` | JSON `mcpServers`; remote `{type,url}` |
| Cursor | `.cursor/mcp.json` | JSON `mcpServers` (same as Claude) |
| Codex CLI | `.codex/config.toml` | TOML `[mcp_servers.<name>]`, merged surgically (other tables/comments preserved) |
| Grok CLI | `.grok/config.toml` | TOML `[mcp_servers.<name>]`, same surgical merge as Codex (project scope) |
| VS Code | `.vscode/mcp.json` | JSON `servers`; secrets become top-level `inputs[]` + `${input:VAR}` |
| OpenCode | `opencode.json` | JSON `mcp`; `{type:"local",command:[cmd,...args],environment}` |
| Antigravity | `~/.gemini/config/mcp_config.json` | JSON `mcpServers`; remote uses `serverUrl`. **User-scoped** (`--user-scope`) |
| Claude user | `~/.claude.json` | JSON `mcpServers`. **User-scoped** (`--harness claude-user --user-scope`) |
| Cursor user | `~/.cursor/mcp.json` | JSON `mcpServers`. **User-scoped** (`--harness cursor --user-scope`) |
| Codex user | `~/.codex/config.toml` | TOML `[mcp_servers.<name>]`. **User-scoped** (`--harness codex-user --user-scope`) |
| OpenCode user | `~/.config/opencode/opencode.json` | JSON `mcp`. **User-scoped** (`--harness opencode-user --user-scope`) |
| Kimi user | `~/.kimi-code/mcp.json` (legacy root; `~/.kimi` when the capability probe selects it) | JSON `mcpServers`. **User-scoped** (`--harness kimi-user --user-scope`) |
| Grok user | `~/.grok/config.toml` | TOML `[mcp_servers.<name>]`. **User-scoped** (`--harness grok-user --user-scope`) |
| OpenClaw | `~/.openclaw/openclaw.json` | JSON `mcp.servers`. **User-scoped** (`--user-scope`) |
| Hermes | `~/.hermes/config.yaml` | YAML `mcp_servers`. **User-scoped** (`--user-scope`) |

By default a repo's sync targets the tools in its Brigade selection (`.brigade/config.json`)
that have an adapter. VS Code is not a Brigade harness: it is included only when the repo
already has a `.vscode/` directory, so a sync never creates one unasked (`--harness vscode`
forces it). A tool with no adapter is reported as `unsupported` by `brigade mcp doctor`
rather than silently skipped. New adapters are added in `src/brigade/mcp_adapters.py`.

## Commands

```bash
brigade mcp init                       # scaffold .brigade/mcp.json + sidecar, update .gitignore
brigade mcp add --name github \
  --command npx --args "-y @modelcontextprotocol/server-github" \
  --env GITHUB_AUTH_ENV=ref:BRIGADE_GITHUB_AUTH_ENV --timeout 60
brigade mcp list                       # show the catalog
brigade mcp plan                       # preview what a sync would do (read-only)
brigade mcp sync                       # dry-run across every configured tool
brigade mcp sync --write               # actually merge into each tool's config
brigade mcp sync --write --verify      # write, then verify the selected runtimes
brigade mcp sync --write --user-scope  # also write configured user-global targets (stdio servers gate; see below)
brigade mcp sync --harness cursor --user-scope --write  # write ~/.cursor/mcp.json
brigade mcp verify --harness cursor --name github  # bounded runtime handshake
brigade mcp doctor                     # validate the catalog, report unsupported tools
brigade mcp import cursor --merge   # read an existing config into the catalog
brigade operator sync-mcp --write      # validate -> sync -> summary, one receipt
```

Cursor stays project-scoped unless both `--harness cursor` and `--user-scope`
are present. A user-scoped Cursor projection drops a brigade-code-graph `--db` argument
when that database resolves inside the source repository, so the global client
does not pin every workspace to one repository.

## Field projection fidelity

`plan`, `sync`, and `doctor` include a `fidelity` report in JSON and render its
non-preserved fields in text. Version 1 reports source-level output semantics.
Runtime health still requires an explicit `verify` invocation.

The report contains:

- `summary`: counts of `preserved`, `transformed`, `unsupported`, and
  `intentionally_omitted` field rows, plus the number of blocking rows.
- `targets`: each selected harness with state `ok`, `partial`, `blocked`, or
  `unevaluated`, and its blocking count.
- `servers`: harness, server, project/user scope, `evaluated`, `faithful`, optional
  OpenCode layout, and field rows. Each field has a class-level name, status,
  stable reason, and `blocking`. Conversion rows may name destination fields and
  units. `faithful` is false when an evaluated projection has unsupported fields.
- `excluded`: catalog entries disabled by canonical membership or routed away
  from a selected harness. These rows respect `--name` and do not add actions or
  change existing action counts.

Codex and OpenCode, including their user scopes, have full field contracts.
Other adapters report `evaluated: false` and `faithful: null`. Known security
losses still block those destinations. Report rows omit credential values,
header names, environment variable names, commands, and URLs. Server and harness
identities remain command metadata.

For example, a Codex HTTP server with canonical `timeout: 7` can commit
successfully with exit 0 while reporting this field:

```json
{
  "field": "timeout",
  "status": "unsupported",
  "reason": "remote_timeout_unsupported",
  "blocking": false,
  "destination": "timeout",
  "units": "seconds"
}
```

That server has `faithful: false` and its target is `partial`. Transaction
`projection.terminal_state: committed` describes the write outcome separately.
Codex stdio reports emitted timeout seconds without asserting native runtime
support. OpenCode converts canonical seconds to milliseconds: a flat timeout or
nested catalog/execution timeouts. Native startup is retained where the layout
supports it. OpenCode's remote shape collapses HTTP/SSE, so SSE projects with
`sse_distinction_unsupported`, a nonblocking warning.

Canonical `enabled` controls membership. OpenCode native enabled/disabled
controls activation and follows the destination layout's precedence. Targets
are routing metadata and description stays in the catalog. These intentional
omissions can accompany a faithful projection. What happens when a catalog entry
carries generic headers and the live entry already has native auth depends on the
harness:

- **Codex.** Retained native auth wins over a generic header of the same name. The shadowed
  header is reported with `native_auth_precedence`. If you change that generic header after
  adoption, the server is blocked with `native_auth_collision`.
- **OpenCode.** A canonical header replaces a retained native header of the same name, reported
  as `explicit_canonical_replacement`; other retained native headers are kept.
- **Claude Code (`headersHelper`).** The live helper is the auth for the harness. Brigade never
  wrote it, so it outranks auth that reached the catalog through the Codex-native fields
  (`bearer_token_env_var`, `http_headers`, `env_http_headers`): the sync leaves those fields out of
  the Claude entry, keeps the helper, writes the entry and reports it `current`. Each dropped field
  is reported with `native_auth_helper_precedence` (field name only, not blocking), and a catalog
  edit can never add or swap a credential on a helper-backed entry. Catalog `headers` are different:
  they are declared for every harness, so a server whose projection would still carry a `headers`
  map next to a live helper, including a mixed entry with both `headers` and a Codex-native field,
  is blocked with `native_auth_source_collision`. Nothing is written for that server, and neither
  `--force` nor `--adopt` overrides it. Remove the catalog headers or the live helper to continue.
  Without a live helper nothing changes: every one of those fields is projected into Claude
  `headers`.

### Remote-server auth that stays native

Two kinds of tool-native auth survive `import` and `sync` without becoming catalog
data you edit by hand:

- **Claude Code `headersHelper`** (`claude`, `claude-user`). It is a command the tool runs to
  print a header map, so Brigade keeps it out of the catalog and out of
  `.brigade/mcp/state.json` (no command, no hash of it). A sync that rewrites
  the same live entry carries the key over only while the entry stays remote and both URLs are
  strictly canonical and name the same origin. The origin check fails closed: a URL with
  userinfo, a backslash, whitespace or control characters, a percent-encoded or non-ASCII
  host, an empty host, a port that is empty, zero, above 65535 or not plain decimal, a
  scheme other than `http` or `https`, or text after the host that does not start with `/`
  has no origin, so the helper is never kept for it. Two URLs match only when their host text
  is identical after lowercasing and dropping an explicit default port; a trailing dot, an IPv6
  literal written another way, or an IDNA spelling is a different origin. This is deliberate:
  Python's URL parser and the WHATWG parser Claude Code runs on can read the same string
  differently, and a disagreement would send the helper's credentials to a host you never
  approved. If the catalog moves the server to another origin, or to stdio, that server is
  blocked on that harness (`native_auth_origin_changed` or `native_auth_transport_changed`) and
  nothing is written for it. `--force` and `--adopt` do not override the block; remove the live
  entry or restore its auth by hand. Other servers in the same file are still written.
  The same key is never removed with its entry: `sync --prune`, a harness profile catalog
  removal and a profile uninstall block that server with `native_auth_blocks_removal`, remove
  nothing for it and finish the rest. No flag overrides it. Remove the `headersHelper` line
  or the entry by hand; a later run then completes. A profile run with a blocked server exits
  1 and reports `incomplete`, and its ownership record stays so the next run can finish.
  Ownership fingerprints ignore this key, so an entry that differs from the projection only by
  the helper reads `current` and a second sync is a no-op. Other unmodeled keys are still
  replaced by a rewrite: only `headersHelper` is allowlisted.
- **Codex `bearer_token_env_var`, `http_headers`, `env_http_headers`**. `import --merge` copies them
  into an existing catalog entry only when the name matches, both entries are remote on the exact
  same normalized URL (a URL with userinfo or any other non-canonical form never matches), and the
  existing entry has no auth of its own: any `headers`, `http_headers`, `env_http_headers` or
  `bearer_token_env_var` counts as existing auth. It reports the server under `native_auth_merged`
  instead of `skipped_existing`. A different URL, a different name, or existing auth is left alone.

A literal header or a kept secret (`--keep-secrets`) can still put a value in the catalog; this
section is about the two native forms above, which carry a command or a variable name, never a token.

Explicit canonical HTTP auth on stdio, OpenCode OAuth settings discarded by
another adapter, and explicit disabled activation without a destination
equivalent are blocking losses. This includes `oauth: false`, which explicitly
disables OAuth auto detection and must not be silently discarded. Plan and sync
return exit 2 and refuse all transaction mutations. `--force` and `--adopt` do not bypass them. Live remote
auth removed during a switch to stdio remains intentional transport cleanup
when the canonical stdio entry contains no HTTP auth. Existing native-auth
collisions retain exit 1 behavior: the conflicted destination is skipped while
safe siblings may commit. Doctor reports `native_auth_collision` as a warning
so `operator sync-mcp` can preserve that partial-write behavior. The fidelity
row remains blocking and the target remains `blocked`. Other blocking losses
remain doctor errors.

The pre-write report survives transaction refusal, rollback, and unfinished
recovery output. `doctor` assesses the default project targets, ignores foreign
ownership conflicts, and creates no native files. Malformed active native
configs fail diagnosis while other targets are still assessed. Ordinary JSON
adapters continue accepting blank or whitespace-only configs. Cursor and Kimi
user adapters retain their strict blank-file refusals and actionable diagnostics.
Malformed nonblank JSON now refuses the entire transaction. VS Code input entries
must have string IDs. Malformed IDs report `native_config_malformed` before the
input collector runs, including when the canonical catalog is empty. All selected
invalid target plans block the transaction, including targets held behind the global
stdio acknowledgment. Blocking rows other than `native_auth_collision` make
doctor return exit 1. Nonblocking unsupported fields produce warnings and
leave its exit status at 0. Use the existing `plan`/`sync --user-scope` options to
inspect user targets. Hermes YAML retains its existing bounded parser semantics
and remains unevaluated for field fidelity.

## User-scoped stdio servers multiply processes

A stdio MCP server is not a shared daemon. Every active client session starts
its own child process for every stdio server in its configuration, so a
user-wide config with `S` stdio servers costs `S x active sessions` processes.
A desktop client holding nine sessions over a 20-server user catalog runs 180
server processes before doing any work.

Because of that, a user-scoped sync that would write one or more stdio servers
never completes silently. Interactive runs print the destination, the stdio
count, and the process formula, then ask for confirmation. Non-interactive and
`--json` runs fail with exit 2 unless `--allow-global-stdio` is passed. Dry-run
and plan output carry `transport` and `scope` on every item so the exposure is
visible before writing. Prefer project-scoped configuration, or a shared
HTTP/SSE transport where the client supports one; remote servers never gate.

## Runtime verification

`brigade mcp verify` checks runtime health without changing harness config. For each selected
server, it reports `config_current` and `runtime_healthy` separately. `config_current` is true
only when every selected harness projection for that server is current. `runtime_healthy` is
true only after the server completes MCP initialization and returns a list from `tools/list`.

Stdio checks launch `[command, *args]` without a shell. HTTP checks disable redirects and accept
bounded JSON or event-stream responses. Both paths use one per-server deadline, cap captured
output, send no `tools/call` request, and retain only the negotiated protocol version and tool
count. Stdio timeouts terminate the spawned process group. Failures use 4 stable classes:
`timeout`, `startup_failure`, `connection_failure`, and `protocol_failure`.

Every runtime check writes a machine-readable receipt under
`.brigade/mcp/verify-runs/<run-id>/receipt.json`. Receipts exclude command arguments, raw process
output, header values, and environment values. Use `--name` and `--harness` to narrow execution,
and `--timeout` or `sync --verify-timeout` to override the catalog timeout for that run.

## Merge and ownership

Brigade owns only the server keys recorded in `.brigade/mcp/state.json`. For each
`(tool, server)` it compares the canonical definition, what it last wrote, and the live value
in the file:

| Situation | Status | Default action |
|-----------|--------|----------------|
| Not in the file | `missing` | create |
| In the file, not owned by Brigade | `foreign` | skip (`--adopt` to take over) |
| Owned, matches | `current` | skip |
| Owned, canonical changed | `stale` | update |
| Owned, edited outside Brigade | `conflicted` | skip (`--force` to overwrite) |
| Dropped from the catalog, still pristine | `orphan` | skip (`--prune` to remove) |
| Dropped from the catalog, edited in place | `orphan-edited` | leave untouched |

A server you added to a tool config yourself is `foreign` and never modified. A server Brigade
manages but you then edited is a `conflict` and is left alone unless you pass `--force`. Orphans
(removed from the catalog) are deleted only with `--prune`, and only when still byte-identical to
what Brigade wrote. If the sidecar is lost (e.g. a fresh clone), any live server identical to what
Brigade would write is reconciled back to owned, so re-syncing does not spuriously conflict.

## Secrets

Brigade writes references, never values:

- `passthrough` / `expand` tools (Claude, Cursor, Codex, OpenCode, Antigravity): `"VAR": "${VAR}"`.
- VS Code: a top-level `inputs[]` entry (`promptString`, `password: true`) plus `"${input:VAR}"`.

On `import`, a literal-looking secret (a value matching a known secret field name) is demoted to a
`{"ref": ...}` and reported under `secrets_demoted`; the dropped value never lands in the catalog,
the sidecar, or any JSON receipt.
