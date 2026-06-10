# agent-notify

<p>
  <a href="https://github.com/escoffier-labs/agent-notify/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/escoffier-labs/agent-notify/ci.yml?branch=main&style=for-the-badge&label=ci" alt="CI status"></a>
  <a href="https://github.com/escoffier-labs/agent-notify/releases"><img src="https://img.shields.io/github/v/release/escoffier-labs/agent-notify?style=for-the-badge&label=release" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/go-1.22%2B-00ADD8?style=for-the-badge&logo=go&logoColor=white" alt="Go 1.22+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

A privacy-first notification dispatcher for AI coding agents and any other host process. Sends to Discord, Telegram, and Signal - no telemetry, no third-party push infrastructure, no Anthropic / OpenAI involvement.

Built for engineers running an always-on agent stack who want push notifications when the agent finishes or needs input, without routing real-time session activity through a vendor's notification service.

## Why this exists

If you set `DISABLE_TELEMETRY=1` to keep your agent harness from phoning home, you've also disabled the harness's built-in mobile push feature (which routes through the same telemetry plumbing). `agent-notify` gives you the same UX with zero data flow you didn't ask for: messages go from your machine directly to Discord's API, the Telegram Bot API, or your self-hosted Signal CLI - and nowhere else.

## Privacy posture

- No telemetry endpoints. Ever.
- No update checks at startup or runtime.
- No persistent state. No state file. No log file. No cache.
- Outbound HTTP only to channel URLs you configured.
- Test `cmd/agent-notify/privacy_test.go` asserts the above.

## Install

Install the latest tagged release with `go install`:

```bash
go install github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest
```

Or build from source:

```bash
git clone https://github.com/escoffier-labs/agent-notify.git
cd agent-notify
make install   # builds and copies to ~/bin/agent-notify
```

Prebuilt binaries (linux, macOS, windows for amd64 and arm64) plus a `checksums.txt` are attached to each [release](https://github.com/escoffier-labs/agent-notify/releases). Download the archive for your platform, verify the checksum, extract, and drop the binary in `~/bin/` or `/usr/local/bin/`:

```bash
tar -xzf agent-notify_*_linux_amd64.tar.gz
install -m 0755 agent-notify_*_linux_amd64/agent-notify ~/bin/agent-notify
```

Confirm the installed binary:

```bash
agent-notify version
```

## Quickstart (no config file)

Set env vars for the channel(s) you want and run:

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
agent-notify "hello from agent-notify"
```

The explicit subcommand form is equivalent:

```bash
agent-notify send "hello from agent-notify"
```

Multiple channels at once:

```bash
export DISCORD_WEBHOOK_URL='...'
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
agent-notify "build finished"   # fans out to both
```

## Config file (when you outgrow env-only)

Generate a starter config:

```bash
agent-notify init
```

Or create `~/.config/agent-notify/config.toml` manually:

```toml
[channels.tg-personal]
type = "telegram"
bot_token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env   = "TELEGRAM_CHAT_ID"

[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[channels.signal-personal]
type = "signal"
url_env  = "SIGNAL_CLI_URL"
from_env = "SIGNAL_FROM"
to_env   = "SIGNAL_TO"

[profiles.agent-stop]
channels = ["tg-personal", "discord-main"]
default  = true

[profiles.error]
channels = ["tg-personal", "discord-main", "signal-personal"]
prefix   = "🚨 "
```

Secrets stay in env vars (the config references env-var names, not literal tokens).

Validate the wiring without sending a live notification:

```bash
agent-notify status --json
agent-notify doctor
agent-notify doctor --json
```

## Routing precedence

1. `--to <names>` (explicit, comma-separated) - overrides everything else.
2. `--profile <name>` - channels from the named profile in config.
3. Profile in config with `default = true`.
4. All configured channels.

`--skip <names>` filters from any of the above.

```bash
agent-notify "build done"                              # default profile or all channels
agent-notify --profile error "5 critical alerts"       # error profile
agent-notify --to tg-personal "ack"                    # only Telegram
agent-notify --profile error --skip signal "minor"     # error profile minus Signal
```

## Hook integrations

### Claude Code (`~/.claude/settings.json`)

Generate the snippet:

```bash
agent-notify hooks print claude-code --profile agent-stop
```

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [
        { "type": "command", "command": "agent-notify --hook claude-code-stop --profile agent-stop" }
      ]
    }],
    "Notification": [{
      "hooks": [
        { "type": "command", "command": "agent-notify --hook claude-code-notification --profile agent-stop" }
      ]
    }]
  }
}
```

### Claude Desktop

Not applicable - Claude Desktop does not expose a hook surface that runs local commands. Use the Claude Code integration instead, or call `agent-notify` from a shortcut/script bound to whatever event you care about.

### OpenClaw

OpenClaw has its own multi-channel delivery built in, so you typically would not wire `agent-notify` for OpenClaw's own events. If you do want to use it (e.g., uniformity across all your agents), call it from a plugin's `agent_end` hook:

```typescript
api.on("agent_end", async (event, ctx) => {
  const proc = spawn("agent-notify", ["--hook", "custom", "--profile", "agent-stop"]);
  proc.stdin.write(JSON.stringify({
    title: "OpenClaw session ended",
    body: `Session ${event.sessionId} done`,
    source: "openclaw",
  }));
  proc.stdin.end();
});
```

### Hermes Agent

Same pattern as OpenClaw - wire `agent-notify` to whichever scheduled-task or session-end hook Hermes exposes in your version. Pass canonical JSON via stdin and use `--hook custom` (the default).

### Codex CLI (`~/.codex/config.toml`)

Generate the snippet:

```bash
agent-notify hooks print codex --profile agent-stop
```

```toml
notify = ["agent-notify", "--hook", "codex-notify", "--profile", "agent-stop"]
```

## Adding a custom hook source (escape hatch)

If a built-in adapter ever breaks because an upstream tool changes its event schema, write a small shell wrapper that extracts the fields you want and pipes canonical JSON to `agent-notify`:

```bash
#!/usr/bin/env bash
# my-tool-notify.sh - wrapper for some-future-agent
event=$(cat)
body=$(echo "$event" | jq -r '.message_field // "(no message)"')
title=$(echo "$event" | jq -r '.title_field // "MyTool"')
jq -n --arg t "$title" --arg b "$body" \
  '{title: $t, body: $b, source: "my-tool"}' \
  | agent-notify --profile agent-stop
```

Then point the upstream tool's hook config at `my-tool-notify.sh` instead.

## Exit codes

- `0` - all sends succeeded
- `1+` - number of channel sends that failed (other channels still received the message)
- `2` - config or input error before any send was attempted

## Channel formatting

| Channel | Format |
|---------|--------|
| Discord | Embed with title + body. Color by level (info=blue, warn=yellow, error=red, success=green). Tags as inline fields. Source as footer. |
| Telegram | Markdown V2. Level emoji prefix (ℹ️ / ⚠️ / 🚨 / ✅). Title bolded. Tags as italicized footer. |
| Signal | Plain text. Level emoji prefix. Title on its own line. Tags as `[tag1, tag2]` footer. |

## Limitations (v1)

- No retry queue. A rate-limited or down channel means dropped notification.
- No templating. The canonical message goes through as-is.
- Three channels only. Adding more is straightforward (one file in `internal/channels/`).

## License

MIT - see [LICENSE](LICENSE).
