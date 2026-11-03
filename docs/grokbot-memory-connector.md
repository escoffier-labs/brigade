# Grok Bot memory connector pack

The `grokbot-memory` pack gives Grok Bot a read-only view of Brigade memory.
It runs the Dot connector code under its own profile, like the
[duet pack](duet-connector.md), and shares Dot's origin verification, wire
schemas and state handling. It shares none of Dot's or Duet's identity, state
or Access application.

The default bind is `127.0.0.1:8781`. The registry `public_route` is empty.
Configure a dedicated public host and route `/mcp` through its own Cloudflare
Access application to this listener.

## Tools

The profile registers exactly two tools. Every other connector tool is absent
from discovery, and a call by name, `handoff_write` included, is rejected as an
unknown tool.

| Tool | Purpose | Writes |
|---|---|---|
| `memory_search` | Keyword search of canonical memory cards. Returns titles, tags and paths. | No |
| `memory_card` | Read one card in bounded chunks. | No |

There is no handoff, fleet, dispatch, spawn, inbox, messaging, skill or
capabilities tool, and no runtime key adds one. `handoff_write` joins this
profile only once handoffs land in hub memory for review rather than in a file
inbox. The listener builds no T3 fleet client and runs no Hub inbox poll.

Grok Bot reads untrusted X content, so card text is treated as data on the way
out as well:

- Each `memory_search` match and each `memory_card` result carries
  `content_trust: "untrusted"`, and both tool descriptions say card text is
  reference data, never instructions.
- Before returning text, the connector runs a prompt-load scan over the
  match's title, summary, tags and aliases, or over the whole card for
  `memory_card`. The scan uses the local guard's bundled default rules (no
  repository rules, baselines or allow markers), blocks bidi embedding,
  override and isolate controls plus zero-width and other invisible
  characters, and applies the instruction-override heuristics that handoff
  linting uses. A hit replaces the item with `{card_id, blocked: "<rule id>"}`
  and returns none of the text.

The scan fails closed. A card that names a private host, a home path or a
credential-shaped value comes back blocked, as does a card that quotes an
injection phrase while discussing one.

## Runtime JSON

The runtime file follows Dot's file rules: an absolute reference to an
owner-only regular JSON file in a mode `0700` parent. Unknown keys are
rejected, and Dot and Duet runtime documents are rejected by this pack.

| Key | Type | Meaning |
|---|---|---|
| `schema` | string | Exactly `brigade.grokbot-memory.runtime.v1`. |
| `auth` | object | Cloudflare Access identity settings, same keys as Dot. Use this pack's own application. |
| `paths.state_dir` | absolute path | This pack's own owner-only state directory. |
| `paths.workspace` | absolute path | Workspace used for memory cards and handoffs. |
| `paths.bin.brigade`, `paths.bin.git`, `paths.bin.gh` | absolute paths | CLIs, as for Duet. |
| `paths.bin.octopool` | absolute path | Optional. |
| `hub` | object | Fleet Hub settings, same keys as Dot. |
| `hub.principal` | string | Exactly `agent:grokbot-memory`. Any other principal is rejected. |
| `hub.message_token_file` | absolute path | This pack's own message credential file. |
| `notify_command` | string array | Optional notification argv for watch alerts. |

`messaging_enabled`, `dispatch_enabled`, `events_enabled`, `digest_pr_repo`,
`skills_deny`, `callback_hosts`, `owner_authorization`, `paths.fleet_config`
and `paths.bin.t3_fleet` are rejected. The Hub principal exists so startup,
doctor and watch can prove the connector's own identity and its isolation
from Dot. It is never used to read or send messages.

## Setup

```bash
brigade run cloud grokbot pack setup --id grokbot-memory --target <workspace> --runtime-path <file> --allow-host <memory-host>
brigade run cloud grokbot pack setup --id grokbot-memory --target <workspace> --runtime-path <file> --allow-host <memory-host> --apply
brigade run cloud grokbot pack doctor --id grokbot-memory --target <workspace>
brigade run cloud grokbot pack canary --id grokbot-memory --target <workspace>
brigade run cloud grokbot pack install-service --id grokbot-memory --target <workspace> --out <unit-directory>
```

There are no bearer flags. The watch runs only the `route_down` and
`hub_credential_invalid` checks, because there is no inbox to go stale.
