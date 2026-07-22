# Contributing to agent-notify

`agent-notify` is a privacy-first notification dispatcher for AI coding agents: a single static Go binary that sends to Discord, Telegram, and Signal with no telemetry and no third-party push service. Patches are welcome. Before you start, please skim this file so we both spend our time on the right things.

## What kinds of changes land easily

- **Bug fixes** in routing, channel adapters, hook adapters, `doctor`, `status`, `init`, or `hooks print`.
- **A new channel adapter** under `internal/channels/`, following the existing Discord/Telegram/Signal pattern (one `Channel` implementation, formatting documented in the README table, env-var-only secrets).
- **A new hook adapter** under `internal/adapter/` for an agent that emits a different event JSON shape.
- **Doctor checks** that catch a real, observed misconfiguration.
- **Test coverage** for any of the above, especially the privacy invariants.

## What needs a conversation first

- **A breaking change to the canonical message model** (`title`, `body`, `level`, `tags`, `source`) or to the config schema. These are the public surface and renaming them later is painful. Open an issue first describing the user story.
- **Anything that adds a runtime dependency.** The tool has a single small dependency (`BurntSushi/toml`) on purpose, and we want to keep the footprint near zero. New runtime deps need a clear justification.
- **Anything that writes to disk by default** (a cache, a log file, a state file). The privacy posture promises none of these. A feature that needs persistence must be opt-in and documented.

## What does not land

- **Telemetry, update checks, or any outbound HTTP to a host the user did not configure.** This is the whole point of the project. `cmd/agent-notify/privacy_test.go` guards it and CI will fail if you break the invariant.
- **Personal details, hostnames, real IPs, account IDs, tokens, or unredacted absolute paths** in code, tests, or docs. Use `192.0.2.x` (RFC 5737) for example IPs and `EXAMPLE` placeholders for tokens.
- **AI co-authorship trailers on commits** (`Co-Authored-By: <model>`). Conventional commits only.

## Local dev

```bash
git clone https://github.com/escoffier-labs/agent-notify.git
cd agent-notify
go build ./...
go vet ./...
go test -race ./...
```

The `Makefile` wraps the common targets (`make build`, `make test`, `make install`).

## Testing the wiring without a live send

`doctor` and `status` validate config and channel env vars without sending anything, so you can exercise routing changes safely:

```bash
agent-notify init                 # writes a starter config
agent-notify doctor               # checks config + env, no network
agent-notify status --json        # resolved routing as JSON
```

## Adding a channel adapter

1. Add `internal/channels/<channel>.go` implementing the `Channel` interface (`Name`, `Type`, `Send`).
2. Wire its config fields and env-var lookups into `buildRegistry` in `cmd/agent-notify/main.go`.
3. Document the channel's formatting in the README "Channel formatting" table.
4. Add a test that asserts the adapter encodes a message correctly and never logs a secret.

## Filing issues

Please use the templates under `.github/ISSUE_TEMPLATE/`. The bug form asks for the version, OS, and full output up front so we can reproduce without a round trip. Before posting output, remove tokens, webhook URLs, private hostnames, and unredacted absolute paths.

For a security problem, do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the MIT License, same as the rest of the repo.
