# Brigade agent plugin

The Brigade plugin gives Claude Code and Codex working agent memory in the session where you install it. It provides six memory tools (search and read memory cards, write handoffs, show a work brief), the `brigade-work` skill, a non-blocking SessionStart brief in Claude Code, and an opt-in `enforce` skill that turns on Brigade's full work loop for one repository. The same memory server also runs over loopback HTTP for MCP gateways that do not accept stdio servers.

## Install in Claude Code

```
/plugin marketplace add escoffier-labs/brigade
/plugin install brigade@brigade
```

The plugin launches the memory server through npm, so it requires Brigade 0.30.0 or later on npm. If you already have the CLI from pipx, change the `command` in the plugin's MCP config to `brigade` and drop the first two arguments.

## Install in Codex

```
codex plugin marketplace add escoffier-labs/brigade
codex plugin add brigade@brigade
```

The Codex plugin ships no hook, because codex-cli 0.163.0 has no verified plugin hook contract. Codex gets the brief from the `work_brief` tool, which the `brigade-work` skill tells agents to run first.

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
- `card://<path>` is accepted as an alias for a bare path.
- A bare path resolves in the repo store first, then the user store.

`write_handoff` with `action` `create-card` or `update-card` (plus `target_card` and frontmatter `content`) is the reviewed route for proposing card changes. It never edits a canonical card. If the content fails lint, the draft is deleted and the tool returns the lint errors so the agent can retry. Content is capped at 256 KiB, and facts and evidence at 50 items each. Cards are served only when their real path stays inside the store, so a symlinked cards directory contributes nothing, and a handoff inbox that resolves outside the store is refused.

## Where memory lives

The repo store is chosen in this order:

1. An explicit `--target` is always used as the repo store, wired or not. This keeps existing `--target .` configs working.
2. With no `--target`, the server walks up from its working directory to the nearest `.brigade/config.json`.
3. The home directory is never treated as a repo.

The user store is `$BRIGADE_HOME/memory/cards`, with handoffs in `$BRIGADE_HOME/memory/handoffs`. `BRIGADE_HOME` defaults to `~/.brigade`. A missing store returns empty results, not errors.

## Loopback HTTP for MCP gateways

Print the path of the bearer token file, then start the server:

```
brigade memory serve-mcp --print-token-path
brigade memory serve-mcp --http 127.0.0.1:8790
```

Register `http://127.0.0.1:8790/mcp` with the header `Authorization: Bearer <contents of the token file>`. The token is generated on first start with mode 0600 and is never printed. The file must be a regular file owned by you with no group or other permission bits.

Executor v1 can use the stdio command instead. Executor v2 needs the HTTP mode. Binding off loopback is refused. The server checks the Host and Origin headers, answers 401 without a valid bearer, and drops a connection that stalls for 10 seconds.

## Turn on the full work loop

The plugin starts memory-first and ships no enforcement hooks. Ask the agent to use the `enforce` skill (`/brigade:enforce` in Claude Code) when you want the full loop in one repository. After showing you the command, it runs:

```
brigade init --target . --depth repo --harnesses <harness> --owner <harness> --git-exclude
```

That writes `.brigade/` into the repo, installs the harness's Brigade hooks (verification capture, raw-check blocking, and the handoff-before-stop gate), and adds local git excludes so none of it is committed. It then runs `brigade work brief --target .`. The hooks take effect in the next session.
