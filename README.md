<p align="center">
  <img src="docs/assets/brigade-wordmark.svg" alt="Brigade (by Escoffier Labs)" width="640">
</p>

<p align="center">
  <strong>Coordinate your agents. Keep the tools you use.</strong>
</p>

Brigade is an open-source coordination layer for AI coding agents. It sits beside Codex, Claude Code, Cursor, OpenCode, T3 Code, and the rest, and gives them shared tasks, shared memory, and verification records any agent can check.

<p align="center">
  <a href="https://brigade.tools">Website</a> ·
  <a href="https://brigade.tools/docs">Docs</a> ·
  <a href="QUICKSTART.md">Quickstart</a> ·
  <a href="docs/comparison.md">Compare</a> ·
  <a href="CHANGELOG.md">Changelog</a>
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

**Stable:** v0.30.0 on PyPI and npm. **Beta:** main is the 0.31 preview line. [Channels](docs/update-channels.md)

## Why Brigade

You run several agents at once. Sol in Codex takes one task, Opus manages another, and Argon takes a third here and there, whether they share one window in a multi-harness tool like T3 Code or each run in its own app. Your Grok Bot runs the rest of your life, and your Dots spin up work of their own. Every agent starts from its own conversation. None of them knows who owns which task, what the others already tried, or whether a fix was actually checked.

Brigade holds that state outside any one tool. Agents read the same brief, claim the same tasks, and leave handoffs and evidence the next agent can inspect.

- **Keep your tools.** Works alongside the agents you already use. Start with one tool in one repository and add more when you need them.
- **Work agents can pick up.** Tasks, dependencies, claims, and handoffs make the next action discoverable without replaying a conversation.
- **Checkable results.** Verification records keep the command, exit code, logs, and Git state, so another agent can confirm "tests pass" for itself.
- **You set the limits.** Model access, scopes, budgets, and approval gates apply to Brigade-managed work, and you can inspect or stop it.

## How it works

1. **Start with context.** Agents begin from a work brief: relevant memory, open tasks, and existing evidence.
2. **Claim the work.** Ownership is explicit, and conflicting or dependent work is visible.
3. **Run within your rules.** Managed execution uses your configured models, scopes, budgets, and approvals.
4. **Verify the result.** Checks run through Brigade and leave a record another agent can inspect.
5. **Hand off.** The result, remaining blockers, and evidence stay where the next agent will find them.

A local setup needs no server. Fleet Hub adds shared ownership, presence, messaging, and model policy across machines when you want it. See the [execution model](docs/execution-model.md).

## Quick start

With Node.js 22 or newer (Linux glibc x64/ARM64, macOS, or Windows x64):

```bash
npm install --global @escoffier-labs/brigade@0.30.0
brigade setup
```

Or with Python 3.10+: `pipx install brigade-cli` (or `uv tool install brigade-cli`), then `brigade setup`.

Wire up a repository for the tools you use, then read the first brief:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex,claude --dry-run   # preview
brigade operator quickstart --target ./my-repo --harnesses codex,claude
brigade work brief --target ./my-repo
```

Run a check so the result is recorded:

```bash
brigade work verify run --target ./my-repo --command "pytest -q"
```

The npm package manages its own Python, so the first launch needs network access. Git is required for repository features. See the [npm package notes](npm/feasibility/README.md), [first 10 minutes](docs/first-10-minutes.md), and [adopting an existing setup](docs/agent-assisted-setup.md).

## Works with

| Tool | Connection |
| --- | --- |
| **Codex, Claude Code, Cursor, OpenCode** | Workspace instructions, handoff inboxes, and tool and skill configuration. Hooks vary by tool. |
| **T3 Code** | Remote execution and session visibility. Full child-task governance is in progress. |
| **Grok Bot** | Role-scoped connector packs for bounded jobs. |
| **OpenClaw, Hermes** | Memory-owner workflows, writer handoffs, and memory care. |
| **Dot** | Memory, skills, Hub messaging, and authorized T3 fleet dispatch. |
| **Duet** (0.31 beta) | Memory, skills, Hub messaging, and read-only fleet views. |

Hosted connectors need explicit setup. See the [technical guide](docs/technical-guide.md), [Dot guide](docs/grokbot-dot.md), and [Duet guide](docs/duet-connector.md).

## MCP servers

A memory server with bounded recall, work briefs, and handoff drafting, plus a read-only skills server:

```bash
brigade memory serve-mcp --stdio
brigade skills serve-mcp --stdio
```

`write_handoff` drafts a handoff and never edits a canonical card. [Transport details](docs/technical-guide.md#streamable-http-for-the-mcp-servers)

<!-- MCP Registry ownership markers; keep each on its own line. -->
<!-- mcp-name: io.github.escoffier-labs/brigade-memory -->
<!-- mcp-name: io.github.escoffier-labs/brigade-skills -->

## In the 0.31 beta

- **Parallel workers.** Dispatch ready tasks into separate worktrees, supervise them, and keep results for review.
- **Memory reflection.** Evidence-backed consolidation proposals for review. Nothing is rewritten automatically.
- **Run patrol.** Detects local lane states and offers explicit repair actions.
- **In progress:** [fleet coordination](https://github.com/escoffier-labs/brigade/issues/1121), [governed T3 delegation](https://github.com/escoffier-labs/brigade/issues/2046), [cross-repository campaigns](https://github.com/escoffier-labs/brigade/issues/1128), and the [agent mesh](docs/phase-agent-mesh.md).

## Reference

[Commands](docs/command-inventory.md) · [Execution model](docs/execution-model.md) · [Comparisons](docs/comparison.md) · [Contributing](CONTRIBUTING.md) · [Issues](https://github.com/escoffier-labs/brigade/issues) · [MIT license](LICENSE)

The Python CLI may send a passive version request. [Telemetry](docs/telemetry.md) lists its fields, retention, and opt-outs.

<details>
<summary>About the downloads badge</summary>

The downloads badge sums `brigade-cli` on PyPI (excluding mirrors) and `@escoffier-labs/brigade` on npm over the same 30 completed UTC days. Downloads include automation and do not count unique people. The [metric JSON](https://raw.githubusercontent.com/escoffier-labs/brigade/download-metrics/downloads.json) records each source, the period, and the last successful refresh. npm platform companions are excluded. Daily refreshes retain the previous metric if a source fails validation or its latest day is more than three UTC days old.

</details>

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), npm [`@escoffier-labs/brigade`](https://www.npmjs.com/package/@escoffier-labs/brigade), command `brigade`.
