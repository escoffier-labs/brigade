# README coverage map

Migration date: 2026-08-13
README structure refreshed: 2026-10-26

The root README is a short landing page. Every topic that left the previous README has a durable home below. Claims intentionally retired from the front page are listed at the end.

## Topic mapping

| Old README topic | Durable home |
| --- | --- |
| Kitchen scene / social banner / demo SVG | Kept as assets under `docs/assets/`, not linked from README. Brand metaphor: [overview](overview.md#mise-en-place-brand-metaphor) |
| Badge row (CI, PyPI, Python, Rust, Go, license) | Restored below the README's primary links |
| "The loop" five-step circuit | [overview](overview.md#verified-work-loop) |
| Agent paste-ready install prompt | [agents guide](agents-guide.md#paste-ready-install-prompt), [agent-assisted setup](agent-assisted-setup.md#paste-ready-install-prompt) |
| Shell install (`pipx`, `uv tool`, `brigade setup`, quickstart) | [QUICKSTART.md](../QUICKSTART.md), [first 10 minutes](first-10-minutes.md) |
| User-scope Claude Code work hooks | [agents guide](agents-guide.md#claude-code-user-scope-work-hooks), [agent-assisted setup](agent-assisted-setup.md#claude-code-user-scope-work-hooks) |
| Update channels (stable vs beta) | [update channels](update-channels.md) |
| Best-effort update check and `BRIGADE_NO_UPDATE_CHECK=1` | [update channels](update-channels.md) |
| Operator doctor profile, `ready: yes`, and default local footprint | [first 10 minutes](first-10-minutes.md), [new-user quickstart](new-user-quickstart.md), [agents guide](agents-guide.md) |
| Homegrown setup adoption with `brigade operator adopt plan` | [agent-assisted setup](agent-assisted-setup.md#adapting-a-homegrown-setup), [technical guide](technical-guide.md) |
| Contributor first-PR path and local checks | [CONTRIBUTING.md](../CONTRIBUTING.md#your-first-pr) |
| Verify receipt example | [work closeout](work-closeout.md#abridged-receipt-example-schema_version-2) |
| Evidence log / receipt export | [evidence packages](evidence-package.md), [receipt schemas](receipt-schemas.md), [operator center](operator-center.md) |
| Code graph queries and impact | [code intelligence](code-intelligence.md) |
| Memory MCP tools, HTTP transport, and uvx launch | [memory MCP tools](agent-plugin.md#tools), [MCP transports](technical-guide.md#streamable-http-for-the-mcp-servers) |
| Bug-report triage | [bug reports](technical-guide.md#bug-reports) |
| npm trial with npx and first-launch network access | [npm package notes](../npm/feasibility/README.md) |
| MCP sync walkthrough and per-tool table | [mcp-sync](mcp-sync.md) |
| Shared memory workflow diagram | [memory care](memory-care.md), [memory operations](memory-operations.md), assets under `docs/assets/` |
| Outcome rank / capture / reconcile / explain | [outcome scoring](outcome-scoring.md) |
| Optional stations table (Pantry, Token Glace, Skillet, Bootstrap Doctor, notifications) | [overview](overview.md), [station contract](station-contract.md) |
| Short "why not something else" bullets | Detailed tables in [comparison](comparison.md), linked from the README |
| "What Brigade is not" / name collisions | [execution model](execution-model.md) and project identity in the README footer |
| "Why I built this" origin story | [overview](overview.md#why-this-exists), Cookbook link retained there |
| Public vs historical names | [overview](overview.md#mise-en-place-brand-metaphor), [technical guide](technical-guide.md) |
| Harness matrix | [technical guide](technical-guide.md) |
| Brigadeclaw Q&A pointer | [https://brigade.tools/brigadeclaw](https://brigade.tools/brigadeclaw) (site). Not required on README |
| Docs index and license | README Reference section |
| Project identity (GitHub, site, PyPI, command) | README identity footer. Also [overview](overview.md#project-identity) |

## Intentionally retired from the front page

- Product art, terminal demo, and memory-workflow illustrations as README chrome
- JSON receipt dumps and YAML/TOML configuration samples. Short install and verification commands remain in the README
- Autonomous or "reflective memory learning" claims
- Framing Brigade as a required daemon, hosted memory service, distributed task database, or full autonomous fleet runtime
- Claiming every Brigade command writes a receipt
- One-checkout performance snapshots, source line numbers, and outcome scores that become stale as the repository changes
- The volatile memory-card count from the origin story

## Stable vs 0.31 beta labeling

| Line | Meaning |
| --- | --- |
| Stable v0.30.0 | Published PyPI release `brigade-cli==0.30.0` and npm `@escoffier-labs/brigade@0.30.0` |
| Current main / 0.31 beta | Features on origin/main after v0.30.0 and `0.31.0.devYYYYMMDD` wheels. Labeled beside README capabilities and in this docs set |
