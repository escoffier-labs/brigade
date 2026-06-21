# MCP server config sync (`brigade mcp`)

Every agent tool reads its MCP servers from a different file in a different shape. Keeping
the same servers wired across Claude Code, Cursor, Codex, VS Code, OpenCode, and Antigravity
means hand-editing six configs. `brigade mcp` keeps one canonical catalog and merges it into
each tool's native config.

This is the one place Brigade writes runtime config into tool-owned files, so it is bounded
on purpose: it is **explicit** (never runs from `doctor`/`brief`/`work run`), **dry-run by
default**, **merges by server key** (servers you added by hand are never touched), and
**never inlines secrets** (env values are written as `${VAR}` references). Brigade still does
not start MCP servers or store auth.

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
never reads the process environment to materialize a value; it writes the reference form each
tool expands at launch.

## Supported tools

| Tool | File | Shape |
|------|------|-------|
| Claude Code | `.mcp.json` | JSON `mcpServers`; remote `{type,url}` |
| Cursor | `.cursor/mcp.json` | JSON `mcpServers` (same as Claude) |
| Codex CLI | `.codex/config.toml` | TOML `[mcp_servers.<name>]`, merged surgically (other tables/comments preserved) |
| VS Code | `.vscode/mcp.json` | JSON `servers`; secrets become top-level `inputs[]` + `${input:VAR}` |
| OpenCode | `opencode.json` | JSON `mcp`; `{type:"local",command:[cmd,...args],environment}` |
| Antigravity | `~/.gemini/config/mcp_config.json` | JSON `mcpServers`; remote uses `serverUrl`. **User-scoped** (`--user-scope`) |

By default a repo's sync targets the tools in its Brigade selection (`.brigade/config.json`)
that have an adapter, plus VS Code. A tool with no adapter is reported as `unsupported` by
`brigade mcp doctor` rather than silently skipped. New adapters are added in
`src/brigade/mcp_adapters.py`.

## Commands

```bash
brigade mcp init                       # scaffold .brigade/mcp.json + sidecar, update .gitignore
brigade mcp add --name github \
  --command npx --args "-y @modelcontextprotocol/server-github" \
  --env GITHUB_TOKEN=ref:GITHUB_TOKEN --timeout 60
brigade mcp list                       # show the catalog
brigade mcp plan                       # preview what a sync would do (read-only)
brigade mcp sync                       # dry-run across every configured tool
brigade mcp sync --write               # actually merge into each tool's config
brigade mcp sync --write --user-scope  # also write Antigravity's user-global config
brigade mcp doctor                     # validate the catalog, report unsupported tools
brigade mcp import --harness cursor --merge   # read an existing config into the catalog
brigade operator sync-mcp --write      # validate -> sync -> summary, one receipt
```

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
