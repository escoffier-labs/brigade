<p align="center">
  <img src="docs/assets/brigade-wordmark.svg" alt="Brigade (by Escoffier Labs)" width="640">
</p>

<p align="center">
  <strong>Coordinate your agents. Keep the tools you use.</strong><br>
  Brigade works alongside Codex, Claude Code, Cursor, OpenCode, T3 Code, Grok Bot, OpenClaw, Hermes, Dot, and Duet. It adds coordination, shared context, and checkable work records to your existing setup.
</p>

<p align="center">
  <a href="https://brigade.tools/docs/getting-started/install">Install</a> ·
  <a href="https://brigade.tools/docs">Docs</a> ·
  <a href="#works-with-your-existing-setup">Integrations</a> ·
  <a href="docs/comparison.md">Compare</a> ·
  <a href="QUICKSTART.md">Quickstart</a>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/brigade.svg?workflow=ci.yml&branch=main&label=ci&size=xs" alt="CI status">
  <img src="https://shieldcn.dev/pypi/v/brigade-cli.svg?label=pypi&size=xs" alt="PyPI version">
  <img src="https://shieldcn.dev/https/raw.githubusercontent.com/escoffier-labs/brigade/download-metrics/downloads.json.svg?size=xs" alt="Combined PyPI and npm downloads per month">
  <img src="https://shieldcn.dev/badge/python-3.10+-blue.svg?logo=python&logoColor=white&size=xs" alt="Python 3.10+">
  <img src="https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs" alt="Rust: code graph engine">
  <img src="https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs" alt="Go: evidence log engine">
  <img src="https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs" alt="MIT license">
</p>

Keep using your preferred agent tools. Start with one tool in one repository, then connect more agents and machines as you need them.

**Stable:** v0.30.0 on PyPI and npm. **Beta:** main is the 0.31 preview line (`0.31.0.devYYYYMMDD` wheels). Beta features and work in progress are labeled below. See the [changelog](CHANGELOG.md).

## Give the next agent more than a conversation

You investigate a bug in Claude Code, implement a fix in Codex, and return to the work through T3 Code. Brigade gives that work continuity:

1. **Start with context.** Read the work brief, relevant memory, and existing evidence.
2. **Make ownership explicit.** Claim tasks and inspect dependencies or conflicting work.
3. **Run within your rules.** Use configured model access, scopes, budgets, and approval gates for Brigade-managed execution.
4. **Check the result.** Run verification and keep the command, exit code, logs, and Git state.
5. **Hand off the work.** Leave the result, remaining blockers, and evidence where another agent can inspect them.

Agents can continue within the authority you give them. You choose where approval is required and can inspect or stop Brigade-managed work.

## What Brigade adds

### Work that agents can pick up

Tasks, dependencies, ready-work views, claims, and handoffs make the next action discoverable. Fleet Hub adds shared coordination across enrolled machines, including ownership views, presence, messaging, and model policy.

**0.31 beta:** Dispatch ready tasks into separate worktrees, supervise children, and retain results for review. Watch mode fills available capacity within configured limits. A successful worker run leaves work for review and landing.

### Evidence you can inspect

Run a check through Brigade:

```bash
brigade work verify run --target . --command "pytest -q"
```

The verification record captures the command and its actual exit code, logs, and Git state. Run journals and lineage connect execution to its context. Evidence packages let another agent inspect a selected verification record and check its integrity.

A passing command establishes the result of that check. Completion still depends on the work's acceptance criteria.

### Context that travels between tools

Keep a designated memory owner, with handoffs from the tools doing the work. Brigade provides handoff linting, ingestion coverage, recall, and memory-care checks. Shared skill and tool catalogs can project reviewed configuration into supported tools.

**0.31 beta:** Memory reflection derives evidence-backed candidates and can create consolidation proposals for review. It does not automatically rewrite claims.

### Visibility while agents work

Inspect runs, work ownership, machines, and cloud tasks through Brigade's CLI and operator views. Fleet messaging lets enrolled agents exchange information under explicit grants.

**0.31 beta:** Run patrol identifies local lane states and offers explicit repair actions. The Command Deck and CI-board integrations continue to expand.

## Works with your existing setup

Choose the integrations you need. Each connects a different part of the workflow.

| Your system | Brigade connection |
| --- | --- |
| **Codex, Claude Code, Cursor, OpenCode** | Workspace instructions, handoff inboxes, and supported tool and skill configuration. Execution adapters and hooks vary by tool. |
| **T3 Code** | Remote execution integration and session visibility. The complete app-owned child-task governance flow is in progress. |
| **Grok Bot** | Role-scoped connector packs for bounded jobs and configured workflows. |
| **OpenClaw, Hermes** | Memory-owner workflows, writer handoffs, workspace setup, and memory care. |
| **Dot** | A connector for memory, skills, Hub messaging, and authorized T3 fleet dispatch. |
| **Duet** | **0.31 beta:** A connector for memory, skills, Hub messaging, and read-only fleet views. Duet executes on its own cloud machine. |

Capabilities differ by integration, configuration, and release channel. Hosted connectors need explicit setup. See the [technical guide](docs/technical-guide.md), [Dot guide](docs/grokbot-dot.md), and [Duet guide](docs/duet-connector.md).

## Start with one repository

With Node.js 22 or newer on glibc Linux x64/ARM64, macOS x64/ARM64, or Windows x64:

```bash
npm install --global @escoffier-labs/brigade@0.30.0
brigade setup
```

The npm package manages its own Python environment. The first launch downloads a managed Python, so it needs network access. Leave optional dependencies enabled, and install Git for repository features. Upgrade npm-managed installs with npm. See the [npm package notes](npm/feasibility/README.md).

Or use Python 3.10 or newer with `pipx install brigade-cli` or `uv tool install brigade-cli`, followed by `brigade setup`.

Preview the setup for a repository you control:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex,claude --dry-run
```

Apply the setup, then read the first brief:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex,claude
brigade work brief --target ./my-repo
```

Select the tools you use. The local setup can be used without a Fleet Hub. Add fleet coordination or hosted-agent connectors when you need them.

[First 10 minutes](docs/first-10-minutes.md) · [Adopt an existing setup](docs/agent-assisted-setup.md) · [Stable and beta channels](docs/update-channels.md)

## Where the beta is heading

- **Independent work across a fleet.** Shared ownership, dependencies, and evidence help agents recover their next action without reconstructing a conductor's conversation. [Fleet coordination](https://github.com/escoffier-labs/brigade/issues/1121)
- **Governed delegation through T3 Code.** Explicit batches, independently addressable child results, and isolated workspaces under Brigade's existing controls. [Delegation epic](https://github.com/escoffier-labs/brigade/issues/2046)
- **Cross-repository campaigns.** Track one assignment across repositories with bounded ownership and evidence-backed completion. Execution and completion wiring remain in progress. [Campaigns](https://github.com/escoffier-labs/brigade/issues/1128)
- **More ways for agents to connect.** Encrypted messaging, teammate invitations, no-hub delivery, and Hub Lite are being built in stages. [Mesh design](docs/phase-agent-mesh.md)

## MCP servers

Brigade includes a memory MCP server with bounded recall, work briefs, and handoff drafting, plus a read-only skills MCP server:

```bash
brigade memory serve-mcp --stdio
brigade skills serve-mcp --stdio
```

The memory server's `write_handoff` tool drafts a handoff and never edits a canonical card. [MCP transport details](docs/technical-guide.md#streamable-http-for-the-mcp-servers).

<!-- MCP Registry ownership markers; keep each on its own line. -->
<!-- mcp-name: io.github.escoffier-labs/brigade-memory -->
<!-- mcp-name: io.github.escoffier-labs/brigade-skills -->

## Reference

[Commands](docs/command-inventory.md) · [Execution model](docs/execution-model.md) · [Comparisons](docs/comparison.md) · [Contributing](CONTRIBUTING.md) · [Report an issue](https://github.com/escoffier-labs/brigade/issues) · [MIT license](LICENSE)

The Python CLI may send a passive version request. [Python CLI telemetry](docs/telemetry.md) lists its fields, retention, and opt-outs.

<details>
<summary>About the downloads badge</summary>

The downloads badge sums `brigade-cli` on PyPI (excluding mirrors) and `@escoffier-labs/brigade` on npm over the same 30 completed UTC days. Downloads include automation and do not count unique people. The [metric JSON](https://raw.githubusercontent.com/escoffier-labs/brigade/download-metrics/downloads.json) records each source, the period, and the last successful refresh. npm platform companions are excluded. Daily refreshes retain the previous metric if a source fails validation or its latest day is more than three UTC days old.

</details>

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), npm [`@escoffier-labs/brigade`](https://www.npmjs.com/package/@escoffier-labs/brigade), command `brigade`.
