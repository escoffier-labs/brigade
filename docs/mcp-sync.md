# MCP server config sync (`brigade mcp`)

Every agent tool reads its MCP servers from a different file in a different shape. Keeping
the same servers wired across Claude Code, Cursor, Codex, VS Code, OpenCode, and Antigravity
means hand-editing six configs. `brigade mcp` keeps one canonical catalog and merges it into
each tool's native config.

If you are comparing approaches before adopting Brigade, start here:
[sync MCP servers across coding agents](https://brigade.tools/compare/sync-mcp-servers-across-coding-agents).

This is the one place Brigade writes runtime config into tool-owned files, so it is bounded
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
| Claude user | `~/.claude.json` | JSON `mcpServers`. **User-scoped** (`--user-scope`) |
| Cursor user | `~/.cursor/mcp.json` | JSON `mcpServers`. **User-scoped** (`--harness cursor --user-scope`) |
| Codex user | `~/.codex/config.toml` | TOML `[mcp_servers.<name>]`. **User-scoped** (`--user-scope`) |
| Grok user | `~/.grok/config.toml` | TOML `[mcp_servers.<name>]`. **User-scoped** (`--user-scope`) |
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
brigade mcp import --harness cursor --merge   # read an existing config into the catalog
brigade operator sync-mcp --write      # validate -> sync -> summary, one receipt
```

Cursor stays project-scoped unless both `--harness cursor` and `--user-scope`
are present. A user-scoped Cursor projection drops a GraphTrail `--db` argument
when that database resolves inside the source repository, so the global client
does not pin every workspace to one repository.

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
