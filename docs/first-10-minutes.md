# First 10 Minutes With Brigade

This is the shortest path from install to a healthy local setup. It is meant for a repo or workspace you control.

## 1. Install

```bash
pipx install brigade-cli
brigade --version
```

Expected:

```text
brigade 0.8.1
```

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

Dry-run should show planned local files only. It should not start services, install schedulers, publish, push, tag, or mutate remotes.

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

Healthy first-run shape:

```text
operator doctor: ready yes
blocking issues: 0
handoff doctor: no warnings
security scan: findings 0
security doctor: issues 0
```

`operator doctor` may suggest `brigade daily plan --target .` as the next command. That is normal. It means setup is ready and Brigade can now show the local daily loop.

## 6. What To Commit

Usually safe to commit after review:

- `AGENTS.md`
- `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`
- `tools/`
- public docs

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

## 7. If It Fails

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
