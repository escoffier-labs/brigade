# Fleet MCP Events

Part of #1887, slice 5. The fleet MCP station can offer the draft MCP Events
webhook mode to a hosted agent, so the agent wakes when something happens on the
fleet instead of polling the inbox. It ships off. Nothing here exposes the
station or the hub to the public internet, and no tunnel is configured. Putting
an endpoint in front of a hosted platform is a separate operator decision.

## What it does

Three methods on the fleet station, next to the existing tools:

| Method | Behavior |
|---|---|
| `events/list` | Returns the event catalog with each event's filter schema and payload schema. |
| `events/subscribe` | Creates or refreshes a subscription. The hub verifies the callback with a signed challenge before it stores anything. |
| `events/unsubscribe` | Removes a subscription by `id`, or by the original name, arguments and callback URL. Idempotent. |

The station also answers `server/discover`, and the shared transport in
`mcp_server.py` accepts protocol version `2026-07-28` next to `2024-11-05`,
`2025-03-26` and `2025-06-18`. A client that sends an unknown version is still
told `2025-06-18`. Over HTTP, a `2026-07-28` request must carry `Mcp-Method`
(and `Mcp-Name` for `tools/call`) matching the body, or it gets a 400 with
JSON-RPC code `-32020`. Older versions do not need those headers.

Delivery is webhook only. Polling, streaming and the draft's `gap` and
`terminated` notifications are not implemented, and every event has
`cursor: null` because none of them can be replayed.

## Events

| Event | Arguments | Payload `data` |
|---|---|---|
| `message.created` | `recipient` (optional, must equal the caller) | `message_id`, `recipient`, `sender`, `priority`, `created_at`, and `kind` when set |
| `claim.expired` | `owner_node` (optional) | `owner_node`, `target_digest`, `expired_at`, and `target` when it is a short token without whitespace |
| `run.failed` | `node_id` (optional) | `node_id`, and `run_id`, `exit_status`, `failed_at` when present and well formed |

Payloads are thin. They never carry a message subject, body, thread, label or
reference. Free text that a node chose is included only when it matches a strict
token pattern, so a claim target such as `owner/repo` goes through and a sentence
does not. The agent reads full records with its own authenticated tools
(`fleet_inbox`, `fleet_claims`), where peer text is already fenced as untrusted.

## Turning it on

Both sides are off by default.

1. Hub: start `brigade fleet serve` with
   `BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS` set to a comma separated list of exact
   host names that may receive webhooks, for example the hosted platform's
   connector host. No hosts means events are off. Wildcards, URLs and suffix
   matches are refused at startup. `BRIGADE_FLEET_EVENTS_ALLOW_LOOPBACK_HTTP=1`
   additionally allows plain `http` to loopback hosts that are also listed, for a
   local test receiver only.
2. Station: `brigade fleet mcp --events` (stdio, or `--http` on loopback). It
   advertises the `events` capability and forwards the three methods to the hub
   with the station's own principal credential. It never takes an identity from a
   request.

The hub schema moved to version 38 for the subscription, event and delivery
tables. They are created whether or not events are enabled, and stay empty
unless a subscription exists.

## Delivery contract

Webhooks follow Standard Webhooks: `webhook-id` equals the body's `eventId`,
`webhook-timestamp` is Unix seconds, and `webhook-signature` is
`v1,<base64 HMAC-SHA256>` over `eventId.timestamp.body` with the subscription
secret (`whsec_` plus 24 to 64 base64 bytes). `X-MCP-Subscription-Id` names the
subscription. A secret rotation signs with the old and new key for five minutes.

A `2xx` acknowledges. Failures retry with exponential backoff and a fresh
timestamp and signature, keeping the same event id, up to eight attempts. `410`
deletes the subscription. Other `4xx` responses (except `408` and `429`),
redirects and `413` are permanent for that event. Ten permanent failures in a row
drop the subscription. A subscription receives at most 120 deliveries an hour;
the rest wait. Subscriptions last at most seven days; `ttlMs` is clamped to
between one minute and seven days, and `null` does not mean unlimited.

## Threat model

Assets: fleet data (inbox contents, claims, run state), the hub's outbound
network position, and the principal credentials.

* **Who can subscribe.** Any active hub principal, as itself. `message.created`
  is limited to the caller's own inbox with no grant that widens it. For
  `claim.expired` and `run.failed`, an `agent:` principal receives only events
  for nodes its send grants name, the same visibility `fleet_claims` and
  `fleet_who` give it, checked again for every delivery. A revoked principal
  stops receiving and its subscriptions are deleted on the next tick.
* **Unsubscribe and takeover.** The subscription id is derived from the owner,
  URL, event and arguments, and every write and delete is keyed on the owning
  principal, so one principal cannot remove or overwrite another's subscription.
* **SSRF.** Callback hosts are an exact operator allowlist. The URL must be
  `https` on port 443 with no credentials or fragment. The hub resolves the name
  once, rejects the connection unless every answer is a public address
  (including IPv4-mapped IPv6), connects to that address, and verifies TLS
  against the original host name. Redirects, proxies and response decompression
  are not used, responses are capped at 16 KiB, and each socket operation and the
  response read are limited to ten seconds. Link-local and metadata addresses fail even when the operator
  allows loopback `http`. The same checks run for the verification challenge and
  for every delivery, so a name that later resolves somewhere private is refused
  at connection time.
* **Forged or replayed callbacks.** The challenge is single use, random and
  signed, and compared in constant time before anything is stored. Receivers can
  reject stale timestamps; the signature covers the event id, timestamp and exact
  body bytes.
* **Secret at rest.** The signing secret has to be recoverable to sign, so the
  hub stores it in its SQLite file, as the dot pack does. Protect that file like
  the hub token store. Secrets never appear in responses, logs or errors.
* **Data leaving the hub.** Only the thin payloads above. This is the main
  reason the payloads omit text.
* **Prompt injection through events.** Event data contains no instructions and
  no free text from peers. A compromised node cannot put a sentence into a
  payload through a claim target or run id, because those fields are dropped
  unless they are a short token.
* **Resource exhaustion.** Twenty subscriptions per principal, 200 per hub, four
  concurrent challenge verifications, a 120 per hour ceiling per subscription,
  24 hour event retention, and a bounded batch per tick.
* **Not covered.** Whether the callback host is trustworthy is the operator's
  call when they add it to the allowlist. TLS trust is the system store. Delivery
  is at least once; receivers must tolerate duplicates and reordering.

## What dots allows on event-triggered tasks

What the repo can verify: the dot pack (`docs/grokbot-dot.md`,
`src/brigade/dot/events.py`) already implements the same three methods and treats
an event as a thin wake. The agent is expected to call the inbox tool after
waking, not to act on the payload.

What the public OpenAI documentation says, retrieved 2026-10-22 from the MCP
Events guide (`developers.openai.com/plugins/build/mcp-events`) and the dots
pages on `learn.chatgpt.com`:

* A dot can be asked to respond to a supported event, and a connected source
  alone does not create a monitoring task.
* Dots work within the user's existing app permissions, built-in safeguards and
  automatic action review. A plugin must be connected and permitted for each
  action. Drafting a message does not grant permission to send it.
* In workspaces, administrators control which apps and actions are available and
  which need approval.

What this repo cannot verify: whether an event-triggered task may call a given
tool without a person approving it, how approval prompts behave when no one is
watching, and how a workspace's settings change either. Those depend on the
platform and on each workspace's configuration, and the platform documents them
only in general terms. Do not treat anything here as a statement about dots'
behavior beyond the points above, and test the actual approval path in the target
workspace before relying on an unattended action.

Until that is checked, keep the fleet side to notify, send and reply. The
operator decision of 2026-10-20 on the issue allows a remote connector to carry
event delivery plus message send and reply with no command execution. This slice
adds none of that exposure: no tunnel, no public route, no new tool that runs
commands.
