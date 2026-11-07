# Grok Bot memory connector pack

The `grokbot-memory` pack gives Grok Bot a read view of hub memory and a way to
submit handoffs for review. It runs the Dot connector code under its own
profile, like the [duet pack](duet-connector.md), and shares Dot's origin
verification, wire schemas and state handling. It shares none of Dot's or
Duet's identity, state or Access application. Its inbox side is a separate
principal and pack: see the [Grok Bot inbox pack](grokbot-inbox-connector.md).

The default bind is `127.0.0.1:8781`. The registry `public_route` is empty.
Configure a dedicated public host and route `/mcp` through its own Cloudflare
Access application to this listener.

## Tools

The profile registers exactly three tools. Every other connector tool is absent
from discovery, and a call by name is rejected as an unknown tool. No dispatch,
claim, fleet, skill, messaging or capabilities tool is reachable from it.

| Tool | Purpose | Writes |
|---|---|---|
| `memory_search` | Keyword search of the hub memory cards this principal may read. Returns titles, tags and paths. | No |
| `memory_card` | Read one card in bounded chunks. | No |
| `handoff_write` | Submit one Memory Handoff to the hub review queue. | Hub `memory_handoffs` only |

`memory_search` and `memory_card` run the same `brigade memory` commands Dot
and Duet use, with the prompt-load scan on. The difference is the credential.
The connector points those commands at this pack's own message token
(`hub.message_token_file`) instead of the host's node credential, so the
principal's grants are the boundary, not the node's wider defaults. Set the
workspace `[hub]` table to `mode = "read"` with `read_scopes`, and keep the
workspace dedicated to this pack with no local cards: a local card is served
without any hub grant. With hub memory on and no hub URL the tools fail closed
with `hub_credential_unavailable` and never fall back to the node credential.

`handoff_write` posts the handoff to the hub's `handoff` action as the
principal. The text lands in `memory_handoffs` in the `pending` state for
review. It never writes a card, a local file or a file inbox. A credential-shaped
value is refused before anything leaves the host, and the hub scans the text
again. The same content inside a ten-minute window carries one idempotency id,
so a retry after a lost answer does not file it twice. The tool answers
`{ok, handoff_id, destination: "memory_handoffs", state: "pending_review"}`.

The listener builds no T3 fleet client and runs no Hub inbox poll.

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
  and returns none of the text. A refused search match keeps its hub `scope` when that scope is valid and scans clean.

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
| `paths.workspace` | absolute path | Workspace whose `[hub]` table names the read scopes. Keep it dedicated to this pack, with no local cards. |
| `paths.bin.brigade`, `paths.bin.git`, `paths.bin.gh` | absolute paths | CLIs, as for Duet. |
| `paths.bin.octopool` | absolute path | Optional. |
| `hub` | object | Fleet Hub settings, same keys as Dot. |
| `hub.principal` | string | Exactly `agent:grokbot-memory`. Any other principal is rejected. |
| `hub.message_token_file` | absolute path | This pack's own message credential file. |
| `notify_command` | string array | Optional notification argv for watch alerts. |

`messaging_enabled`, `dispatch_enabled`, `events_enabled`, `digest_pr_repo`,
`skills_deny`, `callback_hosts`, `owner_authorization`, `paths.fleet_config`
and `paths.bin.t3_fleet` are rejected. The Hub principal is the identity every
memory read and handoff is made under, and startup, doctor and watch prove it
and its isolation from Dot. It is never used to read or send messages.

## Grants

A message principal holds no memory grant until the hub admin adds one, so
the runbook step below is required before the pack can answer. The hub's
admin-only `grant` action on `POST /memory` sets it. Grant the memory
principal `read` on `fleet` and the `handoff` right, and nothing else:

```json
{"action": "grant", "principal": "agent:grokbot-memory", "scope": "fleet", "rights": ["read", "handoff"], "client_event_id": "<26-character time-ordered id>"}
```

Set the workspace `[hub]` table to `mode = "read"` and `read_scopes = ["fleet"]`.
A collection scope is never granted by default or by a wildcard, so the pack
cannot read the imported Cerebro vault (`collection:vault/cerebro`) until the
operator adds a second grant naming that exact scope with `read`, and lists the
scope in `read_scopes`. Do that only when Grok Bot should see the vault.
Without it, search results and card reads leave the collection out.

## Retired vault-backed tools

The `cerebro-memory` pack and its five `cerebro_*` tools (`cerebro_search`,
`cerebro_show`, `cerebro_propose`, `cerebro_proposal_status`,
`cerebro_health`) are retired. They read a vault through a separate CLI and
bypassed hub grants. After the vault import into hub memory, the memory pack
answers the same questions from the hub, and `handoff_write` replaces
`cerebro_propose` for anything worth keeping. To migrate an installed
instance, stop and disable the `brigade-grokbot-cerebro-memory.service` unit,
delete `.brigade/grokbot/packs/cerebro-memory.json` from the workspace, and
point the Grok Bot connector at the `grokbot-memory` host instead.

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
