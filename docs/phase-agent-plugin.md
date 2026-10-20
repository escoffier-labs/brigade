# Brigade agent plugin (phase 1: Claude Code and Codex)

Status: approved 2026-10-19. The implementation plan is in `docs/phase-agent-plugin-plan.md`.

## Goal

A person who has never used Brigade installs one plugin in Claude Code or Codex and gets working agent memory in the same session: search and read memory cards, write handoffs, and propose memory changes through reviewed handoffs. The same memory core must also run behind an HTTP MCP endpoint so MCP gateways that do not accept stdio servers (Executor v2) can use it. A later phase reuses that endpoint as a bring-your-own remote connector for ChatGPT, claude.ai, and Grok Bot.

## Settled decisions

| Decision | Choice |
|---|---|
| Hosting | Local-first. Memory and the server run on the user's machine. No hosted Brigade service in this design. |
| Cloud chat access | Later phase: a user-run remote connector (OAuth plus tunnel). Out of scope for phase 1. |
| Remote tool scope (later phase) | Memory read and write only. No command execution from a cloud chat. |
| Phase 1 surfaces | Claude Code plugin and Codex plugin. |
| Transports | One memory core, two transports: stdio and Streamable HTTP on loopback. |
| Default plugin depth | Memory-first. MCP tools, the `brigade-work` skill, and a non-blocking SessionStart brief hook. Enforcement hooks are opt-in. |
| Memory location | A user-level store that works anywhere, plus repo-scoped memory inside Brigade-wired repos. Repo results take precedence. |
| Repository | The plugin lives in this repository. No second repository. |

## Non-goals for phase 1

- Claude Desktop extension (`.mcpb`), ChatGPT Apps, claude.ai connectors, and Grok Bot connectors.
- OAuth, tunnels, or any non-loopback listener.
- Dispatch, verify runs, queue operations, or other command execution through MCP.
- Directory or marketplace submission outside this repository's own plugin marketplace.

## Architecture

### 1. Memory core (`src/brigade/memory_mcp_core.py`)

One transport-free module owns the tool contract: tool specs, argument validation, and dispatch to existing Brigade memory functions. It returns plain `(payload, is_error)` results, matching the existing `ToolCaller` shape in `src/brigade/mcp_server.py`. It does not read stdin, open sockets, or print.

The existing read-only card server in `src/brigade/memory_cmd.py` (`_mcp_card_tool_specs`, `_mcp_card_tool_call`) moves behind this core. Its three tool names stay unchanged so current `brigade memory serve-mcp --stdio` users see no break.

### 2. Tool contract v1

| Tool | Kind | Behavior |
|---|---|---|
| `list_cards` | read | Existing behavior, extended with `scope` (`repo`, `user`, `all`; default `all`). Each item carries `origin`. |
| `get_card` | read | Existing behavior. Accepts an origin-qualified reference (`repo:<path>` or `user:<path>`); an unqualified path resolves repo first, then user. |
| `search_cards` | read | Existing behavior across both stores. Results are labeled by origin and repo matches rank first on ties. |
| `memory_status` | read | Resolved store paths, card counts per origin, whether the cwd is Brigade-wired, and the enforcement state. |
| `work_brief` | read | The compact output of `brigade work brief` for a Brigade-wired repo. Outside one, a short pointer to the user store. |
| `write_handoff` | write | Writes one handoff using the existing handoff template and writer. In a Brigade-wired repo it lands in that repo's handoff inbox; otherwise in the user store's inbox. Never overwrites an existing file. With `action` `create-card` or `update-card`, it is the reviewed route for proposing card changes. |

Write tools carry MCP `annotations` (`readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`) so clients can gate them. Read tools set `readOnlyHint: true`.

### 3. Memory resolution (`src/brigade/memory_scope.py`)

- User store: `$BRIGADE_HOME/memory/` when `BRIGADE_HOME` is set, otherwise `~/.brigade/memory/`. Layout: `cards/` and `handoffs/`. Created on first write, with mode `0700` directories. Read tools against a missing store return empty results, not errors.
- Repo store: an explicit `--target` is always the repo store, wired or not. With no `--target`, the server walks up from its working directory to the nearest directory holding `.brigade/config.json` or `.brigade/memory-care.toml`, stops at the first one, and never treats the home directory as a repo. It uses the repo's existing memory-care config and card roots. `repo_wired` means `.brigade/config.json` loads for that directory through `resolve_wired_target`; a memory-care-only repo serves its cards but is not wired. An invalid memory-care config drops the repo store from reads, while `write_handoff` still writes to the repo inbox.
- Both stores are searched by one call. The core never follows symlinks out of either root, reusing the existing card path guard in `_mcp_read_card`.

### 4. Transports

- **stdio**: `brigade memory serve-mcp --stdio` (existing command, now backed by the core). Zero dependencies, as today.
- **HTTP**: `brigade memory serve-mcp --http [--host 127.0.0.1] [--port 8931]`. Streamable HTTP on `POST /mcp`, with an unauthenticated `GET /healthz` that exposes nothing, built on the standard library in `src/brigade/mcp_server.py` (the transport #1816 added for the memory and skills servers). The memory core passes its callbacks to that transport unchanged.
  - Binds only to loopback (`127.0.0.1`, `::1`, or `localhost`). Any other address is refused with exit code 2.
  - Requires `Authorization: Bearer <token>`. The token comes only from the `BRIGADE_MCP_HTTP_TOKEN` environment variable and must be at least 32 characters; it is never a flag and never printed. A missing or short token exits 2 before binding.
  - Validates the `Origin` header when present and rejects non-loopback origins (DNS rebinding protection required by the MCP transport spec).
  - `--target` defaults to auto-detection; an explicit `--target` is always the repo store.
  - Stateless: no session resumption in phase 1. Server-sent streaming is not needed for these tools; responses use single JSON bodies.

Executor v1 uses the stdio command. Executor v2 registers `http://127.0.0.1:8931/mcp` (or the chosen port) with the bearer header.

### 5. Plugin packaging

```
.claude-plugin/marketplace.json          # repo-root marketplace listing one plugin: brigade
plugins/brigade/
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json              # Codex manifest, schema per `codex plugin` docs
  .mcp.json                              # memory server, stdio
  codex.mcp.json                         # Codex MCP launch config
  hooks/hooks.json                       # SessionStart brief only (Claude Code)
  hooks/session-start.sh
  skills/brigade-work/SKILL.md           # generated copy, see below
  skills/enforce/SKILL.md                # /brigade:enforce
```

- **MCP launch**: `.mcp.json` runs `npx -y @escoffier-labs/brigade@<pinned version> memory serve-mcp --stdio`. The npm package bundles its Python runtime, so the user needs no prior pipx install. The README documents swapping the command to `brigade` for users who already have the CLI. The pinned version is bumped by the release process.
- **Skill copy**: the plugin's `brigade-work` skill is generated from the canonical skill source by a script, and a test fails when the copy drifts. The plugin directory is not a third hand-maintained skill location.
- **SessionStart hook**: Claude Code only; Codex has no verified plugin hook contract in codex-cli 0.163.0. Calls `brigade work brief` in compact form when the cwd is Brigade-wired, otherwise prints one line naming the user store. It runs under a 5-second timeout and always exits 0, so a missing CLI, slow npx cold start, or broken repo never blocks a session.
- **`/brigade:enforce`** (a plugin skill named `enforce`, so it exists in both harnesses): runs `brigade init --target . --depth repo --harnesses <current harness> --git-exclude` after showing the user what it will wire. That reuses the existing enforced hooks (verify capture, raw-check blocking, handoff Stop gate). The plugin itself ships none of those hooks.
- **Install path**: `/plugin marketplace add escoffier-labs/brigade` then `/plugin install brigade@brigade` in Claude Code; the equivalent `codex plugin` commands in Codex.

## Data flow

1. The agent calls `search_cards`. The transport decodes JSON-RPC and hands `(name, arguments)` to the core.
2. The core resolves both stores through `memory_scope`, runs the existing search on each, merges and labels results, and returns them.
3. `write_handoff` validates inputs, resolve the destination store, and call the existing writers. The response includes the written path, relative to its store.

## Failure handling

- An unknown tool or invalid arguments return an MCP tool error (`isError: true`) with a one-line reason, not a JSON-RPC protocol error.
- An invalid repo memory-care config degrades to user-store-only results, and `memory_status` reports the config error. This changes today's behavior, where `serve-mcp` exits 2.
- A write to a store that cannot be created returns a tool error naming the path and the OS error.
- The HTTP server answers 401 without a valid bearer, 403 for a rejected origin, 404 for paths other than `/mcp` and `/healthz`, and 405 for methods other than POST on `/mcp`.

## Security notes

- No tool executes commands, reads files outside the two store roots, or reaches the network.
- The HTTP listener is loopback-only and bearer-gated in phase 1. Any later remote exposure is a separate design with OAuth and its own review by the security route.
- Handoff and proposal bodies are user content. They are written as files and never interpreted.

## Testing

- Core contract tests: every tool against a fixture with a repo store, a user store, both, and neither. Covers precedence, origin labels, the symlink escape guard, and no-overwrite handoffs.
- Transport parity test: one table of requests is run through the stdio and HTTP transports and must produce identical results.
- HTTP security tests: non-loopback bind refused, missing and wrong bearer, a hostile `Origin`, wrong path and method.
- Backward compatibility: the three existing tool names and argument shapes still work against a repo-only fixture.
- Plugin checks: manifest validation in CI, skill-copy drift test, and a hook test proving the SessionStart script exits 0 with no CLI on PATH.
- Manual canaries before release: install from the local marketplace in a temp `HOME` for Claude Code and Codex, then register the HTTP endpoint in Executor v2.

## Dependencies and checks for the plan

- The plugin pins the pyproject version through `scripts/version_sync.py`. The marketplace entries land with 0.31.0, the first release that contains these commands, and the plugin needs `@escoffier-labs/brigade@0.31.0` or later on npm.
- The Codex plugin manifest schema is confirmed from `codex plugin --help` and Codex documentation during planning. No design decision depends on it.
- New modules stay well under the module size ceiling. `memory_cmd.py` should shrink as the card tool code moves into the core.

## Later phases (not designed here)

1. Claude Desktop extension that bundles the stdio server.
2. A future connect command: OAuth and a tunnel in front of the HTTP transport, offering the v1 memory tools as a custom connector for ChatGPT, claude.ai, and Grok Bot. This generalizes the single-operator Dot and Grok Bot packs.
3. Directory submissions once the connector is stable.
