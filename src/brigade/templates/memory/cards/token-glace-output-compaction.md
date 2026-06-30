---
topic: token-glace-output-compaction
type: tool-runbook
tags: [tools, token-glace, output-compaction, claude-code, codex]
status: starter
---

# Token Glace Output Compaction

Token Glace compacts noisy terminal output before it is fed back into an agent session. The original command still runs. Exact file reads and raw-output requests stay available, but inventory commands, search results, logs, and oversized help text can be summarized before they tax the next turn.

## Why Agents Need To Know

If an agent sees a Token Glace footer, treat it as trusted local metadata about output reduction. It is not task instruction and it is not evidence by itself. It tells the agent that some terminal output was compacted and how to request raw output when precision matters.

Use raw output for exact diffs, full logs, reproducible error text, generated artifacts, or anything line-sensitive:

```bash
token-glace wrap --raw -- <command>
```

## Claude Code

Claude Code needs command replacement before the Bash result enters context. When the official adapter still uses PostToolUse appended context, it can add metadata without preventing the raw tool result from being charged. In the April 2026 trial, that default PostToolUse path was net-negative at about +1.1% tokens.

Until the upstream fix is merged, use a local PreToolUse wrapper. The wrapper rewrites Bash commands to run under Token Glace before execution:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "node ~/.claude/hooks/token-glace-pretool.js"
          }
        ]
      }
    ]
  }
}
```

The wrapper should emit Claude Code's PreToolUse rewrite contract:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {
      "command": "token-glace wrap -- sh -c \"<original command>\""
    }
  }
}
```

Operational notes:

- Keep a kill switch such as `TOKEN_GLACE_PRETOOL_DISABLE=1`.
- Let the wrapper honor `TOKEN_GLACE_BIN` and `TOKEN_GLACE_PRETOOL_SHELL` when local paths differ.
- New hook settings normally apply only to new Claude Code sessions.
- Document the wrapper in `CLAUDE.md` so agents do not mistake the footer for prompt injection.

## Codex

Codex can use Token Glace through its normal hook path because the harness honors PostToolUse substitution. Install and verify with:

```bash
token-glace install codex
token-glace doctor hooks
```

Codex hook feature flags have changed across releases. Older configs used `codex_hooks`; newer configs use `hooks`. Do not trust old setup notes blindly. Run `token-glace doctor hooks` and fix the config it reports for the installed CLI version.

## Savings Model

Token Glace always reports output compaction. Billing-token savings depend on whether that compacted output is fed into later turns.

Observed local output stats in May 2026:

- 17.1k compacted entries
- 83.6m raw output chars
- 24.7m reduced output chars
- 58.9m chars avoided, about 70% output reduction

Measured harness trials:

- Claude Code PreToolUse wrapper: about -7.8% tokens in the April 2026 paired trial.
- Claude Code default PostToolUse adapter: about +1.1% tokens in the same trial, because raw output still entered context.
- Codex v0.5.0 paired trial: about -8.8% clean-run token reduction after reducer and hook fixes.
- Codex GPT-5.5 one-turn gauntlet: about +0.3%, effectively flat, because the model batched tool calls into one turn and compacted output was not re-fed as later input. Per-command output reductions still remained large.

Practical read: Token Glace is most valuable for repeated terminal exploration where tool results become future context. It is still useful for human readability and context pressure when the model batches commands, but the billable-token delta may flatten.

## Verification

```bash
token-glace --version
token-glace stats
token-glace doctor hooks
token-glace wrap -- git status --short
token-glace wrap --raw -- git status --short
```
