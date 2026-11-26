<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/brigade-wordmark-dark.svg">
    <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/brigade-wordmark.svg" alt="BrigadeStack by Escoffier Labs" width="640">
  </picture>
</p>

<h1 align="center">Run agents anywhere. Same memory everywhere.</h1>

<p align="center">
  <a href="https://brigadestack.com">Website</a> ·
  <a href="https://brigadestack.com/docs">Docs</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/QUICKSTART.md">Quickstart</a> ·
  <a href="https://brigadestack.com/compare">Compare</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://github.com/escoffier-labs/brigadestack/actions/workflows/ci.yml?query=event%3Amerge_group"><img src="https://github.com/escoffier-labs/brigadestack/actions/workflows/ci.yml/badge.svg?event=merge_group" alt="CI status of the merge queue"></a>
  <a href="https://pypi.org/project/brigadestack/"><img src="https://shieldcn.dev/pypi/v/brigadestack.svg?label=pypi&size=xs" alt="PyPI version of brigadestack"></a>
  <img src="https://shieldcn.dev/https/raw.githubusercontent.com/escoffier-labs/brigadestack/download-metrics/downloads.json.svg?size=xs" alt="Combined PyPI and npm downloads per month">
  <img src="https://shieldcn.dev/badge/python-3.11+-blue.svg?logo=python&logoColor=white&size=xs" alt="Python 3.11+">
  <img src="https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs" alt="Rust: code graph engine">
  <img src="https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs" alt="Go: evidence log engine">
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/LICENSE"><img src="https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs" alt="MIT license"></a>
</p>

BrigadeStack gives the agent tools you already use one shared memory and one set of MCP servers and skills. It keeps a record of the work they check.

Install with pipx (Python 3.11 or newer) or npm (Node.js 22 or newer). The command is `brigadestack`. `brigadestack` on PyPI is the Python package. The `brigade-cli` PyPI package (the same code under the old name) and the `@escoffier-labs/brigade` npm compatibility package install the same CLI as `brigade`, so existing users keep their command and `pipx upgrade brigade-cli` keeps working.

```bash
pipx install brigadestack
npm install -g brigadestack
```

Install only one npm package globally. `brigadestack` and `@escoffier-labs/brigade` both link `brigade`, so npm stops with `EEXIST` on the second one. If `@escoffier-labs/brigade` is installed and you want the new name, run `npm uninstall --global @escoffier-labs/brigade` first, then `npm install --global brigadestack`. Keeping `@escoffier-labs/brigade` and running `npm update --global @escoffier-labs/brigade` also works.

After `pipx upgrade brigadestack` or `npm update --global brigadestack` on stable, run `brigade setup` to match the managed engines to the CLI. With v0.30.0 engines and CLI 0.31.0 or 0.32.0, captured verification can report `miseledger indexing: import failed (receipts pending)`. See [Repair managed engines](docs/update-channels.md#repair-managed-engines) for beta and unreleased source builds.

Sections marked <sup>*</sup> describe the 0.32.0 beta. It is published as daily pre-release builds of `brigadestack`, which install the `brigade` command: `pipx install --pip-args='--pre' brigadestack`. Stable is 0.31.0.

## Shared memory, shared tools

Claude Code, Codex and Cursor each keep their own notes and MCP config, so what one agent learns on Monday is gone when another starts on Tuesday.

BrigadeStack keeps memory as markdown cards in your repository. An agent that learns something durable writes a handoff. A reviewer files it as a card. Commit the card, and any checkout finds it with `brigadestack memory search`.

Claude Code also gets recall at session start. Codex gets memory through its handoff inbox and an agent-run `memory search`.

<p align="center">
  <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/readme-shared-memory.svg" alt="Claude Code, Codex, Cursor, OpenCode, ChatGPT dots and Claude Duet connect to BrigadeStack shared memory, and share memory, check records, MCP servers and skills through it." width="720">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/readme-handoff-flow.svg" alt="Three steps: one session drafts a handoff, a reviewer files it as the card payments-retry-policy.md, and brigade memory search in a second checkout finds that card." width="720">
</p>

<details>
<summary>The commands behind that picture</summary>

This is real output, with the home directory shortened to `~`.

```console
$ cd ~/api
$ brigadestack handoff draft --type decision \
    --title "Payments client retry policy" \
    --summary "Retries stop after 3 attempts with jittered backoff." \
    --fact "Backoff starts at 200 ms and doubles." \
    --fact "A 4xx response is never retried." \
    --action create-card --target-card payments-retry-policy.md \
    --content-file card.md
handoff draft: ~/api/.codex/memory-handoffs/20261118-014553-payments-client-retry-policy.md
id: 20261118-014553-payments-client-retry-policy
inbox: .codex/memory-handoffs
action: create-card
target_card: payments-retry-policy.md
lint: ok
$ cp card.md memory/cards/payments-retry-policy.md
$ git add memory && git commit -qm "memory: payments retry policy"
$ git worktree add -q ../api-2 -b second && cd ../api-2
$ brigadestack memory search "retry backoff"
memory search: retry backoff (1 match(es))
- memory/cards/payments-retry-policy.md  [0.74]  payments-retry-policy
```

</details>

### One list of MCP servers and skills

MCP servers and skills follow the same rule as memory. List a server once in `.brigade/mcp.json`, and `brigadestack mcp sync` writes it into each set-up tool's config, through adapters for Claude Code, Codex, Cursor, OpenCode and Grok. Antigravity needs `--user-scope`, and VS Code is included when the repository has a `.vscode` folder. It previews until `--write` and keeps Codex and OpenCode native auth<sup>*</sup>. `brigadestack skills sync --target all` previews the same way for skills.

<sup>*</sup> In the 0.32.0 beta, sync also keeps Claude Code's own auth helper and refuses to remove or re-point an entry that has one.

<p align="center">
  <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/readme-shared-tools.svg" alt="The MCP catalog .brigade/mcp.json and the skill registry are each kept once, then written to the config and skills folder of Claude Code, Codex, Cursor and OpenCode." width="720">
</p>

## Then prove it

"Tests pass" is a claim. `brigadestack work verify run` turns it into a record. It runs the command and keeps the command, the exit code, the logs and a fingerprint of the tree that was tested. Another agent can open it and see what ran against which code.

Records live under `.brigade/work/verify-runs/`, one folder per run. A failing command leaves a failed record too.

<p align="center">
  <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/readme-check-record.svg" alt="A check record: the command python -m pytest -q, exit code 0, the two log files, and the fingerprint of the tree that was tested." width="720">
</p>

### Know what a change touches

The code graph is a local index of your repository's symbols and the calls between them. `brigadestack setup` installs the optional engine, and `brigadestack code sync` builds the index. `brigadestack code callers <symbol>` shows who calls a function, `impact` adds callees, and `affected <files>` lists the tests that reach changed files. Below is a real answer for one function here.

<p align="center">
  <img src="https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/readme-code-graph.svg" alt="Code graph for the function normalized_url in src/brigade/mcp_native_auth.py, lines 201 to 208. Its direct callers are merge_imported_native_auth and two tests. merge_imported_native_auth is called by import_servers and two more tests. In total 6 functions in 3 files call it, 4 of them tests." width="720">
</p>

## Modes<sup>*</sup>

`strict` is the default. Checks run through `brigadestack work verify run`, and Claude Code's stop hook asks for a record before a session finishes. `observe` records checks and blocks nothing. `off` drops the check rules and keeps memory and briefs. Safety rails (secret scanning and pre-push checks) are a separate setting, on by default in every mode.

<sup>*</sup> `observe` and the names `strict` and `off` are in the 0.32.0 beta, as the capture shows. Version 0.31.0 calls them `engineer` and `yolo` and has no `observe`. The beta still accepts the old names.

```console
$ brigade work mode show
work mode: strict; safety rails: on (~/api)
$ brigade work mode set observe
work mode: observe; safety rails: on (~/api)
$ brigade work mode set off
work mode: off; safety rails: on (~/api)
```

## Quick start

Wire a repository, write a first handoff, and run a first check. Add `--dry-run` to the first command to preview it.

```bash
brigadestack operator quickstart --target . --harnesses codex,claude
brigadestack handoff draft --type setup --title "Test command" \
  --summary "Run pytest -q from the repo root before every push." \
  --action no-card --target-document TOOLS.md \
  --content "Run pytest -q from the repo root."
brigadestack work verify run --command "git diff --check"
```

The first command writes `.brigade/`, a handoff inbox for each tool you name, and the MCP config. The second drafts a handoff, and lint refuses what the ingester would reject. The third records the command, exit code and logs. Keep `.brigade/` state out of commits unless you mean to share it. See the [first 10 minutes](https://github.com/escoffier-labs/brigadestack/blob/main/docs/first-10-minutes.md).

## Works with

| Tool | What you get |
| --- | --- |
| Claude Code, Codex, Cursor, OpenCode | Workspace instructions, handoff inboxes, and MCP and skill config. |
| ChatGPT Dot | Memory, skills, Hub messages and T3 fleet dispatch, through a connector you host. |
| Claude Duet | Memory, skills, Hub messages and read-only fleet views, through a connector you host. |
| T3 Code | Session presence only. |

Setup guides: [Dot](https://github.com/escoffier-labs/brigadestack/blob/main/docs/grokbot-dot.md) and [Duet](https://github.com/escoffier-labs/brigadestack/blob/main/docs/duet-connector.md).

## Across machines

Agents on several machines share memory through a hub service you run on a network you control. A card pushed from a laptop turns up in a search on a desktop. Task ownership, messages and model policy live in the same place. The [fleet docs](https://github.com/escoffier-labs/brigadestack/blob/main/docs/fleet-sync.md) cover setup.

## Limits

A check record shows what ran. It does not show that the tests are good. A handoff is a draft until someone files it as a card. The packaged hub and the Dot listener are Linux only. Dot and Duet are not woken instantly: each checks for work when a session or scheduled run starts.

## MCP servers

- `brigadestack memory serve-mcp --stdio` serves memory cards, the work brief and `write_handoff`, which drafts a handoff and never edits a card.
- `brigadestack skills serve-mcp --stdio` serves the reviewed skill registry, read-only: search, read and lint skills.

<!-- MCP Registry ownership markers; keep each on its own line. -->
<!-- mcp-name: io.github.escoffier-labs/brigade-memory -->
<!-- mcp-name: io.github.escoffier-labs/brigade-skills -->

<p align="center">
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/docs/command-inventory.md">Commands</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/docs/execution-model.md">Execution model</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/CONTRIBUTING.md">Contributing</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/issues">Issues</a> ·
  <a href="https://github.com/escoffier-labs/brigadestack/blob/main/LICENSE">MIT license</a>
</p>

Questions and ideas: [Discussions](https://github.com/escoffier-labs/brigadestack/discussions).

BrigadeStack is built by [Escoffier Labs](https://escoffierlabs.dev). Principal engineer: [Solomon Neas](https://solomonneas.dev).

The Python CLI may send a passive version request. [Telemetry](https://github.com/escoffier-labs/brigadestack/blob/main/docs/telemetry.md) lists its fields and opt-outs.

<details>
<summary>About the downloads badge</summary>

The downloads badge sums `brigadestack` and `brigade-cli` on PyPI (excluding mirrors) and `brigadestack` and `@escoffier-labs/brigade` on npm over the same 30 completed UTC days. Downloads include automation and do not count unique people. The [metric JSON](https://raw.githubusercontent.com/escoffier-labs/brigadestack/download-metrics/downloads.json) records each source, the period, and the last successful refresh. npm platform companions are excluded. Daily refreshes retain the previous metric if a source fails validation or its latest day is more than three UTC days old.

</details>
