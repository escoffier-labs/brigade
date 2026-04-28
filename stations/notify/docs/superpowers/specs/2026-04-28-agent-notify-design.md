# agent-notify - Design Spec

**Date:** 2026-04-28
**Status:** Approved (brainstorming complete; ready for implementation plan)

## Purpose

A single static binary that dispatches notifications from any source on the host (Claude Code hooks, Codex CLI hooks, manual CLI calls, cron jobs, n8n workflows over SSH) to one or more user-controlled channels (Discord, Telegram, Signal, plus future channels).

Privacy-first: no cloud round-trip, no telemetry, no Anthropic involvement, no third-party push infrastructure. All HTTP calls go to user-configured endpoints (Discord webhook, Telegram Bot API, the user's self-hosted Signal CLI relay) and nowhere else.

The motivation is concrete: the user has chosen to keep `DISABLE_TELEMETRY=1` set globally to avoid Anthropic eligibility/telemetry traffic. That choice disables Claude Code's built-in mobile push notification feature (which routes through Anthropic's notification infrastructure). `agent-notify` provides equivalent UX without the data-flow tradeoff.

## Scope

### In scope (v1)

- One static Go binary, installable as `~/bin/agent-notify` (or anywhere on `$PATH`)
- Three channel adapters: Discord (webhook), Telegram (Bot API), Signal (signal-cli REST API)
- Three input modes (auto-detected): plain string, canonical JSON, structured hook event
- Built-in hook adapters for the common cases: Claude Code Stop, Claude Code Notification, Codex notify
- Hybrid TOML + environment-variable config with env-only fast path (works with no config file)
- Hybrid routing model: explicit `--to`, named `--profile`, default profile, fallback-to-all
- Per-channel best-effort error handling with structured stderr output and informative exit codes

### Out of scope (v1)

- Retry queue or persistent state (rate-limited or down channels are dropped, not queued)
- Message templating engine
- Additional channels (Pushover, ntfy, Slack, email, generic webhook) - straightforward to add later
- Rate limiting or dedup (the n8n failure classifier already handles dedup at its source)
- Auto-update / self-update logic - install path is "build and copy"

## Architecture

```
       stdin / args ──►┌─────────────────────┐
                       │   agent-notify      │
   --hook claude-code- │  ┌───────────────┐  │
   stop / codex-notify │  │ adapter layer │  │       ┌──► Discord
   / custom        ───►│  │ (input shape  │  ├───────┼──► Telegram
                       │  │ → canonical)  │  │       └──► Signal
                       │  └───────┬───────┘  │
                       │  ┌───────▼───────┐  │
                       │  │   router      │  │
                       │  │ (--to /       │  │
                       │  │  --profile /  │  │
                       │  │  default /    │  │
                       │  │  all)         │  │
                       │  └───────┬───────┘  │
                       │  ┌───────▼───────┐  │
                       │  │  channel      │  │
                       │  │  adapters     │  │
                       │  └───────────────┘  │
                       └─────────────────────┘
```

Three internal layers, each independently testable:

1. **Adapter layer** - converts heterogeneous input (string, canonical JSON, hook event JSON) to a canonical message struct.
2. **Router** - resolves the canonical message + flags + config into a final list of channels.
3. **Channel adapters** - one per channel type; each takes a canonical message + channel config and performs the HTTP call.

Each layer can be unit-tested without the others present.

## Canonical message schema

```json
{
  "title":  "string (optional)",
  "body":   "string (required)",
  "level":  "info | warn | error | success (optional, default: info)",
  "source": "string (optional, free-form, e.g., 'claude-code', 'codex', 'cron', 'wazuh')",
  "tags":   ["array", "of", "strings", "(optional)"]
}
```

Built-in adapters convert their inputs to this shape. Custom callers (any future hook, n8n nodes, manual scripts) speak this shape directly via stdin.

## Input adapters

### Auto-detection (default, no `--hook` flag)

- If stdin is empty and a positional argument is provided → treat the argument as plain string body
- If stdin is non-empty and parses as valid JSON matching the canonical schema → use as-is
- If stdin is non-empty plain text → treat as body
- If stdin is non-empty JSON that does NOT match canonical schema → error with helpful message

### Built-in hook adapters

All built-in adapters read **defensively**: try multiple known field names, ignore unknown fields, fall back to sensible defaults when fields are missing. This means most upstream schema bumps (added fields, renamed-with-aliases) do not break the adapter.

| Flag | Source | What it extracts |
|------|--------|------------------|
| `--hook claude-code-stop` | Claude Code `Stop` hook event JSON on stdin | `cwd`, `session_id`, transcript snippet → canonical `{title: "Claude Code session ended", body: "<cwd>: <last message excerpt>", source: "claude-code"}` |
| `--hook claude-code-notification` | Claude Code `Notification` hook event JSON on stdin | `message`, `cwd` → canonical `{title: "Claude Code", body: "<message>", source: "claude-code"}` |
| `--hook codex-notify` | Codex CLI notify event JSON on stdin | session id + last-message → canonical `{title: "Codex session", body: "<excerpt>", source: "codex"}` |
| `--hook custom` (default) | Plain string OR canonical JSON on stdin | Per auto-detection above |

### Escape hatch when an adapter breaks

If an upstream tool (Codex is the most volatile candidate) makes a structurally different schema change that the built-in adapter cannot handle defensively, the user writes a small shell wrapper:

```bash
#!/usr/bin/env bash
# codex-notify-wrapper.sh
exec_event=$(cat)
body=$(echo "$exec_event" | jq -r '.message // .body // "(no message)"')
title=$(echo "$exec_event" | jq -r '.title // "Codex"')
jq -n --arg t "$title" --arg b "$body" '{title: $t, body: $b, source: "codex"}' \
  | agent-notify
```

The wrapper extracts what the user wants and pipes canonical JSON to `agent-notify`. The user is back in business in minutes; no `agent-notify` release required.

## Configuration

### Location

`~/.config/agent-notify/config.toml`

### No-config fast path

If the config file does not exist, `agent-notify` operates in env-only mode:

- Reads `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, `SIGNAL_CLI_URL` + `SIGNAL_FROM` + `SIGNAL_TO` from the environment
- For each set of variables fully present, registers an implicit channel
- Creates one implicit "default" profile that fans to all configured channels

This means the simplest install (drop binary, set one or more channel env vars, run) works immediately without a config file.

### TOML schema (when present)

```toml
# Channels - one named definition per channel instance.
# Multiple instances of the same type are allowed (e.g., two Discord webhooks).
# Secrets stay in env vars; this file holds only structure + env-var references.

[channels.telegram-personal]
type = "telegram"
bot_token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env   = "TELEGRAM_CHAT_ID"

[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[channels.signal-personal]
type = "signal"
url_env  = "SIGNAL_CLI_URL"   # e.g., http://signal-cli:8080/v2/send
from_env = "SIGNAL_FROM"      # sender phone number
to_env   = "SIGNAL_TO"        # recipient UUID or phone

# Profiles - named groups that select channels and (optionally) override formatting.

[profiles.agent-stop]
channels = ["telegram-personal", "discord-main"]
default  = true   # Used when no --profile and no --to are passed.

[profiles.error]
channels = ["telegram-personal", "discord-main", "signal-personal"]
prefix   = "🚨 "  # Prepended to body across all channels.

# Optional: timeout and retries per channel (v1 ignores retries; reserved for v2).
[defaults]
timeout_seconds = 10
```

### Env vars referenced by config

Config does NOT contain secrets. Channels reference env-var names; the binary reads those at runtime. This preserves the user's existing `.env` workflow (`~/.openclaw/workspace/.env` exported by `.bashrc`) and keeps secrets out of any committed file.

## Routing precedence

When `agent-notify` runs, the channel selection flows in this order (first match wins):

1. **`--to <names>`** - explicit comma-separated channel names. Overrides everything else.
2. **`--profile <name>`** - named profile from config; uses its `channels` list.
3. **Config-defined default profile** (the profile with `default = true`).
4. **Fallback to all configured channels** - implicit default if no profile is set anywhere.

Then `--skip <names>` (comma-separated channel names) filters from the resolved list.

Examples:

```bash
agent-notify "build done"                        # → default profile or all channels
agent-notify --profile error "wazuh: 5 critical" # → channels in [profiles.error]
agent-notify --to telegram-personal "ack"        # → only Telegram, regardless of profile
agent-notify --profile error --skip signal-personal "..." # → error profile minus Signal
```

## Channel formatting

Same canonical message goes to all channels. Each channel adapter formats appropriately for its medium:

| Channel | Format |
|---------|--------|
| Discord | Embed with title + body. Color by level (info=blue, warn=yellow, error=red, success=green). Tags as inline fields. Source as footer. |
| Telegram | Markdown V2. Level emoji prefix (ℹ️ / ⚠️ / 🚨 / ✅). Title bolded. Tags as italicized footer. |
| Signal | Plain text. Level emoji prefix. Title on its own line. Tags as `[tag1, tag2]` footer. |

Per-profile overrides (e.g., `prefix = "🚨 "` in `[profiles.error]`) prepend to the body across all channels in that profile.

## Error handling

- **Per-channel best-effort.** Each channel send is an independent goroutine; one channel failing does not block or skip the others.
- **Structured stderr on failure.** A failed send emits a single line: `[agent-notify] FAIL channel=<name> type=<discord|telegram|signal> error=<short error>`.
- **Exit code = number of failed channels.** Exit 0 = all sends succeeded. Exit N = N channels failed. Allows callers (cron, scripts, hooks) to detect partial success.
- **No retries in v1.** A rate-limited or down channel results in a dropped notification. Retry queue with backoff is reserved for v2 if it turns out to matter in practice.
- **Per-channel timeout.** 10 seconds default, configurable via `[defaults] timeout_seconds`. Each channel's HTTP call is wrapped with this timeout independently.
- **Config errors fail loud at startup.** Missing required env vars for a referenced channel, malformed TOML, or unknown channel type cause an early exit with a clear message before any HTTP calls are attempted.

## Repo + install

- **New repo:** `solomonneas/agent-notify`
- **Build:** `go build -o agent-notify ./cmd/agent-notify`
- **Install:** drop binary in `~/bin/` (already on `$PATH` per existing setup)
- **Cross-build:** Makefile targets for AMD64 and ARM64 (covers typical Linux servers, Macs, LXC containers, Pi targets)
- **License:** MIT (matches other small tools in this stack)
- **README** documents all five install paths per the project convention: Claude Code hooks, Claude Desktop (N/A - note this), OpenClaw delivery integration, Hermes Agent integration, Codex CLI notify. Also includes the no-config fast-path quickstart and a TOML reference.
- **Privacy posture statement** in README: no telemetry, no update checks, no third-party calls beyond user-configured channel endpoints.

## Hook integration examples

### Claude Code (`~/.claude/settings.json`)

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

### Codex CLI (`~/.codex/config.toml`)

```toml
notify = ["agent-notify", "--hook", "codex-notify", "--profile", "agent-stop"]
```

### Manual / cron / n8n SSH

```bash
agent-notify "build finished"
agent-notify --profile error "wazuh: 5 high-severity alerts in last hour"
echo '{"title":"Backup","body":"restic snapshot complete","level":"success"}' | agent-notify
```

## Privacy guarantees (committed in README + verified by tests)

- No telemetry endpoints. The binary never makes HTTP calls to anything not explicitly configured by the user.
- No update checks at startup or runtime.
- No persistent state. No state file, no log file, no cache. Only stderr output (which the caller controls).
- No phoning home, ever.

A simple test asserts the binary's outbound HTTP behavior: in a sandbox with no channels configured, `agent-notify "test"` makes zero HTTP calls.

## Open questions for the implementation plan

(None blocking - these are decisions the plan will resolve.)

- Test harness: `testify` or stdlib only? Lean: stdlib for v1.
- Go module name: `github.com/solomonneas/agent-notify`.
- Minimum Go version: 1.22 (for new `slices`/`maps` stdlib helpers if used).
- Whether to ship a `--version` flag and how it's stamped (build-time `-ldflags`).

## Success criteria

- `agent-notify "test message"` works on a clean install with one channel env var set, no config file.
- A Claude Code Stop hook calling `agent-notify --hook claude-code-stop --profile agent-stop` posts to Telegram + Discord (per the example profile) and exits 0.
- Bringing one channel down (kill Signal CLI) results in: other channels still receive the message, exit code is 1, stderr contains the failed channel.
- Adding a new channel of an existing type (second Discord webhook) requires only a TOML edit, no code change.
- Adding a new channel of a new type (e.g., Pushover) requires one new file in `internal/channels/` and a registration line.
- The binary makes zero HTTP calls to anything not in the user's channel config.
