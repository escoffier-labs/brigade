# Wiring the code map and evidence log MCP

> **Public names:** code map (`brigade code`) and evidence log (`brigade evidence`).
> New installs use `brigade setup` to install the canonical engine binaries.

Installing the CLIs is not the same as wiring them. If the binaries are on PATH but no
`[mcp_servers.*]` block (Codex) or `mcpServers` entry (Claude Code) references them, agents
cannot call the tools. `brigade doctor` validates the local MCP catalog but does not prove a
live tool call works. Use `brigade mcp verify` for bounded runtime handshakes, then test a read
query from the harness.

This page covers wiring both MCP servers into a coding harness, verifying the wiring end to
end, when an agent should reach for each, and the common failure modes. It assumes the CLIs
are already installed: `brigade-code-graph` plus `brigade-code-graph-mcp` on PATH, and `brigade-evidence` on PATH.

The two servers differ in one important way:

- **brigade-code-graph** is one SQLite graph per repo. The MCP server resolves a database per call, so
  a single running server answers for any indexed repo through a `repo` or `db` argument.
- **brigade-evidence** is a single local archive (XDG data path), fed by crawls and Brigade receipt
  imports. There is no per-repo database. Queries narrow by a `project` or `source` filter.

For the fleet path that keeps one catalog and merges it into every harness, see
[MCP sync](mcp-sync.md). The snippets below are the same servers, written by hand.

## 1. Claude Code

Add each server to `.mcp.json` (project scope, committed) or `~/.claude.json` (user scope).

`claude mcp add`:

```bash
# brigade-code-graph: bind the default db to this repo's graph. -s sets the scope.
claude mcp add brigade-code-graph -s project -- \
  /abs/path/to/brigade-code-graph-mcp --db /abs/path/to/repo/.brigade/code-graph/code-graph.db

# brigade-evidence: single archive, no per-repo path.
claude mcp add brigade-evidence -s project -- brigade-evidence mcp
```

Or edit `.mcp.json` directly:

```jsonc
{
  "mcpServers": {
    "brigade-code-graph": {
      "command": "/abs/path/to/brigade-code-graph-mcp",
      "args": ["--db", "/abs/path/to/repo/.brigade/code-graph/code-graph.db"]
    },
    "miseledger": {
      "command": "/abs/path/to/brigade-evidence",
      "args": ["mcp"],
      "timeout": 60
    }
  }
}
```

`--db` is the default database. Every brigade-code-graph tool also accepts a `repo` argument (uses
`<repo>/.brigade/code-graph/code-graph.db`) or a `db` argument (explicit path), so one server can
answer for any indexed repo even without a default. The db is opened lazily per call, so the
server starts before the default db exists.

### Memory cap for brigade-code-graph (defense in depth)

A sync pointed at a filesystem or home root once climbed to ~53GB of pending graph and
OOM-froze a desktop. `brigade-code-graph` now refuses home and filesystem roots outright, but a hard
memory cap on the MCP process is still worth wiring. Point `command` at a wrapper instead of
the binary:

```bash
#!/usr/bin/env bash
# brigade-code-graph-mcp-capped: hard 4GB cap, no swap. Real working set is well under 1GB.
uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
exec systemd-run --user --scope --quiet \
  --slice=app-agent-workloads.slice \
  -p MemoryMax=4G -p MemorySwapMax=0 \
  /abs/path/to/brigade-code-graph-mcp "$@"
```

Then set `"command": "/abs/path/to/brigade-code-graph-mcp-capped"` in the config above. Any future
runaway gets killed instead of taking the machine down.

## 2. Codex CLI

Codex reads TOML from `~/.codex/config.toml`. Add one table per server:
Replace `/ABSOLUTE_ACCOUNT_HOME` below with your account's actual absolute home directory; MCP command fields do not expand this placeholder.

```toml
[mcp_servers.brigade-code-graph]
command = '/abs/path/to/brigade-code-graph-mcp'
args = ['--db', '/abs/path/to/repo/.brigade/code-graph/code-graph.db']

[mcp_servers.brigade-evidence]
command = '/ABSOLUTE_ACCOUNT_HOME/.local/bin/brigade-evidence'
args = ['mcp']
timeout = 60
```

To use the memory cap, point `command` at the wrapper. It forwards `"$@"`, so keep the same
`args`:

```toml
[mcp_servers.brigade-code-graph]
command = '/abs/path/to/brigade-code-graph-mcp-capped'
args = ['--db', '/abs/path/to/repo/.brigade/code-graph/code-graph.db']
```

Codex config is user-scoped, so a single `[mcp_servers.brigade-code-graph]` block covers every repo.
Bind it to the repo you work in most, or omit `--db` and pass `repo` per call.

## 3. Verify the wiring end to end

Wiring is real only when a read tool returns data. Check each server two ways: the CLI's own
health check, and one live tool call.

### brigade-code-graph

The graph must exist and be fresh. From the repo:

```bash
brigade-code-graph --db .brigade/code-graph/code-graph.db doctor .
```

Healthy output ends with a `FRESH` verdict:

```
repo: root=/abs/path/to/repo db=/abs/path/to/repo/.brigade/code-graph/code-graph.db
version: tool=0.4.0 schema=9/9 needs_migration=false
last_sync: synced_at=1784324912 age_seconds=373
pending: new_files=0 changed_files=0 deleted_files=0 fingerprint_stale=0
verdict: FRESH
```

Then call a read tool from the harness. Ask the agent to run the `impact` tool, or from the
CLI:

```bash
brigade-code-graph --db .brigade/code-graph/code-graph.db callers serve
```

```
main --calls@21 hops=1--> serve  (src/bin/graphtrail-mcp.rs -> src/mcp.rs)
```

A row per caller with a resolved edge and source path is a healthy response. Over MCP, the
`repos` tool lists indexed databases the server can reach. It has no CLI equivalent.

### brigade-evidence

```bash
brigade-evidence doctor --mcp --json
```

Every check reports `"ok": true`, and the MCP checks confirm the protocol initializes and
tools register:

```json
{ "name": "paths", "ok": true, "detail": "/ABSOLUTE_ACCOUNT_HOME/.local/share/brigade/evidence/evidence.db" }
{ "name": "schema", "ok": true, "detail": "version 5" }
{ "name": "fts", "ok": true, "detail": "sqlite fts5" }
{ "name": "mcp_initialize", "ok": true }
{ "name": "mcp_tools", "ok": true, "detail": "tools=5" }
```

Then a live query:

```bash
brigade-evidence search "outcome ranking" --limit 3
```

```
70b9c578193d6a3d [claude/tool_call] ...[outcome] rank: ... [ranking]: none ...
f59c0a366ae18a87 [claude/tool_call] ...print("[ranking]: none") ...
```

An ID plus a source tag and a matched snippet per row is healthy. An empty result means the
archive has no matching items yet, not that the wiring is broken (see failure modes).

## 4. When to reach for each

Both are read-first tools consulted before an edit or a claim, not after.

### brigade-code-graph: before a non-trivial change

Reach for brigade-code-graph when the question is about relationships between symbols, not text: who
calls this, what does it call, what breaks if I change it, what changed between two versions.
Use it before touching control flow, a signature, a data structure, or anything imported by
more than one module. Tools: `callers`, `callees`, `impact`, `affected`, `context`, `diff`,
`doctor`, `search`, `neighbors`, `outline`, `cycles`, `dead_code`, `explain`, `stats`, `repos`.

Example. Before changing `serve`, get its blast radius:

```bash
brigade-code-graph --db .brigade/code-graph/code-graph.db impact serve --depth 2
```

```
Serve --calls@8 hops=1--> Normalize  (go/service.go -> go/service.go)
main --calls@21 hops=1--> serve  (src/bin/graphtrail-mcp.rs -> src/mcp.rs)
serve --calls@91 hops=1--> handle_request  (src/mcp.rs -> src/mcp.rs)
serve_returns_parse_error_for_invalid_json --calls@749 hops=1--> serve  (tests/mcp.rs -> src/mcp.rs)
```

Paste the impact set (the actual symbol and test names) into your reasoning before the first
edit.

### brigade-evidence: before relying on a claim

Reach for brigade-evidence before you trust a claim about past work, a run, a receipt, or a number.
It searches the local archive of agent sessions and imported receipts and returns them as
untrusted evidence. Tools: `search_evidence`, `show_item`, `list_sources`,
`create_evidence_bundle`, `show_evidence_bundle`.

Example. Before asserting how ranking behaves today, find the evidence:

```bash
brigade-evidence search "outcome ranking" --limit 3
```

```
70b9c578193d6a3d [claude/tool_call] ...[outcome] rank: ... [ranking]: none ...
```

Then open the full item and cite its ID:

```bash
brigade-evidence show 70b9c578193d6a3d
```

If the search returns nothing, write "no brigade-evidence evidence" rather than asserting from
memory.

Wiring alone does not create this habit. The before-change consultation is realized only when
changes flow through Brigade write-workers (which attach code-graph and evidence briefs
automatically), or when an `AGENTS.md` rule mandates a brigade-code-graph impact check before
non-trivial edits and a brigade-evidence evidence check before relying on a claim. Installed but
unprompted, the tools stay dormant while the dashboards look green.

## 5. Common failure modes

**Server not on PATH.** The harness launches the command with a minimal environment and cannot
find `brigade-code-graph-mcp` or `brigade-evidence`. Symptom: the server never registers, tools are
uncallable. Fix: use an absolute `command` path (`which brigade-code-graph-mcp`, `which brigade-evidence`)
rather than a bare name.

**Wrong database path (brigade-code-graph).** `--db` points at a path with no graph, so every call
returns empty or errors. The db is opened lazily, so the server still starts, which hides the
problem. Fix: confirm the file exists and `brigade-code-graph --db <path> doctor .` reports the repo
you expect. Or drop the default and pass `repo`/`db` per call.

**Repo not synced or indexed (brigade-code-graph).** The `.brigade/code-graph/code-graph.db` file does not
exist, or the repo was never indexed. `callers`/`impact` return nothing. Fix:

```bash
brigade-code-graph --db .brigade/code-graph/code-graph.db init .
brigade-code-graph --db .brigade/code-graph/code-graph.db sync .
```

**Archive empty (brigade-evidence).** `search` returns no rows because nothing has been crawled or
imported yet. Fix:

```bash
brigade-evidence init
brigade-evidence crawl sessions
```

Brigade also imports run receipts into the archive, so a wired repo fills over time.

**Stale index (brigade-code-graph).** `doctor` reports `STALE` when source files changed after the
last sync (`pending` shows nonzero `new_files`/`changed_files`, or `fingerprint_stale`). Query
answers are then out of date. Fix by re-syncing before you trust `impact`/`callers`:

```bash
brigade-code-graph --db .brigade/code-graph/code-graph.db sync .
brigade-code-graph --db .brigade/code-graph/code-graph.db doctor .   # expect FRESH
```

A MemoryMax cap on the brigade-code-graph wrapper turns a runaway sync into a killed process instead of
a frozen machine. See the wrapper in section 1.
