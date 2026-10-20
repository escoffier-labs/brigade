# Brigade agent plugin

The Brigade plugin gives Claude Code and Codex working agent memory in the session where you install it. It provides six memory tools (search and read memory cards, write handoffs, show a work brief), the `brigade-work` skill, a non-blocking SessionStart brief in Claude Code, and an opt-in `enforce` skill that turns on Brigade's full work loop for one repository. The same memory server also runs over loopback HTTP for MCP gateways that do not accept stdio servers.

## Install in Claude Code

```
/plugin marketplace add escoffier-labs/brigade
/plugin install brigade@brigade
```

The marketplace install works from Brigade 0.31.0, the release that adds the marketplace entries. The plugin launches the memory server through npm, so it requires Brigade 0.31.0 or later on npm. If you already have the CLI from pipx, change the `command` in the plugin's MCP config to `brigade` and drop the first two arguments.

Before 0.31.0, the only way to try the plugin is to load `plugins/brigade` locally with its `command` changed to a `brigade` CLI built from this branch, because no published package has these commands yet.

## Install in Codex

```
codex plugin marketplace add escoffier-labs/brigade
codex plugin add brigade@brigade
```

The Codex plugin ships no hook, because codex-cli 0.163.0 has no verified plugin hook contract. Codex users can call the `work_brief` tool for the same brief the Claude Code hook prints.

`write_handoff` is marked as a write tool, so Codex asks for approval before calling it. Under `codex exec` with the default `never` approval policy the call is rejected; pass `--approve-for-me` to route it through automatic review.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `list_cards` | read | Lists memory cards from the repo and user stores, labeled by origin. Takes an optional `scope` of `repo`, `user`, or `all`. |
| `get_card` | read | Reads one card by reference. |
| `search_cards` | read | Keyword search across both stores; repo matches rank first on ties. |
| `memory_status` | read | Shows the resolved stores, card counts, and whether the repo is Brigade-wired. |
| `work_brief` | read | Compact work brief for the current repo: branch, dirty files, sessions, next task. |
| `write_handoff` | write | Writes one Memory Handoff to the repo inbox, or to the user store outside a repo. Never overwrites a file. |

Card references use one grammar:

- `repo:<path>` reads from the repo store.
- `user:<path>` reads from the user store.
- `card://<path>` is accepted as an alias for a bare path. Resources list repo cards as `card://<path>` and user-store cards as `user-card://<path>`.
- A bare path resolves in the repo store first, then the user store.

`write_handoff` with `action` `create-card` or `update-card` (plus `target_card` and frontmatter `content`) is the reviewed route for proposing card changes. It never edits a canonical card. If the content fails lint, the draft is deleted and the tool returns the lint errors so the agent can retry. Content is capped at 256 KiB, and facts and evidence at 50 items each. Cards are served only when their real path stays inside the store, so a symlinked cards directory contributes nothing, and a handoff inbox that resolves outside the store is refused.

## Where memory lives

The repo store is chosen in this order:

1. An explicit `--target` is always used as the repo store, wired or not. This keeps existing `--target .` configs working.
2. With no `--target`, the server walks up from its working directory to the nearest directory holding `.brigade/config.json` or `.brigade/memory-care.toml`, and stops at the first one it finds.
3. The home directory is never treated as a repo.

The repo counts as Brigade-wired only when `.brigade/config.json` loads; a repo found through `memory-care.toml` alone serves its cards, but `work_brief` reports it as not wired. If the repo's `memory-care.toml` is invalid, its cards are dropped from reads and `memory_status` reports the error, but `write_handoff` still writes to the repo inbox.

The user store is `$BRIGADE_HOME/memory/cards`, with handoffs in `$BRIGADE_HOME/memory/handoffs`. `BRIGADE_HOME` defaults to `~/.brigade`. A missing store returns empty results, not errors.

User-store handoffs are plain Markdown files under `$BRIGADE_HOME/memory/handoffs`. Read them directly, and check one with `brigade handoff lint <file>`.

## Loopback HTTP for MCP gateways

Put a bearer token of at least 32 characters in `BRIGADE_MCP_HTTP_TOKEN`, then start the server:

```
export BRIGADE_MCP_HTTP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
brigade memory serve-mcp --http [--host 127.0.0.1] [--port 8931]
```

The token is read only from the environment, never from a flag, so it stays out of process listings and shell history. The default host is `127.0.0.1` and the default port is 8931. Register `http://127.0.0.1:8931/mcp` with the header `Authorization: Bearer <token>`. MCP requests go to `POST /mcp`. `GET /healthz` answers without a token and reveals nothing about the stores.

Executor v1 can use the stdio command instead. Executor v2 needs the HTTP mode. Binding off loopback (`127.0.0.1`, `::1`, or `localhost`) is refused with exit code 2, as is a missing or short token. The server checks the Host and Origin headers, answers 401 without a valid bearer, and drops a connection that stalls for 10 seconds.

## Turn on the full work loop

The plugin starts memory-first and ships no enforcement hooks. Ask the agent to use the `enforce` skill (`/brigade:enforce` in Claude Code) when you want the full loop in one repository. After showing you the command, it runs:

```
brigade init --target . --depth repo --harnesses <harness> --owner <harness> --git-exclude
```

That writes `.brigade/`, the `brigade-work` skill, AGENTS guidance, and a handoff inbox into the repo. The full enforced hooks (verification capture, raw-check blocking, and the handoff-before-stop gate) are installed for Claude Code only, into `.claude/settings.json`; they take effect in the next session. Codex gets the skills and AGENTS guidance from `brigade init`, but no hooks. `--git-exclude` keeps Brigade's own files out of git through local excludes. Some repos already track `.claude/settings.json`, and there the hook entries show up as a change to that file. The skill then runs `brigade work brief --target .`.
