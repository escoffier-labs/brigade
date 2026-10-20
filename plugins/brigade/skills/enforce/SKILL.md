---
name: enforce
description: Use when the user asks to turn on Brigade's full work loop (verification capture, raw-check blocking, and the handoff-before-stop gate) in the current repository.
---

# Turn on the Brigade work loop

The Brigade plugin starts in memory-first mode. This skill wires the current repository into Brigade's enforced loop.

1. Run `brigade --version`. If the command is missing, tell the user to install the CLI with `pipx install brigade-cli` or `npm install -g @escoffier-labs/brigade`, then stop.
2. Use `claude` as the harness name in Claude Code and `codex` in Codex.
3. Show the user this command and explain it before running it. It writes `.brigade/` into the repository, installs that harness's Brigade hooks, and adds local git excludes so none of it is committed:

   `brigade init --target . --depth repo --harnesses <harness> --owner <harness> --git-exclude`

4. After the user agrees, run it. Then run `brigade work brief --target .` and summarize the brief.
5. Tell the user the enforced hooks take effect in the next session.
