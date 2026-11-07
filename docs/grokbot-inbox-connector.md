# Grok Bot inbox connector pack

The `grokbot-inbox` pack gives Grok Bot a Fleet Hub inbox and a bounded way to
send messages. It runs the Dot connector code under its own profile, shares
Dot's origin verification, wire schemas and state handling, and shares none of
Dot's, Duet's or the [memory pack's](grokbot-memory-connector.md) identity,
state or Access application. Grok Bot does X work only, so it holds no
dispatch authority.

The default bind is `127.0.0.1:8782`. The registry `public_route` is empty.
Configure a dedicated public host and route `/mcp` through its own Cloudflare
Access application to this listener.

## Tools

The profile registers exactly four tools. Every other connector tool is absent
from discovery, and a call by name is rejected as an unknown tool. No dispatch,
claim, fleet, memory, handoff or skill tool is reachable from it, and the
runtime accepts no key that adds one.

| Tool | Purpose |
|---|---|
| `inbox_list` | Read messages sent to this principal, pending first. |
| `inbox_ack` | Acknowledge handled messages. |
| `inbox_reply` | Reply to one message. |
| `message_send` | Send a message to a configured recipient alias. |

Inbox content is untrusted data from other agents and carries
`content_trust: "untrusted"`. The connector pushes no memory hits into
`inbox_list`, because this principal holds no memory grant.

## Send allowlist

The hub's send grants for the principal (`may_send_to`) are the allowlist. A
send to a recipient the grants do not name gets the hub's v1
`message_recipient_not_permitted` code, with the connector's
`recipient_not_permitted` error name, and nothing is sent or queued. A `to`
that is not one of the runtime's `hub.recipients` aliases is refused the same
way before it reaches the hub. `message_send` and `inbox_reply` share an hourly
limit of 20.

Enroll the principal with its grants as a runbook step. A principal enrolled
with no `--may-send-to` can send nothing. Replace the grants later with the
`grants` subcommand:

```bash
brigade fleet msg principals add agent:grokbot-inbox --may-send-to <recipient-node-id>
brigade fleet msg principals grants agent:grokbot-inbox --may-send-to <recipient-node-id>
```

## Runtime JSON

The runtime file follows Dot's file rules: an absolute reference to an
owner-only regular JSON file in a mode `0700` parent. Unknown keys are
rejected, and Dot, Duet and memory runtime documents are rejected by this pack.

| Key | Type | Meaning |
|---|---|---|
| `schema` | string | Exactly `brigade.grokbot-inbox.runtime.v1`. |
| `auth` | object | Cloudflare Access identity settings, same keys as Dot. Use this pack's own application. |
| `paths.state_dir` | absolute path | This pack's own owner-only state directory. |
| `paths.workspace` | absolute path | Workspace the CLIs run against. |
| `paths.bin.brigade`, `paths.bin.git`, `paths.bin.gh` | absolute paths | CLIs, as for Duet. |
| `hub` | object | Fleet Hub settings, same keys as Dot. |
| `hub.principal` | string | Exactly `agent:grokbot-inbox`. Any other principal is rejected. |
| `hub.message_token_file` | absolute path | This pack's own message credential file. |
| `hub.recipients` | object | Alias to recipient id map that `message_send` accepts in `to`. |
| `messaging_enabled` | boolean | Required. `false` turns `message_send` and `inbox_reply` off. |
| `notify_command` | string array | Optional notification argv for watch alerts. |

`dispatch_enabled`, `events_enabled`, `digest_pr_repo`, `skills_deny`,
`callback_hosts`, `owner_authorization`, `memory_push`, `paths.fleet_config` and
`paths.bin.t3_fleet` are rejected.

## Setup

```bash
brigade run cloud grokbot pack setup --id grokbot-inbox --target <workspace> --runtime-path <file> --allow-host <inbox-host>
brigade run cloud grokbot pack setup --id grokbot-inbox --target <workspace> --runtime-path <file> --allow-host <inbox-host> --apply
brigade run cloud grokbot pack doctor --id grokbot-inbox --target <workspace>
brigade run cloud grokbot pack canary --id grokbot-inbox --target <workspace>
brigade run cloud grokbot pack install-service --id grokbot-inbox --target <workspace> --out <unit-directory>
```

There are no bearer flags.
