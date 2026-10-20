---
name: enforce
description: Use when the user asks to turn on Brigade's full work loop (verification capture, raw-check blocking, and the handoff-before-stop gate) in the current repository.
---

# Turn on the Brigade work loop

The Brigade plugin starts in memory-first mode. This skill wires the current repository into Brigade's enforced loop.

1. Run `brigade --version`. If the command is missing, tell the user to install the CLI with `pipx install brigade-cli` or `npm install -g @escoffier-labs/brigade`, then stop.
2. Use `claude` as the harness name in Claude Code and `codex` in Codex.
3. Show the user this command and explain it before running it:

   `brigade init --target . --depth repo --harnesses <harness> --owner <harness> --git-exclude`

   It writes `.brigade/`, the `brigade-work` skill, AGENTS guidance, and a handoff inbox into the repository. In Claude Code it also installs the enforced hooks (verification capture, raw-check blocking, and the handoff-before-stop gate) into `.claude/settings.json`. Codex gets the skills and AGENTS guidance but no hooks. `--git-exclude` keeps Brigade's own files out of git through local excludes. Tell the user that `.claude/settings.json` may already be tracked in their repo, in which case the hook entries show up as a change there.

4. After the user agrees, run it. Then run `brigade work brief --target .` and summarize the brief.
5. In Claude Code, tell the user the enforced hooks take effect in the next session.
