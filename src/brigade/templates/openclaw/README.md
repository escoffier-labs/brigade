# OpenClaw Fragments

These are JSON fragments meant to be inspected and merged by hand into your `~/.openclaw/openclaw.json`. `brigade` does not mutate your live OpenClaw config; it generates fragments and lets you review them first.

## Files

| Fragment | Purpose |
|----------|---------|
| `model-aliases.openclaw.json` | Suggested alias map under `agents.defaults.models`. |
| `ollama-memory-search.openclaw.json` | Local Ollama embeddings for memory search. |
| `acp-escalation.openclaw.json` | ACP escalation lane via the `acpx` plugin. |
| `memory-sweep-cron.openclaw.json` | Nightly memory sweep cron stub plus chat-sweep JSON output guidance. |

## Merge

`jq` is the safest way to merge a fragment into a live config without losing surrounding keys:

```bash
# Inspect first
jq . brigade-fragments/model-aliases.openclaw.json

# Merge (replace MERGE_PATH and re-check before saving)
jq -s '.[0] * .[1]' ~/.openclaw/openclaw.json brigade-fragments/model-aliases.openclaw.json \
  > /tmp/openclaw.json.merged
diff ~/.openclaw/openclaw.json /tmp/openclaw.json.merged
mv /tmp/openclaw.json.merged ~/.openclaw/openclaw.json
```

## Verification

```bash
brigade doctor --target ~/.openclaw/workspace --harness openclaw
```

The doctor reports which fragments your live config has picked up and which checks still need manual work.

## Gotchas

- Aliases referencing `<provider/...>` placeholders must be replaced with real ids before merging.
- The ACP fragment assumes you have already installed `acpx` (see [solos-cookbook/ai-stack/acp-claude-code.md](https://github.com/solomonneas/solos-cookbook) for the install path).
- `openclaw doctor` (the OpenClaw tool, not `brigade doctor`) has historically rewritten `openai-codex/*` prefixes on certain versions. If you use OAuth-only auth, audit `agents.defaults.model.primary` after any OpenClaw upgrade.
- Stagger ingest and sweep cron minutes around OpenClaw update windows. Avoid running memory ingest at the same time as updater jobs.
