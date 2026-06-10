# First 10 Minutes With Brigade

This is the shortest path from install to a healthy local setup. It is meant for a repo or workspace you control.

## 1. Install

```bash
pipx install brigade-cli
brigade --version
```

Expected: `brigade X.Y.Z` matching the [latest release](https://pypi.org/project/brigade-cli/).

If `pipx` is missing, install it with your OS package manager or Python packaging tool, then rerun the command above. Brigade requires Python 3.10 or newer.

## 2. Pick A Target

For a code repo:

```bash
cd ./my-repo
```

For a scratch check:

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
cd "$target"
```

For an operator workspace such as an OpenClaw or Hermes home, use that workspace directory instead of a code repo.

## 3. Preview

For the current Codex-style setup:

```bash
brigade operator quickstart --target . --harnesses codex --dry-run
```

For multiple writer surfaces:

```bash
brigade operator quickstart --target . --harnesses codex,claude,opencode,antigravity,pi,cursor,aider,goose,continue,copilot,qwen,kimi,adal,openhands --dry-run
```

For an OpenClaw or Hermes workspace:

```bash
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw --dry-run
```

Quickstart's dry-run lists the planned steps. For the file-by-file preview of what would be written, use `brigade init --target . --harnesses codex --dry-run` (init is the template-install step that quickstart runs first). Neither writes anything, starts services, installs schedulers, publishes, pushes, tags, or mutates remotes.

## 4. Apply

Run the same command without `--dry-run`:

```bash
brigade operator quickstart --target . --harnesses codex
```

Expected shape:

```text
status: ok
```

Quickstart scopes handoff source coverage to the writer harnesses you selected. A `--harnesses codex` setup watches `.codex/memory-handoffs/` and leaves Claude, OpenCode, Hermes, and OpenClaw paths quiet until you add them.

## 5. Check Health

```bash
brigade operator doctor --target . --profile local-operator
brigade handoff doctor --target .
brigade security scan --target . --output-dir .brigade/security/latest
brigade security doctor --target .
```

Healthy first-run shape (the key lines to look for):

```text
ready: yes
blocking_issues: 0
```

from `operator doctor`, every `handoff doctor` line prefixed `[ok]`, `findings: 0` from the security scan, and every `security doctor` line `[ok]`. The doctors print one line per check, so expect a couple dozen `[ok]` lines rather than a one-line summary.

`operator doctor` may suggest `brigade daily plan --target .` as the next command. That is normal. It means setup is ready and Brigade can now show the local daily loop.

## 6. Handoff Concepts In 60 Seconds

A handoff is a note an agent writes for the memory owner to file later. Four things decide where it lands:

- **Inbox**: each harness writes to its own folder (`.codex/memory-handoffs/` for Codex sessions, `.claude/memory-handoffs/` for Claude Code). Use the inbox matching the tool that learned the fact; for a note you are writing yourself, any selected inbox works - the ingester watches all of them.
- **Type** (`--type`): what kind of note this is - `decision`, `workflow`, `gotcha`, and more (`brigade handoff draft --help` lists the valid values). It helps the reviewer, not the router.
- **Action**: `no-card` (the default) appends a short fact to a shared document such as `.learnings/LEARNINGS.md` or `TOOLS.md`. `create-card`/`update-card` proposes a standalone memory card for bigger durable topics, and requires `--target-card` plus card content starting with YAML frontmatter.
- **Content** (`--content` or `--content-file`): the durable note itself, required. The title and summary are the envelope; the content is what gets filed.

When unsure, `no-card` with a two-sentence content is the right default. `brigade handoff-template` prints the full format.

One more concept: the **memory owner**. Quickstart auto-selects which harness owns durable memory (it prints the pick and `--owner` overrides it). In a code repo this mostly decides which tool's conventions the ingest docs assume; the auto-pick is fine until you have an opinion.

## 7. What To Commit

Usually safe to commit after review:

- `AGENTS.md`, `CLAUDE.md`, `INSTALL_FOR_AGENTS.md`, `SAFETY_RULES.md`
- `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`
- `tools/`
- `hooks/` (the pre-push content-guard hook; activate it with `git config core.hooksPath hooks`)
- public docs

Inbox folders stay local except each inbox's `TEMPLATE.md`, which is deliberately un-ignored so the handoff format travels with the repo. A `?? .codex/` in `git status` is just that template.

Usually local-only:

- `.brigade/`
- `.codex/`
- `.claude/`
- `.opencode/`
- `.antigravity/`
- `.pi/`
- `.cursor/`
- `.hermes/`
- `.openclaw/`
- `.mcp/`
- generated `scripts/` projections

If the target is a local operator workspace rather than a git repo, treat the same split as a backup rule: durable reviewed memory can be synced, generated host wiring should stay local.

## 8. If It Fails

Collect redacted machine-readable output:

```bash
brigade --version
brigade operator quickstart --target . --harnesses codex --json
brigade operator doctor --target . --profile local-operator --json
brigade operator verify-harness --target . --harness codex --json
brigade handoff doctor --target . --json
brigade tools doctor --target . --json
brigade skills doctor --target . --json
brigade security scan --target . --fail-on none --json
```

Review before sharing. Do not paste tokens, private hostnames, private repo names, or unredacted absolute paths into a public issue.

Open the Quickstart setup problem form:

```text
https://github.com/escoffier-labs/brigade/issues/new/choose
```

Include:

- exact commands
- `brigade --version`
- Python version
- OS
- selected harnesses
- the redacted `issue_report` object from quickstart JSON
